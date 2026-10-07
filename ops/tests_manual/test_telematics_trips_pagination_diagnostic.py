#!/usr/bin/env python3
"""Isolated tests for `ops/diagnose_telematics_trips_pagination.py`.

No network, no live Telematics API, no production secrets, no database. HTTP is
supplied by a fake session; the live provider account is supplied by a fake
loader. Run with:

    PYTHONPATH="$PWD" python3 ops/tests_manual/test_telematics_trips_pagination_diagnostic.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import diagnose_telematics_trips_pagination as diag  # noqa: E402


FAKE_USERNAME = "fake-provider-username-9d1f"
FAKE_PASSWORD = "fake-provider-password-4c7a"
FAKE_BASE_URL = "https://fleetapi.example.invalid/rest"
FAKE_CLIENT_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"

# Distinctive personal-looking values used to prove they never reach evidence.
SENSITIVE_REGISTRATION = "ZZSENSITIVEPLATE1"
SENSITIVE_DRIVER = "Sensitive Driver Name"
SENSITIVE_ADDRESS = "12 Sensitive Street, Sensitive City"
SENSITIVE_LATITUDE = 52.1234567
SENSITIVE_LONGITUDE = 21.7654321
SENSITIVE_TIMESTAMP = "2026-07-31 06:17:41"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.status_code = status_code
        self.headers = dict(headers or {"Content-Type": "application/json"})
        if body is not None:
            self._body = body
        else:
            self._body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset:offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Records every call; yields queued responses or raises queued exceptions."""

    def __init__(self, script: Sequence[Any]):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []
        self.auth: Any = None
        self.closed = False

    def request(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(dict(kwargs))
        if not self.script:
            raise AssertionError("fake session called more times than the test scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def fake_access(_client_code: str) -> diag.ProviderAccess:
    return diag.ProviderAccess(
        client_id=FAKE_CLIENT_ID,
        client_code="DELTA00001",
        base_url=FAKE_BASE_URL,
        username=FAKE_USERNAME,
        password=FAKE_PASSWORD,
    )


def trip_row(trip_id: int, **overrides: Any) -> Dict[str, Any]:
    row = {
        "trip_id": trip_id,
        "vehicle_id": 900000 + trip_id,
        "registration": SENSITIVE_REGISTRATION,
        "driver_name": SENSITIVE_DRIVER,
        "start_address": SENSITIVE_ADDRESS,
        "start_latitude": SENSITIVE_LATITUDE,
        "start_longitude": SENSITIVE_LONGITUDE,
        "start_timestamp": SENSITIVE_TIMESTAMP,
        "end_timestamp": SENSITIVE_TIMESTAMP,
    }
    row.update(overrides)
    return row


def meta(current_page: Any = 1, last_page: Any = 43, total: Any = 429, per_page: Any = 1000) -> Dict[str, Any]:
    return {
        "from": 1,
        "to": 1000,
        "current_page": current_page,
        "per_page": per_page,
        "last_page": last_page,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------

class TempEvidence:
    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="telematics-pagination-test-"))
        # `execute` refuses a directory that already holds a bundle.
        self.dir = self.path / "evidence"

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def make_plan(
    *,
    output_dir: Path,
    pages: str = "1,2",
    limit: str = "1000",
    start: str = "2026-07-31 06:00:00",
    end: str = "2026-07-31 07:00:00",
    client_code: str = "DELTA00001",
    live: bool = False,
    timeout_retry: bool = False,
    extra: Sequence[str] = (),
) -> diag.DiagnosticPlan:
    argv = [
        "--client-code", client_code,
        "--start-timestamp", start,
        "--end-timestamp", end,
        "--pages", pages,
        "--limit", limit,
        "--output-dir", str(output_dir),
    ]
    if live:
        argv.append("--allow-live-request")
    if timeout_retry:
        argv.append("--allow-timeout-retry")
    argv.extend(extra)
    return diag.build_plan(diag.build_parser().parse_args(argv))


def run_live_case(
    *,
    evidence_dir: Path,
    script: Sequence[Any],
    pages: str = "1,2",
    timeout_retry: bool = False,
) -> tuple[Dict[str, Any], FakeSession]:
    plan = make_plan(output_dir=evidence_dir, pages=pages, live=True, timeout_retry=timeout_retry)
    session = FakeSession(script)
    result = diag.execute(
        plan,
        session_factory=lambda _access: session,
        access_loader=fake_access,
        monotonic=lambda: 0.0,
    )
    return result, session


def evidence_blobs(directory: Path) -> Dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file()}


def expect_safety(fn, code: str) -> diag.DiagnosticSafetyError:
    try:
        fn()
    except diag.DiagnosticSafetyError as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}"
        return exc
    raise AssertionError(f"expected DiagnosticSafetyError {code}, nothing was raised")


# ---------------------------------------------------------------------------
# 1. Dry-run opens no socket
# ---------------------------------------------------------------------------

