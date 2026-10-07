#!/usr/bin/env python3
"""`ARTIFACT_EXPLORER_EXPORT` — the operator table downloads what it shows.

Artifact Explorer gains one capability and only one: the table it already
renders can be downloaded as XLSX or CSV, at the CURRENT filtration rather than
the current page. Everything else about the screen is unchanged — it stays a
read-and-inspect operator surface with no artifact lifecycle, no ownership, no
mutation and no background-export lifecycle of its own.

The load-bearing assertions here are the two that an export can get wrong in a
way nobody notices until the file is already in a spreadsheet:

* **SCOPE.** The file is the whole filtered result. A file holding the fifty
  rows that happened to be on screen would look correct and be wrong, so the
  paging loop is exercised against a cursor that really honours LIMIT/OFFSET.
* **FIELD SUBSET.** An export is exactly where a screen's field subset gets
  bypassed by serialising the underlying row dict. `storage_key`, `bucket_name`,
  `metadata_json` and `owner_user_id` are in every row the query returns and in
  none of the table's cells; this suite proves they are in none of its bytes
  either, in both formats.

RBAC is asserted end to end: the fixture store holds artifacts the account may
not view, and the assertion is that they are absent from the FILE, resolved
through the same `user_can_access_artifact` predicate the list uses.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ops/tests_manual/test_artifact_explorer_export.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
import types
import typing
import zipfile
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

    post = patch = delete = get

    def on_event(self, *args, **kwargs):
        return lambda fn: fn


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        # Materialized once: the route hands over a one-shot iterator, and a
        # test that read it twice would report an empty second file.
        self._bytes = b"".join(body)
        self.body = self._bytes
        self.media_type = media_type
        self.headers = headers or {}

    def content(self) -> bytes:
        return self._bytes


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

VISIBLE_WORKFLOW = "alpha"
HIDDEN_WORKFLOW = "restricted"
HIDDEN_CLIENT = "SECRET99"

#: Fields present in every row the query returns and in no cell of the table.
#: None of them may appear in an exported file, in either format.
NEVER_EXPORTED = ("storage_key", "bucket_name", "owner_user_id", "content_type")
NEVER_EXPORTED_VALUES = ("artifacts/secret-key", "internal-bucket", "owner-9999", "application/x-internal")


def _artifact(index: int, *, kind: str, workflow: str, expired: bool = False) -> dict:
    return {
        "artifact_id": f"00000000-0000-0000-0000-{index:012d}",
        "kind": kind,
        "workflow_name": workflow,
        "stage_name": "stage1",
        "artifact_role": "output",
        "report_type": "monthly",
        "client_code": HIDDEN_CLIENT if workflow == HIDDEN_WORKFLOW else "ALPHA00001",
        "layout_version": 2,
        "run_id": f"11111111-1111-1111-1111-{index:012d}",
        "raw_file_id": None,
        "filename": f"file_{index}.csv",
        "display_filename": f"file_{index}.csv",
        "original_filename": f"orig_{index}.csv",
        # The four fields the table never renders, with values distinctive
        # enough that a leak into a file is unmistakable.
        "storage_key": "artifacts/secret-key",
        "bucket_name": "internal-bucket",
        "owner_user_id": "owner-9999",
        "content_type": "application/x-internal",
        "file_ext": "csv",
        "size_bytes": 1024 * (index + 1),
        "sha256": f"9f2c41ab8e7d{index:04d}" + "0" * 40,
        "metadata_json": {"internal": "must-not-export"},
        "expires_at": None,
        "expired_at": "2026-01-01T00:00:00+00:00" if expired else None,
        "created_at": f"2026-08-{(index % 28) + 1:02d} 04:00",
        "description": None,
        "manual_metadata_json": {},
        "tags": ["nightly", "verified"],
    }


def _store() -> list[dict]:
    items: list[dict] = []
    index = 0
    for _ in range(7):
        items.append(_artifact(index, kind="query_snapshot", workflow=VISIBLE_WORKFLOW))
        index += 1
    for _ in range(5):
        items.append(_artifact(index, kind="export_file", workflow=VISIBLE_WORKFLOW))
        index += 1
    items.append(_artifact(index, kind="export_file", workflow=VISIBLE_WORKFLOW, expired=True))
    index += 1
    for _ in range(11):
        items.append(_artifact(index, kind="audit_log", workflow=HIDDEN_WORKFLOW))
        index += 1
    return items


STORE = _store()


def _operator_user() -> dict:
    return {
        "user_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "username": "operator",
        "display_name": "Ola Nowak",
        "is_active": True,
        "is_admin": False,
        "permissions": [
            {
                "can_view": True,
                "can_preview": True,
                "can_download": False,
                "can_edit_annotations": False,
                "workflow_name": VISIBLE_WORKFLOW,
                "stage_name": None,
                "artifact_role": None,
                "report_type": None,
                "client_code": None,
                "file_ext": None,
                "layout_version": None,
                "tag": None,
            }
        ],
    }


def _authorized(user: dict, action: str = "view") -> list[dict]:
    return [a for a in STORE if api_main.user_can_access_artifact(user, a, action)]


# ---------------------------------------------------------------------------
# Fake database that really paginates
# ---------------------------------------------------------------------------


class _Cursor:
    """Answers the module's real SQL from `STORE`, honouring LIMIT/OFFSET.

    Honouring them is the point: the export's paging loop is the code that turns
    "the current page" into "the whole filtered result", and a cursor that
    ignored LIMIT would let a broken loop pass.
    """

    def __init__(self, state: "_DbState"):
        self.state = state
        self._rows: list[dict] = []
        self._one: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        params = list(params or [])
        self.state.statements.append((text, params))
        visible = [a for a in _authorized(self.state.user) if self.state.matches(a)]
        if text.startswith("SELECT kind, COUNT(*) AS total FROM artifacts"):
            counts: dict[str, int] = {}
            for artifact in _authorized(self.state.user):
                counts[artifact["kind"]] = counts.get(artifact["kind"], 0) + 1
            self._rows = [{"kind": k, "total": v} for k, v in sorted(counts.items())]
        elif text.startswith("SELECT COUNT(*) AS total FROM artifacts"):
            self._one = {"total": len(visible)}
        elif text.startswith("SELECT DISTINCT tag"):
            self._rows = []
        elif text.startswith("SELECT DISTINCT"):
            field = text.split()[2]
            values = sorted({str(a.get(field)) for a in _authorized(self.state.user) if a.get(field)})
            self._rows = [{"value": value} for value in values]
        else:
            limit, offset = None, 0
            if " LIMIT %s OFFSET %s" in text and len(params) >= 2:
                limit, offset = int(params[-2]), int(params[-1])
                self.state.pages.append((limit, offset))
            window = visible[offset:] if limit is None else visible[offset: offset + limit]
            self._rows = list(window)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, state: "_DbState"):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def cursor(self):
        return _Cursor(self.state)


class _DbState:
    def __init__(self, user: dict, *, kind_filter: list[str] | None = None):
        self.user = user
        self.kind_filter = kind_filter or []
        self.statements: list[tuple[str, list]] = []
        self.pages: list[tuple[int, int]] = []

    def matches(self, artifact: dict) -> bool:
        if self.kind_filter:
            return str(artifact.get("kind")) in {str(k) for k in self.kind_filter}
        return True


class _QueryParams:
    def __init__(self, pairs: list[tuple[str, str]]):
        self._pairs = pairs

    def get(self, key, default=None):
        for name, value in self._pairs:
            if name == key:
                return value
        return default

    def getlist(self, key):
        return [value for name, value in self._pairs if name == key]


class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/artifact-explorer/export", pairs=None):
        pairs = pairs or []
        self.url = _FakeUrl(path, "&".join(f"{k}={v}" for k, v in pairs))
        self.cookies = {}
        self.query_params = _QueryParams(list(pairs))


class _Patched:
    def __init__(self, user: dict, *, kind_filter: list[str] | None = None):
        self.state = _DbState(user, kind_filter=kind_filter)
        self.user = user
        self._saved: dict = {}

    def __enter__(self) -> "_Patched":
        self._saved = {
            "db_conn": api_main.db_conn,
            "get_current_artifact_user": api_main.get_current_artifact_user,
            "_database_export_schema_available": api_main._database_export_schema_available,
            "_artifact_browser_row": api_main._artifact_browser_row,
        }
        api_main.db_conn = lambda: _Conn(self.state)
        api_main.get_current_artifact_user = lambda request: self.user
        api_main._database_export_schema_available = lambda: False
        api_main._artifact_browser_row = lambda row: dict(row)
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, value in self._saved.items():
            setattr(api_main, name, value)
        return None


def _export(user: dict, *, pairs=None, kind_filter=None, **kwargs):
    with _Patched(user, kind_filter=kind_filter) as patched:
        response = api_main.artifact_explorer_export(_FakeRequest(pairs=pairs), **kwargs)
    return response, patched.state


def _csv_rows(response) -> list[list[str]]:
    text = response.content().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _xlsx_sheet(response) -> str:
    with zipfile.ZipFile(io.BytesIO(response.content())) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _test_export_columns_are_the_table_columns() -> None:
    """The file's header row IS the table's header row, minus `Actions`."""
    with _Patched(_operator_user()):
        table = api_main._artifact_explorer_artifacts_table(
            _authorized(_operator_user())[:1],
            user=_operator_user(),
            current_sort="created_at_desc",
            base_params={},
        )
    headers = [re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
    # The sort headers carry an arrow glyph inside the link text.
    headers = [re.sub(r"[↑↓▲▼\s]+$", "", h).strip() for h in headers]
    assert headers[-1] == "Actions", headers
    exported = [c["display_name"] for c in api_main._artifact_explorer_export_columns()]
    assert exported == headers[:-1], (exported, headers[:-1])
    print("PASS: exported columns are exactly the operator table's columns, in its order")


def _test_file_never_carries_unrendered_fields() -> None:
    keys = {c["column_name"] for c in api_main._artifact_explorer_export_columns()}
    for field in NEVER_EXPORTED:
        assert field not in keys, field
    for fmt in ("csv", "xlsx"):
        response, _ = _export(_operator_user(), format=fmt)
        blob = response.content()
        text = blob.decode("utf-8", errors="ignore") if fmt == "csv" else _xlsx_sheet(response)
        for value in NEVER_EXPORTED_VALUES:
            assert value not in text, f"{fmt}: unrendered field value {value!r} reached the file"
        assert "must-not-export" not in text, f"{fmt}: metadata_json reached the file"
    print("PASS: fields the table does not render reach neither format")


def _test_scope_is_the_filtered_result_not_the_page() -> None:
    user = _operator_user()
    expected = len(_authorized(user))
    saved = api_main.ARTIFACT_EXPLORER_EXPORT_PAGE_SIZE
    api_main.ARTIFACT_EXPLORER_EXPORT_PAGE_SIZE = 2  # force the paging loop to run
    try:
        response, state = _export(user, format="csv")
    finally:
        api_main.ARTIFACT_EXPLORER_EXPORT_PAGE_SIZE = saved
    rows = _csv_rows(response)
    assert len(rows) - 1 == expected, (len(rows) - 1, expected)
    assert len(state.pages) > 1, state.pages
    assert all(limit == 2 for limit, _ in state.pages), state.pages
    assert [offset for _, offset in state.pages] == list(range(0, expected, 2))[: len(state.pages)], state.pages
    print(f"PASS: the file is the whole filtered result ({expected} rows), paged, not the visible page")


def _test_filters_narrow_the_file_exactly_as_the_screen() -> None:
    user = _operator_user()
    pairs = [("kind", "export_file")]
    response, _ = _export(user, pairs=pairs, kind_filter=["export_file"], format="csv")
    rows = _csv_rows(response)
    expected = [a for a in _authorized(user) if a["kind"] == "export_file"]
    assert len(rows) - 1 == len(expected), (len(rows) - 1, len(expected))
    names = {row[2] for row in rows[1:]}
    assert names == {a["display_filename"] for a in expected}, names
    print("PASS: a filter that narrows the screen narrows the file to the same rows")


def _test_rbac_rows_are_absent_from_the_file() -> None:
    response, state = _export(_operator_user(), format="csv")
    body = response.content().decode("utf-8-sig")
    assert HIDDEN_CLIENT not in body, "an artifact the account may not view reached the file"
    hidden = [a for a in STORE if a["workflow_name"] == HIDDEN_WORKFLOW]
    for artifact in hidden:
        assert artifact["display_filename"] not in body, artifact["display_filename"]
    data_statements = [s for s, _ in state.statements if s.startswith("SELECT artifacts.")]
    assert data_statements, state.statements
    assert all("permission" in s or "EXISTS" in s or "is_admin" in s.lower() or "%s" in s for s in data_statements)
    print(f"PASS: {len(hidden)} unauthorized artifacts are absent from the file, by the list's own predicate")


def _test_ceiling_refuses_rather_than_truncates() -> None:
    saved = api_main.ARTIFACT_EXPLORER_MAX_EXPORT_ROWS
    api_main.ARTIFACT_EXPLORER_MAX_EXPORT_ROWS = 3
    try:
        response, state = _export(_operator_user(), format="csv")
    finally:
        api_main.ARTIFACT_EXPLORER_MAX_EXPORT_ROWS = saved
    assert isinstance(response, _HTMLResponse), type(response)
    assert response.status_code == 413, response.status_code
    html = response.body.decode("utf-8")
    assert portal_i18n.t("art.export.too_many.title") in html, html[:400]
    assert str(len(_authorized(_operator_user()))) in html, "the refusal does not name the row count"
    assert "3" in html, "the refusal does not name the limit"
    # One query, not a full walk of a set it already refused.
    assert len(state.pages) == 1, state.pages
    print("PASS: an oversized export is refused with both numbers and costs one query")


def _test_format_contract() -> None:
    response, _ = _export(_operator_user())
    assert response.media_type.startswith("application/vnd.openxmlformats"), response.media_type
    assert response.headers["Content-Disposition"].endswith('.xlsx"'), response.headers
    sheet = _xlsx_sheet(response)
    assert sheet.count("<row ") == len(_authorized(_operator_user())) + 1, sheet.count("<row ")

    response, _ = _export(_operator_user(), format="csv")
    assert response.media_type == "text/csv; charset=utf-8", response.media_type
    assert response.content().startswith(b"\xef\xbb\xbf"), "CSV lost its BOM"

    for bad in ("pdf", "xls", "json"):
        try:
            _export(_operator_user(), format=bad)
        except _HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
        else:
            raise AssertionError(f"format={bad} was accepted")
    print("PASS: XLSX default, CSV on request, every other format refused with 400")


def _test_machine_values_not_screen_values() -> None:
    response, _ = _export(_operator_user(), format="csv")
    rows = _csv_rows(response)
    header, first = rows[0], rows[1]
    hash_index = header.index(portal_i18n.t("art.col.hash"))
    size_index = header.index(portal_i18n.t("art.col.size"))
    expected_hash = _authorized(_operator_user())[0]["sha256"]
    assert first[hash_index] == expected_hash, (first[hash_index], expected_hash)
    assert first[hash_index] != api_main._short_id(expected_hash), "the file kept the screen's short hash"
    assert first[size_index].isdigit(), first[size_index]
    tags_index = header.index("Tags")
    assert first[tags_index] == "nightly, verified", first[tags_index]
    state_index = header.index(portal_i18n.t("art.col.state"))
    assert first[state_index] in {portal_i18n.t("art.state.verified"), portal_i18n.t("art.state.removed")}
    print("PASS: full digests and byte counts, not the screen's shortened and formatted forms")


def _test_export_bar_on_the_catalogue_page() -> None:
    with _Patched(_operator_user()) as patched:
        response = api_main.artifact_explorer_index(_FakeRequest(path="/artifact-explorer"))
    html = response.body.decode("utf-8")
    assert "art-export-bar" in html, "the export bar is missing from the catalogue page"
    assert "/artifact-explorer/export?" in html, html[:200]
    assert "format=xlsx" in html and "format=csv" in html, "both formats must be offered"
    links = re.findall(r'href="(/artifact-explorer/export\?[^"]*)"', html)
    assert len(links) == 2, links
    for link in links:
        assert "offset=" not in link and "limit=" not in link, f"the export link carries pagination: {link}"
        assert "sort=created_at_desc" in link, f"the export link dropped the sort: {link}"
    caption = re.search(r'<span class="art-export-caption">(.*?)</span>', html, re.S)
    assert caption and str(len(_authorized(_operator_user()))) in caption.group(1), caption
    assert "data-grid-export.js" not in html, "Database Explorer's export module leaked onto Artifacts"
    # Zero rows: no button rather than a header-only file.
    assert api_main._artifact_explorer_export_bar(export_path="/x", params={}, total=0) == ""
    print("PASS: the catalogue page offers both formats, without pagination, and names the count")


def _test_multi_value_filter_fields_match_the_route() -> None:
    """The export's parameter shapes are read off the live list route."""
    hints = typing.get_type_hints(api_main.artifact_explorer_index)
    declared = {
        name
        for name, hint in hints.items()
        if name in api_main.ARTIFACT_EXPLORER_FILTER_FIELDS and "List[str]" in str(hint).replace("list[str]", "List[str]")
    }
    assert declared == set(api_main.ARTIFACT_EXPLORER_LIST_MULTI_FILTER_FIELDS), (
        declared, set(api_main.ARTIFACT_EXPLORER_LIST_MULTI_FILTER_FIELDS)
    )
    print("PASS: repeatable filter fields agree with the list route's own signature")


