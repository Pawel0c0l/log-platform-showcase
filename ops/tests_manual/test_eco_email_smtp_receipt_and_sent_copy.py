#!/usr/bin/env python3
"""Eco Driving e-mail — the relay's receipt, and the copy in the Sent folder.

WHAT THIS PINS

Two narrow claims, for ALL FOUR mailers (ALPHA weekly/monthly, BRAVO person
weekly/monthly), and nothing beyond them:

1.  When example.invalid accepts a message, the send log keeps enough of the relay's own
    answer to prove it: the final reply code, its verbatim text, the queue
    identifier IF the relay emitted one, the `Message-ID` and the moment of
    acceptance. `sent` still means "accepted by example.invalid SMTP for relay" — never
    "delivered".
2.  The EXACT bytes that were transmitted are appended to the sender mailbox's
    Sent folder, and only ever after that acceptance.

And the boundary that matters more than either:

    SMTP accepted -> Sent copy failed  MUST NOT  -> resend.

No network of any kind is opened: every SMTP and IMAP object here is a double,
and the doubles are the same shapes the existing eco suites use.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 \\
        ops/tests_manual/test_eco_email_smtp_receipt_and_sent_copy.py
"""
from __future__ import annotations

import json
import smtplib
import sys
from datetime import date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.common import eco_email_transport as transport  # noqa: E402
from jobs.common import eco_sent_archive as archive  # noqa: E402

CLIENT_ID = "00000000-0000-0000-0000-000000000001"
SEND_LOG_ID = "00000000-0000-0000-0000-0000000000a1"
QUEUE_LINE = b"2.0.0 Ok: queued as 4ABCDEF123"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        return
    FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")


# ==============================================================================
# Transport doubles. None of them touches a socket.
# ==============================================================================


class _BaseSMTP:
    instances: list = []

    def __init__(self, host=None, port=None, timeout=None):
        self.host = host
        self.port = port
        self.transmitted: list[bytes] = []
        self.envelopes: list[tuple] = []
        self.data_calls = 0
        self.quit_called = False
        type(self).instances.append(self)
        _BaseSMTP.instances.append(self)

    def starttls(self, context=None):
        return None

    def login(self, username, password):
        return None

    def quit(self):
        self.quit_called = True


class RelaySMTP(_BaseSMTP):
    """A relay that answers the terminating dot the way a real one does.

    `sendmail` calls `self.data()` and drops its answer on the floor, exactly
    as CPython's `smtplib` does — which is what makes the observer in
    `eco_email_transport` the thing under test rather than a convenience.
    """

    instances: list = []
    final_reply = (250, QUEUE_LINE)

    def data(self, message):
        self.data_calls += 1
        return type(self).final_reply

    def sendmail(self, sender, recipients, message):
        self.envelopes.append((sender, tuple(recipients)))
        self.transmitted.append(message)
        code, resp = self.data(message)
        if code != 250:
            raise smtplib.SMTPDataError(code, resp)
        return {}


class SilentSMTP(_BaseSMTP):
    """A client that exposes no `data()` step at all. Evidence: none."""

    instances: list = []

    def sendmail(self, sender, recipients, message):
        self.envelopes.append((sender, tuple(recipients)))
        self.transmitted.append(message)
        return {}


class RejectingSMTP(_BaseSMTP):
    """A relay that refuses the message with a real 5xx. PROVED not sent."""

    instances: list = []

    def sendmail(self, sender, recipients, message):
        self.envelopes.append((sender, tuple(recipients)))
        self.transmitted.append(message)
        raise smtplib.SMTPDataError(550, b"5.7.1 rejected")


class DroppingSMTP(_BaseSMTP):
    """The connection dies after DATA. AMBIGUOUS — it may already be gone."""

    instances: list = []

    def sendmail(self, sender, recipients, message):
        self.envelopes.append((sender, tuple(recipients)))
        self.transmitted.append(message)
        raise smtplib.SMTPServerDisconnected("dropped after DATA")


