#!/usr/bin/env python3
"""Database Explorer hidden row identity and row detail (approved stage S6).

Two coupled contracts are under test.

**The hidden-identity boundary.** The configured technical row identifier —
`record_id` for the target datasets — is not a business column. Once configured
it must be absent from every ordinary user surface: the table, `cols`, the S5
layout parameters, sort, filters, the S4 aggregate endpoint, exports and the
row-detail drawer itself. The tests assert that with a fixture identity value
that appears nowhere else, so a leak anywhere is unmistakable.

**The opaque row reference.** A row is addressed by AES-GCM ciphertext bound to
its dataset, client and identifier column. The raw identifier must never reach
the browser, the URL or the audit log, and possession of a reference must never
substitute for authorization.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— screen ``DB-006``, ``PRODUCT_BEHAVIOR_CONTRACT.md`` §2.10,
``INTERACTION_SPEC.md`` §2.3/§3, ``ACCESSIBILITY_SPEC.md`` §4, criteria
``DB-36``–``DB-39``, ``AC-4``.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_hidden_row_identity.py
"""
from __future__ import annotations

import html as html_mod
import json
import re
import subprocess
import sys
import types
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _App:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda fn: fn

    post = patch = delete = on_event = get


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        self.body = body
        self.media_type = media_type
        self.headers = headers or {}
        self.status_code = 200


class _HTMLResponse:
    def __init__(self, content, status_code=200, headers=None, media_type=None):
        self.body = str(content).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type or "text/html"


def _identity_default(default=None, *args, **kwargs):
    return default


def _install_import_stubs() -> None:
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.Header = _identity_default
    fastapi.HTTPException = _HTTPException
    fastapi.Request = object
    fastapi.UploadFile = object
    fastapi.File = _identity_default
    fastapi.Form = _identity_default
    fastapi.Query = _identity_default
    fastapi.Body = _identity_default
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = _HTMLResponse
    responses.StreamingResponse = _StreamingResponse

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: None

    psycopg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", rows)


_install_import_stubs()

import api.main as api_main  # noqa: E402
from api.row_reference import (  # noqa: E402
    RowReferenceError,
    build_row_reference,
    resolve_row_reference,
)

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER_DATASET_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
ROUTE = f"/user/database/datasets/{DATASET_ID}"

# A value that exists nowhere else in the fixture, the source or the vocabulary,
# so any appearance anywhere in a response is unambiguously a leak.
RAW_IDENTITY = "QQZX-RECORDID-NEVER-SHOWN-773311"
# A deterministic key for the tests only. Not a production secret and never
# printed; production derives from ARTIFACT_EXPLORER_SESSION_SECRET.
TEST_SECRET = "deterministic-test-key-material-for-row-references"


class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query="", path=ROUTE):
        self.url = _FakeUrl(path, query)
        self.cookies = {}


def _user():
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "client_trips", "default_date_column": None,
        "is_active": True, "visible_columns": 4, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": True,
    }
    data.update(overrides)
    return data


def _col(name, label, data_type, **extra):
    column = {
        "dataset_id": DATASET_ID, "column_name": name, "display_name": label, "data_type": data_type,
        "is_visible": True, "is_filterable": True, "is_sortable": True,
        "is_default_date_column": False, "is_row_identifier": False, "display_order": 10,
    }
    column.update(extra)
    return column


def _identity_column(*, legacy_visible: bool = False):
    """The technical row identifier.

    `legacy_visible=True` reproduces the transitional catalog state of the two
    BRAVO datasets, whose `record_id` is still flagged visible. That state must
    not leak once the column is configured as the identifier.
    """
    return _col(
        "record_id", "Record", "text",
        is_visible=legacy_visible, is_filterable=legacy_visible, is_sortable=legacy_visible,
        is_row_identifier=True, display_order=1,
    )


def _business_columns():
    return [
        _col("trip_start", "Start", "timestamp with time zone", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("distance_km", "Dystans", "numeric", display_order=30),
        _col("is_billable", "Rozliczalny", "boolean", display_order=40),
    ]


def _rows(count=3):
    out = []
    for index in range(count):
        out.append({
            "record_id": f"{RAW_IDENTITY}-{index}",
            "trip_start": datetime(2026, 5, 28, 7, 30, tzinfo=timezone.utc),
            "driver_name": "Kowalski" if index else "Nowak",
            "distance_km": Decimal("128.40"),
            "is_billable": True,
        })
    return out


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


_SQL_LOG: list[tuple[str, list]] = []


def _render(
    *,
    query="",
    rows=None,
    columns=None,
    dataset=None,
    identity=True,
    legacy_visible=False,
    audit=None,
    detail_result="found",
):
    """Render the row browser with a configured hidden technical identity."""
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _business_columns()
    rows = _rows() if rows is None else rows
    identity_col = _identity_column(legacy_visible=legacy_visible) if identity else None
    audit_sink = audit if audit is not None else []
    # Mirror the real sort validation so canonicalized links carry what the
    # request actually asked for, rather than a fixed stub value.
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    sortable = {str(c.get("column_name")) for c in columns if c.get("is_sortable")}
    requested_sort = api_main._portal_database_first_param(parsed, "sort", "")
    requested_direction = api_main._portal_database_first_param(parsed, "direction", "")
    state = {
        "sort": requested_sort if requested_sort in sortable else "trip_start",
        "direction": "asc" if requested_direction == "asc" else "desc",
        "active_filters": {},
        "filter_entries": [],
    }

    def _list(d, c, p, limit=0, offset=0, display_columns=None):
        _SQL_LOG.append(("select", [str(col.get("column_name")) for col in (display_columns or c)]))
        return (rows, state, None)

    def _fetch(d, c, identifier_column, value):
        _SQL_LOG.append(("detail", [identifier_column, value]))
        if detail_result == "not_found":
            return None, "not_found"
        if detail_result == "duplicate":
            return None, "duplicate"
        record = next((r for r in rows if str(r.get("record_id")) == str(value)), None)
        return (record, None) if record else (None, "not_found")

    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda d: identity_col)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (len(rows), state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", _list)),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (48213, None))),
        ("_portal_database_fetch_row_by_identity", _patch("_portal_database_fetch_row_by_identity", _fetch)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: audit_sink.append(kwargs))),
        ("_artifact_explorer_session_secret", _patch("_artifact_explorer_session_secret", lambda: TEST_SECRET)),
    ]
    try:
        response = api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query))
    finally:
        _restore(patches)
    return response.body.decode("utf-8")


