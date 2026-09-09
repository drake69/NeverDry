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
from never_dry.driver import DeliveryMode, ZoneDriver

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
