#!/usr/bin/env python3
"""Dry-run-first recompute of the observed-delivery-lag daily slices (M-LAG).

Specification of record:
  docs/21_telematics_delivery_lag_trace.md §6 (why slices are recomputed, and how
    deep), §7 (buckets and percentiles), §8 (discovery attribution and why it is
    resolved here rather than denormalized onto the trip)
  db/migrations/063_workflow_a_trip_delivery_lag_daily.sql (the target relation)
  db/client_business/048_client_trips_first_seen_response_received_at.sql
    (the immutable fact every metric is computed from)
  jobs/api/telematics/delivery_lag.py (every calculation; this module does I/O only)

WHAT THIS DOES.
    For each selected client, recomputes a contiguous trailing window of
    ``(client, trip_end_date)`` slices from the CURRENT contents of
    ``client_trips`` and upserts them into
    ``workflow_a_control.trip_delivery_lag_daily``.

    Recompute rather than increment, because ``end_timestamp`` is mutable: a trip
    can be observed while still open and have its end corrected later, moving it
    between date slices. A counter would keep the trip in the old slice forever
    and never add it to the new one. Rewriting both slices wholesale makes that
    self-correcting, and makes re-running the job idempotent.

    The window depth is derived from the client's deepest enabled
    ``lookback_days`` plus a margin, because a fire can only correct a trip
    inside its own effective window — so nothing older can still move.

WHAT THIS DOES NOT DO.
    * no provider request of any kind — it reads two databases and nothing else;
    * no write to ``client_trips``, any schedule, any coverage row, any
      ``provider_request_log`` row or any client configuration;
    * no backfill and no imputation. A trip without first-seen provenance is
      counted in ``trips_total`` and excluded from every distribution;
    * no threshold, no alert, no anomaly classification. Those need a measured
      distribution, which is what this produces (docs/21 §10).

Typical use — dry-run first, always::

    PYTHONPATH="$PWD" python3 ops/aggregate_telematics_delivery_lag.py \\
        --client-code ALPHA00001 \\
        --expected-environment production \\
        --expected-platform-uuid <uuid>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.timezone_utils import (  # noqa: E402
    DEFAULT_BUSINESS_TIMEZONE,
    set_pg_session_timezone,
)
from jobs.api.telematics.delivery_lag import (  # noqa: E402
    BUCKET_NAMES,
    DISCOVERY_COLUMNS,
    DISCOVERY_UNATTRIBUTED,
    TripObservation,
    build_daily_slice,
    recompute_horizon_days,
    weekly_guarantee_from_schedules,
)
from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RECONCILIATION_RUN_TYPES,
    TRIPS_SYNC_DATASET_NAME,
)
from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: E402
from ops.audit_telematics_coverage_bootstrap import (  # noqa: E402
    _load_dotenv,
    canonical_uuid,
    platform_dsn_from_env,
    verify_platform_identity,
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_REFUSED = 4
EXIT_RUNTIME_FAILURE = 5

MIGRATION_CEILING = "063_workflow_a_trip_delivery_lag_daily.sql"
CLIENT_MIGRATION_COLUMN = "first_seen_response_received_at_utc"

#: Cadence period in days per reconciliation role, for the guarantee arithmetic.
#: `frequency` is constrained to match the role by migration 062's CHECK, so this
#: mapping is a restatement of a rule the database already enforces.
ROLE_CADENCE_PERIOD_DAYS = {
    "WEEKLY_RECONCILIATION": 7,
    # 31 is the worst case, not the mean: a guarantee must hold in the longest
    # month, including October's 31.0417 d across the autumn DST transition.
    "MONTHLY_RECONCILIATION": 31,
}

SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")
COMPUTED_BY = "ops/aggregate_telematics_delivery_lag.py"


class LagAggregationRefused(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str, exit_code: int = EXIT_REFUSED) -> LagAggregationRefused:
    return LagAggregationRefused(code, message, exit_code)


def _safe_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_TOKEN_RE.match(text):
        raise _refuse(
            "INVALID_PARAMETER",
            f"{label} must match {SAFE_TOKEN_RE.pattern}",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def _require_psycopg():
    try:
        import psycopg  # noqa: F401
        from psycopg.rows import dict_row  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise _refuse(
            "DEPENDENCY_MISSING", "psycopg is required", EXIT_RUNTIME_FAILURE
        ) from exc
    import psycopg
    from psycopg.rows import dict_row

    return psycopg, dict_row


# ---------------------------------------------------------------------------
# Platform reads
# ---------------------------------------------------------------------------

def verify_migration_ceiling(cur) -> None:
    cur.execute("SELECT filename FROM public.schema_migrations ORDER BY filename")
    applied = {str(r["filename"]) for r in cur.fetchall()}
    if MIGRATION_CEILING not in applied:
        raise _refuse(
            "MIGRATION_CEILING_MISSING",
            f"{MIGRATION_CEILING} is not applied; there is nowhere to write the "
            "slices",
        )


def resolve_clients(cur, *, client_code: Optional[str]) -> List[Dict[str, Any]]:
    """Enabled clients that have a `trips_sync` schedule of any cadence."""
    sql = """
        SELECT DISTINCT ca.client_id::text AS client_id,
               ca.client_code            AS client_code,
               ca.client_db_host, ca.client_db_port, ca.client_db_name,
               ca.client_db_user, ca.client_db_password_secret_ref,
               ca.client_db_schema
          FROM workflow_a_control.client_account ca
          JOIN workflow_a_control.client_dataset_schedule cds
            ON cds.client_id = ca.client_id
           AND cds.dataset_name = %s
         WHERE ca.enabled = true
    """
    params: List[Any] = [TRIPS_SYNC_DATASET_NAME]
    if client_code:
        sql += " AND ca.client_code = %s"
        params.append(client_code)
    sql += " ORDER BY ca.client_code"
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    if client_code and not rows:
        raise _refuse(
            "CLIENT_NOT_FOUND",
            f"no enabled client with a {TRIPS_SYNC_DATASET_NAME} schedule for "
            f"{client_code!r}",
        )
    return rows


def read_cadences(cur, *, client_id: str) -> Tuple[List[int], List[Tuple[int, int]]]:
    """(enabled lookbacks, reconciliation (lookback, period) pairs).

    Both are read from the live schedule rather than assumed: the recompute
    horizon and the guarantee boundary are properties of what is actually
    enabled, and hard-coding either is how a stale boundary outlives the cadence
    that justified it.
    """
    cur.execute(
        """
        SELECT run_type, lookback_days
          FROM workflow_a_control.client_dataset_schedule
         WHERE client_id = %s AND dataset_name = %s AND enabled = true
        """,
        (client_id, TRIPS_SYNC_DATASET_NAME),
    )
    lookbacks: List[int] = []
    reconciliation: List[Tuple[int, int]] = []
    for row in cur.fetchall():
        lookback = int(row["lookback_days"])
        lookbacks.append(lookback)
        role = str(row["run_type"])
        if role in SCHEDULE_RECONCILIATION_RUN_TYPES:
            period = ROLE_CADENCE_PERIOD_DAYS.get(role)
            if period is not None:
                reconciliation.append((lookback, period))
    return lookbacks, reconciliation


def resolve_run_types(cur, *, request_ids: Sequence[str]) -> Dict[str, str]:
    """request_id -> the `run_type` of the schedule whose fire made the request.

    Resolvable only while the request row survives its 180-day horizon. A
    request_id absent from the result is reported as unattributed and is NEVER
    defaulted to the base role — see `delivery_lag.discovery_column_for`.

    Only FINALIZED rows carry a `run_history_id` at all (migration 061's
    `ck_provider_request_log_pending_claims_nothing`), so a PENDING request fact
    left behind by a rolled-back business transaction resolves to nothing here,
    which is correct: no committed trip points at it.
    """
    if not request_ids:
        return {}
    cur.execute(
        """
        SELECT prl.request_id::text AS request_id,
               cds.run_type         AS run_type
          FROM workflow_a_control.provider_request_log prl
          JOIN workflow_a_control.client_schedule_run_history csrh
            ON csrh.run_history_id = prl.run_history_id
          JOIN workflow_a_control.client_dataset_schedule cds
            ON cds.schedule_id = csrh.schedule_id
         WHERE prl.request_id = ANY(%s)
        """,
        (list(request_ids),),
    )
    return {str(r["request_id"]): str(r["run_type"]) for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Client business read
# ---------------------------------------------------------------------------

def _client_conn(client: Dict[str, Any]):
    psycopg, _ = _require_psycopg()
    dsn = (
        f"host={client['client_db_host']} "
        f"port={client['client_db_port']} "
        f"dbname={client['client_db_name']} "
        f"user={client['client_db_user']} "
        f"password={resolve_secret(client['client_db_password_secret_ref'])}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def read_observations(
    cur, *, window_start: datetime, window_end: datetime,
) -> List[Dict[str, Any]]:
    """Every trip whose CURRENT end falls in the recompute window.

    Keyed on the current `end_timestamp`, not on any stored slice assignment.
    That is the whole correction mechanism: a trip that moved date is simply
    found under its new date on the next pass.
    """
    cur.execute(
        """
        SELECT provider_trip_id,
               end_timestamp,
               first_seen_request_id::text AS first_seen_request_id,
               first_seen_response_received_at_utc
          FROM public.client_trips
         WHERE end_timestamp >= %s AND end_timestamp < %s
        """,
        (window_start, window_end),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Slice assembly and upsert
# ---------------------------------------------------------------------------

UPSERT_COLUMNS = (
    ("client_id", "client_code", "trip_end_date", "trips_total",
     "trips_with_provenance", "trips_provenance_pending",
     "lag_p50_seconds", "lag_p90_seconds",
     "lag_p95_seconds", "lag_max_seconds", "lag_min_seconds",
     "weekly_guarantee_seconds")
    + BUCKET_NAMES
    + tuple(DISCOVERY_COLUMNS.values()) + (DISCOVERY_UNATTRIBUTED,)
    + ("recompute_horizon_days", "computed_by")
)

#: Every column except the identity is rewritten on conflict. Rewriting all of
#: them is what makes the upsert a recompute rather than a merge: a metric that
#: was carried over from a previous pass would be exactly the staleness this
#: design exists to remove.
_UPSERT_SET = ", ".join(
    f"{name}=EXCLUDED.{name}"
    for name in UPSERT_COLUMNS
    if name not in ("client_id", "trip_end_date")
) + ", computed_at=now()"

UPSERT_SQL = (
    "INSERT INTO workflow_a_control.trip_delivery_lag_daily ({columns}) "
    "VALUES ({marks}) "
    "ON CONFLICT (client_id, trip_end_date) DO UPDATE SET {sets}"
).format(
    columns=", ".join(UPSERT_COLUMNS),
    marks=", ".join(["%s"] * len(UPSERT_COLUMNS)),
    sets=_UPSERT_SET,
)


def build_slices(
    *,
    observations: Sequence[Dict[str, Any]],
    run_types: Dict[str, str],
    dates: Sequence[date],
    business_tz: ZoneInfo,
    weekly_guarantee_seconds: int,
) -> List[Dict[str, Any]]:
    by_date: Dict[date, List[TripObservation]] = {d: [] for d in dates}
    for row in observations:
        end_ts = row["end_timestamp"]
        if end_ts is None:
            # No end: it belongs to no date slice and has no lag. Skipped rather
            # than bucketed into an arbitrary day.
            continue
        slice_date = end_ts.astimezone(business_tz).date()
        if slice_date not in by_date:
            continue
        request_id = row.get("first_seen_request_id")
        by_date[slice_date].append(
            TripObservation(
                provider_trip_id=int(row["provider_trip_id"]),
                end_timestamp=end_ts,
                first_seen_response_received_at_utc=(
                    row["first_seen_response_received_at_utc"]
                ),
                first_seen_request_id=request_id,
                first_seen_run_type=(
                    run_types.get(request_id) if request_id else None
                ),
            )
        )
    return [
        build_daily_slice(
            trip_end_date=d,
            observations=by_date[d],
            weekly_guarantee_seconds=weekly_guarantee_seconds,
        ).as_row()
        for d in dates
    ]


def run(args) -> Tuple[int, Dict[str, Any]]:
    client_code = (
        _safe_token(args.client_code, label="--client-code")
        if args.client_code else None
    )
    dry_run = not bool(args.execute)
    if not dry_run and str(args.confirm or "") != "RECOMPUTE":
        raise _refuse(
            "CONFIRMATION_MISMATCH",
            "--confirm RECOMPUTE is required for a write",
            EXIT_INVALID_PARAMETERS,
        )

    business_tz = ZoneInfo(args.business_timezone or DEFAULT_BUSINESS_TIMEZONE)
    today_local = (
        datetime.now(timezone.utc).astimezone(business_tz).date()
        if args.through_date is None
        else date.fromisoformat(args.through_date)
    )

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    psycopg, dict_row = _require_psycopg()

    report: Dict[str, Any] = {
        "mode": "DRY_RUN" if dry_run else "EXECUTE",
        "business_timezone": str(business_tz),
        "through_date": today_local.isoformat(),
        "clients": [],
    }

    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as pconn:
        with pconn.cursor() as pcur:
            report["platform_identity"] = verify_platform_identity(
                pcur,
                expected_environment=str(args.expected_environment),
                expected_platform_uuid=expected_uuid,
            )
            verify_migration_ceiling(pcur)
            clients = resolve_clients(pcur, client_code=client_code)

            for client in clients:
                lookbacks, reconciliation = read_cadences(
                    pcur, client_id=client["client_id"]
                )
                horizon = (
                    args.horizon_days
                    if args.horizon_days is not None
                    else recompute_horizon_days(enabled_lookback_days=lookbacks)
                )
                guarantee = weekly_guarantee_from_schedules(
                    reconciliation_cadences=reconciliation
                )
                dates = [
                    today_local - timedelta(days=offset)
                    for offset in range(horizon - 1, -1, -1)
                ]
                window_start = datetime.combine(
                    dates[0], time.min, tzinfo=business_tz
                )
                window_end = datetime.combine(
                    dates[-1] + timedelta(days=1), time.min, tzinfo=business_tz
                )

                with _client_conn(client) as cconn:
                    with cconn.cursor(row_factory=dict_row) as ccur:
                        _assert_client_column(ccur)
                        observations = read_observations(
                            ccur, window_start=window_start,
                            window_end=window_end,
                        )

                request_ids = sorted({
                    row["first_seen_request_id"]
                    for row in observations
                    if row.get("first_seen_request_id")
                })
                run_types = resolve_run_types(pcur, request_ids=request_ids)

                rows = build_slices(
                    observations=observations,
                    run_types=run_types,
                    dates=dates,
                    business_tz=business_tz,
                    weekly_guarantee_seconds=guarantee,
                )

                summary = {
                    "client_code": client["client_code"],
                    "client_id": client["client_id"],
                    "enabled_lookbacks": sorted(lookbacks),
                    "reconciliation_cadences": reconciliation,
                    "recompute_horizon_days": horizon,
                    "weekly_guarantee_seconds": guarantee,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "slices": len(rows),
                    "trips_scanned": len(observations),
                    "trips_with_provenance": sum(
                        r["trips_with_provenance"] for r in rows
                    ),
                    # Non-zero means the recomputed window is NOT historically
                    # complete: those trips have a real observation that is not
                    # yet on the trip row. Surfaced in the report so an operator
                    # sees it without querying the slices.
                    "trips_provenance_pending": sum(
                        r["trips_provenance_pending"] for r in rows
                    ),
                    "request_ids_resolved": len(run_types),
                    "request_ids_unresolved": len(request_ids) - len(run_types),
                }

                if not dry_run:
                    payload = [
                        tuple(
                            _value_for(name, row, client, horizon)
                            for name in UPSERT_COLUMNS
                        )
                        for row in rows
                    ]
                    with pconn.cursor() as wcur:
                        wcur.executemany(UPSERT_SQL, payload)
                    summary["rows_upserted"] = len(payload)

                report["clients"].append(summary)

            if dry_run:
                pconn.rollback()
            else:
                pconn.commit()

    return EXIT_OK, report


def _assert_client_column(cur) -> None:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'client_trips'
           AND column_name = %s
        """,
        (CLIENT_MIGRATION_COLUMN,),
    )
    if cur.fetchone() is None:
        raise _refuse(
            "CLIENT_MIGRATION_MISSING",
            f"public.client_trips.{CLIENT_MIGRATION_COLUMN} is absent; apply "
            "client migration 048 before aggregating this client",
        )


