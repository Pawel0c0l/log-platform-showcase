#!/usr/bin/env python3
"""Manual regression tests for Artifact Explorer UI Phase 4.

The host venv used for manual tests may not include API-container packages
such as FastAPI, so this file installs tiny import stubs before importing
``api.main``.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_artifact_explorer_phase4.py
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
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

    def post(self, *args, **kwargs):
        return lambda fn: fn

    def patch(self, *args, **kwargs):
        return lambda fn: fn

    def delete(self, *args, **kwargs):
        return lambda fn: fn

    def on_event(self, *args, **kwargs):
        return lambda fn: fn


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        self.body = body
        self.media_type = media_type
        self.headers = headers or {}


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


ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
RUN_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"
RAW_FILE_ID = "11e44cf1-7421-47b6-8d5e-f75675b37d88"


def _artifact_row(**overrides):
    row = {
        "artifact_id": ARTIFACT_ID,
        "run_id": RUN_ID,
        "created_at": datetime(2026, 5, 12, 22, 11, 44, tzinfo=timezone.utc),
        "kind": "REPORT",
        "filename": 'unsafe"<script>.txt',
        "content_type": "text/plain",
        "size_bytes": 12345,
        "sha256": "abc",
        "storage_backend": "S3",
        "storage_key": "workflow_b/stage_2_clean/key.txt",
        "raw_file_id": RAW_FILE_ID,
        "workflow_name": "workflow_b",
        "stage_name": "stage_2_clean",
        "artifact_role": "debug_sample",
        "report_type": "report_207",
        "client_code": "CLIENT_A",
        "display_filename": 'unsafe"<script>.txt',
        "original_filename": "source.xls",
        "file_ext": "txt",
        "layout_version": 2,
        "metadata_json": {"note": "<unsafe>"},
    }
    row.update(overrides)
    return row


def _html(response) -> str:
    return response.body.decode("utf-8")


def _test_list_page_renders_filters_table_and_actions() -> None:
    old_list = api_main._list_artifact_browser_items
    old_facets = api_main._get_artifact_browser_facets
    old_kind_counts = api_main._get_artifact_kind_counts
    # S11 added the RBAC-scoped artifact-kind rail; its bounded aggregate is
    # another real database read the list route now makes.
    api_main._get_artifact_kind_counts = lambda **kwargs: [("report_pack", 1)]
    api_main._list_artifact_browser_items = lambda **kwargs: {
        "data": [api_main._artifact_browser_row(_artifact_row())],
        "meta": {"limit": 50, "offset": 0, "count": 1, "total": 1},
    }
    api_main._get_artifact_browser_facets = lambda **kwargs: {
        "workflow_name": ["workflow_a", "workflow_b"],
        "stage_name": ["stage_2_clean"],
        "artifact_role": ["debug_sample"],
        "report_type": ["report_207"],
        "original_filename": ["source.xls", "source-2.xls"],
        "display_filename": ['unsafe"<script>.txt'],
        "file_ext": ["txt"],
        "client_code": ["CLIENT_A"],
        "kind": ["REPORT"],
        "layout_version": [1, 2],
        "tags": ["reviewed"],
    }
    try:
        html = _html(
            api_main.artifact_explorer_index(
                workflow_name=["workflow_b"],
                client_code=["CLIENT_A"],
                file_ext=["txt"],
                sort="workflow_stage",
                limit=50,
                offset=0,
            )
        )
    finally:
        api_main._list_artifact_browser_items = old_list
        api_main._get_artifact_browser_facets = old_facets
        api_main._get_artifact_kind_counts = old_kind_counts

    for field in (
        'name="workflow_name"',
        'name="stage_name"',
        'name="artifact_role"',
        'name="report_type"',
        'name="original_filename"',
        'name="display_filename"',
        'name="file_ext"',
        'name="client_code"',
        'name="kind"',
        'name="search"',
        'name="date_from"',
        'name="date_to"',
        'name="layout_version"',
        'name="run_id_search"',
        'name="raw_file_id_search"',
    ):
        assert field in html, field
    assert 'name="sort"' in html, html
    assert 'class="combo-input" id="workflow_name" name="workflow_name"' in html, html
    assert 'value="workflow_b"' in html, html
    assert 'data-value="workflow_b"' in html, html
    assert 'data-value="source.xls"' in html, html
    assert '<input type="checkbox" value="workflow_b" checked>' in html, html
    assert 'class="combo-input" id="client_code" name="client_code"' in html, html
    assert 'class="combo-input" id="original_filename" name="original_filename"' in html, html
    assert 'class="combo-input" id="display_filename" name="display_filename"' in html, html
    assert 'class="combo-input" id="kind" name="kind"' in html, html
    assert '<input type="checkbox" value="CLIENT_A" checked>' in html, html
    assert 'data-value="source.xls"' in html, html
    assert '<option value="workflow_stage" selected>workflow_stage</option>' in html, html
    assert 'data-artifact-filters="1"' in html, html
    # S11 added the approved `aria-sort` semantics to the sortable headers; the
    # sort links themselves are unchanged.
    assert '<th class="sortable" aria-sort="none"><a class="sort-label"' in html, html
    assert "sort-arrow" not in html, html
    assert "sort=workflow_name_asc" in html, html
    assert "sort=file_ext_asc" in html, html
    assert "workflow_name=workflow_b" in html, html
    assert "client_code=CLIENT_A" in html, html
    assert "file_ext=txt" in html, html
    assert "Extension" in html, html
    assert "Original filename" in html, html
    assert 'data-copy-value="CLIENT_A"' in html, html
    assert 'data-copy-value="txt"' in html, html
    assert "unsafe&quot;&lt;script&gt;.txt" in html, html
    assert "source.xls" in html, html
    assert 'data-copy-value="source.xls"' in html, html
    assert 'title="Click to copy"' in html, html
    # S11 moved the module's progressive enhancement out of an inline <script>
    # into a versioned page-scoped asset. Assert the page loads it, and assert
    # the behaviour on the asset itself rather than on the rendered markup.
    assert '/static/js/artifact-explorer.js?v=' in html, html
    module_js = (REPO_ROOT / "api" / "static" / "js" / "artifact-explorer.js").read_text(encoding="utf-8")
    assert 'navigator.clipboard.writeText' in module_js, "clipboard enhancement lost"
    assert 'input.name + "_search"' in module_js, "combo search-parameter fallback lost"
    assert 'data-copy-status' in html, html
    assert 'data-copy-value="Details"' not in html, html
    assert f"/artifact-explorer/artifacts/{ARTIFACT_ID}" in html, html
    assert f"/artifact-explorer/artifacts/{ARTIFACT_ID}/download" in html, html
    assert "Showing 1 of 1 artifacts" in html, html
    print("PASS: list page renders filters, escaped artifact rows, and actions")


def _test_searchable_multiselect_inputs_and_selected_values() -> None:
    facets = {
        "workflow_name": ["workflow_a", "workflow_b"],
        "stage_name": ["stage_1_fetch", "stage_2_clean"],
        "artifact_role": ["cleaned", "debug_sample"],
        "report_type": ["report_207", "report_602"],
        "original_filename": ["backup.xls", "source.xls"],
        "display_filename": ["cleaned.csv", "summary.csv"],
        "file_ext": ["csv", "txt"],
        "client_code": ["CLIENT_A", "CLIENT_B"],
        "kind": ["REPORT"],
        "layout_version": [1, 2],
        "tags": ["reviewed", "speeding"],
    }
    values = {
        "workflow_name": ["workflow_b", "workflow_a"],
        "stage_name": ["stage_2_clean"],
        "artifact_role": ["debug_sample"],
        "report_type": ["report_207"],
        "original_filename": ["source.xls", "backup.xls"],
        "display_filename": ["cleaned.csv"],
        "file_ext": ["txt"],
        "client_code": ["CLIENT_A"],
        "kind": ["REPORT"],
        "layout_version": ["2"],
        "tag": ["reviewed"],
        "limit": 50,
        "sort": "created_at_desc",
    }
    html = api_main._artifact_explorer_filter_inputs(values, facets)
    for field in (
        "workflow_name",
        "stage_name",
        "artifact_role",
        "report_type",
        "original_filename",
        "display_filename",
        "file_ext",
        "client_code",
        "kind",
        "layout_version",
        "tag",
    ):
        assert f'data-combo="{field}"' in html, field
        assert f'class="combo-input" id="{field}" name="{field}"' in html, field
    assert 'value="workflow_a, workflow_b"' in html, html
    assert 'data-value="workflow_a"' in html, html
    assert 'data-value="workflow_b"' in html, html
    assert 'data-value="reviewed"' in html, html
    assert 'data-value="cleaned.csv"' in html, html
    assert 'data-value="REPORT"' in html, html
    assert '<input type="checkbox" value="reviewed" checked>' in html, html
    assert '<input type="checkbox" value="source.xls" checked>' in html, html
    assert '<input type="checkbox" value="backup.xls" checked>' in html, html
    print("PASS: searchable multi-select inputs render selected comma-separated values and facet options")


def _test_filter_inputs_preserve_typed_search_and_exact_precedence() -> None:
    facets = {
        "workflow_name": ["workflow_b"],
        "stage_name": ["stage_2_clean"],
        "artifact_role": ["cleaned"],
        "report_type": ["report_207", "report_250"],
        "original_filename": ["207 raport.xls", "250 raport.xls"],
        "display_filename": ["workflow_b_report_207.csv"],
        "file_ext": ["csv"],
        "client_code": ["CLIENT_A"],
        "kind": ["REPORT"],
        "layout_version": [1, 2],
        "tags": ["reviewed"],
    }
    values = {
        "original_filename_search": "207",
        "display_filename_search": "workflow_b",
        "stage_name_search": "clean",
        "report_type": ["report_207"],
        "report_type_search": "250",
        "run_id_search": "1111",
        "raw_file_id_search": "ac38",
        "limit": 50,
        "sort": "created_at_desc",
    }
    html = api_main._artifact_explorer_filter_inputs(values, facets)
    assert 'id="original_filename" name="original_filename" type="text" autocomplete="off" value="207"' in html, html
    assert 'id="display_filename" name="display_filename" type="text" autocomplete="off" value="workflow_b"' in html, html
    assert 'id="stage_name" name="stage_name" type="text" autocomplete="off" value="clean"' in html, html
    assert 'id="report_type" name="report_type" type="text" autocomplete="off" value="report_207"' in html, html
    assert '<input type="checkbox" value="report_207" checked>' in html, html
    assert 'value="250"' not in html, html
    assert 'id="run_id" name="run_id_search" type="text" value="1111"' in html, html
    assert 'id="raw_file_id" name="raw_file_id_search" type="text" value="ac38"' in html, html
    print("PASS: filter inputs preserve typed search text and exact selections take precedence")


def _test_original_filename_filter_passes_values_and_preserves_selection() -> None:
    old_list = api_main._list_artifact_browser_items
    old_facets = api_main._get_artifact_browser_facets
    old_kind_counts = api_main._get_artifact_kind_counts
    api_main._get_artifact_kind_counts = lambda **kwargs: [("report_pack", 3)]
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        selected = set(api_main._normalize_filter_values(kwargs["filters"].get("original_filename")))
        rows = [
            api_main._artifact_browser_row(_artifact_row(original_filename="alpha.xls")),
            api_main._artifact_browser_row(_artifact_row(original_filename="beta.xls")),
            api_main._artifact_browser_row(_artifact_row(original_filename="gamma.xls")),
        ]
        if selected:
            rows = [row for row in rows if row.get("original_filename") in selected]
        return {"data": rows, "meta": {"limit": 50, "offset": 0, "count": len(rows), "total": len(rows)}}

    api_main._list_artifact_browser_items = fake_list
    api_main._get_artifact_browser_facets = lambda **kwargs: {
        "workflow_name": ["workflow_a", "workflow_b"],
        "stage_name": ["stage_2_clean"],
        "artifact_role": ["debug_sample"],
        "report_type": ["report_207"],
        "original_filename": ["alpha.xls", "beta.xls", 'unsafe"<source>.xls'],
        "display_filename": ["alpha.csv", "beta.csv"],
        "file_ext": ["txt"],
        "client_code": ["CLIENT_A"],
        "kind": ["REPORT"],
        "layout_version": [1, 2],
        "tags": ["reviewed"],
    }
    try:
        one_html = _html(
            api_main.artifact_explorer_index(
                workflow_name=["workflow_b"],
                original_filename=["alpha.xls"],
                client_code=["CLIENT_A"],
                limit=50,
                offset=0,
            )
        )
        one_filters = dict(captured["filters"])
        many_html = _html(
            api_main.artifact_explorer_index(
                workflow_name=["workflow_b"],
                original_filename=["alpha.xls", "beta.xls"],
                client_code=["CLIENT_A"],
                limit=50,
                offset=0,
            )
        )
        many_filters = dict(captured["filters"])
    finally:
        api_main._list_artifact_browser_items = old_list
        api_main._get_artifact_browser_facets = old_facets
        api_main._get_artifact_kind_counts = old_kind_counts

    assert one_filters["original_filename"] == ["alpha.xls"], one_filters
    assert one_filters["workflow_name"] == ["workflow_b"], one_filters
    assert one_filters["client_code"] == ["CLIENT_A"], one_filters
    assert "alpha.xls" in one_html, one_html
    assert 'data-copy-value="beta.xls"' not in one_html, one_html
    assert 'value="alpha.xls"' in one_html, one_html
    assert '<input type="checkbox" value="alpha.xls" checked>' in one_html, one_html

    assert many_filters["original_filename"] == ["alpha.xls", "beta.xls"], many_filters
    assert "alpha.xls" in many_html, many_html
    assert "beta.xls" in many_html, many_html
    assert "gamma.xls" not in many_html, many_html
    assert 'value="alpha.xls, beta.xls"' in many_html, many_html
    assert 'data-value="unsafe&quot;&lt;source&gt;.xls"' in many_html, many_html
    assert "original_filename=alpha.xls" in many_html, many_html
    assert "original_filename=beta.xls" in many_html, many_html
    assert "workflow_name=workflow_b" in many_html, many_html
    assert "client_code=CLIENT_A" in many_html, many_html
    print("PASS: original_filename filter renders options, preserves selections, and passes filters")


def _test_sortable_headers_preserve_filters_and_extension_column() -> None:
    params = {
        "workflow_name": ["workflow_b", "workflow_a"],
        "file_ext": ["csv"],
        "tag": ["reviewed"],
        "limit": 25,
        "offset": 50,
        "sort": "created_at_desc",
    }
    header = api_main._artifact_explorer_sort_header(
        "Extension",
        "file_ext",
        current_sort="file_ext_desc",
        base_params=params,
    )
    assert "Extension ↓" in header, header
    assert "sort=file_ext_asc" in header, header
    assert "sort=file_ext_desc" not in header, header
    assert header.count("↓") == 1, header
    assert "↑" not in header, header
    assert "sort-arrow" not in header, header
    assert 'aria-label="Sort Extension ascending"' in header, header
    assert "workflow_name=workflow_b" in header, header
    assert "workflow_name=workflow_a" in header, header
    assert "file_ext=csv" in header, header
    assert "tag=reviewed" in header, header
    assert "offset=50" in header, header
    print("PASS: sortable header link preserves filters and shows one descending indicator")


def _test_sortable_header_indicators() -> None:
    params = {"workflow_name": ["workflow_b"], "search": "speed", "limit": 25, "offset": 50}
    unsorted = api_main._artifact_explorer_sort_header(
        "Workflow",
        "workflow_name",
        current_sort="created_at_desc",
        base_params=params,
    )
    assert "Workflow ↑" not in unsorted, unsorted
    assert "Workflow ↓" not in unsorted, unsorted
    assert "sort=workflow_name_asc" in unsorted, unsorted
    assert "workflow_name=workflow_b" in unsorted, unsorted
    assert "search=speed" in unsorted, unsorted
    assert "limit=25" in unsorted, unsorted
    assert "offset=50" in unsorted, unsorted

    ascending = api_main._artifact_explorer_sort_header(
        "Workflow",
        "workflow_name",
        current_sort="workflow_name_asc",
        base_params=params,
    )
    assert "Workflow ↑" in ascending, ascending
    assert "Workflow ↓" not in ascending, ascending
    assert ascending.count("↑") == 1, ascending
    assert "sort=workflow_name_desc" in ascending, ascending
    assert 'aria-label="Sort Workflow descending"' in ascending, ascending

    descending = api_main._artifact_explorer_sort_header(
        "Workflow",
        "workflow_name",
        current_sort="workflow_name_desc",
        base_params=params,
    )
    assert "Workflow ↓" in descending, descending
    assert "Workflow ↑" not in descending, descending
    assert descending.count("↓") == 1, descending
    assert "sort=workflow_name_asc" in descending, descending
    assert 'aria-label="Sort Workflow ascending"' in descending, descending
    assert all("sort-arrow" not in header for header in (unsorted, ascending, descending))
    print("PASS: sortable headers show no inactive indicators and one active indicator")


def _test_extension_column_handles_missing_file_ext() -> None:
    old_list = api_main._list_artifact_browser_items
    old_facets = api_main._get_artifact_browser_facets
    old_kind_counts = api_main._get_artifact_kind_counts
    api_main._list_artifact_browser_items = lambda **kwargs: {
        "data": [api_main._artifact_browser_row(_artifact_row(file_ext=None, original_filename=None))],
        "meta": {"limit": 50, "offset": 0, "count": 1, "total": 1},
    }
    api_main._get_artifact_browser_facets = lambda **kwargs: {key: [] for key in api_main.ARTIFACT_BROWSER_FACET_FIELDS}
    api_main._get_artifact_kind_counts = lambda **kwargs: []
    try:
        html = _html(api_main.artifact_explorer_index(limit=50, offset=0, sort="file_ext_asc"))
    finally:
        api_main._list_artifact_browser_items = old_list
        api_main._get_artifact_browser_facets = old_facets
        api_main._get_artifact_kind_counts = old_kind_counts
    assert "Extension ↑" in html, html
    assert "<td>-</td>" in html, html
    assert "<td class=\"mono\">-</td>" in html, html
    assert 'data-copy-value="-"' not in html, html
    print("PASS: extension column uses file_ext and handles NULL values")


def _test_list_page_handles_no_artifacts() -> None:
    old_list = api_main._list_artifact_browser_items
    old_facets = api_main._get_artifact_browser_facets
    old_kind_counts = api_main._get_artifact_kind_counts
    api_main._list_artifact_browser_items = lambda **kwargs: {
        "data": [],
        "meta": {"limit": 50, "offset": 0, "count": 0, "total": 0},
    }
    api_main._get_artifact_browser_facets = lambda **kwargs: {key: [] for key in api_main.ARTIFACT_BROWSER_FACET_FIELDS}
    api_main._get_artifact_kind_counts = lambda **kwargs: []
    try:
        html = _html(api_main.artifact_explorer_index(limit=50, offset=0))
    finally:
        api_main._list_artifact_browser_items = old_list
        api_main._get_artifact_browser_facets = old_facets
        api_main._get_artifact_kind_counts = old_kind_counts
    assert "No artifacts matched the current filters" in html, html
    print("PASS: list page handles empty results")


def _test_legacy_layout_badge_is_visible() -> None:
    html = api_main._artifact_explorer_artifacts_table(
        [api_main._artifact_browser_row(_artifact_row(layout_version=1))],
        user=api_main._artifact_explorer_system_admin_user(),
        current_sort="created_at_desc",
        base_params={},
    )
    assert "1 (legacy layout)" in html, html
    print("PASS: list page labels layout_version=1 artifacts as legacy layout")


def _test_detail_page_renders_metadata_lineage_preview_and_download() -> None:
    old_get_row = api_main._get_artifact_browser_row
    old_get_run = api_main._get_run_summary
    old_get_raw = api_main._get_raw_file_summary
    old_get_folders = api_main._get_artifact_virtual_folders
    old_list_folders = api_main._list_all_virtual_folders_flat
    old_preview = api_main._artifact_explorer_preview_for_row
    api_main._get_artifact_browser_row = lambda artifact_id: _artifact_row()
    api_main._get_run_summary = lambda run_id: {"run_id": run_id, "status": "SUCCESS", "params": {"token": "***REDACTED***"}}
    api_main._get_raw_file_summary = lambda raw_file_id: {"id": raw_file_id, "stage2_report_type": "report_207"}
    api_main._get_artifact_virtual_folders = lambda artifact_id: []
    api_main._list_all_virtual_folders_flat = lambda: []
    api_main._artifact_explorer_preview_for_row = lambda row, artifact: {
        "artifact_id": ARTIFACT_ID,
        "preview_type": "text",
        "file_ext": "txt",
        "text": "<preview>",
        "truncated": False,
        "encoding": "utf-8",
    }
    try:
        html = _html(api_main.artifact_explorer_detail(ARTIFACT_ID))
    finally:
        api_main._get_artifact_browser_row = old_get_row
        api_main._get_run_summary = old_get_run
        api_main._get_raw_file_summary = old_get_raw
        api_main._get_artifact_virtual_folders = old_get_folders
        api_main._list_all_virtual_folders_flat = old_list_folders
        api_main._artifact_explorer_preview_for_row = old_preview

    assert "Artifact Metadata" in html, html
    assert "Lineage" in html, html
    assert "workflow_b/stage_2_clean/key.txt" in html, html
    assert "CLIENT_A" in html, html
    assert "&lt;unsafe&gt;" in html, html
    assert "&lt;preview&gt;" in html, html
    assert f"/artifact-explorer/artifacts/{ARTIFACT_ID}/download" in html, html
    print("PASS: detail page renders escaped metadata, lineage, preview, and download")


def _test_table_preview_escapes_cells() -> None:
    html = api_main._artifact_explorer_render_preview(
        {
            "artifact_id": ARTIFACT_ID,
            "preview_type": "table",
            "file_ext": "csv",
            "columns": ["name"],
            "rows": [{"name": "<cell>"}],
            "row_count_previewed": 1,
            "truncated": False,
            "delimiter": ";",
            "encoding": "utf-8-sig",
        },
        artifact_id=ARTIFACT_ID,
    )
    assert "&lt;cell&gt;" in html, html
    assert "<cell>" not in html, html
    print("PASS: table preview escapes unsafe cell values")


def _test_download_route_validation_and_missing_row() -> None:
    old_get_row = api_main._get_artifact_browser_row
    api_main._get_artifact_browser_row = lambda artifact_id: None
    try:
        try:
            api_main.artifact_explorer_download_artifact(ARTIFACT_ID, disposition="embed")
        except _HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
        else:
            raise AssertionError("invalid disposition should raise 400")

        try:
            api_main.artifact_explorer_download_artifact(ARTIFACT_ID)
        except _HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("missing artifact row should raise 404")
    finally:
        api_main._get_artifact_browser_row = old_get_row
    print("PASS: UI download route validates disposition and handles missing artifact")


def main() -> None:
    _test_list_page_renders_filters_table_and_actions()
    _test_searchable_multiselect_inputs_and_selected_values()
    _test_filter_inputs_preserve_typed_search_and_exact_precedence()
    _test_original_filename_filter_passes_values_and_preserves_selection()
    _test_sortable_headers_preserve_filters_and_extension_column()
    _test_sortable_header_indicators()
    _test_extension_column_handles_missing_file_ext()
    _test_list_page_handles_no_artifacts()
    _test_legacy_layout_badge_is_visible()
    _test_detail_page_renders_metadata_lineage_preview_and_download()
    _test_table_preview_escapes_cells()
    _test_download_route_validation_and_missing_row()


if __name__ == "__main__":
    main()
