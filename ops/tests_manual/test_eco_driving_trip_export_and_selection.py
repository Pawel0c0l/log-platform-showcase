#!/usr/bin/env python3
"""UI-20260820-09: spreadsheet export and cell selection for the trip table.

The reason this suite exists is the FIRST test in it. This screen sits under the
Stage 5 security contract in `docs/06_security.md`, which enumerates a safe row
DTO -- amended once, deliberately, to add the vehicle registration and nothing
else. An export is exactly where such a subset gets bypassed, by serialising the
underlying row dict instead of the columns the page shows.

So the property under test is not "the export works". It is that the export
CANNOT carry a field the table does not display, and that the property holds
structurally rather than by review: one column enumeration, `columns()`, serves
the screen, the file and the clipboard. A test that only checked today's field
list would pass forever while the guarantee rotted.
"""
from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from datetime import datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_eco_driving_explorer_api as api_t  # noqa: E402
import test_eco_driving_explorer_provider as prov  # noqa: E402
import test_eco_driving_explorer_trip_page as tp  # noqa: E402
# The Node harness driver, reused rather than rebuilt: the two tables are served
# by ONE module, so they must be proven by one harness.
import test_portal_database_grid_selection as grid  # noqa: E402

from api.eco_driving_explorer import page_routes as PR  # noqa: E402
from api.eco_driving_explorer import trip_export as X  # noqa: E402
from api.eco_driving_explorer import trip_view_models as T  # noqa: E402
from api.eco_driving_explorer.pages import EcoDrivingPages, PageResult, _ExportFile  # noqa: E402
from api.eco_driving_explorer.service import EcoDrivingApiService  # noqa: E402

IDENT = dict(client_code=api_t.CLIENT_CODE, ranking_family=api_t.FAMILY,
             period_key=api_t.WEEKLY_TOKEN, assigned_id="12345")

# Fields a trip row can carry that the table does not show. Route geometry,
# addresses, driver identity and the raw provider payload are all reachable from
# the underlying row and none of them may reach a file.
FORBIDDEN = {
    "latitude": 52.2297,
    "longitude": 21.0122,
    "start_address": "Marszalkowska 1",
    "end_address": "Pulawska 2",
    "driver_name": "Jan Kowalski",
    "driver_tag": "TAG-9",
    "email": "kowalski@example.com",
    "route_polyline": "u{~vFvyys@fS]",
    "raw_source": "provider-blob",
}


def _service(*, rows=1, total=None, ranking=True, trip=True, hostile=False):
    row = prov.make_trip_row()
    if hostile:
        row.update(FORBIDDEN)
    responses = dict(api_t._match_responses())
    responses["trips_list"] = [row] * rows
    responses["trips_count"] = [{"total_count": total if total is not None else rows}]
    backend = api_t.FakeBackend(
        access_map={(api_t.USER["user_id"], api_t.CLIENT_CODE):
                    api_t._access(ranking=ranking, trip=trip)},
        responses=responses,
    )
    return EcoDrivingPages(EcoDrivingApiService(backend))


def _export(pages, fmt="xlsx"):
    return pages.ranking_entry_trips_export(
        user=api_t.USER, export_format=fmt, **IDENT)


def test_export_cannot_carry_a_field_the_table_does_not_show():
    """The security property, against a row deliberately loaded with leaks."""

    pages = _service(hostile=True)
    xlsx = _export(pages, "xlsx")
    csv_bytes = _export(_service(hostile=True), "csv").body

    archive = zipfile.ZipFile(io.BytesIO(xlsx.body))
    xml = b"".join(archive.read(name) for name in archive.namelist())

    for field, value in FORBIDDEN.items():
        needle = str(value).encode()
        assert needle not in xml, f"{field} leaked into the XLSX"
        assert needle not in csv_bytes, f"{field} leaked into the CSV"
        assert field.encode() not in xml, f"{field} name leaked into the XLSX"
        assert field.encode() not in csv_bytes, f"{field} name leaked into the CSV"


