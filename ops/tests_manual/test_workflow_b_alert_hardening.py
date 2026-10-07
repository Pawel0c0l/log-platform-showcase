#!/usr/bin/env python3
"""Deterministic tests for the Workflow B alert-hardening slice (P0-A, P0-F, P1-H).

The audit found three defects that this suite pins:

  P0-A  Every incident raised from inside a job process was persisted with
        `email_decision='suppressed'`, `reason='recipients_not_configured'`,
        because `log-workflow-b.service` sourced no EnvironmentFile and
        `SUSPECTED_BUG_ALERT_TO` lives only in root-owned
        /etc/log-platform/runtime.env. Transport worked; rich alerts never left.

  P0-F  The email worker returned 0 whatever happened, so a dead-lettered alert
        ended as an unnoticed row, and a stopped worker looked exactly like an
        idle one.

  P1-H  A failed orchestration persisted no structured outcome, and
        `_invoke_stage` caught bare `Exception` without binding it, discarding
        the type, message and traceback of the one failure class that has no
        typed partial result.

The pure tests need nothing but stdlib. The persistence tests need a throwaway
database and refuse to touch `logdb`:

    PYTHONPATH="$PWD" .venv/bin/python \\
        ops/tests_manual/test_workflow_b_alert_hardening.py

    ALERT_HARDENING_TEST_DSN='postgresql://u:p@127.0.0.1:55433/disposable' \\
        PYTHONPATH="$PWD" .venv/bin/python \\
        ops/tests_manual/test_workflow_b_alert_hardening.py

No real SMTP is ever used: delivery is exercised with an in-memory transport.
"""
from __future__ import annotations

import os
import re
import smtplib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The orchestrator imports Stage 2/3, which import pandas. The alerting contract
# under test does not, so a stub keeps this suite runnable without the heavy dep.
try:  # pragma: no cover - environment dependent
    import pandas  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    _pandas = types.ModuleType("pandas")
    _pandas.DataFrame = type("DataFrame", (), {})
    _pandas.Series = type("Series", (), {})
    sys.modules["pandas"] = _pandas

import api.suspected_bug as sb  # noqa: E402
import ops.operational_alert as oa  # noqa: E402
import ops.suspected_bug_email_worker as worker  # noqa: E402
from jobs.mail.stage1_batch_contract import Stage1BatchResult  # noqa: E402
from jobs.reports.stage2.batch_contract import Stage2BatchResult  # noqa: E402
from jobs.reports.stage3.batch_contract import (  # noqa: E402
    Stage3BatchResult,
    Stage3ItemResult,
    Stage3Outcome,
)
from jobs.reports.workflow_b import orchestrator as job  # noqa: E402
from ops.systemd_environment_files import (  # noqa: E402
    RUNTIME_ENVIRONMENT_FILE,
    merged_dropin_text,
    parse_environment_file,
    unit_environment,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 15, 18, 0, tzinfo=UTC)
RUN_ID = "77777777-7777-7777-7777-777777777777"
RAW_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"

SYSTEMD_DIR = REPO_ROOT / "ops" / "systemd" / "proposed"

# A realistic redaction of the production runtime.env: only the keys that decide
# whether alerting works. The real file is root:root 0600 and is never read here.
RUNTIME_ENV_TEXT = """\
# platform runtime configuration
POSTGRES_HOST=127.0.0.1
SUSPECTED_BUG_ALERT_TO=ops@example.invalid
AUTOMATION_SMTP_HOST=smtp.example.invalid
AUTOMATION_SMTP_FROM=automation@example.invalid
"""
IDENTITY_ENV_TEXT = "LOG_PLATFORM_TARGET_ENVIRONMENT=production\n"


# --------------------------------------------------------------- helpers


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.conn.sql.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self.row = {"pg_try_advisory_lock": self.conn.locked}
        elif "pg_advisory_unlock" in sql:
            self.row = {"pg_advisory_unlock": True}

    def fetchone(self):
        return self.row


class Conn:
    def __init__(self, locked=True):
        self.locked = locked
        self.sql = []
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class Client:
    def __init__(self):
        self.logs = []

    def log(self, level, kind, source, message, **kwargs):
        self.logs.append({"level": level, "message": message, **kwargs})

    def find(self, message):
        return [item for item in self.logs if item["message"] == message]


class Patch:
    def __init__(self, target, **values):
        self.target = target
        self.values = values
        self.originals = {}

    def __enter__(self):
        for name, value in self.values.items():
            self.originals[name] = getattr(self.target, name)
            setattr(self.target, name, value)
        return self

    def __exit__(self, *_args):
        for name, value in self.originals.items():
            setattr(self.target, name, value)


def unit_text(unit_name: str) -> str:
    """Base unit plus its drop-ins, in the order systemd merges them."""
    base = (SYSTEMD_DIR / unit_name).read_text(encoding="utf-8")
    dropins = merged_dropin_text(SYSTEMD_DIR / f"{unit_name}.d")
    return f"{base}\n{dropins}" if dropins else base


def stage3_loaded() -> Stage3BatchResult:
    result = Stage3BatchResult(discovered_candidate_count=1, eligible_count=1)
    result.items.append(
        Stage3ItemResult(
            raw_file_id=RAW_ID,
            client_code="BRAVO00016",
            report_type="report_207",
            outcome=Stage3Outcome.LOADED,
            destination_schema="telematics_reports",
            destination_table="report_207",
            persisted_status="OK",
            inserted_rows=1200,
        )
    )
    return result


# ------------------------------------------------- P0-A: env propagation


def test_workflow_b_unit_supplies_the_authoritative_alert_configuration() -> None:
    """The real service boundary, not `load_alert_config()` in isolation.

    The defect never lived in the loader — the loader was correct and reported
    exactly what it was given. It lived in the unit definition. So the assertion
    has to start from the shipped unit text and end at a resolved config.
    """
    text = unit_text("log-workflow-b.service")
    env = unit_environment(
        text,
        file_contents={
            str(RUNTIME_ENVIRONMENT_FILE): RUNTIME_ENV_TEXT,
            "/etc/log-platform/environment-identity.env": IDENTITY_ENV_TEXT,
        },
    )
    config = sb.load_alert_config(env)
    assert config.recipients_configured, "unit does not supply SUSPECTED_BUG_ALERT_TO"
    assert config.environment == "production"

    readiness = oa.AlertingReadiness(config, env=env)
    assert readiness.ready, readiness.problems

    # And the decision that actually mattered in production: with this env a
    # brand-new incident is enqueued rather than suppressed.
    decision = sb._decide_email(
        config=config,
        incident_created=True,
        previous_state=None,
        previous_material_signature=None,
        previous_email_enqueued_at=None,
        material_signature="sig",
        now=NOW,
    )
    assert decision.enqueue
    assert decision.suppression_reason is None
    print("PASS: the Workflow B unit resolves a configured recipient end to end")


