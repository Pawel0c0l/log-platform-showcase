#!/usr/bin/env python3
"""Manual checks for Eco Driving weekly email notification helpers."""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from email import policy
from email.parser import BytesParser
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving import email_visuals as visuals  # noqa: E402
from jobs.ecodriving import job_eco_driving_weekly_email_notifications as job  # noqa: E402


CLIENT_ID = "00000000-0000-0000-0000-000000000001"


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


def sample_row(**overrides) -> dict:
    row = {
        "client_id": CLIENT_ID,
        "client_code": "TEST",
        "assigned_id": "DRIVER-1",
        "recipient_email": "driver@example.test",
        "original_recipient_email": "driver@example.test",
        "ranking_type": "INCLUDED",
        "week_start_date": date(2026, 5, 1),
        "week_end_date": date(2026, 5, 18),
        "period_start_date": date(2026, 5, 1),
        "period_end_date": date(2026, 5, 18),
        "eco_driving_score_total": Decimal("87.00"),
        "ranking_position": 2,
        "ranking_total_participants": 10,
        "ecodriving_rating_type_share_percent": Decimal("73.50"),
        "qualification_status": "QUALIFIED",
        "ranking_included": True,
        "overrev_maxpoints_subtract": Decimal("-4.00"),
        "harsh_braking_maxpoints_subtract": Decimal("0.00"),
        "harsh_acceleration_maxpoints_subtract": Decimal("-1.50"),
        "harsh_turning_maxpoints_subtract": Decimal("0.00"),
        "idle_maxpoints_subtract": Decimal("0.00"),
        "speeding_140_160_maxpoints_subtract": Decimal("0.00"),
        "speeding_160_170_maxpoints_subtract": Decimal("0.00"),
        "speeding_170_plus_maxpoints_subtract": Decimal("0.00"),
        "top_1_validation": "Nadmierne obroty",
        "top_2_validation": None,
        "ecodriving_rating_type": "bezpieczny",
        "driver_name": "Test Driver",
    }
    row.update(overrides)
    return row


UNSUPPORTED_EMAIL_CSS = (
    "position:absolute",
    "linear-gradient",
    "calc(",
    "<script",
)


UNWANTED_SCORE_BAR_BACKGROUND_VALUES = (
    "#FFFFFF",
    "white",
    "#F4F7F5",
    "#F8FAFC",
    "#EEF2F7",
    "#F1F5F9",
    "#E5E7EB",
)


def assert_outlook_safe_fragment(fragment: str) -> None:
    lowered = fragment.lower()
    for marker in UNSUPPORTED_EMAIL_CSS:
        assert marker not in lowered, marker
    assert "<svg" not in lowered
    assert "javascript:" not in lowered


def assert_no_unwanted_score_bar_background_attrs(fragment: str) -> None:
    for value in UNWANTED_SCORE_BAR_BACKGROUND_VALUES:
        escaped = re.escape(value)
        assert not re.search(rf'background(?:-color)?\s*:\s*{escaped}', fragment, re.IGNORECASE), value
        assert not re.search(rf'bgcolor\s*=\s*["\']{escaped}["\']', fragment, re.IGNORECASE), value


