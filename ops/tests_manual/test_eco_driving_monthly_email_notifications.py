#!/usr/bin/env python3
"""Manual checks for Eco Driving monthly email notification helpers."""

from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving import email_visuals as visuals  # noqa: E402
from jobs.ecodriving import job_eco_driving_monthly_email_notifications as job  # noqa: E402
from jobs.ecodriving.eco_scoring import score_metric  # noqa: E402


CLIENT_ID = "00000000-0000-0000-0000-000000000001"
UNSUPPORTED_EMAIL_CSS = (
    "position:absolute",
    "transform:",
    "linear-gradient",
    "display:flex",
    "display:grid",
    "calc(",
    "<script",
    "<svg",
    "javascript:",
)


def sample_row(**overrides) -> dict:
    row = {
        "client_id": CLIENT_ID,
        "client_code": "TEST",
        "assigned_id": "DRIVER-1",
        "recipient_email": "driver@example.test",
        "original_recipient_email": "driver@example.test",
        "ranking_type": "INCLUDED",
        "month_start_date": date(2026, 4, 1),
        "month_end_date": date(2026, 5, 1),
        "week_start_date": date(2026, 4, 1),
        "week_end_date": date(2026, 5, 1),
        "period_start_date": date(2026, 4, 1),
        "period_end_date": date(2026, 5, 1),
        "eco_driving_score_total": Decimal("78.00"),
        "ranking_position": 4,
        "ranking_total_participants": 20,
        "ecodriving_rating_type_share_percent": Decimal("41.25"),
        "qualification_status": "QUALIFIED",
        "overrev_events_per_100km": Decimal("2.00"),
        "harsh_braking_events_per_100km": Decimal("0.00"),
        "harsh_acceleration_events_per_100km": Decimal("1"),
        "harsh_turning_events_per_100km": Decimal("4.00"),
        "idle_events_per_100km": Decimal("2.00"),
        "speeding_140_160_events_per_100km": Decimal("0.00"),
        "speeding_160_170_events_per_100km": Decimal("5"),
        "speeding_170_plus_events_per_100km": Decimal("0.00"),
        "ranking_included": True,
        "overrev_maxpoints_subtract": Decimal("-4.00"),
        "harsh_braking_maxpoints_subtract": Decimal("0.00"),
        "harsh_acceleration_maxpoints_subtract": Decimal("-5.00"),
        "harsh_turning_maxpoints_subtract": Decimal("0.00"),
        "idle_maxpoints_subtract": Decimal("-3.00"),
        "speeding_140_160_maxpoints_subtract": Decimal("0.00"),
        "speeding_160_170_maxpoints_subtract": Decimal("-10.00"),
        "speeding_170_plus_maxpoints_subtract": Decimal("0.00"),
        "top_1_validation": "Prędkość 160-170 km/h",
        "top_2_validation": "Gwałtowne przyspieszenia",
        "ecodriving_rating_type": "akceptowalny",
        "driver_name": "Test Driver",
    }
    row.update(overrides)
    return row


class FakeCursor:
    def __init__(self, fetchone_result=None):
        self.fetchone_result = fetchone_result
        self.executed: list[tuple[str, tuple | list | None]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        if self.fetchone_result is None:
            return []
        return self.fetchone_result if isinstance(self.fetchone_result, list) else [self.fetchone_result]


class EnvPatch:
    def __init__(self, values: dict[str, str | None]):
        self.values = values
        self.old: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, exc_type, exc, tb):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def assert_outlook_safe_fragment(fragment: str) -> None:
    lowered = fragment.lower()
    for marker in UNSUPPORTED_EMAIL_CSS:
        if marker == "transform:":
            assert re.search(r"(?<!text-)transform\s*:", lowered) is None, marker
        else:
            assert marker not in lowered, marker


