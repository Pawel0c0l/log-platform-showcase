"""Narrowly shared Telematics coverage-advancement CAS (C6 / C11).

Specification of record:
  ``docs/15_telematics_coverage_mutation_contract.md`` §4 (claim-time carrier and
    null-safe CAS), §5 (successful finalization, lock order, no-op), §7.1/§7.2
    (normative SQL shapes), §9 (error taxonomy)
  ``docs/13_telematics_trips_stabilization_windows.md`` §5.2/§5.4, §12
  ``docs/14_telematics_trips_compatibility_implementation_plan.md`` §3/C11, §12
  ``db/migrations/057_workflow_a_trips_coverage_state.sql`` (physical contract)
  ``db/migrations/058_telematics_trips_manual_recovery.sql`` (recovery identity and
    the extended ``covered_through_source`` vocabulary)
  ``db/migrations/062_workflow_a_multi_cadence_schedule_identity.sql`` (M5 — the
    coverage row is owned by ``(client_id, dataset_name)``)

**Coverage ownership since M5.** A coverage row is a fact about a *dataset*, not
about one schedule: ``(client_id, dataset_name)`` addresses it, and every cadence
over that dataset — base, weekly reconciliation, monthly reconciliation — shares
the one row, the one lock and the one compare-and-swap. ``schedule_id`` remains
on the row and remains one of the eleven CAS fields, but as the **immutable
originating-schedule provenance** written once by the bootstrap writer: nothing
in this module, in the dispatcher or in the recovery tool ever rewrites it, so it
does not track "last advancer" and must never be read as such.

That distinction is why the two concepts are kept in separate fields rather than
collapsed. A fire is identified by its schedule; the watermark it moves is
identified by its dataset. Before M5 those happened to coincide.

**This module is the single low-level coverage-advancement surface.** It holds
the one authorized ``covered_through_ts`` compare-and-swap and the one
authorized coverage row lock used by an advancement path. Exactly two reviewed
callers exist:

* ``jobs.api.telematics.dispatcher._finalize_compat_success`` — scheduled
  compatibility success (``covered_through_source = 'scheduled_run'``);
* ``ops.recover_telematics_trips_window`` — the reviewed manual compatibility
  recovery (``covered_through_source = 'manual_recovery'``).

Both bind their CAS parameters from an immutable claim-time snapshot taken
*before* the business work ran, never from a mutation-time reread
(``docs/15_…`` §4.3). This is one mutation contract with two entry points, not
a second independent mutation model: the predicate, the null-safety, the
monotonicity rule and the conflict classification are identical for both.

Deliberate scope boundaries, all load-bearing:

* it never writes ``coverage_start_ts``, ``bootstrap_status``,
  ``bootstrap_evidence_ref``, ``seeded_at``, ``seeded_by`` or
  ``last_gap_detected_ts``;
* it never inserts, deletes or upserts a coverage row;
* it never touches ``client_schedule_run_history``, a schedule, a client
  configuration or a client business database;
* it never commits or rolls back — the caller owns the transaction boundary
  explicitly (``docs/15_…`` §7 "Python performs an explicit ``commit()`` or
  ``rollback()``");
* it never retries and never re-reads the row to manufacture expected values;
* the gap transition (``READY → GAP_DETECTED``) stays where it is, in
  ``dispatcher._finalize_compat_gap``. This module owns advancement only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

# Kept as literals rather than imported from the pure C4/C5 window helper, so
# that helper keeps exactly its two authorized consumers and this module stays
# import-free of it. Drift between the two definitions is prevented statically
# by `ops/tests_manual/test_telematics_trips_recovery_workflow.py`.
COVERAGE_STATUS_READY = "READY"
TRIPS_SYNC_DATASET_NAME = "trips_sync"
TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1 = "data_invariants_v1"

# `covered_through_source` vocabulary. Migration 057 seeded the first three;
# migration 058 adds `manual_recovery` so a watermark moved by the reviewed
# manual recovery stays distinguishable from a scheduled advancement and from a
# raw operator edit. Reusing 'operator' here would destroy the operator-race
# signal that `docs/15_…` §4.3 depends on.
COVERAGE_SOURCE_BOOTSTRAP = "bootstrap"
COVERAGE_SOURCE_SCHEDULED_RUN = "scheduled_run"
COVERAGE_SOURCE_OPERATOR = "operator"
COVERAGE_SOURCE_MANUAL_RECOVERY = "manual_recovery"

COVERAGE_SOURCE_VOCABULARY = frozenset({
    COVERAGE_SOURCE_BOOTSTRAP,
    COVERAGE_SOURCE_SCHEDULED_RUN,
    COVERAGE_SOURCE_OPERATOR,
    COVERAGE_SOURCE_MANUAL_RECOVERY,
})

# Only these two provenance values may ever be *written* by an advancement.
# 'bootstrap' belongs to the one-shot insert writer and 'operator' describes a
# reviewed manual edit that this module deliberately cannot perform.
COVERAGE_ADVANCEMENT_SOURCES = frozenset({
    COVERAGE_SOURCE_SCHEDULED_RUN,
    COVERAGE_SOURCE_MANUAL_RECOVERY,
})

# Stable classifications (`docs/15_…` §9). Owned here so the scheduled and the
# manual path cannot drift apart; `dispatcher` re-exports them unchanged.
TRIPS_COVERAGE_ADVANCE_CONFLICT = "TRIPS_COVERAGE_ADVANCE_CONFLICT"
TRIPS_COVERAGE_POSTWRITE_VERIFICATION_FAILED = (
    "TRIPS_COVERAGE_POSTWRITE_VERIFICATION_FAILED"
)

# ---------------------------------------------------------------------------
# Canonical coverage fingerprint (one algorithm, one version, one owner)
# ---------------------------------------------------------------------------

COVERAGE_FINGERPRINT_VERSION = "telematics-coverage-fingerprint/1"

# Fixed ordered projection. `client_code` and `updated_at` are *included* here
# even though the CAS excludes them (`docs/15_…` §4.2): a fingerprint is
# evidence about the whole stored row, not the authorization predicate. The two
# roles are deliberately different and must not be conflated — a fingerprint
# never authorizes a mutation.
COVERAGE_FINGERPRINT_FIELDS: Sequence[str] = (
    "schedule_id",
    "client_id",
    "client_code",
    "dataset_name",
    "coverage_start_ts",
    "covered_through_ts",
    "bootstrap_status",
    "bootstrap_evidence_ref",
    "covered_through_source",
    "seeded_at",
    "seeded_by",
    "last_gap_detected_ts",
    "updated_at",
)

_TIMESTAMP_FIELDS = frozenset({
    "coverage_start_ts",
    "covered_through_ts",
    "seeded_at",
    "last_gap_detected_ts",
    "updated_at",
})

_IDENTITY_FIELDS = frozenset({"schedule_id", "client_id"})

# The eleven CAS fields of `docs/15_…` §4.1, in the documented order.
COVERAGE_CAS_FIELDS: Sequence[str] = (
    "schedule_id",
    "client_id",
    "dataset_name",
    "coverage_start_ts",
    "covered_through_ts",
    "bootstrap_status",
    "bootstrap_evidence_ref",
    "covered_through_source",
    "seeded_at",
    "seeded_by",
    "last_gap_detected_ts",
)


@dataclass(frozen=True)
class CoverageOwner:
    """The durable identity of one coverage row (M5).

    Introduced so that "which watermark" is a named concept rather than two
    parameters that happen to travel together, and so that no caller can reach
    for ``schedule_id`` when it means the coverage row. It is deliberately *not*
    a schedule: several schedules may share one owner.
    """

    client_id: str
    dataset_name: str

    def as_dict(self) -> Dict[str, Any]:
        return {"client_id": self.client_id, "dataset_name": self.dataset_name}


class CoverageCasConflict(RuntimeError):
    """Bounded advancement refusal. Carries identifiers only, never row content.

    Raised *without* rolling back: the caller owns the transaction and decides
    whether a compensating terminal write is permitted (``docs/15_…`` §7.6).
    """

    def __init__(
        self,
        code: str,
        *,
        detail: str,
        schedule_id: Optional[str] = None,
        client_id: Optional[str] = None,
        dataset_name: Optional[str] = None,
        rows_updated: Optional[int] = None,
    ) -> None:
        self.code = code
        self.detail = str(detail)[:300]
        # `schedule_id` is retained because it is still meaningful provenance and
        # existing operator tooling reads it. `client_id`/`dataset_name` name the
        # coverage row that was actually contended, which since M5 is the
        # identity that matters.
        self.schedule_id = str(schedule_id) if schedule_id is not None else None
        self.client_id = str(client_id) if client_id is not None else None
        self.dataset_name = dataset_name
        self.rows_updated = rows_updated
        super().__init__(f"{code}: {self.detail}")


def _utc(value: object) -> object:
    """Normalize representation only — never precision and never value."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc)


