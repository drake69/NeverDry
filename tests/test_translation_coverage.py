"""Coverage guards: every piece of text the integration puts in front of a person
comes from a catalogue, and every catalogue entry is actually reached.

``test_translation_consistency.py`` guards the text that is *already* in
``strings.json``: that the languages agree, that a label is not a paragraph, that no
message reads an internal identifier out loud. It is a good set of guards and it has
caught real defects. What it cannot do is notice text that never entered the catalogue
in the first place, because a file it does not read is a file it cannot fail on.

That blind spot is the whole subject of #211, and the reason this module exists is less
the defect than its history. The gap was audited by hand twice, five weeks apart, and
both audits produced the same artefact: three numbers in an issue body. A number in an
issue body is a photograph. It was accurate on the day it was taken and it went stale
the next time anyone added an entity, with nothing anywhere to say so. The second audit
did not find the first one's list had drifted; it simply produced a different list, and
the difference went unremarked until a third reading put them side by side.

So the deliverable here is not a corrected count. It is the count made executable. What
follows fails with the list attached, and the list is regenerated on every run by
reading the source rather than by remembering it.

The three guards close a triangle, and it is the closure that matters rather than any
one edge:

* every entity takes its name from the catalogue (nothing is named by a literal);
* every ``translation_key`` the source names exists in ``strings.json``;
* every entry in ``strings.json`` is named by some entity.

Two of those directions are the obvious ones. The third is the one no hand audit
performed, because a person auditing translations looks for what is *missing* from the
catalogue and never for what is *present and unreachable*. Six entries under
``entity.sensor`` were translated into Italian and into German by two different people
and have never rendered in any language: ``ModelInputSensor`` assigns ``_attr_name`` and
never sets ``_attr_translation_key``, so the catalogue is bypassed and the English
literal wins. Volunteer time was spent twice on text no user has ever seen. That is the
specific failure this file exists to make impossible to repeat.

Everything is read statically. No Home Assistant import, no YAML dependency, nothing
executed: CI installs pytest and nothing else, and a guard that needs a dependency to
run is a guard that stops running the day the dependency is dropped.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"
_STRINGS = _COMPONENT / "strings.json"
_SERVICES_YAML = _COMPONENT / "services.yaml"
_NOTIFIER = _COMPONENT / "valve_notifier.py"

# The platform modules are *discovered*, never listed. A hard-coded tuple is exactly how
# a new platform file slips past every guard here on the day it lands: the file is
# present, the entities are unnamed, and the tests are green.
_PLATFORM_FILES = sorted(p for p in _COMPONENT.glob("*.py") if not p.name.startswith("_"))


def _catalogue() -> dict:
    return json.loads(_STRINGS.read_text(encoding="utf-8"))


def _entity_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """Classes in one module that are Home Assistant entities.

    A class counts when it inherits something whose name ends in ``Entity`` (``SensorEntity``,
    ``ButtonEntity``, ``RestoreSensor`` is caught by the second rule) or when it inherits a
    class already counted in the same module. The transitive step is what catches
    ``_ZoneTextSensor`` and the twelve zone sensors built on it: the base carries the naming,
    the subclasses only pass a literal into it, and a check that looked at leaf classes alone
    would report the base and miss the twelve, or the reverse.
    """
    found: dict[str, ast.ClassDef] = {}
    for _ in range(8):  # fixed point; the hierarchy here is three deep at most
        grew = False
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name in found:
                continue
            for base in node.bases:
                label = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                if label.endswith("Entity") or label in found:
                    found[node.name] = node
                    grew = True
                    break
        if not grew:
            break
    return list(found.values())


def _attr_assignments(cls: ast.ClassDef, attribute: str) -> list[tuple[int, ast.AST]]:
    """Every assignment to ``_attr_<attribute>`` in a class, class-level or on ``self``."""
    out: list[tuple[int, ast.AST]] = []
    for node in ast.walk(cls):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == f"_attr_{attribute}":
                out.append((node.lineno, node.value))
    return out


def test_no_entity_is_named_by_a_literal():
    """An entity name is a catalogue key, never a string in the source.

    ``_attr_name = "Deficit"`` is English served to a Dutch user under a Dutch form, and it
    is invisible from the translation files: nothing in ``strings.json`` is missing, so
    every guard that reads ``strings.json`` passes while the interface is half translated.
    That is how the defect survived two audits.

    ``_attr_name = None`` stays allowed. It is the Home Assistant idiom for *this entity is
    the device*, it names nothing, and there is no text in it to translate.
    """
    offenders: list[str] = []
    for path in _PLATFORM_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in _entity_classes(tree):
            for lineno, value in _attr_assignments(cls, "name"):
                if isinstance(value, ast.Constant) and value.value is None:
                    continue
                shown = repr(value.value) if isinstance(value, ast.Constant) else ast.unparse(value)
                offenders.append(f"{path.name}:{lineno} {cls.name}._attr_name = {shown}")

    assert not offenders, (
        "these entities are named in the source instead of the catalogue, so their names stay "
        "English in every language:\n  " + "\n  ".join(offenders)
    )


def _keys_passed_into(tree: ast.Module, cls: ast.ClassDef, parameter: str) -> set[str]:
    """Literal translation keys handed to ``cls`` through the ``parameter`` of its ``__init__``.

    A base class that takes its key as an argument is invisible to a check that only reads
    assignments: ``self._attr_translation_key = translation_key`` names no key at all, and the
    twelve zone sensors built on ``_ZoneTextSensor`` would then be held to nothing. That is the
    same blind spot, in the same three classes, that let the hand audit undercount the English
    names by three - so it is worth the extra pass rather than a comment saying it is known.

    Both call shapes are read: the class invoked by name, and ``super().__init__`` inside a
    subclass. Only literal arguments are collected; anything computed is left alone, since a
    guard that guessed there would report keys nobody wrote.
    """
    init = next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
    if init is None:
        return set()
    names = [a.arg for a in init.args.args]
    if parameter not in names:
        return set()
    # Every call site omits ``self``, whether it goes through the class name or through
    # ``super()``, so the argument list is always shifted by one against the signature.
    position = names.index(parameter) - 1

    heirs = {cls.name}
    for _ in range(4):
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(getattr(b, "id", "") in heirs for b in node.bases):
                heirs.add(node.name)

    def literal_at(call: ast.Call) -> str | None:
        if 0 <= position < len(call.args):
            argument = call.args[position]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                return argument.value
        for keyword in call.keywords:
            if (
                keyword.arg == parameter
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return keyword.value.value
        return None

    found: set[str] = set()
    for node in ast.walk(tree):
        # The class invoked by name, anywhere in the module.
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == cls.name:
            found.add(literal_at(node) or "")
        # ``super().__init__`` but only inside a class that actually inherits this one:
        # scanning every super() call in the file would read one class's arguments against
        # another class's signature, which is how icons and unique-id suffixes end up being
        # reported as translation keys.
        if isinstance(node, ast.ClassDef) and node.name in heirs and node is not cls:
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "__init__"
                ):
                    found.add(literal_at(inner) or "")
    return found - {""}


def _declared_translation_keys() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Two views of the keys the source declares: what it *states*, and what it *could* produce.

    Most are plain literals. One family is not: ``ModelInputSensor`` builds its key as
    ``f"model_input_{key}"`` over a module-level table, because the same class publishes six
    different quantities and writing six near-identical classes to satisfy a test would be
    the test dictating the design.

    An f-string is therefore expanded by substituting **every string literal in that module**
    into its holes. That over-generates on purpose, which is why the two views are kept apart
    and used in opposite directions:

    * *stated* holds literals only, and answers "which keys must exist in the catalogue" - an
      over-generated key must never be allowed to demand an entry, or the guard would insist
      on hundreds of files that were never meant to exist;
    * *possible* holds literals plus expansions, and answers "which catalogue entries are
      reachable" - here breadth is the safe direction, because the cost of being wrong is
      failing to report a dead entry rather than inventing one.

    What that gives up is narrow and worth stating: a stale ``model_input_*`` entry goes
    unreported if the matching fragment survives as a literal somewhere in the same file.
    """
    stated: dict[str, set[str]] = {}
    possible: dict[str, set[str]] = {}
    for path in _PLATFORM_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        plain: set[str] = set()
        built: set[str] = set()
        for cls in _entity_classes(tree):
            for _, value in _attr_assignments(cls, "translation_key"):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    plain.add(value.value)
                elif isinstance(value, ast.Name):
                    plain.update(_keys_passed_into(tree, cls, value.id))
                elif isinstance(value, ast.JoinedStr):
                    prefix = "".join(
                        part.value
                        for part in value.values
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    )
                    built.update(prefix + literal for literal in literals)
        if plain or built:
            stated[path.stem] = plain
            possible[path.stem] = plain | built
    return stated, possible


