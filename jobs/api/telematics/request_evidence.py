#!/usr/bin/env python3
"""M4 — durable provider-request / sub-window completeness evidence.

WHAT THIS PROVES, AND WHY NOTHING ELSE DOES IT.
    The compatibility `/trips` fetch contract already enforces almost everything
    about a sub-window *that was attempted*: transport failure, HTTP failure,
    page repeat, page overlap, unstable advisory total, total reconciliation
    failure, and every row/byte/elapsed/page budget raise
    `TelematicsProviderSafetyError` and discard the whole sub-window. A sub-window
    that returns at all in compatibility mode has already passed its invariants.

    The fact nobody records is about the **set**, not the members:

        a sub-window that was NEVER ATTEMPTED — a chunk-iteration defect, a
        silently truncated effective window, a short tiling — is invisible to
        every mechanism now in force, M3 included.

    Every sub-window that *was* attempted passes its invariants perfectly, so
    "they all succeeded" is not evidence that the effective window was covered.
    This module carries the missing statement: the sub-windows attempted
    **exactly tile** `[E_start, E_end)`, contiguous and gap-free, and every one
    of them reached a valid complete terminal state.

    See `docs/20_telematics_ingestion_permanent_repair_plan.md` §4.3, §4.3a and
    §21 (the M4 implementation contract), and `docs/12` §4–§8 for the
    `data_invariants_v1` semantics this module observes but never changes.

THE TILING UNIT IS THE JOB-LEVEL CHUNK.
    `sync_trips_and_speeding._build_trip_fetch_chunks` is what actually tiles
    the effective window: its `(request_start_ts, exclusive_end_ts)` pairs are
    contiguous and half-open, and their union is exactly `[E_start, E_end)`.
    Each chunk is then handed to `TelematicsFleetProviderClient.fetch_trips`,
    which splits it again through `iter_31d_windows`.

    That second split is 1:1 for every reachable configuration — `chunk_days` is
    bounded by `TRIPS_MAX_CHUNK_DAYS = 5` and the provider sub-window by
    `TRIPS_MAX_SUB_WINDOW_DAYS = 30` — and this module **requires** it rather
    than assuming it. A chunk that produced anything other than exactly one
    provider sub-window covering exactly its requested interval is recorded
    INCOMPLETE, so a future constant change that made the split real would stall
    coverage loudly instead of advancing it on a tiling nobody verified.
    `ops/tests_manual/test_telematics_m4_window_completeness.py` pins both the
    constant relationship and the fail-closed direction.

WHAT IT IS NOT.
    Not a raw-response archive. It records identity, position, timing, counts
    and terminal state — never provider payloads. Not an authorization: the
    dispatcher binds every identity column from its own claim, and trusts this
    carrier only for facts it cross-checks (`verify_window_completeness`).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Version of the completeness carrier. Hashed into nothing and compared
#: exactly: an unrecognized version is refused, never best-effort parsed.
WINDOW_COMPLETENESS_VERSION = "telematics-trips-window-completeness/1"

#: Terminal state of one tiling unit.
SUBWINDOW_COMPLETE = "COMPLETE"
SUBWINDOW_INCOMPLETE = "INCOMPLETE"

SUBWINDOW_STATUSES = frozenset({SUBWINDOW_COMPLETE, SUBWINDOW_INCOMPLETE})

#: The single authoritative terminal condition of `data_invariants_v1`
#: (`docs/12` §7): a short page, including an empty one. Recorded, never
#: redefined here.
TERMINATION_SHORT_PAGE = "short_page"

#: Advisory-total reconciliation state, mirroring the accepted D5 Option B rule
#: (`docs/16` §5). `absent` is a **permitted** terminal state, not a failure —
#: only a *present* total that fails to reconcile is one, and that raises inside
#: the fetch contract long before this module sees it.
#:
#: Held as literals rather than imported from `provider_client`, which would
#: drag `requests` and the whole HTTP client into every consumer of this
#: contract — including the dispatcher's gate. Drift between the two definitions
#: is prevented statically by
#: `ops/tests_manual/test_telematics_m4_window_completeness.py`.
TOTAL_RECONCILIATION_ABSENT = "absent"
TOTAL_RECONCILIATION_EXACT = "exact"

TOTAL_RECONCILIATION_STATES = frozenset({
    TOTAL_RECONCILIATION_ABSENT,
    TOTAL_RECONCILIATION_EXACT,
})

#: Why a tiling unit is INCOMPLETE. Free text is deliberately not allowed: a
#: refusal must classify, and the classification reaches `error_summary`.
INCOMPLETE_NOT_TERMINATED = "not_terminated"
INCOMPLETE_NO_SUBWINDOW = "no_provider_subwindow"
INCOMPLETE_SPLIT_UNVERIFIABLE = "provider_subwindow_split_unverifiable"
INCOMPLETE_NO_PAGES = "no_pages"

INCOMPLETE_REASONS = frozenset({
    INCOMPLETE_NOT_TERMINATED,
    INCOMPLETE_NO_SUBWINDOW,
    INCOMPLETE_SPLIT_UNVERIFIABLE,
    INCOMPLETE_NO_PAGES,
})

TRIPS_ENDPOINT = "/trips"


class WindowCompletenessError(ValueError):
    """A malformed, absent or unverifiable completeness carrier. Never advisory."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# --- refusal classifications owned here so both sides name the same cause ----
