"""Driver Eco Dashboard V1 — integration with the EXISTING Eco Driving mailings.

WHAT THIS IS, AND WHAT IT REFUSES TO BE

The four existing Eco Driving notification jobs

    jobs/ecodriving/job_eco_driving_weekly_email_notifications.py
    jobs/ecodriving/job_eco_driving_monthly_email_notifications.py
    jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py
    jobs/ecodriving_person/job_eco_driving_person_monthly_email_notifications.py

are already the fleet orchestrator. They select the client, enumerate drivers,
resolve identity and recipient, choose the authoritative reporting period,
isolate per-driver failures, account for the run, render the message, enforce
send-log idempotency and send over example.invalid SMTP. Not one of those
responsibilities is re-implemented here, and none of them may be.

This module adds the ONE thing they do not have: a secure per-driver dashboard
link. Per candidate it

    builds the privacy-minimised snapshot from the SAME persisted Eco data the
    job is mailing about -> publishes it under the logical delivery identity ->
    obtains the current capability URL -> records the handoff -> returns the URL

and then gets out of the way. It sends nothing. It never touches
`jobs.ecodriving_dashboard.email_provider`, so a second SMTP lifecycle around
the same message cannot exist: the module does not import it, and the
publication-only seam it calls (`publisher.ensure_capability`) has no
provider-facing step at all.

THE PERIOD IS AN INPUT, NEVER A DECISION

`period_start_date` / `period_end_date` come from the job that already selected
them — the persisted cumulative W1/W2/W3 weekly snapshot, or the closed month.
This module derives no boundary of its own and substitutes nothing. It computes
only the *metadata* the snapshot contract needs about the period it was handed
(which month it belongs to, which sequence inside that month, how many closed
periods that month has), and it verifies that metadata against the period it was
given. A disagreement is a hard refusal, never a correction: telling a driver
about a period other than the one their e-mail is about would be worse than
sending no link.

ONE CONNECTION PER RUN, NOT ONE PER DRIVER

Snapshot reads run on a cursor lent by the Eco job's own client-business
connection, so they cost no new session. Each driver's read is wrapped in its
own SUBTRANSACTION, because sharing that connection would otherwise turn one
driver's SQL error into the whole run's `25P02`: PostgreSQL aborts the entire
transaction on any statement failure, so catching it in Python is not isolation
unless a savepoint can undo it. See `_snapshot_cursor`.

The delivery ledger needs autocommit — "persisted" has to mean persisted the
moment the call returns — which the Eco job's transactional connection cannot
provide, so exactly ONE additional connection is opened lazily per run and
reused for every driver. Population statistics that are identical for every
driver in the period are fetched once.

FAILURE SEMANTICS

A product state is not a failure. A snapshot that says "insufficient qualifying
distance" or "report not ready" is a legitimate dashboard, and it is published
and linked like any other.

A TECHNICAL failure — snapshot construction, a database error, integrity,
publication, capability persistence, an expired capability that could not be
rotated, preflight, authorization, a template that cannot show the link —
returns `FAILED` and no URL. The caller must then not send that driver's e-mail
at all: an Eco message that silently lost its dashboard link would be a
misleading success. Every other driver in the run is unaffected.

NO CAPABILITY EVER LEAVES THIS MODULE EXCEPT AS THE RETURN VALUE

`DashboardLinkOutcome.capability_url` is handed to the caller's template
context. It is never logged, never summarised, never written to a send-log
metadata field and never placed in an exception message. `summary()` and
`failure_code` are non-secret by construction.
"""

from __future__ import annotations

import html
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterator, Mapping, Optional

from jobs.ecodriving.job_eco_driving_aggregate import (
    _month_bounded_weekly_periods,
    _next_month_start,
    _period_for_cumulative_boundary,
    _previous_month_start,
)
from jobs.ecodriving_dashboard import publisher as pub
from jobs.ecodriving_dashboard import secure_delivery_client as sdc
from jobs.ecodriving_dashboard import sources
from jobs.ecodriving_dashboard.dashboard_email import (
    EmailConstructionError,
    build_capability_url,
)
from jobs.ecodriving_dashboard.dashboard_rollout import (
    DashboardMailingNotEnabled,
    DashboardMailingRollout,
    DashboardRolloutDeclarationError,
    require_dashboard_mailing_enabled,
)
from jobs.ecodriving_dashboard.delivery_contract import (
    DeliveryContractError,
    DeliveryIdentity,
    DeliveryState,
)
from jobs.ecodriving_dashboard.delivery_ledger import DeliveryLedger
from jobs.ecodriving_dashboard.job_eco_dashboard_snapshot import (
    _client_business_pg_conn,
    _dict_row_factory,
    _monthly_identity,
    _weekly_identity,
    build_delivery_snapshot_from_cursor,
)
from jobs.ecodriving_dashboard.publication import PrivacyContext, PublicationRefused
from jobs.ecodriving_dashboard.snapshot_builder import PeriodIdentity
from jobs.ecodriving_dashboard.snapshot_contract import (
    PERIOD_TYPE_MONTHLY,
    PERIOD_TYPE_WEEKLY,
    SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE,
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_REPORT_NOT_READY,
    SnapshotContractError,
)

# --- the template seam ---------------------------------------------------------

#: The two placeholders the existing Eco templates carry. Present in all four
#: template families so the dashboard has a defined position in every message,
#: and rendered as the empty string when the integration is off — which is what
#: keeps the templates valid for the deployments that have no dashboard yet.
#:
#: They are two POSITIONS for ONE capability, not two capabilities. The section
#: placeholder carries the titled `Twój Panel EcoDriving` block that sits
#: directly before `Podsumowanie`; the link placeholder carries the bare CTA
#: repeated after the final recommendation section. Both substitute the SAME
#: already-minted URL — see `dashboard_link_context`, which is the only place a
#: capability becomes template context and which builds both fragments from a
#: single argument precisely so a second one cannot be requested.
DASHBOARD_LINK_PLACEHOLDER = "eco_dashboard_link_html"
DASHBOARD_SECTION_PLACEHOLDER = "eco_dashboard_section_html"

#: Document order. Used only for reporting, so a template error names the
#: placeholders the way a reader of the template meets them.
DASHBOARD_PLACEHOLDERS = (DASHBOARD_SECTION_PLACEHOLDER, DASHBOARD_LINK_PLACEHOLDER)

#: Names the mailing lifecycle the ledger hands over to. Recorded on the ledger
#: row so an operator reading it knows where the send accounting lives.
MAILER_ECO_WEEKLY = "eco_driving_weekly_email_notifications"
MAILER_ECO_MONTHLY = "eco_driving_monthly_email_notifications"
MAILER_ECO_PERSON_WEEKLY = "eco_person_driving_weekly_email_notifications"
MAILER_ECO_PERSON_MONTHLY = "eco_person_driving_monthly_email_notifications"

ENV_DASHBOARD_BASE_URL = "ECO_DASHBOARD_BASE_URL"
ENV_PUBLISHER_ENDPOINT = "ECO_DASHBOARD_PUBLISHER_URL"
ENV_PUBLISHER_TOKEN = "ECO_DASHBOARD_PUBLISHER_TOKEN"

#: THE opt-in. `ops/runner.py --with-dashboard` sets this parameter, and nothing
#: else turns the integration on. See `DashboardLinkSettings.from_params`.
PARAM_WITH_DASHBOARD = "with_dashboard"

#: The pre-opt-in name for the same request, still accepted so a caller that
#: passes it explicitly keeps working. Like `with_dashboard` it must be given
#: explicitly; neither name has ever a default of "on".
PARAM_DASHBOARD_LINK = "dashboard_link"

