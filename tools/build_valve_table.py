#!/usr/bin/env python3
"""Render the compatibility table in ``docs/valve-compatibility.md`` from the CSV.

``docs/valve-compatibility.csv`` holds **facts only**: what the device exposes, how it
is reached, who established the row. The verdict is *derived* here, every time, from
those facts.

That split is the whole point. A hand-written verdict beside hand-written columns is two
sources of truth with nobody reconciling them: the day a firmware row gains a counter and
the verdict stays where it was, the table starts lying and no test notices. Deriving it
means the two cannot disagree, and it means the rule is written down (below) instead of
living in whoever filled the row.

Run with ``--check`` to verify the document matches the CSV without rewriting it; that is
what the test does.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_DOCS = Path(__file__).resolve().parent.parent / "docs"
_CSV = _DOCS / "valve-compatibility.csv"
_MD = _DOCS / "valve-compatibility.md"

_BEGIN = "<!-- BEGIN GENERATED TABLE: edit valve-compatibility.csv, then run tools/build_valve_table.py -->"
_END = "<!-- END GENERATED TABLE -->"

# ── The verdict rule ──────────────────────────────────────────────────────
#
# The tier answers one question: **how well does this device serve flow-meter mode?**
# Timer mode needs nothing but a valve entity, so every listed device can do it, which is
# why the bottom tier is "timer-only" and not "bad". A valve that opens and closes reliably
# and reports nothing is not a bad valve; it is a valve NeverDry drives on a clock, which is
# a supported mode, not a degraded one. Calling it "bad" would misinform the person reading
# the table to decide what to buy.
_TIERS = {
    "good": "delivery measurement available with no extra setup",
    "partial": "delivery measurement available, but with a documented caveat or extra step",
    "timer-only": "no delivery measurement, so NeverDry runs it on a clock",
}


def _verdict(row: dict[str, str]) -> tuple[str, str]:
    """Return ``(tier, reason)`` for one CSV row."""
    has_flow = row["flow_rate"] not in ("", "no")
    has_session = row["volume_session"] == "yes"
    has_aggregate = row["volume_aggregate"] not in ("", "no")

    if not (has_flow or has_session or has_aggregate):
        return "timer-only", "on/off only, and nothing reports what was delivered"

    evidence = []
    if has_flow:
        evidence.append(f"flow rate in {row['flow_rate']}")
    if has_session:
        evidence.append("session counter")
    if has_aggregate and not has_session:
        evidence.append(f"{row['volume_aggregate']} counter only")

    # A caveat that touches the delivery measurement is not a footnote: it is the difference
    # between a number you can trust and one you cannot. It costs the row its top tier.
    if row["caveat"] == "unit_change":
        return "partial", f"{', '.join(evidence)}, but the firmware can change its own counter units"
    if not has_session and has_aggregate:
        return "partial", f"{', '.join(evidence)}, subject to the calendar-reset caveat"
    if row["needs_config"] not in ("", "none") and row["needs_config"] != "history":
        return "partial", f"{', '.join(evidence)}, reachable only after a documented step"
    return "good", ", ".join(evidence)


_MARK = {"yes": "✅", "no": "❌", "": "-", "on_request": "⚠️ on request", "none": "❌ none"}


def _cell(value: str) -> str:
    return _MARK.get(value, value)


def render() -> str:
    rows = list(csv.DictReader(_CSV.open(encoding="utf-8")))
    out = [
        "| Vendor / model | Firmware | Via | Valve | Flow rate | Volume counters | History | "
        "Needs config? | Verdict | Why | LoD | By |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        tier, reason = _verdict(row)
        counters = []
        if row["volume_session"] == "yes":
            counters.append("session")
        if row["volume_aggregate"] not in ("", "no"):
            counters.append(row["volume_aggregate"])
        datecode = f" ({row['datecode']})" if row["datecode"] else ""
        out.append(
            f"| {row['vendor']} **{row['model']}** "
            f"| {row['firmware']}{datecode} "
            f"| {row['via']} | `{row['valve_domain']}.*` "
            f"| {_cell(row['flow_rate'])} "
            f"| {'✅ ' + ' + '.join(counters) if counters else '❌'} "
            f"| {_cell(row['history'])} | {_cell(row['needs_config'])} "
            f"| **{tier}** | {reason} | {_cell(row['lod'])} | {row['reported_by']} |"
        )
    legend = " · ".join(f"*{k}* = {v}" for k, v in _TIERS.items())
    out += ["", "**Verdict**, derived from the columns, never typed: " + legend]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()

    doc = _MD.read_text(encoding="utf-8")
    if _BEGIN not in doc or _END not in doc:
        print(f"markers not found in {_MD.name}", file=sys.stderr)
        return 2
    head, rest = doc.split(_BEGIN, 1)
    _, tail = rest.split(_END, 1)
    rebuilt = f"{head}{_BEGIN}\n{render()}\n{_END}{tail}"

    if args.check:
        if rebuilt != doc:
            print(f"{_MD.name} is out of step with {_CSV.name}: run tools/build_valve_table.py", file=sys.stderr)
            return 1
        return 0
    _MD.write_text(rebuilt, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
