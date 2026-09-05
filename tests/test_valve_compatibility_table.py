"""The compatibility table in the docs must be what the CSV says it is.

``docs/valve-compatibility.csv`` holds the facts a reporter established; the table in
``docs/valve-compatibility.md`` is rendered from it by ``tools/build_valve_table.py``, and
the verdict column is derived rather than typed.

Without this test the arrangement is decoration: someone edits the markdown by hand because
it is right there, the CSV stays behind, and the page a person uses to choose what to buy
stops matching the data the project actually holds. It is the same failure this repository
just found between ``strings.json`` and ``en.json``: two documents, no reconciliation, and
the same remedy.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CSV = _ROOT / "docs" / "valve-compatibility.csv"
_MD = _ROOT / "docs" / "valve-compatibility.md"


def _builder():
    """Import ``tools/build_valve_table.py`` without making ``tools`` a package."""
    spec = importlib.util.spec_from_file_location("build_valve_table", _ROOT / "tools" / "build_valve_table.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_rendered_table_matches_the_csv():
    assert _builder().main.__module__  # import guard: a broken generator fails here, not silently
    module = _builder()
    doc = _MD.read_text(encoding="utf-8")
    assert module._BEGIN in doc and module._END in doc, "generated-table markers missing from the document"

    current = doc.split(module._BEGIN, 1)[1].split(module._END, 1)[0].strip()
    assert current == module.render().strip(), (
        "docs/valve-compatibility.md is out of step with valve-compatibility.csv: "
        "edit the CSV and run tools/build_valve_table.py, do not edit the table by hand"
    )


def test_every_row_declares_the_columns_the_verdict_rule_reads():
    """A row missing a field would take a verdict from a default nobody chose."""
    required = {
        "vendor",
        "model",
        "firmware",
        "via",
        "valve_domain",
        "flow_rate",
        "volume_session",
        "volume_aggregate",
        "history",
        "needs_config",
        "caveat",
        "reported_by",
    }
    rows = list(csv.DictReader(_CSV.open(encoding="utf-8")))
    assert rows, "the compatibility CSV is empty"

    problems: list[str] = []
    for index, row in enumerate(rows, start=2):
        missing = required - set(row)
        if missing:
            problems.append(f"line {index}: columns absent: {sorted(missing)}")
        for field in ("vendor", "model", "firmware", "via", "valve_domain", "reported_by"):
            if not (row.get(field) or "").strip():
                problems.append(f"line {index}: '{field}' is empty, and it is not optional")
        if row.get("valve_domain") not in ("switch", "valve"):
            problems.append(f"line {index}: valve_domain '{row.get('valve_domain')}' is neither switch nor valve")
    assert not problems, "malformed rows in valve-compatibility.csv:\n  " + "\n  ".join(problems)


def test_a_row_with_no_measurement_is_timer_only_not_bad():
    """The bottom tier says what NeverDry does with the device, not that it is poor.

    An on/off valve is driven on a clock, which is a supported mode. A table that called it
    "bad" would be telling someone not to buy hardware that works.
    """
    module = _builder()
    barebones = {
        "flow_rate": "no",
        "volume_session": "no",
        "volume_aggregate": "",
        "history": "no",
        "needs_config": "none",
        "caveat": "",
    }
    tier, reason = module._verdict(barebones)
    assert tier == "timer-only"
    assert "clock" in reason or "delivered" in reason
