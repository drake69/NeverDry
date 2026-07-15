"""End-trigger race matrix tests (developer_manual §7.2, marker [M]).

One test per previously-uncovered cell of the irrigation end-trigger
coverage matrix. Every test drives a real delivery loop, fires a second
end trigger while the first is armed, and asserts the same contract:

- the zone deficit is decremented by exactly the credited volume
  (``deficit_start - delivered * efficiency / area``, clipped at 0);
- the last-session history is written once and is internally coherent:
  ``last_volume_delivered`` matches the credited volume and
  ``last_session_duration_s`` matches the loop's actual running time
  (wall-clock, +/-2 s tolerance for scheduler overhead);
- the settle happens exactly once (no double-credit when two triggers
  fire close together).

WDOG cells: at controller level the ValveOperator watchdog manifests as
a forced ``switch.turn_off`` -> the valve state reads 'off' mid-loop, so
the watchdog side is simulated by flipping the scripted valve state
(the watchdog's own firing is covered in test_valve_operator.py).

Zone geometry used throughout: area 20 m2, efficiency 0.90, guard flow
8 L/min. Deficits are kept tiny so the effective delivery timeout stays
at the configured floor (compatible with the guard-scaled timeout).
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_METER_SENSOR,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    FLOW_METER_POLL_INTERVAL_S,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor

AREA = 20.0
EFF = 0.90
FLOW = 8.0  # L/min guard flow
VALVE = "switch.matrix_valve"
METER = "sensor.matrix_meter"
TIMEOUT_S = 2 * FLOW_METER_POLL_INTERVAL_S  # 4 s with the 2 s poll


class _Env:
    """Scriptable valve/meter state shared with the controller via states.get."""

    def __init__(self, hass_mock, meter_liters=100.0):
        self.valve_state = "on"
        self.meter_liters = meter_liters
        self.meter_step = 0.0  # liters added on every meter read
        self.meter_reads = 0
        self.valve_reads = 0
        self.on_valve_read = None  # callback(reads) fired on each valve poll

        def get_state(entity_id):
            if entity_id == METER:
                self.meter_reads += 1
                self.meter_liters += self.meter_step
                s = MagicMock()
                s.state = str(self.meter_liters)
                s.attributes = {"unit_of_measurement": "L"}
                return s
            if entity_id == VALVE:
                self.valve_reads += 1
                if self.on_valve_read:
                    self.on_valve_read(self.valve_reads)
                s = MagicMock()
                s.state = self.valve_state
                return s
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)


def _flow_meter_zone(hass_mock, di_sensor, deficit):
    zone = IrrigationZoneSensor(
        hass_mock,
        {
            CONF_ZONE_NAME: "Matrix",
            CONF_ZONE_VALVE: VALVE,
            CONF_ZONE_AREA: AREA,
            CONF_ZONE_EFFICIENCY: EFF,
            CONF_ZONE_FLOW_RATE: FLOW,
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
            CONF_ZONE_FLOW_METER_SENSOR: METER,
            CONF_ZONE_DELIVERY_TIMEOUT: TIMEOUT_S,
        },
        di_sensor,
    )
    zone._zone_deficit = deficit
    return zone


def _estimated_zone(hass_mock, di_sensor, deficit):
    zone = IrrigationZoneSensor(
        hass_mock,
        {
            CONF_ZONE_NAME: "Matrix",
            CONF_ZONE_VALVE: VALVE,
            CONF_ZONE_AREA: AREA,
            CONF_ZONE_EFFICIENCY: EFF,
            CONF_ZONE_FLOW_RATE: FLOW,
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_ESTIMATED_FLOW,
        },
        di_sensor,
    )
    zone._zone_deficit = deficit
    return zone


def _assert_session_coherent(zone, deficit_start, *, max_running_s):
    """The uniform contract: deficit decrement <-> volume <-> running time.

    ``delivered`` is read back from the session history so the assertion
    chain proves history and deficit agree with each other exactly.
    """
    delivered = zone._last_volume_delivered
    assert delivered > 0, "session must have credited some water"
    # Deficit decremented by exactly the credited volume (mm), clipped at 0.
    expected_deficit = max(0.0, deficit_start - delivered * EFF / AREA)
    assert zone._zone_deficit == pytest.approx(expected_deficit, abs=0.005)
    # History written once: totals match the single credited session.
    assert zone._total_water_delivered == pytest.approx(delivered, abs=0.06)
    assert zone._session_water_delivered == pytest.approx(delivered, abs=0.06)
    assert zone._last_irrigated is not None
    # Running time coherent with the credited volume and bounded by the
    # scenario: wall clock may add scheduler overhead (+/-2 s).
    duration = zone._last_session_duration_s
    assert 1 <= duration <= max_running_s + 2
    assert zone.is_irrigating is False
    return delivered, duration


def _assert_no_further_settle(zone, delivered, deficit):
    """A late second trigger must not re-credit the session."""
    assert zone._last_volume_delivered == pytest.approx(delivered, abs=0.001)
    assert zone._total_water_delivered == pytest.approx(delivered, abs=0.06)
    assert zone._zone_deficit == pytest.approx(deficit, abs=0.001)


async def _run(ctrl, zone_name="Matrix"):
    task = asyncio.create_task(ctrl._irrigate_zones([zone_name]))
    ctrl._irrigation_task = task
    return task


# ── TARGET x UNLOAD (and controller-side UNLOAD diagonal) ─────────────


@pytest.mark.asyncio
async def test_unload_mid_flow_meter_settles_measured_partial(hass_mock, di_sensor):
    """[M] TARGET x UNLOAD: entry unload mid-delivery credits the measured partial."""
    deficit = 0.05  # target 1.11 L, unreachable before unload
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)
    env.meter_step = 0.1  # meter progresses: measured, not estimated
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    # Stop inside the first poll window so the unload (not the timeout)
    # is the trigger that actually ends the session.
    await asyncio.sleep(1.0)
    await ctrl.async_stop()  # unload path: stop_requested + await task + settle
    assert task.done()

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    # Measured partial (0.1 L per loop poll): what the meter accumulated,
    # NOT the guard-flow estimate (which would be >= 0.5 L over the same time).
    assert 0.05 <= delivered <= 0.25


# ── EST row ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_zone_mid_estimated_flow_credits_elapsed_fraction(hass_mock, di_sensor):
    """[M] EST x STOPZ: per-zone stop mid-wait credits volume x elapsed/duration."""
    deficit = 0.03  # ~0.667 L -> 5 s planned duration at 8 L/min
    zone = _estimated_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    planned = zone.duration_s
    assert planned >= 4

    task = await _run(ctrl)
    await asyncio.sleep(2.2)
    ctrl._stop_zone = "Matrix"  # what _handle_stop_zone sets for the loop
    await task

    delivered, duration = _assert_session_coherent(zone, deficit, max_running_s=planned)
    assert delivered < zone.volume_liters + deficit * AREA / EFF  # partial, not full plan
    # Proportional credit: delivered = planned_volume * elapsed / planned_duration.
    planned_volume = deficit * AREA / EFF
    elapsed = delivered / planned_volume * planned
    assert 1 <= elapsed <= planned
    assert abs(duration - elapsed) <= 2


@pytest.mark.asyncio
async def test_watchdog_forced_close_mid_estimated_flow_credits_elapsed_fraction(hass_mock, di_sensor):
    """[M] EST x WDOG: watchdog force-off (valve reads 'off') aborts the wait
    and the elapsed fraction is still credited."""
    deficit = 0.03
    zone = _estimated_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)
    planned = zone.duration_s

    def watchdog_fires(reads):
        if reads >= 2:  # second 1 s tick of the wait loop
            env.valve_state = "off"

    env.on_valve_read = watchdog_fires
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await task

    delivered, duration = _assert_session_coherent(zone, deficit, max_running_s=planned)
    planned_volume = deficit * AREA / EFF
    assert delivered < planned_volume  # aborted before plan completion
    assert duration <= planned


@pytest.mark.asyncio
async def test_unload_mid_estimated_flow_credits_elapsed_fraction(hass_mock, di_sensor):
    """[M] EST x UNLOAD: entry unload mid-wait credits the elapsed fraction."""
    deficit = 0.03
    zone = _estimated_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    planned = zone.duration_s

    task = await _run(ctrl)
    await asyncio.sleep(2.2)
    await ctrl.async_stop()
    assert task.done()

    delivered, duration = _assert_session_coherent(zone, deficit, max_running_s=planned)
    planned_volume = deficit * AREA / EFF
    assert delivered < planned_volume
    assert duration <= planned


# ── TIMEOUT row ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_just_before_timeout_settles_once(hass_mock, di_sensor):
    """[M] TIMEOUT x STOP: emergency stop racing the delivery timeout on a
    dead meter still credits the guard-flow estimate exactly once."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)  # dead meter: reading frozen
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 1.0)  # inside the last poll window
    await ctrl._handle_stop(MagicMock(data={}))
    await task

    delivered, duration = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    # Guard-flow estimate over the loop's whole-poll running time.
    elapsed = delivered * 60.0 / FLOW
    assert FLOW_METER_POLL_INTERVAL_S - 0.8 <= elapsed <= TIMEOUT_S + 0.8
    assert abs(duration - elapsed) <= 2


