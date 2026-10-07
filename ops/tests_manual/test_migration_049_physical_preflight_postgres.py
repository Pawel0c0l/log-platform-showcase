#!/usr/bin/env python3
"""Migration 049 must be proved PHYSICALLY at activation, not by its ledger row.

Run:
    ECO_DASHBOARD_049_PREFLIGHT_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5433/disposable \\
      python3 ops/tests_manual/test_migration_049_physical_preflight_postgres.py

THE DEFECT THIS CLOSES.
    `db/schema_requirements.json` declared migration 049 by relation, a subset
    of columns and a list of constraint NAMES. Independent review built the
    database that shape accepts: `049` recorded applied in
    `public.schema_migrations`, `provider_name` dropped, and every required
    CHECK replaced by a same-named `CHECK (true)`. Release schema preflight
    passed. The publisher then failed on its first `DeliveryLedger.load()` with
    `UndefinedColumn: provider_name`.

    So "the ledger says 049 was applied" is not release evidence, and neither is
    "a relation with roughly the right name exists". The requirement now states
    the physical contract — every column of the runtime projection with the
    attributes that matter, every CHECK/UNIQUE/PRIMARY KEY by canonical
    definition, the guard trigger by its complete binding and the guard function
    by the digest of its body — and this suite proves the gate enforces it.

WHAT IS PROVED HERE.
    1. CONTROL. The exact, untouched migration satisfies the declaration.
    2. FIDELITY. The declaration EQUALS the migration's load-bearing physical
       truth, derived from the catalog the migration itself produces. A future
       edit to 049 that is not synchronized into the requirements file fails
       here rather than silently narrowing the gate.
    3. CORRUPTION. One requirement is damaged at a time — every provider-binding
       column, column attributes, every CHECK replaced by a same-named
       `CHECK (true)`, every uniqueness contract removed or weakened, and every
       feasible way to defeat the guard trigger or its function — and the gate
       fails closed on each.
    4. CLASSIFICATION. A full release preflight over a real fleet whose client
       RECORDS 049 but whose physical schema is malformed is refused with the
       repository-standard `RELEASE_SCHEMA_CLIENT_OBJECT_MISSING`, and an
       unrecorded 049 is still refused with
       `RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING`.

DESTRUCTIVE. It creates and drops its own databases in the instance the DSN
names, so it refuses any DSN that is not loopback. No production database, no
persistent migration, no release activation, no e-mail, no provider and no
Cloudflare resource is involved anywhere in this file.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.release_schema_preflight import (  # noqa: E402
    AffectedClient,
    DEFAULT_STATE_ABSENT,
    DEFAULT_STATE_EXPRESSION,
    SchemaPreflightError,
    parse_requirements,
    requirement_defects,
    verify_schema_prerequisites,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)

#: THE HOST DELIVERY LIFECYCLE'S RUNTIME PROJECTION, pinned here as schema.
#:
#: Every statement of the lifecycle names these columns explicitly, never
#: `SELECT *`, so a column that disappears from the relation is a production
#: failure and not a degraded read. That is what makes the projection part of
#: migration 049's physical contract — and this suite has to be able to prove
#: the contract from a clean checkout of committed repository content, so the
#: projection is STATED here rather than imported from the publisher package
#: that consumes it.
#:
#: It is not documentation. `requirement_fidelity()` asserts the declaration in
#: `db/schema_requirements.json` covers exactly these columns, and
#: `corruption_defaults()` asserts this tuple EQUALS the column set migration
#: 049 actually creates in the catalog. A column added to or removed from 049
#: without synchronizing both fails here rather than in production.
RUNTIME_COLUMNS: Tuple[str, ...] = (
    "delivery_id", "operation_id", "client_id", "identity_key", "period_type",
    "period_start_date", "period_end_date", "send_scope", "subject_ref",
    "payload_digest", "recipient_identity", "recipient_email", "state",
    "external_mailer",
    "capability_id", "capability_secret", "capability_digest",
    "capability_expires_at", "bearer_generation", "bearer_persisted_at",
    "bearer_cleared_at", "provider_name", "provider_idempotency_key",
    "provider_backend_id", "provider_message_fingerprint",
    "provider_bound_capability_id", "provider_bound_bearer_generation",
    "provider_message_id", "provider_attempts", "provider_submitted_at",
    "provider_accepted_at", "remote_delivered_at", "finalized_at",
    "failure_phase", "failure_code", "failure_detail",
    "operator_action_required", "lease_owner", "lease_expires_at",
    "attempt_count", "last_run_id", "created_at", "updated_at", "metadata_json",
)

ENV = "ECO_DASHBOARD_049_PREFLIGHT_TEST_DSN"

#: THE CONTRACT IS INSTALLED BY A CHAIN, AND THE GATE IS KEYED ON ITS LAST LINK.
#:
#: 049 created the ledger and is APPLIED shared history — every enabled client
#: business database recorded it on 2026-08-19 ~19:55 CEST in its pre-review
#: physical form, and the migration runner keys on the filename, so it will
#: never execute again anywhere it already ran. The reviewed external-mailer
#: ownership contract therefore arrives as the forward migration 050, and the
#: expired-capability retirement contract as 051. The requirement in
#: `db/schema_requirements.json` is keyed on the LAST of them, because that is
#: the file whose presence makes the declared contract true: a client carrying
#: 049 alone, or 049 + 050, is refused on the ledger check before any physical
#: inspection.
#:
#: Everything below reads "migration 049" as "the schema the 049 + 050 + 051
#: chain installs" — which is what `_apply_exact_migrations` builds.
MIGRATION_049 = "049_eco_dashboard_delivery_operation.sql"
MIGRATION_050 = "050_eco_dashboard_external_mailer_ownership.sql"
#: Expired-capability retirement: `CAPABILITY_RETIRED`, the CHECK that makes a
#: retired row physically unable to hold a raw bearer, and the CHECK that stops
#: that minimisation from erasing which grant the delivery held. Forward for the
#: same reason 050 was, and the requirement is re-keyed onto it.
MIGRATION_051 = "051_eco_dashboard_capability_retirement.sql"
MIGRATION_CHAIN = (MIGRATION_049, MIGRATION_050, MIGRATION_051)
MIGRATION_FILE = MIGRATION_051
MIGRATION_PATHS = tuple(ROOT / "db" / "client_business" / name
                        for name in MIGRATION_CHAIN)
MIGRATION_PATH = MIGRATION_PATHS[0]
SCHEMA = "public"
TABLE = "eco_dashboard_delivery_operation"
QUALIFIED = f"{SCHEMA}.{TABLE}"

#: One database for the requirement/corruption matrix, one platform database and
#: one client business database for the release-classification section.
PROBE_DB = "eco049_probe"
PLATFORM_DB = "eco049_platform"
CLIENT_DB = "eco049_client"
#: The runtime probe needs its own database: `DeliveryLedger` promotes its
#: connection to autocommit, which would disarm the corruption matrix's
#: rollbacks if the two shared one.
RUNTIME_DB = "eco049_runtime"
CLIENT_ID = "635908f1-64d4-4b68-82f4-c776b19984a5"
CLIENT_CODE = "ECO049001"

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


# ---------------------------------------------------------------------------
# Disposable instance plumbing
# ---------------------------------------------------------------------------

def _base_dsn() -> str:
    return os.environ[ENV].rsplit("/", 1)[0]


def _connect(dbname: str):
    import psycopg

    return psycopg.connect(f"{_base_dsn()}/{dbname}")


def _admin():
    import psycopg

    conn = psycopg.connect(os.environ[ENV])
    conn.autocommit = True
    return conn


def _recreate_databases(*names: str) -> None:
    with _admin() as conn:
        for name in names:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{name}"')


def _drop_databases(*names: str) -> None:
    with _admin() as conn:
        for name in names:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _apply_exact_049(conn) -> None:
    """The chain, byte-for-byte, plus only what it presumes already exists.

    All three files, in order, and nothing else: each later one is a delta over
    the relation its predecessor creates and refuses to run without it, which is
    itself part of what makes the two rollout paths converge.
    """
    conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
        "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    for migration in MIGRATION_PATHS:
        conn.execute(migration.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The declaration under test
# ---------------------------------------------------------------------------

def _document() -> Dict:
    return json.loads(
        (ROOT / "db" / "schema_requirements.json").read_text(encoding="utf-8")
    )


def _raw_049() -> Dict:
    for requirement in _document()["requirements"]:
        if requirement.get("migration") == MIGRATION_FILE:
            return requirement
    raise AssertionError(f"{MIGRATION_FILE} is not declared in schema_requirements.json")


def _parsed_049():
    for requirement in parse_requirements(_document()):
        if requirement.migration == MIGRATION_FILE:
            return requirement
    raise AssertionError(f"{MIGRATION_FILE} did not survive the strict parser")


# ---------------------------------------------------------------------------
# The migration's own physical truth, read from the catalog it produces
# ---------------------------------------------------------------------------

def _catalog_columns(cur) -> Dict[str, Tuple[str, bool, Optional[str]]]:
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s
        """,
        (SCHEMA, TABLE),
    )
    return {
        str(r[0]): (str(r[1]), str(r[2]).upper() == "YES",
                    None if r[3] is None else str(r[3]))
        for r in cur.fetchall()
    }


