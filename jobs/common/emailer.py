from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Iterable


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    from_addr: str
    timeout_s: int = 30


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_smtp_config_from_env() -> SmtpConfig:
    host = (os.getenv("AUTOMATION_SMTP_HOST") or "").strip()
    if not host:
        raise RuntimeError("Missing required SMTP env: AUTOMATION_SMTP_HOST")

    use_tls = _env_bool("AUTOMATION_SMTP_USE_TLS", True)
    default_port = 587 if use_tls else 25
    port = int(os.getenv("AUTOMATION_SMTP_PORT") or default_port)

    return SmtpConfig(
        host=host,
        port=port,
        username=(os.getenv("AUTOMATION_SMTP_USERNAME") or "").strip() or None,
        password=os.getenv("AUTOMATION_SMTP_PASSWORD") or None,
        use_tls=use_tls,
        from_addr=(os.getenv("AUTOMATION_SMTP_FROM") or "automations@example.invalid").strip(),
    )


def _normalize_recipients(to_addrs: str | Iterable[str]) -> list[str]:
    if isinstance(to_addrs, str):
        raw = to_addrs.replace(";", ",").split(",")
    else:
        raw = list(to_addrs)
    return [addr.strip() for addr in raw if addr and addr.strip()]


@dataclass(frozen=True)
class SendResult:
    """Delivery identity of one sent message. `message_id` is the RFC 5322
    Message-ID header this sender set, usable as the provider message id."""

    message_id: str
    recipients: tuple[str, ...]


def send_html_email(
    *,
    to_addrs: str | Iterable[str],
    subject: str,
    html_body: str,
    text_body: str,
    config: SmtpConfig | None = None,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    message_id: str | None = None,
) -> SendResult:
    smtp_config = config or load_smtp_config_from_env()
    recipients = _normalize_recipients(to_addrs)
    if not recipients:
        raise RuntimeError("No email recipients configured")

    msg = EmailMessage()
    msg["From"] = smtp_config.from_addr
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    smtp = smtp_factory(smtp_config.host, smtp_config.port, timeout=smtp_config.timeout_s)
    try:
        if smtp_config.use_tls:
            smtp.starttls(context=ssl.create_default_context())
        if smtp_config.username or smtp_config.password:
            smtp.login(smtp_config.username or "", smtp_config.password or "")
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    return SendResult(message_id=str(msg.get("Message-ID") or ""), recipients=tuple(recipients))
