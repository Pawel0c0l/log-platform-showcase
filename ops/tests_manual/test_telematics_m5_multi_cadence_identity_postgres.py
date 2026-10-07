#!/usr/bin/env python3
"""M5 — multi-cadence schedule identity and one shared per-dataset watermark.

WHAT THIS PINS, and why each part needs a real database.

    1. **Three roles are representable, a fourth is not.** The `run_type`
       vocabulary, the re-keyed schedule uniqueness and the "at most one base
       schedule" partial index are asserted against PostgreSQL, because a rule
       that lives only in Python is not a rule a direct SQL writer has to obey.
    2. **One dataset, one watermark.** Two schedules of one dataset resolve the
       *same* coverage row, one advances it and the other observes the advanced
       value. That is the whole of M5 and it is not observable without a commit.
    3. **The CAS fingerprint did not narrow.** M5 changed which row the
       compare-and-swap addresses. It must not have changed what the predicate
       compares, so the eleven-field claim is re-proven field by field against
       the re-keyed statement.
    4. **Decision A — a pre-M5 release still works after 062.** The old
       `WHERE schedule_id = …` shape is executed verbatim against the migrated
       schema, including once a second cadence exists, and the exact boundary is
       asserted rather than described.
    5. **Decision B — recovery exclusivity follows the watermark.** Two
       non-terminal recoveries on two cadences of one dataset are rejected by
       the database, not merely survived by the CAS.
    6. **The migration refuses rather than repairs.** Each contradiction it
       guards against is constructed and the migration is run against it.

    Static checks always run. The PostgreSQL checks need
    `TELEMATICS_M5_IDENTITY_TEST_DSN` pointing at a *disposable* PostgreSQL 16 —
    never `logdb`:

      docker run -d --rm --name m5-pg -e POSTGRES_PASSWORD=... \\
          -e POSTGRES_USER=loguser -e POSTGRES_DB=m5_test \\
          -p 55811:5432 postgres:16
      TELEMATICS_M5_IDENTITY_TEST_DSN='postgresql://loguser:...@127.0.0.1:55811/m5_test' \\
          .venv/bin/python ops/tests_manual/test_telematics_m5_multi_cadence_identity_postgres.py

DESTRUCTIVE. Drops and recreates its own schema. The loopback guard runs before
any connection is attempted.
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
import pathlib
from pathlib import Path
from typing import List

from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jobs.api.telematics import coverage_finalization as cf  # noqa: E402
from jobs.api.telematics import dispatcher as d  # noqa: E402
from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RUN_TYPE_BASE,
    SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
    SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
    SCHEDULE_RUN_TYPES,
    ScheduleMutationRefused,
    assert_run_type_cadence_coherent,
    validate_schedule_run_type,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)

ENV = "TELEMATICS_M5_IDENTITY_TEST_DSN"

MIGRATION_NAME = "062_workflow_a_multi_cadence_schedule_identity.sql"
MIGRATIONS_DIR = ROOT / "db" / "migrations"

#: Minimum prerequisite chain reproducing the authoritative account, schedule,
#: history, coverage, recovery and evidence schema that 062 binds to.
PREREQUISITE_MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    # Needed only by the trip-metrics diagnostic, which reads
    # `client_account.trip_metrics_population_source`.
    "040_workflow_a_trip_metrics_population_source.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "061_workflow_a_provider_request_log.sql",
)

MODE = "data_invariants_v1"
CID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
CODE = "TST00001"
SID_BASE = "7cac378a-5787-4d62-85d1-282bed208c8c"
SID_WEEKLY = "9c9c9261-2cf7-4b6f-8955-b515814be2f7"
SID_MONTHLY = "f25c8a6c-7ca5-4899-8a16-2490d9e5e241"
#: A second owner, so "belongs to another client" is constructible.
OTHER_CID = "e8d95748-107a-4eb9-8f7f-ab2901c84c97"
OTHER_CODE = "OTH00001"
SID_OTHER = "e6bdd83c-17d2-45a4-895c-d0a7f648aab3"
#: A second dataset for the same client, so "belongs to another dataset" is too.
OTHER_DATASET = "fuel_daily_aggregation"
#: A third client that is enabled but has NO `trips_sync` schedule of any cadence.
#: The trip-metrics diagnostic must still report it, which is only true while its
#: role/dataset predicates live in the LEFT JOIN condition rather than the WHERE
#: clause. Seeded per-test, not by `fresh`, so no other proof's counts move.
NOSCHED_CID = "f2da1f95-33d4-4ea3-8e5d-c22bbd7e2357"
NOSCHED_CODE = "NOS00001"
SID_NOSCHED_OTHER_DATASET = "86767804-4762-45bc-8dda-a24370d8d272"

#: Live modules whose subject is "the base schedule for this client/dataset".
#: Each must resolve `run_type = 'DAILY'` explicitly, or a valid second cadence
#: makes it ambiguous the day M6 lands.
BASE_INTENDED_SURFACES = (
    "ops/activate_telematics_trips_schedule.py",
    "ops/recover_telematics_trips_window.py",
    "ops/audit_telematics_coverage_bootstrap.py",
    "ops/bootstrap_telematics_trips_coverage.py",
    "ops/audit_telematics_cold_start.py",
    "ops/checks/check_trip_metrics_population_source.py",
    "scripts/onboard_workflow_a_client.py",
    "jobs/api/telematics/control_plane.py",
)

#: Live modules that legitimately enumerate EVERY cadence. Narrowing these to the
#: base role would be a defect, not a fix — the dispatcher must dispatch every
#: enabled row, and the watchdog/readiness surfaces exist to inventory them all.
WHOLE_INVENTORY_SURFACES = (
    "jobs/api/telematics/dispatcher.py",
    "ops/execution_watchdog.py",
    "ops/environment_identity_promotion.py",
    # ADDED BY M-LAG. The delivery-lag aggregation must see EVERY cadence, not
    # the base one: it derives the recompute horizon from the deepest enabled
    # `lookback_days` across all roles, and the bucket boundary from the
    # reconciliation cadences specifically. Narrowing it to the base role would
    # silently under-recompute once a reconciliation cadence with a deeper
    # lookback is enabled, which is precisely the staleness the recompute design
    # exists to prevent.
    "ops/aggregate_telematics_delivery_lag.py",
)

#: Live modules that issue NO schedule SQL of their own. They never resolve a
#: schedule row; they consume one that `control_plane.load_dataset_schedule` —
#: itself base-scoped, and audited as a base-intended surface above — already
#: resolved. Their cadence scope is therefore inherited, not declared, and a
#: `run_type` filter here would be meaningless. Calling these "schedule_id-keyed"
#: was wrong: that is a different semantic, enumerated separately below.
#:
#: Value records HOW each one obtains the already-base-scoped schedule, so the
#: classification asserts a verified fact rather than restating a label.
TRANSITIVE_CONSUMER_SURFACES = {
    "jobs/api/telematics/aggregate_trip_fuel_daily.py": "calls_loader",
    "jobs/api/telematics/sync_trips_and_speeding.py": "calls_loader",
    # Receives the `DatasetSchedule` its caller loaded and only re-verifies it;
    # it does not import the loader and issues no schedule SQL.
    "jobs/api/telematics/manual_recovery_authority.py": "receives_loaded_schedule",
}

#: Live modules that are unambiguous without a role filter for a reason that is
#: NOT "someone else resolved it for me".
#:
#:   * `keys_on_schedule_id` — addresses one exact row by its own primary key,
#:     supplied by the operator. Cadence cannot be ambiguous: a `schedule_id`
#:     identifies at most one row whatever roles exist.
#:   * `helper_only` — names the relation in prose/validation vocabulary and
#:     issues no SQL against it at all.
EXACT_SCHEDULE_OR_HELPER_SURFACES = {
    "ops/bootstrap_telematics_cold_start_coverage.py": "keys_on_schedule_id",
    "jobs/api/telematics/schedule_mutation_surfaces.py": "helper_only",
}

#: ADDED BY M6. A fifth semantic, because none of the four above describes it
#: honestly.
#:
#: A role-explicit surface never resolves a schedule without naming `run_type`
#: in that very statement — sometimes the base role (to read the row it derives
#: from and to prove the base is enabled), sometimes an explicitly requested
#: reconciliation role (to read or enable its own row). Calling it
#: "base-intended" would be false, because it deliberately addresses non-base
#: rows; calling it "whole-inventory" would be false in the opposite direction,
#: because it must never enumerate cadences blindly.
#:
#: The safety property is therefore not "it filters to DAILY" but "it can never
#: touch a row whose role it did not name", plus "it consults the reconciliation
#: policy oracle before every write". Both are asserted below.
ROLE_EXPLICIT_SURFACES = {
    "ops/manage_telematics_reconciliation_schedule.py": "reconciliation_lifecycle",
}
SID_OTHER_DATASET = "91f92254-93de-43c7-877f-44f19a665ba6"

A = datetime(2026, 6, 1, tzinfo=timezone.utc)
W = datetime(2026, 8, 1, tzinfo=timezone.utc)
SEEDED = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
OLD = datetime(2026, 7, 1, 13, tzinfo=timezone.utc)

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _refuses(label: str, fn, conn) -> None:
    """Assert PostgreSQL itself rejects `fn()`, not just the Python above it."""
    try:
        fn()
        conn.rollback()
        _check(label, False, "the database accepted it")
    except Exception:
        conn.rollback()
        _check(label, True)


def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


# ===========================================================================
# Static checks — no database required
# ===========================================================================

def test_migration_is_the_next_one_and_is_transactional() -> None:
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    _check("062 is present", MIGRATION_NAME in names)
    # M5's migration was the highest WHEN M5 LANDED. Later milestones add their
    # own — M-LAG added 063 — so asserting "highest" would turn every future
    # migration into an M5 regression. What must stay true is that 062 did not
    # collide with or skip a number, which is the defect the check was for.
    numbers = sorted(int(n.split("_", 1)[0]) for n in names)
    _check(
        "no two platform migrations share a number",
        len(numbers) == len(set(numbers)),
        f"duplicated: {sorted({n for n in numbers if numbers.count(n) > 1})}",
    )
    m5_number = int(MIGRATION_NAME.split("_", 1)[0])
    up_to_m5 = [n for n in numbers if n <= m5_number]
    _check(
        "062 did not skip a number below itself",
        up_to_m5 == sorted(set(up_to_m5)) and up_to_m5[-1] == m5_number,
        f"numbers up to {m5_number}: {up_to_m5}",
    )
    sql = _sql(MIGRATION_NAME)
    # `ops/db_migrate.sh` runs migrations under psql autocommit, so atomicity has
    # to be requested explicitly. Without it a failed guard could leave a
    # half-installed re-key behind, which is the one outcome a control-plane
    # migration must never produce.
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    first_statement = re.search(
        r"^[ \t\r\n]*(BEGIN;|ALTER\b|CREATE\b|UPDATE\b|DROP\b|DO\b)", body, re.M
    )
    _check(
        "062 opens an explicit transaction before any DDL",
        first_statement is not None and first_statement.group(1) == "BEGIN;",
        f"first statement was {first_statement.group(1) if first_statement else None}",
    )
    _check("062 commits explicitly", sql.rstrip().endswith("COMMIT;"))
    _check(
        "062 never repairs contradictory state",
        not re.search(r"\bDELETE\s+FROM\s+workflow_a_control\.", sql, re.I),
        "a repair statement would contradict the fail-closed contract",
    )


def test_python_and_sql_agree_on_the_vocabulary() -> None:
    """One vocabulary, or the database and the dispatcher will drift apart."""
    sql = _sql(MIGRATION_NAME)
    match = re.search(
        r"ck_client_dataset_schedule_run_type\s*\n\s*CHECK \(run_type IN \(([^)]*)\)\)",
        sql,
    )
    _check("the run_type CHECK is present in 062", match is not None)
    if match:
        in_sql = {token.strip().strip("'") for token in match.group(1).split(",")}
        _check(
            "Python and SQL carry the same run_type vocabulary",
            in_sql == set(SCHEDULE_RUN_TYPES),
            f"sql={sorted(in_sql)} python={sorted(SCHEDULE_RUN_TYPES)}",
        )


def test_the_validator_fails_closed() -> None:
    _check(
        "the base role validates",
        validate_schedule_run_type("DAILY") == SCHEDULE_RUN_TYPE_BASE,
    )
    for bad in ("daily", "", None, "WEEKLY", "ANNUAL_RECONCILIATION"):
        try:
            validate_schedule_run_type(bad)
            _check(f"an unknown run_type {bad!r} is refused", False)
        except ScheduleMutationRefused:
            _check(f"an unknown run_type {bad!r} is refused", True)


def test_run_type_cadence_coherence_in_python() -> None:
    # The base role is deliberately unconstrained: migrations 032 and 046 already
    # seed weekly base schedules for the Eco datasets.
    for frequency in ("daily", "weekly", "monthly"):
        assert_run_type_cadence_coherent(
            run_type=SCHEDULE_RUN_TYPE_BASE, frequency=frequency,
        )
    _check("a base schedule may carry any cadence", True)

    assert_run_type_cadence_coherent(
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, frequency="weekly",
    )
    for role, bad in (
        (SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, "daily"),
        (SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, "monthly"),
        (SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION, "weekly"),
    ):
        try:
            assert_run_type_cadence_coherent(run_type=role, frequency=bad)
            _check(f"{role} + {bad} is refused", False)
        except ScheduleMutationRefused:
            _check(f"{role} + {bad} is refused", True)


def test_no_production_module_addresses_coverage_by_schedule_id() -> None:
    """The mixed-key state is what M5 exists to remove; prove none survives.

    Scans production modules for a coverage statement still keyed on
    `schedule_id`. `client_schedule_run_history` and `client_dataset_recovery_run`
    are legitimately schedule-keyed and are not matched: the scan looks only at
    statements naming the coverage relation.
    """
    import ast as _ast

    #: `ops/audit_telematics_cold_start.count_coverage_rows` deliberately keeps a
    #: WIDE `schedule_id = %s OR client_id = %s` probe. It is a cold-start
    #: *emptiness* audit whose job is to notice any pre-existing coverage,
    #: including a malformed row naming this schedule under another client.
    #: Narrowing it to the M5 owner key would blind it to exactly what it exists
    #: to find, so it is exempt by review rather than by accident.
    EXEMPT = {"ops/audit_telematics_cold_start.py"}

    offenders = []
    for path in sorted(
        list((ROOT / "jobs").rglob("*.py"))
        + list((ROOT / "ops").glob("*.py"))
        + list((ROOT / "scripts").glob("*.py"))
    ):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if "client_dataset_coverage" not in text or rel in EXEMPT:
            continue
        # Walk string literals rather than raw text: Python concatenates adjacent
        # literals into ONE node, so each SQL statement is examined whole and a
        # neighbouring statement's predicate cannot be misattributed to it.
        for node in _ast.walk(_ast.parse(text, rel)):
            if not isinstance(node, _ast.Constant) or not isinstance(node.value, str):
                continue
            sql_text = node.value
            if "client_dataset_coverage" not in sql_text:
                continue
            if re.search(r"WHERE\s+schedule_id\s*=", sql_text, re.I):
                offenders.append(f"{rel}:{node.lineno}")
    _check(
        "no production module still addresses coverage by schedule_id",
        not offenders,
        f"offenders: {sorted(set(offenders))}",
    )


def test_release_gate_declares_062() -> None:
    import json

    data = json.loads((ROOT / "db" / "schema_requirements.json").read_text())
    entry = [
        r for r in data["requirements"] if r["migration"] == MIGRATION_NAME
    ]
    _check("schema_requirements.json declares 062", len(entry) == 1)
    if not entry:
        return
    req = entry[0]
    _check("062 is declared platform-scope", req["scope"] == "platform")
    _check("062 carries no client-business requirement", req["scope"] != "client_business")
    relations = {r["table"]: r for r in req["relations"]}

    def _defs(table: str) -> dict:
        """Name -> declared definition, for whichever requirement form is used."""
        out = {}
        for entry in relations[table].get("constraints", []):
            if isinstance(entry, str):
                out[entry] = None
            else:
                out[entry["name"]] = entry.get("definition")
        for entry in relations[table].get("indexes", []):
            out[entry["name"]] = entry["definition"]
        return out

    _check(
        "the gate requires client_dataset_schedule.run_type",
        any(
            c["name"] == "run_type" and c["nullable"] is False
            for c in relations["client_dataset_schedule"]["columns"]
        ),
    )
    # Every load-bearing 062 shape must be declared BY DEFINITION. A name-only
    # requirement is what let a drifted schema through independent review.
    expected = {
        "client_dataset_schedule": {
            "uq_client_dataset_schedule",
            "uq_client_dataset_schedule_owner_identity",
            "ck_client_dataset_schedule_run_type",
            "ck_client_dataset_schedule_run_type_cadence",
            "uq_client_dataset_schedule_base",
        },
        "client_dataset_coverage": {
            "uq_client_dataset_coverage_dataset",
            "pk_client_dataset_coverage",
            "fk_client_dataset_coverage_schedule",
        },
        "client_dataset_recovery_run": {
            "fk_client_dataset_recovery_run_schedule",
            "uq_client_dataset_recovery_run_active",
            "uq_client_dataset_recovery_run_approved_window",
        },
    }
    for table, names in expected.items():
        declared = _defs(table)
        missing = sorted(names - set(declared))
        _check(
            f"the gate declares every load-bearing 062 shape on {table}",
            not missing,
            f"missing: {missing}",
        )
        nameless = sorted(n for n in names if declared.get(n) is None)
        _check(
            f"every declared 062 shape on {table} carries a definition",
            not nameless,
            f"name-only: {nameless}",
        )


def test_operator_surfaces_scope_to_the_base_schedule() -> None:
    """M6 must not break an M5-owned operator surface the day it lands."""
    onboarding = (ROOT / "scripts" / "onboard_workflow_a_client.py").read_text()
    _check(
        "onboarding names the re-keyed conflict target",
        "ON CONFLICT (client_id, dataset_name, run_type) DO NOTHING" in onboarding,
    )
    _check(
        "onboarding seeds the base role only",
        "SCHEDULE_RUN_TYPE_BASE" in onboarding
        and SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION not in onboarding
        and SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION not in onboarding,
    )
    for name in BASE_INTENDED_SURFACES:
        text = (ROOT / name).read_text()
        _check(
            f"{name} resolves the base schedule explicitly",
            "SCHEDULE_RUN_TYPE_BASE" in text
            and ("run_type = %s" in text or "run_type=%s" in text),
        )


def test_no_live_schedule_resolver_escapes_the_base_role_audit() -> None:
    """Recursive, so directory depth cannot hide a resolver.

    The previous version of this audit enumerated `ops/*.py` and therefore never
    looked inside `ops/checks/`, which is exactly how a live base-intended
    diagnostic survived a full review pass. This walks the whole live tree.

    It is deliberately NOT a blanket "everything must filter DAILY" rule — that
    would be wrong. Several surfaces legitimately enumerate every cadence, and
    forcing a base filter on them would break the dispatcher outright. So every
    live module that names the schedule relation must be classified into exactly
    one of FOUR sets, and an unclassified module fails: the point is that a NEW
    resolver cannot be added silently, not that a particular filter is present.

    The four categories are distinct semantics, not stylistic groupings, and each
    is asserted rather than asserted-by-label:

      * base-intended       — resolves the base row itself, so it must name the
                              base role in its own predicate;
      * whole-inventory     — must NOT narrow its schedule enumeration;
      * transitive consumer — issues no schedule SQL at all; its scope is
                              inherited from the already-base-scoped loader;
      * exact-schedule/     — unambiguous by primary key, or holds no schedule
        helper                SQL whatsoever.

    An earlier revision merged the last two into one "schedule_id-keyed" set.
    That was false of all three transitive consumers: none of them keys on a
    `schedule_id`, and describing them that way hid the fact that their
    correctness depends entirely on the loader staying base-scoped.
    """
    live: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        # `tmp/` holds scratch probes, not deployed code; `snapshots/` and
        # `release*/` are frozen copies of past trees. Auditing those would
        # assert something about history rather than about what runs today.
        if rel.startswith((
            "ops/tests_manual/", "release", ".venv/", "snapshots/", "tmp/",
            "backups/",
        )):
            continue
        if "client_dataset_schedule" not in path.read_text(encoding="utf-8"):
            continue
        live.append(rel)

    categories = {
        "base-intended": set(BASE_INTENDED_SURFACES),
        "whole-inventory": set(WHOLE_INVENTORY_SURFACES),
        "transitive-consumer": set(TRANSITIVE_CONSUMER_SURFACES),
        "exact-schedule-or-helper": set(EXACT_SCHEDULE_OR_HELPER_SURFACES),
        "role-explicit": set(ROLE_EXPLICIT_SURFACES),
    }
    classified: set[str] = set()
    for members in categories.values():
        classified |= members
    overlap = sorted(
        name
        for name in classified
        if sum(name in members for members in categories.values()) > 1
    )
    _check(
        "each classified module belongs to exactly one category",
        not overlap,
        f"claimed by more than one category: {overlap}",
    )
    unclassified = sorted(set(live) - classified)
    _check(
        "every live module naming the schedule relation is classified",
        not unclassified,
        f"unclassified: {unclassified} — classify each as base-intended, "
        f"whole-inventory, transitive-consumer or exact-schedule/helper "
        f"before this can pass",
    )
    stale = sorted(classified - set(live))
    _check(
        "the classification carries no stale entries",
        not stale,
        f"no longer reference the schedule relation: {stale}",
    )
    import ast as _ast

    for name in WHOLE_INVENTORY_SURFACES:
        text = (ROOT / name).read_text()
        # Statement-precise, not file-wide. Two distinctions matter:
        #
        #   * merely NAMING the base constant is fine — the dispatcher imports it
        #     as the default for `ScheduleRow.run_type` while still enumerating
        #     every row;
        #   * a `run_type` predicate is fine in a COVERAGE query — the
        #     dispatcher's provenance join must check the anchor carries the base
        #     role. What must never be narrowed is the SCHEDULE ENUMERATION.
        #
        # So the rule applies only to literals that name the schedule relation
        # and not the coverage relation.
        offenders = []
        for node in _ast.walk(_ast.parse(text, name)):
            if not isinstance(node, _ast.Constant) or not isinstance(node.value, str):
                continue
            sql_text = node.value
            if "client_dataset_schedule" not in sql_text:
                continue
            if "client_dataset_coverage" in sql_text:
                continue
            if re.search(r"run_type\s*=", sql_text):
                offenders.append(node.lineno)
        _check(
            f"{name} enumerates every cadence (no base filter)",
            not offenders,
            f"a whole-inventory schedule query must not be narrowed to the base "
            f"role; lines {offenders}",
        )

    def _schedule_sql(name: str) -> list[int]:
        """Lines of string literals that run SQL against the schedule relation.

        Naming the relation in a docstring or a refusal message is not resolving
        a schedule; issuing `FROM`/`JOIN`/`UPDATE`/`INSERT INTO` against it is.
        """
        found = []
        for node in _ast.walk(_ast.parse((ROOT / name).read_text(), name)):
            if not isinstance(node, _ast.Constant) or not isinstance(node.value, str):
                continue
            if "client_dataset_schedule" not in node.value:
                continue
            if re.search(
                r"(FROM|JOIN|UPDATE|INSERT\s+INTO)\s+\S*client_dataset_schedule",
                node.value,
                re.I,
            ):
                found.append(node.lineno)
        return found

    # --- transitive consumers: scope is INHERITED, so they must own no query --
    for name, how in sorted(TRANSITIVE_CONSUMER_SURFACES.items()):
        own_sql = _schedule_sql(name)
        _check(
            f"{name} issues no schedule SQL of its own",
            not own_sql,
            f"a transitive consumer that resolves schedule rows itself is "
            f"misclassified — it would need its own base filter; lines {own_sql}",
        )
        text = (ROOT / name).read_text()
        if how == "calls_loader":
            _check(
                f"{name} reaches the schedule through the base-scoped loader",
                "load_dataset_schedule" in text,
                "classified as calling control_plane.load_dataset_schedule, but "
                "the call is absent",
            )
        else:
            _check(
                f"{name} consumes a schedule loaded by its caller",
                "load_dataset_schedule" not in text
                and "schedule" in text,
                "classified as receiving an already-loaded schedule, but it "
                "imports the loader itself — reclassify as calls_loader",
            )

    # The whole category is only sound while the loader stays base-scoped.
    # `control_plane.py` carries that guarantee and is audited as base-intended.
    _check(
        "the loader every transitive consumer depends on is base-intended",
        "jobs/api/telematics/control_plane.py" in BASE_INTENDED_SURFACES,
        "transitive scope is inherited from control_plane.load_dataset_schedule; "
        "it must remain in the base-role audit",
    )

    # --- role-explicit (M6): every statement names the role it addresses ------
    for name, kind in sorted(ROLE_EXPLICIT_SURFACES.items()):
        text = (ROOT / name).read_text()
        tree = _ast.parse(text, name)
        blind = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Constant) or not isinstance(node.value, str):
                continue
            sql_text = node.value
            if not re.search(
                r"(FROM|JOIN|UPDATE|INSERT\s+INTO)\s+\S*client_dataset_schedule",
                sql_text,
                re.I,
            ):
                continue
            # An INSERT names the role in its column list rather than in a
            # predicate; a SELECT/UPDATE must carry it as a predicate. Either
            # way the statement must mention `run_type` — a statement that does
            # not is cadence-blind, which is the only failure this category has.
            if "run_type" not in sql_text:
                blind.append(node.lineno)
        _check(
            f"{name} names run_type in every schedule statement",
            not blind,
            f"a role-explicit surface must never address a schedule row whose "
            f"cadence it did not name; lines {blind}",
        )
        if kind == "reconciliation_lifecycle":
            _check(
                f"{name} consults the reconciliation policy oracle",
                "assert_reconciliation_creation_permitted" in text
                and "assert_reconciliation_activation_permitted" in text,
                "a reconciliation lifecycle surface must call BOTH deny-by-"
                "default guards; writing without them would inherit no policy",
            )
            _check(
                f"{name} derives its row through the pure projection",
                "derive_reconciliation_schedule" in text
                and "RECONCILIATION_INSERT_FIELDS" in text,
                "the INSERT must be built from the audited projection so no "
                "inherited column can fall back to a table default",
            )

    # --- exact-schedule / helper: unambiguous for their own stated reason ----
    for name, kind in sorted(EXACT_SCHEDULE_OR_HELPER_SURFACES.items()):
        text = (ROOT / name).read_text()
        if kind == "keys_on_schedule_id":
            _check(
                f"{name} addresses one exact row by schedule_id",
                bool(re.search(r"WHERE\s+schedule_id\s*=\s*%s", text, re.I)),
                "classified as schedule_id-keyed, but no primary-key predicate "
                "is present; a role-blind lookup would be cadence-ambiguous",
            )
        else:
            _check(
                f"{name} issues no SQL against the schedule relation",
                not _schedule_sql(name),
                "classified as helper-only, but it queries the schedule "
                "relation; reclassify it as a resolver",
            )


def test_m5_creates_no_reconciliation_schedule_anywhere() -> None:
    """No cadence row is seeded anywhere. Amended by M6, not weakened.

    M5's original claim was "nothing in the repository can insert a
    reconciliation schedule row". M6 deliberately changes that: it adds exactly
    one reviewed, deny-by-default surface whose entire purpose is to insert one,
    from operator-supplied parameters, always disabled.

    What must still hold — and what this now asserts — is the part that was
    always the real invariant:

      * **no migration** seeds a reconciliation row. A migration runs itself,
        against production, with no operator in the loop; a seeded cadence there
        would be a rollout disguised as a schema change;
      * **no module other than the one registered surface** inserts one;
      * that surface hard-codes **no client**. It may carry the approved cadence
        defaults, but the client it acts on must come from a parameter, so it
        cannot become an ALPHA-only feature or fire at anybody by default.
    """
    permitted = "ops/manage_telematics_reconciliation_schedule.py"
    _check(
        "the permitted inserter is the registered reconciliation surface",
        permitted in set(ROLE_EXPLICIT_SURFACES),
        "the exemption below must name a module the base-role audit classifies",
    )

    insert_re = re.compile(
        r"INSERT\s+INTO\s+workflow_a_control\.client_dataset_schedule", re.I
    )
    roles = ("WEEKLY_RECONCILIATION", "MONTHLY_RECONCILIATION")

    migration_offenders = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if insert_re.search(text) and any(f"'{role}'" in text for role in roles):
            migration_offenders.append(path.relative_to(ROOT).as_posix())
    _check(
        "no migration seeds a reconciliation schedule row",
        not migration_offenders,
        f"offenders: {sorted(set(migration_offenders))}",
    )

    module_offenders = []
    for path in sorted(
        list((ROOT / "jobs").rglob("*.py"))
        + list((ROOT / "ops").glob("*.py"))
        + list((ROOT / "scripts").glob("*.py"))
    ):
        rel = path.relative_to(ROOT).as_posix()
        if rel == permitted:
            continue
        text = path.read_text(encoding="utf-8")
        if insert_re.search(text) and any(role in text for role in roles):
            module_offenders.append(rel)
    _check(
        "only the registered surface can insert a reconciliation schedule row",
        not module_offenders,
        f"offenders: {sorted(set(module_offenders))}",
    )

    surface = (ROOT / permitted).read_text(encoding="utf-8")
    _check(
        "the registered surface hard-codes no client",
        not re.search(r"[A-Z]{4,5}\d{5}", surface.split('"""', 2)[-1]),
        "a client code appears outside the module docstring; the client must "
        "always arrive as --client-code so this cannot become a per-client "
        "feature or act on anyone by default",
    )


