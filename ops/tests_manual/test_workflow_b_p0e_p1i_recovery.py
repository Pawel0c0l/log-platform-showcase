#!/usr/bin/env python3
"""Deterministic regressions for Stage 3 crash recovery (P0-E) and the
`public.runs` lifecycle (P1-I).

Both defects share one shape: a durable record that stops telling the truth
after an interruption, and that nothing afterwards is responsible for fixing.

  P0-E  `_select_stage3_candidates` admitted a file only on NULL / '' / a
        superseded 'OK'. 'RUNNING' and 'ERROR' were terminal by omission, so a
        process killed just after `_mark_stage3_started` committed removed the
        file from the autonomous system permanently.

  P1-I  `run_context` called `finish_run(SUCCESS)` *inside* its own try, so a
        finalization failure on a run that had done all its work was written to
        `public.runs` as FAILED.

The P0-E half of this suite was rewritten after independent review found the
first recovery model unsafe. The tests that would have caught those defects are
marked with the property they falsify:

  * raw-file identity is not attempt identity — a superseded file's *old*
    destination rows must never prove that the *current* attempt committed;
  * there is no universal `_raw_file_id` — Alpha GPS uses bare column names,
    and 44 production files load through it;
  * report 207 is not convergent — it increments counters and is safe only
    because of its `migrated_to_client_db` marker;
  * a wall-clock grace cannot prove liveness when a legitimate execution may
    run for 6 hours (`TimeoutStartSec`) or unbounded (standalone Stage 3).

The high-value cases deliberately run against a real database with the real
loaders and the real probe rather than mocked classifier output. Set
WORKFLOW_B_RECOVERY_TEST_DSN to a disposable database; without it those tests
skip and the pure ones still run. The DSN is asserted not to be production
before a single statement is issued.
"""
from __future__ import annotations

import os
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

try:
    import pandas  # noqa: F401
except ModuleNotFoundError:
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = type("DataFrame", (), {})
    pandas_stub.Series = type("Series", (), {})
    sys.modules["pandas"] = pandas_stub

from api import client as api_client
from jobs.reports.stage3 import job_stage3 as s3
from jobs.reports.stage3.batch_contract import (
    Stage3BatchResult,
    Stage3ItemResult,
    Stage3Outcome,
)
from jobs.reports.stage3.recovery import (
    DEFAULT_STAGE3_STALE_GRACE_MINUTES,
    DESTINATION_PROVENANCE,
    DestinationEvidence,
    Stage3LoadStrategy,
    Stage3RecoveryClass,
    classify_stage3_recovery,
    format_stage3_error_evidence,
    is_stale_running,
    parse_stage3_error_evidence,
    probe_destination_evidence,
    stage3_file_advisory_lock_key,
)
from jobs.reports.workflow_b import postprocessor_registry as registry

RAW_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"
RUN_ID = "278ec6a3-6068-4090-8295-e62c0c6f6224"
ARTIFACT_OLD = "aaaaaaaa-0000-0000-0000-000000000001"
ARTIFACT_NEW = "bbbbbbbb-0000-0000-0000-000000000002"
NOW = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[PASS] {name}")
    else:
        FAILURES.append(name)
        print(f"[FAIL] {name}{(' - ' + detail) if detail else ''}")


# --------------------------------------------------------------------------- #
# Minimal doubles (pure tests only; the evidence tests use a real database)
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self) -> None:
        self.logs: list[tuple] = []

    def log(self, level, kind, source, message, **kwargs) -> None:
        self.logs.append((level, message, kwargs.get("context") or {}))

    def messages(self) -> list[str]:
        return [m for _, m, _ in self.logs]

    def download_artifact(self, *_a, **_k):
        raise AssertionError("a reconciled load must not download its artifact again")


class Cursor:
    def __init__(self, owner: "Conn") -> None:
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None) -> None:
        self.owner.sql.append((" ".join(str(sql).split()), params))
        if "pg_try_advisory_lock" in str(sql):
            self.owner.rows.append({"pg_try_advisory_lock": self.owner.lock_granted})

    def fetchone(self):
        return self.owner.rows.pop(0) if self.owner.rows else None

    def fetchall(self):
        rows, self.owner.rows = self.owner.rows, []
        return rows


class Conn:
    def __init__(self, rows=None, *, lock_granted: bool = True) -> None:
        self.sql: list[tuple] = []
        self.rows = list(rows or [])
        self.committed = 0
        self.rolled_back = 0
        self.closed = False
        self.lock_granted = lock_granted

    def cursor(self):
        return Cursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


class Patch:
    def __init__(self, target, **attrs):
        self.target, self.attrs = target, attrs
        self.saved: dict = {}

    def __enter__(self):
        for key, value in self.attrs.items():
            self.saved[key] = getattr(self.target, key)
            setattr(self.target, key, value)
        return self

    def __exit__(self, *_exc):
        for key, value in self.saved.items():
            setattr(self.target, key, value)
        return False


def _candidate(**kwargs) -> s3.Candidate:
    base = dict(
        raw_file_id=RAW_ID,
        client_code="BRAVO00016",
        report_type="report_207",
        source_filename="207.xls",
        source_sha256="deadbeef",
    )
    base.update(kwargs)
    return s3.Candidate(**base)


def _evidence(current: int | None, raw: int | None,
              strategy=Stage3LoadStrategy.TELEMATICS_TECHNICAL,
              unreadable: str | None = None) -> DestinationEvidence:
    return DestinationEvidence(current, raw, strategy, unreadable)


# --------------------------------------------------------------------------- #
# P0-E — attempt identity is content identity, not raw-file identity
# --------------------------------------------------------------------------- #

def test_p0e_superseded_old_rows_do_not_prove_current_commit() -> None:
    """BLOCKER 2, at the classifier level.

    Old rows for the same raw file exist (rows_for_raw_file > 0) but none carry
    this attempt's cleaned artifact. The previous model called that
    RECONCILE_COMMITTED and would have finalized the platform row OK while the
    newer cleaned generation was never loaded.
    """
    decision = classify_stage3_recovery(
        stage3_status="RUNNING",
        stage3_started_at=NOW - timedelta(hours=20),
        now=NOW,
        evidence=_evidence(current=0, raw=5000),
    )
    check("P0-E stale rows from a previous generation never prove the current attempt committed",
          decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)
    check("P0-E the reason names the superseded generation explicitly",
          "previous cleaned generation" in decision.reason, decision.reason)


def test_p0e_current_attempt_rows_do_prove_commit() -> None:
    decision = classify_stage3_recovery(
        stage3_status="RUNNING",
        stage3_started_at=NOW - timedelta(hours=20),
        now=NOW,
        evidence=_evidence(current=1234, raw=6000),
    )
    check("P0-E rows carrying this attempt's cleaned artifact do prove commit",
          decision.recovery_class == Stage3RecoveryClass.RECONCILE_COMMITTED, decision.reason)


def test_p0e_raw_file_count_alone_never_decides() -> None:
    """The diagnostic count must not be able to flip the decision on its own."""
    for raw_rows in (0, 1, 99999):
        decision = classify_stage3_recovery(
            stage3_status="RUNNING",
            stage3_started_at=NOW - timedelta(hours=20),
            now=NOW,
            evidence=_evidence(current=0, raw=raw_rows),
        )
        check(f"P0-E rows_for_raw_file={raw_rows} still yields SAFE_REPLAY, never reconcile",
              decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)


# --------------------------------------------------------------------------- #
# P0-E — per-strategy provenance
# --------------------------------------------------------------------------- #

def test_p0e_alpha_gps_provenance_is_not_underscore_prefixed() -> None:
    """BLOCKER 1. Production `telematics_reports."Alpha_GPS_Baza_LOG"` has no
    `_raw_file_id`; a probe hard-coded to it declares 44 production files
    uninspectable."""
    alpha = DESTINATION_PROVENANCE[Stage3LoadStrategy.ALPHA_GPS_REPLACE_ALL]
    telematics = DESTINATION_PROVENANCE[Stage3LoadStrategy.TELEMATICS_TECHNICAL]
    check("P0-E Alpha GPS provenance uses bare column names",
          alpha.raw_file_column == "raw_file_id"
          and alpha.cleaned_artifact_column == "cleaned_artifact_id", str(alpha))
    check("P0-E telematics provenance uses underscore-prefixed names",
          telematics.raw_file_column == "_raw_file_id"
          and telematics.cleaned_artifact_column == "_source_artifact_id", str(telematics))
    check("P0-E the two strategies genuinely differ, so one probe cannot serve both blindly",
          alpha.raw_file_column != telematics.raw_file_column)


def test_p0e_strategy_resolution_matches_the_loader_branch() -> None:
    check("P0-E Alpha_GPS_Baza_LOG resolves to the replace-all strategy",
          s3._stage3_load_strategy("Alpha_GPS_Baza_LOG")
          is Stage3LoadStrategy.ALPHA_GPS_REPLACE_ALL)
    check("P0-E report_207 resolves to the telematics technical strategy",
          s3._stage3_load_strategy("report_207") is Stage3LoadStrategy.TELEMATICS_TECHNICAL)
    # A report type that is merely *new* correctly resolves to the telematics
    # strategy, because that is genuinely the branch
    # `_load_dataframe_to_destination` would take for it. Fail-closed lives at
    # the probe, which reports "unreadable" when the destination does not exist
    # or lacks provenance — see the PostgreSQL cases.
    check("P0-E an ordinary new report type maps to the branch its loader would take",
          s3._stage3_load_strategy("report_999_new") is Stage3LoadStrategy.TELEMATICS_TECHNICAL)
    for unsafe in ("bad name; drop table", ""):
        check(f"P0-E an unusable report type {unsafe!r} resolves to no strategy",
              s3._stage3_load_strategy(unsafe) is None)


