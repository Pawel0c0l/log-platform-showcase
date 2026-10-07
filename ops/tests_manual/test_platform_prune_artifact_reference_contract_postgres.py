#!/usr/bin/env python3
"""The prune artifact-reference contract against a real PostgreSQL 16.

WHAT THIS PROVES.
    That `validate_artifact_reference_contract` accepts the EXACT six-reference
    contract the Portal V1 migration chain actually produces — not a
    hand-written list of tuples that happens to match — and that the Portal
    generated-report lifecycle behaves under a real catalog the way
    `artifact_exclusion_reason` assumes it does.

    The 2026-08-20 03:30 CEST production prune failed closed with
    `PRUNE_REFERENCE_CONTRACT_AMBIGUOUS` because migration 068 added a sixth
    foreign key to `public.artifacts`. Appending that key to the allowlist is
    only half the fix, and the database is the reason — but not in the way the
    DDL reads at first glance. 068 declares

        CHECK ((NOT is_available) OR (artifact_id IS NOT NULL))

    which looks like it would refuse the `ON DELETE SET NULL` for an AVAILABLE
    member. It does not. 068 also installs a BEFORE trigger,
    `portal_generated_report_files_availability`, that flips `is_available` to
    FALSE in the same statement — deliberately, so "object cleanup must never be
    BLOCKED by ... the availability marker".

    The consequence is worse than an abort, which is why it is proven here
    rather than argued: deleting the artifact of a live, downloadable report
    SUCCEEDS SILENTLY and converts it to `Pliki wygasły`. Nothing in the schema
    defends it. The plan-time exclusion is the only thing that does.

DESTRUCTIVE, AND TASK-OWNED. Like the S15 suite, this accepts no DSN. It starts
its own `postgres:16` container on a free loopback port, creates a database
inside it, and removes the container in a `finally`. There is no input that can
point it at `logdb`, staging or production. It performs no production write and
never invokes the prune entrypoint.

    cd /opt/log-platform-worktrees/portal-prune-reference
    env PYTHONDONTWRITEBYTECODE=1 /opt/log-platform/.venv/bin/python \
        ops/tests_manual/test_platform_prune_artifact_reference_contract_postgres.py

Without a usable Docker daemon or a local `postgres:16` image it prints
``LIVE_MIGRATION_TEST_NOT_AVAILABLE`` and exits 0.
"""
from __future__ import annotations

import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `api.platform_prune` imports boto3 at module scope for the object-store half
# of the worker. This suite exercises the DATABASE half only and never builds a
# client, so a stub keeps the suite runnable on an interpreter without boto3
# rather than silently skipping.
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("boto3")
        stub.client = lambda *a, **k: None
        sys.modules["boto3"] = stub

try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover
    print("LIVE_MIGRATION_TEST_NOT_AVAILABLE: psycopg is unavailable; run this suite with .venv/bin/python")
    raise SystemExit(0)

from ops.tests_manual.disposable_postgres import (  # noqa: E402
    DisposablePostgresUnavailable,
    disposable_postgres,
)

from api import platform_prune as prune  # noqa: E402

MIGRATIONS = (
    REPO_ROOT / "db" / "migrations" / "068_portal_generated_reports.sql",
    REPO_ROOT / "db" / "migrations" / "069_portal_generated_reports_integrity.sql",
)

#: The exact contract production carries at platform migration ceiling 069 —
#: including, for each reference, the DELETE ACTION and the referencing-column
#: nullability the prune's destructive step depends on. Written out here rather
#: than imported from the worker so that a change to the worker's constant has
#: to be restated as production truth, against a real catalog, to pass.
REF = prune.ArtifactReference
EXPECTED_SIX = {
    REF("public.artifact_metadata_overrides", "artifact_id",
        "public.artifacts", "artifact_id", "CASCADE", False),
    REF("public.artifact_tags", "artifact_id",
        "public.artifacts", "artifact_id", "CASCADE", False),
    REF("public.artifact_virtual_folder_items", "artifact_id",
        "public.artifacts", "artifact_id", "CASCADE", False),
    REF("public.database_export_jobs", "artifact_id",
        "public.artifacts", "artifact_id", "SET NULL", True),
    REF("ingest.raw_file", "stage2_cleaned_artifact_id",
        "public.artifacts", "artifact_id", "SET NULL", True),
    REF("public.portal_generated_report_files", "artifact_id",
        "public.artifacts", "artifact_id", "SET NULL", True),
}

PORTAL_REFERENCE = REF(
    "public.portal_generated_report_files", "artifact_id",
    "public.artifacts", "artifact_id", "SET NULL", True,
)

CLIENT = "ACME_01"
DSN = ""


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def run(sql: str, values=None):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            try:
                return cur.fetchall()
            except psycopg.ProgrammingError:
                return []


def _expect_sqlstate(sql: str, values=None) -> str:
    try:
        run(sql, values)
    except psycopg.errors.Error as exc:
        return str(getattr(exc, "sqlstate", "") or "") or type(exc).__name__
    raise AssertionError(f"statement was expected to fail: {sql[:160]}")


def _expect_prune_error(code: str, callback) -> None:
    try:
        callback()
    except prune.PlatformPruneError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected {code}")


