#!/usr/bin/env python3
"""S12 — `ECO_DRIVING_PRESENTATION_NON_BLOCKED_SUBSET`.

Deterministic checks for the modernized Eco Driving presentation. No database
and no HTTP client: the framework-independent controllers run against the
reusable fake backend and synthetic client-database reader.

The suite is organised around the claims that are cheapest to get wrong and
most expensive to get wrong silently:

* **the adversarial unit/colour fixture** — two drivers built so that the raw
  event count and the normalized coefficient imply *opposite* naive colour
  choices. If any severity ever starts reading the printed number, exactly one
  of these two rows flips and the assertion fails;
* **the 100 km boundary** — 99 999 m, 100 000 m and above, plus a period that
  qualifies while containing a very short trip, which must stay listed;
* **composition arithmetic** — persisted losses, their total, the share, the
  zero-loss case and the deterministic tie order;
* **histogram and trend scope** — one client, one period, one ranking group,
  chronological, never zero-filled;
* **the deliberate removals** — asserted as negatives against real rendered
  pages so a removed panel cannot quietly return.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_eco_driving_presentation_s12.py
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_eco_driving_explorer_api as api_t  # noqa: E402
import test_eco_driving_explorer_provider as prov  # noqa: E402

from api.eco_driving_explorer import detail_view_models as D  # noqa: E402
from api.eco_driving_explorer import eco_view as V  # noqa: E402
from api.eco_driving_explorer import html as H  # noqa: E402
from api.eco_driving_explorer import queries as q  # noqa: E402
from api.eco_driving_explorer import score_presentation as SP  # noqa: E402
from api.eco_driving_explorer.eco_scoring import (  # noqa: E402
    METRIC_MAX_POINTS,
    MIN_QUALIFYING_DISTANCE_METERS,
    RATING_THRESHOLDS,
    REQUIRED_METRICS,
    SCORING_RULES,
    calculate_maxpoints_subtractions,
    calculate_eco_score,
)
from api.eco_driving_explorer.pages import EcoDrivingPages  # noqa: E402
from api.eco_driving_explorer.service import EcoDrivingApiService  # noqa: E402
from api.portal_ui import assets as portal_assets  # noqa: E402
from api.portal_ui.i18n import t  # noqa: E402

CC, FAMILY, TOKEN, USER = api_t.CLIENT_CODE, api_t.FAMILY, api_t.WEEKLY_TOKEN, api_t.USER
KEY = prov.WEEKLY_KEY

PASSES: list[str] = []


def ok(message: str) -> None:
    PASSES.append(message)
    print(f"PASS: {message}")


# --- fixtures ----------------------------------------------------------------


def entry_row(**overrides) -> dict:
    """A persisted stats row with every S12 column present.

    Losses default to the same ``min(points - max, 0)`` the aggregation job
    persists, so a fixture cannot accidentally encode a different loss model.
    """

    row = prov.make_entry_row()
    for metric in REQUIRED_METRICS:
        row[q.SUBTRACT_COLUMNS[metric]] = Decimal(0)
    row.update(overrides)
    return row


def scored_row(rates: dict[str, int], *, distance_meters: int = 2_200_000, **overrides) -> dict:
    """A row scored end-to-end by the repository's own scoring helper.

    Points, losses and the total score are produced by ``eco_scoring`` rather
    than written by hand, so the fixture cannot drift away from production
    scoring.
    """

    total_km = q.total_kilometers(distance_meters)
    result = calculate_eco_score({m: Decimal(rates.get(m, 0)) for m in REQUIRED_METRICS})
    losses = calculate_maxpoints_subtractions(result)
    row = entry_row(
        total_distance_meters=distance_meters,
        total_kilometers=total_km,
        eco_driving_score_total=Decimal(result["eco_driving_score_total"]),
    )
    for metric in REQUIRED_METRICS:
        rate = Decimal(rates.get(metric, 0))
        row[metric] = int((rate * total_km / Decimal(100)).to_integral_value())
        row[q.RATE_COLUMNS[metric]] = rate
        row[q.POINT_COLUMNS[metric]] = Decimal(result["metric_points"][metric])
        row[q.SUBTRACT_COLUMNS[metric]] = Decimal(losses[metric])
    row.update(overrides)
    return row


def responses(**extra) -> dict:
    base = {
        "entries_list": [entry_row()],
        "entries_count": [{"total_count": 1}],
        "single_entry": [entry_row()],
        "weekly_periods": weekly_periods(),
        "monthly_periods": [],
        "score_distribution": [],
        "driver_trend": [],
        "period_progression": [],
        "trips_list": [],
        "trips_count": [{"total_count": 0}],
        "recon_totals": [prov.make_totals_row()],
        "window_diag": [prov.make_window_diag()],
    }
    base.update(extra)
    return base


def weekly_periods() -> list[dict]:
    """Two persisted weekly periods, the second one being the fixture's period."""

    return [
        {
            "month_start_date": date(2026, 7, 1),
            "period_start_date": date(2026, 7, 1),
            "period_end_date": date(2026, 7, 20),
            "period_sequence_in_month": 3,
            "period_label": "2026-07-W3",
            "is_partial_period": False,
            "included_count": 30, "excluded_count": 900, "unknown_count": 20,
            "not_ranked_count": 500,
            "source_calculated_at": None,
        },
        {
            "month_start_date": KEY.month_start_date,
            "period_start_date": KEY.period_start_date,
            "period_end_date": KEY.period_end_date,
            "period_sequence_in_month": KEY.period_sequence_in_month,
            "period_label": "2026-07-W4",
            "is_partial_period": False,
            "included_count": 32, "excluded_count": 982, "unknown_count": 32,
            "not_ranked_count": 474,
            # Of those 474, twelve hold ranking permission and simply drove too
            # little: they are listed on the `w rankingu` tab, so its chip
            # counts 32 + 12.
            "not_ranked_included_count": 12,
            "source_calculated_at": None,
        },
    ]


