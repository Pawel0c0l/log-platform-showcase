#!/usr/bin/env python3
"""Manual tests for the Telematics `/trips` compatibility pagination state machine (C7).

Covers `docs/12_telematics_trips_pagination_compatibility.md` §13 (T1-T29, T35),
the accepted `docs/16_telematics_d5_total_policy_decision.md` §5.4 empty/short-page
cases, the strict-mode regression set, and the minimum mode propagation
(`docs/14_…` §8 S7/S8/S8a/S9).

Everything runs against a scripted fake provider session. Live network access is
made fatal at import time: no provider request may be issued by this suite.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_telematics_trips_pagination_compat.py
"""
from __future__ import annotations

import json
import os
import random
import socket
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------
# Make any live network access fatal before the provider client is imported.
# --------------------------------------------------------------------------

class LiveNetworkAttempted(AssertionError):
    """Raised if any code under test tries to open a real socket."""


def _fatal_network(*_args, **_kwargs):
    raise LiveNetworkAttempted("live network access is not authorized in this suite")


import ssl  # noqa: E402,F401  (imported before socket is neutered)
import requests  # noqa: E402

# Every outbound path a provider request could take is now fatal.
socket.socket.connect = _fatal_network  # type: ignore[assignment]
socket.socket.connect_ex = _fatal_network  # type: ignore[assignment]
socket.create_connection = _fatal_network  # type: ignore[assignment]
requests.Session.request = _fatal_network  # type: ignore[assignment]
requests.Session.get = _fatal_network  # type: ignore[assignment]
requests.api.request = _fatal_network  # type: ignore[assignment]


def _install_stub(name: str, attrs: dict | None = None) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except Exception:
        pass
    mod = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_install_stub("psycopg")
_install_stub("psycopg.rows", attrs={"dict_row": object()})

from jobs.api.telematics import provider_client as pc  # noqa: E402
from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402
from jobs.api.telematics.provider_client import TelematicsFleetProviderClient  # noqa: E402
from jobs.api.telematics.provider_safety import (  # noqa: E402
    TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S_ENV,
    TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV,
    TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV,
    TelematicsProviderSafetyError,
    CompatibilitySafetyLimits,
    ProviderRunBudget,
    SafetyLimits,
)
from jobs.trips_pagination_mode import (  # noqa: E402
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    TRIPS_PAGINATION_MODE_STRICT_META,
)


FAILURES: list[str] = []
COMPAT_ENV_NAMES = (
    TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV,
    TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV,
    TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S_ENV,
)


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


WINDOW_START = _ts("2026-07-28T16:00:00Z")
WINDOW_END = _ts("2026-07-28T17:00:00Z")


