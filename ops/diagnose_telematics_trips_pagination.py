#!/usr/bin/env python3
"""Standalone, GET-only diagnostic for the Telematics `/trips` pagination contract.

Purpose
-------

Between 2026-07-29 and 2026-08-01 the provider `/trips` endpoint started
emitting pagination metadata that contradicts the requested page:

  - `page=2&limit=1000` still answers `meta.current_page=1`,
  - `meta.last_page` looks like it is computed with page size 10,
  - `data` still carries up to 1000 rows.

The production client (`jobs/api/telematics/provider_client.py`) correctly
refuses that response with `PAGINATION_MISMATCH`, so no business rows are
written. What is still unknown is whether the provider *applies* the `page`
parameter to the returned rows while lying in `meta`. This tool answers exactly
that question and nothing else.

It is deliberately **not** part of the job/runner stack. It never creates a
platform run, never claims a schedule, never touches a client business
database, never uploads an artifact and never sends email. It issues at most
two `GET /trips` requests (three only when the single, explicitly enabled
transport-timeout retry fires) and writes a sanitized evidence bundle.

Safety model
------------

  - live execution requires `--allow-live-request`; without it the tool stops
    before any socket is opened and prints a dry-run plan;
  - only `DELTA00001` is accepted (smallest known affected fleet);
  - method is pinned to GET and the endpoint to `/trips`;
  - exactly the requested pages are fetched, at most two, with no page loop;
  - `1 <= limit <= 1000`; window `> 0` and `<= 1 hour`;
  - no retry unless `--allow-timeout-retry`, and then at most one, for
    transport-level timeouts only — never for pagination or shape anomalies;
  - redirects are never followed; a cross-host `Location` is a hard stop;
  - the response body is read under a hard byte cap;
  - the request budget is enforced before every single request.

Evidence model
--------------

Raw trip rows are never written to disk. Trip identities are HMAC-SHA256
digests keyed by a run-specific salt that lives only in memory and is never
persisted, so page 1 and page 2 stay comparable within one execution while the
bundle stays non-reversible for a practical observer.

Dry-run example (safe, no network, no credentials resolved):

    PYTHONPATH="$PWD" .venv/bin/python ops/diagnose_telematics_trips_pagination.py \
      --client-code DELTA00001 \
      --start-timestamp "2026-07-31 06:00:00" \
      --end-timestamp "2026-07-31 07:00:00" \
      --pages 1,2 \
      --limit 1000 \
      --output-dir /var/tmp/telematics-pagination-evidence
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `trips_wire_window` is imported so the diagnostic formats request timestamps
# byte identically to production. Production `/trips` addresses trips by
# Europe/Warsaw wall-clock (docs/18 §1), so a diagnostic built on the UTC
# projection would probe a period shifted by the Warsaw offset and report on
# different rows than the job reads. `_provider_dt_str` is kept only for the
# UTC evidence labels, which must stay UTC to remain comparable across runs.
# Importing the module does not construct a client and does not start any
# automatic pagination.
from jobs.api.telematics.provider_client import (  # noqa: E402
    _provider_dt_str,
    trips_wire_window,
)


TOOL_NAME = "diagnose_telematics_trips_pagination"
TOOL_VERSION = "1.0.0"

HTTP_METHOD = "GET"
ENDPOINT_PATH = "/trips"

# Start with the smallest known affected fleet. Extending this set requires an
# explicit reviewed change; there is intentionally no fallback client.
ALLOWED_CLIENT_CODES: Tuple[str, ...] = ("DELTA00001",)

MAX_PAGES_PER_EXECUTION = 2
LIVE_REQUIRED_PAGE_COUNT = 2
MAX_PAGE_NUMBER = 1000
MIN_LIMIT = 1
MAX_LIMIT = 1000
MAX_WINDOW_SECONDS = 3600
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MIN_MAX_RESPONSE_BYTES = 1024
MAX_TIMEOUT_RETRIES = 1
RESPONSE_CHUNK_BYTES = 64 * 1024

# Result classifications — exactly one is reported per execution.
RESULT_DRY_RUN_READY = "TELEMATICS_TRIPS_DIAGNOSTIC_DRY_RUN_READY"
RESULT_PAGES_DISTINCT = "TELEMATICS_TRIPS_PAGES_DISTINCT_METADATA_BROKEN"
RESULT_PAGE_PARAMETER_IGNORED = "TELEMATICS_TRIPS_PAGE_PARAMETER_IGNORED"
RESULT_PAGES_PARTIALLY_OVERLAP = "TELEMATICS_TRIPS_PAGES_PARTIALLY_OVERLAP"
RESULT_PAGE_2_EMPTY = "TELEMATICS_TRIPS_PAGE_2_EMPTY"
RESULT_IDENTITY_UNRESOLVED = "TELEMATICS_TRIPS_IDENTITY_CONTRACT_UNRESOLVED"
RESULT_RESPONSE_MALFORMED = "TELEMATICS_TRIPS_PROVIDER_RESPONSE_MALFORMED"
RESULT_TRANSPORT_FAILURE = "TELEMATICS_TRIPS_DIAGNOSTIC_TRANSPORT_FAILURE"
RESULT_SAFETY_BLOCKED = "TELEMATICS_TRIPS_DIAGNOSTIC_SAFETY_BLOCKED"

# Cross-page verdicts.
VERDICT_DISTINCT_CONTINUATION = "DISTINCT_CONTINUATION"
VERDICT_IDENTICAL_REPEAT = "IDENTICAL_REPEAT"
VERDICT_PARTIAL_OVERLAP = "PARTIAL_OVERLAP"
VERDICT_EMPTY_PAGE = "EMPTY_PAGE"
VERDICT_UNKNOWN_IDENTITY_CONTRACT = "UNKNOWN_IDENTITY_CONTRACT"

_VERDICT_TO_RESULT = {
    VERDICT_DISTINCT_CONTINUATION: RESULT_PAGES_DISTINCT,
    VERDICT_IDENTICAL_REPEAT: RESULT_PAGE_PARAMETER_IGNORED,
    VERDICT_PARTIAL_OVERLAP: RESULT_PAGES_PARTIALLY_OVERLAP,
    VERDICT_EMPTY_PAGE: RESULT_PAGE_2_EMPTY,
    VERDICT_UNKNOWN_IDENTITY_CONTRACT: RESULT_IDENTITY_UNRESOLVED,
}

EXIT_OK = 0
EXIT_SAFETY_BLOCKED = 2
EXIT_TRANSPORT_FAILURE = 3
EXIT_RESPONSE_MALFORMED = 4
EXIT_IDENTITY_UNRESOLVED = 5

_EXIT_BY_RESULT = {
    RESULT_DRY_RUN_READY: EXIT_OK,
    RESULT_PAGES_DISTINCT: EXIT_OK,
    RESULT_PAGE_PARAMETER_IGNORED: EXIT_OK,
    RESULT_PAGES_PARTIALLY_OVERLAP: EXIT_OK,
    RESULT_PAGE_2_EMPTY: EXIT_OK,
    RESULT_IDENTITY_UNRESOLVED: EXIT_IDENTITY_UNRESOLVED,
    RESULT_RESPONSE_MALFORMED: EXIT_RESPONSE_MALFORMED,
    RESULT_TRANSPORT_FAILURE: EXIT_TRANSPORT_FAILURE,
    RESULT_SAFETY_BLOCKED: EXIT_SAFETY_BLOCKED,
}

# Response headers that may be written to evidence. Authorization-related and
# cookie headers are deliberately absent and are never read.
EVIDENCE_HEADER_ALLOWLIST = (
    "content-type",
    "content-length",
    "date",
    "server",
    "x-request-id",
    "x-correlation-id",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "retry-after",
)

PAGINATION_META_FIELDS = ("from", "to", "current_page", "per_page", "last_page", "total")

# Trip identity. Production ingestion keys `client_trips` on
# `(client_id, provider_trip_id)` where `provider_trip_id = int(row["trip_id"])`
# (`jobs/api/telematics/sync_trips_and_speeding.py`), and derives `record_id` via
# `record_id.for_client_trips(...)`. The diagnostic reuses that same business
# key rather than inventing an identity algorithm.
IDENTITY_SOURCE_PROVIDER_TRIP_ID = "provider_trip_id"
IDENTITY_SOURCE_COMPOSITE_FALLBACK = "composite_fallback"
IDENTITY_FIELD_PRIMARY = "trip_id"
# Only used when `trip_id` is unusable, which by itself already means the
# identity contract is unresolved. Values are HMAC inputs only and are never
# written to evidence in any form.
COMPOSITE_FALLBACK_FIELDS = ("vehicle_id", "registration", "start_timestamp", "end_timestamp")

_UNIT_SEPARATOR = "\x1f"

EVIDENCE_FILE_MODE = 0o600
EVIDENCE_DIR_MODE = 0o700

REQUEST_PLAN_FILENAME = "request_plan.json"
CROSS_PAGE_FILENAME = "cross_page_comparison.json"
SUMMARY_FILENAME = "diagnostic_summary.json"
MANIFEST_FILENAME = "MANIFEST.txt"
SHA256SUMS_FILENAME = "SHA256SUMS"


class DiagnosticSafetyError(RuntimeError):
    """Safety stop. No further provider request may be issued after this."""

    def __init__(self, code: str, message: str, *, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.context = dict(context or {})


class DiagnosticTransportError(RuntimeError):
    """Transport-level failure (timeout, connection error, non-2xx status)."""

    def __init__(self, code: str, message: str, *, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.context = dict(context or {})


class DiagnosticResponseError(RuntimeError):
    """The provider answered, but the payload does not match the `/trips` contract."""

    def __init__(self, code: str, message: str, *, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.context = dict(context or {})


# ---------------------------------------------------------------------------
# Sanitization helpers
# ---------------------------------------------------------------------------

def _sanitize_origin(base_url: str) -> str:
    """Return `scheme://host[:port]`, dropping any userinfo, path, query."""
    parts = urlsplit(base_url if "//" in base_url else f"//{base_url}", scheme="https")
    host = parts.hostname or ""
    if not host:
        raise DiagnosticSafetyError(
            "PROVIDER_BASE_URL_INVALID", "provider base URL has no resolvable hostname"
        )
    origin = f"{parts.scheme or 'https'}://{host}"
    if parts.port:
        origin = f"{origin}:{parts.port}"
    return origin


def _base_path(base_url: str) -> str:
    parts = urlsplit(base_url if "//" in base_url else f"//{base_url}", scheme="https")
    return (parts.path or "").rstrip("/")


def _redact(text: Any, secret_values: Sequence[str]) -> str:
    """Remove credential material and URI userinfo from any operator-visible text."""
    rendered = str(text)
    for value in secret_values:
        if value and len(value) >= 3 and value in rendered:
            rendered = rendered.replace(value, "[redacted]")
    # `scheme://user:pass@host` never reaches evidence, even if a base URL carried it.
    out: List[str] = []
    for chunk in rendered.split("//"):
        at = chunk.find("@")
        if at > 0 and "/" not in chunk[:at] and " " not in chunk[:at]:
            chunk = "[redacted]@" + chunk[at + 1:]
        out.append(chunk)
    return "//".join(out)


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _short_digest(digest_hex: str) -> str:
    return digest_hex[:16]


# ---------------------------------------------------------------------------
# Stable trip identity (HMAC-keyed, in-memory salt)
# ---------------------------------------------------------------------------

def row_identity(row: Any, *, salt: bytes) -> Tuple[str, str]:
    """Return `(hmac_digest_hex, identity_source)` for one provider trip row.

    Primary identity is the ingestion business key component `trip_id`. The
    composite fallback exists only so a broken-identity page can still be
    compared; using it marks the identity contract as unresolved.
    """
    if isinstance(row, dict):
        raw_trip_id = row.get(IDENTITY_FIELD_PRIMARY)
        if raw_trip_id is not None and not isinstance(raw_trip_id, bool):
            try:
                material = f"{IDENTITY_SOURCE_PROVIDER_TRIP_ID}{_UNIT_SEPARATOR}{int(raw_trip_id)}"
            except (TypeError, ValueError):
                pass
            else:
                return _hmac_hex(material, salt=salt), IDENTITY_SOURCE_PROVIDER_TRIP_ID
        parts = [IDENTITY_SOURCE_COMPOSITE_FALLBACK]
        for name in COMPOSITE_FALLBACK_FIELDS:
            parts.append(f"{name}={row.get(name)!r}")
        return _hmac_hex(_UNIT_SEPARATOR.join(parts), salt=salt), IDENTITY_SOURCE_COMPOSITE_FALLBACK

    material = f"{IDENTITY_SOURCE_COMPOSITE_FALLBACK}{_UNIT_SEPARATOR}non_object{_UNIT_SEPARATOR}"
    material += _canonical_json_bytes(row).decode("utf-8", errors="replace")
    return _hmac_hex(material, salt=salt), IDENTITY_SOURCE_COMPOSITE_FALLBACK


def _hmac_hex(material: str, *, salt: bytes) -> str:
    return hmac.new(salt, material.encode("utf-8"), hashlib.sha256).hexdigest()


def summarize_data_identity(data: Sequence[Any], *, salt: bytes) -> Dict[str, Any]:
    identities: List[str] = []
    sources: List[str] = []
    for row in data:
        digest, source = row_identity(row, salt=salt)
        identities.append(digest)
        sources.append(source)

    unique = sorted(set(identities))
    primary_count = sum(1 for s in sources if s == IDENTITY_SOURCE_PROVIDER_TRIP_ID)
    fallback_count = len(sources) - primary_count
    if not sources:
        identity_source = IDENTITY_SOURCE_PROVIDER_TRIP_ID
    elif fallback_count == 0:
        identity_source = IDENTITY_SOURCE_PROVIDER_TRIP_ID
    elif primary_count == 0:
        identity_source = IDENTITY_SOURCE_COMPOSITE_FALLBACK
    else:
        identity_source = "mixed"

    return {
        "row_count": len(data),
        "page_data_sha256": _sha256_hex(_canonical_json_bytes(list(data))),
        "ordered_identity_sha256": _sha256_hex("\n".join(identities).encode("utf-8")),
        "unordered_identity_sha256": _sha256_hex("\n".join(unique).encode("utf-8")),
        "first_identity": _short_digest(identities[0]) if identities else None,
        "last_identity": _short_digest(identities[-1]) if identities else None,
        "unique_identity_count": len(unique),
        "duplicate_identity_count": len(identities) - len(unique),
        "identity_source": identity_source,
        "rows_with_provider_trip_id": primary_count,
        "rows_without_provider_trip_id": fallback_count,
        "identity_contract_resolved": fallback_count == 0,
        # Retained in memory only for the cross-page comparison.
        "_identities": identities,
    }


# ---------------------------------------------------------------------------
# Pagination metadata capture
# ---------------------------------------------------------------------------

def summarize_pagination_meta(payload: Any) -> Dict[str, Any]:
    meta_present = isinstance(payload, dict) and "meta" in payload
    meta_raw = payload.get("meta") if isinstance(payload, dict) else None

    summary: Dict[str, Any] = {
        "meta_present": bool(meta_present),
        "meta_type": _json_type_name(meta_raw) if meta_present else "absent",
        "meta_is_object": isinstance(meta_raw, dict),
        "fields": {},
        "other_meta_keys": [],
        "meta_nested_unexpectedly": False,
        "nested_meta_keys": [],
    }

    if not isinstance(meta_raw, dict):
        # A non-object `meta` is itself an unexpected nesting/shape signal.
        summary["meta_nested_unexpectedly"] = meta_present
        for name in PAGINATION_META_FIELDS:
            summary["fields"][name] = {"present": False, "type": "absent", "value": None}
        return summary

    nested: List[str] = []
    for key, value in meta_raw.items():
        if isinstance(value, (dict, list)):
            nested.append(str(key))

    for name in PAGINATION_META_FIELDS:
        if name not in meta_raw:
            summary["fields"][name] = {"present": False, "type": "absent", "value": None}
            continue
        value = meta_raw[name]
        entry: Dict[str, Any] = {"present": True, "type": _json_type_name(value)}
        if isinstance(value, (dict, list)):
            # Never persist a nested structure blindly; record shape only.
            entry["value"] = None
            entry["nested_key_count"] = len(value)
        else:
            entry["value"] = value
        summary["fields"][name] = entry

    summary["other_meta_keys"] = sorted(
        str(key) for key in meta_raw.keys() if key not in PAGINATION_META_FIELDS
    )
    summary["other_meta_key_types"] = {
        str(key): _json_type_name(meta_raw[key])
        for key in sorted(meta_raw.keys())
        if key not in PAGINATION_META_FIELDS
    }
    summary["nested_meta_keys"] = sorted(nested)
    summary["meta_nested_unexpectedly"] = bool(nested)
    return summary


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiagnosticPlan:
    client_code: str
    start_ts: datetime
    end_ts: datetime
    pages: Tuple[int, ...]
    limit: int
    include_private: bool
    timeout_seconds: int
    max_response_bytes: int
    output_dir: Path
    allow_live_request: bool
    allow_timeout_retry: bool
    expected_base_url: Optional[str]

    @property
    def window_seconds(self) -> int:
        return int((self.end_ts - self.start_ts).total_seconds())

    @property
    def max_requests(self) -> int:
        base = len(self.pages)
        return base + (MAX_TIMEOUT_RETRIES if self.allow_timeout_retry else 0)

    def provider_params(self, page: int) -> Dict[str, Any]:
        wire_start, wire_end, _wire_context = trips_wire_window(self.start_ts, self.end_ts)
        return {
            "start_timestamp": wire_start,
            "end_timestamp": wire_end,
            "incl_private": str(bool(self.include_private)).lower(),
            "page": page,
            "limit": self.limit,
        }

    def safety_bounds(self) -> Dict[str, Any]:
        return {
            "method": HTTP_METHOD,
            "endpoint_path": ENDPOINT_PATH,
            "allowed_client_codes": list(ALLOWED_CLIENT_CODES),
            "max_pages_per_execution": MAX_PAGES_PER_EXECUTION,
            "live_required_page_count": LIVE_REQUIRED_PAGE_COUNT,
            "min_limit": MIN_LIMIT,
            "max_limit": MAX_LIMIT,
            "max_window_seconds": MAX_WINDOW_SECONDS,
            "min_timeout_seconds": MIN_TIMEOUT_SECONDS,
            "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
            "max_response_bytes_ceiling": MAX_MAX_RESPONSE_BYTES,
            "max_timeout_retries": MAX_TIMEOUT_RETRIES if self.allow_timeout_retry else 0,
            "retry_only_for": "transport_timeout" if self.allow_timeout_retry else "none",
            "automatic_page_loop": False,
            "follow_redirects": False,
            "cross_host_redirect_allowed": False,
            "effective_max_requests": self.max_requests,
            "effective_max_response_bytes": self.max_response_bytes,
            "effective_timeout_seconds": self.timeout_seconds,
        }

    def public_document(self) -> Dict[str, Any]:
        return {
            "tool_name": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "client_code": self.client_code,
            "method": HTTP_METHOD,
            "endpoint_path": ENDPOINT_PATH,
            "expected_base_url": self.expected_base_url,
            "requested_pages": list(self.pages),
            "limit": self.limit,
            "incl_private": bool(self.include_private),
            "window_start_ts": _provider_dt_str(self.start_ts),
            "window_end_ts": _provider_dt_str(self.end_ts),
            "wire_start_timestamp": trips_wire_window(self.start_ts, self.end_ts)[0],
            "wire_end_timestamp": trips_wire_window(self.start_ts, self.end_ts)[1],
            "window_seconds": self.window_seconds,
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "allow_live_request": self.allow_live_request,
            "allow_timeout_retry": self.allow_timeout_retry,
            "request_budget": self.max_requests,
            "output_dir": str(self.output_dir),
            "safety_bounds": self.safety_bounds(),
            "planned_requests": [
                {
                    "request_sequence": index,
                    "method": HTTP_METHOD,
                    "endpoint_path": ENDPOINT_PATH,
                    "params": self.provider_params(page),
                }
                for index, page in enumerate(self.pages, start=1)
            ],
        }


def _parse_timestamp(raw: str, *, label: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise DiagnosticSafetyError("TIMESTAMP_INVALID", f"{label} is required")
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DiagnosticSafetyError(
                "TIMESTAMP_INVALID",
                f"{label} must be 'YYYY-MM-DD HH:MM:SS' or ISO-8601",
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_pages(raw: str) -> Tuple[int, ...]:
    pages: List[int] = []
    for part in (raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            page = int(text)
        except ValueError as exc:
            raise DiagnosticSafetyError("PAGES_INVALID", "--pages must be comma-separated integers") from exc
        if page < 1 or page > MAX_PAGE_NUMBER:
            raise DiagnosticSafetyError(
                "PAGES_OUT_OF_RANGE", f"page numbers must be between 1 and {MAX_PAGE_NUMBER}"
            )
        if page in pages:
            raise DiagnosticSafetyError("PAGES_DUPLICATE", "--pages must not repeat a page number")
        pages.append(page)
    if not pages:
        raise DiagnosticSafetyError("PAGES_INVALID", "--pages must name at least one page")
    if len(pages) > MAX_PAGES_PER_EXECUTION:
        raise DiagnosticSafetyError(
            "PAGES_EXCEED_LIMIT",
            f"at most {MAX_PAGES_PER_EXECUTION} pages may be fetched per execution",
        )
    return tuple(pages)


def _parse_bool_option(raw: str, *, label: str) -> bool:
    text = (raw or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise DiagnosticSafetyError("BOOL_OPTION_INVALID", f"{label} must be true or false")


def build_plan(args: argparse.Namespace) -> DiagnosticPlan:
    client_code = (args.client_code or "").strip()
    if client_code not in ALLOWED_CLIENT_CODES:
        raise DiagnosticSafetyError(
            "CLIENT_NOT_ALLOWED",
            "client code is not permitted by this diagnostic",
            context={"allowed_client_codes": list(ALLOWED_CLIENT_CODES), "client_code": client_code},
        )

    start_ts = _parse_timestamp(args.start_timestamp, label="--start-timestamp")
    end_ts = _parse_timestamp(args.end_timestamp, label="--end-timestamp")
    if end_ts <= start_ts:
        raise DiagnosticSafetyError(
            "WINDOW_NOT_POSITIVE", "--end-timestamp must be later than --start-timestamp"
        )
    window_seconds = int((end_ts - start_ts).total_seconds())
    if window_seconds > MAX_WINDOW_SECONDS:
        raise DiagnosticSafetyError(
            "WINDOW_TOO_LONG",
            f"requested window must be at most {MAX_WINDOW_SECONDS} seconds",
            context={"window_seconds": window_seconds},
        )

    pages = _parse_pages(args.pages)

    limit = int(args.limit)
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise DiagnosticSafetyError(
            "LIMIT_OUT_OF_RANGE",
            f"--limit must be between {MIN_LIMIT} and {MAX_LIMIT}",
            context={"limit": limit},
        )

    timeout_seconds = int(args.timeout_seconds)
    if timeout_seconds < MIN_TIMEOUT_SECONDS or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise DiagnosticSafetyError(
            "TIMEOUT_OUT_OF_RANGE",
            f"--timeout-seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}",
        )

    max_response_bytes = int(args.max_response_bytes)
    if max_response_bytes < MIN_MAX_RESPONSE_BYTES or max_response_bytes > MAX_MAX_RESPONSE_BYTES:
        raise DiagnosticSafetyError(
            "MAX_RESPONSE_BYTES_OUT_OF_RANGE",
            f"--max-response-bytes must be between {MIN_MAX_RESPONSE_BYTES} and {MAX_MAX_RESPONSE_BYTES}",
        )

    include_private = _parse_bool_option(args.include_private, label="--include-private")

    if args.allow_live_request and len(pages) != LIVE_REQUIRED_PAGE_COUNT:
        raise DiagnosticSafetyError(
            "LIVE_REQUIRES_TWO_PAGES",
            "a live execution must request exactly two pages so the comparison is meaningful",
            context={"requested_pages": list(pages)},
        )

    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = (Path.cwd() / output_dir).resolve()
    else:
        output_dir = Path(os.path.normpath(str(output_dir)))
    if output_dir == REPO_ROOT or REPO_ROOT in output_dir.parents:
        raise DiagnosticSafetyError(
            "OUTPUT_DIR_INSIDE_REPOSITORY",
            "evidence must be written outside the repository working tree",
        )

    expected_base_url = (args.expected_base_url or "").strip() or None

    return DiagnosticPlan(
        client_code=client_code,
        start_ts=start_ts,
        end_ts=end_ts,
        pages=pages,
        limit=limit,
        include_private=include_private,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        output_dir=output_dir,
        allow_live_request=bool(args.allow_live_request),
        allow_timeout_retry=bool(args.allow_timeout_retry),
        expected_base_url=expected_base_url,
    )


# ---------------------------------------------------------------------------
# Request budget
# ---------------------------------------------------------------------------

@dataclass
class RequestBudget:
    """Hard cap checked before every single network request."""

    max_requests: int
    used: int = 0

    def consume(self, *, purpose: str) -> int:
        if self.used >= self.max_requests:
            raise DiagnosticSafetyError(
                "REQUEST_BUDGET_EXCEEDED",
                "diagnostic request budget exhausted",
                context={"max_requests": self.max_requests, "used": self.used, "purpose": purpose},
            )
        self.used += 1
        return self.used


# ---------------------------------------------------------------------------
# Live fetch
# ---------------------------------------------------------------------------

@dataclass
class FetchOutcome:
    request_sequence: int
    requested_page: int
    http: Dict[str, Any]
    payload: Any
    retry_count: int
    raw_length: int


def _sanitized_headers(headers: Any) -> Dict[str, str]:
    if not headers:
        return {}
    try:
        items = list(headers.items())
    except AttributeError:
        return {}
    safe: Dict[str, str] = {}
    for key, value in items:
        name = str(key).strip().lower()
        if name in EVIDENCE_HEADER_ALLOWLIST:
            safe[name] = str(value)[:500]
    return safe


def _redirect_target(response: Any, *, request_url: str) -> Optional[Dict[str, str]]:
    location = None
    headers = getattr(response, "headers", None)
    if headers:
        try:
            location = headers.get("Location") or headers.get("location")
        except AttributeError:
            location = None
    if not location:
        return None
    absolute = urljoin(request_url, str(location))
    parts = urlsplit(absolute)
    return {"host": parts.hostname or "", "path": parts.path or "", "scheme": parts.scheme or ""}


def _read_bounded_body(response: Any, *, max_bytes: int, requested_page: int) -> bytes:
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=RESPONSE_CHUNK_BYTES):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise DiagnosticSafetyError(
                "RESPONSE_TOO_LARGE",
                "provider response exceeded the configured byte cap",
                context={"max_response_bytes": max_bytes, "requested_page": requested_page},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_page(
    *,
    session: Any,
    origin: str,
    url: str,
    plan: DiagnosticPlan,
    page: int,
    budget: RequestBudget,
    secret_values: Sequence[str],
    monotonic: Any,
) -> FetchOutcome:
    """Issue exactly one bounded `GET /trips`, plus at most one timeout retry."""
    timeout_module = _requests_exceptions()
    retry_count = 0
    attempt = 0

    while True:
        attempt += 1
        sequence = budget.consume(purpose=f"page_{page}_attempt_{attempt}")
        started_at = monotonic()
        response = None
        try:
            response = session.request(
                method=HTTP_METHOD,
                url=url,
                params=plan.provider_params(page),
                timeout=plan.timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            status_code = int(getattr(response, "status_code", 0))
            headers = _sanitized_headers(getattr(response, "headers", None))

            if 300 <= status_code < 400:
                target = _redirect_target(response, request_url=url) or {}
                target_host = target.get("host") or ""
                origin_host = urlsplit(origin).hostname or ""
                history = [
                    {
                        "status": status_code,
                        "host": target_host,
                        "path": target.get("path") or "",
                        "scheme": target.get("scheme") or "",
                        "followed": False,
                    }
                ]
                code = "REDIRECT_CROSS_HOST" if target_host != origin_host else "REDIRECT_NOT_FOLLOWED"
                raise DiagnosticSafetyError(
                    code,
                    "provider answered with a redirect; the diagnostic never follows redirects",
                    context={
                        "requested_page": page,
                        "request_sequence": sequence,
                        "status_code": status_code,
                        "redirect_count": 1,
                        "redirect_history": history,
                        "expected_host": origin_host,
                        "response_headers": headers,
                    },
                )

            body = _read_bounded_body(
                response, max_bytes=plan.max_response_bytes, requested_page=page
            )
            elapsed = max(0.0, monotonic() - started_at)

            http_info: Dict[str, Any] = {
                "request_sequence": sequence,
                "requested_page": page,
                "method": HTTP_METHOD,
                "endpoint_path": ENDPOINT_PATH,
                "http_status": status_code,
                "response_content_type": headers.get("content-type"),
                "response_byte_length": len(body),
                "elapsed_seconds": round(elapsed, 6),
                "redirect_count": 0,
                "redirect_history": [],
                "response_headers": headers,
                "retry_count": retry_count,
            }

            if status_code < 200 or status_code >= 300:
                raise DiagnosticTransportError(
                    "HTTP_STATUS_NOT_OK",
                    "provider returned a non-success HTTP status",
                    context={**http_info},
                )

            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DiagnosticResponseError(
                    "RESPONSE_NOT_JSON",
                    "provider response body is not valid UTF-8 JSON",
                    context={**http_info, "parse_error_class": type(exc).__name__},
                ) from None

            return FetchOutcome(
                request_sequence=sequence,
                requested_page=page,
                http=http_info,
                payload=payload,
                retry_count=retry_count,
                raw_length=len(body),
            )
        except timeout_module.Timeout as exc:
            if plan.allow_timeout_retry and retry_count < MAX_TIMEOUT_RETRIES:
                retry_count += 1
                continue
            raise DiagnosticTransportError(
                "TRANSPORT_TIMEOUT",
                "provider request timed out",
                context={
                    "requested_page": page,
                    "request_sequence": sequence,
                    "retry_count": retry_count,
                    "exception_class": type(exc).__name__,
                    "detail": _redact(exc, secret_values)[:300],
                },
            ) from None
        except timeout_module.RequestException as exc:
            raise DiagnosticTransportError(
                "TRANSPORT_ERROR",
                "provider request failed at transport level",
                context={
                    "requested_page": page,
                    "request_sequence": sequence,
                    "retry_count": retry_count,
                    "exception_class": type(exc).__name__,
                    "detail": _redact(exc, secret_values)[:300],
                },
            ) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001 - closing must never mask the primary error
                    pass


def _requests_exceptions():
    import requests  # local import keeps dry-run free of any HTTP machinery

    return requests.exceptions


# ---------------------------------------------------------------------------
# Page summary and cross-page comparison
# ---------------------------------------------------------------------------

def summarize_page(outcome: FetchOutcome, *, salt: bytes) -> Dict[str, Any]:
    payload = outcome.payload
    if not isinstance(payload, dict):
        raise DiagnosticResponseError(
            "RESPONSE_NOT_OBJECT",
            "provider response is not a JSON object",
            context={**outcome.http, "payload_type": _json_type_name(payload)},
        )

    data = payload.get("data")
    if data is None:
        raise DiagnosticResponseError(
            "RESPONSE_DATA_MISSING",
            "provider response has no data key",
            context={**outcome.http, "top_level_keys": sorted(str(k) for k in payload.keys())},
        )
    if not isinstance(data, list):
        raise DiagnosticResponseError(
            "RESPONSE_DATA_NOT_LIST",
            "provider data is not a list",
            context={**outcome.http, "data_type": _json_type_name(data)},
        )

    identity = summarize_data_identity(data, salt=salt)
    identities = identity.pop("_identities")

    summary = {
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "requested_page": outcome.requested_page,
        "http": outcome.http,
        "pagination_meta": summarize_pagination_meta(payload),
        "data_identity": identity,
        "top_level_keys": sorted(str(k) for k in payload.keys()),
    }
    return {"summary": summary, "identities": identities}


def compare_pages(
    *,
    first_page: int,
    second_page: int,
    first: Dict[str, Any],
    second: Dict[str, Any],
    first_identities: Sequence[str],
    second_identities: Sequence[str],
) -> Dict[str, Any]:
    first_identity = first["data_identity"]
    second_identity = second["data_identity"]

    set_one = set(first_identities)
    set_two = set(second_identities)
    intersection = set_one & set_two

    def _ratio(numerator: int, denominator: int) -> Optional[float]:
        if denominator <= 0:
            return None
        return round(numerator / denominator, 6)

    identity_resolved = bool(
        first_identity["identity_contract_resolved"] and second_identity["identity_contract_resolved"]
    )

    if len(second_identities) == 0:
        verdict = VERDICT_EMPTY_PAGE
    elif not identity_resolved:
        verdict = VERDICT_UNKNOWN_IDENTITY_CONTRACT
    elif set_one == set_two and set_one:
        verdict = VERDICT_IDENTICAL_REPEAT
    elif not intersection:
        verdict = VERDICT_DISTINCT_CONTINUATION
    else:
        verdict = VERDICT_PARTIAL_OVERLAP

    return {
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "first_page": first_page,
        "second_page": second_page,
        "first_page_row_count": first_identity["row_count"],
        "second_page_row_count": second_identity["row_count"],
        "exact_page_payload_equality": (
            first_identity["page_data_sha256"] == second_identity["page_data_sha256"]
        ),
        "ordered_identity_sequence_equality": (
            first_identity["ordered_identity_sha256"] == second_identity["ordered_identity_sha256"]
        ),
        "unordered_identity_set_equality": (
            first_identity["unordered_identity_sha256"] == second_identity["unordered_identity_sha256"]
        ),
        "intersection_count": len(intersection),
        "intersection_ratio_of_first_page": _ratio(len(intersection), len(set_one)),
        "intersection_ratio_of_second_page": _ratio(len(intersection), len(set_two)),
        "first_page_only_count": len(set_one - set_two),
        "second_page_only_count": len(set_two - set_one),
        "identity_contract_resolved": identity_resolved,
        "identity_source_first_page": first_identity["identity_source"],
        "identity_source_second_page": second_identity["identity_source"],
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Evidence bundle
# ---------------------------------------------------------------------------

class EvidenceBundle:
    """Writes only sanitized evidence, with 0600 files under a 0700 directory."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.filenames: List[str] = []

    def prepare(self) -> None:
        if self.directory.is_symlink():
            raise DiagnosticSafetyError(
                "OUTPUT_DIR_SYMLINK", "evidence directory must not be a symlink"
            )
        self.directory.mkdir(mode=EVIDENCE_DIR_MODE, parents=True, exist_ok=True)
        if not self.directory.is_dir():
            raise DiagnosticSafetyError("OUTPUT_DIR_NOT_DIRECTORY", "evidence path is not a directory")
        for reserved in (REQUEST_PLAN_FILENAME, SUMMARY_FILENAME, MANIFEST_FILENAME, SHA256SUMS_FILENAME):
            if (self.directory / reserved).exists():
                raise DiagnosticSafetyError(
                    "OUTPUT_DIR_NOT_EMPTY",
                    "evidence directory already holds a bundle; choose a fresh directory",
                    context={"conflicting_file": reserved},
                )
        try:
            os.chmod(self.directory, EVIDENCE_DIR_MODE)
        except OSError:
            pass

    def write_json(self, filename: str, document: Any) -> None:
        self.write_text(filename, json.dumps(document, indent=2, sort_keys=True, default=str) + "\n")

    def write_text(self, filename: str, text: str) -> None:
        path = self.directory / filename
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, EVIDENCE_FILE_MODE)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(path, EVIDENCE_FILE_MODE)
        if filename not in self.filenames:
            self.filenames.append(filename)

    def finalize(self, *, header: Dict[str, Any]) -> None:
        listed = sorted(self.filenames)
        lines = [
            f"{TOOL_NAME} {TOOL_VERSION}",
            f"generated_at_utc: {header['execution_timestamp_utc']}",
            f"repository_commit: {header['repository_commit']}",
            f"client_code: {header['client_code']}",
            f"endpoint: {header['sanitized_endpoint']}",
            f"requested_pages: {header['requested_pages']}",
            f"limit: {header['limit']}",
            f"window: {header['window_start_ts']} .. {header['window_end_ts']}",
            f"live_execution_enabled: {header['live_execution_enabled']}",
            f"request_count: {header['request_count']}",
            f"result_classification: {header['result_classification']}",
            "",
            "files:",
        ]
        for name in listed:
            size = (self.directory / name).stat().st_size
            lines.append(f"  {name} ({size} bytes)")
        lines.append("")
        lines.append("No raw provider payload, credential or personal trip field is retained.")
        self.write_text(MANIFEST_FILENAME, "\n".join(lines) + "\n")

        checksum_lines = []
        for name in sorted(self.filenames):
            digest = _sha256_hex((self.directory / name).read_bytes())
            checksum_lines.append(f"{digest}  {name}")
        self.write_text(SHA256SUMS_FILENAME, "\n".join(checksum_lines) + "\n")


