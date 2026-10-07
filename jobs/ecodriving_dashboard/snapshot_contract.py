"""Driver Eco Dashboard V1 — versioned snapshot contract.

Authoritative design sources:
  design-handoffs/driver-eco-dashboard/as-is/v1.0/eco-driving-as-is-audit-handoff/
  design-handoffs/driver-eco-dashboard/as-is/v1.0/
      DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF/design-handoff/

This module owns:
  * the versioned contract identity (`SNAPSHOT_CONTRACT_ID` / `SCHEMA_VERSION`);
  * the canonical dashboard category catalogue and its mapping onto the
    existing `eco_scoring.REQUIRED_METRICS`;
  * the scoring band ladder derived from `eco_scoring.SCORING_RULES`;
  * the coefficient arithmetic (identical to the aggregation jobs);
  * the semantic status rule (coefficient -> existing bucket -> points -> status);
  * the pre-publication assertions A1..A13 and the privacy field ban list.

Hard invariants (owner-approved, they override any older draft):
  * the 100 km gate is a *reporting-period-level* gate only. There is no daily
    distance threshold of any kind;
  * a raw event count never determines a status. Only the normalised
    coefficient, banded by the existing Eco scoring table, does;
  * an incomplete score is never published as a dashboard.

Nothing in this module writes to a database or mutates Eco Driving behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from jobs.ecodriving.eco_scoring import (
    METRIC_MAX_POINTS,
    MIN_QUALIFYING_DISTANCE_METERS,
    RATING_FALLBACK_LABEL,
    RATING_THRESHOLDS,
    REQUIRED_METRICS,
    SCORING_RULES,
)


SNAPSHOT_CONTRACT_ID = "driver_eco_dashboard_snapshot"
SCHEMA_VERSION = 1
SNAPSHOT_LOCALE = "pl-PL"

# --- period / state vocabulary -------------------------------------------------

PERIOD_TYPE_WEEKLY = "weekly"
PERIOD_TYPE_MONTHLY = "monthly"
PERIOD_TYPES = (PERIOD_TYPE_WEEKLY, PERIOD_TYPE_MONTHLY)

QUALIFICATION_QUALIFIED = "QUALIFIED"
QUALIFICATION_LOW_DISTANCE = "LOW_DISTANCE"
QUALIFICATION_NO_DISTANCE = "NO_DISTANCE"

SNAPSHOT_STATUS_OK = "OK"
SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE = "INSUFFICIENT_DISTANCE"
SNAPSHOT_STATUS_REPORT_NOT_READY = "REPORT_NOT_READY"
SNAPSHOT_STATUSES = (
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE,
    SNAPSHOT_STATUS_REPORT_NOT_READY,
)

RANKING_STATE_RANKED = "RANKED"
RANKING_STATE_NOT_RANKED_BY_CONFIGURATION = "NOT_RANKED_BY_CONFIGURATION"
RANKING_STATE_NOT_ON_ROSTER = "NOT_ON_ROSTER"
RANKING_STATE_LEFT_RANKING = "LEFT_RANKING"
RANKING_STATES = (
    RANKING_STATE_RANKED,
    RANKING_STATE_NOT_RANKED_BY_CONFIGURATION,
    RANKING_STATE_NOT_ON_ROSTER,
    RANKING_STATE_LEFT_RANKING,
)

RANKING_TRANSITION_RANKED_TO_RANKED = "RANKED_TO_RANKED"
RANKING_TRANSITION_NEWLY_RANKED = "NEWLY_RANKED"
RANKING_TRANSITION_LEFT_RANKING = "LEFT_RANKING"
RANKING_TRANSITION_NOT_RANKED = "NOT_RANKED_TO_NOT_RANKED"
RANKING_TRANSITION_NO_BASIS = "NO_COMPARISON_BASIS"
RANKING_TRANSITIONS = (
    RANKING_TRANSITION_RANKED_TO_RANKED,
    RANKING_TRANSITION_NEWLY_RANKED,
    RANKING_TRANSITION_LEFT_RANKING,
    RANKING_TRANSITION_NOT_RANKED,
    RANKING_TRANSITION_NO_BASIS,
)

COMPARISON_KIND_PREVIOUS_CUMULATIVE_PERIOD = "PREVIOUS_CUMULATIVE_PERIOD"
COMPARISON_KIND_PREVIOUS_CLOSED_MONTH = "PREVIOUS_CLOSED_MONTH"

STATUS_GREEN = "green"
STATUS_YELLOW = "yellow"
STATUS_RED = "red"
STATUS_NEUTRAL = "neutral"
STATUSES = (STATUS_GREEN, STATUS_YELLOW, STATUS_RED, STATUS_NEUTRAL)

INSIGHT_LARGEST_LOSS = "LARGEST_LOSS"
INSIGHT_MOST_IMPROVED = "MOST_IMPROVED"
INSIGHT_MOST_DETERIORATED = "MOST_DETERIORATED"
INSIGHT_BEST_OPPORTUNITY = "BEST_OPPORTUNITY"
INSIGHT_CODES = (
    INSIGHT_LARGEST_LOSS,
    INSIGHT_MOST_IMPROVED,
    INSIGHT_MOST_DETERIORATED,
    INSIGHT_BEST_OPPORTUNITY,
)

SELECTED_BY_POINTS_LOST = "points_lost"
SELECTED_BY_COEFFICIENT_DELTA = "coefficient_delta"
SELECTED_BY_THRESHOLD_GAIN = "threshold_gain"

MAX_NEAR_THRESHOLD_CATEGORIES = 2

# Semantic rating keys. The Polish driver-facing labels stay in the frontend
# copy layer; the snapshot only carries the semantic value.
RATING_TYPE_SAFE = "safe"
RATING_TYPE_ACCEPTABLE = "acceptable"
RATING_TYPE_DANGEROUS = "dangerous"
RATING_TYPE_BY_STORED_LABEL: dict[str, str] = {
    RATING_THRESHOLDS[0][1]: RATING_TYPE_SAFE,
    RATING_THRESHOLDS[1][1]: RATING_TYPE_ACCEPTABLE,
    RATING_FALLBACK_LABEL: RATING_TYPE_DANGEROUS,
}

MIN_QUALIFYING_DISTANCE_KM = MIN_QUALIFYING_DISTANCE_METERS // 1000
SCORE_MAX = 100

TOTAL_KM_QUANTUM = Decimal("0.001")
RAW_RATE_QUANTUM = Decimal("0.0001")
STORED_RATE_QUANTUM = Decimal("1")

WEEKDAY_SHORT_LABELS_PL = ("Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd")


class SnapshotContractError(RuntimeError):
    """Raised when a snapshot violates the contract and must not be published."""

    def __init__(self, assertion: str, message: str, diagnostics: dict | None = None) -> None:
        self.assertion = assertion
        self.diagnostics = diagnostics or {}
        super().__init__(f"{assertion}: {message}")


# --- canonical category catalogue ---------------------------------------------


@dataclass(frozen=True)
class CategorySpec:
    """One dashboard category mapped onto one existing Eco scoring metric."""

    key: str
    metric: str
    label: str
    short_label: str
    points_max: int
    deemphasize: bool


# Order is REQUIRED_METRICS order, which is the canonical declaration order used
# for every deterministic tie-break in this contract.
CATEGORY_SPECS: tuple[CategorySpec, ...] = (
    CategorySpec("overrev", "overrev_events_count", "Nadmierne obroty", "Obroty", 15, True),
    CategorySpec("harsh_braking", "harsh_braking_events", "Gwałtowne hamowania", "Hamowania", 10, False),
    CategorySpec(
        "harsh_acceleration",
        "harsh_acceleration_events",
        "Gwałtowne przyspieszenia",
        "Przyspieszenia",
        10,
        False,
    ),
    CategorySpec("harsh_turning", "harsh_turning_events", "Gwałtowne skręty", "Skręty", 10, False),
    CategorySpec("idle", "idle_events", "Postój na biegu jałowym", "Postój", 10, False),
    CategorySpec("speeding_140_160", "speeding_140_160_count", "Prędkość 140–160 km/h", "140–160", 15, False),
    CategorySpec("speeding_160_170", "speeding_160_170_count", "Prędkość 160–170 km/h", "160–170", 15, False),
    CategorySpec("speeding_170_plus", "speeding_170_plus_count", "Prędkość powyżej 170 km/h", ">170", 15, False),
)

CATEGORY_BY_KEY: dict[str, CategorySpec] = {spec.key: spec for spec in CATEGORY_SPECS}
CATEGORY_BY_METRIC: dict[str, CategorySpec] = {spec.metric: spec for spec in CATEGORY_SPECS}
CATEGORY_KEYS: tuple[str, ...] = tuple(spec.key for spec in CATEGORY_SPECS)
CATEGORY_DECLARATION_INDEX: dict[str, int] = {spec.key: index for index, spec in enumerate(CATEGORY_SPECS)}


def status_for_points(points: int | None, points_max: int) -> str:
    """The one status rule of the whole product (period-level and daily)."""

    if points is None:
        return STATUS_NEUTRAL
    if int(points) == int(points_max):
        return STATUS_GREEN
    if int(points) >= 0:
        return STATUS_YELLOW
    return STATUS_RED


# --- scoring band ladder -------------------------------------------------------


@dataclass(frozen=True)
class ScoringBand:
    index: int
    label: str
    lower_bound: int | None
    upper_bound: int | None
    points: int
    points_lost: int
    status: str

    def as_public_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "upper_bound": self.upper_bound,
            "points": self.points,
            "points_lost": self.points_lost,
            "status": self.status,
        }


def _band_label(lower_bound: int, upper_bound: int | None) -> str:
    if upper_bound is None:
        return f"> {lower_bound - 1}"
    if lower_bound == upper_bound:
        return str(upper_bound)
    return f"{lower_bound}–{upper_bound}"


def build_bands(category_key: str) -> tuple[ScoringBand, ...]:
    """Return the full scoring ladder for one category.

    Derived from `eco_scoring.SCORING_RULES` — never a dashboard-local table.
    """

    spec = CATEGORY_BY_KEY.get(category_key)
    if spec is None:
        raise ValueError(f"Unknown dashboard category: {category_key!r}")
    rules = SCORING_RULES[spec.metric]
    points_max = METRIC_MAX_POINTS[spec.metric]

    bands: list[ScoringBand] = []
    lower = 0
    for index, bucket in enumerate(rules.buckets):
        upper = int(bucket.upper_bound)
        bands.append(
            ScoringBand(
                index=index,
                label=_band_label(lower, upper),
                lower_bound=lower,
                upper_bound=upper,
                points=bucket.points,
                points_lost=min(bucket.points - points_max, 0),
                status=status_for_points(bucket.points, points_max),
            )
        )
        lower = upper + 1
    bands.append(
        ScoringBand(
            index=len(bands),
            label=_band_label(lower, None),
            lower_bound=lower,
            upper_bound=None,
            points=rules.final_points,
            points_lost=min(rules.final_points - points_max, 0),
            status=status_for_points(rules.final_points, points_max),
        )
    )
    return tuple(bands)


BANDS_BY_CATEGORY: dict[str, tuple[ScoringBand, ...]] = {
    spec.key: build_bands(spec.key) for spec in CATEGORY_SPECS
}


def band_index_for_points_lost(category_key: str, points_lost: int | None) -> int | None:
    """A4 — first band whose `points_lost` equals the category's `points_lost`."""

    if points_lost is None:
        return None
    for band in BANDS_BY_CATEGORY[category_key]:
        if band.points_lost == int(points_lost):
            return band.index
    return None