def test_the_file_and_the_screen_share_one_column_enumeration():
    """Structural, not a field list: the guarantee above must not need a test.

    If these ever became two enumerations, the test above would keep passing for
    the fields it happens to name while the property it protects was already gone.
    """

    cols = T.columns()
    keys = [c.key for c in cols]
    assert len(keys) == len(set(keys)), "duplicate column key"

    html = tp.call(tp.make_pages()[0]).body_html
    rendered = re.findall(r'<th[^>]*data-eco-column="([^"]+)"', html)
    assert rendered == keys, (rendered, keys)

    header = X.to_csv(cols, []).decode("utf-8-sig").strip().split(X.CSV_DELIMITER)
    assert header == [c.export_label for c in cols]
    # Every column is answerable both ways round; neither side may grow alone.
    assert all(callable(c.display) and callable(c.value) for c in cols)


def test_clipboard_and_file_agree_on_every_value():
    """`Ctrl`+`C` on a rectangle and a download of the same rows are one number.

    Both go through `trip_export.text_cell`. If the table ever formatted its copy
    attribute itself, a distance could paste as `36.00` and download as `36,00`,
    and the mismatch would only surface in someone else's spreadsheet.
    """

    html = tp.call(tp.make_pages()[0]).body_html
    copied = [unescape(v) for v in
              re.findall(r'<td[^>]*data-eco-copy="([^"]*)"', html)]

    csv_text = _export(_service(), "csv").body.decode("utf-8-sig")
    from_file = csv_text.splitlines()[1].split(X.CSV_DELIMITER)

    width = len(T.columns())
    assert len(from_file) == width
    assert copied and len(copied) % width == 0, len(copied)
    # The page renders many rows; the fixture repeats one, so every rendered row
    # must equal the file's row cell for cell.
    for start in range(0, len(copied), width):
        assert copied[start:start + width] == from_file, (
            start // width, copied[start:start + width], from_file)


def test_export_scope_is_the_whole_filtered_view_not_the_visible_page():
    """Paged through `MAX_PAGE_SIZE`, never by raising the cap."""

    seen = []

    def fetch(page, limit):
        seen.append((page, limit))
        return [{}] * limit, {"total_count": 1200, "has_next": page < 3}

    rows = X.collect_rows(fetch)
    assert seen == [(1, X.PAGE_SIZE), (2, X.PAGE_SIZE), (3, X.PAGE_SIZE)]
    assert len(rows) == 3 * X.PAGE_SIZE
    assert X.PAGE_SIZE <= 500, "must not exceed queries.MAX_PAGE_SIZE"


def test_an_oversized_view_refuses_rather_than_truncating_silently():
    """A short spreadsheet nobody was told about is the worst outcome here."""

    calls = []

    def fetch(page, limit):
        calls.append(page)
        return [{}] * limit, {"total_count": 999_999, "has_next": True}

    try:
        X.collect_rows(fetch)
    except X.ExportTruncated as exc:
        assert exc.total == 999_999
        assert len(calls) == 1, "the limit must be caught on the first page"
    else:  # pragma: no cover
        raise AssertionError("an oversized view must not produce a file")

    result = _export(_service(rows=1, total=999_999), "xlsx")
    assert isinstance(result, PageResult) and result.status_code == 413
    assert str(X.MAX_EXPORT_ROWS) in result.body_html


def test_export_authorization_is_the_pages_own_not_a_second_gate():
    """No trip permission, no file -- and no bytes of any kind."""

    result = _export(_service(trip=False), "xlsx")
    assert not isinstance(result, _ExportFile)
    assert isinstance(result, PageResult) and result.status_code == 403


def test_an_unknown_format_falls_back_and_never_dumps_something_else():
    for candidate in ("../../etc/passwd", "html", "", "XLSX", "csv"):
        result = _export(_service(), candidate)
        assert isinstance(result, _ExportFile)
        assert result.extension in ("xlsx", "csv")
        if candidate == "csv":
            assert result.extension == "csv"


def test_the_download_filename_cannot_be_steered_by_the_query():
    """Hostile values are sanitised wherever they enter the name.

    Since `UI-20260827-05a` the builder takes the RESOLVED SCOPE rather than the
    request, so the query cannot reach it at all -- but the values in that scope
    still originate in the query, so the sanitising is still what stops a
    traversal or a header split. The hostile cases are therefore fed through the
    scope, which is the path they now actually take.
    """

    hostile = [
        (("assigned_id", "../../../etc/passwd"),),
        (("assigned_id", 'a"; rm -rf /; #'),),
        (("period_key", "x\r\nSet-Cookie: a=1"),),
        (("assigned_id", "Zażółć gęślą jaźń"),),
        (("month", "2026-08/../.."), ("weeks", "1,2\r\n")),
    ]
    for scope in hostile:
        name = PR._export_filename(scope, "xlsx")
        stem = name.rsplit(".", 1)[0]
        assert all(c in PR._FILENAME_SAFE or c == "_" for c in stem), name
        assert name.endswith(".xlsx")
        assert ".." not in name and "/" not in name and "\r" not in name

    assert PR._export_filename((), "csv") == "przejazdy.csv"


def test_csv_is_openable_by_a_polish_excel_without_an_import_wizard():
    body = _export(_service(), "csv").body
    assert body.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM required"
    text = body.decode("utf-8-sig")
    assert "\r\n" in text
    header, first = text.splitlines()[0], text.splitlines()[1]
    assert header.count(";") == first.count(";") == len(T.columns()) - 1
    # A distance is a number in the file, with the locale's decimal mark, and
    # the unit has moved into the heading so the cell stays numeric.
    assert re.search(r"(^|;)\d+,\d\d(;|$)", first), first
    assert any("(km)" in h for h in header.split(";")), header


def test_xlsx_cells_are_typed_so_the_columns_can_be_summed_and_sorted():
    pages = _service()
    book_bytes = _export(pages, "xlsx").body
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(book_bytes)).active
    header = [c.value for c in sheet[1]]
    assert header == [c.export_label for c in T.columns()]

    cols = T.columns()
    row = next(sheet.iter_rows(min_row=2, max_row=2))
    for col, cell in zip(cols, row):
        if cell.value is None:
            continue
        if col.key.endswith("_ts"):
            assert isinstance(cell.value, datetime), (col.key, cell.value)
        elif col.key == "trip_distance_km":
            assert isinstance(cell.value, (int, float)), (col.key, cell.value)
            assert not isinstance(cell.value, str)
    assert sheet.freeze_panes == "A2"