#: Every snapshot status the contract defines is a dashboard a driver may open.
#: `INSUFFICIENT_DISTANCE` and `REPORT_NOT_READY` are PRODUCT states — the
#: dashboard renders them as itself — so they are published and linked exactly
#: like `OK`. Only a technical failure withholds a link.
PUBLISHABLE_SNAPSHOT_STATUSES = frozenset({
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE,
    SNAPSHOT_STATUS_REPORT_NOT_READY,
})

#: Shape-only placeholder, used by render-only runs so the link's POSITION and
#: escaping are provable without publishing anything. It is not a capability, no
#: grant is ever minted with it, and nothing built from it is transmitted.
_RENDER_ONLY_CAPABILITY = "0" * 43


#: The existing Eco jobs use four execution scopes; the delivery identity has
#: two. The mapping is a decision, not a coincidence:
#:
#:   * `forced` collapses to `normal` ON PURPOSE. A force-resend is the SAME
#:     logical delivery sent again, so it must converge on the SAME publication
#:     and the SAME capability. Treating it as its own scope would mint a second
#:     dashboard for one driver and one period — a different link in the second
#:     e-mail, and two live grants where the design allows one;
#:   * `test` stays separate, because a test send goes to a different mailbox
#:     and must never be bound to the driver's real delivery;
#:   * `render_only` HAS NO ENTRY, deliberately and permanently. It is not an
#:     omission to be repaired by adding `render_only -> normal` or
#:     `render_only -> test`: either would bind a rehearsal to a real driver's
#:     publication identity, and the rehearsal would then collide with — or
#:     silently stand in for — the delivery the real send must own. A
#:     render-only run publishes nothing, so it needs no delivery scope at all,
#:     and asking this mapping for one is a bug in the caller. The enforcement
#:     is that delivery-scope normalisation happens only where publication is
#:     actually possible; see `EcoDashboardLinkService.publication_send_scope`.
DELIVERY_SEND_SCOPE_BY_EXECUTION_SCOPE = {
    "normal": "normal",
    "forced": "normal",
    "test": "test",
}

#: The EXECUTION scope a render-only run arrives under — the value
#: `ExecutionContract.send_scope` yields for `execution_mode=render_only`, which
#: all four Eco jobs pass verbatim as `send_scope=execution.send_scope`. It is
#: an execution scope, never a delivery scope: it has no entry in the mapping
#: above and never acquires one.
RENDER_ONLY_SEND_SCOPE = "render_only"


def delivery_send_scope(execution_send_scope: str) -> str:
    try:
        return DELIVERY_SEND_SCOPE_BY_EXECUTION_SCOPE[str(execution_send_scope)]
    except KeyError:
        raise DeliveryContractError(
            f"no dashboard delivery scope for execution scope "
            f"{execution_send_scope!r}") from None


class LinkStatus:
    """What `link_for` established for one driver."""

    #: A real, published capability link for this driver and period.
    LINKED = "LINKED"
    #: The integration is not configured for this run. The e-mail is the
    #: pre-dashboard message and is sent normally.
    DISABLED = "DISABLED"
    #: The template this recipient gets shows no dashboard — the below-threshold
    #: message. Nothing was published and no capability was minted; the e-mail is
    #: sent normally. Distinct from `DISABLED`, which is about the RUN.
    NO_DASHBOARD_IN_TEMPLATE = "NO_DASHBOARD_IN_TEMPLATE"
    #: A render-only run: the message was built around a placeholder link and
    #: nothing was published.
    RENDER_ONLY = "RENDER_ONLY"
    #: A TECHNICAL failure. This driver's e-mail must not be sent.
    FAILED = "FAILED"


class DashboardIntegrationConfigurationError(RuntimeError):
    """The run asked for dashboard links and cannot produce them.

    Raised at construction, before any candidate is processed, so a run that
    was explicitly asked for links never sends a single link-less e-mail
    instead.
    """


class DashboardTemplateError(RuntimeError):
    """The template cannot carry a visible dashboard link.

    Raised BEFORE anything is published, so a template that could not show the
    link never causes a snapshot upload or a capability handoff.
    """


class DashboardPeriodMismatch(RuntimeError):
    """The period handed in is not one the Eco weekly/monthly model describes.

    Never resolved by substituting a nearby period. The dashboard must be about
    exactly the period the e-mail is about.
    """


@dataclass(frozen=True)
class DashboardLinkOutcome:
    """One driver's result. Nothing here is a secret except `capability_url`."""

    status: str
    capability_url: Optional[str] = None
    failure_code: Optional[str] = None
    detail: str = ""
    snapshot_status: Optional[str] = None
    delivery_state: Optional[str] = None
    operation_id: Optional[str] = None

    @property
    def blocks_send(self) -> bool:
        """True when this driver's Eco e-mail must NOT be sent."""
        return self.status == LinkStatus.FAILED

    @property
    def has_link(self) -> bool:
        return bool(self.capability_url)

    def audit(self) -> dict:
        """The non-secret record of what happened, safe for a send log."""
        return {
            "dashboard_link_status": self.status,
            "dashboard_snapshot_status": self.snapshot_status,
            "dashboard_delivery_state": self.delivery_state,
            "dashboard_operation_id": self.operation_id,
            "dashboard_failure_code": self.failure_code,
        }


# --- the authoritative period, verified rather than chosen ---------------------


def period_identities_for(
    period_type: str, period_start_date: date, period_end_date: date
) -> tuple[PeriodIdentity, Optional[PeriodIdentity]]:
    """Describe the period the Eco job ALREADY selected. Choose nothing.

    `period_end_date` is the exclusive boundary the Eco jobs persist, and
    `period_start_date` is the cumulative start they persist alongside it — the
    calendar month start for weekly, the month start for monthly.

    For weekly, the canonical month-bounded bucket list is consulted to learn
    which cumulative snapshot ends at that exact boundary, which is what
    supplies `period_sequence_in_month`, `is_partial_period` and the previous
    cumulative period used as the comparison basis. That is a LOOKUP of the
    period handed in, not a selection: the resulting cumulative start is
    compared against the one the job persisted and a disagreement raises.

    W1 = month-to-date through W1, W2 = W1+W2, W3 = W1+W2+W3, cut at month end.
    That model lives in `jobs/ecodriving/job_eco_driving_aggregate.py` and is
    read from there; it is never recomputed here.
    """
    if period_end_date <= period_start_date:
        raise DashboardPeriodMismatch("period_end_date must be after period_start_date")

    if period_type == PERIOD_TYPE_WEEKLY:
        try:
            period = _period_for_cumulative_boundary(period_end_date)
        except ValueError as error:
            raise DashboardPeriodMismatch(str(error)) from None
        if period.period_start_date != period_start_date:
            raise DashboardPeriodMismatch(
                "the persisted cumulative period start does not match the weekly "
                "period ending at the selected boundary"
            )
        month_periods = _month_bounded_weekly_periods(period.month_start_date)
        current = _weekly_identity(period, len(month_periods))
        previous = None
        if period.period_sequence_in_month > 1:
            previous = _weekly_identity(
                month_periods[period.period_sequence_in_month - 2], len(month_periods)
            )
        return current, previous

    if period_type == PERIOD_TYPE_MONTHLY:
        if period_start_date.day != 1 or _next_month_start(period_start_date) != period_end_date:
            raise DashboardPeriodMismatch(
                "the selected monthly period is not a whole calendar month"
            )
        previous_start = _previous_month_start(period_start_date)
        return (
            _monthly_identity(period_start_date, period_end_date),
            _monthly_identity(previous_start, period_start_date),
        )

    raise DashboardPeriodMismatch(f"unsupported period_type: {period_type!r}")


# --- the rendered fragment -----------------------------------------------------