# --- coefficient arithmetic ----------------------------------------------------
#
# Deliberately re-stated here rather than imported from a 1600-line job module,
# so the contract has no import-time dependency on either aggregation pipeline.
# `test_eco_dashboard_snapshot.py` asserts these agree, value for value, with
# both `job_eco_driving_aggregate` and `job_eco_driving_person_aggregate`.


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def total_kilometers_from_meters(total_distance_meters: int) -> Decimal:
    return _quantize(Decimal(int(total_distance_meters)) / Decimal("1000"), TOTAL_KM_QUANTUM)


def raw_coefficient_per_100km(event_count: int, total_kilometers: Decimal) -> Decimal | None:
    if total_kilometers <= 0:
        return None
    return _quantize((Decimal(int(event_count)) / total_kilometers) * Decimal("100"), RAW_RATE_QUANTUM)


def coefficient_per_100km(event_count: int | None, total_kilometers: Decimal) -> int | None:
    """Return the integer scoring coefficient for one category.

    `None` means "not calculable under the existing scoring contract" and maps
    to the neutral status — never to zero.
    """

    if event_count is None:
        return None
    raw = raw_coefficient_per_100km(int(event_count), total_kilometers)
    if raw is None:
        return None
    return int(_quantize(raw, STORED_RATE_QUANTUM))


