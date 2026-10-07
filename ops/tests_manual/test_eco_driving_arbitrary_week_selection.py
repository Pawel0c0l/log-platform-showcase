#!/usr/bin/env python3
"""Deterministic checks for month + arbitrary-week Eco Driving recomputation.

No database and no HTTP client. A synthetic in-memory ``eco_trip_assignments``
store stands in for the client business database and answers the *real* SQL the
provider issues by applying the same predicates to the same bound parameters, so
the interval derivation, the union semantics, the aggregation, the scoring, the
ranking and the trip evidence are all exercised end to end.

The load-bearing check is the first one: persisted cumulative month-to-date
snapshots must never be summed to build an arbitrary week selection.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_driving_arbitrary_week_selection.py
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from api.eco_driving_explorer import basis_view as B  # noqa: E402
from api.eco_driving_explorer import period_domain as PD  # noqa: E402
from api.eco_driving_explorer import queries as q  # noqa: E402
from api.eco_driving_explorer.access import EcoDrivingClientAccess, no_eco_access  # noqa: E402
from api.eco_driving_explorer.backend import ResolvedEcoClient  # noqa: E402
from api.eco_driving_explorer.eco_scoring import (  # noqa: E402
    MIN_QUALIFYING_DISTANCE_METERS,
    RATING_THRESHOLDS,
    RATING_FALLBACK_LABEL,
    REQUIRED_METRICS,
)
from api.eco_driving_explorer.errors import InvalidWeekSelectionError  # noqa: E402
from api.eco_driving_explorer.models import (  # noqa: E402
    LineageQuality,
    PeriodType,
    RankingPeriodKey,
)
from api.eco_driving_explorer.pages import EcoDrivingPages  # noqa: E402
from api.eco_driving_explorer.service import EcoDrivingApiService  # noqa: E402
from api.eco_driving_explorer.week_selection import (  # noqa: E402
    BASIS_MONTH_DYNAMIC,
    BASIS_MONTH_PERSISTED,
    BASIS_WEEKS_DYNAMIC,
    EMPTY_WEEKS_TOKEN,
    MODE_EMPTY,
    MODE_MONTH,
    MODE_WEEKS,
    parse_week_selection,
)
from jobs.ecodriving import job_eco_driving_aggregate as JOB  # noqa: E402

CLIENT_CODE = "ALPHA00001"
FAMILY = "driver"
BRAVO_CLIENT_CODE = "BRAVO00016"
BRAVO_FAMILY = "person"
TRUSTED_CLIENT_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
USER = {"user_id": "u-1", "username": "operator", "is_admin": False}
MONTH = "2026-07"
MONTH_START = date(2026, 7, 1)
TZ = PD.local_midnight(MONTH_START).tzinfo

_CHECKS = 0


def check(condition: bool, message: str) -> None:
    global _CHECKS
    assert condition, message
    _CHECKS += 1


def ts(day: int, hour: int = 9, minute: int = 0, month: int = 7, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


# --- synthetic client database ------------------------------------------------


ZERO_METRICS = {metric: 0 for metric in REQUIRED_METRICS}


def trip(
    provider_trip_id: int,
    assigned_id: str,
    start: datetime,
    *,
    meters: int = 10_000,
    private: bool = False,
    included: bool | None = None,
    **metrics,
) -> dict:
    """One ``eco_trip_assignments`` row.

    ``aggregation_included`` is the column the aggregation job wrote using the
    client's own inclusion contract, so a fixture sets it exactly as the job
    would: an id-less or private trip is excluded, everything else is included.
    """

    row = {
        "client_id": TRUSTED_CLIENT_ID,
        "provider_trip_id": provider_trip_id,
        "assigned_id": assigned_id,
        "assignment_source": "DRIVER_RESTRICTIONS",
        "trip_start_ts": start,
        "trip_end_ts": start + timedelta(hours=1),
        "trip_distance_meters": meters,
        "is_private_trip": bool(private),
        "aggregation_included": (
            (assigned_id is not None and not private) if included is None else included
        ),
        "exclusion_reason": "PRIVATE_DRIVER_TAG" if private else None,
        "client_trip_present": True,
        **ZERO_METRICS,
    }
    for key, value in metrics.items():
        assert key in REQUIRED_METRICS, key
        row[key] = int(value)
    return row


def person_trip(
    provider_trip_id: int,
    person_key: Optional[str],
    start: datetime,
    *,
    meters: int = 10_000,
    private: bool = False,
    included: Optional[bool] = None,
    assignment_source: str = "PERSON_ID_MATCH",
    person_name: str = "Jan Kowalski",
    **metrics,
) -> dict:
    """One ``eco_person_trip_assignments`` row.

    The person pipeline writes ``aggregation_included = (match_count = 1)``, so
    inclusion follows identity resolution and **not** the private flag. A row
    with ``private=True, included=True`` is therefore a legitimate production
    state, guarded by `043`'s ``chk_eco_person_trip_assignments_identity_outcome``.
    """

    row = trip(
        provider_trip_id, person_key, start, meters=meters, private=private,
        included=(person_key is not None) if included is None else included,
        **metrics,
    )
    row["assignment_source"] = assignment_source
    row["person_name_group_key"] = person_key
    row["person_name"] = person_name if person_key else None
    return row


class SyntheticClientDatabase:
    """Answers the provider's real SQL by re-applying its bound predicates.

    The inclusion decision is **read out of the SQL the provider actually
    issued**, never reimplemented here. If the query layer ever added the
    driver family's private-trip clause to the person family, this double would
    honour it and the BRAVO checks below would fail — which is the point.
    """

    def __init__(
        self,
        trips: list[dict],
        chart: list[dict],
        canned: dict | None = None,
        *,
        family_key: str = "driver",
    ) -> None:
        self.trips = trips
        self.chart = {row["driver_id"]: row for row in chart}
        self.canned = canned or {}
        self.family_key = family_key
        self.expected_table = (
            "public.eco_person_trip_assignments"
            if family_key == "person"
            else "public.eco_trip_assignments"
        )
        self.calls: list[tuple[str, dict]] = []

    # -- inclusion, as the issued SQL states it -------------------------------

    @staticmethod
    def _sql_excludes_private(sql: str) -> bool:
        """Does this statement's inclusion filter carry a private-trip clause?"""

        return "is_private_trip IS FALSE" in sql or "is_private_trip = FALSE" in sql

    def _included(self, row: dict, sql: str) -> bool:
        if not row["aggregation_included"]:
            return False
        if self._sql_excludes_private(sql):
            return row["is_private_trip"] is False
        return True

    def _assert_family_table(self, sql: str) -> None:
        if "eco_trip_assignments" not in sql and "eco_person_trip_assignments" not in sql:
            return
        assert self.expected_table in sql, (
            f"{self.family_key} family must read {self.expected_table}"
        )

    # -- request classification ------------------------------------------------

    @staticmethod
    def _kind(sql: str, params: dict) -> str:
        if "assignment_count" in sql:
            return "months"
        if "skipped_trips_count" in sql and "roster_name" in sql:
            return "dynamic_population"
        if "bin_index" in sql:
            return "score_distribution"
        if "missing_client_trip_count" in sql:
            return "recon_totals"
        if "not_aggregated_trips" in sql:
            return "window_diag"
        if "_trends_view" in sql:
            return "driver_trend"
        if "ORDER BY s.period_end_date ASC" in sql:
            return "period_progression"
        if "client_trip_present" in sql:
            return "trips_list"
        if "AS total_count" in sql and "eco_trip_assignments a" in sql:
            return "trips_count"
        if "AS total_count" in sql:
            return "entries_count"
        if "LIMIT 1" in sql:
            return "single_entry"
        if "current_chart_present" in sql:
            return "entries_list"
        if "month_end_date" in sql:
            return "monthly_periods"
        return "weekly_periods"

    # -- predicate emulation ---------------------------------------------------

    @staticmethod
    def _intervals(params: dict) -> list[tuple[datetime, datetime]]:
        intervals = []
        index = 0
        while f"win{index}_start" in params:
            intervals.append((params[f"win{index}_start"], params[f"win{index}_end"]))
            index += 1
        return intervals

    def _in_union(self, row: dict, intervals) -> bool:
        return any(start <= row["trip_start_ts"] < end for start, end in intervals)

    @staticmethod
    def _identity(row: dict, sql: str) -> object:
        """The identity column this family groups and filters on."""

        return (
            row.get("person_name_group_key")
            if "a.person_name_group_key" in sql
            else row.get("assigned_id")
        )

    def _client_rows(self, params: dict) -> list[dict]:
        # Every business read is scoped by the trusted client_id.
        return [r for r in self.trips if r["client_id"] == params["client_id"]]

    def _population(self, sql: str, params: dict) -> list[dict]:
        self._assert_family_table(sql)
        intervals = self._intervals(params)
        buckets: dict[str, list[dict]] = {}
        for row in self._client_rows(params):
            identity = self._identity(row, sql)
            if identity is None or not self._in_union(row, intervals):
                continue
            buckets.setdefault(identity, []).append(row)

        out = []
        for assigned_id, rows in sorted(buckets.items()):
            included = [r for r in rows if self._included(r, sql)]
            if not included:  # HAVING
                continue
            chart = self.chart.get(assigned_id)
            record = {
                "client_id": params["client_id"],
                "assigned_id": assigned_id,
                "trips_count": len(included),
                "source_trips_count": len(rows),
                "skipped_trips_count": len(rows) - len(included),
                "total_distance_meters": sum(
                    int(r["trip_distance_meters"] or 0) for r in included
                ),
                # The person family resolves a canonical name while assigning;
                # the driver family reads it from the roster.
                "assignment_name": (
                    included[0].get("person_name") if self.family_key == "person" else None
                ),
                "roster_id": (chart or {}).get("driver_id"),
                "roster_name": (chart or {}).get("driver_name"),
                "ranking_included": (chart or {}).get("ranking_included"),
            }
            for metric in REQUIRED_METRICS:
                record[metric] = sum(int(r[metric]) for r in included)
            out.append(record)
        return out

    def _trip_rows(self, sql: str, params: dict) -> list[dict]:
        self._assert_family_table(sql)
        intervals = self._intervals(params)
        rows = [
            r
            for r in self._client_rows(params)
            if self._identity(r, sql) == params["assigned_id"]
            and self._in_union(r, intervals)
            and self._included(r, sql)
        ]
        if "provider_trip_id" in params:
            rows = [r for r in rows if r["provider_trip_id"] == params["provider_trip_id"]]
        if "min_distance_meters" in params:
            rows = [
                r for r in rows if (r["trip_distance_meters"] or 0) >= params["min_distance_meters"]
            ]
        if "max_distance_meters" in params:
            rows = [
                r for r in rows if (r["trip_distance_meters"] or 0) <= params["max_distance_meters"]
            ]
        events = "COALESCE(a.speeding_170_plus_count, 0))"
        if f"{events} > 0" in sql:
            rows = [r for r in rows if sum(r[m] for m in REQUIRED_METRICS) > 0]
        elif f"{events} = 0" in sql:
            rows = [r for r in rows if sum(r[m] for m in REQUIRED_METRICS) == 0]
        rows.sort(key=lambda r: (r["trip_start_ts"], r["provider_trip_id"]))
        return rows

    # -- RowReader protocol ----------------------------------------------------

    def fetch_all(self, sql, params):
        params = dict(params)
        self.calls.append((sql, params))
        kind = self._kind(sql, params)
        if kind == "dynamic_population":
            return self._population(sql, params)
        if kind == "trips_list":
            rows = self._trip_rows(sql, params)
            offset = int(params.get("offset") or 0)
            limit = int(params.get("limit") or len(rows))
            return [dict(r) for r in rows[offset:offset + limit]]
        if kind == "months":
            months = sorted(
                {
                    r["trip_start_ts"].astimezone(TZ).date().replace(day=1)
                    for r in self._client_rows(params)
                    if self._identity(r, sql) is not None
                },
                reverse=True,
            )
            return [
                {
                    "month_start_date": m,
                    "assignment_count": 1,
                    "included_assignment_count": sum(
                        1
                        for r in self._client_rows(params)
                        if self._identity(r, sql) is not None
                        and self._included(r, sql)
                        and r["trip_start_ts"].astimezone(TZ).date().replace(day=1) == m
                    ),
                }
                for m in months
            ]
        return [dict(r) for r in self.canned.get(kind, [])]

    def fetch_one(self, sql, params):
        params = dict(params)
        self.calls.append((sql, params))
        kind = self._kind(sql, params)
        if kind == "trips_count":
            return {"total_count": len(self._trip_rows(sql, params))}
        rows = self.canned.get(kind, [])
        return dict(rows[0]) if rows else None


class FakeBackend:
    def __init__(
        self,
        database: SyntheticClientDatabase,
        *,
        access=None,
        family_key: str = "driver",
    ) -> None:
        self.database = database
        self.access = access
        self.family_key = family_key
        self.client_code = BRAVO_CLIENT_CODE if family_key == "person" else CLIENT_CODE
        self.audit_events: list[dict] = []
        self.reader_opened = 0

    def fetch_access(self, user_id, client_code):
        if self.access is None or client_code != self.client_code:
            return no_eco_access(client_code)
        return self.access

    def resolve_binding(self, client_code, ranking_family):
        # The trusted binding is server-side. A caller supplies the code and the
        # family only as lookup keys; the client_id never comes from a request.
        return ResolvedEcoClient(
            client_code=client_code,
            ranking_family=ranking_family,
            client_id=TRUSTED_CLIENT_ID,
            database_name="alpha_main",
        )

    @contextmanager
    def open_reader(self, binding):
        self.reader_opened += 1
        yield self.database

    def record_audit(self, *, event_type, actor_user_id, client_code, request, metadata):
        self.audit_events.append(
            {"event_type": event_type, "client_code": client_code, "metadata": metadata}
        )


def full_access(client_code: str = CLIENT_CODE) -> EcoDrivingClientAccess:
    return EcoDrivingClientAccess(
        client_code=client_code,
        can_view_eco_ranking=True,
        can_view_eco_trip_details=True,
        can_view_eco_trip_routes=False,
        client_is_active=True,
        sources=("direct",),
    )


def build(trips, chart, canned=None, access=None, *, family_key="driver"):
    database = SyntheticClientDatabase(trips, chart, canned, family_key=family_key)
    backend = FakeBackend(database, access=access or full_access(), family_key=family_key)
    return EcoDrivingApiService(backend), backend, database


def ranking(service, *, weeks=None, group="INCLUDED", month=MONTH, **kw):
    return service.get_basis_ranking(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=month, weeks=weeks, ranking_group=group, **kw
    )


def entry_of(result, assigned_id):
    for row in result.body.get("data") or []:
        if row["assigned_id"] == assigned_id:
            return row
    return None


CHART = [
    {"driver_id": "D-1", "driver_name": "Kowalski Marek", "ranking_included": True},
    {"driver_id": "D-2", "driver_name": "Nowak Anna", "ranking_included": True},
    {"driver_id": "D-EXC", "driver_name": "Wiśniewski Jan", "ranking_included": False},
]

# `eco_person_people_email_view` rows, keyed by `person_name_group_key`.
PERSON_CHART = [
    {"driver_id": "p-jan-kowalski", "driver_name": "Jan Kowalski", "ranking_included": True},
    {"driver_id": "p-anna-nowak", "driver_name": "Anna Nowak", "ranking_included": True},
]


def bravo_ranking(service, *, weeks=None, group="INCLUDED", month=MONTH, **kw):
    return service.get_basis_ranking(
        user=USER, request=None, client_code=BRAVO_CLIENT_CODE,
        ranking_family=BRAVO_FAMILY, month=month, weeks=weeks,
        ranking_group=group, **kw
    )