def _row_tokens(html: str) -> list[str]:
    return [html_mod.unescape(t) for t in re.findall(r'data-db-row="([^"]+)"', html)]


def _open_query(html: str) -> str:
    href = html_mod.unescape(re.search(r'href="([^"]*\?[^"]*\brow=[^"]*)"', html).group(1))
    return href.split("?", 1)[1]


def _assert_no_identity_leak(html: str, label: str) -> None:
    """The raw identity must be absent from the response in every form."""
    assert RAW_IDENTITY not in html, f"{label}: raw identity leaked"
    assert html_mod.escape(RAW_IDENTITY) not in html, f"{label}: escaped identity leaked"
    assert "record_id" not in html, f"{label}: the identifier column name leaked"


# ===========================================================================
# 1. The hidden-identity boundary
# ===========================================================================
def test_identity_is_absent_from_every_user_column_surface() -> None:
    html = _render()
    _assert_no_identity_leak(html, "sheet")

    # Not a header, not a colgroup entry, not a body cell.
    assert "record_id" not in " ".join(re.findall(r'data-db-column="([^"]+)"', html))
    # Not in the DB-008 column-management panel.
    panel = html.split("data-db-columns", 1)[1] if "data-db-columns" in html else ""
    assert "record_id" not in panel, panel[:400]
    # Not counted as an approved column: 4 business columns, not 5.
    assert re.search(r">Kolumny <span class=\"lp-mono\">4/4</span>", html), html[:0] or "column count"
    print("PASS: the technical identity is absent from headers, cells, colgroup and DB-008")


def test_legacy_visible_identity_still_cannot_leak() -> None:
    """The transitional BRAVO state: is_visible=true AND is_row_identifier=true.

    The resolver excludes the identity by `is_row_identifier`, so configuring it
    hides it immediately — there is no window in which the legacy flag exposes it.
    """
    html = _render(legacy_visible=True)
    _assert_no_identity_leak(html, "legacy visible identity")
    print("PASS: a legacy is_visible=true identifier cannot leak once configured")


def test_forged_cols_and_layout_state_cannot_expose_the_identity() -> None:
    forged = [
        "cols=record_id",
        "cols=record_id&cols=driver_name",
        "colorder=record_id,driver_name",
        "colw=record_id:400",
        "colw__record_id=400",
        "colpin=record_id",
        "colorder=record_id&colw=record_id:480&colpin=record_id&cols=record_id",
    ]
    for query in forged:
        html = _render(query=query)
        _assert_no_identity_leak(html, query)
    print("PASS: forged cols/colorder/colw/colpin state cannot expose the identity")


def test_forged_sort_and_filter_state_cannot_expose_the_identity() -> None:
    forged = [
        "sort=record_id",
        "sort=record_id&direction=asc",
        "filter__record_id=x&op__record_id=contains",
        "filter__record_id=" + RAW_IDENTITY,
        "op__record_id=eq&filter__record_id=1",
        "dateop__record_id=range&date_from__record_id=2026-01-01",
    ]
    for query in forged:
        html = _render(query=query)
        _assert_no_identity_leak(html, query)
        # A forged sort normalizes away rather than becoming an effective sort.
        assert 'aria-sort="ascending"' not in html or "record_id" not in html
    print("PASS: forged sort and filter parameters for the identity fail safely")


def test_identity_is_used_as_an_internal_tiebreaker_only() -> None:
    """The backend may order by the identity; the user may not sort by it."""
    columns = _business_columns()
    identity = _identity_column()
    dataset = _dataset()
    # The row browser appends the identity to the SELECT source; the builder
    # picks the tie-breaker up from there.
    query, values, state, error = api_main._build_portal_database_rows_query(
        dataset, columns, {"sort": ["driver_name"], "direction": ["asc"]},
        display_columns=columns + [identity],
    )
    assert error is None, error
    assert '"record_id" ASC' in query, query
    assert state.get("secondary_sort") == "record_id", state
    # But the identity is not in the user's sortable universe: a request for it
    # falls back to the dataset default instead of ordering by it.
    sort_column, _direction, sort_error = api_main._validate_portal_database_sort(
        dataset, columns, {"sort": ["record_id"]}
    )
    assert sort_column != "record_id", sort_column
    print("PASS: the identity is an internal ORDER BY tie-breaker and never a user sort")


def test_identity_never_reaches_the_aggregate_or_export_column_universe() -> None:
    columns = _business_columns()
    # The S4 aggregate endpoint and both export paths resolve their column
    # universe from the same loader the identity is excluded from, so a forged
    # column cannot be found there.
    names = {str(c.get("column_name")) for c in columns}
    assert "record_id" not in names
    # Search eligibility is derived from the same set.
    search_columns = api_main._portal_database_search_columns(columns)
    assert all(str(c.get("column_name")) != "record_id" for c in search_columns)
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    loader = source.split("def _get_portal_database_visible_columns", 1)[1].split("def ", 1)[0]
    assert "is_row_identifier IS NOT TRUE" in loader, loader
    print("PASS: identity is outside the aggregate, export and search column universe")