def make(*, trip=False, routes=False, data=None):
    access = {(USER["user_id"], CC): api_t._access(ranking=True, trip=trip, routes=routes)}
    backend = api_t.FakeBackend(access_map=access, responses=data or responses())
    return EcoDrivingPages(EcoDrivingApiService(backend)), backend


def ranking(controller, **extra):
    args = dict(user=USER, client_code=CC, ranking_family=FAMILY, period_key=TOKEN)
    args.update(extra)
    return controller.rankings(**args)


def detail(controller, **extra):
    args = dict(user=USER, client_code=CC, ranking_family=FAMILY,
                period_key=TOKEN, assigned_id="12345")
    args.update(extra)
    return controller.ranking_entry(**args)


def cells(body: str) -> list[tuple[str, str]]:
    """Every metric cell as ``(severity, text)``, in document order."""

    out = []
    cursor = 0
    marker = 'data-severity="'
    while True:
        found = body.find(marker, cursor)
        if found == -1:
            return out
        start = found + len(marker)
        end = body.index('"', start)
        severity = body[start:end]
        close = body.index("</td>", end)
        out.append((severity, body[end:close]))
        cursor = close


# --- 1. the adversarial unit / colour fixture --------------------------------


def test_colour_follows_the_coefficient_not_the_count():
    """Two rows whose count and coefficient point in opposite directions.

    ``long_haul`` drives 10 000 km and racks up 200 harsh-braking events — a
    large, alarming-looking count — but only 2 events per 100 km, which the
    repository ladder scores at a rung that loses nothing. ``short_haul`` drives
    150 km with 15 events — a small count — but 10 per 100 km, which the ladder
    scores into negative points.

    A colour driven by the printed number would paint them the wrong way round
    in at least one of the two display modes. The assertion is therefore made in
    **both** modes, and the severities must be identical across them.
    """

    metric = "harsh_turning_events"
    # 10 000 km and 400 cornering events -> 4 per 100 km, the ladder's top rung.
    long_haul = scored_row({metric: 4}, distance_meters=10_000_000,
                           assigned_id="LONG", ranking_position=1)
    # 150 km and 34 events -> 23 per 100 km, a rung that subtracts points.
    short_haul = scored_row({metric: 23}, distance_meters=150_000,
                            assigned_id="SHORT", ranking_position=2)

    # The fixture really is adversarial: the big count is the safe driver.
    assert long_haul[metric] > short_haul[metric], (long_haul[metric], short_haul[metric])
    assert long_haul[q.RATE_COLUMNS[metric]] < short_haul[q.RATE_COLUMNS[metric]]

    index = V.RANKING_METRIC_ORDER.index(metric)
    seen = {}
    for unit in ("rate", "sum"):
        controller, _ = make(data=responses(
            entries_list=[long_haul, short_haul], entries_count=[{"total_count": 2}]
        ))
        body = ranking(controller, unit=unit).body_html
        metric_cells = cells(body)
        per_row = len(V.RANKING_METRIC_ORDER)
        assert len(metric_cells) == 2 * per_row, len(metric_cells)
        long_severity, long_text = metric_cells[index]
        short_severity, short_text = metric_cells[per_row + index]

        assert long_severity == SP.SEVERITY_OK, (unit, long_severity)
        assert short_severity == SP.SEVERITY_BAD, (unit, short_severity)

        if unit == "sum":
            # The printed value really did change, and it is the raw count.
            assert str(long_haul[metric]) in long_text.replace("\u202f", "").replace(" ", "")
            assert str(short_haul[metric]) in short_text.replace("\u202f", "").replace(" ", "")
        else:
            assert "4,00" in long_text
            assert "23,00" in short_text
        seen[unit] = (long_severity, short_severity)

    assert seen["rate"] == seen["sum"], seen
    ok("semantic colour is coefficient-derived and identical in / 100 km and Σ mode")


def test_no_raw_count_thresholds_exist_in_the_presentation_layer():
    """The severity API structurally cannot see a count.

    ``metric_severity`` accepts points and loss only. There is no count
    parameter to pass, so a raw-count threshold cannot be introduced without
    changing this signature — which this assertion pins.
    """

    import inspect

    params = set(inspect.signature(SP.metric_severity).parameters)
    assert params == {"metric_key", "points", "loss"}, params
    params = set(inspect.signature(SP.severity_from_rate).parameters)
    assert params == {"metric_key", "rate"}, params

    # And the value the cell prints is a separate argument from the values it
    # classifies by.
    params = set(inspect.signature(V.metric_cell).parameters)
    assert "displayed_value" in params and "points" in params and "loss" in params
    ok("no raw-count threshold can enter severity: the API takes no count")


