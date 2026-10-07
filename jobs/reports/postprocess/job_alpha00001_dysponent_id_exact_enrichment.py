from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jobs.reports.postprocess import job_alpha00001_dysponent_id_enrichment as shared


JOB_SOURCE = "jobs.reports.postprocess.job_alpha00001_dysponent_id_exact_enrichment"


@dataclass(frozen=True)
class ExactEnrichmentRequest:
    client_code: str
    client_id: str | None
    provider_trip_ids: tuple[int, ...]
    dry_run: bool
    expected_update_count: int | None
    strict: bool


def _parse_request(params: dict[str, Any]) -> ExactEnrichmentRequest:
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    client_code = shared._optional_client_code(params.get("client_code")) or shared.ALLOWED_CLIENT_CODE
    shared._validate_client_code_allowed(client_code)
    provider_trip_ids = _parse_provider_trip_ids(params.get("provider_trip_ids"))
    dry_run = shared._bool_param(params.get("dry_run", True))
    strict = shared._bool_param(params.get("strict", True))
    expected_update_count = _optional_non_negative_int(
        params.get("expected_update_count"), "expected_update_count"
    )
    if not dry_run and expected_update_count is None:
        raise ValueError(
            "expected_update_count is required when dry_run=false; use the preceding dry-run value"
        )
    return ExactEnrichmentRequest(
        client_code=client_code,
        client_id=shared._optional_str(params.get("client_id")),
        provider_trip_ids=provider_trip_ids,
        dry_run=dry_run,
        expected_update_count=expected_update_count,
        strict=strict,
    )


def _parse_provider_trip_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("provider_trip_ids must be a non-empty list of positive integers")
    parsed: list[int] = []
    seen: set[int] = set()
    for raw_value in value:
        if isinstance(raw_value, bool):
            raise ValueError("provider_trip_ids must contain only positive integers")
        try:
            provider_trip_id = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider_trip_ids must contain only positive integers") from exc
        if provider_trip_id <= 0:
            raise ValueError("provider_trip_ids must contain only positive integers")
        if provider_trip_id in seen:
            raise ValueError("provider_trip_ids must not contain duplicates")
        seen.add(provider_trip_id)
        parsed.append(provider_trip_id)
    return tuple(parsed)


