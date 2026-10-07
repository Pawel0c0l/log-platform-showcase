"""SQL definitions, allowlists, and the read-only client-database reader.

All SQL is fixed and repository-controlled. Values are always passed as bound
parameters. Sort fields and directions come from explicit allowlists. Table and
schema names are never taken from the caller. Every business-data query is
scoped by ``client_id``.

The per-100km rate and kilometre rounding mirror
``jobs/ecodriving/job_eco_driving_aggregate.py`` (``KILOMETERS``/``RAW_RATE``/
``STORED_RATE`` quantums) so reconciliation matches the persisted aggregation.
Scoring thresholds are not duplicated here; they are reused from
``jobs.ecodriving.eco_scoring``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Optional, Protocol, Sequence

if __package__.startswith("api."):
    from ..timezone_utils import get_business_timezone
else:
    from timezone_utils import get_business_timezone
from .errors import (
    InvalidPaginationError,
    InvalidSortFieldError,
)
from .models import (
    EVENT_METRIC_COLUMNS,
    RankingGroup,
    SortDirection,
)

# --- Fixed, repository-controlled object names (never caller-supplied) -------

WEEKLY_STATS_TABLE = "public.eco_driver_weekly_stats"
MONTHLY_STATS_TABLE = "public.eco_driver_monthly_stats"
ASSIGNMENTS_TABLE = "public.eco_trip_assignments"
CLIENT_TRIPS_TABLE = "public.client_trips"
DRIVER_CHART_TABLE = "public.eco_drivers_id_chart"

PERSON_WEEKLY_STATS_TABLE = "public.eco_person_weekly_stats"
PERSON_MONTHLY_STATS_TABLE = "public.eco_person_monthly_stats"
PERSON_ASSIGNMENTS_TABLE = "public.eco_person_trip_assignments"
PERSON_ROSTER_VIEW = "public.eco_person_people_email_view"


# --- Ranking families --------------------------------------------------------
#
# Two production Eco pipelines exist, and they are NOT the same data contract:
#
#   * the **driver** family (`jobs/ecodriving/job_eco_driving_aggregate.py`)
#     keys on the opaque `assigned_id` and writes
#     `aggregation_included = (assigned_id IS NOT NULL AND is_private_trip IS FALSE)`,
#     so a private trip can never be aggregated. `028`'s
#     `chk_eco_trip_assignments_private_trip_exclusion` makes that
#     unrepresentable at the schema level too;
#
#   * the **person** family
#     (`jobs/ecodriving_person/job_eco_driving_person_aggregate.py`) keys on
#     `person_name_group_key` and writes `aggregation_included = (match_count = 1)`.
#     The private flag is recorded but has **no bearing on inclusion**, and
#     `043`'s `chk_eco_person_trip_assignments_identity_outcome` ties inclusion
#     to a resolved person identity alone. A trip marked private therefore does
#     contribute for this family, by design.
#
# Each family's dynamic predicate below is byte-equivalent to that family's own
# canonical aggregation query. Nothing here infers a private-trip policy from a
# generic rule, and one family's contract cannot reach the other.


@dataclass(frozen=True)
class FamilySources:
    """Repository-controlled object names and predicates for one ranking family.

    Every field is a fixed literal chosen by the registry, never caller input.
    """

    family_key: str
    assignments_table: str
    identity_column: str
    stats_identity_column: str
    weekly_stats_table: str
    monthly_stats_table: str
    roster_table: str
    roster_identity_column: str
    roster_name_column: str
    weekly_trends_view: str
    monthly_trends_view: str
    # The family's own aggregation-time inclusion filter, copied from its job.
    included_predicate: str
    # True when the display name comes from the assignment row rather than the
    # roster (the person pipeline resolves a canonical name while assigning).
    name_from_assignment: bool = False
    assignment_name_column: Optional[str] = None


DRIVER_SOURCES = FamilySources(
    family_key="driver",
    assignments_table=ASSIGNMENTS_TABLE,
    identity_column="assigned_id",
    stats_identity_column="assigned_id",
    weekly_stats_table=WEEKLY_STATS_TABLE,
    monthly_stats_table=MONTHLY_STATS_TABLE,
    roster_table=DRIVER_CHART_TABLE,
    roster_identity_column="driver_id",
    roster_name_column="driver_name",
    weekly_trends_view="public.eco_driver_weekly_trends_view",
    monthly_trends_view="public.eco_driver_monthly_trends_view",
    # Verbatim from `_fetch_aggregate_rows` in the driver job. The private
    # clause is redundant given `028`'s CHECK constraint and is kept for the
    # same belt-and-braces reason the job keeps it.
    included_predicate="a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE",
)

PERSON_SOURCES = FamilySources(
    family_key="person",
    assignments_table=PERSON_ASSIGNMENTS_TABLE,
    identity_column="person_name_group_key",
    stats_identity_column="person_name_group_key",
    weekly_stats_table=PERSON_WEEKLY_STATS_TABLE,
    monthly_stats_table=PERSON_MONTHLY_STATS_TABLE,
    roster_table=PERSON_ROSTER_VIEW,
    roster_identity_column="person_name_group_key",
    roster_name_column="person_name",
    weekly_trends_view="public.eco_person_weekly_trends_view",
    monthly_trends_view="public.eco_person_monthly_trends_view",
    # Verbatim from `_fetch_aggregate_rows` in the person job. There is
    # deliberately no private-trip clause: this pipeline includes an applicable
    # trip whose driver tag is marked private.
    included_predicate="a.aggregation_included IS TRUE",
    name_from_assignment=True,
    assignment_name_column="person_name",
)

FAMILY_SOURCES: dict[str, FamilySources] = {
    DRIVER_SOURCES.family_key: DRIVER_SOURCES,
    PERSON_SOURCES.family_key: PERSON_SOURCES,
}

# Mirrors RATE_COLUMNS / POINT_COLUMNS in the aggregation job (stable schema).
RATE_COLUMNS: dict[str, str] = {
    "overrev_events_count": "overrev_events_per_100km",
    "harsh_braking_events": "harsh_braking_events_per_100km",
    "harsh_acceleration_events": "harsh_acceleration_events_per_100km",
    "harsh_turning_events": "harsh_turning_events_per_100km",
    "idle_events": "idle_events_per_100km",
    "speeding_140_160_count": "speeding_140_160_events_per_100km",
    "speeding_160_170_count": "speeding_160_170_events_per_100km",
    "speeding_170_plus_count": "speeding_170_plus_events_per_100km",
}

POINT_COLUMNS: dict[str, str] = {
    "overrev_events_count": "overrev_points",
    "harsh_braking_events": "harsh_braking_points",
    "harsh_acceleration_events": "harsh_acceleration_points",
    "harsh_turning_events": "harsh_turning_points",
    "idle_events": "idle_points",
    "speeding_140_160_count": "speeding_140_160_points",
    "speeding_160_170_count": "speeding_160_170_points",
    "speeding_170_plus_count": "speeding_170_plus_points",
}

# Persisted per-metric points lost versus the metric maximum
# (``LEAST(metric_points - metric_max_points, 0)``), written by the aggregation
# job and migration 031/037. Read-only here: the Explorer never recomputes a
# different loss model when these values exist.
SUBTRACT_COLUMNS: dict[str, str] = {
    "overrev_events_count": "overrev_maxpoints_subtract",
    "harsh_braking_events": "harsh_braking_maxpoints_subtract",
    "harsh_acceleration_events": "harsh_acceleration_maxpoints_subtract",
    "harsh_turning_events": "harsh_turning_maxpoints_subtract",
    "idle_events": "idle_maxpoints_subtract",
    "speeding_140_160_count": "speeding_140_160_maxpoints_subtract",
    "speeding_160_170_count": "speeding_160_170_maxpoints_subtract",
    "speeding_170_plus_count": "speeding_170_plus_maxpoints_subtract",
}

# --- Rate / kilometre rounding (mirrors the aggregation job) -----------------

KILOMETERS_QUANTUM = Decimal("0.001")
RAW_RATE_QUANTUM = Decimal("0.0001")
STORED_RATE_QUANTUM = Decimal("1")


def total_kilometers(meters: int | None) -> Decimal:
    return (Decimal(int(meters or 0)) / Decimal(1000)).quantize(
        KILOMETERS_QUANTUM, rounding=ROUND_HALF_UP
    )


def raw_rate_per_100km(event_count: int | None, total_km: Decimal) -> Optional[Decimal]:
    if total_km <= 0:
        return None
    return ((Decimal(int(event_count or 0)) / total_km) * Decimal(100)).quantize(
        RAW_RATE_QUANTUM, rounding=ROUND_HALF_UP
    )


def stored_rate_per_100km(event_count: int | None, total_km: Decimal) -> Optional[Decimal]:
    raw = raw_rate_per_100km(event_count, total_km)
    if raw is None:
        return None
    return raw.quantize(STORED_RATE_QUANTUM, rounding=ROUND_HALF_UP)


def business_local_midnight(day: date) -> datetime:
    """Local business-timezone midnight for a date (mirrors the job boundary)."""

    tz = get_business_timezone()
    return datetime(day.year, day.month, day.day, tzinfo=tz)


# --- Pagination --------------------------------------------------------------

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100
MIN_PAGE_SIZE = 1


def normalize_pagination(page: int, limit: int) -> tuple[int, int, int]:
    """Validate 1-based page + limit; return (page, limit, offset).

    Rejects out-of-range values with a typed error rather than silently
    clamping, so internal callers fail closed.
    """

    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise InvalidPaginationError("page must be an integer >= 1")
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidPaginationError("limit must be an integer")
    if limit < MIN_PAGE_SIZE or limit > MAX_PAGE_SIZE:
        raise InvalidPaginationError(f"limit must be between {MIN_PAGE_SIZE} and {MAX_PAGE_SIZE}")
    return page, limit, (page - 1) * limit


# --- Sort allowlists ---------------------------------------------------------

# Sort keys are a **semantic** vocabulary, not physical column names. The key
# ``assigned_id`` means "the entry's identity" and resolves to whichever stats
# column carries it for the selected family — ``assigned_id`` for the driver
# family, ``person_name_group_key`` for the person family, which migration 043
# renamed. The key stays stable in URLs so a shared link keeps working across
# families, and no physical column name is ever exposed to or accepted from a
# browser.

ENTRY_SORT_FIELD_KEYS: tuple[str, ...] = (
    "ranking_position",
    "eco_driving_score_total",
    "total_distance_meters",
    "total_kilometers",
    "trips_count",
    "assigned_id",
)


def entry_sort_fields(sources: FamilySources = None) -> dict[str, str]:
    """Allowlist of sort keys to that family's physical stats columns."""

    sources = sources or DRIVER_SOURCES
    identity = sources.stats_identity_column
    return {
        "ranking_position": "s.ranking_position",
        "eco_driving_score_total": "s.eco_driving_score_total",
        "total_distance_meters": "s.total_distance_meters",
        "total_kilometers": "s.total_kilometers",
        "trips_count": "s.trips_count",
        "assigned_id": f"s.{identity}",
    }


