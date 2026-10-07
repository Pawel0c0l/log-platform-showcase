"""Outlook-safe Eco Driving email visual components."""

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from jobs.ecodriving.eco_scoring import METRIC_MAX_POINTS, SCORING_RULES


SCORE_BAR_WIDTH_PX = 360
SCORE_ARROW_WIDTH_PX = 12
SCORE_BOUNDARY_COLOR = "#6B7280"
SCORE_AXIS_LABEL_WIDTH_PX = 24
SCORE_SEGMENTS = (
    ("red", 144, "#EF4444"),
    ("yellow", 162, "#FFD400"),
    ("green", 54, "#22C55E"),
)

TILE_COLORS = {
    "green": {
        "background": "#E8F7DD",
        "border": "#8AD04F",
        "points": "#166534",
    },
    "yellow": {
        "background": "#FFF7CC",
        "border": "#FFD400",
        "points": "#92400E",
    },
    "red": {
        "background": "#FEE2E2",
        "border": "#EF4444",
        "points": "#B91C1C",
    },
}

LOST_POINTS_COLOR_RULES = {
    "overrev": {
        "green": {0},
        "yellow": {-4, -8},
        "red": {-11, -15, -22, -30},
    },
    "harsh_braking": {
        "green": {0},
        "yellow": {-2, -6},
        "red": {-8, -10, -15, -20},
    },
    "harsh_acceleration": {
        "green": {0},
        "yellow": {-5},
        "red": {-10, -15, -20},
    },
    "harsh_turning": {
        "green": {0},
        "yellow": {-3},
        "red": {-6, -10, -14, -18, -20},
    },
    "idle": {
        "green": {0},
        "yellow": {-3, -6},
        "red": {-10, -15, -17, -20},
    },
    "speeding_140_160": {
        "green": {0},
        "yellow": {-5},
        "red": {-10, -15, -20, -25, -30},
    },
    "speeding_160_170": {
        "green": {0},
        "yellow": {-5},
        "red": {-10, -15, -20, -25, -30},
    },
    "speeding_170_plus": {
        "green": {0},
        "red": {-15, -22, -30},
    },
}


@dataclass(frozen=True)
class LostPointsMetric:
    key: str
    field_name: str
    label_html: str


@dataclass(frozen=True)
class AreaScoreMetric:
    key: str
    metric_name: str
    rate_field_name: str
    subtract_field_name: str
    label_html: str


@dataclass(frozen=True)
class AreaScoreSegment:
    threshold_label: str
    lost_points_label: str
    color: str


AREA_SEGMENT_GREEN = "#8BD450"
AREA_SEGMENT_YELLOW = "#FFF200"
AREA_SEGMENT_RED = "#FF0000"
AREA_SCORE_METRICS = (
    AreaScoreMetric("overrev", "overrev_events_count", "overrev_events_per_100km", "overrev_maxpoints_subtract", "Nadmierne obroty"),
    AreaScoreMetric("harsh_braking", "harsh_braking_events", "harsh_braking_events_per_100km", "harsh_braking_maxpoints_subtract", "Gwałtowne hamowania"),
    AreaScoreMetric("harsh_acceleration", "harsh_acceleration_events", "harsh_acceleration_events_per_100km", "harsh_acceleration_maxpoints_subtract", "Gwałtowne przyspieszenia"),
    AreaScoreMetric("harsh_turning", "harsh_turning_events", "harsh_turning_events_per_100km", "harsh_turning_maxpoints_subtract", "Gwałtowne skręty"),
    AreaScoreMetric("idle", "idle_events", "idle_events_per_100km", "idle_maxpoints_subtract", "Postój na biegu jałowym"),
    AreaScoreMetric("speeding_140_160", "speeding_140_160_count", "speeding_140_160_events_per_100km", "speeding_140_160_maxpoints_subtract", "Prędkość 140–160 km/h"),
    AreaScoreMetric("speeding_160_170", "speeding_160_170_count", "speeding_160_170_events_per_100km", "speeding_160_170_maxpoints_subtract", "Prędkość 160–170 km/h"),
    AreaScoreMetric("speeding_170_plus", "speeding_170_plus_count", "speeding_170_plus_events_per_100km", "speeding_170_plus_maxpoints_subtract", "Prędkość powyżej 170 km/h"),
)