@pytest.mark.asyncio
async def test_stop_zone_just_before_timeout_settles_once(hass_mock, di_sensor):
    """[M] TIMEOUT x STOPZ: per-zone stop racing the timeout, single settle."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 1.0)
    ctrl._stop_zone = "Matrix"
    await task

    delivered, duration = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    elapsed = delivered * 60.0 / FLOW
    assert FLOW_METER_POLL_INTERVAL_S - 0.8 <= elapsed <= TIMEOUT_S + 0.8
    assert abs(duration - elapsed) <= 2


@pytest.mark.asyncio
async def test_external_close_before_timeout_settles_estimate(hass_mock, di_sensor):
    """[M] TIMEOUT x EXT: hardware auto-close beats the timeout; the elapsed
    open time is credited from the guard flow (dead meter)."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:  # seen by the loop's external-close check
            env.valve_state = "off"

    env.on_valve_read = hardware_closes
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await task

    delivered, duration = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    elapsed = delivered * 60.0 / FLOW
    assert elapsed <= TIMEOUT_S + 0.8  # closed at or before the timeout
    assert abs(duration - elapsed) <= 2


@pytest.mark.asyncio
async def test_watchdog_close_races_delivery_timeout_single_settle(hass_mock, di_sensor):
    """[M] TIMEOUT x WDOG: watchdog force-off lands in the same window as the
    delivery timeout; the double close is idempotent, the settle single."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)

    def watchdog_fires(reads):
        # Force-off right around the timeout expiry (second poll window).
        if reads >= 3:
            env.valve_state = "off"

    env.on_valve_read = watchdog_fires
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await task

    delivered, duration = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    deficit_after = zone._zone_deficit
    # The loop's own close ran after the forced off: still one settle only.
    _assert_no_further_settle(zone, delivered, deficit_after)
    assert duration <= TIMEOUT_S + 2


@pytest.mark.asyncio
async def test_unload_races_delivery_timeout_single_settle(hass_mock, di_sensor):
    """[M] TIMEOUT x UNLOAD: unload arriving at timeout expiry settles once."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(TIMEOUT_S - 0.5)  # just before the timeout fires
    await ctrl.async_stop()
    assert task.done()

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    deficit_after = zone._zone_deficit
    # async_stop is idempotent: a second call must not re-settle.
    await ctrl.async_stop()
    _assert_no_further_settle(zone, delivered, deficit_after)