def entry_default_order(sources: FamilySources = None) -> str:
    sources = sources or DRIVER_SOURCES
    return (
        "(s.ranking_position IS NULL), s.ranking_position ASC, "
        "s.eco_driving_score_total DESC NULLS LAST, s.total_distance_meters DESC, "
        f"s.{sources.stats_identity_column} ASC"
    )


def entry_tiebreaker(sources: FamilySources = None) -> str:
    sources = sources or DRIVER_SOURCES
    return f"s.{sources.stats_identity_column} ASC"


# Driver-family defaults, kept so existing driver-only call sites and their
# regressions resolve one definition rather than a second copy.
ENTRY_SORT_FIELDS: dict[str, str] = entry_sort_fields(DRIVER_SOURCES)
ENTRY_DEFAULT_ORDER = entry_default_order(DRIVER_SOURCES)
ENTRY_TIEBREAKER = entry_tiebreaker(DRIVER_SOURCES)

TRIP_SORT_FIELDS: dict[str, str] = {
    # A blank plate is a missing plate, and the view already renders both as the
    # same placeholder. `NULLIF(btrim(...), '')` makes the ordering agree with
    # that rendering, so `resolve_order_by`'s `NULLS LAST` puts genuinely
    # unknown vehicles at the bottom in BOTH directions instead of floating a
    # whitespace-only value to the top of an ascending sort.
    "vehicle_registration": "NULLIF(btrim(ct.registration), '')",
    "trip_start_ts": "a.trip_start_ts",
    "trip_end_ts": "a.trip_end_ts",
    "provider_trip_id": "a.provider_trip_id",
    "trip_distance_meters": "a.trip_distance_meters",
    "total_scoring_events": "(" + " + ".join(
        f"COALESCE(a.{metric}, 0)" for metric in EVENT_METRIC_COLUMNS
    ) + ")",
}

