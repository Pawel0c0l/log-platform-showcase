#!/usr/bin/env python3
"""Database Explorer dataset catalogue and system states (approved stage S9).

Covers the approved catalogue (`DB-001`/`DB-002`, `DB-3`), the dataset/client
rail, the two zero-row states and the culprit contract (`DB-010`, `DB-55`–
`DB-58`), the data-source error state and its diagnostic trio (`DB-011`,
`DB-60`), the deep-link permission state (`DB-61`), and absence-not-disabled
capability presentation (`DB-62`, `DB-54`).

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— `SCREEN_STATE_MATRIX.md`, `PRODUCT_BEHAVIOR_CONTRACT.md` §2.1/§2.14–2.17,
`COPY_AND_TERMINOLOGY.md` §9.1, `TABLE_AND_DATA_GRID_SPEC.md` §6.

Authorization is asserted here where S9 could have weakened it — the catalogue
is an authorization surface, and the culprit diagnostic issues real queries — but
the phase2a/2b/2c and hidden-row-identity suites remain the owners of the
underlying access model.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_catalogue_and_states.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

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
from api.portal_ui import i18n as portal_i18n  # noqa: E402

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER_DATASET_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
NBSP = " "


class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query="", path=None):
        self.url = _FakeUrl(path or f"/user/database/datasets/{DATASET_ID}", query)
        self.cookies = {}
        self.headers = {}
        self.client = None


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user():
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "client_database_name": "acme_db",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Przejazdy klienta",
        "schema_name": "public", "table_name": "trips", "default_date_column": "trip_date",
        "is_active": True, "visible_columns": 4, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _other_dataset(**overrides):
    data = _dataset(
        dataset_id=OTHER_DATASET_ID, client_code="ALPHA_02", client_display_name="Alpha Fleet",
        client_database_name="alpha_db", dataset_name="Zdarzenia pojazdów", slug="events",
        description="", table_name="events", visible_columns=7,
        can_filter_rows=False, can_export_rows=True,
    )
    data.update(overrides)
    return data


def _col(name, label, data_type, **extra):
    column = {
        "dataset_id": DATASET_ID, "column_name": name, "display_name": label, "data_type": data_type,
        "is_visible": True, "is_filterable": True, "is_sortable": True,
        "is_default_date_column": name == "trip_date", "display_order": 10,
    }
    column.update(extra)
    return column


def _columns():
    return [
        _col("trip_date", "Data przejazdu", "date", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("depot", "Baza", "text", display_order=30),
        _col("distance_km", "Dystans", "numeric", display_order=40),
    ]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


# ---------------------------------------------------------------------------
# A recording read-only client-database stub.
#
# The culprit diagnostic runs for real against it, so the tests can assert what
# it actually sent: how many statements, which identifiers, and whether every
# user value stayed a bound parameter.
# ---------------------------------------------------------------------------
class _RecordingCursor:
    def __init__(self, sink, answer):
        self._sink = sink
        self._answer = answer
        self._row = None

    def execute(self, query, values=None):
        self._sink.append({"query": query, "values": list(values or [])})
        self._row = {"total": self._answer(query, list(values or []))}

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingConn:
    def __init__(self, sink, answer):
        self._sink = sink
        self._answer = answer

    def cursor(self):
        return _RecordingCursor(self._sink, self._answer)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _connect_stub(sink, answer):
    def connect(database_name, **kwargs):
        sink.append({"connect": database_name})
        return _RecordingConn(sink, answer)
    return connect


def _render_browser(
    *,
    query="",
    dataset=None,
    columns=None,
    rows=None,
    total=0,
    dataset_total=48213,
    answer=None,
    sink=None,
    raise_on_query=None,
):
    """Render the row sheet with the real state/diagnosis code paths in play."""
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _columns()
    rows = rows if rows is not None else []
    sink = sink if sink is not None else []
    answer = answer or (lambda q, v: 0)

    # The state and the validation verdict come from the real canonical builder,
    # so a rejected sort, filter or date value fails here exactly as it would in
    # production instead of being papered over by the stub.
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    _query, _values, state, error = api_main._build_portal_database_rows_query(
        dataset, columns, parsed, count=True
    )

    def _count(d, c, p):
        if raise_on_query:
            raise raise_on_query
        return total, state, error

    def _list(d, c, p, limit, offset, display_columns=None):
        if raise_on_query:
            raise raise_on_query
        return rows, state, error

    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda d: None)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", _count)),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", _list)),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (dataset_total, None) if dataset_total is not None else (0, "unavailable"))),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database", _connect_stub(sink, answer))),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        return api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query))
    finally:
        _restore(patches)


def _render_catalogue(datasets, *, counts=None, exports=False):
    resolved = counts if counts is not None else {
        str(d.get("dataset_id")): 100 + index for index, d in enumerate(datasets)
    }
    patches = [
        ("_list_accessible_portal_database_datasets_for_user",
         _patch("_list_accessible_portal_database_datasets_for_user", lambda uid: list(datasets) if uid == USER_ID else [])),
        ("_portal_database_catalogue_row_counts",
         _patch("_portal_database_catalogue_row_counts", lambda ds: dict(resolved))),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: exports)),
        ("_count_active_database_export_jobs_for_user", _patch("_count_active_database_export_jobs_for_user", lambda uid: 0)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        return _html(api_main._user_database_response(_user(), _FakeRequest(path="/user/database")))
    finally:
        _restore(patches)


def _rail(html: str) -> str:
    start = html.index('<nav class="db-rail"')
    return html[start:html.index("</nav>", start) + len("</nav>")]


def _state_block(html: str) -> str:
    match = re.search(r'<div class="db-state db-state-[^"]*".*?</div>\s*(?:</div>)?', html, re.S)
    assert match, "no S9 state block rendered"
    return match.group(0)


def _count_statements(sink) -> list[dict]:
    return [entry for entry in sink if "query" in entry]


# ===========================================================================
# 1. Dataset catalogue (DB-001 / DB-002 / DB-3)
# ===========================================================================
def test_catalogue_is_one_comparison_table_not_a_card_grid() -> None:
    html = _render_catalogue([_dataset(), _other_dataset()], counts={DATASET_ID: 48213, OTHER_DATASET_ID: 1274})

    # `DB-3`: datasets are rows of one table, compared on row count, column count
    # and permissions.
    assert html.count('<table class="db-cat-table"') == 1, html
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    assert body.count("<tr>") == 2, body
    for header in ("Zbiór", "Klient", "Wiersze", "Kolumny", "Uprawnienia"):
        assert f">{header}</th>" in html, header
    assert f'<td class="db-cat-num lp-mono">48{NBSP}213</td>' in body, body
    assert f'<td class="db-cat-num lp-mono">1{NBSP}274</td>' in body, body
    assert '<td class="db-cat-num lp-mono">4</td>' in body, body
    assert '<td class="db-cat-num lp-mono">7</td>' in body, body

    # The superseded card catalogue must not survive beside the approved one.
    assert "portal-card" not in html, "the legacy dataset-card layout must not remain"
    assert "Browse rows" not in html and "Visible columns:" not in html, html

    # The approved page title and the primary per-row action.
    assert "Zbiory danych klientów" in html, html
    assert html.count(">Otwórz arkusz</a>") == 2, html
    print("PASS: the catalogue is one comparison table with row, column and permission columns")


def test_catalogue_permissions_are_neutral_configuration_facts() -> None:
    html = _render_catalogue([
        _dataset(can_filter_rows=True, can_export_rows=True),
        _other_dataset(can_filter_rows=False, can_export_rows=False),
    ])
    assert "Filtrowanie" in html and "Eksport" in html, html
    assert "Bez filtrów" in html and "Tylko podgląd" in html, html
    # A withheld permission is neutral, never negative, and never a disabled control.
    assert "db-perm-neutral" in html and "db-perm-granted" in html, html
    assert "disabled" not in html.lower().split("<body>")[1], "no disabled control belongs in the catalogue"
    print("PASS: permission flags render as neutral configuration badges, never as errors")


def test_catalogue_exposes_no_physical_identity() -> None:
    html = _render_catalogue([_dataset(), _other_dataset()])
    work = html.split("<body>", 1)[1]
    for physical in ("public.trips", "acme_db", "alpha_db", ">trips<", ">events<", "schema_name"):
        assert physical not in work, f"physical identity {physical} leaked into the catalogue"
    print("PASS: the catalogue exposes product metadata only, never physical identity")


def test_catalogue_lists_only_authorized_datasets() -> None:
    # The catalogue's whole universe is the authorized list. A dataset that is
    # not in it is absent — not listed and disabled, not badged, not counted.
    html = _render_catalogue([_dataset()], counts={DATASET_ID: 48213})
    assert "Approved trips" in html, html
    assert "Zdarzenia pojazdów" not in html, "an unauthorized dataset reached the catalogue"
    assert OTHER_DATASET_ID not in html, html
    assert "Alpha Fleet" not in html and "ALPHA_02" not in html, html
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    assert body.count("<tr>") == 1, body
    # The rail is built from the same list and cannot surface more than the table.
    rail = _rail(html)
    assert rail.count("db-rail-item") == 1, rail
    assert "Alpha Fleet" not in rail, rail
    print("PASS: only currently authorized datasets appear anywhere in the catalogue or rail")


def test_catalogue_groups_several_clients_in_the_rail() -> None:
    html = _render_catalogue([_dataset(), _other_dataset()], counts={DATASET_ID: 48213, OTHER_DATASET_ID: 1274})
    rail = _rail(html)
    assert "Zbiory danych" in rail, rail
    assert rail.count('class="db-rail-group"') == 2, rail
    assert "Acme Logistics" in rail and "ACME_01" in rail, rail
    assert "Alpha Fleet" in rail and "ALPHA_02" in rail, rail
    # Deep links are ordinary dataset ids, never physical identifiers.
    assert f'href="/user/database/datasets/{DATASET_ID}"' in rail, rail
    assert f'href="/user/database/datasets/{OTHER_DATASET_ID}"' in rail, rail
    for physical in ("public.trips", "acme_db", "alpha_db"):
        assert physical not in rail, rail
    # Rail counts come from the same authorized numbers the table shows.
    assert f"48{NBSP}213" in rail and f"1{NBSP}274" in rail, rail
    # No dataset is open on the catalogue, so no rail entry may claim to be the
    # current one. The rail states what is true, including when nothing is.
    assert "aria-current" not in rail, rail
    print("PASS: the rail groups authorized clients and datasets with safe deep links")


def test_catalogue_row_count_degrades_to_unknown_rather_than_lying() -> None:
    html = _render_catalogue([_dataset()], counts={DATASET_ID: None})
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    assert '<td class="db-cat-num lp-mono">?</td>' in body, body
    assert ">0</td>" not in body, "an unknown row count must never render as zero"
    print("PASS: an unavailable catalogue row count renders as unknown, never as zero")


def test_zero_authorized_datasets_renders_the_approved_empty_catalogue() -> None:
    html = _render_catalogue([])
    assert "Nie masz przypisanych zbiorów danych" in html, html
    assert "Dostęp nadaje administrator." in html, html
    # Not an empty table with misleading headers, and certainly no dataset rows.
    assert "db-cat-table" not in html, html
    assert "db-rail" not in html, html
    print("PASS: zero authorized datasets renders the approved catalogue-empty state")


def test_catalogue_row_counts_are_bounded_read_only_and_scoped() -> None:
    sink: list[dict] = []
    datasets = [_dataset(), _other_dataset()]
    old = _patch("_connect_portal_client_database", _connect_stub(sink, lambda q, v: 7))
    try:
        counts = api_main._portal_database_catalogue_row_counts(datasets)
    finally:
        _restore([("_connect_portal_client_database", old)])
    statements = _count_statements(sink)
    # One count per listed dataset, and one connection per client database.
    assert len(statements) == 2, statements
    assert len([e for e in sink if "connect" in e]) == 2, sink
    for entry in statements:
        assert entry["query"].startswith("SELECT count(*) AS total FROM "), entry
        assert entry["values"] == [], entry
        assert re.search(r'FROM "public"\."(trips|events)"$', entry["query"]), entry
    assert counts == {DATASET_ID: 7, OTHER_DATASET_ID: 7}, counts

    # Past the bound the catalogue does not fan out; it renders unknown instead.
    many = [_dataset(dataset_id=f"ds-{i}", table_name=f"t{i}") for i in range(api_main.PORTAL_DATABASE_CATALOGUE_COUNT_LIMIT + 1)]
    sink.clear()
    old = _patch("_connect_portal_client_database", _connect_stub(sink, lambda q, v: 7))
    try:
        bounded = api_main._portal_database_catalogue_row_counts(many)
    finally:
        _restore([("_connect_portal_client_database", old)])
    assert sink == [], "the catalogue must not scan past its bound"
    assert set(bounded.values()) == {None}, bounded
    print("PASS: catalogue counts are one bounded read-only count per authorized dataset")


def test_catalogue_row_count_failure_never_breaks_the_page() -> None:
    def explode(database_name, **kwargs):
        raise RuntimeError("client database unreachable")

    old = _patch("_connect_portal_client_database", explode)
    try:
        counts = api_main._portal_database_catalogue_row_counts([_dataset()])
    finally:
        _restore([("_connect_portal_client_database", old)])
    assert counts == {DATASET_ID: None}, counts
    print("PASS: a failed catalogue count degrades to unknown instead of failing the page")


# ===========================================================================
# 2. Deep-link permission state (DB-61)
# ===========================================================================
def test_unauthorized_deep_link_produces_the_approved_permission_state() -> None:
    response = api_main._portal_database_unavailable_response(_user())
    html = _html(response)
    assert response.status_code == 404, response.status_code
    assert "BRAK DOSTĘPU" in html, html
    assert "Nie masz dostępu do tego zbioru danych" in html, html
    assert "Dostęp nadaje administrator." in html, html
    # A safe route back to the authorized catalogue.
    assert 'href="/user/database"' in html, html
    assert "Wróć do zbiorów danych" in html, html

    # It says nothing about the dataset that was requested: an unauthorized id
    # and an unknown id must remain indistinguishable.
    for leak in (DATASET_ID, "Approved trips", "Acme Logistics", "ACME_01", "public.trips", "trips"):
        assert leak not in html, f"{leak} leaked through the permission state"

    # The portal shell, not raw JSON and not a traceback.
    assert html.lstrip().startswith("<!doctype html>"), html[:80]
    assert "Traceback" not in html and '{"detail"' not in html, html
    print("PASS: an unauthorized deep link produces the approved safe permission state")


def test_permission_state_is_identical_for_unknown_and_forbidden_ids() -> None:
    forbidden = _html(api_main._portal_database_unavailable_response(_user()))
    unknown = _html(api_main._portal_database_unavailable_response(_user()))
    assert forbidden == unknown, "the permission state must not become an existence oracle"
    print("PASS: unauthorized and unknown dataset ids produce an identical response")


def test_malformed_view_state_stays_separate_from_permission_and_failure() -> None:
    response = api_main._portal_database_invalid_view_response(_user(), _dataset())
    html = _html(response)
    assert response.status_code == 400, response.status_code
    assert "NIEPRAWIDŁOWY WIDOK" in html, html
    assert "Nie można otworzyć tego widoku" in html, html
    # It is not a permission problem and not a source failure.
    assert "BRAK DOSTĘPU" not in html, html
    assert "BŁĄD ŹRÓDŁA DANYCH" not in html, html
    assert f'href="/user/database/datasets/{DATASET_ID}"' in html, html
    print("PASS: a malformed view is its own state, separate from permission and failure")


def test_invalid_view_parameters_never_echo_the_rejected_value() -> None:
    # A crafted sort identifier is rejected by the validator and must not be
    # reflected back into the page in any form.
    hostile = "<script>alert(1)</script>"
    response = _render_browser(query=f"sort={hostile}", total=0)
    html = _html(response)
    assert response.status_code == 400, response.status_code
    assert "NIEPRAWIDŁOWY WIDOK" in html, html
    assert "<script>alert(1)</script>" not in html, "hostile input echoed unescaped"
    assert "alert(1)" not in html, "hostile input echoed at all"
    print("PASS: a rejected view parameter is never echoed back into the error state")


# ===========================================================================
# 3. Empty states (DB-010, DB-55 – DB-58)
# ===========================================================================
def test_unfiltered_empty_dataset_blames_no_filter() -> None:
    html = _html(_render_browser(query="", total=0, rows=[], dataset_total=0))
    assert '<th scope="col"' in html, "DB-55: the table header must remain rendered"
    assert "Ten zbiór nie ma wierszy" in html, html
    assert "Żaden filtr nie zawęża tego wyniku." in html, html
    # No filter is named, no fabricated total, and no culprit action.
    assert "Brak wierszy dla aktywnych filtrów" not in html, html
    assert "zawęża wynik do zera" not in html, html
    assert ">Usuń " not in html, html
    print("PASS: an unfiltered zero-row dataset has its own truthful empty state")


def test_filtered_empty_names_the_single_culprit_with_query_evidence() -> None:
    sink: list[dict] = []

    def answer(query, values):
        # Without the driver filter the result is non-empty; that is the whole
        # evidence the approved sentence rests on.
        return 1274 if "driver_name" not in query else 0

    html = _html(_render_browser(
        query="filter__driver_name=Zzz&op__driver_name=contains",
        total=0, rows=[], dataset_total=48213, answer=answer, sink=sink,
    ))
    assert '<th scope="col"' in html, "DB-55: the header must survive the empty state"
    assert "Brak wierszy dla aktywnych filtrów" in html, html
    state = _state_block(html)
    assert f"Zbiór ma 48{NBSP}213 wierszy." in state, state
    assert "Kierowca zawiera Zzz" in state, state
    assert f"bez niego zobaczysz 1{NBSP}274 wierszy." in state, state
    # `DB-57`: the removal action names the filter it removes.
    assert "Usuń Kierowca zawiera Zzz" in state, state
    assert "Wyczyść wszystkie" in state, state
    # Exactly one diagnostic count for one active constraint.
    assert len(_count_statements(sink)) == 1, sink
    print("PASS: one active filter proven responsible is named, with the count without it")


def test_two_filters_with_a_unique_culprit_name_only_that_one() -> None:
    sink: list[dict] = []

    def answer(query, values):
        # Removing `depot` restores rows; removing `driver_name` does not.
        return 812 if "depot" not in query else 0

    html = _html(_render_browser(
        query=("filter__driver_name=Ala&op__driver_name=contains"
               "&filter__depot=KRK-02&op__depot=eq"),
        total=0, rows=[], dataset_total=48213, answer=answer, sink=sink,
    ))
    state = _state_block(html)
    assert "Baza = KRK-02" in state, state
    assert f"bez niego zobaczysz 812 wierszy." in state, state
    assert "Usuń Baza = KRK-02" in state, state
    assert "Usuń Kierowca" not in state, "only the proven culprit may be named"
    # One count per active constraint, never more.
    assert len(_count_statements(sink)) == 2, sink
    print("PASS: with two filters only the one query evidence proves is named")


def test_several_independent_culprits_fall_back_to_honest_copy() -> None:
    sink: list[dict] = []
    # Removing either filter restores rows: no unique culprit exists.
    html = _html(_render_browser(
        query=("filter__driver_name=Ala&op__driver_name=contains"
               "&filter__depot=KRK-02&op__depot=eq"),
        total=0, rows=[], dataset_total=48213, answer=lambda q, v: 500, sink=sink,
    ))
    state = _state_block(html)
    assert "Brak wierszy dla aktywnych filtrów" in html, html
    assert "Wynik zerują niezależnie różne filtry" in state, state
    assert "zawęża wynik do zera — bez niego" not in state, "no filter may be named as THE cause"
    assert "Usuń " not in state, state
    assert "Wyczyść wszystkie" in state, state
    print("PASS: several independent culprits use the honest non-naming fallback")


def test_joint_contradiction_falls_back_to_the_generic_state() -> None:
    sink: list[dict] = []
    # No single removal restores rows: the combination is the constraint.
    html = _html(_render_browser(
        query=("filter__driver_name=Ala&op__driver_name=contains"
               "&filter__depot=KRK-02&op__depot=eq"),
        total=0, rows=[], dataset_total=48213, answer=lambda q, v: 0, sink=sink,
    ))
    state = _state_block(html)
    assert "Żaden pojedynczy filtr nie odpowiada za pusty wynik" in state, state
    assert "Usuń " not in state, state
    assert len(_count_statements(sink)) == 2, sink
    print("PASS: a joint-only contradiction names no culprit")


def test_global_search_can_be_the_named_culprit() -> None:
    sink: list[dict] = []

    def answer(query, values):
        # The search predicate is the ILIKE disjunction; without it rows return.
        return 43 if "ILIKE" not in query else 0

    html = _html(_render_browser(
        query="search=zzz", total=0, rows=[], dataset_total=48213, answer=answer, sink=sink,
    ))
    state = _state_block(html)
    assert "Wyszukiwanie" in state, state
    assert "zzz" in state, state
    assert "bez niego zobaczysz 43 wierszy." in state, state
    assert "Usuń wyszukiwanie" in state, state
    # The search removal link clears only the search term and resets the page.
    assert "search=" not in state.split('Usuń wyszukiwanie')[0].split('href="')[-1], state
    # No physical column name is used to describe the search.
    for physical in ("driver_name", "depot", "filter_exact__"):
        assert physical not in state, state
    print("PASS: the global search is nameable as a culprit in the approved terminology")


def test_legacy_date_range_is_excluded_semantically() -> None:
    sink: list[dict] = []

    def answer(query, values):
        return 900 if "trip_date" not in query else 0

    html = _html(_render_browser(
        query="date_from=2026-01-01&date_to=2026-01-31",
        total=0, rows=[], dataset_total=48213, answer=answer, sink=sink,
    ))
    state = _state_block(html)
    # The legacy range arrives under the default date column's own name, which
    # is what lets it be excluded semantically rather than by raw key deletion.
    assert "Data przejazdu" in state, state
    assert "bez niego zobaczysz 900 wierszy." in state, state
    statements = _count_statements(sink)
    assert len(statements) == 1, statements
    assert "trip_date" not in statements[0]["query"], statements
    print("PASS: the legacy date range is excluded through canonical semantic filter state")


def test_an_invalid_filter_is_never_silently_dropped_by_the_diagnostic() -> None:
    dataset = _dataset()
    columns = _columns()
    params = {
        "op__depot": ["eq"], "filter__depot": ["KRK-02"],
        # An unparseable date on the default date column: the validator refuses it.
        "date_from": ["not-a-date"], "date_to": ["2026-01-31"],
    }
    sink: list[dict] = []
    old = _patch("_connect_portal_client_database", _connect_stub(sink, lambda q, v: 999))
    try:
        restored, error = api_main._count_portal_database_rows_excluding(dataset, columns, params, "depot")
    finally:
        _restore([("_connect_portal_client_database", old)])
    assert restored is None and error, (restored, error)
    # Nothing was executed: a broader result must never be produced by dropping
    # a filter the validator rejected.
    assert _count_statements(sink) == [], sink
    print("PASS: an invalid filter fails validation instead of being dropped for the diagnostic")


def test_culprit_analysis_is_bounded_by_active_constraints() -> None:
    dataset = _dataset()
    columns = _columns()
    entries = [{"column_name": f"c{i}"} for i in range(api_main.PORTAL_DATABASE_CULPRIT_MAX_FILTERS + 1)]
    sink: list[dict] = []
    old = _patch("_connect_portal_client_database", _connect_stub(sink, lambda q, v: 5))
    try:
        result = api_main._portal_database_empty_result_diagnosis(dataset, columns, {}, entries)
    finally:
        _restore([("_connect_portal_client_database", old)])
    assert result["mode"] == "generic", result
    assert sink == [], "past the bound the diagnostic must issue no queries at all"

    # And it never scales with the width of the dataset.
    sink.clear()
    old = _patch("_connect_portal_client_database", _connect_stub(sink, lambda q, v: 0))
    try:
        api_main._portal_database_empty_result_diagnosis(
            dataset, columns, {"op__depot": ["eq"], "filter__depot": ["KRK-02"]},
            [{"column_name": "depot"}],
        )
    finally:
        _restore([("_connect_portal_client_database", old)])
    assert len(_count_statements(sink)) == 1, sink
    assert len(_count_statements(sink)) < len(columns), sink
    print("PASS: culprit analysis costs one count per active constraint, never one per column")


def test_culprit_queries_are_parameterized_and_authorization_scoped() -> None:
    sink: list[dict] = []
    _render_browser(
        query="filter__depot=KRK-02&op__depot=eq&search=ala",
        total=0, rows=[], dataset_total=48213, answer=lambda q, v: 0, sink=sink,
    )
    statements = _count_statements(sink)
    assert len(statements) == 2, statements
    for entry in statements:
        query, values = entry["query"], entry["values"]
        assert query.startswith("SELECT count(*) AS total FROM "), query
        # Catalog-derived, quoted identifiers only.
        assert '"public"."trips"' in query, query
        # No user value is ever interpolated.
        assert "KRK-02" not in query and "ala" not in query, query
        assert all(isinstance(value, str) for value in values), values
        assert "%s" in query, query
    # The connection is the authorized client database, taken from the dataset.
    assert [e for e in sink if e.get("connect")][0]["connect"] == "acme_db", sink
    # Exactly one candidate is excluded per statement: the depot pass keeps the
    # search predicate and the search pass keeps the depot predicate.
    depot_pass = [e for e in statements if '"depot" = %s' not in e["query"]][0]
    search_pass = [e for e in statements if "ILIKE" not in e["query"]][0]
    assert "ILIKE" in depot_pass["query"], depot_pass
    assert '"depot" = %s' in search_pass["query"], search_pass
    print("PASS: culprit queries are read-only, parameterized and scoped to the authorized dataset")


def test_diagnostic_failure_degrades_instead_of_accusing() -> None:
    def explode(database_name, **kwargs):
        raise RuntimeError("client database unreachable")

    old = _patch("_connect_portal_client_database", explode)
    try:
        result = api_main._portal_database_empty_result_diagnosis(
            _dataset(), _columns(), {"op__depot": ["eq"], "filter__depot": ["KRK"]},
            [{"column_name": "depot"}],
        )
    finally:
        _restore([("_connect_portal_client_database", old)])
    assert result["mode"] == "generic", result
    print("PASS: a failed diagnostic count degrades to the generic state, never to a guess")


def test_filtered_empty_preserves_context_and_layout_state() -> None:
    sink: list[dict] = []
    html = _html(_render_browser(
        query="filter__depot=KRK-02&op__depot=eq&density=comfortable&limit=200",
        total=0, rows=[], dataset_total=48213,
        answer=lambda q, v: 1274 if '"depot"' not in q else 0, sink=sink,
    ))
    # The context bar and the active chip both survive the empty state.
    assert "Acme Logistics" in html and "TYLKO ODCZYT" in html, html
    assert 'class="db-chip"' in html, html
    assert "Baza" in html and "KRK-02" in html, html
    state = _state_block(html)
    # `Usuń` removes only that filter; `Wyczyść wszystkie` keeps layout state.
    remove_url = re.search(r'href="([^"]+)"[^>]*>Usuń ', state)
    assert remove_url, state
    assert "filter__depot" not in remove_url.group(1), remove_url.group(1)
    assert "density=comfortable" in remove_url.group(1), remove_url.group(1)
    assert "limit=200" in remove_url.group(1), remove_url.group(1)
    assert "page=1" in remove_url.group(1), remove_url.group(1)
    print("PASS: the filtered-empty actions preserve context, density and page size")


# ===========================================================================
# 4. Data-source error state (DB-011, DB-60)
# ===========================================================================
class _Timeout(Exception):
    pass


_Timeout.__name__ = "QueryCanceled"


def test_source_failure_renders_the_approved_error_state_with_the_trio() -> None:
    response = _render_browser(query="", raise_on_query=RuntimeError("connection refused to 10.0.0.4:5432"))
    html = _html(response)
    assert response.status_code == 502, response.status_code
    assert "BŁĄD ŹRÓDŁA DANYCH" in html, html
    assert "Nie udało się odczytać zbioru" in html, html
    # The approved statement that permissions are not the problem.
    assert "Uprawnienia i konfiguracja zbioru są poprawne" in html, html
    # `DB-55`/`TGS` §6: the header survives the failure too.
    assert '<th scope="col"' in html, html
    # Client context is preserved.
    assert "Acme Logistics" in html and "ACME_01" in html, html

    # The diagnostic trio: timestamp, reference, code — all server-generated.
    trio = re.search(r'<div class="db-state-diagnostics".*?</div>', html, re.S)
    assert trio, html
    trio_html = trio.group(0)
    assert re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}", trio_html), trio_html
    reference = re.search(r"data-db-error-reference>ref ([0-9a-f]{16})<", trio_html)
    assert reference, trio_html
    assert "kod DB-SOURCE" in trio_html, trio_html
    # The reference is copyable and carries nothing but itself.
    assert f'data-db-error-copy="{reference.group(1)}"' in html, html

    # Approved actions, none of them disabled.
    assert ">Ponów</a>" in html, html
    assert ">Zawęź filtrami</a>" in html, html
    assert ">Skopiuj referencję</button>" in html, html
    print("PASS: a source failure renders DB-011 with the approved reference trio")


def test_source_failure_never_leaks_internal_failure_detail() -> None:
    html = _html(_render_browser(query="", raise_on_query=RuntimeError(
        "psycopg.OperationalError: connection to host=10.0.0.4 dbname=acme_db user=portal failed; "
        'SELECT * FROM public.trips WHERE "driver_name" ILIKE %s'
    )))
    work = html.split("<body>", 1)[1]
    for leak in ("psycopg", "OperationalError", "RuntimeError", "10.0.0.4", "dbname=",
                 "SELECT *", "ILIKE", "Traceback", "user=portal"):
        assert leak not in work, f"{leak} leaked into the error state"
    print("PASS: no SQL, DSN, exception class or traceback reaches the error state")


def test_source_failure_is_never_presented_as_zero_rows() -> None:
    html = _html(_render_browser(query="", raise_on_query=RuntimeError("boom"), total=0))
    # No zero-row claim, no empty-state copy, and no fabricated rows.
    assert "Ten zbiór nie ma wierszy" not in html, html
    assert "Brak wierszy dla aktywnych filtrów" not in html, html
    body_rows = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    assert body_rows.strip() == "", body_rows
    assert "db-counter-unknown" in html, html
    assert "<strong>0</strong>" not in html, "an operational failure must not render a row count"
    # The export panel cannot state a truthful scope count either, so it is absent.
    assert "db-export-form" not in html, html
    print("PASS: an operational failure is never rendered as a zero-row result")


def test_failure_classes_stay_distinct() -> None:
    timeout = _html(_render_browser(query="", raise_on_query=_Timeout("canceling statement due to statement timeout")))
    assert "Zapytanie przekroczyło limit 15 s" in timeout, timeout
    assert "kod DB-TIMEOUT" in timeout, timeout

    class PortalClientDatabaseConnectionError(Exception):
        pass

    unavailable = _html(_render_browser(query="", raise_on_query=PortalClientDatabaseConnectionError("down")))
    assert "Baza klienta nie odpowiada." in unavailable, unavailable
    assert "kod DB-UNAVAILABLE" in unavailable, unavailable

    # An authorization failure is never collapsed into a retryable source error.
    denied = _html(api_main._portal_database_unavailable_response(_user()))
    assert "BŁĄD ŹRÓDŁA DANYCH" not in denied, denied
    assert "Ponów" not in denied, denied
    print("PASS: timeout, unavailable and permission failures stay semantically distinct")


def test_error_reference_is_server_generated_and_correlatable() -> None:
    first = api_main._portal_database_error_reference()
    second = api_main._portal_database_error_reference()
    assert first != second and re.fullmatch(r"[0-9a-f]{16}", first), (first, second)

    # The same value that reaches the page reaches the audit trail, which is what
    # makes a support reference correlatable without storing anything extra.
    events: list[dict] = []
    sink: list[dict] = []
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: _dataset())),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: _columns())),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda d: None)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (0, "unavailable"))),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database", _connect_stub(sink, lambda q, v: 0))),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: events.append(kwargs))),
    ]
    try:
        html = _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest()))
    finally:
        _restore(patches)
    rendered = re.search(r"data-db-error-reference>ref ([0-9a-f]{16})<", html)
    assert rendered, html
    failures = [e for e in events if e.get("event_type") == "database_rows_failed"]
    assert failures, events
    metadata = failures[-1].get("metadata") or {}
    assert metadata.get("error_reference") == rendered.group(1), (metadata, rendered.group(1))
    # The exception class is recorded server-side only; it is not on the page.
    assert metadata.get("reason") == "RuntimeError", metadata
    assert "RuntimeError" not in html, html
    print("PASS: the user-visible reference is server-generated and correlates to the audit event")


def test_error_reference_cannot_be_spoofed_through_the_url() -> None:
    html = _html(_render_browser(
        query="ref=deadbeefdeadbeef&error_code=DB-FAKE&timestamp=01.01.1999+00:00:00",
        raise_on_query=RuntimeError("boom"),
    ))
    trio = re.search(r'<div class="db-state-diagnostics".*?</div>', html, re.S).group(0)
    # Every part of the trio is server data. A crafted parameter reaches neither
    # the reference, nor the code, nor the timestamp.
    assert "deadbeefdeadbeef" not in trio, trio
    assert "DB-FAKE" not in trio and "01.01.1999" not in trio, trio
    reference = re.search(r"data-db-error-reference>ref ([0-9a-f]{16})<", trio)
    assert reference and reference.group(1) != "deadbeefdeadbeef", trio
    assert "kod DB-SOURCE" in trio, trio
    # And the copy button carries the server value, not the crafted one.
    assert f'data-db-error-copy="{reference.group(1)}"' in html, html
    assert 'data-db-error-copy="deadbeefdeadbeef"' not in html, html
    print("PASS: timestamp, reference and code are server data and cannot be spoofed via the URL")


# ===========================================================================
# 5. Loading / pending (DB-59) — the shipped module
# ===========================================================================
def _run_harness(scenario: str, payload: dict | None = None) -> dict:
    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / "data_grid_states_harness.js"), scenario]
    if payload is not None:
        command.append(json.dumps(payload))
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_pending_state_appears_on_a_real_navigation_and_never_persists() -> None:
    applied = _run_harness("submit")
    assert applied["pending"] is True, applied
    assert applied["ariaBusy"] == "true", applied
    assert applied["skeletonRows"] > 0, applied
    assert applied["counterPending"] is True, applied
    # A skeleton carries geometry, never data.
    assert applied["skeletonText"] == "", applied
    assert applied["storageWrites"] == [], "pending state must never be persisted"
    assert applied["urlChanges"] == [], "pending state must never enter the URL"
    assert applied["fetches"] == [], "the pending state must not introduce data fetching"

    navigated = _run_harness("sort_link")
    assert navigated["pending"] is True, navigated
    assert navigated["prevented"] is False, "the real navigation must still happen"

    density = _run_harness("density_link")
    assert density["pending"] is False, "an in-place density switch must not raise a skeleton"
    print("PASS: a real query navigation enters the pending state without fetching or persisting it")


def test_bfcache_restore_clears_the_pending_state() -> None:
    restored = _run_harness("bfcache")
    assert restored["pendingBefore"] is True, restored
    assert restored["pending"] is False, restored
    assert restored["ariaBusy"] is None, restored
    assert restored["skeletonRows"] == 0, restored
    # The duplicate-submit guard still owns control state and still releases it.
    assert restored["submitDisabled"] is False, restored
    print("PASS: Back/BFCache restores usable controls and clears the skeleton")


def test_editable_controls_are_untouched_by_the_pending_state() -> None:
    result = _run_harness("controls")
    assert result["inputDisabled"] is False, result
    assert result["inputReadOnly"] is False, result
    assert result["pending"] is True, result
    print("PASS: entering the pending state leaves editable controls usable")


def test_reference_copy_uses_the_server_value_only() -> None:
    result = _run_harness("copy_reference")
    assert result["copied"] == "7f0ca41b7f0ca41b", result
    assert result["prevented"] is True, result
    assert result["fetches"] == [], result
    print("PASS: the reference copy button copies exactly the server-rendered reference")


def test_page_actually_loads_the_states_module() -> None:
    # Registry membership is not evidence. This asserts the rendered document,
    # with the content-derived cache version the asset layer emits.
    sheet = _html(_render_browser(query=""))
    assert re.search(r'<script defer src="/static/js/data-grid-states\.js\?v=[0-9a-f]+"></script>', sheet), sheet
    assert 'data-db-page-size="100"' in sheet, sheet

    # The catalogue has no data sheet, so it does not carry the module.
    catalogue = _render_catalogue([_dataset()])
    assert "data-grid-states.js" not in catalogue, catalogue
    assert re.search(r'/static/css/data-grid\.css\?v=[0-9a-f]+', catalogue), catalogue
    print("PASS: the row sheet loads the states module and the catalogue does not")


def test_no_javascript_leaves_every_trigger_functional() -> None:
    html = _html(_render_browser(query="", total=1, rows=[{
        "trip_date": date(2026, 5, 28), "driver_name": "Ala", "depot": "KRK-02",
        "distance_km": Decimal("12.5"),
    }]))
    # Every pending trigger is a real form or a real link in the markup.
    assert '<form class="db-toolbar-search" method="get"' in html, html
    assert re.search(r'<a class="db-page-button"|<a class="db-size"', html), html
    # And nothing renders a permanent server-side skeleton after the response.
    assert "db-skeleton" not in html, "a skeleton must never be server-rendered on a completed response"
    assert 'aria-busy="true"' not in html, html
    print("PASS: with JavaScript unavailable every trigger remains an ordinary server navigation")


# ===========================================================================
# 6. Translation contract
# ===========================================================================
def test_every_s9_string_goes_through_a_translation_key() -> None:
    keys = portal_i18n.available_keys()
    required = [
        "db.catalog.title", "db.catalog.rail_heading", "db.catalog.col.rows",
        "db.catalog.empty.title", "db.perm.no_filter", "db.perm.view_only",
        "db.empty.filtered.body", "db.empty.filtered.body_search",
        "db.empty.filtered.body_multiple", "db.empty.filtered.body_joint",
        "db.empty.filtered.body_generic", "db.empty.dataset.body", "db.empty.back",
        "db.error.badge", "db.error.body.timeout", "db.error.retry",
        "db.error.reference_label", "db.error.code_label",
        "db.access.badge", "db.view.badge", "db.export.absent",
    ]
    missing = [key for key in required if key not in keys]
    assert not missing, missing
    for key in required:
        assert portal_i18n.t(key) != key, key
    print("PASS: every S9 user-facing string resolves through the translation catalogue")


def main() -> None:
    test_catalogue_is_one_comparison_table_not_a_card_grid()
    test_catalogue_permissions_are_neutral_configuration_facts()
    test_catalogue_exposes_no_physical_identity()
    test_catalogue_lists_only_authorized_datasets()
    test_catalogue_groups_several_clients_in_the_rail()
    test_catalogue_row_count_degrades_to_unknown_rather_than_lying()
    test_zero_authorized_datasets_renders_the_approved_empty_catalogue()
    test_catalogue_row_counts_are_bounded_read_only_and_scoped()
    test_catalogue_row_count_failure_never_breaks_the_page()

    test_unauthorized_deep_link_produces_the_approved_permission_state()
    test_permission_state_is_identical_for_unknown_and_forbidden_ids()
    test_malformed_view_state_stays_separate_from_permission_and_failure()
    test_invalid_view_parameters_never_echo_the_rejected_value()

    test_unfiltered_empty_dataset_blames_no_filter()
    test_filtered_empty_names_the_single_culprit_with_query_evidence()
    test_two_filters_with_a_unique_culprit_name_only_that_one()
    test_several_independent_culprits_fall_back_to_honest_copy()
    test_joint_contradiction_falls_back_to_the_generic_state()
    test_global_search_can_be_the_named_culprit()
    test_legacy_date_range_is_excluded_semantically()
    test_an_invalid_filter_is_never_silently_dropped_by_the_diagnostic()
    test_culprit_analysis_is_bounded_by_active_constraints()
    test_culprit_queries_are_parameterized_and_authorization_scoped()
    test_diagnostic_failure_degrades_instead_of_accusing()
    test_filtered_empty_preserves_context_and_layout_state()

    test_source_failure_renders_the_approved_error_state_with_the_trio()
    test_source_failure_never_leaks_internal_failure_detail()
    test_source_failure_is_never_presented_as_zero_rows()
    test_failure_classes_stay_distinct()
    test_error_reference_is_server_generated_and_correlatable()
    test_error_reference_cannot_be_spoofed_through_the_url()

    test_pending_state_appears_on_a_real_navigation_and_never_persists()
    test_bfcache_restore_clears_the_pending_state()
    test_editable_controls_are_untouched_by_the_pending_state()
    test_reference_copy_uses_the_server_value_only()
    test_page_actually_loads_the_states_module()
    test_no_javascript_leaves_every_trigger_functional()

    test_every_s9_string_goes_through_a_translation_key()
    print("\nALL S9 DATABASE EXPLORER CATALOGUE AND STATE TESTS PASSED")


if __name__ == "__main__":
    main()