def test_score_bar_rendering_boundaries_and_outlook_safety() -> None:
    test_values = (0, 39, 40, 84, 85, 100, -5, 123, None, "not-a-number")
    for value in test_values:
        fragment = visuals.render_score_bar_html(value)
        assert '#EF4444' in fragment
        assert '#FFD400' in fragment
        assert '#22C55E' in fragment
        assert 'width="144"' in fragment
        assert 'width="162"' in fragment
        assert 'width="54"' in fragment
        left_px, arrow_px, right_px = visuals.score_bar_arrow_widths(value)
        assert left_px >= 0
        assert arrow_px == visuals.SCORE_ARROW_WIDTH_PX
        assert right_px >= 0
        assert left_px + arrow_px + right_px == visuals.SCORE_BAR_WIDTH_PX
        assert f'width="{left_px}"' in fragment
        assert f'width="{right_px}"' in fragment
        assert '▼' in fragment
        assert '>40</td>' in fragment
        assert '>85</td>' in fragment
        assert '▲' not in fragment
        assert '&#9650;' not in fragment
        assert '{eco_score_bar_html}' not in fragment
        assert fragment.count(f'border-left:1px solid {visuals.SCORE_BOUNDARY_COLOR}') == 2
        assert_no_unwanted_score_bar_background_attrs(fragment)
        assert_outlook_safe_fragment(fragment)

    score_76_fragment = visuals.render_score_bar_html(76)
    assert '▼' in score_76_fragment
    assert '>40</td>' in score_76_fragment
    assert '>85</td>' in score_76_fragment
    assert '#EF4444' in score_76_fragment
    assert '#FFD400' in score_76_fragment
    assert '#22C55E' in score_76_fragment
    assert_no_unwanted_score_bar_background_attrs(score_76_fragment)
    assert_outlook_safe_fragment(score_76_fragment)

    yellow_score_76_fragment = visuals.render_score_bar_html(76, background_color="#FFFDEB")
    assert 'bgcolor="#FFFDEB"' in yellow_score_76_fragment
    assert 'background-color:#FFFDEB' in yellow_score_76_fragment
    assert '#EF4444' in yellow_score_76_fragment
    assert '#FFD400' in yellow_score_76_fragment
    assert '#22C55E' in yellow_score_76_fragment
    assert_no_unwanted_score_bar_background_attrs(yellow_score_76_fragment)
    assert_outlook_safe_fragment(yellow_score_76_fragment)

    for value, expected_center_px in ((40, 144), (85, 306)):
        left_px, arrow_px, _right_px = visuals.score_bar_arrow_widths(value)
        actual_center_px = left_px + arrow_px / 2
        assert abs(actual_center_px - expected_center_px) <= 1, (
            value,
            actual_center_px,
            expected_center_px,
        )

    label_widths = visuals.score_axis_label_widths()
    assert label_widths == (132, 24, 138, 24, 42)
    assert sum(label_widths) == visuals.SCORE_BAR_WIDTH_PX
    label_40_center_px = label_widths[0] + label_widths[1] / 2
    label_85_center_px = sum(label_widths[:3]) + label_widths[3] / 2
    assert label_40_center_px == 144
    assert label_85_center_px == 306

    segment_widths = tuple(width_px for _name, width_px, _color in visuals.SCORE_SEGMENTS)
    assert segment_widths == (144, 162, 54)
    assert sum(segment_widths) == visuals.SCORE_BAR_WIDTH_PX
    assert segment_widths[0] == 144
    assert segment_widths[0] + segment_widths[1] == 306

    score_85_fragment = visuals.render_score_bar_html(85)
    assert '▼' in score_85_fragment
    assert '>40</td>' in score_85_fragment
    assert '>85</td>' in score_85_fragment
    assert '{' not in score_85_fragment and '}' not in score_85_fragment

    assert visuals.score_bar_arrow_widths(0) == (0, 12, 348)
    assert visuals.score_bar_arrow_widths(100) == (348, 12, 0)
    assert visuals.score_bar_arrow_widths(39) == (134, 12, 214)
    assert visuals.score_bar_arrow_widths(40) == (138, 12, 210)
    assert visuals.score_bar_arrow_widths(84) == (296, 12, 52)
    assert visuals.score_bar_arrow_widths(85) == (300, 12, 48)
    print("PASS: score bar renders fixed segments with center-aligned arrow widths")

def test_lost_points_color_rules_and_fallbacks() -> None:
    for metric_key, rules in visuals.LOST_POINTS_COLOR_RULES.items():
        for expected_color, values in rules.items():
            for value in values:
                assert visuals.lost_points_color_key(metric_key, value) == expected_color
                assert visuals.lost_points_color_key(metric_key, str(value)) == expected_color

    assert visuals.lost_points_color_key("overrev", None) == "green"
    assert visuals.lost_points_color_key("overrev", "") == "green"
    assert visuals.lost_points_color_key("overrev", "bad") == "green"
    assert visuals.lost_points_color_key("overrev", 3) == "green"
    assert visuals.lost_points_color_key("overrev", -100) == "red"
    assert visuals.lost_points_color_key("unknown_metric", -1) == "red"

    row = sample_row(
        overrev_maxpoints_subtract=Decimal("-4.00"),
        harsh_braking_maxpoints_subtract=Decimal("-10.00"),
        harsh_acceleration_maxpoints_subtract=None,
    )
    fragment = visuals.render_lost_points_tiles_html(row)
    assert visuals.TILE_COLORS["yellow"]["points"] in fragment
    assert visuals.TILE_COLORS["red"]["points"] in fragment
    assert visuals.TILE_COLORS["green"]["points"] in fragment
    assert "Gwa&#322;towne hamowania" in fragment
    assert "None" not in fragment
    assert "bad" not in fragment
    assert_outlook_safe_fragment(fragment)
    print("PASS: lost-points tile colors follow per-metric rules and safe fallbacks")