def display_kilometers(total_kilometers: Decimal) -> int:
    return int(_quantize(total_kilometers, Decimal("1")))


def qualification_status_for_meters(total_distance_meters: int) -> str:
    """The existing Eco qualification gate, applied to a whole reporting period."""

    meters = int(total_distance_meters)
    if meters <= 0:
        return QUALIFICATION_NO_DISTANCE
    if meters < MIN_QUALIFYING_DISTANCE_METERS:
        return QUALIFICATION_LOW_DISTANCE
    return QUALIFICATION_QUALIFIED


def ranking_state_for_group(ranking_group: str | None, *, previously_ranked: bool) -> str:
    """Derive the driver-facing ranking state.

    The raw `ranking_group` / `ranking_included` values never leave the host.
    `LEFT_RANKING` is only produced for a driver who is not currently in the
    `EXCLUDED` population, because an `EXCLUDED` driver must never receive any
    browser-visible ranking position — current or historical.
    """

    if ranking_group == "INCLUDED":
        return RANKING_STATE_RANKED
    if ranking_group == "EXCLUDED":
        return RANKING_STATE_NOT_RANKED_BY_CONFIGURATION
    if previously_ranked:
        return RANKING_STATE_LEFT_RANKING
    return RANKING_STATE_NOT_ON_ROSTER