def test_p0e_unknown_strategy_is_never_replayed() -> None:
    decision = classify_stage3_recovery(
        stage3_status="RUNNING",
        stage3_started_at=NOW - timedelta(hours=20),
        now=NOW,
        evidence=DestinationEvidence(0, 0, None, None),
    )
    check("P0-E a strategy with no declared provenance is operator-owned, not replayed",
          decision.recovery_class == Stage3RecoveryClass.TERMINAL_OPERATOR, decision.reason)


# --------------------------------------------------------------------------- #
# P0-E — zero-row success
# --------------------------------------------------------------------------- #

def test_p0e_zero_rows_is_not_proof_of_no_commit() -> None:
    """Codex: zero destination rows != "attempt did not commit".

    Both current loaders can commit zero rows — Alpha GPS commits a bare DELETE
    for an empty workbook, and the telematics insert path commits when every row
    was filtered. The contract is therefore no longer `rows == 0 => SAFE_REPLAY`
    by fiat: it is replay *because the writer for this strategy is declared
    idempotent*, which is what makes redoing a zero-row commit harmless.
    """
    for strategy in (Stage3LoadStrategy.TELEMATICS_TECHNICAL,
                     Stage3LoadStrategy.ALPHA_GPS_REPLACE_ALL):
        provenance = DESTINATION_PROVENANCE[strategy]
        check(f"P0-E {strategy.value} declares that a zero-row commit is possible",
              provenance.zero_row_commit_possible is True)
        check(f"P0-E {strategy.value} declares its replay idempotent, which is why zero replays",
              provenance.replay_is_idempotent is True)
        decision = classify_stage3_recovery(
            stage3_status="RUNNING",
            stage3_started_at=NOW - timedelta(hours=20),
            now=NOW,
            evidence=_evidence(current=0, raw=0, strategy=strategy),
        )
        check(f"P0-E {strategy.value} zero-row state replays rather than being stranded",
              decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)
        check("P0-E the reason cites the writer's idempotence, not a bare row count",
              "idempotent" in decision.reason, decision.reason)


# --------------------------------------------------------------------------- #
# P0-E — ambiguity is never replay and never success
# --------------------------------------------------------------------------- #

def test_p0e_unreadable_evidence_is_never_replay_or_success() -> None:
    for reason in ("destination table not found",
                   "destination lacks provenance",
                   "OperationalError: could not connect"):
        decision = classify_stage3_recovery(
            stage3_status="RUNNING",
            stage3_started_at=NOW - timedelta(hours=20),
            now=NOW,
            evidence=_evidence(current=None, raw=None, unreadable=reason),
        )
        check(f"P0-E unreadable evidence ({reason[:28]}...) is operator-owned",
              decision.recovery_class == Stage3RecoveryClass.TERMINAL_OPERATOR, decision.reason)
        check("P0-E the refusal names why the evidence could not be read",
              reason[:20] in decision.reason, decision.reason)


# --------------------------------------------------------------------------- #
# P0-E — RUNNING ownership: a lock, not a clock
# --------------------------------------------------------------------------- #

def test_p0e_live_owner_outranks_the_clock() -> None:
    """The reviewed design used a 240-minute grace while
    `log-workflow-b.service` allows TimeoutStartSec=6h. A live execution near
    its maximum runtime must not be stolen even when the clock says stale."""
    decision = classify_stage3_recovery(
        stage3_status="RUNNING",
        stage3_started_at=NOW - timedelta(hours=100),   # far beyond any grace
        now=NOW,
        evidence=_evidence(current=0, raw=0),
        file_lock_acquired=False,
    )
    check("P0-E a held ownership lock outranks an expired age bound",
          decision.recovery_class == Stage3RecoveryClass.AWAIT_LIVE_OWNER, decision.reason)


def test_p0e_grace_clears_every_supported_execution_bound() -> None:
    """Boundary that falsifies the reviewed 240-minute value."""
    check("P0-E the secondary grace exceeds log-workflow-b TimeoutStartSec=6h",
          DEFAULT_STAGE3_STALE_GRACE_MINUTES > 6 * 60,
          f"{DEFAULT_STAGE3_STALE_GRACE_MINUTES} minutes")
    check("P0-E the reviewed 240-minute value would NOT have cleared it",
          240 < 6 * 60, "regression guard")

    five_hours = NOW - timedelta(hours=5)
    stale, _ = is_stale_running(stage3_started_at=five_hours, now=NOW)
    check("P0-E an execution 5 hours in — legal under a 6h timeout — is not stale",
          stale is False)

    nine_hours = NOW - timedelta(hours=9)
    stale, _ = is_stale_running(stage3_started_at=nine_hours, now=NOW)
    check("P0-E an execution beyond every supported bound is stale",
          stale is True)


def test_p0e_grace_still_recovers_within_one_cycle() -> None:
    """The bound must not be so large that recovery waits more than a cycle."""
    check("P0-E the grace stays below the 10-hour minimum gap between fires",
          DEFAULT_STAGE3_STALE_GRACE_MINUTES < 10 * 60,
          f"{DEFAULT_STAGE3_STALE_GRACE_MINUTES} minutes")


def test_p0e_young_running_with_free_lock_stands_aside() -> None:
    decision = classify_stage3_recovery(
        stage3_status="RUNNING",
        stage3_started_at=NOW - timedelta(minutes=5),
        now=NOW,
        evidence=_evidence(current=0, raw=0),
        file_lock_acquired=True,
    )
    check("P0-E a young RUNNING row is deferred even with a free lock",
          decision.recovery_class == Stage3RecoveryClass.AWAIT_GRACE, decision.reason)


def test_p0e_running_without_started_at_is_recoverable() -> None:
    decision = classify_stage3_recovery(
        stage3_status="RUNNING", stage3_started_at=None, now=NOW,
        evidence=_evidence(current=0, raw=0),
    )
    check("P0-E RUNNING with no start time is recovered, not stranded",
          decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)


def test_p0e_lock_key_is_stable_namespaced_and_bounded() -> None:
    from jobs.reports.workflow_b.orchestrator import workflow_b_advisory_lock_key

    a = stage3_file_advisory_lock_key(RAW_ID)
    b = stage3_file_advisory_lock_key(RAW_ID)
    c = stage3_file_advisory_lock_key(str(uuid.uuid4()))
    check("P0-E the per-file lock key is deterministic", a == b, f"{a} vs {b}")
    check("P0-E different files take different locks", a != c)
    check("P0-E the key fits a signed bigint", -(2**63) <= a < 2**63, str(a))
    check("P0-E the per-file lock cannot collide with the cycle-wide lock",
          a != workflow_b_advisory_lock_key())


def test_p0e_every_candidate_claims_ownership_before_loading() -> None:
    """Not only recovery candidates: two concurrent Stage 3 invocations must not
    both load the same fresh file."""
    conn = Conn(lock_granted=False)
    try:
        with Patch(s3, _require_downstream_policy_configured=lambda *_a, **_k: None):
            s3._process_candidate(conn, Client(), RUN_ID, _candidate())
        raised = None
    except s3.Stage3RecoveryNotOwnedError as exc:
        raised = exc
    check("P0-E a fresh candidate whose lock is held is deferred, not loaded",
          raised is not None and raised.recovery_class == Stage3RecoveryClass.AWAIT_LIVE_OWNER,
          getattr(raised, "recovery_class", None))
    check("P0-E the lock is attempted before any status write",
          any("pg_try_advisory_lock" in sql for sql, _ in conn.sql)
          and not any(sql.upper().startswith("UPDATE") for sql, _ in conn.sql),
          str(conn.sql))


def test_p0e_ownership_is_released_after_processing() -> None:
    conn = Conn(lock_granted=True)
    with Patch(s3,
               _require_downstream_policy_configured=lambda *_a, **_k: None,
               _resolve_stage3_recovery=lambda *_a, **_k: None,
               _load_candidate=lambda *_a, **_k: "loaded"):
        out = s3._process_candidate(conn, Client(), RUN_ID, _candidate())
    check("P0-E the ordinary path still runs under the lock", out == "loaded", str(out))
    check("P0-E the ownership lock is released when processing ends",
          any("pg_advisory_unlock" in sql for sql, _ in conn.sql), str(conn.sql))


def test_p0e_ownership_is_released_even_when_the_load_raises() -> None:
    conn = Conn(lock_granted=True)

    def boom(*_a, **_k):
        raise RuntimeError("load exploded")

    with Patch(s3,
               _require_downstream_policy_configured=lambda *_a, **_k: None,
               _resolve_stage3_recovery=lambda *_a, **_k: None,
               _load_candidate=boom):
        try:
            s3._process_candidate(conn, Client(), RUN_ID, _candidate())
        except RuntimeError:
            pass
    check("P0-E a failing load still releases the file's ownership lock",
          any("pg_advisory_unlock" in sql for sql, _ in conn.sql), str(conn.sql))


# --------------------------------------------------------------------------- #
# P0-E — durable ERROR retryability
# --------------------------------------------------------------------------- #

def test_p0e_error_evidence_round_trips() -> None:
    marked = format_stage3_error_evidence("candidate_load", retryable=True) + "boom"
    category, retryable = parse_stage3_error_evidence(marked)
    check("P0-E a retryable marker round-trips", category == "candidate_load" and retryable is True,
          f"{category}/{retryable}")
    marked = format_stage3_error_evidence("configuration_or_validation", retryable=False) + "bad"
    category, retryable = parse_stage3_error_evidence(marked)
    check("P0-E a non-retryable marker round-trips",
          category == "configuration_or_validation" and retryable is False,
          f"{category}/{retryable}")
    check("P0-E an unmarked legacy error yields no verdict (fail closed)",
          parse_stage3_error_evidence("plain old message") == (None, None))
    check("P0-E a NULL error yields no verdict", parse_stage3_error_evidence(None) == (None, None))