WINDOW_EVIDENCE_MALFORMED = "TRIPS_WINDOW_EVIDENCE_MALFORMED"
WINDOW_EVIDENCE_ABSENT = "TRIPS_WINDOW_EVIDENCE_ABSENT"
WINDOW_EVIDENCE_IDENTITY_MISMATCH = "TRIPS_WINDOW_EVIDENCE_IDENTITY_MISMATCH"
WINDOW_TILING_INCOMPLETE = "TRIPS_WINDOW_TILING_INCOMPLETE"
SUBWINDOW_INCOMPLETE_REFUSAL = "TRIPS_SUBWINDOW_INCOMPLETE"


# ---------------------------------------------------------------------------
# Parsing helpers — strict, never coercing, never defaulting
# ---------------------------------------------------------------------------

def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_instant(raw: object, *, field: str) -> datetime:
    text = str(raw or "").strip()
    if not text:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{field} is required"
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{field} is not an ISO-8601 instant"
        ) from exc
    if parsed.utcoffset() is None:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{field} is not timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _require_uuid(raw: object, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except (TypeError, ValueError) as exc:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{field} is not a canonical UUID"
        ) from exc


def _require_int(raw: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{field} must be a JSON integer"
        )
    if raw < minimum:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{field} must be >= {minimum}"
        )
    return raw


def _require_text(raw: object, *, field: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{field} is required"
        )
    return text


def _exact_fields(payload: object, known: Sequence[str], *, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED, f"{what} is not a JSON object"
        )
    present = set(payload)
    expected = set(known)
    unknown = sorted(present - expected)
    if unknown:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED,
            f"unknown field(s) in {what}: {', '.join(unknown)}",
        )
    missing = sorted(expected - present)
    if missing:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_MALFORMED,
            f"missing field(s) in {what}: {', '.join(missing)}",
        )
    return payload


# ---------------------------------------------------------------------------
# Immutable records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageRequestRecord:
    """One provider page request, and what came back.

    `request_id` is minted in the child at request time and is the identity a
    newly inserted `client_trips` row points at through
    `first_seen_request_id`. It is generated once and never regenerated, so the
    same value reaches the client business database and the platform evidence
    table even though nothing spans both.
    """

    request_id: str
    page: int
    wire_start_value: str
    wire_end_value: str
    request_started_at_utc: datetime
    response_received_at_utc: datetime
    http_status: int
    row_count: int

    _FIELDS = (
        "request_id", "page", "wire_start_value", "wire_end_value",
        "request_started_at_utc", "response_received_at_utc",
        "http_status", "row_count",
    )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "page": self.page,
            "wire_start_value": self.wire_start_value,
            "wire_end_value": self.wire_end_value,
            "request_started_at_utc": _iso(self.request_started_at_utc),
            "response_received_at_utc": _iso(self.response_received_at_utc),
            "http_status": self.http_status,
            "row_count": self.row_count,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> "PageRequestRecord":
        data = _exact_fields(payload, cls._FIELDS, what="a page record")
        started = _parse_instant(
            data.get("request_started_at_utc"), field="request_started_at_utc"
        )
        received = _parse_instant(
            data.get("response_received_at_utc"), field="response_received_at_utc"
        )
        if received < started:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "response_received_at_utc precedes request_started_at_utc",
            )
        return cls(
            request_id=_require_uuid(data.get("request_id"), field="request_id"),
            page=_require_int(data.get("page"), field="page", minimum=1),
            wire_start_value=_require_text(
                data.get("wire_start_value"), field="wire_start_value"
            ),
            wire_end_value=_require_text(
                data.get("wire_end_value"), field="wire_end_value"
            ),
            request_started_at_utc=started,
            response_received_at_utc=received,
            http_status=_require_int(
                data.get("http_status"), field="http_status", minimum=100
            ),
            row_count=_require_int(data.get("row_count"), field="row_count"),
        )


