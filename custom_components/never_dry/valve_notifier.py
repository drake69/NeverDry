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
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# ── Enums ─────────────────────────────────────────────────────────────


class NotificationKind(StrEnum):
    """The fixed catalogue of conditions the notifier can surface."""

    COMMAND_FAILED = "command_failed"
    UNREACHABLE_PASSIVE = "unreachable_passive"
    UNREACHABLE_AT_IRRIGATION = "unreachable_at_irrigation"
    FLOW_METER_DEAD = "flow_meter_dead"
    # Same fault, different consequence, so a different condition rather than a
    # sentence passed in by the caller: the volume could not be estimated
    # either, and the zone's deficit is left standing.
    DELIVERY_UNCREDITED = "delivery_uncredited"
    STUCK_OPEN = "stuck_open"
    LEAK_DETECTED = "leak_detected"
    ZONE_DISABLED = "zone_disabled"
    BATTERY_LOW = "battery_low"
    IRRIGATION_INEFFECTIVE = "irrigation_ineffective"
    MODEL_DRIFT = "model_drift"
    WATER_ME_NOW = "water_me_now"
    WATCHDOG_TRIGGERED = "watchdog_triggered"


class Severity(StrEnum):
    """Severity tier carried on every notification."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ── Text ────────────────────────────────────────────────────

# The title and body of every notification live in the translation catalogue, under
# ``common``, as ``notification_<kind>_title`` and ``notification_<kind>_body``.
#
# ``common`` rather than ``issues`` on purpose. A repair issue is a statement about the
# *installation*: a setting is deprecated, a credential expired, go and check something in
# your configuration. What arrives here is a statement about the *garden*: a valve is stuck
# open and water is running. Filing the second under the catalogue built for the first would
# make the text translatable at the cost of saying something untrue about what it is, and it
# would put "shut off your mains" on the same page as housekeeping. The condition itself
# belongs in an entity the user can automate on; this catalogue only holds the words.
#
# The category is not free choice either: hassfest validates ``strings.json`` against a
# closed schema, so a ``notifications`` key of our own fails CI. ``common`` is the neutral
# bucket that schema does provide.
_TEXT_PREFIX = f"component.{DOMAIN}.common.notification_"


async def _resolve(hass: HomeAssistant, kind: NotificationKind) -> tuple[str, str]:
    """Title and body for one kind, in the user's language.

    Home Assistant loads English first and lays the requested language over it, so a key a
    translator has not reached yet degrades to English rather than to nothing. A key missing
    everywhere would degrade to the raw identifier, which is why there is a test that will not
    let one exist.
    """
    resources = await async_get_translations(hass, hass.config.language, "common", {DOMAIN})
    return (
        resources.get(f"{_TEXT_PREFIX}{kind.value}_title", kind.value),
        resources.get(f"{_TEXT_PREFIX}{kind.value}_body", ""),
    )


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
        title_text, body_text = await _resolve(self._hass, kind)
        try:
            message = body_text.format(**ctx)
        except KeyError as missing:
            _LOGGER.error(
                "Notification %s/%s missing context key %s; using raw template",
                zone,
                kind.value,
                missing,
            )
            message = body_text

        key = (zone, kind)
        existing = self._active.get(key)
        if existing and existing.context == ctx and existing.severity == severity:
            return False

        notification_id = self._notification_id(zone, kind)
        title = f"[{severity.value.upper()}] {title_text}"
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
