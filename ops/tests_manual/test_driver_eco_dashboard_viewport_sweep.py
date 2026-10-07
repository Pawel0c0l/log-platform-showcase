#!/usr/bin/env python3
"""Driver Eco Dashboard — fixture × viewport sweep.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_viewport_sweep.py

This is the layout half of the design integration contract. It says nothing
about what the dashboard looks like — no screenshots, no golden files, no class
names — and everything about whether it *works* at the widths a driver actually
opens it on:

    * the page boots through `window.EcoApp.boot({source})` with no uncaught
      exception and no console error;
    * the DOCUMENT never scrolls sideways, and no ordinary element escapes the
      viewport;
    * below the approved 760 px boundary NOTHING pans sideways at all. The
      mobile redesign replaced the panning daily table with a vertical list of
      day cards, so the one surface that used to be allowed to pan no longer
      exists there. At and above the boundary the desktop page keeps its
      exception: `.ed-scrollx` may scroll inside its own box — and nothing else
      may — while its box stays inside the viewport and its content stays
      reachable;
    * the state that was supposed to render did render;
    * a state that must expose no Eco payload still exposes none at 320 px;
    * the page stays operable: one heading, tab navigation, a focusable control.

The state set is the smallest one that covers materially different layouts: a
ranked driver with a sparse rating distribution, a dangerous rating, a driver
with no ranking, a period with no comparison basis, a 31-day month, and the two
fail-closed states. The sparse distribution is built here rather than added to
`assets/fixtures/`, which must stay byte-identical to production.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

from eco_dashboard_browser import (  # noqa: E402
    APPROVED_PAN_CLASS, Browser, ContractServer, contains_number, load_fixture,
    normalise, readable,
)

#: 320 is the narrowest width the product supports; 1440 the reference desktop.
#: 768x1024 and 1920x1080 are the two TALL viewports (>=951px of height), so
#: they are what exercises the tall-viewport composition; 1366x768 and
#: 1600x900 sit in the compact tiers. The pair matters: the tall tier is
#: keyed on height, and keying it on height ALONE once pushed slide 1 to
#: 790px inside the 768px portrait tablet.
VIEWPORTS = ((320, 900), (390, 844), (768, 1024), (1280, 900), (1366, 768),
             (1440, 900), (1600, 900), (1920, 1080))

#: A pixel of rounding tolerance, and nothing more: a scrollbar-free headless
#: viewport reports integral widths.
OVERFLOW_TOLERANCE = 1

FORBIDDEN_MARKERS = ("undefined", "NaN", "[object Object]", "EXCLUDED", "UNKNOWN_DRIVER")

#: `null` and `Infinity` need word boundaries rather than a substring test:
#: Polish prose legitimately contains those letter sequences, and a false
#: positive at one viewport would block a correct layout for no reason.
FORBIDDEN_WORDS = (re.compile(r"\bnull\b", re.I), re.compile(r"\bInfinity\b"))


def sparse_ranked_document() -> dict:
    document = load_fixture("ranked_acceptable")
    document["periods"]["weekly"]["current"]["rating_group_distribution"] = {
        "safe": 49.37, "acceptable": 50.63,
    }
    return document


SYNTHETIC = {"weekly_ranked_sparse": sparse_ranked_document()}

#: The dashboard is three sections, routed as `#<period>/<1|2|3>` in both
#: approved presentations. A fail-closed period has no sections at all — it
#: renders one full-screen state panel, with no tab bar — so it is swept at the
#: single route it can occupy.
SLIDES = ("1", "2", "3")

#: The approved responsive boundary. Below it the mobile tab page renders and
#: no element may pan; at and above it the desktop slide page renders.
DESKTOP_MIN_WIDTH = 760

#: (label, fixture | None, synthetic | None, period, slides, expectation)
STATES = (
    ("weekly ranked, sparse distribution", None, "weekly_ranked_sparse", "weekly",
     SLIDES, "eco"),
    ("weekly ranked, dangerous", "ranked_dangerous", None, "weekly",
     SLIDES, "eco"),
    ("weekly, not ranked by configuration", "not_ranked_by_configuration", None, "weekly",
     SLIDES, "eco"),
    ("weekly, no comparison basis", "no_comparison", None, "weekly",
     SLIDES, "eco"),
    ("monthly, 31 days", "monthly_31_days", None, "monthly",
     SLIDES, "eco"),
    ("insufficient period distance", "insufficient_period_distance", None, "weekly",
     ("1",), "fail_closed"),
    ("report not ready", "report_not_ready", None, "weekly",
     ("1",), "fail_closed"),
)


def expected_values(fixture: str | None, synthetic: str | None, period: str) -> tuple[int, ...]:
    document = SYNTHETIC[synthetic] if synthetic else load_fixture(fixture)
    block = document["periods"][period]["current"]
    if block is None:
        return ()
    return (block["eco_score_total"], block["total_kilometers"])


def sweep(browser: Browser, server: ContractServer) -> None:
    checked = 0
    panned = 0
    for label, fixture, synthetic, period, slides, expectation in STATES:
        values = expected_values(fixture, synthetic, period)
        for slide in slides:
            for width, height in VIEWPORTS:
                settle = browser.mount(
                    server.harness_url(fixture=fixture, document=synthetic,
                                       route=f"#{period}/{slide}", width=width, height=height),
                    viewport=width,
                )
                where = f"{label} · slide {slide} · {width}px"
                assert browser.viewport() == width, (
                    f"{where}: the layout viewport is {browser.viewport()}px, not {width}px"
                )

                # 1. it boots, and nothing is broken.
                errors = browser.errors()
                assert errors == [], f"{where}: {errors}"
                assert settle < 5.0, f"{where}: took {settle:.1f}s to settle"

                markup = browser.markup()
                assert markup.strip(), f"{where}: rendered nothing"
                text = normalise(browser.text())
                content = readable(markup) + " \n " + text
                for marker in FORBIDDEN_MARKERS:
                    assert marker not in content, f"{where}: rendered {marker!r}"
                for word in FORBIDDEN_WORDS:
                    found = word.search(content)
                    assert not found, f"{where}: rendered {found.group()!r}"

                # 2. the intended state is on screen. The score and the period
                # distance are slide 1's headline numbers; the later slides
                # carry their own content and are checked for cleanliness.
                if expectation == "eco":
                    if slide == "1":
                        for value in values:
                            assert contains_number(content, value), \
                                f"{where}: the snapshot value {value} never rendered"
                    assert browser.hash() == f"#{period}/{slide}", \
                        f"{where}: the route became {browser.hash()}"
                else:
                    assert not re.search(r"\d+(?:[.,]\d+)?\s?%", text), \
                        f"{where}: a fail-closed period rendered a percentage"
                    assert not re.search(r"\d+\s?pkt", text), \
                        f"{where}: a fail-closed period rendered points"
                    assert "na 100 km" not in text, f"{where}: a fail-closed period rendered a rate"

                # 3. the DOCUMENT does not scroll sideways, no ordinary element
                # escapes the viewport, and the only element that pans is the
                # approved analytical track — bounded, and still reachable.
                geometry = browser.overflow()
                viewport_width = geometry["clientWidth"]
                assert geometry["scrollWidth"] <= viewport_width + OVERFLOW_TOLERANCE, (
                    f"{where}: document horizontal overflow "
                    f"({geometry['scrollWidth']} > {viewport_width}), "
                    f"widest element {geometry['offender']}"
                )
                assert geometry["bodyScrollWidth"] <= viewport_width + OVERFLOW_TOLERANCE, (
                    f"{where}: <body> horizontal overflow "
                    f"({geometry['bodyScrollWidth']} > {viewport_width})"
                )
                assert geometry["widestRight"] <= viewport_width + OVERFLOW_TOLERANCE, (
                    f"{where}: {geometry['offender']} reaches {geometry['widestRight']}px "
                    f"in a {viewport_width}px viewport"
                )
                if width < DESKTOP_MIN_WIDTH:
                    assert geometry["panners"] == [], (
                        f"{where}: the approved mobile page has no panning "
                        f"surface, yet {geometry['panners']} scrolls sideways"
                    )
                for pan in geometry["panners"]:
                    assert pan["approved"], (
                        f"{where}: {pan['selector']} scrolls horizontally; only "
                        f".{APPROVED_PAN_CLASS} may"
                    )
                    assert pan["left"] >= -OVERFLOW_TOLERANCE and \
                        pan["right"] <= viewport_width + OVERFLOW_TOLERANCE, (
                        f"{where}: the pan track itself escapes the viewport "
                        f"({pan['left']}…{pan['right']} in {viewport_width}px)"
                    )
                    assert pan["focusables"] >= 1 or pan["tabbable"], (
                        f"{where}: the panned content is unreachable from the keyboard"
                    )
                    panned += 1

                # 4. it stays operable.
                operable = browser.in_frame(
                    """
                    var focusable = root.querySelectorAll(
                      'button:not([disabled]), a[href], [role="tab"]:not([disabled])');
                    return {
                      headings: root.querySelectorAll('h1').length,
                      focusable: focusable.length,
                      onclick: root.querySelectorAll('[onclick]').length
                    };
                    """
                )
                assert operable["headings"] == 1, f"{where}: {operable['headings']} <h1> elements"
                assert operable["onclick"] == 0, f"{where}: an inline onclick handler"
                if expectation == "eco":
                    assert operable["focusable"] >= 1, f"{where}: nothing is operable"
                checked += 1
    print(f"PASS sweep: {checked} state × slide × viewport combinations, "
          f"{panned} approved .{APPROVED_PAN_CLASS} pan surfaces "
          f"(none below {DESKTOP_MIN_WIDTH}px, by design), no other overflow")


def main() -> None:
    # The window only has to be big enough to hold the widest frame.
    with ContractServer(SYNTHETIC) as server, Browser(width=1600, height=1000) as browser:
        sweep(browser, server)
    print("Driver Eco Dashboard viewport sweep passed")


if __name__ == "__main__":
    main()