def test_monthly_template_inventory_and_routing() -> None:
    job.validate_template_inventory(job.DEFAULT_TEMPLATE_DIR)
    expected = {
        "Miesięczne - Bezpieczni.html",
        "Miesięczne - Bezpieczni - norank.html",
        "Miesięczne - Akceptowalni.html",
        "Miesięczne - Akceptowalni - norank.html",
        "Miesięczne - Niebezpieczni.html",
        "Miesięczne - Niebezpieczni - norank.html",
        "Miesięczne - Niezakwalifikowani.html",
    }
    assert set(job.REQUIRED_TEMPLATE_FILENAMES) == expected

    assert job._select_template(sample_row(qualification_status="LOW_DISTANCE")) == (
        "low_distance",
        "Miesięczne - Niezakwalifikowani.html",
        "low_distance",
    )
    assert job._select_template(sample_row(qualification_status="NO_DISTANCE")) == (
        "low_distance",
        "Miesięczne - Niezakwalifikowani.html",
        "low_distance",
    )
    assert job._select_template(sample_row(ecodriving_rating_type="bezpieczny")) == (
        "bezpieczny",
        "Miesięczne - Bezpieczni.html",
        "ranked",
    )
    assert job._select_template(sample_row(ecodriving_rating_type="akceptowalny")) == (
        "akceptowalny",
        "Miesięczne - Akceptowalni.html",
        "ranked",
    )
    assert job._select_template(sample_row(ecodriving_rating_type="niebezpieczny")) == (
        "niebezpieczny",
        "Miesięczne - Niebezpieczni.html",
        "ranked",
    )
    assert job._select_template(
        sample_row(
            ecodriving_rating_type="akceptowalny",
            ranking_included=False,
            ranking_position=None,
            ranking_total_participants=None,
        )
    ) == ("akceptowalny", "Miesięczne - Akceptowalni - norank.html", "norank")
    print("PASS: monthly template inventory and routing are complete")


def test_monthly_period_aliases_latest_period_and_subject() -> None:
    expected = (date(2026, 4, 1), date(2026, 5, 1))
    assert job._resolve_explicit_period_params(
        {"period_start_date": "2026-04-01", "period_end_date": "2026-05-01"}
    ) == expected
    assert job._resolve_explicit_period_params(
        {"month_start_date": "2026-04-01", "month_end_date": "2026-05-01"}
    ) == expected
    assert job._resolve_explicit_period_params(
        {"week_start_date": "2026-04-01", "week_end_date": "2026-05-01"}
    ) == expected

    finalized = datetime(2026, 5, 1, 0, 1, tzinfo=ZoneInfo("Europe/Warsaw"))
    cur = FakeCursor({"period_start_date": expected[0], "period_end_date": expected[1], "period_label":"2026-04", "snapshot_min_updated_at":finalized, "snapshot_max_updated_at":finalized})
    rows = job.fetch_period_candidates(cur, stats_table="public.eco_driver_monthly_stats", client_id=CLIENT_ID, report_type="monthly")
    selected = job.select_period_for_send(rows, report_type="monthly", contract=job.resolve_execution_contract({}), explicit_period=expected, clock=finalized)
    assert (selected.selected.period_start_date, selected.selected.period_end_date) == expected
    sql, params = cur.executed[-1]
    assert "month_start_date AS period_start_date" in sql
    assert "min(updated_at) AS snapshot_min_updated_at" in sql
    assert "public.eco_driver_monthly_stats" in sql
    assert params == (CLIENT_ID,)

    subject = job.render_subject(
        "Program Ecodriving - Miesięczna aktualizacja wyniku ({month_start_date} - {month_end_date})",
        sample_row(),
    )
    assert subject == "Program Ecodriving - Miesięczna aktualizacja wyniku (01.04.2026 - 30.04.2026)"
    context = job.build_template_context(sample_row())
    assert context["month_start_date"] == "01.04.2026"
    assert context["month_end_date"] == "30.04.2026"
    assert context["period_end_date"] == "30.04.2026"
    source = Path(job.__file__).read_text(encoding="utf-8")
    for column in (
        "overrev_events_per_100km",
        "harsh_braking_events_per_100km",
        "harsh_acceleration_events_per_100km",
        "harsh_turning_events_per_100km",
        "idle_events_per_100km",
        "speeding_140_160_events_per_100km",
        "speeding_160_170_events_per_100km",
        "speeding_170_plus_events_per_100km",
        "overrev_maxpoints_subtract",
        "harsh_braking_maxpoints_subtract",
        "harsh_acceleration_maxpoints_subtract",
        "harsh_turning_maxpoints_subtract",
        "idle_maxpoints_subtract",
        "speeding_140_160_maxpoints_subtract",
        "speeding_160_170_maxpoints_subtract",
        "speeding_170_plus_maxpoints_subtract",
    ):
        assert f"s.{column}" in source
    print("PASS: monthly period aliases, latest period query, subject rendering, rate and subtract column selection work")