class FakeIMAP:
    instances: list = []
    existing_message_ids: set = set()
    append_raises = False

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.append_calls: list[tuple] = []
        FakeIMAP.instances.append(self)

    def login(self, username, password):
        return "OK", [b"logged in"]

    def list(self, reference, pattern):
        return "OK", [b'(\\HasNoChildren \\Sent) "." INBOX.Sent']

    def select(self, mailbox, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        message_id = str(criteria[-1])
        if message_id in FakeIMAP.existing_message_ids:
            return "OK", [b"1"]
        return "OK", [b""]

    def append(self, mailbox, flags, date_time, mime_bytes):
        self.append_calls.append((mailbox, flags, date_time, mime_bytes))
        if FakeIMAP.append_raises:
            raise RuntimeError("IMAP APPEND refused")
        msg = BytesParser(policy=policy.default).parsebytes(mime_bytes)
        FakeIMAP.existing_message_ids.add(str(msg["Message-ID"]))
        return "OK", [b"appended"]

    def logout(self):
        return "OK", [b"bye"]


def _reset_doubles() -> None:
    for cls in (_BaseSMTP, RelaySMTP, SilentSMTP, RejectingSMTP, DroppingSMTP):
        cls.instances = []
    FakeIMAP.instances = []
    FakeIMAP.existing_message_ids = set()
    FakeIMAP.append_raises = False


ARCHIVE_SETTINGS = archive.SentArchiveSettings(
    env_prefix="TEST",
    host="imap.example.invalid",
    port=993,
    username="sender@example.invalid",
    password="imap-secret",
    use_ssl=True,
    timeout_seconds=5,
)

SMTP_SETTINGS = SimpleNamespace(
    env_prefix="TEST",
    host="smtp.example.invalid",
    port=587,
    username="sender@example.invalid",
    password="smtp-secret",
    use_tls=True,
    use_ssl=False,
    from_email="sender@example.invalid",
    from_name="Program Ecodriving",
    reply_to=None,
    timeout_seconds=5,
)


# ==============================================================================
# PART 1 — what the relay said, captured without touching the failure path
# ==============================================================================


def _prepared():
    return transport.prepare_email(
        settings=SMTP_SETTINGS,
        recipient_email="driver@example.invalid",
        subject="Raport",
        html_body="<p>tresc</p>",
        text_body="tresc",
        message_id="<pinned@example.invalid>",
    )


def test_the_relays_own_answer_is_kept() -> None:
    _reset_doubles()
    prepared = _prepared()
    acceptance = transport.submit_prepared_email(
        settings=SMTP_SETTINGS, prepared=prepared, smtp_factory=RelaySMTP)

    check("the final reply code is retained", acceptance.code == 250)
    check("its text is retained verbatim",
          acceptance.response_text == "2.0.0 Ok: queued as 4ABCDEF123",
          str(acceptance.response_text))
    check("the relay's queue identifier is retained",
          acceptance.queue_id == "4ABCDEF123", str(acceptance.queue_id))
    check("the Message-ID is the one that was transmitted",
          acceptance.message_id == prepared.message_id)
    check("the acceptance is timestamped", isinstance(acceptance.accepted_at, datetime))
    check("the send log's provider_response is the relay's own line",
          acceptance.provider_response == "250 2.0.0 Ok: queued as 4ABCDEF123",
          acceptance.provider_response)

    evidence = acceptance.as_metadata()[transport.KEY_ACCEPTANCE]
    check("the durable evidence says accepted FOR RELAY, not delivered",
          evidence["result"] == "ACCEPTED_BY_SMTP_FOR_RELAY"
          and "DELIVERED" not in json.dumps(evidence).upper())
    check("and it names where the evidence came from",
          evidence["evidence"] == transport.EVIDENCE_FINAL_REPLY)
    check("the observer did not disturb the one transmission",
          len(RelaySMTP.instances) == 1
          and RelaySMTP.instances[0].transmitted == [prepared.mime_bytes]
          and RelaySMTP.instances[0].data_calls == 1)
    PASSED.append("the_relays_own_answer_is_kept")


def test_no_evidence_is_never_invented() -> None:
    _reset_doubles()
    prepared = _prepared()
    acceptance = transport.submit_prepared_email(
        settings=SMTP_SETTINGS, prepared=prepared, smtp_factory=SilentSMTP)
    check("a client with no observable DATA step yields no reply code",
          acceptance.code is None and acceptance.response_text is None)
    check("and no queue identifier is manufactured", acceptance.queue_id is None)
    check("the acceptance itself still stands",
          acceptance.provider_response == transport.LEGACY_ACCEPTANCE_RESPONSE)
    check("and the record says the evidence was unobtainable",
          acceptance.as_metadata()[transport.KEY_ACCEPTANCE]["evidence"]
          == transport.EVIDENCE_LIBRARY_RETURN)

    # A relay that says 250 and nothing identifiable gets no invented ID either.
    for text, expected in (
            ("2.0.0 Ok: queued as D6B0F1C0A2", "D6B0F1C0A2"),
            ("OK id=1abcde-0001Ab-2C", "1abcde-0001Ab-2C"),
            ("2.0.0 x9AB2c3d004567 Message accepted for delivery", "x9AB2c3d004567"),
            ("2.0.0 Ok", None),
            ("Message accepted", None),
            ("", None),
            (None, None)):
        check("queue-id parsing is evidence-only",
              transport.parse_queue_id(text) == expected, f"{text!r}")
    PASSED.append("no_evidence_is_never_invented")


def test_a_refusal_is_still_a_refusal() -> None:
    """The observer must not change what a failure proves."""
    import jobs.common.eco_smtp_submission as sub

    _reset_doubles()
    prepared = _prepared()
    for double, expected in ((RejectingSMTP, sub.DEFINITE_NOT_SUBMITTED),
                             (DroppingSMTP, sub.AMBIGUOUS_SUBMISSION)):
        try:
            transport.submit_prepared_email(
                settings=SMTP_SETTINGS, prepared=prepared, smtp_factory=double)
        except sub.SmtpSubmissionError as error:
            check("the classification is unchanged by the receipt capture",
                  error.classification == expected, f"{double.__name__}: {error}")
        else:  # pragma: no cover
            FAILED.append(f"{double.__name__} did not raise")

    # A 4xx/5xx final reply is a proved rejection even when it arrives through
    # the observed `data()` step.
    class FourFiftyRelay(RelaySMTP):
        instances: list = []
        final_reply = (451, b"4.3.0 try later")

    try:
        transport.submit_prepared_email(
            settings=SMTP_SETTINGS, prepared=prepared, smtp_factory=FourFiftyRelay)
    except sub.SmtpSubmissionError as error:
        check("a negative final reply is still definite, not ambiguous",
              error.classification == sub.DEFINITE_NOT_SUBMITTED, str(error))
    else:  # pragma: no cover
        FAILED.append("the 451 relay did not raise")
    PASSED.append("a_refusal_is_still_a_refusal")


# ==============================================================================
# PART 2 — the four mailers, driven end to end with no infrastructure
# ==============================================================================


class _Cursor:
    def __init__(self, owner):
        self.owner = owner
        self.rowcount = 0

    def execute(self, query, args=None):
        self.owner.queries.append((" ".join(str(query).split()), args))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self):
        self.queries: list[tuple] = []
        self.commits = 0

    def cursor(self, *a, **k):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


