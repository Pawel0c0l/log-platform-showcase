#!/usr/bin/env python3
"""Eco Driving e-mail — an AMBIGUOUS SMTP result is never retried automatically.

Run:
    python3 ops/tests_manual/test_eco_email_ambiguous_smtp_safety.py

    # and, for the durable half, against a DISPOSABLE local instance:
    ECO_EMAIL_AMBIGUITY_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5439/disposable \\
      python3 ops/tests_manual/test_eco_email_ambiguous_smtp_safety.py

THE DEFECT THIS SUITE EXISTS FOR

All four Eco mailers treated every exception raised while sending as `failed`,
and `failed` is retryable. So this was reachable on the ordinary 06:00/20:00
cadence with no human involved:

    reserve -> example.invalid ACCEPTS the message -> the connection dies before the
    final response is read -> Python raises -> the row is marked `failed` ->
    the next normal run reserves again -> the SAME e-mail is sent again.

SMTP carries no idempotency key, so a second submission IS a second message.
The policy is `SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY = NO`, and this suite is
the evidence that the code now implements it.

WHAT IS PROVED, AND WHERE

TIER A (no infrastructure) — the classification rule itself, and the fact that
all four mailers share one implementation of it rather than four lookalikes.

TIER B (a DISPOSABLE PostgreSQL) — the durable behaviour, against the REAL
`eco_*_email_send_log` tables built by the REAL migrations 027-046, including
migration 044's partial unique indexes. Skipped LOUDLY without a DSN: a skipped
half is reported as skipped, never as a pass.

DESTRUCTIVE (Tier B only). It creates and drops schema objects in the database
the DSN names, and refuses any DSN that is not loopback.

NOT DONE ANYWHERE IN THIS FILE: a real SMTP connection, a real e-mail, a live
provider call, or any mutation of a production database. The transports are
scripted fakes.
"""

from __future__ import annotations

import os
import smtplib
import socket
import ssl
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.common import eco_email_reconciliation as rec  # noqa: E402
from jobs.common import eco_smtp_submission as sub  # noqa: E402

PASSED: list[str] = []
SKIPPED: list[str] = []

ENV_DSN = "ECO_EMAIL_AMBIGUITY_TEST_DSN"

#: The four Eco mailers, their send log and the column each is keyed by.
MAILERS = (
    ("ALPHA weekly", "jobs/ecodriving/job_eco_driving_weekly_email_notifications.py",
     "eco_driving_weekly_email_send_log", "assigned_id"),
    ("ALPHA monthly", "jobs/ecodriving/job_eco_driving_monthly_email_notifications.py",
     "eco_driving_monthly_email_send_log", "assigned_id"),
    ("BRAVO weekly",
     "jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py",
     "eco_person_weekly_email_send_log", "person_name_group_key"),
    ("BRAVO monthly",
     "jobs/ecodriving_person/job_eco_driving_person_monthly_email_notifications.py",
     "eco_person_monthly_email_send_log", "person_name_group_key"),
)

#: The migrations that build the real Eco client-business schema, in order.
ECO_MIGRATIONS = ("009", "011", "020", "027", "028", "029", "030", "031", "032",
                  "033", "034", "035", "036", "037", "039", "040", "041", "043",
                  "044", "046")

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
PERIOD_START = date(2026, 5, 1)
PERIOD_END = date(2026, 5, 18)
MONTH_START = date(2026, 4, 1)
MONTH_END = date(2026, 5, 1)


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ==============================================================================
# TIER A — the classification rule
# ==============================================================================


def test_only_protocol_evidence_makes_a_failure_retryable() -> None:
    """A failure is retryable only where the protocol PROVED non-acceptance.

    The two columns below are the whole safety argument. Everything on the left
    is a definitive negative reply or a phase that runs before any message
    content exists; everything on the right is a case where the remote server
    may have taken the message and this host simply never heard.
    """
    definite = {
        "the greeting failed": smtplib.SMTPHeloError(500, b"bad helo"),
        "SMTPUTF8 is unsupported and was needed":
            smtplib.SMTPNotSupportedError("SMTPUTF8 not supported"),
        "MAIL FROM was rejected":
            smtplib.SMTPSenderRefused(550, b"sender rejected", "from@example.invalid"),
        "every recipient was rejected":
            smtplib.SMTPRecipientsRefused({"to@example.invalid": (550, b"no such user")}),
        "DATA itself was refused": smtplib.SMTPDataError(554, b"transaction failed"),
        "the terminating dot got a negative reply":
            smtplib.SMTPDataError(452, b"insufficient storage"),
    }
    for name, error in definite.items():
        check("a proved rejection stays retryable",
              sub.classify_transmit_exception(error) == sub.DEFINITE_NOT_SUBMITTED,
              f"{name} -> {sub.classify_transmit_exception(error)}")

    ambiguous = {
        "the connection dropped mid-DATA":
            smtplib.SMTPServerDisconnected("connection closed"),
        "the final response never arrived": socket.timeout("timed out"),
        "the socket died": ConnectionResetError("connection reset by peer"),
        "a generic OS error surfaced": OSError("broken pipe"),
        "an exception type this module has never seen":
            RuntimeError("something nobody modelled"),
        "some recipients were refused after the message was transmitted":
            RuntimeError("SMTP refused recipients: ['x@example.invalid']"),
    }
    for name, error in ambiguous.items():
        check("an unproved failure is ambiguous",
              sub.classify_transmit_exception(error) == sub.AMBIGUOUS_SUBMISSION,
              f"{name} -> {sub.classify_transmit_exception(error)}")

    check("the DEFAULT is ambiguous, so an unmodelled failure fails safe",
          sub.classify_transmit_exception(Exception("?")) == sub.AMBIGUOUS_SUBMISSION)
    PASSED.append("only_protocol_evidence_makes_a_failure_retryable")


class _ScriptedSMTP:
    """A scripted SMTP transport. Connects to nothing and sends nothing."""

    def __init__(self, *, fail_on: str | None = None, error: BaseException | None = None):
        self.fail_on = fail_on
        self.error = error
        self.calls: list[str] = []

    def _step(self, name: str):
        self.calls.append(name)
        if self.fail_on == name and self.error is not None:
            raise self.error

    def starttls(self, context=None):
        self._step("starttls")

    def login(self, username, password):
        self._step("login")

    def send_message(self, message):
        self._step("send_message")

    def sendmail(self, sender, recipients, message):
        self._step("sendmail")
        return {}

    def quit(self):
        self.calls.append("quit")


def _settings() -> sub.SmtpSessionSettings:
    return sub.SmtpSessionSettings(
        host="smtp.invalid", port=587, timeout_seconds=5, use_tls=True,
        use_ssl=False, username="u", password="p")


def test_the_session_says_where_a_failure_happened_and_what_it_proves() -> None:
    """Everything before the transmit step is definite BY CONSTRUCTION."""
    for phase, expected_phase in (("connect", sub.PHASE_CONNECT),
                                  ("starttls", sub.PHASE_STARTTLS),
                                  ("login", sub.PHASE_LOGIN)):
        transport = _ScriptedSMTP(fail_on=phase,
                                  error=OSError("nothing was transmitted"))

        def factory(host, port, timeout=None, _t=transport, _p=phase):
            if _p == "connect":
                raise OSError("connection refused")
            return _t

        try:
            sub.run_smtp_session(settings=_settings(), smtp_factory=factory,
                                 transmit=lambda smtp: smtp.send_message(object()))
        except sub.SmtpSubmissionError as error:
            check(f"a {phase} failure is definite",
                  error.classification == sub.DEFINITE_NOT_SUBMITTED, str(error))
            check("and the phase is recorded for an operator",
                  error.phase == expected_phase, error.phase)
            check("and it is not ambiguous", not error.ambiguous)
            check("no message was transmitted",
                  "send_message" not in transport.calls, str(transport.calls))
            continue
        raise AssertionError(f"a {phase} failure did not raise")

    transport = _ScriptedSMTP(fail_on="send_message",
                              error=smtplib.SMTPServerDisconnected("dropped"))
    try:
        sub.run_smtp_session(settings=_settings(),
                             smtp_factory=lambda *a, **k: transport,
                             transmit=lambda smtp: smtp.send_message(object()))
    except sub.SmtpSubmissionError as error:
        check("a drop during transmission is ambiguous", error.ambiguous, str(error))
        check("and the phase says so", error.phase == sub.PHASE_TRANSMIT)
    else:  # pragma: no cover - the transport is scripted to raise
        raise AssertionError("the transmit failure did not raise")
    check("the session was still closed", transport.calls[-1] == "quit",
          str(transport.calls))

    # A success path returns the transmit value and closes the session.
    ok = _ScriptedSMTP()
    result = sub.run_smtp_session(settings=_settings(),
                                  smtp_factory=lambda *a, **k: ok,
                                  transmit=lambda smtp: "TRANSMITTED")
    check("a successful session returns the transmit result", result == "TRANSMITTED")
    check("and quits", ok.calls == ["starttls", "login", "quit"], str(ok.calls))

    # A `quit()` that fails AFTER acceptance must not turn a delivered message
    # into a failure — that is the false negative that causes duplicate mail.
    class QuitExplodes(_ScriptedSMTP):
        def quit(self):
            raise smtplib.SMTPServerDisconnected("server hung up on QUIT")

    quitter = QuitExplodes()
    result = sub.run_smtp_session(settings=_settings(),
                                  smtp_factory=lambda *a, **k: quitter,
                                  transmit=lambda smtp: "TRANSMITTED")
    check("a failing QUIT after acceptance is still a success", result == "TRANSMITTED")
    PASSED.append("the_session_says_where_a_failure_happened_and_what_it_proves")


