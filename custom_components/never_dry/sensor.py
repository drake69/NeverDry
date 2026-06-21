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
import math
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfArea,
    UnitOfLength,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
    UnitOfVolumetricFlux,
)
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
    CONF_ZONE_HW_MAX_DURATION_PAYLOAD,
    CONF_ZONE_HW_MAX_DURATION_TOPIC,
    CONF_ZONE_IRRIGATION_MODE,
    CONF_ZONE_IRRIGATION_TIME,
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
    ET_BUFFER_MIN_READINGS,
    ET_BUFFER_SIZE,
    ET_TEMP_VALID_RANGE,
    KC_ANCHOR_DAYS,
    PLANT_FAMILIES,
    RAIN_TYPE_EVENT,
    SYSTEM_TYPES,
)
from .controller import IrrigationController

_LOGGER = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
#  SensorBuffer — rolling median for ET input robustness
# ══════════════════════════════════════════════════════════


class SensorBuffer:
    """Rolling FIFO buffer of valid numeric sensor readings.

    Rejects ``None``, ``'unavailable'``, ``'unknown'``, NaN, ±inf, and
    values outside ``valid_range``. Returns the median of buffered readings
    as a robust estimate; returns ``None`` when fewer than
    ``min_readings`` valid samples are available.
    """

    def __init__(
        self,
        size: int,
        valid_range: tuple[float, float] = (-math.inf, math.inf),
    ) -> None:
        self._size = size
        self._lo, self._hi = valid_range
        self._buf: deque[float] = deque(maxlen=size)

    def push(self, raw) -> bool:
        """Parse and push ``raw`` if it is a valid in-range finite number.

        Returns ``True`` when the value was accepted.
        """
        if raw in (None, "unavailable", "unknown"):
            return False
        try:
            v = float(raw)
        except (ValueError, TypeError):
            return False
        if not math.isfinite(v) or v < self._lo or v > self._hi:
            return False
        self._buf.append(v)
        return True

    def median(self, min_readings: int = 1) -> float | None:
        """Return the median, or ``None`` if fewer than ``min_readings`` samples."""
        if len(self._buf) < min_readings:
            return None
        s = sorted(self._buf)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    def __len__(self) -> int:
        return len(self._buf)


def _to_celsius(state) -> float | None:
    """Return temperature in °C from a HA State object.

    Converts from °F if unit_of_measurement is '°F'. Returns None when the
    state is unavailable or not numeric.
    """
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    if state.attributes.get("unit_of_measurement") == "°F":
        return (value - 32) * 5 / 9
    return value


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
        entities.append(ZoneRainSensor(zone_sensor, zone_device))
        entities.append(ZoneSessionWaterSensor(zone_sensor, zone_device))
        entities.append(ZoneYearlyWaterSensor(zone_sensor, zone_device))
        # Operational info
        entities.append(ZoneLastIrrigatedSensor(zone_sensor, zone_device))
        entities.append(ZoneLastSourceSensor(zone_sensor, zone_device))
        entities.append(ZoneLastVolumeSensor(zone_sensor, zone_device))
        entities.append(ZoneFlowRateSensor(zone_sensor, zone_device))
        entities.append(ZoneDurationSensor(zone_sensor, zone_device))
        entities.append(ZoneLastDurationSensor(zone_sensor, zone_device))
        entities.append(ZoneKcSensor(zone_sensor, zone_device))
        # Diagnostic (config)
        entities.append(ZoneIrrigationModeSensor(zone_sensor, zone_device))
        entities.append(ZoneIrrigationTimeSensor(zone_sensor, zone_device))
        entities.append(ZoneThresholdSensor(zone_sensor, zone_device))
        entities.append(ZoneAreaSensor(zone_sensor, zone_device))
        entities.append(ZoneEfficiencySensor(zone_sensor, zone_device))
        # Linked mirrors of external entities configured for this zone
        slug = zone_conf[CONF_ZONE_NAME].lower().replace(" ", "_")
        if zone_conf.get(CONF_ZONE_VALVE):
            entities.append(
                ZoneLinkedSensor(
                    hass,
                    zone_conf[CONF_ZONE_VALVE],
                    "Valve",
                    "mdi:valve",
                    f"linked_valve_{slug}",
                    zone_device,
                )
            )
        if zone_conf.get(CONF_ZONE_BATTERY_SENSOR):
            entities.append(
                ZoneLinkedSensor(
                    hass,
                    zone_conf[CONF_ZONE_BATTERY_SENSOR],
                    "Battery",
                    "mdi:battery",
                    f"linked_battery_{slug}",
                    zone_device,
                )
            )
        if zone_conf.get(CONF_ZONE_FLOW_METER_SENSOR):
            entities.append(
                ZoneLinkedSensor(
                    hass,
                    zone_conf[CONF_ZONE_FLOW_METER_SENSOR],
                    "Flow meter",
                    "mdi:gauge",
                    f"linked_flow_{slug}",
                    zone_device,
                )
            )

    return entities, di_sensor, zone_sensors


