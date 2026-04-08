"""Sensor platform for the NeverDry integration.

Provides:
- ETSensor: instantaneous evapotranspiration estimate [mm/h]
- DrynessIndexSensor: reference soil water deficit [mm] (Kc=1.0)
- IrrigationZoneSensor: per-zone deficit, volume, and duration (N instances)
  Each zone tracks its own deficit scaled by a crop coefficient Kc
  that varies seasonally based on the plant family.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType

from .controller import IrrigationController
from .const import (
    CONF_ALPHA,
    CONF_INTER_ZONE_DELAY,
    CONF_D_MAX,
    CONF_FIELD_CAPACITY,
    CONF_RAIN_SENSOR,
    CONF_ROOT_DEPTH,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_ZONES,
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_KC,
    CONF_ZONE_NAME,
    CONF_ZONE_PLANT_FAMILY,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
    DEFAULT_ALPHA,
    DEFAULT_D_MAX,
    DEFAULT_EFFICIENCY,
    DEFAULT_FIELD_CAPACITY,
    DEFAULT_INTER_ZONE_DELAY,
    DEFAULT_KC,
    DEFAULT_ROOT_DEPTH,
    DEFAULT_T_BASE,
    DEFAULT_THRESHOLD,
    DOMAIN,
    KC_ANCHOR_DAYS,
    PLANT_FAMILIES,
    SYSTEM_TYPES,
)

_LOGGER = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
#  Kc computation
# ══════════════════════════════════════════════════════════


def compute_kc(
    day_of_year: int,
    plant_family: str | None,
    manual_kc: float | None,
    latitude: float = 45.0,
) -> float:
    """Compute the crop coefficient for a given day of year.

    Priority: manual_kc > plant_family seasonal profile > DEFAULT_KC (1.0).

    The seasonal profile uses 4 anchor points (winter, spring, summer,
    autumn) with linear interpolation.  For southern hemisphere
    (latitude < 0) the day is shifted by 182 days.
    """
    if manual_kc is not None:
        return manual_kc

    if plant_family is None or plant_family not in PLANT_FAMILIES:
        return DEFAULT_KC

    kc_values = PLANT_FAMILIES[plant_family]["kc_seasonal"]

    # Southern hemisphere: shift by half a year
    doy = day_of_year
    if latitude < 0:
        doy = ((doy + 182 - 1) % 365) + 1  # keep in 1-365 range

    anchors = list(KC_ANCHOR_DAYS)  # (15, 105, 196, 288)
    values = list(kc_values)

    # Find surrounding anchors and interpolate
    for i in range(4):
        a1 = anchors[i]
        a2 = anchors[(i + 1) % 4]
        v1 = values[i]
        v2 = values[(i + 1) % 4]

        if a2 > a1:
            # Normal segment (e.g., winter→spring, spring→summer, summer→autumn)
            if a1 <= doy < a2:
                frac = (doy - a1) / (a2 - a1)
                return round(v1 + frac * (v2 - v1), 4)
        else:
            # Wrap-around segment (autumn→winter, crossing year boundary)
            if doy >= a1 or doy < a2:
                span = (365 - a1) + a2
                dist = (doy - a1) % 365
                frac = dist / span
                return round(v1 + frac * (v2 - v1), 4)

    return DEFAULT_KC  # fallback


# ══════════════════════════════════════════════════════════
#  Entity creation helpers
# ══════════════════════════════════════════════════════════


def _create_entities(
    hass: HomeAssistant, config: dict
) -> tuple[list[SensorEntity], DrynessIndexSensor, list[IrrigationZoneSensor]]:
    """Create sensor entities from a config dict (shared by YAML and UI)."""
    et_sensor = ETSensor(hass, config)
    di_sensor = DrynessIndexSensor(hass, config)
    entities: list[SensorEntity] = [et_sensor, di_sensor]

    zone_sensors: list[IrrigationZoneSensor] = []
    for zone_conf in config.get(CONF_ZONES, []):
        zone_sensor = IrrigationZoneSensor(hass, zone_conf, di_sensor)
        zone_sensors.append(zone_sensor)
        entities.append(zone_sensor)

    return entities, di_sensor, zone_sensors


def _setup_controller(
    hass: HomeAssistant,
    config: dict,
    di_sensor: DrynessIndexSensor,
    zone_sensors: list[IrrigationZoneSensor],
) -> IrrigationController:
    """Create the irrigation controller and register all services."""
    inter_zone_delay = config.get(CONF_INTER_ZONE_DELAY, DEFAULT_INTER_ZONE_DELAY)
    controller = IrrigationController(
        hass, di_sensor, zone_sensors, inter_zone_delay
    )
    controller.register_services()
    return controller


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities,
    discovery_info=None,
) -> None:
    """Set up the NeverDry sensors from YAML configuration."""
    entities, di_sensor, zone_sensors = _create_entities(hass, config)
    async_add_entities(entities, True)
    _setup_controller(hass, config, di_sensor, zone_sensors)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the NeverDry sensors from a config entry (UI)."""
    config = dict(entry.data)
    entities, di_sensor, zone_sensors = _create_entities(hass, config)
    async_add_entities(entities, True)
    _setup_controller(hass, config, di_sensor, zone_sensors)