@dataclass(frozen=True)
class SubWindowRecord:
    """One tiling unit of the effective window, and every page it cost.

    `covers_from_ts`/`covers_to_ts` are the **half-open** slice of
    `[E_start, E_end)` this unit is responsible for; they are what the tiling
    check adds up. `requested_from_ts`/`requested_to_ts` are what was actually
    put on the wire, whose end is *inclusive* and therefore one boundary step
    short of `covers_to_ts` for every non-final unit. Conflating the two is
    exactly how a tiling check turns into a rounding argument, so both are kept.
    """

    index: int
    covers_from_ts: datetime
    covers_to_ts: datetime
    requested_from_ts: datetime
    requested_to_ts: datetime
    sub_window_label: Optional[str]
    status: str
    termination_reason: Optional[str]
    total_reconciliation: Optional[str]
    incomplete_reason: Optional[str]
    pages: Tuple[PageRequestRecord, ...]

    _FIELDS = (
        "index", "covers_from_ts", "covers_to_ts", "requested_from_ts",
        "requested_to_ts", "sub_window_label", "status", "termination_reason",
        "total_reconciliation", "incomplete_reason", "pages",
    )

    @property
    def complete(self) -> bool:
        return self.status == SUBWINDOW_COMPLETE

    @property
    def row_count(self) -> int:
        return sum(page.row_count for page in self.pages)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "covers_from_ts": _iso(self.covers_from_ts),
            "covers_to_ts": _iso(self.covers_to_ts),
            "requested_from_ts": _iso(self.requested_from_ts),
            "requested_to_ts": _iso(self.requested_to_ts),
            "sub_window_label": self.sub_window_label,
            "status": self.status,
            "termination_reason": self.termination_reason,
            "total_reconciliation": self.total_reconciliation,
            "incomplete_reason": self.incomplete_reason,
            "pages": [page.as_dict() for page in self.pages],
        }

    @classmethod
    def from_mapping(cls, payload: object) -> "SubWindowRecord":
        data = _exact_fields(payload, cls._FIELDS, what="a sub-window record")
        status = str(data.get("status") or "")
        if status not in SUBWINDOW_STATUSES:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                f"sub-window status {status!r} is not known",
            )
        raw_pages = data.get("pages")
        if not isinstance(raw_pages, (list, tuple)):
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED, "pages must be a JSON array"
            )
        pages = tuple(PageRequestRecord.from_mapping(item) for item in raw_pages)

        label_raw = data.get("sub_window_label")
        termination = data.get("termination_reason")
        reconciliation = data.get("total_reconciliation")
        incomplete_reason = data.get("incomplete_reason")

        if reconciliation is not None and \
                str(reconciliation) not in TOTAL_RECONCILIATION_STATES:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                f"total_reconciliation {reconciliation!r} is not known",
            )
        if incomplete_reason is not None and \
                str(incomplete_reason) not in INCOMPLETE_REASONS:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                f"incomplete_reason {incomplete_reason!r} is not known",
            )

        record = cls(
            index=_require_int(data.get("index"), field="index", minimum=1),
            covers_from_ts=_parse_instant(
                data.get("covers_from_ts"), field="covers_from_ts"
            ),
            covers_to_ts=_parse_instant(
                data.get("covers_to_ts"), field="covers_to_ts"
            ),
            requested_from_ts=_parse_instant(
                data.get("requested_from_ts"), field="requested_from_ts"
            ),
            requested_to_ts=_parse_instant(
                data.get("requested_to_ts"), field="requested_to_ts"
            ),
            sub_window_label=None if label_raw is None else str(label_raw),
            status=status,
            termination_reason=None if termination is None else str(termination),
            total_reconciliation=(
                None if reconciliation is None else str(reconciliation)
            ),
            incomplete_reason=(
                None if incomplete_reason is None else str(incomplete_reason)
            ),
            pages=pages,
        )
        record._check_internal_consistency()
        return record

    def _check_internal_consistency(self) -> None:
        """A unit that contradicts itself is refused here, not downstream."""
        if self.covers_to_ts <= self.covers_from_ts:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "covers_to_ts must be strictly after covers_from_ts; an empty "
                "tiling unit covers nothing and can prove nothing",
            )
        if self.requested_to_ts < self.requested_from_ts:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "requested_to_ts precedes requested_from_ts",
            )
        if not self.complete:
            # An INCOMPLETE unit is a refusal carrier: it must say why, and it
            # must not also claim a valid termination.
            if self.incomplete_reason is None:
                raise WindowCompletenessError(
                    WINDOW_EVIDENCE_MALFORMED,
                    "an INCOMPLETE sub-window must carry incomplete_reason",
                )
            if self.termination_reason is not None:
                raise WindowCompletenessError(
                    WINDOW_EVIDENCE_MALFORMED,
                    "an INCOMPLETE sub-window must not claim a valid termination",
                )
            return
        if self.incomplete_reason is not None:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "a COMPLETE sub-window must not carry incomplete_reason",
            )
        if self.termination_reason != TERMINATION_SHORT_PAGE:
            # A short page is the only authoritative terminal condition the
            # compatibility contract defines (`docs/12` §7). Anything else
            # claiming COMPLETE describes a termination this repository does not
            # implement, which is a forged or drifted record either way.
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                f"a COMPLETE sub-window terminated on "
                f"{self.termination_reason!r}, not {TERMINATION_SHORT_PAGE!r}",
            )
        if self.total_reconciliation not in TOTAL_RECONCILIATION_STATES:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "a COMPLETE sub-window must record its advisory-total "
                "reconciliation state (ABSENT is permitted, unknown is not)",
            )
        if not self.sub_window_label:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "a COMPLETE sub-window must name the provider sub-window it used",
            )
        if not self.pages:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "a COMPLETE sub-window must have fetched at least one page",
            )
        expected_pages = list(range(1, len(self.pages) + 1))
        if [page.page for page in self.pages] != expected_pages:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                "a COMPLETE sub-window must record pages 1..N in order",
            )


