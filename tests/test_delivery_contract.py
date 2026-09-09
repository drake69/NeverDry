"""The delivery contract: three modes, three responsibilities.

Each delivery mode declares who answers for the water that leaves the pipe,
and therefore which witness the system may appeal to:

    estimated_flow  -> the user     (declared the flow rate; duration is the dose)
    flow_meter      -> NeverDry     (measures, so answers for its own measurement)
    volume_preset   -> the valve    (accepted the dose and closes itself)

The field case these tests encode (2026-09-08/09, zone "Giardino Melograno"):
a SONOFF SWV-ZFE publishes its volume counter on a fixed 300 s cadence rather
than per litre delivered. The open-verification window was a 90 s constant, so
the guard closed a healthy valve in 7 openings out of 10 -- the counter simply
had not spoken yet. Same valve, same plumbing, opposite verdicts depending on
where the device's clock fell.

Design notes: docs/design/flow-rate-provenance.md (a still meter qualifies an
action, it never refuses one) and docs/design/delivery-contract.md.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_METER_SENSOR,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_VALVE,
    CONF_ZONE_VOLUME_ENTITY,
    DELIVERY_MODE_VOLUME_PRESET,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.controller import IrrigationController
from never_dry.driver import DeliveryMode, OperationStatus, ZoneDriver
from never_dry.sensor import IrrigationZoneSensor

#: The cadence measured on the SWV-ZFE in the field, in seconds.
FIELD_METER_CADENCE_S = 300.0

#: The constant that closed healthy valves. Any window derived from a fake
#: meter must be able to exceed it, or the test cannot see the defect.
OLD_CONSTANT_S = 90.0


def _hass(meter_reading: float = 0.0) -> MagicMock:
    """A hass mock whose meter entity sits at ``meter_reading`` and never moves."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    state = MagicMock()
    state.state = str(meter_reading)
    state.attributes = {"unit_of_measurement": "L"}
    hass.states.get = MagicMock(return_value=state)
    hass.async_create_task = MagicMock()
    return hass


def _zone(
    mode: DeliveryMode,
    *,
    meter: str | None = "sensor.meter",
    flow_rate_lpm: float = 4.4,
    resolution_l: float | None = None,
    cadence_s: float | None = None,
) -> ZoneDriver:
    """A zone as the user configured it, with no fsm_config override.

    Passing ``fsm_config`` explicitly is what hides this defect in the existing
    suite: it bypasses the ``has_flow_meter = meter is not None`` derivation that
    the real integration uses. These tests must go through that derivation.
    """
    driver = ZoneDriver(
        _hass(),
        "switch.valve",
        delivery_mode=mode,
        flow_meter_sensor=meter,
        flow_rate_lpm=flow_rate_lpm,
        max_retries=0,
        backoff_s=(0.01,),
        name="testzone",
    )
    if resolution_l is not None:
        driver._session_flow.resolution_l = resolution_l
    if cadence_s is not None:
        driver._session_flow.refresh_cadence_s = cadence_s
    return driver


def _zone_sensor(hass_mock, di_sensor, **overrides):
    """A configured zone as the integration builds it, for the controller path.

    The driver-level ``_zone`` above answers "would the guard arm?". This one is
    needed for the other half of the contract, which is only decidable where the
    water is credited: the controller.
    """
    config = {
        CONF_ZONE_NAME: "TestZone",
        CONF_ZONE_VALVE: "switch.valve_test",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
    }
    config.update(overrides)
    return IrrigationZoneSensor(hass_mock, config, di_sensor)


def _frozen_meter(zone, meter_entity: str, reading: str = "100.0"):
    """states.get side effect: the meter never moves, the valve reads "on"."""

    def get_state(entity_id):
        if entity_id == meter_entity:
            state = MagicMock()
            state.state = reading
            state.attributes = {"unit_of_measurement": "L"}
            return state
        if entity_id == zone.valve:
            state = MagicMock()
            state.state = "on"
            return state
        return None

    return get_state


