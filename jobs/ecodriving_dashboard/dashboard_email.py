"""Driver Eco Dashboard V1 — capability link and e-mail message construction.

THE LINK

    <dashboard-base-url>#k=<raw capability>

The capability lives in the URL **fragment** and nowhere else. A fragment is
never sent to a server, never appears in an access log, never reaches a
`Referer` header and never enters a query string — which is the whole reason
the delivery boundary was designed around one. `build_capability_url` refuses
to put it anywhere else, and refuses a base URL that already carries a query or
a fragment of its own.

WHAT THE MESSAGE MAY CONTAIN

The dashboard link and enough report context for the driver to know what it is:
the programme name, the period, and how long the link stays valid. Nothing
else. Specifically never: `subject_ref`, the R2 object key, the publication
operation id, the client id, the driver identity key, capability metadata, or
any authorization detail. Those are all internal identifiers whose only effect
in a mailbox would be to leak structure.

The primary artefact is the LINK. No snapshot file is attached: the dashboard
is a live authorized view, and an attachment would be an unauthenticated copy
of the same data sitting in a mailbox forever.

Subject and tone follow the existing Eco Driving notification convention
(`jobs/ecodriving/job_eco_driving_weekly_email_notifications.py`), so a driver
receives one recognisable family of messages rather than two.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from jobs.ecodriving_dashboard.delivery_contract import (
    DeliveryContractError,
    normalise_email,
)
from jobs.ecodriving_dashboard.email_provider import OutboundMessage

CAPABILITY_FRAGMENT_PREFIX = "#k="

#: Existing product convention for this programme's e-mail.
PROGRAMME_NAME = "Program Ecodriving"

WEEKLY_SUBJECT = "Program Ecodriving - Twoj panel wynikow ({start} - {end})"
MONTHLY_SUBJECT = "Program Ecodriving - Twoj panel wynikow za {month}"

_PL_MONTHS = (
    "styczen", "luty", "marzec", "kwiecien", "maj", "czerwiec",
    "lipiec", "sierpien", "wrzesien", "pazdziernik", "listopad", "grudzien",
)

_BASE_URL_PATTERN = re.compile(r"^https://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._~\-/]*)?$")
#: Loopback over plain HTTP exists for local verification only and is the one
#: exception; every deployed base URL must be HTTPS.
_LOCAL_BASE_URL_PATTERN = re.compile(
    r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/[A-Za-z0-9._~\-/]*)?$")


class EmailConstructionError(ValueError):
    """The message cannot be built safely, so it is not built at all."""


def build_capability_url(base_url: str, raw_capability: str) -> str:
    """`<base>#k=<raw>` and nothing else.

    Refuses a base URL that is not configured, is not HTTPS (loopback aside),
    or already carries a query string or fragment — appending a fragment to a
    URL that has one would silently produce a link that does not work, and
    appending to one with a query would put the boundary one typo away from a
    capability in a query string.
    """
    base = str(base_url or "").strip()
    if not base:
        # Deliberately no default: hard-coding a production domain that is not
        # yet provisioned would ship a link that goes nowhere.
        raise EmailConstructionError("DASHBOARD_BASE_URL_NOT_CONFIGURED")
    if "#" in base or "?" in base:
        raise EmailConstructionError("DASHBOARD_BASE_URL_NOT_A_BARE_URL")
    if not (_BASE_URL_PATTERN.match(base) or _LOCAL_BASE_URL_PATTERN.match(base)):
        raise EmailConstructionError("DASHBOARD_BASE_URL_INVALID")
    capability = str(raw_capability or "")
    if not re.match(r"^[A-Za-z0-9_-]{43}$", capability):
        # The Worker's capability alphabet. A value outside it cannot be a
        # capability, and encoding one into a link would be inventing a format.
        raise EmailConstructionError("CAPABILITY_MALFORMED")
    return f"{base}{CAPABILITY_FRAGMENT_PREFIX}{capability}"


def redact_capability_url(url: str) -> str:
    """The only form of a capability link that may be printed or logged."""
    marker = url.find(CAPABILITY_FRAGMENT_PREFIX)
    if marker == -1:
        return url
    return url[: marker + len(CAPABILITY_FRAGMENT_PREFIX)] + "[REDACTED]"


def _pl_date(value: date) -> str:
    return f"{value.day} {_PL_MONTHS[value.month - 1]} {value.year}"


@dataclass(frozen=True)
class DashboardEmailContext:
    """Everything the message is allowed to know.

    Note what is absent: no subject_ref, no object key, no operation id, no
    client id, no driver identity key. The type is the contract.
    """

    recipient_email: str
    period_type: str
    period_start_date: date
    period_end_date: date  # exclusive
    capability_url: str
    expires_at: Optional[datetime] = None
    programme_name: str = PROGRAMME_NAME


def build_subject(context: DashboardEmailContext) -> str:
    inclusive_end = _inclusive_end(context)
    if context.period_type == "monthly":
        return MONTHLY_SUBJECT.format(
            month=f"{_PL_MONTHS[context.period_start_date.month - 1]} "
                  f"{context.period_start_date.year}")
    return WEEKLY_SUBJECT.format(
        start=context.period_start_date.isoformat(), end=inclusive_end.isoformat())


def _inclusive_end(context: DashboardEmailContext) -> date:
    from datetime import timedelta

    return context.period_end_date - timedelta(days=1)


def build_bodies(context: DashboardEmailContext) -> tuple[str, str]:
    """HTML and plain-text bodies. The link is the artefact; the rest is context."""
    inclusive_end = _inclusive_end(context)
    if context.period_type == "monthly":
        period_phrase = (f"za {_PL_MONTHS[context.period_start_date.month - 1]} "
                         f"{context.period_start_date.year}")
    else:
        period_phrase = (f"za okres {_pl_date(context.period_start_date)} - "
                         f"{_pl_date(inclusive_end)}")

    validity = ""
    if context.expires_at is not None:
        validity = (f"Link jest wazny do {context.expires_at:%Y-%m-%d %H:%M} UTC "
                    f"i jest przeznaczony wylacznie dla Ciebie.")
    else:  # pragma: no cover - the publisher always knows the expiry
        validity = "Link jest przeznaczony wylacznie dla Ciebie."

    text_body = "\n".join([
        f"{context.programme_name}",
        "",
        f"Twoj panel wynikow {period_phrase} jest gotowy.",
        "",
        context.capability_url,
        "",
        validity,
        "Nie przekazuj tego linku innym osobom.",
    ])

    safe_url = html.escape(context.capability_url, quote=True)
    html_body = (
        "<!DOCTYPE html><html lang=\"pl\"><body>"
        f"<p>{html.escape(context.programme_name)}</p>"
        f"<p>Twoj panel wynikow {html.escape(period_phrase)} jest gotowy.</p>"
        f"<p><a href=\"{safe_url}\">Otworz panel wynikow</a></p>"
        f"<p>{html.escape(validity)}<br>Nie przekazuj tego linku innym osobom.</p>"
        "</body></html>"
    )
    return html_body, text_body


def build_message(context: DashboardEmailContext, *, message_id: str) -> OutboundMessage:
    """One recipient-bound message.

    `message_id` is derived from the publication operation, so it is stable
    across every retry — a duplicate, if a non-idempotent provider ever
    produced one, would at least be identifiable as the same logical message
    rather than looking like a second legitimate send.
    """
    if not context.capability_url or CAPABILITY_FRAGMENT_PREFIX not in context.capability_url:
        raise EmailConstructionError("CAPABILITY_URL_MISSING_FRAGMENT")
    if "?" in context.capability_url.split(CAPABILITY_FRAGMENT_PREFIX)[0]:
        raise EmailConstructionError("CAPABILITY_URL_HAS_QUERY")
    try:
        recipient = normalise_email(context.recipient_email)
    except DeliveryContractError:
        raise EmailConstructionError("RECIPIENT_EMAIL_INVALID") from None

    html_body, text_body = build_bodies(context)
    return OutboundMessage(
        recipient_email=recipient,
        subject=build_subject(context),
        html_body=html_body,
        text_body=text_body,
        message_id=message_id,
    )


def build_message_id(provider_idempotency_key: str, *, domain: str) -> str:
    """A deterministic RFC 5322 Message-ID for one logical delivery."""
    safe_domain = str(domain or "").strip().lower()
    if not re.match(r"^[a-z0-9.\-]+$", safe_domain):
        raise EmailConstructionError("MESSAGE_ID_DOMAIN_INVALID")
    if not re.match(r"^[A-Za-z0-9._-]+$", str(provider_idempotency_key or "")):
        raise EmailConstructionError("MESSAGE_ID_KEY_INVALID")
    return f"<{provider_idempotency_key}@{safe_domain}>"