class _Client:
    def __init__(self):
        self.logs: list[tuple] = []

    def log(self, *a, **k):
        self.logs.append((a, k))

    def contexts(self):
        return [dict(k.get("context") or {}) for _a, k in self.logs]

    def errors(self):
        return [(a, k) for a, k in self.logs if a and a[0] == "ERROR"]


class _Run:
    def __init__(self, summary, conn, client, smtp_double):
        self.summary = summary
        self.conn = conn
        self.client = client
        self.smtp = smtp_double.instances[0] if smtp_double.instances else None
        self.imap = FakeIMAP.instances[0] if FakeIMAP.instances else None

    def updates(self, needle: str) -> list[tuple]:
        return [(q, a) for q, a in self.conn.queries if needle in q]

    def sent_update(self):
        rows = [(q, a) for q, a in self.conn.queries
                if "status = 'sent'" in q or "status='sent'" in q]
        return rows[0] if rows else None


MAILERS = ("ALPHA weekly", "ALPHA monthly", "BRAVO weekly", "BRAVO monthly")

_MISSING = object()


def _mailer(label: str):
    from jobs.ecodriving import job_eco_driving_monthly_email_notifications as dm
    from jobs.ecodriving import job_eco_driving_weekly_email_notifications as dw
    from jobs.ecodriving_person import (
        job_eco_driving_person_monthly_email_notifications as pm)
    from jobs.ecodriving_person import (
        job_eco_driving_person_weekly_email_notifications as pw)
    return {"ALPHA weekly": (dw, False, False),
            "ALPHA monthly": (dm, False, True),
            "BRAVO weekly": (pw, True, False),
            "BRAVO monthly": (pm, True, True)}[label]


def drive(label: str, *, smtp_double=RelaySMTP, archive_settings=ARCHIVE_SETTINGS,
          append_raises: bool = False) -> _Run:
    """Run the REAL `run()` of one mailer against doubles, through the send."""
    mod, person, monthly = _mailer(label)
    _reset_doubles()
    FakeIMAP.append_raises = append_raises

    conn = _Conn()
    client = _Client()
    saved: dict = {}

    def patch(name, value):
        if name not in saved:
            saved[name] = getattr(mod, name, _MISSING)
        setattr(mod, name, value)

    start, end, period_label = ((date(2026, 6, 1), date(2026, 7, 1), "2026-06")
                                if monthly
                                else (date(2026, 7, 1), date(2026, 7, 20), "W3"))
    finalized = (datetime.combine(end, datetime.min.time(), ZoneInfo("Europe/Warsaw"))
                 + timedelta(minutes=1))
    row = {"client_id": CLIENT_ID, "period_start_date": start, "period_end_date": end,
           "recipient_email": "driver@example.invalid",
           "ecodriving_rating_type": "bezpieczny", "ranking_included": False,
           "qualification_status": "OK", "ranking_type": "EXCLUDED"}
    if person:
        row.update(person_name_group_key="PERSON-KEY", person_name="Person")
    else:
        row.update(assigned_id="DRIVER-1")

    reservation = SimpleNamespace(
        outcome="reserved", send_log_id=SEND_LOG_ID, idempotency_key="k",
        existing_status=None, existing_send_log_id=None, parent_send_log_id=None,
        reserved=True)

    real_submit = transport.submit_prepared_email
    real_store = archive.store_sent_copy

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
            "period_label": period_label, "snapshot_min_updated_at": finalized,
            "snapshot_max_updated_at": finalized}])
        patch("_fetch_candidates", lambda *a, **k: [dict(row)])
        patch("classify_candidate", lambda *a, **k: mod.CandidateDecision(
            None, "bezpieczny", "template.html", "norank"))
        patch("_already_sent", lambda *a, **k: False)
        patch("_load_template", lambda **k: "<p>tresc</p>")
        patch("build_template_context", lambda r: {})
        patch("render_template", lambda *a, **k: "<p>rendered</p>")
        patch("render_subject", lambda *a, **k: "Raport")
        patch("render_with_dashboard_link", lambda *a, **k: (
            "<p>rendered</p>", SimpleNamespace(audit=lambda: {})))
        patch("load_smtp_settings_from_env", lambda *a, **k: SMTP_SETTINGS)
        patch("_sent_archive_settings", lambda *a, **k: archive_settings)
        patch("unresolved_ambiguous_send", lambda *a, **k: None)
        patch("reserve_send", lambda *a, **k: reservation)
        # The transport and the mailbox are the ONLY things replaced inside the
        # real code path: same functions, doubles instead of sockets.
        patch("submit_prepared_email",
              lambda **kw: real_submit(smtp_factory=smtp_double, **kw))
        patch("store_sent_copy",
              lambda **kw: real_store(imap_factory=FakeIMAP, **kw))

        summary = mod.run(client, "run-receipt-test",
                          {"client_id": CLIENT_ID, "execution_mode": "normal_send"})
        return _Run(summary, conn, client, smtp_double)
    finally:
        for name, value in saved.items():
            if value is _MISSING:
                delattr(mod, name)
            else:
                setattr(mod, name, value)