def test_all_four_mailers_share_one_safety_contract() -> None:
    """Four mailers, ONE classification and ONE durable marker. No lookalikes."""
    for label, path, _table, _column in MAILERS:
        source = read(path)
        check("the mailer classifies its SMTP failure",
              "classify_exception(exc)" in source, label)
        check("an ambiguous result is marked ambiguous, not failed",
              "mark_send_ambiguous" in source, label)
        check("a proved rejection is still marked failed",
              "mark_send_failed" in source or "mark_reserved_send_failed" in source,
              label)
        check("the shared classification module is the source of truth",
              "jobs.common.eco_smtp_submission" in source, label)
        check("the mailer refuses to publish for an ineligible message",
              "unresolved_ambiguous_send(" in source, label)
        check("and it reports the ambiguous outcome in its run summary",
              '"smtp_ambiguous_count"' in source
              and '"ambiguous_reconciliation_blocked_count"' in source, label)

    # NO NEW TRANSPORT, NO PROVIDER, NO QUEUE. The correction is a state
    # machine change; the wire protocol is untouched.
    for label, path, _table, _column in MAILERS:
        source = read(path)
        # `force_resend` is an existing, unrelated operator control, so the scan
        # names actual provider SDKs rather than the substring "resend".
        for forbidden in ("import resend", "boto3", "sendgrid", "brevo",
                          "ses_client", "requests.post", "SmtpEmailProvider"):
            check("no external e-mail provider was introduced",
                  forbidden.lower() not in source.lower(), f"{label}: {forbidden}")
        check("example.invalid SMTP remains the only transport",
              "smtp" in source.lower(), label)

    shared = read("jobs/common/eco_smtp_submission.py")
    check("the shared module speaks only smtplib",
          "import smtplib" in shared and "requests" not in shared)
    PASSED.append("all_four_mailers_share_one_safety_contract")


def test_the_eligibility_check_precedes_the_dashboard_capability() -> None:
    """No capability is published or rotated for a message that cannot be sent.

    The dashboard step publishes a snapshot and MAY rotate an expired
    capability. Doing that for a driver whose previous submission is unresolved
    would be remote work performed on behalf of a message the run is then going
    to refuse to send — and a rotation the operator never asked for.
    """
    for label, path, _table, _column in MAILERS:
        source = read(path)
        eligibility = source.index("unresolved_ambiguous_send(")
        dashboard = source.index("render_with_dashboard_link(")
        reservation = source.index("reserve_send(\n") if "reserve_send(\n" in source \
            else source.index("reserve_send(")
        check("eligibility is established BEFORE the dashboard link is obtained",
              eligibility < dashboard, f"{label}: {eligibility} vs {dashboard}")
        check("...which is itself before the send is reserved",
              dashboard < reservation, f"{label}: {dashboard} vs {reservation}")
    PASSED.append("the_eligibility_check_precedes_the_dashboard_capability")


def test_the_bravo_mime_and_archive_path_is_unchanged() -> None:
    """BRAVO weekly still preserves the EXACT MIME bytes it transmitted.

    The person weekly mailer archives the sent message into the IMAP Sent
    folder and stores its SHA-256, which is only meaningful if the bytes stored
    are the bytes sent. The session refactor moved WHERE the transport call is
    wrapped, never WHAT is transmitted, and this pins that.
    """
    from jobs.ecodriving_person import email_delivery as delivery

    settings = delivery.SmtpSettings(
        env_prefix="TEST", host="smtp.invalid", port=587, username="u",
        password="p", use_tls=True, use_ssl=False,
        from_email="no-reply@example.invalid", from_name="Program Ecodriving",
        reply_to=None, timeout_seconds=5)
    prepared = delivery.prepare_email(
        settings=settings, recipient_email="person@example.invalid",
        subject="Raport", html_body="<p>tresc</p>", text_body="tresc")

    sent: dict = {}

    class RecordingSMTP(_ScriptedSMTP):
        def sendmail(self, sender, recipients, message):
            sent["sender"] = sender
            sent["recipients"] = list(recipients)
            sent["bytes"] = message
            return {}

    response = delivery.send_prepared_email(
        settings=settings, prepared=prepared,
        smtp_factory=lambda *a, **k: RecordingSMTP())
    check("the exact prepared MIME bytes are what was transmitted",
          sent["bytes"] == prepared.mime_bytes)
    check("the envelope is unchanged",
          sent["sender"] == prepared.envelope_sender
          and sent["recipients"] == list(prepared.recipients))
    check("the archived digest still describes the transmitted bytes",
          prepared.mime_sha256 == __import__("hashlib").sha256(
              sent["bytes"]).hexdigest())
    check("and the provider response is the same string the send log records",
          response == "SMTP accepted message without raising an exception")

    # A drop during that same `sendmail` is ambiguous, exactly like the other
    # three mailers — the MIME-preserving path gets no weaker guarantee.
    dropping = _ScriptedSMTP(fail_on="sendmail",
                             error=smtplib.SMTPServerDisconnected("dropped"))
    try:
        delivery.send_prepared_email(settings=settings, prepared=prepared,
                                     smtp_factory=lambda *a, **k: dropping)
    except sub.SmtpSubmissionError as error:
        check("BRAVO weekly classifies its drop as ambiguous too", error.ambiguous)
    else:  # pragma: no cover
        raise AssertionError("the BRAVO weekly drop did not raise")
    PASSED.append("the_bravo_mime_and_archive_path_is_unchanged")


# ==============================================================================
# TIER A — the two production send modes, and what an SMTPDataError proves
# ==============================================================================


