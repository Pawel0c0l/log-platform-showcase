"""
Workflow A — deterministic per-row record_id helpers.

Every Workflow A row in a client business DB carries a deterministic UUID v5
`record_id` that is unique across the dataset and stable across re-runs. This
lets the platform:

  - upsert via `ON CONFLICT (record_id)` regardless of the underlying composite
    business key,
  - control overwrite-vs-keep behavior with a single dataset-level toggle
    (`overwrite_existing`),
  - back-reference rows from external systems without exposing internal
    composite keys.

Namespace
---------

`NAMESPACE_WORKFLOW_A` is a fixed UUID computed once via
`uuid.uuid5(uuid.NAMESPACE_DNS, "log-platform.workflow_a")` and hard-coded
below so the value is **frozen**: any change here would re-derive every
record_id and break dedup. Treat it as immutable.

Per-table formulas
------------------

The formulas below define the canonical `record_id` for each Workflow A
table. The components are joined with the ASCII Unit Separator U+001F to
avoid accidental collisions between adjacent fields. Components are always
stringified deterministically:

  - UUIDs / strings        → str(value)
  - integers               → str(int(value))
  - dates / day-of-month   → ISO-8601 date string ("YYYY-MM-DD")

If a required component is None the helper raises `ValueError` so callers
must handle missing data explicitly, not silently produce a degenerate id.

Rollout safety: existing rows do not yet have a `record_id`. The plan is:
add the column nullable (DDL 014), backfill via `scripts/backfill_record_id.py`,
validate uniqueness, then `SET NOT NULL` and add a `UNIQUE INDEX CONCURRENTLY`.
See `docs/10_scheduler_design.md` and the implementation plan.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any


# Pre-computed once via:
#   uuid.uuid5(uuid.NAMESPACE_DNS, "log-platform.workflow_a")
# Hard-coded so the value is stable even if NAMESPACE_DNS or the input
# string ever changes by accident.
NAMESPACE_WORKFLOW_A: uuid.UUID = uuid.UUID("331a59e5-a43c-4447-895a-ebc72cdd4eac")

_SEP = "\x1f"  # ASCII Unit Separator (U+001F)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require(name: str, value: Any) -> Any:
    if value is None:
        raise ValueError(f"record_id: required component is None: {name}")
    return value


def _fmt_int(name: str, value: Any) -> str:
    v = _require(name, value)
    try:
        return str(int(v))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"record_id: component {name!r} must be int-convertible, got {v!r}") from exc


def _fmt_str(name: str, value: Any) -> str:
    v = _require(name, value)
    s = str(v).strip()
    if s == "":
        raise ValueError(f"record_id: required string component is empty: {name}")
    return s


def _fmt_date(name: str, value: Any) -> str:
    """Return YYYY-MM-DD for a date / datetime / 'YYYY-MM-DD' string."""
    v = _require(name, value)
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    # accept 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS...' — keep date prefix only
    if len(s) < 10:
        raise ValueError(f"record_id: component {name!r} not parseable as date: {s!r}")
    return s[:10]


def _v5(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE_WORKFLOW_A, _SEP.join(parts))


# ---------------------------------------------------------------------------
# Per-table formulas
# ---------------------------------------------------------------------------

def for_client_trips(*, client_id: Any, provider_trip_id: Any) -> uuid.UUID:
    """record_id formula for `public.client_trips`.

    Composite business key: (client_id, provider_trip_id).
    """
    return _v5(
        "client_trips",
        _fmt_str("client_id", client_id),
        _fmt_int("provider_trip_id", provider_trip_id),
    )


def for_client_speeding_notifications(
    *, client_id: Any, provider_notification_id: Any,
) -> uuid.UUID:
    """record_id formula for `public.client_speeding_notifications`.

    Composite business key: (client_id, provider_notification_id).
    `provider_notification_id` is itself a UUID (real or job-derived deterministic).
    """
    return _v5(
        "client_speeding_notifications",
        _fmt_str("client_id", client_id),
        _fmt_str("provider_notification_id", provider_notification_id),
    )


def for_client_vehicle_daily_fuel(
    *, client_id: Any, vehicle_id: Any, day: Any,
) -> uuid.UUID:
    """record_id formula for `public.client_vehicle_daily_fuel`.

    Composite business key: (client_id, vehicle_id, day).
    """
    return _v5(
        "client_vehicle_daily_fuel",
        _fmt_str("client_id", client_id),
        _fmt_str("vehicle_id", vehicle_id),
        _fmt_date("day", day),
    )


def for_client_vehicle_driver_daily_fuel(
    *, client_id: Any, vehicle_id: Any, driver_id: Any, day: Any,
) -> uuid.UUID:
    """record_id formula for `public.client_vehicle_driver_daily_fuel`.

    Composite business key: (client_id, vehicle_id, driver_id, day).
    """
    return _v5(
        "client_vehicle_driver_daily_fuel",
        _fmt_str("client_id", client_id),
        _fmt_str("vehicle_id", vehicle_id),
        _fmt_str("driver_id", driver_id),
        _fmt_date("day", day),
    )


# ---------------------------------------------------------------------------
# Dispatch by table name (used by backfill script and tests)
# ---------------------------------------------------------------------------

def compute(table_name: str, **kwargs: Any) -> uuid.UUID:
    """Compute record_id for `table_name` using the registered formula.

    Raises `KeyError` for unknown tables. Use this for backfill / tests where
    the formula is selected dynamically; production code paths should call the
    explicit `for_*` function for static type clarity.
    """
    fn = _BY_TABLE.get(table_name)
    if fn is None:
        raise KeyError(f"No record_id formula for table: {table_name!r}")
    return fn(**kwargs)


_BY_TABLE = {
    "client_trips": for_client_trips,
    "client_speeding_notifications": for_client_speeding_notifications,
    "client_vehicle_daily_fuel": for_client_vehicle_daily_fuel,
    "client_vehicle_driver_daily_fuel": for_client_vehicle_driver_daily_fuel,
}