# --------------------------------------------------------------------------
# Scripted provider session
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200, headers: dict | None = None):
        self.payload = payload
        self.status_code = status_code
        self.text = "<fake response body>"
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class ScriptedSession:
    """Returns scripted payloads in request order and records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.auth = None

    def get(self, url: str, *, params: dict, timeout: int):
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        if not self.responses:
            raise AssertionError(
                f"unexpected extra provider request #{len(self.calls)} params={dict(params)}"
            )
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)

    @property
    def requested_pages(self) -> list:
        return [c["params"].get("page") for c in self.calls]

    @property
    def requested_paths(self) -> list:
        return [c["url"] for c in self.calls]


ABSENT = object()


def _rows(ids) -> list:
    return [
        {
            "trip_id": i,
            "registration": f"REG{i:04d}",
            "start_timestamp": "2026-07-28 16:00:00",
            "end_timestamp": "2026-07-28 16:10:00",
        }
        for i in ids
    ]


def _broken_meta(total=ABSENT, **overrides) -> dict:
    """The production defect: `current_page` pinned to 1, `per_page` the default."""
    meta = {"current_page": 1, "per_page": 10, "last_page": 6, "from": 1, "to": 25}
    meta.update(overrides)
    if total is not ABSENT:
        meta["total"] = total
    return meta


def _payload(rows, total=ABSENT, *, meta=ABSENT, **meta_overrides) -> dict:
    out: dict = {"data": rows}
    if meta is ABSENT:
        out["meta"] = _broken_meta(total, **meta_overrides)
    elif meta is not None or meta is None:
        if meta is not ABSENT:
            out["meta"] = meta
    return out


def _payload_without_meta(rows) -> dict:
    return {"data": rows}


def _client(responses, *, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1, page_limit=25,
            log_sink=None, safety_limits=None, **kwargs):
    limits = safety_limits or SafetyLimits()
    client = TelematicsFleetProviderClient(
        base_url="https://fleet.example.test",
        basic_auth_username="user",
        basic_auth_password="secret",
        timeout_s=12,
        page_limit=page_limit,
        safety_limits=limits,
        budget=ProviderRunBudget(limits=limits),
        trips_pagination_mode=mode,
        log_fn=(lambda level, message, context: log_sink.append((level, message, context)))
        if log_sink is not None
        else None,
        **kwargs,
    )
    session = ScriptedSession(responses)
    client._session = session
    return client, session


def _fetch(client):
    return client.fetch_trips(
        window_start_ts=WINDOW_START, window_end_ts=WINDOW_END, incl_private=True,
    )


def _expect_abort(label: str, responses, *, code: str, page_limit=25,
                  context_expectations: dict | None = None, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
                  safety_limits=None, expected_requests: int | None = None) -> None:
    client, session = _client(responses, mode=mode, page_limit=page_limit, safety_limits=safety_limits)
    try:
        rows = _fetch(client)
    except TelematicsProviderSafetyError as exc:
        ok = exc.code == code
        detail = f"code={exc.code!r} expected={code!r}"
        if ok and context_expectations:
            for key, value in context_expectations.items():
                if exc.context.get(key) != value:
                    ok = False
                    detail = f"context[{key!r}]={exc.context.get(key)!r} expected={value!r}"
                    break
        if ok and expected_requests is not None and len(session.calls) != expected_requests:
            ok = False
            detail = f"requests={len(session.calls)} expected={expected_requests}"
        _check(label, ok, detail)
        return
    except Exception as exc:  # noqa: BLE001
        _check(label, False, f"unexpected {type(exc).__name__}: {exc}")
        return
    _check(label, False, f"no abort raised; returned {len(rows)} rows")


# --------------------------------------------------------------------------
# Strict-mode regressions (docs/12 T1, T2; docs/14 §8 S8)
# --------------------------------------------------------------------------

def test_strict_healthy_metadata_multi_page() -> None:
    client, session = _client(
        [
            {"data": _rows(range(1, 26)), "meta": {"current_page": 1, "last_page": 3, "total": 58}},
            {"data": _rows(range(26, 51)), "meta": {"current_page": 2, "last_page": 3, "total": 58}},
            {"data": _rows(range(51, 59)), "meta": {"current_page": 3, "last_page": 3, "total": 58}},
        ],
        mode=TRIPS_PAGINATION_MODE_STRICT_META,
    )
    rows = _fetch(client)
    _check("strict: healthy metadata echo paginates and terminates on last_page",
           len(rows) == 58 and session.requested_pages == [1, 2, 3],
           f"rows={len(rows)} pages={session.requested_pages}")
    _check("strict: request count unchanged for the healthy 3-page fixture",
           len(session.calls) == 3, f"requests={len(session.calls)}")


def test_strict_current_page_behind_is_pagination_mismatch() -> None:
    # The production defect: page 2 answered with meta.current_page == 1.
    _expect_abort(
        "strict: current_page behind requested page aborts PAGINATION_MISMATCH",
        [
            {"data": _rows(range(1, 26)), "meta": {"current_page": 1, "last_page": 6, "total": 58}},
            {"data": _rows(range(26, 51)), "meta": {"current_page": 1, "last_page": 6, "total": 58}},
        ],
        code="PAGINATION_MISMATCH",
        mode=TRIPS_PAGINATION_MODE_STRICT_META,
        expected_requests=2,
    )


def test_strict_current_page_ahead_is_pagination_mismatch() -> None:
    _expect_abort(
        "strict: current_page ahead of requested page aborts PAGINATION_MISMATCH",
        [{"data": _rows(range(1, 26)), "meta": {"current_page": 2, "last_page": 6, "total": 58}}],
        code="PAGINATION_MISMATCH",
        mode=TRIPS_PAGINATION_MODE_STRICT_META,
        expected_requests=1,
    )


def test_strict_tolerates_matching_integer_like_strings() -> None:
    client, session = _client(
        [
            {"data": _rows(range(1, 26)), "meta": {"current_page": "1", "last_page": "2"}},
            {"data": _rows(range(26, 40)), "meta": {"current_page": "2", "last_page": "2"}},
        ],
        mode=TRIPS_PAGINATION_MODE_STRICT_META,
    )
    rows = _fetch(client)
    _check("strict: integer-like string metadata still tolerated",
           len(rows) == 39 and len(session.calls) == 2,
           f"rows={len(rows)} requests={len(session.calls)}")


def test_strict_partial_and_missing_metadata() -> None:
    _expect_abort(
        "strict: partial metadata (current_page without last_page) aborts MALFORMED_PAGINATION",
        [{"data": _rows(range(1, 5)), "meta": {"current_page": 1}}],
        code="MALFORMED_PAGINATION",
        mode=TRIPS_PAGINATION_MODE_STRICT_META,
    )
    client, session = _client(
        [_payload_without_meta(_rows(range(1, 5)))],
        mode=TRIPS_PAGINATION_MODE_STRICT_META,
    )
    rows = _fetch(client)
    _check("strict: absent metadata on the first page returns that page",
           len(rows) == 4 and len(session.calls) == 1,
           f"rows={len(rows)} requests={len(session.calls)}")


def test_strict_terminates_on_last_page_zero() -> None:
    client, session = _client(
        [{"data": [], "meta": {"current_page": 1, "last_page": 0, "total": 0}}],
        mode=TRIPS_PAGINATION_MODE_STRICT_META,
    )
    rows = _fetch(client)
    _check("strict: last_page == 0 terminates after one request",
           rows == [] and len(session.calls) == 1,
           f"rows={len(rows)} requests={len(session.calls)}")


def test_strict_is_the_default_mode() -> None:
    client, session = _client(
        [{"data": _rows(range(1, 3)), "meta": {"current_page": 1, "last_page": 1}}],
        mode=None,
    )
    rows = _fetch(client)
    _check("strict: mode defaults to strict_meta when none is supplied",
           client.trips_pagination_mode == TRIPS_PAGINATION_MODE_STRICT_META and len(rows) == 2,
           f"mode={client.trips_pagination_mode!r}")


def test_non_trips_endpoints_stay_strict_under_compatibility_mode() -> None:
    client, session = _client(
        [
            {"data": [{"vehicle_id": 1, "registration": "REG1"}],
             "meta": {"current_page": 1, "last_page": 1, "total": 1}},
        ],
        mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
        page_limit=25,
    )
    rows = client.fetch_vehicles_fleet()
    _check("boundary: /vehicles keeps strict pagination while the client is compatibility",
           len(rows) == 1 and session.requested_paths == ["https://fleet.example.test/vehicles"],
           f"rows={len(rows)} paths={session.requested_paths}")

    # And the strict metadata contract still governs that endpoint.
    client2, _ = _client(
        [{"data": [{"vehicle_id": 2, "registration": "REG2"}],
          "meta": {"current_page": 2, "last_page": 6, "total": 9}}],
        mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    )
    try:
        client2.fetch_vehicles_fleet()
        _check("boundary: /vehicles still raises PAGINATION_MISMATCH on broken metadata", False,
               "no abort raised")
    except TelematicsProviderSafetyError as exc:
        _check("boundary: /vehicles still raises PAGINATION_MISMATCH on broken metadata",
               exc.code == "PAGINATION_MISMATCH", f"code={exc.code}")


# --------------------------------------------------------------------------
# Compatibility successes (docs/12 T4-T9c; docs/16 §5.4)
# --------------------------------------------------------------------------

def test_compat_short_first_page() -> None:
    client, session = _client([_payload(_rows(range(1, 9)), 8)])
    rows = _fetch(client)
    _check("compat: one short first page succeeds in one request",
           len(rows) == 8 and session.requested_pages == [1],
           f"rows={len(rows)} pages={session.requested_pages}")


def test_compat_empty_first_page_absent_total() -> None:
    client, session = _client([_payload([])])
    rows = _fetch(client)
    _check("compat: empty first page with absent total is a successful zero-row result",
           rows == [] and len(session.calls) == 1,
           f"rows={rows!r} requests={len(session.calls)}")


def test_compat_empty_first_page_total_zero() -> None:
    client, session = _client([_payload([], 0)])
    rows = _fetch(client)
    _check("compat: empty first page with total 0 reconciles exactly",
           rows == [] and len(session.calls) == 1,
           f"rows={rows!r} requests={len(session.calls)}")


def test_compat_full_pages_then_short_page() -> None:
    log: list = []
    client, session = _client(
        [
            _payload(_rows(range(1, 26)), 58),
            _payload(_rows(range(26, 51)), 58),
            _payload(_rows(range(51, 59)), 58),
        ],
        log_sink=log,
    )
    rows = _fetch(client)
    ids = [r["trip_id"] for r in rows]
    _check("compat: full pages continue and the short page terminates",
           len(rows) == 58 and session.requested_pages == [1, 2, 3],
           f"rows={len(rows)} pages={session.requested_pages}")
    _check("compat: provider row order is preserved end to end",
           ids == list(range(1, 59)), f"first={ids[:3]} last={ids[-3:]}")
    summaries = [c for _l, m, c in log if m == "telematics_trips_compat_subwindow_summary"]
    _check("compat: sub-window summary reports exact total reconciliation",
           len(summaries) == 1
           and summaries[0]["total_reconciliation"] == "exact"
           and summaries[0]["accumulated_unique_rows"] == 58
           and summaries[0]["termination_reason"] == "short_page",
           f"summary={summaries[-1] if summaries else None}")


def test_compat_full_pages_then_empty_page() -> None:
    client, session = _client(
        [
            _payload(_rows(range(1, 26)), 50),
            _payload(_rows(range(26, 51)), 50),
            _payload([], 50),
        ],
    )
    rows = _fetch(client)
    _check("compat: an exact multiple terminates on the extra empty page",
           len(rows) == 50 and session.requested_pages == [1, 2, 3],
           f"rows={len(rows)} pages={session.requested_pages}")


def test_compat_exact_multiple_single_page() -> None:
    client, session = _client([_payload(_rows(range(1, 26)), 25), _payload([], 25)])
    rows = _fetch(client)
    _check("compat: a full first page never terminates, the empty second page does",
           len(rows) == 25 and session.requested_pages == [1, 2],
           f"rows={len(rows)} pages={session.requested_pages}")


def test_compat_ignores_contradictory_metadata() -> None:
    client, session = _client(
        [
            _payload(_rows(range(1, 26)), 33, current_page=1, per_page=10, last_page=6, **{"from": 1, "to": 25}),
            _payload(_rows(range(26, 34)), 33, current_page=1, per_page=999, last_page=0, **{"from": 1, "to": 25}),
        ],
    )
    rows = _fetch(client)
    _check("compat: pinned current_page and contradictory per_page/last_page do not control the loop",
           len(rows) == 33 and session.requested_pages == [1, 2],
           f"rows={len(rows)} pages={session.requested_pages}")


def test_compat_absent_total_multi_page() -> None:
    log: list = []
    client, session = _client(
        [_payload(_rows(range(1, 26))), _payload(_rows(range(26, 30)))],
        log_sink=log,
    )
    rows = _fetch(client)
    summaries = [c for _l, m, c in log if m == "telematics_trips_compat_subwindow_summary"]
    _check("compat: absent total is permitted and terminates on the short page",
           len(rows) == 29 and summaries[-1]["total_reconciliation"] == "absent"
           and summaries[-1]["total_present"] is False,
           f"rows={len(rows)} summary={summaries[-1] if summaries else None}")


def test_compat_meta_absent_entirely() -> None:
    client, session = _client([_payload_without_meta(_rows(range(1, 5)))])
    rows = _fetch(client)
    _check("compat: a response without any meta block is a valid short page",
           len(rows) == 4 and len(session.calls) == 1,
           f"rows={len(rows)} requests={len(session.calls)}")


# --------------------------------------------------------------------------
# Compatibility failures (docs/12 T10-T29)
# --------------------------------------------------------------------------

def test_compat_identity_failures() -> None:
    missing = [{"registration": "REG1", "start_timestamp": "2026-07-28 16:00:00"}]
    _expect_abort("compat: row without trip_id aborts PAGINATION_COMPAT_IDENTITY_MISSING",
                  [_payload(missing, 1)],
                  code="PAGINATION_COMPAT_IDENTITY_MISSING",
                  context_expectations={"identity_defect": "missing"})
    _expect_abort("compat: null trip_id aborts as a missing identity",
                  [_payload([{"trip_id": None}], 1)],
                  code="PAGINATION_COMPAT_IDENTITY_MISSING",
                  context_expectations={"identity_defect": "missing"})
    for label, value in (("boolean", True), ("non-numeric string", "abc"), ("non-integral float", 1.5)):
        _expect_abort(f"compat: {label} trip_id aborts as a malformed identity",
                      [_payload([{"trip_id": value}], 1)],
                      code="PAGINATION_COMPAT_IDENTITY_MISSING",
                      context_expectations={"identity_defect": "malformed"})


def test_compat_malformed_response_shapes() -> None:
    _expect_abort("compat: data not a list aborts MALFORMED_RESPONSE",
                  [{"data": {"trip_id": 1}, "meta": _broken_meta(1)}],
                  code="MALFORMED_RESPONSE")
    _expect_abort("compat: missing data key aborts MALFORMED_RESPONSE",
                  [{"meta": _broken_meta(1)}],
                  code="MALFORMED_RESPONSE")
    _expect_abort("compat: a row that is not an object aborts MALFORMED_RESPONSE",
                  [_payload([{"trip_id": 1}, "not-an-object"], 2)],
                  code="MALFORMED_RESPONSE")
    _expect_abort("compat: meta changing JSON type mid-sub-window aborts PAGINATION_COMPAT_SHAPE_UNSTABLE",
                  [_payload(_rows(range(1, 26))), {"data": _rows(range(26, 30)), "meta": []}],
                  code="PAGINATION_COMPAT_SHAPE_UNSTABLE")


def test_compat_duplicate_and_overlap_failures() -> None:
    _expect_abort("compat: duplicate identity inside one page aborts PAGINATION_COMPAT_DUPLICATE_IN_PAGE",
                  [_payload(_rows([1, 2, 3, 2]), 4)],
                  code="PAGINATION_COMPAT_DUPLICATE_IN_PAGE")
    _expect_abort("compat: overlap with the immediately previous page aborts PAGINATION_COMPAT_PAGE_OVERLAP",
                  [_payload(_rows(range(1, 26)), 58), _payload(_rows(range(20, 45)), 58)],
                  code="PAGINATION_COMPAT_PAGE_OVERLAP")
    _expect_abort("compat: partial overlap of one identity in 25 aborts PAGINATION_COMPAT_PAGE_OVERLAP",
                  [_payload(_rows(range(1, 26)), 58), _payload(_rows([25] + list(range(26, 50))), 58)],
                  code="PAGINATION_COMPAT_PAGE_OVERLAP",
                  context_expectations={"overlap_count": 1})
    _expect_abort("compat: overlap with a non-adjacent page aborts (full-history tracking)",
                  [
                      _payload(_rows(range(1, 26)), 200),
                      _payload(_rows(range(26, 51)), 200),
                      _payload(_rows(range(51, 76)), 200),
                      _payload(_rows(list(range(76, 100)) + [3]), 200),
                  ],
                  code="PAGINATION_COMPAT_PAGE_OVERLAP",
                  expected_requests=4)


def test_compat_repeat_failures() -> None:
    repeated = _rows(range(1, 26))
    _expect_abort("compat: identical ordered identity fingerprint aborts PAGINATION_COMPAT_PAGE_REPEATED",
                  [_payload(list(repeated), 58), _payload(list(repeated), 58)],
                  code="PAGINATION_COMPAT_PAGE_REPEATED",
                  context_expectations={"repeat_kind": "ordered_fingerprint"})
    reordered = list(reversed(_rows(range(1, 26))))
    _check("compat: the reordered fixture really is a different order",
           [r["trip_id"] for r in reordered] != [r["trip_id"] for r in repeated])
    _expect_abort("compat: same identity set in a different order aborts as an unordered repeat",
                  [_payload(list(repeated), 58), _payload(reordered, 58)],
                  code="PAGINATION_COMPAT_PAGE_REPEATED",
                  context_expectations={"repeat_kind": "unordered_identity_set"})
    cosmetic = [dict(r, registration="CHANGED") for r in repeated]
    _expect_abort("compat: identity-based fingerprints see through a cosmetic field change",
                  [_payload(list(repeated), 58), _payload(cosmetic, 58)],
                  code="PAGINATION_COMPAT_PAGE_REPEATED",
                  context_expectations={"repeat_kind": "ordered_fingerprint"})


def test_compat_page_larger_than_limit() -> None:
    _expect_abort("compat: a page larger than the requested limit aborts PAGINATION_COMPAT_ROWS_EXCEED_LIMIT",
                  [_payload(_rows(range(1, 30)), 29)],
                  code="PAGINATION_COMPAT_ROWS_EXCEED_LIMIT",
                  context_expectations={"returned_count": 29})


def test_compat_total_invalid_variants() -> None:
    for label, value in (
        ("bool", True),
        ("float", 58.0),
        ("numeric string", "58"),
        ("null", None),
        ("object", {"count": 58}),
        ("array", [58]),
        ("negative", -1),
    ):
        _expect_abort(f"compat: total as {label} aborts PAGINATION_COMPAT_TOTAL_INVALID",
                      [_payload(_rows(range(1, 9)), value)],
                      code="PAGINATION_COMPAT_TOTAL_INVALID",
                      expected_requests=1)


def test_compat_total_unstable_variants() -> None:
    _expect_abort("compat: total changing between pages aborts PAGINATION_COMPAT_TOTAL_UNSTABLE",
                  [_payload(_rows(range(1, 26)), 58), _payload(_rows(range(26, 51)), 59)],
                  code="PAGINATION_COMPAT_TOTAL_UNSTABLE")
    _expect_abort("compat: total disappearing mid-sub-window aborts PAGINATION_COMPAT_TOTAL_UNSTABLE",
                  [_payload(_rows(range(1, 26)), 58), _payload(_rows(range(26, 51)))],
                  code="PAGINATION_COMPAT_TOTAL_UNSTABLE")
    _expect_abort("compat: total appearing mid-sub-window aborts PAGINATION_COMPAT_TOTAL_UNSTABLE",
                  [_payload(_rows(range(1, 26))), _payload(_rows(range(26, 51)), 58)],
                  code="PAGINATION_COMPAT_TOTAL_UNSTABLE")


def test_compat_total_bound_and_reconciliation() -> None:
    _expect_abort("compat: accumulated rows above total abort PAGINATION_COMPAT_TOTAL_EXCEEDED",
                  [_payload(_rows(range(1, 26)), 30), _payload(_rows(range(26, 51)), 30)],
                  code="PAGINATION_COMPAT_TOTAL_EXCEEDED")
    _expect_abort("compat: non-empty data with total 0 aborts PAGINATION_COMPAT_TOTAL_EXCEEDED",
                  [_payload(_rows(range(1, 9)), 0)],
                  code="PAGINATION_COMPAT_TOTAL_EXCEEDED",
                  expected_requests=1)
    _expect_abort("compat: short page with total above accumulated rows aborts reconciliation",
                  [_payload(_rows(range(1, 26)), 99), _payload(_rows(range(26, 30)), 99)],
                  code="PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED",
                  expected_requests=2)
    _expect_abort("compat: empty page with a present higher total aborts reconciliation",
                  [_payload([], 5)],
                  code="PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED",
                  expected_requests=1)


def test_compat_absent_total_code_never_exists() -> None:
    sources = "\n".join(
        (REPO_ROOT / rel).read_text(encoding="utf-8")
        for rel in (
            "jobs/api/telematics/provider_client.py",
            "jobs/api/telematics/provider_safety.py",
            "jobs/api/telematics/sync_trips_and_speeding.py",
        )
    )
    forbidden = (
        "PAGINATION_COMPAT_TOTAL_ABSENT",
        "PAGINATION_COMPAT_TOTAL_IMPLAUSIBLE",
        "PAGINATION_COMPAT_TOTAL_CHANGED",
        "PAGINATION_COMPAT_ROWS_EXCEED_TOTAL",
        "PAGINATION_COMPAT_SHORT_PAGE_INCONSISTENT_WITH_TOTAL",
    )
    present = [name for name in forbidden if name in sources]
    _check("D5: no withdrawn or superseded total classification exists in runtime code",
           not present, f"found={present}")


def test_compat_budget_failures() -> None:
    # Page budget: 3 permitted pages, all full.
    limits = SafetyLimits()
    limits.max_pages_per_subwindow = 3
    _expect_abort("compat: page budget aborts MAX_PAGES_PER_SUBWINDOW before the next request",
                  [_payload(_rows(range(i * 25 + 1, i * 25 + 26)), 999) for i in range(3)],
                  code="MAX_PAGES_PER_SUBWINDOW",
                  safety_limits=limits,
                  expected_requests=3)

    # Request budget per sub-window.
    limits2 = SafetyLimits()
    limits2.max_requests_per_subwindow = 2
    _expect_abort("compat: request budget aborts MAX_REQUESTS_PER_SUBWINDOW",
                  [_payload(_rows(range(i * 25 + 1, i * 25 + 26)), 999) for i in range(2)],
                  code="MAX_REQUESTS_PER_SUBWINDOW",
                  safety_limits=limits2,
                  expected_requests=2)

    # Row budget.
    os.environ[TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV] = "10"
    try:
        _expect_abort("compat: row budget aborts PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED",
                      [_payload(_rows(range(1, 26)), 999)],
                      code="PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED",
                      expected_requests=1)
    finally:
        os.environ.pop(TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV, None)

    # Per-response byte budget.
    os.environ[TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV] = "64"
    try:
        _expect_abort("compat: response byte budget aborts PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED",
                      [_payload(_rows(range(1, 26)), 999)],
                      code="PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED",
                      context_expectations={"scope": "response"},
                      expected_requests=1)
    finally:
        os.environ.pop(TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV, None)

    # Elapsed budget: a deterministic clock that jumps once the first page has
    # been served, so the budget is evaluated before the second request.
    client, session = _client(
        [_payload(_rows(range(1, 26)), 999), _payload(_rows(range(26, 51)), 999)],
    )
    real_monotonic = pc.time.monotonic
    pc.time.monotonic = lambda: 0.0 if not session.calls else 5000.0  # type: ignore[assignment]
    try:
        _fetch(client)
        _check("compat: elapsed budget aborts PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED", False,
               "no abort raised")
    except TelematicsProviderSafetyError as exc:
        _check("compat: elapsed budget aborts PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED",
               exc.code == "PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED" and len(session.calls) == 1,
               f"code={exc.code} requests={len(session.calls)}")
    finally:
        pc.time.monotonic = real_monotonic  # type: ignore[assignment]


def test_compat_invalid_configuration_fails_closed_before_any_request() -> None:
    for env_name, bad_value, reason in (
        (TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV, "not-a-number", "not_an_integer"),
        (TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV, "0", "not_positive"),
        (TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S_ENV, "-5", "not_positive"),
        (TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV, str(64 * 1024 * 1024 + 1), "above_ceiling"),
    ):
        os.environ[env_name] = bad_value
        try:
            client, session = _client([_payload(_rows(range(1, 9)), 8)])
            try:
                _fetch(client)
                _check(f"compat config: {env_name}={bad_value!r} must fail closed", False,
                       "no abort raised")
            except TelematicsProviderSafetyError as exc:
                _check(f"compat config: {env_name}={bad_value!r} fails closed with no request issued",
                       exc.code == "PAGINATION_COMPAT_CONFIG_INVALID"
                       and exc.context.get("reason") == reason
                       and len(session.calls) == 0,
                       f"code={exc.code} context={exc.context} requests={len(session.calls)}")
        finally:
            os.environ.pop(env_name, None)

    _check("compat config: defaults derive from the approved page/page-budget product",
           CompatibilitySafetyLimits.from_env(page_limit=1000, max_pages_per_subwindow=50)
           == CompatibilitySafetyLimits(
               max_rows_per_subwindow=50000,
               max_response_bytes=32 * 1024 * 1024,
               max_response_bytes_per_subwindow=32 * 1024 * 1024 * 50,
               max_elapsed_s=900,
           ),
           f"limits={CompatibilitySafetyLimits.from_env(page_limit=1000, max_pages_per_subwindow=50)}")


def test_compat_unknown_mode_fails_closed() -> None:
    for bad in ("data_invariants_v2", "", "STRICT_META", 7, True):
        try:
            TelematicsFleetProviderClient(
                base_url="https://fleet.example.test",
                basic_auth_username="u",
                basic_auth_password="p",
                trips_pagination_mode=bad,
            )
            _check(f"compat: unknown provider mode {bad!r} must fail closed", False, "no error")
        except ValueError:
            _check(f"compat: unknown provider mode {bad!r} fails closed", True)


# --------------------------------------------------------------------------
# Write-boundary and observability guarantees
# --------------------------------------------------------------------------

def test_compat_never_returns_partially_validated_pages() -> None:
    client, session = _client(
        [
            _payload(_rows(range(1, 26)), 999),
            _payload(_rows(range(26, 51)), 999),
            _payload(_rows([10] + list(range(51, 75))), 999),
        ],
    )
    try:
        _fetch(client)
        _check("compat: a later-page failure discards every earlier page", False, "no abort raised")
    except TelematicsProviderSafetyError as exc:
        _check("compat: a later-page failure discards every earlier page",
               exc.code == "PAGINATION_COMPAT_PAGE_OVERLAP" and len(session.calls) == 3,
               f"code={exc.code} requests={len(session.calls)}")


def test_compat_logs_are_sanitized() -> None:
    log: list = []
    client, _session = _client(
        [_payload(_rows(range(1, 26)), 30), _payload(_rows(range(26, 31)), 30)],
        log_sink=log,
    )
    _fetch(client)
    compat_records = [(m, c) for _l, m, c in log if m.startswith("telematics_trips_compat_")]
    rendered = json.dumps([c for _m, c in compat_records], default=str)
    leaked = [needle for needle in ("REG0001", "REG0026", "2026-07-28 16:10:00") if needle in rendered]
    _check("observability: compatibility page logs carry no registration, timestamp or payload",
           not leaked and len(compat_records) == 3, f"leaked={leaked} records={len(compat_records)}")

    page_records = [c for m, c in compat_records if m == "telematics_trips_compat_page"]
    required = {
        "trips_pagination_mode", "requested_page", "requested_limit", "returned_count",
        "accumulated_unique_rows", "short_page", "total_present", "advisory_total",
        "page_identity_fingerprint", "page_identity_set_fingerprint", "response_bytes",
        "pages_remaining_subwindow",
    }
    _check("observability: per-page evidence carries the required sanitized keys",
           all(required <= set(c) for c in page_records),
           f"missing={[sorted(required - set(c)) for c in page_records]}")
    _check("observability: raw identities are never emitted, only truncated digests",
           all(len(c["page_identity_fingerprint"]) == 16 for c in page_records)
           and not any(str(i) in json.dumps(c["page_identity_fingerprint"]) for c in page_records for i in [999999]),
           "digest length check")

    salt_hex = client._compat_identity_salt.hex()
    _check("observability: the per-execution HMAC salt never reaches a log record",
           salt_hex not in rendered)


def test_c7_writes_no_coverage_state() -> None:
    for rel in ("jobs/api/telematics/provider_client.py", "jobs/api/telematics/provider_safety.py"):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        _check(f"boundary: {rel} names no coverage surface",
               "client_dataset_coverage" not in source and "covered_through_ts" not in source)
    sync_source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text(encoding="utf-8")
    _check("boundary: the sync job still names no coverage surface",
           "client_dataset_coverage" not in sync_source and "covered_through_ts" not in sync_source)
    _check("boundary: the sync job still commits its business transaction exactly once",
           sync_source.count("conn.commit()") == 1,
           f"commits={sync_source.count('conn.commit()')}")
    # M4 added exactly one further commit, on a DIFFERENT connection: the
    # durable request facts that make `client_trips.first_seen_request_id`
    # resolvable, written to the platform database before the business
    # transaction commits. It is named distinctly so the count above keeps
    # meaning "business commits", and it is still not a coverage write — the
    # coverage-surface checks above cover that and remain green.
    _check("boundary: the only other commit is the M4 platform request-fact write",
           sync_source.count("platform_db.commit()") == 1,
           f"platform_commits={sync_source.count('platform_db.commit()')}")
    _check("boundary: the request-fact write happens before the business commit",
           sync_source.index("platform_db.commit()")
           < sync_source.index("            conn.commit()"),
           "a request fact must be durable before a trip can reference it")


# --------------------------------------------------------------------------
# Mode propagation (docs/14 §8 S8a)
# --------------------------------------------------------------------------

def test_sync_normalizes_the_supplied_mode() -> None:
    _check("propagation: absent parameter resolves to strict_meta",
           job._trips_pagination_mode({}) == TRIPS_PAGINATION_MODE_STRICT_META)
    _check("propagation: explicit null resolves to strict_meta",
           job._trips_pagination_mode({"trips_pagination_mode": None}) == TRIPS_PAGINATION_MODE_STRICT_META)
    _check("propagation: explicit strict_meta is preserved",
           job._trips_pagination_mode({"trips_pagination_mode": "strict_meta"})
           == TRIPS_PAGINATION_MODE_STRICT_META)
    _check("propagation: data_invariants_v1 is preserved",
           job._trips_pagination_mode({"trips_pagination_mode": "data_invariants_v1"})
           == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)
    for bad in ("data_invariants_v2", "", " data_invariants_v1 ", 1, True):
        try:
            job._trips_pagination_mode({"trips_pagination_mode": bad})
            _check(f"propagation: invalid job param {bad!r} must fail closed", False, "no error")
        except ValueError:
            _check(f"propagation: invalid job param {bad!r} fails closed", True)


def test_sync_run_passes_the_mode_to_the_provider_client() -> None:
    """`run()` hands the normalized mode to the provider client and fails before any write."""
    recorded: dict = {}
    sentinel = TelematicsProviderSafetyError("PAGINATION_COMPAT_PAGE_OVERLAP", "scripted stop")

    class RecordingProviderClient:
        def __init__(self, **kwargs):
            recorded.update(kwargs)
            self.page_limit = kwargs.get("page_limit")

        def metrics_snapshot(self):
            return {"total_requests": 0}

        def fetch_trips(self, **_kwargs):
            raise sentinel

    class FakeLogClient:
        def __init__(self):
            self.records: list = []

        def log(self, level, kind, source, message, run_id=None, context=None):
            self.records.append((level, message, dict(context or {})))

    cfg = types.SimpleNamespace(
        client_id="00000000-0000-0000-0000-000000000001",
        client_code="TEST00001",
        provider_base_url="https://fleet.example.test",
        provider_basic_auth_username="user",
        provider_basic_auth_password_secret_ref="TEST_PROVIDER_SECRET",
        trip_metrics_population_source="api_migration",
        trips_pagination_mode="data_invariants_v1",
    )
    schedule = types.SimpleNamespace(exists=True, enabled=True, overwrite_existing=True)

    originals = {
        "TelematicsFleetProviderClient": job.TelematicsFleetProviderClient,
        "load_client_account_config": job.load_client_account_config,
        "load_dataset_schedule": job.load_dataset_schedule,
        "resolve_secret": job.resolve_secret,
        "_client_business_pg_conn": job._client_business_pg_conn,
    }
    opened: list = []
    job.TelematicsFleetProviderClient = RecordingProviderClient  # type: ignore[assignment]
    job.load_client_account_config = lambda *, client_id: cfg  # type: ignore[assignment]
    job.load_dataset_schedule = lambda *, client_id, dataset_name: schedule  # type: ignore[assignment]
    job.resolve_secret = lambda ref: "secret-value"  # type: ignore[assignment]

    def _forbidden_conn(_cfg):
        opened.append(_cfg)
        raise AssertionError("client-business connection opened despite a provider safety stop")

    job._client_business_pg_conn = _forbidden_conn  # type: ignore[assignment]

    log_client = FakeLogClient()
    params = {
        "client_id": cfg.client_id,
        "window_start_ts": "2026-07-20T00:00:00Z",
        "window_end_ts": "2026-07-21T00:00:00Z",
        "trips_pagination_mode": "data_invariants_v1",
    }
    try:
        try:
            job.run(log_client, "run-1", params)
            _check("propagation: a provider safety stop must end the run", False, "run returned")
        except TelematicsProviderSafetyError as exc:
            _check("propagation: the provider safety stop ends the run before any business write",
                   exc.code == "PAGINATION_COMPAT_PAGE_OVERLAP" and not opened,
                   f"code={exc.code} opened={len(opened)}")
        _check("propagation: run() passes the normalized mode at provider-client construction",
               recorded.get("trips_pagination_mode") == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
               f"recorded={recorded.get('trips_pagination_mode')!r}")
        modes_logged = {
            c.get("trips_pagination_mode")
            for _l, _m, c in log_client.records
            if "trips_pagination_mode" in c
        }
        _check("propagation: the resolved mode appears in the run log contract",
               modes_logged == {TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1}, f"modes={modes_logged}")

        # A run without the parameter stays strict.
        recorded.clear()
        strict_params = dict(params)
        strict_params.pop("trips_pagination_mode")
        try:
            job.run(FakeLogClient(), "run-2", strict_params)
        except TelematicsProviderSafetyError:
            pass
        _check("propagation: a run without the parameter constructs a strict client",
               recorded.get("trips_pagination_mode") == TRIPS_PAGINATION_MODE_STRICT_META,
               f"recorded={recorded.get('trips_pagination_mode')!r}")

        # An unknown mode fails closed before any provider client exists.
        recorded.clear()
        unknown_params = dict(params)
        unknown_params["trips_pagination_mode"] = "loose"
        try:
            job.run(FakeLogClient(), "run-3", unknown_params)
            _check("propagation: an unknown job-param mode fails closed", False, "run returned")
        except ValueError:
            _check("propagation: an unknown job-param mode fails closed before any provider client",
                   not recorded, f"recorded={recorded}")
    finally:
        for name, value in originals.items():
            setattr(job, name, value)


def test_dispatcher_and_recovery_emit_the_mode_parameter() -> None:
    from jobs.api.telematics import dispatcher  # noqa: PLC0415

    compat_params = dispatcher._build_job_params(
        client_id="c1", client_code="BRAVO00016", dataset_name="trips_sync",
        event_enrichment_mode="enabled",
        window_start_ts=WINDOW_START, window_end_ts=WINDOW_END,
        scheduled_fire_ts=WINDOW_END,
        nominal_window_start_ts=WINDOW_START, nominal_window_end_ts=WINDOW_END,
        trips_stabilization_delay_seconds=10800, trips_overlap_seconds=1,
        trips_pagination_mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    )
    _check("propagation: the dispatcher emits trips_pagination_mode for a compatibility fire",
           compat_params.get("trips_pagination_mode") == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
           f"params={compat_params}")
    _check("propagation: the dispatcher-emitted mode normalizes to compatibility in the job",
           job._trips_pagination_mode(compat_params) == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)

    strict_params = dispatcher._build_job_params(
        client_id="c1", client_code="DELTA00001", dataset_name="trips_sync",
        event_enrichment_mode="enabled",
        window_start_ts=WINDOW_START, window_end_ts=WINDOW_END,
        scheduled_fire_ts=WINDOW_END,
    )
    _check("propagation: a strict fire emits no mode parameter and resolves to strict_meta",
           "trips_pagination_mode" not in strict_params
           and job._trips_pagination_mode(strict_params) == TRIPS_PAGINATION_MODE_STRICT_META,
           f"params={strict_params}")

    recovery_source = (REPO_ROOT / "ops/recover_telematics_trips_window.py").read_text(encoding="utf-8")
    _check("propagation: the C11 recovery runner still emits the frozen compatibility mode",
           '"trips_pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1' in recovery_source)


# --------------------------------------------------------------------------
# Generated sequences (docs/12 T35)
# --------------------------------------------------------------------------

def test_generated_sequences_never_return_duplicates_or_partial_windows() -> None:
    rng = random.Random(20260803)
    limit = 10
    violations: list[str] = []
    successes = 0
    aborts = 0

    for case in range(300):
        pages: list = []
        next_id = 1
        page_count = rng.randint(1, 6)
        emitted: list[int] = []
        for _page in range(page_count):
            size = limit if rng.random() < 0.6 else rng.randint(0, limit - 1)
            ids = list(range(next_id, next_id + size))
            next_id += size
            perturbation = rng.random()
            if perturbation < 0.12 and emitted:
                ids = ids[:-1] + [rng.choice(emitted)] if ids else [rng.choice(emitted)]
            elif perturbation < 0.18 and len(ids) >= 2:
                ids[-1] = ids[0]
            elif perturbation < 0.22:
                ids = ids + [next_id]
                next_id += 1
            emitted.extend(i for i in ids if i not in emitted)
            total = ABSENT if rng.random() < 0.5 else len(emitted)
            pages.append(_payload(_rows(ids), total))
            if size < limit and perturbation >= 0.22:
                break
        # Guarantee the scripted session can always answer one more request.
        pages.append(_payload([], ABSENT))

        client, _session = _client(pages, page_limit=limit)
        try:
            rows = _fetch(client)
        except TelematicsProviderSafetyError:
            aborts += 1
            continue
        except AssertionError as exc:  # scripted session exhausted
            violations.append(f"case {case}: {exc}")
            continue
        successes += 1
        ids = [r["trip_id"] for r in rows]
        if len(set(ids)) != len(ids):
            violations.append(f"case {case}: duplicate identity in a successful result")
        if ids != sorted(ids):
            # Generated pages are ascending, so a successful result must be too.
            violations.append(f"case {case}: provider order not preserved")

    _check("T35: every generated sequence either succeeds duplicate-free or aborts",
           not violations, f"violations={violations[:3]}")
    _check("T35: the generated matrix exercised both outcomes",
           successes > 0 and aborts > 0, f"successes={successes} aborts={aborts}")


def main() -> int:
    for name in COMPAT_ENV_NAMES:
        os.environ.pop(name, None)

    test_strict_healthy_metadata_multi_page()
    test_strict_current_page_behind_is_pagination_mismatch()
    test_strict_current_page_ahead_is_pagination_mismatch()
    test_strict_tolerates_matching_integer_like_strings()
    test_strict_partial_and_missing_metadata()
    test_strict_terminates_on_last_page_zero()
    test_strict_is_the_default_mode()
    test_non_trips_endpoints_stay_strict_under_compatibility_mode()

    test_compat_short_first_page()
    test_compat_empty_first_page_absent_total()
    test_compat_empty_first_page_total_zero()
    test_compat_full_pages_then_short_page()
    test_compat_full_pages_then_empty_page()
    test_compat_exact_multiple_single_page()
    test_compat_ignores_contradictory_metadata()
    test_compat_absent_total_multi_page()
    test_compat_meta_absent_entirely()

    test_compat_identity_failures()
    test_compat_malformed_response_shapes()
    test_compat_duplicate_and_overlap_failures()
    test_compat_repeat_failures()
    test_compat_page_larger_than_limit()
    test_compat_total_invalid_variants()
    test_compat_total_unstable_variants()
    test_compat_total_bound_and_reconciliation()
    test_compat_absent_total_code_never_exists()
    test_compat_budget_failures()
    test_compat_invalid_configuration_fails_closed_before_any_request()
    test_compat_unknown_mode_fails_closed()

    test_compat_never_returns_partially_validated_pages()
    test_compat_logs_are_sanitized()
    test_c7_writes_no_coverage_state()

    test_sync_normalizes_the_supplied_mode()
    test_sync_run_passes_the_mode_to_the_provider_client()
    test_dispatcher_and_recovery_emit_the_mode_parameter()

    test_generated_sequences_never_return_duplicates_or_partial_windows()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nOK - Telematics /trips compatibility pagination manual tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
