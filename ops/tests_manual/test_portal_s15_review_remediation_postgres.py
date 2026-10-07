#!/usr/bin/env python3
"""S15 review-remediation evidence, against a real PostgreSQL 16.

Every test here exists because an independent review found a concrete defect in
the S15 candidate `891106e`. Each one reproduces the finding's mechanism and then
proves the corrected behaviour — against a real database and a real object-store
fake, never against a Python model of either.

    1  concurrent report-instance idempotency   REPORT_INSTANCE_CONCURRENT_IDEMPOTENCY
    2  publication claim fencing / replay       REPORT_PUBLICATION_FENCING
    3  preview MIME / previewability            REPORT_PREVIEW_DELIVERY_SAFETY
    4  bulk-archive memory bound                REPORT_BULK_ARCHIVE_BOUND
    5  forgeable availability summary           REPORT_AVAILABILITY_SUMMARY_INTEGRITY
    6  RP-18 source provenance                  REPORT_SOURCE_PROVENANCE_INTEGRITY
    6b RP-18 active source-dataset binding      REPORT_SOURCE_DATASET_BINDING
    7  definition file contract                 REPORT_FILE_CONTRACT_ENFORCEMENT
    8  complete pagination                      REPORT_LIBRARY_COMPLETE_PAGINATION

DESTRUCTIVE, AND TASK-OWNED. Like the S15 suite it reuses, this accepts no DSN:
it starts its own `postgres:16` container on a free loopback port, verifies the
server really is major version 16, creates its own database inside it, and
removes the container in a `finally`. It applies `068` and `069` to nothing else.

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \\
        ops/tests_manual/test_portal_s15_review_remediation_postgres.py

Without a usable Docker daemon or a local `postgres:16` image it prints
``LIVE_MIGRATION_TEST_NOT_AVAILABLE`` and exits 0.
"""
from __future__ import annotations

import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The S15 suite owns the stub installation, the prerequisite schema, the artifact
# fixtures and the service wiring. Reusing it is what makes this suite evidence
# about the SAME system rather than about a second, friendlier one.
from ops.tests_manual import test_portal_s15_report_explorer_postgres as base  # noqa: E402

if not hasattr(base, "psycopg"):  # pragma: no cover - driver-less interpreter
    print("LIVE_MIGRATION_TEST_NOT_AVAILABLE: psycopg is unavailable")
    raise SystemExit(0)

psycopg = base.psycopg
api_main = base.api_main

from api.report_explorer.contract import normalize_contract  # noqa: E402
from api.report_explorer.delivery import (  # noqa: E402
    ARCHIVE_MAX_TOTAL_BYTES,
    INLINE_PREVIEW_CONTENT_TYPES,
    safe_archive_name,
    unique_archive_names,
)
from api.report_explorer.errors import (  # noqa: E402
    AttemptAlreadyCompletedError,
    ReportFileContractError,
    ReportFilePreviewUnsupportedError,
    ReportProvenanceError,
    StaleAttemptError,
)
from api.report_explorer.models import LibraryQuery  # noqa: E402
from api.report_explorer.periods import InvalidPeriodError, ReportingPeriod, canonical_period  # noqa: E402
from api.report_explorer.publication import DefinitionSpec, PublishedFile  # noqa: E402

CLIENT = base.CLIENT
OTHER_CLIENT = base.OTHER_CLIENT
ALICE = base.ALICE
CAROL = base.CAROL
FRANK = base.FRANK
DATASET_ACME = base.DATASET_ACME
DATASET_OTHER = base.DATASET_OTHER

run = base.run
connect = base.connect
_expect_error = base._expect_error
_new_artifact = base._new_artifact
_user = base._user

FAILURES: list[str] = []


def _ok(message: str) -> None:
    print(f"PASS: {message}")


def _reset() -> None:
    base._reset_schema()
    base._apply_migration()


def _seed() -> tuple[str, str]:
    return base._seed_definitions()


def _pdf(name: str, *, size: int = 4096, client: str = CLIENT, expires_at=None) -> PublishedFile:
    return PublishedFile(
        artifact_id=_new_artifact(client_code=client, name=name, size=size, expires_at=expires_at),
        display_filename=name, file_format="PDF", content_type="application/pdf",
        size_bytes=size, semantic_role="main_document", is_main_file=True,
        is_previewable=True,
    )


# ===========================================================================
# 0. The migration chain itself
# ===========================================================================