def test_the_entity_catalogue_and_the_entities_name_each_other():
    """``strings.json`` and the source agree in both directions, and the second one matters.

    A key in the source with no entry renders as the raw key. An entry with no key in the
    source is worse in a quieter way: it looks like a translated entity, it is handed to
    volunteers to translate, and it reaches nobody. Six of these have been sitting under
    ``entity.sensor`` being translated by hand into two languages.
    """
    catalogue = _catalogue().get("entity", {})
    stated, possible = _declared_translation_keys()
    problems: list[str] = []

    for platform, entries in sorted(catalogue.items()):
        for key in sorted(set(entries) - possible.get(platform, set())):
            problems.append(f"entity.{platform}.{key} is in strings.json but no entity sets that translation_key")
    for platform, keys in sorted(stated.items()):
        for key in sorted(keys - set(catalogue.get(platform, {}))):
            problems.append(f"{platform}.py sets translation_key '{key}' with no entry in strings.json")

    assert not problems, "the entity catalogue and the entities have drifted apart:\n  " + "\n  ".join(problems)


def test_notifications_come_from_the_catalogue():
    """The messages that arrive when something goes wrong are translated like everything else.

    These are the worst strings in the integration to leave in English, because they are read
    in the one situation where the reader is not browsing: a valve is stuck open, water is
    running, and the notification is the whole interface. ``_TEMPLATES`` in
    ``valve_notifier.py`` holds them as literals and passes them through no catalogue at all.

    Both directions again: every kind the notifier can raise needs an entry, and an entry with
    no kind behind it is text nobody will ever be shown.
    """
    source = _NOTIFIER.read_text(encoding="utf-8")
    tree = ast.parse(source)

    kinds = {
        stmt.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "NotificationKind"
        for stmt in node.body
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
    }
    assert kinds, "NotificationKind not found in valve_notifier.py - the extractor has drifted"

    common = _catalogue().get("common", {})
    catalogued = {
        key[len("notification_") : -len("_title")]
        for key in common
        if key.startswith("notification_") and key.endswith("_title")
    }

    problems: list[str] = []
    for kind in sorted(kinds - catalogued):
        problems.append(f"{kind}: no common.notification_{kind}_title in strings.json")
    for kind in sorted(catalogued - kinds):
        problems.append(f"common.notification_{kind}_*: no NotificationKind raises it")
    for kind in sorted(kinds & catalogued):
        if f"notification_{kind}_body" not in common:
            problems.append(f"{kind}: has a title but no body")

    # Prose reaching the notifier as a literal is the defect coming back. Docstrings and
    # logging formats are prose too, and neither is shown to a user, so both are excluded
    # rather than tolerated: a guard that fires on comments gets silenced, and a silenced
    # guard is how the catalogue drifts the second time.
    exempt = {
        id(node) for node in ast.walk(tree) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            exempt.add(id(node.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = getattr(node.func.value, "id", "")
            if target.startswith("_LOGGER") or node.func.attr in {"debug", "info", "warning", "error", "exception"}:
                exempt.update(id(argument) for argument in node.args)
    for module_node in ast.walk(tree):
        for child in ast.iter_child_nodes(module_node):
            prose = isinstance(child, ast.Constant) and isinstance(child.value, str) and len(child.value.split()) > 4
            if prose and id(child) not in exempt:
                problems.append(f"valve_notifier.py:{child.lineno} literal text {child.value[:50]!r}")

    assert not problems, "notification text does not come from the catalogue:\n  " + "\n  ".join(problems)


def _services_yaml_structure() -> set[str]:
    """The dotted label paths ``services.yaml`` implies: one per service and per field.

    Read by hand rather than with PyYAML on purpose: CI installs pytest and nothing else, and
    a guard that needs a dependency is a guard that stops running the day the dependency goes.
    The file is plain two-space YAML with no anchors and no flow style, so indentation is
    enough to tell a service from one of its fields.

    Structure, not text. Since the wording moved into the catalogue there is nothing left in
    this file to compare against, and the useful question changed with it: not "does the text
    match" but "did somebody add a service, or a field, and leave it with nothing to say".
    """
    paths: set[str] = set()
    service = None
    for raw in _SERVICES_YAML.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        match = re.match(r"([A-Za-z_]\w*):", raw.strip())
        if not match:
            continue
        key = match.group(1)
        if indent == 0:
            service = key
            paths.update({f"{service}.name", f"{service}.description"})
        elif indent == 4 and service and key != "fields":
            paths.update({f"{service}.fields.{key}.name", f"{service}.fields.{key}.description"})
    return paths


def test_services_are_mirrored_in_the_catalogue():
    """Every label in ``services.yaml`` has a counterpart under ``services`` in ``strings.json``.

    The service dialogs are a surface in their own right, and today ``strings.json`` has no
    ``services`` section at all, so all of it is English everywhere. Worth noting while this
    is being fixed: the guards that forbid a label from being a paragraph and forbid text from
    naming an internal identifier both read ``translations/*.json`` and therefore have never
    looked at this file. ``irrigate_zone`` describes itself with ``estimated_flow``,
    ``flow_meter`` and ``volume_preset``, which is precisely what those guards exist to
    prevent, in a file they do not read.
    """
    catalogue = _catalogue().get("services", {})
    flat: set[str] = set()
    for service, entry in catalogue.items():
        for key in ("name", "description"):
            if key in entry:
                flat.add(f"{service}.{key}")
        for field, spec in (entry.get("fields") or {}).items():
            for key in ("name", "description"):
                if key in spec:
                    flat.add(f"{service}.fields.{field}.{key}")

    declared = _services_yaml_structure()
    assert declared, "no services found in services.yaml - the reader has drifted from the file"

    problems = [f"services.yaml has {path} with nothing to translate it" for path in sorted(declared - flat)]
    problems += [
        f"strings.json has services.{path} with no counterpart in services.yaml" for path in sorted(flat - declared)
    ]

    assert not problems, "services.yaml and the catalogue have drifted apart:\n  " + "\n  ".join(problems)
