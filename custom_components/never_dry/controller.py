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
    async_track_time_interval,
)

from .const import (
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
        self._last_service_call: float = 0.0
        # Manual valve tracking: valve_entity_id → flow meter reading at valve open
        self._manual_valve_open: dict[str, float | None] = {}
        # Reverse map: valve_entity_id → zone_name
        self._valve_to_zone: dict[str, str] = {
            zs.valve: zs.zone_name for zs in zone_sensors if zs.valve
        }
        # Battery sensor → zone_name map
        self._battery_to_zone: dict[str, str] = {
            zs.battery_sensor: zs.zone_name
            for zs in zone_sensors
            if zs.battery_sensor
        }
        # Track which zones have already been alerted for low battery
        self._battery_alerted: set[str] = set()

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
            async_track_state_change_event(
                self._hass, valve_entities, self._on_valve_state_change
            )

        # Monitor battery sensors for low-battery alerts
        battery_entities = [b for b in self._battery_to_zone if b]
        if battery_entities:
            async_track_state_change_event(
                self._hass, battery_entities, self._on_battery_change
            )

        # Start monitoring mode if no valves are configured
        if self._monitoring_mode:
            from datetime import timedelta

            _LOGGER.info(
                "No valves configured — running in monitoring mode. "
                "Irrigation alerts will be sent every 6 hours when needed."
            )
            self._unsub_monitor = async_track_time_interval(
                self._hass,
                self._check_and_notify,
                timedelta(hours=6),
            )

    # ── Rate limiting ──────────────────────────────────────

    def _is_throttled(self, service_name: str) -> bool:
        """Return True if a service call should be rejected (rate limit)."""
        now = time.monotonic()
        elapsed = now - self._last_service_call
        if elapsed < MIN_SERVICE_INTERVAL_S:
            _LOGGER.warning(
                "Service %s throttled — %0.1fs since last call (min %ds)",
                service_name,
                elapsed,
                MIN_SERVICE_INTERVAL_S,
            )
            return True
        self._last_service_call = now
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
        if self._is_throttled("irrigate_zone"):
            return
        zone_name = call.data.get(ATTR_ZONE_NAME)
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
        if self._is_throttled("mark_irrigated"):
            return
        zone_name = call.data.get(ATTR_ZONE_NAME)
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

                success = await self._deliver_water(zone)

                if self._stop_requested:
                    break

                if success:
                    volume = zone.volume_liters
                    duration = zone.duration_s
                    irrigated_zones.append(zone_name)
                    self._hass.bus.async_fire(
                        EVENT_IRRIGATION_COMPLETE,
                        {
                            "zone": zone_name,
                            "source": "automatic",
                            "volume_liters": round(volume, 1),
                            "duration_s": duration,
                            "deficit_mm": round(zone._zone_deficit, 2),
                        },
                    )
                    _LOGGER.info("Completed irrigation: zone='%s'", zone_name)

                # Inter-zone delay (pressure stabilization)
                if i < len(zone_names) - 1 and not self._stop_requested:
                    _LOGGER.debug("Inter-zone delay: %ds", self._inter_zone_delay)
                    await asyncio.sleep(self._inter_zone_delay)

            # Reset deficits for irrigated zones
            if irrigated_zones and not self._stop_requested:
                for zone_name in irrigated_zones:
                    self._zones[zone_name].reset_deficit()
                    self._zones[zone_name].async_write_ha_state()
                # Reset reference sensor only if ALL zones were irrigated
                if set(irrigated_zones) == set(self._zones.keys()):
                    self._dryness.reset()
                self._dryness.async_write_ha_state()
                _LOGGER.info(
                    "Irrigation cycle complete. %d zone(s) irrigated, zone deficits reset",
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

    async def _deliver_water(self, zone) -> bool:
        """Deliver water to a zone using its configured delivery mode.

        Returns True if delivery completed successfully.
        """
        mode = zone.delivery_mode
        if mode == DELIVERY_MODE_ESTIMATED_FLOW:
            return await self._deliver_estimated_flow(zone)
        if mode == DELIVERY_MODE_FLOW_METER:
            return await self._deliver_flow_meter(zone)
        if mode == DELIVERY_MODE_VOLUME_PRESET:
            return await self._deliver_volume_preset(zone)
        _LOGGER.error("Unknown delivery mode '%s' for zone '%s'", mode, zone.zone_name)
        return False

    async def _deliver_estimated_flow(self, zone) -> bool:
        """Open valve, wait calculated duration, close valve."""
        duration = zone.duration_s
        if duration <= 0:
            return False

        await self._open_valve(zone.valve)
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        await self._wait_with_stop_check(duration)

        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        return not self._stop_requested

    async def _deliver_volume_preset(self, zone) -> bool:
        """Send volume target to smart valve, wait for it to close itself."""
        volume = zone.volume_liters
        if volume <= 0:
            return False

        volume_entity = zone.volume_entity
        if not volume_entity:
            _LOGGER.error("Zone '%s' has no volume_entity configured", zone.zone_name)
            return False

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
                return False
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
        return True

    async def _deliver_flow_meter(self, zone) -> bool:
        """Open valve, monitor flow sensor, close when target volume reached."""
        volume_target = zone.volume_liters
        if volume_target <= 0:
            return False

        meter_entity = zone.flow_meter_sensor
        if not meter_entity:
            _LOGGER.error("Zone '%s' has no flow_meter_sensor configured", zone.zone_name)
            return False

        # Read initial meter value
        initial_reading = self._read_flow_meter(meter_entity)
        if initial_reading is None:
            _LOGGER.error(
                "Flow meter '%s' unavailable for zone '%s', skipping",
                meter_entity,
                zone.zone_name,
            )
            return False

        await self._open_valve(zone.valve)
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        timeout = zone.delivery_timeout
        elapsed = 0
        while elapsed < timeout:
            if self._stop_requested:
                await self._close_valve(zone.valve)
                zone.set_irrigating(False)
                zone.async_write_ha_state()
                return False

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
                # Meter reset during irrigation — use current reading as new baseline
                _LOGGER.warning("Flow meter reset detected, adjusting baseline")
                initial_reading = 0.0
                delivered = current_reading

            if delivered >= volume_target:
                break
        else:
            _LOGGER.warning(
                "Zone '%s' flow_meter timeout (%ds). Closing valve.",
                zone.zone_name,
                timeout,
            )

        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        return not self._stop_requested

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
            # Valve opened manually — record flow meter baseline if available
            flow_start = None
            if zone.flow_meter_sensor:
                flow_start = self._read_flow_meter(zone.flow_meter_sensor)
            self._manual_valve_open[entity_id] = flow_start
            _LOGGER.info(
                "Manual valve open detected: zone='%s', flow_start=%s",
                zone_name,
                flow_start,
            )

        elif old_state.state == "on" and new_state.state == "off":
            # Valve closed — compensate deficit
            flow_start = self._manual_valve_open.pop(entity_id, None)

            if zone.flow_meter_sensor and flow_start is not None:
                flow_end = self._read_flow_meter(zone.flow_meter_sensor)
                if flow_end is not None:
                    delivered_liters = max(0.0, flow_end - flow_start)
                    # Convert liters to mm: mm = L / area_m2
                    if zone._area > 0:
                        delivered_mm = delivered_liters / zone._area
                        zone._zone_deficit = max(0.0, zone._zone_deficit - delivered_mm * zone._efficiency)
                    _LOGGER.info(
                        "Manual irrigation measured: zone='%s', delivered=%.1fL, "
                        "new deficit=%.2fmm",
                        zone_name,
                        delivered_liters,
                        zone._zone_deficit,
                    )
                else:
                    zone.reset_deficit()
                    _LOGGER.info(
                        "Manual irrigation detected (flow meter unavailable at close): "
                        "zone='%s', deficit reset",
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
