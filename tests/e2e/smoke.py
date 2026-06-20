#!/usr/bin/env python3
"""
NeverDry pre-release smoke tests — run against a live Home Assistant instance.

Usage:
    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=<long-lived token>
    export ZONE_NAME="Giardino_Ortensia"   # zone name as configured in NeverDry
    export VALVE_ENTITY=switch.ortensia    # physical valve switch (required for --interactive)
    python tests/e2e/smoke.py
    python tests/e2e/smoke.py --no-valves       # skip tests that open valves
    python tests/e2e/smoke.py --interactive     # also run tests requiring manual actions
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))


def _load_dotenv() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

from ha_client import HAClient  # noqa: E402

DOMAIN = "never_dry"
INTERACTIVE_TIMEOUT_S = 15

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


# ── test registry ─────────────────────────────────────────────────────────────

_tests: list[tuple[str, bool, bool]] = []  # (name, needs_valves, interactive)


def smoke(needs_valves: bool = False, interactive: bool = False):
    def decorator(fn):
        _tests.append((fn.__name__, needs_valves, interactive))
        return fn

    return decorator


def _zone_alphanum(zone: str) -> str:
    return re.sub(r"[^a-z0-9]", "", zone.lower())


def _zone_entity(states: list, zone: str, keyword: str):
    za = _zone_alphanum(zone)
    return next(
        (
            s
            for s in states
            if za in re.sub(r"[^a-z0-9]", "", s["entity_id"])
            and keyword in s["entity_id"]
            and s["state"] not in ("unknown", "unavailable")
        ),
        None,
    )


# ── non-valve tests ───────────────────────────────────────────────────────────


@smoke()
def test_ha_reachable(ha: HAClient, zone: str) -> None:
    """HA responds on the REST API."""
    import requests

    r = requests.get(f"{ha._base}/api/", headers=ha._headers, timeout=5)
    assert r.status_code == 200, f"HTTP {r.status_code}"


@smoke()
def test_integration_loaded(ha: HAClient, zone: str) -> None:
    """NeverDry config entry is present and loaded."""
    entries = ha.find_entries(DOMAIN)
    assert entries, "No never_dry config entry found"
    loaded = [e for e in entries if e.get("state") == "loaded"]
    assert loaded, f"Entry found but state={entries[0].get('state')!r} (expected 'loaded')"


@smoke()
def test_entities_present(ha: HAClient, zone: str) -> None:
    """Core never_dry entities exist in HA state machine."""
    states = ha.list_states()
    nd = [s["entity_id"] for s in states if "neverdry" in s["entity_id"] or "never_dry" in s["entity_id"]]
    assert nd, "No neverdry/never_dry entities found in HA state machine"


@smoke()
def test_et_sensor_valid(ha: HAClient, zone: str) -> None:
    """ET sensor has a numeric, non-negative value."""
    states = ha.list_states()
    et = next((s for s in states if "et_hourly" in s["entity_id"] or "et_estimate" in s["entity_id"]), None)
    assert et, "ET sensor entity not found"
    assert et["state"] not in ("unknown", "unavailable"), f"ET sensor state={et['state']!r}"
    assert float(et["state"]) >= 0, f"ET value negative: {et['state']}"


@smoke()
def test_zone_entities_present(ha: HAClient, zone: str) -> None:
    """Entities for the configured zone exist."""
    za = _zone_alphanum(zone)
    states = ha.list_states()
    found = [s for s in states if za in re.sub(r"[^a-z0-9]", "", s["entity_id"])]
    assert found, f"No entities found for zone '{zone}' (slug: '{za}'). Check ZONE_NAME."


@smoke()
def test_no_stray_notifications(ha: HAClient, zone: str) -> None:
    """No active NeverDry CRITICAL persistent notifications."""
    states = ha.list_states()
    critical = [
        s
        for s in states
        if s["entity_id"].startswith("persistent_notification.")
        and "never_dry" in s.get("attributes", {}).get("notification_id", "")
        and "CRITICAL" in s.get("attributes", {}).get("title", "").upper()
        and s["state"] == "notifying"
    ]
    assert not critical, "Active CRITICAL NeverDry notifications: " + ", ".join(
        s["attributes"].get("title", s["entity_id"]) for s in critical
    )


@smoke()
def test_reset_deficit(ha: HAClient, zone: str) -> None:
    """reset zeros the deficit; set_deficit restores the prior value."""
    states = ha.list_states()
    ent = _zone_entity(states, zone, "deficit")
    pre = float(ent["state"]) if ent else 0.0

    ha.call_service(DOMAIN, "reset")
    time.sleep(1)

    if ent:
        after = float(ha.get_state(ent["entity_id"])["state"])
        assert after < 0.5, f"Deficit not zeroed after reset (got {after} mm)"

    ha.call_service(DOMAIN, "set_deficit", {"zone_name": zone, "deficit_mm": pre})
    time.sleep(1)


@smoke()
def test_config_reload(ha: HAClient, zone: str) -> None:
    """Config entry reloads and comes back loaded."""
    entries = ha.find_entries(DOMAIN)
    assert entries, "No never_dry config entry found"
    ha.call_service("homeassistant", "reload_config_entry", {"entry_id": entries[0]["entry_id"]})
    time.sleep(4)
    loaded = [e for e in ha.find_entries(DOMAIN) if e.get("state") == "loaded"]
    assert loaded, "Entry not loaded after reload"


# ── valve tests ───────────────────────────────────────────────────────────────


@smoke(needs_valves=True)
def test_irrigate_zone_and_stop(ha: HAClient, zone: str) -> None:
    """irrigate_zone + stop complete without error; integration stays loaded."""
    ha.call_service(DOMAIN, "set_deficit", {"zone_name": zone, "deficit_mm": 5.0})
    time.sleep(1)
    ha.call_service(DOMAIN, "irrigate_zone", {"zone_name": zone})
    time.sleep(8)
    ha.call_service(DOMAIN, "stop")
    time.sleep(6)

    loaded = [e for e in ha.find_entries(DOMAIN) if e.get("state") == "loaded"]
    assert loaded, "Integration not loaded after irrigate+stop cycle"


@smoke(needs_valves=True)
def test_deficit_reduces_after_irrigation(ha: HAClient, zone: str) -> None:
    """After irrigation, zone deficit is lower than the pre-irrigation value."""
    time.sleep(12)  # ensure MIN_SERVICE_INTERVAL_S (10s) has passed since previous irrigate_zone
    states = ha.list_states()
    ent = _zone_entity(states, zone, "deficit")
    assert ent, f"No deficit entity found for zone '{zone}'"

    target = 5.0
    ha.call_service(DOMAIN, "set_deficit", {"zone_name": zone, "deficit_mm": target})
    time.sleep(1)

    ha.call_service(DOMAIN, "irrigate_zone", {"zone_name": zone})
    time.sleep(8)
    ha.call_service(DOMAIN, "stop")
    time.sleep(6)

    after = float(ha.get_state(ent["entity_id"])["state"])
    assert after < target, f"Deficit did not reduce: was {target} mm, now {after} mm"


@smoke(needs_valves=True)
def test_emergency_stop_all_zones(ha: HAClient, zone: str) -> None:
    """irrigate_all followed by immediate stop leaves integration loaded."""
    ha.call_service(DOMAIN, "irrigate_all")
    time.sleep(2)
    ha.call_service(DOMAIN, "stop")
    time.sleep(3)

    loaded = [e for e in ha.find_entries(DOMAIN) if e.get("state") == "loaded"]
    assert loaded, "Integration not loaded after emergency stop"


# ── interactive tests ─────────────────────────────────────────────────────────


@smoke(needs_valves=True, interactive=True)
def test_external_zha_close_aborts_session(ha: HAClient, zone: str) -> None:
    """Close valve from ZHA while irrigating → session aborts, last_irrigated updates."""
    valve_entity = os.environ.get("VALVE_ENTITY", "")
    assert valve_entity, "VALVE_ENTITY env var not set — required for interactive tests"

    za = _zone_alphanum(zone)
    states = ha.list_states()
    li_ent = next(
        (s for s in states if za in re.sub(r"[^a-z0-9]", "", s["entity_id"]) and "last_irrigated" in s["entity_id"]),
        None,
    )
    pre_li = li_ent["state"] if li_ent else None

    ha.call_service(DOMAIN, "set_deficit", {"zone_name": zone, "deficit_mm": 5.0})
    time.sleep(1)
    ha.call_service(DOMAIN, "irrigate_zone", {"zone_name": zone})
    time.sleep(2)

    print(
        f"\n  {CYAN}ACTION{RESET}  Close '{valve_entity}' from ZHA/Zigbee within {INTERACTIVE_TIMEOUT_S}s", flush=True
    )

    closed = ha.wait_for_state(valve_entity, "off", timeout_s=INTERACTIVE_TIMEOUT_S)
    assert closed, f"Valve {valve_entity} did not close within {INTERACTIVE_TIMEOUT_S}s"

    time.sleep(3)
    ha.call_service(DOMAIN, "stop")  # safety net

    if li_ent:
        after_li = ha.get_state(li_ent["entity_id"])["state"]
        assert after_li != pre_li, f"last_irrigated did not update after external close (still {pre_li!r})"


@smoke(needs_valves=True, interactive=True)
def test_manual_valve_open_detected(ha: HAClient, zone: str) -> None:
    """Open and close the valve manually → NeverDry detects it as manual irrigation."""
    valve_entity = os.environ.get("VALVE_ENTITY", "")
    assert valve_entity, "VALVE_ENTITY env var not set — required for interactive tests"

    za = _zone_alphanum(zone)
    states = ha.list_states()
    li_ent = next(
        (s for s in states if za in re.sub(r"[^a-z0-9]", "", s["entity_id"]) and "last_irrigated" in s["entity_id"]),
        None,
    )
    pre_li = li_ent["state"] if li_ent else None

    print(
        f"\n  {CYAN}ACTION{RESET}  Open '{valve_entity}' manually from HA (switch.turn_on), wait 10s, then close it.",
        flush=True,
    )
    print(f"         You have {INTERACTIVE_TIMEOUT_S}s to open it.", flush=True)

    opened = ha.wait_for_state(valve_entity, "on", timeout_s=INTERACTIVE_TIMEOUT_S)
    assert opened, f"Valve {valve_entity} was not opened within {INTERACTIVE_TIMEOUT_S}s"

    print(f"  {CYAN}ACTION{RESET}  Now close '{valve_entity}' within {INTERACTIVE_TIMEOUT_S}s.", flush=True)
    closed = ha.wait_for_state(valve_entity, "off", timeout_s=INTERACTIVE_TIMEOUT_S)
    assert closed, f"Valve {valve_entity} was not closed within {INTERACTIVE_TIMEOUT_S}s"

    time.sleep(3)

    if li_ent:
        after_li = ha.get_state(li_ent["entity_id"])["state"]
        assert after_li != pre_li, f"NeverDry did not detect manual irrigation (last_irrigated still {pre_li!r})"


# ── runner ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="NeverDry pre-release smoke tests")
    parser.add_argument("--no-valves", action="store_true", help="Skip tests that open real valves")
    parser.add_argument("--interactive", action="store_true", help="Enable interactive tests requiring manual actions")
    args = parser.parse_args()

    url = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    zone = os.environ.get("ZONE_NAME", "")

    if not url or not token or not zone:
        print(f"{RED}Missing env vars. Set HA_URL, HA_TOKEN, ZONE_NAME.{RESET}")
        return 1

    ha = HAClient(url, token)

    print(f"\n{BOLD}NeverDry smoke tests → {url}{RESET}")
    flags = []
    if args.no_valves:
        flags.append("valves SKIPPED")
    if args.interactive:
        flags.append("interactive ENABLED")
    print(f"Zone: {zone!r}  |  {', '.join(flags) or 'standard run'}\n")

    # Snapshot deficit before any test modifies it
    za = _zone_alphanum(zone)
    try:
        all_states = ha.list_states()
        snap_ent = next(
            (
                s
                for s in all_states
                if za in re.sub(r"[^a-z0-9]", "", s["entity_id"])
                and "deficit" in s["entity_id"]
                and s["state"] not in ("unknown", "unavailable")
            ),
            None,
        )
        pre_run_deficit = float(snap_ent["state"]) if snap_ent else None
    except Exception:
        pre_run_deficit = None

    fn_map = {
        fn.__name__: fn
        for fn in [
            test_ha_reachable,
            test_integration_loaded,
            test_entities_present,
            test_et_sensor_valid,
            test_zone_entities_present,
            test_no_stray_notifications,
            test_reset_deficit,
            test_config_reload,
            test_irrigate_zone_and_stop,
            test_deficit_reduces_after_irrigation,
            test_emergency_stop_all_zones,
            test_external_zha_close_aborts_session,
            test_manual_valve_open_detected,
        ]
    }

    passed = failed = skipped = 0

    for name, needs_valves, interactive in _tests:
        fn = fn_map[name]
        label = name.replace("test_", "").replace("_", " ")

        if needs_valves and args.no_valves:
            print(f"  {YELLOW}SKIP{RESET}  {label}")
            skipped += 1
            continue
        if interactive and not args.interactive:
            print(f"  {YELLOW}SKIP{RESET}  {label}  (pass --interactive to enable)")
            skipped += 1
            continue

        try:
            fn(ha, zone)
            print(f"  {GREEN}PASS{RESET}  {label}")
            passed += 1
        except AssertionError as e:
            print(f"  {RED}FAIL{RESET}  {label}: {e}")
            failed += 1
        except Exception as e:
            print(f"  {RED}ERROR{RESET} {label}: {type(e).__name__}: {e}")
            failed += 1

    total = passed + failed + skipped
    color = GREEN if failed == 0 else RED
    print(f"\n{color}{BOLD}{passed}/{total - skipped} passed{RESET}", end="")
    if skipped:
        print(f"  {YELLOW}({skipped} skipped){RESET}", end="")
    print()

    if pre_run_deficit is not None:
        try:
            ha.call_service(DOMAIN, "set_deficit", {"zone_name": zone, "deficit_mm": pre_run_deficit})
            print(f"\n  {YELLOW}teardown{RESET}  deficit restored → {pre_run_deficit:.2f} mm")
        except Exception as e:
            print(f"\n  {RED}teardown WARN{RESET}  could not restore deficit: {e}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