def test_unit_toggle_changes_nothing_but_the_printed_number():
    row = scored_row({"harsh_braking_events": 4, "speeding_140_160_count": 3})
    rendered = {}
    for unit in ("rate", "sum"):
        controller, _ = make(data=responses(entries_list=[row], entries_count=[{"total_count": 1}]))
        result = ranking(controller, unit=unit)
        assert result.status_code == 200
        rendered[unit] = result.body_html

    for body in rendered.values():
        # score, position, rating band and qualification are identical
        assert f">{row['ranking_position']}<" in body
        assert "bezpieczny" in body
        assert t("eco.qualification.met") in body
    assert [s for s, _ in cells(rendered["rate"])] == [s for s, _ in cells(rendered["sum"])]

    # The toggle is two real links with a programmatic current state, and the
    # unit rides the URL so the view is shareable and works without scripting.
    assert 'aria-current="true"' in rendered["rate"]
    assert "unit=sum" in rendered["rate"] and "unit=rate" in rendered["sum"]
    assert t("eco.unit.rate") in rendered["rate"] and t("eco.unit.sum") in rendered["rate"]
    ok("the / 100 km ⇄ Σ toggle changes only the printed value, and rides the URL")


# --- 2. the 100 km reporting-period rule -------------------------------------


def test_hundred_km_threshold_is_a_reporting_period_rule():
    """Boundary behaviour, taken from the repository's own gate.

    ``_qualification_and_calculation`` qualifies at exactly
    ``MIN_QUALIFYING_DISTANCE_METERS``; 99 999 m is ``LOW_DISTANCE``. The
    boundary is asserted in metres, never inferred from a rounded kilometre
    display.
    """

    assert MIN_QUALIFYING_DISTANCE_METERS == 100_000

    cases = (
        (99_999, "LOW_DISTANCE", False),
        (MIN_QUALIFYING_DISTANCE_METERS, "QUALIFIED", True),
        (250_000, "QUALIFIED", True),
    )
    for meters, status, expect_metrics in cases:
        row = scored_row({"harsh_braking_events": 4}, distance_meters=meters,
                         qualification_status=status,
                         ranking_group="INCLUDED" if status == "QUALIFIED" else None,
                         ranking_position=1 if status == "QUALIFIED" else None)
        controller, _ = make(data=responses(single_entry=[row]))
        body = detail(controller).body_html
        if expect_metrics:
            assert t("eco.state.insufficient_title") not in body, meters
            assert t("eco.comp.lost") in body, meters
        else:
            assert t("eco.state.insufficient_title") in body, meters
            # No score, no position hero and no composition table for the period.
            assert t("eco.comp.lost") not in body, meters
            assert "eco-hero-value" not in body, meters
    ok("100 km qualification is applied to the reporting period, at the exact metre boundary")


def test_no_distance_state_is_distinct_from_low_distance():
    row = entry_row(qualification_status="NO_DISTANCE", calculation_status="NO_DISTANCE",
                    total_distance_meters=0, total_kilometers=Decimal("0"),
                    eco_driving_score_total=None, ranking_group=None, ranking_position=None)
    controller, _ = make(data=responses(single_entry=[row]))
    body = detail(controller).body_html
    assert t("eco.state.no_distance_title") in body
    assert t("eco.state.insufficient_title") not in body
    ok("a zero-distance period gets its own state, not the below-threshold one")


def test_a_short_day_inside_an_eligible_period_is_not_suppressed():
    """No daily gate exists, and none may be introduced.

    The period qualifies on its total. A 4 km trip inside it is still a
    contributing trip and must still be listed: the threshold is evaluated once,
    for the whole period.
    """

    # `UI-20260820-02` B replaced the provider trip id with the vehicle plate in
    # this preview table, so the two rows are identified here by what the page
    # actually renders: their plates and their distances. The assertion still
    # proves exactly what it always proved — both contributing trips are listed,
    # and the short one is not suppressed.
    tiny = prov.make_trip_row(provider_trip_id=999_001, trip_distance_meters=4_000,
                              vehicle_registration="WX 99001")
    normal = prov.make_trip_row(provider_trip_id=999_002, trip_distance_meters=120_000,
                                vehicle_registration="WX 99002")
    controller, backend = make(trip=True, data=responses(
        single_entry=[scored_row({"harsh_braking_events": 1}, distance_meters=2_200_000)],
        trips_list=[tiny, normal],
        trips_count=[{"total_count": 2}],
    ))
    body = detail(controller).body_html
    assert "WX 99001" in body and "WX 99002" in body
    # UI-20260820-07 standardised distance on `X,YY km` from the stored metres.
    # The proposition is unchanged: both contributing trips are listed, and the
    # short one is not suppressed. Only the rendering of the distance moved.
    assert ">4,00 km<" in body and ">120,00 km<" in body
    # The trip query carries no distance floor of its own.
    sql = backend.reader.last_sql("trips_list")
    assert "min_distance_meters" not in sql
    assert str(MIN_QUALIFYING_DISTANCE_METERS) not in sql
    ok("a low-distance day inside an eligible period stays represented; no daily gate")


# --- 3. ranking groups -------------------------------------------------------


def test_group_chips_replace_tabs_and_keep_the_three_groups_distinct():
    controller, _ = make()
    body = ranking(controller).body_html
    for key in ("eco.group.included", "eco.group.excluded", "eco.group.unknown"):
        assert t(key) in body, key
    # Counts are visible, and they come from the persisted period counters.
    # `w rankingu` counts its whole population: 32 ranked + 12 permitted but
    # below the threshold.
    assert ">982<" in body and ">32<" in body and ">44<" in body
    assert 'class="eco-tabs"' not in body and "eco-tab active" not in body
    assert 'role="group"' in body and 'aria-current="true"' in body
    # Each group is still its own filtered ranking; they are never merged.
    for group in ("INCLUDED", "EXCLUDED", "UNKNOWN_DRIVER"):
        assert f"ranking_group={group}" in body
    ok("ranking-group tabs are replaced by chips with counts; the three groups stay distinct")


