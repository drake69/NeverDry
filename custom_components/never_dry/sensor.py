"""Sensor platform for the NeverDry integration.

Provides:
- ETSensor: instantaneous evapotranspiration estimate [mm/h]
- DrynessIndexSensor: reference soil water deficit [mm] (Kc=1.0)
- IrrigationZoneSensor: per-zone deficit, volume, and duration (N instances)
  Each zone tracks its own deficit scaled by a crop coefficient Kc
  that varies seasonally based on the plant family.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ALPHA,
    CONF_BACKFILL_DAYS,
    CONF_D_MAX,
    CONF_FIELD_CAPACITY,
    CONF_INTER_ZONE_DELAY,
    CONF_RAIN_SENSOR,
    CONF_RAIN_SENSOR_TYPE,
    CONF_ROOT_DEPTH,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_BATTERY_SENSOR,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_METER_SENSOR,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_KC,
    CONF_ZONE_NAME,
    CONF_ZONE_PLANT_FAMILY,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
    CONF_ZONE_VOLUME_ENTITY,
    CONF_ZONES,
    DEFAULT_ALPHA,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_D_MAX,
    DEFAULT_DELIVERY_MODE,
    DEFAULT_DELIVERY_TIMEOUT_S,
    DEFAULT_EFFICIENCY,
    DEFAULT_FIELD_CAPACITY,
    DEFAULT_INTER_ZONE_DELAY,
    DEFAULT_KC,
    DEFAULT_RAIN_SENSOR_TYPE,
    DEFAULT_ROOT_DEPTH,
    DEFAULT_T_BASE,
    DEFAULT_THRESHOLD,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DOMAIN,
    KC_ANCHOR_DAYS,
    PLANT_FAMILIES,
    RAIN_TYPE_EVENT,
    SYSTEM_TYPES,
)
from .controller import IrrigationController

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


def _hub_device_info(entry_id: str) -> DeviceInfo:
    """Device info for the main NeverDry hub (ET + deficit sensors)."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="NeverDry",
        manufacturer="NeverDry",
        model="Smart Watering Controller",
    )


def _zone_device_info(entry_id: str, zone_name: str) -> DeviceInfo:
    """Device info for a zone (sensor + buttons grouped together)."""
    slug = zone_name.lower().replace(" ", "_")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{slug}")},
        name=f"NeverDry {zone_name}",
        manufacturer="NeverDry",
        model="Irrigation Zone",
        via_device=(DOMAIN, entry_id),
    )


def _create_entities(
    hass: HomeAssistant, config: dict, entry_id: str = "yaml"
) -> tuple[list[SensorEntity], DrynessIndexSensor, list[IrrigationZoneSensor]]:
    """Create sensor entities from a config dict (shared by YAML and UI)."""
    hub_device = _hub_device_info(entry_id)
    et_sensor = ETSensor(hass, config, hub_device)
    di_sensor = DrynessIndexSensor(hass, config, hub_device)
    entities: list[SensorEntity] = [et_sensor, di_sensor]

    zone_sensors: list[IrrigationZoneSensor] = []
    for zone_conf in config.get(CONF_ZONES, []):
        zone_device = _zone_device_info(entry_id, zone_conf[CONF_ZONE_NAME])
        zone_sensor = IrrigationZoneSensor(hass, zone_conf, di_sensor, zone_device)
        zone_sensors.append(zone_sensor)
        entities.append(zone_sensor)
        entities.append(ZoneDeficitSensor(zone_sensor, zone_device))

    return entities, di_sensor, zone_sensors


def _setup_controller(
    hass: HomeAssistant,
    config: dict,
    di_sensor: DrynessIndexSensor,
    zone_sensors: list[IrrigationZoneSensor],
) -> IrrigationController:
    """Create the irrigation controller and register all services."""
    inter_zone_delay = config.get(CONF_INTER_ZONE_DELAY, DEFAULT_INTER_ZONE_DELAY)
    controller = IrrigationController(hass, di_sensor, zone_sensors, inter_zone_delay)
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
    entities, di_sensor, zone_sensors = _create_entities(hass, config, entry.entry_id)
    async_add_entities(entities, True)
    _setup_controller(hass, config, di_sensor, zone_sensors)


# ══════════════════════════════════════════════════════════
#  ETSensor
# ══════════════════════════════════════════════════════════