def test_an_smtp_data_error_is_read_from_its_reply_code_not_its_type() -> None:
    """`SMTPDataError` is only a proved rejection when it carries a 4xx/5xx.

    THE DEFECT THIS CLOSES. `smtplib.SMTP.data()` raises `SMTPDataError` both
    BEFORE the body is written (the `DATA` command was not answered with 354)
    and AFTER the whole message has been transmitted (`sendmail` saw a final
    reply that was not 250). Only the second case can have delivered anything,
    and in that case the reply code is the ONLY evidence available. But
    `getreply()` returns `smtp_code = -1` for a response it could not parse, so
    `SMTPDataError(-1, b"garbled")` is an exception of the "definite" type that
    proves nothing at all. Classifying it as retryable would put a second copy
    of the message in the driver's inbox on the next ordinary run.
    """
    proved = {
        "a transient 4xx refusal of the message": 400,
        "insufficient storage for the message": 452,
        "a permanent server-side refusal": 500,
        "the transaction failed": 554,
        "the bottom of the negative range": 400,
        "the top of the negative range": 599,
    }
    for name, code in proved.items():
        error = smtplib.SMTPDataError(code, b"rejected")
        check("a real negative reply proves the message was refused",
              sub.classify_transmit_exception(error) == sub.DEFINITE_NOT_SUBMITTED,
              f"{name} ({code}) -> {sub.classify_transmit_exception(error)}")

    unproved = {
        "smtplib could not parse the response line at all": -1,
        "no code was reported": 0,
        "an unexpected success code": 250,
        "an unexpected intermediate code": 354,
        "a code outside every SMTP class": 600,
        "just below the negative range": 399,
        "an absurd out-of-range code": 999,
    }
    for name, code in unproved.items():
        error = smtplib.SMTPDataError(code, b"garbled")
        check("anything that is not a negative reply is AMBIGUOUS",
              sub.classify_transmit_exception(error) == sub.AMBIGUOUS_SUBMISSION,
              f"{name} ({code}) -> {sub.classify_transmit_exception(error)}")

    # A code the runtime could not represent as a number is not evidence
    # either. These are defensive rather than expected from CPython, and the
    # rule is the same one: no negative reply, no proof.
    for name, code in (("no code attribute value at all", None),
                       ("a non-numeric code", "unavailable"),
                       ("a boolean that would read as code 1", True),
                       ("an object that is not a code", object())):
        error = smtplib.SMTPDataError(554, b"rejected")
        error.smtp_code = code
        check("an unrepresentable code is not proof of rejection",
              sub.classify_transmit_exception(error) == sub.AMBIGUOUS_SUBMISSION,
              f"{name} -> {sub.classify_transmit_exception(error)}")

    # A NUMERIC STRING IS STILL A NEGATIVE REPLY. Accepting it cannot weaken
    # the rule: the range test is what decides, not the Python type.
    text_coded = smtplib.SMTPDataError(554, b"rejected")
    text_coded.smtp_code = "554"
    check("a textual 5xx is still a proved rejection",
          sub.classify_transmit_exception(text_coded) == sub.DEFINITE_NOT_SUBMITTED)

    # The classifications the review already accepted are untouched.
    unchanged = {
        "MAIL FROM was rejected":
            (smtplib.SMTPSenderRefused(550, b"sender rejected", "from@example.invalid"),
             sub.DEFINITE_NOT_SUBMITTED),
        "every recipient was rejected":
            (smtplib.SMTPRecipientsRefused({"to@example.invalid": (550, b"no user")}),
             sub.DEFINITE_NOT_SUBMITTED),
        "the greeting failed": (smtplib.SMTPHeloError(500, b"bad helo"),
                                sub.DEFINITE_NOT_SUBMITTED),
        "SMTPUTF8 is unsupported":
            (smtplib.SMTPNotSupportedError("SMTPUTF8 not supported"),
             sub.DEFINITE_NOT_SUBMITTED),
        "the connection dropped mid-DATA":
            (smtplib.SMTPServerDisconnected("closed"), sub.AMBIGUOUS_SUBMISSION),
        "the final response never arrived":
            (socket.timeout("timed out"), sub.AMBIGUOUS_SUBMISSION),
    }
    for name, (error, expected) in unchanged.items():
        check("an already-approved classification is unchanged",
              sub.classify_transmit_exception(error) == expected,
              f"{name} -> {sub.classify_transmit_exception(error)}")

    # And the whole-exception entry point agrees with the transmit rule.
    wrapped = sub.SmtpSubmissionError(
        sub.AMBIGUOUS_SUBMISSION, sub.PHASE_TRANSMIT,
        smtplib.SMTPDataError(-1, b"garbled"))
    check("the wrapper carries the ambiguous verdict outward", wrapped.ambiguous)
    check("and a bare unparsable data error classifies the same way",
          sub.classify_exception(smtplib.SMTPDataError(-1, b"garbled"))
          == sub.AMBIGUOUS_SUBMISSION)
    PASSED.append("an_smtp_data_error_is_read_from_its_reply_code_not_its_type")


def test_a_garbled_final_reply_travels_the_ambiguous_path_end_to_end() -> None:
    """`SMTPDataError(-1, ...)` out of a real session becomes an ambiguous row.

    The classification is only useful if the session runner carries it: an
    unparsable final reply must surface as an ambiguous `SmtpSubmissionError`
    with the operator detail attached, exactly like a mid-DATA disconnect.
    """
    settings = sub.SmtpSessionSettings(
        host="smtp.invalid", port=587, timeout_seconds=5, use_tls=True,
        use_ssl=False, username="u", password="p")

    def transmit(_smtp):
        raise smtplib.SMTPDataError(-1, b"garbled")

    try:
        sub.run_smtp_session(settings=settings, transmit=transmit,
                             smtp_factory=lambda *a, **k: _ScriptedSMTP())
    except sub.SmtpSubmissionError as error:
        check("an unparsable final reply is ambiguous, not a proved rejection",
              error.ambiguous, error.classification)
        check("and it is reported as a transmit-phase event",
              error.phase == sub.PHASE_TRANSMIT, error.phase)
        check("with detail an operator can act on",
              "SMTPDataError" in error.operator_detail, error.operator_detail)
    else:  # pragma: no cover
        raise AssertionError("the garbled final reply did not raise")

    # The same session with a REAL 5xx stays definite and therefore retryable.
    def rejected(_smtp):
        raise smtplib.SMTPDataError(554, b"transaction failed")

    try:
        sub.run_smtp_session(settings=settings, transmit=rejected,
                             smtp_factory=lambda *a, **k: _ScriptedSMTP())
    except sub.SmtpSubmissionError as error:
        check("a real 5xx refusal is still definite and retryable",
              error.classification == sub.DEFINITE_NOT_SUBMITTED, error.classification)
    else:  # pragma: no cover
        raise AssertionError("the rejected message did not raise")
    PASSED.append("a_garbled_final_reply_travels_the_ambiguous_path_end_to_end")


def test_both_production_modes_share_one_ambiguity_gate() -> None:
    """The gate is a property of the execution contract, not of four `if`s.

    `force_resend` exists to override an established `sent`. An unresolved
    ambiguous submission is not an established anything, so both production
    modes ask the same question, and the exemption is `test_send` only.
    """
    from jobs.ecodriving.email_safety import ExecutionMode, resolve_execution_contract

    gated = {
        "normal_send": {"execution_mode": "normal_send"},
        "force_resend": {"execution_mode": "force_resend",
                         "force_resend_reason": "operator asked for a resend"},
    }
    for mode, params in gated.items():
        contract = resolve_execution_contract(params)
        check("a production send is gated on unresolved ambiguity",
              contract.blocks_on_unresolved_ambiguous_send, mode)
        check("and it addresses the driver's real recipient",
              contract.is_real_recipient_scope and contract.test_recipient_email is None,
              mode)

    exempt = {
        "test_send": {"execution_mode": "test_send",
                      "test_recipient_email": "ops@example.invalid"},
        "render_only": {"execution_mode": "render_only"},
    }
    for mode, params in exempt.items():
        contract = resolve_execution_contract(params)
        check("only a test or render-only run is exempt",
              not contract.blocks_on_unresolved_ambiguous_send, mode)

    # THE EXEMPTION IS BOUNDED BY CONSTRUCTION, NOT BY TRUST.
    test_contract = resolve_execution_contract(
        {"execution_mode": "test_send", "test_recipient_email": "ops@example.invalid"})
    check("a test send is its own delivery scope",
          test_contract.send_scope == "test", test_contract.send_scope)
    check("a test send always has an explicit test mailbox",
          test_contract.test_recipient_email == "ops@example.invalid")
    check("and it never claims the real recipient scope",
          not test_contract.is_real_recipient_scope)
    for missing in ({"execution_mode": "test_send"},):
        try:
            resolve_execution_contract(missing)
        except Exception as error:
            check("a test send without a test mailbox is refused",
                  getattr(error, "code", "") == "TEST_RECIPIENT_REQUIRED", str(error))
        else:  # pragma: no cover
            raise AssertionError("a test send without a recipient was accepted")
    for production in ({"execution_mode": "normal_send",
                        "test_recipient_email": "ops@example.invalid"},
                       {"execution_mode": "force_resend",
                        "force_resend_reason": "r",
                        "test_recipient_email": "ops@example.invalid"}):
        try:
            resolve_execution_contract(production)
        except Exception as error:
            check("a production send cannot be redirected to a test mailbox",
                  getattr(error, "code", "") == "NORMAL_RECIPIENT_SCOPE_REQUIRED",
                  str(error))
        else:  # pragma: no cover
            raise AssertionError("a production send accepted a test recipient")

    check("the forced scope is distinct from the normal one",
          resolve_execution_contract(gated["force_resend"]).send_scope == "forced")
    check("and both are production scopes, never 'test'",
          {resolve_execution_contract(p).send_scope for p in gated.values()}
          == {"normal", "forced"})
    check("the enum still carries exactly the four modes",
          {m.value for m in ExecutionMode}
          == {"render_only", "test_send", "normal_send", "force_resend"})

    # ONE implementation, used by all four mailers, and no mailer keeps a
    # NORMAL_SEND-only spelling of the gate.
    for label, path, _table, _column in MAILERS:
        source = read(path)
        check("the mailer gates on the shared contract property",
              "if execution.blocks_on_unresolved_ambiguous_send:" in source, label)
        check("and no NORMAL_SEND-only gate is left in front of the dashboard",
              "ExecutionMode.NORMAL_SEND \\\n" not in source
              and 'execution.send_scope != "test"' not in source, label)
    PASSED.append("both_production_modes_share_one_ambiguity_gate")