def _catalog_constraints(cur) -> Dict[str, Tuple[str, bool, str]]:
    """Every CHECK, UNIQUE and PRIMARY KEY of the relation."""
    cur.execute(
        """
        SELECT conname, pg_get_constraintdef(oid), convalidated, contype
          FROM pg_constraint
         WHERE conrelid = %s::regclass AND contype IN ('c', 'u', 'p')
        """,
        (QUALIFIED,),
    )
    return {
        str(r[0]): (str(r[1]), bool(r[2]), str(r[3])) for r in cur.fetchall()
    }


def _catalog_indexes(cur) -> Dict[str, Tuple[str, bool, bool]]:
    """`name -> (definition, is_unique, backs_a_constraint)`."""
    cur.execute(
        """
        SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisunique,
               EXISTS (SELECT 1 FROM pg_constraint x WHERE x.conindid = i.indexrelid)
          FROM pg_index i
          JOIN pg_class c ON c.oid = i.indexrelid
         WHERE i.indrelid = %s::regclass
        """,
        (QUALIFIED,),
    )
    return {
        str(r[0]): (str(r[1]), bool(r[2]), bool(r[3])) for r in cur.fetchall()
    }


_TRIGGER_EVENT_BITS = (("INSERT", 4), ("DELETE", 8), ("UPDATE", 16),
                       ("TRUNCATE", 32))


def _catalog_triggers(cur) -> Dict[str, Dict]:
    cur.execute(
        """
        SELECT t.tgname, t.tgtype, t.tgenabled,
               fn.nspname || '.' || p.proname,
               pg_get_expr(t.tgqual, t.tgrelid),
               COALESCE(
                 (SELECT array_agg(a.attname ORDER BY a.attnum)
                    FROM unnest(t.tgattr::int2[]) AS x(attnum)
                    JOIN pg_attribute a
                      ON a.attrelid = t.tgrelid AND a.attnum = x.attnum),
                 ARRAY[]::name[])
          FROM pg_trigger t
          JOIN pg_proc p ON p.oid = t.tgfoid
          JOIN pg_namespace fn ON fn.oid = p.pronamespace
         WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal
        """,
        (QUALIFIED,),
    )
    out: Dict[str, Dict] = {}
    for name, tgtype, tgenabled, function, when, update_columns in cur.fetchall():
        value = int(tgtype)
        out[str(name)] = {
            "timing": ("INSTEAD OF" if value & 64
                       else "BEFORE" if value & 2 else "AFTER"),
            "events": sorted(e for e, bit in _TRIGGER_EVENT_BITS if value & bit),
            "level": "ROW" if value & 1 else "STATEMENT",
            "function": str(function),
            "enabled": str(tgenabled),
            "condition": None if when is None else str(when),
            "update_columns": sorted(str(c) for c in (update_columns or ())),
        }
    return out


def _catalog_function(cur, schema: str, name: str) -> Optional[Dict]:
    cur.execute(
        """
        SELECT pg_get_function_identity_arguments(p.oid), l.lanname,
               pg_get_function_result(p.oid),
               encode(sha256(convert_to(p.prosrc, 'UTF8')), 'hex')
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_language l ON l.oid = p.prolang
         WHERE n.nspname = %s AND p.proname = %s
        """,
        (schema, name),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"arguments": str(row[0]), "language": str(row[1]),
            "returns": str(row[2]), "body_sha256": str(row[3])}


# ---------------------------------------------------------------------------
# 1. CONTROL — the exact migration satisfies the declaration
# ---------------------------------------------------------------------------

def control_exact_migration(cur) -> None:
    print("\n### CONTROL — exact, untouched migration 049")
    defects = requirement_defects(cur, _parsed_049())
    _check("exact migration 049 satisfies the 049 physical requirement",
           defects == [], "; ".join(sorted(defects)))


# ---------------------------------------------------------------------------
# 2. FIDELITY — the declaration EQUALS the migration's load-bearing truth
# ---------------------------------------------------------------------------