def test_permitted_below_threshold_driver_is_listed_without_a_score():
    """The `w rankingu` tab is the ranking POPULATION, not just its ranked part.

    A driver the roster permitted who did not reach the qualifying distance is
    listed there — with the facts that survive the threshold (identity, distance,
    trips, qualification) and without the ones that do not. Section 1 of the
    detail page refuses that driver a score and section 4 refuses the
    composition; a ranking row that printed either would be the back door around
    both, and a clean 8 km driver would sort above the fleet on `Wynik Eco`.
    """

    ranked = entry_row(assigned_id="RANKED")
    low = entry_row(
        assigned_id="LOWKM",
        ranking_group=None,
        ranking_included=True,
        ranking_position=None,
        ranking_total_participants=None,
        qualification_status="LOW_DISTANCE",
        total_distance_meters=8_000,
        total_kilometers=Decimal("8.000"),
        trips_count=2,
        eco_driving_score_total=Decimal("100.00"),
        ecodriving_rating_type="bezpieczny",
    )
    controller, backend = make(data=responses(
        entries_list=[ranked, low], entries_count=[{"total_count": 2}],
    ))
    body = ranking(controller).body_html

    # The SQL asked for the tab's population, not merely the group.
    listing = backend.reader.last_sql("entries_list")
    assert "s.ranking_group = %(ranking_group)s" in listing
    assert "(s.ranking_group IS NULL AND s.ranking_included IS TRUE)" in listing

    # The row is there, navigable, and honest about why it has no position.
    assert "assigned_id=LOWKM" in body
    assert "8,00 km" in body
    assert t("eco.qualification.not_met") in body
    assert t("eco.group.included_unranked", count=12) in body
    positions = _position_cells(body)
    assert len(positions) == 2 and "—" in positions[1], positions

    # And the withheld half stays withheld: no score, no rating band, and every
    # metric cell of that row is the placeholder rather than a coefficient
    # extrapolated from 8 km.
    row_html = body.split("assigned_id=RANKED")[0].split("<tr>")[-1]
    low_html = body.split("<tr>")[-1]
    assert "eco-score-number" in row_html          # the qualified row still scores
    assert "eco-score-number" not in low_html
    assert low["ecodriving_rating_type"] not in low_html
    assert "data-severity=" not in low_html
    ok("a permitted below-threshold driver is listed on the tab, without a score")


def test_driver_search_is_bounded_escaped_and_url_borne():
    """Free-text search over the driver identity the ranking actually shows.

    The term never reaches SQL as text: it is bound, and its LIKE
    metacharacters are neutralised, so ``100%`` searches for the literal string
    rather than widening to everything.
    """

    controller, backend = make()
    body = ranking(controller, search="Kowal%ski").body_html

    params = backend.reader.last_params("entries_list")
    assert params["search"] == "%Kowal\\%ski%"
    assert q.ENTRY_SEARCH_PREDICATE in backend.reader.last_sql("entries_list")
    # The count must see the same join, or the total would disagree with the rows.
    assert "eco_drivers_id_chart" in backend.reader.last_sql("entries_count")

    # The active search is a removable chip and lives in the URL.
    assert t("eco.search.chip", term="Kowal%ski") in body
    assert t("eco.search.clear") in body
    assert 'name="search"' in body and 'method="get"' in body

    # Bounded length, and a blank term is simply no search.
    assert ranking(controller, search="x" * (q.MAX_SEARCH_LENGTH + 1)).status_code == 422
    controller, backend = make()
    ranking(controller, search="   ")
    assert backend.reader.last_params("entries_list").get("search") is None

    # The audit records that a search happened, never what was searched for.
    controller, backend = make()
    ranking(controller, search="Kowalski")
    meta = [e["metadata"] for e in backend.audit_events
            if e["event_type"] == "eco_driving_ranking_viewed"][0]
    assert meta["search_applied"] is True
    assert "Kowalski" not in str(meta)
    ok("driver search is bounded, wildcard-escaped, URL-borne and audited without the term")


def test_excluded_driver_keeps_classification_and_detail_access():
    row = scored_row({"harsh_braking_events": 1}, ranking_group="EXCLUDED",
                     ranking_included=False, ranking_position=3)
    controller, _ = make(data=responses(entries_list=[row], single_entry=[row],
                                        entries_count=[{"total_count": 1}]))
    body = ranking(controller, ranking_group="EXCLUDED").body_html
    assert "assigned_id=12345" in body           # navigable to the driver page
    assert row["ecodriving_rating_type"] in body  # keeps its own rating band
    assert ">EXCLUDED<" not in body               # group never replaces the rating

    page = detail(controller, ranking_group="EXCLUDED")
    assert page.status_code == 200
    assert t("eco.group.excluded") in page.body_html
    assert row["ecodriving_rating_type"] in page.body_html
    ok("an EXCLUDED driver keeps its rating band and its own detail page")


def test_production_three_band_vocabulary_is_preserved():
    labels = [label for _minimum, label in RATING_THRESHOLDS]
    assert labels == ["bezpieczny", "akceptowalny"]
    assert set(V.RATING_CLASS) == {"bezpieczny", "akceptowalny", "niebezpieczny"}
    # The four-band vocabulary from the visual design is deliberately absent.
    for banned in ("bardzo dobra", "przeciętna", "wymaga uwagi"):
        assert banned not in V.RATING_CLASS
    for band in V.RATING_CLASS:
        assert band in V.rating_badge(band)
    controller, _ = make()
    body = ranking(controller).body_html
    for banned in ("bardzo dobra", "przeciętna", "wymaga uwagi"):
        assert banned not in body, banned
    ok("the production three-band rating vocabulary and thresholds are preserved")