def test_every_incident_raising_job_unit_sources_the_same_configuration() -> None:
    """One authoritative source, applied to every unit that can raise incidents.

    `log-job@retention-purge.service` and `log-job@dispatcher.service` already
    had this pair; Workflow B was the outlier. Asserting the whole set stops the
    next unit from being added without it.
    """
    from ops.systemd_environment_files import effective_environment_files

    incident_raising_units = (
        "log-workflow-b.service",
        "log-job@dispatcher.service",
        "log-job@retention-purge.service",
    )
    for unit in incident_raising_units:
        files = effective_environment_files(unit_text(unit))
        assert str(RUNTIME_ENVIRONMENT_FILE) in files, f"{unit} cannot resolve recipients"
        assert "/etc/log-platform/environment-identity.env" in files, unit

    # The delivery side must read the same file, or "authoritative" is a claim
    # rather than a fact.
    for unit in ("suspected-bug-email-worker.service", "execution-watchdog.service"):
        files = effective_environment_files((SYSTEMD_DIR / unit).read_text(encoding="utf-8"))
        assert str(RUNTIME_ENVIRONMENT_FILE) in files, unit
    print("PASS: job units and the delivery worker read one authoritative configuration")


def test_absent_recipient_configuration_fails_closed_and_loudly() -> None:
    """No recipient must suppress *and* say so; silence is the original defect."""
    env = unit_environment(
        unit_text("log-workflow-b.service"),
        file_contents={
            # runtime.env present but with no alert recipient: the exact shape of
            # a half-configured host.
            str(RUNTIME_ENVIRONMENT_FILE): "POSTGRES_HOST=127.0.0.1\n",
            "/etc/log-platform/environment-identity.env": IDENTITY_ENV_TEXT,
        },
    )
    config = sb.load_alert_config(env)
    assert not config.recipients_configured

    decision = sb._decide_email(
        config=config,
        incident_created=True,
        previous_state=None,
        previous_material_signature=None,
        previous_email_enqueued_at=None,
        material_signature="sig",
        now=NOW,
    )
    assert not decision.enqueue
    assert decision.suppression_reason == sb.SUPPRESSED_RECIPIENTS_NOT_CONFIGURED
    # Classified as a configuration defect, not as ordinary throttling.
    assert sb.is_configuration_suppression(decision.suppression_reason)
    assert not sb.is_configuration_suppression(sb.SUPPRESSED_COOLDOWN)
    assert not sb.is_configuration_suppression(sb.SUPPRESSED_DUPLICATE_NOTIFICATION)
    print("PASS: a missing recipient fails closed and is classed a configuration defect")


def test_configuration_suppression_emits_an_operational_error() -> None:
    """An inert alert path must be loud at the moment it swallows an incident."""
    events: list[dict] = []

    def capture(event, **fields):
        events.append({"event": event, **fields})

    suppressed = sb.SuspectedBugReportResult(
        fingerprint="f", reported=True, incident_id="i",
        email_suppressed=True, suppression_reason=sb.SUPPRESSED_RECIPIENTS_NOT_CONFIGURED,
    )
    assert suppressed.delivery_not_configured
    assert suppressed.to_dict()["delivery_not_configured"] is True

    with Patch(oa, _stderr_event=capture):
        oa._warn_if_delivery_not_configured(
            suppressed, incident_code="JOB_TERMINAL_FAILURE", component="jobs.x"
        )
    assert len(events) == 1
    assert events[0]["event"] == "operational_alert_delivery_not_configured"
    assert events[0]["suppression_reason"] == sb.SUPPRESSED_RECIPIENTS_NOT_CONFIGURED
    assert "runtime.env" in events[0]["remediation"]

    # A cooldown is the mechanism working. Warning about it would train the
    # operator to ignore the message that matters.
    throttled = sb.SuspectedBugReportResult(
        fingerprint="f", reported=True,
        email_suppressed=True, suppression_reason=sb.SUPPRESSED_COOLDOWN,
    )
    assert not throttled.delivery_not_configured
    events.clear()
    with Patch(oa, _stderr_event=capture):
        oa._warn_if_delivery_not_configured(
            throttled, incident_code="JOB_TERMINAL_FAILURE", component="jobs.x"
        )
    assert events == []
    print("PASS: configuration suppression is loud; cooldown stays quiet")


def test_no_production_recipient_is_hard_coded() -> None:
    """Recipients come from configuration, never from the tree.

    Two exclusions, both deliberate and both narrow:

    * systemd template unit names (`log-job@dispatcher.service`) match an email
      regex and are not addresses;
    * RFC 2606/6761 reserved domains cannot be a real mailbox.

    The one real address in this surface is
    `AlertingReadiness.SMTP_FROM_DEFAULT`, which is a **sender** fallback
    mirroring `jobs.common.emailer`'s default, not a recipient. It is asserted
    explicitly below rather than allow-listed away, because "there is exactly one
    embedded address and it is a sender" is a stronger statement than "the scan
    found nothing".
    """
    address = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    unit_name = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]*\.(service|timer|socket)$")
    reserved = (".invalid", ".example", "example.com", "example.org", "example.net")

    roots = [
        REPO_ROOT / "ops" / "operational_alert.py",
        REPO_ROOT / "ops" / "suspected_bug_email_worker.py",
        REPO_ROOT / "ops" / "execution_watchdog.py",
        REPO_ROOT / "api" / "suspected_bug.py",
        REPO_ROOT / "ops" / "watchdog_expectations.json",
        REPO_ROOT / "jobs" / "reports" / "workflow_b" / "orchestrator.py",
        Path(__file__),
    ]
    roots.extend(sorted(SYSTEMD_DIR.rglob("*.service")))
    roots.extend(sorted(SYSTEMD_DIR.rglob("*.conf")))

    found: list[str] = []
    for path in roots:
        for match in address.findall(path.read_text(encoding="utf-8")):
            if unit_name.match(match) or any(match.endswith(item) for item in reserved):
                continue
            found.append(f"{path.name}:{match}")

    assert len(roots) >= 10, "static scan covered suspiciously few files"
    assert found == [
        f"operational_alert.py:{oa.AlertingReadiness.SMTP_FROM_DEFAULT}"
    ], f"unexpected embedded address(es): {found}"

    # Prove the one survivor is a sender and can never become a recipient.
    assert oa.AlertingReadiness.SMTP_FROM_DEFAULT not in sb.SuspectedBugAlertConfig().recipients
    assert sb.load_alert_config({}).recipients == ()
    default = oa.AlertingReadiness(sb.load_alert_config({}), env={})
    assert "recipients_not_configured" in default.problems
    assert default.effective_from == oa.AlertingReadiness.SMTP_FROM_DEFAULT
    # No recipient fallback exists: an unconfigured host sends to nobody rather
    # than borrowing the sender, a report recipient or a customer address.
    print(f"PASS: no recipient is embedded in {len(roots)} files; the one address is a sender")


def test_systemd_failure_fallback_is_preserved() -> None:
    """Defense in depth: the rich job incident does not replace OnFailure=."""
    text = unit_text("log-workflow-b.service")
    assert "OnFailure=log-platform-unit-failure@%n.service" in text
    # OnFailure= is a [Unit] directive; under [Service] systemd ignores it with
    # only a warning, which would leave the routing silently inert.
    dropin = (
        SYSTEMD_DIR / "log-workflow-b.service.d" / "95-onfailure.conf"
    ).read_text(encoding="utf-8")
    assert dropin.index("[Unit]") < dropin.index("OnFailure=")
    # The new drop-in must not have displaced it.
    names = sorted(p.name for p in (SYSTEMD_DIR / "log-workflow-b.service.d").glob("*.conf"))
    assert names == ["90-runtime-environment.conf", "95-onfailure.conf"]
    print("PASS: unit-level failure routing survives the environment retrofit")