TRIP_DEFAULT_ORDER = "a.trip_start_ts ASC, a.provider_trip_id ASC"
TRIP_TIEBREAKER = "a.provider_trip_id ASC"


def _coerce_direction(direction: SortDirection | str | None) -> SortDirection:
    if direction is None:
        return SortDirection.ASC
    if isinstance(direction, SortDirection):
        return direction
    try:
        return SortDirection(str(direction).strip().upper())
    except ValueError as exc:
        raise InvalidSortFieldError("sort direction must be ASC or DESC") from exc


def resolve_order_by(
    sort_field: Optional[str],
    direction: SortDirection | str | None,
    *,
    allowlist: Mapping[str, str],
    default_order: str,
    tiebreaker: str,
) -> str:
    """Return a safe ORDER BY body built only from allowlisted fragments."""

    if sort_field is None:
        return default_order
    column = allowlist.get(sort_field)
    if column is None:
        raise InvalidSortFieldError("unsupported sort field")
    resolved_direction = _coerce_direction(direction)
    return f"{column} {resolved_direction.value} NULLS LAST, {tiebreaker}"


# --- Select fragments (fixed identifiers) ------------------------------------

_RATE_SELECT = ", ".join(f"s.{RATE_COLUMNS[m]}" for m in EVENT_METRIC_COLUMNS)
_POINT_SELECT = ", ".join(f"s.{POINT_COLUMNS[m]}" for m in EVENT_METRIC_COLUMNS)
_SUBTRACT_SELECT = ", ".join(f"s.{SUBTRACT_COLUMNS[m]}" for m in EVENT_METRIC_COLUMNS)
_ENTRY_METRIC_SELECT = ", ".join(f"s.{m}" for m in EVENT_METRIC_COLUMNS)

def _entry_base_select(sources: FamilySources) -> str:
    """Persisted-stats projection for one family.

    Every metric, rate, point and loss column name is identical across the two
    stats tables (`027`/`039`); only the identity column and the roster name
    differ, so the projection is generated rather than duplicated.
    """

    return f"""
    s.{sources.stats_identity_column} AS assigned_id,
    s.ranking_group,
    s.ranking_included,
    s.ranking_position,
    s.ranking_total_participants,
    s.qualification_status,
    s.calculation_status,
    s.trips_count,
    s.total_distance_meters,
    s.total_kilometers,
    s.eco_driving_score_total,
    s.ecodriving_rating_type,
    s.ecodriving_rating_type_share_percent,
    {_ENTRY_METRIC_SELECT},
    {_RATE_SELECT},
    {_POINT_SELECT},
    {_SUBTRACT_SELECT},
    c.{sources.roster_name_column} AS current_driver_name,
    (c.{sources.roster_identity_column} IS NOT NULL) AS current_chart_present,
    c.ranking_included AS current_chart_ranking_included
"""


def _entry_join(sources: FamilySources, *, weekly: bool) -> str:
    stats_table = sources.weekly_stats_table if weekly else sources.monthly_stats_table
    return f"""
    {stats_table} s
    LEFT JOIN {sources.roster_table} c
      ON c.client_id = s.client_id
     AND c.{sources.roster_identity_column} = s.{sources.stats_identity_column}
"""


# Retained for backwards compatibility with the driver-only call sites.
_ENTRY_BASE_SELECT = _entry_base_select(DRIVER_SOURCES)

_ASSIGNMENT_COUNTER_SELECT = ", ".join(f"a.{m}" for m in EVENT_METRIC_COLUMNS)
_ASSIGNMENT_COUNTER_SUM = ", ".join(
    f"COALESCE(SUM(a.{m}), 0) AS {m}" for m in EVENT_METRIC_COLUMNS
)

def _trip_join(sources: FamilySources) -> str:
    return f"""
    {sources.assignments_table} a
    LEFT JOIN {CLIENT_TRIPS_TABLE} ct
      ON ct.client_id = a.client_id AND ct.provider_trip_id = a.provider_trip_id
"""


_TRIP_JOIN = _trip_join(DRIVER_SOURCES)


def _contributing_where(sources: FamilySources) -> str:
    """Contributing-trip predicate for one family.

    The inclusion clause is the family's own, so trip evidence is drawn from
    exactly the universe that fed the score. For the person family that
    deliberately includes an applicable trip whose driver tag is marked
    private, because its aggregation job includes it.
    """

    return f"""
    a.client_id = %(client_id)s::uuid
    AND a.{sources.identity_column} = %(assigned_id)s
    AND a.trip_start_ts >= %(period_start_ts)s
    AND a.trip_start_ts < %(period_end_ts)s
    AND {sources.included_predicate}
"""


# Contributing-trip predicate shared by list, count, and reconstruction SUM.
_CONTRIBUTING_WHERE = _contributing_where(DRIVER_SOURCES)