def _metric_card(fragment: str, label: str) -> str:
    start = fragment.index(f"<!-- Visual Score Bar: {label} -->")
    next_match = re.search(r"\n                    <!-- Visual Score Bar:", fragment[start + 1 :])
    if next_match is None:
        end = fragment.index("Kolory pokazują orientacyjny poziom wpływu na wynik", start)
    else:
        end = start + 1 + next_match.start()
    return fragment[start:end]


def _metric_black_marker_indexes(card: str) -> list[int]:
    marker_cells = re.findall(
        r'<td width="[^"]+" align="center" valign="top" style="padding:2px 0 0 0; font-size:15px; line-height:15px; color:(#[0-9A-F]{6}); font-weight:bold;">(?:▲|&nbsp;)</td>',
        card,
    )
    return [index for index, color in enumerate(marker_cells) if color == "#111111"]


def test_monthly_area_score_axes_are_dynamic_and_outlook_safe() -> None:
    row = sample_row()
    fragment = visuals.render_area_score_axes_html(row)
    assert "<!-- Visual Scoring Details -->" in fragment
    assert "Wizualizacja punktacji według obszarów" in fragment
    assert "Każdy pasek pokazuje progi punktowe dla danego obszaru." in fragment
    assert "Jak czytać pasek punktacji?" in fragment
    assert "Ilość wykroczeń:" in fragment
    assert "na 100 km" in fragment
    assert "▲" in fragment
    assert "▼" not in fragment
    assert "Nadmierne obroty" in fragment
    assert "Prędkość powyżej 170 km/h" in fragment
    assert visuals.AREA_SEGMENT_GREEN in fragment
    assert visuals.AREA_SEGMENT_YELLOW in fragment
    assert visuals.AREA_SEGMENT_RED in fragment
    assert "15 pkt" not in fragment
    assert "10 pkt" not in fragment
    assert "-4" in fragment
    assert "-8" in fragment
    assert 'style="background-color:#FFFFFF; border:1px solid #E3E8E5;"' in fragment
    assert 'style="margin-top:14px; background-color:#F8FAFC; border:1px solid #E3E8E5;"' in fragment
    assert 'style="background-color:#FFFFFF; border:1px solid #E3E8E5; margin-bottom:14px;"' in fragment
    assert 'style="margin-top:12px; border-collapse:collapse;"' in fragment
    assert 'class="area-marker-cell"' not in fragment
    assert "border:1px solid #D9DEE2" not in fragment
    assert_outlook_safe_fragment(fragment)
    lowered_fragment = fragment.lower()
    assert "http://" not in lowered_fragment
    assert "https://" not in lowered_fragment
    assert "canvas" not in lowered_fragment
    assert "transform:" not in lowered_fragment
    assert "display:flex" not in lowered_fragment
    assert "display:grid" not in lowered_fragment
    assert "{area_score_axes_html}" not in fragment

    overrev = visuals.AREA_SCORE_METRICS[0]
    segments = visuals.area_metric_segments(overrev)
    assert len(segments) == 7
    assert segments[0].threshold_label == "0"
    assert segments[1].threshold_label == "1–2"
    assert segments[-1].threshold_label == "&gt;20"
    assert [segment.lost_points_label for segment in segments] == ["0", "-4", "-8", "-11", "-15", "-22", "-30"]
    overrev_card = _metric_card(fragment, "Nadmierne obroty")
    assert overrev_card.count('width="14.285714%" align="center" valign="top"') == 7
    assert _metric_black_marker_indexes(overrev_card) == [1]
    assert "2 na 100 km" in overrev_card
    assert "2,0 na 100 km" not in overrev_card
    assert "2.0 na 100 km" not in overrev_card
    rounded_display_fragment = visuals.render_area_score_axes_html(sample_row(overrev_events_per_100km=Decimal("1.50")))
    assert "2 na 100 km" in _metric_card(rounded_display_fragment, "Nadmierne obroty")
    assert "1,5 na 100 km" not in rounded_display_fragment

    assert visuals.area_marker_segment_index(overrev, Decimal("0")) == 0
    assert visuals.area_marker_segment_index(overrev, Decimal("3")) == 2
    assert visuals.area_marker_segment_index(overrev, Decimal("2.49")) == 1
    assert visuals.area_marker_segment_index(overrev, Decimal("2.50")) == 2
    assert visuals.area_marker_segment_index(overrev, Decimal("25")) == 6
    assert visuals.area_marker_segment_index(overrev, None) is None
    assert visuals.area_marker_segment_index_from_subtract(overrev, Decimal("0.00")) == 0
    assert visuals.area_marker_segment_index_from_subtract(overrev, Decimal("-4.00")) == 1
    assert visuals.area_marker_segment_index_from_subtract(overrev, Decimal("-8.00")) == 2
    assert visuals.area_marker_segment_index_from_subtract(overrev, Decimal("-30.00")) == 6
    assert visuals.area_marker_segment_index_from_subtract(overrev, None) is None

    low_fragment = visuals.render_area_score_axes_html(sample_row(overrev_events_per_100km=Decimal("0"), overrev_maxpoints_subtract=Decimal("0.00")))
    mid_fragment = visuals.render_area_score_axes_html(sample_row(overrev_events_per_100km=Decimal("3"), overrev_maxpoints_subtract=Decimal("-8.00")))
    high_fragment = visuals.render_area_score_axes_html(sample_row(overrev_events_per_100km=Decimal("25"), overrev_maxpoints_subtract=Decimal("-30.00")))
    assert _metric_black_marker_indexes(_metric_card(low_fragment, "Nadmierne obroty")) == [0]
    assert _metric_black_marker_indexes(_metric_card(mid_fragment, "Nadmierne obroty")) == [2]
    assert _metric_black_marker_indexes(_metric_card(high_fragment, "Nadmierne obroty")) == [6]

    missing_rate_fragment = visuals.render_area_score_axes_html(sample_row(overrev_events_per_100km=None, overrev_maxpoints_subtract=Decimal("-4.00")))
    assert "brak danych na 100 km" in missing_rate_fragment
    missing_rate_overrev_card = _metric_card(missing_rate_fragment, "Nadmierne obroty")
    assert _metric_black_marker_indexes(missing_rate_overrev_card) == [1]

    missing_subtract_fragment = visuals.render_area_score_axes_html(sample_row(overrev_maxpoints_subtract=None))
    missing_subtract_overrev_card = _metric_card(missing_subtract_fragment, "Nadmierne obroty")
    assert _metric_black_marker_indexes(missing_subtract_overrev_card) == []
    assert "▲" not in missing_subtract_overrev_card
    print("PASS: monthly area score axes follow exact reference structure, dynamic marker buckets, and Outlook safety")


