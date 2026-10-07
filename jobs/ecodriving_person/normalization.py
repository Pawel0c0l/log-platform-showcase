"""Unicode-aware identity helpers for the isolated Eco Driving Person workflow."""

from __future__ import annotations

import unicodedata
from typing import Any


def _nfc_text(value: Any) -> str | None:
    if value is None:
        return None
    return unicodedata.normalize("NFC", str(value))


def normalize_person_source_identity(value: Any) -> str | None:
    """Build the source-alias match key without transliteration.

    Only Unicode letters and decimal digits are retained. All whitespace,
    punctuation, symbols, and other special characters are discarded.
    """
    text = _nfc_text(value)
    if text is None:
        return None
    lowered = text.lower()
    key = "".join(
        char
        for char in lowered
        if unicodedata.category(char).startswith("L")
        or unicodedata.category(char) == "Nd"
    )
    return unicodedata.normalize("NFC", key) or None


def canonicalize_person_name(value: Any) -> str | None:
    """Return a human-readable NFC name with surrounding/repeated whitespace removed."""
    text = _nfc_text(value)
    if text is None:
        return None
    canonical = " ".join(text.strip().split())
    return unicodedata.normalize("NFC", canonical) or None


def person_name_group_key(value: Any) -> str | None:
    """Build the case-insensitive physical-person key without transliteration."""
    canonical = canonicalize_person_name(value)
    if canonical is None:
        return None
    return unicodedata.normalize("NFC", canonical.lower())


def normalize_driver_name(value: Any) -> str | None:
    """Backward-compatible name for source identity normalization."""
    return normalize_person_source_identity(value)


def normalize_person_identity_text(value: Any) -> str | None:
    """Backward-compatible name for physical-person grouping."""
    return person_name_group_key(value)


def normalize_email(value: Any) -> str | None:
    canonical = canonicalize_person_name(value)
    return canonical.lower() if canonical else None
