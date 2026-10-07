#!/usr/bin/env python3
"""Deterministic regressions for the Workflow B unresolved-input signal.

The defect this suite pins down is a saturated aggregate. Production had been
finishing every Workflow B cycle as `SUCCEEDED_WITH_REVIEW_ITEMS` over a backlog
of 59 unresolved files that grows by roughly four `report_112` files a week. The
run stayed `SUCCESS`, the execution watchdog stayed `OK`, and no incident of any
kind existed for the condition — so the 60th newly stranded input produced no
materially new signal at all:

    ARRIVED INPUT -> NOT FULLY PROCESSED -> NO ADEQUATE ACTIONABLE SIGNAL

The correction is granularity, not severity: the cycle still finishes as a
review outcome, and each unresolved input additionally carries its own durable
`WORKFLOW_B_INPUT_UNRESOLVED` incident.

Two halves:

  pure        collection, fingerprint identity and the mailbox-inspection
              invariant, with no database at all;
  PostgreSQL  the incident lifecycle end to end against a disposable
              PostgreSQL 16 this suite starts and removes itself — new incident
              while a backlog is open, no duplicate alert on rescan, resolution
              when the input becomes terminal, and the bounded per-cycle intake.

The PostgreSQL half self-provisions through `disposable_postgres`; it never
accepts a DSN from the environment and can therefore not be pointed at
production. No SMTP is configured, so the outbox is inspected and never
delivered.

    PYTHONPATH="$PWD" .venv/bin/python \\
        ops/tests_manual/test_workflow_b_unresolved_input_signal.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import pandas  # noqa: F401
except ModuleNotFoundError:  # Stage 2 imports it transitively; nothing here uses it.
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = type("DataFrame", (), {})
    pandas_stub.Series = type("Series", (), {})
    sys.modules["pandas"] = pandas_stub

from jobs.mail.stage1_batch_contract import Stage1BatchResult, Stage1ItemResult, Stage1Outcome
from jobs.reports.stage2.batch_contract import Stage2BatchResult, Stage2ItemResult, Stage2Outcome
from jobs.reports.stage3.batch_contract import Stage3BatchResult, Stage3ItemResult, Stage3Outcome
from jobs.reports.workflow_b import orchestrator as job
from jobs.reports.workflow_b import unresolved_inputs as sig


FAILURES: list[str] = []
CHECKS_RUN = 0
NOW = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
RUN_ID = "44444444-4444-4444-4444-444444444444"


def check(name: str, condition: bool, detail: Any = "") -> None:
    """Record one check. `detail` is only rendered on failure, and is coerced:
    a failing assertion must never be replaced by a TypeError from its own
    diagnostics."""
    global CHECKS_RUN
    CHECKS_RUN += 1
    if condition:
        print(f"[PASS] {name}")
        return
    FAILURES.append(name)
    rendered = "" if detail == "" or detail is None else str(detail)
    print(f"[FAIL] {name}{(' -> ' + rendered) if rendered else ''}")


def raw_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


class Client:
    """Captures log calls; never touches the API."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = []

    def log(self, level, _type, _source, message, run_id=None, context=None, **_kw):
        self.entries.append((level, message, dict(context or {})))

    def messages(self) -> list[str]:
        return [message for _level, message, _ctx in self.entries]

    def context_for(self, message: str) -> dict:
        for _level, entry, context in self.entries:
            if entry == message:
                return context
        return {}


# --------------------------------------------------------------------------- #
# Collection — which arrived inputs count as unresolved
# --------------------------------------------------------------------------- #

def _stranded_stage2_item(index: int, reason: str = "missing_client_code") -> Stage2ItemResult:
    return Stage2ItemResult(
        raw_file_id=raw_id(index),
        outcome=Stage2Outcome.STRANDED_UNROUTABLE,
        persisted_status="OK",
        reason_code=reason,
        review_required=True,
        report_type="report_112",
    )


def _result_with(stage2_items: list[Stage2ItemResult],
                 postprocessors: list[job.WorkflowBPostprocessorResult] | None = None,
                 stage3_items: list[Stage3ItemResult] | None = None,
                 ) -> job.WorkflowBBatchResult:
    result = job.WorkflowBBatchResult(mode="scheduled")
    result.lock_acquired = True
    result.stage1.started = result.stage1.completed = True
    result.stage1.result = Stage1BatchResult(mailbox_check_completed=True)
    result.stage2.started = result.stage2.completed = True
    result.stage2.result = Stage2BatchResult(items=list(stage2_items))
    result.stage2.operator_action_required = any(item.review_required for item in stage2_items)
    result.stage3.started = result.stage3.completed = True
    result.stage3.result = Stage3BatchResult(items=list(stage3_items or []))
    result.postprocessors = list(postprocessors or [])
    return result


def _replayable_stage3_item(index: int, reason: str = "missing_report_policy") -> Stage3ItemResult:
    """A Stage 3 item left in a status autonomous discovery re-admits.

    After the Stage 2 refutation this is the *only* replayable unresolved class
    the repository still has, so every assertion about the replayable budget —
    deferral, next-cycle pickup, supersession ordering, head-of-queue starvation
    — is written against this shape. `persisted_status=None` is the blocked-
    before-stamping case `_stage3_batch_status_eligible_sql` re-admits as NULL,
    and unlike a swept Stage 2 row its rediscovery does not depend on where it
    sorts: Stage 3 discovery runs with no LIMIT, so the whole eligible set comes
    back on every cycle.
    """
    return Stage3ItemResult(
        raw_file_id=raw_id(index),
        client_code="BRAVO00016",
        report_type="report_207",
        outcome=Stage3Outcome.BLOCKED_OPERATOR_ACTION,
        persisted_status=None,
        error_category=reason,
        operator_action_required=True,
    )


def _replayable_backlog(indexes) -> job.WorkflowBBatchResult:
    return _result_with([], stage3_items=[_replayable_stage3_item(i) for i in indexes])


def _postprocessor(index: int, outcome: job.WorkflowBPostprocessorOutcome, *,
                   operator: bool = False, retryable: bool = False,
                   category: str | None = None) -> job.WorkflowBPostprocessorResult:
    return job.WorkflowBPostprocessorResult(
        postprocessor_name="report_207_trip_metrics",
        raw_file_id=raw_id(index),
        client_code="ALPHA00001",
        report_type="report_207",
        outcome=outcome,
        retryable=retryable,
        operator_action_required=operator,
        error_category=category,
    )


def test_the_production_stranded_shape_is_collected() -> None:
    """The 56-file `report_112` shape: Stage 2 says OK, nobody can route it."""
    items = sig.collect_unresolved_inputs(_result_with([_stranded_stage2_item(1)]))
    check("a stranded unroutable file is collected", len(items) == 1, str(items))
    if items:
        item = items[0]
        check("it names the input", item.raw_file_id == raw_id(1), item.raw_file_id)
        check("it names the blocking reason", item.reason_code == "missing_client_code", item.reason_code)
        check("it names the stage", item.stage == "stage2", item.stage)
        check("it carries the report type", item.report_type == "report_112", str(item.report_type))


def test_a_retryable_failure_is_not_an_unresolved_input() -> None:
    """The next cycle retries it, and a persistent one already fails the run."""
    result = _result_with([])
    result.stage3.result = Stage3BatchResult(items=[Stage3ItemResult(
        raw_file_id=raw_id(2), client_code="C", report_type="report_207",
        outcome=Stage3Outcome.FAILED_RETRYABLE_DATABASE, retryable=True,
    )])
    check("a retryable Stage 3 failure raises no per-input incident",
          sig.collect_unresolved_inputs(result) == [])


def test_a_cycle_level_failure_without_an_input_is_not_collected() -> None:
    """A missing IMAP_PASSWORD is a cycle failure; the terminal-failure path owns it."""
    result = _result_with([])
    result.stage1.result = Stage1BatchResult(mailbox_check_completed=False, items=[Stage1ItemResult(
        Stage1Outcome.FAILED_NON_RETRYABLE_CONFIGURATION,
        operator_action_required=True, error_category="IMAP_PASSWORD_missing",
    )])
    check("a configuration failure with no raw file raises no per-input incident",
          sig.collect_unresolved_inputs(result) == [])


def test_every_blocked_class_reaches_the_signal() -> None:
    """One representative of each material unresolved class from the contract."""
    result = _result_with([
        _stranded_stage2_item(3, "missing_client_code"),
        Stage2ItemResult(raw_file_id=raw_id(4), outcome=Stage2Outcome.STRANDED_AWAITING_REVIEW,
                         reason_code="cleaning_not_implemented", review_required=True),
        Stage2ItemResult(raw_file_id=raw_id(5), outcome=Stage2Outcome.UNSUPPORTED_REPORT,
                         reason_code="unsupported_report"),
        Stage2ItemResult(raw_file_id=raw_id(6), outcome=Stage2Outcome.AMBIGUOUS_DETECTION,
                         reason_code="ambiguous_detection"),
    ])
    result.stage3.result = Stage3BatchResult(items=[Stage3ItemResult(
        raw_file_id=raw_id(7), client_code="BRAVO00016", report_type="report_207",
        outcome=Stage3Outcome.BLOCKED_OPERATOR_ACTION,
        error_category="missing_report_policy", operator_action_required=True,
    )])
    result.postprocessors = [job.WorkflowBPostprocessorResult(
        postprocessor_name="report_207_speeding_migration", raw_file_id=raw_id(8),
        client_code="ALPHA00001", report_type="report_207",
        outcome=job.WorkflowBPostprocessorOutcome.BLOCKED_UNSUPPORTED_CONFIGURATION,
        operator_action_required=True, error_category="unsupported_configuration",
    )]
    collected = {(item.raw_file_id, item.stage) for item in sig.collect_unresolved_inputs(result)}
    expected = {
        (raw_id(3), "stage2"), (raw_id(4), "stage2"), (raw_id(5), "stage2"),
        (raw_id(6), "stage2"), (raw_id(7), "stage3"), (raw_id(8), "postprocess"),
    }
    check("every material unresolved class produces one signal item",
          collected == expected, str(sorted(collected ^ expected)))


def test_a_healthy_cycle_collects_nothing() -> None:
    check("an empty mailbox produces no unresolved-input signal",
          sig.collect_unresolved_inputs(_result_with([])) == [])


# --------------------------------------------------------------------------- #
# Replayability — which unresolved items a later cycle will offer again
# --------------------------------------------------------------------------- #

def _stage2_processing_item(index: int, outcome=Stage2Outcome.PENDING_HUMAN_REVIEW,
                            reason: str = "stage2_exception") -> Stage2ItemResult:
    """A Stage 2 item this cycle's *processing* produced, not the sweep.

    Distinguished from `_stranded_stage2_item` by outcome alone, which is the
    whole point: `STRANDED_*` is emitted only by `_stranded_item`, i.e. only by
    the durable unrouted sweep.
    """
    return Stage2ItemResult(
        raw_file_id=raw_id(index),
        outcome=outcome,
        persisted_status="PENDING_REVIEW",
        reason_code=reason,
        review_required=True,
        report_type="report_112",
    )


def test_stage2_is_one_shot_whatever_produced_the_item() -> None:
    """The Stage 2 refutation, in memory.

    The second candidate split Stage 2 in two: an item carrying a `STRANDED_*`
    outcome came from the durable unrouted sweep, therefore sat inside the
    oldest-first `max_pages * page_size` prefix, therefore — the argument went —
    could only move *forward* in that prefix and would be swept again. Review
    disproved the premise rather than the arithmetic, and the repository is what
    disproves it: `_candidate_rows(raw_file_ids=[...])` admits any NORMALIZED
    row by id with no eligibility predicate, and `_persist_stage2` then writes
    `stage2_updated_at = NOW()` whether or not material work remains. A row that
    was inside the prefix is now behind it.

    The corrected model does not try to tell the two provenances apart, because
    the invalidating write applies to both. Every material Stage 2 unresolved
    event is one-shot, so it must become durably actionable in the cycle that
    observed it.
    """
    swept = sig.collect_unresolved_inputs(_result_with([_stranded_stage2_item(920)]))[0]
    processed = sig.collect_unresolved_inputs(_result_with([_stage2_processing_item(921)]))[0]
    check("a sweep-observed Stage 2 item is one-shot", not swept.replayable, str(swept))
    check("a Stage 2 item produced by this cycle's processing is one-shot",
          not processed.replayable, str(processed))
    check("both are nevertheless the same nominal stage",
          swept.stage == processed.stage == "stage2",
          f"{swept.stage} {processed.stage}")

    # Every Stage 2 outcome the collector recognises, including both
    # `_stranded_item` outcomes the previous candidate treated as proof.
    for outcome in (Stage2Outcome.STRANDED_UNROUTABLE, Stage2Outcome.STRANDED_AWAITING_REVIEW,
                    Stage2Outcome.PENDING_HUMAN_REVIEW, Stage2Outcome.UNSUPPORTED_REPORT,
                    Stage2Outcome.AMBIGUOUS_DETECTION, Stage2Outcome.REJECTED_VALIDATION,
                    Stage2Outcome.FAILED_NON_RETRYABLE,
                    Stage2Outcome.FAILED_IDEMPOTENCY_CONFLICT):
        item = sig.collect_unresolved_inputs(_result_with([Stage2ItemResult(
            raw_file_id=raw_id(923), outcome=outcome, reason_code="r",
            review_required=True)]))[0]
        check(f"a {outcome.value} Stage 2 item is one-shot", not item.replayable, str(item))

    check("no Stage 2 provenance test survives in the module",
          not hasattr(sig, "stage2_item_is_replayable")
          and not hasattr(sig, "STAGE2_SWEEP_OBSERVED_OUTCOMES"),
          str(sorted(name for name in dir(sig) if "stage2" in name.lower())))


def test_stage3_replayability_does_not_rest_on_an_ordinal_position() -> None:
    """Why the Stage 2 refutation does not propagate to Stage 3.

    Both proofs used to share one sentence about `stage2_updated_at ASC, id ASC`.
    They never had to: autonomous Stage 3 discovery is invoked with no `limit`,
    so `_select_stage3_candidates` emits no LIMIT clause and returns the entire
    eligible set. The ordering decides processing order inside a set every member
    of which is already selected, so a mutation that re-stamps the sort key moves
    a row *within* an unbounded set instead of *out of* a bounded prefix.

    Asserted against the real query text and the real orchestrator parameter
    contract, so a future change that introduces a default Stage 3 batch limit
    breaks this rather than silently invalidating the Stage 3 replayable class.
    """
    from jobs.reports.stage3 import job_stage3 as s3

    captured: dict[str, Any] = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, query, params=None):
            captured["query"] = query
            captured["params"] = params
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    s3._select_stage3_candidates(_Conn(), limit=None)
    check("autonomous Stage 3 discovery selects the whole eligible set",
          "LIMIT" not in captured.get("query", ""), captured.get("query", "")[-200:])
    check("and it filters on the durable status, not on a position",
          "stage3_status IS NULL" in captured.get("query", ""), "")

    # The orchestrator never supplies a limit, so `limit=None` is the deployed
    # call and not merely a reachable one.
    stage1, stage2, stage3, mode = job._validate_params({"mode": "scheduled"})
    check("the scheduled orchestrator passes no Stage 3 batch limit",
          stage3.get("limit") is None and mode == "scheduled", str(stage3))


def _stage3_item(index: int, persisted_status: str | None) -> Stage3ItemResult:
    return Stage3ItemResult(
        raw_file_id=raw_id(index), client_code="BRAVO00016", report_type="report_207",
        outcome=Stage3Outcome.BLOCKED_OPERATOR_ACTION, persisted_status=persisted_status,
        error_category="missing_report_policy", operator_action_required=True,
    )


def _collect_stage3(item: Stage3ItemResult) -> sig.UnresolvedInput:
    result = _result_with([])
    result.stage3.result = Stage3BatchResult(items=[item])
    return sig.collect_unresolved_inputs(result)[0]


