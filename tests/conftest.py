"""Shared test fixtures for NeverDry tests.

Since we test the core logic without a full Home Assistant runtime,
we mock the HA dependencies and expose the sensor classes directly.
"""

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Stub out homeassistant imports before loading our code ────────
def _create_ha_stubs():
    """Create minimal stubs for homeassistant modules."""

    # homeassistant.components.sensor
    sensor_mod = ModuleType("homeassistant.components.sensor")
    sensor_mod.SensorEntity = type(
        "SensorEntity",
        (),
        {
            "async_write_ha_state": lambda self: None,
        },
    )

    class SensorStateClass:
        MEASUREMENT = "measurement"

    sensor_mod.SensorStateClass = SensorStateClass

    # homeassistant.core
    core_mod = ModuleType("homeassistant.core")
    core_mod.HomeAssistant = MagicMock
    core_mod.ServiceCall = MagicMock
    core_mod.callback = lambda fn: fn

    # homeassistant.helpers.event
    event_mod = ModuleType("homeassistant.helpers.event")
    event_mod.async_track_state_change_event = MagicMock()
    event_mod.async_track_time_interval = MagicMock()

    # homeassistant.helpers.restore_state
    restore_mod = ModuleType("homeassistant.helpers.restore_state")
    restore_mod.RestoreEntity = type(
        "RestoreEntity",
        (),
        {
            "async_get_last_state": lambda self: None,
        },
    )

    # homeassistant.helpers.typing
    typing_mod = ModuleType("homeassistant.helpers.typing")
    typing_mod.ConfigType = dict

    # homeassistant.config_entries
    config_entries_mod = ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = MagicMock

    # homeassistant.helpers.entity_platform
    entity_platform_mod = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform_mod.AddEntitiesCallback = MagicMock

    # homeassistant.helpers.config_validation
    cv_mod = ModuleType("homeassistant.helpers.config_validation")
    cv_mod.config_entry_only_config_schema = lambda domain: {}

    # Register all stubs
    helpers_mod = ModuleType("homeassistant.helpers")
    helpers_mod.config_validation = cv_mod
    mods = {
        "homeassistant": ModuleType("homeassistant"),
        "homeassistant.components": ModuleType("homeassistant.components"),
        "homeassistant.components.sensor": sensor_mod,
        "homeassistant.config_entries": config_entries_mod,
        "homeassistant.core": core_mod,
        "homeassistant.helpers": helpers_mod,
        "homeassistant.helpers.config_validation": cv_mod,
        "homeassistant.helpers.entity_platform": entity_platform_mod,
        "homeassistant.helpers.event": event_mod,
        "homeassistant.helpers.restore_state": restore_mod,
        "homeassistant.helpers.typing": typing_mod,
    }
    sys.modules.update(mods)


_create_ha_stubs()

# Now we can safely import our code
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "custom_components"))
from never_dry.const import (  # noqa: E402
    CONF_ALPHA,
    CONF_RAIN_SENSOR,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
)
from never_dry.controller import IrrigationController  # noqa: E402
from never_dry.sensor import (  # noqa: E402
    DrynessIndexSensor,
    ETSensor,
    IrrigationZoneSensor,
)


def _make_state(value):
    """Create a mock HA state object."""
    state = MagicMock()
    state.state = str(value)
    return state


@pytest.fixture
def base_config():
    """Minimal valid configuration (no zones)."""
    return {
        CONF_TEMP_SENSOR: "sensor.temperature",
        CONF_RAIN_SENSOR: "sensor.rain",
        CONF_ALPHA: 0.22,
        CONF_T_BASE: 9.0,
    }


@pytest.fixture
def hass_mock():
    """Mock HomeAssistant instance with async services."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.services.async_register = MagicMock()
    # async_create_task runs the coroutine directly for testing
    hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
    # Mock HA config with latitude (northern hemisphere)
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    return hass


@pytest.fixture
def et_sensor(hass_mock, base_config):
    """Create an ETSensor instance."""
    return ETSensor(hass_mock, base_config)


@pytest.fixture
def di_sensor(hass_mock, base_config):
    """Create a DrynessIndexSensor instance."""
    return DrynessIndexSensor(hass_mock, base_config)


@pytest.fixture
def zone_orto(hass_mock, di_sensor):
    """Create an IrrigationZoneSensor for 'Orto'."""
    zone_config = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
    }
    return IrrigationZoneSensor(hass_mock, zone_config, di_sensor)


@pytest.fixture
def zone_prato(hass_mock, di_sensor):
    """Create an IrrigationZoneSensor for 'Prato'."""
    zone_config = {
        CONF_ZONE_NAME: "Prato",
        CONF_ZONE_VALVE: "switch.valve_prato",
        CONF_ZONE_AREA: 50.0,
        CONF_ZONE_EFFICIENCY: 0.70,
        CONF_ZONE_FLOW_RATE: 15.0,
    }
    return IrrigationZoneSensor(hass_mock, zone_config, di_sensor)


@pytest.fixture
def controller(hass_mock, di_sensor, zone_orto, zone_prato):
    """Create an IrrigationController with two zones."""
    return IrrigationController(hass_mock, di_sensor, [zone_orto, zone_prato], inter_zone_delay=0)


@pytest.fixture
def make_state():
    """Factory for mock state objects."""
    return _make_state


@pytest.fixture
def make_event():
    """Factory for mock state change events."""

    def _make(new_value):
        event = MagicMock()
        event.data = {"new_state": _make_state(new_value)}
        return event

    return _make
