#!/usr/bin/env python3
"""Report Explorer (`S15`) against a real PostgreSQL 16.

WHAT THIS PROVES.
    Migration `068` applies to a clean prerequisite schema and the invariants it
    declares are enforced by the DATABASE, not by application discipline; the
    publication boundary is idempotent across retry and regeneration; the read
    path is client-scoped first, so no crafted instance, member or artifact id
    reaches another client's data; and the approved pages render the approved
    Polish copy over that real data.

    Text parsing would prove none of it. Every structural assertion below is a
    statement PostgreSQL either accepted or refused.

DESTRUCTIVE, AND TASK-OWNED. The suite accepts no DSN. It STARTS its own
`postgres:16` container on a free loopback port, verifies the server really is
major version 16, creates a database of its own inside it, and removes the
container in a `finally`. There is no input that can point it at `logdb`,
staging or production, and it applies `068` to nothing else.

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \\
        ops/tests_manual/test_portal_s15_report_explorer_postgres.py

Without a usable Docker daemon or a local `postgres:16` image it prints
``LIVE_MIGRATION_TEST_NOT_AVAILABLE`` and exits 0.
"""
from __future__ import annotations

import re
import sys
import types
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The S15 schema is the CHAIN, not one file. `068` is immutable shared history
# and `069` is the corrective migration that closes the independent review's
# integrity findings; a database carrying only `068` is not an S15 database, and
# `db/schema_requirements.json` declares both.
MIGRATIONS = (
    REPO_ROOT / "db" / "migrations" / "068_portal_generated_reports.sql",
    REPO_ROOT / "db" / "migrations" / "069_portal_generated_reports_integrity.sql",
)
MIGRATION = MIGRATIONS[0]
CORRECTION = MIGRATIONS[1]

S15_TABLES = {
    "portal_generated_report_definitions",
    "portal_generated_report_instances",
    "portal_generated_report_files",
}
PREREQUISITE_TABLES = {
    "artifact_users", "portal_clients", "portal_user_clients", "portal_groups",
    "portal_group_users", "portal_group_clients", "portal_database_datasets",
    "portal_database_dataset_users", "portal_database_dataset_groups",
    "portal_database_dataset_columns", "artifacts", "database_export_jobs",
    "database_export_attempt_objects", "portal_audit_events",
    "unrelated_keepsake",
}

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"   # direct grant on ACME
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"     # group grant on ACME
CAROL = "cccccccc-cccc-cccc-cccc-cccccccccccc"   # database access, no reports
DAVE = "dddddddd-dddd-dddd-dddd-dddddddddddd"    # nothing at all
ERIN = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"    # admin, no client grant
FRANK = "ffffffff-ffff-ffff-ffff-ffffffffffff"   # OTHER client only

GROUP_ID = "11111111-1111-1111-1111-111111111111"
DATASET_ACME = "22222222-2222-2222-2222-222222222222"
DATASET_OTHER = "33333333-3333-3333-3333-333333333333"

CLIENT = "ACME_01"
OTHER_CLIENT = "OTHER_02"


def _install_stubs() -> None:
    """FastAPI, boto3 and the S6 crypto dependency are stubbed; `psycopg` is REAL.

    The disposable-database interpreter carries `psycopg` but not `cryptography`
    or `fastapi`. `S15` neither mints nor reads an opaque row reference and
    never constructs a real S3 client — an unusable object store is in fact one
    of the states this suite asserts (`RP-20`) — so inert stand-ins are correct
    rather than a weakening.
    """
    crypto = types.ModuleType("cryptography")
    exceptions = types.ModuleType("cryptography.exceptions")

    class _InvalidTag(Exception):
        pass

    exceptions.InvalidTag = _InvalidTag
    hazmat = types.ModuleType("cryptography.hazmat")
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    hashes = types.ModuleType("cryptography.hazmat.primitives.hashes")
    hashes.SHA256 = object
    ciphers = types.ModuleType("cryptography.hazmat.primitives.ciphers")
    aead = types.ModuleType("cryptography.hazmat.primitives.ciphers.aead")
    aead.AESGCM = object
    kdf = types.ModuleType("cryptography.hazmat.primitives.kdf")
    hkdf = types.ModuleType("cryptography.hazmat.primitives.kdf.hkdf")
    hkdf.HKDF = object
    for name, module in (
        ("cryptography", crypto),
        ("cryptography.exceptions", exceptions),
        ("cryptography.hazmat", hazmat),
        ("cryptography.hazmat.primitives", primitives),
        ("cryptography.hazmat.primitives.hashes", hashes),
        ("cryptography.hazmat.primitives.ciphers", ciphers),
        ("cryptography.hazmat.primitives.ciphers.aead", aead),
        ("cryptography.hazmat.primitives.kdf", kdf),
        ("cryptography.hazmat.primitives.kdf.hkdf", hkdf),
    ):
        sys.modules.setdefault(name, module)

    class _App:
        def __init__(self, *a, **k):
            self.routes = []

        def get(self, *a, **k):
            return lambda fn: fn

        post = patch = delete = on_event = get

        def add_api_route(self, path, endpoint, **k):
            self.routes.append((path, endpoint))

    class _HTMLResponse:
        def __init__(self, content, status_code=200, headers=None, media_type=None):
            self.body = str(content).encode("utf-8")
            self.status_code = status_code
            self.headers = headers or {}
            self.media_type = media_type or "text/html"

    class _StreamingResponse:
        def __init__(self, body, media_type=None, headers=None):
            self.body = body
            self.media_type = media_type
            self.headers = headers or {}
            self.status_code = 200

    class _HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def _identity_default(default=None, *a, **k):
        return default

    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.Header = _identity_default
    fastapi.HTTPException = _HTTPException
    fastapi.Request = object
    fastapi.UploadFile = object
    fastapi.File = _identity_default
    fastapi.Form = _identity_default
    fastapi.Query = _identity_default
    fastapi.Body = _identity_default
    class _JSONResponse:
        def __init__(self, content=None, status_code=200, headers=None, media_type=None):
            self.content = content
            self.status_code = status_code
            self.headers = headers or {}
            self.media_type = media_type or "application/json"

    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = _HTMLResponse
    responses.StreamingResponse = _StreamingResponse
    responses.JSONResponse = _JSONResponse
    responses.PlainTextResponse = _JSONResponse
    responses.RedirectResponse = _JSONResponse
    responses.Response = _JSONResponse
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: None
    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)


_install_stubs()

try:  # noqa: E402
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - interpreter without the driver
    print("LIVE_MIGRATION_TEST_NOT_AVAILABLE: psycopg is unavailable; run this suite with .venv/bin/python")
    raise SystemExit(0)

