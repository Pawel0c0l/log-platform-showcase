from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jobs.reports.postprocess import job_alpha00001_dysponent_id_enrichment as shared


JOB_SOURCE = "jobs.reports.postprocess.job_alpha00001_driver_chart_exact_import"
ALLOWED_CLIENT_CODE = "ALPHA00001"
CHART_SCHEMA = "public"
CHART_TABLE = "eco_drivers_id_chart"
REQUIRED_COLUMNS = {
    "driver_id",
    "driver_name",
    "email",
    "ranking_included",
    "is_active",
}
OPTIONAL_COLUMNS = {"notes", "source", "effective_from", "effective_to"}
SUPPORTED_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class ImportRequest:
    client_code: str
    input_path: Path
    driver_ids: tuple[str, ...]
    dry_run: bool
    expected_insert_count: int | None
    expected_update_count: int | None
    allow_updates: bool
    allow_extra_ids: bool
    allow_identical_duplicates: bool


@dataclass(frozen=True)
class RosterRow:
    row_number: int
    driver_id: str
    driver_name: str
    email: str | None
    ranking_included: bool
    is_active: bool
    metadata: dict[str, str]

    def comparable_payload(self) -> tuple[Any, ...]:
        return (
            self.driver_id,
            self.driver_name,
            self.email,
            self.ranking_included,
            self.is_active,
            tuple(sorted(self.metadata.items())),
        )


@dataclass(frozen=True)
class ParsedRoster:
    input_row_count: int
    valid_rows: tuple[RosterRow, ...]
    rejected_rows: tuple[dict[str, Any], ...]
    identical_duplicates_collapsed: tuple[dict[str, Any], ...]
    columns: tuple[str, ...]


@dataclass(frozen=True)
class ChangePlan:
    inserts: tuple[dict[str, Any], ...]
    updates: tuple[dict[str, Any], ...]
    unchanged_ids: tuple[str, ...]
    protected_existing: tuple[dict[str, Any], ...]


def _parse_request(params: dict[str, Any]) -> ImportRequest:
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    client_code = shared._optional_client_code(params.get("client_code"))
    if not client_code:
        raise ValueError("Missing required param: client_code")
    if client_code != ALLOWED_CLIENT_CODE:
        raise ValueError(
            f"This job is hard-limited to client_code={ALLOWED_CLIENT_CODE}; got {client_code!r}"
        )

    raw_input_path = shared._optional_str(params.get("input_path"))
    if not raw_input_path:
        raise ValueError("Missing required param: input_path")
    input_path = Path(raw_input_path).expanduser().resolve()
    if input_path.suffix.lower() != ".csv":
        raise ValueError("input_path must point to a .csv roster")

    driver_ids = _parse_driver_ids(params.get("driver_ids"))
    dry_run = shared._bool_param(params.get("dry_run", True))
    expected_insert_count = _optional_non_negative_int(
        params.get("expected_insert_count"), "expected_insert_count"
    )
    expected_update_count = _optional_non_negative_int(
        params.get("expected_update_count"), "expected_update_count"
    )
    if not dry_run and (
        expected_insert_count is None or expected_update_count is None
    ):
        raise ValueError(
            "expected_insert_count and expected_update_count are required when "
            "dry_run=false; use the preceding dry-run values"
        )

    return ImportRequest(
        client_code=client_code,
        input_path=input_path,
        driver_ids=driver_ids,
        dry_run=dry_run,
        expected_insert_count=expected_insert_count,
        expected_update_count=expected_update_count,
        allow_updates=shared._bool_param(params.get("allow_updates", False)),
        allow_extra_ids=shared._bool_param(params.get("allow_extra_ids", False)),
        allow_identical_duplicates=shared._bool_param(
            params.get("allow_identical_duplicates", False)
        ),
    )


def _parse_driver_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("driver_ids must be a non-empty list of unique non-empty values")
    parsed: list[str] = []
    seen: set[str] = set()
    for raw_value in value:
        if isinstance(raw_value, bool):
            raise ValueError("driver_ids must contain only non-empty text values")
        driver_id = str(raw_value or "").strip()
        if not driver_id:
            raise ValueError("driver_ids must contain only non-empty text values")
        if driver_id in seen:
            raise ValueError("driver_ids must not contain duplicates")
        seen.add(driver_id)
        parsed.append(driver_id)
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


