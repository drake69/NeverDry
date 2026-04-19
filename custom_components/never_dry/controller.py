"""Irrigation controller for the NeverDry integration.

Manages valve on/off cycles directly. Given a zone (or all zones),
the controller delivers water using one of three delivery modes:
  - estimated_flow: open valve → wait timer-based duration → close valve
  - flow_meter: open valve → monitor flow sensor → close at target volume
  - volume_preset: send volume to smart valve → wait for self-close

Zones are irrigated sequentially to avoid pressure drops.
An emergency stop service closes all valves immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)

from .const import (
    ANOMALY_DEFICIT_MULTIPLIER,
    ATTR_ZONE_NAME,
    DEFAULT_BATTERY_LOW_THRESHOLD,
    DEFAULT_INTER_ZONE_DELAY,
    DEFAULT_THRESHOLD,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    DOMAIN,
    EVENT_IRRIGATION_COMPLETE,
    FLOW_METER_POLL_INTERVAL_S,
    MIN_SERVICE_INTERVAL_S,
    SERVICE_IRRIGATE_ALL,
    SERVICE_IRRIGATE_ZONE,
    SERVICE_MARK_IRRIGATED,
    SERVICE_RESET,
    SERVICE_STOP,
)

MONITORING_INTERVAL = 6 * 3600  # 6 hours in seconds

_LOGGER = logging.getLogger(__name__)


class IrrigationController:
    """Controls irrigation valves based on deficit calculations.

    Holds references to the DrynessIndexSensor and all IrrigationZoneSensors.
    Exposes HA services to trigger irrigation.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        dryness_sensor,
        zone_sensors: list,
        inter_zone_delay: int = DEFAULT_INTER_ZONE_DELAY,
    ) -> None:
        self._hass = hass
        self._dryness = dryness_sensor
        self._zones = {zs.zone_name: zs for zs in zone_sensors}
        self._inter_zone_delay = inter_zone_delay
        self._running = False
        self._stop_requested = False
        self._active_valve: str | None = None
        self._irrigation_task: asyncio.Task | None = None
        self._monitoring_mode = not any(zs.valve for zs in zone_sensors)
        self._unsub_monitor = None
        self._last_service_call: dict[str, float] = {}
        # Manual valve tracking: valve_entity_id → flow meter reading at valve open
        self._manual_valve_open: dict[str, float | None] = {}
        # Reverse map: valve_entity_id → zone_name
        self._valve_to_zone: dict[str, str] = {zs.valve: zs.zone_name for zs in zone_sensors if zs.valve}
        # Battery sensor → zone_name map
        self._battery_to_zone: dict[str, str] = {
            zs.battery_sensor: zs.zone_name for zs in zone_sensors if zs.battery_sensor
        }
        # Track which zones have already been alerted for low battery
        self._battery_alerted: set[str] = set()
        # Track which zones have already been alerted for anomalous deficit
        self._deficit_anomaly_alerted: set[str] = set()

    @property
    def is_monitoring_mode(self) -> bool:
        """True if no valves are configured (monitoring only)."""
        return self._monitoring_mode

    @property
    def is_running(self) -> bool:
        """Return True if an irrigation cycle is in progress."""
        return self._running

    @property
    def active_valve(self) -> str | None:
        """Return the entity_id of the currently open valve, or None."""
        return self._active_valve

    def register_services(self) -> None:
        """Register all irrigation services with Home Assistant."""
        self._hass.services.async_register(DOMAIN, SERVICE_RESET, self._handle_reset)
        self._hass.services.async_register(DOMAIN, SERVICE_IRRIGATE_ZONE, self._handle_irrigate_zone)
        self._hass.services.async_register(DOMAIN, SERVICE_IRRIGATE_ALL, self._handle_irrigate_all)
        self._hass.services.async_register(DOMAIN, SERVICE_STOP, self._handle_stop)
        self._hass.services.async_register(DOMAIN, SERVICE_MARK_IRRIGATED, self._handle_mark_irrigated)

        # Monitor valve state changes to detect manual irrigation
        valve_entities = [v for v in self._valve_to_zone if v]
        if valve_entities:
            async_track_state_change_event(self._hass, valve_entities, self._on_valve_state_change)

        # Monitor battery sensors for low-battery alerts
        battery_entities = [b for b in self._battery_to_zone if b]
        if battery_entities:
            async_track_state_change_event(self._hass, battery_entities, self._on_battery_change)

        # Periodic deficit anomaly check (all modes, every 6h)
        from datetime import timedelta

        async_track_time_interval(
            self._hass,
            self._check_deficit_anomaly,
            timedelta(hours=6),
        )

        # Set up automatic irrigation per zone based on mode
        for zs in self._zones.values():
            if not zs.valve:
                continue
            mode = zs.irrigation_mode

            if mode == "scheduled" and zs.irrigation_time:
                try:
                    parts = zs.irrigation_time.split(":")
                    hour, minute = int(parts[0]), int(parts[1])
                    async_track_time_change(
                        self._hass,
                        self._make_scheduled_handler(zs.zone_name),
                        hour=hour,
                        minute=minute,
                        second=0,
                    )
                    _LOGGER.info(
                        "Mode B (scheduled): zone='%s' at %s",
                        zs.zone_name,
                        zs.irrigation_time,
                    )
                except (ValueError, IndexError):
                    _LOGGER.error(
                        "Invalid irrigation_time '%s' for zone '%s'",
                        zs.irrigation_time,
                        zs.zone_name,
                    )

            elif mode == "reactive":
                # Register listener on dryness sensor for reactive mode
                zs._dryness.register_zone_listener(
                    self._make_reactive_handler(zs.zone_name),
                )
                _LOGGER.info(
                    "Mode A (reactive): zone='%s', threshold=%.1fmm",
                    zs.zone_name,
                    zs._threshold,
                )

        # Start monitoring mode if no valves are configured
        if self._monitoring_mode:
            _LOGGER.info(
                "No valves configured — running in monitoring mode. "
                "Irrigation alerts will be sent every 6 hours when needed."
            )
            self._unsub_monitor = async_track_time_interval(
                self._hass,
                self._check_and_notify,
                timedelta(hours=6),
            )

    # ── Scheduled irrigation ────────────────────────────────

    def _make_scheduled_handler(self, zone_name: str):
        """Create a time-triggered handler for a specific zone."""

        @callback
        def _handler(now) -> None:
            zone = self._zones.get(zone_name)
            if zone is None:
                return
            threshold = zone.extra_state_attributes.get(
                "threshold_mm",
                DEFAULT_THRESHOLD,
            )
            if zone._zone_deficit < threshold:
                _LOGGER.debug(
                    "Scheduled check: zone='%s' deficit=%.1fmm < threshold=%.1fmm, skipping",
                    zone_name,
                    zone._zone_deficit,
                    threshold,
                )
                return
            if self._running:
                _LOGGER.warning(
                    "Scheduled irrigation for '%s' skipped — another irrigation is in progress",
                    zone_name,
                )
                return
            _LOGGER.info(
                "Scheduled irrigation triggered: zone='%s', deficit=%.1fmm, threshold=%.1fmm",
                zone_name,
                zone._zone_deficit,
                threshold,
            )
            self._irrigation_task = self._hass.async_create_task(
                self._irrigate_zones([zone_name]),
            )

        return _handler

    def _make_reactive_handler(self, zone_name: str):
        """Create a deficit-triggered handler for reactive mode (Mode A)."""

        def _handler(dt_h: float, et_h: float, rain: float) -> None:
            zone = self._zones.get(zone_name)
            if zone is None:
                return
            if zone._zone_deficit < zone._threshold:
                return
            if self._running:
                return
            if self._is_throttled("reactive", zone_name):
                return
            _LOGGER.info(
                "Reactive irrigation triggered: zone='%s', deficit=%.1fmm >= threshold=%.1fmm",
                zone_name,
                zone._zone_deficit,
                zone._threshold,
            )
            self._irrigation_task = self._hass.async_create_task(
                self._irrigate_zones([zone_name]),
            )

        return _handler

    # ── Rate limiting ──────────────────────────────────────

    def _is_throttled(self, service_name: str, zone_name: str | None = None) -> bool:
        """Return True if a service call should be rejected (rate limit).

        Throttling is per service+zone so that calling irrigate on
        different zones in quick succession is allowed.
        """
        key = f"{service_name}:{zone_name}" if zone_name else service_name
        now = time.monotonic()
        last = self._last_service_call.get(key, 0.0)
        elapsed = now - last
        if elapsed < MIN_SERVICE_INTERVAL_S:
            _LOGGER.warning(
                "Service %s throttled — %0.1fs since last call (min %ds)",
                key,
                elapsed,
                MIN_SERVICE_INTERVAL_S,
            )
            return True
        self._last_service_call[key] = now
        return False

    # ── Service handlers ─────────────────────────────────

    async def _handle_reset(self, call: ServiceCall) -> None:
        """Reset reference deficit and all zone deficits to zero."""
        if self._is_throttled("reset"):
            return
        self._dryness.reset()
        self._dryness.async_write_ha_state()
        for zs in self._zones.values():
            zs.reset_deficit()
            zs.async_write_ha_state()

    async def _handle_irrigate_zone(self, call: ServiceCall) -> None:
        """Irrigate a single zone by name."""
        zone_name = call.data.get(ATTR_ZONE_NAME)
        if self._is_throttled("irrigate_zone", zone_name):
            return
        if zone_name not in self._zones:
            _LOGGER.error(
                "Zone '%s' not found. Available: %s",
                zone_name,
                list(self._zones.keys()),
            )
            return

        if self._running:
            _LOGGER.warning("Irrigation already in progress, ignoring request")
            return

        self._irrigation_task = self._hass.async_create_task(self._irrigate_zones([zone_name]))

    async def _handle_irrigate_all(self, call: ServiceCall) -> None:
        """Irrigate all zones sequentially."""
        if self._is_throttled("irrigate_all"):
            return
        if self._running:
            _LOGGER.warning("Irrigation already in progress, ignoring request")
            return

        self._irrigation_task = self._hass.async_create_task(self._irrigate_zones(list(self._zones.keys())))

    async def _handle_stop(self, call: ServiceCall) -> None:
        """Emergency stop: close all valves immediately."""
        _LOGGER.info("Emergency stop requested")
        self._stop_requested = True

        # Close the currently active valve
        if self._active_valve:
            await self._close_valve(self._active_valve)

        # Safety: close all configured valves
        for zs in self._zones.values():
            if zs.valve:
                await self._close_valve(zs.valve)

        self._running = False
        self._active_valve = None

    async def _handle_mark_irrigated(self, call: ServiceCall) -> None:
        """Mark one or all zones as manually irrigated (reset deficit, no valve)."""
        zone_name = call.data.get(ATTR_ZONE_NAME)
        if self._is_throttled("mark_irrigated", zone_name):
            return
        if zone_name is not None:
            if zone_name not in self._zones:
                _LOGGER.error(
                    "Zone '%s' not found. Available: %s",
                    zone_name,
                    list(self._zones.keys()),
                )
                return
            self._zones[zone_name].reset_deficit()
            self._zones[zone_name].async_write_ha_state()
            _LOGGER.info("Zone '%s' marked as irrigated, deficit reset", zone_name)
        else:
            for zs in self._zones.values():
                zs.reset_deficit()
                zs.async_write_ha_state()
            _LOGGER.info("All zones marked as irrigated, deficits reset")

    # ── Core irrigation logic ────────────────────────────

    async def _irrigate_zones(self, zone_names: list[str]) -> None:
        """Run irrigation cycle for the given zones sequentially."""
        self._running = True
        self._stop_requested = False
        irrigated_zones = []

        try:
            for i, zone_name in enumerate(zone_names):
                if self._stop_requested:
                    _LOGGER.info("Irrigation stopped by user after %d zones", i)
                    break

                zone = self._zones[zone_name]

                if not zone.valve:
                    _LOGGER.warning("Zone '%s' has no valve configured, skipping", zone_name)
                    continue

                if zone.volume_liters <= 0:
                    _LOGGER.info(
                        "Zone '%s' needs 0L irrigation (deficit=%.1fmm), skipping",
                        zone_name,
                        zone._zone_deficit,
                    )
                    continue

                _LOGGER.info(
                    "Starting irrigation: zone='%s', mode='%s', volume=%.1fL, deficit=%.1fmm",
                    zone_name,
                    zone.delivery_mode,
                    zone.volume_liters,
                    zone._zone_deficit,
                )

                volume_target = zone.volume_liters
                delivered = await self._deliver_water(zone)

                if self._stop_requested:
                    break

                if delivered > 0:
                    irrigated_zones.append(
                        (zone_name, delivered, volume_target),
                    )
                    self._hass.bus.async_fire(
                        EVENT_IRRIGATION_COMPLETE,
                        {
                            "zone": zone_name,
                            "source": "automatic",
                            "volume_liters": round(delivered, 1),
                            "volume_target": round(volume_target, 1),
                            "deficit_mm": round(zone._zone_deficit, 2),
                        },
                    )
                    _LOGGER.info(
                        "Completed irrigation: zone='%s', delivered=%.1fL of %.1fL target",
                        zone_name,
                        delivered,
                        volume_target,
                    )

                # Inter-zone delay (pressure stabilization)
                if i < len(zone_names) - 1 and not self._stop_requested:
                    _LOGGER.debug("Inter-zone delay: %ds", self._inter_zone_delay)
                    await asyncio.sleep(self._inter_zone_delay)

            # Adjust deficits for irrigated zones
            if irrigated_zones and not self._stop_requested:
                all_complete = True
                for zone_name, delivered, target in irrigated_zones:
                    zone = self._zones[zone_name]
                    if delivered >= target:
                        # Full irrigation — reset deficit to zero
                        zone.reset_deficit()
                    else:
                        # Partial irrigation — reduce deficit proportionally
                        all_complete = False
                        delivered_mm = delivered * zone._efficiency / zone._area if zone._area > 0 else 0.0
                        zone._zone_deficit = max(
                            0.0,
                            zone._zone_deficit - delivered_mm,
                        )
                        zone._last_volume_delivered = round(delivered, 1)
                        zone._session_water_delivered = round(delivered, 1)
                        zone._total_water_delivered += delivered
                        zone._yearly_water_delivered += delivered
                        _LOGGER.info(
                            "Partial irrigation: zone='%s', delivered=%.1fL/%.1fL, deficit reduced to %.2fmm",
                            zone_name,
                            delivered,
                            target,
                            zone._zone_deficit,
                        )
                    zone.async_write_ha_state()
                # Reset reference sensor only if ALL zones fully irrigated
                zone_names_irrigated = {z[0] for z in irrigated_zones}
                if all_complete and zone_names_irrigated == set(self._zones.keys()):
                    self._dryness.reset()
                self._dryness.async_write_ha_state()
                _LOGGER.info(
                    "Irrigation cycle complete. %d zone(s) irrigated",
                    len(irrigated_zones),
                )

        except Exception:
            _LOGGER.exception("Error during irrigation cycle")
            # Safety: close all valves on error
            for zs in self._zones.values():
                if zs.valve:
                    await self._close_valve(zs.valve)
                zs.set_irrigating(False)
        finally:
            self._running = False
            self._active_valve = None

    # ── Delivery mode dispatch ────────────────────────────

    async def _deliver_water(self, zone) -> float:
        """Deliver water to a zone using its configured delivery mode.

        Returns the volume actually delivered in liters (0.0 on failure).
        """
        mode = zone.delivery_mode
        if mode == DELIVERY_MODE_ESTIMATED_FLOW:
            return await self._deliver_estimated_flow(zone)
        if mode == DELIVERY_MODE_FLOW_METER:
            return await self._deliver_flow_meter(zone)
        if mode == DELIVERY_MODE_VOLUME_PRESET:
            return await self._deliver_volume_preset(zone)
        _LOGGER.error("Unknown delivery mode '%s' for zone '%s'", mode, zone.zone_name)
        return 0.0

    async def _deliver_estimated_flow(self, zone) -> float:
        """Open valve, wait calculated duration, close valve."""
        duration = zone.duration_s
        if duration <= 0:
            return 0.0

        await self._open_valve(zone.valve)
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        await self._wait_with_stop_check(duration)

        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        # Estimated flow assumes full delivery if not stopped
        return zone.volume_liters if not self._stop_requested else 0.0

    async def _deliver_volume_preset(self, zone) -> float:
        """Send volume target to smart valve, wait for it to close itself."""
        volume = zone.volume_liters
        if volume <= 0:
            return 0.0

        volume_entity = zone.volume_entity
        if not volume_entity:
            _LOGGER.error("Zone '%s' has no volume_entity configured", zone.zone_name)
            return 0.0

        # Send volume target to the number entity
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": volume_entity, "value": round(volume, 1)},
        )
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        # Wait for the smart valve to finish (monitor switch state)
        timeout = zone.delivery_timeout
        elapsed = 0
        while elapsed < timeout:
            if self._stop_requested:
                await self._close_valve(zone.valve)
                zone.set_irrigating(False)
                zone.async_write_ha_state()
                return 0.0
            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S

            # Check if the valve switch has turned off (valve closed itself)
            valve_state = self._hass.states.get(zone.valve)
            if valve_state and valve_state.state == "off":
                break
        else:
            _LOGGER.warning(
                "Zone '%s' volume_preset timeout (%ds). Forcing valve close.",
                zone.zone_name,
                timeout,
            )
            await self._close_valve(zone.valve)

        zone.set_irrigating(False)
        zone.async_write_ha_state()
        # Smart valve reports it completed, assume full delivery
        return volume

    async def _deliver_flow_meter(self, zone) -> float:
        """Open valve, monitor flow sensor, close when target volume reached.

        Supports two sensor types:
        - Cumulative volume (L): reads difference between start and current
        - Flow rate (L/h, L/min, m³/h): integrates rate over time

        Returns volume actually delivered in liters.
        """
        volume_target = zone.volume_liters
        if volume_target <= 0:
            return 0.0

        meter_entity = zone.flow_meter_sensor
        if not meter_entity:
            _LOGGER.error("Zone '%s' has no flow_meter_sensor configured", zone.zone_name)
            return 0.0

        # Detect sensor type from unit of measurement
        is_rate_sensor = self._is_flow_rate_sensor(meter_entity)

        if is_rate_sensor:
            # Flow rate mode: accumulate volume from rate readings
            return await self._deliver_flow_rate(zone, meter_entity, volume_target)

        # Cumulative volume mode: read difference
        initial_reading = self._read_flow_meter(meter_entity)
        if initial_reading is None:
            _LOGGER.error(
                "Flow meter '%s' unavailable for zone '%s', skipping",
                meter_entity,
                zone.zone_name,
            )
            return 0.0

        await self._open_valve(zone.valve)
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        timeout = zone.delivery_timeout
        elapsed = 0
        delivered = 0.0
        while elapsed < timeout:
            if self._stop_requested:
                await self._close_valve(zone.valve)
                zone.set_irrigating(False)
                zone.async_write_ha_state()
                return delivered

            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S

            current_reading = self._read_flow_meter(meter_entity)
            if current_reading is None:
                _LOGGER.warning(
                    "Flow meter '%s' became unavailable during irrigation of zone '%s'",
                    meter_entity,
                    zone.zone_name,
                )
                continue

            delivered = current_reading - initial_reading
            if delivered < 0:
                _LOGGER.warning("Flow meter reset detected, adjusting baseline")
                initial_reading = 0.0
                delivered = current_reading

            if delivered >= volume_target:
                break
        else:
            _LOGGER.warning(
                "Zone '%s' flow_meter timeout (%ds). Delivered %.1fL of %.1fL target. Closing valve.",
                zone.zone_name,
                timeout,
                delivered,
                volume_target,
            )

        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        return delivered

    async def _deliver_flow_rate(
        self,
        zone,
        meter_entity: str,
        volume_target: float,
    ) -> float:
        """Deliver water by integrating a flow rate sensor over time.

        Returns volume actually delivered in liters.
        """
        await self._open_valve(zone.valve)
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        delivered = 0.0
        timeout = zone.delivery_timeout
        elapsed = 0

        _LOGGER.info(
            "Zone '%s' using flow rate sensor '%s', target=%.1fL",
            zone.zone_name,
            meter_entity,
            volume_target,
        )

        while elapsed < timeout:
            if self._stop_requested:
                await self._close_valve(zone.valve)
                zone.set_irrigating(False)
                zone.async_write_ha_state()
                return delivered

            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S

            rate = self._read_flow_meter(meter_entity)
            if rate is None or rate < 0:
                continue

            # Convert rate to L per poll interval
            unit = self._get_flow_meter_unit(meter_entity)
            if unit in ("L/min", "l/min"):
                delivered += rate / 60 * FLOW_METER_POLL_INTERVAL_S
            elif unit in ("m³/h",):
                delivered += rate * 1000 / 3600 * FLOW_METER_POLL_INTERVAL_S
            else:
                # L/h or default
                delivered += rate / 3600 * FLOW_METER_POLL_INTERVAL_S

            if delivered >= volume_target:
                _LOGGER.info(
                    "Zone '%s' target reached: delivered=%.1fL",
                    zone.zone_name,
                    delivered,
                )
                break
        else:
            _LOGGER.warning(
                "Zone '%s' flow_rate timeout (%ds). Delivered %.1fL of %.1fL target. Closing valve.",
                zone.zone_name,
                timeout,
                delivered,
                volume_target,
            )

        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        return delivered

    def _is_flow_rate_sensor(self, entity_id: str) -> bool:
        """Check if the sensor reports a flow rate (not cumulative volume)."""
        unit = self._get_flow_meter_unit(entity_id)
        return unit in ("L/h", "l/h", "L/min", "l/min", "m³/h")

    def _get_flow_meter_unit(self, entity_id: str) -> str | None:
        """Get the unit of measurement of a flow meter sensor."""
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return state.attributes.get("unit_of_measurement")

    def _read_flow_meter(self, entity_id: str) -> float | None:
        """Read the current value of a flow meter sensor."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    # ── Valve helpers ─────────────────────────────────────

    async def _wait_with_stop_check(self, duration_s: int) -> None:
        """Wait for duration, checking for stop requests every second."""
        for _ in range(duration_s):
            if self._stop_requested:
                return
            await asyncio.sleep(1)

    async def _open_valve(self, entity_id: str) -> None:
        """Turn on a valve switch."""
        self._active_valve = entity_id
        await self._hass.services.async_call("switch", "turn_on", {"entity_id": entity_id})

    async def _close_valve(self, entity_id: str) -> None:
        """Turn off a valve switch."""
        await self._hass.services.async_call("switch", "turn_off", {"entity_id": entity_id})
        if self._active_valve == entity_id:
            self._active_valve = None

    # ── Manual valve detection ───────────────────────────

    @callback
    def _on_valve_state_change(self, event) -> None:
        """Detect manual valve operation (not initiated by controller)."""
        if self._running:
            return  # controller is driving the valve, ignore

        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None or old_state is None:
            return
        if entity_id not in self._valve_to_zone:
            return

        zone_name = self._valve_to_zone[entity_id]
        zone = self._zones.get(zone_name)
        if zone is None:
            return

        if old_state.state == "off" and new_state.state == "on":
            # Valve opened manually — record baseline
            if zone.flow_meter_sensor:
                is_rate = self._is_flow_rate_sensor(zone.flow_meter_sensor)
                if is_rate:
                    # For rate sensors, record open timestamp
                    self._manual_valve_open[entity_id] = time.monotonic()
                else:
                    # For cumulative sensors, record current reading
                    self._manual_valve_open[entity_id] = self._read_flow_meter(zone.flow_meter_sensor)
            else:
                self._manual_valve_open[entity_id] = None
            _LOGGER.info(
                "Manual valve open detected: zone='%s'",
                zone_name,
            )

        elif old_state.state == "on" and new_state.state == "off":
            # Valve closed — compensate deficit
            baseline = self._manual_valve_open.pop(entity_id, None)

            if zone.flow_meter_sensor and baseline is not None:
                is_rate = self._is_flow_rate_sensor(zone.flow_meter_sensor)
                if is_rate:
                    # Estimate volume from average flow rate x duration
                    duration_s = time.monotonic() - baseline
                    current_rate = self._read_flow_meter(zone.flow_meter_sensor)
                    if current_rate is not None and current_rate > 0:
                        unit = self._get_flow_meter_unit(zone.flow_meter_sensor)
                        if unit in ("L/min", "l/min"):
                            delivered_liters = current_rate / 60 * duration_s
                        elif unit in ("m³/h",):
                            delivered_liters = current_rate * 1000 / 3600 * duration_s
                        else:  # L/h or default
                            delivered_liters = current_rate / 3600 * duration_s
                    else:
                        delivered_liters = 0.0
                else:
                    # Cumulative: simple difference
                    flow_end = self._read_flow_meter(zone.flow_meter_sensor)
                    delivered_liters = max(0.0, flow_end - baseline) if flow_end is not None else 0.0

                if delivered_liters > 0 and zone._area > 0:
                    delivered_mm = delivered_liters / zone._area
                    zone._zone_deficit = max(0.0, zone._zone_deficit - delivered_mm * zone._efficiency)
                    _LOGGER.info(
                        "Manual irrigation measured: zone='%s', delivered=%.1fL, new deficit=%.2fmm",
                        zone_name,
                        delivered_liters,
                        zone._zone_deficit,
                    )
                else:
                    zone.reset_deficit()
                    _LOGGER.info(
                        "Manual irrigation detected (flow meter reading zero): zone='%s', deficit reset",
                        zone_name,
                    )
            else:
                # No flow meter — full deficit reset
                zone.reset_deficit()
                _LOGGER.info(
                    "Manual irrigation detected (no flow meter): zone='%s', deficit reset",
                    zone_name,
                )

            zone.async_write_ha_state()
            self._hass.bus.async_fire(
                EVENT_IRRIGATION_COMPLETE,
                {
                    "zone": zone_name,
                    "source": "manual",
                    "deficit_mm": round(zone._zone_deficit, 2),
                },
            )

    # ── Battery monitoring ────────────────────────────────

    @callback
    def _on_battery_change(self, event) -> None:
        """Alert when a valve battery drops below threshold."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        entity_id = event.data.get("entity_id")
        if entity_id not in self._battery_to_zone:
            return

        try:
            level = float(new_state.state)
        except (ValueError, TypeError):
            return

        zone_name = self._battery_to_zone[entity_id]

        if level <= DEFAULT_BATTERY_LOW_THRESHOLD:
            if zone_name not in self._battery_alerted:
                self._battery_alerted.add(zone_name)
                self._hass.async_create_task(
                    self._hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Low battery — irrigation valve",
                            "message": (
                                f"Zone **{zone_name}**: valve battery at {level:.0f}%. "
                                f"Replace batteries soon to avoid irrigation failures."
                            ),
                            "notification_id": f"{DOMAIN}_battery_{zone_name}",
                        },
                    )
                )
                _LOGGER.warning(
                    "Low battery alert: zone='%s', level=%.0f%%",
                    zone_name,
                    level,
                )
        else:
            # Battery recovered (e.g. replaced) — reset alert
            self._battery_alerted.discard(zone_name)

    # ── Deficit anomaly detection ─────────────────────────

    async def _check_deficit_anomaly(self, now=None) -> None:
        """Alert when a zone's deficit is anomalously high (possible malfunction).

        Called every 6 hours in all modes. Alerts once per zone until
        the deficit drops back below the anomaly threshold.
        """
        for zs in self._zones.values():
            threshold = zs.extra_state_attributes.get("threshold_mm", DEFAULT_THRESHOLD)
            anomaly_limit = threshold * ANOMALY_DEFICIT_MULTIPLIER
            zone_deficit = zs._zone_deficit

            if zone_deficit >= anomaly_limit:
                if zs.zone_name not in self._deficit_anomaly_alerted:
                    self._deficit_anomaly_alerted.add(zs.zone_name)
                    await self._hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Anomalous deficit — possible malfunction",
                            "message": (
                                f"Zone **{zs.zone_name}**: deficit {zone_deficit:.1f} mm "
                                f"exceeds {anomaly_limit:.0f} mm "
                                f"({ANOMALY_DEFICIT_MULTIPLIER}\u00d7 threshold). "
                                f"Irrigation may not be working correctly. "
                                f"Check valve, schedule, and HA logs."
                            ),
                            "notification_id": f"{DOMAIN}_anomaly_{zs.zone_name}",
                        },
                    )
                    _LOGGER.warning(
                        "Deficit anomaly: zone='%s', deficit=%.1fmm, limit=%.0fmm",
                        zs.zone_name,
                        zone_deficit,
                        anomaly_limit,
                    )
            else:
                self._deficit_anomaly_alerted.discard(zs.zone_name)

    # ── Monitoring mode (no valves) ──────────────────────

    async def _check_and_notify(self, now=None) -> None:
        """Check per-zone deficits and send notification if irrigation needed.

        Called every 6 hours when no valves are configured (monitoring mode).
        """
        zone_lines = []
        needs_irrigation = False
        for zs in self._zones.values():
            zone_deficit = zs._zone_deficit
            threshold = zs.extra_state_attributes.get("threshold_mm", DEFAULT_THRESHOLD)
            if zone_deficit >= threshold:
                needs_irrigation = True
                zone_lines.append(
                    f"- **{zs.zone_name}**: deficit {zone_deficit:.1f} mm, "
                    f"{zs.volume_liters:.0f} L ({zs.duration_s // 60} min)"
                )

        if not needs_irrigation:
            return

        message = (
            "Your garden needs watering:\n\n" + "\n".join(zone_lines) + "\n\nNo irrigation valves are configured — "
            "please water manually or configure valves in the integration settings."
        )

        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "🌱 Irrigation needed",
                "message": message,
                "notification_id": f"{DOMAIN}_irrigation_alert",
            },
        )