def test_stage3_replayability_follows_the_durable_status_discovery_re_admits() -> None:
    """`_stage3_batch_status_eligible_sql` (P0-E) re-admits NULL, '', RUNNING
    past grace and ERROR. Anything else is a state nothing re-selects, so it is
    one-shot — including a status a future Stage 3 has not been written yet."""
    for status in (None, "", "ERROR", "RUNNING", "  error  "):
        item = _collect_stage3(_stage3_item(930, status))
        check(f"a Stage 3 item left at {status!r} is replayable", item.replayable, str(item))
    for status in ("OK", "BLOCKED", "SOMETHING_NEW"):
        item = _collect_stage3(_stage3_item(931, status))
        check(f"a Stage 3 item left at {status!r} is one-shot", not item.replayable, str(item))


def test_one_shot_sources_stay_one_shot() -> None:
    """`postprocess` and `stage1` have no rediscovery mechanism at all."""
    postprocess = sig.collect_unresolved_inputs(_result_with([], [_postprocessor(
        932, job.WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE,
        operator=True, category="ambiguous_enrichment_match")]))[0]
    check("a postprocess failure is one-shot", not postprocess.replayable, str(postprocess))

    stage1_result = _result_with([])
    stage1_result.stage1.result = Stage1BatchResult(
        mailbox_check_completed=True,
        items=[Stage1ItemResult(Stage1Outcome.FAILED_NON_RETRYABLE_VALIDATION,
                                raw_file_id=raw_id(933), operator_action_required=True,
                                error_category="unsupported_attachment")])
    stage1_items = sig.collect_unresolved_inputs(stage1_result)
    check("a Stage 1 blocked input is collected and one-shot",
          len(stage1_items) == 1 and not stage1_items[0].replayable, str(stage1_items))


def test_an_undeclared_event_source_is_one_shot_by_default() -> None:
    """Fail-safe direction: `replayable` is a stored field whose default is
    False, so a future collection source that forgets to decide inherits
    one-shot rather than "the next cycle will find it again"."""
    invented = sig.UnresolvedInput(raw_file_id=raw_id(934), stage="stage_of_the_future",
                                   outcome="X", reason_code="y")
    check("an undeclared event source is treated as one-shot", not invented.replayable)
    check("and declaring it replayable has to be explicit",
          sig.UnresolvedInput(raw_file_id=raw_id(934), stage="stage2", outcome="X",
                              reason_code="y").replayable is False)


def test_the_collected_production_shape_carries_its_replayability() -> None:
    """The exact 2026-08-24 20:00 cycle shape: a Stage 2 backlog plus the ALPHA
    postprocessor failure that arrived behind it.

    Under the corrected model that whole cycle is one-shot, which is precisely
    why the starvation it produced cannot recur: there is no longer a class that
    can consume capacity the postprocess failure needed, and the one-shot budget
    is sized for the standing Stage 2 population rather than for one file.
    """
    result = _result_with(
        [_stranded_stage2_item(index) for index in range(910, 915)],
        [_postprocessor(915, job.WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE,
                        operator=True, category="ambiguous_enrichment_match")],
    )
    collected = sig.collect_unresolved_inputs(result)
    one_shot = [item for item in collected if not item.replayable]
    check("the whole production cycle shape is one-shot",
          len(collected) == 6 and len(one_shot) == 6, str(collected))

    # A replayable Stage 3 item in the same cycle still takes the other path.
    mixed = sig.collect_unresolved_inputs(_result_with(
        [_stranded_stage2_item(916)], stage3_items=[_replayable_stage3_item(917)]))
    check("and Stage 3 is still the class that may be deferred",
          {item.stage: item.replayable for item in mixed} == {"stage2": False, "stage3": True},
          str(mixed))


def test_no_part_of_a_stage2_sweep_is_replayable() -> None:
    """The in-memory half of the refutation, over a real reconciliation sweep.

    The previous candidate asserted the opposite of the last check here: that
    everything an exhaustive sweep reached was replayable, and only a file
    behind the backstop was one-shot. Both halves are one-shot now, so a budget
    may merely defer neither of them.
    """
    backlog = _unowned_rows(300)
    sweep, result = _reconcile_over(backlog, page_size=100, max_pages=2)
    newest = _stage2_processing_item(940)
    result.items.append(newest)

    check("the sweep really did stop before the whole set",
          sweep.truncated is True and sweep.rows_inspected == 200,
          f"truncated={sweep.truncated} inspected={sweep.rows_inspected}")

    collected = sig.collect_unresolved_inputs(_result_with(list(result.items)))
    by_id = {item.raw_file_id: item for item in collected}
    check("the item behind the backstop is collected", raw_id(940) in by_id, str(len(collected)))
    check("and nothing the sweep did reach is replayable either",
          not any(item.replayable for item in collected),
          str([item.raw_file_id for item in collected if item.replayable][:5]))

    _sweep, covered = _reconcile_over(_unowned_rows(300), page_size=100, max_pages=50)
    reached = sig.collect_unresolved_inputs(_result_with(list(covered.items)))
    check("an exhaustive sweep does not confer replayability",
          len(reached) == 300 and not any(item.replayable for item in reached),
          str(sum(1 for item in reached if item.replayable)))


# --------------------------------------------------------------------------- #
# Fingerprint identity — the property that makes a new problem visible
# --------------------------------------------------------------------------- #

def _fingerprint(item: sig.UnresolvedInput) -> str:
    return sig.build_event(item, run_id=RUN_ID, environment="test", occurred_at=NOW).fingerprint()


def test_two_stranded_files_are_two_incidents() -> None:
    """The whole point: the 60th problem cannot hide inside the first 59."""
    first = sig.collect_unresolved_inputs(_result_with([_stranded_stage2_item(10)]))[0]
    second = sig.collect_unresolved_inputs(_result_with([_stranded_stage2_item(11)]))[0]
    check("two different stranded inputs have different fingerprints",
          _fingerprint(first) != _fingerprint(second))


def test_the_same_problem_keeps_one_identity_across_cycles() -> None:
    """Rescanning must not fork one incident into a new one every 06:00 and 20:00."""
    item = sig.collect_unresolved_inputs(_result_with([_stranded_stage2_item(12)]))[0]
    later = sig.build_event(item, run_id="99999999-9999-9999-9999-999999999999",
                            environment="test", occurred_at=NOW + timedelta(days=3))
    check("run id and time do not change the fingerprint",
          _fingerprint(item) == later.fingerprint())


def test_a_changed_blocking_reason_is_a_different_problem() -> None:
    same_file_new_reason = sig.collect_unresolved_inputs(
        _result_with([_stranded_stage2_item(12, "missing_stage2_report_type")]))[0]
    original = sig.collect_unresolved_inputs(_result_with([_stranded_stage2_item(12)]))[0]
    check("a different blocking reason on the same file is a different incident",
          _fingerprint(original) != _fingerprint(same_file_new_reason))


def test_the_incident_says_what_to_do() -> None:
    event = sig.build_event(
        sig.collect_unresolved_inputs(_result_with([_stranded_stage2_item(13)]))[0],
        run_id=RUN_ID, environment="test", occurred_at=NOW)
    payload = event.sanitized_payload()
    check("the incident names the raw file", payload["raw_file_id"] == raw_id(13))
    check("the incident carries an operator action",
          "client_code" in str(payload["suggested_action"]), str(payload["suggested_action"]))
    check("the incident is a warning, not an error",
          payload["severity"] == "warning", payload["severity"])


# --------------------------------------------------------------------------- #
# Blocker 1 — the truncation incident's durable identity is the complete set
# --------------------------------------------------------------------------- #

def _one_shot(index: int, reason: str = "ambiguous_enrichment_match") -> sig.UnresolvedInput:
    return sig.UnresolvedInput(
        raw_file_id=raw_id(index), stage="postprocess",
        outcome="FAILED_NON_RETRYABLE", reason_code=reason,
        client_code="ALPHA00001", report_type="report_207",
    )


def _truncation(items) -> Any:
    return sig.build_truncation_event(items, run_id=RUN_ID, environment="test", occurred_at=NOW)


def test_two_populations_sharing_a_displayed_prefix_are_two_incidents() -> None:
    """Blocker 1. The reviewed candidate fingerprinted the *capped* list.

    `MAX_LISTED_UNREPORTED_ONE_SHOT` is 50, so two 51-item populations whose
    first 50 sorted identities are identical collided onto one fingerprint —
    same count, same displayed prefix — and the second cycle's distinct set was
    folded in as another occurrence of a problem it is not. The durable identity
    now covers every member.
    """
    shared = [_one_shot(index) for index in range(2000, 2050)]
    first = shared + [_one_shot(2900)]
    second = shared + [_one_shot(2901)]
    check("both populations really are above the display cap",
          len(first) == 51 > sig.MAX_LISTED_UNREPORTED_ONE_SHOT, str(len(first)))

    first_event, second_event = _truncation(first), _truncation(second)
    shown_first = [entry["raw_file_id"] for entry in first_event.evidence["unreported"]]
    shown_second = [entry["raw_file_id"] for entry in second_event.evidence["unreported"]]
    check("[defect shape] their displayed evidence is byte-identical",
          shown_first == shown_second and len(shown_first) == 50, str(len(shown_first)))
    check("and their counts are identical too",
          first_event.affected_record_count == second_event.affected_record_count == 51,
          f"{first_event.affected_record_count} {second_event.affected_record_count}")
    check("yet the durable fingerprints differ",
          first_event.fingerprint() != second_event.fingerprint(),
          first_event.fingerprint())
    check("because the digest covers the members past the cap",
          bool(first_event.fingerprint_fields.get("unreported_one_shot_digest"))
          and (first_event.fingerprint_fields.get("unreported_one_shot_digest")
               != second_event.fingerprint_fields.get("unreported_one_shot_digest")),
          str(first_event.fingerprint_fields))

    # The defect itself, against the same data: capping first is what collided.
    capped_first = sorted(item.identity for item in first)[:50]
    capped_second = sorted(item.identity for item in second)[:50]
    check("[defect] the capped representation the candidate hashed is the same value",
          capped_first == capped_second, str(len(capped_first)))


def test_the_same_membership_in_any_order_is_one_incident() -> None:
    """Identical complete sets deduplicate, whatever order they arrive in, and
    however many times a member is repeated."""
    items = [_one_shot(index) for index in range(2100, 2151)]
    shuffled = list(reversed(items[25:])) + list(items[:25])
    duplicated = shuffled + [items[7], items[7], items[40]]
    check("reordering the same members does not change the identity",
          _truncation(items).fingerprint() == _truncation(shuffled).fingerprint())
    check("nor does repeating members inside one cycle",
          _truncation(items).fingerprint() == _truncation(duplicated).fingerprint())
    check("and the count stays the true membership count, not the arrival count",
          _truncation(duplicated).affected_record_count == 51,
          str(_truncation(duplicated).affected_record_count))
    check("removing one member does change it",
          _truncation(items).fingerprint() != _truncation(items[:-1]).fingerprint())


def test_display_stays_capped_while_identity_stays_complete() -> None:
    """Criterion 3: bounding the payload and identifying the condition are
    separate concerns, and a large one-shot cycle cannot create a huge incident."""
    import json

    big = [_one_shot(index) for index in range(3000, 8000)]
    event = _truncation(big)
    payload = event.sanitized_payload()
    check("the exact total count is truthful",
          payload["affected_record_count"] == 5000
          and payload["details"]["unreported_one_shot_count"] == 5000,
          str(payload["affected_record_count"]))
    check("the human-readable evidence stays bounded",
          len(payload["evidence"]["unreported"]) <= sig.MAX_LISTED_UNREPORTED_ONE_SHOT,
          str(len(payload["evidence"]["unreported"])))
    check("and says it is a truncated view",
          payload["details"]["truncated_list"] is True, str(payload["details"]))
    check("the durable identity is two scalars, not a serialized set",
          set(payload["fingerprint_fields"]) ==
          {"unreported_one_shot_digest", "unreported_one_shot_count"},
          str(payload["fingerprint_fields"]))
    size = len(json.dumps(payload, ensure_ascii=False, default=str))
    check("a 5 000-input one-shot cycle still persists a small payload",
          size < 40_000, f"{size} bytes")
    check("the identity still distinguishes it from one member fewer",
          event.fingerprint() != _truncation(big[:-1]).fingerprint())

    # Same digest, computed the streaming way, is order-independent at scale.
    identities = sig.canonical_one_shot_identities(big)
    check("the canonical projection is sorted, deduplicated and complete",
          identities == sorted(set(identities)) and len(identities) == 5000,
          str(len(identities)))
    check("the digest is a function of that projection alone",
          sig.one_shot_membership_digest(identities)
          == event.fingerprint_fields.get("unreported_one_shot_digest"))


def test_a_different_reason_on_the_same_files_is_a_different_truncation() -> None:
    """The membership is `(raw_file_id, stage, reason_code)`, so the same files
    blocked for a different reason are a different condition."""
    files = list(range(2200, 2260))
    same_files_new_reason = [_one_shot(index, "schema_not_ready") for index in files]
    original = [_one_shot(index) for index in files]
    check("a changed blocking reason changes the truncation identity",
          _truncation(original).fingerprint() != _truncation(same_files_new_reason).fingerprint())


# --------------------------------------------------------------------------- #
# The mailbox-inspection invariant
# --------------------------------------------------------------------------- #

def test_healthy_no_work_stays_healthy() -> None:
    result = _result_with([])
    settled = job._finish_or_raise(result)
    check("an executed cycle with an empty mailbox is SUCCEEDED_NO_WORK",
          settled.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK, settled.outcome.value)


def test_an_uninspected_mailbox_cannot_be_healthy_no_work() -> None:
    result = _result_with([])
    result.stage1.result = Stage1BatchResult(mailbox_check_completed=False)
    try:
        job._finish_or_raise(result)
        check("an uninspected mailbox cannot report healthy no-work", False, "no error raised")
    except job.WorkflowBOrchestrationError as exc:
        settled = exc.partial_result
        check("an uninspected mailbox cannot report healthy no-work",
              settled.outcome == job.WorkflowBOutcome.FAILED_RETRYABLE, settled.outcome.value)
        check("the failure names the mailbox check",
              settled.stage1.error_category == "mailbox_check_not_completed",
              str(settled.stage1.error_category))


def test_a_review_cycle_is_still_a_review_cycle() -> None:
    """Granularity, not severity: one stranded file must not fail the whole run."""
    settled = job._finish_or_raise(_result_with([_stranded_stage2_item(14)]))
    check("a stranded input still ends the cycle as SUCCEEDED_WITH_REVIEW_ITEMS",
          settled.outcome == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS,
          settled.outcome.value)


# --------------------------------------------------------------------------- #
# Blocker 2 — the fail-closed condition, and how narrow it is
# --------------------------------------------------------------------------- #

def test_an_unrepresented_one_shot_input_cannot_end_as_a_success() -> None:
    """Blocker 2, criteria 5 and 6, at the outcome boundary.

    The state that must be impossible: a one-shot unresolved input was observed,
    neither its own incident nor the aggregate that stands in for it exists, and
    the cycle nevertheless settles as SUCCEEDED / SUCCEEDED_NO_WORK /
    SUCCEEDED_WITH_REVIEW_ITEMS. It is the *only* signalling condition allowed
    to change a verdict.
    """
    forbidden = {job.WorkflowBOutcome.SUCCEEDED, job.WorkflowBOutcome.SUCCEEDED_NO_WORK,
                 job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS}

    for label, build in (
        ("an otherwise healthy no-work cycle", lambda: _result_with([])),
        ("an otherwise review cycle", lambda: _result_with([_stranded_stage2_item(950)])),
    ):
        result = build()
        result.unresolved_signal_safety_failure = True
        try:
            settled = job._finish_or_raise(result)
            check(f"{label} with unrepresented one-shot work cannot settle", False,
                  settled.outcome.value)
        except job.WorkflowBOrchestrationError as exc:
            settled = exc.partial_result
            check(f"{label} with unrepresented one-shot work fails closed",
                  settled.outcome == job.WorkflowBOutcome.FAILED_NON_RETRYABLE
                  and settled.outcome not in forbidden, settled.outcome.value)
            check(f"{label} names the cause in the exception text",
                  "one_shot_unresolved_input_without_durable_signal" in str(exc), str(exc))
            check(f"{label} carries the cause into the operator incident",
                  exc.operational_incident_details.get("unresolved_signal_safety_failure") is True,
                  str(exc.operational_incident_details))
            check(f"{label} records the failure in the terminal payload",
                  settled.to_dict()["unresolved_signal_safety_failure"] is True,
                  str(settled.to_dict()["unresolved_signal_safety_failure"]))