def test_the_page_declares_a_complete_selection_contract():
    result = tp.call(tp.make_pages()[0])
    html = result.body_html

    assert "js/data-grid-selection.js" in result.page_assets
    assert "data-db-sheet" in html
    # The module reads its vocabulary overrides off the host; without these it
    # would look for the Database Explorer's table and find nothing.
    for attribute in ('data-grid-table="table.eco-table"',
                      'data-grid-column-attr="data-eco-column"',
                      'data-grid-copy-attr="data-eco-copy"'):
        assert attribute in html, attribute
    # It writes enablement onto the host, and the CSS keys off that ancestor.
    assert html.index("data-db-sheet") < html.index("<table")
    # Selection is a visual state; without a live region it does not exist for a
    # screen reader.
    assert "data-db-select-status" in html and 'aria-live="polite"' in html
    assert "data-db-select-count" in html

    strings = json.loads(unescape(
        re.search(r'data-db-select-strings="([^"]+)"', html).group(1)))
    assert set(strings) == {"summary", "copied", "copyFailed", "row", "column", "cell"}
    # Three Polish plural categories, or the module announces the wrong noun.
    for key in ("row", "column", "cell"):
        assert len(strings[key]) == 3 and all(strings[key]), strings[key]


def test_selection_reaches_exactly_the_columns_the_table_shows():
    html = tp.call(tp.make_pages()[0]).body_html
    heads = re.findall(r'<th[^>]*data-eco-column="([^"]+)"', html)
    cells = re.findall(r'<td[^>]*data-eco-column="([^"]+)"', html)
    assert heads == [c.key for c in T.columns()]
    assert set(cells) == set(heads)
    # Every marked cell publishes a clipboard value, including empty ones -- a
    # missing attribute would silently shift a rectangle's columns on paste.
    marked = re.findall(r'<td[^>]*data-eco-column="[^"]+"[^>]*>', html)
    assert all("data-eco-copy=" in cell for cell in marked)


