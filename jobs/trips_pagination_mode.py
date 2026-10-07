from __future__ import annotations

TRIPS_PAGINATION_MODE_STRICT_META = "strict_meta"
TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1 = "data_invariants_v1"

TRIPS_PAGINATION_MODE_DEFAULT = TRIPS_PAGINATION_MODE_STRICT_META

TRIPS_PAGINATION_MODE_VALUES = (
    TRIPS_PAGINATION_MODE_STRICT_META,
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
)


def normalize_trips_pagination_mode(value: object) -> str:
    if value is None:
        return TRIPS_PAGINATION_MODE_DEFAULT
    if not isinstance(value, str) or value not in TRIPS_PAGINATION_MODE_VALUES:
        allowed = ", ".join(TRIPS_PAGINATION_MODE_VALUES)
        raise ValueError(
            f"trips_pagination_mode must be one of: {allowed}; got {value!r}"
        )
    return value