def weekday_short_label(day: date) -> str:
    return WEEKDAY_SHORT_LABELS_PL[day.weekday()]


# --- privacy ban list ----------------------------------------------------------

FORBIDDEN_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "assigned_id",
        "chassis_number",
        "client_code",
        "client_id",
        "day_status",
        "driver_email",
        "driver_id",
        "driver_key",
        "driver_name",
        "driver_surname",
        "driver_tag_description",
        "email",
        "email_address",
        "employee_id",
        "geofence",
        "latitude",
        "longitude",
        "min_daily_evaluation_km",
        "notification_email",
        "odometer",
        "person_name",
        "person_name_group_key",
        "phone",
        "provider_trip_id",
        "ranking_group",
        "ranking_included",
        "record_id",
        "recipient_email",
        "registration",
        "source_person_id",
        "trip_end_ts",
        "trip_mode",
        "trip_start_ts",
        "vehicle_registration",
    }
)

# Substring rules catch renamed variants of the same forbidden concept.
FORBIDDEN_FIELD_SUBSTRINGS: tuple[str, ...] = (
    "budget",
    "chassis",
    "client_code",
    "day_status",
    "driver_key",
    "driver_name",
    "driver_tag",
    "email",
    "geofence",
    "latitude",
    "longitude",
    "odometer",
    "password",
    "person_name",
    "phone",
    "ranking_group",
    "ranking_included",
    "registration",
    "secret",
    "smtp",
    "surname",
    "trip_mode",
)


def iter_document_keys(node: Any, path: str = "$") -> Any:
    """Yield `(key, path)` for every mapping key in a snapshot document."""

    if isinstance(node, Mapping):
        for key, value in node.items():
            child_path = f"{path}.{key}"
            yield str(key), child_path
            yield from iter_document_keys(value, child_path)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from iter_document_keys(value, f"{path}[{index}]")