def _marker_bucket_subtract(row: dict, label: str) -> int | None:
    card = _metric_card(visuals.render_area_score_axes_html(row), label)
    indexes = _metric_black_marker_indexes(card)
    if not indexes:
        return None
    metric = next(metric for metric in visuals.AREA_SCORE_METRICS if metric.label_html == label)
    return int(visuals.area_metric_segments(metric)[indexes[0]].lost_points_label)


def test_monthly_marker_uses_persisted_rounded_rate_scoring() -> None:
    overrev = visuals.AREA_SCORE_METRICS[0]
    harsh_braking = visuals.AREA_SCORE_METRICS[1]

    # Raw 2.49 rounds to 2 and scores in overrev's 1-2 bucket (-4).
    assert score_metric(overrev.metric_name, Decimal("2.49")) == 11
    assert visuals.area_marker_segment_index(overrev, Decimal("2")) == 1
    assert visuals.area_marker_segment_index_from_subtract(overrev, Decimal("-4")) == 1
    rounded_down_row = sample_row(
        overrev_events_per_100km=Decimal("2"),
        overrev_maxpoints_subtract=Decimal("-4.00"),
    )
    rounded_down_card = _metric_card(visuals.render_area_score_axes_html(rounded_down_row), "Nadmierne obroty")
    assert "2 na 100 km" in rounded_down_card
    assert _metric_black_marker_indexes(rounded_down_card) == [1]

    # Raw 2.50 rounds half-up to 3 and scores in overrev's 3-5 bucket (-8).
    assert score_metric(overrev.metric_name, Decimal("2.50")) == 7
    rounded_half_up_row = sample_row(
        overrev_events_per_100km=Decimal("3"),
        overrev_maxpoints_subtract=Decimal("-8.00"),
    )
    assert _metric_black_marker_indexes(_metric_card(visuals.render_area_score_axes_html(rounded_half_up_row), "Nadmierne obroty")) == [2]

    # Raw 0.49 harsh braking rounds to 0 and does not lose points.
    assert score_metric(harsh_braking.metric_name, Decimal("0.49")) == 10
    assert visuals.area_marker_segment_index(harsh_braking, Decimal("0")) == 0
    assert visuals.area_marker_segment_index_from_subtract(harsh_braking, Decimal("0")) == 0
    rounded_to_zero_row = sample_row(
        harsh_braking_events_per_100km=Decimal("0"),
        harsh_braking_maxpoints_subtract=Decimal("0.00"),
    )
    assert _marker_bucket_subtract(rounded_to_zero_row, "Gwałtowne hamowania") == 0

    # Raw 0.50 harsh braking rounds half-up to 1 and scores in the 1 bucket (-2).
    assert score_metric(harsh_braking.metric_name, Decimal("0.50")) == 8
    assert visuals.area_marker_segment_index(harsh_braking, Decimal("1")) == 1
    assert visuals.area_marker_segment_index_from_subtract(harsh_braking, Decimal("-2")) == 1
    rounded_half_up_braking = sample_row(
        harsh_braking_events_per_100km=Decimal("1"),
        harsh_braking_maxpoints_subtract=Decimal("-2.00"),
    )
    assert _marker_bucket_subtract(rounded_half_up_braking, "Gwałtowne hamowania") == -2

    # Raw 6.49 harsh braking rounds to 6 and stays in the 5-6 bucket (-8).
    assert score_metric(harsh_braking.metric_name, Decimal("6.49")) == 2
    assert visuals.area_marker_segment_index(harsh_braking, Decimal("6")) == 3
    assert visuals.area_marker_segment_index_from_subtract(harsh_braking, Decimal("-8")) == 3
    six_display_row = sample_row(
        harsh_braking_events_per_100km=Decimal("6"),
        harsh_braking_maxpoints_subtract=Decimal("-8.00"),
    )
    assert _marker_bucket_subtract(six_display_row, "Gwałtowne hamowania") == -8
    print("PASS: monthly visual markers follow persisted rounded-rate scoring buckets")


