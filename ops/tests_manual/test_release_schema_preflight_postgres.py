#!/usr/bin/env python3
"""Release activation must prove its schema prerequisites, or refuse.

THE DEFECT THIS CLOSES.
    `manage_release.py activate` verified release *bytes* and nothing else. M4
    made that concrete: the trip upsert names `client_trips.first_seen_request_id`
    unconditionally, so activating the M4 release while one client business
    database still lacks migration 047 would point production at code that
    client cannot run — and the failure would be every ingest, fleet-wide, not
    a single degraded feature. Independent review classified it as blocking.

WHAT IS TESTED HERE, AND WHY IT NEEDS A REAL DATABASE.
    Every assertion below is about the difference between what a ledger *claims*
    and what a schema *is*. That difference cannot be faked in a unit test: the
    whole point is that `public.schema_migrations` can say `applied` while the
    column is absent, which is exactly what a half-finished rollout looks like.
    So the fleet is built for real — a platform database and three client
    databases in the disposable instance — and then damaged in one specific way
    per case.

    The requirements parser is exercised on its own, without a database, in
    `test_release_schema_requirements_parser.py`: a malformed DECLARATION is a
    different failure from a malformed SCHEMA and must not be diagnosed here.

DESTRUCTIVE. Creates and drops its own schemas and client databases.
"""
from __future__ import annotations