def test_full_email_visual_fragments_render_without_unresolved_placeholders() -> None:
    row = sample_row(
        eco_driving_score_total=Decimal("84.00"),
        overrev_maxpoints_subtract=Decimal("-11.00"),
        harsh_braking_maxpoints_subtract=Decimal("-2.00"),
        harsh_acceleration_maxpoints_subtract=Decimal("0.00"),
        harsh_turning_maxpoints_subtract=Decimal("-3.00"),
        idle_maxpoints_subtract=Decimal("-20.00"),
        speeding_140_160_maxpoints_subtract=Decimal("-5.00"),
        speeding_160_170_maxpoints_subtract=Decimal("-10.00"),
        speeding_170_plus_maxpoints_subtract=Decimal("-15.00"),
    )
    template = (job.DEFAULT_TEMPLATE_DIR / "Tygodniowe - Bezpieczni.html").read_text(
        encoding="utf-8"
    )
    rendered = job.render_template(
        template,
        job.build_template_context(row),
        template_variant="ranked",
    )
    assert "{eco_score_bar_html}" not in rendered
    assert "{lost_points_tiles_html}" not in rendered
    assert "eco_score_bar_html" not in rendered
    assert "lost_points_tiles_html" not in rendered
    assert "{{" not in rendered and "}}" not in rendered
    assert '#EF4444' in rendered
    assert '#FFD400' in rendered
    assert '#22C55E' in rendered
    assert 'role="presentation"' in rendered
    left_px, _arrow_px, right_px = visuals.score_bar_arrow_widths(84)
    assert f'width="{left_px}"' in rendered
    assert f'width="{right_px}"' in rendered
    assert_outlook_safe_fragment(rendered)
    print("PASS: full weekly template renders generated visual fragments safely")

def test_weekly_email_uses_persisted_score_and_subtract_values() -> None:
    row = sample_row(
        eco_driving_score_total=Decimal("50.00"),
        overrev_maxpoints_subtract=Decimal("0.00"),
        idle_maxpoints_subtract=Decimal("-7.00"),
    )
    context = job.build_template_context(row)
    assert context["eco_driving_score_total"] == "50"
    assert context["idle_maxpoints_subtract"] == "-7"

    fragment = visuals.render_lost_points_tiles_html(row)
    assert "-7" in fragment
    assert "-3" not in fragment

    source = Path(job.__file__).read_text(encoding="utf-8")
    assert "calculate_eco_score" not in source
    assert "score_metric" not in source
    for column in (
        "eco_driving_score_total",
        "overrev_maxpoints_subtract",
        "idle_maxpoints_subtract",
        "speeding_170_plus_maxpoints_subtract",
    ):
        assert f"s.{column}" in source
    print("PASS: weekly email renders persisted score/subtract fields without recomputing scoring")


def _html_section(rendered: str, start_marker: str, end_marker: str) -> str:
    start = rendered.index(start_marker)
    end = rendered.index(end_marker, start)
    return rendered[start:end]


def _template_score_card_section(filename: str, end_marker: str) -> str:
    template = (job.DEFAULT_TEMPLATE_DIR / filename).read_text(encoding="utf-8")
    return _html_section(template, "<!-- Score Card -->", end_marker)


def _score_card_layout_signature(section: str) -> str:
    signature = re.sub(r'bgcolor="#[0-9A-Fa-f]{6}" ?', '', section)
    signature = re.sub(r'background-color:#[0-9A-Fa-f]{6}; ?', '', signature)
    signature = re.sub(r'border:1px solid #[0-9A-Fa-f]{6};?', 'border:1px solid THEME;', signature)
    signature = re.sub(r'color:#[0-9A-Fa-f]{6};', 'color:THEME;', signature)
    signature = re.sub(r';\s+"', ';"', signature)
    return re.sub(r"\s+", " ", signature).strip()


