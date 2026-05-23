"""Tests for IrrigationController when wired with a ValveOperator + notifier.

These tests cover the behaviours introduced by AI-031:
- An operator-reported FAILED open aborts the delivery cleanly.
- An operator in MAINTENANCE is treated like a failed open.
- Manual valve listener relies on per-valve operator state, not the
  legacy global ``_running`` flag, fixing the AI-003 race.
- Emergency stop closes valves concurrently.
- ``volume_preset`` falls back to ``switch.turn_on`` when the smart
  valve does not auto-open after ``number.set_value``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
    CONF_ZONE_VOLUME_ENTITY,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor
from never_dry.valve_fsm import ValveState
from never_dry.valve_operator import OperationResult, OperationStatus

# ── Helpers ───────────────────────────────────────────────────────────


def _fake_operator(state: ValveState = ValveState.IDLE, **kwargs):
    """Build a stand-in operator with AsyncMock open/close + a state property."""
    op = MagicMock()
    op.state = state
    op.is_in_maintenance = state == ValveState.MAINTENANCE
    op.open = AsyncMock(return_value=kwargs.get("open_result", OperationResult(OperationStatus.OK)))
    op.close = AsyncMock(return_value=kwargs.get("close_result", OperationResult(OperationStatus.OK)))
    return op


def _make_zone_orto(hass_mock, di_sensor):
    """Build the standard 'Orto' zone for these tests."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
    }
    return IrrigationZoneSensor(hass_mock, cfg, di_sensor)


# ── Operator integration ─────────────────────────────────────────────


async def test_open_failure_aborts_delivery(hass_mock, di_sensor):
    """A FAILED open from the operator prevents any close, returns 0 delivered."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0  # ensure volume_liters > 0

    op = _fake_operator(
        open_result=OperationResult(OperationStatus.FAILED, "OPEN_FAILED"),
    )
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    delivered = await ctrl._deliver_estimated_flow(zone)
    assert delivered == 0.0
    op.open.assert_awaited_once()
    op.close.assert_not_called()


async def test_maintenance_open_is_treated_as_failed(hass_mock, di_sensor):
    """An operator in MAINTENANCE returns MAINTENANCE → delivery aborts."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0

    op = _fake_operator(
        state=ValveState.MAINTENANCE,
        open_result=OperationResult(OperationStatus.MAINTENANCE, "in_maintenance"),
    )
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    delivered = await ctrl._deliver_estimated_flow(zone)
    assert delivered == 0.0


async def test_close_uses_operator_when_present(hass_mock, di_sensor):
    """``_close_valve`` routes through the operator when one is registered."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    op = _fake_operator()
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )
    ok = await ctrl._close_valve(zone.valve)
    assert ok is True
    op.close.assert_awaited_once()
    # No direct switch.turn_off should have been issued.
    turn_off_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert turn_off_calls == []


async def test_emergency_stop_closes_in_parallel(hass_mock, di_sensor):
    """``_handle_stop`` calls close concurrently on every configured valve."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone2_cfg = {
        CONF_ZONE_NAME: "Prato",
        CONF_ZONE_VALVE: "switch.valve_prato",
        CONF_ZONE_AREA: 50.0,
        CONF_ZONE_EFFICIENCY: 0.70,
        CONF_ZONE_FLOW_RATE: 15.0,
    }
    zone2 = IrrigationZoneSensor(hass_mock, zone2_cfg, di_sensor)

    op1 = _fake_operator()
    op2 = _fake_operator()
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone, zone2],
        inter_zone_delay=0,
        valve_operators={zone.valve: op1, zone2.valve: op2},
    )
    await ctrl._handle_stop(MagicMock())

    op1.close.assert_awaited_once()
    op2.close.assert_awaited_once()
    assert ctrl.is_running is False


# ── Manual valve listener using operator state ───────────────────────