# ===========================================================================
# Prerequisite schema
#
# The five pre-Portal referencing relations, in the shape that matters to the
# prune planner: the columns it projects and — critically — the DELETE ACTIONS
# the real migrations declare, since this suite's whole subject is what happens
# to a referencing row when its artifact is deleted. `unrelated_artifact` and a
# populated `runs` exist so "the fix pruned what it should" is a fact and not an
# absence of evidence.
# ===========================================================================
def _build_prerequisite_schema() -> None:
    run("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    run("CREATE SCHEMA IF NOT EXISTS ingest;")
    run("CREATE SCHEMA IF NOT EXISTS ops_control;")
    run(
        """
        CREATE TABLE portal_clients (
          client_code TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          is_active BOOLEAN NOT NULL DEFAULT TRUE
        );
        CREATE TABLE portal_database_datasets (
          dataset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          client_code TEXT NOT NULL REFERENCES portal_clients(client_code),
          slug TEXT NOT NULL,
          UNIQUE (client_code, slug)
        );
        CREATE TABLE runs (
          run_id UUID PRIMARY KEY,
          status TEXT NOT NULL,
          started_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE artifacts (
          artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id UUID REFERENCES runs(run_id),
          storage_key TEXT NOT NULL,
          storage_backend TEXT NOT NULL DEFAULT 'S3',
          workflow_name TEXT,
          raw_file_id BIGINT,
          client_code TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        -- `ts`, not `created_at`: the planner names the column, so the fixture
        -- has to carry the real one or it proves nothing about the real query.
        CREATE TABLE logs (
          id BIGSERIAL PRIMARY KEY,
          run_id UUID REFERENCES runs(run_id),
          ts TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Curation references. ON DELETE CASCADE, exactly as production carries
        -- them: deleting the artifact silently destroys the curation, which is
        -- why `artifact_retained_reference` retains at plan time instead.
        CREATE TABLE artifact_metadata_overrides (
          artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
          note TEXT
        );
        CREATE TABLE artifact_tags (
          artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
          tag TEXT NOT NULL
        );
        CREATE TABLE artifact_virtual_folder_items (
          artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
          folder TEXT NOT NULL
        );

        -- Separate-retention and Workflow B references. ON DELETE SET NULL.
        CREATE TABLE database_export_jobs (
          job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
          expires_at TIMESTAMPTZ
        );
        CREATE TABLE ingest.raw_file (
          raw_file_id BIGSERIAL PRIMARY KEY,
          stage2_cleaned_artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE SET NULL
        );

        -- Migration 064. ON DELETE RESTRICT: a reconciled run can never be
        -- pruned, so `run_exclusion_reason` must retain it at plan time.
        CREATE TABLE ops_control.run_reconciliation (
          run_id UUID PRIMARY KEY REFERENCES runs(run_id) ON DELETE RESTRICT,
          reconciled_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    run(
        "INSERT INTO portal_clients (client_code, display_name) VALUES (%s, %s)",
        (CLIENT, "Acme"),
    )


def _apply_portal_migrations() -> None:
    for path in MIGRATIONS:
        run(path.read_text(encoding="utf-8"))


def _observed_references() -> set:
    """The catalog, read through the planner's own query."""
    with connect() as conn:
        with conn.cursor() as cur:
            captured = {}

            class CapturingCursor:
                def execute(self, sql, params=None):
                    cur.execute(sql, params)

                def fetchone(self):
                    return cur.fetchone()

                def fetchall(self):
                    rows = cur.fetchall()
                    captured["rows"] = rows
                    return rows

            prune.validate_artifact_reference_contract(CapturingCursor())
            return {
                REF(
                    str(r["referencing_table"]),
                    str(r["referencing_column"]),
                    str(r["referenced_table"]),
                    str(r["referenced_column"]),
                    str(r["delete_action"]),
                    bool(r["referencing_column_nullable"]),
                )
                for r in captured["rows"]
            }


def _with_catalog_drift(*statements) -> None:
    """Apply catalog DDL, assert the prune refuses, then ROLL BACK.

    PostgreSQL's DDL is transactional, so each drift case is undone by never
    being committed. That is stronger than a hand-written `finally` restore:
    there is no restore statement that can itself be wrong and silently leave
    the next test running against a mutated catalog.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            _expect_prune_error(
                "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                lambda: prune.validate_artifact_reference_contract(cur),
            )
            # And the planner refuses BEFORE it computes a single candidate, so
            # no delete is ever planned against a drifted lifecycle.
            _expect_prune_error(
                "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                lambda: prune.build_prune_plan(
                    cur, cutoff=datetime.now(timezone.utc), retention_days=60, lock_rows=False
                ),
            )
        conn.rollback()
    # The rollback restored the reviewed contract, which is itself asserted
    # rather than assumed.
    assert _observed_references() == EXPECTED_SIX


#: Repointing the Portal FK is a DROP plus an ADD; the referencing column and
#: its NOT NULL-ness are untouched by both, which is exactly the class of drift
#: the old `(table, column)` contract could not see.
def _repoint_portal_fk(clause: str) -> tuple:
    return (
        "ALTER TABLE portal_generated_report_files "
        "DROP CONSTRAINT portal_generated_report_files_artifact_id_fkey",
        "ALTER TABLE portal_generated_report_files "
        "ADD CONSTRAINT portal_generated_report_files_artifact_id_fkey " + clause,
    )


def _new_artifact(*, created_at, workflow_name="platform", run_id=None, key=None) -> str:
    rows = run(
        """
        INSERT INTO artifacts (run_id, storage_key, storage_backend, workflow_name, created_at)
        VALUES (%s, %s, 'S3', %s, %s)
        RETURNING artifact_id::text AS artifact_id
        """,
        (run_id, key or f"artifacts/{uuid.uuid4()}.bin", workflow_name, created_at),
    )
    return rows[0]["artifact_id"]


def _publish_member(artifact_id, *, is_available=True, filename="raport.pdf") -> str:
    """One published generated-report instance carrying one member.

    Written through the REAL 068 DDL, so every invariant that migration
    declares — `I-6` exactly one main file, `I-7` members require a
    publication, the availability CHECK — has to hold for this fixture to
    exist at all. The instance and its member are one transaction because 068's
    integrity triggers are `DEFERRABLE INITIALLY DEFERRED`: a published
    instance with no member and a member with no publication are both refused
    at COMMIT, and only the pair is valid.
    """
    token = uuid.uuid4().hex[:8]
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO portal_generated_report_definitions
                      (type_key, display_name, cadence_class, period_kind,
                       generation_definition_ref)
                VALUES (%s, 'Raport tygodniowy', 'weekly', 'week', 'reports.weekly.v1')
                RETURNING definition_id::text AS definition_id
                """,
                (f"raport_tygodniowy_{token}",),
            )
            definition_id = cur.fetchone()["definition_id"]
            cur.execute(
                """
                INSERT INTO portal_generated_report_instances
                      (definition_id, client_code, period_kind, period_key,
                       period_start, period_end, display_name,
                       generation_state, last_published_at)
                VALUES (%s, %s, 'week',
                        -- 069 binds `period_key` to `(period_kind, period_start)`
                        -- by CHECK. Deriving it with the migration's own
                        -- expression keeps the fixture honest instead of
                        -- hardcoding a literal that could drift from the rule.
                        lpad(EXTRACT(ISOYEAR FROM DATE '2026-08-10')::text, 4, '0') || '-W' ||
                        lpad(EXTRACT(WEEK    FROM DATE '2026-08-10')::text, 2, '0'),
                        DATE '2026-08-10', DATE '2026-08-16', %s,
                        'succeeded', now())
                RETURNING instance_id::text AS instance_id
                """,
                (definition_id, CLIENT, "Tydzień 33 · 2026"),
            )
            instance_id = cur.fetchone()["instance_id"]
            cur.execute(
                """
                INSERT INTO portal_generated_report_files
                      (instance_id, artifact_id, display_filename, file_format,
                       content_type, size_bytes, semantic_role, is_main_file, is_available)
                VALUES (%s, %s, %s, 'PDF', 'application/pdf', 2048,
                        'main_document', TRUE, %s)
                RETURNING member_id::text AS member_id
                """,
                (instance_id, artifact_id, filename, is_available),
            )
            member_id = cur.fetchone()["member_id"]
        conn.commit()
    return member_id


def _plan(cutoff):
    """The REAL planner, over the REAL catalog, in a real transaction."""
    with connect() as conn:
        with conn.cursor() as cur:
            plan = prune.build_prune_plan(cur, cutoff=cutoff, retention_days=60, lock_rows=False)
            conn.rollback()
            return plan


# ===========================================================================
# 1. The contract the migrations actually produce
# ===========================================================================
def test_the_portal_chain_produces_exactly_the_expected_six_references() -> None:
    observed = _observed_references()
    assert observed == EXPECTED_SIX, observed
    assert len(observed) == 6
    # Read from the catalog, not from the constant: the Portal reference is
    # present under exactly the identity the allowlist declares — including the
    # delete action and the nullability the lifecycle rests on.
    assert PORTAL_REFERENCE in observed
    assert prune.EXPECTED_ARTIFACT_REFERENCES == observed

    # The five pre-Portal references keep the actions their own migrations
    # declared. This is the non-regression half of the strengthening: the
    # contract got wider, their semantics did not move.
    by_table = {r.referencing_table: r for r in observed}
    assert by_table["public.artifact_metadata_overrides"].delete_action == "CASCADE"
    assert by_table["public.artifact_tags"].delete_action == "CASCADE"
    assert by_table["public.artifact_virtual_folder_items"].delete_action == "CASCADE"
    assert by_table["public.database_export_jobs"].delete_action == "SET NULL"
    assert by_table["ingest.raw_file"].delete_action == "SET NULL"
    # Schema qualification is explicit and does not come from `search_path`:
    # `ingest.raw_file` and the five public relations are named the same way.
    assert all("." in r.referencing_table for r in observed)
    assert all(r.referenced_table == "public.artifacts" for r in observed)
    assert all(r.referenced_column == "artifact_id" for r in observed)
    # Every declared delete action is actually implemented by enabled triggers.
    assert all(r.delete_action_enforced for r in observed)


def test_the_portal_foreign_key_is_set_null_over_a_nullable_column() -> None:
    row = run(
        """
        SELECT c.conname, c.confdeltype, a.attnotnull,
               c.confrelid::regclass::text AS referenced
        FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f'
          AND c.conrelid = 'public.portal_generated_report_files'::regclass
          AND c.confrelid = 'public.artifacts'::regclass
        """
    )[0]
    assert row["conname"] == "portal_generated_report_files_artifact_id_fkey"
    assert row["confdeltype"] == "n", "the reference must be ON DELETE SET NULL"
    assert row["attnotnull"] is False, "artifact_id must be nullable for SET NULL to be legal"
    assert row["referenced"] == "artifacts"

    # And the CHECK that makes SET NULL conditional rather than free.
    definition = run(
        """
        SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
        WHERE conname = 'portal_generated_report_files_available_needs_object_check'
        """
    )[0]["def"]
    assert "is_available" in definition and "artifact_id IS NOT NULL" in definition


def test_an_unknown_seventh_reference_still_fails_closed() -> None:
    run(
        """
        CREATE TABLE some_future_feature (
          id BIGSERIAL PRIMARY KEY,
          artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE CASCADE
        )
        """
    )
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                _expect_prune_error(
                    "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                    lambda: prune.validate_artifact_reference_contract(cur),
                )
                # And the planner refuses before it plans anything, so no
                # candidate is even computed against an unreviewed schema.
                _expect_prune_error(
                    "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                    lambda: prune.build_prune_plan(
                        cur, cutoff=datetime.now(timezone.utc), retention_days=60, lock_rows=False
                    ),
                )
    finally:
        run("DROP TABLE some_future_feature")
    assert _observed_references() == EXPECTED_SIX


