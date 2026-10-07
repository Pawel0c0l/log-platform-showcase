"""Per-input actionable signal for everything Workflow B discovered but could not finish.

Workflow B is an autonomous mailbox ingestion pipeline. Absence of mail is not a
failure; an input that *arrived* and did not reach a terminal state is. Before
this module the only evidence of the second case was the run-level outcome
`SUCCEEDED_WITH_REVIEW_ITEMS`, which production had been asserting on every
cycle since 2026-08-17 over a backlog of 59 unresolved files that grows by ~4
`report_112` files a week. The run stayed `SUCCESS`, the watchdog stayed `OK`,
and no incident of any kind was raised — so a newly stranded input produced no
materially new signal at all. That is exactly the saturated aggregate the
operating contract forbids:

    ARRIVED INPUT -> NOT FULLY PROCESSED -> NO ADEQUATE ACTIONABLE SIGNAL

The fix is granularity, not severity. The cycle keeps finishing as
`SUCCEEDED_WITH_REVIEW_ITEMS` — turning every review condition into a hard
failure would stop unrelated valid inputs from being processed — but each
unresolved input additionally gets its own durable `suspected_bug` incident.

Three properties make that safe to run twice a day forever:

* **one incident per (input, blocking reason)** — the 60th problem has a
  fingerprint no earlier problem has, so it is `REASON_NEW` and it is delivered
  even though 59 incidents are already open;
* **report once, not once per scan** — an input whose incident is already open
  is not re-reported, so the alert path never sees the same unchanged problem
  again and no reminder storm can build up from rescanning;
* **positive, stage-appropriate resolution only** — an incident closes when the
  platform can prove *the condition that incident names* is no longer
  unresolved. Proof is per stage: the ingest/clean/load chain is proved terminal
  by `stage3_status='OK'`, `DUPLICATE_CONTENT` or a row that no longer exists,
  and that same evidence proves **nothing** about work owed after the load, so
  it may not close a `postprocess` incident. A file merely missing from this
  cycle's window — a truncated sweep, a stage that never ran — keeps its
  incident open, and so does one whose evidence is ambiguous.

Two orderings the module has to get right, because both failure modes are
silent:

* **supersession is ordered.** When an input's blocking reason changes from A to
  B, the incident for A is closed only after B is durably represented by an open
  incident, re-read from the database rather than assumed. A replacement the
  per-cycle bound deferred, or one whose persistence failed, leaves A open: the
  state `unresolved input -> old incident closed -> replacement absent` must not
  exist even for one cycle.
* **the per-cycle bound is spent by incidents opened, not by attempts.** It
  exists to keep first activation against an existing backlog from enqueuing an
  unbounded burst into the alert outbox, which is a property of what actually
  persisted. A candidate whose persistence keeps failing therefore cannot
  monopolise the slot and starve the ones behind it. What the bound defers is
  logged explicitly and picked up by the next cycle; a silent cap would read as
  "everything is reported" when it is not.

REPLAYABLE VERSUS ONE-SHOT, AND WHY THE BOUND HAS TO KNOW THE DIFFERENCE.
    "Picked up by the next cycle" was never true of every class, and production
    proved it. On 2026-08-24 20:00 an ALPHA `Alpha_GPS_Baza_LOG` file arrived,
    cleaned, loaded 8 999 rows, and its postprocessor then returned
    `FAILED_NON_RETRYABLE / AMBIGUOUS_ENRICHMENT_MATCH` with
    `operator_action_required`. It was collected as the 64th unresolved input of
    that cycle, fell into the 44 the 20-slot bound deferred behind a 63-file
    Stage 2 backlog — and at 06:00 the next morning it was not in the population
    at all. `unresolved_input_count` went 64 -> 63 with `incidents_resolved: 0`.
    The input had simply left, and only a domain-specific
    `ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT` incident kept it visible. A generic
    safety contract may not depend on every postprocessor happening to alert for
    itself:

        ARRIVED -> STAGE 3 OK -> POSTPROCESS FAILED -> DEFERRED
                -> NEVER REDISCOVERED -> NO DURABLE ACTIONABLE SIGNAL

    So each collected item declares whether the platform can *prove* it will be
    offered again. The first candidate derived that from the producing stage
    alone, and review disproved the Stage 2 half of the table: a stage label is
    not a rediscovery guarantee. The invariant is per event:

        an unresolved event may take the bounded replayable deferral path only
        when the mechanism that would re-offer it is proved, from this cycle's
        own evidence, to reach that exact event again on a later natural cycle.

    Anything else — including anything a future collection source adds — is
    one-shot. Misclassifying a replayable item as one-shot costs at most an
    earlier incident that dedup then keeps quiet; the opposite mistake is the
    permanent signal loss this whole module exists to prevent.

    What each source can actually prove, and nothing more:

    * `stage2` — **one-shot, whatever produced the item.** The second candidate
      claimed the unrouted reconciliation sweep was a rediscovery guarantee for
      the items *it* returned: the sweep pages `stage2_unrouted_files` in
      `(COALESCE(stage2_updated_at,'epoch'), id)` ascending order, so a swept
      item sits at an ordinal position inside the
      `UNROUTED_SWEEP_MAX_PAGES * UNROUTED_SWEEP_LIMIT` prefix, and that
      position was argued to be monotonically non-increasing. Review disproved
      the premise, and the repository — not a hypothetical — is what disproves
      it:

          `_candidate_rows(params={"raw_file_ids": [...]})` drops the
          eligibility predicate entirely and admits *any* `NORMALIZED` row by
          id. Processing it calls `_persist_stage2`, which writes
          `stage2_updated_at = NOW()` unconditionally. If the new durable state
          is still materially unresolved and still non-retryable — the ordinary
          outcome of re-running detection on a file that is unroutable for a
          content reason — the row stays in the sweep's qualifying set but now
          carries the newest key in it, so it sorts *last*. Behind a persistent
          unresolved prefix at or beyond the 10 000-row page-count backstop the
          sweep stops before reaching it, on that cycle and on every later one,
          while `_candidate_rows` normal discovery still refuses it as
          non-retryable.

      `ops/renormalize_raw_file.py --apply --reset-stage2` is a second path out
      of the swept prefix: it clears the Stage 2 state and ordering fields, and
      guarantees nothing about exhaustive later discovery of that specific row.

      A position argument is only as strong as the set of writers that can
      change the position, and Stage 2 ordering is writable by supported
      operator paths that carry no obligation to preserve it. So the proof is
      not repairable by refining the provenance test, and refining it is exactly
      what produced two invalid candidates. Stage 2 is one-shot: no sweep-origin
      versus current-cycle distinction, no per-outcome exception. The cost is an
      incident opened in the observing cycle that dedup then keeps quiet; the
      alternative cost is the permanent disappearance this module exists to
      prevent.
    * `stage3` — **replayable when the durable status the item leaves behind is
      one `_stage3_batch_status_eligible_sql` (P0-E) re-admits**: NULL, empty,
      `RUNNING` past its grace, or `ERROR`. This is the one class whose proof
      does *not* rest on an ordinal position, which is why the Stage 2 refutation
      above does not touch it: the orchestrator passes no `limit` to
      `process_stage3_batch`, so `_select_stage3_candidates` emits no `LIMIT`
      clause and every eligible row is discovered on every cycle. Membership of
      the eligible set is the whole predicate, it is a function of the row's own
      durable `stage3_status`, and nothing about a backlog, a page count or a
      sort key can displace a member of it. A durable status outside that set is
      one nothing re-admits, and is treated as one-shot.
    * `postprocess` — **one-shot.** `_discover_postprocessor_plans` derives work
      solely from the Stage 3 identities loaded *in this cycle*; once
      `stage3_status='OK'` the file is never offered to a postprocessor again
      and nothing durable records that the work is still owed.
    * `stage1` — **one-shot.** A collected Stage 1 item always names a raw file,
      which means its `ingest.imap_message` row is already committed and every
      later cycle skips the message as `REUSED_MESSAGE`. The only path that
      re-derives such an item is the artifact reconciliation batch, and that is
      `limit`-bounded rather than an exhaustive sweep, so it is not a
      rediscovery guarantee.

    A one-shot event is therefore never merely deferred. The per-cycle bound the
    backlog contends for governs replayable candidates only; one-shot candidates
    get their own separate, explicit budget, and whatever is still not durably
    open when the pass ends — deferred by that budget *or* failed to persist —
    is named in one bounded `WORKFLOW_B_UNRESOLVED_SIGNAL_TRUNCATED` incident
    before the evidence disappears. Determined from the same post-write re-read
    of the incident table that supersession uses: proof, not intent.

FAIL-CLOSED, AND ONLY HERE.
    The truncation incident is itself a write, and a write can fail. If the
    individual incident could not be persisted *and* the aggregate that stands
    in for it could not be persisted either, then a material one-shot condition
    was observed and left no durable actionable representation anywhere — and
    its evidence is gone with the cycle. That, and nothing wider, is the state
    the cycle must not settle on: `signal.safety_contract_violated` is raised
    only for one-shot inputs that end the pass with neither an individual nor an
    aggregate durable incident, both established by re-reading
    `suspected_bug_incidents` rather than by trusting a returned
    `report.error is None`. The orchestrator turns that one flag into an
    ordinary terminal Workflow B failure, which the existing
    `JOB_TERMINAL_FAILURE` / run-FAILED / systemd `OnFailure` path already makes
    independently actionable. Every other reporting fault stays contained: a
    failed replayable incident, a domain review item, an unreachable alert path
    on a cycle with no one-shot inputs — none of those may fail the run.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from api.suspected_bug import SuspectedBugEvent, load_alert_config, safe_report_suspected_bug


INCIDENT_CODE = "WORKFLOW_B_INPUT_UNRESOLVED"
#: Raised when this cycle could not give every one-shot unresolved input its own
#: incident. It exists so that a resource bound can never become permanent
#: silence: the evidence for a one-shot input is gone after this cycle, so the
#: fact that it was not represented has to become durable in the same cycle.
TRUNCATED_INCIDENT_CODE = "WORKFLOW_B_UNRESOLVED_SIGNAL_TRUNCATED"
COMPONENT = "workflow_b.orchestrator"
WORKFLOW_NAME = "workflow_b"
SUBJECT_TYPE = "workflow_b_input"
SUBJECT_KEY = "ingest.raw_file.id"
TRUNCATED_SUBJECT_TYPE = "workflow_b_unresolved_signal"
TRUNCATED_SUBJECT_KEY = "workflow_b.unresolved_signal.unreported_one_shot"

#: How many *new* incidents one cycle may open for **replayable** unresolved
#: inputs — after the Stage 2 refutation, the Stage 3 class alone. Only ever
#: reached while draining a pre-existing backlog: in steady state a cycle opens
#: one incident per newly unresolved input, which is a handful at most. What
#: this bound defers is genuinely deferred — the next cycle rediscovers the same
#: durable rows, unbounded and by durable status rather than by position, and
#: picks up where this one stopped.
DEFAULT_MAX_NEW_INCIDENTS_PER_CYCLE = 20
MAX_NEW_INCIDENTS_ENV = "WORKFLOW_B_MAX_NEW_UNRESOLVED_INCIDENTS_PER_CYCLE"

#: The separate budget for **one-shot** unresolved inputs, which no later cycle
#: is proved to offer again. It is a resource backstop, never a pacing
#: mechanism, and the difference matters because Stage 2 is now one-shot: the
#: durable unrouted backlog *is* re-observed on most cycles even though nothing
#: guarantees it, so this budget does meet a standing population rather than
#: only one cycle's fresh work.
#:
#: That is bounded on both sides and neither side is silent.
#:
#: * **First activation against the standing backlog.** Production's unrouted
#:   Stage 2 set is on the order of 60 files growing by ~4 a week, so the first
#:   cycle opens ~60 incidents in one pass rather than draining 20 a cycle over
#:   three days. Each is one `REASON_NEW` outbox row; the delivery worker runs
#:   every 5 minutes at `SUSPECTED_BUG_EMAIL_WORKER_BATCH_SIZE` (10), i.e. 120
#:   an hour, so a burst of this size is spread over well under an hour by the
#:   outbox itself and never becomes a synchronous send storm.
#: * **Every later cycle.** The same files match an already-open incident by
#:   fingerprint, are removed from `pending` before any budget is consulted, and
#:   produce no new outbox message at all. The standing backlog therefore costs
#:   its notifications exactly once, not once per cycle — the dedup is what
#:   makes a re-observed one-shot population quiet, not the bound.
#: * **A backlog above this number.** Whatever the bound refuses is still made
#:   durable, in aggregate and with an exact count and complete-set digest, by
#:   `TRUNCATED_INCIDENT_CODE`; if even that cannot be persisted the cycle
#:   fail-closes. The remainder is then individually represented over following
#:   cycles as the already-open ones stop consuming capacity. No path loses it.
DEFAULT_MAX_NEW_ONE_SHOT_INCIDENTS_PER_CYCLE = 100
MAX_NEW_ONE_SHOT_INCIDENTS_ENV = "WORKFLOW_B_MAX_NEW_ONE_SHOT_UNRESOLVED_INCIDENTS_PER_CYCLE"

#: Durable `ingest.raw_file.stage3_status` values that
#: `_stage3_batch_status_eligible_sql` (P0-E) re-admits to autonomous Stage 3
#: discovery. An item whose durable status is outside this set — or unknown —
#: has no proof of rediscovery and is treated as one-shot. `None` counts and is
#: handled by the caller: a Stage 3 failure raised before the row was stamped
#: leaves `stage3_status` NULL, which discovery re-admits.
STAGE3_REDISCOVERED_STATUSES = frozenset({"", "ERROR", "RUNNING"})

#: How many unreported one-shot identities the truncation incident *displays*.
#: It bounds the human-readable payload only. The durable identity of that
#: incident covers the complete set through `one_shot_membership_digest`, and
#: the count is always exact.
MAX_LISTED_UNREPORTED_ONE_SHOT = 50

#: Namespace prefix of the complete-set digest. Explicit so the identity of a
#: truncation incident cannot silently change meaning if the tuple shape ever
#: grows a field.
ONE_SHOT_DIGEST_NAMESPACE = b"workflow_b.unresolved_signal.one_shot.v1"

#: Stage-1 outcomes whose item names an input that arrived and stopped.
#: `FAILED_RETRYABLE_*` is deliberately absent: the next cycle retries it, and a
#: retryable failure that persists already fails the run on every cycle, which
#: the terminal-failure incident path owns.
_STAGE1_UNRESOLVED_OUTCOMES = frozenset({
    "BLOCKED_OPERATOR_ACTION",
    "FAILED_NON_RETRYABLE_VALIDATION",
    "FAILED_NON_RETRYABLE_CONFIGURATION",
})

_STAGE2_UNRESOLVED_OUTCOMES = frozenset({
    "STRANDED_UNROUTABLE",
    "STRANDED_AWAITING_REVIEW",
    "PENDING_HUMAN_REVIEW",
    "UNSUPPORTED_REPORT",
    "AMBIGUOUS_DETECTION",
    "REJECTED_VALIDATION",
    "FAILED_NON_RETRYABLE",
    "FAILED_IDEMPOTENCY_CONFLICT",
})

_STAGE3_UNRESOLVED_OUTCOMES = frozenset({
    "BLOCKED_OPERATOR_ACTION",
    "BLOCKED_RECOVERY_OPERATOR",
    "REJECTED_VALIDATION",
    "FAILED_NON_RETRYABLE_CONFIGURATION",
    "FAILED_ENVIRONMENT_IDENTITY",
    "FAILED_PERMISSION",
    "FAILED_SCHEMA_NOT_READY",
    "FAILED_WRITER_VALIDATION",
})

_POSTPROCESSOR_UNRESOLVED_OUTCOMES = frozenset({
    "BLOCKED_OPERATOR_ACTION",
    "BLOCKED_UNSUPPORTED_CONFIGURATION",
    "FAILED_NON_RETRYABLE",
    "FAILED_ENVIRONMENT_IDENTITY",
    "FAILED_PERMISSION",
    "FAILED_SCHEMA_NOT_READY",
})

#: Postprocessor outcomes that are durable evidence the work owed for an input
#: actually happened. `SKIPPED_ALREADY_COMPLETED` counts: the writer itself
#: reports there is nothing left to do for that source.
_POSTPROCESSOR_COMPLETED_OUTCOMES = frozenset({
    "SUCCEEDED",
    "SKIPPED_ALREADY_COMPLETED",
})

#: Stages whose incident a completed ingest/clean/load chain actually resolves.
#: `postprocess` is absent on purpose — see `resolve_recovered_incidents`.
_LOAD_PROVABLE_STAGES = frozenset({"stage1", "stage2", "stage3"})

#: How many individual reporting failures one cycle writes a line for. The
#: aggregate count is always reported; this only stops a systemic alert-path
#: outage from writing one log row per pending input.
MAX_LOGGED_REPORT_FAILURES = 5

#: What an operator is being asked to do, per blocking reason. Absent reasons
#: fall back to the generic line; nothing here is invented per incident.
_SUGGESTED_ACTIONS: dict[str, str] = {
    "missing_client_code": (
        "Stage 2 accepted the file but produced no client_code, so Stage 3 routing can never "
        "consume it. Decide which client the report belongs to and either extend detection for "
        "this report shape or record the file as intentionally unsupported."
    ),
    "missing_stage2_report_type": (
        "Stage 2 accepted the file but produced no report type. Extend detection for this shape "
        "or record the file as intentionally unsupported."
    ),
    "missing_stage2_cleaned_artifact": (
        "Stage 2 reports success but no cleaned artifact is linked, so Stage 3 has nothing to "
        "load. Reconcile the artifact link before reprocessing."
    ),
    "cleaning_not_implemented": (
        "The report type was recognised but no cleaning implementation exists for it. Implement "
        "the Stage 2 type or record it as intentionally unsupported."
    ),
    "low_detection_confidence": (
        "Detection could not identify the report with enough confidence. Confirm the report type "
        "by hand, then reprocess the file explicitly."
    ),
    "missing_report_policy": (
        "No workflow_b_control.report_type_client_load_policy row exists for this "
        "(client, report type). Configure the policy; the file stays eligible for an automatic "
        "retry and no business data was written."
    ),
}

_GENERIC_SUGGESTED_ACTION = (
    "This input entered Workflow B and did not reach a terminal state. Inspect the raw file and "
    "either complete processing or record it as intentionally unsupported."
)


@dataclass(frozen=True, slots=True)
class UnresolvedInput:
    """One arrived input that this cycle could not carry to a terminal state."""

    raw_file_id: str
    stage: str
    outcome: str
    reason_code: str
    client_code: str | None = None
    report_type: str | None = None
    detail: str | None = None
    #: Can the platform *prove*, from this cycle's own evidence, that a later
    #: natural cycle re-offers this exact event? Carried per event rather than
    #: derived from `stage`, because review disproved that a stage label is a
    #: rediscovery guarantee: two Stage 2 items with the same stage, the same
    #: reason and opposite provenance have opposite answers. It defaults to
    #: `False` so a collection source that forgets to decide is fail-safe, and
    #: `collect_unresolved_inputs` is the only place that ever sets it True —
    #: each time against a mechanism named in the module docstring.
    replayable: bool = False

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.raw_file_id, self.stage, self.reason_code)


@dataclass(slots=True)
class UnresolvedInputSignalResult:
    """What the signalling pass actually did. Serialized into the terminal payload."""

    unresolved_input_count: int = 0
    already_open_count: int = 0
    incidents_opened: int = 0
    incidents_deferred: int = 0
    incidents_resolved: int = 0
    report_failures: int = 0
    incidents_attempted: int = 0
    max_new_incidents_per_cycle: int = DEFAULT_MAX_NEW_INCIDENTS_PER_CYCLE
    skipped: str | None = None

    #: The one-shot half, reported separately because its bound means something
    #: different: `incidents_deferred` really is deferred, `one_shot_unreported`
    #: never comes back and is why the truncation incident exists.
    one_shot_input_count: int = 0
    one_shot_incidents_opened: int = 0
    one_shot_unreported: int = 0
    truncation_incident_reported: bool = False
    max_new_one_shot_incidents_per_cycle: int = DEFAULT_MAX_NEW_ONE_SHOT_INCIDENTS_PER_CYCLE

    #: Proved by re-reading `suspected_bug_incidents`, not by the return value
    #: of the write: an open incident whose fingerprint is exactly the one this
    #: cycle's truncation event carries. `truncation_incident_reported` says
    #: what the call claimed; this says what the database holds.
    truncation_incident_durable: bool = False
    #: One-shot inputs that ended the pass with **neither** an individual
    #: durable incident **nor** durable aggregate coverage. Non-zero is the one
    #: state the cycle may not settle on.
    one_shot_without_durable_evidence: int = 0

    @property
    def safety_contract_violated(self) -> bool:
        """A material one-shot condition was observed and left no durable trace.

        Narrow on purpose. An ordinary reporting failure, a deferred replayable
        candidate, a domain review item and an alert path that is down on a
        cycle with no one-shot inputs are all false here — none of them loses
        evidence. Only the state

            ONE-SHOT UNRESOLVED + no individual incident + no aggregate incident

        is true, and only that may not end as a successful or review cycle.
        """
        return self.one_shot_without_durable_evidence > 0

    def to_dict(self) -> dict[str, Any]:
        value = {
            "unresolved_input_count": self.unresolved_input_count,
            "already_open_count": self.already_open_count,
            "incidents_opened": self.incidents_opened,
            "incidents_attempted": self.incidents_attempted,
            "incidents_deferred": self.incidents_deferred,
            "incidents_resolved": self.incidents_resolved,
            "report_failures": self.report_failures,
            "max_new_incidents_per_cycle": self.max_new_incidents_per_cycle,
            "one_shot_input_count": self.one_shot_input_count,
            "one_shot_incidents_opened": self.one_shot_incidents_opened,
            "one_shot_unreported": self.one_shot_unreported,
            "truncation_incident_reported": self.truncation_incident_reported,
            "truncation_incident_durable": self.truncation_incident_durable,
            "one_shot_without_durable_evidence": self.one_shot_without_durable_evidence,
            "safety_contract_violated": self.safety_contract_violated,
            "max_new_one_shot_incidents_per_cycle": self.max_new_one_shot_incidents_per_cycle,
        }
        if self.skipped:
            value["skipped"] = self.skipped
        return value


# --------------------------------------------------------------------- collect


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _outcome_value(item: Any) -> str:
    outcome = getattr(item, "outcome", None)
    return str(getattr(outcome, "value", outcome) or "")


def _stage_items(stage: Any) -> Sequence[Any]:
    result = getattr(stage, "result", None)
    items = getattr(result, "items", None)
    return items if isinstance(items, (list, tuple)) else ()


def stage3_item_is_replayable(item: Any) -> bool:
    """Does this item leave a durable status Stage 3 discovery re-admits?

    P0-E's `_stage3_batch_status_eligible_sql` re-admits NULL, empty, `RUNNING`
    past its grace and `ERROR`; a blocked attempt that never stamped the row
    leaves NULL, which is the `persisted_status is None` case.

    Unlike the Stage 2 sweep this does not depend on where the row sorts.
    Autonomous Stage 3 discovery runs with no `limit`, so the query carries no
    `LIMIT` and returns the *entire* eligible set; `ORDER BY stage2_updated_at
    ASC, id ASC` only decides processing order within a set every member of
    which is already selected. A mutation path that re-stamps `stage2_updated_at`
    — `_persist_stage2`, a renormalize/reset — therefore moves the row inside an
    unbounded set rather than out of a bounded prefix, which is exactly the
    property Stage 2 lacks.

    Any other durable status — including one a future Stage 3 introduces — is a
    state nothing re-admits, so it is one-shot. Reading the item's own
    `persisted_status` rather than the stage name is what makes that fail-safe
    instead of assumed.
    """
    persisted = getattr(item, "persisted_status", None)
    if persisted is None:
        return True
    return str(persisted).strip().upper() in STAGE3_REDISCOVERED_STATUSES


def collect_unresolved_inputs(result: Any) -> list[UnresolvedInput]:
    """Project one cycle's stage items onto the inputs that still need a human.

    Pure and total: it reads only the in-memory batch result, so the same cycle
    that produced the state also names it, and a test can build any state
    without a database.

    An item qualifies when it *both* names a `raw_file_id` — an input with no
    raw file is a cycle-level failure, which the terminal-failure incident path
    already owns — and reports a durable non-retryable blocked state. Retryable
    failures are excluded on purpose: the next cycle retries them by itself, and
    one that never clears fails the run every time, which alerts on its own.

    This is also the **only** place `replayable` is decided, per event and
    against a named mechanism. Every construction below states it explicitly,
    including the ones that state `False`, so adding a source without answering
    the question is not possible by omission: the field defaults to one-shot,
    which is the fail-safe direction.
    """
    collected: list[UnresolvedInput] = []
    seen: set[tuple[str, str, str]] = set()

    def add(candidate: UnresolvedInput) -> None:
        if candidate.identity in seen:
            return
        seen.add(candidate.identity)
        collected.append(candidate)

    for item in _stage_items(getattr(result, "stage1", None)):
        raw_file_id = _text(getattr(item, "raw_file_id", None))
        outcome = _outcome_value(item)
        if not raw_file_id:
            continue
        if not (getattr(item, "operator_action_required", False) or outcome in _STAGE1_UNRESOLVED_OUTCOMES):
            continue
        add(UnresolvedInput(
            raw_file_id=raw_file_id,
            stage="stage1",
            outcome=outcome,
            reason_code=_text(getattr(item, "error_category", None)) or outcome.lower(),
            client_code=_text(getattr(item, "client_code", None)),
            report_type=_text(getattr(item, "report_type", None)),
            detail=_text(getattr(item, "exception_message", None)),
            # One-shot: the `ingest.imap_message` row is already committed, so
            # every later cycle skips the message as `REUSED_MESSAGE`.
            replayable=False,
        ))

    for item in _stage_items(getattr(result, "stage2", None)):
        raw_file_id = _text(getattr(item, "raw_file_id", None))
        outcome = _outcome_value(item)
        if not raw_file_id:
            continue
        if not (getattr(item, "review_required", False) or outcome in _STAGE2_UNRESOLVED_OUTCOMES):
            continue
        add(UnresolvedInput(
            raw_file_id=raw_file_id,
            stage="stage2",
            outcome=outcome,
            reason_code=(
                _text(getattr(item, "reason_code", None))
                or _text(getattr(item, "error_category", None))
                or outcome.lower()
            ),
            client_code=_text(getattr(item, "client_code", None)),
            report_type=_text(getattr(item, "report_type", None)),
            detail=_text(getattr(item, "persisted_status", None)),
            # One-shot: no Stage 2 rediscovery mechanism survives the
            # repository's own targeted mutation paths. See the module
            # docstring.
            replayable=False,
        ))

    for item in _stage_items(getattr(result, "stage3", None)):
        raw_file_id = _text(getattr(item, "raw_file_id", None))
        outcome = _outcome_value(item)
        if not raw_file_id or getattr(item, "retryable", False):
            continue
        if not (getattr(item, "operator_action_required", False) or outcome in _STAGE3_UNRESOLVED_OUTCOMES):
            continue
        add(UnresolvedInput(
            raw_file_id=raw_file_id,
            stage="stage3",
            outcome=outcome,
            reason_code=_text(getattr(item, "error_category", None)) or outcome.lower(),
            client_code=_text(getattr(item, "client_code", None)),
            report_type=_text(getattr(item, "report_type", None)),
            detail=_text(getattr(item, "error_detail", None)),
            replayable=stage3_item_is_replayable(item),
        ))

    for item in getattr(result, "postprocessors", None) or ():
        raw_file_id = _text(getattr(item, "raw_file_id", None))
        outcome = _outcome_value(item)
        if not raw_file_id or getattr(item, "retryable", False):
            continue
        if not (getattr(item, "operator_action_required", False) or outcome in _POSTPROCESSOR_UNRESOLVED_OUTCOMES):
            continue
        add(UnresolvedInput(
            raw_file_id=raw_file_id,
            stage="postprocess",
            outcome=outcome,
            reason_code=_text(getattr(item, "error_category", None)) or outcome.lower(),
            client_code=_text(getattr(item, "client_code", None)),
            report_type=_text(getattr(item, "report_type", None)),
            detail=_text(getattr(item, "error_detail", None)),
            # One-shot: plans come only from the Stage 3 identities this cycle
            # loaded, and `stage3_status='OK'` retires the file from that set.
            replayable=False,
        ))

    return collected


# ------------------------------------------------------------------- incidents


def build_event(item: UnresolvedInput, *, run_id: str | None, environment: str,
                occurred_at: datetime) -> SuspectedBugEvent:
    """The incident for one blocked input.

    `fingerprint_fields` carries the raw file, the stage and the blocking reason,
    and nothing else. That is deliberate on both sides:

    * the raw file id is in it, so two different stranded files are two
      incidents and the newer one is `REASON_NEW` rather than another occurrence
      of the older one;
    * timestamps, counts and artifact ids are *not* in it, so re-reporting the
      same unchanged problem cannot fork the incident.

    Including the reason means a file whose blocking reason changes opens a new
    incident. That is the intended reading — a different reason is a different
    thing for an operator to do. The old incident is then closed by
    `resolve_recovered_incidents`, but only once the replacement is durably open:
    a different current fingerprint is the *trigger* for supersession, never the
    proof of it.
    """
    title = f"Workflow B input unresolved: {item.reason_code} ({item.stage})"
    summary_parts = [
        f"An input that reached Workflow B could not be carried to a terminal state at {item.stage}.",
        f"Outcome {item.outcome}, blocking reason {item.reason_code}.",
    ]
    if item.client_code:
        summary_parts.append(f"Client {item.client_code}.")
    if item.report_type:
        summary_parts.append(f"Report type {item.report_type}.")
    summary_parts.append(
        "It is not retried automatically and stays invisible to downstream processing until "
        "an operator resolves it."
    )
    details: dict[str, Any] = {
        "stage": item.stage,
        "outcome": item.outcome,
        "reason_code": item.reason_code,
    }
    if item.detail:
        details["state_detail"] = item.detail
    return SuspectedBugEvent(
        incident_code=INCIDENT_CODE,
        title=title,
        summary=" ".join(summary_parts),
        occurred_at=occurred_at,
        environment=environment,
        severity="warning",
        component=COMPONENT,
        workflow_name=WORKFLOW_NAME,
        stage_name=item.stage,
        job_name=COMPONENT,
        client_code=item.client_code,
        report_type=item.report_type,
        run_id=run_id,
        raw_file_id=item.raw_file_id,
        subject_type=SUBJECT_TYPE,
        subject_key=SUBJECT_KEY,
        subject_value=item.raw_file_id,
        processing_outcome=item.outcome,
        details=details,
        suggested_action=_SUGGESTED_ACTIONS.get(item.reason_code, _GENERIC_SUGGESTED_ACTION),
        fingerprint_fields={
            "raw_file_id": item.raw_file_id,
            "stage": item.stage,
            "reason_code": item.reason_code,
        },
    )


def canonical_one_shot_identities(
    items: Iterable[UnresolvedInput],
) -> list[tuple[str, str, str]]:
    """The complete unreported membership set, in one canonical order.

    Deduplicated and sorted, so the same membership presented in any input
    order is the same list. Everything that has to be stable under reordering —
    the digest, the displayed prefix, the count — is derived from this one
    projection rather than from the caller's arrival order.
    """
    return sorted({item.identity for item in items})


def one_shot_membership_digest(identities: Sequence[tuple[str, str, str]]) -> str:
    """A bounded, complete-set identity for an unreported one-shot population.

    The reviewed candidate fingerprinted the *displayed* list, which was capped
    at `MAX_LISTED_UNREPORTED_ONE_SHOT`. Two different populations that share a
    count and their first 50 sorted entries therefore collided onto a single
    incident, and the second one was silently folded into the first as another
    occurrence of a problem it is not. Display bounding and durable identity are
    now separate concerns: the payload stays capped, this covers every member.

    Streaming by construction — one `sha256` fed record by record — so a large
    cycle costs a constant-size identity rather than a serialized structure that
    grows with the population. Records are joined with delimiters that cannot
    occur in a UUID, a stage name or a reason code, so no two distinct
    memberships can serialize to the same byte string.
    """
    digest = hashlib.sha256()
    digest.update(ONE_SHOT_DIGEST_NAMESPACE)
    for raw_file_id, stage, reason_code in identities:
        digest.update(b"\x1e")
        digest.update("\x1f".join((str(raw_file_id), str(stage), str(reason_code)))
                      .encode("utf-8", "replace"))
    return digest.hexdigest()


def build_truncation_event(items: Sequence[UnresolvedInput], *, run_id: str | None,
                           environment: str, occurred_at: datetime) -> SuspectedBugEvent:
    """The one incident that stands in for one-shot inputs this cycle could not
    represent individually.

    It is opened only when the alternative is silence. A one-shot input's
    evidence exists exclusively in the cycle that produced it, so "the bound
    deferred it" and "its own incident could not be written" have the same
    consequence — nothing will ever raise it again — and both are therefore
    covered here rather than only the first.

    **Identity covers the complete membership; the payload does not have to.**
    `fingerprint_fields` carries `one_shot_membership_digest` over every
    unreported identity plus the exact count, which is two short scalars no
    matter how large the population is — and `SuspectedBugEvent` sanitization
    caps lists at 25 entries anyway, so a verbatim set in the fingerprint would
    have been both unbounded and, above that cap, not even the thing an operator
    sees. Two different truncations are two incidents; re-observing the
    identical set — which one-shot semantics make very nearly impossible — is
    one. Nothing time-varying is in it, for the same reason as `build_event`.

    The human-readable half stays bounded: `evidence.unreported` lists the first
    `MAX_LISTED_UNREPORTED_ONE_SHOT` identities in the same canonical order and
    says so, and the count is always the true total.
    """
    identities = canonical_one_shot_identities(items)
    digest = one_shot_membership_digest(identities)
    listed = identities[:MAX_LISTED_UNREPORTED_ONE_SHOT]
    stages = sorted({stage for _raw, stage, _reason in identities})
    reasons = sorted({reason for _raw, _stage, reason in identities})
    return SuspectedBugEvent(
        incident_code=TRUNCATED_INCIDENT_CODE,
        title=f"Workflow B could not report {len(identities)} one-shot unresolved input(s)",
        summary=(
            f"{len(identities)} input(s) reached Workflow B, failed at a point no later cycle "
            f"re-offers ({', '.join(stages)}), and did not receive their own "
            f"{INCIDENT_CODE} incident in the cycle that observed them. That evidence is not "
            "reproducible, so this incident carries it instead. Blocking reasons: "
            f"{', '.join(reasons)}."
        ),
        occurred_at=occurred_at,
        environment=environment,
        severity="error",
        component=COMPONENT,
        workflow_name=WORKFLOW_NAME,
        job_name=COMPONENT,
        run_id=run_id,
        subject_type=TRUNCATED_SUBJECT_TYPE,
        subject_key=TRUNCATED_SUBJECT_KEY,
        subject_value=digest,
        affected_record_count=len(identities),
        details={
            "unreported_one_shot_count": len(identities),
            "unreported_one_shot_digest": digest,
            "stages": stages,
            "reason_codes": reasons,
            "max_new_one_shot_incidents_per_cycle": max_new_one_shot_incidents_per_cycle(),
            "env_var": MAX_NEW_ONE_SHOT_INCIDENTS_ENV,
            "listed": len(listed),
            "truncated_list": len(listed) < len(identities),
        },
        evidence={
            "unreported": [
                {"raw_file_id": raw_file_id, "stage": stage, "reason_code": reason}
                for raw_file_id, stage, reason in listed
            ],
        },
        suggested_action=(
            "These inputs entered Workflow B and stopped at a point that is not re-offered on a "
            "later cycle, so no future run will raise them again. Inspect each raw file listed "
            "here and either complete the work it is waiting on or record it as intentionally "
            f"unsupported. If more were unreported than are listed, the full count and the "
            f"membership digest are in details. If this recurs, raise "
            f"{MAX_NEW_ONE_SHOT_INCIDENTS_ENV}."
        ),
        fingerprint_fields={
            "unreported_one_shot_digest": digest,
            "unreported_one_shot_count": len(identities),
        },
    )


def postprocessor_completed_raw_file_ids(result: Any) -> set[str]:
    """Inputs whose postprocessing this cycle observed reaching completion.

    This is the *only* durable postprocessor evidence the platform has, and the
    reason `stage3_status='OK'` must never be read as one. There is no
    postprocessor column on `ingest.raw_file`, no per-postprocessor state table
    and no artifact row for a postprocessor run: `_discover_postprocessor_plans`
    derives its work from the Stage 3 identities loaded *in this cycle*, so once
    a file's Stage 3 is `OK` the postprocessor is never re-offered for it and
    nothing durable records whether it ever ran. Adding schema to make automatic
    resolution convenient is not warranted by that; observing an actual
    completion is.

    `operator_action_required` disqualifies an item even on a nominally
    successful outcome: a postprocessor that finished and still wants a human has
    not resolved anything.
    """
    completed: set[str] = set()
    for item in getattr(result, "postprocessors", None) or ():
        raw_file_id = _text(getattr(item, "raw_file_id", None))
        if not raw_file_id or getattr(item, "operator_action_required", False):
            continue
        if _outcome_value(item) in _POSTPROCESSOR_COMPLETED_OUTCOMES:
            completed.add(raw_file_id)
    return completed


def load_open_incidents(conn) -> list[dict[str, Any]]:
    """Open incidents this module owns, with the input and stage each is about.

    The stage is read back because resolution is stage-specific: what proves a
    Stage 2 incident recovered proves nothing about a `postprocess` one. It is
    taken from `fingerprint_fields.stage` first — that is the value the
    fingerprint was actually built from — with `stage_name` as the fallback.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT incident_id::text AS incident_id,
                   fingerprint,
                   latest_payload ->> 'raw_file_id' AS raw_file_id,
                   COALESCE(latest_payload -> 'fingerprint_fields' ->> 'stage',
                            latest_payload ->> 'stage_name') AS stage
            FROM suspected_bug_incidents
            WHERE incident_code = %s
              AND state = 'open'
            """,
            (INCIDENT_CODE,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return rows


def truncation_incident_is_durable(conn, fingerprint: str) -> bool:
    """Is the exact aggregate representation this cycle needs actually in the table?

    The reviewed candidate answered this from `report.error is None`, which is a
    statement about a function call, not about durable state — and
    `load_open_incidents` cannot answer it either, because that query is scoped
    to `INCIDENT_CODE` and the truncation incident carries a different code.
    Asking the table directly, by the fingerprint the event itself computes, is
    what makes "an aggregate representation exists" provable. An incident opened
    by an earlier cycle for the identical membership counts: the condition is
    represented, which is the whole obligation.

    Any failure to establish the fact answers `False`. The caller escalates on
    `False`, so an unreadable database must not be allowed to look like proof.
    """
    if not fingerprint:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 AS present
                FROM suspected_bug_incidents
                WHERE incident_code = %s
                  AND fingerprint = %s
                  AND state = 'open'
                """,
                (TRUNCATED_INCIDENT_CODE, fingerprint),
            )
            present = cur.fetchone() is not None
        conn.rollback()
        return present
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def raw_file_terminal_facts(conn, raw_file_ids: Sequence[str]) -> dict[str, dict[str, bool]]:
    """What the platform can *prove* about each input's durable state.

    Two independent facts per input, kept separate because they prove different
    things:

    * `absent` — the row is gone, so nothing at any stage is waiting for it;
    * `load_terminal` — `stage3_status='OK'` or `status='DUPLICATE_CONTENT'`.
      This proves the ingest/clean/load chain finished. It proves **nothing**
      about work owed after the load, which is exactly why the caller may not
      apply it to a `postprocess` incident.

    An input this function cannot resolve — not a UUID, or a row that exists and
    is neither — gets both facts false, so the caller's failure mode is leaving
    an incident open rather than closing a live one.
    """
    facts: dict[str, dict[str, bool]] = {
        str(value): {"absent": False, "load_terminal": False}
        for value in raw_file_ids if value
    }
    candidates = [value for value in facts if _is_uuid(value)]
    if not candidates:
        return facts
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS raw_file_id,
                   (status = 'DUPLICATE_CONTENT'
                    OR btrim(COALESCE(stage3_status, '')) = 'OK') AS load_terminal
            FROM ingest.raw_file
            WHERE id = ANY(%s::uuid[])
            """,
            (candidates,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    present: set[str] = set()
    for row in rows:
        raw_file_id = str(row["raw_file_id"])
        present.add(raw_file_id)
        facts[raw_file_id]["load_terminal"] = bool(row["load_terminal"])
    for value in candidates:
        if value not in present:
            # A raw file that no longer exists cannot be waiting for anyone.
            facts[value]["absent"] = True
    return facts


def resolve_recovered_incidents(conn, open_incidents: Sequence[dict[str, Any]], *,
                                current_fingerprints: set[str],
                                superseded_by: set[tuple[str, str]],
                                postprocess_completed: set[str]) -> list[str]:
    """Close the incidents whose condition is provably no longer unresolved.

    Two closable shapes, and only two — but each now carries its own proof
    obligation rather than sharing one.

    **Recovered.** The condition the incident names reached a terminal state, as
    judged for that incident's own stage:

    * any stage — the `ingest.raw_file` row is gone;
    * `stage1` / `stage2` / `stage3` — the load chain is terminal
      (`stage3_status='OK'` or `DUPLICATE_CONTENT`);
    * `postprocess` — this cycle observed the postprocessor completing for that
      input. A Stage 3 `OK` deliberately does not qualify: the sequence "Stage 3
      succeeds, postprocessor fails non-retryably, next cycle sees Stage 3 `OK`
      and no postprocessor result because Stage 3 has nothing left to offer" is
      precisely how a postprocessor incident used to close while the work was
      still owed;
    * an incident whose stage cannot be read stays open unless its row is gone.

    **Superseded.** The same input is blocked at the same stage for a *different*
    reason, and `superseded_by` says that replacement is durably open — proved by
    re-reading the incident table after creation, not by having intended to
    create it. A replacement the per-cycle bound deferred, or one whose
    persistence failed, is absent from that set, so its predecessor stays open
    and the input keeps exactly one actionable incident throughout.

    Everything else stays open. In particular an incident whose input this cycle
    never examined is untouched: a bounded sweep, a stage that did not run or a
    truncated batch must never look like a resolution.
    """
    candidates = [
        incident for incident in open_incidents
        if incident["fingerprint"] not in current_fingerprints
    ]
    if not candidates:
        return []

    closable: list[str] = []
    needs_proof: list[dict[str, Any]] = []
    for incident in candidates:
        raw_file_id = _text(incident.get("raw_file_id"))
        stage = _text(incident.get("stage")) or ""
        if raw_file_id and (raw_file_id, stage) in superseded_by:
            closable.append(incident["incident_id"])
        elif raw_file_id:
            needs_proof.append(incident)

    if needs_proof:
        facts = raw_file_terminal_facts(
            conn, [_text(incident.get("raw_file_id")) or "" for incident in needs_proof]
        )
        for incident in needs_proof:
            raw_file_id = _text(incident.get("raw_file_id")) or ""
            stage = _text(incident.get("stage")) or ""
            fact = facts.get(raw_file_id) or {}
            if fact.get("absent"):
                closable.append(incident["incident_id"])
            elif stage in _LOAD_PROVABLE_STAGES and fact.get("load_terminal"):
                closable.append(incident["incident_id"])
            elif stage == "postprocess" and raw_file_id in postprocess_completed:
                closable.append(incident["incident_id"])

    resolved = sorted(set(closable))
    if not resolved:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE suspected_bug_incidents
               SET state = 'resolved',
                   resolved_at = now(),
                   updated_at = now()
             WHERE incident_id = ANY(%s::uuid[])
               AND incident_code = %s
               AND state = 'open'
            """,
            (resolved, INCIDENT_CODE),
        )
    conn.commit()
    return resolved


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(1, value)