def _read_roster(
    input_path: Path,
    *,
    allow_identical_duplicates: bool,
) -> ParsedRoster:
    if not input_path.exists() or not input_path.is_file():
        raise ValueError(f"Roster file does not exist or is not a file: {input_path}")

    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if reader.fieldnames is None:
            raise ValueError("Roster CSV has no header row")

        normalized_columns = [str(name or "").strip().lower() for name in reader.fieldnames]
        if len(normalized_columns) != len(set(normalized_columns)):
            raise ValueError("Roster CSV has duplicate column names after normalization")
        missing_columns = sorted(REQUIRED_COLUMNS - set(normalized_columns))
        if missing_columns:
            raise ValueError(
                "Roster CSV is missing required columns: " + ", ".join(missing_columns)
            )
        unexpected_columns = sorted(set(normalized_columns) - SUPPORTED_COLUMNS)
        if unexpected_columns:
            raise ValueError(
                "Roster CSV has unsupported columns: " + ", ".join(unexpected_columns)
            )

        valid_candidates: list[RosterRow] = []
        rejected: list[dict[str, Any]] = []
        input_row_count = 0
        for row_number, raw_row in enumerate(reader, start=2):
            input_row_count += 1
            row = {
                normalized_columns[index]: str(raw_row.get(original_name) or "").strip()
                for index, original_name in enumerate(reader.fieldnames)
            }
            parsed, reasons = _parse_roster_row(row_number, row)
            if reasons:
                rejected.append(
                    {
                        "row_number": row_number,
                        "driver_id": row.get("driver_id") or None,
                        "reasons": reasons,
                    }
                )
            elif parsed is not None:
                valid_candidates.append(parsed)

    grouped: dict[str, list[RosterRow]] = {}
    for row in valid_candidates:
        grouped.setdefault(row.driver_id, []).append(row)

    valid_rows: list[RosterRow] = []
    collapsed: list[dict[str, Any]] = []
    for driver_id, rows in grouped.items():
        if len(rows) == 1:
            valid_rows.append(rows[0])
            continue
        identical = len({row.comparable_payload() for row in rows}) == 1
        if identical and allow_identical_duplicates:
            valid_rows.append(rows[0])
            collapsed.append(
                {
                    "driver_id": driver_id,
                    "kept_row_number": rows[0].row_number,
                    "collapsed_row_numbers": [row.row_number for row in rows[1:]],
                }
            )
            continue
        reason = (
            "DUPLICATE_DRIVER_ID_IDENTICAL_NOT_ALLOWED"
            if identical
            else "DUPLICATE_DRIVER_ID_CONFLICT"
        )
        for row in rows:
            rejected.append(
                {
                    "row_number": row.row_number,
                    "driver_id": driver_id,
                    "reasons": [reason],
                }
            )

    valid_rows.sort(key=lambda row: row.row_number)
    rejected.sort(key=lambda row: row["row_number"])
    return ParsedRoster(
        input_row_count=input_row_count,
        valid_rows=tuple(valid_rows),
        rejected_rows=tuple(rejected),
        identical_duplicates_collapsed=tuple(collapsed),
        columns=tuple(normalized_columns),
    )


def _parse_roster_row(
    row_number: int,
    row: dict[str, str],
) -> tuple[RosterRow | None, list[str]]:
    reasons: list[str] = []
    driver_id = row.get("driver_id", "").strip()
    driver_name = row.get("driver_name", "").strip()
    email = row.get("email", "").strip() or None
    if not driver_id:
        reasons.append("MISSING_DRIVER_ID")
    if not driver_name:
        reasons.append("MISSING_DRIVER_NAME")
    if email and (len(email) > 254 or not EMAIL_RE.fullmatch(email)):
        reasons.append("INVALID_EMAIL")

    ranking_included = _parse_explicit_bool(
        row.get("ranking_included", ""), "ranking_included", reasons
    )
    is_active = _parse_explicit_bool(row.get("is_active", ""), "is_active", reasons)

    effective_from = _parse_optional_date(
        row.get("effective_from", ""), "effective_from", reasons
    )
    effective_to = _parse_optional_date(
        row.get("effective_to", ""), "effective_to", reasons
    )
    if effective_from and effective_to and effective_to < effective_from:
        reasons.append("EFFECTIVE_TO_BEFORE_EFFECTIVE_FROM")

    if reasons:
        return None, reasons

    metadata = {
        key: value
        for key, value in {
            "notes": row.get("notes", "").strip(),
            "source": row.get("source", "").strip(),
            "effective_from": effective_from.isoformat() if effective_from else "",
            "effective_to": effective_to.isoformat() if effective_to else "",
        }.items()
        if value
    }
    return (
        RosterRow(
            row_number=row_number,
            driver_id=driver_id,
            driver_name=driver_name,
            email=email,
            ranking_included=bool(ranking_included),
            is_active=bool(is_active),
            metadata=metadata,
        ),
        [],
    )


