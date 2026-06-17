"""Rolling latency statistics for valve confirmation response time.

Measures the time between a CMD_OPEN/CMD_CLOSE dispatch and the matching
hardware confirmation (OBS_SWITCH_ON / OBS_SWITCH_OFF).  Builds a rolling
window of up to WINDOW_SIZE samples and computes mean + 3σ as the adaptive
timeout fed back into the FSM timer, replacing the fixed 10 s default.

Samples are persisted to HA storage so the statistical model survives
integration reloads and HA restarts.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

WINDOW_SIZE: int = 20
MIN_SAMPLES: int = 3
SIGMA: float = 3.0
DEFAULT_TIMEOUT_S: float = 10.0
MIN_TIMEOUT_S: float = 2.0
MAX_TIMEOUT_S: float = 30.0

_STORAGE_VERSION: int = 1


@dataclass
class LatencyWindow:
    """Rolling window of confirmed-response latency samples."""

    _samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=WINDOW_SIZE), repr=False
    )

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)

    def adaptive_timeout_s(self) -> float:
        """Return mean + SIGMA·σ clamped to [MIN_TIMEOUT_S, MAX_TIMEOUT_S].

        Returns DEFAULT_TIMEOUT_S when fewer than MIN_SAMPLES have been collected.
        """
        if len(self._samples) < MIN_SAMPLES:
            return DEFAULT_TIMEOUT_S
        mean, std = self._mean_std()
        raw_s = (mean + SIGMA * std) / 1000.0
        return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, raw_s))

    def as_dict(self) -> dict[str, Any]:
        n = len(self._samples)
        if n == 0:
            return {"sample_count": 0, "adaptive_timeout_s": DEFAULT_TIMEOUT_S}
        mean, std = self._mean_std()
        sorted_s = sorted(self._samples)
        p95_idx = min(int(math.ceil(0.95 * n)) - 1, n - 1)
        return {
            "sample_count": n,
            "mean_ms": round(mean, 1),
            "std_ms": round(std, 1),
            "p95_ms": round(sorted_s[p95_idx], 1),
            "min_ms": round(sorted_s[0], 1),
            "max_ms": round(sorted_s[-1], 1),
            "adaptive_timeout_s": round(self.adaptive_timeout_s(), 2),
        }

    def _mean_std(self) -> tuple[float, float]:
        n = len(self._samples)
        mean = sum(self._samples) / n
        variance = sum((x - mean) ** 2 for x in self._samples) / n
        return mean, math.sqrt(variance)


class ValveLatencyTracker:
    """Tracks and persists open/close confirmation latency for a single valve."""

    def __init__(self, hass: HomeAssistant, switch_entity_id: str) -> None:
        safe_id = switch_entity_id.replace(".", "_").replace("/", "_")
        self._store: Store = Store(
            hass, _STORAGE_VERSION, f"never_dry.latency.{safe_id}"
        )
        self.open = LatencyWindow()
        self.close = LatencyWindow()

    async def async_load(self) -> None:
        """Load persisted samples from HA storage."""
        data = await self._store.async_load()
        if not data:
            return
        for s in data.get("open", []):
            self.open.record(float(s))
        for s in data.get("close", []):
            self.close.record(float(s))

    async def async_save(self) -> None:
        """Persist current samples to HA storage."""
        await self._store.async_save(
            {
                "open": list(self.open._samples),
                "close": list(self.close._samples),
            }
        )

    def open_timeout_s(self) -> float:
        return self.open.adaptive_timeout_s()

    def close_timeout_s(self) -> float:
        return self.close.adaptive_timeout_s()

    def as_dict(self) -> dict[str, Any]:
        return {"open": self.open.as_dict(), "close": self.close.as_dict()}