# ===========================================================================
# PostgreSQL checks
# ===========================================================================

def apply_chain(conn, *, through_062: bool = True) -> None:
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    for name in PREREQUISITE_MIGRATIONS:
        conn.execute(_sql(name))
    if through_062:
        conn.execute(_sql(MIGRATION_NAME))
    conn.commit()


def seed_client(conn) -> None:
    conn.execute(
        """INSERT INTO workflow_a_control.client_account
          (client_id,client_code,client_name,provider_type,provider_base_url,
           provider_basic_auth_username,provider_basic_auth_password_secret_ref,
           client_db_host,client_db_port,client_db_name,client_db_user,
           client_db_password_secret_ref,client_db_schema,speed_trigger_filter_text,
           enabled,trips_pagination_mode)
          VALUES (%s,%s,'Test','telematics','https://example.invalid','u','REF',
                  '127.0.0.1',5432,'db','u','REF','public','speeding',true,%s)
           ON CONFLICT (client_id) DO NOTHING""",
        (CID, CODE, MODE),
    )
    conn.commit()


def insert_schedule(
    conn, *, schedule_id: str, run_type=None, frequency="daily",
    day_of_week=None, day_of_month=None, enabled=True,
) -> None:
    columns = [
        "schedule_id", "client_id", "client_code", "dataset_name", "enabled",
        "frequency", "day_of_week", "day_of_month", "run_time", "timezone",
        "lookback_days", "overwrite_existing",
    ]
    values = [
        schedule_id, CID, CODE, "trips_sync", enabled, frequency, day_of_week,
        day_of_month, "02:00", "UTC", 4, True,
    ]
    if run_type is not None:
        columns.append("run_type")
        values.append(run_type)
    conn.execute(
        f"""INSERT INTO workflow_a_control.client_dataset_schedule
            ({','.join(columns)}) VALUES ({','.join(['%s'] * len(values))})""",
        tuple(values),
    )