def test_environment_file_parsing_matches_systemd_semantics() -> None:
    """The helper must not be more capable than systemd, or it proves nothing."""
    parsed = dict(parse_environment_file(
        "# comment\n"
        "; also a comment\n"
        "\n"
        "PLAIN=value\n"
        'QUOTED="with spaces"\n'
        "EMPTY=\n"
        "NOT_AN_ASSIGNMENT\n"
        "SUSPECTED_BUG_ALERT_TO=a@example.invalid,b@example.invalid\n"
    ))
    assert parsed["PLAIN"] == "value"
    assert parsed["QUOTED"] == "with spaces"
    assert parsed["EMPTY"] == ""
    assert "NOT_AN_ASSIGNMENT" not in parsed
    assert sb.parse_recipients(parsed["SUSPECTED_BUG_ALERT_TO"]) == (
        "a@example.invalid", "b@example.invalid",
    )

    # An empty EnvironmentFile= resets the list; a later file overrides an
    # earlier key; an unlisted file contributes nothing.
    text = (
        "[Service]\n"
        "EnvironmentFile=/ignored.env\n"
        "EnvironmentFile=\n"
        "EnvironmentFile=/first.env\n"
        "EnvironmentFile=/second.env\n"
        "Environment=INLINE_ONLY=inline\n"
    )
    env = unit_environment(
        text,
        file_contents={
            "/first.env": "SHARED=first\nONLY_FIRST=1\n",
            "/second.env": "SHARED=second\n",
            "/ignored.env": "SHOULD_NOT_APPEAR=1\n",
        },
    )
    assert env["SHARED"] == "second"
    assert env["ONLY_FIRST"] == "1"
    assert "SHOULD_NOT_APPEAR" not in env
    # An inline assignment with no file counterpart still applies.
    assert env["INLINE_ONLY"] == "inline"

    # An unreadable authoritative file must never be silently treated as empty:
    # that is exactly how the original defect stayed invisible.
    try:
        unit_environment("EnvironmentFile=/missing.env\n", file_contents={})
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a missing EnvironmentFile was silently ignored")
    print("PASS: environment-file merge reproduces systemd order and reset semantics")


def test_environment_file_overrides_inline_environment() -> None:
    """`EnvironmentFile=` beats `Environment=`, whatever order they appear in.

    From `man systemd.exec` (systemd 255 on this host):

        Settings from these files override settings made with Environment=.

    Confirmed empirically with a disposable transient user unit: with
    `Environment=X=inline` plus a file setting `X=from_file`, the process saw
    `from_file` in both declaration orders.

    The helper originally applied files first and inline second — i.e. plain
    textual order — which inverts this for the conflict case. That did not break
    P0-A, because `SUSPECTED_BUG_ALERT_TO` is never set inline, but the helper is
    part of the verification surface and must not encode behaviour systemd does
    not have. This test fails under the old ordering.
    """
    contents = {"/authoritative.env": "CONFLICT=from_file\n"}

    # File declared after the inline assignment.
    after = unit_environment(
        "[Service]\nEnvironment=CONFLICT=from_inline\n"
        "EnvironmentFile=/authoritative.env\n",
        file_contents=contents,
    )
    assert after["CONFLICT"] == "from_file", after

    # ...and before it. Textual order must not change the answer.
    before = unit_environment(
        "[Service]\nEnvironmentFile=/authoritative.env\n"
        "Environment=CONFLICT=from_inline\n",
        file_contents=contents,
    )
    assert before["CONFLICT"] == "from_file", before
    assert after == before

    # The rules that remain order-sensitive still are: among files, the later
    # file wins; among inline assignments, the later assignment wins.
    ordered = unit_environment(
        "[Service]\n"
        "Environment=INLINE_TWICE=first\n"
        "Environment=INLINE_TWICE=second\n"
        "EnvironmentFile=/a.env\n"
        "EnvironmentFile=/b.env\n",
        file_contents={"/a.env": "FILE_TWICE=a\n", "/b.env": "FILE_TWICE=b\n"},
    )
    assert ordered["INLINE_TWICE"] == "second"
    assert ordered["FILE_TWICE"] == "b"

    # The precedence rule must not swallow an inline variable the files do not
    # mention — that would be the opposite defect.
    mixed = unit_environment(
        "[Service]\n"
        "Environment=ONLY_INLINE=kept CONFLICT=from_inline\n"
        "EnvironmentFile=/authoritative.env\n",
        file_contents=contents,
    )
    assert mixed["ONLY_INLINE"] == "kept"
    assert mixed["CONFLICT"] == "from_file"

    # `base` models the inherited environment, which both layers override.
    inherited = unit_environment(
        "[Service]\nEnvironmentFile=/authoritative.env\n",
        file_contents=contents,
        base={"CONFLICT": "from_parent", "UNTOUCHED": "kept"},
    )
    assert inherited["CONFLICT"] == "from_file"
    assert inherited["UNTOUCHED"] == "kept"
    print("PASS: EnvironmentFile= overrides Environment= in either declaration order")


# ------------------------------------- P1-H: structured failure evidence


def test_unexpected_stage_exception_preserves_its_identity() -> None:
    """The failure class with no typed partial result keeps type and message."""

    def boom(client, run_id, params):
        raise ValueError("stage 2 blew up on a malformed workbook")

    result = job.WorkflowBBatchResult()
    conn = Conn(locked=True)
    client = Client()

    with Patch(
        job,
        _platform_pg_conn=lambda: conn,
        fetch_reports_batch=lambda *a, **k: Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=boom,
        process_stage3_batch=lambda *a, **k: Stage3BatchResult(),
    ):
        try:
            job.run(client, RUN_ID, {})
        except job.WorkflowBOrchestrationError as exc:
            result = exc.partial_result
            details = exc.operational_incident_details
            message = str(exc)
        else:  # pragma: no cover
            raise AssertionError("an unexpected stage exception must fail the run")

    assert result.outcome == job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR
    stage = result.failing_stage
    assert stage is not None and stage.stage_name == "workflow_b.stage2"
    assert stage.exception_type == "ValueError"
    assert "malformed workbook" in stage.exception_message
    assert stage.stack_trace and "ValueError" in stage.stack_trace

    # The exception text names the cause, so `error_signature()` can separate a
    # Stage 2 crash from a Stage 3 crash instead of collapsing them.
    assert "workflow_b.stage2" in message
    assert "ValueError" in message

    # Structured evidence reaches the incident layer.
    assert details["terminal_outcome"] == "FAILED_UNEXPECTED_STAGE_ERROR"
    assert details["failing_stage"] == "workflow_b.stage2"
    assert details["stage_exception_type"] == "ValueError"
    assert details["workflow_name"] == "workflow_b"

    # And the terminal payload is persisted on the failure path, which is the
    # run an operator actually has to reconstruct.
    finished = client.find("Workflow B orchestration finished")
    assert len(finished) == 1
    assert finished[0]["level"] == "ERROR"
    context = finished[0]["context"]
    assert context["outcome"] == "FAILED_UNEXPECTED_STAGE_ERROR"
    assert context["stage2"]["exception_type"] == "ValueError"
    print("PASS: an unexpected stage exception keeps its type, message and payload")


