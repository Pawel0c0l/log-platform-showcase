"""Eco Driving e-mail — the copy in the sender's Sent folder.

WHAT THIS IS FOR

A message this platform sends should be visible where a person would look for
it: in the sending mailbox's Sent / Wysłane folder, identical to the message
the recipient got. That is all this module does — it `APPEND`s the EXACT MIME
bytes that were transmitted into the mailbox's `\\Sent` folder over IMAP, and
verifies the copy by `Message-ID`.

THE RULE THAT OUTRANKS EVERYTHING ELSE HERE

    SMTP transmission and the Sent-folder copy are two separate effects, and
    only the first one is irreversible.

Once example.invalid has accepted a message, the customer either has it or the relay owes
it to them. Nothing that happens afterwards may cause a second submission. So
`store_sent_copy()` NEVER RAISES: not when IMAP is down, not when the mailbox
cannot be discovered, not when the verification search disagrees, not when
recording the outcome in the send log fails. It returns what happened, the
caller counts it and logs it, and the SMTP result stands untouched.

A failed copy is an operator's problem, visible as `sent_archive_status =
'failed'` (or the same statement in `metadata_json`), an ERROR line naming the
`Message-ID`, and a counter in the run summary. It is never an instruction to
resend, and there is no automatic recovery loop here.

WHICH MAILBOX

The `\\Sent` special-use folder the account itself advertises, discovered per
session. `{PREFIX}_IMAP_SENT_MAILBOX` overrides that when the server does not
advertise one, and a configured name that does not exist is a hard
configuration error rather than a guess at a provider-specific default.

WHICH CREDENTIALS

The sending mailbox's own. `{PREFIX}_IMAP_USERNAME` / `{PREFIX}_IMAP_PASSWORD`
are OPTIONAL overrides; when they are absent the `{PREFIX}_SMTP_USERNAME` /
`{PREFIX}_SMTP_PASSWORD` already configured for this sender are used, because
the Sent folder being written to belongs to the account that authenticated to
SMTP. One mailbox is configured once. The IMAP ENDPOINT is not inherited that
way — host, port and SSL mode are protocol facts about a different service and
are never guessed from the SMTP ones.
"""

from __future__ import annotations

import imaplib
import os
import re
import ssl
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

#: Sent-copy outcomes. The first three are the values the person send logs'
#: `sent_archive_status` CHECK constraint accepts (`db/client_business/
#: 041_eco_person_sent_archive_state.sql`), plus the `pending` that
#: `mark_sent_mime_preserved` writes before the attempt.
STATUS_APPENDED = "appended"
STATUS_ALREADY_PRESENT = "already_present"
STATUS_FAILED = "failed"
#: Never written to that column: it describes the absence of an attempt, not
#: the outcome of one.
STATUS_NOT_CONFIGURED = "not_configured"

#: The `metadata_json` key used by the send logs that have no dedicated
#: archive columns (the two ALPHA driver tables).
KEY_SENT_COPY = "sent_folder_copy"

_IMAP_ENV_SUFFIXES = (
    "_IMAP_HOST",
    "_IMAP_PORT",
    "_IMAP_USERNAME",
    "_IMAP_PASSWORD",
    "_IMAP_USE_SSL",
    "_IMAP_TIMEOUT_SECONDS",
    "_IMAP_SENT_MAILBOX",
)

#: What IMAP cannot borrow from anywhere else. An SMTP endpoint is not an IMAP
#: endpoint, so host, port and the SSL mode are stated explicitly or the
#: configuration is incomplete — they are never derived from the SMTP ones.
_REQUIRED_IMAP_SUFFIXES = (
    "_IMAP_HOST",
    "_IMAP_PORT",
    "_IMAP_USE_SSL",
)

#: What IMAP DOES borrow. The Sent folder belongs to the mailbox the message
#: was sent FROM, so the account that authenticated to SMTP is the account that
#: owns the copy: one mailbox, one username, one password. `{PREFIX}_IMAP_
#: USERNAME` / `{PREFIX}_IMAP_PASSWORD` remain supported for the provider that
#: wants a different IMAP login for the same mailbox; unset, the SMTP variables
#: of the SAME prefix are read directly. Nothing is copied between the two
#: namespaces, and no credential value is logged, echoed or written back.
_CREDENTIAL_FALLBACK = (
    ("_IMAP_USERNAME", "_SMTP_USERNAME", True),
    ("_IMAP_PASSWORD", "_SMTP_PASSWORD", False),
)