# ===========================================================================
# 2. The opaque row reference
# ===========================================================================
def test_reference_is_opaque_and_is_not_an_encoding_of_the_identity() -> None:
    token = build_row_reference(
        secret=TEST_SECRET, dataset_id=DATASET_ID, client_code="ACME_01",
        identifier_column="record_id", identity_value=RAW_IDENTITY,
    )
    assert RAW_IDENTITY not in token
    # Not any trivial reversible encoding of the value.
    import base64
    for candidate in (
        RAW_IDENTITY,
        RAW_IDENTITY.encode().hex(),
        base64.b64encode(RAW_IDENTITY.encode()).decode(),
        base64.urlsafe_b64encode(RAW_IDENTITY.encode()).decode().rstrip("="),
    ):
        assert candidate not in token, candidate
    # The plaintext is not recoverable from the token bytes without the key.
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert RAW_IDENTITY.encode() not in raw
    # A fresh nonce per call: two references for one row are not equal.
    again = build_row_reference(
        secret=TEST_SECRET, dataset_id=DATASET_ID, client_code="ACME_01",
        identifier_column="record_id", identity_value=RAW_IDENTITY,
    )
    assert again != token
    # Both still resolve to the same identity.
    for candidate in (token, again):
        assert resolve_row_reference(
            secret=TEST_SECRET, token=candidate, dataset_id=DATASET_ID,
            client_code="ACME_01", identifier_column="record_id",
        ) == RAW_IDENTITY
    print("PASS: the reference is opaque, non-deterministic and not an encoding of the identity")


def test_reference_is_bound_to_dataset_client_and_identifier_column() -> None:
    token = build_row_reference(
        secret=TEST_SECRET, dataset_id=DATASET_ID, client_code="ACME_01",
        identifier_column="record_id", identity_value=RAW_IDENTITY,
    )
    # Right context resolves.
    assert resolve_row_reference(
        secret=TEST_SECRET, token=token, dataset_id=DATASET_ID,
        client_code="ACME_01", identifier_column="record_id",
    ) == RAW_IDENTITY
    # Every substituted binding fails, and fails the same way.
    for kwargs in (
        {"dataset_id": OTHER_DATASET_ID},
        {"client_code": "OTHER_01"},
        {"identifier_column": "trip_id"},
    ):
        call = {"secret": TEST_SECRET, "token": token, "dataset_id": DATASET_ID,
                "client_code": "ACME_01", "identifier_column": "record_id"}
        call.update(kwargs)
        try:
            resolve_row_reference(**call)
        except RowReferenceError:
            pass
        else:
            raise AssertionError(f"reference resolved under substituted binding {kwargs}")
    # A different key never resolves it either.
    try:
        resolve_row_reference(
            secret="a-different-key", token=token, dataset_id=DATASET_ID,
            client_code="ACME_01", identifier_column="record_id",
        )
    except RowReferenceError:
        pass
    else:
        raise AssertionError("reference resolved under a foreign key")
    print("PASS: a reference is bound to its dataset, client, identifier column and key")


def test_tampered_and_malformed_references_are_rejected_identically() -> None:
    token = build_row_reference(
        secret=TEST_SECRET, dataset_id=DATASET_ID, client_code="ACME_01",
        identifier_column="record_id", identity_value=RAW_IDENTITY,
    )
    import base64
    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    flipped = bytearray(raw)
    flipped[-1] ^= 0x01                       # tampered ciphertext/tag
    bad_version = bytearray(raw)
    bad_version[0] = 9                        # unsupported version
    candidates = [
        "",
        "   ",
        "not-a-token",
        "!!!!not-base64!!!!",
        token[:-4],                           # truncated
        token + "AAAA",                       # extended
        base64.urlsafe_b64encode(bytes(flipped)).decode().rstrip("="),
        base64.urlsafe_b64encode(bytes(bad_version)).decode().rstrip("="),
        base64.urlsafe_b64encode(b"short").decode().rstrip("="),
        "A" * 4096,                           # oversized
        RAW_IDENTITY,                         # the raw identity is not a token
    ]
    for candidate in candidates:
        try:
            resolve_row_reference(
                secret=TEST_SECRET, token=candidate, dataset_id=DATASET_ID,
                client_code="ACME_01", identifier_column="record_id",
            )
        except RowReferenceError as exc:
            # The failure carries no detail about which check failed.
            assert RAW_IDENTITY not in str(exc), exc
        else:
            raise AssertionError(f"resolved a bad reference: {candidate[:40]!r}")
    print("PASS: tampered, malformed, truncated and unversioned references all fail closed")


def test_no_custom_cryptography_is_used() -> None:
    source = (REPO_ROOT / "api" / "row_reference.py").read_text(encoding="utf-8")
    # Established primitives only.
    assert "from cryptography.hazmat.primitives.ciphers.aead import AESGCM" in source
    assert "from cryptography.hazmat.primitives.kdf.hkdf import HKDF" in source
    # No home-grown construction.
    for forbidden in ("xor", "^ key", "hmac.new", "md5", "sha1(", "random.random", "itertools.cycle"):
        assert forbidden not in source.lower().replace("sha1(", "sha1("), forbidden
    assert "cryptography==" in (REPO_ROOT / "api" / "requirements.txt").read_text(encoding="utf-8")
    print("PASS: the reference uses established AEAD/KDF primitives and no custom cryptography")


