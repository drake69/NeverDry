"""NeverDry — Home Assistant Custom Integration.

Calculates cumulative soil water deficit based on real-time
evapotranspiration and precipitation, following a simplified FAO-56
water balance model.  Directly controls irrigation valves.

Supports both YAML configuration and UI-based config flow.
"""

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import CONFIG_VERSION, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the NeverDry integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to the current schema version.

    Called automatically by HA when entry.version < ConfigFlow.VERSION.
    Add migration steps here when CONFIG_VERSION is bumped.
    """
    _LOGGER.debug(
        "Migrating NeverDry config entry from version %s to %s",
        entry.version,
        CONFIG_VERSION,
    )

    if entry.version > CONFIG_VERSION:
        _LOGGER.error(
            "Config entry version %s is newer than supported (%s)",
            entry.version,
            CONFIG_VERSION,
        )
        return False

    if entry.version == 1:
        new_data = {**entry.data}
        for zone in new_data.get("zones", []):
            zone.setdefault("delivery_mode", "estimated_flow")
        hass.config_entries.async_update_entry(entry, data=new_data, version=2)

    _LOGGER.info(
        "Migration of NeverDry config entry to version %s successful",
        CONFIG_VERSION,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NeverDry from a config entry (UI)."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
