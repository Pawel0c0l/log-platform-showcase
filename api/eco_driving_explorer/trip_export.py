"""Export of the contributing-trip table, in the formats a spreadsheet wants.

THE SECURITY PROPERTY, first, because it is the reason this module is shaped
this way. The columns are not enumerated here. They come from
`trip_view_models.columns()` — the SAME objects the page renders — so a field
the page does not show has no path into a file. There is no second list to drift
out of sync with the first, and adding a column to the export is not possible
without adding it to the screen.

That matters because this screen sits under the Stage 5 contract in
`docs/06_security.md`, which enumerates a safe row DTO and was amended once,
deliberately and with owner authorization, to add the vehicle registration and
nothing else. An export is exactly where such a subset gets bypassed — by
serialising the underlying row dict, which carries fields the table never shows.
This module never sees a row dict except through a column's own accessor.

VALUES ARE MACHINE FORMS, not the screen's. `4,00 km` pastes into Excel as text
and cannot be summed; the column header carries the unit and the cell carries a
number. Timestamps go in as real datetimes. Owner decision, taken with the
consequence stated: the file will not look identical to the page.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Iterable

#: The page caps a request at 500 rows (`queries.MAX_PAGE_SIZE`). An export of
#: the whole filtered view pages through that ceiling rather than bypassing it —
#: the cap is a safety limit on one query, not a limit on what a person selected.
PAGE_SIZE = 500

#: A guard on the total. Without one, a filter that matches everything turns a
#: click into an unbounded read. Reported when it truncates rather than silently
#: producing a short file, because a silently incomplete spreadsheet is the worst
#: outcome for something someone is about to do arithmetic on.
MAX_EXPORT_ROWS = 50_000

CSV_DELIMITER = ";"
CSV_BOM = "﻿"


class ExportTruncated(Exception):
    """Raised when the filtered view exceeds `MAX_EXPORT_ROWS`."""

    def __init__(self, total: int) -> None:
        super().__init__(f"{total} rows exceeds the {MAX_EXPORT_ROWS} export limit")
        self.total = total


def collect_rows(fetch_page, *, max_rows: int = MAX_EXPORT_ROWS) -> list[dict]:
    """Every row of the current filtered view, by paging the existing service.

    `fetch_page(page, limit)` returns the service's `(rows, meta)` for one page —
    injected rather than imported so this module never builds a query of its own
    and cannot widen the filters the page applied.
    """

    rows: list[dict] = []
    page = 1
    while True:
        batch, meta = fetch_page(page, PAGE_SIZE)
        rows.extend(batch or [])
        total = int((meta or {}).get("total_count") or 0)
        if total > max_rows:
            raise ExportTruncated(total)
        if not batch or not (meta or {}).get("has_next"):
            return rows
        page += 1


def _cell(value: Any) -> Any:
    """A value a spreadsheet can use, or the empty cell."""

    if value is None or value == "":
        return None
    return value


def text_cell(value: Any) -> str:
    """A machine value as text, in the form a Polish Excel reads as a value.

    Shared by the CSV writer and by the table's `Ctrl`+`C`, deliberately: copying
    a rectangle out of the table and downloading the same rows must not produce
    two different numbers. One function is the only way that stays true, so this
    is not a helper -- it is the definition.
    """

    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M:%S")
    if isinstance(value, float):
        return f"{value:.2f}".replace(".", ",")
    return str(value)


def to_xlsx(columns: Iterable, rows: list[dict], *, sheet_title: str = "Przejazdy") -> bytes:
    """Typed cells, so dates sort as dates and distances add up.

    `openpyxl` is already a platform dependency; nothing new is introduced.
    """

    from openpyxl import Workbook
    from openpyxl.styles import Font

    cols = list(columns)
    book = Workbook()
    sheet = book.active
    # Excel refuses a sheet title over 31 characters or containing []:*?/\
    sheet.title = sheet_title[:31]

    sheet.append([col.export_label for col in cols])
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        sheet.append([_cell(col.value(row)) for col in cols])

    # Dates are datetimes in the cell; give the column a display format so Excel
    # shows what the screen showed rather than its own locale default.
    for index, col in enumerate(cols, start=1):
        letter = sheet.cell(row=1, column=index).column_letter
        sample = next((col.value(r) for r in rows if col.value(r) is not None), None)
        if isinstance(sample, datetime):
            for cell in sheet[letter][1:]:
                cell.number_format = "DD.MM.YYYY HH:MM:SS"
            sheet.column_dimensions[letter].width = 21
        else:
            # A column that prints fixed places on screen says so; the cell
            # still holds the number, only its display changes.
            number_format = getattr(col, "number_format", None)
            if number_format:
                for cell in sheet[letter][1:]:
                    cell.number_format = number_format
            sheet.column_dimensions[letter].width = max(12, min(30, len(col.export_label) + 3))
    sheet.freeze_panes = "A2"

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def to_csv(columns: Iterable, rows: list[dict]) -> bytes:
    """CSV a Polish Excel opens without an import wizard.

    Three choices, each of which produces mangled columns if made differently:

    * SEMICOLON delimiter — a comma cannot separate fields in a locale where it
      is the decimal mark, and Excel picks the delimiter from its locale, not
      from the file;
    * COMMA decimals — matching that locale, so a number is a number;
    * UTF-8 WITH BOM — without it Excel reads the file as the system code page
      and the Polish diacritics in `Źródło przypisania` arrive corrupted.
    """

    cols = list(columns)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=CSV_DELIMITER, quoting=csv.QUOTE_MINIMAL,
                        lineterminator="\r\n")
    writer.writerow([col.export_label for col in cols])
    for row in rows:
        writer.writerow([text_cell(col.value(row)) for col in cols])
    return (CSV_BOM + out.getvalue()).encode("utf-8")
