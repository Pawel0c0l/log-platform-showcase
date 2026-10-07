#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import inspect
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from api import platform_prune as prune


IDENTITY = prune.RuntimeIdentity(
    environment="local_dev",
    platform_uuid="52517750-7438-4558-8490-2736ae4cc629",
    postgres_host="127.0.0.1",
    postgres_port=5432,
    postgres_db="logdb",
    postgres_user="logdb",
)


class FakeCursor:
    def __init__(self, connection, *, lock_available=True):
        self.connection = connection
        self.lock_available = lock_available
        self.rowcount = 0
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.connection.statements.append((normalized, params))
        if "pg_try_advisory_lock" in normalized:
            self._one = {"locked": self.lock_available}
        elif normalized.startswith("DELETE FROM"):
            self.rowcount = len(params[0])

    def fetchone(self):
        return self._one


class FakeConnection:
    def __init__(self, *, lock_available=True):
        self.lock_available = lock_available
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self, lock_available=self.lock_available)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakeS3:
    def __init__(self, *, fail_head=False, fail_delete_at=None):
        self.fail_head = fail_head
        self.fail_delete_at = fail_delete_at
        self.head_calls = 0
        self.delete_calls = 0

    def head_bucket(self, **_kwargs):
        self.head_calls += 1
        if self.fail_head:
            raise RuntimeError("unavailable")

    def delete_object(self, **_kwargs):
        self.delete_calls += 1
        if self.fail_delete_at == self.delete_calls:
            raise RuntimeError("delete failed")


def expect_error(code, callback):
    try:
        callback()
    except prune.PlatformPruneError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected {code}")


def test_retention_and_timezone():
    for malformed in (None, True, 0, -1, 3651, "60.0", "060", "unknown"):
        expect_error(
            "PRUNE_RETENTION_CONFIGURATION_INVALID",
            lambda malformed=malformed: prune.validate_retention_days(malformed),
        )
    assert prune.validate_retention_days("60") == 60
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    cutoff = prune.calculate_cutoff(now_utc=now, retention_days=60)
    assert cutoff == datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    expect_error(
        "PRUNE_TIMEZONE_INVALID",
        lambda: prune.calculate_cutoff(now_utc=datetime(2026, 7, 13), retention_days=60),
    )
    planner_source = inspect.getsource(prune.build_prune_plan)
    # Four cutoff predicates: artifacts, logs, runs, and — since M4 — the
    # provider request log. Every one of them must be STRICTLY `<`, so a row
    # exactly on the cutoff is retained rather than deleted; that is the
    # property this pair of assertions exists for, and the count is what keeps a
    # newly added predicate from escaping it.
    assert planner_source.count(" < %s") == 4
    assert " <= %s" not in planner_source

    # The Portal generated-report projection must stay availability-scoped.
    # Dropping `AND ref.is_available` would silently promote every expired
    # member back into a permanent artifact retention and quietly disable the
    # 60-day horizon for the whole Report Explorer surface.
    assert "portal_generated_report_files ref" in planner_source
    assert "AND ref.is_available" in planner_source

    # M4 evidence prunes on its own fixed 180-day horizon, deliberately
    # independent of the worker's `--days` (docs/20 §4.5). It is a constant
    # rather than a flag so an operator cannot shorten it by passing `--days`.
    assert prune.PROVIDER_REQUEST_LOG_RETENTION_DAYS == 180
    m4_cutoff = prune.calculate_cutoff(
        now_utc=now, retention_days=prune.PROVIDER_REQUEST_LOG_RETENTION_DAYS,
    )
    assert m4_cutoff == datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    assert m4_cutoff < cutoff


