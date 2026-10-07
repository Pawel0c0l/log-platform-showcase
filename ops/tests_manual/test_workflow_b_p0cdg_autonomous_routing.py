#!/usr/bin/env python3
"""Deterministic regressions for the Workflow B autonomous-routing slice.

Three defects, one theme: Workflow B could report operational success while the
work it exists to do had not happened.

  P0-D  Losing the orchestration lock returned a result that `run_context`
        recorded as SUCCESS. The watchdog classifies a SUCCESS run for a
        scheduled fire as OK, so a cycle that never entered a single stage
        satisfied the schedule. Workflow B fires only at 06:00 and 20:00, so
        nothing retried it for another 10-14 hours.

  P0-C  Stage 2 discovery re-picks a file only when its state is unset,
        retryable, or the historical retryable shape. Stage 3 discovery consumes
        a file only when Stage 2 landed on 'OK' *and* it is routable. Every other
        durable Stage 2 state belongs to neither, and after the cycle that
        created it nothing ever mentions it again.

  P0-G  Stage 3 committed client business data and only then did the orchestrator
        resolve the per-client policy that its postprocessor step requires —
        turning a missing configuration row into changed customer data plus a
        failed run.

The PostgreSQL half needs a disposable database; set WORKFLOW_B_ROUTING_TEST_DSN
to run it. Without it those tests skip and the pure tests still run.
"""
from __future__ import annotations

import os
import sys
import types

try:
    import pandas  # noqa: F401
except ModuleNotFoundError:
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = type("DataFrame", (), {})
    pandas_stub.Series = type("Series", (), {})
    sys.modules["pandas"] = pandas_stub

from jobs.mail.stage1_batch_contract import Stage1BatchResult
from jobs.reports.stage2 import job_stage2 as s2
from jobs.reports.stage2.batch_contract import (
    Stage2BatchResult,
    Stage2ItemResult,
    Stage2Outcome,
    has_batch_failures,
    item_carries_operator_ownership,
)
from jobs.reports.stage3 import job_stage3 as s3
from jobs.reports.stage3.batch_contract import (
    Stage3BatchResult,
    Stage3ItemResult,
    Stage3Outcome,
)
from jobs.reports.workflow_b import orchestrator as job
from jobs.reports.workflow_b.unresolved_inputs import UnresolvedInputSignalResult


RAW_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RUN_ID = "dc9329ad-50e7-4801-885c-8fedf006f0ee"
FAILURES: list[str] = []
CHECKS_RUN = 0


# --------------------------------------------------------------------------- #
# Minimal doubles
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self) -> None:
        self.logs: list[tuple] = []

    def log(self, level, kind, source, message, **kwargs) -> None:
        self.logs.append((level, message, kwargs.get("context")))

    def download_artifact(self, *_a, **_k):
        raise AssertionError("no artifact may be downloaded for a blocked candidate")

    def upload_artifact(self, *_a, **_k):
        raise AssertionError("no artifact may be uploaded for a blocked candidate")


class Cursor:
    """Records every statement so write ORDER can be asserted, not just outcome."""

    def __init__(self, owner: "Conn") -> None:
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None) -> None:
        self.owner.sql.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self.owner.rows.pop(0) if self.owner.rows else None

    def fetchall(self):
        rows, self.owner.rows = self.owner.rows, []
        return rows


class Conn:
    def __init__(self, *, locked: bool = True, rows=None) -> None:
        self.sql: list[tuple] = []
        self.rows = list(rows or [])
        self.locked = locked
        self.closed = False
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        if self.rows == [] and any("pg_try_advisory_lock" in s for s, _ in self.sql) is False:
            pass
        return Cursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


class LockConn(Conn):
    """A connection whose only job is to answer pg_try_advisory_lock."""

    def cursor(self):
        cur = Cursor(self)
        original = cur.execute

        def execute(sql, params=None):
            original(sql, params)
            if "pg_try_advisory_lock" in str(sql):
                self.rows.append({"pg_try_advisory_lock": self.locked})

        cur.execute = execute
        return cur