def test_p0e_retryable_error_has_an_autonomous_owner() -> None:
    decision = classify_stage3_recovery(
        stage3_status="ERROR",
        stage3_started_at=NOW - timedelta(hours=20),
        stage3_error=format_stage3_error_evidence("candidate_load", retryable=True) + "timeout",
        now=NOW,
        evidence=_evidence(current=0, raw=0),
    )
    check("P0-E a durably retryable ERROR is replayed autonomously",
          decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)


def test_p0e_non_retryable_error_does_not_red_cycle_forever() -> None:
    """Cadence alone is not a substitute for durable classification: a
    deterministic failure would otherwise fail identically at 06:00 and 20:00
    indefinitely."""
    decision = classify_stage3_recovery(
        stage3_status="ERROR",
        stage3_started_at=NOW - timedelta(hours=20),
        stage3_error=format_stage3_error_evidence(
            "configuration_or_validation", retryable=False) + "malformed",
        now=NOW,
        evidence=_evidence(current=0, raw=0),
    )
    check("P0-E a durably non-retryable ERROR is operator-owned, not auto-retried",
          decision.recovery_class == Stage3RecoveryClass.TERMINAL_OPERATOR, decision.reason)
    check("P0-E the refusal names the durable category",
          decision.error_category == "configuration_or_validation", str(decision.error_category))


def test_p0e_error_after_commit_reconciles_regardless_of_retryability() -> None:
    """An exception raised *after* the destination commit also lands in ERROR,
    so committed evidence must outrank the retryability marker."""
    for retryable in (True, False):
        decision = classify_stage3_recovery(
            stage3_status="ERROR",
            stage3_started_at=NOW - timedelta(hours=20),
            stage3_error=format_stage3_error_evidence("candidate_load", retryable=retryable) + "x",
            now=NOW,
            evidence=_evidence(current=77, raw=77),
        )
        check(f"P0-E ERROR with committed current-attempt rows reconciles (retryable={retryable})",
              decision.recovery_class == Stage3RecoveryClass.RECONCILE_COMMITTED, decision.reason)


def test_p0e_unmarked_error_is_operator_owned() -> None:
    decision = classify_stage3_recovery(
        stage3_status="ERROR",
        stage3_started_at=NOW - timedelta(hours=20),
        stage3_error="legacy message with no marker",
        now=NOW,
        evidence=_evidence(current=0, raw=0),
    )
    check("P0-E an ERROR with no durable classification is never auto-retried",
          decision.recovery_class == Stage3RecoveryClass.TERMINAL_OPERATOR, decision.reason)


def test_p0e_error_marker_uses_the_same_classifier_as_the_item() -> None:
    """If the durable marker and the reported item could disagree, a
    non-retryable failure could still be retried forever after a restart."""
    cases = [
        (ValueError("bad"), False),
        (RuntimeError("transient database blip"), True),
        (PermissionError("permission denied"), False),
    ]
    for exc, expected_retryable in cases:
        _outcome, category, retryable, _op = s3.classify_stage3_exception(exc, has_candidate=True)
        item = s3._stage3_failure_item(_candidate(), exc, dry_run=False, force_reprocess=False)
        check(f"P0-E {type(exc).__name__} classifies retryable={expected_retryable} consistently",
              retryable == expected_retryable == item.retryable,
              f"classifier={retryable} item={item.retryable}")
        check(f"P0-E {type(exc).__name__} shares one category between marker and item",
              category == item.error_category, f"{category} vs {item.error_category}")


def test_p0e_p0g_block_stays_null_and_is_not_recovery() -> None:
    """Criterion: P0-G must remain ordinary policy-repair retry."""
    decision = classify_stage3_recovery(
        stage3_status=None, stage3_started_at=None, now=NOW, evidence=None,
    )
    check("P0-E a P0-G blocked row is NOT_RECOVERY, i.e. ordinary retry",
          decision.recovery_class == Stage3RecoveryClass.NOT_RECOVERY, decision.reason)

    import inspect
    text = inspect.getsource(s3._process_candidate)
    check("P0-E the P0-G gate still precedes the ownership lock and the probe",
          text.index("_require_downstream_policy_configured")
          < text.index("_try_stage3_file_lock")
          < text.index("_resolve_stage3_recovery"), "ordering regressed")


# --------------------------------------------------------------------------- #
# P0-E — deterministic writer validation is NOT retryable
# --------------------------------------------------------------------------- #

def _fake_df(rows: list[dict], columns: list[str] | None = None):
    """A real pandas frame, so the writers under test run their real code.

    Falling back to a hand-rolled double here would be exactly the kind of test
    the review warned about: it would prove the classifier agrees with itself
    rather than that the writer genuinely refuses the input.
    """
    import pandas

    if not rows:
        return pandas.DataFrame(columns=list(columns or []))
    return pandas.DataFrame(rows, columns=list(columns) if columns else None)


def test_p0e_alpha_gps_empty_result_is_not_retryable() -> None:
    """Reachable through the real writer, not a synthetic RuntimeError string."""
    empty = _fake_df([], list(s3.ALPHA_GPS_REQUIRED_COLUMNS))
    rows, errors = s3._alpha_gps_rows(empty)
    check("P0-E an empty Alpha GPS workbook really does yield empty_result",
          rows == [] and errors == ["empty_result"], f"{rows} {errors}")

    try:
        s3._load_alpha_gps_replace_all(
            Conn(), raw_file_id=RAW_ID, run_id=RUN_ID, client_code="ALPHA00001",
            report_type="Alpha_GPS_Baza_LOG", source_artifact_id=ARTIFACT_NEW,
            source_filename="x.xlsm", df=empty, source_sha256=None,
            raw_artifact_id=None, normalized_artifact_id=None,
            cleaned_artifact_id=ARTIFACT_NEW,
            destination_schema="telematics_reports", destination_table="Alpha_GPS_Baza_LOG",
        )
        raised = None
    except s3.Stage3WriterValidationError as exc:
        raised = exc
    check("P0-E empty_result raises the structured writer-validation type",
          raised is not None and raised.signal == "alpha_gps_rows_rejected",
          getattr(raised, "signal", type(raised).__name__ if raised else None))

    outcome, category, retryable, operator = s3.classify_stage3_exception(
        raised, has_candidate=True)
    check("P0-E empty_result is classified NON-retryable",
          retryable is False and operator is True, f"retryable={retryable}")
    check("P0-E empty_result reports FAILED_WRITER_VALIDATION",
          outcome == Stage3Outcome.FAILED_WRITER_VALIDATION, outcome.value)
    check("P0-E the category carries the structured signal",
          category == "writer_validation_alpha_gps_rows_rejected", category)


def test_p0e_alpha_gps_malformed_row_is_not_retryable() -> None:
    """A bad date in the workbook. The same workbook parses the same way forever."""
    bad = _fake_df(
        [{
            "ID": "1",
            "Nr rejestracyjny": "ABC123",
            "Data przydziału": "not-a-date",
            "Nazwa Pliku csv": "f.csv",
        }],
        list(s3.ALPHA_GPS_REQUIRED_COLUMNS),
    )
    rows, errors = s3._alpha_gps_rows(bad)
    check("P0-E a malformed assignment date is rejected deterministically by the writer",
          rows == [] and any("row 1" in e for e in errors), f"{rows} {errors}")

    try:
        s3._load_alpha_gps_replace_all(
            Conn(), raw_file_id=RAW_ID, run_id=RUN_ID, client_code="ALPHA00001",
            report_type="Alpha_GPS_Baza_LOG", source_artifact_id=ARTIFACT_NEW,
            source_filename="x.xlsm", df=bad, source_sha256=None,
            raw_artifact_id=None, normalized_artifact_id=None,
            cleaned_artifact_id=ARTIFACT_NEW,
            destination_schema="telematics_reports", destination_table="Alpha_GPS_Baza_LOG",
        )
        raised = None
    except s3.Stage3WriterValidationError as exc:
        raised = exc
    _outcome, _cat, retryable, operator = s3.classify_stage3_exception(raised, has_candidate=True)
    check("P0-E a malformed Alpha GPS row is NON-retryable and operator-owned",
          raised is not None and retryable is False and operator is True,
          f"raised={raised} retryable={retryable}")


def test_p0e_missing_required_alpha_gps_columns_is_not_retryable() -> None:
    missing = _fake_df([{"ID": "1"}], ["ID"])
    _rows, errors = s3._alpha_gps_rows(missing)
    check("P0-E a workbook missing required columns is rejected by the real writer",
          any("missing required columns" in e for e in errors), str(errors))


def test_p0e_duplicate_index_refusal_is_not_retryable() -> None:
    """The plan-level refusal that blocks creating the record_id unique index."""
    exc = s3.Stage3WriterValidationError(
        "Cannot create record_id unique index because destination table already "
        "contains duplicate non-empty record_id values: A (2)",
        signal="load_plan_rejected",
    )
    outcome, category, retryable, operator = s3.classify_stage3_exception(exc, has_candidate=True)
    check("P0-E a duplicate-record_id index refusal is NON-retryable",
          retryable is False and operator is True, f"retryable={retryable}")
    check("P0-E it reports FAILED_WRITER_VALIDATION with its signal",
          outcome == Stage3Outcome.FAILED_WRITER_VALIDATION
          and category == "writer_validation_load_plan_rejected", f"{outcome} {category}")


def test_p0e_column_validation_families_are_not_retryable() -> None:
    """Exercise the real `_validate_report_columns` rather than fake messages."""
    families = {
        "empty column": [""],
        "NUL byte": ["ok\x00bad"],
        "metadata collision": ["_raw_file_id"],
        "duplicate column": ["a", "a"],
    }
    for label, columns in families.items():
        try:
            s3._validate_report_columns(columns)
            raised = None
        except s3.Stage3WriterValidationError as exc:
            raised = exc
        check(f"P0-E {label} is rejected as structured writer validation",
              raised is not None and raised.signal == "cleaned_report_columns_invalid",
              getattr(raised, "signal", None))
        if raised is not None:
            _o, _c, retryable, _op = s3.classify_stage3_exception(raised, has_candidate=True)
            check(f"P0-E {label} is NON-retryable", retryable is False)