def test_ordinary_signalling_and_review_conditions_are_not_global_failures() -> None:
    """Criterion 6. Granularity, not severity, everywhere else."""
    review = _result_with([_stranded_stage2_item(951)])
    review.unresolved_input_signal = sig.UnresolvedInputSignalResult(
        unresolved_input_count=1, incidents_opened=1)
    settled = job._finish_or_raise(review)
    check("a represented review item is still SUCCEEDED_WITH_REVIEW_ITEMS",
          settled.outcome == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS,
          settled.outcome.value)

    # A failed *replayable* incident is not a safety failure: the next cycle
    # rediscovers the same durable row and tries again.
    deferred = _result_with([_stranded_stage2_item(952)])
    deferred.unresolved_input_signal = sig.UnresolvedInputSignalResult(
        unresolved_input_count=1, incidents_deferred=1, report_failures=1)
    check("a deferred or failed replayable incident does not fail the cycle",
          deferred.unresolved_input_signal.safety_contract_violated is False
          and job._finish_or_raise(deferred).outcome
          == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS)

    # One-shot work that *is* durably represented, individually or in aggregate.
    for label, signal in (
        ("individually", sig.UnresolvedInputSignalResult(
            one_shot_input_count=1, one_shot_incidents_opened=1)),
        ("in aggregate", sig.UnresolvedInputSignalResult(
            one_shot_input_count=1, one_shot_unreported=1,
            truncation_incident_reported=True, truncation_incident_durable=True)),
    ):
        check(f"one-shot work represented {label} is not a safety failure",
              signal.safety_contract_violated is False, signal.to_dict())

    unrepresented = sig.UnresolvedInputSignalResult(
        one_shot_input_count=1, one_shot_unreported=1,
        truncation_incident_reported=False, one_shot_without_durable_evidence=1)
    check("only the unrepresented case violates the contract",
          unrepresented.safety_contract_violated is True, unrepresented.to_dict())


def test_a_stage_failure_keeps_its_own_cause() -> None:
    """The fail-closed branch is below the stage failures: a cycle that already
    failed for a stage reason stays diagnosable as that."""
    result = _result_with([])
    result.stage2.started = True
    result.stage2.unexpected_failure = True
    result.stage2.exception = RuntimeError("stage 2 exploded")
    result.stage2.exception_type = "RuntimeError"
    result.unresolved_signal_safety_failure = True
    try:
        job._finish_or_raise(result)
        check("a stage failure keeps its own cause", False, "no error raised")
    except job.WorkflowBOrchestrationError as exc:
        check("a stage failure keeps its own cause",
              exc.partial_result.outcome == job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR,
              exc.partial_result.outcome.value)
        check("and the safety failure is still recorded for the operator",
              exc.operational_incident_details.get("unresolved_signal_safety_failure") is True,
              str(exc.operational_incident_details))


def test_the_signalling_pass_is_the_only_thing_that_sets_the_flag() -> None:
    """A result nobody signalled over cannot be a safety failure by default."""
    check("a fresh batch result defaults to no safety failure",
          job.WorkflowBBatchResult().unresolved_signal_safety_failure is False)
    check("and a healthy cycle settles normally",
          job._finish_or_raise(_result_with([])).outcome
          == job.WorkflowBOutcome.SUCCEEDED_NO_WORK)


def test_one_shot_observation_is_answerable_without_a_database() -> None:
    """`_finish_cycle` needs this on the path where signalling could not run."""
    check("a healthy cycle observed no one-shot work",
          sig.one_shot_unresolved_input_count(_result_with([])) == 0)
    check("a replayable Stage 3 backlog alone is not one-shot work",
          sig.one_shot_unresolved_input_count(
              _replayable_backlog(range(960, 965))) == 0)
    check("a Stage 2 backlog is one-shot work, so the fail-safe path must see it",
          sig.one_shot_unresolved_input_count(
              _result_with([_stranded_stage2_item(index) for index in range(960, 965)])) == 5)
    check("a postprocess failure is",
          sig.one_shot_unresolved_input_count(_result_with([], [_postprocessor(
              966, job.WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE,
              operator=True, category="ambiguous_enrichment_match")])) == 1)


# --------------------------------------------------------------------------- #
# The sweep's own page limit must not read as coverage
# --------------------------------------------------------------------------- #

def _unowned_rows(count: int) -> list[dict]:
    """`count` durably unowned rows, oldest first, each with its keyset cursor."""
    return [
        {"raw_file_id": raw_id(500 + index), "stage2_status": "OK", "client_code": None,
         "stage2_report_type": "report_112", "stage2_pending_reason": None,
         "stage2_outcome_category": None, "stage2_cleaned_artifact_id": None,
         "source_identity": None,
         "stage2_updated_at": NOW + timedelta(minutes=index),
         "sweep_cursor_at": NOW + timedelta(minutes=index)}
        for index in range(count)
    ]


def _paged_sweep(rows: list[dict]):
    """The real keyset contract of `stage2_unrouted_files`, in memory.

    Same total order, same exclusive cursor, same short-final-page signal. The
    PostgreSQL half asserts the SQL implements this; this lets the pagination
    *loop* be falsified without a database.
    """
    def fake(_conn, *, limit: int = 200, after=None) -> list[dict]:
        start_index = 0
        if after is not None:
            keys = [(row["sweep_cursor_at"], row["raw_file_id"]) for row in rows]
            start_index = keys.index(after) + 1
        return rows[start_index:start_index + limit]
    return fake


class _SweepConn:
    def close(self): pass
    def rollback(self): pass


def _reconcile_over(rows: list[dict], *, page_size: int, max_pages: int = 50):
    from jobs.reports.stage2 import job_stage2 as s2

    result = Stage2BatchResult()
    original = s2.stage2_unrouted_files
    s2.stage2_unrouted_files = _paged_sweep(rows)
    try:
        sweep = s2.reconcile_unrouted_stage2_files(
            _SweepConn(), result, page_size=page_size, max_pages=max_pages
        )
    finally:
        s2.stage2_unrouted_files = original
    return sweep, result


def test_a_persistent_backlog_cannot_hide_newer_unresolved_inputs() -> None:
    """Blocker 3. Oldest-first + one fixed page = permanent starvation.

    250 durably unowned rows, none of which ever resolves. Under the reviewed
    candidate the cycle read the oldest page and stopped, so rows 201+ were never
    inspected on this cycle or any later one. The contrast below is the same data
    through the old single-page read, so the regression fails against the old
    behaviour rather than asserting the new one in isolation.
    """
    from jobs.reports.stage2 import job_stage2 as s2

    rows = _unowned_rows(250)
    sweep, result = _reconcile_over(rows, page_size=100)
    seen = [item.raw_file_id for item in result.items]
    check("every unowned file is inspected, not just the oldest page",
          sweep.rows_inspected == 250 and len(seen) == 250,
          f"inspected={sweep.rows_inspected} items={len(seen)}")
    check("the sweep exhausted the set rather than being cut short",
          sweep.truncated is False and sweep.pages_read == 3,
          f"truncated={sweep.truncated} pages={sweep.pages_read}")
    check("the newest unowned file gets its own review item",
          rows[-1]["raw_file_id"] in seen)
    check("no file is inspected twice across pages", len(set(seen)) == len(seen))
    check("every file carries an actionable unresolved outcome",
          all(item.outcome == Stage2Outcome.STRANDED_UNROUTABLE and item.review_required
              for item in result.items))

    # The defect, against the same data: one oldest-first page and nothing else.
    original = s2.stage2_unrouted_files
    s2.stage2_unrouted_files = _paged_sweep(rows)
    try:
        legacy_page = s2.stage2_unrouted_files(_SweepConn(), limit=100)
    finally:
        s2.stage2_unrouted_files = original
    legacy_ids = {row["raw_file_id"] for row in legacy_page}
    check("[defect] a single oldest-first page really does miss the newer files",
          rows[-1]["raw_file_id"] not in legacy_ids and len(legacy_ids) == 100,
          str(len(legacy_ids)))

    # And the newly unresolved input is independently actionable through the
    # incident path, which is what the owner contract actually asks for.
    signal_items = sig.collect_unresolved_inputs(_result_with(list(result.items)))
    check("the newest unowned file reaches the per-input signal too",
          any(item.raw_file_id == rows[-1]["raw_file_id"] for item in signal_items),
          str(len(signal_items)))


def test_the_sweep_backstop_is_announced_never_absorbed() -> None:
    """The page-count backstop is a resource bound, and it says so out loud."""
    sweep, result = _reconcile_over(_unowned_rows(250), page_size=100, max_pages=1)
    check("the backstop stops the sweep",
          sweep.truncated is True and sweep.rows_inspected == 100,
          f"truncated={sweep.truncated} inspected={sweep.rows_inspected}")
    check("what it did inspect is still reported", len(result.items) == 100)

    warning = "Stage 2 unrouted sweep hit its page-count backstop; some unowned files were not inspected"
    check("hitting the backstop is logged as a bound, not as coverage",
          warning in _sweep_cycle(250, page_size=100, max_pages=1).messages())
    check("an exhausted sweep stays silent",
          warning not in _sweep_cycle(250, page_size=100).messages())
    check("an empty sweep stays silent",
          warning not in _sweep_cycle(0, page_size=100).messages())


def _sweep_cycle(row_count: int, *, page_size: int = 200, max_pages: int = 50) -> "Client":
    """One autonomous Stage 2 batch whose unrouted sweep sees `row_count` rows."""
    from jobs.reports.stage2 import job_stage2 as s2

    rows = _unowned_rows(row_count)
    client = Client()
    real_reconcile = s2.reconcile_unrouted_stage2_files
    originals = (s2._pg_conn, s2._candidate_rows, s2.stage2_unrouted_files,
                 s2.reconcile_unrouted_stage2_files)
    s2._pg_conn = lambda: _SweepConn()
    s2._candidate_rows = lambda *_a, **_k: []
    s2.stage2_unrouted_files = _paged_sweep(rows)
    s2.reconcile_unrouted_stage2_files = (
        lambda conn, result, **_kw: real_reconcile(
            conn, result, page_size=page_size, max_pages=max_pages)
    )
    try:
        s2.process_stage2_batch(client, RUN_ID, {})
    except Exception:
        # `has_batch_failures` raises on review items; the log is what matters here.
        pass
    finally:
        (s2._pg_conn, s2._candidate_rows, s2.stage2_unrouted_files,
         s2.reconcile_unrouted_stage2_files) = originals
    return client


# --------------------------------------------------------------------------- #
# The incident lifecycle, against a real PostgreSQL
# --------------------------------------------------------------------------- #

