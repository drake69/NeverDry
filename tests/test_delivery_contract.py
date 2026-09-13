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

import asyncio
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
    DELIVERY_MODE_FLOW_METER,
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

        # Two consumers read the clock per publication now: every report is
        # timed for the publication cadence, and an advance is timed again for
        # this one. The advances land on readings 3 and 5, so those are the two
        # that have to sit a cadence apart.
        t0 = 1000.0
        clock = iter([t0, t0, t0, t0 + FIELD_METER_CADENCE_S, t0 + FIELD_METER_CADENCE_S])
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


class TestTheCadenceSurvivesAShortDose:
    """The field case of 2026-09-09, and why the older rule could never see it.

    Melograno was dosed for 171 s by a meter on a 300 s clock. Across the whole
    session the counter never advanced, so it never spoke twice while open and
    the cadence stayed unmeasured for ever. Its closing word landed 39 s after
    the valve shut -- outside the old rule in both respects, being neither an
    advance nor something observed while open.

    So publications are timed rather than advances, and the timing runs on past
    the close. Both departures are needed: either one alone still misses this.
    """

    @staticmethod
    def _meter_event(value: float):
        event = MagicMock()
        state = MagicMock()
        state.state = str(value)
        event.data = {"new_state": state}
        return event

    @pytest.mark.asyncio
    async def test_the_closing_tick_after_the_valve_shut_still_teaches_the_cadence(self, monkeypatch):
        """21:05:02 open, 21:09:21 closed, 21:10:00 the meter finally speaks."""
        driver = _zone(DeliveryMode.FLOW_METER)
        driver._dispatch = AsyncMock()
        open_now = True
        monkeypatch.setattr(type(driver), "is_open", property(lambda self: open_now))

        now = 1000.0
        monkeypatch.setattr("never_dry.driver.monotonic", lambda: now)
        await driver._handle_flow_state(self._meter_event(19.0))  # while open

        open_now = False  # the valve shuts, and the meter has not spoken since
        now = 1000.0 + 298.0
        await driver._handle_flow_state(self._meter_event(39.0))

        assert driver._session_flow.publication_median_s == pytest.approx(298.0)

    @pytest.mark.asyncio
    async def test_a_republication_with_no_new_water_is_still_a_publication(self, monkeypatch):
        """A meter repeating itself is timing information, not a flow observation.

        It must reach the cadence and nothing else: fed to the state machine a
        repeat would argue that a running zone is dry.
        """
        driver = _zone(DeliveryMode.FLOW_METER)
        driver._dispatch = AsyncMock()
        monkeypatch.setattr(type(driver), "is_open", property(lambda self: True))

        now = 1000.0
        monkeypatch.setattr("never_dry.driver.monotonic", lambda: now)
        driver._on_flow_report(self._meter_event(19.0))
        now = 1000.0 + FIELD_METER_CADENCE_S
        driver._on_flow_report(self._meter_event(19.0))

        assert driver._session_flow.publication_median_s == pytest.approx(FIELD_METER_CADENCE_S)
        driver._dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_silence_between_two_irrigations_is_not_a_cadence(self, monkeypatch):
        """Both ends of a gap must be under observation, and the gap must fit.

        A zone watered once a day would otherwise report a cadence of 24 hours,
        which is the schedule talking, not the meter.
        """
        driver = _zone(DeliveryMode.FLOW_METER)
        driver._dispatch = AsyncMock()
        monkeypatch.setattr(type(driver), "is_open", property(lambda self: True))

        now = 1000.0
        monkeypatch.setattr("never_dry.driver.monotonic", lambda: now)
        await driver._handle_flow_state(self._meter_event(19.0))
        now = 1000.0 + 86400.0  # same time tomorrow
        await driver._handle_flow_state(self._meter_event(25.0))

        assert driver._session_flow.publication_median_s is None

    def test_what_reaches_the_card_is_the_median_not_the_worst_case(self):
        """One long gap is an artefact -- a restart, a device off the mesh."""
        driver = _zone(DeliveryMode.FLOW_METER)
        for interval in (300.0, 300.0, 300.0, 590.0):
            driver._session_flow.observe_publication_interval(interval)

        assert driver.meter_publication_median_s == pytest.approx(300.0)
        assert driver.meter_publication_peak_s == pytest.approx(590.0)