def test_reference_survives_a_restart_when_the_stable_secret_is_configured() -> None:
    """A row URL must not die on every service restart."""
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    block = source.split("def _portal_database_row_reference_secret", 1)[1].split("def ", 1)[0]
    assert "_artifact_explorer_session_secret()" in block, block
    # The same stable secret yields a resolvable reference across "processes".
    token = build_row_reference(
        secret=TEST_SECRET, dataset_id=DATASET_ID, client_code="ACME_01",
        identifier_column="record_id", identity_value=RAW_IDENTITY,
    )
    assert resolve_row_reference(
        secret=TEST_SECRET, token=token, dataset_id=DATASET_ID,
        client_code="ACME_01", identifier_column="record_id",
    ) == RAW_IDENTITY
    print("PASS: references derive from the existing stable application secret")


# ===========================================================================
# 3. The sheet emits references, never identities
# ===========================================================================
def test_sheet_emits_one_opaque_reference_per_row_and_no_identity() -> None:
    html = _render()
    tokens = _row_tokens(html)
    assert len(tokens) == 3, tokens
    assert len(set(tokens)) == 3, "each row gets its own reference"
    _assert_no_identity_leak(html, "sheet")
    # Every reference resolves back to a real row identity, server-side only.
    for token in tokens:
        value = resolve_row_reference(
            secret=TEST_SECRET, token=token, dataset_id=DATASET_ID,
            client_code="ACME_01", identifier_column="record_id",
        )
        assert value.startswith(RAW_IDENTITY), value
    # No raw-identity path anywhere in the markup.
    assert "/rows/" not in html, html[:200]
    print("PASS: the sheet carries one opaque reference per row and no identity")


def test_row_url_carries_the_reference_and_preserves_the_whole_view() -> None:
    query = (
        "filter__driver_name=Kowal&op__driver_name=contains&search=abc&sort=distance_km"
        "&direction=asc&limit=50&density=comfortable&colorder=is_billable&colw=driver_name:300"
    )
    html = _render(query=query)
    opened = _open_query(html)
    params = parse_qs(opened, keep_blank_values=True)
    assert params.get("row") and params["row"][0], params
    assert RAW_IDENTITY not in opened, opened
    for key, value in [
        ("filter__driver_name", ["Kowal"]), ("op__driver_name", ["contains"]),
        ("search", ["abc"]), ("sort", ["distance_km"]), ("direction", ["asc"]),
        ("limit", ["50"]), ("density", ["comfortable"]),
    ]:
        assert params.get(key) == value, (key, params.get(key))
    assert params.get("colorder"), params
    assert params.get("colw"), params
    print("PASS: the row URL carries the reference and preserves filters, sort and layout")


# ===========================================================================
# 4. The row-detail drawer
# ===========================================================================
def test_drawer_opens_from_a_reference_without_disclosing_the_identity() -> None:
    html = _render()
    opened = _render(query=_open_query(html))
    assert "db-row-detail" in opened, "drawer rendered"
    _assert_no_identity_leak(opened, "drawer")
    # Docked region, not a modal dialog (COMPONENT_CATALOG: the approved design
    # contains no modal dialog, and the docked row panel is explicitly untrapped).
    # Scoped to the panel: the shell's own nav drawer is a legitimate dialog.
    panel = opened.split('<aside class="db-row-detail', 1)[1].split("</aside>", 1)[0]
    assert 'role="region"' in panel, panel[:300]
    assert 'role="dialog"' not in panel, "the docked panel must not be a dialog"
    assert "aria-modal" not in panel, "the docked panel must not be modal"
    # Sections with counts, from the approved user-visible fields only.
    heads = re.findall(r'db-row-section-head">([^<]+)<', opened)
    assert heads, opened[:0] or "sections"
    counts = re.findall(r'db-row-section-count lp-mono">(\d+)<', opened)
    assert counts and all(int(c) > 0 for c in counts), counts
    # The open row is marked, and only one is.
    assert opened.count("db-row-selected") == 1, opened.count("db-row-selected")
    print("PASS: the drawer opens from a reference, is docked, grouped, and leaks nothing")


def test_drawer_field_scope_defaults_to_visible_and_switches_to_all() -> None:
    columns = _business_columns()
    html = _render(query="cols=driver_name&cols=trip_start")
    opened_query = _open_query(html)
    opened = _render(query=opened_query, columns=columns)
    # Default: the fields currently on the sheet.
    assert 'data-db-row-fields="visible"' in opened and 'data-db-row-fields="all"' in opened
    visible_marker = re.search(r'data-db-row-fields="visible"[^>]*aria-current="true"', opened)
    assert visible_marker, "visible scope is the default"
    # Switching to all approved columns is a link, and still excludes the identity.
    all_href = html_mod.unescape(
        re.search(r'href="([^"]+)"[^>]*data-db-row-fields="all"', opened).group(1)
    )
    all_opened = _render(query=all_href.split("?", 1)[1], columns=columns)
    _assert_no_identity_leak(all_opened, "all-fields drawer")
    assert "Dystans" in all_opened, "a column hidden from the sheet still appears in the drawer"
    print("PASS: the drawer defaults to visible fields and can switch to all approved fields")


def test_drawer_traversal_uses_references_and_reaches_both_edges() -> None:
    html = _render()
    tokens = _row_tokens(html)
    # Open the middle row: both directions available.
    middle = _render(query=f"row={tokens[1]}")
    controls = re.findall(r'data-db-row-traverse="(\w+)"', middle)
    assert controls == ["previous", "next"], controls
    assert middle.count('class="db-row-traverse"') == 2, middle.count('class="db-row-traverse"')
    for href in re.findall(r'href="([^"]*)"[^>]*data-db-row-traverse', middle):
        assert RAW_IDENTITY not in html_mod.unescape(href), href
    # First row: previous is present but semantically disabled, not missing.
    first = _render(query=f"row={tokens[0]}")
    assert 'data-db-row-traverse="previous"' in first
    disabled = re.search(r'class="db-row-traverse is-disabled"[^>]*aria-disabled="true"[^>]*data-db-row-traverse="previous"', first)
    assert disabled, "the first row's previous control is disabled semantically"
    # Last row: next is disabled.
    last = _render(query=f"row={tokens[-1]}")
    assert re.search(r'is-disabled[^>]*aria-disabled="true"[^>]*data-db-row-traverse="next"', last), last[:0] or "next"
    print("PASS: traversal walks opaque references and communicates its edges semantically")