def test_p0e_unsafe_identifier_and_unusable_artifact_are_not_retryable() -> None:
    from pathlib import Path
    import tempfile

    try:
        s3._safe_table_name("bad name; drop table")
        raised = None
    except s3.Stage3WriterValidationError as exc:
        raised = exc
    check("P0-E an unsafe destination identifier is structured writer validation",
          raised is not None and raised.signal == "unsafe_destination_identifier",
          getattr(raised, "signal", None))

    with tempfile.TemporaryDirectory() as d:
        empty = Path(d) / "empty.csv"
        empty.write_bytes(b"")
        try:
            s3._require_nonempty_artifact_file(empty, ARTIFACT_NEW)
            raised2 = None
        except s3.Stage3WriterValidationError as exc:
            raised2 = exc
        check("P0-E an empty downloaded artifact is structured writer validation",
              raised2 is not None and raised2.signal == "downloaded_artifact_unusable",
              getattr(raised2, "signal", None))
        if raised2 is not None:
            _o, _c, retryable, _op = s3.classify_stage3_exception(raised2, has_candidate=True)
            check("P0-E an empty downloaded artifact is NON-retryable", retryable is False)


def test_p0e_transient_families_remain_retryable() -> None:
    """The correction must not make every RuntimeError non-retryable."""
    import psycopg

    transient = [
        ("psycopg OperationalError", psycopg.OperationalError("connection to server was lost")),
        ("bare connection RuntimeError", RuntimeError("connection reset by peer")),
        ("storage/IO failure", OSError("read timed out while downloading artifact")),
    ]
    for label, exc in transient:
        outcome, category, retryable, operator = s3.classify_stage3_exception(
            exc, has_candidate=True)
        check(f"P0-E {label} stays RETRYABLE with autonomous ownership",
              retryable is True and operator is False,
              f"{outcome.value}/{category}/retryable={retryable}")
        check(f"P0-E {label} is not misfiled as writer validation",
              outcome != Stage3Outcome.FAILED_WRITER_VALIDATION, outcome.value)


def test_p0e_no_retryability_decision_depends_on_message_text() -> None:
    """Reword any writer message and the durable verdict must not move."""
    a = s3.Stage3WriterValidationError("row 3: invalid date", signal="alpha_gps_rows_rejected")
    b = s3.Stage3WriterValidationError("completely different wording",
                                       signal="alpha_gps_rows_rejected")
    ca = s3.classify_stage3_exception(a, has_candidate=True)
    cb = s3.classify_stage3_exception(b, has_candidate=True)
    check("P0-E classification depends on the signal, not the message", ca == cb, f"{ca} vs {cb}")

    # And the inverse: the same words on a plain RuntimeError must not be
    # dragged into the non-retryable bucket by text matching.
    plain = s3.classify_stage3_exception(RuntimeError("row 3: invalid date"), has_candidate=True)
    check("P0-E an unstructured exception with the same text is not text-matched",
          plain[2] is True, str(plain))


def test_p0e_durable_marker_matches_the_typed_result_for_every_family() -> None:
    """The marker `_mark_stage3_error` writes must agree with the reported item."""
    families = [
        s3.Stage3WriterValidationError("empty", signal="alpha_gps_rows_rejected"),
        s3.Stage3WriterValidationError("cols", signal="cleaned_report_columns_invalid"),
        s3.Stage3WriterValidationError("plan", signal="load_plan_rejected"),
        s3.Stage3WriterValidationError("acct", signal="client_account_not_configured"),
        RuntimeError("connection reset by peer"),
        ValueError("configuration"),
    ]
    for exc in families:
        outcome, category, retryable, _op = s3.classify_stage3_exception(exc, has_candidate=True)
        item = s3._stage3_failure_item(_candidate(), exc, dry_run=False, force_reprocess=False)
        conn = Conn()
        with conn.cursor() as cur:
            s3._mark_stage3_error(
                cur, raw_file_id=RAW_ID, error_message=str(exc),
                destination_schema="telematics_reports", destination_table="report_207",
                data_overwrite=False, error_category=category, retryable=retryable)
        persisted = conn.sql[-1][1][0]
        parsed_category, parsed_retryable = parse_stage3_error_evidence(persisted)
        label = getattr(exc, "signal", type(exc).__name__)
        check(f"P0-E[{label}] item and classifier agree on retryability",
              item.retryable == retryable, f"{item.retryable} vs {retryable}")
        check(f"P0-E[{label}] item and classifier agree on outcome",
              item.outcome == outcome, f"{item.outcome} vs {outcome}")
        check(f"P0-E[{label}] the durable marker round-trips the same verdict",
              (parsed_category, parsed_retryable) == (category, retryable),
              f"{parsed_category}/{parsed_retryable} vs {category}/{retryable}")


def test_p0e_writer_validation_error_owns_the_operator_not_a_replay() -> None:
    """Requirement 7: retry=no plus no committed rows must NOT be SAFE_REPLAY."""
    marker = format_stage3_error_evidence(
        "writer_validation_alpha_gps_rows_rejected", retryable=False) + "empty_result"
    decision = classify_stage3_recovery(
        stage3_status="ERROR", stage3_started_at=NOW - timedelta(hours=20),
        stage3_error=marker, now=NOW, evidence=_evidence(current=0, raw=0))
    check("P0-E a deterministic writer failure never auto-replays",
          decision.recovery_class == Stage3RecoveryClass.TERMINAL_OPERATOR, decision.reason)
    check("P0-E the refusal names the durable writer-validation category",
          decision.error_category == "writer_validation_alpha_gps_rows_rejected",
          str(decision.error_category))


def test_p0e_transient_error_still_recovers_autonomously() -> None:
    """Requirement 8: the retryable branch must keep working."""
    marker = format_stage3_error_evidence("candidate_load", retryable=True) + "connection lost"
    decision = classify_stage3_recovery(
        stage3_status="ERROR", stage3_started_at=NOW - timedelta(hours=20),
        stage3_error=marker, now=NOW, evidence=_evidence(current=0, raw=0))
    check("P0-E a transient failure is still replayed autonomously",
          decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)


def test_p0e_committed_attempt_still_outranks_retry_no() -> None:
    """Requirement 9: accepted precedence must survive this correction."""
    marker = format_stage3_error_evidence(
        "writer_validation_load_plan_rejected", retryable=False) + "duplicate record_id"
    decision = classify_stage3_recovery(
        stage3_status="ERROR", stage3_started_at=NOW - timedelta(hours=20),
        stage3_error=marker, now=NOW, evidence=_evidence(current=42, raw=42))
    check("P0-E committed current-attempt rows still beat a retry=no marker",
          decision.recovery_class == Stage3RecoveryClass.RECONCILE_COMMITTED, decision.reason)


def test_p0e_writer_validation_fails_the_cycle_as_non_retryable() -> None:
    exc = s3.Stage3WriterValidationError("empty", signal="alpha_gps_rows_rejected")
    item = s3._stage3_failure_item(_candidate(), exc, dry_run=False, force_reprocess=False)
    batch = Stage3BatchResult(items=[item])
    counts = batch.to_dict()
    check("P0-E a writer-validation failure fails the batch",
          batch.has_failures is True)
    check("P0-E it is counted as non-retryable, not as self-healing work",
          counts["non_retryable_failure_count"] == 1 and counts["retryable_failure_count"] == 0,
          str(counts))
    check("P0-E it has its own visible counter",
          counts["writer_validation_failure_count"] == 1, str(counts))


# --------------------------------------------------------------------------- #
# P0-E — postprocessor recovery safety is declared, not assumed
# --------------------------------------------------------------------------- #

def test_p0e_every_registered_postprocessor_declares_recovery_safety() -> None:
    for name, spec in registry.POSTPROCESSOR_REGISTRY.items():
        check(f"P0-E {name} declares an explicit recovery safety",
              spec.recovery_safety in set(registry.PostprocessorRecoverySafety),
              str(spec.recovery_safety))
        if spec.recovery_may_reoffer:
            check(f"P0-E {name} justifies why replay cannot duplicate its effect",
                  len(spec.recovery_safety_justification) > 40,
                  spec.recovery_safety_justification)


def test_p0e_report_207_safety_is_the_marker_not_convergence() -> None:
    """Codex: report 207 is NOT inherently convergent. The justification must
    say so, or a future change could 'preserve convergence' that never existed."""
    spec = registry.POSTPROCESSOR_REGISTRY[registry.REPORT_207_POSTPROCESSOR]
    text = spec.recovery_safety_justification.lower()
    check("P0-E report 207's justification states it is not convergent",
          "not convergent" in text, text)
    check("P0-E report 207's justification names the migrated_to_client_db marker",
          "migrated_to_client_db" in text, text)


def test_p0e_new_postprocessor_defaults_to_unsafe() -> None:
    spec = registry.PostprocessorSpec(
        "hypothetical", "report_207", None, lambda *_a: None, lambda _p: {},
        "telematics_reports", "report_207",
        frozenset({registry.PostprocessorExecutionMode.EXECUTE}),
    )
    check("P0-E an undeclared postprocessor is not recovery-safe by default",
          spec.recovery_safety is registry.PostprocessorRecoverySafety.UNKNOWN
          and spec.recovery_may_reoffer is False, str(spec.recovery_safety))


def test_p0e_recovered_identity_is_tagged() -> None:
    reconciled = Stage3ItemResult(
        raw_file_id=RAW_ID, client_code="BRAVO00016", report_type="report_207",
        outcome=Stage3Outcome.RECOVERED_RECONCILED,
        destination_schema="telematics_reports", destination_table="report_207",
        persisted_status="OK",
    )
    fresh = Stage3ItemResult(
        raw_file_id=RAW_ID, client_code="BRAVO00016", report_type="report_207",
        outcome=Stage3Outcome.LOADED,
        destination_schema="telematics_reports", destination_table="report_207",
        persisted_status="OK",
    )
    identities = Stage3BatchResult(items=[reconciled, fresh]).successful_load_identities
    by_recovered = {i.recovered for i in identities}
    check("P0-E both a reconciled and a fresh load yield identities",
          len(identities) == 2, str(identities))
    check("P0-E only the reconciled identity is tagged recovered",
          by_recovered == {True, False}, str(by_recovered))