#: The CTA label both dashboard entry points show. One constant, because the
#: two placements are the same button in two places.
DASHBOARD_CTA_LABEL = "Zobacz szczegóły swojej jazdy"

#: The titled block that sits directly before `Podsumowanie`.
DASHBOARD_SECTION_TITLE = "Twój Panel EcoDriving"
DASHBOARD_SECTION_DESCRIPTION = (
    "Przejrzyj szczegółową analizę Twoich tras oraz śledź Twoje postępy "
    "w Twoim EcoDriving Dashboard. Kliknij przycisk poniżej!"
)

#: Outlook desktop renders the VML button at a FIXED width, so the width has to
#: hold the label rather than shrink to it. The existing ALPHA programme button
#: is 260px for a ~146px label at 16px bold Arial; `DASHBOARD_CTA_LABEL` measures
#: ~240px in the same face, so the same treatment plus the same slack lands here.
#: Everything else — 48px height, 50% arcsize, both #1F8A4C colours, the 16px
#: bold centre — is copied unchanged from that button.
DASHBOARD_CTA_VML_WIDTH_PX = 360


def _dashboard_cta_html(safe_url: str) -> str:
    """The green pill CTA, in the SAME two variants the ALPHA programme CTA uses.

    An MSO conditional VML `v:roundrect` for Outlook desktop, which has no
    `border-radius`, and a downlevel-revealed `<a>` pill for every other client.
    The inline style on the anchor is character-for-character the one the
    existing `Dowiedz się więcej` button carries, so the two buttons are the
    same object visually and diverge only in label, width and destination.

    `safe_url` must already be attribute-escaped: it is placed in `href` twice,
    once per variant, and both are the SAME capability URL.
    """
    return (
        '<!--[if mso]>'
        '<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:w="urn:schemas-microsoft-com:office:word" '
        f'href="{safe_url}" '
        f'style="height:48px; v-text-anchor:middle; width:{DASHBOARD_CTA_VML_WIDTH_PX}px;" '
        'arcsize="50%" '
        'strokecolor="#1F8A4C" '
        'fillcolor="#1F8A4C">'
        '<w:anchorlock/>'
        '<center style="color:#FFFFFF; font-family:Arial, Helvetica, sans-serif; '
        f'font-size:16px; font-weight:bold;">{DASHBOARD_CTA_LABEL}</center>'
        '</v:roundrect>'
        '<![endif]-->'
        '<!--[if !mso]><!-- -->'
        f'<a href="{safe_url}" '
        'style="display:inline-block; background-color:#1F8A4C; color:#FFFFFF; '
        'text-decoration:none; font-size:16px; font-weight:bold; padding:15px 28px; '
        f'border-radius:999px;">{DASHBOARD_CTA_LABEL}</a>'
        '<!--<![endif]-->'
    )


#: The `Niezakwalifikowani` variants are the one family whose content cards are
#: rounded; the other 24 templates use square cards. The section block adopts
#: whichever the surrounding template uses so it does not read as imported from
#: a different design. Nothing else about the block varies.
CARD_RADIUS_ROUNDED = "18px"


def template_card_radius(template_html: str) -> str:
    """The card corner radius the template's own content cards use, or ``""``."""
    return (CARD_RADIUS_ROUNDED
            if f"border-radius:{CARD_RADIUS_ROUNDED}" in (template_html or "")
            else "")


def render_dashboard_section_block(capability_url: str, *, card_radius: str = "") -> str:
    """The `Twój Panel EcoDriving` section — title, description, CTA.

    A whole `<tr>` in the existing card language of these e-mails (the same
    white card, `#E3E8E5` border and 24px padding the `Podsumowanie` block
    uses), so that the empty string — what a run without the integration
    substitutes — leaves no empty row, gap or placeholder residue behind.

    Same escaping contract as `render_dashboard_link_block`, and the same URL.

    Centred exactly the way the standalone CTA below already is: the deprecated
    `align="center"` ATTRIBUTE on the cell, because Word — which renders Outlook
    desktop and therefore the VML variant — honours that and not every CSS
    alignment, plus `text-align:center` for the clients that ignore the
    attribute. The three children carry it explicitly so a client that resets
    inheritance still centres the heading, the description and the button.
    Presentation only: no colour, no padding, no button style and no URL moves.
    """
    safe_url = html.escape(str(capability_url), quote=True)
    radius = f" border-radius:{card_radius};" if card_radius else ""
    return (
        '<tr>'
        '<td style="padding:10px 32px 16px 32px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="background-color:#FFFFFF; border:1px solid #E3E8E5;{radius}">'
        '<tr>'
        '<td align="center" style="padding:24px; text-align:center;">'
        '<div style="font-size:18px; font-weight:bold; color:#1F2933; '
        'text-align:center;">'
        f'{DASHBOARD_SECTION_TITLE}'
        '</div>'
        '<div style="font-size:15px; line-height:1.7; color:#52616B; margin-top:10px; '
        'text-align:center;">'
        f'{DASHBOARD_SECTION_DESCRIPTION}'
        '</div>'
        '<div style="margin-top:20px; text-align:center;">'
        + _dashboard_cta_html(safe_url) +
        '</div>'
        '</td>'
        '</tr>'
        '</table>'
        '</td>'
        '</tr>'
    )


def render_dashboard_link_block(capability_url: str) -> str:
    """The second dashboard entry point: the same CTA, on its own, no heading.

    The URL is escaped with `quote=True` before it is placed in `href`, so a
    value that somehow contained a quote could not close the attribute. It is a
    whole `<tr>` so that the empty string — what a run without the integration
    substitutes — leaves no empty row behind in the table.

    No tracking pixel, no redirector, no click-through wrapper: the address in
    the message is the address the capability was minted for, and rewriting it
    would hand the bearer to whatever performed the rewrite. It is also the
    address the section block above already carries — one capability, shown
    twice, never re-minted.

    Already centred by the `align="center"` attribute Word needs; the matching
    `text-align:center` states the same intent for clients that ignore it.
    """
    safe_url = html.escape(str(capability_url), quote=True)
    return (
        '<tr>'
        '<td align="center" style="padding:8px 32px 30px 32px; text-align:center;">'
        + _dashboard_cta_html(safe_url) +
        '</td>'
        '</tr>'
    )


#: Regions of an HTML template in which a placeholder produces no visible link.
#: The scan below is deliberately a SCANNER and not a parser: the four Eco
#: template families are static, hand-maintained HTML e-mails rendered by a
#: `str`-substitution step, so the only question worth answering is "does this
#: exact placeholder sit in text content, or inside something that swallows
#: it?" — and that question is answerable with a linear pass.
_LINK_PLACEHOLDER_TOKEN = "{" + DASHBOARD_LINK_PLACEHOLDER + "}"
_SECTION_PLACEHOLDER_TOKEN = "{" + DASHBOARD_SECTION_PLACEHOLDER + "}"
_PLACEHOLDER_TOKENS = (_SECTION_PLACEHOLDER_TOKEN, _LINK_PLACEHOLDER_TOKEN)


class _Placement:
    TEXT = "TEXT"
    COMMENT = "HTML_COMMENT"
    TAG = "TAG"
    SCRIPT = "SCRIPT_OR_STYLE"
    UNTERMINATED = "UNTERMINATED_REGION"


_OPENS_RAW_TEXT = re.compile(r"<\s*(script|style)\b", re.IGNORECASE)


