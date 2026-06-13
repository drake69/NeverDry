"""Tests for the SESSION_RESULT structured INFO log line.

The line is the only stable contract between the integration and any
external post-hoc analysis tool. The format must remain parseable by
``key=value`` splitting on whitespace after the ``SESSION_RESULT``
marker.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

SESSION_RE = re.compile(r"SESSION_RESULT\s+(?P<kv>.+)$")


def parse_session_line(line: str) -> dict[str, str]:
    m = SESSION_RE.search(line)
    assert m, f"no SESSION_RESULT found in: {line!r}"
    pairs = m.group("kv").split()
    out: dict[str, str] = {}
    for p in pairs:
        k, _, v = p.partition("=")
        out[k] = v
    return out


class TestLogSessionResultFormat:
    """Direct exercise of the helper for predictable values."""

    def test_emits_all_required_keys(self, controller, zone_orto, caplog):
        ts_start = datetime(2026, 5, 25, 19, 37, 24)
        ts_end = datetime(2026, 5, 25, 19, 44, 37)

        with caplog.at_level(logging.INFO, logger="never_dry.controller"):
            controller._log_session_result(
                zone_name="Orto",
                zone=zone_orto,
                source="manual",
                ts_start=ts_start,
                ts_end=ts_end,
                volume_target_L=30.0,
                volume_delivered_L=28.8,
                deficit_mm_pre=0.93,
                deficit_mm_post=0.03,
            )

        lines = [r.getMessage() for r in caplog.records if "SESSION_RESULT" in r.getMessage()]
        assert len(lines) == 1
        kv = parse_session_line(lines[0])
        assert kv["zone"] == "Orto"
        assert kv["source"] == "manual"
        assert kv["delivery_mode"] == zone_orto.delivery_mode
        assert kv["duration_s"] == "433.0"
        assert kv["volume_target_L"] == "30.0"
        assert kv["volume_delivered_L"] == "28.8"
        assert kv["deficit_mm_pre"] == "0.93"
        assert kv["deficit_mm_post"] == "0.03"
        assert kv["ts_start"] == ts_start.isoformat()
        assert kv["ts_end"] == ts_end.isoformat()

    def test_null_volume_target(self, controller, zone_orto, caplog):
        # Manual sessions don't have a planned target.
        with caplog.at_level(logging.INFO, logger="never_dry.controller"):
            controller._log_session_result(
                zone_name="Orto",
                zone=zone_orto,
                source="manual",
                ts_start=datetime(2026, 5, 25, 19, 0, 0),
                ts_end=datetime(2026, 5, 25, 19, 0, 30),
                volume_target_L=None,
                volume_delivered_L=4.0,
                deficit_mm_pre=0.5,
                deficit_mm_post=0.3,
            )
        kv = parse_session_line(
            next(r.getMessage() for r in caplog.records if "SESSION_RESULT" in r.getMessage()),
        )
        assert kv["volume_target_L"] == "null"
        assert kv["duration_s"] == "30.0"

    def test_clamps_negative_duration(self, controller, zone_orto, caplog):
        # Defensive: if a caller swaps start/end the duration must not be negative.
        with caplog.at_level(logging.INFO, logger="never_dry.controller"):
            controller._log_session_result(
                zone_name="Orto",
                zone=zone_orto,
                source="manual",
                ts_start=datetime(2026, 5, 25, 19, 0, 30),
                ts_end=datetime(2026, 5, 25, 19, 0, 0),
                volume_target_L=None,
                volume_delivered_L=0.0,
                deficit_mm_pre=0.0,
                deficit_mm_post=0.0,
            )
        kv = parse_session_line(
            next(r.getMessage() for r in caplog.records if "SESSION_RESULT" in r.getMessage()),
        )
        assert kv["duration_s"] == "0.0"


class TestSessionResultIntegration:
    """End-to-end: a real _irrigate_zones cycle emits exactly one line."""

    @pytest.mark.asyncio
    async def test_auto_cycle_emits_session_result(self, controller, zone_orto, caplog):
        zone_orto._zone_deficit = 5.0
        controller._wait_with_stop_check = AsyncMock()

        with caplog.at_level(logging.INFO, logger="never_dry.controller"):
            await controller._irrigate_zones(["Orto"])

        lines = [r.getMessage() for r in caplog.records if "SESSION_RESULT" in r.getMessage()]
        assert len(lines) == 1, f"expected 1 SESSION_RESULT line, got {len(lines)}"
        kv = parse_session_line(lines[0])
        assert kv["zone"] == "Orto"
        assert kv["source"] in ("automatic", "manual")  # set by _current_source if any
        assert float(kv["duration_s"]) >= 0.0
        assert float(kv["volume_delivered_L"]) > 0.0

    def test_manual_open_then_close_emits_session_result(self, controller, hass_mock, zone_orto, caplog):
        # Wire the valve so the controller knows about it.
        controller._valve_to_zone[zone_orto.valve] = zone_orto.zone_name
        controller._zones[zone_orto.zone_name] = zone_orto
        zone_orto._zone_deficit = 1.2

        # Simulate the manual OPEN event.
        open_event = MagicMock()
        open_event.data = {
            "entity_id": zone_orto.valve,
            "new_state": MagicMock(state="on"),
            "old_state": MagicMock(state="off"),
        }
        controller._on_valve_state_change(open_event)

        # Meta captured?
        meta = controller._manual_session_meta.get(zone_orto.valve)
        assert meta is not None
        ts_start, deficit_pre = meta
        assert isinstance(ts_start, datetime)
        assert deficit_pre == pytest.approx(1.2)

        # Simulate the manual CLOSE event.
        close_event = MagicMock()
        close_event.data = {
            "entity_id": zone_orto.valve,
            "new_state": MagicMock(state="off"),
            "old_state": MagicMock(state="on"),
        }
        with caplog.at_level(logging.INFO, logger="never_dry.controller"):
            controller._on_valve_state_change(close_event)

        lines = [r.getMessage() for r in caplog.records if "SESSION_RESULT" in r.getMessage()]
        assert len(lines) == 1
        kv = parse_session_line(lines[0])
        assert kv["zone"] == zone_orto.zone_name
        assert kv["source"] == "manual"
        assert kv["volume_target_L"] == "null"
        # And the meta dict is drained.
        assert zone_orto.valve not in controller._manual_session_meta
