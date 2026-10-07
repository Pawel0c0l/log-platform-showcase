#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — integration with the EXISTING Eco Driving mailings.

Run:
    python3 ops/tests_manual/test_eco_mailing_dashboard_link_integration.py

    # and, for the ledger-backed half, against a DISPOSABLE local instance:
    ECO_DASHBOARD_ECO_MAILING_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5439/disposable \\
      python3 ops/tests_manual/test_eco_mailing_dashboard_link_integration.py

WHAT THIS SUITE IS ABOUT

The four existing weekly/monthly Eco e-mail jobs now carry a per-driver
dashboard link. Everything that could go wrong with that is a property of the
SEAM, not of the snapshot contract (covered by
`test_eco_dashboard_snapshot.py`), the Worker (covered by
`test_driver_eco_dashboard_delivery.py`) or the provider lifecycle (covered by
`test_driver_eco_dashboard_publisher_lifecycle.py`). This file covers the seam:

  * the period the dashboard is about is the period the JOB selected, and a
    period the Eco weekly model does not describe is refused rather than
    replaced;
  * the right driver gets the right link, in that driver's message only;
  * a valid product state — insufficient qualifying distance — is a dashboard
    like any other and still produces a link;
  * a TECHNICAL failure withholds the link AND the e-mail, for that driver
    only, while the rest of the run continues;
  * a rerun of one logical delivery converges on ONE publication, ONE
    capability and the SAME link;
  * the existing example.invalid SMTP lifecycle is untouched, the dashboard's own
    `SmtpEmailProvider` is never reached from these flows, and no capability
    reaches a log, a summary, an audit record or an exception;
  * a fleet run does not open a database connection per driver.

TIER A needs nothing at all. TIER B needs a disposable PostgreSQL instance
carrying migration 049 and exercises the REAL `DeliveryLedger` — its
constraints, its guard trigger and its fenced transitions — against a fake
publisher transport. Tier B is skipped LOUDLY when no DSN is exported: a skipped
half is reported as skipped, never as a pass.

DESTRUCTIVE (Tier B only). It drops and recreates
`public.eco_dashboard_delivery_operation` in the database the DSN names, and
refuses any DSN that is not loopback.

NOT DONE ANYWHERE IN THIS FILE: a real e-mail, an SMTP connection, a live
provider call, a Cloudflare resource, a wrangler invocation, a deployment, a
schedule, or any mutation of a production database.
"""

from __future__ import annotations

import ast
import hashlib
import html
import inspect
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving_dashboard import eco_mailing_integration as emi  # noqa: E402
from jobs.ecodriving_dashboard import publisher as pub  # noqa: E402
from jobs.ecodriving_dashboard import secure_delivery_client as sdc  # noqa: E402
from jobs.ecodriving_dashboard.delivery_contract import (  # noqa: E402
    DeliveryIdentity,
    DeliveryState,
    derive_operation_id,
)

PASSED: list[str] = []
SKIPPED: list[str] = []

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
BASE_URL = "https://eco.example.invalid/dashboard"
ENDPOINT = "https://publisher.example.invalid"
TOKEN = "SYNTHETIC-PUBLISHER-CREDENTIAL-2f9c1a"
#: Two synthetic 43-character capability shapes. Neither is a grant and neither
#: names a real system; both exist only to be searched for.
CAP_A = "A" * 43
CAP_B = "B" * 43

#: The two template insertion points. Both are required in every template, so a
#: synthetic fixture that exercises ONE of them must still carry the other in a
#: valid position — otherwise the rejection under test could be the wrong one.
SECTION_TOKEN = "{" + emi.DASHBOARD_SECTION_PLACEHOLDER + "}"
LINK_TOKEN = "{" + emi.DASHBOARD_LINK_PLACEHOLDER + "}"


def with_valid_section(template: str) -> str:
    return SECTION_TOKEN + "\n" + template


WEEK_START = date(2026, 5, 1)
WEEK_END_EXCLUSIVE = date(2026, 5, 18)   # W3 of 2026-05
MONTH_START = date(2026, 4, 1)
MONTH_END_EXCLUSIVE = date(2026, 5, 1)


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _function_body_source(source: str, name: str) -> str:
    """One top-level function's CODE, with its docstring removed.

    Prose that says "this never calls `_submit`" would otherwise defeat a
    source scan for `_submit`, which is the wrong way round: the comment is
    what the code is supposed to be checked against.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"no top-level function named {name!r}")


ECO_JOBS = (
    "jobs/ecodriving/job_eco_driving_weekly_email_notifications.py",
    "jobs/ecodriving/job_eco_driving_monthly_email_notifications.py",
    "jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py",
    "jobs/ecodriving_person/job_eco_driving_person_monthly_email_notifications.py",
)

TEMPLATE_DIRS = (
    "assets/email_templates/ecodriving/weekly",
    "assets/email_templates/ecodriving/monthly",
    "assets/email_templates/ecodriving_person/weekly",
    "assets/email_templates/ecodriving_person/monthly",
)


# ==============================================================================
# TIER A — no database, no network, no Node, no e-mail
# ==============================================================================


# --- 1. the authoritative period is consumed, never re-chosen -------------------


def test_the_period_the_job_selected_is_the_period_the_dashboard_uses() -> None:
    current, previous = emi.period_identities_for("weekly", WEEK_START, WEEK_END_EXCLUSIVE)
    check("current start is the job's start", current.period_start_date == WEEK_START)
    check("current end is the job's end",
          current.period_end_date_exclusive == WEEK_END_EXCLUSIVE)

    # W1/W2/W3 CUMULATIVE, read from the existing Eco model and not recomputed:
    # every cumulative period of a month starts at the month start, and the
    # comparison basis is the PRECEDING cumulative snapshot, not an incremental
    # week.
    check("weekly period is cumulative month-to-date",
          current.month_start_date == WEEK_START and current.period_sequence_in_month == 3)
    check("previous is the preceding CUMULATIVE period, not a bare week",
          previous is not None
          and previous.period_start_date == WEEK_START
          and previous.period_sequence_in_month == 2
          and previous.period_end_date_exclusive < WEEK_END_EXCLUSIVE)

    # W1 has no predecessor inside its month; the integration must not reach
    # into the previous month for one.
    w1_current, w1_previous = emi.period_identities_for("weekly", WEEK_START, date(2026, 5, 4))
    check("W1 is cumulative from the month start", w1_current.period_sequence_in_month == 1)
    check("W1 has no in-month predecessor", w1_previous is None)

    month_current, month_previous = emi.period_identities_for(
        "monthly", MONTH_START, MONTH_END_EXCLUSIVE)
    check("monthly consumes the closed month verbatim",
          month_current.period_start_date == MONTH_START
          and month_current.period_end_date_exclusive == MONTH_END_EXCLUSIVE)
    check("monthly compares against the preceding closed month",
          month_previous is not None
          and month_previous.period_start_date == date(2026, 3, 1)
          and month_previous.period_end_date_exclusive == MONTH_START)
    PASSED.append("the_period_the_job_selected_is_the_period_the_dashboard_uses")


def test_a_period_outside_the_eco_model_is_refused_not_substituted() -> None:
    refusals = [
        # A start that is not the cumulative month start the job persists.
        ("weekly", date(2026, 5, 4), WEEK_END_EXCLUSIVE),
        # A boundary no cumulative weekly period ends at.
        ("weekly", WEEK_START, date(2026, 5, 19)),
        # A "month" that is not a whole calendar month.
        ("monthly", MONTH_START, date(2026, 4, 18)),
        ("monthly", date(2026, 4, 2), date(2026, 5, 2)),
        # An inverted period.
        ("weekly", WEEK_END_EXCLUSIVE, WEEK_START),
    ]
    for period_type, start, end in refusals:
        try:
            emi.period_identities_for(period_type, start, end)
        except emi.DashboardPeriodMismatch:
            continue
        raise AssertionError(f"{period_type} {start}..{end} must be refused")

    # THE POINT OF THE REFUSAL: nothing near-by is offered instead.
    source = inspect.getsource(emi.period_identities_for)
    for forbidden in ("resolve_previous_completed_weekly_snapshot",
                      "resolve_previous_completed_month",
                      "resolve_final_month_weekly_snapshot",
                      "now_business_tz", "date.today"):
        check("the integration selects no period of its own",
              forbidden not in source, forbidden)
    PASSED.append("a_period_outside_the_eco_model_is_refused_not_substituted")


def test_the_dashboard_never_recomputes_eco_scoring() -> None:
    source = read("jobs/ecodriving_dashboard/eco_mailing_integration.py")
    for forbidden in ("eco_driving_score_total =", "def _score", "ranking_position =",
                      "qualification_status ="):
        check("no scoring is re-derived in the integration",
              forbidden not in source, forbidden)
    # The one path from Eco data to publishable bytes is the shared one.
    check("the integration builds through the single host snapshot path",
          "build_delivery_snapshot_from_cursor" in source)
    PASSED.append("the_dashboard_never_recomputes_eco_scoring")


# --- 2. the link in the message ------------------------------------------------


def test_the_link_is_escaped_and_carries_no_tracking() -> None:
    hostile = "https://eco.example.invalid/a\"onmouseover=alert(1)"
    blocks = {
        "cta": emi.render_dashboard_link_block(hostile + "#k=" + CAP_A),
        "section": emi.render_dashboard_section_block(hostile + "#k=" + CAP_A),
    }
    for name, block in blocks.items():
        check("the raw quote never reaches the attribute",
              '"onmouseover' not in block, name)
        check("the URL is attribute-escaped", "&quot;onmouseover" in block, name)
        # Two hrefs per block: the MSO VML button and the non-MSO anchor pill.
        check("both Outlook variants carry the href",
              block.count('href="') == 2, f"{name}: {block.count(chr(34))}")
        check("the non-MSO anchor is well formed", block.count("<a href=") == 1, name)
        for forbidden in ("<script", "javascript:", "utm_", "/r/", "click.", "track"):
            check("no tracking or script in the fragment",
                  forbidden not in block.lower(), f"{name}: {forbidden}")
        check("no image beacon", "<img" not in block.lower(), name)

    empty = emi.dashboard_link_context(None)
    check("a run without the integration contributes empty fragments",
          empty == {emi.DASHBOARD_SECTION_PLACEHOLDER: "",
                    emi.DASHBOARD_LINK_PLACEHOLDER: ""}, str(empty))
    filled = emi.dashboard_link_context(BASE_URL + "#k=" + CAP_A)
    check("a run with a link contributes both anchors",
          CAP_A in filled[emi.DASHBOARD_LINK_PLACEHOLDER]
          and CAP_A in filled[emi.DASHBOARD_SECTION_PLACEHOLDER])
    PASSED.append("the_link_is_escaped_and_carries_no_tracking")


def test_both_dashboard_entry_points_are_one_capability_shown_twice() -> None:
    """Two placements, one URL, one label, and the ALPHA button's exact styling.

    THE CASE THIS CLOSES. Showing the dashboard twice must stay a PRESENTATION
    duplication. If either fragment could reach a second capability the mail
    would carry two live grants for one delivery, and the whole
    one-publication-per-delivery contract would be a comment rather than a fact.
    `dashboard_link_context` takes ONE url and is the only producer, so the
    property is checked where it is decided.
    """
    url = BASE_URL + "#k=" + CAP_A
    context = emi.dashboard_link_context(url)
    rendered = context[emi.DASHBOARD_SECTION_PLACEHOLDER] + context[emi.DASHBOARD_LINK_PLACEHOLDER]

    check("exactly two dashboard CTA anchors", rendered.count("<a href=") == 2)
    check("and four hrefs in total counting the Outlook variants",
          rendered.count('href="') == 4)
    hrefs = set(re.findall(r'href="([^"]+)"', rendered))
    check("every href is the SAME already-minted capability URL",
          hrefs == {html.escape(url, quote=True)}, str(hrefs))
    check("no other capability shape appears", CAP_B not in rendered)

    check("both buttons show the agreed label",
          rendered.count("Zobacz szczegóły swojej jazdy") == 4, rendered)
    check("the section carries its title",
          "Twój Panel EcoDriving" in context[emi.DASHBOARD_SECTION_PLACEHOLDER])
    check("the bare CTA carries no title and no description",
          "Twój Panel EcoDriving" not in context[emi.DASHBOARD_LINK_PLACEHOLDER])

    # The styling contract is the EXISTING ALPHA programme button, read from the
    # shipped template rather than restated here.
    alpha = read("assets/email_templates/ecodriving/weekly/Tygodniowe - Niebezpieczni.html")
    pill = ("display:inline-block; background-color:#1F8A4C; color:#FFFFFF; "
            "text-decoration:none; font-size:16px; font-weight:bold; "
            "padding:15px 28px; border-radius:999px;")
    check("the ALPHA pill style is still what the template ships", pill in alpha)
    check("both dashboard blocks reuse it character for character",
          all(pill in block for block in
              (context[emi.DASHBOARD_SECTION_PLACEHOLDER],
               context[emi.DASHBOARD_LINK_PLACEHOLDER])))
    for vml in ('height:48px; v-text-anchor:middle;', 'arcsize="50%"',
                'strokecolor="#1F8A4C"', 'fillcolor="#1F8A4C"', "<w:anchorlock/>",
                "<!--[if mso]>", "<![endif]-->", "<!--[if !mso]><!-- -->",
                "<!--<![endif]-->"):
        check("the Outlook-safe contract is not downgraded",
              all(vml in block for block in
                  (context[emi.DASHBOARD_SECTION_PLACEHOLDER],
                   context[emi.DASHBOARD_LINK_PLACEHOLDER])), vml)
    check("the VML box was widened for the longer label, not redesigned",
          f"width:{emi.DASHBOARD_CTA_VML_WIDTH_PX}px;" in rendered
          and emi.DASHBOARD_CTA_VML_WIDTH_PX >= 320)

    # Empty means EMPTY: no row, no cell, no residue.
    off = emi.dashboard_link_context(None)
    check("the disabled case leaves nothing behind",
          off[emi.DASHBOARD_SECTION_PLACEHOLDER] == ""
          and off[emi.DASHBOARD_LINK_PLACEHOLDER] == "")
    check("both fragments are whole table rows",
          all(context[k].startswith("<tr>") and context[k].endswith("</tr>")
              for k in (emi.DASHBOARD_SECTION_PLACEHOLDER,
                        emi.DASHBOARD_LINK_PLACEHOLDER)))
    PASSED.append("both_dashboard_entry_points_are_one_capability_shown_twice")


