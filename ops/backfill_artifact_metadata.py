#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
STAGE1_SOURCE = "jobs.mail.fetch_reports"
STAGE2_SOURCE = "jobs.reports.stage2.job_stage2"
V2_KEY_RE = re.compile(
    r"^(?P<workflow>[^/]+)/(?P<stage>[^/]+)/yyyy=\d{4}/mm=\d{2}/dd=\d{2}/run_id=[^/]+/"
    r"(?:(?:report_type=(?P<report>[^/]+)/))?(?P<role>[^/]+)/(?P<filename>[^/]+)$"
)
SHA_PREFIX_RE = re.compile(r"^(?P<sha>[0-9a-f]{64})(?:__|\.|$)", re.IGNORECASE)
REPORT_SUFFIX_RE = re.compile(r"__(?P<report_type>report_[a-z0-9_]+)\.[^.]+$", re.IGNORECASE)


@dataclass
class InferenceResult:
    updates: dict[str, Any]
    confidence: str | None
    sources: list[str]
    ambiguous_reasons: list[str]


def _pg_conn():
    import psycopg
    from psycopg.rows import dict_row

    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return psycopg.connect(dsn, row_factory=dict_row)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _should_update(current: Any, inferred: Any, *, unknown_ok: bool = False) -> bool:
    if inferred is None or (isinstance(inferred, str) and not inferred.strip()):
        return False
    if _is_empty(current):
        return True
    if unknown_ok and str(current).strip().lower() in {"unknown", "workflow_unknown", "stage_unknown", "artifact"}:
        return str(current).strip() != str(inferred).strip()
    return False


def _file_ext(*values: Any) -> str | None:
    for value in values:
        if not value:
            continue
        ext = Path(str(value)).suffix.lower().lstrip(".")
        if ext:
            return ext
    return None


def _basename(value: Any) -> str | None:
    if not value:
        return None
    return Path(str(value)).name


def _sha_prefix(filename: Any) -> str | None:
    if not filename:
        return None
    match = SHA_PREFIX_RE.match(str(filename))
    return match.group("sha").lower() if match else None


def _report_type_from_filename(filename: Any) -> str | None:
    if not filename:
        return None
    match = REPORT_SUFFIX_RE.search(str(filename))
    return match.group("report_type").lower() if match else None