# ══════════════════════════════════════════════════════════
#  ETSensor
# ══════════════════════════════════════════════════════════


class ETSensor(SensorEntity):
    """Instantaneous evapotranspiration estimate [mm/h].

    Uses a simplified linear model: ET_h = max(0, alpha * (T - T_base) / 24)
    """

    _attr_name = "ET Hourly Estimate"
    _attr_unique_id = "et_hourly_estimate"
    _attr_native_unit_of_measurement = "mm/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sun-thermometer"

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        self._hass = hass
        self._temp_sensor = config[CONF_TEMP_SENSOR]
        self._alpha = config.get(CONF_ALPHA, DEFAULT_ALPHA)
        self._t_base = config.get(CONF_T_BASE, DEFAULT_T_BASE)
        self._value = 0.0

    async def async_added_to_hass(self) -> None:
        """Register state change listener on temperature sensor."""
        async_track_state_change_event(
            self._hass, [self._temp_sensor], self._on_temp_change
        )

    @callback
    def _on_temp_change(self, event) -> None:
        """Update ET estimate when temperature changes."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        try:
            t = float(new_state.state)
            self._value = max(0.0, self._alpha * (t - self._t_base) / 24)
        except (ValueError, TypeError):
            pass
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._value, 4)


# ══════════════════════════════════════════════════════════
#  DrynessIndexSensor (reference, Kc=1.0)
# ══════════════════════════════════════════════════════════


class DrynessIndexSensor(SensorEntity, RestoreEntity):
    """Reference soil water deficit [mm] at Kc=1.0.

    Integrates ET - precipitation in real-time using forward Euler
    with variable time steps (event-driven).  Zone sensors register
    as listeners to receive (dt_h, et_h, rain) broadcasts and track
    their own per-zone deficit scaled by Kc.
    """

    _attr_name = "NeverDry"
    _attr_unique_id = "never_dry"
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent-alert"

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        self._hass = hass
        self._temp_sensor = config[CONF_TEMP_SENSOR]
        self._rain_sensor = config[CONF_RAIN_SENSOR]
        self._alpha = config.get(CONF_ALPHA, DEFAULT_ALPHA)
        self._t_base = config.get(CONF_T_BASE, DEFAULT_T_BASE)
        self._d_max = config.get(CONF_D_MAX, DEFAULT_D_MAX)
        self._vwc_sensor = config.get(CONF_VWC_SENSOR)
        self._field_cap = config.get(CONF_FIELD_CAPACITY, DEFAULT_FIELD_CAPACITY)
        self._root_depth = config.get(CONF_ROOT_DEPTH, DEFAULT_ROOT_DEPTH)
        self._deficit = 0.0
        self._last_update = datetime.now()
        self._zone_listeners: list[Callable] = []

    def register_zone_listener(self, listener: Callable) -> None:
        """Register a zone sensor callback for ET/rain broadcasts."""
        self._zone_listeners.append(listener)

    @property
    def deficit(self) -> float:
        """Current reference deficit in mm (Kc=1.0)."""
        return self._deficit

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register listeners."""
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable"):
            try:
                self._deficit = float(last.state)
            except (ValueError, TypeError):
                pass

        tracked = [self._temp_sensor, self._rain_sensor]
        if self._vwc_sensor:
            tracked.append(self._vwc_sensor)

        async_track_state_change_event(
            self._hass, tracked, self._on_sensor_change
        )

    @callback
    def _on_sensor_change(self, event) -> None:
        """Recalculate deficit on any tracked sensor change."""
        now = datetime.now()
        dt_h = (now - self._last_update).total_seconds() / 3600.0
        self._last_update = now

        if self._vwc_sensor:
            self._update_from_vwc()
            # In VWC mode, broadcast zeros — zones use VWC deficit * Kc
            self._broadcast_to_zones(0.0, 0.0, 0.0)
        else:
            self._update_from_model(dt_h)
            # Broadcast raw ET and rain to zone listeners
            try:
                t = float(self._hass.states.get(self._temp_sensor).state)
                rain = float(self._hass.states.get(self._rain_sensor).state)
                et_h = max(0.0, self._alpha * (t - self._t_base) / 24)
            except (TypeError, ValueError, AttributeError):
                et_h = 0.0
                rain = 0.0
            self._broadcast_to_zones(dt_h, et_h, rain)

        self.async_write_ha_state()

    def _broadcast_to_zones(
        self, dt_h: float, et_h: float, rain: float
    ) -> None:
        """Notify all registered zone sensors with ET/rain data."""
        for listener in self._zone_listeners:
            listener(dt_h, et_h, rain)

    def _update_from_vwc(self) -> None:
        """Update deficit from direct VWC measurement."""
        vwc_state = self._hass.states.get(self._vwc_sensor)
        if vwc_state is None:
            return
        try:
            vwc = float(vwc_state.state)
            self._deficit = max(
                0.0, (self._field_cap - vwc) * self._root_depth * 1000
            )
        except (ValueError, TypeError):
            pass

    def _update_from_model(self, dt_h: float) -> None:
        """Update deficit from ET model and precipitation."""
        try:
            t = float(self._hass.states.get(self._temp_sensor).state)
            rain = float(self._hass.states.get(self._rain_sensor).state)
        except (TypeError, ValueError, AttributeError):
            return

        et_dt = max(0.0, self._alpha * (t - self._t_base) / 24) * dt_h
        self._deficit = max(
            0.0, min(self._deficit + et_dt - rain, self._d_max)
        )

    def reset(self) -> None:
        """Reset deficit to zero (called after irrigation)."""
        self._deficit = 0.0
        self._last_update = datetime.now()

    @property
    def native_value(self) -> float:
        return round(self._deficit, 2)