def test_p0e_recovered_identity_cannot_reoffer_an_undeclared_postprocessor() -> None:
    from jobs.reports.workflow_b import orchestrator as job
    from jobs.reports.stage3.batch_contract import Stage3SuccessfulLoadIdentity

    identity = Stage3SuccessfulLoadIdentity(
        raw_file_id=RAW_ID, client_code="BRAVO00016", report_type="report_207",
        destination_schema="telematics_reports", destination_table="report_207",
        source_cleaned_artifact_id=ARTIFACT_NEW, final_status="OK", recovered=True,
    )
    unsafe = registry.PostprocessorSpec(
        registry.REPORT_207_POSTPROCESSOR, "report_207", None,
        lambda *_a: None, lambda _p: {}, "telematics_reports", "report_207",
        frozenset({registry.PostprocessorExecutionMode.EXECUTE}),
    )
    with Patch(registry, POSTPROCESSOR_REGISTRY={registry.REPORT_207_POSTPROCESSOR: unsafe}):
        try:
            job._discover_postprocessor_plans(Conn(), [identity])
            raised = None
        except job.Stage3RecoveryReofferNotDeclaredSafe as exc:
            raised = exc
    check("P0-E a recovered identity fails closed against an undeclared postprocessor",
          raised is not None, "no refusal raised")
    check("P0-E the refusal names the postprocessor that is not declared safe",
          raised is not None and registry.REPORT_207_POSTPROCESSOR in raised.unsafe_names,
          getattr(raised, "unsafe_names", None))


def test_p0e_fresh_identity_is_unaffected_by_the_reoffer_gate() -> None:
    from jobs.reports.workflow_b import orchestrator as job
    from jobs.reports.stage3.batch_contract import Stage3SuccessfulLoadIdentity

    identity = Stage3SuccessfulLoadIdentity(
        raw_file_id=RAW_ID, client_code="BRAVO00016", report_type="report_207",
        destination_schema="telematics_reports", destination_table="report_207",
        source_cleaned_artifact_id=ARTIFACT_NEW, final_status="OK", recovered=False,
    )
    unsafe = registry.PostprocessorSpec(
        registry.REPORT_207_POSTPROCESSOR, "report_207", None,
        lambda *_a: None, lambda _p: {}, "telematics_reports", "report_207",
        frozenset({registry.PostprocessorExecutionMode.EXECUTE}),
    )
    with Patch(registry, POSTPROCESSOR_REGISTRY={registry.REPORT_207_POSTPROCESSOR: unsafe}):
        try:
            job._discover_postprocessor_plans(Conn(), [identity])
            raised = None
        except job.Stage3RecoveryReofferNotDeclaredSafe as exc:
            raised = exc
    check("P0-E a freshly loaded identity is never blocked by the recovery gate",
          raised is None, repr(raised))


# --------------------------------------------------------------------------- #
# P0-E — batch visibility
# --------------------------------------------------------------------------- #

def test_p0e_blocked_recovery_is_operator_visible_every_cycle() -> None:
    decision = classify_stage3_recovery(
        stage3_status="SKIPPED_NO_RECORD_ID", stage3_started_at=None, now=NOW, evidence=None,
    )
    exc = s3.Stage3RecoveryNotOwnedError(
        decision, raw_file_id=RAW_ID, client_code="BRAVO00016", report_type="report_207")
    item = s3._stage3_failure_item(_candidate(), exc, dry_run=False, force_reprocess=False)
    batch = Stage3BatchResult(items=[item])
    check("P0-E a blocked recovery is BLOCKED_RECOVERY_OPERATOR",
          item.outcome == Stage3Outcome.BLOCKED_RECOVERY_OPERATOR, item.outcome.value)
    check("P0-E a blocked recovery stops the cycle reporting plain success",
          batch.has_failures is True and item.operator_action_required is True)
    check("P0-E a blocked recovery counts as non-retryable, not self-healing",
          batch.to_dict()["non_retryable_failure_count"] == 1, str(batch.to_dict()))


def test_p0e_deferred_ownership_is_a_skip_not_a_failure() -> None:
    for klass in (Stage3RecoveryClass.AWAIT_LIVE_OWNER, Stage3RecoveryClass.AWAIT_GRACE):
        from jobs.reports.stage3.recovery import Stage3RecoveryDecision
        exc = s3.Stage3RecoveryNotOwnedError(
            Stage3RecoveryDecision(klass, "held elsewhere"),
            raw_file_id=RAW_ID, client_code="BRAVO00016", report_type="report_207")
        item = s3._stage3_failure_item(_candidate(), exc, dry_run=False, force_reprocess=False)
        batch = Stage3BatchResult(items=[item])
        check(f"P0-E {klass.value} is a skip, not a failure",
              item.outcome == Stage3Outcome.SKIPPED_RECOVERY_DEFERRED
              and batch.has_failures is False
              and item.operator_action_required is False, item.outcome.value)


def test_p0e_running_and_error_are_discoverable() -> None:
    sql = s3._stage3_batch_status_eligible_sql("rf")
    check("P0-E a stale RUNNING row is admitted by discovery",
          "'RUNNING'" in sql and "stage3_started_at" in sql, sql)
    check("P0-E an ERROR row is admitted by discovery", "'ERROR'" in sql, sql)
    check("P0-E the stale grace is a bound parameter, not a literal",
          "make_interval(mins => %s)" in sql, sql)


def test_p0e_discovery_projects_the_attempt_scoped_columns() -> None:
    captured: dict = {}

    class SelectConn(Conn):
        def cursor(self):
            cur = Cursor(self)
            original = cur.execute

            def execute(sql, params=None):
                original(sql, params)
                captured["sql"] = " ".join(str(sql).split())
                captured["params"] = list(params or [])
                self.rows = []

            cur.execute = execute
            return cur

    s3._select_stage3_candidates(SelectConn())
    sql, params = captured["sql"], captured["params"]
    check("P0-E discovery projects the cleaned artifact needed for attempt scoping",
          "stage2_cleaned_artifact_id" in sql, sql[:300])
    check("P0-E discovery projects stage3_error, which carries retryability",
          "rf.stage3_error" in sql, sql[:300])
    check("P0-E the grace binds before the artifact parameters",
          params[:3] == [DEFAULT_STAGE3_STALE_GRACE_MINUTES, s3.WORKFLOW_NAME,
                         s3.STAGE2_CLEAN_STAGE], str(params))


def test_p0e_normal_execution_opens_no_client_connection() -> None:
    called: list[str] = []
    platform = Conn()
    with Patch(s3,
               _load_client_account_by_code=lambda *_a, **_k: called.append("account"),
               _client_business_pg_conn=lambda *_a, **_k: called.append("destination")):
        outcome = s3._resolve_stage3_recovery(platform, Client(), RUN_ID, _candidate())
    check("P0-E a never-attempted file takes the ordinary path", outcome is None, repr(outcome))
    check("P0-E a never-attempted file opens no client business connection", called == [], str(called))
    check("P0-E a never-attempted file writes nothing during classification",
          platform.sql == [] and platform.committed == 0, str(platform.sql))


# --------------------------------------------------------------------------- #
# P1-I — run lifecycle
# --------------------------------------------------------------------------- #

class RunClient:
    """Records the run lifecycle calls a job makes through the platform API."""

    def __init__(self, *, fail_finish_on: set[str] | None = None) -> None:
        self.fail_finish_on = fail_finish_on or set()
        self.started = 0
        self.finishes: list[str] = []
        self.logs: list[tuple] = []

    def start_run(self, **_kwargs) -> str:
        self.started += 1
        return RUN_ID

    def finish_run(self, run_id: str, status: str) -> None:
        self.finishes.append(status)
        if status in self.fail_finish_on:
            raise RuntimeError(f"platform API unreachable while writing {status}")

    def log(self, level, kind, source, message, **kwargs) -> None:
        self.logs.append((level, message, kwargs.get("context") or {}))

    def messages(self) -> list[str]:
        return [m for _, m, _ in self.logs]


def test_p1i_success_is_finalized_once() -> None:
    client = RunClient()
    with api_client.run_context(client, trigger="SCHEDULED", source="t") as run_id:
        check("P1-I the body receives the run id", run_id == RUN_ID, run_id)
    check("P1-I a successful run is marked SUCCESS exactly once",
          client.finishes == ["SUCCESS"], str(client.finishes))
    check("P1-I a successful run logs its terminal state",
          any("Run finished: SUCCESS" in m for m in client.messages()), str(client.messages()))


def test_p1i_application_failure_is_finalized_failed() -> None:
    client = RunClient()
    try:
        with api_client.run_context(client, trigger="SCHEDULED", source="t"):
            raise ValueError("the job itself broke")
        raised = None
    except ValueError as exc:
        raised = exc
    check("P1-I an application exception propagates untouched",
          isinstance(raised, ValueError) and str(raised) == "the job itself broke", repr(raised))
    check("P1-I an application failure is marked FAILED",
          client.finishes == ["FAILED"], str(client.finishes))
    check("P1-I an application failure logs its traceback",
          any(m.startswith("Run failed:") for m in client.messages()), str(client.messages()))