def _raw_metadata(row: dict[str, Any], raw_by_sha: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    if row.get("raw_file_id") or row.get("raw_original_filename") or row.get("raw_stage2_report_type"):
        return {
            "id": row.get("raw_file_id"),
            "original_filename": row.get("raw_original_filename"),
            "raw_path": row.get("raw_path"),
            "normalized_csv_path": row.get("normalized_csv_path"),
            "stage2_report_type": row.get("raw_stage2_report_type"),
            "sha256": row.get("raw_sha256"),
        }
    sha = _sha_prefix(row.get("filename") or row.get("storage_key"))
    if sha and raw_by_sha and sha in raw_by_sha:
        return raw_by_sha[sha]
    return None


def infer_updates(
    row: dict[str, Any],
    *,
    raw_by_sha: dict[str, dict[str, Any]] | None = None,
    min_confidence: str = "high",
) -> InferenceResult:
    updates: dict[str, Any] = {}
    sources: list[str] = []
    ambiguous: list[str] = []
    min_score = CONFIDENCE_ORDER[min_confidence]

    def add(field: str, value: Any, source: str, *, unknown_ok: bool = False, confidence: str = "high") -> None:
        if CONFIDENCE_ORDER[confidence] < min_score:
            return
        if _should_update(row.get(field), value, unknown_ok=unknown_ok):
            updates[field] = value
            if source not in sources:
                sources.append(source)

    storage_key = row.get("storage_key") or ""
    filename = row.get("filename")
    display_filename = row.get("display_filename")
    original_filename = row.get("original_filename")
    run_source = row.get("run_source")
    raw = _raw_metadata(row, raw_by_sha)

    v2_match = V2_KEY_RE.match(storage_key)
    if v2_match:
        add("workflow_name", v2_match.group("workflow"), "storage_key_v2", unknown_ok=True)
        add("stage_name", v2_match.group("stage"), "storage_key_v2", unknown_ok=True)
        add("artifact_role", v2_match.group("role"), "storage_key_v2", unknown_ok=True)
        add("report_type", v2_match.group("report"), "storage_key_v2", unknown_ok=True)
        if row.get("layout_version") != 2:
            updates["layout_version"] = 2
            sources.append("storage_key_v2")

    if run_source == STAGE1_SOURCE:
        add("workflow_name", "workflow_b", "run_source")
        add("stage_name", "stage_1_fetch", "run_source")
        raw_name = _basename(raw.get("raw_path")) if raw else None
        norm_name = _basename(raw.get("normalized_csv_path")) if raw else None
        current_name = _basename(filename)
        if raw and current_name and raw_name and current_name == raw_name:
            add("artifact_role", "raw", "ingest.raw_file.raw_path")
        elif raw and current_name and norm_name and current_name == norm_name:
            add("artifact_role", "normalized", "ingest.raw_file.normalized_csv_path")
        elif raw and _file_ext(filename, original_filename) in {"xls", "xlsx"}:
            add("artifact_role", "raw", "file_ext")
        elif raw and _file_ext(filename, original_filename) == "csv":
            add("artifact_role", "normalized", "file_ext")
        else:
            ambiguous.append("stage1_role_unknown")

    if run_source == STAGE2_SOURCE:
        add("workflow_name", "workflow_b", "run_source")
        add("stage_name", "stage_2_clean", "run_source")
        report_type = (raw or {}).get("stage2_report_type") or _report_type_from_filename(filename)
        add("report_type", report_type, "stage2_report_type_or_filename", unknown_ok=True)
        if _report_type_from_filename(filename):
            add("artifact_role", "cleaned", "stage2_cleaned_filename")
        else:
            ambiguous.append("stage2_role_unknown")

    if raw:
        add("raw_file_id", str(raw.get("id")), "sha256_filename_match")
        add("original_filename", raw.get("original_filename"), "ingest.raw_file")
        add("report_type", raw.get("stage2_report_type"), "ingest.raw_file.stage2_report_type", unknown_ok=True)

    add("file_ext", _file_ext(filename, display_filename, original_filename, storage_key), "filename_or_storage_key")
    add("display_filename", display_filename or filename, "existing_filename")

    if updates:
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        if isinstance(metadata, dict):
            new_metadata = dict(metadata)
            new_metadata.setdefault("backfilled", True)
            new_metadata.setdefault("backfill_source", ",".join(sources))
            new_metadata.setdefault("backfill_confidence", "high")
            new_metadata.setdefault("backfill_fields", sorted(updates.keys()))
            updates["metadata_json"] = new_metadata

    missing_semantic = [
        field
        for field in ("workflow_name", "stage_name", "artifact_role", "report_type", "file_ext", "display_filename")
        if _is_empty(row.get(field))
    ]
    missing_identity = [
        field
        for field in ("workflow_name", "stage_name", "artifact_role", "report_type")
        if _is_empty(row.get(field))
    ]
    metadata_only_fields = {"file_ext", "display_filename", "metadata_json"}
    if missing_identity and updates and set(updates).issubset(metadata_only_fields):
        updates = {}
        ambiguous.append("semantic_identity_unknown")
    if missing_semantic and not updates and not ambiguous:
        ambiguous.append("no_high_confidence_source")

    return InferenceResult(
        updates=updates,
        confidence="high" if updates else None,
        sources=sources,
        ambiguous_reasons=ambiguous,
    )


def _load_raw_by_sha(cur) -> dict[str, dict[str, Any]]:
    cur.execute(
        """
        SELECT id, sha256, original_filename, raw_path, normalized_csv_path, stage2_report_type
        FROM ingest.raw_file
        WHERE sha256 IS NOT NULL
        """
    )
    by_sha: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for row in cur.fetchall():
        sha = str(row["sha256"]).lower()
        if sha in by_sha:
            duplicates.add(sha)
            continue
        by_sha[sha] = {
            "id": row["id"],
            "sha256": sha,
            "original_filename": row.get("original_filename"),
            "raw_path": row.get("raw_path"),
            "normalized_csv_path": row.get("normalized_csv_path"),
            "stage2_report_type": row.get("stage2_report_type"),
        }
    for sha in duplicates:
        by_sha.pop(sha, None)
    return by_sha


def _select_rows(cur, args: argparse.Namespace) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if args.artifact_id:
        where.append("a.artifact_id = %s")
        params.append(args.artifact_id)
    if args.run_id:
        where.append("a.run_id = %s")
        params.append(args.run_id)
    if args.only_layout_version is not None:
        where.append("a.layout_version = %s")
        params.append(args.only_layout_version)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    limit_sql = "LIMIT %s" if args.limit is not None else ""
    if args.limit is not None:
        params.append(args.limit)
    cur.execute(
        f"""
        SELECT
          a.artifact_id, a.run_id, a.filename, a.storage_key, a.raw_file_id,
          a.workflow_name, a.stage_name, a.artifact_role, a.report_type,
          a.display_filename, a.original_filename, a.file_ext, a.layout_version,
          a.metadata_json,
          r.source AS run_source,
          rf.sha256 AS raw_sha256,
          rf.original_filename AS raw_original_filename,
          rf.raw_path,
          rf.normalized_csv_path,
          rf.stage2_report_type AS raw_stage2_report_type
        FROM artifacts a
        LEFT JOIN runs r ON r.run_id = a.run_id
        LEFT JOIN ingest.raw_file rf ON rf.id = a.raw_file_id
        {where_sql}
        ORDER BY a.created_at ASC
        {limit_sql}
        """,
        params,
    )
    return list(cur.fetchall())


def _apply_update(cur, artifact_id: Any, updates: dict[str, Any]) -> None:
    assignments = []
    params: list[Any] = []
    for field, value in updates.items():
        if field == "metadata_json":
            assignments.append("metadata_json = %s::jsonb")
            params.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
        else:
            assignments.append(f"{field} = %s")
            params.append(value)
    params.append(artifact_id)
    cur.execute(f"UPDATE artifacts SET {', '.join(assignments)} WHERE artifact_id = %s", params)


def run(args: argparse.Namespace) -> dict[str, int]:
    summary = {
        "total_scanned": 0,
        "would_update": 0,
        "updated": 0,
        "skipped_ambiguous": 0,
        "skipped_already_complete": 0,
        "errors": 0,
    }
    with _pg_conn() as conn:
        with conn.cursor() as cur:
            raw_by_sha = _load_raw_by_sha(cur)
            rows = _select_rows(cur, args)
            summary["total_scanned"] = len(rows)
            for row in rows:
                try:
                    result = infer_updates(row, raw_by_sha=raw_by_sha, min_confidence=args.min_confidence)
                    if result.updates:
                        if args.apply:
                            _apply_update(cur, row["artifact_id"], result.updates)
                            summary["updated"] += 1
                        else:
                            summary["would_update"] += 1
                            print(
                                json.dumps(
                                    {
                                        "artifact_id": str(row["artifact_id"]),
                                        "filename": row.get("filename"),
                                        "updates": result.updates,
                                        "sources": result.sources,
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    default=str,
                                )
                            )
                    elif result.ambiguous_reasons:
                        summary["skipped_ambiguous"] += 1
                    else:
                        summary["skipped_already_complete"] += 1
                except Exception as exc:
                    summary["errors"] += 1
                    print(f"ERROR artifact_id={row.get('artifact_id')}: {exc}")
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill high-confidence artifact metadata without touching MinIO objects.")
    parser.add_argument("--apply", action="store_true", help="Apply updates. Default is dry-run.")
    parser.add_argument("--artifact-id")
    parser.add_argument("--run-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-layout-version", type=int)
    parser.add_argument("--min-confidence", choices=sorted(CONFIDENCE_ORDER), default="high")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run(args)
    mode = "apply" if args.apply else "dry-run"
    print(f"mode={mode}")
    for key, value in summary.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
