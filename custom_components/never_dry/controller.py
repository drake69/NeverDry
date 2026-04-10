"""Irrigation controller for the NeverDry integration.

Manages valve on/off cycles directly. Given a zone (or all zones),
the controller:
  1. Opens the valve
  2. Waits for the calculated duration (based on deficit, area, flow rate)
  3. Closes the valve
  4. Resets the deficit

Zones are irrigated sequentially to avoid pressure drops.
An emergency stop service closes all valves immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ATTR_ZONE_NAME,
    DEFAULT_INTER_ZONE_DELAY,
    DEFAULT_THRESHOLD,
    DOMAIN,
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
                duration = zone.duration_s
                valve = zone.valve

                if not valve:
                    _LOGGER.warning("Zone '%s' has no valve configured, skipping", zone_name)
                    continue

                if duration <= 0:
                    _LOGGER.info(
                        "Zone '%s' needs 0s irrigation (deficit=%.1fmm), skipping",
                        zone_name,
                        zone._zone_deficit,
                    )
                    continue

                _LOGGER.info(
                    "Starting irrigation: zone='%s', valve='%s', duration=%ds, volume=%.1fL, deficit=%.1fmm",
                    zone_name,
                    valve,
                    duration,
                    zone.volume_liters,
                    zone._zone_deficit,
                )

                # Open valve
                await self._open_valve(valve)
                zone.set_irrigating(True)
                zone.async_write_ha_state()

                # Wait for calculated duration
                await self._wait_with_stop_check(duration)

                # Close valve
                await self._close_valve(valve)
                zone.set_irrigating(False)
                zone.async_write_ha_state()

                if self._stop_requested:
                    break

                irrigated_zones.append(zone_name)

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