# --- 4. score composition ----------------------------------------------------


def test_composition_uses_persisted_losses_share_and_descending_order():
    rates = {
        "harsh_braking_events": 10,       # deep loss
        "speeding_140_160_count": 4,      # medium loss
        "harsh_turning_events": 6,        # small loss
    }
    row = scored_row(rates)
    controller, _ = make(data=responses(single_entry=[row]))
    body = detail(controller).body_html

    losses = {m: Decimal(row[q.SUBTRACT_COLUMNS[m]]) for m in REQUIRED_METRICS}
    total = sum(abs(value) for value in losses.values())
    assert total > 0

    # Rows are ordered by lost-point impact, descending.
    order = _composition_order(body)
    expected = sorted(REQUIRED_METRICS, key=lambda m: SP.composition_sort_key(m, losses[m]))
    assert order == [V.metric_label(m) for m in expected], order

    # Every persisted loss is printed with its own sign, and each share matches.
    for metric, loss in losses.items():
        assert V.fmt_decimal(loss, 0) in body, metric
        share = SP.loss_share_percent(loss, total)
        if share is not None:
            assert f"{V.fmt_decimal(share, 0)} %" in body, (metric, share)
    assert t("eco.detail.composition_arithmetic", base=100, lost=V.fmt_decimal(total, 0)) in body

    # Maxima come from the repository, per metric.
    for metric in REQUIRED_METRICS:
        assert str(METRIC_MAX_POINTS[metric]) in body
    ok("composition uses persisted points/maxima/losses, correct shares and descending order")


def test_composition_tie_break_is_deterministic():
    """Two metrics losing exactly the same amount keep the canonical order."""

    keys = [SP.composition_sort_key(m, Decimal(-5)) for m in REQUIRED_METRICS]
    assert keys == sorted(keys)
    assert [m for m in REQUIRED_METRICS] == sorted(
        REQUIRED_METRICS, key=lambda m: SP.composition_sort_key(m, Decimal(-5))
    )
    # A metric with no loss value at all sorts last, not first.
    assert SP.composition_sort_key("idle_events", None) > SP.composition_sort_key("idle_events", Decimal(0))
    ok("equal losses fall back to the canonical metric order; missing losses sort last")


def test_zero_total_loss_is_reported_honestly():
    row = scored_row({})  # a perfect period: nothing lost anywhere
    assert all(Decimal(row[q.SUBTRACT_COLUMNS[m]]) == 0 for m in REQUIRED_METRICS)
    controller, _ = make(data=responses(single_entry=[row]))
    body = detail(controller).body_html
    assert t("eco.comp.no_loss") in body
    assert "% </span>" not in body
    # No share is manufactured from a zero denominator.
    assert SP.loss_share_percent(Decimal(0), Decimal(0)) is None
    assert SP.loss_share_percent(Decimal(-3), Decimal(0)) is None
    ok("a zero total loss yields no share at all, never a fabricated 0 %")


def test_share_denominator_is_the_driver_own_total_loss():
    losses = {m: Decimal(0) for m in REQUIRED_METRICS}
    losses["harsh_braking_events"] = Decimal(-6)
    losses["idle_events"] = Decimal(-2)
    total = SP.total_loss_magnitude(losses)
    assert total == Decimal(8)
    assert SP.loss_share_percent(losses["harsh_braking_events"], total) == Decimal("75.0")
    assert SP.loss_share_percent(losses["idle_events"], total) == Decimal("25.0")
    ok("share of loss divides by the driver's own total lost-point magnitude")


def test_prog_column_states_the_ladder_not_a_single_threshold():
    """`Próg` must not present one bucket as though it were the whole rule."""

    for metric, rules in SCORING_RULES.items():
        steps = SP.ladder_steps(metric)
        assert len(steps) == len(rules.buckets) + 1
        assert steps[-1].is_final and steps[-1].points == rules.final_points
        # Selection reproduces `score_metric` exactly.
        for bucket in rules.buckets:
            step = SP.applicable_ladder_step(metric, bucket.upper_bound)
            assert step is not None and step.points == bucket.points, metric

    row = scored_row({"harsh_braking_events": 5})
    controller, _ = make(data=responses(single_entry=[row]))
    body = detail(controller).body_html
    # The cell states which rung applies *and* how many rungs there are.
    braking_steps = SP.ladder_steps("harsh_braking_events")
    applied = SP.applicable_ladder_step("harsh_braking_events", Decimal(5))
    assert t("eco.comp.threshold_step", step=applied.index, total=applied.total_steps) in body
    assert t("eco.comp.threshold_note") in body
    assert len(braking_steps) > 2  # it really is a ladder, not one threshold
    ok("Próg presents the applicable rung plus its position in the repository ladder")


def _composition_order(body: str) -> list[str]:
    order = []
    cursor = body.find('data-eco-section="composition"')
    end = body.find('data-eco-section="progression"')
    section = body[cursor:end if end != -1 else len(body)]
    marker = '<th scope="row">'
    pos = 0
    while True:
        found = section.find(marker, pos)
        if found == -1:
            return order
        start = found + len(marker)
        stop = section.index("</th>", start)
        order.append(section[start:stop])
        pos = stop


# --- 5. fleet histogram ------------------------------------------------------