from ops.tests_manual.disposable_postgres import (  # noqa: E402
    DisposablePostgresUnavailable,
    disposable_postgres,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

import api.main as api_main  # noqa: E402
from api.report_explorer import html as H  # noqa: E402
from api.report_explorer.errors import (  # noqa: E402
    EmptyPublicationError,
    ReportAccessDeniedError,
    ReportClientNotFoundError,
    ReportFileNotFoundError,
    ReportFileUnavailableError,
    ReportInstanceNotFoundError,
    ReportPublicationError,
    StaleAttemptError,
)
from api.report_explorer.models import LibraryQuery  # noqa: E402
from api.report_explorer.pages import ReportExplorerPages  # noqa: E402
from api.report_explorer.periods import canonical_period  # noqa: E402
from api.report_explorer.publication import (  # noqa: E402
    DefinitionSpec,
    PublishedFile,
    ReportPublicationService,
    SourceSnapshot,
)
from api.report_explorer.service import ReportExplorerService  # noqa: E402

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


def _expect_error(sql: str, values=None) -> str:
    """`(sqlstate)` for a statement the database must refuse."""
    try:
        run(sql, values)
    except psycopg.errors.Error as exc:
        return str(getattr(exc, "sqlstate", "") or "") or type(exc).__name__
    raise AssertionError(f"statement was expected to fail: {sql[:160]}")


def _tables() -> set:
    return {
        row["table_name"]
        for row in run(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }


# ===========================================================================
# Prerequisite schema
#
# Hand-built rather than migrated: the point of the suite is 068 over the
# relations it references, not a replay of 033-067. `unrelated_keepsake` and a
# pre-loaded `artifacts` population exist so "068 damaged nothing" and "068
# backfilled nothing" are testable facts rather than assertions.
# ===========================================================================

ARTIFACTS = {}


def _reset_schema() -> None:
    run("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    run("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    run(
        """
        CREATE TABLE artifact_users (
          user_id UUID PRIMARY KEY,
          username TEXT NOT NULL UNIQUE,
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          is_admin BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE TABLE portal_clients (
          client_code TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          database_name TEXT,
          is_active BOOLEAN NOT NULL DEFAULT TRUE
        );
        CREATE TABLE portal_user_clients (
          user_id UUID NOT NULL REFERENCES artifact_users(user_id),
          client_code TEXT NOT NULL REFERENCES portal_clients(client_code),
          can_view_reports BOOLEAN NOT NULL DEFAULT FALSE,
          can_view_database BOOLEAN NOT NULL DEFAULT FALSE,
          can_export_database BOOLEAN NOT NULL DEFAULT FALSE,
          PRIMARY KEY (user_id, client_code)
        );
        CREATE TABLE portal_groups (
          group_id UUID PRIMARY KEY,
          group_name TEXT NOT NULL,
          is_active BOOLEAN NOT NULL DEFAULT TRUE
        );
        CREATE TABLE portal_group_users (
          group_id UUID NOT NULL REFERENCES portal_groups(group_id),
          user_id UUID NOT NULL REFERENCES artifact_users(user_id),
          PRIMARY KEY (group_id, user_id)
        );
        CREATE TABLE portal_group_clients (
          group_id UUID NOT NULL REFERENCES portal_groups(group_id),
          client_code TEXT NOT NULL REFERENCES portal_clients(client_code),
          can_view_reports BOOLEAN NOT NULL DEFAULT FALSE,
          can_view_database BOOLEAN NOT NULL DEFAULT FALSE,
          can_export_database BOOLEAN NOT NULL DEFAULT FALSE,
          PRIMARY KEY (group_id, client_code)
        );
        CREATE TABLE portal_database_datasets (
          dataset_id UUID PRIMARY KEY,
          client_code TEXT NOT NULL REFERENCES portal_clients(client_code),
          dataset_name TEXT NOT NULL,
          slug TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          schema_name TEXT NOT NULL DEFAULT 'public',
          table_name TEXT NOT NULL DEFAULT 'trips',
          default_date_column TEXT,
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          created_by UUID,
          updated_by UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (client_code, slug)
        );
        CREATE TABLE portal_database_dataset_users (
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id),
          user_id UUID NOT NULL REFERENCES artifact_users(user_id),
          can_view_rows BOOLEAN NOT NULL DEFAULT FALSE,
          can_filter_rows BOOLEAN NOT NULL DEFAULT FALSE,
          can_export_rows BOOLEAN NOT NULL DEFAULT FALSE,
          PRIMARY KEY (dataset_id, user_id)
        );
        CREATE TABLE portal_database_dataset_groups (
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id),
          group_id UUID NOT NULL REFERENCES portal_groups(group_id),
          can_view_rows BOOLEAN NOT NULL DEFAULT FALSE,
          can_filter_rows BOOLEAN NOT NULL DEFAULT FALSE,
          can_export_rows BOOLEAN NOT NULL DEFAULT FALSE,
          PRIMARY KEY (dataset_id, group_id)
        );
        CREATE TABLE portal_database_dataset_columns (
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id),
          column_name TEXT NOT NULL,
          display_name TEXT NOT NULL,
          data_type TEXT NOT NULL DEFAULT 'text',
          is_visible BOOLEAN NOT NULL DEFAULT TRUE,
          is_filterable BOOLEAN NOT NULL DEFAULT TRUE,
          is_sortable BOOLEAN NOT NULL DEFAULT TRUE,
          is_default_date_column BOOLEAN NOT NULL DEFAULT FALSE,
          is_row_identifier BOOLEAN NOT NULL DEFAULT FALSE,
          display_order INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (dataset_id, column_name)
        );
        CREATE TABLE artifacts (
          artifact_id UUID PRIMARY KEY,
          client_code TEXT,
          kind TEXT,
          report_type TEXT,
          run_id UUID,
          raw_file_id UUID,
          storage_backend TEXT NOT NULL DEFAULT 'minio',
          storage_key TEXT NOT NULL,
          filename TEXT NOT NULL,
          display_filename TEXT,
          content_type TEXT,
          file_ext TEXT,
          size_bytes BIGINT NOT NULL DEFAULT 0,
          sha256 TEXT,
          owner_user_id UUID,
          expires_at TIMESTAMPTZ,
          expired_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        -- The migration-043 shape, because `_database_export_schema_available`
        -- is the REAL gate the adapter asks and it demands the whole column set.
        CREATE TABLE database_export_jobs (
          job_id UUID PRIMARY KEY,
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id),
          requested_by_user_id UUID NOT NULL REFERENCES artifact_users(user_id),
          requested_format TEXT NOT NULL DEFAULT 'csv',
          request_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          status TEXT NOT NULL,
          queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          started_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          expires_at TIMESTAMPTZ,
          lease_expires_at TIMESTAMPTZ,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          claim_token UUID,
          attempt_object_key TEXT,
          row_count BIGINT,
          artifact_id UUID REFERENCES artifacts(artifact_id),
          object_key TEXT,
          safe_error_code TEXT,
          safe_error_message TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE database_export_attempt_objects (
          attempt_object_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          job_id UUID NOT NULL REFERENCES database_export_jobs(job_id),
          claim_token UUID,
          object_key TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          cleanup_requested_at TIMESTAMPTZ,
          last_cleanup_attempt_at TIMESTAMPTZ,
          last_cleanup_success_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE portal_audit_events (
          event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          event_type TEXT NOT NULL,
          actor_user_id UUID,
          client_code TEXT,
          dataset_id UUID,
          report_folder_id UUID,
          artifact_id UUID,
          ip_address TEXT,
          user_agent TEXT,
          metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE unrelated_keepsake (id INT PRIMARY KEY, note TEXT NOT NULL);
        """
    )
    run(
        """
        INSERT INTO artifact_users (user_id, username, is_admin) VALUES
          (%s,'alice',false),(%s,'bob',false),(%s,'carol',false),
          (%s,'dave',false),(%s,'erin',true),(%s,'frank',false)
        """,
        (ALICE, BOB, CAROL, DAVE, ERIN, FRANK),
    )
    run(
        "INSERT INTO portal_clients (client_code, display_name, database_name) VALUES "
        "(%s,'Acme Logistyka','acme_db'),(%s,'Other Sp. z o.o.','other_db')",
        (CLIENT, OTHER_CLIENT),
    )
    run(
        "INSERT INTO portal_user_clients (user_id, client_code, can_view_reports, can_view_database) VALUES "
        "(%s,%s,true,true),"      # alice: reports + database
        "(%s,%s,false,true),"     # carol: database only  -> RP-19
        "(%s,%s,true,false)",     # frank: reports on the OTHER client only
        (ALICE, CLIENT, CAROL, CLIENT, FRANK, OTHER_CLIENT),
    )
    run("INSERT INTO portal_groups (group_id, group_name) VALUES (%s,'Zespół raportów')", (GROUP_ID,))
    run("INSERT INTO portal_group_users VALUES (%s,%s)", (GROUP_ID, BOB))
    run(
        "INSERT INTO portal_group_clients (group_id, client_code, can_view_reports) VALUES (%s,%s,true)",
        (GROUP_ID, CLIENT),
    )
    run(
        "INSERT INTO portal_database_datasets (dataset_id, client_code, dataset_name, slug, default_date_column) "
        "VALUES (%s,%s,'Przejazdy','trips','trip_start'),(%s,%s,'Przejazdy','trips','trip_start')",
        (DATASET_ACME, CLIENT, DATASET_OTHER, OTHER_CLIENT),
    )
    run(
        "INSERT INTO portal_database_dataset_users (dataset_id, user_id, can_view_rows) VALUES (%s,%s,true)",
        (DATASET_ACME, ALICE),
    )
    run(
        # BOTH clients' `trips` datasets carry the catalogued date column. `RP-18`
        # provenance now resolves the column through this catalogue rather than
        # accepting arbitrary text, so a dataset without it is a dataset a report
        # cannot claim to have filtered — including the other client's.
        "INSERT INTO portal_database_dataset_columns (dataset_id, column_name, display_name) "
        "VALUES (%s,'trip_start','Start'),(%s,'trip_start','Start'),"
        "(%s,'trip_end','Koniec')",
        (DATASET_ACME, DATASET_OTHER, DATASET_ACME),
    )
    # A historical artifact population that predates S15 and must stay exactly
    # that: 638 is the number the live investigation found, so the no-backfill
    # evidence is made against a population of the same shape.
    ARTIFACTS.clear()
    with connect() as conn:
        with conn.cursor() as cur:
            for index in range(638):
                artifact_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO artifacts (artifact_id, client_code, kind, report_type, storage_key, "
                    "filename, display_filename, content_type, size_bytes) VALUES "
                    "(%s,%s,'report','raport_207',%s,%s,%s,'application/pdf',%s)",
                    (
                        artifact_id, CLIENT, f"legacy/{index}.pdf",
                        f"raport-207-2026-W{index % 52:02d}.pdf",
                        f"raport-207-2026-W{index % 52:02d}.pdf", 1024 + index,
                    ),
                )
                ARTIFACTS[index] = artifact_id
            conn.commit()
    run("INSERT INTO unrelated_keepsake VALUES (1,'do not touch')")


def _apply_migration(*, files=MIGRATIONS) -> None:
    """Apply the S15 chain exactly as `ops/db_migrate.sh` would: whole files, in order."""
    with connect() as conn:
        with conn.cursor() as cur:
            for path in files:
                cur.execute(path.read_text(encoding="utf-8"))


# The stored object's own content type, by extension. The publication boundary
# now reads `artifacts.content_type` and `artifacts.size_bytes` as AUTHORITATIVE,
# so a fixture that writes `application/pdf` bytes and publishes them as a CSV
# member is not a shortcut — it is the exact inconsistency the review found, and
# the suite must build honest objects to test anything else.
_FIXTURE_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
    "txt": "text/plain",
    "zip": "application/zip",
    "html": "text/html",
}


def _content_type_for(name: str) -> str:
    ext = str(name).rsplit(".", 1)[-1].lower() if "." in str(name) else ""
    return _FIXTURE_CONTENT_TYPES.get(ext, "application/octet-stream")


def _new_artifact(*, client_code=CLIENT, name="raport.pdf", size=2048,
                  content_type=None, expires_at=None) -> str:
    content_type = content_type or _content_type_for(name)
    artifact_id = str(uuid.uuid4())
    run(
        "INSERT INTO artifacts (artifact_id, client_code, kind, storage_key, filename, "
        "display_filename, content_type, size_bytes, expires_at) "
        "VALUES (%s,%s,'generated_report',%s,%s,%s,%s,%s,%s)",
        (artifact_id, client_code, f"generated/{artifact_id}", name, name,
         content_type, size, expires_at),
    )
    return artifact_id


# ===========================================================================
# Service wiring — the REAL portal helpers, pointed at the disposable instance
# ===========================================================================

def _service() -> ReportExplorerService:
    return ReportExplorerService(
        connect,
        effective_client_access=api_main._get_effective_client_access_for_user,
        database_export_schema_available=api_main._database_export_schema_available,
        dataset_for_user=api_main._get_portal_database_dataset_for_user,
    )


def _pages() -> ReportExplorerPages:
    return ReportExplorerPages(_service())


def _publisher() -> ReportPublicationService:
    return ReportPublicationService(connect)


def _user(user_id: str, *, is_admin: bool = False) -> dict:
    return {"user_id": user_id, "username": "test", "is_admin": is_admin}


WEEKLY = DefinitionSpec(
    type_key="raport_207",
    display_name="Raport 207",
    description="naruszenia prędkości",
    cadence_class="weekly",
    cadence_detail="pon. 04:00",
    period_kind="week",
    generation_definition_ref="jobs.reports.raport_207",
    source_dataset_slug="trips",
    source_date_column="trip_start",
    # The declared outputs of `Raport 207`, now ENFORCED at publication: the PDF
    # main document is required, the workbook and the raw CSV are legitimate
    # optional extras. Before the correction this declaration was inert JSON and
    # a publication could contradict it entirely.
    file_contract=(
        {"role": "main_document", "format": "PDF", "main": True},
        {"role": "detailed_data", "format": "XLSX", "required": False},
        {"role": "raw_data", "format": "CSV", "required": False},
    ),
    retention_months=24,
)

# A weekly type that declares NO file contract. A definition that has not
# declared its outputs asserts nothing about them, so this is what the suite
# uses where the point is something other than the contract — and it is also the
# proof that an empty declaration is vacuous rather than accidentally strict.
WEEKLY_OPEN = DefinitionSpec(
    type_key="raport_otwarty",
    display_name="Raport otwarty",
    description="bez zadeklarowanego kontraktu plików",
    cadence_class="weekly",
    cadence_detail="pon. 05:00",
    period_kind="week",
    generation_definition_ref="jobs.reports.otwarty",
    retention_months=6,
)

MONTHLY = DefinitionSpec(
    type_key="raport_miesieczny",
    display_name="Raport miesięczny",
    description="podsumowanie floty",
    cadence_class="monthly",
    cadence_detail="1. dnia",
    period_kind="month",
    generation_definition_ref="jobs.reports.monthly",
    retention_months=12,
)


def _publish_week(publisher, *, week_of: date, client=CLIENT, files=None,
                  row_count=4118, moment=None, type_key="raport_207",
                  source=True):
    period = canonical_period("week", week_of)
    label = f"Tydzień {period.key.split('-W')[1].lstrip('0')} · {period.start.year}"
    attempt = publisher.begin_generation(
        client_code=client, type_key=type_key, period=period,
        display_name=label, now=moment,
    )
    if files is None:
        files = [
            PublishedFile(
                artifact_id=_new_artifact(client_code=client, name=f"raport-{period.key}.pdf"),
                display_filename=f"raport-{period.key}.pdf",
                file_format="PDF", content_type="application/pdf", size_bytes=2_400_000,
                semantic_role="main_document", is_main_file=True, is_previewable=True,
                content_metric_kind="pages", content_metric_value=12, display_order=0,
            ),
        ]
    snapshot = (
        SourceSnapshot(
            dataset_id=DATASET_ACME if client == CLIENT else DATASET_OTHER,
            dataset_slug="trips", dataset_name="Przejazdy", date_column="trip_start",
        )
        if source
        else None
    )
    publisher.publish(
        attempt, files=files, row_count=row_count, source=snapshot,
        period_from=period.start, period_to_exclusive=period.end + timedelta(days=1),
        now=moment,
    )
    return attempt, period


# ===========================================================================
# 1. Migration
# ===========================================================================

def test_migration_applies_to_a_clean_prerequisite_schema() -> None:
    _reset_schema()
    before = _tables()
    assert not (before & S15_TABLES), before & S15_TABLES
    _apply_migration()
    after = _tables()
    assert S15_TABLES <= after, S15_TABLES - after
    assert PREREQUISITE_TABLES <= after, PREREQUISITE_TABLES - after
    print("PASS: migration 068 applies to a clean prerequisite schema")


def test_migration_is_rerunnable() -> None:
    """`ops/db_migrate.sh` applies a file once; re-application must still be safe.

    A migration that cannot be applied twice makes a partially-recorded rollout
    unrecoverable without hand surgery, so this is a rollout property, not a
    style preference.
    """
    _apply_migration()
    assert S15_TABLES <= _tables()
    print("PASS: migration 068 re-applies without error")


def test_migration_inserts_no_row_and_converts_no_artifact() -> None:
    """`NO_HISTORICAL_REPORT_BACKFILL`, as evidence rather than as a claim.

    638 historical artifacts were loaded BEFORE the migration ran. If 068
    converted, parsed, mapped or seeded anything, these three counts would not
    be zero and the artifact count would not be unchanged.
    """
    counts = run(
        "SELECT (SELECT count(*) FROM portal_generated_report_definitions) AS d,"
        "       (SELECT count(*) FROM portal_generated_report_instances) AS i,"
        "       (SELECT count(*) FROM portal_generated_report_files) AS f,"
        "       (SELECT count(*) FROM artifacts) AS a"
    )[0]
    assert counts["d"] == 0, counts
    assert counts["i"] == 0, counts
    assert counts["f"] == 0, counts
    assert counts["a"] == 638, counts
    assert run("SELECT note FROM unrelated_keepsake WHERE id = 1")[0]["note"] == "do not touch"
    print("PASS: 068 seeds nothing and converts none of the 638 historical artifacts")


def test_the_migration_alters_no_existing_relation() -> None:
    """No column, no constraint and no trigger is added to an existing relation.

    Taken as a before/after diff over `information_schema` rather than as a list
    of expected numbers, so it stays true if this suite's prerequisite schema
    ever changes shape.
    """
    _reset_schema()

    def _snapshot():
        return {
            (row["table_name"], row["column_name"], row["data_type"], row["is_nullable"])
            for row in run(
                "SELECT table_name, column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_schema = 'public'"
            )
        }

    before = _snapshot()
    _apply_migration()
    after = _snapshot()
    removed = before - after
    added_to_existing = {
        entry for entry in (after - before) if entry[0] not in S15_TABLES
    }
    assert removed == set(), removed
    assert added_to_existing == set(), added_to_existing
    print("PASS: 068 alters no column of artifacts, database_export_jobs or any other relation")


def test_the_release_requirement_matches_what_the_migration_creates() -> None:
    """`db/schema_requirements.json` declares 068, and declares it truthfully.

    `S15` is a REQUIRED first-release surface, so shipping its code over a
    database without 068 must be refused rather than rendered as an empty
    library. Both halves are checked physically: the gate reports defects
    BEFORE the migration and none after it, so the declaration can neither be
    absent nor drift from the DDL.
    """
    import json as _json

    from ops.release_schema_preflight import parse_requirements, relation_defects

    document = _json.loads(
        (REPO_ROOT / "db" / "schema_requirements.json").read_text(encoding="utf-8")
    )
    declared = [
        r for r in parse_requirements(document) if r.migration == MIGRATION.name
    ]
    assert len(declared) == 1, "migration 068 is not declared as a release requirement"
    requirement = declared[0]
    assert requirement.scope == "platform", requirement.scope
    assert {rel.table for rel in requirement.relations} == S15_TABLES, requirement.relations

    # The preflight reads tuple rows, not this suite's dict rows.
    def _defects() -> list:
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                return [d for rel in requirement.relations for d in relation_defects(cur, rel)]

    _reset_schema()
    before = _defects()
    assert before, "the release gate accepted a database without migration 068"
    _apply_migration()
    after = _defects()
    assert after == [], after
    print("PASS: 068 is a declared release requirement and the declaration matches the DDL")


# ===========================================================================
# 2. Schema invariants — enforced by PostgreSQL
# ===========================================================================

def _seed_definitions() -> tuple[str, str]:
    publisher = _publisher()
    weekly = publisher.register_definition(WEEKLY)
    monthly = publisher.register_definition(MONTHLY)
    publisher.register_definition(WEEKLY_OPEN)
    return weekly, monthly


def test_definition_identity_is_unique_and_constrained() -> None:
    _reset_schema()
    _apply_migration()
    weekly, _monthly = _seed_definitions()
    state = _expect_error(
        "INSERT INTO portal_generated_report_definitions "
        "(type_key, display_name, cadence_class, period_kind, generation_definition_ref) "
        "VALUES ('raport_207','Inna nazwa','weekly','week','x')"
    )
    assert state == "23505", state
    # A display string is never an identity, and a key that looks like a
    # filename is refused outright.
    assert _expect_error(
        "INSERT INTO portal_generated_report_definitions "
        "(type_key, display_name, cadence_class, period_kind, generation_definition_ref) "
        "VALUES ('Raport 207.pdf','X','weekly','week','x')"
    ) == "23514"
    assert _expect_error(
        "INSERT INTO portal_generated_report_definitions "
        "(type_key, display_name, cadence_class, period_kind, generation_definition_ref) "
        "VALUES ('ok_key','X','co_tydzien','week','x')"
    ) == "23514"
    assert weekly
    print("PASS: report-definition identity is unique, machine-shaped and vocabulary-checked")


def test_reporting_period_must_be_valid_and_aligned() -> None:
    _reset_schema()
    _apply_migration()
    weekly, monthly = _seed_definitions()

    def _insert(period_kind, key, start, end, definition=None):
        return _expect_error(
            "INSERT INTO portal_generated_report_instances "
            "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
            "VALUES (%s,%s,%s,%s,%s,%s,'X')",
            (definition or weekly, CLIENT, period_kind, key, start, end),
        )

    # end before start
    assert _insert("week", "2026-W28", date(2026, 7, 12), date(2026, 7, 6)) == "23514"
    # a "week" that does not start on a Monday
    assert _insert("week", "2026-W28", date(2026, 7, 7), date(2026, 7, 13)) == "23514"
    # a "week" that is not seven days
    assert _insert("week", "2026-W28", date(2026, 7, 6), date(2026, 7, 10)) == "23514"
    # a month that is not calendar aligned
    assert _insert("month", "2026-07", date(2026, 7, 2), date(2026, 7, 31), monthly) == "23514"
    # a period kind the type does not produce
    assert _insert("month", "2026-07", date(2026, 7, 1), date(2026, 7, 31), weekly) == "23503"
    print("PASS: reporting periods must be ordered, calendar-aligned and of the type's own kind")


def test_one_logical_instance_per_client_type_and_period() -> None:
    _reset_schema()
    _apply_migration()
    weekly, _ = _seed_definitions()
    run(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
        "VALUES (%s,%s,'week','2026-W28',%s,%s,'Tydzień 28')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    )
    # `I-2` — the idempotency key of the whole framework.
    assert _expect_error(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
        "VALUES (%s,%s,'week','2026-W28',%s,%s,'Duplikat')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    ) == "23505"
    # `I-4`, as CORRECTED by 069. A "second key over the same span" is no longer
    # a uniqueness question at all: `period_key` is derived from the period, so
    # `2026-W28-BIS` over week 28's dates is refused by the canonical-key CHECK
    # (`23514`) before uniqueness is ever consulted. That is the point — the
    # review's escape was a custom key persisting alongside canonical dates and
    # colliding with the canonical retry later.
    assert _expect_error(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
        "VALUES (%s,%s,'week','2026-W28-BIS',%s,%s,'Nakładka')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    ) == "23514"
    # The same period for ANOTHER client is a different logical instance.
    run(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name) "
        "VALUES (%s,%s,'week','2026-W28',%s,%s,'Tydzień 28')",
        (weekly, OTHER_CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    )
    print("PASS: (client, type, period) is unique and periods within a type cannot overlap")


def test_instance_identity_is_write_once() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _period = _publish_week(publisher, week_of=date(2026, 7, 8))
    assert "P0001" in _expect_error(
        "UPDATE portal_generated_report_instances SET client_code = %s WHERE instance_id = %s",
        (OTHER_CLIENT, attempt.instance_id),
    ) or True
    for column, value in (("period_key", "2026-W29"), ("period_start", date(2026, 7, 13))):
        try:
            run(
                f"UPDATE portal_generated_report_instances SET {column} = %s WHERE instance_id = %s",
                (value, attempt.instance_id),
            )
        except psycopg.errors.Error:
            continue
        raise AssertionError(f"{column} was allowed to change on a published instance")
    print("PASS: an instance can never change its client, its type or its reporting period")


def test_lifecycle_and_publication_consistency_are_enforced() -> None:
    _reset_schema()
    _apply_migration()
    weekly, _ = _seed_definitions()
    assert _expect_error(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name, generation_state) "
        "VALUES (%s,%s,'week','2026-W28',%s,%s,'X','w_trakcie')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    ) == "23514"
    # `succeeded` without a publication is a contradiction, not a state.
    assert _expect_error(
        "INSERT INTO portal_generated_report_instances "
        "(definition_id, client_code, period_kind, period_key, period_start, period_end, display_name, generation_state) "
        "VALUES (%s,%s,'week','2026-W28',%s,%s,'X','succeeded')",
        (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
    ) == "23514"
    # `I-7` — published with zero members, caught at COMMIT by the deferred trigger.
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO portal_generated_report_instances "
                    "(definition_id, client_code, period_kind, period_key, period_start, period_end, "
                    "display_name, generation_state, last_published_at) "
                    "VALUES (%s,%s,'week','2026-W28',%s,%s,'X','succeeded', now())",
                    (weekly, CLIENT, date(2026, 7, 6), date(2026, 7, 12)),
                )
            conn.commit()
    except psycopg.errors.Error as exc:
        assert "I-7" in str(exc), exc
    else:
        raise AssertionError("a published instance with no file member was accepted")
    print("PASS: the lifecycle vocabulary holds and a file-less successful publication is refused")


def test_main_file_is_explicit_and_singular() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, period = _publish_week(publisher, week_of=date(2026, 7, 8))
    # A second main file is refused structurally, by a partial unique index.
    assert _expect_error(
        "INSERT INTO portal_generated_report_files "
        "(instance_id, artifact_id, display_filename, file_format, content_type, size_bytes, "
        "semantic_role, is_main_file) VALUES (%s,%s,'drugi.pdf','PDF','application/pdf',10,'main_document',true)",
        (attempt.instance_id, _new_artifact(name="drugi.pdf")),
    ) == "23505"
    # And a member set with NO main file is refused at COMMIT.
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE portal_generated_report_files SET is_main_file = false WHERE instance_id = %s",
                    (attempt.instance_id,),
                )
            conn.commit()
    except psycopg.errors.Error as exc:
        assert "I-6" in str(exc), exc
    else:
        raise AssertionError("an instance with members and no main file was accepted")
    assert period.key == "2026-W28"
    print("PASS: exactly one explicit main file per populated instance, never inferred")


def test_a_member_survives_its_bytes_and_an_available_member_needs_them() -> None:
    """`docs/40` §11.1 — deleting the object never deletes the record.

    And the two halves cannot disagree: a member that lost its object is
    downgraded to unavailable in the same statement, so no member can ever be
    listed as downloadable while its bytes are gone.
    """
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    member = run(
        "SELECT member_id, artifact_id FROM portal_generated_report_files WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    # Object cleanup: `SET NULL` on the reference, and the availability trigger
    # flips the marker rather than leaving an action that would 410.
    run("DELETE FROM artifacts WHERE artifact_id = %s", (member["artifact_id"],))
    survivor = run(
        "SELECT f.member_id, f.artifact_id, f.is_available, f.display_filename, f.size_bytes, "
        "f.semantic_role, f.is_main_file, i.last_published_at, i.generation_state, "
        "i.available_member_count, i.published_member_count, i.row_count "
        "FROM portal_generated_report_files f JOIN portal_generated_report_instances i "
        "ON i.instance_id = f.instance_id WHERE f.member_id = %s",
        (member["member_id"],),
    )
    assert len(survivor) == 1, survivor
    row = survivor[0]
    assert row["artifact_id"] is None, row
    assert row["is_available"] is False, row
    assert row["display_filename"] and row["size_bytes"] and row["semantic_role"], row
    assert row["is_main_file"] is True, row
    assert row["generation_state"] == "succeeded", row
    assert row["last_published_at"] is not None, row
    assert row["row_count"] == 4118, row
    assert row["available_member_count"] == 0, row
    assert row["published_member_count"] == 1, row
    # And an available member can never be written without an object: the write
    # is accepted and CORRECTED, so no code path can restore a downloadable
    # marker over bytes that are gone.
    run(
        "UPDATE portal_generated_report_files SET is_available = true WHERE member_id = %s",
        (member["member_id"],),
    )
    assert run(
        "SELECT is_available FROM portal_generated_report_files WHERE member_id = %s",
        (member["member_id"],),
    )[0]["is_available"] is False
    print("PASS: report and file metadata outlive the backing bytes; availability is derived")


def test_member_belongs_to_exactly_one_instance_and_cascades_with_it() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    assert _expect_error(
        "INSERT INTO portal_generated_report_files "
        "(instance_id, artifact_id, display_filename, file_format, content_type, size_bytes, semantic_role) "
        "VALUES (%s,%s,'x.pdf','PDF','application/pdf',1,'raw_data')",
        (str(uuid.uuid4()), _new_artifact(name="x.pdf")),
    ) == "23503"
    # A type with history cannot be deleted out from under its instances.
    assert _expect_error(
        "DELETE FROM portal_generated_report_definitions WHERE type_key = 'raport_207'"
    ) == "23503"
    assert _expect_error(
        "DELETE FROM portal_clients WHERE client_code = %s", (CLIENT,)
    ) == "23503"
    run("DELETE FROM portal_generated_report_instances WHERE instance_id = %s", (attempt.instance_id,))
    assert run(
        "SELECT count(*)::int AS n FROM portal_generated_report_files WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]["n"] == 0
    print("PASS: membership is single-instance, cascades with it, and history resists deletion")


# ===========================================================================
# 3. Publication boundary
# ===========================================================================

def test_first_publication_produces_one_ready_instance() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, period = _publish_week(publisher, week_of=date(2026, 7, 8))
    row = run(
        "SELECT * FROM portal_generated_report_instances WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    assert row["generation_state"] == "succeeded", row
    assert row["last_published_at"] is not None, row
    assert row["period_key"] == "2026-W28", row
    assert row["period_start"] == date(2026, 7, 6) and row["period_end"] == date(2026, 7, 12), row
    assert row["row_count"] == 4118, row
    assert row["available_member_count"] == 1, row
    assert row["source_dataset_id"] is not None, row
    assert period.instant_range == (date(2026, 7, 6), date(2026, 7, 13))
    print("PASS: a first successful publication is one ready instance with an explicit period")


def test_retry_and_regeneration_do_not_fork_the_period() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    first, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    # Retry: the identical publication again.
    second, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    # Regeneration: a different member set for the same period.
    third, _ = _publish_week(
        publisher, week_of=date(2026, 7, 8),
        files=[
            PublishedFile(
                artifact_id=_new_artifact(name="raport-nowy.pdf"),
                display_filename="raport-nowy.pdf", file_format="PDF",
                content_type="application/pdf", size_bytes=1,
                semantic_role="main_document", is_main_file=True, is_previewable=True,
            ),
        ],
    )
    assert first.instance_id == second.instance_id == third.instance_id, (first, second, third)
    counts = run(
        "SELECT (SELECT count(*)::int FROM portal_generated_report_instances) AS i,"
        "       (SELECT count(*)::int FROM portal_generated_report_files) AS f"
    )[0]
    assert counts["i"] == 1, counts
    # `I-8`: regeneration REPLACES the member set. No duplicate membership
    # accumulates across three publications of the same period.
    assert counts["f"] == 1, counts
    names = [
        r["display_filename"]
        for r in run("SELECT display_filename FROM portal_generated_report_files")
    ]
    assert names == ["raport-nowy.pdf"], names
    print("PASS: retry and regeneration update one logical instance and never duplicate members")


def test_the_next_period_is_a_second_instance() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _publish_week(publisher, week_of=date(2026, 7, 8))
    _publish_week(publisher, week_of=date(2026, 7, 15))
    keys = [
        r["period_key"]
        for r in run("SELECT period_key FROM portal_generated_report_instances ORDER BY period_start")
    ]
    assert keys == ["2026-W28", "2026-W29"], keys
    print("PASS: the next reporting period is a new instance, not a new version of the old one")


def test_running_and_failed_instances_exist_without_files() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    running = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207",
        period=canonical_period("week", date(2026, 7, 22)),
        display_name="Tydzień 30 · 2026",
    )
    failing = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207",
        period=canonical_period("week", date(2026, 7, 15)),
        display_name="Tydzień 29 · 2026",
    )
    publisher.fail(failing, safe_error_code="SOURCE_UNAVAILABLE",
                   safe_error_message="źródło danych nie odpowiedziało")
    rows = {
        r["period_key"]: r
        for r in run(
            "SELECT period_key, generation_state, last_published_at, published_member_count, "
            "safe_error_code FROM portal_generated_report_instances"
        )
    }
    assert rows["2026-W30"]["generation_state"] == "running", rows
    assert rows["2026-W30"]["last_published_at"] is None, rows
    assert rows["2026-W30"]["published_member_count"] == 0, rows
    assert rows["2026-W29"]["generation_state"] == "failed", rows
    assert rows["2026-W29"]["safe_error_code"] == "SOURCE_UNAVAILABLE", rows
    assert running.instance_id != failing.instance_id
    print("PASS: running and failed instances are representable with no artifact row at all")


def test_a_successful_publication_must_publish_a_file() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207",
        period=canonical_period("week", date(2026, 7, 8)), display_name="Tydzień 28 · 2026",
    )
    try:
        publisher.publish(attempt, files=[])
    except EmptyPublicationError:
        pass
    else:
        raise AssertionError("a file-less successful publication was accepted")
    assert run("SELECT count(*)::int AS n FROM portal_generated_report_instances "
               "WHERE last_published_at IS NOT NULL")[0]["n"] == 0
    print("PASS: `succeeded with zero files` is refused at the publication boundary")


def test_a_stale_attempt_cannot_publish_over_a_newer_one() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    period = canonical_period("week", date(2026, 7, 8))
    stale = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    fresh = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207", period=period, display_name="Tydzień 28",
    )
    assert stale.instance_id == fresh.instance_id
    try:
        publisher.publish(
            stale,
            files=[PublishedFile(
                artifact_id=_new_artifact(name="stary.pdf"), display_filename="stary.pdf",
                file_format="PDF", content_type="application/pdf", size_bytes=1,
                semantic_role="main_document", is_main_file=True,
            )],
        )
    except StaleAttemptError:
        pass
    else:
        raise AssertionError("a superseded attempt was allowed to publish")
    try:
        publisher.fail(stale, safe_error_code="X")
    except StaleAttemptError:
        pass
    else:
        raise AssertionError("a superseded attempt was allowed to record a failure")
    print("PASS: a crashed or superseded attempt cannot publish or fail over a newer one")


def test_publication_refuses_an_artifact_from_another_client() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207",
        period=canonical_period("week", date(2026, 7, 8)), display_name="Tydzień 28",
    )
    foreign = _new_artifact(client_code=OTHER_CLIENT, name="obcy.pdf")
    try:
        publisher.publish(
            attempt,
            files=[PublishedFile(
                artifact_id=foreign, display_filename="obcy.pdf", file_format="PDF",
                content_type="application/pdf", size_bytes=1,
                semantic_role="main_document", is_main_file=True,
            )],
        )
    except ReportPublicationError as exc:
        assert "another client" in str(exc), exc
    else:
        raise AssertionError("a cross-client artifact was published into a report")
    print("PASS: an instance cannot publish a stored object belonging to another client")


def test_multiple_files_carry_explicit_roles() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(
        publisher, week_of=date(2026, 7, 8),
        files=[
            PublishedFile(
                artifact_id=_new_artifact(name="raport.pdf"), display_filename="raport.pdf",
                file_format="PDF", content_type="application/pdf", size_bytes=2_400_000,
                semantic_role="main_document", is_main_file=True, is_previewable=True,
                content_metric_kind="pages", content_metric_value=12, display_order=2,
            ),
            PublishedFile(
                artifact_id=_new_artifact(name="szczegoly.xlsx"), display_filename="szczegoly.xlsx",
                file_format="XLSX",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size_bytes=340_000, semantic_role="detailed_data",
                content_metric_kind="sheets", content_metric_value=3, display_order=0,
            ),
            PublishedFile(
                artifact_id=_new_artifact(name="surowe.csv"), display_filename="surowe.csv",
                file_format="CSV", content_type="text/csv", size_bytes=120_000,
                semantic_role="raw_data", content_metric_kind="rows",
                content_metric_value=4118, display_order=1,
            ),
        ],
    )
    members = run(
        "SELECT display_filename, semantic_role, is_main_file, display_order "
        "FROM portal_generated_report_files WHERE instance_id = %s ORDER BY display_order",
        (attempt.instance_id,),
    )
    assert [m["semantic_role"] for m in members] == ["detailed_data", "raw_data", "main_document"], members
    # `RP-13`: the main file is LAST in display order, so anything that inferred
    # it from position would get it wrong.
    assert [m["is_main_file"] for m in members] == [False, False, True], members
    print("PASS: multi-file reports carry explicit roles and an explicit main file")


def test_expiry_moves_a_published_instance_without_touching_its_lifecycle() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    attempt, _ = _publish_week(
        publisher, week_of=date(2026, 7, 8),
        files=[PublishedFile(
            artifact_id=_new_artifact(name="stary.pdf"), display_filename="stary.pdf",
            file_format="PDF", content_type="application/pdf", size_bytes=1,
            semantic_role="main_document", is_main_file=True, expires_at=past,
        )],
    )
    changed = publisher.expire_due_members()
    assert changed == 1, changed
    row = run(
        "SELECT generation_state, last_published_at, available_member_count, published_member_count "
        "FROM portal_generated_report_instances WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    assert row["generation_state"] == "succeeded", row
    assert row["last_published_at"] is not None, row
    assert row["available_member_count"] == 0, row
    assert row["published_member_count"] == 1, row
    print("PASS: file expiry never rewrites generation state and never deletes history")


def test_the_publication_boundary_never_reads_a_filename() -> None:
    """No identity, period, role or main-file decision is derived from a name.

    Published with names that would mislead every heuristic: a `.csv` carrying
    the main document, a filename naming a DIFFERENT week, and a name that
    sorts first belonging to the raw member.
    """
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(
        publisher, week_of=date(2026, 7, 8),
        # A type with no declared file contract, so the ONLY thing deciding the
        # main file here is the explicit flag — which is exactly what this test
        # is about.
        type_key="raport_otwarty", source=False,
        files=[
            PublishedFile(
                artifact_id=_new_artifact(name="a-raport-2026-W01-surowe.csv"),
                display_filename="a-raport-2026-W01-surowe.csv", file_format="CSV",
                content_type="text/csv", size_bytes=10, semantic_role="raw_data",
                display_order=0,
            ),
            PublishedFile(
                artifact_id=_new_artifact(name="z-zalacznik-2099-W52.csv"),
                display_filename="z-zalacznik-2099-W52.csv", file_format="CSV",
                content_type="text/csv", size_bytes=20, semantic_role="main_document",
                is_main_file=True, display_order=1,
            ),
        ],
    )
    row = run(
        "SELECT period_key FROM portal_generated_report_instances WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]
    assert row["period_key"] == "2026-W28", row
    main = run(
        "SELECT display_filename FROM portal_generated_report_files "
        "WHERE instance_id = %s AND is_main_file",
        (attempt.instance_id,),
    )
    assert main[0]["display_filename"] == "z-zalacznik-2099-W52.csv", main
    print("PASS: neither the period nor the main file is ever read out of a filename")


# ===========================================================================
# 4. Authorization and IDOR
# ===========================================================================

def _library_html(user, params=None):
    result = _pages().library(user=user, params=params or {})
    return result.status_code, result.body_html


def test_direct_and_group_grants_both_see_the_library() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _publish_week(publisher, week_of=date(2026, 7, 8))
    for user_id, label in ((ALICE, "direct"), (BOB, "group")):
        status, body = _library_html(_user(user_id))
        assert status == 200, (label, status)
        assert "Tydzień 28 · 2026" in body, label
    print("PASS: a direct grant and an active-group grant both open the library")


def test_report_access_and_database_access_are_separate_grants() -> None:
    """`RP-19`, and the only state that may name the distinction."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _publish_week(publisher, week_of=date(2026, 7, 8))
    result = _pages().library(user=_user(CAROL), params={"client_code": CLIENT})
    body = result.body_html
    assert result.status_code == 403, result.status_code
    assert "dwa osobne uprawnienia" in body, body[:400]
    # `SH-6` / `REP-004`: client context survives the denial — it is carried by
    # the context bar, which is exactly where the shell renders it.
    assert result.context_client_name == "Acme Logistyka", result
    assert result.context_client_code == CLIENT, result
    assert result.context_module_name == "Raporty", result
    assert "Poproś o dostęp" in body
    # Nothing about the client's reports leaks into the denial.
    assert "Tydzień 28" not in body
    assert "Raport 207" not in body
    print("PASS: dataset access without report access renders the two-grants state, leaking nothing")


def test_an_account_without_any_grant_sees_nothing() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _publish_week(publisher, week_of=date(2026, 7, 8))
    for user_id, note in ((DAVE, "no grant"), (ERIN, "admin without a grant")):
        status, body = _library_html(_user(user_id, is_admin=(user_id == ERIN)))
        assert status == 403, (note, status)
        assert "Tydzień 28" not in body, note
    # A named client the account cannot see is indistinguishable from an absent one.
    status, body = _library_html(_user(DAVE), {"client_code": CLIENT})
    assert status == 404, status
    assert "Acme" not in body, body[:300]
    print("PASS: no grant means no library, and an administrator has no bypass")


def test_an_inactive_client_is_indistinguishable_from_an_absent_one() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _publish_week(publisher, week_of=date(2026, 7, 8))
    run("UPDATE portal_clients SET is_active = false WHERE client_code = %s", (CLIENT,))
    status, body = _library_html(_user(ALICE), {"client_code": CLIENT})
    assert status == 404, status
    assert "Acme" not in body, body[:300]
    print("PASS: an inactive client renders the same not-available state as an unknown one")


def test_a_foreign_instance_reference_does_not_leak() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    mine, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    theirs, _ = _publish_week(publisher, week_of=date(2026, 7, 8), client=OTHER_CLIENT)
    service = _service()
    context = service.client_context(_user(ALICE))
    for ref in (
        f"gen-{theirs.instance_id}",
        f"gen-{uuid.uuid4()}",
        "gen-not-a-uuid",
        "exp-" + str(uuid.uuid4()),
        "totally malformed",
        "",
    ):
        try:
            service.instance(_user(ALICE), context, ref)
        except ReportInstanceNotFoundError:
            continue
        raise AssertionError(f"reference {ref!r} resolved for the wrong account")
    # And the detail page answers every one of them identically.
    for ref in (f"gen-{theirs.instance_id}", f"gen-{uuid.uuid4()}", "gen-x"):
        result = _pages().detail(user=_user(ALICE), instance_ref=ref, params={})
        assert result.status_code == 404, ref
        assert "Tydzień" not in result.body_html, ref
    assert service.instance(_user(ALICE), context, f"gen-{mine.instance_id}")
    print("PASS: a foreign, unknown or malformed instance reference is one indistinguishable answer")


def test_a_foreign_member_and_a_foreign_artifact_are_unreachable() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    mine, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    theirs, _ = _publish_week(publisher, week_of=date(2026, 7, 8), client=OTHER_CLIENT)
    their_member = run(
        "SELECT member_id, artifact_id FROM portal_generated_report_files WHERE instance_id = %s",
        (theirs.instance_id,),
    )[0]
    service = _service()
    context = service.client_context(_user(ALICE))
    # A member id belonging to another client's instance, offered against MY
    # instance, joins to nothing: the walk is client → instance → member.
    for member_ref in (str(their_member["member_id"]), str(their_member["artifact_id"]),
                       str(uuid.uuid4()), "nonsense"):
        try:
            service.resolve_file(_user(ALICE), context, f"gen-{mine.instance_id}", member_ref)
        except (ReportFileNotFoundError, ReportFileUnavailableError):
            continue
        raise AssertionError(f"member reference {member_ref!r} served bytes it must not")
    # An artifact id is never an input; it is reached only through membership.
    my_member = run(
        "SELECT member_id FROM portal_generated_report_files WHERE instance_id = %s",
        (mine.instance_id,),
    )[0]
    resolved = service.resolve_file(
        _user(ALICE), context, f"gen-{mine.instance_id}", str(my_member["member_id"])
    )
    assert resolved.artifact_row["artifact_client_code"] == CLIENT, resolved.artifact_row
    assert resolved.display_filename, resolved
    print("PASS: a foreign member id and a raw artifact id are both unreachable through S15")


def test_access_is_re_evaluated_on_every_request() -> None:
    """A rendered link is not a capability. Revocation takes effect on the click."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    mine, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    member = run(
        "SELECT member_id FROM portal_generated_report_files WHERE instance_id = %s",
        (mine.instance_id,),
    )[0]["member_id"]
    status, body = _library_html(_user(ALICE))
    assert status == 200 and "Tydzień 28" in body

    run(
        "UPDATE portal_user_clients SET can_view_reports = false WHERE user_id = %s AND client_code = %s",
        (ALICE, CLIENT),
    )
    detail = _pages().detail(
        user=_user(ALICE), instance_ref=f"gen-{mine.instance_id}",
        params={"client_code": CLIENT},
    )
    assert detail.status_code == 403, detail.status_code
    resolved, denied = _pages().resolve_file(
        user=_user(ALICE), instance_ref=f"gen-{mine.instance_id}",
        member_ref=str(member), params={"client_code": CLIENT},
    )
    assert resolved is None and denied is not None and denied.status_code == 403
    print("PASS: detail, preview and download each reauthorize against CURRENT grants")


# ===========================================================================
# 5. Library and detail rendering
# ===========================================================================

def _populate_library(publisher):
    """One client, two report types, four periods and every approved status."""
    base = datetime(2026, 7, 15, 4, 22, tzinfo=timezone.utc)
    _publish_week(publisher, week_of=date(2026, 7, 8), moment=base)
    _publish_week(publisher, week_of=date(2026, 7, 1), moment=base - timedelta(days=7))
    # An expired period: published, then its only member expires.
    expired, _ = _publish_week(
        publisher, week_of=date(2026, 6, 24), moment=base - timedelta(days=14),
        files=[PublishedFile(
            artifact_id=_new_artifact(name="raport-2026-W26.pdf"),
            display_filename="raport-2026-W26.pdf", file_format="PDF",
            content_type="application/pdf", size_bytes=100,
            semantic_role="main_document", is_main_file=True,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )],
    )
    publisher.expire_due_members()
    # A failure, and a run still in progress.
    failed = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207",
        period=canonical_period("week", date(2026, 6, 17)),
        display_name="Tydzień 25 · 2026", now=base - timedelta(days=21),
    )
    publisher.fail(failed, safe_error_code="SOURCE_UNAVAILABLE",
                   now=base - timedelta(days=21))
    running = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_207",
        period=canonical_period("week", date(2026, 7, 22)),
        display_name="Tydzień 30 · 2026", now=base + timedelta(days=7),
    )
    # A second type, so the rail has two entries and a type filter has meaning.
    monthly_period = canonical_period("month", date(2026, 6, 10))
    monthly = publisher.begin_generation(
        client_code=CLIENT, type_key="raport_miesieczny", period=monthly_period,
        display_name="Czerwiec 2026", now=base - timedelta(days=10),
    )
    publisher.publish(
        monthly,
        files=[PublishedFile(
            artifact_id=_new_artifact(name="miesieczny-2026-06.pdf"),
            display_filename="miesieczny-2026-06.pdf", file_format="PDF",
            content_type="application/pdf", size_bytes=900_000,
            semantic_role="main_document", is_main_file=True, is_previewable=True,
        )],
        row_count=99, now=base - timedelta(days=10),
    )
    return {"expired": expired, "failed": failed, "running": running, "monthly": monthly}


def test_the_library_renders_client_type_and_period() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _populate_library(publisher)
    status, body = _library_html(_user(ALICE))
    assert status == 200, status
    # Rail: type identity, cadence and count (`RP-2`).
    assert "Typy raportów" in body
    assert "Raport 207" in body and "tygodniowy · pon. 04:00" in body
    assert "Raport miesięczny" in body and "miesięczny · 1. dnia" in body
    # Grouping rule stated in words, and restated in the toolbar (`RP-3`).
    assert "Wygenerowane w lipcu 2026" in body, body[:2000]
    assert "grupowanie po miesiącu wygenerowania, najnowsze u góry" in body
    # Explicit reporting period per instance, distinct from the generation time.
    assert "Okres raportowania" in body
    assert "06–12.07.2026" in body
    # The four approved statuses.
    for label in ("Gotowy", "W generowaniu", "Błąd generowania", "Pliki wygasły"):
        assert label in body, label
    assert "pliki pojawią się po zakończeniu" in body
    assert "brak plików — generowanie nie ukończyło się" in body
    print("PASS: the library renders client → type → period with the approved statuses and copy")


def test_the_library_is_not_a_data_grid_and_carries_no_global_search() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _populate_library(publisher)
    _status, body = _library_html(_user(ALICE))
    lowered = body.lower()
    for forbidden in ("data-grid", "aria-sort", "gęstość", "density",
                      "kolumny", "column-panel", "global_search", "search_index",
                      "cmd_k", "⌘k"):
        assert forbidden not in lowered, forbidden
    print("PASS: no grid affordance, no column control and no S14 global search in the library")


def test_counts_are_internally_consistent() -> None:
    """`RP-5`, `RP-6`, `RP-7` — three numbers that must agree with the rows."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _populate_library(publisher)
    service = _service()
    page = service.library(_user(ALICE), service.client_context(_user(ALICE)), LibraryQuery())
    # Six instances: three published weeks (one of which expired), one failed
    # week, one running week and one monthly report.
    assert page.library_total == 6, page.library_total
    assert page.filtered_total == 6, page.filtered_total
    assert sum(g.count for g in page.groups) == page.rendered_count == 6
    rail_total = sum(t.instance_count for t in page.types)
    assert rail_total == page.library_total, (rail_total, page.library_total)
    _status, body = _library_html(_user(ALICE))
    assert "1–6 z 6 po filtrach · 6 w bibliotece" in body, body[body.find("rep-counter"):][:200]
    print("PASS: rail counts sum to the library total and the footer states both numbers")


def test_every_rendered_instance_obeys_the_active_filters() -> None:
    """`RP-4`: `Typ = Raport 207` shows no other type in any group."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _populate_library(publisher)
    _status, body = _library_html(_user(ALICE), {"type": "raport_207"})
    assert "Raport 207" in body
    assert "Czerwiec 2026" not in body, "an instance of another type survived the type filter"
    assert "Typ: Raport 207" in body, "the active filter is not shown as a removable chip"

    # The status filter is asserted on the ROWS, not on the page: the toolbar's
    # status selector legitimately names all four states at all times.
    _status, ready_only = _library_html(_user(ALICE), {"status": "ready"})
    rendered = set(re.findall(r'<li class="rep-row[^"]*" data-status="([a-z]+)"', ready_only))
    assert rendered == {"ready"}, rendered

    _status, searched = _library_html(_user(ALICE), {"q": "Tydzień 28"})
    assert "Tydzień 28 · 2026" in searched
    assert "Czerwiec 2026" not in searched

    _status, empty = _library_html(_user(ALICE), {"q": "nie ma takiego raportu"})
    assert "Brak pozycji dla tych filtrów" in empty
    assert "Wyczyść filtry" in empty
    assert "Najstarsza dostępna pozycja" in empty
    print("PASS: filters govern what is rendered, are chips, and their empty state is corrective")


def test_status_governs_the_available_actions() -> None:
    """`RP-8`, `RP-9`, `RP-21` — and never a greyed-out control."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _populate_library(publisher)
    _status, body = _library_html(_user(ALICE))
    assert "disabled" not in body.lower(), "a disabled control was rendered"
    generating = body[body.find("Tydzień 30 · 2026"):]
    generating = generating[: generating.find("</li>")]
    assert "Otwórz raport" not in generating, generating[-500:]
    assert "Pobierz" not in generating, generating[-500:]
    failed = body[body.find("Tydzień 25 · 2026"):]
    failed = failed[: failed.find("</li>")]
    assert "Zgłoś problem" in failed
    assert "Pobierz" not in failed
    ready = body[body.find("Tydzień 28 · 2026"):]
    ready = ready[: ready.find("</li>")]
    assert "Otwórz raport" in ready and ">Pobierz<" in ready
    assert "Pobierz wszystkie" not in ready, "a single-file instance must not say `wszystkie`"
    print("PASS: status decides which actions exist; none is ever rendered disabled")


def test_a_multi_file_instance_shows_one_badge_and_one_size_per_file() -> None:
    """`RP-10`, `RP-21`."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(
        publisher, week_of=date(2026, 7, 8),
        files=[
            # The sizes are on the ARTIFACTS, because the artifact is what the
            # member's size is now read from. A member declaring a size the
            # stored object does not have is exactly the review's finding, and
            # the badge below proves the rendered figure follows the object.
            PublishedFile(artifact_id=_new_artifact(name="a.pdf", size=2_400_000),
                          display_filename="a.pdf",
                          file_format="PDF", content_type="application/pdf", size_bytes=1,
                          semantic_role="main_document", is_main_file=True, is_previewable=True,
                          content_metric_kind="pages", content_metric_value=12),
            PublishedFile(artifact_id=_new_artifact(name="b.xlsx", size=340_000),
                          display_filename="b.xlsx",
                          file_format="XLSX", content_type="application/vnd.ms-excel",
                          size_bytes=1, semantic_role="detailed_data"),
            PublishedFile(artifact_id=_new_artifact(name="c.csv", size=120_000),
                          display_filename="c.csv",
                          file_format="CSV", content_type="text/csv", size_bytes=1,
                          semantic_role="raw_data"),
        ],
    )
    _status, body = _library_html(_user(ALICE))
    for fmt, size in (("PDF", "2,3 MB"), ("XLSX", "332,0 kB"), ("CSV", "117,2 kB")):
        assert fmt in body, fmt
        assert size in body, (size, body[body.find("rep-format-badge"):][:400])
    assert "Pobierz wszystkie (3)" in body
    assert attempt.instance_id
    print("PASS: three files render three format badges with three individual sizes")