def test_p1i_finalization_failure_does_not_become_a_false_failed() -> None:
    """The core P1-I defect: a run that did all its work was recorded FAILED."""
    client = RunClient(fail_finish_on={"SUCCESS"})
    try:
        with api_client.run_context(client, trigger="SCHEDULED", source="t"):
            pass
        raised = None
    except BaseException as exc:
        raised = exc
    check("P1-I a finalization failure raises RunFinalizationError, not a generic failure",
          isinstance(raised, api_client.RunFinalizationError), repr(raised))
    check("P1-I the raised error states that the application succeeded",
          getattr(raised, "application_succeeded", None) is True, repr(raised))
    check("P1-I a successful run is NEVER downgraded to FAILED by a finalization failure",
          "FAILED" not in client.finishes, str(client.finishes))
    check("P1-I the run is not falsely logged as an application failure",
          not any(m.startswith("Run failed:") for m in client.messages()),
          str(client.messages()))


def test_p1i_finalization_failure_is_separately_observable() -> None:
    client = RunClient(fail_finish_on={"SUCCESS"})
    try:
        with api_client.run_context(client, trigger="SCHEDULED", source="t"):
            pass
    except api_client.RunFinalizationError:
        pass
    tagged = [
        ctx for level, msg, ctx in client.logs
        if ctx.get("classification") == api_client.RUN_FINALIZATION_FAILED
    ]
    check("P1-I the finalization failure carries a stable classification",
          len(tagged) == 1, str(client.logs))
    check("P1-I the record states the intended status and that the work succeeded",
          bool(tagged) and tagged[0]["intended_status"] == "SUCCESS"
          and tagged[0]["application_succeeded"] is True, str(tagged))


def test_p1i_finalization_failure_never_masks_the_primary_exception() -> None:
    client = RunClient(fail_finish_on={"FAILED"})
    try:
        with api_client.run_context(client, trigger="SCHEDULED", source="t"):
            raise KeyError("primary")
        raised = None
    except BaseException as exc:
        raised = exc
    check("P1-I the application exception still wins when FAILED cannot be written",
          isinstance(raised, KeyError), repr(raised))
    tagged = [
        ctx for _, _, ctx in client.logs
        if ctx.get("classification") == api_client.RUN_FINALIZATION_FAILED
    ]
    check("P1-I a stranded RUNNING row now has a stated cause instead of a silent pass",
          len(tagged) == 1 and tagged[0]["intended_status"] == "FAILED", str(client.logs))
    check("P1-I the stranded-row record preserves the primary exception",
          bool(tagged) and "KeyError" in str(tagged[0].get("primary_exception")), str(tagged))


def test_p1i_logging_failure_cannot_replace_the_diagnosis() -> None:
    """The API is what just failed, so the error logger may not raise over it."""

    class MuteClient(RunClient):
        """Logging works long enough to open the run, then dies with the API.

        Failing the very first "Run started" log would make the logger the
        primary exception, which is a different (and pre-existing) behaviour;
        the invariant under test is that a logger failure while *handling* a
        failure cannot overwrite the diagnosis.
        """

        def log(self, *args, **kwargs):
            if self.logs or self.finishes:
                raise RuntimeError("logging endpoint is down too")
            return super().log(*args, **kwargs)

    client = MuteClient(fail_finish_on={"FAILED"})
    try:
        with api_client.run_context(client, trigger="SCHEDULED", source="t"):
            raise ValueError("primary")
        raised = None
    except BaseException as exc:
        raised = exc
    check("P1-I a failing logger does not replace the application exception",
          isinstance(raised, ValueError) and str(raised) == "primary", repr(raised))


def test_p1i_interruption_leaves_a_deterministically_stale_row() -> None:
    """A killed process writes nothing; the watchdog contract is what finds it."""
    from ops import execution_watchdog as wd

    started = NOW - timedelta(minutes=wd.DEFAULT_STALE_GRACE_MINUTES + 1)
    expectation = wd.SystemdExpectation(
        subject="workflow_b_orchestrator",
        component="workflow_b.orchestrator",
        run_source="jobs.reports.workflow_b.orchestrator",
        times=("06:00", "20:00"),
    )
    observation = wd.evaluate_systemd_subject(
        expectation=expectation,
        fire_utc=started,
        run={"run_id": RUN_ID, "status": "RUNNING", "started_at": started},
        now_utc=NOW,
    )
    check("P1-I an interrupted run is deterministically classified STALE",
          observation.verdict == wd.VERDICT_STALE, observation.verdict)
    check("P1-I the stale verdict raises an operator incident",
          observation.incident_code == wd.INCIDENT_SCHEDULED_RUN_STALE,
          str(observation.incident_code))

    healthy = wd.evaluate_systemd_subject(
        expectation=expectation,
        fire_utc=NOW - timedelta(minutes=2),
        run={"run_id": RUN_ID, "status": "RUNNING", "started_at": NOW - timedelta(minutes=2)},
        now_utc=NOW,
    )
    check("P1-I a healthy in-flight run is not called stale",
          healthy.verdict == wd.VERDICT_IN_WINDOW, healthy.verdict)


def test_p1i_file_and_run_staleness_are_deliberately_ordered() -> None:
    """The two graces answer different questions and must not be equated.

    An earlier version of this suite asserted they were the same number. That
    was wrong, and independent review is why: the *file* bound has to clear
    every legitimate execution lifetime (`TimeoutStartSec=6h`) before a file may
    be taken from a possible owner, whereas the watchdog's *run* bound only has
    to notice that a row stopped moving. Forcing them equal is what produced a
    240-minute file grace that could steal a live 5-hour load.

    What must hold is the ordering: the file is never declared recoverable
    before the watchdog would already have raised the run as STALE, so an
    operator always sees the run incident first and never finds a file recovered
    behind a run still believed healthy.
    """
    from ops import execution_watchdog as wd

    check("P1-I the file bound is not below the watchdog's run bound",
          DEFAULT_STAGE3_STALE_GRACE_MINUTES >= wd.DEFAULT_STALE_GRACE_MINUTES,
          f"file={DEFAULT_STAGE3_STALE_GRACE_MINUTES} run={wd.DEFAULT_STALE_GRACE_MINUTES}")
    check("P1-I the file bound clears the largest supported execution timeout",
          DEFAULT_STAGE3_STALE_GRACE_MINUTES > 6 * 60,
          str(DEFAULT_STAGE3_STALE_GRACE_MINUTES))


# --------------------------------------------------------------------------- #
# PostgreSQL: real tables, real provenance, real transactions
# --------------------------------------------------------------------------- #

def _assert_disposable(dsn: str) -> None:
    lowered = dsn.lower()
    for forbidden in ("logdb", "prod"):
        if forbidden in lowered:
            raise SystemExit(
                f"refusing to run destructive tests against a DSN containing {forbidden!r}"
            )


TELEMATICS_DDL = """
DROP SCHEMA IF EXISTS telematics_reports CASCADE;
CREATE SCHEMA telematics_reports;
CREATE TABLE telematics_reports.report_207 (
    record_id TEXT,
    payload TEXT,
    _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    _raw_file_id TEXT,
    _source_artifact_id TEXT,
    _source_filename TEXT,
    _stage3_run_id TEXT
);
"""

ALPHA_DDL = """
DROP SCHEMA IF EXISTS telematics_reports CASCADE;
CREATE SCHEMA telematics_reports;
CREATE TABLE telematics_reports."Alpha_GPS_Baza_LOG" (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT,
    registration TEXT,
    assignment_date DATE,
    csv_filename TEXT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    workflow_run_id TEXT,
    raw_file_id TEXT,
    source_artifact_id TEXT,
    normalized_artifact_id TEXT,
    cleaned_artifact_id TEXT,
    source_sha256 TEXT,
    source_row_number INTEGER,
    raw_row_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""


def _probe(conn, *, strategy, schema, table, artifact):
    return probe_destination_evidence(
        conn,
        strategy=strategy,
        destination_schema=schema,
        destination_table=table,
        raw_file_id=RAW_ID,
        cleaned_artifact_id=artifact,
    )


def test_pg_telematics_attempt_scoping(dsn: str) -> None:
    """Supersede/reclean, against a real table with real provenance rows."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(TELEMATICS_DDL)
        conn.commit()

        # A previous generation loaded successfully.
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO telematics_reports.report_207"
                " (record_id, payload, _raw_file_id, _source_artifact_id, _stage3_run_id)"
                " VALUES (%s,%s,%s,%s,%s)",
                [(f"r{i}", "old", RAW_ID, ARTIFACT_OLD, "run-old") for i in range(5)],
            )
        conn.commit()

        ev = _probe(conn, strategy=Stage3LoadStrategy.TELEMATICS_TECHNICAL,
                    schema="telematics_reports", table="report_207", artifact=ARTIFACT_NEW)
        check("P0-E/pg old rows exist for the raw file", ev.rows_for_raw_file == 5, str(ev))
        check("P0-E/pg but none belong to the new attempt", ev.rows_for_current_attempt == 0, str(ev))
        decision = classify_stage3_recovery(
            stage3_status="RUNNING", stage3_started_at=NOW - timedelta(hours=20),
            now=NOW, evidence=ev)
        check("P0-E/pg SUPERSEDE: a crash before the new commit must replay, not reconcile",
              decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)

        # The positive case: the new attempt commits.
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO telematics_reports.report_207"
                " (record_id, payload, _raw_file_id, _source_artifact_id, _stage3_run_id)"
                " VALUES (%s,%s,%s,%s,%s)",
                [(f"n{i}", "new", RAW_ID, ARTIFACT_NEW, RUN_ID) for i in range(3)],
            )
        conn.commit()
        ev = _probe(conn, strategy=Stage3LoadStrategy.TELEMATICS_TECHNICAL,
                    schema="telematics_reports", table="report_207", artifact=ARTIFACT_NEW)
        decision = classify_stage3_recovery(
            stage3_status="RUNNING", stage3_started_at=NOW - timedelta(hours=20),
            now=NOW, evidence=ev)
        check("P0-E/pg the committed new attempt is recognized",
              ev.rows_for_current_attempt == 3
              and decision.recovery_class == Stage3RecoveryClass.RECONCILE_COMMITTED,
              f"{ev} {decision.reason}")

        # A rolled-back attempt must leave no evidence at all.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telematics_reports.report_207"
                " (record_id, payload, _raw_file_id, _source_artifact_id, _stage3_run_id)"
                " VALUES ('rb','rolled', %s, %s, 'run-rb')",
                (RAW_ID, "cccccccc-0000-0000-0000-000000000003"),
            )
        conn.rollback()
        ev = _probe(conn, strategy=Stage3LoadStrategy.TELEMATICS_TECHNICAL,
                    schema="telematics_reports", table="report_207",
                    artifact="cccccccc-0000-0000-0000-000000000003")
        check("P0-E/pg a rolled-back attempt leaves no committed evidence",
              ev.rows_for_current_attempt == 0, str(ev))

        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA telematics_reports CASCADE")
        conn.commit()