def seed_coverage(conn, *, schedule_id=SID_BASE, w=W) -> None:
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_coverage
          (schedule_id,client_id,client_code,dataset_name,coverage_start_ts,
           covered_through_ts,bootstrap_status,bootstrap_evidence_ref,seeded_at,
           seeded_by,covered_through_source,last_gap_detected_ts,updated_at)
          VALUES (%s,%s,%s,'trips_sync',%s,%s,'READY','artifact:safe',%s,
                  'operator','bootstrap',NULL,%s)""",
        (schedule_id, CID, CODE, A, w, SEEDED, OLD),
    )


def seed_other_owners(conn) -> None:
    """A second client, and a second dataset for the first client.

    Both exist so that "provenance belongs to a different owner" is a state a
    test can actually construct — without them the adversarial cases would be
    unreachable and the proofs vacuous.
    """
    conn.execute(
        """INSERT INTO workflow_a_control.client_account
          (client_id,client_code,client_name,provider_type,provider_base_url,
           provider_basic_auth_username,provider_basic_auth_password_secret_ref,
           client_db_host,client_db_port,client_db_name,client_db_user,
           client_db_password_secret_ref,client_db_schema,speed_trigger_filter_text,
           enabled,trips_pagination_mode)
          VALUES (%s,%s,'Other','telematics','https://example.invalid','u','REF',
                  '127.0.0.1',5432,'db','u','REF','public','speeding',true,%s)
           ON CONFLICT (client_id) DO NOTHING""",
        (OTHER_CID, OTHER_CODE, MODE),
    )
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
           run_time,timezone,lookback_days,overwrite_existing)
          VALUES (%s,%s,%s,'trips_sync',true,'daily','02:00','UTC',4,true)
           ON CONFLICT (schedule_id) DO NOTHING""",
        (SID_OTHER, OTHER_CID, OTHER_CODE),
    )
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
           run_time,timezone,lookback_days,overwrite_existing)
          VALUES (%s,%s,%s,%s,true,'daily','02:00','UTC',4,true)
           ON CONFLICT (schedule_id) DO NOTHING""",
        (SID_OTHER_DATASET, CID, CODE, OTHER_DATASET),
    )


def fresh(conn) -> None:
    """A migrated schema holding exactly one base schedule and one watermark."""
    apply_chain(conn)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)
    seed_other_owners(conn)
    seed_coverage(conn)
    conn.commit()


# ---- schedule identity ----------------------------------------------------

def test_existing_rows_become_base_schedules(conn) -> None:
    """The backfill classifies; it never infers."""
    apply_chain(conn, through_062=False)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE, frequency="daily")
    # A weekly BASE schedule, exactly like the Eco rows migrations 032/046 seed.
    conn.execute(
        """INSERT INTO workflow_a_control.dataset_registry
             (dataset_name, job_module, description)
           VALUES ('eco_weekly','jobs.api.telematics.aggregate_trip_fuel_daily',
                   'weekly base schedule fixture')
           ON CONFLICT (dataset_name) DO NOTHING"""
    )
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
           day_of_week,run_time,timezone,lookback_days,overwrite_existing)
          VALUES (%s,%s,%s,'eco_weekly',false,'weekly',0,'05:00','UTC',0,true)""",
        (SID_WEEKLY, CID, CODE),
    )
    conn.commit()

    conn.execute(_sql(MIGRATION_NAME))
    conn.commit()

    rows = dict(
        conn.execute(
            "SELECT dataset_name, run_type FROM"
            " workflow_a_control.client_dataset_schedule"
        ).fetchall()
    )
    _check(
        "every pre-existing schedule becomes the base role",
        rows == {"trips_sync": "DAILY", "eco_weekly": "DAILY"},
        str(rows),
    )
    _check(
        "a weekly base schedule survives the backfill unchanged",
        rows.get("eco_weekly") == SCHEDULE_RUN_TYPE_BASE,
        "the role is orthogonal to the cadence; forcing them to agree would "
        "have rejected the Eco rows",
    )


