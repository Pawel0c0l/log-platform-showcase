"""Eco Driving e-mail — what an SMTP failure is actually evidence of.

WHY THIS MODULE EXISTS

All four Eco Driving mailers used to treat every exception raised while sending
as one thing: `failed`. A `failed` row is retryable, so the following sequence
was reachable on the ordinary 06:00/20:00 cadence, with no operator involved:

    reserve -> example.invalid ACCEPTS the message -> the connection dies before the
    final response is read -> Python raises -> the row is marked `failed` ->
    the next normal run sees a retryable row -> the SAME message is submitted
    to the SAME driver again.

SMTP has no idempotency key. A second submission is a second message, so the
only way to be safe is to stop claiming to know something the protocol did not
tell us. THAT is what this module computes: not "did it fail", but "can this
host PROVE the remote server did not accept the message?".

THE TWO ANSWERS, AND THE EVIDENCE EACH ONE REQUIRES

`DEFINITE_NOT_SUBMITTED`
    The message data was demonstrably never accepted. Either the failure
    happened before any message content could be transmitted — the connection
    was never established, STARTTLS failed, authentication failed, EHLO was
    refused, the envelope sender or every recipient was rejected — or the
    server issued a DEFINITIVE NEGATIVE REPLY — a real 4xx or 5xx code — to
    `DATA` or to the terminating dot, which RFC 5321 defines as a refusal of
    the whole message. A reply smtplib could not parse into such a code is not
    that evidence, whatever exception type carried it. Retrying is safe, and
    these stay `failed` exactly as they are today.

`AMBIGUOUS_SUBMISSION`
    Everything else. The transmission had begun (or may have begun) and the
    caller cannot exclude remote acceptance: the connection dropped mid-DATA,
    the read of the final response timed out, the socket died, an unrecognised
    exception surfaced. There is no protocol evidence here, so this host must
    not guess — and MUST NOT resubmit automatically.

The default is `AMBIGUOUS_SUBMISSION`. An exception type this module has never
seen is not evidence of a rejection, and treating unknown as safe is the exact
mistake being corrected.

WHAT THIS MODULE DOES NOT DO

It does not change the transport. example.invalid SMTP stays authoritative, no provider
is introduced, nothing here makes SMTP idempotent, and no message is retried,
queued or rewritten. It classifies, and the send-log lifecycle
(`jobs.common.eco_email_reconciliation`) is what makes the classification
durable.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Optional

#: The message was demonstrably not accepted. Safe to retry automatically.
DEFINITE_NOT_SUBMITTED = "DEFINITE_NOT_SUBMITTED"
#: Remote acceptance cannot be excluded. NEVER retried automatically.
AMBIGUOUS_SUBMISSION = "AMBIGUOUS_SUBMISSION"

#: Where in one SMTP session the failure happened. Operator-facing only; the
#: classification above is what any code branches on.
PHASE_CONNECT = "CONNECT"
PHASE_STARTTLS = "STARTTLS"
PHASE_LOGIN = "LOGIN"
PHASE_TRANSMIT = "TRANSMIT"
#: The envelope (`MAIL FROM` / `RCPT TO`) was being negotiated on an already
#: open session and the failure happened BEFORE `DATA` was issued. Nothing of
#: the message body was ever written, so this is definite non-acceptance —
#: `jobs.common.eco_email_transport` proves it by observing that the client's
#: `data()` step was never entered.
PHASE_ENVELOPE = "ENVELOPE"

#: Exception types that carry PROTOCOL EVIDENCE of non-acceptance when they are
#: raised out of the transmit step.
#:
#: * `SMTPHeloError`          the greeting failed; no envelope, no data.
#: * `SMTPNotSupportedError`  raised while building the envelope (SMTPUTF8),
#:                            before any content is written.
#: * `SMTPSenderRefused`      `MAIL FROM` rejected; smtplib resets and raises.
#: * `SMTPRecipientsRefused`  EVERY recipient rejected; `DATA` is never issued.
#:
#: `SMTPDataError` is NOT in this tuple. It is the one transmit error whose
#: evidence lives in its REPLY CODE rather than in its type — see
#: `_proves_protocol_rejection` below.
_DEFINITE_TRANSMIT_ERRORS = (
    smtplib.SMTPHeloError,
    smtplib.SMTPNotSupportedError,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPRecipientsRefused,
)

#: The SMTP reply-code range that PROVES the remote server refused. RFC 5321
#: gives 4xx (transient negative) and 5xx (permanent negative) exactly that
#: meaning; nothing else does.
_NEGATIVE_REPLY_MIN = 400
_NEGATIVE_REPLY_MAX = 600


def _proves_protocol_rejection(error: BaseException) -> bool:
    """Does this response exception carry a real 4xx/5xx negative reply?

    WHY THE TYPE IS NOT ENOUGH, FOR `SMTPDataError` SPECIFICALLY.

    CPython's `smtplib` raises `SMTPDataError` from two structurally different
    places in one session:

    * from `data()` when the `DATA` command itself is not answered with 354 —
      the body has NOT been written, so nothing could have been accepted; and
    * from `sendmail()` when the reply to the TERMINATING DOT is not 250 — by
      which point the entire message HAS been transmitted.

    In the second case the exception is safe to call a rejection only because
    the server said so. A genuine 4xx/5xx final reply is that statement. But
    `SMTP.getreply()` returns `smtp_code = -1` when the response line is
    malformed or unparsable, and `SMTPDataError(-1, b"garbled")` says nothing
    at all about what the remote did with a message it has already received.
    Treating that as a proven rejection would mark the row retryable and put a
    second copy of the message in the driver's inbox on the next ordinary run.

    So: a code inside the negative-reply range is evidence. Anything else —
    `-1`, `0`, an unexpected 2xx/3xx, `>= 600`, `None`, or a code the runtime
    could not represent as an integer at all — is not evidence, and the caller
    falls back to AMBIGUOUS_SUBMISSION.
    """
    code = getattr(error, "smtp_code", None)
    if isinstance(code, bool):
        # `True` is an `int` in Python and would otherwise be read as code 1.
        return False
    if isinstance(code, int):
        numeric = code
    else:
        if isinstance(code, (bytes, bytearray)):
            try:
                code = code.decode("ascii", "strict")
            except (UnicodeDecodeError, AttributeError):
                return False
        if not isinstance(code, str) or not code.strip().isdigit():
            return False
        try:
            numeric = int(code.strip())
        except ValueError:  # pragma: no cover - guarded by isdigit above
            return False
    return _NEGATIVE_REPLY_MIN <= numeric < _NEGATIVE_REPLY_MAX


class SmtpSubmissionError(RuntimeError):
    """One SMTP attempt that did not complete, plus what it is evidence of.

    `classification` is the load-bearing field. `phase` and `cause` exist so an
    operator reading a blocked row can see what happened without the caller
    having to reconstruct it.
    """

    def __init__(self, classification: str, phase: str, cause: BaseException) -> None:
        self.classification = classification
        self.phase = phase
        self.cause = cause
        super().__init__(f"{classification}/{phase}: {type(cause).__name__}: {cause}")

    @property
    def ambiguous(self) -> bool:
        return self.classification == AMBIGUOUS_SUBMISSION

    @property
    def operator_detail(self) -> str:
        return f"{self.phase}: {type(self.cause).__name__}: {self.cause}"


def classify_transmit_exception(error: BaseException) -> str:
    """What an exception out of the transmit step proves, and only that.

    Kept separate from the session runner so the rule itself is testable
    without a transport, and so a caller that already has its own session
    handling can reuse the classification rather than reimplement it.
    """
    if isinstance(error, smtplib.SMTPDataError):
        # The body may already be on the wire by the time this is raised, so
        # the reply code — not the type — is what has to prove the refusal.
        return (DEFINITE_NOT_SUBMITTED if _proves_protocol_rejection(error)
                else AMBIGUOUS_SUBMISSION)
    if isinstance(error, _DEFINITE_TRANSMIT_ERRORS):
        return DEFINITE_NOT_SUBMITTED
    return AMBIGUOUS_SUBMISSION


def classify_exception(error: BaseException) -> str:
    """The classification of any exception, whatever it came from.

    An `SmtpSubmissionError` already carries the answer. Anything else that
    reaches a caller's send-attempt handler — a database error while recording
    the outcome, a MIME construction failure after submission, a bug — is
    treated as AMBIGUOUS unless it is itself protocol evidence, because by then
    the message may already be gone.
    """
    if isinstance(error, SmtpSubmissionError):
        return error.classification
    return classify_transmit_exception(error)


@dataclass(frozen=True)
class SmtpSessionSettings:
    """The transport parameters one session needs. Carries no secret policy."""

    host: str
    port: int
    timeout_seconds: int
    use_tls: bool
    use_ssl: bool
    username: Optional[str]
    password: Optional[str]


def run_smtp_session(
    *,
    settings: SmtpSessionSettings,
    transmit: Callable[[Any], Any],
    smtp_factory: Optional[Callable[..., Any]] = None,
) -> Any:
    """Connect, secure, authenticate, transmit — and say what a failure proves.

    `transmit` receives the live `SMTP` object and performs the ONE call that
    submits the message (`send_message` for three mailers, `sendmail` for the
    MIME-preserving BRAVO weekly path). Everything before it is a definite
    pre-submission phase by construction: no message content has been written
    yet, so a failure there cannot have been accepted.

    `quit()` failures are still swallowed. A server that accepted the message
    and then dropped the connection during `QUIT` has delivered it, and turning
    that into a failure would be a false negative in the one direction that
    causes duplicate mail.
    """
    factory = smtp_factory or (smtplib.SMTP_SSL if settings.use_ssl else smtplib.SMTP)
    try:
        smtp = factory(settings.host, settings.port,
                       timeout=settings.timeout_seconds)
    except BaseException as error:
        raise SmtpSubmissionError(DEFINITE_NOT_SUBMITTED, PHASE_CONNECT, error) from error

    try:
        phase = PHASE_STARTTLS
        try:
            if settings.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            phase = PHASE_LOGIN
            if settings.username or settings.password:
                smtp.login(settings.username or "", settings.password or "")
        except BaseException as error:
            raise SmtpSubmissionError(DEFINITE_NOT_SUBMITTED, phase, error) from error

        try:
            return transmit(smtp)
        except SmtpSubmissionError:
            raise
        except BaseException as error:
            raise SmtpSubmissionError(
                classify_transmit_exception(error), PHASE_TRANSMIT, error) from error
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


# ==============================================================================
# One session, many messages
# ==============================================================================

#: Operator switch. `false` restores one session per message without a release.
ENV_SESSION_REUSE = "ECO_EMAIL_SMTP_SESSION_REUSE"
#: Upper bound on messages per session; `0`/unset means "until the relay ends it".
ENV_SESSION_MAX_MESSAGES = "ECO_EMAIL_SMTP_SESSION_MAX_MESSAGES"

def _is_connection_loss(error: BaseException) -> bool:
    """Does this failure say the CONNECTION is gone, rather than that the relay
    refused something? On a reused session that, in the envelope phase, is
    what an idle timeout looks like — and it is the only thing that earns a
    retry. `SMTPException` is itself an `OSError` subclass, so a real reply
    (`SMTPSenderRefused`, `SMTPRecipientsRefused`, ...) must be excluded by
    name: a refusal answered on a live connection is never retried.
    """
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return True
    return isinstance(error, OSError) and not isinstance(error, smtplib.SMTPException)


def session_reuse_enabled_from_env() -> bool:
    raw = (os.getenv(ENV_SESSION_REUSE) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def session_max_messages_from_env() -> Optional[int]:
    raw = (os.getenv(ENV_SESSION_MAX_MESSAGES) or "").strip()
    if not raw:
        return None
    value = int(raw)
    return value if value > 0 else None


class ReusableSmtpSession:
    """One authenticated SMTP session kept open across messages.

    WHY. example.invalid delays its `220` greeting by 12–25 s for every new connection
    from this host (a reverse-DNS wait on the relay side that this host cannot
    change). With one session per message that delay is paid ~1000 times per
    run — 5–7 hours — while the message itself takes well under a second. A
    session that stays open pays it once.

    WHAT DOES NOT CHANGE. Every message is still ONE `sendmail`, its final
    reply is still captured per message, and `SmtpSubmissionError` still
    carries the same DEFINITE / AMBIGUOUS classification with the same
    meaning. `run()` is the same contract as `run_smtp_session()`: connect,
    secure and authenticate are definite pre-submission phases; a failure out
    of `transmit` is classified by `jobs.common.eco_email_transport`.

    WHAT A SHARED SESSION HAS TO GET RIGHT, AND HOW.

    * A relay may end the session between two messages (idle timeout, per-
      session message cap, restart). Before REUSING a session a `NOOP` proves
      it is alive; a dead one is dropped and a fresh one opened — no message
      was involved, so nothing is classified.
    * The session can also die in the small window after the `NOOP`, during
      the envelope. The transport raises that with `PHASE_ENVELOPE`, which is
      proof that `DATA` was never issued. Only then, only on a REUSED session
      and only for a connection-loss error is the SAME message retried, once,
      on a fresh session. A refusal (`5xx` to `MAIL FROM`) is not retried, and
      nothing that reached `DATA` is ever retried.
    * After ANY failure out of `transmit` the session state is unknown and it
      is discarded. The next message reconnects. Correctness over the 25 s.
    * `max_messages_per_session` bounds a session so a relay cap is met on
      this side rather than discovered by a dropped connection.
    """

    def __init__(
        self,
        settings: "SmtpSessionSettings | Callable[[], SmtpSessionSettings]",
        *,
        smtp_factory: Optional[Callable[..., Any]] = None,
        reuse: bool = True,
        max_messages_per_session: Optional[int] = None,
    ) -> None:
        # Resolved on the first `_open()`, not here: a run whose every
        # candidate is skipped, rendered only, or refused before SMTP must be
        # able to hold a session object without ever touching the settings.
        self._settings_source = settings
        self._settings: Optional[SmtpSessionSettings] = None
        self._smtp_factory = smtp_factory
        self.reuse = bool(reuse)
        self.max_messages_per_session = (
            int(max_messages_per_session) if max_messages_per_session else None)
        self._smtp: Any = None
        self._messages_on_session = 0
        self.counters = {
            "smtp_sessions_opened": 0,
            "smtp_session_messages": 0,
            "smtp_session_liveness_failures": 0,
            "smtp_session_envelope_retries": 0,
            "smtp_session_dropped_after_failure": 0,
        }

    @property
    def settings(self) -> SmtpSessionSettings:
        if self._settings is None:
            source = self._settings_source
            self._settings = source() if callable(source) else source
        return self._settings

    @property
    def _factory(self) -> Callable[..., Any]:
        if self._smtp_factory is not None:
            return self._smtp_factory
        return smtplib.SMTP_SSL if self.settings.use_ssl else smtplib.SMTP

    @classmethod
    def from_env(cls, settings: "SmtpSessionSettings | Callable[[], SmtpSessionSettings]",
                 smtp_factory: Optional[Callable[..., Any]] = None) -> "ReusableSmtpSession":
        return cls(settings, smtp_factory=smtp_factory,
                   reuse=session_reuse_enabled_from_env(),
                   max_messages_per_session=session_max_messages_from_env())

    # -- lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._smtp is not None

    def _open(self) -> Any:
        """Connect, secure, authenticate — the definite pre-submission phases."""
        s = self.settings
        try:
            smtp = self._factory(s.host, s.port, timeout=s.timeout_seconds)
        except BaseException as error:
            raise SmtpSubmissionError(DEFINITE_NOT_SUBMITTED, PHASE_CONNECT, error) from error
        phase = PHASE_STARTTLS
        try:
            if s.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            phase = PHASE_LOGIN
            if s.username or s.password:
                smtp.login(s.username or "", s.password or "")
        except BaseException as error:
            try:
                smtp.quit()
            except Exception:
                pass
            raise SmtpSubmissionError(DEFINITE_NOT_SUBMITTED, phase, error) from error
        self._smtp = smtp
        self._messages_on_session = 0
        self.counters["smtp_sessions_opened"] += 1
        return smtp

    def _drop(self) -> None:
        smtp, self._smtp = self._smtp, None
        self._messages_on_session = 0
        if smtp is None:
            return
        try:
            smtp.quit()
        except Exception:
            # A relay that hangs up on QUIT has still done everything it was
            # asked to do; see `run_smtp_session`.
            pass

    def close(self) -> None:
        self._drop()

    def __enter__(self) -> "ReusableSmtpSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def summary(self) -> dict:
        return dict(self.counters)

    # -- the one call --------------------------------------------------------

    def _alive(self, smtp: Any) -> bool:
        """`NOOP` as liveness proof. No message is involved, so no evidence is
        produced either way: a dead session is simply not reused."""
        try:
            reply = smtp.noop()
        except BaseException:
            return False
        if isinstance(reply, tuple) and reply and isinstance(reply[0], int):
            return reply[0] == 250
        # A client that does not answer `noop()` with a reply tuple (a double,
        # a wrapper) is taken at its word that it did not raise.
        return True

    def _session_for_next_message(self) -> tuple[Any, bool]:
        """The session to submit on, and whether it is a REUSED one."""
        smtp = self._smtp
        if smtp is not None:
            if not self._alive(smtp):
                self.counters["smtp_session_liveness_failures"] += 1
                self._drop()
                smtp = None
        if smtp is None:
            return self._open(), False
        return smtp, True

    def run(self, transmit: Callable[[Any], Any]) -> Any:
        """Submit ONE message through `transmit`, on a kept session.

        Same contract as `run_smtp_session`; see the class docstring for the
        single retry this may perform and the exact conditions for it.
        """
        smtp, reused = self._session_for_next_message()
        try:
            result = self._transmit_once(smtp, transmit)
        except SmtpSubmissionError as error:
            if (reused and error.phase == PHASE_ENVELOPE
                    and _is_connection_loss(error.cause)):
                # The relay ended the session under us before `DATA`. Nothing
                # was transmitted, so the message is still unsent, and this
                # is the one retry. It runs on a FRESH session, where the same
                # failure would be a real refusal and is raised as such.
                self.counters["smtp_session_envelope_retries"] += 1
                smtp = self._open()
                result = self._transmit_once(smtp, transmit)
            else:
                raise
        self._messages_on_session += 1
        self.counters["smtp_session_messages"] += 1
        if (not self.reuse or (self.max_messages_per_session
                               and self._messages_on_session >= self.max_messages_per_session)):
            self._drop()
        return result

    def _transmit_once(self, smtp: Any, transmit: Callable[[Any], Any]) -> Any:
        try:
            return transmit(smtp)
        except SmtpSubmissionError:
            self.counters["smtp_session_dropped_after_failure"] += 1
            self._drop()
            raise
        except BaseException as error:
            self.counters["smtp_session_dropped_after_failure"] += 1
            self._drop()
            raise SmtpSubmissionError(
                classify_transmit_exception(error), PHASE_TRANSMIT, error) from error