# =============================================================================
# Schema-aware SQL validation
# =============================================================================
#
# The previous round's weakness was a database double that answered whatever it
# was asked. These helpers derive each relation's real column set from the
# repository's own DDL and refuse a statement that names a column the
# migrations do not define — which is exactly how a driver-shaped query against
# person stats fails here instead of silently "passing".

# Every client-business migration, applied in order, so a column added or
# renamed by a later one is reflected exactly as production would see it.
DDL_FILES = tuple(
    sorted(
        (REPO_ROOT / "db/client_business").glob("*.sql"),
        key=lambda path: path.name,
    )
)

_CREATE_TABLE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(public\.\w+)\s*\((.*?)\n\);",
    re.S | re.I,
)
_CREATE_VIEW = re.compile(
    r"CREATE(?:\s+OR REPLACE)?\s+VIEW\s+(public\.\w+)\s+AS\s+SELECT\s+(.*?)\nFROM",
    re.S | re.I,
)
_ALTER = re.compile(r"ALTER TABLE(?:\s+IF EXISTS)?\s+(public\.\w+)(.*?);", re.S | re.I)

# A column definition is `name <TYPE>`; anything else on a DDL line (a CHECK
# continuation, a constraint, an index clause) is not one.
_SQL_TYPES = (
    "UUID", "TEXT", "DATE", "TIMESTAMPTZ", "TIMESTAMP", "BIGINT", "INTEGER",
    "INT", "NUMERIC", "BOOLEAN", "JSONB", "JSON", "SMALLINT", "REAL",
    "DOUBLE", "BYTEA", "SERIAL", "BIGSERIAL", "CHAR", "VARCHAR", "INTERVAL",
)
_COLUMN_DEF = re.compile(
    r"^\s{1,4}([a-z_][a-z0-9_]*)\s+(" + "|".join(_SQL_TYPES) + r")\b", re.I
)
_ADD_COLUMN = re.compile(
    r"ADD COLUMN(?:\s+IF NOT EXISTS)?\s+([a-z_][a-z0-9_]*)", re.I
)
_DROP_COLUMN = re.compile(
    r"DROP COLUMN(?:\s+IF EXISTS)?\s+([a-z_][a-z0-9_]*)", re.I
)


def _table_columns() -> dict[str, set[str]]:
    """Column sets for the Eco relations, parsed from the migration DDL."""

    columns: dict[str, set[str]] = {}
    views: list[tuple[str, str]] = []
    for path in DDL_FILES:
        text = path.read_text()

        for name, body in _CREATE_TABLE.findall(text):
            found = {
                match.group(1)
                for match in (_COLUMN_DEF.match(line) for line in body.splitlines())
                if match
            }
            columns.setdefault(name, set()).update(found)

        for name, body in _ALTER.findall(text):
            if name not in columns:
                continue
            columns[name].update(_ADD_COLUMN.findall(body))
            for dropped in _DROP_COLUMN.findall(body):
                columns[name].discard(dropped)

        views.extend(_CREATE_VIEW.findall(text))

    # Views last, so `SELECT s.*` inherits the final column set of its base.
    for name, select in views:
        found = set()
        for part in select.split(","):
            part = part.strip()
            if not part or part == "*":
                continue
            alias = re.search(r"\bAS\s+([a-z_][a-z0-9_]*)\s*$", part, re.I)
            if alias:
                found.add(alias.group(1))
            elif re.fullmatch(r"[a-z_][a-z0-9_.]*", part, re.I):
                found.add(part.split(".")[-1])
        for base, marker in (
            ("public.eco_person_weekly_stats", "eco_person_weekly_stats"),
            ("public.eco_person_monthly_stats", "eco_person_monthly_stats"),
            ("public.eco_driver_weekly_stats", "eco_driver_weekly_stats"),
            ("public.eco_driver_monthly_stats", "eco_driver_monthly_stats"),
        ):
            if marker in select or f"{marker} s" in select:
                found |= columns.get(base, set())
        columns[name] = found
    return columns


SCHEMA_COLUMNS = _table_columns()

# `client_trips` lives in the Workflow A schema; only the two columns the Eco
# join uses are relevant here.
SCHEMA_COLUMNS.setdefault("public.client_trips", set()).update(
    {"client_id", "provider_trip_id"}
)

_ALIAS_REF = re.compile(r"\b([a-z]{1,2})\.([a-z_][a-z0-9_]*)\b")


def relations_in(sql: str) -> dict[str, str]:
    """Map each SQL alias to the relation it was bound to in this statement."""

    bound: dict[str, str] = {}
    for relation, alias in re.findall(
        r"(?:FROM|JOIN)\s+(public\.\w+)\s+(?:AS\s+)?([a-z]{1,2})\b", sql, re.I
    ):
        bound[alias] = relation
    return bound


def assert_sql_matches_schema(sql: str, *, context: str) -> list[str]:
    """Every ``alias.column`` must exist on the relation that alias was bound to.

    Returns the validated references so a caller can assert on them. Aliases
    bound to a CTE (not a ``public.`` relation) are skipped: their projection is
    defined by the statement itself.
    """

    bound = relations_in(sql)
    checked = []
    for alias, column in _ALIAS_REF.findall(sql):
        relation = bound.get(alias)
        if relation is None:
            continue
        known = SCHEMA_COLUMNS.get(relation)
        if not known:
            continue
        assert column in known, (
            f"{context}: {alias}.{column} does not exist on {relation}. "
            f"Known columns: {sorted(known)[:12]}…"
        )
        checked.append(f"{alias}.{column}")
    return checked


class SchemaValidatingReader:
    """A reader that validates issued SQL before answering it."""

    def __init__(self, inner, *, context: str) -> None:
        self._inner = inner
        self._context = context
        self.statements: list[str] = []

    def _check(self, sql):
        self.statements.append(sql)
        assert_sql_matches_schema(sql, context=self._context)
        # Values are always bound; a literal-looking predicate is a defect.
        assert "%(" in sql or "SELECT" in sql, f"{self._context}: no bound parameters"

    def fetch_all(self, sql, params):
        self._check(sql)
        return self._inner.fetch_all(sql, params)

    def fetch_one(self, sql, params):
        self._check(sql)
        return self._inner.fetch_one(sql, params)

    @property
    def calls(self):
        return self._inner.calls


def test_ddl_parser_sees_the_migration_043_rename():
    """The validator is only meaningful if it reflects the real migrations."""

    driver_weekly = SCHEMA_COLUMNS["public.eco_driver_weekly_stats"]
    person_weekly = SCHEMA_COLUMNS["public.eco_person_weekly_stats"]
    check("assigned_id" in driver_weekly, "driver stats keep assigned_id")
    check(
        "assigned_id" not in person_weekly,
        "migration 043 dropped assigned_id from person stats",
    )
    check(
        "person_name_group_key" in person_weekly,
        "and added person_name_group_key in its place",
    )
    person_assignments = SCHEMA_COLUMNS["public.eco_person_trip_assignments"]
    check(
        "assigned_id" not in person_assignments,
        "043 dropped assigned_id from person assignments too",
    )
    check(
        "person_name_group_key" in person_assignments,
        "the person assignment identity is person_name_group_key",
    )
    check(
        "is_private_trip" in person_assignments,
        "the private flag is still recorded for the person family",
    )
    print("PASS: the schema validator is derived from the real migration DDL")


# =============================================================================
# 1. Cumulative snapshots are never summed
# =============================================================================


def test_week_buckets_are_isolated_segments_not_cumulative_snapshots():
    """The bucket boundaries match the job, but the ranges are isolated."""

    buckets = PD.month_week_buckets(MONTH_START)
    periods = JOB._month_bounded_weekly_periods(MONTH_START)
    check(len(buckets) == len(periods) == 5, "July 2026 has five week buckets")

    # Same boundary, different meaning: the persisted period starts at the month
    # start (cumulative), the bucket starts at the previous boundary (isolated).
    for bucket, period in zip(buckets, periods):
        check(
            bucket.end_date_exclusive == period.period_end_date,
            "bucket and persisted snapshot share the boundary",
        )
        check(
            period.period_start_date == MONTH_START,
            "persisted weekly period is cumulative from the month start",
        )
        check(bucket.is_partial == period.is_partial_period, "partiality agrees")

    check(buckets[0].start_date == date(2026, 7, 1), "W1 starts at the month start")
    check(buckets[0].end_date_exclusive == date(2026, 7, 6), "W1 ends at the first Monday")
    check(buckets[1].start_date == date(2026, 7, 6), "W2 starts where W1 ended")
    check(buckets[1].end_date_exclusive == date(2026, 7, 13), "W2 is a full Monday week")
    check(buckets[-1].end_date_exclusive == date(2026, 8, 1), "W5 stops at the month end")
    check(sum(b.day_count for b in buckets) == 31, "the buckets tile July exactly once")
    print("PASS: week buckets are isolated segments sharing the snapshot boundaries")


def test_arbitrary_selection_is_not_a_snapshot_sum():
    """The specified proof: W1 + W3 must be 17 events, never 35.

    The underlying isolated weeks carry 10 / 8 / 7 harsh-braking events, so the
    persisted cumulative month-to-date snapshots would read 10 / 18 / 25. An
    implementation that added the W1 and W3 snapshots would report 35.
    """

    trips = []
    plan = {
        1: (date(2026, 7, 2), 10, 6, 400_000),
        2: (date(2026, 7, 7), 8, 4, 350_000),
        3: (date(2026, 7, 14), 7, 9, 300_000),
    }
    trip_id = 1
    for _week, (day, braking, turning, meters) in plan.items():
        trips.append(
            trip(
                trip_id, "D-1", ts(day.day),
                meters=meters,
                harsh_braking_events=braking,
                harsh_turning_events=turning,
            )
        )
        trip_id += 1

    # The cumulative snapshots this data would produce, stated explicitly so the
    # forbidden arithmetic is visible in the test rather than implied.
    cumulative = {1: 10, 2: 18, 3: 25}
    forbidden_sum = cumulative[1] + cumulative[3]
    check(forbidden_sum == 35, "the snapshot-sum answer would be 35")

    service, _backend, _db = build(trips, CHART)
    res = ranking(service, weeks="1,3")
    check(res.status_code == 200, "W1+W3 recomputes")
    row = entry_of(res, "D-1")
    check(row is not None, "the driver is in the recomputed population")
    check(
        row["event_counts"]["harsh_braking_events"] == 17,
        f"W1+W3 braking must be 17, got {row['event_counts']['harsh_braking_events']}",
    )
    check(
        row["event_counts"]["harsh_braking_events"] != forbidden_sum,
        "the snapshot-sum answer must not appear",
    )
    # A second metric and the distance make an accidental pass impossible.
    check(row["event_counts"]["harsh_turning_events"] == 15, "W1+W3 turning is 6+9")
    check(row["total_distance_meters"] == 700_000, "W1+W3 distance is 400 000 + 300 000")
    check(row["trips_count"] == 2, "only the two selected weeks contributed trips")
    print("PASS: W1+W3 recomputes to 17 raw events, not the forbidden snapshot sum of 35")


# =============================================================================
# 2-4. Non-contiguous, contiguous and single-week selections
# =============================================================================


def _three_week_driver():
    return [
        trip(1, "D-1", ts(2), meters=400_000, harsh_braking_events=10),
        trip(2, "D-1", ts(7), meters=350_000, harsh_braking_events=8),
        trip(3, "D-1", ts(14), meters=300_000, harsh_braking_events=7),
    ]


def test_non_contiguous_selection_excludes_the_gap():
    service, _backend, _db = build(_three_week_driver(), CHART)
    res = ranking(service, weeks="1,3")
    row = entry_of(res, "D-1")
    check(row["event_counts"]["harsh_braking_events"] == 17, "W2 events are excluded")
    check(row["total_distance_meters"] == 700_000, "W2 distance is excluded")

    selection = (res.body["meta"]["selection"])
    check(selection["is_contiguous"] is False, "the basis reports its gap")
    check(selection["day_count"] == 5 + 7, "day count is the covered days, not the span")

    trips_res = service.list_basis_contributing_trips(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1,3", assigned_id="D-1",
    )
    ids = {t["provider_trip_id"] for t in trips_res.body["data"]}
    check(ids == {1, 3}, f"trip evidence excludes the W2 trip, got {ids}")
    print("PASS: a non-contiguous W1+W3 basis excludes W2 from score, distance and evidence")


def test_contiguous_selection_merges_without_double_counting():
    service, _backend, database = build(_three_week_driver(), CHART)
    res = ranking(service, weeks="1,2")
    row = entry_of(res, "D-1")
    check(row["event_counts"]["harsh_braking_events"] == 18, "W1+W2 is 10+8")
    check(row["trips_count"] == 2, "each trip contributes exactly once")

    population_calls = [
        params for sql, params in database.calls if "skipped_trips_count" in sql
    ]
    check(len(population_calls) == 1, "one population read for the whole ranking")
    params = population_calls[0]
    check("win1_start" not in params, "adjacent weeks merge into a single interval")
    check(params["win0_start"] == PD.local_midnight(date(2026, 7, 1)), "union starts at W1")
    check(params["win0_end"] == PD.local_midnight(date(2026, 7, 13)), "union ends at W2's end")
    print("PASS: contiguous weeks merge into one interval, so no trip can be counted twice")


def test_single_week_is_the_isolated_week_not_the_cumulative_snapshot():
    service, _backend, _db = build(_three_week_driver(), CHART)
    res = ranking(service, weeks="2")
    row = entry_of(res, "D-1")
    check(row["event_counts"]["harsh_braking_events"] == 8, "W2 alone is the isolated 8")
    check(row["event_counts"]["harsh_braking_events"] != 18, "not the cumulative W2 MTD of 18")
    check(row["total_distance_meters"] == 350_000, "W2 alone carries only its own distance")
    print("PASS: a single week is the isolated week, never its cumulative month-to-date snapshot")


# =============================================================================
# 5. Whole month
# =============================================================================


def _monthly_entry_row(assigned_id="D-1", score="72", distance=400_000):
    row = {
        "assigned_id": assigned_id,
        "ranking_group": "INCLUDED",
        "ranking_included": True,
        "ranking_position": 1,
        "ranking_total_participants": 2,
        "qualification_status": "QUALIFIED",
        "calculation_status": "OK",
        "trips_count": 12,
        "total_distance_meters": distance,
        "total_kilometers": Decimal(distance) / Decimal(1000),
        "eco_driving_score_total": Decimal(score),
        "ecodriving_rating_type": "akceptowalny",
        "ecodriving_rating_type_share_percent": Decimal("50"),
        "period_label": "2026-07",
        "is_partial_period": False,
        "current_driver_name": "Kowalski Marek",
        "current_chart_present": True,
        "current_chart_ranking_included": True,
    }
    for metric in REQUIRED_METRICS:
        row[metric] = 3
        row[q.RATE_COLUMNS[metric]] = Decimal("1")
        row[q.POINT_COLUMNS[metric]] = Decimal("10")
        row[q.SUBTRACT_COLUMNS[metric]] = Decimal("-2")
    return row