def test_three_roles_coexist_and_a_fourth_row_is_rejected(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    insert_schedule(
        conn, schedule_id=SID_MONTHLY,
        run_type=SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
        frequency="monthly", day_of_month=1,
    )
    conn.commit()
    n = conn.execute(
        "SELECT count(*) FROM workflow_a_control.client_dataset_schedule"
        " WHERE client_id = %s AND dataset_name = 'trips_sync'",
        (CID,),
    ).fetchone()[0]
    _check("three cadences coexist for one dataset", n == 3, f"n={n}")

    _refuses(
        "a duplicate (client, dataset, run_type) is rejected",
        lambda: insert_schedule(
            conn, schedule_id=str(uuid.uuid4()),
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
            frequency="weekly", day_of_week=1,
        ),
        conn,
    )
    _refuses(
        "a second BASE schedule is rejected",
        lambda: insert_schedule(
            conn, schedule_id=str(uuid.uuid4()), run_type=SCHEDULE_RUN_TYPE_BASE,
        ),
        conn,
    )


def test_the_database_closes_the_run_type_vocabulary(conn) -> None:
    fresh(conn)
    _refuses(
        "an unknown run_type is rejected by the database",
        lambda: insert_schedule(
            conn, schedule_id=str(uuid.uuid4()), run_type="ANNUAL_RECONCILIATION",
        ),
        conn,
    )
    _refuses(
        "a lowercase run_type is rejected by the database",
        lambda: insert_schedule(
            conn, schedule_id=str(uuid.uuid4()), run_type="daily",
        ),
        conn,
    )
    _refuses(
        "a reconciliation role contradicting its cadence is rejected",
        lambda: insert_schedule(
            conn, schedule_id=str(uuid.uuid4()),
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, frequency="daily",
        ),
        conn,
    )


def test_the_column_defaults_to_the_base_role(conn) -> None:
    """An INSERT that predates M5 keeps producing a base schedule."""
    apply_chain(conn)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)  # no run_type supplied
    conn.commit()
    value = conn.execute(
        "SELECT run_type FROM workflow_a_control.client_dataset_schedule"
        " WHERE schedule_id = %s",
        (SID_BASE,),
    ).fetchone()[0]
    _check("an omitted run_type defaults to the base role", value == "DAILY")


# ---- shared coverage ------------------------------------------------------