def test_dry_run_opens_no_socket() -> None:
    evidence = TempEvidence()
    try:
        def forbidden(*_args: Any, **_kwargs: Any):
            raise AssertionError("dry-run attempted to open a network connection")

        argv = [
            "--client-code", "DELTA00001",
            "--start-timestamp", "2026-07-31 06:00:00",
            "--end-timestamp", "2026-07-31 07:00:00",
            "--pages", "1,2",
            "--limit", "1000",
            "--output-dir", str(evidence.dir),
        ]
        stdout = io.StringIO()
        with patch.object(socket, "socket", forbidden), \
                patch.object(socket, "create_connection", forbidden), \
                patch.object(diag, "load_provider_access", forbidden), \
                contextlib.redirect_stdout(stdout):
            exit_code = diag.main(argv)

        assert exit_code == diag.EXIT_OK, exit_code
        printed = json.loads(stdout.getvalue())
        assert printed["result_classification"] == diag.RESULT_DRY_RUN_READY
        assert printed["request_count"] == 0
        assert printed["live_execution_enabled"] is False

        summary = json.loads((evidence.dir / diag.SUMMARY_FILENAME).read_text())
        assert summary["result_classification"] == diag.RESULT_DRY_RUN_READY
        assert summary["live_execution_enabled"] is False
        assert summary["request_count"] == 0

        plan_doc = json.loads((evidence.dir / diag.REQUEST_PLAN_FILENAME).read_text())
        assert plan_doc["planned_requests"][0]["method"] == "GET"
        assert plan_doc["planned_requests"][0]["endpoint_path"] == "/trips"
        assert [entry["params"]["page"] for entry in plan_doc["planned_requests"]] == [1, 2]
        print("PASS  1 dry-run opens no socket and always reports DRY_RUN_READY")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 2. Explicit live flag is required
# ---------------------------------------------------------------------------

def test_live_flag_is_required() -> None:
    evidence = TempEvidence()
    try:
        plan = make_plan(output_dir=evidence.dir)
        assert plan.allow_live_request is False

        def forbidden_session(*_args: Any, **_kwargs: Any):
            raise AssertionError("session was constructed without --allow-live-request")

        def forbidden_loader(*_args: Any, **_kwargs: Any):
            raise AssertionError("provider credentials were resolved without --allow-live-request")

        result = diag.execute(
            plan, session_factory=forbidden_session, access_loader=forbidden_loader
        )
        assert result["summary"]["result_classification"] == diag.RESULT_DRY_RUN_READY

        live_plan = make_plan(output_dir=evidence.path / "live", live=True)
        assert live_plan.allow_live_request is True
        assert live_plan.max_requests == 2
        print("PASS  2 network work happens only behind --allow-live-request")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 3. Client allowlist
# ---------------------------------------------------------------------------

def test_only_delta00001_is_allowed() -> None:
    evidence = TempEvidence()
    try:
        assert diag.ALLOWED_CLIENT_CODES == ("DELTA00001",)
        for rejected in ("ALPHA00001", "FOXTROT00001", "delta00001", "", "DELTA00002"):
            exc = expect_safety(
                lambda code=rejected: make_plan(output_dir=evidence.dir, client_code=code),
                "CLIENT_NOT_ALLOWED",
            )
            assert exc.context["allowed_client_codes"] == ["DELTA00001"]
        make_plan(output_dir=evidence.dir, client_code="DELTA00001")
        print("PASS  3 only DELTA00001 is accepted; no silent fallback client")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 4. Window bounds
# ---------------------------------------------------------------------------

def test_window_bounds() -> None:
    evidence = TempEvidence()
    try:
        expect_safety(
            lambda: make_plan(
                output_dir=evidence.dir,
                start="2026-07-31 06:00:00",
                end="2026-07-31 07:00:01",
            ),
            "WINDOW_TOO_LONG",
        )
        expect_safety(
            lambda: make_plan(
                output_dir=evidence.dir,
                start="2026-07-31 06:00:00",
                end="2026-07-31 06:00:00",
            ),
            "WINDOW_NOT_POSITIVE",
        )
        expect_safety(
            lambda: make_plan(
                output_dir=evidence.dir,
                start="2026-07-31 07:00:00",
                end="2026-07-31 06:00:00",
            ),
            "WINDOW_NOT_POSITIVE",
        )
        exactly_one_hour = make_plan(
            output_dir=evidence.dir,
            start="2026-07-31 06:00:00",
            end="2026-07-31 07:00:00",
        )
        assert exactly_one_hour.window_seconds == diag.MAX_WINDOW_SECONDS
        print("PASS  4 window must be positive and at most one hour")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 5. Page bounds
# ---------------------------------------------------------------------------

def test_page_bounds() -> None:
    evidence = TempEvidence()
    try:
        expect_safety(lambda: make_plan(output_dir=evidence.dir, pages="1,2,3"), "PAGES_EXCEED_LIMIT")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, pages="1,2,3,4"), "PAGES_EXCEED_LIMIT")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, pages="1,1"), "PAGES_DUPLICATE")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, pages="0,1"), "PAGES_OUT_OF_RANGE")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, pages=""), "PAGES_INVALID")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, pages="a,b"), "PAGES_INVALID")
        # A live execution needs exactly two pages for the comparison to mean anything.
        expect_safety(
            lambda: make_plan(output_dir=evidence.dir, pages="1", live=True),
            "LIVE_REQUIRES_TWO_PAGES",
        )
        assert make_plan(output_dir=evidence.dir, pages="1,2").pages == (1, 2)
        assert make_plan(output_dir=evidence.dir, pages="3,7").pages == (3, 7)
        print("PASS  5 at most two explicit pages; no default or inferred page")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 6. Limit bounds