def test_a_dropped_expected_reference_still_fails_closed() -> None:
    run("ALTER TABLE artifact_tags DROP CONSTRAINT artifact_tags_artifact_id_fkey")
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                _expect_prune_error(
                    "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                    lambda: prune.validate_artifact_reference_contract(cur),
                )
    finally:
        run(
            "ALTER TABLE artifact_tags ADD CONSTRAINT artifact_tags_artifact_id_fkey "
            "FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE"
        )
    assert _observed_references() == EXPECTED_SIX


def test_a_repointed_expected_reference_still_fails_closed() -> None:
    """Column identity is part of the contract, not just the table."""
    run("ALTER TABLE ingest.raw_file ADD COLUMN stage3_cleaned_artifact_id UUID")
    run(
        "ALTER TABLE ingest.raw_file DROP CONSTRAINT raw_file_stage2_cleaned_artifact_id_fkey"
    )
    run(
        "ALTER TABLE ingest.raw_file ADD CONSTRAINT raw_file_stage3_cleaned_artifact_id_fkey "
        "FOREIGN KEY (stage3_cleaned_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE SET NULL"
    )
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                _expect_prune_error(
                    "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                    lambda: prune.validate_artifact_reference_contract(cur),
                )
    finally:
        run(
            "ALTER TABLE ingest.raw_file DROP CONSTRAINT raw_file_stage3_cleaned_artifact_id_fkey"
        )
        run("ALTER TABLE ingest.raw_file DROP COLUMN stage3_cleaned_artifact_id")
        run(
            "ALTER TABLE ingest.raw_file ADD CONSTRAINT raw_file_stage2_cleaned_artifact_id_fkey "
            "FOREIGN KEY (stage2_cleaned_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE SET NULL"
        )
    assert _observed_references() == EXPECTED_SIX


