"""Eco Driving e-mail — the one message, and what example.invalid said about it.

WHY THIS MODULE EXISTS

All four Eco Driving mailers build one `multipart/alternative` message and hand
it to the example.invalid relay in one SMTP session. Two things used to be lost at that
point, and both are needed to answer an ordinary operator question — "was this
report actually sent, and where is the copy?":

1.  **The relay's own words.** `smtplib.SMTP.sendmail` reads the reply to the
    terminating dot, checks it is `250`, and then DISCARDS it. That reply is
    the only place example.invalid ever states its acceptance in its own terms, queue
    identifier included when it emits one. The send log recorded a constant
    literal instead.
2.  **The exact bytes.** Three of the four mailers serialised the message
    inside `send_message`, so nothing else could ever see what was transmitted
    and no Sent-folder copy could be byte-identical to it.

This module owns both: it prepares the message ONCE, transmits those exact
bytes, and returns what the relay answered.

WHAT IT DOES NOT DO

It does not change the transport, the session, or the failure classification.
`jobs.common.eco_smtp_submission.run_smtp_session` is still what connects,
secures, authenticates and says what a failure is evidence of; the final-reply
capture here is a pure observer bolted onto the session's own `data()` step and
cannot turn a failure into a success or a success into a failure.

It says nothing about DELIVERY. `SmtpAcceptance` means exactly one thing:

    example.invalid SMTP accepted this message from this host for relay.

Not that a recipient server took it, not that it reached a mailbox, not that
anybody read it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Callable, Optional

from jobs.common.eco_smtp_submission import (
    DEFINITE_NOT_SUBMITTED,
    PHASE_ENVELOPE,
    ReusableSmtpSession,
    SmtpSessionSettings,
    SmtpSubmissionError,
    run_smtp_session,
)

#: What the send log's `provider_response` said before the relay's own reply
#: could be captured, and what it still says when the reply is unobtainable —
#: for a transport double, or a client object that does not expose `data()`.
#:
#: IT IS A STATEMENT ABOUT THE LIBRARY, NOT ABOUT example.invalid. It is kept verbatim so
#: historical rows and new rows mean the same thing, and it must never be read
#: or presented as the relay's own words. `smtp_acceptance.final_reply_observed`
#: is the field that says which of the two a row holds.
LEGACY_ACCEPTANCE_RESPONSE = "SMTP accepted message without raising an exception"

#: The single `metadata_json` key this module writes. Namespaced away from the
#: ambiguous-submission markers in `jobs.common.eco_email_reconciliation`, which
#: are read by the reservation guard: acceptance evidence must never be able to
#: influence what may or may not be reserved.
KEY_ACCEPTANCE = "smtp_acceptance"

#: The one statement a successful submission supports. Deliberately NOT
#: `DELIVERED`.
ACCEPTED_FOR_RELAY = "ACCEPTED_BY_SMTP_FOR_RELAY"

#: Where the evidence came from.
EVIDENCE_FINAL_REPLY = "SMTP_FINAL_REPLY"
EVIDENCE_LIBRARY_RETURN = "LIBRARY_RETURN_WITHOUT_FINAL_REPLY"

#: How a relay names the message it just took. Each pattern belongs to a real
#: MTA family; NOTHING is synthesised when none of them matches, because a
#: queue identifier we invented could not be quoted back at example.invalid.
_QUEUE_ID_PATTERNS = (
    # Postfix: "2.0.0 Ok: queued as D6B0F1C0A2"
    re.compile(r"queued\s+as\s+([A-Za-z0-9][A-Za-z0-9._-]{3,})", re.IGNORECASE),
    # Exim: "OK id=1abcde-0001Ab-2C"
    re.compile(r"\bid=([A-Za-z0-9][A-Za-z0-9._-]{3,})", re.IGNORECASE),
    # Explicit, whatever emits it: "queue-id: ABC123"
    re.compile(r"\bqueue[ _-]?id\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]{3,})",
               re.IGNORECASE),
    # Sendmail: "2.0.0 x9AB2c3d004567 Message accepted for delivery"
    re.compile(r"\b([A-Za-z0-9]{9,})\s+Message\s+accepted\s+for\s+delivery",
               re.IGNORECASE),
)

#: Response text is evidence, not a data feed. Truncated so one talkative relay
#: cannot bloat a send-log row.
_MAX_RESPONSE_CHARS = 500


def parse_queue_id(response_text: Optional[str]) -> Optional[str]:
    """The relay's own identifier for the message, or None. Never invented."""
    if not response_text:
        return None
    for pattern in _QUEUE_ID_PATTERNS:
        match = pattern.search(response_text)
        if match:
            return match.group(1)
    return None