@dataclass(frozen=True)
class SentArchiveSettings:
    env_prefix: str
    host: str
    port: int
    username: str
    password: str
    use_ssl: bool
    timeout_seconds: int
    sent_mailbox: str | None = None


@dataclass(frozen=True)
class SentArchiveResult:
    status: str
    mailbox: str
    message_id: str
    matched_count: int


@dataclass(frozen=True)
class SentCopyOutcome:
    """What the Sent-folder copy did. Never an SMTP statement."""

    status: str
    message_id: str
    mailbox: str | None = None
    error: str | None = None

    @property
    def stored(self) -> bool:
        return self.status in (STATUS_APPENDED, STATUS_ALREADY_PRESENT)

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED

    @property
    def attempted(self) -> bool:
        return self.status != STATUS_NOT_CONFIGURED

    def as_metadata(self) -> dict:
        return {
            KEY_SENT_COPY: {
                "status": self.status,
                "mailbox": self.mailbox,
                "message_id": self.message_id,
                "error": (self.error or None) and str(self.error)[:500],
                "recorded_at": datetime.now(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        }


def _env_bool(name: str, default: bool | None = None) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if default is None:
            raise ValueError(f"{name} must be set to true or false")
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


def _env_value(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _resolve_credential(
    env_prefix: str, imap_suffix: str, smtp_suffix: str, *, strip: bool
) -> str | None:
    """The IMAP override if it is set, otherwise this sender's SMTP credential.

    Reads only; the value is returned to the caller and never stored back into
    the environment under a second name.
    """
    for suffix in (imap_suffix, smtp_suffix):
        raw = os.getenv(f"{env_prefix}{suffix}")
        if raw is None:
            continue
        value = raw.strip() if strip else raw
        if value:
            return value
    return None


def load_sent_archive_settings_from_env(*, env_prefix: str) -> SentArchiveSettings:
    """Fail-closed IMAP configuration for one sender identity.

    The transport settings are IMAP's own and must be given. The credentials
    are the MAILBOX's: unless an explicit `{PREFIX}_IMAP_USERNAME` /
    `{PREFIX}_IMAP_PASSWORD` override says otherwise, the same account already
    configured for SMTP under this prefix is used, so one mailbox never needs
    two copies of its own password. If neither source has them, that is a
    configuration error like any other missing value — never a silent skip.
    """
    required_names = [f"{env_prefix}{suffix}" for suffix in _REQUIRED_IMAP_SUFFIXES]
    values = {name: os.getenv(name) for name in required_names}
    missing = [name for name in required_names if not (values[name] or "").strip()]

    credentials: dict[str, str | None] = {}
    for imap_suffix, smtp_suffix, strip in _CREDENTIAL_FALLBACK:
        resolved = _resolve_credential(
            env_prefix, imap_suffix, smtp_suffix, strip=strip
        )
        credentials[imap_suffix] = resolved
        if resolved is None:
            missing.append(
                f"{env_prefix}{imap_suffix} (or {env_prefix}{smtp_suffix})"
            )

    if missing:
        raise RuntimeError(
            "MANUAL_STEP_REQUIRED_MISSING_SENT_FOLDER_CONFIG: " + ", ".join(missing)
        )
    port = int(str(values[f"{env_prefix}_IMAP_PORT"]).strip())
    if port < 1 or port > 65535:
        raise ValueError(f"{env_prefix}_IMAP_PORT must be between 1 and 65535")
    timeout_seconds = int(os.getenv(f"{env_prefix}_IMAP_TIMEOUT_SECONDS") or "30")
    if timeout_seconds <= 0:
        raise ValueError(f"{env_prefix}_IMAP_TIMEOUT_SECONDS must be > 0")
    return SentArchiveSettings(
        env_prefix=env_prefix,
        host=str(values[f"{env_prefix}_IMAP_HOST"]).strip(),
        port=port,
        username=str(credentials["_IMAP_USERNAME"]),
        password=str(credentials["_IMAP_PASSWORD"]),
        use_ssl=_env_bool(f"{env_prefix}_IMAP_USE_SSL", None),
        timeout_seconds=timeout_seconds,
        sent_mailbox=_env_value(f"{env_prefix}_IMAP_SENT_MAILBOX") or None,
    )


def sent_archive_configured(*, env_prefix: str) -> bool:
    """Has this sender identity been given a mailbox to file its copies in?"""
    return any(_env_value(f"{env_prefix}{suffix}") for suffix in _IMAP_ENV_SUFFIXES)


def resolve_sent_archive_settings(
    *, env_prefix: str, required: bool
) -> SentArchiveSettings | None:
    """The IMAP settings for this sender, or None when it has none configured.

    `required=True` keeps the existing BRAVO00016 contract: that sender must
    have a Sent mailbox, and a missing or half-written configuration stops the
    run before any mail is sent rather than silently dropping the copy.

    `required=False` is how a sender identity opts in by configuration alone —
    no IMAP variables means no copy is attempted (loudly reported in the run
    summary), while a PARTIAL configuration is still an error, because a
    half-configured mailbox is a mistake, not a decision.
    """
    if required or sent_archive_configured(env_prefix=env_prefix):
        return load_sent_archive_settings_from_env(env_prefix=env_prefix)
    return None


def _make_imap(settings: SentArchiveSettings, imap_factory: Callable[..., Any] | None):
    if imap_factory is not None:
        return imap_factory(settings.host, settings.port, timeout=settings.timeout_seconds)
    if settings.use_ssl:
        return imaplib.IMAP4_SSL(
            settings.host,
            settings.port,
            ssl_context=ssl.create_default_context(),
            timeout=settings.timeout_seconds,
        )
    return imaplib.IMAP4(settings.host, settings.port, timeout=settings.timeout_seconds)


#: One IMAP `LIST` row: `(\Attr \Attr) "delim" name`. The delimiter is a quoted
#: character on every server this platform talks to, but the unquoted form
#: (`NIL`) is equally legal, so both are matched — a row this pattern cannot
#: read is dropped, and a dropped `\Sent` row means no Sent copy at all.
_LIST_RE = re.compile(r"^\((?P<attrs>.*?)\)\s+(?P<delim>\"[^\"]*\"|\S+)\s+(?P<name>.+)$")


def _decode_imap_atom(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
        text = text.replace(r"\"", '"').replace(r"\\", "\\")
    return text


def _parse_list_rows(rows: Iterable[Any]) -> list[tuple[set[str], str]]:
    parsed: list[tuple[set[str], str]] = []
    for raw in rows or []:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        match = _LIST_RE.match(line.strip())
        if not match:
            continue
        attrs = {part.strip().lower() for part in match.group("attrs").split() if part.strip()}
        parsed.append((attrs, _decode_imap_atom(match.group("name"))))
    return parsed


def _imap_check(typ: str, data: Any, operation: str) -> Any:
    if typ != "OK":
        raise RuntimeError(f"IMAP {operation} failed")
    return data


def _discover_sent_mailbox(imap: Any, settings: SentArchiveSettings) -> str:
    rows = _imap_check(*imap.list('""', '"*"'), operation="LIST")
    parsed = _parse_list_rows(rows)
    if settings.sent_mailbox:
        available = {name for _attrs, name in parsed}
        if settings.sent_mailbox not in available:
            raise RuntimeError(
                "MANUAL_STEP_REQUIRED_MISSING_SENT_FOLDER_CONFIG: "
                f"{settings.env_prefix}_IMAP_SENT_MAILBOX"
            )
        return settings.sent_mailbox
    sent = [name for attrs, name in parsed if r"\sent" in attrs]
    if len(sent) != 1:
        raise RuntimeError(
            "MANUAL_STEP_REQUIRED_MISSING_SENT_FOLDER_CONFIG: "
            f"{settings.env_prefix}_IMAP_SENT_MAILBOX"
        )
    return sent[0]


def _search_message_id(imap: Any, mailbox: str, message_id: str) -> int:
    _imap_check(*imap.select(mailbox, readonly=True), operation="SELECT")
    rows = _imap_check(
        *imap.search(None, "HEADER", "Message-ID", message_id),
        operation="SEARCH",
    )
    raw = b" ".join(part for part in rows if isinstance(part, bytes))
    return len([part for part in raw.split() if part])


def archive_sent_message(
    *,
    settings: SentArchiveSettings,
    message_id: str,
    mime_bytes: bytes,
    sent_at: datetime | None = None,
    imap_factory: Callable[..., Any] | None = None,
) -> SentArchiveResult:
    """APPEND the transmitted bytes to the Sent folder, verified by Message-ID.

    Raises on any IMAP problem. `store_sent_copy()` is the caller that turns
    that into a recorded outcome; the raising form stays for the operator's
    `archive_only` recovery path, which wants the failure to surface.
    """
    imap = _make_imap(settings, imap_factory)
    try:
        _imap_check(*imap.login(settings.username, settings.password), operation="LOGIN")
        mailbox = _discover_sent_mailbox(imap, settings)
        before_count = _search_message_id(imap, mailbox, message_id)
        if before_count:
            return SentArchiveResult(
                status=STATUS_ALREADY_PRESENT,
                mailbox=mailbox,
                message_id=message_id,
                matched_count=before_count,
            )
        append_time = sent_at or datetime.now(timezone.utc)
        _imap_check(
            *imap.append(
                mailbox,
                r"(\Seen)",
                imaplib.Time2Internaldate(append_time),
                mime_bytes,
            ),
            operation="APPEND",
        )
        after_count = _search_message_id(imap, mailbox, message_id)
        if after_count != 1:
            raise RuntimeError(f"Sent archive verification found {after_count} copies")
        return SentArchiveResult(
            status=STATUS_APPENDED,
            mailbox=mailbox,
            message_id=message_id,
            matched_count=after_count,
        )
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def store_sent_copy(
    *,
    settings: SentArchiveSettings | None,
    message_id: str,
    mime_bytes: bytes,
    conn: Any,
    record: Callable[[SentCopyOutcome], None],
    client: Any = None,
    job_source: str = "",
    run_id: str = "",
    log_message: str = "Eco Driving Sent-folder copy failed after SMTP acceptance",
    log_context: dict | None = None,
    sent_at: datetime | None = None,
    imap_factory: Callable[..., Any] | None = None,
) -> SentCopyOutcome:
    """File the transmitted message in the Sent folder. **NEVER RAISES.**

    Called only after the SMTP acceptance has already been committed, and
    deliberately OUTSIDE the caller's SMTP try/except so that no failure here
    can reach the code that decides whether a submission was ambiguous. There
    is no path from this function back to SMTP.

    `record` writes the outcome into the send log with the caller's cursor;
    if that write or its commit fails, the row keeps its committed `sent`
    state and the failure is reported instead of raised.
    """
    if settings is None:
        return SentCopyOutcome(status=STATUS_NOT_CONFIGURED, message_id=message_id)

    try:
        result = archive_sent_message(
            settings=settings,
            message_id=message_id,
            mime_bytes=mime_bytes,
            sent_at=sent_at or datetime.now(timezone.utc),
            imap_factory=imap_factory,
        )
        outcome = SentCopyOutcome(
            status=result.status, message_id=result.message_id, mailbox=result.mailbox
        )
    except Exception as exc:  # noqa: BLE001 - a copy may never break a send
        outcome = SentCopyOutcome(
            status=STATUS_FAILED, message_id=message_id, error=str(exc)
        )

    try:
        record(outcome)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - same rule for the bookkeeping
        try:
            conn.rollback()
        except Exception:
            pass
        outcome = replace(
            outcome,
            status=STATUS_FAILED,
            error=f"{outcome.error + ' | ' if outcome.error else ''}"
                  f"sent-copy outcome not recorded: {exc}",
        )

    if outcome.failed and client is not None:
        try:
            client.log(
                "ERROR",
                "SCRIPT",
                job_source,
                log_message,
                run_id=run_id,
                context={
                    **(log_context or {}),
                    "smtp_message_id": message_id,
                    "sent_copy_status": outcome.status,
                    # The customer already has the message, or example.invalid owes it to
                    # them. This is a mailbox problem, never a resend trigger.
                    "smtp_result_stands": True,
                    "operator_action_required": True,
                    "resend_authorized": False,
                },
                error=str(outcome.error),
            )
        except Exception:
            pass

    return outcome


def count_sent_copy(summary: dict, outcome: SentCopyOutcome) -> None:
    """One counter vocabulary for all four mailers' run summaries."""
    if outcome.status == STATUS_APPENDED:
        summary["sent_archive_count"] = summary.get("sent_archive_count", 0) + 1
    elif outcome.status == STATUS_ALREADY_PRESENT:
        summary["sent_archive_already_present_count"] = (
            summary.get("sent_archive_already_present_count", 0) + 1)
    elif outcome.status == STATUS_FAILED:
        summary["sent_archive_failed_count"] = (
            summary.get("sent_archive_failed_count", 0) + 1)
    else:
        summary["sent_archive_skipped_count"] = (
            summary.get("sent_archive_skipped_count", 0) + 1)