def test_acceptable_uses_canonical_promoted_template() -> None:
    ranked_source = job.DEFAULT_TEMPLATE_DIR / "Tygodniowe - Niebezpieczni.html"
    ranked_canonical = job.DEFAULT_TEMPLATE_DIR / "Tygodniowe - Akceptowalni.html"
    norank_source = job.DEFAULT_TEMPLATE_DIR / "Tygodniowe - Niebezpieczni - norank.html"
    norank_canonical = job.DEFAULT_TEMPLATE_DIR / "Tygodniowe - Akceptowalni - norank.html"
    diagnostic_marker = "Niebezpieczni" + "2"

    assert ranked_canonical.exists()
    assert norank_canonical.exists()
    assert not any(path.name.startswith(f"Tygodniowe - {diagnostic_marker}") for path in job.DEFAULT_TEMPLATE_DIR.glob("*.html"))
    assert diagnostic_marker not in repr(job.RANKED_TEMPLATE_MAP)
    assert diagnostic_marker not in repr(job.NORANK_TEMPLATE_MAP)
    assert diagnostic_marker not in repr(job.REQUIRED_TEMPLATE_FILENAMES)

    ranked_source_html = ranked_source.read_text(encoding="utf-8")
    ranked_canonical_html = ranked_canonical.read_text(encoding="utf-8")
    assert "kierowców akceptowalnych" in ranked_canonical_html
    assert "kierowców niebezpiecznych" not in ranked_canonical_html
    assert "<!-- Score Card -->" in ranked_canonical_html
    assert "{eco_score_bar_html}" in ranked_canonical_html

    norank_source_html = norank_source.read_text(encoding="utf-8")
    norank_canonical_html = norank_canonical.read_text(encoding="utf-8")
    assert "<!-- Score Card -->" in norank_canonical_html
    assert "{eco_score_bar_html}" in norank_canonical_html

    ranked_row = sample_row(ecodriving_rating_type="akceptowalny")
    ranked_selection = job._select_template(ranked_row)
    assert ranked_selection == ("akceptowalny", "Tygodniowe - Akceptowalni.html", "ranked")
    ranked_rendered = job.render_template(
        ranked_canonical_html,
        job.build_template_context(ranked_row),
        template_variant="ranked",
    )
    assert "{eco_score_bar_html}" not in ranked_rendered
    assert "kierowców akceptowalnych" in ranked_rendered
    assert "kierowców niebezpiecznych" not in ranked_rendered
    assert "▼" in ranked_rendered
    assert ">40</td>" in ranked_rendered
    assert ">85</td>" in ranked_rendered
    assert "▲" not in ranked_rendered
    assert_outlook_safe_fragment(ranked_rendered)

    norank_row = sample_row(
        ecodriving_rating_type="akceptowalny",
        ranking_included=False,
        ranking_position=None,
        ranking_total_participants=None,
    )
    norank_selection = job._select_template(norank_row)
    assert norank_selection == ("akceptowalny", "Tygodniowe - Akceptowalni - norank.html", "norank")
    norank_rendered = job.render_template(
        norank_canonical_html,
        job.build_template_context(norank_row),
        template_variant="norank",
    )
    assert "{eco_score_bar_html}" not in norank_rendered
    assert "▼" in norank_rendered
    assert ">40</td>" in norank_rendered
    assert ">85</td>" in norank_rendered
    assert "▲" not in norank_rendered
    assert_outlook_safe_fragment(norank_rendered)

    assert job._select_template(sample_row(ecodriving_rating_type="niebezpieczny")) == (
        "niebezpieczny",
        "Tygodniowe - Niebezpieczni.html",
        "ranked",
    )
    assert job._select_template(
        sample_row(
            ecodriving_rating_type="niebezpieczny",
            ranking_included=False,
            ranking_position=None,
            ranking_total_participants=None,
        )
    ) == ("niebezpieczny", "Tygodniowe - Niebezpieczni - norank.html", "norank")
    assert job._select_template(sample_row(ecodriving_rating_type="bezpieczny")) == (
        "bezpieczny",
        "Tygodniowe - Bezpieczni.html",
        "ranked",
    )
    assert job._select_template(
        sample_row(
            ecodriving_rating_type="bezpieczny",
            ranking_included=False,
            ranking_position=None,
            ranking_total_participants=None,
        )
    ) == ("bezpieczny", "Tygodniowe - Bezpieczni - norank.html", "norank")
    print("PASS: acceptable drivers use promoted canonical Akceptowalni templates")

