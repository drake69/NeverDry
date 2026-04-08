"""Config flow for the NeverDry integration.

Provides a multi-step UI setup:
  1. Select temperature and rain sensors, ET model parameters
  2. Add irrigation zones (repeatable)
  3. Options flow to edit parameters and add/remove zones later
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALPHA,
    CONF_D_MAX,
    CONF_FIELD_CAPACITY,
    CONF_RAIN_SENSOR,
    CONF_RAIN_SENSOR_TYPE,
    CONF_ROOT_DEPTH,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_ZONES,
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_KC,
    CONF_ZONE_PLANT_FAMILY,
    CONF_ZONE_VALVE,
    DEFAULT_ALPHA,
    DEFAULT_D_MAX,
    DEFAULT_EFFICIENCY,
    DEFAULT_FIELD_CAPACITY,
    DEFAULT_ROOT_DEPTH,
    DEFAULT_T_BASE,
    DEFAULT_THRESHOLD,
    DEFAULT_RAIN_SENSOR_TYPE,
    DOMAIN,
    PLANT_FAMILIES,
    RAIN_TYPE_DAILY_TOTAL,
    RAIN_TYPE_EVENT,
    SYSTEM_TYPE_DRIP,
    SYSTEM_TYPE_MANUAL,
    SYSTEM_TYPE_MICRO_SPRINKLER,
    SYSTEM_TYPE_SPRINKLER,
)

_LOGGER = logging.getLogger(__name__)

STEP_SENSORS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
        ),
        vol.Required(CONF_RAIN_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(
            CONF_RAIN_SENSOR_TYPE, default=DEFAULT_RAIN_SENSOR_TYPE
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=RAIN_TYPE_EVENT,
                        label="Event-based (mm per event — tipping bucket)",
                    ),
                    selector.SelectOptionDict(
                        value=RAIN_TYPE_DAILY_TOTAL,
                        label="Daily total (cumulative mm since midnight)",
                    ),
                ],
                mode="dropdown",
            )
        ),
        vol.Optional(CONF_ALPHA, default=DEFAULT_ALPHA): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.05, max=1.0, step=0.01, mode="box",
                unit_of_measurement="mm/°C/day",
            )
        ),
        vol.Optional(CONF_T_BASE, default=DEFAULT_T_BASE): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-5.0, max=20.0, step=0.5, mode="box",
                unit_of_measurement="°C",
            )
        ),
        vol.Optional(CONF_D_MAX, default=DEFAULT_D_MAX): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10.0, max=500.0, step=10.0, mode="box",
                unit_of_measurement="mm",
            )
        ),
        vol.Optional(CONF_VWC_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
    }
)

STEP_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZONE_NAME): selector.TextSelector(),
        vol.Optional(CONF_ZONE_VALVE): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="switch")
        ),
        vol.Required(CONF_ZONE_AREA): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.1, max=10000.0, step=0.1, mode="box",
                unit_of_measurement="m²",
            )
        ),
        vol.Required(CONF_ZONE_SYSTEM_TYPE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=SYSTEM_TYPE_DRIP, label="Drip irrigation (η=0.92)"),
                    selector.SelectOptionDict(value=SYSTEM_TYPE_MICRO_SPRINKLER, label="Micro-sprinklers (η=0.80)"),
                    selector.SelectOptionDict(value=SYSTEM_TYPE_SPRINKLER, label="Pop-up sprinklers (η=0.68)"),
                    selector.SelectOptionDict(value=SYSTEM_TYPE_MANUAL, label="Manual / hose (η=0.55)"),
                ],
                mode="dropdown",
            )
        ),
        vol.Optional(CONF_ZONE_EFFICIENCY): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.1, max=1.0, step=0.05, mode="slider",
            )
        ),
        vol.Optional(CONF_ZONE_PLANT_FAMILY): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=key, label=data["label"])
                    for key, data in PLANT_FAMILIES.items()
                ],
                mode="dropdown",
            )
        ),
        vol.Optional(CONF_ZONE_KC): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.1, max=2.0, step=0.05, mode="box",
            )
        ),
        vol.Required(CONF_ZONE_FLOW_RATE): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.1, max=200.0, step=0.1, mode="box",
                unit_of_measurement="L/min",
            )
        ),
        vol.Optional(
            CONF_ZONE_THRESHOLD, default=DEFAULT_THRESHOLD
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1.0, max=100.0, step=1.0, mode="box",
                unit_of_measurement="mm",
            )
        ),
    }
)


class NeverDryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NeverDry."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._zones: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Select sensors and ET model parameters."""
        if user_input is not None:
            self._data = user_input
            return await self.async_step_zone()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SENSORS_SCHEMA,
        )

    async def async_step_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Add an irrigation zone."""
        if user_input is not None:
            self._zones.append(user_input)
            return await self.async_step_add_another()

        return self.async_show_form(
            step_id="zone",
            data_schema=STEP_ZONE_SCHEMA,
            description_placeholders={
                "zone_count": str(len(self._zones)),
            },
        )

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3: Ask whether to add another zone or finish."""
        if user_input is not None:
            if user_input.get("add_another"):
                return await self.async_step_zone()
            return self._create_entry()

        return self.async_show_form(
            step_id="add_another",
            data_schema=vol.Schema(
                {
                    vol.Required("add_another", default=False): bool,
                }
            ),
            description_placeholders={
                "zone_count": str(len(self._zones)),
                "zone_names": ", ".join(z[CONF_ZONE_NAME] for z in self._zones),
            },
        )

    def _create_entry(self) -> config_entries.ConfigFlowResult:
        """Create the config entry with all collected data."""
        self._data[CONF_ZONES] = self._zones
        title = f"NeverDry ({len(self._zones)} zone{'s' if len(self._zones) != 1 else ''})"
        return self.async_create_entry(title=title, data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> NeverDryOptionsFlow:
        """Get the options flow handler."""
        return NeverDryOptionsFlow(config_entry)


class NeverDryOptionsFlow(config_entries.OptionsFlow):
    """Handle options for NeverDry (edit after setup)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show menu: edit model params or manage zones."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["model_params", "add_zone"],
        )

    async def async_step_model_params(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit ET model parameters."""
        if user_input is not None:
            new_data = {**self._config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return self.async_create_entry(data={})

        current = self._config_entry.data
        return self.async_show_form(
            step_id="model_params",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ALPHA,
                        default=current.get(CONF_ALPHA, DEFAULT_ALPHA),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.05, max=1.0, step=0.01, mode="box",
                            unit_of_measurement="mm/°C/day",
                        )
                    ),
                    vol.Optional(
                        CONF_T_BASE,
                        default=current.get(CONF_T_BASE, DEFAULT_T_BASE),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=-5.0, max=20.0, step=0.5, mode="box",
                            unit_of_measurement="°C",
                        )
                    ),
                    vol.Optional(
                        CONF_D_MAX,
                        default=current.get(CONF_D_MAX, DEFAULT_D_MAX),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=10.0, max=500.0, step=10.0, mode="box",
                            unit_of_measurement="mm",
                        )
                    ),
                }
            ),
        )

    async def async_step_add_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add a new irrigation zone."""
        if user_input is not None:
            new_data = dict(self._config_entry.data)
            zones = list(new_data.get(CONF_ZONES, []))
            zones.append(user_input)
            new_data[CONF_ZONES] = zones
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="add_zone",
            data_schema=STEP_ZONE_SCHEMA,
        )