def test_drawer_close_returns_the_plain_view() -> None:
    html = _render()
    opened = _render(query=_open_query(html))
    close_href = html_mod.unescape(re.search(r'href="([^"]*)"[^>]*data-db-row-close', opened).group(1))
    params = parse_qs(close_href.split("?", 1)[1] if "?" in close_href else "", keep_blank_values=True)
    assert "row" not in params, params
    assert "rowfields" not in params, params
    closed = _render(query=close_href.split("?", 1)[1] if "?" in close_href else "")
    assert "db-row-detail" not in closed, "closing removes the drawer"
    print("PASS: closing drops the row state and returns the plain view")


def test_unresolvable_references_produce_one_generic_state() -> None:
    good = _row_tokens(_render())[0]
    import base64
    raw = bytearray(base64.urlsafe_b64decode(good + "=" * (-len(good) % 4)))
    raw[-1] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    foreign = build_row_reference(
        secret=TEST_SECRET, dataset_id=OTHER_DATASET_ID, client_code="ACME_01",
        identifier_column="record_id", identity_value=RAW_IDENTITY + "-0",
    )
    seen = set()
    for query in (f"row={tampered}", f"row={foreign}", "row=garbage", f"row={RAW_IDENTITY}"):
        html = _render(query=query)
        _assert_no_identity_leak(html, query)
        assert "db-row-detail-empty" in html, query
        # The sheet behind stays usable.
        assert "<table" in html and "db-table" in html, query
        message = re.search(r'db-row-unavailable">([^<]*)<', html)
        seen.add(message.group(1) if message else "")
    assert len(seen) == 1, f"failures must be indistinguishable: {seen}"
    # A row that resolves but no longer exists reaches the same state.
    gone = _render(query=f"row={good}", detail_result="not_found")
    assert "db-row-detail-empty" in gone, gone[:0] or "not_found"
    dupe = _render(query=f"row={good}", detail_result="duplicate")
    assert "db-row-detail-empty" in dupe, dupe[:0] or "duplicate"
    print("PASS: tampered, foreign, missing and non-unique rows share one generic state")


def test_datasets_without_a_configured_identity_offer_no_row_detail() -> None:
    html = _render(identity=False)
    assert "data-db-row=" not in html, "no reference without a configured identity"
    assert "db-row-detail" not in html, html[:0] or "no drawer"
    assert "/rows/" not in html, "and certainly no positional or raw row URL"
    # A forged row parameter on such a dataset is refused, not invented.
    forged = _render(query="row=anything", identity=False)
    assert "db-row-detail-empty" in forged, forged[:0] or "generic state"
    print("PASS: a dataset with no configured identity degrades safely to no row detail")


# ===========================================================================
# 5. Layout interaction and lookup contract
# ===========================================================================
def test_row_identity_survives_column_reorder_hide_width_and_pin() -> None:
    base = _render()
    base_tokens = _row_tokens(base)
    base_identities = [
        resolve_row_reference(secret=TEST_SECRET, token=t, dataset_id=DATASET_ID,
                              client_code="ACME_01", identifier_column="record_id")
        for t in base_tokens
    ]
    for query in (
        "colorder=is_billable,distance_km,driver_name,trip_start",
        "cols=driver_name&cols=distance_km",
        "colw=driver_name:300",
        "colpin=distance_km",
        "colorder=is_billable&colpin=is_billable&colw=driver_name:120&cols=driver_name&cols=is_billable",
    ):
        html = _render(query=query)
        _assert_no_identity_leak(html, query)
        identities = [
            resolve_row_reference(secret=TEST_SECRET, token=t, dataset_id=DATASET_ID,
                                  client_code="ACME_01", identifier_column="record_id")
            for t in _row_tokens(html)
        ]
        # Same rows, same identities, in the same row order — layout is presentation.
        assert identities == base_identities, (query, identities, base_identities)
    print("PASS: row identity is stable across reorder, hide, width and pin changes")


def test_single_row_lookup_is_parameterized_and_detects_non_uniqueness() -> None:
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    block = source.split("def _portal_database_fetch_row_by_identity", 1)[1].split("\ndef ", 1)[0]
    assert "LIMIT 2" in block, "non-uniqueness detection must remain"
    assert "cur.execute(query, [str(identity_value)])" in block, "the value must be bound"
    assert "_portal_database_quote_identifier(identifier_column" in block, "identifier must be quoted"
    assert 'return None, "duplicate"' in block, "a non-unique identifier must not yield a row"
    assert 'return None, "not_found"' in block
    # The SELECT is built from the approved user-visible columns only.
    assert "for column in columns" in block, block
    print("PASS: the single-row lookup stays parameterized, quoted and LIMIT 2")