def test_eligibility_contract():
    base_artifact = {
        "run_status": "SUCCESS",
        "raw_file_id": None,
        "workflow_name": "platform",
        "export_reference": False,
        "metadata_reference": False,
        "tag_reference": False,
        "folder_reference": False,
        "generated_report_reference": False,
        "storage_backend": "S3",
        "storage_key": "opaque",
    }
    assert prune.artifact_exclusion_reason(base_artifact) is None
    variants = {
        "artifact_active_run": {"run_status": "RUNNING"},
        "artifact_workflow_b": {"workflow_name": "workflow_b"},
        "artifact_separate_retention": {"workflow_name": "database_explorer"},
        "artifact_generated_report_available": {"generated_report_reference": True},
        "artifact_retained_reference": {"tag_reference": True},
        "artifact_storage_backend_unsupported": {"storage_backend": "LOCAL"},
        "artifact_storage_identity_ambiguous": {"storage_key": ""},
    }
    for expected, change in variants.items():
        assert prune.artifact_exclusion_reason(base_artifact | change) == expected
    assert prune.log_exclusion_reason({"run_status": "RUNNING"}) == "log_active_run"
    assert prune.log_exclusion_reason({"run_status": "SUCCESS"}) is None
    assert prune.run_exclusion_reason({"status": "RUNNING"}) == "run_active"
    assert prune.run_exclusion_reason(
        {"status": "SUCCESS", "has_logs": True, "has_artifacts": False}
    ) == "run_retained_reference"
    assert prune.run_exclusion_reason(
        {"status": "SUCCESS", "has_logs": False, "has_artifacts": False}
    ) is None

    # Portal V1 (migration 068). The projection is availability-scoped in SQL,
    # so an EXPIRED member — one already swept to `is_available = FALSE` by
    # `ReportPublication.expire_due_members` — never sets the flag and therefore
    # never retains the artifact. Its history row survives the delete through
    # the FK's own ON DELETE SET NULL, which is what 068 designed it for.
    assert prune.artifact_exclusion_reason(
        base_artifact | {"generated_report_reference": False}
    ) is None
    # An AVAILABLE member is retained. Nothing in the schema would stop the
    # delete — 068's availability trigger drops `is_available` to satisfy its
    # own CHECK — so a live, downloadable report would silently become
    # `Pliki wygasły`. This exclusion is the only defence, and the real catalog
    # behaviour it rests on is proven in
    # `test_platform_prune_artifact_reference_contract_postgres.py`.
    assert prune.artifact_exclusion_reason(
        base_artifact | {"generated_report_reference": True}
    ) == "artifact_generated_report_available"
    # It is its own lifecycle class, not folded into the curation class: those
    # references CASCADE, this one does not.
    assert prune.artifact_exclusion_reason(
        base_artifact | {"generated_report_reference": True}
    ) != "artifact_retained_reference"

    # A retrospectively reconciled run (migration 064) is retained for the same
    # reason logs and artifacts retain one. Its FK is ON DELETE RESTRICT, so
    # planning it for deletion would fail the DB transaction *after* MinIO
    # objects were already deleted.
    reconciled = {
        "status": "FAILED", "has_logs": False, "has_artifacts": False,
        "has_reconciliation": True,
    }
    assert prune.run_exclusion_reason(reconciled) == "run_retained_reference"
    # Terminal status does not matter; CANCELED is as terminal as FAILED.
    assert prune.run_exclusion_reason(
        reconciled | {"status": "CANCELED"}
    ) == "run_retained_reference"
    # An unreconciled terminal run with no other reference stays prune-eligible,
    # so the new rule retains only what it must.
    assert prune.run_exclusion_reason(
        reconciled | {"has_reconciliation": False}
    ) is None
    # A schema without migration 064 reports the flag as false and must keep
    # pruning normally rather than retaining everything.
    assert prune.run_exclusion_reason(
        {"status": "FAILED", "has_logs": False, "has_artifacts": False}
    ) is None
    # Existing precedence is unchanged: an active run is still `run_active`.
    assert prune.run_exclusion_reason(
        {"status": "RUNNING", "has_reconciliation": True}
    ) == "run_active"


def sample_plan():
    return prune.PrunePlan(
        cutoff=datetime(2026, 5, 14, tzinfo=timezone.utc),
        retention_days=60,
        artifacts=[
            prune.ArtifactCandidate("00000000-0000-0000-0000-000000000001", "secret-object-key")
        ],
        log_ids=[1],
        run_ids=["00000000-0000-0000-0000-000000000002"],
        excluded={"run_active": 5, "artifact_workflow_b": 93},
    )


