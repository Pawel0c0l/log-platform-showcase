#!/usr/bin/env python3
"""Release-gate DECLARATIONS must be parsed fail-closed. No database needed.

Run:
    python3 ops/tests_manual/test_release_schema_requirements_parser.py

THE DEFECT THIS CLOSES.
    `db/schema_requirements.json` is release-gate configuration: what it says is
    the only thing standing between an activation and a client schema that
    cannot run the release. The parser used to be tolerant, and independent
    review demonstrated all three shapes of what that costs, on this very file:

      * `"defualt"` — an unknown key was silently ignored, so the misspelled
        assertion was simply not made and the gate got weaker with no diagnostic;
      * `"enabeld"` — the same, removing the trigger-enabled assertion;
      * `"nullable": "false"` — coerced by `bool("false")` into `True`, i.e. the
        declaration was enforced as the OPPOSITE of what it read.

    None of the three is distinguishable from a correct declaration by looking
    at the gate's output, which is exactly why they must be refused at parse
    time as `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED` rather than interpreted.

WHY THIS FILE HAS NO DATABASE.
    Every case here is about the DECLARATION, not about any schema. Burying
    them inside a PostgreSQL suite would make a parse failure look like a
    schema failure and would make the cheapest checks in the repository depend
    on a disposable instance. Physical observation stays in
    `test_migration_049_physical_preflight_postgres.py`.

READ-ONLY. Touches no database and no network. It writes only inside a
temporary directory it creates and removes itself — `load_release_requirements`
reads a release TREE, so proving how an unreadable or malformed source is
classified requires a source to exist — and it mutates nothing in the
repository.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.release_schema_preflight import (  # noqa: E402
    DEFAULT_STATE_ABSENT,
    DEFAULT_STATE_EXPRESSION,
    SCHEMA_REQUIREMENTS_RELPATH,
    SchemaPreflightError,
    load_release_requirements,
    parse_capabilities,
    parse_requirements,
)

MALFORMED = "RELEASE_SCHEMA_REQUIREMENTS_MALFORMED"
UNREADABLE = "RELEASE_SCHEMA_REQUIREMENTS_UNREADABLE"
#: The Driver Eco Dashboard requirement is anchored on 050: 049 is applied
#: shared history and the reviewed contract arrives as the forward migration.
#: Resolved below, once `_document` and the shape resolver exist. Pinning the
#: filename broke this suite when `050_eco_dashboard_external_mailer_ownership`
#: was superseded by `051_eco_dashboard_capability_retirement` — the DECLARED
#: shape was identical (the same 44 columns, the same defaults), so every
#: assertion here still held; only the name it was reached by had moved.
MIGRATION_ECO_DASH = ""

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _document() -> Dict[str, Any]:
    return json.loads(
        (ROOT / "db" / "schema_requirements.json").read_text(encoding="utf-8")
    )


def _requirement(document: Dict[str, Any], migration_prefix: str) -> Dict[str, Any]:
    for requirement in document["requirements"]:
        if requirement["migration"].startswith(migration_prefix):
            return requirement
    raise AssertionError(f"no requirement starting {migration_prefix!r}")


def _migration_declaring(*column_names: str) -> str:
    """The migration prefix whose first relation declares all of `column_names`.

    Resolved from the document instead of pinned. These cases are about the
    PARSER; the migration they happen to mutate is incidental, and it lives in
    `db/schema_requirements.json`, which is live release-gate configuration that
    legitimately changes. Pinning `COLUMN_RICH` broke this whole suite the day that
    migration was superseded by `051` — a configuration change, not a parser
    regression, reported as fifty-three failures.
    """
    document = _document()
    for requirement in document["requirements"]:
        for relation in requirement.get("relations") or []:
            declared = {column["name"] for column in relation.get("columns") or []}
            if declared.issuperset(column_names):
                return requirement["migration"][:3]
    raise AssertionError(f"no requirement declares columns {column_names!r}")


def _relation(document: Dict[str, Any], migration_prefix: str) -> Dict[str, Any]:
    return _requirement(document, migration_prefix)["relations"][0]


def _column(document: Dict[str, Any], migration_prefix: str,
            name: str) -> Dict[str, Any]:
    for column in _relation(document, migration_prefix)["columns"]:
        if column["name"] == name:
            return column
    raise AssertionError(f"no column {name!r}")


def _refuses(label: str, mutate: Callable[[Dict[str, Any]], None],
             expected_code: str = MALFORMED) -> None:
    """One damaged declaration must be refused with the standard code."""
    document = _document()
    mutate(document)
    try:
        parse_requirements(document)
    except SchemaPreflightError as exc:
        _check(f"{label} -> {expected_code}", exc.code == expected_code,
               f"actual: {exc.code}: {exc.detail}")
        return
    _check(f"{label} -> {expected_code}", False,
           "the declaration was ACCEPTED")


def _accepts(label: str, mutate: Callable[[Dict[str, Any]], None]) -> None:
    document = _document()
    mutate(document)
    try:
        parse_requirements(document)
    except SchemaPreflightError as exc:
        _check(label, False, f"refused: {exc.code}: {exc.detail}")
        return
    _check(label, True)


# ---------------------------------------------------------------------------
# 1. CONTROL — the repository's own declaration parses and keeps its meaning
# ---------------------------------------------------------------------------

#: The requirement these cases mutate: the one declaring the columns they reach
#: for. Resolved by shape so a renumbering in the live document cannot break the
#: parser suite again.
COLUMN_RICH = _migration_declaring("state", "metadata_json")

#: The full migration name behind that prefix.
MIGRATION_ECO_DASH = next(
    requirement["migration"] for requirement in _document()["requirements"]
    if requirement["migration"].startswith(COLUMN_RICH)
)


def control() -> None:
    print("\n### CONTROL — the current declaration is valid")
    document = _document()
    try:
        parsed = parse_requirements(document)
    except SchemaPreflightError as exc:
        _check("db/schema_requirements.json parses", False,
               f"{exc.code}: {exc.detail}")
        return
    _check("db/schema_requirements.json parses", True)
    _check("every declared requirement survives the strict parser",
           len(parsed) == len(document["requirements"]),
           f"{len(parsed)} of {len(document['requirements'])}")
    _check("capabilities parse to the declared set",
           parse_capabilities(document)
           == frozenset(document.get("capabilities", [])))

    # The unrelated M4/M5/M-LAG declarations must not have been narrowed by the
    # strict parser: their constraints, indexes and alternatives all survive.
    by_migration = {r.migration: r for r in parsed}
    for migration in ("061_workflow_a_provider_request_log.sql",
                      "062_workflow_a_multi_cadence_schedule_identity.sql",
                      "063_workflow_a_trip_delivery_lag_daily.sql",
                      "047_client_trips_first_seen_request_id.sql",
                      "048_client_trips_first_seen_response_received_at.sql"):
        _check(f"{migration} still parses to a requirement",
               migration in by_migration, sorted(by_migration))
    m_lag = by_migration.get("048_client_trips_first_seen_response_received_at.sql")
    if m_lag is not None:
        _check("the 048 EXPAND/CONTRACT alternatives are preserved",
               len(m_lag.relations[0].constraint_alternatives) == 2,
               str(m_lag.relations[0].constraint_alternatives))
    m5 = by_migration.get("062_workflow_a_multi_cadence_schedule_identity.sql")
    if m5 is not None:
        _check("the 062 partial unique indexes are preserved",
               sum(len(r.indexes) for r in m5.relations) >= 3,
               str([len(r.indexes) for r in m5.relations]))

    # `_comment` is reserved everywhere and asserts nothing, which is what the
    # 048 alternatives already rely on.
    _accepts("a '_comment' on any declaration object is accepted",
             lambda d: _column(d, COLUMN_RICH, "state").update(
                 {"_comment": "prose, not an assertion"}))
    _refuses("a '_comment' that is neither prose nor lines of prose",
             lambda d: _column(d, COLUMN_RICH, "state").update({"_comment": {"a": 1}}))


# ---------------------------------------------------------------------------
# 2. UNKNOWN AND MISSPELLED KEYS
# ---------------------------------------------------------------------------

def unknown_keys() -> None:
    print("\n### UNKNOWN / MISSPELLED KEYS — refused, never ignored")

    def rename(entry: Dict[str, Any], old: str, new: str) -> None:
        entry[new] = entry.pop(old)

    _refuses("THE REVIEWED TYPO: a column key 'defualt'",
             lambda d: rename(_column(d, COLUMN_RICH, "metadata_json"),
                              "default", "defualt"))
    _refuses("THE REVIEWED TYPO: a trigger key 'enabeld'",
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update(
                 {"enabeld": False}))
    _refuses("a column key 'nullabel'",
             lambda d: rename(_column(d, COLUMN_RICH, "state"),
                              "nullable", "nullabel"))
    _refuses("a constraint key 'definiton'",
             lambda d: rename(_relation(d, COLUMN_RICH)["constraints"][0],
                              "definition", "definiton"))
    _refuses("a constraint key 'validaded'",
             lambda d: rename(_relation(d, COLUMN_RICH)["constraints"][0],
                              "validated", "validaded"))
    _refuses("a trigger key 'evnets'",
             lambda d: rename(_relation(d, COLUMN_RICH)["triggers"][0],
                              "events", "evnets"))
    _refuses("a function key 'body_sha265'",
             lambda d: rename(_requirement(d, COLUMN_RICH)["functions"][0],
                              "body_sha256", "body_sha265"))
    _refuses("a relation key 'colummns'",
             lambda d: _relation(d, COLUMN_RICH).update({"colummns": []}))
    _refuses("an index key 'definitions'",
             lambda d: rename(_relation(d, "062")["indexes"][0],
                              "definition", "definitions"))
    _refuses("a requirement key 'scoope'",
             lambda d: _requirement(d, COLUMN_RICH).update({"scoope": "platform"}))
    _refuses("a constraint-alternative key 'constraint'",
             lambda d: _relation(d, "048")["constraint_alternatives"][0].update(
                 {"constraint": []}))
    _refuses("a document key 'requirments'",
             lambda d: d.update({"requirments": []}))


# ---------------------------------------------------------------------------
# 3. WRONG JSON PRIMITIVE TYPES
# ---------------------------------------------------------------------------

def wrong_types() -> None:
    print("\n### WRONG PRIMITIVE TYPES — no coercion, no truthiness")

    # THE REVIEWED COERCION. `bool("false")` is True, so this declaration was
    # enforced as the exact opposite of what a human reading it would conclude.
    _refuses('THE REVIEWED COERCION: "nullable": "false"',
             lambda d: _column(d, COLUMN_RICH, "state").update({"nullable": "false"}))
    _refuses('"nullable": "true"',
             lambda d: _column(d, COLUMN_RICH, "state").update({"nullable": "true"}))
    _refuses('"nullable": 0',
             lambda d: _column(d, COLUMN_RICH, "state").update({"nullable": 0}))
    _refuses('"nullable": 1',
             lambda d: _column(d, COLUMN_RICH, "state").update({"nullable": 1}))
    _refuses('"nullable": null',
             lambda d: _column(d, COLUMN_RICH, "state").update({"nullable": None}))
    _refuses('"validated": "false"',
             lambda d: _relation(d, COLUMN_RICH)["constraints"][0].update(
                 {"validated": "false"}))
    _refuses('"validated": 0',
             lambda d: _relation(d, COLUMN_RICH)["constraints"][0].update(
                 {"validated": 0}))
    _refuses('"enabled": "true"',
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update(
                 {"enabled": "true"}))
    _refuses('"enabled": 1',
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update(
                 {"enabled": 1}))
    _refuses("an integer where a string is required (column type)",
             lambda d: _column(d, COLUMN_RICH, "state").update({"type": 7}))
    _refuses("a list where a string is required (relation table)",
             lambda d: _relation(d, COLUMN_RICH).update({"table": ["a", "b"]}))
    _refuses("a string where a list is required (relation columns)",
             lambda d: _relation(d, COLUMN_RICH).update({"columns": "all of them"}))
    _refuses("an object where a list is required (relation constraints)",
             lambda d: _relation(d, COLUMN_RICH).update({"constraints": {"a": 1}}))
    _refuses("a list where an object is required (a column)",
             lambda d: _relation(d, COLUMN_RICH)["columns"].append(["name", "text"]))
    _refuses("a non-string member of a string array (trigger events)",
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update(
                 {"events": ["INSERT", 4]}))
    _refuses("null where a value is required (function schema)",
             lambda d: _requirement(d, COLUMN_RICH)["functions"][0].update(
                 {"schema": None}))
    _refuses("an empty string where a name is required",
             lambda d: _column(d, COLUMN_RICH, "state").update({"name": ""}))
    _refuses("capabilities carrying a non-string",
             lambda d: d.update({"capabilities": ["ok", 7]}))
    _refuses("requirements that are not an array",
             lambda d: d.update({"requirements": {"049": {}}}))


# ---------------------------------------------------------------------------
# 4. ENUM AND STRUCTURAL VALIDATION
# ---------------------------------------------------------------------------

def enum_and_structure() -> None:
    print("\n### ENUMS AND STRUCTURE")
    _refuses("an unsupported scope",
             lambda d: _requirement(d, COLUMN_RICH).update({"scope": "client"}))
    _refuses("an unsupported trigger timing",
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update(
                 {"timing": "SOMETIMES"}))
    _refuses("an unsupported trigger level",
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update(
                 {"level": "TABLE"}))
    _refuses("an unsupported trigger event",
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update(
                 {"events": ["INSERT", "SELECT"]}))
    _refuses("a trigger asserting no event at all",
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].update({"events": []}))
    _refuses("a trigger declaring no function",
             lambda d: _relation(d, COLUMN_RICH)["triggers"][0].pop("function"))
    _refuses("a function body_sha256 that is not a digest",
             lambda d: _requirement(d, COLUMN_RICH)["functions"][0].update(
                 {"body_sha256": "not-a-digest"}))
    _refuses("a column declared twice",
             lambda d: _relation(d, COLUMN_RICH)["columns"].append(
                 copy.deepcopy(_column(d, COLUMN_RICH, "state"))))
    _refuses("a constraint declared twice",
             lambda d: _relation(d, COLUMN_RICH)["constraints"].append(
                 copy.deepcopy(_relation(d, COLUMN_RICH)["constraints"][0])))
    _refuses("a requirement declaring nothing physical",
             lambda d: _requirement(d, COLUMN_RICH).update(
                 {"relations": [], "functions": []}))
    _refuses("a relation declaring nothing physical",
             lambda d: _relation(d, COLUMN_RICH).update(
                 {"columns": [], "constraints": [], "indexes": [],
                  "triggers": [], "constraint_alternatives": []}))
    _refuses("a single-member constraint_alternatives group",
             lambda d: _relation(d, "048").update(
                 {"constraint_alternatives":
                  _relation(d, "048")["constraint_alternatives"][:1]}))
    _refuses("an unsupported requirements document version",
             lambda d: d.update({"version": "log-platform-schema-requirements/2"}),
             expected_code="RELEASE_SCHEMA_REQUIREMENTS_VERSION_UNSUPPORTED")


# ---------------------------------------------------------------------------
# 5. THE DEFAULT STATE MODEL
# ---------------------------------------------------------------------------

def default_state_model() -> None:
    print("\n### DEFAULT STATE — three states, one representation each")
    document = _document()
    parsed = [r for r in parse_requirements(document)
              if r.migration == MIGRATION_ECO_DASH][0]
    columns = {c.name: c for c in parsed.relations[0].columns}
    _check("every one of the 44 runtime columns declares a default state",
           len(columns) == 44
           and all(c.default is not None for c in columns.values()),
           str(sorted(n for n, c in columns.items() if c.default is None)))
    _check("provider_name is required to have NO default",
           columns["provider_name"].default.state == DEFAULT_STATE_ABSENT,
           str(columns["provider_name"].default))
    _check("metadata_json is required to have its exact default",
           columns["metadata_json"].default.state == DEFAULT_STATE_EXPRESSION
           and columns["metadata_json"].default.expression == "'{}'::jsonb",
           str(columns["metadata_json"].default))
    _check("an absent-default requirement carries no expression",
           all(c.default.expression is None for c in columns.values()
               if c.default.state == DEFAULT_STATE_ABSENT))

    # A requirement that omits the key still means "not checked", so releases
    # and requirements that never asserted a default keep parsing unchanged.
    document = _document()
    _column(document, COLUMN_RICH, "state").pop("default")
    unspecified = [
        c for r in parse_requirements(document) if r.migration == MIGRATION_ECO_DASH
        for c in r.relations[0].columns if c.name == "state"
    ][0]
    _check("omitting 'default' still means 'not asserted'",
           unspecified.default is None, str(unspecified.default))

    # The string shorthand is the M4-era form and keeps meaning the expression
    # state — the same string-or-object convention `constraints` already uses.
    document = _document()
    _column(document, COLUMN_RICH, "metadata_json")["default"] = "'{}'::jsonb"
    shorthand = [
        c for r in parse_requirements(document) if r.migration == MIGRATION_ECO_DASH
        for c in r.relations[0].columns if c.name == "metadata_json"
    ][0]
    _check("the string shorthand still means the expression state",
           shorthand.default is not None
           and shorthand.default.state == DEFAULT_STATE_EXPRESSION
           and shorthand.default.expression == "'{}'::jsonb",
           str(shorthand.default))

    _refuses('"default": null is refused, not read as "no default"',
             lambda d: _column(d, COLUMN_RICH, "state").update({"default": None}))
    _refuses("an unknown default state",
             lambda d: _column(d, COLUMN_RICH, "state").update(
                 {"default": {"state": "whatever"}}))
    _refuses("the expression state with no expression",
             lambda d: _column(d, COLUMN_RICH, "state").update(
                 {"default": {"state": DEFAULT_STATE_EXPRESSION}}))
    _refuses("the absent state carrying an expression",
             lambda d: _column(d, COLUMN_RICH, "state").update(
                 {"default": {"state": DEFAULT_STATE_ABSENT,
                              "expression": "'x'::text"}}))
    _refuses("a default expression of the wrong type",
             lambda d: _column(d, COLUMN_RICH, "state").update(
                 {"default": {"state": DEFAULT_STATE_EXPRESSION,
                              "expression": 0}}))
    _refuses("a default object with an unknown key",
             lambda d: _column(d, COLUMN_RICH, "state").update(
                 {"default": {"state": DEFAULT_STATE_ABSENT, "value": "x"}}))
    _refuses("a default declared as a list",
             lambda d: _column(d, COLUMN_RICH, "state").update({"default": ["absent"]}))
    _refuses("a default object declaring no state",
             lambda d: _column(d, COLUMN_RICH, "state").update(
                 {"default": {"expression": "'x'::text"}}))
    _refuses("an empty default expression",
             lambda d: _column(d, COLUMN_RICH, "state").update({"default": "   "}))


# ---------------------------------------------------------------------------
# 6. HISTORICAL DECLARATIONS STILL PARSE
# ---------------------------------------------------------------------------

def historical_forms() -> None:
    """A strict parser must not make older releases un-activatable.

    Requirements are read FROM THE RELEASE TREE, so a rollback parses an older
    document with today's parser. Every form the repository has ever shipped —
    name-only constraints, requirements without `functions`, relations without
    `indexes` or `triggers` — must therefore keep parsing.
    """
    print("\n### HISTORICAL FORMS — older releases stay activatable")
    m4_era = {
        "version": "log-platform-schema-requirements/1",
        "_comment": ["the M4-era shape, kept as a compatibility fixture"],
        "requirements": [{
            "migration": "061_workflow_a_provider_request_log.sql",
            "scope": "platform",
            "milestone": "M4",
            "reason": "name-only constraints and no functions key",
            "relations": [{
                "schema": "workflow_a_control",
                "table": "provider_request_log",
                "columns": [{"name": "request_id", "type": "uuid",
                             "nullable": False}],
                "constraints": ["uq_provider_request_log_identity"],
            }],
        }],
    }
    try:
        parsed = parse_requirements(m4_era)
        _check("the M4-era document shape still parses", len(parsed) == 1)
        _check("a bare-string constraint still means a name-only requirement",
               parsed[0].relations[0].constraints[0].name
               == "uq_provider_request_log_identity"
               and parsed[0].relations[0].constraints[0].definition is None)
        _check("a column that declares no default is still 'not asserted'",
               parsed[0].relations[0].columns[0].default is None)
    except SchemaPreflightError as exc:
        _check("the M4-era document shape still parses", False,
               f"{exc.code}: {exc.detail}")
    _check("a document without 'capabilities' declares none",
           parse_capabilities(m4_era) == frozenset())


# ---------------------------------------------------------------------------
# 7. ALTERNATIVE GROUPS — internally well-formed, or refused
# ---------------------------------------------------------------------------

def _alternatives(document: Dict[str, Any],
                  migration_prefix: str = "048") -> List[Dict[str, Any]]:
    return _relation(document, migration_prefix)["constraint_alternatives"]


def _group(document: Dict[str, Any], state: str) -> Dict[str, Any]:
    for group in _alternatives(document):
        if group["state"] == state:
            return group
    raise AssertionError(f"no {state!r} alternative group")


def alternative_group_identity() -> None:
    """A one-of group must be internally well-formed, not merely non-empty.

    THE DEFECT THIS CLOSES. Duplicate columns, constraints, indexes, triggers,
    functions, relations and requirements were all refused — but not duplicate
    constraints INSIDE a `constraint_alternatives` group, which was the one
    place the check was not wired up. Independent review duplicated the 048
    EXPAND constraint, the document parsed, and physical matching then reported
    no defect: the duplication was invisible from both ends.

    A group is satisfied only when ALL of its constraints hold, so two
    declarations of one identity are redundant when they agree and unsatisfiable
    when they do not. Neither is a thing the gate should have to interpret.
    """
    print("\n### ALTERNATIVE GROUPS — one identity, one declaration")

    # 1-3. Duplicates inside ONE group, in each shape they can take.
    _refuses("a constraint identity duplicated inside one alternative group",
             lambda d: _group(d, "EXPAND")["constraints"].append(
                 copy.deepcopy(_group(d, "EXPAND")["constraints"][0])))
    _refuses("the same declaration repeated verbatim in one group",
             lambda d: _group(d, "CONTRACT")["constraints"].append(
                 copy.deepcopy(_group(d, "CONTRACT")["constraints"][0])))

    def _same_name_different_body(document: Dict[str, Any]) -> None:
        group = _group(document, "EXPAND")
        clone = copy.deepcopy(group["constraints"][0])
        clone["definition"] = "CHECK ((first_seen_request_id IS NOT NULL))"
        clone["validated"] = True
        group["constraints"].append(clone)

    _refuses("one identity carrying a materially different definition",
             _same_name_different_body)

    def _name_only_duplicate(document: Dict[str, Any]) -> None:
        group = _group(document, "EXPAND")
        group["constraints"].append(group["constraints"][0]["name"])

    _refuses("a bare-string constraint duplicating an object in the same group",
             _name_only_duplicate)

    def _bare_string_pair(document: Dict[str, Any]) -> None:
        for group in _alternatives(document):
            group["constraints"] = ["ck_same_name", "ck_same_name"]

    _refuses("two bare-string constraints of one name in a group",
             _bare_string_pair)

    # 4. Genuinely distinct alternatives inside one group remain supported.
    def _two_distinct(document: Dict[str, Any]) -> None:
        group = _group(document, "CONTRACT")
        group["constraints"].append({
            "name": "ck_client_trips_first_seen_instant_needs_request",
            "definition": ("CHECK (((first_seen_response_received_at_utc IS "
                           "NULL) OR (first_seen_request_id IS NOT NULL))))"),
            "validated": True,
        })

    _accepts("two DISTINCT constraints inside one alternative group",
             _two_distinct)

    # The scope is the group, not the relation: an expand-contract span may
    # legitimately name the same constraint in both states — NOT VALID on one
    # side and validated on the other — and rejecting that would break the
    # compatibility declaration the key exists for.
    def _same_name_across_groups(document: Dict[str, Any]) -> None:
        expand = _group(document, "EXPAND")["constraints"][0]
        contract = _group(document, "CONTRACT")["constraints"][0]
        contract["name"] = expand["name"]

    _accepts("one constraint name appearing in TWO different groups",
             _same_name_across_groups)

    # ... and not across relations either: two relation identities may carry
    # identically named constraints, which is ordinary in PostgreSQL.
    def _same_name_other_relation(document: Dict[str, Any]) -> None:
        requirement = _requirement(document, "048")
        clone = copy.deepcopy(requirement["relations"][0])
        clone["table"] = "client_trips_other"
        requirement["relations"].append(clone)

    _accepts("identically named constraints in two distinct relations",
             _same_name_other_relation)

    # 5-6. The repository's own 048 declaration, unchanged, in both states.
    document = _document()
    parsed = [r for r in parse_requirements(document)
              if r.migration.startswith("048")][0]
    groups = {g.state: g for g in parsed.relations[0].constraint_alternatives}
    _check("the current 048 EXPAND alternative still parses",
           "EXPAND" in groups
           and [c.name for c in groups["EXPAND"].constraints]
           == ["ck_client_trips_first_seen_instant_needs_request"]
           and groups["EXPAND"].constraints[0].validated is False,
           str(groups.get("EXPAND")))
    _check("the current 048 CONTRACT alternative still parses",
           "CONTRACT" in groups
           and [c.name for c in groups["CONTRACT"].constraints]
           == ["ck_client_trips_first_seen_pairing"]
           and groups["CONTRACT"].constraints[0].validated is True,
           str(groups.get("CONTRACT")))
    _check("both alternative states survive as distinct groups",
           len(groups) == 2, str(sorted(groups)))

    # The pre-existing group rules are untouched by the new one.
    _refuses("a duplicate alternative STATE is still refused",
             lambda d: _alternatives(d).append(
                 copy.deepcopy(_alternatives(d)[0])))
    _refuses("an alternative group asserting no constraint is still refused",
             lambda d: _group(d, "EXPAND").update({"constraints": []}))


# ---------------------------------------------------------------------------
# 8. THE SOURCE — unreadable is not the same failure as malformed
# ---------------------------------------------------------------------------

def _load(tree: Path):
    return load_release_requirements(tree)


def _load_refuses(label: str, tree: Path, expected_code: str) -> None:
    try:
        _load(tree)
    except SchemaPreflightError as exc:
        _check(f"{label} -> {expected_code}", exc.code == expected_code,
               f"actual: {exc.code}: {exc.detail}")
        return
    _check(f"{label} -> {expected_code}", False, "the source was ACCEPTED")


def _release_tree(root: Path, name: str, content) -> Path:
    """A release tree whose requirements file carries exactly `content`."""
    tree = root / name
    (tree / Path(SCHEMA_REQUIREMENTS_RELPATH).parent).mkdir(
        parents=True, exist_ok=True)
    path = tree / SCHEMA_REQUIREMENTS_RELPATH
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return tree


def source_classification() -> None:
    """`UNREADABLE` and `MALFORMED` answer two different operator questions.

    THE DEFECT THIS CLOSES. `except (OSError, json.JSONDecodeError)` reported a
    truncated, perfectly readable file as `RELEASE_SCHEMA_REQUIREMENTS_UNREADABLE`,
    sending an operator to check filesystem permissions for a content defect.
    A source that could not be obtained and a source that was obtained and is
    wrong are different facts, and the release classification must say which.
    """
    print("\n### SOURCE CLASSIFICATION — unreadable vs malformed")
    valid = json.dumps(_document())

    with tempfile.TemporaryDirectory(prefix="reqsrc-") as raw:
        root = Path(raw)

        # A — the source could not be obtained.
        missing = root / "absent"
        (missing / "db").mkdir(parents=True)
        try:
            requirements, capabilities, note = _load(missing)
            _check("a release with NO requirements file still predates the "
                   "mechanism and passes",
                   requirements == [] and capabilities == frozenset()
                   and note == "release_predates_schema_requirements",
                   f"{note}: {requirements}")
        except SchemaPreflightError as exc:
            _check("a release with NO requirements file still predates the "
                   "mechanism and passes", False, f"{exc.code}: {exc.detail}")

        directory = root / "directory"
        (directory / SCHEMA_REQUIREMENTS_RELPATH).mkdir(parents=True)
        _load_refuses("a directory where the requirements file must be",
                      directory, UNREADABLE)

        if os.geteuid() != 0:
            denied = _release_tree(root, "denied", valid)
            (denied / SCHEMA_REQUIREMENTS_RELPATH).chmod(0o000)
            _load_refuses("a requirements file that cannot be read",
                          denied, UNREADABLE)
            (denied / SCHEMA_REQUIREMENTS_RELPATH).chmod(0o644)
        else:
            _check("a requirements file that cannot be read -> UNREADABLE",
                   False, "not asserted: running as root would read it anyway")

        # A simulated I/O failure on a file that exists and is permitted: the
        # class of failure no fixture can arrange, asserted at the seam.
        simulated = _release_tree(root, "ioerror", valid)
        real_read_bytes = Path.read_bytes

        def _explode(self):  # noqa: ANN001
            raise OSError(5, "simulated I/O error")

        Path.read_bytes = _explode  # type: ignore[assignment]
        try:
            _load_refuses("a simulated OSError while reading", simulated,
                          UNREADABLE)
        finally:
            Path.read_bytes = real_read_bytes  # type: ignore[assignment]

        # B — the source arrived and is not a JSON document.
        _load_refuses("invalid JSON syntax",
                      _release_tree(root, "syntax", '{"version": }'),
                      MALFORMED)
        _load_refuses("a truncated JSON document",
                      _release_tree(root, "truncated", valid[: len(valid) // 2]),
                      MALFORMED)
        _load_refuses("an empty requirements file",
                      _release_tree(root, "empty", ""), MALFORMED)
        _load_refuses("a whitespace-only requirements file",
                      _release_tree(root, "blank", "   \n  "), MALFORMED)
        _load_refuses("a requirements file that is not UTF-8",
                      _release_tree(root, "binary", b"\xff\xfe{\x00"),
                      MALFORMED)
        _load_refuses("a JSON document that is not an object",
                      _release_tree(root, "array", "[]"), MALFORMED)

        # C — valid JSON, invalid declaration. Including the B2 case, which
        # must reach the RELEASE as MALFORMED and never as a schema result.
        broken = _document()
        _column(broken, COLUMN_RICH, "state")["nullable"] = "false"
        _load_refuses("valid JSON declaring a coerced boolean",
                      _release_tree(root, "coerced", json.dumps(broken)),
                      MALFORMED)

        duplicated = _document()
        _group(duplicated, "EXPAND")["constraints"].append(
            copy.deepcopy(_group(duplicated, "EXPAND")["constraints"][0]))
        _load_refuses("valid JSON duplicating a constraint in one alternative",
                      _release_tree(root, "dupalt", json.dumps(duplicated)),
                      MALFORMED)

        # D — the correct document takes the normal path.
        try:
            requirements, capabilities, note = _load(
                _release_tree(root, "valid", valid))
            _check("the repository's own document loads normally",
                   note == "release_declared_requirements"
                   and len(requirements) == len(_document()["requirements"])
                   and capabilities == frozenset(
                       _document().get("capabilities", [])),
                   f"{note}: {len(requirements)} requirement(s)")
        except SchemaPreflightError as exc:
            _check("the repository's own document loads normally", False,
                   f"{exc.code}: {exc.detail}")

    _check("UNREADABLE and MALFORMED remain distinct classifications",
           UNREADABLE != MALFORMED)


# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 78)
    print("RELEASE SCHEMA REQUIREMENTS — STRICT DECLARATION PARSING")
    print("=" * 78)
    control()
    unknown_keys()
    wrong_types()
    enum_and_structure()
    default_state_model()
    historical_forms()
    alternative_group_identity()
    source_classification()
    print("\n" + "=" * 78)
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("ALL REQUIREMENT-PARSER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
