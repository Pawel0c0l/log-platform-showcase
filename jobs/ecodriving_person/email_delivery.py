"""Eco Driving Person e-mail — sender identity, transport, Sent-folder copy.

The message building, the SMTP submission and the IMAP Sent-folder copy are
shared with the two ALPHA driver mailers and live in `jobs.common`:

* `jobs.common.eco_email_transport` — one prepared message, one submission, and
  the acceptance evidence example.invalid returns for it;
* `jobs.common.eco_sent_archive` — the copy filed in the sender's Sent folder.

What stays here is what is genuinely person-specific: which environment
namespace a client sends from, and the fail-closed loading of that namespace.
The shared names are re-exported so existing callers and tests keep one import
site per concern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from jobs.common.eco_email_transport import (
    LEGACY_ACCEPTANCE_RESPONSE,
    PreparedEmail,
    SmtpAcceptance,
    build_email_message,
    open_run_session,
    prepare_email,
    submit_prepared_email,
)
from jobs.common.eco_sent_archive import (
    SentArchiveResult,
    SentArchiveSettings,
    archive_sent_message,
    load_sent_archive_settings_from_env,
    resolve_sent_archive_settings,
    sent_archive_configured,
)

__all__ = [
    "BRAVO00016_CLIENT_CODE",
    "BRAVO00016_WEEKLY_EMAIL_PREFIX",
    "DEFAULT_PERSON_EMAIL_PREFIX",
    "LEGACY_ACCEPTANCE_RESPONSE",
    "PreparedEmail",
    "SentArchiveResult",
    "SentArchiveSettings",
    "SmtpAcceptance",
    "SmtpSettings",
    "archive_sent_message",
    "build_email_message",
    "env_prefix_for_client",
    "load_sent_archive_settings_from_env",
    "load_smtp_settings_from_env",
    "open_run_session",
    "prepare_email",
    "resolve_sent_archive_settings",
    "send_html_email",
    "send_prepared_email",
    "sent_archive_configured",
    "submit_prepared_email",
]


BRAVO00016_CLIENT_CODE = "BRAVO00016"
BRAVO00016_WEEKLY_EMAIL_PREFIX = "BRAVO_ECO_WEEKLY_EMAIL"
DEFAULT_PERSON_EMAIL_PREFIX = "ECO_PERSON_EMAIL"


@dataclass(frozen=True)
class SmtpSettings:
    env_prefix: str
    host: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    use_ssl: bool
    from_email: str
    from_name: str
    reply_to: str | None
    timeout_seconds: int


def env_prefix_for_client(client_code: str | None) -> str:
    if (client_code or "").strip().upper() == BRAVO00016_CLIENT_CODE:
        return BRAVO00016_WEEKLY_EMAIL_PREFIX
    return DEFAULT_PERSON_EMAIL_PREFIX


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


def _missing_smtp_secret(prefix: str) -> RuntimeError:
    if prefix == BRAVO00016_WEEKLY_EMAIL_PREFIX:
        return RuntimeError("MANUAL_STEP_REQUIRED_MISSING_SMTP_SECRET")
    return RuntimeError(
        f"Missing required Eco Driving Person SMTP env vars for non-dry-run: {prefix}_SMTP_PASSWORD"
    )


def load_smtp_settings_from_env(
    *,
    dry_run: bool,
    client_code: str | None = None,
    env_prefix: str | None = None,
) -> SmtpSettings:
    prefix = env_prefix or env_prefix_for_client(client_code)
    use_tls = _env_bool(f"{prefix}_SMTP_USE_TLS", True)
    use_ssl = _env_bool(f"{prefix}_SMTP_USE_SSL", False)
    if use_tls and use_ssl:
        raise RuntimeError(f"{prefix}_SMTP_USE_TLS and {prefix}_SMTP_USE_SSL cannot both be true")

    host = _env_value(f"{prefix}_SMTP_HOST")
    username = _env_value(f"{prefix}_SMTP_USERNAME") or None
    password = os.getenv(f"{prefix}_SMTP_PASSWORD") or None
    from_email_default = (
        "no-reply.ecodriving@example.invalid"
        if prefix == DEFAULT_PERSON_EMAIL_PREFIX
        else ""
    )
    from_name_default = "Program Ecodriving"
    from_email = (os.getenv(f"{prefix}_FROM_EMAIL") or from_email_default).strip()
    from_name = (os.getenv(f"{prefix}_FROM_NAME") or from_name_default).strip()
    reply_to = _env_value(f"{prefix}_REPLY_TO") or None
    timeout_seconds = int(os.getenv(f"{prefix}_TIMEOUT_SECONDS") or "30")

    port_raw = _env_value(f"{prefix}_SMTP_PORT")
    if port_raw:
        port = int(port_raw)
    else:
        port = 465 if use_ssl else 587

    if not dry_run:
        missing = []
        if not host:
            missing.append(f"{prefix}_SMTP_HOST")
        if not port_raw:
            missing.append(f"{prefix}_SMTP_PORT")
        if not username:
            missing.append(f"{prefix}_SMTP_USERNAME")
        if password is None or password == "":
            if prefix == BRAVO00016_WEEKLY_EMAIL_PREFIX:
                raise _missing_smtp_secret(prefix)
            missing.append(f"{prefix}_SMTP_PASSWORD")
        if not from_email:
            missing.append(f"{prefix}_FROM_EMAIL")
        if missing:
            raise RuntimeError(
                "Missing required Eco Driving Person SMTP env vars for non-dry-run: "
                + ", ".join(missing)
            )

    if timeout_seconds <= 0:
        raise ValueError(f"{prefix}_TIMEOUT_SECONDS must be > 0")

    return SmtpSettings(
        env_prefix=prefix,
        host=host,
        port=port,
        username=username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        from_email=from_email,
        from_name=from_name,
        reply_to=reply_to,
        timeout_seconds=timeout_seconds,
    )


def send_prepared_email(
    *,
    settings: SmtpSettings,
    prepared: PreparedEmail,
    smtp_factory: Callable[..., Any] | None = None,
) -> str:
    """Submit the prepared message and return the send log's provider response.

    Kept for callers that only need the string the send log stores. A caller
    that needs the acceptance evidence itself — the relay's reply code, its
    verbatim text and any queue identifier — calls `submit_prepared_email`
    directly; this is the same single submission either way.
    """
    return submit_prepared_email(
        settings=settings, prepared=prepared, smtp_factory=smtp_factory
    ).provider_response


def send_html_email(
    *,
    settings: SmtpSettings,
    recipient_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    smtp_factory: Callable[..., Any] | None = None,
) -> str:
    prepared = prepare_email(
        settings=settings,
        recipient_email=recipient_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )
    submit_prepared_email(
        settings=settings, prepared=prepared, smtp_factory=smtp_factory
    )
    return prepared.message_id