class TestCase1TheUserAnswersForTheWater:
    """estimated_flow: the user declared the rate, the duration is the dose.

    A meter may be configured -- it refines the design rate -- but it is an
    observer. If it could still close the valve, declaring a simple on/off valve
    and declaring a metered one would produce identical behaviour on opening,
    and the choice the user made would mean nothing.
    """

    def test_a_silent_meter_never_refuses_the_opening(self):
        """T1: the guard must not be armed at all in this mode."""
        driver = _zone(DeliveryMode.ESTIMATED_FLOW)
        assert driver.flow_guard_armed is False

    def test_it_holds_even_when_the_meter_is_declared_and_healthy(self):
        """T11: the presence of a meter is not a grant of authority over the valve."""
        driver = _zone(DeliveryMode.ESTIMATED_FLOW, resolution_l=1.0, cadence_s=14.0)
        assert driver.flow_guard_armed is False

    def test_the_meter_stays_an_observer(self):
        """T3: disarming the guard must not disconnect the meter.

        Removing the sensor from the configuration is the field workaround, and
        it costs the zone its learned flow rate for good. The fix must keep the
        observation and drop only the veto.
        """
        driver = _zone(DeliveryMode.ESTIMATED_FLOW)
        assert driver._flow_sensor_entity_id == "sensor.meter"


class TestCase2NeverDryAnswersForItsOwnMeasurement:
    """flow_meter: the guard is legitimate, but the threshold is the device's.

    A window that NeverDry picks is a claim about a device it has not measured.
    When the claim is wrong the valve pays, and the log blames the valve.
    """

    def test_the_window_comes_from_the_cadence_not_from_resolution_over_rate(self):
        """T4: the two derivations are made to disagree, and the cadence must win.

        A meter with a 1 L step on a 60 L/min zone can move its counter one
        second in: resolution/rate says a 10 s window is generous. But this
        meter only publishes every 60 s, so a valve opened just after a report
        waits a minute while watering normally.

            resolution / rate -> ~1 s   -> window would be 10 s (the floor)
            cadence           -> 60 s   -> window must exceed 60 s
        """
        driver = _zone(
            DeliveryMode.FLOW_METER,
            resolution_l=1.0,
            flow_rate_lpm=60.0,
            cadence_s=60.0,
        )
        window, verdict = driver.flow_verify_window()
        assert verdict is None, "a 60 s cadence is still short enough to guard"
        assert window > 60.0, (
            f"window of {window:.0f}s follows resolution/rate, not the cadence: "
            f"a healthy valve opened just after a report is closed while watering"
        )

    def test_a_cadence_too_long_to_guard_is_declared_inapplicable(self):
        """The field meter: 300 s of legitimate silence is past any useful guard.

        Stretching the window instead would make "commanded but dry" take five
        minutes to notice, and that detection is the only reason the guard
        exists. So it steps aside rather than growing without limit.
        """
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=6.0, cadence_s=FIELD_METER_CADENCE_S)
        _, verdict = driver.flow_verify_window()
        assert verdict is not None
        assert "300" in verdict, "the verdict should name the cadence that caused it"

    def test_an_unknown_cadence_cannot_arm_the_guard(self):
        """T5: absence of the defining quantity is not licence to invent one.

        This is the branch that produced every field failure. The existing test
        in test_session_flow.py asserts ``verdict is None`` here, which is the
        defect written down as an expectation.
        """
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=2.0)
        _, verdict = driver.flow_verify_window()
        assert verdict is not None, "no cadence means no threshold, so no guard"

    def test_a_prompt_meter_still_gets_a_tight_window(self):
        """A correct fix must not buy safety by making every window generous."""
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=1.0, cadence_s=14.0)
        window, verdict = driver.flow_verify_window()
        assert verdict is None
        assert window < OLD_CONSTANT_S