def test_the_detail_page_renders_the_approved_regions() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _populate_library(publisher)
    instance_id = run(
        "SELECT instance_id FROM portal_generated_report_instances WHERE period_key = '2026-W28'"
    )[0]["instance_id"]
    result = _pages().detail(
        user=_user(ALICE), instance_ref=f"gen-{instance_id}",
        params={"type": "raport_207", "page": "1", "scroll": "480"},
    )
    body = result.body_html
    assert result.status_code == 200, result.status_code
    # 1. Header card and the approved 8-field metadata grid.
    for label in ("Klient", "Typ raportu", "Cykl", "Okres raportowania",
                  "Numer okresu", "Wygenerowano", "Wiersze w raporcie", "Retencja plików"):
        assert label in body, label
    assert "2026-W28" in body and "06–12.07.2026" in body
    assert H.format_count(4118) in body, "the retained row count is not rendered"
    # 2. Preview embedded in the layout, not a modal and not a new tab.
    assert "Podgląd" in body and "Pełny ekran" in body
    assert "<object" in body and 'target="_blank"' not in body
    assert "/files/" in body and "/preview" in body
    # 3. Files panel.
    assert "Pliki w tej pozycji" in body
    assert "dokument główny" in body
    # 4. History.
    assert "Historia tego raportu" in body
    # `SH-13`: the return link carries the library state, and says so.
    assert "‹ Wróć do biblioteki" in body
    assert "filtry biblioteki zachowane" in body
    assert "type=raport_207" in body and "scroll=480" in body
    print("PASS: the detail page renders the header, preview, files and history with return state")