def requirement_fidelity(cur) -> None:
    """The declaration is the contract, so it must not be able to drift.

    "Load-bearing" is defined here once, mechanically, and every part of it is
    derived from the schema migration 049 actually installs:

      * COLUMNS are exactly `RUNTIME_COLUMNS` — the projection every statement
        in the lifecycle names explicitly, pinned in this file and proved equal
        to the catalog 049 produces. A column missing from the declaration is
        one whose removal the lifecycle's own projection would discover in
        production, which is the reviewed failure verbatim.
      * CONSTRAINTS are every CHECK, UNIQUE and PRIMARY KEY of the relation.
        049 adds no decorative ones; each states either a lifecycle invariant or
        an identity contract.
      * TRIGGERS are every non-internal trigger, and FUNCTIONS every function
        those triggers bind to.
      * INDEXES that back no constraint are deliberately excluded — they are
        access-path performance. The exclusion is bounded rather than assumed:
        a unique index backing no constraint WOULD be a uniqueness contract, so
        its appearance fails this test until it is declared.

    A migration edit that adds, removes or reshapes any of those without
    synchronizing `db/schema_requirements.json` fails here.
    """
    print("\n### REQUIREMENT FIDELITY — declaration == migration 049 truth")
    raw = _raw_049()
    relations = raw["relations"]
    _check("the 049 requirement declares exactly one relation",
           len(relations) == 1, str([(r.get("schema"), r.get("table")) for r in relations]))
    relation = relations[0]
    _check("it is the delivery ledger relation",
           (relation.get("schema"), relation.get("table")) == (SCHEMA, TABLE),
           str((relation.get("schema"), relation.get("table"))))

    # --- columns -----------------------------------------------------------
    declared_columns = {c["name"]: c for c in relation.get("columns", [])}
    catalog = _catalog_columns(cur)
    missing = sorted(set(RUNTIME_COLUMNS) - set(declared_columns))
    _check("every column of the runtime projection is declared",
           not missing, f"undeclared: {missing}")
    _check("`provider_name` is declared",
           "provider_name" in declared_columns)
    extra = sorted(set(declared_columns) - set(RUNTIME_COLUMNS))
    _check("no declared column is absent from the runtime projection",
           not extra, f"declared but never projected: {extra}")

    # THE DEFAULT STATE IS PART OF THE PHYSICAL CONTRACT, IN BOTH DIRECTIONS.
    #     Independent review defeated the previous rule — "declare a default
    #     only where the migration creates one" — by ADDING one:
    #     `provider_name DEFAULT 'fake'` on a column the declaration therefore
    #     said nothing about. Preflight passed and
    #     `DeliveryLedger.ensure_operation()` was then refused by
    #     `chk_..._binding_coherent`, because the narrow INSERT names none of
    #     the six provider-binding columns and depends on every one of them
    #     staying implicitly NULL.
    #
    #     So absence of a default is declared as explicitly as presence, for
    #     every one of the 43 runtime columns, and this is where that stays
    #     synchronized with the migration: adding, removing or changing any
    #     default in 049 without updating the declaration fails here.
    parsed_columns = {c.name: c for c in _parsed_049().relations[0].columns}
    undeclared_state = sorted(
        name for name in declared_columns
        if parsed_columns.get(name) is None or parsed_columns[name].default is None
    )
    _check("every declared column states its EXACT default state, absence "
           "included",
           not undeclared_state,
           f"columns declaring no default state: {undeclared_state}")

    attribute_defects: List[str] = []
    for name, declared in sorted(declared_columns.items()):
        if name not in catalog:
            attribute_defects.append(f"{name}: absent from migration 049")
            continue
        dtype, nullable, default = catalog[name]
        if declared.get("type") != dtype:
            attribute_defects.append(
                f"{name}: type {declared.get('type')!r} != {dtype!r}")
        if declared.get("nullable") != nullable:
            attribute_defects.append(
                f"{name}: nullable {declared.get('nullable')!r} != {nullable!r}")
        state = parsed_columns.get(name).default if name in parsed_columns else None
        if state is None:
            continue  # already reported above
        if default is None:
            if state.state != DEFAULT_STATE_ABSENT:
                attribute_defects.append(
                    f"{name}: migration 049 creates no default, declaration "
                    f"requires {state.expression!r}")
        elif state.state != DEFAULT_STATE_EXPRESSION:
            attribute_defects.append(
                f"{name}: migration 049 creates DEFAULT {default!r}, "
                "declaration requires no default")
        elif " ".join(state.expression.split()) != " ".join(default.split()):
            attribute_defects.append(
                f"{name}: default {state.expression!r} != {default!r}")
    _check("every declared column attribute equals migration 049's catalog truth",
           not attribute_defects, "; ".join(attribute_defects))

    catalog_no_default = sorted(n for n, (_t, _n, d) in catalog.items() if d is None)
    declared_no_default = sorted(
        name for name, column in parsed_columns.items()
        if column.default is not None
        and column.default.state == DEFAULT_STATE_ABSENT
    )
    _check("the set of columns required to have NO default is exactly "
           "migration 049's",
           catalog_no_default == declared_no_default,
           f"catalog={catalog_no_default} declared={declared_no_default}")

    # --- constraints -------------------------------------------------------
    declared_constraints = {
        (c if isinstance(c, str) else c["name"]): (
            None if isinstance(c, str) else c.get("definition"),
            True if isinstance(c, str) else c.get("validated", True),
        )
        for c in relation.get("constraints", [])
    }
    catalog_constraints = _catalog_constraints(cur)
    undeclared = sorted(set(catalog_constraints) - set(declared_constraints))
    _check("every CHECK, UNIQUE and PRIMARY KEY of migration 049 is declared",
           not undeclared, f"undeclared: {undeclared}")
    unknown = sorted(set(declared_constraints) - set(catalog_constraints))
    _check("no declared constraint is absent from migration 049",
           not unknown, f"declared but not created: {unknown}")

    definition_defects: List[str] = []
    for name in sorted(set(declared_constraints) & set(catalog_constraints)):
        declared_definition, declared_validated = declared_constraints[name]
        definition, validated, _contype = catalog_constraints[name]
        if declared_definition is None:
            definition_defects.append(f"{name}: declared by NAME ONLY")
        elif " ".join(declared_definition.split()) != " ".join(definition.split()):
            definition_defects.append(
                f"{name}: definition != {definition}")
        if bool(declared_validated) != validated:
            definition_defects.append(
                f"{name}: validated {declared_validated!r} != {validated!r}")
    _check("every declared constraint pins the canonical definition and "
           "validation status",
           not definition_defects, "; ".join(definition_defects))

    unique_names = sorted(
        name for name, (_d, _v, contype) in catalog_constraints.items()
        if contype in ("u", "p")
    )
    _check("every uniqueness contract of migration 049 is declared by definition",
           all(declared_constraints.get(n, (None, None))[0] is not None
               for n in unique_names),
           str(unique_names))

    # --- indexes -----------------------------------------------------------
    stray_unique = sorted(
        name for name, (_definition, is_unique, backs) in
        _catalog_indexes(cur).items() if is_unique and not backs
    )
    _check("migration 049 creates no uniqueness contract outside a constraint",
           not stray_unique,
           f"unique indexes backing no constraint must be declared: {stray_unique}")

    # --- triggers ----------------------------------------------------------
    declared_triggers = {t["name"]: t for t in relation.get("triggers", [])}
    catalog_triggers = _catalog_triggers(cur)
    _check("every non-internal trigger of migration 049 is declared",
           set(catalog_triggers) == set(declared_triggers),
           f"catalog={sorted(catalog_triggers)} declared={sorted(declared_triggers)}")

    binding_defects: List[str] = []
    for name in sorted(set(declared_triggers) & set(catalog_triggers)):
        declared = declared_triggers[name]
        observed = catalog_triggers[name]
        for field in ("timing", "level", "function"):
            if declared.get(field) != observed[field]:
                binding_defects.append(
                    f"{name}.{field}: {declared.get(field)!r} != {observed[field]!r}")
        if sorted(declared.get("events", [])) != observed["events"]:
            binding_defects.append(
                f"{name}.events: {declared.get('events')!r} != {observed['events']!r}")
        if declared.get("enabled", True) is not True:
            binding_defects.append(f"{name}: declared as not-required-enabled")
        if observed["enabled"] not in ("O", "A"):
            binding_defects.append(f"{name}: migration leaves tgenabled="
                                   f"{observed['enabled']}")
        if (declared.get("condition") or None) != observed["condition"]:
            binding_defects.append(
                f"{name}.condition: {declared.get('condition')!r} != "
                f"{observed['condition']!r}")
        if sorted(declared.get("update_columns") or []) != observed["update_columns"]:
            binding_defects.append(
                f"{name}.update_columns: {declared.get('update_columns')!r} != "
                f"{observed['update_columns']!r}")
    _check("every declared trigger binding equals migration 049's catalog truth",
           not binding_defects, "; ".join(binding_defects))

    # --- functions ---------------------------------------------------------
    declared_functions = {
        f"{f['schema']}.{f['name']}": f for f in raw.get("functions", [])
    }
    bound = {observed["function"] for observed in catalog_triggers.values()}
    _check("every function a declared trigger binds to is itself declared",
           bound <= set(declared_functions),
           f"bound={sorted(bound)} declared={sorted(declared_functions)}")

    function_defects_found: List[str] = []
    for identity, declared in sorted(declared_functions.items()):
        observed = _catalog_function(cur, declared["schema"], declared["name"])
        if observed is None:
            function_defects_found.append(f"{identity}: migration creates no such function")
            continue
        for field in ("arguments", "language", "returns", "body_sha256"):
            if declared.get(field) != observed[field]:
                function_defects_found.append(
                    f"{identity}.{field}: {declared.get(field)!r} != "
                    f"{observed[field]!r}")
    _check("every declared function equals migration 049's catalog truth, "
           "including the digest of its body",
           not function_defects_found, "; ".join(function_defects_found))
    _check("the guard function's implementation is pinned, not only its name",
           all(f.get("body_sha256") for f in declared_functions.values()),
           str(sorted(declared_functions)))