def _sent_copy_record(run: _Run, label: str) -> tuple | None:
    """The statement THIS send log uses to record a Sent-copy outcome."""
    if label.startswith("BRAVO"):
        rows = run.updates("sent_archive_status")
        return rows[-1] if rows else None
    rows = [(q, a) for q, a in run.conn.queries
            if "metadata_json" in q and a
            and any(isinstance(x, str) and archive.KEY_SENT_COPY in x for x in a)]
    return rows[-1] if rows else None


def test_acceptance_is_recorded_with_its_evidence() -> None:
    for label in MAILERS:
        run = drive(label)
        check("the send is counted as accepted",
              run.summary["sent_count"] == 1
              and run.summary["smtp_accepted_count"] == 1
              and run.summary["failed_count"] == 0, label)
        sent = run.sent_update()
        check("the send log row is marked sent", sent is not None, label)
        if sent is None:
            continue
        args = [a for a in sent[1] if isinstance(a, str)]
        check("provider_response is the relay's own final reply",
              "250 2.0.0 Ok: queued as 4ABCDEF123" in args, f"{label}: {args}")
        evidence = [a for a in args if transport.KEY_ACCEPTANCE in a]
        check("the acceptance evidence is persisted with the row",
              len(evidence) == 1, label)
        if evidence:
            payload = json.loads(evidence[0])[transport.KEY_ACCEPTANCE]
            check("with the code, the text and the queue id",
                  payload["smtp_code"] == 250
                  and payload["queue_id"] == "4ABCDEF123"
                  and payload["smtp_response"].startswith("2.0.0 Ok"), label)
            check("and it never claims delivery",
                  payload["result"] == "ACCEPTED_BY_SMTP_FOR_RELAY", label)
        check("the Message-ID that was transmitted is the one recorded",
              run.smtp is not None
              and any(isinstance(a, str) and a.startswith("<") for a in args), label)
    PASSED.append("acceptance_is_recorded_with_its_evidence")


def test_the_transmitted_message_is_the_one_filed_in_sent() -> None:
    for label in MAILERS:
        run = drive(label)
        check("a Sent-folder copy was made", run.summary["sent_archive_count"] == 1
              and run.imap is not None, label)
        if run.imap is None or run.smtp is None:
            continue
        check("exactly one APPEND", len(run.imap.append_calls) == 1, label)
        appended = run.imap.append_calls[0][3]
        check("the appended bytes ARE the transmitted bytes",
              appended == run.smtp.transmitted[0], label)
        msg = BytesParser(policy=policy.default).parsebytes(appended)
        transmitted = BytesParser(policy=policy.default).parsebytes(
            run.smtp.transmitted[0])
        check("so From, To, Subject and Message-ID are the recipient's own",
              (msg["From"], msg["To"], msg["Subject"], msg["Message-ID"])
              == (transmitted["From"], transmitted["To"], transmitted["Subject"],
                  transmitted["Message-ID"]), label)
        check("the copy is filed in the discovered \\Sent mailbox",
              run.imap.append_calls[0][0] == "INBOX.Sent", label)
        check("and the copy outcome is recorded on the send log",
              _sent_copy_record(run, label) is not None, label)
    PASSED.append("the_transmitted_message_is_the_one_filed_in_sent")


def test_nothing_unaccepted_is_ever_filed() -> None:
    for label in MAILERS:
        rejected = drive(label, smtp_double=RejectingSMTP)
        check("a rejected message is not marked sent",
              rejected.sent_update() is None
              and rejected.summary["sent_count"] == 0, label)
        check("and no Sent copy exists for it",
              rejected.imap is None
              and rejected.summary["sent_archive_count"] == 0, label)
        check("the rejection stays retryable, not ambiguous",
              rejected.summary["smtp_ambiguous_count"] == 0
              and rejected.summary["smtp_failed_count"] == 1, label)

        ambiguous = drive(label, smtp_double=DroppingSMTP)
        check("an ambiguous outcome is not marked sent either",
              ambiguous.sent_update() is None, label)
        check("and is not filed in Sent",
              ambiguous.imap is None
              and ambiguous.summary["sent_archive_count"] == 0, label)
        check("it is frozen for an operator",
              ambiguous.summary["smtp_ambiguous_count"] == 1, label)
        check("and it is NOT resubmitted inside the run",
              ambiguous.smtp is not None
              and len(ambiguous.smtp.transmitted) == 1, label)
        check("the operator is told action is required",
              any(c.get("operator_action_required") is True
                  for c in ambiguous.client.contexts()), label)
    PASSED.append("nothing_unaccepted_is_ever_filed")


