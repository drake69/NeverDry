"""Tests for valve_latency — rolling latency windows and adaptive timeouts."""

from __future__ import annotations

import asyncio
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from never_dry.valve_latency import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    MIN_SAMPLES,
    MIN_TIMEOUT_S,
    SIGMA,
    WINDOW_SIZE,
    LatencyWindow,
    ValveLatencyTracker,
)

# ── LatencyWindow ─────────────────────────────────────────────────────


def test_adaptive_timeout_default_below_min_samples():
    """Returns DEFAULT_TIMEOUT_S when fewer than MIN_SAMPLES collected."""
    w = LatencyWindow()
    for _ in range(MIN_SAMPLES - 1):
        w.record(500.0)
    assert w.adaptive_timeout_s() == DEFAULT_TIMEOUT_S


def test_adaptive_timeout_with_sufficient_samples():
    """adaptive_timeout_s = clamp(mean + 3*sigma / 1000, MIN, MAX)."""
    w = LatencyWindow()
    samples = [200.0] * 10  # mean=200, std=0 -> raw=200ms=0.2s -> clamped to MIN
    for s in samples:
        w.record(s)
    result = w.adaptive_timeout_s()
    expected_raw = (200.0 + SIGMA * 0.0) / 1000.0
    expected = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, expected_raw))
    assert abs(result - expected) < 0.01


def test_adaptive_timeout_clamped_to_min():
    """Very fast responses clamp to MIN_TIMEOUT_S."""
    w = LatencyWindow()
    for _ in range(10):
        w.record(1.0)  # 1 ms -> mean+3*sigma ~= 0.001 s -> below MIN
    assert w.adaptive_timeout_s() == MIN_TIMEOUT_S


def test_adaptive_timeout_clamped_to_max():
    """Very slow responses clamp to MAX_TIMEOUT_S."""
    w = LatencyWindow()
    for _ in range(10):
        w.record(50_000.0)  # 50 s each -> well above MAX
    assert w.adaptive_timeout_s() == MAX_TIMEOUT_S


def test_adaptive_timeout_varied_samples():
    """Mean + 3*sigma formula holds for a realistic spread."""
    w = LatencyWindow()
    samples = [100.0, 120.0, 150.0, 110.0, 130.0, 90.0, 200.0, 105.0, 115.0, 125.0]
    for s in samples:
        w.record(s)
    mean = sum(samples) / len(samples)
    std = math.sqrt(sum((x - mean) ** 2 for x in samples) / len(samples))
    expected_s = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, (mean + SIGMA * std) / 1000.0))
    assert abs(w.adaptive_timeout_s() - expected_s) < 0.001


def test_window_rolls_over_max_size():
    """Only the last WINDOW_SIZE samples are retained."""
    w = LatencyWindow()
    for i in range(WINDOW_SIZE + 5):
        w.record(float(i))
    assert len(w._samples) == WINDOW_SIZE


def test_as_dict_empty():
    """as_dict on an empty window returns sample_count=0 and default timeout."""
    d = LatencyWindow().as_dict()
    assert d["sample_count"] == 0
    assert d["adaptive_timeout_s"] == DEFAULT_TIMEOUT_S


def test_as_dict_populated():
    """as_dict includes mean, std, p95, min, max, and adaptive_timeout_s."""
    w = LatencyWindow()
    samples = [100.0, 200.0, 300.0, 400.0, 500.0]
    for s in samples:
        w.record(s)
    d = w.as_dict()
    assert d["sample_count"] == 5
    assert d["min_ms"] == 100.0
    assert d["max_ms"] == 500.0
    assert "mean_ms" in d
    assert "std_ms" in d
    assert "p95_ms" in d
    assert "adaptive_timeout_s" in d


def test_as_dict_p95():
    """p95 is the 95th percentile of the sorted samples."""
    w = LatencyWindow()
    for i in range(1, 21):  # 1..20
        w.record(float(i * 10))
    d = w.as_dict()
    # 95th percentile of 20 samples -> index ceil(0.95*20)-1 = 18 -> value 190
    assert d["p95_ms"] == 190.0


# ── ValveLatencyTracker ───────────────────────────────────────────────


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    return store


@pytest.fixture
def hass():
    h = MagicMock()
    h.async_create_task = lambda coro: asyncio.ensure_future(coro)
    return h


@pytest.fixture
def tracker(hass, mock_store):
    with patch("never_dry.valve_latency.Store", return_value=mock_store):
        t = ValveLatencyTracker(hass, "switch.zone1_valve")
    return t, mock_store


async def test_tracker_load_empty(tracker):
    t, _ = tracker
    await t.async_load()
    assert len(t.open._samples) == 0
    assert len(t.close._samples) == 0


async def test_tracker_load_persisted_samples(hass, mock_store):
    mock_store.async_load = AsyncMock(
        return_value={"open": [100.0, 200.0, 150.0], "close": [50.0, 60.0]}
    )
    with patch("never_dry.valve_latency.Store", return_value=mock_store):
        t = ValveLatencyTracker(hass, "switch.valve")
    await t.async_load()
    assert list(t.open._samples) == [100.0, 200.0, 150.0]
    assert list(t.close._samples) == [50.0, 60.0]


async def test_tracker_save(tracker):
    t, store = tracker
    t.open.record(120.0)
    t.close.record(80.0)
    await t.async_save()
    store.async_save.assert_awaited_once_with(
        {"open": [120.0], "close": [80.0]}
    )


def test_tracker_open_timeout_s_default(tracker):
    t, _ = tracker
    assert t.open_timeout_s() == DEFAULT_TIMEOUT_S


def test_tracker_close_timeout_s_default(tracker):
    t, _ = tracker
    assert t.close_timeout_s() == DEFAULT_TIMEOUT_S


def test_tracker_as_dict(tracker):
    t, _ = tracker
    d = t.as_dict()
    assert "open" in d
    assert "close" in d
    assert d["open"]["sample_count"] == 0
    assert d["close"]["sample_count"] == 0


def test_tracker_open_timeout_adaptive_after_samples(tracker):
    """After enough open samples, open_timeout_s diverges from default."""
    t, _ = tracker
    for _ in range(MIN_SAMPLES):
        t.open.record(300.0)
    timeout = t.open_timeout_s()
    assert timeout != DEFAULT_TIMEOUT_S
    assert MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S


def test_storage_key_sanitises_entity_id(hass):
    """Dots and slashes in entity_id are replaced in the storage key."""
    captured_key = []

    def fake_store(h, version, key):
        captured_key.append(key)
        s = MagicMock()
        s.async_load = AsyncMock(return_value=None)
        s.async_save = AsyncMock()
        return s

    with patch("never_dry.valve_latency.Store", side_effect=fake_store):
        ValveLatencyTracker(hass, "switch.zone/1")

    assert "." not in captured_key[0].split("never_dry.latency.")[1]
    assert "/" not in captured_key[0]
