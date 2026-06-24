"""Consistency guard: every ``translation_key`` used by a SelectSelector in
``config_flow.py`` must have matching entries in ``translations/en.json``.

Home Assistant lets a SelectSelector replace inline option labels with a
``translation_key`` that resolves human-readable text from the translation
files (``selector.<key>.options.<value>``). If the key is referenced but the
translation file lacks the corresponding ``selector`` entries, the dropdown
silently shows the raw option values (e.g. ``estimated_flow``) instead of a
label — a UX regression that no other test catches.

This test parses ``config_flow.py`` statically (via ``ast``, no HA import) and
fails when a referenced ``translation_key`` is missing options in ``en.json``.
It is intentionally a no-op while the config flow uses inline labels; it
activates the moment someone migrates a selector to ``translation_key``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from never_dry import const

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"
_CONFIG_FLOW = _COMPONENT / "config_flow.py"
_EN_JSON = _COMPONENT / "translations" / "en.json"


def _resolve_options(node: ast.AST) -> set[str] | None:
    """Resolve a SelectSelectorConfig ``options=`` argument to its string values.

    Returns ``None`` when the expression cannot be resolved statically, so the
    caller can fail loudly rather than pass a false negative.
    """
    # options=[CONST_A, CONST_B] or [SelectOptionDict(value=CONST, ...), ...]
    if isinstance(node, ast.List):
        values: set[str] = set()
        for elt in node.elts:
            if isinstance(elt, ast.Name):
                values.add(getattr(const, elt.id))
            elif isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                values.add(elt.value)
            elif isinstance(elt, ast.Call):
                # SelectOptionDict(value=..., label=...)
                value_kw = next((kw for kw in elt.keywords if kw.arg == "value"), None)
                if value_kw is None:
                    return None
                if isinstance(value_kw.value, ast.Name):
                    values.add(getattr(const, value_kw.value.id))
                elif isinstance(value_kw.value, ast.Constant):
                    values.add(value_kw.value.value)
                else:
                    return None
            else:
                return None
        return values
    # options=list(PLANT_FAMILIES.keys()) / list(PLANT_FAMILIES) etc.
    if isinstance(node, ast.Call):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and hasattr(const, sub.id):
                resolved = getattr(const, sub.id)
                if isinstance(resolved, dict):
                    return set(resolved.keys())
    # options=[SelectOptionDict(value=k, ...) for k, v in PLANT_FAMILIES.items()]
    if isinstance(node, ast.ListComp):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and hasattr(const, sub.id):
                resolved = getattr(const, sub.id)
                if isinstance(resolved, dict):
                    return set(resolved.keys())
    return None


def _collect_translation_keyed_selectors() -> list[tuple[str, set[str] | None]]:
    tree = ast.parse(_CONFIG_FLOW.read_text())
    out: list[tuple[str, set[str] | None]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "SelectSelectorConfig"
        ):
            continue
        tk_kw = next((kw for kw in node.keywords if kw.arg == "translation_key"), None)
        if tk_kw is None or not isinstance(tk_kw.value, ast.Constant):
            continue
        opt_kw = next((kw for kw in node.keywords if kw.arg == "options"), None)
        options = _resolve_options(opt_kw.value) if opt_kw is not None else None
        out.append((tk_kw.value.value, options))
    return out


def test_translation_keys_have_matching_options_in_en_json():
    """Each translation_key selector must have all its options translated."""
    selectors = _collect_translation_keyed_selectors()
    if not selectors:
        # Config flow uses inline labels — nothing to validate (guard is dormant).
        return

    en = json.loads(_EN_JSON.read_text())
    selector_section = en.get("selector", {})

    errors: list[str] = []
    for translation_key, options in selectors:
        entry = selector_section.get(translation_key)
        if entry is None:
            errors.append(f"translation_key '{translation_key}' missing from en.json 'selector' section")
            continue
        translated = set(entry.get("options", {}).keys())
        if options is None:
            errors.append(
                f"translation_key '{translation_key}': options could not be resolved statically — "
                "extend _resolve_options() in this test"
            )
            continue
        missing = options - translated
        if missing:
            errors.append(f"translation_key '{translation_key}' missing option labels in en.json: {sorted(missing)}")

    assert not errors, "Selector translation inconsistencies:\n  " + "\n  ".join(errors)