def _tag_end(text: str, start: int) -> int:
    """Index of the `>` that actually closes the tag opened at `start`, or -1.

    QUOTE-AWARENESS IS THE WHOLE POINT. Taking the first `>` after `<` treats

        <a title="> {eco_dashboard_link_html}">

    as a tag that ended at the `>` inside the attribute value, which leaves the
    placeholder looking like ordinary text content. It is not: it is inside an
    attribute, the substitution would inject an anchor element into a `title=`
    string, and nothing would ever render the link. The same holds for
    single-quoted values.

    A quote only opens an attribute value where the HTML tokenizer would let it:
    immediately after the `=` of an attribute (whitespace around the `=` is
    allowed). A quote character anywhere else is an ordinary character, so a
    stray apostrophe in an unquoted value cannot swallow the rest of the
    document and turn a perfectly valid template into a rejection.

    Deliberately still a scanner and not a parser. It answers exactly one
    question — where does this tag end — and it answers it the way a browser
    would for the shapes these hand-maintained templates actually contain.
    """
    quote = ""
    expects_value = False
    index = start + 1
    length = len(text)
    while index < length:
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char == ">":
            return index
        if char in ('"', "'") and expects_value:
            quote = char
            expects_value = False
            index += 1
            continue
        if char == "=":
            expects_value = True
        elif not char.isspace():
            expects_value = False
        index += 1
    return -1


def _placeholder_placements(template_html: str, token: str) -> list[str]:
    """Where each occurrence of the placeholder sits, in document order.

    One linear pass over the template, tracking the only four contexts that
    change the answer: ordinary text, an HTML comment, the inside of a tag, and
    the raw-text content of `<script>`/`<style>`. An unterminated comment or raw
    text element is reported as such rather than optimistically treated as
    text — "the rest of the file is inside something that never closed" is a
    malformed template, not a visible position.
    """
    placements: list[str] = []
    text = template_html or ""
    index = 0
    length = len(text)
    while index < length:
        if text.startswith(token, index):
            placements.append(_Placement.TEXT)
            index += len(token)
            continue
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            region = text[index:] if end == -1 else text[index:end + 3]
            if token in region:
                placements.extend(
                    [_Placement.UNTERMINATED if end == -1 else _Placement.COMMENT]
                    * region.count(token))
            index = length if end == -1 else end + 3
            continue
        raw = _OPENS_RAW_TEXT.match(text, index)
        if raw is not None:
            closing = re.compile(r"<\s*/\s*" + raw.group(1) + r"\s*>", re.IGNORECASE)
            found = closing.search(text, raw.end())
            region = text[index:] if found is None else text[index:found.end()]
            if token in region:
                placements.extend(
                    [_Placement.UNTERMINATED if found is None else _Placement.SCRIPT]
                    * region.count(token))
            index = length if found is None else found.end()
            continue
        if text[index] == "<":
            end = _tag_end(text, index)
            region = text[index:] if end == -1 else text[index:end + 1]
            if token in region:
                placements.extend(
                    [_Placement.UNTERMINATED if end == -1 else _Placement.TAG]
                    * region.count(token))
            index = length if end == -1 else end + 1
            continue
        index += 1
    return placements


def assert_link_placeholder_placement(template_html: str, *, source: str = "") -> None:
    """The template must be able to SHOW the dashboard, not merely contain the word.

    THE CASE THIS CLOSES. Checking that `{eco_dashboard_link_html}` appears
    somewhere in the template — or that the rendered URL appears somewhere in
    the output — accepts a placeholder sitting inside an HTML comment. The
    substitution then happens, the check passes, the snapshot is published, a
    capability is handed over and recorded, and the driver receives an e-mail
    whose dashboard link is inside `<!-- ... -->` where nothing will ever render
    it. Every effect fired; the one thing the integration exists for did not.

    So each insertion point is validated as a POSITION, and it is validated
    before the link is requested — before any publication, any capability, any
    ledger row. Accepted, per placeholder: exactly one occurrence, in text
    content. Rejected: a comment-only placement, a placement inside a tag or an
    attribute, one inside `<script>`/`<style>`, one stranded in an unterminated
    region, a duplicate (which would render the block twice), and an absent or
    malformed placeholder that the substitution step would never resolve.

    BOTH placeholders are required. The two dashboard entry points are one
    presentation contract: a template that carries only one of them would ship a
    message the design does not describe, and it would do so silently.
    """
    label = f" ({source})" if source else ""
    for token in _PLACEHOLDER_TOKENS:
        placements = _placeholder_placements(template_html or "", token)
        if not placements:
            raise DashboardTemplateError(
                f"the template carries no {token} insertion point{label}")
        unusable = sorted({p for p in placements if p != _Placement.TEXT})
        if unusable:
            raise DashboardTemplateError(
                f"the {token} insertion point is not in a renderable position"
                f"{label}: " + ", ".join(unusable))
        if len(placements) > 1:
            raise DashboardTemplateError(
                f"the {token} insertion point occurs {len(placements)} times"
                f"{label}; exactly one is supported")


#: The below-threshold templates, by basename — the message a recipient gets
#: when the period's distance did not reach the Eco qualifying distance, in all
#: four families (driver/person × weekly/monthly). They deliberately carry
#: NEITHER placeholder: that message offers no dashboard at all, so there is no
#: position for one and no capability is minted for it.
#:
#: This is a statement about the TEMPLATES, not a second eligibility rule. The
#: existing selection logic decides which file a recipient gets; this set only
#: records which of those files are expected to be dashboard-free, so preflight
#: can tell "intentionally has no dashboard" from "lost its placeholder".
TEMPLATES_WITHOUT_DASHBOARD = frozenset({
    "Tygodniowe - Niezakwalifikowani.html",
    "Miesięczne - Niezakwalifikowani.html",
})


def template_carries_dashboard(template_html: str) -> bool:
    """True when the template has a position for the dashboard at all.

    Either placeholder is enough to answer yes. A template carrying only one of
    the two is NOT dashboard-free — it is a broken dashboard template, and
    `assert_link_placeholder_placement` is what must reject it. Answering yes
    here is precisely what routes it there.
    """
    text = template_html or ""
    return any(token in text for token in _PLACEHOLDER_TOKENS)


def assert_no_dashboard_placeholders(template_html: str, *, source: str = "") -> None:
    """A template declared dashboard-free must actually carry no insertion point.

    The mirror of `assert_link_placeholder_placement`, and the reason removing
    the CTA from the below-threshold templates does not silently weaken the
    placement guard: every template a run may use is still asserted against what
    it is supposed to be, one way or the other.
    """
    label = f" ({source})" if source else ""
    present = [token for token in _PLACEHOLDER_TOKENS if token in (template_html or "")]
    if present:
        raise DashboardTemplateError(
            f"the template shows no dashboard but still carries "
            + ", ".join(present) + label)


def validate_template_dir_link_placeholders(template_dir, filenames) -> None:
    """Every template a run may use matches its dashboard contract — BEFORE the run.

    Called by the Eco jobs once, next to the existing template-inventory check,
    so a deployment whose templates cannot carry the link stops before the first
    candidate rather than failing every driver one at a time.

    Two contracts, one pass: a dashboard template must be able to SHOW the link;
    a template named in `TEMPLATES_WITHOUT_DASHBOARD` must carry no insertion
    point at all.
    """
    for filename in filenames:
        path = template_dir / filename
        if not path.exists():  # the inventory check owns this failure
            continue
        template_html = path.read_text(encoding="utf-8")
        if os.path.basename(str(filename)) in TEMPLATES_WITHOUT_DASHBOARD:
            assert_no_dashboard_placeholders(template_html, source=str(filename))
            continue
        assert_link_placeholder_placement(template_html, source=str(filename))