def test_the_correction_migration_applies_after_068_and_reruns() -> None:
    """`prerequisites -> 068 -> 069`, then again, then a clean full chain.

    `068` is immutable shared history, so the only supported way to reach the
    corrected schema is the chain. Both orders that can occur in reality are
    exercised: a database that already carries `068` with data in it, and a clean
    database replaying everything.
    """
    # (a) an existing 068-only database, with canonical rows already in it.
    base._reset_schema()
    base._apply_migration(files=(base.MIGRATION,))
    weekly = base._publisher().register_definition(base.WEEKLY)
    run(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
        "VALUES (%s,%s,'week','2026-W28',%s,%s,'Tydzień 28')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    )
    before = run(
        "SELECT count(*)::int AS n FROM portal_generated_report_instances"
    )[0]["n"]
    base._apply_migration(files=(base.CORRECTION,))
    after = run("SELECT count(*)::int AS n FROM portal_generated_report_instances")[0]["n"]
    assert before == after == 1, (before, after)

    # (b) rerunning the correction is a no-op, as repository migration policy
    #     requires of every migration in the tree.
    base._apply_migration(files=(base.CORRECTION,))

    # (c) a clean database running the CURRENT complete chain reaches the same
    #     final schema.
    def _fingerprint() -> tuple:
        constraints = run(
            """
            SELECT c.conname, pg_get_constraintdef(c.oid) AS def, t.relname
              FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname LIKE 'portal_generated_report%'
             ORDER BY t.relname, c.conname
            """
        )
        triggers = run(
            """
            SELECT tg.tgname, t.relname FROM pg_trigger tg
              JOIN pg_class t ON t.oid = tg.tgrelid
             WHERE t.relname LIKE 'portal_generated_report%' AND NOT tg.tgisinternal
             ORDER BY t.relname, tg.tgname
            """
        )
        columns = run(
            """
            SELECT table_name, column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_name LIKE 'portal_generated_report%'
             ORDER BY table_name, column_name
            """
        )
        return (
            tuple((r["relname"], r["conname"], r["def"]) for r in constraints),
            tuple((r["relname"], r["tgname"]) for r in triggers),
            tuple(
                (r["table_name"], r["column_name"], r["data_type"], r["is_nullable"])
                for r in columns
            ),
        )

    upgraded = _fingerprint()
    _reset()
    fresh = _fingerprint()
    assert upgraded == fresh, "an upgraded 068 database and a clean chain must agree"

    # The redundant second identity is gone, and the corrected one is present.
    names = {row["conname"] for row in run(
        "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = 'portal_generated_report_instances'"
    )}
    assert "portal_generated_report_instances_period_key_uniq" not in names, names
    assert "portal_generated_report_instances_period_start_uniq" in names, names
    assert "portal_generated_report_instances_period_key_canonical_check" in names, names
    _ok("068 -> 069 upgrades, reruns, and equals a clean full-chain schema")


def test_the_release_requirement_names_the_whole_chain() -> None:
    """A release built from this tree is NOT ready on 068 alone."""
    import json

    declared = json.loads((REPO_ROOT / "db" / "schema_requirements.json").read_text())
    s15 = [r for r in declared["requirements"] if r.get("milestone") == "S15"]
    migrations = sorted(r["migration"] for r in s15)
    assert migrations == [
        "068_portal_generated_reports.sql",
        "069_portal_generated_reports_integrity.sql",
    ], migrations

    # Every relation, column, constraint and trigger the declaration names must
    # exist in the schema the chain actually produces — the declaration is a
    # promise about a database, not a description of a file.
    _reset()
    present_constraints = {
        row["conname"] for row in run(
            "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname LIKE 'portal_generated_report%'"
        )
    }
    present_triggers = {
        row["tgname"] for row in run(
            "SELECT tgname FROM pg_trigger tg JOIN pg_class t ON t.oid = tg.tgrelid "
            "WHERE t.relname LIKE 'portal_generated_report%' AND NOT tg.tgisinternal"
        )
    }
    present_columns = {
        (row["table_name"], row["column_name"]): row
        for row in run(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name LIKE 'portal_generated_report%'"
        )
    }
    for requirement in s15:
        for relation in requirement["relations"]:
            table = relation["table"]
            for name in relation.get("constraints", ()):
                assert name in present_constraints, (requirement["migration"], name)
            for trigger in relation.get("triggers", ()):
                assert trigger["name"] in present_triggers, (requirement["migration"], trigger)
            for column in relation.get("columns", ()):
                found = present_columns.get((table, column["name"]))
                assert found is not None, (table, column)
                assert (found["is_nullable"] == "YES") == bool(column["nullable"]), (table, column)
    _ok("the release requirement declares 068 AND 069 and matches the produced schema")


# ===========================================================================
# 1. REPORT_INSTANCE_CONCURRENT_IDEMPOTENCY
# ===========================================================================

def test_a_period_key_cannot_describe_another_logical_period() -> None:
    """Sequential mismatch: rejected before it can become a valid instance.

    The review's escape: a custom key over canonical dates was persisted, and the
    canonical retry that followed collided with it instead of reusing it.
    """
    _reset()
    weekly, _ = _seed()

    # (a) the domain object refuses to exist at all.
    try:
        ReportingPeriod(kind="week", key="2026-W01", start=date(2026, 7, 6), end=date(2026, 7, 12))
        raise AssertionError("a mismatched period key must not construct")
    except InvalidPeriodError as exc:
        assert "2026-W28" in str(exc), exc
    try:
        canonical_period("week", date(2026, 7, 8), key="wlasny-klucz")
        raise AssertionError("canonical_period must not accept an override")
    except InvalidPeriodError:
        pass

    # (b) and the database refuses it too, so a writer that bypasses the domain
    #     object gains nothing.
    assert _expect_error(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
        "VALUES (%s,%s,'week','2026-W01',%s,%s,'Podmiana')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    ) == "23514"

    # (c) the canonical key for those dates is accepted, once.
    run(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
        "VALUES (%s,%s,'week','2026-W28',%s,%s,'Tydzień 28')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    )
    rows = run("SELECT period_key FROM portal_generated_report_instances")
    assert [r["period_key"] for r in rows] == ["2026-W28"], rows
    _ok("a period key that names another period is refused by the domain and the database")


def test_concurrent_first_generation_converges_on_one_instance() -> None:
    """The exact race the review reproduced, on two independent connections.

    Part (a) is DETERMINISTIC rather than hopeful: connection A holds an
    uncommitted insert of the period, connection B calls `begin_generation` and
    is observed BLOCKED in `pg_locks`, A commits, and B must resolve to A's row
    through `ON CONFLICT` — not raise `23505`.
    """
    _reset()
    weekly, _ = _seed()
    period = canonical_period("week", date(2026, 7, 8))
    publisher = base._publisher()
    outcome: dict = {}

    blocker = psycopg.connect(base.DSN)
    try:
        with blocker.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(
                "INSERT INTO portal_generated_report_instances "
                "(definition_id, client_code, period_kind, period_key, period_start, period_end, "
                " display_name, generation_state, attempt_count, claim_token) "
                "VALUES (%s,%s,'week','2026-W28',%s,%s,'Tydzień 28','running',1,gen_random_uuid()) "
                "RETURNING instance_id",
                (weekly, CLIENT, period.start, period.end),
            )
            blocked_id = str(cur.fetchone()[0])

        def _second() -> None:
            try:
                outcome["attempt"] = publisher.begin_generation(
                    client_code=CLIENT, type_key="raport_207", period=period,
                    display_name="Tydzień 28 · 2026",
                )
            except Exception as exc:  # noqa: BLE001 - the finding was an escape
                outcome["error"] = exc

        thread = threading.Thread(target=_second)
        thread.start()
        deadline = time.time() + 10
        waiting = False
        while time.time() < deadline:
            granted = run(
                "SELECT count(*)::int AS n FROM pg_locks WHERE NOT granted"
            )[0]["n"]
            if granted:
                waiting = True
                break
            time.sleep(0.05)
        assert waiting, "the second generation must WAIT on the period identity"
        assert "attempt" not in outcome and "error" not in outcome, outcome
        blocker.commit()
        thread.join(timeout=20)
    finally:
        blocker.close()

    assert "error" not in outcome, f"the second generation escaped as {outcome.get('error')!r}"
    attempt = outcome["attempt"]
    assert attempt.instance_id == blocked_id, (attempt.instance_id, blocked_id)
    rows = run("SELECT instance_id, attempt_count FROM portal_generated_report_instances")
    assert len(rows) == 1, rows
    assert int(rows[0]["attempt_count"]) == 2, rows

    # (b) and the symmetric case through the service on BOTH sides, repeated, so
    #     neither ordering can escape.
    for round_index in range(4):
        _reset()
        _seed()
        target = canonical_period("week", date(2026, 6, 1) + timedelta(days=7 * round_index))
        results: list = []
        errors: list = []
        barrier = threading.Barrier(2)

        def _begin() -> None:
            local = base._publisher()
            barrier.wait()
            try:
                results.append(
                    local.begin_generation(
                        client_code=CLIENT, type_key="raport_207", period=target,
                        display_name=f"Tydzień {target.key}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_begin) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        assert len({a.instance_id for a in results}) == 1, results
        stored = run("SELECT instance_id, claim_token FROM portal_generated_report_instances")
        assert len(stored) == 1, stored
        # Exactly one of the two claims is the CURRENT one, deterministically.
        current = str(stored[0]["claim_token"])
        assert sum(1 for a in results if a.claim_token == current) == 1, (results, current)
    _ok("two concurrent begin_generation calls converge on one instance, never on 23505")


def test_retry_and_the_neighbouring_period() -> None:
    """A retry resolves the existing instance; an adjacent period is its own."""
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    first = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    retry = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    assert retry.instance_id == first.instance_id, (first, retry)
    assert retry.claim_token != first.claim_token
    assert retry.attempt_count == first.attempt_count + 1

    neighbour = canonical_period("week", date(2026, 7, 15))
    other = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=neighbour, display_name="Tydzień 29",
    )
    assert other.instance_id != first.instance_id
    rows = run("SELECT period_key FROM portal_generated_report_instances ORDER BY period_start")
    assert [r["period_key"] for r in rows] == ["2026-W28", "2026-W29"], rows
    _ok("a retry keeps the instance identity; an adjacent period is a separate instance")


# ===========================================================================
# 2. REPORT_PUBLICATION_FENCING
# ===========================================================================

def _members(instance_id: str) -> list[dict]:
    return run(
        "SELECT member_id, display_filename, semantic_role FROM portal_generated_report_files "
        "WHERE instance_id = %s ORDER BY display_filename",
        (instance_id,),
    )


def test_two_concurrent_publishes_with_one_claim_replace_membership_once() -> None:
    """The review published twice from one claim and both calls succeeded."""
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    set_a = [_pdf("wersja-a.pdf")]
    set_b = [_pdf("wersja-b.pdf")]
    errors: list = []
    barrier = threading.Barrier(2)

    def _publish(files) -> None:
        local = base._publisher()
        barrier.wait()
        try:
            local.publish(attempt, files=files, row_count=10)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_publish, args=(files,)) for files in (set_a, set_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    members = _members(attempt.instance_id)
    # ONE mutation won. The member set is exactly one of the two published sets,
    # never both and never a duplicate of either.
    assert len(members) == 1, members
    assert members[0]["display_filename"] in {"wersja-a.pdf", "wersja-b.pdf"}, members
    row = run(
        "SELECT generation_state, claim_token, completed_claim_token, completed_claim_outcome, "
        "       published_member_count, available_member_count "
        "  FROM portal_generated_report_instances WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    assert row["generation_state"] == "succeeded", row
    assert row["claim_token"] is None, row
    assert str(row["completed_claim_token"]) == attempt.claim_token, row
    assert row["completed_claim_outcome"] == "succeeded", row
    assert (row["published_member_count"], row["available_member_count"]) == (1, 1), row
    _ok("two concurrent publishes on one claim replace membership exactly once")


def test_a_duplicate_completed_callback_is_a_deterministic_no_op() -> None:
    """A retried callback must not corrupt state or duplicate membership."""
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    publisher.publish(attempt, files=[_pdf("raport.pdf")], row_count=99)
    first_members = _members(attempt.instance_id)
    first_row = run(
        "SELECT last_published_at, row_count, updated_at FROM portal_generated_report_instances "
        "WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]

    # The EXACT callback, repeated — and then repeated with a different file set,
    # which must equally change nothing: a consumed claim performs no mutation
    # merely to look idempotent.
    assert publisher.publish(attempt, files=[_pdf("raport.pdf")], row_count=99) == attempt.instance_id
    assert publisher.publish(attempt, files=[_pdf("inny.pdf")], row_count=1) == attempt.instance_id

    assert _members(attempt.instance_id) == first_members, _members(attempt.instance_id)
    again = run(
        "SELECT last_published_at, row_count, updated_at FROM portal_generated_report_instances "
        "WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    assert again == first_row, (first_row, again)
    _ok("a duplicate completed callback mutates nothing and duplicates no membership")


def test_a_stale_claim_cannot_publish_or_fail_over_a_newer_attempt() -> None:
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    older = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    newer = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    try:
        publisher.publish(older, files=[_pdf("stary.pdf")])
        raise AssertionError("a superseded attempt must not publish")
    except StaleAttemptError:
        pass
    try:
        publisher.fail(older, safe_error_code="STARY")
        raise AssertionError("a superseded attempt must not fail the instance")
    except StaleAttemptError:
        pass
    assert _members(newer.instance_id) == []
    row = run(
        "SELECT generation_state, safe_error_code, claim_token FROM "
        "portal_generated_report_instances WHERE instance_id = %s",
        (newer.instance_id,),
    )[0]
    assert row["generation_state"] == "running", row
    assert row["safe_error_code"] is None, row
    assert str(row["claim_token"]) == newer.claim_token, row

    # The CURRENT attempt still works, which is what makes the refusals fencing
    # rather than breakage.
    publisher.publish(newer, files=[_pdf("nowy.pdf")])
    assert [m["display_filename"] for m in _members(newer.instance_id)] == ["nowy.pdf"]
    _ok("a stale claim can neither publish nor fail over the current attempt")


def test_a_late_fail_cannot_turn_a_published_report_into_a_failed_one() -> None:
    """The review's finding, exactly: `fail()` after a successful publication."""
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    publisher.publish(attempt, files=[_pdf("raport.pdf")])
    before = run(
        "SELECT generation_state, last_published_at, safe_error_code, library_timestamp "
        "  FROM portal_generated_report_instances WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    try:
        publisher.fail(attempt, safe_error_code="SPOZNIONY_BLAD", safe_error_message="za późno")
        raise AssertionError("a completed successful attempt must not be failed")
    except AttemptAlreadyCompletedError as exc:
        assert exc.outcome == "succeeded", exc.outcome
    after = run(
        "SELECT generation_state, last_published_at, safe_error_code, library_timestamp "
        "  FROM portal_generated_report_instances WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    assert after == before, (before, after)
    assert after["generation_state"] == "succeeded", after

    # And the mirror image: a failed attempt cannot later claim to have published.
    retry = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    publisher.fail(retry, safe_error_code="BLAD_SQL")
    publisher.fail(retry, safe_error_code="BLAD_SQL")          # exact replay: no-op
    try:
        publisher.publish(retry, files=[_pdf("po-bledzie.pdf")])
        raise AssertionError("a completed failed attempt must not publish")
    except AttemptAlreadyCompletedError as exc:
        assert exc.outcome == "failed", exc.outcome
    state = run(
        "SELECT generation_state, last_published_at FROM portal_generated_report_instances "
        "WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    # A published instance stays published through a later failed retry
    # (`docs/40` §3.2) — that behaviour is preserved, not changed.
    assert state["generation_state"] == "failed" and state["last_published_at"] is not None, state
    assert [m["display_filename"] for m in _members(attempt.instance_id)] == ["raport.pdf"]
    _ok("a completed attempt cannot be re-completed with a different outcome")


# ===========================================================================
# 3. REPORT_AVAILABILITY_SUMMARY_INTEGRITY
# ===========================================================================

def _summary(instance_id: str) -> tuple:
    row = run(
        "SELECT published_member_count, available_member_count, available_expires_at "
        "  FROM portal_generated_report_instances WHERE instance_id = %s",
        (instance_id,),
    )[0]
    return (
        int(row["published_member_count"]),
        int(row["available_member_count"]),
        row["available_expires_at"],
    )


def _truth(instance_id: str) -> tuple:
    row = run(
        """
        SELECT count(*)::int AS total,
               count(*) FILTER (WHERE is_available)::int AS available,
               CASE WHEN bool_or(is_available AND expires_at IS NULL) THEN NULL
                    ELSE max(expires_at) FILTER (WHERE is_available) END AS horizon
          FROM portal_generated_report_files WHERE instance_id = %s
        """,
        (instance_id,),
    )[0]
    available = int(row["available"] or 0)
    return int(row["total"] or 0), available, (row["horizon"] if available else None)


def test_the_availability_summary_cannot_be_forged_away_from_membership() -> None:
    """Direct SQL set `available_member_count = 1` and the report read as ready."""
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    soon = datetime.now(timezone.utc) + timedelta(days=30)
    publisher.publish(attempt, files=[
        PublishedFile(
            artifact_id=_new_artifact(name="raport.pdf", size=1024),
            display_filename="raport.pdf", file_format="PDF",
            content_type="application/pdf", size_bytes=1024,
            semantic_role="main_document", is_main_file=True, is_previewable=True,
            expires_at=soon,
        ),
        PublishedFile(
            artifact_id=_new_artifact(name="dane.csv", size=64),
            display_filename="dane.csv", file_format="CSV",
            content_type="text/csv", size_bytes=64, semantic_role="raw_data",
        ),
    ])
    instance = attempt.instance_id

    # (a) available members inserted — the summary is the membership.
    assert _summary(instance) == _truth(instance) == (2, 2, None), _summary(instance)

    # (b) the forgery the review performed, in every shape.
    for statement, values in (
        ("UPDATE portal_generated_report_instances SET available_member_count = 1 "
         "WHERE instance_id = %s", (instance,)),
        ("UPDATE portal_generated_report_instances SET published_member_count = 9, "
         "available_member_count = 9 WHERE instance_id = %s", (instance,)),
        ("UPDATE portal_generated_report_instances SET available_expires_at = now() - interval '1 day' "
         "WHERE instance_id = %s", (instance,)),
    ):
        run(statement, values)
        assert _summary(instance) == _truth(instance), (statement, _summary(instance), _truth(instance))
    assert _summary(instance) == (2, 2, None), _summary(instance)

    # (c) one member expires.
    run(
        "UPDATE portal_generated_report_files SET is_available = FALSE "
        "WHERE instance_id = %s AND file_format = 'CSV'",
        (instance,),
    )
    assert _summary(instance) == _truth(instance) == (2, 1, soon.replace(microsecond=soon.microsecond)), _summary(instance)

    # (d) the backing artifact is removed: the FK nulls the reference, the member
    #     survives, availability follows, and the summary follows availability.
    run(
        "DELETE FROM artifacts WHERE artifact_id IN ("
        "  SELECT artifact_id FROM portal_generated_report_files "
        "   WHERE instance_id = %s AND artifact_id IS NOT NULL)",
        (instance,),
    )
    assert _summary(instance) == _truth(instance) == (2, 0, None), _summary(instance)
    run(
        "UPDATE portal_generated_report_instances SET available_member_count = 1 WHERE instance_id = %s",
        (instance,),
    )
    assert _summary(instance) == (2, 0, None), _summary(instance)

    # (e) the read path agrees: a forged summary cannot render `Gotowy`.
    status = run(
        """
        SELECT
          CASE
            WHEN i.last_published_at IS NOT NULL AND i.available_member_count > 0
                 AND (i.available_expires_at IS NULL OR i.available_expires_at > now())
              THEN 'ready'
            WHEN i.last_published_at IS NOT NULL THEN 'expired'
            WHEN i.generation_state = 'failed' THEN 'failed'
            ELSE 'generating' END AS status
          FROM portal_generated_report_instances i WHERE i.instance_id = %s
        """,
        (instance,),
    )[0]["status"]
    assert status == "expired", status

    # (f) a member replacement during publication keeps the summary exact.
    retry = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    publisher.publish(retry, files=[_pdf("nowy-raport.pdf", size=2048)])
    assert _summary(instance) == _truth(instance) == (1, 1, None), _summary(instance)

    # (g) a rolled-back forgery leaves nothing behind.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal_generated_report_instances SET available_member_count = 7 "
                "WHERE instance_id = %s",
                (instance,),
            )
            conn.rollback()
    assert _summary(instance) == _truth(instance) == (1, 1, None), _summary(instance)
    _ok("the availability summary is a projection of membership and cannot be forged")


# ===========================================================================
# 4. REPORT_PREVIEW_DELIVERY_SAFETY
# ===========================================================================

def _resolve(instance_ref: str, member_ref: str, *, user=None, for_preview: bool = False):
    service = base._service()
    who = user or _user(ALICE)
    context = service.client_context(who)
    return service.resolve_file(who, context, instance_ref, member_ref, for_preview=for_preview)


def test_presentation_metadata_cannot_make_content_previewable() -> None:
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    publisher.publish(attempt, files=[
        _pdf("raport.pdf", size=4096),
        PublishedFile(
            artifact_id=_new_artifact(name="dane.csv", size=512),
            display_filename="dane.csv", file_format="CSV", content_type="text/csv",
            size_bytes=512, semantic_role="raw_data",
            # A caller ASKING for an inline CSV. It is not refused as an error —
            # it is simply not granted, because previewability is not a claim.
            is_previewable=True,
        ),
    ])
    instance = attempt.instance_id
    ref = f"gen-{instance}"

    stored = {
        row["file_format"]: row for row in run(
            "SELECT file_format, content_type, is_previewable, size_bytes "
            "  FROM portal_generated_report_files WHERE instance_id = %s",
            (instance,),
        )
    }
    assert stored["CSV"]["is_previewable"] is False, stored["CSV"]
    assert stored["PDF"]["is_previewable"] is True, stored["PDF"]

    members = {
        row["file_format"]: str(row["member_id"]) for row in run(
            "SELECT member_id, file_format FROM portal_generated_report_files WHERE instance_id = %s",
            (instance,),
        )
    }

    # (a) a valid PDF previews.
    resolved = _resolve(ref, members["PDF"], for_preview=True)
    assert resolved.is_previewable and resolved.content_type == "application/pdf", resolved

    # (b) a non-previewable member is refused an inline representation, and its
    #     DOWNLOAD still works — nothing is missing, only the embedding.
    try:
        _resolve(ref, members["CSV"], for_preview=True)
        raise AssertionError("a CSV member must not be served inline")
    except ReportFilePreviewUnsupportedError:
        pass
    assert _resolve(ref, members["CSV"]).display_filename == "dane.csv"

    # (c) publication refuses an HTML payload labelled as a PDF outright.
    retry = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    html_artifact = _new_artifact(name="zlosliwy.html", size=256)
    try:
        publisher.publish(retry, files=[PublishedFile(
            artifact_id=html_artifact, display_filename="raport.pdf", file_format="PDF",
            content_type="application/pdf", size_bytes=256,
            semantic_role="main_document", is_main_file=True, is_previewable=True,
        )])
        raise AssertionError("an HTML object must not publish as a PDF member")
    except Exception as exc:  # noqa: BLE001 - a named publication refusal
        assert "text/html" in str(exc), exc
    publisher.publish(retry, files=[_pdf("raport.pdf", size=4096)])

    # (d) the DATABASE refuses the inconsistent member too, so a writer that
    #     bypasses the boundary gains nothing.
    assert _expect_error(
        "INSERT INTO portal_generated_report_files "
        "(instance_id, artifact_id, display_filename, file_format, content_type, size_bytes, "
        " semantic_role, is_main_file, is_previewable) "
        "VALUES (%s,%s,'podszywka.pdf','PDF','text/html',10,'raw_data',false,true)",
        (instance, html_artifact),
    ) == "23514"
    assert _expect_error(
        "INSERT INTO portal_generated_report_files "
        "(instance_id, artifact_id, display_filename, file_format, content_type, size_bytes, "
        " semantic_role, is_main_file, is_previewable) "
        "VALUES (%s,%s,'dane2.csv','CSV','text/csv',10,'raw_data',false,true)",
        (instance, html_artifact),
    ) == "23514"

    # (e) THE INDEPENDENT LAYER. A member that is internally consistent — `PDF`,
    #     `application/pdf`, previewable — over an artifact whose OWN content type
    #     is `text/html`. Only the delivery path can catch this one, and it does:
    #     the served content type comes from the artifact, never from the member.
    forged_instance = run(
        "SELECT instance_id FROM portal_generated_report_instances WHERE instance_id = %s",
        (instance,),
    )[0]["instance_id"]
    run(
        "UPDATE portal_generated_report_files SET artifact_id = %s "
        "WHERE instance_id = %s AND file_format = 'PDF'",
        (html_artifact, forged_instance),
    )
    forged_member = run(
        "SELECT member_id FROM portal_generated_report_files "
        "WHERE instance_id = %s AND file_format = 'PDF'",
        (forged_instance,),
    )[0]["member_id"]
    try:
        _resolve(ref, str(forged_member), for_preview=True)
        raise AssertionError("bytes that are text/html must never be embedded")
    except ReportFilePreviewUnsupportedError:
        pass
    downloaded = _resolve(ref, str(forged_member))
    assert downloaded.content_type == "text/html", downloaded
    assert downloaded.is_previewable is False, downloaded
    _ok("only genuine PDF bytes are embedded; a member cannot relabel what it points at")


def test_the_preview_route_and_the_inline_response_enforce_it_too() -> None:
    """`RP-14` at the route, and `nosniff` on the wire."""
    _reset()
    _seed()
    publisher = base._publisher()
    period = canonical_period("week", date(2026, 7, 8))
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    publisher.publish(attempt, files=[
        _pdf("raport.pdf"),
        PublishedFile(
            artifact_id=_new_artifact(name="dane.csv", size=512),
            display_filename="dane.csv", file_format="CSV", content_type="text/csv",
            size_bytes=512, semantic_role="raw_data",
        ),
    ])
    ref = f"gen-{attempt.instance_id}"
    members = {
        row["file_format"]: str(row["member_id"]) for row in run(
            "SELECT member_id, file_format FROM portal_generated_report_files WHERE instance_id = %s",
            (attempt.instance_id,),
        )
    }
    pages = base._pages()

    resolved, denied = pages.resolve_file(
        user=_user(ALICE), instance_ref=ref, member_ref=members["CSV"], for_preview=True
    )
    assert resolved is None and denied is not None
    assert denied.status_code == 415, denied.status_code
    assert "REPORT_FILE_NOT_PREVIEWABLE" in denied.body_html
    assert "storage_key" not in denied.body_html and "minio" not in denied.body_html.lower()

    # An unknown member, a foreign instance and an expired member each keep their
    # own already-accepted answer; the preview gate adds no new leak.
    _resolved, foreign = pages.resolve_file(
        user=_user(FRANK), instance_ref=ref, member_ref=members["PDF"], for_preview=True
    )
    assert _resolved is None and foreign is not None and foreign.status_code in (403, 404), foreign.status_code

    # An expired member: the file state, not the preview state.
    run(
        "UPDATE portal_generated_report_files SET is_available = FALSE WHERE instance_id = %s",
        (attempt.instance_id,),
    )
    _resolved, expired = pages.resolve_file(
        user=_user(ALICE), instance_ref=ref, member_ref=members["PDF"], for_preview=True
    )
    assert _resolved is None and expired.status_code == 410, expired.status_code

    # The served inline response tells the browser exactly one thing.
    _reset()
    _seed()
    publisher = base._publisher()
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    publisher.publish(attempt, files=[_pdf("raport.pdf")])
    member = run(
        "SELECT member_id FROM portal_generated_report_files WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]["member_id"]
    resolved = _resolve(f"gen-{attempt.instance_id}", str(member), for_preview=True)
    with _fake_s3({resolved.artifact_row["storage_key"]: b"%PDF-1.7 ..."}):
        response = api_main._serve_report_file(
            resolved, disposition="inline", user=_user(ALICE)
        )
    assert response.media_type == "application/pdf", response.media_type
    assert response.headers.get("X-Content-Type-Options") == "nosniff", response.headers
    assert "inline;" in response.headers.get("Content-Disposition", ""), response.headers
    assert INLINE_PREVIEW_CONTENT_TYPES == frozenset({"application/pdf"})
    _ok("the preview route enforces previewability independently and serves nosniff PDFs only")


# ===========================================================================
# 5. REPORT_BULK_ARCHIVE_BOUND
# ===========================================================================

class _FakeBody:
    """An object body that yields `total` bytes without ever holding them all."""

    def __init__(self, total: int, chunk: int = 64 * 1024) -> None:
        self._left = int(total)
        self._chunk = chunk

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        take = self._left if size is None or size < 0 else min(size, self._left)
        take = min(take, self._chunk)
        self._left -= take
        return b"\0" * take


class _FakeS3:
    """Objects with a REAL size that may disagree with any metadata about them."""

    def __init__(self, objects: dict, *, head_sizes: dict | None = None, missing=()) -> None:
        self.objects = dict(objects)
        self.head_sizes = dict(head_sizes or {})
        self.missing = set(missing)

    def _size(self, key: str) -> int:
        value = self.objects[key]
        return value if isinstance(value, int) else len(value)

    def head_object(self, *, Bucket, Key):  # noqa: N803 - boto3's signature
        if Key in self.missing or Key not in self.objects:
            raise RuntimeError("no such object")
        return {"ContentLength": self.head_sizes.get(Key, self._size(Key))}

    def get_object(self, *, Bucket, Key):  # noqa: N803 - boto3's signature
        if Key in self.missing or Key not in self.objects:
            raise RuntimeError("no such object")
        value = self.objects[Key]
        body = _FakeBody(value) if isinstance(value, int) else _FakeBody(len(value))
        if isinstance(value, bytes):
            body = _RealBody(value)
        return {"Body": body}


class _RealBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk, self._at = self._data[self._at:], len(self._data)
            return chunk
        chunk = self._data[self._at:self._at + size]
        self._at += len(chunk)
        return chunk


class _fake_s3:  # noqa: N801 - a context manager, used as one
    def __init__(self, objects, *, head_sizes=None, missing=(), limit=None) -> None:
        self._fake = _FakeS3(objects, head_sizes=head_sizes, missing=missing)
        self._limit = limit
        self._saved = None
        self._saved_limit = None

    def __enter__(self):
        self._saved = api_main.s3
        api_main.s3 = self._fake
        if self._limit is not None:
            self._saved_limit = api_main.REPORT_ARCHIVE_MAX_TOTAL_BYTES
            api_main.REPORT_ARCHIVE_MAX_TOTAL_BYTES = self._limit
        return self._fake

    def __exit__(self, *exc):
        api_main.s3 = self._saved
        if self._saved_limit is not None:
            api_main.REPORT_ARCHIVE_MAX_TOTAL_BYTES = self._saved_limit
        return False


def _archive(user, instance_ref, *, objects, head_sizes=None, missing=(), limit=None):
    pages = base._pages()
    instance, resolved, denied = pages.resolve_all_files(user=user, instance_ref=instance_ref)
    if denied is not None:
        return None, denied
    with _fake_s3(objects, head_sizes=head_sizes, missing=missing, limit=limit):
        return resolved, api_main._serve_report_archive(instance, resolved, user=user)


def _publish_archive_fixture(publisher, *, names, sizes=None, client=CLIENT):
    period = canonical_period("week", date(2026, 7, 8))
    attempt = publisher.begin_generation(
        client_code=client, type_key="raport_otwarty", period=period,
        display_name="Tydzień 28 · 2026",
    )
    sizes = sizes or [1024] * len(names)
    files = []
    for index, (name, size) in enumerate(zip(names, sizes)):
        files.append(PublishedFile(
            artifact_id=_new_artifact(client_code=client, name=f"obj-{index}.pdf", size=size),
            display_filename=name, file_format="PDF", content_type="application/pdf",
            size_bytes=size, semantic_role="main_document" if index == 0 else "raw_data",
            is_main_file=(index == 0), is_previewable=(index == 0), display_order=index,
        ))
    publisher.publish(attempt, files=files, source=None)
    return attempt


def test_the_archive_bound_uses_real_bytes_not_declared_sizes() -> None:
    _reset()
    _seed()
    publisher = base._publisher()

    # (a) THE REVIEW'S CASE. The member and the head metadata both say "tiny";
    #     the object is not. The declared numbers pass the pre-flight, and the
    #     incremental bound is what actually refuses — with the read stopped, not
    #     completed.
    attempt = _publish_archive_fixture(publisher, names=["a.pdf", "b.pdf"], sizes=[8, 8])
    keys = [
        row["storage_key"] for row in run(
            "SELECT a.storage_key FROM portal_generated_report_files f "
            "  JOIN artifacts a ON a.artifact_id = f.artifact_id "
            " WHERE f.instance_id = %s ORDER BY f.display_order",
            (attempt.instance_id,),
        )
    ]
    _resolved, response = _archive(
        _user(ALICE), f"gen-{attempt.instance_id}",
        objects={keys[0]: 8, keys[1]: 40_000},        # the real object is huge
        head_sizes={keys[0]: 8, keys[1]: 8},          # every declaration says tiny
        limit=16_384,
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 413, response.status_code
    assert "REPORT_ARCHIVE_TOO_LARGE" in body, body[:400]
    assert "Pobierz pliki pojedynczo" in body
    # TRUTHFUL: the storage is not blamed for a size limit.
    assert "Magazyn plików jest niedostępny" not in body

    # (b) honest metadata crossing the bound is refused BEFORE any body is read.
    class _NoGet(_FakeS3):
        def get_object(self, *, Bucket, Key):  # noqa: N803
            raise AssertionError("no object may be read once the bound is known to fail")

    pages = base._pages()
    instance, resolved, denied = pages.resolve_all_files(
        user=_user(ALICE), instance_ref=f"gen-{attempt.instance_id}"
    )
    assert denied is None
    saved_s3, saved_limit = api_main.s3, api_main.REPORT_ARCHIVE_MAX_TOTAL_BYTES
    try:
        api_main.s3 = _NoGet({keys[0]: 10_000, keys[1]: 10_000})
        api_main.REPORT_ARCHIVE_MAX_TOTAL_BYTES = 16_384
        refused = api_main._serve_report_archive(instance, resolved, user=_user(ALICE))
    finally:
        api_main.s3, api_main.REPORT_ARCHIVE_MAX_TOTAL_BYTES = saved_s3, saved_limit
    assert refused.status_code == 413, refused.status_code

    # (c) the product policy itself is unchanged.
    assert ARCHIVE_MAX_TOTAL_BYTES == 256 * 1024 * 1024
    assert api_main.REPORT_ARCHIVE_MAX_TOTAL_BYTES == 256 * 1024 * 1024
    _ok("the archive bound is enforced on authoritative and then on real bytes")


def test_archive_entry_names_are_safe_deterministic_and_authorized() -> None:
    import io
    import zipfile

    # Pure behaviour first, so the rules are stated once and unambiguously.
    assert safe_archive_name("../../etc/passwd") == "passwd"
    assert safe_archive_name("..\\..\\windows\\system32") == "system32"
    assert safe_archive_name("/absolute/raport.pdf") == "raport.pdf"
    assert safe_archive_name("..") == "plik"
    assert safe_archive_name("") == "plik"
    assert safe_archive_name("raport\x00.pdf") == "raport_.pdf"
    assert unique_archive_names(["a.pdf", "a.pdf", "a.pdf"]) == ["a.pdf", "a (2).pdf", "a (3).pdf"]
    assert unique_archive_names(["r", "r"]) == ["r", "r (2)"]

    _reset()
    _seed()
    publisher = base._publisher()

    # Two identical names are refused at the boundary, before the archive is
    # ever reached.
    try:
        _publish_archive_fixture(publisher, names=["raport.pdf", "raport.pdf"])
        raise AssertionError("two members must not share a display filename")
    except Exception as exc:  # noqa: BLE001 - a named publication refusal
        assert "share the display filename" in str(exc), exc

    # Within one instance, two IDENTICAL display filenames cannot exist: the
    # publication boundary refuses them and `UNIQUE (instance_id,
    # display_filename)` refuses them again. The collision that IS reachable is
    # two DISTINCT stored names that sanitise to one entry name, and it must
    # resolve deterministically rather than silently dropping a file.
    attempt = _publish_archive_fixture(publisher, names=["raport:1.pdf", "raport*1.pdf"])
    assert _expect_error(
        "UPDATE portal_generated_report_files SET display_filename = %s WHERE instance_id = %s",
        ("../../../etc/passwd", attempt.instance_id),
    ) == "23514"

    keys = [
        row["storage_key"] for row in run(
            "SELECT a.storage_key FROM portal_generated_report_files f "
            "  JOIN artifacts a ON a.artifact_id = f.artifact_id "
            " WHERE f.instance_id = %s ORDER BY f.display_order",
            (attempt.instance_id,),
        )
    ]
    assert len(keys) == 2, keys
    _resolved, response = _archive(
        _user(ALICE), f"gen-{attempt.instance_id}",
        objects={keys[0]: b"pierwszy", keys[1]: b"drugi"},
    )
    assert response.status_code == 200, getattr(response, "status_code", None)
    payload = b"".join(response.body)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["raport_1.pdf", "raport_1 (2).pdf"], archive.namelist()
        assert archive.read("raport_1.pdf") == b"pierwszy"
        assert archive.read("raport_1 (2).pdf") == b"drugi"
    _ok("archive entry names are path-safe, de-duplicated deterministically and complete")


def test_an_unavailable_or_foreign_member_never_enters_the_archive() -> None:
    import io
    import zipfile

    _reset()
    _seed()
    publisher = base._publisher()
    attempt = _publish_archive_fixture(publisher, names=["a.pdf", "b.pdf", "c.pdf"])
    keys = {
        row["display_filename"]: row["storage_key"] for row in run(
            "SELECT f.display_filename, a.storage_key FROM portal_generated_report_files f "
            "  JOIN artifacts a ON a.artifact_id = f.artifact_id WHERE f.instance_id = %s",
            (attempt.instance_id,),
        )
    }
    # One member expires; one is re-pointed at ANOTHER client's stored object,
    # which the delivery join refuses even though the foreign key is valid.
    run(
        "UPDATE portal_generated_report_files SET is_available = FALSE "
        "WHERE instance_id = %s AND display_filename = 'b.pdf'",
        (attempt.instance_id,),
    )
    foreign = _new_artifact(client_code=OTHER_CLIENT, name="obcy.pdf", size=32)
    run(
        "UPDATE portal_generated_report_files SET artifact_id = %s "
        "WHERE instance_id = %s AND display_filename = 'c.pdf'",
        (foreign, attempt.instance_id),
    )
    foreign_key = run("SELECT storage_key FROM artifacts WHERE artifact_id = %s", (foreign,))[0]["storage_key"]

    resolved, response = _archive(
        _user(ALICE), f"gen-{attempt.instance_id}",
        objects={keys["a.pdf"]: b"jedyny", keys["b.pdf"]: b"wygasly", foreign_key: b"obcy"},
    )
    assert [r.display_filename for r in resolved] == ["a.pdf"], [r.display_filename for r in resolved]
    payload = b"".join(response.body)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["a.pdf"], archive.namelist()

    # A storage failure mid-build is a state, never a partial ZIP.
    _resolved, broken = _archive(
        _user(ALICE), f"gen-{attempt.instance_id}", objects={}, missing={keys["a.pdf"]}
    )
    assert broken.status_code == 503, broken.status_code
    assert "REPORT_FILE_STORE_UNAVAILABLE" in broken.body.decode("utf-8")
    _ok("expired, foreign and unreadable members never reach the archive")


# ===========================================================================
# 6. REPORT_SOURCE_PROVENANCE_INTEGRITY
# ===========================================================================

def _begin(publisher, *, type_key="raport_207", week=date(2026, 7, 8), client=CLIENT):
    period = canonical_period("week", week)
    return publisher.begin_generation(
        client_code=client, type_key=type_key, period=period,
        display_name=f"Tydzień {period.key}",
    ), period


def test_persisted_provenance_cannot_name_another_client_dataset_or_range() -> None:
    from api.report_explorer.publication import SourceSnapshot

    _reset()
    _seed()
    publisher = base._publisher()
    good = dict(dataset_slug="trips", dataset_name="Przejazdy", date_column="trip_start")

    def _try(snapshot, *, week=date(2026, 7, 8), period_from=None, period_to=None):
        attempt, period = _begin(publisher, week=week)
        return publisher.publish(
            attempt, files=[_pdf(f"raport-{period.key}.pdf")], row_count=1,
            source=snapshot,
            period_from=period_from if period_from is not None else period.start,
            period_to_exclusive=(
                period_to if period_to is not None else period.end + timedelta(days=1)
            ),
        )

    def _refused(snapshot, fragment, **kwargs) -> None:
        try:
            _try(snapshot, **kwargs)
            raise AssertionError(f"expected a provenance refusal for {fragment}")
        except ReportProvenanceError as exc:
            assert fragment in str(exc), (fragment, str(exc))

    # (a) the correct binding is stored, with the CATALOGUE's dataset name.
    instance_id = _try(SourceSnapshot(dataset_id=DATASET_ACME, **{**good, "dataset_name": "Kłamstwo"}))
    stored = run(
        "SELECT source_dataset_id, source_provenance_json FROM portal_generated_report_instances "
        "WHERE instance_id = %s",
        (instance_id,),
    )[0]
    snapshot = stored["source_provenance_json"]
    assert str(stored["source_dataset_id"]) == DATASET_ACME, stored
    assert snapshot["dataset_slug"] == "trips", snapshot
    assert snapshot["dataset_name"] == "Przejazdy", snapshot
    assert snapshot["date_column"] == "trip_start", snapshot
    assert snapshot["applied_from"] == "2026-07-06", snapshot
    assert snapshot["applied_to_exclusive"] == "2026-07-13", snapshot

    # (b) another client's dataset id.
    _refused(SourceSnapshot(dataset_id=DATASET_OTHER, **good), "does not resolve to this client")
    # (c) a slug the definition does not declare.
    _refused(
        SourceSnapshot(dataset_id=DATASET_ACME, dataset_slug="pojazdy",
                       dataset_name="Pojazdy", date_column="trip_start"),
        "reads dataset 'trips'",
    )
    # (d) a date column that is not in the catalogue at all.
    _refused(
        SourceSnapshot(dataset_id=DATASET_ACME, dataset_slug="trips",
                       dataset_name="Przejazdy", date_column="DROP TABLE"),
        "filters on 'trip_start'",
    )
    # (e) a column that IS catalogued but is not this definition's — allowed
    #     elsewhere, wrong here.
    _refused(
        SourceSnapshot(dataset_id=DATASET_ACME, dataset_slug="trips",
                       dataset_name="Przejazdy", date_column="trip_end"),
        "filters on 'trip_start'",
    )
    # (f) a range that is not this instance's reporting period.
    _refused(
        SourceSnapshot(dataset_id=DATASET_ACME, **good), "is not this report's reporting period",
        week=date(2026, 8, 5), period_from=date(2020, 1, 1),
    )
    _refused(
        SourceSnapshot(dataset_id=DATASET_ACME, **good), "is not this report's reporting period",
        week=date(2026, 8, 12), period_to=date(2099, 1, 1),
    )
    # (g) a type that declares NO binding cannot record provenance at all.
    attempt, period = _begin(publisher, type_key="raport_otwarty", week=date(2026, 9, 7))
    try:
        publisher.publish(
            attempt, files=[_pdf("otwarty.pdf")],
            source=SourceSnapshot(dataset_id=DATASET_ACME, **good),
            period_from=period.start, period_to_exclusive=period.end + timedelta(days=1),
        )
        raise AssertionError("a type with no source binding must not record provenance")
    except ReportProvenanceError as exc:
        assert "declares no source-data binding" in str(exc), exc

    # (h) a dataset column catalogue that loses the column later does not falsify
    #     what was already stored; the read path degrades on ACCESS, not on text.
    row = run(
        "SELECT source_provenance_json FROM portal_generated_report_instances WHERE instance_id = %s",
        (instance_id,),
    )[0]
    assert row["source_provenance_json"]["date_column"] == "trip_start"
    _ok("persisted RP-18 provenance names this client, this dataset, this column and this period")


def test_publication_refuses_a_dataset_the_catalogue_no_longer_offers() -> None:
    """`REPORT_SOURCE_DATASET_BINDING`: provenance must name a CURRENT dataset.

    The focused review probed this path against a real database and got
    `INACTIVE_DATASET_ACCEPTED=True`: every other binding rule held, but a
    successful publication could still persist provenance naming a dataset
    already withdrawn from the Database Explorer catalogue. The correction adds
    the catalogue's own `is_active IS TRUE` predicate to the publication-time
    resolution — the same one `_get_portal_database_dataset_for_user` enforces
    at click time.

    The rule binds NEW publications only. A report published while its dataset
    was active is history, and history is not rewritten by a later deactivation.
    """
    from api.report_explorer.publication import SourceSnapshot

    _reset()
    _seed()
    publisher = base._publisher()
    good = SourceSnapshot(dataset_id=DATASET_ACME, dataset_slug="trips",
                          dataset_name="Przejazdy", date_column="trip_start")

    def _publish(week: date) -> str:
        attempt, period = _begin(publisher, week=week)
        return publisher.publish(
            attempt, files=[_pdf(f"raport-{period.key}.pdf")], row_count=7,
            source=good, period_from=period.start,
            period_to_exclusive=period.end + timedelta(days=1),
        )

    def _active(state: bool) -> None:
        run("UPDATE portal_database_datasets SET is_active = %s WHERE dataset_id = %s",
            (state, DATASET_ACME))

    def _provenance(instance_id: str) -> dict:
        return run(
            "SELECT source_dataset_id, source_provenance_json, generation_state, row_count "
            "FROM portal_generated_report_instances WHERE instance_id = %s",
            (instance_id,),
        )[0]

    # (a) ACTIVE: the correctly bound, currently active dataset is accepted.
    historical = _publish(date(2026, 7, 8))
    stored = _provenance(historical)
    assert str(stored["source_dataset_id"]) == DATASET_ACME, stored
    assert stored["source_provenance_json"]["dataset_slug"] == "trips", stored
    assert stored["generation_state"] == "succeeded", stored

    # (b) INACTIVE: the SAME otherwise-valid publication is refused. Nothing but
    #     `is_active` changed, so the predicate is the only thing under test.
    _active(False)
    attempt, period = _begin(publisher, week=date(2026, 7, 15))
    try:
        publisher.publish(
            attempt, files=[_pdf(f"raport-{period.key}.pdf")], row_count=7,
            source=good, period_from=period.start,
            period_to_exclusive=period.end + timedelta(days=1),
        )
        raise AssertionError("an inactive dataset must not become authoritative provenance")
    except ReportProvenanceError as exc:
        # The refusal stays indistinguishable from "no such dataset for this
        # client": the publication boundary is not an existence oracle.
        assert "does not resolve to this client's active" in str(exc), exc

    # The refused attempt acquired NO provenance and did not publish: the whole
    # transaction rolled back, so the instance is still an unfinished attempt.
    refused = _provenance(attempt.instance_id)
    assert refused["source_dataset_id"] is None, refused
    assert not (refused["source_provenance_json"] or {}), refused
    assert refused["generation_state"] != "succeeded", refused
    assert not run(
        "SELECT 1 FROM portal_generated_report_files WHERE instance_id = %s",
        (attempt.instance_id,),
    ), "a refused publication must leave no members"

    # (c) HISTORY IS NOT DESTROYED. The report published in (a) while the dataset
    #     was active survives the deactivation intact: same instance, same state,
    #     same files, same persisted provenance, unrewritten.
    survivor = _provenance(historical)
    assert str(survivor["source_dataset_id"]) == DATASET_ACME, survivor
    assert survivor["source_provenance_json"] == stored["source_provenance_json"], survivor
    assert survivor["generation_state"] == "succeeded", survivor
    assert survivor["row_count"] == 7, survivor
    assert run("SELECT 1 FROM portal_generated_report_files WHERE instance_id = %s",
               (historical,)), "deactivation must not delete a published report's files"

    # ...and its source-data ACTION follows the CURRENT catalogue, which no
    # longer offers this dataset: absent, never a link to an unavailable dataset.
    service = base._service()
    ref = f"gen-{historical}"
    detail = service.detail(_user(ALICE), service.client_context(_user(ALICE)), ref)
    assert detail.source_url is None, detail.source_url
    assert detail.source_dataset_name == "", detail.source_dataset_name
    page = base._pages().detail(user=_user(ALICE), instance_ref=ref)
    assert "/user/database/datasets/" not in page.body_html
    # The report itself is still fully readable — degraded navigation, not a
    # degraded report.
    assert page.status_code == 200, page.status_code
    assert detail.instance.status == "ready", detail.instance.status
    assert detail.instance.files, "the historical report keeps its files"

    # (d) REACTIVATION: the exact same dataset, active again, publishes again.
    #     Only `is_active` moved, so nothing else can explain (b).
    _active(True)
    republished = _publish(date(2026, 7, 22))
    reborn = _provenance(republished)
    assert str(reborn["source_dataset_id"]) == DATASET_ACME, reborn
    assert reborn["generation_state"] == "succeeded", reborn
    _ok("publication binds provenance to a CURRENTLY ACTIVE dataset, without rewriting history")


def test_rp18_navigation_still_reauthorizes_database_explorer_independently() -> None:
    """Report access is not database access — before and after the correction."""
    from api.report_explorer.publication import SourceSnapshot

    _reset()
    _seed()
    publisher = base._publisher()
    attempt, period = _begin(publisher)
    publisher.publish(
        attempt, files=[_pdf("raport.pdf")], row_count=4118,
        source=SourceSnapshot(dataset_id=DATASET_ACME, dataset_slug="trips",
                              dataset_name="Przejazdy", date_column="trip_start"),
        period_from=period.start, period_to_exclusive=period.end + timedelta(days=1),
    )
    ref = f"gen-{attempt.instance_id}"
    service = base._service()

    # ALICE holds the dataset grant: the action is present and carries the
    # report's own period as the filter.
    context = service.client_context(_user(ALICE))
    detail = service.detail(_user(ALICE), context, ref)
    assert detail.source_url and DATASET_ACME in detail.source_url, detail.source_url
    assert "date_from__trip_start=2026-07-06" in detail.source_url, detail.source_url
    assert "date_to__trip_start=2026-07-13" in detail.source_url, detail.source_url

    # A report viewer WITHOUT the Database Explorer grant gets no action and no
    # dataset metadata, from the same stored provenance.
    run("DELETE FROM portal_database_dataset_users WHERE user_id = %s AND dataset_id = %s",
        (ALICE, DATASET_ACME))
    revoked = service.detail(_user(ALICE), service.client_context(_user(ALICE)), ref)
    assert revoked.source_url is None, revoked.source_url
    assert revoked.source_dataset_name == "", revoked.source_dataset_name
    page = base._pages().detail(user=_user(ALICE), instance_ref=ref)
    assert "/user/database/datasets/" not in page.body_html
    _ok("RP-18 navigation reauthorizes Database Explorer at click time, independently")


# ===========================================================================
# 7. REPORT_FILE_CONTRACT_ENFORCEMENT
# ===========================================================================

SINGLE = DefinitionSpec(
    type_key="raport_jednoplikowy",
    display_name="Raport jednoplikowy",
    cadence_class="monthly",
    period_kind="month",
    generation_definition_ref="jobs.reports.single",
    file_contract=({"role": "main_document", "format": "PDF", "main": True},),
)


def test_the_declared_file_contract_is_validated_and_enforced() -> None:
    _reset()
    _seed()
    publisher = base._publisher()
    publisher.register_definition(SINGLE)

    # (a) the DECLARATION itself is finite and validated.
    for bad, fragment in (
        (({"role": "nieznana", "format": "PDF", "main": True},), "unknown declared file role"),
        (({"role": "main_document", "format": "DOCX", "main": True},), "unknown declared file format"),
        (({"role": "main_document", "format": "PDF"},), "exactly one main file"),
        (({"role": "main_document", "format": "PDF", "main": True},
          {"role": "detailed_data", "format": "XLSX", "main": True}), "exactly one main file"),
        (({"role": "main_document", "format": "PDF", "main": True},
          {"role": "main_document", "format": "PDF"}), "declares main_document/PDF twice"),
        (({"role": "main_document", "format": "PDF", "main": True, "kolor": "red"},), "unknown file contract key"),
        (({"role": "main_document", "format": "PDF", "main": True, "required": False},), "cannot be optional"),
    ):
        try:
            normalize_contract(bad)
            raise AssertionError(f"expected {fragment}")
        except ReportFileContractError as exc:
            assert fragment in str(exc), (fragment, str(exc))
    assert normalize_contract(()) == ()

    def _publish(files, *, type_key="raport_207", week=date(2026, 7, 8)):
        attempt, _period = _begin(publisher, type_key=type_key, week=week)
        publisher.publish(attempt, files=files, source=None)
        return attempt

    def _refused(files, fragment, **kwargs) -> None:
        try:
            _publish(files, **kwargs)
            raise AssertionError(f"expected a contract refusal for {fragment}")
        except ReportFileContractError as exc:
            assert fragment in str(exc), (fragment, str(exc))

    def _member(name, fmt, role, *, main=False, size=1024):
        return PublishedFile(
            artifact_id=_new_artifact(name=name, size=size), display_filename=name,
            file_format=fmt, content_type={
                "PDF": "application/pdf", "CSV": "text/csv", "TXT": "text/plain",
                "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }[fmt], size_bytes=size, semantic_role=role, is_main_file=main,
        )

    # (b) a valid single-file publication of a single-file definition.
    month = canonical_period("month", date(2026, 7, 15))
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_jednoplikowy", period=month,
        display_name="Lipiec 2026",
    )
    publisher.publish(attempt, files=[_member("m.pdf", "PDF", "main_document", main=True)], source=None)
    assert len(_members(attempt.instance_id)) == 1

    # (c) a valid multi-file publication of the multi-file definition.
    ok = _publish([
        _member("a.pdf", "PDF", "main_document", main=True),
        _member("b.xlsx", "XLSX", "detailed_data"),
        _member("c.csv", "CSV", "raw_data"),
    ])
    assert len(_members(ok.instance_id)) == 3

    # (d) a required member is missing, and an entirely undeclared one arrives
    #     in its place.
    month2 = canonical_period("month", date(2026, 8, 15))
    single_attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_jednoplikowy", period=month2,
        display_name="Sierpień 2026",
    )
    try:
        publisher.publish(
            single_attempt,
            files=[_member("tylko.csv", "CSV", "raw_data", main=True)], source=None,
        )
        raise AssertionError("a publication with no declared main document must be refused")
    except ReportFileContractError as exc:
        assert "does not declare raw_data/CSV" in str(exc), exc

    # (e) a wrong ROLE for a declared format.
    _refused(
        [_member("a.pdf", "PDF", "raw_data", main=True)],
        "does not declare raw_data/PDF", week=date(2026, 7, 15),
    )
    # (f) a wrong FORMAT for a declared role.
    _refused(
        [_member("a.txt", "TXT", "main_document", main=True)],
        "does not declare main_document/TXT", week=date(2026, 7, 22),
    )
    # (g) an undeclared extra member the contract forbids.
    _refused(
        [_member("a.pdf", "PDF", "main_document", main=True),
         _member("extra.txt", "TXT", "detailed_data")],
        "does not declare detailed_data/TXT", week=date(2026, 7, 29),
    )
    # (h) multiple main files, and no main file at all.
    for files, fragment in (
        ([_member("a.pdf", "PDF", "main_document", main=True),
          _member("b.xlsx", "XLSX", "detailed_data", main=True)], "exactly one explicit main file"),
        ([_member("a.pdf", "PDF", "main_document"),
          _member("b.xlsx", "XLSX", "detailed_data")], "exactly one explicit main file"),
    ):
        attempt, _period = _begin(publisher, week=date(2026, 8, 5))
        try:
            publisher.publish(attempt, files=files, source=None)
            raise AssertionError(f"expected {fragment}")
        except Exception as exc:  # noqa: BLE001 - the member validator names it
            assert fragment in str(exc), (fragment, str(exc))

    # (i) the MAIN file must be the declared main output, not merely flagged.
    _refused(
        [_member("a.xlsx", "XLSX", "detailed_data", main=True),
         _member("b.pdf", "PDF", "main_document")],
        "declares its main file as main_document/PDF", week=date(2026, 8, 12),
    )

    # (j) a definition that declares nothing constrains nothing.
    open_attempt, _period = _begin(publisher, type_key="raport_otwarty", week=date(2026, 8, 19))
    publisher.publish(open_attempt, files=[_member("cokolwiek.csv", "CSV", "raw_data", main=True)],
                      source=None)
    assert len(_members(open_attempt.instance_id)) == 1
    _ok("the declared file contract is validated once and proven at every publication")


# ===========================================================================
# 8. REPORT_LIBRARY_COMPLETE_PAGINATION
# ===========================================================================

GENERATED_POPULATION = 5_200      # above the former 5,000 generated ceiling
EXPORT_POPULATION = 620           # above the former 500 export ceiling


def _bulk_population() -> tuple[int, int]:
    """A population larger than BOTH former silent caps, built in the database."""
    publisher = base._publisher()
    daily = publisher.register_definition(DefinitionSpec(
        type_key="raport_dzienny",
        display_name="Raport dzienny",
        cadence_class="on_demand",
        period_kind="day",
        generation_definition_ref="jobs.reports.daily",
    ))
    # Canonical `day` keys are the ISO date, which is exactly what 069's CHECK
    # derives — so this bulk insert is real data, not a constraint bypass.
    run(
        """
        INSERT INTO portal_generated_report_instances
          (definition_id, client_code, period_kind, period_key, period_start, period_end,
           display_name, generation_state, library_timestamp)
        SELECT %s, %s, 'day',
               to_char(d, 'YYYY-MM-DD'), d::date, d::date,
               'Dzień ' || to_char(d, 'YYYY-MM-DD'),
               'pending',
               timestamptz '2020-01-01 00:00:00+00' + (row_number() OVER (ORDER BY d)) * interval '1 hour'
          FROM generate_series(date '2012-01-01', date '2012-01-01' + (%s - 1), interval '1 day') AS d
        """,
        (daily, CLIENT, GENERATED_POPULATION),
    )
    run(
        """
        INSERT INTO artifacts (artifact_id, client_code, kind, storage_key, filename,
                               display_filename, content_type, size_bytes)
        SELECT gen_random_uuid(), %s, 'database_export', 'exports/' || g, 'e' || g || '.csv',
               'eksport-' || g || '.csv', 'text/csv', 100 + g
          FROM generate_series(1, %s) AS g
        """,
        (CLIENT, EXPORT_POPULATION),
    )
    run(
        """
        INSERT INTO database_export_jobs
          (job_id, dataset_id, requested_by_user_id, requested_format, status,
           completed_at, row_count, artifact_id)
        SELECT gen_random_uuid(), %s, %s, 'csv', 'completed',
               timestamptz '2019-01-01 00:00:00+00' + (row_number() OVER (ORDER BY a.artifact_id)) * interval '1 hour',
               7, a.artifact_id
          FROM artifacts a
         WHERE a.kind = 'database_export'
        """,
        (DATASET_ACME, ALICE),
    )
    return GENERATED_POPULATION, EXPORT_POPULATION


def _walk(user, *, limit=500, **params) -> tuple[list[str], int]:
    """Every instance reference reachable by paging, plus the stated total."""
    pages = base._pages()
    service = base._service()
    context = service.client_context(_user(user))
    seen: list[str] = []
    total = None
    page = 1
    while True:
        query = LibraryQuery(client_code=context.client_code, limit=limit, page=page, **params)
        result = service.library(_user(user), context, query)
        if total is None:
            total = result.filtered_total
        assert result.filtered_total == total, (result.filtered_total, total)
        batch = [i.instance_ref for group in result.groups for i in group.instances]
        if not batch:
            break
        seen.extend(batch)
        if page >= result.page_count:
            break
        page += 1
    assert pages is not None
    return seen, int(total or 0)


def test_every_record_stays_reachable_past_the_former_caps() -> None:
    _reset()
    _seed()
    generated, exports = _bulk_population()

    seen, total = _walk(ALICE)
    assert total == generated + exports, (total, generated, exports)
    assert len(seen) == total, (len(seen), total)
    assert len(set(seen)) == total, "no reference may appear on two pages"

    # The counted population and the reachable population are the same set.
    stored = {
        f"gen-{row['instance_id']}" for row in run(
            "SELECT instance_id FROM portal_generated_report_instances WHERE client_code = %s",
            (CLIENT,),
        )
    } | {
        f"exp-{row['job_id']}" for row in run("SELECT job_id FROM database_export_jobs")
    }
    assert set(seen) == stored, (len(set(seen) - stored), len(stored - set(seen)))

    # THE OLDEST RECORD — the one the former caps made unreachable — is on a
    # later page, and it is an EXPORT, which the 500-row cap hid first.
    oldest_export = run(
        "SELECT job_id FROM database_export_jobs ORDER BY completed_at ASC, job_id ASC LIMIT 1"
    )[0]["job_id"]
    assert seen[-1] == f"exp-{oldest_export}", (seen[-1], oldest_export)
    assert seen.index(f"exp-{oldest_export}") > 500, seen.index(f"exp-{oldest_export}")

    oldest_generated = run(
        "SELECT instance_id FROM portal_generated_report_instances "
        " WHERE client_code = %s ORDER BY library_timestamp ASC, instance_id ASC LIMIT 1",
        (CLIENT,),
    )[0]["instance_id"]
    assert seen.index(f"gen-{oldest_generated}") >= 5_000, seen.index(f"gen-{oldest_generated}")

    # Small pages reach exactly the same population, so the page size is not a
    # second ceiling either.
    small, small_total = _walk(ALICE, limit=25)
    assert small_total == total and small == seen, (small_total, total)
    _ok(f"all {total} records remain reachable past the former 500/5000 caps")


def test_pagination_is_stable_across_ties_and_truthful_under_filters() -> None:
    _reset()
    _seed()
    publisher = base._publisher()
    daily = publisher.register_definition(DefinitionSpec(
        type_key="raport_dzienny", display_name="Raport dzienny", cadence_class="on_demand",
        period_kind="day", generation_definition_ref="jobs.reports.daily",
    ))
    # EVERY instance shares one library timestamp: ordering must still be a total
    # order, or adjacent pages would overlap and lose records.
    run(
        """
        INSERT INTO portal_generated_report_instances
          (definition_id, client_code, period_kind, period_key, period_start, period_end,
           display_name, generation_state, library_timestamp)
        SELECT %s, %s, 'day', to_char(d, 'YYYY-MM-DD'), d::date, d::date,
               'Dzień ' || to_char(d, 'YYYY-MM-DD'), 'pending',
               timestamptz '2026-07-01 12:00:00+00'
          FROM generate_series(date '2026-01-01', date '2026-01-01' + 119, interval '1 day') AS d
        """,
        (daily, CLIENT),
    )
    first, total = _walk(ALICE, limit=25)
    assert total == 120, total
    assert len(set(first)) == 120, len(set(first))
    for _ in range(3):
        again, again_total = _walk(ALICE, limit=25)
        assert again == first and again_total == total, "tied rows must page identically"

    # Filters: what the counter says and what the pages render are one population.
    filtered, filtered_total = _walk(ALICE, limit=25, search="Dzień 2026-01-1")
    expected = run(
        "SELECT count(*)::int AS n FROM portal_generated_report_instances "
        " WHERE client_code = %s AND display_name ILIKE %s",
        (CLIENT, "%Dzień 2026-01-1%"),
    )[0]["n"]
    assert filtered_total == expected == len(filtered), (filtered_total, expected, len(filtered))
    assert set(filtered) <= set(first)

    by_status, status_total = _walk(ALICE, limit=25, status="generating")
    assert status_total == 120 == len(by_status), (status_total, len(by_status))
    none_ready, ready_total = _walk(ALICE, limit=25, status="ready")
    assert ready_total == 0 and none_ready == [], (ready_total, none_ready)

    by_year, year_total = _walk(ALICE, limit=25, year=2026)
    assert year_total == 120 == len(by_year), (year_total, len(by_year))
    _ok("ordering is total under ties and every filter's count matches its pages")


# ===========================================================================
# Runner
# ===========================================================================

def _run_all() -> None:
    test_the_correction_migration_applies_after_068_and_reruns()
    test_the_release_requirement_names_the_whole_chain()

    test_a_period_key_cannot_describe_another_logical_period()
    test_concurrent_first_generation_converges_on_one_instance()
    test_retry_and_the_neighbouring_period()

    test_two_concurrent_publishes_with_one_claim_replace_membership_once()
    test_a_duplicate_completed_callback_is_a_deterministic_no_op()
    test_a_stale_claim_cannot_publish_or_fail_over_a_newer_attempt()
    test_a_late_fail_cannot_turn_a_published_report_into_a_failed_one()

    test_the_availability_summary_cannot_be_forged_away_from_membership()

    test_presentation_metadata_cannot_make_content_previewable()
    test_the_preview_route_and_the_inline_response_enforce_it_too()

    test_the_archive_bound_uses_real_bytes_not_declared_sizes()
    test_archive_entry_names_are_safe_deterministic_and_authorized()
    test_an_unavailable_or_foreign_member_never_enters_the_archive()

    test_persisted_provenance_cannot_name_another_client_dataset_or_range()
    test_publication_refuses_a_dataset_the_catalogue_no_longer_offers()
    test_rp18_navigation_still_reauthorizes_database_explorer_independently()

    test_the_declared_file_contract_is_validated_and_enforced()

    test_every_record_stays_reachable_past_the_former_caps()
    test_pagination_is_stable_across_ties_and_truthful_under_filters()

    print("\nALL PASS: PORTAL_S15_REVIEW_FINDINGS_REMEDIATION")


def main() -> None:
    try:
        instance = base.disposable_postgres(label="s15fix")
    except base.DisposablePostgresUnavailable as exc:  # pragma: no cover
        print(f"LIVE_MIGRATION_TEST_NOT_AVAILABLE: {exc}")
        return
    try:
        with instance as (dsn, info):
            base.require_loopback_dsn_or_exit(dsn, label="disposable S15 correction instance")
            assert info["server_version_num"] // 10000 == 16, info
            assert "logdb" not in dsn.lower(), "refusing a production-like database name"
            base.DSN = dsn
            print(f"POSTGRES_16_VERIFIED: {info['server_version']} "
                  f"(container {info['container']}, database {info['database']})")
            api_main.db_conn = base.connect
            _run_all()
    except base.DisposablePostgresUnavailable as exc:
        print(f"LIVE_MIGRATION_TEST_NOT_AVAILABLE: {exc}")
        return

    print("\nDISPOSABLE_POSTGRES_S15_CORRECTION_TEST_PASS")


if __name__ == "__main__":
    main()