# ===========================================================================
# 1b. Catalog drift the OLD `(table, column)` contract could not see
#
# Every case below leaves the referencing `(table, column)` exactly as
# production carries it and mutates only what the DELETE does. Under the
# previous contract all of them were accepted, and the generated-report
# history-preservation guarantee depends on all of them being refused.
# ===========================================================================
def test_portal_fk_flipped_to_cascade_fails_closed() -> None:
    """CASCADE would destroy the history row this reference exists to protect."""
    _with_catalog_drift(
        *_repoint_portal_fk(
            "FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE"
        )
    )


def test_portal_fk_flipped_to_restrict_fails_closed() -> None:
    """RESTRICT aborts the DELETE — after the MinIO objects are already gone."""
    _with_catalog_drift(
        *_repoint_portal_fk(
            "FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT"
        )
    )


def test_portal_fk_left_at_no_action_fails_closed() -> None:
    """The PostgreSQL DEFAULT is NO ACTION, so an omitted clause is drift too."""
    _with_catalog_drift(
        *_repoint_portal_fk("FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)")
    )


def test_the_portal_fk_missing_entirely_fails_closed() -> None:
    """A rollback of migration 068's reference is not a licence to prune.

    The comparison is equality, so the ABSENCE of the Portal reference is as
    ambiguous as an unreviewed extra one — a database that lost it can no
    longer be reasoned about with `artifact_exclusion_reason`'s rules.
    """
    _with_catalog_drift(
        "ALTER TABLE portal_generated_report_files "
        "DROP CONSTRAINT portal_generated_report_files_artifact_id_fkey"
    )


def test_portal_fk_repointed_at_another_relation_fails_closed() -> None:
    """Same referencing `(table, column)`, same SET NULL, different target table.

    The catalog query is keyed on `public.artifacts`, so a reference moved off
    it disappears from the result rather than appearing as a mismatch. Either
    way the prune must refuse: `artifact_id` no longer names an artifact, and
    deleting artifacts no longer touches this column at all.
    """
    _with_catalog_drift(
        "CREATE TABLE archived_artifacts (artifact_id UUID PRIMARY KEY)",
        "INSERT INTO archived_artifacts (artifact_id) "
        "SELECT artifact_id FROM portal_generated_report_files WHERE artifact_id IS NOT NULL",
        *_repoint_portal_fk(
            "FOREIGN KEY (artifact_id) REFERENCES archived_artifacts(artifact_id) "
            "ON DELETE SET NULL"
        ),
    )


def test_portal_delete_action_with_disabled_triggers_fails_closed() -> None:
    """`confdeltype` is a declaration; a trigger is what carries it out.

    `ON DELETE SET NULL` is implemented by a referential-action trigger on the
    REFERENCED table. A superuser can disable it and leave the constraint row —
    endpoints, action, nullability — completely unchanged. The declared action
    then never fires, and deleting an artifact would leave
    `portal_generated_report_files.artifact_id` dangling instead of NULL.
    """
    trigger = run(
        """
        SELECT t.tgname
          FROM pg_trigger t
          JOIN pg_constraint c ON c.oid = t.tgconstraint
         WHERE c.conname = 'portal_generated_report_files_artifact_id_fkey'
           AND t.tgrelid = 'public.artifacts'::regclass
        """
    )
    assert trigger, "the SET NULL action must be implemented by a trigger on `artifacts`"
    _with_catalog_drift(
        f'ALTER TABLE artifacts DISABLE TRIGGER "{trigger[0]["tgname"]}"'
    )


