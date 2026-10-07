"""Eco e-mail — one SMTP session for a whole run, without losing any evidence.

WHAT THIS PROVES

example.invalid delays every new connection's greeting by 12–25 s, so one session per
message costs hours. `ReusableSmtpSession` keeps one session across messages.
These checks establish that doing so changes NOTHING about what a send-log
row may claim:

* every message still gets its own final reply (queue id per message);
* a session the relay has ended is detected by NOOP and replaced silently;
* a connection lost in the ENVELOPE of a reused session — proven by `DATA`
  never having been entered — is retried exactly once, on a fresh session;
* the same loss on a FRESH session is raised as DEFINITE/ENVELOPE, no retry;
* a loss after `DATA` was entered stays AMBIGUOUS and is never retried;
* after any failure the session is discarded;
* `max_messages_per_session` and the env switch bound or disable reuse;
* a client that exposes no `data()` yields no envelope evidence, so the
  conservative AMBIGUOUS classification is unchanged for it.

Nothing here touches a socket. Run with the project interpreter:

    .venv/bin/python ops/tests_manual/test_eco_email_smtp_session_reuse.py
"""

from __future__ import annotations

import os
import smtplib
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.common import eco_email_transport as transport  # noqa: E402
from jobs.common import eco_smtp_submission as sub  # noqa: E402

PASSED: list[str] = []


@dataclass(frozen=True)
class _Settings:
    host: str = "smtp.invalid"
    port: int = 587
    username: str | None = "sender@example.invalid"
    password: str | None = "secret"
    use_tls: bool = True
    use_ssl: bool = False
    from_email: str = "sender@example.invalid"
    from_name: str = "Program Ecodriving"
    reply_to: str | None = None
    timeout_seconds: int = 5


class ScriptedRelay:
    """A relay double. `script` is consumed one entry per `sendmail`:

    * ("ok", queue_id)         -> 250 with that queue id
    * ("drop_envelope", None)  -> connection lost BEFORE data()
    * ("drop_data", None)      -> connection lost INSIDE data()
    * ("reject", code)         -> real negative reply to the dot
    * ("refuse_sender", None)  -> 5xx to MAIL FROM (before data())
    `noop_dead_after` makes NOOP fail once `n` messages were sent on this
    connection, which is what an idle-timed-out session looks like.
    """

    connections: list["ScriptedRelay"] = []
    script: list[tuple[str, object]] = []
    noop_dead_after: int | None = None

    def __init__(self, host=None, port=None, timeout=None):
        self.calls: list[str] = []
        self.sent = 0
        self.quit_called = False
        type(self).connections.append(self)

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append("login")

    def noop(self):
        self.calls.append("noop")
        if self.noop_dead_after is not None and self.sent >= self.noop_dead_after:
            raise smtplib.SMTPServerDisconnected("idle timeout")
        return (250, b"2.0.0 OK")

    def data(self, message):
        self.calls.append("data")
        kind, arg = self._current
        if kind == "drop_data":
            raise smtplib.SMTPServerDisconnected("dropped inside DATA")
        if kind == "reject":
            return (int(arg), b"5.7.1 rejected")
        return (250, f"2.0.0 Ok: queued as {arg}".encode())

    def sendmail(self, sender, recipients, message):
        self.calls.append("sendmail")
        self._current = type(self).script.pop(0)
        kind, arg = self._current
        if kind == "drop_envelope":
            raise smtplib.SMTPServerDisconnected("dropped at MAIL FROM")
        if kind == "refuse_sender":
            raise smtplib.SMTPSenderRefused(550, b"5.1.0 sender rejected", sender)
        code, resp = self.data(message)
        if code != 250:
            raise smtplib.SMTPDataError(code, resp)
        self.sent += 1
        return {}

    def quit(self):
        self.calls.append("quit")
        self.quit_called = True


class NoDataRelay(ScriptedRelay):
    """Exposes no `data()`: no envelope evidence can exist for it."""
    connections: list = []
    data = None  # type: ignore[assignment]

    def sendmail(self, sender, recipients, message):
        self.calls.append("sendmail")
        raise smtplib.SMTPServerDisconnected("dropped somewhere")


def _reset(script, noop_dead_after=None):
    ScriptedRelay.connections = []
    ScriptedRelay.script = list(script)
    ScriptedRelay.noop_dead_after = noop_dead_after


