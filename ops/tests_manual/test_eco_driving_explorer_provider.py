#!/usr/bin/env python3
"""Manual unit-style checks for the Eco Driving Explorer provider foundation.

These tests use synthetic in-memory fixtures only; they do not require a
database. A small fake row reader returns canned rows keyed by query shape, so
the provider mapping, ranking-group isolation, identifier contract, SQL-safety
allowlists, pagination, and reconciliation logic can be exercised in isolation.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving.eco_scoring import (  # noqa: E402
    METRIC_MAX_POINTS,
    REQUIRED_METRICS,
    calculate_eco_score,
)

from api.eco_driving_explorer import (  # noqa: E402
    DriverMetadataSource,
    LineageQuality,
    PeriodType,
    RankingGroup,
    RankingPeriodKey,
    ReconciliationStatus,
    TripContribution,
    classify_trip_contribution,
    get_provider,
    is_registered,
)
from api.eco_driving_explorer import queries as q  # noqa: E402
from api.eco_driving_explorer.errors import (  # noqa: E402
    InvalidPaginationError,
    InvalidRankingGroupError,
    InvalidSortFieldError,
    ProviderNotFoundError,
    RankingEntryNotFoundError,
    UnsupportedPeriodTypeError,
)
from api.eco_driving_explorer.alpha_driver_provider import (  # noqa: E402
    AlphaDriverEcoDrivingProvider,
)
from api.eco_driving_explorer.provider import EcoDrivingExplorerProvider  # noqa: E402

CLIENT_ID = "9536f715-2fd0-4ffd-86ed-ba06f5490c5e"

WEEKLY_KEY = RankingPeriodKey(
    period_type=PeriodType.WEEKLY,
    month_start_date=date(2026, 7, 1),
    period_start_date=date(2026, 7, 1),
    period_end_date=date(2026, 7, 27),
    period_sequence_in_month=4,
)

W5_KEY = RankingPeriodKey(
    period_type=PeriodType.WEEKLY,
    month_start_date=date(2026, 7, 1),
    period_start_date=date(2026, 7, 1),
    period_end_date=date(2026, 8, 1),
    period_sequence_in_month=5,
)


def assert_raises(exc_type, func) -> None:
    try:
        func()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


# --- fake reader -------------------------------------------------------------

class FakeRowReader:
    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _kind(sql: str) -> str:
        # S12 read surfaces are matched first: they carry their own markers and
        # would otherwise fall through to the generic `AS total_count` branch.
        if "bin_index" in sql:
            return "score_distribution"
        if "_trends_view" in sql:
            return "driver_trend"
        if "ORDER BY s.period_end_date ASC" in sql:
            return "period_progression"
        if "not_aggregated_trips" in sql:
            return "window_diag"
        if "missing_client_trip_count" in sql:
            return "recon_totals"
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

    def fetch_all(self, sql, params):
        self.calls.append((sql, dict(params)))
        return [dict(r) for r in self.responses.get(self._kind(sql), [])]

    def fetch_one(self, sql, params):
        self.calls.append((sql, dict(params)))
        rows = self.responses.get(self._kind(sql), [])
        return dict(rows[0]) if rows else None

    def last_params(self, kind: str) -> dict:
        for sql, params in reversed(self.calls):
            if self._kind(sql) == kind:
                return params
        raise AssertionError(f"no call of kind {kind}")

    def last_sql(self, kind: str) -> str:
        for sql, _params in reversed(self.calls):
            if self._kind(sql) == kind:
                return sql
        raise AssertionError(f"no call of kind {kind}")


def make_entry_row(**overrides) -> dict:
    row = {
        "assigned_id": "12345",
        "ranking_group": "INCLUDED",
        "ranking_included": True,
        "ranking_position": 1,
        "ranking_total_participants": 32,
        "qualification_status": "QUALIFIED",
        "calculation_status": "OK",
        "trips_count": 60,
        "total_distance_meters": 2200000,
        "total_kilometers": Decimal("2200.000"),
        "eco_driving_score_total": Decimal("100.00"),
        "ecodriving_rating_type": "bezpieczny",
        "ecodriving_rating_type_share_percent": Decimal("25.00"),
        "period_label": "2026-07-W1",
        "is_partial_period": True,
        "current_driver_name": "REDACTED",
        "current_chart_present": True,
        "current_chart_ranking_included": True,
    }
    for metric in REQUIRED_METRICS:
        row[metric] = 0
        row[q.RATE_COLUMNS[metric]] = Decimal("0")
        row[q.POINT_COLUMNS[metric]] = Decimal(METRIC_MAX_POINTS[metric])
    row.update(overrides)
    return row


def make_totals_row(**overrides) -> dict:
    row = {
        "trips_count": 60,
        "total_distance_meters": 2200000,
        "missing_client_trip_count": 0,
    }
    for metric in REQUIRED_METRICS:
        row[metric] = 0
    row.update(overrides)
    return row


def make_window_diag(**overrides) -> dict:
    row = {"window_trips": 60, "private_trips": 0, "not_aggregated_trips": 0}
    row.update(overrides)
    return row


def make_trip_row(**overrides) -> dict:
    row = {
        "client_id": CLIENT_ID,
        "provider_trip_id": 433185849,
        "trip_start_ts": datetime(2026, 7, 3, 8, 9, 16, tzinfo=timezone.utc),
        "trip_end_ts": datetime(2026, 7, 3, 8, 40, 0, tzinfo=timezone.utc),
        "assigned_id": "12345",
        "assignment_source": "DYSPONENT_ID",
        "trip_distance_meters": 36000,
        "aggregation_included": True,
        "is_private_trip": False,
        "exclusion_reason": None,
        "client_trip_present": True,
        "vehicle_registration": "WX 1234A",
    }
    for metric in REQUIRED_METRICS:
        row[metric] = 0
    row.update(overrides)
    return row


def _provider() -> AlphaDriverEcoDrivingProvider:
    return AlphaDriverEcoDrivingProvider(client_id=CLIENT_ID)


# --- provider + registry -----------------------------------------------------

def test_registry_resolves_and_fails_closed() -> None:
    provider = get_provider("ALPHA00001", "driver", client_id=CLIENT_ID)
    assert isinstance(provider, EcoDrivingExplorerProvider)
    assert provider.provider_key == "alpha00001_driver"
    assert provider.client_code == "ALPHA00001"
    assert provider.ranking_family == "driver"
    assert set(provider.supported_period_types) == {PeriodType.WEEKLY, PeriodType.MONTHLY}
    assert is_registered("ALPHA00001", "driver") is True

    assert_raises(ProviderNotFoundError, lambda: get_provider("UNKNOWN", "driver", client_id=CLIENT_ID))
    assert_raises(ProviderNotFoundError, lambda: get_provider("ALPHA00001", "vehicle", client_id=CLIENT_ID))
    assert is_registered("ALPHA00001", "vehicle") is False


def test_provider_does_not_infer_from_tables() -> None:
    # Registry never resolves an unknown key even if tables might exist.
    assert_raises(ProviderNotFoundError, lambda: get_provider("BRAVO00016", "driver", client_id=CLIENT_ID))


# --- periods -----------------------------------------------------------------

def test_weekly_periods_preserve_labels_and_support_w5() -> None:
    rows = [
        {
            "month_start_date": date(2026, 7, 1),
            "period_start_date": date(2026, 7, 1),
            "period_end_date": date(2026, 7, 6),
            "period_sequence_in_month": 1,
            "period_label": "2026-07-W1",
            "is_partial_period": True,
            "entry_count": 1100,
            "included_count": 30,
            "excluded_count": 1000,
            "unknown_count": 70,
            "source_calculated_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        },
        {
            "month_start_date": date(2026, 7, 1),
            "period_start_date": date(2026, 7, 1),
            "period_end_date": date(2026, 8, 1),
            "period_sequence_in_month": 5,
            "period_label": "2026-07-W5",
            "is_partial_period": True,
            "entry_count": 1222,
            "included_count": 32,
            "excluded_count": 1100,
            "unknown_count": 90,
            "source_calculated_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        },
    ]
    reader = FakeRowReader({"weekly_periods": rows})
    periods = _provider().list_periods(reader, PeriodType.WEEKLY)
    assert [p.period_label for p in periods] == ["2026-07-W1", "2026-07-W5"]
    w5 = periods[1]
    assert w5.period_sequence_in_month == 5  # no special-casing of W1-W4
    assert w5.period_end_date == date(2026, 8, 1)  # exclusive end
    assert w5.is_partial_period is True
    assert w5.key.period_type is PeriodType.WEEKLY
    assert w5.lineage_quality is LineageQuality.RECONSTRUCTED_CURRENT_STATE
    assert w5.entry_counts_by_group["INCLUDED"] == 32
    # client scoping present in the query params
    assert reader.last_params("weekly_periods")["client_id"] == CLIENT_ID


def test_monthly_periods_can_be_empty() -> None:
    reader = FakeRowReader({"monthly_periods": []})
    periods = _provider().list_periods(reader, PeriodType.MONTHLY)
    assert periods == []


def test_period_key_token_roundtrip() -> None:
    assert RankingPeriodKey.from_token(W5_KEY.token) == W5_KEY
    monthly = RankingPeriodKey(
        period_type=PeriodType.MONTHLY,
        month_start_date=date(2026, 7, 1),
        period_start_date=date(2026, 7, 1),
        period_end_date=date(2026, 8, 1),
        period_sequence_in_month=None,
    )
    assert RankingPeriodKey.from_token(monthly.token) == monthly


# --- identifier contract -----------------------------------------------------

def test_assigned_id_is_opaque_text() -> None:
    reader = FakeRowReader({"single_entry": [make_entry_row(assigned_id="007")]})
    entry = _provider().get_ranking_entry(reader, WEEKLY_KEY, "007")
    assert isinstance(entry.assigned_id, str)
    assert entry.assigned_id == "007"
    assert entry.assigned_id != "7"
    # bound as a parameter, never cast
    assert reader.last_params("single_entry")["assigned_id"] == "007"


def test_get_ranking_entry_missing_raises() -> None:
    reader = FakeRowReader({"single_entry": []})
    assert_raises(
        RankingEntryNotFoundError,
        lambda: _provider().get_ranking_entry(reader, WEEKLY_KEY, "nope"),
    )


# --- ranking groups ----------------------------------------------------------

def test_persisted_group_not_moved_by_current_chart() -> None:
    # Persisted EXCLUDED even though the current chart now says included=True.
    reader = FakeRowReader(
        {
            "single_entry": [
                make_entry_row(
                    assigned_id="55",
                    ranking_group="EXCLUDED",
                    ranking_included=False,
                    ranking_position=3,
                    current_chart_present=True,
                    current_chart_ranking_included=True,
                )
            ]
        }
    )
    entry = _provider().get_ranking_entry(reader, WEEKLY_KEY, "55")
    assert entry.ranking_group is RankingGroup.EXCLUDED
    assert entry.ranking_included is False
    assert entry.current_chart_ranking_included is True
    assert entry.driver_metadata_source is DriverMetadataSource.CURRENT_CHART


def test_unknown_driver_group_preserved_without_chart() -> None:
    reader = FakeRowReader(
        {
            "single_entry": [
                make_entry_row(
                    assigned_id="99",
                    ranking_group="UNKNOWN_DRIVER",
                    ranking_included=None,
                    ranking_position=None,
                    ranking_total_participants=None,
                    current_driver_name=None,
                    current_chart_present=False,
                    current_chart_ranking_included=None,
                )
            ]
        }
    )
    entry = _provider().get_ranking_entry(reader, WEEKLY_KEY, "99")
    assert entry.ranking_group is RankingGroup.UNKNOWN_DRIVER
    assert entry.ranking_included is None
    assert entry.current_driver_name is None
    assert entry.driver_metadata_source is DriverMetadataSource.NONE


def test_list_entries_included_and_excluded() -> None:
    reader = FakeRowReader(
        {
            "entries_list": [make_entry_row(assigned_id="a", ranking_group="INCLUDED")],
            "entries_count": [{"total_count": 1}],
        }
    )
    page = _provider().list_ranking_entries(reader, WEEKLY_KEY, ranking_group="INCLUDED")
    assert page.total_count == 1
    assert page.items[0].ranking_group is RankingGroup.INCLUDED
    assert reader.last_params("entries_list")["ranking_group"] == "INCLUDED"


# --- trip membership / SQL contract -----------------------------------------

def test_classify_trip_contribution_boundaries() -> None:
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    base = dict(assigned_id="X", is_private_trip=False, aggregation_included=True)

    at_start = {**base, "trip_start_ts": start}
    at_end = {**base, "trip_start_ts": end}
    inside = {**base, "trip_start_ts": datetime(2026, 7, 10, tzinfo=timezone.utc)}
    private = {**inside, "is_private_trip": True}
    not_agg = {**inside, "aggregation_included": False}
    mismatch = {**inside, "assigned_id": "Y"}

    kw = dict(expected_assigned_id="X", period_start_ts=start, period_end_ts=end)
    assert classify_trip_contribution(at_start, **kw) is TripContribution.CONTRIBUTING
    assert classify_trip_contribution(at_end, **kw) is TripContribution.OUTSIDE_PERIOD
    assert classify_trip_contribution(private, **kw) is TripContribution.EXCLUDED_PRIVATE
    assert classify_trip_contribution(not_agg, **kw) is TripContribution.EXCLUDED_NOT_AGGREGATED
    assert classify_trip_contribution(mismatch, **kw) is TripContribution.ASSIGNED_ID_MISMATCH


def test_contributing_trips_sql_contract() -> None:
    reader = FakeRowReader(
        {
            "trips_list": [make_trip_row()],
            "trips_count": [{"total_count": 1}],
        }
    )
    page = _provider().list_contributing_trips(reader, WEEKLY_KEY, "12345")
    assert page.total_count == 1
    trip = page.items[0]
    assert trip.assigned_id == "12345"
    assert trip.client_trip_present is True
    assert set(trip.event_counts.keys()) == set(REQUIRED_METRICS)

    sql = reader.last_sql("trips_list")
    # boundary and exclusion enforced in SQL
    assert "a.trip_start_ts >= %(period_start_ts)s" in sql
    assert "a.trip_start_ts < %(period_end_ts)s" in sql
    # The inclusion clause is now the driver family's own, copied verbatim from
    # `_fetch_aggregate_rows` in its aggregation job — including the redundant
    # private-trip clause the job also keeps. The person family's clause is
    # deliberately different and is asserted in the arbitrary-week suite.
    assert "a.aggregation_included IS TRUE" in sql
    assert "a.is_private_trip IS FALSE" in sql
    # join uses (client_id, provider_trip_id)
    assert "ct.client_id = a.client_id AND ct.provider_trip_id = a.provider_trip_id" in sql
    # assigned id bound, not concatenated
    assert "a.assigned_id = %(assigned_id)s" in sql
    params = reader.last_params("trips_list")
    assert params["assigned_id"] == "12345"
    assert isinstance(params["period_start_ts"], datetime)


def test_pagination_contract() -> None:
    assert q.normalize_pagination(1, 100) == (1, 100, 0)
    assert q.normalize_pagination(3, 50) == (3, 50, 100)
    assert q.normalize_pagination(1, 500) == (1, 500, 0)
    assert_raises(InvalidPaginationError, lambda: q.normalize_pagination(0, 100))
    assert_raises(InvalidPaginationError, lambda: q.normalize_pagination(1, 0))
    assert_raises(InvalidPaginationError, lambda: q.normalize_pagination(1, 501))

    reader = FakeRowReader({"entries_list": [], "entries_count": [{"total_count": 0}]})
    assert_raises(
        InvalidPaginationError,
        lambda: _provider().list_ranking_entries(reader, WEEKLY_KEY, limit=1000),
    )


# --- SQL safety --------------------------------------------------------------

def test_sort_allowlist_enforced() -> None:
    reader = FakeRowReader({"entries_list": [], "entries_count": [{"total_count": 0}]})
    # valid sort works
    _provider().list_ranking_entries(reader, WEEKLY_KEY, sort_field="eco_driving_score_total", direction="desc")
    assert_raises(
        InvalidSortFieldError,
        lambda: _provider().list_ranking_entries(reader, WEEKLY_KEY, sort_field="; DROP TABLE"),
    )
    assert_raises(
        InvalidSortFieldError,
        lambda: _provider().list_ranking_entries(reader, WEEKLY_KEY, sort_field="ranking_position", direction="sideways"),
    )


def test_invalid_ranking_group_rejected() -> None:
    reader = FakeRowReader({"entries_list": [], "entries_count": [{"total_count": 0}]})
    assert_raises(
        InvalidRankingGroupError,
        lambda: _provider().list_ranking_entries(reader, WEEKLY_KEY, ranking_group="INCLUDED') OR 1=1--"),
    )


def test_assigned_id_with_punctuation_is_bound() -> None:
    nasty = "00;DROP TABLE eco_driver_weekly_stats--"
    reader = FakeRowReader({"single_entry": [make_entry_row(assigned_id=nasty)]})
    entry = _provider().get_ranking_entry(reader, WEEKLY_KEY, nasty)
    assert entry.assigned_id == nasty
    sql = reader.last_sql("single_entry")
    assert "DROP TABLE" not in sql
    assert reader.last_params("single_entry")["assigned_id"] == nasty


def test_no_provider_method_accepts_table_or_schema() -> None:
    forbidden = {"table", "tables", "schema", "schema_name", "table_name", "sql", "query"}
    method_names = [
        "list_periods",
        "list_ranking_entries",
        "get_ranking_entry",
        "list_contributing_trips",
        "reconcile_ranking_entry",
        "get_score_definition",
    ]
    provider = _provider()
    for name in method_names:
        sig = inspect.signature(getattr(provider, name))
        assert forbidden.isdisjoint(sig.parameters.keys()), name


def test_unsupported_period_type_guard() -> None:
    class FakeType:
        pass

    reader = FakeRowReader({})
    assert_raises(
        UnsupportedPeriodTypeError,
        lambda: _provider().list_periods(reader, FakeType()),  # type: ignore[arg-type]
    )


# --- reconciliation ----------------------------------------------------------

def _reconcile(reader) -> object:
    return _provider().reconcile_ranking_entry(reader, WEEKLY_KEY, "12345")


def test_reconcile_match_is_current_state() -> None:
    reader = FakeRowReader(
        {
            "single_entry": [make_entry_row()],
            "recon_totals": [make_totals_row()],
            "window_diag": [make_window_diag()],
        }
    )
    result = _reconcile(reader)
    assert result.reconciliation_status is ReconciliationStatus.MATCH
    assert result.lineage_quality is LineageQuality.RECONSTRUCTED_CURRENT_STATE
    assert result.mismatched_fields == ()
    assert result.reconstructed["eco_driving_score_total"] == Decimal("100")
    assert result.persisted["eco_driving_score_total"] == Decimal("100.00")


def test_reconcile_count_mismatch() -> None:
    reader = FakeRowReader(
        {
            "single_entry": [make_entry_row(trips_count=60)],
            "recon_totals": [make_totals_row(trips_count=59)],
            "window_diag": [make_window_diag(window_trips=59)],
        }
    )
    result = _reconcile(reader)
    assert result.reconciliation_status is ReconciliationStatus.MISMATCH
    assert "trips_count" in result.mismatched_fields


def test_reconcile_distance_mismatch() -> None:
    reader = FakeRowReader(
        {
            "single_entry": [make_entry_row(total_distance_meters=2200000)],
            "recon_totals": [make_totals_row(total_distance_meters=2100000)],
            "window_diag": [make_window_diag()],
        }
    )
    result = _reconcile(reader)
    assert result.reconciliation_status is ReconciliationStatus.MISMATCH
    assert "total_distance_meters" in result.mismatched_fields


def test_reconcile_no_trips() -> None:
    zero_entry = make_entry_row(
        assigned_id="empty",
        trips_count=0,
        total_distance_meters=0,
        total_kilometers=Decimal("0.000"),
        eco_driving_score_total=None,
        ecodriving_rating_type=None,
    )
    for metric in REQUIRED_METRICS:
        zero_entry[metric] = 0
        zero_entry[q.RATE_COLUMNS[metric]] = None
        zero_entry[q.POINT_COLUMNS[metric]] = None
    reader = FakeRowReader(
        {
            "single_entry": [zero_entry],
            "recon_totals": [make_totals_row(trips_count=0, total_distance_meters=0)],
            "window_diag": [make_window_diag(window_trips=0)],
        }
    )
    result = _provider().reconcile_ranking_entry(reader, WEEKLY_KEY, "empty")
    assert result.reconciliation_status is ReconciliationStatus.MATCH
    assert result.diagnostics["reconstructed_trips_count"] == 0


def test_reconcile_reports_missing_source_row() -> None:
    reader = FakeRowReader(
        {
            "single_entry": [make_entry_row()],
            "recon_totals": [make_totals_row(missing_client_trip_count=2)],
            "window_diag": [make_window_diag()],
        }
    )
    result = _reconcile(reader)
    assert result.diagnostics["missing_client_trip_count"] == 2


def test_reconcile_decimal_rounding_matches_eco_scoring() -> None:
    # overrev rate = 44 / (2,200,000/1000) * 100 = 2.0 -> score_metric(overrev, 2) = 11
    total_km = q.total_kilometers(2200000)
    assert total_km == Decimal("2200.000")
    rate = q.stored_rate_per_100km(44, total_km)
    assert rate == Decimal("2")
    expected = calculate_eco_score({m: (rate if m == "overrev_events_count" else Decimal("0")) for m in REQUIRED_METRICS})
    # overrev loses 4 points (15 -> 11): total 96
    assert expected["eco_driving_score_total"] == 96

    entry = make_entry_row(eco_driving_score_total=Decimal("96.00"))
    entry["overrev_events_count"] = 44
    entry[q.RATE_COLUMNS["overrev_events_count"]] = Decimal("2")
    entry[q.POINT_COLUMNS["overrev_events_count"]] = Decimal("11")
    totals = make_totals_row(overrev_events_count=44)
    reader = FakeRowReader(
        {
            "single_entry": [entry],
            "recon_totals": [totals],
            "window_diag": [make_window_diag()],
        }
    )
    result = _reconcile(reader)
    assert result.reconciliation_status is ReconciliationStatus.MATCH
    assert result.reconstructed["eco_driving_score_total"] == Decimal("96")


def test_score_definition_shape() -> None:
    definition = _provider().get_score_definition()
    assert definition.ranking_family == "driver"
    assert definition.max_possible_score == 100
    assert len(definition.metrics) == len(REQUIRED_METRICS)
    keys = {m.metric_key for m in definition.metrics}
    assert keys == set(REQUIRED_METRICS)


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"Eco Driving Explorer provider tests passed ({len(tests)} cases)")


if __name__ == "__main__":
    main()