def test_a_failed_sent_copy_never_retransmits() -> None:
    """THE BOUNDARY. Once example.invalid has it, a mailbox problem is a mailbox problem."""
    for label in MAILERS:
        run = drive(label, append_raises=True)
        check("the SMTP result stands: the row is still sent",
              run.sent_update() is not None
              and run.summary["sent_count"] == 1
              and run.summary["smtp_accepted_count"] == 1, label)
        check("the outcome is NOT reclassified as ambiguous or failed",
              run.summary["smtp_ambiguous_count"] == 0
              and run.summary["failed_count"] == 0, label)
        check("the copy failure is counted",
              run.summary["sent_archive_failed_count"] == 1
              and run.summary["sent_archive_count"] == 0, label)
        record = _sent_copy_record(run, label)
        check("and durably recorded against the row", record is not None, label)
        if record is not None:
            check("as a failure naming what went wrong",
                  any(isinstance(a, str) and "IMAP APPEND refused" in a
                      for a in (record[1] or ())), f"{label}: {record[1]}")
        errors = run.client.errors()
        check("an operator-visible ERROR is logged", len(errors) == 1, label)
        if errors:
            context = dict(errors[0][1].get("context") or {})
            check("saying the SMTP result stands and no resend is authorized",
                  context.get("smtp_result_stands") is True
                  and context.get("resend_authorized") is False
                  and context.get("operator_action_required") is True, label)
        check("EXACTLY ONE transmission happened, before and after the failure",
              run.smtp is not None and len(run.smtp.transmitted) == 1, label)
    PASSED.append("a_failed_sent_copy_never_retransmits")


def test_an_unconfigured_mailbox_is_reported_not_guessed() -> None:
    for label in MAILERS:
        run = drive(label, archive_settings=None)
        check("the send still happens", run.summary["sent_count"] == 1, label)
        check("no mailbox is contacted", run.imap is None, label)
        check("and the run says so rather than claiming a copy",
              run.summary["sent_archive_enabled"] is False
              and run.summary["sent_archive_skipped_count"] == 1
              and run.summary["sent_archive_count"] == 0, label)
    PASSED.append("an_unconfigured_mailbox_is_reported_not_guessed")


def test_weekly_and_monthly_behave_identically() -> None:
    """Within each family the two periods are one program, so pin that."""
    def facts(label: str) -> dict:
        run = drive(label)
        failed = drive(label, append_raises=True)
        rejected = drive(label, smtp_double=RejectingSMTP)
        return {
            "sent": run.sent_update() is not None,
            "provider_response": [a for a in run.sent_update()[1]
                                  if isinstance(a, str) and a.startswith("250 ")],
            "copied": run.summary["sent_archive_count"],
            "appended_is_transmitted":
                run.imap.append_calls[0][3] == run.smtp.transmitted[0],
            "copy_failure_keeps_sent": failed.sent_update() is not None,
            "copy_failure_counted": failed.summary["sent_archive_failed_count"],
            "copy_failure_transmissions": len(failed.smtp.transmitted),
            "rejected_is_not_copied": rejected.imap is None,
        }

    for weekly, monthly in (("ALPHA weekly", "ALPHA monthly"),
                            ("BRAVO weekly", "BRAVO monthly")):
        check("weekly and monthly agree on every observable",
              facts(weekly) == facts(monthly), f"{weekly} vs {monthly}")
    PASSED.append("weekly_and_monthly_behave_identically")


# ==============================================================================
# PART 3 — one sender identity per mailbox, and no duplicate credentials
# ==============================================================================


class EnvPatch:
    def __init__(self, values: dict):
        self.values = values
        self.old: dict = {}

    def __enter__(self):
        import os
        for key, value in self.values.items():
            self.old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        import os
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


#: Every variable any of the four senders could read, cleared by default so a
#: check never depends on what this host happens to have configured.
_ALL_MAIL_ENV = {
    f"{prefix}_{suffix}": None
    for prefix in ("BRAVO_ECO_WEEKLY_EMAIL", "ECO_PERSON_EMAIL", "ECO_WEEKLY_EMAIL")
    for suffix in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                   "SMTP_USE_TLS", "SMTP_USE_SSL", "FROM_EMAIL", "FROM_NAME",
                   "REPLY_TO", "TIMEOUT_SECONDS", "IMAP_HOST", "IMAP_PORT",
                   "IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_USE_SSL",
                   "IMAP_TIMEOUT_SECONDS", "IMAP_SENT_MAILBOX")
}