@dataclass(frozen=True)
class WindowCompleteness:
    """The child's complete statement about how it covered its effective window."""

    version: str
    endpoint: str
    effective_window_start_ts: datetime
    effective_window_end_ts: datetime
    subwindows: Tuple[SubWindowRecord, ...]

    _FIELDS = (
        "version", "endpoint", "effective_window_start_ts",
        "effective_window_end_ts", "subwindows",
    )

    @property
    def page_count(self) -> int:
        return sum(len(sub.pages) for sub in self.subwindows)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "endpoint": self.endpoint,
            "effective_window_start_ts": _iso(self.effective_window_start_ts),
            "effective_window_end_ts": _iso(self.effective_window_end_ts),
            "subwindows": [sub.as_dict() for sub in self.subwindows],
        }

    @classmethod
    def from_mapping(cls, payload: object) -> "WindowCompleteness":
        data = _exact_fields(
            payload, cls._FIELDS, what="the window completeness record"
        )
        version = str(data.get("version") or "")
        if version != WINDOW_COMPLETENESS_VERSION:
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED,
                f"window completeness version {version!r} is not "
                f"{WINDOW_COMPLETENESS_VERSION!r}",
            )
        raw_subwindows = data.get("subwindows")
        if not isinstance(raw_subwindows, (list, tuple)):
            raise WindowCompletenessError(
                WINDOW_EVIDENCE_MALFORMED, "subwindows must be a JSON array"
            )
        return cls(
            version=version,
            endpoint=_require_text(data.get("endpoint"), field="endpoint"),
            effective_window_start_ts=_parse_instant(
                data.get("effective_window_start_ts"),
                field="effective_window_start_ts",
            ),
            effective_window_end_ts=_parse_instant(
                data.get("effective_window_end_ts"),
                field="effective_window_end_ts",
            ),
            subwindows=tuple(
                SubWindowRecord.from_mapping(item) for item in raw_subwindows
            ),
        )


# ---------------------------------------------------------------------------
# The verification — §6 condition 6
# ---------------------------------------------------------------------------