def assert_no_forbidden_fields(document: Mapping[str, Any]) -> None:
    """A12 — the browser payload carries no identity, location or internal field."""

    for key, path in iter_document_keys(document):
        lowered = key.lower()
        if lowered in FORBIDDEN_FIELD_NAMES:
            raise SnapshotContractError("A12", f"forbidden field {key!r} at {path}")
        for fragment in FORBIDDEN_FIELD_SUBSTRINGS:
            if fragment in lowered:
                raise SnapshotContractError(
                    "A12", f"forbidden field fragment {fragment!r} in {key!r} at {path}"
                )


def assert_no_forbidden_values(document: Mapping[str, Any], banned_values: Sequence[str]) -> None:
    """Reject internal identifiers that leaked as *values* (not as field names).

    The diagnostics name the position, never the value: a refusal message must
    not become the thing that leaks the identifier it just caught.
    """

    wanted = [str(value).strip().lower() for value in banned_values if str(value).strip()]
    if not wanted:
        return
    stack: list[Any] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        elif isinstance(node, str):
            lowered = node.lower()
            for index, banned in enumerate(wanted):
                if banned and banned in lowered:
                    raise SnapshotContractError(
                        "A12", f"forbidden source value #{index} appears in the document")


# --- fixed vocabulary (A15) ----------------------------------------------------
#
# A12 refuses forbidden field NAMES and forbidden source VALUES. Neither stops
# an allowlisted string field from carrying something it was never meant to:
# the review demonstrated a person-like name and an HTML fragment surviving in
# a `label` because nothing said what a `label` is allowed to contain.
#
# A15 closes that by inverting the question. Every string in the document must
# belong to a field whose vocabulary the product contract fixes, or to a field
# whose deterministic FORMAT the contract fixes. There is no third category, so
# a newly added string-valued field fails this assertion until someone decides
# which it is — which is the point.
#
# The distinction that matters: `label`, `status`, `code`, `key` and friends are
# fixed UI vocabulary and are serialised from the canonical enums in this
# module, never from source strings. Dates, timestamps and period labels are
# genuinely dynamic but structurally deterministic, so they are validated by
# shape. Numbers, booleans and nulls are not strings and are unaffected.

_BAND_LABELS: frozenset[str] = frozenset(
    band.label for bands in BANDS_BY_CATEGORY.values() for band in bands
)
_CATEGORY_LABELS: frozenset[str] = frozenset(spec.label for spec in CATEGORY_SPECS)
_CATEGORY_SHORT_LABELS: frozenset[str] = frozenset(spec.short_label for spec in CATEGORY_SPECS)
_RATING_TYPES: frozenset[str] = frozenset(RATING_TYPE_BY_STORED_LABEL.values())
_COMPARISON_KINDS: frozenset[str] = frozenset(
    {COMPARISON_KIND_PREVIOUS_CUMULATIVE_PERIOD, COMPARISON_KIND_PREVIOUS_CLOSED_MONTH}
)
_SELECTED_BY: frozenset[str] = frozenset(
    {SELECTED_BY_POINTS_LOST, SELECTED_BY_COEFFICIENT_DELTA, SELECTED_BY_THRESHOLD_GAIN}
)
_QUALIFICATION_STATUSES: frozenset[str] = frozenset(
    {QUALIFICATION_QUALIFIED, QUALIFICATION_LOW_DISTANCE, QUALIFICATION_NO_DISTANCE}
)

#: field name -> the complete set of values that field may ever carry.
FIXED_VOCABULARY: dict[str, frozenset[str]] = {
    "contract_id": frozenset({SNAPSHOT_CONTRACT_ID}),
    "locale": frozenset({SNAPSHOT_LOCALE}),
    "period_type": frozenset(PERIOD_TYPES),
    "qualification_status": _QUALIFICATION_STATUSES,
    "ranking_state": frozenset(RANKING_STATES),
    "ranking_transition": frozenset(RANKING_TRANSITIONS),
    "rating_type": _RATING_TYPES,
    "kind": _COMPARISON_KINDS,
    "key": frozenset(CATEGORY_KEYS),
    "category_key": frozenset(CATEGORY_KEYS),
    "code": frozenset(INSIGHT_CODES),
    "selected_by": _SELECTED_BY,
    "weekday_short": frozenset(WEEKDAY_SHORT_LABELS_PL),
    # `status` is the period-entry status at the top of a period entry and the
    # traffic-light status inside a block. The two vocabularies are disjoint.
    "status": frozenset(SNAPSHOT_STATUSES) | frozenset(STATUSES),
    # `label` is a category label on a category and a band label on a band.
    "label": _CATEGORY_LABELS | _BAND_LABELS,
    "short_label": _CATEGORY_SHORT_LABELS,
    "band_label": _BAND_LABELS,
    "target_band_label": _BAND_LABELS,
}