class Patch:
    def __init__(self, module, **attrs) -> None:
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.saved[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        return self

    def __exit__(self, *_exc):
        for name, value in self.saved.items():
            setattr(self.module, name, value)
        return False


def check(name: str, condition: bool, detail: object = "") -> None:
    global CHECKS_RUN
    CHECKS_RUN += 1
    if condition:
        print(f"[PASS] {name}")
    else:
        FAILURES.append(name)
        rendered = "" if detail == "" or detail is None else str(detail)
        print(f"[FAIL] {name}{(' - ' + rendered) if rendered else ''}")


# --------------------------------------------------------------------------- #
# P0-D — lock contention
# --------------------------------------------------------------------------- #

def _never(*_a, **_k):
    raise AssertionError("no stage may run when the lock was not acquired")


def test_p0d_normal_acquisition_is_unchanged() -> None:
    """Criterion 1: acquiring the lock still runs all three stages in order."""
    calls: list[str] = []
    conn = LockConn(locked=True)
    with Patch(
        job,
        _platform_pg_conn=lambda: conn,
        fetch_reports_batch=lambda *_a, **_k: calls.append("stage1") or Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=lambda *_a, **_k: calls.append("stage2") or Stage2BatchResult(),
        process_stage3_batch=lambda *_a, **_k: calls.append("stage3") or Stage3BatchResult(),
        _discover_postprocessor_plans=lambda *_a, **_k: ([], 0),
    ):
        result = job.run_workflow_b_batch(Client(), RUN_ID, {})
    check("P0-D normal acquisition runs stage1->stage2->stage3",
          calls == ["stage1", "stage2", "stage3"], str(calls))
    check("P0-D normal acquisition still reports SUCCEEDED_NO_WORK",
          result.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK, result.outcome)
    check("P0-D normal acquisition releases the lock",
          any("pg_advisory_unlock" in sql for sql, _ in conn.sql))


def test_p0d_scheduled_contention_cannot_satisfy_the_cycle() -> None:
    """Criteria 2 and 3: a scheduled fire that loses the lock fails, and runs nothing."""
    conn = LockConn(locked=False)
    blocked = None
    with Patch(job, _platform_pg_conn=lambda: conn, fetch_reports_batch=_never,
               process_stage2_batch=_never, process_stage3_batch=_never):
        try:
            job.run_workflow_b_batch(Client(), RUN_ID, {})
        except job.WorkflowBOrchestrationError as exc:
            blocked = exc.partial_result
    check("P0-D scheduled contention raises instead of returning success",
          blocked is not None)
    if blocked is None:
        return
    check("P0-D scheduled contention is BLOCKED_CONCURRENT_EXECUTION",
          blocked.outcome == job.WorkflowBOutcome.BLOCKED_CONCURRENT_EXECUTION, blocked.outcome)
    check("P0-D scheduled contention reports cycle_executed=False",
          blocked.cycle_executed is False)
    # Criterion 3: the stage stubs above raise on any call, so reaching this line
    # at all is the proof that no duplicate processing was started.
    check("P0-D scheduled contention starts no stage",
          blocked.stages_started_count == 0, blocked.stages_started_count)
    check("P0-D scheduled contention never releases a lock it does not hold",
          not any("pg_advisory_unlock" in sql for sql, _ in conn.sql))
    check("P0-D scheduled contention still closes the connection", conn.closed)


def test_p0d_incident_says_nothing_was_written() -> None:
    """The alert must make a manual re-run obviously safe."""
    conn = LockConn(locked=False)
    details = {}
    with Patch(job, _platform_pg_conn=lambda: conn, fetch_reports_batch=_never,
               process_stage2_batch=_never, process_stage3_batch=_never):
        try:
            job.run_workflow_b_batch(Client(), RUN_ID, {})
        except job.WorkflowBOrchestrationError as exc:
            details = exc.operational_incident_details
    check("P0-D incident carries durable_writes_committed=False",
          details.get("durable_writes_committed") is False, str(details))
    check("P0-D incident carries cycle_executed=False",
          details.get("cycle_executed") is False, str(details))
    check("P0-D incident names the contended lock",
          details.get("lock_namespace") == job.WORKFLOW_B_LOCK_NAMESPACE, str(details))
    check("P0-D incident records the mode that made it blocking",
          details.get("mode") == "scheduled", str(details))


def test_p0d_manual_diagnostic_still_yields_silently() -> None:
    """Criterion 4 (ownership): a manual run standing aside is not an incident."""
    conn = LockConn(locked=False)
    with Patch(job, _platform_pg_conn=lambda: conn, fetch_reports_batch=_never,
               process_stage2_batch=_never, process_stage3_batch=_never):
        skipped = job.run_workflow_b_batch(Client(), RUN_ID, {"mode": "manual_diagnostic"})
    check("P0-D manual_diagnostic contention returns SKIPPED_LOCKED without raising",
          skipped.outcome == job.WorkflowBOutcome.SKIPPED_LOCKED, skipped.outcome)
    check("P0-D manual_diagnostic contention reports cycle_executed=False",
          skipped.cycle_executed is False)


def test_p0d_recovery_after_contention_is_natural() -> None:
    """Criterion 4: the next execution, once the lock is free, behaves normally."""
    with Patch(job, _platform_pg_conn=lambda: LockConn(locked=False),
               fetch_reports_batch=_never, process_stage2_batch=_never,
               process_stage3_batch=_never):
        try:
            job.run_workflow_b_batch(Client(), RUN_ID, {})
        except job.WorkflowBOrchestrationError:
            pass
    calls: list[str] = []
    with Patch(
        job,
        _platform_pg_conn=lambda: LockConn(locked=True),
        fetch_reports_batch=lambda *_a, **_k: calls.append("stage1") or Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=lambda *_a, **_k: calls.append("stage2") or Stage2BatchResult(),
        process_stage3_batch=lambda *_a, **_k: calls.append("stage3") or Stage3BatchResult(),
        _discover_postprocessor_plans=lambda *_a, **_k: ([], 0),
    ):
        recovered = job.run_workflow_b_batch(Client(), RUN_ID, {})
    check("P0-D a later free-lock execution recovers with no operator step",
          recovered.cycle_executed and calls == ["stage1", "stage2", "stage3"], str(calls))


def test_p0d_no_work_is_distinguishable_from_lock_skip() -> None:
    """Criterion 5: the whole point of the fix."""
    with Patch(
        job,
        _platform_pg_conn=lambda: LockConn(locked=True),
        fetch_reports_batch=lambda *_a, **_k: Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=lambda *_a, **_k: Stage2BatchResult(),
        process_stage3_batch=lambda *_a, **_k: Stage3BatchResult(),
        _discover_postprocessor_plans=lambda *_a, **_k: ([], 0),
    ):
        no_work = job.run_workflow_b_batch(Client(), RUN_ID, {})
    with Patch(job, _platform_pg_conn=lambda: LockConn(locked=False),
               fetch_reports_batch=_never, process_stage2_batch=_never,
               process_stage3_batch=_never):
        skipped = job.run_workflow_b_batch(Client(), RUN_ID, {"mode": "manual_diagnostic"})
    check("P0-D a genuine no-work cycle executed", no_work.cycle_executed is True)
    check("P0-D a lock-skipped cycle did not execute", skipped.cycle_executed is False)
    check("P0-D the two outcomes are distinct values",
          no_work.outcome != skipped.outcome)
    check("P0-D cycle_executed is serialized for the operator",
          no_work.to_dict()["cycle_executed"] is True
          and skipped.to_dict()["cycle_executed"] is False)


# --------------------------------------------------------------------------- #
# P0-C — Stage 2 terminal routing
# --------------------------------------------------------------------------- #

def test_p0c_stranded_classification_names_the_owner_gap() -> None:
    awaiting = s2._stranded_item({
        "raw_file_id": RAW_ID, "stage2_status": "PENDING_REVIEW",
        "stage2_pending_reason": "cleaning_not_implemented",
        "stage2_outcome_category": "UNSUPPORTED_REPORT",
        "client_code": None, "stage2_report_type": "report_999",
    })
    check("P0-C a parked non-OK Stage 2 state is STRANDED_AWAITING_REVIEW",
          awaiting.outcome == Stage2Outcome.STRANDED_AWAITING_REVIEW, awaiting.outcome)
    check("P0-C stranded review items require operator action",
          awaiting.review_required is True)
    check("P0-C stranded items are never retryable", awaiting.retryable is False)
    check("P0-C the parked reason is preserved for the operator",
          awaiting.reason_code == "cleaning_not_implemented", awaiting.reason_code)

    unroutable = s2._stranded_item({
        "raw_file_id": RAW_ID, "stage2_status": "OK",
        "stage2_pending_reason": None, "stage2_outcome_category": "SUCCEEDED_CREATED",
        "client_code": None, "stage2_report_type": "report_112",
    })
    check("P0-C an OK-but-unroutable file is STRANDED_UNROUTABLE",
          unroutable.outcome == Stage2Outcome.STRANDED_UNROUTABLE, unroutable.outcome)
    check("P0-C an unroutable file names the missing routing key",
          unroutable.reason_code == "missing_client_code", unroutable.reason_code)


def test_p0c_stranded_state_reaches_the_operator_every_cycle() -> None:
    """The defect was that the state was announced once and then went silent."""
    result = Stage2BatchResult()
    result.items.append(s2._stranded_item({
        "raw_file_id": RAW_ID, "stage2_status": "OK", "client_code": None,
        "stage2_report_type": "report_112", "stage2_pending_reason": None,
        "stage2_outcome_category": "SUCCEEDED_CREATED",
    }))
    check("P0-C a stranded file raises operator_action_required",
          result.operator_action_required is True)
    check("P0-C a stranded file is not a batch failure",
          has_batch_failures(result) is False)
    check("P0-C a stranded file is not counted as attempted work",
          result.attempted_count == 0, result.attempted_count)
    payload = result.to_dict()
    check("P0-C the stranded count is serialized",
          payload["stranded_count"] == 1 and payload["stranded_unroutable_count"] == 1,
          str({k: v for k, v in payload.items() if "strand" in k}))


def test_p0c_reconciliation_never_double_reports_or_mutates() -> None:
    """Retry safety: the sweep reads, it does not reprocess."""
    processed = Stage2ItemResult(RAW_ID, Stage2Outcome.PENDING_HUMAN_REVIEW, review_required=True)
    result = Stage2BatchResult(items=[processed])
    other = "e45c5ed4-0d82-4742-8915-c682b9ecbd36"
    conn = Conn(rows=[
        {"raw_file_id": RAW_ID, "stage2_status": "PENDING_REVIEW", "client_code": "C",
         "stage2_report_type": "r", "stage2_pending_reason": "x",
         "stage2_outcome_category": None, "stage2_retryable": False,
         "stage2_cleaned_artifact_id": None, "source_identity": "s", "stage2_updated_at": None},
        {"raw_file_id": other, "stage2_status": "OK", "client_code": None,
         "stage2_report_type": "report_112", "stage2_pending_reason": None,
         "stage2_outcome_category": "SUCCEEDED_CREATED", "stage2_retryable": False,
         "stage2_cleaned_artifact_id": None, "source_identity": "s2", "stage2_updated_at": None},
    ])
    s2.reconcile_unrouted_stage2_files(conn, result)
    ids = [item.raw_file_id for item in result.items]
    check("P0-C a file already reported by processing is not reported twice",
          ids.count(RAW_ID) == 1, str(ids))
    check("P0-C a file only the sweep found is reported once",
          ids.count(other) == 1, str(ids))
    # Verb-position, not substring: `stage2_updated_at` contains "UPDATE".
    verbs = {sql.split(None, 1)[0].upper() for sql, _ in conn.sql if sql.strip()}
    check("P0-C the reconciliation sweep issues only SELECT",
          verbs == {"SELECT"}, str(sorted(verbs)))
    statements = " ".join(sql for sql, _ in conn.sql).upper()
    check("P0-C the reconciliation sweep takes no lock",
          "ADVISORY_LOCK" not in statements.replace(" ", ""), statements[:200])
    check("P0-C the reconciliation sweep opens no write transaction",
          conn.committed == 0, conn.committed)


# --------------------------------------------------------------------------- #
# P0-C — same-cycle ownership of a file this batch itself stranded
#
# The reviewed candidate skipped any raw_file_id already present in
# `result.items`. A file processed in *this* batch to `stage2_status='OK'` with
# no `client_code` is present there as an ordinary `SUCCEEDED_CREATED`, so the
# sweep skipped it and the cycle that created the stranded file reported plain
# SUCCEEDED. The state then waited for the next 06:00/20:00 fire.
# --------------------------------------------------------------------------- #

def _legacy_reconcile(conn, result: Stage2BatchResult, *, limit: int = 200) -> None:
    """The reviewed candidate's exact predicate, kept only to prove the defect.

    Not production code and not imported by it. It exists so the regression can
    show the *same* durable scenario resolving differently before and after the
    correction, rather than asserting the new behaviour in isolation.
    """
    already_reported = {item.raw_file_id for item in result.items}
    for row in s2.stage2_unrouted_files(conn, limit=limit):
        if row["raw_file_id"] in already_reported:
            continue
        result.items.append(s2._stranded_item(row))


def _live_stage2_discovers(row: dict) -> bool:
    """Mirror of `_candidate_rows`' live eligibility branch (retry_technical=True).

    The PostgreSQL test below asserts this predicate against the real query, so
    this Python copy cannot drift silently.
    """
    return row["status"] == "NORMALIZED" and (
        row.get("stage2_status") is None
        or row.get("stage2_retryable") is True
        or (row.get("stage2_status") == "PENDING_REVIEW"
            and row.get("stage2_pending_reason") == "stage2_exception"
            and row.get("stage2_retryable") is None)
    )


def _sweep_finds(row: dict) -> bool:
    """Mirror of `stage2_unrouted_files`: neither rediscoverable nor routable."""
    if row["status"] != "NORMALIZED" or (row.get("stage3_status") or "").strip():
        return False
    if _live_stage2_discovers(row):
        return False
    routable = bool(
        row.get("stage2_status") == "OK"
        and (row.get("client_code") or "").strip()
        and (row.get("stage2_report_type") or "").strip()
        and row.get("has_cleaned_artifact")
    )
    return not routable


SWEEP_COLUMNS = (
    "raw_file_id", "client_code", "stage2_status", "stage2_report_type",
    "stage2_pending_reason", "stage2_outcome_category", "stage2_retryable",
    "stage2_cleaned_artifact_id", "source_identity", "stage2_updated_at",
    # The keyset cursor the paginated sweep advances on. A stub that omits it
    # makes `reconcile_unrouted_stage2_files` stop after one page rather than
    # loop on an identical query, so it has to be part of the shape.
    "sweep_cursor_at",
)


class Stage2CycleConn(Conn):
    """Drives a whole `process_stage2_batch` cycle from a mutable durable table.

    Routes by statement shape rather than by call order, so discovery, the
    per-item refresh and the reconciliation sweep all read the *current* state
    of the same rows. That is what makes the same-cycle claim meaningful: the
    sweep sees exactly what `_run_single_legacy` just wrote.
    """

    def __init__(self, rows) -> None:
        super().__init__()
        self.table = {row["raw_file_id"]: dict(row) for row in rows}

    def _ordered(self) -> list[dict]:
        return [self.table[key] for key in sorted(self.table)]

    def cursor(self):
        cur = Cursor(self)
        original = cur.execute

        def execute(sql, params=None):
            original(sql, params)
            text = " ".join(str(sql).split())
            # Each statement replaces the result set, as a real cursor does. A
            # statement nothing fetches (the unlock) must leave none behind.
            if "pg_try_advisory_lock" in text:
                self.rows = [{"pg_try_advisory_lock": True}]
            elif "WHERE id=%s" in text:
                self.rows = [dict(self.table[params[0]])]
            elif "AS sweep_cursor_at" in text:
                self.rows = [
                    {column: row.get(column) for column in SWEEP_COLUMNS}
                    for row in self._ordered() if _sweep_finds(row)
                ]
            elif "normalized_csv_path" in text and "status = 'NORMALIZED'" in text:
                self.rows = [
                    dict(row) for row in self._ordered() if _live_stage2_discovers(row)
                ]
            else:
                self.rows = []

        cur.execute = execute
        return cur


def _durable_row(raw_file_id: str, **overrides) -> dict:
    row = {
        "raw_file_id": raw_file_id, "status": "NORMALIZED",
        "normalized_csv_path": f"/tmp/{raw_file_id}.csv",
        "original_filename": f"{raw_file_id}.csv", "source_identity": raw_file_id,
        "client_code": None, "stage2_status": None, "stage2_report_type": None,
        "stage2_pending_reason": None, "stage2_outcome_category": None,
        "stage2_retryable": None, "stage2_cleaned_artifact_id": None,
        "stage2_updated_at": None, "stage3_status": None, "has_cleaned_artifact": False,
    }
    row.update(overrides)
    return row


# The state Stage 2 durably leaves behind for the production report_112 family:
# cleaning succeeded, an artifact exists, and no client could be resolved.
UNROUTABLE_AFTER_PROCESSING = {
    "stage2_status": "OK", "client_code": None, "stage2_report_type": "report_112",
    "stage2_pending_reason": None, "stage2_outcome_category": None,
    "stage2_retryable": False, "stage2_cleaned_artifact_id": None,
    "has_cleaned_artifact": True,
}


def _run_unroutable_cycle(reconcile) -> tuple[Stage2BatchResult, Stage2CycleConn]:
    """One Stage 2 batch over a single file that processing strands."""
    conn = Stage2CycleConn([_durable_row(RAW_ID)])

    def _legacy_run(_client, _run_id, _params):
        conn.table[RAW_ID].update(UNROUTABLE_AFTER_PROCESSING)

    with Patch(s2, _pg_conn=lambda: conn, _run_single_legacy=_legacy_run,
               reconcile_unrouted_stage2_files=reconcile):
        result = s2.process_stage2_batch(Client(), RUN_ID, {})
    return result, conn


def test_p0c_same_cycle_stranding_was_invisible_before_the_correction() -> None:
    """The defect, reproduced against the reviewed candidate's own predicate."""
    result, _conn = _run_unroutable_cycle(_legacy_reconcile)
    items = result.items
    check("P0-C[defect] Stage 2 really does append the file as an ordinary success",
          len(items) == 1 and items[0].outcome == Stage2Outcome.SUCCEEDED_CREATED,
          str([(i.raw_file_id, str(i.outcome)) for i in items]))
    check("P0-C[defect] that success carries no review flag",
          items[0].review_required is False)
    check("P0-C[defect] `already_reported` therefore skipped the sweep row",
          all(i.outcome not in {Stage2Outcome.STRANDED_UNROUTABLE,
                                Stage2Outcome.STRANDED_AWAITING_REVIEW} for i in items),
          str([str(i.outcome) for i in items]))
    check("P0-C[defect] so the cycle that stranded the file needed no operator",
          result.operator_action_required is False)


def test_p0c_same_cycle_stranding_is_owned_by_the_cycle_that_created_it() -> None:
    """The correction: same durable scenario, same cycle, now visible."""
    result, conn = _run_unroutable_cycle(s2.reconcile_unrouted_stage2_files)
    items = result.items
    check("P0-C the stranded file is reported in the cycle that created it",
          len(items) == 1 and items[0].outcome == Stage2Outcome.STRANDED_UNROUTABLE,
          str([(i.raw_file_id, str(i.outcome)) for i in items]))
    if not items:
        return
    item = items[0]
    check("P0-C the upgraded item requires operator review", item.review_required is True)
    check("P0-C the upgraded item names the missing routing key",
          item.reason_code == "missing_client_code", item.reason_code)
    check("P0-C the upgraded item keeps the file's identity",
          item.raw_file_id == RAW_ID and item.report_type == "report_112",
          f"{item.raw_file_id} {item.report_type}")
    check("P0-C the upgrade replaces in place rather than duplicating the file",
          [i.raw_file_id for i in items].count(RAW_ID) == 1,
          str([i.raw_file_id for i in items]))
    check("P0-C the cycle now demands operator action",
          result.operator_action_required is True)
    check("P0-C a stranded file is still not a batch failure",
          has_batch_failures(result) is False)
    payload = result.to_dict()
    check("P0-C the file is no longer counted as a Stage 2 success",
          payload["succeeded_created_count"] == 0 and payload["stranded_unroutable_count"] == 1,
          str({k: v for k, v in payload.items() if "count" in k and v}))
    check("P0-C the durable state was read, never rewritten, by the sweep",
          conn.committed == 0, conn.committed)
    check("P0-C serialization still round-trips the item contract",
          payload["items"][0]["outcome"] == "STRANDED_UNROUTABLE"
          and payload["items"][0]["review_required"] is True,
          str(payload["items"][0]))


def test_p0c_ownership_predicate_leaves_owned_files_alone() -> None:
    """Inverse cases: the sweep must not touch a file the batch already owns."""
    parked = Stage2ItemResult(RAW_ID, Stage2Outcome.PENDING_HUMAN_REVIEW, review_required=True)
    stranded = s2._stranded_item({
        "raw_file_id": RAW_ID, "stage2_status": "OK", "client_code": None,
        "stage2_report_type": "report_112", "stage2_pending_reason": None,
        "stage2_outcome_category": None})
    failed = Stage2ItemResult(
        RAW_ID, Stage2Outcome.FAILED_NON_RETRYABLE,
        persisted_status="OK", reason_code="completed_missing_artifact_link")
    created = Stage2ItemResult(RAW_ID, Stage2Outcome.SUCCEEDED_CREATED, persisted_status="OK")

    check("P0-C a parked review item already owns its file",
          item_carries_operator_ownership(parked) is True)
    check("P0-C a reconciliation item already owns its file",
          item_carries_operator_ownership(stranded) is True)
    check("P0-C a technical failure owns its file even without review_required",
          item_carries_operator_ownership(failed) is True and failed.review_required is False)
    check("P0-C an ordinary Stage 2 success owns nothing",
          item_carries_operator_ownership(created) is False)

    # `completed_missing_artifact_link` is the case that makes the predicate more
    # than `review_required`: that row IS in the sweep, and overwriting its
    # FAILED_NON_RETRYABLE item would silence `has_batch_failures` entirely.
    result = Stage2BatchResult(items=[failed])
    conn = Conn(rows=[{
        "raw_file_id": RAW_ID, "stage2_status": "OK", "client_code": "C",
        "stage2_report_type": "report_207", "stage2_pending_reason":
        "completed_missing_artifact_link", "stage2_outcome_category": "FAILED_NON_RETRYABLE",
        "stage2_retryable": False, "stage2_cleaned_artifact_id": None,
        "source_identity": "s", "stage2_updated_at": None}])
    s2.reconcile_unrouted_stage2_files(conn, result)
    check("P0-C a batch failure is not downgraded to a review item",
          len(result.items) == 1 and result.items[0].outcome == Stage2Outcome.FAILED_NON_RETRYABLE,
          str([str(i.outcome) for i in result.items]))
    check("P0-C the batch still fails after reconciliation",
          has_batch_failures(result) is True)


def test_p0c_a_routable_success_is_never_reclassified() -> None:
    """A file that Stage 3 will consume must survive the cycle as a success."""
    conn = Stage2CycleConn([_durable_row(RAW_ID)])

    def _legacy_run(_client, _run_id, _params):
        conn.table[RAW_ID].update({
            "stage2_status": "OK", "client_code": "ALPHA00001",
            "stage2_report_type": "report_207", "stage2_retryable": False,
            "has_cleaned_artifact": True})

    with Patch(s2, _pg_conn=lambda: conn, _run_single_legacy=_legacy_run):
        result = s2.process_stage2_batch(Client(), RUN_ID, {})
    check("P0-C a routable file stays SUCCEEDED_CREATED",
          len(result.items) == 1
          and result.items[0].outcome == Stage2Outcome.SUCCEEDED_CREATED,
          str([str(i.outcome) for i in result.items]))
    check("P0-C a routable file raises no operator action",
          result.operator_action_required is False)
    check("P0-C a routable file is still counted as attempted work",
          result.attempted_count == 1, result.attempted_count)


def test_p0c_parked_review_semantics_are_unchanged() -> None:
    """A file this batch parks for review keeps its processing classification."""
    conn = Stage2CycleConn([_durable_row(RAW_ID)])

    def _legacy_run(_client, _run_id, _params):
        conn.table[RAW_ID].update({
            "stage2_status": "PENDING_REVIEW", "client_code": "C",
            "stage2_report_type": "PENDING_REVIEW", "stage2_retryable": False,
            "stage2_pending_reason": "cleaning_not_implemented"})

    with Patch(s2, _pg_conn=lambda: conn, _run_single_legacy=_legacy_run):
        result = s2.process_stage2_batch(Client(), RUN_ID, {})
    check("P0-C a parked file keeps its UNSUPPORTED_REPORT processing outcome",
          len(result.items) == 1
          and result.items[0].outcome == Stage2Outcome.UNSUPPORTED_REPORT,
          str([str(i.outcome) for i in result.items]))
    check("P0-C a parked file is not additionally reported as stranded",
          [i.raw_file_id for i in result.items].count(RAW_ID) == 1)
    check("P0-C a parked file still requires operator review",
          result.operator_action_required is True)


def test_p0c_same_cycle_stranding_ends_the_orchestrator_cycle_with_review_items() -> None:
    """The contract the defect actually broke, asserted at the cycle boundary."""
    conn = Stage2CycleConn([_durable_row(RAW_ID)])
    stage3_seen: list[dict] = []

    def _legacy_run(_client, _run_id, _params):
        conn.table[RAW_ID].update(UNROUTABLE_AFTER_PROCESSING)

    def _stage3(_client, _run_id, params):
        # Stage 3 discovery is a SQL predicate over routability; this records that
        # the orchestrator asked for no specific file and returns what a real
        # Stage 3 would find for an unroutable one: nothing.
        stage3_seen.append(dict(params))
        return Stage3BatchResult()

    # Stranding is what this test is about, so the signalling pass is stubbed as
    # having succeeded. It cannot simply be left to run against `LockConn`:
    # every Stage 2 unresolved condition is one-shot, so a signalling pass that
    # wrote nothing durable correctly fail-closes the cycle — a contract the
    # unresolved-input suite owns and asserts directly, and which would mask the
    # P0-C routing assertions here.
    signalled = UnresolvedInputSignalResult()

    with Patch(s2, _pg_conn=lambda: conn, _run_single_legacy=_legacy_run), Patch(
        job,
        _platform_pg_conn=lambda: LockConn(locked=True),
        fetch_reports_batch=lambda *_a, **_k: Stage1BatchResult(mailbox_check_completed=True),
        process_stage3_batch=_stage3,
        _discover_postprocessor_plans=lambda *_a, **_k: ([], 0),
        signal_unresolved_inputs=lambda *_a, **_k: signalled,
    ):
        result = job.run_workflow_b_batch(Client(), RUN_ID, {})

    check("P0-C the stranded input reached the signalling pass as one-shot work",
          job.one_shot_unresolved_input_count(result) == 1,
          str(job.one_shot_unresolved_input_count(result)))

    check("P0-C the cycle ends as SUCCEEDED_WITH_REVIEW_ITEMS, not SUCCEEDED",
          result.outcome == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS, result.outcome)
    check("P0-C the cycle did execute, so this is not a no-work signal",
          result.cycle_executed is True)
    check("P0-C the stranded file needs no second scheduled cycle to be seen",
          result.operator_action_required is True)
    stage2_items = result.stage2.result.items
    check("P0-C the operator sees STRANDED_UNROUTABLE in the cycle payload",
          len(stage2_items) == 1
          and stage2_items[0].outcome == Stage2Outcome.STRANDED_UNROUTABLE,
          str([str(i.outcome) for i in stage2_items]))
    check("P0-C Stage 3 was never given the stranded file",
          stage3_seen == [{}], str(stage3_seen))
    check("P0-C no load identity is produced, so no postprocessor consumes it",
          result.stage3.result.successful_load_identities == [])
    # `Client.upload_artifact` / `download_artifact` raise on any call, so
    # reaching this point is the proof that no duplicate artifact or business
    # write was attempted for the stranded file.
    check("P0-C no client/business write was attempted for the stranded file", True)


# --------------------------------------------------------------------------- #
# P0-G — pre-write policy validation
# --------------------------------------------------------------------------- #

class PolicyConn(Conn):
    """Answers the selector-state query and records statement order."""

    def __init__(self, policy_row) -> None:
        super().__init__()
        self.policy_row = policy_row

    def cursor(self):
        cur = Cursor(self)
        original = cur.execute

        def execute(sql, params=None):
            original(sql, params)
            if "report_type_client_load_policy" in str(sql):
                self.rows.append(self.policy_row) if self.policy_row else None

        cur.execute = execute
        return cur


def _candidate(report_type="report_207", client_code="BRAVO00016"):
    return s3.Candidate(
        raw_file_id=RAW_ID, client_code=client_code, report_type=report_type,
        source_filename="f.csv", source_sha256="deadbeef",
    )


def test_p0g_missing_policy_blocks_before_any_write() -> None:
    """Criterion 2: no destination mutation, and no Stage 3 state either."""
    conn = PolicyConn(None)
    opened: list[str] = []
    with Patch(s3, _client_business_pg_conn=lambda *_a, **_k: opened.append("destination")):
        try:
            s3._process_candidate(conn, Client(), RUN_ID, _candidate())
            raised = None
        except s3.Stage3LoadPolicyNotConfiguredError as exc:
            raised = exc
    check("P0-G a missing client policy raises Stage3LoadPolicyNotConfiguredError",
          raised is not None)
    check("P0-G the refusal names the missing configuration",
          raised is not None and raised.category == "missing_report_policy",
          getattr(raised, "category", None))
    check("P0-G the client business database is never opened", opened == [], str(opened))
    statements = " ".join(sql for sql, _ in conn.sql).upper()
    check("P0-G stage3_status is never marked RUNNING",
          "STAGE3_STATUS = 'RUNNING'" not in statements, statements[:300])
    check("P0-G stage3_status is never marked ERROR",
          "STAGE3_STATUS = 'ERROR'" not in statements, statements[:300])
    check("P0-G the platform row is not written at all",
          "UPDATE INGEST.RAW_FILE" not in statements, statements[:300])


def test_p0g_blocked_file_stays_eligible_for_retry() -> None:
    """Criterion 4: configuring the policy is enough; no force-reprocess needed.

    Proven structurally rather than by re-running the batch: eligibility is a SQL
    predicate over stage3_status, and the test above proves the preflight writes
    no stage3_status at all. A row whose stage3_status is still NULL satisfies
    `_stage3_batch_status_eligible_sql` by its first branch.

    P0-E amended the second assertion here. It used to read "'ERROR' is NOT an
    eligible state", which was true and was exactly the defect P0-E fixed:
    ERROR being ineligible is what turned a failed attempt into a permanent
    orphan. What P0-G itself requires is unchanged and is what is asserted now
    — the gate must leave the row's status untouched, so a configured retry
    costs nothing and needs no recovery machinery at all.
    """
    sql = s3._stage3_batch_status_eligible_sql("rf")
    check("P0-G a NULL stage3_status is an eligible state",
          "rf.stage3_status IS NULL" in sql, sql)

    conn = PolicyConn(None)
    try:
        s3._process_candidate(conn, Client(), RUN_ID, _candidate())
    except s3.Stage3LoadPolicyNotConfiguredError:
        pass
    stamped = [s for s, _ in conn.sql if "stage3_status" in s and s.upper().startswith("UPDATE")]
    check("P0-G the block writes no stage3_status, so retry needs no recovery path",
          stamped == [], str(stamped))


def test_p0g_incompatible_selector_also_blocks_pre_write() -> None:
    conn = PolicyConn({"client_default": "report_207_migration", "report_override": None})
    with Patch(s3, _client_business_pg_conn=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("destination opened for an incompatible selector"))):
        try:
            s3._process_candidate(conn, Client(), RUN_ID, _candidate(report_type="report_112"))
            raised = None
        except s3.Stage3LoadPolicyNotConfiguredError as exc:
            raised = exc
    check("P0-G a selector incompatible with the report type blocks pre-write",
          raised is not None and raised.category == "incompatible_report_selector",
          getattr(raised, "category", None))