def test_monthly_visual_diagnostics_and_score_arithmetic() -> None:
    row = sample_row()
    score_diag = visuals.monthly_score_arithmetic_diagnostic(row)
    assert score_diag["assigned_id"] == "DRIVER-1"
    assert score_diag["period_start_date"] == date(2026, 4, 1)
    assert score_diag["period_end_date"] == date(2026, 5, 1)
    assert score_diag["stored_final_score"] == Decimal("78.00")
    assert score_diag["sum_persisted_lost_points"] == -22
    assert score_diag["recomputed_score"] == Decimal("78")
    assert score_diag["difference"] == Decimal("0.00")

    visual_diag = visuals.area_visual_scoring_diagnostics(row)
    assert [item["metric_name"] for item in visual_diag] == [metric.metric_name for metric in visuals.AREA_SCORE_METRICS]
    assert all(item["marker_matches_persisted_subtract"] for item in visual_diag)
    assert sum(item["marker_bucket_subtract"] or 0 for item in visual_diag) == -22
    assert 100 + sum(item["marker_bucket_subtract"] or 0 for item in visual_diag) == int(row["eco_driving_score_total"])
    overrev_diag = next(item for item in visual_diag if item["metric_name"] == "overrev_events_count")
    assert overrev_diag == {
        "metric_key": "overrev",
        "metric_name": "overrev_events_count",
        "metric_label": "Nadmierne obroty",
        "display_per_100km": "2",
        "persisted_subtract": -4,
        "marker_bucket_label": "1–2",
        "marker_bucket_subtract": -4,
        "marker_matches_persisted_subtract": True,
        "display_rate_matches_marker_bucket": True,
    }
    print("PASS: monthly visual diagnostics reconcile marker buckets with stored score arithmetic")