def _test_folder_export_follows_the_folder_type() -> None:
    user = _operator_user()
    calls: list[str] = []

    def _fake_list(name):
        def _inner(folder_id, *, sort, limit, offset, user=None, action=None):
            calls.append(name)
            rows = _authorized(user)
            return {
                "data": rows[offset: offset + limit],
                "meta": {"limit": limit, "offset": offset, "count": len(rows), "total": len(rows)},
            }
        return _inner

    saved = (
        api_main._require_virtual_folder,
        api_main._list_folder_artifacts,
        api_main._list_smart_folder_artifacts,
    )
    api_main._list_folder_artifacts = _fake_list("manual")
    api_main._list_smart_folder_artifacts = _fake_list("smart")
    try:
        for folder_type, expected in (("manual", "manual"), ("smart", "smart")):
            calls.clear()
            api_main._require_virtual_folder = lambda fid, ft=folder_type: {
                "folder_id": fid, "folder_name": "Raporty 2026", "folder_type": ft
            }
            with _Patched(user):
                response = api_main.artifact_explorer_folder_export(
                    "ffffffff-ffff-ffff-ffff-ffffffffffff", _FakeRequest(), format="csv"
                )
            assert calls and set(calls) == {expected}, (folder_type, calls)
            rows = _csv_rows(response)
            assert len(rows) - 1 == len(_authorized(user)), len(rows)
            assert "raporty-2026" in response.headers["Content-Disposition"].lower(), response.headers
    finally:
        (
            api_main._require_virtual_folder,
            api_main._list_folder_artifacts,
            api_main._list_smart_folder_artifacts,
        ) = saved
    print("PASS: a folder exports through the same branch its page renders, named after the folder")