def _session(**kw) -> sub.ReusableSmtpSession:
    settings = transport.session_settings_for(_Settings())
    return sub.ReusableSmtpSession(settings, smtp_factory=ScriptedRelay, **kw)


def _prepared(n: int) -> transport.PreparedEmail:
    return transport.prepare_email(
        settings=_Settings(), recipient_email=f"driver{n}@example.invalid",
        subject=f"msg {n}", html_body="<p>x</p>", text_body="x")


def _submit(session, n):
    return transport.submit_prepared_email(
        settings=_Settings(), prepared=_prepared(n), session=session)


def check_one_session_many_messages():
    _reset([("ok", "QID001"), ("ok", "QID002"), ("ok", "QID003")])
    with _session() as session:
        a1, a2, a3 = (_submit(session, i) for i in (1, 2, 3))
    assert len(ScriptedRelay.connections) == 1, "three messages must share one connection"
    relay = ScriptedRelay.connections[0]
    assert relay.calls.count("starttls") == 1 and relay.calls.count("login") == 1
    assert relay.calls.count("noop") == 2, "reuse is preceded by NOOP; the first message is not"
    assert relay.calls.count("sendmail") == 3 and relay.quit_called
    assert (a1.queue_id, a2.queue_id, a3.queue_id) == ("QID001", "QID002", "QID003"), \
        "each message keeps ITS OWN final reply"
    assert a1.message_id != a2.message_id != a3.message_id
    assert all(a.evidence_available and a.code == 250 for a in (a1, a2, a3))
    assert relay.calls.count("data") == 3, "one data() per message: the observer never nests"
    assert "data" not in vars(relay), "the observer is removed after each message"
    assert session.summary() == {
        "smtp_sessions_opened": 1, "smtp_session_messages": 3,
        "smtp_session_liveness_failures": 0, "smtp_session_envelope_retries": 0,
        "smtp_session_dropped_after_failure": 0}
    PASSED.append("one_session_carries_many_messages_with_per_message_evidence")


def check_dead_session_is_replaced_by_noop():
    _reset([("ok", "QID001"), ("ok", "QID002")], noop_dead_after=1)
    with _session() as session:
        _submit(session, 1)
        a2 = _submit(session, 2)
    assert len(ScriptedRelay.connections) == 2, "a dead session is replaced, not reused"
    first, second = ScriptedRelay.connections
    assert first.calls[-2:] == ["noop", "quit"], first.calls
    assert second.calls.count("sendmail") == 1 and "noop" not in second.calls
    assert a2.queue_id == "QID002"
    assert session.summary()["smtp_session_liveness_failures"] == 1
    assert session.summary()["smtp_sessions_opened"] == 2
    PASSED.append("a_session_the_relay_ended_is_detected_by_noop_and_replaced")


def check_envelope_loss_on_reused_session_is_retried_once():
    _reset([("ok", "QID001"), ("drop_envelope", None), ("ok", "QID002")])
    with _session() as session:
        _submit(session, 1)
        a2 = _submit(session, 2)
    assert len(ScriptedRelay.connections) == 2
    first, second = ScriptedRelay.connections
    assert "data" not in first.calls[first.calls.index("noop"):], \
        "the retried failure must have happened before DATA"
    assert second.calls.count("sendmail") == 1
    assert a2.queue_id == "QID002"
    s = session.summary()
    assert s["smtp_session_envelope_retries"] == 1 and s["smtp_sessions_opened"] == 2
    assert s["smtp_session_messages"] == 2
    PASSED.append("envelope_connection_loss_on_a_reused_session_is_retried_once_on_a_fresh_one")


def check_envelope_loss_on_fresh_session_is_definite_not_retried():
    _reset([("drop_envelope", None), ("ok", "never")])
    session = _session()
    try:
        _submit(session, 1)
    except sub.SmtpSubmissionError as error:
        assert error.classification == sub.DEFINITE_NOT_SUBMITTED
        assert error.phase == sub.PHASE_ENVELOPE
    else:
        raise AssertionError("must raise")
    assert len(ScriptedRelay.connections) == 1, "no retry on a fresh session"
    assert ScriptedRelay.script == [("ok", "never")]
    assert not session.connected, "the failed session is discarded"
    PASSED.append("envelope_loss_on_a_fresh_session_is_definite_and_not_retried")