class TestTheFieldCaseDoesNotRecur:
    """T12: the regression test for the 2026-09-08 field failure.

    The valve is healthy. What varies is only the phase between the opening and
    the meter's 300 s grid, and NeverDry does not control that phase: a schedule
    fires when it fires, and the device's clock is its own.

    So the window must cover the worst case, which is opening one instant after
    a report -- nearly a full cadence of silence with water already flowing.
    Anything less is a guard whose verdict depends on a coin toss: with the 90 s
    constant against a 300 s cadence it passed 90/300 of the time, which is the
    7-failures-in-10 measured in the field.
    """

    def test_the_window_covers_a_full_cadence_of_silence(self):
        driver = _zone(
            DeliveryMode.FLOW_METER,
            resolution_l=6.0,
            cadence_s=FIELD_METER_CADENCE_S,
        )
        window, verdict = driver.flow_verify_window()
        assert verdict is not None or window >= FIELD_METER_CADENCE_S, (
            f"window of {window:.0f}s against a {FIELD_METER_CADENCE_S:.0f}s cadence: "
            f"a valve opened just after a report is closed while it is watering"
        )

    def test_the_old_constant_could_not_have_passed(self):
        """Guards the reasoning itself, so the constant cannot quietly return.

        Not a test of today's code: a statement that any window shorter than the
        cadence is unfit, whatever produced it.
        """
        assert OLD_CONSTANT_S < FIELD_METER_CADENCE_S

    @pytest.mark.parametrize("phase_fraction", [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
    @pytest.mark.parametrize("cadence_s", [60.0, FIELD_METER_CADENCE_S])
    def test_no_phase_of_the_meters_grid_may_refuse_a_healthy_valve(self, cadence_s, phase_fraction):
        """The same healthy valve, opened at ten points of the meter's grid.

        ``phase_fraction`` is how far into the reporting interval the valve
        opens, so the first tick is still ``cadence x (1 - fraction)`` away.
        NeverDry does not choose that phase: the schedule fires when it fires
        and the device's clock is its own, which is why a verdict that depends
        on it is a coin toss rather than a measurement.

        Two cadences on purpose, because they exercise the two halves of the
        rule and a single one would let the test pass for the wrong reason. At
        300 s the guard must stand down; at 60 s it stays armed, so the only way
        through is a window that outlasts the wait. The old derivation,
        resolution over rate, gives about 14 s for this meter and fails the
        second half at every phase but the last.
        """
        driver = _zone(
            DeliveryMode.FLOW_METER,
            resolution_l=1.0,
            cadence_s=cadence_s,
        )
        window, verdict = driver.flow_verify_window()
        wait_for_first_tick = cadence_s * (1.0 - phase_fraction)
        assert verdict is not None or window >= wait_for_first_tick, (
            f"opened {phase_fraction:.0%} into a {cadence_s:.0f}s grid, the first tick is "
            f"{wait_for_first_tick:.0f}s away and the window is {window:.0f}s: "
            f"a watering valve is closed and blamed"
        )


class TestFailuresAreAttributedToWhoeverCausedThem:
    """T10: our own inconclusive check must not be recorded as the device's fault.

    ACTUATION_FAILED reads as "the valve did not actuate", and is documented in
    valve-state-machine.md as a hydraulic or mechanical issue that retrying
    cannot unblock. When the real cause is that our window was shorter than the
    meter's cadence, that verdict names the wrong party -- which is also why the
    field diagnosis was slow: the log accused a valve that had done its job.
    """

    def test_an_unknown_cadence_yields_unverifiable_not_a_device_failure(self):
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=2.0)
        _, verdict = driver.flow_verify_window()
        assert verdict is not None
        assert "not applicable" in verdict or "unverifiable" in verdict.lower()


class TestTheCadenceIsActuallyLearned:
    """T16: the measurement must have a caller, or the guard never arms.

    A quantity nothing writes stays None for ever, and a guard that needs it
    silently never arms. That failure would look exactly like the fix working,
    which is why it gets its own test rather than being assumed.
    """

    @staticmethod
    def _meter_event(value: float):
        event = MagicMock()
        state = MagicMock()
        state.state = str(value)
        event.data = {"new_state": state}
        return event

    @pytest.mark.asyncio
    async def test_two_advances_while_open_teach_the_interval(self, monkeypatch):
        driver = _zone(DeliveryMode.FLOW_METER)
        driver._dispatch = AsyncMock()
        monkeypatch.setattr(type(driver), "is_open", property(lambda self: True))

        # One reading per advance: the first advance starts the clock, the
        # second closes the interval.
        clock = iter([1000.0, 1000.0 + FIELD_METER_CADENCE_S])
        monkeypatch.setattr("never_dry.driver.monotonic", lambda: next(clock))

        await driver._handle_flow_state(self._meter_event(10.0))  # first level, no movement yet
        await driver._handle_flow_state(self._meter_event(16.0))  # advance: starts the clock
        await driver._handle_flow_state(self._meter_event(22.0))  # advance: 300 s later

        assert driver._session_flow.refresh_cadence_s == pytest.approx(FIELD_METER_CADENCE_S)

    @pytest.mark.asyncio
    async def test_a_gap_between_irrigations_is_not_a_cadence(self, monkeypatch):
        """With the valve shut the counter stands still for hours.

        Taking that as the cadence would push the window past any useful guard
        and disable verification on a perfectly prompt meter.
        """
        driver = _zone(DeliveryMode.FLOW_METER)
        driver._dispatch = AsyncMock()
        monkeypatch.setattr(type(driver), "is_open", property(lambda self: False))
        monkeypatch.setattr("never_dry.driver.monotonic", lambda: 1000.0)

        await driver._handle_flow_state(self._meter_event(10.0))
        await driver._handle_flow_state(self._meter_event(16.0))

        assert driver._session_flow.refresh_cadence_s is None


class TestTheMeterIsClassifiedNotJustMeasured:
    """A cadence is a number; what the user needs is what it means.

    Two meters with the same 1 L step behave differently: one publishes per
    litre delivered, the other on a clock. The first can guard an opening, the
    second cannot, and no amount of tuning changes that. So the classification
    is published, not left for the reader to derive.
    """

    def test_a_prompt_meter_is_classified_as_publishing_per_volume(self):
        """1 L step at 4.4 L/min is a tick every ~14 s, and that is what is seen."""
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=1.0, cadence_s=14.0)
        assert driver.meter_refresh_kind == "volume"

    def test_the_field_meter_is_classified_as_publishing_on_a_clock(self):
        """A 2 L step at 4.4 L/min should tick every ~27 s. It reports every 300.

        The gap between what the volume would imply and what the meter does is
        the whole diagnosis: this is the SWV-ZFE.
        """
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=2.0, cadence_s=FIELD_METER_CADENCE_S)
        assert driver.meter_refresh_kind == "periodic"

    def test_without_a_resolution_there_is_nothing_to_compare(self):
        """Honest silence: the classification needs both quantities."""
        driver = _zone(DeliveryMode.FLOW_METER, cadence_s=FIELD_METER_CADENCE_S)
        assert driver.meter_refresh_kind is None

    def test_guard_usable_says_no_for_the_field_meter(self):
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=2.0, cadence_s=FIELD_METER_CADENCE_S)
        assert driver.meter_guard_usable is False

    def test_guard_usable_says_yes_for_a_prompt_meter(self):
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=1.0, cadence_s=14.0)
        assert driver.meter_guard_usable is True

    def test_guard_usable_says_no_in_a_mode_that_does_not_guard(self):
        """Not "could it", but "does it": in Case 1 nothing guards, however good the meter."""
        driver = _zone(DeliveryMode.ESTIMATED_FLOW, resolution_l=1.0, cadence_s=14.0)
        assert driver.meter_guard_usable is False

    def test_the_sample_count_is_published_so_a_lone_reading_is_visible(self):
        """One interval is not a cadence, and the user must be able to see that."""
        driver = _zone(DeliveryMode.FLOW_METER)
        assert driver.meter_refresh_samples == 0
        driver._session_flow.observe_refresh_interval(300.0)
        assert driver.meter_refresh_samples == 1