def test_dry_run_is_stable_and_non_mutating():
    results = []
    for _ in range(2):
        connection = FakeConnection()
        s3 = FakeS3()
        with patch.object(prune, "attest_platform_identity"), patch.object(
            prune, "build_prune_plan", return_value=sample_plan()
        ):
            result = prune.run_prune(
                connection_factory=lambda: connection,
                s3_client=s3,
                bucket="opaque",
                identity=IDENTITY,
                retention_days=60,
                dry_run=True,
                now_utc=datetime(2026, 7, 13, tzinfo=timezone.utc),
            )
        assert result["classification"] == "PRUNE_DRY_RUN_SUCCEEDED"
        assert set(result["mutations"].values()) == {0}
        assert connection.commits == 0
        assert not any(sql.startswith("DELETE ") for sql, _ in connection.statements)
        assert s3.delete_calls == 0
        assert "secret-object-key" not in json.dumps(result)
        results.append(result)
    assert results[0] == results[1]


def test_execution_failure_keeps_database_rows():
    connection = FakeConnection()
    s3 = FakeS3(fail_delete_at=1)
    with patch.object(prune, "attest_platform_identity"), patch.object(
        prune, "build_prune_plan", return_value=sample_plan()
    ):
        expect_error(
            "PRUNE_OBJECT_STORE_DELETE_FAILED",
            lambda: prune.run_prune(
                connection_factory=lambda: connection,
                s3_client=s3,
                bucket="opaque",
                identity=IDENTITY,
                retention_days=60,
                dry_run=False,
            ),
        )
    assert connection.commits == 0
    assert not any(sql.startswith("DELETE ") for sql, _ in connection.statements)


#: The exact FK contract production carries after the Portal V1 migration chain
#: (064-069) was applied on 2026-08-19, in the metadata the prune's DELETE
#: actually depends on. Verified read-only against the live catalog: six
#: references to `public.artifacts`, no more and no fewer, each with the delete
#: action and referencing-column nullability recorded here.
REF = prune.ArtifactReference
PRODUCTION_ARTIFACT_REFERENCES = [
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
]

#: The Portal entry, named once so the mutation cases below read as mutations
#: OF IT rather than as six-line literals that could drift apart from it.
PORTAL_REFERENCE = PRODUCTION_ARTIFACT_REFERENCES[-1]


def _without(reference):
    return [r for r in PRODUCTION_ARTIFACT_REFERENCES if r != reference]


def _portal_mutated(**changes):
    """The production contract with the Portal reference altered in one way."""
    return _without(PORTAL_REFERENCE) + [PORTAL_REFERENCE._replace(**changes)]


class ReferenceCursor:
    """Feeds `validate_artifact_reference_contract` a catalog result.

    The validator itself is never patched: these tests drive the real function
    over its real query result shape, so a change to the comparison — to
    containment, to a count, to a normalised name — fails here.
    """

    def __init__(self, references, column_counts=None, replication_role="origin"):
        self.references = list(references)
        self.column_counts = dict(column_counts or {})
        self.replication_role = replication_role
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))

    def fetchone(self):
        return {"replication_role": self.replication_role}

    def fetchall(self):
        return [
            {
                "referencing_table": reference.referencing_table,
                "referencing_column": reference.referencing_column,
                "referenced_table": reference.referenced_table,
                "referenced_column": reference.referenced_column,
                "delete_action": reference.delete_action,
                "referencing_column_nullable": reference.referencing_column_nullable,
                "delete_action_enforced": reference.delete_action_enforced,
                "referencing_column_count": self.column_counts.get(
                    reference.referencing_table, 1
                ),
            }
            for reference in self.references
        ]