def dashboard_link_context(capability_url: Optional[str], *,
                           card_radius: str = "") -> dict[str, str]:
    """The template context contribution. Empty strings when there is no link.

    BOTH keys are ALWAYS present. A template placeholder with no context entry
    survives rendering and trips the job's unresolved-placeholder guard, so
    supplying the keys unconditionally is what keeps a deployment without the
    dashboard rendering exactly as it does today — and, because both fragments
    are whole `<tr>` elements, the empty case leaves no row, gap or residue.

    ONE ARGUMENT, TWO FRAGMENTS. This is the only place a capability becomes
    template context, and it takes a single URL. The two entry points therefore
    cannot disagree, and showing the dashboard twice cannot become publishing
    twice: nothing here can request, rotate or mint a capability.
    """
    if not capability_url:
        return {DASHBOARD_SECTION_PLACEHOLDER: "", DASHBOARD_LINK_PLACEHOLDER: ""}
    return {
        DASHBOARD_SECTION_PLACEHOLDER: render_dashboard_section_block(
            capability_url, card_radius=card_radius),
        DASHBOARD_LINK_PLACEHOLDER: render_dashboard_link_block(capability_url),
    }


def _setting(params: Mapping[str, Any], key: str, env_name: str) -> str:
    return str(params.get(key) or os.getenv(env_name) or "").strip()


def _optional_bool(params: Mapping[str, Any], key: str) -> Optional[bool]:
    if key not in params or params[key] in (None, ""):
        return None
    value = params[key]
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"true", "1", "yes", "y", "on"}:
        return True
    if lowered in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")


@dataclass(frozen=True)
class DashboardLinkSettings:
    enabled: bool
    dashboard_base_url: str = ""
    publisher_endpoint: str = ""
    publisher_token: str = ""
    render_only: bool = False

    @classmethod
    def from_params(cls, params: Mapping[str, Any], *, render_only: bool) -> "DashboardLinkSettings":
        """OPT-IN ONLY. Absent opt-in is the legacy mailing path, unconditionally.

        THE CONTRACT THIS STATES. Dashboard mailing is off unless the invocation
        asked for it with `--with-dashboard` (`with_dashboard=true`, or its
        `dashboard_link=true` alias). It is NOT turned on by the deployment
        happening to have `ECO_DASHBOARD_BASE_URL` / `ECO_DASHBOARD_PUBLISHER_URL`
        configured, and provisioning a publisher is therefore never a silent
        rollout.

        WHY THE ENVIRONMENT IS NOT EVEN READ WITHOUT THE OPT-IN. Returning early
        is not a micro-optimisation, it is the backward-compatibility contract:
        a default run must have no dependency whatsoever on publisher
        configuration or availability. Nothing below this branch runs, so no
        endpoint is validated, no configuration can be missing, and a broken or
        absent publisher configuration cannot change whether the ordinary e-mail
        is sent.

        WHEN THE OPT-IN IS PRESENT the integration becomes a REQUIREMENT of the
        run: a missing endpoint stops the whole run before a single e-mail is
        sent, instead of quietly degrading to the pre-dashboard message.

        This settles only half of the decision. Dashboard-enabled sending also
        needs client-level rollout permission — see
        `authorize_dashboard_mailing`, which the four Eco jobs call before they
        construct anything.
        """
        requested = _optional_bool(params, PARAM_WITH_DASHBOARD)
        if requested is None:
            requested = _optional_bool(params, PARAM_DASHBOARD_LINK)
        if requested is not True:
            return cls(enabled=False)

        base_url = _setting(params, "dashboard_base_url", ENV_DASHBOARD_BASE_URL)
        endpoint = _setting(params, "publisher_endpoint", ENV_PUBLISHER_ENDPOINT)
        token = _setting(params, "publisher_token", ENV_PUBLISHER_TOKEN)

        if render_only:
            # Nothing remote is contacted, so only the destination has to be
            # usable; a render-only run proves the link's position and escaping.
            if not base_url:
                raise DashboardIntegrationConfigurationError(
                    f"--with-dashboard requires {ENV_DASHBOARD_BASE_URL}")
            return cls(enabled=True, dashboard_base_url=base_url, render_only=True)

        missing = [name for name, value in (
            (ENV_DASHBOARD_BASE_URL, base_url),
            (ENV_PUBLISHER_ENDPOINT, endpoint),
            (ENV_PUBLISHER_TOKEN, token),
        ) if not value]
        if missing:
            raise DashboardIntegrationConfigurationError(
                "--with-dashboard was given but these are not configured: "
                + ", ".join(missing))
        try:
            sdc.validate_publisher_endpoint(endpoint)
        except sdc.SecureDeliveryError as error:
            raise DashboardIntegrationConfigurationError(
                f"{ENV_PUBLISHER_ENDPOINT} is not usable: {error.code}") from None
        return cls(enabled=True, dashboard_base_url=base_url,
                   publisher_endpoint=endpoint, publisher_token=token)


def authorize_dashboard_mailing(
    settings: "DashboardLinkSettings", *,
    client_code: Any,
    rollout: Optional[DashboardMailingRollout] = None,
) -> Optional[str]:
    """THE second half of the two-condition invariant, checked before anything.

    Dashboard-enabled sending requires BOTH an explicit `--with-dashboard`
    invocation (already settled in `settings.enabled`) AND explicit client-level
    rollout permission (`ops/eco_dashboard_mailing_rollout.json`). Neither
    condition alone is sufficient, and this is where the second one is enforced.

    ORDER IS THE POINT. The four Eco jobs call this immediately after the client
    account is resolved and BEFORE they construct `EcoDashboardLinkService`,
    before the candidate loop, and therefore before the first snapshot build,
    the first publication, the first capability, the first send-log reservation
    and the first SMTP connection. A client whose rollout is not enabled costs
    none of them to discover.

    It DOES NOT fall back to a dashboard-less e-mail. A run that was explicitly
    asked for dashboard mailing and may not do it stops, rather than silently
    sending something other than what was asked for.

    Returns the normalised client code when dashboard mailing is authorised, and
    `None` when the run never asked for it — in which case this is a pure no-op
    that reads no declaration at all, so a legacy run cannot be affected by the
    declaration's presence, absence or contents.

    Raises `DashboardMailingNotEnabled` when the client is not enabled, and
    `DashboardRolloutDeclarationError` when the declaration itself cannot be
    trusted. Both are refusals; neither can produce a send.
    """
    if not settings.enabled:
        return None
    return require_dashboard_mailing_enabled(client_code, rollout=rollout)