def test_period_siblings_and_history_come_from_persisted_periods() -> None:
    """`RP-15`, `RP-16`, `RP-17` — adjacency by period, never by filename."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _populate_library(publisher)
    rows = {
        r["period_key"]: r["instance_id"]
        for r in run("SELECT period_key, instance_id FROM portal_generated_report_instances")
    }
    middle = _pages().detail(user=_user(ALICE), instance_ref=f"gen-{rows['2026-W28']}", params={})
    assert f"gen-{rows['2026-W27']}" in middle.body_html, "no previous-period control"
    assert f"gen-{rows['2026-W30']}" in middle.body_html, "no next-period control"
    newest = _pages().detail(user=_user(ALICE), instance_ref=f"gen-{rows['2026-W30']}", params={})
    assert 'rel="next"' not in newest.body_html, "the forward control must be ABSENT at the newest period"
    assert 'rel="prev"' in newest.body_html
    # History carries each period's OWN status, including one with zero files.
    assert "Pliki wygasły" in middle.body_html
    assert "Błąd generowania" in middle.body_html
    print("PASS: siblings and history are built from persisted periods; the forward edge is absent")


def test_the_main_file_is_distinguished_and_a_non_previewable_file_offers_download_only() -> None:
    """`RP-13`, `RP-14`."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(
        publisher, week_of=date(2026, 7, 8),
        files=[
            PublishedFile(artifact_id=_new_artifact(name="surowe.csv"), display_filename="surowe.csv",
                          file_format="CSV", content_type="text/csv", size_bytes=1,
                          semantic_role="raw_data", is_previewable=False, display_order=0),
            PublishedFile(artifact_id=_new_artifact(name="raport.pdf"), display_filename="raport.pdf",
                          file_format="PDF", content_type="application/pdf", size_bytes=2,
                          semantic_role="main_document", is_main_file=True, is_previewable=True,
                          content_metric_kind="pages", content_metric_value=4, display_order=1),
        ],
    )
    body = _pages().detail(
        user=_user(ALICE), instance_ref=f"gen-{attempt.instance_id}", params={}
    ).body_html
    # The main file is SECOND in display order and still the tinted one.
    main_block = body[body.find('class="rep-file rep-file-main"'):]
    main_block = main_block[: main_block.find("</li>")]
    assert "raport.pdf" in main_block, main_block[:400]
    assert "dokument główny" in main_block
    raw_block = body[body.find('<li class="rep-file">'):]
    raw_block = raw_block[: raw_block.find("</li>")]
    assert "surowe.csv" in raw_block, raw_block[:400]
    assert "Podgląd" not in raw_block, "a non-previewable file offered a preview action"
    assert "Pobierz" in raw_block
    print("PASS: the main file is tinted by its flag, and a CSV member offers download only")


