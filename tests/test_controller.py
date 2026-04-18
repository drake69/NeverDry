"""Tests for IrrigationController — valve control and irrigation cycles."""

import time
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_THRESHOLD,
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
        assert hass_mock.services.async_register.call_count == 5


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
        """In monitoring mode, register_services should start monitoring + anomaly timers."""
        from homeassistant.helpers.event import async_track_time_interval

        async_track_time_interval.reset_mock()
        ctrl, _ = self._make_no_valve_controller(hass_mock, di_sensor)
        ctrl.register_services()
        # 2 calls: anomaly check (all modes) + monitoring check (monitoring mode only)
        assert async_track_time_interval.call_count == 2

    def test_register_services_anomaly_only_with_valves(self, controller, hass_mock):
        """With valves, register_services should start only the anomaly timer."""
        from homeassistant.helpers.event import async_track_time_interval

        async_track_time_interval.reset_mock()
        controller.register_services()
        # Only anomaly check, no monitoring check
        async_track_time_interval.assert_called_once()

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
        close_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[1] == "turn_off"]
        assert len(close_calls) >= 1


class TestMarkIrrigated:
    """Test mark_irrigated service (manual irrigation signal)."""

    @pytest.mark.asyncio
    async def test_mark_single_zone(self, controller, zone_orto, zone_prato):
        """Marking a single zone should reset only that zone's deficit."""
        zone_orto._zone_deficit = 15.0
        zone_prato._zone_deficit = 20.0

        call_mock = MagicMock()
        call_mock.data = {"zone_name": "Orto"}
        await controller._handle_mark_irrigated(call_mock)

        assert zone_orto._zone_deficit == 0.0
        assert zone_prato._zone_deficit == 20.0  # untouched

    @pytest.mark.asyncio
    async def test_mark_all_zones(self, controller, zone_orto, zone_prato):
        """Omitting zone_name should reset all zone deficits."""
        zone_orto._zone_deficit = 15.0
        zone_prato._zone_deficit = 20.0

        call_mock = MagicMock()
        call_mock.data = {}
        await controller._handle_mark_irrigated(call_mock)

        assert zone_orto._zone_deficit == 0.0
        assert zone_prato._zone_deficit == 0.0

    @pytest.mark.asyncio
    async def test_mark_unknown_zone_logs_error(self, controller, zone_orto, zone_prato):
        """Marking a non-existent zone should log an error and not reset anything."""
        zone_orto._zone_deficit = 15.0
        zone_prato._zone_deficit = 20.0

        call_mock = MagicMock()
        call_mock.data = {"zone_name": "NonExistent"}
        await controller._handle_mark_irrigated(call_mock)

        assert zone_orto._zone_deficit == 15.0
        assert zone_prato._zone_deficit == 20.0

    @pytest.mark.asyncio
    async def test_mark_irrigated_does_not_reset_reference(self, controller, di_sensor, zone_orto):
        """mark_irrigated should NOT reset the reference deficit."""
        di_sensor._deficit = 25.0
        zone_orto._zone_deficit = 15.0

        call_mock = MagicMock()
        call_mock.data = {"zone_name": "Orto"}
        await controller._handle_mark_irrigated(call_mock)

        assert di_sensor._deficit == 25.0  # untouched

    @pytest.mark.asyncio
    async def test_mark_irrigated_throttled(self, controller, zone_orto):
        """Throttled mark_irrigated should not modify deficit."""
        zone_orto._zone_deficit = 15.0
        controller._is_throttled("warmup")  # set the timestamp

        call_mock = MagicMock()
        call_mock.data = {"zone_name": "Orto"}
        await controller._handle_mark_irrigated(call_mock)

        assert zone_orto._zone_deficit == 15.0  # unchanged