PLATFORM_BOOTSTRAP = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS runs (
  run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
  status TEXT NOT NULL, trigger TEXT NOT NULL, source TEXT NOT NULL, actor TEXT,
  params JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS logs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, level TEXT NOT NULL, type TEXT NOT NULL,
  source TEXT NOT NULL, run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
  message TEXT NOT NULL, context JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT
);
CREATE SCHEMA IF NOT EXISTS ingest;
-- Wide enough for the *real* Stage 2 code paths, not only for the signalling
-- pass: `_candidate_rows`, `_persist_stage2` and `stage2_unrouted_files` are
-- executed verbatim by the mutation-path regression below, so every column and
-- table they name has to exist here. The signalling tests insert an explicit
-- column list, so the extra nullable columns cost them nothing.
CREATE TABLE IF NOT EXISTS ingest.raw_file (
  id UUID PRIMARY KEY, status TEXT NOT NULL, client_code TEXT,
  stage2_status TEXT, stage2_report_type TEXT, stage3_status TEXT,
  normalized_csv_path TEXT, original_filename TEXT, sha256 TEXT, raw_path TEXT,
  stage2_pending_reason TEXT, stage2_outcome_category TEXT, stage2_retryable BOOLEAN,
  stage2_cleaned_artifact_id UUID, stage2_scores JSONB, stage2_schema_diff JSONB,
  stage2_updated_at TIMESTAMPTZ,
  stage3_started_at TIMESTAMPTZ, stage3_finished_at TIMESTAMPTZ, stage3_error TEXT,
  stage3_destination_schema TEXT, stage3_destination_table TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  raw_file_id UUID, workflow_name TEXT, stage_name TEXT, artifact_role TEXT
);
"""

MIGRATION = REPO_ROOT / "db" / "migrations" / "052_suspected_bug_incidents_and_email_outbox.sql"


def _configure_platform_env(dsn: str) -> None:
    parsed = urlparse(dsn)
    os.environ.update({
        "POSTGRES_HOST": parsed.hostname or "127.0.0.1",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_DB": (parsed.path or "/").lstrip("/"),
        "POSTGRES_USER": parsed.username or "",
        "POSTGRES_PASSWORD": parsed.password or "",
        "LOG_PLATFORM_TARGET_ENVIRONMENT": "local_dev",
        "SUSPECTED_BUG_ALERT_TO": "platform-alerts@example.com",
        "SUSPECTED_BUG_ALERT_COOLDOWN_MINUTES": "120",
        "SUSPECTED_BUG_ALERT_REMINDER_HOURS": "24",
        # Nothing can be delivered from this test.
        "AUTOMATION_SMTP_HOST": "",
    })


def _bootstrap(conn, raw_files: list[tuple[str, str, str | None]]) -> None:
    with conn.cursor() as cur:
        cur.execute(PLATFORM_BOOTSTRAP)
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        cur.execute("TRUNCATE suspected_bug_email_outbox, suspected_bug_occurrences, "
                    "suspected_bug_incidents RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM logs")
        cur.execute("DELETE FROM artifacts")
        cur.execute("DELETE FROM ingest.raw_file")
        cur.execute("DELETE FROM runs")
        cur.execute("INSERT INTO runs(run_id, started_at, status, trigger, source) "
                    "VALUES (%s, now(), 'RUNNING', 'SCHEDULED', "
                    "'jobs.reports.workflow_b.orchestrator')", (RUN_ID,))
        for file_id, status, stage3_status in raw_files:
            cur.execute(
                "INSERT INTO ingest.raw_file(id, status, stage2_status, stage2_report_type, "
                "stage3_status) VALUES (%s, %s, 'OK', 'report_112', %s)",
                (file_id, status, stage3_status),
            )
    conn.commit()


def _incident_states(conn) -> dict[str, tuple[str, str]]:
    """raw_file_id -> (state, reason). One input can carry a resolved incident for
    a superseded reason and an open one for the current reason, so the open row
    wins: it is the one an operator still has to act on."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT latest_payload ->> 'raw_file_id' AS raw_file_id, state, "
            "       latest_payload -> 'details' ->> 'reason_code' AS reason_code "
            "FROM suspected_bug_incidents WHERE incident_code = %s "
            "ORDER BY (state = 'open') ASC, last_seen_at ASC",
            (sig.INCIDENT_CODE,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return {str(row["raw_file_id"]): (row["state"], row["reason_code"]) for row in rows}


def _outbox_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM suspected_bug_email_outbox")
        value = int(cur.fetchone()["n"])
    conn.rollback()
    return value


def run_postgres_suite(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    _configure_platform_env(dsn)
    backlog = [raw_id(index) for index in range(101, 104)]

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # --- an existing backlog, first activation ------------------------- #
        _bootstrap(conn, [(file_id, "NORMALIZED", None) for file_id in backlog])
        client = Client()
        backlog_result = _result_with([_stranded_stage2_item(index) for index in range(101, 104)])
        first = sig.signal_unresolved_inputs(client, conn, RUN_ID, backlog_result)
        check("[pg] the whole backlog gets an incident",
              first.incidents_opened == 3, first.to_dict())
        check("[pg] each backlog file has its own open incident",
              set(_incident_states(conn)) == set(backlog), str(sorted(_incident_states(conn))))
        check("[pg] the backlog is announced once, per input",
              _outbox_count(conn) == 3, str(_outbox_count(conn)))

        # --- rescanning the same unchanged backlog ------------------------- #
        outbox_before = _outbox_count(conn)
        second = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, backlog_result)
        check("[pg] a rescan opens no new incident",
              second.incidents_opened == 0, second.to_dict())
        check("[pg] a rescan recognises the whole backlog as already open",
              second.already_open_count == 3, second.to_dict())
        check("[pg] a rescan enqueues no duplicate alert",
              _outbox_count(conn) == outbox_before, str(_outbox_count(conn)))

        # --- a NEW problem while the backlog is open ----------------------- #
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ingest.raw_file(id, status, stage2_status, "
                        "stage2_report_type) VALUES (%s, 'NORMALIZED', 'OK', 'report_112')",
                        (raw_id(104),))
        conn.commit()
        grown = _result_with([_stranded_stage2_item(index) for index in range(101, 105)])
        third = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, grown)
        check("[pg] a newly stranded input opens exactly one incident even with a backlog open",
              third.incidents_opened == 1, third.to_dict())
        check("[pg] the new input is the one that alerted",
              _outbox_count(conn) == outbox_before + 1, str(_outbox_count(conn)))
        check("[pg] the new input has its own open incident",
              _incident_states(conn).get(raw_id(104), ("", ""))[0] == "open",
              str(_incident_states(conn).get(raw_id(104))))

        # --- resolution when an input becomes terminal --------------------- #
        with conn.cursor() as cur:
            cur.execute("UPDATE ingest.raw_file SET client_code = 'ALPHA00001', "
                        "stage3_status = 'OK' WHERE id = %s", (backlog[0],))
        conn.commit()
        recovered = _result_with([_stranded_stage2_item(index) for index in range(102, 105)])
        fourth = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, recovered)
        check("[pg] a repaired input closes its incident",
              fourth.incidents_resolved == 1, fourth.to_dict())
        check("[pg] the repaired input's incident is resolved",
              _incident_states(conn)[backlog[0]][0] == "resolved",
              str(_incident_states(conn)[backlog[0]]))
        check("[pg] the still-blocked inputs keep their incidents open",
              all(_incident_states(conn)[file_id][0] == "open" for file_id in backlog[1:]),
              str(_incident_states(conn)))

        # --- a truncated cycle must never look like a resolution ----------- #
        open_before = sum(state == "open" for state, _ in _incident_states(conn).values())
        blind = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, _result_with([]))
        check("[pg] a cycle that examined nothing resolves nothing",
              blind.incidents_resolved == 0, blind.to_dict())
        check("[pg] the open incidents survive a cycle that saw none of them",
              sum(state == "open" for state, _ in _incident_states(conn).values()) == open_before,
              str(_incident_states(conn)))

        # --- a changed blocking reason supersedes the old incident --------- #
        changed = _result_with([
            _stranded_stage2_item(102, "missing_stage2_report_type"),
            _stranded_stage2_item(103),
            _stranded_stage2_item(104),
        ])
        fifth = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, changed)
        check("[pg] a changed blocking reason opens a new incident",
              fifth.incidents_opened == 1, fifth.to_dict())
        check("[pg] the superseded reason is closed rather than left open forever",
              fifth.incidents_resolved == 1, fifth.to_dict())
        check("[pg] the input now reports the current blocking reason",
              _incident_states(conn)[backlog[1]] == ("open", "missing_stage2_report_type"),
              str(_incident_states(conn)[backlog[1]]))

        # --- the per-cycle intake is bounded, and says so ------------------ #
        # Written against the replayable class, which after the Stage 2
        # refutation is Stage 3 alone: only that budget genuinely *defers*, and
        # only for it is "the next cycle picks this up" a claim the repository
        # can still support.
        _bootstrap(conn, [(raw_id(index), "NORMALIZED", None) for index in range(201, 208)])
        os.environ[sig.MAX_NEW_INCIDENTS_ENV] = "3"
        try:
            client = Client()
            large = _replayable_backlog(range(201, 208))
            bounded = sig.signal_unresolved_inputs(client, conn, RUN_ID, large)
            check("[pg] one cycle opens at most the configured number of new incidents",
                  bounded.incidents_opened == 3, bounded.to_dict())
            check("[pg] the bound reports what it deferred",
                  bounded.incidents_deferred == 4, bounded.to_dict())
            check("[pg] a truncated intake is logged, never silent",
                  "Workflow B deferred unresolved-input incidents to the next cycle"
                  in client.messages(), str(client.messages()))
            drained = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, large)
            check("[pg] the next cycle picks up what the bound deferred",
                  drained.incidents_opened == 3 and drained.incidents_deferred == 1,
                  drained.to_dict())
        finally:
            os.environ.pop(sig.MAX_NEW_INCIDENTS_ENV, None)

        # --- the orchestrator actually runs the pass ----------------------- #
        # Without this the whole signal could be silently dead: `_finish_cycle`
        # deliberately swallows its own failures so the alert path can never
        # fail a healthy cycle, which also means a broken wiring would look
        # exactly like a clean run.
        _bootstrap(conn, [(raw_id(401), "NORMALIZED", None)])
        _run_orchestrator_against(dsn, psycopg, dict_row)
        opened = _incident_states(conn)
        check("[pg] a full orchestrator cycle raises the per-input incident itself",
              set(opened) == {raw_id(401)} and opened[raw_id(401)][0] == "open", str(opened))

        # --- the five blocking review findings ----------------------------- #
        test_pg_blocker1_stage3_ok_cannot_resolve_a_postprocess_incident(conn)
        test_pg_blocker2_supersession_waits_for_a_durable_replacement(conn)
        test_pg_blocker4_a_failing_head_cannot_starve_the_queue(conn)
        test_pg_blocker5_signalling_and_unlock_faults_cannot_replace_the_outcome(
            conn, dsn, psycopg, dict_row)

        # --- the three blocking findings of the final review --------------- #
        test_pg_blocker1_two_large_populations_are_two_durable_incidents(conn)
        test_pg_blocker2_both_persistence_paths_failing_is_a_safety_failure(conn)
        test_pg_blocker2_the_failed_safety_pass_fails_the_whole_cycle(dsn, psycopg, dict_row)

        # --- the Stage 2 refutation and its operational consequence -------- #
        test_pg_stage2_targeted_reprocessing_breaks_sweep_replayability(conn)
        test_pg_stage2_backlog_within_capacity_is_individually_represented(conn)
        test_pg_stage2_backlog_beyond_capacity_is_still_durably_represented(conn)
        test_pg_stage2_persistence_failures_fall_back_then_fail_closed(conn)

        # --- the production one-shot starvation defect --------------------- #
        test_pg_production_one_shot_cannot_be_starved_by_a_replayable_backlog(conn)
        test_pg_many_one_shot_failures_stay_bounded_and_durable(conn)
        test_pg_a_replayable_backlog_alone_never_truncates(conn)
        test_pg_a_repeated_one_shot_condition_does_not_re_alert(conn)

        # --- one bad input does not stop the others ------------------------ #
        _bootstrap(conn, [(raw_id(301), "NORMALIZED", None), (raw_id(302), "NORMALIZED", "OK")])
        mixed = _result_with([_stranded_stage2_item(301)])
        mixed.stage3.result = Stage3BatchResult(items=[Stage3ItemResult(
            raw_file_id=raw_id(302), client_code="ALPHA00001", report_type="report_207",
            outcome=Stage3Outcome.LOADED, persisted_status="OK",
        )])
        partial = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, mixed)
        check("[pg] a successfully loaded input raises no incident alongside a blocked one",
              partial.incidents_opened == 1 and set(_incident_states(conn)) == {raw_id(301)},
              str(_incident_states(conn)))


# --------------------------------------------------------------------------- #
# The five blocking review findings, each against the real incident table
# --------------------------------------------------------------------------- #

def _incident_rows(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT incident_id::text AS incident_id, state, "
            "       latest_payload ->> 'raw_file_id' AS raw_file_id, "
            "       latest_payload -> 'fingerprint_fields' ->> 'stage' AS stage, "
            "       latest_payload -> 'details' ->> 'reason_code' AS reason_code "
            "FROM suspected_bug_incidents WHERE incident_code = %s "
            "ORDER BY first_seen_at ASC, incident_id ASC",
            (sig.INCIDENT_CODE,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return rows


def _open_for(conn, raw_file_id: str) -> list[dict]:
    return [row for row in _incident_rows(conn)
            if row["raw_file_id"] == raw_file_id and row["state"] == "open"]


def _with_failing_report(fingerprints: set[str]):
    """Make persistence fail for exactly these fingerprints, nothing else."""
    from api.suspected_bug import SuspectedBugReportResult

    real = sig.safe_report_suspected_bug

    def fake(event, **kwargs):
        fingerprint = event.fingerprint()
        if fingerprint in fingerprints:
            return SuspectedBugReportResult(
                fingerprint=fingerprint, error="injected: incident persistence refused")
        return real(event, **kwargs)
    return fake


def _everything_replayable(real):
    """The deployed algorithm: one queue, one budget, everything "replayable".

    Injected at collection rather than through a stage table, because
    replayability is no longer a stage-level property — which is the fix under
    test. This reproduces the released behaviour so the assertions below are
    falsifying rather than confirmatory.
    """
    def fake(result):
        return [dataclasses.replace(item, replayable=True) for item in real(result)]
    return fake


def _fingerprint_of(item_index: int, reason: str) -> str:
    """The fingerprint the module will compute for this stranded stage-2 input."""
    config = sig.load_alert_config()
    return sig.build_event(
        sig.collect_unresolved_inputs(
            _result_with([_stranded_stage2_item(item_index, reason)]))[0],
        run_id=RUN_ID, environment=config.environment, occurred_at=NOW,
    ).fingerprint()


def _fingerprint_of_stage3(item_index: int, reason: str) -> str:
    """The same, for the replayable Stage 3 shape the budget tests now use."""
    config = sig.load_alert_config()
    return sig.build_event(
        sig.collect_unresolved_inputs(
            _result_with([], stage3_items=[_replayable_stage3_item(item_index, reason)]))[0],
        run_id=RUN_ID, environment=config.environment, occurred_at=NOW,
    ).fingerprint()


def test_pg_blocker1_stage3_ok_cannot_resolve_a_postprocess_incident(conn) -> None:
    """Blocker 1. `stage3_status='OK'` is proof about the load, and only that.

    The reproduced sequence: Stage 3 succeeds, the postprocessor fails
    non-retryably, and every later cycle carries no postprocessor result at all
    because Stage 3 has nothing left to offer. The reviewed candidate read the
    Stage 3 `OK` on the raw file and closed the postprocessor incident while the
    work was still owed.
    """
    outcomes = job.WorkflowBPostprocessorOutcome
    file_id = raw_id(601)
    _bootstrap(conn, [(file_id, "NORMALIZED", "OK")])

    blocked = _result_with([], [_postprocessor(
        601, outcomes.FAILED_NON_RETRYABLE, operator=True,
        category="runtime_schema_mutation_disabled")])
    first = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, blocked)
    rows = _incident_rows(conn)
    check("[pg] a non-retryable postprocessor block opens its own incident",
          first.incidents_opened == 1 and len(rows) == 1, first.to_dict())
    check("[pg] the incident records the postprocess stage",
          rows and rows[0]["stage"] == "postprocess", str(rows))

    # Every later cycle: Stage 3 stays OK and no postprocessor runs at all.
    for cycle in range(2):
        quiet = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, _result_with([]))
        check(f"[pg] Stage 3 OK resolves no postprocess incident (cycle {cycle + 1})",
              quiet.incidents_resolved == 0, quiet.to_dict())
    check("[pg] the postprocess incident is still open while the work is owed",
          len(_open_for(conn, file_id)) == 1, str(_incident_rows(conn)))

    # Ambiguous evidence must not close it either.
    retry = _result_with([], [_postprocessor(601, outcomes.FAILED_RETRYABLE, retryable=True)])
    ambiguous = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, retry)
    check("[pg] a retryable postprocessor failure is not recovery",
          ambiguous.incidents_resolved == 0 and len(_open_for(conn, file_id)) == 1,
          ambiguous.to_dict())

    other = _result_with([], [_postprocessor(602, outcomes.SUCCEEDED)])
    unrelated = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, other)
    check("[pg] another input's postprocessor success is not this input's proof",
          unrelated.incidents_resolved == 0 and len(_open_for(conn, file_id)) == 1,
          unrelated.to_dict())

    # The one thing that is proof.
    done = _result_with([], [_postprocessor(601, outcomes.SUCCEEDED)])
    recovered = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, done)
    check("[pg] an observed postprocessor completion resolves the incident",
          recovered.incidents_resolved == 1, recovered.to_dict())
    check("[pg] and the input then carries no open incident",
          _open_for(conn, file_id) == [], str(_incident_rows(conn)))

    # A load-stage incident on the same evidence still resolves: the fix is
    # stage-appropriate proof, not a blanket refusal to resolve anything.
    _bootstrap(conn, [(raw_id(603), "NORMALIZED", None)])
    stage2_open = sig.signal_unresolved_inputs(
        Client(), conn, RUN_ID, _result_with([_stranded_stage2_item(603)]))
    with conn.cursor() as cur:
        cur.execute("UPDATE ingest.raw_file SET stage3_status = 'OK' WHERE id = %s", (raw_id(603),))
    conn.commit()
    stage2_closed = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, _result_with([]))
    check("[pg] a stage2 incident still resolves on a completed load",
          stage2_open.incidents_opened == 1 and stage2_closed.incidents_resolved == 1,
          f"{stage2_open.to_dict()} {stage2_closed.to_dict()}")


def test_pg_blocker2_supersession_waits_for_a_durable_replacement(conn) -> None:
    """Blocker 2. A changed reason may close the old incident only after the new
    one exists durably — never on the mere presence of a different fingerprint.
    """
    first_id, second_id = raw_id(701), raw_id(702)
    _bootstrap(conn, [(first_id, "NORMALIZED", None), (second_id, "NORMALIZED", None)])

    original = _result_with([], stage3_items=[
        _replayable_stage3_item(701), _replayable_stage3_item(702)])
    opened = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, original)
    check("[pg] both inputs start with an open incident for reason A",
          opened.incidents_opened == 2, opened.to_dict())

    # Reason changes for both, but capacity admits only one replacement.
    changed = _result_with([], stage3_items=[
        _replayable_stage3_item(701, "missing_stage2_cleaned_artifact"),
        _replayable_stage3_item(702, "missing_stage2_cleaned_artifact"),
    ])
    os.environ[sig.MAX_NEW_INCIDENTS_ENV] = "1"
    try:
        bounded = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, changed)
    finally:
        os.environ.pop(sig.MAX_NEW_INCIDENTS_ENV, None)
    check("[pg] the bound admits one replacement and defers the other",
          bounded.incidents_opened == 1 and bounded.incidents_deferred == 1, bounded.to_dict())
    check("[pg] only the input whose replacement persisted is superseded",
          bounded.incidents_resolved == 1, bounded.to_dict())
    first_open = _open_for(conn, first_id)
    second_open = _open_for(conn, second_id)
    check("[pg] the persisted replacement carries the current reason",
          len(first_open) == 1
          and first_open[0]["reason_code"] == "missing_stage2_cleaned_artifact",
          str(first_open))
    check("[pg] a deferred replacement leaves its predecessor open",
          len(second_open) == 1 and second_open[0]["reason_code"] == "missing_report_policy",
          str(second_open))
    check("[pg] no input is ever left with zero open incidents",
          bool(first_open) and bool(second_open), str(_incident_rows(conn)))

    # Now let capacity through, but make the replacement's persistence fail.
    failing = _fingerprint_of_stage3(702, "missing_stage2_cleaned_artifact")
    real_report = sig.safe_report_suspected_bug
    sig.safe_report_suspected_bug = _with_failing_report({failing})
    try:
        broken = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, changed)
    finally:
        sig.safe_report_suspected_bug = real_report
    check("[pg] a replacement that cannot persist is reported as a failure",
          broken.report_failures == 1 and broken.incidents_opened == 0, broken.to_dict())
    check("[pg] and its predecessor is still not superseded",
          broken.incidents_resolved == 0, broken.to_dict())
    second_open = _open_for(conn, second_id)
    check("[pg] the predecessor survives a failed replacement",
          len(second_open) == 1 and second_open[0]["reason_code"] == "missing_report_policy",
          str(second_open))

    # And once the replacement really lands, supersession happens.
    healed = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, changed)
    check("[pg] a later successful replacement finally supersedes the predecessor",
          healed.incidents_opened == 1 and healed.incidents_resolved == 1, healed.to_dict())
    second_open = _open_for(conn, second_id)
    check("[pg] the input now carries exactly one open incident, for the new reason",
          len(second_open) == 1
          and second_open[0]["reason_code"] == "missing_stage2_cleaned_artifact",
          str(second_open))

    # An unchanged reason is still plain dedup: nothing opened, nothing closed.
    steady = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, changed)
    check("[pg] an unchanged reason opens and resolves nothing",
          steady.incidents_opened == 0 and steady.incidents_resolved == 0
          and steady.already_open_count == 2, steady.to_dict())