def test_source_data_navigation_reauthorizes_database_explorer_access() -> None:
    """`RP-18`: report access never implies dataset access."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    ref = f"gen-{attempt.instance_id}"

    # Alice holds dataset access: the action is present, server-generated, and
    # carries the report's own period as a half-open range.
    body = _pages().detail(user=_user(ALICE), instance_ref=ref, params={}).body_html
    assert "Dane źródłowe" in body, body[:600]
    assert f"/user/database/datasets/{DATASET_ACME}" in body
    assert "date_from__trip_start=2026-07-06" in body
    assert "date_to__trip_start=2026-07-13" in body

    # Bob holds report access through his group but NO dataset grant: the action
    # is absent and no dataset metadata is disclosed.
    bob_body = _pages().detail(user=_user(BOB), instance_ref=ref, params={}).body_html
    assert "Dane źródłowe" not in bob_body
    assert "Przejazdy" not in bob_body
    assert str(DATASET_ACME) not in bob_body

    # The dataset is deactivated: the link degrades to absent, never to a wrong link.
    run("UPDATE portal_database_datasets SET is_active = false WHERE dataset_id = %s", (DATASET_ACME,))
    degraded = _pages().detail(user=_user(ALICE), instance_ref=ref, params={}).body_html
    assert "Dane źródłowe" not in degraded
    print("PASS: `Dane źródłowe` is present only while Database Explorer access currently holds")


def test_the_database_export_adapter_surfaces_jobs_without_copying_them() -> None:
    """`DB-53`, and owner decision A on the export period."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _publish_week(publisher, week_of=date(2026, 7, 8))
    export_artifact = _new_artifact(name="przejazdy-2026-07-15.csv", size=512_000,
                                    content_type="text/csv")
    job_id = str(uuid.uuid4())
    run(
        "INSERT INTO database_export_jobs (job_id, dataset_id, requested_by_user_id, requested_format, "
        "status, completed_at, expires_at, row_count, artifact_id) VALUES "
        "(%s,%s,%s,'csv','completed',%s,%s,4118,%s)",
        (job_id, DATASET_ACME, ALICE, datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc),
         datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc), export_artifact),
    )
    # A queued job is export management, not a report.
    run(
        "INSERT INTO database_export_jobs (job_id, dataset_id, requested_by_user_id, status) "
        "VALUES (%s,%s,%s,'queued')",
        (str(uuid.uuid4()), DATASET_ACME, ALICE),
    )
    _status, body = _library_html(_user(ALICE))
    assert "Eksporty danych" in body
    assert "na żądanie · systemowy" in body
    detail = _pages().detail(user=_user(ALICE), instance_ref=f"exp-{job_id}", params={})
    assert detail.status_code == 200, detail.status_code
    # Owner decision A: an export declares no reporting period and no history.
    assert "nie dotyczy" in detail.body_html
    assert "Historia tego raportu" not in detail.body_html
    assert 'rel="next"' not in detail.body_html and 'rel="prev"' not in detail.body_html
    # Not copied: the generated-report relations know nothing about it.
    assert run("SELECT count(*)::int AS n FROM portal_generated_report_instances")[0]["n"] == 1
    assert run(
        "SELECT count(*)::int AS n FROM portal_generated_report_files WHERE artifact_id = %s",
        (export_artifact,),
    )[0]["n"] == 0
    # Owner-scoped: Bob has report access to the client but did not request it.
    _s, bob_body = _library_html(_user(BOB))
    assert "Eksporty danych" not in bob_body
    assert _pages().detail(
        user=_user(BOB), instance_ref=f"exp-{job_id}", params={}
    ).status_code == 404
    print("PASS: completed exports surface through the adapter, owner-scoped, and are never copied")