def _mailbox_env(prefix: str, *, imap: bool = True) -> dict:
    values = {
        f"{prefix}_SMTP_HOST": "smtp.example.invalid",
        f"{prefix}_SMTP_PORT": "587",
        f"{prefix}_SMTP_USERNAME": "sender@example.invalid",
        f"{prefix}_SMTP_PASSWORD": "smtp-secret",
        f"{prefix}_SMTP_USE_TLS": "true",
        f"{prefix}_SMTP_USE_SSL": "false",
        f"{prefix}_FROM_EMAIL": "sender@example.invalid",
        f"{prefix}_FROM_NAME": "Program Ecodriving",
        f"{prefix}_TIMEOUT_SECONDS": "30",
    }
    if imap:
        values.update({
            f"{prefix}_IMAP_HOST": "imap.example.invalid",
            f"{prefix}_IMAP_PORT": "993",
            f"{prefix}_IMAP_USERNAME": "sender@example.invalid",
            f"{prefix}_IMAP_PASSWORD": "imap-secret",
            f"{prefix}_IMAP_USE_SSL": "true",
            f"{prefix}_IMAP_TIMEOUT_SECONDS": "30",
        })
    return values


def test_one_mailbox_configuration_serves_both_periods() -> None:
    """A sender identity belongs to the CLIENT, not to the reporting period.

    THE DEFECT THIS CLOSES. The person weekly mailer resolved BRAVO00016 to its
    dedicated mailbox namespace while the monthly mailer read the generic
    person namespace unconditionally — a namespace this host does not define,
    so the monthly job could only ever run on hand-exported duplicates of the
    same account's credentials. Both now resolve the same way.
    """
    _, _, _ = _mailer("BRAVO weekly")
    from jobs.ecodriving_person import (
        job_eco_driving_person_monthly_email_notifications as pm)
    from jobs.ecodriving_person import (
        job_eco_driving_person_weekly_email_notifications as pw)

    env = dict(_ALL_MAIL_ENV)
    env.update(_mailbox_env("BRAVO_ECO_WEEKLY_EMAIL"))
    env["BRAVO_ECO_WEEKLY_EMAIL_FROM_EMAIL"] = "automations@example.invalid"
    env["BRAVO_ECO_WEEKLY_EMAIL_FROM_NAME"] = "Ecodriving Telematics"
    with EnvPatch(env):
        weekly = pw.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        monthly = pm.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        check("weekly and monthly resolve ONE BRAVO00016 mailbox namespace",
              weekly.env_prefix == monthly.env_prefix == "BRAVO_ECO_WEEKLY_EMAIL",
              f"{weekly.env_prefix} vs {monthly.env_prefix}")
        check("...one account, one From identity, one set of credentials",
              (weekly.host, weekly.username, weekly.from_email, weekly.from_name)
              == (monthly.host, monthly.username, monthly.from_email, monthly.from_name),
              str(monthly))
        check("and one Sent mailbox, required for this sender",
              pw._sent_archive_settings(weekly) == pm._sent_archive_settings(monthly)
              and pw._sent_archive_settings(weekly) is not None)

        # NO DUPLICATE CREDENTIALS. The generic person namespace is entirely
        # unset above, and neither period needs it.
        import os
        check("no ECO_PERSON_EMAIL_* variable is required for BRAVO00016",
              not any(os.environ.get(name) for name in _ALL_MAIL_ENV
                      if name.startswith("ECO_PERSON_EMAIL_")))

    # A different person client still uses the generic namespace, unchanged.
    env = dict(_ALL_MAIL_ENV)
    env.update(_mailbox_env("ECO_PERSON_EMAIL", imap=False))
    with EnvPatch(env):
        for label, mod in (("weekly", pw), ("monthly", pm)):
            settings = mod.load_smtp_settings_from_env(dry_run=False, client_code="OTHER00001")
            check("another person client keeps the generic sender namespace",
                  settings.env_prefix == "ECO_PERSON_EMAIL", label)
            check("whose Sent copy is optional and simply reported when unset",
                  mod._sent_archive_settings(settings) is None, label)
    PASSED.append("one_mailbox_configuration_serves_both_periods")


def test_the_two_alpha_mailers_share_one_mailbox_too() -> None:
    from jobs.ecodriving import job_eco_driving_monthly_email_notifications as dm
    from jobs.ecodriving import job_eco_driving_weekly_email_notifications as dw

    check("both ALPHA mailers name the same sender namespace",
          dw.EMAIL_ENV_PREFIX == dm.EMAIL_ENV_PREFIX == "ECO_WEEKLY_EMAIL",
          f"{dw.EMAIL_ENV_PREFIX} vs {dm.EMAIL_ENV_PREFIX}")

    env = dict(_ALL_MAIL_ENV)
    with EnvPatch(env):
        check("with no IMAP configuration, neither files a copy and both say so",
              dw._sent_archive_settings() is None and dm._sent_archive_settings() is None)
    env.update(_mailbox_env("ECO_WEEKLY_EMAIL"))
    with EnvPatch(env):
        weekly, monthly = dw._sent_archive_settings(), dm._sent_archive_settings()
        check("configuring that one namespace enables the copy for BOTH",
              weekly is not None and weekly == monthly, f"{weekly} vs {monthly}")
        check("and it is the mailbox the message was sent from",
              weekly.host == "imap.example.invalid"
              and weekly.username == "sender@example.invalid")
    PASSED.append("the_two_alpha_mailers_share_one_mailbox_too")