class TestManualValveDetection:
    """Test automatic detection of manual valve operation."""

    def _make_valve_event(self, entity_id, old_state, new_state):
        """Create a mock state change event for a valve."""
        event = MagicMock()
        old = MagicMock()
        old.state = old_state
        new = MagicMock()
        new.state = new_state
        event.data = {
            "entity_id": entity_id,
            "old_state": old,
            "new_state": new,
        }
        return event

    def test_manual_open_detected(self, controller, hass_mock):
        """Manual valve open should be tracked."""
        event = self._make_valve_event("switch.valve_orto", "off", "on")
        controller._on_valve_state_change(event)
        assert "switch.valve_orto" in controller._manual_valve_open

    def test_manual_close_resets_deficit_no_flow_meter(self, controller, zone_orto, hass_mock):
        """Manual valve close without flow meter should reset deficit."""
        zone_orto._zone_deficit = 15.0

        # Simulate open
        open_event = self._make_valve_event("switch.valve_orto", "off", "on")
        controller._on_valve_state_change(open_event)

        # Simulate close
        close_event = self._make_valve_event("switch.valve_orto", "on", "off")
        controller._on_valve_state_change(close_event)

        assert zone_orto._zone_deficit == 0.0

    def test_manual_close_fires_event(self, controller, zone_orto, hass_mock):
        """Manual valve close should fire irrigation complete event."""
        zone_orto._zone_deficit = 15.0

        open_event = self._make_valve_event("switch.valve_orto", "off", "on")
        controller._on_valve_state_change(open_event)

        close_event = self._make_valve_event("switch.valve_orto", "on", "off")
        controller._on_valve_state_change(close_event)

        hass_mock.bus.async_fire.assert_called_once()
        call_args = hass_mock.bus.async_fire.call_args
        assert call_args.args[0] == "never_dry_irrigation_complete"
        assert call_args.args[1]["source"] == "manual"
        assert call_args.args[1]["zone"] == "Orto"

    def test_ignored_when_controller_running(self, controller, zone_orto, hass_mock):
        """Valve changes during controller-driven irrigation should be ignored."""
        zone_orto._zone_deficit = 15.0
        controller._running = True

        open_event = self._make_valve_event("switch.valve_orto", "off", "on")
        controller._on_valve_state_change(open_event)

        assert "switch.valve_orto" not in controller._manual_valve_open

    def test_flow_meter_compensates_deficit(self, hass_mock, di_sensor):
        """With flow meter, deficit should be reduced by measured water, not fully reset."""
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Metered",
                "valve": "switch.valve_metered",
                CONF_ZONE_AREA: 20.0,
                CONF_ZONE_EFFICIENCY: 0.90,
                CONF_ZONE_FLOW_RATE: 8.0,
                "flow_meter_sensor": "sensor.flow_meter",
            },
            di_sensor,
        )
        zone._zone_deficit = 10.0  # mm

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        # Simulate flow meter reads: 100L at open, 110L at close → 10L delivered
        flow_values = iter([MagicMock(state="100.0"), MagicMock(state="110.0")])
        original_get = hass_mock.states.get

        def mock_get(entity_id):
            if entity_id == "sensor.flow_meter":
                return next(flow_values)
            return original_get(entity_id)

        hass_mock.states.get = mock_get

        # Open
        event_open = MagicMock()
        old_s, new_s = MagicMock(), MagicMock()
        old_s.state, new_s.state = "off", "on"
        event_open.data = {"entity_id": "switch.valve_metered", "old_state": old_s, "new_state": new_s}
        ctrl._on_valve_state_change(event_open)

        # Close
        event_close = MagicMock()
        old_s2, new_s2 = MagicMock(), MagicMock()
        old_s2.state, new_s2.state = "on", "off"
        event_close.data = {"entity_id": "switch.valve_metered", "old_state": old_s2, "new_state": new_s2}
        ctrl._on_valve_state_change(event_close)

        # 10L delivered on 20m² → 0.5mm effective, * 0.9 efficiency → 0.45mm compensation
        # deficit should be 10.0 - 0.45 = 9.55
        assert zone._zone_deficit == pytest.approx(9.55, abs=0.01)

    def test_unknown_valve_ignored(self, controller, hass_mock):
        """Events for unknown valve entities should be ignored."""
        event = self._make_valve_event("switch.unknown", "off", "on")
        controller._on_valve_state_change(event)
        assert "switch.unknown" not in controller._manual_valve_open