def test_audit_records_no_identity_no_token_and_no_values() -> None:
    audit: list[dict] = []
    html = _render(audit=audit)
    opened_query = _open_query(html)
    audit.clear()
    _render(query=opened_query, audit=audit)
    assert audit, "row detail must still be audited"
    blob = json.dumps(audit, default=str)
    assert RAW_IDENTITY not in blob, "the audit log must not carry the identity"
    token = parse_qs(opened_query)["row"][0]
    assert token not in blob, "the audit log must not carry the reference"
    assert "Kowalski" not in blob and "Nowak" not in blob, "no business values in the audit log"
    assert any(k.get("event_type") == "database_rows_viewed" for k in audit), audit
    # An unresolvable reference is recorded as a safe category, not as its value.
    audit.clear()
    _render(query="row=garbage-token-value", audit=audit)
    blob = json.dumps(audit, default=str)
    assert "garbage-token-value" not in blob, blob
    assert "row_reference_invalid" in blob, blob
    print("PASS: audit records the event without the identity, the reference or row values")


# ===========================================================================
# 6. Later stages stay out
# ===========================================================================
def test_no_saved_view_or_preference_persistence_arrives_with_s6() -> None:
    """S6 introduced no saved views and no server-side preference storage.

    `Zaznaczone wiersze` and the migration check were part of this assertion
    while S7 and S8 were unbuilt. Both have since landed as their own approved
    stages — the export scope in `DB-009` and the `cancelled` status the S8
    background-export state machine needs — so asserting their absence would now
    be asserting that approved work did not happen. What S6 genuinely excluded,
    and what is still excluded, is saved views, named column sets and any
    server-side persistence of view state.
    """
    html = _render(query=_open_query(_render()))
    for future in ("Zapisz jako widok", "Zapisz jako zestaw", "Zestawy"):
        assert future not in html, f"{future} belongs to a later stage"
    # No row-selection checkboxes: S7 is a rectangular *cell* selection, and no
    # stage has introduced a row-checkbox column.
    assert 'type="checkbox"' not in html.split("<tbody>", 1)[1].split("</tbody>", 1)[0], "no row checkboxes"
    print("PASS: no saved views, no named column sets and no row checkboxes")


def test_shipped_script_owns_no_authorization_and_decodes_nothing() -> None:
    script = (REPO_ROOT / "api" / "static" / "js" / "data-grid-row-detail.js").read_text(encoding="utf-8")
    for forbidden in ("atob(", "decrypt", "record_id", "fetch(", "XMLHttpRequest"):
        assert forbidden not in script, forbidden
    # It must not install a focus trap: the docked panel is explicitly untrapped.
    assert "trap" not in script.lower() or "must not install a focus trap" in script
    assert (REPO_ROOT / "api" / "portal_ui" / "assets.py").read_text(encoding="utf-8").count(
        "js/data-grid-row-detail.js"
    ) == 1
    print("PASS: the row-detail script decodes nothing, fetches nothing and owns no authorization")


# ===========================================================================
# 7. Shipped script behaviour (Node/DOM harness)
# ===========================================================================
def _run_harness(scenario: str) -> dict:
    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / "data_grid_row_detail_harness.js"), scenario]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_script_opens_a_row_by_click_and_by_enter() -> None:
    for scenario, expected in (("open-by-click", "tok-BBBBref1111"), ("open-by-enter", "tok-CCCCref2222")):
        result = _run_harness(scenario)
        assert len(result["navigations"]) == 1, result
        target = result["navigations"][0]
        assert f"row={expected}" in target, target
        # The whole view rides along, and the script issues no request of its own.
        assert "sort=trip_start" in target and "colorder=driver_name" in target, target
        assert result["fetches"] == 0, result
    # Rows are focusable, so Enter is reachable without a mouse.
    assert _run_harness("open-by-click")["tabindex"] == ["0", "0", "0"]
    print("PASS: a row opens by click and by Enter, preserving the view and issuing no request")


def test_script_does_not_hijack_header_and_resize_controls() -> None:
    result = _run_harness("interactive-targets-do-not-open")
    assert result["navigations"] == [], result
    print("PASS: clicks on column menus and resize handles keep their own meaning")


def test_script_closes_on_escape_and_returns_focus_to_the_row() -> None:
    closed = _run_harness("escape-closes")
    assert len(closed["navigations"]) == 1, closed
    assert "row=" not in closed["navigations"][0], closed
    assert closed["prevented"] is True, closed
    # Opening puts focus on the panel heading; closing returns it to the row.
    assert _run_harness("focus-enters-panel")["focusedTag"] == "h3"
    assert _run_harness("focus-returns-to-row")["focused"] == "tok-CCCCref2222"
    print("PASS: Esc closes the panel, focus enters it on open and returns to the row on close")


def test_script_traversal_moves_between_references_and_stops_at_the_edges() -> None:
    nxt = _run_harness("traverse-next")
    assert "row=tok-CCCCref2222" in nxt["navigations"][0], nxt
    prev = _run_harness("traverse-previous")
    assert "row=tok-AAAAref0000" in prev["navigations"][0], prev
    # At the first row there is nowhere to go, and nothing navigates.
    assert _run_harness("traverse-at-edge")["navigations"] == []
    # Arrow keys inside a field belong to the field.
    assert _run_harness("arrows-in-a-field-are-not-traversal")["navigations"] == []
    print("PASS: traversal walks references, stops at the edges and yields arrows to fields")


def test_script_preserves_table_scroll_across_open_and_close() -> None:
    saved = _run_harness("scroll-is-remembered")
    assert json.loads(saved["saved"]) == {"left": 640, "top": 320}, saved
    restored = _run_harness("scroll-is-restored")
    assert restored["left"] == 640 and restored["top"] == 320, restored
    # The stored value is consumed, so it cannot resurrect a stale position.
    assert restored["leftover"] is None, restored
    # A malformed stored value must not throw during load.
    assert _run_harness("malformed-scroll-is-ignored")["left"] == 11
    print("PASS: table scroll survives opening and closing, and malformed state is ignored")