def _value_for(
    name: str, row: Dict[str, Any], client: Dict[str, Any], horizon: int,
) -> Any:
    if name == "client_id":
        return client["client_id"]
    if name == "client_code":
        return client["client_code"]
    if name == "recompute_horizon_days":
        return horizon
    if name == "computed_by":
        return COMPUTED_BY
    return row[name]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run-first recompute of workflow_a_control.trip_delivery_lag_daily "
            "from the current client_trips contents. Reads two databases; writes "
            "only the slice relation."
        ),
    )
    parser.add_argument(
        "--client-code",
        help="Restrict to one client. Omit to recompute every enabled trips "
             "client.",
    )
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
    parser.add_argument(
        "--through-date",
        help="Most recent slice date (ISO). Defaults to today in the business "
             "timezone.",
    )
    parser.add_argument(
        "--horizon-days", type=int, default=None,
        help="Override the derived recompute depth. The derived value is the "
             "deepest enabled lookback plus a margin; a SHALLOWER override can "
             "leave a moved trip double-counted across two slices until a "
             "deeper pass runs.",
    )
    parser.add_argument("--business-timezone", default=None)
    parser.add_argument(
        "--execute", action="store_true",
        help="Perform the upsert. Without it the tool reports DRY_RUN only.",
    )
    parser.add_argument(
        "--confirm", help="Must be the literal RECOMPUTE when --execute is used.",
    )
    parser.add_argument("--dsn")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, report = run(args)
    except LagAggregationRefused as exc:
        print(f"LAG_AGGREGATION_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(
            f"LAG_AGGREGATION_FAILED {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(report, sort_keys=True, indent=2, default=str))
    print(report["mode"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
