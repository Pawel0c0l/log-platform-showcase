#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import _convert_to_canonical_csv
from jobs.common import environment_identity
from jobs.reports.stage2 import io as stage2_io
from jobs.reports.stage2.detector import detect_report_type
from jobs.reports.stage2.registry import get_report_cls
from jobs.reports.stage2.validation import validate
from jobs.reports.stage2 import job_stage2
from jobs.reports.stage3 import job_stage3

LOCAL_REPLAY_ALLOW_ENV = "WORKFLOW_B_LOCAL_REPLAY_ALLOW"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _ReplayLogCollector:
    def __init__(self) -> None:
        self.logs: list[dict[str, Any]] = []

    def log(self, level, kind, source, message, *, run_id=None, context=None, error=None):
        self.logs.append(
            {
                "level": level,
                "kind": kind,
                "source": source,
                "message": message,
                "run_id": run_id,
                "context": context or {},
                "error": error,
            }
        )


def _load_dotenv_if_present() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(env_path)


def _json_default(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _file_ext(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in {".csv", ".xls", ".xlsx", ".xlsm"}:
        raise RuntimeError(f"Unsupported Workflow B file extension: {ext}")
    return ext


def _source_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_client_config(platform_conn, client_code: str) -> job_stage3.ClientDbConfig:
    with platform_conn.cursor() as cur:
        return job_stage3._load_client_account_by_code(cur, client_code)


def _verified_local_dev_client_guard(
    client_config: job_stage3.ClientDbConfig,
    *,
    runtime_identity: environment_identity.RuntimeIdentity,
    platform_attestation: environment_identity.AttestedDatabaseIdentity,
) -> dict[str, Any]:
    failures: list[str] = []
    if os.getenv(LOCAL_REPLAY_ALLOW_ENV) != "1":
        failures.append(f"{LOCAL_REPLAY_ALLOW_ENV}=1 is required for --load-stage3")
    postgres_host = os.getenv("POSTGRES_HOST", "")
    log_api_url = os.getenv("LOG_API_URL", "")
    if postgres_host not in LOCAL_HOSTS:
        failures.append("POSTGRES_HOST must use a local address for --load-stage3")
    if client_config.client_db_host not in LOCAL_HOSTS:
        failures.append("client_db_host must use a local address for --load-stage3")
    if log_api_url and not any(token in log_api_url for token in ("127.0.0.1", "localhost")):
        failures.append("LOG_API_URL must use a local address for --load-stage3")
    if failures:
        raise RuntimeError("Local/dev replay guard failed: " + "; ".join(failures))

    expectation = environment_identity.ClientIdentityExpectation(
        client_code=client_config.client_code,
        environment=client_config.client_db_environment,
        database_identity_id=client_config.client_db_identity_id,
        database_name=client_config.client_db_name,
        database_user=client_config.client_db_user,
    )
    with job_stage3._client_business_pg_conn(client_config) as client_conn:
        client_attestation = environment_identity.attest_client_identity(
            client_conn, runtime_identity, expectation
        )
    return {
        "ok": True,
        "target_environment": runtime_identity.environment,
        "platform": platform_attestation.context(),
        "client": client_attestation.context(),
    }


def normalize_detect_clean(
    source_path: Path,
    *,
    expected_report_type: str | None = None,
    platform_conn=None,
) -> dict[str, Any]:
    source_path = source_path.expanduser().resolve()
    if not source_path.exists():
        raise RuntimeError(f"File does not exist: {source_path}")
    ext = _file_ext(source_path)
    source_sha = _source_sha256(source_path)

    with tempfile.TemporaryDirectory(prefix="workflow-b-single-file-replay-") as tmpdir:
        tmp = Path(tmpdir)
        normalized_path = tmp / f"{source_path.stem}__normalized.csv"
        stage1_meta = _convert_to_canonical_csv(source_path.read_bytes(), ext, normalized_path)
        raw_df = stage2_io.read_csv_loose(str(normalized_path))
        tables = stage2_io.split_into_tables(raw_df)
        detection = detect_report_type(tables)
        if detection.pending_reason:
            raise RuntimeError(
                "Stage 2 detection did not reach threshold: "
                f"candidate={detection.report_type!r}, score={detection.detect_score}, "
                f"reason={detection.pending_reason}"
            )
        if expected_report_type and detection.report_type != expected_report_type:
            raise RuntimeError(
                f"Expected report_type={expected_report_type!r}, detected {detection.report_type!r}"
            )
        report_cls = get_report_cls(detection.report_type or "")
        if report_cls is None:
            raise RuntimeError(f"Unregistered report type: {detection.report_type}")
        cleaned = report_cls.clean(tables if getattr(report_cls, "MULTI_TABLE", False) else tables[0])
        validation = validate(cleaned, report_cls)
        if not validation.is_valid:
            raise RuntimeError(
                "Stage 2 validation failed: "
                + json.dumps(
                    {
                        "errors": validation.errors,
                        "warnings": validation.warnings,
                        "schema_diff": validation.schema_diff,
                    },
                    ensure_ascii=False,
                    default=_json_default,
                )
            )

        record_id_config = {"id_sync_column_name": None, "record_id_ingredients": None}
        if platform_conn is not None:
            with platform_conn.cursor() as cur:
                record_id_config = job_stage2._load_stage2_registry_config(
                    cur,
                    report_type=detection.report_type,
                )
        log_collector = _ReplayLogCollector()
        final_df = job_stage2._add_record_id_column(
            cleaned.df,
            record_id_ingredients=record_id_config.get("record_id_ingredients"),
            client=log_collector,
            run_id="local-workflow-b-single-file-replay-stage2",
            context={"report_type": detection.report_type, "source_file": str(source_path)},
        )
        cleaned_path = tmp / f"{source_path.stem}__{detection.report_type}__cleaned.csv"
        final_df.to_csv(cleaned_path, sep=";", index=False, encoding="utf-8-sig")
        final_df_for_stage3 = job_stage3._read_cleaned_csv(cleaned_path)

    return {
        "source_path": str(source_path),
        "source_sha256": source_sha,
        "normalized_shape": [int(raw_df.shape[0]), int(raw_df.shape[1])],
        "table_count": len(tables),
        "stage1_date_columns": sorted(str(k) for k in (stage1_meta or {}).keys()),
        "detected_report_type": detection.report_type,
        "detect_score": detection.detect_score,
        "candidates_top3": detection.candidates_top3,
        "cleaned_rows": int(len(cleaned.df)),
        "cleaned_columns": list(cleaned.df.columns),
        "clean_metadata": cleaned.metadata,
        "validation_warnings": validation.warnings,
        "validation_schema_diff": validation.schema_diff,
        "record_id_ingredients": record_id_config.get("record_id_ingredients"),
        "record_id_non_empty_rows": int((final_df_for_stage3["record_id"].astype(str).str.strip() != "").sum())
        if "record_id" in list(final_df_for_stage3.columns)
        else 0,
        "stage2_logs": log_collector.logs,
        "dataframe": final_df_for_stage3,
    }


def _build_stage3_plan(platform_conn, client_config: job_stage3.ClientDbConfig, report_type: str, df) -> dict[str, Any]:
    with platform_conn.cursor() as cur:
        data_overwrite, policy_found = job_stage3._load_data_overwrite_policy(
            cur,
            client_code=client_config.client_code,
            report_type=report_type,
        )
    destination_schema, destination_table = job_stage3._destination_for_report_type(report_type)
    with job_stage3._client_business_pg_conn(client_config) as destination_conn:
        destination_conn.rollback()
        with destination_conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            plan = job_stage3._build_load_plan(
                cur,
                df=df.copy(),
                data_overwrite=data_overwrite,
                destination_schema=destination_schema,
                destination_table=destination_table,
            )
            destination_conn.rollback()
    return {
        "data_overwrite": data_overwrite,
        "data_overwrite_policy_found": policy_found,
        "destination_schema": destination_schema,
        "destination_table": destination_table,
        "destination_database": client_config.client_db_name,
        "input_rows": plan["input_rows"],
        "has_record_id": plan["original_has_record_id"],
        "usable_record_id": plan["usable_record_id"],
        "non_empty_record_id_rows": plan["non_empty_record_id_rows"],
        "empty_record_id_rows": plan["empty_record_id_rows"],
        "duplicate_record_id_rows_in_input": plan["duplicate_record_id_rows_in_input"],
        "destination_schema_exists": plan["destination_schema_exists"],
        "destination_table_exists": plan["destination_table_exists"],
        "columns_to_create": plan["columns_to_create"],
        "columns_to_add": plan["columns_to_add"],
        "unique_index_exists": plan["unique_index_exists"],
        "duplicate_existing_record_ids_detected": plan["duplicate_existing_record_ids_detected"],
        "duplicate_existing_record_ids": plan["duplicate_existing_record_ids"],
        "would_insert_rows": plan["would_insert_rows"],
        "would_update_rows": plan["would_update_rows"],
        "would_skip_rows": plan["would_skip_rows"],
        "would_reject_rows": plan["would_reject_rows"],
        "would_replace_table": plan["would_replace_table"],
        "warnings": plan["warnings"],
        "errors": plan["errors"],
        "dry_run_status": "ERROR" if plan["errors"] else ("WARNING" if plan["warnings"] else "OK"),
    }


def _run_stage3_load(
    client_config: job_stage3.ClientDbConfig,
    *,
    report_type: str,
    source_path: str,
    source_sha256: str,
    df,
) -> dict[str, Any]:
    raw_file_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"workflow-b-local-replay:{client_config.client_code}:{source_sha256}:{report_type}"))
    source_artifact_id = f"local-replay:{source_sha256[:16]}"
    run_id = "local-workflow-b-single-file-replay-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with job_stage3._client_business_pg_conn(client_config) as destination_conn:
        result = job_stage3._load_dataframe_to_destination(
            destination_conn,
            raw_file_id=raw_file_id,
            run_id=run_id,
            client_code=client_config.client_code,
            report_type=report_type,
            source_artifact_id=source_artifact_id,
            source_filename=Path(source_path).name,
            data_overwrite=False,
            df=df.copy(),
            source_sha256=source_sha256,
            raw_artifact_id=source_artifact_id,
            normalized_artifact_id=None,
            cleaned_artifact_id=source_artifact_id,
        )
        result.destination_database = client_config.client_db_name
    return job_stage3._result_context(result)