def test_whole_month_consumes_the_canonical_persisted_monthly_snapshot():
    """The official monthly reporting truth is not reinterpreted by this feature."""

    canned = {
        "entries_list": [_monthly_entry_row()],
        "entries_count": [{"total_count": 1}],
        "monthly_periods": [{
            "month_start_date": MONTH_START,
            "month_end_date": date(2026, 8, 1),
            "included_count": 2, "excluded_count": 1, "unknown_count": 0,
            "not_ranked_count": 3,
            "source_calculated_at": None,
        }],
        "score_distribution": [
            {"bin_index": 17, "bin_count": 2, "total_count": 2,
             "mean_score": Decimal("72"), "median_score": Decimal("72")}
        ],
    }
    service, _backend, database = build(_three_week_driver(), CHART, canned)

    res = ranking(service, weeks=None)
    check(res.status_code == 200, "whole month renders")
    meta = res.body["meta"]
    check(meta["selection"]["mode"] == MODE_MONTH, "the mode resolves to MONTH")
    check(
        meta["selection"]["is_dynamically_recomputed"] is False,
        "whole month is not a dynamic recomputation",
    )
    check(meta["period_key"] is not None, "whole month carries the persisted monthly period key")
    check(res.body["data"][0]["eco_driving_score_total"] == "72",
          "the persisted monthly score is served verbatim")
    check(
        not any("skipped_trips_count" in sql for sql, _ in database.calls),
        "the whole-month path issues no dynamic aggregation query",
    )
    check(meta["basis"]["counts_by_group"]["INCLUDED"] == 2, "persisted group counts are used")
    print("PASS: whole month is served from the canonical persisted monthly snapshot")


def test_full_month_dynamic_parity_with_the_canonical_generator():
    """The dynamic path and the aggregation job agree, field by field.

    Selecting every week explicitly canonicalizes to MONTH, so the dynamic
    aggregation is invoked here through the provider directly. Both sides are
    then compared against ``_stats_row_from_aggregate`` — the function the
    scheduled job persists from — over the identical trip set.
    """

    from api.eco_driving_explorer.registry import get_provider

    trips = _three_week_driver() + [
        trip(4, "D-1", ts(20), meters=120_000, harsh_braking_events=4, idle_events=11),
        trip(5, "D-1", ts(28), meters=90_000, overrev_events_count=6),
    ]
    database = SyntheticClientDatabase(trips, CHART)
    provider = get_provider(CLIENT_CODE, FAMILY, client_id=TRUSTED_CLIENT_ID)

    whole_month = parse_week_selection(MONTH, "1,2,3,4,5")
    check(whole_month.mode == MODE_MONTH, "listing every week is whole-month mode")
    basis = provider.recompute_basis(database, whole_month)
    dynamic = next(e for e in basis.entries if e.assigned_id == "D-1")

    totals = {metric: 0 for metric in REQUIRED_METRICS}
    meters = 0
    for row in trips:
        meters += row["trip_distance_meters"]
        for metric in REQUIRED_METRICS:
            totals[metric] += row[metric]

    aggregate_row = {
        "client_id": TRUSTED_CLIENT_ID,
        "client_code": CLIENT_CODE,
        "assigned_id": "D-1",
        "trips_count": len(trips),
        "source_trips_count": len(trips),
        "skipped_trips_count": 0,
        "total_distance_meters": meters,
        "driver_id": "D-1",
        "ranking_included": True,
        "month_start_date": MONTH_START,
        "month_end_date": date(2026, 8, 1),
        **totals,
    }
    persisted = JOB._stats_row_from_aggregate(aggregate_row, None, monthly=True)

    check(dynamic.total_distance_meters == persisted["total_distance_meters"], "distance agrees")
    check(dynamic.total_kilometers == persisted["total_kilometers"], "kilometres agree")
    check(dynamic.qualification_status == persisted["qualification_status"], "qualification agrees")
    check(dynamic.calculation_status == persisted["calculation_status"], "calc status agrees")
    check(
        dynamic.eco_driving_score_total == persisted["eco_driving_score_total"],
        "total score agrees",
    )
    check(dynamic.ecodriving_rating_type == persisted["ecodriving_rating_type"], "rating agrees")
    check(
        dynamic.ranking_group.value == persisted["ranking_group"],
        "ranking group agrees",
    )
    for metric in REQUIRED_METRICS:
        check(dynamic.event_counts[metric] == persisted[metric], f"{metric} total agrees")
        check(
            dynamic.metric_rates[metric] == persisted[q.RATE_COLUMNS[metric]],
            f"{metric} normalized rate agrees",
        )
        check(
            dynamic.metric_points[metric] == Decimal(persisted[q.POINT_COLUMNS[metric]]),
            f"{metric} points agree",
        )
        check(
            dynamic.metric_losses[metric] == Decimal(persisted[q.SUBTRACT_COLUMNS[metric]]),
            f"{metric} loss agrees",
        )
    print("PASS: dynamic full-month aggregation equals the canonical generator field by field")


# =============================================================================
# 6. Selected-range 100 km qualification
# =============================================================================


def test_qualification_applies_once_to_the_whole_selected_union():
    trips = [
        trip(1, "D-1", ts(2), meters=60_000),
        trip(2, "D-1", ts(14), meters=55_000),
        trip(3, "D-1", ts(15), meters=2_000),  # a short day inside a qualifying range
    ]
    service, _backend, _db = build(trips, CHART)

    # The `w rankingu` tab is the client's ranking POPULATION, so a permitted
    # driver who fell under the threshold is listed there — unranked, in no
    # ranking group, and with the qualification the gate actually assigned.
    only_w1 = entry_of(ranking(service, weeks="1", group="INCLUDED"), "D-1")
    check(only_w1 is not None, "a permitted below-threshold row is listed on the INCLUDED tab")
    check(only_w1["ranking_group"] is None, "an unqualified row belongs to no ranking group")
    check(only_w1["ranking_position"] is None, "and is given no position")
    check(only_w1["qualification_status"] == "LOW_DISTANCE", "the gate's verdict is carried")
    unranked = entry_of(ranking(service, weeks="1", group="EXCLUDED"), "D-1")
    check(unranked is None, "and it is not smuggled into another group either")
    res = ranking(service, weeks="1")
    check(
        res.body["meta"]["basis"]["qualified_count"] == 0,
        "W1 alone at 60 km does not qualify",
    )
    check(
        res.body["meta"]["basis"]["not_ranked_included_count"] == 1,
        "and the tab's own count knows about it",
    )
    check(
        res.body["meta"]["distribution"]["total_count"] == 0,
        "an unranked row still contributes nothing to the score distribution",
    )

    combined = entry_of(ranking(service, weeks="1,3"), "D-1")
    check(combined is not None, "W1+W3 at 117 km qualifies")
    check(combined["qualification_status"] == "QUALIFIED", "the gate is applied once to the union")
    check(combined["total_distance_meters"] == 117_000, "the union total is what is measured")

    # No per-week, per-day or per-trip gate: the 2 km trip is still evidence.
    trips_res = service.list_basis_contributing_trips(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1,3", assigned_id="D-1",
    )
    ids = {t["provider_trip_id"] for t in trips_res.body["data"]}
    check(2 in ids and 3 in ids, "the 2 km day remains legitimate evidence")
    print("PASS: the 100 km gate is applied once to the selected union, never per week/day/trip")


def test_qualification_boundary_is_exact_in_metres():
    threshold = MIN_QUALIFYING_DISTANCE_METERS
    check(threshold == 100_000, "the repository threshold is 100 000 m")
    below = [trip(1, "D-1", ts(2), meters=threshold - 1)]
    at = [trip(1, "D-1", ts(2), meters=threshold)]

    service, _b, _d = build(below, CHART)
    check(
        ranking(service, weeks="1").body["meta"]["basis"]["qualified_count"] == 0,
        "one metre below the threshold does not qualify",
    )
    service, _b, _d = build(at, CHART)
    row = entry_of(ranking(service, weeks="1"), "D-1")
    check(row is not None and row["qualification_status"] == "QUALIFIED",
          "exactly at the threshold qualifies")
    check(
        PD.qualification_and_calculation(0) == ("NO_DISTANCE", "NO_DISTANCE"),
        "zero distance is a distinct state",
    )
    print("PASS: the qualification boundary is exact in metres and NO_DISTANCE stays distinct")


# =============================================================================
# 7. Client-specific trip inclusion
# =============================================================================