LOST_POINTS_METRICS = (
    LostPointsMetric("overrev", "overrev_maxpoints_subtract", "Nadmierne obroty"),
    LostPointsMetric("harsh_braking", "harsh_braking_maxpoints_subtract", "Gwa&#322;towne hamowania"),
    LostPointsMetric("harsh_acceleration", "harsh_acceleration_maxpoints_subtract", "Gwa&#322;towne przyspieszenia"),
    LostPointsMetric("harsh_turning", "harsh_turning_maxpoints_subtract", "Gwa&#322;towne skr&#281;ty"),
    LostPointsMetric("idle", "idle_maxpoints_subtract", "Nadmierny post&#243;j pojazdu"),
    LostPointsMetric("speeding_140_160", "speeding_140_160_maxpoints_subtract", "Pr&#281;dko&#347;&#263; 140-160 km/h"),
    LostPointsMetric("speeding_160_170", "speeding_160_170_maxpoints_subtract", "Pr&#281;dko&#347;&#263; 160-170 km/h"),
    LostPointsMetric("speeding_170_plus", "speeding_170_plus_maxpoints_subtract", "Pr&#281;dko&#347;&#263; powy&#380;ej 170 km/h"),
)

SEVERITY_RANK = {"green": 0, "yellow": 1, "red": 2}


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def score_pct(value: Any) -> int:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return 0
    rounded = int(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(0, min(100, rounded))


def score_bar_arrow_widths(
    value: Any,
    *,
    bar_width_px: int = SCORE_BAR_WIDTH_PX,
    arrow_width_px: int = SCORE_ARROW_WIDTH_PX,
) -> tuple[int, int, int]:
    pct = score_pct(value)
    bar_width = int(bar_width_px)
    arrow_width = int(arrow_width_px)
    max_left_px = max(0, bar_width - arrow_width)
    raw_center_px = pct / 100 * bar_width
    left_px = round(raw_center_px - arrow_width / 2)
    left_px = max(0, min(left_px, max_left_px))
    right_px = bar_width - arrow_width - left_px
    return left_px, arrow_width, right_px


def score_axis_label_widths(
    *,
    bar_width_px: int = SCORE_BAR_WIDTH_PX,
    label_width_px: int = SCORE_AXIS_LABEL_WIDTH_PX,
) -> tuple[int, int, int, int, int]:
    bar_width = int(bar_width_px)
    label_width = int(label_width_px)
    first_label_left = round(bar_width * 0.40 - label_width / 2)
    second_label_left = round(bar_width * 0.85 - label_width / 2)
    left_spacer = max(0, first_label_left)
    middle_spacer = max(0, second_label_left - left_spacer - label_width)
    right_spacer = max(0, bar_width - left_spacer - label_width - middle_spacer - label_width)
    return left_spacer, label_width, middle_spacer, label_width, right_spacer


def _safe_hex_color(value: str | None) -> str | None:
    if value is None:
        return None
    color = str(value).strip()
    if len(color) == 7 and color.startswith("#") and all(char in "0123456789abcdefABCDEF" for char in color[1:]):
        return color.upper()
    return None


def render_score_bar_html(
    value: Any,
    *,
    bar_width_px: int = SCORE_BAR_WIDTH_PX,
    arrow_width_px: int = SCORE_ARROW_WIDTH_PX,
    background_color: str | None = None,
) -> str:
    left_px, arrow_px, right_px = score_bar_arrow_widths(
        value,
        bar_width_px=bar_width_px,
        arrow_width_px=arrow_width_px,
    )
    score_bg_color = _safe_hex_color(background_color)
    table_bg_attr = f' bgcolor="{score_bg_color}"' if score_bg_color else ""
    cell_bg_attr = f' bgcolor="{score_bg_color}"' if score_bg_color else ""
    bg_style = f' background-color:{score_bg_color};' if score_bg_color else ""
    segment_cells = []
    for name, width_px, color in SCORE_SEGMENTS:
        boundary_style = (
            f" border-left:1px solid {SCORE_BOUNDARY_COLOR};"
            if name in {"yellow", "green"}
            else ""
        )
        segment_cells.append(
            f'                          <td width="{width_px}" bgcolor="{color}" '
            f'style="width:{width_px}px; height:12px; background-color:{color};'
            f'{boundary_style} font-size:0; line-height:0;">&nbsp;</td>'
        )
    segments = "\n".join(segment_cells)
    label_left_px, label_40_px, label_middle_px, label_85_px, label_right_px = score_axis_label_widths(
        bar_width_px=bar_width_px
    )
    return f"""<table role="presentation" width="{bar_width_px}" cellspacing="0" cellpadding="0" border="0"{table_bg_attr} style="width:{bar_width_px}px; max-width:{bar_width_px}px; margin-top:22px;{bg_style}">
                      <tr>
                        <td{cell_bg_attr} style="{bg_style.lstrip()}">
                          <table role="presentation" width="{bar_width_px}" cellspacing="0" cellpadding="0" border="0"{table_bg_attr} style="width:{bar_width_px}px; max-width:{bar_width_px}px;{bg_style}">
                            <tr>
                              <td width="{left_px}"{cell_bg_attr} style="width:{left_px}px; height:16px; font-size:0; line-height:0;{bg_style}">&nbsp;</td>
                              <td width="{arrow_px}" align="center"{cell_bg_attr} style="width:{arrow_px}px; height:16px; font-size:13px; line-height:16px; color:#1F2933;{bg_style}">▼</td>
                              <td width="{right_px}"{cell_bg_attr} style="width:{right_px}px; height:16px; font-size:0; line-height:0;{bg_style}">&nbsp;</td>
                            </tr>
                          </table>
                          <table role="presentation" width="{bar_width_px}" cellspacing="0" cellpadding="0" border="0"{table_bg_attr} style="width:{bar_width_px}px; max-width:{bar_width_px}px; height:12px;{bg_style}">
                            <tr>
{segments}
                            </tr>
                          </table>
                          <table role="presentation" width="{bar_width_px}" cellspacing="0" cellpadding="0" border="0"{table_bg_attr} style="width:{bar_width_px}px; max-width:{bar_width_px}px;{bg_style}">
                            <tr>
                              <td width="{label_left_px}"{cell_bg_attr} style="width:{label_left_px}px; height:16px; font-size:0; line-height:0;{bg_style}">&nbsp;</td>
                              <td width="{label_40_px}" align="center"{cell_bg_attr} style="width:{label_40_px}px; height:16px; font-size:11px; line-height:16px; color:#4B5563; white-space:nowrap;{bg_style}">40</td>
                              <td width="{label_middle_px}"{cell_bg_attr} style="width:{label_middle_px}px; height:16px; font-size:0; line-height:0;{bg_style}">&nbsp;</td>
                              <td width="{label_85_px}" align="center"{cell_bg_attr} style="width:{label_85_px}px; height:16px; font-size:11px; line-height:16px; color:#4B5563; white-space:nowrap;{bg_style}">85</td>
                              <td width="{label_right_px}"{cell_bg_attr} style="width:{label_right_px}px; height:16px; font-size:0; line-height:0;{bg_style}">&nbsp;</td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                    </table>"""


def _rounded_lost_points_int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _format_lost_points(value: Any) -> str:
    decimal_value = _to_decimal(value)
    if decimal_value is None or decimal_value >= 0:
        return "0"
    if decimal_value == decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP):
        return str(int(decimal_value))
    rendered = format(decimal_value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP).normalize(), "f")
    return rendered.replace(".", ",")