class ETSensor(SensorEntity):
    """Instantaneous evapotranspiration estimate [mm/h].

    Uses a simplified linear model: ET_h = max(0, alpha * (T - T_base) / 24)
    """

    _attr_has_entity_name = True
    _attr_name = "ET Hourly Estimate"
    _attr_unique_id = "et_hourly_estimate"
    _attr_native_unit_of_measurement = "mm/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sun-thermometer"

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._temp_sensor = config[CONF_TEMP_SENSOR]
        self._alpha = config.get(CONF_ALPHA, DEFAULT_ALPHA)
        self._t_base = config.get(CONF_T_BASE, DEFAULT_T_BASE)
        self._value = 0.0
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register state change listener on temperature sensor."""
        async_track_state_change_event(self._hass, [self._temp_sensor], self._on_temp_change)

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

    _attr_has_entity_name = True
    _attr_name = "Dryness Index"
    _attr_unique_id = "never_dry"
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent-alert"

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._temp_sensor = config[CONF_TEMP_SENSOR]
        self._rain_sensor = config[CONF_RAIN_SENSOR]
        self._alpha = config.get(CONF_ALPHA, DEFAULT_ALPHA)
        self._t_base = config.get(CONF_T_BASE, DEFAULT_T_BASE)
        self._d_max = config.get(CONF_D_MAX, DEFAULT_D_MAX)
        self._vwc_sensor = config.get(CONF_VWC_SENSOR)
        self._field_cap = config.get(CONF_FIELD_CAPACITY, DEFAULT_FIELD_CAPACITY)
        self._root_depth = config.get(CONF_ROOT_DEPTH, DEFAULT_ROOT_DEPTH)
        self._rain_type = config.get(CONF_RAIN_SENSOR_TYPE, DEFAULT_RAIN_SENSOR_TYPE)
        self._backfill_days = config.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        self._deficit = 0.0
        self._last_rain = 0.0  # tracks last rain reading for delta computation
        self._last_update = datetime.now()
        self._zone_listeners: list[Callable] = []
        if device_info:
            self._attr_device_info = device_info

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
        restored = False
        if last and last.state not in ("unknown", "unavailable"):
            with contextlib.suppress(ValueError, TypeError):
                self._deficit = float(last.state)
                restored = True

        if not restored:
            await self._backfill_from_recorder()

        tracked = [self._temp_sensor, self._rain_sensor]
        if self._vwc_sensor:
            tracked.append(self._vwc_sensor)

        async_track_state_change_event(self._hass, tracked, self._on_sensor_change)

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
            # Compute rain delta BEFORE _update_from_model (which also calls it)
            # We need to capture it for broadcasting to zones.
            rain_delta = self._compute_rain_delta()
            try:
                t = float(self._hass.states.get(self._temp_sensor).state)
                et_h = max(0.0, self._alpha * (t - self._t_base) / 24)
            except (TypeError, ValueError, AttributeError):
                et_h = 0.0
                rain_delta = 0.0

            # Update reference deficit
            et_dt = et_h * dt_h
            self._deficit = max(0.0, min(self._deficit + et_dt - rain_delta, self._d_max))

            # Broadcast delta to zone listeners
            self._broadcast_to_zones(dt_h, et_h, rain_delta)

        self.async_write_ha_state()

    def _broadcast_to_zones(self, dt_h: float, et_h: float, rain: float) -> None:
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
            self._deficit = max(0.0, (self._field_cap - vwc) * self._root_depth * 1000)
        except (ValueError, TypeError):
            pass

    def _compute_rain_delta(self) -> float:
        """Compute rain increment since last reading.

        For 'event' type: the sensor value IS the delta (mm per event).
        For 'daily_total' type: compute delta from last reading, handling
        midnight rollover (rain_now < last_rain → new accumulation).
        """
        try:
            rain_now = float(self._hass.states.get(self._rain_sensor).state)
        except (TypeError, ValueError, AttributeError):
            return 0.0

        if self._rain_type == RAIN_TYPE_EVENT:
            # Value IS the delta — but only count it once per state change.
            # We track last_rain to detect repeated reads of the same value.
            if rain_now == self._last_rain:
                return 0.0  # no new event
            self._last_rain = rain_now
            return max(0.0, rain_now)

        # daily_total: compute delta
        rain_delta = rain_now - self._last_rain
        if rain_delta < 0:
            # Sensor reset (midnight rollover) — treat new value as fresh
            rain_delta = rain_now
        self._last_rain = rain_now
        return max(0.0, rain_delta)

    def _update_from_model(self, dt_h: float) -> None:
        """Update deficit from ET model and precipitation (standalone).

        Used by unit tests.  The event-driven path in _on_sensor_change
        computes the same logic inline to capture rain_delta for broadcast.
        """
        try:
            t = float(self._hass.states.get(self._temp_sensor).state)
        except (TypeError, ValueError, AttributeError):
            return

        rain_delta = self._compute_rain_delta()
        et_dt = max(0.0, self._alpha * (t - self._t_base) / 24) * dt_h
        self._deficit = max(0.0, min(self._deficit + et_dt - rain_delta, self._d_max))

    async def _backfill_from_recorder(self) -> None:
        """Replay historical T/rain from HA recorder to bootstrap deficit.

        Called only on first-time setup (no RestoreEntity state).
        Fails gracefully if recorder is not available.
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import (
                get_significant_states,
            )
        except ImportError:
            _LOGGER.warning("Recorder component not available; starting deficit at 0.0")
            return

        instance = get_instance(self._hass)
        if instance is None:
            _LOGGER.warning("Recorder instance not available; starting deficit at 0.0")
            return

        now = datetime.utcnow()
        start_time = now - timedelta(days=self._backfill_days)
        entity_ids = [self._temp_sensor, self._rain_sensor]

        try:
            history = await instance.async_add_executor_job(
                get_significant_states,
                self._hass,
                start_time,
                now,
                entity_ids,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to query recorder for backfill; starting deficit at 0.0",
                exc_info=True,
            )
            return

        if not history:
            _LOGGER.info("No recorder history found for backfill")
            return

        temp_states = history.get(self._temp_sensor, [])
        rain_states = history.get(self._rain_sensor, [])

        if not temp_states:
            _LOGGER.info("No temperature history for backfill")
            return

        deficit = self._replay_water_balance(temp_states, rain_states)
        self._deficit = deficit
        self.async_write_ha_state()

        _LOGGER.info(
            "Backfilled deficit from recorder history: %.2f mm (%d temp states, %d rain states)",
            deficit,
            len(temp_states),
            len(rain_states),
        )

    def _replay_water_balance(
        self,
        temp_states: list,
        rain_states: list,
    ) -> float:
        """Replay the ET water-balance loop over historical states.

        Returns the final deficit value.
        """
        events: list[tuple[datetime, str, float]] = []

        for s in temp_states:
            if s.state in ("unknown", "unavailable"):
                continue
            try:
                events.append((s.last_changed, "temp", float(s.state)))
            except (ValueError, TypeError):
                continue

        for s in rain_states:
            if s.state in ("unknown", "unavailable"):
                continue
            try:
                events.append((s.last_changed, "rain", float(s.state)))
            except (ValueError, TypeError):
                continue

        events.sort(key=lambda e: e[0])

        if not events:
            return 0.0

        deficit = 0.0
        last_temp: float | None = None
        last_rain = 0.0
        last_time = events[0][0]

        for ts, kind, value in events:
            if kind == "temp":
                if last_temp is not None:
                    dt_h = (ts - last_time).total_seconds() / 3600.0
                    et_h = max(0.0, self._alpha * (last_temp - self._t_base) / 24)
                    deficit = max(0.0, min(deficit + et_h * dt_h, self._d_max))
                last_temp = value
                last_time = ts

            elif kind == "rain":
                if last_temp is not None:
                    dt_h = (ts - last_time).total_seconds() / 3600.0
                    et_h = max(0.0, self._alpha * (last_temp - self._t_base) / 24)
                    deficit = max(0.0, min(deficit + et_h * dt_h, self._d_max))
                    last_time = ts

                rain_delta = self._compute_backfill_rain_delta(value, last_rain)
                deficit = max(0.0, deficit - rain_delta)
                last_rain = value

        return deficit

    def _compute_backfill_rain_delta(self, rain_now: float, last_rain: float) -> float:
        """Compute rain delta for backfill replay."""
        if self._rain_type == RAIN_TYPE_EVENT:
            if rain_now == last_rain:
                return 0.0
            return max(0.0, rain_now)

        # daily_total: negative delta = midnight rollover
        rain_delta = rain_now - last_rain
        if rain_delta < 0:
            rain_delta = rain_now
        return max(0.0, rain_delta)

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

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "L"
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        zone_config: dict,
        dryness_sensor: DrynessIndexSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._hass = hass
        self._dryness = dryness_sensor
        self._zone_name = zone_config[CONF_ZONE_NAME]
        self._valve = zone_config.get(CONF_ZONE_VALVE)
        self._area = zone_config.get(CONF_ZONE_AREA, 0.0)
        self._system_type = zone_config.get(CONF_ZONE_SYSTEM_TYPE)
        self._flow_rate = zone_config.get(CONF_ZONE_FLOW_RATE, 0.0)
        self._threshold = zone_config.get(CONF_ZONE_THRESHOLD, DEFAULT_THRESHOLD)
        self._delivery_mode = zone_config.get(CONF_ZONE_DELIVERY_MODE, DEFAULT_DELIVERY_MODE)
        self._volume_entity = zone_config.get(CONF_ZONE_VOLUME_ENTITY)
        self._flow_meter_sensor = zone_config.get(CONF_ZONE_FLOW_METER_SENSOR)
        self._delivery_timeout = zone_config.get(CONF_ZONE_DELIVERY_TIMEOUT, DEFAULT_DELIVERY_TIMEOUT_S)
        self._battery_sensor = zone_config.get(CONF_ZONE_BATTERY_SENSOR)
        self._irrigating = False
        self._last_irrigated: datetime | None = None
        self._last_volume_delivered: float = 0.0

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
        self._attr_name = "Volume"
        self._attr_unique_id = f"irrigation_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info

        # Register as listener on the dryness sensor
        dryness_sensor.register_zone_listener(self._on_et_update)

    async def async_added_to_hass(self) -> None:
        """Restore zone deficit from previous state."""
        last = await self.async_get_last_state()
        if last and last.attributes:
            with contextlib.suppress(ValueError, TypeError):
                self._zone_deficit = float(last.attributes.get("deficit_mm", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                ts = last.attributes.get("last_irrigated")
                if ts:
                    self._last_irrigated = datetime.fromisoformat(ts)
                    self._last_volume_delivered = float(last.attributes.get("last_volume_delivered", 0.0))

    def _get_latitude(self) -> float:
        """Get latitude from HA config, default to 45.0 (northern)."""
        try:
            return self._hass.config.latitude
        except AttributeError:
            return 45.0

    def _get_current_kc(self) -> float:
        """Compute the current Kc for this zone."""
        doy = datetime.now().timetuple().tm_yday
        return compute_kc(doy, self._plant_family, self._manual_kc, self._get_latitude())

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
    def delivery_mode(self) -> str:
        """Configured delivery mode for this zone."""
        return self._delivery_mode

    @property
    def volume_entity(self) -> str | None:
        """Entity ID of the number entity for volume_preset mode."""
        return self._volume_entity

    @property
    def flow_meter_sensor(self) -> str | None:
        """Entity ID of the flow meter sensor for flow_meter mode."""
        return self._flow_meter_sensor

    @property
    def battery_sensor(self) -> str | None:
        """Entity ID of the battery sensor for low-battery alerts."""
        return self._battery_sensor

    @property
    def delivery_timeout(self) -> int:
        """Safety timeout in seconds for flow_meter and volume_preset modes."""
        return self._delivery_timeout

    @property
    def is_irrigating(self) -> bool:
        """True if this zone is currently being irrigated."""
        return self._irrigating

    def set_irrigating(self, state: bool) -> None:
        """Set the irrigating state (called by controller)."""
        self._irrigating = state

    def reset_deficit(self) -> None:
        """Reset this zone's deficit to zero (called after irrigation)."""
        self._last_volume_delivered = round(self.volume_liters, 1)
        self._last_irrigated = datetime.now()
        self._zone_deficit = 0.0

    @property
    def volume_liters(self) -> float:
        """Volume to irrigate this zone [L]."""
        if self._efficiency <= 0:
            return 0.0
        return self._zone_deficit * self._area / self._efficiency

    @property
    def duration_s(self) -> int:
        """Irrigation duration for this zone [s].

        Only meaningful for estimated_flow mode. Returns 0 for other modes.
        """
        if self._delivery_mode != DELIVERY_MODE_ESTIMATED_FLOW:
            return 0
        if self._flow_rate <= 0:
            return 0
        return round(self.volume_liters / self._flow_rate * 60)

    @property
    def native_value(self) -> float:
        return round(self.volume_liters, 1)

    @property
    def extra_state_attributes(self) -> dict:
        kc = self._get_current_kc()
        attrs = {
            "zone_name": self._zone_name,
            "valve": self._valve,
            "delivery_mode": self._delivery_mode,
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
        if self._last_irrigated:
            attrs["last_irrigated"] = self._last_irrigated.isoformat()
            attrs["last_volume_delivered"] = self._last_volume_delivered
        if self._volume_entity:
            attrs["volume_entity"] = self._volume_entity
        if self._flow_meter_sensor:
            attrs["flow_meter_sensor"] = self._flow_meter_sensor
        if self._delivery_mode != DELIVERY_MODE_ESTIMATED_FLOW:
            attrs["delivery_timeout_s"] = self._delivery_timeout
        return attrs


# ══════════════════════════════════════════════════════════
#  ZoneDeficitSensor (per-zone deficit in mm)
# ══════════════════════════════════════════════════════════


class ZoneDeficitSensor(SensorEntity):
    """Per-zone soil water deficit [mm].

    Mirrors the zone deficit from the parent IrrigationZoneSensor
    as a dedicated sensor entity, making it visible in the device page.
    """

    _attr_has_entity_name = True
    _attr_name = "Deficit"
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent-alert"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"deficit_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._zone_deficit, 2)