def test_unexpected_stage_origin_survives_serialization() -> None:
    """Bounded origin evidence must outlive the process, not just the frame.

    `stack_trace` is an in-memory attribute: it dies with the process, and the
    runner's own traceback showed only `_finish_or_raise` re-raising. So after
    journald rotates, nothing said *where* the exception came from. This asserts
    the durable projection — serialized frames plus exception chaining.
    """

    def boom(client, run_id, params):
        raise ValueError("stage 3 blew up parsing a cleaned artifact")

    client = Client()
    with Patch(
        job,
        _platform_pg_conn=lambda: Conn(locked=True),
        fetch_reports_batch=lambda *a, **k: Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=lambda *a, **k: Stage2BatchResult(),
        process_stage3_batch=boom,
    ):
        try:
            job.run(client, RUN_ID, {})
        except job.WorkflowBOrchestrationError as exc:
            error = exc
        else:  # pragma: no cover
            raise AssertionError("an unexpected stage exception must fail the run")

    stage = error.partial_result.failing_stage
    assert stage.stage_name == "workflow_b.stage3"

    # 1. Frames captured, innermost last, bounded.
    frames = stage.origin_frames
    assert frames, "no origin frames captured"
    assert len(frames) <= job.MAX_ORIGIN_FRAMES
    assert frames[-1].endswith(":boom"), frames
    for frame in frames:
        parts = frame.rsplit(":", 2)
        assert len(parts) == 3, frame
        assert parts[1].isdigit(), frame
        # Repository-relative or a bare basename — never an absolute path, which
        # would leak the deployment layout and release id into an email.
        assert not frame.startswith("/"), frame

    # 2. Durable in the terminal payload that reaches `public.logs`.
    finished = client.find("Workflow B orchestration finished")
    assert len(finished) == 1 and finished[0]["level"] == "ERROR"
    serialized = finished[0]["context"]["stage3"]
    assert serialized["origin_frames"] == list(frames)
    assert serialized["exception_type"] == "ValueError"
    assert "blew up parsing" in serialized["exception_message"]

    # 3. Durable in the incident details the alert is built from.
    details = error.operational_incident_details
    assert details["stage_origin_frames"] == list(frames)
    assert details["stage_origin"] == frames[-1]
    assert details["stage_exception_type"] == "ValueError"
    assert details["terminal_outcome"] == "FAILED_UNEXPECTED_STAGE_ERROR"
    assert details["failing_stage"] == "workflow_b.stage3"

    # 4. Exception chaining, so the runner's own `format_exc()` — already
    #    sanitized and length-bounded by `_Sanitizer.stack_trace` — carries the
    #    origin without a new field or a new transport.
    assert isinstance(error.__cause__, ValueError)
    # `raise ... from x` sets __suppress_context__ by design: the explicit cause
    # replaces the implicit "during handling of" chain, which is exactly the
    # rendering wanted here.
    assert error.__suppress_context__
    rendered = "".join(
        __import__("traceback").format_exception(type(error), error, error.__traceback__)
    )
    assert "direct cause" in rendered
    assert "stage 3 blew up parsing" in rendered
    assert "in boom" in rendered

    # 5. Nothing sensitive: frames are path:line:function only. No source text,
    #    no locals, no environment.
    assert "PASSWORD" not in str(frames).upper()
    assert "=" not in "".join(frames), "a frame looks like an assignment, not a location"

    # A typed partial failure has no captured origin and must keep the plain
    # raise: `from None` would set __suppress_context__ and hide Python's own
    # implicit chaining.
    plain = job.WorkflowBBatchResult()
    plain.stage2.typed_partial_failure = True
    plain.stage2.non_retryable_failure = True
    try:
        job._finish_or_raise(plain)
    except job.WorkflowBOrchestrationError as exc:
        assert exc.__cause__ is None
        assert not exc.__suppress_context__
    print("PASS: unexpected-stage origin is durable, bounded and chained")


def test_origin_evidence_is_excluded_from_incident_identity() -> None:
    """Volatile traceback detail must never split one recurring incident.

    A line number moves whenever the file above it changes. If that reached the
    fingerprint, every release would restart the cooldown for an unchanged,
    still-failing cause — the alert-storm shape this machinery exists to avoid.
    """
    def build(line_marker: str, run_id: str, failing_stage: str = "workflow_b.stage2"):
        result = job.WorkflowBBatchResult()
        result.stage2.started = True
        result.stage2.completed = True
        result.stage2.unexpected_failure = True
        result.stage2.non_retryable_failure = True
        result.stage2.error_category = "unexpected_stage_error"
        result.stage2.exception_type = "ValueError"
        result.stage2.exception_message = "malformed workbook"
        result.stage2.origin_frames = (f"jobs/reports/stage2/job_stage2.py:{line_marker}:clean",)
        result.outcome = job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR
        error = job.WorkflowBOrchestrationError(result)
        return sb.SuspectedBugEvent(
            incident_code=oa.INCIDENT_JOB_TERMINAL_FAILURE,
            title="Job terminated with an unhandled failure",
            summary="s",
            occurred_at=NOW,
            environment="production",
            component="jobs.reports.workflow_b.orchestrator",
            exception_type="WorkflowBOrchestrationError",
            stack_trace=f"Traceback ...\n  File job_stage2.py, line {line_marker}\n",
            details={"job_evidence": error.operational_incident_details},
            fingerprint_fields={
                "exception_type": "WorkflowBOrchestrationError",
                "error_signature": oa.error_signature(str(error)),
                "terminal_outcome": "FAILED_UNEXPECTED_STAGE_ERROR",
                "failing_stage": failing_stage,
            },
            run_id=run_id,
        )

    first = build("412", "11111111-1111-1111-1111-111111111111")
    second = build("987", "22222222-2222-2222-2222-222222222222")

    assert first.details != second.details, "the fixture must actually differ"
    assert first.stack_trace != second.stack_trace
    assert first.fingerprint() == second.fingerprint(), (
        "origin/traceback detail leaked into incident identity"
    )
    # And the identity dict itself must not mention any of it.
    identity = first.fingerprint_identity()
    flattened = repr(identity)
    for leaked in ("stage_origin", "origin_frames", "412", "Traceback"):
        assert leaked not in flattened, f"{leaked} participates in identity"

    # The distinctions that *should* still split incidents remain intact.
    other_stage = build(
        "412", "11111111-1111-1111-1111-111111111111", failing_stage="workflow_b.stage3"
    )
    assert other_stage.fingerprint() != first.fingerprint()
    print("PASS: origin evidence is durable but outside dedup identity")