def test_replica_session_role_fails_closed_and_really_would_skip_set_null() -> None:
    """An untouched catalog whose delete actions nonetheless do nothing.

    `session_replication_role = replica` suppresses ORDINARY triggers, and a
    foreign key's referential action is one. Every field the contract compares
    stays identical — `confdeltype` is still 'n', `tgenabled` is still 'O' — so
    this drift is invisible to the catalog comparison and has to be caught from
    the session itself.

    The hazard is demonstrated before it is guarded: in replica mode the same
    DELETE the prune issues leaves `artifact_id` DANGLING instead of NULL,
    which is worse than either outcome the lifecycle contemplates.
    """
    old = datetime.now(timezone.utc) - timedelta(days=400)
    artifact = _new_artifact(created_at=old, key="artifacts/replica-mode.bin")
    member = _publish_member(artifact, is_available=True, filename="replica.pdf")
    run(
        "UPDATE portal_generated_report_files SET is_available = FALSE WHERE member_id = %s::uuid",
        (member,),
    )

    # 1. The hazard is real. Rolled back, so nothing here survives the test.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute("DELETE FROM artifacts WHERE artifact_id = %s::uuid", (artifact,))
            cur.execute(
                "SELECT artifact_id::text AS artifact_id FROM portal_generated_report_files "
                "WHERE member_id = %s::uuid",
                (member,),
            )
            dangling = cur.fetchone()["artifact_id"]
            assert dangling == artifact, (
                "expected replica mode to skip the SET NULL and leave a dangling "
                f"reference, got {dangling!r}"
            )
        conn.rollback()

    # 2. The guard refuses that session before anything is planned, even though
    #    the catalog it would inspect is exactly the accepted one.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            _expect_prune_error(
                "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                lambda: prune.validate_artifact_reference_contract(cur),
            )
            _expect_prune_error(
                "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
                lambda: prune.build_prune_plan(
                    cur, cutoff=datetime.now(timezone.utc), retention_days=60, lock_rows=False
                ),
            )
        conn.rollback()

    # 3. Nothing above deleted anything, and an `origin` session still passes.
    assert run(
        "SELECT count(*) AS n FROM artifacts WHERE artifact_id = %s::uuid", (artifact,)
    )[0]["n"] == 1
    assert _observed_references() == EXPECTED_SIX


def test_disabling_every_referential_trigger_fails_closed() -> None:
    """The blunt version of the same drift, over all six references at once."""
    _with_catalog_drift("ALTER TABLE artifacts DISABLE TRIGGER ALL")


def test_a_validated_contract_cannot_be_repointed_underneath_the_plan() -> None:
    """Validation and planning are separate statements; the lock closes the gap.

    Without it a migration could commit between the two, so the worker would
    validate `SET NULL` and then plan — and delete — under a freshly committed
    `CASCADE`, destroying report history the guard had just certified as safe.
    The planner takes `ACCESS SHARE` on every reviewed relation before it
    trusts the contract, and `ALTER TABLE ... DROP CONSTRAINT` needs
    `ACCESS EXCLUSIVE`, so the migration blocks until the prune is done.
    """
    with connect() as planner:
        with planner.cursor() as cur:
            plan = prune.build_prune_plan(
                cur,
                cutoff=datetime.now(timezone.utc) - timedelta(days=60),
                retention_days=60,
                lock_rows=False,
            )
            assert plan is not None

            with connect() as migrator:
                with migrator.cursor() as other:
                    other.execute("SET lock_timeout = '1500ms'")
                    try:
                        other.execute(
                            "ALTER TABLE portal_generated_report_files "
                            "DROP CONSTRAINT portal_generated_report_files_artifact_id_fkey"
                        )
                    except psycopg.errors.LockNotAvailable:
                        blocked = True
                    else:
                        blocked = False
                    migrator.rollback()

            assert blocked, (
                "a concurrent migration could repoint the Portal reference after it "
                "was validated and before the plan acted on it"
            )
        planner.rollback()

    assert _observed_references() == EXPECTED_SIX