_ISO_DATE = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_ISO_INSTANT = "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
#: `2026-07` (monthly) or `2026-07-W3` (weekly cumulative).
_PERIOD_LABEL = "^[0-9]{4}-[0-9]{2}(-W[0-9]{1,2})?$"
_IANA_TIMEZONE = "^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z][A-Za-z0-9_+-]*){1,2}$"

#: field name -> the deterministic format that field must match.
FIXED_FORMAT: dict[str, str] = {
    "generated_at_utc": _ISO_INSTANT,
    "snapshot_updated_at_utc": _ISO_INSTANT,
    "timezone": _IANA_TIMEZONE,
    "period_label": _PERIOD_LABEL,
    "basis_period_label": _PERIOD_LABEL,
    "date": _ISO_DATE,
    "start_date": _ISO_DATE,
    "end_date_display": _ISO_DATE,
    "period_start_date": _ISO_DATE,
    "period_end_date_exclusive": _ISO_DATE,
    "period_end_date_display": _ISO_DATE,
    "basis_start_date": _ISO_DATE,
    "basis_end_date_exclusive": _ISO_DATE,
    "basis_end_date_display": _ISO_DATE,
}


def _iter_string_values(node: Any, path: str = "$") -> Any:
    """Yield `(key, value, path)` for every string VALUE in the document."""

    if isinstance(node, Mapping):
        for key, value in node.items():
            child_path = f"{path}.{key}"
            if isinstance(value, str):
                yield str(key), value, child_path
            else:
                yield from _iter_string_values(value, child_path)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            child_path = f"{path}[{index}]"
            if isinstance(value, str):
                # A bare string inside a list has no field name to validate
                # against, so the contract simply does not allow one.
                yield "", value, child_path
            else:
                yield from _iter_string_values(value, child_path)


def assert_fixed_vocabulary(document: Mapping[str, Any]) -> None:
    """A15 — every published string is fixed vocabulary or a fixed format.

    Diagnostics name the field and the path, never the offending value: a
    refusal must not become the thing that leaks what it caught.
    """

    import re

    for key, value, path in _iter_string_values(document):
        allowed = FIXED_VOCABULARY.get(key)
        if allowed is not None:
            if value not in allowed:
                raise SnapshotContractError(
                    "A15", f"{key!r} at {path} is not in its fixed vocabulary"
                )
            continue
        pattern = FIXED_FORMAT.get(key)
        if pattern is not None:
            if not re.match(pattern, value):
                raise SnapshotContractError(
                    "A15", f"{key!r} at {path} does not match its required format"
                )
            continue
        raise SnapshotContractError(
            "A15", f"string-valued field {key!r} at {path} has no declared vocabulary or format"
        )


# --- pre-publication assertions ------------------------------------------------


def _require(condition: bool, assertion: str, message: str, diagnostics: dict | None = None) -> None:
    if not condition:
        raise SnapshotContractError(assertion, message, diagnostics)


def _assert_category(category: Mapping[str, Any], *, where: str) -> None:
    key = category["key"]
    spec = CATEGORY_BY_KEY[key]
    points = category["points"]
    points_max = category["points_max"]
    points_lost = category["points_lost"]
    coefficient = category["coefficient_per_100km"]

    _require(points_max == spec.points_max, "A2", f"{where}.{key}: points_max drifted from the scoring table")
    if coefficient is None:
        _require(points is None, "A3", f"{where}.{key}: points present without a coefficient")
        _require(category["status"] == STATUS_NEUTRAL, "A3", f"{where}.{key}: missing coefficient must be neutral")
        return

    _require(points == points_max + points_lost, "A2", f"{where}.{key}: points != points_max + points_lost")
    _require(
        category["status"] == status_for_points(points, points_max),
        "A3",
        f"{where}.{key}: status does not follow the points rule",
    )
    expected_marker = band_index_for_points_lost(key, points_lost)
    _require(
        category["marker_band_index"] == expected_marker,
        "A4",
        f"{where}.{key}: marker_band_index does not match points_lost",
    )