# ---------------------------------------------------------------------------

def test_limit_bounds() -> None:
    evidence = TempEvidence()
    try:
        expect_safety(lambda: make_plan(output_dir=evidence.dir, limit="1001"), "LIMIT_OUT_OF_RANGE")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, limit="5000"), "LIMIT_OUT_OF_RANGE")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, limit="0"), "LIMIT_OUT_OF_RANGE")
        expect_safety(lambda: make_plan(output_dir=evidence.dir, limit="-1"), "LIMIT_OUT_OF_RANGE")
        assert make_plan(output_dir=evidence.dir, limit="1000").limit == 1000
        assert make_plan(output_dir=evidence.dir, limit="1").limit == 1
        print("PASS  6 limit is constrained to 1..1000")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 7. Redirects
# ---------------------------------------------------------------------------

def test_cross_host_redirect_is_rejected() -> None:
    evidence = TempEvidence()
    try:
        redirect = FakeResponse(
            status_code=302,
            headers={"Location": "https://attacker.example.invalid/trips", "Content-Type": "text/html"},
            payload={},
        )
        result, session = run_live_case(evidence_dir=evidence.dir, script=[redirect])
        summary = result["summary"]
        assert summary["result_classification"] == diag.RESULT_SAFETY_BLOCKED
        assert summary["blocked"]["code"] == "REDIRECT_CROSS_HOST"
        history = summary["blocked"]["context"]["redirect_history"]
        assert history[0]["host"] == "attacker.example.invalid"
        assert history[0]["followed"] is False
        # The redirect was never followed: exactly one request was issued.
        assert len(session.calls) == 1
        assert summary["request_count"] == 1
        assert all(call["allow_redirects"] is False for call in session.calls)

        same_host = FakeResponse(
            status_code=301,
            headers={"Location": "https://fleetapi.example.invalid/rest/trips2"},
        )
        result2, session2 = run_live_case(evidence_dir=evidence.path / "same-host", script=[same_host])
        assert result2["summary"]["blocked"]["code"] == "REDIRECT_NOT_FOLLOWED"
        assert len(session2.calls) == 1
        print("PASS  7 redirects are never followed; a cross-host Location is a hard stop")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 8. Credentials never printed or persisted
# ---------------------------------------------------------------------------

def test_credentials_are_never_printed_or_persisted() -> None:
    evidence = TempEvidence()
    try:
        script = [
            FakeResponse(payload={"data": [trip_row(i) for i in range(1, 6)], "meta": meta()}),
            FakeResponse(payload={"data": [trip_row(i) for i in range(1, 6)], "meta": meta()}),
        ]
        plan = make_plan(output_dir=evidence.dir, live=True)
        session = FakeSession(script)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = diag.execute(
                plan,
                session_factory=lambda _access: session,
                access_loader=fake_access,
                monotonic=lambda: 0.0,
            )
            print(json.dumps(result["summary"], default=str))

        forbidden = [
            FAKE_USERNAME,
            FAKE_PASSWORD,
            "Authorization",
            "authorization",
            "Basic ",
            "Set-Cookie",
            "set-cookie",
        ]
        printed = stdout.getvalue()
        for needle in forbidden:
            assert needle not in printed, f"{needle!r} leaked to stdout"

        for name, blob in evidence_blobs(evidence.dir).items():
            text = blob.decode("utf-8", errors="replace")
            for needle in forbidden:
                assert needle not in text, f"{needle!r} leaked into {name}"

        # The evidence keeps only the sanitized origin, never a credential URL.
        summary = json.loads((evidence.dir / diag.SUMMARY_FILENAME).read_text())
        assert summary["sanitized_endpoint"] == "https://fleetapi.example.invalid/rest/trips"
        assert summary["credentials_retained"] is False

        # A credential-bearing base URL is still reduced to a bare origin.
        assert diag._sanitize_origin("https://user:pass@host.example.invalid/rest") == \
            "https://host.example.invalid"
        assert "pass" not in diag._redact("https://user:pass@host/rest", [FAKE_PASSWORD])
        assert diag._redact(f"boom {FAKE_PASSWORD}", [FAKE_PASSWORD]) == "boom [redacted]"
        print("PASS  8 credentials never reach stdout or evidence")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 9. Identical pages -> page parameter ignored
# ---------------------------------------------------------------------------

def test_identical_pages_classify_as_page_parameter_ignored() -> None:
    evidence = TempEvidence()
    try:
        rows = [trip_row(i) for i in range(1, 51)]
        script = [
            FakeResponse(payload={"data": rows, "meta": meta(current_page=1)}),
            FakeResponse(payload={"data": rows, "meta": meta(current_page=1)}),
        ]
        result, session = run_live_case(evidence_dir=evidence.dir, script=script)
        summary = result["summary"]
        comparison = result["cross_page"]

        assert summary["result_classification"] == diag.RESULT_PAGE_PARAMETER_IGNORED
        assert comparison["verdict"] == diag.VERDICT_IDENTICAL_REPEAT
        assert comparison["exact_page_payload_equality"] is True
        assert comparison["ordered_identity_sequence_equality"] is True
        assert comparison["unordered_identity_set_equality"] is True
        assert comparison["intersection_count"] == 50
        assert comparison["intersection_ratio_of_first_page"] == 1.0
        assert comparison["intersection_ratio_of_second_page"] == 1.0
        assert comparison["first_page_only_count"] == 0
        assert comparison["second_page_only_count"] == 0
        assert comparison["identity_source_first_page"] == diag.IDENTITY_SOURCE_PROVIDER_TRIP_ID

        # The requested page really was 2 on the second call.
        assert [call["params"]["page"] for call in session.calls] == [1, 2]
        assert [call["params"]["limit"] for call in session.calls] == [1000, 1000]
        assert all(call["method"] == "GET" for call in session.calls)
        print("PASS  9 identical pages -> TELEMATICS_TRIPS_PAGE_PARAMETER_IGNORED")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 10. Distinct pages -> metadata broken