@dataclass
class EcoDashboardLinkService:
    """Per-run dashboard link production for one client, period and send scope.

    Constructed once by the Eco job, then asked for one link per candidate. It
    holds the per-run facts that must not be recomputed per driver: the pipeline
    family, the schema, the verified period identities, the publisher client,
    the ledger connection and the population-statistics cache.
    """

    settings: DashboardLinkSettings
    client_id: str
    client_code: str
    schema: str
    period_type: str
    period_start_date: date
    period_end_date: date
    send_scope: str
    mailer: str
    read_conn: Any
    cfg: Any = None
    run_id: Optional[str] = None
    logger: Optional[Callable[[str, str, Mapping[str, Any]], None]] = None

    family: Any = field(init=False, default=None)
    current_identity: PeriodIdentity = field(init=False, default=None)
    previous_identity: Optional[PeriodIdentity] = field(init=False, default=None)
    _distribution_cache: dict = field(init=False, default_factory=dict)
    _ledger_conn: Any = field(init=False, default=None)
    _services: Any = field(init=False, default=None)
    _owner: str = field(init=False, default="")
    counters: dict = field(init=False, default_factory=lambda: {
        "dashboard_linked_count": 0,
        "dashboard_render_only_count": 0,
        "dashboard_failed_count": 0,
        "dashboard_snapshot_status_counts": {},
        "dashboard_failure_codes": {},
        "dashboard_ledger_connections_opened": 0,
    })
    #: Expired-capability housekeeping, deliberately NOT part of `counters`.
    #:
    #: `counters` is merged into `summary()`, and all four Eco jobs call
    #: `summary()` immediately BEFORE `close()` — which is where the sweep runs,
    #: because that is the only boundary reached whether or not the run
    #: published anything. Housekeeping numbers put in `counters` would
    #: therefore be read one moment too early and reported as zero on every run,
    #: for ever. They are reported through the `eco_dashboard_capabilities_retired`
    #: log event instead, which is emitted from `close()` and does reach the
    #: job's logger; this dict is the same facts for tests and for callers that
    #: ask after closing.
    maintenance: dict = field(init=False, default_factory=lambda: {
        "capabilities_retired": 0,
        "expired_sessions_removed": 0,
        "ledger_connections_opened": 0,
        "failure_code": None,
        "ran": False,
    })

    def __post_init__(self) -> None:
        if not self.settings.enabled:
            return
        # `send_scope` arrives as the Eco job's EXECUTION scope
        # (`execution.send_scope`), and the four execution scopes do not all
        # name a delivery. Delivery-scope normalisation is therefore performed
        # only where publication is actually possible: a render-only run keeps
        # its execution scope verbatim — required exactly, so a rehearsal can
        # never be constructed under `normal`/`test`/`forced` and can never
        # borrow a real delivery's publication identity — while a delivery run
        # must resolve to a real delivery scope or refuse at construction, as
        # before. Normalising eagerly for every scope made `--with-dashboard` +
        # `execution_mode=render_only` raise `DeliveryContractError` during
        # construction, for every client and all four Eco mailing families,
        # which was the one path the opt-in exists to make safe.
        if self.settings.render_only:
            if self.send_scope != RENDER_ONLY_SEND_SCOPE:
                raise DeliveryContractError(
                    "a render-only run must carry the render-only execution "
                    f"scope {RENDER_ONLY_SEND_SCOPE!r}, not "
                    f"{self.send_scope!r}: a rehearsal never holds a delivery "
                    "send scope")
        elif self.send_scope not in ("normal", "test"):
            self.send_scope = delivery_send_scope(self.send_scope)
        self.family = sources.resolve_pipeline_family(self.client_code)
        self.current_identity, self.previous_identity = period_identities_for(
            self.period_type, self.period_start_date, self.period_end_date)
        self._owner = pub.owner_token(self.run_id)

    def publication_send_scope(self) -> str:
        """The delivery scope this run publishes under, or a refusal.

        The ONLY producer of a delivery send scope, reachable only from the
        publishing path. A render-only run has no delivery and must never
        acquire one: rather than fall back to `normal` or `test` — which would
        hand a rehearsal a real driver's publication identity — it raises, so a
        future caller that reintroduces eager normalisation fails loudly here
        instead of silently publishing under a borrowed scope.
        """
        if self.settings.render_only:
            raise DeliveryContractError(
                "a render-only run publishes nothing and has no delivery send "
                "scope; this call is reachable only from the publishing path")
        return delivery_send_scope(self.send_scope)

    # --- lifecycle ------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    def _log(self, level: str, event: str, context: Mapping[str, Any]) -> None:
        if self.logger is not None:
            self.logger(level, event, dict(context))

    def _publisher_services(self):
        """One ledger connection and one publisher client for the WHOLE run.

        Opened lazily, on the first candidate that actually needs to publish, so
        a run whose every candidate is skipped opens nothing. The ledger demands
        autocommit — "durable before the remote call" is a statement about this
        call, not about a commit the Eco job makes later — which is why it
        cannot share the job's transactional connection and why it must not be
        opened per driver either.
        """
        if self._services is not None:
            return self._services
        # Autocommit is requested at CONSTRUCTION. Setting it afterwards is
        # not merely untidy, it is impossible: the helper configures the
        # business timezone with a statement, so the session is already
        # INTRANS by the time it returns and psycopg refuses the mode change.
        self._ledger_conn = _client_business_pg_conn(self.cfg, autocommit=True)
        self.counters["dashboard_ledger_connections_opened"] += 1
        self._services = pub.PublisherServices(
            ledger=DeliveryLedger(self._ledger_conn),
            client=sdc.SecureDeliveryClient(
                sdc.HttpSecureDeliveryTransport(self.settings.publisher_endpoint),
                publisher_token=self.settings.publisher_token),
            config=pub.PublisherConfig(
                dashboard_base_url=self.settings.dashboard_base_url,
                # No message id is ever built on this path: the existing Eco
                # mailer owns the outbound message and its identity.
                message_id_domain="eco-dashboard.invalid"),
            logger=self.logger,
        )
        return self._services

    #: Names the subtransaction each per-driver snapshot read runs inside. A
    #: fixed identifier is enough because the scope is strictly nested and never
    #: concurrent: the Eco jobs process one candidate at a time on one
    #: connection.
    _SNAPSHOT_SAVEPOINT = "eco_dashboard_snapshot"

    @contextmanager
    def _snapshot_cursor(self) -> Iterator[Any]:
        """A cursor whose SQL failures cannot poison the Eco job's transaction.

        THE CASE THIS CLOSES. The snapshot reads run on a cursor lent by the Eco
        job's own client-business connection — deliberately, because a
        connection per candidate would turn one client run into hundreds of
        sessions. But PostgreSQL aborts the WHOLE transaction on any statement
        error, so catching one driver's database failure as a per-driver
        dashboard failure left the shared transaction in `25P02`: the send-log
        insert for that driver, and every subsequent driver's query, would fail
        with "current transaction is aborted" and the per-driver isolation the
        run depends on would be isolation in name only.

        A SUBTRANSACTION is the smallest mechanism that fixes it without
        changing anything else: the reads stay on the job's connection (no
        second session, no connection storm, no separate client isolation), and
        `ROLLBACK TO SAVEPOINT` — which is legal precisely in an aborted
        transaction — returns the session to exactly the state the job left it
        in. Nothing the Eco job did before this call is rolled back, because a
        savepoint releases or rewinds only what happened inside it, and this
        block writes nothing at all.

        A connection already in autocommit needs none of this: there is no
        surrounding transaction to poison, and issuing `SAVEPOINT` outside a
        transaction block is itself an error.
        """
        conn = self.read_conn
        with conn.cursor(row_factory=_dict_row_factory()) as cur:
            if bool(getattr(conn, "autocommit", False)):
                yield cur
                return
            name = self._SNAPSHOT_SAVEPOINT
            cur.execute(f"SAVEPOINT {name}")
            try:
                yield cur
            except BaseException:
                # The savepoint is rewound and then released, so the session is
                # usable again AND no subtransaction is left open on it.
                cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
                cur.execute(f"RELEASE SAVEPOINT {name}")
                raise
            cur.execute(f"RELEASE SAVEPOINT {name}")

    #: How many expired deliveries one run may retire. Several batches per run
    #: so a first activation over an existing backlog converges quickly, but
    #: bounded so housekeeping can never dominate a mailing run.
    RETIREMENT_BATCHES_PER_RUN = 5

    def _maintenance_ledger(self):
        """A ledger for housekeeping, WITHOUT requiring a publisher client.

        `_publisher_services()` builds a `SecureDeliveryClient`, which refuses
        to exist without a machine credential — so reusing it here would make
        the destruction of expired secret material depend on publisher
        configuration that housekeeping never uses. The sweep talks only to this
        client's own business database.

        Reuses the run's existing ledger connection when the run published
        something, and otherwise opens one for the sweep alone. That open is
        counted separately from `dashboard_ledger_connections_opened`, which
        means "the publish path needed a connection" and is asserted with that
        meaning elsewhere.
        """
        if self._services is not None:
            return self._services.ledger
        if self._ledger_conn is None:
            self._ledger_conn = _client_business_pg_conn(self.cfg, autocommit=True)
            self.maintenance["ledger_connections_opened"] += 1
        return DeliveryLedger(self._ledger_conn)

    def retire_expired_capabilities(self) -> None:
        """Destroy the host's copy of every bearer whose grant has expired.

        WHERE THIS RUNS, AND WHY HERE. This is the ordinary end of a weekly or
        monthly Eco mailing run — the same lifecycle that creates dashboard
        capabilities in the first place, on the same client's ledger, on the
        connection it already holds. That is the point: expiry must be cleaned
        up by the system's normal operation, not by somebody re-mailing the
        exact historical period whose link went stale. With weekly grants living
        10 days and the weekly job running weekly, every expired delivery is
        reached within days of its expiry without anyone scheduling anything.

        IT DOES NOT DEPEND ON THIS RUN HAVING PUBLISHED ANYTHING. An earlier
        version ran only when a ledger connection already existed — that is,
        only when some candidate had reached the publisher — which quietly made
        the guarantee conditional on the wrong thing. A weekly run whose drivers
        were all already sent, whose roster is empty, whose snapshots were all
        refused, or that was simply filtered down to nothing never reaches the
        publisher, and under that version never swept: a bearer that expired
        months ago would have survived every one of those runs and been
        destroyed only if somebody happened to re-mail that exact historical
        period. Retirement now runs on the maintenance boundary itself, and
        opens its own ledger connection when the run has none.

        It also does not depend on the PUBLISHER being usable. `_maintenance_ledger`
        deliberately avoids `_publisher_services()`, so a missing or broken
        machine credential cannot stop expired secret material being destroyed
        in this client's own database.

        FAILURE IS CONTAINED, ALWAYS. Housekeeping must never cost a driver an
        e-mail. Every failure is recorded as a counter and swallowed: the run's
        real work is already done by the time this is called, the state it
        would have cleaned is still exactly as safe as it was, and the next run
        tries again.
        """
        self.maintenance["ran"] = True
        try:
            ledger = self._maintenance_ledger()
            retired = 0
            for _ in range(self.RETIREMENT_BATCHES_PER_RUN):
                batch = ledger.retire_expired_capabilities(run_id=self.run_id)
                retired += len(batch)
                if len(batch) < DeliveryLedger.RETIREMENT_BATCH:
                    # Fewer rows than the batch allows means the expired set is
                    # exhausted. Stopping here keeps the common case at exactly
                    # one statement.
                    break
            self.maintenance["capabilities_retired"] = retired
            if retired:
                self._log("INFO", "eco_dashboard_capabilities_retired",
                          {"retired": retired, "client_id": self.client_id})
        except Exception as error:  # noqa: BLE001
            self.maintenance["failure_code"] = type(error).__name__
            self._log("WARNING", "eco_dashboard_capability_retirement_failed",
                      {"code": type(error).__name__, "client_id": self.client_id})

        # The Worker's half: expired browser sessions. Attempted only when this
        # run already holds a publisher client, because a session exists only
        # where a driver opened a link — which means that client publishes — and
        # because building a client purely to compact sessions would put the
        # host-side bearer destruction above behind publisher configuration it
        # does not need. It removes no grant and can revoke nothing, so a
        # failure here is pure housekeeping debt.
        if self._services is None:
            return
        try:
            compacted = self._services.client.compact_authorization_state()
            self.maintenance["expired_sessions_removed"] = int(
                compacted.get("expired_sessions_removed") or 0)
        except Exception as error:  # noqa: BLE001
            self.maintenance["failure_code"] = type(error).__name__
            self._log("WARNING", "eco_dashboard_session_compaction_failed",
                      {"code": type(error).__name__})

    def close(self) -> None:
        """End of the run, and THE maintenance boundary.

        All four Eco mailing jobs call this from a `finally`, so it is reached
        on every dashboard-enabled invocation — one that mailed a fleet, one
        that found no candidate at all, and one that raised. That is exactly why
        the expiry sweep lives here and not on the publish path: the guarantee
        is that expired bearer material is destroyed by the normal lifecycle,
        not by somebody re-mailing the period it belonged to.

        A RENDER-ONLY run is excluded, and that is not an exception to the rule.
        A rehearsal has no delivery identity, publishes nothing and must open no
        ledger connection at all; sweeping from one would make a dry run mutate
        the durable state it exists to avoid touching.
        """
        if self.enabled and not self.settings.render_only:
            # Runs BEFORE the close below so it can use a connection this run
            # already owns, and AFTER every per-driver decision so it can never
            # influence one. Contains its own failures.
            self.retire_expired_capabilities()
        if self._ledger_conn is not None:
            try:
                self._ledger_conn.close()
            finally:
                self._ledger_conn = None
                self._services = None

    def __enter__(self) -> "EcoDashboardLinkService":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def summary(self) -> dict:
        base = {
            "dashboard_link_enabled": self.enabled,
            "dashboard_link_render_only": bool(self.settings.render_only),
            "dashboard_period_label": (
                self.current_identity.period_label if self.current_identity else None),
        }
        base.update(self.counters)
        return base

    # --- one driver -----------------------------------------------------------

    def template_placement_invalid(self, detail: str) -> DashboardLinkOutcome:
        """The template cannot show a link, established BEFORE anything published.

        Counted as a technical failure like any other, so this driver's e-mail
        is not sent — but no snapshot was uploaded, no capability was minted and
        no ledger row was touched to find that out.
        """
        return self._failed("DASHBOARD_LINK_PLACEHOLDER_INVALID", detail)

    def template_link_missing(self, outcome: DashboardLinkOutcome) -> DashboardLinkOutcome:
        """A link was produced but the rendered message does not carry it.

        Counted and reported as a technical failure like any other, because the
        driver would otherwise receive an e-mail that silently lost the thing
        this integration exists to deliver.
        """
        return self._failed(
            "DASHBOARD_LINK_NOT_IN_TEMPLATE",
            "the rendered template does not contain the dashboard link",
            snapshot_status=outcome.snapshot_status,
            delivery_state=outcome.delivery_state,
            operation_id=outcome.operation_id,
        )

    def link_for(self, *, identity_key: str, recipient_email: str) -> DashboardLinkOutcome:
        """Produce this driver's dashboard link, or refuse to produce one.

        Never raises for a per-driver problem: an exception here would abort the
        whole fleet run for one driver's data. Every technical failure comes
        back as `FAILED`, and the caller isolates it exactly the way it already
        isolates a failed render or a failed send.
        """
        if not self.enabled:
            return DashboardLinkOutcome(LinkStatus.DISABLED)
        try:
            return self._link_for(identity_key=identity_key,
                                  recipient_email=recipient_email)
        except Exception as error:  # noqa: BLE001 - per-driver isolation is the contract
            return self._failed("DASHBOARD_INTEGRATION_ERROR", type(error).__name__)

    def _failed(self, code: str, detail: str, *,
                snapshot_status: Optional[str] = None,
                delivery_state: Optional[str] = None,
                operation_id: Optional[str] = None) -> DashboardLinkOutcome:
        self.counters["dashboard_failed_count"] += 1
        codes = self.counters["dashboard_failure_codes"]
        codes[code] = codes.get(code, 0) + 1
        return DashboardLinkOutcome(
            LinkStatus.FAILED, failure_code=code, detail=detail,
            snapshot_status=snapshot_status, delivery_state=delivery_state,
            operation_id=operation_id)

    def _link_for(self, *, identity_key: str, recipient_email: str) -> DashboardLinkOutcome:
        identity_key = str(identity_key or "").strip()
        if not identity_key:
            return self._failed("DASHBOARD_IDENTITY_KEY_MISSING", "no driver identity key")
        recipient = str(recipient_email or "").strip()
        if not recipient:
            return self._failed("DASHBOARD_RECIPIENT_MISSING", "no recipient address")

        privacy = PrivacyContext(
            identity_key=identity_key,
            client_code=self.client_code,
            # Declared so the value-level sweep would catch the address if it
            # ever appeared in the payload. It is never written into it.
            email_addresses=(recipient,),
        )
        try:
            with self._snapshot_cursor() as cur:
                built = build_delivery_snapshot_from_cursor(
                    cur,
                    family=self.family,
                    schema=self.schema,
                    client_id=self.client_id,
                    identity_key=identity_key,
                    period_type=self.period_type,
                    current_identity=self.current_identity,
                    previous_identity=self.previous_identity,
                    client_code=self.client_code,
                    privacy=privacy,
                    distribution_cache=self._distribution_cache,
                )
        except (PublicationRefused, SnapshotContractError) as error:
            # The publication gate refused the document. Its message names the
            # assertion, never the offending value.
            return self._failed("DASHBOARD_SNAPSHOT_REFUSED",
                                getattr(error, "assertion", type(error).__name__))
        except Exception as error:  # noqa: BLE001
            return self._failed("DASHBOARD_SNAPSHOT_FAILED", type(error).__name__)

        if built is None:
            # The Eco job is mailing about a stats row the dashboard source
            # cannot find for the same driver and period. That is an
            # inconsistency, not a product state.
            return self._failed("DASHBOARD_SNAPSHOT_UNAVAILABLE",
                                "no stats row for the selected period")

        status = built.snapshot_status
        counts = self.counters["dashboard_snapshot_status_counts"]
        counts[status] = counts.get(status, 0) + 1
        if status not in PUBLISHABLE_SNAPSHOT_STATUSES:  # pragma: no cover - defensive
            return self._failed("DASHBOARD_SNAPSHOT_STATUS_UNKNOWN", str(status),
                                snapshot_status=status)

        if self.settings.render_only:
            # Nothing is published and no ledger row is created, so NO DELIVERY
            # IDENTITY IS DERIVED EITHER — this returns before the construction
            # below. That ordering is the contract, not a convenience: a
            # `DeliveryIdentity` is the publication's identity, and minting one
            # for a rehearsal would put a render-only run inside the identity
            # model that decides which real send owns which capability.
            #
            # The real snapshot above was still built from the real source
            # data, so the rehearsal proves what it exists to prove. The
            # placeholder proves the link's position and escaping only.
            try:
                url = build_capability_url(self.settings.dashboard_base_url,
                                           _RENDER_ONLY_CAPABILITY)
            except EmailConstructionError as error:
                return self._failed("DASHBOARD_BASE_URL_UNUSABLE", str(error),
                                    snapshot_status=status)
            self.counters["dashboard_render_only_count"] += 1
            return DashboardLinkOutcome(LinkStatus.RENDER_ONLY, capability_url=url,
                                        snapshot_status=status)

        try:
            identity = DeliveryIdentity(
                client_id=self.client_id,
                identity_key=identity_key,
                period_type=self.period_type,
                period_start_date=built.current_identity.period_start_date,
                period_end_date=built.current_identity.period_end_date_exclusive,
                send_scope=self.publication_send_scope(),
            )
        except DeliveryContractError as error:
            return self._failed("DASHBOARD_DELIVERY_IDENTITY_INVALID", str(error),
                                snapshot_status=status)

        outcome = pub.ensure_capability(
            self._publisher_services(),
            identity=identity,
            payload_digest=built.payload_digest,
            recipient_email=recipient,
            body=built.payload,
            owner=self._owner,
            mailer=self.mailer,
            run_id=self.run_id,
        )
        if not outcome.has_link:
            return self._failed(
                str(outcome.conflict_code or outcome.invocation),
                str(outcome.detail or outcome.invocation),
                snapshot_status=status,
                delivery_state=outcome.state,
                operation_id=(outcome.record.operation_id if outcome.record else None),
            )
        self.counters["dashboard_linked_count"] += 1
        return DashboardLinkOutcome(
            LinkStatus.LINKED,
            capability_url=outcome.capability_url,
            snapshot_status=status,
            delivery_state=outcome.state,
            operation_id=outcome.record.operation_id if outcome.record else None,
        )