class TestBatteryMonitoring:
    """Test low-battery alert for valve sensors."""

    def _make_battery_event(self, entity_id, level):
        """Create a mock battery state change event."""
        event = MagicMock()
        new = MagicMock()
        new.state = str(level)
        event.data = {"entity_id": entity_id, "new_state": new}
        return event

    def _make_controller_with_battery(self, hass_mock, di_sensor, battery_sensor=None):
        from never_dry.sensor import IrrigationZoneSensor

        zone_config = {
            CONF_ZONE_NAME: "Garden",
            "valve": "switch.valve_garden",
            CONF_ZONE_AREA: 20.0,
            CONF_ZONE_EFFICIENCY: 0.85,
            CONF_ZONE_FLOW_RATE: 8.0,
        }
        if battery_sensor:
            zone_config["battery_sensor"] = battery_sensor
        zone = IrrigationZoneSensor(hass_mock, zone_config, di_sensor)
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        return ctrl, zone

    def test_low_battery_sends_notification(self, hass_mock, di_sensor):
        ctrl, _ = self._make_controller_with_battery(hass_mock, di_sensor, battery_sensor="sensor.valve_battery")
        event = self._make_battery_event("sensor.valve_battery", 10)
        ctrl._on_battery_change(event)

        assert "Garden" in ctrl._battery_alerted

    def test_no_alert_above_threshold(self, hass_mock, di_sensor):
        ctrl, _ = self._make_controller_with_battery(hass_mock, di_sensor, battery_sensor="sensor.valve_battery")
        event = self._make_battery_event("sensor.valve_battery", 50)
        ctrl._on_battery_change(event)

        assert len(ctrl._battery_alerted) == 0

    def test_alert_only_once(self, hass_mock, di_sensor):
        """Should not re-alert for same zone until battery recovers."""
        ctrl, _ = self._make_controller_with_battery(hass_mock, di_sensor, battery_sensor="sensor.valve_battery")
        event = self._make_battery_event("sensor.valve_battery", 10)
        ctrl._on_battery_change(event)
        ctrl._on_battery_change(event)  # second time

        # Zone should only be in alerted set once
        assert len(ctrl._battery_alerted) == 1

    def test_re_alerts_after_recovery(self, hass_mock, di_sensor):
        """Should re-alert if battery recovers and drops again."""
        ctrl, _ = self._make_controller_with_battery(hass_mock, di_sensor, battery_sensor="sensor.valve_battery")
        # Drop
        ctrl._on_battery_change(self._make_battery_event("sensor.valve_battery", 10))
        assert "Garden" in ctrl._battery_alerted
        # Recover
        ctrl._on_battery_change(self._make_battery_event("sensor.valve_battery", 80))
        assert "Garden" not in ctrl._battery_alerted
        # Drop again
        ctrl._on_battery_change(self._make_battery_event("sensor.valve_battery", 12))
        assert "Garden" in ctrl._battery_alerted

    def test_no_battery_sensor_no_tracking(self, hass_mock, di_sensor):
        ctrl, _ = self._make_controller_with_battery(hass_mock, di_sensor)
        assert len(ctrl._battery_to_zone) == 0


class TestIrrigationEvent:
    """Test irrigation complete event firing."""

    @pytest.mark.asyncio
    async def test_event_fired_on_zone_completion(self, controller, hass_mock, zone_orto):
        """Event should be fired when a zone completes irrigation."""
        zone_orto._zone_deficit = 5.0
        controller._wait_with_stop_check = AsyncMock()

        await controller._irrigate_zones(["Orto"])

        hass_mock.bus.async_fire.assert_called()
        call_args = hass_mock.bus.async_fire.call_args
        assert call_args.args[0] == "never_dry_irrigation_complete"
        assert call_args.args[1]["source"] == "automatic"
        assert call_args.args[1]["zone"] == "Orto"

    @pytest.mark.asyncio
    async def test_no_event_on_stop(self, controller, hass_mock, zone_orto, zone_prato):
        """No event should fire if irrigation is stopped."""
        zone_orto._zone_deficit = 10.0

        async def stop_during_wait(duration):
            controller._stop_requested = True

        controller._wait_with_stop_check = stop_during_wait

        await controller._irrigate_zones(["Orto"])

        # Event should not have been fired (stop before completion)
        fire_calls = [
            c for c in hass_mock.bus.async_fire.call_args_list if c.args[0] == "never_dry_irrigation_complete"
        ]
        assert len(fire_calls) == 0