# ---------------------------------------------------------------------------

def test_distinct_pages_classify_as_metadata_broken() -> None:
    evidence = TempEvidence()
    try:
        script = [
            FakeResponse(payload={
                "data": [trip_row(i) for i in range(1, 21)],
                "meta": meta(current_page=1),
            }),
            FakeResponse(payload={
                "data": [trip_row(i) for i in range(21, 41)],
                "meta": meta(current_page=1),
            }),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)
        comparison = result["cross_page"]

        assert result["summary"]["result_classification"] == diag.RESULT_PAGES_DISTINCT
        assert comparison["verdict"] == diag.VERDICT_DISTINCT_CONTINUATION
        assert comparison["intersection_count"] == 0
        assert comparison["first_page_only_count"] == 20
        assert comparison["second_page_only_count"] == 20
        assert comparison["exact_page_payload_equality"] is False

        # Reordering the same records must NOT read as a distinct continuation,
        # even though the raw JSON hash differs.
        rows = [trip_row(i) for i in range(1, 21)]
        shuffled = list(reversed(rows))
        reorder = [
            FakeResponse(payload={"data": rows, "meta": meta(current_page=1)}),
            FakeResponse(payload={"data": shuffled, "meta": meta(current_page=1)}),
        ]
        result2, _ = run_live_case(evidence_dir=evidence.path / "reordered", script=reorder)
        comparison2 = result2["cross_page"]
        assert comparison2["exact_page_payload_equality"] is False
        assert comparison2["ordered_identity_sequence_equality"] is False
        assert comparison2["unordered_identity_set_equality"] is True
        assert comparison2["verdict"] == diag.VERDICT_IDENTICAL_REPEAT
        assert result2["summary"]["result_classification"] == diag.RESULT_PAGE_PARAMETER_IGNORED
        print("PASS 10 distinct pages -> METADATA_BROKEN; reordering alone never counts as distinct")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 11. Partial overlap
# ---------------------------------------------------------------------------

def test_partial_overlap_is_detected() -> None:
    evidence = TempEvidence()
    try:
        script = [
            FakeResponse(payload={
                "data": [trip_row(i) for i in range(1, 21)],
                "meta": meta(current_page=1),
            }),
            FakeResponse(payload={
                "data": [trip_row(i) for i in range(16, 36)],
                "meta": meta(current_page=1),
            }),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)
        comparison = result["cross_page"]

        assert result["summary"]["result_classification"] == diag.RESULT_PAGES_PARTIALLY_OVERLAP
        assert comparison["verdict"] == diag.VERDICT_PARTIAL_OVERLAP
        assert comparison["intersection_count"] == 5
        assert comparison["intersection_ratio_of_first_page"] == 0.25
        assert comparison["intersection_ratio_of_second_page"] == 0.25
        assert comparison["first_page_only_count"] == 15
        assert comparison["second_page_only_count"] == 15
        print("PASS 11 partial overlap is detected with intersection ratios")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 12. Empty page 2
# ---------------------------------------------------------------------------

def test_empty_second_page_is_detected() -> None:
    evidence = TempEvidence()
    try:
        script = [
            FakeResponse(payload={
                "data": [trip_row(i) for i in range(1, 21)],
                "meta": meta(current_page=1),
            }),
            FakeResponse(payload={"data": [], "meta": meta(current_page=1)}),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)
        comparison = result["cross_page"]

        assert result["summary"]["result_classification"] == diag.RESULT_PAGE_2_EMPTY
        assert comparison["verdict"] == diag.VERDICT_EMPTY_PAGE
        assert comparison["second_page_row_count"] == 0
        assert comparison["intersection_count"] == 0
        assert comparison["intersection_ratio_of_second_page"] is None

        page2 = json.loads((evidence.dir / "page_2_summary.json").read_text())
        assert page2["data_identity"]["row_count"] == 0
        assert page2["data_identity"]["first_identity"] is None
        assert page2["data_identity"]["last_identity"] is None
        print("PASS 12 an empty page 2 is detected and classified")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 13. Pagination field types
# ---------------------------------------------------------------------------