def test_template_selection() -> None:
    cases = [
        (
            sample_row(
                qualification_status="LOW_DISTANCE",
                ranking_included=True,
                ecodriving_rating_type="bezpieczny",
            ),
            ("low_distance", "Tygodniowe - Niezakwalifikowani.html", "low_distance"),
        ),
        (
            sample_row(
                qualification_status="LOW_DISTANCE",
                ranking_included=False,
                ecodriving_rating_type="niebezpieczny",
            ),
            ("low_distance", "Tygodniowe - Niezakwalifikowani.html", "low_distance"),
        ),
        (
            sample_row(
                qualification_status="NO_DISTANCE",
                ranking_included=True,
                ecodriving_rating_type="bezpieczny",
            ),
            ("low_distance", "Tygodniowe - Niezakwalifikowani.html", "low_distance"),
        ),
        (
            sample_row(
                qualification_status="NO_DISTANCE",
                ranking_included=False,
                ecodriving_rating_type="niebezpieczny",
            ),
            ("low_distance", "Tygodniowe - Niezakwalifikowani.html", "low_distance"),
        ),
        (
            sample_row(
                qualification_status=None,
                ranking_included=False,
                ecodriving_rating_type="bezpieczny",
                ranking_position=None,
                ranking_total_participants=None,
            ),
            ("bezpieczny", "Tygodniowe - Bezpieczni - norank.html", "norank"),
        ),
        (
            sample_row(
                qualification_status=None,
                ranking_included=False,
                ecodriving_rating_type="akceptowalny",
                ranking_position=None,
                ranking_total_participants=None,
            ),
            ("akceptowalny", "Tygodniowe - Akceptowalni - norank.html", "norank"),
        ),
        (
            sample_row(
                qualification_status=None,
                ranking_included=False,
                ecodriving_rating_type="niebezpieczny",
                ranking_position=None,
                ranking_total_participants=None,
            ),
            ("niebezpieczny", "Tygodniowe - Niebezpieczni - norank.html", "norank"),
        ),
        (
            sample_row(
                qualification_status=None,
                ranking_included=True,
                ecodriving_rating_type="akceptowalny",
            ),
            ("akceptowalny", "Tygodniowe - Akceptowalni.html", "ranked"),
        ),
    ]
    for row, expected in cases:
        assert job._select_template(row) == expected
    print("PASS: non-qualified, no-ranking, and ranked template priority")

def test_safe_rendering_replaces_all_placeholders() -> None:
    job.validate_template_inventory(job.DEFAULT_TEMPLATE_DIR)
    render_cases = [
        (sample_row(ecodriving_rating_type=rating_type), filename, "ranked")
        for rating_type, (_, filename) in job.RANKED_TEMPLATE_MAP.items()
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
        for rating_type, (_, filename) in job.NORANK_TEMPLATE_MAP.items()
    )
    render_cases.append(
        (
            sample_row(qualification_status="LOW_DISTANCE"),
            job.LOW_DISTANCE_TEMPLATE[1],
            "low_distance",
        )
    )
    for row, filename, variant in render_cases:
        template = (job.DEFAULT_TEMPLATE_DIR / filename).read_text(encoding="utf-8")
        rendered = job.render_template(
            template,
            job.build_template_context(row),
            template_variant=variant,
        )
        assert "{{" not in rendered and "}}" not in rendered, filename
        assert "{week_start_date}" not in rendered, filename
        assert "{ranking_position}" not in rendered, filename
        if variant == "ranked":
            assert "73,5%" in rendered, filename
            assert "20%" not in rendered, filename
    print("PASS: templates render without unresolved placeholders or old ranking formula output")

def test_polish_period_dates_and_rating_share_expression() -> None:
    row = sample_row(
        period_start_date=date(2026, 4, 1),
        period_end_date=date(2026, 5, 1),
        week_start_date=date(2026, 4, 1),
        week_end_date=date(2026, 5, 1),
        ranking_position=2,
        ranking_total_participants=10,
        ecodriving_rating_type_share_percent=Decimal("73.50"),
    )
    context = job.build_template_context(row)
    assert context["week_start_date"] == "01"
    assert context["week_end_date"] == "30.04.2026"
    assert context["period_start_date_display"] == "01.04.2026"
    assert context["period_end_date_display"] == "30.04.2026"
    assert context["period_end_date"] == "30.04.2026"

    rendered = job.render_template(
        "{{ (ranking_position / ranking_total_participants) * 100 }}% {week_start_date} - {week_end_date}",
        context,
    )
    assert "73,5%" in rendered
    assert "20%" not in rendered
    assert "2026-05-01" not in rendered
    assert "{{" not in rendered and "}}" not in rendered

    empty_context = job.build_template_context(sample_row(ecodriving_rating_type_share_percent=None))
    rendered_empty = job.render_template(
        "{ecodriving_rating_type_share_percent} {{ (ranking_position / ranking_total_participants) * 100 }}",
        empty_context,
        template_variant="low_distance",
    )
    assert "None" not in rendered_empty
    assert "NULL" not in rendered_empty
    print("PASS: period dates render in Polish format and old expression uses rating-type share")


def test_missing_top_2_validation_renders_empty() -> None:
    row = sample_row(ecodriving_rating_type="niebezpieczny", top_2_validation=None)
    template = (job.DEFAULT_TEMPLATE_DIR / "Tygodniowe - Niebezpieczni.html").read_text(
        encoding="utf-8"
    )
    rendered = job.render_template(template, job.build_template_context(row))
    assert "None" not in rendered
    assert "NULL" not in rendered
    print("PASS: missing top_2_validation renders as an empty string")