_TOTAL_SCORING_EVENTS = "(" + " + ".join(
    f"COALESCE(a.{metric}, 0)" for metric in EVENT_METRIC_COLUMNS
) + ")"


def contributing_where(*, provider_trip_id: bool = False, min_distance: bool = False,
                       max_distance: bool = False, has_scoring_events: Optional[bool] = None,
                       sources: FamilySources = None) -> str:
    """Build the shared list/count predicate from fixed allowlisted fragments."""

    conditions = [_contributing_where(sources or DRIVER_SOURCES).strip()]
    if provider_trip_id:
        conditions.append("a.provider_trip_id = %(provider_trip_id)s")
    if min_distance:
        conditions.append("a.trip_distance_meters >= %(min_distance_meters)s")
    if max_distance:
        conditions.append("a.trip_distance_meters <= %(max_distance_meters)s")
    if has_scoring_events is True:
        conditions.append(f"{_TOTAL_SCORING_EVENTS} > 0")
    elif has_scoring_events is False:
        conditions.append(f"{_TOTAL_SCORING_EVENTS} = 0")
    return "\n    AND ".join(conditions)


# --- Period listing SQL ------------------------------------------------------

def weekly_periods_sql(
    *, with_year: bool, with_month: bool, sources: FamilySources = None
) -> str:
    sources = sources or DRIVER_SOURCES
    conditions = ["client_id = %(client_id)s::uuid"]
    if with_year:
        conditions.append("EXTRACT(YEAR FROM month_start_date) = %(year)s")
    if with_month:
        conditions.append("EXTRACT(MONTH FROM month_start_date) = %(month)s")
    where = " AND ".join(conditions)
    return f"""
        SELECT
            month_start_date,
            period_start_date,
            period_end_date,
            period_sequence_in_month,
            period_label,
            bool_or(is_partial_period) AS is_partial_period,
            COUNT(*) AS entry_count,
            COUNT(*) FILTER (WHERE ranking_group = 'INCLUDED') AS included_count,
            COUNT(*) FILTER (WHERE ranking_group = 'EXCLUDED') AS excluded_count,
            COUNT(*) FILTER (WHERE ranking_group = 'UNKNOWN_DRIVER') AS unknown_count,
            -- Rows outside every ranking population (non-QUALIFIED). Reported,
            -- never counted into a ranking group.
            COUNT(*) FILTER (WHERE ranking_group IS NULL) AS not_ranked_count,
            -- The subset of those the roster still allowed into the ranking:
            -- below the distance threshold, but part of the INCLUDED tab's
            -- population. Kept apart from `included_count` so the persisted
            -- group counts stay a faithful read of `ranking_group`.
            COUNT(*) FILTER (
                WHERE ranking_group IS NULL AND ranking_included IS TRUE
            ) AS not_ranked_included_count,
            MAX(updated_at) AS source_calculated_at
        FROM {sources.weekly_stats_table}
        WHERE {where}
        GROUP BY month_start_date, period_start_date, period_end_date,
                 period_sequence_in_month, period_label
        ORDER BY month_start_date, period_start_date, period_end_date,
                 period_sequence_in_month
    """


def monthly_periods_sql(
    *, with_year: bool, with_month: bool, sources: FamilySources = None
) -> str:
    sources = sources or DRIVER_SOURCES
    conditions = ["client_id = %(client_id)s::uuid"]
    if with_year:
        conditions.append("EXTRACT(YEAR FROM month_start_date) = %(year)s")
    if with_month:
        conditions.append("EXTRACT(MONTH FROM month_start_date) = %(month)s")
    where = " AND ".join(conditions)
    return f"""
        SELECT
            month_start_date,
            month_end_date,
            COUNT(*) AS entry_count,
            COUNT(*) FILTER (WHERE ranking_group = 'INCLUDED') AS included_count,
            COUNT(*) FILTER (WHERE ranking_group = 'EXCLUDED') AS excluded_count,
            COUNT(*) FILTER (WHERE ranking_group = 'UNKNOWN_DRIVER') AS unknown_count,
            -- Rows outside every ranking population (non-QUALIFIED). Reported,
            -- never counted into a ranking group.
            COUNT(*) FILTER (WHERE ranking_group IS NULL) AS not_ranked_count,
            -- The subset of those the roster still allowed into the ranking:
            -- below the distance threshold, but part of the INCLUDED tab's
            -- population. Kept apart from `included_count` so the persisted
            -- group counts stay a faithful read of `ranking_group`.
            COUNT(*) FILTER (
                WHERE ranking_group IS NULL AND ranking_included IS TRUE
            ) AS not_ranked_included_count,
            MAX(updated_at) AS source_calculated_at
        FROM {sources.monthly_stats_table}
        WHERE {where}
        GROUP BY month_start_date, month_end_date
        ORDER BY month_start_date
    """


# --- Ranking entry SQL -------------------------------------------------------

# Free-text search over the driver identity the ranking actually shows: the
# opaque assigned id and the current chart name. The pattern is always a bound
# parameter; only the presence of the predicate is decided here.
def entry_search_predicate(sources: FamilySources = None) -> str:
    """Free-text search over the identity the ranking actually shows.

    The pattern is always a bound parameter; only the presence of the predicate
    and which family's columns it names are decided here.
    """

    sources = sources or DRIVER_SOURCES
    return (
        f"(s.{sources.stats_identity_column} ILIKE %(search)s"
        f" OR c.{sources.roster_name_column} ILIKE %(search)s)"
    )


ENTRY_SEARCH_PREDICATE = entry_search_predicate(DRIVER_SOURCES)

# Longest accepted search term. A bound above the widest realistic driver name
# keeps a pathological pattern from reaching the database at all.
MAX_SEARCH_LENGTH = 120


def like_pattern(term: str) -> str:
    """A contains-pattern with the LIKE metacharacters neutralised.

    ``%`` and ``_`` inside a user term are literal characters, not wildcards,
    so a search for ``100%`` cannot silently widen into "everything".
    """

    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# A driver the roster allowed into the ranking who did not reach the qualifying