def test_histogram_bins_cover_the_repository_score_domain():
    assert q.SCORE_BIN_MIN == -100 and q.SCORE_BIN_MAX == 100
    assert q.SCORE_BIN_COUNT * q.SCORE_BIN_WIDTH == q.SCORE_BIN_MAX - q.SCORE_BIN_MIN
    # Boundaries: lower bound inclusive, top bin closed so a perfect 100 lands.
    assert q.score_bin_index(-100) == 0
    assert q.score_bin_index(-91) == 0
    assert q.score_bin_index(-90) == 1
    assert q.score_bin_index(0) == 10
    assert q.score_bin_index(99) == q.SCORE_BIN_COUNT - 1
    assert q.score_bin_index(100) == q.SCORE_BIN_COUNT - 1
    assert q.score_bin_index(-500) == 0
    assert q.score_bin_index(500) == q.SCORE_BIN_COUNT - 1
    assert q.score_bin_index(None) is None
    ok("histogram bins span the repository score domain with a closed top bin")


def test_histogram_is_scoped_to_one_client_period_and_group():
    controller, backend = make(data=responses(score_distribution=[
        {"bin_index": 19, "bin_count": 4, "total_count": 5,
         "mean_score": Decimal("88"), "median_score": Decimal("90")},
        {"bin_index": 12, "bin_count": 1, "total_count": 5,
         "mean_score": Decimal("88"), "median_score": Decimal("90")},
    ]))
    body = ranking(controller, ranking_group="EXCLUDED").body_html

    sql = backend.reader.last_sql("score_distribution")
    params = backend.reader.last_params("score_distribution")
    assert "client_id = %(client_id)s::uuid" in sql
    assert "ranking_group = %(ranking_group)s" in sql
    assert "month_start_date = %(month_start_date)s" in sql
    assert "period_start_date = %(period_start_date)s" in sql
    assert "period_end_date = %(period_end_date)s" in sql
    assert params["client_id"] == api_t.TRUSTED_CLIENT_ID
    assert params["ranking_group"] == "EXCLUDED"
    assert params["month_start_date"] == KEY.month_start_date
    assert params["period_end_date"] == KEY.period_end_date
    # No client code, no database name and no second client can reach the query.
    assert "client_code" not in params
    assert api_t.DB_NAME not in sql and api_t.DB_NAME not in body

    # Real persisted values are rendered; no design sample survives.
    assert f'{t("eco.distribution.median")} 90' in body
    assert f'{t("eco.distribution.mean")} 88' in body
    assert "eco-histogram-bar" in body
    for sample in ("1 512", "498", "71", "68"):
        assert sample not in body, sample
    ok("the histogram is scoped to one authorized client, period and group, on real scores")


def test_histogram_without_a_group_still_excludes_non_ranked_rows():
    sql = q.score_distribution_sql(weekly=True, with_group=False)
    assert "ranking_group IS NOT NULL" in sql
    assert "eco_driving_score_total IS NOT NULL" in sql
    ok("an unfiltered histogram still draws only from the ranking population")


def test_histogram_empty_state_is_explicit():
    controller, _ = make(data=responses(score_distribution=[]))
    body = ranking(controller).body_html
    assert t("eco.distribution.empty") in body
    ok("an empty distribution states so instead of drawing an empty plot")


# --- 6. trend ----------------------------------------------------------------


def trend_row(label: str, end: date, score, *, status="QUALIFIED", seq=1) -> dict:
    return {
        "month_start_date": date(end.year, end.month, 1),
        "period_start_date": date(end.year, end.month, 1),
        "period_end_date": end,
        "period_sequence_in_month": seq,
        "period_label": label,
        "is_partial_period": False,
        "eco_driving_score_total": None if score is None else Decimal(score),
        "ranking_position": 5,
        "ranking_total_participants": 40,
        "qualification_status": status,
        "total_kilometers": Decimal("1200"),
    }


def test_trend_is_chronological_scoped_and_never_zero_filled():
    # The view is read newest-first; the page must present it oldest-first.
    rows = [
        trend_row("2026-07-W4", date(2026, 7, 27), 78, seq=4),
        trend_row("2026-07-W3", date(2026, 7, 20), 74, seq=3),
        trend_row("2026-06-W2", date(2026, 6, 15), 62, seq=2),
    ]
    controller, backend = make(data=responses(driver_trend=rows))
    body = detail(controller).body_html

    positions = [body.index(row["period_label"]) for row in reversed(rows)]
    assert positions == sorted(positions), positions

    sql = backend.reader.last_sql("driver_trend")
    params = backend.reader.last_params("driver_trend")
    assert "_trends_view" in sql                       # the repository's own view
    assert "assigned_id = %(assigned_id)s" in sql      # this driver only
    assert "client_id = %(client_id)s::uuid" in sql
    assert params["assigned_id"] == "12345"
    assert params["client_id"] == api_t.TRUSTED_CLIENT_ID
    assert params["limit"] == q.TREND_PERIOD_LIMIT == 8

    # Only three periods exist, so only three columns are drawn — no filler.
    assert body.count('class="eco-trend-col') == 3
    assert t("eco.detail.trend_missing") in body
    ok("trend is chronological, driver-scoped, view-backed and never zero-filled")


def test_trend_hides_the_score_of_an_unqualified_period():
    rows = [
        trend_row("2026-07-W4", date(2026, 7, 27), 78, seq=4),
        trend_row("2026-06-W1", date(2026, 6, 8), 91, status="LOW_DISTANCE", seq=1),
    ]
    controller, _ = make(data=responses(driver_trend=rows))
    body = detail(controller).body_html
    assert "2026-06-W1" in body     # the period is still shown
    assert ">91<" not in body       # but its unqualified score is not
    ok("an unqualified period contributes a column but never a score to the trend")