def verify_window_completeness(
    completeness: Optional[WindowCompleteness],
    *,
    window_start_ts: datetime,
    window_end_ts: datetime,
    endpoint: str = TRIPS_ENDPOINT,
) -> WindowCompleteness:
    """Prove the effective window was exactly tiled and every unit completed.

    Raises `WindowCompletenessError` with a classification that names the real
    cause. Returns the carrier unchanged on success so the caller can project
    it; it never mutates, never reads a database and never decides anything
    about identity that the caller has not already established.

    The two checks are deliberately separate, because they catch different
    defects and a reader must be able to tell which one fired:

    * **tiling** — the union of the half-open `[covers_from, covers_to)`
      intervals is exactly `[window_start, window_end)`, with no gap, no
      overlap and no overhang. This is the failure class M3 cannot see;
    * **completeness** — every unit in that tiling reached a valid terminal
      state. This restates, durably, what the fetch contract already enforced.
    """
    if completeness is None:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_ABSENT,
            "the execution carried no window completeness evidence; coverage "
            "cannot advance on an unproven window",
        )

    expected_start = window_start_ts.astimezone(timezone.utc)
    expected_end = window_end_ts.astimezone(timezone.utc)

    if completeness.endpoint != endpoint:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_IDENTITY_MISMATCH,
            f"completeness evidence describes endpoint "
            f"{completeness.endpoint!r}, expected {endpoint!r}",
        )
    if completeness.effective_window_start_ts != expected_start:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_IDENTITY_MISMATCH,
            f"completeness evidence starts at "
            f"{_iso(completeness.effective_window_start_ts)}, expected "
            f"{_iso(expected_start)}",
        )
    if completeness.effective_window_end_ts != expected_end:
        raise WindowCompletenessError(
            WINDOW_EVIDENCE_IDENTITY_MISMATCH,
            f"completeness evidence ends at "
            f"{_iso(completeness.effective_window_end_ts)}, expected "
            f"{_iso(expected_end)}",
        )

    if expected_end <= expected_start:
        raise WindowCompletenessError(
            WINDOW_TILING_INCOMPLETE,
            "the effective window is empty; an empty window tiles nothing and "
            "proves nothing",
        )
    if not completeness.subwindows:
        raise WindowCompletenessError(
            WINDOW_TILING_INCOMPLETE,
            "no sub-window was attempted for a non-empty effective window",
        )

    ordered = sorted(completeness.subwindows, key=lambda sub: sub.covers_from_ts)

    # --- tiling: contiguous, gap-free, overlap-free, exact span --------------
    if ordered[0].covers_from_ts != expected_start:
        raise WindowCompletenessError(
            WINDOW_TILING_INCOMPLETE,
            f"the tiling starts at {_iso(ordered[0].covers_from_ts)}, but the "
            f"effective window starts at {_iso(expected_start)}",
        )
    cursor = expected_start
    for sub in ordered:
        if sub.covers_from_ts != cursor:
            if sub.covers_from_ts > cursor:
                raise WindowCompletenessError(
                    WINDOW_TILING_INCOMPLETE,
                    f"the tiling has a gap: nothing covers "
                    f"[{_iso(cursor)}, {_iso(sub.covers_from_ts)})",
                )
            raise WindowCompletenessError(
                WINDOW_TILING_INCOMPLETE,
                f"the tiling overlaps: sub-window {sub.index} starts at "
                f"{_iso(sub.covers_from_ts)}, before the previous unit ended "
                f"at {_iso(cursor)}",
            )
        cursor = sub.covers_to_ts
    if cursor != expected_end:
        if cursor < expected_end:
            raise WindowCompletenessError(
                WINDOW_TILING_INCOMPLETE,
                f"the tiling is short: it ends at {_iso(cursor)}, but the "
                f"effective window ends at {_iso(expected_end)}",
            )
        raise WindowCompletenessError(
            WINDOW_TILING_INCOMPLETE,
            f"the tiling overhangs: it ends at {_iso(cursor)}, past the "
            f"effective window end {_iso(expected_end)}",
        )

    # --- completeness: every unit in that exact tiling terminated validly ----
    incomplete = [sub for sub in ordered if not sub.complete]
    if incomplete:
        first = incomplete[0]
        raise WindowCompletenessError(
            SUBWINDOW_INCOMPLETE_REFUSAL,
            f"{len(incomplete)} of {len(ordered)} sub-window(s) did not reach a "
            f"valid complete terminal state; the first is index {first.index} "
            f"[{_iso(first.covers_from_ts)}, {_iso(first.covers_to_ts)}) "
            f"reason={first.incomplete_reason}",
        )
    return completeness


# ---------------------------------------------------------------------------
# Collector used by the business job and the provider client
# ---------------------------------------------------------------------------

class _OpenSubWindow:
    __slots__ = (
        "label", "requested_from", "requested_to", "wire_start", "wire_end",
        "pages", "terminated", "termination_reason", "total_reconciliation",
    )

    def __init__(
        self, *, label: str, requested_from: datetime, requested_to: datetime,
        wire_start: str, wire_end: str,
    ) -> None:
        self.label = label
        self.requested_from = requested_from
        self.requested_to = requested_to
        self.wire_start = wire_start
        self.wire_end = wire_end
        self.pages: List[PageRequestRecord] = []
        self.terminated = False
        self.termination_reason: Optional[str] = None
        self.total_reconciliation: Optional[str] = None