def verify_bundle(directory: Path) -> List[str]:
    """Re-verify SHA256SUMS against the bundle. Returns failing filenames."""
    failures: List[str] = []
    checksums = (directory / SHA256SUMS_FILENAME).read_text(encoding="utf-8")
    for line in checksums.splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        path = directory / name
        if not path.exists() or _sha256_hex(path.read_bytes()) != digest:
            failures.append(name)
    return failures


# ---------------------------------------------------------------------------
# Provider configuration (read-only control plane)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderAccess:
    client_id: str
    client_code: str
    base_url: str
    username: str
    password: str


def load_provider_access(client_code: str) -> ProviderAccess:
    """Read-only control-plane lookup + existing secret resolution.

    Performs a single `SELECT` against `workflow_a_control.client_account`; it
    creates no run, claims no schedule and writes nothing.
    """
    from jobs.api.telematics.control_plane import _platform_pg_conn  # noqa: PLC0415
    from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    conn = _platform_pg_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                  client_id::text AS client_id,
                  client_code,
                  provider_base_url,
                  provider_basic_auth_username,
                  provider_basic_auth_password_secret_ref
                FROM workflow_a_control.client_account
                WHERE client_code=%s
                  AND enabled=true
                LIMIT 1
                """,
                (client_code,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise DiagnosticSafetyError(
            "CLIENT_ACCOUNT_NOT_FOUND",
            "no enabled client_account row for the requested client code",
            context={"client_code": client_code},
        )

    password = resolve_secret(row["provider_basic_auth_password_secret_ref"])
    if not password:
        raise DiagnosticSafetyError(
            "PROVIDER_SECRET_EMPTY", "resolved provider secret is empty"
        )

    return ProviderAccess(
        client_id=row["client_id"],
        client_code=row["client_code"],
        base_url=row["provider_base_url"],
        username=row["provider_basic_auth_username"],
        password=password,
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _repository_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and value else "unknown"


def run_dry_run(plan: DiagnosticPlan, bundle: EvidenceBundle) -> Dict[str, Any]:
    document = plan.public_document()
    document["live_execution_enabled"] = False
    document["dry_run_note"] = (
        "No socket is opened, no credential is resolved and no control-plane row is read "
        "in dry-run mode. Re-run the identical command with --allow-live-request to execute."
    )
    bundle.write_json(REQUEST_PLAN_FILENAME, document)
    return {
        "result_classification": RESULT_DRY_RUN_READY,
        "request_count": 0,
        "sanitized_endpoint": (
            f"{plan.expected_base_url.rstrip('/')}{ENDPOINT_PATH}"
            if plan.expected_base_url
            else f"<configured provider base URL>{ENDPOINT_PATH}"
        ),
        "pages": {},
        "cross_page": None,
        "blocked": None,
    }


def run_live(
    plan: DiagnosticPlan,
    bundle: EvidenceBundle,
    *,
    budget: RequestBudget,
    session_factory: Any = None,
    access_loader: Any = None,
    monotonic: Any = None,
) -> Dict[str, Any]:
    import time as _time  # noqa: PLC0415

    if len(plan.pages) != LIVE_REQUIRED_PAGE_COUNT:
        raise DiagnosticSafetyError(
            "LIVE_REQUIRES_TWO_PAGES",
            "a live execution must request exactly two pages so the comparison is meaningful",
            context={"requested_pages": list(plan.pages)},
        )

    monotonic = monotonic or _time.monotonic
    access_loader = access_loader or load_provider_access
    access = access_loader(plan.client_code)

    secret_values = [access.password, access.username]
    origin = _sanitize_origin(access.base_url)
    sanitized_endpoint = f"{origin}{_base_path(access.base_url)}{ENDPOINT_PATH}"

    if plan.expected_base_url:
        expected_origin = _sanitize_origin(plan.expected_base_url)
        expected_endpoint = f"{expected_origin}{_base_path(plan.expected_base_url)}"
        configured_endpoint = f"{origin}{_base_path(access.base_url)}"
        if expected_endpoint.rstrip("/") != configured_endpoint.rstrip("/"):
            raise DiagnosticSafetyError(
                "BASE_URL_MISMATCH",
                "configured provider base URL does not match --expected-base-url",
                context={
                    "expected_base_url": expected_endpoint,
                    "configured_base_url": configured_endpoint,
                },
            )

    request_plan = plan.public_document()
    request_plan["live_execution_enabled"] = True
    request_plan["sanitized_endpoint"] = sanitized_endpoint
    request_plan["provider_host"] = urlsplit(origin).hostname
    bundle.write_json(REQUEST_PLAN_FILENAME, request_plan)

    # Run-specific salt: identical for every page of this execution so page 1
    # and page 2 remain comparable; never written anywhere.
    salt = secrets.token_bytes(32)
    url = f"{origin}{_base_path(access.base_url)}{ENDPOINT_PATH}"

    if session_factory is not None:
        session = session_factory(access)
        owns_session = False
    else:
        import requests  # noqa: PLC0415

        session = requests.Session()
        session.auth = (access.username, access.password)
        owns_session = True

    page_summaries: Dict[int, Dict[str, Any]] = {}
    page_identities: Dict[int, List[str]] = {}
    try:
        for page in plan.pages:
            outcome = fetch_page(
                session=session,
                origin=origin,
                url=url,
                plan=plan,
                page=page,
                budget=budget,
                secret_values=secret_values,
                monotonic=monotonic,
            )
            summarized = summarize_page(outcome, salt=salt)
            page_summaries[page] = summarized["summary"]
            page_identities[page] = summarized["identities"]
            bundle.write_json(f"page_{page}_summary.json", summarized["summary"])
    finally:
        if owns_session:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    first_page, second_page = plan.pages[0], plan.pages[1]
    comparison = compare_pages(
        first_page=first_page,
        second_page=second_page,
        first=page_summaries[first_page],
        second=page_summaries[second_page],
        first_identities=page_identities[first_page],
        second_identities=page_identities[second_page],
    )
    bundle.write_json(CROSS_PAGE_FILENAME, comparison)

    return {
        "result_classification": _VERDICT_TO_RESULT[comparison["verdict"]],
        "request_count": budget.used,
        "sanitized_endpoint": sanitized_endpoint,
        "pages": page_summaries,
        "cross_page": comparison,
        "blocked": None,
    }


def execute(
    plan: DiagnosticPlan,
    *,
    session_factory: Any = None,
    access_loader: Any = None,
    monotonic: Any = None,
) -> Dict[str, Any]:
    """Run the diagnostic and always leave a complete evidence bundle behind."""
    bundle = EvidenceBundle(plan.output_dir)
    bundle.prepare()

    budget = RequestBudget(max_requests=plan.max_requests)
    executed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _failed(classification: str, exc: Any) -> Dict[str, Any]:
        return {
            "result_classification": classification,
            "request_count": budget.used,
            "sanitized_endpoint": f"<provider>{ENDPOINT_PATH}",
            "pages": {},
            "cross_page": None,
            "blocked": {"code": exc.code, "message": _redact(exc, []), "context": exc.context},
        }

    outcome: Dict[str, Any]
    try:
        if not plan.allow_live_request:
            outcome = run_dry_run(plan, bundle)
        else:
            outcome = run_live(
                plan,
                bundle,
                budget=budget,
                session_factory=session_factory,
                access_loader=access_loader,
                monotonic=monotonic,
            )
    except DiagnosticSafetyError as exc:
        outcome = _failed(RESULT_SAFETY_BLOCKED, exc)
    except DiagnosticTransportError as exc:
        outcome = _failed(RESULT_TRANSPORT_FAILURE, exc)
    except DiagnosticResponseError as exc:
        outcome = _failed(RESULT_RESPONSE_MALFORMED, exc)

    summary = {
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "repository_commit": _repository_commit(),
        "execution_timestamp_utc": executed_at,
        "client_code": plan.client_code,
        "sanitized_endpoint": outcome["sanitized_endpoint"],
        "method": HTTP_METHOD,
        "window_start_ts": _provider_dt_str(plan.start_ts),
        "window_end_ts": _provider_dt_str(plan.end_ts),
        "wire_start_timestamp": trips_wire_window(plan.start_ts, plan.end_ts)[0],
        "wire_end_timestamp": trips_wire_window(plan.start_ts, plan.end_ts)[1],
        "window_seconds": plan.window_seconds,
        "requested_pages": list(plan.pages),
        "limit": plan.limit,
        "incl_private": plan.include_private,
        "safety_bounds": plan.safety_bounds(),
        "live_execution_enabled": plan.allow_live_request,
        "request_count": outcome["request_count"],
        "result_classification": outcome["result_classification"],
        "cross_page_verdict": (outcome["cross_page"] or {}).get("verdict"),
        "blocked": outcome["blocked"],
        "raw_payload_retained": False,
        "credentials_retained": False,
    }
    bundle.write_json(SUMMARY_FILENAME, summary)
    bundle.finalize(header=summary)

    return {
        "summary": summary,
        "cross_page": outcome["cross_page"],
        "pages": outcome["pages"],
        "evidence_dir": str(plan.output_dir),
        "evidence_files": sorted(bundle.filenames),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Bounded, GET-only Telematics /trips pagination diagnostic (dry-run by default).",
    )
    parser.add_argument("--client-code", required=True, help=f"one of: {', '.join(ALLOWED_CLIENT_CODES)}")
    parser.add_argument("--start-timestamp", required=True, help="'YYYY-MM-DD HH:MM:SS' UTC or ISO-8601")
    parser.add_argument("--end-timestamp", required=True, help="'YYYY-MM-DD HH:MM:SS' UTC or ISO-8601")
    parser.add_argument("--pages", required=True, help="comma-separated page numbers, at most two, e.g. 1,2")
    parser.add_argument("--limit", required=True, type=int, help=f"{MIN_LIMIT}..{MAX_LIMIT}")
    parser.add_argument("--output-dir", required=True, help="evidence directory outside the repository")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)
    parser.add_argument(
        "--include-private",
        default="true",
        help="true|false; production trips sync uses true",
    )
    parser.add_argument(
        "--expected-base-url",
        default=None,
        help="if set, the configured provider base URL must match exactly",
    )
    parser.add_argument(
        "--allow-live-request",
        action="store_true",
        help="required to open any network connection; without it the tool stops at the plan",
    )
    parser.add_argument(
        "--allow-timeout-retry",
        action="store_true",
        help=f"permit at most {MAX_TIMEOUT_RETRIES} retry, for transport timeouts only",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        plan = build_plan(args)
    except DiagnosticSafetyError as exc:
        print(
            json.dumps(
                {
                    "result_classification": RESULT_SAFETY_BLOCKED,
                    "code": exc.code,
                    "message": _redact(exc, []),
                    "context": exc.context,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return EXIT_SAFETY_BLOCKED

    try:
        result = execute(plan)
    except DiagnosticSafetyError as exc:
        print(
            json.dumps(
                {
                    "result_classification": RESULT_SAFETY_BLOCKED,
                    "code": exc.code,
                    "message": _redact(exc, []),
                    "context": exc.context,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return EXIT_SAFETY_BLOCKED

    print(
        json.dumps(
            {
                "result_classification": result["summary"]["result_classification"],
                "evidence_dir": result["evidence_dir"],
                "evidence_files": result["evidence_files"],
                "request_count": result["summary"]["request_count"],
                "live_execution_enabled": result["summary"]["live_execution_enabled"],
                "cross_page_verdict": result["summary"]["cross_page_verdict"],
                "blocked": result["summary"]["blocked"],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return _EXIT_BY_RESULT[result["summary"]["result_classification"]]


if __name__ == "__main__":
    raise SystemExit(main())