def test_pg_alpha_gps_recovery(dsn: str) -> None:
    """BLOCKER 1, against the real Alpha GPS destination contract."""
    import psycopg
    from psycopg.rows import dict_row

    strategy = Stage3LoadStrategy.ALPHA_GPS_REPLACE_ALL
    schema, table = "telematics_reports", "Alpha_GPS_Baza_LOG"

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(ALPHA_DDL)
        conn.commit()

        # 1. RUNNING, crash before the replace-all commits: table still empty.
        ev = _probe(conn, strategy=strategy, schema=schema, table=table, artifact=ARTIFACT_NEW)
        check("P0-E/pg[alpha] an empty destination is READABLE, not uninspectable",
              ev.readable and ev.rows_for_current_attempt == 0, str(ev))
        decision = classify_stage3_recovery(
            stage3_status="RUNNING", stage3_started_at=NOW - timedelta(hours=20),
            now=NOW, evidence=ev)
        check("P0-E/pg[alpha] crash before commit replays",
              decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY, decision.reason)

        # 2. Replace-all opened then rolled back leaves nothing.
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO {schema}."{table}"'
                " (source_id, raw_file_id, cleaned_artifact_id, workflow_run_id)"
                " VALUES ('x', %s, %s, %s)", (RAW_ID, ARTIFACT_NEW, RUN_ID))
        conn.rollback()
        ev = _probe(conn, strategy=strategy, schema=schema, table=table, artifact=ARTIFACT_NEW)
        check("P0-E/pg[alpha] a rolled-back replace-all leaves no attempt evidence",
              ev.rows_for_current_attempt == 0, str(ev))

        # 4. A prior successful load exists; a superseding attempt crashes
        #    before its own commit. Old rows must not count.
        with conn.cursor() as cur:
            cur.executemany(
                f'INSERT INTO {schema}."{table}"'
                " (source_id, raw_file_id, cleaned_artifact_id, workflow_run_id)"
                " VALUES (%s,%s,%s,%s)",
                [(f"old{i}", RAW_ID, ARTIFACT_OLD, "run-old") for i in range(44)])
        conn.commit()
        ev = _probe(conn, strategy=strategy, schema=schema, table=table, artifact=ARTIFACT_NEW)
        decision = classify_stage3_recovery(
            stage3_status="RUNNING", stage3_started_at=NOW - timedelta(hours=20),
            now=NOW, evidence=ev)
        check("P0-E/pg[alpha] SUPERSEDE: 44 old rows do not prove the new attempt committed",
              ev.rows_for_raw_file == 44 and ev.rows_for_current_attempt == 0
              and decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY,
              f"{ev} {decision.reason}")

        # 3. Replace-all committed, platform finalization missing.
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {schema}."{table}"')
            cur.executemany(
                f'INSERT INTO {schema}."{table}"'
                " (source_id, raw_file_id, cleaned_artifact_id, workflow_run_id)"
                " VALUES (%s,%s,%s,%s)",
                [(f"new{i}", RAW_ID, ARTIFACT_NEW, RUN_ID) for i in range(7)])
        conn.commit()
        ev = _probe(conn, strategy=strategy, schema=schema, table=table, artifact=ARTIFACT_NEW)
        decision = classify_stage3_recovery(
            stage3_status="RUNNING", stage3_started_at=NOW - timedelta(hours=20),
            now=NOW, evidence=ev)
        check("P0-E/pg[alpha] a committed replace-all is reconciled, never reloaded",
              decision.recovery_class == Stage3RecoveryClass.RECONCILE_COMMITTED, decision.reason)

        # 5. Zero-row success: the workbook parsed empty, so the commit is a
        #    bare DELETE. Replay is the safe answer because the writer converges.
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {schema}."{table}"')
        conn.commit()
        ev = _probe(conn, strategy=strategy, schema=schema, table=table, artifact=ARTIFACT_NEW)
        decision = classify_stage3_recovery(
            stage3_status="RUNNING", stage3_started_at=NOW - timedelta(hours=20),
            now=NOW, evidence=ev)
        check("P0-E/pg[alpha] a zero-row commit replays safely rather than being stranded",
              ev.readable and decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY,
              f"{ev} {decision.reason}")

        # A table that lacks the provenance columns is unreadable, not empty.
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE {schema}."NoProvenance" (id int)')
        conn.commit()
        ev = _probe(conn, strategy=strategy, schema=schema, table="NoProvenance",
                    artifact=ARTIFACT_NEW)
        check("P0-E/pg[alpha] a destination without provenance is unreadable, not zero",
              ev.readable is False and ev.unreadable_reason is not None, str(ev))

        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA telematics_reports CASCADE")
        conn.commit()


def test_pg_wrong_provenance_column_would_have_failed(dsn: str) -> None:
    """Regression guard for BLOCKER 1: probing Alpha GPS with the telematics
    provenance must be impossible, because the column does not exist."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(ALPHA_DDL)
            cur.execute(
                'INSERT INTO telematics_reports."Alpha_GPS_Baza_LOG"'
                " (source_id, raw_file_id, cleaned_artifact_id) VALUES ('a', %s, %s)",
                (RAW_ID, ARTIFACT_NEW))
        conn.commit()

        wrong = _probe(conn, strategy=Stage3LoadStrategy.TELEMATICS_TECHNICAL,
                       schema="telematics_reports", table="Alpha_GPS_Baza_LOG",
                       artifact=ARTIFACT_NEW)
        check("P0-E/pg the telematics provenance genuinely does not exist on Alpha GPS",
              wrong.readable is False, str(wrong))
        right = _probe(conn, strategy=Stage3LoadStrategy.ALPHA_GPS_REPLACE_ALL,
                       schema="telematics_reports", table="Alpha_GPS_Baza_LOG",
                       artifact=ARTIFACT_NEW)
        check("P0-E/pg the correct strategy classifies the same table autonomously",
              right.readable and right.rows_for_current_attempt == 1, str(right))

        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA telematics_reports CASCADE")
        conn.commit()


def test_pg_report_207_cannot_double_increment(dsn: str) -> None:
    """Report 207 is not convergent. Prove the marker — not convergence — is
    what stops a recovery re-offer from incrementing counters twice, by running
    the real selection/marking shape twice against real rows."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS p207 CASCADE; CREATE SCHEMA p207")
            cur.execute("""
                CREATE TABLE p207.report_207 (
                    ctid_key SERIAL PRIMARY KEY,
                    provider_trip_id TEXT,
                    bucket TEXT,
                    migrated_to_client_db BOOLEAN NOT NULL DEFAULT FALSE,
                    migrated_to_client_db_at TIMESTAMPTZ,
                    migrated_to_client_db_error TEXT
                )""")
            cur.execute("""
                CREATE TABLE p207.client_trips (
                    provider_trip_id TEXT PRIMARY KEY,
                    speeding_140_160_count INTEGER NOT NULL DEFAULT 0
                )""")
            cur.execute("INSERT INTO p207.client_trips VALUES ('T1', 0)")
            cur.executemany(
                "INSERT INTO p207.report_207 (provider_trip_id, bucket) VALUES (%s,%s)",
                [("T1", "speeding_140_160_count")] * 3)
        conn.commit()

        # The real shape: candidate selection is the marker predicate, and the
        # increment plus the marking are CTEs of ONE statement.
        migrate = """
        WITH candidates AS (
            SELECT ctid_key, provider_trip_id
            FROM p207.report_207
            WHERE COALESCE(migrated_to_client_db, FALSE) IS NOT TRUE
        ),
        increments AS (
            SELECT provider_trip_id, count(*)::int AS inc
            FROM candidates GROUP BY provider_trip_id
        ),
        updated_trips AS (
            UPDATE p207.client_trips t
               SET speeding_140_160_count = COALESCE(t.speeding_140_160_count,0) + i.inc
            FROM increments i WHERE t.provider_trip_id = i.provider_trip_id
            RETURNING 1
        ),
        marked_migrated AS (
            UPDATE p207.report_207 r
               SET migrated_to_client_db = TRUE, migrated_to_client_db_at = now()
            FROM candidates c WHERE r.ctid_key = c.ctid_key
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM marked_migrated)::int AS migrated
        """
        with conn.cursor() as cur:
            cur.execute(migrate)
            first = cur.fetchone()["migrated"]
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT speeding_140_160_count c FROM p207.client_trips")
            after_first = cur.fetchone()["c"]

        # The recovery re-offer: exactly the same call again.
        with conn.cursor() as cur:
            cur.execute(migrate)
            second = cur.fetchone()["migrated"]
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT speeding_140_160_count c FROM p207.client_trips")
            after_second = cur.fetchone()["c"]

        check("P0-E/pg[207] the first application migrates and increments",
              first == 3 and after_first == 3, f"migrated={first} count={after_first}")
        check("P0-E/pg[207] a recovery re-offer selects nothing", second == 0, str(second))
        check("P0-E/pg[207] counters are NOT incremented twice",
              after_second == after_first == 3, f"{after_first} -> {after_second}")

        # And the falsification: without the marker, the same re-offer doubles.
        with conn.cursor() as cur:
            cur.execute("UPDATE p207.report_207 SET migrated_to_client_db = FALSE")
            cur.execute(migrate)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT speeding_140_160_count c FROM p207.client_trips")
            unmarked = cur.fetchone()["c"]
        check("P0-E/pg[207] clearing the marker DOES double the counters, proving the marker "
              "is the safety mechanism rather than convergence",
              unmarked == 6, str(unmarked))

        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA p207 CASCADE")
        conn.commit()


