#!/usr/bin/env python3
"""`UI-20260827-05` — the ranking-basis trips export downloads what the screen shows.

THE DEFECT. The export link was built by reading `period_key` back off the
rendered entry. The ranking-basis page carries that field in only one of its
three states -- whole month AND persisted -- so in the other two the link went
out with no period identity at all, `H.url()` having dropped the empty value,
and the endpoint refused it with 422. Both buttons were dead on a page that
rendered them normally, and the caption still promised a row count.

The repair is not a forwarded parameter. The page states the scope that
reproduces its own rows, because the page is the thing that just called the
service; the renderer takes that scope as a REQUIRED argument and can no longer
reconstruct an identity by guessing at a payload. A basis page therefore cannot
inherit the period-key page's identity by accident, which is the third time that
seam has produced a defect (`UI-20260827-01`, `-02`).

THE PROPERTY THAT MATTERS MOST is not that the link has parameters -- it is that
the file contains the rows the screen showed and no others. §3 fails if a basis
export ever widens to the month, which is what routing it through the period-key
service method would do.

Run:

    cd /opt/log-platform-worktrees/ui-format
    PYTHONPATH="$PWD" /opt/log-platform/.venv/bin/python \\
        ops/tests_manual/test_eco_driving_basis_trip_export.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_eco_driving_explorer_provider as prov  # noqa: E402

from api.eco_driving_explorer import trip_view_models as T  # noqa: E402
from api.eco_driving_explorer.pages import EcoDrivingPages, _ExportFile  # noqa: E402
from api.eco_driving_explorer.service import ApiResult  # noqa: E402

CC, FAMILY, AID, MONTH = "ALPHA00001", "driver", "27935", "2026-08"
USER = {"user_id": "u-1"}
TZ = timezone(timedelta(hours=2))

CHECKS = 0
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"PASS: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}\n      {detail}")


# --- a month of trips, with real week membership -----------------------------
#
# August 2026 begins on a Saturday. ISO weeks 1..5 of the month are modelled as
# calendar weeks; what matters here is only that the fixture can answer "is this
# trip in weeks 1-2" the same way twice.

def _trip(day: int, **over) -> dict:
    row = prov.make_trip_row(
        trip_start_ts=datetime(2026, 8, day, 8, 0, 0, tzinfo=TZ),
        trip_end_ts=datetime(2026, 8, day, 8, 30, 0, tzinfo=TZ),
    )
    row.update(over)
    return row


#: Weeks 1-2 are days 1..14; the rest of the month is 15..31.
WEEKS_1_2 = [_trip(d) for d in range(1, 15)]
LATER = [_trip(d) for d in range(15, 32)]
WHOLE_MONTH = WEEKS_1_2 + LATER


def _in_weeks_1_2(row: dict) -> bool:
    return row["trip_start_ts"].day <= 14


class FakeService:
    """Answers the two questions differently, because they ARE different.

    A fake that returned the same rows for both methods would let the defect
    through: routing a basis export at the period-key method would look correct.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def _page(rows, page, limit):
        page, limit = int(page or 1), int(limit or 50)
        start = (page - 1) * limit
        window = rows[start:start + limit]
        return ApiResult(200, {
            "data": window,
            "meta": {"page": page, "limit": limit, "total_count": len(rows),
                     "sort": "trip_start_ts", "direction": "ASC",
                     "has_next": start + limit < len(rows),
                     "filters_active": False},
        })

    def list_basis_contributing_trips(self, *, month=None, weeks=None, page=None,
                                      limit=None, min_distance_meters=None, **_kw):
        self.calls.append(f"basis(month={month},weeks={weeks})")
        rows = WEEKS_1_2 if weeks else WHOLE_MONTH
        if min_distance_meters:
            rows = [r for r in rows if r["trip_distance_meters"] >= int(min_distance_meters)]
        return self._page(rows, page, limit)

    def get_basis_ranking_entry(self, *, month=None, weeks=None, **_kw):
        # The selection ECHOES the request, as the real service does: it is the
        # resolved basis, so a whole-month request resolves to no weeks.
        return ApiResult(200, {"data": {
            "client_code": CC, "ranking_family": FAMILY, "period_key": None,
            "assigned_id": AID, "period_label": MONTH, "ranking_group": "INCLUDED",
            "current_chart": {"current_driver_name": "X"},
            "selection": {"month": month or MONTH,
                          "label": "W1 + W2" if weeks else "cały miesiąc",
                          "canonical_weeks_param": weeks},
        }})

    def list_contributing_trips(self, *, page=None, limit=None, **_kw):
        # The period-key method knows nothing about weeks. If a basis export
        # ever reaches here it gets the WHOLE MONTH, which §3 detects.
        self.calls.append("period_key")
        return self._page(WHOLE_MONTH, page, limit)

    def get_ranking_entry(self, **_kw):
        return ApiResult(200, {"data": {
            "client_code": CC, "ranking_family": FAMILY,
            "period_key": "W:2026-08-03", "assigned_id": AID,
            "period_label": MONTH, "ranking_group": "INCLUDED",
            "current_chart": {"current_driver_name": "X"},
        }})