# ---------------------------------------------------------------------------
# 2b. THE REVIEWED DEFECT, REPLAYED — the gate is strictly stronger than before
# ---------------------------------------------------------------------------

#: The declaration this task replaced, kept verbatim as a historical fixture.
#: It is the shape independent review defeated: relation + column SUBSET +
#: constraint NAMES, with `provider_name` absent from the subset entirely.
PRE_FIX_DECLARATION: Dict = {
    "version": "log-platform-schema-requirements/1",
    "requirements": [{
        "migration": MIGRATION_FILE,
        "scope": "client_business",
        "milestone": "ECO-DASH-V1",
        "reason": "the pre-fix declaration, retained only as a regression fixture",
        "relations": [{
            "schema": SCHEMA,
            "table": TABLE,
            "columns": [
                {"name": "operation_id", "type": "text", "nullable": False},
                {"name": "state", "type": "text", "nullable": False},
                {"name": "recipient_email", "type": "text", "nullable": False},
                {"name": "capability_secret", "type": "text", "nullable": True},
                {"name": "provider_idempotency_key", "type": "text",
                 "nullable": True},
                {"name": "provider_backend_id", "type": "text", "nullable": True},
                {"name": "provider_message_fingerprint", "type": "text",
                 "nullable": True},
                {"name": "provider_bound_capability_id", "type": "text",
                 "nullable": True},
                {"name": "provider_bound_bearer_generation", "type": "integer",
                 "nullable": True},
                {"name": "lease_owner", "type": "text", "nullable": True},
                {"name": "lease_expires_at", "type": "timestamp with time zone",
                 "nullable": True},
            ],
            "constraints": [
                {"name": "chk_eco_dashboard_delivery_operation_provider_key_present"},
                {"name": "chk_eco_dashboard_delivery_operation_binding_coherent"},
                {"name": "chk_eco_dashboard_delivery_operation_provider_unbound"},
                {"name": "chk_eco_dashboard_delivery_operation_lease_pairing"},
                {"name": "chk_eco_dashboard_delivery_operation_bearer_present"},
                {"name": "chk_eco_dashboard_delivery_operation_no_bearer_in_diagnostics"},
            ],
        }],
    }],
}

#: The corruption review actually built. `provider_name` gone, and every
#: constraint the old declaration named back in place under its own name,
#: asserting nothing.
REVIEWED_CORRUPTION: List[str] = (
    [f"ALTER TABLE {QUALIFIED} DROP COLUMN provider_name CASCADE"]
    + [statement
       for name in (
           "chk_eco_dashboard_delivery_operation_provider_key_present",
           "chk_eco_dashboard_delivery_operation_binding_coherent",
           "chk_eco_dashboard_delivery_operation_provider_unbound",
           "chk_eco_dashboard_delivery_operation_lease_pairing",
           "chk_eco_dashboard_delivery_operation_bearer_present",
           "chk_eco_dashboard_delivery_operation_no_bearer_in_diagnostics",
       )
       for statement in (
           f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT IF EXISTS {name}",
           f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT {name} CHECK (true)",
       )]
)


def _requirement_of(document: Dict):
    for requirement in parse_requirements(document):
        if requirement.migration == MIGRATION_FILE:
            return requirement
    raise AssertionError(f"{MIGRATION_FILE} missing from a fixture document")


def reviewed_defect_replay(cur, conn) -> None:
    """Both directions, so "the gate is green" cannot mean "the gate is blind".

    Green results everywhere else would be equally consistent with a requirement
    that asserts nothing at all. This replays the exact reviewed corruption
    against the OLD declaration and the NEW one on the same database: the old
    one must accept it — that is the defect, reproduced — and the new one must
    refuse it naming `provider_name`.
    """
    print("\n### THE REVIEWED DEFECT — replayed against both declarations")
    _check("the pre-fix declaration accepted the exact migration too",
           _requirement_defects_of(cur, PRE_FIX_DECLARATION) == [],
           "the fixture no longer describes the declaration that was replaced")
    try:
        for statement in REVIEWED_CORRUPTION:
            cur.execute(statement)
        old_defects = _requirement_defects_of(cur, PRE_FIX_DECLARATION)
        new_defects = requirement_defects(cur, _parsed_049())
        _check("the PRE-FIX declaration accepts the reviewed corruption "
               "(the defect, reproduced)",
               old_defects == [], f"unexpectedly refused: {sorted(old_defects)}")
        _check("the CURRENT declaration refuses it, naming provider_name",
               any("column_absent" in d and "provider_name" in d
                   for d in new_defects),
               f"defects={sorted(new_defects)}")
        _check("the CURRENT declaration also catches the same-named CHECK (true) "
               "replacements",
               sum(1 for d in new_defects
                   if d.startswith("constraint_definition_mismatch")) >= 5,
               f"defects={sorted(new_defects)}")
    finally:
        conn.rollback()


def _requirement_defects_of(cur, document: Dict) -> List[str]:
    return requirement_defects(cur, _requirement_of(document))


# ---------------------------------------------------------------------------
# 3. CORRUPTION MATRIX — one damaged requirement at a time
# ---------------------------------------------------------------------------

#: Every corruption runs inside its own transaction and is rolled back, so each
#: one starts from the exact migration and no case can mask another. PostgreSQL
#: makes DDL transactional, which is what lets the matrix stay this direct.
NOOP_GUARD = """
CREATE OR REPLACE FUNCTION public.eco_dashboard_delivery_operation_guard()
RETURNS trigger LANGUAGE plpgsql AS $noop$
BEGIN
  RETURN NEW;
END;
$noop$
"""

FOREIGN_GUARD = """
CREATE OR REPLACE FUNCTION public.eco_dashboard_impostor_guard()
RETURNS trigger LANGUAGE plpgsql AS $impostor$
BEGIN
  RETURN NEW;
END;
$impostor$
"""


def _corruption(cur, conn, label: str, statements: List[str],
                expected_defect_prefix: str) -> None:
    """Damage one requirement, assert the gate refuses, roll back."""
    try:
        for statement in statements:
            cur.execute(statement)
        defects = requirement_defects(cur, _parsed_049())
        matched = [d for d in defects if d.startswith(expected_defect_prefix)]
        _check(f"{label} -> preflight fails closed ({expected_defect_prefix})",
               bool(defects) and bool(matched),
               f"defects={sorted(defects)[:4]}")
    finally:
        conn.rollback()


