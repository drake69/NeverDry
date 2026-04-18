"""Button platform for the NeverDry integration.

Provides a "Mark as irrigated" button for each configured irrigation zone.
When pressed, resets the zone's deficit to zero via the mark_irrigated service.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    async_add_entities(_create_buttons(hass, dict(entry.data)), True)


def _create_buttons(hass: HomeAssistant, config: dict) -> list[ButtonEntity]:
    """Create button entities for each configured zone."""
    buttons: list[ButtonEntity] = []
    for zone_conf in config.get(CONF_ZONES, []):
        zone_name = zone_conf[CONF_ZONE_NAME]
        buttons.append(MarkIrrigatedButton(hass, zone_name))
        buttons.append(IrrigateButton(hass, zone_name))
    return buttons


class MarkIrrigatedButton(ButtonEntity):
    """Button to mark a zone as manually irrigated."""

    _attr_icon = "mdi:water-check"

    def __init__(self, hass: HomeAssistant, zone_name: str) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = f"Mark {zone_name} irrigated"
        self._attr_unique_id = f"mark_irrigated_{slug}"

    async def async_press(self) -> None:
        """Handle the button press — reset zone deficit."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_IRRIGATED,
            {ATTR_ZONE_NAME: self._zone_name},
        )


class IrrigateButton(ButtonEntity):
    """Button to trigger irrigation for a zone based on current deficit."""

    _attr_icon = "mdi:sprinkler"

    def __init__(self, hass: HomeAssistant, zone_name: str) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = f"Irrigate {zone_name}"
        self._attr_unique_id = f"irrigate_{slug}"

    async def async_press(self) -> None:
        """Handle the button press — start irrigation for this zone."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_IRRIGATE_ZONE,
            {ATTR_ZONE_NAME: self._zone_name},
        )