def test_committed_stage3_then_postprocessor_failure_says_data_changed() -> None:
    """The one fact that changes the operator's first move."""
    plan = job.WorkflowBPostprocessorPlan(
        postprocessor_name="unsupported_configured_postprocessor",
        raw_file_id=RAW_ID,
        client_code="BRAVO00016",
        report_type="report_207",
        source_cleaned_artifact_id=None,
        selector="invalid",
        selector_origin="unresolved",
        error_category="missing_report_policy",
    )
    result = job.WorkflowBBatchResult()
    result.stage3.result = stage3_loaded()
    result.postprocessors.append(
        job.WorkflowBPostprocessorResult(
            plan.postprocessor_name, plan.raw_file_id, plan.client_code, plan.report_type,
            job.WorkflowBPostprocessorOutcome.BLOCKED_UNSUPPORTED_CONFIGURATION,
            operator_action_required=True, error_category=plan.error_category,
        )
    )
    try:
        job._finish_or_raise(result)
    except job.WorkflowBOrchestrationError as exc:
        details = exc.operational_incident_details
    else:  # pragma: no cover
        raise AssertionError("a blocked postprocessor must fail the run")

    assert details["terminal_outcome"] == "FAILED_NON_RETRYABLE"
    assert details["production_loads_succeeded"] == 1
    assert details["durable_writes_committed"] is True
    assert details["failing_postprocessor"] == "unsupported_configured_postprocessor"
    assert details["postprocessor_error_category"] == "missing_report_policy"
    # Genuinely known identity, carried through — not inferred from params.
    assert details["client_code"] == "BRAVO00016"
    assert details["report_type"] == "report_207"
    assert details["raw_file_id"] == RAW_ID
    print("PASS: a post-commit failure reports that customer data already changed")


def test_incident_details_never_fabricate_unknown_context() -> None:
    """Absent is absent. A wrong client code is worse than a missing one."""
    result = job.WorkflowBBatchResult()
    result.stage1.started = True
    result.stage1.completed = True
    result.stage1.unexpected_failure = True
    result.stage1.error_category = "unexpected_stage_error"
    details = result.incident_details()
    for absent in ("client_code", "report_type", "raw_file_id", "failing_postprocessor"):
        assert absent not in details, absent
    assert details["durable_writes_committed"] is False
    assert details["production_loads_succeeded"] == 0
    print("PASS: unknown context is omitted rather than invented")


def test_runner_boundary_lifts_carried_evidence_into_the_incident() -> None:
    """`ops/runner.py` sees empty params; the exception supplies the rest."""
    captured: dict = {}

    def fake_report(**kwargs):
        captured.update(kwargs)
        return sb.SuspectedBugReportResult(fingerprint="f", reported=True)

    error = job.WorkflowBOrchestrationError(
        _failed_result_with_postprocessor()
    )
    with Patch(oa, report_operational_failure=fake_report):
        oa.report_job_terminal_failure(
            job_module="jobs.reports.workflow_b.orchestrator",
            exc=error,
            run_id=RUN_ID,
            params={},
        )

    assert captured["incident_code"] == oa.INCIDENT_JOB_TERMINAL_FAILURE
    assert captured["workflow_name"] == "workflow_b"
    assert captured["run_id"] == RUN_ID
    assert captured["client_code"] == "BRAVO00016"
    assert "FAILED_NON_RETRYABLE" in captured["summary"]
    # Stage identity participates in the fingerprint, so a Stage 2 crash and a
    # postprocessor block do not collapse onto one incident.
    assert captured["extra_identity"]["terminal_outcome"] == "FAILED_NON_RETRYABLE"
    assert captured["details"]["job_evidence"]["durable_writes_committed"] is True

    # An exception that carries nothing must degrade cleanly, not raise.
    captured.clear()
    with Patch(oa, report_operational_failure=fake_report):
        oa.report_job_terminal_failure(
            job_module="jobs.api.telematics.dispatcher",
            exc=RuntimeError("plain"),
            run_id=RUN_ID,
            params={"client_code": "ALPHA00001"},
        )
    assert captured["workflow_name"] == "workflow_a"
    assert captured["client_code"] == "ALPHA00001"
    assert captured["extra_identity"] is None
    assert "job_evidence" not in captured["details"]

    # A malformed attribute must never disturb the failure being reported.
    class Hostile(RuntimeError):
        @property
        def operational_incident_details(self):
            raise ZeroDivisionError("hostile")

    assert oa._incident_details_from_exception(Hostile("x")) == {}
    bad = RuntimeError("x")
    bad.operational_incident_details = ["not", "a", "mapping"]
    assert oa._incident_details_from_exception(bad) == {}
    print("PASS: carried evidence enriches the incident and degrades safely")


def _failed_result_with_postprocessor() -> job.WorkflowBBatchResult:
    result = job.WorkflowBBatchResult()
    result.stage3.result = stage3_loaded()
    result.postprocessors.append(
        job.WorkflowBPostprocessorResult(
            "report_207_speeding_migration", RAW_ID, "BRAVO00016", "report_207",
            job.WorkflowBPostprocessorOutcome.BLOCKED_UNSUPPORTED_CONFIGURATION,
            operator_action_required=True, error_category="missing_report_policy",
        )
    )
    result.outcome = job.WorkflowBOutcome.FAILED_NON_RETRYABLE
    return result


def test_successful_outcomes_are_unchanged() -> None:
    """Hardening must not move a single success boundary."""
    cases = {
        job.WorkflowBOutcome.SUCCEEDED_NO_WORK: (
            Stage1BatchResult(mailbox_check_completed=True),
            Stage2BatchResult(),
            Stage3BatchResult(),
        ),
        job.WorkflowBOutcome.SUCCEEDED: (
            Stage1BatchResult(mailbox_check_completed=True, raw_files_created=1),
            Stage2BatchResult(),
            Stage3BatchResult(),
        ),
    }
    for expected, (s1, s2, s3) in cases.items():
        client = Client()
        with Patch(
            job,
            _platform_pg_conn=lambda: Conn(locked=True),
            fetch_reports_batch=lambda *a, **k: s1,
            process_stage2_batch=lambda *a, **k: s2,
            process_stage3_batch=lambda *a, **k: s3,
            _discover_postprocessor_plans=lambda *a, **k: ([], 0),
        ):
            result = job.run(client, RUN_ID, {})
        assert result.outcome == expected, (expected, result.outcome)
        finished = client.find("Workflow B orchestration finished")
        assert len(finished) == 1 and finished[0]["level"] == "INFO"

    # Review items remain a success, not a failure.
    review = Stage3BatchResult()
    review.items.append(_review_item())
    client = Client()
    with Patch(
        job,
        _platform_pg_conn=lambda: Conn(locked=True),
        fetch_reports_batch=lambda *a, **k: Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=lambda *a, **k: Stage2BatchResult(),
        process_stage3_batch=lambda *a, **k: review,
        _discover_postprocessor_plans=lambda *a, **k: ([], 0),
    ):
        result = job.run(client, RUN_ID, {})
    assert result.outcome == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS

    # P0-D landed: a *scheduled* lock loss now raises so the run is recorded
    # FAILED. A manual_diagnostic loss keeps the original silent-skip semantics,
    # which is the half this B1 slice asserted and must stay unchanged.
    with Patch(job, _platform_pg_conn=lambda: Conn(locked=False)):
        skipped = job.run(Client(), RUN_ID, {"mode": "manual_diagnostic"})
    assert skipped.outcome == job.WorkflowBOutcome.SKIPPED_LOCKED
    print("PASS: success, review and lock-skip semantics are unchanged")