def test_monthly_display_zero_marker_and_persisted_subtract_are_aligned() -> None:
    row = sample_row(
        eco_driving_score_total=Decimal("100.00"),
        overrev_events_per_100km=Decimal("0"),
        harsh_braking_events_per_100km=Decimal("0"),
        harsh_acceleration_events_per_100km=Decimal("0"),
        harsh_turning_events_per_100km=Decimal("0"),
        idle_events_per_100km=Decimal("0"),
        speeding_140_160_events_per_100km=Decimal("0"),
        speeding_160_170_events_per_100km=Decimal("0"),
        speeding_170_plus_events_per_100km=Decimal("0"),
        overrev_maxpoints_subtract=Decimal("0.00"),
        harsh_braking_maxpoints_subtract=Decimal("0.00"),
        harsh_acceleration_maxpoints_subtract=Decimal("0.00"),
        harsh_turning_maxpoints_subtract=Decimal("0.00"),
        idle_maxpoints_subtract=Decimal("0.00"),
        speeding_140_160_maxpoints_subtract=Decimal("0.00"),
        speeding_160_170_maxpoints_subtract=Decimal("0.00"),
        speeding_170_plus_maxpoints_subtract=Decimal("0.00"),
    )
    fragment = visuals.render_area_score_axes_html(row)
    idle_card = _metric_card(fragment, "Postój na biegu jałowym")
    assert 'Ilość wykroczeń: <strong style="color:#1F2933;">0 na 100 km</strong>' in idle_card
    assert _metric_black_marker_indexes(idle_card) == [0]
    assert _marker_bucket_subtract(row, "Postój na biegu jałowym") == 0

    assert visuals.area_visual_display_consistency_issues(row) == []

    score_diag = visuals.monthly_score_arithmetic_diagnostic(row)
    assert score_diag["sum_persisted_lost_points"] == 0
    assert score_diag["recomputed_score"] == Decimal("100")
    assert score_diag["difference"] == Decimal("0.00")
    print("PASS: monthly displayed zero, marker bucket, and persisted subtract are aligned")