_HW_DURATION_KEYWORDS = frozenset({"max", "duration", "time", "irrigation", "timer", "delay"})
_HW_MINUTE_KEYWORDS = frozenset({"min", "minute", "minutes"})


def _discover_hw_max_duration(
    hass: HomeAssistant,
    switch_entity_id: str,
) -> tuple[str | None, float]:
    """Look for a hardware max-duration ``number`` entity on the same HA device.

    Searches the entity registry for ``number.*`` entities sharing the same
    device as ``switch_entity_id`` whose name contains irrigation/duration
    keywords. Returns ``(entity_id, multiplier)`` where multiplier converts
    seconds to the entity's native unit (1.0 for seconds, 1/60 for minutes),
    or ``(None, 1.0)`` when nothing suitable is found.
    """
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_registry import async_entries_for_device

    ent_reg = er.async_get(hass)
    switch_entry = ent_reg.async_get(switch_entity_id)
    if switch_entry is None or switch_entry.device_id is None:
        return None, 1.0

    device_id = switch_entry.device_id
    candidates = [
        entry
        for entry in async_entries_for_device(ent_reg, device_id, include_disabled_entities=False)
        if entry.domain == "number"
        and any(kw in (entry.entity_id + " " + (entry.original_name or "")).lower() for kw in _HW_DURATION_KEYWORDS)
    ]
    if not candidates:
        return None, 1.0

    best = candidates[0]
    state = hass.states.get(best.entity_id)
    unit = (state.attributes.get("unit_of_measurement", "") if state else "").lower()
    multiplier = 1.0 / 60.0 if any(kw in unit for kw in _HW_MINUTE_KEYWORDS) else 1.0
    _LOGGER.debug(
        "Valve '%s' hw_max_duration entity discovered: %s (multiplier=%.4f)",
        switch_entity_id,
        best.entity_id,
        multiplier,
    )
    return best.entity_id, multiplier


