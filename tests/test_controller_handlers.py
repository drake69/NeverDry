"""Tests for IrrigationController service handlers and mode setup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_IRRIGATION_MODE,
    CONF_ZONE_IRRIGATION_TIME,
    CONF_ZONE_NAME,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor


def _make_zone(hass, di_sensor, name="Orto", valve="switch.valve", mode="manual", irrigation_time=None):
    cfg = {
        CONF_ZONE_NAME: name,
        CONF_ZONE_VALVE: valve,
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_IRRIGATION_MODE: mode,
    }
    if irrigation_time:
        cfg[CONF_ZONE_IRRIGATION_TIME] = irrigation_time
    return IrrigationZoneSensor(hass, cfg, di_sensor)


def _make_call(data):
    call = MagicMock()
    call.data = data
    return call


# ═══════════════════════════════════════════════
#  valve_operators property
# ═══════════════════════════════════════════════

class TestValveOperatorsProperty:
    def test_returns_dict(self, controller):
        ops = controller.valve_operators
        assert isinstance(ops, dict)


# ═══════════════════════════════════════════════
#  _handle_reset
# ═══════════════════════════════════════════════

class TestHandleReset:
    @pytest.mark.asyncio
    async def test_reset_clears_zone_deficits(self, controller, zone_orto, zone_prato):
        zone_orto._zone_deficit = 25.0
        zone_prato._zone_deficit = 30.0
        await controller._handle_reset(_make_call({}))
        assert zone_orto._zone_deficit == 0.0
        assert zone_prato._zone_deficit == 0.0

    @pytest.mark.asyncio
    async def test_reset_clears_dryness_index(self, controller, di_sensor):
        di_sensor._deficit = 40.0
        await controller._handle_reset(_make_call({}))
        assert di_sensor._deficit == 0.0


# ═══════════════════════════════════════════════
#  _handle_irrigate_zone error paths
# ═══════════════════════════════════════════════

class TestHandleIrrigateZoneErrors:
    @pytest.mark.asyncio
    async def test_unknown_zone_logs_error(self, controller, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            await controller._handle_irrigate_zone(_make_call({"zone_name": "NonExistent"}))
        assert "not found" in caplog.text

    @pytest.mark.asyncio
    async def test_already_running_skips(self, controller):
        controller._running = True
        controller._irrigation_task = None
        await controller._handle_irrigate_zone(_make_call({"zone_name": "Orto"}))
        assert controller._irrigation_task is None

    @pytest.mark.asyncio
    async def test_valid_zone_creates_task(self, controller, hass_mock):
        hass_mock.async_create_task = MagicMock(return_value=MagicMock())
        await controller._handle_irrigate_zone(_make_call({"zone_name": "Orto"}))
        hass_mock.async_create_task.assert_called_once()


# ═══════════════════════════════════════════════
#  _handle_irrigate_all error paths
# ═══════════════════════════════════════════════

class TestHandleIrrigateAllErrors:
    @pytest.mark.asyncio
    async def test_already_running_skips(self, controller):
        controller._running = True
        controller._irrigation_task = None
        await controller._handle_irrigate_all(_make_call({}))
        assert controller._irrigation_task is None

    @pytest.mark.asyncio
    async def test_not_running_creates_task(self, controller, hass_mock):
        hass_mock.async_create_task = MagicMock(return_value=MagicMock())
        await controller._handle_irrigate_all(_make_call({}))
        hass_mock.async_create_task.assert_called_once()


# ═══════════════════════════════════════════════
#  _handle_reset_valve
# ═══════════════════════════════════════════════

class TestHandleResetValve:
    @pytest.mark.asyncio
    async def test_unknown_zone_logs_error(self, controller, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            await controller._handle_reset_valve(_make_call({"zone_name": "Ghost"}))
        assert "not found" in caplog.text

    @pytest.mark.asyncio
    async def test_zone_without_valve_logs_error(self, hass_mock, di_sensor, caplog):
        import logging
        cfg = {
            CONF_ZONE_NAME: "NoValve",
            CONF_ZONE_AREA: 10.0,
            CONF_ZONE_EFFICIENCY: 0.9,
            CONF_ZONE_FLOW_RATE: 5.0,
            CONF_ZONE_THRESHOLD: 10.0,
        }
        zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
        ctrl = IrrigationController(hass_mock, di_sensor, [zone])
        with caplog.at_level(logging.ERROR):
            await ctrl._handle_reset_valve(_make_call({"zone_name": "NoValve"}))
        assert "no valve" in caplog.text

    @pytest.mark.asyncio
    async def test_valid_zone_calls_operator_reset(self, controller, zone_orto):
        operator = MagicMock()
        operator.reset_maintenance = AsyncMock()
        controller._valve_operators["switch.valve_orto"] = operator
        await controller._handle_reset_valve(_make_call({"zone_name": "Orto"}))
        operator.reset_maintenance.assert_called_once()


# ═══════════════════════════════════════════════
#  _make_reactive_handler
# ═══════════════════════════════════════════════

class TestReactiveHandler:
    def test_below_threshold_no_irrigation(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 5.0   # threshold is 15.0
        handler = controller._make_reactive_handler("Orto")
        hass_mock.async_create_task = MagicMock()
        handler(1.0, 0.5, 0.0)
        hass_mock.async_create_task.assert_not_called()

    def test_above_threshold_triggers_irrigation(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 20.0  # above threshold 15.0
        handler = controller._make_reactive_handler("Orto")
        hass_mock.async_create_task = MagicMock(return_value=MagicMock())
        handler(1.0, 0.5, 0.0)
        hass_mock.async_create_task.assert_called_once()

    def test_already_running_skips(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 20.0
        controller._running = True
        handler = controller._make_reactive_handler("Orto")
        hass_mock.async_create_task = MagicMock()
        handler(1.0, 0.5, 0.0)
        hass_mock.async_create_task.assert_not_called()

    def test_unknown_zone_no_crash(self, controller, hass_mock):
        handler = controller._make_reactive_handler("Fantasma")
        hass_mock.async_create_task = MagicMock()
        handler(1.0, 0.5, 0.0)   # should not raise
        hass_mock.async_create_task.assert_not_called()

    def test_sets_source_to_reactive(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 20.0
        handler = controller._make_reactive_handler("Orto")
        hass_mock.async_create_task = MagicMock(return_value=MagicMock())
        handler(1.0, 0.5, 0.0)
        assert controller._current_source == "reactive"


# ═══════════════════════════════════════════════
#  _make_scheduled_handler
# ═══════════════════════════════════════════════

class TestScheduledHandler:
    def test_below_threshold_no_irrigation(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 5.0
        handler = controller._make_scheduled_handler("Orto")
        hass_mock.async_create_task = MagicMock()
        handler(MagicMock())
        hass_mock.async_create_task.assert_not_called()

    def test_above_threshold_triggers_irrigation(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 20.0
        handler = controller._make_scheduled_handler("Orto")
        hass_mock.async_create_task = MagicMock(return_value=MagicMock())
        handler(MagicMock())
        hass_mock.async_create_task.assert_called_once()

    def test_already_running_skips(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 20.0
        controller._running = True
        handler = controller._make_scheduled_handler("Orto")
        hass_mock.async_create_task = MagicMock()
        handler(MagicMock())
        hass_mock.async_create_task.assert_not_called()

    def test_unknown_zone_no_crash(self, controller, hass_mock):
        handler = controller._make_scheduled_handler("Fantasma")
        hass_mock.async_create_task = MagicMock()
        handler(MagicMock())
        hass_mock.async_create_task.assert_not_called()

    def test_sets_source_to_scheduled(self, controller, zone_orto, hass_mock):
        zone_orto._zone_deficit = 20.0
        handler = controller._make_scheduled_handler("Orto")
        hass_mock.async_create_task = MagicMock(return_value=MagicMock())
        handler(MagicMock())
        assert controller._current_source == "scheduled"


# ═══════════════════════════════════════════════
#  register_services — mode setup
# ═══════════════════════════════════════════════

class TestRegisterServicesMode:
    def test_mode_b_registers_time_change(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor, name="Orto", mode="scheduled", irrigation_time="07:00")
        ctrl = IrrigationController(hass_mock, di_sensor, [zone])
        with patch("never_dry.controller.async_track_time_change") as mock_track:
            ctrl.register_services()
            mock_track.assert_called_once()
            _, kwargs = mock_track.call_args
            assert kwargs.get("hour") == 7
            assert kwargs.get("minute") == 0

    def test_mode_b_invalid_time_logs_error(self, hass_mock, di_sensor, caplog):
        import logging
        zone = _make_zone(hass_mock, di_sensor, name="Orto", mode="scheduled", irrigation_time="bad")
        ctrl = IrrigationController(hass_mock, di_sensor, [zone])
        with caplog.at_level(logging.ERROR):
            ctrl.register_services()
        assert "Invalid irrigation_time" in caplog.text

    def test_mode_a_registers_dryness_listener(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor, name="Orto", mode="reactive")
        before = len(di_sensor._zone_listeners)
        ctrl = IrrigationController(hass_mock, di_sensor, [zone])
        ctrl.register_services()
        assert len(di_sensor._zone_listeners) > before