def lost_points_color_key(metric_key: str, value: Any) -> str:
    decimal_value = _to_decimal(value)
    if decimal_value is None or decimal_value >= 0:
        return "green"

    rules = LOST_POINTS_COLOR_RULES.get(metric_key)
    if not rules:
        return "red"

    rounded_value = _rounded_lost_points_int(decimal_value)
    for color_key in ("green", "yellow", "red"):
        if rounded_value in rules.get(color_key, set()):
            return color_key

    red_values = rules.get("red", set())
    if red_values and abs(decimal_value) > max(abs(Decimal(v)) for v in red_values):
        return "red"

    candidates: list[tuple[Decimal, int, str]] = []
    for color_key, values in rules.items():
        for rule_value in values:
            diff = abs(abs(decimal_value) - abs(Decimal(rule_value)))
            candidates.append((diff, -SEVERITY_RANK[color_key], color_key))
    if not candidates:
        return "red"
    return min(candidates)[2]


def _render_lost_points_tile(metric: LostPointsMetric, value: Any) -> str:
    color_key = lost_points_color_key(metric.key, value)
    colors = TILE_COLORS[color_key]
    points = html.escape(_format_lost_points(value), quote=True)
    return f"""<table role="presentation" width="100%" height="122" cellspacing="0" cellpadding="0" border="0" style="height:122px; background-color:{colors['background']}; border:1px solid {colors['border']};">
                            <tr>
                              <td valign="top" align="center" style="height:122px; padding:14px 12px;">
                                <table role="presentation" width="100%" height="94" cellspacing="0" cellpadding="0" border="0" style="height:94px;">
                                  <tr>
                                    <td align="center" valign="top" height="34" style="height:34px; font-size:13px; line-height:17px; color:#1F2933; font-weight:bold;">
                                      {metric.label_html}
                                    </td>
                                  </tr>
                                  <tr>
                                    <td align="center" valign="middle" height="42" style="height:42px; font-size:32px; line-height:36px; color:{colors['points']}; font-weight:bold;">
                                      {points}
                                    </td>
                                  </tr>
                                  <tr>
                                    <td align="center" valign="bottom" height="18" style="height:18px; font-size:12px; line-height:16px; color:#7B8790;">
                                      utracone pkt
                                    </td>
                                  </tr>
                                </table>
                              </td>
                            </tr>
                          </table>"""