def test_reference_contract_accepts_exact_portal_v1_schema():
    # 1. The exact six-reference production contract is ACCEPTED.
    cursor = ReferenceCursor(PRODUCTION_ARTIFACT_REFERENCES)
    prune.validate_artifact_reference_contract(cursor)

    # The validator must actually have asked the catalog, keyed on the real
    # referenced relation, and must have asked for the SAFETY-RELEVANT columns
    # rather than just the endpoints — a query that stopped at
    # `(table, column)` would make every mutation case below unfalsifiable.
    assert len(cursor.executed) == 2
    query = cursor.executed[1]
    assert "pg_constraint" in query
    assert "'public.artifacts'::regclass" in query
    assert "contype = 'f'" in query
    assert "confdeltype" in query, "the delete action must be read from the catalog"
    assert "attnotnull" in query, "referencing-column nullability must be read"
    assert "confkey" in query, "the referenced column must be read"
    assert "pg_namespace" in query, "schema qualification must not depend on search_path"
    assert "session_replication_role" in cursor.executed[0], (
        "replica mode suppresses ordinary triggers, so the session's replication "
        "role has to be established before any delete action is trusted"
    )
    query = cursor.executed[1]
    assert "pg_trigger" in query and "tgenabled" in query, (
        "the delete action is carried out by triggers; a declared action whose "
        "triggers are disabled is a fiction, so their state must be read too"
    )

    # 2. The Portal reference is represented EXACTLY, in full: schema-qualified
    #    on both sides, ON DELETE SET NULL, over a nullable column. A near-miss
    #    is not the contract.
    assert PORTAL_REFERENCE in prune.EXPECTED_ARTIFACT_REFERENCES
    assert PORTAL_REFERENCE.referencing_table == "public.portal_generated_report_files"
    assert PORTAL_REFERENCE.referencing_column == "artifact_id"
    assert PORTAL_REFERENCE.referenced_table == "public.artifacts"
    assert PORTAL_REFERENCE.referenced_column == "artifact_id"
    assert PORTAL_REFERENCE.delete_action == "SET NULL"
    assert PORTAL_REFERENCE.referencing_column_nullable is True
    assert PORTAL_REFERENCE.delete_action_enforced is True

    # 3. The five pre-Portal references are unchanged, and the set is exactly
    #    six: nothing was dropped or loosened to make room for the sixth. Their
    #    delete actions are transcribed from migrations 024/026/043/048, so
    #    this also pins that the strengthening changed no legacy semantics.
    assert prune.EXPECTED_ARTIFACT_REFERENCES == set(PRODUCTION_ARTIFACT_REFERENCES)
    assert len(prune.EXPECTED_ARTIFACT_REFERENCES) == 6
    for legacy in (
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
    ):
        assert legacy in prune.EXPECTED_ARTIFACT_REFERENCES


def test_reference_contract_stays_fail_closed():
    # An arbitrary SEVENTH reference still stops the prune. This is the whole
    # point of the exact comparison: the next schema evolution gets reviewed
    # before a delete is planned against it.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(
                PRODUCTION_ARTIFACT_REFERENCES
                + [REF("public.some_future_table", "artifact_id",
                       "public.artifacts", "artifact_id", "CASCADE", True)]
            )
        ),
    )
    # Including one that merely looks Portal-adjacent.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(
                PRODUCTION_ARTIFACT_REFERENCES
                + [REF("public.portal_generated_report_archives", "artifact_id",
                       "public.artifacts", "artifact_id", "SET NULL", True)]
            )
        ),
    )

    # A MISSING expected reference fails closed too — the comparison is equality,
    # not containment, so a dropped or not-yet-migrated relation is just as
    # ambiguous as an extra one. Each of the six, removed in turn.
    for reference in PRODUCTION_ARTIFACT_REFERENCES:
        expect_error(
            "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
            lambda reduced=_without(reference): prune.validate_artifact_reference_contract(
                ReferenceCursor(reduced)
            ),
        )

    # MUTATED metadata of an expected reference fails closed. Endpoint identity
    # is compared, so a renamed relation, a moved schema or a repointed column
    # is a new reference, not the reviewed one.
    for mutated in (
        _portal_mutated(referencing_column="artifact_uuid"),
        _portal_mutated(referencing_table="portal.portal_generated_report_files"),
        _portal_mutated(referencing_table="public.portal_generated_report_file"),
        _portal_mutated(referencing_table="portal_generated_report_files"),
        _without(PRODUCTION_ARTIFACT_REFERENCES[4])
        + [PRODUCTION_ARTIFACT_REFERENCES[4]._replace(
            referencing_column="stage3_cleaned_artifact_id")],
        _without(PRODUCTION_ARTIFACT_REFERENCES[1])
        + [PRODUCTION_ARTIFACT_REFERENCES[1]._replace(referencing_table="artifact_tags")],
    ):
        expect_error(
            "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
            lambda replaced=mutated: prune.validate_artifact_reference_contract(
                ReferenceCursor(replaced)
            ),
        )