def test_pg_blocker4_a_failing_head_cannot_starve_the_queue(conn) -> None:
    """Blocker 4. The per-cycle bound is capacity for incidents *opened*.

    With the reviewed candidate the bound was spent by attempts, so a leading
    fingerprint whose persistence fails every cycle held the only slot forever
    and nothing behind it was ever tried.
    """
    files = [raw_id(800 + index) for index in range(1, 5)]
    _bootstrap(conn, [(file_id, "NORMALIZED", None) for file_id in files])
    batch = _replayable_backlog(range(801, 805))

    head = _fingerprint_of_stage3(801, "missing_report_policy")
    real_report = sig.safe_report_suspected_bug
    sig.safe_report_suspected_bug = _with_failing_report({head})
    os.environ[sig.MAX_NEW_INCIDENTS_ENV] = "1"
    try:
        cycles = [sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch) for _ in range(3)]
    finally:
        sig.safe_report_suspected_bug = real_report
        os.environ.pop(sig.MAX_NEW_INCIDENTS_ENV, None)

    check("[pg] the failing head is retried but never consumes the slot",
          all(cycle.report_failures == 1 for cycle in cycles),
          str([cycle.to_dict() for cycle in cycles]))
    check("[pg] a later pending input is attempted in the very same cycle",
          all(cycle.incidents_attempted == 2 for cycle in cycles),
          str([cycle.incidents_attempted for cycle in cycles]))
    check("[pg] the bound still admits exactly one incident per cycle",
          all(cycle.incidents_opened == 1 for cycle in cycles),
          str([cycle.incidents_opened for cycle in cycles]))
    opened_ids = {row["raw_file_id"] for row in _incident_rows(conn)}
    check("[pg] every input behind the failing head has its own incident",
          opened_ids == set(files[1:]), str(sorted(opened_ids)))
    check("[pg] the head itself is still unreported, and loudly so",
          files[0] not in opened_ids and cycles[-1].report_failures == 1,
          str(sorted(opened_ids)))

    # The bound must still drain a healthy finite backlog normally.
    healthy = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
    check("[pg] with persistence healthy the remaining input drains",
          healthy.incidents_opened == 1 and healthy.incidents_deferred == 0,
          healthy.to_dict())
    check("[pg] a stable backlog then reaches a quiet steady state",
          sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch).incidents_opened == 0)


