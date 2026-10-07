#!/usr/bin/env python3
"""Manual checks for isolated Eco Driving Person workflow.

Run:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_person_driving_workflow.py
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry  # noqa: E402
from jobs.api.telematics.dispatcher import _build_job_params  # noqa: E402
from jobs.ecodriving_person.job_eco_driving_person_aggregate import (  # noqa: E402
    MODE_FINAL_MONTH_WEEKLY_SNAPSHOT,
    MODE_MONTHLY_FULL_AGGREGATION,
    MODE_WEEKLY_CUMULATIVE_SNAPSHOT,
    normalize_driver_name,
)
from jobs.ecodriving_person.email_idempotency import (  # noqa: E402
    DEFAULT_PENDING_STALE_AFTER_MINUTES,
    build_idempotency_key,
    normal_send_scope,
)
from jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications import (  # noqa: E402
    DEFAULT_TEMPLATE_DIR as WEEKLY_TEMPLATE_DIR,
    REQUIRED_TEMPLATE_FILENAMES as WEEKLY_TEMPLATE_FILENAMES,
    classify_candidate,
)
from jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications import (  # noqa: E402
    DEFAULT_TEMPLATE_DIR as MONTHLY_TEMPLATE_DIR,
    REQUIRED_TEMPLATE_FILENAMES as MONTHLY_TEMPLATE_FILENAMES,
)

CLIENT_ID = "00000000-0000-0000-0000-000000000016"
PERSON_ID = "11111111-1111-1111-1111-111111111111"
OTHER_PERSON_ID = "22222222-2222-2222-2222-222222222222"
PERSON_GROUP_KEY = "jan kowalski"


def test_driver_name_normalization() -> None:
    assert normalize_driver_name(" Jan   Kowalski ") == "jankowalski"
    assert normalize_driver_name("JAN KOWALSKI") == "jankowalski"
    assert normalize_driver_name("JAN\t  KOWALSKI\nNowak") == "jankowalskinowak"
    assert normalize_driver_name("ŁUKASZ   ŻÓŁĆ") == "łukaszżółć"
    assert normalize_driver_name(None) is None
    assert normalize_driver_name("   ") is None
    print("PASS: driver_name normalization trims, collapses whitespace, and is case-insensitive")


def test_mapping_import_validation() -> None:
    rows = [
        {"person_id": PERSON_ID, "person_name": "Jan Kowalski", "email": "jan@example.test", "driver_name": "Jan Kowalski", "ranking_included": "true", "is_active": "true"},
        {"person_id": PERSON_ID, "person_name": "Jan Kowalski", "email": "jan@example.test", "driver_name": " J.   Kowalski ", "ranking_included": "true", "is_active": "true"},
        {"person_id": OTHER_PERSON_ID, "person_name": "Anna Nowak", "email": "anna@example.test", "driver_name": "JAN KOWALSKI", "ranking_included": "true", "is_active": "true"},
        {"person_id": PERSON_ID, "person_name": "Jan Kowalski", "email": "different@example.test", "driver_name": "Kowalski Jan", "ranking_included": "true", "is_active": "true"},
        {"person_id": "", "person_name": "No Driver", "email": "nodriver@example.test", "driver_name": " ", "ranking_included": "false", "is_active": "true"},
    ]
    prepared, rejected = _prepare_rows(rows)
    assert len(prepared) == 2
    assert {row["normalized_driver_name"] for row in prepared} == {"jan kowalski", "j. kowalski"}
    rejected_reasons = {reason for row in rejected for reason in row["errors"]}
    assert "alias_assigned_to_multiple_people" in rejected_reasons
    assert "inconsistent_person_identity" in rejected_reasons
    assert "empty_driver_name" in rejected_reasons
    print("PASS: mapping import accepts aliases and rejects conflicts/inconsistent person rows")


def test_mapping_import_stable_identity_resolution() -> None:
    first_rows = [
        {"person_id": "", "person_name": "Jan Kowalski", "email": "jan@example.test", "driver_name": "Jan Kowalski", "ranking_included": "true", "is_active": "true"},
        {"person_id": "", "person_name": " Jan   Kowalski ", "email": "JAN@example.test", "driver_name": "J. Kowalski", "ranking_included": "true", "is_active": "true"},
    ]
    prepared, rejected = _prepare_rows(first_rows)
    assert not rejected
    resolved, rejected, conflicts = _resolve_rows(prepared, people={}, mappings={})
    assert not rejected
    assert not conflicts
    generated_ids = {row["person_id"] for row in resolved}
    assert len(generated_ids) == 1
    generated_id = next(iter(generated_ids))

    people = {
        generated_id: {
            "person_id": generated_id,
            "person_name": "Jan Kowalski",
            "email": "jan@example.test",
            "ranking_included": True,
            "is_active": True,
        }
    }
    mappings = {
        "jan kowalski": [{
            "mapping_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "person_id": generated_id,
            "normalized_driver_name": "jan kowalski",
            "driver_name": "Jan Kowalski",
            "is_active": True,
        }]
    }
    repeated, rejected, conflicts = _resolve_rows(prepared, people=people, mappings=mappings)
    assert not rejected
    assert not conflicts
    assert {row["person_id"] for row in repeated} == {generated_id}

    new_alias_rows = [
        {"person_id": "", "person_name": "Jan Kowalski", "email": "jan@example.test", "driver_name": "Kowalski Jan", "ranking_included": "true", "is_active": "true"},
    ]
    prepared_alias, rejected = _prepare_rows(new_alias_rows)
    assert not rejected
    resolved_alias, rejected, conflicts = _resolve_rows(prepared_alias, people=people, mappings=mappings)
    assert not rejected
    assert not conflicts
    assert resolved_alias[0]["person_id"] == generated_id

    same_name_other_email, rejected = _prepare_rows([
        {"person_id": "", "person_name": "Jan Kowalski", "email": "other@example.test", "driver_name": "Jan Other", "ranking_included": "true", "is_active": "true"},
    ])
    assert not rejected
    resolved_other, rejected, conflicts = _resolve_rows(same_name_other_email, people=people, mappings=mappings)
    assert not rejected
    assert not conflicts
    assert resolved_other[0]["person_id"] != generated_id

    active_conflict, rejected = _prepare_rows([
        {"person_id": OTHER_PERSON_ID, "person_name": "Anna Nowak", "email": "anna@example.test", "driver_name": "Jan Kowalski", "ranking_included": "true", "is_active": "true"},
    ])
    assert not rejected
    _resolved_conflict, rejected, conflicts = _resolve_rows(active_conflict, people=people, mappings=mappings)
    assert rejected
    assert conflicts and conflicts[0]["reason"] == "active_alias_conflict"

    inactive_alias, rejected = _prepare_rows([
        {"person_id": OTHER_PERSON_ID, "person_name": "Anna Nowak", "email": "anna@example.test", "driver_name": "Jan Kowalski", "ranking_included": "true", "is_active": "false"},
    ])
    assert not rejected
    resolved_inactive, rejected, conflicts = _resolve_rows(inactive_alias, people=people, mappings=mappings)
    assert not conflicts
    assert not rejected
    assert resolved_inactive[0]["person_id"] == OTHER_PERSON_ID
    print("PASS: mapping import resolves omitted person_id stably and keeps clients/aliases unambiguous")


def test_schema_integrity_and_trip_inclusion_contract() -> None:
    sql = (REPO_ROOT / "db" / "client_business" / "043_eco_person_physical_person_identity.sql").read_text()
    privilege_sql = (REPO_ROOT / "db" / "client_business" / "040_eco_person_runtime_privileges.sql").read_text()
    sent_archive_sql = (REPO_ROOT / "db" / "client_business" / "041_eco_person_sent_archive_state.sql").read_text()
    onboarding = (REPO_ROOT / "scripts" / "onboard_workflow_a_client.py").read_text()
    aggregate = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_aggregate.py").read_text()

    for token in (
        "eco_person_people",
        "ALTER COLUMN person_id TYPE TEXT",
        "person_id_match_key",
        "person_name_group_key",
        "eco_person_trip_assignments",
        "UNMAPPED_DRIVER_NAME",
        "SKIPPED_NO_DRIVER_NAME",
        "eco_person_weekly_stats",
        "eco_person_monthly_stats",
        "eco_person_weekly_email_send_log",
        "eco_person_monthly_email_send_log",
        "fk_eco_person_trip_assignments_source_person",
        "fk_eco_person_weekly_email_send_log_stats",
        "fk_eco_person_monthly_email_send_log_stats",
        "uq_eco_person_weekly_email_send_log_normal_idempotency",
        "uq_eco_person_monthly_email_send_log_normal_idempotency",
    ):
        assert token in sql, token

    assert "Driver_Restrictions" not in aggregate
    assert "Dysponent_ID" not in aggregate
    assert "PRIVATE_DRIVER_TAG" not in aggregate
    assert "a.is_private_trip IS FALSE" not in aggregate
    assert "driver_tag_description" in aggregate
    assert "trip_mode" in aggregate
    assert "UNMAPPED_DRIVER_NAME" in aggregate
    assert "SKIPPED_NO_DRIVER_NAME" in aggregate
    assert "eco_person_people_email_view" in privilege_sql
    assert "GRANT SELECT ON TABLE public.eco_person_people_email_view" in privilege_sql
    assert "GRANT DELETE ON TABLE public.eco_person_weekly_stats" in privilege_sql
    assert "sent_mime_bytes BYTEA" in sent_archive_sql
    assert "sent_archive_status IN ('pending','appended','already_present','failed')" in sent_archive_sql
    assert "040_eco_person_runtime_privileges.sql" in onboarding
    assert "041_eco_person_sent_archive_state.sql" in onboarding
    print("PASS: new schema is isolated and aggregate includes private/null/unknown trip modes when mapped")


def test_email_reservation_contract() -> None:
    weekly_src = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_weekly_email_notifications.py").read_text()
    monthly_src = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_monthly_email_notifications.py").read_text()
    schema = (REPO_ROOT / "db" / "client_business" / "043_eco_person_physical_person_identity.sql").read_text()
    sent_archive_sql = (REPO_ROOT / "db" / "client_business" / "041_eco_person_sent_archive_state.sql").read_text()

    key = build_idempotency_key(
        report_type="weekly",
        client_id=CLIENT_ID,
        person_name_group_key=PERSON_GROUP_KEY,
        period_start_date=datetime(2026, 6, 1).date(),
        period_end_date=datetime(2026, 6, 8).date(),
        template_type="bezpieczny",
    )
    assert key == (
        "eco_person|weekly|00000000-0000-0000-0000-000000000016|"
        f"person_sha256:{hashlib.sha256(PERSON_GROUP_KEY.encode('utf-8')).hexdigest()}|"
        "2026-06-01|2026-06-08|bezpieczny"
    )
    assert normal_send_scope(force_resend=False, test_recipient_email=None) == "normal"
    assert normal_send_scope(force_resend=True, test_recipient_email=None) == "forced"
    assert normal_send_scope(force_resend=False, test_recipient_email="qa@example.test") == "test"
    assert DEFAULT_PENDING_STALE_AFTER_MINUTES == 120

    for src in (weekly_src, monthly_src):
        assert "reserve_send(" in src
        assert "conn.commit()" in src
        assert "send_html_email(" in src
        assert "mark_send_sent(" in src
        assert "mark_send_failed(" in src
        assert "pending_stale_after_minutes" in src
        assert "eco_driving_weekly_email_send_log" not in src
        assert "eco_driving_monthly_email_send_log" not in src

    assert "WHERE send_scope = 'normal' AND status IN ('pending', 'sent')" in schema
    assert "AND send_scope = 'normal'\n          AND status = 'sent'" in weekly_src
    assert "sent_mime_bytes" in sent_archive_sql
    assert "sent_archive_status" in weekly_src
    assert "archive_only" in weekly_src
    assert "stale pending reservation reclaimed before retry" in (REPO_ROOT / "jobs" / "ecodriving_person" / "email_idempotency.py").read_text()
    print("PASS: email jobs use pending reservations, stale-pending handling, and isolated send logs")


def test_dispatcher_and_registry_contract() -> None:
    expected_jobs = {
        "eco_person_driving_weekly_snapshot": "jobs.ecodriving_person.job_eco_driving_person_aggregate",
        "eco_person_driving_month_end_weekly_snapshot": "jobs.ecodriving_person.job_eco_driving_person_aggregate",
        "eco_person_driving_monthly_aggregation": "jobs.ecodriving_person.job_eco_driving_person_aggregate",
        "eco_person_driving_weekly_email_notifications": "jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications",
        "eco_person_driving_monthly_email_notifications": "jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications",
    }
    for dataset, job in expected_jobs.items():
        assert registry.DATASETS[dataset].job_module == job

    fire = datetime(2026, 5, 18, 1, 0, tzinfo=timezone.utc)
    common = {
        "client_id": CLIENT_ID,
        "client_code": "BRAVO00016",
        "event_enrichment_mode": "enabled",
        "window_start_ts": fire,
        "window_end_ts": fire,
    }
    weekly = _build_job_params(dataset_name="eco_person_driving_weekly_snapshot", **common)
    assert weekly["mode"] == MODE_WEEKLY_CUMULATIVE_SNAPSHOT
    assert weekly["include_weekly"] is True and weekly["include_monthly"] is False
    final_weekly = _build_job_params(dataset_name="eco_person_driving_month_end_weekly_snapshot", **common)
    assert final_weekly["mode"] == MODE_FINAL_MONTH_WEEKLY_SNAPSHOT
    monthly = _build_job_params(dataset_name="eco_person_driving_monthly_aggregation", **common)
    assert monthly["mode"] == MODE_MONTHLY_FULL_AGGREGATION
    email = _build_job_params(dataset_name="eco_person_driving_weekly_email_notifications", **common)
    assert "window_start_ts" not in email and email["trigger"] == "SCHEDULED"

    migration = (REPO_ROOT / "db" / "migrations" / "046_workflow_a_eco_person_registry.sql").read_text()
    assert "false AS enabled" in migration
    assert "ON CONFLICT (client_id, dataset_name) DO NOTHING" in migration
    assert "Europe/Warsaw" in migration
    print("PASS: dispatcher recognizes isolated datasets and default schedules remain disabled")


def test_email_contract() -> None:
    for filename in WEEKLY_TEMPLATE_FILENAMES:
        assert (WEEKLY_TEMPLATE_DIR / filename).exists(), filename
    for filename in MONTHLY_TEMPLATE_FILENAMES:
        assert (MONTHLY_TEMPLATE_DIR / filename).exists(), filename

    row = {
        "recipient_email": "jan@example.test",
        "qualification_status": "QUALIFIED",
        "ecodriving_rating_type": "bezpieczny",
        "ranking_included": True,
    }
    assert classify_candidate(row, already_sent=False, force_resend=False).should_send
    assert classify_candidate(row | {"recipient_email": ""}, already_sent=False, force_resend=False).status == "skipped_missing_email"
    assert classify_candidate(row, already_sent=True, force_resend=False).status == "skipped_already_sent"
    assert classify_candidate(row, already_sent=True, force_resend=True).should_send

    weekly_src = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_weekly_email_notifications.py").read_text()
    monthly_src = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_monthly_email_notifications.py").read_text()
    for src in (weekly_src, monthly_src):
        assert "eco_person_people_email_view" in src
        assert "No Eco Driving Person aggregation rows found" in src
        assert "ecoalpha" not in src.lower()
        assert "send_scope" in src
    assert "load_delivery_smtp_settings_from_env" in weekly_src
    assert "ECO_PERSON_EMAIL_SMTP_HOST" in monthly_src
    print("PASS: email jobs use isolated templates, real-person recipients, idempotency, dry-run/test hooks")


def test_alpha00001_regression_surface_unchanged() -> None:
    aggregate = (REPO_ROOT / "jobs" / "ecodriving" / "job_eco_driving_aggregate.py").read_text()
    weekly_email = (REPO_ROOT / "jobs" / "ecodriving" / "job_eco_driving_weekly_email_notifications.py").read_text()
    monthly_email = (REPO_ROOT / "jobs" / "ecodriving" / "job_eco_driving_monthly_email_notifications.py").read_text()
    person_aggregate = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_aggregate.py").read_text()
    person_weekly = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_weekly_email_notifications.py").read_text()
    person_monthly = (REPO_ROOT / "jobs" / "ecodriving_person" / "job_eco_driving_person_monthly_email_notifications.py").read_text()
    schema = (REPO_ROOT / "db" / "client_business" / "028_eco_driving_periods_and_driver_chart.sql").read_text()

    assert "NULLIF(btrim(\"Driver_Restrictions\"), '') AS driver_restrictions_raw" in aggregate
    assert "NULLIF(btrim(\"Dysponent_ID\"), '') AS dysponent_id_raw" in aggregate
    assert "WHEN driver_restrictions_raw IS NOT NULL THEN driver_restrictions_raw" in aggregate
    assert "WHEN dysponent_id_raw IS NOT NULL THEN dysponent_id_raw" in aggregate
    assert "PRIVATE_DRIVER_TAG" in aggregate
    assert "a.is_private_trip IS FALSE" in aggregate
    assert "eco_trip_assignments" in aggregate
    assert "eco_driver_weekly_stats" in aggregate
    assert "eco_driver_monthly_stats" in aggregate
    assert "eco_drivers_id_chart" in aggregate
    assert "eco_driving_weekly_email_send_log" in weekly_email
    assert "assets" in weekly_email and "ecodriving" in weekly_email
    for driver_source in (aggregate, weekly_email, monthly_email):
        assert "eco_person_" not in driver_source
        assert "jobs.ecodriving_person" not in driver_source
    for person_source in (person_aggregate, person_weekly, person_monthly):
        assert "jobs.ecodriving.email_snapshot_validation" not in person_source
        assert "eco_driving_weekly_email_send_log" not in person_source
        assert "eco_driving_monthly_email_send_log" not in person_source
    assert 's.ranking_included AS ranking_included' in weekly_email
    assert 's.ranking_included AS ranking_included' in monthly_email
    assert 'DEFAULT_TEMPLATE_DIR = REPO_ROOT / "assets" / "email_templates" / "ecodriving" / "weekly"' in weekly_email
    assert 'DEFAULT_TEMPLATE_DIR = REPO_ROOT / "assets" / "email_templates" / "ecodriving_person" / "weekly"' in person_weekly
    assert "public.eco_drivers_id_chart" in schema
    print("PASS: driver snapshot routing remains isolated from BRAVO00016 person tables, templates, and delivery")


def main() -> None:
    test_driver_name_normalization()
    test_schema_integrity_and_trip_inclusion_contract()
    test_email_reservation_contract()
    test_dispatcher_and_registry_contract()
    test_email_contract()
    test_alpha00001_regression_surface_unchanged()
    print("OK - Eco Driving Person workflow checks passed")


if __name__ == "__main__":
    main()