def max_new_incidents_per_cycle() -> int:
    return _positive_int_env(MAX_NEW_INCIDENTS_ENV, DEFAULT_MAX_NEW_INCIDENTS_PER_CYCLE)


def max_new_one_shot_incidents_per_cycle() -> int:
    return _positive_int_env(
        MAX_NEW_ONE_SHOT_INCIDENTS_ENV, DEFAULT_MAX_NEW_ONE_SHOT_INCIDENTS_PER_CYCLE
    )


def signal_unresolved_inputs(client, conn, run_id: str, result: Any) -> UnresolvedInputSignalResult:
    """Open, keep and close the per-input incidents for one Workflow B cycle.

    Runs after the stages, on every path that entered them, including the ones
    that end in `WorkflowBOrchestrationError`: an input's actionability does not
    depend on whether some other input failed.

    The order below is the contract, not an implementation detail. Creation
    happens first, the durable state is then re-read from the database, and only
    incidents whose replacement is present in that re-read are superseded. Doing
    resolution first — or trusting that a create *was going to* succeed — is what
    lets an unresolved input end a cycle with no open incident at all.

    The pass ends by checking the one thing it cannot leave unproved: that every
    one-shot input it observed is represented durably, individually or in the
    aggregate. If some are not, `safety_contract_violated` is set and the
    orchestrator fails the cycle — see the module docstring. Nothing else here
    can change a cycle's verdict.
    """
    signal = UnresolvedInputSignalResult(
        max_new_incidents_per_cycle=max_new_incidents_per_cycle(),
        max_new_one_shot_incidents_per_cycle=max_new_one_shot_incidents_per_cycle(),
    )
    items = collect_unresolved_inputs(result)
    signal.unresolved_input_count = len(items)
    signal.one_shot_input_count = sum(1 for item in items if not item.replayable)

    config = load_alert_config()
    occurred_at = datetime.now(timezone.utc)
    events = [
        (item, build_event(item, run_id=run_id, environment=config.environment, occurred_at=occurred_at))
        for item in items
    ]
    fingerprints = {event.fingerprint(): item for item, event in events}

    open_incidents = load_open_incidents(conn)
    open_fingerprints = {incident["fingerprint"] for incident in open_incidents}

    pending = [(item, event) for item, event in events if event.fingerprint() not in open_fingerprints]
    signal.already_open_count = len(events) - len(pending)

    # Two budgets, because "deferred" means two different things. A replayable
    # candidate the bound refuses is genuinely picked up next cycle; a one-shot
    # candidate it refuses is gone. They are therefore never allowed to contend
    # for the same capacity — the production defect was a 63-file Stage 2
    # backlog consuming all 20 slots ahead of a postprocess failure that no
    # later cycle would ever produce again.
    one_shot_pending = [pair for pair in pending if not pair[0].replayable]
    replayable_pending = [pair for pair in pending if pair[0].replayable]

    def drain(queue, limit: int) -> tuple[int, int]:
        """Attempt `queue` until `limit` incidents are *opened*.

        Returns `(opened, never_attempted)`. The bound is spent by incidents
        opened, not by attempts. A candidate whose persistence fails costs an
        attempt and releases the slot, so it cannot hold the same capacity
        forever while the inputs behind it are never tried — and it is reported
        as a failure rather than counted as deferred coverage.
        """
        opened = 0
        for index, (item, event) in enumerate(queue):
            if opened >= limit:
                return opened, len(queue) - index
            signal.incidents_attempted += 1
            report = safe_report_suspected_bug(event, conn=conn, config=config, now=occurred_at)
            if report.error:
                signal.report_failures += 1
                if signal.report_failures <= MAX_LOGGED_REPORT_FAILURES:
                    _log(client, "WARNING", run_id,
                         "Workflow B unresolved-input incident could not be reported",
                         {"raw_file_id": item.raw_file_id, "stage": item.stage,
                          "reason_code": item.reason_code, "replayable": item.replayable,
                          "error": report.error})
                continue
            opened += 1
        return opened, 0

    # One-shot first, and on its own budget: whatever is left of it afterwards
    # cannot be recovered by waiting.
    signal.one_shot_incidents_opened = drain(
        one_shot_pending, signal.max_new_one_shot_incidents_per_cycle
    )[0]
    replayable_opened, signal.incidents_deferred = drain(
        replayable_pending, signal.max_new_incidents_per_cycle
    )
    signal.incidents_opened = signal.one_shot_incidents_opened + replayable_opened

    # Proof, not intent: what is durably open *now*, after everything this cycle
    # tried to write. A deferred or failed replacement is simply not in here, and
    # its predecessor therefore survives resolution below.
    durable_open = load_open_incidents(conn)
    durable_fingerprints = {incident["fingerprint"] for incident in durable_open}

    # `incidents_deferred` keeps its original meaning exactly: replayable work
    # this cycle chose not to attempt, which the next cycle rediscovers.
    #
    # The one-shot half is measured differently and deliberately so — against
    # the durable re-read, not against a loop index. "Deferred by the budget"
    # and "attempted but did not persist" have the same consequence for an input
    # nothing will offer again, so both belong in the same set.
    unreported_one_shot = [
        item for item, event in one_shot_pending if event.fingerprint() not in durable_fingerprints
    ]
    signal.one_shot_unreported = len(unreported_one_shot)
    if unreported_one_shot:
        # The evidence for these disappears with this cycle, so the fact that
        # they were not individually represented has to become durable now. One
        # bounded incident, never one per input: this path exists precisely
        # because per-input capacity ran out.
        truncation = build_truncation_event(
            unreported_one_shot, run_id=run_id,
            environment=config.environment, occurred_at=occurred_at,
        )
        report = safe_report_suspected_bug(truncation, conn=conn, config=config, now=occurred_at)
        signal.truncation_incident_reported = not report.error
        # Proof, again, and about the fallback itself this time. `report.error`
        # describes a call; only the table describes the durable state a later
        # operator can act on, and the fallback is the last representation these
        # inputs will ever have.
        signal.truncation_incident_durable = truncation_incident_is_durable(
            conn, truncation.fingerprint()
        )
        if not signal.truncation_incident_durable:
            signal.one_shot_without_durable_evidence = len(unreported_one_shot)
        _log(client,
             "ERROR" if signal.safety_contract_violated else "WARNING", run_id,
             "Workflow B could not report every one-shot unresolved input individually",
             {"unreported_one_shot": signal.one_shot_unreported,
              "max_new_one_shot_incidents_per_cycle": signal.max_new_one_shot_incidents_per_cycle,
              "env_var": MAX_NEW_ONE_SHOT_INCIDENTS_ENV,
              "truncation_incident_reported": signal.truncation_incident_reported,
              "truncation_incident_durable": signal.truncation_incident_durable,
              "truncation_incident_code": TRUNCATED_INCIDENT_CODE,
              "error": report.error,
              "identities": unresolved_input_identities(
                  unreported_one_shot[:MAX_LISTED_UNREPORTED_ONE_SHOT])})
        if signal.safety_contract_violated:
            # Neither representation exists. Say so in its own line, because the
            # cycle is about to fail on exactly this and the operator needs the
            # identities the failed incident would have carried.
            _log(client, "ERROR", run_id,
                 "Workflow B observed one-shot unresolved inputs with no durable "
                 "actionable representation; failing the cycle",
                 {"one_shot_without_durable_evidence":
                      signal.one_shot_without_durable_evidence,
                  "truncation_incident_code": TRUNCATED_INCIDENT_CODE,
                  "identities": unresolved_input_identities(
                      unreported_one_shot[:MAX_LISTED_UNREPORTED_ONE_SHOT])})
        durable_open = load_open_incidents(conn)
        durable_fingerprints = {incident["fingerprint"] for incident in durable_open}
    superseded_by = {
        (item.raw_file_id, item.stage)
        for fingerprint, item in fingerprints.items()
        if fingerprint in durable_fingerprints
    }

    resolved = resolve_recovered_incidents(
        conn, durable_open,
        current_fingerprints=set(fingerprints),
        superseded_by=superseded_by,
        postprocess_completed=postprocessor_completed_raw_file_ids(result),
    )
    signal.incidents_resolved = len(resolved)

    level = "ERROR" if signal.safety_contract_violated else (
        "WARNING" if (signal.incidents_deferred or signal.report_failures
                      or signal.one_shot_unreported) else "INFO")
    _log(client, level, run_id, "Workflow B unresolved-input signal", signal.to_dict())
    if signal.report_failures > MAX_LOGGED_REPORT_FAILURES:
        _log(client, "WARNING", run_id,
             "Workflow B unresolved-input incident reporting is failing for many inputs",
             {"report_failures": signal.report_failures,
              "individually_logged": MAX_LOGGED_REPORT_FAILURES,
              "incidents_attempted": signal.incidents_attempted})
    if signal.incidents_deferred:
        # Never let a bound look like coverage: say out loud that this cycle did
        # not report everything it found, and that the next one will.
        _log(client, "WARNING", run_id,
             "Workflow B deferred unresolved-input incidents to the next cycle",
             {"deferred": signal.incidents_deferred,
              "max_new_incidents_per_cycle": signal.max_new_incidents_per_cycle,
              "env_var": MAX_NEW_INCIDENTS_ENV})
    return signal


def _log(client, level: str, run_id: str, message: str, context: dict[str, Any]) -> None:
    try:
        client.log(level, "SCRIPT", COMPONENT, message, run_id=run_id, context=context)
    except Exception:
        # Observability must never convert a healthy cycle into a failed one.
        pass


def one_shot_unresolved_input_count(result: Any) -> int:
    """How many observed inputs of this cycle nothing will ever offer again.

    Pure and database-free, so the orchestrator can still answer "was anything
    at risk?" on the path where the signalling pass could not run at all — a
    connection it never opened proves nothing about the incidents it never
    wrote, and a cycle that observed one-shot work must not settle on that.
    """
    return sum(1 for item in collect_unresolved_inputs(result) if not item.replayable)


def unresolved_input_identities(items: Iterable[UnresolvedInput]) -> list[dict[str, Any]]:
    """Compact, secret-free projection used by the terminal payload and tests."""
    return [
        {
            "raw_file_id": item.raw_file_id,
            "stage": item.stage,
            "outcome": item.outcome,
            "reason_code": item.reason_code,
            "client_code": item.client_code,
            "report_type": item.report_type,
        }
        for item in items
    ]