def test_trend_empty_state():
    controller, _ = make(data=responses(driver_trend=[]))
    body = detail(controller).body_html
    assert t("eco.detail.trend_empty") in body
    ok("a driver with no comparable periods gets an explicit empty trend")


# --- 7. cumulative snapshots are never summed --------------------------------


def test_period_progression_is_cumulative_and_never_summed():
    def progression_row(label, end, score, km, seq):
        row = {
            "period_label": label,
            "period_start_date": date(2026, 7, 1),
            "period_end_date": end,
            "period_sequence_in_month": seq,
            "is_partial_period": False,
            "eco_driving_score_total": Decimal(score),
            "qualification_status": "QUALIFIED",
            "total_distance_meters": km * 1000,
            "total_kilometers": Decimal(km),
            "trips_count": seq * 10,
        }
        for metric in REQUIRED_METRICS:
            row[metric] = 0
            row[q.RATE_COLUMNS[metric]] = Decimal(0)
        return row

    rows = [
        progression_row("2026-07-W3", date(2026, 7, 20), 74, 1800, 3),
        progression_row("2026-07-W4", KEY.period_end_date, 78, 2200, 4),
    ]
    controller, backend = make(data=responses(period_progression=rows))
    body = detail(controller).body_html

    assert t("eco.detail.progression_note") in body
    assert t("eco.detail.progression_caption") in body
    # The delta between consecutive cumulative snapshots is shown; the sum is not.
    assert "+4" in body
    assert ">152<" not in body   # 74 + 78 must never appear
    assert ">4000<" not in body  # 1800 + 2200 must never appear

    sql = backend.reader.last_sql("period_progression")
    assert "SUM(" not in sql.upper()
    assert "month_start_date = %(month_start_date)s" in sql
    ok("in-month snapshots render as a cumulative progression with deltas, never a sum")


def test_monthly_entry_has_no_in_month_progression():
    from api.eco_driving_explorer.registry import get_provider
    from api.eco_driving_explorer.models import PeriodType, RankingPeriodKey

    provider = get_provider(CC, FAMILY, client_id=prov.CLIENT_ID)
    monthly = RankingPeriodKey(
        period_type=PeriodType.MONTHLY,
        month_start_date=date(2026, 7, 1),
        period_start_date=date(2026, 7, 1),
        period_end_date=date(2026, 8, 1),
    )
    reader = prov.FakeRowReader({})
    assert provider.get_period_progression(reader, monthly, "12345") == ()
    ok("a monthly entry reports no in-month snapshot progression rather than inventing one")


# --- 8. deliberate removals --------------------------------------------------


def test_ranking_has_no_per_row_lineage_and_states_it_once_in_context():
    controller, _ = make()
    result = ranking(controller)
    body = result.body_html
    assert H.LINEAGE_LABEL not in body
    assert H.LINEAGE_LABEL in result.context_meta_html
    assert result.context_meta_html.count(H.LINEAGE_LABEL) == 1
    assert t("eco.ranking.family_badge") in result.context_meta_html
    assert result.context_client_code == CC
    ok("the lineage qualifier appears once in the context bar and never per ranking row")


def test_removed_panels_are_absent_from_every_eco_page():
    banned = (
        "Reconciliation", "Rekoncyliacja", "reconciliation_status",
        "How the score was calculated", "Jak policzono wynik",
        "Full scoring definition and rating thresholds",
        "Minimum qualifying distance", "Threshold rules",
        "Persisted versus current reconstructed aggregates",
    )
    controller, _ = make(trip=True, routes=True)
    bodies = [
        controller.landing(user=USER).body_html,
        ranking(controller).body_html,
        detail(controller).body_html,
        controller.ranking_entry_trips(
            user=USER, client_code=CC, ranking_family=FAMILY,
            period_key=TOKEN, assigned_id="12345",
        ).body_html,
    ]
    for body in bodies:
        for marker in banned:
            assert marker not in body, marker
        assert 'class="eco-tabs"' not in body
    ok("reconciliation, score-definition and tab surfaces are absent from every Eco page")


def test_no_raw_row_link_and_no_per_trip_contribution():
    controller, _ = make(trip=True, data=responses(
        trips_list=[prov.make_trip_row()], trips_count=[{"total_count": 1}]
    ))
    for body in (detail(controller).body_html,
                 controller.ranking_entry_trips(
                     user=USER, client_code=CC, ranking_family=FAMILY,
                     period_key=TOKEN, assigned_id="12345").body_html):
        assert "record_id" not in body
        assert "/user/database" not in body
        assert "Wkład w wynik" not in body
    ok("no raw-row link, no record_id and no invented per-trip contribution")


# --- 9. shared shell / accessibility ----------------------------------------


