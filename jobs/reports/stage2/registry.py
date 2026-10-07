from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jobs.reports.stage2.types import (
    D1041Report,
    D1047Report,
    D1052EcoDrivingReport,
    D1052Report,
    EcoDrivingDriver,
    EcoDrivingVehicle,
    N1041Report,
    AlphaGPSBazaLog,
    Report112,
    Report207,
    Report602,
    Report602EV,
)

# Python remains the runtime source of truth for Stage 2 detection and
# cleaning. ``workflow_b_control.report_type_registry`` is consulted as an
# advisory read model: Stage 2 cross-checks the DB rows on startup and logs
# warnings when the DB and Python lists drift.
REGISTERED_REPORTS = [
    D1041Report,
    N1041Report,
    Report112,
    Report207,
    AlphaGPSBazaLog,
    Report602,
    Report602EV,
    D1047Report,
    D1052EcoDrivingReport,
    D1052Report,
    EcoDrivingDriver,
    EcoDrivingVehicle,
]


def get_report_cls(report_type: str):
    for cls in REGISTERED_REPORTS:
        if cls.TYPE == report_type:
            return cls
    return None


@dataclass(frozen=True)
class DbReportTypeDefinition:
    """Advisory in-memory view of a ``report_type_registry`` row."""

    report_type: str
    enabled: bool
    implementation_status: str
    cleaner_module: str | None
    cleaner_function: str | None
    cleaner_entrypoint: str | None
    detection_module: str | None
    detection_rules: dict[str, Any]
    required_columns: list[str]
    optional_columns: list[str]
    multi_table: bool
    column_types: dict[str, str]
    id_sync_column_name: str | None
    record_id_ingredients: str | None


@dataclass(frozen=True)
class RegistryReconcileReport:
    db_definitions: dict[str, DbReportTypeDefinition]
    db_only: list[str]
    python_only: list[str]
    cleaner_mismatches: list[dict[str, Any]]
    db_unavailable_reason: str | None = None


def _load_db_report_type_rows(cur) -> list[dict[str, Any]] | None:
    """Return raw rows from ``workflow_b_control.report_type_registry``.

    Returns ``None`` when the schema/table is not present, so callers can
    treat the DB registry as optional advisory metadata.
    """

    cur.execute("SELECT to_regclass(%s) AS rel", ("workflow_b_control.report_type_registry",))
    rel_row = cur.fetchone()
    rel_value = rel_row.get("rel") if isinstance(rel_row, dict) else (rel_row[0] if rel_row else None)
    if rel_value is None:
        return None

    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'workflow_b_control'
          AND table_name = 'report_type_registry'
        """
    )
    present_columns = {
        (row["column_name"] if isinstance(row, dict) else row[0]) for row in cur.fetchall()
    }

    base_columns = [
        "report_type",
        "enabled",
        "implementation_status",
        "cleaner_module",
        "cleaner_function",
        "detection_module",
        "detection_rules",
        "required_columns",
        "optional_columns",
    ]
    optional_columns = [
        "cleaner_entrypoint",
        "multi_table",
        "column_types",
        "id_sync_column_name",
        "record_id_ingredients",
    ]
    select_columns = [c for c in base_columns + optional_columns if c in present_columns]
    cur.execute(
        f"SELECT {', '.join(select_columns)} FROM workflow_b_control.report_type_registry"
    )
    rows = []
    for raw in cur.fetchall():
        if isinstance(raw, dict):
            row = dict(raw)
        else:
            row = dict(zip(select_columns, raw))
        rows.append(row)
    return rows


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _coerce_str_dict(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def load_db_report_type_definitions(cur) -> tuple[dict[str, DbReportTypeDefinition], str | None]:
    """Load all DB-registered report types into typed definitions.

    Returns ``({}, reason)`` if the DB registry is not available. The reason
    string is suitable for logging context.
    """

    raw_rows = _load_db_report_type_rows(cur)
    if raw_rows is None:
        return {}, "report_type_registry_missing"

    definitions: dict[str, DbReportTypeDefinition] = {}
    for row in raw_rows:
        detection_rules = row.get("detection_rules")
        if not isinstance(detection_rules, dict):
            detection_rules = {}
        definitions[row["report_type"]] = DbReportTypeDefinition(
            report_type=row["report_type"],
            enabled=bool(row.get("enabled", True)),
            implementation_status=str(row.get("implementation_status") or "implemented"),
            cleaner_module=row.get("cleaner_module"),
            cleaner_function=row.get("cleaner_function"),
            cleaner_entrypoint=row.get("cleaner_entrypoint"),
            detection_module=row.get("detection_module"),
            detection_rules=detection_rules,
            required_columns=_coerce_str_list(row.get("required_columns")),
            optional_columns=_coerce_str_list(row.get("optional_columns")),
            multi_table=bool(row.get("multi_table")) if row.get("multi_table") is not None else False,
            column_types=_coerce_str_dict(row.get("column_types")),
            id_sync_column_name=row.get("id_sync_column_name"),
            record_id_ingredients=row.get("record_id_ingredients"),
        )
    return definitions, None


def _python_cleaner_entrypoint(cls) -> str:
    module = cls.__module__
    name = cls.__name__
    return f"{module}:{name}.clean"


def reconcile_registry(cur) -> RegistryReconcileReport:
    """Compare the DB registry with the Python registry; report drift.

    The Python registry remains authoritative for runtime behavior. This
    function returns a structured report that the Stage 2 job logs as
    diagnostics so operators can spot rows that exist in only one side or
    cleaner entrypoints that disagree.
    """

    db_defs, unavailable_reason = load_db_report_type_definitions(cur)
    py_types = {cls.TYPE for cls in REGISTERED_REPORTS}
    db_types = set(db_defs.keys())

    db_only = sorted(db_types - py_types)
    python_only = sorted(py_types - db_types)

    cleaner_mismatches: list[dict[str, Any]] = []
    py_by_type = {cls.TYPE: cls for cls in REGISTERED_REPORTS}
    for report_type in sorted(db_types & py_types):
        db_def = db_defs[report_type]
        py_cls = py_by_type[report_type]
        py_entrypoint = _python_cleaner_entrypoint(py_cls)
        db_entrypoint = db_def.cleaner_entrypoint
        if db_entrypoint and db_entrypoint != py_entrypoint:
            cleaner_mismatches.append(
                {
                    "report_type": report_type,
                    "python_entrypoint": py_entrypoint,
                    "db_entrypoint": db_entrypoint,
                }
            )

    return RegistryReconcileReport(
        db_definitions=db_defs,
        db_only=db_only,
        python_only=python_only,
        cleaner_mismatches=cleaner_mismatches,
        db_unavailable_reason=unavailable_reason,
    )