# ══════════════════════════════════════════════════════════
#  IrrigationZoneSensor (per-zone deficit with Kc)
# ══════════════════════════════════════════════════════════


class IrrigationZoneSensor(SensorEntity, RestoreEntity):
    """Per-zone irrigation volume and duration.

    Each zone tracks its own deficit:
        D_zone(t) = clamp(D_zone(t-1) + ET_h * Kc(doy) * Δt - rain, 0, D_max)

    The crop coefficient Kc varies seasonally based on the plant family
    and is auto-adjusted for hemisphere via hass.config.latitude.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "L"
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        zone_config: dict,
        dryness_sensor: DrynessIndexSensor,
    ) -> None:
        self._hass = hass
        self._dryness = dryness_sensor
        self._zone_name = zone_config[CONF_ZONE_NAME]
        self._valve = zone_config.get(CONF_ZONE_VALVE)
        self._area = zone_config.get(CONF_ZONE_AREA, 0.0)
        self._system_type = zone_config.get(CONF_ZONE_SYSTEM_TYPE)
        self._flow_rate = zone_config.get(CONF_ZONE_FLOW_RATE, 0.0)
        self._threshold = zone_config.get(CONF_ZONE_THRESHOLD, DEFAULT_THRESHOLD)
        self._irrigating = False

        # Kc: manual override > plant family seasonal profile > 1.0
        self._plant_family = zone_config.get(CONF_ZONE_PLANT_FAMILY)
        self._manual_kc = zone_config.get(CONF_ZONE_KC)

        # Per-zone deficit
        self._zone_deficit = 0.0
        self._d_max = dryness_sensor._d_max

        # Efficiency: explicit value > system_type default > global default
        if CONF_ZONE_EFFICIENCY in zone_config:
            self._efficiency = zone_config[CONF_ZONE_EFFICIENCY]
        elif self._system_type and self._system_type in SYSTEM_TYPES:
            self._efficiency = SYSTEM_TYPES[self._system_type]["default_efficiency"]
        else:
            self._efficiency = DEFAULT_EFFICIENCY

        slug = self._zone_name.lower().replace(" ", "_")
        self._attr_name = f"Irrigation {self._zone_name}"
        self._attr_unique_id = f"irrigation_zone_{slug}"

        # Register as listener on the dryness sensor
        dryness_sensor.register_zone_listener(self._on_et_update)

    async def async_added_to_hass(self) -> None:
        """Restore zone deficit from previous state."""
        last = await self.async_get_last_state()
        if last and last.attributes:
            try:
                self._zone_deficit = float(
                    last.attributes.get("deficit_mm", 0.0)
                )
            except (ValueError, TypeError):
                pass

    def _get_latitude(self) -> float:
        """Get latitude from HA config, default to 45.0 (northern)."""
        try:
            return self._hass.config.latitude
        except AttributeError:
            return 45.0

    def _get_current_kc(self) -> float:
        """Compute the current Kc for this zone."""
        doy = datetime.now().timetuple().tm_yday
        return compute_kc(doy, self._plant_family, self._manual_kc,
                          self._get_latitude())

    def _on_et_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update zone-specific deficit when base sensor broadcasts."""
        # In VWC mode (dt_h==0, et_h==0, rain==0), use base deficit * Kc
        if dt_h == 0.0 and et_h == 0.0 and rain == 0.0:
            kc = self._get_current_kc()
            self._zone_deficit = self._dryness.deficit * kc
        else:
            kc = self._get_current_kc()
            self._zone_deficit = max(
                0.0,
                min(self._zone_deficit + et_h * kc * dt_h - rain, self._d_max),
            )
        self.async_write_ha_state()

    @property
    def zone_name(self) -> str:
        """Zone display name."""
        return self._zone_name

    @property
    def valve(self) -> str | None:
        """Entity ID of the valve switch."""
        return self._valve

    @property
    def is_irrigating(self) -> bool:
        """True if this zone is currently being irrigated."""
        return self._irrigating

    def set_irrigating(self, state: bool) -> None:
        """Set the irrigating state (called by controller)."""
        self._irrigating = state

    def reset_deficit(self) -> None:
        """Reset this zone's deficit to zero (called after irrigation)."""
        self._zone_deficit = 0.0

    @property
    def volume_liters(self) -> float:
        """Volume to irrigate this zone [L]."""
        if self._efficiency <= 0:
            return 0.0
        return self._zone_deficit * self._area / self._efficiency

    @property
    def duration_s(self) -> int:
        """Irrigation duration for this zone [s]."""
        if self._flow_rate <= 0:
            return 0
        return round(self.volume_liters / self._flow_rate * 60)

    @property
    def native_value(self) -> float:
        return round(self.volume_liters, 1)

    @property
    def extra_state_attributes(self) -> dict:
        kc = self._get_current_kc()
        return {
            "zone_name": self._zone_name,
            "valve": self._valve,
            "system_type": self._system_type,
            "plant_family": self._plant_family,
            "kc": round(kc, 3),
            "kc_override": self._manual_kc,
            "area_m2": self._area,
            "efficiency": self._efficiency,
            "flow_rate_lpm": self._flow_rate,
            "threshold_mm": self._threshold,
            "volume_liters": round(self.volume_liters, 1),
            "duration_s": self.duration_s,
            "deficit_mm": round(self._zone_deficit, 2),
            "irrigating": self._irrigating,
        }
