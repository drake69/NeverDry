"""Tests for IrrigationZoneSensor — per-zone volume and duration calculations."""

from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_KC,
    CONF_ZONE_NAME,
    CONF_ZONE_PLANT_FAMILY,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
)
from never_dry.sensor import IrrigationZoneSensor


def _make_hass_mock():
    """Create a hass mock with latitude."""
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    return hass


def _make_zone(
    di_sensor,
    name="Test",
    area=45.0,
    efficiency=0.85,
    flow_rate=10.0,
    valve="switch.valve",
    threshold=20.0,
    plant_family=None,
    kc=None,
):
    """Helper to create a zone sensor with specific params."""
    zone_config = {
        CONF_ZONE_NAME: name,
        CONF_ZONE_VALVE: valve,
        CONF_ZONE_AREA: area,
        CONF_ZONE_EFFICIENCY: efficiency,
        CONF_ZONE_FLOW_RATE: flow_rate,
        CONF_ZONE_THRESHOLD: threshold,
    }
    if plant_family is not None:
        zone_config[CONF_ZONE_PLANT_FAMILY] = plant_family
    if kc is not None:
        zone_config[CONF_ZONE_KC] = kc
    return IrrigationZoneSensor(_make_hass_mock(), zone_config, di_sensor)


class TestVolumeCalculation:
    """Test V = D_zone * A / η [liters]."""

    def test_basic_volume(self, di_sensor):
        """10mm deficit, 45m², η=0.85 → 529.4 L."""
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 10.0
        assert zone.volume_liters == pytest.approx(529.4, abs=0.1)

    def test_zero_deficit(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 0.0
        assert zone.volume_liters == 0.0

    def test_high_deficit(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 100.0
        expected = 100.0 * 45.0 / 0.85
        assert zone.volume_liters == pytest.approx(expected, abs=0.1)

    def test_small_area(self, di_sensor):
        """Balcony pots: 2m²."""
        zone = _make_zone(di_sensor, area=2.0, efficiency=0.90)
        zone._zone_deficit = 5.0
        # 5 * 2 / 0.90 = 11.1 L
        assert zone.volume_liters == pytest.approx(11.1, abs=0.1)

    def test_zero_efficiency_no_crash(self, di_sensor):
        zone = _make_zone(di_sensor, efficiency=0.0)
        zone._zone_deficit = 10.0
        assert zone.volume_liters == 0.0


class TestDurationCalculation:
    """Test t_irr = V / Q * 60 [seconds]."""

    def test_basic_duration(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 10.0
        volume = 10.0 * 45.0 / 0.85
        expected_s = round(volume / 10.0 * 60)
        assert zone.duration_s == expected_s

    def test_zero_deficit_zero_duration(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 0.0
        assert zone.duration_s == 0

    def test_high_flow_rate_shorter(self, di_sensor):
        slow = _make_zone(di_sensor, flow_rate=5.0)
        fast = _make_zone(di_sensor, flow_rate=20.0)
        slow._zone_deficit = 10.0
        fast._zone_deficit = 10.0
        assert slow.duration_s > fast.duration_s
        assert slow.duration_s == pytest.approx(fast.duration_s * 4, abs=1)

    def test_zero_flow_rate_no_crash(self, di_sensor):
        zone = _make_zone(di_sensor, flow_rate=0.0)
        zone._zone_deficit = 10.0
        assert zone.duration_s == 0

    def test_zero_area_zero_duration(self, di_sensor):
        zone = _make_zone(di_sensor, area=0.0)
        zone._zone_deficit = 10.0
        assert zone.volume_liters == 0.0
        assert zone.duration_s == 0


class TestNativeValue:
    """native_value is volume in liters (rounded)."""

    def test_native_value_is_volume(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 10.0
        assert zone.native_value == round(zone.volume_liters, 1)

    def test_native_value_zero(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 0.0
        assert zone.native_value == 0.0


class TestZoneAttributes:
    """Test extra_state_attributes contain all zone info."""

    def test_all_keys_present(self, di_sensor):
        zone = _make_zone(di_sensor, name="Orto", valve="switch.v1", plant_family="vegetables")
        zone._zone_deficit = 5.0
        attrs = zone.extra_state_attributes
        assert attrs["zone_name"] == "Orto"
        assert attrs["valve"] == "switch.v1"
        assert attrs["area_m2"] == 45.0
        assert attrs["efficiency"] == 0.85
        assert attrs["flow_rate_lpm"] == 10.0
        assert attrs["threshold_mm"] == 20.0
        assert attrs["plant_family"] == "vegetables"
        assert "kc" in attrs
        assert "kc_override" in attrs
        assert "volume_liters" in attrs
        assert "duration_s" in attrs
        assert attrs["deficit_mm"] == 5.0
        assert "irrigating" in attrs
        assert "system_type" in attrs

    def test_zone_tracks_own_deficit(self, di_sensor):
        """Zone should track its own deficit, not the shared one."""
        zone = _make_zone(di_sensor)
        zone._zone_deficit = 0.0
        assert zone.extra_state_attributes["deficit_mm"] == 0.0
        zone._zone_deficit = 42.0
        assert zone.extra_state_attributes["deficit_mm"] == 42.0


class TestZoneMetadata:
    """Test sensor name and unique_id generation."""

    def test_name(self, di_sensor):
        zone = _make_zone(di_sensor, name="Orto")
        assert zone._attr_name == "Irrigation Orto"

    def test_unique_id(self, di_sensor):
        zone = _make_zone(di_sensor, name="Orto")
        assert zone._attr_unique_id == "irrigation_zone_orto"

    def test_unique_id_with_spaces(self, di_sensor):
        zone = _make_zone(di_sensor, name="Giardino Sud")
        assert zone._attr_unique_id == "irrigation_zone_giardino_sud"

    def test_unit(self, di_sensor):
        zone = _make_zone(di_sensor)
        assert zone._attr_native_unit_of_measurement == "L"

    def test_icon(self, di_sensor):
        zone = _make_zone(di_sensor)
        assert zone._attr_icon == "mdi:sprinkler-variant"


class TestMultiZone:
    """Test that multiple zones compute independently with per-zone deficit."""

    def test_different_volumes_different_deficits(self, di_sensor, zone_orto, zone_prato):
        """Two zones with different deficits should have different volumes."""
        zone_orto._zone_deficit = 10.0
        zone_prato._zone_deficit = 10.0
        # Orto: 10 * 20 / 0.90 = 222.2 L
        # Prato: 10 * 50 / 0.70 = 714.3 L
        assert zone_orto.volume_liters == pytest.approx(222.2, abs=0.1)
        assert zone_prato.volume_liters == pytest.approx(714.3, abs=0.1)

    def test_different_durations(self, di_sensor, zone_orto, zone_prato):
        """Two zones should have different durations based on their flow rates."""
        zone_orto._zone_deficit = 10.0
        zone_prato._zone_deficit = 10.0
        # Orto: 222.2 / 8 * 60 = 1667 s
        # Prato: 714.3 / 15 * 60 = 2857 s
        assert zone_orto.duration_s == pytest.approx(1667, abs=1)
        assert zone_prato.duration_s == pytest.approx(2857, abs=1)

    def test_reset_zeroes_zone_deficit(self, di_sensor, zone_orto, zone_prato):
        """reset_deficit zeroes only that zone."""
        zone_orto._zone_deficit = 10.0
        zone_prato._zone_deficit = 10.0
        assert zone_orto.volume_liters > 0
        assert zone_prato.volume_liters > 0

        zone_orto.reset_deficit()
        assert zone_orto.volume_liters == 0.0
        assert zone_prato.volume_liters > 0  # prato unaffected

    def test_zones_accumulate_independently(self, di_sensor, zone_orto, zone_prato):
        """Zone deficits are independent."""
        zone_orto._zone_deficit = 5.0
        zone_prato._zone_deficit = 20.0
        assert zone_orto.volume_liters < zone_prato.volume_liters


class TestIrrigationFeedback:
    """Test last_irrigated and last_volume_delivered tracking."""

    def test_reset_deficit_sets_last_irrigated(self, zone_orto):
        """reset_deficit should record a timestamp."""
        assert zone_orto._last_irrigated is None
        zone_orto._zone_deficit = 10.0
        zone_orto.reset_deficit()
        assert zone_orto._last_irrigated is not None

    def test_reset_deficit_sets_last_volume_delivered(self, zone_orto):
        """reset_deficit should capture volume before resetting."""
        zone_orto._zone_deficit = 10.0
        expected_volume = round(zone_orto.volume_liters, 1)
        zone_orto.reset_deficit()
        assert zone_orto._last_volume_delivered == expected_volume

    def test_reset_deficit_zero_volume(self, zone_orto):
        """reset_deficit with zero deficit should record 0 volume."""
        zone_orto._zone_deficit = 0.0
        zone_orto.reset_deficit()
        assert zone_orto._last_volume_delivered == 0.0
        assert zone_orto._last_irrigated is not None

    def test_last_irrigated_in_attributes(self, zone_orto, di_sensor):
        """last_irrigated should appear in extra_state_attributes after reset."""
        zone_orto._zone_deficit = 5.0
        zone_orto.reset_deficit()
        attrs = zone_orto.extra_state_attributes
        assert "last_irrigated" in attrs
        assert "last_volume_delivered" in attrs

    def test_no_last_irrigated_before_reset(self, zone_orto, di_sensor):
        """last_irrigated should NOT appear in attributes before any reset."""
        attrs = zone_orto.extra_state_attributes
        assert "last_irrigated" not in attrs

    def test_restore_last_irrigated(self, hass_mock, di_sensor):
        """last_irrigated and last_volume_delivered should be restored from state."""
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock

        from never_dry.sensor import IrrigationZoneSensor

        zone = IrrigationZoneSensor(
            hass_mock,
            {
                "name": "Test",
                "area_m2": 20.0,
                "efficiency": 0.9,
                "flow_rate_lpm": 8.0,
            },
            di_sensor,
        )

        # Simulate RestoreEntity returning a previous state
        last_state = MagicMock()
        last_state.attributes = {
            "deficit_mm": "3.5",
            "last_irrigated": "2026-04-15T10:30:00",
            "last_volume_delivered": "55.0",
        }
        zone.async_get_last_state = AsyncMock(return_value=last_state)

        import asyncio
        asyncio.get_event_loop().run_until_complete(zone.async_added_to_hass())

        assert zone._last_irrigated == datetime.fromisoformat("2026-04-15T10:30:00")
        assert zone._last_volume_delivered == 55.0
        assert zone._zone_deficit == 3.5
