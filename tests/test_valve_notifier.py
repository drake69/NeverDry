"""Tests for valve_notifier — unified persistent_notification bus with dedup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.valve_notifier import (
    _TEMPLATES,
    NotificationKind,
    Severity,
    ValveNotifier,
)

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def hass():
    """Mock HomeAssistant whose services.async_call records every invocation."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


@pytest.fixture
def notifier(hass):
    """Build a fresh ValveNotifier bound to the mock HA."""
    return ValveNotifier(hass)


def _create_calls(hass) -> list[dict]:
    """Return the kwargs of every ``persistent_notification.create`` call."""
    return [
        call.args[2] if len(call.args) >= 3 else call.kwargs.get("service_data", {})
        for call in hass.services.async_call.call_args_list
        if len(call.args) >= 2 and call.args[:2] == ("persistent_notification", "create")
    ]


def _dismiss_calls(hass) -> list[dict]:
    """Return the kwargs of every ``persistent_notification.dismiss`` call."""
    return [
        call.args[2] if len(call.args) >= 3 else call.kwargs.get("service_data", {})
        for call in hass.services.async_call.call_args_list
        if len(call.args) >= 2
        and call.args[:2] == ("persistent_notification", "dismiss")
    ]


# ── Notify ────────────────────────────────────────────────────────────


async def test_notify_creates_persistent_notification(notifier, hass):
    """A fresh notify call calls persistent_notification.create."""
    created = await notifier.notify(
        zone="Orto",
        kind=NotificationKind.COMMAND_FAILED,
        context={"operation": "open", "error_detail": "OPEN_FAILED"},
    )
    assert created is True
    calls = _create_calls(hass)
    assert len(calls) == 1
    payload = calls[0]
    assert payload["notification_id"] == "never_dry_orto_command_failed"
    assert "Valve command failed" in payload["title"]
    assert "Orto" in payload["message"]
    assert "OPEN_FAILED" in payload["message"]


async def test_notify_includes_severity_in_title(notifier, hass):
    """The severity tag is prefixed to the title."""
    await notifier.notify(
        zone="Prato",
        kind=NotificationKind.STUCK_OPEN,
        severity=Severity.CRITICAL,
        context={"flow": "0.4 L/min"},
    )
    payload = _create_calls(hass)[0]
    assert payload["title"].startswith("[CRITICAL]")


async def test_notify_deduplicates_same_context(notifier, hass):
    """A second notify with identical context is a no-op."""
    ctx = {"operation": "close", "error_detail": "CLOSE_LEAK"}
    await notifier.notify("Orto", NotificationKind.COMMAND_FAILED, context=ctx)
    await notifier.notify("Orto", NotificationKind.COMMAND_FAILED, context=ctx)
    assert len(_create_calls(hass)) == 1


async def test_notify_updates_on_different_context(notifier, hass):
    """A notify with a changed context re-creates (HA overwrites)."""
    await notifier.notify(
        "Orto",
        NotificationKind.COMMAND_FAILED,
        context={"operation": "open", "error_detail": "OPEN_FAILED"},
    )
    await notifier.notify(
        "Orto",
        NotificationKind.COMMAND_FAILED,
        context={"operation": "open", "error_detail": "CLOSE_VERIFICATION_FAILED"},
    )
    calls = _create_calls(hass)
    assert len(calls) == 2
    assert calls[0]["notification_id"] == calls[1]["notification_id"]


async def test_notify_updates_on_severity_change(notifier, hass):
    """Same context but a higher severity must re-create."""
    ctx = {"flow": "0.3 L/min"}
    await notifier.notify("Orto", NotificationKind.STUCK_OPEN, Severity.WARNING, ctx)
    await notifier.notify("Orto", NotificationKind.STUCK_OPEN, Severity.CRITICAL, ctx)
    assert len(_create_calls(hass)) == 2


async def test_notify_returns_false_on_dedup(notifier):
    """The second call with identical payload returns ``False``."""
    ctx = {"operation": "open", "error_detail": "OPEN_FAILED"}
    first = await notifier.notify("Orto", NotificationKind.COMMAND_FAILED, context=ctx)
    second = await notifier.notify("Orto", NotificationKind.COMMAND_FAILED, context=ctx)
    assert first is True
    assert second is False


async def test_notify_missing_context_key_does_not_crash(notifier, hass, caplog):
    """A missing placeholder logs an error but still creates a notification."""
    created = await notifier.notify(
        "Orto",
        NotificationKind.STUCK_OPEN,
        context={},  # missing 'flow'
    )
    assert created is True
    assert "missing context key" in caplog.text


# ── Clear ────────────────────────────────────────────────────────────


