from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone


LAYOUT_VERSION = 2
UNKNOWN_WORKFLOW_NAME = "workflow_unknown"
UNKNOWN_STAGE_NAME = "stage_unknown"
UNKNOWN_REPORT_TYPE = "unknown"
MISSING_RAW_FILE_ID = "na"
DEFAULT_ARTIFACT_ROLE = "artifact"
IDEMPOTENT_LAYOUT_VERSION = 1


_COMPONENT_RE = re.compile(r"[^a-z0-9_.-]+")
_UNDERSCORE_RE = re.compile(r"_+")


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sanitize_artifact_component(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_value = ascii_value.strip().lower()
    ascii_value = ascii_value.replace("/", "_").replace("\\", "_")
    ascii_value = _COMPONENT_RE.sub("_", ascii_value)
    ascii_value = _UNDERSCORE_RE.sub("_", ascii_value)
    ascii_value = ascii_value.strip("._-")
    return ascii_value or UNKNOWN_REPORT_TYPE


def _sanitize_ext(ext: str) -> str:
    value = str(ext or "").strip().lower().lstrip(".")
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value or "bin"


def sanitize_artifact_display_filename(filename: str, fallback_ext: str) -> str:
    value = unicodedata.normalize("NFKD", str(filename or ""))
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.strip().replace("/", "_").replace("\\", "_")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    value = value.strip("._-")
    if "." in value:
        stem, ext = value.rsplit(".", 1)
    else:
        stem, ext = value, fallback_ext
    stem = stem.strip("._-") or "artifact"
    return f"{stem}.{_sanitize_ext(ext)}"


def _raw_file_short_id(raw_file_id: str | None) -> str:
    if raw_file_id is None or not str(raw_file_id).strip():
        return MISSING_RAW_FILE_ID
    short_id = str(raw_file_id).strip()[:8]
    return sanitize_artifact_component(short_id) or MISSING_RAW_FILE_ID


def build_artifact_display_filename(
    report_type: str | None,
    created_at: datetime,
    raw_file_id: str | None,
    artifact_role: str,
    ext: str,
) -> str:
    dt = _utc(created_at)
    report_component = sanitize_artifact_component(report_type or UNKNOWN_REPORT_TYPE)
    timestamp_utc = dt.strftime("%Y%m%dT%H%M%SZ")
    raw_short = _raw_file_short_id(raw_file_id)
    role_component = sanitize_artifact_component(artifact_role or DEFAULT_ARTIFACT_ROLE)
    return f"{report_component}__{timestamp_utc}__{raw_short}__{role_component}.{_sanitize_ext(ext)}"


def build_artifact_object_key(
    workflow_name: str,
    stage_name: str,
    run_id: str,
    artifact_role: str,
    ext: str,
    created_at: datetime | None = None,
    report_type: str | None = None,
    raw_file_id: str | None = None,
    display_filename: str | None = None,
) -> str:
    dt = _utc(created_at or datetime.now(timezone.utc))
    workflow_component = sanitize_artifact_component(workflow_name or UNKNOWN_WORKFLOW_NAME)
    stage_component = sanitize_artifact_component(stage_name or UNKNOWN_STAGE_NAME)
    run_component = sanitize_artifact_component(run_id or MISSING_RAW_FILE_ID)
    role_component = sanitize_artifact_component(artifact_role or DEFAULT_ARTIFACT_ROLE)
    report_component = sanitize_artifact_component(report_type or UNKNOWN_REPORT_TYPE)
    filename = display_filename or build_artifact_display_filename(
        report_type=report_component,
        created_at=dt,
        raw_file_id=raw_file_id,
        artifact_role=role_component,
        ext=ext,
    )
    filename = sanitize_artifact_display_filename(filename, ext)

    parts = [
        workflow_component,
        stage_component,
        f"yyyy={dt:%Y}",
        f"mm={dt:%m}",
        f"dd={dt:%d}",
        f"run_id={run_component}",
    ]
    if report_component != UNKNOWN_REPORT_TYPE:
        parts.append(f"report_type={report_component}")
    parts.extend([role_component, filename])
    return "/".join(parts)


def build_idempotent_artifact_object_key(idempotency_scope: str, idempotency_key: str, ext: str) -> str:
    identity = f"{idempotency_scope}\0{idempotency_key}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return f"idempotent/v{IDEMPOTENT_LAYOUT_VERSION}/{digest[:2]}/{digest[2:4]}/{digest}.{_sanitize_ext(ext)}"
