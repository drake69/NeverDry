"""Tests for zone device-registry cleanup on zone deletion.

Covers the bug where deleting a zone left an orphaned device in the
registry (entities removed, device card lingering) which blocked a clean
uninstall of the integration.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from never_dry import (
    async_remove_config_entry_device,
    zone_device_identifier,
    zone_slug,
)
from never_dry.const import CONF_ZONE_NAME, CONF_ZONES, DOMAIN


def _make_entry(entry_id: str, zone_names: list[str]) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {CONF_ZONES: [{CONF_ZONE_NAME: n} for n in zone_names]}
    return entry


def _make_device(*identifiers: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(id="dev1", identifiers=set(identifiers))


class TestZoneSlug:
    def test_lowercases_and_replaces_spaces(self):
        assert zone_slug("Orto Grande") == "orto_grande"

    def test_identifier_format(self):
        assert zone_device_identifier("abc", "Orto Grande") == (DOMAIN, "abc_orto_grande")


class TestAsyncRemoveConfigEntryDevice:
    """async_remove_config_entry_device gates the UI delete button."""

    @pytest.mark.asyncio
    async def test_orphan_zone_device_is_removable(self, hass_mock):
        """A device for a zone no longer in config can be deleted."""
        entry = _make_entry("abc", ["Orto"])
        # Device belongs to a zone "Prato" that was deleted from config.
        device = _make_device(zone_device_identifier("abc", "Prato"))
        assert await async_remove_config_entry_device(hass_mock, entry, device) is True

    @pytest.mark.asyncio
    async def test_configured_zone_device_is_kept(self, hass_mock):
        """A device for a still-configured zone must NOT be removable."""
        entry = _make_entry("abc", ["Orto", "Prato"])
        device = _make_device(zone_device_identifier("abc", "Orto"))
        assert await async_remove_config_entry_device(hass_mock, entry, device) is False

    @pytest.mark.asyncio
    async def test_hub_device_is_kept(self, hass_mock):
        """The hub device (entry_id identifier) must NOT be removable."""
        entry = _make_entry("abc", ["Orto"])
        device = _make_device((DOMAIN, "abc"))
        assert await async_remove_config_entry_device(hass_mock, entry, device) is False

    @pytest.mark.asyncio
    async def test_zone_with_spaces_in_name(self, hass_mock):
        """Slug with spaces is matched correctly against config zones."""
        entry = _make_entry("abc", ["Orto Grande"])
        device = _make_device(zone_device_identifier("abc", "Orto Grande"))
        assert await async_remove_config_entry_device(hass_mock, entry, device) is False


class TestConfigFlowRemovesDevice:
    """The options flow proactively removes the device on zone deletion."""

    @pytest.mark.asyncio
    async def test_remove_zone_device_calls_registry(self, hass_mock, monkeypatch):
        import never_dry.config_flow as cf

        entry = _make_entry("abc", ["Orto", "Prato"])

        registry = MagicMock()
        found_device = SimpleNamespace(id="dev-orto", identifiers={zone_device_identifier("abc", "Orto")})
        registry.async_get_device.return_value = found_device
        monkeypatch.setattr(cf.dr, "async_get", lambda hass: registry, raising=False)

        flow = cf.NeverDryOptionsFlow(entry)
        flow.hass = hass_mock

        flow._remove_zone_device("Orto")

        registry.async_get_device.assert_called_once_with(identifiers={zone_device_identifier("abc", "Orto")})
        registry.async_remove_device.assert_called_once_with("dev-orto")

    @pytest.mark.asyncio
    async def test_remove_zone_device_noop_when_absent(self, hass_mock, monkeypatch):
        import never_dry.config_flow as cf

        entry = _make_entry("abc", ["Orto"])
        registry = MagicMock()
        registry.async_get_device.return_value = None
        monkeypatch.setattr(cf.dr, "async_get", lambda hass: registry, raising=False)

        flow = cf.NeverDryOptionsFlow(entry)
        flow.hass = hass_mock

        flow._remove_zone_device("Ghost")

        registry.async_remove_device.assert_not_called()
