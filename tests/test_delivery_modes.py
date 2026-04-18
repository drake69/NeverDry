"""Tests for the three valve delivery modes."""

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
    CONF_ZONE_VALVE,
    CONF_ZONE_VOLUME_ENTITY,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    FLOW_METER_POLL_INTERVAL_S,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor


def _make_zone(hass_mock, di_sensor, **overrides):
    """Create a zone sensor with given overrides."""
    config = {
        CONF_ZONE_NAME: "TestZone",
        CONF_ZONE_VALVE: "switch.valve_test",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_ESTIMATED_FLOW,
    }
    config.update(overrides)
    return IrrigationZoneSensor(hass_mock, config, di_sensor)


class TestEstimatedFlowDelivery:
    """Test estimated_flow delivery mode (existing behavior)."""

    @pytest.mark.asyncio
    async def test_opens_waits_closes(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 5.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._wait_with_stop_check = AsyncMock()

        await ctrl._deliver_estimated_flow(zone)

        # Valve should have been opened and closed
        calls = hass_mock.services.async_call.call_args_list
        assert any("turn_on" in str(c) for c in calls)
        assert any("turn_off" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_skips_zero_duration(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 0.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        result = await ctrl._deliver_estimated_flow(zone)

        assert result == 0.0
        hass_mock.services.async_call.assert_not_called()

    def test_default_delivery_mode(self, hass_mock, di_sensor):
        """Zone without explicit delivery_mode defaults to estimated_flow."""
        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Default",
                CONF_ZONE_VALVE: "switch.valve",
                CONF_ZONE_AREA: 10.0,
                CONF_ZONE_FLOW_RATE: 5.0,
            },
            di_sensor,
        )
        assert zone.delivery_mode == DELIVERY_MODE_ESTIMATED_FLOW


class TestVolumePresetDelivery:
    """Test volume_preset delivery mode."""

    @pytest.mark.asyncio
    async def test_sends_volume_to_number_entity(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_DELIVERY_TIMEOUT: 10,
            },
        )
        zone._zone_deficit = 5.0

        # Simulate valve closing itself after set_value
        valve_state = MagicMock()
        valve_state.state = "off"
        hass_mock.states.get = MagicMock(return_value=valve_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_volume_preset(zone)

        assert result > 0
        # Check number.set_value was called
        set_value_calls = [
            c
            for c in hass_mock.services.async_call.call_args_list
            if c.args[0] == "number" and c.args[1] == "set_value"
        ]
        assert len(set_value_calls) == 1
        assert set_value_calls[0].args[2]["entity_id"] == "number.valve_volume"

    @pytest.mark.asyncio
    async def test_timeout_forces_close(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_DELIVERY_TIMEOUT: FLOW_METER_POLL_INTERVAL_S,  # very short timeout
            },
        )
        zone._zone_deficit = 5.0

        # Valve never closes itself
        valve_state = MagicMock()
        valve_state.state = "on"
        hass_mock.states.get = MagicMock(return_value=valve_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_volume_preset(zone)

        assert result > 0
        # Valve should be force-closed
        close_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_off" in str(c)]
        assert len(close_calls) >= 1

    @pytest.mark.asyncio
    async def test_no_volume_entity_returns_false(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET},
        )
        zone._zone_deficit = 5.0

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_volume_preset(zone)

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_stop_during_preset(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0

        valve_state = MagicMock()
        valve_state.state = "on"
        hass_mock.states.get = MagicMock(return_value=valve_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._stop_requested = True

        result = await ctrl._deliver_volume_preset(zone)

        assert result == 0.0


class TestFlowMeterDelivery:
    """Test flow_meter delivery mode."""

    @pytest.mark.asyncio
    async def test_closes_at_target_volume(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0
        target_volume = zone.volume_liters

        # Simulate flow meter: starts at 100, ends at 100 + target (cumulative L)
        readings = iter([100.0, 100.0, 100.0 + target_volume + 1])
        meter_state = MagicMock()
        meter_state.attributes = {"unit_of_measurement": "L"}

        def get_state(entity_id):
            if entity_id == "sensor.flow_meter":
                meter_state.state = str(next(readings))
                return meter_state
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result > 0
        # Valve should have been opened and closed
        close_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_off" in str(c)]
        assert len(close_calls) >= 1

    @pytest.mark.asyncio
    async def test_unavailable_sensor_skips(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
            },
        )
        zone._zone_deficit = 5.0

        unavailable = MagicMock()
        unavailable.state = "unavailable"
        hass_mock.states.get = MagicMock(return_value=unavailable)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0
        # No valve should have been opened
        open_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_on" in str(c)]
        assert len(open_calls) == 0

    @pytest.mark.asyncio
    async def test_no_flow_meter_entity_returns_false(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER},
        )
        zone._zone_deficit = 5.0

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_meter_reset_adjusts_baseline(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0
        target_volume = zone.volume_liters

        # Simulate: unit check, initial=100, then meter resets to 50, then reaches target
        readings = iter([100.0, 100.0, 50.0, target_volume + 1])
        meter_state = MagicMock()
        meter_state.attributes = {"unit_of_measurement": "L"}

        def get_state(entity_id):
            if entity_id == "sensor.flow_meter":
                meter_state.state = str(next(readings))
                return meter_state
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result > 0

    @pytest.mark.asyncio
    async def test_stop_during_flow_meter(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0

        meter_state = MagicMock()
        meter_state.state = "0.0"
        meter_state.attributes = {"unit_of_measurement": "L"}
        hass_mock.states.get = MagicMock(return_value=meter_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._stop_requested = True

        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0


class TestDeliveryModeDispatch:
    """Test the _deliver_water dispatch method."""

    @pytest.mark.asyncio
    async def test_dispatches_estimated_flow(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 5.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._wait_with_stop_check = AsyncMock()

        result = await ctrl._deliver_water(zone)

        assert result > 0

    @pytest.mark.asyncio
    async def test_unknown_mode_returns_false(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._delivery_mode = "nonexistent_mode"
        zone._zone_deficit = 5.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        result = await ctrl._deliver_water(zone)

        assert result == 0.0


class TestDurationByMode:
    """Test that duration_s returns 0 for non-estimated_flow modes."""

    def test_estimated_flow_has_duration(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 5.0
        assert zone.duration_s > 0

    def test_flow_meter_zero_duration(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER},
        )
        zone._zone_deficit = 5.0
        assert zone.duration_s == 0

    def test_volume_preset_zero_duration(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET},
        )
        zone._zone_deficit = 5.0
        assert zone.duration_s == 0


class TestDeliveryModeAttributes:
    """Test delivery mode in zone state attributes."""

    def test_delivery_mode_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        assert zone.extra_state_attributes["delivery_mode"] == DELIVERY_MODE_ESTIMATED_FLOW

    def test_volume_entity_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_vol",
            },
        )
        attrs = zone.extra_state_attributes
        assert attrs["volume_entity"] == "number.valve_vol"
        assert "delivery_timeout_s" in attrs

    def test_flow_meter_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow",
            },
        )
        attrs = zone.extra_state_attributes
        assert attrs["flow_meter_sensor"] == "sensor.flow"
        assert "delivery_timeout_s" in attrs

    def test_estimated_flow_no_timeout_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        attrs = zone.extra_state_attributes
        assert "delivery_timeout_s" not in attrs