def render_lost_points_tiles_html(row: dict[str, Any]) -> str:
    rows: list[str] = []
    metrics = list(LOST_POINTS_METRICS)
    for row_index in range(0, len(metrics), 2):
        pair = metrics[row_index:row_index + 2]
        is_last_row = row_index + 2 >= len(metrics)
        cells: list[str] = []
        for col_index, metric in enumerate(pair):
            padding = (
                "0 6px 0 0" if is_last_row and col_index == 0
                else "0 0 0 6px" if is_last_row
                else "0 6px 12px 0" if col_index == 0
                else "0 0 12px 6px"
            )
            tile = _render_lost_points_tile(metric, row.get(metric.field_name))
            cells.append(
                f"""                        <td width="50%" valign="top" style="padding:{padding};">
                          {tile}
                        </td>"""
            )
        rows.append(
            """                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                      <tr>
{cells}
                      </tr>
                    </table>""".format(cells="\n\n".join(cells))
        )
    return "\n\n".join(rows)




def metric_score_bounds(metric_name: str) -> tuple[int, int]:
    rules = SCORING_RULES[metric_name]
    values = [bucket.points for bucket in rules.buckets] + [rules.final_points]
    return min(values), max(values)


def area_score_from_subtract(metric: AreaScoreMetric, value: Any) -> int:
    max_points = METRIC_MAX_POINTS[metric.metric_name]
    min_points, max_allowed = metric_score_bounds(metric.metric_name)
    decimal_value = _to_decimal(value)
    subtract_value = Decimal("0") if decimal_value is None else decimal_value
    score_value = Decimal(max_points) + subtract_value
    rounded = int(score_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(min_points, min(max_allowed, rounded))


def _format_intish_decimal(value: Decimal) -> str:
    if value == value.quantize(Decimal("1"), rounding=ROUND_HALF_UP):
        return str(int(value))
    rendered = format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize(), "f")
    return rendered.replace(".", ",")