def test_p0g_configured_client_is_untouched() -> None:
    """Criterion 1: the configured path must behave exactly as before."""
    conn = PolicyConn({"client_default": "report_207_migration", "report_override": None})
    s3._require_downstream_policy_configured(conn, _candidate(client_code="ALPHA00001"))
    check("P0-G a configured client passes the gate with no exception", True)

    disabled = PolicyConn({"client_default": "api_migration", "report_override": "disabled"})
    s3._require_downstream_policy_configured(
        disabled, _candidate(report_type="Alpha_GPS_Baza_LOG", client_code="ALPHA00001"))
    check("P0-G a 'disabled' selector, which creates no plan, passes the gate", True)

    workflow_a = PolicyConn({"client_default": "api_migration", "report_override": None})
    s3._require_downstream_policy_configured(workflow_a, _candidate(client_code="ALPHA00001"))
    check("P0-G a Workflow A source selector passes the gate", True)


def test_p0g_block_is_operator_visible_and_non_retryable() -> None:
    exc = s3.Stage3LoadPolicyNotConfiguredError(
        "missing_report_policy", "missing", client_code="BRAVO00016", report_type="report_207")
    item = s3._stage3_failure_item(_candidate(), exc, dry_run=False, force_reprocess=False)
    check("P0-G the blocked item is BLOCKED_OPERATOR_ACTION",
          item.outcome == Stage3Outcome.BLOCKED_OPERATOR_ACTION, item.outcome)
    check("P0-G the blocked item requires operator action",
          item.operator_action_required is True)
    check("P0-G the blocked item is not auto-retryable", item.retryable is False)
    batch = Stage3BatchResult(items=[item])
    check("P0-G a blocked file fails the Stage 3 batch", batch.has_failures is True)
    check("P0-G a blocked file counts as non-retryable, so the cycle cannot report success",
          batch.to_dict()["non_retryable_failure_count"] == 1,
          str(batch.to_dict()["non_retryable_failure_count"]))
    check("P0-G a blocked file produces no successful load identity",
          batch.successful_load_identities == [])