def test_candidate_decisions() -> None:
    known = sample_row()
    assert job.classify_candidate(known, already_sent=False, force_resend=False).should_send
    low_distance = job.classify_candidate(
        sample_row(qualification_status="LOW_DISTANCE", ecodriving_rating_type="inny"),
        already_sent=False,
        force_resend=False,
    )
    assert low_distance.should_send
    assert low_distance.template_type == "low_distance"
    assert low_distance.template_filename == "Tygodniowe - Niezakwalifikowani.html"
    assert low_distance.template_variant == "low_distance"

    no_distance = job.classify_candidate(
        sample_row(qualification_status="NO_DISTANCE", ecodriving_rating_type="inny"),
        already_sent=False,
        force_resend=False,
    )
    assert no_distance.should_send
    assert no_distance.template_type == "low_distance"
    assert no_distance.template_filename == "Tygodniowe - Niezakwalifikowani.html"
    assert no_distance.template_variant == "low_distance"
    assert (
        job.classify_candidate(
            sample_row(recipient_email=""),
            already_sent=False,
            force_resend=False,
        ).status
        == "skipped_missing_email"
    )
    assert (
        job.classify_candidate(
            sample_row(ecodriving_rating_type="inny"),
            already_sent=False,
            force_resend=False,
        ).status
        == "skipped_unknown_rating_type"
    )
    assert (
        job.classify_candidate(known, already_sent=True, force_resend=False).status
        == "skipped_already_sent"
    )
    assert job.classify_candidate(known, already_sent=True, force_resend=True).should_send
    print("PASS: missing email, unknown rating, already sent, and force resend decisions")


def test_snapshot_consistent_routing_and_ranked_invariants() -> None:
    ranked = sample_row(ranking_included=True, current_ranking_included=False)
    assert job._select_template(ranked) == (
        "bezpieczny",
        "Tygodniowe - Bezpieczni.html",
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
        "bezpieczny",
        "Tygodniowe - Bezpieczni - norank.html",
        "norank",
    )

    incident = sample_row(
        assigned_id="SYNTHETIC-29525",
        eco_driving_score_total=Decimal("100"),
        ranking_included=None,
        current_ranking_included=True,
        ranking_position=None,
        ranking_total_participants=None,
        ecodriving_rating_type_share_percent=None,
    )
    assert job._select_template(incident) is None
    incident_decision = job.classify_candidate(
        incident,
        already_sent=False,
        force_resend=False,
    )
    assert incident_decision.should_send is False
    assert incident_decision.status == "failed"
    assert incident_decision.classification == job.INVALID_RANKING_SNAPSHOT
    assert incident_decision.invalid_fields == ("ranking_included",)
    assert incident_decision.template_variant != "ranked"

    for field in (
        "ranking_position",
        "ranking_total_participants",
        "ecodriving_rating_type_share_percent",
    ):
        invalid = sample_row(**{field: None})
        decision = job.classify_candidate(invalid, already_sent=False, force_resend=False)
        assert decision.should_send is False, field
        assert decision.classification == job.INVALID_RANKING_SNAPSHOT, field
        assert field in decision.invalid_fields, field

    for share in (Decimal("0.00"), Decimal("100.00")):
        row = sample_row(ecodriving_rating_type_share_percent=share)
        decision = job.classify_candidate(row, already_sent=False, force_resend=False)
        assert decision.should_send
        context = job.build_template_context(row)
        assert context["ecodriving_rating_type_share_percent"] == str(int(share))

    expected = sample_row(
        ranking_position=1,
        ranking_total_participants=32,
        ecodriving_rating_type_share_percent=Decimal("34.38"),
    )
    context = job.build_template_context(expected)
    assert context["ranking_position"] == "1"
    assert context["ranking_total_participants"] == "32"
    assert context["ecodriving_rating_type_share_percent"] == "34,4"
    template = (job.DEFAULT_TEMPLATE_DIR / "Tygodniowe - Bezpieczni.html").read_text(
        encoding="utf-8"
    )
    rendered = job.render_template(template, context, template_variant="ranked")
    assert "#1" in rendered
    assert "34,4%" in rendered
    assert re.search(r">\s*#\s*<", rendered) is None
    assert re.search(r">\s*%\s*<", rendered) is None

    for invalid_context in (
        job.build_template_context(sample_row(ranking_position=None)),
        job.build_template_context(sample_row(ranking_position=0)),
        job.build_template_context(sample_row(ranking_total_participants=0)),
        job.build_template_context(sample_row(ecodriving_rating_type_share_percent="")),
        job.build_template_context(sample_row(ecodriving_rating_type_share_percent=Decimal("101"))),
    ):
        try:
            job.render_template(template, invalid_context, template_variant="ranked")
        except ValueError as exc:
            assert job.INVALID_RANKING_SNAPSHOT in str(exc)
        else:
            raise AssertionError("invalid ranked context must fail before rendering")

    overridden = dict(expected)
    overridden["test_recipient_email"] = "qa@example.test"
    assert job.classify_candidate(
        overridden,
        already_sent=False,
        force_resend=False,
    ) == job.classify_candidate(expected, already_sent=False, force_resend=False)
    assert job._resolve_explicit_period_params(
        {"period_start_date": "2026-07-01", "period_end_date": "2026-07-20"}
    ) == (date(2026, 7, 1), date(2026, 7, 20))

    source = Path(job.__file__).read_text(encoding="utf-8")
    assert "s.ranking_included AS ranking_included" in source
    assert "c.ranking_included AS current_ranking_included" in source
    invalid_branch = source[source.index("if not decision.should_send:") : source.index(
        "template_html =", source.index("if not decision.should_send:")
    )]
    assert "continue" in invalid_branch
    assert "send_html_email" not in invalid_branch
    print("PASS: weekly routing is snapshot-consistent and invalid ranked snapshots never render or send")


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.messages = []
        self.mime_bytes = []
        self.envelopes = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self, context):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def sendmail(self, sender, recipients, mime_bytes):
        # The mailer transmits the EXACT prepared bytes, so the double records
        # them and parses them back — what is asserted below is what was on
        # the wire, not a message object the caller still owned.
        self.envelopes.append((sender, tuple(recipients)))
        self.mime_bytes.append(mime_bytes)
        self.messages.append(BytesParser(policy=policy.default).parsebytes(mime_bytes))
        return {}

    def quit(self):
        self.quit_called = True