def check_sender_refusal_on_reused_session_is_not_retried():
    _reset([("ok", "QID001"), ("refuse_sender", None), ("ok", "never")])
    with _session() as session:
        _submit(session, 1)
        try:
            _submit(session, 2)
        except sub.SmtpSubmissionError as error:
            assert error.classification == sub.DEFINITE_NOT_SUBMITTED
            assert error.phase == sub.PHASE_ENVELOPE
            assert isinstance(error.cause, smtplib.SMTPSenderRefused)
        else:
            raise AssertionError("must raise")
    assert len(ScriptedRelay.connections) == 1, "a refusal is not a lost connection; no retry"
    assert ScriptedRelay.script == [("ok", "never")]
    PASSED.append("a_relay_refusal_in_the_envelope_is_definite_but_never_retried")


def check_loss_inside_data_stays_ambiguous_and_is_never_retried():
    _reset([("ok", "QID001"), ("drop_data", None), ("ok", "QID003")])
    with _session() as session:
        _submit(session, 1)
        try:
            _submit(session, 2)
        except sub.SmtpSubmissionError as error:
            assert error.classification == sub.AMBIGUOUS_SUBMISSION, error
            assert error.phase == sub.PHASE_TRANSMIT
        else:
            raise AssertionError("must raise")
        assert ScriptedRelay.script == [("ok", "QID003")], "nothing after DATA is ever retried"
        assert not session.connected, "state unknown after the failure: discarded"
        a3 = _submit(session, 3)
    assert len(ScriptedRelay.connections) == 2 and a3.queue_id == "QID003"
    assert session.summary()["smtp_session_dropped_after_failure"] == 1
    PASSED.append("a_loss_after_DATA_stays_ambiguous_is_not_retried_and_drops_the_session")


def check_negative_reply_to_the_dot_stays_definite_and_drops_session():
    _reset([("ok", "QID001"), ("reject", 550), ("ok", "QID003")])
    with _session() as session:
        _submit(session, 1)
        try:
            _submit(session, 2)
        except sub.SmtpSubmissionError as error:
            assert error.classification == sub.DEFINITE_NOT_SUBMITTED
            assert error.phase == sub.PHASE_TRANSMIT
        else:
            raise AssertionError("must raise")
        assert ScriptedRelay.script == [("ok", "QID003")]
        _submit(session, 3)
    assert len(ScriptedRelay.connections) == 2
    PASSED.append("a_real_5xx_to_the_dot_is_still_definite_and_still_not_retried")


def check_max_messages_per_session_bounds_a_session():
    _reset([("ok", f"QID00{i}") for i in range(1, 6)])
    with _session(max_messages_per_session=2) as session:
        for i in range(1, 6):
            _submit(session, i)
    assert [c.sent for c in ScriptedRelay.connections] == [2, 2, 1]
    assert all(c.quit_called for c in ScriptedRelay.connections)
    PASSED.append("max_messages_per_session_rolls_the_session_over")


def check_thousand_messages_do_not_nest_the_observer():
    _reset([("ok", f"QID{i:04d}") for i in range(1200)])
    with _session() as session:
        for i in range(1200):
            assert _submit(session, i).queue_id == f"QID{i:04d}"
    assert len(ScriptedRelay.connections) == 1
    PASSED.append("twelve_hundred_messages_on_one_session_keep_a_flat_observer")


def check_reuse_switched_off_is_one_session_per_message():
    _reset([("ok", "QID001"), ("ok", "QID002")])
    with _session(reuse=False) as session:
        _submit(session, 1)
        _submit(session, 2)
    assert [c.sent for c in ScriptedRelay.connections] == [1, 1]
    assert all("noop" not in c.calls for c in ScriptedRelay.connections)
    PASSED.append("reuse_off_restores_one_session_per_message")