def test_monthly_templates_render_without_unresolved_placeholders() -> None:
    render_cases = [
        (sample_row(ecodriving_rating_type=rating_type), filename, "ranked")
        for rating_type, (_template_type, filename) in job.RANKED_TEMPLATE_MAP.items()
    ]
    render_cases.extend(
        (
            sample_row(
                ecodriving_rating_type=rating_type,
                ranking_included=False,
                ranking_position=None,
                ranking_total_participants=None,
            ),
            filename,
            "norank",
        )
        for rating_type, (_template_type, filename) in job.NORANK_TEMPLATE_MAP.items()
    )
    render_cases.append((sample_row(qualification_status="LOW_DISTANCE"), job.LOW_DISTANCE_TEMPLATE[1], "low_distance"))

    for row, filename, variant in render_cases:
        template_path = job.DEFAULT_TEMPLATE_DIR / filename
        template = template_path.read_text(encoding="utf-8")
        if variant != "low_distance":
            assert "{area_score_axes_html}" in template, filename
            assert "{lost_points_tiles_html}" not in template, filename
        rendered = job.render_template(template, job.build_template_context(row), template_variant=variant)
        assert "{{" not in rendered and "}}" not in rendered, filename
        assert "{area_score_axes_html}" not in rendered, filename
        assert "{month_start_date}" not in rendered, filename
        assert "{month_end_date}" not in rendered, filename
        assert "01.04.2026 - 30.04.2026" in rendered or variant == "low_distance", filename
        if variant != "low_distance":
            assert "▲" in rendered, filename
            assert "Wizualizacja punktacji według obszarów" in rendered, filename
            assert "Jak czytać pasek punktacji?" in rendered, filename
            assert "Ilość wykroczeń:" in rendered, filename
        assert_outlook_safe_fragment(rendered)
    print("PASS: all monthly templates render safely without unresolved placeholders")


