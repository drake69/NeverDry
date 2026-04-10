"""Tests for config entry migration (async_migrate_entry)."""

from unittest.mock import MagicMock

import pytest
from never_dry import async_migrate_entry
from never_dry.const import CONFIG_VERSION


def _make_entry(version: int) -> MagicMock:
    """Create a mock ConfigEntry with the given version."""
    entry = MagicMock()
    entry.version = version
    return entry


class TestConfigMigration:
    """Test async_migrate_entry behavior."""

    @pytest.mark.asyncio
    async def test_current_version_succeeds(self, hass_mock):
        """Entry at current CONFIG_VERSION should migrate successfully."""
        entry = _make_entry(CONFIG_VERSION)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True

    @pytest.mark.asyncio
    async def test_older_version_succeeds(self, hass_mock):
        """Entry older than CONFIG_VERSION should migrate successfully."""
        entry = _make_entry(1)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True

    @pytest.mark.asyncio
    async def test_newer_version_fails(self, hass_mock):
        """Entry newer than CONFIG_VERSION should fail (downgrade not supported)."""
        entry = _make_entry(CONFIG_VERSION + 1)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is False

    @pytest.mark.asyncio
    async def test_far_future_version_fails(self, hass_mock):
        """Entry from far future should fail gracefully."""
        entry = _make_entry(999)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is False

    @pytest.mark.asyncio
    async def test_v1_to_v2_adds_delivery_mode(self, hass_mock):
        """V1 entry should get delivery_mode added to all zones."""
        entry = _make_entry(1)
        entry.data = {
            "temperature_sensor": "sensor.temp",
            "rain_sensor": "sensor.rain",
            "zones": [
                {"name": "Orto", "valve": "switch.v1"},
                {"name": "Prato", "valve": "switch.v2"},
            ],
        }
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True
        # Check that zones got delivery_mode added
        updated_data = hass_mock.config_entries.async_update_entry.call_args
        new_data = updated_data.kwargs.get("data", updated_data[1].get("data", {}))
        for zone in new_data["zones"]:
            assert zone["delivery_mode"] == "estimated_flow"

    @pytest.mark.asyncio
    async def test_v1_to_v2_preserves_existing_fields(self, hass_mock):
        """V1 migration should not remove existing zone fields."""
        entry = _make_entry(1)
        entry.data = {
            "temperature_sensor": "sensor.temp",
            "rain_sensor": "sensor.rain",
            "zones": [
                {"name": "Orto", "valve": "switch.v1", "area_m2": 20.0},
            ],
        }
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True
        updated_data = hass_mock.config_entries.async_update_entry.call_args
        new_data = updated_data.kwargs.get("data", updated_data[1].get("data", {}))
        assert new_data["zones"][0]["area_m2"] == 20.0
        assert new_data["zones"][0]["name"] == "Orto"