def _test_export_bar_on_the_folder_page() -> None:
    """A folder's table offers the download too, at that folder's route."""
    user = _operator_user()
    rows = _authorized(user)
    saved = (api_main._get_virtual_folder_detail, api_main._artifact_explorer_folder_create_form)
    api_main._get_virtual_folder_detail = lambda fid, **kw: {
        "folder": {"folder_id": fid, "folder_name": "Raporty 2026", "folder_type": "manual",
                   "description": None, "parent_folder_id": None},
        "artifacts": {"data": rows, "meta": {"limit": 50, "offset": 0,
                                             "count": len(rows), "total": len(rows)}},
        "child_folders": [],
        "breadcrumbs": [],
    }
    api_main._artifact_explorer_folder_create_form = lambda fid=None: ""
    try:
        with _Patched(user):
            response = api_main.artifact_explorer_folder_detail(
                "ffffffff-ffff-ffff-ffff-ffffffffffff", _FakeRequest(path="/artifact-explorer/folders")
            )
    finally:
        (api_main._get_virtual_folder_detail, api_main._artifact_explorer_folder_create_form) = saved
    html = response.body.decode("utf-8")
    links = re.findall(r'href="(/artifact-explorer/folders/[^"]*/export\?[^"]*)"', html)
    assert len(links) == 2, links
    for link in links:
        assert "offset=" not in link and "limit=" not in link, link
    assert str(len(rows)) in html, "the folder caption must name the row count"
    print("PASS: a virtual folder's table offers the same download at its own route")