class _OpenTilingUnit:
    __slots__ = ("index", "covers_from", "covers_to", "requested_from",
                 "requested_to", "subwindows")

    def __init__(
        self, *, index: int, covers_from: datetime, covers_to: datetime,
        requested_from: datetime, requested_to: datetime,
    ) -> None:
        self.index = index
        self.covers_from = covers_from
        self.covers_to = covers_to
        self.requested_from = requested_from
        self.requested_to = requested_to
        self.subwindows: List[_OpenSubWindow] = []


class RequestEvidenceCollector:
    """Accumulates request evidence during one execution's `/trips` fetch.

    Two callers, deliberately: the **job** declares the tiling units it intends
    to cover (`begin_tiling_unit`), and the **provider client** reports what it
    actually did inside each one. Neither can produce a complete record alone,
    which is what makes a chunk-iteration defect visible: a unit the job never
    opened simply is not in the tiling, and the union check then fails.

    Enabled or not, it never raises into the fetch path. A collector defect must
    cost a refused fire, never a failed provider call, so every entry point is
    tolerant of being called out of order and records the anomaly as
    INCOMPLETE instead.
    """

    def __init__(
        self,
        *,
        endpoint: str = TRIPS_ENDPOINT,
        effective_window_start_ts: datetime,
        effective_window_end_ts: datetime,
    ) -> None:
        self.endpoint = endpoint
        self.effective_window_start_ts = effective_window_start_ts.astimezone(
            timezone.utc
        )
        self.effective_window_end_ts = effective_window_end_ts.astimezone(
            timezone.utc
        )
        self._units: List[_OpenTilingUnit] = []
        self._current: Optional[_OpenTilingUnit] = None
        #: provider_trip_id -> (request_id, response_received_at_utc) of the page
        #: that FIRST returned it. Never overwritten: a trip re-observed on a
        #: later page, a later sub-window or an overlapping tiling unit keeps its
        #: first sighting, which is the whole point of first-seen provenance.
        #:
        #: **The pair is stored as a pair on purpose (M-LAG).** The identity and
        #: the instant are two halves of one observation, and `client_trips`
        #: carries a CHECK that they are both present or both absent. Keeping
        #: them in one entry means there is no code path that can produce one
        #: without the other — a second dict keyed the same way could drift.
        self._first_seen: Dict[int, Tuple[str, datetime]] = {}

    # -- job side -----------------------------------------------------------

    def begin_tiling_unit(
        self,
        *,
        index: int,
        covers_from: datetime,
        covers_to: datetime,
        requested_from: datetime,
        requested_to: datetime,
    ) -> None:
        unit = _OpenTilingUnit(
            index=index,
            covers_from=covers_from.astimezone(timezone.utc),
            covers_to=covers_to.astimezone(timezone.utc),
            requested_from=requested_from.astimezone(timezone.utc),
            requested_to=requested_to.astimezone(timezone.utc),
        )
        self._units.append(unit)
        self._current = unit

    def end_tiling_unit(self) -> None:
        self._current = None

    # -- provider-client side ----------------------------------------------

    def begin_subwindow(
        self,
        *,
        label: str,
        requested_from: datetime,
        requested_to: datetime,
        wire_start: str,
        wire_end: str,
    ) -> None:
        if self._current is None:
            # A sub-window fetched outside any declared tiling unit cannot be
            # attributed, so it is dropped rather than guessed at. The unit it
            # should have belonged to is then missing from the tiling and the
            # union check refuses — which is the correct direction.
            return
        self._current.subwindows.append(
            _OpenSubWindow(
                label=label,
                requested_from=requested_from.astimezone(timezone.utc),
                requested_to=requested_to.astimezone(timezone.utc),
                wire_start=str(wire_start),
                wire_end=str(wire_end),
            )
        )

    def record_page(
        self,
        *,
        page: int,
        request_started_at_utc: datetime,
        response_received_at_utc: datetime,
        http_status: int,
        row_count: int,
    ) -> Optional[str]:
        """Record one page request and return its minted `request_id`."""
        open_sub = self._open_subwindow()
        if open_sub is None:
            return None
        request_id = str(uuid.uuid4())
        open_sub.pages.append(
            PageRequestRecord(
                request_id=request_id,
                page=int(page),
                wire_start_value=open_sub.wire_start,
                wire_end_value=open_sub.wire_end,
                request_started_at_utc=request_started_at_utc.astimezone(
                    timezone.utc
                ),
                response_received_at_utc=response_received_at_utc.astimezone(
                    timezone.utc
                ),
                http_status=int(http_status),
                row_count=int(row_count),
            )
        )
        return request_id

    def record_first_seen(self, *, request_id: Optional[str], identities) -> None:
        """Bind provider identities to the request that first returned them.

        The observation instant is resolved from the page record this
        `request_id` was minted on rather than taken as a parameter, so a caller
        cannot pass an identity and an instant that belong to different
        requests. A `request_id` this collector never recorded is ignored
        entirely: binding an identity to an unknown request would produce a
        first-seen reference the platform evidence table has no row for.
        """
        if not request_id:
            return
        received_at = self._response_received_at(request_id)
        if received_at is None:
            return
        for raw in identities:
            try:
                provider_trip_id = int(raw)
            except (TypeError, ValueError):
                continue
            self._first_seen.setdefault(
                provider_trip_id, (request_id, received_at)
            )

    def _response_received_at(self, request_id: str) -> Optional[datetime]:
        """The instant `request_id`'s response arrived, from the page record.

        Searched newest-first: `record_first_seen` is called immediately after
        `record_page`, so the match is the last page of the open sub-window on
        every real call and the walk terminates at once.
        """
        open_sub = self._open_subwindow()
        if open_sub is not None:
            for page in reversed(open_sub.pages):
                if page.request_id == request_id:
                    return page.response_received_at_utc
        for unit in reversed(self._units):
            for sub in reversed(unit.subwindows):
                for page in reversed(sub.pages):
                    if page.request_id == request_id:
                        return page.response_received_at_utc
        return None

    def complete_subwindow(
        self, *, termination_reason: str, total_reconciliation: str,
    ) -> None:
        open_sub = self._open_subwindow()
        if open_sub is None:
            return
        open_sub.terminated = True
        open_sub.termination_reason = termination_reason
        open_sub.total_reconciliation = total_reconciliation

    def _open_subwindow(self) -> Optional[_OpenSubWindow]:
        if self._current is None or not self._current.subwindows:
            return None
        return self._current.subwindows[-1]

    # -- results ------------------------------------------------------------

    def first_seen_request_ids(self) -> Dict[int, str]:
        """provider_trip_id -> the request that first returned it."""
        return {tid: pair[0] for tid, pair in self._first_seen.items()}

    def first_seen_observations(self) -> Dict[int, Tuple[str, datetime]]:
        """provider_trip_id -> (request_id, response_received_at_utc).

        The M-LAG accessor. Returns the pair so a caller writing a trip row
        cannot populate one half of the first-seen event and not the other; the
        client-side CHECK constraint refuses that shape anyway, so producing it
        would be a failed insert rather than a silent defect, but the pair is
        the honest interface.
        """
        return dict(self._first_seen)

    def build(self) -> WindowCompleteness:
        """Project what was observed into the immutable carrier.

        A tiling unit is COMPLETE only when it produced **exactly one** provider
        sub-window, that sub-window asked for exactly the interval the unit
        declared, it terminated validly, and it fetched at least one page. Every
        other shape is INCOMPLETE with a classified reason — including the shape
        a future `chunk_days > TRIPS_MAX_SUB_WINDOW_DAYS` would create, which
        this deliberately refuses rather than silently re-deriving a tiling the
        job did not declare.
        """
        records: List[SubWindowRecord] = []
        for unit in self._units:
            records.append(self._project_unit(unit))
        return WindowCompleteness(
            version=WINDOW_COMPLETENESS_VERSION,
            endpoint=self.endpoint,
            effective_window_start_ts=self.effective_window_start_ts,
            effective_window_end_ts=self.effective_window_end_ts,
            subwindows=tuple(records),
        )

    def _project_unit(self, unit: _OpenTilingUnit) -> SubWindowRecord:
        def incomplete(reason: str, *, sub: Optional[_OpenSubWindow]) -> SubWindowRecord:
            return SubWindowRecord(
                index=unit.index,
                covers_from_ts=unit.covers_from,
                covers_to_ts=unit.covers_to,
                requested_from_ts=unit.requested_from,
                requested_to_ts=unit.requested_to,
                sub_window_label=None if sub is None else sub.label,
                status=SUBWINDOW_INCOMPLETE,
                termination_reason=None,
                total_reconciliation=None,
                incomplete_reason=reason,
                pages=tuple(sub.pages) if sub is not None else (),
            )

        if not unit.subwindows:
            return incomplete(INCOMPLETE_NO_SUBWINDOW, sub=None)
        if len(unit.subwindows) != 1:
            return incomplete(INCOMPLETE_SPLIT_UNVERIFIABLE, sub=unit.subwindows[0])
        sub = unit.subwindows[0]
        if sub.requested_from != unit.requested_from or \
                sub.requested_to != unit.requested_to:
            return incomplete(INCOMPLETE_SPLIT_UNVERIFIABLE, sub=sub)
        if not sub.terminated:
            return incomplete(INCOMPLETE_NOT_TERMINATED, sub=sub)
        if not sub.pages:
            return incomplete(INCOMPLETE_NO_PAGES, sub=sub)
        return SubWindowRecord(
            index=unit.index,
            covers_from_ts=unit.covers_from,
            covers_to_ts=unit.covers_to,
            requested_from_ts=unit.requested_from,
            requested_to_ts=unit.requested_to,
            sub_window_label=sub.label,
            status=SUBWINDOW_COMPLETE,
            termination_reason=sub.termination_reason,
            total_reconciliation=sub.total_reconciliation,
            incomplete_reason=None,
            pages=tuple(sub.pages),
        )