def assert_period_block(block: Mapping[str, Any]) -> None:
    """Assertions A1..A9 and A13 for one publishable period block."""

    categories = block["categories"]
    _require(len(categories) == len(CATEGORY_SPECS), "A1", "a period block must carry all eight categories")
    _require(
        [category["key"] for category in categories] == list(CATEGORY_KEYS),
        "A1",
        "categories must follow REQUIRED_METRICS declaration order",
    )

    for category in categories:
        _assert_category(category, where="period")

    _require(block["scoring_complete"] is True, "A10", "scoring_complete must be true in a published block")
    _require(
        block["qualification_status"] == QUALIFICATION_QUALIFIED,
        "A11",
        "only a QUALIFIED reporting period may be published",
    )

    total_lost = sum(int(category["points_lost"]) for category in categories)
    _require(
        int(block["eco_score_total"]) == SCORE_MAX + total_lost,
        "A1",
        "eco_score_total != 100 + sum(points_lost)",
        {"eco_score_total": block["eco_score_total"], "sum_points_lost": total_lost},
    )

    ranked = block["ranking_state"] == RANKING_STATE_RANKED
    for field in ("ranking_position", "ranking_total_participants", "rating_group_share_percent"):
        present = field in block and block[field] is not None
        _require(
            present == ranked,
            "A5",
            f"{field} must be present if and only if ranking_state == RANKED",
        )
    if ranked:
        # The contract cannot recompute a ranking — it has no roster — but the
        # two values it does carry must at least be consistent with each other,
        # so an out-of-range position cannot be published.
        position = int(block["ranking_position"])
        participants = int(block["ranking_total_participants"])
        _require(participants >= 1, "A5", "a ranked block needs at least one participant")
        _require(
            1 <= position <= participants,
            "A5",
            "ranking_position must lie within the participant count",
            {"ranking_position": position, "ranking_total_participants": participants},
        )
        share = block["rating_group_share_percent"]
        _require(
            share is None or 0 <= float(share) <= 100,
            "A5",
            "rating_group_share_percent must be a percentage",
        )

    end_exclusive = date.fromisoformat(block["period_end_date_exclusive"])
    end_display = date.fromisoformat(block["period_end_date_display"])
    _require((end_exclusive - end_display).days == 1, "A6", "period_end_date_display must be end_exclusive - 1 day")

    if block["period_type"] == PERIOD_TYPE_WEEKLY:
        start = date.fromisoformat(block["period_start_date"])
        _require(start.day == 1, "A7", "a weekly period always starts on the first day of its month")
        _require(
            block["period_label"].startswith(f"{start:%Y-%m}"),
            "A7",
            "weekly period_start_date must match the month in period_label",
        )

    days = block["days"]
    _require(
        sum(int(day["kilometers"]) for day in days) == int(block["total_kilometers"]),
        "A8",
        "daily kilometres must sum to the period distance",
    )
    for category in categories:
        key = category["key"]
        if category["count"] is None:
            continue
        daily_total = 0
        for day in days:
            for day_category in day["categories"]:
                if day_category["key"] == key:
                    daily_total += int(day_category["count"] or 0)
        _require(
            daily_total == int(category["count"]),
            "A9",
            f"daily counts for {key} do not sum to the period count",
            {"daily_total": daily_total, "period_count": category["count"]},
        )

    near = block["near_threshold"]
    _require(len(near) <= MAX_NEAR_THRESHOLD_CATEGORIES, "A13", "at most two near-threshold categories")

    _require(
        all(insight["code"] in INSIGHT_CODES for insight in block["coaching"]),
        "A13",
        "unknown coaching insight code",
    )
    _require(
        all(insight["category_key"] != "overrev" for insight in block["coaching"]),
        "A13",
        "over-rev must never be used for coaching",
    )