def _iso_or_none(value: object) -> Optional[str]:
    normalized = _utc(value)
    if normalized is None:
        return None
    if isinstance(normalized, datetime):
        return normalized.isoformat().replace("+00:00", "Z")
    return str(normalized)


def coverage_fingerprint(row: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 fingerprint of one coverage row.

    Algorithm (version ``telematics-coverage-fingerprint/1``):

    1. project exactly ``COVERAGE_FINGERPRINT_FIELDS``, in that fixed order;
    2. render every timestamp as an absolute UTC ISO-8601 instant with a ``Z``
       suffix, at the precision actually stored. Nothing is rounded or
       truncated: two valid renderings of the same instant fingerprint equal,
       a different instant does not;
    3. render identity columns as canonical text, other columns as their JSON
       value, and ``NULL`` as JSON ``null``;
    4. serialize as compact UTF-8 JSON with sorted keys;
    5. SHA-256, lowercase hex.

    The version string is part of the hashed payload, so a future algorithm
    change cannot silently compare equal to a stored version-1 fingerprint.
    """
    projection: Dict[str, Any] = {
        "fingerprint_version": COVERAGE_FINGERPRINT_VERSION,
    }
    for field in COVERAGE_FINGERPRINT_FIELDS:
        value = row.get(field)
        if field in _TIMESTAMP_FIELDS:
            projection[field] = _iso_or_none(value)
        elif field in _IDENTITY_FIELDS:
            projection[field] = None if value is None else str(value)
        else:
            projection[field] = None if value is None else str(value)
    canonical = json.dumps(
        projection,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Immutable claim-time snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageClaimSnapshot:
    """The eleven CAS values retained from before the business work started.

    A plain immutable carrier: no connection, no cursor, no raw row, no logger,
    no ``updated_at``, no secret and no trip payload (``docs/15_…`` §4).

    ``coverage_owner()`` is the M5 identity — the pair that addresses the row.
    ``schedule_id`` is the row's immutable originating schedule and is compared,
    never used to locate the row. For a reconciliation fire the two differ, and
    that is correct: the snapshot describes the *coverage row*, so it carries the
    row's own owning schedule and not the schedule that happens to be firing.
    """

    schedule_id: str
    client_id: str
    dataset_name: str
    coverage_start_ts: Optional[datetime]
    covered_through_ts: Optional[datetime]
    bootstrap_status: Optional[str]
    bootstrap_evidence_ref: Optional[str]
    covered_through_source: Optional[str]
    seeded_at: Optional[datetime]
    seeded_by: Optional[str]
    last_gap_detected_ts: Optional[datetime]

    @classmethod
    def from_state(cls, state: Any) -> "CoverageClaimSnapshot":
        """Build from any carrier exposing the eleven claim-time attributes.

        Duck-typed on purpose: it accepts the pure C4/C5 helper's
        ``CoverageState`` without importing that helper, which keeps its
        authorized consumer set at exactly two.
        """
        return cls(
            schedule_id=str(state.schedule_id),
            client_id=str(state.client_id),
            dataset_name=state.dataset_name,
            coverage_start_ts=state.coverage_start_ts,
            covered_through_ts=state.covered_through_ts,
            bootstrap_status=state.bootstrap_status,
            bootstrap_evidence_ref=state.bootstrap_evidence_ref,
            covered_through_source=state.covered_through_source,
            seeded_at=state.seeded_at,
            seeded_by=state.seeded_by,
            last_gap_detected_ts=state.last_gap_detected_ts,
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CoverageClaimSnapshot":
        return cls(
            schedule_id=str(row["schedule_id"]),
            client_id=str(row["client_id"]),
            dataset_name=row["dataset_name"],
            coverage_start_ts=row.get("coverage_start_ts"),
            covered_through_ts=row.get("covered_through_ts"),
            bootstrap_status=row.get("bootstrap_status"),
            bootstrap_evidence_ref=row.get("bootstrap_evidence_ref"),
            covered_through_source=row.get("covered_through_source"),
            seeded_at=row.get("seeded_at"),
            seeded_by=row.get("seeded_by"),
            last_gap_detected_ts=row.get("last_gap_detected_ts"),
        )

    def coverage_owner(self) -> "CoverageOwner":
        """The M5 identity of the coverage row this snapshot describes."""
        return CoverageOwner(
            client_id=str(self.client_id), dataset_name=self.dataset_name,
        )

    def cas_params(self) -> Dict[str, Any]:
        """Bind approved CAS values only from this retained object."""
        return {
            "claim_schedule_id": self.schedule_id,
            "claim_client_id": self.client_id,
            "claim_dataset_name": self.dataset_name,
            "claim_coverage_start_ts": _utc(self.coverage_start_ts),
            "claim_covered_through_ts": _utc(self.covered_through_ts),
            "claim_bootstrap_status": self.bootstrap_status,
            "claim_bootstrap_evidence_ref": self.bootstrap_evidence_ref,
            "claim_covered_through_source": self.covered_through_source,
            "claim_seeded_at": _utc(self.seeded_at),
            "claim_seeded_by": self.seeded_by,
            "claim_last_gap_detected_ts": _utc(self.last_gap_detected_ts),
        }


def snapshot_matches_row(
    row: Mapping[str, Any], snapshot: CoverageClaimSnapshot,
) -> bool:
    """Null-safe, timezone-normalized comparison of all eleven CAS fields."""
    for field in COVERAGE_CAS_FIELDS:
        expected = getattr(snapshot, field)
        observed = row.get(field)
        if field in _TIMESTAMP_FIELDS:
            if _utc(observed) != _utc(expected):
                return False
        elif field in _IDENTITY_FIELDS:
            if str(observed) != str(expected):
                return False
        elif observed != expected:
            return False
    return True


@dataclass(frozen=True)
class CoverageAdvanceResult:
    """Sanitized structured outcome. Identifiers and instants only."""

    moved: bool
    rows_updated: int
    schedule_id: str
    expected_old_covered_through_ts: Optional[str]
    new_covered_through_ts: Optional[str]
    covered_through_source: str
    mutation_ts: Optional[str]
    coverage_start_ts_unchanged: bool
    verified: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "moved": self.moved,
            "rows_updated": self.rows_updated,
            "schedule_id": self.schedule_id,
            "expected_old_covered_through_ts": (
                self.expected_old_covered_through_ts
            ),
            "new_covered_through_ts": self.new_covered_through_ts,
            "covered_through_source": self.covered_through_source,
            "mutation_ts": self.mutation_ts,
            "coverage_start_ts_unchanged": self.coverage_start_ts_unchanged,
            "verified": self.verified,
        }


# ---------------------------------------------------------------------------
# Coverage row reads
# ---------------------------------------------------------------------------

def lock_coverage_row_for_update(
    cur, *, client_id: str, dataset_name: str,
) -> list:
    """Lock the dataset's coverage row and return the current values.

    Step 2 of the ``docs/15_…`` §5.1 order: coverage is always locked before
    the history/recovery row, globally. The returned values are the *current*
    locked row; they are never copied into ``claim_*`` parameters and never
    treated as claim-time evidence (``docs/15_…`` §4.3 anti-pattern).

    **Addressed by ``(client_id, dataset_name)`` since M5.** The watermark is a
    fact about the dataset, not about one schedule, so every cadence over that
    dataset locks the same row here and therefore serializes against every
    other. That serialization is the whole point: it is what makes a shared
    watermark safe once M6 adds a second cadence.
    """
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id,
               client_id::text AS client_id, client_code, dataset_name,
               coverage_start_ts, covered_through_ts, bootstrap_status,
               bootstrap_evidence_ref, covered_through_source,
               seeded_at, seeded_by, last_gap_detected_ts, updated_at
          FROM workflow_a_control.client_dataset_coverage
         WHERE client_id = %(claim_client_id)s
           AND dataset_name = %(claim_dataset_name)s
         FOR UPDATE
        """,
        {"claim_client_id": client_id, "claim_dataset_name": dataset_name},
    )
    return [dict(row) for row in cur.fetchall()]