def test_the_dashboard_presentation_is_centred_in_both_placements() -> None:
    """Both dashboard entry points are horizontally centred, and only that.

    THE CASE THIS CLOSES. Centring an e-mail is not a CSS question, it is a
    question of WHICH centring each engine obeys. Outlook desktop renders
    through Word, which honours the deprecated `align` ATTRIBUTE on a cell and
    is the engine that draws the VML button; everything else obeys
    `text-align`. A fragment that carried only one of the two would centre in
    half the estate and silently left-align in the other half, and no
    deterministic check downstream would notice.

    So both are asserted, in both placements — and the SECOND half of this test
    asserts what must NOT have moved: the label, the URL, the button styling,
    the VML contract and the empty-when-disabled behaviour. This change is
    presentation alignment; if it ever becomes anything else, this fails.
    """
    url = BASE_URL + "#k=" + CAP_A
    context = emi.dashboard_link_context(url)
    section = context[emi.DASHBOARD_SECTION_PLACEHOLDER]
    standalone = context[emi.DASHBOARD_LINK_PLACEHOLDER]

    # --- 1/2. the titled section, and its heading, text and CTA -------------
    card_cell = re.search(r'<td([^>]*padding:24px[^>]*)>', section)
    check("the section card still has its 24px content cell", card_cell is not None,
          section)
    cell_attrs = card_cell.group(1)
    check("Word centres the section card cell by attribute",
          'align="center"' in cell_attrs, cell_attrs)
    check("every other client centres it by CSS",
          "text-align:center" in cell_attrs, cell_attrs)

    divs = re.findall(r'<div([^>]*)>', section)
    check("the section still carries exactly its three children",
          len(divs) == 3, str(divs))
    for label, attrs in zip(("heading", "description", "CTA"), divs):
        check("the section child is centred explicitly",
              "text-align:center" in attrs, f"{label}: {attrs}")

    heading_at = section.index(emi.DASHBOARD_SECTION_TITLE)
    description_at = section.index(emi.DASHBOARD_SECTION_DESCRIPTION)
    cta_at = section.index("<!--[if mso]>")
    check("the centred children are the heading, the text and the CTA, in order",
          heading_at < description_at < cta_at, section)

    # --- 3. the standalone repeated CTA ------------------------------------
    bare_cell = re.search(r'<td([^>]*)>', standalone)
    check("the standalone CTA has its cell", bare_cell is not None, standalone)
    check("Word centres the standalone CTA by attribute",
          'align="center"' in bare_cell.group(1), bare_cell.group(1))
    check("every other client centres the standalone CTA by CSS",
          "text-align:center" in bare_cell.group(1), bare_cell.group(1))
    check("the standalone CTA gained no heading and no description",
          emi.DASHBOARD_SECTION_TITLE not in standalone
          and emi.DASHBOARD_SECTION_DESCRIPTION not in standalone)

    # --- 4. the Outlook/VML path is still the same button ------------------
    for vml in ('<!--[if mso]>', '<v:roundrect ', 'arcsize="50%"',
                'strokecolor="#1F8A4C"', 'fillcolor="#1F8A4C"', '<w:anchorlock/>',
                'v-text-anchor:middle', '</v:roundrect>', '<![endif]-->',
                '<!--[if !mso]><!-- -->', '<!--<![endif]-->'):
        check("the MSO/VML CTA survives centring unchanged",
              vml in section and vml in standalone, vml)
    check("the VML width is unchanged",
          section.count(f"width:{emi.DASHBOARD_CTA_VML_WIDTH_PX}px;") == 1
          and standalone.count(f"width:{emi.DASHBOARD_CTA_VML_WIDTH_PX}px;") == 1)
    check("centring was not smuggled into the VML shape's own style",
          "text-align:center" not in section[section.index("<v:roundrect"):
                                             section.index("</v:roundrect>")])

    # --- 5. still ONE capability, shown twice ------------------------------
    rendered = section + standalone
    hrefs = set(re.findall(r'href="([^"]+)"', rendered))
    check("both centred placements carry the SAME single capability URL",
          hrefs == {html.escape(url, quote=True)}, str(hrefs))
    check("and no second capability appeared", CAP_B not in rendered)
    check("the label is untouched in all four variants",
          rendered.count(emi.DASHBOARD_CTA_LABEL) == 4, rendered)

    # --- 6. disabled still renders to nothing at all -----------------------
    off = emi.dashboard_link_context(None)
    check("centring did not give the disabled case something to render",
          off[emi.DASHBOARD_SECTION_PLACEHOLDER] == ""
          and off[emi.DASHBOARD_LINK_PLACEHOLDER] == "")

    # --- 7. presentation only: nothing but alignment moved -----------------
    pill = ("display:inline-block; background-color:#1F8A4C; color:#FFFFFF; "
            "text-decoration:none; font-size:16px; font-weight:bold; "
            "padding:15px 28px; border-radius:999px;")
    check("the anchor pill is still character for character the ALPHA button",
          pill in section and pill in standalone)
    check("the card's own colours and border did not move",
          "background-color:#FFFFFF; border:1px solid #E3E8E5;" in section)
    check("the section cell padding did not move", "padding:24px;" in section)
    check("the standalone cell padding did not move",
          "padding:8px 32px 30px 32px;" in standalone)
    check("the heading and body colours did not move",
          "color:#1F2933;" in section and "color:#52616B;" in section)
    check("both fragments are still whole table rows",
          all(f.startswith("<tr>") and f.endswith("</tr>")
              for f in (section, standalone)))

    # The rounded-card family adopts the template's radius exactly as before.
    rounded = emi.render_dashboard_section_block(
        url, card_radius=emi.CARD_RADIUS_ROUNDED)
    check("the rounded variant is still centred and still rounded",
          f"border-radius:{emi.CARD_RADIUS_ROUNDED};" in rounded
          and 'align="center"' in rounded)

    # Alignment is a string of HTML, so neither renderer may have acquired a
    # side effect while it was being centred. Checked against the CODE, so a
    # docstring saying so cannot satisfy it.
    module_source = read("jobs/ecodriving_dashboard/eco_mailing_integration.py")
    for fn in ("render_dashboard_section_block", "render_dashboard_link_block",
               "_dashboard_cta_html"):
        body = _function_body_source(module_source, fn)
        for forbidden in ("publish", "mint", "issue", "send", "smtp", "imap",
                          "cursor", "commit", "ledger", "requests."):
            check("the renderer is still pure presentation",
                  forbidden not in body.lower(), f"{fn}: {forbidden}")
    PASSED.append("the_dashboard_presentation_is_centred_in_both_placements")


def test_every_existing_template_family_has_the_insertion_point() -> None:
    """Both insertion points, in the agreed order, in every shipped template.

    The ORDER is part of the presentation contract, not a rendering accident:
    the titled section belongs directly before `Podsumowanie`, and the repeated
    CTA belongs after the last recommendation/action section — `Obszar do
    poprawy` where the variant has one — and before the legacy programme CTA
    and the footer.

    The below-threshold templates are the ONE exception, and they are checked
    for the opposite property in
    `test_the_below_threshold_templates_show_no_dashboard`: a recipient who has
    not reached the qualifying distance is offered no dashboard, so those files
    carry no insertion point to order.
    """
    seen = 0
    below_threshold = 0
    with_improvement = 0
    for directory in TEMPLATE_DIRS:
        files = sorted((REPO_ROOT / directory).glob("*.html"))
        check("template family is not empty", bool(files), directory)
        for path in files:
            body = path.read_text(encoding="utf-8")
            if path.name in emi.TEMPLATES_WITHOUT_DASHBOARD:
                below_threshold += 1
                continue
            for placeholder in emi.DASHBOARD_PLACEHOLDERS:
                token = "{" + placeholder + "}"
                check("template carries the dashboard placeholder",
                      body.count(token) == 1, f"{path}: {token}")
                check("the placeholder sits between table rows, not inside one",
                      re.search(r"</tr>\s*(<!--[^>]*-->\s*)?\{" + placeholder + r"\}",
                                body) is not None,
                      f"{path}: {token}")

            section_at = body.index(SECTION_TOKEN)
            cta_at = body.index(LINK_TOKEN)
            check("the section comes first", section_at < cta_at, str(path))

            # 1. the titled block sits directly before the summary section...
            if "Podsumowanie" in body:
                check("the dashboard section precedes Podsumowanie",
                      section_at < body.index("Podsumowanie"), str(path))
            after_section = body[section_at + len(SECTION_TOKEN):]
            check("the Main Message row follows it immediately",
                  0 <= after_section.find("<!-- Main Message -->") < 40,
                  str(path))

            # 2. ...and the repeated CTA follows the improvement/recommendation
            #    content, ahead of the legacy programme CTA and the footer.
            if "Obszar do poprawy" in body:
                with_improvement += 1
                check("the second CTA follows Obszar do poprawy",
                      cta_at > body.index("Obszar do poprawy"), str(path))
            elif "Rekomendacja" in body:
                check("the second CTA follows the recommendation section",
                      cta_at > body.index("Rekomendacja"), str(path))
            if "Dowiedz się więcej" in body:
                check("the second CTA precedes the legacy programme CTA",
                      cta_at < body.index("Dowiedz się więcej"), str(path))
            check("the second CTA precedes the footer",
                  cta_at < body.index("<!-- Footer -->"), str(path))
            seen += 1
    check("all four families are covered", seen + below_threshold == 28,
          str(seen + below_threshold))
    check("every family ships exactly one below-threshold template",
          below_threshold == 4, str(below_threshold))
    check("the dashboard-carrying templates are the rest", seen == 24, str(seen))
    check("the improvement variants really exist", with_improvement == 16,
          str(with_improvement))
    PASSED.append("every_existing_template_family_has_the_insertion_point")


def test_a_rendered_message_carries_the_dashboard_twice() -> None:
    """The acceptance property, proved on the SHIPPED templates end to end.

    The fragment tests above prove what the two blocks contain and the inventory
    test proves where the two placeholders sit. Neither proves the thing that
    actually reaches a driver, which is the RENDERED message: two dashboard
    entry points, one URL, the summary section after the first and the
    improvement section before the second, and the legacy programme CTA still
    exactly where it was.
    """
    url = BASE_URL + "#k=" + CAP_A
    escaped = html.escape(url, quote=True)
    variants = [
        # a ranked `safe` variant (no `Obszar do poprawy`)...
        "assets/email_templates/ecodriving/weekly/Tygodniowe - Bezpieczni.html",
        # ...a ranked `dangerous` variant that has one...
        "assets/email_templates/ecodriving/weekly/Tygodniowe - Niebezpieczni.html",
        # ...a monthly `acceptable` one...
        "assets/email_templates/ecodriving/monthly/Miesięczne - Akceptowalni.html",
        # ...a no-rank variant...
        "assets/email_templates/ecodriving/weekly/Tygodniowe - Akceptowalni - norank.html",
        # ...and the BRAVO family, which ships no programme CTA at all.
        "assets/email_templates/ecodriving_person/weekly/Tygodniowe - Niebezpieczni.html",
    ]
    for relative in variants:
        template = read(relative)
        service = ScriptedService({
            "DRIVER-A": emi.DashboardLinkOutcome(emi.LinkStatus.LINKED,
                                                 capability_url=url,
                                                 snapshot_status="OK"),
        })
        body, outcome = emi.render_with_dashboard_link(
            service, identity_key="DRIVER-A", recipient_email="a@example.invalid",
            context={}, template_html=template, render=_render(template))
        check("the message is produced", body is not None, relative)
        check("one link was requested, once", len(service.asked) == 1, relative)

        check("exactly two dashboard CTA anchors",
              body.count(f'<a href="{escaped}"') == 2, relative)
        dashboard_hrefs = re.findall(
            r'<a href="([^"]+)"[^>]*>Zobacz szczegóły swojej jazdy</a>', body)
        check("both are the same already-minted capability URL",
              dashboard_hrefs == [escaped, escaped], f"{relative}: {dashboard_hrefs}")
        check("both show the agreed label",
              body.count("Zobacz szczegóły swojej jazdy") == 4, relative)
        check("and the Outlook VML variant is there twice",
              body.count(f'href="{escaped}" style="height:48px') == 2, relative)

        section_at = body.index("Twój Panel EcoDriving")
        cta_at = body.rindex(f'<a href="{escaped}"')
        if "Podsumowanie" in body:
            check("the section precedes Podsumowanie",
                  section_at < body.index("Podsumowanie"), relative)
        if "Obszar do poprawy" in body:
            check("the second CTA follows Obszar do poprawy",
                  cta_at > body.index("Obszar do poprawy"), relative)
        if "Dowiedz się więcej" in body:
            check("the legacy programme CTA survives, after both of ours",
                  body.count("Dowiedz się więcej") == 2
                  and body.index("Dowiedz się więcej") > cta_at, relative)
        else:
            check("the BRAVO family still has no programme CTA",
                  "Dowiedz się więcej" not in body, relative)

        # ...and the same template with the integration off is the legacy mail.
        off, off_outcome = emi.render_with_dashboard_link(
            None, identity_key="DRIVER-A", recipient_email="a@example.invalid",
            context={}, template_html=template, render=_render(template))
        check("no dashboard CTA at all", CAP_A not in off, relative)
        check("no dashboard section at all",
              "Twój Panel EcoDriving" not in off
              and "Zobacz szczegóły swojej jazdy" not in off, relative)
        check("and no empty row, gap or placeholder residue",
              re.search(r"<tr>\s*</tr>", off) is None
              and SECTION_TOKEN not in off and LINK_TOKEN not in off, relative)
        check("the send is not blocked", not off_outcome.blocks_send, relative)
        check("the linked case did not block either", not outcome.blocks_send, relative)
    PASSED.append("a_rendered_message_carries_the_dashboard_twice")


#: The four below-threshold mailing variants — the message a recipient gets for
#: a period under the qualifying distance, in every family.
BELOW_THRESHOLD_TEMPLATES = (
    "assets/email_templates/ecodriving/weekly/Tygodniowe - Niezakwalifikowani.html",
    "assets/email_templates/ecodriving/monthly/Miesięczne - Niezakwalifikowani.html",
    "assets/email_templates/ecodriving_person/weekly/Tygodniowe - Niezakwalifikowani.html",
    "assets/email_templates/ecodriving_person/monthly/Miesięczne - Niezakwalifikowani.html",
)


def test_the_below_threshold_templates_show_no_dashboard() -> None:
    """A recipient under the qualifying distance is offered no dashboard.

    Proved on the SHIPPED files and end to end through the exact seam the four
    jobs call, with the integration ON — the case that matters, because a run
    with the integration off proves nothing about a CTA that was removed.

    Three properties, because two of them are individually satisfiable by a
    broken outcome: the rendered message carries no dashboard CTA and no
    capability URL; NO link was requested, so nothing was published and no
    capability was minted for a message that could not show one; and the send
    is not blocked — the below-threshold e-mail still goes out, unchanged apart
    from the CTA that was removed.
    """
    url = BASE_URL + "#k=" + CAP_A
    escaped = html.escape(url, quote=True)
    for relative in BELOW_THRESHOLD_TEMPLATES:
        template = read(relative)
        check("the template carries no dashboard insertion point",
              SECTION_TOKEN not in template and LINK_TOKEN not in template, relative)

        service = ScriptedService({
            "DRIVER-A": emi.DashboardLinkOutcome(emi.LinkStatus.LINKED,
                                                 capability_url=url,
                                                 snapshot_status="OK"),
        })
        body, outcome = emi.render_with_dashboard_link(
            service, identity_key="DRIVER-A", recipient_email="a@example.invalid",
            context={}, template_html=template, render=_render(template))

        check("the message is produced", body is not None, relative)
        check("the send is not blocked", not outcome.blocks_send, relative)
        check("it is reported as a template that shows no dashboard",
              outcome.status == emi.LinkStatus.NO_DASHBOARD_IN_TEMPLATE, relative)
        check("no link was requested at all", service.asked == [], relative)
        check("and it was not recorded as a template failure",
              service.template_failures == 0, relative)

        check("no dashboard CTA anchor", f'<a href="{escaped}"' not in body, relative)
        check("no dashboard CTA label",
              "Zobacz szczegóły swojej jazdy" not in body, relative)
        check("no dashboard section", "Twój Panel EcoDriving" not in body, relative)
        check("no capability, escaped or not",
              CAP_A not in body and url not in body and escaped not in body, relative)
        check("no dashboard base URL of any shape", BASE_URL not in body, relative)
        check("and no placeholder residue or empty row",
              SECTION_TOKEN not in body and LINK_TOKEN not in body
              and re.search(r"<tr>\s*</tr>", body) is None, relative)

        # ...and the rest of the message is exactly what it was.
        check("the below-threshold copy is intact",
              "Twój aktualny dystans jest zbyt niski" in body
              and "100 km" in body, relative)
        check("the footer is intact", "<!-- Footer -->" in body, relative)
        check("the template renders unchanged, because nothing was substituted",
              body == template, relative)
    PASSED.append("the_below_threshold_templates_show_no_dashboard")


def test_the_below_threshold_templates_pass_the_run_preflight() -> None:
    """The run-level check accepts them, and still rejects a real regression.

    `validate_template_dir_link_placeholders` runs once per job, over the whole
    required template inventory, before any candidate. It must not fail on a
    template that deliberately shows no dashboard — and it must still fail when
    such a template grows one back, or when a dashboard template loses one.
    """
    for directory in TEMPLATE_DIRS:
        path = REPO_ROOT / directory
        names = sorted(p.name for p in path.glob("*.html"))
        emi.validate_template_dir_link_placeholders(path, names)
        check("every family ships one below-threshold template",
              sum(1 for n in names if n in emi.TEMPLATES_WITHOUT_DASHBOARD) == 1,
              directory)
    PASSED.append("the_below_threshold_templates_pass_the_run_preflight_dir")

    below = next(iter(emi.TEMPLATES_WITHOUT_DASHBOARD))
    try:
        emi.assert_no_dashboard_placeholders(
            "<tr>x</tr>\n" + SECTION_TOKEN + "\n" + LINK_TOKEN, source=below)
    except emi.DashboardTemplateError as error:
        check("the rejection names what it found",
              emi.DASHBOARD_SECTION_PLACEHOLDER in str(error), str(error))
    else:
        raise AssertionError("a below-threshold template that grew a CTA back was accepted")

    # A dashboard template that lost ONE placeholder is still a broken dashboard
    # template, not a dashboard-free one: the placement guard must still reject
    # it, and the render seam must still route it there.
    half = "<tr>x</tr>\n" + SECTION_TOKEN
    try:
        emi.assert_link_placeholder_placement(half, source="half")
    except emi.DashboardTemplateError:
        pass
    else:
        raise AssertionError("a template missing one placeholder was accepted")
    check("a half-placeholder template still counts as carrying a dashboard",
          emi.template_carries_dashboard(half))

    service = ExplodingService()
    body, outcome = emi.render_with_dashboard_link(
        service, identity_key="DRIVER-A", recipient_email="a@example.invalid",
        context={}, template_html=half, render=_render(half))
    check("and it blocks the send rather than shipping a link-less message",
          body is None and outcome.blocks_send)
    check("refused as a placement failure, before any publication",
          service.placement_failures == 1)
    PASSED.append("the_below_threshold_templates_pass_the_run_preflight")