async def test_clear_dismisses_active(notifier, hass):
    """``clear`` dismisses the matching notification."""
    await notifier.notify(
        "Orto",
        NotificationKind.COMMAND_FAILED,
        context={"operation": "open", "error_detail": "OPEN_FAILED"},
    )
    cleared = await notifier.clear("Orto", NotificationKind.COMMAND_FAILED)
    assert cleared is True
    dismissed = _dismiss_calls(hass)
    assert len(dismissed) == 1
    assert dismissed[0] == {"notification_id": "never_dry_orto_command_failed"}
    assert notifier.is_active("Orto", NotificationKind.COMMAND_FAILED) is False


async def test_clear_returns_false_when_nothing_to_clear(notifier, hass):
    """``clear`` on an unknown key is a no-op returning False."""
    cleared = await notifier.clear("Ghost", NotificationKind.LEAK_DETECTED)
    assert cleared is False
    assert _dismiss_calls(hass) == []


async def test_clear_zone_clears_every_kind_for_zone(notifier, hass):
    """``clear_zone`` dismisses all notifications scoped to a given zone."""
    await notifier.notify(
        "Orto",
        NotificationKind.COMMAND_FAILED,
        context={"operation": "open", "error_detail": "OPEN_FAILED"},
    )
    await notifier.notify(
        "Orto",
        NotificationKind.STUCK_OPEN,
        context={"flow": "0.2 L/min"},
    )
    await notifier.notify(
        "Prato",
        NotificationKind.COMMAND_FAILED,
        context={"operation": "close", "error_detail": "CLOSE_VERIFICATION_FAILED"},
    )
    n = await notifier.clear_zone("Orto")
    assert n == 2
    assert notifier.is_active("Orto", NotificationKind.COMMAND_FAILED) is False
    assert notifier.is_active("Orto", NotificationKind.STUCK_OPEN) is False
    assert notifier.is_active("Prato", NotificationKind.COMMAND_FAILED) is True


async def test_clear_all_clears_everything(notifier):
    """``clear_all`` empties the notifier."""
    await notifier.notify(
        "Orto",
        NotificationKind.BATTERY_LOW,
        context={"sensor_name": "valve_orto", "percent": 12},
    )
    await notifier.notify(
        "Prato",
        NotificationKind.LEAK_DETECTED,
        context={"flow": "0.3 L/min"},
    )
    n = await notifier.clear_all()
    assert n == 2
    assert notifier.active_keys() == []


# ── Notification id ──────────────────────────────────────────────────


async def test_notification_id_sanitises_zone(notifier, hass):
    """Spaces and unicode in zone names collapse to underscores."""
    await notifier.notify(
        "Giardino davanti!",
        NotificationKind.WATER_ME_NOW,
        context={"deficit": "12.0mm"},
    )
    payload = _create_calls(hass)[0]
    assert payload["notification_id"] == "never_dry_giardino_davanti_water_me_now"


async def test_notification_id_falls_back_for_empty_zone(notifier, hass):
    """An empty or whitespace zone falls back to ``"global"``."""
    await notifier.notify(
        "",
        NotificationKind.LEAK_DETECTED,
        context={"flow": "0.5 L/min"},
    )
    payload = _create_calls(hass)[0]
    assert payload["notification_id"] == "never_dry_global_leak_detected"


# ── Templates coverage ──────────────────────────────────────────────


def test_every_kind_has_a_template():
    """Every NotificationKind must have a registered template."""
    for kind in NotificationKind:
        assert kind in _TEMPLATES
        tpl = _TEMPLATES[kind]
        assert tpl.title
        assert tpl.body


@pytest.mark.parametrize(
    ("kind", "context"),
    [
        (
            NotificationKind.COMMAND_FAILED,
            {"operation": "open", "error_detail": "OPEN_FAILED"},
        ),
        (NotificationKind.UNREACHABLE_PASSIVE, {"duration": "6h"}),
        (NotificationKind.UNREACHABLE_AT_IRRIGATION, {}),
        (NotificationKind.FLOW_METER_DEAD, {}),
        (NotificationKind.STUCK_OPEN, {"flow": "0.5 L/min"}),
        (NotificationKind.LEAK_DETECTED, {"flow": "0.3 L/min"}),
        (NotificationKind.ZONE_DISABLED, {"failures": 3}),
        (NotificationKind.BATTERY_LOW, {"sensor_name": "valve_orto", "percent": 10}),
        (NotificationKind.IRRIGATION_INEFFECTIVE, {}),
        (NotificationKind.MODEL_DRIFT, {"correlation": 0.42}),
        (NotificationKind.WATER_ME_NOW, {"deficit": "12.0mm"}),
    ],
)
async def test_every_kind_renders_with_supplied_context(notifier, hass, kind, context):
    """Every kind formats its template with the documented context keys."""
    created = await notifier.notify("Orto", kind, context=context)
    assert created is True
    assert len(_create_calls(hass)) == 1