def test_an_empty_table_offers_neither_export_nor_selection():
    responses = dict(api_t._match_responses(),
                     trips_list=[], trips_count=[{"total_count": 0}])
    html = tp.call(tp.make_pages(responses=responses)[0]).body_html
    assert "eco-export-bar" not in html, "a header-only file helps nobody"
    assert "data-db-sheet" not in html
    assert "eco-select-footer" not in html


def test_the_export_route_is_registered_and_scoped_to_the_trip_query():
    class App:
        def __init__(self):
            self.routes = {}

        def add_api_route(self, path, endpoint, **kwargs):
            self.routes[path] = endpoint

    app = App()
    PR.register_eco_driving_page_routes(
        app, pages=tp.make_pages()[0],
        require_user=lambda request: api_t.USER,
        render=lambda result, user: result)
    assert PR.RANKING_ENTRY_TRIPS_EXPORT_PATH in app.routes
    # The export must not offer a page/limit knob: it is the whole filtered view,
    # and accepting those would imply the file follows the screen's pagination.
    source = Path(PR.__file__).read_text()
    body = source.split("async def eco_driving_ranking_entry_trips_export")[1]
    body = body.split("app.add_api_route(")[0]
    assert '_q(request, "page")' not in body
    assert '_q(request, "limit")' not in body


# ===========================================================================
# The module, actually executed
#
# Everything above proves the page EMITS a selection contract. None of it proves
# the module READS it: an ignored override would send the module looking for
# `table.db-table`, find nothing, and ship a table that silently never selects.
# These run the shipped JavaScript against a synthetic DOM built with the eco
# vocabulary.
# ===========================================================================

def test_the_module_selects_a_single_cell_in_the_eco_table():
    out = grid._run("eco-single-cell")
    assert out["enabled"] == "on"
    assert out["selected"] == ["1:driver_name"]
    assert out["active"] == "1:driver_name"
    assert out["footer"] == "zaznaczono 1 wiersz × 1 kolumna · 1 komórka"
    # A selection must never cost a request or a navigation.
    assert out["fetches"] == 0 and out["navigations"] == []


def test_the_module_forms_rectangles_by_pointer_and_by_keyboard():
    expected = ["1:trip_start", "1:driver_name", "2:trip_start", "2:driver_name"]
    for scenario in ("eco-rectangle", "eco-keyboard-extends"):
        out = grid._run(scenario)
        assert out["selected"] == expected, (scenario, out["selected"])
        assert out["footer"] == "zaznaczono 2 wiersze × 2 kolumny · 4 komórki"


def test_the_module_copies_the_cell_level_machine_value_as_tsv():
    """The eco table publishes its copy value on the cell, not an inner span."""

    out = grid._run("eco-copy-rectangle")
    assert len(out["clipboard"]) == 1
    text = out["clipboard"][0]
    assert text.split("\r\n") == [
        "2026-05-28T07:31:45.512+00:00\tKowalski 1",
        "2026-05-28T07:32:45.512+00:00\tKowalski 2",
    ], text


def test_an_empty_cell_keeps_its_column_in_the_pasted_rectangle():
    """Otherwise every column right of a blank shifts one left on paste."""

    out = grid._run("eco-empty-cell-copies-empty")
    assert out["clipboard"] == ["2026-05-28T07:30:45.512+00:00\t"]


def test_selection_is_off_below_the_responsive_cutoff():
    """A touch drag across cells fights the table's own scrolling."""

    out = grid._run("eco-disabled-below-cutoff")
    assert out["enabled"] == "off"
    assert out["selected"] == [] and out["active"] is None


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("PASS:", test.__name__)
    print(f"Eco Driving trip export and selection tests passed ({len(tests)} cases)")


if __name__ == "__main__":
    main()
