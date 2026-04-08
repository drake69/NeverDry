"""Tests for IrrigationController — valve control and irrigation cycles."""

import time
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    MIN_SERVICE_INTERVAL_S,
)
from never_dry.controller import IrrigationController


class TestControllerState:
    """Test controller state tracking."""

    def test_initial_state(self, controller):
        assert controller.is_running is False
        assert controller.active_valve is None

    def test_register_services(self, controller, hass_mock):
        controller.register_services()
        assert hass_mock.services.async_register.call_count == 4


class TestIrrigateSingleZone:
    """Test irrigating a single zone."""

    @pytest.mark.asyncio
    async def test_opens_and_closes_valve(self, controller, hass_mock, di_sensor, zone_orto):
        """Controller should open valve, wait, close valve."""
        zone_orto._zone_deficit = 5.0

        controller._wait_with_stop_check = AsyncMock()

        await controller._irrigate_zones(["Orto"])

        # Verify valve was opened and closed
        calls = hass_mock.services.async_call.call_args_list
        open_calls = [c for c in calls if c == call("switch", "turn_on", {"entity_id": "switch.valve_orto"})]
        close_calls = [c for c in calls if c == call("switch", "turn_off", {"entity_id": "switch.valve_orto"})]
        assert len(open_calls) == 1
        assert len(close_calls) == 1

    @pytest.mark.asyncio
    async def test_resets_zone_deficit_after_irrigation(self, controller, di_sensor, zone_orto):
        """Zone deficit should be reset to zero after successful irrigation."""
        zone_orto._zone_deficit = 10.0
        controller._wait_with_stop_check = AsyncMock()

        await controller._irrigate_zones(["Orto"])

        assert zone_orto._zone_deficit == 0.0

    @pytest.mark.asyncio
    async def test_skips_zone_without_valve(self, hass_mock, di_sensor):
        """Zone without valve should be skipped."""
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "NoValve",
                CONF_ZONE_AREA: 10.0,
                CONF_ZONE_EFFICIENCY: 0.85,
                CONF_ZONE_FLOW_RATE: 5.0,
            },
            di_sensor,
        )

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._wait_with_stop_check = AsyncMock()
        di_sensor._deficit = 10.0

        await ctrl._irrigate_zones(["NoValve"])

        # No valve calls should have been made
        hass_mock.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_zone_with_zero_duration(self, controller, di_sensor):
        """Zone with zero deficit should be skipped."""
        di_sensor._deficit = 0.0
        controller._wait_with_stop_check = AsyncMock()

        await controller._irrigate_zones(["Orto"])

        # No valve calls for zero duration
        hass_mock_calls = [c for c in controller._hass.services.async_call.call_args_list if "turn_on" in str(c)]
        assert len(hass_mock_calls) == 0

    @pytest.mark.asyncio
    async def test_sets_irrigating_flag(self, controller, di_sensor, zone_orto):
        """Zone should be marked as irrigating during the cycle."""
        zone_orto._zone_deficit = 5.0
        irrigating_states = []

        async def capture_state(duration):
            irrigating_states.append(zone_orto.is_irrigating)

        controller._wait_with_stop_check = capture_state

        await controller._irrigate_zones(["Orto"])

        # During irrigation it should have been True
        assert True in irrigating_states
        # After irrigation it should be False
        assert zone_orto.is_irrigating is False


class TestIrrigateAllZones:
    """Test sequential irrigation of all zones."""

    @pytest.mark.asyncio
    async def test_irrigates_all_zones_sequentially(self, controller, hass_mock, di_sensor, zone_orto, zone_prato):
        """All zones should be irrigated in order."""
        zone_orto._zone_deficit = 10.0
        zone_prato._zone_deficit = 10.0
        controller._wait_with_stop_check = AsyncMock()

        await controller._irrigate_zones(["Orto", "Prato"])

        calls = hass_mock.services.async_call.call_args_list
        turn_on_entities = [c.args[2]["entity_id"] for c in calls if c.args[1] == "turn_on"]
        assert turn_on_entities == ["switch.valve_orto", "switch.valve_prato"]

    @pytest.mark.asyncio
    async def test_deficit_reset_after_all_zones(self, controller, di_sensor, zone_orto, zone_prato):
        """All zone deficits and reference deficit should reset after full cycle."""
        zone_orto._zone_deficit = 10.0
        zone_prato._zone_deficit = 10.0
        di_sensor._deficit = 10.0
        controller._wait_with_stop_check = AsyncMock()

        await controller._irrigate_zones(["Orto", "Prato"])

        assert zone_orto._zone_deficit == 0.0
        assert zone_prato._zone_deficit == 0.0
        assert di_sensor._deficit == 0.0