def test_module_uses_the_shared_asset_and_token_layer():
    from api.eco_driving_explorer.pages import PageResult

    assert H.ECO_DRIVING_PAGE_ASSETS == ("css/eco-driving.css",)
    assert "css/eco-driving.css" in portal_assets.PAGE_ASSETS
    tags = portal_assets.page_asset_tags(*H.ECO_DRIVING_PAGE_ASSETS)
    assert 'rel="stylesheet"' in tags and "css/eco-driving.css" in tags

    controller, _ = make()
    result = ranking(controller)
    # The ranking table became a selection surface in `UI-20260827-02`, so a
    # populated ranking declares the selectable set. It is still the shared Eco
    # stylesheet plus the shared selection layer — no module-private asset.
    assert result.page_assets == H.ECO_DRIVING_SELECTABLE_PAGE_ASSETS
    assert result.page_assets[0] == "css/eco-driving.css"
    assert isinstance(result, PageResult)
    assert "<style" not in result.body_html  # no second theme layer

    css = (ROOT / "api" / "static" / "css" / "eco-driving.css").read_text(encoding="utf-8")
    for hard_coded in ("#fff", "#000", "rgb(", "rgba("):
        assert hard_coded not in css.lower(), hard_coded
    assert "var(--lp-" in css
    ok("Eco Driving renders on the shared asset/token layer with no module palette")


def test_every_semantic_state_carries_a_non_colour_signal():
    row = scored_row({"harsh_braking_events": 10, "idle_events": 3})
    controller, _ = make(data=responses(entries_list=[row], entries_count=[{"total_count": 1}],
                                        single_entry=[row]))
    body = ranking(controller).body_html
    rendered = cells(body)
    assert rendered, "expected metric cells"
    for severity, _text in rendered:
        assert severity in SP.SEVERITY_ORDER
    # Every severity that is rendered also states its word, so the state is
    # readable with colour removed entirely.
    for severity, _text in rendered:
        assert t(V.SEVERITY_WORD_KEYS[severity]) in body, severity
    assert t("eco.severity.bad") in body
    assert "lp-visually-hidden" in body
    # The rating band is a word, so it reads without colour at all.
    assert "bezpieczny" in body
    ok("metric severity and rating band both carry a non-colour textual signal")


def test_translation_keys_resolve():
    missing = [key for key in _used_keys() if t(key) == key]
    assert not missing, missing
    ok("every Eco translation key used by the module resolves to Polish copy")


def _used_keys() -> list[str]:
    import re

    keys: set[str] = set()
    for name in ("eco_view.py", "pages.py", "detail_view_models.py", "trip_view_models.py"):
        text = (ROOT / "api" / "eco_driving_explorer" / name).read_text(encoding="utf-8")
        keys.update(re.findall(r't\(\s*"(eco\.[a-z0-9_.]+)"', text))
    return sorted(keys)


# --- 10. context switching (`D-009`) ----------------------------------------


def test_context_switch_to_a_period_without_the_driver_lands_on_the_ranking():
    absent = responses(single_entry=[])
    controller, _ = make(data=absent)
    result = detail(controller, ranking_group="EXCLUDED", page="2", limit="100")
    assert result.status_code == 404
    body = result.body_html
    # The ranking for the *new* context is rendered, and the message names the
    # driver and the reason.
    assert "12345" in body
    # UI-20260820-07: the period renders through the shared formatter now. The
    # expectation is built from that formatter rather than from a literal, so a
    # future format change moves the code and the test together instead of
    # leaving this asserting a rendering the page no longer produces.
    from api.portal_ui.formats import format_date as _fmt_date
    assert t("eco.state.driver_absent",
             driver="12345",
             period=(f"{_fmt_date(KEY.period_start_date)} → "
                     f"{_fmt_date(KEY.period_end_date)}")) in body
    assert t("eco.col.position") in body
    ok("D-009: a driver absent from the new context lands on that context's ranking, explained")


def test_back_navigation_restores_the_exact_ranking_context():
    controller, _ = make()
    body = detail(controller, ranking_group="EXCLUDED", page="2", limit="100",
                  sort="assigned_id", direction="asc", unit="sum").body_html
    for fragment in ("ranking_group=EXCLUDED", "page=2", "limit=100",
                     "sort=assigned_id", "direction=asc", "unit=sum"):
        assert fragment in body, fragment
    ok("back navigation from the driver page restores group, page, size, sort and unit")


# --- 11. ranking truth -------------------------------------------------------


def test_ranking_position_comes_from_the_persisted_row():
    rows = [
        entry_row(assigned_id="A", ranking_position=7, eco_driving_score_total=Decimal("50")),
        entry_row(assigned_id="B", ranking_position=2, eco_driving_score_total=Decimal("90")),
    ]
    controller, _ = make(data=responses(entries_list=rows, entries_count=[{"total_count": 2}]))
    body = ranking(controller).body_html
    # Row order is the query's; the printed positions are the persisted numbers,
    # not 1..n renumbered from whatever happens to be on this page.
    positions = _position_cells(body)
    assert positions == ["7", "2"], positions
    ok("ranking position is the persisted value and is never recomputed from the page")


def _position_cells(body: str) -> list[str]:
    # Attribute-tolerant: the cell also carries selection marking since
    # `UI-20260827-02`, and this helper is about the printed position.
    import re

    return re.findall(r'<td class="eco-cell-position"[^>]*>(.*?)</td>', body, re.S)


def test_position_and_score_share_one_visual_weight():
    css = (ROOT / "api" / "static" / "css" / "eco-driving.css").read_text(encoding="utf-8")
    assert ".eco-cell-position" in css and ".eco-score-number" in css
    # Both are mono, semibold and of the same order of size: neither is a
    # caption of the other (`EC-8`).
    position_block = css.split(".eco-cell-position")[1].split("}")[0]
    score_block = css.split(".eco-score-number")[1].split("}")[0]
    for block in (position_block, score_block):
        assert "--lp-font-mono" in block
        assert "--lp-weight-semibold" in block
    ok("ranking position and score are presented at equal visual weight")


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"OK - Eco Driving S12 presentation checks passed ({len(tests)} cases)")


if __name__ == "__main__":
    main()
