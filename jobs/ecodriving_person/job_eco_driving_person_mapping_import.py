"""Workflow A — validated physical-person identity CSV import job."""

from __future__ import annotations

import json
import re
from pathlib import Path

from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.ecodriving_person.physical_person_import import (
    DEFAULT_DELIMITER,
    DEFAULT_ENCODING,
    apply_rows,
    classify_changes,
    inspect_and_validate_csv,
    load_existing_people,
)

JOB_SOURCE = "jobs.ecodriving_person.job_eco_driving_person_mapping_import"
DATASET_NAME = "eco_person_driving_mapping_import"
SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


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


def _safe_ident(name: str) -> str:
    if not SAFE_IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _client_business_pg_conn(cfg):
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={cfg.client_db_host} "
        f"port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} "
        f"user={cfg.client_db_user} "
        f"password={resolve_secret(cfg.client_db_password_secret_ref)}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _as_bool(params: dict, key: str, default: bool) -> bool:
    if key not in params:
        return default
    value = params[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean")


def _trim(value: object) -> str:
    return str(value or "").strip()


def run(client, run_id: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    client_id = _trim(params.get("client_id"))
    csv_path = _trim(params.get("csv_path"))
    if not client_id:
        raise ValueError("Missing required param: client_id")
    if not csv_path:
        raise ValueError("Missing required param: csv_path")
    dry_run = _as_bool(params, "dry_run", True)
    apply_changes = _as_bool(params, "apply", False)
    if apply_changes and dry_run:
        raise ValueError("Use dry_run=false together with apply=true to write mapping changes")
    if not dry_run and not apply_changes:
        raise ValueError("dry_run=false requires apply=true")

    cfg = _load_client_account_config(client_id=client_id)
    schema = _safe_ident(cfg.client_db_schema)
    people_table = f"{schema}.eco_person_people"
    encoding = _trim(params.get("encoding")) or DEFAULT_ENCODING
    delimiter = str(params.get("delimiter") or DEFAULT_DELIMITER)
    if len(delimiter) != 1:
        raise ValueError("delimiter must be exactly one character")

    prepared, file_report = inspect_and_validate_csv(
        Path(csv_path),
        expected_client_id=client_id,
        encoding=encoding,
        delimiter=delimiter,
    )
    summary = {
        "job_name": DATASET_NAME,
        "client_id": client_id,
        "client_code": cfg.client_code,
        "dry_run": dry_run,
        "apply": apply_changes,
        "csv_path": str(Path(csv_path).expanduser().resolve()),
        **file_report,
    }
    summary["blocking_error_count"] = len(file_report["blocking_errors"])
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Starting Eco Driving Person mapping import",
        run_id=run_id,
        context=summary,
    )

    if summary["blocking_error_count"]:
        if apply_changes:
            raise ValueError(
                "Eco Driving Person import blocked by validation errors: "
                + json.dumps(file_report["blocking_errors"], ensure_ascii=False)
            )
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Eco Driving Person import dry-run found blocking errors",
            run_id=run_id,
            context=summary,
        )
        return summary

    conn = _client_business_pg_conn(cfg)
    try:
        with conn.cursor(row_factory=_dict_row_factory()) as cur:
            existing = load_existing_people(
                cur,
                people_table=people_table,
                client_id=client_id,
            )
            summary.update(classify_changes(prepared, existing))
            if apply_changes:
                apply_rows(
                    cur,
                    people_table=people_table,
                    client_id=client_id,
                    rows=prepared,
                )
                conn.commit()
            else:
                conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Eco Driving Person mapping import complete",
        run_id=run_id,
        context=summary,
    )
    return summary
