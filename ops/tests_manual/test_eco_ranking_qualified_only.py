"""Regression coverage for the QUALIFIED-only ECO ranking eligibility contract.

Ranking eligibility is gated on `qualification_status`: only `QUALIFIED` rows
belong to a ranking group, receive a ranking position, or contribute to
`ranking_total_participants`. `LOW_DISTANCE` and `NO_DISTANCE` rows sit outside
every ranking population (`ranking_group is None`) and must never displace a
QUALIFIED participant or inflate a denominator.

That is an ELIGIBILITY contract, not a visibility one. The Explorer's
`w rankingu` tab lists the client's ranking population, so a driver the roster
permitted who fell under the threshold is shown there — unranked, group-less
and outside every denominator this file asserts. The tab's SQL is checked below
for exactly that distinction.

Both implementations are exercised through their real production helpers
(`_stats_row_from_aggregate`, `_ranking_group`, `_apply_rankings`,
`_count_rank_groups`) rather than a test-only reimplementation.

Run: PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_ranking_qualified_only.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from jobs.ecodriving import job_eco_driving_aggregate as DRIVER
from jobs.ecodriving_person import job_eco_driving_person_aggregate as PERSON

METRICS = DRIVER.REQUIRED_METRICS
MONTH_START = date(2026, 6, 1)
MONTH_END = date(2026, 7, 1)

DRIVER_PERIOD = DRIVER.RankingPeriod(
    period_start_date=MONTH_START,
    period_end_date=date(2026, 6, 8),
    month_start_date=MONTH_START,
    period_sequence_in_month=1,
    period_label="2026-06-W1",
    is_partial_period=False,
)
PERSON_PERIOD = PERSON.RankingPeriod(
    period_start_date=MONTH_START,
    period_end_date=date(2026, 6, 8),
    month_start_date=MONTH_START,
    period_sequence_in_month=1,
    period_label="2026-06-W1",
    is_partial_period=False,
)


# --- variant abstraction -----------------------------------------------------
# The two implementations differ only in identity column and chart-presence
# column; everything asserted below is the shared eligibility contract.

class Variant:
    def __init__(self, name, module, id_key, chart_key, period, extra_row=None):
        self.name = name
        self.module = module
        self.id_key = id_key
        self.chart_key = chart_key
        self.period = period
        self.extra_row = extra_row or {}

    def source_row(self, ident, *, meters, chart=True, ranking_included=True, events=0):
        row = {
            "client_id": "11111111-1111-1111-1111-111111111111",
            "client_code": self.name,
            self.id_key: ident,
            "trips_count": 4,
            "source_trips_count": 4,
            "skipped_trips_count": 0,
            "total_distance_meters": meters,
            self.chart_key: ident if chart else None,
            "ranking_included": ranking_included if chart else None,
        }
        row.update(self.extra_row)
        for metric in METRICS:
            row[metric] = events
        return row

    def build(self, source_rows, *, monthly=False):
        """Run the real aggregation pipeline: stats -> rankings -> rating share."""
        if monthly:
            source_rows = [
                dict(r, month_start_date=MONTH_START, month_end_date=MONTH_END)
                for r in source_rows
            ]
        stats = [
            self.module._stats_row_from_aggregate(
                r, None if monthly else self.period, monthly=monthly
            )
            for r in source_rows
        ]
        self.module._apply_rankings(stats, monthly=monthly)
        self.module._apply_rating_type_share_percent(stats, monthly=monthly)
        return {row[self.id_key]: row for row in stats}


VARIANTS = (
    Variant("ALPHA00001", DRIVER, "assigned_id", "driver_id", DRIVER_PERIOD),
    Variant(
        "BRAVO00016",
        PERSON,
        "person_name_group_key",
        "physical_person_id",
        PERSON_PERIOD,
        extra_row={"person_name": "Test Person", "is_active": True},
    ),
)

QUALIFYING_METERS = 250_000
LOW_DISTANCE_METERS = 50_000
NO_DISTANCE_METERS = 0

_failures: list[str] = []
_checks = 0


def check(label, actual, expected):
    global _checks
    _checks += 1
    if actual != expected:
        _failures.append(f"{label}: expected {expected!r}, got {actual!r}")


def assert_not_ranked(variant, row, label):
    """The complete 'outside every ranking population' assertion."""
    check(f"[{variant.name}] {label} ranking_group", row["ranking_group"], None)
    check(f"[{variant.name}] {label} ranking_position", row["ranking_position"], None)
    check(f"[{variant.name}] {label} ranking_total_participants",
          row["ranking_total_participants"], None)


def assert_ranked(variant, row, label, *, group, position, total):
    check(f"[{variant.name}] {label} ranking_group", row["ranking_group"], group)
    check(f"[{variant.name}] {label} ranking_position", row["ranking_position"], position)
    check(f"[{variant.name}] {label} ranking_total_participants",
          row["ranking_total_participants"], total)


# --- Scenario 1: LOW_DISTANCE excluded from the INCLUDED ranking -------------

def scenario_1_low_distance_excluded(variant, *, monthly=False):
    suffix = "monthly" if monthly else "weekly"
    rows = variant.build(
        [
            # A scores higher than B; C is LOW_DISTANCE but would outscore both.
            variant.source_row("A", meters=QUALIFYING_METERS, events=0),
            variant.source_row("B", meters=QUALIFYING_METERS, events=20),
            variant.source_row("C", meters=LOW_DISTANCE_METERS, events=0),
        ],
        monthly=monthly,
    )
    check(f"[{variant.name}] s1/{suffix} C qualification",
          rows["C"]["qualification_status"], "LOW_DISTANCE")
    # C has the best possible score and a chart entry, yet must not rank.
    check(f"[{variant.name}] s1/{suffix} C keeps its score",
          rows["C"]["eco_driving_score_total"] is not None, True)
    check(f"[{variant.name}] s1/{suffix} C keeps chart config",
          rows["C"]["ranking_included"], True)
    assert_ranked(variant, rows["A"], f"s1/{suffix} A", group="INCLUDED", position=1, total=2)
    assert_ranked(variant, rows["B"], f"s1/{suffix} B", group="INCLUDED", position=2, total=2)
    assert_not_ranked(variant, rows["C"], f"s1/{suffix} C")


# --- Scenario 2: NO_DISTANCE excluded ---------------------------------------

def scenario_2_no_distance_excluded(variant):
    rows = variant.build(
        [
            variant.source_row("A", meters=QUALIFYING_METERS, events=0),
            variant.source_row("B", meters=QUALIFYING_METERS, events=20),
            variant.source_row("Z", meters=NO_DISTANCE_METERS, events=0),
        ]
    )
    check(f"[{variant.name}] s2 Z qualification",
          rows["Z"]["qualification_status"], "NO_DISTANCE")
    assert_not_ranked(variant, rows["Z"], "s2 Z")
    # QUALIFIED participants are untouched by the presence of the NO_DISTANCE row.
    assert_ranked(variant, rows["A"], "s2 A", group="INCLUDED", position=1, total=2)
    assert_ranked(variant, rows["B"], "s2 B", group="INCLUDED", position=2, total=2)


# --- Scenario 3: EXCLUDED population ----------------------------------------

def scenario_3_excluded_population(variant):
    rows = variant.build(
        [
            variant.source_row("A", meters=QUALIFYING_METERS, events=0),
            variant.source_row("E1", meters=QUALIFYING_METERS, events=0, ranking_included=False),
            variant.source_row("E2", meters=QUALIFYING_METERS, events=20, ranking_included=False),
            variant.source_row("L", meters=LOW_DISTANCE_METERS, events=0, ranking_included=False),
        ]
    )
    assert_ranked(variant, rows["A"], "s3 A", group="INCLUDED", position=1, total=1)
    assert_ranked(variant, rows["E1"], "s3 E1", group="EXCLUDED", position=1, total=2)
    assert_ranked(variant, rows["E2"], "s3 E2", group="EXCLUDED", position=2, total=2)
    assert_not_ranked(variant, rows["L"], "s3 L")
    check(f"[{variant.name}] s3 L keeps chart config", rows["L"]["ranking_included"], False)


# --- Scenario 4: UNKNOWN_DRIVER stays reserved for QUALIFIED -----------------

def scenario_4_unknown_driver(variant):
    rows = variant.build(
        [
            variant.source_row("Q", meters=QUALIFYING_METERS, events=0, chart=False),
            variant.source_row("L", meters=LOW_DISTANCE_METERS, events=0, chart=False),
            variant.source_row("Z", meters=NO_DISTANCE_METERS, events=0, chart=False),
        ]
    )
    # QUALIFIED without a chart mapping keeps the historical UNKNOWN_DRIVER behavior.
    check(f"[{variant.name}] s4 Q ranking_group", rows["Q"]["ranking_group"], "UNKNOWN_DRIVER")
    check(f"[{variant.name}] s4 Q ranking_position", rows["Q"]["ranking_position"], None)
    check(f"[{variant.name}] s4 Q ranking_total_participants",
          rows["Q"]["ranking_total_participants"], None)
    # Non-qualified rows must NOT fall back to UNKNOWN_DRIVER.
    assert_not_ranked(variant, rows["L"], "s4 L")
    assert_not_ranked(variant, rows["Z"], "s4 Z")


# --- Scenario 5: threshold transition ---------------------------------------

def scenario_5_threshold_transition(variant):
    below = variant.build([variant.source_row("T", meters=99_999, events=0)])["T"]
    check(f"[{variant.name}] s5 99.999km qualification",
          below["qualification_status"], "LOW_DISTANCE")
    check(f"[{variant.name}] s5 99.999km total_kilometers",
          str(below["total_kilometers"]), "99.999")
    assert_not_ranked(variant, below, "s5 99.999km")

    at = variant.build([variant.source_row("T", meters=100_000, events=0)])["T"]
    check(f"[{variant.name}] s5 100.000km qualification",
          at["qualification_status"], "QUALIFIED")
    check(f"[{variant.name}] s5 100.000km total_kilometers",
          str(at["total_kilometers"]), "100.000")
    assert_ranked(variant, at, "s5 100.000km", group="INCLUDED", position=1, total=1)


# --- Scenario 6: participant denominator ------------------------------------

def scenario_6_participant_denominator(variant):
    source = [
        variant.source_row(f"Q{i:02d}", meters=QUALIFYING_METERS, events=i)
        for i in range(10)
    ] + [
        variant.source_row(f"L{i}", meters=LOW_DISTANCE_METERS, events=0)
        for i in range(3)
    ]
    rows = variant.build(source)

    for i in range(10):
        check(f"[{variant.name}] s6 Q{i:02d} total participants",
              rows[f"Q{i:02d}"]["ranking_total_participants"], 10)
    positions = sorted(rows[f"Q{i:02d}"]["ranking_position"] for i in range(10))
    check(f"[{variant.name}] s6 positions are 1..10", positions, list(range(1, 11)))
    for i in range(3):
        assert_not_ranked(variant, rows[f"L{i}"], f"s6 L{i}")

    counts = variant.module._count_rank_groups(list(rows.values()))
    check(f"[{variant.name}] s6 group counters (incl, excl, unknown, not_ranked)",
          counts, (10, 0, 0, 3))


# --- Scenario 7: weekly and monthly share the contract ----------------------

def scenario_7_weekly_and_monthly(variant):
    scenario_1_low_distance_excluded(variant, monthly=False)
    scenario_1_low_distance_excluded(variant, monthly=True)


# --- Direct gate unit check -------------------------------------------------

def scenario_gate_matrix(variant):
    gate = variant.module._ranking_group
    check(f"[{variant.name}] gate QUALIFIED+chart+true", gate("QUALIFIED", True, True), "INCLUDED")
    check(f"[{variant.name}] gate QUALIFIED+chart+false", gate("QUALIFIED", True, False), "EXCLUDED")
    check(f"[{variant.name}] gate QUALIFIED+no chart", gate("QUALIFIED", False, None), "UNKNOWN_DRIVER")
    for status in ("LOW_DISTANCE", "NO_DISTANCE"):
        for chart, included in ((True, True), (True, False), (False, None)):
            check(f"[{variant.name}] gate {status}+chart={chart}+incl={included}",
                  gate(status, chart, included), None)

    # Biconditional enforced by chk_<table>_ranking_requires_qualified in
    # migration 046: a row has a ranking group if and only if it is QUALIFIED.
    # Both directions must hold, so QUALIFIED never yields None.
    for status in ("QUALIFIED", "LOW_DISTANCE", "NO_DISTANCE"):
        for chart, included in ((True, True), (True, False), (False, None)):
            check(f"[{variant.name}] biconditional {status}+chart={chart}+incl={included}",
                  gate(status, chart, included) is not None, status == "QUALIFIED")


# --- Explorer / API surface --------------------------------------------------
# The ALPHA Explorer must expose a non-qualified row as a report row with no
# ranking coordinates, and must never count it into a ranking population.

def scenario_explorer_not_ranked_entry():
    from api.eco_driving_explorer import queries as eq
    from api.eco_driving_explorer.models import PeriodType, RankingPeriodKey
    from api.eco_driving_explorer.alpha_driver_provider import AlphaDriverEcoDrivingProvider
    from api.eco_driving_explorer.serialization import serialize_ranking_entry

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_eco_driving_explorer_provider import FakeRowReader, make_entry_row

    row = make_entry_row(
        assigned_id="LD1",
        ranking_group=None,
        ranking_included=True,
        ranking_position=None,
        ranking_total_participants=None,
        qualification_status="LOW_DISTANCE",
        ecodriving_rating_type_share_percent=None,
    )
    key = RankingPeriodKey(
        period_type=PeriodType.WEEKLY,
        month_start_date=MONTH_START,
        period_start_date=MONTH_START,
        period_end_date=date(2026, 6, 8),
        period_sequence_in_month=1,
    )
    provider = AlphaDriverEcoDrivingProvider(client_id="11111111-1111-1111-1111-111111111111")
    reader = FakeRowReader({"single_entry": [row]})
    entry = provider.get_ranking_entry(reader, key, assigned_id="LD1")

    check("[explorer] entry ranking_group", entry.ranking_group, None)
    check("[explorer] entry ranking_position", entry.ranking_position, None)
    check("[explorer] entry ranking_total_participants", entry.ranking_total_participants, None)
    check("[explorer] qualification stays visible", entry.qualification_status, "LOW_DISTANCE")

    body = serialize_ranking_entry(entry)
    check("[explorer] serialized ranking_group", body["ranking_group"], None)
    check("[explorer] serialized ranking_position", body["ranking_position"], None)
    check("[explorer] serialized qualification_status", body["qualification_status"], "LOW_DISTANCE")

    # Group summaries count only real ranking populations; NULL is reported apart.
    for sql in (
        eq.weekly_periods_sql(with_year=False, with_month=False),
        eq.monthly_periods_sql(with_year=False, with_month=False),
    ):
        check("[explorer] summary counts not-ranked separately",
              "COUNT(*) FILTER (WHERE ranking_group IS NULL) AS not_ranked_count" in sql, True)
        for group in ("INCLUDED", "EXCLUDED", "UNKNOWN_DRIVER"):
            check(f"[explorer] summary still counts {group}",
                  f"FILTER (WHERE ranking_group = '{group}')" in sql, True)

    # PRESENTATION, not eligibility. The `INCLUDED` tab is the client's ranking
    # POPULATION, so it also lists a permitted driver whose period fell under
    # the threshold — still group-less, position-less and outside every
    # denominator asserted above. `EXCLUDED` and `UNKNOWN_DRIVER` describe no
    # permission and keep plain equality, so a NULL group cannot reach them.
    included = eq.entries_sql(
        weekly=True, ranking_group="INCLUDED", order_by="s.assigned_id ASC"
    )
    check("[explorer] the INCLUDED tab still matches its own group",
          "s.ranking_group = %(ranking_group)s" in included, True)
    check("[explorer] and admits the permitted below-threshold population",
          "(s.ranking_group IS NULL AND s.ranking_included IS TRUE)" in included, True)
    for group in ("EXCLUDED", "UNKNOWN_DRIVER"):
        sql = eq.entries_sql(
            weekly=True, ranking_group=group, order_by="s.assigned_id ASC"
        )
        check(f"[explorer] {group} filter uses equality alone",
              "s.ranking_group = %(ranking_group)s" in sql
              and "ranking_included IS TRUE" not in sql, True)
    # The count must ask the tab's question, not the group's, or the chip and
    # the pagination would disagree with the rows underneath them.
    count_sql = eq.entries_count_sql(weekly=True, ranking_group="INCLUDED")
    check("[explorer] the INCLUDED count matches the INCLUDED listing",
          "(ranking_group IS NULL AND ranking_included IS TRUE)" in count_sql, True)
    # And the period summary counts that population apart from the ranked one.
    for sql in (
        eq.weekly_periods_sql(with_year=False, with_month=False),
        eq.monthly_periods_sql(with_year=False, with_month=False),
    ):
        check("[explorer] summary counts the permitted-unranked population",
              "ranking_group IS NULL AND ranking_included IS TRUE" in sql
              and "AS not_ranked_included_count" in sql, True)


def main() -> int:
    scenario_explorer_not_ranked_entry()
    for variant in VARIANTS:
        scenario_gate_matrix(variant)
        scenario_1_low_distance_excluded(variant)
        scenario_2_no_distance_excluded(variant)
        scenario_3_excluded_population(variant)
        scenario_4_unknown_driver(variant)
        scenario_5_threshold_transition(variant)
        scenario_6_participant_denominator(variant)
        scenario_7_weekly_and_monthly(variant)

    if _failures:
        print(f"FAIL — {len(_failures)} of {_checks} checks failed:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"OK — {_checks} checks passed for {len(VARIANTS)} ECO variants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
