"""What may be served inline, and how a bulk archive stays bounded.

Two review findings meet here, and both have the same shape: a *declaration*
about bytes was trusted as if it were a *fact* about bytes.

PREVIEW.
    A member declaring `file_format='PDF'`, `content_type='text/html'` and
    `is_previewable=true` was embedded into the detail page. Migration `069`
    makes that member unstorable, and the publication boundary binds member
    metadata to the artifact's own. This module is the third, independent layer:
    the preview route asks it, per request, whether these authoritative bytes
    may be served inline at all. Metadata written before `069`, or a future
    write path that forgets, cannot reach an inline response through it.

    The allowlist is not new policy. `api/artifacts/preview.py` already decides
    that exactly one content type is handed to the browser as-is (`pdf_inline`)
    and that every other supported type is decoded server-side. S15 reuses that
    decision rather than inventing a second, broader one.

ARCHIVE.
    `Pobierz wszystkie (n)` summed `size_bytes` as DECLARED by the members and
    then read every object in full. A member declaring `1` over a 4 GB object
    passed the check and then allocated 4 GB. Here the limit is enforced against
    AUTHORITATIVE object metadata and again, incrementally, against the bytes
    actually read — so the bound holds even when the object is bigger than
    anything said it would be.
"""
from __future__ import annotations

import posixpath
import re

# The one content type this platform embeds. Keep in step with
# `api/artifacts/preview.py`'s `pdf_inline` decision and with migration `069`'s
# `portal_generated_report_files_previewable_content_check`.
INLINE_PREVIEW_CONTENT_TYPES = frozenset({"application/pdf"})

# `Pobierz wszystkie (n)` builds its archive in memory, so the product cap is
# also the allocation cap. The value is the approved 256 MB policy; what changed
# is that it is now enforced against real bytes.
ARCHIVE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
ARCHIVE_READ_CHUNK_BYTES = 1024 * 1024

_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")


class ArchiveTooLargeError(Exception):
    """The authorized member set does not fit the archive bound.

    Raised BEFORE and DURING byte consumption. It is a truthful product state —
    "download the files individually" — and deliberately not a storage failure,
    because the file store is working perfectly.
    """

    def __init__(self, *, total_bytes: int = 0, limit_bytes: int = ARCHIVE_MAX_TOTAL_BYTES) -> None:
        super().__init__("the report archive exceeds the size limit")
        self.total_bytes = int(total_bytes)
        self.limit_bytes = int(limit_bytes)


def may_preview_inline(content_type: str, *, is_previewable: bool) -> bool:
    """Whether these bytes may be embedded in the detail page.

    BOTH conditions are required and neither implies the other: the member must
    be marked previewable (a product decision, `RP-14`) AND the authoritative
    content type must be one this platform serves inline (a safety decision).
    """
    if not is_previewable:
        return False
    return normalize_content_type(content_type) in INLINE_PREVIEW_CONTENT_TYPES


def normalize_content_type(value) -> str:
    """`application/pdf` from `application/pdf; charset=binary`, casefolded."""
    return str(value or "").split(";")[0].strip().lower()


def safe_archive_name(value: str, *, fallback: str = "plik") -> str:
    """One archive entry name: a NAME, never a path.

    Every separator, drive marker, control character and traversal segment is
    removed rather than escaped, because an archive entry has no legitimate use
    for any of them and a consumer that rebuilds a path from the entry name is
    exactly the extraction bug this prevents.
    """
    text = str(value or "")
    # LAST SEGMENT FIRST. `../../etc/passwd` must contribute `passwd`, not a
    # mangled `.._.._etc_passwd` that still carries the shape of a path — so the
    # separators are resolved before anything else is rewritten. Windows
    # separators are normalised first, because an archive is extracted on both.
    text = posixpath.basename(text.replace("\\", "/"))
    text = _UNSAFE_NAME_CHARS.sub("_", text)
    # A name that is only dots, dashes or spaces is not a name.
    text = text.strip().strip(".").lstrip("-").strip()
    if not text:
        return fallback
    return text[:200]


def unique_archive_names(names) -> list[str]:
    """Deterministic de-duplication, in the order given.

    Two members may legitimately publish the same display filename in different
    roles, and a ZIP with two identical entry names is a file most tools resolve
    by silently dropping one. The first occurrence keeps the name; later ones
    take ` (2)`, ` (3)` … before the extension.
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    for raw in names:
        name = safe_archive_name(raw)
        key = name.casefold()
        count = seen.get(key, 0)
        seen[key] = count + 1
        if count == 0:
            result.append(name)
            continue
        stem, dot, ext = name.rpartition(".")
        candidate = f"{stem} ({count + 1}){dot}{ext}" if dot else f"{name} ({count + 1})"
        # The suffixed name could itself collide with a name declared later.
        while candidate.casefold() in seen:
            count += 1
            seen[key] = count + 1
            candidate = f"{stem} ({count + 1}){dot}{ext}" if dot else f"{name} ({count + 1})"
        seen[candidate.casefold()] = 1
        result.append(candidate)
    return result


def assert_declared_total_within_bound(sizes, *, limit_bytes: int = ARCHIVE_MAX_TOTAL_BYTES) -> int:
    """The cheap pre-flight over AUTHORITATIVE sizes.

    This is a fast refusal, never the guarantee: object metadata can be stale or
    absent, so :func:`read_object_within_bound` re-enforces the same limit
    against the bytes that actually arrive.
    """
    total = 0
    for size in sizes:
        total += max(0, int(size or 0))
        if total > limit_bytes:
            raise ArchiveTooLargeError(total_bytes=total, limit_bytes=limit_bytes)
    return total


def read_object_within_bound(body, *, consumed: int, limit_bytes: int = ARCHIVE_MAX_TOTAL_BYTES,
                             chunk_bytes: int = ARCHIVE_READ_CHUNK_BYTES) -> bytes:
    """Read one object, stopping the moment the CUMULATIVE bound is crossed.

    `consumed` is what the archive already holds. The read is chunked and the
    running total is checked after every chunk, so an object larger than anything
    declared costs at most one chunk beyond the limit rather than its own size.
    """
    parts: list[bytes] = []
    total = int(consumed)
    while True:
        chunk = body.read(chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise ArchiveTooLargeError(total_bytes=total, limit_bytes=limit_bytes)
        parts.append(chunk)
    return b"".join(parts)


__all__ = [
    "ARCHIVE_MAX_TOTAL_BYTES",
    "ARCHIVE_READ_CHUNK_BYTES",
    "ArchiveTooLargeError",
    "INLINE_PREVIEW_CONTENT_TYPES",
    "assert_declared_total_within_bound",
    "may_preview_inline",
    "normalize_content_type",
    "read_object_within_bound",
    "safe_archive_name",
    "unique_archive_names",
]