def _all_incident_rows(conn) -> list[dict]:
    """Every incident of any code, so "some other alert covered it" is visible."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT incident_code, state, "
            "       latest_payload ->> 'raw_file_id' AS raw_file_id, "
            "       latest_payload -> 'fingerprint_fields' ->> 'stage' AS stage, "
            "       latest_payload -> 'evidence' -> 'unreported' AS unreported "
            "FROM suspected_bug_incidents ORDER BY first_seen_at ASC, incident_code ASC"
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return rows


def _one_shot_starvation_cycle(conn, *, backlog_size: int, replayable_cap: int,
                               one_shot_files: list[int], failing_fingerprints=None):
    """The live shape: a replayable backlog larger than the replayable budget,
    plus one-shot postprocess failures produced in the same cycle.

    The backlog is Stage 3 rather than Stage 2 now — the starvation defect is a
    property of two classes contending for one budget, not of Stage 2, and Stage
    3 is the class that still legitimately holds the replayable budget open.
    """
    outcomes = job.WorkflowBPostprocessorOutcome
    batch = _result_with(
        [],
        [_postprocessor(index, outcomes.FAILED_NON_RETRYABLE, operator=True,
                        category="ambiguous_enrichment_match") for index in one_shot_files],
        stage3_items=[_replayable_stage3_item(index)
                      for index in range(1001, 1001 + backlog_size)],
    )
    real_report = sig.safe_report_suspected_bug
    if failing_fingerprints:
        sig.safe_report_suspected_bug = _with_failing_report(failing_fingerprints)
    os.environ[sig.MAX_NEW_INCIDENTS_ENV] = str(replayable_cap)
    try:
        return sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch), batch
    finally:
        sig.safe_report_suspected_bug = real_report
        os.environ.pop(sig.MAX_NEW_INCIDENTS_ENV, None)


def _bootstrap_starvation(conn, *, backlog_size: int, one_shot_files: list[int]) -> None:
    """Backlog rows are pre-Stage-3; the one-shot rows already loaded (`OK`),
    which is exactly why no later cycle re-offers them to a postprocessor."""
    _bootstrap(conn, (
        [(raw_id(index), "NORMALIZED", None) for index in range(1001, 1001 + backlog_size)]
        + [(raw_id(index), "NORMALIZED", "OK") for index in one_shot_files]
    ))


def test_pg_production_one_shot_cannot_be_starved_by_a_replayable_backlog(conn) -> None:
    """The 2026-08-24 defect, reproduced and fixed.

    Production: 63 replayable Stage 2 inputs, a 20-slot budget, and an ALPHA
    `Alpha_GPS_Baza_LOG` file whose postprocessor failed
    `AMBIGUOUS_ENRICHMENT_MATCH` in the same cycle. The postprocess event was
    collected 64th, deferred behind the backlog, and by the next cycle it was
    not in the population at all — `unresolved_input_count` 64 -> 63 with
    `incidents_resolved: 0` and no `WORKFLOW_B_INPUT_UNRESOLVED` incident ever.

    Same shape here, scaled: five replayable inputs against a three-slot budget.
    """
    one_shot = 1101
    _bootstrap_starvation(conn, backlog_size=5, one_shot_files=[one_shot])

    # --- the deployed behaviour, reproduced ---------------------------------- #
    # Collapsing the two classes into one is exactly what the released code did:
    # a single queue in collection order (postprocess last) against a single
    # budget. Asserting the defect here is what makes the fix below meaningful.
    original_collect = sig.collect_unresolved_inputs
    sig.collect_unresolved_inputs = _everything_replayable(original_collect)
    try:
        deployed, _ = _one_shot_starvation_cycle(
            conn, backlog_size=5, replayable_cap=3, one_shot_files=[one_shot])
    finally:
        sig.collect_unresolved_inputs = original_collect
    check("[pg] the deployed single-budget behaviour starves the one-shot input",
          deployed.incidents_opened == 3 and _open_for(conn, raw_id(one_shot)) == [],
          f"{deployed.to_dict()} {_incident_rows(conn)}")
    check("[pg] and leaves no durable evidence that it was dropped",
          not any(row["incident_code"] == sig.TRUNCATED_INCIDENT_CODE
                  for row in _all_incident_rows(conn)),
          str(_all_incident_rows(conn)))

    # --- the fix ------------------------------------------------------------- #
    _bootstrap_starvation(conn, backlog_size=5, one_shot_files=[one_shot])
    first, _ = _one_shot_starvation_cycle(
        conn, backlog_size=5, replayable_cap=3, one_shot_files=[one_shot])
    one_shot_open = _open_for(conn, raw_id(one_shot))
    check("[pg] the one-shot input is reported in the cycle that observed it",
          len(one_shot_open) == 1, str(_incident_rows(conn)))
    check("[pg] its incident records the postprocess stage",
          [row["stage"] for row in one_shot_open] == ["postprocess"], str(one_shot_open))
    check("[pg] the signal counts the one-shot half separately",
          first.one_shot_input_count == 1 and first.one_shot_incidents_opened == 1
          and first.one_shot_unreported == 0, first.to_dict())
    check("[pg] the replayable budget is still exactly what it was",
          first.incidents_opened == 4 and first.incidents_deferred == 2, first.to_dict())

    # --- criterion 2: no domain-specific incident is doing the work ---------- #
    codes = {row["incident_code"] for row in _all_incident_rows(conn)}
    check("[pg] the generic mechanism alone carries the contract",
          codes == {sig.INCIDENT_CODE}, str(sorted(codes)))

    # --- the next natural cycle: the evidence is gone, the signal is not ----- #
    # Stage 3 is `OK`, so `_discover_postprocessor_plans` offers nothing and the
    # input is absent from the population. Its incident must survive that.
    following = sig.signal_unresolved_inputs(
        Client(), conn, RUN_ID, _replayable_backlog(range(1001, 1006)))
    check("[pg] the following cycle no longer sees the one-shot input at all",
          following.one_shot_input_count == 0, following.to_dict())
    check("[pg] Stage 3 OK does not resolve it",
          following.incidents_resolved == 0, following.to_dict())
    check("[pg] the input still has its durable actionable incident",
          len(_open_for(conn, raw_id(one_shot))) == 1, str(_incident_rows(conn)))
    check("[pg] and the replayable backlog kept draining behind it",
          following.incidents_opened == 2 and following.incidents_deferred == 0,
          following.to_dict())


def test_pg_many_one_shot_failures_stay_bounded_and_durable(conn) -> None:
    """More one-shot inputs than the one-shot budget, in one cycle.

    Fixing the single-item case by removing the bound would just move the storm.
    What must hold instead: individual incidents up to the bound, and *one*
    bounded incident naming everything that did not get one — because for a
    one-shot input "deferred" and "lost" are the same word.
    """
    one_shot = [1201, 1202, 1203, 1204]
    _bootstrap_starvation(conn, backlog_size=5, one_shot_files=one_shot)

    os.environ[sig.MAX_NEW_ONE_SHOT_INCIDENTS_ENV] = "2"
    try:
        bounded, _ = _one_shot_starvation_cycle(
            conn, backlog_size=5, replayable_cap=3, one_shot_files=one_shot)
    finally:
        os.environ.pop(sig.MAX_NEW_ONE_SHOT_INCIDENTS_ENV, None)

    check("[pg] the one-shot budget is respected",
          bounded.one_shot_incidents_opened == 2, bounded.to_dict())
    check("[pg] what it refused is counted, not absorbed",
          bounded.one_shot_unreported == 2 and bounded.truncation_incident_reported,
          bounded.to_dict())
    truncation = [row for row in _all_incident_rows(conn)
                  if row["incident_code"] == sig.TRUNCATED_INCIDENT_CODE]
    check("[pg] exactly one truncation incident is opened, never one per input",
          len(truncation) == 1, str(truncation))
    named = {entry["raw_file_id"] for row in truncation for entry in (row["unreported"] or [])}
    check("[pg] it names every input it stands in for",
          named == {raw_id(1203), raw_id(1204)}, str(sorted(named)))
    check("[pg] the whole cycle stays bounded: 2 one-shot + 3 replayable + 1 truncation",
          len(_all_incident_rows(conn)) == 6, str(_all_incident_rows(conn)))

    # A one-shot input whose own incident cannot persist is lost just as
    # permanently as a deferred one, so it belongs in the same evidence.
    _bootstrap_starvation(conn, backlog_size=5, one_shot_files=[1205])
    failing = sig.build_event(
        sig.UnresolvedInput(raw_file_id=raw_id(1205), stage="postprocess",
                            outcome="FAILED_NON_RETRYABLE",
                            reason_code="ambiguous_enrichment_match",
                            client_code="ALPHA00001", report_type="report_207"),
        run_id=RUN_ID, environment=sig.load_alert_config().environment, occurred_at=NOW,
    ).fingerprint()
    broken, _ = _one_shot_starvation_cycle(
        conn, backlog_size=5, replayable_cap=3, one_shot_files=[1205],
        failing_fingerprints={failing})
    check("[pg] a one-shot incident that cannot persist is reported as a failure",
          broken.report_failures == 1 and broken.one_shot_incidents_opened == 0,
          broken.to_dict())
    check("[pg] and it is still made durable in aggregate",
          broken.one_shot_unreported == 1 and broken.truncation_incident_reported,
          broken.to_dict())
    check("[pg] the input is never left with no evidence at all",
          any(row["incident_code"] == sig.TRUNCATED_INCIDENT_CODE
              and any(entry["raw_file_id"] == raw_id(1205)
                      for entry in (row["unreported"] or []))
              for row in _all_incident_rows(conn)),
          str(_all_incident_rows(conn)))


def test_pg_a_replayable_backlog_alone_never_truncates(conn) -> None:
    """Criterion 3 and 4 together: a large replayable backlog still activates
    under its own bound, drains across cycles, and raises no truncation
    evidence — nothing about it was ever lost."""
    files = list(range(1301, 1311))
    _bootstrap(conn, [(raw_id(index), "NORMALIZED", None) for index in files])
    batch = _replayable_backlog(files)

    os.environ[sig.MAX_NEW_INCIDENTS_ENV] = "4"
    try:
        cycles = [sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch) for _ in range(3)]
    finally:
        os.environ.pop(sig.MAX_NEW_INCIDENTS_ENV, None)

    check("[pg] first activation stays bounded",
          cycles[0].incidents_opened == 4 and cycles[0].incidents_deferred == 6,
          cycles[0].to_dict())
    check("[pg] the backlog drains over later cycles",
          [cycle.incidents_opened for cycle in cycles] == [4, 4, 2],
          str([cycle.to_dict() for cycle in cycles]))
    check("[pg] every backlog input ends with its own incident",
          {row["raw_file_id"] for row in _incident_rows(conn)} == {raw_id(i) for i in files},
          str(len(_incident_rows(conn))))
    check("[pg] a replayable bound never opens truncation evidence",
          all(not cycle.truncation_incident_reported and cycle.one_shot_unreported == 0
              for cycle in cycles)
          and not any(row["incident_code"] == sig.TRUNCATED_INCIDENT_CODE
                      for row in _all_incident_rows(conn)),
          str(_all_incident_rows(conn)))

    # Criterion 7: an already-represented condition stays quiet on rescan.
    outbox_before = _outbox_count(conn)
    steady = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
    check("[pg] a rescan of the drained backlog opens nothing and alerts nothing",
          steady.incidents_opened == 0 and steady.already_open_count == len(files)
          and _outbox_count(conn) == outbox_before, steady.to_dict())


def _truncation_rows(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT incident_id::text AS incident_id, fingerprint, state, "
            "       latest_payload -> 'fingerprint_fields' ->> 'unreported_one_shot_digest' "
            "         AS digest, "
            "       (latest_payload -> 'affected_record_count')::int AS affected, "
            "       occurrence_count, "
            "       latest_payload -> 'evidence' -> 'unreported' AS unreported "
            "FROM suspected_bug_incidents WHERE incident_code = %s "
            "ORDER BY first_seen_at ASC, incident_id ASC",
            (sig.TRUNCATED_INCIDENT_CODE,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return rows


def _unreported_ids(row: dict) -> list[str]:
    """The raw file ids a truncation incident actually displays.

    `SuspectedBugEvent` sanitization caps every list at `MAX_LIST_ITEMS` and
    appends a marker *string*, so the stored evidence is a list of dicts
    followed by a bounded-payload notice. Filtering to the dicts keeps these
    assertions about membership rather than about the sanitizer.
    """
    return [entry["raw_file_id"] for entry in (row.get("unreported") or [])
            if isinstance(entry, dict)]


def _one_shot_batch(indexes: list[int]) -> job.WorkflowBBatchResult:
    outcomes = job.WorkflowBPostprocessorOutcome
    return _result_with([], [
        _postprocessor(index, outcomes.FAILED_NON_RETRYABLE, operator=True,
                       category="ambiguous_enrichment_match") for index in indexes])


def test_pg_blocker1_two_large_populations_are_two_durable_incidents(conn) -> None:
    """Blocker 1, against the real incident table.

    Two cycles, two 51-input one-shot populations sharing their first 50 sorted
    identities. The reviewed candidate hashed the capped list, so the second
    population upserted onto the first incident as another *occurrence* — same
    fingerprint, same title, no new alert — and the 51st input of the second
    cycle was represented by an incident that does not name it. Two distinct
    memberships must be two distinct durable incidents.
    """
    # The leading file of each batch absorbs the single individual-incident slot,
    # so both cycles leave a 51-member unreported set whose first 50 sorted
    # identities are identical and whose 51st differs. That is the collision
    # shape exactly.
    shared = list(range(2400, 2450))
    first_files, second_files = [2481] + shared + [2490], [2482] + shared + [2491]
    _bootstrap(conn, [(raw_id(index), "NORMALIZED", "OK")
                      for index in sorted(set(first_files + second_files))])

    os.environ[sig.MAX_NEW_ONE_SHOT_INCIDENTS_ENV] = "1"
    try:
        first = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, _one_shot_batch(first_files))
        second = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, _one_shot_batch(second_files))
    finally:
        os.environ.pop(sig.MAX_NEW_ONE_SHOT_INCIDENTS_ENV, None)

    check("[pg] both cycles truncate above the display cap",
          first.one_shot_unreported == 51 and second.one_shot_unreported == 51,
          f"{first.to_dict()} {second.to_dict()}")
    rows = _truncation_rows(conn)
    check("[pg] two different memberships are two durable incidents",
          len(rows) == 2, str([(row["fingerprint"], row["affected"]) for row in rows]))
    check("[pg] their digests differ",
          len({row["digest"] for row in rows}) == 2, str([row["digest"] for row in rows]))
    check("[pg] neither is an extra occurrence of the other",
          all(int(row["occurrence_count"]) == 1 for row in rows),
          str([row["occurrence_count"] for row in rows]))
    check("[pg] each alerted in its own right",
          _outbox_count(conn) >= 2, str(_outbox_count(conn)))
    displayed = [_unreported_ids(row) for row in rows]
    check("[pg] the displayed evidence really is the identical capped prefix",
          len(displayed) == 2 and displayed[0] == displayed[1] and bool(displayed[0]),
          str([len(entry) for entry in displayed]))
    check("[pg] the displayed evidence is a strict subset of the membership",
          all(len(_unreported_ids(row)) < int(row["affected"]) for row in rows),
          str([(len(_unreported_ids(row)), row["affected"]) for row in rows]))
    check("[pg] and both stay durable and carry the true total",
          all(row["state"] == "open" and int(row["affected"]) == 51 for row in rows),
          str([(row["state"], row["affected"]) for row in rows]))

    # Re-observing the *identical* membership is dedup, not a third incident:
    # same digest, same fingerprint, one more occurrence on the row that exists.
    # Replayed through the durable path directly, because a later signalling
    # cycle over the same files necessarily observes a different unreported set
    # once some of them already carry their own incident.
    membership = [_one_shot(index) for index in second_files if index != 2482]
    repeat = sig.build_truncation_event(
        membership, run_id=RUN_ID, environment=sig.load_alert_config().environment,
        occurred_at=NOW)
    before = {row["fingerprint"] for row in _truncation_rows(conn)}
    check("[pg] the replayed membership is the one already stored",
          repeat.fingerprint() in before, repeat.fingerprint())
    sig.safe_report_suspected_bug(repeat, conn=conn, config=sig.load_alert_config(), now=NOW)
    after = _truncation_rows(conn)
    check("[pg] the identical membership reuses its incident",
          len(after) == 2 and {row["fingerprint"] for row in after} == before,
          str(len(after)))
    check("[pg] and it is durably provable rather than merely reported",
          sig.truncation_incident_is_durable(conn, repeat.fingerprint()))
    check("[pg] a membership never seen before is not",
          not sig.truncation_incident_is_durable(
              conn, sig.build_truncation_event(
                  membership + [_one_shot(2492)], run_id=RUN_ID,
                  environment=sig.load_alert_config().environment,
                  occurred_at=NOW).fingerprint()))


def _fail_every_report():
    from api.suspected_bug import SuspectedBugReportResult

    def fake(event, **_kwargs):
        return SuspectedBugReportResult(
            fingerprint=event.fingerprint(), error="injected: incident persistence refused")
    return fake


def _fail_reports_for_codes(codes: set[str]):
    """Fail persistence for whole incident codes, so the individual and the
    aggregate halves can be broken independently."""
    real = sig.safe_report_suspected_bug
    from api.suspected_bug import SuspectedBugReportResult

    def fake(event, **kwargs):
        if event.incident_code in codes:
            return SuspectedBugReportResult(
                fingerprint=event.fingerprint(), error="injected: incident persistence refused")
        return real(event, **kwargs)
    return fake


def test_pg_blocker2_both_persistence_paths_failing_is_a_safety_failure(conn) -> None:
    """Blocker 2. The fallback must prove *itself* durable.

    The reviewed candidate read `report.error is None` on the truncation write
    and stopped there, and the post-write re-read it did perform is scoped to
    `WORKFLOW_B_INPUT_UNRESOLVED`, so it could never have seen the truncation
    incident anyway. With both writes failing, a one-shot input therefore ended
    the cycle with no individual incident, no aggregate incident and a perfectly
    healthy signal result.
    """
    real_report = sig.safe_report_suspected_bug

    # 1. individual fails, aggregate succeeds -> aggregate covers it.
    _bootstrap(conn, [(raw_id(2501), "NORMALIZED", "OK")])
    sig.safe_report_suspected_bug = _fail_reports_for_codes({sig.INCIDENT_CODE})
    try:
        covered = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, _one_shot_batch([2501]))
    finally:
        sig.safe_report_suspected_bug = real_report
    check("[pg] individual failure with a working aggregate is covered",
          covered.one_shot_unreported == 1 and covered.truncation_incident_durable
          and covered.one_shot_without_durable_evidence == 0,
          covered.to_dict())
    check("[pg] and that is not a safety failure",
          covered.safety_contract_violated is False, covered.to_dict())
    check("[pg] the aggregate incident really is in the table",
          len(_truncation_rows(conn)) == 1 and _truncation_rows(conn)[0]["state"] == "open",
          str(_truncation_rows(conn)))

    # 2. individual succeeds -> no aggregate, no failure.
    _bootstrap(conn, [(raw_id(2502), "NORMALIZED", "OK")])
    healthy = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, _one_shot_batch([2502]))
    check("[pg] a one-shot input reported individually needs no aggregate",
          healthy.one_shot_incidents_opened == 1 and healthy.one_shot_unreported == 0
          and not healthy.truncation_incident_reported
          and healthy.safety_contract_violated is False, healthy.to_dict())
    check("[pg] and no truncation incident exists at all",
          _truncation_rows(conn) == [], str(_truncation_rows(conn)))

    # 3. individual fails AND aggregate fails -> the safety contract is violated.
    _bootstrap(conn, [(raw_id(2503), "NORMALIZED", "OK")])
    client = Client()
    sig.safe_report_suspected_bug = _fail_every_report()
    try:
        lost = sig.signal_unresolved_inputs(client, conn, RUN_ID, _one_shot_batch([2503]))
    finally:
        sig.safe_report_suspected_bug = real_report
    check("[pg] nothing durable was written for the one-shot input",
          _incident_rows(conn) == [] and _truncation_rows(conn) == [],
          f"{_incident_rows(conn)} {_truncation_rows(conn)}")
    check("[pg] the pass proves the aggregate is absent rather than assuming it",
          lost.truncation_incident_reported is False
          and lost.truncation_incident_durable is False, lost.to_dict())
    check("[pg] and reports it as a safety-contract violation",
          lost.one_shot_without_durable_evidence == 1
          and lost.safety_contract_violated is True, lost.to_dict())
    check("[pg] the violation is logged as ERROR with the lost identities",
          any(level == "ERROR" and raw_id(2503) in str(context)
              for level, _message, context in client.entries), str(client.messages()))

    # 4. only a subset cannot be represented.
    _bootstrap(conn, [(raw_id(index), "NORMALIZED", "OK") for index in (2504, 2505, 2506)])
    doomed = sig.build_event(
        sig.UnresolvedInput(raw_file_id=raw_id(2505), stage="postprocess",
                            outcome="FAILED_NON_RETRYABLE",
                            reason_code="ambiguous_enrichment_match",
                            client_code="ALPHA00001", report_type="report_207"),
        run_id=RUN_ID, environment=sig.load_alert_config().environment, occurred_at=NOW,
    ).fingerprint()
    sig.safe_report_suspected_bug = _with_failing_report({doomed})
    try:
        subset = sig.signal_unresolved_inputs(
            Client(), conn, RUN_ID, _one_shot_batch([2504, 2505, 2506]))
    finally:
        sig.safe_report_suspected_bug = real_report
    represented = {row["raw_file_id"] for row in _incident_rows(conn)}
    aggregated = {entry["raw_file_id"]
                  for row in _truncation_rows(conn) for entry in (row["unreported"] or [])}
    check("[pg] the inputs that could be reported were",
          represented == {raw_id(2504), raw_id(2506)}, str(sorted(represented)))
    check("[pg] the one that could not is carried by the aggregate",
          aggregated == {raw_id(2505)}, str(sorted(aggregated)))
    check("[pg] so no input is left without evidence and the cycle may settle",
          subset.one_shot_without_durable_evidence == 0
          and subset.safety_contract_violated is False, subset.to_dict())


def test_pg_blocker2_the_failed_safety_pass_fails_the_whole_cycle(dsn, psycopg, dict_row) -> None:
    """Criteria 4C, 5 and 7 through the real orchestrator.

    The cycle must end in the existing durable operational failure path — the
    raised `WorkflowBOrchestrationError` that makes `runs.status` FAILED and
    `ops/runner.py` raise `JOB_TERMINAL_FAILURE` — and it must do that without
    touching the orchestration advisory-lock session.
    """
    real_report = sig.safe_report_suspected_bug
    outcomes = job.WorkflowBPostprocessorOutcome

    def cycle(**kwargs):
        return _orchestrator_cycle(dsn, psycopg, dict_row, **kwargs)

    # A real cycle carrying a one-shot postprocess failure. The orchestrator's
    # own postprocessor discovery is not the seam under test; the failure is
    # attached to the result the signalling pass then collects, so the lock, the
    # connection lifecycle, `_finish_cycle` and `_finish_or_raise` are all real.
    original_finish = job._finish_cycle

    def finish_with_one_shot(client, run_id, result):
        result.postprocessors = [job.WorkflowBPostprocessorResult(
            postprocessor_name="alpha00001_dysponent_id_enrichment",
            raw_file_id=raw_id(2601), client_code="ALPHA00001", report_type="report_207",
            outcome=outcomes.FAILED_NON_RETRYABLE, retryable=False,
            operator_action_required=True, error_category="ambiguous_enrichment_match")]
        return original_finish(client, run_id, result)

    with psycopg.connect(dsn, row_factory=dict_row) as setup:
        _bootstrap(setup, [(raw_id(2601), "NORMALIZED", "OK")])

    job._finish_cycle = finish_with_one_shot
    sig.safe_report_suspected_bug = _fail_every_report()
    try:
        outcome, error, conns, client = cycle()
    finally:
        job._finish_cycle = original_finish
        sig.safe_report_suspected_bug = real_report

    check("[pg] the cycle cannot settle: it raises the terminal orchestration error",
          isinstance(error, job.WorkflowBOrchestrationError), f"{type(error).__name__}: {error}")
    settled = getattr(error, "partial_result", None)
    check("[pg] and its outcome is a durable FAILED, not a success or review outcome",
          settled is not None
          and settled.outcome == job.WorkflowBOutcome.FAILED_NON_RETRYABLE,
          str(getattr(settled, "outcome", None)))
    check("[pg] the operator incident names the safety failure",
          error.operational_incident_details.get("unresolved_signal_safety_failure") is True,
          str(getattr(error, "operational_incident_details", None)))
    check("[pg] the exception text carries a distinct cause",
          "one_shot_unresolved_input_without_durable_signal" in str(error), str(error))
    check("[pg] the lock session was still relinquished cleanly",
          conns and conns[0].closed is True and _lock_is_free(dsn, psycopg, dict_row),
          str([getattr(item, "closed", None) for item in conns]))

    # 5. A connection-level signalling fault with one-shot work present: the
    #    lock session stays clean and the cycle still refuses to settle.
    job._finish_cycle = finish_with_one_shot
    try:
        outcome, error, conns, client = cycle(poison_signalling=True)
    finally:
        job._finish_cycle = original_finish
    check("[pg] a poisoned signalling path with one-shot work fails closed too",
          isinstance(error, job.WorkflowBOrchestrationError)
          and error.partial_result.outcome == job.WorkflowBOutcome.FAILED_NON_RETRYABLE,
          f"{type(error).__name__}: {error}")
    check("[pg] signalling still ran on its own connection",
          len(conns) >= 2 and conns[0] is not conns[1], str(len(conns)))
    check("[pg] the orchestration lock session is unaffected and released",
          conns[0].closed is True and _lock_is_free(dsn, psycopg, dict_row))
    check("[pg] and the failure is observable in the log",
          SIGNAL_FAILURE_LOG in client.messages(), str(client.messages()))

    # And with no one-shot work at all, a poisoned signalling path is contained
    # exactly as before — criterion 6.
    outcome, error, conns, client = cycle(poison_signalling=True)
    check("[pg] a poisoned signalling path with no one-shot work stays contained",
          error is None and outcome is not None
          and outcome.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK
          and outcome.unresolved_signal_safety_failure is False,
          f"error={error!r} outcome={getattr(outcome, 'outcome', None)}")
    check("[pg] a later natural cycle acquires the lock and completes",
          _lock_is_free(dsn, psycopg, dict_row))


# --------------------------------------------------------------------------- #
# The Stage 2 refutation, against the real Stage 2 code and real PostgreSQL
# --------------------------------------------------------------------------- #

#: Enough rows to put the real sweep's `UNROUTED_SWEEP_MAX_PAGES *
#: UNROUTED_SWEEP_LIMIT` backstop (50 x 200 = 10 000) under genuine pressure.
#: Deliberately the production constants rather than shrunken test ones: the
#: disproved claim was specifically about that prefix, so a scaled-down
#: reproduction would be evidence about a different mechanism.
STAGE2_PRESSURE_ROWS = 10_000


def _stage2_row_values(index: int, updated_at_sql: str) -> tuple:
    stem = f"wfb-stage2-{index:08d}"
    return (raw_id(index), f"/var/lib/log-platform/normalized/{stem}.csv", stem, updated_at_sql)


def _bootstrap_stage2_mutation(conn, *, front: list[int], target: int) -> None:
    """A durably unrouted Stage 2 population the *real* sweep can page through.

    Every row is `NORMALIZED`, `PENDING_REVIEW` for a content reason, and not
    retryable, which is exactly the shape that satisfies `stage2_unrouted_files`
    (not re-admitted by `STAGE2_REDISCOVERY_SQL`, not routable to Stage 3) and
    that `_candidate_rows` normal discovery refuses. Ordering is the whole point,
    so the timestamps are explicit: the front rows are oldest, the target sits
    just behind them, and the pressure rows fill the rest of the backstop
    prefix.
    """
    with conn.cursor() as cur:
        cur.execute(PLATFORM_BOOTSTRAP)
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        cur.execute("TRUNCATE suspected_bug_email_outbox, suspected_bug_occurrences, "
                    "suspected_bug_incidents RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM logs")
        cur.execute("DELETE FROM artifacts")
        cur.execute("DELETE FROM ingest.raw_file")
        cur.execute("DELETE FROM runs")
        cur.execute("INSERT INTO runs(run_id, started_at, status, trigger, source) "
                    "VALUES (%s, now(), 'RUNNING', 'SCHEDULED', "
                    "'jobs.reports.workflow_b.orchestrator')", (RUN_ID,))
        for index, offset in [(value, "30 days") for value in front] + [(target, "29 days")]:
            file_id, path, stem, _ = _stage2_row_values(index, offset)
            cur.execute(
                """
                INSERT INTO ingest.raw_file(
                    id, status, normalized_csv_path, original_filename, sha256,
                    stage2_status, stage2_report_type, stage2_pending_reason,
                    stage2_outcome_category, stage2_retryable, stage2_updated_at)
                VALUES (%s, 'NORMALIZED', %s, %s, %s, 'PENDING_REVIEW', 'report_112',
                        'low_detection_confidence', 'AMBIGUOUS_DETECTION', false,
                        now() - %s::interval)
                """,
                (file_id, path, f"{stem}.csv", stem, offset),
            )
        # The backstop pressure: newer than the target, so the target is inside
        # the swept prefix until something re-stamps it.
        cur.execute(
            """
            INSERT INTO ingest.raw_file(
                id, status, normalized_csv_path, original_filename, sha256,
                stage2_status, stage2_report_type, stage2_pending_reason,
                stage2_outcome_category, stage2_retryable, stage2_updated_at)
            SELECT gen_random_uuid(),
                   'NORMALIZED',
                   '/var/lib/log-platform/normalized/pressure-' || n || '.csv',
                   'pressure-' || n || '.csv',
                   'pressure-' || n,
                   'PENDING_REVIEW', 'report_112', 'low_detection_confidence',
                   'AMBIGUOUS_DETECTION', false,
                   now() - '28 days'::interval + (n || ' seconds')::interval
              FROM generate_series(1, %s) AS n
            """,
            (STAGE2_PRESSURE_ROWS,),
        )
    conn.commit()


def _natural_stage2_sweep(conn):
    """One cycle's reconciliation sweep, with the production bounds."""
    from jobs.reports.stage2 import job_stage2 as s2
    from jobs.reports.stage2.batch_contract import Stage2BatchResult

    result = Stage2BatchResult()
    sweep = s2.reconcile_unrouted_stage2_files(conn, result)
    return sweep, result


