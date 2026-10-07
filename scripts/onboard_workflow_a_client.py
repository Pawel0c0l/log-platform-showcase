#!/usr/bin/env python3
"""
Workflow A — Client Onboarding Script

**This is a fresh-client creation tool.** It creates a client that does not
exist. It is not a repair tool, not a resume tool and not a decommissioning
tool, and it refuses — before any mutation and before any provider request —
any target that already exists, is ambiguous, carries the residue of an earlier
interrupted attempt, or has already progressed past creation. Forcing such a
client back through `CREATED_DISABLED_STRICT` would misreport its real state.

Reads non-secret config from a YAML file, resolves secrets from env vars
named after client_name, and provisions:
  1. Client business Postgres database + user + DDL + grants
  2. Control-plane rows in the platform DB (client_account, ten disabled
     schedule rows, fourteen disabled retention rows) — created, verified and
     committed as ONE transaction
  3. Preflight validation (DB connectivity, provider auth)

SIDE EFFECTS, IN EXECUTION ORDER, AND HOW EACH IS UNDONE
--------------------------------------------------------
Two databases are involved and they cannot share a transaction, so they are
handled by two different mechanisms — a transaction for the control plane, and
exact compensation for what precedes it.

  1. `provider_auth_preflight` — one HTTP GET to the provider. Read-only,
     nothing to undo. Runs *after* the zero-state check, so a refused target
     issues no provider request at all.
  2. `create_client_db` — CREATE DATABASE / CREATE USER / GRANT CONNECT on the
     client host. Outside PostgreSQL transactional control (a database cannot be
     created in a transaction) and in a different database, therefore
     **compensated**: `state.created_db` / `state.created_user` are set only
     when this invocation created them, and the cleanup drops only those. A
     pre-existing database or role is found, recorded as pre-existing, and never
     dropped.
  3. `apply_client_ddl` — DDL plus the migration baseline, in the client
     database, autocommit. Contained by the database created in (2); if that
     database was created here, dropping it removes all of this. If it
     pre-existed, the DDL files are idempotent (`CREATE ... IF NOT EXISTS`,
     `ON CONFLICT DO NOTHING`).
  4. `apply_grants` / `apply_workflow_b_stage3_permissions` — GRANTs, and one
     cluster-wide Stage 3 loader role. Same containment as (3). The loader role
     is shared infrastructure and is deliberately never dropped.
  5. `client_db_preflight` — read-only.
  6. `create_control_plane_state` — the only platform-database writes, all in
     ONE transaction: the `client_account` row, ten `client_dataset_schedule`
     rows and fourteen `client_table_retention` rows, followed by the definitive
     verification of the newly created state, followed by exactly one COMMIT.
     Rollback-capable in full: a verification failure leaves zero new rows and
     every pre-existing row byte-identical.

No filesystem artifact, credential file, subprocess, Git effect or migration of
its own is produced by any path.

Usage:
  # Dry-run (no changes):
  python scripts/onboard_workflow_a_client.py --config config.yaml

  # Apply:
  python scripts/onboard_workflow_a_client.py --config config.yaml --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jobs.trip_metrics_population_source import (
    TRIP_METRICS_POPULATION_SOURCE_DEFAULT,
    normalize_trip_metrics_population_source,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RUN_TYPE_BASE,
    SURFACE_ONBOARDING,
    TRIPS_PAGINATION_MODE_STRICT_META,
    TRIPS_SYNC_DATASET_NAME,
    assert_creation_permitted,
)
from jobs.reports.stage3 import permissions as stage3_permissions  # noqa: E402

CLIENT_BUSINESS_DDL_FILES = [
    REPO_ROOT / "db" / "client_business" / "020_client_trips_final_schema.sql",
    REPO_ROOT / "db" / "client_business" / "021_add_trip_mode_to_client_trips.sql",
    REPO_ROOT / "db" / "client_business" / "024_alpha00001_client_trips_dysponent_id.sql",
    REPO_ROOT / "db" / "client_business" / "025_alpha00001_dysponent_id_batch_indexes.sql",
    REPO_ROOT / "db" / "client_business" / "026_add_driver_restrictions_to_client_trips.sql",
    REPO_ROOT / "db" / "client_business" / "027_eco_driving_schema.sql",
    REPO_ROOT / "db" / "client_business" / "028_eco_driving_periods_and_driver_chart.sql",
    REPO_ROOT / "db" / "client_business" / "029_eco_driving_nullable_scores.sql",
    REPO_ROOT / "db" / "client_business" / "030_eco_driving_trend_views.sql",
    REPO_ROOT / "db" / "client_business" / "031_eco_driving_validation_fields.sql",
    REPO_ROOT / "db" / "client_business" / "032_eco_driving_weekly_email_notifications.sql",
    REPO_ROOT / "db" / "client_business" / "033_eco_driving_weekly_email_send_log_grants.sql",
    REPO_ROOT / "db" / "client_business" / "034_eco_driving_rating_type_share_percent.sql",
    REPO_ROOT / "db" / "client_business" / "035_eco_driving_monthly_email_notifications.sql",
    REPO_ROOT / "db" / "client_business" / "036_eco_driving_round_per_100km_stats.sql",
    REPO_ROOT / "db" / "client_business" / "037_eco_driving_score_from_rounded_per_100km.sql",
    REPO_ROOT / "db" / "client_business" / "038_environment_identity.sql",
    REPO_ROOT / "db" / "client_business" / "039_eco_person_driving_schema.sql",
    REPO_ROOT / "db" / "client_business" / "040_eco_person_runtime_privileges.sql",
    REPO_ROOT / "db" / "client_business" / "041_eco_person_sent_archive_state.sql",
    REPO_ROOT / "db" / "client_business" / "043_eco_person_physical_person_identity.sql",
    REPO_ROOT / "db" / "client_business" / "044_eco_email_fail_closed_idempotency.sql",
    REPO_ROOT / "db" / "client_business" / "045_environment_identity_promotion_primitive.sql",
    REPO_ROOT / "db" / "client_business" / "046_eco_ranking_qualified_only.sql",
    # M4: additive, nullable first-seen provenance. Applied to a new client here
    # and to existing clients by scripts/apply_client_business_migrations.py, so
    # the two rollout paths cannot diverge (docs/20 §21.4).
    REPO_ROOT / "db" / "client_business" / "047_client_trips_first_seen_request_id.sql",
    # M-LAG EXPAND: the paired first-observation instant. A client onboarded after
    # the M-LAG release must NOT receive a schema older than the running writer
    # expects — the trip INSERT names this column unconditionally, so a new client
    # without it could not ingest at all. Applied here for a new client and by
    # scripts/apply_client_business_migrations.py for existing ones, so the two
    # rollout paths converge (docs/21 §11).
    REPO_ROOT / "db" / "client_business" / "048_client_trips_first_seen_response_received_at.sql",
    # Driver Eco Dashboard V1: the host publication/e-mail delivery ledger, its
    # row guard and its runtime grants. Applied here for a new client and by
    # scripts/apply_client_business_migrations.py for existing ones, so a client
    # onboarded after this release does not receive a database the publisher
    # cannot use. The migration's own header states the contract it installs.
    # The grant block mirrors the client_trips DML roles,
    # so it must run after client_trips exists — which it does, 020 is first.
    REPO_ROOT / "db" / "client_business" / "049_eco_dashboard_delivery_operation.sql",
    # Driver Eco Dashboard V1 CORRECTION. 049 is applied shared history on every
    # existing client, so the reviewed external-mailer ownership contract —
    # `external_mailer`, the EXTERNAL_MAILER_HANDOFF state, the ownership CHECKs
    # and the reviewed guard body — arrives as a forward migration instead of an
    # edit. A new client must land on the SAME final contract as a migrated one,
    # so 050 runs here too, immediately after the 049 it upgrades.
    REPO_ROOT / "db" / "client_business" / "050_eco_dashboard_external_mailer_ownership.sql",
    # Driver Eco Dashboard V1 EXPIRED-CAPABILITY RETIREMENT. Same reasoning as
    # 050: 049 and 050 are applied shared history, so the `CAPABILITY_RETIRED`
    # state, the constraint that makes a retired row unable to hold a bearer,
    # and the narrowed open-operations index arrive forward. A new client must
    # land on the SAME final contract as a migrated one, so 051 runs here too,
    # immediately after the 050 it upgrades.
    REPO_ROOT / "db" / "client_business" / "051_eco_dashboard_capability_retirement.sql",
    REPO_ROOT / "db" / "client_business" / "012_client_vehicle_daily_fuel.sql",
    REPO_ROOT / "db" / "client_business" / "013_client_vehicle_driver_daily_fuel.sql",
    REPO_ROOT / "db" / "client_business" / "014_add_record_id_and_synced_at.sql",
]

# New onboarding uses 020 plus additive/rebuild follow-ups as the direct final
# client_trips schema. Mark older
# client_trips rollout files as applied/superseded so the existing-client
# migration runner does not later add deprecated columns such as trip-level
# fuel to a newly onboarded database.
CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED = [
    "009_workflow_a_client_business.sql",
    "010_add_client_code.sql",
    "011_extend_client_trips.sql",
    "012_client_vehicle_daily_fuel.sql",
    "013_client_vehicle_driver_daily_fuel.sql",
    "014_add_record_id_and_synced_at.sql",
    "015_add_rpm_columns.sql",
    "016_add_odometer_columns.sql",
    "018_client_trips_no_fuel.sql",
    "019_add_speeding_violation_count_columns.sql",
    "020_client_trips_final_schema.sql",
    "021_add_trip_mode_to_client_trips.sql",
    "024_alpha00001_client_trips_dysponent_id.sql",
    "025_alpha00001_dysponent_id_batch_indexes.sql",
    "026_add_driver_restrictions_to_client_trips.sql",
    "027_eco_driving_schema.sql",
    "028_eco_driving_periods_and_driver_chart.sql",
    "029_eco_driving_nullable_scores.sql",
    "030_eco_driving_trend_views.sql",
    "031_eco_driving_validation_fields.sql",
    "032_eco_driving_weekly_email_notifications.sql",
    "033_eco_driving_weekly_email_send_log_grants.sql",
    "034_eco_driving_rating_type_share_percent.sql",
    "035_eco_driving_monthly_email_notifications.sql",
    "036_eco_driving_round_per_100km_stats.sql",
    "037_eco_driving_score_from_rounded_per_100km.sql",
    "038_environment_identity.sql",
    "039_eco_person_driving_schema.sql",
    "040_eco_person_runtime_privileges.sql",
    "041_eco_person_sent_archive_state.sql",
    "043_eco_person_physical_person_identity.sql",
    "044_eco_email_fail_closed_idempotency.sql",
    "045_environment_identity_promotion_primitive.sql",
    "046_eco_ranking_qualified_only.sql",
    "047_client_trips_first_seen_request_id.sql",
    "048_client_trips_first_seen_response_received_at.sql",
    "049_eco_dashboard_delivery_operation.sql",
    "050_eco_dashboard_external_mailer_ownership.sql",
    "051_eco_dashboard_capability_retirement.sql",
]

# Logical Workflow A datasets/tables that should get a default-disabled
# control-plane row when a client is onboarded. Kept in sync with the
# Python registry at jobs/api/telematics/registry.py (validated at runtime
# in seed_dataset_schedule_and_retention()).
#: Onboarding creates every schedule row disabled, without exception and
#: without an override. This named constant is the declared creation intent the
#: mutation-surface guard validates. The INSERT itself writes the SQL literal
#: `FALSE`; what proves the stored value is the in-transaction, pre-commit
#: read-back in `verify_onboarding_state_on_cursor`.
SEEDED_SCHEDULE_ENABLED = False

#: The pagination mode a newly onboarded client starts in. Not written by the
#: INSERT — it is the `client_account.trips_pagination_mode` column default set
#: by migration 055 — but asserted after creation so a changed default cannot
#: silently produce a client whose `trips_sync` schedule could then be activated.
ONBOARDED_TRIPS_PAGINATION_MODE = TRIPS_PAGINATION_MODE_STRICT_META

DEFAULT_DATASETS = (
    TRIPS_SYNC_DATASET_NAME,
    "fuel_daily_aggregation",
    "eco_driving_weekly_snapshot",
    "eco_driving_month_end_weekly_snapshot",
    "eco_driving_monthly_aggregation",
    "eco_person_driving_weekly_snapshot",
    "eco_person_driving_month_end_weekly_snapshot",
    "eco_person_driving_monthly_aggregation",
    "eco_person_driving_weekly_email_notifications",
    "eco_person_driving_monthly_email_notifications",
)
DEFAULT_DATASET_SCHEDULES = {
    "eco_driving_weekly_snapshot": {
        "frequency": "weekly",
        "day_of_week": 0,
        "day_of_month": None,
        "run_time": "03:00",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
    "eco_driving_month_end_weekly_snapshot": {
        "frequency": "monthly",
        "day_of_week": None,
        "day_of_month": 1,
        "run_time": "03:30",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
    "eco_driving_monthly_aggregation": {
        "frequency": "monthly",
        "day_of_week": None,
        "day_of_month": 1,
        "run_time": "04:00",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
    "eco_person_driving_weekly_snapshot": {
        "frequency": "weekly",
        "day_of_week": 0,
        "day_of_month": None,
        "run_time": "05:00",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
    "eco_person_driving_month_end_weekly_snapshot": {
        "frequency": "monthly",
        "day_of_week": None,
        "day_of_month": 1,
        "run_time": "05:30",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
    "eco_person_driving_monthly_aggregation": {
        "frequency": "monthly",
        "day_of_week": None,
        "day_of_month": 1,
        "run_time": "06:00",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
    "eco_person_driving_weekly_email_notifications": {
        "frequency": "weekly",
        "day_of_week": 0,
        "day_of_month": None,
        "run_time": "07:00",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
    "eco_person_driving_monthly_email_notifications": {
        "frequency": "monthly",
        "day_of_week": None,
        "day_of_month": 1,
        "run_time": "07:30",
        "timezone": "Europe/Warsaw",
        "lookback_days": 0,
    },
}
DEFAULT_TABLES = (
    "client_trips",
    "client_speeding_notifications",
    "client_vehicle_daily_fuel",
    "client_vehicle_driver_daily_fuel",
    "eco_trip_assignments",
    "eco_driver_weekly_stats",
    "eco_driver_monthly_stats",
    "eco_person_people",
    "eco_person_driver_mappings",
    "eco_person_trip_assignments",
    "eco_person_weekly_stats",
    "eco_person_monthly_stats",
    "eco_person_weekly_email_send_log",
    "eco_person_monthly_email_send_log",
)
PROVIDER_AUTH_PREFLIGHT_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# .env loading (optional, never overrides existing env)
# ---------------------------------------------------------------------------

def _load_dotenv_if_present() -> None:
    # DETERMINISTIC TESTS MUST NOT READ REAL OPERATOR CREDENTIALS.
    #     A schema/rollout test supplies its own synthetic loopback
    #     configuration and has no business opening the host's `.env` — not
    #     because a value would be printed, but because a test that can read a
    #     production secret file is a test that can act on production
    #     configuration by accident. `LOG_PLATFORM_NO_DOTENV=1` is the explicit
    #     opt-out those tests set; no operator workflow sets it, so ordinary
    #     manual and scheduled execution is unchanged.
    if os.environ.get("LOG_PLATFORM_NO_DOTENV") == "1":
        return

    dotenv_path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(dotenv_path):
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path, override=False)
        print(f"  [INFO] Loaded environment variables from {dotenv_path}")
        return
    except ImportError:
        pass

    # Fallback: lightweight KEY=VALUE parser when python-dotenv is absent.
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
        print(f"  [INFO] Loaded environment variables from {dotenv_path} (fallback parser)")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require(module_name: str, pip_name: str | None = None):
    try:
        return __import__(module_name)
    except ImportError:
        pkg = pip_name or module_name
        print(f"ERROR: Missing dependency '{module_name}'. Install: pip install {pkg}")
        sys.exit(1)


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def _info(msg: str) -> None:
    print(f"  {_green('✓')} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_yellow('⚠')} {msg}")


def _err(msg: str) -> None:
    print(f"  {_red('✗')} {msg}")


def _section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {_bold(title)}")
    print(f"{'─' * 60}")


def _dry(msg: str) -> None:
    print(f"  {_yellow('[DRY-RUN]')} {msg}")


# ---------------------------------------------------------------------------
# Rollback state tracker
# ---------------------------------------------------------------------------

@dataclass
class OnboardState:
    """Tracks resources created by this run for compensating cleanup.

    Every field means "this invocation created it", never "it exists". That
    distinction is what keeps the cleanup from ever touching a pre-existing
    database, role or control-plane row: a resource the script found already
    present leaves its flag false and is never compensated.

    `inserted_client_id` is set inside the control-plane transaction and cleared
    again if that transaction rolls back, so it only ever names a *committed*
    row of this invocation.
    """
    created_db: bool = False
    created_user: bool = False
    inserted_client_id: Optional[str] = None
    control_plane_committed: bool = False


class OnboardError(RuntimeError):
    """Raised by apply steps to trigger rollback."""


# ---------------------------------------------------------------------------
# Fresh-creation refusals
# ---------------------------------------------------------------------------
#
# This script is a *fresh-client creation tool*. It is not a repair tool, not a
# resume tool and not a decommissioning tool, and it must never try to force an
# existing or already-progressed client back through the first state of the
# cold-start state machine. Every one of these is refused before any mutation.

#: The target client already exists in the control plane.
ONBOARDING_REFUSED_CLIENT_EXISTS = "ONBOARDING_REFUSED_CLIENT_EXISTS"
#: No client row, but control-plane rows for the target already exist — the
#: residue of an interrupted or partially completed earlier attempt.
ONBOARDING_REFUSED_PARTIAL_STATE = "ONBOARDING_REFUSED_PARTIAL_STATE"
#: The target exists and has moved past creation: coverage, a recovery row,
#: schedule history or a platform business run.
ONBOARDING_REFUSED_PROGRESS_STATE = "ONBOARDING_REFUSED_PROGRESS_STATE"
#: The target identity does not resolve to exactly one client.
ONBOARDING_REFUSED_AMBIGUOUS_STATE = "ONBOARDING_REFUSED_AMBIGUOUS_STATE"

ONBOARDING_REFUSAL_CODES = frozenset({
    ONBOARDING_REFUSED_CLIENT_EXISTS,
    ONBOARDING_REFUSED_PARTIAL_STATE,
    ONBOARDING_REFUSED_PROGRESS_STATE,
    ONBOARDING_REFUSED_AMBIGUOUS_STATE,
})


class OnboardingRefusal(OnboardError):
    """A refusal to create, raised *before* any write.

    Distinct from `OnboardError` because the two demand different operator
    action: an `OnboardError` during apply means something broke and the
    invocation's own resources are rolled back, while a refusal means the
    target was never eligible and **nothing was attempted**. A refusal
    therefore never triggers rollback — there is nothing this invocation
    created to compensate — and never reports an onboarding state.
    """

    def __init__(self, code: str, message: str, evidence: Optional[dict] = None) -> None:
        self.code = code
        self.evidence = dict(evidence or {})
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@dataclass
class OnboardConfig:
    client_name: str
    client_key: str
    client_code: Optional[str]
    enabled: bool
    provider_type: str
    provider_base_url: str
    client_db_host: str
    client_db_port: int
    client_db_name: str
    client_db_schema: str
    speed_trigger_filter_text: str

    api_username_env: str
    api_key_env: str
    db_username_env: str
    db_key_env: str

    api_username: str
    api_key: str
    db_username: str
    db_key: str

    trip_metrics_population_source: str = TRIP_METRICS_POPULATION_SOURCE_DEFAULT


def load_config(yaml_path: str) -> OnboardConfig:
    yaml = _require("yaml", "PyYAML")
    with open(yaml_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        print(f"ERROR: YAML must be a mapping, got {type(raw).__name__}")
        sys.exit(1)

    required_keys = [
        "client_name", "provider_type", "provider_base_url",
        "client_db_host", "client_db_name", "speed_trigger_filter_text",
    ]
    for k in required_keys:
        if k not in raw or raw[k] is None:
            print(f"ERROR: Missing required YAML key: {k}")
            sys.exit(1)

    client_name_raw = str(raw["client_name"]).strip()
    client_key = client_name_raw.upper()

    api_username_env = f"{client_key}_API_USERNAME"
    api_key_env = f"{client_key}_API_KEY"
    db_username_env = f"{client_key}_DB_USERNAME"
    db_key_env = f"{client_key}_DB_KEY"

    missing_env: list[str] = []
    for var in [api_username_env, api_key_env, db_username_env, db_key_env]:
        if os.getenv(var) is None:
            missing_env.append(var)
    if missing_env:
        print(f"ERROR: Missing required environment variables:")
        for v in missing_env:
            print(f"  - {v}")
        print(f"\nSet them before running, e.g.:")
        print(f"  export {api_username_env}='...'")
        print(f"  export {api_key_env}='...'")
        print(f"  export {db_username_env}='...'")
        print(f"  export {db_key_env}='...'")
        sys.exit(1)

    return OnboardConfig(
        client_name=client_name_raw,
        client_key=client_key,
        client_code=raw.get("client_code"),
        enabled=bool(raw.get("enabled", True)),
        provider_type=str(raw.get("provider_type", "telematics_fleet")),
        provider_base_url=str(raw["provider_base_url"]).rstrip("/"),
        client_db_host=str(raw["client_db_host"]),
        client_db_port=int(raw.get("client_db_port", 5432)),
        client_db_name=str(raw["client_db_name"]),
        client_db_schema=str(raw.get("client_db_schema", "public")),
        speed_trigger_filter_text=str(raw["speed_trigger_filter_text"]),
        api_username_env=api_username_env,
        api_key_env=api_key_env,
        db_username_env=db_username_env,
        db_key_env=db_key_env,
        api_username=os.environ[api_username_env],
        api_key=os.environ[api_key_env],
        db_username=os.environ[db_username_env],
        db_key=os.environ[db_key_env],
        trip_metrics_population_source=normalize_trip_metrics_population_source(
            raw.get("trip_metrics_population_source")
        ),
    )


# ---------------------------------------------------------------------------
# Platform DB helpers
# ---------------------------------------------------------------------------

def _platform_dsn() -> str:
    h = os.getenv("POSTGRES_HOST", "127.0.0.1")
    p = os.getenv("POSTGRES_PORT", "5432")
    d = os.getenv("POSTGRES_DB", "logdb")
    u = os.getenv("POSTGRES_USER", "loguser")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={h} port={p} dbname={d} user={u} password={pw}"


def _platform_conn():
    psycopg = _require("psycopg")
    from psycopg.rows import dict_row
    return psycopg.connect(_platform_dsn(), row_factory=dict_row, autocommit=True)


def _admin_dsn(host: str, port: int, dbname: str = "postgres") -> str:
    u = os.getenv("POSTGRES_USER", "loguser")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={dbname} user={u} password={pw}"


def _client_dsn(cfg: OnboardConfig) -> str:
    return (
        f"host={cfg.client_db_host} port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} user={cfg.db_username} password={cfg.db_key}"
    )


# ---------------------------------------------------------------------------
# Preflight: validate everything before writes
# ---------------------------------------------------------------------------

def preflight_all(cfg: OnboardConfig, *, args: argparse.Namespace) -> None:
    """Run all read-only validations, cheapest and most decisive first.

    Ordering is deliberate. The zero-state check runs **before** the provider
    auth preflight, so a target that is not a fresh client is refused without a
    single provider request: an existing or progressed client must not cause any
    outbound call, and the refusal is a fact about the control plane that
    provider credentials cannot change.
    """

    # -- Control-plane zero-state check (refuses before anything else) --
    check_platform_control_plane(cfg)

    # -- Provider auth --
    if not args.skip_provider_auth_check:
        provider_auth_preflight(cfg)
    else:
        _section("Provider auth preflight")
        _warn("Skipped (--skip-provider-auth-check)")

    # -- DDL files exist --
    if not args.skip_ddl:
        for ddl_path in CLIENT_BUSINESS_DDL_FILES:
            if not ddl_path.exists():
                raise OnboardError(f"DDL file not found: {ddl_path}")
        _info("All DDL files present")

    # -- Admin connectivity to client DB host --
    if not args.skip_db_create:
        _section("Preflight — admin connectivity")
        psycopg = _require("psycopg")
        try:
            conn = psycopg.connect(
                _admin_dsn(cfg.client_db_host, cfg.client_db_port, dbname="postgres"),
                autocommit=True,
            )
            conn.close()
            _info(f"Admin connection to {cfg.client_db_host}:{cfg.client_db_port} OK")
        except Exception as e:
            raise OnboardError(
                f"Cannot connect to Postgres admin on {cfg.client_db_host}:{cfg.client_db_port}: {e}"
            ) from e


# ---------------------------------------------------------------------------
# Step: Validate platform DB control-plane
# ---------------------------------------------------------------------------

def _table_present(cur, qualified_name: str) -> bool:
    """Is this control-plane table present at all?

    A platform database that has not yet applied `057`/`058` legitimately has no
    coverage or recovery table. Absence means "no such state can exist", which
    is a clean zero state, not a failure.
    """
    cur.execute("SELECT to_regclass(%s)::text AS present", (qualified_name,))
    return bool((cur.fetchone() or {}).get("present"))


def _count(cur, sql: str, params: tuple) -> int:
    cur.execute(sql, params)
    return int((cur.fetchone() or {"n": 0})["n"])


def _progress_evidence(cur, *, client_id: str, client_code: Optional[str]) -> dict:
    """Everything that proves the target has moved past creation.

    Read-only. Each probe is skipped when its table is absent, so an older
    platform database reports zero rather than crashing. Queries mirror the
    ones `ops/recover_telematics_trips_window.py` already uses, so "progress"
    means the same thing to both tools.
    """
    code = client_code or ""
    evidence: dict = {}

    if _table_present(cur, "workflow_a_control.client_dataset_coverage"):
        evidence["coverage_rows"] = _count(
            cur,
            "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
            " WHERE client_id = %s",
            (client_id,),
        )
    if _table_present(cur, "workflow_a_control.client_dataset_recovery_run"):
        evidence["recovery_rows"] = _count(
            cur,
            "SELECT count(*) AS n"
            "  FROM workflow_a_control.client_dataset_recovery_run"
            " WHERE client_id = %s",
            (client_id,),
        )
    if _table_present(cur, "workflow_a_control.client_schedule_run_history"):
        evidence["schedule_history_rows"] = _count(
            cur,
            "SELECT count(*) AS n"
            "  FROM workflow_a_control.client_schedule_run_history"
            " WHERE client_id = %s",
            (client_id,),
        )
    if _table_present(cur, "public.runs"):
        evidence["platform_business_runs"] = _count(
            cur,
            "SELECT count(*) AS n FROM public.runs"
            " WHERE params ->> 'client_id' = %s"
            "    OR (%s <> '' AND params ->> 'client_code' = %s)",
            (client_id, code, code),
        )
    return evidence


def _orphan_control_plane_evidence(cur, *, client_code: Optional[str]) -> dict:
    """Control-plane rows carrying the target code but no `client_account` row.

    This is the residue an interrupted earlier attempt leaves behind, and it is
    exactly what onboarding must not silently build on top of. Probed by
    `client_code` because that is the only identity available when no client row
    exists to supply a `client_id`.
    """
    if not client_code:
        return {}
    evidence: dict = {}
    probes = (
        ("schedule_rows", "workflow_a_control.client_dataset_schedule"),
        ("retention_rows", "workflow_a_control.client_table_retention"),
        ("coverage_rows", "workflow_a_control.client_dataset_coverage"),
        ("recovery_rows", "workflow_a_control.client_dataset_recovery_run"),
        ("schedule_history_rows", "workflow_a_control.client_schedule_run_history"),
    )
    for label, table in probes:
        if not _table_present(cur, table):
            continue
        found = _count(
            cur,
            f"SELECT count(*) AS n FROM {table} WHERE client_code = %s",
            (client_code,),
        )
        if found:
            evidence[label] = found
    return evidence


def check_platform_control_plane(cfg: OnboardConfig) -> None:
    """Prove the target is a genuine zero state, or refuse before any mutation.

    Onboarding may continue **only** when the target client/schedule state does
    not exist at all. Anything else — an existing client, an ambiguous identity,
    the residue of an interrupted attempt, or a client that has already
    progressed — is refused here, with no write attempted and no provider work
    performed, because the correct response to each is a different, separately
    reviewed procedure.

    Returns `None` on success. It deliberately no longer returns an existing
    row: the historical caller treated that row as "skip the insert and carry
    on", which is what allowed an existing or already-progressed client to be
    driven through the rest of the script and then reported as
    `CREATED_DISABLED_STRICT`.
    """
    _section("Platform DB — zero-state check")
    conn = _platform_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='workflow_a_control' AND table_name='client_account'"
            )
            if not cur.fetchone():
                raise OnboardError(
                    "Table workflow_a_control.client_account does not exist. "
                    "Run: bash ops/db_migrate.sh"
                )
            _info("workflow_a_control.client_account exists")

            # Every identity this onboarding would claim, in one query. A target
            # that collides on any of them is not a fresh client.
            code = cfg.client_code or ""
            cur.execute(
                """
                SELECT client_id::text AS client_id, client_name, client_code,
                       client_db_name
                  FROM workflow_a_control.client_account
                 WHERE client_name = %s
                    OR (%s <> '' AND client_code = %s)
                    OR client_db_name = %s
                 ORDER BY client_id
                """,
                (cfg.client_name, code, code, cfg.client_db_name),
            )
            candidates = [dict(r) for r in cur.fetchall()]

            if len(candidates) > 1:
                raise OnboardingRefusal(
                    ONBOARDING_REFUSED_AMBIGUOUS_STATE,
                    f"the target identity resolves to {len(candidates)} existing "
                    "client_account rows (client_name / client_code / "
                    "client_db_name collide with more than one client); "
                    "onboarding never guesses which one was meant",
                    {"client_ids": [c["client_id"] for c in candidates]},
                )

            if candidates:
                existing = candidates[0]
                progress = _progress_evidence(
                    cur,
                    client_id=existing["client_id"],
                    client_code=existing.get("client_code") or cfg.client_code,
                )
                progressed = {k: v for k, v in progress.items() if v}
                if progressed:
                    raise OnboardingRefusal(
                        ONBOARDING_REFUSED_PROGRESS_STATE,
                        f"client_id={existing['client_id']} already exists and has "
                        "moved past creation; onboarding never mutates a client "
                        "that has coverage, a recovery row, schedule history or a "
                        "platform business run",
                        {"client_id": existing["client_id"], **progressed},
                    )
                raise OnboardingRefusal(
                    ONBOARDING_REFUSED_CLIENT_EXISTS,
                    f"client_id={existing['client_id']} "
                    f"(client_name={existing['client_name']!r}, "
                    f"client_code={existing.get('client_code')!r}, "
                    f"client_db_name={existing['client_db_name']!r}) already "
                    "exists; onboarding creates a new client and never re-runs "
                    "over an existing one",
                    {"client_id": existing["client_id"]},
                )

            # No client row. Residue from an interrupted earlier attempt is still
            # a non-zero state and must not be built on top of.
            orphans = _orphan_control_plane_evidence(cur, client_code=cfg.client_code)
            if orphans:
                raise OnboardingRefusal(
                    ONBOARDING_REFUSED_PARTIAL_STATE,
                    f"no client_account row exists for client_code "
                    f"{cfg.client_code!r}, but control-plane rows carrying that "
                    "code do; this is partially created state from an earlier "
                    "attempt and must be diagnosed before anything is created",
                    orphans,
                )

            _info(
                f"Zero state confirmed for client_name={cfg.client_name!r} / "
                f"client_code={cfg.client_code or '(none)'} — no client, "
                "schedule, coverage, recovery, history or business run exists"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step: Create client database + user
# ---------------------------------------------------------------------------

def create_client_db(cfg: OnboardConfig, state: OnboardState, *, apply: bool) -> None:
    _section("Client DB — create database and user")

    psycopg = _require("psycopg")
    from psycopg import sql

    if not apply:
        _dry(f"Would create database '{cfg.client_db_name}'")
        _dry(f"Would create user '{cfg.db_username}'")
        _dry(f"Would grant CONNECT on '{cfg.client_db_name}' to '{cfg.db_username}'")
        return

    conn = psycopg.connect(
        _admin_dsn(cfg.client_db_host, cfg.client_db_port, dbname="postgres"),
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (cfg.client_db_name,)
            )
            db_exists = cur.fetchone() is not None

            if db_exists:
                _info(f"Database '{cfg.client_db_name}' already exists")
            else:
                cur.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cfg.client_db_name))
                )
                state.created_db = True
                _info(f"Created database '{cfg.client_db_name}'")

            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (cfg.db_username,)
            )
            user_exists = cur.fetchone() is not None

            if user_exists:
                _info(f"User '{cfg.db_username}' already exists")
                cur.execute(
                    sql.SQL("ALTER USER {} WITH PASSWORD {}").format(
                        sql.Identifier(cfg.db_username),
                        sql.Literal(cfg.db_key),
                    ),
                )
                _info(f"Updated password for user '{cfg.db_username}'")
            else:
                cur.execute(
                    sql.SQL("CREATE USER {} WITH PASSWORD {}").format(
                        sql.Identifier(cfg.db_username),
                        sql.Literal(cfg.db_key),
                    ),
                )
                state.created_user = True
                _info(f"Created user '{cfg.db_username}'")

            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(cfg.client_db_name),
                    sql.Identifier(cfg.db_username),
                )
            )
            _info(f"Granted CONNECT on '{cfg.client_db_name}' to '{cfg.db_username}'")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step: Apply client business DDL
# ---------------------------------------------------------------------------

def apply_client_ddl(cfg: OnboardConfig, *, apply: bool) -> None:
    _section("Client DB — apply DDL")
    psycopg = _require("psycopg")

    if not apply:
        for ddl_path in CLIENT_BUSINESS_DDL_FILES:
            _dry(f"Would apply {ddl_path.name} to '{cfg.client_db_name}'")
        return

    admin_dsn = _admin_dsn(cfg.client_db_host, cfg.client_db_port, dbname=cfg.client_db_name)
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            for ddl_path in CLIENT_BUSINESS_DDL_FILES:
                ddl_sql = ddl_path.read_text(encoding="utf-8")
                cur.execute(ddl_sql)
                _info(f"Applied {ddl_path.name}")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.schema_migrations (
                  filename TEXT PRIMARY KEY,
                  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            for filename in CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED:
                cur.execute(
                    "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING",
                    (filename,),
                )
            _info("Recorded client-business schema migration baseline")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step: Grants
# ---------------------------------------------------------------------------

def apply_grants(cfg: OnboardConfig, *, apply: bool) -> None:
    _section("Client DB — grants")
    psycopg = _require("psycopg")
    from psycopg import sql

    schema = cfg.client_db_schema
    tables = [
        "client_trips",
        "client_speeding_notifications",
        "client_vehicle_daily_fuel",
        "client_vehicle_driver_daily_fuel",
        "eco_driving_weekly_email_send_log",
        "eco_driving_monthly_email_send_log",
        "eco_person_people",
        "eco_person_driver_mappings",
        "eco_person_trip_assignments",
        "eco_person_weekly_stats",
        "eco_person_monthly_stats",
        "eco_person_weekly_email_send_log",
        "eco_person_monthly_email_send_log",
        # Driver Eco Dashboard V1 host delivery ledger. It has to be named here
        # and not left to the migration's own grant block: `apply_client_ddl`
        # runs BEFORE `apply_grants`, so when 049 executes during onboarding
        # `client_trips` carries no grants yet and the block it mirrors from is
        # empty. SELECT/INSERT/UPDATE is exactly what the publisher needs; the
        # lifecycle never deletes a row, which is why 049's own grant block
        # withholds DELETE as well.
        "eco_dashboard_delivery_operation",
    ]
    eco_person_views = [
        "eco_person_driver_mappings_view",
        "eco_person_people_email_view",
        "eco_person_weekly_trends_view",
        "eco_person_monthly_trends_view",
    ]
    eco_person_recalculation_delete_tables = [
        "eco_person_weekly_stats",
        "eco_person_monthly_stats",
    ]
    eco_person_functions = [
        ("eco_person_normalize_driver_name", ["TEXT"]),
        ("eco_person_driver_mappings_normalize_trigger", []),
    ]

    if not apply:
        _dry(f"Would grant USAGE ON SCHEMA {schema} to '{cfg.db_username}'")
        for t in tables:
            _dry(f"Would grant SELECT, INSERT, UPDATE on {schema}.{t} to '{cfg.db_username}'")
        for v in eco_person_views:
            _dry(f"Would grant SELECT on {schema}.{v} to '{cfg.db_username}'")
        for t in eco_person_recalculation_delete_tables:
            _dry(f"Would grant DELETE on {schema}.{t} to '{cfg.db_username}'")
        for function_name, arg_types in eco_person_functions:
            rendered_args = ", ".join(arg_types)
            _dry(f"Would grant EXECUTE on function {schema}.{function_name}({rendered_args}) to '{cfg.db_username}'")
        _dry(f"Would grant USAGE, SELECT on all sequences in {schema} to '{cfg.db_username}'")
        _dry(f"Would grant ops_control marker SELECT and promotion-function EXECUTE to '{cfg.db_username}'")
        _dry(f"Would revoke direct ops_control.environment_identity UPDATE from '{cfg.db_username}'")
        return

    admin_dsn = _admin_dsn(cfg.client_db_host, cfg.client_db_port, dbname=cfg.client_db_name)
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(cfg.db_username)
                    )
                )
                _info(f"Granted USAGE ON SCHEMA {schema}")
            except Exception as e:
                raise OnboardError(f"Grant USAGE ON SCHEMA {schema} failed: {e}") from e

            for t in tables:
                try:
                    cur.execute(
                        sql.SQL("GRANT SELECT, INSERT, UPDATE ON {}.{} TO {}").format(
                            sql.Identifier(schema),
                            sql.Identifier(t),
                            sql.Identifier(cfg.db_username),
                        )
                    )
                    _info(f"Granted SELECT, INSERT, UPDATE on {schema}.{t}")
                except Exception as e:
                    raise OnboardError(
                        f"Grant failed on {schema}.{t}. Table may not exist or DDL not applied: {e}"
                    ) from e

            for v in eco_person_views:
                try:
                    cur.execute(
                        sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                            sql.Identifier(schema),
                            sql.Identifier(v),
                            sql.Identifier(cfg.db_username),
                        )
                    )
                    _info(f"Granted SELECT on {schema}.{v}")
                except Exception as e:
                    raise OnboardError(
                        f"Grant failed on {schema}.{v}. View may not exist or DDL not applied: {e}"
                    ) from e

            for t in eco_person_recalculation_delete_tables:
                try:
                    cur.execute(
                        sql.SQL("GRANT DELETE ON {}.{} TO {}").format(
                            sql.Identifier(schema),
                            sql.Identifier(t),
                            sql.Identifier(cfg.db_username),
                        )
                    )
                    _info(f"Granted DELETE on {schema}.{t}")
                except Exception as e:
                    raise OnboardError(
                        f"Grant DELETE failed on {schema}.{t}. Table may not exist or DDL not applied: {e}"
                    ) from e

            for function_name, arg_types in eco_person_functions:
                try:
                    function_args = sql.SQL(", ").join(sql.SQL(arg_type) for arg_type in arg_types)
                    cur.execute(
                        sql.SQL("GRANT EXECUTE ON FUNCTION {}.{}({}) TO {}").format(
                            sql.Identifier(schema),
                            sql.Identifier(function_name),
                            function_args,
                            sql.Identifier(cfg.db_username),
                        )
                    )
                    rendered_args = ", ".join(arg_types)
                    _info(f"Granted EXECUTE on {schema}.{function_name}({rendered_args})")
                except Exception as e:
                    rendered_args = ", ".join(arg_types)
                    raise OnboardError(
                        f"Grant EXECUTE failed on {schema}.{function_name}({rendered_args}). "
                        f"Function may not exist or DDL not applied: {e}"
                    ) from e

            try:
                cur.execute(
                    sql.SQL(
                        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
                    ).format(
                        sql.Identifier(schema), sql.Identifier(cfg.db_username)
                    )
                )
                _info(f"Granted USAGE, SELECT on all sequences in {schema}")
            except Exception as e:
                raise OnboardError(f"Grant on sequences in {schema} failed: {e}") from e

            try:
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA ops_control TO {}").format(sql.Identifier(cfg.db_username)))
                cur.execute(sql.SQL("GRANT SELECT ON ops_control.environment_identity TO {}").format(sql.Identifier(cfg.db_username)))
                cur.execute(sql.SQL("REVOKE UPDATE ON ops_control.environment_identity FROM {}").format(sql.Identifier(cfg.db_username)))
                cur.execute(sql.SQL(
                    "GRANT EXECUTE ON FUNCTION ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text) TO {}"
                ).format(sql.Identifier(cfg.db_username)))
                _info("Granted least-privilege environment identity promotion capability")
            except Exception as e:
                raise OnboardError(f"Environment identity promotion grant failed: {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step: Workflow B Stage 3 loader permissions
# ---------------------------------------------------------------------------

def apply_workflow_b_stage3_permissions(cfg: OnboardConfig, *, apply: bool) -> None:
    _section("Client DB — Workflow B Stage 3 loader permissions")

    statements = [
        stage3_permissions.build_ensure_loader_role_sql(),
        *stage3_permissions.build_stage3_permission_sql(
            client_db_name=cfg.client_db_name,
            client_db_user=cfg.db_username,
        ),
    ]

    if not apply:
        for statement in statements:
            _dry(statement)
        return

    psycopg = _require("psycopg")
    conn = psycopg.connect(
        _admin_dsn(cfg.client_db_host, cfg.client_db_port, dbname="postgres"),
        autocommit=True,
    )
    try:
        stage3_permissions.ensure_stage3_permissions_for_client(
            conn,
            cfg.client_code or cfg.client_name,
            cfg.client_db_name,
            cfg.db_username,
        )
        _info(
            "Granted Workflow B Stage 3 loader role membership and database "
            f"CONNECT/CREATE on '{cfg.client_db_name}'"
        )
    except Exception as e:
        raise OnboardError(
            "Workflow B Stage 3 permission bootstrap failed. This step requires "
            "PostgreSQL admin credentials allowed to CREATE ROLE and GRANT database privileges: "
            f"{e}"
        ) from e
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step: Insert control-plane row
# ---------------------------------------------------------------------------

def insert_control_plane(
    cfg: OnboardConfig, state: OnboardState, *, apply: bool, cur=None,
) -> Optional[str]:
    """Insert the `client_account` row on a caller-owned cursor.

    `cur` is required whenever `apply` is true, and it is deliberately not
    optional-with-a-fallback: there is no longer any path that opens its own
    autocommit connection and commits this row on its own. The row is created
    inside the single control-plane transaction that also seeds the schedule and
    retention rows and verifies the result, so a later failure leaves nothing
    behind.
    """
    _section("Platform DB — insert client_account")

    provider_password_ref = cfg.api_key_env
    db_password_ref = cfg.db_key_env

    print(f"  provider_basic_auth_username        = '{cfg.api_username}'")
    print(f"  provider_basic_auth_password_secret_ref = '{provider_password_ref}'")
    print(f"  client_db_user                      = '{cfg.db_username}'")
    print(f"  client_db_password_secret_ref        = '{db_password_ref}'")
    print(f"  trip_metrics_population_source      = '{cfg.trip_metrics_population_source}'")

    if not apply:
        _dry("Would INSERT into workflow_a_control.client_account")
        return None

    if cur is None:
        raise OnboardError(
            "insert_control_plane requires the caller's transactional cursor; "
            "onboarding no longer commits the client_account row on its own"
        )
    cur.execute(
        """
        INSERT INTO workflow_a_control.client_account (
            client_name,
            client_code,
            enabled,
            provider_type,
            provider_base_url,
            provider_basic_auth_username,
            provider_basic_auth_password_secret_ref,
            client_db_host,
            client_db_port,
            client_db_name,
            client_db_user,
            client_db_password_secret_ref,
            client_db_schema,
            speed_trigger_filter_text,
            trip_metrics_population_source
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING client_id::text
        """,
        (
            cfg.client_name,
            cfg.client_code,
            cfg.enabled,
            cfg.provider_type,
            cfg.provider_base_url,
            cfg.api_username,
            provider_password_ref,
            cfg.client_db_host,
            cfg.client_db_port,
            cfg.client_db_name,
            cfg.db_username,
            db_password_ref,
            cfg.client_db_schema,
            cfg.speed_trigger_filter_text,
            cfg.trip_metrics_population_source,
        ),
    )
    row = cur.fetchone()
    client_id = row["client_id"]
    state.inserted_client_id = client_id
    _info(f"Inserted client_account: client_id = {client_id} (uncommitted)")
    return client_id


# ---------------------------------------------------------------------------
# Step: Seed default-disabled schedule + retention rows
# ---------------------------------------------------------------------------

def seed_dataset_schedule_and_retention(
    client_id: str, client_code: Optional[str], *, apply: bool, cur=None,
) -> None:
    """Insert default-DISABLED schedule + retention rows for `client_id`.

    Rows are inserted with `enabled=false` so this step is intentionally
    safe — it only creates placeholders the operator can later flip on by
    editing `workflow_a_control.client_dataset_schedule` and
    `workflow_a_control.client_table_retention`.

    Each row is `INSERT ... ON CONFLICT DO NOTHING`. The composite UNIQUE on
    (client_id, dataset_name) / (client_id, table_name) is what catches the
    conflict. Note that this is now a defensive property only: onboarding
    refuses an existing client long before reaching this step, so within one
    invocation the conflict clause can never actually fire.

    Cross-checks against the platform registry happen via the FK constraints
    on `dataset_name` / `table_name`. We additionally validate against the
    Python registry below for early failure.

    When applying, `cur` is the caller's transactional cursor — the same one the
    `client_account` INSERT used — so schedule and retention rows are part of
    the same all-or-nothing unit and cannot survive a failed verification.
    """
    _section("Platform DB — seed default-disabled schedule + retention")

    try:
        from jobs.api.telematics import registry as py_registry
    except ImportError:
        if not apply:
            _dry("Would import jobs.api.telematics.registry to validate dataset/table names")
        else:
            raise OnboardError(
                "Could not import jobs.api.telematics.registry — fix PYTHONPATH or "
                "deploy the package before onboarding."
            )
        py_registry = None  # type: ignore[assignment]

    if py_registry is not None:
        unknown_ds = [d for d in DEFAULT_DATASETS if d not in py_registry.DATASETS]
        unknown_tb = [t for t in DEFAULT_TABLES if t not in py_registry.TABLES]
        if unknown_ds or unknown_tb:
            raise OnboardError(
                f"DEFAULT_DATASETS / DEFAULT_TABLES drift from registry: "
                f"unknown_datasets={unknown_ds} unknown_tables={unknown_tb}"
            )

    if not apply:
        for ds in DEFAULT_DATASETS:
            sched = DEFAULT_DATASET_SCHEDULES.get(ds, {})
            # The same invariant the apply path enforces, so a dry run proves
            # the plan is admissible rather than merely describing it.
            assert_creation_permitted(
                surface=SURFACE_ONBOARDING,
                dataset_name=ds,
                enabled=SEEDED_SCHEDULE_ENABLED,
            )
            _dry(
                f"Would INSERT client_dataset_schedule "
                f"(client_id={client_id}, client_code={client_code or '(none)'}, "
                f"dataset={ds}, enabled=false, "
                f"frequency={sched.get('frequency', 'daily')}, "
                f"run_time={sched.get('run_time', '02:00')}, "
                f"timezone={sched.get('timezone', 'UTC')}, "
                f"event_enrichment_mode=enabled)"
            )
        for tb in DEFAULT_TABLES:
            _dry(
                f"Would INSERT client_table_retention "
                f"(client_id={client_id}, client_code={client_code or '(none)'}, "
                f"table={tb}, enabled=false, retention_days=365)"
            )
        return

    if cur is None:
        raise OnboardError(
            "seed_dataset_schedule_and_retention requires the caller's "
            "transactional cursor; onboarding no longer commits schedule or "
            "retention rows on its own"
        )

    for ds in DEFAULT_DATASETS:
        sched = DEFAULT_DATASET_SCHEDULES.get(ds, {})
        # Three separate things, none of which is the other:
        #   1. the guard below validates the *intended* creation
        #      contract — that onboarding never asks for an enabled
        #      `trips_sync`. A client reaches an enabled schedule only
        #      through the reviewed cold-start state machine, whose last
        #      step is `ops/activate_telematics_trips_schedule.py`;
        #   2. the INSERT writes a disabled schedule through the SQL
        #      literal `FALSE`, not through SEEDED_SCHEDULE_ENABLED, so
        #      the constant is the declared intent rather than the value
        #      the statement carries;
        #   3. the verification re-reads the row on this same cursor,
        #      before the commit, and refuses an enabled `trips_sync`,
        #      which is what actually proves what will be stored.
        assert_creation_permitted(
            surface=SURFACE_ONBOARDING,
            dataset_name=ds,
            enabled=SEEDED_SCHEDULE_ENABLED,
        )
        # M5: onboarding seeds the BASE schedule and only the base schedule.
        # The conflict target must name `run_type` because migration 062 re-keyed
        # `uq_client_dataset_schedule` to (client_id, dataset_name, run_type);
        # the old two-column target no longer matches any unique index and would
        # fail outright. Reconciliation cadences are registered by M6/M7 through
        # their own reviewed surface, never here.
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule (
                client_id, client_code, dataset_name, enabled,
                frequency, day_of_week, day_of_month, run_time, timezone,
                lookback_days, overwrite_existing, run_type
            ) VALUES (
                %s, %s, %s, FALSE,
                %s, %s, %s, %s, %s,
                %s, TRUE, %s
            )
            ON CONFLICT (client_id, dataset_name, run_type) DO NOTHING
            """,
            (
                client_id,
                client_code,
                ds,
                sched.get("frequency", "daily"),
                sched.get("day_of_week"),
                sched.get("day_of_month"),
                sched.get("run_time", "02:00"),
                sched.get("timezone", "UTC"),
                sched.get("lookback_days", 1),
                SCHEDULE_RUN_TYPE_BASE,
            ),
        )
        _info(f"Seeded client_dataset_schedule row (dataset={ds}, enabled=false)")

    for tb in DEFAULT_TABLES:
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_table_retention (
                client_id, client_code, table_name, enabled, retention_days
            ) VALUES (
                %s, %s, %s, FALSE, 365
            )
            ON CONFLICT (client_id, table_name) DO NOTHING
            """,
            (client_id, client_code, tb),
        )
        _info(f"Seeded client_table_retention row (table={tb}, enabled=false)")


# ---------------------------------------------------------------------------
# Step: Provider auth preflight
# ---------------------------------------------------------------------------

def provider_auth_preflight(cfg: OnboardConfig) -> None:
    _section("Provider auth preflight")

    requests = _require("requests")

    endpoint = "/vehicles"
    test_url = f"{cfg.provider_base_url}{endpoint}"
    params = {
        "limit": 1,
        "page": 1,
    }

    print(f"  Lightweight auth check: GET {endpoint}  (limit=1, page=1)")
    try:
        resp = requests.get(
            test_url,
            params=params,
            auth=(cfg.api_username, cfg.api_key),
            timeout=PROVIDER_AUTH_PREFLIGHT_TIMEOUT_S,
        )
    except Exception as e:
        raise OnboardError(f"Provider HTTP request failed: {e}") from e

    if resp.status_code == 200:
        try:
            payload = resp.json()
        except Exception as e:
            raise OnboardError(f"Provider lightweight auth check returned non-JSON response: {e}") from e
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise OnboardError(
                "Provider lightweight auth check returned unexpected JSON shape "
                "(expected object with data array)."
            )
        _info(f"Provider lightweight auth check OK ({endpoint}, HTTP {resp.status_code})")
    elif resp.status_code in (401, 403):
        raise OnboardError(
            f"Provider auth FAILED (HTTP {resp.status_code}). "
            f"Check {cfg.api_username_env} and {cfg.api_key_env}."
        )
    elif resp.status_code == 422:
        _warn(f"Provider returned 422 from {endpoint} — review provider params manually")
    else:
        _warn(f"Provider returned HTTP {resp.status_code} from {endpoint} — review manually")
        _warn(f"Body preview: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Step: Client DB preflight
# ---------------------------------------------------------------------------

def client_db_preflight(cfg: OnboardConfig) -> None:
    _section("Client DB preflight")
    psycopg = _require("psycopg")
    from psycopg.rows import dict_row

    try:
        conn = psycopg.connect(_client_dsn(cfg), row_factory=dict_row, autocommit=True)
    except Exception as e:
        raise OnboardError(f"Cannot connect to client DB as '{cfg.db_username}': {e}") from e

    _info(f"Connected to '{cfg.client_db_name}' as '{cfg.db_username}'")

    try:
        with conn.cursor() as cur:
            for table in ["client_trips", "client_speeding_notifications"]:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s",
                    (cfg.client_db_schema, table),
                )
                if cur.fetchone():
                    _info(f"Table {cfg.client_db_schema}.{table} exists")
                else:
                    raise OnboardError(f"Table {cfg.client_db_schema}.{table} NOT FOUND")

                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s AND column_name='client_code'",
                    (cfg.client_db_schema, table),
                )
                if cur.fetchone():
                    _info(f"  └─ client_code column present")
                else:
                    _warn(f"  └─ client_code column MISSING — apply 010_add_client_code.sql")

            for table in ["client_trips", "client_speeding_notifications"]:
                try:
                    cur.execute(
                        f"SELECT COUNT(*) AS cnt FROM {cfg.client_db_schema}.{table} LIMIT 1"
                    )
                    _info(f"SELECT on {table}: OK")
                except Exception as e:
                    raise OnboardError(f"SELECT on {table} failed (grant issue?): {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def rollback_onboarding(state: OnboardState, cfg: OnboardConfig) -> bool:
    """
    Best-effort rollback of resources created during this run.
    Returns True if all cleanup succeeded, False if any step failed.
    """
    if not state.created_db and not state.created_user and not state.inserted_client_id:
        _section("Rollback")
        _info("No resources were created by this run — nothing to roll back")
        return True

    _section("Rollback — best-effort cleanup")
    all_ok = True
    psycopg = _require("psycopg")
    from psycopg import sql

    # 1. Delete control-plane row
    if state.inserted_client_id:
        try:
            conn = _platform_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM workflow_a_control.client_account WHERE client_id=%s",
                        (state.inserted_client_id,),
                    )
                _info(f"Deleted control-plane row: client_id={state.inserted_client_id}")
                state.inserted_client_id = None
            finally:
                conn.close()
        except Exception as e:
            _err(f"Failed to delete control-plane row {state.inserted_client_id}: {e}")
            all_ok = False

    # 2. Drop database (must terminate connections first)
    if state.created_db:
        try:
            conn = psycopg.connect(
                _admin_dsn(cfg.client_db_host, cfg.client_db_port, dbname="postgres"),
                autocommit=True,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname=%s AND pid <> pg_backend_pid()",
                        (cfg.client_db_name,),
                    )
                    cur.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(
                            sql.Identifier(cfg.client_db_name)
                        )
                    )
                _info(f"Dropped database '{cfg.client_db_name}'")
                state.created_db = False
            finally:
                conn.close()
        except Exception as e:
            _err(f"Failed to drop database '{cfg.client_db_name}': {e}")
            all_ok = False

    # 3. Drop user
    if state.created_user:
        try:
            conn = psycopg.connect(
                _admin_dsn(cfg.client_db_host, cfg.client_db_port, dbname="postgres"),
                autocommit=True,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("DROP USER IF EXISTS {}").format(
                            sql.Identifier(cfg.db_username)
                        )
                    )
                _info(f"Dropped user '{cfg.db_username}'")
                state.created_user = False
            finally:
                conn.close()
        except Exception as e:
            _err(f"Failed to drop user '{cfg.db_username}': {e}")
            all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# Output: manual run command
# ---------------------------------------------------------------------------

#: The normative onboarding state machine. A client must not skip a state, and
#: the mere existence of rows — including a coverage row — never means a later
#: state has been reached. Onboarding itself completes exactly one of them.
ONBOARDING_STATE_MACHINE = (
    "CREATED_DISABLED_STRICT",
    "ZERO_STATE_VERIFIED",
    "BASELINE_CREATED",
    "COMPATIBILITY_MODE_SET",
    "RECOVERY_EXECUTED_AND_COMMITTED",
    "COVERAGE_VERIFIED",
    "SCHEDULE_ACTIVATED",
    "FIRST_NATURAL_FIRE_VERIFIED",
    "PRODUCTION_READY",
)

#: The only state this script may leave a client in.
ONBOARDING_TERMINAL_STATE = "CREATED_DISABLED_STRICT"

ONBOARDING_STATE_VERSION = "telematics-onboarding-state/1"


def verify_onboarding_state_on_cursor(
    cur, client_id: str, client_code: Optional[str],
) -> dict:
    """Prove the state on this cursor is exactly the first state, or raise.

    This is the *definitive* verification, and the apply path runs it on the
    same transactional cursor that created the rows, immediately **before** the
    commit. Verifying after a commit — as this used to — meant a refusal
    happened with the rows already durable, so a rejected onboarding left a
    client, ten schedule rows and fourteen retention rows behind.

    It refuses:

      * an ambiguous client identity (a duplicated `client_code`);
      * a duplicated or absent authoritative `trips_sync` schedule;
      * an enabled `trips_sync` schedule;
      * a pagination mode other than `strict_meta`.

    Returns the onboarding state reference later tools bind to.
    """
    cur.execute(
        """
        SELECT client_id::text AS client_id, client_code,
               trips_pagination_mode
          FROM workflow_a_control.client_account
         WHERE client_id = %s
        """,
        (client_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if len(rows) != 1:
        raise OnboardError(
            f"client_id {client_id} resolves to {len(rows)} "
            "client_account rows; exactly one is required"
        )
    account = rows[0]
    if client_code:
        cur.execute(
            "SELECT count(*) AS n"
            "  FROM workflow_a_control.client_account"
            " WHERE client_code = %s",
            (client_code,),
        )
        duplicates = int((cur.fetchone() or {})["n"])
        if duplicates != 1:
            raise OnboardError(
                f"client_code {client_code!r} resolves to {duplicates} "
                "clients; onboarding refuses an ambiguous identity"
            )

    mode = str(account.get("trips_pagination_mode") or "")
    if mode != ONBOARDED_TRIPS_PAGINATION_MODE:
        raise OnboardError(
            f"the new client is {mode!r}, expected "
            f"{ONBOARDED_TRIPS_PAGINATION_MODE!r}; onboarding creates a "
            "strict_meta client and the compatibility mode is set later, "
            "as its own reviewed state-machine step"
        )

    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id, enabled
          FROM workflow_a_control.client_dataset_schedule
         WHERE client_id = %s AND dataset_name = %s
           AND run_type = %s
         ORDER BY schedule_id
        """,
        (client_id, TRIPS_SYNC_DATASET_NAME, SCHEDULE_RUN_TYPE_BASE),
    )
    schedules = [dict(r) for r in cur.fetchall()]
    if len(schedules) != 1:
        raise OnboardError(
            f"{TRIPS_SYNC_DATASET_NAME} resolves to {len(schedules)} "
            "schedule rows for this client; exactly one authoritative "
            "schedule is required and no competing schedule may exist"
        )
    if bool(schedules[0]["enabled"]):
        raise OnboardError(
            f"the {TRIPS_SYNC_DATASET_NAME} schedule is enabled; "
            "onboarding never enables it"
        )
    return {
        "onboarding_state_version": ONBOARDING_STATE_VERSION,
        "state": ONBOARDING_TERMINAL_STATE,
        "client_id": client_id,
        "client_code": account.get("client_code"),
        "dataset_name": TRIPS_SYNC_DATASET_NAME,
        "schedule_id": schedules[0]["schedule_id"],
        "trips_pagination_mode": mode,
        "trips_sync_schedule_enabled": False,
        "production_ready": False,
        "reporting_ready": False,
        "verified_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }


def verify_onboarding_state(
    client_id: str, client_code: Optional[str],
) -> dict:
    """Read-only re-verification on its own connection.

    Retained for inspection and for callers that want to re-check a client after
    the fact. The apply path does **not** use it: it verifies inside its own
    transaction, before committing, through
    `verify_onboarding_state_on_cursor`.
    """
    conn = _platform_conn()
    try:
        with conn.cursor() as cur:
            return verify_onboarding_state_on_cursor(cur, client_id, client_code)
    finally:
        conn.close()


def create_control_plane_state(cfg: OnboardConfig, state: OnboardState) -> dict:
    """Create and verify the whole control-plane state in one transaction.

    The complete unit is:

        BEGIN
          INSERT client_account
          INSERT 10 × client_dataset_schedule   (all disabled)
          INSERT 14 × client_table_retention    (all disabled)
          verify the exact newly created state
        COMMIT

    Every affected control-plane resource lives in the same platform database,
    so the definitive verification is performed inside this transaction and
    before the commit — the preferred option of the two the review allows. A
    verification failure rolls the whole thing back: zero newly created target
    rows, and every pre-existing row untouched, because nothing outside the
    inserted `client_id` is ever written or read for update.

    `state.inserted_client_id` is cleared on rollback. It is the flag the
    compensating cleanup uses to decide whether a control-plane row of *this
    invocation* exists, and after a rollback none does; leaving it set would
    make the cleanup issue a DELETE for a row that was never committed.
    """
    _section("Platform DB — control-plane transaction")
    psycopg = _require("psycopg")
    from psycopg.rows import dict_row

    conn = psycopg.connect(_platform_dsn(), row_factory=dict_row, autocommit=False)
    try:
        with conn.cursor() as cur:
            client_id = insert_control_plane(cfg, state, apply=True, cur=cur)
            if not client_id:
                raise OnboardError("client_account INSERT returned no client_id")
            seed_dataset_schedule_and_retention(
                client_id, cfg.client_code, apply=True, cur=cur,
            )
            _section("Platform DB — pre-commit verification")
            verified = verify_onboarding_state_on_cursor(
                cur, client_id, cfg.client_code,
            )
        conn.commit()
        state.control_plane_committed = True
        _info(
            "Control-plane transaction verified and committed "
            f"(client_id = {client_id})"
        )
        return verified
    except BaseException:
        try:
            conn.rollback()
            _err(
                "Control-plane transaction rolled back — no client_account, "
                "schedule or retention row was created by this invocation"
            )
        finally:
            # Nothing was committed, so there is no control-plane row for the
            # compensating cleanup to remove.
            state.inserted_client_id = None
        raise
    finally:
        conn.close()


def print_refusal_guidance(refusal: OnboardingRefusal, cfg: OnboardConfig) -> None:
    """Say what was found, and point at the procedure that actually applies.

    Deliberately never prints an onboarding state and never mentions
    `CREATED_DISABLED_STRICT`: this target was not created, and claiming the
    first state for an existing or progressed client is the exact confusion
    this refusal exists to prevent. It also never offers a direct business-job
    command — diagnosis is read-only, and resume/decommissioning are separate,
    separately reviewed procedures that this script does not implement.
    """
    _section(f"Onboarding REFUSED — {refusal.code}")

    _err(str(refusal))
    if refusal.evidence:
        print()
        print("  Observed state:")
        for key in sorted(refusal.evidence):
            print(f"    {key}: {refusal.evidence[key]}")
    print()
    _warn(
        "Nothing was created, changed or deleted. No database, role, "
        "control-plane row, schedule or provider request was touched."
    )
    print()
    print("  This script creates a NEW client only. Choose the procedure that")
    print("  matches what was found — it is never this script:")
    print()
    print("  1. Read-only diagnosis (always safe, always first):")
    print()
    print(textwrap.dedent(f"""\
        cd /opt/log-platform

        PYTHONPATH="$PWD" .venv/bin/python ops/audit_telematics_cold_start.py \\
          --client-code {cfg.client_code or '<CLIENT_CODE>'} \\
          --dataset {TRIPS_SYNC_DATASET_NAME} \\
          --expected-environment <environment> \\
          --expected-platform-uuid <platform uuid>
    """))
    print("  2. Resuming a partially completed cold start: follow the state")
    print("     machine from the state the diagnosis reports. Each state has its")
    print("     own reviewed tool — see docs/07_operations.md §5.5. There is no")
    print("     resume workflow in this script, by design.")
    print()
    print("  3. Retiring the target instead: controlled decommissioning is a")
    print("     separate operator procedure. Do NOT delete control-plane rows by")
    print("     hand to make this script proceed.")
    print()
    print("  Do NOT run jobs.api.telematics.sync_trips_and_speeding to 'check' the")
    print("  client: against a disabled schedule it skips its work and still")
    print("  exits 0.")


def print_next_state_machine_step(state: dict, cfg: OnboardConfig) -> None:
    """State plainly that the account is not production-ready, and what is next.

    The historical "first manual run command" printed here was actively
    misleading: run directly against a disabled schedule, the sync job returns
    without doing any work and exits 0. Printing that command as the next step
    is what made a skipped run look like a first successful run. It is replaced
    by the audit, which reads nothing but state and writes nothing at all.
    """
    _section("Onboarding result — NOT production-ready")

    _warn(
        "This account is NOT production-ready and NOT reporting-ready. "
        "Onboarding completes exactly one state of the cold-start state machine."
    )
    print(f"  onboarding_state:     {state['state']} "
          f"(1 of {len(ONBOARDING_STATE_MACHINE)})")
    print(f"  client_id:            {state['client_id']}")
    print(f"  client_code:          {state['client_code'] or '(none)'}")
    print(f"  schedule_id:          {state['schedule_id']}")
    print(f"  trips_pagination_mode:{state['trips_pagination_mode']}")
    print(f"  trips_sync enabled:   {state['trips_sync_schedule_enabled']}")
    print()
    print("  State machine (a client must not skip a state):")
    for index, name in enumerate(ONBOARDING_STATE_MACHINE, start=1):
        marker = "✓" if name == state["state"] else " "
        print(f"    [{marker}] {index}. {name}")
    print()
    _warn(
        "A coverage row existing does NOT mean the client is reporting-ready. "
        "Coverage advances only after a committed business execution."
    )
    print()
    print("  Required next step — ZERO_STATE_VERIFIED (read-only, no writes):")
    print()
    print(textwrap.dedent(f"""\
        cd /opt/log-platform

        PYTHONPATH="$PWD" .venv/bin/python ops/audit_telematics_cold_start.py \\
          --client-code {state['client_code'] or '<CLIENT_CODE>'} \\
          --dataset {TRIPS_SYNC_DATASET_NAME} \\
          --expected-environment <environment> \\
          --expected-platform-uuid <platform uuid>
    """))
    print("  Do NOT run jobs.api.telematics.sync_trips_and_speeding directly at")
    print("  this point: the schedule is disabled, so the job skips its work and")
    print("  still exits 0. See docs/07_operations.md §5.5 for the full sequence.")
    print()
    print("  Onboarding state reference (for the tools that follow):")
    print("  " + json.dumps(state, sort_keys=True))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # Loaded HERE rather than at import time: importing this module — which a
    # deterministic test does, for its DDL and grant declarations — must have
    # no environment side effect at all. Execution as a program still resolves
    # configuration exactly as before, because nothing above `main` reads it.
    _load_dotenv_if_present()

    parser = argparse.ArgumentParser(
        description="Onboard a new Workflow A (Telematics) client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, help="Path to client YAML config")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes (default: dry-run)")
    parser.add_argument("--skip-provider-auth-check", action="store_true")
    parser.add_argument("--skip-db-create", action="store_true")
    parser.add_argument("--skip-ddl", action="store_true")
    parser.add_argument("--skip-grants", action="store_true")
    parser.add_argument("--skip-control-plane", action="store_true")
    # Retained for command-line compatibility only. Onboarding no longer prints
    # a direct sync command: at CREATED_DISABLED_STRICT that command would skip
    # its work and still exit 0, which is exactly the confusion this hardening
    # removes. The window is chosen later, when a recovery is approved.
    parser.add_argument(
        "--window-start-ts", default=None,
        help="Deprecated no-op; the recovery window is approved separately",
    )
    parser.add_argument(
        "--window-end-ts", default=None,
        help="Deprecated no-op; the recovery window is approved separately",
    )
    args = parser.parse_args()

    mode = _green("APPLY") if args.apply else _yellow("DRY-RUN")
    print(f"\n{'═' * 60}")
    print(f"  Workflow A — Client Onboarding  [{mode}]")
    print(f"{'═' * 60}")

    cfg = load_config(args.config)

    _section("Configuration summary")
    print(f"  client_name:              {cfg.client_name}")
    print(f"  client_code:              {cfg.client_code or '(none)'}")
    print(f"  provider_type:            {cfg.provider_type}")
    print(f"  provider_base_url:        {cfg.provider_base_url}")
    print(f"  client_db_host:           {cfg.client_db_host}")
    print(f"  client_db_port:           {cfg.client_db_port}")
    print(f"  client_db_name:           {cfg.client_db_name}")
    print(f"  client_db_schema:         {cfg.client_db_schema}")
    print(f"  speed_trigger_filter_text:{cfg.speed_trigger_filter_text}")
    print(f"  trip_metrics_population_source: {cfg.trip_metrics_population_source}")
    print(f"  env vars resolved:        {cfg.api_username_env}, {cfg.api_key_env}, "
          f"{cfg.db_username_env}, {cfg.db_key_env}")

    # -- DRY-RUN path: no rollback logic needed --
    if not args.apply:
        try:
            preflight_all(cfg, args=args)
        except OnboardingRefusal as refusal:
            # A dry run refuses on exactly the same evidence as an apply, so the
            # operator learns the target is ineligible before ever passing
            # `--apply`, and learns it without a provider request or a write.
            print_refusal_guidance(refusal, cfg)
            return 1
        except OnboardError as e:
            _err(str(e))
            return 1

        if not args.skip_db_create:
            _section("Client DB — create database and user")
            _dry(f"Would create database '{cfg.client_db_name}'")
            _dry(f"Would create user '{cfg.db_username}'")

        if not args.skip_ddl:
            _section("Client DB — apply DDL")
            for ddl_path in CLIENT_BUSINESS_DDL_FILES:
                _dry(f"Would apply {ddl_path.name} to '{cfg.client_db_name}'")

        if not args.skip_grants:
            apply_grants(cfg, apply=False)
            apply_workflow_b_stage3_permissions(cfg, apply=False)

        _section("Client DB preflight")
        _dry("Would validate client DB connectivity, tables, and columns")

        if not args.skip_control_plane:
            insert_control_plane(cfg, OnboardState(), apply=False)
            seed_dataset_schedule_and_retention(
                "<new_client_id>", cfg.client_code, apply=False,
            )
            _dry(
                "Would verify the newly created state on the same transaction "
                "and commit only if it is exactly CREATED_DISABLED_STRICT"
            )

        _section("Summary")
        _info("Dry-run complete — no changes made")
        _info("Re-run with --apply to execute")
        return 0

    # -- APPLY path: with rollback on failure --
    state = OnboardState()

    onboarding_state: Optional[dict] = None
    client_id: Optional[str] = None

    try:
        # Phase 1: read-only preflight. This refuses any target that is not a
        # genuine zero state, before a single write and before any provider
        # request, and raises `OnboardingRefusal` when it does.
        preflight_all(cfg, args=args)

        # Phase 2: client DB provisioning
        if not args.skip_db_create:
            create_client_db(cfg, state, apply=True)
        else:
            _section("Client DB — create database and user")
            _warn("Skipped (--skip-db-create)")

        if not args.skip_ddl:
            apply_client_ddl(cfg, apply=True)
        else:
            _section("Client DB — apply DDL")
            _warn("Skipped (--skip-ddl)")

        if not args.skip_grants:
            apply_grants(cfg, apply=True)
            apply_workflow_b_stage3_permissions(cfg, apply=True)
        else:
            _section("Client DB — grants")
            _warn("Skipped (--skip-grants)")

        # Phase 3: validate client DB
        client_db_preflight(cfg)

        # Phase 4: the complete control-plane state — insert, seed, verify and
        # commit — as one transaction. Verification happens inside it, before
        # the commit, so a failure leaves zero newly created rows.
        if not args.skip_control_plane:
            onboarding_state = create_control_plane_state(cfg, state)
            client_id = onboarding_state["client_id"]
        else:
            _section("Platform DB — control-plane transaction")
            _warn("Skipped (--skip-control-plane)")

    except OnboardingRefusal as refusal:
        # A refusal happens before any mutation is attempted, so there is
        # nothing of this invocation to compensate. Running the cleanup here
        # would risk acting on resources this invocation did not create.
        print_refusal_guidance(refusal, cfg)
        _section("Summary")
        _err(f"Onboarding REFUSED — {refusal.code}; nothing was created")
        return 1
    except Exception as e:
        _section("ERROR")
        _err(str(e))
        rollback_ok = rollback_onboarding(state, cfg)

        _section("Summary")
        if rollback_ok:
            _err("Onboarding FAILED — rollback completed successfully")
        else:
            _err("Onboarding FAILED — rollback was PARTIAL (see errors above)")
            _err("Manual cleanup may be required")
        return 1

    # -- Success --
    _section("Summary")
    skipped_any = (
        args.skip_db_create or args.skip_ddl or args.skip_grants
        or args.skip_control_plane or args.skip_provider_auth_check
    )
    if args.skip_control_plane:
        _warn(
            "Control-plane steps were skipped (--skip-control-plane); no client "
            "was created, the onboarding state cannot be verified and no state "
            "reference is emitted."
        )
        return 0
    if onboarding_state and client_id:
        # Deliberately not "onboarded successfully": rows exist, and that is a
        # different claim from the client being usable. The state reported here
        # is the one that was verified inside the transaction that created it —
        # it is not re-derived, and it is never assumed from the fact that the
        # inserts ran.
        _info(f"Control-plane rows created and verified: client_id = {client_id}")
        if skipped_any:
            _warn("Some steps were skipped. Verify completeness manually.")
        print_next_state_machine_step(onboarding_state, cfg)
    else:
        _warn("No client_id available — check steps above")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