def test_string_and_integer_pagination_fields_record_types() -> None:
    evidence = TempEvidence()
    try:
        string_meta = {
            "from": "1",
            "to": "1000",
            "current_page": "1",
            "per_page": "10",
            "last_page": 43,
            "total": 429,
        }
        script = [
            FakeResponse(payload={"data": [trip_row(1)], "meta": string_meta}),
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta(current_page=1)}),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)
        assert result["summary"]["result_classification"] == diag.RESULT_PAGE_PARAMETER_IGNORED

        page1 = json.loads((evidence.dir / "page_1_summary.json").read_text())
        fields = page1["pagination_meta"]["fields"]
        assert fields["current_page"] == {"present": True, "type": "str", "value": "1"}
        assert fields["per_page"] == {"present": True, "type": "str", "value": "10"}
        assert fields["last_page"] == {"present": True, "type": "int", "value": 43}
        assert fields["total"] == {"present": True, "type": "int", "value": 429}

        page2 = json.loads((evidence.dir / "page_2_summary.json").read_text())
        assert page2["pagination_meta"]["fields"]["current_page"]["type"] == "int"
        assert page2["pagination_meta"]["fields"]["current_page"]["value"] == 1
        print("PASS 13 pagination fields are recorded with their JSON types")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 14. Missing metadata
# ---------------------------------------------------------------------------

def test_missing_metadata_is_recorded_safely() -> None:
    evidence = TempEvidence()
    try:
        script = [
            FakeResponse(payload={"data": [trip_row(1), trip_row(2)]}),
            FakeResponse(payload={"data": [trip_row(1), trip_row(2)], "meta": None}),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)
        assert result["summary"]["result_classification"] == diag.RESULT_PAGE_PARAMETER_IGNORED

        page1 = json.loads((evidence.dir / "page_1_summary.json").read_text())
        pagination = page1["pagination_meta"]
        assert pagination["meta_present"] is False
        assert pagination["meta_type"] == "absent"
        assert pagination["meta_is_object"] is False
        for name in diag.PAGINATION_META_FIELDS:
            assert pagination["fields"][name] == {"present": False, "type": "absent", "value": None}

        page2 = json.loads((evidence.dir / "page_2_summary.json").read_text())
        assert page2["pagination_meta"]["meta_present"] is True
        assert page2["pagination_meta"]["meta_type"] == "null"
        assert page2["pagination_meta"]["meta_is_object"] is False
        print("PASS 14 absent and null metadata are recorded without crashing")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 15. Nested metadata
# ---------------------------------------------------------------------------

def test_nested_metadata_is_detected() -> None:
    evidence = TempEvidence()
    try:
        nested = {
            "pagination": {"current_page": 1, "last_page": 43},
            "links": ["first", "last"],
            "total": 429,
        }
        script = [
            FakeResponse(payload={"data": [trip_row(1)], "meta": nested}),
            FakeResponse(payload={"data": [trip_row(1)], "meta": nested}),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)
        page1 = json.loads((evidence.dir / "page_1_summary.json").read_text())
        pagination = page1["pagination_meta"]

        assert pagination["meta_nested_unexpectedly"] is True
        assert pagination["nested_meta_keys"] == ["links", "pagination"]
        assert pagination["other_meta_keys"] == ["links", "pagination"]
        assert pagination["other_meta_key_types"] == {"links": "list", "pagination": "object"}
        assert pagination["fields"]["current_page"]["present"] is False
        assert pagination["fields"]["total"] == {"present": True, "type": "int", "value": 429}
        # The nested structure itself is never persisted.
        blob = (evidence.dir / "page_1_summary.json").read_text()
        assert "\"first\"" not in blob and "'first'" not in blob

        # A non-object `meta` is also flagged as an unexpected shape.
        script2 = [
            FakeResponse(payload={"data": [trip_row(1)], "meta": [1, 2, 3]}),
            FakeResponse(payload={"data": [trip_row(1)], "meta": [1, 2, 3]}),
        ]
        result2, _ = run_live_case(evidence_dir=evidence.path / "listmeta", script=script2)
        page1b = json.loads((evidence.path / "listmeta" / "page_1_summary.json").read_text())
        assert page1b["pagination_meta"]["meta_type"] == "list"
        assert page1b["pagination_meta"]["meta_nested_unexpectedly"] is True
        assert result2["summary"]["result_classification"] == diag.RESULT_PAGE_PARAMETER_IGNORED
        print("PASS 15 unexpectedly nested or non-object metadata is detected")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 16 + 17. Raw payload and sensitive fields never reach evidence
# ---------------------------------------------------------------------------