def _targeted_stage2_reprocess(conn, index: int) -> None:
    """The real repository mutation path, not a stand-in for it.

    `_candidate_rows` with explicit `raw_file_ids` — the operator/diagnostic
    path — followed by the `_persist_stage2` write the processing code performs.
    The row comes back still materially unresolved and still non-retryable, and
    `_persist_stage2` stamps `stage2_updated_at = NOW()` regardless.
    """
    from jobs.reports.stage2 import job_stage2 as s2

    file_id, path, _stem, _ = _stage2_row_values(index, "")
    picked = s2._candidate_rows(conn, {"raw_file_ids": [file_id], "limit": 10})
    check("[pg] targeted Stage 2 admits a row normal discovery refuses",
          [row["raw_file_id"] for row in picked] == [file_id], str(picked))
    with conn.cursor() as cur:
        s2._persist_stage2(
            cur,
            file_path=Path(path),
            report_type="report_112",
            status="PENDING_REVIEW",
            scores={"detect_score": 0.41, "final_score": 0.41},
            schema_diff={"errors": ["low_detection_confidence"]},
            pending_reason="low_detection_confidence",
            outcome_category="AMBIGUOUS_DETECTION",
            retryable=False,
        )
    conn.commit()


def _normal_stage2_discovery_reaches(conn, index: int) -> bool:
    from jobs.reports.stage2 import job_stage2 as s2

    rows = s2._candidate_rows(conn, {"limit": 500})
    return raw_id(index) in {row["raw_file_id"] for row in rows}


def _as_old_candidate(real):
    """The reviewed candidate's Stage 2 rule, reinstated for the defect half.

    `stage2_item_is_replayable` no longer exists, so the disproved behaviour is
    reconstructed here from its exact definition — a `STRANDED_*` outcome was
    read as sweep provenance and therefore as a rediscovery guarantee. Without
    this the assertions below would confirm the fix instead of falsifying the
    thing it replaced.
    """
    sweep_outcomes = {"STRANDED_UNROUTABLE", "STRANDED_AWAITING_REVIEW"}

    def fake(result):
        return [
            dataclasses.replace(item, replayable=True)
            if item.stage == "stage2" and item.outcome in sweep_outcomes else item
            for item in real(result)
        ]
    return fake


def _swept_items(result, indexes: list[int]) -> list:
    """The sweep's own items for these files, in the order it produced them."""
    wanted = {raw_id(index) for index in indexes}
    return [item for item in result.items if item.raw_file_id in wanted]


def test_pg_stage2_targeted_reprocessing_breaks_sweep_replayability(conn) -> None:
    """THE blocker: a swept Stage 2 row can be moved behind the sweep backstop.

    The reviewed candidate proved Stage 2 sweep items replayable from their
    ordinal position inside the oldest-first `50 x 200` prefix, arguing that the
    position can only fall. This executes the repository path that raises it:

    1. R is a durably unrouted Stage 2 row and a natural sweep reaches it;
    2. 10 000 further unresolved rows sit behind it, so the sweep is genuinely
       at its page-count backstop;
    3. under the old model R's individual incident is deferred as "replayable";
    4. `_candidate_rows(raw_file_ids=[R])` + `_persist_stage2` — the real
       targeted path — leave R still unresolved and stamp `stage2_updated_at`;
    5. R now sorts behind the entire prefix, so no later sweep returns it and
       normal discovery still refuses it. The deferred incident is never raised.

    Under the corrected model step 3 does not happen: R is one-shot, so it
    already holds a durable individual incident before step 4 can hide it.
    """
    front = [901001, 901002, 901003]
    target = 901500

    # --- the deployed/reviewed behaviour, reproduced ----------------------- #
    _bootstrap_stage2_mutation(conn, front=front, target=target)
    sweep, swept = _natural_stage2_sweep(conn)
    check("[pg] the sweep really is at its page-count backstop",
          sweep.truncated is True and sweep.rows_inspected == STAGE2_PRESSURE_ROWS,
          f"truncated={sweep.truncated} inspected={sweep.rows_inspected}")
    check("[pg] and a natural sweep does reach R while it is in the prefix",
          raw_id(target) in {item.raw_file_id for item in swept.items}, str(sweep))

    batch = _result_with(_swept_items(swept, front + [target]))
    check("[pg] the cycle observes the front rows and R, in sweep order",
          [item.raw_file_id for item in batch.stage2.result.items]
          == [raw_id(index) for index in front + [target]],
          str([item.raw_file_id for item in batch.stage2.result.items]))

    original_collect = sig.collect_unresolved_inputs
    sig.collect_unresolved_inputs = _as_old_candidate(original_collect)
    os.environ[sig.MAX_NEW_INCIDENTS_ENV] = "3"
    try:
        deployed = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
    finally:
        sig.collect_unresolved_inputs = original_collect
        os.environ.pop(sig.MAX_NEW_INCIDENTS_ENV, None)
    check("[pg][defect] the old model calls every swept row replayable",
          deployed.one_shot_input_count == 0 and deployed.unresolved_input_count == 4,
          deployed.to_dict())
    check("[pg][defect] so R's incident is merely deferred behind the front rows",
          deployed.incidents_deferred == 1 and _open_for(conn, raw_id(target)) == [],
          f"{deployed.to_dict()} {_incident_rows(conn)}")
    check("[pg][defect] and nothing durable records that it was dropped",
          _truncation_rows(conn) == [] and deployed.safety_contract_violated is False,
          str(_truncation_rows(conn)))

    # The mutation the position proof cannot survive.
    _targeted_stage2_reprocess(conn, target)
    after_sweep, after = _natural_stage2_sweep(conn)
    check("[pg][defect] after targeted reprocessing no later sweep reaches R",
          raw_id(target) not in {item.raw_file_id for item in after.items}
          and after_sweep.truncated is True, str(after_sweep))
    check("[pg][defect] and normal Stage 2 discovery does not re-admit it either",
          not _normal_stage2_discovery_reaches(conn, target), "")
    check("[pg][defect] R is therefore permanently invisible with no incident at all",
          _open_for(conn, raw_id(target)) == [] and _truncation_rows(conn) == [],
          str(_incident_rows(conn)))

    # --- the corrected model ------------------------------------------------ #
    _bootstrap_stage2_mutation(conn, front=front, target=target)
    _sweep2, swept2 = _natural_stage2_sweep(conn)
    fixed_batch = _result_with(_swept_items(swept2, front + [target]))
    os.environ[sig.MAX_NEW_INCIDENTS_ENV] = "3"
    try:
        fixed = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, fixed_batch)
    finally:
        os.environ.pop(sig.MAX_NEW_INCIDENTS_ENV, None)
    check("[pg] every swept Stage 2 row is one-shot, so none is merely deferred",
          fixed.one_shot_input_count == 4 and fixed.incidents_deferred == 0
          and fixed.one_shot_unreported == 0, fixed.to_dict())
    check("[pg] R holds its own durable incident in the observing cycle",
          len(_open_for(conn, raw_id(target))) == 1, str(_incident_rows(conn)))
    check("[pg] and the replayable bound could not suppress it",
          [row["stage"] for row in _open_for(conn, raw_id(target))] == ["stage2"],
          str(_open_for(conn, raw_id(target))))

    # The same mutation, against the corrected state.
    _targeted_stage2_reprocess(conn, target)
    lost_sweep, lost = _natural_stage2_sweep(conn)
    check("[pg] the mutation still removes R from every future sweep",
          raw_id(target) not in {item.raw_file_id for item in lost.items}
          and lost_sweep.truncated is True, str(lost_sweep))
    later = sig.signal_unresolved_inputs(
        Client(), conn, RUN_ID, _result_with(_swept_items(lost, front)))
    check("[pg] a later cycle that can no longer see R does not resolve it",
          later.incidents_resolved == 0 and len(_open_for(conn, raw_id(target))) == 1,
          later.to_dict())
    surviving = _open_for(conn, raw_id(target))
    check("[pg] the actionable signal survives the loss of the evidence",
          len(surviving) == 1 and surviving[0]["reason_code"] == "low_detection_confidence",
          str(surviving))


def test_pg_stage2_backlog_within_capacity_is_individually_represented(conn) -> None:
    """Required regression 1. A persistent Stage 2 backlog at or below the
    one-shot bound gets one durable incident per input, in one cycle — which is
    what "one-shot" costs operationally and why the bound is 100 rather than 20.
    """
    backlog = list(range(1601, 1661))          # the ~60 production-sized backlog
    _bootstrap(conn, [(raw_id(index), "NORMALIZED", None) for index in backlog])
    batch = _result_with([_stranded_stage2_item(index) for index in backlog])

    cycle = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
    check("[pg] a 60-file Stage 2 backlog is entirely one-shot",
          cycle.one_shot_input_count == 60, cycle.to_dict())
    check("[pg] it fits inside the one-shot bound and is fully represented",
          cycle.one_shot_incidents_opened == 60 and cycle.one_shot_unreported == 0
          and cycle.max_new_one_shot_incidents_per_cycle == 100, cycle.to_dict())
    check("[pg] every input has its own open incident",
          {row["raw_file_id"] for row in _incident_rows(conn)}
          == {raw_id(index) for index in backlog}, str(len(_incident_rows(conn))))
    check("[pg] no truncation evidence was needed and the cycle is not a failure",
          _truncation_rows(conn) == [] and cycle.safety_contract_violated is False,
          str(_truncation_rows(conn)))
    check("[pg] the burst costs exactly one notification per input, once",
          _outbox_count(conn) == 60, str(_outbox_count(conn)))

    # Required regression 5: the standing backlog is re-observed every cycle,
    # and correctness does not depend on that — but neither does it re-alert.
    outbox_before = _outbox_count(conn)
    repeat = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
    check("[pg] re-observing the whole backlog opens nothing new",
          repeat.one_shot_incidents_opened == 0 and repeat.already_open_count == 60,
          repeat.to_dict())
    check("[pg] and enqueues no new outbox message",
          _outbox_count(conn) == outbox_before, str(_outbox_count(conn)))
    check("[pg] a re-observed one-shot population needs no truncation evidence",
          repeat.one_shot_unreported == 0 and not repeat.truncation_incident_reported,
          repeat.to_dict())


def test_pg_stage2_backlog_beyond_capacity_is_still_durably_represented(conn) -> None:
    """Required regression 2. Above the one-shot bound the remainder is carried
    by the truncation incident — exact count, complete-set digest — and drains
    into individual incidents over later cycles as the represented ones stop
    consuming capacity."""
    backlog = list(range(1701, 1711))
    _bootstrap(conn, [(raw_id(index), "NORMALIZED", None) for index in backlog])
    batch = _result_with([_stranded_stage2_item(index) for index in backlog])

    os.environ[sig.MAX_NEW_ONE_SHOT_INCIDENTS_ENV] = "4"
    try:
        first = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
        check("[pg] individual capacity is spent, and the rest is counted",
              first.one_shot_incidents_opened == 4 and first.one_shot_unreported == 6,
              first.to_dict())
        truncation = _truncation_rows(conn)
        check("[pg] one truncation incident stands in for the remainder",
              len(truncation) == 1 and truncation[0]["affected"] == 6, str(truncation))
        check("[pg] its identity covers the complete unreported set",
              truncation[0]["digest"] == sig.one_shot_membership_digest(
                  sig.canonical_one_shot_identities([
                      sig.UnresolvedInput(raw_file_id=raw_id(index), stage="stage2",
                                          outcome="STRANDED_UNROUTABLE",
                                          reason_code="missing_client_code")
                      for index in backlog[4:]])),
              str(truncation[0]["digest"]))
        check("[pg] the aggregate is durable, so the cycle is not a failure",
              first.truncation_incident_durable is True
              and first.safety_contract_violated is False, first.to_dict())

        second = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
        check("[pg] the next cycle represents the next four individually",
              second.one_shot_incidents_opened == 4 and second.already_open_count == 4,
              second.to_dict())
        third = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
        check("[pg] and the backlog reaches full individual representation",
              third.one_shot_incidents_opened == 2 and third.one_shot_unreported == 0,
              third.to_dict())
    finally:
        os.environ.pop(sig.MAX_NEW_ONE_SHOT_INCIDENTS_ENV, None)

    check("[pg] no Stage 2 input was ever left without durable representation",
          {row["raw_file_id"] for row in _incident_rows(conn)}
          == {raw_id(index) for index in backlog}, str(len(_incident_rows(conn))))


def test_pg_stage2_persistence_failures_fall_back_then_fail_closed(conn) -> None:
    """Required regressions 3 and 4, for the Stage 2 class specifically.

    A Stage 2 individual incident that cannot persist is covered by the
    aggregate; when the aggregate cannot persist either, the cycle fail-closes
    through the already-accepted `safety_contract_violated` path.
    """
    backlog = list(range(1801, 1806))
    batch = _result_with([_stranded_stage2_item(index) for index in backlog])

    # --- 3: the individual write fails, the aggregate covers it ------------- #
    _bootstrap(conn, [(raw_id(index), "NORMALIZED", None) for index in backlog])
    failing = {_fingerprint_of(1803, "missing_client_code")}
    real_report = sig.safe_report_suspected_bug
    sig.safe_report_suspected_bug = _with_failing_report(failing)
    try:
        partial = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
    finally:
        sig.safe_report_suspected_bug = real_report
    check("[pg] the failed Stage 2 incident is reported as a failure, not as coverage",
          partial.report_failures == 1 and partial.one_shot_incidents_opened == 4,
          partial.to_dict())
    check("[pg] the aggregate covers exactly the input that could not persist",
          partial.one_shot_unreported == 1 and partial.truncation_incident_durable,
          partial.to_dict())
    check("[pg] and names it",
          [_unreported_ids(row) for row in _truncation_rows(conn)] == [[raw_id(1803)]],
          str(_truncation_rows(conn)))
    check("[pg] a covered Stage 2 input is not a safety failure",
          partial.safety_contract_violated is False, partial.to_dict())

    # --- 4: both writes fail -> fail-closed --------------------------------- #
    _bootstrap(conn, [(raw_id(index), "NORMALIZED", None) for index in backlog])
    sig.safe_report_suspected_bug = _fail_every_report()
    try:
        broken = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, batch)
    finally:
        sig.safe_report_suspected_bug = real_report
    check("[pg] neither representation exists",
          broken.one_shot_unreported == 5 and broken.truncation_incident_durable is False,
          broken.to_dict())
    check("[pg] so the Stage 2 cycle fail-closes",
          broken.safety_contract_violated is True
          and broken.one_shot_without_durable_evidence == 5, broken.to_dict())