class TestTheSettleWaitFollowsTheMeterToo:
    """The last tick of a session lands after the valve is shut, by how much
    the meter decides.

    A fixed 30 s wait was already there for exactly this reason, and it is the
    same shape of defect as the verification window: a constant standing in for
    a property of the device. On the field meter the closing tick arrived three
    and a half minutes after the valve shut, so 30 s of patience missed it and
    the session's measured flow was computed from a truncated volume.

    The wait runs in a background task and never delays the session, so
    lengthening it costs nothing but a later diagnostic.
    """

    def test_a_prompt_meter_keeps_the_default_wait(self):
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=1.0, cadence_s=14.0)
        assert driver.settle_delay_s == pytest.approx(30.0)

    def test_the_field_meter_gets_a_wait_that_can_see_its_last_tick(self):
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=6.0, cadence_s=FIELD_METER_CADENCE_S)
        assert driver.settle_delay_s > FIELD_METER_CADENCE_S

    def test_an_unmeasured_cadence_falls_back_to_the_default(self):
        driver = _zone(DeliveryMode.FLOW_METER)
        assert driver.settle_delay_s == pytest.approx(30.0)

    def test_the_wait_is_capped_so_a_task_cannot_hang_around_for_ever(self):
        """A meter reporting hourly would otherwise leave a task pending all that time."""
        driver = _zone(DeliveryMode.FLOW_METER, cadence_s=3600.0)
        assert driver.settle_delay_s <= 600.0


