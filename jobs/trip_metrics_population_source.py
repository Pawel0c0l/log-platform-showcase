from __future__ import annotations

TRIP_METRICS_SOURCE_API = "api_migration"
TRIP_METRICS_SOURCE_REPORT_207 = "report_207_migration"
TRIP_METRICS_SOURCE_D105_2_ECODRIVING = "d105_2_ecodriving_migration"
TRIP_METRICS_SOURCE_DISABLED = "disabled"

TRIP_METRICS_POPULATION_SOURCE_DEFAULT = TRIP_METRICS_SOURCE_API

TRIP_METRICS_POPULATION_SOURCE_VALUES = (
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_REPORT_207,
    TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    TRIP_METRICS_SOURCE_DISABLED,
)

TRIP_METRICS_SOURCE_MISMATCH_REASON = "trip_metrics_population_source_mismatch"


def normalize_trip_metrics_population_source(value: object) -> str:
    if value is None:
        return TRIP_METRICS_POPULATION_SOURCE_DEFAULT
    normalized = str(value).strip().lower()
    if not normalized:
        return TRIP_METRICS_POPULATION_SOURCE_DEFAULT
    if normalized not in TRIP_METRICS_POPULATION_SOURCE_VALUES:
        allowed = ", ".join(TRIP_METRICS_POPULATION_SOURCE_VALUES)
        raise ValueError(
            f"trip_metrics_population_source must be one of: {allowed}; got {value!r}"
        )
    return normalized


def is_required_trip_metrics_source(selected: object, required: str) -> bool:
    return normalize_trip_metrics_population_source(selected) == required


def trip_metrics_source_skip_context(selected: object, required: str) -> dict[str, str]:
    return {
        "trip_metrics_population_source": normalize_trip_metrics_population_source(selected),
        "required_trip_metrics_population_source": required,
        "skip_reason": TRIP_METRICS_SOURCE_MISMATCH_REASON,
    }