def _review_item():
    """A review item whose unresolved condition is *replayable*.

    This assertion depends on one property: a stubbed incident store that writes
    nothing must lose no evidence, so the cycle can still be a success with
    review items. After review disproved the Stage 2 position argument — a
    targeted reprocess re-stamps `stage2_updated_at` and moves a swept row
    behind the sweep backstop — Stage 2 no longer has that property, and a Stage
    2 review item left with no durable incident is now the one signalling
    condition that fails a cycle closed.

    Stage 3 does still have it: autonomous discovery runs with no LIMIT, so every
    row left in an eligible durable status is re-offered on every later cycle.
    """
    from jobs.reports.stage3.batch_contract import Stage3ItemResult, Stage3Outcome

    return Stage3ItemResult(
        raw_file_id=RAW_ID,
        client_code="BRAVO00016",
        report_type="report_207",
        outcome=Stage3Outcome.BLOCKED_OPERATOR_ACTION,
        persisted_status=None,
        error_category="missing_report_policy",
        operator_action_required=True,
    )


def test_logging_failure_cannot_replace_the_original_exception() -> None:
    """The new terminal-summary write must never become the reported failure.

    Scoped to the summary write this slice added. The orchestrator's other
    `client.log` calls were already unguarded before this change and are out of
    scope here — widening the assertion would claim a fix that was not made.
    """

    class SummaryFailsClient(Client):
        def log(self, level, kind, source, message, **kwargs):
            if message == "Workflow B orchestration finished":
                raise RuntimeError("logging backend down")
            super().log(level, kind, source, message, **kwargs)

    with Patch(
        job,
        _platform_pg_conn=lambda: Conn(locked=True),
        fetch_reports_batch=lambda *a, **k: (_ for _ in ()).throw(ValueError("stage 1 died")),
    ):
        try:
            job.run(SummaryFailsClient(), RUN_ID, {})
        except job.WorkflowBOrchestrationError as exc:
            assert exc.partial_result.outcome == job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR
            assert exc.partial_result.failing_stage.exception_type == "ValueError"
        except RuntimeError as exc:  # pragma: no cover
            raise AssertionError(f"logging replaced the original failure: {exc}")

    # Same guard on the success path: a failed summary write must not convert a
    # successful cycle into a failed one.
    with Patch(
        job,
        _platform_pg_conn=lambda: Conn(locked=True),
        fetch_reports_batch=lambda *a, **k: Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=lambda *a, **k: Stage2BatchResult(),
        process_stage3_batch=lambda *a, **k: Stage3BatchResult(),
        _discover_postprocessor_plans=lambda *a, **k: ([], 0),
    ):
        ok = job.run(SummaryFailsClient(), RUN_ID, {})
    assert ok.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK
    print("PASS: a failed terminal-summary write changes no outcome in either direction")


def test_origin_capture_failure_cannot_replace_the_original_failure() -> None:
    """Describing a failure must never become a different failure."""

    def boom(client, run_id, params):
        raise ValueError("the real cause")

    # Frame extraction blows up. The run must still fail as
    # FAILED_UNEXPECTED_STAGE_ERROR with type and message intact, just without
    # frames.
    def exploding_frames(exc, **kwargs):
        raise RuntimeError("traceback machinery broken")

    with Patch(
        job,
        _platform_pg_conn=lambda: Conn(locked=True),
        fetch_reports_batch=boom,
        origin_frames=exploding_frames,
    ):
        try:
            job.run(Client(), RUN_ID, {})
        except job.WorkflowBOrchestrationError as exc:
            stage = exc.partial_result.failing_stage
            assert exc.partial_result.outcome == job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR
            assert stage.exception_type == "ValueError"
            assert stage.exception_message == "the real cause"
            assert stage.origin_frames == ()
        except RuntimeError as exc:  # pragma: no cover
            raise AssertionError(f"evidence capture replaced the failure: {exc}")

    # `incident_details()` blowing up must not either.
    class HostileResult(job.WorkflowBBatchResult):
        def incident_details(self):
            raise ZeroDivisionError("details exploded")

    hostile = HostileResult()
    hostile.outcome = job.WorkflowBOutcome.FAILED_NON_RETRYABLE
    error = job.WorkflowBOrchestrationError(hostile)
    assert error.operational_incident_details == {}
    assert "Workflow B orchestration failed" in str(error)

    # And the shared reporting boundary still degrades cleanly on that shape.
    assert oa._incident_details_from_exception(error) == {}
    print("PASS: a failure in evidence capture never replaces the reported failure")


# ------------------------------------------------ P0-F: delivery health


def test_dead_letter_batch_exits_non_zero_and_retry_does_not() -> None:
    """The worker cannot email about itself, so its exit code is the signal."""
    calls: list[dict] = []

    def fake_run_once(**kwargs):
        return calls.pop(0)

    with Patch(worker, run_once=fake_run_once, load_alert_config=lambda: sb.SuspectedBugAlertConfig()):
        argv = sys.argv
        try:
            sys.argv = ["worker", "--once"]

            calls.append({"claimed": 2, sb.OUTBOX_SENT: 2, sb.OUTBOX_RETRY: 0,
                          sb.OUTBOX_DEAD_LETTER: 0})
            assert worker.main() == 0, "a healthy batch must not fail the unit"

            calls.append({"claimed": 0, sb.OUTBOX_SENT: 0, sb.OUTBOX_RETRY: 0,
                          sb.OUTBOX_DEAD_LETTER: 0})
            assert worker.main() == 0, "an empty batch must not fail the unit"

            # A transient SMTP outage is the retry mechanism working. Failing
            # the unit here would page on every brief hiccup.
            calls.append({"claimed": 1, sb.OUTBOX_SENT: 0, sb.OUTBOX_RETRY: 1,
                          sb.OUTBOX_DEAD_LETTER: 0})
            assert worker.main() == 0, "a transient retry must not fail the unit"

            # Terminal: this alert will never be delivered.
            calls.append({"claimed": 1, sb.OUTBOX_SENT: 0, sb.OUTBOX_RETRY: 0,
                          sb.OUTBOX_DEAD_LETTER: 1})
            assert worker.main() == worker.EXIT_DEAD_LETTER
            assert worker.EXIT_DEAD_LETTER != 0
        finally:
            sys.argv = argv
    print("PASS: dead-letter fails the unit; healthy and retrying batches do not")