def test_the_legacy_programme_cta_is_untouched() -> None:
    """The ALPHA `Dowiedz się więcej` button is the reference, not the target."""
    programme_url = ("https://sharepoint.example.invalid/witryny/hrportal/organizacja-pracy/"
                     "SitePages/Podejmij-wyzwanie-%E2%80%93-startuje-Program-"
                     "Ecodriving!.aspx")
    seen = 0
    for directory in TEMPLATE_DIRS:
        for path in sorted((REPO_ROOT / directory).glob("*.html")):
            body = path.read_text(encoding="utf-8")
            if "Dowiedz się więcej" not in body:
                continue
            seen += 1
            check("both Outlook variants of the legacy CTA survive",
                  body.count("Dowiedz się więcej") == 2, str(path))
            check("its destination is unchanged",
                  body.count(programme_url) == 2, str(path))
            check("its VML geometry is unchanged",
                  'style="height:48px; v-text-anchor:middle; width:260px;"' in body,
                  str(path))
    check("the ALPHA family still ships the programme CTA", seen == 14, str(seen))
    PASSED.append("the_legacy_programme_cta_is_untouched")


def test_the_insertion_point_is_a_position_not_a_substring() -> None:
    """A placeholder a renderer would swallow is refused, not published around.

    Substring presence — of the placeholder in the template, or of the rendered
    URL in the message — accepts a link nobody can see: `<!-- {ph} -->`
    substitutes cleanly, passes both checks, and reaches the driver inside a
    comment. Every remote effect fires and the one thing the integration exists
    for does not happen. Placement is therefore checked as a POSITION.
    """
    token = "{" + emi.DASHBOARD_LINK_PLACEHOLDER + "}"

    for directory in TEMPLATE_DIRS:
        for path in sorted((REPO_ROOT / directory).glob("*.html")):
            if path.name in emi.TEMPLATES_WITHOUT_DASHBOARD:
                continue  # shows no dashboard by design; see its own test
            emi.assert_link_placeholder_placement(
                path.read_text(encoding="utf-8"), source=path.name)

    accepted = (
        ("between table rows", f"<table><tr><td>a</td></tr>{token}</table>"),
        ("after a comment that closed", f"<body><!-- note -->\n{token}\n</body>"),
    )
    for name, template in accepted:
        emi.assert_link_placeholder_placement(with_valid_section(template), source=name)

    rejected = {
        "inside an HTML comment": f"<body><!-- {token} --></body>",
        "inside an Outlook conditional comment": f"<body><!--[if mso]>{token}<![endif]--></body>",
        "inside a tag attribute": f'<body><a href="{token}">x</a></body>',
        "inside a tag": f"<body><td {token}>x</td></body>",
        "inside script": f"<body><script>var a='{token}';</script></body>",
        "inside style": f"<body><style>/* {token} */</style></body>",
        "in an unterminated comment": f"<body><!-- {token}</body>",
        "absent": "<body><p>no insertion point</p></body>",
        "malformed - no closing brace": "<body>{eco_dashboard_link_html</body>",
        "duplicated": f"<body>{token}{token}</body>",
        "a visible copy plus a commented one": f"<body><!-- {token} -->{token}</body>",
    }
    rejected = {name: with_valid_section(t) for name, t in rejected.items()}

    # The SECOND insertion point is held to the same rule, and a template that
    # carries only one of the two is refused: half the agreed presentation is
    # not a lesser version of it, it is a message the design does not describe.
    rejected.update({
        "the section placeholder is absent": f"<body>{token}</body>",
        "the section placeholder is commented out":
            f"<body><!-- {SECTION_TOKEN} -->{token}</body>",
        "the section placeholder is inside an attribute":
            f'<body><a title="{SECTION_TOKEN}">x</a>{token}</body>',
        "the section placeholder is duplicated":
            f"<body>{SECTION_TOKEN}{SECTION_TOKEN}{token}</body>",
        "the link placeholder is absent": f"<body>{SECTION_TOKEN}</body>",
    })
    for name, template in rejected.items():
        try:
            emi.assert_link_placeholder_placement(template, source=name)
        except emi.DashboardTemplateError:
            continue
        raise AssertionError(f"accepted an unusable placement: {name}")
    PASSED.append("the_insertion_point_is_a_position_not_a_substring")


def test_a_quoted_attribute_cannot_fake_the_end_of_a_tag() -> None:
    """`>` inside an attribute value does not end the tag, and never did.

    THE DEFECT THIS CLOSES. The scanner took the first `>` after `<` as the end
    of the tag. In

        <a title="> {eco_dashboard_link_html}">

    that `>` is INSIDE the quoted `title` value, so the placeholder is still in
    an attribute — but the scanner had already declared the tag over and
    classified it as visible text. The template was accepted, the snapshot was
    published, a capability was minted and handed over, and the driver received
    a message whose dashboard link had been substituted into a tooltip string
    where nothing renders it. Exactly the failure the placement check exists to
    prevent, reached through the one shape it did not model.
    """
    token = "{" + emi.DASHBOARD_LINK_PLACEHOLDER + "}"

    rejected = {
        "double-quoted attribute containing >":
            f'<body><a title="> {token}">x</a></body>',
        "single-quoted attribute containing >":
            f"<body><a title='> {token}'>x</a></body>",
        "quoted attribute with whitespace around the =":
            f'<body><a title = "> {token}">x</a></body>',
        "quoted attribute containing > in a CLOSING-looking tag":
            f'<body><td data-x="a>b" title="> {token}">x</td></body>',
        "quoted attribute containing > with the tag never closed":
            f'<body><a title="> {token}',
    }
    for name, template in rejected.items():
        try:
            emi.assert_link_placeholder_placement(with_valid_section(template), source=name)
        except emi.DashboardTemplateError:
            continue
        raise AssertionError(f"accepted a placeholder inside an attribute: {name}")

    # ...and the other direction, which is the half a blunter fix would break:
    # a quoted `>` that really is inside a tag which really does close, with the
    # placeholder in the text content after it.
    accepted = {
        "text after a double-quoted attribute containing >":
            f'<body><a title="a > b">link</a>{token}</body>',
        "text after a single-quoted attribute containing >":
            f"<body><a title='a > b'>link</a>{token}</body>",
        "text after several quoted > in one tag":
            f'<body><td data-a="x>y" data-b=\'p>q\'>cell</td>{token}</body>',
        "an apostrophe in ordinary prose does not open an attribute":
            f"<body><p>driver's report</p>{token}</body>",
        "a stray quote in an UNQUOTED attribute value does not open one":
            f"<body><td data-x=y\'z >cell</td>{token}</body>",
    }
    for name, template in accepted.items():
        emi.assert_link_placeholder_placement(with_valid_section(template), source=name)

    # The real templates are the acceptance case that actually ships.
    seen = 0
    below_threshold = 0
    for directory in TEMPLATE_DIRS:
        for path in sorted((REPO_ROOT / directory).glob("*.html")):
            body = path.read_text(encoding="utf-8")
            if path.name in emi.TEMPLATES_WITHOUT_DASHBOARD:
                # No insertion point to scan — held to the opposite rule.
                emi.assert_no_dashboard_placeholders(body, source=path.name)
                below_threshold += 1
                continue
            emi.assert_link_placeholder_placement(body, source=path.name)
            seen += 1
    check("all 28 shipped templates remain valid under quote-aware scanning",
          seen + below_threshold == 28, str(seen + below_threshold))
    check("24 carry the dashboard and 4 deliberately do not",
          (seen, below_threshold) == (24, 4), f"{seen}/{below_threshold}")
    PASSED.append("a_quoted_attribute_cannot_fake_the_end_of_a_tag")