def check_env_switches():
    old = {k: os.environ.get(k) for k in (sub.ENV_SESSION_REUSE, sub.ENV_SESSION_MAX_MESSAGES)}
    try:
        os.environ.pop(sub.ENV_SESSION_REUSE, None)
        os.environ.pop(sub.ENV_SESSION_MAX_MESSAGES, None)
        s = transport.open_run_session(_Settings())
        assert s.reuse is True and s.max_messages_per_session is None
        os.environ[sub.ENV_SESSION_REUSE] = "false"
        os.environ[sub.ENV_SESSION_MAX_MESSAGES] = "50"
        s = transport.open_run_session(_Settings())
        assert s.reuse is False and s.max_messages_per_session == 50
        os.environ[sub.ENV_SESSION_MAX_MESSAGES] = "0"
        assert transport.open_run_session(_Settings()).max_messages_per_session is None
        assert not s.connected, "opening a run session opens no connection"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    PASSED.append("env_switches_control_reuse_and_the_per_session_cap")


def check_no_data_step_means_no_envelope_evidence():
    NoDataRelay.connections = []
    settings = transport.session_settings_for(_Settings())
    session = sub.ReusableSmtpSession(settings, smtp_factory=NoDataRelay)
    try:
        _submit(session, 1)
    except sub.SmtpSubmissionError as error:
        assert error.classification == sub.AMBIGUOUS_SUBMISSION, \
            "without an observable data() step, a drop is NOT proven pre-DATA"
        assert error.phase == sub.PHASE_TRANSMIT
    else:
        raise AssertionError("must raise")
    # And the one-shot path is unchanged in the same way.
    NoDataRelay.connections = []
    try:
        transport.submit_prepared_email(settings=_Settings(), prepared=_prepared(2),
                                        smtp_factory=NoDataRelay)
    except sub.SmtpSubmissionError as error:
        assert error.classification == sub.AMBIGUOUS_SUBMISSION
    else:
        raise AssertionError("must raise")
    PASSED.append("a_client_without_data_yields_no_envelope_evidence_and_stays_ambiguous")


def check_explicit_factory_takes_precedence_over_session():
    class OneShot(ScriptedRelay):
        connections: list = []
    OneShot.connections = []
    _reset([("ok", "QID001")])
    with _session() as session:
        a = transport.submit_prepared_email(
            settings=_Settings(), prepared=_prepared(1), session=session,
            smtp_factory=OneShot)
    assert a.queue_id == "QID001", "the evidence comes from the explicit transport"
    assert len(OneShot.connections) == 1 and OneShot.connections[0].quit_called
    assert not session.connected and session.summary()["smtp_sessions_opened"] == 0, \
        "an explicit transport for this message must not open the run session"
    PASSED.append("an_explicit_smtp_factory_names_the_transport_and_bypasses_the_session")


def check_pre_submission_phases_on_open():
    class LoginFails(ScriptedRelay):
        connections: list = []
        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
    settings = transport.session_settings_for(_Settings())
    session = sub.ReusableSmtpSession(settings, smtp_factory=LoginFails)
    try:
        session.run(lambda smtp: (_ for _ in ()).throw(AssertionError("must not transmit")))
    except sub.SmtpSubmissionError as error:
        assert error.classification == sub.DEFINITE_NOT_SUBMITTED and error.phase == sub.PHASE_LOGIN
    else:
        raise AssertionError("must raise")
    assert not session.connected and LoginFails.connections[0].quit_called
    PASSED.append("connect_starttls_login_failures_stay_definite_pre_submission_phases")


def main() -> int:
    for check in (
        check_one_session_many_messages,
        check_dead_session_is_replaced_by_noop,
        check_envelope_loss_on_reused_session_is_retried_once,
        check_envelope_loss_on_fresh_session_is_definite_not_retried,
        check_sender_refusal_on_reused_session_is_not_retried,
        check_loss_inside_data_stays_ambiguous_and_is_never_retried,
        check_negative_reply_to_the_dot_stays_definite_and_drops_session,
        check_max_messages_per_session_bounds_a_session,
        check_thousand_messages_do_not_nest_the_observer,
        check_reuse_switched_off_is_one_session_per_message,
        check_env_switches,
        check_no_data_step_means_no_envelope_evidence,
        check_explicit_factory_takes_precedence_over_session,
        check_pre_submission_phases_on_open,
    ):
        check()
        print(f"PASS  {PASSED[-1]}")
    print("\n" + "=" * 78)
    print(f"{len(PASSED)} checks passed — Eco e-mail SMTP session reuse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