class TestMeasuringTheMeterDoesNotArmTheGuard:
    """Non-regression: the guard keeps its own, stricter evidence.

    Publication timing is generous on purpose -- it observes, and observing
    cannot misfire. The guard closes valves, so it still rests on the worst gap
    between *advances while water flows*. Letting the lenient measurement arm
    the strict decision is exactly how a healthy valve gets closed again.
    """

    def test_a_measured_publication_cadence_leaves_verification_inapplicable(self):
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=1.0)
        for _ in range(5):
            driver._session_flow.observe_publication_interval(20.0)

        _, verdict = driver.flow_verify_window()
        assert verdict is not None
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


class TestTheSettleReadWaitsForTheEventNotTheClock:
    """The closing tick is announced; nothing had to guess when it would come.

    The wait used to be a computed number of seconds. Both field zones lost a
    healthy sample to it -- by 9 s on one meter and by 172 s on the other --
    and the number could not improve on its own, because it was derived from a
    cadence that only those same discarded sessions could have measured.

    The driver already subscribes to the meter's publications, so the answer
    was arriving on a callback while a sleeper waited beside it. Waiting on
    the event needs no cadence, and works the first time a meter is ever seen.
    """

    @staticmethod
    def _meter_at(driver, value: float) -> None:
        driver._hass.states.get = MagicMock(
            return_value=MagicMock(state=str(value), attributes={"unit_of_measurement": "L"}),
        )

    @pytest.mark.asyncio
    async def test_the_reading_happens_when_the_meter_speaks(self):
        """The field case of pino, 2026-09-10: silent at 30 s, 25 L at 202 s."""
        driver = _zone(DeliveryMode.FLOW_METER)
        self._meter_at(driver, 0.0)

        pending = asyncio.ensure_future(driver.async_settled_volume("sensor.meter", 0.0))
        await asyncio.sleep(0)
        assert not pending.done(), "read before the meter spoke: this is the defect"

        self._meter_at(driver, 25.0)
        driver._note_meter_publication()

        assert await asyncio.wait_for(pending, timeout=1.0) == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_a_meter_with_nothing_more_to_say_is_still_read(self, monkeypatch):
        """The ceiling is a backstop, not a verdict.

        A cumulative counter that went quiet has simply finished talking, and
        its standing value is the right answer.
        """
        driver = _zone(DeliveryMode.FLOW_METER)
        self._meter_at(driver, 108.0)

        async def instant_timeout(awaitable, timeout):
            awaitable.close()
            raise TimeoutError

        monkeypatch.setattr("never_dry.driver.asyncio.wait_for", instant_timeout)

        assert await driver.async_settled_volume("sensor.meter", 100.0) == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_a_zone_watering_again_has_no_settled_volume(self, monkeypatch):
        """Half of two sessions is not a measurement of either."""
        driver = _zone(DeliveryMode.FLOW_METER)
        self._meter_at(driver, 25.0)
        monkeypatch.setattr(type(driver), "is_open", property(lambda self: True))

        pending = asyncio.ensure_future(driver.async_settled_volume("sensor.meter", 0.0))
        await asyncio.sleep(0)
        driver._note_meter_publication()

        assert await asyncio.wait_for(pending, timeout=1.0) is None

    @pytest.mark.asyncio
    async def test_a_stale_publication_does_not_answer_for_the_next_session(self):
        """The event is re-armed before waiting, or every read returns at once."""
        driver = _zone(DeliveryMode.FLOW_METER)
        self._meter_at(driver, 25.0)
        driver._note_meter_publication()  # a publication from before this read

        pending = asyncio.ensure_future(driver.async_settled_volume("sensor.meter", 0.0))
        await asyncio.sleep(0)
        assert not pending.done(), "an old publication satisfied a new wait"

        driver._note_meter_publication()
        assert await asyncio.wait_for(pending, timeout=1.0) == pytest.approx(25.0)


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


