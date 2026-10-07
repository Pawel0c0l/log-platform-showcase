"""Workflow A — Driver Eco Dashboard V1 snapshot generator (read-only).

Builds one driver's presentation snapshot for one closed reporting period and
returns it as a job result. It reads the existing Eco Driving tables and writes
nothing: publication (R2), capability links and the Cloudflare Worker are a
later milestone and are deliberately absent here.

Params
------
client_id            (required) Workflow A client UUID
identity_key         (required) pipeline-family identity value; server-side only
client_code          optional override for pipeline-family resolution
period_type          "weekly" (default) | "monthly"
period_end_date      weekly only, YYYY-MM-DD exclusive cumulative boundary
month                monthly only, YYYY-MM
output_path          optional local path for the serialised document
include_document     bool, default false — include the document in the result

The job is intentionally not registered in a dataset schedule yet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from api.timezone_utils import (
    get_business_timezone,
    get_business_timezone_name,
    set_pg_session_timezone,
)
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.ecodriving.job_eco_driving_aggregate import (
    _month_bounded_weekly_periods,
    _next_month_start,
    _period_for_cumulative_boundary,
    _previous_month_start,
    resolve_previous_completed_month,
    resolve_previous_completed_weekly_snapshot,
)
from jobs.ecodriving_dashboard import sources
from jobs.ecodriving_dashboard.snapshot_builder import PeriodIdentity
from jobs.ecodriving_dashboard.publication import (
    PrivacyContext,
    build_publishable_snapshot,
    serialize_publishable_snapshot,
)
from jobs.ecodriving_dashboard.snapshot_contract import (
    PERIOD_TYPE_MONTHLY,
    PERIOD_TYPE_WEEKLY,
)


JOB_SOURCE = "jobs.ecodriving_dashboard.job_eco_dashboard_snapshot"
DATASET_NAME = "eco_dashboard_snapshot"

SNAPSHOT_UNAVAILABLE = "SNAPSHOT_UNAVAILABLE"


def _safe_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _dict_row_factory():
    try:
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Missing dependency for Postgres row factory: psycopg") from exc
    return dict_row


def _load_client_account_config(*, client_id: str):
    try:
        from jobs.api.telematics.control_plane import load_client_account_config
    except ImportError as exc:
        raise RuntimeError("Missing dependency for Workflow A control plane: psycopg") from exc
    return load_client_account_config(client_id=client_id)


def _client_business_pg_conn(cfg, *, autocommit: bool = False):
    """One client-business session, with its transaction mode fixed at birth.

    `autocommit` is deliberately a CONSTRUCTION-TIME argument and deliberately
    keyword-only. Configuring the business timezone is a statement, and a
    statement on a non-autocommit psycopg session opens a transaction, after
    which psycopg refuses the mode change outright ("can't change 'autocommit'
    now: connection in transaction status INTRANS"). A caller that opened the
    connection first and flipped the flag afterwards could therefore never
    succeed — so the flag is not offered after the fact.

    The default stays transactional: the Eco jobs, the snapshot reads and the
    mailing send-log accounting all own real transactions and must keep them.
    Only the delivery ledger asks for autocommit, because "durably persisted
    before the remote publisher call" has to be a statement about that call and
    not about some later commit.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Missing dependency for Postgres connection: psycopg") from exc
    dsn = (
        f"host={cfg.client_db_host} "
        f"port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} "
        f"user={cfg.client_db_user} "
        f"password={resolve_secret(cfg.client_db_password_secret_ref)}"
    )
    # Timezone AFTER the mode is already final: on an autocommit session this
    # leaves no open transaction behind, which is exactly what the ledger needs.
    return set_pg_session_timezone(psycopg.connect(dsn, autocommit=autocommit))


def _local_midnight(day: date) -> datetime:
    from datetime import time

    return datetime.combine(day, time.min, tzinfo=get_business_timezone())


def _weekly_identity(period, closed_periods_in_month: int) -> PeriodIdentity:
    return PeriodIdentity(
        period_type=PERIOD_TYPE_WEEKLY,
        period_label=period.period_label,
        period_start_date=period.period_start_date,
        period_end_date_exclusive=period.period_end_date,
        month_start_date=period.month_start_date,
        period_sequence_in_month=period.period_sequence_in_month,
        closed_periods_in_month=closed_periods_in_month,
        is_partial_period=period.is_partial_period,
    )


def _monthly_identity(month_start: date, month_end: date) -> PeriodIdentity:
    return PeriodIdentity(
        period_type=PERIOD_TYPE_MONTHLY,
        period_label=f"{month_start:%Y-%m}",
        period_start_date=month_start,
        period_end_date_exclusive=month_end,
        month_start_date=month_start,
        period_sequence_in_month=None,
        closed_periods_in_month=len(_month_bounded_weekly_periods(month_start)),
        is_partial_period=False,
    )


def _resolve_weekly_identities(params: dict) -> tuple[PeriodIdentity, PeriodIdentity | None]:
    raw = params.get("period_end_date")
    if raw:
        period = _period_for_cumulative_boundary(date.fromisoformat(str(raw).strip()))
    else:
        period = resolve_previous_completed_weekly_snapshot()
    month_periods = _month_bounded_weekly_periods(period.month_start_date)
    current = _weekly_identity(period, len(month_periods))
    previous = None
    if period.period_sequence_in_month > 1:
        previous_period = month_periods[period.period_sequence_in_month - 2]
        previous = _weekly_identity(previous_period, len(month_periods))
    return current, previous


def _resolve_monthly_identities(params: dict) -> tuple[PeriodIdentity, PeriodIdentity | None]:
    raw = params.get("month")
    if raw:
        month_start = datetime.strptime(str(raw).strip(), "%Y-%m").date().replace(day=1)
        month_end = _next_month_start(month_start)
    else:
        month_start, month_end = resolve_previous_completed_month()
    previous_start = _previous_month_start(month_start)
    return (
        _monthly_identity(month_start, month_end),
        _monthly_identity(previous_start, month_start),
    )


def _period_updated_at(stats_row: dict, identity: PeriodIdentity) -> datetime:
    """The freshness of one persisted period, as a STABLE fact about the data.

    `updated_at` is what the Eco aggregation job stamped on the stats row, so
    it moves only when the data moves. The fallback is the period's own closing
    boundary rather than the current time: a row whose freshness column is
    absent must still describe the same instant on every rebuild, because this
    value reaches the canonical snapshot bytes and therefore the payload digest.
    Wall-clock time here would mean the same source data serialised to different
    octets on every run.
    """
    updated_at = stats_row.get("updated_at")
    if updated_at is None:
        return _local_midnight(identity.period_end_date_exclusive).astimezone(timezone.utc)
    if updated_at.tzinfo is None:
        return updated_at.replace(tzinfo=timezone.utc)
    return updated_at


def _snapshot_generated_at(current, previous=None) -> datetime:
    """THE document's `generated_at_utc`, derived from source state only.

    A dashboard snapshot is identified downstream by SHA-256 of its exact
    canonical bytes: the delivery ledger binds the digest to one logical
    delivery and refuses a rerun whose bytes disagree. Stamping the document
    with the current time would therefore make every rebuild of unchanged data
    a payload conflict — a legitimate delayed rerun (recovery, retry, an
    operator re-running the same period) would be refused for having produced
    "different" bytes for identical facts.

    The authoritative alternative is already in hand: the persisted Eco stats
    rows the document is built from carry their own `updated_at`. The newest of
    the periods that actually contributed is what the document was generated
    FROM, it is stable for as long as the data is, and it moves exactly when a
    genuine recalculation moves it — which is also the digest change a rerun
    over changed data is supposed to produce.
    """
    stamps = [current.snapshot_updated_at_utc]
    if previous is not None:
        stamps.append(previous.snapshot_updated_at_utc)
    return max(stamps)


def _load_period(
    cur,
    *,
    family,
    schema: str,
    period_type: str,
    client_id: str,
    identity_key: str,
    identity: PeriodIdentity,
    with_distribution: bool,
    distribution_cache: dict | None = None,
):
    stats_row = sources.fetch_stats_row(
        cur,
        family=family,
        schema=schema,
        period_type=period_type,
        client_id=client_id,
        identity_key=identity_key,
        period_start_date=identity.period_start_date,
        period_end_date=identity.period_end_date_exclusive,
    )
    if stats_row is None:
        return None, None
    distribution = None
    if with_distribution and stats_row.get("ranking_group") == "INCLUDED":
        # A POPULATION statistic: identical for every driver in one client,
        # period type and period. Computing it per driver is the same query
        # repeated once per candidate, which is exactly the shape a fleet run
        # must not have — so a caller processing many drivers passes a cache and
        # pays for it once.
        cache_key = (
            period_type,
            identity.period_start_date,
            identity.period_end_date_exclusive,
        )
        if distribution_cache is not None and cache_key in distribution_cache:
            distribution = distribution_cache[cache_key]
        else:
            distribution = sources.fetch_rating_group_distribution(
                cur,
                family=family,
                schema=schema,
                period_type=period_type,
                client_id=client_id,
                period_start_date=identity.period_start_date,
                period_end_date=identity.period_end_date_exclusive,
            )
            if distribution_cache is not None:
                distribution_cache[cache_key] = distribution
    ranking = sources.ranking_facts_from_stats_row(stats_row, rating_group_distribution=distribution)
    updated_at = _period_updated_at(stats_row, identity)
    period_input = sources.fetch_period_input(
        cur,
        family=family,
        schema=schema,
        client_id=client_id,
        identity_key=identity_key,
        identity=identity,
        start_ts=_local_midnight(identity.period_start_date),
        end_ts=_local_midnight(identity.period_end_date_exclusive),
        ranking=ranking,
        snapshot_updated_at_utc=updated_at,
        persisted_eco_score_total=stats_row.get("eco_driving_score_total"),
    )
    return period_input, stats_row


def resolve_period_identities(period_type: str, params: dict):
    """The closed reporting period this run is about, plus its comparison basis."""
    if period_type == PERIOD_TYPE_WEEKLY:
        return _resolve_weekly_identities(params)
    return _resolve_monthly_identities(params)


@dataclass(frozen=True)
class DeliverySnapshot:
    """One driver's publishable snapshot, plus the facts a publisher needs.

    `payload` is the EXACT canonical byte sequence: the same bytes the digest
    is computed over and the same bytes the publisher uploads. Nothing
    downstream re-serialises the document, because a second serialisation is
    not guaranteed to produce the same octets.
    """

    snapshot: Any
    payload: bytes
    payload_digest: str
    current_identity: PeriodIdentity
    previous_identity: PeriodIdentity | None
    family: Any
    client_code: str
    period_type: str

    @property
    def snapshot_status(self) -> str:
        return self.snapshot.internal["snapshot_status"]

    @property
    def days_materialized(self) -> int:
        entry = self.snapshot.document["periods"][self.period_type]
        return 0 if entry["current"] is None else len(entry["current"]["days"])


def build_delivery_snapshot_from_cursor(
    cur,
    *,
    family,
    schema: str,
    client_id: str,
    identity_key: str,
    period_type: str,
    current_identity: PeriodIdentity,
    previous_identity: PeriodIdentity | None,
    client_code: str,
    privacy: PrivacyContext | None = None,
    distribution_cache: dict | None = None,
) -> DeliverySnapshot | None:
    """The whole snapshot build, over a cursor the CALLER owns.

    WHY THIS EXISTS SEPARATELY. `build_delivery_snapshot` opens a connection,
    which is exactly right for a one-driver invocation and exactly wrong for a
    fleet run: the existing Eco weekly/monthly jobs process every driver of a
    client in one pass, and a connection per candidate would turn one client run
    into hundreds of new sessions against the client business database. Those
    jobs already hold an open connection to that database, so they lend a cursor
    on it and pay for no additional session at all.

    It also takes the reporting period as an ARGUMENT rather than resolving one.
    The Eco job has already selected the authoritative period — the persisted
    W1/W2/W3 cumulative snapshot, or the closed month — and a second resolver
    inside this call would be a second opinion about which period a driver is
    being told about.

    Returns `None` when the requested closed period has no stats row at all.
    """
    current, _stats_row = _load_period(
        cur,
        family=family,
        schema=schema,
        period_type=period_type,
        client_id=str(client_id),
        identity_key=identity_key,
        identity=current_identity,
        with_distribution=True,
        distribution_cache=distribution_cache,
    )
    if current is None:
        return None

    previous = None
    if previous_identity is not None:
        previous, _ = _load_period(
            cur,
            family=family,
            schema=schema,
            period_type=period_type,
            client_id=str(client_id),
            identity_key=identity_key,
            identity=previous_identity,
            with_distribution=False,
        )

    days = sources.fetch_daily_inputs(
        cur,
        family=family,
        schema=schema,
        client_id=str(client_id),
        identity_key=identity_key,
        start_ts=_local_midnight(current_identity.period_start_date),
        end_ts=_local_midnight(current_identity.period_end_date_exclusive),
        business_timezone=get_business_timezone_name(),
    )

    cur.execute(
        sources.weekly_series_sql(family, schema),
        {
            "client_id": str(client_id),
            "identity_key": identity_key,
            "month_start_date": current_identity.month_start_date,
            "period_end_date": current_identity.period_end_date_exclusive,
        },
    )
    series = sources.series_inputs_from_rows([dict(row) for row in cur.fetchall()])
    resolved_code = str(client_code or "")

    # The supported publisher entry point. `PrivacyContext` is mandatory and is
    # the only source of the value-level privacy sweep, so this job cannot
    # produce publishable bytes with the sweep silently disabled. A caller that
    # knows more identifying values — the delivery job knows the recipient's
    # address — declares them by passing its own context.
    snapshot = build_publishable_snapshot(
        privacy=privacy or PrivacyContext(identity_key=identity_key,
                                          client_code=resolved_code),
        # Derived from the persisted source rows, never from the clock: the
        # same logical driver/period rebuilt later must produce byte-identical
        # canonical output and therefore the same payload digest.
        generated_at_utc=_snapshot_generated_at(current, previous),
        period_type=period_type,
        current=current,
        previous=previous,
        days=days,
        series=series,
        internal={
            "client_id": str(client_id),
            "pipeline_family": family.name,
        },
        business_timezone=get_business_timezone_name(),
    )

    # Canonical deterministic bytes, already gated and budget-checked by
    # `build_publishable_snapshot`. The serialiser accepts only the builder's
    # own result type, so no hand-assembled document can reach this line.
    payload = serialize_publishable_snapshot(snapshot)
    return DeliverySnapshot(
        snapshot=snapshot,
        payload=payload,
        payload_digest=snapshot.payload_digest,
        current_identity=current_identity,
        previous_identity=previous_identity,
        family=family,
        client_code=resolved_code,
        period_type=period_type,
    )


def build_delivery_snapshot(
    *,
    client_id: str,
    identity_key: str,
    period_type: str,
    params: dict | None = None,
    client_code: str | None = None,
    privacy: PrivacyContext | None = None,
) -> DeliverySnapshot | None:
    """THE single host path from Eco Driving data to publishable bytes.

    The read-only generator job, the publication/delivery job and the Eco
    mailing integration all end up in `build_delivery_snapshot_from_cursor`, so
    there is exactly one code path that produces the canonical octets a
    publication is identified by. A second path would be a second chance to
    disagree about them.

    This wrapper is the ONE-DRIVER form: it resolves the client account, opens a
    connection of its own and selects the reporting period from `params`. A
    caller processing many drivers uses the cursor form directly.

    Returns `None` when the requested closed period has no stats row at all.
    """
    params = dict(params or {})
    cfg = _load_client_account_config(client_id=str(client_id))
    schema = _safe_ident(cfg.client_db_schema)
    resolved_code = str(client_code or params.get("client_code")
                        or getattr(cfg, "client_code", "") or "")
    family = sources.resolve_pipeline_family(resolved_code)
    current_identity, previous_identity = resolve_period_identities(period_type, params)

    conn = _client_business_pg_conn(cfg)
    try:
        with conn.cursor(row_factory=_dict_row_factory()) as cur:
            return build_delivery_snapshot_from_cursor(
                cur,
                family=family,
                schema=schema,
                client_id=str(client_id),
                identity_key=identity_key,
                period_type=period_type,
                current_identity=current_identity,
                previous_identity=previous_identity,
                client_code=resolved_code,
                privacy=privacy,
            )
    finally:
        conn.close()


def run(client, run_id: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    client_id = params.get("client_id")
    if not client_id:
        raise ValueError("Missing required param: client_id")
    identity_key = str(params.get("identity_key") or "").strip()
    if not identity_key:
        raise ValueError("Missing required param: identity_key")

    period_type = str(params.get("period_type") or PERIOD_TYPE_WEEKLY).strip().lower()
    if period_type not in (PERIOD_TYPE_WEEKLY, PERIOD_TYPE_MONTHLY):
        raise ValueError("period_type must be 'weekly' or 'monthly'")

    include_document = bool(params.get("include_document") or False)
    output_path = params.get("output_path")

    current_identity, _previous_identity = resolve_period_identities(period_type, params)

    result: dict[str, Any] = {
        "job_name": DATASET_NAME,
        "client_id": str(client_id),
        "pipeline_family": None,
        "period_type": period_type,
        "period_label": current_identity.period_label,
        "period_start_date": current_identity.period_start_date.isoformat(),
        "period_end_date_exclusive": current_identity.period_end_date_exclusive.isoformat(),
        "business_timezone": get_business_timezone_name(),
        "snapshot_status": SNAPSHOT_UNAVAILABLE,
        "snapshot_status_reasons": [],
        "days_materialized": 0,
        "wrote_output": False,
        "payload_bytes": 0,
    }

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Eco Dashboard snapshot generation started",
        run_id=run_id,
        context={key: result[key] for key in ("client_id", "period_type", "period_label")},
    )

    built = build_delivery_snapshot(
        client_id=str(client_id),
        identity_key=identity_key,
        period_type=period_type,
        params=params,
    )
    if built is None:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "No Eco Driving stats row for the requested closed period",
            run_id=run_id,
            context={"period_label": current_identity.period_label},
        )
        return result

    snapshot = built.snapshot
    result["pipeline_family"] = built.family.name
    result["snapshot_status"] = snapshot.internal["snapshot_status"]
    result["snapshot_status_reasons"] = snapshot.internal["snapshot_status_reasons"]
    result["days_materialized"] = built.days_materialized

    payload = built.payload
    result["payload_bytes"] = len(payload)
    result["payload_digest"] = built.payload_digest

    if output_path:
        Path(str(output_path)).write_bytes(payload)
        result["wrote_output"] = True

    if include_document:
        result["document"] = snapshot.document

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Eco Dashboard snapshot generated",
        run_id=run_id,
        context={
            "period_label": result["period_label"],
            "snapshot_status": result["snapshot_status"],
            "snapshot_status_reasons": result["snapshot_status_reasons"],
            "days_materialized": result["days_materialized"],
        },
    )
    return result