def corruption_columns(cur, conn) -> None:
    print("\n### CORRUPTION — columns")
    provider_binding = (
        "provider_idempotency_key", "provider_name", "provider_backend_id",
        "provider_message_fingerprint", "provider_bound_capability_id",
        "provider_bound_bearer_generation",
    )
    for column in provider_binding:
        _corruption(
            cur, conn, f"provider-binding column {column} removed",
            [f"ALTER TABLE {QUALIFIED} DROP COLUMN {column} CASCADE"],
            f"column_absent:{QUALIFIED}.{column}",
        )
    # The rest of the runtime projection is load-bearing for exactly the same
    # reason, checked over the whole set rather than a sampled few.
    for column in RUNTIME_COLUMNS:
        if column in provider_binding:
            continue
        _corruption(
            cur, conn, f"projected column {column} removed",
            [f"ALTER TABLE {QUALIFIED} DROP COLUMN {column} CASCADE"],
            f"column_absent:{QUALIFIED}.{column}",
        )
    _corruption(
        cur, conn, "provider_bound_bearer_generation retyped to text",
        [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT "
         "chk_eco_dashboard_delivery_operation_bound_generation",
         f"ALTER TABLE {QUALIFIED} ALTER COLUMN "
         "provider_bound_bearer_generation TYPE text"],
        f"column_type_mismatch:{QUALIFIED}.provider_bound_bearer_generation",
    )
    _corruption(
        cur, conn, "state loses NOT NULL",
        [f"ALTER TABLE {QUALIFIED} ALTER COLUMN state DROP NOT NULL"],
        f"column_nullability_mismatch:{QUALIFIED}.state",
    )
    _corruption(
        cur, conn, "capability_secret made NOT NULL",
        [f"ALTER TABLE {QUALIFIED} ALTER COLUMN capability_secret SET NOT NULL"],
        f"column_nullability_mismatch:{QUALIFIED}.capability_secret",
    )


#: A default that is semantically non-NULL, per PostgreSQL data type. Used to
#: ADD a default to a column migration 049 deliberately leaves without one.
_FAKE_DEFAULT_BY_TYPE: Dict[str, str] = {
    "text": "'fake'",
    "uuid": "'9752f2ac-3d6d-4ad6-863e-9f5a718922f2'::uuid",
    "integer": "7",
    "boolean": "true",
    "timestamp with time zone": "now()",
    "date": "CURRENT_DATE",
    "jsonb": "'{\"injected\": true}'::jsonb",
}

#: A MATERIALLY different value of the same type, used to alter a default the
#: migration does create. Never equal to the migration's own.
_ALTERED_DEFAULT_BY_TYPE: Dict[str, str] = {
    "text": "'corrupted'::text",
    "uuid": "'98bee240-7429-46b1-86ab-cccefb40e453'::uuid",
    "integer": "9",
    "boolean": "true",
    "timestamp with time zone": "'2000-01-01T00:00:00+00'::timestamptz",
    "date": "'2000-01-01'::date",
    "jsonb": "'{\"x\": 1}'::jsonb",
}

#: An EQUIVALENT source spelling of the same default, which PostgreSQL
#: canonicalizes back to the identical catalog entry. The gate must not fail on
#: source formatting — only on a materially different expression.
_EQUIVALENT_SOURCE_FORM: Dict[str, str] = {
    "delivery_id": "(gen_random_uuid())",
    "send_scope": "CAST('normal' AS text)",
    "bearer_generation": "(0)",
    "provider_attempts": "((0))",
    "operator_action_required": "FALSE",
    "attempt_count": "(0)",
    "created_at": "(now())",
    "updated_at": "(now())",
    "metadata_json": "CAST('{}' AS jsonb)",
}


def corruption_defaults(cur, conn) -> None:
    """EVERY column's default state, in every direction it can be broken.

    THE REVIEWED DEFECT, GENERALIZED. `provider_name DEFAULT 'fake'` passed the
    previous gate because a requirement could not say "this column must have no
    default": `None` already meant "not checked". The narrow lifecycle INSERT
    names 13 of the 43 columns, so the other 30 are either filled from a
    declared default or must stay implicitly NULL — and an ADDED default on any
    of the latter changes what that INSERT writes. It is load-bearing schema,
    and the matrix below proves it is enforced as such, one column at a time.
    """
    print("\n### CORRUPTION — column DEFAULT state, all 43 columns")
    catalog = _catalog_columns(cur)
    no_default = [n for n, (_t, _n, d) in sorted(catalog.items()) if d is None]
    with_default = [n for n, (_t, _n, d) in sorted(catalog.items()) if d is not None]
    _check("the matrix covers all 43 runtime columns",
           sorted(no_default + with_default) == sorted(RUNTIME_COLUMNS),
           f"catalog={sorted(catalog)}")

    # --- columns migration 049 leaves WITHOUT a default --------------------
    for column in no_default:
        dtype = catalog[column][0]
        expression = _FAKE_DEFAULT_BY_TYPE.get(dtype)
        if expression is None:
            _check(f"{column}: no fake default is defined for type {dtype}",
                   False, "extend _FAKE_DEFAULT_BY_TYPE")
            continue
        _corruption(
            cur, conn,
            f"{column} ({dtype}) given a default migration 049 never creates",
            [f"ALTER TABLE {QUALIFIED} ALTER COLUMN {column} "
             f"SET DEFAULT {expression}"],
            f"column_default_present:{QUALIFIED}.{column}",
        )
    # The exact reviewed value, named explicitly so the historical defect has a
    # test that says its own name.
    _corruption(
        cur, conn, "THE REVIEWED DEFECT: provider_name DEFAULT 'fake'",
        [f"ALTER TABLE {QUALIFIED} ALTER COLUMN provider_name "
         "SET DEFAULT 'fake'"],
        f"column_default_present:{QUALIFIED}.provider_name",
    )

    # --- columns migration 049 gives an explicit default -------------------
    for column in with_default:
        dtype, _nullable, default = catalog[column]
        _corruption(
            cur, conn, f"{column} loses the default the lifecycle INSERT needs",
            [f"ALTER TABLE {QUALIFIED} ALTER COLUMN {column} DROP DEFAULT"],
            f"column_default_mismatch:{QUALIFIED}.{column}",
        )
        altered = _ALTERED_DEFAULT_BY_TYPE.get(dtype)
        if altered is None:
            _check(f"{column}: no altered default is defined for type {dtype}",
                   False, "extend _ALTERED_DEFAULT_BY_TYPE")
            continue
        _corruption(
            cur, conn, f"{column} default materially changed",
            [f"ALTER TABLE {QUALIFIED} ALTER COLUMN {column} "
             f"SET DEFAULT {altered}"],
            f"column_default_mismatch:{QUALIFIED}.{column}",
        )

    # --- harmless equivalent catalog representations still pass ------------
    for column in with_default:
        equivalent = _EQUIVALENT_SOURCE_FORM.get(column)
        if equivalent is None:
            _check(f"{column}: no equivalent source form is defined", False,
                   "extend _EQUIVALENT_SOURCE_FORM")
            continue
        try:
            cur.execute(f"ALTER TABLE {QUALIFIED} ALTER COLUMN {column} "
                        f"SET DEFAULT {equivalent}")
            reprinted = _catalog_columns(cur)[column][2]
            defects = requirement_defects(cur, _parsed_049())
            _check(f"{column}: equivalent source form {equivalent} still passes",
                   defects == [],
                   f"catalog reprints {reprinted!r} (exact 049: "
                   f"{catalog[column][2]!r}); defects={sorted(defects)[:3]}")
        finally:
            conn.rollback()