class _DashboardReached(AssertionError):
    """Raised the instant a run performs dashboard, reservation or SMTP work."""


class _ExplodingDashboardService:
    """A dashboard service that cannot be touched without saying so.

    It is installed in place of the real `EcoDashboardLinkService`, and the
    REAL `render_with_dashboard_link` is left in the job untouched — so the only
    way `link_for` runs is the job actually walking its dashboard step. That is
    the evidence in both directions: a blocked candidate must return a normal
    summary with this service never asked anything, and an eligible candidate
    must reach it.
    """

    instances: list["_ExplodingDashboardService"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.enabled = True
        self.closed = False
        self.reached: list[dict] = []
        _ExplodingDashboardService.instances.append(self)

    def summary(self) -> dict:
        return {"dashboard_link_enabled": True}

    def close(self) -> None:
        self.closed = True

    def link_for(self, *, identity_key: str, recipient_email: str):
        self.reached.append({"identity_key": identity_key,
                             "recipient_email": recipient_email})
        raise _DashboardReached(f"link_for({identity_key} -> {recipient_email})")

    #: The publication seam the real service calls. Present so a refactor that
    #: bypassed `link_for` still could not publish quietly.
    def ensure_capability(self, *a, **k):  # pragma: no cover - defensive
        raise _DashboardReached("ensure_capability was invoked")

    def template_placement_invalid(self, detail: str):  # pragma: no cover
        raise _DashboardReached(f"placement refusal: {detail}")

    def template_link_missing(self, outcome):  # pragma: no cover
        raise _DashboardReached("link presence refusal")

    def bound_send_scope(self) -> str:
        """The delivery scope the JOB bound this service to at construction."""
        return str(self.kwargs.get("send_scope"))


class _FakeCursor:
    """Records every statement. Answers nothing the harness did not script."""

    def __init__(self, owner: "_FakeConn"):
        self.owner = owner
        self.rowcount = 0

    def execute(self, query, args=None):
        self.owner.queries.append((str(query), args))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.queries: list[tuple] = []
        self.commits = 0

    def cursor(self, *a, **k):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


class _RunClient:
    def __init__(self):
        self.logs: list[tuple] = []

    def log(self, *a, **k):
        self.logs.append((a, k))

    def contexts(self) -> list[dict]:
        return [dict(kwargs.get("context") or {}) for _args, kwargs in self.logs]


class _DrivenRun:
    """What one scripted `run()` did, and what it never touched."""

    def __init__(self, summary, error, conn, client, services):
        self.summary = summary
        self.error = error
        self.conn = conn
        self.client = client
        self.services = services

    @property
    def dashboard(self) -> "_ExplodingDashboardService":
        assert len(self.services) == 1, f"{len(self.services)} dashboard services"
        return self.services[0]


_MISSING = object()

_GATED_TEMPLATE = ("<p>tresc</p>\n<p>{eco_dashboard_section_html}</p>"
                   "\n<p>{eco_dashboard_link_html}</p>")

NORMAL_PARAMS = {"execution_mode": "normal_send"}
FORCED_PARAMS = {"execution_mode": "force_resend",
                 "force_resend_reason": "operator asked for a resend"}
TEST_PARAMS = {"execution_mode": "test_send",
               "test_recipient_email": "ops@example.invalid"}

#: The driver identity every scripted candidate carries, for both key columns.
SUBJECT = "DRIVER-AMBIGUOUS"
DRIVER_RECIPIENT = "driver@example.invalid"


def _mailer_modules():
    """The four mailers as modules. Imported lazily: only this section drives them."""
    from jobs.ecodriving import job_eco_driving_monthly_email_notifications as dm
    from jobs.ecodriving import job_eco_driving_weekly_email_notifications as dw
    from jobs.ecodriving_person import (
        job_eco_driving_person_monthly_email_notifications as pm)
    from jobs.ecodriving_person import (
        job_eco_driving_person_weekly_email_notifications as pw)
    return (("ALPHA weekly", dw, False, False),
            ("ALPHA monthly", dm, False, True),
            ("BRAVO weekly", pw, True, False),
            ("BRAVO monthly", pm, True, True))


def _drive_mailer(mod, *, person: bool, monthly: bool, params: dict,
                  unresolved: bool, conn=None, subject: str = SUBJECT,
                  patch_unresolved: bool = True,
                  period: tuple[date, date] | None = None) -> _DrivenRun:
    """Run the REAL `run()` of one mailer with no transport and no SMTP.

    Everything the run needs from OUTSIDE is scripted. Everything the run
    DECIDES is the real code, including the eligibility gate, the ordering
    around it and the run accounting. The dashboard service, `reserve_send` and
    the SMTP call all raise `_DashboardReached`, so a path that gets past the
    gate is loud rather than silent.

    `conn` accepts a REAL client-business connection so Tier B can run the same
    driver against the real send log and the real dashboard ledger, with
    `patch_unresolved=False` leaving the eligibility query itself real.
    """
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    conn = _FakeConn() if conn is None else conn
    client = _RunClient()
    saved: dict = {}

    def patch(name, value):
        if name not in saved:
            saved[name] = getattr(mod, name, _MISSING)
        setattr(mod, name, value)

    start, end, label = ((date(2026, 6, 1), date(2026, 7, 1), "2026-06") if monthly
                         else (date(2026, 7, 1), date(2026, 7, 20), "W3"))
    if period is not None:
        # Tier B drives the same code against the REAL send log, so the run's
        # period must be the period the seeded rows were written for.
        start, end = period
    finalized = (datetime.combine(end, datetime.min.time(), ZoneInfo("Europe/Warsaw"))
                 + timedelta(minutes=1))
    row = {
        "client_id": CLIENT_ID, "period_start_date": start, "period_end_date": end,
        "recipient_email": DRIVER_RECIPIENT, "ecodriving_rating_type": "bezpieczny",
        "ranking_included": False, "qualification_status": "OK",
        "ranking_type": "EXCLUDED",
    }
    if person:
        row.update(person_name_group_key=subject, person_name="Person")
    else:
        row.update(assigned_id=subject)

    blocked = rec.AmbiguousSend(
        send_log_id="00000000-0000-0000-0000-0000000000aa", status="pending",
        phase=sub.PHASE_TRANSMIT,
        detail="TRANSMIT: SMTPServerDisconnected: dropped after DATA",
        marked_at="2026-08-01T06:00:00+02:00")

    def explode(message):
        def _boom(*a, **k):
            raise _DashboardReached(message)
        return _boom

    try:
        patch("_load_client_account_config", lambda **k: SimpleNamespace(
            client_db_schema="public",
            client_code="BRAVO00016" if person else "ALPHA00001"))
        patch("_client_business_pg_conn", lambda cfg: conn)
        patch("validate_template_inventory", lambda p: None)
        patch("validate_template_dir_link_placeholders", lambda *a, **k: None)
        patch("_table_columns", lambda *a, **k: (
            {"email", "person_name"} if person else {"email", "driver_name"}))
        patch("_select_email_column", lambda c: "email")
        patch("fetch_period_candidates", lambda *a, **k: [{
            "period_start_date": start, "period_end_date": end,
            "period_label": label, "snapshot_min_updated_at": finalized,
            "snapshot_max_updated_at": finalized}])
        patch("_fetch_candidates", lambda *a, **k: [dict(row)])
        patch("classify_candidate", lambda *a, **k: mod.CandidateDecision(
            None, "bezpieczny", "template.html", "norank"))
        patch("_already_sent", lambda *a, **k: False)
        patch("_load_template", lambda **k: _GATED_TEMPLATE)
        patch("build_template_context", lambda r: {})
        patch("render_template", lambda *a, **k: "<p>rendered</p>")
        patch("render_subject", lambda *a, **k: "Raport")
        patch("load_smtp_settings_from_env", lambda *a, **k: SimpleNamespace(
            env_prefix="TEST", from_name="x", from_email="x@example.invalid"))
        patch("_sent_archive_settings", lambda *a, **k: None)

        patch("EcoDashboardLinkService", _ExplodingDashboardService)
        if patch_unresolved:
            patch("unresolved_ambiguous_send",
                  lambda *a, **k: blocked if unresolved else None)
        patch("reserve_send", explode("reserve_send was reached"))
        patch("submit_prepared_email", explode("SMTP was reached"))
        if hasattr(mod, "send_html_email"):
            patch("send_html_email", explode("SMTP was reached"))

        _ExplodingDashboardService.instances = []
        summary = None
        error = None
        try:
            summary = mod.run(client, "run-gate-test",
                              {"client_id": CLIENT_ID, **params})
        except _DashboardReached as reached:
            error = reached
        return _DrivenRun(summary, error, conn, client,
                          list(_ExplodingDashboardService.instances))
    finally:
        for name, value in saved.items():
            if value is _MISSING:
                delattr(mod, name)
            else:
                setattr(mod, name, value)


def test_an_unresolved_ambiguity_stops_both_production_modes_before_the_dashboard() -> None:
    """No publication, no capability, no reservation, no SMTP — in EITHER mode.

    THE DEFECT THIS CLOSES. The early gate ran for `normal_send` only. A
    `force_resend` therefore walked into the dashboard step, where an EXPIRED
    capability is recovered by ROTATION — the predecessor revoked, a new bearer
    minted — and only then reached `reserve_send`, which correctly refused. The
    operator got a rotated capability, no e-mail, and an invalidated link inside
    a message example.invalid may already have accepted.
    """
    for label, mod, person, monthly in _mailer_modules():
        for mode, params in (("normal_send", NORMAL_PARAMS),
                             ("force_resend", FORCED_PARAMS)):
            tag = f"{label}/{mode}"
            run = _drive_mailer(mod, person=person, monthly=monthly,
                                params=params, unresolved=True)
            check("the run completed instead of causing a remote effect",
                  run.error is None, f"{tag}: {run.error}")
            check("the run reports the mode the operator asked for",
                  run.summary["execution_mode"] == mode, tag)
            check("the candidate was withheld for reconciliation",
                  run.summary["ambiguous_reconciliation_blocked_count"] == 1, tag)
            check("counted as blocked, never as a send",
                  run.summary["idempotency_blocked_count"] == 1
                  and run.summary["sent_count"] == 0
                  and run.summary["smtp_attempt_count"] == 0
                  and run.summary["smtp_ambiguous_count"] == 0, f"{tag}: {run.summary}")
            check("the dashboard service was never asked for anything",
                  run.dashboard.reached == [], tag)
            check("and it was still closed cleanly", run.dashboard.closed, tag)

            # `reserve_send` and the SMTP call explode in this harness, so the
            # absence of `_DashboardReached` above already proves neither ran.
            # The statement scan adds that no dashboard ledger row was even
            # named on the job's own connection.
            statements = " ".join(q for q, _a in run.conn.queries).lower()
            check("no dashboard ledger statement was issued",
                  "eco_dashboard_delivery_operation" not in statements, tag)

            # The operator is told exactly what happened and what was NOT done.
            withheld = [c for c in run.client.contexts()
                        if c.get("operator_action_required") is True]
            check("the run says a human is required", withheld, tag)
            check("and states that no capability was touched",
                  all(c.get("dashboard_capability_touched") is False
                      for c in withheld), tag)
            check("naming the unresolved row, not the message content",
                  all(c.get("ambiguous_send_log_id")
                      == "00000000-0000-0000-0000-0000000000aa" for c in withheld),
                  tag)
    PASSED.append("an_unresolved_ambiguity_stops_both_production_modes_before_the_dashboard")


def test_the_gate_blocks_only_what_it_must() -> None:
    """With nothing unresolved, every mode still does its normal work.

    A gate that blocked more than the ambiguous case would be a silent outage:
    forced resends and test sends would stop producing dashboards. So the same
    harness, with no unresolved row, must REACH the dashboard in all three send
    modes — and the test send must reach it as the test mailbox, in the test
    scope, never as the driver.
    """
    for label, mod, person, monthly in _mailer_modules():
        for mode, params, recipient in (
                ("normal_send", NORMAL_PARAMS, DRIVER_RECIPIENT),
                ("force_resend", FORCED_PARAMS, DRIVER_RECIPIENT),
                ("test_send", TEST_PARAMS, "ops@example.invalid")):
            tag = f"{label}/{mode}"
            run = _drive_mailer(mod, person=person, monthly=monthly,
                                params=params, unresolved=False)
            check("an eligible candidate does reach the dashboard step",
                  run.error is not None, tag)
            check("through link_for, once, for this driver",
                  [r["identity_key"] for r in run.dashboard.reached] == [SUBJECT],
                  f"{tag}: {run.dashboard.reached}")
            check("addressed to the address the message will actually go to",
                  run.dashboard.reached[0]["recipient_email"] == recipient,
                  f"{tag}: {run.dashboard.reached}")
            expected_scope = {"normal_send": "normal", "force_resend": "forced",
                              "test_send": "test"}[mode]
            check("and bound to the delivery scope of that mode",
                  run.dashboard.bound_send_scope() == expected_scope,
                  f"{tag}: {run.dashboard.bound_send_scope()}")
    PASSED.append("the_gate_blocks_only_what_it_must")


def test_a_forced_resend_still_overrides_an_established_sent() -> None:
    """`force_resend` keeps the power it is for: overriding a KNOWN `sent`.

    The correction removes exactly one thing from force-resend — the ability to
    proceed past an UNKNOWN SMTP outcome. Its product contract is otherwise
    untouched: it skips the already-sent short circuit, carries its operator
    reason, reserves in the `forced` scope and links itself to the send it is
    replacing.
    """
    for label, path, _table, _column in MAILERS:
        source = read(path)
        # The already-sent short circuit is NORMAL_SEND's, and forced still
        # bypasses it — that is what an override means.
        check("the already-sent short circuit stays normal-send only",
              "if decision.should_send and execution.mode is ExecutionMode.NORMAL_SEND:"
              in source, label)

    from jobs.ecodriving.email_safety import resolve_execution_contract
    forced = resolve_execution_contract(FORCED_PARAMS)
    check("forced still requires and carries an operator reason",
          forced.force_resend and forced.force_resend_reason
          == "operator asked for a resend")
    check("and it still addresses the driver's real recipient",
          forced.is_real_recipient_scope and forced.test_recipient_email is None)

    # And the reservation layer still links a forced send to its parent, which
    # is only meaningful because forced proceeds past an established `sent`.
    idem = read("jobs/ecodriving/email_idempotency.py")
    check("a forced reservation finds the sent row it supersedes",
          "parent_send_log_id" in idem and "AND send_scope='normal' AND status='sent'"
          in idem)
    PASSED.append("a_forced_resend_still_overrides_an_established_sent")


# ==============================================================================
# TIER B — the durable contract, against the REAL send logs
# ==============================================================================


class _Decision:
    template_type = "bezpieczni"
    template_filename = "Tygodniowe - Bezpieczni.html"
    template_variant = "ranked"


def _apply_eco_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    for number in ECO_MIGRATIONS:
        matches = sorted((REPO_ROOT / "db" / "client_business").glob(f"{number}_*.sql"))
        if not matches:  # pragma: no cover - the chain is pinned above
            raise AssertionError(f"migration {number} is missing")
        sql = matches[0].read_text(encoding="utf-8").replace("SET LOCAL ", "SET ")
        with conn.cursor() as cur:
            cur.execute(sql)


#: The person send logs carry a real FK into their stats table, so the subjects
#: this suite reserves for must exist there. Seeding them is not a fixture
#: convenience: it is what keeps the suite honest about running against the REAL
#: schema instead of a permissive lookalike.
SUBJECTS = ("DRIVER-DEFINITE", "DRIVER-AMBIGUOUS", "DRIVER-HEALTHY")


def _seed_person_stats(conn) -> None:
    for subject in SUBJECTS:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.eco_person_weekly_stats (
                    client_id, person_name_group_key, person_name,
                    week_start_date, week_end_date,
                    period_start_date, period_end_date, month_start_date,
                    period_sequence_in_month, period_label,
                    qualification_status, calculation_status, ranking_group)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'QUALIFIED','OK','INCLUDED')
                ON CONFLICT DO NOTHING""",
                (CLIENT_ID, subject, subject, PERIOD_START, PERIOD_END,
                 PERIOD_START, PERIOD_END, PERIOD_START, 3, "2026-05-W3"))
            cur.execute("""
                INSERT INTO public.eco_person_monthly_stats (
                    client_id, person_name_group_key, person_name,
                    month_start_date, month_end_date,
                    qualification_status, calculation_status, ranking_group)
                VALUES (%s,%s,%s,%s,%s,'QUALIFIED','OK','INCLUDED')
                ON CONFLICT DO NOTHING""",
                (CLIENT_ID, subject, subject, MONTH_START, MONTH_END))


def _driver_row(assigned_id: str, *, report_type: str = "weekly") -> dict:
    start, end = _period_for(report_type)
    return {
        "client_id": CLIENT_ID,
        "assigned_id": assigned_id,
        "person_name_group_key": assigned_id,
        "person_name": assigned_id,
        "period_start_date": start,
        "period_end_date": end,
        "ranking_type": "driver",
        "qualification_status": "QUALIFIED",
        "ranking_included": True,
        "ecodriving_rating_type": "bezpieczni",
    }


def _period_for(report_type: str) -> tuple[date, date]:
    """Weekly is a cumulative W3 window; monthly is one closed month."""
    return ((PERIOD_START, PERIOD_END) if report_type == "weekly"
            else (MONTH_START, MONTH_END))


def _report_type_of(table: str) -> str:
    return "monthly" if "monthly" in table else "weekly"


def _reserve(cur, module, table, assigned_id, *, scope="normal", force_reason=None):
    report_type = _report_type_of(table)
    kwargs = dict(
        send_log_table=f"public.{table}", report_type=report_type,
        run_id=str(uuid.uuid4()),
        row=_driver_row(assigned_id, report_type=report_type),
        decision=_Decision(), recipient_email=f"{assigned_id}@example.invalid",
        original_recipient_email=f"{assigned_id}@example.invalid",
        subject="Raport", send_scope=scope, pending_stale_after_minutes=120,
        metadata_json={"synthetic": True},
    )
    if "force_resend_reason" in module.reserve_send.__code__.co_varnames:
        kwargs["force_resend_reason"] = force_reason
    return module.reserve_send(cur, **kwargs)


def _status_of(cur, table: str, send_log_id: str) -> tuple:
    cur.execute(f"""SELECT status, sent_at, error_message,
                           metadata_json ->> '{rec.KEY_RESULT}',
                           metadata_json ->> '{rec.KEY_PHASE}',
                           (metadata_json ->> '{rec.KEY_REQUIRES_OPERATOR}')::boolean
                      FROM public.{table} WHERE send_log_id = %s::uuid""",
                (send_log_id,))
    return cur.fetchone()


def _tier_b_dashboard_interaction(conn, dsn: str) -> None:
    """Capability handoff, then an ambiguous SMTP result. What each side says.

    The two ledgers answer different questions and neither may answer the
    other's. After a handoff followed by an ambiguous submission:

      * the DASHBOARD ledger says a link was handed to an external mailer and
        says NOTHING about whether a message was delivered — no provider
        identity, no acceptance, no remote delivery;
      * the ECO SEND LOG says the submission is unresolved and needs a human;
      * a normal rerun neither submits nor ROTATES the capability, because the
        eligibility check runs before the dashboard step. A rotation is a real
        remote effect and an operator did not ask for one.
    """
    from jobs.ecodriving import email_idempotency as driver_idem

    # 049 creates the ledger and 050 adds the reviewed external-mailer
    # ownership contract; a fixture carrying only 049 is a schema the approved
    # runtime cannot use.
    migrations = [REPO_ROOT / "db" / "client_business" / name
                  for name in ("049_eco_dashboard_delivery_operation.sql",
                               "050_eco_dashboard_external_mailer_ownership.sql")]
    with conn.cursor() as cur:
        for migration in migrations:
            cur.execute(
                migration.read_text(encoding="utf-8").replace("SET LOCAL ", "SET "))
        cur.execute("""
            INSERT INTO public.eco_dashboard_delivery_operation (
                operation_id, client_id, identity_key, period_type,
                period_start_date, period_end_date, send_scope, subject_ref,
                payload_digest, recipient_identity, recipient_email, state,
                external_mailer, capability_id, capability_secret,
                capability_digest, capability_expires_at, bearer_generation,
                bearer_persisted_at, metadata_json)
            VALUES (%s,%s,%s,'weekly',%s,%s,'normal','subject-ref',%s,
                    'rcpt_x','driver-ambiguous@example.invalid',
                    'EXTERNAL_MAILER_HANDOFF', %s, %s, %s, %s,
                    now() + interval '14 days', 1, now(),
                    jsonb_build_object('external_mailer_handoff',
                        jsonb_build_object('mailer', %s::text)))
            RETURNING delivery_id""",
            ("OP" + "1" * 30, CLIENT_ID, "DRIVER-HANDOFF", PERIOD_START,
             PERIOD_END, "b" * 64, "eco_driving_weekly_email_notifications",
             "e" * 32, "S" * 43,
             __import__("hashlib").sha256(("S" * 43).encode()).hexdigest(),
             "eco_driving_weekly_email_notifications"))

    table = "eco_driving_weekly_email_send_log"
    with conn.cursor() as cur:
        held = _reserve(cur, driver_idem, table, "DRIVER-HANDOFF")
        check("the handed-over delivery reserved its send", held.reserved)
        rec.mark_send_ambiguous(
            cur, send_log_table=f"public.{table}", send_log_id=held.send_log_id,
            phase=sub.PHASE_TRANSMIT,
            detail="TRANSMIT: SMTPServerDisconnected: dropped after DATA",
            error_message="AMBIGUOUS_SUBMISSION/TRANSMIT")

        cur.execute("""SELECT state, external_mailer, provider_name,
                              provider_idempotency_key, provider_message_id,
                              provider_accepted_at, remote_delivered_at,
                              finalized_at, provider_attempts, bearer_generation,
                              capability_id, operator_action_required
                         FROM public.eco_dashboard_delivery_operation""")
        (state, owner, provider, key, message_id, accepted, delivered, finalized,
         attempts, generation, capability_id, operator) = cur.fetchone()
        check("the dashboard ledger still only records the handoff",
              state == "EXTERNAL_MAILER_HANDOFF", str(state))
        check("it names the mailer that owns the send accounting",
              owner == "eco_driving_weekly_email_notifications", str(owner))
        check("and asserts NOTHING about SMTP delivery",
              provider is None and key is None and message_id is None
              and accepted is None and delivered is None and finalized is None
              and attempts == 0)
        check("the dashboard row does not claim an operator problem of its own",
              operator is False)

        # A normal rerun: blocked by the send ledger, and the capability is not
        # touched — same generation, same grant.
        blocked = rec.unresolved_ambiguous_send(
            cur, send_log_table=f"public.{table}", subject_column="assigned_id",
            client_id=CLIENT_ID, subject_value="DRIVER-HANDOFF",
            report_type="weekly", period_start_date=PERIOD_START,
            period_end_date=PERIOD_END)
        check("a normal rerun establishes the message is not eligible",
              blocked is not None and blocked.send_log_id == held.send_log_id)
        rerun = _reserve(cur, driver_idem, table, "DRIVER-HANDOFF")
        check("and reserves nothing",
              rerun.outcome == rec.AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
              rerun.outcome)

        cur.execute("""SELECT bearer_generation, capability_id, state
                         FROM public.eco_dashboard_delivery_operation""")
        after = cur.fetchone()
        check("no capability was rotated for a message that cannot be sent",
              after == (generation, capability_id, state), str(after))

    _tier_b_forced_run_leaves_an_expired_capability_alone(conn, dsn)

    with conn.cursor() as cur:
        # Cleanup: this section owns rows the per-table matrix also uses.
        cur.execute(f"DELETE FROM public.{table} WHERE assigned_id = 'DRIVER-HANDOFF'")
        cur.execute("DROP TABLE public.eco_dashboard_delivery_operation CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS "
                    "public.eco_dashboard_delivery_operation_guard() CASCADE")
    PASSED.append("tier_b: the dashboard ledger never claims an SMTP delivery")
    PASSED.append("tier_b: an unresolved ambiguity rotates no capability")


#: The ledger columns a rotation, a recovery or a publication would move. Read
#: before and after the forced run and compared as a whole, so a change to ANY
#: of them fails the test rather than only the ones this suite thought of.
_LEDGER_COLUMNS = (
    "state, capability_id, capability_digest, capability_expires_at, "
    "bearer_generation, bearer_persisted_at, payload_digest, recipient_email, "
    "external_mailer, attempt_count, provider_attempts, updated_at, "
    "operator_action_required, metadata_json"
)


def _tier_b_forced_run_leaves_an_expired_capability_alone(conn, dsn: str) -> None:
    """A FORCED run over an unresolved ambiguity rotates nothing. The real row.

    THE DEFECT THIS CLOSES, AT THE LEDGER. The early gate ran for `normal_send`
    only, so `force_resend` reached the dashboard step. An EXPIRED capability is
    exactly the case that step does not leave alone: it recovers by ROTATION —
    the predecessor revoked, `bearer_generation` advanced, a new capability
    minted — and the run then hit `reserve_send`, which refused. The link inside
    a message example.invalid may already have accepted was invalidated for nothing.

    This drives the REAL ALPHA weekly `run()` against the REAL send log and the
    REAL migration-049 ledger, with the eligibility query UNPATCHED, and the
    dashboard service replaced by one that raises on contact.
    """
    import psycopg

    from jobs.ecodriving import email_idempotency as driver_idem

    table = "eco_driving_weekly_email_send_log"
    subject = "DRIVER-FORCED"
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.eco_dashboard_delivery_operation (
                operation_id, client_id, identity_key, period_type,
                period_start_date, period_end_date, send_scope, subject_ref,
                payload_digest, recipient_identity, recipient_email, state,
                external_mailer, capability_id, capability_secret,
                capability_digest, capability_expires_at, bearer_generation,
                bearer_persisted_at, metadata_json)
            VALUES (%s,%s,%s,'weekly',%s,%s,'normal','subject-ref',%s,
                    'rcpt_forced', 'driver@example.invalid',
                    'EXTERNAL_MAILER_HANDOFF', %s, %s, %s, %s,
                    now() - interval '2 days', 1, now(),
                    jsonb_build_object('external_mailer_handoff',
                        jsonb_build_object('mailer', %s::text)))""",
            ("OP" + "2" * 30, CLIENT_ID, subject, PERIOD_START, PERIOD_END,
             "c" * 64, "eco_driving_weekly_email_notifications", "f" * 32,
             "T" * 43,
             __import__("hashlib").sha256(("T" * 43).encode()).hexdigest(),
             "eco_driving_weekly_email_notifications"))
        cur.execute("""SELECT capability_expires_at < now()
                         FROM public.eco_dashboard_delivery_operation
                        WHERE identity_key = %s""", (subject,))
        check("the fixture really is an EXPIRED capability", cur.fetchone()[0] is True)

        held = _reserve(cur, driver_idem, table, subject)
        check("the forced-case delivery reserved its send", held.reserved)
        rec.mark_send_ambiguous(
            cur, send_log_table=f"public.{table}", send_log_id=held.send_log_id,
            phase=sub.PHASE_TRANSMIT,
            detail="TRANSMIT: SMTPServerDisconnected: dropped after DATA",
            error_message="AMBIGUOUS_SUBMISSION/TRANSMIT")

        cur.execute(f"""SELECT {_LEDGER_COLUMNS}
                          FROM public.eco_dashboard_delivery_operation
                         WHERE identity_key = %s""", (subject,))
        before = cur.fetchone()

    from jobs.ecodriving import job_eco_driving_weekly_email_notifications as dw

    for mode, params in (("normal_send", NORMAL_PARAMS),
                         ("force_resend", FORCED_PARAMS)):
        job_conn = psycopg.connect(dsn)
        try:
            run = _drive_mailer(dw, person=False, monthly=False, params=params,
                                unresolved=False, conn=job_conn, subject=subject,
                                patch_unresolved=False,
                                period=(PERIOD_START, PERIOD_END))
        finally:
            job_conn.close()
        check("the real eligibility query blocked the run before the dashboard",
              run.error is None, f"{mode}: {run.error}")
        check("and it was accounted as an ambiguity block",
              run.summary["ambiguous_reconciliation_blocked_count"] == 1
              and run.summary["sent_count"] == 0, f"{mode}: {run.summary}")
        check("the dashboard service was never asked for a link",
              run.dashboard.reached == [], mode)

        with conn.cursor() as cur:
            cur.execute(f"""SELECT {_LEDGER_COLUMNS}
                              FROM public.eco_dashboard_delivery_operation
                             WHERE identity_key = %s""", (subject,))
            after = cur.fetchone()
        check("the EXPIRED capability was not rotated, recovered or revoked",
              after == before, f"{mode}: {after} != {before}")

        with conn.cursor() as cur:
            cur.execute(f"""SELECT count(*) FROM public.{table}
                             WHERE assigned_id = %s""", (subject,))
            check("and the run wrote no second send-log row",
                  cur.fetchone()[0] == 1, mode)
            cur.execute(f"""SELECT status, metadata_json ->> '{rec.KEY_RESULT}'
                              FROM public.{table} WHERE send_log_id = %s::uuid""",
                        (held.send_log_id,))
            check("the unresolved row is exactly as the operator will find it",
                  cur.fetchone() == ("pending", rec.VALUE_AMBIGUOUS), mode)

    # DEFENCE IN DEPTH IS STILL THERE. Reaching `reserve_send` anyway — a
    # concurrent process marking the row ambiguous after this run's check —
    # is still refused by the database path, in both production scopes.
    with conn.cursor() as cur:
        for scope, reason in (("normal", None), ("forced", "operator asked")):
            late = _reserve(cur, driver_idem, table, subject, scope=scope,
                            force_reason=reason)
            check("the reservation guard still refuses independently",
                  late.outcome == rec.AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
                  f"{scope}: {late.outcome}")
        cur.execute(f"DELETE FROM public.{table} WHERE assigned_id = %s", (subject,))
        cur.execute("DELETE FROM public.eco_dashboard_delivery_operation "
                    "WHERE identity_key = %s", (subject,))
    PASSED.append("tier_b: a forced run over an unresolved ambiguity rotates "
                  "no expired capability")
    PASSED.append("tier_b: reserve_send remains the durable concurrency backstop")