@dataclass(frozen=True)
class SmtpAcceptance:
    """example.invalid accepted this message for relay — and here is what it said.

    `code`/`response_text` are the reply to the terminating dot: the single
    protocol moment at which the relay takes responsibility for the message.
    They are `None` only when the client object in use did not expose the
    `data()` step to be observed; the acceptance itself is still established,
    because `sendmail` raises unless that reply was `250`.
    """

    message_id: str
    accepted_at: datetime
    code: Optional[int] = None
    response_text: Optional[str] = None
    queue_id: Optional[str] = None

    @property
    def evidence_available(self) -> bool:
        return self.code is not None

    @property
    def evidence_source(self) -> str:
        return EVIDENCE_FINAL_REPLY if self.evidence_available else EVIDENCE_LIBRARY_RETURN

    @property
    def provider_response(self) -> str:
        """What the send log's `provider_response` column records.

        The relay's verbatim final reply when we have it — always prefixed by
        its reply code, so an observed row is recognisable on sight. Otherwise
        the literal this column has always held, which describes what the
        LIBRARY did and claims nothing about what example.invalid said. Which of the two
        a row holds is stated outright in
        `metadata_json.smtp_acceptance.final_reply_observed`; nothing here ever
        fabricates server text to fill the column.
        """
        if not self.evidence_available:
            return LEGACY_ACCEPTANCE_RESPONSE
        text = (self.response_text or "").strip()
        return f"{self.code} {text}".strip()

    def as_metadata(self) -> dict:
        """The durable evidence, as one namespaced `metadata_json` object."""
        return {
            KEY_ACCEPTANCE: {
                "result": ACCEPTED_FOR_RELAY,
                "evidence": self.evidence_source,
                # THE DISCRIMINATOR. `false` means: smtplib returned from the
                # submission without raising — which is itself proof the relay
                # answered the terminating dot with 250 — but this host did not
                # capture the reply text, so `provider_response` holds the
                # library literal and NOT example.invalid's words. Never read a `false`
                # row as if the relay had been quoted.
                "final_reply_observed": self.evidence_available,
                "smtp_code": self.code,
                "smtp_response": self.response_text,
                "queue_id": self.queue_id,
                "message_id": self.message_id,
                "accepted_at": self.accepted_at.astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        }

    @classmethod
    def from_final_reply(
        cls,
        reply: Any,
        *,
        message_id: str,
        accepted_at: Optional[datetime] = None,
    ) -> "SmtpAcceptance":
        code, text = _decode_final_reply(reply)
        return cls(
            message_id=message_id,
            accepted_at=accepted_at or datetime.now(timezone.utc),
            code=code,
            response_text=text,
            queue_id=parse_queue_id(text),
        )


def _decode_final_reply(reply: Any) -> tuple[Optional[int], Optional[str]]:
    """`(code, resp)` as smtplib returns it, made durable — or `(None, None)`.

    Anything unexpected degrades to "no evidence captured". A malformed reply
    must never be reported as if the relay had said it.
    """
    if not isinstance(reply, tuple) or len(reply) != 2:
        return None, None
    code, raw = reply
    if isinstance(code, bool) or not isinstance(code, int):
        return None, None
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", "replace")
    elif raw is None:
        text = ""
    else:
        text = str(raw)
    text = " ".join(text.split())[:_MAX_RESPONSE_CHARS]
    return code, text


class _FinalReplyRecorder:
    """Keeps the reply to the terminating dot that `sendmail` throws away.

    A PURE OBSERVER. It wraps `SMTP.data`, records what that call returned and
    hands the value straight back, so every branch of `sendmail` — including
    every raise — behaves exactly as it did before. Nothing here decides
    anything; the classification in `jobs.common.eco_smtp_submission` is
    untouched and remains the only authority on what a failure proves.
    """

    def __init__(self) -> None:
        self.reply: Any = None
        #: Whether `data()` could be observed at all. Without this, nothing
        #: below can be said about the phase a failure happened in.
        self.installed: bool = False
        #: Whether the `DATA` step was ENTERED. `installed and not
        #: data_entered` after a failure is proof the message body was never
        #: written: `smtplib.sendmail` issues `DATA` from `data()` and from
        #: nowhere else.
        self.data_entered: bool = False

    def install(self, smtp: Any) -> "_FinalReplyRecorder":
        try:
            original = smtp.data
        except Exception:
            return self
        if not callable(original):
            return self
        # ON A KEPT SESSION THIS RUNS ONCE PER MESSAGE ON THE SAME OBJECT. A
        # recorder left behind by the previous message would be wrapped again
        # here, and a thousand messages later `data()` would be a thousand
        # nested calls deep. So: never wrap a wrapper, and `uninstall()` after
        # the message so the client is handed back exactly as it was.
        original = getattr(original, "_eco_wrapped_original", original)

        def _recording_data(message: Any) -> Any:
            self.data_entered = True
            result = original(message)
            try:
                self.reply = result
            except Exception:  # pragma: no cover - defensive only
                pass
            return result

        _recording_data._eco_wrapped_original = original  # type: ignore[attr-defined]
        try:
            instance_dict = getattr(smtp, "__dict__", None)
            self._had_instance_attr = (instance_dict is not None and "data" in instance_dict)
            self._previous_attr = instance_dict.get("data") if self._had_instance_attr else None
            smtp.data = _recording_data
            self._smtp = smtp
            self.installed = True
        except Exception:
            # A client that will not accept the attribute (slots, a proxy) just
            # yields no evidence. It never yields a broken send.
            pass
        return self

    def uninstall(self) -> None:
        """Hand the client back as it was. Safe to call when nothing was installed."""
        smtp = getattr(self, "_smtp", None)
        if smtp is None or not self.installed:
            return
        try:
            if self._had_instance_attr:
                smtp.data = self._previous_attr
            else:
                delattr(smtp, "data")
        except Exception:  # pragma: no cover - defensive only
            pass
        self._smtp = None

    def proves_envelope_phase(self) -> bool:
        """True only when it is PROVEN that `DATA` was never issued."""
        return self.installed and not self.data_entered


@dataclass(frozen=True)
class PreparedEmail:
    """One message, built once: the object, its exact bytes, its envelope."""

    message_id: str
    message: EmailMessage
    mime_bytes: bytes
    mime_sha256: str
    envelope_sender: str
    recipients: tuple[str, ...]


def build_email_message(
    *,
    settings: Any,
    recipient_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    message_id: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.from_name, settings.from_email))
    msg["To"] = recipient_email
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=False)
    if settings.reply_to:
        msg["Reply-To"] = settings.reply_to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def prepare_email(
    *,
    settings: Any,
    recipient_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    message_id: str | None = None,
) -> PreparedEmail:
    domain = settings.from_email.split("@", 1)[-1] if "@" in settings.from_email else None
    final_message_id = message_id or make_msgid(domain=domain)
    msg = build_email_message(
        settings=settings,
        recipient_email=recipient_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        message_id=final_message_id,
    )
    mime_bytes = msg.as_bytes(policy=policy.SMTP)
    return PreparedEmail(
        message_id=final_message_id,
        message=msg,
        mime_bytes=mime_bytes,
        mime_sha256=hashlib.sha256(mime_bytes).hexdigest(),
        envelope_sender=settings.from_email,
        recipients=(recipient_email,),
    )