#: THE ONLY INSERT THE HOST DELIVERY LIFECYCLE EVER ISSUES, as SQL.
#:
#: A row is created by naming these 13 columns and no others. The remaining 30
#: are either filled from a default migration 049 declares or must stay
#: implicitly NULL — which is precisely why an ADDED default is a defect and
#: not a convenience. `ON CONFLICT DO NOTHING` with no conflict target is
#: reproduced because it is load-bearing: a concurrent duplicate violates
#: `uq_..._operation` and `uq_..._identity` at the same time, and naming one
#: arbiter would let the other surface as an error instead of the "somebody
#: else already created it" that it is. The explicit RETURNING projection is
#: reproduced for the same reason the lifecycle uses one: the row it reads back
#: must be nameable column by column.
#:
#: Stated as SQL rather than driven through the publisher package, so this
#: proof needs nothing outside committed repository content. The shape — not
#: the Python that issues it — is the part of the runtime contract migration
#: 049 has to satisfy.
_NARROW_LIFECYCLE_INSERT_COLUMNS = (
    "operation_id", "client_id", "identity_key", "period_type",
    "period_start_date", "period_end_date", "send_scope", "subject_ref",
    "payload_digest", "recipient_identity", "recipient_email", "state",
    "last_run_id",
)

_NARROW_LIFECYCLE_INSERT_VALUES = (
    "op0000000000000049probe",           # operation_id
    CLIENT_ID,                           # client_id
    "driver-000049",                     # identity_key
    "weekly",                            # period_type
    "2026-01-05",                        # period_start_date
    "2026-01-12",                        # period_end_date
    "normal",                            # send_scope
    "subject-049",                       # subject_ref
    "a" * 64,                            # payload_digest
    "driver-000049",                     # recipient_identity
    "driver@example.invalid",            # recipient_email
    "PREPARED",                          # state
    None,                                # last_run_id
)