class TestEmergencyStop:
    """Test emergency stop functionality."""

    @pytest.mark.asyncio
    async def test_stop_closes_all_valves(self, controller, hass_mock, di_sensor):
        """Emergency stop should close all configured valves."""
        di_sensor._deficit = 10.0
        call_mock = MagicMock()
        call_mock.data = {}
        await controller._handle_stop(call_mock)

        close_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[1] == "turn_off"]
        valve_ids = {c.args[2]["entity_id"] for c in close_calls}
        assert "switch.valve_orto" in valve_ids
        assert "switch.valve_prato" in valve_ids

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, controller, hass_mock):
        call_mock = MagicMock()
        call_mock.data = {}
        controller._running = True
        await controller._handle_stop(call_mock)
        assert controller.is_running is False

    @pytest.mark.asyncio
    async def test_stop_interrupts_cycle(self, controller, hass_mock, di_sensor, zone_orto, zone_prato):
        """Stop request during irrigation should interrupt the cycle."""
        zone_orto._zone_deficit = 10.0
        zone_prato._zone_deficit = 10.0

        async def stop_during_wait(duration):
            controller._stop_requested = True

        controller._wait_with_stop_check = stop_during_wait

        await controller._irrigate_zones(["Orto", "Prato"])

        # Only the first zone's valve should have been opened
        turn_on_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[1] == "turn_on"]
        assert len(turn_on_calls) == 1

        # Zone deficits should NOT be reset (cycle was interrupted)
        assert zone_orto._zone_deficit == 10.0
        assert zone_prato._zone_deficit == 10.0


class TestSystemType:
    """Test irrigation system type default efficiencies."""

    def test_drip_default_efficiency(self, hass_mock, di_sensor):
        from never_dry.const import CONF_ZONE_SYSTEM_TYPE
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Drip",
                CONF_ZONE_AREA: 10.0,
                CONF_ZONE_FLOW_RATE: 5.0,
                CONF_ZONE_SYSTEM_TYPE: "drip",
            },
            di_sensor,
        )
        assert zone._efficiency == 0.92

    def test_sprinkler_default_efficiency(self, hass_mock, di_sensor):
        from never_dry.const import CONF_ZONE_SYSTEM_TYPE
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Sprinkler",
                CONF_ZONE_AREA: 50.0,
                CONF_ZONE_FLOW_RATE: 15.0,
                CONF_ZONE_SYSTEM_TYPE: "sprinkler",
            },
            di_sensor,
        )
        assert zone._efficiency == 0.68

    def test_explicit_efficiency_overrides_system_type(self, hass_mock, di_sensor):
        from never_dry.const import CONF_ZONE_SYSTEM_TYPE
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Custom",
                CONF_ZONE_AREA: 10.0,
                CONF_ZONE_FLOW_RATE: 5.0,
                CONF_ZONE_SYSTEM_TYPE: "drip",
                CONF_ZONE_EFFICIENCY: 0.75,
            },
            di_sensor,
        )
        assert zone._efficiency == 0.75

    def test_no_system_type_uses_global_default(self, hass_mock, di_sensor):
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Plain",
                CONF_ZONE_AREA: 10.0,
                CONF_ZONE_FLOW_RATE: 5.0,
            },
            di_sensor,
        )
        assert zone._efficiency == 0.85


class TestZoneProperties:
    """Test the new zone properties."""

    def test_zone_name_property(self, zone_orto):
        assert zone_orto.zone_name == "Orto"

    def test_valve_property(self, zone_orto):
        assert zone_orto.valve == "switch.valve_orto"

    def test_irrigating_default_false(self, zone_orto):
        assert zone_orto.is_irrigating is False

    def test_set_irrigating(self, zone_orto):
        zone_orto.set_irrigating(True)
        assert zone_orto.is_irrigating is True
        zone_orto.set_irrigating(False)
        assert zone_orto.is_irrigating is False

    def test_irrigating_in_attributes(self, zone_orto, di_sensor):
        di_sensor._deficit = 5.0
        assert zone_orto.extra_state_attributes["irrigating"] is False
        zone_orto.set_irrigating(True)
        assert zone_orto.extra_state_attributes["irrigating"] is True