def _optional_non_negative_int(value: Any, name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def run(client, run_id: str, params: dict):
    request = _parse_request(params or {})
    _log(
        client,
        run_id,
        "INFO",
        "ALPHA00001 exact Dysponent_ID enrichment started",
        context=_request_context(request),
    )
    with shared._platform_pg_conn() as platform_conn:
        config = shared._load_client_config(platform_conn, request.client_code)
    if request.client_id and request.client_id != config.client_id:
        raise ValueError("client_id does not match the enabled ALPHA00001 client account")

    conn = shared._client_business_pg_conn(config)
    try:
        with conn.cursor() as cur:
            source_columns = shared._existing_columns(
                cur, shared.ASSIGNMENT_SCHEMA, shared.ASSIGNMENT_TABLE
            )
            trip_columns = shared._existing_columns(
                cur, shared.CLIENT_TRIPS_SCHEMA, shared.CLIENT_TRIPS_TABLE
            )
            shared._validate_required_schema(source_columns, trip_columns)
        conn.rollback()

        if request.dry_run:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")

        with conn.cursor() as cur:
            analysis, proposals = _analyze_exact_scope(
                cur,
                config=config,
                request=request,
                has_imported_at="imported_at" in source_columns,
            )
            if request.dry_run:
                conn.rollback()
                summary = {**analysis, "rows_updated": 0, "updated_provider_trip_ids": []}
            else:
                would_update = int(analysis["would_update_count"])
                if request.expected_update_count != would_update:
                    raise ValueError(
                        "expected_update_count does not match current preflight: "
                        f"expected={request.expected_update_count}, current={would_update}"
                    )
                updated_ids = _apply_exact_updates(
                    cur,
                    client_id=str(config.client_id),
                    proposals=proposals,
                )
                if len(updated_ids) != would_update:
                    raise RuntimeError(
                        "exact update count changed during execution; transaction rolled back"
                    )
                conn.commit()
                summary = {
                    **analysis,
                    "rows_updated": len(updated_ids),
                    "updated_provider_trip_ids": updated_ids,
                }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _log(
        client,
        run_id,
        "INFO",
        "ALPHA00001 exact Dysponent_ID enrichment finished",
        context=summary,
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


def _analyze_exact_scope(
    cur,
    *,
    config: shared.ClientDbConfig,
    request: ExactEnrichmentRequest,
    has_imported_at: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cur.execute(
        f"""
        SELECT
            client_id,
            client_code,
            provider_trip_id,
            registration,
            start_timestamp,
            end_timestamp,
            {_quote(shared.CLIENT_TRIPS_TARGET_COLUMN)} AS current_dysponent_id
        FROM {_qualified(shared.CLIENT_TRIPS_SCHEMA, shared.CLIENT_TRIPS_TABLE)}
        WHERE client_id = %s
          AND provider_trip_id = ANY(%s)
        ORDER BY provider_trip_id
        """,
        (config.client_id, list(request.provider_trip_ids)),
    )
    trips_by_id = {int(row["provider_trip_id"]): row for row in cur.fetchall()}
    proposed_updates: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []

    for provider_trip_id in request.provider_trip_ids:
        trip = trips_by_id.get(provider_trip_id)
        if trip is None:
            skipped_rows.append(
                {"provider_trip_id": provider_trip_id, "reason": "TARGET_TRIP_NOT_FOUND"}
            )
            continue
        current_dysponent_id = str(trip.get("current_dysponent_id") or "").strip()
        if current_dysponent_id:
            skipped_rows.append(
                {
                    "provider_trip_id": provider_trip_id,
                    "registration": trip["registration"],
                    "reason": "ALREADY_ASSIGNED",
                    "current_dysponent_id": current_dysponent_id,
                }
            )
            continue

        history_rows, next_assignment_date = _load_latest_history(
            cur,
            registration=str(trip["registration"] or ""),
            trip_start_ts=trip["start_timestamp"],
            has_imported_at=has_imported_at,
        )
        selected, distinct_ids, selection_error = _select_history_row(
            history_rows, strict=request.strict
        )
        if selection_error == "NO_ASSIGNMENT_HISTORY":
            skipped_rows.append(
                {
                    "provider_trip_id": provider_trip_id,
                    "registration": trip["registration"],
                    "reason": selection_error,
                }
            )
            continue
        if selection_error == "AMBIGUOUS_ASSIGNMENT_HISTORY":
            skipped_rows.append(
                {
                    "provider_trip_id": provider_trip_id,
                    "registration": trip["registration"],
                    "reason": selection_error,
                    "competing_assignment_ids": distinct_ids,
                    "assignment_date": history_rows[0]["assignment_date"].isoformat(),
                }
            )
            continue

        proposal = {
            "provider_trip_id": provider_trip_id,
            "registration": str(trip["registration"]),
            "trip_start_timestamp": trip["start_timestamp"].isoformat(),
            "trip_end_timestamp": (
                trip["end_timestamp"].isoformat() if trip["end_timestamp"] else None
            ),
            "current_dysponent_id": None,
            "proposed_dysponent_id": str(selected["source_id"]).strip(),
            "assignment_date": selected["assignment_date"].isoformat(),
            "next_assignment_date": (
                next_assignment_date.isoformat() if next_assignment_date else None
            ),
            "latest_history_rows": len(history_rows),
            "distinct_assignment_ids": distinct_ids,
            "history_registration": str(selected["registration"]),
            "history_imported_at": (
                selected["imported_at"].isoformat() if selected.get("imported_at") else None
            ),
        }
        proposed_updates.append(proposal)
        proposals.append(
            {
                "provider_trip_id": provider_trip_id,
                "dysponent_id": proposal["proposed_dysponent_id"],
            }
        )

    summary = {
        **_request_context(request),
        "client_db_name": config.client_db_name,
        "resolved_client_id": config.client_id,
        "requested_count": len(request.provider_trip_ids),
        "candidates_found": len(trips_by_id),
        "would_update_count": len(proposals),
        "proposed_updates": proposed_updates,
        "skipped_count": len(skipped_rows),
        "skipped_rows": skipped_rows,
        "all_requested_safe_to_update": len(proposals) == len(request.provider_trip_ids),
        "counter_columns_touched": [],
        "target_column": shared.CLIENT_TRIPS_TARGET_COLUMN,
        "status": "OK" if not skipped_rows else "WARNING",
    }
    return summary, proposals



def _select_history_row(
    history_rows: list[dict[str, Any]],
    *,
    strict: bool,
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    if not history_rows:
        return None, [], "NO_ASSIGNMENT_HISTORY"
    distinct_ids = sorted({str(row["source_id"]).strip() for row in history_rows})
    if len(distinct_ids) != 1:
        return None, distinct_ids, "AMBIGUOUS_ASSIGNMENT_HISTORY"
    return history_rows[0], distinct_ids, None


def _load_latest_history(
    cur,
    *,
    registration: str,
    trip_start_ts: Any,
    has_imported_at: bool,
) -> tuple[list[dict[str, Any]], Any]:
    imported_select = "log.imported_at" if has_imported_at else "NULL::timestamptz AS imported_at"
    imported_order = "imported_at DESC NULLS LAST," if has_imported_at else ""
    registration_expr = shared._registration_sql_expr("log.registration")
    cur.execute(
        f"""
        WITH eligible AS (
            SELECT
                btrim(log.source_id) AS source_id,
                log.registration,
                log.assignment_date,
                {imported_select}
            FROM {_qualified(shared.ASSIGNMENT_SCHEMA, shared.ASSIGNMENT_TABLE)} AS log
            WHERE {registration_expr} = %s
              AND log.source_id IS NOT NULL
              AND btrim(log.source_id) <> ''
              AND log.assignment_date <= %s::date
        ),
        latest AS (
            SELECT max(assignment_date) AS assignment_date
            FROM eligible
        )
        SELECT source_id, registration, assignment_date, imported_at
        FROM eligible
        WHERE assignment_date = (SELECT assignment_date FROM latest)
        ORDER BY {imported_order} source_id ASC
        """,
        (shared._normalize_registration(registration), trip_start_ts),
    )
    rows = list(cur.fetchall())
    cur.execute(
        f"""
        SELECT min(log.assignment_date) AS next_assignment_date
        FROM {_qualified(shared.ASSIGNMENT_SCHEMA, shared.ASSIGNMENT_TABLE)} AS log
        WHERE {registration_expr} = %s
          AND log.source_id IS NOT NULL
          AND btrim(log.source_id) <> ''
          AND log.assignment_date > %s::date
        """,
        (shared._normalize_registration(registration), trip_start_ts),
    )
    next_row = cur.fetchone() or {}
    return rows, next_row.get("next_assignment_date")


def _apply_exact_updates(
    cur,
    *,
    client_id: str,
    proposals: list[dict[str, Any]],
) -> list[int]:
    updated_ids: list[int] = []
    for proposal in proposals:
        cur.execute(
            f"""
            UPDATE {_qualified(shared.CLIENT_TRIPS_SCHEMA, shared.CLIENT_TRIPS_TABLE)}
               SET {_quote(shared.CLIENT_TRIPS_TARGET_COLUMN)} = %s
             WHERE client_id = %s
               AND provider_trip_id = %s
               AND (
                    {_quote(shared.CLIENT_TRIPS_TARGET_COLUMN)} IS NULL
                    OR btrim({_quote(shared.CLIENT_TRIPS_TARGET_COLUMN)}) = ''
               )
            RETURNING provider_trip_id
            """,
            (
                proposal["dysponent_id"],
                client_id,
                proposal["provider_trip_id"],
            ),
        )
        row = cur.fetchone()
        if row:
            updated_ids.append(int(row["provider_trip_id"]))
    return updated_ids


def _request_context(request: ExactEnrichmentRequest) -> dict[str, Any]:
    return {
        "client_code": request.client_code,
        "client_id": request.client_id,
        "provider_trip_ids": list(request.provider_trip_ids),
        "dry_run": request.dry_run,
        "expected_update_count": request.expected_update_count,
        "strict": request.strict,
    }



def _log(
    client,
    run_id: str,
    level: str,
    message: str,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    if not client or not hasattr(client, "log"):
        return
    client.log(
        level,
        "SCRIPT",
        JOB_SOURCE,
        message,
        run_id=run_id,
        context=context or {},
    )


def _quote(identifier: str) -> str:
    return shared._qi(identifier)


def _qualified(schema_name: str, table_name: str) -> str:
    return shared._qualified_ident(schema_name, table_name)