def _setup_controller(
    hass: HomeAssistant,
    config: dict,
    di_sensor: DrynessIndexSensor,
    zone_sensors: list[IrrigationZoneSensor],
) -> IrrigationController:
    """Create the irrigation controller and register all services.

    Also builds one :class:`ValveOperator` per zone with a valve and a
    shared :class:`ValveNotifier`. Smart valves controlled in
    ``volume_preset`` mode bypass the operator: their entry is omitted
    from the dict.
    """
    from .valve_notifier import ValveNotifier  # local import: optional path
    from .valve_operator import ValveOperator

    inter_zone_delay = config.get(CONF_INTER_ZONE_DELAY, DEFAULT_INTER_ZONE_DELAY)

    notifier = ValveNotifier(hass)
    valve_operators: dict = {}
    for zs in zone_sensors:
        if not zs.valve:
            continue
        # volume_preset relies on smart-valve self-control; bypass operator.
        if getattr(zs, "delivery_mode", None) == "volume_preset":
            continue
        hw_entity, hw_mult = _discover_hw_max_duration(hass, zs.valve)
        op = ValveOperator(
            hass=hass,
            switch_entity_id=zs.valve,
            flow_sensor_entity_id=zs.flow_meter_sensor,
            zone_name=zs.zone_name,
            notifier=notifier,
            max_open_duration_s=zs.delivery_timeout,
            hw_max_duration_entity=hw_entity,
            hw_max_duration_multiplier=hw_mult,
            hw_max_duration_topic=zs.hw_max_duration_topic,
            hw_max_duration_payload_template=zs.hw_max_duration_payload,
        )
        valve_operators[zs.valve] = op
        zs.set_operator(op)

    controller = IrrigationController(
        hass,
        di_sensor,
        zone_sensors,
        inter_zone_delay,
        valve_operators=valve_operators,
        notifier=notifier,
    )
    controller.register_services()
    return controller  # caller may store valve_operators via controller.valve_operators


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
    from .const import DOMAIN

    config = dict(entry.data)
    entities, di_sensor, zone_sensors = _create_entities(hass, config, entry.entry_id)
    async_add_entities(entities, True)
    controller = _setup_controller(hass, config, di_sensor, zone_sensors)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][f"_controller_{entry.entry_id}"] = controller
    hass.data[DOMAIN][f"_operators_{entry.entry_id}"] = controller.valve_operators


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
    _attr_device_class = SensorDeviceClass.PRECIPITATION_INTENSITY
    _attr_native_unit_of_measurement = UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR
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
        t = _to_celsius(new_state)
        if t is not None:
            self._value = max(0.0, self._alpha * (t - self._t_base) / 24)
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
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
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
        self._last_rain = 0.0
        self._last_update = datetime.now()
        self._zone_listeners: list[Callable] = []
        self._temp_buffer = SensorBuffer(ET_BUFFER_SIZE, valid_range=ET_TEMP_VALID_RANGE)
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
            rain_delta = self._compute_rain_delta()

            # Push temp into the buffer (converted to °C if sensor reports °F);
            # invalid/unavailable readings are rejected, median stays stable.
            raw_state = self._hass.states.get(self._temp_sensor)
            self._temp_buffer.push(_to_celsius(raw_state))

            t_median = self._temp_buffer.median(ET_BUFFER_MIN_READINGS)
            if t_median is None:
                # Buffer not ready yet (startup); keep deficit frozen.
                self.async_write_ha_state()
                return

            et_h = max(0.0, self._alpha * (t_median - self._t_base) / 24)
            et_dt = et_h * dt_h
            self._deficit = max(0.0, min(self._deficit + et_dt - rain_delta, self._d_max))
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
            # VWC sensor not yet numeric (boot / unavailable):
            # keep the previous self._deficit unchanged.
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
        raw_state = self._hass.states.get(self._temp_sensor)
        self._temp_buffer.push(raw_state.state if raw_state is not None else None)
        t_median = self._temp_buffer.median(ET_BUFFER_MIN_READINGS)
        if t_median is None:
            return

        rain_delta = self._compute_rain_delta()
        et_dt = max(0.0, self._alpha * (t_median - self._t_base) / 24) * dt_h
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
            t = _to_celsius(s)
            if t is not None:
                events.append((s.last_changed, "temp", t))

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

    def set_deficit_mm(self, value: float) -> None:
        """Set deficit to an arbitrary value [mm] — intended for testing/debugging."""
        self._deficit = max(0.0, min(float(value), self._d_max))
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
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
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
        if self._flow_rate > 30:
            _LOGGER.warning(
                "Zone '%s': flow_rate_lpm=%.1f L/min (= %.0f L/h) looks unrealistically high "
                "for garden irrigation. If you configured it in L/h, divide by 60 (e.g. %.0f L/h → %.2f L/min).",
                zone_config.get(CONF_ZONE_NAME, "?"),
                self._flow_rate,
                self._flow_rate * 60,
                self._flow_rate,
                self._flow_rate / 60,
            )
        self._threshold = zone_config.get(CONF_ZONE_THRESHOLD, DEFAULT_THRESHOLD)
        self._delivery_mode = zone_config.get(CONF_ZONE_DELIVERY_MODE, DEFAULT_DELIVERY_MODE)
        self._volume_entity = zone_config.get(CONF_ZONE_VOLUME_ENTITY)
        self._flow_meter_sensor = zone_config.get(CONF_ZONE_FLOW_METER_SENSOR)
        self._delivery_timeout = zone_config.get(CONF_ZONE_DELIVERY_TIMEOUT, DEFAULT_DELIVERY_TIMEOUT_S)
        self._battery_sensor = zone_config.get(CONF_ZONE_BATTERY_SENSOR)
        self._irrigation_mode = zone_config.get(CONF_ZONE_IRRIGATION_MODE, "manual")
        self._irrigation_time = zone_config.get(CONF_ZONE_IRRIGATION_TIME)
        self._hw_max_duration_topic: str | None = zone_config.get(CONF_ZONE_HW_MAX_DURATION_TOPIC)
        self._hw_max_duration_payload: str = zone_config.get(CONF_ZONE_HW_MAX_DURATION_PAYLOAD, "{value}")
        self._irrigating = False
        self._last_irrigated: datetime | None = None
        self._last_volume_delivered: float = 0.0
        self._last_irrigation_source: str | None = None
        self._last_session_duration_s: int = 0
        self._operator = None  # set by _setup_controller after operator creation
        # Snapshot of zone_deficit captured by the controller at the start
        # of an irrigation cycle. Used by flow-metered delivery modes for
        # real-time deficit updates: every update is computed as
        # ``max(0, snapshot - delivered_mm)`` so intermediate writes are
        # idempotent and the end-of-cycle settle never double-counts.
        # ``None`` outside an active cycle.
        self._deficit_at_irrigation_start: float | None = None
        self._total_rain: float = 0.0
        self._total_water_delivered: float = 0.0
        self._yearly_water_delivered: float = 0.0
        self._yearly_water_year: int = datetime.now().year
        self._session_water_delivered: float = 0.0

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
        """Restore zone deficit from previous state.

        If no previous state exists (new zone), initialize from the
        global Dryness Index scaled by this zone's Kc — so a new zone
        starts with a realistic deficit instead of zero.
        """
        last = await self.async_get_last_state()
        if last and last.attributes:
            with contextlib.suppress(ValueError, TypeError):
                self._zone_deficit = float(last.attributes.get("deficit_mm", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                ts = last.attributes.get("last_irrigated")
                if ts:
                    self._last_irrigated = datetime.fromisoformat(ts)
                    self._last_volume_delivered = float(last.attributes.get("last_volume_delivered", 0.0))
                    self._last_irrigation_source = last.attributes.get("last_irrigation_source")
                    self._last_session_duration_s = int(last.attributes.get("last_session_duration_s", 0))
            with contextlib.suppress(ValueError, TypeError):
                self._total_rain = float(last.attributes.get("total_rain_mm", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                self._total_water_delivered = float(last.attributes.get("total_water_delivered_l", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                self._yearly_water_delivered = float(last.attributes.get("yearly_water_delivered_l", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                self._yearly_water_year = int(last.attributes.get("yearly_water_year", datetime.now().year))
                # Reset yearly counter if year changed since last save
                if datetime.now().year != self._yearly_water_year:
                    self._yearly_water_delivered = 0.0
                    self._yearly_water_year = datetime.now().year
        else:
            # New zone: seed deficit from global Dryness Index * Kc
            kc = self._get_current_kc()
            self._zone_deficit = self._dryness.deficit * kc

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
            if rain > 0:
                self._total_rain += rain
            self._zone_deficit = max(
                0.0,
                min(self._zone_deficit + et_h * kc * dt_h - rain, self._d_max),
            )
        if getattr(self, "hass", None):
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
    def irrigation_mode(self) -> str:
        """Configured irrigation mode: manual, reactive, or scheduled."""
        return self._irrigation_mode

    @property
    def irrigation_time(self) -> str | None:
        """Configured daily irrigation time (HH:MM) or None."""
        return self._irrigation_time

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
        """Safety timeout in seconds for flow_meter and volume_preset modes.

        Returns the greater of the configured floor and the estimated delivery
        duration, so large deficits never hit the timeout before completion.
        """
        return max(self._delivery_timeout, round(self.duration_s * 1.1))

    @property
    def hw_max_duration_topic(self) -> str | None:
        """Optional raw MQTT topic for writing the on-device hardware max-duration."""
        return self._hw_max_duration_topic

    @property
    def hw_max_duration_payload(self) -> str:
        """Payload template for the hw_max_duration MQTT publish (``{value}`` placeholder)."""
        return self._hw_max_duration_payload

    def set_operator(self, operator) -> None:
        """Attach the ValveOperator so FSM state can be exposed in attributes."""
        self._operator = operator

    @property
    def is_irrigating(self) -> bool:
        """True if this zone is currently being irrigated."""
        return self._irrigating

    def set_irrigating(self, state: bool) -> None:
        """Set the irrigating state (called by controller)."""
        if state and not self._irrigating:
            # Starting a new irrigation session
            self._session_water_delivered = 0.0
        self._irrigating = state

    def set_deficit_mm(self, value: float) -> None:
        """Set zone deficit to an arbitrary value [mm] — intended for testing/debugging."""
        self._zone_deficit = max(0.0, min(float(value), self._d_max))

    def reset_deficit(self, source: str = "unknown") -> None:
        """Reset this zone's deficit to zero (called after irrigation)."""
        self._last_irrigation_source = source
        self._last_volume_delivered = round(self.volume_liters, 1)
        self._session_water_delivered = self._last_volume_delivered
        self._total_water_delivered += self._last_volume_delivered
        # Reset yearly counter on year change
        now = datetime.now()
        if now.year != self._yearly_water_year:
            self._yearly_water_delivered = 0.0
            self._yearly_water_year = now.year
        self._yearly_water_delivered += self._last_volume_delivered
        self._last_irrigated = now
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
            "irrigation_mode": self._irrigation_mode,
            "irrigation_time": self._irrigation_time,
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
        attrs["total_rain_mm"] = round(self._total_rain, 2)
        attrs["total_water_delivered_l"] = round(self._total_water_delivered, 1)
        attrs["yearly_water_delivered_l"] = round(self._yearly_water_delivered, 1)
        attrs["yearly_water_year"] = self._yearly_water_year
        attrs["session_water_delivered_l"] = round(self._session_water_delivered, 1)
        if self._last_irrigated:
            attrs["last_irrigated"] = self._last_irrigated.isoformat()
            attrs["last_volume_delivered"] = self._last_volume_delivered
            attrs["last_irrigation_source"] = self._last_irrigation_source
            attrs["last_session_duration_s"] = self._last_session_duration_s
        if self._volume_entity:
            attrs["volume_entity"] = self._volume_entity
        if self._flow_meter_sensor:
            attrs["flow_meter_sensor"] = self._flow_meter_sensor
        if self._delivery_mode != DELIVERY_MODE_ESTIMATED_FLOW:
            attrs["delivery_timeout_s"] = self.delivery_timeout
        if self._operator is not None:
            attrs["valve_fsm_state"] = self._operator.state.value
            attrs["valve_in_maintenance"] = self._operator.is_in_maintenance
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
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_name = "Deficit"
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
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
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._zone_deficit, 2)

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "flow_rate_lpm": self._zone_sensor._flow_rate,
            "irrigating": self._zone_sensor._irrigating,
        }
        if self._zone_sensor._last_irrigated:
            attrs["last_session_duration_s"] = self._zone_sensor._last_session_duration_s
        op = self._zone_sensor._operator
        if op is not None:
            attrs["valve_fsm_state"] = op.state.value
            attrs["valve_in_maintenance"] = op.is_in_maintenance
        return attrs


# ══════════════════════════════════════════════════════════
#  ZoneRainSensor (cumulative rain per zone in mm)
# ══════════════════════════════════════════════════════════


class ZoneRainSensor(SensorEntity):
    """Cumulative rain received by this zone [mm]."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_name = "Rain"
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:weather-rainy"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"rain_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._total_rain, 2)


# ══════════════════════════════════════════════════════════
#  ZoneSessionWaterSensor (current/last irrigation session in L)
# ══════════════════════════════════════════════════════════


class ZoneSessionWaterSensor(SensorEntity):
    """Water delivered in the current or last irrigation session [L].

    Resets to zero when a new irrigation starts.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_name = "Session water"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-pump"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"session_water_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._session_water_delivered, 1)


# ══════════════════════════════════════════════════════════
#  ZoneYearlyWaterSensor (yearly cumulative irrigation in L)
# ══════════════════════════════════════════════════════════


class ZoneYearlyWaterSensor(SensorEntity):
    """Water delivered by irrigation this year [L].

    Resets automatically on January 1st.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_name = "Yearly water"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"yearly_water_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._yearly_water_delivered, 1)


# ══════════════════════════════════════════════════════════
#  ZoneDurationSensor / ZoneLastDurationSensor
# ══════════════════════════════════════════════════════════


class ZoneDurationSensor(SensorEntity):
    """Planned irrigation duration for the next session [s]."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_name = "Duration"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer"
    _attr_should_poll = False

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"duration_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._zone_sensor.duration_s


class ZoneLastDurationSensor(SensorEntity):
    """Actual duration of the last completed irrigation session [s]."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_name = "Last duration"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer"
    _attr_should_poll = False

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"last_duration_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        if not self._zone_sensor._last_irrigated:
            return None
        return self._zone_sensor._last_session_duration_s


# ══════════════════════════════════════════════════════════
#  Zone diagnostic / info sensors
# ══════════════════════════════════════════════════════════


class _ZoneTextSensor(SensorEntity):
    """Base for zone text sensors shown in device page."""

    _attr_has_entity_name = True

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        name: str,
        icon: str,
        unique_suffix: str,
        device_info: DeviceInfo | None = None,
        diagnostic: bool = False,
    ) -> None:
        self._zone_sensor = zone_sensor
        self._attr_name = name
        self._attr_icon = icon
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"{unique_suffix}_{slug}"
        if device_info:
            self._attr_device_info = device_info
        if diagnostic:
            from homeassistant.const import EntityCategory

            self._attr_entity_category = EntityCategory.DIAGNOSTIC


class ZoneLastIrrigatedSensor(_ZoneTextSensor):
    """When the zone was last irrigated."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Last irrigated",
            "mdi:clock-outline",
            "last_irrigated_zone",
            device_info,
        )

    @property
    def native_value(self) -> str | None:
        ts = self._zone_sensor._last_irrigated
        return ts.isoformat() if ts else None


class ZoneLastSourceSensor(_ZoneTextSensor):
    """How the zone was last irrigated."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Last source",
            "mdi:information-outline",
            "last_source_zone",
            device_info,
        )

    @property
    def native_value(self) -> str | None:
        return self._zone_sensor._last_irrigation_source


class ZoneFlowRateSensor(_ZoneTextSensor):
    """Configured flow rate for this zone [L/min]."""

    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.LITERS_PER_MINUTE

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Flow rate",
            "mdi:gauge",
            "flow_rate_zone",
            device_info,
        )

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._flow_rate, 2)


class ZoneLastVolumeSensor(_ZoneTextSensor):
    """Volume delivered in the last irrigation."""

    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Last volume",
            "mdi:water",
            "last_volume_zone",
            device_info,
        )

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._last_volume_delivered, 1)


class ZoneIrrigationModeSensor(_ZoneTextSensor):
    """Configured irrigation mode."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Irrigation mode",
            "mdi:cog",
            "irrigation_mode_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> str:
        return self._zone_sensor._irrigation_mode


class ZoneIrrigationTimeSensor(_ZoneTextSensor):
    """Configured daily irrigation time."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Irrigation time",
            "mdi:clock-time-six",
            "irrigation_time_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> str | None:
        return self._zone_sensor._irrigation_time


class ZoneThresholdSensor(_ZoneTextSensor):
    """Configured irrigation threshold."""

    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Threshold",
            "mdi:target",
            "threshold_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> float:
        return self._zone_sensor._threshold


class ZoneAreaSensor(_ZoneTextSensor):
    """Configured zone area."""

    _attr_device_class = SensorDeviceClass.AREA
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Area",
            "mdi:texture-box",
            "area_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> float:
        return self._zone_sensor._area


class ZoneEfficiencySensor(_ZoneTextSensor):
    """Configured zone efficiency."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Efficiency",
            "mdi:percent",
            "efficiency_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._efficiency, 2)


class ZoneKcSensor(_ZoneTextSensor):
    """Current crop coefficient Kc."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Kc",
            "mdi:leaf",
            "kc_zone",
            device_info,
        )
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h, et_h, rain):
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._get_current_kc(), 3)


# ══════════════════════════════════════════════════════════
#  ZoneLinkedSensor — mirrors an external HA entity inside
#  the NeverDry zone device (valve, battery, flow meter)
# ══════════════════════════════════════════════════════════


class ZoneLinkedSensor(SensorEntity):
    """Mirrors the state of an external HA entity within the NeverDry zone device.

    Used to surface valve switch state, battery level, and flow meter readings
    directly on the zone device card without leaving the NeverDry UI context.
    Updates in real-time via state-change subscription.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        source_entity_id: str,
        name: str,
        icon: str,
        unique_id: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._hass = hass
        self._source_entity_id = source_entity_id
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = unique_id
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        async_track_state_change_event(self.hass, [self._source_entity_id], self._on_source_change)

    @callback
    def _on_source_change(self, event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self):
        state = self.hass.states.get(self._source_entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        raw = state.state
        if raw == "on":
            return "open"
        if raw == "off":
            return "closed"
        try:
            return float(raw)
        except ValueError:
            return raw

    @property
    def native_unit_of_measurement(self) -> str | None:
        state = self.hass.states.get(self._source_entity_id)
        if state:
            return state.attributes.get("unit_of_measurement")
        return None

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self._source_entity_id)
        return state is not None and state.state not in ("unavailable", "unknown")
