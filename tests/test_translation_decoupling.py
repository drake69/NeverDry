"""The integration's Python must not read translation files. This keeps it that way.

Localisation belongs to the two presentation surfaces and to nothing else: Home Assistant
resolves ``translations/<lang>.json`` for the backend GUI, and the card carries its own
dictionaries for the browser. Neither is the integration's business — the Python side deals
in identifiers (``estimated_flow``, ``req_open``, error codes) and hands them out; the text
is somebody else's problem, resolved later, in the user's language, in a layer that knows
what language that is.

That separation held everywhere until the notifications were localised, and the argument the
docstring above asked for was duly had. It has exactly one exception now, and the exception
is instructive rather than grudging.

``persistent_notification.create`` takes a finished title and a finished message. There is no
later layer: for a notification, the string handed over *is* the presentation, so "emit an
identifier and let somebody else resolve it" has nobody to name. Either the text is resolved
in Python or the notification reads ``stuck_open`` at the user. ``valve_notifier.py`` is
therefore allowed to resolve, and nothing else is.

The honest version of the rule the exception implies: a condition that wants to reach a person
*without* Python composing prose has to stop being a notification and become an entity, which
the user's own automation can act on in their own words. That is tracked separately, and it is
the direction of travel; until it arrives, the bell needs its words.

Worth recording how this was found, because it says something about the guard: it fired on a
*comment* that happened to name a translation file, not on the call that does the resolving.
The substring list below could not see ``async_get_translations`` at all, so the coupling it
exists to prevent would have walked straight past it. The detection is widened here to match
what the docstring always claimed to check.

``manifest.json`` is the one file production code legitimately reads (``__init__.py`` takes
the version from it) and it is not a translation file, so it is out of scope by construction
rather than by exception.
"""

from __future__ import annotations

from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"

# Reaching for translated text, by either route: opening a file, or asking Home Assistant to
# resolve one. The second was missing until it was needed, which is the more common of the two
# in a modern integration and the only one that actually appears in this codebase.
_FORBIDDEN = ("strings.json", "translations/", "translations\\", "async_get_translations")

# The one module allowed to resolve text, for the reason set out at the top of this file.
_ALLOWED = {"valve_notifier.py"}


def test_no_production_module_reads_translation_files():
    offenders: list[str] = []
    for module in sorted(_COMPONENT.rglob("*.py")):
        if "__pycache__" in module.parts or module.name in _ALLOWED:
            continue
        source = module.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            for needle in _FORBIDDEN:
                if needle in line:
                    offenders.append(f"{module.relative_to(_COMPONENT)}:{line_number} mentions '{needle}'")

    assert not offenders, (
        "production code must not resolve translated text: emit identifiers and let the "
        "presentation layer resolve them. If this module genuinely has no later layer to "
        "hand an identifier to, say so at the top of this file and add it to the exception "
        "list, rather than widening the rule quietly:\n  " + "\n  ".join(offenders)
    )


def test_the_exception_list_names_only_modules_that_exist():
    """A stale name in the exception list is a hole nobody can see.

    A module renamed or deleted leaves its exemption behind, and the next file to take that
    name inherits a permission it never argued for.
    """
    missing = sorted(name for name in _ALLOWED if not (_COMPONENT / name).exists())
    assert not missing, f"exempted modules that no longer exist: {missing}"