def _parse_explicit_bool(value: str, field: str, reasons: list[str]) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    reasons.append(f"INVALID_OR_MISSING_{field.upper()}")
    return None


def _parse_optional_date(
    value: str,
    field: str,
    reasons: list[str],
) -> date | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        reasons.append(f"INVALID_{field.upper()}")
        return None


def run(client, run_id: str, params: dict):
    request = _parse_request(params or {})
    roster = _read_roster(
        request.input_path,
        allow_identical_duplicates=request.allow_identical_duplicates,
    )
    _log(
        client,
        run_id,
        "INFO",
        "ALPHA00001 exact driver chart import started",
        context=_request_context(request, roster),
    )
    if not request.dry_run and roster.rejected_rows:
        _log(
            client,
            run_id,
            "ERROR",
            "ALPHA00001 exact driver chart import rejected invalid roster rows",
            context={
                "input_path": str(request.input_path),
                "rejected_row_count": len(roster.rejected_rows),
                "rejected_rows": list(roster.rejected_rows),
            },
        )
        raise ValueError("Roster contains rejected rows; real import is not allowed")

    with shared._platform_pg_conn() as platform_conn:
        config = shared._load_client_config(platform_conn, request.client_code)
    if not config.client_id:
        raise RuntimeError("Enabled ALPHA00001 client account has no client_id")

    conn = shared._client_business_pg_conn(config)
    try:
        summary = _execute_import(
            conn,
            config=config,
            request=request,
            roster=roster,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _log(
        client,
        run_id,
        "INFO",
        "ALPHA00001 exact driver chart import finished",
        context=summary,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return summary


def _execute_import(
    conn,
    *,
    config: shared.ClientDbConfig,
    request: ImportRequest,
    roster: ParsedRoster,
) -> dict[str, Any]:
    if request.dry_run:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")

    allowlist = set(request.driver_ids)
    valid_by_id = {row.driver_id: row for row in roster.valid_rows}
    extra_input_ids = sorted(set(valid_by_id) - allowlist)
    missing_allowlist_ids = sorted(allowlist - set(valid_by_id))
    eligible_rows = [
        row
        for row in roster.valid_rows
        if row.driver_id in allowlist or request.allow_extra_ids
    ]
    eligible_ids = [row.driver_id for row in eligible_rows]

    with conn.cursor() as cur:
        _validate_chart_schema(cur)
        existing = _fetch_existing_rows(
            cur,
            client_id=str(config.client_id),
            driver_ids=eligible_ids,
            lock_rows=not request.dry_run,
        )
        plan = _plan_changes(
            eligible_rows,
            existing,
            allow_updates=request.allow_updates,
        )
        summary = _build_summary(
            config=config,
            request=request,
            roster=roster,
            plan=plan,
            eligible_ids=eligible_ids,
            missing_allowlist_ids=missing_allowlist_ids,
            extra_input_ids=extra_input_ids,
        )

        if request.dry_run:
            conn.rollback()
            return summary

        insert_count = len(plan.inserts)
        update_count = len(plan.updates)
        if request.expected_insert_count != insert_count:
            raise ValueError(
                "expected_insert_count does not match current preflight: "
                f"expected={request.expected_insert_count}, current={insert_count}"
            )
        if request.expected_update_count != update_count:
            raise ValueError(
                "expected_update_count does not match current preflight: "
                f"expected={request.expected_update_count}, current={update_count}"
            )

        inserted_ids, updated_ids = _apply_changes(
            cur,
            client_id=str(config.client_id),
            plan=plan,
        )
        if len(inserted_ids) != insert_count or len(updated_ids) != update_count:
            raise RuntimeError(
                "driver chart change count changed during execution; transaction rolled back"
            )
        conn.commit()
        return {
            **summary,
            "rows_inserted": len(inserted_ids),
            "rows_updated": len(updated_ids),
            "inserted_driver_ids": inserted_ids,
            "updated_driver_ids": updated_ids,
        }


def _validate_chart_schema(cur) -> None:
    columns = shared._existing_columns(cur, CHART_SCHEMA, CHART_TABLE)
    required = {
        "client_id",
        "driver_id",
        "driver_name",
        "email",
        "ranking_included",
        "is_active",
        "metadata_json",
        "updated_at",
    }
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            f"{CHART_SCHEMA}.{CHART_TABLE} is missing required columns: {', '.join(missing)}"
        )


def _fetch_existing_rows(
    cur,
    *,
    client_id: str,
    driver_ids: list[str],
    lock_rows: bool,
) -> dict[str, dict[str, Any]]:
    if not driver_ids:
        return {}
    lock_sql = "FOR UPDATE" if lock_rows else ""
    cur.execute(
        f"""
        SELECT
            driver_id,
            driver_name,
            email,
            ranking_included,
            is_active,
            metadata_json
        FROM {CHART_SCHEMA}.{CHART_TABLE}
        WHERE client_id = %s
          AND driver_id = ANY(%s)
        {lock_sql}
        """,
        (client_id, driver_ids),
    )
    return {str(row["driver_id"]): dict(row) for row in cur.fetchall()}


def _plan_changes(
    eligible_rows: list[RosterRow],
    existing: dict[str, dict[str, Any]],
    *,
    allow_updates: bool,
) -> ChangePlan:
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    unchanged_ids: list[str] = []
    protected_existing: list[dict[str, Any]] = []

    for row in eligible_rows:
        current = existing.get(row.driver_id)
        current_metadata = dict(current.get("metadata_json") or {}) if current else {}
        desired_metadata = {**current_metadata, **row.metadata}
        desired = {
            "driver_id": row.driver_id,
            "driver_name": row.driver_name,
            "email": row.email,
            "ranking_included": row.ranking_included,
            "is_active": row.is_active,
            "metadata_json": desired_metadata,
        }
        if current is None:
            inserts.append(desired)
            continue

        changed_fields = [
            field
            for field in (
                "driver_name",
                "email",
                "ranking_included",
                "is_active",
                "metadata_json",
            )
            if current.get(field) != desired[field]
        ]
        if not changed_fields:
            unchanged_ids.append(row.driver_id)
        elif allow_updates:
            updates.append({**desired, "changed_fields": changed_fields})
        else:
            protected_existing.append(
                {"driver_id": row.driver_id, "changed_fields": changed_fields}
            )

    return ChangePlan(
        inserts=tuple(inserts),
        updates=tuple(updates),
        unchanged_ids=tuple(unchanged_ids),
        protected_existing=tuple(protected_existing),
    )


def _apply_changes(
    cur,
    *,
    client_id: str,
    plan: ChangePlan,
) -> tuple[list[str], list[str]]:
    inserted_ids: list[str] = []
    for row in plan.inserts:
        cur.execute(
            f"""
            INSERT INTO {CHART_SCHEMA}.{CHART_TABLE} (
                client_id,
                driver_id,
                driver_name,
                email,
                ranking_included,
                is_active,
                metadata_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (client_id, driver_id) DO NOTHING
            RETURNING driver_id
            """,
            (
                client_id,
                row["driver_id"],
                row["driver_name"],
                row["email"],
                row["ranking_included"],
                row["is_active"],
                json.dumps(row["metadata_json"], ensure_ascii=False, sort_keys=True),
            ),
        )
        inserted = cur.fetchone()
        if inserted is not None:
            inserted_ids.append(str(inserted["driver_id"]))

    updated_ids: list[str] = []
    for row in plan.updates:
        cur.execute(
            f"""
            UPDATE {CHART_SCHEMA}.{CHART_TABLE}
            SET
                driver_name = %s,
                email = %s,
                ranking_included = %s,
                is_active = %s,
                metadata_json = %s::jsonb,
                updated_at = now()
            WHERE client_id = %s
              AND driver_id = %s
            RETURNING driver_id
            """,
            (
                row["driver_name"],
                row["email"],
                row["ranking_included"],
                row["is_active"],
                json.dumps(row["metadata_json"], ensure_ascii=False, sort_keys=True),
                client_id,
                row["driver_id"],
            ),
        )
        updated = cur.fetchone()
        if updated is not None:
            updated_ids.append(str(updated["driver_id"]))
    return inserted_ids, updated_ids


def _build_summary(
    *,
    config: shared.ClientDbConfig,
    request: ImportRequest,
    roster: ParsedRoster,
    plan: ChangePlan,
    eligible_ids: list[str],
    missing_allowlist_ids: list[str],
    extra_input_ids: list[str],
) -> dict[str, Any]:
    eligible_by_id = {
        row.driver_id: row for row in roster.valid_rows if row.driver_id in eligible_ids
    }
    return {
        "job_name": JOB_SOURCE,
        "client_resolved": {
            "client_code": config.client_code,
            "client_id": config.client_id,
            "client_db_name": config.client_db_name,
        },
        "input_path": str(request.input_path),
        "input_filename": request.input_path.name,
        "input_columns": list(roster.columns),
        "input_row_count": roster.input_row_count,
        "valid_row_count": len(roster.valid_rows),
        "rejected_row_count": len(roster.rejected_rows),
        "rejected_rows": list(roster.rejected_rows),
        "identical_duplicates_collapsed": list(roster.identical_duplicates_collapsed),
        "allowlist_driver_ids": list(request.driver_ids),
        "missing_allowlist_ids": missing_allowlist_ids,
        "extra_input_ids": extra_input_ids,
        "extra_ids_skipped": [] if request.allow_extra_ids else extra_input_ids,
        "eligible_driver_ids": eligible_ids,
        "proposed_insert_count": len(plan.inserts),
        "proposed_insert_driver_ids": [row["driver_id"] for row in plan.inserts],
        "proposed_update_count": len(plan.updates),
        "proposed_updates": [
            {"driver_id": row["driver_id"], "changed_fields": row["changed_fields"]}
            for row in plan.updates
        ],
        "skipped_existing_unchanged_count": len(plan.unchanged_ids),
        "skipped_existing_unchanged_ids": list(plan.unchanged_ids),
        "skipped_existing_protected_count": len(plan.protected_existing),
        "skipped_existing_protected": list(plan.protected_existing),
        "existing_chart_row_would_change": bool(
            plan.updates or plan.protected_existing
        ),
        "missing_email_driver_ids": sorted(
            driver_id
            for driver_id, row in eligible_by_id.items()
            if row.email is None
        ),
        "missing_ranking_included_driver_ids": [],
        "missing_is_active_driver_ids": [],
        "allow_updates": request.allow_updates,
        "allow_extra_ids": request.allow_extra_ids,
        "dry_run": request.dry_run,
        "rows_inserted": 0,
        "rows_updated": 0,
        "inserted_driver_ids": [],
        "updated_driver_ids": [],
    }


def _request_context(
    request: ImportRequest,
    roster: ParsedRoster,
) -> dict[str, Any]:
    return {
        "client_code": request.client_code,
        "input_path": str(request.input_path),
        "input_row_count": roster.input_row_count,
        "valid_row_count": len(roster.valid_rows),
        "rejected_row_count": len(roster.rejected_rows),
        "driver_ids": list(request.driver_ids),
        "dry_run": request.dry_run,
        "allow_updates": request.allow_updates,
        "allow_extra_ids": request.allow_extra_ids,
        "allow_identical_duplicates": request.allow_identical_duplicates,
        "expected_insert_count": request.expected_insert_count,
        "expected_update_count": request.expected_update_count,
    }


def _log(
    client,
    run_id: str,
    level: str,
    message: str,
    *,
    context: dict[str, Any],
) -> None:
    if client is None:
        return
    client.log(
        level,
        "SCRIPT",
        JOB_SOURCE,
        message,
        run_id=run_id,
        context=context,
    )