def read_coverage_row(
    cur, *, client_id: str, dataset_name: str,
) -> Optional[Dict[str, Any]]:
    """Plain unlocked read of one coverage row. Never used to build CAS values."""
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id,
               client_id::text AS client_id, client_code, dataset_name,
               coverage_start_ts, covered_through_ts, bootstrap_status,
               bootstrap_evidence_ref, covered_through_source,
               seeded_at, seeded_by, last_gap_detected_ts, updated_at
          FROM workflow_a_control.client_dataset_coverage
         WHERE client_id = %s
           AND dataset_name = %s
        """,
        (client_id, dataset_name),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if not rows:
        return None
    if len(rows) > 1:  # pragma: no cover - impossible under uq_..._coverage_dataset
        raise CoverageCasConflict(
            TRIPS_COVERAGE_ADVANCE_CONFLICT,
            detail="more than one coverage row exists for this dataset",
            client_id=client_id,
            dataset_name=dataset_name,
        )
    return rows[0]


# ---------------------------------------------------------------------------
# The single authorized coverage advancement
# ---------------------------------------------------------------------------

def advance_covered_through_cas(
    cur,
    *,
    snapshot: CoverageClaimSnapshot,
    candidate_covered_through_ts: datetime,
    source: str,
    mutation_ts: datetime,
    expected_bootstrap_status: str = COVERAGE_STATUS_READY,
    expected_trips_pagination_mode: str = TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    require_advance: bool = False,
) -> CoverageAdvanceResult:
    """Advance ``covered_through_ts`` under the approved compare-and-swap.

    Preconditions the caller must already have established: the coverage row is
    locked (``lock_coverage_row_for_update``), the locked row equals
    ``snapshot`` (``snapshot_matches_row``), the owning claim/recovery row is
    locked and still ``RUNNING``, and the business work committed successfully.

    Semantics:

    * ``W`` is monotone. ``candidate <= W`` issues **no** ``UPDATE`` at all and
      returns a validated no-op with every coverage column byte-unchanged
      (``docs/15_…`` §5.2, §7.3). ``require_advance=True`` turns that no-op into
      a refusal instead, which is what the manual recovery path wants: a
      recovery window that cannot move ``W`` is an operator error, not a
      silent success.
    * ``coverage_start_ts`` is never in the ``SET`` list, so ``A`` cannot move.
    * ``bootstrap_status`` is never in the ``SET`` list, so this function cannot
      create, clear or force a ``READY``/``GAP_DETECTED`` transition.
    * the predicate binds all eleven retained claim values null-safely plus the
      strict monotonicity guard ``covered_through_ts < new``. Anything other
      than exactly one affected row is ``TRIPS_COVERAGE_ADVANCE_CONFLICT``.
      **M5 changed which row this addresses, not what it compares.** The row is
      located by ``(client_id, dataset_name)``; ``schedule_id`` moved from being
      the addressing key to being one of the eleven compared claim values, so the
      safety fingerprint is exactly as wide as it was before.
    * after a successful ``UPDATE`` the row is read back inside the same
      transaction and every resulting value is verified.

    It never commits, never rolls back and never retries.
    """
    if source not in COVERAGE_ADVANCEMENT_SOURCES:
        raise ValueError(
            "covered_through_source must be one of "
            f"{sorted(COVERAGE_ADVANCEMENT_SOURCES)}; got {source!r}"
        )
    if expected_bootstrap_status != COVERAGE_STATUS_READY:
        raise ValueError(
            "coverage advancement requires an expected READY row; got "
            f"{expected_bootstrap_status!r}"
        )
    if snapshot.bootstrap_status != COVERAGE_STATUS_READY:
        raise ValueError(
            "the retained claim snapshot is not READY; advancement is refused "
            "before any statement is issued"
        )
    if expected_trips_pagination_mode != TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1:
        raise ValueError(
            "coverage advancement exists only for data_invariants_v1; got "
            f"{expected_trips_pagination_mode!r}"
        )
    if snapshot.dataset_name != TRIPS_SYNC_DATASET_NAME:
        raise ValueError(
            f"coverage advancement is {TRIPS_SYNC_DATASET_NAME}-only; got "
            f"{snapshot.dataset_name!r}"
        )
    if not isinstance(candidate_covered_through_ts, datetime) or (
        candidate_covered_through_ts.utcoffset() is None
    ):
        raise ValueError(
            "candidate_covered_through_ts must be a timezone-aware instant"
        )
    if not isinstance(mutation_ts, datetime) or mutation_ts.utcoffset() is None:
        raise ValueError("mutation_ts must be a timezone-aware instant")

    expected_old = _utc(snapshot.covered_through_ts)
    if expected_old is None:
        raise ValueError(
            "the retained claim snapshot has no covered_through_ts; there is "
            "no expected old W to compare against"
        )
    candidate = _utc(candidate_covered_through_ts)
    mutation = _utc(mutation_ts)

    # `schedule_id` is the coverage row's immutable originating schedule, carried
    # for provenance and still compared by the CAS predicate. Since M5 the row is
    # *addressed* by its owner, `(client_id, dataset_name)`.
    schedule_id = str(snapshot.schedule_id)
    client_id = str(snapshot.client_id)
    dataset_name = snapshot.dataset_name
    moved = bool(candidate > expected_old)

    if not moved:
        if require_advance:
            raise CoverageCasConflict(
                TRIPS_COVERAGE_ADVANCE_CONFLICT,
                detail=(
                    "the requested window end does not advance the watermark; "
                    "an explicitly authorized recovery must move W forward"
                ),
                schedule_id=schedule_id,
                client_id=client_id,
                dataset_name=dataset_name,
                rows_updated=0,
            )
        return CoverageAdvanceResult(
            moved=False,
            rows_updated=0,
            schedule_id=schedule_id,
            expected_old_covered_through_ts=_iso_or_none(expected_old),
            new_covered_through_ts=_iso_or_none(expected_old),
            covered_through_source=str(snapshot.covered_through_source),
            mutation_ts=None,
            coverage_start_ts_unchanged=True,
            verified=True,
        )

    params = {
        **snapshot.cas_params(),
        "new_covered_through_ts": candidate,
        "new_covered_through_source": source,
        "mutation_ts": mutation,
    }
    cur.execute(
        """
        UPDATE workflow_a_control.client_dataset_coverage
           SET covered_through_ts = %(new_covered_through_ts)s,
               covered_through_source = %(new_covered_through_source)s,
               updated_at = %(mutation_ts)s
         WHERE client_id = %(claim_client_id)s
           AND dataset_name = %(claim_dataset_name)s
           AND schedule_id = %(claim_schedule_id)s
           AND bootstrap_status = %(claim_bootstrap_status)s
           AND coverage_start_ts
               IS NOT DISTINCT FROM %(claim_coverage_start_ts)s
           AND covered_through_ts
               IS NOT DISTINCT FROM %(claim_covered_through_ts)s
           AND bootstrap_evidence_ref
               IS NOT DISTINCT FROM %(claim_bootstrap_evidence_ref)s
           AND covered_through_source = %(claim_covered_through_source)s
           AND seeded_at IS NOT DISTINCT FROM %(claim_seeded_at)s
           AND seeded_by IS NOT DISTINCT FROM %(claim_seeded_by)s
           AND last_gap_detected_ts
               IS NOT DISTINCT FROM %(claim_last_gap_detected_ts)s
           AND covered_through_ts < %(new_covered_through_ts)s
        """,
        params,
    )
    rows_updated = cur.rowcount
    if rows_updated != 1:
        raise CoverageCasConflict(
            TRIPS_COVERAGE_ADVANCE_CONFLICT,
            detail=(
                "the coverage compare-and-swap affected "
                f"{rows_updated} row(s); expected exactly one"
            ),
            schedule_id=schedule_id,
            client_id=client_id,
            dataset_name=dataset_name,
            rows_updated=rows_updated,
        )

    stored = read_coverage_row(
        cur, client_id=client_id, dataset_name=dataset_name,
    )
    mismatches = _verify_advanced_row(
        stored=stored,
        snapshot=snapshot,
        new_covered_through_ts=candidate,
        source=source,
        mutation_ts=mutation,
    )
    if mismatches:
        raise CoverageCasConflict(
            TRIPS_COVERAGE_POSTWRITE_VERIFICATION_FAILED,
            detail=(
                "the advanced coverage row does not match the approved "
                f"decision: {', '.join(sorted(mismatches))}"
            ),
            schedule_id=schedule_id,
            client_id=client_id,
            dataset_name=dataset_name,
            rows_updated=rows_updated,
        )

    return CoverageAdvanceResult(
        moved=True,
        rows_updated=rows_updated,
        schedule_id=schedule_id,
        expected_old_covered_through_ts=_iso_or_none(expected_old),
        new_covered_through_ts=_iso_or_none(candidate),
        covered_through_source=source,
        mutation_ts=_iso_or_none(mutation),
        coverage_start_ts_unchanged=True,
        verified=True,
    )


def _verify_advanced_row(
    *,
    stored: Optional[Mapping[str, Any]],
    snapshot: CoverageClaimSnapshot,
    new_covered_through_ts: object,
    source: str,
    mutation_ts: object,
) -> list:
    """Every value the transaction intended, and nothing else, must be stored."""
    if stored is None:
        return ["row_absent"]
    mismatches = []
    if str(stored.get("schedule_id")) != str(snapshot.schedule_id):
        mismatches.append("schedule_id")
    if str(stored.get("client_id")) != str(snapshot.client_id):
        mismatches.append("client_id")
    if stored.get("dataset_name") != snapshot.dataset_name:
        mismatches.append("dataset_name")
    # Advanced values.
    if _utc(stored.get("covered_through_ts")) != _utc(new_covered_through_ts):
        mismatches.append("covered_through_ts")
    if stored.get("covered_through_source") != source:
        mismatches.append("covered_through_source")
    if _utc(stored.get("updated_at")) != _utc(mutation_ts):
        mismatches.append("updated_at")
    # Protected values — byte-unchanged by contract.
    if _utc(stored.get("coverage_start_ts")) != _utc(snapshot.coverage_start_ts):
        mismatches.append("coverage_start_ts")
    if stored.get("bootstrap_status") != snapshot.bootstrap_status:
        mismatches.append("bootstrap_status")
    if stored.get("bootstrap_evidence_ref") != snapshot.bootstrap_evidence_ref:
        mismatches.append("bootstrap_evidence_ref")
    if _utc(stored.get("seeded_at")) != _utc(snapshot.seeded_at):
        mismatches.append("seeded_at")
    if stored.get("seeded_by") != snapshot.seeded_by:
        mismatches.append("seeded_by")
    if _utc(stored.get("last_gap_detected_ts")) != _utc(
        snapshot.last_gap_detected_ts
    ):
        mismatches.append("last_gap_detected_ts")
    return mismatches