def test_a_half_configured_mailbox_is_an_error_not_a_guess() -> None:
    from jobs.ecodriving import job_eco_driving_weekly_email_notifications as dw

    env = dict(_ALL_MAIL_ENV)
    env["ECO_WEEKLY_EMAIL_IMAP_HOST"] = "imap.example.invalid"
    env["ECO_WEEKLY_EMAIL_IMAP_PORT"] = "993"
    with EnvPatch(env):
        try:
            dw._sent_archive_settings()
        except RuntimeError as error:
            check("a partial IMAP configuration fails closed, naming what is missing",
                  str(error).startswith("MANUAL_STEP_REQUIRED_MISSING_SENT_FOLDER_CONFIG")
                  and "IMAP_USERNAME" in str(error) and "IMAP_PASSWORD" in str(error),
                  str(error))
        else:  # pragma: no cover
            FAILED.append("a half-configured mailbox was accepted")
    PASSED.append("a_half_configured_mailbox_is_an_error_not_a_guess")


def test_one_mailbox_is_configured_once_not_twice() -> None:
    """The Sent copy logs in as the account that sent the mail.

    THE DEFECT THIS CLOSES. Archiving demanded `{PREFIX}_IMAP_USERNAME` and
    `{PREFIX}_IMAP_PASSWORD` even when `{PREFIX}_SMTP_USERNAME` and
    `{PREFIX}_SMTP_PASSWORD` already named that same mailbox — a second copy of
    one password, kept in step by hand. The IMAP variables are now an override,
    and the SMTP credentials of the SAME prefix are the default.
    """
    from jobs.ecodriving import job_eco_driving_monthly_email_notifications as dm
    from jobs.ecodriving import job_eco_driving_weekly_email_notifications as dw

    # (1) and (5): the mailbox's SMTP login, plus the IMAP settings the
    # protocol genuinely needs, and NO duplicated credentials.
    env = dict(_ALL_MAIL_ENV)
    env.update(_mailbox_env("ECO_WEEKLY_EMAIL", imap=False))
    env.update({
        "ECO_WEEKLY_EMAIL_IMAP_HOST": "imap.example.invalid",
        "ECO_WEEKLY_EMAIL_IMAP_PORT": "993",
        "ECO_WEEKLY_EMAIL_IMAP_USE_SSL": "true",
    })
    with EnvPatch(env):
        # (6): both ALPHA mailers inherit it, weekly and monthly alike.
        weekly, monthly = dw._sent_archive_settings(), dm._sent_archive_settings()
        check("no IMAP_USERNAME/PASSWORD: the configuration is still complete",
              weekly is not None and weekly == monthly, f"{weekly} vs {monthly}")
        # (2): the resolved credentials ARE the SMTP ones.
        check("...and the Sent copy logs in with the SMTP credentials",
              (weekly.username, weekly.password)
              == ("sender@example.invalid", "smtp-secret"),
              weekly.username)
        check("...at the IMAP endpoint, which is its own and was stated",
              (weekly.host, weekly.port, weekly.use_ssl)
              == ("imap.example.invalid", 993, True), str(weekly))

    # (3): an explicit override still wins, for a provider that needs one.
    env["ECO_WEEKLY_EMAIL_IMAP_USERNAME"] = "archive@example.invalid"
    env["ECO_WEEKLY_EMAIL_IMAP_PASSWORD"] = "imap-only-secret"
    with EnvPatch(env):
        overridden = dw._sent_archive_settings()
        check("an explicit IMAP credential overrides the SMTP one",
              (overridden.username, overridden.password)
              == ("archive@example.invalid", "imap-only-secret"),
              overridden.username)

    # (4): neither source has them -> fail closed, naming BOTH ways to fix it.
    env.pop("ECO_WEEKLY_EMAIL_IMAP_USERNAME")
    env.pop("ECO_WEEKLY_EMAIL_IMAP_PASSWORD")
    env["ECO_WEEKLY_EMAIL_SMTP_USERNAME"] = ""
    env["ECO_WEEKLY_EMAIL_SMTP_PASSWORD"] = ""
    with EnvPatch(env):
        try:
            dw._sent_archive_settings()
        except RuntimeError as error:
            check("with credentials from NEITHER source it fails closed",
                  str(error).startswith(
                      "MANUAL_STEP_REQUIRED_MISSING_SENT_FOLDER_CONFIG"),
                  str(error))
            check("...naming the IMAP override and the SMTP default alike",
                  "ECO_WEEKLY_EMAIL_IMAP_USERNAME" in str(error)
                  and "ECO_WEEKLY_EMAIL_SMTP_USERNAME" in str(error)
                  and "ECO_WEEKLY_EMAIL_IMAP_PASSWORD" in str(error)
                  and "ECO_WEEKLY_EMAIL_SMTP_PASSWORD" in str(error),
                  str(error))
        else:  # pragma: no cover
            FAILED.append("a mailbox with no credentials at all was accepted")

    # (5): SMTP credentials alone are NOT a Sent-folder configuration. An SMTP
    # host is not an IMAP host and is never promoted into one.
    env = dict(_ALL_MAIL_ENV)
    env.update(_mailbox_env("ECO_WEEKLY_EMAIL", imap=False))
    with EnvPatch(env):
        check("SMTP credentials alone do not invent an IMAP endpoint",
              dw._sent_archive_settings() is None)
    env["ECO_WEEKLY_EMAIL_IMAP_HOST"] = "imap.example.invalid"
    with EnvPatch(env):
        try:
            dw._sent_archive_settings()
        except RuntimeError as error:
            check("and a host without a port or SSL mode is still an error",
                  "ECO_WEEKLY_EMAIL_IMAP_PORT" in str(error)
                  and "ECO_WEEKLY_EMAIL_IMAP_USE_SSL" in str(error),
                  str(error))
        else:  # pragma: no cover
            FAILED.append("an IMAP host with no port or SSL mode was accepted")
    PASSED.append("one_mailbox_is_configured_once_not_twice")