def test_portal_delete_semantics_are_pinned_not_merely_its_endpoints():
    """The Codex finding, made falsifiable.

    Every case here leaves `(table, column)` EXACTLY as production carries it
    and changes only what the DELETE does. Under the old `(table, column)`
    contract all of them passed; the generated-report history-preservation
    guarantee depends on all of them failing.
    """
    # CASCADE would delete the member row instead of preserving it: the report
    # history disappears with its bytes.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(_portal_mutated(delete_action="CASCADE"))
        ),
    )
    # RESTRICT would abort the DELETE — after the MinIO objects are already
    # gone, leaving rows pointing at bytes that no longer exist.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(_portal_mutated(delete_action="RESTRICT"))
        ),
    )
    # NO ACTION and SET DEFAULT are no more reviewed than the two above.
    for action in ("NO ACTION", "SET DEFAULT", "UNRECOGNISED"):
        expect_error(
            "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
            lambda action=action: prune.validate_artifact_reference_contract(
                ReferenceCursor(_portal_mutated(delete_action=action))
            ),
        )

    # A NOT NULL referencing column makes `SET NULL` raise mid-delete rather
    # than preserve history, so nullability is part of the guard and not an
    # inference from the delete action.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(_portal_mutated(referencing_column_nullable=False))
        ),
    )

    # A declared `SET NULL` whose referential-action triggers have been
    # disabled does not set anything to NULL — the DELETE would leave a
    # dangling reference. `confdeltype` alone cannot see that.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(_portal_mutated(delete_action_enforced=False))
        ),
    )

    # A reference repointed at a different target column or a different target
    # relation is a different lifecycle, even with an identical referencing
    # `(table, column)` and an identical delete action.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(_portal_mutated(referenced_column="storage_key"))
        ),
    )
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(_portal_mutated(referenced_table="archive.artifacts"))
        ),
    )

    # The legacy references are pinned by the same rule, so a future migration
    # cannot quietly relax one of them either.
    for index, drifted in ((0, "SET NULL"), (3, "CASCADE"), (4, "CASCADE")):
        reference = PRODUCTION_ARTIFACT_REFERENCES[index]
        expect_error(
            "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
            lambda reference=reference, drifted=drifted: (
                prune.validate_artifact_reference_contract(
                    ReferenceCursor(
                        _without(reference)
                        + [reference._replace(delete_action=drifted)]
                    )
                )
            ),
        )

    # A COMPOSITE key to `artifacts` arrives as several rows that each look
    # like a whole reference. It is refused rather than compared per column.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(
            ReferenceCursor(
                PRODUCTION_ARTIFACT_REFERENCES,
                column_counts={"public.portal_generated_report_files": 2},
            )
        ),
    )

    # A session running as `replica` suppresses every ordinary trigger, so each
    # constraint still reads as enforced while its delete action does nothing.
    # The catalog is IDENTICAL to the accepted one here; only the session differs.
    for role in ("replica", "local", ""):
        expect_error(
            "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
            lambda role=role: prune.validate_artifact_reference_contract(
                ReferenceCursor(PRODUCTION_ARTIFACT_REFERENCES, replication_role=role)
            ),
        )

    # And an empty catalog result is not a licence to prune.
    expect_error(
        "PRUNE_REFERENCE_CONTRACT_AMBIGUOUS",
        lambda: prune.validate_artifact_reference_contract(ReferenceCursor([])),
    )