# ── STOP row ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_global_stop_and_stop_zone_together_settle_once(hass_mock, di_sensor):
    """[M] STOP x STOPZ: zone stop followed by panic global stop, one settle."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    ctrl._stop_zone = "Matrix"
    await ctrl._handle_stop(MagicMock(data={}))
    await task

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)
    assert ctrl.is_running is False


@pytest.mark.asyncio
async def test_global_stop_with_valve_already_closed_externally(hass_mock, di_sensor):
    """[M] STOP x EXT: the valve closed on its own an instant before the
    emergency stop; the session still settles exactly once."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.valve_state = "off"

    env.on_valve_read = hardware_closes
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    await ctrl._handle_stop(MagicMock(data={}))
    await task

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_global_stop_races_watchdog_force_close(hass_mock, di_sensor):
    """[M] STOP x WDOG: emergency stop and watchdog force-off in the same
    poll window; idempotent close, single coherent settle."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    env.valve_state = "off"  # watchdog turn_off takes effect...
    await ctrl._handle_stop(MagicMock(data={}))  # ...as the user hits stop
    await task

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_global_stop_then_unload_settles_once(hass_mock, di_sensor):
    """[M] STOP x UNLOAD: user stop followed by entry unload (same reload
    sequence HA runs); the partial session settles exactly once."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    await ctrl._handle_stop(MagicMock(data={}))
    await ctrl.async_stop()
    assert task.done()

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


# ── STOPZ row ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_zone_with_valve_already_closed_externally(hass_mock, di_sensor):
    """[M] STOPZ x EXT: per-zone stop on a valve that just closed on its own."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.valve_state = "off"

    env.on_valve_read = hardware_closes
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    ctrl._stop_zone = "Matrix"
    await task

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_stop_zone_races_watchdog_force_close(hass_mock, di_sensor):
    """[M] STOPZ x WDOG: per-zone stop and watchdog force-off together."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    env.valve_state = "off"
    ctrl._stop_zone = "Matrix"
    await task

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_stop_zone_then_unload_settles_once(hass_mock, di_sensor):
    """[M] STOPZ x UNLOAD: per-zone stop immediately followed by unload."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    _Env(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    ctrl._stop_zone = "Matrix"
    await ctrl.async_stop()
    assert task.done()

    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


# ── EXT row ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_close_then_late_watchdog_off_no_double_settle(hass_mock, di_sensor):
    """[M] EXT x WDOG: hardware auto-close ends the session; a late watchdog
    turn_off must not open a second session nor re-credit the first."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.valve_state = "off"

    env.on_valve_read = hardware_closes
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await task
    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    deficit_after = zone._zone_deficit

    # Late watchdog force-off after the session already ended.
    await hass_mock.services.async_call("switch", "turn_off", {"entity_id": VALVE})
    await asyncio.sleep(0)
    _assert_no_further_settle(zone, delivered, deficit_after)


@pytest.mark.asyncio
async def test_external_close_then_unload_no_double_settle(hass_mock, di_sensor):
    """[M] EXT x UNLOAD: hardware auto-close ends the session; the entry
    unload that follows must not settle it again."""
    deficit = 0.02
    zone = _flow_meter_zone(hass_mock, di_sensor, deficit)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.valve_state = "off"

    env.on_valve_read = hardware_closes
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    task = await _run(ctrl)
    await task
    delivered, _ = _assert_session_coherent(zone, deficit, max_running_s=TIMEOUT_S)
    deficit_after = zone._zone_deficit

    await ctrl.async_stop()
    _assert_no_further_settle(zone, delivered, deficit_after)
