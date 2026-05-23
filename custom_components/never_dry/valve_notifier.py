"""Unified valve-notifier subsystem.

Single API consumed by every subsystem that needs to surface a
condition to the user: valve operations (AI-029), flow-rate model
analysis (AI-040), indoor zone logic (AI-046), zone-health (AI-035).

The notifier sits on top of Home Assistant's ``persistent_notification``
service and adds two essential behaviours:

1. **Deduplication.** A second ``notify(zone, kind, ...)`` with the same
   context is a no-op. A second call with a *different* context updates
   the existing notification in place (HA's ``create`` with the same
   ``notification_id`` overwrites).
2. **Auto-clear.** ``clear(zone, kind)`` dismisses the notification once
   the condition resolves, so the user does not have to dismiss it
   manually.

Notification messages are built from per-kind templates formatted with
the ``context`` dict supplied by the caller.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


# ── Enums ─────────────────────────────────────────────────────────────


class NotificationKind(StrEnum):
    """The fixed catalogue of conditions the notifier can surface."""

    COMMAND_FAILED = "command_failed"
    UNREACHABLE_PASSIVE = "unreachable_passive"
    UNREACHABLE_AT_IRRIGATION = "unreachable_at_irrigation"
    FLOW_METER_DEAD = "flow_meter_dead"
    STUCK_OPEN = "stuck_open"
    LEAK_DETECTED = "leak_detected"
    ZONE_DISABLED = "zone_disabled"
    BATTERY_LOW = "battery_low"
    IRRIGATION_INEFFECTIVE = "irrigation_ineffective"
    MODEL_DRIFT = "model_drift"
    WATER_ME_NOW = "water_me_now"


class Severity(StrEnum):
    """Severity tier carried on every notification."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ── Templates ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Template:
    """Static title + body template for one :class:`NotificationKind`."""

    title: str
    body: str


_TEMPLATES: dict[NotificationKind, _Template] = {
    NotificationKind.COMMAND_FAILED: _Template(
        title="Valve command failed",
        body="Zone '{zone}': {operation} failed ({error_detail}).",
    ),
    NotificationKind.UNREACHABLE_PASSIVE: _Template(
        title="Valve unreachable",
        body="Zone '{zone}' valve has been unavailable for {duration}.",
    ),
    NotificationKind.UNREACHABLE_AT_IRRIGATION: _Template(
        title="Valve unreachable at irrigation time",
        body=(
            "Could not start irrigation for zone '{zone}': valve unavailable "
            "({reason}). Check the valve battery / Zigbee mesh before retrying."
        ),
    ),
    NotificationKind.FLOW_METER_DEAD: _Template(
        title="Flow meter unavailable",
        body="Zone '{zone}' flow meter became unavailable during irrigation.",
    ),
    NotificationKind.STUCK_OPEN: _Template(
        title="Valve stuck open — shut off mains water",
        body=(
            "Zone '{zone}' valve switch reports OFF but water is still flowing "
            "({flow}). The integration has already attempted recovery and "
            "triggered an emergency stop for every other zone. "
            "**Manually shut off your main water valve now** and inspect "
            "the affected valve before resuming irrigation."
        ),
    ),
    NotificationKind.LEAK_DETECTED: _Template(
        title="Leak detected",
        body="Flow detected while every valve is closed. Last reading: {flow}.",
    ),
    NotificationKind.ZONE_DISABLED: _Template(
        title="Zone disabled",
        body="Zone '{zone}' auto-disabled after {failures} consecutive failures.",
    ),
    NotificationKind.BATTERY_LOW: _Template(
        title="Battery low",
        body="Battery for {sensor_name} is at {percent}%.",
    ),
    NotificationKind.IRRIGATION_INEFFECTIVE: _Template(
        title="Irrigation appears ineffective",
        body="Zone '{zone}': soil moisture did not rise after the last irrigation.",
    ),
    NotificationKind.MODEL_DRIFT: _Template(
        title="Model drift detected",
        body="Zone '{zone}': moisture/model correlation has dropped to {correlation}.",
    ),
    NotificationKind.WATER_ME_NOW: _Template(
        title="Manual irrigation suggested",
        body="Zone '{zone}': deficit {deficit} above threshold; water by hand.",
    ),
}