def test_generated_report_publication_has_no_production_caller_yet():
    """A tripwire on the availability exclusion's remaining assumption.

    The plan-time `is_available` check is safe against a concurrent publication
    for two proven reasons: an existing member's availability only ever moves
    TRUE -> FALSE, and the real (non-dry-run) plan holds `FOR UPDATE OF
    artifact`, which conflicts with the `FOR KEY SHARE` an inserting
    publication takes on the parent row.

    A THIRD assumption is not enforced by anything: `_bind_members_to_artifacts`
    accepts any artifact of the right client and content type, with no age
    bound, so a publisher COULD bind an artifact already older than the
    retention horizon. Today that is unreachable — `ReportPublicationService`
    is constructed in `api/main.py` but nothing calls `.publish(...)`, and
    production carries zero definitions, instances and members.

    When that changes, this test fails, and the question to answer before
    deleting the assertion is: can the generator bind an artifact older than
    `--days`? If it can, the availability exclusion needs an age-independent
    guard; if it only ever binds objects it just uploaded, the window cannot
    open and this test can go.
    """
    publication = (REPO_ROOT / "api" / "report_explorer" / "publication.py").read_text("utf-8")
    assert "def publish(" in publication, "the publication entry point moved; re-derive this"
    # No age bound on binding — the reason the assumption is worth tracking.
    assert "created_at" not in publication.split("_bind_members_to_artifacts", 1)[1][:2000]

    callers = []
    for directory in ("api", "jobs", "scripts", "ops"):
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "tests_manual" in path.parts or path.name == "publication.py":
                continue
            text = path.read_text("utf-8", errors="ignore")
            if "ReportPublicationService" in text and "_report_publication_service." in text:
                callers.append(str(path.relative_to(REPO_ROOT)))
    assert not callers, (
        "a generated-report publisher was wired up: re-check whether it can bind "
        f"an artifact older than the retention horizon before trusting the "
        f"availability exclusion ({callers})"
    )


def test_dependency_and_lock_failures():
    expect_error(
        "PRUNE_DATABASE_UNAVAILABLE",
        lambda: prune.run_prune(
            connection_factory=lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")),
            s3_client=FakeS3(),
            bucket="opaque",
            identity=IDENTITY,
            retention_days=60,
            dry_run=True,
        ),
    )
    connection = FakeConnection(lock_available=False)
    with patch.object(prune, "attest_platform_identity"):
        expect_error(
            "PRUNE_LOCKED",
            lambda: prune.run_prune(
                connection_factory=lambda: connection,
                s3_client=FakeS3(),
                bucket="opaque",
                identity=IDENTITY,
                retention_days=60,
                dry_run=True,
            ),
        )
    connection = FakeConnection()
    with patch.object(prune, "attest_platform_identity"):
        expect_error(
            "PRUNE_DEPENDENCY_FAILED",
            lambda: prune.run_prune(
                connection_factory=lambda: connection,
                s3_client=FakeS3(fail_head=True),
                bucket="opaque",
                identity=IDENTITY,
                retention_days=60,
                dry_run=True,
            ),
        )


def test_host_minio_endpoint_selection():
    explicit = {
        "MINIO_HOST_ENDPOINT": "host-loopback:9000",
        "MINIO_ENDPOINT": prune.COMPOSE_INTERNAL_MINIO_ENDPOINT,
    }
    assert prune.select_host_minio_endpoint(explicit) == "host-loopback:9000"
    assert prune.select_host_minio_endpoint(
        {"MINIO_ENDPOINT": prune.COMPOSE_INTERNAL_MINIO_ENDPOINT}
    ) == prune.DEFAULT_HOST_MINIO_ENDPOINT
    assert prune.select_host_minio_endpoint(
        {"MINIO_ENDPOINT": "operator-hostname:9000"}
    ) == "operator-hostname:9000"
    expect_error(
        "PRUNE_HOST_MINIO_ENDPOINT_MISSING",
        lambda: prune.select_host_minio_endpoint({}),
    )
    expect_error(
        "PRUNE_HOST_MINIO_ENDPOINT_INVALID",
        lambda: prune.select_host_minio_endpoint({"MINIO_HOST_ENDPOINT": ""}),
    )
    credentials = {
        "MINIO_ENDPOINT": prune.COMPOSE_INTERNAL_MINIO_ENDPOINT,
        "MINIO_ROOT_USER": "opaque-user",
        "MINIO_ROOT_PASSWORD": "opaque-secret",
        "MINIO_SECURE": "0",
    }
    sentinel = object()
    with patch.object(prune.boto3, "client", return_value=sentinel) as client:
        assert prune._s3_client(credentials) is sentinel
    kwargs = client.call_args.kwargs
    assert kwargs["aws_access_key_id"] == "opaque-user"
    assert kwargs["aws_secret_access_key"] == "opaque-secret"
    assert "opaque" not in str(prune.PlatformPruneError("PRUNE_HOST_MINIO_ENDPOINT_MISSING"))