def _format_rate_value(value: Any) -> str:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return "brak danych"
    return _format_intish_decimal(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _format_threshold_bound(value: Decimal) -> str:
    return _format_intish_decimal(value)


def _threshold_label(previous_upper: Decimal | None, upper: Decimal | None) -> str:
    if upper is None:
        return f"&gt;{_format_threshold_bound(previous_upper or Decimal('0'))}"
    if previous_upper is None:
        if upper == 0:
            return "0"
        return f"0–{_format_threshold_bound(upper)}"
    lower = previous_upper + Decimal("1")
    if lower == upper:
        return _format_threshold_bound(upper)
    return f"{_format_threshold_bound(lower)}–{_format_threshold_bound(upper)}"


def _segment_color(*, points: int, max_points: int) -> str:
    if points == max_points:
        return AREA_SEGMENT_GREEN
    if points >= 0:
        return AREA_SEGMENT_YELLOW
    return AREA_SEGMENT_RED


def area_metric_segments(metric: AreaScoreMetric) -> tuple[AreaScoreSegment, ...]:
    rules = SCORING_RULES[metric.metric_name]
    max_points = METRIC_MAX_POINTS[metric.metric_name]
    segments: list[AreaScoreSegment] = []
    previous_upper: Decimal | None = None
    for bucket in rules.buckets:
        lost_points = min(int(bucket.points) - max_points, 0)
        segments.append(
            AreaScoreSegment(
                threshold_label=_threshold_label(previous_upper, bucket.upper_bound),
                lost_points_label=str(lost_points),
                color=_segment_color(points=int(bucket.points), max_points=max_points),
            )
        )
        previous_upper = bucket.upper_bound
    final_lost_points = min(int(rules.final_points) - max_points, 0)
    segments.append(
        AreaScoreSegment(
            threshold_label=_threshold_label(previous_upper, None),
            lost_points_label=str(final_lost_points),
            color=_segment_color(points=int(rules.final_points), max_points=max_points),
        )
    )
    return tuple(segments)


def area_marker_segment_index(metric: AreaScoreMetric, value: Any) -> int | None:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None
    rounded_value = decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    rules = SCORING_RULES[metric.metric_name]
    for index, bucket in enumerate(rules.buckets):
        if rounded_value <= bucket.upper_bound:
            return index
    return len(rules.buckets)


def area_marker_segment_index_from_subtract(metric: AreaScoreMetric, value: Any) -> int | None:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None
    subtract_value = _rounded_lost_points_int(decimal_value)
    for index, segment in enumerate(area_metric_segments(metric)):
        if int(segment.lost_points_label) == subtract_value:
            return index
    return None


def _area_marker_segment_index_for_row(metric: AreaScoreMetric, row: dict[str, Any]) -> int | None:
    if metric.subtract_field_name in row:
        return area_marker_segment_index_from_subtract(metric, row.get(metric.subtract_field_name))
    # Fallback for legacy direct callers that do not provide persisted scoring
    # columns. Monthly job rows include *_maxpoints_subtract, which is the
    # source of truth for marker placement.
    return area_marker_segment_index(metric, row.get(metric.rate_field_name))


def area_visual_scoring_diagnostics(row: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for metric in AREA_SCORE_METRICS:
        marker_index = _area_marker_segment_index_for_row(metric, row)
        rate_marker_index = area_marker_segment_index(metric, row.get(metric.rate_field_name))
        segments = area_metric_segments(metric)
        marker_bucket_subtract = (
            int(segments[marker_index].lost_points_label)
            if marker_index is not None
            else None
        )
        marker_bucket_label = (
            segments[marker_index].threshold_label
            if marker_index is not None
            else None
        )
        persisted_decimal = _to_decimal(row.get(metric.subtract_field_name))
        persisted_subtract = (
            _rounded_lost_points_int(persisted_decimal)
            if persisted_decimal is not None
            else None
        )
        diagnostics.append(
            {
                "metric_key": metric.key,
                "metric_name": metric.metric_name,
                "metric_label": metric.label_html,
                "display_per_100km": _format_rate_value(row.get(metric.rate_field_name)),
                "persisted_subtract": persisted_subtract,
                "marker_bucket_label": marker_bucket_label,
                "marker_bucket_subtract": marker_bucket_subtract,
                "marker_matches_persisted_subtract": persisted_subtract == marker_bucket_subtract,
                "display_rate_matches_marker_bucket": rate_marker_index == marker_index,
            }
        )
    return diagnostics


def area_visual_display_consistency_issues(row: dict[str, Any]) -> list[dict[str, Any]]:
    # TODO(product): use this helper if monthly email copy changes from rounded
    # display rates to raw rates, threshold ranges, or a combined display.
    return [
        item for item in area_visual_scoring_diagnostics(row)
        if not item["display_rate_matches_marker_bucket"]
    ]


def monthly_score_arithmetic_diagnostic(row: dict[str, Any]) -> dict[str, Any]:
    metric_subtractions: dict[str, int | None] = {}
    for metric in AREA_SCORE_METRICS:
        subtract_decimal = _to_decimal(row.get(metric.subtract_field_name))
        metric_subtractions[metric.subtract_field_name] = (
            _rounded_lost_points_int(subtract_decimal)
            if subtract_decimal is not None
            else None
        )

    present_subtractions = [
        value for value in metric_subtractions.values()
        if value is not None
    ]
    expected_metric_count = len(AREA_SCORE_METRICS)
    max_score = sum(METRIC_MAX_POINTS[metric.metric_name] for metric in AREA_SCORE_METRICS)
    sum_persisted_lost_points = sum(present_subtractions)
    recomputed_score = (
        Decimal(max_score + sum_persisted_lost_points)
        if len(present_subtractions) == expected_metric_count
        else None
    )
    stored_score = _to_decimal(row.get("eco_driving_score_total"))
    difference = (
        stored_score - recomputed_score
        if stored_score is not None and recomputed_score is not None
        else None
    )
    return {
        "assigned_id": row.get("assigned_id"),
        "period_start_date": row.get("period_start_date") or row.get("month_start_date"),
        "period_end_date": row.get("period_end_date") or row.get("month_end_date"),
        "stored_final_score": stored_score,
        "metric_subtractions": metric_subtractions,
        "sum_persisted_lost_points": sum_persisted_lost_points,
        "recomputed_score": recomputed_score,
        "difference": difference,
    }


def _segment_width_percent(segment_count: int) -> str:
    return f"{100 / segment_count:.6f}%"


def _render_area_score_axis(metric: AreaScoreMetric, row: dict[str, Any]) -> str:
    rate_value = row.get(metric.rate_field_name)
    marker_index = _area_marker_segment_index_for_row(metric, row)
    rate_decimal = _to_decimal(rate_value)
    if rate_decimal is None:
        rate_display = "brak danych na 100 km"
    else:
        rate_display = f"{html.escape(_format_rate_value(rate_value), quote=True)} na 100 km"
    segments = area_metric_segments(metric)
    width_percent = _segment_width_percent(len(segments))

    threshold_cells = []
    lost_points_cells = []
    color_cells = []
    marker_cells = []
    for index, segment in enumerate(segments):
        divider_style = "" if index == 0 else "border-left:1px solid #FFFFFF;"
        trailing_space = " " if index == 0 else f" {divider_style}"
        threshold_cells.append(
            f'<td width="{width_percent}" align="center" valign="middle" style="padding:6px 3px; font-size:10px; line-height:13px; color:#52616B;{trailing_space}">{segment.threshold_label}</td>'
        )
        lost_points_cells.append(
            f'<td width="{width_percent}" align="center" valign="middle" style="padding:6px 3px; font-size:12px; line-height:14px; color:#1F2933; font-weight:bold;{trailing_space}">{segment.lost_points_label}</td>'
        )
        color_cells.append(
            f'<td width="{width_percent}" height="18" style="height:18px; background-color:{segment.color}; font-size:0; line-height:0;{trailing_space}">&nbsp;</td>'
        )
        if marker_index == index:
            marker_cells.append(
                f'<td width="{width_percent}" align="center" valign="top" style="padding:2px 0 0 0; font-size:15px; line-height:15px; color:#111111; font-weight:bold;">▲</td>'
            )
        else:
            marker_cells.append(
                f'<td width="{width_percent}" align="center" valign="top" style="padding:2px 0 0 0; font-size:15px; line-height:15px; color:#FFFFFF; font-weight:bold;">&nbsp;</td>'
            )

    return f'''                    <!-- Visual Score Bar: {metric.label_html} -->
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#FFFFFF; border:1px solid #E3E8E5; margin-bottom:14px;">
                      <tr>
                        <td style="padding:16px 16px 14px 16px;">

                          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                            <tr>
                              <td align="left" valign="top" style="font-size:15px; line-height:19px; color:#1F2933; font-weight:bold;">
                                {metric.label_html}
                              </td>
                              <td align="right" valign="top" style="font-size:13px; line-height:18px; color:#52616B;">
                                Ilość wykroczeń: <strong style="color:#1F2933;">{rate_display}</strong>
                              </td>
                            </tr>
                          </table>

                          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:12px; border-collapse:collapse;">
                            <tr>
                              {''.join(threshold_cells)}
                            </tr>
                            <tr>
                              {''.join(lost_points_cells)}
                            </tr>
                            <tr>
                              {''.join(color_cells)}
                            </tr>
                            <tr>
                              {''.join(marker_cells)}
                            </tr>
                          </table>

                        </td>
                      </tr>
                    </table>'''


def _render_area_score_axes_legend() -> str:
    return '''                    <!-- Legend / explanatory image for visual score bars -->
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:14px; background-color:#F8FAFC; border:1px solid #E3E8E5;">
                      <tr>
                        <td style="padding:14px 14px 12px 14px;">
                          <div style="font-size:14px; line-height:18px; color:#1F2933; font-weight:bold;">
                            Jak czytać pasek punktacji?
                          </div>

                          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:10px; border-collapse:collapse;">
                            <tr>
                              <td width="30" align="center" valign="middle" style="padding:6px 2px; font-size:12px; line-height:14px; color:#1F2933; font-weight:bold; background-color:#FFFFFF; border:1px solid #D7DEE2;">1</td>
                              <td width="33.333333%" align="center" valign="middle" style="padding:6px 3px; font-size:10px; line-height:13px; color:#52616B; border-top:1px solid #D7DEE2; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2; background-color:#FFFFFF;">0</td>
                              <td width="33.333333%" align="center" valign="middle" style="padding:6px 3px; font-size:10px; line-height:13px; color:#52616B; border-top:1px solid #D7DEE2; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2; background-color:#FFFFFF;">1–2</td>
                              <td width="33.333333%" align="center" valign="middle" style="padding:6px 3px; font-size:10px; line-height:13px; color:#52616B; border-top:1px solid #D7DEE2; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2; background-color:#FFFFFF;">3–5</td>
                            </tr>
                            <tr>
                              <td width="30" align="center" valign="middle" style="padding:6px 2px; font-size:12px; line-height:14px; color:#1F2933; font-weight:bold; background-color:#FFFFFF; border-left:1px solid #D7DEE2; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2;">2</td>
                              <td width="33.333333%" align="center" valign="middle" style="padding:6px 3px; font-size:12px; line-height:14px; color:#1F2933; font-weight:bold; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2; background-color:#FFFFFF;">0</td>
                              <td width="33.333333%" align="center" valign="middle" style="padding:6px 3px; font-size:12px; line-height:14px; color:#1F2933; font-weight:bold; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2; background-color:#FFFFFF;">-4</td>
                              <td width="33.333333%" align="center" valign="middle" style="padding:6px 3px; font-size:12px; line-height:14px; color:#1F2933; font-weight:bold; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2; background-color:#FFFFFF;">-8</td>
                            </tr>
                            <tr>
                              <td width="30" height="16" style="height:16px; background-color:#FFFFFF; font-size:0; line-height:0; border-left:1px solid #D7DEE2; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2;">&nbsp;</td>
                              <td width="33.333333%" height="16" style="height:16px; background-color:#8BD450; font-size:0; line-height:0; border-right:1px solid #FFFFFF; border-bottom:1px solid #D7DEE2;">&nbsp;</td>
                              <td width="33.333333%" height="16" style="height:16px; background-color:#FFF200; font-size:0; line-height:0; border-right:1px solid #FFFFFF; border-bottom:1px solid #D7DEE2;">&nbsp;</td>
                              <td width="33.333333%" height="16" style="height:16px; background-color:#FF0000; font-size:0; line-height:0; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2;">&nbsp;</td>
                            </tr>
                            <tr>
                              <td width="30" align="center" valign="middle" style="padding:4px 2px; font-size:12px; line-height:15px; color:#1F2933; font-weight:bold; background-color:#FFFFFF; border-left:1px solid #D7DEE2; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2;">3</td>
                              <td colspan="3" align="left" valign="top" style="padding:0; background-color:#FFFFFF; border-right:1px solid #D7DEE2; border-bottom:1px solid #D7DEE2;">
                                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;">
                                  <tr>
                                    <td width="33.333333%" align="center" valign="bottom" style="height:16px; padding:3px 0 0 0; font-size:0; line-height:0;">&nbsp;</td>
                                    <td width="33.333333%" align="center" valign="bottom" style="height:16px; padding:3px 0 0 0; font-size:15px; line-height:15px; color:#111111; font-weight:bold;">▲</td>
                                    <td width="33.333333%" align="center" valign="bottom" style="height:16px; padding:3px 0 0 0; font-size:0; line-height:0;">&nbsp;</td>
                                  </tr>
                                </table>
                              </td>
                            </tr>
                          </table>

                          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:10px; border-collapse:collapse;">
                            <tr>
                              <td width="24" valign="top" style="font-size:12px; line-height:18px; color:#1F2933; font-weight:bold; padding:0 6px 4px 0;">1.</td>
                              <td valign="top" style="font-size:12px; line-height:18px; color:#52616B; padding:0 0 4px 0;">Odpowiadający przedział ilości wykroczeń na 100 km dla danej punktacji.</td>
                            </tr>
                            <tr>
                              <td width="24" valign="top" style="font-size:12px; line-height:18px; color:#1F2933; font-weight:bold; padding:0 6px 4px 0;">2.</td>
                              <td valign="top" style="font-size:12px; line-height:18px; color:#52616B; padding:0 0 4px 0;">Ilość utraconych punktów względem maksymalnej oceny do zdobycia.</td>
                            </tr>
                            <tr>
                              <td width="24" valign="top" style="font-size:12px; line-height:18px; color:#1F2933; font-weight:bold; padding:0 6px 0 0;">3.</td>
                              <td valign="top" style="font-size:12px; line-height:18px; color:#52616B; padding:0;">Twoja obecna punktacja.</td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                    </table>'''


def render_area_score_axes_html(row: dict[str, Any]) -> str:
    cards = "\n".join(_render_area_score_axis(metric, row) for metric in AREA_SCORE_METRICS)
    legend = _render_area_score_axes_legend()
    return f'''          <!-- Visual Scoring Details -->
          <tr>
            <td style="padding:0 32px 20px 32px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#FFFFFF; border:1px solid #E3E8E5;">
                <tr>
                  <td style="padding:22px 20px 6px 20px;">
                    <div style="font-size:18px; font-weight:bold; color:#1F2933;">
                      Wizualizacja punktacji według obszarów
                    </div>

                    <div style="font-size:13px; line-height:1.6; color:#52616B; margin-top:8px;">
                      Każdy pasek pokazuje progi punktowe dla danego obszaru. Górny wiersz pokazuje przedział
                      ilości wykroczeń na 100 km, dolny wiersz pokazuje utracone punkty względem maksymalnej
                      oceny do zdobycia, a czarny marker wskazuje Twój obecny wskaźnik.
                    </div>

{legend}
                  </td>
                </tr>

                <tr>
                  <td style="padding:12px 20px 20px 20px;">

{cards}

                    <div style="font-size:12px; line-height:1.6; color:#7B8790; margin-top:2px;">
                      Kolory pokazują orientacyjny poziom wpływu na wynik: zielony oznacza najlepszy próg,
                      żółty obszar ostrzegawczy, a czerwony próg z istotną stratą punktów.
                    </div>

                  </td>
                </tr>
              </table>
            </td>
          </tr>'''