def test_raw_trip_data_and_sensitive_fields_stay_out_of_evidence() -> None:
    evidence = TempEvidence()
    try:
        rows = [trip_row(i) for i in range(1, 31)]
        script = [
            FakeResponse(payload={"data": rows, "meta": meta(current_page=1)}),
            FakeResponse(payload={"data": rows[10:], "meta": meta(current_page=1)}),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)
        assert result["summary"]["result_classification"] == diag.RESULT_PAGES_PARTIALLY_OVERLAP

        forbidden = [
            SENSITIVE_REGISTRATION,
            SENSITIVE_DRIVER,
            SENSITIVE_ADDRESS,
            str(SENSITIVE_LATITUDE),
            str(SENSITIVE_LONGITUDE),
            SENSITIVE_TIMESTAMP,
            "driver_name",
            "start_latitude",
            "start_longitude",
            "start_address",
            "registration",
            "\"trip_id\"",
            "900001",
        ]
        blobs = evidence_blobs(evidence.dir)
        assert set(blobs) >= {
            diag.REQUEST_PLAN_FILENAME,
            "page_1_summary.json",
            "page_2_summary.json",
            diag.CROSS_PAGE_FILENAME,
            diag.SUMMARY_FILENAME,
            diag.MANIFEST_FILENAME,
            diag.SHA256SUMS_FILENAME,
        }
        for name, blob in blobs.items():
            text = blob.decode("utf-8", errors="replace")
            for needle in forbidden:
                assert needle not in text, f"{needle!r} leaked into {name}"

        # Raw trip ids are unrecoverable: identities are salted HMACs, not
        # digests an observer can reproduce from a guessed trip_id.
        page1 = json.loads((evidence.dir / "page_1_summary.json").read_text())
        first_identity = page1["data_identity"]["first_identity"]
        assert len(first_identity) == 16
        naive = hashlib.sha256(b"provider_trip_id\x1f1").hexdigest()[:16]
        assert first_identity != naive
        assert result["summary"]["raw_payload_retained"] is False

        # Two runs over the same data must not produce the same identity digests.
        result_b, _ = run_live_case(
            evidence_dir=evidence.path / "second-run",
            script=[
                FakeResponse(payload={"data": rows, "meta": meta(current_page=1)}),
                FakeResponse(payload={"data": rows[10:], "meta": meta(current_page=1)}),
            ],
        )
        page1_b = json.loads((evidence.path / "second-run" / "page_1_summary.json").read_text())
        assert page1_b["data_identity"]["first_identity"] != first_identity
        # ...while staying internally comparable within each run.
        assert result_b["cross_page"]["intersection_count"] == \
            result["cross_page"]["intersection_count"] == 20
        print("PASS 16/17 no raw rows, no personal fields, no reversible identities in evidence")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 18. Manifest and checksums
# ---------------------------------------------------------------------------

def test_manifest_and_checksums_validate() -> None:
    evidence = TempEvidence()
    try:
        script = [
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta(current_page=1)}),
            FakeResponse(payload={"data": [trip_row(2)], "meta": meta(current_page=1)}),
        ]
        result, _session = run_live_case(evidence_dir=evidence.dir, script=script)

        assert diag.verify_bundle(evidence.dir) == []

        manifest = (evidence.dir / diag.MANIFEST_FILENAME).read_text()
        assert diag.TOOL_VERSION in manifest
        assert "DELTA00001" in manifest
        assert result["summary"]["result_classification"] in manifest
        assert "repository_commit:" in manifest
        assert "request_count: 2" in manifest

        checksums = (evidence.dir / diag.SHA256SUMS_FILENAME).read_text().splitlines()
        listed = {line.split("  ", 1)[1] for line in checksums if line.strip()}
        assert diag.SHA256SUMS_FILENAME not in listed
        assert {"page_1_summary.json", "page_2_summary.json", diag.MANIFEST_FILENAME} <= listed

        # Tampering is detected.
        (evidence.dir / "page_1_summary.json").write_text("{\"tampered\": true}\n")
        assert diag.verify_bundle(evidence.dir) == ["page_1_summary.json"]

        # Files are operator-private.
        for path in evidence.dir.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600, path
        assert evidence.dir.stat().st_mode & 0o777 == 0o700

        # A directory that already holds a bundle is refused.
        plan = make_plan(output_dir=evidence.dir)
        expect_safety(lambda: diag.execute(plan), "OUTPUT_DIR_NOT_EMPTY")
        print("PASS 18 manifest and SHA256SUMS validate, detect tampering, and stay 0600")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 19. Request budget
# ---------------------------------------------------------------------------

def test_request_budget_cannot_be_exceeded() -> None:
    evidence = TempEvidence()
    try:
        plan = make_plan(output_dir=evidence.dir, live=True)
        assert plan.max_requests == 2

        script = [
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta(current_page=1)}),
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta(current_page=1)}),
        ]
        session = FakeSession(script)
        result = diag.execute(
            plan,
            session_factory=lambda _access: session,
            access_loader=fake_access,
            monotonic=lambda: 0.0,
        )
        assert len(session.calls) == 2
        assert result["summary"]["request_count"] == 2
        assert result["summary"]["safety_bounds"]["effective_max_requests"] == 2
        assert result["summary"]["safety_bounds"]["automatic_page_loop"] is False

        # A third request is refused before the socket is touched.
        budget = diag.RequestBudget(max_requests=2)
        budget.consume(purpose="page_1")
        budget.consume(purpose="page_2")
        exhausted = FakeSession([FakeResponse(payload={"data": []})])
        expect_safety(
            lambda: diag.fetch_page(
                session=exhausted,
                origin="https://fleetapi.example.invalid",
                url="https://fleetapi.example.invalid/rest/trips",
                plan=plan,
                page=3,
                budget=budget,
                secret_values=[FAKE_PASSWORD],
                monotonic=lambda: 0.0,
            ),
            "REQUEST_BUDGET_EXCEEDED",
        )
        assert exhausted.calls == []

        # With the timeout retry enabled the ceiling is three, never more.
        retry_plan = make_plan(output_dir=evidence.path / "retry", live=True, timeout_retry=True)
        assert retry_plan.max_requests == 3
        assert retry_plan.safety_bounds()["max_timeout_retries"] == 1

        # The response byte cap is enforced while streaming.
        big_plan = make_plan(
            output_dir=evidence.path / "big",
            live=True,
            extra=["--max-response-bytes", "2048"],
        )
        oversized = FakeResponse(body=b"x" * 8192)
        oversized_session = FakeSession([oversized])
        blocked = diag.execute(
            big_plan,
            session_factory=lambda _access: oversized_session,
            access_loader=fake_access,
            monotonic=lambda: 0.0,
        )
        assert blocked["summary"]["blocked"]["code"] == "RESPONSE_TOO_LARGE"
        assert blocked["summary"]["result_classification"] == diag.RESULT_SAFETY_BLOCKED
        print("PASS 19 the request budget and response byte cap are enforced")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# 20. Timeout retry