def test_compose_prune_identity_contract():
    repo = Path(__file__).resolve().parents[2]
    compose = (repo / "docker-compose.yml").read_text()
    api_section = compose.split("  api:", 1)[1].split("\nvolumes:", 1)[0]
    required = {
        "LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID",
        "LOG_PLATFORM_EXPECTED_POSTGRES_HOST",
        "LOG_PLATFORM_EXPECTED_POSTGRES_PORT",
        "LOG_PLATFORM_EXPECTED_POSTGRES_DB",
        "LOG_PLATFORM_EXPECTED_POSTGRES_USER",
    }
    declared = {
        line.strip().split(":", 1)[0]
        for line in api_section.splitlines()
        if line.strip().startswith("LOG_PLATFORM_")
    }
    assert declared == required
    assert "path: /etc/log-platform/environment-identity.env" in api_section
    assert "required: false" in api_section
    assert "LOG_PLATFORM_TARGET_ENVIRONMENT:" not in api_section
    assert "LOG_PLATFORM_COMPOSE_EXPECTED_POSTGRES_HOST:-postgres" in api_section
    assert api_section.count(":?required}") == 4
    assert "MINIO_ENDPOINT:" in api_section and "MINIO_ENDPOINT}" in api_section
    values = {
        "LOG_PLATFORM_TARGET_ENVIRONMENT": "local_dev",
        "LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID": IDENTITY.platform_uuid,
        "LOG_PLATFORM_EXPECTED_POSTGRES_HOST": "postgres",
        "LOG_PLATFORM_EXPECTED_POSTGRES_PORT": "5432",
        "LOG_PLATFORM_EXPECTED_POSTGRES_DB": "logdb",
        "LOG_PLATFORM_EXPECTED_POSTGRES_USER": "logdb",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "logdb",
        "POSTGRES_USER": "logdb",
    }
    assert prune.load_runtime_identity(values).postgres_host == "postgres"


def test_environment_identity_fails_closed():
    values = {
        "LOG_PLATFORM_TARGET_ENVIRONMENT": "local_dev",
        "LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID": IDENTITY.platform_uuid,
        "LOG_PLATFORM_EXPECTED_POSTGRES_HOST": "127.0.0.1",
        "LOG_PLATFORM_EXPECTED_POSTGRES_PORT": "5432",
        "LOG_PLATFORM_EXPECTED_POSTGRES_DB": "logdb",
        "LOG_PLATFORM_EXPECTED_POSTGRES_USER": "logdb",
        "POSTGRES_HOST": "wrong-host",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "logdb",
        "POSTGRES_USER": "logdb",
    }
    expect_error(
        "PRUNE_ENVIRONMENT_IDENTITY_MISMATCH",
        lambda: prune.load_runtime_identity(values),
    )


def test_backup_lock_contention():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / ".backup.lock"
        with path.open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            expect_error(
                "PRUNE_BACKUP_ACTIVE",
                lambda: _enter_backup_lock(path),
            )


def _enter_backup_lock(path):
    with prune.coordinated_backup_lock(path):
        raise AssertionError("lock should not have been acquired")


def test_systemd_and_api_sources():
    repo = Path(__file__).resolve().parents[2]
    service = (repo / "ops/systemd/log-platform-prune.service").read_text()
    wrapper = (repo / "ops/platform_prune.sh").read_text()
    api_source = (repo / "api/main.py").read_text()
    assert "--execute --days 60" in service
    assert "User=logplatform" in service
    assert "WorkingDirectory=" in service
    assert "TimeoutStartSec=30min" in service
    assert "curl" not in service + wrapper
    assert "http://" not in service + wrapper
    assert "PRUNE_API_EXECUTION_DISABLED_USE_COORDINATED_HOST_COMMAND" in api_source