class TestOurOwnRefusalNeverCancelsTheCredit:
    """T9: a check we could not conclude must not cost the zone its water.

    The invariant of the contract, and the one worth the most: the verification
    decides whether we may *trust* the meter's account of a session, never
    whether the water that already left the pipe happened. When the guard closed
    a healthy valve in the field, the session ended in error and the litres that
    had run were credited to nobody -- so the deficit stood, the zone was
    watered again the next day, and the model was wrong in the direction that
    floods rather than the one that dries.

    What is credited is what the meter finally reports, not an estimate. A
    genuinely dry pipe therefore credits nothing, which is correct and is the
    reason this cannot be done with time multiplied by the declared rate.

    The reading has to wait for the meter's own cadence: on the field device the
    closing tick landed three and a half minutes after the valve shut, so an
    immediate read would return the same truncated figure that caused the whole
    diagnosis to take a day.
    """

    @staticmethod
    def _refused_open():
        return MagicMock(status=OperationStatus.FAILED, error_detail="flow_unverifiable")

    def _zone_and_controller(self, hass_mock, di_sensor, *, reading="100.0"):
        zone = _zone_sensor(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
            },
        )
        zone._zone_deficit = 5.0
        hass_mock.states.get = MagicMock(side_effect=_frozen_meter(zone, "sensor.flow_meter", reading))
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        return zone, ctrl

    @staticmethod
    def _capture_tasks(hass_mock):
        """Run the deferred work deterministically instead of hoping it ran."""
        spawn = hass_mock.async_create_task
        tasks = []

        def capture(coro):
            task = spawn(coro)
            tasks.append(task)
            return task

        hass_mock.async_create_task = capture
        return tasks

    @pytest.mark.asyncio
    async def test_a_refused_opening_still_credits_what_the_meter_saw(self, hass_mock, di_sensor):
        zone, ctrl = self._zone_and_controller(hass_mock, di_sensor)
        driver = MagicMock()
        driver.async_turn_on = AsyncMock(return_value=self._refused_open())
        driver.async_settled_volume = AsyncMock(return_value=8.0)
        ctrl._valve_operators[zone.valve] = driver
        tasks = self._capture_tasks(hass_mock)

        delivered = await ctrl._deliver_flow_meter(zone)
        for task in tasks:
            await task

        assert delivered == 0.0, "the session did fail: the synchronous figure is honest"
        # 8 L on 20 m2 at 0.90 efficiency is 0.36 mm of the 5 mm owed.
        assert zone._zone_deficit == pytest.approx(5.0 - 8.0 * 0.90 / 20.0)

    @pytest.mark.asyncio
    async def test_a_dry_pipe_credits_nothing(self, hass_mock, di_sensor):
        """The guard exists for this case, and the fix must not blunt it."""
        zone, ctrl = self._zone_and_controller(hass_mock, di_sensor)
        driver = MagicMock()
        driver.async_turn_on = AsyncMock(return_value=self._refused_open())
        driver.async_settled_volume = AsyncMock(return_value=None)
        ctrl._valve_operators[zone.valve] = driver
        tasks = self._capture_tasks(hass_mock)

        await ctrl._deliver_flow_meter(zone)
        for task in tasks:
            await task

        assert zone._zone_deficit == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_the_credit_waits_for_the_meter_before_it_answers(self, hass_mock, di_sensor):
        """The condition the credit depends on: read too early and it reads zero.

        Neither a constant nor immediate. What the credited figure waits for is
        the meter's own next word, whenever that comes.
        """
        driver = _zone(DeliveryMode.FLOW_METER, resolution_l=6.0, cadence_s=FIELD_METER_CADENCE_S)
        driver._hass.states.get = MagicMock(
            return_value=MagicMock(state="100.0", attributes={"unit_of_measurement": "L"}),
        )

        pending = asyncio.ensure_future(driver.async_settled_volume("sensor.meter", 100.0))
        await asyncio.sleep(0)
        assert not pending.done(), "answered before the meter had spoken"

        driver._hass.states.get = MagicMock(
            return_value=MagicMock(state="108.0", attributes={"unit_of_measurement": "L"}),
        )
        driver._note_meter_publication()

        assert await asyncio.wait_for(pending, timeout=1.0) == pytest.approx(8.0)