class TestValveMonitoringEdgeCases:
    """Edge cases for manual valve detection."""

    def _make_valve_event(self, entity_id, old_state, new_state):
        event = MagicMock()
        old = MagicMock()
        old.state = old_state
        new = MagicMock()
        new.state = new_state
        event.data = {
            "entity_id": entity_id,
            "old_state": old,
            "new_state": new,
        }
        return event

    def test_none_new_state_ignored(self, controller):
        """Event with new_state=None should be silently ignored."""
        event = MagicMock()
        event.data = {"entity_id": "switch.valve_orto", "old_state": MagicMock(), "new_state": None}
        controller._on_valve_state_change(event)
        assert "switch.valve_orto" not in controller._manual_valve_open

    def test_none_old_state_ignored(self, controller):
        """Event with old_state=None should be silently ignored."""
        event = MagicMock()
        event.data = {"entity_id": "switch.valve_orto", "old_state": None, "new_state": MagicMock()}
        controller._on_valve_state_change(event)
        assert "switch.valve_orto" not in controller._manual_valve_open

    def test_flow_meter_unavailable_at_open(self, hass_mock, di_sensor):
        """Flow meter returning None at open should store None as baseline."""
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Metered",
                "valve": "switch.valve_m",
                CONF_ZONE_AREA: 20.0,
                CONF_ZONE_EFFICIENCY: 0.9,
                CONF_ZONE_FLOW_RATE: 8.0,
                "flow_meter_sensor": "sensor.flow",
            },
            di_sensor,
        )
        zone._zone_deficit = 10.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        # Flow meter returns unavailable
        unavail = MagicMock()
        unavail.state = "unavailable"
        hass_mock.states.get = lambda eid: unavail if eid == "sensor.flow" else None

        event = self._make_valve_event("switch.valve_m", "off", "on")
        ctrl._on_valve_state_change(event)
        assert ctrl._manual_valve_open["switch.valve_m"] is None

        # Close: flow_start is None → should fall back to full reset
        close_event = self._make_valve_event("switch.valve_m", "on", "off")
        # Flow meter now available at close
        avail = MagicMock()
        avail.state = "50.0"
        hass_mock.states.get = lambda eid: avail if eid == "sensor.flow" else None
        ctrl._on_valve_state_change(close_event)
        # flow_start was None → code takes else branch → full reset
        assert zone._zone_deficit == 0.0

    def test_zero_area_no_division_error(self, hass_mock, di_sensor):
        """Zone with area=0 should not crash on manual valve close with flow meter."""
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "NoArea",
                "valve": "switch.valve_na",
                CONF_ZONE_AREA: 0.0,
                CONF_ZONE_EFFICIENCY: 0.9,
                CONF_ZONE_FLOW_RATE: 8.0,
                "flow_meter_sensor": "sensor.flow",
            },
            di_sensor,
        )
        zone._zone_deficit = 10.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        flow_values = iter([MagicMock(state="100.0"), MagicMock(state="120.0")])
        hass_mock.states.get = lambda eid: next(flow_values) if eid == "sensor.flow" else None

        open_event = self._make_valve_event("switch.valve_na", "off", "on")
        ctrl._on_valve_state_change(open_event)

        close_event = self._make_valve_event("switch.valve_na", "on", "off")
        ctrl._on_valve_state_change(close_event)

        # area=0 → no compensation applied, deficit unchanged
        assert zone._zone_deficit == 10.0

    def test_manual_event_has_no_volume_or_duration(self, controller, zone_orto, hass_mock):
        """Manual close event should not include volume_liters or duration_s."""
        zone_orto._zone_deficit = 15.0

        open_ev = self._make_valve_event("switch.valve_orto", "off", "on")
        controller._on_valve_state_change(open_ev)

        close_ev = self._make_valve_event("switch.valve_orto", "on", "off")
        controller._on_valve_state_change(close_ev)

        call_args = hass_mock.bus.async_fire.call_args
        event_data = call_args.args[1]
        assert "volume_liters" not in event_data
        assert "duration_s" not in event_data
        assert event_data["source"] == "manual"