# ---------------------------------------------------------------------------
# Durable request facts — the child side of the cross-database handoff
# ---------------------------------------------------------------------------

#: Lifecycle states of a `workflow_a_control.provider_request_log` row. Held as
#: literals here and in migration 061; `ops/tests_manual/…m4_provider_request_log_postgres.py`
#: pins that the two agree.
REQUEST_STATUS_PENDING = "PENDING"
REQUEST_STATUS_FINALIZED = "FINALIZED"


def persist_pending_request_facts(
    conn,
    *,
    platform_run_id: str,
    completeness: WindowCompleteness,
) -> int:
    """Durably record this execution's request facts, before the business commit.

    WHAT PROBLEM THIS SOLVES.
        `client_trips.first_seen_request_id` is immutable by design: once a trip
        row commits carrying request identity `R`, no later overlapping upsert
        may change it. That immutability is only safe if `R` is *resolvable*.
        The first M4 candidate wrote the platform evidence only at coverage
        finalization — after the business commit — so a platform failure in
        between left a committed, permanently immutable reference to a request
        that never became durable anywhere. Independent review reproduced it.

        Calling this before the business transaction commits closes that window
        by ordering alone, with no distributed transaction:

            request facts durable (PENDING)
              -> business commit (trips carry first_seen_request_id)
                -> verified promotion to FINALIZED, with the coverage CAS

        If the business transaction later rolls back, the rows written here are
        unreferenced and inert: no trip points at them, and no coverage read
        accepts a PENDING row. That is the harmless direction of the failure.

    WHAT THIS DELIBERATELY DOES NOT WRITE.
        No `run_history_id`, `client_id`, `schedule_id`, `dataset_name` and no
        completeness fields. A request fact is not attributed to a fire, and
        claims nothing about whether its sub-window finished. Those columns are
        the dispatcher's to write, bound from its own claim, and migration 061
        enforces their absence on a PENDING row with a CHECK constraint — so a
        request fact cannot masquerade as coverage evidence even if this writer
        were wrong.

    IDEMPOTENCY.
        `request_id` is the primary key and is minted once per page request, so
        a retry of this call within the same launch re-inserts the same rows and
        `ON CONFLICT DO NOTHING` makes that a no-op. It never updates an
        existing row, so it can neither promote nor demote one, and a row that
        some later fire has already finalized is left exactly as it is.

    The caller owns the transaction boundary: this issues one statement and does
    not commit, matching every other writer in this subsystem.
    """
    rows = [
        (
            page.request_id,
            REQUEST_STATUS_PENDING,
            str(platform_run_id),
            completeness.endpoint,
            completeness.effective_window_start_ts,
            completeness.effective_window_end_ts,
            sub.index,
            sub.sub_window_label,
            sub.covers_from_ts,
            sub.covers_to_ts,
            sub.requested_from_ts,
            sub.requested_to_ts,
            page.wire_start_value,
            page.wire_end_value,
            page.page,
            page.request_started_at_utc,
            page.response_received_at_utc,
            page.http_status,
            page.row_count,
        )
        for sub in completeness.subwindows
        for page in sub.pages
    ]
    if not rows:
        # A window whose every unit failed before its first page is legitimate —
        # the run is failing anyway — and a zero-row provider window still has
        # one page record per unit. Nothing to write is not an error here.
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO workflow_a_control.provider_request_log (
                request_id, status, platform_run_id,
                endpoint, effective_window_start_ts, effective_window_end_ts,
                sub_window_index, sub_window_label,
                covers_from_ts, covers_to_ts,
                requested_from_ts, requested_to_ts,
                wire_start_value, wire_end_value, page,
                request_started_at_utc, response_received_at_utc,
                http_status, row_count
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
            ON CONFLICT (request_id) DO NOTHING
            """,
            rows,
        )
    return len(rows)