def test_monthly_candidate_decisions_and_send_log_contract() -> None:
    known = sample_row()
    assert job.classify_candidate(known, already_sent=False, force_resend=False).should_send
    assert job.classify_candidate(known, already_sent=True, force_resend=False).status == "skipped_already_sent"
    assert job.classify_candidate(known, already_sent=True, force_resend=True).should_send
    assert job.classify_candidate(sample_row(recipient_email=""), already_sent=False, force_resend=False).status == "skipped_missing_email"
    assert job.classify_candidate(sample_row(ecodriving_rating_type="inny"), already_sent=False, force_resend=False).status == "skipped_unknown_rating_type"

    cur = FakeCursor({"exists": 1})
    assert job._already_sent(
        cur,
        send_log_table="public.eco_driving_monthly_email_send_log",
        client_id=CLIENT_ID,
        assigned_id="DRIVER-1",
        period_start_date=date(2026, 4, 1),
        period_end_date=date(2026, 5, 1),
        template_type="akceptowalny",
    ) is True
    sql, params = cur.executed[-1]
    assert "public.eco_driving_monthly_email_send_log" in sql
    assert params[2] == "monthly"

    migration = (REPO_ROOT / "db/client_business/035_eco_driving_monthly_email_notifications.sql").read_text(encoding="utf-8")
    safety_migration = (REPO_ROOT / "db/client_business/044_eco_email_fail_closed_idempotency.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.eco_driving_monthly_email_send_log" in migration
    assert "CHECK (report_type = 'monthly')" in migration
    assert "eco_driving_monthly_email_send_log" in safety_migration
    assert "'_normal_identity'" in safety_migration
    normal_index = safety_migration.split("EXECUTE format('CREATE UNIQUE INDEX %I ON public.%I",1)[1].split(";",1)[0]
    assert "template_type" not in normal_index
    assert "pending" in normal_index and "sent" in normal_index
    for status in (
        "pending",
        "skipped_already_sent",
        "skipped_missing_email",
        "skipped_unknown_rating_type",
        "dry_run_rendered",
        "sent",
        "failed",
    ):
        assert status in migration
    print("PASS: monthly candidate decisions and send-log idempotency use monthly report type")


def test_monthly_snapshot_consistent_routing_and_ranked_invariants() -> None:
    ranked = sample_row(ranking_included=True, current_ranking_included=False)
    assert job._select_template(ranked) == (
        "akceptowalny",
        "Miesięczne - Akceptowalni.html",
        "ranked",
    )
    norank = sample_row(
        ranking_included=False,
        current_ranking_included=True,
        ranking_position=None,
        ranking_total_participants=None,
        ecodriving_rating_type_share_percent=None,
    )
    assert job._select_template(norank) == (
        "akceptowalny",
        "Miesięczne - Akceptowalni - norank.html",
        "norank",
    )
    incomplete = sample_row(
        ranking_included=None,
        current_ranking_included=True,
        ranking_position=None,
        ranking_total_participants=None,
        ecodriving_rating_type_share_percent=None,
    )
    assert job._select_template(incomplete) is None
    decision = job.classify_candidate(incomplete, already_sent=False, force_resend=False)
    assert decision.should_send is False
    assert decision.status == "failed"
    assert decision.classification == job.INVALID_RANKING_SNAPSHOT
    assert decision.template_variant != "ranked"

    for field in (
        "ranking_position",
        "ranking_total_participants",
        "ecodriving_rating_type_share_percent",
    ):
        invalid = job.classify_candidate(
            sample_row(**{field: None}),
            already_sent=False,
            force_resend=False,
        )
        assert invalid.should_send is False, field
        assert invalid.classification == job.INVALID_RANKING_SNAPSHOT, field
        assert field in invalid.invalid_fields, field

    template = (job.DEFAULT_TEMPLATE_DIR / "Miesięczne - Akceptowalni.html").read_text(
        encoding="utf-8"
    )
    for share in (Decimal("0"), Decimal("100")):
        row = sample_row(ecodriving_rating_type_share_percent=share)
        assert job.classify_candidate(row, already_sent=False, force_resend=False).should_send
        rendered = job.render_template(
            template,
            job.build_template_context(row),
            template_variant="ranked",
        )
        assert f"{int(share)}%" in rendered

    expected = sample_row(
        ranking_position=1,
        ranking_total_participants=32,
        ecodriving_rating_type_share_percent=Decimal("34.38"),
    )
    rendered = job.render_template(
        template,
        job.build_template_context(expected),
        template_variant="ranked",
    )
    assert "#1" in rendered
    assert "34,4%" in rendered
    assert re.search(r">\s*#\s*<", rendered) is None
    assert re.search(r">\s*%\s*<", rendered) is None

    source = Path(job.__file__).read_text(encoding="utf-8")
    assert "s.ranking_included AS ranking_included" in source
    assert "c.ranking_included AS current_ranking_included" in source
    invalid_branch = source[source.index("if not decision.should_send:") : source.index(
        "template_html =", source.index("if not decision.should_send:")
    )]
    assert "continue" in invalid_branch
    assert "send_html_email" not in invalid_branch
    print("PASS: monthly routing is snapshot-consistent and invalid ranked snapshots never render or send")


def test_monthly_smtp_uses_existing_weekly_env_names() -> None:
    with EnvPatch(
        {
            "ECO_WEEKLY_EMAIL_SMTP_HOST": "",
            "ECO_WEEKLY_EMAIL_SMTP_PORT": "",
            "ECO_WEEKLY_EMAIL_SMTP_USERNAME": "",
            "ECO_WEEKLY_EMAIL_SMTP_PASSWORD": "",
        }
    ):
        job.load_smtp_settings_from_env(dry_run=True)
        try:
            job.load_smtp_settings_from_env(dry_run=False)
        except RuntimeError as exc:
            assert "ECO_WEEKLY_EMAIL_SMTP_PASSWORD" in str(exc)
        else:
            raise AssertionError("monthly non-dry-run should require existing weekly SMTP password env")
    print("PASS: monthly SMTP config reuses existing ECO_WEEKLY_EMAIL_* variables")


def main() -> None:
    test_monthly_template_inventory_and_routing()
    test_monthly_period_aliases_latest_period_and_subject()
    test_monthly_area_score_axes_are_dynamic_and_outlook_safe()
    test_monthly_marker_uses_persisted_rounded_rate_scoring()
    test_monthly_visual_diagnostics_and_score_arithmetic()
    test_monthly_display_zero_marker_and_persisted_subtract_are_aligned()
    test_monthly_templates_render_without_unresolved_placeholders()
    test_monthly_candidate_decisions_and_send_log_contract()
    test_monthly_snapshot_consistent_routing_and_ranked_invariants()
    test_monthly_smtp_uses_existing_weekly_env_names()


if __name__ == "__main__":
    main()
