#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.common.environment_identity import (  # noqa: E402
    EnvironmentIdentityError,
    attest_platform_identity,
    load_runtime_identity,
)
from jobs.reports.postprocess import job_alpha00001_dysponent_id_enrichment as enrichment  # noqa: E402

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_NOT_READY = 4
EXIT_RUNTIME_FAILURE = 5
DEFAULT_START_DATE = "2026-07-21"


class CliLogClient:
    def log(self, level, event_type, source, message, **kwargs):
        context = kwargs.get("context") or {}
        print(json.dumps({
            "level": level,
            "event_type": event_type,
            "source": source,
            "message": message,
            "context": context,
        }, sort_keys=True, default=str), file=sys.stderr)


def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    from dotenv import load_dotenv
    load_dotenv(path, override=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run-first ALPHA00001 Dysponent_ID backfill (Warsaw-local dates)."
    )
    client = parser.add_mutually_exclusive_group(required=True)
    client.add_argument("--client-code")
    client.add_argument("--client-id")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", help="Exclusive Warsaw-local end date; capped to the latest "
                                           "trip data, and to the source-load day only with "
                                           "--require-fresh-source")
    parser.add_argument("--execute", action="store_true", help="Enable batched target updates")
    parser.add_argument("--overwrite-existing", action="store_true")
    parser.add_argument("--coverage-threshold", default="95.00")
    parser.add_argument("--distance-coverage-threshold")
    parser.add_argument("--max-ambiguities", type=int, default=enrichment.DEFAULT_MAX_AMBIGUOUS_MATCHES)
    parser.add_argument("--max-source-age-hours", type=int, default=enrichment.DEFAULT_MAX_SOURCE_AGE_HOURS)
    parser.add_argument("--batch-size", type=int, default=enrichment.DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--require-fresh-source",
        action="store_true",
        help="Enforce the source freshness limit and cap the window at the source-load day; by "
             "default the full requested range is enriched from the last committed assignment "
             "snapshot, so assignments changed after that snapshot may be written as outdated values",
    )
    parser.add_argument(
        "--no-suspected-bug-report",
        action="store_true",
        help="Print detected assignment conflicts without writing a durable suspected_bug "
             "incident or enqueueing an alert email",
    )
    return parser


def _resolve_client(args) -> tuple[str, str]:
    runtime = load_runtime_identity()
    with enrichment._platform_pg_conn() as conn:
        attest_platform_identity(conn, runtime)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            if args.client_code:
                cur.execute(
                    """SELECT client_id::text, client_code
                       FROM workflow_a_control.client_account
                       WHERE enabled IS TRUE AND client_code = %s""",
                    (args.client_code,),
                )
            else:
                cur.execute(
                    """SELECT client_id::text, client_code
                       FROM workflow_a_control.client_account
                       WHERE enabled IS TRUE AND client_id = %s""",
                    (args.client_id,),
                )
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("requested enabled client was not found")
    code = str(row["client_code"])
    client_id = str(row["client_id"])
    enrichment._validate_client_code_allowed(code)
    return code, client_id


def _params(args, client_code: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "client_code": client_code,
        "dry_run": not args.execute,
        "overwrite_existing": args.overwrite_existing,
        "process_all": True,
        "date_from": args.start_date,
        "batch_size": args.batch_size,
        "max_source_age_hours": args.max_source_age_hours,
        "min_coverage_percent": args.coverage_threshold,
        "min_distance_coverage_percent": (
            args.distance_coverage_threshold or args.coverage_threshold
        ),
        "max_ambiguous_matches": args.max_ambiguities,
        "trigger": "controlled_alpha_dysponent_backfill",
        "report_suspected_bugs": not args.no_suspected_bug_report,
        "require_fresh_source": args.require_fresh_source,
    }
    if args.end_date:
        values["date_to"] = args.end_date
    if args.max_batches is not None:
        values["max_batches"] = args.max_batches
    if args.limit is not None:
        values["limit"] = args.limit
    return values


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.max_ambiguities < 0:
            raise ValueError("--max-ambiguities must be non-negative")
        if args.batch_size <= 0 or args.max_source_age_hours <= 0:
            raise ValueError("batch size and source age must be positive")
        client_code, client_id = _resolve_client(args)
        summary = enrichment.run(
            CliLogClient(),
            "alpha-dysponent-backfill-dry-run" if not args.execute else "alpha-dysponent-backfill-execute",
            _params(args, client_code),
        )
        if str(summary.get("client_id")) != client_id:
            raise EnvironmentIdentityError(
                enrichment.ENVIRONMENT_IDENTITY_NOT_VERIFIED,
                "resolved client identity changed during backfill preflight",
            )
        print(json.dumps({"command": "alpha_dysponent_backfill", **summary}, sort_keys=True, default=str))
        return EXIT_OK if summary.get("readiness_passed") else EXIT_NOT_READY
    except EnvironmentIdentityError as exc:
        print(json.dumps({"status": "ERROR", "error_code": "ENVIRONMENT_IDENTITY_NOT_VERIFIED",
                          "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return EXIT_IDENTITY_NOT_VERIFIED
    except enrichment.EnrichmentPreconditionError as exc:
        print(json.dumps({"status": "ERROR", "error_code": exc.code,
                          "message": str(exc), "diagnostics": exc.diagnostics},
                         sort_keys=True, default=str), file=sys.stderr)
        return EXIT_NOT_READY
    except (ValueError, argparse.ArgumentError) as exc:
        print(json.dumps({"status": "ERROR", "error_code": "INVALID_PARAMETERS",
                          "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return EXIT_INVALID_PARAMETERS
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error_code": type(exc).__name__},
                         sort_keys=True), file=sys.stderr)
        return EXIT_RUNTIME_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