def tier_b(dsn: str) -> None:
    import psycopg

    from jobs.ecodriving import email_idempotency as driver_idem
    from jobs.ecodriving_person import email_idempotency as person_idem

    conn = psycopg.connect(dsn)
    conn.autocommit = True
    try:
        _apply_eco_schema(conn)
        _seed_person_stats(conn)
        PASSED.append("tier_b: the REAL eco client-business schema applies cleanly")

        modules = {
            "eco_driving_weekly_email_send_log": driver_idem,
            "eco_driving_monthly_email_send_log": driver_idem,
            "eco_person_weekly_email_send_log": person_idem,
            "eco_person_monthly_email_send_log": person_idem,
        }

        for table, module in modules.items():
            subject = "assigned_id" if "driving" in table else "person_name_group_key"
            report_type = _report_type_of(table)
            period_start, period_end = _period_for(report_type)
            with conn.cursor() as cur:
                # --- 1. a definite pre-submission failure stays retryable ----
                safe = _reserve(cur, module, table, "DRIVER-DEFINITE")
                check(f"[{table}] a fresh delivery reserves", safe.reserved, safe.outcome)
                module.mark_send_failed(
                    cur, send_log_table=f"public.{table}",
                    send_log_id=safe.send_log_id,
                    error_message="DEFINITE_NOT_SUBMITTED/CONNECT: refused")
                again = _reserve(cur, module, table, "DRIVER-DEFINITE")
                check(f"[{table}] a proved rejection is retried automatically",
                      again.reserved, again.outcome)

                # --- 2. an ambiguous result is durable and NOT retryable -----
                held = _reserve(cur, module, table, "DRIVER-AMBIGUOUS")
                check(f"[{table}] the ambiguous delivery reserved", held.reserved)
                rec.mark_send_ambiguous(
                    cur, send_log_table=f"public.{table}",
                    send_log_id=held.send_log_id, phase=sub.PHASE_TRANSMIT,
                    detail="TRANSMIT: SMTPServerDisconnected: dropped mid-DATA",
                    error_message="AMBIGUOUS_SUBMISSION/TRANSMIT")
                status, sent_at, error, result, phase, operator = _status_of(
                    cur, table, held.send_log_id)
                check(f"[{table}] the row is NOT downgraded to failed",
                      status == "pending", str(status))
                check(f"[{table}] and claims no delivery", sent_at is None)
                check(f"[{table}] the ambiguity is durable",
                      result == rec.VALUE_AMBIGUOUS and phase == sub.PHASE_TRANSMIT,
                      f"{result}/{phase}")
                check(f"[{table}] and it asks for a human", operator is True)
                check(f"[{table}] the operator can see what happened",
                      bool(error), str(error))

                # --- 3. a NORMAL rerun refuses, and says why ----------------
                rerun = _reserve(cur, module, table, "DRIVER-AMBIGUOUS")
                check(f"[{table}] a normal rerun does not reserve", not rerun.reserved)
                check(f"[{table}] and reports the ambiguity, not a plain block",
                      rerun.outcome == rec.AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
                      rerun.outcome)
                still = _status_of(cur, table, held.send_log_id)
                check(f"[{table}] the rerun changed nothing about the row",
                      (still[0], still[3]) == ("pending", rec.VALUE_AMBIGUOUS),
                      str(still))

                # --- 4. force_resend does NOT bypass it ---------------------
                forced = _reserve(cur, module, table, "DRIVER-AMBIGUOUS",
                                  scope="forced", force_reason="operator asked")
                check(f"[{table}] a forced resend does not reserve either",
                      not forced.reserved, forced.outcome)
                check(f"[{table}] and it is refused for the ambiguity",
                      forced.outcome == rec.AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
                      forced.outcome)
                cur.execute(f"""SELECT count(*) FROM public.{table}
                                 WHERE {subject} = 'DRIVER-AMBIGUOUS'""")
                check(f"[{table}] the forced attempt wrote no second reservation",
                      cur.fetchone()[0] == 1)

                # --- 5. other drivers are unaffected ------------------------
                other = _reserve(cur, module, table, "DRIVER-HEALTHY")
                check(f"[{table}] another driver reserves normally", other.reserved,
                      other.outcome)

                # --- 6. the eligibility query finds it, and only it ----------
                found = rec.unresolved_ambiguous_send(
                    cur, send_log_table=f"public.{table}", subject_column=subject,
                    client_id=CLIENT_ID, subject_value="DRIVER-AMBIGUOUS",
                    report_type=report_type, period_start_date=period_start,
                    period_end_date=period_end)
                check(f"[{table}] the pre-flight check sees the unresolved row",
                      found is not None and found.send_log_id == held.send_log_id)
                clear = rec.unresolved_ambiguous_send(
                    cur, send_log_table=f"public.{table}", subject_column=subject,
                    client_id=CLIENT_ID, subject_value="DRIVER-HEALTHY",
                    report_type=report_type, period_start_date=period_start,
                    period_end_date=period_end)
                check(f"[{table}] and does not block an unrelated driver",
                      clear is None)

                # --- 7. the operator exit, in both directions ---------------
                check(f"[{table}] resolving a row that is not ambiguous is refused",
                      rec.resolve_ambiguous_send(
                          cur, send_log_table=f"public.{table}",
                          send_log_id=other.send_log_id,
                          resolution=rec.RESOLUTION_DELIVERED,
                          operator="ops", reason="mistake") is False)

                done = rec.resolve_ambiguous_send(
                    cur, send_log_table=f"public.{table}",
                    send_log_id=held.send_log_id,
                    resolution=rec.RESOLUTION_NOT_DELIVERED,
                    operator="ops@example.invalid",
                    reason="the Sent folder and the provider log both show nothing")
                check(f"[{table}] the operator can resolve it", done)
                status, sent_at, _e, result, _p, operator = _status_of(
                    cur, table, held.send_log_id)
                check(f"[{table}] 'not delivered' makes it retryable again",
                      status == "failed" and sent_at is None, str(status))
                check(f"[{table}] and the row no longer asks for a human",
                      operator is False and result == "RECONCILED",
                      f"{operator}/{result}")
                after = _reserve(cur, module, table, "DRIVER-AMBIGUOUS")
                check(f"[{table}] so the next normal run sends it exactly once",
                      after.reserved, after.outcome)

                # ...and the other attestation ends it the other way.
                rec.mark_send_ambiguous(
                    cur, send_log_table=f"public.{table}",
                    send_log_id=after.send_log_id, phase=sub.PHASE_TRANSMIT,
                    detail="TRANSMIT: timeout waiting for the final response",
                    error_message="AMBIGUOUS_SUBMISSION/TRANSMIT")
                check(f"[{table}] 'delivered' closes it as sent",
                      rec.resolve_ambiguous_send(
                          cur, send_log_table=f"public.{table}",
                          send_log_id=after.send_log_id,
                          resolution=rec.RESOLUTION_DELIVERED,
                          operator="ops@example.invalid",
                          reason="found in the Sent folder"))
                status, sent_at, _e, result, _p, operator = _status_of(
                    cur, table, after.send_log_id)
                check(f"[{table}] the row is sent, with a delivery timestamp",
                      status == "sent" and sent_at is not None, str(status))
                final = _reserve(cur, module, table, "DRIVER-AMBIGUOUS")
                check(f"[{table}] and no rerun mails that period again",
                      not final.reserved and final.outcome == "existing",
                      final.outcome)

                # --- 8. the operator listing is complete and read-only ------
                listing = rec.list_unresolved_ambiguous_sends(
                    cur, send_log_table=f"public.{table}", subject_column=subject)
                check(f"[{table}] nothing unresolved is left after reconciliation",
                      listing == [], str(listing))

        _tier_b_dashboard_interaction(conn, dsn)

        PASSED.append("tier_b: an ambiguous submission blocks every automatic resend")
        PASSED.append("tier_b: force_resend cannot bypass an unresolved ambiguity")
        PASSED.append("tier_b: the operator reconciliation path is deterministic")
        PASSED.append("tier_b: all four send logs behave identically")
    finally:
        conn.close()