class TestCase1CreditsWhatTheUserDeclared:
    """T2: in estimated_flow the declared rate is the answer, not a proposal.

    The other half of Case 1. That the guard cannot refuse the opening is only
    useful if the water then reaches the model: a session that runs and is
    credited nothing leaves the deficit standing, the zone is watered again
    tomorrow, and the meter that was demoted to observer has quietly kept its
    veto -- moved from the valve to the arithmetic.

    The meter may still improve the figure when it has one to offer. What it may
    not do is reduce the credit to zero by saying nothing.
    """

    @pytest.mark.asyncio
    async def test_a_zone_without_a_meter_credits_the_declared_rate(self, hass_mock, di_sensor):
        """No witness at all is the plain case, and the dose is the duration."""
        zone = _zone_sensor(hass_mock, di_sensor)
        zone._zone_deficit = 5.0
        target = zone.volume_liters
        assert target > 0, "the fixture must ask for water, or the test proves nothing"

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._wait_with_stop_check = AsyncMock(side_effect=lambda duration, **kwargs: duration)

        await ctrl._irrigate_zones(["TestZone"])

        assert zone._total_water_delivered == pytest.approx(target, abs=0.2)
        assert zone._zone_deficit == 0.0

    @pytest.mark.asyncio
    async def test_a_meter_that_never_moves_does_not_zero_the_credit(self, hass_mock, di_sensor):
        """A frozen counter is an observer with nothing to say, not a verdict.

        Far more likely a stalled sensor than a dry pipe: the valve was
        commanded open and the user's own figure says what that delivers.
        """
        zone = _zone_sensor(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter"},
        )
        zone._zone_deficit = 5.0
        target = zone.volume_liters
        hass_mock.states.get = MagicMock(side_effect=_frozen_meter(zone, "sensor.flow_meter"))

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._wait_with_stop_check = AsyncMock(side_effect=lambda duration, **kwargs: duration)

        delivered = await ctrl._deliver_estimated_flow(zone)

        assert delivered == pytest.approx(target, abs=0.2)


class TestCase3CreditsTheDoseTheValveAccepted:
    """T7: in volume_preset the valve answers, so its dose is the credit.

    NeverDry arms a volume and the valve closes itself on it. Crediting anything
    else would be second-guessing the only party that measured the water, and
    the mode exists precisely because that party is better placed than we are.
    """

    @pytest.mark.asyncio
    async def test_the_credit_is_the_dose_armed_on_the_valve(self, hass_mock, di_sensor, monkeypatch):
        zone = _zone_sensor(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_DELIVERY_TIMEOUT: 10,
            },
        )
        zone._zone_deficit = 5.0
        target = zone.volume_liters
        assert target > 0

        # The valve reads "off" throughout: it never auto-opens, and the poll
        # loop sees it shut and treats that as the self-close.
        closed = MagicMock()
        closed.state = "off"
        hass_mock.states.get = MagicMock(return_value=closed)
        monkeypatch.setattr("never_dry.controller.asyncio.sleep", AsyncMock())

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        delivered = await ctrl._deliver_volume_preset(zone)

        assert delivered == pytest.approx(target)


class TestCase3IsNotGuardedOnTheLivePath:
    """T8: what the seam claims about Case 3, and what actually runs.

    The plan of 2026-09-08 decided that the guard stays a gate in Cases 2 and 3
    with the device's threshold. It is implemented for Case 2 only, and this
    test exists so that the gap is a recorded fact rather than a belief.

    ``ZoneDriver`` does arm the guard for volume_preset, but nothing on the live
    path asks it: ``_deliver_volume_preset`` bypasses the driver deliberately,
    because a smart valve driving its own state does not fit "I command, you
    obey". So the arming is real and unreachable, in the same way that
    ``deliver()`` itself is (AI-345).

    This test is written to fail the day Case 3 is routed through the operator,
    which is exactly when the divergence should be reconsidered rather than
    silently resolved.
    """

    def test_the_driver_seam_would_guard_it(self):
        driver = _zone(DeliveryMode.VOLUME_PRESET, resolution_l=1.0, cadence_s=14.0)
        assert driver.flow_guard_armed is True
        assert driver.meter_guard_usable is True

    @pytest.mark.asyncio
    async def test_but_the_live_path_never_consults_the_driver(self, hass_mock, di_sensor, monkeypatch):
        zone = _zone_sensor(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 10,
            },
        )
        zone._zone_deficit = 5.0

        closed = MagicMock()
        closed.state = "off"
        hass_mock.states.get = MagicMock(return_value=closed)
        monkeypatch.setattr("never_dry.controller.asyncio.sleep", AsyncMock())

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        operator = MagicMock()
        operator.async_turn_on = AsyncMock()
        ctrl._valve_operators[zone.valve] = operator

        await ctrl._deliver_volume_preset(zone)

        operator.async_turn_on.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_spy_above_is_wired_and_would_have_seen_it(self, hass_mock, di_sensor):
        """Without this, "never called" could just mean "never registered".

        An assertion that something did not happen is worth exactly as much as
        the proof that it could have. ``_open_valve`` is the seam volume_preset
        skips, so consulting it here shows the same registration the previous
        test relies on is live.
        """
        zone = _zone_sensor(hass_mock, di_sensor)
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        operator = MagicMock()
        operator.async_turn_on = AsyncMock(return_value=MagicMock(status=OperationStatus.OK))
        ctrl._valve_operators[zone.valve] = operator

        assert await ctrl._open_valve(zone.valve) is True
        operator.async_turn_on.assert_awaited_once()