def test_p0g_no_duplicate_destination_records() -> None:
    """Criterion 5: a blocked candidate contributes nothing downstream."""
    blocked = s3._stage3_failure_item(
        _candidate(),
        s3.Stage3LoadPolicyNotConfiguredError(
            "missing_report_policy", "missing", client_code="C", report_type="r"),
        dry_run=False, force_reprocess=False)
    loaded = Stage3ItemResult(
        raw_file_id="other", client_code="ALPHA00001", report_type="report_207",
        outcome=Stage3Outcome.LOADED, destination_schema="telematics_reports",
        destination_table="report_207", persisted_status="OK")
    batch = Stage3BatchResult(items=[blocked, loaded])
    identities = batch.successful_load_identities
    check("P0-G only the genuinely loaded file reaches postprocessor discovery",
          len(identities) == 1 and identities[0].raw_file_id == "other",
          str([i.raw_file_id for i in identities]))


def test_p0g_uses_the_same_resolver_as_the_orchestrator() -> None:
    """The two gates must not be able to drift apart."""
    from jobs.reports.workflow_b import trip_metrics_selector as selector
    check("P0-G Stage 3 imports the orchestrator's own selector resolver",
          s3.resolve_workflow_b_trip_metrics_population_source
          is selector.resolve_workflow_b_trip_metrics_population_source)
    check("P0-G Stage 3 imports the orchestrator's own policy-state loader",
          s3.load_report_policy_selector_state is selector.load_report_policy_selector_state)
    check("P0-G the orchestrator still uses the same resolver after the load",
          job.resolve_workflow_b_trip_metrics_population_source
          is selector.resolve_workflow_b_trip_metrics_population_source)