class TestMonitoringMode:
    """Test monitoring mode (no valves configured)."""

    def _make_no_valve_controller(self, hass_mock, di_sensor, zone_deficit=0.0):
        """Create controller with zones that have no valves."""
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Garden",
                CONF_ZONE_AREA: 30.0,
                CONF_ZONE_EFFICIENCY: 0.85,
                CONF_ZONE_FLOW_RATE: 10.0,
            },
            di_sensor,
        )
        zone._zone_deficit = zone_deficit
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        return ctrl, zone

    def test_monitoring_mode_detected(self, hass_mock, di_sensor):
        """Controller should detect monitoring mode when no valves configured."""
        ctrl, _ = self._make_no_valve_controller(hass_mock, di_sensor)
        assert ctrl.is_monitoring_mode is True

    def test_normal_mode_with_valves(self, controller):
        """Controller with valves should not be in monitoring mode."""
        assert controller.is_monitoring_mode is False

    def test_register_services_starts_monitor(self, hass_mock, di_sensor):
        """In monitoring mode, register_services should start the periodic check."""
        from homeassistant.helpers.event import async_track_time_interval

        ctrl, _ = self._make_no_valve_controller(hass_mock, di_sensor)
        ctrl.register_services()
        async_track_time_interval.assert_called_once()

    def test_register_services_no_monitor_with_valves(self, controller, hass_mock):
        """With valves, register_services should NOT start the periodic check."""
        from homeassistant.helpers.event import async_track_time_interval

        async_track_time_interval.reset_mock()
        controller.register_services()
        async_track_time_interval.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_when_deficit_above_threshold(self, hass_mock, di_sensor):
        """Should send notification when zone deficit exceeds threshold."""
        ctrl, _zone = self._make_no_valve_controller(hass_mock, di_sensor, zone_deficit=25.0)

        await ctrl._check_and_notify()

        hass_mock.services.async_call.assert_called_once()
        call_args = hass_mock.services.async_call.call_args
        assert call_args.args[0] == "persistent_notification"
        assert call_args.args[1] == "create"
        assert "25.0 mm" in call_args.args[2]["message"]

    @pytest.mark.asyncio
    async def test_no_notify_when_deficit_below_threshold(self, hass_mock, di_sensor):
        """Should NOT send notification when zone deficit is below threshold."""
        ctrl, _ = self._make_no_valve_controller(hass_mock, di_sensor, zone_deficit=5.0)

        await ctrl._check_and_notify()

        hass_mock.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notify_when_deficit_zero(self, hass_mock, di_sensor):
        """Should NOT send notification when zone deficit is zero."""
        ctrl, _ = self._make_no_valve_controller(hass_mock, di_sensor, zone_deficit=0.0)

        await ctrl._check_and_notify()

        hass_mock.services.async_call.assert_not_called()


class TestRateLimiting:
    """Test service call rate limiting."""

    def test_first_call_not_throttled(self, controller):
        """First service call should never be throttled."""
        assert controller._is_throttled("test") is False

    def test_rapid_second_call_is_throttled(self, controller):
        """A call within MIN_SERVICE_INTERVAL_S should be throttled."""
        controller._is_throttled("first")
        assert controller._is_throttled("second") is True

    def test_call_after_interval_not_throttled(self, controller):
        """A call after the minimum interval should not be throttled."""
        controller._is_throttled("first")
        # Simulate time passing beyond the throttle window
        controller._last_service_call = time.monotonic() - MIN_SERVICE_INTERVAL_S - 1
        assert controller._is_throttled("second") is False

    @pytest.mark.asyncio
    async def test_reset_throttled_does_nothing(self, controller, di_sensor):
        """Throttled reset should not modify deficit."""
        di_sensor._deficit = 15.0
        controller._is_throttled("warmup")  # set the timestamp

        call_mock = MagicMock()
        call_mock.data = {}
        await controller._handle_reset(call_mock)

        assert di_sensor._deficit == 15.0  # unchanged

    @pytest.mark.asyncio
    async def test_irrigate_zone_throttled_does_nothing(self, controller, zone_orto):
        """Throttled irrigate_zone should not start irrigation."""
        zone_orto._zone_deficit = 10.0
        controller._is_throttled("warmup")

        call_mock = MagicMock()
        call_mock.data = {"zone_name": "Orto"}
        await controller._handle_irrigate_zone(call_mock)

        assert controller.is_running is False

    @pytest.mark.asyncio
    async def test_irrigate_all_throttled_does_nothing(self, controller):
        """Throttled irrigate_all should not start irrigation."""
        controller._is_throttled("warmup")

        call_mock = MagicMock()
        call_mock.data = {}
        await controller._handle_irrigate_all(call_mock)

        assert controller.is_running is False

    @pytest.mark.asyncio
    async def test_stop_is_never_throttled(self, controller, hass_mock):
        """Emergency stop should never be throttled."""
        controller._is_throttled("warmup")  # set timestamp

        call_mock = MagicMock()
        call_mock.data = {}
        await controller._handle_stop(call_mock)

        # Stop should still close valves even when called rapidly
        close_calls = [
            c
            for c in hass_mock.services.async_call.call_args_list
            if c.args[1] == "turn_off"
        ]
        assert len(close_calls) >= 1