def render_with_dashboard_link(
    service: Optional["EcoDashboardLinkService"],
    *,
    identity_key: str,
    recipient_email: str,
    context: dict,
    template_html: str,
    render: Callable[[dict], str],
) -> tuple[Optional[str], DashboardLinkOutcome]:
    """The whole per-candidate step, in one place for all four Eco jobs.

    Returns `(html_body, outcome)`. `html_body` is `None` exactly when this
    driver's e-mail must not be sent — the caller records the failure through
    its own run accounting and moves to the next driver.

    ORDER MATTERS, IN BOTH DIRECTIONS.

    The template's insertion point is validated FIRST, before the link is even
    requested, because everything the request causes is an effect: a snapshot
    upload, a minted capability, a durable handoff. A template that could not
    show the link must cost none of them.

    The link is then obtained BEFORE the message is rendered, because the link
    is part of the message; and the rendered message is checked AFTER, because
    a template that dropped the placeholder between validation and rendering —
    a different file, a variant substitution — is only observable there. The
    two checks answer different questions and neither replaces the other: the
    first is about POSITION, the second about PRESENCE.

    A `None` service, or a disabled one, contributes an empty fragment: the
    template renders exactly as it did before the dashboard existed, and the
    placement rule does not apply because no link is being delivered.

    A template with NO insertion point at all is the below-threshold message,
    which shows no dashboard by design. It is answered first, and answered the
    same way whether or not the run has the integration on, so that message
    costs no publication, no capability and no ledger row. It is not a template
    failure and it never blocks the send. A template carrying only one of the
    two placeholders is NOT this case: `template_carries_dashboard` says yes and
    the placement assertion below rejects it, exactly as before.
    """
    if not template_carries_dashboard(template_html):
        context.update(dashboard_link_context(None))
        return render(context), DashboardLinkOutcome(LinkStatus.NO_DASHBOARD_IN_TEMPLATE)

    if service is None or not service.enabled:
        context.update(dashboard_link_context(None))
        return render(context), DashboardLinkOutcome(LinkStatus.DISABLED)

    try:
        assert_link_placeholder_placement(template_html)
    except DashboardTemplateError as error:
        return None, service.template_placement_invalid(str(error))

    outcome = service.link_for(identity_key=identity_key, recipient_email=recipient_email)
    if outcome.blocks_send:
        return None, outcome
    context.update(dashboard_link_context(
        outcome.capability_url, card_radius=template_card_radius(template_html)))
    html_body = render(context)
    if not link_is_present(html_body, outcome):
        return None, service.template_link_missing(outcome)
    return html_body, outcome


def link_is_present(html_body: str, outcome: DashboardLinkOutcome) -> bool:
    """Prove the rendered message actually carries the link it was given.

    THE CASE THIS CLOSES. The link reaches the message through a template
    placeholder. A template that lost the placeholder — an edited copy, a
    `template_dir` override, a family that was never updated — renders a
    perfectly valid Eco e-mail with no dashboard link in it, and nothing else
    in the pipeline would notice. Checking the rendered output is the only place
    that fact is observable, so a link that was produced but did not land is a
    technical failure and the message is not sent.
    """
    if not outcome.has_link:
        return True
    return html.escape(str(outcome.capability_url), quote=True) in html_body


#: Delivery states a handed-over link may legitimately be read from. Exported so
#: the integration's expectation is stated once and asserted in tests rather
#: than implied by control flow.
LINKABLE_DELIVERY_STATES = frozenset({DeliveryState.EXTERNAL_MAILER_HANDOFF})