# ---------------------------------------------------------------------------

def test_timeout_retry_happens_at_most_once() -> None:
    evidence = TempEvidence()
    try:
        # Disabled by default: one attempt, then a transport failure.
        no_retry_session = FakeSession([requests.exceptions.Timeout("timed out")])
        plan = make_plan(output_dir=evidence.dir, live=True)
        result = diag.execute(
            plan,
            session_factory=lambda _access: no_retry_session,
            access_loader=fake_access,
            monotonic=lambda: 0.0,
        )
        assert len(no_retry_session.calls) == 1
        assert result["summary"]["result_classification"] == diag.RESULT_TRANSPORT_FAILURE
        assert result["summary"]["blocked"]["code"] == "TRANSPORT_TIMEOUT"
        assert result["summary"]["blocked"]["context"]["retry_count"] == 0
        assert result["summary"]["safety_bounds"]["max_timeout_retries"] == 0

        # Enabled: exactly one retry, then give up.
        retry_session = FakeSession([
            requests.exceptions.Timeout("timed out"),
            requests.exceptions.Timeout("timed out again"),
        ])
        retry_plan = make_plan(output_dir=evidence.path / "retry-twice", live=True, timeout_retry=True)
        retry_result = diag.execute(
            retry_plan,
            session_factory=lambda _access: retry_session,
            access_loader=fake_access,
            monotonic=lambda: 0.0,
        )
        assert len(retry_session.calls) == 2, retry_session.calls
        assert retry_result["summary"]["blocked"]["code"] == "TRANSPORT_TIMEOUT"
        assert retry_result["summary"]["blocked"]["context"]["retry_count"] == 1
        assert retry_result["summary"]["request_count"] == 2

        # A retry that succeeds is recorded on the page summary.
        recovering = FakeSession([
            requests.exceptions.Timeout("timed out"),
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta(current_page=1)}),
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta(current_page=1)}),
        ])
        recover_plan = make_plan(output_dir=evidence.path / "recovered", live=True, timeout_retry=True)
        recovered = diag.execute(
            recover_plan,
            session_factory=lambda _access: recovering,
            access_loader=fake_access,
            monotonic=lambda: 0.0,
        )
        assert len(recovering.calls) == 3
        assert recovered["summary"]["request_count"] == 3
        page1 = json.loads((evidence.path / "recovered" / "page_1_summary.json").read_text())
        assert page1["http"]["retry_count"] == 1
        assert page1["http"]["request_sequence"] == 2

        # A non-timeout transport error is never retried.
        connection_session = FakeSession([requests.exceptions.ConnectionError("refused")])
        conn_plan = make_plan(output_dir=evidence.path / "conn", live=True, timeout_retry=True)
        conn_result = diag.execute(
            conn_plan,
            session_factory=lambda _access: connection_session,
            access_loader=fake_access,
            monotonic=lambda: 0.0,
        )
        assert len(connection_session.calls) == 1
        assert conn_result["summary"]["blocked"]["code"] == "TRANSPORT_ERROR"
        print("PASS 20 the timeout retry fires at most once and only for timeouts")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# Extra: malformed payloads and unresolved identity
# ---------------------------------------------------------------------------