class TestDeficitAnomaly:
    """Test anomalous deficit detection."""

    def _make_controller(self, hass_mock, di_sensor, zone_deficit=0.0, threshold=15.0):
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Garden",
                "valve": "switch.valve_garden",
                CONF_ZONE_AREA: 20.0,
                CONF_ZONE_EFFICIENCY: 0.85,
                CONF_ZONE_FLOW_RATE: 8.0,
                CONF_ZONE_THRESHOLD: threshold,
            },
            di_sensor,
        )
        zone._zone_deficit = zone_deficit
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        return ctrl, zone

    @pytest.mark.asyncio
    async def test_alerts_when_deficit_exceeds_2x_threshold(self, hass_mock, di_sensor):
        """Should alert when deficit > 2× threshold."""
        ctrl, _ = self._make_controller(hass_mock, di_sensor, zone_deficit=35.0, threshold=15.0)
        await ctrl._check_deficit_anomaly()

        hass_mock.services.async_call.assert_called_once()
        call_args = hass_mock.services.async_call.call_args
        assert call_args.args[0] == "persistent_notification"
        assert "35.0 mm" in call_args.args[2]["message"]
        assert "Garden" in ctrl._deficit_anomaly_alerted

    @pytest.mark.asyncio
    async def test_no_alert_below_2x_threshold(self, hass_mock, di_sensor):
        """Should not alert when deficit < 2× threshold."""
        ctrl, _ = self._make_controller(hass_mock, di_sensor, zone_deficit=25.0, threshold=15.0)
        await ctrl._check_deficit_anomaly()

        hass_mock.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_only_once(self, hass_mock, di_sensor):
        """Should not re-alert for same zone."""
        ctrl, _ = self._make_controller(hass_mock, di_sensor, zone_deficit=40.0, threshold=15.0)
        await ctrl._check_deficit_anomaly()
        await ctrl._check_deficit_anomaly()

        assert hass_mock.services.async_call.call_count == 1

    @pytest.mark.asyncio
    async def test_re_alerts_after_recovery(self, hass_mock, di_sensor):
        """Should re-alert if deficit drops and rises again."""
        ctrl, zone = self._make_controller(hass_mock, di_sensor, zone_deficit=40.0, threshold=15.0)
        await ctrl._check_deficit_anomaly()
        assert "Garden" in ctrl._deficit_anomaly_alerted

        # Deficit recovers
        zone._zone_deficit = 10.0
        await ctrl._check_deficit_anomaly()
        assert "Garden" not in ctrl._deficit_anomaly_alerted

        # Deficit rises again
        zone._zone_deficit = 35.0
        await ctrl._check_deficit_anomaly()
        assert hass_mock.services.async_call.call_count == 2

    @pytest.mark.asyncio
    async def test_exactly_at_2x_threshold_alerts(self, hass_mock, di_sensor):
        """Deficit exactly at 2× threshold should trigger alert."""
        ctrl, _ = self._make_controller(hass_mock, di_sensor, zone_deficit=30.0, threshold=15.0)
        await ctrl._check_deficit_anomaly()

        assert "Garden" in ctrl._deficit_anomaly_alerted


class TestBatteryMonitoringEdgeCases:
    """Edge cases for battery alert logic."""

    def _make_battery_event(self, entity_id, level):
        event = MagicMock()
        new = MagicMock()
        new.state = str(level)
        event.data = {"entity_id": entity_id, "new_state": new}
        return event

    def _make_controller_with_battery(self, hass_mock, di_sensor):
        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Garden",
                "valve": "switch.valve_garden",
                CONF_ZONE_AREA: 20.0,
                CONF_ZONE_EFFICIENCY: 0.85,
                CONF_ZONE_FLOW_RATE: 8.0,
                "battery_sensor": "sensor.valve_battery",
            },
            di_sensor,
        )
        return IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    def test_non_numeric_battery_state(self, hass_mock, di_sensor):
        """Non-numeric battery state should not crash."""
        ctrl = self._make_controller_with_battery(hass_mock, di_sensor)
        event = self._make_battery_event("sensor.valve_battery", "unavailable")
        ctrl._on_battery_change(event)  # should not raise
        assert len(ctrl._battery_alerted) == 0

    def test_exactly_at_threshold_alerts(self, hass_mock, di_sensor):
        """Battery exactly at 15% should trigger alert."""
        ctrl = self._make_controller_with_battery(hass_mock, di_sensor)
        event = self._make_battery_event("sensor.valve_battery", 15)
        ctrl._on_battery_change(event)
        assert "Garden" in ctrl._battery_alerted

    def test_unknown_battery_entity_ignored(self, hass_mock, di_sensor):
        """Battery event for unknown entity should be ignored."""
        ctrl = self._make_controller_with_battery(hass_mock, di_sensor)
        event = self._make_battery_event("sensor.unknown_battery", 5)
        ctrl._on_battery_change(event)
        assert len(ctrl._battery_alerted) == 0

    def test_none_new_state_ignored(self, hass_mock, di_sensor):
        """Battery event with new_state=None should be silently ignored."""
        ctrl = self._make_controller_with_battery(hass_mock, di_sensor)
        event = MagicMock()
        event.data = {"entity_id": "sensor.valve_battery", "new_state": None}
        ctrl._on_battery_change(event)
        assert len(ctrl._battery_alerted) == 0


class TestMarkIrrigatedFeedback:
    """Test that mark_irrigated now sets irrigation timestamps via reset_deficit."""

    @pytest.mark.asyncio
    async def test_mark_irrigated_sets_last_irrigated(self, controller, zone_orto):
        """mark_irrigated calls reset_deficit which should set last_irrigated."""
        zone_orto._zone_deficit = 15.0
        assert zone_orto._last_irrigated is None

        call_mock = MagicMock()
        call_mock.data = {"zone_name": "Orto"}
        await controller._handle_mark_irrigated(call_mock)

        assert zone_orto._last_irrigated is not None
        assert zone_orto._last_volume_delivered > 0