# --------------------------------------------------------------------------- #
# P0-C — the SQL claim, against a real PostgreSQL
# --------------------------------------------------------------------------- #

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE TABLE ingest.raw_file (
    id uuid PRIMARY KEY,
    status text NOT NULL,
    client_code text,
    stage2_status text,
    stage2_report_type text,
    stage2_pending_reason text,
    stage2_outcome_category text,
    stage2_retryable boolean,
    stage2_cleaned_artifact_id uuid,
    stage2_updated_at timestamptz,
    stage3_status text,
    stage3_finished_at timestamptz,
    -- P0-E. Stage 3 discovery now also reads the durable state a crashed
    -- attempt leaves behind, so the fixture has to carry the same columns the
    -- real ingest.raw_file does or the predicate cannot be exercised at all.
    stage3_started_at timestamptz,
    stage3_destination_schema text,
    stage3_destination_table text,
    stage3_error text,
    raw_path text,
    normalized_csv_path text,
    original_filename text,
    sha256 text
);
CREATE TABLE artifacts (
    artifact_id uuid PRIMARY KEY,
    raw_file_id uuid,
    workflow_name text,
    stage_name text,
    artifact_role text,
    report_type text
);
"""

# (label, columns...) — every durable shape the pipeline can leave behind.
FIXTURES = [
    # Stage 2 will re-pick these.
    ("unprocessed",        "NORMALIZED", "C", None, False, None, False),
    ("retryable",          "NORMALIZED", "C", "PENDING_REVIEW", True, None, False),
    ("historical_retry",   "NORMALIZED", "C", "PENDING_REVIEW", None, None, False),
    # Stage 3 will consume this one.
    ("routable_ok",        "NORMALIZED", "C", "OK", False, None, True),
    # Stage 3 already handled these.
    ("loaded",             "NORMALIZED", "C", "OK", False, "OK", True),
    ("stage3_error",       "NORMALIZED", "C", "OK", False, "ERROR", True),
    # Nobody owns these.
    ("parked_review",      "NORMALIZED", "C", "PENDING_REVIEW", False, None, False),
    ("ok_no_client",       "NORMALIZED", None, "OK", False, None, True),
    ("ok_no_artifact",     "NORMALIZED", "C", "OK", False, None, False),
    # Not Workflow B's to own.
    ("duplicate_content",  "DUPLICATE_CONTENT", "C", None, False, None, False),
]

EXPECTED_STRANDED = {"parked_review", "ok_no_client", "ok_no_artifact"}


def _uuid_for(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def test_p0c_sweep_is_exactly_the_complement_of_both_discoveries(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS ingest CASCADE")
            cur.execute("DROP TABLE IF EXISTS artifacts CASCADE")
            cur.execute(SCHEMA_SQL)
            for index, (label, status, client, s2_status, retryable,
                        s3_status, has_artifact) in enumerate(FIXTURES):
                pending = "stage2_exception" if label == "historical_retry" else (
                    "cleaning_not_implemented" if s2_status == "PENDING_REVIEW" else None)
                cur.execute(
                    """INSERT INTO ingest.raw_file
                       (id, status, client_code, stage2_status, stage2_pending_reason,
                        stage2_outcome_category, stage2_retryable, stage2_report_type,
                        stage3_status, sha256, stage2_updated_at,
                        normalized_csv_path, original_filename)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), %s, %s)""",
                    (_uuid_for(index), status, client, s2_status, pending,
                     None, retryable, "report_207", s3_status, label,
                     f"/tmp/{label}.csv", f"{label}.csv"),
                )
                if has_artifact:
                    cur.execute(
                        """INSERT INTO artifacts
                           (artifact_id, raw_file_id, workflow_name, stage_name,
                            artifact_role, report_type)
                           VALUES (%s,%s,'workflow_b','stage_2_clean','cleaned','report_207')""",
                        (_uuid_for(1000 + index), _uuid_for(index)),
                    )
        conn.commit()

        rows = s2.stage2_unrouted_files(conn)
        found = {row["source_identity"] for row in rows}
        check("P0-C[pg] the sweep finds exactly the unowned files",
              found == EXPECTED_STRANDED, f"found={sorted(found)} expected={sorted(EXPECTED_STRANDED)}")

        # The complement claim, asserted against the real predicates rather than
        # against a hand-written expectation of them.
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT sha256 FROM ingest.raw_file
                 WHERE status = 'NORMALIZED'
                   AND (stage3_status IS NULL OR btrim(stage3_status) = '')
                   AND {s2.STAGE2_REDISCOVERY_SQL}
            """)
            rediscovered = {row["sha256"] for row in cur.fetchall()}
            cur.execute(f"""
                SELECT sha256 FROM ingest.raw_file
                 WHERE status = 'NORMALIZED'
                   AND (stage3_status IS NULL OR btrim(stage3_status) = '')
                   AND {s2.STAGE3_ROUTABLE_SQL}
            """)
            routable = {row["sha256"] for row in cur.fetchall()}
            cur.execute("""
                SELECT sha256 FROM ingest.raw_file
                 WHERE status = 'NORMALIZED'
                   AND (stage3_status IS NULL OR btrim(stage3_status) = '')
            """)
            pending_all = {row["sha256"] for row in cur.fetchall()}
        conn.rollback()

        check("P0-C[pg] no file is claimed by both discoveries",
              rediscovered & routable == set(), str(rediscovered & routable))
        check("P0-C[pg] sweep + discoveries partition every pending file exactly",
              rediscovered | routable | found == pending_all
              and found & (rediscovered | routable) == set(),
              f"redisc={sorted(rediscovered)} routable={sorted(routable)} stranded={sorted(found)}")
        check("P0-C[pg] a file Stage 3 already touched is left to Stage 3 recovery",
              "stage3_error" not in found and "loaded" not in found, str(sorted(found)))
        check("P0-C[pg] a non-NORMALIZED input is not claimed by this sweep",
              "duplicate_content" not in found, str(sorted(found)))

        # `STAGE2_REDISCOVERY_SQL` is written for the sweep, so on its own it is
        # only a claim *about* discovery. This runs the discovery function the
        # scheduled cycle actually calls and compares what it returns.
        discovered = {row["source_identity"] for row in s2._candidate_rows(conn, {"limit": 100})}
        conn.rollback()
        check("P0-C[pg] live Stage 2 discovery returns exactly the rediscovery set",
              discovered == rediscovered,
              f"live={sorted(discovered)} predicate={sorted(rediscovered)}")
        check("P0-C[pg] live Stage 2 discovery never re-picks a stranded file",
              discovered & found == set(), str(sorted(discovered & found)))

        # The other half of "nobody owns it", against Stage 3's real selection
        # query rather than against `STAGE3_ROUTABLE_SQL`'s restatement of it.
        stage3_candidates = {c.source_sha256 for c in s3._select_stage3_candidates(conn, limit=100)}
        conn.rollback()
        check("P0-C[pg] live Stage 3 discovery consumes no stranded file",
              stage3_candidates & found == set(), str(sorted(stage3_candidates & found)))
        check("P0-C[pg] live Stage 3 discovery still consumes the routable file",
              "routable_ok" in stage3_candidates, str(sorted(stage3_candidates)))


