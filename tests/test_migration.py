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