def test_manual_listener_ignored_when_operator_busy(hass_mock, di_sensor):
    """A state change while the operator is in REQ_OPEN is not a manual irrigation."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    op = _fake_operator(state=ValveState.REQ_OPEN)
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    event = MagicMock()
    event.data = {
        "entity_id": zone.valve,
        "old_state": MagicMock(state="off"),
        "new_state": MagicMock(state="on"),
    }
    ctrl._on_valve_state_change(event)
    # No manual baseline should have been recorded.
    assert zone.valve not in ctrl._manual_valve_open


def test_manual_listener_records_when_operator_idle(hass_mock, di_sensor):
    """When the operator is IDLE, an off→on state change *is* a manual irrigation."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    op = _fake_operator(state=ValveState.IDLE)
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    event = MagicMock()
    event.data = {
        "entity_id": zone.valve,
        "old_state": MagicMock(state="off"),
        "new_state": MagicMock(state="on"),
    }
    ctrl._on_valve_state_change(event)
    assert zone.valve in ctrl._manual_valve_open


# ── volume_preset auto-open + fallback ───────────────────────────────


async def test_volume_preset_falls_back_to_turn_on(hass_mock, di_sensor):
    """If the smart valve never auto-opens, controller sends switch.turn_on."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0

    # Valve never reports "on" → grace expires.
    off_state = MagicMock()
    off_state.state = "off"
    hass_mock.states.get = MagicMock(return_value=off_state)

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl.auto_open_grace_s = 0.05

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == zone.volume_liters

    turn_on_calls = [
        c
        for c in hass_mock.services.async_call.call_args_list
        if c.args[:2] == ("switch", "turn_on") and c.args[2].get("entity_id") == "switch.valve_orto"
    ]
    assert len(turn_on_calls) == 1, "fallback turn_on must be issued exactly once"


async def test_volume_preset_no_turn_on_when_auto_opened(hass_mock, di_sensor):
    """If the smart valve reports 'on' within the grace window, no turn_on is sent."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0

    # First the valve is on (auto-opened) and then it closes itself.
    states = iter(
        [
            MagicMock(state="on"),  # grace poll → auto-open detected
            MagicMock(state="on"),  # main loop poll #1
            MagicMock(state="off"),  # main loop poll #2 → done
        ]
    )

    def _state_for(_entity_id):
        return next(states, MagicMock(state="off"))

    hass_mock.states.get = MagicMock(side_effect=_state_for)

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl.auto_open_grace_s = 0.05

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == zone.volume_liters

    turn_on_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_on")]
    assert turn_on_calls == [], "no fallback turn_on when the valve auto-opened"


# ── _last_irrigated regression coverage ──────────────────────────────


async def test_partial_irrigation_updates_last_irrigated(hass_mock, di_sensor):
    """A partial delivery (delivered < target) must still bump _last_irrigated.

    Regression: before this fix, the partial-irrigation branch in
    ``_irrigate_zones`` updated volume counters but forgot to stamp
    ``_last_irrigated`` and ``_last_irrigation_source``, leaving the UI
    looking like nothing had happened.
    """
    zone = _make_zone_orto(hass_mock, di_sensor)
    # Force a positive deficit and a target volume to deliver.
    zone._zone_deficit = 10.0

    op = _fake_operator()
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    # Force a partial delivery: deliver half of the requested volume.
    target_volume = zone.volume_liters
    partial = target_volume / 2

    async def _fake_deliver(_zone):
        """Return a partial delivery so the partial-branch runs."""
        return partial

    ctrl._deliver_water = _fake_deliver  # type: ignore[assignment]
    assert zone._last_irrigated is None

    await ctrl._irrigate_zones(["Orto"])

    assert zone._last_irrigated is not None, "partial irrigation must stamp _last_irrigated"
    assert zone._last_irrigation_source in ("automatic", None)  # legacy or new
    assert zone._last_volume_delivered == round(partial, 1)


async def test_volume_preset_stop_during_run(hass_mock, di_sensor):
    """A stop signal during volume_preset issues switch.turn_off and returns 0."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0
    on_state = MagicMock(state="on")
    hass_mock.states.get = MagicMock(return_value=on_state)

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl.auto_open_grace_s = 0.05
    ctrl._stop_requested = True

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == 0.0
    turn_off_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert any(c.args[2].get("entity_id") == "switch.valve_orto" for c in turn_off_calls)