class FailingSMTP(FakeSMTP):
    def sendmail(self, sender, recipients, mime_bytes):
        raise RuntimeError("smtp unavailable")


def test_smtp_success_generates_message_id_and_html_message() -> None:
    FakeSMTP.instances = []
    settings = job.SmtpSettings(
        host="smtp.example.test",
        port=587,
        username="robot@example.test",
        password="secret",
        use_tls=True,
        use_ssl=False,
        from_email="no-reply.eco-alpha@example.invalid",
        from_name="Program Ecodriving",
        reply_to=None,
        timeout_seconds=30,
    )
    message_id = job.send_html_email(
        settings=settings,
        recipient_email="driver@example.test",
        subject="Subject",
        html_body="<p>Hello <strong>HTML</strong></p>",
        smtp_factory=FakeSMTP,
    )
    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.test"
    assert smtp.port == 587
    assert smtp.started_tls is True
    assert smtp.login_args == ("robot@example.test", "secret")
    assert smtp.quit_called is True
    assert len(smtp.messages) == 1
    msg = smtp.messages[0]
    assert msg["Message-ID"] == message_id
    assert msg["From"] == "Program Ecodriving <no-reply.eco-alpha@example.invalid>"
    assert msg["To"] == "driver@example.test"
    assert "text/html" in msg.as_string()
    assert smtp.envelopes == [("no-reply.eco-alpha@example.invalid", ("driver@example.test",))]
    print("PASS: SMTP success builds an HTML email with generated Message-ID")


def test_smtp_exception_raises_for_failed_status_path() -> None:
    settings = job.SmtpSettings(
        host="smtp.example.test",
        port=587,
        username="robot@example.test",
        password="secret",
        use_tls=False,
        use_ssl=False,
        from_email="no-reply.eco-alpha@example.invalid",
        from_name="Program Ecodriving",
        reply_to=None,
        timeout_seconds=30,
    )
    try:
        job.send_html_email(
            settings=settings,
            recipient_email="driver@example.test",
            subject="Subject",
            html_body="<p>Hello</p>",
            smtp_factory=FailingSMTP,
        )
    except RuntimeError as exc:
        assert "smtp unavailable" in str(exc)
    else:
        raise AssertionError("SMTP exception should propagate to failed status handling")
    print("PASS: SMTP exception propagates so the job can log status='failed'")


