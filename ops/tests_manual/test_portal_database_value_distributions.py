#!/usr/bin/env python3
"""Database Explorer value distributions (approved stage S4).

Covers the aggregate disclosure surface added to the S3 column menus: the
distinct-value picker, the 14-bucket numeric histogram, the authorization that
governs both, the faceted scope, the bounded result set, the degenerate numeric
domains, and the on-demand loading that keeps them out of the initial page.

Also covers the two accepted S3 review corrections that ship with this stage:
`Wyczyść wszystkie` preserving every selected column (asserted in the S3 suite,
which owns the reset URL) and the submit guard recovering after a back-forward
cache restore.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— screen ``DB-005`` §4, ``TABLE_AND_DATA_GRID_SPEC.md`` §3.2/§3.3/§5.1,
``IMPLEMENTATION_ACCEPTANCE_CRITERIA.md`` ``DB-10``/``DB-11``.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_value_distributions.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Import stubs
# ---------------------------------------------------------------------------
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

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query=""):
        self.url = _FakeUrl(f"/user/database/datasets/{DATASET_ID}/distribution", query)
        self.cookies = {}


def _user():
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "trips", "default_date_column": "trip_start",
        "is_active": True, "visible_columns": 5, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _col(name, label, data_type, **extra):
    column = {
        "dataset_id": DATASET_ID, "column_name": name, "display_name": label, "data_type": data_type,
        "is_visible": True, "is_filterable": True, "is_sortable": True,
        "is_default_date_column": name == "trip_start", "display_order": 10,
    }
    column.update(extra)
    return column


def _columns():
    return [
        _col("trip_start", "Start", "timestamp with time zone", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("distance_km", "Dystans", "numeric", display_order=30),
        _col("is_billable", "Rozliczalny", "boolean", display_order=40),
        _col("note", "Notatka", "text", is_filterable=False, display_order=50),
        _col("internal_secret", "Ukryte", "text", is_visible=False, is_filterable=False,
             is_sortable=False, display_order=60),
    ]


def _visible_columns():
    return [c for c in _columns() if c["is_visible"]]


def _params(query: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        parsed.setdefault(key, []).append(value)
    return parsed


def _column(name: str):
    return next(c for c in _visible_columns() if c["column_name"] == name)


def _distribution_query(column_name: str, query: str = "", *, dataset=None, columns=None):
    return api_main._build_portal_database_distribution_query(
        dataset if dataset is not None else _dataset(),
        columns if columns is not None else _visible_columns(),
        _params(query),
        _column(column_name) if columns is None else next(c for c in columns if c["column_name"] == column_name),
    )


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


class _Cursor:
    def __init__(self, capture, rows):
        self._capture = capture
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, values=None):
        self._capture.append({"query": query, "values": list(values or [])})

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self, capture, rows):
        self._capture = capture
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, *args, **kwargs):
        return _Cursor(self._capture, self._rows)


def _call_endpoint(*, query, dataset=None, columns=None, rows=None, raises=None, user=None):
    """Invoke the endpoint with the client database replaced by a capture stub."""
    dataset_value = _dataset() if dataset is None else dataset
    columns_value = _visible_columns() if columns is None else columns
    capture: list[dict] = []
    audit: list[dict] = []

    def _connect(database_name, **kwargs):
        if raises is not None:
            raise raises
        return _Connection(capture, rows or [])

    patches = [
        ("_get_portal_database_dataset_for_user",
         _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset_value)),
        ("_get_portal_database_visible_columns",
         _patch("_get_portal_database_visible_columns", lambda d: columns_value)),
        ("_portal_dataset_client_database_name",
         _patch("_portal_dataset_client_database_name", lambda d: "acme_db")),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database", _connect)),
        ("_rows_to_dicts", _patch("_rows_to_dicts", lambda r: list(r))),
        ("_portal_audit_event_safe",
         _patch("_portal_audit_event_safe", lambda **kwargs: audit.append(kwargs))),
    ]
    try:
        response = api_main._portal_database_distribution_response(
            user if user is not None else _user(), DATASET_ID, _FakeRequest(query=query)
        )
    finally:
        _restore(patches)
    payload = json.loads(response.body.decode("utf-8"))
    return {
        "status": response.status_code,
        "media_type": response.media_type,
        "payload": payload,
        "sql": capture,
        "audit": audit,
    }


# ===========================================================================
# 1. Aggregate authorization — every path must fail closed
# ===========================================================================
def test_distribution_requires_an_accessible_dataset() -> None:
    # The gating query already enforces the client grant, the dataset grant and
    # `can_view_rows`; an inaccessible dataset resolves to None.
    result = _call_endpoint(query="column=driver_name", dataset=False)
    assert result["status"] == 404, result
    assert result["payload"] == {"error": api_main.PORTAL_DATABASE_DISTRIBUTION_DENIED_MESSAGE}, result
    assert result["sql"] == [], "no client-database statement may run for an inaccessible dataset"
    assert [event["event_type"] for event in result["audit"]] == ["database_distribution_denied"], result["audit"]
    print("PASS: an inaccessible client/dataset gets no aggregate and no query")


def test_distribution_requires_can_filter_rows() -> None:
    # A value/count list is filter reconnaissance even though it returns no rows,
    # so it needs the filtering capability, not just view access.
    denied = _dataset(can_filter_rows=False, can_view_rows=True)
    for column in ("driver_name", "distance_km", "is_billable", "trip_start"):
        result = _call_endpoint(query=f"column={column}", dataset=denied)
        assert result["status"] == 404, (column, result)
        assert result["sql"] == [], (column, "no aggregate may run without can_filter_rows")
        assert result["payload"]["error"] == api_main.PORTAL_DATABASE_DISTRIBUTION_DENIED_MESSAGE, result
        reasons = [event["metadata"].get("reason") for event in result["audit"]]
        assert reasons == ["filtering_not_permitted"], reasons
    print("PASS: can_filter_rows=false blocks every distribution")


def test_unapproved_and_non_filterable_columns_cannot_be_probed() -> None:
    for column in (
        "internal_secret",   # not visible in the catalog
        "note",              # visible but not filterable
        "nonexistent",       # not a column at all
        "trips; DROP TABLE trips",
        "*",
        "1",
        "",
    ):
        result = _call_endpoint(query="column=" + column.replace(" ", "%20").replace(";", "%3B"))
        assert result["status"] == 404, (column, result)
        assert result["sql"] == [], (column, "an unapproved column must not reach the database")
        assert result["payload"]["error"] == api_main.PORTAL_DATABASE_DISTRIBUTION_DENIED_MESSAGE, result
        # The refusal is identical for every cause, so it cannot be used to
        # learn which columns exist.
        if column:
            assert column not in json.dumps(result["payload"]), (column, "column name echoed back")
    print("PASS: no unapproved or non-filterable column can be probed")


def test_the_refusal_never_distinguishes_its_cause() -> None:
    inaccessible = _call_endpoint(query="column=driver_name", dataset=False)
    unfiltered = _call_endpoint(query="column=driver_name", dataset=_dataset(can_filter_rows=False))
    unapproved = _call_endpoint(query="column=internal_secret")
    bodies = {json.dumps(r["payload"], sort_keys=True) for r in (inaccessible, unfiltered, unapproved)}
    statuses = {r["status"] for r in (inaccessible, unfiltered, unapproved)}
    assert len(bodies) == 1, bodies
    assert statuses == {404}, statuses
    print("PASS: every refusal is byte-identical, so it cannot be used to probe")


def test_the_caller_cannot_choose_the_aggregation() -> None:
    # Mode, bucket count and row limit are resolved from the catalog and from
    # module constants; nothing the caller sends can change the shape of the
    # aggregate or become part of it.
    hostile = (
        "column=driver_name&mode=numeric",
        "column=driver_name&buckets=9999",
        "column=driver_name&limit=100000",
        "column=distance_km&mode=categorical",
        "column=distance_km&buckets=1);DROP TABLE trips--",
        "column=driver_name&family=numeric",
        "column=driver_name&group_by=internal_secret",
    )
    for query in hostile:
        result = _call_endpoint(query=query, rows=[{"value": "a", "value_count": 1, "distinct_count": 1,
                                                    "non_null_count": 1, "scope_rows": 1}])
        assert result["status"] == 200, (query, result)
        sql = result["sql"][0]["query"]
        values = result["sql"][0]["values"]
        assert "internal_secret" not in sql, (query, sql)
        assert "DROP" not in sql.upper(), (query, sql)
        assert "9999" not in sql and "100000" not in sql, (query, sql)
        if "column=driver_name" in query:
            assert 'GROUP BY value' in sql and 'CAST("driver_name" AS TEXT)' in sql, sql
            assert values[-1] == api_main.PORTAL_DATABASE_DISTINCT_VALUE_LIMIT, values
        else:
            assert "width_bucket" in sql, sql
            assert values[-2:] == [api_main.PORTAL_DATABASE_HISTOGRAM_BUCKETS] * 2, values
    print("PASS: mode, bucket count and limit are server-resolved and unreachable from the URL")


# ===========================================================================
# 2. Query shape
# ===========================================================================
def test_categorical_query_shape() -> None:
    sql, values, mode, error, _entries = _distribution_query("driver_name")
    assert error is None and mode == "categorical", (mode, error)
    # Exactly one GROUP BY, over a catalog identifier, quoted.
    assert sql.count("GROUP BY") == 1, sql
    assert 'CAST("driver_name" AS TEXT) AS value' in sql, sql
    assert 'FROM "public"."trips"' in sql, sql
    # Bounded, and the bound is a parameter rather than formatted into the text.
    assert sql.rstrip().endswith("LIMIT %s"), sql
    assert values == [api_main.PORTAL_DATABASE_DISTINCT_VALUE_LIMIT], values
    # One aggregate expression set, none of it caller-supplied.
    assert "count(*)" in sql and "count(value)" in sql, sql
    assert "internal_secret" not in sql, sql
    print("PASS: the categorical query groups one approved identifier under a bound limit")


def test_numeric_query_shape() -> None:
    sql, values, mode, error, _entries = _distribution_query("distance_km")
    assert error is None and mode == "numeric", (mode, error)
    assert 'CAST("distance_km" AS numeric) AS value' in sql, sql
    assert "width_bucket(base.value, b.min_value, b.max_value, %s)" in sql, sql
    # The bucket count is bound twice (the width_bucket call and the LEAST cap)
    # and is the module constant, never text from the URL.
    assert values == [api_main.PORTAL_DATABASE_HISTOGRAM_BUCKETS,
                      api_main.PORTAL_DATABASE_HISTOGRAM_BUCKETS], values
    assert str(api_main.PORTAL_DATABASE_HISTOGRAM_BUCKETS) not in sql, "bucket count must be bound, not inlined"
    print("PASS: the numeric query buckets with width_bucket under a bound bucket count")


def test_families_without_an_approved_distribution_get_only_a_summary() -> None:
    # `TGS` 5.1 item 4 gives boolean no distribution, and names none for date.
    # Both still need the identity's non-null count (item 1).
    for column_name in ("is_billable", "trip_start"):
        sql, values, mode, error, _entries = _distribution_query(column_name)
        assert error is None and mode == "summary", (column_name, mode, error)
        assert "GROUP BY" not in sql and "width_bucket" not in sql, (column_name, sql)
        assert "count(*)" in sql and f'count("{column_name}")' in sql, sql
        assert values == [], values
    print("PASS: boolean and date columns get the identity count and no distribution")


def test_the_scope_reuses_the_validated_filter_conditions() -> None:
    query = (
        "op__distance_km=between&filter_from__distance_km=10&filter_to__distance_km=20"
        "&op__is_billable=is_true&search=abc"
    )
    sql, values, _mode, error, _entries = _distribution_query("driver_name", query)
    assert error is None, error
    # The same fragments the row query emits, not a second filter implementation.
    assert '"distance_km" >= %s' in sql and '"distance_km" <= %s' in sql, sql
    assert '"is_billable" IS TRUE' in sql, sql
    # The global search is a cross-column predicate, not this column's filter,
    # so it still applies even to that column's own distribution. Only one text
    # column is filterable in this dataset, hence one ILIKE.
    assert 'CAST("driver_name" AS TEXT) ILIKE %s' in sql, "the global search still applies"
    assert values[:-1] == ["10", "20", "%abc%"], values
    assert values[-1] == api_main.PORTAL_DATABASE_DISTINCT_VALUE_LIMIT, values
    # No user value is anywhere in the statement text.
    for literal in ("10", "20", "abc"):
        assert f"'{literal}'" not in sql, (literal, sql)
    print("PASS: the aggregate scope reuses the validated row conditions verbatim")


def test_a_rejected_filter_in_the_view_state_refuses_the_aggregate() -> None:
    # If the surrounding view carries a filter the builder refuses, the aggregate
    # must refuse too rather than silently widening its own scope.
    result = _call_endpoint(query="column=driver_name&op__distance_km=gt&filter__distance_km=abc")
    assert result["status"] == 400, result
    assert result["sql"] == [], "a rejected filter must not produce a broader aggregate"
    assert result["payload"]["error"] == api_main.PORTAL_DATABASE_DISTRIBUTION_FAILED_MESSAGE, result
    assert "abc" not in json.dumps(result["payload"]), "the rejected value must not be echoed"
    print("PASS: a rejected surrounding filter refuses the aggregate instead of widening it")


def test_injection_shaped_filter_values_stay_parameters_in_the_aggregate() -> None:
    payload = "' OR '1'='1"
    encoded = payload.replace("'", "%27").replace(" ", "%20").replace("=", "%3D")
    sql, values, _mode, error, _entries = _distribution_query(
        "distance_km", f"op__driver_name=eq&filter__driver_name={encoded}"
    )
    assert error is None, error
    assert payload not in sql, sql
    assert values[0] == payload, values
    assert sql.count("%s") == len(values), (sql, values)
    print("PASS: an injection-shaped filter value is a bound parameter in the aggregate too")


# ===========================================================================
# 3. Faceted scope (self-filter semantics)
# ===========================================================================
def test_the_columns_own_filter_is_excluded_from_its_own_distribution() -> None:
    query = (
        "op__driver_name=contains&filter__driver_name=Kow"
        "&op__distance_km=gt&filter__distance_km=5"
    )
    own, own_values, _mode, error, _entries = _distribution_query("driver_name", query)
    assert error is None, error
    # Its own filter is gone…
    assert "ILIKE" not in own, own
    assert "%Kow%" not in own_values, own_values
    # …every other filter stands.
    assert '"distance_km" > %s' in own, own
    assert own_values[0] == "5", own_values

    # And the other column's distribution still carries the text filter, because
    # only the column being inspected is excluded.
    other, other_values, _mode, error, _entries = _distribution_query("distance_km", query)
    assert error is None, error
    assert 'CAST("driver_name" AS TEXT) ILIKE %s' in other, other
    assert "%Kow%" in other_values, other_values
    assert '"distance_km" >' not in other, "the numeric column's own filter must be excluded"
    print("PASS: a distribution excludes only the filter of the column it describes")


def test_every_parameter_family_of_the_own_column_is_excluded() -> None:
    cases = [
        ("driver_name", "op__driver_name=in&filter_in__driver_name=a%0Ab", "IN ("),
        ("driver_name", "op__driver_name=blank", "IS NULL"),
        ("distance_km", "op__distance_km=between&filter_from__distance_km=1&filter_to__distance_km=2", ">="),
        ("trip_start", "dateop__trip_start=range&date_from__trip_start=2026-01-01", ">="),
        ("trip_start", "dateop__trip_start=blank", "IS NULL"),
    ]
    for column_name, query, fragment in cases:
        sql, _values, _mode, error, _entries = _distribution_query(column_name, query)
        assert error is None, (query, error)
        assert f'"{column_name}" {fragment}' not in sql, (query, sql)
        assert "WHERE" not in sql.split("FROM")[1].split(")")[0] or fragment not in sql, (query, sql)
    print("PASS: value, range, multi-value and date parameter families are all excluded")


def test_the_response_states_its_scope() -> None:
    result = _call_endpoint(
        query="column=driver_name&op__driver_name=contains&filter__driver_name=Kow",
        rows=[{"value": "Kowalski", "value_count": 3, "distinct_count": 1,
               "non_null_count": 3, "scope_rows": 3}],
    )
    assert result["payload"]["scope"] == "filtered_excluding_column", result["payload"]
    # And the UI carries the sentence that says so, so the numbers are never
    # presented as if they described the visible rows.
    catalogue = json.loads(api_main._portal_database_distribution_strings())
    assert "nie filtr tej kolumny" in catalogue["scope"], catalogue["scope"]
    print("PASS: the faceted scope is stated in the payload and in words")


# ===========================================================================
# 4. Categorical results
# ===========================================================================
def _categorical_rows(pairs, *, distinct, non_null, scope_rows):
    return [
        {"value": value, "value_count": count, "distinct_count": distinct,
         "non_null_count": non_null, "scope_rows": scope_rows}
        for value, count in pairs
    ]


def test_categorical_payload_counts_and_markers() -> None:
    rows = _categorical_rows(
        [("Kowalski", 412), (None, 90), ("", 31), ("   ", 7)],
        distinct=4, non_null=450, scope_rows=540,
    )
    result = _call_endpoint(query="column=driver_name", rows=rows)
    payload = result["payload"]
    assert result["status"] == 200 and result["media_type"] == "application/json", result
    assert payload["mode"] == "categorical", payload
    # Exact counts, straight from the query — nothing derived or estimated.
    assert payload["values"] == [
        {"value": "Kowalski", "count": 412},
        {"value": None, "count": 90},
        {"value": "", "count": 31},
        {"value": "   ", "count": 7},
    ], payload["values"]
    # NULL, empty text and whitespace stay three separate rows: S2 made that a
    # hard requirement and a picker that merged them would undo it.
    assert payload["values"][1]["value"] is None, payload
    assert payload["values"][2]["value"] == "", payload
    assert payload["values"][3]["value"] == "   ", payload
    assert payload["distinct_count"] == 4 and payload["non_null_count"] == 450, payload
    assert payload["scope_rows"] == 540, payload
    assert payload["truncated"] is False, payload
    print("PASS: categorical counts are exact and keep NULL, empty and whitespace apart")


def test_categorical_result_is_bounded_and_says_so() -> None:
    assert api_main.PORTAL_DATABASE_DISTINCT_VALUE_LIMIT == 50, "TGS §3.2 caps the picker at 50"
    rows = _categorical_rows(
        [(f"value-{i}", 100 - i) for i in range(50)],
        distinct=8123, non_null=90000, scope_rows=90000,
    )
    result = _call_endpoint(query="column=driver_name", rows=rows)
    payload = result["payload"]
    assert len(payload["values"]) == 50, len(payload["values"])
    assert payload["limit"] == 50, payload
    # A high-cardinality column must not read as if 50 were all of them.
    assert payload["truncated"] is True, payload
    assert payload["distinct_count"] == 8123, payload

    # And the statement itself is what bounds it.
    sql, values, _mode, _error, _entries = _distribution_query("driver_name")
    assert values[-1] == 50, values
    assert "LIMIT %s" in sql, sql
    print("PASS: the value list is bounded at 50 and a partial list is declared partial")


def test_categorical_empty_scope() -> None:
    result = _call_endpoint(query="column=driver_name", rows=[])
    payload = result["payload"]
    assert payload["values"] == [], payload
    assert payload["distinct_count"] == 0 and payload["scope_rows"] == 0, payload
    assert payload["truncated"] is False, payload
    print("PASS: an empty scope returns an empty, honest categorical payload")


# ===========================================================================
# 5. Numeric histogram
# ===========================================================================
def _numeric_rows(scope_rows, value_count, minimum, maximum, buckets):
    if not buckets:
        return [{"scope_rows": scope_rows, "value_count": value_count,
                 "min_value": minimum, "max_value": maximum,
                 "bucket": None, "bucket_count": None}]
    return [
        {"scope_rows": scope_rows, "value_count": value_count,
         "min_value": minimum, "max_value": maximum,
         "bucket": index, "bucket_count": count}
        for index, count in buckets
    ]


def test_numeric_payload_has_the_approved_bucket_contract() -> None:
    assert api_main.PORTAL_DATABASE_HISTOGRAM_BUCKETS == 14, "TGS §3.3 fixes 14 buckets"
    rows = _numeric_rows(120, 100, Decimal("0"), Decimal("140"),
                         [(1, 10), (2, 30), (7, 45), (14, 15)])
    result = _call_endpoint(query="column=distance_km", rows=rows)
    payload = result["payload"]
    assert payload["mode"] == "numeric" and payload["domain"] == "range", payload
    assert payload["bucket_count"] == 14 and len(payload["buckets"]) == 14, payload
    # Every bucket is present, including the empty ones, so the shape is stable.
    assert [b["index"] for b in payload["buckets"]] == list(range(1, 15)), payload["buckets"]
    assert sum(b["count"] for b in payload["buckets"]) == 100 == payload["non_null_count"], payload
    # min and max are labelled, and NULLs are reported rather than hidden.
    assert payload["min"] == "0" and payload["max"] == "140", payload
    assert payload["null_count"] == 20 and payload["scope_rows"] == 120, payload
    # Edges are contiguous and end exactly on max.
    assert payload["buckets"][0]["min"] == "0", payload["buckets"][0]
    assert payload["buckets"][-1]["max"] == "140", payload["buckets"][-1]
    for previous, following in zip(payload["buckets"], payload["buckets"][1:]):
        assert previous["max"] == following["min"], (previous, following)
    print("PASS: DB-10 — 14 buckets with labelled min/max and contiguous edges")


def test_numeric_bounds_are_decimal_exact() -> None:
    # A float round trip would move an edge; the source precision is not the
    # histogram's to redefine.
    rows = _numeric_rows(3, 3, Decimal("0.1"), Decimal("0.3"), [(1, 1), (14, 2)])
    payload = _call_endpoint(query="column=distance_km", rows=rows)["payload"]
    assert payload["min"] == "0.1" and payload["max"] == "0.3", payload
    edges = [b["min"] for b in payload["buckets"]] + [payload["buckets"][-1]["max"]]
    for edge in edges:
        assert "e" not in edge.lower(), ("no exponent notation should reach the UI", edge)
    # The exact arithmetic is reproducible with Decimal, not float.
    expected = api_main._portal_database_histogram_bounds(Decimal("0.1"), Decimal("0.3"), 14)
    assert [(b["min"], b["max"]) for b in payload["buckets"]] == expected, payload["buckets"]
    assert Decimal(expected[0][0]) == Decimal("0.1") and Decimal(expected[-1][1]) == Decimal("0.3")
    print("PASS: bucket edges are exact decimals and never round-trip through float")


def test_numeric_negative_and_zero_domains() -> None:
    rows = _numeric_rows(50, 50, Decimal("-12.5"), Decimal("7.75"), [(1, 1), (9, 47), (14, 2)])
    payload = _call_endpoint(query="column=distance_km", rows=rows)["payload"]
    assert payload["min"] == "-12.5" and payload["max"] == "7.75", payload
    assert payload["domain"] == "range", payload
    assert sum(b["count"] for b in payload["buckets"]) == 50, payload

    # Zero is a value, not an absence: it must be inside the domain.
    zero_rows = _numeric_rows(10, 10, Decimal("0"), Decimal("5"), [(1, 6), (14, 4)])
    zero = _call_endpoint(query="column=distance_km", rows=zero_rows)["payload"]
    assert zero["min"] == "0" and zero["domain"] == "range", zero
    assert zero["null_count"] == 0, zero
    print("PASS: negative, decimal and zero domains bucket correctly")


def test_numeric_degenerate_domains_are_defined_states() -> None:
    cases = {
        "empty": _numeric_rows(0, 0, None, None, []),
        "all_null": _numeric_rows(7, 0, None, None, []),
        "single_value": _numeric_rows(3, 3, Decimal("7"), Decimal("7"), []),
    }
    for expected, rows in cases.items():
        payload = _call_endpoint(query="column=distance_km", rows=rows)["payload"]
        assert payload["domain"] == expected, (expected, payload)
        # No fabricated buckets in any degenerate case.
        assert payload["buckets"] == [], (expected, payload)
    single = _call_endpoint(query="column=distance_km", rows=cases["single_value"])["payload"]
    assert single["min"] == "7" and single["max"] == "7", single
    assert single["non_null_count"] == 3, single
    all_null = _call_endpoint(query="column=distance_km", rows=cases["all_null"])["payload"]
    assert all_null["non_null_count"] == 0 and all_null["null_count"] == 7, all_null
    print("PASS: empty, all-NULL and single-value domains are three defined states, never invented bars")


# ===========================================================================
# 6. Response hygiene, errors and audit
# ===========================================================================
def test_the_response_exposes_nothing_internal() -> None:
    rows = _categorical_rows([("Kowalski", 3)], distinct=1, non_null=3, scope_rows=3)
    body = json.dumps(_call_endpoint(query="column=driver_name", rows=rows)["payload"])
    for leaked in ("public", "trips", "acme_db", "SELECT", "GROUP BY", "CAST", "schema_name", "table_name"):
        assert leaked not in body, (leaked, body)
    print("PASS: no physical table, database, SQL or catalog internals reach the browser")


def test_a_failed_aggregate_degrades_without_exposing_the_error() -> None:
    result = _call_endpoint(query="column=driver_name", raises=RuntimeError("relation does not exist: secret_table"))
    assert result["status"] == 502, result
    assert result["payload"] == {"error": api_main.PORTAL_DATABASE_DISTRIBUTION_FAILED_MESSAGE}, result
    assert "secret_table" not in json.dumps(result["payload"]), result
    assert "RuntimeError" not in json.dumps(result["payload"]), result
    reasons = [event["metadata"].get("reason") for event in result["audit"]]
    assert reasons == ["RuntimeError"], reasons
    assert [event["event_type"] for event in result["audit"]] == ["database_distribution_failed"], result["audit"]
    print("PASS: a failed aggregate returns a safe message and records a diagnosable audit reason")


def test_audit_records_the_request_without_recording_values() -> None:
    rows = _categorical_rows([("Kowalski", 3)], distinct=1, non_null=3, scope_rows=3)
    result = _call_endpoint(
        query=("column=driver_name&op__distance_km=gt&filter__distance_km=42"
               "&search=Kowalski&op__is_billable=is_true"),
        rows=rows,
    )
    events = result["audit"]
    assert [event["event_type"] for event in events] == ["database_distribution_viewed"], events
    event = events[0]
    assert event["actor_user_id"] == USER_ID, event
    assert event["client_code"] == "ACME_01" and event["dataset_id"] == DATASET_ID, event
    metadata = event["metadata"]
    assert metadata["column"] == "driver_name" and metadata["mode"] == "categorical", metadata
    # Which columns were filtered, never with what.
    assert metadata["filter_keys"] == ["distance_km", "is_billable"], metadata
    assert metadata["search_present"] is True, metadata
    blob = json.dumps(metadata)
    assert "42" not in blob and "Kowalski" not in blob, blob
    assert "gt" not in blob and "is_true" not in blob, blob
    print("PASS: the audit names the user, client, dataset, column and mode — and no values")


def test_a_forged_column_name_is_not_written_to_the_audit_verbatim() -> None:
    result = _call_endpoint(query="column=" + "trips%3B%20DROP".replace("%3B", "%3B"))
    metadata = result["audit"][0]["metadata"]
    # Only an identifier-shaped name is recorded; anything else is dropped rather
    # than becoming attacker-controlled text in the audit trail.
    assert metadata["column"] is None, metadata
    print("PASS: a non-identifier column name is not written into the audit record")


# ===========================================================================
# 7. On-demand rendering — nothing aggregate in the initial page
# ===========================================================================
def _render_sheet(query: str = "", *, dataset=None, columns=None):
    dataset_value = _dataset() if dataset is None else dataset
    columns_value = _visible_columns() if columns is None else columns
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    _c, _v, applied, _e, entries = api_main._build_portal_database_filter_conditions(
        dataset_value, columns_value, parsed
    )
    state = {"sort": "trip_start", "direction": "desc",
             "active_filters": applied, "filter_entries": entries}
    row = {c["column_name"]: "v" for c in columns_value}
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset_value)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns_value)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (3, state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows",
                                              lambda d, c, p, limit, offset, display_columns=None: ([row], state, None))),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (48213, None))),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database",
                                                   lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                                                       "rendering the sheet must not run an aggregate")))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        return api_main._portal_database_row_browser_response(
            _user(), DATASET_ID, _FakeRequest(query=query)
        ).body.decode("utf-8")
    finally:
        _restore(patches)


def test_the_initial_page_runs_no_aggregate_and_embeds_no_values() -> None:
    # The stubbed connector raises if anything tries to query, so a page that
    # renders at all proves no aggregate ran.
    html = _render_sheet()

    # Containers only — no values, no counts, no buckets in the initial HTML.
    assert html.count("data-db-distribution-url") == 4, "one per filterable column"
    assert "db-distribution-value" not in html, html[:0]
    assert "db-histogram-bar" not in html, "no bucket may be pre-rendered"
    # The vocabulary ships as templates on the container; no resolved COUNT may
    # appear anywhere outside those attributes.
    without_strings = re.sub(r'data-db-strings="[^"]*"', "", html)
    assert "unikalnych" not in without_strings, "no distinct count may be pre-rendered"
    assert "niepustych" not in without_strings, "no non-null count may be pre-rendered"
    assert "{count}" in html, "the count template ships unresolved"
    body = re.search(r'<div class="db-distribution-body"[^>]*></div>', html)
    assert body, "the distribution body ships empty"

    # The eligible families get a visible section; boolean and date get only the
    # hidden summary container, because the contract gives them no distribution.
    assert html.count("Wartości w kolumnie") == 1, "one text column is filterable"
    assert html.count("Rozkład wartości") == 1, "one numeric column is filterable"
    assert 'data-db-distribution-mode="summary"' in html, html[:0]
    print("PASS: the initial page carries containers only and runs no aggregate query")


def test_distribution_urls_carry_the_scope_and_nothing_else() -> None:
    html = _render_sheet(
        "op__driver_name=contains&filter__driver_name=Kow&search=abc"
        "&limit=50&density=comfortable&page=3&sort=driver_name&direction=asc"
    )
    urls = re.findall(r'data-db-distribution-url="([^"]+)"', html)
    assert urls, html[:0]
    for raw in urls:
        url = raw.replace("&amp;", "&")
        query = parse_qs(urlparse(url).query)
        assert urlparse(url).path == f"/user/database/datasets/{DATASET_ID}/distribution", url
        assert len(query["column"]) == 1, url
        # Scope only: the filters and the search.
        assert query["filter__driver_name"] == ["Kow"], url
        assert query["search"] == ["abc"], url
        # Paging, density, sort and the column selection cannot change which
        # rows an aggregate covers, so they are absent.
        for absent in ("limit", "density", "page", "sort", "direction", "cols"):
            assert absent not in query, (absent, url)
    print("PASS: a distribution URL carries the scope and the column, nothing else")


def test_no_distribution_container_when_filtering_is_not_permitted() -> None:
    html = _render_sheet(dataset=_dataset(can_filter_rows=False))
    assert "data-db-distribution" not in html, "no aggregate affordance without can_filter_rows"
    assert "Wartości w kolumnie" not in html and "Rozkład wartości" not in html, html[:0]
    print("PASS: filtering-disabled datasets expose no distribution affordance at all")


def test_the_page_still_ships_no_inline_script() -> None:
    html = _render_sheet()
    assert "<script>" not in html, "the row sheet must keep shipping no inline script"
    assert "/static/js/data-grid-distribution.js?v=" in html, html[:0]
    # The vocabulary rides on the element instead.
    assert "data-db-strings=" in html, html[:0]
    catalogue = json.loads(api_main._portal_database_distribution_strings())
    for key in ("loading", "error", "empty", "allNull", "singleValue", "truncated",
                "scope", "distinct", "nonNull", "range", "bucket",
                "selectValue", "selectBlank", "nullMarker", "blankMarker"):
        assert catalogue.get(key), key
    assert catalogue["nullMarker"] == "brak wartości" and catalogue["blankMarker"] == "pusty tekst"
    print("PASS: strings ship as data, not as an inline script")


def test_progressive_enhancement_survives() -> None:
    html = _render_sheet()
    # The S3 manual controls are untouched by S4: operator select, value field
    # and a real submit inside a real GET form.
    assert 'name="op__driver_name"' in html and 'name="filter__driver_name"' in html, html[:0]
    assert '<form class="db-col-section db-col-filter" method="get" data-db-col-form>' in html, html[:0]
    assert 'class="db-col-apply" type="submit"' in html, html[:0]
    # The distribution script owns no query semantics or permissions.
    source = (REPO_ROOT / "api" / "static" / "js" / "data-grid-distribution.js").read_text(encoding="utf-8")
    for forbidden in ("SELECT ", "GROUP BY", "width_bucket", "can_filter_rows", "password", "dsn"):
        assert forbidden not in source, forbidden
    print("PASS: filtering still works without JavaScript and the script holds no query semantics")


# ===========================================================================
# 8. Frontend behaviour in the shipped script
# ===========================================================================
def _harness(scenario: str) -> dict:
    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / "data_grid_distribution_harness.js"), scenario]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_a_distribution_is_fetched_only_when_its_menu_opens() -> None:
    result = _harness("on-demand")
    assert result["afterLoad"]["requests"] == 0, "no request may fire on page load"
    assert result["afterOpenFirst"]["requests"] == 1, "opening one menu fetches one column"
    assert "column=driver_name" in result["afterOpenFirst"]["url"], result
    assert result["loadingText"] == "Wczytywanie…", result
    assert result["loadingBusy"] == "true", "the loading region is announced"
    assert result["afterRespond"]["busy"] == "false", result
    assert result["afterReopen"]["requests"] == 1, "reopening the same menu must not requery"
    print("PASS: one bounded request per opened menu, none on load, none on reopen")


def test_categorical_rendering_keeps_the_value_semantics() -> None:
    result = _harness("categorical-render")
    # Thousands are grouped with the approved non-breaking space, as in the table.
    assert result["nonNull"] == "1\u00a0183 niepustych", result
    assert result["hasDistinct"] and result["truncated"], result
    assert result["scopeStated"], "the faceted scope must be stated in the menu"
    assert result["nullMarker"] and result["blankMarker"], result
    assert result["whitespaceKept"], "a whitespace-only value stays its own value"
    # NULL and empty string are not `in (…)` candidates; whitespace is.
    assert result["pickable"] == ["Kowalski", "   "], result["pickable"]
    print("PASS: DB-11 — the picker shows counts and keeps NULL, empty and whitespace apart")


def test_selecting_values_stages_the_existing_s3_filter_contract() -> None:
    result = _harness("categorical-select")
    # Staged as one exact hidden input per value — the only form that can carry
    # the whitespace-only value the picker also offers.
    assert result["afterOnePick"] == {"operator": "in", "staged": ["Kowalski"]}, result
    assert result["afterTwoPicks"]["operator"] == "in", result
    assert result["afterTwoPicks"]["staged"] == ["Kowalski", "   "], result
    assert result["textareaCleared"], "the two value forms must not disagree"
    # Staging only — the menu's own Zastosuj is still what applies it.
    assert result["requests"] == 1, "selecting a value must not query or submit"
    # NULL/empty offers the approved `puste`, which is the only operator that
    # can express them.
    assert result["afterBlank"]["operator"] == "blank", result
    print("PASS: picked values stage into `in (…)`, and blanks stage `puste` — no new query contract")


def test_the_histogram_marker_is_presentation_only() -> None:
    result = _harness("numeric-marker")
    assert result["bars"] == 3, result
    assert result["axisStated"], "min and max are labelled"
    assert result["markedAtRest"] == 0, result
    # DB-10: the threshold is marked BEFORE the filter is applied…
    assert result["markedAfterTyping"] == ["2"], result
    # …and moving it issues no request and changes no applied state.
    assert result["requestsAfterTyping"] == 1, "moving the marker must not requery"
    assert result["stagedValueUnchanged"] == "15", result
    print("PASS: DB-10 — the threshold marker moves before Apply and costs no query")


def test_degenerate_numeric_domains_render_as_words_not_bars() -> None:
    cases = _harness("numeric-degenerate")["cases"]
    assert cases["empty"]["bars"] == 0 and "Brak wierszy" in cases["empty"]["text"], cases["empty"]
    assert cases["all_null"]["bars"] == 0 and "są puste" in cases["all_null"]["text"], cases["all_null"]
    assert cases["single_value"]["bars"] == 0, cases["single_value"]
    assert "takie same: 7" in cases["single_value"]["text"], cases["single_value"]
    print("PASS: a degenerate domain is explained in words rather than drawn as invented bars")


def test_a_stale_response_cannot_replace_newer_menu_state() -> None:
    across = _harness("stale-response")
    assert across["requests"] == 2, across
    assert "NOWY" in across["secondMenu"] and "STARY" in across["firstMenu"], across

    # For one container the in-flight guard now makes a concurrent duplicate
    # impossible, so the same-container race cannot be created at all; the
    # pending response still lands after a close/reopen.
    same = _harness("stale-same-container")
    assert same["requests"] == 1, same
    assert "NOWY" in same["rendered"], same
    assert same["nonNull"] == "5 niepustych", same
    print("PASS: a late response for a superseded request is discarded")


def test_a_distribution_failure_leaves_the_menu_usable() -> None:
    result = _harness("error-state")
    assert "Nie udało się wczytać" in result["text"], result
    assert result["busy"] == "false", result
    # Manual operator/value filtering in the same menu is unaffected.
    assert result["manualFilterUsable"] == {"operator": "eq", "value": "Kowalski"}, result
    # A failure is not cached, so reopening retries.
    assert result["requestsAfterReopen"] == 2, result
    print("PASS: a failed distribution leaves manual filtering intact and retries on reopen")


# ===========================================================================
# 9. S3 review correction — BFCache submit guard
# ===========================================================================
def _filters_harness(scenario: str) -> dict:
    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / "data_grid_filters_harness.js"), scenario]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_submit_guard_recovers_after_a_back_forward_cache_restore() -> None:
    result = _filters_harness("submit-guard-bfcache")
    # The guard still does its job during the submission.
    assert result["afterSubmit"] == {"disabled": True, "ariaDisabled": "true", "widthPinned": True}, result
    # An ordinary load must not release it.
    assert result["afterFreshLoad"]["disabled"] is True, result
    # A restored page is usable again: enabled, aria cleared, width released.
    assert result["afterRestore"] == {"disabled": False, "ariaDisabled": None, "widthPinned": False}, result
    assert result["resubmitted"] == 1, "Apply works again after Back"
    print("PASS: browser Back restores a usable Apply without weakening the guard")


# ===========================================================================
# 10. Stage boundary
# ===========================================================================
def test_distributions_add_no_persistence_and_degrade_without_s13() -> None:
    """S4's stage boundary, restated for the state of the repository.

    It originally asserted that saved views and named column sets did not exist
    and that `db/` carried no diff at all. Approved stage S13 added both
    deliberately, so asserting their absence would now assert that an approved
    stage was not built.

    Two things S4 does still own are checked instead, and the second is worth
    more than the original: the value-distribution path itself writes nothing,
    and this harness runs with the S13 schema UNAVAILABLE, so the sheet it
    renders proves the migration-absent behaviour — the saved-object controls
    are ABSENT rather than rendered inert or crashing on a missing relation.
    """
    html = _render_sheet()
    for absent in ("Zapisz jako zestaw", "Zestawy", "Zapisz jako widok", "db-views", "db-colsets"):
        assert absent not in html, f"{absent} must be absent when S13 persistence is unavailable"
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    start = source.index("def _portal_database_distribution_response")
    end = source.index("def _portal_database_distribution_metadata")
    distribution_source = source[start:end]
    for forbidden in ("INSERT", "UPDATE ", "DELETE"):
        assert forbidden not in distribution_source, f"the distribution path must not write: {forbidden}"
    print("PASS: distributions stay read-only and degrade cleanly without S13 persistence")



# ===========================================================================
# 11. Independent-review corrections
# ===========================================================================
def test_picker_values_round_trip_losslessly() -> None:
    """The picker must be able to filter back to any value it displayed.

    Before the correction the selection was serialized through newline-joined
    text and parsed with `splitlines()` + `strip()`, so whitespace-only text,
    leading or trailing spaces and embedded newlines could be displayed and
    counted but never filtered back to themselves.
    """
    sources = ["   ", " lead", "trail ", "a\nb", "\ttab", "' OR '1'='1", "normal", ""]
    query = "op__driver_name=in&" + "&".join(
        "filter_exact__driver_name=" + quote(value, safe="") for value in sources
    )
    conditions, values, _active, error, entries = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(), _params(query)
    )
    assert error is None, error
    # Byte-for-byte, in order, one placeholder each.
    assert values == sources, values
    assert conditions == ['CAST("driver_name" AS TEXT) IN (' + ", ".join(["%s"] * len(sources)) + ")"], conditions
    assert entries[0]["values"] == sources, entries
    # Still data, never syntax.
    for payload in sources:
        if payload.strip():
            assert payload not in conditions[0], payload
    print("PASS: whitespace, newline, tab and injection-shaped values round-trip exactly")


def test_the_human_newline_form_is_unchanged() -> None:
    # Existing hand-written and no-script URLs keep trimming and dropping blank
    # lines: a person typing into a textarea cannot mean a stray leading space.
    _c, values, _a, error, _e = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(),
        _params("op__driver_name=in&filter_in__driver_name=" + quote("Ala\n\n  Ola  \nAla")),
    )
    assert error is None and values == ["Ala", "Ola"], values
    print("PASS: the human newline form keeps its trimming semantics")


def test_exact_values_take_precedence_and_stay_bounded() -> None:
    over = "op__driver_name=in&" + "&".join(
        "filter_exact__driver_name=" + quote(f"v{i}") for i in range(51)
    )
    _c, _v, _a, error, _e = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(), _params(over)
    )
    assert error == "Kierowca accepts at most 50 values; 51 were provided.", error

    exactly = "op__driver_name=in&" + "&".join(
        "filter_exact__driver_name=" + quote(f"v{i}") for i in range(50)
    )
    _c, values, _a, error, _e = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(), _params(exactly)
    )
    assert error is None and len(values) == 50, len(values)

    # When both forms are present the exact one wins, because it is the only one
    # that can carry the picker's values.
    _c, values, _a, error, _e = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(),
        _params("op__driver_name=in&filter_in__driver_name=typed&filter_exact__driver_name=" + quote(" exact ")),
    )
    assert error is None and values == [" exact "], values
    print("PASS: exact values win over the typed form, dedupe, and stay capped at 50")


def test_full_filter_state_is_validated_before_faceted_exclusion() -> None:
    """A malformed filter on the inspected column must refuse, not vanish.

    Before the correction the inspected column's parameters were deleted from
    the raw request and only the remainder was validated, so a rejected filter
    silently disappeared and the aggregate answered over a broader population
    than the rows it was supposed to describe.
    """
    # Malformed filter ON the inspected column.
    result = _call_endpoint(query="column=distance_km&op__distance_km=gt&filter__distance_km=abc")
    assert result["status"] == 400, result
    assert result["sql"] == [], "no aggregate may run once canonical validation rejected the request"

    # And directly at the builder, for both inspection targets.
    for inspected in ("distance_km", "driver_name"):
        _sql, _values, _mode, error, _entries = _distribution_query(
            inspected, "op__distance_km=gt&filter__distance_km=abc"
        )
        assert error == "Dystans must be a number.", (inspected, error)

    # Other malformed own-column shapes are refused too, rather than dropped.
    for query, expected in (
        ("op__distance_km=between&filter_from__distance_km=30&filter_to__distance_km=20",
         "Dystans from must be less than or equal to Dystans to."),
        ("op__distance_km=nonsense&filter__distance_km=1",
         "Operator nonsense is not allowed for distance_km."),
        ("dateop__trip_start=older&date__trip_start=not-a-date",
         "Start must be an ISO date or datetime."),
    ):
        column = "distance_km" if "distance_km" in query else "trip_start"
        _sql, _values, _mode, error, _entries = _distribution_query(column, query)
        assert error == expected, (query, error)
    print("PASS: the whole filter state is validated before the inspected column is excluded")


def test_faceted_exclusion_covers_every_validated_filter_form() -> None:
    cases = [
        ("driver_name", "op__driver_name=eq&filter__driver_name=K"),
        ("driver_name", "op__driver_name=blank"),
        ("driver_name", "op__driver_name=in&filter_exact__driver_name=" + quote("  x  ")),
        ("distance_km", "op__distance_km=between&filter_from__distance_km=1&filter_to__distance_km=2"),
        ("is_billable", "op__is_billable=is_true"),
        ("trip_start", "dateop__trip_start=range&date_from__trip_start=2026-01-01&date_to__trip_start=2026-02-01"),
        ("trip_start", "dateop__trip_start=blank"),
        ("trip_start", "date_from=2026-01-01&date_to=2026-02-01"),  # legacy default-date
    ]
    for column_name, query in cases:
        # Inspecting the column itself: its filter is gone from the scope.
        sql, values, _mode, error, entries = _distribution_query(column_name, query)
        assert error is None, (query, error)
        where = sql.split(" WHERE ", 1)[1].split(")", 1)[0] if " WHERE " in sql else ""
        assert f'"{column_name}"' not in where, (query, where)
        assert column_name not in {e["column_name"] for e in entries}, (query, entries)

        # Inspecting a different column: the same filter still applies.
        other = "driver_name" if column_name != "driver_name" else "distance_km"
        sql, _values, _mode, error, entries = _distribution_query(other, query)
        assert error is None, (query, error)
        assert column_name in {e["column_name"] for e in entries}, (query, entries)
    print("PASS: every validated own-column filter form is excluded; other columns keep theirs")


def test_legacy_default_date_faceting_in_both_directions() -> None:
    query = "date_from=2026-01-01&date_to=2026-02-01"
    dataset = _dataset(default_date_column="trip_start")

    # Inspecting the default date column: its own legacy range is excluded.
    sql, values, _mode, error, entries = api_main._build_portal_database_distribution_query(
        dataset, _visible_columns(), _params(query), _column("trip_start")
    )
    assert error is None, error
    assert " WHERE " not in sql, sql
    assert values == [], values
    assert entries == [], entries

    # Inspecting any other column: the legacy range still constrains the scope.
    sql, values, _mode, error, entries = api_main._build_portal_database_distribution_query(
        dataset, _visible_columns(), _params(query), _column("driver_name")
    )
    assert error is None, error
    assert '"trip_start" >= %s' in sql and '"trip_start" <= %s' in sql, sql
    assert values[:2] == ["2026-01-01", "2026-02-01"], values

    # And the row browser is untouched — legacy date compatibility stays.
    row_query, row_values, _state, row_error = api_main._build_portal_database_rows_query(
        dataset, _visible_columns(), _params(query), limit=10, offset=0
    )
    assert row_error is None and '"trip_start" >= %s' in row_query, row_query
    assert row_values[:2] == ["2026-01-01", "2026-02-01"], row_values
    print("PASS: the legacy default-date range is faceted out only for its own column")


def test_trusted_audit_metadata_holds_only_resolved_columns() -> None:
    """Unauthorized identifier-shaped input must not enter trusted metadata."""
    for column in ("internal_secret", "note", "attacker_field"):
        result = _call_endpoint(query=f"column={column}&filter__attacker_field=x&op__attacker_field=eq")
        assert result["status"] == 404, (column, result)
        metadata = result["audit"][0]["metadata"]
        # `column` is the approved-column field: nothing resolved, so it is null.
        assert metadata["column"] is None, (column, metadata)
        # Validated filter targets are absent entirely for a denied request, so a
        # forged filter key cannot appear as one.
        assert "filter_keys" not in metadata, (column, metadata)
        assert "attacker_field" not in json.dumps({k: v for k, v in metadata.items()
                                                   if k != "requested_column"}), metadata
        # The caller's own input is recorded under an explicitly untrusted name,
        # mirroring the existing `requested_dataset_id` convention.
        assert metadata.get("requested_column") == column, metadata

    # A forged filter key on an approved column makes canonical validation reject
    # the whole request — the stronger outcome, and the forged name never becomes
    # a validated target in the audit either.
    rows = _categorical_rows([("Kowalski", 3)], distinct=1, non_null=3, scope_rows=3)
    forged = _call_endpoint(
        query=("column=driver_name&op__distance_km=gt&filter__distance_km=5"
               "&filter__attacker_field=x&op__attacker_field=eq"),
        rows=rows,
    )
    assert forged["status"] == 400, forged
    assert forged["sql"] == [], "a forged filter key must not reach the database"
    forged_metadata = forged["audit"][0]["metadata"]
    # The inspected column DID resolve, so naming it is correct and trusted.
    assert forged_metadata["column"] == "driver_name", forged_metadata
    # But no validated filter targets exist, so the field is absent rather than
    # carrying the attacker's name.
    assert "filter_keys" not in forged_metadata, forged_metadata
    assert "attacker_field" not in json.dumps(forged_metadata), forged_metadata

    clean = _call_endpoint(query="column=driver_name&op__distance_km=gt&filter__distance_km=5", rows=rows)
    metadata = clean["audit"][0]["metadata"]
    assert metadata["column"] == "driver_name", metadata
    assert metadata["filter_keys"] == ["distance_km"], metadata
    assert "requested_column" not in metadata, metadata
    # The inspected column is faceted out of the scope, so it is named once.
    assert "driver_name" not in metadata["filter_keys"], metadata
    print("PASS: trusted audit fields hold only resolved, validated column identities")


def test_unauthenticated_and_authenticated_refusals_are_documented_correctly() -> None:
    # The platform's normal unauthenticated answer is preserved; it discloses
    # nothing because no dataset or column has been resolved at that point.
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert "status_code=401" in source, "unauthenticated requests keep the platform 401"
    # Every AUTHENTICATED refusal remains indistinguishable.
    authenticated = [
        _call_endpoint(query="column=driver_name", dataset=False),
        _call_endpoint(query="column=driver_name", dataset=_dataset(can_filter_rows=False)),
        _call_endpoint(query="column=internal_secret"),
    ]
    assert {r["status"] for r in authenticated} == {404}, authenticated
    assert len({json.dumps(r["payload"], sort_keys=True) for r in authenticated}) == 1, authenticated
    # And the comment no longer claims the unauthenticated case is identical.
    assert "An unauthenticated request is a different question" in source, "refusal wording must be accurate"
    print("PASS: 401 for unauthenticated, one indistinguishable 404 for every authenticated refusal")


def test_picker_selection_preserves_off_list_values() -> None:
    result = _harness("selection-preserves-off-list")
    # Visible rows start reflecting the existing selection.
    assert result["initialChecked"] == [["Kowalski", True], ["Poznań", False]], result
    assert result["offListNoticed"], "the off-list count is stated"
    # Adding a value EXTENDS; it does not rebuild from the visible checkboxes.
    assert result["afterAdd"] == ["Kowalski", "Kraków", "Rzeszów", "Poznań"], result
    # Deselecting removes only that one, and off-list values survive.
    assert result["afterRemove"] == ["Kraków", "Rzeszów", "Poznań"], result
    print("PASS: off-list selections survive adding and removing visible values")


def test_picker_staging_is_lossless_end_to_end() -> None:
    result = _harness("selection-lossless")
    # What the picker staged is byte-identical to what it displayed…
    assert result["staged"] == result["sources"], result
    # …and it resolves through the canonical builder to the same values.
    query = "op__driver_name=in&" + "&".join(
        "filter_exact__driver_name=" + quote(value, safe="") for value in result["staged"]
    )
    conditions, values, _a, error, _e = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(), _params(query)
    )
    assert error is None, error
    assert values == result["sources"], values
    assert conditions[0].count("%s") == len(values), conditions
    print("PASS: picker staging round-trips end to end through the canonical filter contract")


def test_picker_enforces_the_cap_without_truncating() -> None:
    result = _harness("selection-cap")
    assert result["boxReverted"], "the refused checkbox is reverted"
    assert result["limitStated"], "the cap is stated"
    assert result["exactWritten"] == 0, "a refused addition writes nothing"
    assert result["selectionUnchanged"], "the previous valid selection stands"
    assert result["textareaIntact"], "the submittable form still carries the previous 50 values"
    print("PASS: exceeding 50 is refused, and the previous valid selection is preserved")


def test_only_one_request_per_scope_while_one_is_in_flight() -> None:
    result = _harness("in-flight-reopen")
    assert result["afterOpen"] == 1, result
    # Closing and reopening while the first request is pending must not duplicate.
    assert result["afterReopenWhilePending"] == 1, result
    assert result["afterReopenWhenLoaded"] == 1, result
    print("PASS: reopening during a pending request issues no duplicate aggregate")


def test_a_failed_request_can_be_retried() -> None:
    result = _harness("failed-retry")
    assert result["afterFailure"] == 1, result
    assert result["afterReopen"] == 2, "a failure clears the guard so reopening retries"
    assert result["recovered"], "the retry renders"
    print("PASS: a failed distribution clears the in-flight guard and retries on reopen")


def test_local_search_filters_the_fetched_set_only() -> None:
    result = _harness("local-search")
    assert result["stagedInitial"] == ["Kowalski", "OFFLIST", "Nowak"], result
    assert result["beforeSearch"] == 3, result
    assert result["afterSearch"] == 1, "the list narrows to matching fetched rows"
    # No aggregate request on any keystroke.
    assert result["requestsAfterSearch"] == 1, result
    # Hiding a selected row neither deselects it nor drops staged state.
    assert result["stagedAfterSearch"] == ["Kowalski", "OFFLIST", "Nowak"], result
    assert result["stagedWhileHidden"] == ["Kowalski", "OFFLIST", "Nowak"], result
    assert result["afterClear"] == 3, "clearing the search restores every fetched row"
    assert result["stillChecked"], "a previously hidden selected row is still selected"
    # The list is still honestly described as partial.
    assert result["truncatedNoticeBefore"] and result["truncatedNoticeAfter"], result
    print("PASS: local search narrows the fetched set only, emits no request and drops no selection")


# ===========================================================================
def main() -> None:
    test_distribution_requires_an_accessible_dataset()
    test_distribution_requires_can_filter_rows()
    test_unapproved_and_non_filterable_columns_cannot_be_probed()
    test_the_refusal_never_distinguishes_its_cause()
    test_the_caller_cannot_choose_the_aggregation()

    test_categorical_query_shape()
    test_numeric_query_shape()
    test_families_without_an_approved_distribution_get_only_a_summary()
    test_the_scope_reuses_the_validated_filter_conditions()
    test_a_rejected_filter_in_the_view_state_refuses_the_aggregate()
    test_injection_shaped_filter_values_stay_parameters_in_the_aggregate()

    test_the_columns_own_filter_is_excluded_from_its_own_distribution()
    test_every_parameter_family_of_the_own_column_is_excluded()
    test_the_response_states_its_scope()

    test_categorical_payload_counts_and_markers()
    test_categorical_result_is_bounded_and_says_so()
    test_categorical_empty_scope()

    test_numeric_payload_has_the_approved_bucket_contract()
    test_numeric_bounds_are_decimal_exact()
    test_numeric_negative_and_zero_domains()
    test_numeric_degenerate_domains_are_defined_states()

    test_the_response_exposes_nothing_internal()
    test_a_failed_aggregate_degrades_without_exposing_the_error()
    test_audit_records_the_request_without_recording_values()
    test_a_forged_column_name_is_not_written_to_the_audit_verbatim()

    test_the_initial_page_runs_no_aggregate_and_embeds_no_values()
    test_distribution_urls_carry_the_scope_and_nothing_else()
    test_no_distribution_container_when_filtering_is_not_permitted()
    test_the_page_still_ships_no_inline_script()
    test_progressive_enhancement_survives()

    test_a_distribution_is_fetched_only_when_its_menu_opens()
    test_categorical_rendering_keeps_the_value_semantics()
    test_selecting_values_stages_the_existing_s3_filter_contract()
    test_the_histogram_marker_is_presentation_only()
    test_degenerate_numeric_domains_render_as_words_not_bars()
    test_a_stale_response_cannot_replace_newer_menu_state()
    test_a_distribution_failure_leaves_the_menu_usable()

    test_submit_guard_recovers_after_a_back_forward_cache_restore()

    test_distributions_add_no_persistence_and_degrade_without_s13()

    test_picker_values_round_trip_losslessly()
    test_the_human_newline_form_is_unchanged()
    test_exact_values_take_precedence_and_stay_bounded()
    test_full_filter_state_is_validated_before_faceted_exclusion()
    test_faceted_exclusion_covers_every_validated_filter_form()
    test_legacy_default_date_faceting_in_both_directions()
    test_trusted_audit_metadata_holds_only_resolved_columns()
    test_unauthenticated_and_authenticated_refusals_are_documented_correctly()
    test_picker_selection_preserves_off_list_values()
    test_picker_staging_is_lossless_end_to_end()
    test_picker_enforces_the_cap_without_truncating()
    test_only_one_request_per_scope_while_one_is_in_flight()
    test_a_failed_request_can_be_retried()
    test_local_search_filters_the_fetched_set_only()

    print("\nALL VALUE DISTRIBUTION TESTS PASSED")


if __name__ == "__main__":
    main()