# distance in this period. `046` makes `ranking_group IS NULL` exactly "not
# QUALIFIED" — that is, `LOW_DISTANCE` or `NO_DISTANCE` — and the persisted
# `ranking_included` still carries the permission the roster granted, so the
# pair names "allowed in, but with too few kilometres to be ranked".
PERMITTED_NOT_RANKED_PREDICATE = (
    "(s.ranking_group IS NULL AND s.ranking_included IS TRUE)"
)


def group_predicate(group_value: str) -> str:
    """The membership test for one ranking tab.

    `INCLUDED` is the client's ranking population, not merely the rows that
    happened to earn a position: a driver with ranking permission who drove too
    few kilometres (or none) belongs on that tab, unranked, rather than
    vanishing from every tab into a bare count. `EXCLUDED` and `UNKNOWN_DRIVER`
    keep plain equality — neither describes a permission, so neither has a
    below-threshold population to absorb.
    """

    if group_value == RankingGroup.INCLUDED.value:
        return (
            f"(s.ranking_group = %(ranking_group)s"
            f" OR {PERMITTED_NOT_RANKED_PREDICATE})"
        )
    return "s.ranking_group = %(ranking_group)s"


def _entry_where(
    *,
    weekly: bool,
    ranking_group: Optional[str],
    with_search: bool = False,
    sources: FamilySources = None,
) -> str:
    sources = sources or DRIVER_SOURCES
    conditions = ["s.client_id = %(client_id)s::uuid"]
    if weekly:
        conditions.append("s.month_start_date = %(month_start_date)s")
        conditions.append("s.period_start_date = %(period_start_date)s")
        conditions.append("s.period_end_date = %(period_end_date)s")
    else:
        conditions.append("s.month_start_date = %(month_start_date)s")
    if ranking_group is not None:
        conditions.append(group_predicate(ranking_group))
    if with_search:
        conditions.append(entry_search_predicate(sources))
    return " AND ".join(conditions)


def entries_sql(
    *,
    weekly: bool,
    ranking_group: Optional[str],
    order_by: str,
    with_search: bool = False,
    sources: FamilySources = None,
) -> str:
    sources = sources or DRIVER_SOURCES
    join = _entry_join(sources, weekly=weekly)
    where = _entry_where(
        weekly=weekly, ranking_group=ranking_group, with_search=with_search,
        sources=sources,
    )
    return f"""
        SELECT {_entry_base_select(sources)},
               {"s.period_label, s.is_partial_period" if weekly else "to_char(s.month_start_date, 'YYYY-MM') AS period_label, FALSE AS is_partial_period"}
        FROM {join}
        WHERE {where}
        ORDER BY {order_by}
        LIMIT %(limit)s OFFSET %(offset)s
    """


def entries_count_sql(
    *,
    weekly: bool,
    ranking_group: Optional[str],
    with_search: bool = False,
    sources: FamilySources = None,
) -> str:
    sources = sources or DRIVER_SOURCES
    stats_table = sources.weekly_stats_table if weekly else sources.monthly_stats_table
    if with_search:
        # The search reaches the roster name, so the count must see the same
        # join as the list or the two would disagree.
        join = _entry_join(sources, weekly=weekly)
        where = _entry_where(
            weekly=weekly, ranking_group=ranking_group, with_search=True, sources=sources
        )
        return f"""
            SELECT COUNT(*) AS total_count
            FROM {join}
            WHERE {where}
        """
    # Without the search the count needs no roster join: the roster never
    # changes group membership. `ranking_included` is the PERSISTED permission
    # on the stats row, not a roster column, so the unjoined count still asks
    # the same question the listing asks.
    where = _entry_where(
        weekly=weekly, ranking_group=ranking_group, sources=sources
    ).replace("s.", "")
    return f"""
        SELECT COUNT(*) AS total_count
        FROM {stats_table}
        WHERE {where}
    """


def single_entry_sql(*, weekly: bool, sources: FamilySources = None) -> str:
    sources = sources or DRIVER_SOURCES
    join = _entry_join(sources, weekly=weekly)
    where = _entry_where(weekly=weekly, ranking_group=None, sources=sources)
    return f"""
        SELECT {_entry_base_select(sources)},
               {"s.period_label, s.is_partial_period" if weekly else "to_char(s.month_start_date, 'YYYY-MM') AS period_label, FALSE AS is_partial_period"}
        FROM {join}
        WHERE {where} AND s.{sources.stats_identity_column} = %(assigned_id)s
        LIMIT 1
    """


# --- Contributing trips SQL --------------------------------------------------

def contributing_trips_sql(
    order_by: str, where: Optional[str] = None, sources: FamilySources = None
) -> str:
    sources = sources or DRIVER_SOURCES
    return f"""
        SELECT
            a.client_id,
            a.provider_trip_id,
            a.trip_start_ts,
            a.trip_end_ts,
            a.{sources.identity_column} AS assigned_id,
            a.assignment_source,
            a.trip_distance_meters,
            a.aggregation_included,
            a.is_private_trip,
            a.exclusion_reason,
            {_ASSIGNMENT_COUNTER_SELECT},
            ct.registration AS vehicle_registration,
            (ct.provider_trip_id IS NOT NULL) AS client_trip_present
        FROM {_trip_join(sources)}
        WHERE {where or _contributing_where(sources)}
        ORDER BY {order_by}
        LIMIT %(limit)s OFFSET %(offset)s
    """


def contributing_trips_count_sql(
    where: Optional[str] = None, sources: FamilySources = None
) -> str:
    sources = sources or DRIVER_SOURCES
    return f"""
        SELECT COUNT(*) AS total_count
        FROM {sources.assignments_table} a
        WHERE {where or _contributing_where(sources)}
    """


CONTRIBUTING_TRIPS_COUNT_SQL = contributing_trips_count_sql()