def test_hard_ceiling_relaxes_only_the_retention_exclusions():
    """The ceiling pass is the same planner with an older cutoff.

    What it must change: the two exclusions that are RETENTION decisions —
    Workflow B lineage and user curation — stop protecting an artifact that is
    past the global ceiling. Those two classes were unbounded before.

    What it must NOT change: `artifact_active_run` (a run is still writing),
    the two storage-identity refusals (we cannot name the object to delete),
    `artifact_separate_retention` (the Database Explorer 3-day lifecycle owns
    those bytes and deletes them itself) and
    `artifact_generated_report_available` (the product is still offering the
    file; deleting it would silently flip a live report to `Pliki wygasly`).
    Those are correctness guards, not horizons, and a longer horizon does not
    make them safe.
    """
    from ops import retention_registry

    base = {
        "run_status": "SUCCESS", "raw_file_id": None, "workflow_name": "platform",
        "export_reference": False, "metadata_reference": False, "tag_reference": False,
        "folder_reference": False, "generated_report_reference": False,
        "storage_backend": "S3", "storage_key": "opaque",
    }
    relaxed = {
        "artifact_workflow_b": {"workflow_name": "workflow_b"},
        "artifact_retained_reference": {"metadata_reference": True},
    }
    for reason, patch in relaxed.items():
        row = {**base, **patch}
        assert prune.artifact_exclusion_reason(row) == reason
        assert prune.artifact_exclusion_reason(row, hard_ceiling=True) is None, reason

    still_protective = {
        "artifact_active_run": {"run_status": "RUNNING"},
        "artifact_separate_retention": {"workflow_name": "database_explorer"},
        "artifact_generated_report_available": {"generated_report_reference": True},
        "artifact_storage_backend_unsupported": {"storage_backend": "LOCAL"},
        "artifact_storage_identity_ambiguous": {"storage_key": " leading-space"},
    }
    for reason, patch in still_protective.items():
        row = {**base, **patch}
        assert prune.artifact_exclusion_reason(row) == reason
        assert prune.artifact_exclusion_reason(row, hard_ceiling=True) == reason, reason

    # Exactly two, named once, in a frozen set the planner reads.
    assert prune.CEILING_RELAXED_EXCLUSIONS == frozenset(
        {"artifact_workflow_b", "artifact_retained_reference"}
    )

    # The ceiling is imported, not restated, and it is calendar arithmetic.
    assert prune.HARD_RETENTION_MONTHS == retention_registry.HARD_RETENTION_MONTHS == 13
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    assert prune.hard_retention_cutoff(now) == datetime(
        2025, 2, 28, 12, 0, tzinfo=timezone.utc
    ), "13 calendar months, clamped at the month end — never a day count"
    assert prune.hard_retention_cutoff(now) < prune.calculate_cutoff(
        now_utc=now, retention_days=60
    ), "the ceiling horizon must be older than the ordinary one"

    # A day count and the ceiling are mutually exclusive, in the API and the CLI.
    expect_error(
        "PRUNE_RETENTION_CONFIGURATION_INVALID",
        lambda: prune.run_prune(
            connection_factory=lambda: None, s3_client=None, bucket="b",
            identity=IDENTITY, retention_days=60, dry_run=True, hard_ceiling=True,
        ),
    )
    for argv in (["--dry-run"], ["--dry-run", "--days", "60", "--hard-ceiling"]):
        try:
            prune.cli(argv)
        except SystemExit as exc:
            assert exc.code == 2, argv
        else:  # pragma: no cover - argparse always exits here
            raise AssertionError(f"{argv} should have been refused")

    # The plan reports which horizon produced it, so a JSON reader never has to
    # guess whether "retention_days: null" means ceiling or means broken.
    plan = prune.PrunePlan(
        cutoff=prune.hard_retention_cutoff(now), retention_days=None, hard_ceiling=True,
    )
    summary = plan.safe_summary()
    assert summary["hard_ceiling"] is True
    assert summary["retention_days"] is None
    assert summary["retention_months"] == 13
    assert summary["policy_id"] == retention_registry.GLOBAL_POLICY_ID

    ordinary = prune.PrunePlan(cutoff=now, retention_days=60).safe_summary()
    assert ordinary["hard_ceiling"] is False
    assert ordinary["retention_months"] is None
    assert ordinary["retention_days"] == 60


def main():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print(f"platform prune focused tests: {len(tests)} passed")


if __name__ == "__main__":
    main()