def test_env_validation_and_dry_run_password_rule() -> None:
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
            raise AssertionError("non-dry-run should require SMTP password")

    with EnvPatch(
        {
            "ECO_WEEKLY_EMAIL_SMTP_USE_TLS": "true",
            "ECO_WEEKLY_EMAIL_SMTP_USE_SSL": "true",
        }
    ):
        try:
            job.load_smtp_settings_from_env(dry_run=True)
        except RuntimeError as exc:
            assert "cannot both be true" in str(exc)
        else:
            raise AssertionError("TLS and SSL cannot both be enabled")
    print("PASS: SMTP env validation follows dry-run and TLS/SSL rules")


RENDER_ONLY_BRANCH_MARKER = "if execution.mode is ExecutionMode.RENDER_ONLY:"


def test_dry_run_branch_does_not_call_smtp_source_contract() -> None:
    # The marker is the actual render-only guard in the job. It used to be
    # `if dry_run:`; searching for the old spelling silently stopped proving
    # anything the moment the job started branching on the execution contract.
    source = Path(job.__file__).read_text(encoding="utf-8")
    start = source.index(RENDER_ONLY_BRANCH_MARKER)
    dry_run_branch = source[start : source.index("reservation = reserve_send(", start)]
    assert "dry_run_rendered" in dry_run_branch
    assert "send_html_email" not in dry_run_branch
    # Acceptance and failure are recorded through the reserved-send lifecycle,
    # never by writing a status literal beside the SMTP call.
    assert "mark_reserved_send_sent(" in source
    assert "mark_reserved_send_failed(" in source
    assert "status=\"failed\"" in source
    print("PASS: render-only branch renders/logs without SMTP and send failures use failed status")


def test_migration_creates_send_log_table_and_indexes() -> None:
    sql = (
        REPO_ROOT
        / "db"
        / "client_business"
        / "032_eco_driving_weekly_email_notifications.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.eco_driving_weekly_email_send_log" in sql
    assert "send_log_id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in sql
    assert "uq_eco_driving_weekly_email_send_log_sent_once" in sql
    assert "WHERE status = 'sent'" in sql
    assert "metadata_json->>'force_resend' IS DISTINCT FROM 'true'" in sql
    safety_sql = (REPO_ROOT / "db" / "client_business" / "044_eco_email_fail_closed_idempotency.sql").read_text(encoding="utf-8")
    normal_index = safety_sql.split("EXECUTE format('CREATE UNIQUE INDEX %I ON public.%I", 1)[1].split(";", 1)[0]
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
        assert status in sql

    grants_sql = (
        REPO_ROOT
        / "db"
        / "client_business"
        / "033_eco_driving_weekly_email_send_log_grants.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS qualification_status" in grants_sql
    assert "ADD COLUMN IF NOT EXISTS ranking_included" in grants_sql
    assert "ADD COLUMN IF NOT EXISTS template_variant" in grants_sql
    assert "'ranked', 'norank', 'low_distance'" in grants_sql
    assert "public.client_trips" in grants_sql
    assert "privilege_type IN ('SELECT', 'INSERT', 'UPDATE')" in grants_sql
    assert "GRANT %s ON TABLE public.eco_driving_weekly_email_send_log TO %I" in grants_sql
    assert "SEQUENCE" not in grants_sql.upper()

    share_sql = (
        REPO_ROOT
        / "db"
        / "client_business"
        / "034_eco_driving_rating_type_share_percent.sql"
    ).read_text(encoding="utf-8")
    assert "ecodriving_rating_type_share_percent NUMERIC(7, 2)" in share_sql
    assert "CREATE OR REPLACE VIEW public.eco_driver_weekly_trends_view" in share_sql
    assert "CREATE OR REPLACE VIEW public.eco_driver_monthly_trends_view" in share_sql
    print("PASS: migrations define send log table, statuses, index, grants, and share column")


def main() -> None:
    test_score_bar_rendering_boundaries_and_outlook_safety()
    test_lost_points_color_rules_and_fallbacks()
    test_full_email_visual_fragments_render_without_unresolved_placeholders()
    test_weekly_email_uses_persisted_score_and_subtract_values()
    test_acceptable_uses_canonical_promoted_template()
    test_template_selection()
    test_safe_rendering_replaces_all_placeholders()
    test_polish_period_dates_and_rating_share_expression()
    test_missing_top_2_validation_renders_empty()
    test_candidate_decisions()
    test_snapshot_consistent_routing_and_ranked_invariants()
    test_smtp_success_generates_message_id_and_html_message()
    test_smtp_exception_raises_for_failed_status_path()
    test_env_validation_and_dry_run_password_rule()
    test_dry_run_branch_does_not_call_smtp_source_contract()
    test_migration_creates_send_log_table_and_indexes()


if __name__ == "__main__":
    main()
