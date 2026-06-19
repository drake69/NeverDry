"""Tests for ZoneLinkedSensor — mirrors external HA entities in the zone device."""

from unittest.mock import MagicMock, patch

import pytest
from never_dry.sensor import ZoneLinkedSensor


def _make_hass(entity_states: dict | None = None):
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    states = entity_states or {}

    def get_state(entity_id):
        return states.get(entity_id)

    hass.states.get = get_state
    return hass


def _make_state(state_value, unit=None):
    s = MagicMock()
    s.state = state_value
    s.attributes = {"unit_of_measurement": unit} if unit else {}
    return s


class TestZoneLinkedSensorInit:
    def test_name_and_icon(self):
        hass = _make_hass()
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid_1")
        assert sensor._attr_name == "Valve"
        assert sensor._attr_icon == "mdi:valve"

    def test_unique_id(self):
        hass = _make_hass()
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "linked_valve_orto")
        assert sensor._attr_unique_id == "linked_valve_orto"

    def test_device_info_assigned(self):
        hass = _make_hass()
        from homeassistant.helpers.device_registry import DeviceInfo

        device = DeviceInfo(identifiers={("never_dry", "orto")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid", device)
        assert sensor._attr_device_info is device


class TestZoneLinkedSensorValue:
    def test_valve_on_returns_open(self):
        hass = _make_hass({"switch.valve": _make_state("on")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.native_value == "open"

    def test_valve_off_returns_closed(self):
        hass = _make_hass({"switch.valve": _make_state("off")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.native_value == "closed"

    def test_numeric_sensor_returns_float(self):
        hass = _make_hass({"sensor.battery": _make_state("85", "%")})
        sensor = ZoneLinkedSensor(hass, "sensor.battery", "Battery", "mdi:battery", "uid")
        sensor.hass = hass
        assert sensor.native_value == pytest.approx(85.0)

    def test_float_sensor_returns_float(self):
        hass = _make_hass({"sensor.flow": _make_state("3.5", "L/min")})
        sensor = ZoneLinkedSensor(hass, "sensor.flow", "Flow meter", "mdi:water-flow", "uid")
        sensor.hass = hass
        assert sensor.native_value == pytest.approx(3.5)

    def test_unavailable_source_returns_none(self):
        hass = _make_hass({"switch.valve": _make_state("unavailable")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.native_value is None

    def test_unknown_source_returns_none(self):
        hass = _make_hass({"switch.valve": _make_state("unknown")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.native_value is None

    def test_missing_entity_returns_none(self):
        hass = _make_hass({})
        sensor = ZoneLinkedSensor(hass, "switch.missing", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.native_value is None


class TestZoneLinkedSensorUnit:
    def test_inherits_unit_from_source(self):
        hass = _make_hass({"sensor.battery": _make_state("85", "%")})
        sensor = ZoneLinkedSensor(hass, "sensor.battery", "Battery", "mdi:battery", "uid")
        sensor.hass = hass
        assert sensor.native_unit_of_measurement == "%"

    def test_no_unit_when_source_missing(self):
        hass = _make_hass({})
        sensor = ZoneLinkedSensor(hass, "switch.missing", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.native_unit_of_measurement is None

    def test_valve_switch_has_no_unit(self):
        hass = _make_hass({"switch.valve": _make_state("on")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.native_unit_of_measurement is None


class TestZoneLinkedSensorAvailability:
    def test_available_when_source_ok(self):
        hass = _make_hass({"switch.valve": _make_state("on")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.available is True

    def test_unavailable_when_source_unavailable(self):
        hass = _make_hass({"switch.valve": _make_state("unavailable")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.available is False

    def test_unavailable_when_entity_missing(self):
        hass = _make_hass({})
        sensor = ZoneLinkedSensor(hass, "switch.missing", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        assert sensor.available is False


class TestZoneLinkedSensorStateChange:
    @pytest.mark.asyncio
    async def test_subscribes_on_added_to_hass(self):
        hass = _make_hass({"switch.valve": _make_state("off")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        with patch("never_dry.sensor.async_track_state_change_event") as mock_track:
            await sensor.async_added_to_hass()
            mock_track.assert_called_once_with(hass, ["switch.valve"], sensor._on_source_change)

    def test_on_source_change_writes_state(self):
        hass = _make_hass({"switch.valve": _make_state("on")})
        sensor = ZoneLinkedSensor(hass, "switch.valve", "Valve", "mdi:valve", "uid")
        sensor.hass = hass
        sensor.async_write_ha_state = MagicMock()
        event = MagicMock()
        sensor._on_source_change(event)
        sensor.async_write_ha_state.assert_called_once()