def submit_prepared_email(
    *,
    settings: Any,
    prepared: PreparedEmail,
    smtp_factory: Callable[..., Any] | None = None,
    session: ReusableSmtpSession | None = None,
) -> SmtpAcceptance:
    """Transmit the prepared bytes once, and return what example.invalid answered.

    ONE `sendmail`, no rewrite. The return value exists only on the success
    path: any failure leaves this function as an `SmtpSubmissionError`
    carrying its classification, exactly as before.

    Without `session` this is one SMTP session per message, as it always was.
    With a `ReusableSmtpSession` the message goes out on that kept session;
    the session decides connect/reconnect and performs the one retry it is
    allowed (envelope-phase connection loss on a reused session — see its
    docstring). The evidence returned is per message either way.

    An explicit `smtp_factory` names the transport for THIS submission and
    takes precedence over `session`: it is the injection seam the mailer
    tests use to put a double under the real job code, and a double handed
    in for one message must not be silently bypassed by a session that was
    built around the real `smtplib`.
    """

    def _transmit(smtp: Any) -> Any:
        recorder = _FinalReplyRecorder().install(smtp)
        try:
            refused = smtp.sendmail(
                prepared.envelope_sender,
                list(prepared.recipients),
                prepared.mime_bytes,
            )
        except BaseException as error:
            if recorder.proves_envelope_phase():
                # `DATA` was never issued, so no byte of the body was written:
                # definite non-acceptance whatever the exception type. Stated
                # with its own phase so a kept session can tell an idle
                # timeout from a refusal, and an operator can see which.
                raise SmtpSubmissionError(
                    DEFINITE_NOT_SUBMITTED, PHASE_ENVELOPE, error) from error
            raise
        finally:
            recorder.uninstall()
        if refused:
            # PARTIAL ACCEPTANCE IS STILL ACCEPTANCE. `sendmail` returns a
            # non-empty map only when at least one recipient WAS accepted and
            # the message was therefore transmitted; if every recipient is
            # refused it raises `SMTPRecipientsRefused` instead. So this is
            # deliberately a plain exception, which classifies as AMBIGUOUS: a
            # copy is out.
            raise RuntimeError(f"SMTP refused recipients: {sorted(refused)}")
        return recorder.reply

    if session is not None and smtp_factory is None:
        reply = session.run(_transmit)
    else:
        reply = run_smtp_session(
            settings=session_settings_for(settings),
            transmit=_transmit,
            smtp_factory=smtp_factory,
        )
    return SmtpAcceptance.from_final_reply(reply, message_id=prepared.message_id)


def session_settings_for(settings: Any) -> SmtpSessionSettings:
    """The transport parameters of a mailer's `SmtpSettings`, nothing else."""
    return SmtpSessionSettings(
        host=settings.host,
        port=settings.port,
        timeout_seconds=settings.timeout_seconds,
        use_tls=settings.use_tls,
        use_ssl=settings.use_ssl,
        username=settings.username,
        password=settings.password,
    )


def open_run_session(settings: Any,
                     smtp_factory: Callable[..., Any] | None = None) -> ReusableSmtpSession:
    """The session a mailer keeps for one whole run. Opens nothing until the
    first message; honours `ECO_EMAIL_SMTP_SESSION_REUSE` / `_MAX_MESSAGES`."""
    return ReusableSmtpSession.from_env(lambda: session_settings_for(settings),
                                        smtp_factory=smtp_factory)