# ── Internal entry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ActiveNotification:
    """Snapshot of an active notification used for dedup and inspection."""

    zone: str
    kind: NotificationKind
    severity: Severity
    notification_id: str
    title: str
    message: str
    context: dict
    created_at: datetime = field(default_factory=datetime.now)


# ── Public notifier ───────────────────────────────────────────────────


class ValveNotifier:
    """User-facing notification bus for NeverDry conditions.

    One instance per integration. Maintains a ``(zone, kind) → notification``
    map and proxies create / dismiss to Home Assistant's
    ``persistent_notification`` service.
    """

    _DOMAIN: ClassVar[str] = "persistent_notification"
    _ID_PREFIX: ClassVar[str] = "never_dry"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the notifier to a Home Assistant instance."""
        self._hass = hass
        self._active: dict[tuple[str, NotificationKind], _ActiveNotification] = {}

    # ── Public API ───────────────────────────────────────────────────

    async def notify(
        self,
        zone: str,
        kind: NotificationKind,
        severity: Severity = Severity.WARNING,
        context: dict | None = None,
    ) -> bool:
        """Surface a notification, deduplicating against active ones.

        Returns ``True`` when the call created or updated a notification,
        ``False`` when the call was deduplicated (same zone, kind and
        context as the currently active one).
        """
        ctx = dict(context or {})
        ctx.setdefault("zone", zone)
        template = _TEMPLATES[kind]
        try:
            message = template.body.format(**ctx)
        except KeyError as missing:
            _LOGGER.error(
                "Notification %s/%s missing context key %s; using raw template",
                zone,
                kind.value,
                missing,
            )
            message = template.body

        key = (zone, kind)
        existing = self._active.get(key)
        if existing and existing.context == ctx and existing.severity == severity:
            return False

        notification_id = self._notification_id(zone, kind)
        title = f"[{severity.value.upper()}] {template.title}"
        await self._hass.services.async_call(
            self._DOMAIN,
            "create",
            {
                "notification_id": notification_id,
                "title": title,
                "message": message,
            },
            blocking=False,
        )
        self._active[key] = _ActiveNotification(
            zone=zone,
            kind=kind,
            severity=severity,
            notification_id=notification_id,
            title=title,
            message=message,
            context=ctx,
        )
        return True

    async def clear(self, zone: str, kind: NotificationKind) -> bool:
        """Dismiss the notification for ``(zone, kind)``.

        Returns ``True`` when something was dismissed, ``False`` when no
        active notification matched.
        """
        key = (zone, kind)
        entry = self._active.pop(key, None)
        if entry is None:
            return False
        await self._hass.services.async_call(
            self._DOMAIN,
            "dismiss",
            {"notification_id": entry.notification_id},
            blocking=False,
        )
        return True

    async def clear_zone(self, zone: str) -> int:
        """Dismiss every active notification for ``zone``.

        Returns the number of notifications dismissed.
        """
        keys = [k for k in self._active if k[0] == zone]
        for _, kind in keys:
            await self.clear(zone, kind)
        return len(keys)

    async def clear_all(self) -> int:
        """Dismiss every notification owned by this notifier."""
        keys = list(self._active)
        for zone, kind in keys:
            await self.clear(zone, kind)
        return len(keys)

    def is_active(self, zone: str, kind: NotificationKind) -> bool:
        """Return ``True`` if a notification is currently active for ``(zone, kind)``."""
        return (zone, kind) in self._active

    def active_keys(self) -> list[tuple[str, NotificationKind]]:
        """Return a snapshot list of active ``(zone, kind)`` pairs."""
        return list(self._active.keys())

    # ── Internals ────────────────────────────────────────────────────

    @classmethod
    def _notification_id(cls, zone: str, kind: NotificationKind) -> str:
        """Build a deterministic notification id for ``(zone, kind)``."""
        safe_zone = re.sub(r"\W+", "_", zone.strip().lower()).strip("_") or "global"
        return f"{cls._ID_PREFIX}_{safe_zone}_{kind.value}"