def _test_translation_keys_resolve() -> None:
    keys = portal_i18n.available_keys()
    for key in (
        "art.export.caption",
        "art.export.xlsx",
        "art.export.csv",
        "art.export.too_many.title",
        "art.export.too_many.body",
    ):
        assert key in keys, f"missing translation key {key}"
        assert portal_i18n.t(key) != key, key
    assert "{total}" not in portal_i18n.t("art.export.caption", total=7)
    assert "7" in portal_i18n.t("art.export.caption", total=7)
    print("PASS: export copy resolves through the shared translation catalogue")


def main() -> None:
    _test_export_columns_are_the_table_columns()
    _test_file_never_carries_unrendered_fields()
    _test_scope_is_the_filtered_result_not_the_page()
    _test_filters_narrow_the_file_exactly_as_the_screen()
    _test_rbac_rows_are_absent_from_the_file()
    _test_ceiling_refuses_rather_than_truncates()
    _test_format_contract()
    _test_machine_values_not_screen_values()
    _test_export_bar_on_the_catalogue_page()
    _test_multi_value_filter_fields_match_the_route()
    _test_folder_export_follows_the_folder_type()
    _test_export_bar_on_the_folder_page()
    _test_translation_keys_resolve()
    print("\nALL PASS: ARTIFACT_EXPLORER_EXPORT")


if __name__ == "__main__":
    main()