def test_the_planner_pins_the_schema_it_was_validated_against() -> None:
    """Validation and deletion must name the same relations.

    The contract is checked against `public.artifacts` explicitly while the
    planner's own statements name relations unqualified. A `search_path` that
    puts another schema first — set on the role, the database, or inherited
    through `PGOPTIONS` — would otherwise leave the guard inspecting `public`
    while the planner read, and the delete path deleted, a shadow relation.
    """
    old = datetime.now(timezone.utc) - timedelta(days=400)
    genuine = _new_artifact(created_at=old, key="artifacts/genuine.bin")

    run("CREATE SCHEMA IF NOT EXISTS shadow")
    run(
        """
        CREATE TABLE IF NOT EXISTS shadow.artifacts (
          artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id UUID,
          storage_key TEXT NOT NULL,
          storage_backend TEXT NOT NULL DEFAULT 'S3',
          workflow_name TEXT,
          raw_file_id BIGINT,
          client_code TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    run(
        "INSERT INTO shadow.artifacts (storage_key, workflow_name, created_at) "
        "VALUES ('artifacts/shadow-decoy.bin', 'platform', %s)",
        (old,),
    )
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                # A session whose search_path prefers the shadow schema. Every
                # unqualified `artifacts` below would resolve there by default.
                cur.execute("SET search_path = shadow, public")
                cur.execute("SELECT 'artifacts'::regclass::oid AS oid")
                shadowed = cur.fetchone()["oid"]
                cur.execute("SELECT 'public.artifacts'::regclass::oid AS oid")
                assert shadowed != cur.fetchone()["oid"], (
                    "the fixture must actually shadow `artifacts`, or this proves nothing"
                )

                plan = prune.build_prune_plan(
                    cur,
                    cutoff=datetime.now(timezone.utc) - timedelta(days=60),
                    retention_days=60,
                    lock_rows=False,
                )
                keys = {a.storage_key for a in plan.artifacts}
                assert "artifacts/genuine.bin" in keys, (
                    "the planner read the shadow relation instead of the reviewed one"
                )
                assert "artifacts/shadow-decoy.bin" not in keys, (
                    "the planner planned a MinIO object named by a shadow row"
                )
                assert genuine in [a.artifact_id for a in plan.artifacts]
            conn.rollback()
    finally:
        run("DROP SCHEMA shadow CASCADE")
    assert _observed_references() == EXPECTED_SIX


def test_portal_fk_repointed_at_another_target_column_fails_closed() -> None:
    """Same referencing column, same SET NULL, different candidate key.

    `artifacts` is not required to carry exactly one unique column forever. A
    reference repointed at a second one is a different lifecycle wearing an
    identical `(table, column)` name, so the referenced column is compared.
    """
    _with_catalog_drift(
        "ALTER TABLE artifacts ADD COLUMN legacy_artifact_id UUID",
        "UPDATE artifacts SET legacy_artifact_id = artifact_id",
        "ALTER TABLE artifacts ADD CONSTRAINT artifacts_legacy_artifact_id_key "
        "UNIQUE (legacy_artifact_id)",
        *_repoint_portal_fk(
            "FOREIGN KEY (artifact_id) REFERENCES artifacts(legacy_artifact_id) "
            "ON DELETE SET NULL"
        ),
    )


def test_portal_referencing_column_made_not_null_fails_closed() -> None:
    """`SET NULL` over a NOT NULL column is a delete that raises mid-prune.

    Nullability is guarded explicitly rather than inferred from the delete
    action, because the two can drift apart: this catalog keeps SET NULL and
    only removes the column's ability to accept it.
    """
    _with_catalog_drift(
        # A member whose artifact was already cleaned up carries NULL, and
        # `SET NOT NULL` would reject THAT for reasons unrelated to the
        # contract. Give those rows an object so the DDL succeeds and the
        # VALIDATOR is what refuses. Nothing is committed, so the NULLs return.
        "INSERT INTO artifacts (storage_key) VALUES ('drift/nullability.bin')",
        "UPDATE portal_generated_report_files SET artifact_id = "
        "(SELECT artifact_id FROM artifacts WHERE storage_key = 'drift/nullability.bin') "
        "WHERE artifact_id IS NULL",
        # 068's integrity triggers are DEFERRABLE INITIALLY DEFERRED, so the
        # UPDATE above leaves pending events and PostgreSQL refuses to ALTER a
        # table that has them. Flushing them here is not a workaround for a
        # violation: the members and their publications are untouched, so the
        # invariants hold immediately.
        "SET CONSTRAINTS ALL IMMEDIATE",
        "ALTER TABLE portal_generated_report_files ALTER COLUMN artifact_id SET NOT NULL",
    )


def test_a_legacy_reference_relaxed_from_cascade_fails_closed() -> None:
    """The five pre-Portal references are pinned by the same rule.

    Strengthening the contract for the Portal reference alone would have left
    five FKs whose delete semantics could still change unreviewed. This proves
    the guard is uniform without changing what those references currently do.
    """
    _with_catalog_drift(
        "ALTER TABLE artifact_tags DROP CONSTRAINT artifact_tags_artifact_id_fkey",
        "ALTER TABLE artifact_tags ADD CONSTRAINT artifact_tags_artifact_id_fkey "
        "FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE SET NULL",
    )


def test_a_composite_reference_to_artifacts_fails_closed() -> None:
    """A multi-column FK arrives as several rows that each look like one.

    Without the explicit refusal those per-column rows would be compared as if
    they were whole references, and a two-column key whose first column matched
    an expected entry could slip through.
    """
    _with_catalog_drift(
        "ALTER TABLE artifacts ADD COLUMN cohort_key TEXT",
        "ALTER TABLE artifacts ADD CONSTRAINT artifacts_cohort_key "
        "UNIQUE (artifact_id, cohort_key)",
        "CREATE TABLE composite_reference (artifact_id UUID, cohort_key TEXT, "
        "FOREIGN KEY (artifact_id, cohort_key) "
        "REFERENCES artifacts(artifact_id, cohort_key) ON DELETE SET NULL)",
    )


# ===========================================================================
# 2. The Portal lifecycle, decided by the database
# ===========================================================================
def test_the_database_does_not_defend_an_available_member() -> None:
    """Why the allowlist entry alone would have been the WRONG fix.

    This is the exact statement the prune executes. Run without the plan-time
    exclusion it does not raise, does not roll back, and does not warn: the
    artifact is destroyed and a report the product was still offering for
    download silently becomes `Pliki wygasły`.

    The availability CHECK does not prevent it, because 068's own BEFORE
    trigger satisfies the CHECK by dropping availability first. Asserting the
    opposite from the DDL alone would have been wrong, so it is asserted here
    against a real server instead.
    """
    old = datetime.now(timezone.utc) - timedelta(days=400)
    artifact = _new_artifact(created_at=old)
    member = _publish_member(artifact, is_available=True)

    run("DELETE FROM artifacts WHERE artifact_id = %s::uuid", (artifact,))

    assert run(
        "SELECT count(*) AS n FROM artifacts WHERE artifact_id = %s::uuid", (artifact,)
    )[0]["n"] == 0, "the database raised no objection: the bytes are simply gone"
    state = run(
        "SELECT artifact_id, is_available FROM portal_generated_report_files "
        "WHERE member_id = %s::uuid",
        (member,),
    )[0]
    assert state["artifact_id"] is None
    assert state["is_available"] is False, (
        "068's availability trigger silently demoted a live report rather than "
        "refusing the delete"
    )
    # The trigger that does it is a declared part of the schema, not incidental.
    assert run(
        "SELECT count(*) AS n FROM pg_trigger WHERE tgname = %s AND NOT tgisinternal",
        ("portal_generated_report_files_availability",),
    )[0]["n"] == 1


def test_an_available_member_retains_its_artifact_at_plan_time() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    artifact = _new_artifact(created_at=old)
    _publish_member(artifact, is_available=True)

    plan = _plan(datetime.now(timezone.utc) - timedelta(days=60))
    assert artifact not in [a.artifact_id for a in plan.artifacts]
    assert plan.excluded.get("artifact_generated_report_available", 0) >= 1


def test_an_unavailable_member_does_not_retain_its_artifact_and_survives_it() -> None:
    """The other half of the owner's lifecycle rule, end to end.

    An expired member is not a retention reason; the artifact is planned,
    deleted through the REAL delete path, and the history row survives with
    `artifact_id = NULL`, which is what migration 068 designed SET NULL for.
    """
    old = datetime.now(timezone.utc) - timedelta(days=400)
    artifact = _new_artifact(created_at=old)
    member = _publish_member(artifact, is_available=True, filename="wygasly.pdf")
    # Availability is dropped first, exactly as `expire_due_members` does it —
    # the CHECK makes that ordering mandatory, not stylistic.
    run(
        "UPDATE portal_generated_report_files SET is_available = FALSE WHERE member_id = %s::uuid",
        (member,),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    plan = _plan(cutoff)
    assert artifact in [a.artifact_id for a in plan.artifacts]

    with connect() as conn:
        with conn.cursor() as cur:
            live = prune.build_prune_plan(cur, cutoff=cutoff, retention_days=60, lock_rows=False)
            live.artifacts = [a for a in live.artifacts if a.artifact_id == artifact]
            live.log_ids, live.run_ids = [], []
            live.provider_request_log_rows = 0
            result = prune._delete_database_rows(cur, live)
            conn.commit()
    assert result["artifact_rows"] == 1

    surviving = run(
        "SELECT artifact_id, display_filename, is_available, size_bytes "
        "FROM portal_generated_report_files WHERE member_id = %s::uuid",
        (member,),
    )
    assert len(surviving) == 1, "the member row must outlive its bytes"
    assert surviving[0]["artifact_id"] is None
    assert surviving[0]["display_filename"] == "wygasly.pdf"
    assert surviving[0]["is_available"] is False


# ===========================================================================
# 2b. Why the availability check is not a time-of-check/time-of-use window
# ===========================================================================
def test_publication_cannot_bind_an_artifact_the_prune_has_locked() -> None:
    """The exclusion reads `is_available` at PLAN time and deletes later.

    That is only safe if no concurrent publication can make a member available
    for an artifact already planned. It cannot, and the reason is a lock the
    prune already takes rather than anything added here: the real
    (non-dry-run) path plans with `FOR UPDATE OF artifact`, and INSERTing a row
    that REFERENCES an artifact takes `FOR KEY SHARE` on that parent row.
    `FOR KEY SHARE` conflicts with `FOR UPDATE`, so the publication blocks
    until the prune transaction ends — and then fails its own foreign key
    against the deleted row rather than resurrecting it.

    Asserted against a real server with a `lock_timeout`, because "these lock
    modes conflict" is exactly the kind of claim that is easy to get backwards.
    """
    old = datetime.now(timezone.utc) - timedelta(days=400)
    planned_artifact = _new_artifact(created_at=old, key="artifacts/toctou-planned.bin")
    # A published instance to hang a second member on. Its own artifact is
    # recent, so nothing about this fixture is itself prunable.
    carrier = _new_artifact(created_at=datetime.now(timezone.utc))
    member = _publish_member(carrier, is_available=True, filename="carrier.pdf")
    instance_id = run(
        "SELECT instance_id::text AS instance_id FROM portal_generated_report_files "
        "WHERE member_id = %s::uuid",
        (member,),
    )[0]["instance_id"]

    with connect() as planner:
        with planner.cursor() as cur:
            plan = prune.build_prune_plan(
                cur,
                cutoff=datetime.now(timezone.utc) - timedelta(days=60),
                retention_days=60,
                lock_rows=True,
            )
            assert planned_artifact in [a.artifact_id for a in plan.artifacts], (
                "the fixture must actually be planned, or this proves nothing"
            )

            # A concurrent publication tries to bind the planned artifact.
            with connect() as publisher:
                with publisher.cursor() as other:
                    other.execute("SET lock_timeout = '1500ms'")
                    try:
                        other.execute(
                            """
                            INSERT INTO portal_generated_report_files (
                              instance_id, artifact_id, display_filename, file_format,
                              content_type, size_bytes, semantic_role, is_main_file,
                              is_available)
                            VALUES (%s::uuid, %s::uuid, 'zalacznik.csv', 'CSV', 'text/csv',
                                    16, 'raw_data', FALSE, TRUE)
                            """,
                            (instance_id, planned_artifact),
                        )
                    except psycopg.errors.LockNotAvailable:
                        blocked = True
                    else:
                        blocked = False
                    publisher.rollback()

            assert blocked, (
                "the publication was NOT serialised against the planned artifact: "
                "the availability exclusion would be a real TOCTOU window"
            )
        planner.rollback()

    # And the artifact is still there, unpruned: nothing above deleted anything.
    assert run(
        "SELECT count(*) AS n FROM artifacts WHERE artifact_id = %s::uuid",
        (planned_artifact,),
    )[0]["n"] == 1


def test_availability_only_moves_from_true_to_false_in_the_codebase() -> None:
    """Repository evidence for the other half of the TOCTOU argument.

    An existing member never becomes available again. Only two paths write the
    marker outside tests — `expire_due_members`, which sets it FALSE, and 068's
    own trigger, which sets it FALSE — and republication does not flip a row at
    all: it DELETEs the instance's members and INSERTs new ones, which are new
    rows bound to freshly created artifacts rather than resurrections.
    """
    publication = (REPO_ROOT / "api" / "report_explorer" / "publication.py").read_text("utf-8")
    trigger_sql = (REPO_ROOT / "db" / "migrations" / "068_portal_generated_reports.sql").read_text("utf-8")

    assert "SET is_available = FALSE" in publication
    assert "NEW.is_available := FALSE" in trigger_sql
    # Republication replaces rather than reactivates.
    assert "DELETE FROM portal_generated_report_files WHERE instance_id = %s" in publication

    # No production path anywhere sets the marker back to TRUE on an EXISTING
    # row. Scanned across the whole non-test tree, not just this module.
    import re

    reactivate = re.compile(r"SET\s+is_available\s*=\s*(TRUE|true|%s)", re.IGNORECASE)
    offenders = []
    for directory in ("api", "jobs", "scripts", "db"):
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sql"} or not path.is_file():
                continue
            if reactivate.search(path.read_text("utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"a path re-enables availability on an existing member: {offenders}"


# ===========================================================================
# 3. Everything else the planner already promised
# ===========================================================================
def test_existing_reference_classes_and_plain_candidates_are_unchanged() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)

    plain = _new_artifact(created_at=old)
    tagged = _new_artifact(created_at=old)
    run("INSERT INTO artifact_tags (artifact_id, tag) VALUES (%s::uuid, 'keep')", (tagged,))
    exported = _new_artifact(created_at=old)
    run("INSERT INTO database_export_jobs (artifact_id) VALUES (%s::uuid)", (exported,))
    workflow_b = _new_artifact(created_at=old, workflow_name="workflow_b")
    recent = _new_artifact(created_at=datetime.now(timezone.utc))

    plan = _plan(cutoff)
    planned = set(a.artifact_id for a in plan.artifacts)
    assert plain in planned, "an independently eligible artifact must remain pruneable"
    assert tagged not in planned
    assert exported not in planned
    assert workflow_b not in planned
    assert recent not in planned
    assert plan.excluded.get("artifact_retained_reference", 0) >= 1
    assert plan.excluded.get("artifact_separate_retention", 0) >= 1
    assert plan.excluded.get("artifact_workflow_b", 0) >= 1


def test_historical_reconciliation_retention_is_unchanged() -> None:
    """Migration 064 semantics must survive this change untouched."""
    run(
        "INSERT INTO runs (run_id, status, started_at) VALUES (%s::uuid, 'FAILED', %s)",
        (str(uuid.uuid4()), datetime.now(timezone.utc) - timedelta(days=400)),
    )
    reconciled = str(uuid.uuid4())
    run(
        "INSERT INTO runs (run_id, status, started_at) VALUES (%s::uuid, 'FAILED', %s)",
        (reconciled, datetime.now(timezone.utc) - timedelta(days=400)),
    )
    run("INSERT INTO ops_control.run_reconciliation (run_id) VALUES (%s::uuid)", (reconciled,))

    plan = _plan(datetime.now(timezone.utc) - timedelta(days=60))
    assert reconciled not in plan.run_ids
    assert plan.excluded.get("run_retained_reference", 0) >= 1

    # And the database agrees about WHY: the FK is RESTRICT, so planning it
    # would have failed the transaction after MinIO objects were gone.
    deltype = run(
        "SELECT confdeltype FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid = 'ops_control.run_reconciliation'::regclass "
        "AND confrelid = 'public.runs'::regclass"
    )[0]["confdeltype"]
    assert deltype == "r"
    assert (
        prune.run_exclusion_reason(
            {"status": "FAILED", "has_logs": False, "has_artifacts": False, "has_reconciliation": True}
        )
        == "run_retained_reference"
    )


def main() -> None:
    global DSN
    try:
        with disposable_postgres(label="prune-refs") as (dsn, _info):
            DSN = dsn
            _build_prerequisite_schema()
            _apply_portal_migrations()
            tests = [
                value
                for name, value in sorted(globals().items())
                if name.startswith("test_") and callable(value)
            ]
            for test in tests:
                test()
                print(f"{test.__name__}: OK")
            print(f"platform prune artifact-reference contract: {len(tests)} passed")
    except DisposablePostgresUnavailable as exc:
        print(f"LIVE_MIGRATION_TEST_NOT_AVAILABLE: {exc}")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
