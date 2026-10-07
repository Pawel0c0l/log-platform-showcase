#!/usr/bin/env python3
"""Send the current Stage 2 pending-review notification manually.

This operator tool reads the platform DB state directly. It does not run Stage 2
and intentionally does not use Stage 2's process-local duplicate protection.
"""
from __future__ import annotations

import argparse
import os
import sys
from html import escape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from jobs.common.emailer import send_html_email


REPO_ROOT = Path(__file__).resolve().parent.parent
LOW_CONFIDENCE_PENDING_REASON = "low_detection_confidence"
DEFAULT_ARTIFACT_EXPLORER_BASE_URL = "http://localhost:8000"
DEFAULT_PENDING_REVIEW_NOTIFY_TO = "owner@example.invalid"
SMTP_SECRET_ENV_NAMES = {"AUTOMATION_SMTP_USERNAME", "AUTOMATION_SMTP_PASSWORD"}


class UserFacingError(RuntimeError):
    pass


def _load_dotenv_if_present() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass

    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip("'\"")


def _require(module_name: str, feature: str):
    try:
        return __import__(module_name)
    except ImportError as exc:
        raise UserFacingError(
            f"Missing dependency for {feature}: {module_name}. Install host deps from requirements-host.txt."
        ) from exc


def _platform_dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _connect():
    psycopg = _require("psycopg", "Postgres connection")
    from psycopg.rows import dict_row

    return psycopg.connect(_platform_dsn(), row_factory=dict_row)