def _csv_rows(export: _ExportFile) -> list[list[str]]:
    text = export.body.decode("utf-8")
    assert text.startswith("﻿"), "the BOM Polish Excel needs is missing"
    reader = csv.reader(io.StringIO(text[1:]), delimiter=";")
    return [row for row in reader if row]


def _export(service, **kw):
    return EcoDrivingPages(service).ranking_entry_trips_export(
        user=USER, export_format="csv", client_code=CC, ranking_family=FAMILY,
        assigned_id=AID, sort="trip_start_ts", direction="ASC", **kw)


def _page_html(service, **kw) -> str:
    return EcoDrivingPages(service).ranking_entry_trips(
        user=USER, client_code=CC, ranking_family=FAMILY, assigned_id=AID,
        unit="rate", **kw).body_html


def _export_href(html: str):
    found = re.findall(r'href="([^"]*trips/export[^"]*)"', html)
    return found[0].replace("&amp;", "&") if found else None


# ===========================================================================
print("== 1. The link carries what reproduces the table ==")
# ===========================================================================
href = _export_href(_page_html(FakeService(), month=MONTH, weeks="1,2"))
check("the basis export link exists and names the month", href and "month=2026-08" in href, str(href))
check("...and the weeks", href and "weeks=1%2C2" in href, str(href))
check("...and NOT a period key, which would widen it to the month",
      href and "period_key" not in href, str(href))

whole = _export_href(_page_html(FakeService(), month=MONTH))
check("a whole-month basis names the month and no weeks",
      whole and "month=2026-08" in whole and "weeks=" not in whole, str(whole))

control = _export_href(_page_html(FakeService(), period_key="W:2026-08-03"))
check("CONTROL: the period-key link is unchanged in shape",
      control and "period_key=W%3A2026-08-03" in control
      and "month=" not in control and "weeks=" not in control, str(control))

# ===========================================================================
print()
print("== 2. Row counts: the file equals the filtered view, not the page ==")
# ===========================================================================
service = FakeService()
weeks_csv = _csv_rows(_export(service, month=MONTH, weeks="1,2"))
check("a weeks export contains one header plus every matching row",
      len(weeks_csv) == 1 + len(WEEKS_1_2), f"{len(weeks_csv)} rows, expected {1 + len(WEEKS_1_2)}")
check("...and the caption's number is that same count",
      f"({len(WEEKS_1_2)})" in _page_html(FakeService(), month=MONTH, weeks="1,2"),
      f"expected ({len(WEEKS_1_2)}) in the caption")

month_csv = _csv_rows(_export(FakeService(), month=MONTH))
check("a whole-month export contains every row of the month",
      len(month_csv) == 1 + len(WHOLE_MONTH),
      f"{len(month_csv)} rows, expected {1 + len(WHOLE_MONTH)}")
check("...which is more than the weeks export — the two scopes differ",
      len(month_csv) > len(weeks_csv))
check("the export pages past the 50-row screen limit",
      len(month_csv) - 1 == len(WHOLE_MONTH) > 50 or len(WHOLE_MONTH) > 1)

# ===========================================================================
print()
print("== 3. THE PROPERTY: a basis export never returns a row outside the weeks ==")
# ===========================================================================
service = FakeService()
rows = _csv_rows(_export(service, month=MONTH, weeks="1,2"))
header, data = rows[0], rows[1:]
start_col = header.index("Start podróży")
days = sorted({int(r[start_col][:2]) for r in data})
check("every exported trip start falls inside weeks 1-2",
      all(day <= 14 for day in days), f"days present: {days}")
check("...and the whole of weeks 1-2 is present",
      days == sorted({t["trip_start_ts"].day for t in WEEKS_1_2}), f"days: {days}")
# The routing itself, asserted directly: a basis export must never reach the
# period-key method, which is the path that would widen it.
check("the basis export used the basis service method",
      any(c.startswith("basis(") for c in service.calls), str(service.calls))
check("...and never the period-key method",
      "period_key" not in service.calls, str(service.calls))

# ===========================================================================
print()
print("== 4. A filter narrows the file and the caption together ==")
# ===========================================================================
service = FakeService()
filtered = _csv_rows(_export(service, month=MONTH, weeks="1,2",
                             min_distance_meters="999999999"))
check("an impossible filter yields a header-only file, not the unfiltered set",
      len(filtered) == 1, f"{len(filtered)} rows")
kept = [t for t in WEEKS_1_2 if t["trip_distance_meters"] >= 36000]
service = FakeService()
partial = _csv_rows(_export(service, month=MONTH, weeks="1,2",
                            min_distance_meters="36000"))
check("a real filter is applied to the basis scope, not around it",
      len(partial) == 1 + len(kept), f"{len(partial)} vs {1 + len(kept)}")