def reconstruction_totals_sql(sources: FamilySources = None) -> str:
    """Reconstructed totals for one entry, over the family's own trip universe.

    Reconciliation compares a persisted stats row against what the current
    assignments say. That comparison is only meaningful if it reads the **same**
    source, identity and inclusion contract the family's aggregation uses — a
    driver-shaped reconstruction would query the wrong table with the wrong
    identity for the person family and reconcile against a universe that never
    produced the row.
    """

    sources = sources or DRIVER_SOURCES
    return f"""
    SELECT
        COUNT(*) AS trips_count,
        COALESCE(SUM(a.trip_distance_meters), 0) AS total_distance_meters,
        {_ASSIGNMENT_COUNTER_SUM},
        COUNT(*) FILTER (WHERE ct.provider_trip_id IS NULL) AS missing_client_trip_count
    FROM {_trip_join(sources)}
    WHERE {_contributing_where(sources)}
"""


def window_diagnostics_sql(sources: FamilySources = None) -> str:
    """Counts over the full window **without** the inclusion filter.

    Deliberately unfiltered so excluded trips can be counted safely. It still
    scopes to the family's own assignments table and identity column, and it
    reports the two exclusion reasons the families use — the private flag, which
    only the driver family acts on, and the inclusion decision itself, which
    both do.
    """

    sources = sources or DRIVER_SOURCES
    return f"""
    SELECT
        COUNT(*) AS window_trips,
        COUNT(*) FILTER (WHERE a.is_private_trip = TRUE) AS private_trips,
        COUNT(*) FILTER (WHERE a.aggregation_included = FALSE) AS not_aggregated_trips
    FROM {sources.assignments_table} a
    WHERE a.client_id = %(client_id)s::uuid
      AND a.{sources.identity_column} = %(assigned_id)s
      AND a.trip_start_ts >= %(period_start_ts)s
      AND a.trip_start_ts < %(period_end_ts)s
"""


# Driver-family defaults for the existing driver-only call sites.
RECONSTRUCTION_TOTALS_SQL = reconstruction_totals_sql(DRIVER_SOURCES)
WINDOW_DIAGNOSTICS_SQL = window_diagnostics_sql(DRIVER_SOURCES)


# --- Fleet score distribution (S12 histogram) --------------------------------

# Bin geometry is derived from the repository's own score domain
# (``MIN_POSSIBLE_SCORE`` .. ``MAX_POSSIBLE_SCORE`` in ``eco_scoring``), never
# from a design sample. ``SCORE_BIN_WIDTH`` divides that domain exactly.
SCORE_BIN_WIDTH = 10
SCORE_BIN_MIN = -100
SCORE_BIN_MAX = 100
SCORE_BIN_COUNT = (SCORE_BIN_MAX - SCORE_BIN_MIN) // SCORE_BIN_WIDTH