def _fetch_pending_review_rows(
    conn,
    *,
    include_all_pending_reasons: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    reason_filter = ""
    params: list[Any] = []
    if not include_all_pending_reasons:
        reason_filter = "AND rf.stage2_pending_reason = %s"
        params.append(LOW_CONFIDENCE_PENDING_REASON)

    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              a.run_id::text AS run_id,
              rf.id::text AS raw_file_id,
              COALESCE(
                rf.normalized_csv_path,
                rf.raw_path,
                a.display_filename,
                a.filename
              ) AS filename,
              rf.normalized_csv_path AS normalized_path,
              rf.raw_path AS current_file_path,
              rf.original_filename,
              COALESCE(
                a.metadata_json ->> 'detected_candidate_report_type',
                rf.stage2_report_type
              ) AS detected_candidate_report_type,
              COALESCE(
                a.metadata_json ->> 'detect_score',
                rf.stage2_scores ->> 'detect_score'
              ) AS detect_score,
              rf.stage2_pending_reason AS pending_reason,
              a.artifact_id::text AS artifact_id
            FROM ingest.raw_file rf
            LEFT JOIN LATERAL (
              SELECT
                artifact_id,
                run_id,
                filename,
                display_filename,
                artifact_role,
                report_type,
                metadata_json,
                created_at
              FROM artifacts
              WHERE raw_file_id = rf.id
              ORDER BY
                (report_type = 'PENDING_REVIEW') DESC NULLS LAST,
                (artifact_role = 'debug_sample') DESC NULLS LAST,
                created_at DESC
              LIMIT 1
            ) a ON TRUE
            WHERE rf.stage2_status = 'PENDING_REVIEW'
              {reason_filter}
            ORDER BY rf.stage2_updated_at DESC NULLS LAST, rf.id DESC
            {limit_clause}
            """,
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def _artifact_explorer_base_url() -> str:
    return (os.getenv("ARTIFACT_EXPLORER_BASE_URL") or DEFAULT_ARTIFACT_EXPLORER_BASE_URL).rstrip("/")


def _artifact_explorer_link(row: dict[str, Any]) -> str:
    base_url = _artifact_explorer_base_url()
    artifact_id = row.get("artifact_id")
    if artifact_id:
        return f"{base_url}/artifact-explorer/artifacts/{artifact_id}"

    params: dict[str, str] = {}
    if row.get("run_id"):
        params["run_id"] = str(row["run_id"])
    if row.get("raw_file_id"):
        params["raw_file_id"] = str(row["raw_file_id"])
    suffix = f"?{urlencode(params)}" if params else ""
    return f"{base_url}/artifact-explorer{suffix}"


def _score_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _build_notification_email(
    rows: list[dict[str, Any]],
    *,
    include_all_pending_reasons: bool = False,
) -> tuple[str, str, str]:
    count = len(rows)
    if include_all_pending_reasons:
        subject = f"[Automations] Stage 2 pending review: current database state ({count})"
        intro = f"Stage 2 currently has {count} report(s) that require manual review."
    else:
        subject = f"[Automations] Stage 2 pending review: low detection confidence ({count})"
        intro = (
            f"Stage 2 detected {count} report(s) that require manual review because "
            "the report type could not be detected with safe confidence."
        )

    header = (
        "<tr>"
        "<th>Run ID</th>"
        "<th>Filename / path</th>"
        "<th>Original filename</th>"
        "<th>Candidate type</th>"
        "<th>Score</th>"
        "<th>Reason</th>"
        "<th>Artifact Explorer</th>"
        "</tr>"
    )
    body_rows = []
    text_lines = [intro, ""]
    for row in rows:
        score = _score_text(row.get("detect_score"))
        link = _artifact_explorer_link(row)
        body_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('run_id') or ''))}</td>"
            f"<td>{escape(str(row.get('filename') or ''))}</td>"
            f"<td>{escape(str(row.get('original_filename') or ''))}</td>"
            f"<td>{escape(str(row.get('detected_candidate_report_type') or ''))}</td>"
            f"<td>{escape(score)}</td>"
            f"<td>{escape(str(row.get('pending_reason') or ''))}</td>"
            f'<td><a href="{escape(link, quote=True)}">Open artifact</a></td>'
            "</tr>"
        )
        text_lines.append(
            " | ".join(
                [
                    str(row.get("run_id") or ""),
                    str(row.get("filename") or ""),
                    str(row.get("original_filename") or ""),
                    str(row.get("detected_candidate_report_type") or ""),
                    score,
                    str(row.get("pending_reason") or ""),
                    link,
                ]
            )
        )

    html_body = (
        "<html><body>"
        f"<p>{escape(intro)}</p>"
        '<table style="border-collapse:collapse;" border="1" cellpadding="6" cellspacing="0">'
        f"{header}{''.join(body_rows)}"
        "</table>"
        "</body></html>"
    )
    return subject, html_body, "\n".join(text_lines)


def _parse_recipients(value: str | None) -> list[str]:
    if value is None:
        value = DEFAULT_PENDING_REVIEW_NOTIFY_TO
    return [addr.strip() for addr in value.replace(";", ",").split(",") if addr.strip()]


def _missing_smtp_config_vars() -> list[str]:
    missing = []
    if not (os.getenv("AUTOMATION_SMTP_HOST") or "").strip():
        missing.append("AUTOMATION_SMTP_HOST")
    return missing


def _redact_smtp_secrets(message: str) -> str:
    redacted = message
    for env_name in SMTP_SECRET_ENV_NAMES:
        value = os.getenv(env_name)
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def _print_preview(rows: list[dict[str, Any]], *, max_rows: int = 20) -> None:
    print(f"Stage 2 pending-review rows matched: {len(rows)}")
    if not rows:
        return
    print(f"Previewing first {min(max_rows, len(rows))} row(s):")
    for idx, row in enumerate(rows[:max_rows], start=1):
        print(
            " | ".join(
                [
                    f"{idx}. run_id={row.get('run_id') or ''}",
                    f"file={row.get('filename') or ''}",
                    f"original={row.get('original_filename') or ''}",
                    f"candidate={row.get('detected_candidate_report_type') or ''}",
                    f"score={_score_text(row.get('detect_score'))}",
                    f"reason={row.get('pending_reason') or ''}",
                    f"link={_artifact_explorer_link(row)}",
                ]
            )
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one aggregated Stage 2 pending-review notification for the current DB state."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print a safe preview without sending email.")
    parser.add_argument("--limit", type=int, help="Optional row limit for testing.")
    parser.add_argument(
        "--include-all-pending-reasons",
        action="store_true",
        help="Include all PENDING_REVIEW reasons instead of only low_detection_confidence.",
    )
    parser.add_argument("--to", help="Override notification recipient(s), comma- or semicolon-separated.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    connect_fn: Callable[[], Any] = _connect,
    email_sender: Callable[..., None] = send_html_email,
    load_env: bool = True,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return 2

    if load_env:
        _load_dotenv_if_present()

    try:
        conn = connect_fn()
    except UserFacingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: Could not connect to platform DB using POSTGRES_* env: {exc}", file=sys.stderr)
        return 2

    try:
        rows = _fetch_pending_review_rows(
            conn,
            include_all_pending_reasons=args.include_all_pending_reasons,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"ERROR: Stage 2 pending-review query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if not rows:
        print("No matching Stage 2 pending-review rows found; nothing to send.")
        return 0

    if args.dry_run:
        _print_preview(rows)
        print("Dry run only; email was not sent.")
        return 0

    recipients = _parse_recipients(args.to if args.to is not None else os.getenv("STAGE2_PENDING_REVIEW_NOTIFY_TO"))
    if not recipients:
        print("ERROR: Missing recipient configuration: STAGE2_PENDING_REVIEW_NOTIFY_TO", file=sys.stderr)
        return 2

    missing_smtp = _missing_smtp_config_vars()
    if missing_smtp:
        print("ERROR: Missing SMTP config: " + ", ".join(missing_smtp), file=sys.stderr)
        return 2

    subject, html_body, text_body = _build_notification_email(
        rows,
        include_all_pending_reasons=args.include_all_pending_reasons,
    )
    try:
        email_sender(
            to_addrs=recipients,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
    except Exception as exc:
        message = _redact_smtp_secrets(str(exc))[:400]
        print(
            f"ERROR: Stage 2 pending-review notification email failed: {type(exc).__name__}: {message}",
            file=sys.stderr,
        )
        return 1

    print(f"Sent Stage 2 pending-review notification with {len(rows)} row(s) to {len(recipients)} recipient(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