class ExplodingService:
    """A service that fails the test if it is asked to publish anything."""

    enabled = True

    def __init__(self):
        self.placement_failures = 0

    def link_for(self, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError(
            "a template that cannot show the link must be refused BEFORE any "
            "snapshot is published or any capability is minted")

    def template_placement_invalid(self, detail: str):
        self.placement_failures += 1
        return emi.DashboardLinkOutcome(
            emi.LinkStatus.FAILED, failure_code="DASHBOARD_LINK_PLACEHOLDER_INVALID",
            detail=detail)


def test_placement_is_refused_before_any_publication_effect() -> None:
    token = "{" + emi.DASHBOARD_LINK_PLACEHOLDER + "}"
    service = ExplodingService()
    commented = f"<html><body><p>{{driver_name}}</p><!-- {token} --></body></html>"

    body, outcome = emi.render_with_dashboard_link(
        service, identity_key="DRIVER-A", recipient_email="a@example.invalid",
        context={"driver_name": "A"}, template_html=commented,
        render=_render(commented))
    check("no message is produced", body is None)
    check("it is a technical failure with its own code",
          outcome.blocks_send
          and outcome.failure_code == "DASHBOARD_LINK_PLACEHOLDER_INVALID",
          str(outcome.failure_code))
    check("the service recorded it once", service.placement_failures == 1)

    # ...and the ORDER is a property of the code, not of this one scenario.
    source = _function_body_source(read("jobs/ecodriving_dashboard/eco_mailing_integration.py"),
                                   "render_with_dashboard_link")
    validate_at = source.index("assert_link_placeholder_placement")
    link_at = source.index("service.link_for")
    check("placement is validated before the link is requested",
          validate_at < link_at, source[:200])

    # ...and every job checks its whole inventory before the first candidate.
    for path in ECO_JOBS:
        job = read(path)
        check("the job validates its template inventory up front",
              "validate_template_dir_link_placeholders(" in job, path)
        check("before the per-driver loop reaches the dashboard",
              job.index("validate_template_dir_link_placeholders(")
              < job.index("render_with_dashboard_link("), path)
    PASSED.append("placement_is_refused_before_any_publication_effect")


# --- 3. per-driver binding, product states and technical failures ---------------


class ScriptedService:
    """An `EcoDashboardLinkService` stand-in with a scripted answer per driver.

    Used to drive `render_with_dashboard_link` — the exact code the four jobs
    call — without a database. What it must be faithful about is the CONTRACT:
    one outcome per driver, and `template_link_missing` recorded the same way a
    real failure is.
    """

    enabled = True

    def __init__(self, answers: dict):
        self.answers = answers
        self.asked: list[tuple[str, str]] = []
        self.template_failures = 0

    def link_for(self, *, identity_key: str, recipient_email: str):
        self.asked.append((identity_key, recipient_email))
        return self.answers[identity_key]

    def template_link_missing(self, outcome):
        self.template_failures += 1
        return emi.DashboardLinkOutcome(
            emi.LinkStatus.FAILED, failure_code="DASHBOARD_LINK_NOT_IN_TEMPLATE",
            detail="the rendered template does not contain the dashboard link",
            snapshot_status=outcome.snapshot_status)


TEMPLATE = ("<html><body><p>{driver_name}</p>"
            + SECTION_TOKEN + LINK_TOKEN + "</body></html>")


def _render(template: str):
    def render(context: dict) -> str:
        out = template
        for key, value in context.items():
            out = out.replace("{" + key + "}", str(value))
        return out
    return render


def test_each_driver_gets_exactly_their_own_link() -> None:
    url_a = BASE_URL + "#k=" + CAP_A
    url_b = BASE_URL + "#k=" + CAP_B
    service = ScriptedService({
        "DRIVER-A": emi.DashboardLinkOutcome(emi.LinkStatus.LINKED, capability_url=url_a,
                                             snapshot_status="OK"),
        "DRIVER-B": emi.DashboardLinkOutcome(emi.LinkStatus.LINKED, capability_url=url_b,
                                             snapshot_status="OK"),
    })
    bodies = {}
    for driver, address in (("DRIVER-A", "a@example.invalid"),
                            ("DRIVER-B", "b@example.invalid")):
        body, outcome = emi.render_with_dashboard_link(
            service, identity_key=driver, recipient_email=address,
            context={"driver_name": driver}, template_html=TEMPLATE,
            render=_render(TEMPLATE))
        check("the message was produced", body is not None, driver)
        check("the outcome is a link", outcome.status == emi.LinkStatus.LINKED)
        bodies[driver] = body

    check("A carries A's capability and nothing else",
          CAP_A in bodies["DRIVER-A"] and CAP_B not in bodies["DRIVER-A"])
    check("B carries B's capability and nothing else",
          CAP_B in bodies["DRIVER-B"] and CAP_A not in bodies["DRIVER-B"])
    check("each driver was asked exactly once with their own address",
          service.asked == [("DRIVER-A", "a@example.invalid"),
                            ("DRIVER-B", "b@example.invalid")])
    PASSED.append("each_driver_gets_exactly_their_own_link")


def test_a_valid_insufficient_distance_dashboard_still_produces_a_link() -> None:
    check("insufficient distance is a publishable product state",
          "INSUFFICIENT_DISTANCE" in emi.PUBLISHABLE_SNAPSHOT_STATUSES)
    check("report-not-ready is a publishable product state",
          "REPORT_NOT_READY" in emi.PUBLISHABLE_SNAPSHOT_STATUSES)

    service = ScriptedService({
        "DRIVER-LOW": emi.DashboardLinkOutcome(
            emi.LinkStatus.LINKED, capability_url=BASE_URL + "#k=" + CAP_A,
            snapshot_status="INSUFFICIENT_DISTANCE"),
    })
    body, outcome = emi.render_with_dashboard_link(
        service, identity_key="DRIVER-LOW", recipient_email="low@example.invalid",
        context={"driver_name": "Low"}, template_html=TEMPLATE,
        render=_render(TEMPLATE))
    check("a below-threshold driver is still mailed", body is not None)
    check("and still receives a link", CAP_A in body)
    check("the product state is not a failure", not outcome.blocks_send)
    PASSED.append("a_valid_insufficient_distance_dashboard_still_produces_a_link")


def test_a_technical_failure_withholds_that_driver_s_message_only() -> None:
    good = emi.DashboardLinkOutcome(emi.LinkStatus.LINKED,
                                    capability_url=BASE_URL + "#k=" + CAP_A,
                                    snapshot_status="OK")
    for failure_code in ("DASHBOARD_SNAPSHOT_FAILED", "DASHBOARD_SNAPSHOT_REFUSED",
                         "PUBLICATION_REFUSED", "DASHBOARD_BASE_URL_UNUSABLE",
                         "PREFLIGHT_FAILED"):
        service = ScriptedService({
            "DRIVER-1": good,
            "DRIVER-BAD": emi.DashboardLinkOutcome(
                emi.LinkStatus.FAILED, failure_code=failure_code, detail="synthetic"),
            "DRIVER-2": good,
        })
        results = {}
        for driver in ("DRIVER-1", "DRIVER-BAD", "DRIVER-2"):
            results[driver] = emi.render_with_dashboard_link(
                service, identity_key=driver, recipient_email=f"{driver}@example.invalid",
                context={"driver_name": driver}, template_html=TEMPLATE,
                render=_render(TEMPLATE))
        check("the failing driver produces no message",
              results["DRIVER-BAD"][0] is None, failure_code)
        check("and is reported as blocking the send",
              results["DRIVER-BAD"][1].blocks_send, failure_code)
        check("the drivers before and after are unaffected",
              results["DRIVER-1"][0] is not None and results["DRIVER-2"][0] is not None,
              failure_code)
    PASSED.append("a_technical_failure_withholds_that_driver_s_message_only")


def test_a_template_that_lost_the_placeholder_blocks_the_send() -> None:
    service = ScriptedService({
        "DRIVER-A": emi.DashboardLinkOutcome(emi.LinkStatus.LINKED,
                                             capability_url=BASE_URL + "#k=" + CAP_A,
                                             snapshot_status="OK"),
    })
    body, outcome = emi.render_with_dashboard_link(
        service, identity_key="DRIVER-A", recipient_email="a@example.invalid",
        context={"driver_name": "A"},
        # The placement gate accepts this template — the placeholder IS in text
        # content — and the RENDERER is what drops it. That is the second,
        # different failure the post-render presence check exists for.
        template_html=TEMPLATE,
        render=_render("<html><body><p>{driver_name}</p></body></html>"))
    check("a message that silently lost its link is not sent", body is None)
    check("and is recorded as a technical failure",
          outcome.blocks_send
          and outcome.failure_code == "DASHBOARD_LINK_NOT_IN_TEMPLATE")
    check("the service counted it", service.template_failures == 1)
    PASSED.append("a_template_that_lost_the_placeholder_blocks_the_send")


def test_a_run_without_the_integration_renders_exactly_as_before() -> None:
    body, outcome = emi.render_with_dashboard_link(
        None, identity_key="DRIVER-A", recipient_email="a@example.invalid",
        context={"driver_name": "A"}, template_html=TEMPLATE,
        render=_render(TEMPLATE))
    check("the message is still produced", body is not None)
    check("the placeholder resolves to nothing",
          "{" + emi.DASHBOARD_LINK_PLACEHOLDER + "}" not in body and "<a href" not in body)
    check("and it does not block the send", not outcome.blocks_send)
    check("the status says why", outcome.status == emi.LinkStatus.DISABLED)
    PASSED.append("a_run_without_the_integration_renders_exactly_as_before")


def test_each_job_binds_its_own_family_identity_and_mailer() -> None:
    """ALPHA by `assigned_id`, BRAVO by `person_name_group_key`, four mailers."""
    expected = {
        "jobs/ecodriving/job_eco_driving_weekly_email_notifications.py":
            ("assigned_id", emi.MAILER_ECO_WEEKLY, "REPORT_TYPE"),
        "jobs/ecodriving/job_eco_driving_monthly_email_notifications.py":
            ("assigned_id", emi.MAILER_ECO_MONTHLY, "REPORT_TYPE"),
        "jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py":
            ("person_name_group_key", emi.MAILER_ECO_PERSON_WEEKLY, "REPORT_TYPE"),
        "jobs/ecodriving_person/job_eco_driving_person_monthly_email_notifications.py":
            ("person_name_group_key", emi.MAILER_ECO_PERSON_MONTHLY, "REPORT_TYPE"),
    }
    mailers = set()
    for path, (identity_column, mailer, period_type) in expected.items():
        source = read(path)
        check("the job hands the dashboard its own driver identity",
              f'identity_key=str(row.get("{identity_column}") or "")' in source, path)
        check("the job names its own mailing lifecycle",
              f"mailer={_mailer_constant(mailer)}," in source, path)
        check("the job hands over its own report type",
              f"period_type={period_type}," in source, path)
        check("and the period it already selected, not a new one",
              "period_start_date=period_start_date," in source
              and "period_end_date=period_end_date," in source, path)
        check("bound to the address the message actually goes to",
              "recipient_email=effective_recipient," in source, path)
        mailers.add(mailer)
    check("the four jobs are four distinct mailing lifecycles", len(mailers) == 4)
    PASSED.append("each_job_binds_its_own_family_identity_and_mailer")


def _mailer_constant(value: str) -> str:
    for name in ("MAILER_ECO_WEEKLY", "MAILER_ECO_MONTHLY",
                 "MAILER_ECO_PERSON_WEEKLY", "MAILER_ECO_PERSON_MONTHLY"):
        if getattr(emi, name) == value:
            return name
    raise AssertionError(value)


def test_a_render_only_run_publishes_nothing() -> None:
    """The existing render-only contract: no external effect, still a link shape."""
    calls: list = []

    def exploding_conn(cfg, **kwargs):  # pragma: no cover - must never be reached
        calls.append(cfg)
        raise AssertionError("a render-only run must open no ledger connection")

    original_build = emi.build_delivery_snapshot_from_cursor
    original_conn = emi._client_business_pg_conn
    try:
        emi._client_business_pg_conn = exploding_conn  # type: ignore[assignment]
        emi.build_delivery_snapshot_from_cursor = (  # type: ignore[assignment]
            lambda cur, **kwargs: SyntheticSnapshot(
                payload=b"{}", payload_digest="0" * 64,
                current_identity=kwargs["current_identity"],
                previous_identity=kwargs["previous_identity"],
                family=kwargs["family"], client_code=kwargs["client_code"],
                period_type=kwargs["period_type"]))
        # `send_scope` is the REAL caller value: all four Eco jobs pass
        # `execution.send_scope`, which resolves to `"render_only"` for a
        # render-only execution. Constructing with `"normal"` here was the
        # blind spot that let the real rehearsal path fail at construction.
        service = emi.EcoDashboardLinkService(
            settings=emi.DashboardLinkSettings(enabled=True, dashboard_base_url=BASE_URL,
                                               render_only=True),
            client_id=CLIENT_ID, client_code="ALPHA00001", schema="public",
            period_type="weekly", period_start_date=WEEK_START,
            period_end_date=WEEK_END_EXCLUSIVE,
            send_scope=emi.RENDER_ONLY_SEND_SCOPE,
            mailer=emi.MAILER_ECO_WEEKLY, read_conn=_NullConn())
        outcome = service.link_for(identity_key="DRIVER-A",
                                   recipient_email="a@example.invalid")
        check("a render-only run still proves the link shape",
              outcome.status == emi.LinkStatus.RENDER_ONLY and outcome.has_link)
        check("it does not block the send", not outcome.blocks_send)
        check("the placeholder capability is not a real grant",
              outcome.capability_url.endswith("#k=" + "0" * 43))
        check("no ledger connection was opened", calls == [])
        check("and no publication was recorded",
              service.summary()["dashboard_linked_count"] == 0
              and service.summary()["dashboard_render_only_count"] == 1)
    finally:
        emi.build_delivery_snapshot_from_cursor = original_build  # type: ignore[assignment]
        emi._client_business_pg_conn = original_conn  # type: ignore[assignment]
    PASSED.append("a_render_only_run_publishes_nothing")


class _NullCursor:
    def __init__(self, statements: list):
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        # The savepoint discipline is real SQL on the lent connection, so the
        # stand-in has to accept it the way a real cursor would.
        self.statements.append(" ".join(str(sql).split()))


class _NullConn:
    """A cursor source for a path that issues no SQL of its own.

    Transactional, like the connection the Eco jobs actually lend, so the
    per-driver subtransaction the integration opens is exercised rather than
    skipped.
    """

    autocommit = False

    def __init__(self):
        self.statements: list = []

    def cursor(self, **kwargs):
        return _NullCursor(self.statements)


# --- 4. the existing SMTP lifecycle stays authoritative -------------------------


def test_the_integrated_jobs_never_reach_the_dashboard_email_provider() -> None:
    for path in ECO_JOBS:
        source = read(path)
        for forbidden in ("SmtpEmailProvider", "email_provider", "advance_delivery",
                          "record_delivery_intent", "record_provider_accepted",
                          "dashboard_email"):
            check("the Eco job does not reach the dashboard mailer",
                  forbidden not in source, f"{path}: {forbidden}")
        check("it uses the publication-only seam",
              "render_with_dashboard_link" in source, path)

    # The integration module's PROSE names `email_provider` to say it never
    # touches it, so this scans the code rather than the file: every import and
    # every dotted name in the module's AST.
    tree = ast.parse(read("jobs/ecodriving_dashboard/eco_mailing_integration.py"))
    imported: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    for forbidden in ("email_provider", "smtplib", "smtp",
                      "jobs.ecodriving_dashboard.email_provider"):
        check("the integration module imports no e-mail sender",
              not any(forbidden in module for module in imported), forbidden)
    for forbidden in ("SmtpEmailProvider", "advance_delivery", "send_message",
                      "submit", "reconcile"):
        check("the integration module calls no e-mail sender",
              forbidden not in names, forbidden)

    # And the seam itself: `ensure_capability` has no provider-facing step. Its
    # docstring names the steps it does NOT perform, so the body is compared
    # with the prose removed.
    seam = _function_body_source(read("jobs/ecodriving_dashboard/publisher.py"),
                                 "ensure_capability")
    for forbidden in ("_submit", "_reconcile", "_record_intent",
                      "_mark_remote_delivered", "provider"):
        check("the publication-only seam never touches a provider",
              forbidden not in seam, forbidden)
    handoff = _function_body_source(read("jobs/ecodriving_dashboard/publisher.py"),
                                    "_handoff")
    for forbidden in ("provider", "_submit", "record_delivery"):
        check("the handoff step never touches a provider",
              forbidden not in handoff, forbidden)
    PASSED.append("the_integrated_jobs_never_reach_the_dashboard_email_provider")


def _without_comments(source: str) -> str:
    """The CODE of a source slice, with `#` comments removed.

    A slice cannot be parsed on its own (it is an indented fragment), so this is
    a line scan rather than an AST walk. It is deliberately conservative: a `#`
    inside a string literal would strip the rest of that line too, which can only
    make a forbidden-token scan stricter, never looser.
    """
    lines = []
    for line in source.splitlines():
        stripped = line.split("#", 1)[0] if "#" in line else line
        lines.append(stripped)
    return "\n".join(lines)


def test_the_existing_smtp_safety_semantics_are_unchanged() -> None:
    weekly = read("jobs/ecodriving/job_eco_driving_weekly_email_notifications.py")
    # The reservation -> send -> mark lifecycle, in that order, still surrounds
    # the one SMTP call, and the dashboard step sits strictly BEFORE it.
    order = [weekly.index(marker) for marker in (
        "render_with_dashboard_link(",
        "reservation = reserve_send(",
        "acceptance = submit_prepared_email(",
        "mark_reserved_send_sent(",
    )]
    check("the dashboard link is obtained before the send is reserved",
          order == sorted(order), str(order))
    check("exactly one SMTP submission call site",
          weekly.count("submit_prepared_email(") == 2
          and weekly.count("acceptance = submit_prepared_email(") == 1,
          str(weekly.count("submit_prepared_email(")))

    # NO AUTOMATIC AMBIGUOUS RETRY. The SMTP failure path records the outcome
    # and either stops or raises; nothing loops back into a resend. Scanned with
    # COMMENTS REMOVED, because prose describing the retry policy is exactly
    # what the code is being checked against.
    failure_block = _without_comments(weekly[weekly.index("except Exception as exc:"):])
    for forbidden in ("for attempt", "while ", "retry", "submit_prepared_email"):
        check("no automatic retry was introduced around SMTP",
              forbidden not in failure_block, forbidden)
    check("an ambiguous SMTP result is durably distinguished, not marked failed",
          "mark_reserved_send_ambiguous(" in failure_block
          and "classify_exception(exc)" in failure_block, "weekly failure block")

    for path in ECO_JOBS:
        source = read(path)
        check("the send log remains the send authority",
              "reserve_send(" in source, path)
    PASSED.append("the_existing_smtp_safety_semantics_are_unchanged")


def test_no_capability_reaches_a_log_a_summary_or_an_audit_record() -> None:
    url = BASE_URL + "#k=" + CAP_A
    outcome = emi.DashboardLinkOutcome(
        emi.LinkStatus.LINKED, capability_url=url, snapshot_status="OK",
        delivery_state=DeliveryState.EXTERNAL_MAILER_HANDOFF, operation_id="op-1")
    audit = outcome.audit()
    check("the audit record carries no capability",
          CAP_A not in repr(audit) and "#k=" not in repr(audit))

    result = pub.CapabilityResult(
        pub.INVOCATION.COMPLETED, state=DeliveryState.EXTERNAL_MAILER_HANDOFF,
        next_action="EXTERNAL_MAILER_OWNS_DELIVERY", steps=2, capability_url=url)
    summary = result.summary()
    check("the invocation summary carries no capability",
          CAP_A not in repr(summary) and "#k=" not in repr(summary))
    check("but it still says a link exists", summary["capability_url_present"] is True)

    failed = emi.DashboardLinkOutcome(emi.LinkStatus.FAILED,
                                      failure_code="DASHBOARD_SNAPSHOT_FAILED",
                                      detail="ValueError")
    check("a failure detail is a bounded code, never a value",
          CAP_A not in repr(failed.audit()))

    # The job-level summary the run accounting publishes is built from the
    # service counters, which are counts and codes only.
    service = emi.EcoDashboardLinkService(
        settings=emi.DashboardLinkSettings(enabled=False),
        client_id=CLIENT_ID, client_code="ALPHA00001", schema="public",
        period_type="weekly", period_start_date=WEEK_START,
        period_end_date=WEEK_END_EXCLUSIVE, send_scope="normal",
        mailer=emi.MAILER_ECO_WEEKLY, read_conn=None)
    check("the run summary is counts and flags only",
          all(not isinstance(v, str) or "#k=" not in v
              for v in service.summary().values()))
    PASSED.append("no_capability_reaches_a_log_a_summary_or_an_audit_record")


def test_a_force_resend_is_the_same_logical_delivery() -> None:
    check("forced collapses onto normal", emi.delivery_send_scope("forced") == "normal")
    check("normal stays normal", emi.delivery_send_scope("normal") == "normal")
    check("a test send is its own scope", emi.delivery_send_scope("test") == "test")

    normal = DeliveryIdentity(client_id=CLIENT_ID, identity_key="DRIVER-A",
                              period_type="weekly", period_start_date=WEEK_START,
                              period_end_date=WEEK_END_EXCLUSIVE, send_scope="normal")
    test = DeliveryIdentity(client_id=CLIENT_ID, identity_key="DRIVER-A",
                            period_type="weekly", period_start_date=WEEK_START,
                            period_end_date=WEEK_END_EXCLUSIVE, send_scope="test")
    check("a force-resend converges on ONE publication operation",
          derive_operation_id(normal) == derive_operation_id(
              DeliveryIdentity(client_id=CLIENT_ID, identity_key="DRIVER-A",
                               period_type="weekly", period_start_date=WEEK_START,
                               period_end_date=WEEK_END_EXCLUSIVE,
                               send_scope=emi.delivery_send_scope("forced"))))
    check("a test send is a different operation",
          derive_operation_id(normal) != derive_operation_id(test))
    PASSED.append("a_force_resend_is_the_same_logical_delivery")


def test_the_integration_is_off_unless_the_run_opted_in() -> None:
    """OPT-IN, not "configured".

    THE CONTRACT THAT CHANGED. This module used to turn itself on whenever the
    deployment happened to be fully configured, which made provisioning
    `ECO_DASHBOARD_BASE_URL` / `ECO_DASHBOARD_PUBLISHER_URL` a silent mailing
    rollout. It is now off unless the invocation asked for it with
    `--with-dashboard`, and being asked for it is still only half of the
    requirement — see `test_eco_dashboard_optin_rollout_and_transport.py` for the
    client-level rollout permission that has to hold as well.
    """
    saved = {k: os.environ.pop(k, None) for k in
             (emi.ENV_DASHBOARD_BASE_URL, emi.ENV_PUBLISHER_ENDPOINT,
              emi.ENV_PUBLISHER_TOKEN)}
    try:
        settings = emi.DashboardLinkSettings.from_params({}, render_only=False)
        check("an unconfigured deployment keeps today's behaviour", not settings.enabled)

        for opt_in in ("with_dashboard", "dashboard_link"):
            try:
                emi.DashboardLinkSettings.from_params({opt_in: True},
                                                      render_only=False)
            except emi.DashboardIntegrationConfigurationError as error:
                check("and says exactly what is missing",
                      emi.ENV_PUBLISHER_ENDPOINT in str(error))
            else:
                raise AssertionError(
                    f"an explicit {opt_in} with no configuration must refuse")

        configured = emi.DashboardLinkSettings.from_params(
            {"dashboard_base_url": BASE_URL, "publisher_endpoint": ENDPOINT,
             "publisher_token": TOKEN}, render_only=False)
        check("configuration ALONE no longer turns it on", not configured.enabled)
        check("and it captures nothing from the environment either",
              not configured.dashboard_base_url and not configured.publisher_endpoint)

        opted_in = emi.DashboardLinkSettings.from_params(
            {"with_dashboard": True, "dashboard_base_url": BASE_URL,
             "publisher_endpoint": ENDPOINT, "publisher_token": TOKEN},
            render_only=False)
        check("the explicit opt-in plus configuration turns it on", opted_in.enabled)
        check("an explicit opt-out wins over configuration",
              not emi.DashboardLinkSettings.from_params(
                  {"dashboard_base_url": BASE_URL, "publisher_endpoint": ENDPOINT,
                   "publisher_token": TOKEN, "with_dashboard": False},
                  render_only=False).enabled)

        render = emi.DashboardLinkSettings.from_params(
            {"with_dashboard": True, "dashboard_base_url": BASE_URL},
            render_only=True)
        check("a render-only run needs no publisher at all",
              render.enabled and render.render_only and not render.publisher_endpoint)
        check("but a render-only run still has to opt in",
              not emi.DashboardLinkSettings.from_params(
                  {"dashboard_base_url": BASE_URL}, render_only=True).enabled)

        try:
            emi.DashboardLinkSettings.from_params(
                {"with_dashboard": True, "dashboard_base_url": BASE_URL,
                 "publisher_endpoint": "http://localhost.attacker.invalid",
                 "publisher_token": TOKEN}, render_only=False)
        except emi.DashboardIntegrationConfigurationError:
            pass
        else:
            raise AssertionError("a loopback-lookalike endpoint must be refused")
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    PASSED.append("the_integration_is_off_unless_the_run_opted_in")


def test_no_schedule_was_activated_and_no_second_orchestrator_exists() -> None:
    integration = read("jobs/ecodriving_dashboard/eco_mailing_integration.py")
    for forbidden in ("cron", "systemd", "schedule", "APScheduler", "timer"):
        check("the integration registers no schedule",
              forbidden not in integration.lower().replace("scheduler/runner", ""),
              forbidden)
    for path in ECO_JOBS:
        source = read(path)
        check("the fleet loop is still the Eco job's own",
              source.count("for row in candidates:") == 1, path)
        check("no dashboard orchestrator enumerates drivers",
              "job_eco_dashboard_publish" not in source, path)
    PASSED.append("no_schedule_was_activated_and_no_second_orchestrator_exists")


# ==============================================================================
# TIER B — the real ledger, the real 049 schema, a fake publisher transport
# ==============================================================================

ENV_DSN = "ECO_DASHBOARD_ECO_MAILING_TEST_DSN"


class FakePublishTransport:
    """Answers the publisher protocol without a network or a Worker.

    It is deliberately NOT a Worker model: the Worker's own semantics are
    proved against the real Worker elsewhere. Here it exists to make the HOST
    side observable — how many publications one fleet run causes, and what the
    host does with each answer.
    """

    def __init__(self):
        self.published: dict[str, str] = {}
        self.publish_calls = 0
        self.recover_calls = 0
        #: Every period type the host stated, publish and recover alike.
        self.period_types: list[str] = []
        self.maintenance_calls = 0
        self.refuse: set[str] = set()
        self.unrecoverable: set[str] = set()
        self.generations: dict[str, int] = {}

    def post(self, path, headers, body):
        raise AssertionError("the fake transport is driven through the client seam")


#: The Worker's authoritative period -> lifetime policy, restated here ONLY so
#: this double can produce the expiry a real publication would. The numbers
#: themselves are asserted against `worker/lib/capability_ttl.js` by
#: `test_driver_eco_dashboard_capability_ttl.py`, which is where they are
#: allowed to be checked; a drift there fails that suite rather than silently
#: making this double lie.
FAKE_TTL_DAYS = {"weekly": 10, "monthly": 60}


class FakeSecureDeliveryClient:
    credential = TOKEN

    def __init__(self, transport: FakePublishTransport):
        self.transport = transport

    def _expiry(self, period_type: str):
        """Refuse an unstated period exactly as both real ends do.

        The host must derive this from the ledger row, so a double that
        accepted anything would hide the one defect that matters here: a
        publication that does not say which reporting period it is for, and
        therefore gets a lifetime nobody chose.
        """
        if period_type not in FAKE_TTL_DAYS:
            raise AssertionError(
                f"a publication must state a known period type, got {period_type!r}")
        self.transport.period_types.append(period_type)
        return datetime.now(timezone.utc) + timedelta(days=FAKE_TTL_DAYS[period_type])

    def publish(self, *, operation_id: str, subject_ref: str, payload_digest: str,
                body: bytes, period_type: str):
        self.transport.publish_calls += 1
        expires_at = self._expiry(period_type)
        if operation_id in self.transport.refuse:
            raise sdc.SecureDeliveryError("PUBLICATION_REFUSED",
                                          "synthetic publication refusal", 500)
        capability = self.transport.published.get(operation_id)
        if capability is None:
            # A distinct 43-character capability per operation, deterministic
            # so a rerun can be proved to reuse rather than rotate.
            capability = (operation_id + "0" * 43)[:43].replace("_", "0").replace("-", "0")
            self.transport.published[operation_id] = capability
        return sdc.PublishOutcome(
            status=sdc.PUBLISHED, next_action=sdc.PERSIST_BEARER, http_status=201,
            operation={"state": "PUBLISHED", "bearer_generation": 1},
            capability=capability, capability_id="c" * 32,
            expires_at=expires_at,
            bearer_available=True)

    def recover(self, *, operation_id: str, period_type: str):
        """The EXPLICIT rotation the Worker offers: revoke, mint, return once.

        Modelled only as far as the host contract goes — a replacement bearer,
        a new capability id, a fresh expiry and the next bearer generation —
        because the Worker's own transactional semantics are proved against the
        real Worker elsewhere.
        """
        self.transport.recover_calls += 1
        expires_at = self._expiry(period_type)
        if operation_id in self.transport.unrecoverable:
            return sdc.PublishOutcome(
                status=sdc.NOT_RECOVERABLE, next_action=sdc.NONE, http_status=409,
                operation={"state": "DELIVERED"}, reason="ALREADY_DELIVERED")
        generation = self.transport.generations.get(operation_id, 1) + 1
        self.transport.generations[operation_id] = generation
        capability = (("R%d" % generation) + operation_id + "0" * 43)[:43] \
            .replace("_", "0").replace("-", "0")
        self.transport.published[operation_id] = capability
        return sdc.PublishOutcome(
            status=sdc.RECOVERED, next_action=sdc.PERSIST_BEARER, http_status=200,
            operation={"state": "GRANT_MINTED", "bearer_generation": generation},
            capability=capability, capability_id="d" * 32,
            expires_at=expires_at,
            bearer_available=True)

    def compact_authorization_state(self):
        """The Worker's expired-session compaction. Removes no grant.

        Modelled so the wiring is observed rather than swallowed: the service's
        housekeeping contains every failure, which would otherwise turn a
        missing method on this double into a silent pass.
        """
        self.transport.maintenance_calls += 1
        return {"status": "COMPACTED", "expired_sessions_removed": 0,
                "batch_full": False, "expired_capabilities_retained": True}

    def record_delivery(self, **kwargs):  # pragma: no cover - must never happen
        raise AssertionError("the publication-only path must not record delivery")


@dataclass
class SyntheticSnapshot:
    payload: bytes
    payload_digest: str
    current_identity: Any
    previous_identity: Any
    family: Any
    client_code: str
    period_type: str
    _status: str = "OK"

    @property
    def snapshot_status(self) -> str:
        return self._status


def _tier_b_context(dsn: str):
    import psycopg

    conn = psycopg.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.eco_dashboard_delivery_operation CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS public.eco_dashboard_delivery_operation_guard() CASCADE")
        # 049 creates the ledger, 050 adds the reviewed external-mailer
        # ownership contract the integration depends on, and 051 adds
        # `CAPABILITY_RETIRED` — without which the expired-capability sweep the
        # ordinary run performs at close would be refused by the state CHECK.
        # All three, in order.
        for migration in ("db/client_business/049_eco_dashboard_delivery_operation.sql",
                          "db/client_business/050_eco_dashboard_external_mailer_ownership.sql",
                          "db/client_business/051_eco_dashboard_capability_retirement.sql"):
            cur.execute(read(migration).replace("SET LOCAL ", "SET "))
    return conn


class CountingConnectionFactory:
    """Counts every client-business connection a fleet run opens.

    It builds them the way the REAL `_client_business_pg_conn` builds them —
    transaction mode chosen at connect time, business timezone configured with
    a real statement afterwards. That ordering is load-bearing: a stand-in that
    connected without the timezone statement would let a caller flip
    `autocommit` after the fact and never notice that psycopg refuses exactly
    that against a live session ("connection in transaction status INTRANS").
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.opened = 0
        self.autocommit_requests: list = []
        self.conns: list = []

    def __call__(self, cfg, *, autocommit: bool = False):
        import psycopg

        from api.timezone_utils import set_pg_session_timezone

        self.opened += 1
        self.autocommit_requests.append(autocommit)
        conn = set_pg_session_timezone(psycopg.connect(self.dsn, autocommit=autocommit))
        self.conns.append(conn)
        return conn

    def close(self):
        for conn in self.conns:
            conn.close()


def _service(dsn: str, factory, *, statuses: dict, snapshot_errors: dict,
             seen_identities: list, client_id: str = CLIENT_ID,
             period=(WEEK_START, WEEK_END_EXCLUSIVE)):
    """A real service with a synthetic snapshot source.

    The snapshot BUILDER is stubbed — its contract is proved by
    `test_eco_dashboard_snapshot.py` and re-proving it here would test the same
    code twice while needing a whole Eco schema. Everything else is real: the
    ledger, its constraints, the guard trigger, the fenced transitions, the
    publication-only state machine and the service's own caching and
    connection discipline.
    """
    def fake_build(cur, **kwargs):
        # Record what the integration ACTUALLY asked for, so the period
        # pass-through is observed rather than assumed.
        seen_identities.append((kwargs["identity_key"], kwargs["current_identity"],
                                kwargs["previous_identity"], kwargs["period_type"]))
        key = kwargs["identity_key"]
        if key in snapshot_errors:
            raise snapshot_errors[key]
        if statuses.get(key) == "__MISSING__":
            return None
        digest = ("%064x" % (abs(hash(key)) % (16 ** 60)))[:64]
        return SyntheticSnapshot(
            payload=b'{"synthetic":true}',
            payload_digest=digest,
            current_identity=kwargs["current_identity"],
            previous_identity=kwargs["previous_identity"],
            family=kwargs["family"], client_code=kwargs["client_code"],
            period_type=kwargs["period_type"],
            _status=statuses.get(key, "OK"))

    emi.build_delivery_snapshot_from_cursor = fake_build  # type: ignore[assignment]
    emi._client_business_pg_conn = factory  # type: ignore[assignment]

    service = emi.EcoDashboardLinkService(
        settings=emi.DashboardLinkSettings(
            enabled=True, dashboard_base_url=BASE_URL,
            publisher_endpoint=ENDPOINT, publisher_token=TOKEN),
        client_id=client_id, client_code="ALPHA00001", schema="public",
        period_type="weekly", period_start_date=period[0], period_end_date=period[1],
        send_scope="normal", mailer=emi.MAILER_ECO_WEEKLY,
        read_conn=factory.read_conn, cfg=object(), run_id="run-1")
    return service


def _install_fake_client(service, transport):
    """Replace the HTTP publisher client, leaving the ledger and states real."""
    original = service._publisher_services

    def build():
        services = original()
        services.client = FakeSecureDeliveryClient(transport)
        return services

    service._publisher_services = build  # type: ignore[assignment]
    return service


class _SnapshotSqlFailure:
    """Raises a GENUINE database error on the connection the Eco job lent.

    Not a Python exception standing in for one: the whole point of the finding
    is what PostgreSQL does to the surrounding transaction when a statement
    fails, and only a real failed statement does it.
    """

    def __init__(self, failing_keys: set):
        self.failing_keys = failing_keys
        self.cursors_used = 0

    def __call__(self, cur, **kwargs):
        self.cursors_used += 1
        if kwargs["identity_key"] in self.failing_keys:
            cur.execute("SELECT * FROM public.a_table_that_does_not_exist")
        digest = ("%064x" % (abs(hash(kwargs["identity_key"])) % (16 ** 60)))[:64]
        return SyntheticSnapshot(
            payload=b'{"synthetic":true}', payload_digest=digest,
            current_identity=kwargs["current_identity"],
            previous_identity=kwargs["previous_identity"],
            family=kwargs["family"], client_code=kwargs["client_code"],
            period_type=kwargs["period_type"])


def _tier_b_sql_error_isolation(dsn: str, factory, transport, admin) -> None:
    """One driver's SQL error must not become the whole run's SQLSTATE 25P02.

    The snapshot reads run on the Eco job's own connection — deliberately, so a
    fleet run does not open a session per driver. PostgreSQL aborts the entire
    transaction on any statement error, so catching the failure in Python is not
    enough: without a subtransaction the next statement on that connection, and
    every one after it, fails with "current transaction is aborted". That is
    the send-log insert for the driver that failed, and then every remaining
    driver's work.

    This drives the real thing: a real failed statement, on a real transactional
    connection that already carries uncommitted work, followed by the writes the
    job would actually make.
    """
    import psycopg

    opened_before = factory.opened
    with admin.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.eco_test_run_writes")
        cur.execute("CREATE TABLE public.eco_test_run_writes (note TEXT)")

    job_conn = psycopg.connect(dsn)     # the connection an Eco job owns
    try:
        with job_conn.cursor() as cur:
            # Uncommitted work from earlier in the same run.
            cur.execute("INSERT INTO public.eco_test_run_writes VALUES (%s)",
                        ("before-the-failing-driver",))

        failing = _SnapshotSqlFailure({"DRIVER-SQL-BOOM"})
        original = emi.build_delivery_snapshot_from_cursor
        emi.build_delivery_snapshot_from_cursor = failing  # type: ignore[assignment]
        try:
            service = emi.EcoDashboardLinkService(
                settings=emi.DashboardLinkSettings(
                    enabled=True, dashboard_base_url=BASE_URL,
                    publisher_endpoint=ENDPOINT, publisher_token=TOKEN),
                client_id=CLIENT_ID, client_code="ALPHA00001", schema="public",
                period_type="weekly", period_start_date=WEEK_START,
                period_end_date=WEEK_END_EXCLUSIVE, send_scope="normal",
                mailer=emi.MAILER_ECO_WEEKLY, read_conn=job_conn, cfg=object(),
                run_id="run-sql-isolation")
            _install_fake_client(service, transport)
            try:
                boom = service.link_for(identity_key="DRIVER-SQL-BOOM",
                                        recipient_email="boom-sql@example.invalid")
                check("the driver whose read failed gets no link", not boom.has_link)
                check("and it is a bounded per-driver dashboard failure",
                      boom.blocks_send
                      and boom.failure_code == "DASHBOARD_SNAPSHOT_FAILED",
                      str(boom.failure_code))

                # THE ASSERTION THE FINDING IS ABOUT.
                with job_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO public.eco_test_run_writes VALUES (%s)",
                        ("the-failing-driver-send-log",))
                    cur.execute("SELECT count(*) FROM public.eco_test_run_writes")
                    check("the shared transaction is usable immediately after",
                          cur.fetchone()[0] == 2)

                after = service.link_for(identity_key="DRIVER-SQL-NEXT",
                                         recipient_email="next-sql@example.invalid")
                check("the NEXT driver is processed normally", after.has_link,
                      f"{after.failure_code} {after.detail}")
                check("and reaches the handoff state a normal e-mail needs",
                      after.delivery_state == DeliveryState.EXTERNAL_MAILER_HANDOFF)

                check("this run opened ONE ledger connection, not one per candidate",
                      factory.opened == opened_before + 1
                      and service.summary()["dashboard_ledger_connections_opened"] == 1,
                      f"{opened_before} -> {factory.opened}")
                check("the failure was counted once, without a secret",
                      service.summary()["dashboard_failure_codes"]
                      == {"DASHBOARD_SNAPSHOT_FAILED": 1})
            finally:
                service.close()

            job_conn.commit()
            with admin.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.eco_test_run_writes")
                check("nothing the job had already written was rolled back",
                      cur.fetchone()[0] == 2)

            # fail_fast differs only in what the JOB does with the outcome: it
            # raises and rolls its own transaction back. The dashboard-side
            # contract — a bounded failure and a usable session — is the same,
            # which is what makes that rollback the job's decision rather than
            # PostgreSQL's.
            for path in ECO_JOBS:
                job = read(path)
                marker = job.index("dashboard_link_blocked_count\"] += 1")
                tail = job[marker:marker + 2000]
                check("fail_fast raises only after the failure is recorded",
                      "if fail_fast:" in tail and "raise RuntimeError(" in tail, path)
        finally:
            emi.build_delivery_snapshot_from_cursor = original  # type: ignore[assignment]
    finally:
        job_conn.close()
    PASSED.append("tier_b: a SQL error for one driver leaves the shared transaction usable")


def _tier_b_expiry_lifecycle(dsn: str, factory, transport, admin) -> None:
    """An expired capability is never handed to the mailer as a usable link."""
    seen: list = []
    service = _service(dsn, factory, statuses={}, snapshot_errors={},
                       seen_identities=seen)
    _install_fake_client(service, transport)
    try:
        identity = DeliveryIdentity(
            client_id=CLIENT_ID, identity_key="DRIVER-EXPIRY", period_type="weekly",
            period_start_date=WEEK_START, period_end_date=WEEK_END_EXCLUSIVE,
            send_scope="normal")
        operation_id = derive_operation_id(identity)
        address = "expiry@example.invalid"

        first = service.link_for(identity_key="DRIVER-EXPIRY", recipient_email=address)
        check("the first run produces a link", first.has_link,
              f"{first.failure_code} {first.detail}")

        # --- 1. a NON-EXPIRED handoff rerun reuses the same capability --------
        recovers_before = transport.recover_calls
        publishes_before = transport.publish_calls
        rerun = service.link_for(identity_key="DRIVER-EXPIRY", recipient_email=address)
        check("a live capability is reused, not rotated",
              rerun.capability_url == first.capability_url)
        check("nothing was published or recovered for it",
              transport.recover_calls == recovers_before
              and transport.publish_calls == publishes_before)

        with admin.cursor() as cur:
            cur.execute("""SELECT capability_secret, subject_ref, payload_digest,
                                  recipient_email, recipient_identity, bearer_generation
                             FROM public.eco_dashboard_delivery_operation
                            WHERE operation_id = %s""", (operation_id,))
            (stale_bearer, subject_ref, payload_digest, recipient,
             recipient_identity, generation) = cur.fetchone()
        check("the live bearer is what the link carries",
              stale_bearer and stale_bearer in first.capability_url)

        # --- 2. the same capability, now EXPIRED ------------------------------
        with admin.cursor() as cur:
            cur.execute("""UPDATE public.eco_dashboard_delivery_operation
                              SET capability_expires_at = now() - interval '1 day'
                            WHERE operation_id = %s""", (operation_id,))
        rotated = service.link_for(identity_key="DRIVER-EXPIRY", recipient_email=address)
        check("an expired handoff still yields a link", rotated.has_link,
              f"{rotated.failure_code} {rotated.detail}")
        check("but NOT the expired one",
              rotated.capability_url != first.capability_url)
        check("the expired bearer is not in the returned URL",
              stale_bearer not in rotated.capability_url)
        check("it came from the explicit recovery operation, not a re-publish",
              transport.recover_calls == recovers_before + 1
              and transport.publish_calls == publishes_before, str(transport.recover_calls))
        check("and the delivery is handed over again",
              rotated.delivery_state == DeliveryState.EXTERNAL_MAILER_HANDOFF)

        with admin.cursor() as cur:
            cur.execute("""SELECT state, capability_secret, capability_expires_at,
                                  subject_ref, payload_digest, recipient_email,
                                  recipient_identity, bearer_generation, operation_id,
                                  provider_idempotency_key, provider_name,
                                  bearer_cleared_at,
                                  metadata_json->'external_mailer_handoff'->>'mailer'
                             FROM public.eco_dashboard_delivery_operation
                            WHERE operation_id = %s""", (operation_id,))
            row = cur.fetchone()
        (state, bearer, expires_at, subject2, digest2, recipient2, identity2,
         generation2, operation2, provider_key, provider_name, cleared_at, mailer) = row

        # --- 3. the URL handed over is never already expired ------------------
        check("the persisted capability is live",
              expires_at > datetime.now(timezone.utc), str(expires_at))
        check("the returned URL carries the CURRENT bearer",
              bearer and bearer in rotated.capability_url)

        # --- 4. rotation preserved the delivery identity ----------------------
        check("one logical delivery, unchanged",
              (operation2, subject2, digest2) == (operation_id, subject_ref, payload_digest))
        check("the recipient binding is untouched",
              (recipient2, identity2) == (recipient, recipient_identity))
        check("the mailer that owns the send is still recorded",
              mailer == emi.MAILER_ECO_WEEKLY, str(mailer))
        check("and no provider identity was bound by the rotation",
              provider_key is None and provider_name is None)
        check("the bearer generation advanced exactly once",
              generation2 == generation + 1, f"{generation} -> {generation2}")

        # --- 5. the stale bearer was destroyed, not kept ----------------------
        check("the expired bearer is gone from the row", bearer != stale_bearer)
        check("no dead bearer survives anywhere in the table",
              _bearer_absent_from_table(admin, stale_bearer))
        check("the replacement is a live bearer, so no clearance is pending",
              cleared_at is None)

        with admin.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.eco_dashboard_delivery_operation "
                        "WHERE identity_key = 'DRIVER-EXPIRY'")
            check("still exactly one row for the logical delivery", cur.fetchone()[0] == 1)

        # --- 6. a rotation that CANNOT succeed refuses, it does not improvise --
        transport.unrecoverable.add(operation_id)
        with admin.cursor() as cur:
            cur.execute("""UPDATE public.eco_dashboard_delivery_operation
                              SET capability_expires_at = now() - interval '1 day'
                            WHERE operation_id = %s""", (operation_id,))
        refused = service.link_for(identity_key="DRIVER-EXPIRY", recipient_email=address)
        check("an unrecoverable expiry yields no link", not refused.has_link)
        check("and blocks that driver's e-mail", refused.blocks_send)
        with admin.cursor() as cur:
            cur.execute("""SELECT state, capability_secret, operator_action_required,
                                  bearer_cleared_at, failure_code
                             FROM public.eco_dashboard_delivery_operation
                            WHERE operation_id = %s""", (operation_id,))
            state3, bearer3, operator, cleared3, failure_code = cur.fetchone()
        check("it is escalated rather than guessed at",
              state3 == DeliveryState.OPERATOR_REQUIRED and operator is True, str(state3))
        # THE MINIMISATION RULE, OBSERVED AT REST. The rotation destroys the
        # expired bearer as it leaves the handoff, so a delivery that then fails
        # to recover is parked WITHOUT the dead secret rather than keeping it
        # because the row happens to be terminal.
        check("and the unusable bearer is not retained", bearer3 is None)
        check("its destruction is stamped", cleared3 is not None)
        check("no bearer of any generation survives in the row",
              _bearer_absent_from_table(admin, bearer))

        # --- 7. no SMTP claim was made anywhere on this path -------------------
        check("the publication-only path never recorded a delivery phase",
              transport.recover_calls == recovers_before + 2
              and transport.publish_calls == publishes_before)
    finally:
        service.close()
    PASSED.append("tier_b: an expired capability is rotated through recovery, never handed over")


def _bearer_absent_from_table(admin, bearer: str) -> bool:
    with admin.cursor() as cur:
        cur.execute("""SELECT count(*) FROM public.eco_dashboard_delivery_operation
                        WHERE capability_secret = %s
                           OR COALESCE(failure_detail, '') LIKE %s
                           OR COALESCE(failure_code, '') LIKE %s
                           OR metadata_json::text LIKE %s""",
                    (bearer, f"%{bearer}%", f"%{bearer}%", f"%{bearer}%"))
        return cur.fetchone()[0] == 0


def _tier_b_external_mailer_boundary(dsn: str, transport, admin) -> None:
    """A delivery the Eco mailer owns can never become a provider submission.

    OWNERSHIP IS BOUND AT CREATION, WHICH IS WHY THIS HOLDS IN EVERY STATE.
    `ensure_capability` writes `external_mailer` in the INSERT that creates the
    row, so there is no window — not one statement wide — in which an
    Eco-created delivery exists without saying who owns its send accounting. The
    earlier design annotated ownership only when the handoff was recorded, which
    left `PREPARED` and `CAPABILITY_PERSISTED` adoptable by the provider
    lifecycle: it could claim the lease, count an attempt, bind an idempotency
    key and submit a SECOND, dashboard-specific message to a driver the Eco job
    had already mailed.

    Each crash state below is the row a process death would actually leave
    behind. For every one of them the provider path must refuse BEFORE the lease
    is claimed, and the refusal is proved by comparing the whole row before and
    after — not merely by reading the returned code.
    """
    import psycopg

    from jobs.ecodriving_dashboard import email_provider as ep
    from jobs.ecodriving_dashboard.delivery_ledger import DeliveryLedger

    #: Every column a provider-driven invocation would have to touch to do any
    #: work at all. If none of them moved, nothing was adopted.
    WITNESS = (
        "state", "external_mailer", "lease_owner", "lease_expires_at",
        "attempt_count", "provider_name", "provider_idempotency_key",
        "provider_backend_id", "provider_message_fingerprint",
        "provider_bound_capability_id", "provider_bound_bearer_generation",
        "provider_message_id", "provider_attempts", "provider_submitted_at",
        "provider_accepted_at", "remote_delivered_at", "finalized_at",
        "capability_id", "capability_secret", "bearer_generation",
    )

    def snapshot(operation_id: str):
        with admin.cursor() as cur:
            cur.execute(f"""SELECT {', '.join(WITNESS)}
                              FROM public.eco_dashboard_delivery_operation
                             WHERE operation_id = %s""", (operation_id,))
            return cur.fetchone()

    conn = psycopg.connect(dsn)
    conn.autocommit = True
    try:
        services = pub.PublisherServices(
            ledger=DeliveryLedger(conn),
            client=FakeSecureDeliveryClient(transport),
            config=pub.PublisherConfig(dashboard_base_url=BASE_URL,
                                       message_id_domain="eco-dashboard.invalid"),
            provider=ep.FakeEmailProvider(),
        )
        identity = DeliveryIdentity(
            client_id=CLIENT_ID, identity_key="DRIVER-1", period_type="weekly",
            period_start_date=WEEK_START, period_end_date=WEEK_END_EXCLUSIVE,
            send_scope="normal")
        operation_id = derive_operation_id(identity)
        with admin.cursor() as cur:
            cur.execute("""SELECT state, payload_digest, external_mailer FROM
                             public.eco_dashboard_delivery_operation
                            WHERE operation_id = %s""", (operation_id,))
            state, digest, owner = cur.fetchone()
        check("the fixture delivery is a handed-over one",
              state == DeliveryState.EXTERNAL_MAILER_HANDOFF, str(state))
        check("and ownership is durable on the row itself",
              owner == emi.MAILER_ECO_WEEKLY, str(owner))

        # ------------------------------------------------------------------
        # THE CRASH MATRIX. Each entry is the committed row a process death at
        # that point leaves behind, reconstructed from the handed-over fixture.
        # ------------------------------------------------------------------
        # Each entry writes the WHOLE row shape, so the matrix does not depend
        # on the order it is walked in.
        BEARER = "R" * 43
        BEARER_DIGEST = hashlib.sha256(BEARER.encode("utf-8")).hexdigest()
        CAP_ID = "d" * 32

        no_bearer = ("capability_secret = NULL, capability_id = NULL, "
                     "capability_digest = NULL, capability_expires_at = NULL, "
                     "bearer_generation = 0, bearer_persisted_at = NULL, "
                     "bearer_cleared_at = NULL")
        with_bearer = (
            "capability_secret = %(bearer)s, capability_id = %(cap_id)s, "
            "capability_digest = %(digest)s, "
            "capability_expires_at = now() + interval '14 days', "
            "bearer_persisted_at = now(), bearer_cleared_at = NULL")
        cleared_bearer = (
            "capability_secret = NULL, capability_id = %(cap_id)s, "
            "capability_digest = %(digest)s, "
            "capability_expires_at = now() - interval '1 day', "
            "bearer_persisted_at = now(), bearer_cleared_at = now()")

        crash_states = (
            ("immediately after operation creation",
             f"state = 'PREPARED', {no_bearer}, metadata_json = '{{}}'::jsonb, "
             "failure_phase = NULL, failure_code = NULL"),
            ("at PREPARED, after a publication attempt that persisted nothing",
             f"state = 'PREPARED', {no_bearer}, metadata_json = '{{}}'::jsonb, "
             "failure_phase = 'PUBLICATION', failure_code = 'TIMEOUT'"),
            ("at CAPABILITY_PERSISTED, before the handoff was recorded",
             f"state = 'CAPABILITY_PERSISTED', {with_bearer}, "
             "bearer_generation = 1, metadata_json = '{}'::jsonb, "
             "failure_phase = NULL, failure_code = NULL"),
            ("mid-rotation of an expired bearer, in BEARER_RECOVERY_REQUIRED",
             f"state = 'BEARER_RECOVERY_REQUIRED', {cleared_bearer}, "
             "bearer_generation = 1, metadata_json = '{}'::jsonb, "
             "failure_phase = 'PUBLICATION', failure_code = 'CAPABILITY_EXPIRED'"),
            ("after a RECOVERED capability was persisted, handoff not re-recorded",
             f"state = 'CAPABILITY_PERSISTED', {with_bearer}, "
             "bearer_generation = 2, metadata_json = '{}'::jsonb, "
             "failure_phase = NULL, failure_code = NULL"),
            ("in the recorded handoff itself",
             f"state = 'EXTERNAL_MAILER_HANDOFF', {with_bearer}, "
             "bearer_generation = 2, "
             "metadata_json = jsonb_build_object('external_mailer_handoff', "
             "  jsonb_build_object('mailer', %(mailer)s::text)), "
             "failure_phase = NULL, failure_code = NULL"),
        )

        for label, assignments in crash_states:
            with admin.cursor() as cur:
                cur.execute(
                    "UPDATE public.eco_dashboard_delivery_operation SET "
                    + assignments + " WHERE operation_id = %(operation_id)s",
                    {"bearer": BEARER, "cap_id": CAP_ID, "digest": BEARER_DIGEST,
                     "mailer": emi.MAILER_ECO_WEEKLY, "operation_id": operation_id})
            before = snapshot(operation_id)
            check(f"the crash state carries ownership ({label})",
                  before[1] == emi.MAILER_ECO_WEEKLY, str(before[1]))

            result = pub.advance_delivery(
                services, identity=identity, payload_digest=digest,
                recipient_email="driver-1@example.invalid", body=b'{"synthetic":true}',
                owner=pub.owner_token("run-boundary"), run_id="run-boundary")

            check(f"the provider path refuses a delivery crashed {label}",
                  result.conflict_code == "EXTERNAL_MAILER_OWNS_DELIVERY",
                  f"{result.invocation} {result.conflict_code} {result.detail}")
            check("it names the lifecycle that owns the send",
                  result.next_action == "EXTERNAL_MAILER_OWNS_DELIVERY")
            check("and nothing was progressed", result.steps == 0)

            after = snapshot(operation_id)
            check(f"NOT ONE column moved ({label})", before == after,
                  f"before={before} after={after}")
            check("no lease was claimed", after[2] is None and after[3] is None)
            check("the attempt count did not move", before[4] == after[4])
            check("no provider identity exists", all(v is None for v in after[5:12]))
            check("no provider attempt was counted", after[12] == 0)

        # ------------------------------------------------------------------
        # ...AND THE OTHER HALF, WHICH IS WHAT MAKES THE FIX A BOUNDARY RATHER
        # THAN A KILL SWITCH: an ordinary provider-owned dashboard delivery is
        # unaffected and still runs its own lifecycle.
        # ------------------------------------------------------------------
        provider_identity = DeliveryIdentity(
            client_id=CLIENT_ID, identity_key="DRIVER-PROVIDER-OWNED",
            period_type="weekly", period_start_date=WEEK_START,
            period_end_date=WEEK_END_EXCLUSIVE, send_scope="normal")
        provider_operation = derive_operation_id(provider_identity)
        body = b'{"synthetic":"provider-owned"}'
        provider_digest = hashlib.sha256(body).hexdigest()
        # Bounded to ONE step on purpose. This file's fake client refuses every
        # provider-facing call, because the publication-only seam must never
        # make one; the full provider lifecycle is proved in
        # `test_driver_eco_dashboard_publisher_lifecycle.py`. What has to be
        # shown HERE is only that the ownership boundary does not swallow an
        # ordinary delivery: it is not refused, it claims its lease, it counts
        # its attempt and it performs its first durable step.
        provider_result = pub.advance_delivery(
            services, identity=provider_identity, payload_digest=provider_digest,
            recipient_email="provider-owned@example.invalid", body=body,
            owner=pub.owner_token("run-provider"), run_id="run-provider",
            max_steps=1)
        check("a provider-owned delivery is NOT refused",
              provider_result.conflict_code != "EXTERNAL_MAILER_OWNS_DELIVERY",
              f"{provider_result.invocation} {provider_result.conflict_code} "
              f"{provider_result.detail}")
        check("and it progressed through its own lifecycle",
              provider_result.steps > 0, str(provider_result.steps))
        with admin.cursor() as cur:
            cur.execute("""SELECT external_mailer, state, attempt_count
                             FROM public.eco_dashboard_delivery_operation
                            WHERE operation_id = %s""", (provider_operation,))
            owned_by, provider_state, attempts = cur.fetchone()
        check("the provider-owned row carries no external ownership",
              owned_by is None, str(owned_by))
        check("and it claimed its lease like any ordinary delivery",
              attempts >= 1, f"{provider_state} attempts={attempts}")
        check("its own lifecycle advanced it past PREPARED",
              provider_state == DeliveryState.CAPABILITY_PERSISTED, str(provider_state))

        # ------------------------------------------------------------------
        # OWNERSHIP CANNOT BE ACQUIRED, LOST OR EDITED AFTERWARDS.
        # ------------------------------------------------------------------
        for label, sql, params in (
            ("cleared", "UPDATE public.eco_dashboard_delivery_operation "
                        "SET external_mailer = NULL WHERE operation_id = %s",
             (operation_id,)),
            ("changed", "UPDATE public.eco_dashboard_delivery_operation "
                        "SET external_mailer = %s WHERE operation_id = %s",
             ("some_other_mailer", operation_id)),
            ("acquired after the fact",
             "UPDATE public.eco_dashboard_delivery_operation "
             "SET external_mailer = %s WHERE operation_id = %s",
             (emi.MAILER_ECO_WEEKLY, provider_operation)),
        ):
            try:
                with admin.cursor() as cur:
                    cur.execute(sql, params)
            except Exception as error:
                # The admin connection is autocommit, so a refused statement
                # leaves nothing to roll back.
                check(f"ownership cannot be {label}",
                      "OWNERSHIP_IMMUTABLE" in str(error), str(error))
                continue
            raise AssertionError(f"ownership was {label}")
    finally:
        conn.close()
    PASSED.append("tier_b: the provider lifecycle never adopts an externally-owned delivery")

def _tier_b_retirement_on_close(dsn: str, factory, transport, admin) -> None:
    """THE WIRING: ending an ordinary mailing run destroys expired bearers.

    The sweep itself is proved exhaustively in
    `test_eco_dashboard_capability_retirement_postgres.py`. What is proved here
    is the thing that suite cannot see — that the ordinary Eco mailing lifecycle
    actually invokes it, on the connection it already holds, without anybody
    re-mailing the historical period whose link went stale.
    """
    seen: list = []
    service = _service(dsn, factory, statuses={}, snapshot_errors={},
                       seen_identities=seen)
    _install_fake_client(service, transport)
    try:
        stale = service.link_for(identity_key="DRIVER-RETIRE-OLD",
                                 recipient_email="old@example.invalid")
        fresh = service.link_for(identity_key="DRIVER-RETIRE-NEW",
                                 recipient_email="new@example.invalid")
        check("both drivers were linked", stale.has_link and fresh.has_link,
              f"{stale.failure_code} / {fresh.failure_code}")

        # One grant is wound past its own expiry. Nothing else changes, and
        # nothing re-mails either period.
        with admin.cursor() as cur:
            cur.execute("""UPDATE public.eco_dashboard_delivery_operation
                              SET capability_expires_at = now() - interval '1 day'
                            WHERE operation_id = %s""", (stale.operation_id,))

        maintenance_before = transport.maintenance_calls
        service.close()

        # `admin` yields tuples, so the columns are read positionally.
        with admin.cursor() as cur:
            cur.execute("""SELECT operation_id, state, capability_secret,
                                  capability_digest
                             FROM public.eco_dashboard_delivery_operation
                            WHERE operation_id = ANY(%s)""",
                        ([stale.operation_id, fresh.operation_id],))
            rows = {r[0]: r for r in cur.fetchall()}

        old_row = rows[stale.operation_id]
        new_row = rows[fresh.operation_id]
        check("the expired delivery was retired by the ordinary run",
              old_row[1] == DeliveryState.CAPABILITY_RETIRED, str(old_row[1]))
        check("its raw bearer no longer exists", old_row[2] is None)
        check("but which grant it held is still provable", old_row[3] is not None)
        check("the still-valid delivery is untouched",
              new_row[1] == DeliveryState.EXTERNAL_MAILER_HANDOFF
              and new_row[2] is not None, str(new_row[1]))
        check("and the run reported the housekeeping it did",
              service.maintenance["capabilities_retired"] == 1,
              str(service.maintenance))
        check("no housekeeping failure was swallowed",
              service.maintenance["failure_code"] is None,
              str(service.maintenance["failure_code"]))
        check("the housekeeping numbers stay out of the run summary, which "
              "every job reads BEFORE close()",
              "dashboard_capabilities_retired_count" not in service.summary(),
              str(sorted(service.summary())))
        check("the Worker's expired-session compaction was asked for too",
              transport.maintenance_calls == maintenance_before + 1)
    finally:
        service.close()


def _tier_b_retirement_without_any_publication(dsn: str, factory, transport, admin) -> None:
    """THE GUARANTEE: cleanup must not depend on publishing anything.

    This is the case the first version of the sweep got wrong. It ran only when
    a ledger connection already existed — which happens only when some candidate
    reached the publisher — so a run whose drivers were all already sent, whose
    roster was empty, whose snapshots were all refused, or that was simply
    filtered down to nothing swept NOTHING. A bearer that expired months ago
    then survived every such run, and would have been destroyed only if somebody
    happened to re-mail that exact historical period.

    So the invocation modelled here calls `link_for` ZERO times. It is a service
    that was constructed, found nothing to do, and closed — the shape of a quiet
    week. The expired delivery it must still clean up was published by an
    EARLIER run, on a different service instance, exactly as a historical period
    would have been.
    """
    seen: list = []

    # --- an earlier run publishes two deliveries and goes away ---------------
    earlier = _service(dsn, factory, statuses={}, snapshot_errors={},
                       seen_identities=seen)
    _install_fake_client(earlier, transport)
    stale = earlier.link_for(identity_key="DRIVER-QUIET-OLD",
                             recipient_email="quiet-old@example.invalid")
    live = earlier.link_for(identity_key="DRIVER-QUIET-NEW",
                            recipient_email="quiet-new@example.invalid")
    check("the earlier run linked both drivers", stale.has_link and live.has_link,
          f"{stale.failure_code} / {live.failure_code}")
    earlier.close()

    with admin.cursor() as cur:
        cur.execute("""UPDATE public.eco_dashboard_delivery_operation
                          SET capability_expires_at = now() - interval '1 day'
                        WHERE operation_id = %s""", (stale.operation_id,))

    # --- a LATER run that publishes nothing at all ---------------------------
    quiet = _service(dsn, factory, statuses={}, snapshot_errors={},
                     seen_identities=seen)
    _install_fake_client(quiet, transport)
    publishes_before = transport.publish_calls
    recovers_before = transport.recover_calls

    check("the quiet run has not touched the publisher path",
          quiet._services is None and quiet._ledger_conn is None)
    quiet.close()

    check("and it never did — zero publications, zero recoveries",
          transport.publish_calls == publishes_before
          and transport.recover_calls == recovers_before,
          f"{transport.publish_calls - publishes_before} publishes")
    check("no link was produced by the quiet run",
          quiet.summary()["dashboard_linked_count"] == 0)
    check("the quiet run still performed maintenance", quiet.maintenance["ran"])
    check("and opened its own ledger connection to do it",
          quiet.maintenance["ledger_connections_opened"] == 1,
          str(quiet.maintenance))
    check("which is NOT counted as a publish-path connection",
          quiet.summary()["dashboard_ledger_connections_opened"] == 0,
          str(quiet.summary()["dashboard_ledger_connections_opened"]))
    check("no housekeeping failure was swallowed",
          quiet.maintenance["failure_code"] is None,
          str(quiet.maintenance["failure_code"]))
    check("exactly the expired delivery was retired",
          quiet.maintenance["capabilities_retired"] == 1,
          str(quiet.maintenance))

    with admin.cursor() as cur:
        cur.execute("""SELECT operation_id, state, capability_secret,
                              capability_digest, capability_id, bearer_generation
                         FROM public.eco_dashboard_delivery_operation
                        WHERE operation_id = ANY(%s)""",
                    ([stale.operation_id, live.operation_id],))
        rows = {r[0]: r for r in cur.fetchall()}
    old_row, new_row = rows[stale.operation_id], rows[live.operation_id]

    check("the expired delivery is retired by a run that published nothing",
          old_row[1] == DeliveryState.CAPABILITY_RETIRED, str(old_row[1]))
    check("its plaintext bearer is gone", old_row[2] is None)
    check("the audit identity that says WHICH grant it was survives",
          old_row[3] is not None and old_row[4] is not None and old_row[5] >= 1)

    # --- the still-valid delivery is untouched and still reusable ------------
    check("the still-valid delivery is not retired",
          new_row[1] == DeliveryState.EXTERNAL_MAILER_HANDOFF, str(new_row[1]))
    check("and it still holds the bearer its reuse semantics require",
          new_row[2] is not None)

    reuse = _service(dsn, factory, statuses={}, snapshot_errors={},
                     seen_identities=seen)
    _install_fake_client(reuse, transport)
    try:
        again = reuse.link_for(identity_key="DRIVER-QUIET-NEW",
                               recipient_email="quiet-new@example.invalid")
        check("the untouched capability is still reusable by a same-period resend",
              again.has_link and again.capability_url == live.capability_url,
              f"{again.failure_code} {again.detail}")
        check("and reusing it rotated nothing",
              transport.recover_calls == recovers_before,
              str(transport.recover_calls - recovers_before))
    finally:
        reuse.close()

    # --- the sweep is a no-op the second time --------------------------------
    with admin.cursor() as cur:
        cur.execute("""SELECT updated_at, bearer_cleared_at, metadata_json
                         FROM public.eco_dashboard_delivery_operation
                        WHERE operation_id = %s""", (stale.operation_id,))
        before_repeat = cur.fetchone()

    repeat = _service(dsn, factory, statuses={}, snapshot_errors={},
                      seen_identities=seen)
    _install_fake_client(repeat, transport)
    repeat.close()
    check("a second quiet run retires nothing",
          repeat.maintenance["capabilities_retired"] == 0, str(repeat.maintenance))
    check("and reports no failure", repeat.maintenance["failure_code"] is None,
          str(repeat.maintenance["failure_code"]))

    with admin.cursor() as cur:
        cur.execute("""SELECT updated_at, bearer_cleared_at, metadata_json
                         FROM public.eco_dashboard_delivery_operation
                        WHERE operation_id = %s""", (stale.operation_id,))
        after_repeat = cur.fetchone()
    check("the retired row was not rewritten — no duplicate retirement evidence",
          after_repeat == before_repeat, "a repeat sweep moved a retired row")

    # PROTECTED STATES ARE NOT RE-PROVED HERE. `PROVIDER_AMBIGUOUS` and
    # `OPERATOR_REQUIRED` are unreachable on this path by construction — every
    # delivery it creates is external-mailer-owned, and migration 050 makes the
    # provider states physically impossible for such a row. The exclusions
    # belong to the sweep statement itself, which is the same statement however
    # it is invoked, and are proved against real fixtures in
    # `test_eco_dashboard_capability_retirement_postgres.py`. What this path
    # owns, and proves above, is that the sweep RUNS with no publication and
    # spares a still-valid capability.


def tier_b(dsn: str) -> None:
    import psycopg

    admin = _tier_b_context(dsn)
    try:
        # --- fleet run: many drivers, ONE ledger connection -------------------
        factory = CountingConnectionFactory(dsn)
        factory.read_conn = psycopg.connect(dsn)
        seen: list = []
        transport = FakePublishTransport()
        original_build = emi.build_delivery_snapshot_from_cursor
        original_conn = emi._client_business_pg_conn
        try:
            service = _service(dsn, factory, statuses={"DRIVER-3": "INSUFFICIENT_DISTANCE"},
                               snapshot_errors={}, seen_identities=seen)
            _install_fake_client(service, transport)
            drivers = [f"DRIVER-{i}" for i in range(1, 9)]
            urls = {}
            for driver in drivers:
                outcome = service.link_for(identity_key=driver,
                                           recipient_email=f"{driver.lower()}@example.invalid")
                check("every driver got a link", outcome.has_link,
                      f"{driver}: {outcome.failure_code} {outcome.detail}")
                check("and the handoff is durable",
                      outcome.delivery_state in emi.LINKABLE_DELIVERY_STATES
                      and outcome.delivery_state == DeliveryState.EXTERNAL_MAILER_HANDOFF,
                      driver)
                urls[driver] = outcome.capability_url

            check("no two drivers share a capability",
                  len(set(urls.values())) == len(drivers))
            check("one publication per driver, no more",
                  transport.publish_calls == len(drivers), str(transport.publish_calls))
            check("EXACTLY ONE additional connection for the whole fleet",
                  factory.opened == 1, str(factory.opened))
            check("the run summary reports it",
                  service.summary()["dashboard_ledger_connections_opened"] == 1)
            check("it was asked for autocommit at CONSTRUCTION, not afterwards",
                  factory.autocommit_requests == [True],
                  str(factory.autocommit_requests))
            check("and the live session really is in autocommit",
                  factory.conns[0].autocommit is True)
            check("a below-threshold driver was linked, not failed",
                  service.summary()["dashboard_snapshot_status_counts"].get(
                      "INSUFFICIENT_DISTANCE") == 1)
            PASSED.append("tier_b: a fleet run opens one connection and publishes once per driver")

            # --- the authoritative period reached the snapshot builder --------
            for _key, current, previous, period_type in seen:
                check("the builder was given the job's period",
                      current.period_start_date == WEEK_START
                      and current.period_end_date_exclusive == WEEK_END_EXCLUSIVE
                      and period_type == "weekly")
                check("and the preceding CUMULATIVE period as the basis",
                      previous is not None and previous.period_sequence_in_month == 2)
            PASSED.append("tier_b: the authoritative period is passed through unchanged")

            # --- rerun: same link, no new publication, one row ----------------
            before = transport.publish_calls
            rerun = service.link_for(identity_key="DRIVER-1",
                                     recipient_email="driver-1@example.invalid")
            check("a rerun returns the SAME link", rerun.capability_url == urls["DRIVER-1"])
            check("and publishes nothing new",
                  transport.publish_calls == before, str(transport.publish_calls))
            check("and stays in the handoff state",
                  rerun.delivery_state == DeliveryState.EXTERNAL_MAILER_HANDOFF)
            with admin.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.eco_dashboard_delivery_operation")
                check("one durable row per logical delivery",
                      cur.fetchone()[0] == len(drivers), "row count")
                cur.execute("""SELECT state, capability_secret IS NOT NULL,
                                      provider_idempotency_key, provider_name,
                                      operator_action_required,
                                      metadata_json->'external_mailer_handoff'->>'mailer'
                                 FROM public.eco_dashboard_delivery_operation
                                WHERE identity_key = 'DRIVER-1'""")
                state, has_bearer, key, provider, operator, mailer = cur.fetchone()
                check("terminal state is the handoff",
                      state == DeliveryState.EXTERNAL_MAILER_HANDOFF, str(state))
                check("the bearer is retained so a rerun cannot rotate it", has_bearer)
                check("no provider identity was ever bound",
                      key is None and provider is None)
                check("no operator is required for a healthy handoff", operator is False)
                check("the ledger names the mailing lifecycle that owns the send",
                      mailer == emi.MAILER_ECO_WEEKLY, str(mailer))
            PASSED.append("tier_b: a rerun is idempotent and never rotates the capability")

            # --- a publication failure blocks ONLY that driver -----------------
            failing = DeliveryIdentity(
                client_id=CLIENT_ID, identity_key="DRIVER-REFUSED", period_type="weekly",
                period_start_date=WEEK_START, period_end_date=WEEK_END_EXCLUSIVE,
                send_scope="normal")
            transport.refuse.add(derive_operation_id(failing))
            refused = service.link_for(identity_key="DRIVER-REFUSED",
                                       recipient_email="refused@example.invalid")
            check("a publication failure yields no link", not refused.has_link)
            check("and blocks that driver's e-mail", refused.blocks_send)
            check("with a bounded, non-secret code",
                  refused.failure_code and "#k=" not in str(refused.failure_code))
            after = service.link_for(identity_key="DRIVER-2",
                                     recipient_email="driver-2@example.invalid")
            check("the next driver is unaffected", after.has_link)
            PASSED.append("tier_b: a publication failure isolates to one driver")

            # --- a snapshot exception blocks ONLY that driver -------------------
            seen2: list = []
            service2 = _service(dsn, factory,
                                statuses={},
                                snapshot_errors={"DRIVER-BOOM": RuntimeError("synthetic")},
                                seen_identities=seen2)
            _install_fake_client(service2, transport)
            boom = service2.link_for(identity_key="DRIVER-BOOM",
                                     recipient_email="boom@example.invalid")
            check("a snapshot exception yields no link", not boom.has_link)
            check("it is classified as a technical failure",
                  boom.failure_code == "DASHBOARD_SNAPSHOT_FAILED", str(boom.failure_code))
            check("the exception value never becomes the detail",
                  "synthetic" not in str(boom.detail))
            ok_again = service2.link_for(identity_key="DRIVER-4",
                                         recipient_email="driver-4@example.invalid")
            check("and the run continues for everyone else", ok_again.has_link)
            with admin.cursor() as cur:
                cur.execute("""SELECT count(*) FROM public.eco_dashboard_delivery_operation
                                WHERE identity_key = 'DRIVER-BOOM'""")
                check("a failed snapshot creates no ledger row at all",
                      cur.fetchone()[0] == 0)
            PASSED.append("tier_b: a snapshot failure isolates to one driver and publishes nothing")

            # --- a recipient change is refused, never redirected ---------------
            conflict = service2.link_for(identity_key="DRIVER-1",
                                         recipient_email="someone.else@example.invalid")
            check("a rerun naming a different recipient gets no link",
                  not conflict.has_link)
            check("and is a refusal, not a redirect",
                  "RECIPIENT_CONFLICT" in str(conflict.failure_code)
                  + str(conflict.detail), str(conflict.failure_code))
            with admin.cursor() as cur:
                cur.execute("""SELECT recipient_email
                                 FROM public.eco_dashboard_delivery_operation
                                WHERE identity_key = 'DRIVER-1'""")
                check("the bound recipient is untouched",
                      cur.fetchone()[0] == "driver-1@example.invalid")
            PASSED.append("tier_b: a recipient change is refused, never silently redirected")

            # --- a SQL error for one driver does not poison the fleet run ----
            _tier_b_sql_error_isolation(dsn, factory, transport, admin)

            # --- an expired capability is rotated, never handed over -----------
            _tier_b_expiry_lifecycle(dsn, factory, transport, admin)

            # --- the provider lifecycle never adopts a handed-over delivery ----
            _tier_b_external_mailer_boundary(dsn, transport, admin)

            # --- ending the run destroys expired bearers, with no resend -------
            _tier_b_retirement_on_close(dsn, factory, transport, admin)
            PASSED.append("tier_b: closing an ordinary run retires expired capabilities")

            # --- ...and does so even when the run published NOTHING ------------
            _tier_b_retirement_without_any_publication(dsn, factory, transport, admin)
            PASSED.append("tier_b: a run that publishes nothing still retires "
                          "expired capabilities and spares valid ones")

            service.close()
            service2.close()
        finally:
            emi.build_delivery_snapshot_from_cursor = original_build  # type: ignore[assignment]
            emi._client_business_pg_conn = original_conn  # type: ignore[assignment]
            factory.read_conn.close()
            factory.close()
    finally:
        admin.close()


# ==============================================================================
# TIER C — the REAL snapshot builder against REAL Eco tables
# ==============================================================================
#
# Finding: the canonical snapshot carried `datetime.now()` as `generated_at_utc`,
# so the SAME driver, period and source data serialised to different octets on
# every rebuild. The digest is the delivery ledger's payload identity, so a
# legitimate DELAYED rerun — recovery, a retry, an operator re-running one
# period — would be refused as a payload conflict for data that never changed.
#
# The evidence has to come from the REAL builder over REAL SQL, because the
# defect lived in the one function that turns rows into publishable bytes. A
# stubbed builder would prove nothing about it.

ECO_METRIC_COLUMNS = (
    "overrev_events_count", "harsh_braking_events", "harsh_acceleration_events",
    "harsh_turning_events", "idle_events", "speeding_140_160_count",
    "speeding_160_170_count", "speeding_170_plus_count",
)

TIER_C_IDENTITY = "SYNTHETIC-DRIVER-DETERMINISM"
TIER_C_UPDATED_AT = datetime(2026, 5, 18, 3, 15, 42, tzinfo=timezone.utc)


def _tier_c_schema(conn) -> None:
    metrics = ",\n            ".join(f"{name} BIGINT NOT NULL DEFAULT 0"
                                     for name in ECO_METRIC_COLUMNS)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.eco_trip_assignments")
        cur.execute("DROP TABLE IF EXISTS public.eco_driver_weekly_stats")
        cur.execute(f"""
            CREATE TABLE public.eco_trip_assignments (
            client_id UUID NOT NULL,
            assigned_id TEXT NOT NULL,
            trip_start_ts TIMESTAMPTZ NOT NULL,
            trip_distance_meters BIGINT NOT NULL DEFAULT 0,
            aggregation_included BOOLEAN NOT NULL DEFAULT TRUE,
            is_private_trip BOOLEAN NOT NULL DEFAULT FALSE,
            {metrics}
            )""")
        cur.execute("""
            CREATE TABLE public.eco_driver_weekly_stats (
              client_id UUID NOT NULL,
              assigned_id TEXT NOT NULL,
              period_label TEXT NOT NULL,
              period_start_date DATE NOT NULL,
              period_end_date DATE NOT NULL,
              month_start_date DATE NOT NULL,
              qualification_status TEXT,
              calculation_status TEXT,
              ranking_group TEXT,
              ranking_included BOOLEAN,
              ranking_position INTEGER,
              ranking_total_participants INTEGER,
              ecodriving_rating_type TEXT,
              ecodriving_rating_type_share_percent NUMERIC,
              eco_driving_score_total NUMERIC,
              total_distance_meters BIGINT,
              trips_count INTEGER,
              updated_at TIMESTAMPTZ
            )""")


def _tier_c_seed(conn) -> None:
    """One qualifying cumulative weekly period, built from ordinary trip days."""
    day_km = (70, 85, 0, 92, 64, 20, 0, 78, 88, 95, 60, 5, 0, 99, 84, 91, 77)
    trips = 0
    total_meters = 0
    with conn.cursor() as cur:
        for offset, km in enumerate(day_km):
            if km <= 0:
                continue
            trips += 1
            total_meters += km * 1000
            cur.execute(
                "INSERT INTO public.eco_trip_assignments "
                "(client_id, assigned_id, trip_start_ts, trip_distance_meters) "
                "VALUES (%s, %s, %s, %s)",
                (CLIENT_ID, TIER_C_IDENTITY,
                 datetime(2026, 5, 1 + offset, 9, 30, tzinfo=timezone.utc),
                 km * 1000))
        cur.execute("""
            INSERT INTO public.eco_driver_weekly_stats
              (client_id, assigned_id, period_label, period_start_date,
               period_end_date, month_start_date, qualification_status,
               calculation_status, ranking_group, ranking_included,
               ranking_position, ranking_total_participants,
               ecodriving_rating_type, ecodriving_rating_type_share_percent,
               eco_driving_score_total, total_distance_meters, trips_count,
               updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'QUALIFIED', 'COMPLETE', 'INCLUDED',
                    TRUE, 4, 51, 'bezpieczny', 62.5, %s, %s, %s, %s)""",
            (CLIENT_ID, TIER_C_IDENTITY, "2026-05-W3", WEEK_START, WEEK_END_EXCLUSIVE,
             WEEK_START, 100, total_meters, trips, TIER_C_UPDATED_AT))
    conn.commit()


def _tier_c_build(conn, snapshot_module):
    from jobs.ecodriving_dashboard import sources as src
    from jobs.ecodriving_dashboard.publication import PrivacyContext

    current, previous = emi.period_identities_for(
        "weekly", WEEK_START, WEEK_END_EXCLUSIVE)
    with conn.cursor(row_factory=snapshot_module._dict_row_factory()) as cur:
        return snapshot_module.build_delivery_snapshot_from_cursor(
            cur,
            family=src.resolve_pipeline_family("ALPHA00001"),
            schema="public",
            client_id=CLIENT_ID,
            identity_key=TIER_C_IDENTITY,
            period_type="weekly",
            current_identity=current,
            previous_identity=previous,
            client_code="ALPHA00001",
            privacy=PrivacyContext(identity_key=TIER_C_IDENTITY,
                                   client_code="ALPHA00001"))


def tier_c(dsn: str) -> None:
    import json
    import time

    import psycopg

    from jobs.ecodriving_dashboard import job_eco_dashboard_snapshot as snap

    conn = psycopg.connect(dsn)
    try:
        _tier_c_schema(conn)
        conn.commit()
        _tier_c_seed(conn)

        first = _tier_c_build(conn, snap)
        check("the real builder produced a snapshot", first is not None)
        check("from a period the Eco model describes",
              first.current_identity.period_end_date_exclusive == WEEK_END_EXCLUSIVE)

        document = json.loads(first.payload.decode("utf-8"))
        check("the document is stamped from the persisted data, not the clock",
              document["generated_at_utc"] == TIER_C_UPDATED_AT.isoformat().replace(
                  "+00:00", "Z"),
              document["generated_at_utc"])

        # Wall-clock time advances across the second boundary, which is exactly
        # what used to change the octets.
        time.sleep(1.2)

        second = _tier_c_build(conn, snap)
        check("the canonical BYTES are identical across the rerun",
              first.payload == second.payload,
              f"{len(first.payload)} vs {len(second.payload)}")
        check("and so is the SHA-256 payload digest",
              first.payload_digest == second.payload_digest,
              f"{first.payload_digest} vs {second.payload_digest}")
        check("the digest is the digest of those exact bytes",
              hashlib.sha256(second.payload).hexdigest() == second.payload_digest)
        PASSED.append("tier_c: unchanged source data rebuilds to identical canonical bytes")

        # --- a GENUINE data change must still move the digest -----------------
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.eco_trip_assignments
                              SET trip_distance_meters = trip_distance_meters + 1000
                            WHERE assigned_id = %s AND trip_start_ts = %s""",
                        (TIER_C_IDENTITY, datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)))
        conn.commit()
        changed = _tier_c_build(conn, snap)
        check("changed source data changes the bytes",
              changed.payload != second.payload)
        check("and therefore the digest",
              changed.payload_digest != second.payload_digest)

        with conn.cursor() as cur:
            cur.execute("""UPDATE public.eco_driver_weekly_stats
                              SET updated_at = %s WHERE assigned_id = %s""",
                        (TIER_C_UPDATED_AT + timedelta(hours=3), TIER_C_IDENTITY))
        conn.commit()
        recalculated = _tier_c_build(conn, snap)
        check("a recalculation of the same period also moves the digest",
              recalculated.payload_digest != changed.payload_digest)
        PASSED.append("tier_c: genuine source changes still produce a different digest")

        # --- what that means at the delivery ledger ---------------------------
        # The rerun digest is the SAME, so the ledger returns the existing
        # capability; the changed digest is DIFFERENT, so the ledger refuses it
        # as a payload conflict instead of silently republishing.
        from jobs.ecodriving_dashboard.delivery_ledger import DeliveryLedger
        from jobs.ecodriving_dashboard.delivery_ledger import LedgerConflict

        ledger_conn = psycopg.connect(dsn)
        ledger_conn.autocommit = True
        try:
            ledger = DeliveryLedger(ledger_conn)
            identity = DeliveryIdentity(
                client_id=CLIENT_ID, identity_key=TIER_C_IDENTITY, period_type="weekly",
                period_start_date=WEEK_START, period_end_date=WEEK_END_EXCLUSIVE,
                send_scope="normal")
            record, created = pub.prepare_delivery(
                ledger, identity, payload_digest=first.payload_digest,
                recipient_email="determinism@example.invalid", run_id="run-tier-c")
            check("the first build opens the operation", created)
            again, created_again = pub.prepare_delivery(
                ledger, identity, payload_digest=second.payload_digest,
                recipient_email="determinism@example.invalid", run_id="run-tier-c")
            check("a rerun of unchanged data is the SAME operation",
                  not created_again and again.operation_id == record.operation_id)
            try:
                pub.prepare_delivery(
                    ledger, identity, payload_digest=changed.payload_digest,
                    recipient_email="determinism@example.invalid", run_id="run-tier-c")
            except LedgerConflict as conflict:
                check("changed bytes are refused as a payload conflict",
                      "PAYLOAD" in conflict.code, conflict.code)
            else:
                raise AssertionError("changed canonical bytes were not refused")
            PASSED.append("tier_c: a rerun reuses the operation; changed bytes are a conflict")
        finally:
            ledger_conn.close()
    finally:
        conn.close()