def test_bravos_explicit_imap_credentials_are_left_alone() -> None:
    """(7) BRAVO00016 already states its IMAP login; the default must not win."""
    from jobs.ecodriving_person import (
        job_eco_driving_person_monthly_email_notifications as pm)
    from jobs.ecodriving_person import (
        job_eco_driving_person_weekly_email_notifications as pw)

    env = dict(_ALL_MAIL_ENV)
    env.update(_mailbox_env("BRAVO_ECO_WEEKLY_EMAIL"))
    env["BRAVO_ECO_WEEKLY_EMAIL_FROM_EMAIL"] = "automations@example.invalid"
    env["BRAVO_ECO_WEEKLY_EMAIL_FROM_NAME"] = "Ecodriving Telematics"
    env["BRAVO_ECO_WEEKLY_EMAIL_IMAP_USERNAME"] = "automations@example.invalid"
    env["BRAVO_ECO_WEEKLY_EMAIL_IMAP_PASSWORD"] = "bravo-imap-secret"
    with EnvPatch(env):
        weekly = pw.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        monthly = pm.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        w_archive = pw._sent_archive_settings(weekly)
        m_archive = pm._sent_archive_settings(monthly)
        check("BRAVO keeps the approved shared sender",
              (weekly.from_name, weekly.from_email)
              == ("Ecodriving Telematics", "automations@example.invalid"),
              f"{weekly.from_name} <{weekly.from_email}>")
        check("both BRAVO periods still resolve one required Sent mailbox",
              w_archive is not None and w_archive == m_archive)
        check("...with its OWN explicit IMAP credentials, not the SMTP ones",
              (w_archive.username, w_archive.password)
              == ("automations@example.invalid", "bravo-imap-secret"),
              w_archive.username)
    PASSED.append("bravos_explicit_imap_credentials_are_left_alone")


def test_an_unobserved_reply_is_never_dressed_up_as_one() -> None:
    """`sent` without a captured reply must not read like a quotation."""
    _reset_doubles()
    prepared = _prepared()
    unobserved = transport.submit_prepared_email(
        settings=SMTP_SETTINGS, prepared=prepared, smtp_factory=SilentSMTP)
    observed = transport.submit_prepared_email(
        settings=SMTP_SETTINGS, prepared=prepared, smtp_factory=RelaySMTP)

    for acceptance, expected in ((observed, True), (unobserved, False)):
        payload = acceptance.as_metadata()[transport.KEY_ACCEPTANCE]
        check("the record states outright whether the relay was quoted",
              payload["final_reply_observed"] is expected, str(payload))
    check("an unobserved row carries no reply code and no reply text",
          unobserved.as_metadata()[transport.KEY_ACCEPTANCE]["smtp_code"] is None
          and unobserved.as_metadata()[transport.KEY_ACCEPTANCE]["smtp_response"] is None
          and unobserved.as_metadata()[transport.KEY_ACCEPTANCE]["queue_id"] is None)
    check("and its provider_response is the library literal, not server text",
          unobserved.provider_response == transport.LEGACY_ACCEPTANCE_RESPONSE
          and not unobserved.provider_response[:3].isdigit(),
          unobserved.provider_response)
    check("while an observed one is recognisable by its reply code",
          observed.provider_response.split(" ", 1)[0] == "250")
    PASSED.append("an_unobserved_reply_is_never_dressed_up_as_one")


def main() -> int:
    tests = (
        test_the_relays_own_answer_is_kept,
        test_no_evidence_is_never_invented,
        test_a_refusal_is_still_a_refusal,
        test_acceptance_is_recorded_with_its_evidence,
        test_the_transmitted_message_is_the_one_filed_in_sent,
        test_nothing_unaccepted_is_ever_filed,
        test_a_failed_sent_copy_never_retransmits,
        test_an_unconfigured_mailbox_is_reported_not_guessed,
        test_weekly_and_monthly_behave_identically,
        test_one_mailbox_configuration_serves_both_periods,
        test_the_two_alpha_mailers_share_one_mailbox_too,
        test_a_half_configured_mailbox_is_an_error_not_a_guess,
        test_one_mailbox_is_configured_once_not_twice,
        test_bravos_explicit_imap_credentials_are_left_alone,
        test_an_unobserved_reply_is_never_dressed_up_as_one,
    )
    for test in tests:
        test()
    for name in PASSED:
        print(f"PASS  {name}")
    if FAILED:
        print(f"\nFAILED {len(FAILED)} check(s):")
        for failure in FAILED:
            print(f"  - {failure}")
        return 1
    print(f"\n{len(PASSED)} checks passed — Eco e-mail SMTP receipt and Sent copy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