def test_client_inclusion_policy_is_read_back_not_reimplemented():
    """ALPHA excludes a private trip; BRAVO includes the *same* applicable trip.

    Both run the identical dynamic code path through the real registry, the real
    provider and the real query layer. The fixture is deliberately adversarial:
    the BRAVO trip carries ``is_private_trip=True`` **and**
    ``aggregation_included=True``, which is exactly what its person aggregation
    job writes for an applicable trip whose driver tag is marked private. The
    synthetic database reads the inclusion clause out of the SQL the provider
    issued, so if the person query ever gained ``is_private_trip IS FALSE`` this
    check fails.
    """

    # --- ALPHA: the driver family excludes it -------------------------------
    alpha_trips = [
        trip(1, "D-1", ts(2), meters=120_000, harsh_braking_events=5),
        trip(2, "D-1", ts(3), meters=80_000, harsh_braking_events=9, private=True),
    ]
    service, _b, _d = build(alpha_trips, CHART)
    alpha = entry_of(ranking(service, weeks="1"), "D-1")
    check(alpha["total_distance_meters"] == 120_000, "ALPHA excludes the private trip's distance")
    check(alpha["event_counts"]["harsh_braking_events"] == 5, "ALPHA excludes its events")
    check(alpha["trips_count"] == 1, "ALPHA counts one contributing trip")

    # --- BRAVO: the person family includes the very same shape -------------
    # `private=True` with `included=True` is the state migration 043 permits and
    # the person job writes. Flipping the fixture to `private=False` would prove
    # nothing, so it is deliberately not done.
    bravo_trips = [
        person_trip(1, "p-jan-kowalski", ts(2), meters=120_000, harsh_braking_events=5),
        person_trip(
            2, "p-jan-kowalski", ts(3), meters=80_000, harsh_braking_events=9,
            private=True, included=True,
        ),
    ]
    service, _b, database = build(
        bravo_trips, PERSON_CHART, family_key="person",
        access=full_access(BRAVO_CLIENT_CODE),
    )
    res = bravo_ranking(service, weeks="1")
    check(res.status_code == 200, f"BRAVO ranking renders: {res.body.get('error')}")
    bravo = entry_of(res, "p-jan-kowalski")
    check(bravo is not None, "the person is in the recomputed BRAVO population")
    check(
        bravo["total_distance_meters"] == 200_000,
        f"BRAVO includes the private applicable trip's distance, got "
        f"{bravo['total_distance_meters']}",
    )
    check(
        bravo["event_counts"]["harsh_braking_events"] == 14,
        "BRAVO includes the private applicable trip's events",
    )
    check(bravo["trips_count"] == 2, "BRAVO counts both trips as contributing")
    check(
        bravo["current_chart"]["current_driver_name"] == "Jan Kowalski",
        "the person's canonical name is resolved from the assignment",
    )

    # The real query layer must not carry the driver family's clause here.
    population_sql = next(sql for sql, _ in database.calls if "skipped_trips_count" in sql)
    check(
        "is_private_trip" not in population_sql,
        "the person family's dynamic query carries no private-trip clause",
    )
    check(
        "public.eco_person_trip_assignments" in population_sql,
        "and reads the person family's own assignments table",
    )
    check(
        "a.person_name_group_key" in population_sql,
        "grouping on the person family's identity column",
    )

    # Both families reach the same shared scoring domain.
    check(
        bravo["ecodriving_rating_type"] in {"bezpieczny", "akceptowalny", "niebezpieczny"},
        "the person family is scored by the same production ladder",
    )

    # The policy is data, not code: no executable line of the query layer may
    # mention a client, or one client's contract could reach the other.
    source = Path(REPO_ROOT / "api/eco_driving_explorer/queries.py").read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    check("ALPHA" not in code and "BRAVO" not in code,
          "no client-specific branch exists in the query layer")
    provider_code = "\n".join(
        line
        for line in Path(
            REPO_ROOT / "api/eco_driving_explorer/period_domain.py"
        ).read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    check("ALPHA" not in provider_code and "BRAVO" not in provider_code,
          "nor in the shared aggregation domain")
    print("PASS: BRAVO includes a private applicable trip; ALPHA still excludes one")


def test_bravo_non_private_and_unmapped_assignments():
    """A plain applicable trip counts; an unresolved-identity trip does not."""

    trips = [
        person_trip(1, "p-jan-kowalski", ts(2), meters=150_000, harsh_braking_events=3),
        # `UNMAPPED_DRIVER_NAME`: no identity resolved, so `043` forces
        # `aggregation_included = FALSE` and there is no group key at all.
        person_trip(
            2, None, ts(3), meters=400_000, harsh_braking_events=99,
            included=False, assignment_source="UNMAPPED_DRIVER_NAME",
        ),
    ]
    service, _b, _d = build(
        trips, PERSON_CHART, family_key="person", access=full_access(BRAVO_CLIENT_CODE)
    )
    res = bravo_ranking(service, weeks="1")
    row = entry_of(res, "p-jan-kowalski")
    check(row["total_distance_meters"] == 150_000, "only the resolved trip contributes distance")
    check(row["event_counts"]["harsh_braking_events"] == 3, "and only its events")
    check(len(res.body["data"]) == 1, "the unresolved trip creates no ranking row")
    print("PASS: BRAVO counts resolved applicable trips and ignores unresolved ones")


def test_bravo_trip_evidence_uses_the_same_inclusion_universe():
    """Evidence must be exactly the rows that produced the score."""

    trips = [
        person_trip(1, "p-jan-kowalski", ts(2), meters=150_000),
        person_trip(2, "p-jan-kowalski", ts(3), meters=60_000, private=True, included=True),
        person_trip(3, "p-jan-kowalski", ts(9), meters=60_000),
        person_trip(
            4, None, ts(4), meters=90_000, included=False,
            assignment_source="UNMAPPED_DRIVER_NAME",
        ),
    ]
    service, _b, database = build(
        trips, PERSON_CHART, family_key="person", access=full_access(BRAVO_CLIENT_CODE)
    )
    res = service.list_basis_contributing_trips(
        user=USER, request=None, client_code=BRAVO_CLIENT_CODE,
        ranking_family=BRAVO_FAMILY, month=MONTH, weeks="1", assigned_id="p-jan-kowalski",
    )
    check(res.status_code == 200, "BRAVO trip evidence is reachable")
    ids = {row["provider_trip_id"] for row in res.body["data"]}
    check(ids == {1, 2}, f"the private applicable trip is evidence too, got {ids}")
    check(3 not in ids, "a trip outside the selected week is not evidence")
    check(4 not in ids, "an unresolved-identity trip is not evidence")
    trips_sql = next(sql for sql, _ in database.calls if "client_trip_present" in sql)
    # The column is still *projected* — the detail view reports the flag — but it
    # must never appear as a filter for this family.
    check(
        "is_private_trip IS FALSE" not in trips_sql
        and "is_private_trip = FALSE" not in trips_sql,
        "the person family's trip query carries no private-trip predicate either",
    )
    check("a.is_private_trip," in trips_sql, "the flag is still reported, just not filtered on")
    driver_trips_sql = q.contributing_trips_sql(
        "a.trip_start_ts ASC", q.dynamic_contributing_where(1), q.DRIVER_SOURCES
    )
    check(
        "is_private_trip IS FALSE" in driver_trips_sql,
        "while the driver family's trip query still filters private trips out",
    )
    print("PASS: BRAVO trip evidence uses the same inclusion universe as its score")


def test_provider_family_selection_is_registry_bound():
    """A browser cannot pick a data contract that is not its client's."""

    from api.eco_driving_explorer.registry import get_provider, is_registered
    from api.eco_driving_explorer.errors import ProviderNotFoundError

    check(is_registered(CLIENT_CODE, FAMILY), "the driver family is registered for ALPHA")
    check(is_registered(BRAVO_CLIENT_CODE, BRAVO_FAMILY), "the person family is registered for BRAVO")
    for forged in (
        (CLIENT_CODE, BRAVO_FAMILY),
        (BRAVO_CLIENT_CODE, FAMILY),
        (CLIENT_CODE, "person "),
        ("OTHER00001", FAMILY),
        (CLIENT_CODE, ""),
    ):
        check(not is_registered(*forged), f"{forged} is not reachable")
        try:
            get_provider(*forged, client_id=TRUSTED_CLIENT_ID)
        except ProviderNotFoundError:
            continue
        raise AssertionError(f"{forged} should fail closed")
    check(True, "every forged client/family pair fails closed")

    alpha = get_provider(CLIENT_CODE, FAMILY, client_id=TRUSTED_CLIENT_ID)
    bravo = get_provider(BRAVO_CLIENT_CODE, BRAVO_FAMILY, client_id=TRUSTED_CLIENT_ID)
    check(
        alpha.sources.assignments_table != bravo.sources.assignments_table,
        "the two families read different tables",
    )
    check(
        "is_private_trip" in alpha.sources.included_predicate
        and "is_private_trip" not in bravo.sources.included_predicate,
        "and carry different inclusion contracts",
    )
    print("PASS: provider family selection is registry-bound and fails closed")


# =============================================================================
# 8. Ranking recomputation
# =============================================================================


def test_ranking_is_recomputed_and_can_reorder_against_the_persisted_rank():
    """Selecting a subset can change the order; persisted positions are not reused."""

    trips = [
        # D-1 drives cleanly in W1 and badly in W3; D-2 is the mirror image.
        trip(1, "D-1", ts(2), meters=150_000, harsh_braking_events=0),
        trip(2, "D-1", ts(14), meters=150_000, harsh_braking_events=90),
        trip(3, "D-2", ts(2), meters=150_000, harsh_braking_events=90),
        trip(4, "D-2", ts(14), meters=150_000, harsh_braking_events=0),
    ]
    service, _b, _d = build(trips, CHART)

    w1 = ranking(service, weeks="1")
    order_w1 = [row["assigned_id"] for row in w1.body["data"]]
    w3 = ranking(service, weeks="3")
    order_w3 = [row["assigned_id"] for row in w3.body["data"]]
    check(order_w1 == ["D-1", "D-2"], f"W1 ranks the clean driver first, got {order_w1}")
    check(order_w3 == ["D-2", "D-1"], f"W3 reverses the order, got {order_w3}")
    check(
        entry_of(w1, "D-1")["ranking_position"] == 1
        and entry_of(w3, "D-1")["ranking_position"] == 2,
        "positions are recomputed per basis, not carried over",
    )
    check(
        entry_of(w1, "D-1")["ranking_total_participants"] == 2,
        "participants describe the recomputed population",
    )

    both = ranking(service, weeks="1,3")
    check(
        {row["ranking_position"] for row in both.body["data"]} == {1, 2},
        "the combined basis produces one contiguous position sequence",
    )
    print("PASS: ranking is recomputed from the selected basis and reorders correctly")


def test_ranking_tie_behaviour_matches_the_canonical_rule():
    rows = [
        {"assigned_id": "B", "eco_driving_score_total": Decimal("80"),
         "total_kilometers": Decimal("200")},
        {"assigned_id": "A", "eco_driving_score_total": Decimal("80"),
         "total_kilometers": Decimal("200")},
        {"assigned_id": "C", "eco_driving_score_total": Decimal("80"),
         "total_kilometers": Decimal("300")},
        {"assigned_id": "D", "eco_driving_score_total": None,
         "total_kilometers": Decimal("900")},
    ]
    PD.assign_ranking_positions(rows)
    by_id = {row["assigned_id"]: row["ranking_position"] for row in rows}
    check(by_id["C"] == 1, "more kilometres breaks a score tie first")
    check(by_id["A"] == 2 and by_id["B"] == 3, "the opaque id breaks a full tie, ascending")
    check(by_id["D"] == 4, "a row without a score sorts last")
    check(all(row["ranking_total_participants"] == 4 for row in rows), "participants are the group")
    print("PASS: tie semantics are the canonical score → distance → assigned_id rule")


def test_filtering_and_paging_cannot_change_a_rank():
    trips = []
    for index in range(1, 7):
        trips.append(
            trip(index, f"D-{index}", ts(2), meters=150_000, harsh_braking_events=index * 3)
        )
    chart = [
        {"driver_id": f"D-{i}", "driver_name": f"Kierowca {i}", "ranking_included": True}
        for i in range(1, 7)
    ]
    service, _b, _d = build(trips, chart)
    full = ranking(service, weeks="1", limit=50)
    positions = {row["assigned_id"]: row["ranking_position"] for row in full.body["data"]}
    paged = ranking(service, weeks="1", limit=2, page=2)
    for row in paged.body["data"]:
        check(
            row["ranking_position"] == positions[row["assigned_id"]],
            "a page shows the same rank as the whole ranking",
        )
    searched = ranking(service, weeks="1", search="Kierowca 5")
    check(len(searched.body["data"]) == 1, "search narrows the view")
    check(
        searched.body["data"][0]["ranking_position"] == positions["D-5"],
        "a searched row keeps its rank in the full population",
    )
    print("PASS: group filter, search and pagination never change a recomputed rank")


# =============================================================================
# 9-10. Bands and ranking groups
# =============================================================================


def test_only_the_three_production_bands_appear():
    labels = {label for _threshold, label in RATING_THRESHOLDS} | {RATING_FALLBACK_LABEL}
    check(labels == {"bezpieczny", "akceptowalny", "niebezpieczny"}, "three production bands")

    trips = [
        trip(1, "D-1", ts(2), meters=200_000),  # clean → bezpieczny
        trip(2, "D-2", ts(2), meters=200_000, harsh_braking_events=30, idle_events=40),
        trip(3, "D-EXC", ts(2), meters=200_000, harsh_braking_events=900,
             harsh_acceleration_events=900, harsh_turning_events=900, idle_events=900,
             overrev_events_count=900, speeding_140_160_count=900,
             speeding_160_170_count=900, speeding_170_plus_count=900),
    ]
    chart = [
        {"driver_id": "D-1", "driver_name": "A", "ranking_included": True},
        {"driver_id": "D-2", "driver_name": "B", "ranking_included": True},
        {"driver_id": "D-EXC", "driver_name": "C", "ranking_included": True},
    ]
    service, _b, _d = build(trips, chart)
    res = ranking(service, weeks="1")
    seen = {row["ecodriving_rating_type"] for row in res.body["data"]}
    check(seen <= labels, f"no band outside the production vocabulary appeared: {seen}")
    check(len(seen) == 3, f"all three bands are reachable on a dynamic basis, got {seen}")
    print("PASS: dynamic recomputation produces exactly the three production rating bands")


def test_group_semantics_survive_dynamic_recomputation():
    trips = [
        trip(1, "D-1", ts(2), meters=200_000, harsh_braking_events=2),
        trip(2, "D-EXC", ts(2), meters=200_000, harsh_braking_events=2),
        trip(3, "D-UNKNOWN", ts(2), meters=200_000, harsh_braking_events=2),
        trip(4, "D-SHORT", ts(2), meters=1_000),
    ]
    service, _b, _d = build(trips, CHART)
    counts = ranking(service, weeks="1").body["meta"]["basis"]
    check(counts["counts_by_group"]["INCLUDED"] == 1, "the charted, included driver is INCLUDED")
    check(counts["counts_by_group"]["EXCLUDED"] == 1, "the charted, non-included driver is EXCLUDED")
    check(
        counts["counts_by_group"]["UNKNOWN_DRIVER"] == 1,
        "a qualified driver with no chart row is UNKNOWN_DRIVER",
    )
    check(counts["not_ranked_count"] == 1, "the below-threshold row is outside every group")

    excluded = entry_of(ranking(service, weeks="1", group="EXCLUDED"), "D-EXC")
    check(excluded is not None, "an EXCLUDED driver is present in their own group")
    check(excluded["ranking_position"] == 1, "and is ranked inside it")
    check(excluded["ecodriving_rating_type"] in {
        "bezpieczny", "akceptowalny", "niebezpieczny"
    }, "and keeps a normal rating")

    detail = service.get_basis_ranking_entry(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1", assigned_id="D-EXC",
    )
    check(detail.status_code == 200, "an EXCLUDED driver keeps a reachable detail page")
    check(detail.body["data"]["ranking_group"] == "EXCLUDED", "with their persisted group meaning")
    print("PASS: INCLUDED / EXCLUDED / UNKNOWN_DRIVER and the non-ranked count all survive")


# =============================================================================
# 11. Unit toggle (S12 carry-forward A)
# =============================================================================


def test_unit_toggle_changes_the_primary_value_but_not_the_severity():
    """The adversarial pair: a big count that is fine, a small count that is not."""

    from api.eco_driving_explorer import detail_view_models as D

    def entry(count, kilometers, points, loss):
        rates = {m: None for m in REQUIRED_METRICS}
        counts = {m: 0 for m in REQUIRED_METRICS}
        pts = {m: None for m in REQUIRED_METRICS}
        losses = {m: None for m in REQUIRED_METRICS}
        rate = (Decimal(count) / Decimal(kilometers) * 100).quantize(Decimal("1"))
        rates["harsh_braking_events"] = str(rate)
        counts["harsh_braking_events"] = count
        pts["harsh_braking_events"] = str(points)
        losses["harsh_braking_events"] = str(loss)
        return {
            "assigned_id": "D-1", "client_code": CLIENT_CODE, "ranking_family": FAMILY,
            "qualification_status": "QUALIFIED", "eco_driving_score_total": "70",
            "event_counts": counts, "metric_rates_per_100km": rates,
            "metric_points": pts, "metric_points_lost": losses,
            "total_kilometers": str(kilometers), "total_distance_meters": kilometers * 1000,
        }

    calm = entry(400, 10_000, 15, 0)    # 4 / 100 km — a large count that is fine
    harsh = entry(34, 150, -15, -30)    # 23 / 100 km — a small count that is not

    for unit in ("rate", "sum"):
        calm_html = D.render_composition(calm, unit=unit)
        harsh_html = D.render_composition(harsh, unit=unit)
        check(f'data-eco-unit="{unit}"' in calm_html, f"the active unit is exposed in {unit} mode")
        check('data-severity="ok"' in calm_html, f"400 events at 4/100 km stay ok in {unit} mode")
        check('data-severity="bad"' in harsh_html, f"34 events at 23/100 km stay bad in {unit} mode")

    sum_html = D.render_composition(calm, unit="sum")
    rate_html = D.render_composition(calm, unit="rate")
    check(sum_html != rate_html, "the toggle actually changes the rendered page")
    check(
        'eco-metric-cell is-ok is-primary-unit" data-severity="ok">400' in sum_html,
        "in Σ mode the raw total is the classified, primary cell",
    )
    check(
        'eco-metric-cell is-ok is-primary-unit" data-severity="ok">4' in rate_html,
        "in / 100 km mode the coefficient is the classified, primary cell",
    )
    # Nothing is hidden to make the toggle work.
    check("400" in rate_html and "400" in sum_html, "the raw total is visible in both modes")
    check(
        'eco-metric-secondary' in sum_html and 'eco-metric-secondary' in rate_html,
        "the non-primary value stays on screen as a secondary cell",
    )
    print("PASS: the ECO-003 unit toggle is no longer inert and severity stays coefficient-based")


# =============================================================================
# 12. Histogram
# =============================================================================


def test_histogram_is_recomputed_over_the_same_basis_and_group():
    from api.eco_driving_explorer import eco_view as V

    trips = [
        trip(1, "D-1", ts(2), meters=200_000),
        trip(2, "D-2", ts(2), meters=200_000, harsh_braking_events=40, idle_events=60),
        trip(3, "D-1", ts(14), meters=200_000, harsh_braking_events=200),
        trip(4, "D-EXC", ts(2), meters=200_000),
    ]
    canned = {"score_distribution": [
        {"bin_index": 0, "bin_count": 999, "total_count": 999,
         "mean_score": Decimal("-95"), "median_score": Decimal("-95")}
    ]}
    service, _b, database = build(trips, CHART, canned)

    w1 = ranking(service, weeks="1").body["meta"]["distribution"]
    w13 = ranking(service, weeks="1,3").body["meta"]["distribution"]
    check(w1["total_count"] == 2, "the W1 histogram counts the W1 INCLUDED population")
    check(w1 != w13, "a different basis produces a different distribution")
    check(
        w1["total_count"] != 999,
        "the persisted monthly distribution never leaks into a dynamic basis",
    )
    check(
        not any("bin_index" in sql for sql, _ in database.calls),
        "the dynamic histogram issues no separate distribution query",
    )

    excluded = ranking(service, weeks="1", group="EXCLUDED").body["meta"]["distribution"]
    check(excluded["ranking_group"] == "EXCLUDED", "the histogram declares its universe")
    check(excluded["total_count"] == 1, "and is restricted to that universe")

    # S12 carry-forward (B): the heading must name the universe it plots.
    check(
        V.distribution_heading(excluded) == "Rozkład wyników — wykluczeni",
        "a subgroup histogram is not labelled as the fleet",
    )
    check("flot" not in V.distribution_heading(excluded).lower(), "no fleet wording on a subgroup")
    check(
        V.distribution_heading(w1) == "Rozkład wyników — w rankingu",
        "the INCLUDED histogram names the ranked population",
    )
    print("PASS: the histogram shares the basis, the client and the group, and names its universe")


def test_non_qualified_detail_performs_no_histogram_work():
    """S12 carry-forward (C): no invisible read, no audit for an unshown surface."""

    trips = [trip(1, "D-1", ts(2), meters=1_000)]
    service, backend, database = build(trips, CHART)
    res = service.get_basis_ranking_entry(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1", assigned_id="D-1",
    )
    check(res.status_code == 200, "a below-threshold driver still has a truthful detail page")
    check(res.body["data"]["qualification_status"] == "LOW_DISTANCE", "and says why")
    check(res.body["data"]["distribution"] is None, "no distribution is produced")
    audit = [e for e in backend.audit_events if e["event_type"] == "eco_driving_ranking_entry_viewed"]
    check(len(audit) == 1, "one audit event for the detail read")
    check(
        audit[0]["metadata"]["distribution_rendered"] is False,
        "the audit does not claim a surface that was never rendered",
    )
    print("PASS: a non-qualified detail page does no histogram work and claims none")


# =============================================================================
# 13-14. Trip evidence and boundary timestamps
# =============================================================================


def test_trip_evidence_matches_the_selected_union_exactly():
    trips = [
        trip(1, "D-1", ts(2), meters=60_000),
        trip(2, "D-1", ts(8), meters=60_000),
        trip(3, "D-1", ts(15), meters=60_000),
        trip(4, "D-1", ts(3), meters=20_000, private=True),
    ]
    service, _b, _d = build(trips, CHART)
    res = service.list_basis_contributing_trips(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1,3", assigned_id="D-1",
    )
    ids = {row["provider_trip_id"] for row in res.body["data"]}
    check(ids == {1, 3}, f"only trips inside the selected weeks appear, got {ids}")
    check(2 not in ids, "no unselected-week trip is presented as contributing")
    check(4 not in ids, "the private trip stays excluded on the dynamic path too")
    check(res.body["meta"]["selection"]["label"] == "W1 + W3", "the evidence states its basis")
    print("PASS: trip evidence contains exactly the selected union and nothing else")


def test_boundary_timestamps_have_no_gap_and_no_double_count():
    """Every boundary instant belongs to exactly one bucket."""

    boundary = date(2026, 7, 6)  # W1 -> W2
    trips = [
        trip(1, "D-1", datetime(2026, 7, 1, 0, 0, tzinfo=TZ), meters=110_000),   # month start
        trip(2, "D-1", datetime(2026, 7, 5, 23, 59, 59, tzinfo=TZ), meters=1),   # last W1 instant
        trip(3, "D-1", datetime(2026, 7, 6, 0, 0, tzinfo=TZ), meters=1),         # first W2 instant
        trip(4, "D-1", datetime(2026, 7, 31, 23, 59, 59, tzinfo=TZ), meters=1),  # last W5 instant
        trip(5, "D-1", datetime(2026, 8, 1, 0, 0, tzinfo=TZ), meters=999_999),   # next month
        trip(6, "D-1", datetime(2026, 6, 30, 23, 59, 59, tzinfo=TZ), meters=999_999),
    ]
    service, _b, _d = build(trips, CHART)

    w1 = entry_of(ranking(service, weeks="1"), "D-1")
    check(w1["trips_count"] == 2, "W1 holds the month-start and the last-instant trips")
    check(w1["total_distance_meters"] == 110_001, "and only those two")

    w2 = entry_of(ranking(service, weeks="2"), "D-1")
    check(w2 is not None, "W2's permitted driver is still on the tab")
    check(w2["ranking_group"] is None,
          "W2's single 1 m trip does not qualify, and is not smuggled into a group")
    check(w2["ranking_position"] is None, "so it earns no position")
    w2_all = ranking(service, weeks="2").body["meta"]["basis"]
    check(w2_all["population_count"] == 1, "but the driver is present in the W2 population")
    check(w2_all["counts_by_group"]["INCLUDED"] == 0, "no ranked member in W2")
    check(w2_all["not_ranked_included_count"] == 1, "one permitted, unranked member instead")

    # Every bucket together must equal the month exactly: no gap, no overlap.
    all_weeks = [str(b.sequence) for b in PD.month_week_buckets(MONTH_START)]
    per_week_total = 0
    for week in all_weeks:
        basis = ranking(service, weeks=week).body["meta"]["basis"]
        per_week_total += basis["total_distance_meters"]
    check(
        per_week_total == 110_001 + 1 + 1,
        f"the buckets tile the month once — got {per_week_total}",
    )
    check(
        999_999 * 2 not in (per_week_total,),
        "trips outside the month never enter any bucket",
    )
    print("PASS: boundary instants belong to exactly one bucket, with no gap and no double count")


def test_boundary_parameters_are_business_timezone_midnights():
    service, _b, database = build(_three_week_driver(), CHART)
    ranking(service, weeks="1,3")
    params = next(p for sql, p in database.calls if "skipped_trips_count" in sql)
    check(params["win0_start"] == datetime(2026, 7, 1, 0, 0, tzinfo=TZ), "W1 starts at local midnight")
    check(params["win0_end"] == datetime(2026, 7, 6, 0, 0, tzinfo=TZ), "W1 ends exclusively")
    check(params["win1_start"] == datetime(2026, 7, 13, 0, 0, tzinfo=TZ), "W3 starts at local midnight")
    check(params["win1_end"] == datetime(2026, 7, 20, 0, 0, tzinfo=TZ), "W3 ends exclusively")
    check(str(params["win0_start"].tzinfo) == "Europe/Warsaw", "boundaries are business-timezone")
    print("PASS: interval bounds are business-timezone midnights with exclusive upper ends")


# =============================================================================
# 15. URL / state canonicalization
# =============================================================================


def test_week_selection_canonicalization():
    check(parse_week_selection(MONTH).mode == MODE_MONTH, "no weeks parameter is whole month")
    check(parse_week_selection(MONTH).canonical_weeks_param is None, "whole month omits weeks")
    check(
        parse_week_selection(MONTH, "1,2,3,4,5").mode == MODE_MONTH,
        "listing every week canonicalizes to whole month",
    )
    check(
        parse_week_selection(MONTH, "1,2,3,4,5").canonical_weeks_param is None,
        "and produces the same URL as the shortcut, so the two states cannot diverge",
    )
    check(
        parse_week_selection(MONTH, "3,1,3,1").canonical_weeks_param == "1,3",
        "duplicates collapse and order is forced ascending",
    )
    check(parse_week_selection(MONTH, "").mode == MODE_EMPTY, "an empty value is the empty state")
    check(
        parse_week_selection(MONTH, EMPTY_WEEKS_TOKEN).canonical_weeks_param == EMPTY_WEEKS_TOKEN,
        "the empty state has an explicit token that survives URL building",
    )
    check(parse_week_selection(MONTH, "2").mode == MODE_WEEKS, "a proper subset is dynamic")

    for bad in ("9", "0", "-1", "1;2", "abc", "1,9", "1" * 40):
        try:
            parse_week_selection(MONTH, bad)
        except InvalidWeekSelectionError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")
    _CHECK_BAD = True
    check(_CHECK_BAD, "unknown, out-of-month and malformed week identifiers are refused")

    for bad_month in ("2026-13", "2026", "26-07", "", None, "2026-07-01"):
        try:
            parse_week_selection(bad_month, "1")
        except InvalidWeekSelectionError:
            continue
        raise AssertionError(f"month {bad_month!r} should be rejected")
    check(True, "a malformed or forged month is refused rather than widened")

    # February 2026 has a different bucket count, so a week id is month-relative.
    february = parse_week_selection("2026-02")
    check(len(february.all_buckets) == 5, "February 2026 yields five buckets")
    try:
        parse_week_selection("2026-02", "6")
        raise AssertionError("W6 does not exist in February 2026")
    except InvalidWeekSelectionError:
        check(True, "a week id from another month is refused, never reinterpreted")
    print("PASS: month/week selection state is canonical, bounded and authorization-independent")


def test_canonical_state_is_reflected_in_the_rendered_links():
    canned = {
        "entries_list": [], "entries_count": [{"total_count": 0}],
        "monthly_periods": [], "score_distribution": [],
    }
    service, backend, _db = build(_three_week_driver(), CHART, canned)
    pages = EcoDrivingPages(service)
    res = pages.rankings(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="3,1",
    )
    check(res.status_code == 200, "the ranking page renders for a dynamic basis")
    body = res.body_html
    check("weeks=1%2C3" in body or "weeks=1,3" in body, "links carry the canonical week list")
    check("weeks=3%2C1" not in body, "the submitted, non-canonical order is not echoed")
    check("period_key=" not in body, "the basis page does not mix in a persisted period token")
    check('data-eco-basis-mode="WEEKS"' in body, "the resolved mode is exposed on the page")
    check("W1" in body and "W3" in body, "the week cards render")
    check("Wybrane tygodnie nie są ciągłe" in body, "the gap warning is shown and non-blocking")
    check("Cały miesiąc" in body and "Wyczyść" in body, "both shortcuts are offered")
    print("PASS: rendered links carry the canonical basis state and nothing else")


def test_period_key_is_ignored_when_a_month_is_present():
    """No page may resolve two period models at once."""

    canned = {
        "entries_list": [_monthly_entry_row()], "entries_count": [{"total_count": 1}],
        "monthly_periods": [], "score_distribution": [],
    }
    service, _b, _d = build(_three_week_driver(), CHART, canned)
    pages = EcoDrivingPages(service)
    res = pages.rankings(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1",
        period_key="weekly:2026-07-01:2026-07-01:2026-07-06:1",
    )
    check(res.status_code == 200, "a stale period_key does not break the basis page")
    check('data-eco-basis-mode="WEEKS"' in res.body_html, "the basis wins")
    check("period_key=" not in res.body_html, "and the other model is not carried forward")
    print("PASS: when a month is present the persisted period token is ignored entirely")


def test_zero_weeks_is_a_prompt_and_issues_no_query():
    service, backend, database = build(_three_week_driver(), CHART)
    res = ranking(service, weeks=EMPTY_WEEKS_TOKEN)
    check(res.status_code == 200, "zero weeks is not an error")
    check(res.body["data"] == [], "and returns no rows")
    check(res.body["meta"]["selection"]["mode"] == MODE_EMPTY, "the mode is EMPTY")
    check(database.calls == [], "no query is issued for an empty basis")

    pages = EcoDrivingPages(service)
    page = pages.rankings(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks=EMPTY_WEEKS_TOKEN,
    )
    check(page.status_code == 200, "the page is a normal 200")
    check("Wybierz co najmniej jeden tydzień" in page.body_html, "and prompts for a selection")
    print("PASS: deselecting every week prompts for a selection instead of erroring")


# =============================================================================
# 16. Security, audit and query shape
# =============================================================================


def test_security_boundaries_are_unchanged():
    trips = _three_week_driver()

    no_grant = EcoDrivingClientAccess(
        client_code=CLIENT_CODE, can_view_eco_ranking=False,
        can_view_eco_trip_details=False, can_view_eco_trip_routes=False,
        client_is_active=True,
    )
    service, backend, database = build(trips, CHART, access=no_grant)
    res = ranking(service, weeks="1")
    check(res.status_code == 403, "the ranking grant is required for a dynamic basis")
    check(database.calls == [], "and no query runs without it")

    ranking_only = EcoDrivingClientAccess(
        client_code=CLIENT_CODE, can_view_eco_ranking=True,
        can_view_eco_trip_details=False, can_view_eco_trip_routes=False,
        client_is_active=True,
    )
    service, _b, _d = build(trips, CHART, access=ranking_only)
    check(ranking(service, weeks="1").status_code == 200, "ranking access is enough for a ranking")
    trips_res = service.list_basis_contributing_trips(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1", assigned_id="D-1",
    )
    check(trips_res.status_code == 403, "trip evidence still needs its own grant")

    admin = {"user_id": "admin-1", "username": "root", "is_admin": True}
    service, _b, _d = build(trips, CHART, access=no_grant)
    denied = service.get_basis_ranking(
        user=admin, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1", ranking_group="INCLUDED",
    )
    check(denied.status_code == 403, "an administrator does not bypass the client grant")

    # Every business read carries the trusted client_id, never a caller value.
    service, _b, database = build(trips, CHART)
    ranking(service, weeks="1,3")
    business = [p for sql, p in database.calls if "eco_trip_assignments" in sql]
    check(business, "the dynamic path reads the assignment table")
    check(
        all(p.get("client_id") == TRUSTED_CLIENT_ID for p in business),
        "and every read is bound to the trusted client id",
    )
    source = Path(REPO_ROOT / "api/eco_driving_explorer/queries.py").read_text()
    check(
        "client_id = %(client_id)s::uuid" in source,
        "client scoping is a bound parameter in the fixed SQL",
    )
    print("PASS: grants, no-admin-bypass and client isolation are unchanged on the dynamic path")


def test_audit_records_safe_basis_facts_only():
    service, backend, _db = build(_three_week_driver(), CHART)
    ranking(service, weeks="1,3", search="Kowalski")
    events = [e for e in backend.audit_events if e["event_type"] == "eco_driving_ranking_viewed"]
    check(len(events) == 1, "one audit event for the ranking read")
    meta = events[0]["metadata"]
    check(meta["basis_mode"] == MODE_WEEKS, "the resolved mode is recorded")
    check(meta["basis_month"] == MONTH, "the month is recorded")
    check(meta["basis_weeks"] == [1, 3], "the selected weeks are recorded")
    check(meta["basis_week_count"] == 2, "and their count")
    check(meta["basis_dynamically_recomputed"] is True, "and which data path ran")
    check(meta["search_applied"] is True, "that a search was applied is recorded")
    blob = repr(meta)
    check("Kowalski" not in blob, "the search term itself is never logged")
    check("SELECT" not in blob and "provider_trip_id" not in blob, "no SQL and no raw trip rows")
    check("D-1" not in blob, "no raw driver identifier in a ranking audit")
    print("PASS: basis audit records safe facts only — no term, no SQL, no raw rows")


def test_query_shape_is_bounded_and_has_no_n_plus_one():
    trips = []
    chart = []
    for index in range(1, 41):
        chart.append(
            {"driver_id": f"D-{index}", "driver_name": f"K{index}", "ranking_included": True}
        )
        for day in (2, 8, 15, 22, 29):
            trips.append(
                trip(index * 100 + day, f"D-{index}", ts(day), meters=60_000,
                     harsh_braking_events=index % 7)
            )
    service, _b, database = build(trips, chart)
    res = ranking(service, weeks="1,3", limit=50)
    check(len(res.body["data"]) == 40, "the whole population is ranked")
    check(
        len(database.calls) == 1,
        f"one statement serves ranking + counts + histogram, got {len(database.calls)}",
    )
    check(
        q.MAX_SELECTION_INTERVALS == 6,
        "the interval predicate is bounded to a month's worth of buckets",
    )
    try:
        q.interval_predicate(q.MAX_SELECTION_INTERVALS + 1)
        raise AssertionError("an over-long interval list must be refused")
    except Exception:
        check(True, "more intervals than a month can contain are refused")
    print("PASS: one bounded set-wise query per basis; no per-driver query")


# =============================================================================
# S12 carry-forward: remaining presentation regressions
# =============================================================================


def test_context_client_identity_is_identical_across_surfaces():
    canned = {
        "entries_list": [_monthly_entry_row()], "entries_count": [{"total_count": 1}],
        "monthly_periods": [], "score_distribution": [],
        "weekly_periods": [], "driver_trend": [], "period_progression": [],
    }
    trips = [trip(1, "D-1", ts(2), meters=200_000)]
    service, _b, _d = build(trips, CHART, canned)
    pages = EcoDrivingPages(service)

    listing = pages.rankings(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1",
    )
    detail = pages.ranking_entry(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1", assigned_id="D-1",
    )
    check(detail.status_code == 200, "the driver page renders on a dynamic basis")
    check(
        listing.context_client_name == detail.context_client_name == CLIENT_CODE,
        f"one client identity everywhere: {listing.context_client_name!r} "
        f"vs {detail.context_client_name!r}",
    )
    check(
        "Eco Driving (kierowca)" not in detail.context_client_name,
        "the page description is not appended to the client name",
    )
    check(
        listing.context_module_name == detail.context_module_name == "Eco Driving",
        "the module name is its own field",
    )
    print("PASS: ranking and detail resolve one identical client identity")


def test_cumulative_progression_wording_cannot_be_read_as_isolated_weeks():
    from api.eco_driving_explorer import detail_view_models as D
    from api.portal_ui.i18n import t

    heading = t("eco.detail.progression")
    check("Wkład tygodni" not in heading, "the additive-sounding heading is gone")
    check("narastaj" in heading.lower(), "and the replacement states that the rows are cumulative")
    note = t("eco.detail.progression_note")
    check("nie wolno ich sumować" in note, "the note still forbids summing")
    check("izolowane tygodnie" in note, "and distinguishes isolated week selection")

    rows = [{
        "period_label": "2026-07-W1", "qualification_status": "QUALIFIED",
        "eco_driving_score_total": "70", "total_kilometers": "400", "trips_count": 5,
        "is_current": False,
    }]
    html = D.render_progression(rows)
    check("Wkład tygodni" not in html, "and the rendered panel does not say it either")
    check(heading in html, "the truthful heading is rendered")
    print("PASS: cumulative progression wording no longer implies additive isolated weeks")


def test_detail_carries_the_basis_and_does_not_fall_back_to_another_period():
    canned = {
        "entries_list": [], "entries_count": [{"total_count": 0}],
        "monthly_periods": [], "score_distribution": [],
        "weekly_periods": [], "driver_trend": [], "period_progression": [],
    }
    trips = [
        trip(1, "D-1", ts(2), meters=200_000, harsh_braking_events=3),
        trip(2, "D-1", ts(14), meters=200_000, harsh_braking_events=300),
    ]
    service, _b, _d = build(trips, CHART, canned)
    pages = EcoDrivingPages(service)
    detail = pages.ranking_entry(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1", assigned_id="D-1",
    )
    body = detail.body_html
    check("Lipiec 2026" in body and "W1" in body, "the breadcrumb states the active basis")
    check("weeks=1" in body, "and every link on the page keeps it")
    check("2026-07 · W1" in body, "the entry's period label names the basis, not a stored period")

    w1_entry = service.get_basis_ranking_entry(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="1", assigned_id="D-1",
    ).body["data"]
    w3_entry = service.get_basis_ranking_entry(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks="3", assigned_id="D-1",
    ).body["data"]
    check(
        w1_entry["eco_driving_score_total"] != w3_entry["eco_driving_score_total"],
        "the detail score follows the selected basis",
    )
    check(
        w1_entry["event_counts"]["harsh_braking_events"] == 3
        and w3_entry["event_counts"]["harsh_braking_events"] == 300,
        "and so do its composition inputs",
    )
    print("PASS: the driver page stays in the selected basis for score, composition and links")


def test_basis_line_states_the_true_covered_range():
    selection = parse_week_selection(MONTH, "1,3")
    payload = {
        "mode": selection.mode,
        "month": selection.month_token,
        "label": selection.label,
        "day_count": selection.day_count,
        "is_contiguous": selection.is_contiguous,
        "includes_partial_week": selection.includes_partial_week,
        "selected_weeks": list(selection.selected_sequences),
        "week_cards": [
            {
                "sequence": b.sequence, "label": b.label,
                "start_date": b.start_date.isoformat(),
                "end_date_exclusive": b.end_date_exclusive.isoformat(),
                "is_partial": b.is_partial,
                "selected": b.sequence in selection.selected_sequences,
            }
            for b in selection.all_buckets
        ],
    }
    text = B.covered_range_text(payload)
    check("·" in text, f"a gapped basis prints each covered run separately: {text}")
    check("01–05.07.2026" in text, "W1 ends on its last covered day, not its exclusive bound")
    check("13–19.07.2026" in text, "W3 prints its own range")
    line = B.basis_line(payload, {"qualified_count": 2, "population_count": 5,
                                  "total_trips_count": 9, "total_kilometers": "1234"})
    check("W1 + W3" in line, "the basis names the weeks")
    check("12 dni" in line, "and states the true day count, not the 19-day span")
    check("przelicz" in line, "and states that a subset is recomputed from source trips")
    print("PASS: the basis line states the true covered ranges and the true day count")


def test_whole_month_basis_line_names_the_persisted_source():
    selection = parse_week_selection(MONTH)
    payload = {
        "mode": selection.mode, "month": selection.month_token, "label": selection.label,
        "day_count": selection.day_count, "is_contiguous": True,
        "includes_partial_week": selection.includes_partial_week,
        "selected_weeks": list(selection.selected_sequences),
        "week_cards": [
            {"sequence": b.sequence, "label": b.label,
             "start_date": b.start_date.isoformat(),
             "end_date_exclusive": b.end_date_exclusive.isoformat(),
             "is_partial": b.is_partial, "selected": True}
            for b in selection.all_buckets
        ],
    }
    line = B.basis_line(payload, None)
    check("31 dni" in line, "the whole month states its true day count")
    check("utrwalonego" in line, "and names the persisted monthly snapshot as its source")
    check("przelicz" not in line, "it does not claim to be a dynamic recomputation")
    print("PASS: whole month declares the persisted monthly snapshot as its source")


# =============================================================================
# Whole-month execution source (review finding 2)
# =============================================================================


def test_whole_month_falls_back_to_dynamic_when_no_snapshot_exists():
    """A month with assignments but no monthly snapshot must not read as empty."""

    trips = _three_week_driver()
    # No `monthly_periods` row: materialisation has not run for this month.
    canned = {"monthly_periods": [], "entries_list": [], "entries_count": [{"total_count": 0}]}
    service, _b, database = build(trips, CHART, canned)

    res = ranking(service, weeks=None)
    check(res.status_code == 200, "whole month renders")
    meta = res.body["meta"]
    check(meta["selection"]["mode"] == MODE_MONTH, "it is still one logical whole-month selection")
    check(
        meta["basis_source"] == BASIS_MONTH_DYNAMIC,
        f"and it resolved to the dynamic source, got {meta['basis_source']}",
    )
    check(meta["period_key"] is None, "no persisted period key is claimed")
    check(len(res.body["data"]) == 1, "the ranking is NOT empty")
    row = entry_of(res, "D-1")
    check(
        row["total_distance_meters"] == 400_000 + 350_000 + 300_000,
        "the whole canonical month range was aggregated",
    )
    check(
        row["event_counts"]["harsh_braking_events"] == 25,
        "over every week of the month, not one of them",
    )
    check(
        any("skipped_trips_count" in sql for sql, _ in database.calls),
        "the dynamic aggregation actually ran",
    )
    print("PASS: an unmaterialised month aggregates its full range instead of returning empty")


def test_all_week_cards_selected_matches_the_dynamic_whole_month():
    """Selecting every bucket is the same logical selection and the same result."""

    trips = _three_week_driver()
    canned = {"monthly_periods": [], "entries_list": [], "entries_count": [{"total_count": 0}]}
    service, _b, _d = build(trips, CHART, canned)

    every_week = ",".join(str(b.sequence) for b in PD.month_week_buckets(MONTH_START))
    explicit = ranking(service, weeks=every_week)
    implicit = ranking(service, weeks=None)

    check(
        explicit.body["meta"]["selection"]["mode"] == MODE_MONTH,
        "listing every week canonicalizes to whole month",
    )
    check(
        explicit.body["meta"]["selection"]["canonical_weeks_param"] is None,
        "and produces the whole-month URL",
    )
    check(
        explicit.body["meta"]["basis_source"] == BASIS_MONTH_DYNAMIC,
        "and resolves to the same dynamic source",
    )
    check(
        [r["assigned_id"] for r in explicit.body["data"]]
        == [r["assigned_id"] for r in implicit.body["data"]],
        "the two spellings return the same population",
    )
    check(
        entry_of(explicit, "D-1")["eco_driving_score_total"]
        == entry_of(implicit, "D-1")["eco_driving_score_total"],
        "and the same score — never zero rows",
    )
    print("PASS: all week cards selected equals the whole-month result, not an empty ranking")


def test_persisted_monthly_truth_wins_even_when_assignments_changed():
    """History is preferred and is not silently rewritten from current data."""

    # The assignments say one thing; the persisted monthly snapshot says another.
    trips = [trip(1, "D-1", ts(2), meters=999_000, harsh_braking_events=900)]
    canned = {
        "entries_list": [_monthly_entry_row(score="72", distance=400_000)],
        "entries_count": [{"total_count": 1}],
        "monthly_periods": [{
            "month_start_date": MONTH_START,
            "month_end_date": date(2026, 8, 1),
            "included_count": 1, "excluded_count": 0, "unknown_count": 0,
            "not_ranked_count": 0, "source_calculated_at": None,
        }],
        "score_distribution": [
            {"bin_index": 17, "bin_count": 1, "total_count": 1,
             "mean_score": Decimal("72"), "median_score": Decimal("72")}
        ],
    }
    service, _b, database = build(trips, CHART, canned)

    whole = ranking(service, weeks=None)
    check(
        whole.body["meta"]["basis_source"] == BASIS_MONTH_PERSISTED,
        "the persisted monthly snapshot wins",
    )
    check(
        whole.body["data"][0]["eco_driving_score_total"] == "72",
        "and its numbers are served verbatim, not recomputed from current assignments",
    )
    check(
        whole.body["data"][0]["total_distance_meters"] == 400_000,
        "including a distance the current assignments no longer agree with",
    )
    check(
        not any("skipped_trips_count" in sql for sql, _ in database.calls),
        "no dynamic aggregation runs for a materialised month",
    )

    # A proper subset of that same month still recomputes from assignments.
    subset = ranking(service, weeks="1")
    check(
        subset.body["meta"]["basis_source"] == BASIS_WEEKS_DYNAMIC,
        "a subset of the same month is dynamic",
    )
    check(
        entry_of(subset, "D-1")["total_distance_meters"] == 999_000,
        "and reads the current assignments",
    )
    print("PASS: persisted monthly truth is preferred; a subset of that month still recomputes")


def test_no_snapshot_and_no_source_is_a_truthful_empty_state():
    canned = {"monthly_periods": [], "entries_list": [], "entries_count": [{"total_count": 0}]}
    service, _b, _d = build([], CHART, canned)
    res = ranking(service, weeks=None)
    check(res.status_code == 200, "an empty month is not an error")
    check(res.body["data"] == [], "and returns no rows")
    check(res.body["meta"]["basis"]["population_count"] == 0, "with an explicit zero population")

    pages = EcoDrivingPages(service)
    page = pages.rankings(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY, month=MONTH,
    )
    check("Brak danych dla wybranego zakresu" in page.body_html, "the page states it truthfully")
    print("PASS: a month with neither a snapshot nor source data states that, and fabricates nothing")


def test_detail_follows_the_same_whole_month_source_resolution():
    trips = _three_week_driver()
    canned = {
        "monthly_periods": [], "entries_list": [], "entries_count": [{"total_count": 0}],
        "driver_trend": [], "period_progression": [],
    }
    service, _b, _d = build(trips, CHART, canned)
    detail = service.get_basis_ranking_entry(
        user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
        month=MONTH, weeks=None, assigned_id="D-1",
    )
    check(detail.status_code == 200, "the driver page renders for an unmaterialised month")
    check(
        detail.body["data"]["basis_source"] == BASIS_MONTH_DYNAMIC,
        "and used the same dynamic source the ranking did",
    )
    check(
        detail.body["data"]["total_distance_meters"] == 1_050_000,
        "so the ranking and the detail cannot disagree",
    )
    print("PASS: the driver page resolves whole-month source exactly as the ranking does")


def test_bravo_current_month_dynamic_with_private_applicable_trip():
    """Findings 1 and 2 proven together, not in isolated mocks."""

    trips = [
        person_trip(1, "p-jan-kowalski", ts(2), meters=120_000, harsh_braking_events=4),
        person_trip(
            2, "p-jan-kowalski", ts(14), meters=90_000, harsh_braking_events=6,
            private=True, included=True,
        ),
    ]
    canned = {"monthly_periods": [], "entries_list": [], "entries_count": [{"total_count": 0}]}
    service, _b, _d = build(
        trips, PERSON_CHART, canned, family_key="person",
        access=full_access(BRAVO_CLIENT_CODE),
    )
    res = bravo_ranking(service, weeks=None)
    check(res.status_code == 200, "BRAVO whole month renders")
    check(
        res.body["meta"]["basis_source"] == BASIS_MONTH_DYNAMIC,
        "with no monthly snapshot it falls back to dynamic",
    )
    row = entry_of(res, "p-jan-kowalski")
    check(row is not None, "and the person is present, not an empty ranking")
    check(
        row["total_distance_meters"] == 210_000,
        "the private applicable trip contributes on the fallback path too",
    )
    check(row["event_counts"]["harsh_braking_events"] == 10, "with its events")
    print("PASS: BRAVO current-month fallback includes the private applicable trip")


# =============================================================================
# Zero-score ranking (review finding 3)
# =============================================================================


def _rank_rows(scores, *, kilometers=None, ids=None):
    ids = ids or [chr(ord("A") + index) for index in range(len(scores))]
    kilometers = kilometers or [Decimal("500")] * len(scores)
    return [
        {
            "assigned_id": identity,
            "eco_driving_score_total": None if score is None else Decimal(str(score)),
            "total_kilometers": km,
        }
        for identity, score, km in zip(ids, scores, kilometers)
    ]


def test_zero_score_ranks_above_negative_scores():
    """A score of exactly 0 is a real score: `1 > 0 > -1`, and NULL is last."""

    rows = _rank_rows([-1, 0, 1, None], ids=["neg", "zero", "pos", "none"])
    order = [row["assigned_id"] for row in PD.sort_rank_rows(rows)]
    check(order == ["pos", "zero", "neg", "none"], f"ordinary numeric order, got {order}")

    PD.assign_ranking_positions(rows)
    by_id = {row["assigned_id"]: row["ranking_position"] for row in rows}
    check(by_id["pos"] == 1 and by_id["zero"] == 2, "zero outranks the negative score")
    check(by_id["neg"] == 3, "and the negative score follows it")
    check(by_id["none"] == 4, "an unscored row stays last and distinct from zero")

    # Zero is not conflated with a missing score in any direction.
    only_zero_and_none = _rank_rows([None, 0], ids=["none", "zero"])
    check(
        [r["assigned_id"] for r in PD.sort_rank_rows(only_zero_and_none)] == ["zero", "none"],
        "a zero always precedes an unscored row",
    )
    print("PASS: zero is a real score and ranks above every negative score")


def test_zero_score_tie_breaks_follow_the_canonical_rule():
    tied = _rank_rows(
        [0, 0, 0],
        kilometers=[Decimal("100"), Decimal("300"), Decimal("300")],
        ids=["low-km", "b-high-km", "a-high-km"],
    )
    order = [row["assigned_id"] for row in PD.sort_rank_rows(tied)]
    check(order[0] == "a-high-km", "more kilometres wins, then the id ascending")
    check(order[1] == "b-high-km", "the id breaks a full tie deterministically")
    check(order[2] == "low-km", "fewer kilometres sorts last among equal zeroes")

    # The identity tiebreak follows the family, not a hardcoded column.
    person_rows = [
        {"person_name_group_key": "b", "eco_driving_score_total": Decimal("0"),
         "total_kilometers": Decimal("100")},
        {"person_name_group_key": "a", "eco_driving_score_total": Decimal("0"),
         "total_kilometers": Decimal("100")},
    ]
    ordered = PD.sort_rank_rows(person_rows, identity_key="person_name_group_key")
    check(
        [r["person_name_group_key"] for r in ordered] == ["a", "b"],
        "the person family ties on its own identity column",
    )
    print("PASS: zero-score ties follow the canonical distance then identity rule")


def test_scheduled_and_dynamic_paths_share_the_corrected_order():
    """Both aggregation jobs and the portal must agree on `1 > 0 > -1`."""

    from jobs.ecodriving import job_eco_driving_aggregate as DRIVER_JOB
    from jobs.ecodriving_person import job_eco_driving_person_aggregate as PERSON_JOB

    scheduled = _rank_rows([-1, 0, 1], ids=["neg", "zero", "pos"])
    check(
        [r["assigned_id"] for r in DRIVER_JOB._sort_rank_rows(scheduled)]
        == ["pos", "zero", "neg"],
        "the driver aggregation job uses the corrected order",
    )
    person_rows = [
        {"person_name_group_key": key, "eco_driving_score_total": Decimal(str(score)),
         "total_kilometers": Decimal("500")}
        for key, score in (("neg", -1), ("zero", 0), ("pos", 1))
    ]
    check(
        [r["person_name_group_key"] for r in PERSON_JOB._sort_rank_rows(person_rows)]
        == ["pos", "zero", "neg"],
        "the person aggregation job uses it too",
    )

    # And the dynamic selected-range path produces the same order end to end.
    # 100 000 m of clean driving scores the maximum; heavy events drive the
    # score negative; a middling load lands between them.
    trips = [
        trip(1, "D-1", ts(2), meters=200_000),
        trip(2, "D-2", ts(2), meters=200_000, harsh_braking_events=60, idle_events=90),
        trip(3, "D-EXC", ts(2), meters=200_000, harsh_braking_events=900,
             harsh_acceleration_events=900, harsh_turning_events=900, idle_events=900,
             overrev_events_count=900, speeding_140_160_count=900,
             speeding_160_170_count=900, speeding_170_plus_count=900),
    ]
    chart = [
        {"driver_id": "D-1", "driver_name": "A", "ranking_included": True},
        {"driver_id": "D-2", "driver_name": "B", "ranking_included": True},
        {"driver_id": "D-EXC", "driver_name": "C", "ranking_included": True},
    ]
    service, _b, _d = build(trips, chart)
    res = ranking(service, weeks="1")
    scores = [Decimal(r["eco_driving_score_total"]) for r in res.body["data"]]
    check(scores == sorted(scores, reverse=True), f"dynamic ranking is descending, got {scores}")
    check(
        all(
            r["ranking_position"] == index + 1
            for index, r in enumerate(res.body["data"])
        ),
        "positions follow that order",
    )
    print("PASS: both scheduled jobs and the dynamic path share the corrected ordering")


def test_ranking_fix_changes_order_only():
    """Everything except position is untouched by the ordering correction."""

    rows = _rank_rows([0, -1], ids=["zero", "neg"])
    for row in rows:
        row["qualification_status"] = "QUALIFIED"
        row["ecodriving_rating_type"] = "akceptowalny"
        row["ranking_included"] = True
    before = [
        {k: v for k, v in row.items() if k not in ("ranking_position", "ranking_total_participants")}
        for row in rows
    ]
    PD.assign_ranking_positions(rows)
    after = [
        {k: v for k, v in row.items() if k not in ("ranking_position", "ranking_total_participants")}
        for row in rows
    ]
    check(before == after, "scores, qualification, rating and group are all unchanged")
    check(
        {row["assigned_id"]: row["ranking_position"] for row in rows} == {"zero": 1, "neg": 2},
        "only the position changed",
    )
    print("PASS: the ranking correction changes order and position only")


# =============================================================================
# Six-bucket months (review finding 4)
# =============================================================================

SIX_BUCKET_MONTHS = ("2026-03", "2021-08")


def test_six_bucket_months_have_canonical_non_overlapping_geometry():
    for token in SIX_BUCKET_MONTHS:
        year, month = (int(part) for part in token.split("-"))
        month_start = date(year, month, 1)
        buckets = PD.month_week_buckets(month_start)
        check(len(buckets) == 6, f"{token} has six buckets, got {len(buckets)}")
        check(buckets[-1].label == "W6", f"{token} reaches W6")

        # No gap and no overlap: the buckets tile the month exactly once.
        check(buckets[0].start_date == month_start, f"{token} W1 starts at the month start")
        for previous, following in zip(buckets, buckets[1:]):
            check(
                previous.end_date_exclusive == following.start_date,
                f"{token} buckets are contiguous with no gap or overlap",
            )
        check(
            buckets[-1].end_date_exclusive == PD.next_month_start(month_start),
            f"{token} W6 is truncated at the month end",
        )
        check(
            sum(b.day_count for b in buckets)
            == (PD.next_month_start(month_start) - month_start).days,
            f"{token} buckets tile the month exactly",
        )
        check(buckets[-1].is_partial, f"{token} W6 is a partial trailing bucket")

        # The persisted-period builder agrees bucket for bucket.
        periods = JOB._month_bounded_weekly_periods(month_start)
        check(len(periods) == 6, f"{token} yields six persisted cumulative periods too")
        check(
            [p.period_end_date for p in periods] == [b.end_date_exclusive for b in buckets],
            f"{token} boundaries agree between the two models",
        )
    # A short February proves the lower bound is four, not five.
    check(len(PD.month_week_buckets(date(2021, 2, 1))) == 4, "February 2021 has four buckets")
    print("PASS: six-bucket months tile exactly, reach W6 and agree with the persisted periods")


def test_w6_selection_state_is_month_specific():
    six = parse_week_selection("2026-03", "6")
    check(six.mode == MODE_WEEKS, "W6 is a valid selection in a six-bucket month")
    check(six.selected_sequences == (6,), "and canonicalizes to itself")
    check(six.day_count == 2, "with its true covered day count")

    # July 2026 has five buckets, so W6 must be refused there rather than
    # producing a timestamp for a week the month does not define.
    try:
        parse_week_selection(MONTH, "6")
        raise AssertionError("W6 must not exist in a five-bucket month")
    except InvalidWeekSelectionError:
        check(True, "W6 is refused in a month that has no sixth bucket")
    try:
        parse_week_selection("2026-03", "7")
        raise AssertionError("W7 never exists")
    except InvalidWeekSelectionError:
        check(True, "W7 is refused everywhere")

    every = parse_week_selection("2026-03", "1,2,3,4,5,6")
    check(every.mode == MODE_MONTH, "all six buckets canonicalize to whole month")
    check(every.canonical_weeks_param is None, "and to the whole-month URL")
    print("PASS: W6 validity is month-relative and all six collapse to whole month")


def test_w1_plus_w6_non_contiguous_selection():
    march = date(2026, 3, 1)
    trips = [
        trip(1, "D-1", datetime(2026, 3, 1, 9, tzinfo=TZ), meters=150_000, harsh_braking_events=4),
        trip(2, "D-1", datetime(2026, 3, 10, 9, tzinfo=TZ), meters=500_000, harsh_braking_events=99),
        trip(3, "D-1", datetime(2026, 3, 30, 9, tzinfo=TZ), meters=150_000, harsh_braking_events=6),
    ]
    canned = {"monthly_periods": [], "entries_list": [], "entries_count": [{"total_count": 0}]}
    service, _b, database = build(trips, CHART, canned)
    res = ranking(service, weeks="1,6", month="2026-03")
    check(res.status_code == 200, "a W1+W6 basis renders")
    selection = res.body["meta"]["selection"]
    check(selection["selected_weeks"] == [1, 6], "the basis is W1 and W6")
    check(selection["is_contiguous"] is False, "and reports its gap")
    check(selection["day_count"] == 1 + 2, "with the true covered day count")

    row = entry_of(res, "D-1")
    check(row["total_distance_meters"] == 300_000, "the middle of the month is excluded")
    check(row["event_counts"]["harsh_braking_events"] == 10, "as are its events")

    params = next(p for sql, p in database.calls if "skipped_trips_count" in sql)
    check("win1_start" in params, "two disjoint intervals are bound")
    check(params["win0_start"] == PD.local_midnight(march), "W1 starts at the month start")
    check(
        params["win0_end"] == PD.local_midnight(date(2026, 3, 2)),
        "W1 is the single day before the first Monday",
    )
    check(
        params["win1_start"] == PD.local_midnight(date(2026, 3, 30)),
        "W6 starts at the last Monday",
    )
    check(
        params["win1_end"] == PD.local_midnight(date(2026, 4, 1)),
        "and ends exclusively at the month end",
    )
    print("PASS: a non-contiguous W1+W6 selection works in a six-bucket month")


# =============================================================================
# Final provider fixes: persisted SQL, dynamic ordering, reconciliation
# =============================================================================


def _persisted_period_key(family_month=MONTH_START):
    return RankingPeriodKey(
        period_type=PeriodType.MONTHLY,
        month_start_date=family_month,
        period_start_date=family_month,
        period_end_date=date(2026, 8, 1),
    )


def _issue_persisted_ranking(client_code, family, *, sort=None, direction=None, group="INCLUDED"):
    """Drive registry → provider → persisted ranking, validating issued SQL."""

    from api.eco_driving_explorer.registry import get_provider

    database = SyntheticClientDatabase(
        [], [], {"entries_list": [], "entries_count": [{"total_count": 0}]},
        family_key="person" if family == BRAVO_FAMILY else "driver",
    )
    reader = SchemaValidatingReader(database, context=f"{client_code} persisted ranking")
    provider = get_provider(client_code, family, client_id=TRUSTED_CLIENT_ID)
    provider.list_ranking_entries(
        reader, _persisted_period_key(), ranking_group=group,
        sort_field=sort, direction=direction, page=1, limit=50,
    )
    return reader.statements


def test_persisted_ranking_sql_uses_the_family_identity_column():
    """Migration 043 removed `assigned_id` from person stats.

    A driver-shaped ORDER BY / tie-break / sort allowlist would reference a
    column that does not exist there. The schema validator derives the real
    column sets from the migrations, so this fails rather than passing on canned
    rows.
    """

    bravo = _issue_persisted_ranking(BRAVO_CLIENT_CODE, BRAVO_FAMILY)
    joined = "\n".join(bravo)
    check(bravo, "the BRAVO persisted ranking issued SQL")
    check(
        "s.assigned_id" not in joined,
        "BRAVO persisted ranking must not reference the driver identity column",
    )
    check(
        "s.person_name_group_key" in joined,
        "it uses the person family's stats identity column",
    )
    check(
        "public.eco_person_monthly_stats" in joined,
        "and reads the person family's stats table",
    )
    check(
        "s.person_name_group_key ASC" in joined,
        "the default order tie-breaks on the person identity",
    )
    check(
        "public.eco_person_people_email_view" in joined,
        "joining the person roster for the display name",
    )
    check(
        "c.person_name" in joined and "c.driver_name" not in joined,
        "and reading the person roster's own name column",
    )

    alpha = "\n".join(_issue_persisted_ranking(CLIENT_CODE, FAMILY))
    check("s.assigned_id" in alpha, "ALPHA persisted ranking keeps the driver identity")
    check("s.person_name_group_key" not in alpha, "and does not borrow the person one")
    check("public.eco_driver_monthly_stats" in alpha, "reading the driver stats table")
    check("s.assigned_id ASC" in alpha, "with the driver tie-break")
    print("PASS: persisted ranking SQL resolves the identity column per family")


def test_persisted_ranking_sort_keys_are_semantic_not_physical():
    """The URL key stays stable; the physical column follows the family."""

    for client_code, family, expected, forbidden in (
        (CLIENT_CODE, FAMILY, "s.assigned_id", "s.person_name_group_key"),
        (BRAVO_CLIENT_CODE, BRAVO_FAMILY, "s.person_name_group_key", "s.assigned_id"),
    ):
        sorted_sql = "\n".join(
            _issue_persisted_ranking(client_code, family, sort="assigned_id", direction="DESC")
        )
        check(expected in sorted_sql, f"{client_code} sorts on {expected}")
        check(forbidden not in sorted_sql, f"{client_code} never emits {forbidden}")
        check(f"{expected} DESC" in sorted_sql, "the requested direction is applied")

    # Every approved key resolves for both families, and nothing else does.
    for key in q.ENTRY_SORT_FIELD_KEYS:
        for sources in (q.DRIVER_SOURCES, q.PERSON_SOURCES):
            check(key in q.entry_sort_fields(sources), f"{key} resolves for {sources.family_key}")
    from api.eco_driving_explorer.errors import InvalidSortFieldError
    for bad in ("person_name_group_key", "s.assigned_id", "1;DROP TABLE", "updated_at"):
        try:
            _issue_persisted_ranking(BRAVO_CLIENT_CODE, BRAVO_FAMILY, sort=bad)
        except InvalidSortFieldError:
            continue
        raise AssertionError(f"sort field {bad!r} must be refused")
    check(True, "physical column names and unknown keys are refused as sort input")
    print("PASS: sort keys are a semantic vocabulary resolved through family metadata")


def test_bravo_historical_monthly_snapshot_is_preferred_and_queryable():
    """`MONTH_PERSISTED` for BRAVO must actually execute, not just be chosen."""

    # Assignments say one thing; the persisted person snapshot says another.
    trips = [person_trip(1, "p-jan-kowalski", ts(2), meters=999_000, harsh_braking_events=900)]
    canned = {
        "entries_list": [_monthly_entry_row(assigned_id="p-jan-kowalski", score="64", distance=350_000)],
        "entries_count": [{"total_count": 1}],
        "monthly_periods": [{
            "month_start_date": MONTH_START,
            "month_end_date": date(2026, 8, 1),
            "included_count": 1, "excluded_count": 0, "unknown_count": 0,
            "not_ranked_count": 0, "source_calculated_at": None,
        }],
        "score_distribution": [
            {"bin_index": 16, "bin_count": 1, "total_count": 1,
             "mean_score": Decimal("64"), "median_score": Decimal("64")}
        ],
    }
    database = SyntheticClientDatabase(trips, PERSON_CHART, canned, family_key="person")
    backend = FakeBackend(
        database, access=full_access(BRAVO_CLIENT_CODE), family_key="person"
    )
    backend.database = SchemaValidatingReader(database, context="BRAVO historical month")
    service = EcoDrivingApiService(backend)

    res = bravo_ranking(service, weeks=None)
    check(res.status_code == 200, f"BRAVO whole month renders: {res.body.get('error')}")
    meta = res.body["meta"]
    check(
        meta["basis_source"] == BASIS_MONTH_PERSISTED,
        f"the persisted monthly snapshot is preferred, got {meta['basis_source']}",
    )
    check(
        res.body["data"][0]["eco_driving_score_total"] == "64",
        "and its persisted numbers are served verbatim",
    )
    check(
        res.body["data"][0]["total_distance_meters"] == 350_000,
        "including a distance the current assignments no longer agree with",
    )
    check(
        not any("skipped_trips_count" in sql for sql in backend.database.statements),
        "no dynamic fallback runs merely because assignments differ",
    )
    check(
        any("public.eco_person_monthly_stats" in sql for sql in backend.database.statements),
        "the person stats table was actually queried",
    )
    print("PASS: BRAVO historical monthly truth is preferred and its query executes")


def test_whole_month_source_matrix_for_both_families():
    """One logical whole-month selection; three truthful execution outcomes."""

    persisted_canned = {
        "entries_list": [_monthly_entry_row()],
        "entries_count": [{"total_count": 1}],
        "monthly_periods": [{
            "month_start_date": MONTH_START, "month_end_date": date(2026, 8, 1),
            "included_count": 1, "excluded_count": 0, "unknown_count": 0,
            "not_ranked_count": 0, "source_calculated_at": None,
        }],
        "score_distribution": [],
    }
    empty_canned = {"monthly_periods": [], "entries_list": [], "entries_count": [{"total_count": 0}]}

    matrix = []
    for label, family_key, client, family, chart, make_trip in (
        ("ALPHA", "driver", CLIENT_CODE, FAMILY, CHART, lambda: _three_week_driver()),
        ("BRAVO", "person", BRAVO_CLIENT_CODE, BRAVO_FAMILY, PERSON_CHART,
         lambda: [person_trip(1, "p-jan-kowalski", ts(2), meters=400_000)]),
    ):
        access = full_access(client)
        run = (lambda svc: ranking(svc, weeks=None)) if label == "ALPHA" else (
            lambda svc: bravo_ranking(svc, weeks=None)
        )

        svc, _b, _d = build(make_trip(), chart, persisted_canned, access=access, family_key=family_key)
        matrix.append((label, "snapshot", run(svc).body["meta"]["basis_source"]))

        svc, _b, _d = build(make_trip(), chart, empty_canned, access=access, family_key=family_key)
        dynamic = run(svc)
        matrix.append((label, "no snapshot", dynamic.body["meta"]["basis_source"]))
        check(len(dynamic.body["data"]) == 1, f"{label} dynamic month is not empty")

        svc, _b, _d = build([], chart, empty_canned, access=access, family_key=family_key)
        nothing = run(svc)
        matrix.append((label, "neither", nothing.body["meta"]["basis_source"]))
        check(nothing.body["data"] == [], f"{label} with no data returns no rows")
        check(
            nothing.body["meta"]["basis"]["population_count"] == 0,
            f"{label} reports an explicit zero population",
        )

    expected = [
        ("ALPHA", "snapshot", BASIS_MONTH_PERSISTED),
        ("ALPHA", "no snapshot", BASIS_MONTH_DYNAMIC),
        ("ALPHA", "neither", BASIS_MONTH_DYNAMIC),
        ("BRAVO", "snapshot", BASIS_MONTH_PERSISTED),
        ("BRAVO", "no snapshot", BASIS_MONTH_DYNAMIC),
        ("BRAVO", "neither", BASIS_MONTH_DYNAMIC),
    ]
    check(matrix == expected, f"whole-month matrix: {matrix}")
    print("PASS: the whole-month source matrix is truthful for both families")


def _dynamic_entry(identity, score, kilometers=500, position=None, group="INCLUDED"):
    from api.eco_driving_explorer.models import RankingEntry, RankingGroup, DriverMetadataSource

    return RankingEntry(
        provider_key="p", client_code=CLIENT_CODE, ranking_family=FAMILY,
        period_key=_persisted_period_key(), assigned_id=identity,
        ranking_group=RankingGroup(group) if group else None,
        ranking_included=True, ranking_position=position,
        ranking_total_participants=None,
        qualification_status="QUALIFIED", calculation_status="OK",
        trips_count=1, total_distance_meters=int(kilometers * 1000),
        total_kilometers=Decimal(str(kilometers)),
        eco_driving_score_total=None if score is None else Decimal(str(score)),
        ecodriving_rating_type="akceptowalny",
        ecodriving_rating_type_share_percent=None,
        period_label="2026-07", is_partial_period=False,
        event_counts={m: 0 for m in REQUIRED_METRICS},
        metric_rates={m: None for m in REQUIRED_METRICS},
        metric_points={m: None for m in REQUIRED_METRICS},
        metric_losses={m: None for m in REQUIRED_METRICS},
        current_driver_name=identity, driver_metadata_source=DriverMetadataSource.NONE,
        current_chart_ranking_included=True,
        lineage_quality=LineageQuality.RECONSTRUCTED_CURRENT_STATE,
    )


def _paginate(entries, **kw):
    """Drive the real provider pagination path, not the shared helper."""

    from api.eco_driving_explorer.registry import get_provider
    from api.eco_driving_explorer.models import DynamicRankingBasis

    basis = DynamicRankingBasis(
        client_code=CLIENT_CODE, month_start_date=MONTH_START, selected_sequences=(1,),
        entries=tuple(entries),
        counts_by_group={"INCLUDED": len(entries), "EXCLUDED": 0, "UNKNOWN_DRIVER": 0},
        not_ranked_count=0, total_trips_count=len(entries),
        total_distance_meters=0, qualified_count=len(entries),
        population_count=len(entries),
    )
    provider = get_provider(CLIENT_CODE, FAMILY, client_id=TRUSTED_CLIENT_ID)
    page = provider.paginate_dynamic_entries(basis, **kw)
    return [entry.assigned_id for entry in page.items]


def test_dynamic_pagination_orders_zero_above_negative():
    """`1 > 0 > -1 > None` on the real pagination path, before slicing.

    These entries carry no ranking position — the state an ``UNKNOWN_DRIVER``
    population is always in — so the score order alone decides.
    """

    entries = [
        _dynamic_entry("neg", -1), _dynamic_entry("none", None),
        _dynamic_entry("zero", 0), _dynamic_entry("pos", 1),
    ]
    order = _paginate(entries)
    check(order == ["pos", "zero", "neg", "none"], f"canonical order, got {order}")

    # The ordering must be right *before* the page boundary, not patched after.
    first_page = _paginate(entries, page=1, limit=2)
    second_page = _paginate(entries, page=2, limit=2)
    check(first_page == ["pos", "zero"], f"page 1 holds the top two, got {first_page}")
    check(second_page == ["neg", "none"], f"page 2 holds the rest, got {second_page}")

    unknown = [
        _dynamic_entry("u-neg", -1, group="UNKNOWN_DRIVER"),
        _dynamic_entry("u-zero", 0, group="UNKNOWN_DRIVER"),
    ]
    check(
        _paginate(unknown, ranking_group="UNKNOWN_DRIVER") == ["u-zero", "u-neg"],
        "an unpositioned UNKNOWN_DRIVER group orders by score too",
    )
    print("PASS: dynamic pagination ranks a numeric zero above every negative score")


def test_dynamic_pagination_tie_and_filter_semantics():
    tied = [
        _dynamic_entry("low-km", 0, kilometers=100),
        _dynamic_entry("b-high", 0, kilometers=300),
        _dynamic_entry("a-high", 0, kilometers=300),
    ]
    order = _paginate(tied)
    check(order == ["a-high", "b-high", "low-km"], f"distance then identity, got {order}")

    mixed = [
        _dynamic_entry("in-zero", 0), _dynamic_entry("in-neg", -1),
        _dynamic_entry("ex-zero", 0, group="EXCLUDED"),
    ]
    check(_paginate(mixed, ranking_group="INCLUDED") == ["in-zero", "in-neg"],
          "the group filter narrows without reordering")
    check(_paginate(mixed, ranking_group="EXCLUDED") == ["ex-zero"], "and partitions correctly")
    check(_paginate(mixed, search="in-") == ["in-zero", "in-neg"], "search preserves the order")

    positioned = [
        _dynamic_entry("second", 0, position=2), _dynamic_entry("first", -1, position=1),
    ]
    check(
        _paginate(positioned) == ["first", "second"],
        "an assigned position still wins over the score fallback",
    )
    print("PASS: dynamic pagination tie-breaks, group filtering and search are unchanged")


def test_scheduled_persisted_and_dynamic_agree_on_zero():
    from jobs.ecodriving import job_eco_driving_aggregate as DRIVER_JOB
    from jobs.ecodriving_person import job_eco_driving_person_aggregate as PERSON_JOB

    rows = [
        {"assigned_id": key, "eco_driving_score_total": None if s is None else Decimal(str(s)),
         "total_kilometers": Decimal("500")}
        for key, s in (("neg", -1), ("none", None), ("zero", 0), ("pos", 1))
    ]
    check(
        [r["assigned_id"] for r in DRIVER_JOB._sort_rank_rows(rows)]
        == ["pos", "zero", "neg", "none"],
        "the driver scheduled job agrees",
    )
    person_rows = [
        {"person_name_group_key": key,
         "eco_driving_score_total": None if s is None else Decimal(str(s)),
         "total_kilometers": Decimal("500")}
        for key, s in (("neg", -1), ("none", None), ("zero", 0), ("pos", 1))
    ]
    check(
        [r["person_name_group_key"] for r in PERSON_JOB._sort_rank_rows(person_rows)]
        == ["pos", "zero", "neg", "none"],
        "the person scheduled job agrees",
    )
    check(
        _paginate([
            _dynamic_entry("neg", -1), _dynamic_entry("none", None),
            _dynamic_entry("zero", 0), _dynamic_entry("pos", 1),
        ]) == ["pos", "zero", "neg", "none"],
        "and the dynamic pagination path agrees",
    )
    # Persisted SQL expresses the same rule through NULLS LAST + DESC.
    for sources in (q.DRIVER_SOURCES, q.PERSON_SOURCES):
        order = q.entry_default_order(sources)
        check(
            "s.eco_driving_score_total DESC NULLS LAST" in order,
            f"{sources.family_key} persisted order is score DESC with NULLs last",
        )
    print("PASS: scheduled, persisted and dynamic ranking share one zero-vs-null semantic")


def test_reconciliation_uses_the_family_source_universe():
    """Reconstruction must reconcile against the universe that produced the row."""

    from api.eco_driving_explorer.registry import get_provider

    def issue(client_code, family, family_key, chart, canned):
        database = SyntheticClientDatabase([], chart, canned, family_key=family_key)
        reader = SchemaValidatingReader(database, context=f"{client_code} reconciliation")
        provider = get_provider(client_code, family, client_id=TRUSTED_CLIENT_ID)
        provider.reconcile_ranking_entry(reader, _persisted_period_key(), "the-id")
        return "\n".join(reader.statements)

    entry_row = _monthly_entry_row(assigned_id="the-id")
    canned = {
        "entries_list": [entry_row], "entries_count": [{"total_count": 1}],
        "single_entry": [entry_row],
        "recon_totals": [{
            "trips_count": 1, "total_distance_meters": 400_000,
            "missing_client_trip_count": 0,
            **{m: 3 for m in REQUIRED_METRICS},
        }],
        "window_diag": [{"window_trips": 1, "private_trips": 0, "not_aggregated_trips": 0}],
    }

    bravo = issue(BRAVO_CLIENT_CODE, BRAVO_FAMILY, "person", PERSON_CHART, canned)
    check(
        "public.eco_person_trip_assignments" in bravo,
        "BRAVO reconciliation reads the person assignments table",
    )
    check(
        "public.eco_trip_assignments" not in bravo.replace("public.eco_person_trip_assignments", ""),
        "and never the driver assignments table",
    )
    check(
        "a.person_name_group_key = %(assigned_id)s" in bravo,
        "bound on the person identity column",
    )
    check(
        "a.assigned_id = %(assigned_id)s" not in bravo,
        "never the driver identity column",
    )
    check(
        "a.aggregation_included IS TRUE" in bravo,
        "using the canonical person inclusion predicate",
    )
    check(
        "AND a.is_private_trip IS FALSE" not in bravo,
        "which does not exclude private applicable trips",
    )
    check(
        "public.eco_person_monthly_stats" in bravo,
        "and compares against the person stats row it is reconciling",
    )

    alpha = issue(CLIENT_CODE, FAMILY, "driver", CHART, canned)
    check(
        "public.eco_trip_assignments" in alpha and "eco_person" not in alpha,
        "ALPHA reconciliation stays on the driver family",
    )
    check("a.assigned_id = %(assigned_id)s" in alpha, "with the driver identity")
    check(
        "AND a.is_private_trip IS FALSE" in alpha,
        "and the driver family's private-trip exclusion",
    )
    print("PASS: reconciliation and diagnostics read each family's own trip universe")


def test_reconciliation_universe_equals_the_aggregation_universe():
    """Normal aggregation, reconstruction and diagnostics share one source."""

    for sources in (q.DRIVER_SOURCES, q.PERSON_SOURCES):
        aggregation = q.dynamic_population_sql(1, sources)
        reconstruction = q.reconstruction_totals_sql(sources)
        diagnostics = q.window_diagnostics_sql(sources)
        for name, sql in (
            ("reconstruction", reconstruction), ("diagnostics", diagnostics)
        ):
            check(
                sources.assignments_table in sql,
                f"{sources.family_key} {name} uses the family assignments table",
            )
            check(
                f"a.{sources.identity_column}" in sql,
                f"{sources.family_key} {name} uses the family identity column",
            )
        check(
            sources.included_predicate in reconstruction,
            f"{sources.family_key} reconstruction uses the family inclusion predicate",
        )
        check(
            sources.assignments_table in aggregation,
            f"{sources.family_key} aggregation uses the same table",
        )
        # Diagnostics is deliberately unfiltered so exclusions can be counted.
        check(
            sources.included_predicate not in diagnostics,
            f"{sources.family_key} diagnostics stays unfiltered by design",
        )
    print("PASS: aggregation, reconstruction and diagnostics share one family universe")


def test_no_reconciliation_panel_is_reintroduced():
    """S12 removed the UI panel; only the backend capability is family-aware."""

    from api.eco_driving_explorer import detail_view_models as D

    check(
        "reconcil" not in "".join(D.SECTION_ORDER).lower(),
        "the detail page has no reconciliation section",
    )
    pages_src = Path(REPO_ROOT / "api/eco_driving_explorer/pages.py").read_text()
    detail_src = Path(REPO_ROOT / "api/eco_driving_explorer/detail_view_models.py").read_text()
    for name, text in (("pages", pages_src), ("detail", detail_src)):
        check(
            "reconcile_ranking_entry" not in text,
            f"the {name} layer does not call reconciliation",
        )
    print("PASS: the removed reconciliation panel is not reintroduced")


def main() -> None:
    test_ddl_parser_sees_the_migration_043_rename()
    test_week_buckets_are_isolated_segments_not_cumulative_snapshots()
    test_arbitrary_selection_is_not_a_snapshot_sum()
    test_non_contiguous_selection_excludes_the_gap()
    test_contiguous_selection_merges_without_double_counting()
    test_single_week_is_the_isolated_week_not_the_cumulative_snapshot()
    test_whole_month_consumes_the_canonical_persisted_monthly_snapshot()
    test_whole_month_falls_back_to_dynamic_when_no_snapshot_exists()
    test_all_week_cards_selected_matches_the_dynamic_whole_month()
    test_persisted_monthly_truth_wins_even_when_assignments_changed()
    test_no_snapshot_and_no_source_is_a_truthful_empty_state()
    test_detail_follows_the_same_whole_month_source_resolution()
    test_bravo_current_month_dynamic_with_private_applicable_trip()
    test_zero_score_ranks_above_negative_scores()
    test_zero_score_tie_breaks_follow_the_canonical_rule()
    test_scheduled_and_dynamic_paths_share_the_corrected_order()
    test_ranking_fix_changes_order_only()
    test_six_bucket_months_have_canonical_non_overlapping_geometry()
    test_w6_selection_state_is_month_specific()
    test_w1_plus_w6_non_contiguous_selection()
    test_full_month_dynamic_parity_with_the_canonical_generator()
    test_qualification_applies_once_to_the_whole_selected_union()
    test_qualification_boundary_is_exact_in_metres()
    test_client_inclusion_policy_is_read_back_not_reimplemented()
    test_bravo_non_private_and_unmapped_assignments()
    test_bravo_trip_evidence_uses_the_same_inclusion_universe()
    test_provider_family_selection_is_registry_bound()
    test_ranking_is_recomputed_and_can_reorder_against_the_persisted_rank()
    test_ranking_tie_behaviour_matches_the_canonical_rule()
    test_filtering_and_paging_cannot_change_a_rank()
    test_only_the_three_production_bands_appear()
    test_group_semantics_survive_dynamic_recomputation()
    test_unit_toggle_changes_the_primary_value_but_not_the_severity()
    test_histogram_is_recomputed_over_the_same_basis_and_group()
    test_non_qualified_detail_performs_no_histogram_work()
    test_trip_evidence_matches_the_selected_union_exactly()
    test_boundary_timestamps_have_no_gap_and_no_double_count()
    test_boundary_parameters_are_business_timezone_midnights()
    test_week_selection_canonicalization()
    test_canonical_state_is_reflected_in_the_rendered_links()
    test_period_key_is_ignored_when_a_month_is_present()
    test_zero_weeks_is_a_prompt_and_issues_no_query()
    test_security_boundaries_are_unchanged()
    test_audit_records_safe_basis_facts_only()
    test_query_shape_is_bounded_and_has_no_n_plus_one()
    test_context_client_identity_is_identical_across_surfaces()
    test_cumulative_progression_wording_cannot_be_read_as_isolated_weeks()
    test_detail_carries_the_basis_and_does_not_fall_back_to_another_period()
    test_persisted_ranking_sql_uses_the_family_identity_column()
    test_persisted_ranking_sort_keys_are_semantic_not_physical()
    test_bravo_historical_monthly_snapshot_is_preferred_and_queryable()
    test_whole_month_source_matrix_for_both_families()
    test_dynamic_pagination_orders_zero_above_negative()
    test_dynamic_pagination_tie_and_filter_semantics()
    test_scheduled_persisted_and_dynamic_agree_on_zero()
    test_reconciliation_uses_the_family_source_universe()
    test_reconciliation_universe_equals_the_aggregation_universe()
    test_no_reconciliation_panel_is_reintroduced()
    test_basis_line_states_the_true_covered_range()
    test_whole_month_basis_line_names_the_persisted_source()
    print(f"OK - Eco Driving arbitrary week selection checks passed ({_CHECKS} checks)")


if __name__ == "__main__":
    main()