import json
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops.release_schema_preflight import (  # noqa: E402
    AffectedClient,
    SchemaPreflightError,
    enumerate_affected_clients,
    fleet_fingerprint,
    activation_fence,
    FIRST_SEEN_EXPAND_CONSTRAINT,
    FIRST_SEEN_EXPAND_DEFINITION,
    FIRST_SEEN_STRICT_CONSTRAINT,
    FIRST_SEEN_STRICT_DEFINITION,
    inspect_release_bridge_compatibility,
    parse_requirements,
    relation_defects,
    verify_schema_prerequisites,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "TELEMATICS_M4_PREFLIGHT_TEST_DSN"

PLATFORM_MIGRATIONS = (
    "008_workflow_a_control_plane.sql", "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    # S15's prerequisites, applied in real migration order. 068 declares
    # foreign keys onto `portal_clients` and `portal_database_datasets`, so a
    # fixture that jumped straight to 068 could not execute it at all. These
    # two are NOT declared in db/schema_requirements.json and are applied here
    # only because the migration that IS declared cannot run without them.
    "033_portal_client_access.sql",
    "035_portal_database_catalog.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "061_workflow_a_provider_request_log.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
    # M-LAG. Declared in db/schema_requirements.json, so the correct-fleet case
    # must apply it or the gate refuses activation — which is the gate working,
    # not a defect.
    "063_workflow_a_trip_delivery_lag_daily.sql",
    # S15 Report Explorer generated-report persistence. Declared in
    # db/schema_requirements.json exactly like M-LAG's 063, and declared as TWO
    # requirements: 068 installs the three relations and 069 is the corrective
    # migration that closes the independent-review findings over them. A
    # release carrying the S15 code is not satisfied by 068 alone, so the
    # correct-fleet fixture must materialize both or the gate refuses
    # activation — which would again be the gate working, not a defect.
    "068_portal_generated_reports.sql",
    "069_portal_generated_reports_integrity.sql",
)
PLATFORM_MIGRATION = "061_workflow_a_provider_request_log.sql"
#: The S15 platform migrations, in the order `build_fleet` applies them.
PLATFORM_MIGRATION_S15 = "068_portal_generated_reports.sql"
PLATFORM_MIGRATION_S15_INTEGRITY = "069_portal_generated_reports_integrity.sql"
#: The three relations 068 installs, in drop order (child first).
S15_RELATIONS = (
    "portal_generated_report_files",
    "portal_generated_report_instances",
    "portal_generated_report_definitions",
)

#: The bootstrap relations 068's foreign keys reach that NO migration creates.
#:
#: A real platform database is `api/main.py`'s `SCHEMA_SQL` bootstrap first and
#: `db/migrations/` on top of it, so `artifact_users` and `artifacts` are
#: already there when 033/035/068 run. This disposable database starts empty,
#: so the two bootstrap relations are created here — narrowed to the columns
#: the migrations under test actually reach: `artifact_users(user_id)` for
#: 033/035's grant columns and `artifacts(artifact_id)` for 068's file
#: reference. Nothing in `db/schema_requirements.json` declares either of them,
#: so nothing here is asserted against them; they exist so the DECLARED
#: migration can execute instead of failing on a missing foreign-key target.
#:
#: No rows are seeded. Every reference 068 declares is a foreign key, and a
#: foreign key needs its target RELATION, never a target row — the preflight
#: matcher reads pg_catalog and never reads a single tuple.
#:
#: `pgcrypto` is deliberately NOT created here: `gen_random_uuid()` is core
#: since PostgreSQL 13, and 008 — the first migration this fixture applies —
#: installs the extension for everything downstream that really needs it.
PORTAL_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS public.artifact_users (
  user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  is_active BOOLEAN NOT NULL DEFAULT true,
  is_admin BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.artifacts (
  artifact_id UUID PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind TEXT NOT NULL DEFAULT 'report',
  filename TEXT NOT NULL DEFAULT 'artifact.pdf',
  content_type TEXT NOT NULL DEFAULT 'application/pdf',
  size_bytes BIGINT NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  storage_backend TEXT NOT NULL DEFAULT 'minio',
  storage_key TEXT NOT NULL DEFAULT ''
);
"""
CLIENT_MIGRATION = "047_client_trips_first_seen_request_id.sql"
#: M-LAG's client-side prerequisite. Applied only where 047 genuinely ran: its
#: pairing CHECK is defined over 047's column, so the deliberately-broken
#: scenarios below must not receive it.
CLIENT_MIGRATION_MLAG = "048_client_trips_first_seen_response_received_at.sql"
#: Driver Eco Dashboard V1 host delivery ledger. Its physical contract is
#: declared in `db/schema_requirements.json` and installed by TWO migrations:
#: 049 creates the ledger and is applied shared history, and 050 adds the
#: reviewed external-mailer ownership contract as a forward migration. The
#: requirement is keyed on 050, because that is the file whose presence makes
#: the declared contract true.
CLIENT_MIGRATION_ECO_DASH_BASE = "049_eco_dashboard_delivery_operation.sql"
CLIENT_MIGRATION_ECO_DASH = "050_eco_dashboard_external_mailer_ownership.sql"
CLIENT_MIGRATIONS_ECO_DASH = (CLIENT_MIGRATION_ECO_DASH_BASE,
                              CLIENT_MIGRATION_ECO_DASH)
MODE = "data_invariants_v1"

#: Four enabled clients. Three own a `trips_sync` schedule; **the fourth does
#: not**, and that is the point — independent review reproduced a gate that
#: silently skipped exactly such a client and passed. Four also means "one
#: broken client" is a genuinely partial fleet rather than the whole of it.
CLIENTS = (
    ("bd7662a5-eeb4-4614-8720-d477abfcb227", "AAA00001", "m4pf_a", True),
    ("b454f82c-5857-4bab-8342-b7258e5cf7de", "BBB00001", "m4pf_b", True),
    ("f6222a11-06ee-4e4f-8b25-302a9d963cfa", "CCC00001", "m4pf_c", True),
    ("44444444-4444-4444-8444-444444444444", "DDD00001", "m4pf_d", False),
)
ALL_CODES = tuple(code for _id, code, _db, _sched in CLIENTS)
NO_SCHEDULE_CODE = "DDD00001"

_failures: List[str] = []


def _release_requirements() -> List[Dict]:
    """The requirements this release declares, read from the tree under test."""
    return json.loads(
        (ROOT / "db" / "schema_requirements.json").read_text(encoding="utf-8")
    )["requirements"]


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _expect_refusal(label: str, expected_code: str, fn) -> None:
    try:
        fn()
    except SchemaPreflightError as exc:
        _check(f"{label} -> {expected_code}", exc.code == expected_code,
               f"actual: {exc.code}: {exc.detail}")
        return
    _check(f"{label} -> {expected_code}", False, "the gate PASSED")


# ---------------------------------------------------------------------------
# Fixture: a real platform database and a real client fleet
# ---------------------------------------------------------------------------

def _connect(dbname: str):
    import psycopg
    dsn = os.environ[ENV]
    base = dsn.rsplit("/", 1)[0]
    return psycopg.connect(f"{base}/{dbname}")


def _admin():
    import psycopg
    conn = psycopg.connect(os.environ[ENV])
    conn.autocommit = True
    return conn


def _platform_db_name() -> str:
    return os.environ[ENV].rsplit("/", 1)[1]


def build_fleet(
    *,
    client_migration_applied=None,
    physical_column=None,
    wrong_column_type=None,
    enabled=None,
    drop_database=None,
) -> None:
    """(Re)build the platform control plane and the client databases.

    Every knob maps client_code -> bool and defaults to healthy, so each case
    damages exactly one thing and everything else stays honest.
    """
    client_migration_applied = client_migration_applied or {}
    physical_column = physical_column or {}
    wrong_column_type = wrong_column_type or {}
    enabled = enabled or {}
    drop_database = drop_database or {}

    platform = _connect(_platform_db_name())
    platform.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    # The S15 relations live in `public`, which this fixture does not drop
    # wholesale, and 068 creates them with IF NOT EXISTS. A scenario that
    # damaged one — dropped a constraint, dropped the table — would therefore
    # survive into the NEXT build and make an unrelated case fail for the wrong
    # reason. Dropping them here is the `public`-schema equivalent of the
    # `DROP SCHEMA workflow_a_control CASCADE` above: every build re-executes
    # the real 068 and 069 against a clean slate.
    for relation in S15_RELATIONS:
        platform.execute(f"DROP TABLE IF EXISTS public.{relation} CASCADE")
    platform.execute(PORTAL_BOOTSTRAP_SQL)
    platform.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migrations "
        "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    platform.execute("DELETE FROM public.schema_migrations")
    for name in PLATFORM_MIGRATIONS:
        platform.execute((ROOT / "db/migrations" / name).read_text())
        platform.execute(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
            "ON CONFLICT DO NOTHING", (name,),
        )
    for client_id, code, dbname, has_schedule in CLIENTS:
        platform.execute(
            """INSERT INTO workflow_a_control.client_account
              (client_id,client_code,client_name,provider_type,provider_base_url,
               provider_basic_auth_username,provider_basic_auth_password_secret_ref,
               client_db_host,client_db_port,client_db_name,client_db_user,
               client_db_password_secret_ref,client_db_schema,
               speed_trigger_filter_text,enabled,trips_pagination_mode)
              VALUES (%s,%s,%s,'telematics','https://example.invalid','u','REF',
                      %s,%s,%s,'u','REF','public','speeding',%s,%s)""",
            (client_id, code, code, _host(), _port(), dbname,
             enabled.get(code, True), MODE),
        )
        if has_schedule:
            platform.execute(
                """INSERT INTO workflow_a_control.client_dataset_schedule
                  (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
                   run_time,timezone,lookback_days,overwrite_existing)
                  VALUES (%s,%s,%s,'trips_sync',true,'daily','02:00','UTC',3,true)""",
                (str(uuid.uuid4()), client_id, code),
            )
    platform.commit()
    platform.close()

    admin = _admin()
    for _client_id, code, dbname, _sched in CLIENTS:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        if not drop_database.get(code, False):
            admin.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()

    for _client_id, code, dbname, _sched in CLIENTS:
        if drop_database.get(code, False):
            continue
        conn = _connect(dbname)
        conn.execute(
            """CREATE TABLE public.client_trips (
                 client_id UUID NOT NULL,
                 provider_trip_id BIGINT NOT NULL,
                 synced_at TIMESTAMPTZ NULL,
                 PRIMARY KEY (client_id, provider_trip_id))"""
        )
        conn.execute(
            "CREATE TABLE public.schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        if wrong_column_type.get(code, False):
            # Ledger will say 047 is applied; the column exists but is TEXT,
            # not uuid. A name-only check would accept this.
            conn.execute(
                "ALTER TABLE public.client_trips "
                "ADD COLUMN first_seen_request_id TEXT NULL"
            )
        elif physical_column.get(code, True):
            conn.execute(
                (ROOT / "db/client_business" / CLIENT_MIGRATION).read_text()
            )
            conn.execute(
                (ROOT / "db/client_business" / CLIENT_MIGRATION_MLAG).read_text()
            )
        if client_migration_applied.get(code, True):
            conn.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
                (CLIENT_MIGRATION,),
            )
            if physical_column.get(code, True) and not wrong_column_type.get(
                code, False
            ):
                conn.execute(
                    "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
                    (CLIENT_MIGRATION_MLAG,),
                )
        # The Driver Eco Dashboard delivery ledger is a client-business
        # prerequisite of this release too, and no scenario in this suite
        # targets it — every negative case here is about the client_trips
        # first-seen columns. It is therefore applied and recorded for every
        # client, so a scenario fails for the reason it is testing.
        for eco_dash_migration in CLIENT_MIGRATIONS_ECO_DASH:
            conn.execute(
                (ROOT / "db/client_business" / eco_dash_migration).read_text()
            )
            conn.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
                (eco_dash_migration,),
            )
        conn.commit()
        conn.close()


def _host() -> str:
    dsn = os.environ[ENV]
    return dsn.split("@")[1].split(":")[0]


def _port() -> int:
    dsn = os.environ[ENV]
    return int(dsn.split("@")[1].split(":")[1].split("/")[0])


def _platform_factory():
    return _connect(_platform_db_name())


def _client_factory(client: AffectedClient):
    return _connect(client.db_name)


#: Cached once per process. See `_repository_release_tree`.
_REPOSITORY_RELEASE_TREE: Optional[Path] = None


def _repository_declaration(applied_only: bool = True) -> Dict:
    """The working tree's declaration, narrowed to what this fixture applies.

    `build_fleet` materializes exactly `CLIENT_MIGRATION`,
    `CLIENT_MIGRATION_MLAG` and the whole `CLIENT_MIGRATIONS_ECO_DASH` chain
    in each client
    business database. A `client_business` requirement for any OTHER migration
    would therefore refuse every run here for a reason that has nothing to do
    with the gate behaviour under test — it would be asserting the working
    copy's current migration inventory, which this suite does not exist to pin.
    Platform requirements are left exactly as declared, because `build_fleet`
    applies the full platform set.

    The set is "what the fixture applies", not "what the repository declares",
    so a migration belongs here only once `build_fleet` really installs and
    records it. 049 does, so its requirement is exercised rather than narrowed
    away — and it is the whole 049 + 050 chain that is applied, so the
    requirement keyed on 050 is exercised in full.
    """
    document = json.loads(
        (ROOT / "db/schema_requirements.json").read_text(encoding="utf-8")
    )
    if applied_only:
        applied = {CLIENT_MIGRATION, CLIENT_MIGRATION_MLAG,
                   *CLIENT_MIGRATIONS_ECO_DASH}
        document["requirements"] = [
            requirement for requirement in document["requirements"]
            if requirement.get("scope") != "client_business"
            or requirement.get("migration") in applied
        ]
    return document


def _repository_release_tree() -> Path:
    """A release tree carrying `_repository_declaration`, materialized once."""
    global _REPOSITORY_RELEASE_TREE
    if _REPOSITORY_RELEASE_TREE is None:
        tree = Path(tempfile.mkdtemp(prefix="preflight-release-tree-"))
        (tree / "db").mkdir(parents=True, exist_ok=True)
        (tree / "db" / "schema_requirements.json").write_text(
            json.dumps(_repository_declaration(), indent=2), encoding="utf-8"
        )
        _REPOSITORY_RELEASE_TREE = tree
    return _REPOSITORY_RELEASE_TREE


def _run(release_tree: Path):
    if Path(release_tree) == ROOT:
        release_tree = _repository_release_tree()
    return verify_schema_prerequisites(
        release_tree=release_tree, release_id="test-release",
        platform_conn_factory=_platform_factory,
        client_conn_factory=_client_factory,
    )


# ---------------------------------------------------------------------------
# B1 / B8 / B9 — the healthy fleet passes, and passes for the right reason
# ---------------------------------------------------------------------------

def test_B1_B8_correct_fleet_passes() -> None:
    print("\n## test_B1_B8_correct_fleet_passes")
    build_fleet()
    report = _run(ROOT)
    _check("B1: a correctly migrated fleet passes", True)
    _check("B8/E1: every enabled client was actually examined",
           report.affected_client_count == len(CLIENTS),
           f"affected={report.affected_client_count}")
    # The exact set, not the count: independent review's failure was a gate
    # that examined a healthy subset and reported success.
    checked = {c["client_code"] for c in report.client_checks}
    _check("B8/E1: the checked set is exactly the enabled fleet",
           checked == set(ALL_CODES), f"checked={sorted(checked)}")
    _check("E1: including the enabled client that owns NO trips schedule",
           NO_SCHEDULE_CODE in checked)
    # Every platform requirement the release declares, not a fixed count: M5
    # added a second one, and a gate that checked only the first would be
    # exactly the partial-verification defect this suite exists to catch.
    declared_platform = [
        r["migration"] for r in _release_requirements() if r["scope"] == "platform"
    ]
    _check("B1: every platform requirement was checked too",
           sorted(c["migration"] for c in report.platform_checks)
           == sorted(declared_platform)
           and all(c["ledger_recorded"] is True for c in report.platform_checks),
           f"checked={sorted(c['migration'] for c in report.platform_checks)} "
           f"declared={sorted(declared_platform)}")
    _check("B1: the report records a fleet fingerprint",
           bool(report.fleet_fingerprint))


def test_B9_new_client_baseline_satisfies_the_gate() -> None:
    print("\n## test_B9_new_client_baseline_satisfies_the_gate")
    # An onboarded client gets the DDL applied AND the filename recorded, which
    # is exactly what the gate demands. Proven against the onboarding source so
    # the two rollout paths cannot drift apart.
    onboarding = (ROOT / "scripts/onboard_workflow_a_client.py").read_text()
    _check("B9: the new-client baseline applies the client migration",
           f'"{CLIENT_MIGRATION}"' in onboarding)
    _check("B9: and records it in the ledger the gate reads",
           onboarding.count(f'"{CLIENT_MIGRATION}"') >= 2)
    build_fleet()
    report = _run(ROOT)
    _check("B9: a baseline-shaped client passes the gate",
           report.affected_client_count == len(CLIENTS))


# ---------------------------------------------------------------------------
# B2 / B3 — the platform half
# ---------------------------------------------------------------------------

def test_B2_platform_ledger_missing_fails() -> None:
    print("\n## test_B2_platform_ledger_missing_fails")
    build_fleet()
    conn = _connect(_platform_db_name())
    conn.execute("DELETE FROM public.schema_migrations WHERE filename=%s",
                 (PLATFORM_MIGRATION,))
    conn.commit()
    conn.close()
    _expect_refusal("B2: platform migration not recorded",
                    "RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING",
                    lambda: _run(ROOT))


def test_B3_platform_ledger_lies_fails() -> None:
    print("\n## test_B3_platform_ledger_lies_fails")
    build_fleet()
    conn = _connect(_platform_db_name())
    # The ledger still claims 061; the relation is gone. This is precisely the
    # state a partially applied or restored database is in, and it is the case
    # a ledger-only gate would wave through.
    conn.execute("DROP TABLE workflow_a_control.provider_request_log")
    conn.commit()
    conn.close()
    _expect_refusal("B3: ledger says applied, object missing",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_B3b_platform_object_present_but_wrong_shape_fails() -> None:
    print("\n## test_B3b_platform_object_present_but_wrong_shape_fails")
    build_fleet()
    conn = _connect(_platform_db_name())
    # The table exists and the ledger agrees, but a column the release needs is
    # gone. A relation-existence check alone would pass this.
    conn.execute(
        "ALTER TABLE workflow_a_control.provider_request_log DROP COLUMN status"
    )
    conn.commit()
    conn.close()
    _expect_refusal("B3b: required column absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_B3c_platform_constraint_missing_fails() -> None:
    print("\n## test_B3c_platform_constraint_missing_fails")
    build_fleet()
    conn = _connect(_platform_db_name())
    # Dropping the constraint that keeps a PENDING request fact from claiming
    # completeness. The column set is intact, so only a constraint-aware gate
    # catches it — and this is the constraint blocker 1 depends on.
    conn.execute(
        "ALTER TABLE workflow_a_control.provider_request_log "
        "DROP CONSTRAINT ck_provider_request_log_pending_claims_nothing"
    )
    conn.commit()
    conn.close()
    _expect_refusal("B3c: required constraint absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


# ---------------------------------------------------------------------------
# B4 / B5 / B6 — the fleet half, one client damaged at a time
# ---------------------------------------------------------------------------

def test_B4_one_client_missing_ledger_fails() -> None:
    print("\n## test_B4_one_client_missing_ledger_fails")
    build_fleet(client_migration_applied={"BBB00001": False})
    _expect_refusal("B4: one of three clients has not recorded 047",
                    "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING",
                    lambda: _run(ROOT))


def test_B5_one_client_ledger_lies_fails() -> None:
    print("\n## test_B5_one_client_ledger_lies_fails")
    # The ledger records 047; the column was never created. This is the exact
    # shape of a rollout that reported success and did not finish, and it is
    # what makes a physical check non-negotiable.
    build_fleet(physical_column={"CCC00001": False})
    _expect_refusal("B5: one client's ledger claims 047 with no column",
                    "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_B6_unreachable_client_fails() -> None:
    print("\n## test_B6_unreachable_client_fails")
    build_fleet()
    admin = _admin()
    admin.execute('DROP DATABASE IF EXISTS "m4pf_b" WITH (FORCE)')
    admin.close()
    _expect_refusal("B6: a required client database is unreachable",
                    "RELEASE_SCHEMA_CLIENT_UNREACHABLE",
                    lambda: _run(ROOT))
    build_fleet()


# ---------------------------------------------------------------------------
# B7 — a check that examined nothing must never read as a pass
# ---------------------------------------------------------------------------

def test_E6_E7_zero_enabled_and_disabled_clients() -> None:
    print("\n## test_E6_E7_zero_enabled_and_disabled_clients")
    # E7: a DISABLED client is out of scope by the contract's own words — the
    # requirement is about the *enabled* fleet. Its database is deliberately
    # destroyed, so if the gate wrongly included it the run would fail.
    build_fleet(enabled={NO_SCHEDULE_CODE: False},
                drop_database={NO_SCHEDULE_CODE: True})
    report = _run(ROOT)
    checked = {c["client_code"] for c in report.client_checks}
    _check("E7: a disabled client is excluded, exactly per the contract",
           checked == set(ALL_CODES) - {NO_SCHEDULE_CODE},
           f"checked={sorted(checked)}")
    _check("E7: and the enabled count agrees with what was checked",
           report.affected_client_count == len(ALL_CODES) - 1)

    # E6: zero enabled clients. The client-business requirement is vacuously
    # satisfied — there is no client database that could be unmigrated — but the
    # report must say so in its own words rather than reading as "verified".
    # With the corrected enumeration this is the ONLY way the set can be empty,
    # so it can no longer be produced by partial enumeration.
    conn = _connect(_platform_db_name())
    conn.execute("UPDATE workflow_a_control.client_account SET enabled=false")
    conn.commit()
    conn.close()
    report = _run(ROOT)
    _check("E6: a genuinely empty fleet is recorded as such, not as verified",
           report.note == "no_enabled_client_accounts" and not report.client_checks,
           f"note={report.note}")
    build_fleet()


def test_E2_E3_enabled_client_without_a_schedule_is_still_required() -> None:
    print("\n## test_E2_E3_enabled_client_without_a_schedule_is_still_required")
    # THE EXACT DEFECT INDEPENDENT REVIEW REPRODUCED. Four enabled accounts,
    # three with schedules and healthy; the fourth owns no schedule and is not
    # migrated. The old rule filtered it out and passed.
    build_fleet(client_migration_applied={NO_SCHEDULE_CODE: False})
    _expect_refusal("E2: enabled client with no schedule lacks 047",
                    "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING",
                    lambda: _run(ROOT))

    # E3: same client, database gone entirely.
    build_fleet(drop_database={NO_SCHEDULE_CODE: True})
    _expect_refusal("E3: enabled client with no schedule is unreachable",
                    "RELEASE_SCHEMA_CLIENT_UNREACHABLE",
                    lambda: _run(ROOT))
    build_fleet()


def test_E4_E5_physical_column_is_verified_not_assumed() -> None:
    print("\n## test_E4_E5_physical_column_is_verified_not_assumed")
    # E4: ledger says applied, column absent — a rollout that reported success
    # and did not finish.
    build_fleet(physical_column={"CCC00001": False})
    _expect_refusal("E4: ledger claims 047, column absent",
                    "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING",
                    lambda: _run(ROOT))

    # E5: the column exists with the wrong type. A name-only check passes this;
    # the release stores uuids into it and would fail at runtime.
    build_fleet(wrong_column_type={"BBB00001": True})
    _expect_refusal("E5: column present with the wrong type",
                    "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING",
                    lambda: _run(ROOT))
    build_fleet()


def test_partial_enumeration_is_structurally_impossible() -> None:
    print("\n## test_partial_enumeration_is_structurally_impossible")
    import inspect
    from ops.release_schema_preflight import FLEET_RELATION
    # Only the executable body: the docstring deliberately explains the removed
    # filter, and matching prose would make this a test about wording.
    source = inspect.getsource(enumerate_affected_clients)
    body = source.split('"""')[-1]
    _check("the enumeration no longer filters on a trips_sync schedule",
           "trips_sync" not in body and "client_dataset_schedule" not in body,
           "an eligibility filter here is exactly the reproduced defect")
    _check("it selects from the authoritative fleet relation only",
           FLEET_RELATION == "workflow_a_control.client_account"
           and "FLEET_RELATION" in body)
    # And the runtime guard that would catch a reintroduced filter.
    build_fleet()
    report = _run(ROOT)
    _check("enumerated count equals the enabled account count",
           report.affected_client_count == report.enabled_account_count
           == len(ALL_CODES),
           f"affected={report.affected_client_count} "
           f"enabled={report.enabled_account_count}")


def test_B7b_the_gate_accepts_no_filter() -> None:
    print("\n## test_B7b_the_gate_accepts_no_filter")
    import inspect
    signature = inspect.signature(verify_schema_prerequisites)
    params = set(signature.parameters)
    _check("B7b: the gate exposes no client filter parameter",
           not (params & {"client_id", "client_name", "only_client_id",
                          "only_client_name", "clients", "limit"}),
           f"params={sorted(params)}")
    enum_src = inspect.getsource(enumerate_affected_clients)
    _check("B7b: enumeration selects every enabled account, unfiltered",
           "enabled = true" in enum_src and "LIMIT" not in enum_src.upper())


# ---------------------------------------------------------------------------
# B10 — the check-to-use window
# ---------------------------------------------------------------------------

def test_R3_mutation_before_the_fence_is_caught() -> None:
    print("\n## test_R3_mutation_before_the_fence_is_caught")
    build_fleet()
    report = _run(ROOT)
    baseline = report.fleet_fingerprint

    # An unchanged fleet passes the fence and yields, holding the lock.
    with activation_fence(expected_fingerprint=baseline,
                          platform_conn_factory=_platform_factory,
                          client_conn_factory=_client_factory) as observed:
        _check("R3: an unchanged fleet enters the fence", observed == baseline)

    # A fifth client is enabled AFTER validation but BEFORE the fence. This is
    # the ordering the fence cannot prevent — and must therefore detect.
    conn = _connect(_platform_db_name())
    late_id = "fbfe405c-a65f-4275-898f-deb81ceb4df2"
    conn.execute(
        """INSERT INTO workflow_a_control.client_account
          (client_id,client_code,client_name,provider_type,provider_base_url,
           provider_basic_auth_username,provider_basic_auth_password_secret_ref,
           client_db_host,client_db_port,client_db_name,client_db_user,
           client_db_password_secret_ref,client_db_schema,
           speed_trigger_filter_text,enabled,trips_pagination_mode)
          VALUES (%s,'EEE00001','EEE00001','telematics','https://example.invalid',
                  'u','REF',%s,%s,'m4pf_absent','u','REF','public','speeding',
                  true,%s)""",
        (late_id, _host(), _port(), MODE),
    )
    conn.commit()
    conn.close()

    def _enter():
        with activation_fence(expected_fingerprint=baseline,
                              platform_conn_factory=_platform_factory,
                              client_conn_factory=_client_factory):
            pass
    _expect_refusal("R3: a fleet mutation before the fence refuses activation",
                    "RELEASE_SCHEMA_FLEET_CHANGED_DURING_ACTIVATION", _enter)

    # And re-running the full gate refuses on its own terms: the new client
    # genuinely cannot satisfy the prerequisite.
    _expect_refusal("R3: re-running the gate refuses the new client",
                    "RELEASE_SCHEMA_CLIENT_UNREACHABLE",
                    lambda: _run(ROOT))
    build_fleet()


def test_R2_raw_sql_cannot_commit_while_the_fence_is_held() -> None:
    print("\n## test_R2_raw_sql_cannot_commit_while_the_fence_is_held")
    # THE MANDATORY CASE. A completely independent connection that knows nothing
    # about this module — no advisory lock, no cooperation — attempts the fleet
    # mutation while the fence is held. It must block on the database, not on a
    # convention. This is what the previous advisory-lock implementation could
    # not do, and the reason it was rejected.
    import threading
    import time as _time

    build_fleet()
    report = _run(ROOT)
    baseline = report.fleet_fingerprint

    events = []
    committed = threading.Event()
    started = threading.Event()

    def _raw_sql_writer():
        raw = _connect(_platform_db_name())          # plain psycopg, nothing else
        try:
            with raw.cursor() as cur:
                started.set()
                cur.execute(
                    "UPDATE workflow_a_control.client_account "
                    "SET enabled = false WHERE client_code = %s",
                    ("AAA00001",),
                )
            raw.commit()
            events.append(("raw_sql_committed", _time.monotonic()))
            committed.set()
        except Exception as exc:                      # pragma: no cover
            events.append((f"raw_sql_failed:{type(exc).__name__}", _time.monotonic()))
            committed.set()
        finally:
            raw.close()

    writer = threading.Thread(target=_raw_sql_writer, daemon=True)

    with activation_fence(expected_fingerprint=baseline,
                          platform_conn_factory=_platform_factory,
                          client_conn_factory=_client_factory):
        writer.start()
        started.wait(timeout=5)
        # Give the raw writer a generous chance to commit. It must not.
        blocked = not committed.wait(timeout=3.0)
        _check("R2: raw SQL is BLOCKED from committing inside the fence", blocked,
               f"events={events}")
        # The pointer swap happens at exactly this point in the real sequence.
        events.append(("pointer_swapped", _time.monotonic()))

    # Fence released: the writer may now proceed (R4).
    completed = committed.wait(timeout=10)
    _check("R4: after the fence releases, the write completes", completed,
           f"events={events}")
    order = [name for name, _ts in events]
    _check("R2: the swap strictly precedes the raw-SQL commit",
           order.index("pointer_swapped") < order.index("raw_sql_committed"),
           f"order={order}")
    writer.join(timeout=5)
    build_fleet()


def test_R1_cooperative_enablement_is_blocked_too() -> None:
    print("\n## test_R1_cooperative_enablement_is_blocked_too")
    # The supported enablement path writes the same table, so it is fenced by
    # the same lock — no separate protocol, and nothing to opt into.
    import threading
    import time as _time

    build_fleet()
    report = _run(ROOT)
    baseline = report.fleet_fingerprint

    events = []
    done = threading.Event()
    started = threading.Event()

    def _cooperative_writer():
        conn = _connect(_platform_db_name())
        try:
            with conn.cursor() as cur:
                started.set()
                # An onboarding-shaped INSERT: a brand-new enabled client.
                cur.execute(
                    """INSERT INTO workflow_a_control.client_account
                      (client_id,client_code,client_name,provider_type,
                       provider_base_url,provider_basic_auth_username,
                       provider_basic_auth_password_secret_ref,client_db_host,
                       client_db_port,client_db_name,client_db_user,
                       client_db_password_secret_ref,client_db_schema,
                       speed_trigger_filter_text,enabled,trips_pagination_mode)
                      VALUES (%s,'FFF00001','FFF00001','telematics',
                              'https://example.invalid','u','REF',%s,%s,
                              'm4pf_f','u','REF','public','speeding',true,%s)""",
                    (str(uuid.uuid4()), _host(), _port(), MODE),
                )
            conn.commit()
            events.append(("enablement_committed", _time.monotonic()))
        except Exception as exc:                      # pragma: no cover
            events.append((f"enablement_failed:{type(exc).__name__}", _time.monotonic()))
        finally:
            conn.close()
            done.set()

    writer = threading.Thread(target=_cooperative_writer, daemon=True)
    with activation_fence(expected_fingerprint=baseline,
                          platform_conn_factory=_platform_factory,
                          client_conn_factory=_client_factory):
        writer.start()
        started.wait(timeout=5)
        blocked = not done.wait(timeout=3.0)
        _check("R1: a supported enablement is blocked inside the fence", blocked,
               f"events={events}")
        events.append(("pointer_swapped", _time.monotonic()))
    _check("R1: it completes once the fence releases", done.wait(timeout=10))
    order = [name for name, _ts in events]
    _check("R1: the swap strictly precedes the enablement commit",
           order.index("pointer_swapped") < order.index("enablement_committed"),
           f"order={order}")
    writer.join(timeout=5)
    build_fleet()


def test_the_fence_blocks_writes_but_not_reads() -> None:
    print("\n## test_the_fence_blocks_writes_but_not_reads")
    # SHARE is the minimum mode that serializes writes. Unrelated reads of the
    # control plane must stay unaffected, or an activation would stall the
    # dispatcher for its duration.
    build_fleet()
    baseline = _run(ROOT).fleet_fingerprint
    with activation_fence(expected_fingerprint=baseline,
                          platform_conn_factory=_platform_factory,
                          client_conn_factory=_client_factory):
        reader = _connect(_platform_db_name())
        try:
            count = reader.execute(
                "SELECT count(*) FROM workflow_a_control.client_account"
            ).fetchone()[0]
            reader.rollback()
            _check("an ordinary read is not blocked by the fence",
                   count == len(ALL_CODES), f"count={count}")
        finally:
            reader.close()


def test_the_fleet_fence_is_a_table_lock_not_an_advisory_lock() -> None:
    """The FLEET is fenced by a real table lock. That has not changed.

    The fence now ALSO takes `SCHEMA_TRANSITION_LOCK_KEY`, and that one is an
    advisory lock on purpose: it serialises release activation against the
    client-schema CONTRACT closure, whose only two participants are repository
    tools that both take it. For fleet membership an advisory lock would still
    be wrong — any session can write `client_account` and none would volunteer —
    so the assertion below is scoped to how the FLEET is held, not to whether
    the module mentions an advisory lock anywhere.
    """
    print("\n## test_the_fleet_fence_is_a_table_lock_not_an_advisory_lock")
    source = (ROOT / "ops/release_schema_preflight.py").read_text()
    fence = source[source.index("def activation_fence("):]
    fence = fence[:fence.index("\n    finally:")]
    # Executable statements only: the docstring explains both primitives and
    # would otherwise satisfy either assertion by describing it.
    code = fence.split('"""')[2]
    _check("the fence takes a real table lock on the fleet relation",
           "LOCK TABLE {FLEET_RELATION} IN SHARE MODE" in code, code[:400])
    _check("the FLEET is never held by a session-level advisory lock",
           "pg_advisory_lock" not in code and "pg_advisory_unlock" not in code,
           "an advisory lock constrains only code that volunteers to take it")
    _check("while the schema-transition boundary IS held by the shared "
           "advisory key, taken before the table lock",
           "SCHEMA_TRANSITION_LOCK_KEY" in code
           and code.index("pg_advisory_xact_lock") < code.index("LOCK TABLE"))
    _check("and the authoritative capability re-read happens before the yield",
           code.index("check_schema_state_guards") < code.index("yield observed"))
    boundary = (ROOT / "ops/release_boundary.py").read_text()
    swap = boundary.index("_swap_pointer(layout.current")
    fence = boundary.index("with fence:")
    _check("the pointer swap happens inside the fence block", fence < swap)
    _check("no fingerprint re-check survives outside the fence",
           "recheck_fleet_fingerprint" not in boundary)


# ---------------------------------------------------------------------------
# B11 — the gate is read-only
# ---------------------------------------------------------------------------

#: Every statement the gate actually sent to PostgreSQL during the last
#: read-only run, captured at the transport boundary rather than read out of
#: the source. Populated by `_RecordingCursor`.
_EXECUTED: List[str] = []


def _recording_cursor_class():
    """A cursor that records the SQL it is asked to execute, then executes it.

    THE POINT OF INTERCEPTING HERE. The previous evidence for "the gate is
    read-only" was an AST scan of string literals in
    `ops/release_schema_preflight.py`. That is weaker than the invariant it
    stands for in one specific way independent review named: a statement
    assembled at runtime — `"UP" + "DATE ..."`, an f-string, a name pulled from
    a table — is mutating SQL that no literal scan can see. A cursor sees the
    finished string, whatever produced it, and sees nothing else: comments and
    docstrings that merely MENTION `UPDATE` never reach it, which is the
    false-positive class that made the older whole-file scan unusable.
    """
    import psycopg

    class _RecordingCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):  # type: ignore[override]
            text = query if isinstance(query, str) else bytes(query).decode(
                "utf-8", "replace")
            _EXECUTED.append(text)
            return super().execute(query, params, **kwargs)

    return _RecordingCursor


def _readonly_connect(dbname: str):
    """A connection PostgreSQL itself refuses to let write.

    `default_transaction_read_only` is set as a libpq startup option, so it
    applies from the first statement of the first transaction and is not
    something the gate can be inside a transaction to avoid. Any INSERT,
    UPDATE, DELETE or DDL raises `read_only_sql_transaction` (SQLSTATE 25006)
    in the server, before it can have an effect — so the DATABASE, not a
    heuristic over Python source, is what proves the invariant.
    """
    import psycopg

    dsn = os.environ[ENV]
    base = dsn.rsplit("/", 1)[0]
    conn = psycopg.connect(
        f"{base}/{dbname}",
        options="-c default_transaction_read_only=on",
        cursor_factory=_recording_cursor_class(),
    )
    return conn


def _run_readonly(release_tree: Path):
    """The real gate, against connections the server will not let write."""
    if Path(release_tree) == ROOT:
        release_tree = _repository_release_tree()
    return verify_schema_prerequisites(
        release_tree=release_tree, release_id="test-release-readonly",
        platform_conn_factory=lambda: _readonly_connect(_platform_db_name()),
        client_conn_factory=lambda client: _readonly_connect(client.db_name),
    )


#: The only statement kinds a read-only gate may issue. Checked against the
#: FIRST keyword of every statement the cursor actually sent.
_READ_ONLY_LEADING_KEYWORDS = frozenset({
    "SELECT", "WITH", "SET", "SHOW", "RESET", "BEGIN", "COMMIT", "ROLLBACK",
    "DECLARE", "FETCH", "CLOSE",
})


def _leading_keyword(statement: str) -> str:
    """The first SQL keyword, with leading comments and whitespace removed."""
    text = statement.strip()
    while True:
        if text.startswith("--"):
            _newline, _sep, text = text.partition("\n")
            text = text.lstrip()
            continue
        if text.startswith("/*"):
            _body, _sep, text = text.partition("*/")
            text = text.lstrip()
            continue
        break
    return text.split(None, 1)[0].upper().strip("(;") if text else ""


def test_B11_the_gate_mutates_nothing() -> None:
    """The gate is read-only, proved by the DATABASE and by what it executed.

    Three independent pieces of evidence, because each answers a question the
    others do not:

      1. STATE. A digest of every row of every table the gate touches, before
         and after two full runs. A digest rather than a row count: an UPDATE
         that rewrites a value changes nothing countable.
      2. DB ENFORCEMENT. The same run performed against sessions started with
         `default_transaction_read_only=on`. It passes, so nothing the gate
         does needs write permission — and any statement that did would have
         been refused by PostgreSQL rather than merely noticed afterwards.
      3. TRANSPORT BOUNDARY. Every statement the gate actually sent, captured
         by the cursor, checked by leading keyword. This covers SQL the gate
         assembles at runtime, which no source scan can see, and it cannot be
         tripped by prose: a docstring is not a statement.

    Plus a recurrence detector for (2) and (3) together: a preflight step is
    temporarily replaced by one that assembles `"UP" + "DATE ..."` at runtime,
    and both the server and the boundary check must catch it. Without that,
    "the gate executed no mutation" would be indistinguishable from "the
    detector cannot see one".
    """
    print("\n## test_B11_the_gate_mutates_nothing")
    build_fleet()

    def snapshot() -> Dict[str, object]:
        """Row-level digests, not counts: a rewritten value must be visible."""
        out: Dict[str, object] = {}
        conn = _connect(_platform_db_name())
        for table in ("client_account", "client_dataset_schedule",
                      "provider_request_log"):
            out[table] = conn.execute(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, '|' "
                f"ORDER BY t::text)), '') FROM workflow_a_control.{table} AS t"
            ).fetchone()
        out["ledger"] = sorted(
            r[0] for r in conn.execute(
                "SELECT filename FROM public.schema_migrations"
            ).fetchall()
        )
        conn.rollback()
        conn.close()
        for _cid, _code, dbname, _sched in CLIENTS:
            client = _connect(dbname)
            out[f"{dbname}:trips"] = client.execute(
                "SELECT count(*), coalesce(md5(string_agg(t::text, '|' "
                "ORDER BY t::text)), '') FROM public.client_trips AS t"
            ).fetchone()
            out[f"{dbname}:eco"] = client.execute(
                "SELECT count(*), coalesce(md5(string_agg(t::text, '|' "
                "ORDER BY t::text)), '') "
                "FROM public.eco_dashboard_delivery_operation AS t"
            ).fetchone()
            out[f"{dbname}:ledger"] = sorted(
                r[0] for r in client.execute(
                    "SELECT filename FROM public.schema_migrations"
                ).fetchall()
            )
            client.rollback()
            client.close()
        return out

    before = snapshot()
    _run(ROOT)
    _run(ROOT)
    after = snapshot()
    _check("B11: running the gate twice changes no row anywhere",
           before == after, f"before={before} after={after}")

    # --- 2. the database itself refuses to let the gate write ---------------
    _EXECUTED.clear()
    report = None
    try:
        report = _run_readonly(ROOT)
    except Exception as exc:  # noqa: BLE001 - the failure IS the result
        _check("B11: the gate completes against a READ ONLY session", False,
               f"{type(exc).__name__}: {exc}")
    if report is not None:
        _check("B11: the gate completes against a READ ONLY session",
               report.requirements_checked > 0
               and all(not c["physical_defects"] for c in report.client_checks),
               str(report.as_dict())[:300])
    _check("B11: and it really did execute statements there",
           len(_EXECUTED) > 10, f"{len(_EXECUTED)} statements")

    # --- 3. every statement it actually sent, by leading keyword ------------
    offenders = sorted({
        _leading_keyword(statement) for statement in _EXECUTED
        if _leading_keyword(statement) not in _READ_ONLY_LEADING_KEYWORDS
    })
    _check("B11: every statement the gate executed is read-only",
           not offenders, f"leading keywords: {offenders}")

    # Prose is not a statement: the module documents `ALTER TABLE ... DISABLE
    # TRIGGER` and `UPDATE` at length, and none of it reaches a cursor.
    module_source = (ROOT / "ops/release_schema_preflight.py").read_text()
    _check("B11: the module's prose still discusses mutating SQL, and that is "
           "not a finding",
           "DISABLE TRIGGER" in module_source and not offenders)

    # --- the recurrence detector -------------------------------------------
    # A dynamically assembled mutation, of exactly the shape a literal scan
    # cannot see. It must be caught, or none of the evidence above means
    # anything.
    import psycopg

    import ops.release_schema_preflight as preflight

    original = preflight.ledger_has_migration
    caught: Dict[str, object] = {}

    def _mutating_ledger_has_migration(cur, filename: str) -> bool:
        verb = "UP" + "DATE"
        target = ".".join(["workflow_a_control", "client_account"])
        cur.execute(f"{verb} {target} SET client_name = client_name || 'x'")
        return original(cur, filename)

    _EXECUTED.clear()
    preflight.ledger_has_migration = _mutating_ledger_has_migration
    try:
        _run_readonly(ROOT)
        caught["error"] = None
    except psycopg.errors.ReadOnlySqlTransaction as exc:
        caught["error"] = f"{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        caught["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        preflight.ledger_has_migration = original

    _check("B11 RECURRENCE: a dynamically assembled UPDATE is refused BY "
           "POSTGRESQL, not by a source scan",
           caught.get("error") == "ReadOnlySqlTransaction",
           f"outcome: {caught.get('error')!r}")
    replayed = sorted({
        _leading_keyword(statement) for statement in _EXECUTED
        if _leading_keyword(statement) not in _READ_ONLY_LEADING_KEYWORDS
    })
    _check("B11 RECURRENCE: and the transport-boundary check sees it too",
           replayed == ["UPDATE"], f"leading keywords: {replayed}")

    # The mutation was refused, so the fleet is byte-identical to before it.
    _check("B11 RECURRENCE: the refused mutation left no trace",
           snapshot() == before)

    # And the same detector fires for every other mutating shape, including DDL
    # that no `UPDATE`-shaped check would notice.
    for label, statement in (
        ("INSERT", "INS" + "ERT INTO workflow_a_control.client_account "
                   "(client_id) VALUES (gen_random_uuid())"),
        ("DELETE", "DEL" + "ETE FROM workflow_a_control.client_account"),
        ("DDL", "CRE" + "ATE TABLE public.b11_should_never_exist (x int)"),
    ):
        def _mutate(cur, filename: str, _sql: str = statement) -> bool:
            cur.execute(_sql)
            return original(cur, filename)

        preflight.ledger_has_migration = _mutate
        try:
            _run_readonly(ROOT)
            outcome = None
        except psycopg.errors.ReadOnlySqlTransaction:
            outcome = "ReadOnlySqlTransaction"
        except Exception as exc:  # noqa: BLE001
            outcome = f"{type(exc).__name__}: {exc}"
        finally:
            preflight.ledger_has_migration = original
        _check(f"B11 RECURRENCE: a dynamically assembled {label} is refused by "
               "PostgreSQL",
               outcome == "ReadOnlySqlTransaction", f"outcome: {outcome!r}")

    _check("B11: the fleet is unchanged after every recurrence probe",
           snapshot() == before)


# ---------------------------------------------------------------------------
# Release-tree pinning
# ---------------------------------------------------------------------------

def test_requirements_are_read_from_the_release_tree(tmp_root: Path) -> None:
    print("\n## test_requirements_are_read_from_the_release_tree")
    build_fleet()

    # A release predating the mechanism declares nothing and passes, so rollback
    # to any historical release stays possible.
    old_release = tmp_root / "old"
    old_release.mkdir(parents=True, exist_ok=True)
    report = _run(old_release)
    _check("an older release is not blocked by a requirement it never had",
           report.note == "release_predates_schema_requirements"
           and report.requirements_checked == 0)

    # A release that declares the requirement is held to it even when the
    # working tree would have said something else.
    new_release = tmp_root / "new" / "db"
    new_release.mkdir(parents=True, exist_ok=True)
    (new_release / "schema_requirements.json").write_text(
        json.dumps(_repository_declaration(), indent=2), encoding="utf-8"
    )
    conn = _connect(_platform_db_name())
    conn.execute("DELETE FROM public.schema_migrations WHERE filename=%s",
                 (PLATFORM_MIGRATION,))
    conn.commit()
    conn.close()
    _expect_refusal("the release's own requirements are enforced",
                    "RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING",
                    lambda: _run(tmp_root / "new"))
    build_fleet()



# ---------------------------------------------------------------------------
# M5 / 062 — the gate must verify physical DEFINITIONS, not names
#
# Independent review found the name-only check would accept a database whose
# 062 ledger row exists and whose constraint names exist, but whose definitions,
# columns, predicates or indexes are materially wrong. Each case below installs
# exactly that shape: right name, wrong physical contract.
# ---------------------------------------------------------------------------

def _damage_platform(sql: str) -> None:
    build_fleet()
    conn = _connect(_platform_db_name())
    for statement in sql.strip().split(";\n"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()
    conn.close()


def test_M5_correct_shape_passes() -> None:
    print("\n## test_M5_correct_shape_passes")
    build_fleet()
    report = _run(ROOT)
    checked = {c["migration"] for c in report.platform_checks}
    _check("M5: 062 is among the verified platform requirements",
           "062_workflow_a_multi_cadence_schedule_identity.sql" in checked,
           f"checked={sorted(checked)}")
    _check("M5: a correct 062 shape passes", True)


def test_M5_ledger_missing_fails() -> None:
    print("\n## test_M5_ledger_missing_fails")
    _damage_platform(
        "DELETE FROM public.schema_migrations "
        "WHERE filename = '062_workflow_a_multi_cadence_schedule_identity.sql'"
    )
    _expect_refusal("M5: 062 ledger row absent",
                    "RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING",
                    lambda: _run(ROOT))


def test_M5_run_type_missing_fails() -> None:
    print("\n## test_M5_run_type_missing_fails")
    _damage_platform(
        "ALTER TABLE workflow_a_control.client_dataset_schedule "
        "DROP COLUMN run_type CASCADE"
    )
    _expect_refusal("M5: run_type absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_same_name_wrong_constraint_columns_fails() -> None:
    """The old schedule uniqueness, reinstalled under the new name."""
    print("\n## test_M5_same_name_wrong_constraint_columns_fails")
    _damage_platform(
        "ALTER TABLE workflow_a_control.client_dataset_schedule "
        "DROP CONSTRAINT uq_client_dataset_schedule;\n"
        "ALTER TABLE workflow_a_control.client_dataset_schedule "
        "ADD CONSTRAINT uq_client_dataset_schedule UNIQUE (client_id, dataset_name)"
    )
    _expect_refusal("M5: schedule uniqueness over the pre-M5 columns",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_same_name_wrong_check_expression_fails() -> None:
    print("\n## test_M5_same_name_wrong_check_expression_fails")
    _damage_platform(
        "ALTER TABLE workflow_a_control.client_dataset_schedule "
        "DROP CONSTRAINT ck_client_dataset_schedule_run_type;\n"
        "ALTER TABLE workflow_a_control.client_dataset_schedule "
        "ADD CONSTRAINT ck_client_dataset_schedule_run_type "
        "CHECK (run_type IN ('DAILY', 'WEEKLY_RECONCILIATION', "
        "'MONTHLY_RECONCILIATION', 'ANNUAL_RECONCILIATION'))"
    )
    _expect_refusal("M5: run_type vocabulary widened under the same name",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_coverage_owner_uniqueness_missing_fails() -> None:
    print("\n## test_M5_coverage_owner_uniqueness_missing_fails")
    _damage_platform(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "DROP CONSTRAINT uq_client_dataset_coverage_dataset"
    )
    _expect_refusal("M5: shared-coverage uniqueness absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_owner_provenance_coherence_fk_wrong_fails() -> None:
    """The single-column FK from 057, reinstalled under the corrected name."""
    print("\n## test_M5_owner_provenance_coherence_fk_wrong_fails")
    _damage_platform(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "DROP CONSTRAINT fk_client_dataset_coverage_schedule;\n"
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "ADD CONSTRAINT fk_client_dataset_coverage_schedule "
        "FOREIGN KEY (schedule_id) REFERENCES "
        "workflow_a_control.client_dataset_schedule (schedule_id) ON DELETE CASCADE"
    )
    _expect_refusal("M5: coverage provenance FK reverted to the pre-M5 shape",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_base_index_wrong_predicate_fails() -> None:
    print("\n## test_M5_base_index_wrong_predicate_fails")
    _damage_platform(
        "DROP INDEX workflow_a_control.uq_client_dataset_schedule_base;\n"
        "CREATE UNIQUE INDEX uq_client_dataset_schedule_base "
        "ON workflow_a_control.client_dataset_schedule (client_id, dataset_name) "
        "WHERE run_type = 'WEEKLY_RECONCILIATION'"
    )
    _expect_refusal("M5: base-role index carries the wrong predicate",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_recovery_active_index_wrong_predicate_fails() -> None:
    print("\n## test_M5_recovery_active_index_wrong_predicate_fails")
    _damage_platform(
        "DROP INDEX workflow_a_control.uq_client_dataset_recovery_run_active;\n"
        "CREATE UNIQUE INDEX uq_client_dataset_recovery_run_active "
        "ON workflow_a_control.client_dataset_recovery_run (client_id, dataset_name) "
        "WHERE status = 'RUNNING'"
    )
    _expect_refusal("M5: recovery exclusivity narrowed to RUNNING only",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_recovery_approval_index_wrong_keys_fails() -> None:
    print("\n## test_M5_recovery_approval_index_wrong_keys_fails")
    _damage_platform(
        "DROP INDEX workflow_a_control.uq_client_dataset_recovery_run_approved_window;\n"
        "CREATE UNIQUE INDEX uq_client_dataset_recovery_run_approved_window "
        "ON workflow_a_control.client_dataset_recovery_run "
        "(client_id, schedule_id, window_start_ts, window_end_ts, approval_ref)"
    )
    _expect_refusal("M5: approval idempotency reverted to the schedule key",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_M5_unvalidated_constraint_fails() -> None:
    """A constraint that exists but is NOT VALID enforces nothing."""
    print("\n## test_M5_unvalidated_constraint_fails")
    _damage_platform(
        "ALTER TABLE workflow_a_control.client_dataset_schedule "
        "DROP CONSTRAINT ck_client_dataset_schedule_run_type_cadence;\n"
        "ALTER TABLE workflow_a_control.client_dataset_schedule "
        "ADD CONSTRAINT ck_client_dataset_schedule_run_type_cadence "
        "CHECK (((run_type = 'DAILY'::text) OR ((run_type = "
        "'WEEKLY_RECONCILIATION'::text) AND (frequency = 'weekly'::text)) OR "
        "((run_type = 'MONTHLY_RECONCILIATION'::text) AND (frequency = "
        "'monthly'::text)))) NOT VALID"
    )
    _expect_refusal("M5: a NOT VALID constraint is not an effective one",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


# ---------------------------------------------------------------------------
# S15 — THE GENERATED-REPORT REQUIREMENT IS REALLY EVALUATED, BOTH HALVES
#
# 068 and 069 are the first PLATFORM requirements this suite's fixture has to
# install outside `workflow_a_control`, and they are declared as two separate
# requirements on purpose: 069 is the corrective migration, so a database
# carrying 068 alone is NOT a database this release may be activated over.
#
# The correct-fleet case above already proves the passing half generically —
# `test_B1_B8` asserts that the checked platform set equals the DECLARED
# platform set with `ledger_recorded` true for every entry, so 068 and 069
# cannot silently drop out of it. What is asserted here is the other half: each
# way an S15 database can be wrong is refused, and refused with the code that
# says which layer caught it.
# ---------------------------------------------------------------------------

def _platform_sql(*statements: str) -> None:
    """Damage the platform database in exactly one way, then close."""
    conn = _connect(_platform_db_name())
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def test_S15_correct_shape_passes() -> None:
    print("\n## test_S15_correct_shape_passes")
    build_fleet()
    report = _run(ROOT)
    checked = {c["migration"] for c in report.platform_checks}
    _check("S15: both generated-report migrations were really checked",
           {PLATFORM_MIGRATION_S15, PLATFORM_MIGRATION_S15_INTEGRITY}
           <= checked,
           f"checked={sorted(checked)}")


def test_S15_068_ledger_missing_fails() -> None:
    print("\n## test_S15_068_ledger_missing_fails")
    build_fleet()
    _platform_sql(
        "DELETE FROM public.schema_migrations WHERE filename="
        f"'{PLATFORM_MIGRATION_S15}'"
    )
    _expect_refusal("S15: 068 not recorded",
                    "RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING",
                    lambda: _run(ROOT))


def test_S15_069_ledger_missing_fails() -> None:
    print("\n## test_S15_069_ledger_missing_fails")
    build_fleet()
    # The state production would be in after applying 068 and stopping. 068 is
    # recorded and its objects are real; the release is still refused, because
    # the corrective migration is a separate declared requirement.
    _platform_sql(
        "DELETE FROM public.schema_migrations WHERE filename="
        f"'{PLATFORM_MIGRATION_S15_INTEGRITY}'"
    )
    _expect_refusal("S15: 068 alone does not satisfy the release",
                    "RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING",
                    lambda: _run(ROOT))


def test_S15_ledger_lies_relation_missing_fails() -> None:
    print("\n## test_S15_ledger_lies_relation_missing_fails")
    build_fleet()
    # Both migrations stay recorded and a whole declared relation is gone —
    # a restored or partially applied database. A ledger-only gate passes this.
    _platform_sql(
        "DROP TABLE public.portal_generated_report_files CASCADE"
    )
    _expect_refusal("S15: ledger says applied, relation missing",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_S15_068_column_missing_fails() -> None:
    print("\n## test_S15_068_column_missing_fails")
    build_fleet()
    # The relation exists and the ledger agrees; one column the library query
    # orders by is gone. Only a shape-aware gate catches it.
    _platform_sql(
        "ALTER TABLE public.portal_generated_report_instances "
        "DROP COLUMN library_timestamp CASCADE"
    )
    _expect_refusal("S15: required 068 column absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_S15_069_column_missing_fails() -> None:
    print("\n## test_S15_069_column_missing_fails")
    build_fleet()
    # 069's terminal-claim column. Without it a replayed callback can publish
    # twice, which is the finding 069 exists to close.
    _platform_sql(
        "ALTER TABLE public.portal_generated_report_instances "
        "DROP COLUMN completed_claim_token CASCADE"
    )
    _expect_refusal("S15: required 069 column absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_S15_069_constraint_missing_fails() -> None:
    print("\n## test_S15_069_constraint_missing_fails")
    build_fleet()
    # Recorded, every column present, one 069 CHECK dropped: the malformed
    # case where the schema looks migrated and is not.
    _platform_sql(
        "ALTER TABLE public.portal_generated_report_files "
        "DROP CONSTRAINT portal_generated_report_files_previewable_content_check"
    )
    _expect_refusal("S15: required 069 constraint absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_S15_069_trigger_missing_fails() -> None:
    print("\n## test_S15_069_trigger_missing_fails")
    build_fleet()
    # The summary guard that makes the availability counters a projection of
    # the member rows. Dropping it leaves every column and CHECK intact.
    _platform_sql(
        "DROP TRIGGER portal_generated_report_instance_summary_guard "
        "ON public.portal_generated_report_instances"
    )
    _expect_refusal("S15: required 069 trigger absent",
                    "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                    lambda: _run(ROOT))


def test_S15_damage_does_not_survive_the_next_build() -> None:
    print("\n## test_S15_damage_does_not_survive_the_next_build")
    # The S15 relations live in `public`, which `build_fleet` does not drop
    # wholesale, and 068 creates them with IF NOT EXISTS. This asserts the
    # fixture's own reset: after the damage above, one rebuild is enough to
    # get a clean, fully migrated platform back — otherwise every case after a
    # negative one would be failing for an inherited reason.
    _platform_sql(
        "ALTER TABLE public.portal_generated_report_files "
        "DROP CONSTRAINT portal_generated_report_files_previewable_content_check"
    )
    build_fleet()
    report = _run(ROOT)
    _check("S15: a rebuilt fixture is fully migrated again",
           report.affected_client_count == len(CLIENTS))


# ---------------------------------------------------------------------------
# G6 NARROWNESS DOES NOT LEAK INTO PREFLIGHT
#
# The bridge-declaration proof in `inspect_release_bridge_compatibility` is
# exact about ONE relation and deliberately says nothing about the rest of a
# release's requirements. That is only safe while normal preflight still
# evaluates the WHOLE declared set and fails closed on anything missing — the
# alternative would be a release that G6 calls bridge-compatible and that then
# cannot actually be activated. Both halves are asserted here, against the real
# repository document rather than a narrowed fixture.
# ---------------------------------------------------------------------------

def test_the_full_requirement_set_is_evaluated_and_fails_closed(
    tmp_root: Path,
) -> None:
    print("\n## test_the_full_requirement_set_is_evaluated_and_fails_closed")
    build_fleet()

    # The fixture's narrowing is currently VACUOUS: every client_business
    # migration the repository declares is one `build_fleet` applies. So the
    # passing case above really did evaluate the actual full requirement set,
    # and this is stated rather than assumed — if a future migration is declared
    # and not applied by the fixture, this fails and says so.
    full = _repository_declaration(applied_only=False)
    narrowed = _repository_declaration(applied_only=True)
    _check("the fixture evaluates the REAL full requirement set, unnarrowed",
           full == narrowed,
           "declared but not applied by the fixture: "
           + str(sorted(
               {r["migration"] for r in full["requirements"]}
               - {r["migration"] for r in narrowed["requirements"]}
           )))

    #: The unrelated relation no client database has. Declared two ways, so
    #: BOTH client-scoped layers of the gate are shown to be fail-closed: once
    #: under a migration the fleet never recorded (the ledger layer) and once
    #: under a migration every client HAS recorded (the physical layer, which a
    #: ledger check alone would never reach).
    probe_relation = {
        "schema": "public",
        "table": "client_unrelated_probe",
        "columns": [{"name": "probe_id", "type": "bigint", "nullable": False}],
    }

    def _tree(name: str, document: Dict) -> Path:
        tree = tmp_root / name
        (tree / "db").mkdir(parents=True, exist_ok=True)
        (tree / "db" / "schema_requirements.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )
        return tree

    unrecorded = _repository_declaration(applied_only=False)
    unrecorded["requirements"].append({
        "migration": "998_synthetic_unrelated_requirement.sql",
        "scope": "client_business",
        "milestone": "UNRELATED",
        "reason": "synthetic probe: a requirement no client database satisfies",
        "relations": [json.loads(json.dumps(probe_relation))],
    })

    recorded = _repository_declaration(applied_only=False)
    for requirement in recorded["requirements"]:
        if requirement.get("migration") == CLIENT_MIGRATION_ECO_DASH:
            requirement["relations"].append(
                json.loads(json.dumps(probe_relation))
            )
            break
    else:  # pragma: no cover — the fixture would have to stop applying 050
        raise AssertionError(f"{CLIENT_MIGRATION_ECO_DASH} is not declared")

    for label, document, expected_code in (
        ("under a migration the fleet never recorded",
         unrecorded, "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING"),
        ("under a migration every client HAS recorded",
         recorded, "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING"),
    ):
        tree = _tree(f"full-set-{expected_code.lower()}", document)

        # G6 still calls it bridge-compatible — exactness is scoped to the
        # bridge relation, and a requirement elsewhere is none of its business.
        record = inspect_release_bridge_compatibility(tree)
        _check(f"G6 still reports the exact bridge declaration as compatible: "
               f"{label}",
               record["compatible"]
               and record["declares_exact_bridge_alternatives"],
               str(record["defects"]))

        # And preflight refuses the release anyway. This is the property that
        # makes G6's narrowness safe: nothing G6 ignores is thereby waved
        # through activation.
        _expect_refusal(
            f"full preflight still fails closed on the unrelated requirement "
            f"G6 ignores, {label}",
            expected_code, lambda tree=tree: _run(tree),
        )
    build_fleet()


# ---------------------------------------------------------------------------
# THE HARDEST ACCEPTANCE CHECK — G6 READY IMPLIES A REAL DATABASE ACCEPTS IT
#
# `inspect_release_bridge_compatibility` proves the complete participating
# relation against a MODELED canonical state, because it must answer before any
# DDL runs and with no client cursor in reach. That model is only worth
# anything if a REAL PostgreSQL relation in the same state agrees, so this test
# builds both canonical states as actual tables and runs the actual
# `relation_defects` — the function activation itself decides with — over the
# packaged relation of every fixture G6 called READY.
#
# The property is one-way and deliberate: G6 READY => zero relation defects in
# canonical EXPAND and in canonical validated CONTRACT. G6 may be stricter.
# ---------------------------------------------------------------------------

_BRIDGE_PROBE_DB = "telematics_m4_bridge_probe"


def _build_canonical_bridge_relation(cur, state: str, *, rich: bool) -> None:
    """The relation as the transition really leaves it, as real DDL.

    `rich` adds the columns and primary key a production `client_trips` also
    carries. It must not change any verdict: the canonical model is a LOWER
    BOUND and the matcher is monotone in what the catalog contains, so a
    requirement satisfied by the bare state is satisfied by the richer one.
    """
    cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
    extra = ", id bigserial PRIMARY KEY, trip_ref text" if rich else ""
    cur.execute(
        "CREATE TABLE public.client_trips ("
        "first_seen_request_id uuid, "
        f"first_seen_response_received_at_utc timestamptz{extra})"
    )
    if state == "EXPAND":
        cur.execute(
            f"ALTER TABLE public.client_trips ADD CONSTRAINT "
            f"{FIRST_SEEN_EXPAND_CONSTRAINT} {FIRST_SEEN_EXPAND_DEFINITION} "
            f"NOT VALID"
        )
    else:
        cur.execute(
            f"ALTER TABLE public.client_trips ADD CONSTRAINT "
            f"{FIRST_SEEN_STRICT_CONSTRAINT} {FIRST_SEEN_STRICT_DEFINITION}"
        )


def _packaged_bridge_relation(document: Dict):
    """The one participating relation, parsed by the strict release parser."""
    for requirement in parse_requirements(document):
        for relation in requirement.relations:
            names = {c.name for c in relation.constraints}
            names.update(c.name
                         for group in relation.constraint_alternatives
                         for c in group.constraints)
            if names & {FIRST_SEEN_EXPAND_CONSTRAINT,
                        FIRST_SEEN_STRICT_CONSTRAINT}:
                return relation
    raise AssertionError("the document declares no participating relation")


def test_G6_ready_implies_real_canonical_preflight_passes(
    tmp_root: Path,
) -> None:
    print("\n## test_G6_ready_implies_real_canonical_preflight_passes")
    import psycopg

    repository = _repository_declaration(applied_only=False)

    def _mutated(mutate) -> Dict:
        document = json.loads(json.dumps(repository))
        mutate(_bridge_relation_json(document))
        return document

    fixtures = [
        ("the exact canonical bridge relation", repository),
        ("+ a column BOTH canonical states carry",
         _mutated(lambda r: r.setdefault("columns", []).append(
             {"name": "first_seen_request_id", "type": "uuid",
              "nullable": True}))),
        ("+ an absent unconditional ck_synthetic_unconditional",
         _mutated(lambda r: r.setdefault("constraints", []).append(
             {"name": "ck_synthetic_unconditional",
              "definition": "CHECK ((first_seen_request_id IS NOT NULL))",
              "validated": True}))),
        ("+ a required column neither canonical state carries",
         _mutated(lambda r: r.setdefault("columns", []).append(
             {"name": "first_seen_synthetic_absent_col", "type": "uuid",
              "nullable": True}))),
        ("+ a required index neither canonical state carries",
         _mutated(lambda r: r.setdefault("indexes", []).append(
             {"name": "ix_synthetic_absent",
              "definition": "CREATE INDEX ix_synthetic_absent ON "
                            "public.client_trips USING btree "
                            "(first_seen_request_id)"}))),
    ]

    admin = _admin()
    admin.execute(f'DROP DATABASE IF EXISTS "{_BRIDGE_PROBE_DB}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{_BRIDGE_PROBE_DB}"')
    admin.close()
    try:
        for rich in (False, True):
            shape = "production-shaped" if rich else "bare canonical"
            for index, (label, document) in enumerate(fixtures):
                tree = tmp_root / f"g6-real-{int(rich)}-{index}"
                (tree / "db").mkdir(parents=True, exist_ok=True)
                (tree / "db" / "schema_requirements.json").write_text(
                    json.dumps(document, indent=2), encoding="utf-8"
                )
                compatible = bool(
                    inspect_release_bridge_compatibility(tree)["compatible"]
                )
                relation = _packaged_bridge_relation(document)
                observed: Dict[str, List[str]] = {}
                conn = _connect(_BRIDGE_PROBE_DB)
                conn.autocommit = True
                try:
                    for state in ("EXPAND", "CONTRACT"):
                        with conn.cursor() as cur:
                            _build_canonical_bridge_relation(
                                cur, state, rich=rich
                            )
                            observed[state] = relation_defects(cur, relation)
                finally:
                    conn.close()
                clean = not observed["EXPAND"] and not observed["CONTRACT"]
                _check(
                    f"G6 READY => real canonical EXPAND and CONTRACT preflight "
                    f"both pass ({shape}): {label}",
                    (not compatible) or clean,
                    f"G6=compatible expand={observed['EXPAND']} "
                    f"contract={observed['CONTRACT']}",
                )
                if index == 0:
                    _check(f"and the honest release really is READY with zero "
                           f"real defects ({shape})",
                           compatible and clean,
                           f"compatible={compatible} observed={observed}")
    finally:
        admin = _admin()
        admin.execute(
            f'DROP DATABASE IF EXISTS "{_BRIDGE_PROBE_DB}" WITH (FORCE)'
        )
        admin.close()


def _bridge_relation_json(document: Dict) -> Dict:
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            # `constraints` accepts the name-only string form as well as the
            # object form, exactly as `_parse_constraint` does.
            def _name(entry):
                return entry if isinstance(entry, str) else entry["name"]

            names = {_name(c) for c in relation.get("constraints") or []}
            names.update(
                _name(c)
                for group in relation.get("constraint_alternatives") or []
                for c in group["constraints"]
            )
            if names & {FIRST_SEEN_EXPAND_CONSTRAINT,
                        FIRST_SEEN_STRICT_CONSTRAINT}:
                return relation
    raise AssertionError("the document declares no participating relation")


def main() -> int:
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower():
        raise RuntimeError("refusing a production-like DSN")

    import tempfile
    with tempfile.TemporaryDirectory(prefix="m4-preflight-") as tmp:
        tmp_root = Path(tmp)
        test_B1_B8_correct_fleet_passes()
        test_B9_new_client_baseline_satisfies_the_gate()
        test_B2_platform_ledger_missing_fails()
        test_B3_platform_ledger_lies_fails()
        test_B3b_platform_object_present_but_wrong_shape_fails()
        test_B3c_platform_constraint_missing_fails()
        test_B4_one_client_missing_ledger_fails()
        test_B5_one_client_ledger_lies_fails()
        test_B6_unreachable_client_fails()
        test_E6_E7_zero_enabled_and_disabled_clients()
        test_E2_E3_enabled_client_without_a_schedule_is_still_required()
        test_E4_E5_physical_column_is_verified_not_assumed()
        test_partial_enumeration_is_structurally_impossible()
        test_B7b_the_gate_accepts_no_filter()
        test_R3_mutation_before_the_fence_is_caught()
        test_R2_raw_sql_cannot_commit_while_the_fence_is_held()
        test_R1_cooperative_enablement_is_blocked_too()
        test_the_fence_blocks_writes_but_not_reads()
        test_the_fleet_fence_is_a_table_lock_not_an_advisory_lock()
        test_B11_the_gate_mutates_nothing()
        test_M5_correct_shape_passes()
        test_M5_ledger_missing_fails()
        test_M5_run_type_missing_fails()
        test_M5_same_name_wrong_constraint_columns_fails()
        test_M5_same_name_wrong_check_expression_fails()
        test_M5_coverage_owner_uniqueness_missing_fails()
        test_M5_owner_provenance_coherence_fk_wrong_fails()
        test_M5_base_index_wrong_predicate_fails()
        test_M5_recovery_active_index_wrong_predicate_fails()
        test_M5_recovery_approval_index_wrong_keys_fails()
        test_M5_unvalidated_constraint_fails()
        test_S15_correct_shape_passes()
        test_S15_068_ledger_missing_fails()
        test_S15_069_ledger_missing_fails()
        test_S15_ledger_lies_relation_missing_fails()
        test_S15_068_column_missing_fails()
        test_S15_069_column_missing_fails()
        test_S15_069_constraint_missing_fails()
        test_S15_069_trigger_missing_fails()
        test_S15_damage_does_not_survive_the_next_build()
        test_requirements_are_read_from_the_release_tree(tmp_root)
        test_the_full_requirement_set_is_evaluated_and_fails_closed(tmp_root)
        test_G6_ready_implies_real_canonical_preflight_passes(tmp_root)

    # Leave no client databases behind.
    admin = _admin()
    for _cid, _code, dbname, _sched in CLIENTS:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    admin.close()

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