def test_worker_never_reports_itself_through_the_alert_path() -> None:
    """Anti-recursion, asserted rather than assumed."""
    source = (REPO_ROOT / "ops" / "suspected_bug_email_worker.py").read_text(encoding="utf-8")
    assert "operational_alert" not in source, "the worker must not import the alert reporter"
    assert "report_suspected_bug" not in source.replace("suspected_bug_email_outbox", "")
    assert oa.is_self_alerting("ops.suspected_bug_email_worker")

    # Its unit must carry no *active* OnFailure=, or the failed-unit signal
    # would loop back into the machinery that is broken. The unit documents this
    # in a comment, so the assertion has to read directives, not substrings.
    unit = (SYSTEMD_DIR / "suspected-bug-email-worker.service").read_text(encoding="utf-8")
    directives = [
        line.strip() for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(line.startswith("OnFailure=") for line in directives), directives
    assert any("OnFailure" in line for line in unit.splitlines() if line.strip().startswith("#")), (
        "the anti-recursion decision must stay documented in the unit"
    )
    # The comparison that proves the check is meaningful: the units that *should*
    # route failure do carry the active directive.
    watchdog = (SYSTEMD_DIR / "execution-watchdog.service").read_text(encoding="utf-8")
    assert any(
        line.strip().startswith("OnFailure=") for line in watchdog.splitlines()
    ), "the watchdog must still route its own unit failure"
    print("PASS: the worker signals failure without using the path that failed")


# --------------------------------------------------------- persistence


def test_persistence(dsn: str) -> None:
    """Rich enqueue, heartbeat liveness and dead-letter observability, for real."""
    import psycopg
    from psycopg.rows import dict_row

    from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit

    # This suite drops and recreates tables. The shared guard decides what
    # "local" means, and it decides before a connection is opened.
    require_loopback_dsn_or_exit(dsn, label="ALERT_HARDENING_TEST_DSN")
    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        _bootstrap(conn)
        _assert_rich_job_incident_is_enqueued(conn)
        _assert_origin_evidence_is_durable(conn)
        _assert_worker_records_liveness(conn)
        _assert_dead_letter_is_observable(conn, dsn)
    finally:
        conn.close()


def _bootstrap(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
        cur.execute("CREATE SCHEMA ops_control")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cur.execute("DROP TABLE IF EXISTS suspected_bug_email_outbox CASCADE")
        cur.execute("DROP TABLE IF EXISTS suspected_bug_occurrences CASCADE")
        cur.execute("DROP TABLE IF EXISTS suspected_bug_incidents CASCADE")
        cur.execute("DROP TABLE IF EXISTS logs CASCADE")
        cur.execute("DROP TABLE IF EXISTS runs CASCADE")
        cur.execute(
            """
            CREATE TABLE runs (
              run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
              status TEXT NOT NULL, trigger TEXT NOT NULL, source TEXT NOT NULL, actor TEXT,
              params JSONB NOT NULL DEFAULT '{}'::jsonb
            );
            CREATE TABLE logs (
              id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, level TEXT NOT NULL,
              type TEXT NOT NULL, source TEXT NOT NULL,
              run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
              message TEXT NOT NULL, context JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT
            );
            """
        )
        cur.execute(
            (REPO_ROOT / "db/migrations/052_suspected_bug_incidents_and_email_outbox.sql")
            .read_text(encoding="utf-8")
        )
        cur.execute(
            (REPO_ROOT / "db/migrations/059_operational_watchdog_state.sql")
            .read_text(encoding="utf-8")
        )
    conn.commit()


def _assert_rich_job_incident_is_enqueued(conn) -> None:
    config = sb.SuspectedBugAlertConfig(
        recipients=("ops@example.invalid",),
        environment="production",
        cooldown=timedelta(hours=2),
    )
    error = job.WorkflowBOrchestrationError(_failed_result_with_postprocessor())
    result = oa.report_job_terminal_failure(
        job_module="jobs.reports.workflow_b.orchestrator",
        exc=error,
        run_id=None,
        params={},
        stack_trace="Traceback (most recent call last):\n  ...\n",
        conn=conn,
        config=config,
        now=NOW,
    )
    assert result.reported, result.error
    assert result.email_enqueued, result.suppression_reason
    assert not result.delivery_not_configured

    with conn.cursor() as cur:
        cur.execute(
            "SELECT email_decision, email_decision_reason, payload "
            "FROM suspected_bug_occurrences WHERE occurrence_id = %s::uuid",
            (result.occurrence_id,),
        )
        occurrence = cur.fetchone()
        cur.execute(
            "SELECT subject, body_text, recipients FROM suspected_bug_email_outbox "
            "WHERE outbox_id = %s::uuid",
            (result.outbox_id,),
        )
        outbox = cur.fetchone()
    conn.rollback()

    assert occurrence["email_decision"] == "enqueued"
    assert occurrence["email_decision_reason"] == sb.REASON_NEW

    payload = occurrence["payload"]
    assert payload["workflow_name"] == "workflow_b"
    assert payload["stage_name"] is None or "workflow_b" in str(payload["stage_name"])
    assert payload["client_code"] == "BRAVO00016"
    assert payload["exception_type"] == "WorkflowBOrchestrationError"

    body = outbox["body_text"]
    # The delivered alert must carry what the SYSTEMD_UNIT_FAILURE alert could
    # not: which workflow, which client, and that data already changed.
    assert "workflow_b" in body
    assert "BRAVO00016" in body
    assert "FAILED_NON_RETRYABLE" in body
    assert "durable_writes_committed" in body or "True" in body
    # And never a secret.
    assert "PASSWORD" not in body.upper()
    print("PASS: a Workflow B terminal failure enqueues a rich, actionable alert")


def _assert_origin_evidence_is_durable(conn) -> None:
    """Origin frames must survive the whole path into stored JSONB, twice over.

    A real raised-and-chained exception, not a hand-built fixture: the point is
    that the evidence survives `_invoke_stage` -> `_finish_or_raise` ->
    `report_job_terminal_failure` -> sanitizer -> JSONB, which a fixture cannot
    demonstrate.
    """
    import traceback as tb

    def boom(client, run_id, params):
        raise ValueError("cleaned artifact had an unparseable header row")

    with Patch(
        job,
        _platform_pg_conn=lambda: Conn(locked=True),
        fetch_reports_batch=lambda *a, **k: Stage1BatchResult(mailbox_check_completed=True),
        process_stage2_batch=boom,
        process_stage3_batch=lambda *a, **k: Stage3BatchResult(),
    ):
        try:
            job.run(Client(), RUN_ID, {})
        except job.WorkflowBOrchestrationError as exc:
            error = exc
        else:  # pragma: no cover
            raise AssertionError("expected an unexpected-stage failure")

    frames = error.partial_result.failing_stage.origin_frames
    assert frames

    result = oa.report_job_terminal_failure(
        job_module="jobs.reports.workflow_b.orchestrator",
        exc=error,
        run_id=None,
        params={},
        # Exactly what ops/runner.py passes at its own boundary. Because
        # `_finish_or_raise` chained the origin, this now contains the ValueError
        # frames too, where before it showed only the re-raise.
        stack_trace="".join(tb.format_exception(type(error), error, error.__traceback__)),
        conn=conn,
        config=sb.SuspectedBugAlertConfig(
            recipients=("ops@example.invalid",), environment="production"
        ),
        now=datetime.now(UTC),
    )
    assert result.reported, result.error

    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM suspected_bug_occurrences WHERE occurrence_id = %s::uuid",
            (result.occurrence_id,),
        )
        payload = cur.fetchone()["payload"]
        cur.execute(
            "SELECT body_text FROM suspected_bug_email_outbox WHERE incident_id = %s::uuid "
            "ORDER BY created_at DESC LIMIT 1",
            (result.incident_id,),
        )
        row = cur.fetchone()
    conn.rollback()

    # 1. Structured frames survived into stored JSONB.
    evidence = payload["details"]["job_evidence"]
    assert evidence["stage_origin"] == frames[-1], evidence
    assert evidence["stage_exception_type"] == "ValueError"
    assert evidence["failing_stage"] == "workflow_b.stage2"

    # 2. The chained traceback survived into the sanitized stored stack_trace.
    stored_trace = payload["stack_trace"]
    assert stored_trace, "no stack trace persisted"
    assert "unparseable header row" in stored_trace
    assert "in boom" in stored_trace

    # 3. Bounded by the existing sanitizer, not by hope.
    assert len(stored_trace) <= sb.MAX_STACK_TRACE_CHARS
    assert len(stored_trace.splitlines()) <= sb.MAX_STACK_TRACE_LINES

    # 4. Present in what the operator actually receives.
    if row is not None:
        assert "workflow_b.stage2" in row["body_text"]

    # 5. No secret material rode along.
    for banned in ("POSTGRES_PASSWORD", "IMAP_PASSWORD", "SMTP_PASSWORD", "API_WRITE_TOKEN"):
        assert banned not in stored_trace, banned
        assert banned not in str(evidence), banned
    print("PASS: origin frames and the chained traceback survive into stored evidence")


def _assert_worker_records_liveness(conn) -> None:
    counts = worker.run_once(
        config=sb.SuspectedBugAlertConfig(recipients=("ops@example.invalid",)),
        sender=lambda **kwargs: types.SimpleNamespace(message_id="m"),
        conn=conn,
    )
    assert counts["heartbeat_recorded"] is True

    with conn.cursor() as cur:
        cur.execute(
            "SELECT component, last_beat_at, beat_count FROM ops_control.scheduler_heartbeat "
            "WHERE component = %s",
            (worker.HEARTBEAT_COMPONENT,),
        )
        row = cur.fetchone()
    conn.rollback()
    assert row is not None, "the worker recorded no liveness"
    assert row["beat_count"] >= 1

    # An idle batch must still beat, or "nothing to send" would look like death.
    before = row["beat_count"]
    worker.run_once(
        config=sb.SuspectedBugAlertConfig(recipients=("ops@example.invalid",)),
        sender=lambda **kwargs: types.SimpleNamespace(message_id="m"),
        conn=conn,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT beat_count FROM ops_control.scheduler_heartbeat WHERE component = %s",
            (worker.HEARTBEAT_COMPONENT,),
        )
        after = cur.fetchone()["beat_count"]
    conn.rollback()
    assert after == before + 1
    print("PASS: the worker stamps liveness on every batch, including empty ones")


def _assert_dead_letter_is_observable(conn, dsn: str) -> None:
    import ops.execution_watchdog as wd

    config = sb.SuspectedBugAlertConfig(
        recipients=("ops@example.invalid",),
        max_attempts=1,
        environment="production",
    )
    # Real wall clock, not the fixture instant: `available_at` is set from `now`
    # and `claim_batch` only claims rows that are already due, so a future
    # fixture timestamp would make the row unclaimable and the assertion below
    # would pass for the wrong reason.
    real_now = datetime.now(UTC)
    reported = oa.report_operational_failure(
        incident_code="ALERT_DELIVERY_PROBE",
        title="undeliverable",
        summary="this alert will never arrive",
        component="jobs.reports.workflow_b.orchestrator",
        conn=conn,
        config=config,
        now=real_now,
    )
    assert reported.email_enqueued, reported.suppression_reason

    def refusing(**kwargs):
        raise smtplib.SMTPRecipientsRefused({"ops@example.invalid": (550, b"nope")})

    counts = worker.run_once(config=config, sender=refusing, conn=conn)
    assert counts[sb.OUTBOX_DEAD_LETTER] == 1, counts

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempts, last_error FROM suspected_bug_email_outbox "
            "WHERE outbox_id = %s::uuid",
            (reported.outbox_id,),
        )
        row = cur.fetchone()
    conn.rollback()
    assert row["status"] == sb.OUTBOX_DEAD_LETTER

    # The durable half: the watchdog reports it as a non-OK subject that keeps
    # asserting itself until an operator clears the row.
    expectation = wd.AlertDeliveryExpectation()
    state = wd.load_alert_delivery_state(
        conn, expectation=expectation, now_utc=NOW + timedelta(hours=7)
    )
    assert int(state["dead_letter_count"]) == 1
    observation = wd.evaluate_alert_delivery_subject(
        expectation=expectation, state=state, now_utc=NOW + timedelta(hours=7)
    )
    assert observation.verdict == wd.VERDICT_STALE
    assert observation.alerting
    assert observation.incident_code == wd.INCIDENT_ALERT_DELIVERY_FAILED

    # Repeated scans must re-observe, never re-storm: one durable subject.
    again = wd.evaluate_alert_delivery_subject(
        expectation=expectation, state=state, now_utc=NOW + timedelta(hours=8)
    )
    assert again.subject_key == observation.subject_key

    # And once the operator clears it, the subject returns to healthy so the
    # incident can be resolved.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM suspected_bug_email_outbox WHERE status = 'dead_letter'")
    conn.commit()
    cleared = wd.evaluate_alert_delivery_subject(
        expectation=expectation,
        state=wd.load_alert_delivery_state(
            conn, expectation=expectation, now_utc=NOW + timedelta(hours=9)
        ),
        now_utc=NOW + timedelta(hours=9),
    )
    assert cleared.verdict == wd.VERDICT_OK
    print("PASS: a permanently refused alert becomes a durable, clearable observation")


