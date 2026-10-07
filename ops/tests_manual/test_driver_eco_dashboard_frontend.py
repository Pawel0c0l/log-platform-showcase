#!/usr/bin/env python3
"""Driver Eco Dashboard frontend — integration and product contract suite.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_frontend.py

WHAT THIS SUITE IS FOR
----------------------
The presentation layer is design-owned and expected to be replaced wholesale by
a different implementation (different DOM, CSS, charts, copy, section order).
The repository's acceptance layer must therefore test whether a frontend is
CORRECT, not whether it resembles the V1 markup.

The frozen integration contract is exactly two functions:

    window.EcoApp.boot({ source })
    window.EcoRender.renderAccessState(code)

plus the production package shape the delivery Worker allowlists, and the
repo-owned security bootstrap. Everything else — element counts, class names,
DOM nesting, section order, exact wording — is deliberately NOT asserted.

What IS asserted, and must survive any redesign:
  * both entrypoints exist and behave;
  * a snapshot is the only input, and the rendered values come from it;
  * fail-closed states expose no Eco payload at all;
  * ranking absence is a legitimate dashboard, never an invented rank;
  * a sparse rating distribution never grows an absent bucket;
  * the 100 km gate stays period-level — there is no daily threshold;
  * raw counts and normalised coefficients both stay reachable;
  * a missing comparison is never rendered as a zero change;
  * the period distance carries its period-over-period movement, and no
    movement at all when there is no basis for one;
  * access states render with no snapshot;
  * the page is a real tablist a driver can operate from the keyboard, and its
    disclosures are honest;
  * the capability fragment is taken out of the URL by the repository's own
    bootstrap BEFORE the design touches routing or history;
  * the page makes no external request.

ROUTE VOCABULARY. The shipped design is three slides, routed as
`#<weekly|monthly>/<1|2|3>`. That vocabulary belongs to the design, not to the
frozen security/data boundary, and this suite reads it from the shipped
`js/app.js` rather than hardcoding a second copy of it.

Behaviour is proved in a real browser (`eco_dashboard_browser.py`); the API
surface is proved headlessly (`eco_dashboard_render_harness.js`). No screenshot
comparison and no test-id taxonomy is used: queries are semantic (roles, ARIA,
visible text) or data-driven (mutate the snapshot, watch the output move).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

from eco_dashboard_browser import (  # noqa: E402
    APP_ROOT, APPROVED_PAN_CLASS, PRODUCTION_ASSETS, Browser, ContractServer,
    contains_number, fixture_names, load_fixture, normalise, readable,
)

HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_dashboard_render_harness.js"
#: Design-owned: a replacement frontend may rewrite every one of these.
PRESENTATION_FILES = ("js/format.js", "js/render.js", "js/app.js", "css/dashboard.css")

#: The approved design export these five files ARE, byte for byte.
#:
#: Source: `design-handoffs/driver-eco-dashboard/driver-eco-dashboard-final-design-v3/`
#: (`DESIGN_SOURCE_MANIFEST.md`, "Production files (exactly five)"). The package
#: is a delivered input, not a repository dependency — recording its digests
#: here lets the repository prove on its own that the shipped presentation layer
#: is the approved export and not a local fork of it.
#:
#: A repository-side correction NEVER edits one of these files. If a real
#: product or security contract cannot be met without changing one, that is a
#: design-source correction and the design must re-export.
#:
#: When a newly approved design lands, replace this table with its manifest's.
DESIGN_EXPORT = "driver-eco-dashboard-final-design-v3"
DESIGN_OWNED_SHA256 = {
    "index.html": "d50444ae62e8067d8d29111741a6c14dd0129d3b7fb346682ef4bf065b476ca7",
    "css/dashboard.css": "b6cddbb37b08b41b02990bc6cd41b87a1c7a65ed176a1b0ac085acecf02a288e",
    "js/format.js": "6a4b834cb26d2acf5285b5f3f79f3989f5dc0e429e6d690509476c56be3aa998",
    "js/render.js": "165a819b17db8f41bb4738d30682b39ac8bb2fb1548cd9e3aaef1fe3642e6f0f",
    "js/app.js": "b244e43ad2d194ca847402ea63e3160b1e0b935266294362a62e2f84a3670d32",
}

#: The repository owns these three and the design never touches them.
REPO_OWNED_JS = ("js/capability-bootstrap.js", "js/boot.js", "js/snapshot-source.js")

ACCESS_CODES = ("INVALID_LINK", "LINK_EXPIRED", "SNAPSHOT_UNAVAILABLE", "SERVICE_UNAVAILABLE")

#: The design's own slide vocabulary, verified against the shipped source by
#: `test_the_route_vocabulary_is_the_shipped_one` before it is used anywhere.
SLIDES = ("1", "2", "3")
SLIDE_SCORE, SLIDE_AXES, SLIDE_DAYS = SLIDES
ROUTE = re.compile(r"^#(weekly|monthly)/([123])$")

#: A capability that cannot occur by coincidence, used to prove the repo-owned
#: bootstrap takes the fragment before the design can route over it.
CAPABILITY_SENTINEL = "cap-9f2b7c41e5a04d6c"

#: Field names the browser payload may never contain, in any casing.
FORBIDDEN_FIELDS = (
    "driver_key", "client_code", "client_id", "driver_name", "person_name",
    "driver_surname", "person_name_group_key", "assigned_id", "source_person_id",
    "email", "phone", "employee", "registration", "chassis", "latitude", "longitude",
    "geofence", "odometer", "provider_trip_id", "record_id", "trip_start_ts",
    "trip_end_ts", "driver_tag_description", "trip_mode", "ranking_included",
    "ranking_group", "day_status", "min_daily_evaluation_km", "smtp", "password", "secret",
)

#: Host-internal ranking vocabulary. The driver must never see any of it.
INTERNAL_WORDS = ("EXCLUDED", "INCLUDED", "UNKNOWN_DRIVER", "LOW_DISTANCE", "NO_DISTANCE")

#: Values that betray a renderer reading a field that is not there. Matched on
#: word boundaries: Polish prose legitimately contains these letter sequences
#: (`porownania` carries "nan"), and a false positive here would be a redesign
#: blocker for no reason.
BROKEN_VALUE_PATTERNS = (
    re.compile(r"\bundefined\b", re.I),
    re.compile(r"\bnull\b", re.I),
    re.compile(r"\bNaN\b"),
    re.compile(r"\bInfinity\b"),
    re.compile(r"\[object [A-Z]\w*\]"),
)

#: Sentinels chosen so they cannot occur by coincidence in the fixture data.
#: Verified against every rendering used below, in visible text and in markup
#: with the inline layout maths removed.
SENTINEL_SCORE = 47
SENTINEL_KILOMETERS = 34181
SENTINEL_TRIPS = 5551
SENTINEL_COUNT = 8887
SENTINEL_COEFFICIENT = 7771
SENTINEL_DAY_COUNT = 4443
SENTINEL_DAY_COEFFICIENT = 3332
SENTINEL_RANK = 137
SENTINEL_PARTICIPANTS = 941
SENTINEL_RANK_DELTA = 219
SENTINEL_SCORE_DELTA = 583
SENTINEL_DISTANCE_DELTA = 6543


# --------------------------------------------------------------- helpers ---


def node(scenario: str, *args: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario, *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"harness {scenario} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def source_of(relative: str) -> str:
    return (APP_ROOT / relative).read_text(encoding="utf-8")


def without_comments(relative: str) -> str:
    """Source with its comments removed, so documentation cannot fail a grep."""
    text = source_of(relative)
    if relative.endswith((".js", ".css")):
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"(?m)(^|[\s;{}(),=])//[^\n]*", r"\1 ", text)
    if relative.endswith((".html", ".htm")):
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    return text


def periods_of(document: dict) -> list[str]:
    return [name for name in ("weekly", "monthly") if document["periods"].get(name)]


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def assert_no_broken_values(text: str, label: str) -> None:
    for pattern in BROKEN_VALUE_PATTERNS:
        found = pattern.search(text)
        assert not found, f"{label}: rendered {found.group()!r}"


def assert_no_private_content(text: str, label: str) -> None:
    haystack = text.lower()
    for field in FORBIDDEN_FIELDS:
        assert field not in haystack, f"{label}: leaked {field!r}"
    for word in INTERNAL_WORDS:
        assert word not in text, f"{label}: leaked internal vocabulary {word!r}"


def expand_every_disclosure(browser: Browser) -> int:
    """Activate every collapsed disclosure control, the way a driver would.

    Returns how many were opened. Activation is a real `click()` on the real
    control, so a design that wires its disclosure to something other than the
    element carrying `aria-expanded` fails here rather than passing on markup
    that no one can actually reach.
    """
    opened = browser.in_frame(
        """
        var controls = root.querySelectorAll('[aria-expanded="false"]');
        var n = 0;
        for (var i = 0; i < controls.length; i++) { controls[i].click(); n++; }
        return n;
        """
    )
    browser.settle()
    return opened


#: `overflow-x: auto|scroll`, or the shorthand that implies it.
_SCROLLS_SIDEWAYS = re.compile(
    r"(?:^|;)\s*overflow(?:-x)?\s*:\s*(?:auto|scroll)(?:\s+\S+)?\s*(?:;|$)", re.I)
_RULE = re.compile(r"([^{}]*)\{([^{}]*)\}", re.S)


def assert_horizontal_scrolling_is_scoped(css: str) -> None:
    """C1: horizontal panning is allowed for ONE approved component, not as a rescue.

    The product rule is that the DOCUMENT must never scroll sideways — proved
    at every viewport by `test_driver_eco_dashboard_viewport_sweep.py`. A
    dedicated analytical surface pans inside its own bounded box on purpose:
    the daily table carries a column per event category and would be unreadable
    if it were squeezed into 320 px. So a blanket ban on `overflow-x` would
    forbid the right answer; what is forbidden is any OTHER element quietly
    using a scrollbar to hide a layout that does not fit.
    """
    stripped = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    offenders = []
    for selector, body in _RULE.findall(stripped):
        if not _SCROLLS_SIDEWAYS.search(";" + body.strip()):
            continue
        selector = " ".join(selector.split())
        if f".{APPROVED_PAN_CLASS}" not in selector:
            offenders.append(f"{selector} {{{body.strip()}}}")
    assert not offenders, (
        "horizontal scrolling is not an acceptable rescue; only "
        f".{APPROVED_PAN_CLASS} may pan: {offenders}"
    )
    assert f".{APPROVED_PAN_CLASS}" in stripped, \
        f"the approved pan surface .{APPROVED_PAN_CLASS} is gone from the stylesheet"


PERCENT = re.compile(r"\d+(?:[.,]\d+)?\s?%")
ZERO_PERCENT = re.compile(r"(?<![\d.,])0(?:[.,]0+)?\s?%")


# ------------------------------------------------- synthetic input states ---


def sparse_ranked_document() -> dict:
    """A real production shape: a ranked population with nobody in one group.

    The host derives the distribution by grouping the rated population, so an
    empty group produces no row and therefore no key. The checked-in fixture set
    carries the three-bucket case only, so this state is built here instead of
    being added to `assets/`, which must stay byte-identical to production.
    """
    document = load_fixture("ranked_acceptable")
    block = document["periods"]["weekly"]["current"]
    block["rating_group_distribution"] = {"safe": 49.37, "acceptable": 50.63}
    return document


def sentinel_document() -> dict:
    """Distinctive values in every place a dashboard must read from the snapshot."""
    document = load_fixture("ranked_acceptable")
    block = document["periods"]["weekly"]["current"]
    block["eco_score_total"] = SENTINEL_SCORE
    block["total_kilometers"] = SENTINEL_KILOMETERS
    block["trips_count"] = SENTINEL_TRIPS
    category = next(entry for entry in block["categories"] if entry["key"] == "harsh_turning")
    category["count"] = SENTINEL_COUNT
    category["coefficient_per_100km"] = SENTINEL_COEFFICIENT
    day = block["days"][0]
    day_category = next(entry for entry in day["categories"] if entry["key"] == "idle")
    day_category["count"] = SENTINEL_DAY_COUNT
    day_category["coefficient_per_100km"] = SENTINEL_DAY_COEFFICIENT
    return document


def poisoned_document(status: str) -> dict:
    """A non-OK entry that still carries a full Eco payload.

    The delivery layer refuses such an object, and the frontend must too: a
    frontend that renders whatever it is handed would leak a score for a period
    that did not qualify. Every sentinel below must be invisible.
    """
    document = sentinel_document()
    document["periods"]["weekly"]["status"] = status
    return document


def ranked_sentinel_document() -> dict:
    """A ranked driver whose ranking numbers cannot occur by coincidence."""
    document = load_fixture("ranked_acceptable")
    block = document["periods"]["weekly"]["current"]
    block["ranking_position"] = SENTINEL_RANK
    block["ranking_total_participants"] = SENTINEL_PARTICIPANTS
    block["comparison"]["previous_ranking_position"] = SENTINEL_RANK + SENTINEL_RANK_DELTA
    block["comparison"]["ranking_position_delta_places"] = SENTINEL_RANK_DELTA
    return document


def unranked_sentinel_document() -> dict:
    """The same driver, not in the ranking: the ranking facts are simply absent."""
    document = ranked_sentinel_document()
    block = document["periods"]["weekly"]["current"]
    block["ranking_state"] = "NOT_RANKED_BY_CONFIGURATION"
    block["ranking_transition"] = "NOT_RANKED_TO_NOT_RANKED"
    for field in ("ranking_position", "ranking_total_participants", "rating_group_share_percent"):
        block.pop(field, None)
    block["rating_group_distribution"] = None
    block["comparison"]["previous_ranking_position"] = None
    block["comparison"]["ranking_position_delta_places"] = None
    return document


def newly_ranked_sentinel_document() -> dict:
    """Ranked now, with no comparable previous rank: a position but no movement."""
    document = ranked_sentinel_document()
    block = document["periods"]["weekly"]["current"]
    block["ranking_transition"] = "NEWLY_RANKED"
    block["comparison"]["previous_ranking_position"] = None
    block["comparison"]["ranking_position_delta_places"] = None
    return document


def comparable_sentinel_document() -> dict:
    """A period with a comparison basis, whose movements cannot occur by chance."""
    document = load_fixture("ranked_acceptable")
    block = document["periods"]["weekly"]["current"]
    comparison = block["comparison"]
    comparison["previous_eco_score_total"] = block["eco_score_total"] + SENTINEL_SCORE_DELTA
    comparison["eco_score_delta"] = -SENTINEL_SCORE_DELTA
    comparison["previous_total_kilometers"] = block["total_kilometers"] + SENTINEL_DISTANCE_DELTA
    return document


def no_basis_sentinel_document() -> dict:
    """The same period with no comparable predecessor: every delta loses its basis."""
    document = comparable_sentinel_document()
    entry = document["periods"]["weekly"]
    entry["current"]["comparison"] = None
    entry["current"]["ranking_transition"] = "NO_COMPARISON_BASIS"
    entry["previous"] = None
    return document


SYNTHETIC_DOCUMENTS = {
    "comparable_sentinels": comparable_sentinel_document(),
    "no_basis_sentinels": no_basis_sentinel_document(),
    "ranked_sentinels": ranked_sentinel_document(),
    "unranked_sentinels": unranked_sentinel_document(),
    "newly_ranked_sentinels": newly_ranked_sentinel_document(),
    "sparse_ranked": sparse_ranked_document(),
    "sentinels": sentinel_document(),
    "poisoned_insufficient": poisoned_document("INSUFFICIENT_DISTANCE"),
    "poisoned_not_ready": poisoned_document("REPORT_NOT_READY"),
}


# ------------------------------------------------------ 1. package shape ---


def test_production_package_shape_is_production_compatible() -> None:
    """The Worker allowlist, the CSP and the bootstrap order are integration facts."""
    for relative in PRODUCTION_ASSETS:
        assert (APP_ROOT / relative).is_file(), f"missing production asset {relative}"

    index = source_of("index.html")
    # No inline script: the delivery CSP is `script-src 'self'`.
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>\s*\S", index), "inline <script> in index.html"
    # No inline <style> element: `style-src-elem 'self'`. Style ATTRIBUTES stay legal.
    assert not re.search(r"<style[\s>]", index, re.I), "inline <style> element in index.html"

    # The capability must be taken out of the URL before anything else can run,
    # and the exchange must be the last thing the document does.
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', index)
    assert scripts, "index.html loads no script"
    assert scripts[0].endswith("js/capability-bootstrap.js"), scripts
    assert scripts[-1].endswith("js/boot.js"), scripts
    head = index.split("</head>")[0]
    assert "capability-bootstrap.js" in head, "capability cleanup must run in <head>"

    # The mount point and the snapshot URL are what boot.js reads.
    assert re.search(r'id="eco-root"[^>]*data-snapshot-url="/api/snapshot"', index) or \
        re.search(r'data-snapshot-url="/api/snapshot"[^>]*id="eco-root"', index), index[:400]

    assert 'lang="pl"' in index
    assert re.search(r'name="robots"[^>]*noindex', index)
    assert re.search(r'name="referrer"[^>]*no-referrer', index)

    # The page asks for no identity and offers no login.
    for banned in ("<form", "<input", "password", "login", "driver_key"):
        assert banned not in index.lower(), banned

    # Nothing is fetched from anywhere but this origin. Comments are stripped
    # first: prose that mentions a URL is documentation, not a dependency.
    for relative in PRODUCTION_ASSETS:
        text = without_comments(relative)
        for pattern in (r'src\s*=\s*["\']\s*(?:https?:)?//', r'href\s*=\s*["\']\s*(?:https?:)?//',
                        r"@import\s+(?:url\()?\s*['\"]?\s*(?:https?:)?//",
                        r"url\(\s*['\"]?\s*(?:https?:)?//",
                        r"""(?:fetch|open|import)\s*\(\s*['"`]\s*(?:https?:)?//"""):
            assert not re.search(pattern, text, re.I), f"{relative} names an external host"
    print("PASS test_production_package_shape_is_production_compatible")


NOTE_PHRASE = "poprzedni miesi\u0105c: "


def _panel(markup: str, index: int) -> str:
    """The markup of one trend tabpanel, panel 0 ending where panel 1 begins."""
    start = markup.index(f'id="ed-trend-panel-{index}"')
    nxt = markup.find(f'id="ed-trend-panel-{index + 1}"', start)
    return markup[start:nxt if nxt != -1 else markup.index("</section>", start)]


def _bars(fragment: str) -> int:
    return fragment.count('class="ed-tbar"') + fragment.count('class="edm-tbar"')


def test_the_monthly_trend_card_offers_two_tabs() -> None:
    """The comparison opens first, and each tab shows what it promises.

    Tab (a) is two bars — the month before and this month — sourced from
    `series_reference`, which the delivery path fills from the SAME previous
    month the comparison block uses. Tab (b) is the month's own weekly buckets.
    """
    for tree in ("default", "mobile"):
        markup = node("render_pair", "monthly_31_days", "monthly", "0")[tree]
        assert 'role="tablist"' in markup, tree
        assert 'data-action="trend"' in markup, tree
        assert 'id="ed-trend-tab-0"' in markup and 'id="ed-trend-tab-1"' in markup, tree
        # (a) is the default, and exactly one tab claims selection.
        assert markup.count('aria-selected="true"') >= 1, tree
        assert 'id="ed-trend-tab-0"' in markup
        head = markup[markup.index('id="ed-trend-tab-0"'):]
        assert 'aria-selected="true"' in head[:200], f"{tree}: tab (a) must be selected by default"
        assert _bars(_panel(markup, 0)) == 2, f"{tree}: tab (a) must show exactly two bars"
        assert _bars(_panel(markup, 1)) == 3, f"{tree}: tab (b) must show the month's buckets"
        # The panel that is not selected is hidden, not merely unstyled.
        assert "hidden" in _panel(markup, 1)[:200], tree
    print("PASS test_the_monthly_trend_card_offers_two_tabs")


def test_a_month_whose_reference_is_missing_falls_back_to_its_comparison() -> None:
    """A page must not contradict itself.

    Every live August 2026 document was built before `series_reference` was
    derived, so it carries `series_reference: null` while its own `comparison`
    block still names the previous closed month and its score — and the score
    card renders that comparison in the header. Keyed on the reference alone,
    the same page said "13 pkt więcej niż w poprzednim miesiącu" above a card
    reading "brak podstawy do porównania". `monthly_previous_from_comparison`
    is that exact shape.
    """
    fixture = "monthly_previous_from_comparison"
    document = load_fixture(fixture)
    entry = document["periods"]["monthly"]
    assert entry["series_reference"] is None, "the fixture no longer models an aged document"
    comparison = entry["current"]["comparison"]
    assert comparison["kind"] == "PREVIOUS_CLOSED_MONTH"
    basis = comparison["previous_eco_score_total"]
    assert isinstance(basis, int)

    for tree in ("default", "mobile"):
        markup = node("render_pair", fixture, "monthly", "0")[tree]
        head = markup[markup.index('id="ed-trend-tab-0"'):]
        assert 'aria-selected="true"' in head[:200], (
            f"{tree}: the comparison tab must open, the document has a comparison")
        panel = _panel(markup, 0)
        assert _bars(panel) == 2, f"{tree}: expected the previous month and this month"
        assert "brak podstawy do por\u00F3wnania" not in strip_tags(panel), (
            f"{tree}: the card denies a comparison the header is already showing")
        # The bar carries the comparison's OWN value and its own dates.
        # `rangeShort` collapses a same-month range, so June reads "01-30.06".
        assert str(basis) in strip_tags(panel), f"{tree}: the previous score is not on the bar"
        assert "01\u201330.06" in strip_tags(panel), (
            f"{tree}: the bar is not labelled with the comparison's basis dates")
        # And the header still says what it said.
        assert "ni\u017C w poprzednim miesi\u0105cu" in strip_tags(markup), tree

    # PRECEDENCE. Where both sources exist the reference wins — and
    # `monthly_31_days` is built so they DISAGREE (reference 69, the comparison's
    # own previous 95), which is the divergence ECO-20260903-01 exists to
    # handle. So the bar proves which source was read, not merely that two bars
    # appeared.
    both = load_fixture("monthly_31_days")["periods"]["monthly"]
    reference = both["series_reference"]["eco_score_total"]
    comparison_basis = both["current"]["comparison"]["previous_eco_score_total"]
    assert reference != comparison_basis, (
        "monthly_31_days no longer distinguishes the two sources; precedence is untested")
    for tree in ("default", "mobile"):
        panel = strip_tags(_panel(node("render_pair", "monthly_31_days", "monthly", "0")[tree], 0))
        assert str(reference) in panel, (
            f"{tree}: series_reference ({reference}) must win over the comparison")
        assert str(comparison_basis) not in panel, (
            f"{tree}: the comparison's {comparison_basis} leaked past the reference")
    print("PASS test_a_month_whose_reference_is_missing_falls_back_to_its_comparison")


def test_a_month_without_a_previous_month_opens_on_progress() -> None:
    """Never open on the emptier tab."""
    for tree in ("default", "mobile"):
        markup = node("render_pair", "monthly_without_previous_month", "monthly", "0")[tree]
        assert 'role="tablist"' in markup, tree
        head = markup[markup.index('id="ed-trend-tab-1"'):]
        assert 'aria-selected="true"' in head[:200], f"{tree}: tab (b) must be the default here"
        assert _bars(_panel(markup, 0)) == 1, f"{tree}: only this month can be shown"
        assert "brak podstawy do por\u00F3wnania" in _panel(markup, 0), tree
    print("PASS test_a_month_without_a_previous_month_opens_on_progress")


def test_a_weekly_trend_card_has_no_tabs() -> None:
    for tree in ("default", "mobile"):
        markup = node("render_pair", "ranked_acceptable", "weekly", "0")[tree]
        assert 'data-action="trend"' not in markup, f"{tree}: weekly must keep the progress view only"
        assert "Wynik w poprzednich okresach" in markup, tree
    print("PASS test_a_weekly_trend_card_has_no_tabs")


def test_the_rejected_previous_month_note_is_gone_everywhere() -> None:
    for fixture in fixture_names():
        document = load_fixture(fixture)
        for period in periods_of(document):
            for slide in range(3):
                pair = node("render_pair", fixture, period, str(slide))
                for tree in ("default", "mobile"):
                    assert NOTE_PHRASE not in pair[tree], f"{fixture}/{period}/{slide}/{tree}"
    print("PASS test_the_rejected_previous_month_note_is_gone_everywhere")


def test_the_previous_marker_is_named_for_the_period_it_compares() -> None:
    """A monthly document compares against a month; a weekly one against a report."""
    monthly = node("render_pair", "monthly_31_days", "monthly", "1")["default"]
    assert "poprzedni miesi\u0105c" in monthly
    assert "Teraz vs poprzedni miesi\u0105c" in monthly
    weekly = node("render_pair", "ranked_acceptable", "weekly", "1")["default"]
    assert "poprzedni raport" in weekly
    assert "Teraz vs poprzedni raport" in weekly
    assert "poprzedni miesi\u0105c" not in weekly
    print("PASS test_the_previous_marker_is_named_for_the_period_it_compares")


#: Every phrase that NAMES the comparison period, in both vocabularies.
PREV_PERIOD_PHRASES = ("w poprzednim okresie", "poprzedniego okresu",
                       "z poprzednim okresem", "Poprzedni okres")
PREV_MONTH_PHRASES = ("w poprzednim miesi\u0105cu", "poprzedniego miesi\u0105ca",
                      "z poprzednim miesi\u0105cem", "Poprzedni miesi\u0105c")
#: Phrases about the CURRENT period, or period-neutral. These must NOT move.
#: Rendered by some fixture:
PERIOD_NEUTRAL_PHRASES = ("Tw\u00F3j wynik w tym okresie",
                          "pierwszy zamkni\u0119ty okres \u2014 brak por\u00F3wnania")
#: Only reachable on a zero delta, which no fixture carries — held in the source.
PERIOD_NEUTRAL_SOURCE_PHRASES = ("tyle samo km, co poprzednio",
                                 "Dystans w tym okresie")


def test_a_monthly_report_names_the_month_wherever_it_names_the_previous_period() -> None:
    """A monthly document must never borrow the weekly vocabulary, or vice versa.

    Swept over EVERY fixture, both trees, all three slides, because the phrase
    is emitted from nine separate sites and a missed one is invisible until a
    driver reads it. The two vocabularies are mutually exclusive by document
    frame, so each sweep proves the other's absence as well.
    """
    monthly_hits = weekly_hits = 0
    for fixture in fixture_names():
        document = load_fixture(fixture)
        for period in periods_of(document):
            wanted, forbidden = ((PREV_MONTH_PHRASES, PREV_PERIOD_PHRASES)
                                 if period == "monthly"
                                 else (PREV_PERIOD_PHRASES, PREV_MONTH_PHRASES))
            for slide in range(3):
                pair = node("render_pair", fixture, period, str(slide))
                for tree in ("default", "mobile"):
                    text = strip_tags(pair[tree])
                    leaked = [phrase for phrase in forbidden if phrase in text]
                    assert not leaked, (
                        f"{fixture}/{period}/slide{slide}/{tree}: a {period} report "
                        f"used the wrong vocabulary: {leaked}")
                    found = len([phrase for phrase in wanted if phrase in text])
                    if period == "monthly":
                        monthly_hits += found
                    else:
                        weekly_hits += found
    assert monthly_hits > 0, "no monthly fixture ever named the previous month"
    assert weekly_hits > 0, "no weekly fixture ever named the previous period"

    # The element the feedback was about — the score-card delta — in BOTH
    # directions, so neither arm of the ternary was left on a bare literal.
    # `monthly_31_days` scores 10 points below its previous month; the "wi\u0119cej"
    # arm is proved on the weekly side, where a positive delta exists.
    monthly = node("render_pair", "monthly_31_days", "monthly", "0")
    for tree in ("default", "mobile"):
        assert "pkt mniej ni\u017C w poprzednim miesi\u0105cu" in strip_tags(monthly[tree]), tree
    rising = node("render_pair", "ranked_safe", "weekly", "0")
    for tree in ("default", "mobile"):
        assert "pkt wi\u0119cej ni\u017C w poprzednim okresie" in strip_tags(rising[tree]), tree

    # Out of scope, and still exactly as they were.
    weekly = strip_tags(node("render_pair", "no_comparison", "weekly", "0")["default"])
    for phrase in PERIOD_NEUTRAL_PHRASES:
        assert phrase in weekly or phrase in strip_tags(monthly["default"]), phrase
    render = source_of("js/render.js")
    for phrase in PERIOD_NEUTRAL_SOURCE_PHRASES:
        assert phrase in render, f"a period-neutral phrase was rewritten: {phrase!r}"
    print(f"PASS test_a_monthly_report_names_the_month_wherever_it_names_the_previous_period "
          f"({monthly_hits} monthly / {weekly_hits} weekly phrase sites)")


def test_the_comparison_wording_follows_the_document_not_the_comparison() -> None:
    """A monthly report with NOTHING to compare against still says "month".

    The wording used to key on `comparison.kind`, which is absent exactly when
    there is no comparison — so the one document that most needs to say which
    period it means fell back to the weekly word.
    """
    render = without_comments("js/render.js")
    assert 'cur.period_type === "monthly"' in render, (
        "the wording must key on the document frame, not on the comparison object")
    assert 'comparison.kind === "PREVIOUS_CLOSED_MONTH"' not in render, (
        "a comparison-keyed branch is back; it fails on the no-basis documents")
    for tree in ("default", "mobile"):
        for slide in (0, 1):
            markup = node("render_pair", "monthly_without_previous_month",
                          "monthly", str(slide))[tree]
            text = strip_tags(markup)
            leaked = [phrase for phrase in PREV_PERIOD_PHRASES if phrase in text]
            assert not leaked, f"{tree}/slide{slide}: no-basis monthly leaked {leaked}"
            assert "poprzedni raport" not in text, (
                f"{tree}/slide{slide}: a MONTHLY document called its basis a report")
    assert "Teraz vs poprzedni miesi\u0105c" in strip_tags(
        node("render_pair", "monthly_without_previous_month", "monthly", "1")["default"])
    print("PASS test_the_comparison_wording_follows_the_document_not_the_comparison")


TREND_TRACK = re.compile(
    r'<div class="(ed|edm)-trend-seg" role="tablist" data-selected="([01])"')


def test_the_trend_switch_is_one_sliding_thumb() -> None:
    """The thumb is the ONLY selection indicator, and it starts where it belongs.

    Three things have to hold together or the switch lies about its state:
    the track carries the selected index in the MARKUP (so the first paint is
    already correct and nothing slides in from cell 0 on mount), exactly one
    decorative thumb exists per tablist, and the selected label no longer
    paints a pill of its own behind the thumb.
    """
    for fixture, expected in (("monthly_31_days", "0"),
                              ("monthly_without_previous_month", "1")):
        for tree, prefix in (("default", "ed"), ("mobile", "edm")):
            markup = node("render_pair", fixture, "monthly", "0")[tree]
            found = TREND_TRACK.search(markup)
            assert found, f"{fixture}/{tree}: no trend track carrying its selected index"
            assert found.group(1) == prefix, f"{fixture}/{tree}: wrong tree prefix"
            assert found.group(2) == expected, (
                f"{fixture}/{tree}: the track opens on cell {found.group(2)}, "
                f"but tab {expected} is the selected one — the thumb would slide on mount")
            thumb = f'<span class="{prefix}-trend-seg-thumb" aria-hidden="true"></span>'
            assert markup.count(thumb) == 1, (
                f"{fixture}/{tree}: expected exactly one decorative thumb, "
                f"found {markup.count(thumb)}")
            # The thumb precedes the tabs, and the tabs are what carry aria.
            assert markup.index(thumb) < markup.index('id="ed-trend-tab-0"'), tree

    css = without_comments("css/dashboard.css")
    for prefix in ("ed", "edm"):
        rule = re.search(
            r"\." + prefix + r"-trend-seg-tab\[aria-selected=\"true\"\]\{([^}]*)\}", css)
        assert rule, f"{prefix}: no selected-label rule"
        assert "background" not in rule.group(1), (
            f"{prefix}: the selected label still paints its own background "
            f"({rule.group(1)!r}) — the thumb must be the only indicator")
        # Only `transform` animates: nothing layout-bound.
        # Anchored to a rule of its OWN: `.ed-trend-seg-thumb` also appears in
        # the reduced-motion group rule, which is not the declaration hunted here.
        thumb_rule = re.search(
            r"(?m)^\s*\." + prefix + r"-trend-seg-thumb\{([^}]*)\}", css)
        assert thumb_rule, f"{prefix}: no thumb rule"
        body = thumb_rule.group(1)
        assert "position:absolute" in body, prefix
        assert re.search(r"transition:transform \.\d+s", body), (
            f"{prefix}: the thumb must transition `transform` only, got {body!r}")
        for layout_bound in ("transition:left", "transition:width", "transition:margin",
                             "transition:all"):
            assert layout_bound not in body, f"{prefix}: {layout_bound} is layout-bound"
    # Reduced motion is named explicitly, not left to the universal rule alone.
    reduce_block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    reduce_block = reduce_block[:reduce_block.index("\n}")]
    assert "ed-trend-seg-thumb" in reduce_block and "edm-trend-seg-thumb" in reduce_block, (
        "the reduced-motion block does not name the trend thumb")
    print("PASS test_the_trend_switch_is_one_sliding_thumb")


def test_the_trend_switch_does_not_borrow_a_distribution_segment_class() -> None:
    """REGRESSION. `.ed-seg` / `.edm-seg` are the axis and band distribution
    segments. A tablist that reuses either name inherits their box — and once
    the tablist became a positioned track, it would have handed every one of
    those segments a containing block too. The two components stay disjoint.
    """
    css = without_comments("css/dashboard.css")
    render = source_of("js/render.js")
    for seg, tablist in (("ed-seg", "ed-trend-seg"), ("edm-seg", "edm-trend-seg")):
        assert f'class="{seg}" role="tablist"' not in render, (
            f"the trend tablist is back on `{seg}`, which the distribution bar owns")
        assert f'class="{tablist}" role="tablist"' in render, tablist
        # The distribution segment still exists under its own name, unchanged.
        assert re.search(r"\." + seg + r"\{", css), f"{seg} rule vanished"
    print("PASS test_the_trend_switch_does_not_borrow_a_distribution_segment_class")


def test_the_slide_tablist_and_the_trend_tablist_do_not_collide() -> None:
    """`applySlide` addresses its panels POSITIONALLY, so a second tablist that
    it could see would shift the slide indices, not merely lose aria state."""
    app = source_of("js/app.js")
    assert 'SLIDE_TAB_SELECTOR = \'[role="tab"][data-action="dot"]\'' in app
    assert 'SLIDE_PANEL_SELECTOR = \'[role="tabpanel"][id^="ed-panel-"]\'' in app
    assert 'document.querySelectorAll(\'[role="tab"]\')' not in app, (
        "the slide machinery must not claim every tablist on the page")
    assert 'document.querySelectorAll(\'[role="tabpanel"]\')' not in app
    print("PASS test_the_slide_tablist_and_the_trend_tablist_do_not_collide")


def test_the_shipped_presentation_is_the_approved_design_export() -> None:
    """The five design-owned files are byte-identical to the approved export.

    This is the integration's hardest rule, and it exists to stop the slow
    failure where a repository "fixes" the design in place: the fork drifts, the
    design package stops describing what ships, and the next re-export silently
    reverts the fix. The acceptance layer, the repo-owned bootstrap and the
    Worker are where a repository-side correction belongs — never here.
    """
    actual = {}
    for relative, expected in DESIGN_OWNED_SHA256.items():
        digest = hashlib.sha256((APP_ROOT / relative).read_bytes()).hexdigest()
        actual[relative] = digest
    drifted = {name: actual[name] for name, expected in DESIGN_OWNED_SHA256.items()
               if actual[name] != expected}
    assert not drifted, (
        f"the shipped presentation layer is no longer the {DESIGN_EXPORT} export — "
        f"these files were edited in the repository: {sorted(drifted)}. A repository-side "
        "correction belongs in the acceptance layer, in the repo-owned bootstrap or in the "
        "Worker; if the design itself must change, the design source must be corrected and "
        "re-exported."
    )
    # And the split is honest: a repo-owned file is not in the design's list.
    assert not set(DESIGN_OWNED_SHA256) & set(REPO_OWNED_JS)
    for relative in REPO_OWNED_JS:
        assert (APP_ROOT / relative).is_file(), relative
    print(f"PASS test_the_shipped_presentation_is_the_approved_design_export "
          f"({len(DESIGN_OWNED_SHA256)}/{len(DESIGN_OWNED_SHA256)} = {DESIGN_EXPORT})")


def test_secure_bootstrap_stays_owned_by_the_repository() -> None:
    """The design layer may never touch the credential path."""
    bootstrap = without_comments("js/capability-bootstrap.js")
    assert '"#k="' in bootstrap or "'#k='" in bootstrap
    assert "replaceState" in bootstrap and "pushState" not in bootstrap
    assert "__ecoTakeCapability" in bootstrap

    boot = without_comments("js/boot.js")
    assert "/api/session" in boot and '"POST"' in boot
    assert "__ecoTakeCapability" in boot
    assert "EcoApp.boot" in boot, "boot.js must call the frozen entrypoint"
    assert "renderAccessState" in boot

    snapshot_source = without_comments("js/snapshot-source.js")
    assert "same-origin" in snapshot_source

    # No frontend file may persist or re-read credential material.
    for relative in PRODUCTION_ASSETS:
        if not relative.endswith(".js"):
            continue
        text = without_comments(relative)
        for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie",
                       "eval(", "new Function", "new Worker", "serviceWorker"):
            assert banned not in text, f"{relative} uses {banned}"

    # The presentation layer specifically must not know about the credential.
    for relative in PRESENTATION_FILES:
        text = without_comments(relative)
        for banned in ("__ecoTakeCapability", "/api/session", "capability", "bearer",
                       "Authorization", "session_id"):
            assert banned not in text, f"{relative} touches the credential path: {banned}"
    print("PASS test_secure_bootstrap_stays_owned_by_the_repository")


def test_no_business_derivation_happens_in_the_browser() -> None:
    """Status, bands and ratings are read from the snapshot, never recomputed."""
    sources = {name: without_comments(name) for name in PRESENTATION_FILES if name.endswith(".js")}
    for name, text in sources.items():
        for signature in ("SCORING_RULES", "METRIC_MAX_POINTS", "ScoringBucket",
                          "round_rate_for_scoring", "final_points", "harsh_braking_events"):
            assert signature not in text, f"{name} duplicates scoring internals: {signature}"
        for arithmetic in ("/ kilometers", "/ total_kilometers", "/ day.kilometers",
                           "count /", "counts /", "ROUND_HALF_UP"):
            assert arithmetic not in text, f"{name} recomputes a coefficient: {arithmetic}"
        for banding in ("upper_bound >", "upper_bound <", "statusFromCount"):
            assert banding not in text, f"{name} bands a value itself: {banding}"
        # No frontend copy of the rating ladder: the thresholds arrive in
        # `constants.rating_thresholds`.
        assert not re.search(r"(score|total)\s*>=?\s*85\b", text), name
        assert not re.search(r"(score|total)\s*>=?\s*40\b", text), name
        # No daily distance gate of any kind.
        assert not re.search(r"kilometers\s*[<>]=?\s*(?:50|100)\b", text), f"{name} gates a day"

    css = without_comments("css/dashboard.css")
    assert not re.search(r"\[data-count", css), "the stylesheet keys on a raw count"
    assert not re.search(r"\[data-events", css), "the stylesheet keys on a raw count"
    print("PASS test_no_business_derivation_happens_in_the_browser")


def test_fixtures_match_the_generator() -> None:
    """The checked-in browser fixtures are exactly what the snapshot builder emits."""
    import eco_dashboard_fixtures as generator  # noqa: PLC0415 - test-local import

    built = generator.build_fixtures()
    for name, snapshot in built.items():
        path = APP_ROOT / "fixtures" / f"{name}.json"
        assert path.exists(), name
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk == snapshot.document, f"{name} is stale — regenerate the fixtures"
    print("PASS test_fixtures_match_the_generator")


# ------------------------------------------------- 2. the frozen frontend ---


def test_frozen_frontend_api_is_exposed() -> None:
    """R1: exactly the two functions the repository calls, and they load cleanly."""
    api = node("api")
    assert api["load_failures"] == [], api["load_failures"]
    assert api["loaded"] == ["format.js", "render.js", "snapshot-source.js", "app.js"]
    assert api["globals"]["EcoApp"] == "object", api["globals"]
    assert api["globals"]["EcoRender"] == "object", api["globals"]
    assert api["eco_app_boot"] == "function", "window.EcoApp.boot({source}) is required"
    assert api["eco_render_access_state"] == "function", \
        "window.EcoRender.renderAccessState(code) is required"
    # Anything beyond the two entrypoints is the design's business, and this
    # suite deliberately asserts nothing about it.
    print("PASS test_frozen_frontend_api_is_exposed")


def test_the_route_vocabulary_is_the_shipped_one() -> None:
    """B2: the acceptance layer must test the design's routes, not V1's.

    The old suite asserted `#<period>/<summary|detailed>`, which was V1
    presentation vocabulary and never part of the frozen boundary. The shipped
    design routes three slides. Reading the accepted shape out of the shipped
    source keeps this suite from carrying a stale second copy of it — if the
    design changes its route grammar, this fails first and loudly, instead of
    every navigation test failing obscurely.
    """
    app = without_comments("js/app.js")
    accepted = re.search(r"/\^#\(([a-z|]+)\)\\/\(\[([0-9]+)\]\)\$/", app)
    assert accepted, "js/app.js no longer parses a recognisable hash route"
    periods = accepted.group(1).split("|")
    slides = tuple(accepted.group(2))
    assert set(periods) == {"weekly", "monthly"}, periods
    assert slides == SLIDES, f"the design routes slides {slides}, this suite expects {SLIDES}"
    for period in periods:
        for slide in SLIDES:
            assert ROUTE.fullmatch(f"#{period}/{slide}"), (period, slide)
    # And the V1 vocabulary really is gone, rather than both being accepted.
    assert not ROUTE.fullmatch("#weekly/summary")
    assert "summary" not in app and "detailed" not in app, \
        "js/app.js still carries the V1 view vocabulary"
    print("PASS test_the_route_vocabulary_is_the_shipped_one")


def test_boot_takes_its_snapshot_only_from_the_supplied_source() -> None:
    """The design must not open its own transport, and must not defer the load."""
    out = node("boot_contract", "ranked_acceptable")
    assert out["threw"] is None, out["threw"]
    assert out["load_calls_immediate"] == 1, out
    print("PASS test_boot_takes_its_snapshot_only_from_the_supplied_source")


def test_access_states_render_without_a_snapshot() -> None:
    """Every transport outcome has standalone markup that reveals nothing."""
    states = node("access")
    for code in ACCESS_CODES:
        state = states[code]
        assert state["ok"], (code, state.get("error"))
        html = state["html"]
        assert len(html.strip()) > 0, code
        text = strip_tags(html)
        assert_no_broken_values(text, code)
        assert_no_private_content(text, code)
        for banned in ("<form", "<input", "token", "capability", "identyfikator"):
            assert banned not in html.lower(), (code, banned)
        # A terminal state carries no Eco value of any kind.
        assert not PERCENT.search(normalise(text)), f"{code} rendered a percentage"
        assert "na 100 km" not in text, code
    # An unrecognised code must still fail closed rather than throw.
    assert states["__unknown"]["ok"], states["__unknown"].get("error")
    assert len(states["__unknown"]["html"].strip()) > 0
    print("PASS test_access_states_render_without_a_snapshot")


def test_snapshot_input_vocabulary_is_stable() -> None:
    """The repo-owned input boundary: schema gate and HTTP status mapping."""
    out = node("source_contract")
    assert out["contract_id"] == "driver_eco_dashboard_snapshot"
    assert out["schema_version"] == 1
    assert out["valid"]["ok"] is True
    assert out["null_document"]["code"] == "SERVICE_UNAVAILABLE"
    assert out["wrong_contract"]["code"] == "SERVICE_UNAVAILABLE"
    assert out["wrong_version"]["code"] == "SERVICE_UNAVAILABLE"
    assert out["no_periods"]["code"] == "SNAPSHOT_UNAVAILABLE"
    assert out["status_401"] == "INVALID_LINK"
    assert out["status_403"] == "INVALID_LINK"
    assert out["status_410"] == "LINK_EXPIRED"
    assert out["status_404"] == "SNAPSHOT_UNAVAILABLE"
    assert out["status_429"] == "SERVICE_UNAVAILABLE"
    assert out["status_500"] == "SERVICE_UNAVAILABLE"
    assert set(out["factories"]) == {"createWorkerSource", "createFixtureSource", "createStaticSource"}
    print("PASS test_snapshot_input_vocabulary_is_stable")


# ------------------------------------------------ 3. behaviour in browser ---


def test_every_state_renders_through_the_frozen_boundary(browser: Browser, server: ContractServer) -> None:
    """Every checked-in state boots, settles and stays clean, in every experience."""
    slowest = 0.0
    for name in fixture_names():
        document = load_fixture(name)
        for period in periods_of(document):
            for slide in SLIDES:
                url = server.harness_url(fixture=name, route=f"#{period}/{slide}")
                settle = browser.mount(url)
                slowest = max(slowest, settle)
                label = f"{name}/{period}/slide{slide}"
                assert browser.errors() == [], f"{label}: {browser.errors()}"
                assert browser.api()["hasEcoApp"], label
                markup = browser.markup()
                assert len(markup.strip()) > 0, f"{label}: rendered nothing"
                text = browser.text()
                assert_no_broken_values(text, label)
                assert_no_broken_values(markup, label)
                assert_no_private_content(markup, label)
                # The synthetic identity used to build the fixtures is internal.
                assert "synthetic-driver" not in markup.lower(), label
                assert "synt00001" not in markup.lower(), label
    # Motion is allowed; never settling is not.
    assert slowest < 5.0, f"the dashboard took {slowest:.1f}s to settle"
    print("PASS test_every_state_renders_through_the_frozen_boundary")


def test_rendered_values_come_from_the_snapshot(browser: Browser, server: ContractServer) -> None:
    """Change the document, the dashboard must change with it."""
    browser.mount(server.harness_url(document="sentinels", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    headline = browser.text()
    for value in (SENTINEL_SCORE, SENTINEL_KILOMETERS):
        assert contains_number(headline, value), f"the first slide never rendered {value}"

    browser.mount(server.harness_url(document="sentinels", route=f"#weekly/{SLIDE_DAYS}"))
    daily = browser.text()
    assert contains_number(daily, SENTINEL_DAY_COUNT), "the day's raw count never rendered"
    print("PASS test_rendered_values_come_from_the_snapshot")


def test_counts_and_coefficients_stay_separately_available(browser: Browser, server: ContractServer) -> None:
    """A1 (revised 2026-08-24). Two different facts, both still reachable.

    WHAT CHANGED AND WHY. The original contract required every DAY's normalised
    coefficient to be reachable through a per-day disclosure on slide 3. That
    disclosure was removed by owner decision (`18717cf`): the daily table now
    reports counts only, and the normalised coefficient is presented once, for
    the period, on slide 2 at the axes — where it is a rate over the period's
    own distance and is the number the driver is actually scored on.

    So the requirement is NOT relaxed, it is relocated. What still has to hold
    is the thing the contract was always about: a count and a rate are two
    different facts, and neither may disappear from the dashboard. What is
    additionally asserted now is that the removed disclosure is really gone —
    a design that quietly grew a per-day coefficient back would contradict the
    approved UX just as much as one that lost the rate altogether.
    """
    # --- 1. slide 2 carries BOTH facts for the period -----------------------
    browser.mount(server.harness_url(document="sentinels", route=f"#weekly/{SLIDE_AXES}"))
    assert browser.errors() == []
    period = browser.text() + " \n " + readable(browser.markup())
    assert contains_number(period, SENTINEL_COUNT), "the period's raw count is not reachable"
    assert contains_number(period, SENTINEL_COEFFICIENT), \
        "the period's normalised coefficient is not reachable"
    # The rate is unambiguously a rate, and it is reachable WITHOUT opening
    # anything: it is the slide's own standing content, not progressive detail.
    standing = normalise(browser.text())
    assert "na 100" in standing, "the period's coefficient carries no unit"
    assert contains_number(browser.text(), SENTINEL_COEFFICIENT), \
        "the period's coefficient is only in markup, not on screen"

    # --- 2. slide 3 carries the day's raw count, with nothing to open -------
    browser.mount(server.harness_url(document="sentinels", route=f"#weekly/{SLIDE_DAYS}"))
    assert browser.errors() == []
    daily = browser.text()
    assert contains_number(daily, SENTINEL_DAY_COUNT), \
        "the day's raw count is not readable without opening anything"

    # --- 3. the removed disclosure is really gone --------------------------
    days = browser.in_frame(
        """
        var card = null;
        var panels = root.querySelectorAll('[role="tabpanel"]');
        for (var i = 0; i < panels.length; i++) {
          if (panels[i].querySelector('.ed-scrollx')) { card = panels[i]; break; }
        }
        if (!card) return null;
        return {
          expanders: card.querySelectorAll('[aria-expanded]').length,
          daytaps: card.querySelectorAll('[data-action="daytap"]').length,
          details: card.querySelectorAll('.ed-daydet, .ed-coef, .ed-caret').length,
          openable: card.querySelectorAll('.ed-openable').length,
          rows: card.querySelectorAll('.ed-day').length
        };
        """
    )
    assert days is not None, "the daily slide no longer contains the day table"
    assert days["rows"] >= 1, "the daily table rendered no day rows"
    assert days["expanders"] == 0, \
        f"the daily table grew {days['expanders']} disclosure controls the UX removed"
    assert days["daytaps"] == 0, "the per-day tap action was reintroduced"
    assert days["details"] == 0, "per-day detail markup was reintroduced"
    assert days["openable"] == 0, "a day row is still marked openable"

    # Nothing anywhere on the daily slide renders a PER-DAY coefficient: the
    # sentinel exists in the document and is deliberately not presented.
    assert not contains_number(daily, SENTINEL_DAY_COEFFICIENT), \
        "a per-day normalised coefficient is being rendered after all"
    assert not contains_number(readable(browser.markup()), SENTINEL_DAY_COEFFICIENT), \
        "a per-day normalised coefficient is hidden in the daily markup"

    # --- 4. the two concepts stay distinct, and the day points at the rate --
    text = normalise(daily)
    assert "sumy zdarze" in text, \
        "the daily table no longer says its numbers are per-day sums"
    assert "poprzednim slajdzie" in text, \
        "the daily table does not tell the driver where the rates live"

    # --- 5. and the period's rate did not cost the period's count ----------
    browser.mount(server.harness_url(document="sentinels", route=f"#weekly/{SLIDE_AXES}"))
    reopened = browser.text() + " \n " + readable(browser.markup())
    assert contains_number(reopened, SENTINEL_COUNT) and \
        contains_number(reopened, SENTINEL_COEFFICIENT), \
        "the axes lost one of the two facts on a second mount"
    print("PASS test_counts_and_coefficients_stay_separately_available")


#: The approved mobile presentation and the approved desktop presentation are
#: separated by one width, and this is it. `MOBILE_WIDTHS` are the two the
#: design was drawn against plus the narrowest supported phone; `DESKTOP_EDGE`
#: is the first width at which the desktop page must be back, untouched.
MOBILE_WIDTHS = ((320, 900), (390, 844), (759, 900))
DESKTOP_EDGE = (760, 900)


def test_the_mobile_daily_list_replaced_the_pan_track(
        browser: Browser, server: ContractServer) -> None:
    """The approved mobile page has no horizontally panning surface at all.

    WHAT CHANGED, AND WHY THIS TEST DID. The previous approved mobile design
    kept the desktop daily table and let it pan sideways inside `.ed-scrollx`;
    that made the track the one component allowed to scroll horizontally, and
    the contract was that it had to stay operable without a pointer. The
    approved mobile redesign replaces the table with a vertical list of day
    cards, so the track is not narrowed or hidden — the surface it existed for
    is gone. What has to be proved now is the stronger property: below the
    boundary NOTHING pans, and the daily data is still all there, per day,
    reachable from the keyboard as ordinary cards.

    The stylesheet still scopes `overflow-x` to `.ed-scrollx` alone, and the
    rule still lives behind the mobile media query, which is what keeps the
    desktop table's behaviour, and the no-`matchMedia` fallback, intact. That
    is asserted separately by `assert_horizontal_scrolling_is_scoped`.
    """
    for width, height in MOBILE_WIDTHS:
        where = f"{width}px"
        browser.mount(
            server.harness_url(fixture="no_driving_day", route=f"#weekly/{SLIDE_DAYS}",
                               width=width, height=height),
            viewport=width,
        )
        assert browser.errors() == [], (where, browser.errors())

        shape = browser.in_frame(
            """
            var panel = root.querySelector('[role="tabpanel"]:not([aria-hidden="true"])');
            if (!panel) return null;
            var rails = panel.querySelectorAll('[data-action="acc"]');
            return {
              panTracks: root.querySelectorAll('.' + arguments[0]).length,
              slides: root.querySelectorAll('.ed-slide').length,
              movers: root.querySelectorAll('.ed-mover').length,
              days: panel.querySelectorAll('.edm-day').length,
              openable: rails.length,
              tabbableRails: [].filter.call(rails, function (r) {
                return r.tabIndex >= 0;
              }).length,
              restDays: panel.querySelectorAll('.edm-day.rest').length
            };
            """,
            APPROVED_PAN_CLASS,
        )
        assert shape is not None, f"{where}: no visible tab panel"
        assert shape["panTracks"] == 0, \
            f"{where}: the horizontal pan track is still rendered on mobile"
        assert shape["slides"] == 0 and shape["movers"] == 0, \
            f"{where}: the desktop slide machinery is still in the mobile DOM"
        assert shape["days"] >= 1, f"{where}: the daily list rendered no day cards"
        assert shape["restDays"] >= 1, \
            f"{where}: a 0 km day is not rendered as its own neutral card"
        assert shape["openable"] == shape["days"] - shape["restDays"], (
            f"{where}: {shape['openable']} openable rails for "
            f"{shape['days'] - shape['restDays']} driving days"
        )
        assert shape["tabbableRails"] == shape["openable"], \
            f"{where}: a day card is not reachable from the keyboard"

        # Every day in the snapshot has a card, and every one of its non-zero
        # counts is readable once that card is opened.
        document = load_fixture("no_driving_day")
        days = document["periods"]["weekly"]["current"]["days"]
        assert shape["days"] == len(days), \
            f"{where}: {shape['days']} cards for {len(days)} days"

        opened = browser.in_frame(
            """
            var panel = root.querySelector('[role="tabpanel"]:not([aria-hidden="true"])');
            var rails = panel.querySelectorAll('[data-action="acc"]');
            var text = '';
            for (var i = 0; i < rails.length; i++) {
              rails[i].click();
              var id = rails[i].getAttribute('aria-controls');
              var body = id ? doc.getElementById(id) : null;
              if (body) text += ' ' + (body.innerText || body.textContent || '');
            }
            return text;
            """
        )
        counted = 0
        for day in days:
            for category in day["categories"]:
                if category.get("count"):
                    assert contains_number(normalise(opened), category["count"]), (
                        f"{where}: {day['date']} {category['key']} count "
                        f"{category['count']} is not readable once the day is opened"
                    )
                    counted += 1
        assert counted, f"{where}: the fixture carries no non-zero daily count to check"

        # And the document still does not scroll sideways, with nothing else
        # quietly panning in the track's place.
        geometry = browser.overflow()
        assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, \
            f"{where}: the document scrolls sideways ({geometry['offender']})"
        assert geometry["panners"] == [], \
            f"{where}: something pans horizontally on mobile: {geometry['panners']}"

    print("PASS test_the_mobile_daily_list_replaced_the_pan_track")


def test_the_presentation_boundary_is_the_approved_width(
        browser: Browser, server: ContractServer) -> None:
    """<= 759 px is the mobile tab page; >= 760 px is the desktop slide page.

    The two presentations are separate DOM trees produced by the same pure
    renderer, and exactly one of them exists at a time. This is the guard that
    stops either from leaking into the other: mobile machinery may not appear
    one pixel above the boundary, and desktop machinery may not appear one
    pixel below it.

    The renderer half is proved headlessly, by asking it for the desktop page
    with and without the flag and comparing the two strings: the desktop markup
    must be the SAME BYTES whether the mobile branch exists or not.
    """
    for width, height in (*MOBILE_WIDTHS, DESKTOP_EDGE, (1280, 900)):
        mobile = width <= 759
        where = f"{width}px"
        browser.mount(
            server.harness_url(fixture="ranked_acceptable", route="#weekly/1",
                               width=width, height=height),
            viewport=width,
        )
        seen = browser.in_frame(
            """
            return {
              mobileApp: root.querySelectorAll('.edm-app').length,
              tabBars: root.querySelectorAll('.edm-tabs').length,
              mobilePanels: root.querySelectorAll('.edm-panel').length,
              desktopSlides: root.querySelectorAll('.ed-slide').length,
              desktopMover: root.querySelectorAll('.ed-mover').length,
              desktopDots: root.querySelectorAll('.ed-dots').length,
              tabs: root.querySelectorAll('[role="tab"]').length,
              panels: root.querySelectorAll('[role="tabpanel"]').length,
              headings: root.querySelectorAll('h1').length
            };
            """
        )
        if mobile:
            assert seen["mobileApp"] == 1 and seen["tabBars"] == 1, (where, seen)
            assert seen["mobilePanels"] == 3, (where, seen)
            assert seen["desktopSlides"] == 0 and seen["desktopMover"] == 0 and \
                seen["desktopDots"] == 0, \
                f"{where}: desktop slide machinery leaked below the boundary: {seen}"
        else:
            assert seen["mobileApp"] == 0 and seen["tabBars"] == 0 and \
                seen["mobilePanels"] == 0, \
                f"{where}: mobile machinery leaked at or above 760px: {seen}"
            assert seen["desktopSlides"] == 3 and seen["desktopMover"] == 1 and \
                seen["desktopDots"] == 1, (where, seen)
        # Both presentations are the same page: one h1, three tabs, three panels.
        assert seen["tabs"] == 3 and seen["panels"] == 3, (where, seen)
        assert seen["headings"] == 1, f"{where}: {seen['headings']} <h1> elements"

    # The renderer's desktop output cannot depend on the mobile branch existing.
    for fixture in fixture_names():
        document = load_fixture(fixture)
        for period in periods_of(document):
            for slide in range(3):
                pair = node("render_pair", fixture, period, str(slide))
                assert pair["default"] == pair["explicit_desktop"], (
                    f"{fixture}/{period}/{slide}: the desktop markup differs "
                    "between the default call and mobile:false"
                )
                ok = document["periods"][period]["status"] == "OK"
                assert pair["mobile"] != pair["default"] or not ok, (
                    f"{fixture}/{period}/{slide}: the mobile call returned the "
                    "desktop page"
                )
                # The mobile tree is swept over the WHOLE fixture matrix here,
                # which the browser suites cannot afford to do: every state
                # must render clean values and leak nothing, not just the seven
                # the viewport sweep opens in a browser.
                where = f"mobile {fixture}/{period}/{slide}"
                content = readable(pair["mobile"])
                assert_no_broken_values(content, where)
                assert_no_private_content(strip_tags(content), where)
                if not ok:
                    # A fail-closed period is full-screen in both presentations:
                    # no tab bar, and no Eco payload to expose.
                    assert "edm-tabs" not in pair["mobile"], \
                        f"{where}: a fail-closed period rendered the tab bar"
                    text = normalise(strip_tags(content))
                    assert not PERCENT.search(text), f"{where}: rendered a percentage"
                    assert "na 100 km" not in text, f"{where}: rendered a rate"
    print("PASS test_the_presentation_boundary_is_the_approved_width")


def test_the_mobile_tabs_and_accordions_are_honest(
        browser: Browser, server: ContractServer) -> None:
    """The bottom bar is a real tablist and the disclosures tell the truth.

    Asserted as behaviour: the roving tabindex moves, `aria-selected` follows
    exactly one tab, the route follows the tab, the two tabs that are not on
    screen are out of the accessibility tree AND out of the tab order, and an
    accordion that says it is collapsed really is — its body is `hidden`, so a
    screen reader cannot read a panel the driver cannot see.
    """
    browser.mount(
        server.harness_url(fixture="ranked_acceptable", route="#weekly/1",
                           width=390, height=844),
        viewport=390,
    )
    assert browser.errors() == [], browser.errors()

    state = browser.in_frame(
        """
        var tabs = root.querySelectorAll('[role="tab"]');
        var panels = root.querySelectorAll('[role="tabpanel"]');
        var out = { tabs: [], panels: [] };
        for (var i = 0; i < tabs.length; i++) {
          out.tabs.push({
            selected: tabs[i].getAttribute('aria-selected'),
            tabIndex: tabs[i].tabIndex,
            controls: tabs[i].getAttribute('aria-controls'),
            name: (tabs[i].innerText || '').trim(),
            resolves: !!doc.getElementById(tabs[i].getAttribute('aria-controls'))
          });
        }
        for (var j = 0; j < panels.length; j++) {
          out.panels.push({
            hidden: panels[j].getAttribute('aria-hidden'),
            inert: panels[j].hasAttribute('inert') || panels[j].inert === true,
            display: win.getComputedStyle(panels[j]).display,
            labelledby: panels[j].getAttribute('aria-labelledby'),
            tabIndex: panels[j].tabIndex
          });
        }
        return out;
        """
    )
    assert [t["selected"] for t in state["tabs"]] == ["true", "false", "false"], state["tabs"]
    assert [t["tabIndex"] for t in state["tabs"]] == [0, -1, -1], state["tabs"]
    assert all(t["resolves"] for t in state["tabs"]), state["tabs"]
    assert [t["name"] for t in state["tabs"]] == ["Wynik", "Wykroczenia", "Dni"], \
        state["tabs"]
    assert state["panels"][0]["hidden"] is None and not state["panels"][0]["inert"]
    for panel in state["panels"][1:]:
        assert panel["hidden"] == "true" and panel["inert"], panel
        assert panel["display"] == "none", panel

    # Keyboard: ArrowRight moves and activates, and the route follows.
    moved = browser.in_frame(
        """
        var tabs = root.querySelectorAll('[role="tab"]');
        tabs[0].focus();
        tabs[0].dispatchEvent(new win.KeyboardEvent('keydown',
          { key: 'ArrowRight', bubbles: true }));
        return {
          selected: [].map.call(root.querySelectorAll('[role="tab"]'), function (t) {
            return t.getAttribute('aria-selected'); }).join(','),
          focused: doc.activeElement ? doc.activeElement.id : null,
          visible: [].filter.call(root.querySelectorAll('[role="tabpanel"]'),
            function (p) { return win.getComputedStyle(p).display !== 'none'; }).length
        };
        """
    )
    assert moved["selected"] == "false,true,false", moved
    assert moved["focused"] == "ed-tab-1", moved
    assert moved["visible"] == 1, moved
    assert browser.hash() == "#weekly/2", browser.hash()

    # Accordion: collapsed means hidden, expanded means readable, and the
    # trigger states which.
    disclosure = browser.in_frame(
        """
        var rail = root.querySelector('.edm-cat [data-action="acc"]');
        var body = doc.getElementById(rail.getAttribute('aria-controls'));
        var before = {
          expanded: rail.getAttribute('aria-expanded'),
          hidden: body.hasAttribute('hidden'),
          display: win.getComputedStyle(body).display
        };
        rail.click();
        var after = {
          expanded: rail.getAttribute('aria-expanded'),
          hidden: body.hasAttribute('hidden'),
          display: win.getComputedStyle(body).display,
          text: (body.innerText || '').trim()
        };
        rail.click();
        var closed = {
          expanded: rail.getAttribute('aria-expanded'),
          hidden: body.hasAttribute('hidden')
        };
        return { before: before, after: after, closed: closed,
                 isButton: rail.tagName === 'BUTTON' };
        """
    )
    assert disclosure["isButton"], "the accordion rail is not a real button"
    assert disclosure["before"] == {"expanded": "false", "hidden": True, "display": "none"}, \
        disclosure["before"]
    assert disclosure["after"]["expanded"] == "true" and \
        not disclosure["after"]["hidden"] and \
        disclosure["after"]["display"] != "none", disclosure["after"]
    assert "na 100" in normalise(disclosure["after"]["text"]), disclosure["after"]["text"]
    assert disclosure["closed"] == {"expanded": "false", "hidden": True}, disclosure["closed"]
    print("PASS test_the_mobile_tabs_and_accordions_are_honest")


def test_reduced_motion_shows_the_final_values_immediately() -> None:
    """`prefers-reduced-motion: reduce` must not cost a driver any content.

    Two things are proved with the preference forced on in the browser itself:
    nothing that the entrance animation would have revealed is left invisible,
    and no count-up runs — the score is its final value in the very first frame
    the page is sampled, not a number on its way there.
    """
    with ContractServer(SYNTHETIC_DOCUMENTS) as server, \
            Browser(width=1200, height=1000, reduced_motion=True) as browser:
        for width, height in ((390, 844), (1280, 900)):
            where = f"{width}px"
            browser.mount(
                server.harness_url(fixture="ranked_acceptable", route="#weekly/1",
                                   width=width, height=height),
                viewport=width,
            )
            reduced = browser.in_frame(
                """
                if (!win.matchMedia('(prefers-reduced-motion: reduce)').matches) return null;
                /* Every panel, not just the visible one: an animation hook
                 * that stays at opacity 0 is a defect wherever it sits, and
                 * slide 1 of the desktop page carries none of its own. */
                var faded = root.querySelectorAll('.ed-fade,.ed-casc');
                var invisible = 0;
                for (var i = 0; i < faded.length; i++) {
                  if (parseFloat(win.getComputedStyle(faded[i]).opacity) < 0.99) invisible++;
                }
                var counters = root.querySelectorAll('[data-cu]');
                var mismatched = 0;
                for (var j = 0; j < counters.length; j++) {
                  var shown = counters[j].querySelector('.cu');
                  var final = counters[j].querySelector('.sr-only');
                  if (shown && final && shown.textContent !== final.textContent) mismatched++;
                }
                return { faded: faded.length, invisible: invisible,
                         counters: counters.length, mismatched: mismatched };
                """
            )
            assert reduced is not None, \
                f"{where}: the browser did not report the reduced-motion preference"
            assert reduced["faded"] >= 1, f"{where}: nothing carries an entrance hook"
            assert reduced["invisible"] == 0, (
                f"{where}: {reduced['invisible']} of {reduced['faded']} animated "
                "elements stayed invisible under reduced motion"
            )
            assert reduced["counters"] >= 1, f"{where}: no counted value on screen"
            assert reduced["mismatched"] == 0, (
                f"{where}: {reduced['mismatched']} counted values were still "
                "animating under reduced motion"
            )

            # The trend switch lives on a monthly report, so it needs its own
            # mount; under `reduce` the thumb must JUMP to the cell, not slide.
            browser.mount(
                server.harness_url(fixture="monthly_31_days", route="#monthly/1",
                                   width=width, height=height),
                viewport=width,
            )
            thumb = browser.in_frame(
                """
                var t = root.querySelector('[class$="-trend-seg-thumb"]');
                if (!t) return null;
                var cs = win.getComputedStyle(t);
                return {dur: cs.transitionDuration, prop: cs.transitionProperty};
                """
            )
            assert thumb is not None, f"{where}: the monthly trend switch has no thumb"
            assert thumb["dur"] in ("0s", "0s, 0s", ""), (
                f"{where}: the trend thumb still slides for {thumb['dur']} under "
                "prefers-reduced-motion: reduce")
    print("PASS test_reduced_motion_shows_the_final_values_immediately")


def test_slide_navigation_is_routable(browser: Browser, server: ContractServer) -> None:
    """B2: the design's own three-slide routing, exercised as a contract.

    What is asserted is behaviour, not vocabulary decoration: every slide the
    design routes can be entered directly by URL, the route survives the mount,
    each slide renders its own content, the tablist follows the route, and an
    unroutable URL is corrected rather than left broken or left empty.
    """
    seen = set()
    for slide in SLIDES:
        browser.mount(server.harness_url(fixture="ranked_acceptable", route=f"#weekly/{slide}"))
        assert browser.errors() == [], (slide, browser.errors())
        state = browser.hash()
        assert state == f"#weekly/{slide}", f"asked for slide {slide}, landed on {state}"
        markup = browser.markup()
        assert len(markup.strip()) > 0, slide

        # The routed slide is the one that is actually selected and reachable.
        selection = browser.in_frame(
            """
            var tabs = root.querySelectorAll('[role="tab"]');
            var panels = root.querySelectorAll('[role="tabpanel"]');
            var selected = -1, live = -1;
            for (var i = 0; i < tabs.length; i++) {
              if (tabs[i].getAttribute('aria-selected') === 'true') selected = i;
            }
            for (var j = 0; j < panels.length; j++) {
              if (panels[j].getAttribute('aria-hidden') !== 'true') live = j;
            }
            return { tabs: tabs.length, panels: panels.length, selected: selected, live: live };
            """
        )
        assert selection["tabs"] == len(SLIDES), selection
        assert selection["panels"] == len(SLIDES), selection
        assert selection["selected"] == int(slide) - 1, selection
        assert selection["live"] == int(slide) - 1, \
            f"slide {slide} is routed but panel {selection['live']} is the visible one"

        # Each slide really is a different section. All three panels stay in the
        # DOM (the transition moves them), so this reads the routed panel rather
        # than the whole root, which would be identical for every route.
        seen.add(browser.in_frame(
            r"""
            var panels = root.querySelectorAll('[role="tabpanel"]');
            for (var i = 0; i < panels.length; i++) {
              if (panels[i].getAttribute('aria-hidden') !== 'true') {
                return (panels[i].textContent || '').replace(/\s+/g, ' ').trim();
              }
            }
            return '';
            """
        ))

    assert len(seen) == len(SLIDES), "two slides rendered the same content"
    assert all(text.strip() for text in seen), "a routed slide rendered nothing"

    # This fixture has no monthly period. Routing into it must not be possible,
    # must not empty the page, and must not leave a route that lies.
    document = load_fixture("ranked_acceptable")
    assert document["periods"]["monthly"] is None
    browser.set_hash(f"#monthly/{SLIDE_SCORE}")
    browser.settle()
    assert browser.errors() == [], browser.errors()
    assert len(browser.markup().strip()) > 0, "an unavailable period emptied the page"
    corrected = browser.hash()
    assert ROUTE.fullmatch(corrected), corrected
    assert corrected.startswith("#weekly/"), \
        f"an absent period was entered: the route stayed {corrected}"

    # An unrecognised route falls back to a real one instead of breaking.
    browser.mount(server.harness_url(fixture="ranked_acceptable", route="#nope/nope"))
    assert browser.errors() == []
    assert len(browser.markup().strip()) > 0
    assert ROUTE.fullmatch(browser.hash()), browser.hash()
    print("PASS test_slide_navigation_is_routable")


def test_the_capability_is_taken_before_the_design_routes(
    browser: Browser, server: ContractServer
) -> None:
    """B2's security half: the design routes over a URL that is already clean.

    The dashboard now owns the fragment — it writes `#<period>/<slide>` through
    `history.replaceState`. That is ordinary UI routing and it is the design's
    business, but it must never be able to observe, preserve or re-publish the
    capability that arrived in `#k=…`. The repository-owned bootstrap runs
    first, in <head>, and takes it out of the URL and out of history before any
    presentation script has executed.
    """
    browser.mount(server.harness_url(
        fixture="ranked_acceptable", route=f"#k={CAPABILITY_SENTINEL}"))
    assert browser.errors() == [], browser.errors()

    # The design routed — and what it routed over carries no capability.
    landed = browser.hash()
    assert ROUTE.fullmatch(landed), f"the design did not route: {landed!r}"
    assert CAPABILITY_SENTINEL not in landed

    # `document.referrer` is deliberately NOT checked: a fragment is never sent
    # in a referrer, and the harness carries the route through the shell's query
    # string, so a check here would measure the test rig rather than the page.
    url_state = browser.in_frame(
        "return { href: win.location.href, hash: win.location.hash,"
        " search: win.location.search };"
    )
    for where, value in url_state.items():
        assert CAPABILITY_SENTINEL not in (value or ""), f"the capability survived in {where}"

    # History is overwritten, not stacked. The absolute length is meaningless
    # here — the frame shares the session history of every earlier mount — so
    # what is measured is the DELTA across a slide change: the design routes
    # with `replaceState`, so Back can never walk back towards the entry the
    # capability arrived on.
    depth_before = browser.in_frame("return win.history.length;")
    browser.in_frame(
        """
        var tabs = root.querySelectorAll('[role="tab"]');
        for (var i = 0; i < tabs.length; i++) {
          if (tabs[i].getAttribute('aria-selected') !== 'true') { tabs[i].click(); return; }
        }
        """
    )
    browser.settle()
    assert ROUTE.fullmatch(browser.hash()), browser.hash()
    depth_after = browser.in_frame("return win.history.length;")
    assert depth_after == depth_before, (
        "routing pushed a history entry instead of replacing it "
        f"({depth_before} -> {depth_after})"
    )

    # It reached the repository's one-shot accessor, and nothing else.
    handover = browser.in_frame(
        """
        if (typeof win.__ecoTakeCapability !== 'function') return { available: false };
        return { available: true, first: win.__ecoTakeCapability(),
                 second: win.__ecoTakeCapability() };
        """
    )
    assert handover["available"], "the repo-owned capability accessor is gone"
    assert handover["first"] == CAPABILITY_SENTINEL, \
        "the bootstrap did not capture the capability"
    assert handover["second"] is None, "the capability stayed readable after its first read"

    # And it is nowhere the design can see it.
    haystack = browser.markup() + " \n " + browser.text()
    assert CAPABILITY_SENTINEL not in haystack, "the capability reached the DOM"
    stored = browser.in_frame(
        """
        var out = { local: null, session: null, cookie: doc.cookie || '' };
        try { out.local = win.localStorage.length; } catch (e) { out.local = 'blocked'; }
        try { out.session = win.sessionStorage.length; } catch (e) { out.session = 'blocked'; }
        return out;
        """
    )
    assert stored["cookie"] == "", f"the page set a cookie: {stored['cookie']!r}"
    for store in ("local", "session"):
        assert stored[store] in (0, "blocked"), f"the page wrote to {store}Storage"
    print("PASS test_the_capability_is_taken_before_the_design_routes")


def test_fail_closed_states_expose_no_eco_payload(browser: Browser, server: ContractServer) -> None:
    """A period that did not qualify, or is not scored, shows nothing but its identity."""
    for name, expected in (("insufficient_period_distance", "INSUFFICIENT_DISTANCE"),
                           ("report_not_ready", "REPORT_NOT_READY")):
        document = load_fixture(name)
        assert document["periods"]["weekly"]["status"] == expected
        browser.mount(server.harness_url(fixture=name, route=f"#weekly/{SLIDE_SCORE}"))
        assert browser.errors() == [], name
        markup = browser.markup()
        text = normalise(browser.text())
        assert len(markup.strip()) > 0, name
        assert not PERCENT.search(text), f"{name} rendered a percentage"
        assert "na 100 km" not in text, name
        assert not re.search(r"\d+\s?pkt", text), f"{name} rendered a points value"
        assert_no_broken_values(markup, name)
        assert_no_private_content(markup, name)

    # A mis-published document that carries a payload it should not: none of it
    # may reach the DOM, an attribute, or the page source.
    for document_name in ("poisoned_insufficient", "poisoned_not_ready"):
        for slide in SLIDES:
            browser.mount(server.harness_url(document=document_name, route=f"#weekly/{slide}"))
            label = f"{document_name}/slide{slide}"
            assert browser.errors() == [], label
            haystack = readable(browser.markup()) + " \n " + browser.text()
            for sentinel in (SENTINEL_SCORE, SENTINEL_KILOMETERS, SENTINEL_TRIPS,
                             SENTINEL_COUNT, SENTINEL_COEFFICIENT,
                             SENTINEL_DAY_COUNT, SENTINEL_DAY_COEFFICIENT):
                assert not contains_number(haystack, sentinel), \
                    f"{label} leaked a suppressed Eco value ({sentinel})"
    print("PASS test_fail_closed_states_expose_no_eco_payload")


def test_ranking_absence_is_valid_and_never_invented(browser: Browser, server: ContractServer) -> None:
    """No rank is a normal dashboard, not an error and not a placeholder.

    The three documents below differ ONLY in their ranking facts, and those
    facts carry values that cannot appear by coincidence — so the assertions
    below are about what the design read from the snapshot, not about what it
    looks like.
    """
    browser.mount(server.harness_url(document="ranked_sentinels", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    ranked = normalise(browser.text())
    assert contains_number(ranked, SENTINEL_PARTICIPANTS), "a ranked driver lost the ranking size"
    assert contains_number(ranked, SENTINEL_RANK), "a ranked driver lost the position"
    assert contains_number(ranked, SENTINEL_RANK_DELTA), "a ranked driver lost the rank movement"

    browser.mount(server.harness_url(document="unranked_sentinels", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    unranked = normalise(browser.text())
    block = load_fixture("ranked_acceptable")["periods"]["weekly"]["current"]
    # The Eco dashboard itself is intact — this is a valid dashboard, not an error.
    assert contains_number(unranked, block["eco_score_total"]), "the score disappeared"
    assert contains_number(unranked, block["total_kilometers"]), "the distance disappeared"
    # ... and not one ranking fact was invented.
    for sentinel, what in ((SENTINEL_PARTICIPANTS, "ranking size"),
                           (SENTINEL_RANK, "position"),
                           (SENTINEL_RANK_DELTA, "rank movement")):
        assert not contains_number(unranked, sentinel), f"an unranked driver was given a {what}"
    assert not ZERO_PERCENT.search(unranked), "an absent group share was rendered as zero"

    # A newly ranked driver has a position but nothing to compare it with.
    browser.mount(server.harness_url(document="newly_ranked_sentinels", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    newly = normalise(browser.text())
    assert contains_number(newly, SENTINEL_RANK), "a newly ranked driver lost the position"
    assert not contains_number(newly, SENTINEL_RANK_DELTA), \
        "a rank movement was invented for a driver with no comparable rank"

    # The checked-in states carry the same rule with their own data.
    participants = load_fixture("ranked_acceptable")["periods"]["weekly"]["current"][
        "ranking_total_participants"]
    for name in ("not_ranked_by_configuration", "not_on_roster", "left_ranking"):
        document = load_fixture(name)
        current = document["periods"]["weekly"]["current"]
        assert current["ranking_state"] != "RANKED"
        assert current.get("rating_group_distribution") is None
        browser.mount(server.harness_url(fixture=name, route=f"#weekly/{SLIDE_SCORE}"))
        assert browser.errors() == [], name
        text = normalise(browser.text())
        assert contains_number(text, current["eco_score_total"]), f"{name} lost the score"
        assert not contains_number(text, participants), f"{name} invented a ranking size"
        assert_no_private_content(browser.markup(), name)
    print("PASS test_ranking_absence_is_valid_and_never_invented")


def test_sparse_rating_distribution_renders_only_present_buckets(
    browser: Browser, server: ContractServer
) -> None:
    """`rating_group_distribution` is SPARSE by construction.

    The host derives it by grouping the ranked, qualified, rated population, so
    a group nobody is in produces no row and therefore no key. Any non-empty
    subset of {safe, acceptable, dangerous} is a valid distribution, and an
    absent bucket means absent — not zero, not a dash, not a placeholder.

    The V1 renderer walked a fixed safe/acceptable/dangerous order and left a
    zero-width segment, an empty value after the absent bucket's label and the
    literal "null" in the bar's accessible name. That residue used to be
    recorded by a characterisation test that existed only until the replacement
    frontend landed; it has landed, so the residue is now asserted absent here
    instead — the product rule and its former exceptions in one place.
    """
    browser.mount(server.harness_url(fixture="ranked_acceptable", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    full_text = normalise(browser.text())
    full_percentages = PERCENT.findall(full_text)
    full_block = load_fixture("ranked_acceptable")["periods"]["weekly"]["current"]
    assert set(full_block["rating_group_distribution"]) == {"safe", "acceptable", "dangerous"}

    browser.mount(server.harness_url(document="sparse_ranked", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == [], browser.errors()
    sparse_text = normalise(browser.text())
    sparse_percentages = PERCENT.findall(sparse_text)

    # The field is genuinely read: one bucket fewer produces less output. A
    # hardcoded three-bucket chart would not move.
    assert len(sparse_percentages) < len(full_percentages), \
        "removing a distribution bucket changed nothing — the buckets are hardcoded"
    # The absent bucket is not given a fabricated value.
    fabricated = ZERO_PERCENT.search(sparse_text)
    assert not fabricated, f"an absent bucket rendered as zero: {fabricated.group()!r}"
    for dash in ("niebezpieczny —", "niebezpieczny –", "niebezpieczny -", "niebezpieczny brak"):
        assert dash not in sparse_text, f"an absent bucket rendered as {dash!r}"
    # The buckets that do exist are still shown.
    for value in (49.37, 50.63):
        assert contains_number(sparse_text, value) or contains_number(sparse_text, round(value, 1)), \
            f"the present bucket {value} disappeared"
    # And a sparse distribution is not an error state.
    assert contains_number(sparse_text, full_block["eco_score_total"]), "the dashboard collapsed"

    # The three former V1 residues, asserted absent. A bucket nobody is in
    # produces no segment, no empty value and no "null" in an accessible name.
    sparse_markup = browser.markup()
    assert not re.search(r"width:\s*0(?:\.0+)?%", sparse_markup), \
        "an absent bucket still produces a zero-width segment"
    assert not re.search(r'aria-label="[^"]*\bnull\b', sparse_markup), \
        "an absent bucket still puts \"null\" in an accessible name"
    assert not re.search(r"niebezpieczny\s*</", sparse_markup), \
        "an absent bucket still renders its label with an empty value"
    assert_no_broken_values(sparse_markup, "sparse_ranked")
    assert_no_broken_values(sparse_text, "sparse_ranked")
    print("PASS test_sparse_rating_distribution_renders_only_present_buckets")


def test_the_distance_gate_is_period_level_only(browser: Browser, server: ContractServer) -> None:
    """Inside a qualified period, a short day is an ordinary day."""
    document = load_fixture("ranked_acceptable")
    block = document["periods"]["weekly"]["current"]
    assert block["total_kilometers"] >= document["constants"]["min_qualifying_distance_km"]
    short_days = [day for day in block["days"] if 0 < day["kilometers"] < 100]
    assert short_days, "the fixture no longer contains a sub-100 km day"

    browser.mount(server.harness_url(fixture="ranked_acceptable", route=f"#weekly/{SLIDE_DAYS}"))
    assert browser.errors() == []
    text = normalise(browser.text())
    markup = browser.markup()

    for day in short_days:
        # The day is present, with its own distance, and it is evaluated.
        assert day["date"][-2:] in text or contains_number(text, day["kilometers"]), day["date"]
        evaluated = [c for c in day["categories"] if c["coefficient_per_100km"] is not None]
        assert evaluated, day["date"]

    # A zero-kilometre day is the neutral state, never a fabricated evaluation.
    zero_days = [day for day in load_fixture("no_driving_day")["periods"]["weekly"]["current"]["days"]
                 if day["kilometers"] == 0]
    assert zero_days
    for day in zero_days:
        assert all(c["coefficient_per_100km"] is None and c["status"] == "neutral"
                   for c in day["categories"]), day["date"]
    browser.mount(server.harness_url(fixture="no_driving_day", route=f"#weekly/{SLIDE_DAYS}"))
    assert browser.errors() == []
    assert_no_broken_values(browser.markup(), "no_driving_day")

    # No daily threshold may be announced, implied or coded.
    for banned in ("dzienny próg", "próg dzienny", "minimum dzienne", "min. 50 km", "min. 100 km"):
        assert banned.lower() not in text.lower(), banned
    assert "min_daily_evaluation_km" not in markup
    print("PASS test_the_distance_gate_is_period_level_only")


def test_absent_comparison_is_not_rendered_as_zero_change(
    browser: Browser, server: ContractServer
) -> None:
    """`comparison: null` is a state of its own, never a zero delta.

    Both documents are identical apart from the comparison block, and the
    movements they carry are values that cannot appear by coincidence — so this
    asserts what the design read, not how it looks.
    """
    browser.mount(server.harness_url(document="comparable_sentinels", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    comparable = normalise(browser.text())
    assert contains_number(comparable, SENTINEL_SCORE_DELTA), "the score movement never rendered"
    assert contains_number(comparable, SENTINEL_DISTANCE_DELTA), "the distance movement never rendered"

    browser.mount(server.harness_url(document="no_basis_sentinels", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    no_basis = normalise(browser.text())
    assert_no_broken_values(browser.markup(), "no_basis_sentinels")

    # Nothing from the removed basis survives ...
    assert not contains_number(no_basis, SENTINEL_SCORE_DELTA), "a score movement was invented"
    assert not contains_number(no_basis, SENTINEL_DISTANCE_DELTA), "a distance movement was invented"
    # ... and nothing is replaced by a fabricated "no change". An unsigned `0 pkt`
    # is a legitimate value elsewhere on the page (a category that lost nothing);
    # a SIGNED zero is only ever a delta, so that is what is forbidden here.
    signed_zero = re.search(r"[+\u2212-]\s?0(?:[.,]0+)?\s?(?:pkt|km|miejsc|%)", no_basis)
    assert not signed_zero, f"a zero change was fabricated: {signed_zero.group()!r}"
    assert not ZERO_PERCENT.search(no_basis), "a zero-percent change was fabricated"
    # The state is communicated rather than silently dropped.
    assert no_basis != comparable, "a missing comparison changed nothing in the output"

    # The checked-in state renders the same way.
    document = load_fixture("no_comparison")
    assert document["periods"]["weekly"]["current"]["comparison"] is None
    browser.mount(server.harness_url(fixture="no_comparison", route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    fixture_text = normalise(browser.text())
    assert not re.search(r"[+\u2212-]\s?0(?:[.,]0+)?\s?(?:pkt|km|miejsc|%)", fixture_text)
    assert_no_broken_values(browser.markup(), "no_comparison")
    print("PASS test_absent_comparison_is_not_rendered_as_zero_change")


def test_period_distance_carries_its_movement(
    browser: Browser, server: ContractServer
) -> None:
    """A2. The period distance is shown with how it moved, or with nothing.

    Both documents are the same period; only the comparison differs. Where a
    basis exists the driver gets three facts — this period's distance, the
    direction of travel, and the previous period's own absolute value — so the
    difference is never the only evidence. Where no basis exists, nothing is
    fabricated: no zero, no neutral arrow, no invented previous value.
    """
    comparable = comparable_sentinel_document()["periods"]["weekly"]["current"]
    now_km = round(comparable["total_kilometers"])
    was_km = round(comparable["comparison"]["previous_total_kilometers"])
    movement = abs(was_km - now_km)
    assert movement == SENTINEL_DISTANCE_DELTA, (now_km, was_km)

    browser.mount(server.harness_url(document="comparable_sentinels",
                                     route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    with_basis = normalise(browser.text())
    assert contains_number(with_basis, now_km), "this period's distance is not shown"
    assert contains_number(with_basis, was_km), \
        "the previous period's own distance is not shown alongside the movement"
    assert contains_number(with_basis, movement), "the distance movement is not shown"
    # The movement is directional, not a bare number.
    assert re.search(r"[\u25B2\u25BC=]", with_basis), "the movement carries no direction"
    assert_no_broken_values(browser.markup(), "comparable_sentinels")

    browser.mount(server.harness_url(document="no_basis_sentinels",
                                     route=f"#weekly/{SLIDE_SCORE}"))
    assert browser.errors() == []
    no_basis = normalise(browser.text())
    assert contains_number(no_basis, now_km), "this period's distance disappeared with the basis"
    assert not contains_number(no_basis, was_km), "a previous distance was invented"
    assert not contains_number(no_basis, movement), "a distance movement was invented"
    assert not re.search(r"[+\u2212\u25B2\u25BC-]\s?0(?:[.,]0+)?\s?km", no_basis), \
        "a zero distance movement was fabricated"
    assert no_basis != with_basis, "losing the comparison changed nothing"
    assert_no_broken_values(browser.markup(), "no_basis_sentinels")
    print("PASS test_period_distance_carries_its_movement")


def test_keyboard_and_accessibility_contract(browser: Browser, server: ContractServer) -> None:
    """Structure a driver can operate without a mouse, whatever the design looks like."""
    browser.mount(server.harness_url(fixture="ranked_acceptable", route=f"#weekly/{SLIDE_DAYS}"))
    assert browser.errors() == []

    audit = browser.in_frame(
        r"""
        var interactive = root.querySelectorAll('button, a[href], [role="tab"], [tabindex]');
        var expanders = root.querySelectorAll('[aria-expanded]');
        var brokenControls = [];
        for (var i = 0; i < expanders.length; i++) {
          var control = expanders[i];
          /* aria-controls is an ID LIST: one disclosure may legitimately reveal
           * more than one region (the axis reveal shows band labels AND band
           * points). Every id in it must resolve. */
          var ids = (control.getAttribute('aria-controls') || '').split(/\s+/)
            .filter(function (value) { return value.length > 0; });
          var tag = control.tagName.toLowerCase();
          var isControl = tag === 'button' || tag === 'a' || control.getAttribute('role') === 'button'
            || control.getAttribute('role') === 'tab';
          var dangling = ids.filter(function (id) { return !doc.getElementById(id); });
          if (!isControl || dangling.length) {
            brokenControls.push(tag + '#' + ids.join('+') + (dangling.length ? ' -> ' + dangling.join('+') : ''));
          }
        }
        var positiveTabindex = 0, unfocusable = 0;
        for (var j = 0; j < interactive.length; j++) {
          var value = interactive[j].getAttribute('tabindex');
          if (value !== null && parseInt(value, 10) > 0) positiveTabindex++;
        }
        var clickable = root.querySelectorAll('div[onclick], span[onclick], td[onclick], li[onclick]');
        return {
          headings: root.querySelectorAll('h1').length,
          interactive: interactive.length,
          expanders: expanders.length,
          brokenControls: brokenControls,
          positiveTabindex: positiveTabindex,
          onclickElements: clickable.length,
          tablists: root.querySelectorAll('[role="tablist"]').length,
          tabpanels: root.querySelectorAll('[role="tabpanel"]').length,
          imgRoles: root.querySelectorAll('[role="img"], svg[aria-label], figure').length,
          hiddenWithoutButton: 0
        };
        """
    )
    assert audit["headings"] == 1, f"exactly one <h1> is required, found {audit['headings']}"
    assert audit["interactive"] >= 4, audit
    assert audit["onclickElements"] == 0, "an interactive element is not a real control"
    assert audit["positiveTabindex"] == 0, "positive tabindex breaks the tab order"
    assert audit["brokenControls"] == [], audit["brokenControls"]
    assert audit["tablists"] >= 1 and audit["tabpanels"] >= 1, audit
    assert audit["expanders"] >= 1, "the detailed view exposes no disclosure control"

    # A3 — the slide navigation is a real tablist, not dots that look like one.
    tablist = browser.in_frame(
        """
        var list = root.querySelector('[role="tablist"]');
        var tabs = root.querySelectorAll('[role="tab"]');
        var panels = root.querySelectorAll('[role="tabpanel"]');
        var selected = 0, roving = 0, broken = [], inert = 0, labelled = 0;
        for (var i = 0; i < tabs.length; i++) {
          var tab = tabs[i];
          if (tab.getAttribute('aria-selected') === 'true') selected++;
          if (String(tab.tabIndex) === '0') roving++;
          var controls = tab.getAttribute('aria-controls');
          if (!controls || !doc.getElementById(controls)) broken.push('tab:' + controls);
        }
        for (var j = 0; j < panels.length; j++) {
          var panel = panels[j];
          var by = panel.getAttribute('aria-labelledby');
          if (by && doc.getElementById(by)) labelled++;
          if (panel.getAttribute('aria-hidden') === 'true') {
            inert++;
            if (!(panel.hasAttribute('inert') || panel.inert === true)) {
              broken.push('panel-not-inert:' + panel.id);
            }
          }
        }
        return {
          hasList: !!list,
          orientation: list ? list.getAttribute('aria-orientation') : null,
          named: list ? !!(list.getAttribute('aria-label') ||
                           list.getAttribute('aria-labelledby')) : false,
          tabs: tabs.length, panels: panels.length, selected: selected,
          roving: roving, hidden: inert, labelled: labelled, broken: broken
        };
        """
    )
    assert tablist["hasList"], "the slide navigation is not a tablist"
    assert tablist["named"], "the tablist has no accessible name"
    assert tablist["tabs"] == len(SLIDES), tablist
    assert tablist["panels"] == len(SLIDES), tablist
    assert tablist["selected"] == 1, f"{tablist['selected']} tabs claim to be selected"
    assert tablist["roving"] == 1, \
        f"roving tabindex is broken: {tablist['roving']} tabs are in the tab order"
    assert tablist["hidden"] == len(SLIDES) - 1, tablist
    assert tablist["labelled"] == len(SLIDES), "a tabpanel is not labelled by its tab"
    assert tablist["broken"] == [], tablist["broken"]

    # Keyboard focus reaches the navigation, and the arrow keys drive it.
    keyboard = browser.in_frame(
        """
        function press(node, key) {
          node.dispatchEvent(new win.KeyboardEvent('keydown', {
            key: key, bubbles: true, cancelable: true }));
        }
        function selectedIndex() {
          var tabs = root.querySelectorAll('[role="tab"]');
          for (var i = 0; i < tabs.length; i++) {
            if (tabs[i].getAttribute('aria-selected') === 'true') return i;
          }
          return -1;
        }
        var tabs = root.querySelectorAll('[role="tab"]');
        var start = selectedIndex();
        /* Roving tabindex: the SELECTED tab is the one in the tab order, so
         * that is the one a keyboard user lands on. */
        var entry = tabs[start];
        entry.focus();
        var focused = doc.activeElement === entry;
        var rovingIsSelected = String(entry.tabIndex) === '0';
        press(doc.activeElement, 'ArrowDown');
        var afterNext = selectedIndex();
        var followsFocus = doc.activeElement === tabs[afterNext];
        press(doc.activeElement, 'End');
        var afterEnd = selectedIndex();
        press(doc.activeElement, 'Home');
        var afterHome = selectedIndex();
        return { focused: focused, rovingIsSelected: rovingIsSelected,
                 start: start, afterNext: afterNext, followsFocus: followsFocus,
                 afterEnd: afterEnd, afterHome: afterHome,
                 hash: win.location.hash, count: tabs.length };
        """
    )
    assert keyboard["focused"], "a tab control cannot take keyboard focus"
    assert keyboard["rovingIsSelected"], \
        "the tab in the tab order is not the selected one"
    expected_next = (keyboard["start"] + 1) % keyboard["count"]
    assert keyboard["afterNext"] == expected_next, \
        f"ArrowDown did not move the selection: {keyboard}"
    assert keyboard["followsFocus"], "focus did not follow the selection"
    assert keyboard["afterEnd"] == keyboard["count"] - 1, f"End did not reach the last tab: {keyboard}"
    assert keyboard["afterHome"] == 0, f"Home did not reach the first tab: {keyboard}"
    assert ROUTE.fullmatch(keyboard["hash"]), keyboard
    assert keyboard["hash"].endswith("/1"), \
        f"Home selected the first tab but the route says {keyboard['hash']}"
    browser.settle()
    assert browser.errors() == [], browser.errors()

    # A3 — the disclosures are honest: activating one really reveals its target.
    browser.mount(server.harness_url(fixture="ranked_acceptable", route=f"#weekly/{SLIDE_DAYS}"))
    disclosure = browser.in_frame(
        r"""
        var control = root.querySelector('[aria-expanded="false"]');
        if (!control) return { found: false };
        var ids = (control.getAttribute('aria-controls') || '').split(/\s+/).filter(Boolean);
        function state() {
          return ids.map(function (id) {
            var node = doc.getElementById(id);
            return { id: id, exists: !!node,
                     hidden: node ? (node.hidden ||
                       node.getAttribute('aria-hidden') === 'true') : null };
          });
        }
        var before = state();
        control.click();
        var expanded = control.getAttribute('aria-expanded');
        var after = state();
        control.click();
        return { found: true, ids: ids, before: before, after: after,
                 expanded: expanded,
                 collapsedAgain: control.getAttribute('aria-expanded') };
        """
    )
    assert disclosure["found"], "the daily view exposes no disclosure control"
    assert disclosure["ids"], "a disclosure control names nothing it controls"
    assert disclosure["expanded"] == "true", "activating a disclosure did not expand it"
    assert disclosure["collapsedAgain"] == "false", "a disclosure cannot be closed again"
    for before, after in zip(disclosure["before"], disclosure["after"]):
        assert before["exists"] and after["exists"], before
        assert before["hidden"] is True, f"{before['id']} was already revealed while collapsed"
        assert after["hidden"] is False, f"{after['id']} stayed hidden after expanding"
    browser.settle()
    assert browser.errors() == [], browser.errors()

    css = source_of("css/dashboard.css")
    assert ":focus-visible" in css, "no visible focus style"
    assert "prefers-reduced-motion" in css, "motion is not reducible"
    assert "forced-colors" in css, "high-contrast mode is not handled"
    assert_horizontal_scrolling_is_scoped(css)
    print("PASS test_keyboard_and_accessibility_contract")


#: Read the switch: where the thumb actually is, where each cell actually is.
_MEASURE_SWITCH = """
var track = root.querySelector('[role="tablist"][data-selected]');
if (!track) return null;
var thumb = track.querySelector('[class$="-trend-seg-thumb"]');
var tabs = track.querySelectorAll('[role="tab"]');
if (!thumb || tabs.length !== 2) return {track: !!track, thumb: !!thumb, tabs: tabs.length};
function box(el) { var r = el.getBoundingClientRect();
                   return {x: r.left, w: r.width, h: r.height}; }
var cs = win.getComputedStyle(thumb);
var ts = win.getComputedStyle(track);
return {
  selected: track.getAttribute("data-selected"),
  ariaAt: tabs[0].getAttribute("aria-selected") === "true" ? 0 : 1,
  hidden: thumb.getAttribute("aria-hidden"),
  thumb: box(thumb), cells: [box(tabs[0]), box(tabs[1])],
  track: box(track), trackBg: ts.backgroundColor,
  prop: cs.transitionProperty, dur: cs.transitionDuration,
  zLabel: win.getComputedStyle(tabs[0]).zIndex
};
"""


def test_the_trend_thumb_lands_on_the_cell_it_claims(
        browser: Browser, server: ContractServer) -> None:
    """The switch is measured, not inspected.

    First paint must already be ON the selected cell — a thumb that animates
    in from cell 0 is the exact defect `data-selected` in the markup exists to
    prevent, and it is invisible to any markup-only assertion. Then a click
    must move it by exactly one cell, and only `transform` may be animating.
    """
    for tree, width, height in (("desktop", 1280, 900), ("mobile", 390, 844)):
        browser.resize(width, height)
        browser.mount(server.harness_url(fixture="monthly_31_days", route="#monthly/1",
                                         width=width, height=height),
                      viewport=width)
        first = browser.in_frame(_MEASURE_SWITCH)
        assert first and "thumb" in first and isinstance(first["thumb"], dict), \
            f"{tree}: no measurable trend switch ({first})"
        assert first["hidden"] == "true", f"{tree}: the thumb is exposed to assistive tech"
        assert first["selected"] == "0" and first["ariaAt"] == 0, \
            f"{tree}: the comparison tab must open selected ({first['selected']})"
        # The track carries the fill; a transparent track is not a switch.
        assert "rgba(0, 0, 0, 0)" not in first["trackBg"], \
            f"{tree}: the track has no fill ({first['trackBg']})"
        # Equal cells, and the thumb is one of them, already in place.
        assert abs(first["cells"][0]["w"] - first["cells"][1]["w"]) <= 1, \
            f"{tree}: the cells are not equal width ({first['cells']})"
        assert abs(first["thumb"]["w"] - first["cells"][0]["w"]) <= 1.5, \
            f"{tree}: the thumb is not one cell wide ({first['thumb']} vs {first['cells'][0]})"
        assert abs(first["thumb"]["x"] - first["cells"][0]["x"]) <= 1.5, \
            f"{tree}: FIRST PAINT is not on the selected cell — the thumb would " \
            f"slide in on mount ({first['thumb']['x']} vs {first['cells'][0]['x']})"
        assert first["prop"] == "transform", \
            f"{tree}: the thumb animates {first['prop']!r}, which is not transform alone"
        assert first["dur"] not in ("0s", ""), f"{tree}: the thumb does not animate at all"
        assert first["zLabel"] == "1", f"{tree}: labels do not sit above the thumb"

        browser.in_frame(
            """root.querySelectorAll('[role="tab"][data-action="trend"]')[1].click();"""
        )
        browser.settle()
        moved = browser.in_frame(_MEASURE_SWITCH)
        assert moved["selected"] == "1" and moved["ariaAt"] == 1, \
            f"{tree}: the click did not move the switch ({moved['selected']})"
        assert abs(moved["thumb"]["x"] - moved["cells"][1]["x"]) <= 1.5, \
            f"{tree}: the thumb did not land on cell 1 " \
            f"({moved['thumb']['x']} vs {moved['cells'][1]['x']})"
        # It moved by exactly one cell, and the track did not resize under it.
        assert abs((moved["thumb"]["x"] - first["thumb"]["x"])
                   - first["cells"][0]["w"]) <= 1.5, f"{tree}: not a one-cell slide"
        assert abs(moved["track"]["w"] - first["track"]["w"]) <= 0.5, \
            f"{tree}: the track resized when the tab changed — the switch is layout-bound"

    # The narrowest supported phone still has nothing to pan.
    browser.resize(320, 720)
    browser.mount(server.harness_url(fixture="monthly_31_days", route="#monthly/1",
                                     width=320, height=720), viewport=320)
    narrow = browser.in_frame(_MEASURE_SWITCH)
    assert narrow["track"]["w"] <= 320, f"320px: the track is {narrow['track']['w']}px wide"
    wide = browser.overflow()
    assert wide["scrollWidth"] <= wide["clientWidth"] + 1, (
        f"320px: the document pans sideways ({wide['scrollWidth']} > "
        f"{wide['clientWidth']}, widest {wide['offender']})")
    unapproved = [pan for pan in wide["panners"] if not pan["approved"]]
    assert unapproved == [], f"320px: an unapproved pan surface appeared: {unapproved}"
    browser.resize(1280, 900)
    print("PASS test_the_trend_thumb_lands_on_the_cell_it_claims")


def test_the_band_ramp_is_one_table() -> None:
    """The stylesheet paints the desktop bands and render.js paints the mobile
    ones. Two lists of colours is a drift waiting to happen, so they are pinned
    to each other here — the same discipline the retention constants use.
    """
    render = source_of("js/render.js")
    css = without_comments("css/dashboard.css")

    ramp = re.search(r"var SEG_RAMP = \{(.*?)\};", render, re.S)
    assert ramp, "SEG_RAMP is gone from render.js"
    js_ramp = {
        status: re.findall(r'"(#[0-9A-Fa-f]{6})"', body)
        for status, body in re.findall(r"(\w+): \[([^\]]*)\]", ramp.group(1))
    }
    assert js_ramp["yellow"][0] == "#F3E3B8" and js_ramp["red"][0] == "#EFB4AB", js_ramp
    assert len(js_ramp["yellow"]) == 4 and len(js_ramp["red"]) == 3, js_ramp

    for letter, status in (("y", "yellow"), ("r", "red"), ("g", "green")):
        base = re.search(r"\.ed-seg\." + letter + r"\{background:(#[0-9A-Fa-f]{6})\}", css)
        assert base, f"no base rule for .ed-seg.{letter}"
        assert base.group(1).upper() == js_ramp[status][0].upper(), (
            f".ed-seg.{letter} base {base.group(1)} != SEG_RAMP {status}[0] {js_ramp[status][0]}")
        for step in range(1, len(js_ramp[status])):
            rule = re.search(
                r"\.ed-seg\." + letter + r'\[data-step="' + str(step) +
                r'"\]\{background:(#[0-9A-Fa-f]{6})\}', css)
            assert rule, f".ed-seg.{letter}[data-step=\"{step}\"] missing from the stylesheet"
            assert rule.group(1).upper() == js_ramp[status][step].upper(), (
                f"step {step} of {status}: css {rule.group(1)} != js {js_ramp[status][step]}")
        # And no step beyond what the ramp defines.
        beyond = len(js_ramp[status])
        assert f'.ed-seg.{letter}[data-step="{beyond}"]' not in css, (
            f".ed-seg.{letter} paints a step the ramp does not define")

    # The legend still describes what the bands do.
    assert "#CDE3CF,#F3E3B8,#F3CBA6,#EFB4AB" in css, "the legend gradient moved"
    assert js_ramp["yellow"][-1] == "#F3CBA6", (
        "the yellow ramp no longer ends on the legend's third stop")
    print("PASS test_the_band_ramp_is_one_table "
          f"(yellow {len(js_ramp['yellow'])} steps, red {len(js_ramp['red'])})")


def test_consecutive_bands_of_one_status_are_distinguishable() -> None:
    """The complaint was that a run of one status reads as a single block."""
    for fixture in fixture_names():
        document = load_fixture(fixture)
        for period in periods_of(document):
            markup = node("render_pair", fixture, period, "1")["default"]
            for row in re.findall(r'<div class="ed-axis-segs".*?</div>\s*<div class="ed-tips',
                                  markup, re.S):
                steps = re.findall(r'class="ed-seg (\w)" data-step="(\d)"', row)
                if len(steps) < 2:
                    continue
                for (cls_a, step_a), (cls_b, step_b) in zip(steps, steps[1:]):
                    if cls_a == cls_b and cls_a in ("y", "r"):
                        assert step_a != step_b, (
                            f"{fixture}/{period}: two consecutive {cls_a} bands share "
                            f"step {step_a} — they will paint identically")
    # And the ramp is long enough for the longest run the data actually makes,
    # so a future fixture with a longer one fails here instead of blending.
    longest: dict[str, int] = {}
    for fixture in fixture_names():
        document = load_fixture(fixture)
        for period in periods_of(document):
            current = document["periods"][period].get("current") or {}
            for category in current.get("categories") or []:
                bands = category.get("bands") or []
                run = 1
                for a, b in zip(bands, bands[1:]):
                    run = run + 1 if a["status"] == b["status"] else 1
                    longest[b["status"]] = max(longest.get(b["status"], 1), run)
    render = source_of("js/render.js")
    ramp = re.search(r"var SEG_RAMP = \{(.*?)\};", render, re.S).group(1)
    sizes = {status: len(re.findall(r'"#[0-9A-Fa-f]{6}"', body))
             for status, body in re.findall(r"(\w+): \[([^\]]*)\]", ramp)}
    for status, run in sorted(longest.items()):
        assert sizes.get(status, 0) >= run, (
            f"{status} runs up to {run} bands but its ramp has only "
            f"{sizes.get(status, 0)} steps — the tail would clamp and blend")
    # ANCHORING. The last band of every run takes the DEEPEST stop its status
    # has, so "the worst band" is one colour on every axis regardless of how
    # many bands the run happens to contain. Anchored at the light end instead,
    # a 2-band red run stopped at the middle red while a 3-band run reached the
    # deepest, and the same severity read as two different reds.
    deepest = {"y": sizes["yellow"] - 1, "r": sizes["red"] - 1, "g": sizes["green"] - 1}
    checked = 0
    for fixture in fixture_names():
        document = load_fixture(fixture)
        for period in periods_of(document):
            markup = node("render_pair", fixture, period, "1")["default"]
            for row in re.findall(r'<div class="ed-axis-segs".*?</div>\s*<div class="ed-tips',
                                  markup, re.S):
                bands = re.findall(r'class="ed-seg (\w)" data-step="(\d)"', row)
                for index, (letter, step) in enumerate(bands):
                    if letter not in deepest:
                        continue
                    last_of_run = (index + 1 == len(bands)
                                   or bands[index + 1][0] != letter)
                    if last_of_run:
                        assert int(step) == deepest[letter], (
                            f"{fixture}/{period}: a run of '{letter}' ends on step {step}, "
                            f"not the deepest ({deepest[letter]}) — the worst band of this "
                            "axis is a different colour from the worst band of another")
                        checked += 1
    assert checked > 0, "no runs were checked"
    print(f"PASS the severe end of every run is anchored ({checked} runs)")
    print("PASS test_consecutive_bands_of_one_status_are_distinguishable "
          f"(longest runs {dict(sorted(longest.items()))}, ramps {dict(sorted(sizes.items()))})")


def test_the_distribution_segments_kept_their_flat_band(
        browser: Browser, server: ContractServer) -> None:
    """REGRESSION, measured. When the trend tablist was called `.ed-seg` it
    handed every axis and band distribution segment a 1 px border, a pill
    radius and 2 px of padding — a contiguous coloured band became a row of
    outlined lozenges. Nothing in the suite saw it, so this measures it.
    """
    for tree, width, height, selector in (
            ("desktop", 1280, 900, ".ed-axis-segs > .ed-seg"),
            ("mobile", 390, 844, ".edm-seg-bar > .edm-seg")):
        browser.resize(width, height)
        browser.mount(server.harness_url(fixture="monthly_31_days", route="#monthly/2",
                                         width=width, height=height),
                      viewport=width)
        seen = browser.in_frame(
            """
            var els = root.querySelectorAll(arguments[0]);
            var bad = [];
            for (var i = 0; i < els.length; i++) {
              var cs = win.getComputedStyle(els[i]);
              if (cs.borderTopWidth !== "0px" || cs.paddingTop !== "0px") {
                bad.push({i: i, bw: cs.borderTopWidth, pad: cs.paddingTop});
              }
            }
            return {n: els.length, bad: bad.slice(0, 3), badCount: bad.length};
            """,
            selector,
        )
        assert seen["n"] > 0, f"{tree}: no distribution segments on the axes slide"
        assert seen["badCount"] == 0, (
            f"{tree}: {seen['badCount']} of {seen['n']} distribution segments carry a "
            f"borrowed border or padding: {seen['bad']}")
    browser.resize(1280, 900)
    print("PASS test_the_distribution_segments_kept_their_flat_band")


def test_the_axis_bands_are_separated_and_the_detail_stays_on_demand(
        browser: Browser, server: ContractServer) -> None:
    """Measured, not inspected.

    The separators must be real pixels and consecutive bands of one status must
    actually paint differently — that is what shows where the thresholds are.
    The NUMBERS stay on demand: a resting range row was tried and removed, so
    both tip rows must rest invisible at every width and appear only through
    hover or `.show`.
    """
    browser.resize(1280, 900)
    browser.mount(server.harness_url(fixture="monthly_31_days", route="#monthly/2",
                                     width=1280, height=900), viewport=1280)
    seen = browser.in_frame(
        """
        var rows = root.querySelectorAll('.ed-axis-segs');
        var out = [];
        for (var i = 0; i < rows.length; i++) {
          var segs = rows[i].querySelectorAll('.ed-seg');
          if (segs.length < 2) continue;
          var gaps = [], colours = [], widths = [];
          for (var j = 0; j < segs.length; j++) {
            var b = segs[j].getBoundingClientRect();
            widths.push(Math.round(b.width * 10) / 10);
            colours.push(win.getComputedStyle(segs[j]).backgroundColor);
            if (j) gaps.push(Math.round((b.left - segs[j-1].getBoundingClientRect().right) * 10) / 10);
          }
          var axis = rows[i].parentElement;
          var top = axis.querySelector('.ed-tips.top'), bot = axis.querySelector('.ed-tips.bot');
          out.push({n: segs.length, gaps: gaps, colours: colours, widths: widths,
                    radiusFirst: win.getComputedStyle(segs[0]).borderTopLeftRadius,
                    radiusLast: win.getComputedStyle(segs[segs.length-1]).borderTopRightRadius,
                    labelOpacity: top ? win.getComputedStyle(top).opacity : null,
                    pointsOpacity: bot ? win.getComputedStyle(bot).opacity : null});
        }
        return out;
        """
    )
    assert seen, "no multi-band axis on the axes slide"
    widest = max(seen, key=lambda row: row["n"])
    assert widest["n"] >= 5, f"expected an axis with at least 5 bands, got {widest['n']}"

    for row in seen:
        assert set(row["gaps"]) == {2.0}, f"separators are not a uniform 2 px: {row['gaps']}"
        assert max(row["widths"]) - min(row["widths"]) <= 1.0, (
            f"bands are not equal width: {row['widths']}")
        assert row["radiusFirst"] == "999px" and row["radiusLast"] == "999px", (
            f"the outer ends are not pill-rounded: {row['radiusFirst']}/{row['radiusLast']}")
        # Consecutive bands never paint the same colour twice in a row.
        for a, b in zip(row["colours"], row["colours"][1:]):
            assert a != b, f"two adjacent bands paint identically ({a}); the run still blends"
        # BOTH rows stay hidden until asked for. The tone steps and the 2 px
        # separators carry the thresholds at rest; the numbers are the reveal,
        # which is exactly what the legend's "najedź lub dotknij oś" promises.
        assert row["labelOpacity"] == "0", (
            f"the range row rests visible ({row['labelOpacity']}) — it was removed on purpose")
        assert row["pointsOpacity"] == "0", (
            f"the points row rests visible ({row['pointsOpacity']}), so the legend now lies")

    # No horizontal overflow at any supported width, and the row grew ≤ 14 px.
    for width, height in ((320, 720), (760, 900), (1280, 900)):
        browser.resize(width, height)
        browser.mount(server.harness_url(fixture="monthly_31_days", route="#monthly/2",
                                         width=width, height=height), viewport=width)
        flow = browser.overflow()
        assert flow["scrollWidth"] <= flow["clientWidth"] + 1, (
            f"{width}px: the document pans sideways (widest {flow['offender']})")
        assert [pan for pan in flow["panners"] if not pan["approved"]] == [], width
    browser.resize(1280, 900)
    # The stylesheet must not reveal either row anywhere except on demand.
    css = without_comments("css/dashboard.css")
    reveals = re.findall(r"([^{}]*\.ed-tips[^{}]*)\{([^}]*opacity:1[^}]*)\}", css)
    assert reveals, "nothing reveals the tip rows any more"
    for selector, _ in reveals:
        assert ":hover" in selector or ".show" in selector, (
            f"`{selector.strip()}` makes a tip row visible outside hover/.show")
    assert "@media(hover:hover){.ed-axis-row:hover .ed-tips{opacity:1}}" in css, \
        "the pointer reveal is gone"
    assert ".ed-axis-row.show .ed-tips{opacity:1}" in css, "the tap/keyboard reveal is gone"

    print(f"PASS test_the_axis_bands_are_separated_and_the_detail_stays_on_demand "
          f"({len(seen)} axes, widest {widest['n']} bands, {len(reveals)} reveal rules)")


def test_the_page_makes_no_external_request(browser: Browser, server: ContractServer) -> None:
    """One application-owned fetch, and nothing else — the privacy posture."""
    browser.mount(server.harness_url(fixture="ranked_acceptable", route=f"#weekly/{SLIDE_SCORE}"))
    origin = server.origin
    external = [url for url in browser.resources()
                if not url.startswith(origin) and not url.startswith("data:")]
    assert external == [], f"the page fetched something external: {external}"
    print("PASS test_the_page_makes_no_external_request")


# ------------------------------------------------------------------ main ---


BROWSER_TESTS = (
    test_every_state_renders_through_the_frozen_boundary,
    test_rendered_values_come_from_the_snapshot,
    test_counts_and_coefficients_stay_separately_available,
    test_the_mobile_daily_list_replaced_the_pan_track,
    test_the_presentation_boundary_is_the_approved_width,
    test_the_mobile_tabs_and_accordions_are_honest,
    test_slide_navigation_is_routable,
    test_the_capability_is_taken_before_the_design_routes,
    test_fail_closed_states_expose_no_eco_payload,
    test_ranking_absence_is_valid_and_never_invented,
    test_sparse_rating_distribution_renders_only_present_buckets,
    test_the_distance_gate_is_period_level_only,
    test_absent_comparison_is_not_rendered_as_zero_change,
    test_period_distance_carries_its_movement,
    test_keyboard_and_accessibility_contract,
    test_the_trend_thumb_lands_on_the_cell_it_claims,
    test_the_distribution_segments_kept_their_flat_band,
    test_the_axis_bands_are_separated_and_the_detail_stays_on_demand,
    test_the_page_makes_no_external_request,
)


def main() -> None:
    test_production_package_shape_is_production_compatible()
    test_the_monthly_trend_card_offers_two_tabs()
    test_a_month_whose_reference_is_missing_falls_back_to_its_comparison()
    test_a_month_without_a_previous_month_opens_on_progress()
    test_a_weekly_trend_card_has_no_tabs()
    test_the_rejected_previous_month_note_is_gone_everywhere()
    test_the_previous_marker_is_named_for_the_period_it_compares()
    test_a_monthly_report_names_the_month_wherever_it_names_the_previous_period()
    test_the_comparison_wording_follows_the_document_not_the_comparison()
    test_the_trend_switch_is_one_sliding_thumb()
    test_the_trend_switch_does_not_borrow_a_distribution_segment_class()
    test_the_band_ramp_is_one_table()
    test_consecutive_bands_of_one_status_are_distinguishable()
    test_the_slide_tablist_and_the_trend_tablist_do_not_collide()
    test_the_shipped_presentation_is_the_approved_design_export()
    test_secure_bootstrap_stays_owned_by_the_repository()
    test_no_business_derivation_happens_in_the_browser()
    test_fixtures_match_the_generator()
    test_frozen_frontend_api_is_exposed()
    test_the_route_vocabulary_is_the_shipped_one()
    test_boot_takes_its_snapshot_only_from_the_supplied_source()
    test_access_states_render_without_a_snapshot()
    test_snapshot_input_vocabulary_is_stable()

    with ContractServer(SYNTHETIC_DOCUMENTS) as server, Browser() as browser:
        for test in BROWSER_TESTS:
            test(browser, server)

    #: Its own browser: the preference has to be set before Firefox starts.
    test_reduced_motion_shows_the_final_values_immediately()

    print("Driver Eco Dashboard frontend contract tests passed")


if __name__ == "__main__":
    main()