def test_pg_file_ownership_lock_is_real(dsn: str) -> None:
    """Liveness must come from a lock a second live session genuinely cannot take."""
    import psycopg
    from psycopg.rows import dict_row

    key = stage3_file_advisory_lock_key(RAW_ID)
    with psycopg.connect(dsn, row_factory=dict_row) as owner:
        with owner.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            check("P0-E/pg the owning session takes the file lock", cur.fetchone()["ok"] is True)

        with psycopg.connect(dsn, row_factory=dict_row) as other:
            with other.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
                check("P0-E/pg a second live session cannot steal the file",
                      cur.fetchone()["ok"] is False)
            other.rollback()

    # The owner's connection has now closed, exactly as a crashed process's does.
    with psycopg.connect(dsn, row_factory=dict_row) as after:
        with after.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            check("P0-E/pg a dead owner's lock is released, so an orphan becomes recoverable",
                  cur.fetchone()["ok"] is True)
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        after.rollback()


def test_p0e_eligibility_against_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS p0e_rf")
            cur.execute("""
                CREATE TABLE p0e_rf (
                    id uuid PRIMARY KEY, label text, stage3_status text,
                    stage3_started_at timestamptz, stage3_finished_at timestamptz,
                    stage2_updated_at timestamptz)""")
            rows = [
                ("never attempted", None, None, None, None, True),
                ("empty status", "", None, None, None, True),
                ("completed", "OK", None, "now", "old", False),
                ("superseded by stage 2", "OK", None, "old", "now", True),
                ("running, 5 hours in", "RUNNING", "five_hours", None, None, False),
                ("running, beyond every bound", "RUNNING", "stale", None, None, True),
                ("running, no start time", "RUNNING", None, None, None, True),
                ("error", "ERROR", None, "old", "old", True),
                ("skipped no record id", "SKIPPED_NO_RECORD_ID", None, "old", "old", False),
            ]
            expected = {}
            for label, status, started, finished, updated, want in rows:
                def ts(token):
                    return {
                        None: "NULL",
                        "five_hours": "now() - interval '5 hours'",
                        "stale": "now() - interval '9 hours'",
                        "old": "now() - interval '3 days'",
                        "now": "now()",
                    }[token]
                cur.execute(
                    f"""INSERT INTO p0e_rf VALUES (gen_random_uuid(), %s, %s,
                        {ts(started)}, {ts(finished)}, {ts(updated)})""",
                    (label, status))
                expected[label] = want

            predicate = s3._stage3_batch_status_eligible_sql("rf")
            cur.execute(f"SELECT label FROM p0e_rf rf WHERE {predicate}",
                        (DEFAULT_STAGE3_STALE_GRACE_MINUTES,))
            selected = {r["label"] for r in cur.fetchall()}
            for label, want in expected.items():
                check(f"P0-E/pg {'selects' if want else 'excludes'} {label!r}",
                      (label in selected) == want, f"selected={sorted(selected)}")
            cur.execute("DROP TABLE p0e_rf")
        conn.commit()


def test_p1i_transition_guard_against_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS p1i_runs")
            cur.execute("CREATE TABLE p1i_runs (run_id uuid PRIMARY KEY, status text,"
                        " ended_at timestamptz)")
            cur.execute("INSERT INTO p1i_runs VALUES (%s, 'RUNNING', NULL)", (RUN_ID,))
            guard = """
                UPDATE p1i_runs SET status=%s, ended_at=now()
                WHERE run_id=%s
                  AND (status IS NULL OR status NOT IN ('SUCCESS','FAILED','CANCELED') OR status=%s)
                RETURNING run_id
            """
            cur.execute(guard, ("FAILED", RUN_ID, "FAILED"))
            check("P1-I/pg the first terminalization of a RUNNING row succeeds",
                  cur.fetchone() is not None)
            cur.execute(guard, ("SUCCESS", RUN_ID, "SUCCESS"))
            check("P1-I/pg a settled FAILED cannot be rewritten to SUCCESS",
                  cur.fetchone() is None)
            cur.execute(guard, ("FAILED", RUN_ID, "FAILED"))
            check("P1-I/pg repeating the same terminal status stays idempotent",
                  cur.fetchone() is not None)
            cur.execute("SELECT status FROM p1i_runs WHERE run_id=%s", (RUN_ID,))
            check("P1-I/pg the durable outcome is the one recorded first",
                  cur.fetchone()["status"] == "FAILED")
            cur.execute("DROP TABLE p1i_runs")
        conn.commit()


def main() -> int:
    print("=== P0-E: attempt identity is content identity ===")
    test_p0e_superseded_old_rows_do_not_prove_current_commit()
    test_p0e_current_attempt_rows_do_prove_commit()
    test_p0e_raw_file_count_alone_never_decides()

    print("\n=== P0-E: per-strategy provenance ===")
    test_p0e_alpha_gps_provenance_is_not_underscore_prefixed()
    test_p0e_strategy_resolution_matches_the_loader_branch()
    test_p0e_unknown_strategy_is_never_replayed()

    print("\n=== P0-E: zero-row success and ambiguity ===")
    test_p0e_zero_rows_is_not_proof_of_no_commit()
    test_p0e_unreadable_evidence_is_never_replay_or_success()

    print("\n=== P0-E: RUNNING ownership is a lock, not a clock ===")
    test_p0e_live_owner_outranks_the_clock()
    test_p0e_grace_clears_every_supported_execution_bound()
    test_p0e_grace_still_recovers_within_one_cycle()
    test_p0e_young_running_with_free_lock_stands_aside()
    test_p0e_running_without_started_at_is_recoverable()
    test_p0e_lock_key_is_stable_namespaced_and_bounded()
    test_p0e_every_candidate_claims_ownership_before_loading()
    test_p0e_ownership_is_released_after_processing()
    test_p0e_ownership_is_released_even_when_the_load_raises()

    print("\n=== P0-E: durable ERROR retryability ===")
    test_p0e_error_evidence_round_trips()
    test_p0e_retryable_error_has_an_autonomous_owner()
    test_p0e_non_retryable_error_does_not_red_cycle_forever()
    test_p0e_error_after_commit_reconciles_regardless_of_retryability()
    test_p0e_unmarked_error_is_operator_owned()
    test_p0e_error_marker_uses_the_same_classifier_as_the_item()
    test_p0e_p0g_block_stays_null_and_is_not_recovery()

    print("\n=== P0-E: deterministic writer validation is not retryable ===")
    test_p0e_alpha_gps_empty_result_is_not_retryable()
    test_p0e_alpha_gps_malformed_row_is_not_retryable()
    test_p0e_missing_required_alpha_gps_columns_is_not_retryable()
    test_p0e_duplicate_index_refusal_is_not_retryable()
    test_p0e_column_validation_families_are_not_retryable()
    test_p0e_unsafe_identifier_and_unusable_artifact_are_not_retryable()
    test_p0e_transient_families_remain_retryable()
    test_p0e_no_retryability_decision_depends_on_message_text()
    test_p0e_durable_marker_matches_the_typed_result_for_every_family()
    test_p0e_writer_validation_error_owns_the_operator_not_a_replay()
    test_p0e_transient_error_still_recovers_autonomously()
    test_p0e_committed_attempt_still_outranks_retry_no()
    test_p0e_writer_validation_fails_the_cycle_as_non_retryable()

    print("\n=== P0-E: postprocessor recovery safety is declared ===")
    test_p0e_every_registered_postprocessor_declares_recovery_safety()
    test_p0e_report_207_safety_is_the_marker_not_convergence()
    test_p0e_new_postprocessor_defaults_to_unsafe()
    test_p0e_recovered_identity_is_tagged()
    test_p0e_recovered_identity_cannot_reoffer_an_undeclared_postprocessor()
    test_p0e_fresh_identity_is_unaffected_by_the_reoffer_gate()

    print("\n=== P0-E: batch visibility ===")
    test_p0e_blocked_recovery_is_operator_visible_every_cycle()
    test_p0e_deferred_ownership_is_a_skip_not_a_failure()
    test_p0e_running_and_error_are_discoverable()
    test_p0e_discovery_projects_the_attempt_scoped_columns()
    test_p0e_normal_execution_opens_no_client_connection()

    print("\n=== P1-I: the public.runs lifecycle ===")
    test_p1i_success_is_finalized_once()
    test_p1i_application_failure_is_finalized_failed()
    test_p1i_finalization_failure_does_not_become_a_false_failed()
    test_p1i_finalization_failure_is_separately_observable()
    test_p1i_finalization_failure_never_masks_the_primary_exception()
    test_p1i_logging_failure_cannot_replace_the_diagnosis()
    test_p1i_interruption_leaves_a_deterministically_stale_row()
    test_p1i_file_and_run_staleness_are_deliberately_ordered()

    dsn = os.getenv("WORKFLOW_B_RECOVERY_TEST_DSN")
    print("\n=== PostgreSQL: real tables, real provenance, real transactions ===")
    if not dsn:
        print("SKIP: WORKFLOW_B_RECOVERY_TEST_DSN unset - PostgreSQL tests not run")
    else:
        _assert_disposable(dsn)
        test_pg_telematics_attempt_scoping(dsn)
        test_pg_alpha_gps_recovery(dsn)
        test_pg_wrong_provenance_column_would_have_failed(dsn)
        test_pg_report_207_cannot_double_increment(dsn)
        test_pg_file_ownership_lock_is_real(dsn)
        test_p0e_eligibility_against_postgres(dsn)
        test_p1i_transition_guard_against_postgres(dsn)

    print()
    if FAILURES:
        print(f"FAIL - {len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("OK - Workflow B P0-E/P1-I recovery regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