def main() -> int:
    tier_a = [
        test_only_protocol_evidence_makes_a_failure_retryable,
        test_an_smtp_data_error_is_read_from_its_reply_code_not_its_type,
        test_a_garbled_final_reply_travels_the_ambiguous_path_end_to_end,
        test_the_session_says_where_a_failure_happened_and_what_it_proves,
        test_all_four_mailers_share_one_safety_contract,
        test_the_eligibility_check_precedes_the_dashboard_capability,
        test_both_production_modes_share_one_ambiguity_gate,
        test_an_unresolved_ambiguity_stops_both_production_modes_before_the_dashboard,
        test_the_gate_blocks_only_what_it_must,
        test_a_forced_resend_still_overrides_an_established_sent,
        test_the_bravo_mime_and_archive_path_is_unchanged,
    ]
    print("### TIER A — no infrastructure")
    for test in tier_a:
        test()
        print(f"PASS  {test.__name__}")

    dsn = os.getenv(ENV_DSN)
    print("\n### TIER B — the REAL eco send logs on a disposable PostgreSQL")
    if not dsn:
        SKIPPED.append("tier_b")
        print(f"SKIPPED: export {ENV_DSN} (a DISPOSABLE loopback instance) to run "
              "the durable half. It is NOT evidence until it runs.")
    else:
        from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit

        require_loopback_dsn_or_exit(dsn, label=ENV_DSN)
        try:
            import psycopg  # noqa: F401
        except ImportError:
            print("FAILED: psycopg is not importable in this interpreter; a silent "
                  "skip here would look like a pass; use the project interpreter.")
            return 1
        tier_b(dsn)
        for name in [p for p in PASSED if p.startswith("tier_b")]:
            print(f"PASS  {name}")

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} checks passed — Eco e-mail ambiguous-SMTP safety")
    for name in SKIPPED:
        print(f"SKIPPED: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