def test_one_watermark_per_dataset(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()
    _refuses(
        "a second watermark for one dataset is rejected",
        lambda: seed_coverage(conn, schedule_id=SID_WEEKLY),
        conn,
    )


def test_two_cadences_resolve_the_same_coverage_row(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()

    base = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    weekly = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    _check("both cadences find a coverage row", base is not None and weekly is not None)
    if base and weekly:
        _check(
            "both cadences resolve the SAME row",
            base.schedule_id == weekly.schedule_id == SID_BASE
            and base.covered_through_ts == weekly.covered_through_ts,
        )
        _check(
            "the shared row keeps its originating schedule as provenance",
            base.schedule_id == SID_BASE,
            "the weekly schedule must not have become the owner",
        )


def test_a_reconciliation_fire_is_not_refused_for_a_schedule_mismatch(conn) -> None:
    """The pre-M5 identity check would have called this 'bootstrap required'."""
    from jobs.api.telematics.coverage_windows import (
        COVERAGE_GATE_ALLOWED, evaluate_coverage_gate,
    )

    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    result = evaluate_coverage_gate(
        schedule_id=SID_WEEKLY,          # a DIFFERENT schedule from the row's
        client_id=CID,
        client_code=CODE,
        dataset_name="trips_sync",
        scheduled_fire_ts=W + timedelta(hours=6),
        lookback_days=4,
        stabilization_delay_seconds=10800,
        overlap_seconds=3600,
        max_recovery_span_seconds=2678400,
        coverage_state=state,
        now_utc=W + timedelta(hours=7),
    )
    _check(
        "a reconciliation fire is allowed against the shared row",
        result.allowed and result.classification == COVERAGE_GATE_ALLOWED,
        f"classification={result.classification} reason={result.reason}",
    )


def test_advance_then_observe_across_cadences(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()

    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    snapshot = cf.CoverageClaimSnapshot.from_state(state)
    new_w = W + timedelta(hours=12)
    with conn.cursor(row_factory=dict_row) as cur:
        cf.lock_coverage_row_for_update(
            cur, client_id=CID, dataset_name="trips_sync",
        )
        result = cf.advance_covered_through_cas(
            cur,
            snapshot=snapshot,
            candidate_covered_through_ts=new_w,
            source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
            mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
        )
    conn.commit()
    _check("the base fire advanced the shared watermark", result.moved)

    observed = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    _check(
        "the other cadence observes the advanced watermark",
        observed is not None and observed.covered_through_ts == new_w,
        f"observed={observed.covered_through_ts if observed else None}",
    )
    _check(
        "advancement did not rewrite the owning schedule",
        observed is not None and observed.schedule_id == SID_BASE,
        "schedule_id must be immutable provenance, never a last-advancer pointer",
    )


def test_a_behind_w_reconciliation_run_is_a_validated_no_op(conn) -> None:
    fresh(conn)
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    snapshot = cf.CoverageClaimSnapshot.from_state(state)
    with conn.cursor(row_factory=dict_row) as cur:
        cf.lock_coverage_row_for_update(
            cur, client_id=CID, dataset_name="trips_sync",
        )
        result = cf.advance_covered_through_cas(
            cur,
            snapshot=snapshot,
            candidate_covered_through_ts=W - timedelta(days=3),
            source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
            mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
        )
    conn.commit()
    _check(
        "a behind-W run is a no-op, not a failure",
        result.moved is False and result.rows_updated == 0 and result.verified,
    )
    after = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    _check("the watermark did not move backwards", after.covered_through_ts == W)


def test_recovery_require_advance_still_refuses_a_behind_w_window(conn) -> None:
    fresh(conn)
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    snapshot = cf.CoverageClaimSnapshot.from_state(state)
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cf.advance_covered_through_cas(
                cur,
                snapshot=snapshot,
                candidate_covered_through_ts=W - timedelta(days=1),
                source=cf.COVERAGE_SOURCE_MANUAL_RECOVERY,
                mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
                require_advance=True,
            )
        _check("require_advance still refuses a behind-W recovery", False)
    except cf.CoverageCasConflict as exc:
        _check(
            "require_advance still refuses a behind-W recovery",
            exc.code == cf.TRIPS_COVERAGE_ADVANCE_CONFLICT
            and exc.dataset_name == "trips_sync",
        )
    conn.rollback()


def test_every_cas_field_is_compared(conn) -> None:
    """All ELEVEN claim fields, perturbed individually in the CLAIM.

    Independent review found the earlier version of this proof overstated itself:
    it perturbed the stored row, which cannot isolate `client_id` or
    `dataset_name` — those two now address the row, and since the composite
    provenance FK landed they cannot even be changed alone. So this loop
    perturbs the SNAPSHOT instead. That is the exact mirror of the same
    predicate (the CAS compares claim against row, and it does not care which
    side moved) and it isolates every field, including the two the row-based
    loop below structurally cannot.
    """
    fresh(conn)
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    base = cf.CoverageClaimSnapshot.from_state(state)

    claim_perturbations = {
        "schedule_id": SID_WEEKLY,
        "client_id": OTHER_CID,
        "dataset_name": "fuel_daily_aggregation",
        "coverage_start_ts": A - timedelta(days=1),
        "covered_through_ts": W - timedelta(hours=1),
        "bootstrap_status": "GAP_DETECTED",
        "bootstrap_evidence_ref": "artifact:other",
        "covered_through_source": "operator",
        "seeded_at": SEEDED - timedelta(hours=1),
        "seeded_by": "someone-else",
        "last_gap_detected_ts": OLD,
    }
    _check(
        "the perturbation set is exactly the authoritative CAS field set",
        set(claim_perturbations) == set(cf.COVERAGE_CAS_FIELDS),
        f"test={sorted(claim_perturbations)} "
        f"authoritative={sorted(cf.COVERAGE_CAS_FIELDS)}",
    )

    from dataclasses import replace as _replace

    for field, value in claim_perturbations.items():
        snapshot = _replace(base, **{field: value})
        if field == "bootstrap_status":
            # A non-READY claim is refused before any statement is issued, which
            # is a stronger refusal than the CAS predicate — assert that instead
            # of pretending the SQL did the work.
            try:
                with conn.cursor(row_factory=dict_row) as cur:
                    cf.advance_covered_through_cas(
                        cur, snapshot=snapshot,
                        candidate_covered_through_ts=W + timedelta(days=1),
                        source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
                        mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
                    )
                conn.rollback()
                _check(f"a changed claim {field} is refused", False)
            except ValueError:
                conn.rollback()
                _check(f"a changed claim {field} is refused", True)
            continue
        if field == "dataset_name":
            # Advancement is trips_sync-only and refuses another dataset before
            # issuing SQL. Same reasoning as above.
            try:
                with conn.cursor(row_factory=dict_row) as cur:
                    cf.advance_covered_through_cas(
                        cur, snapshot=snapshot,
                        candidate_covered_through_ts=W + timedelta(days=1),
                        source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
                        mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
                    )
                conn.rollback()
                _check(f"a changed claim {field} is refused", False)
            except ValueError:
                conn.rollback()
                _check(f"a changed claim {field} is refused", True)
            continue
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cf.advance_covered_through_cas(
                    cur, snapshot=snapshot,
                    candidate_covered_through_ts=W + timedelta(days=1),
                    source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
                    mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
                )
            conn.rollback()
            _check(f"a changed claim {field} is refused", False,
                   "the CAS advanced under a claim that does not match the row")
        except cf.CoverageCasConflict:
            conn.rollback()
            _check(f"a changed claim {field} is refused", True)

    # And the excluded field must still NOT refuse.
    snapshot = _replace(base, client_code="OTHER001") if hasattr(base, "client_code") else base
    _check(
        "client_code is not a CAS field",
        "client_code" not in cf.COVERAGE_CAS_FIELDS,
    )


def test_the_cas_fingerprint_did_not_narrow(conn) -> None:
    """The same predicate, proven from the other side: the STORED ROW moves.

    Nine of the eleven claim fields are freely mutable on the stored row;
    `client_id` and `dataset_name` are not, because they address the row and are
    now bound to the provenance schedule by the composite FK. Those two are
    covered by `test_every_cas_field_is_compared`. `client_code` is the control:
    it is deliberately excluded from the predicate and must NOT refuse.
    """
    perturbations = {
        "schedule_id": SID_WEEKLY,
        "client_code": "OTHER001",  # excluded from the CAS — must NOT refuse
        "coverage_start_ts": A - timedelta(days=1),
        "covered_through_ts": W - timedelta(hours=1),
        "bootstrap_status": "GAP_DETECTED",
        "bootstrap_evidence_ref": "artifact:other",
        "covered_through_source": "operator",
        "seeded_at": SEEDED - timedelta(hours=1),
        "seeded_by": "someone-else",
        "last_gap_detected_ts": OLD,
    }
    for field, value in perturbations.items():
        fresh(conn)
        # A second schedule must exist before schedule_id can be perturbed to it.
        insert_schedule(
            conn, schedule_id=SID_WEEKLY,
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
            frequency="weekly", day_of_week=0,
        )
        conn.commit()
        state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
        snapshot = cf.CoverageClaimSnapshot.from_state(state)
        conn.execute(
            f"UPDATE workflow_a_control.client_dataset_coverage"
            f"   SET {field} = %s WHERE client_id = %s AND dataset_name = %s",
            (value, CID, "trips_sync"),
        )
        conn.commit()

        excluded = field == "client_code"
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cf.advance_covered_through_cas(
                    cur,
                    snapshot=snapshot,
                    candidate_covered_through_ts=W + timedelta(days=1),
                    source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
                    mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
                )
            conn.rollback()
            _check(
                f"CAS behaviour for a changed {field}",
                excluded,
                "the CAS accepted a claim field that changed",
            )
        except cf.CoverageCasConflict:
            conn.rollback()
            _check(f"CAS behaviour for a changed {field}", not excluded,
                   "the CAS refused a field the contract excludes")


def test_concurrent_advancement_serializes_on_the_shared_row(conn, dsn) -> None:
    """Two cadences contend on ONE row: one wins, the other is refused."""
    import psycopg

    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()

    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    snapshot = cf.CoverageClaimSnapshot.from_state(state)

    other = psycopg.connect(dsn, autocommit=False)
    try:
        # The weekly cadence commits first, from its own connection.
        with other.cursor(row_factory=dict_row) as cur:
            cf.lock_coverage_row_for_update(
                cur, client_id=CID, dataset_name="trips_sync",
            )
            cf.advance_covered_through_cas(
                cur,
                snapshot=snapshot,
                candidate_covered_through_ts=W + timedelta(hours=6),
                source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
                mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
            )
        other.commit()

        # The base cadence now holds a stale claim over the same shared row.
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cf.lock_coverage_row_for_update(
                    cur, client_id=CID, dataset_name="trips_sync",
                )
                cf.advance_covered_through_cas(
                    cur,
                    snapshot=snapshot,
                    candidate_covered_through_ts=W + timedelta(hours=12),
                    source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
                    mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
                )
            conn.rollback()
            _check("a stale claim over the shared row is refused", False)
        except cf.CoverageCasConflict as exc:
            conn.rollback()
            _check(
                "a stale claim over the shared row is refused",
                exc.code == cf.TRIPS_COVERAGE_ADVANCE_CONFLICT,
            )
    finally:
        other.close()


# ---- Decision A: pre-M5 rollback compatibility ----------------------------

PRE_M5_CAS = """
UPDATE workflow_a_control.client_dataset_coverage
   SET covered_through_ts = %(new_covered_through_ts)s,
       covered_through_source = %(new_covered_through_source)s,
       updated_at = %(mutation_ts)s
 WHERE schedule_id = %(claim_schedule_id)s
   AND client_id = %(claim_client_id)s
   AND dataset_name = %(claim_dataset_name)s
   AND bootstrap_status = %(claim_bootstrap_status)s
   AND coverage_start_ts IS NOT DISTINCT FROM %(claim_coverage_start_ts)s
   AND covered_through_ts IS NOT DISTINCT FROM %(claim_covered_through_ts)s
   AND bootstrap_evidence_ref IS NOT DISTINCT FROM %(claim_bootstrap_evidence_ref)s
   AND covered_through_source = %(claim_covered_through_source)s
   AND seeded_at IS NOT DISTINCT FROM %(claim_seeded_at)s
   AND seeded_by IS NOT DISTINCT FROM %(claim_seeded_by)s
   AND last_gap_detected_ts IS NOT DISTINCT FROM %(claim_last_gap_detected_ts)s
   AND covered_through_ts < %(new_covered_through_ts)s
"""


def _run_pre_m5_cas(conn, *, schedule_id: str, new_w) -> int:
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    params = dict(cf.CoverageClaimSnapshot.from_state(state).cas_params())
    params["claim_schedule_id"] = schedule_id
    params.update(
        new_covered_through_ts=new_w,
        new_covered_through_source="scheduled_run",
        mutation_ts=datetime.now(timezone.utc).replace(microsecond=0),
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(PRE_M5_CAS, params)
        return cur.rowcount


def test_decision_a_pre_m5_release_still_operates_after_062(conn) -> None:
    """The boundary, asserted rather than described.

    Through all of M5 — one base schedule per dataset — the pre-M5 statement
    shape resolves exactly one row and advances it. That is what makes rollback
    to the pre-M5 release possible without a schema downgrade.
    """
    fresh(conn)
    affected = _run_pre_m5_cas(
        conn, schedule_id=SID_BASE, new_w=W + timedelta(hours=1),
    )
    conn.commit()
    _check(
        "a pre-M5 release's schedule-keyed CAS still affects exactly one row",
        affected == 1,
        f"rowcount={affected}",
    )

    # And after a future cadence exists: the BASE schedule still resolves...
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()
    affected = _run_pre_m5_cas(
        conn, schedule_id=SID_BASE, new_w=W + timedelta(hours=2),
    )
    conn.commit()
    _check(
        "after a second cadence exists the base schedule still resolves",
        affected == 1,
        f"rowcount={affected}",
    )

    # ...and the reconciliation schedule finds nothing, which is the fail-closed
    # half of the boundary: a pre-M5 release refuses such a fire, it never moves
    # the wrong watermark.
    affected = _run_pre_m5_cas(
        conn, schedule_id=SID_WEEKLY, new_w=W + timedelta(hours=3),
    )
    conn.rollback()
    _check(
        "a pre-M5 release finds no row for a reconciliation schedule",
        affected == 0,
        f"rowcount={affected}",
    )


def test_the_rollback_anchor_survives_the_migration(conn) -> None:
    fresh(conn)
    row = conn.execute(
        """SELECT conname FROM pg_constraint
            WHERE conrelid = 'workflow_a_control.client_dataset_coverage'::regclass
              AND conname = 'pk_client_dataset_coverage' AND contype = 'p'"""
    ).fetchone()
    _check("the coverage primary key on schedule_id is retained", row is not None)
    notnull = conn.execute(
        """SELECT attnotnull FROM pg_attribute
            WHERE attrelid = 'workflow_a_control.client_dataset_coverage'::regclass
              AND attname = 'schedule_id'"""
    ).fetchone()[0]
    _check("coverage.schedule_id stays NOT NULL", notnull is True)


# ---- Decision B: recovery exclusivity -------------------------------------

def insert_recovery(conn, *, schedule_id: str, status="PLANNED",
                    approval_ref="APPROVAL-1", window_start=None) -> None:
    # `ck_client_dataset_recovery_run_window_anchor` requires
    # window_start_ts = expected_old_covered_through_ts, so the anchor moves with
    # the window rather than being pinned to W.
    window_start = window_start or W
    # A terminal status must carry started_at/finished_at (and, for a
    # non-success, an error classification) or migration 058's consistency
    # constraints reject it.
    terminal = status in ("SUCCESS", "FAILED", "FINALIZATION_CONFLICT")
    started = datetime.now(timezone.utc) if terminal else None
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_recovery_run
          (recovery_run_id,client_id,client_code,schedule_id,dataset_name,
           window_start_ts,window_end_ts,expected_old_covered_through_ts,status,
           reason,approval_ref,repository_head,pagination_mode,
           stabilization_delay_seconds,overlap_seconds,max_recovery_span_seconds,
           initial_coverage_snapshot,initial_coverage_fingerprint,
           started_at,finished_at,error_classification)
          VALUES (%s,%s,%s,%s,'trips_sync',%s,%s,%s,%s,'reason',%s,%s,%s,
                  10800,3600,2678400,'{}'::jsonb,%s,%s,%s,%s)""",
        (
            str(uuid.uuid4()), CID, CODE, schedule_id, window_start,
            window_start + timedelta(days=1), window_start, status, approval_ref,
            "0" * 40, MODE, "a" * 64, started, started,
            "TEST" if status in ("FAILED", "FINALIZATION_CONFLICT") else None,
        ),
    )


def test_decision_b_recovery_exclusivity_follows_the_watermark(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    insert_recovery(conn, schedule_id=SID_BASE, status="RUNNING")
    conn.commit()

    _refuses(
        "a concurrent recovery on ANOTHER cadence of the same dataset is rejected",
        lambda: insert_recovery(conn, schedule_id=SID_WEEKLY, status="PLANNED"),
        conn,
    )

    # Terminal recoveries never block: the exclusivity is about active work.
    conn.execute(
        "UPDATE workflow_a_control.client_dataset_recovery_run"
        "   SET status='SUCCESS', started_at=now(), finished_at=now()"
    )
    conn.commit()
    insert_recovery(conn, schedule_id=SID_WEEKLY, status="PLANNED",
                    approval_ref="APPROVAL-2")
    conn.commit()
    _check("a recovery is allowed once the previous one is terminal", True)


def test_one_approval_stays_one_execution_across_cadences(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    insert_recovery(conn, schedule_id=SID_BASE, status="SUCCESS")
    conn.commit()
    _refuses(
        "the same approved window cannot be replayed under another cadence",
        lambda: insert_recovery(
            conn, schedule_id=SID_WEEKLY, status="SUCCESS",
        ),
        conn,
    )


# ---- migration fail-closed guards -----------------------------------------

def test_the_migration_refuses_two_watermarks_for_one_dataset(conn) -> None:
    apply_chain(conn, through_062=False)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)
    # A second schedule is only possible once the old constraint is gone, which
    # is exactly the corrupt shape the guard exists to catch.
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_schedule"
        " DROP CONSTRAINT uq_client_dataset_schedule"
    )
    insert_schedule(conn, schedule_id=SID_WEEKLY)
    seed_coverage(conn, schedule_id=SID_BASE)
    seed_coverage(conn, schedule_id=SID_WEEKLY, w=W - timedelta(days=5))
    conn.commit()

    _refuses(
        "062 refuses two watermarks for one dataset",
        lambda: conn.execute(_sql(MIGRATION_NAME)),
        conn,
    )
    rows = conn.execute(
        "SELECT count(*) FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()[0]
    _check("062 merged, deleted and rewrote nothing", rows == 2, f"rows={rows}")


def test_the_migration_refuses_a_missing_uniqueness_constraint(conn) -> None:
    """Without it, labelling every row DAILY would be an inference."""
    apply_chain(conn, through_062=False)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_schedule"
        " DROP CONSTRAINT uq_client_dataset_schedule"
    )
    conn.commit()
    _refuses(
        "062 refuses when the schedule uniqueness it reasons from is absent",
        lambda: conn.execute(_sql(MIGRATION_NAME)),
        conn,
    )


def test_the_migration_refuses_concurrent_active_recoveries(conn) -> None:
    apply_chain(conn, through_062=False)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)
    conn.execute(
        "DROP INDEX workflow_a_control.uq_client_dataset_recovery_run_active"
    )
    insert_recovery(conn, schedule_id=SID_BASE, status="RUNNING",
                    approval_ref="A-1")
    insert_recovery(conn, schedule_id=SID_BASE, status="PLANNED",
                    approval_ref="A-2",
                    window_start=W - timedelta(days=9))  # a different window
    conn.commit()
    _refuses(
        "062 refuses to re-key exclusivity over concurrent active recoveries",
        lambda: conn.execute(_sql(MIGRATION_NAME)),
        conn,
    )


def test_the_migration_leaves_nothing_behind_when_it_refuses(conn) -> None:
    """The explicit transaction is what makes a refusal total."""
    apply_chain(conn, through_062=False)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)
    seed_coverage(conn, schedule_id=SID_BASE)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_schedule"
        " DROP CONSTRAINT uq_client_dataset_schedule"
    )
    conn.commit()
    try:
        conn.execute(_sql(MIGRATION_NAME))
        conn.commit()
    except Exception:
        conn.rollback()
    present = conn.execute(
        """SELECT count(*) FROM pg_attribute
            WHERE attrelid = 'workflow_a_control.client_dataset_schedule'::regclass
              AND attname = 'run_type' AND NOT attisdropped"""
    ).fetchone()[0]
    _check(
        "a refused 062 installs no partial re-key",
        present == 0,
        "run_type exists after a refusal; the migration is not atomic",
    )


# ---- zero behaviour change ------------------------------------------------

def test_zero_behaviour_change_for_a_single_base_schedule(conn) -> None:
    """The whole point of M5: with one DAILY schedule, nothing observable moves."""
    from jobs.api.telematics.coverage_windows import derive_effective_window

    fresh(conn)
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    fire = W + timedelta(hours=6)
    window = derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=4,
        stabilization_delay_seconds=10800,
        overlap_seconds=3600,
        max_recovery_span_seconds=2678400,
        coverage_start_ts=state.coverage_start_ts,
        covered_through_ts=state.covered_through_ts,
    )
    expected_end = fire - timedelta(seconds=10800)
    expected_start = min(
        fire - timedelta(days=4) - timedelta(seconds=10800 + 3600),
        state.covered_through_ts - timedelta(seconds=3600),
    )
    _check(
        "E_end is unchanged by M5",
        window.effective_window_end_ts == expected_end,
    )
    _check(
        "E_start is unchanged by M5",
        window.effective_window_start_ts == expected_start,
    )
    _check("W is unchanged by M5", state.covered_through_ts == W)
    _check(
        "the fingerprint algorithm is unchanged",
        cf.COVERAGE_FINGERPRINT_VERSION == "telematics-coverage-fingerprint/1"
        and list(cf.COVERAGE_FINGERPRINT_FIELDS)[0] == "schedule_id",
        "changing the projection would invalidate every stored fingerprint",
    )



# ---- Finding 1: coverage owner / provenance coherence ---------------------

def test_coherent_coverage_provenance_is_accepted(conn) -> None:
    fresh(conn)
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    _check(
        "a coverage row anchored to its own base schedule loads",
        state is not None and state.schedule_id == SID_BASE,
    )


def test_coverage_provenance_from_another_owner_is_structurally_rejected(conn) -> None:
    """The composite FK, not application discipline, is what refuses this."""
    fresh(conn)
    conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    conn.commit()

    _refuses(
        "coverage anchored to ANOTHER CLIENT's schedule is rejected",
        lambda: seed_coverage(conn, schedule_id=SID_OTHER),
        conn,
    )
    _refuses(
        "coverage anchored to ANOTHER DATASET's schedule is rejected",
        lambda: seed_coverage(conn, schedule_id=SID_OTHER_DATASET),
        conn,
    )
    # And an UPDATE cannot smuggle in what the INSERT refused.
    seed_coverage(conn, schedule_id=SID_BASE)
    conn.commit()
    _refuses(
        "re-anchoring an existing coverage row to a foreign schedule is rejected",
        lambda: conn.execute(
            "UPDATE workflow_a_control.client_dataset_coverage"
            "   SET schedule_id = %s WHERE client_id = %s",
            (SID_OTHER, CID),
        ),
        conn,
    )


def test_the_migration_refuses_pre_existing_incoherent_coverage(conn) -> None:
    """Constructed under the PRE-062 schema, where the old FK allowed it."""
    apply_chain(conn, through_062=False)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)
    seed_other_owners(conn)
    # 057's single-column FK permits this; that is exactly the hole.
    seed_coverage(conn, schedule_id=SID_OTHER)
    conn.commit()

    _refuses(
        "062 refuses coverage whose provenance belongs to another owner",
        lambda: conn.execute(_sql(MIGRATION_NAME)),
        conn,
    )
    rows = conn.execute(
        "SELECT count(*) FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()[0]
    present = conn.execute(
        """SELECT count(*) FROM pg_attribute
            WHERE attrelid = 'workflow_a_control.client_dataset_schedule'::regclass
              AND attname = 'run_type' AND NOT attisdropped"""
    ).fetchone()[0]
    _check("the refused migration reassigned nothing", rows == 1, f"rows={rows}")
    _check("the refused migration installed no partial state", present == 0)


def test_runtime_refuses_a_watermark_anchored_to_a_reconciliation_cadence(conn) -> None:
    """The half the FK cannot express, enforced fail-closed at load time.

    A partial unique index cannot be a foreign-key target, so "the anchor carries
    the base role" is not a constraint. It is enforced at the coverage INSERT
    surface and re-checked here — this test constructs the drifted state directly
    and proves the dispatcher refuses to run against it.
    """
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    # Legal under every constraint: same owner, real schedule — wrong role.
    conn.execute(
        "UPDATE workflow_a_control.client_dataset_coverage"
        "   SET schedule_id = %s WHERE client_id = %s AND dataset_name = 'trips_sync'",
        (SID_WEEKLY, CID),
    )
    conn.commit()

    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    _check(
        "a watermark anchored to a reconciliation cadence is not a usable claim",
        state is None,
        "the loader returned a coverage state anchored to a non-base schedule",
    )
    after = conn.execute(
        "SELECT covered_through_ts FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()[0]
    _check("the refusal mutated nothing", after == W)


# ---- Finding 2: base/provenance lifecycle ---------------------------------

def test_deleting_the_anchoring_schedule_cannot_destroy_the_watermark(conn) -> None:
    """057's ON DELETE CASCADE was correct for a private watermark, not a shared one."""
    fresh(conn)
    _refuses(
        "deleting the anchoring base schedule is refused",
        lambda: conn.execute(
            "DELETE FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s",
            (SID_BASE,),
        ),
        conn,
    )
    row = conn.execute(
        "SELECT schedule_id::text, covered_through_ts"
        "  FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()
    _check(
        "the shared watermark is untouched after the refused delete",
        row is not None and row[0] == SID_BASE and row[1] == W,
    )


def test_the_anchor_holds_once_a_reconciliation_cadence_exists(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()

    _refuses(
        "the base cannot be deleted while a reconciliation cadence shares its watermark",
        lambda: conn.execute(
            "DELETE FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s",
            (SID_BASE,),
        ),
        conn,
    )
    _refuses(
        "the base cannot be re-owned out from under the watermark",
        lambda: conn.execute(
            "UPDATE workflow_a_control.client_dataset_schedule"
            "   SET client_id = %s WHERE schedule_id = %s",
            (OTHER_CID, SID_BASE),
        ),
        conn,
    )
    _check(
        "a reconciliation schedule that anchors nothing stays deletable",
        conn.execute(
            "DELETE FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s",
            (SID_WEEKLY,),
        ) is not None,
    )
    conn.rollback()

    row = conn.execute(
        "SELECT schedule_id::text, covered_through_ts, bootstrap_status"
        "  FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()
    _check(
        "the shared watermark is semantically unchanged throughout",
        row is not None and row[0] == SID_BASE and row[1] == W
        and row[2] == "READY",
    )


def test_no_second_watermark_can_be_bootstrapped_for_the_dataset(conn) -> None:
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()
    _refuses(
        "a reconciliation cadence cannot become a second anchor",
        lambda: seed_coverage(conn, schedule_id=SID_WEEKLY),
        conn,
    )


def test_base_cardinality_is_enforced_in_its_claimed_domain(conn) -> None:
    """"Exactly one" is true where coverage exists; globally it is at most one.

    The global claim was never true and must not be documented: the release
    gate's own fleet fixture deliberately contains an enabled client that owns no
    trips schedule at all. What IS enforced, and what M6 needs, is proven here.
    """
    fresh(conn)
    _refuses(
        "at most one base schedule per (client, dataset)",
        lambda: insert_schedule(
            conn, schedule_id=str(uuid.uuid4()), run_type=SCHEDULE_RUN_TYPE_BASE,
        ),
        conn,
    )
    # A client may own NO schedule for a dataset — so "exactly one base schedule"
    # is false globally, which is why the invariant is scoped to coverage.
    n = conn.execute(
        "SELECT count(*) FROM workflow_a_control.client_dataset_schedule"
        " WHERE client_id = %s AND dataset_name = 'eco_person_driving_weekly_snapshot'",
        (CID,),
    ).fetchone()[0]
    _check("a dataset may legitimately have no schedule at all", n == 0)
    # And where coverage DOES exist, exactly one schedule anchors it and cannot
    # be removed — proven by the delete refusal plus the single-row uniqueness.
    anchors = conn.execute(
        """SELECT count(*) FROM workflow_a_control.client_dataset_coverage c
             JOIN workflow_a_control.client_dataset_schedule s
               ON  s.schedule_id  = c.schedule_id
               AND s.client_id    = c.client_id
               AND s.dataset_name = c.dataset_name
            WHERE c.client_id = %s AND c.dataset_name = 'trips_sync'""",
        (CID,),
    ).fetchone()[0]
    _check("exactly one schedule anchors the coverage-bearing dataset", anchors == 1)


# ---- Finding 3: recovery owner / provenance coherence ---------------------

def test_recovery_provenance_must_match_its_stored_owner(conn) -> None:
    fresh(conn)
    insert_recovery(conn, schedule_id=SID_BASE, status="PLANNED")
    conn.commit()
    _check("a coherent recovery row is accepted", True)

    conn.execute("DELETE FROM workflow_a_control.client_dataset_recovery_run")
    conn.commit()
    _refuses(
        "a recovery naming ANOTHER CLIENT's schedule is rejected",
        lambda: insert_recovery(conn, schedule_id=SID_OTHER),
        conn,
    )
    _refuses(
        "a recovery naming ANOTHER DATASET's schedule is rejected",
        lambda: insert_recovery(conn, schedule_id=SID_OTHER_DATASET),
        conn,
    )


def test_the_migration_refuses_pre_existing_incoherent_recovery(conn) -> None:
    apply_chain(conn, through_062=False)
    seed_client(conn)
    insert_schedule(conn, schedule_id=SID_BASE)
    seed_other_owners(conn)
    seed_coverage(conn, schedule_id=SID_BASE)
    # 058's single-column FK permits a recovery row whose stored owner disagrees
    # with the schedule it names.
    insert_recovery(conn, schedule_id=SID_OTHER, status="SUCCESS")
    conn.commit()

    _refuses(
        "062 refuses recovery rows whose schedule belongs to another owner",
        lambda: conn.execute(_sql(MIGRATION_NAME)),
        conn,
    )
    rows = conn.execute(
        "SELECT count(*) FROM workflow_a_control.client_dataset_recovery_run"
    ).fetchone()[0]
    _check("the refused migration rewrote no recovery evidence", rows == 1)


def test_owner_keyed_recovery_exclusivity_is_owner_scoped(conn) -> None:
    """Blocks the same owner across cadences; leaves a different owner alone."""
    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    insert_recovery(conn, schedule_id=SID_BASE, status="RUNNING")
    conn.commit()

    _refuses(
        "an active recovery on another cadence of the SAME owner is blocked",
        lambda: insert_recovery(conn, schedule_id=SID_WEEKLY),
        conn,
    )

    # A different owner must remain completely unaffected.
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_recovery_run
          (recovery_run_id,client_id,client_code,schedule_id,dataset_name,
           window_start_ts,window_end_ts,expected_old_covered_through_ts,status,
           reason,approval_ref,repository_head,pagination_mode,
           stabilization_delay_seconds,overlap_seconds,max_recovery_span_seconds,
           initial_coverage_snapshot,initial_coverage_fingerprint)
          VALUES (%s,%s,%s,%s,'trips_sync',%s,%s,%s,'PLANNED','reason',%s,%s,%s,
                  10800,3600,2678400,'{}'::jsonb,%s)""",
        (str(uuid.uuid4()), OTHER_CID, OTHER_CODE, SID_OTHER, W,
         W + timedelta(days=1), W, "OTHER-APPROVAL", "0" * 40, MODE, "b" * 64),
    )
    conn.commit()
    _check("a recovery for a DIFFERENT owner is unaffected", True)


# ---- Finding 5: operator base-role resolvers ------------------------------

def test_operator_resolvers_pick_the_base_schedule(conn) -> None:
    """Every live base-role resolver, against a valid two-cadence dataset."""
    import ops.audit_telematics_cold_start as cold_start_audit
    import ops.audit_telematics_coverage_bootstrap as coverage_audit
    import ops.bootstrap_telematics_trips_coverage as bootstrap

    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()

    with conn.cursor(row_factory=dict_row) as cur:
        resolved = coverage_audit.resolve_schedule(
            cur, client_id=CID, dataset_name="trips_sync",
        )
        _check(
            "audit_telematics_coverage_bootstrap.resolve_schedule picks the base",
            resolved["schedule_id"] == SID_BASE,
            f"picked {resolved['schedule_id']}",
        )

        n = bootstrap._count_target_enabled_schedules(
            cur, client_id=CID, dataset_name="trips_sync",
        )
        _check(
            "bootstrap._count_target_enabled_schedules counts base only",
            n == 1, f"n={n}",
        )

        # The cold-start resolver exists for a schedule that has never fired,
        # so its precondition is a DISABLED base. Satisfy that precondition
        # rather than weakening it; the assertion under test is which row it
        # picks when a reconciliation cadence is also present.
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_schedule"
            "   SET enabled = false WHERE schedule_id = %s",
            (SID_BASE,),
        )
        cold = cold_start_audit.resolve_cold_start_schedule(
            cur, client_id=CID, dataset_name="trips_sync",
            expected_schedule_id=SID_BASE,
        )
        _check(
            "audit_telematics_cold_start.resolve_cold_start_schedule picks the base",
            cold["schedule_id"] == SID_BASE,
            f"picked {cold['schedule_id']}",
        )
    conn.rollback()

    still_there = conn.execute(
        "SELECT count(*) FROM workflow_a_control.client_dataset_schedule"
        " WHERE schedule_id = %s AND run_type = %s",
        (SID_WEEKLY, SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION),
    ).fetchone()[0]
    _check("no resolver mutated the reconciliation schedule", still_there == 1)


def test_the_child_job_reads_the_base_schedule_configuration(conn) -> None:
    """`control_plane.load_dataset_schedule` had a LIMIT 1 with no ORDER BY."""
    import jobs.api.telematics.control_plane as control_plane

    fresh(conn)
    insert_schedule(
        conn, schedule_id=SID_WEEKLY,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly", day_of_week=0,
    )
    conn.commit()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT schedule_id::text AS schedule_id, lookback_days"
            "  FROM workflow_a_control.client_dataset_schedule"
            " WHERE client_id=%s AND dataset_name=%s AND run_type=%s LIMIT 1",
            (CID, "trips_sync", SCHEDULE_RUN_TYPE_BASE),
        )
        row = cur.fetchone()
    _check(
        "the child job's schedule read resolves the base row deterministically",
        row is not None and row["schedule_id"] == SID_BASE,
    )
    _check(
        "control_plane scopes its read to the base role",
        "run_type=%s" in
        pathlib.Path(ROOT / "jobs/api/telematics/control_plane.py").read_text(),
    )


def test_the_trip_metrics_diagnostic_reports_each_client_once_and_keeps_no_schedule_clients(conn, dsn) -> None:
    """`ops/checks/check_trip_metrics_population_source._load_clients`.

    The last surviving cadence-ambiguous resolver. Its LEFT JOIN matched every
    cadence of `trips_sync`, so a valid second schedule returned the client
    twice — two records, two client-database probes, and two possibly
    disagreeing answers for the same client.

    Proven here against a real two-cadence dataset, driving the real function
    rather than re-implementing its SQL.

    The fix has a failure mode of its own, and this proves that too. Scoping the
    join to the base role is only correct while the predicate stays in the JOIN
    condition: moved into the WHERE clause it reads identically, still emits each
    client once, and still reports the base configuration — but it silently
    demotes the LEFT JOIN to an inner join and erases every enabled client that
    has no `trips_sync` schedule at all. Those are precisely the clients an
    onboarding diagnostic exists to surface, and their disappearance looks like
    "nothing to report" rather than like a bug.

    So a third client is seeded with NO `trips_sync` schedule of any cadence, and
    both invocation paths must still emit it with NULL schedule columns. Under
    the WHERE variant `nosched` is empty in the unfiltered call and
    `rows_nosched` is empty in the filtered one, so this test fails on the
    regression without needing to mutate the source to prove it.
    """
    import os

    import ops.checks.check_trip_metrics_population_source as diag

    fresh(conn)
    # Enabled, and deliberately schedule-less for `trips_sync`. It does carry a
    # schedule for a DIFFERENT dataset, so the join's `dataset_name` predicate is
    # exercised in the same position as the role predicate — both must stay in
    # the JOIN condition for this client to survive.
    conn.execute(
        """INSERT INTO workflow_a_control.client_account
          (client_id,client_code,client_name,provider_type,provider_base_url,
           provider_basic_auth_username,provider_basic_auth_password_secret_ref,
           client_db_host,client_db_port,client_db_name,client_db_user,
           client_db_password_secret_ref,client_db_schema,speed_trigger_filter_text,
           enabled,trips_pagination_mode)
          VALUES (%s,%s,'No Schedule','telematics','https://example.invalid','u','REF',
                  '127.0.0.1',5432,'db','u','REF','public','speeding',true,%s)""",
        (NOSCHED_CID, NOSCHED_CODE, MODE),
    )
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
           run_time,timezone,lookback_days,overwrite_existing)
          VALUES (%s,%s,%s,%s,true,'daily','02:00','UTC',4,true)""",
        (SID_NOSCHED_OTHER_DATASET, NOSCHED_CID, NOSCHED_CODE, OTHER_DATASET),
    )
    conn.commit()
    # The reconciliation row carries DIFFERENT decorating values, so selecting
    # the wrong cadence is detectable rather than merely duplicated.
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
           day_of_week,run_time,timezone,lookback_days,overwrite_existing,
           run_type,event_enrichment_mode)
          VALUES (%s,%s,%s,'trips_sync',false,'weekly',0,'02:30','UTC',16,true,
                  %s,'disabled')""",
        (SID_WEEKLY, CID, CODE, SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION),
    )
    conn.commit()

    parsed = {
        piece.split("=", 1)[0]: piece.split("=", 1)[1]
        for piece in _dsn_parts(dsn)
    }
    previous = {k: os.environ.get(k) for k in
                ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
                 "POSTGRES_USER", "POSTGRES_PASSWORD")}
    os.environ.update({
        "POSTGRES_HOST": parsed["host"], "POSTGRES_PORT": parsed["port"],
        "POSTGRES_DB": parsed["dbname"], "POSTGRES_USER": parsed["user"],
        "POSTGRES_PASSWORD": parsed.get("password", ""),
    })
    try:
        rows = diag._load_clients(None)
        rows_scoped = diag._load_clients(CODE)
        rows_nosched = diag._load_clients(NOSCHED_CODE)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    mine = [r for r in rows if r["client_code"] == CODE]
    _check(
        "the diagnostic emits the two-cadence client exactly once",
        len(mine) == 1,
        f"emitted {len(mine)} rows for {CODE}",
    )
    if mine:
        _check(
            "it reports the BASE schedule's configuration",
            mine[0]["trips_sync_enabled"] is True
            and mine[0]["event_enrichment_mode"] == "enabled",
            f"got enabled={mine[0]['trips_sync_enabled']} "
            f"mode={mine[0]['event_enrichment_mode']}",
        )
    _check(
        "the same holds when a single client is requested",
        len(rows_scoped) == 1,
        f"emitted {len(rows_scoped)} rows",
    )
    # The second owner seeded by `fresh` has one base schedule and no
    # reconciliation cadence: pre-M5 behaviour, unchanged.
    other = [r for r in rows if r["client_code"] == OTHER_CODE]
    _check(
        "a single-base client is reported exactly once, as before M5",
        len(other) == 1 and other[0]["trips_sync_enabled"] is True,
        f"emitted {len(other)} rows for {OTHER_CODE}",
    )

    # --- the LEFT JOIN must survive the base scoping -----------------------
    nosched = [r for r in rows if r["client_code"] == NOSCHED_CODE]
    _check(
        "an enabled client with no trips_sync schedule is still reported",
        len(nosched) == 1,
        f"emitted {len(nosched)} rows for {NOSCHED_CODE}; 0 means the role or "
        f"dataset predicate moved from the JOIN condition into the WHERE clause "
        f"and turned the LEFT JOIN into an inner join",
    )
    if nosched:
        _check(
            "its schedule-derived columns are NULL, not defaulted",
            nosched[0]["trips_sync_enabled"] is None
            and nosched[0]["event_enrichment_mode"] is None,
            f"got enabled={nosched[0]['trips_sync_enabled']!r} "
            f"mode={nosched[0]['event_enrichment_mode']!r}",
        )
    _check(
        "the no-schedule client is reachable by --client-code too",
        len(rows_nosched) == 1
        and rows_nosched[0]["client_code"] == NOSCHED_CODE,
        f"emitted {len(rows_nosched)} rows; the filtered path must not narrow "
        f"LEFT JOIN semantics an operator can see in the unfiltered listing",
    )
    if rows_nosched:
        _check(
            "the filtered path reports NULL schedule columns as well",
            rows_nosched[0]["trips_sync_enabled"] is None
            and rows_nosched[0]["event_enrichment_mode"] is None,
            f"got enabled={rows_nosched[0]['trips_sync_enabled']!r} "
            f"mode={rows_nosched[0]['event_enrichment_mode']!r}",
        )

    # Defence in depth for the same defect, read off the shipped source rather
    # than the database: the role predicate must sit in the JOIN condition.
    # The behavioural proof above is the primary evidence; this one names the
    # exact edit that would break it.
    body = inspect.getsource(diag._load_clients).split("LEFT JOIN", 1)[-1]
    on_block, _, where_block = body.partition("WHERE")
    _check(
        "the base-role predicate is a JOIN condition, not a WHERE predicate",
        re.search(r"run_type\s*=\s*%s", on_block)
        and not re.search(r"run_type\s*=\s*%s", where_block),
        "moving `cds.run_type = %s` below the WHERE keyword drops every "
        "client that has no trips_sync schedule",
    )


def _dsn_parts(dsn: str) -> list[str]:
    """Accept either DSN form; the suite is invoked with a URL."""
    if "://" in dsn:
        from urllib.parse import urlparse

        parsed = urlparse(dsn)
        return [
            f"host={parsed.hostname}", f"port={parsed.port or 5432}",
            f"dbname={(parsed.path or '/').lstrip('/')}",
            f"user={parsed.username or ''}",
            f"password={parsed.password or ''}",
        ]
    return [piece for piece in dsn.split() if "=" in piece]

def main() -> int:
    print("=== static ===")
    test_migration_is_the_next_one_and_is_transactional()
    test_python_and_sql_agree_on_the_vocabulary()
    test_the_validator_fails_closed()
    test_run_type_cadence_coherence_in_python()
    test_no_production_module_addresses_coverage_by_schedule_id()
    test_release_gate_declares_062()
    test_operator_surfaces_scope_to_the_base_schedule()
    test_no_live_schedule_resolver_escapes_the_base_role_audit()
    test_m5_creates_no_reconciliation_schedule_anywhere()

    dsn = os.environ.get(ENV, "").strip()
    if not dsn:
        print(f"\nSKIP PostgreSQL checks — set {ENV} to a disposable database")
    else:
        require_loopback_dsn_or_exit(dsn, label=ENV)
        import psycopg

        print("\n=== postgres ===")
        with psycopg.connect(dsn, autocommit=False) as conn:
            test_existing_rows_become_base_schedules(conn)
            test_three_roles_coexist_and_a_fourth_row_is_rejected(conn)
            test_the_database_closes_the_run_type_vocabulary(conn)
            test_the_column_defaults_to_the_base_role(conn)
            test_one_watermark_per_dataset(conn)
            test_two_cadences_resolve_the_same_coverage_row(conn)
            test_a_reconciliation_fire_is_not_refused_for_a_schedule_mismatch(conn)
            test_advance_then_observe_across_cadences(conn)
            test_a_behind_w_reconciliation_run_is_a_validated_no_op(conn)
            test_recovery_require_advance_still_refuses_a_behind_w_window(conn)
            test_every_cas_field_is_compared(conn)
            test_the_cas_fingerprint_did_not_narrow(conn)
            # --- independent-review corrections ---
            test_coherent_coverage_provenance_is_accepted(conn)
            test_coverage_provenance_from_another_owner_is_structurally_rejected(conn)
            test_the_migration_refuses_pre_existing_incoherent_coverage(conn)
            test_runtime_refuses_a_watermark_anchored_to_a_reconciliation_cadence(conn)
            test_deleting_the_anchoring_schedule_cannot_destroy_the_watermark(conn)
            test_the_anchor_holds_once_a_reconciliation_cadence_exists(conn)
            test_no_second_watermark_can_be_bootstrapped_for_the_dataset(conn)
            test_base_cardinality_is_enforced_in_its_claimed_domain(conn)
            test_recovery_provenance_must_match_its_stored_owner(conn)
            test_the_migration_refuses_pre_existing_incoherent_recovery(conn)
            test_owner_keyed_recovery_exclusivity_is_owner_scoped(conn)
            test_operator_resolvers_pick_the_base_schedule(conn)
            test_the_child_job_reads_the_base_schedule_configuration(conn)
            test_the_trip_metrics_diagnostic_reports_each_client_once_and_keeps_no_schedule_clients(conn, dsn)
            test_concurrent_advancement_serializes_on_the_shared_row(conn, dsn)
            test_decision_a_pre_m5_release_still_operates_after_062(conn)
            test_the_rollback_anchor_survives_the_migration(conn)
            test_decision_b_recovery_exclusivity_follows_the_watermark(conn)
            test_one_approval_stays_one_execution_across_cadences(conn)
            test_the_migration_refuses_two_watermarks_for_one_dataset(conn)
            test_the_migration_refuses_a_missing_uniqueness_constraint(conn)
            test_the_migration_refuses_concurrent_active_recoveries(conn)
            test_the_migration_leaves_nothing_behind_when_it_refuses(conn)
            test_zero_behaviour_change_for_a_single_base_schedule(conn)

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s)")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
