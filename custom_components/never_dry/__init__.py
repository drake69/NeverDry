"""NeverDry — Home Assistant Custom Integration.

Calculates cumulative soil water deficit based on real-time
evapotranspiration and precipitation, following a simplified FAO-56
water balance model.  Directly controls irrigation valves.

Supports both YAML configuration and UI-based config flow.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import pathlib

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ZONE_NAME, CONF_ZONES, CONFIG_VERSION, DOMAIN
from .services import async_unload_services


def zone_slug(zone_name: str) -> str:
    """Slug used to build a zone device identifier.

    Must stay in sync with the slug used in sensor.py / button.py
    DeviceInfo identifiers: ``(DOMAIN, f"{entry_id}_{slug}")``.
    """
    return zone_name.lower().replace(" ", "_")


def zone_device_identifier(entry_id: str, zone_name: str) -> tuple[str, str]:
    """Device-registry identifier for a zone device."""
    return (DOMAIN, f"{entry_id}_{zone_slug(zone_name)}")


_LOGGER = logging.getLogger(__name__)

_ACTIVITY_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_ACTIVITY_LOG_BACKUP_COUNT = 2
_INTEGRATION_VERSION: str = json.loads((pathlib.Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))[
    "version"
]


def _setup_file_logger(hass: HomeAssistant) -> logging.Handler:
    """Attach a rotating file handler to the never_dry logger namespace.

    All modules under custom_components.never_dry use _LOGGER = logging.getLogger(__name__),
    which inherits from this namespace. Attaching once here captures every
    INFO/DEBUG line across controller, sensor, valve_operator, etc.

    File: <ha_config_dir>/never_dry_activity.log (5 MB x 3 files).
    """
    log_path = hass.config.path("never_dry_activity.log")
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_ACTIVITY_LOG_MAX_BYTES,
        backupCount=_ACTIVITY_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    nd_logger = logging.getLogger("custom_components.never_dry")
    nd_logger.setLevel(logging.DEBUG)
    nd_logger.addHandler(handler)
    _LOGGER.info(
        "NeverDry %s — activity log -> %s (%.0f MB x %d)",
        _INTEGRATION_VERSION,
        log_path,
        _ACTIVITY_LOG_MAX_BYTES / 1024 / 1024,
        _ACTIVITY_LOG_BACKUP_COUNT + 1,
    )
    return handler


def _teardown_file_logger(handler: logging.Handler) -> None:
    """Remove the rotating file handler from the never_dry logger and close it."""
    nd_logger = logging.getLogger("custom_components.never_dry")
    nd_logger.removeHandler(handler)
    nd_logger.setLevel(logging.NOTSET)
    handler.close()


PLATFORMS = ["sensor", "button"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_FRONTEND_REGISTERED = "_frontend_registered"
_CARD_FILENAME = "never-dry-zone-card.js"
# Serve the whole www/ DIRECTORY (StaticPathConfig serves directories reliably,
# single files can 404), then reference the card file under it.
_STATIC_URL = f"/{DOMAIN}_static"
_CARD_URL = f"{_STATIC_URL}/{_CARD_FILENAME}"


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Register (or version-refresh) the card as a Lovelace resource.

    Lovelace resources are fetched dynamically by the frontend, so they keep
    working even when the service worker serves a stale cached index.html —
    the failure mode that makes frontend.add_extra_js_url unreliable after
    install/upgrade (GH issue #96). Only possible in storage mode; returns
    False for YAML-managed dashboards so the caller can fall back to
    add_extra_js_url.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.warning("NeverDry: Lovelace data not available, falling back to add_extra_js_url")
        return False
    # HA 2026.2 renamed LovelaceData.mode -> resource_mode; support both.
    mode = getattr(lovelace, "resource_mode", None) or getattr(lovelace, "mode", None)
    if mode != "storage":
        _LOGGER.info(
            "NeverDry: Lovelace resources managed in %s mode, falling back to add_extra_js_url",
            mode,
        )
        return False

    resources = lovelace.resources
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True

    for item in resources.async_items():
        if item.get("url", "").partition("?")[0] == _CARD_URL:
            if item["url"] != url:  # stale ?v= from a previous version: bust the browser cache
                await resources.async_update_item(item["id"], {"url": url})
                _LOGGER.info("NeverDry Zone Card Lovelace resource updated to %s", url)
            else:
                _LOGGER.debug("NeverDry Zone Card Lovelace resource already current (%s)", url)
            return True

    await resources.async_create_item({"res_type": "module", "url": url})
    _LOGGER.info("NeverDry Zone Card added to Lovelace resources (%s)", url)
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and auto-load the NeverDry Zone Lovelace card.

    Registers the bundled www/ folder as a static path, then exposes the card
    JS to the frontend so it appears in the "Add card" picker without manual
    resource setup. Storage-mode dashboards get a real Lovelace resource
    (robust against the service-worker-cached index, GH #96); YAML-mode
    dashboards fall back to add_extra_js_url. Runs once per HA instance.
    """
    if hass.data[DOMAIN].get(_FRONTEND_REGISTERED):
        _LOGGER.debug("NeverDry: frontend already registered, skipping")
        return

    www_dir = str(pathlib.Path(__file__).parent / "www")
    url = f"{_CARD_URL}?v={_INTEGRATION_VERSION}"
    try:
        from homeassistant.components.http import StaticPathConfig

        _LOGGER.info("NeverDry: registering static path %s -> %s", _STATIC_URL, www_dir)
        await hass.http.async_register_static_paths([StaticPathConfig(_STATIC_URL, www_dir, cache_headers=False)])

        if not await _async_register_lovelace_resource(hass, url):
            from homeassistant.components import frontend

            frontend.add_extra_js_url(hass, url)
        hass.data[DOMAIN][_FRONTEND_REGISTERED] = True
        _LOGGER.info("NeverDry Zone Card registered and auto-loaded (%s)", url)
    except Exception:  # never block integration setup on a frontend hiccup
        _LOGGER.exception("NeverDry: failed to register Lovelace card at %s", url)


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


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when config entry data changes (e.g. zone added)."""
    _LOGGER.info("Config entry data changed — reloading integration")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NeverDry from a config entry (UI)."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    handler = await hass.async_add_executor_job(_setup_file_logger, hass)
    hass.data[DOMAIN][f"_log_handler_{entry.entry_id}"] = handler
    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    controller = hass.data.get(DOMAIN, {}).pop(f"_controller_{entry.entry_id}", None)
    if controller is not None:
        await controller.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        handler = hass.data[DOMAIN].pop(f"_log_handler_{entry.entry_id}", None)
        if handler is not None:
            await hass.async_add_executor_job(_teardown_file_logger, handler)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(f"_operators_{entry.entry_id}", None)
        # Drop the domain services when the last controller is gone.
        async_unload_services(hass)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Allow manual deletion of stale zone devices from the UI.

    Returning True re-enables the "Delete device" button in Home Assistant.
    We allow removal of any device whose identifier no longer maps to a
    currently configured zone (orphans left behind after a zone was removed).
    The hub device and devices belonging to still-configured zones are kept.
    """
    valid_identifiers = {(DOMAIN, entry.entry_id)}  # hub device
    for zone in entry.data.get(CONF_ZONES, []):
        valid_identifiers.add(zone_device_identifier(entry.entry_id, zone[CONF_ZONE_NAME]))

    # Removable only if NONE of the device identifiers match a live zone/hub.
    return not any(identifier in valid_identifiers for identifier in device.identifiers)