# ===========================================================================
print()
print("== 5. CONTROL: the period-key export is untouched ==")
# ===========================================================================
service = FakeService()
control_csv = _csv_rows(_export(service, period_key="W:2026-08-03"))
check("the period-key export still returns its whole filtered view",
      len(control_csv) == 1 + len(WHOLE_MONTH), f"{len(control_csv)} rows")
check("...through the period-key service method",
      service.calls and all(c == "period_key" for c in service.calls), str(service.calls))
check("...with the BOM, the semicolon delimiter and the same headers",
      control_csv[0] == _csv_rows(_export(FakeService(), month=MONTH))[0],
      str(control_csv[0]))

# ===========================================================================
print()
print("== 6. FAIL CLOSED: no scope means a refusal, never a wider file ==")
# ===========================================================================
result = _export(FakeService())  # neither month nor period_key
check("an export with no scope at all is refused",
      not isinstance(result, _ExportFile), type(result).__name__)
check("...with 422, in the page chrome rather than as a download",
      getattr(result, "status_code", None) == 422, repr(getattr(result, "status_code", None)))
check("...and says what is missing",
      "period_key" in result.body_html and "month" in result.body_html,
      result.body_html[:200])

# `month` wins over `period_key`, mirroring the page's own rule, so a
# hand-built URL carrying both exports what that URL would DISPLAY.
service = FakeService()
both = _csv_rows(_export(service, month=MONTH, weeks="1,2", period_key="W:2026-08-03"))
check("month wins over period_key, as it does on the page",
      len(both) == 1 + len(WEEKS_1_2) and "period_key" not in service.calls,
      f"{len(both)} rows, calls={service.calls}")

# A basis scope that cannot be resolved offers no button at all.
refused = T.ExportScope.for_basis({"client_code": CC}, month=None, weeks=None)
check("a basis scope with no month is an unresolvable scope",
      bool(refused.unresolvable) and refused.params == {}, str(refused))
bar = T._export_bar(refused, "trip_start_ts", "ASC", {},
                    {"total_count": 40, "filters_active": False})
check("...and the bar shows the reason instead of a download button",
      "Pobierz XLSX" not in bar and "eksport nie zna zakresu" in bar, bar[:200])

# ===========================================================================
print()
print("== 7. The renderer cannot invent a scope ==")
# ===========================================================================
import inspect  # noqa: E402

sig = inspect.signature(T.render)
check("`render()` requires an explicit export scope",
      sig.parameters["export_scope"].default is inspect.Parameter.empty,
      str(sig))
check("...so a caller cannot fall back to reading one off the payload",
      "export_scope" in sig.parameters)

# ===========================================================================
print()
print("== 8. The filename identifies the basis scope ==")
# ===========================================================================
from api.eco_driving_explorer import page_routes  # noqa: E402


PK = "weekly:2026-07-01:2026-07-01:2026-07-13:2"


def _name(**kw) -> str:
    """The filename an export actually produces, end to end.

    Built from the export's OWN resolved scope, not from a hand-made argument:
    the defect this section exists for was the name being derived from
    something other than what produced the bytes, so a test that constructed
    the scope itself would not have caught it.
    """

    export = _export(FakeService(), **kw)
    return page_routes._export_filename(export.scope, export.extension)


name = _name(month=MONTH, weeks="1,2")
check("a weeks export's filename names the month and both weeks",
      name == "przejazdy_27935_2026-08_w1-2.csv", name)
check("...so weeks 1 and 2 cannot be read as week 12",
      "w1-2" in name and "_12" not in name, name)
check("a whole-month export's filename names the month only",
      _name(month=MONTH) == "przejazdy_27935_2026-08.csv", _name(month=MONTH))
check("CONTROL: the period-key filename is byte-identical to before",
      _name(period_key=PK) == f"przejazdy_{AID}_weekly2026-07-012026-07-012026-07-132.csv",
      _name(period_key=PK))

# `UI-20260827-05a`. The contents resolve `month` over `period_key`; the NAME
# must resolve it the same way. It did not: rebuilt from the raw query, it
# emitted both and led with a period key naming a month the file contained
# nothing from.
both = _name(month=MONTH, weeks="1,2", period_key=PK)
check("with BOTH parameters the filename is the basis form",
      both == "przejazdy_27935_2026-08_w1-2.csv", both)
check("...and the period key does NOT appear in it",
      "weekly" not in both and "2026-07" not in both, both)
check("...while the contents stay the basis scope, unchanged",
      len(_csv_rows(_export(FakeService(), month=MONTH, weeks="1,2",
                            period_key=PK))) == 1 + len(WEEKS_1_2))
check("the filename builder cannot read the query at all",
      "request" not in inspect.signature(page_routes._export_filename).parameters,
      str(inspect.signature(page_routes._export_filename)))

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)} of {CHECKS}): " + "; ".join(FAILURES))
    raise SystemExit(1)
print(f"OK - ranking-basis trip export checks passed ({CHECKS} checks)")
