"""Thin HTTP client for Home Assistant REST API."""

from __future__ import annotations

import time
from typing import Any

import requests


class HAClient:
    def __init__(self, url: str, token: str, timeout: int = 10) -> None:
        self._base = url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    # ── read ──────────────────────────────────────────────────────────────

    def get_state(self, entity_id: str) -> dict[str, Any]:
        r = requests.get(
            f"{self._base}/api/states/{entity_id}",
            headers=self._headers,
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def list_states(self) -> list[dict[str, Any]]:
        r = requests.get(
            f"{self._base}/api/states",
            headers=self._headers,
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_config_entries(self) -> list[dict[str, Any]]:
        r = requests.get(
            f"{self._base}/api/config/config_entries/entry",
            headers=self._headers,
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    # ── write ─────────────────────────────────────────────────────────────

    def call_service(self, domain: str, service: str, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        r = requests.post(
            f"{self._base}/api/services/{domain}/{service}",
            headers=self._headers,
            json=data or {},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    # ── helpers ───────────────────────────────────────────────────────────

    def wait_for_state(self, entity_id: str, expected: str, timeout_s: float = 30, poll_s: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                state = self.get_state(entity_id)["state"]
                if state == expected:
                    return True
            except Exception:  # noqa: S110
                pass
            time.sleep(poll_s)
        return False

    def find_entries(self, domain: str) -> list[dict[str, Any]]:
        return [e for e in self.get_config_entries() if e.get("domain") == domain]

    def states_for_domain(self, domain: str) -> list[dict[str, Any]]:
        return [s for s in self.list_states() if s["entity_id"].startswith(f"{domain}.")]