def _issue_narrow_lifecycle_insert(conn) -> Tuple[Optional[str], Optional[str]]:
    """Run the lifecycle INSERT once. Returns `(state_written, error)`."""
    columns = ", ".join(_NARROW_LIFECYCLE_INSERT_COLUMNS)
    placeholders = ",".join(["%s"] * len(_NARROW_LIFECYCLE_INSERT_COLUMNS))
    projection = ", ".join(RUNTIME_COLUMNS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {QUALIFIED} ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT DO NOTHING RETURNING {projection}",
                _NARROW_LIFECYCLE_INSERT_VALUES,
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - the failure IS the evidence
        conn.rollback()
        return None, f"{type(exc).__name__}: {exc}"
    if row is None:
        return None, "ON CONFLICT DO NOTHING: no row was created"
    state_index = RUNTIME_COLUMNS.index("state")
    return str(row[state_index]), None


def runtime_narrow_insert() -> None:
    """The gate's verdict must agree with what the runtime INSERT actually does.

    Two directions on its OWN database, because either alone is consistent with
    a gate that decides nothing: the exact migration must let the narrow
    lifecycle INSERT — the only INSERT the publisher ever issues — succeed, and
    the reviewed `provider_name DEFAULT 'fake'` must be refused by PREFLIGHT,
    i.e. before any runtime is attempted.

    A separate database rather than the corruption matrix's, deliberately: this
    section COMMITS a row, which on the matrix's connection would silently
    disarm every `conn.rollback()` after it.
    """
    print("\n### RUNTIME — the narrow lifecycle INSERT on exact 049")

    _recreate_databases(RUNTIME_DB)
    try:
        with _connect(RUNTIME_DB) as conn:
            _apply_exact_049(conn)
            conn.commit()
            with conn.cursor() as cur:
                _check("runtime probe starts from a schema preflight accepts",
                       requirement_defects(cur, _parsed_049()) == [])
            # The projection the lifecycle reads back must be nameable against
            # the schema 049 installs — the reviewed production failure was a
            # `SELECT` of exactly these columns raising `UndefinedColumn`.
            with conn.cursor() as cur:
                projection_error = None
                try:
                    cur.execute(
                        f"SELECT {', '.join(RUNTIME_COLUMNS)} FROM {QUALIFIED} "
                        "WHERE false")
                except Exception as exc:  # noqa: BLE001
                    projection_error = f"{type(exc).__name__}: {exc}"
                finally:
                    conn.rollback()
            _check("exact 049: the whole runtime projection is selectable",
                   projection_error is None, str(projection_error))

        with _connect(RUNTIME_DB) as runtime_conn:
            state, error = _issue_narrow_lifecycle_insert(runtime_conn)
            _check("exact 049: the narrow lifecycle INSERT succeeds",
                   error is None and state == "PREPARED",
                   f"state={state!r} error={error}")

        # Now the reviewed corruption, on a fresh copy of the same schema.
        _recreate_databases(RUNTIME_DB)
        with _connect(RUNTIME_DB) as conn:
            _apply_exact_049(conn)
            conn.execute(f"ALTER TABLE {QUALIFIED} ALTER COLUMN provider_name "
                         "SET DEFAULT 'fake'")
            conn.commit()
            with conn.cursor() as cur:
                defects = requirement_defects(cur, _parsed_049())
            _check("provider_name DEFAULT 'fake': preflight refuses BEFORE "
                   "runtime is attempted",
                   any(d.startswith(
                       f"column_default_present:{QUALIFIED}.provider_name")
                       for d in defects),
                   f"defects={sorted(defects)[:3]}")
        with _connect(RUNTIME_DB) as runtime_conn:
            _state, runtime_error = _issue_narrow_lifecycle_insert(runtime_conn)
            _check("and that corruption really does break the narrow INSERT",
                   runtime_error is not None
                   and "binding_coherent" in runtime_error,
                   f"runtime outcome: {runtime_error}")
    finally:
        _drop_databases(RUNTIME_DB)



def corruption_checks(cur, conn) -> None:
    """THE reviewed defect: a same-named constraint that asserts nothing."""
    print("\n### CORRUPTION — every CHECK replaced by a same-named CHECK (true)")
    for constraint in _parsed_049().relations[0].constraints:
        name = constraint.name
        definition = constraint.definition or ""
        if not definition.startswith("CHECK"):
            continue
        _corruption(
            cur, conn, f"{name} replaced by a same-named CHECK (true)",
            [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT {name}",
             f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT {name} CHECK (true)"],
            f"constraint_definition_mismatch:{QUALIFIED}.{name}",
        )
        _corruption(
            cur, conn, f"{name} dropped outright",
            [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT {name}"],
            f"constraint_absent:{QUALIFIED}.{name}",
        )
    # A materially weaker predicate that is not literally `true`: the binding
    # coherence CHECK reduced to "the key and the name agree", which is exactly
    # the partial binding the migration exists to forbid.
    weaker = "chk_eco_dashboard_delivery_operation_binding_coherent"
    _corruption(
        cur, conn, f"{weaker} weakened to a partial binding predicate",
        [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT {weaker}",
         f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT {weaker} CHECK "
         "((provider_idempotency_key IS NULL) = (provider_name IS NULL))"],
        f"constraint_definition_mismatch:{QUALIFIED}.{weaker}",
    )
    # A same-named CHECK added back NOT VALID, i.e. an interrupted repair.
    not_valid = "chk_eco_dashboard_delivery_operation_provider_key_present"
    definition = next(
        c.definition for c in _parsed_049().relations[0].constraints
        if c.name == not_valid
    )
    _corruption(
        cur, conn, f"{not_valid} recreated NOT VALID",
        [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT {not_valid}",
         f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT {not_valid} {definition} NOT VALID"],
        f"constraint_not_validated:{QUALIFIED}.{not_valid}",
    )


def corruption_uniqueness(cur, conn) -> None:
    print("\n### CORRUPTION — uniqueness")
    unique = {
        "uq_eco_dashboard_delivery_operation_operation": "(operation_id)",
        "uq_eco_dashboard_delivery_operation_provider_key":
            "(provider_idempotency_key)",
        "uq_eco_dashboard_delivery_operation_identity":
            "(client_id, identity_key, period_type, period_start_date, "
            "period_end_date, send_scope)",
    }
    for name, columns in unique.items():
        _corruption(
            cur, conn, f"{name} removed",
            [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT {name}"],
            f"constraint_absent:{QUALIFIED}.{name}",
        )
        # The adversarial representation PostgreSQL does allow: the name
        # survives as a NON-UNIQUE index, so anything looking for "an index with
        # about the right name" is satisfied while nothing is unique any more.
        _corruption(
            cur, conn, f"{name} replaced by a same-named NON-UNIQUE index",
            [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT {name}",
             f"CREATE INDEX {name} ON {QUALIFIED} {columns}"],
            f"constraint_absent:{QUALIFIED}.{name}",
        )
        _corruption(
            cur, conn, f"{name} replaced by a same-named UNIQUE over wrong columns",
            [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT {name}",
             f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT {name} UNIQUE (last_run_id)"],
            f"constraint_definition_mismatch:{QUALIFIED}.{name}",
        )
    _corruption(
        cur, conn, "the primary key re-keyed onto another column",
        [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT "
         "eco_dashboard_delivery_operation_pkey",
         f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT "
         "eco_dashboard_delivery_operation_pkey PRIMARY KEY (operation_id)"],
        f"constraint_definition_mismatch:{QUALIFIED}."
        "eco_dashboard_delivery_operation_pkey",
    )


def corruption_trigger_and_function(cur, conn) -> None:
    print("\n### CORRUPTION — guard trigger and guard function")
    trigger = "trg_eco_dashboard_delivery_operation_guard"
    function = "public.eco_dashboard_delivery_operation_guard"
    _corruption(
        cur, conn, "guard trigger removed",
        [f"DROP TRIGGER {trigger} ON {QUALIFIED}"],
        f"trigger_absent:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger disabled",
        [f"ALTER TABLE {QUALIFIED} DISABLE TRIGGER {trigger}"],
        f"trigger_disabled:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger set to fire only under replica role",
        [f"ALTER TABLE {QUALIFIED} ENABLE REPLICA TRIGGER {trigger}"],
        f"trigger_disabled:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger rebound to a same-shaped no-op function",
        [FOREIGN_GUARD,
         f"DROP TRIGGER {trigger} ON {QUALIFIED}",
         f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {QUALIFIED} "
         "FOR EACH ROW EXECUTE FUNCTION public.eco_dashboard_impostor_guard()"],
        f"trigger_function_mismatch:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger retimed to AFTER",
        [f"DROP TRIGGER {trigger} ON {QUALIFIED}",
         f"CREATE TRIGGER {trigger} AFTER INSERT OR UPDATE ON {QUALIFIED} "
         f"FOR EACH ROW EXECUTE FUNCTION {function}()"],
        f"trigger_timing_mismatch:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger narrowed to INSERT only",
        [f"DROP TRIGGER {trigger} ON {QUALIFIED}",
         f"CREATE TRIGGER {trigger} BEFORE INSERT ON {QUALIFIED} "
         f"FOR EACH ROW EXECUTE FUNCTION {function}()"],
        f"trigger_events_mismatch:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger narrowed to UPDATE OF one column",
        [f"DROP TRIGGER {trigger} ON {QUALIFIED}",
         f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE OF state ON {QUALIFIED} "
         f"FOR EACH ROW EXECUTE FUNCTION {function}()"],
        f"trigger_update_columns_mismatch:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger given a WHEN condition that never holds",
        [f"DROP TRIGGER {trigger} ON {QUALIFIED}",
         f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {QUALIFIED} "
         f"FOR EACH ROW WHEN (false) EXECUTE FUNCTION {function}()"],
        f"trigger_condition_mismatch:{QUALIFIED}.{trigger}",
    )
    _corruption(
        cur, conn, "guard trigger demoted to STATEMENT level",
        [f"DROP TRIGGER {trigger} ON {QUALIFIED}",
         f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {QUALIFIED} "
         f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()"],
        f"trigger_level_mismatch:{QUALIFIED}.{trigger}",
    )
    # THE case a structural check cannot see. Same name, same signature, same
    # trigger, same everything — and the guard enforces nothing.
    _corruption(
        cur, conn, "guard function body replaced by a no-op",
        [NOOP_GUARD],
        f"function_body_mismatch:{function}()",
    )
    _corruption(
        cur, conn, "guard function removed (cascading its trigger)",
        [f"DROP FUNCTION {function}() CASCADE"],
        f"function_absent:{function}()",
    )


# ---------------------------------------------------------------------------
# 4. CLASSIFICATION — the full release gate over a real fleet
# ---------------------------------------------------------------------------

def _build_platform() -> None:
    with _connect(PLATFORM_DB) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS workflow_a_control")
        conn.execute(
            """
            CREATE TABLE workflow_a_control.client_account (
              client_id UUID PRIMARY KEY,
              client_code TEXT,
              client_name TEXT,
              client_db_host TEXT NOT NULL,
              client_db_port INTEGER NOT NULL,
              client_db_name TEXT NOT NULL,
              enabled BOOLEAN NOT NULL DEFAULT TRUE
            )
            """
        )
        conn.execute(
            "INSERT INTO workflow_a_control.client_account "
            "(client_id, client_code, client_name, client_db_host, "
            " client_db_port, client_db_name, enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, TRUE)",
            (CLIENT_ID, CLIENT_CODE, "Eco 049 probe", _host(), _port(), CLIENT_DB),
        )
        conn.commit()


def _host() -> str:
    return os.environ[ENV].split("@")[1].split(":")[0]


def _port() -> int:
    return int(os.environ[ENV].split("@")[1].split(":")[1].split("/")[0])


def _build_client(*, record_migration: bool = True) -> None:
    with _connect(CLIENT_DB) as conn:
        _apply_exact_049(conn)
        if record_migration:
            for filename in MIGRATION_CHAIN:
                conn.execute(
                    "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (filename,),
                )
        conn.commit()


def _release_tree(tmp_root: Path) -> Path:
    """A release tree declaring only what this minimal fleet can carry.

    The repository declares 047, 048 and platform prerequisites this two-database
    fixture deliberately does not build, and refusing on those would say nothing
    about the property under test. The 049 requirement is carried through
    VERBATIM — narrowing it here would be narrowing the very declaration this
    suite exists to exercise.
    """
    document = _document()
    document["requirements"] = [
        r for r in document["requirements"] if r.get("migration") == MIGRATION_FILE
    ]
    tree = tmp_root / "release"
    (tree / "db").mkdir(parents=True, exist_ok=True)
    (tree / "db" / "schema_requirements.json").write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )
    return tree


def _run_gate(tree: Path):
    return verify_schema_prerequisites(
        release_tree=tree,
        release_id="eco049-probe",
        platform_conn_factory=lambda: _connect(PLATFORM_DB),
        client_conn_factory=lambda client: _connect(client.db_name),
    )


def _expect_refusal(label: str, expected_code: str, tree: Path,
                    expect_in_detail: str = "") -> None:
    try:
        _run_gate(tree)
    except SchemaPreflightError as exc:
        ok = exc.code == expected_code and (
            not expect_in_detail or expect_in_detail in exc.detail
        )
        _check(f"{label} -> {expected_code}", ok, f"actual: {exc.code}: {exc.detail}")
        return
    _check(f"{label} -> {expected_code}", False, "the gate PASSED")


def release_classification(tmp_root: Path) -> None:
    print("\n### RELEASE CLASSIFICATION — recorded 049 is not evidence")
    tree = _release_tree(tmp_root)

    _recreate_databases(PLATFORM_DB, CLIENT_DB)
    _build_platform()
    _build_client()
    report = None
    try:
        report = _run_gate(tree)
    except SchemaPreflightError as exc:
        _check("a fleet carrying the exact migration 049 passes activation",
               False, f"{exc.code}: {exc.detail}")
    if report is not None:
        _check("a fleet carrying the exact migration 049 passes activation", True)
        _check("the client check records no physical defect",
               all(not c["physical_defects"] for c in report.client_checks),
               str(report.client_checks))

    # The key new release invariant: recorded and physically malformed.
    for label, statements, needle in (
        ("provider_name removed",
         [f"ALTER TABLE {QUALIFIED} DROP COLUMN provider_name CASCADE"],
         "column_absent:public.eco_dashboard_delivery_operation.provider_name"),
        # THE ADDED-DEFAULT CASE. Nothing is missing from this schema: a
        # column simply gained a default the migration never created, and the
        # narrow lifecycle INSERT that depends on it staying implicitly NULL is
        # then refused at runtime. Activation must be blocked here, not there.
        ("provider_name given DEFAULT 'fake'",
         [f"ALTER TABLE {QUALIFIED} ALTER COLUMN provider_name "
          "SET DEFAULT 'fake'"],
         "column_default_present:public.eco_dashboard_delivery_operation."
         "provider_name"),
        ("metadata_json losing its default",
         [f"ALTER TABLE {QUALIFIED} ALTER COLUMN metadata_json DROP DEFAULT"],
         "column_default_mismatch:public.eco_dashboard_delivery_operation."
         "metadata_json"),
        ("every provider CHECK replaced by a same-named CHECK (true)",
         [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT "
          "chk_eco_dashboard_delivery_operation_provider_key_present",
          f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT "
          "chk_eco_dashboard_delivery_operation_provider_key_present CHECK (true)",
          f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT "
          "chk_eco_dashboard_delivery_operation_binding_coherent",
          f"ALTER TABLE {QUALIFIED} ADD CONSTRAINT "
          "chk_eco_dashboard_delivery_operation_binding_coherent CHECK (true)"],
         "constraint_definition_mismatch"),
        ("the guard trigger removed",
         [f"DROP TRIGGER trg_eco_dashboard_delivery_operation_guard ON {QUALIFIED}"],
         "trigger_absent"),
        ("the guard function body replaced by a no-op",
         [NOOP_GUARD],
         "function_body_mismatch"),
        ("uq_..._provider_key downgraded to a same-named non-unique index",
         [f"ALTER TABLE {QUALIFIED} DROP CONSTRAINT "
          "uq_eco_dashboard_delivery_operation_provider_key",
          "CREATE INDEX uq_eco_dashboard_delivery_operation_provider_key "
          f"ON {QUALIFIED} (provider_idempotency_key)"],
         "constraint_absent"),
    ):
        _recreate_databases(CLIENT_DB)
        _build_client()
        with _connect(CLIENT_DB) as conn:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        _expect_refusal(
            f"049 RECORDED but {label}",
            "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", tree, needle,
        )

    # MALFORMED DECLARATION. The gate must refuse before it can treat a
    # declaration it did not understand as a set of requirements — a typo must
    # never read as "this release asserts nothing here".
    _recreate_databases(CLIENT_DB)
    _build_client()
    for label, damage in (
        ("a misspelled column key 'defualt'",
         lambda c: c["relations"][0]["columns"][0].update(
             {"defualt": c["relations"][0]["columns"][0].pop("default", None)})),
        ("a misspelled trigger key 'enabeld'",
         lambda c: c["relations"][0]["triggers"][0].update({"enabeld": False})),
        ('a string "false" where a JSON boolean is required',
         lambda c: c["relations"][0]["columns"][1].update({"nullable": "false"})),
        ("a structurally incomplete default declaration",
         lambda c: c["relations"][0]["columns"][1].update(
             {"default": {"state": "expression"}})),
    ):
        document = _document()
        document["requirements"] = [
            r for r in document["requirements"]
            if r.get("migration") == MIGRATION_FILE
        ]
        damage(document["requirements"][0])
        damaged_tree = tmp_root / f"release-malformed-{abs(hash(label))}"
        (damaged_tree / "db").mkdir(parents=True, exist_ok=True)
        (damaged_tree / "db" / "schema_requirements.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )
        _expect_refusal(f"a release declaring {label}",
                        "RELEASE_SCHEMA_REQUIREMENTS_MALFORMED", damaged_tree)

    # And the pre-existing invariant is untouched: an absent 049 is still a
    # ledger refusal, which must not be reclassified by this work.
    _recreate_databases(CLIENT_DB)
    _build_client(record_migration=False)
    _expect_refusal("049 not recorded at all",
                    "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING", tree)

    # A client whose 049 is absent entirely — neither recorded nor physical.
    _recreate_databases(CLIENT_DB)
    with _connect(CLIENT_DB) as conn:
        conn.execute(
            "CREATE TABLE public.schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
    _expect_refusal("049 absent from the client entirely",
                    "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING", tree)


# ---------------------------------------------------------------------------

def main() -> int:
    dsn = os.environ.get(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable loopback PostgreSQL DSN.")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)

    print("=" * 78)
    print("MIGRATION 049 — PHYSICAL ACTIVATION PREFLIGHT")
    print("=" * 78)

    _recreate_databases(PROBE_DB)
    try:
        with _connect(PROBE_DB) as conn:
            _apply_exact_049(conn)
            conn.commit()
            with conn.cursor() as cur:
                control_exact_migration(cur)
                requirement_fidelity(cur)
                reviewed_defect_replay(cur, conn)
                corruption_columns(cur, conn)
                corruption_defaults(cur, conn)
                corruption_checks(cur, conn)
                corruption_uniqueness(cur, conn)
                corruption_trigger_and_function(cur, conn)
                # The exact migration still satisfies the requirement after the
                # whole matrix: every corruption really was rolled back, so no
                # result above was produced against an already-damaged schema.
                print("\n### CONTROL — re-asserted after the corruption matrix")
                control_exact_migration(cur)

        runtime_narrow_insert()

        with tempfile.TemporaryDirectory(prefix="eco049-preflight-") as tmp:
            release_classification(Path(tmp))
    finally:
        _drop_databases(PROBE_DB, PLATFORM_DB, CLIENT_DB, RUNTIME_DB)

    print("\n" + "=" * 78)
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("ALL MIGRATION-049 PHYSICAL PREFLIGHT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