def assert_snapshot_document(document: Mapping[str, Any], *, banned_values: Sequence[str]) -> None:
    """Full pre-publication gate. Any failure means: do not publish."""

    _require(document["schema_version"] == SCHEMA_VERSION, "A0", "unexpected schema_version")
    _require(document["contract_id"] == SNAPSHOT_CONTRACT_ID, "A0", "unexpected contract_id")

    entries = document["periods"]
    _require(set(entries) == set(PERIOD_TYPES), "A0", "periods must carry both period-type keys")
    _require(any(entry is not None for entry in entries.values()), "A0", "a snapshot must carry one period type")

    for period_type, entry in entries.items():
        if entry is None:
            continue
        status = entry["status"]
        _require(status in SNAPSHOT_STATUSES, "A0", f"{period_type}: unknown snapshot status")
        if status == SNAPSHOT_STATUS_OK:
            _require(entry["current"] is not None, "A10", f"{period_type}: OK status without a period block")
            assert_period_block(entry["current"])
        else:
            # A10/A11 — a non-OK entry exposes no Eco result payload at all.
            _require(entry["current"] is None, status_assertion(status), f"{period_type}: Eco payload leaked")
            _require(entry["previous"] is None, status_assertion(status), f"{period_type}: previous payload leaked")
            _require(entry["series"] == [], status_assertion(status), f"{period_type}: trend series leaked")

    assert_no_forbidden_fields(document)
    assert_fixed_vocabulary(document)
    assert_no_forbidden_values(document, banned_values)


def status_assertion(status: str) -> str:
    return "A11" if status == SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE else "A10"


MAX_BROWSER_PAYLOAD_BYTES = 60 * 1024


class NonCanonicalValue(SnapshotContractError):
    """A value that has no single deterministic JSON representation."""


def _canonical_value(node: Any, path: str = "$") -> Any:
    """Reduce a document to values with exactly one canonical encoding.

    Rejects rather than coerces. `Decimal`, `datetime`, `set` and friends each
    have more than one plausible rendering, so allowing them here would make
    the byte output depend on which one a future caller happened to use.
    """

    if node is None or isinstance(node, bool):
        return node
    if isinstance(node, int):
        return node
    if isinstance(node, float):
        if node != node or node in (float("inf"), float("-inf")):
            raise NonCanonicalValue("A16", f"non-finite number at {path}")
        return node
    if isinstance(node, str):
        return node
    if isinstance(node, Mapping):
        canonical: dict[str, Any] = {}
        for key, value in node.items():
            if not isinstance(key, str):
                raise NonCanonicalValue("A16", f"non-string object key at {path}")
            canonical[key] = _canonical_value(value, f"{path}.{key}")
        return canonical
    if isinstance(node, (list, tuple)):
        return [_canonical_value(value, f"{path}[{index}]") for index, value in enumerate(node)]
    raise NonCanonicalValue("A16", f"value of type {type(node).__name__} at {path} is not canonical")


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    """The one canonical byte encoding of a snapshot document.

    Deterministic in every dimension the review named:
      * UTF-8, unescaped (`ensure_ascii=False`), so Polish labels are one
        stable byte sequence rather than `\\uXXXX` escapes;
      * object keys sorted, so two semantically identical documents built with
        different insertion order produce identical bytes;
      * compact separators, which is also the form the 60 kB browser payload
        budget is measured against;
      * `allow_nan=False` plus the explicit sweep above, so NaN/Infinity — which
        are not JSON at all — can never be emitted;
      * arrays keep their order, because array order is semantic here (category
        declaration order, day order, series order).

    Stable across process runs: nothing in the encoding depends on hash seeds,
    dict ordering, locale or the platform.
    """

    import json

    return json.dumps(
        _canonical_value(document),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def serialize_document(document: Mapping[str, Any]) -> bytes:
    """Canonical bytes, plus the browser payload budget."""

    payload = canonical_json_bytes(document)
    if len(payload) > MAX_BROWSER_PAYLOAD_BYTES:
        raise SnapshotContractError(
            "A14",
            f"browser payload {len(payload)} B exceeds the {MAX_BROWSER_PAYLOAD_BYTES} B budget",
        )
    return payload


def to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("snapshot timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