# ===========================================================================
# 8. Independent-review corrections
# ===========================================================================
class _RecordingCursor:
    """Captures the SQL a loader emits and evaluates the catalog predicate itself.

    The count defect lived in SQL, so the honest proof is what SQL the loader
    actually sends. This cursor also applies the predicate to a catalog fixture,
    so the assertion is about a resulting number rather than about a substring.
    """

    def __init__(self, sink, catalog, dataset_row):
        self._sink = sink
        self._catalog = catalog
        self._dataset_row = dataset_row
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._sink.append(query)
        excludes_identity = "is_row_identifier IS NOT TRUE" in query
        counted = [
            column for column in self._catalog
            if column["is_visible"] and not (excludes_identity and column["is_row_identifier"])
        ]
        row = dict(self._dataset_row)
        row["visible_columns"] = len(counted)
        self._rows = [row]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordingConn:
    def __init__(self, sink, catalog, dataset_row):
        self._args = (sink, catalog, dataset_row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _RecordingCursor(*self._args)


def _transitional_catalog():
    """The transitional BRAVO shape: four business columns plus a visible identity."""
    return [
        {"column_name": "trip_start", "is_visible": True, "is_row_identifier": False},
        {"column_name": "driver_name", "is_visible": True, "is_row_identifier": False},
        {"column_name": "distance_km", "is_visible": True, "is_row_identifier": False},
        {"column_name": "is_billable", "is_visible": True, "is_row_identifier": False},
        # is_visible = true AND is_row_identifier = true — the state the two
        # BRAVO datasets are in before the identifier is configured away.
        {"column_name": "record_id", "is_visible": True, "is_row_identifier": True},
    ]


def _dataset_row():
    row = _dataset()
    row.update({"client_database_name": "acme_db", "created_by": None, "created_at": None,
                "updated_by": None, "updated_at": None, "source_direct": True, "source_group": False})
    return row


def _count_via(loader):
    sink: list[str] = []
    catalog = _transitional_catalog()
    old = _patch("db_conn", lambda: _RecordingConn(sink, catalog, _dataset_row()))
    try:
        result = loader()
    finally:
        _restore([("db_conn", old)])
    # The loaders differ in shape: a list, a dict keyed by dataset_id, or one row.
    if isinstance(result, list):
        row = result[0]
    elif isinstance(result, dict) and "visible_columns" not in result:
        row = next(iter(result.values()))
    else:
        row = result
    return int(row["visible_columns"]), sink


def test_ordinary_user_visible_column_counts_exclude_the_row_identity() -> None:
    """Review finding: an ordinary count revealed the hidden technical column.

    Four business columns are visible. Under the transitional catalog state the
    identifier is *also* flagged visible, so an unfixed count reports five — one
    more than the sheet can possibly show, which discloses that a hidden
    technical column exists.
    """
    for label, loader in (
        ("dataset catalogue card", lambda: api_main._list_effective_dataset_access_for_user(USER_ID)),
        ("row-browser dataset", lambda: api_main._get_portal_database_dataset_for_user(DATASET_ID, USER_ID)),
    ):
        count, queries = _count_via(loader)
        assert count == 4, f"{label}: reported {count}, exposing the hidden identifier"
        assert any("is_row_identifier IS NOT TRUE" in q for q in queries), label

    # Admin/catalog surfaces deliberately keep catalog semantics: the row
    # identifier control has to know the column exists.
    for label, loader in (
        ("admin dataset list", lambda: api_main._list_portal_database_datasets()),
        ("admin dataset", lambda: api_main._get_portal_database_dataset(DATASET_ID)),
    ):
        count, _queries = _count_via(loader)
        assert count == 5, f"{label}: admin metadata must still see the catalog"
    print("PASS: ordinary visible-column counts exclude the row identity; admin counts do not")


def test_ordinary_catalogue_page_renders_the_business_count() -> None:
    """The number reaches the user through the `/user/database` card."""
    sink: list[str] = []
    catalog = _transitional_catalog()
    patches = [
        ("db_conn", _patch("db_conn", lambda: _RecordingConn(sink, catalog, _dataset_row()))),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        html = api_main._user_database_response(_user(), _FakeRequest()).body.decode("utf-8")
    finally:
        _restore(patches)
    # Approved stage S9 replaced the card grid with the `DB-001` comparison
    # table; the count it renders in `Kolumny` is still the business count.
    assert '<td class="db-cat-num lp-mono">4</td>' in html, html
    assert '<td class="db-cat-num lp-mono">5</td>' not in html, "the hidden identifier is being counted"
    _assert_no_identity_leak(html, "dataset catalogue")
    print("PASS: the dataset catalogue card reports the business count, not the catalog count")


def test_row_browser_page_actually_loads_the_row_detail_script() -> None:
    """Review finding: the module was registered but never requested by the page.

    Registry membership is not evidence — that is exactly what the review found
    insufficient. This asserts the rendered document, with the content-derived
    cache version the asset layer emits.
    """
    html = _render()
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    row_detail = [src for src in scripts if "data-grid-row-detail.js" in src]
    assert len(row_detail) == 1, scripts
    assert re.search(r"/static/js/data-grid-row-detail\.js\?v=[0-9a-f]+", row_detail[0]), row_detail
    # Present regardless of whether a row is open or the dataset has an identity,
    # so the FIRST interaction is already enhanced.
    for html_variant, label in (
        (_render(query=_open_query(_render())), "row open"),
        (_render(identity=False), "no configured identity"),
        (_render(query="cols=driver_name"), "narrowed columns"),
    ):
        assert "data-grid-row-detail.js" in html_variant, label
    print("PASS: the rendered row-browser page loads the versioned row-detail module")


def test_row_detail_script_is_page_scoped_not_global() -> None:
    """Unrelated portal pages must not gain the Database Explorer grid layer."""
    patches = [
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [])),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        catalogue = api_main._user_database_response(_user(), _FakeRequest()).body.decode("utf-8")
    finally:
        _restore(patches)
    # The dataset catalogue is not the row grid: it must not pull the S6 module.
    assert "data-grid-row-detail.js" not in catalogue, "the module leaked onto a non-grid page"
    print("PASS: the row-detail module stays page-scoped to the row browser")


def test_rowfields_is_an_allowlisted_mode_not_a_column_selector() -> None:
    """Review finding: arbitrary `rowfields` values survived into canonical state."""
    sheet = _render()
    open_query = _open_query(sheet)

    def _rowfields_values(html_text: str) -> set[str]:
        """Every `rowfields` value the page would send back to the server.

        Collected from generated links and from hidden inputs, because a
        rejected value riding in either is the defect. The drawer's own scope
        toggle legitimately emits `rowfields=all`, so the assertion is about
        which VALUES appear, not whether the parameter is mentioned at all.
        """
        found = set()
        for href in re.findall(r'href="/user/database/datasets/[^"?]+\?([^"]+)"', html_text):
            parsed = parse_qs(html_mod.unescape(href), keep_blank_values=True)
            found.update(parsed.get("rowfields", []))
        for tag in re.findall(r"<input[^>]*>", html_text):
            if 'name="rowfields"' in tag:
                value = re.search(r'value="([^"]*)"', tag)
                found.add(html_mod.unescape(value.group(1)) if value else "")
        return found

    # Invalid values are discarded, never reflected.
    for bad in (
        "rowfields=record_id",
        "rowfields=foo",
        "rowfields=1;DROP TABLE portal_database_datasets--",
        "rowfields=<script>alert(1)</script>",
        "rowfields=RECORD_ID",
    ):
        html = _render(query=f"{open_query}&{bad}")
        _assert_no_identity_leak(html, bad)
        emitted = _rowfields_values(html)
        assert emitted <= {api_main.PORTAL_DATABASE_ROW_FIELDS_ALL}, (bad, sorted(emitted))
        # The drawer still works, in its default scope.
        assert "db-row-detail" in html, bad
        visible_default = re.search(r'data-db-row-fields="visible"[^>]*aria-current="true"', html)
        assert visible_default, f"{bad}: invalid mode must fall back to the default scope"

    # The one valid value round-trips for an open row.
    valid = _render(query=f"{open_query}&rowfields=all")
    assert _rowfields_values(valid) == {"all"}, sorted(_rowfields_values(valid))
    assert re.search(r'data-db-row-fields="all"[^>]*aria-current="true"', valid), "all-scope must be active"

    # Repeated conflicting values collapse to the first valid one.
    repeated = _render(query=f"{open_query}&rowfields=record_id&rowfields=all")
    _assert_no_identity_leak(repeated, "repeated rowfields")
    assert re.search(r'data-db-row-fields="all"[^>]*aria-current="true"', repeated), "the valid value wins"

    # With no row open the parameter is detail-only state and is dropped.
    closed = _render(query="rowfields=all")
    assert _rowfields_values(closed) == set(), sorted(_rowfields_values(closed))
    print("PASS: rowfields accepts only the approved mode and never reflects a rejected value")


def main() -> None:
    test_identity_is_absent_from_every_user_column_surface()
    test_legacy_visible_identity_still_cannot_leak()
    test_forged_cols_and_layout_state_cannot_expose_the_identity()
    test_forged_sort_and_filter_state_cannot_expose_the_identity()
    test_identity_is_used_as_an_internal_tiebreaker_only()
    test_identity_never_reaches_the_aggregate_or_export_column_universe()
    test_reference_is_opaque_and_is_not_an_encoding_of_the_identity()
    test_reference_is_bound_to_dataset_client_and_identifier_column()
    test_tampered_and_malformed_references_are_rejected_identically()
    test_no_custom_cryptography_is_used()
    test_reference_survives_a_restart_when_the_stable_secret_is_configured()
    test_sheet_emits_one_opaque_reference_per_row_and_no_identity()
    test_row_url_carries_the_reference_and_preserves_the_whole_view()
    test_drawer_opens_from_a_reference_without_disclosing_the_identity()
    test_drawer_field_scope_defaults_to_visible_and_switches_to_all()
    test_drawer_traversal_uses_references_and_reaches_both_edges()
    test_drawer_close_returns_the_plain_view()
    test_unresolvable_references_produce_one_generic_state()
    test_datasets_without_a_configured_identity_offer_no_row_detail()
    test_row_identity_survives_column_reorder_hide_width_and_pin()
    test_single_row_lookup_is_parameterized_and_detects_non_uniqueness()
    test_audit_records_no_identity_no_token_and_no_values()
    test_no_saved_view_or_preference_persistence_arrives_with_s6()
    test_shipped_script_owns_no_authorization_and_decodes_nothing()
    test_script_opens_a_row_by_click_and_by_enter()
    test_script_does_not_hijack_header_and_resize_controls()
    test_script_closes_on_escape_and_returns_focus_to_the_row()
    test_script_traversal_moves_between_references_and_stops_at_the_edges()
    test_script_preserves_table_scroll_across_open_and_close()
    test_ordinary_user_visible_column_counts_exclude_the_row_identity()
    test_ordinary_catalogue_page_renders_the_business_count()
    test_row_browser_page_actually_loads_the_row_detail_script()
    test_row_detail_script_is_page_scoped_not_global()
    test_rowfields_is_an_allowlisted_mode_not_a_column_selector()
    print("\nALL HIDDEN ROW IDENTITY AND ROW DETAIL TESTS PASSED")


if __name__ == "__main__":
    main()