def main() -> None:
    test_workflow_b_unit_supplies_the_authoritative_alert_configuration()
    test_every_incident_raising_job_unit_sources_the_same_configuration()
    test_absent_recipient_configuration_fails_closed_and_loudly()
    test_configuration_suppression_emits_an_operational_error()
    test_no_production_recipient_is_hard_coded()
    test_systemd_failure_fallback_is_preserved()
    test_environment_file_parsing_matches_systemd_semantics()
    test_environment_file_overrides_inline_environment()
    test_unexpected_stage_exception_preserves_its_identity()
    test_unexpected_stage_origin_survives_serialization()
    test_origin_evidence_is_excluded_from_incident_identity()
    test_origin_capture_failure_cannot_replace_the_original_failure()
    test_committed_stage3_then_postprocessor_failure_says_data_changed()
    test_incident_details_never_fabricate_unknown_context()
    test_runner_boundary_lifts_carried_evidence_into_the_incident()
    test_successful_outcomes_are_unchanged()
    test_logging_failure_cannot_replace_the_original_exception()
    test_dead_letter_batch_exits_non_zero_and_retry_does_not()
    test_worker_never_reports_itself_through_the_alert_path()

    dsn = os.getenv("ALERT_HARDENING_TEST_DSN")
    if dsn:
        test_persistence(dsn)
    else:
        print("SKIP: ALERT_HARDENING_TEST_DSN unset - persistence tests not run")
    print("OK - Workflow B alert hardening (P0-A, P0-F, P1-H) verified")


if __name__ == "__main__":
    main()
