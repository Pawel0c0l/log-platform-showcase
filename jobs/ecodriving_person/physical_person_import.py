"""Validated, atomic CSV import for Eco Driving Person source identities."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

from jobs.ecodriving_person.normalization import (
    canonicalize_person_name,
    normalize_email,
    normalize_person_source_identity,
    person_name_group_key,
)

EXPECTED_COLUMNS = (
    "client_id",
    "person_id",
    "person_name",
    "email",
    "ranking_included",
    "is_active",
    "metadata_json",
    "created_at",
    "updated_at",
)
DEFAULT_ENCODING = "cp1250"
DEFAULT_DELIMITER = ";"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _boolean(value: Any, *, field: str, row_number: int) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y", "on", "tak"}:
        return True
    if normalized in {"false", "0", "no", "n", "off", "nie"}:
        return False
    raise ValueError(f"row {row_number}: {field} must be boolean")


def _metadata(value: Any, *, row_number: int) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"row {row_number}: metadata_json is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"row {row_number}: metadata_json must be a JSON object")
    return parsed


def inspect_and_validate_csv(
    path: Path,
    *,
    expected_client_id: str,
    encoding: str = DEFAULT_ENCODING,
    delimiter: str = DEFAULT_DELIMITER,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    absolute_path = path.expanduser().resolve()
    raw_bytes = absolute_path.read_bytes()
    text = raw_bytes.decode(encoding, errors="strict")
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    headers = tuple(reader.fieldnames or ())
    report: dict[str, Any] = {
        "absolute_path": str(absolute_path),
        "file_size_bytes": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "encoding": encoding,
        "delimiter": delimiter,
        "headers": list(headers),
        "raw_rows": 0,
        "duplicate_input_rows": 0,
        "duplicate_source_row_numbers": [],
        "logical_rows": 0,
        "normalized_source_collisions": [],
        "physical_person_groups": 0,
        "group_conflicts": [],
        "blocking_errors": [],
    }
    if headers != EXPECTED_COLUMNS:
        report["blocking_errors"].append(
            {
                "code": "INVALID_HEADERS",
                "expected": list(EXPECTED_COLUMNS),
                "observed": list(headers),
            }
        )
        return [], report

    raw_rows: list[tuple[int, dict[str, str]]] = [
        (row_number, dict(row))
        for row_number, row in enumerate(reader, start=2)
    ]
    report["raw_rows"] = len(raw_rows)

    unique_rows: list[tuple[int, dict[str, str]]] = []
    first_occurrence: dict[tuple[str, ...], int] = {}
    for row_number, row in raw_rows:
        signature = tuple(row.get(column, "") for column in EXPECTED_COLUMNS)
        first_row = first_occurrence.get(signature)
        if first_row is not None:
            report["duplicate_source_row_numbers"].append(
                {"retained_row": first_row, "duplicate_row": row_number}
            )
            continue
        first_occurrence[signature] = row_number
        unique_rows.append((row_number, row))
    report["duplicate_input_rows"] = len(report["duplicate_source_row_numbers"])
    report["logical_rows"] = len(unique_rows)

    prepared: list[dict[str, Any]] = []
    for row_number, row in unique_rows:
        errors: list[str] = []
        row_client_id = str(row.get("client_id") or "").strip()
        source_person_id = str(row.get("person_id") or "").strip()
        canonical_name = canonicalize_person_name(row.get("person_name"))
        source_match_key = normalize_person_source_identity(source_person_id)
        group_key = person_name_group_key(canonical_name)
        email = str(row.get("email") or "").strip()
        normalized_email = normalize_email(email)

        if row_client_id != expected_client_id:
            errors.append("unexpected_client_id")
        if not source_person_id:
            errors.append("blank_person_id")
        elif source_match_key is None:
            errors.append("empty_normalized_person_id")
        if canonical_name is None or group_key is None:
            errors.append("blank_person_name")
        if not email or not EMAIL_RE.fullmatch(email):
            errors.append("invalid_email")

        try:
            ranking_included = _boolean(
                row.get("ranking_included"),
                field="ranking_included",
                row_number=row_number,
            )
        except ValueError:
            ranking_included = False
            errors.append("invalid_ranking_included")
        try:
            is_active = _boolean(
                row.get("is_active"),
                field="is_active",
                row_number=row_number,
            )
        except ValueError:
            is_active = False
            errors.append("invalid_is_active")
        try:
            metadata_json = _metadata(row.get("metadata_json"), row_number=row_number)
        except ValueError:
            metadata_json = {}
            errors.append("invalid_metadata_json")

        item = {
            "source_row_number": row_number,
            "client_id": row_client_id,
            "person_id": source_person_id,
            "person_id_match_key": source_match_key,
            "person_name": canonical_name,
            "person_name_group_key": group_key,
            "email": email,
            "normalized_email": normalized_email,
            "ranking_included": ranking_included,
            "is_active": is_active,
            "metadata_json": metadata_json,
        }
        if errors:
            report["blocking_errors"].append(
                {"row_number": row_number, "codes": sorted(set(errors))}
            )
        prepared.append(item)

    by_match_key: dict[str, list[dict[str, Any]]] = {}
    by_group: dict[str, list[dict[str, Any]]] = {}
    for item in prepared:
        if item["person_id_match_key"]:
            by_match_key.setdefault(item["person_id_match_key"], []).append(item)
        if item["person_name_group_key"]:
            by_group.setdefault(item["person_name_group_key"], []).append(item)

    for match_key, items in by_match_key.items():
        if len(items) > 1:
            collision = {
                "person_id_match_key": match_key,
                "row_numbers": [item["source_row_number"] for item in items],
            }
            report["normalized_source_collisions"].append(collision)
            report["blocking_errors"].append(
                {"code": "NORMALIZED_SOURCE_IDENTITY_COLLISION", **collision}
            )

    for group_key, items in by_group.items():
        conflicts: list[str] = []
        if len({item["normalized_email"] for item in items}) > 1:
            conflicts.append("email")
        if len({item["ranking_included"] for item in items}) > 1:
            conflicts.append("ranking_included")
        if len({item["is_active"] for item in items}) > 1:
            conflicts.append("is_active")
        if conflicts:
            conflict = {
                "person_name_group_key": group_key,
                "row_numbers": [item["source_row_number"] for item in items],
                "fields": conflicts,
            }
            report["group_conflicts"].append(conflict)
            report["blocking_errors"].append(
                {"code": "PHYSICAL_PERSON_GROUP_CONFLICT", **conflict}
            )

    report["physical_person_groups"] = len(by_group)
    alias_counts = [len(items) for items in by_group.values()]
    report["people_with_multiple_source_identities"] = sum(
        count > 1 for count in alias_counts
    )
    report["people_with_one_source_identity"] = sum(
        count == 1 for count in alias_counts
    )
    return prepared, report


def load_existing_people(cur, *, people_table: str, client_id: str) -> dict[str, dict[str, Any]]:
    cur.execute(
        f"""
        SELECT person_id, person_id_match_key, person_name, email,
               ranking_included, is_active, metadata_json
        FROM {people_table}
        WHERE client_id = %s
        """,
        (client_id,),
    )
    return {str(row["person_id_match_key"]): dict(row) for row in cur.fetchall()}


def classify_changes(
    rows: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
) -> dict[str, int]:
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    for row in rows:
        current = existing.get(row["person_id_match_key"])
        if current is None:
            counts["inserted"] += 1
            continue
        comparable = (
            str(current["person_id"]),
            str(current["person_name"]),
            str(current["email"] or ""),
            bool(current["ranking_included"]),
            bool(current["is_active"]),
            dict(current["metadata_json"] or {}),
        )
        desired = (
            row["person_id"],
            row["person_name"],
            row["email"],
            row["ranking_included"],
            row["is_active"],
            row["metadata_json"],
        )
        counts["unchanged" if comparable == desired else "updated"] += 1
    return counts


def apply_rows(
    cur,
    *,
    people_table: str,
    client_id: str,
    rows: list[dict[str, Any]],
) -> None:
    cur.executemany(
        f"""
        INSERT INTO {people_table} (
          client_id, person_id, person_id_match_key, person_name,
          person_name_group_key, email, ranking_included, is_active,
          metadata_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (client_id, person_id_match_key) DO UPDATE SET
          person_id = EXCLUDED.person_id,
          person_name = EXCLUDED.person_name,
          person_name_group_key = EXCLUDED.person_name_group_key,
          email = EXCLUDED.email,
          ranking_included = EXCLUDED.ranking_included,
          is_active = EXCLUDED.is_active,
          metadata_json = EXCLUDED.metadata_json,
          updated_at = now()
        """,
        [
            (
                client_id,
                row["person_id"],
                row["person_id_match_key"],
                row["person_name"],
                row["person_name_group_key"],
                row["email"],
                row["ranking_included"],
                row["is_active"],
                json.dumps(row["metadata_json"], ensure_ascii=False),
            )
            for row in rows
        ],
    )
