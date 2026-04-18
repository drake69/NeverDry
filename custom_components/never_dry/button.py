"""Button platform for the NeverDry integration.

Provides per-zone buttons: "Irrigate" and "Mark as irrigated".
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ZONE_NAME,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DOMAIN,
    SERVICE_IRRIGATE_ZONE,
    SERVICE_MARK_IRRIGATED,
)


def _zone_device_info(entry_id: str, zone_name: str) -> DeviceInfo:
    """Device info matching the zone device created in sensor.py."""
    slug = zone_name.lower().replace(" ", "_")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{slug}")},
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities,
    discovery_info=None,
) -> None:
    """Set up the NeverDry buttons from YAML configuration."""
    async_add_entities(_create_buttons(hass, config), True)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the NeverDry buttons from a config entry (UI)."""
    async_add_entities(_create_buttons(hass, dict(entry.data), entry.entry_id), True)


def _create_buttons(hass: HomeAssistant, config: dict, entry_id: str = "yaml") -> list[ButtonEntity]:
    """Create button entities for each configured zone."""
    buttons: list[ButtonEntity] = []
    for zone_conf in config.get(CONF_ZONES, []):
        zone_name = zone_conf[CONF_ZONE_NAME]
        device_info = _zone_device_info(entry_id, zone_name)
        buttons.append(MarkIrrigatedButton(hass, zone_name, device_info))
        buttons.append(IrrigateButton(hass, zone_name, device_info))
    return buttons


class MarkIrrigatedButton(ButtonEntity):
    """Button to mark a zone as manually irrigated."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-check"

    def __init__(self, hass: HomeAssistant, zone_name: str, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = "Mark irrigated"
        self._attr_unique_id = f"mark_irrigated_{slug}"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — reset zone deficit."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_IRRIGATED,
            {ATTR_ZONE_NAME: self._zone_name},
        )


class IrrigateButton(ButtonEntity):
    """Button to trigger irrigation for a zone based on current deficit."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:sprinkler"

    def __init__(self, hass: HomeAssistant, zone_name: str, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = "Irrigate"
        self._attr_unique_id = f"irrigate_{slug}"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — start irrigation for this zone."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_IRRIGATE_ZONE,
            {ATTR_ZONE_NAME: self._zone_name},
        )