def test_pg_a_repeated_one_shot_condition_does_not_re_alert(conn) -> None:
    """Criterion 7 for the one-shot half. The same postprocessor failing again
    on the same input — a re-clean that reloads and fails identically — is one
    incident, not one per cycle."""
    _bootstrap_starvation(conn, backlog_size=1, one_shot_files=[1401])
    outcomes = job.WorkflowBPostprocessorOutcome
    same = _result_with([], [_postprocessor(1401, outcomes.FAILED_NON_RETRYABLE,
                                            operator=True, category="ambiguous_enrichment_match")])
    first = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, same)
    outbox_after_first = _outbox_count(conn)
    second = sig.signal_unresolved_inputs(Client(), conn, RUN_ID, same)
    check("[pg] the one-shot condition opens exactly one incident",
          first.one_shot_incidents_opened == 1 and second.one_shot_incidents_opened == 0,
          f"{first.to_dict()} {second.to_dict()}")
    check("[pg] observing it again is dedup, not a new alert",
          second.already_open_count == 1 and _outbox_count(conn) == outbox_after_first,
          second.to_dict())
    check("[pg] and it opens no truncation evidence either",
          second.one_shot_unreported == 0 and not second.truncation_incident_reported,
          second.to_dict())


class _TrackedConn:
    """A real connection that remembers whether cleanup actually reached it."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.closed = False
        self.poisoned = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        self.closed = True
        self._inner.close()


class _PoisonedConn:
    """A connection broken at connection level: nothing on it can be used."""

    def __init__(self, error) -> None:
        self._error = error
        self.closed = False
        self.poisoned = True

    def cursor(self, *_a, **_kw):
        raise self._error("injected: signalling connection is broken")

    def commit(self):
        raise self._error("injected: signalling connection is broken")

    def rollback(self):
        raise self._error("injected: signalling connection is broken")

    def close(self) -> None:
        self.closed = True


def _lock_is_free(dsn, psycopg, dict_row) -> bool:
    """Can an independent session take the orchestration lock right now?"""
    key = job.workflow_b_advisory_lock_key()
    with psycopg.connect(dsn, row_factory=dict_row) as probe:
        with probe.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (key,))
            got = bool(cur.fetchone()["got"])
        if got:
            with probe.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        probe.commit()
    return got


def _orchestrator_cycle(dsn, psycopg, dict_row, *, stage2_result=None, stage2_raises=None,
                        stage3_result=None,
                        poison_signalling: bool = False, break_unlock: bool = False):
    """One real `run_workflow_b_batch`: real lock, real cleanup, stubbed stages.

    Only the stages and — when asked — the signalling connection are injected.
    The advisory lock, the connection lifecycle, `_finish_cycle` and the error
    precedence are the production ones, because those are what is under test.
    """
    conns = []

    def factory():
        if conns and poison_signalling:
            conn = _PoisonedConn(psycopg.OperationalError)
        else:
            conn = _TrackedConn(psycopg.connect(dsn, row_factory=dict_row))
        conns.append(conn)
        return conn

    def stage2(*_a, **_kw):
        if stage2_raises is not None:
            raise stage2_raises
        return stage2_result if stage2_result is not None else Stage2BatchResult()

    def broken_unlock(_conn):
        raise psycopg.OperationalError("injected: advisory unlock failed")

    client = Client()
    originals = (job._platform_pg_conn, job.fetch_reports_batch, job.process_stage2_batch,
                 job.process_stage3_batch, job._release_global_lock)
    job._platform_pg_conn = factory
    job.fetch_reports_batch = lambda *_a, **_k: Stage1BatchResult(mailbox_check_completed=True)
    job.process_stage2_batch = stage2
    job.process_stage3_batch = (
        lambda *_a, **_k: stage3_result if stage3_result is not None else Stage3BatchResult())
    if break_unlock:
        job._release_global_lock = broken_unlock
    outcome, error = None, None
    try:
        outcome = job.run_workflow_b_batch(client, RUN_ID, {})
    except BaseException as exc:  # the precedence of *whatever* escapes is the point
        error = exc
    finally:
        (job._platform_pg_conn, job.fetch_reports_batch, job.process_stage2_batch,
         job.process_stage3_batch, job._release_global_lock) = originals
    return outcome, error, conns, client


SIGNAL_FAILURE_LOG = "Workflow B unresolved-input signalling failed"
UNLOCK_FAILURE_LOG = "Workflow B advisory unlock failed; closing the lock session instead"


def test_pg_blocker5_signalling_and_unlock_faults_cannot_replace_the_outcome(
        conn, dsn, psycopg, dict_row) -> None:
    """Blocker 5. Containment, cleanup order and error precedence.

    `workflow_b.orchestrator.v1` is a *session-level* advisory lock, so commit
    and rollback on a healthy connection never released it and that was never
    the defect. The defect was that signalling wrote through the very session
    whose job is to hold the lock, so a connection-level signalling fault could
    poison it — and the unconditional unlock in `finally` then raised out of
    cleanup and replaced whatever the cycle had actually decided.
    """
    _bootstrap(conn, [(raw_id(901), "NORMALIZED", None)])

    # The architectural half: signalling never runs on the lock session.
    outcome, error, conns, _client = _orchestrator_cycle(dsn, psycopg, dict_row)
    check("[pg] signalling gets a connection of its own, not the lock session",
          error is None and len(conns) >= 2 and conns[0] is not conns[1],
          f"error={error!r} connections={len(conns)}")
    check("[pg] every connection the cycle opened is closed",
          all(getattr(item, "closed", False) for item in conns),
          str([getattr(item, "closed", None) for item in conns]))

    # A healthy no-work cycle keeps its outcome through a broken signalling path.
    outcome, error, conns, client = _orchestrator_cycle(
        dsn, psycopg, dict_row, poison_signalling=True)
    check("[pg] a poisoned signalling path leaves SUCCEEDED_NO_WORK intact",
          error is None and outcome is not None
          and outcome.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK,
          f"error={error!r} outcome={getattr(outcome, 'outcome', None)}")
    check("[pg] the signalling failure is still observable",
          SIGNAL_FAILURE_LOG in client.messages(), str(client.messages()))
    check("[pg] the poisoned cycle still closed its lock session",
          conns[0].closed is True)
    check("[pg] and the lock is available to the next cycle",
          _lock_is_free(dsn, psycopg, dict_row))

    # A review outcome whose unresolved work is *replayable* is preserved just
    # as exactly: an unreachable alert path loses no evidence there, because the
    # next cycle rediscovers the same durable Stage 3 rows.
    replayable = Stage3BatchResult(items=[_replayable_stage3_item(901)])
    outcome, error, conns, client = _orchestrator_cycle(
        dsn, psycopg, dict_row, stage3_result=replayable, poison_signalling=True)
    check("[pg] a poisoned signalling path leaves SUCCEEDED_WITH_REVIEW_ITEMS intact",
          error is None and outcome is not None
          and outcome.outcome == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS,
          f"error={error!r} outcome={getattr(outcome, 'outcome', None)}")
    check("[pg] the review cycle records that signalling failed rather than a signal",
          outcome is not None and outcome.unresolved_input_signal is None
          and SIGNAL_FAILURE_LOG in client.messages(), str(client.messages()))

    # The Stage 2 counterpart is now the *opposite* assertion, and deliberately
    # so: Stage 2 unresolved work is one-shot, so a cycle that observed it and
    # could not run the signalling pass at all has no durable representation of
    # it anywhere. `_observed_one_shot_unresolved` is the database-free fail-safe
    # for exactly that path, and it must fail the cycle rather than settle on a
    # review outcome that asserts everything was reported.
    stranded = Stage2BatchResult(items=[_stranded_stage2_item(901)])
    outcome, error, conns, client = _orchestrator_cycle(
        dsn, psycopg, dict_row, stage2_result=stranded, poison_signalling=True)
    check("[pg] a Stage 2 review cycle whose signalling died fails closed",
          isinstance(error, job.WorkflowBOrchestrationError)
          and "one_shot_unresolved_input_without_durable_signal" in str(error),
          f"{type(error).__name__}: {error}")
    check("[pg] and it says the signalling pass, not a stage, is why",
          SIGNAL_FAILURE_LOG in client.messages(), str(client.messages()))
    check("[pg] the fail-closed cycle still released its lock",
          conns[0].closed is True and _lock_is_free(dsn, psycopg, dict_row),
          str([getattr(item, "closed", None) for item in conns]))

    # An orchestration failure stays the surfaced exception.
    outcome, error, conns, client = _orchestrator_cycle(
        dsn, psycopg, dict_row, stage2_raises=RuntimeError("stage 2 exploded"),
        poison_signalling=True)
    check("[pg] the original orchestration failure is what escapes",
          isinstance(error, job.WorkflowBOrchestrationError),
          f"{type(error).__name__}: {error}")
    check("[pg] and it still carries the stage failure, not a connection error",
          isinstance(error, job.WorkflowBOrchestrationError)
          and error.partial_result.outcome == job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR,
          str(getattr(getattr(error, "partial_result", None), "outcome", None)))
    check("[pg] the failed cycle released the orchestration lock",
          conns[0].closed is True and _lock_is_free(dsn, psycopg, dict_row))

    # Explicit unlock failure: cleanup must not become the result.
    outcome, error, conns, client = _orchestrator_cycle(
        dsn, psycopg, dict_row, break_unlock=True)
    check("[pg] a failing unlock cannot replace a healthy outcome",
          error is None and outcome is not None
          and outcome.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK,
          f"error={error!r} outcome={getattr(outcome, 'outcome', None)}")
    check("[pg] the failing unlock is logged",
          UNLOCK_FAILURE_LOG in client.messages(), str(client.messages()))
    check("[pg] the connection is closed even though the unlock raised",
          conns[0].closed is True)
    check("[pg] closing the session relinquishes the lock anyway",
          _lock_is_free(dsn, psycopg, dict_row))

    # Both faults at once, on top of an orchestration failure.
    outcome, error, conns, client = _orchestrator_cycle(
        dsn, psycopg, dict_row, stage2_raises=RuntimeError("stage 2 exploded"),
        poison_signalling=True, break_unlock=True)
    check("[pg] signalling and unlock faults together still surface the stage failure",
          isinstance(error, job.WorkflowBOrchestrationError)
          and error.partial_result.outcome == job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR,
          f"{type(error).__name__}: {error}")
    check("[pg] cleanup completed under both faults",
          conns[0].closed is True and _lock_is_free(dsn, psycopg, dict_row))

    # And a natural cycle afterwards behaves normally.
    outcome, error, _conns, _client = _orchestrator_cycle(dsn, psycopg, dict_row)
    check("[pg] a subsequent natural cycle acquires the lock and completes",
          error is None and outcome is not None and outcome.lock_acquired is True
          and outcome.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK,
          f"error={error!r} outcome={getattr(outcome, 'outcome', None)}")


def _run_orchestrator_against(dsn: str, psycopg, dict_row) -> job.WorkflowBBatchResult:
    """One real `run_workflow_b_batch` with stubbed stages, on the disposable DB.

    The stages are stubs; the lock, the connection, the signalling pass and the
    terminal payload are the real ones. That is the seam the wiring lives in.
    """
    stranded = Stage2BatchResult(items=[_stranded_stage2_item(401)])
    original_conn = job._platform_pg_conn
    original_stage1 = job.fetch_reports_batch
    original_stage2 = job.process_stage2_batch
    original_stage3 = job.process_stage3_batch
    job._platform_pg_conn = lambda: psycopg.connect(dsn, row_factory=dict_row)
    job.fetch_reports_batch = lambda *_a, **_k: Stage1BatchResult(mailbox_check_completed=True)
    job.process_stage2_batch = lambda *_a, **_k: stranded
    job.process_stage3_batch = lambda *_a, **_k: Stage3BatchResult()
    try:
        result = job.run_workflow_b_batch(Client(), RUN_ID, {})
    finally:
        job._platform_pg_conn = original_conn
        job.fetch_reports_batch = original_stage1
        job.process_stage2_batch = original_stage2
        job.process_stage3_batch = original_stage3
    check("[pg] the cycle still finishes as a review outcome, not a failure",
          result.outcome == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS,
          result.outcome.value)
    check("[pg] the terminal payload carries the signal result",
          (result.to_dict().get("unresolved_input_signal") or {}).get("incidents_opened") == 1,
          str(result.to_dict().get("unresolved_input_signal")))
    return result


# --------------------------------------------------------------------------- #

def main() -> int:
    print("=== collection: which arrived inputs are unresolved ===")
    test_the_production_stranded_shape_is_collected()
    test_a_retryable_failure_is_not_an_unresolved_input()
    test_a_cycle_level_failure_without_an_input_is_not_collected()
    test_every_blocked_class_reaches_the_signal()
    test_a_healthy_cycle_collects_nothing()

    print("\n=== replayability: proved per event, never assumed from a stage name ===")
    test_stage2_is_one_shot_whatever_produced_the_item()
    test_stage3_replayability_does_not_rest_on_an_ordinal_position()
    test_stage3_replayability_follows_the_durable_status_discovery_re_admits()
    test_one_shot_sources_stay_one_shot()
    test_an_undeclared_event_source_is_one_shot_by_default()
    test_the_collected_production_shape_carries_its_replayability()
    test_no_part_of_a_stage2_sweep_is_replayable()

    print("\n=== identity: a new problem cannot hide inside an old backlog ===")
    test_two_stranded_files_are_two_incidents()
    test_the_same_problem_keeps_one_identity_across_cycles()
    test_a_changed_blocking_reason_is_a_different_problem()
    test_the_incident_says_what_to_do()

    print("\n=== truncation identity: the complete set, independent of the display cap ===")
    test_two_populations_sharing_a_displayed_prefix_are_two_incidents()
    test_the_same_membership_in_any_order_is_one_incident()
    test_display_stays_capped_while_identity_stays_complete()
    test_a_different_reason_on_the_same_files_is_a_different_truncation()

    print("\n=== fail-closed: the one signalling condition that changes a verdict ===")
    test_an_unrepresented_one_shot_input_cannot_end_as_a_success()
    test_ordinary_signalling_and_review_conditions_are_not_global_failures()
    test_a_stage_failure_keeps_its_own_cause()
    test_the_signalling_pass_is_the_only_thing_that_sets_the_flag()
    test_one_shot_observation_is_answerable_without_a_database()

    print("\n=== outcome: an empty mailbox is healthy, an uninspected one is not ===")
    test_healthy_no_work_stays_healthy()
    test_an_uninspected_mailbox_cannot_be_healthy_no_work()
    test_a_review_cycle_is_still_a_review_cycle()

    print("\n=== fairness: a persistent backlog cannot hide a newer input ===")
    test_a_persistent_backlog_cannot_hide_newer_unresolved_inputs()
    test_the_sweep_backstop_is_announced_never_absorbed()

    print("\n=== incident lifecycle, against a disposable PostgreSQL 16 ===")
    # The incident lifecycle is *required* evidence, not an optional extra: the
    # supersession ordering, the stage-specific resolution proof, the intake
    # fairness and the lock/error containment are all statements about durable
    # database state, and none of them can be established in memory. A run that
    # cannot execute this half is therefore VERIFICATION BLOCKED, never OK.
    blocked = None
    try:
        from ops.tests_manual.disposable_postgres import (
            DisposablePostgresUnavailable, disposable_postgres,
        )
    except ImportError as exc:  # pragma: no cover - environment shape only
        blocked = f"disposable PostgreSQL helper unavailable ({exc})"
    else:
        try:
            with disposable_postgres(label="wfb-unresolved") as (dsn, info):
                print(f"  instance: {info['container']} PostgreSQL {info['server_version']}")
                run_postgres_suite(dsn)
        except DisposablePostgresUnavailable as exc:
            blocked = str(exc)
    if blocked:
        print(f"NOT RUN: {blocked}")

    print()
    print(f"checks executed: {CHECKS_RUN} ({len(FAILURES)} failed)")
    if FAILURES:
        print(f"FAIL - {len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    if blocked:
        print("VERIFICATION BLOCKED - the required PostgreSQL incident-lifecycle half "
              f"did not run: {blocked}")
        return 2
    print("OK - Workflow B unresolved-input signal regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