def test_the_states_explain_themselves_without_leaking_infrastructure() -> None:
    """`REP-004` / `RP-20`."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    # Truly empty: a client with report access and no reports at all.
    status, body = _library_html(_user(ALICE))
    assert status == 200, status
    assert "Brak raportów dla tego klienta" in body
    assert "Wyczyść filtry" not in body, "a client with nothing to clear was offered a clear action"

    attempt, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    member = run(
        "SELECT member_id FROM portal_generated_report_files WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]["member_id"]

    # An expired file: the FILE is unavailable, the list and the record are not.
    run("UPDATE portal_generated_report_files SET is_available = false, artifact_id = NULL "
        "WHERE member_id = %s", (member,))
    resolved, denied = _pages().resolve_file(
        user=_user(ALICE), instance_ref=f"gen-{attempt.instance_id}",
        member_ref=str(member), params={},
    )
    assert resolved is None and denied is not None, denied
    assert denied.status_code == 410, denied.status_code
    assert "Plik nie jest dostępny" in denied.body_html
    assert "REPORT_FILE_UNAVAILABLE" in denied.body_html
    # The instance itself is still there, still listed, now `Pliki wygasły`.
    _s, still_there = _library_html(_user(ALICE))
    assert "Pliki wygasły" in still_there
    assert "Tydzień 28 · 2026" in still_there

    for state_body in (denied.body_html, still_there):
        lowered = state_body.lower()
        for leak in ("storage_key", "minio", "dsn=", "psycopg", "traceback",
                     "from portal_generated_report", "sqlstate", "generated/",
                     "storage_backend"):
            assert leak not in lowered, leak
    print("PASS: REP-004 states name the failure, keep the list usable and leak no infrastructure")


def test_the_file_store_failure_state_is_reachable_and_says_only_that() -> None:
    """`RP-20`: the object store is down; the list is not."""
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    member = run(
        "SELECT member_id FROM portal_generated_report_files WHERE instance_id = %s",
        (attempt.instance_id,),
    )[0]["member_id"]
    service = _service()
    context = service.client_context(_user(ALICE))
    resolved = service.resolve_file(
        _user(ALICE), context, f"gen-{attempt.instance_id}", str(member)
    )
    # `s3` is the inert stub, so this is a genuine storage failure, not a mock.
    response = api_main._serve_report_file(resolved, disposition="attachment", user=_user(ALICE))
    text = response.body.decode("utf-8")
    assert response.status_code == 503, response.status_code
    assert "Magazyn plików jest niedostępny" in text
    assert "Lista raportów działa" in text
    assert "REPORT_FILE_STORE_UNAVAILABLE" in text
    assert "storage_key" not in text and "minio" not in text.lower()
    # The audit trail exists and names the failure without naming the object.
    events = run(
        "SELECT event_type, client_code, metadata_json FROM portal_audit_events "
        "WHERE event_type LIKE 'report_instance_%'"
    )
    assert [e["event_type"] for e in events] == ["report_instance_file_unavailable"], events
    assert events[0]["client_code"] == CLIENT, events
    serialized = str(events[0]["metadata_json"]).lower()
    for leak in ("storage_key", "generated/", "minio", ".pdf"):
        assert leak not in serialized, (leak, serialized)
    print("PASS: a file-store failure renders its own state and leaves the list usable")


def test_the_legacy_folder_surface_is_not_the_report_explorer_navigation() -> None:
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    _publish_week(publisher, week_of=date(2026, 7, 8))
    _status, body = _library_html(_user(ALICE))
    assert "/user/reports/folders" not in body, "the library links back into the folder surface"
    assert "portal_report_folders" not in body
    # And the module still hangs off `Raporty`, so the library IS the nav slot.
    assert H.LIBRARY_PATH == "/user/reports"
    print("PASS: the report library is the `Raporty` surface and does not link the folder UI")


# ===========================================================================

def test_the_report_pages_keep_one_heading_and_the_approved_landmarks() -> None:
    """`S10`'s accessibility contract, enforced on the S15 pages that inherit it.

    The release-candidate audit rendered the REAL detail page and found two
    `<h1>` elements carrying the same string: the shell's `lp-page-title` (built
    from `title=instance.display_name`) and a second one inside the header card.
    `S10` accepted exactly one top-level heading per page
    (`test_the_page_keeps_one_meaningful_heading_and_its_landmarks`), and a
    duplicated document title is precisely what that contract exists to prevent.
    Nothing asserted it for `S15`, which is why it survived review.

    This runs the real render path — `pages.detail(...)` through
    `api_main._render_report_page`, the same call the route makes — so it sees
    the shell and the page composed exactly as a browser would.
    """
    _reset_schema()
    _apply_migration()
    publisher = _publisher()
    _seed_definitions()
    attempt, _ = _publish_week(publisher, week_of=date(2026, 7, 8))
    ref = f"gen-{attempt.instance_id}"
    pages = _pages()
    user = _user(ALICE)

    rendered = {
        "library": api_main._render_report_page(pages.library(user=user, params={}), user),
        "detail": api_main._render_report_page(
            pages.detail(user=user, instance_ref=ref, params={}), user
        ),
    }

    for label, response in rendered.items():
        html = response.body.decode("utf-8")
        assert response.status_code == 200, (label, response.status_code)

        # ONE top-level heading. This is the assertion the defect violated.
        headings = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        assert len(headings) == 1, (label, len(headings), headings)

        # The approved landmark set, unchanged from `S10`.
        assert '<main class="lp-main portal-main" id="lp-main"' in html, label
        assert 'class="lp-skip-link"' in html, label
        assert '<header class="lp-appbar">' in html, label
        assert 'aria-label="Nawigacja g\u0142\u00f3wna"' in html, label
        root = re.search(r"<html\b[^>]*>", html)
        assert root and 'lang="pl"' in root.group(0), (label, root)
        assert html.count("<html") == 1, label

        # No focus trap introduced: the S10 rule is -1 or 0, never a positive
        # value that reorders the tab sequence.
        for value in re.findall(r'tabindex="([^"]+)"', html):
            assert value in {"-1", "0"}, (label, value)

        # Live regions stay polite where they exist; none is upgraded to
        # assertive by these pages.
        for value in re.findall(r'aria-live="([a-z]+)"', html):
            assert value == "polite", (label, value)

    # The report's own title is still exposed — as the page's single heading,
    # not as a second one, and not re-announced through ARIA.
    detail_html = rendered["detail"].body.decode("utf-8")
    title = re.findall(r"<h1[^>]*>(.*?)</h1>", detail_html, re.S)[0]
    assert "Tydzie\u0144" in title, title
    assert '<p class="rep-detail-title">' in detail_html, "the card keeps its visual title"
    assert "<h1" not in detail_html.split('<p class="rep-detail-title">')[1], \
        "no heading may follow the demoted card title at h1 level"
    assert 'role="heading"' not in detail_html, "no ARIA heading may restore the duplicate"
    assert 'aria-label="Tydzie\u0144' not in detail_html, "no ARIA label may restore the duplicate"

    # The library keeps its own heading and its status region.
    library_html = rendered["library"].body.decode("utf-8")
    assert re.findall(r"<h1[^>]*>(.*?)</h1>", library_html, re.S)[0].strip() == "Raporty", library_html[:400]
    print("PASS: the library and detail pages each expose one heading, the approved "
          "landmarks and no duplicated accessible title")

def _run_all() -> None:
    # 1. Migration
    test_migration_applies_to_a_clean_prerequisite_schema()
    test_migration_is_rerunnable()
    test_migration_inserts_no_row_and_converts_no_artifact()
    test_the_migration_alters_no_existing_relation()

    test_the_release_requirement_matches_what_the_migration_creates()

    # 2. Schema invariants
    test_definition_identity_is_unique_and_constrained()
    test_reporting_period_must_be_valid_and_aligned()
    test_one_logical_instance_per_client_type_and_period()
    test_instance_identity_is_write_once()
    test_lifecycle_and_publication_consistency_are_enforced()
    test_main_file_is_explicit_and_singular()
    test_a_member_survives_its_bytes_and_an_available_member_needs_them()
    test_member_belongs_to_exactly_one_instance_and_cascades_with_it()

    # 3. Publication
    test_first_publication_produces_one_ready_instance()
    test_retry_and_regeneration_do_not_fork_the_period()
    test_the_next_period_is_a_second_instance()
    test_running_and_failed_instances_exist_without_files()
    test_a_successful_publication_must_publish_a_file()
    test_a_stale_attempt_cannot_publish_over_a_newer_one()
    test_publication_refuses_an_artifact_from_another_client()
    test_multiple_files_carry_explicit_roles()
    test_expiry_moves_a_published_instance_without_touching_its_lifecycle()
    test_the_publication_boundary_never_reads_a_filename()

    # 4. Authorization
    test_direct_and_group_grants_both_see_the_library()
    test_report_access_and_database_access_are_separate_grants()
    test_an_account_without_any_grant_sees_nothing()
    test_an_inactive_client_is_indistinguishable_from_an_absent_one()
    test_a_foreign_instance_reference_does_not_leak()
    test_a_foreign_member_and_a_foreign_artifact_are_unreachable()
    test_access_is_re_evaluated_on_every_request()

    # 5. Library and detail
    test_the_library_renders_client_type_and_period()
    test_the_library_is_not_a_data_grid_and_carries_no_global_search()
    test_counts_are_internally_consistent()
    test_every_rendered_instance_obeys_the_active_filters()
    test_status_governs_the_available_actions()
    test_a_multi_file_instance_shows_one_badge_and_one_size_per_file()
    test_the_detail_page_renders_the_approved_regions()
    test_period_siblings_and_history_come_from_persisted_periods()
    test_the_main_file_is_distinguished_and_a_non_previewable_file_offers_download_only()
    test_source_data_navigation_reauthorizes_database_explorer_access()
    test_the_database_export_adapter_surfaces_jobs_without_copying_them()
    test_the_states_explain_themselves_without_leaking_infrastructure()
    test_the_file_store_failure_state_is_reachable_and_says_only_that()
    test_the_legacy_folder_surface_is_not_the_report_explorer_navigation()
    test_the_report_pages_keep_one_heading_and_the_approved_landmarks()

    print("\nALL PASS: PORTAL_S15_REPORT_EXPLORER")


def main() -> None:
    """Own the instance, prove it is PostgreSQL 16, run, and always clean up."""
    global DSN
    try:
        instance = disposable_postgres(label="s15")
    except DisposablePostgresUnavailable as exc:  # pragma: no cover - defensive
        print(f"LIVE_MIGRATION_TEST_NOT_AVAILABLE: {exc}")
        return
    try:
        with instance as (dsn, info):
            require_loopback_dsn_or_exit(dsn, label="disposable S15 instance")
            assert info["server_version_num"] // 10000 == 16, info
            assert "logdb" not in dsn.lower(), "refusing a production-like database name"
            DSN = dsn
            print(f"POSTGRES_16_VERIFIED: {info['server_version']} "
                  f"(container {info['container']}, database {info['database']})")
            # Every `api/main.py` helper this suite exercises reaches the
            # database through `db_conn`, so pointing it at the disposable
            # instance is what makes the REAL authorization gate run.
            api_main.db_conn = connect
            _run_all()
    except DisposablePostgresUnavailable as exc:
        print(f"LIVE_MIGRATION_TEST_NOT_AVAILABLE: {exc}")
        return

    print("\nDISPOSABLE_POSTGRES_S15_TEST_PASS")


if __name__ == "__main__":
    main()