def test_malformed_payload_and_unresolved_identity() -> None:
    evidence = TempEvidence()
    try:
        cases = [
            ({"meta": meta()}, "RESPONSE_DATA_MISSING"),
            ({"data": {"rows": []}, "meta": meta()}, "RESPONSE_DATA_NOT_LIST"),
            ([1, 2, 3], "RESPONSE_NOT_OBJECT"),
        ]
        for index, (payload, expected_code) in enumerate(cases):
            result, _ = run_live_case(
                evidence_dir=evidence.path / f"malformed-{index}",
                script=[FakeResponse(payload=payload), FakeResponse(payload=payload)],
            )
            assert result["summary"]["result_classification"] == diag.RESULT_RESPONSE_MALFORMED
            assert result["summary"]["blocked"]["code"] == expected_code

        not_json = FakeResponse(body=b"<html>gateway</html>", headers={"Content-Type": "text/html"})
        result, _ = run_live_case(
            evidence_dir=evidence.path / "not-json",
            script=[not_json, FakeResponse(payload={"data": []})],
        )
        assert result["summary"]["blocked"]["code"] == "RESPONSE_NOT_JSON"

        error_status = FakeResponse(status_code=503, payload={"message": "unavailable"})
        result, _ = run_live_case(
            evidence_dir=evidence.path / "http-503",
            script=[error_status, FakeResponse(payload={"data": []})],
        )
        assert result["summary"]["result_classification"] == diag.RESULT_TRANSPORT_FAILURE
        assert result["summary"]["blocked"]["code"] == "HTTP_STATUS_NOT_OK"

        # Rows without a usable trip_id leave the identity contract unresolved.
        broken = [{"vehicle_id": 1, "registration": SENSITIVE_REGISTRATION}, {"trip_id": None}]
        result, _ = run_live_case(
            evidence_dir=evidence.path / "identity",
            script=[
                FakeResponse(payload={"data": broken, "meta": meta()}),
                FakeResponse(payload={"data": broken[:1], "meta": meta()}),
            ],
        )
        assert result["summary"]["result_classification"] == diag.RESULT_IDENTITY_UNRESOLVED
        assert result["cross_page"]["verdict"] == diag.VERDICT_UNKNOWN_IDENTITY_CONTRACT
        assert result["cross_page"]["identity_contract_resolved"] is False
        page1 = json.loads((evidence.path / "identity" / "page_1_summary.json").read_text())
        assert page1["data_identity"]["identity_source"] == diag.IDENTITY_SOURCE_COMPOSITE_FALLBACK
        assert page1["data_identity"]["rows_without_provider_trip_id"] == 2

        # Duplicate rows within one page are counted.
        duplicated = [trip_row(1), trip_row(1), trip_row(2)]
        result, _ = run_live_case(
            evidence_dir=evidence.path / "dupes",
            script=[
                FakeResponse(payload={"data": duplicated, "meta": meta()}),
                FakeResponse(payload={"data": duplicated, "meta": meta()}),
            ],
        )
        page1 = json.loads((evidence.path / "dupes" / "page_1_summary.json").read_text())
        assert page1["data_identity"]["row_count"] == 3
        assert page1["data_identity"]["unique_identity_count"] == 2
        assert page1["data_identity"]["duplicate_identity_count"] == 1
        print("PASS 21 malformed payloads, bad status and unresolved identity are classified")
    finally:
        evidence.cleanup()


# ---------------------------------------------------------------------------
# Extra: request shape matches the production /trips contract
# ---------------------------------------------------------------------------

def test_request_shape_matches_production_contract() -> None:
    evidence = TempEvidence()
    try:
        script = [
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta()}),
            FakeResponse(payload={"data": [trip_row(1)], "meta": meta()}),
        ]
        _result, session = run_live_case(evidence_dir=evidence.dir, script=script)

        for call in session.calls:
            assert call["method"] == "GET"
            assert call["url"] == "https://fleetapi.example.invalid/rest/trips"
            assert call["allow_redirects"] is False
            assert call["stream"] is True
            assert call["timeout"] == diag.DEFAULT_TIMEOUT_SECONDS
            params = call["params"]
            # Production `/trips` addresses trips by Europe/Warsaw wall-clock
            # (docs/18 §1), so the diagnostic must too — otherwise it probes a
            # period shifted by the Warsaw offset and reports on different rows
            # than the job reads. 2026-07-31 is CEST, so the intended UTC
            # window 06:00–07:00 goes on the wire as 08:00–09:00.
            assert params["start_timestamp"] == "2026-07-31 08:00:00"
            assert params["end_timestamp"] == "2026-07-31 09:00:00"
            assert params["incl_private"] == "true"
            assert set(params) == {
                "start_timestamp", "end_timestamp", "incl_private", "page", "limit",
            }

        # Evidence must stay outside the repository working tree.
        expect_safety(
            lambda: make_plan(output_dir=REPO_ROOT / "artifacts" / "evidence"),
            "OUTPUT_DIR_INSIDE_REPOSITORY",
        )
        # The configured base URL can be pinned by the operator.
        mismatch = make_plan(
            output_dir=evidence.path / "pinned",
            live=True,
            extra=["--expected-base-url", "https://other.example.invalid/rest"],
        )
        blocked = diag.execute(
            mismatch,
            session_factory=lambda _access: FakeSession([]),
            access_loader=fake_access,
            monotonic=lambda: 0.0,
        )
        assert blocked["summary"]["blocked"]["code"] == "BASE_URL_MISMATCH"
        print("PASS 22 request shape mirrors the production /trips contract")
    finally:
        evidence.cleanup()


def main() -> None:
    test_dry_run_opens_no_socket()
    test_live_flag_is_required()
    test_only_delta00001_is_allowed()
    test_window_bounds()
    test_page_bounds()
    test_limit_bounds()
    test_cross_host_redirect_is_rejected()
    test_credentials_are_never_printed_or_persisted()
    test_identical_pages_classify_as_page_parameter_ignored()
    test_distinct_pages_classify_as_metadata_broken()
    test_partial_overlap_is_detected()
    test_empty_second_page_is_detected()
    test_string_and_integer_pagination_fields_record_types()
    test_missing_metadata_is_recorded_safely()
    test_nested_metadata_is_detected()
    test_raw_trip_data_and_sensitive_fields_stay_out_of_evidence()
    test_manifest_and_checksums_validate()
    test_request_budget_cannot_be_exceeded()
    test_timeout_retry_happens_at_most_once()
    test_malformed_payload_and_unresolved_identity()
    test_request_shape_matches_production_contract()
    print("telematics trips pagination diagnostic tests: OK")


if __name__ == "__main__":
    main()