def score_bin_index(score: Decimal | int | float | None) -> Optional[int]:
    """0-based bin for one score, clamped to the repository score domain.

    The top bin is closed on both sides so a perfect ``100`` lands in the last
    bin rather than in a bin that does not exist.
    """

    if score is None:
        return None
    value = score if isinstance(score, Decimal) else Decimal(str(score))
    if value < SCORE_BIN_MIN:
        value = Decimal(SCORE_BIN_MIN)
    if value >= SCORE_BIN_MAX:
        return SCORE_BIN_COUNT - 1
    index = int((value - Decimal(SCORE_BIN_MIN)) // Decimal(SCORE_BIN_WIDTH))
    if index < 0:
        return 0
    if index > SCORE_BIN_COUNT - 1:
        return SCORE_BIN_COUNT - 1
    return index


def score_distribution_sql(
    *, weekly: bool, with_group: bool, sources: FamilySources = None
) -> str:
    """Bucket counts + median/mean over exactly one client, period and group.

    Binning is done in SQL with the same clamped, top-bin-closed rule as
    :func:`score_bin_index`, so the Python and SQL paths cannot drift.
    """

    sources = sources or DRIVER_SOURCES
    stats_table = sources.weekly_stats_table if weekly else sources.monthly_stats_table
    conditions = ["client_id = %(client_id)s::uuid", "eco_driving_score_total IS NOT NULL"]
    if weekly:
        conditions.append("month_start_date = %(month_start_date)s")
        conditions.append("period_start_date = %(period_start_date)s")
        conditions.append("period_end_date = %(period_end_date)s")
    else:
        conditions.append("month_start_date = %(month_start_date)s")
    if with_group:
        conditions.append("ranking_group = %(ranking_group)s")
    else:
        # No group filter still means "inside some ranking population": a row
        # outside every population (NULL group) is not part of the fleet the
        # ranking is drawn from.
        conditions.append("ranking_group IS NOT NULL")
    where = " AND ".join(conditions)
    return f"""
        WITH scoped AS (
            SELECT eco_driving_score_total AS score
            FROM {stats_table}
            WHERE {where}
        ), binned AS (
            SELECT
                LEAST(
                    {SCORE_BIN_COUNT} - 1,
                    GREATEST(
                        0,
                        FLOOR((LEAST(GREATEST(score, {SCORE_BIN_MIN}), {SCORE_BIN_MAX}) - ({SCORE_BIN_MIN}))
                              / {SCORE_BIN_WIDTH}::numeric)::int
                    )
                ) AS bin_index,
                score
            FROM scoped
        )
        SELECT
            bin_index,
            COUNT(*) AS bin_count,
            (SELECT COUNT(*) FROM binned) AS total_count,
            (SELECT AVG(score) FROM binned) AS mean_score,
            (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY score) FROM binned) AS median_score
        FROM binned
        GROUP BY bin_index
        ORDER BY bin_index
    """


# --- Driver trend (S12) ------------------------------------------------------

WEEKLY_TRENDS_VIEW = "public.eco_driver_weekly_trends_view"
MONTHLY_TRENDS_VIEW = "public.eco_driver_monthly_trends_view"

# Approved trend window: the last eight comparable periods (`ECO-003` §5.2).
TREND_PERIOD_LIMIT = 8


def driver_trend_sql(*, weekly: bool, sources: FamilySources = None) -> str:
    """The driver's own last ``TREND_PERIOD_LIMIT`` persisted periods.

    Reads the repository's existing trend views; it derives no new trend metric
    and fabricates no period. Periods the driver has no row for simply do not
    appear — the caller must never zero-fill them.
    """

    sources = sources or DRIVER_SOURCES
    if weekly:
        return f"""
            SELECT
                month_start_date,
                period_start_date,
                period_end_date,
                period_sequence_in_month,
                period_label,
                is_partial_period,
                eco_driving_score_total,
                ranking_position,
                ranking_total_participants,
                qualification_status,
                total_kilometers
            FROM {sources.weekly_trends_view}
            WHERE client_id = %(client_id)s::uuid
              AND {sources.stats_identity_column} = %(assigned_id)s
              AND (month_start_date, period_end_date) <= (%(month_start_date)s, %(period_end_date)s)
            ORDER BY month_start_date DESC, period_end_date DESC
            LIMIT %(limit)s
        """
    return f"""
        SELECT
            month_start_date,
            month_start_date AS period_start_date,
            month_end_date AS period_end_date,
            NULL::int AS period_sequence_in_month,
            to_char(month_start_date, 'YYYY-MM') AS period_label,
            FALSE AS is_partial_period,
            eco_driving_score_total,
            ranking_position,
            ranking_total_participants,
            qualification_status,
            total_kilometers
        FROM {sources.monthly_trends_view}
        WHERE client_id = %(client_id)s::uuid
          AND {sources.stats_identity_column} = %(assigned_id)s
          AND month_start_date <= %(month_start_date)s
        ORDER BY month_start_date DESC
        LIMIT %(limit)s
    """


# --- Within-month snapshot progression (S12 diagnostic) ----------------------

def period_progression_sql(sources: FamilySources = None) -> str:
    """Every persisted weekly snapshot of one driver inside one month.

    Weekly periods are **cumulative month-to-date** snapshots. These rows are a
    diagnostic progression, never addends: nothing here may be summed, and the
    caller must present them as cumulative snapshots.
    """

    sources = sources or DRIVER_SOURCES
    metric_select = ", ".join(f"s.{m}" for m in EVENT_METRIC_COLUMNS)
    rate_select = ", ".join(f"s.{RATE_COLUMNS[m]}" for m in EVENT_METRIC_COLUMNS)
    return f"""
        SELECT
            s.period_label,
            s.period_start_date,
            s.period_end_date,
            s.period_sequence_in_month,
            s.is_partial_period,
            s.eco_driving_score_total,
            s.qualification_status,
            s.total_distance_meters,
            s.total_kilometers,
            s.trips_count,
            {metric_select},
            {rate_select}
        FROM {sources.weekly_stats_table} s
        WHERE s.client_id = %(client_id)s::uuid
          AND s.{sources.stats_identity_column} = %(assigned_id)s
          AND s.month_start_date = %(month_start_date)s
          AND s.period_end_date <= %(period_end_date)s
        ORDER BY s.period_end_date ASC, s.period_sequence_in_month ASC
    """


# --- Read-only client-database reader ----------------------------------------

class RowReader(Protocol):
    def fetch_all(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        ...

    def fetch_one(self, sql: str, params: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        ...


class ClientDatabaseReader:
    """Read-only reader over a client business-database psycopg connection.

    The connection must already be scoped to the correct client database. Each
    fetch runs in a read-only transaction with a bounded statement timeout,
    mirroring ``jobs/common/environment_identity._read_marker``.
    """

    def __init__(self, conn: Any, *, statement_timeout_ms: int = 15000) -> None:
        self._conn = conn
        self._statement_timeout_ms = int(statement_timeout_ms)

    def _run(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        try:
            with self._conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}")
                cur.execute(sql, dict(params))
                rows = cur.fetchall()
            return [dict(row) for row in rows]
        finally:
            try:
                self._conn.rollback()
            except Exception:
                pass

    def fetch_all(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self._run(sql, params)

    def fetch_one(self, sql: str, params: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        rows = self._run(sql, params)
        return rows[0] if rows else None


def build_period_boundary_params(period_start_date: date, period_end_date: date) -> dict[str, datetime]:
    return {
        "period_start_ts": business_local_midnight(period_start_date),
        "period_end_ts": business_local_midnight(period_end_date),
    }


# --- Dynamic arbitrary-week recomputation ------------------------------------
#
# The persisted weekly stats rows are cumulative month-to-date snapshots, so an
# arbitrary subset of a month's weeks cannot be assembled from them: two
# consecutive snapshots share a prefix and adding them double-counts it. These
# queries therefore go back to ``eco_trip_assignments`` — the per-trip read
# model the aggregation job itself writes and then groups — and recompute the
# totals for the union of the selected isolated intervals.
#
# ``aggregation_included`` and ``is_private_trip`` are columns the job wrote
# using the *client's own* inclusion contract, so reading them back preserves
# ALPHA00001's private-trip exclusion and BRAVO00016's include-all policy without
# the web layer knowing either rule. There is deliberately no client-specific
# branch anywhere below.

# Hard ceiling on how many disjoint intervals one request may carry.
#
# A calendar month yields **four to six** week buckets, not "at most five": a
# month that starts on a Sunday produces a one-day `W1`, four full weeks and a
# trailing `W6` (March 2026 and August 2021 are real examples), while a
# 28-day February starting on a Monday produces only four. Six is therefore the
# true maximum, and it is also the worst case for the interval predicate
# because merging cannot reduce a fully non-contiguous selection.
MAX_SELECTION_INTERVALS = 6


def _interval_param_names(index: int) -> tuple[str, str]:
    return (f"win{index}_start", f"win{index}_end")


def build_interval_params(
    intervals: Sequence[tuple[datetime, datetime]]
) -> dict[str, datetime]:
    """Bind one ``win{n}_start`` / ``win{n}_end`` pair per merged interval."""

    if not intervals:
        raise InvalidPaginationError("at least one selected interval is required")
    if len(intervals) > MAX_SELECTION_INTERVALS:
        raise InvalidPaginationError("too many selected intervals")
    params: dict[str, datetime] = {}
    for index, (start, end) in enumerate(intervals):
        start_name, end_name = _interval_param_names(index)
        params[start_name] = start
        params[end_name] = end
    return params


def interval_predicate(interval_count: int, *, alias: str = "a") -> str:
    """``trip_start_ts`` inside the union of the selected half-open intervals.

    Every bound is a placeholder; the only caller-influenced part of the shape
    is how many intervals there are, which is validated against
    ``MAX_SELECTION_INTERVALS``. Intervals are merged before they get here, so
    no trip can satisfy two disjuncts and be counted twice.
    """

    count = int(interval_count)
    if count < 1:
        raise InvalidPaginationError("at least one selected interval is required")
    if count > MAX_SELECTION_INTERVALS:
        raise InvalidPaginationError("too many selected intervals")
    parts = []
    for index in range(count):
        start_name, end_name = _interval_param_names(index)
        parts.append(
            f"({alias}.trip_start_ts >= %({start_name})s"
            f" AND {alias}.trip_start_ts < %({end_name})s)"
        )
    return "(" + " OR ".join(parts) + ")"


# The include/exclude filter the aggregation job applies when it groups
# assignments, restated once and reused by every aggregate expression below.
def _dynamic_sums(included: str) -> str:
    return ",\n            ".join(
        f"COALESCE(sum(a.{metric}) FILTER (WHERE {included}), 0)::bigint AS {metric}"
        for metric in EVENT_METRIC_COLUMNS
    )


def dynamic_population_sql(interval_count: int, sources: FamilySources = None) -> str:
    """Per-driver totals over the selected interval union, for one client.

    Mirrors ``job_eco_driving_aggregate._fetch_aggregate_rows`` field for field:
    same inclusion filter, same ``assigned_id IS NOT NULL`` requirement, same
    ``HAVING`` on at least one included trip, same LEFT JOIN to the driver chart
    for ranking membership. The only difference is the time predicate, which is
    a union of intervals instead of one contiguous range.

    One statement returns the whole authorized population, so ranking is
    recomputed set-wise and there is no per-driver query.
    """

    sources = sources or DRIVER_SOURCES
    included = sources.included_predicate
    identity = sources.identity_column
    name_select = (
        f'min(a.{sources.assignment_name_column} COLLATE "C") AS assignment_name,'
        if sources.name_from_assignment
        else "NULL::text AS assignment_name,"
    )
    return f"""
        WITH grouped AS (
          SELECT
            a.client_id,
            a.{identity} AS assigned_id,
            {name_select}
            count(*) FILTER (WHERE {included})::int AS trips_count,
            count(*)::int AS source_trips_count,
            count(*) FILTER (WHERE NOT ({included}))::int AS skipped_trips_count,
            COALESCE(sum(COALESCE(a.trip_distance_meters, 0)) FILTER (
              WHERE {included}
            ), 0)::bigint AS total_distance_meters,
            {_dynamic_sums(included)}
          FROM {sources.assignments_table} a
          WHERE a.client_id = %(client_id)s::uuid
            AND {interval_predicate(interval_count)}
            AND a.{identity} IS NOT NULL
          GROUP BY a.client_id, a.{identity}
          HAVING count(*) FILTER (WHERE {included}) > 0
        )
        SELECT
          g.*,
          c.{sources.roster_identity_column} AS roster_id,
          c.{sources.roster_name_column} AS roster_name,
          c.ranking_included
        FROM grouped g
        LEFT JOIN {sources.roster_table} c
          ON c.client_id = g.client_id
         AND c.{sources.roster_identity_column} = g.assigned_id
        ORDER BY g.assigned_id
    """


def dynamic_contributing_where(
    interval_count: int,
    *,
    provider_trip_id: bool = False,
    min_distance: bool = False,
    max_distance: bool = False,
    has_scoring_events: Optional[bool] = None,
    sources: FamilySources = None,
) -> str:
    """Contributing-trip predicate for one driver over the selected union.

    Identical to :func:`contributing_where` apart from the time predicate, so
    the trip evidence shown for a dynamic selection is exactly the set of rows
    that fed its score — no more and no less.
    """

    sources = sources or DRIVER_SOURCES
    conditions = [
        "a.client_id = %(client_id)s::uuid",
        f"a.{sources.identity_column} = %(assigned_id)s",
        interval_predicate(interval_count),
        sources.included_predicate,
    ]
    if provider_trip_id:
        conditions.append("a.provider_trip_id = %(provider_trip_id)s")
    if min_distance:
        conditions.append("a.trip_distance_meters >= %(min_distance_meters)s")
    if max_distance:
        conditions.append("a.trip_distance_meters <= %(max_distance_meters)s")
    if has_scoring_events is True:
        conditions.append(f"{_TOTAL_SCORING_EVENTS} > 0")
    elif has_scoring_events is False:
        conditions.append(f"{_TOTAL_SCORING_EVENTS} = 0")
    return "\n    AND ".join(conditions)


def dynamic_month_bounds_sql(sources: FamilySources = None) -> str:
    """Months that have at least one assignment row, newest first, one client.

    Drives the month stepper for the dynamic basis. It reads the assignment
    table rather than the stats tables because a month can be selectable before
    any snapshot for it exists.
    """

    sources = sources or DRIVER_SOURCES
    return f"""
        SELECT
            date_trunc('month', a.trip_start_ts AT TIME ZONE %(timezone)s)::date
                AS month_start_date,
            count(*)::bigint AS assignment_count,
            count(*) FILTER (WHERE {sources.included_predicate})::bigint
                AS included_assignment_count
        FROM {sources.assignments_table} a
        WHERE a.client_id = %(client_id)s::uuid
          AND a.{sources.identity_column} IS NOT NULL
        GROUP BY 1
        ORDER BY 1 DESC
    """