def test_p0c_sweep_pagination_reaches_past_a_persistent_backlog(dsn: str) -> None:
    """Blocker 3, against the real predicate and the real keyset SQL.

    250 durably unowned rows, ordered oldest-first, none of which ever resolves.
    The reviewed candidate read `LIMIT 200` and stopped there, so rows 201-250
    were invisible on that cycle and — because the first 200 never clear — on
    every cycle after it. The assertions below are the ones that behaviour fails.
    """
    import psycopg
    from psycopg.rows import dict_row

    total = 250
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS ingest CASCADE")
            cur.execute("DROP TABLE IF EXISTS artifacts CASCADE")
            cur.execute(SCHEMA_SQL)
            for index in range(total):
                # `ok_no_client`: Stage 2 succeeded, nobody can route it. The
                # production report_112 shape, and durably unowned forever.
                cur.execute(
                    """INSERT INTO ingest.raw_file
                       (id, status, client_code, stage2_status, stage2_retryable,
                        stage2_report_type, sha256, stage2_updated_at,
                        normalized_csv_path, original_filename)
                       VALUES (%s,'NORMALIZED',NULL,'OK',false,'report_112',%s,
                               now() - make_interval(mins => %s), %s, %s)""",
                    (_uuid_for(2000 + index), f"backlog-{index:04d}", total - index,
                     f"/tmp/backlog-{index}.csv", f"backlog-{index}.csv"),
                )
        conn.commit()

        newest = {f"backlog-{index:04d}" for index in range(total - 50, total)}

        # The defect: one oldest-first page cannot see the newer arrivals.
        page = s2.stage2_unrouted_files(conn, limit=s2.UNROUTED_SWEEP_LIMIT)
        page_ids = {row["source_identity"] for row in page}
        check("P0-C[pg][defect] one oldest-first page really does stop at the limit",
              len(page) == s2.UNROUTED_SWEEP_LIMIT and not (page_ids & newest),
              f"rows={len(page)} newest_seen={len(page_ids & newest)}")

        # The correction: the same cycle, paging the whole qualifying set.
        result = Stage2BatchResult()
        sweep = s2.reconcile_unrouted_stage2_files(conn, result)
        seen = {item.source_identity for item in result.items}
        check("P0-C[pg] the sweep inspects every durably unowned row",
              sweep.rows_inspected == total and len(result.items) == total,
              f"inspected={sweep.rows_inspected} items={len(result.items)}")
        check("P0-C[pg] the sweep completes rather than hitting its backstop",
              sweep.truncated is False and sweep.pages_read == 2,
              f"truncated={sweep.truncated} pages={sweep.pages_read}")
        check("P0-C[pg] every input behind the 200-row backlog is now seen",
              newest <= seen, str(sorted(newest - seen)[:5]))
        check("P0-C[pg] keyset pagination never repeats a row",
              len(seen) == total, f"unique={len(seen)} items={len(result.items)}")
        check("P0-C[pg] every one of them is independently actionable",
              all(item.review_required and item.reason_code == "missing_client_code"
                  for item in result.items))

        # Repeated cycles over the same unresolved backlog stay complete: the
        # coverage is a property of the query, not of anything that drains.
        second = Stage2BatchResult()
        again = s2.reconcile_unrouted_stage2_files(conn, second)
        check("P0-C[pg] a later cycle over the same stuck backlog is just as complete",
              again.rows_inspected == total and len(second.items) == total,
              f"inspected={again.rows_inspected}")

        # A newly arrived unresolved input is visible in the very next cycle.
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ingest.raw_file
                   (id, status, client_code, stage2_status, stage2_retryable,
                    stage2_report_type, sha256, stage2_updated_at,
                    normalized_csv_path, original_filename)
                   VALUES (%s,'NORMALIZED',NULL,'OK',false,'report_112','arrival-now',
                           now(), '/tmp/arrival.csv', 'arrival.csv')""",
                (_uuid_for(2999),),
            )
        conn.commit()
        third = Stage2BatchResult()
        s2.reconcile_unrouted_stage2_files(conn, third)
        check("P0-C[pg] a new arrival behind a permanent backlog is signalled at once",
              "arrival-now" in {item.source_identity for item in third.items})

        # The backstop is a resource bound and reports itself as one.
        capped_result = Stage2BatchResult()
        capped = s2.reconcile_unrouted_stage2_files(
            conn, capped_result, page_size=100, max_pages=1)
        check("P0-C[pg] the page-count backstop truncates and says so",
              capped.truncated is True and capped.rows_inspected == 100,
              f"truncated={capped.truncated} inspected={capped.rows_inspected}")


# --------------------------------------------------------------------------- #

def main() -> int:
    print("=== P0-D: lock contention cannot satisfy a scheduled cycle ===")
    test_p0d_normal_acquisition_is_unchanged()
    test_p0d_scheduled_contention_cannot_satisfy_the_cycle()
    test_p0d_incident_says_nothing_was_written()
    test_p0d_manual_diagnostic_still_yields_silently()
    test_p0d_recovery_after_contention_is_natural()
    test_p0d_no_work_is_distinguishable_from_lock_skip()

    print("\n=== P0-C: no Stage 2 outcome escapes ownership ===")
    test_p0c_stranded_classification_names_the_owner_gap()
    test_p0c_stranded_state_reaches_the_operator_every_cycle()
    test_p0c_reconciliation_never_double_reports_or_mutates()

    print("\n=== P0-C: the cycle that strands a file owns it in that same cycle ===")
    test_p0c_same_cycle_stranding_was_invisible_before_the_correction()
    test_p0c_same_cycle_stranding_is_owned_by_the_cycle_that_created_it()
    test_p0c_ownership_predicate_leaves_owned_files_alone()
    test_p0c_a_routable_success_is_never_reclassified()
    test_p0c_parked_review_semantics_are_unchanged()
    test_p0c_same_cycle_stranding_ends_the_orchestrator_cycle_with_review_items()

    print("\n=== P0-G: policy is proven before the first irreversible write ===")
    test_p0g_missing_policy_blocks_before_any_write()
    test_p0g_blocked_file_stays_eligible_for_retry()
    test_p0g_incompatible_selector_also_blocks_pre_write()
    test_p0g_configured_client_is_untouched()
    test_p0g_block_is_operator_visible_and_non_retryable()
    test_p0g_no_duplicate_destination_records()
    test_p0g_uses_the_same_resolver_as_the_orchestrator()

    # The routing predicates and the sweep's coverage are statements about SQL,
    # so they are *required* evidence and are run against a disposable
    # PostgreSQL 16 this suite starts and removes itself. It never accepts a DSN
    # from the environment — the previous `WORKFLOW_B_ROUTING_TEST_DSN` gate both
    # could be pointed anywhere and let a completely unverified run print OK.
    print("\n=== P0-C: the routing predicates, against PostgreSQL ===")
    blocked = None
    try:
        from ops.tests_manual.disposable_postgres import (
            DisposablePostgresUnavailable, disposable_postgres,
        )
    except ImportError as exc:  # pragma: no cover - environment shape only
        blocked = f"disposable PostgreSQL helper unavailable ({exc})"
    else:
        try:
            with disposable_postgres(label="wfb-routing") as (dsn, info):
                print(f"  instance: {info['container']} PostgreSQL {info['server_version']}")
                test_p0c_sweep_is_exactly_the_complement_of_both_discoveries(dsn)
                test_p0c_sweep_pagination_reaches_past_a_persistent_backlog(dsn)
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
        print("VERIFICATION BLOCKED - the required PostgreSQL routing/sweep half "
              f"did not run: {blocked}")
        return 2
    print("OK - Workflow B P0-C/P0-D/P0-G autonomous-routing regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