# ==============================================================================


def main() -> int:
    tier_a = [
        test_the_period_the_job_selected_is_the_period_the_dashboard_uses,
        test_a_period_outside_the_eco_model_is_refused_not_substituted,
        test_the_dashboard_never_recomputes_eco_scoring,
        test_the_link_is_escaped_and_carries_no_tracking,
        test_both_dashboard_entry_points_are_one_capability_shown_twice,
        test_the_dashboard_presentation_is_centred_in_both_placements,
        test_every_existing_template_family_has_the_insertion_point,
        test_the_legacy_programme_cta_is_untouched,
        test_a_rendered_message_carries_the_dashboard_twice,
        test_the_below_threshold_templates_show_no_dashboard,
        test_the_below_threshold_templates_pass_the_run_preflight,
        test_the_insertion_point_is_a_position_not_a_substring,
        test_a_quoted_attribute_cannot_fake_the_end_of_a_tag,
        test_placement_is_refused_before_any_publication_effect,
        test_each_driver_gets_exactly_their_own_link,
        test_a_valid_insufficient_distance_dashboard_still_produces_a_link,
        test_a_technical_failure_withholds_that_driver_s_message_only,
        test_a_template_that_lost_the_placeholder_blocks_the_send,
        test_a_run_without_the_integration_renders_exactly_as_before,
        test_each_job_binds_its_own_family_identity_and_mailer,
        test_a_render_only_run_publishes_nothing,
        test_the_integrated_jobs_never_reach_the_dashboard_email_provider,
        test_the_existing_smtp_safety_semantics_are_unchanged,
        test_no_capability_reaches_a_log_a_summary_or_an_audit_record,
        test_a_force_resend_is_the_same_logical_delivery,
        test_the_integration_is_off_unless_the_run_opted_in,
        test_no_schedule_was_activated_and_no_second_orchestrator_exists,
    ]
    print("### TIER A — no infrastructure")
    for test in tier_a:
        test()
        print(f"PASS  {test.__name__}")

    dsn = os.getenv(ENV_DSN)
    print("\n### TIER B — real ledger against migrations 049 + 050 + 051")
    if not dsn:
        SKIPPED.append("tier_b_and_tier_c")
        print(f"SKIPPED: export {ENV_DSN} (a DISPOSABLE loopback instance) to run "
              "the ledger-backed half AND the real-snapshot determinism half. "
              "They are NOT evidence until they run.")
    else:
        from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit

        require_loopback_dsn_or_exit(dsn, label=ENV_DSN)
        try:
            import psycopg  # noqa: F401
        except ImportError:
            print("REFUSED: psycopg is not importable in this interpreter. A silent "
                  "skip here would look like a pass; use the project interpreter.")
            return 1
        tier_b(dsn)
        for name in PASSED:
            if name.startswith("tier_b"):
                print(f"PASS  {name}")
        print("\n### TIER C — the REAL snapshot builder against real Eco tables")
        tier_c(dsn)
        for name in PASSED:
            if name.startswith("tier_c"):
                print(f"PASS  {name}")

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} checks passed — Eco mailing dashboard-link integration")
    if SKIPPED:
        print(f"SKIPPED: {', '.join(SKIPPED)}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