def replay_file(
    *,
    client_code: str,
    file_path: str,
    expected_report_type: str | None,
    load_stage3: bool,
) -> dict[str, Any]:
    _load_dotenv_if_present()
    source_path = Path(file_path)
    with job_stage3._platform_pg_conn() as platform_conn:
        if load_stage3:
            runtime_identity = environment_identity.load_runtime_identity()
            environment_identity.require_local_dev(
                runtime_identity, operation_name="workflow_b_stage3_replay"
            )
            platform_attestation = environment_identity.attest_platform_identity(
                platform_conn, runtime_identity
            )
            client_config = _load_client_config(platform_conn, client_code)
            guard = _verified_local_dev_client_guard(
                client_config,
                runtime_identity=runtime_identity,
                platform_attestation=platform_attestation,
            )
        else:
            client_config = _load_client_config(platform_conn, client_code)
            guard = {"required": False, "verified": False, "scope": "load_stage3_only"}

        stage2 = normalize_detect_clean(
            source_path,
            expected_report_type=expected_report_type,
            platform_conn=platform_conn,
        )
        df = stage2.pop("dataframe")
        plan = _build_stage3_plan(platform_conn, client_config, stage2["detected_report_type"], df)
        result = {
            "client_code": client_code,
            "mode": "load_stage3" if load_stage3 else "dry_run",
            "environment_identity_guard": guard,
            "stage2": stage2,
            "stage3_plan": plan,
            "stage3_load": None,
        }
        if load_stage3:
            if plan["errors"]:
                raise RuntimeError("Stage 3 load plan has errors: " + "; ".join(plan["errors"]))
            result["stage3_load"] = _run_stage3_load(
                client_config,
                report_type=stage2["detected_report_type"],
                source_path=stage2["source_path"],
                source_sha256=stage2["source_sha256"],
                df=df,
            )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local/dev-only Workflow B single-file replay through Stage 1, Stage 2, and optional Stage 3 load."
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--file", required=True, help="Local .csv/.xls/.xlsx/.xlsm file to replay")
    parser.add_argument("--report-type", default=None, help="Expected report type; auto-detect when omitted")
    parser.add_argument("--dry-run", action="store_true", help="Run Stage 1/2 and Stage 3 load planning without DB writes")
    parser.add_argument("--load-stage3", action="store_true", help="Load into verified local/dev client DB after guard checks")
    args = parser.parse_args()

    if args.dry_run and args.load_stage3:
        parser.error("--dry-run and --load-stage3 are mutually exclusive")
    if not args.dry_run and not args.load_stage3:
        parser.error("choose --dry-run or --load-stage3")

    try:
        result = replay_file(
            client_code=args.client_code,
            file_path=args.file,
            expected_report_type=args.report_type,
            load_stage3=args.load_stage3,
        )
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
