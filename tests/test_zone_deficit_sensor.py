"""Tests for ZoneDeficitSensor and zone deficit seeding."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
)
from never_dry.sensor import IrrigationZoneSensor, ZoneDeficitSensor


def _make_hass():
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    return hass


def _make_zone(di_sensor, name="Orto", area=20.0, efficiency=0.90):
    zone_config = {
        CONF_ZONE_NAME: name,
        CONF_ZONE_VALVE: "switch.valve",
        CONF_ZONE_AREA: area,
        CONF_ZONE_EFFICIENCY: efficiency,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
    }
    return IrrigationZoneSensor(_make_hass(), zone_config, di_sensor)


class TestZoneDeficitSensorProperties:
    """Test ZoneDeficitSensor entity attributes."""

    def test_name(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_name == "Deficit"

    def test_unique_id(self, di_sensor):
        zone = _make_zone(di_sensor, name="Giardino Melino")
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_unique_id == "deficit_zone_giardino_melino"

    def test_unit(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_native_unit_of_measurement == "mm"

    def test_icon(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_icon == "mdi:water-percent-alert"

    def test_has_entity_name(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_has_entity_name is True

    def test_device_info(self, di_sensor):
        zone = _make_zone(di_sensor)
        from homeassistant.helpers.device_registry import DeviceInfo

        device = DeviceInfo(identifiers={("never_dry", "test_orto")})
        deficit = ZoneDeficitSensor(zone, device)
        assert deficit._attr_device_info is device


class TestZoneDeficitSensorValue:
    """Test deficit value tracks zone deficit."""

    def test_initial_zero(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit.native_value == 0.0

    def test_tracks_zone_deficit(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        zone._zone_deficit = 5.67
        assert deficit.native_value == 5.67

    def test_updates_on_et_broadcast(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        # Simulate ET broadcast
        zone._on_et_update(1.0, 2.0, 0.0)
        # Zone deficit should have increased
        assert deficit.native_value > 0

    def test_rounds_to_two_decimals(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        zone._zone_deficit = 3.14159
        assert deficit.native_value == 3.14

    def test_after_reset(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        zone._zone_deficit = 10.0
        zone.reset_deficit()
        assert deficit.native_value == 0.0


class TestZoneDeficitSeeding:
    """Test that new zones seed deficit from global Dryness Index."""

    @pytest.mark.asyncio
    async def test_seed_from_dryness_index(self, di_sensor):
        """New zone (no restore) should seed deficit from DI * Kc."""
        zone = _make_zone(di_sensor)
        di_sensor._deficit = 8.0
        # Simulate async_added_to_hass with no previous state
        zone.async_get_last_state = AsyncMock(return_value=None)
        await zone.async_added_to_hass()
        # Kc=1.0 (no plant family), so zone deficit = 8.0
        assert zone._zone_deficit == pytest.approx(8.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_restore_overrides_seed(self, di_sensor):
        """Restored state should be used instead of seeding."""
        zone = _make_zone(di_sensor)
        di_sensor._deficit = 8.0
        last_state = MagicMock()
        last_state.attributes = {"deficit_mm": "3.5"}
        zone.async_get_last_state = AsyncMock(return_value=last_state)
        await zone.async_added_to_hass()
        assert zone._zone_deficit == pytest.approx(3.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_seed_zero_when_dryness_zero(self, di_sensor):
        """If DI is 0, new zone deficit should also be 0."""
        zone = _make_zone(di_sensor)
        di_sensor._deficit = 0.0
        zone.async_get_last_state = AsyncMock(return_value=None)
        await zone.async_added_to_hass()
        assert zone._zone_deficit == 0.0
