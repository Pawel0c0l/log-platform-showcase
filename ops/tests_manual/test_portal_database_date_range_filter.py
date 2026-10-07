#!/usr/bin/env python3
"""Column filter dismissal, typed dates and the calendar range (UI-20260831-01).

Three user-reported defects and their fixes:

1. **Dismissal.** Using a column filter dismissed it. The outside-click handler
   passed the menu ELEMENT to `closeAllMenus`, which compares against the menu
   RECORD, so the "except" never matched and every click — including a click on
   the operator select inside the open menu — closed everything. The filter panel
   separately had no outside-click dismissal at all.

2. **Typed dates.** The native date control accepts typing only in the browser's
   own locale shape, unhinted and with no visible rejection.

3. **Calendar range.** A native picker has no concept of a range and cannot mark
   the days between two dates.

The invariant this suite guards hardest is that none of it changed the request:
the filter parameters, their names and their values are what the native inputs
alone produced, because the added controls carry no `name` and every added
button is `type="button"`.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_date_range_filter.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _identity_default(default=None, *args, **kwargs):
    return default


def _install_import_stubs() -> None:
    """Same shim the sibling filtering suite uses: `api.main` is imported for its
    pure HTML helpers, not for a running application, so its heavy third-party
    imports are stubbed rather than installed."""
    fastapi = types.ModuleType("fastapi")
    for name in ("Header", "File", "Form", "Query", "Body"):
        setattr(fastapi, name, _identity_default)
    # No `mount`: `api.main` skips the static-file mount when the app object
    # cannot mount, which is how the offline suites avoid pulling in starlette.
    class _App:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda fn: fn

        post = patch = delete = on_event = get

    fastapi.FastAPI = _App
    fastapi.HTTPException = type("_HTTPException", (Exception,), {})
    fastapi.Request = object
    fastapi.UploadFile = object
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = type("_HTMLResponse", (), {"__init__": lambda self, *a, **k: None})
    responses.StreamingResponse = type("_StreamingResponse", (), {"__init__": lambda self, *a, **k: None})

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: None

    psycopg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", rows)


_install_import_stubs()

import api.main as api_main  # noqa: E402


def _run(harness: str, scenario: str) -> dict:
    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / harness), scenario]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _date(scenario: str) -> dict:
    return _run("data_grid_daterange_harness.js", scenario)


def _filters(scenario: str) -> dict:
    return _run("data_grid_filters_harness.js", scenario)


# ===========================================================================
# 1. A1/A2 — the panel stays open while it is being used
# ===========================================================================
def test_using_a_column_menu_does_not_dismiss_it() -> None:
    result = _filters("menu-survives-use")
    assert result["beforeClick"]["open"] is True, result
    # The reported defect: one click on the operator closed the whole menu.
    assert result["afterClickOnOperator"]["open"] is True, (
        "a click inside an open menu must not dismiss it"
    )
    # A1 explicitly asks for several refinements in one visit.
    assert result["afterSecondSelection"]["open"] is True, result
    assert result["afterSecondSelection"]["operator"] == "blank", result
    # A2: a genuine outside click still dismisses.
    assert result["afterOutside"]["open"] is False, result
    print("PASS: a column menu survives being used and still closes on an outside click")


# ---------------------------------------------------------------------------
# Programmatic focus must not dismiss what it serves (UI-20260901-02)
# ---------------------------------------------------------------------------
def test_the_panel_is_made_to_fit_the_viewport() -> None:
    """L1/D1 — the fix that the previous three rounds were working around.

    `max-height: 60vh` measures against the viewport, but the panel starts at the
    bottom of a sticky header. Measured live at 1280x340: the panel opened at
    y=231, 60vh gave it 204, and its box ended at 435 — 95px below the fold.

    That band is unreachable by ANY scrollbar. The panel is absolutely positioned
    under a sticky header, so it does not move when the grid scrolls; and its own
    scrollbar can only move content within a box that is itself partly
    off-screen. No scroll logic can fix a box that does not fit, which is why
    restoring the grid and letting the grid scroll both failed, in opposite
    directions.

    The available space is (viewport - panel top - margin), which no `vh` value
    can express because none of them know the offset.
    """
    result = _filters("panel-fits-the-viewport")
    short = result["short"]
    assert short["viewport"] == 340, short
    # 340 - 231 - 8 == 101, and the panel now ends above the fold.
    assert short["inlineMaxHeight"] == "101px", short
    assert short["panelBottom"] == 332, short
    assert short["fitsViewport"] is True, "the panel must be entirely on screen"

    # L6: where the stylesheet already fits, nothing is touched — this only ever
    # makes the panel shorter, never taller.
    assert result["tall"]["untouched"] is True, result
    print("PASS: the panel is bounded by the space below its own top edge, and only when needed")


def test_the_panel_reveal_never_overshoots() -> None:
    """L7/D3 — the panel at its exact scroll maximum with the target off the TOP.

    An unclamped request to scroll past the end is silently truncated by the
    engine, and the element can end up pushed out of the opposite edge: the panel
    sat at 545 of a 545 maximum with the field measured at -132.
    """
    result = _filters("reveal-never-overshoots")
    assert result["before"]["scrollTop"] == 545, result
    assert result["before"]["targetTop"] < 0, "the target starts above the panel"

    after = result["after"]
    assert after["withinPanel"] is True, "the reveal must land it in view"
    assert after["withinRange"] is True, "and inside the scrollbox's real range"
    assert 0 <= after["scrollTop"] <= 545, after
    print("PASS: the reveal brings the element into view without scrolling past it")


def test_a_caret_reveal_on_every_keystroke_does_not_dismiss() -> None:
    """K2/K5 — the defect, on the measured ordering.

    Once the caret sits below the fold the browser reveals it again on EVERY
    `input`: one grid scroll per keystroke, not a one-time event. That is why no
    one-shot could cover it — the previous attempt armed on focus, spent itself
    on the first reveal, and the first digit killed the menu.
    """
    result = _filters("caret-reveal-per-keystroke")
    steps = result["perKeystroke"]
    assert len(steps) == 8, steps
    assert result["survivedAllEight"] is True, steps
    # The grid does move — the reveal is allowed to do its job, which is what
    # keeps the field visible. It simply is not read as a scroll of the table.
    assert steps[0]["gridTop"] == 107 and steps[-1]["gridTop"] == 252, steps
    # K4: a wheel over the table is aimed at the grid, and still dismisses.
    assert result["afterWheelOverGrid"]["menuOpen"] is False, result
    print("PASS: eight keystrokes, eight caret reveals, menu open throughout")


def test_dismissal_follows_what_the_gesture_was_aimed_at() -> None:
    """K4 — the discriminator, in both directions.

    Not focus-within: the open handler puts focus inside the panel immediately,
    so that would exempt everything and delete §3. What separates a scroll the
    user performed from collateral of what they are doing in the panel is the
    TARGET of the gesture behind it.
    """
    result = _filters("gesture-target-decides-dismissal")
    # Aimed at the grid — §3 applies.
    assert result["arrowsOnGridCell"]["menuOpen"] is False, result
    assert result["wheelOverGrid"]["menuOpen"] is False, result
    # The same key, aimed into a panel field — collateral, never a dismissal.
    assert result["arrowsInPanelField"]["menuOpen"] is True, result
    # Anything unexplained keeps §3's behaviour rather than quietly losing it.
    assert result["unexplainedScroll"]["menuOpen"] is False, result
    print("PASS: dismissal follows the gesture's target, not the focus location")


def test_the_reveal_scrolls_the_panel_and_not_the_box_that_merely_overflows() -> None:
    """H1/H5 — the half of the guard that was never actually tested.

    `scrollHeight > clientHeight` is not "this element scrolls": it is true of
    anything whose content overflows, `overflow: visible` included. Walking up
    from a calendar day, the first such ancestor is `.db-date-calendar` — taller
    than its box, not scrollable — so the walk stopped there. And because that
    wrongly-chosen box is LARGER than the day, the visibility test concluded
    "already visible" and did nothing at all: no error, no scroll, no reveal.

    The geometry here is the Viewer's live measurement on a 460px viewport: the
    panel scrollbox ends at 518 and the focused day's bottom sat at 673.
    """
    result = _filters("panel-reveals-focused-element")
    before = result["before"]
    assert before["dayVisible"] is False, before
    assert before["dayBottom"] == 673 and before["panelBottom"] == 518, before
    assert before["panelScrollTop"] == 0, before

    after = result["after"]
    # The panel scrolls by exactly the overshoot, and the day comes into view.
    assert after["panelScrollTop"] == 155, after
    assert after["dayVisible"] is True, after
    # The overflowing-but-not-scrollable ancestor is left alone.
    assert after["calendarScrollTop"] == 0, after
    # H2: the grid is never the thing that scrolls, and the menu survives.
    assert after["gridScrollTop"] == 96 and after["gridScrollLeft"] == 420, after
    assert after["menuOpen"] is True, after
    print("PASS: the reveal scrolls the panel scrollbox, never a box that merely overflows")


def test_the_two_modules_always_ship_together() -> None:
    """T3 — the standalone question, settled by the asset manifest.

    The reveal has ONE implementation, in `data-grid-filters.js`, because that
    module owns the dismissal rule it exists to avoid. `data-grid-daterange.js`
    delegates to it and, loaded alone, degrades to declining the scroll without
    revealing — which is a real degradation and therefore asserted not to be a
    shipped configuration: both modules are page assets, and the filters module
    is listed first so its guard is published before the date module runs.
    """
    from api.portal_ui import assets as portal_assets  # noqa: PLC0415

    page_assets = list(portal_assets.PAGE_ASSETS)
    assert "js/data-grid-filters.js" in page_assets, page_assets
    assert "js/data-grid-daterange.js" in page_assets, page_assets
    assert page_assets.index("js/data-grid-filters.js") < page_assets.index(
        "js/data-grid-daterange.js"
    ), "the guard must be published before the module that delegates to it"
    print("PASS: both modules ship together, filters first, so the reveal is always present")


def test_focusing_a_control_on_open_does_not_dismiss_the_menu() -> None:
    """G1/G2 — the reachable case, reproduced before it was fixed.

    A bare `focus()` on an element out of view scrolls its nearest scrollable
    ancestors to reveal it. One of those is the grid, and the grid's scroll is
    what INTERACTION_SPEC §3 reads as "the user scrolled away". Opening a column
    menu near the bottom of the viewport was therefore enough to close it: the
    menu focused its first control, the browser scrolled, and the menu died on
    the gesture that opened it.
    """
    result = _filters("focus-on-open-must-not-dismiss")
    after = result["afterOpen"]
    # D2: the reveal is declined again. It was allowed for one round, on the
    # theory that only the browser could bring a below-fold control into view —
    # but the panel is anchored under a sticky header and does not move with the
    # grid, so the reveal scrolled the table ~150px per keystroke and revealed
    # nothing. With the panel made to fit, its own scrollbar reaches everything.
    assert after["browserScrolled"] is False, after
    assert after["open"] is True, "the menu must survive focusing its own first control"
    # G3: a genuine user scroll still dismisses — §3 is untouched.
    assert result["afterUserScroll"]["open"] is False, result
    print("PASS: focusing a control on open no longer scrolls the grid, and user scroll still dismisses")


def test_focus_returned_on_dismissal_does_not_scroll_the_grid() -> None:
    """The focus-on-close sites: focus still lands on the trigger, without a
    reveal-scroll that would disturb the grid behind it."""
    result = _filters("focus-return-on-dismissal")
    after = result["afterEscape"]
    assert after["focused"] == "summary", after
    print("PASS: focus returned on dismissal no longer scrolls the grid")


def test_every_calendar_focus_goes_through_the_shared_guard() -> None:
    """G1 for the calendar, the most exposed site: every arrow key and every
    activation moves focus to another day inside a `max-height: 60vh` panel."""
    result = _date("calendar-focus-is-guarded")
    assert result["focusCalls"] == 4, result
    assert result["delegatedToSharedGuard"] == 4, (
        "the calendar must use data-grid-filters.js's guard, which owns the "
        "dismissal rule it has to avoid"
    )
    # Loaded without that module the calendar still focuses; only the
    # panel-internal reveal is missing.
    assert result["standalone"]["focusCalls"] == 1, result
    print("PASS: every calendar focus goes through the shared guard, and degrades safely without it")


def test_focus_returned_to_a_rejected_field_is_guarded_on_both_paths() -> None:
    """G1 for the two rejected-field sites: Enter and Apply."""
    result = _date("invalid-field-focus-is-guarded")
    for key in ("afterEnter", "afterSubmit"):
        assert result[key]["focused"] is True, (key, result)
    assert result["afterEnter"]["invalid"] is True, result
    assert result["afterSubmit"]["submissions"] == 0, "an invalid value must still block the apply"
    print("PASS: focus returned to a rejected field is guarded on both the Enter and Apply paths")


def test_a_control_that_rerenders_itself_does_not_dismiss_its_menu() -> None:
    """The calendar rebuilds its grid inside its own day-click handler, so by the
    time the document-level classifier runs the event target is detached. Walking
    that node's parents reaches nothing and the click used to be misread as
    "outside", dismissing the menu the user was working in.

    Fixed in the shared classifier rather than in the calendar, because any
    future control that rebuilds itself on click would otherwise hit it too.
    """
    result = _filters("rerendering-control-stays-open")
    assert result["beforeDayClick"]["open"] is True, result
    # The reproduction is only meaningful if the target really was detached.
    assert result["targetWasDetached"] is True, result
    assert result["afterDayClick"]["open"] is True, result
    # A keyboard activation produces a click with no pointer event before it, so
    # the provenance must come from the keydown and not from a stale gesture.
    assert result["afterKeyboardDayActivation"]["open"] is True, result
    # And a genuine outside click still dismisses.
    assert result["afterOutsideClick"]["open"] is False, result
    print("PASS: a self-rerendering control keeps its menu open, by pointer and by keyboard")


def test_a_control_that_rerenders_itself_does_not_collapse_the_panel() -> None:
    result = _filters("rerendering-control-keeps-panel")
    assert result["afterRerenderClick"]["open"] is True, result
    assert result["afterOutsideClick"]["open"] is False, result
    print("PASS: a self-rerendering control inside the panel does not collapse it")


def test_the_filter_panel_dismisses_on_an_outside_click_only() -> None:
    result = _filters("panel-outside-click")
    assert result["afterInsideClick"]["open"] is True, (
        "a click inside the panel must not collapse it"
    )
    assert result["afterOutsideClick"]["open"] is False, result
    # Collapsing is not applying, and staged edits survive it (DB-14 unchanged).
    assert result["afterOutsideClick"]["operator"] == "blank", result
    assert result["afterOutsideClick"]["submitted"] is False, result
    print("PASS: the filter panel collapses on an outside click and keeps its staged edits")


def test_the_surface_in_use_is_reopened_after_an_apply_navigates() -> None:
    result = _filters("surface-memory")
    remembered = result["remembered"] or ""
    assert remembered.endswith("menu:driver_name"), result
    # Path-scoped, so returning to a different dataset does not reopen a menu.
    assert remembered.startswith("/user/database/datasets/trips"), result
    assert result["openBeforeRestore"] is False, result
    assert result["openAfterRestore"] is True, result
    # Consumed once: a later unrelated load must not reopen anything.
    assert result["memoryConsumed"] is True, result
    print("PASS: the surface in use is reopened after an apply, and the memory is consumed once")


def test_the_apply_carries_the_grid_scroll_and_the_restore_does_not_self_dismiss() -> None:
    result = _filters("restore-keeps-scroll-and-menu")
    # The offsets travel under the key data-grid-row-detail.js already restores
    # on every load, so there is one writer and one reader per navigation.
    assert json.loads(result["scrollRemembered"]) == {"left": 420, "top": 96}, result
    assert result["reopened"] is True, result
    # The restored scroll is the browser catching up, not the user scrolling
    # away: it must not close the menu the restore had just reopened.
    assert result["openAfterRestoredScroll"] is True, result
    # And an ordinary scroll, after the user has actually touched the page,
    # still dismisses — INTERACTION_SPEC.md §3 is unchanged.
    assert result["openAfterUserScroll"] is False, result
    print("PASS: an apply preserves the grid scroll, and the restored scroll does not dismiss the menu")


def test_the_module_still_works_without_session_storage() -> None:
    result = _filters("surface-memory-absent")
    assert result["stillOpen"] is True, result
    assert result["dismissesNormally"] is True, result
    print("PASS: a blocked sessionStorage disables only the memory, not the filter behaviour")


# ===========================================================================
# 2. A3/A4 — typed dates
# ===========================================================================
ACCEPTED_FORMS = {
    "2026-08-31": "2026-08-31",
    "2026.08.31": "2026-08-31",
    "2026/08/31": "2026-08-31",
    "31.08.2026": "2026-08-31",
    "31-08-2026": "2026-08-31",
    "31/08/2026": "2026-08-31",
    "2026-08-31 14:30": "2026-08-31",
    "2026-08-31T14:30": "2026-08-31",
    "  2026-08-31  ": "2026-08-31",
}

# Deliberately rejected rather than guessed at. `03/04/26` is ambiguous and
# `2026-02-30` does not exist; neither may be silently coerced into a filter.
REJECTED_FORMS = (
    "2026-02-30", "2026-13-01", "31.02.2026", "not a date",
    "2026-08", "03/04/26", "2026-08-31 25:00", "2026-08-31 12:74",
)


# ---------------------------------------------------------------------------
# The input mask (UI-20260901-01)
# ---------------------------------------------------------------------------
def test_bare_digits_grow_their_own_separators_as_they_are_typed() -> None:
    """A1/A2/R1/R2 — `01082026` types itself into `01.08.2026`.

    Separators are LAZY: one appears only once the digit after it exists, so the
    field never shows a trailing `01.` waiting to be filled, and a Backspace can
    never be undone by the mask re-adding the separator just deleted.
    """
    result = _date("mask-typing")
    assert result["progressive"] == [
        "0", "01", "01.0", "01.08", "01.08.2", "01.08.20", "01.08.202", "01.08.2026",
    ], result["progressive"]
    assert result["afterDateOnly"] == {"shown": "01.08.2026 00:00",
                                       "wire": "2026-08-01T00:00"}, result
    # R2: keep going and the time separators insert themselves too.
    assert result["beforeCommit"] == "12.08.2026 14:30", result
    assert result["afterDateTime"] == {"shown": "12.08.2026 14:30",
                                       "wire": "2026-08-12T14:30"}, result
    print("PASS: bare digits format themselves into dd.mm.rrrr gg:mm as they are typed")


def test_the_mask_does_not_fight_a_user_who_types_their_own_separators() -> None:
    """A3/R3 — and keeps its hands off the other accepted formats entirely."""
    result = _date("mask-manual-separators")
    # No doubled dots, no scattered caret: the same value either way.
    assert result["whileTyping"] == "01.08.2026", result
    assert result["committed"]["wire"] == "2026-08-01T00:00", result
    # A value in a shape the mask would never emit is left for the parser, so
    # every form UI-20260831-01 documented still commits.
    assert result["isoCommitted"] == "2026-08-31T23:59", result
    print("PASS: manually typed separators and the other accepted formats survive the mask")


def test_backspace_removes_one_digit_and_steps_over_the_separators() -> None:
    """A4/R4 — the choice: a press removes a DIGIT wherever the caret sits.

    Deleting never stalls on a separator that the mask would immediately re-add,
    and the separator disappears with the digit it was introducing.
    """
    result = _date("mask-backspace")
    assert result["start"] == "01.08.2026", result
    assert [step["value"] for step in result["steps"]] == [
        "01.08.202", "01.08.20", "01.08.2", "01.08", "01.0",
    ], result["steps"]
    # The caret follows the digit it deleted rather than jumping to an end.
    assert [step["caret"] for step in result["steps"]] == [9, 8, 7, 5, 4], result["steps"]
    # A mid-value edit reformats positionally and leaves the caret where it was.
    assert result["afterMidDelete"] == {"value": "01.82.026", "caret": 2}, result
    print("PASS: Backspace deletes one digit through the separators, caret intact")


def test_pasted_digits_format_and_commit_and_an_impossible_date_is_flagged() -> None:
    """A5/A6/R5 — a paste arrives as one `input`, so it takes the same path."""
    result = _date("mask-paste-and-invalid")
    assert result["pasted"] == {"shown": "01.08.2026 00:00",
                                "wire": "2026-08-01T00:00"}, result
    assert result["pastedWithTime"] == {"shown": "12.08.2026 14:30",
                                        "wire": "2026-08-12T14:30"}, result
    # Masked for readability but NOT applied: the 32nd of the 13th is refused
    # exactly as `32.13.2026` was before the mask existed.
    assert result["invalid"]["shown"] == "32.13.2026", result
    assert result["invalid"]["invalid"] is True, result
    assert result["invalid"]["wireUnchanged"] == "2026-08-01T00:00", result
    print("PASS: pasted digits format and commit, and an impossible date is flagged not mangled")


def test_the_parser_accepts_bare_digits_even_if_the_mask_never_ran() -> None:
    """R6 — defence in depth, asserted against the parser itself."""
    result = _date("mask-parses-bare-digits")
    assert result["parsed"]["01082026"] == "2026-08-01", result
    assert result["parsed"]["31122026"] == "2026-12-31", result
    assert result["parsed"]["010820261430"] == "2026-08-01 14:30", result
    # A leading two-digit field is always a DAY, so these cannot be rescued as
    # some other ordering — they are impossible dates and are refused.
    for raw in ("32132026", "00002026", "01132026"):
        assert result["parsed"][raw] == "ERROR", (raw, result)
    print("PASS: the commit parser reads bare ddmmyyyy[HHMM] and refuses impossible ones")


def test_typed_dates_are_accepted_in_the_documented_forms() -> None:
    result = _date("typed-formats")
    for written, expected in ACCEPTED_FORMS.items():
        observed = result["accepted"][written]
        assert observed["invalid"] is False, (written, observed)
        assert observed["value"] == expected, (written, observed)
    print(f"PASS: {len(ACCEPTED_FORMS)} written date forms normalise into the native input")


def test_an_unparseable_typed_date_is_rejected_and_never_applied() -> None:
    result = _date("typed-formats")
    for written in REJECTED_FORMS:
        observed = result["rejected"][written]
        assert observed["invalid"] is True, (written, observed)
        # A4: never silently applied — the native input keeps its prior value.
        assert observed["value"] == "2026-01-01", (written, observed)
    print(f"PASS: {len(REJECTED_FORMS)} unparseable inputs are marked invalid and applied to nothing")


def test_a_rejected_typed_date_blocks_the_apply_rather_than_being_dropped() -> None:
    result = _date("typed-invalid-blocks-apply")
    after = result["afterInvalid"]
    assert after["nativeUnchanged"] == "2026-08-01", after
    assert after["invalidMarked"] is True, after
    assert after["ariaInvalid"] == "true", after
    assert after["statusText"], "the rejection must be stated, not only styled"
    # A4: not silently discarded either — the submit does not happen.
    assert result["blocked"] is True, result
    assert result["submissions"] == 0, result
    assert result["focusedInvalid"] is True, result
    # Correcting it lets exactly one ordinary request through.
    assert result["afterFix"]["blocked"] is False, result
    applied = result["afterFix"]["submissions"]
    assert len(applied) == 1, result
    # The range endpoints are what this asserts; the single-mode carrier rides
    # along empty because the operator is `range`.
    assert applied[0]["from"] == "2026-08-02", applied
    assert applied[0]["to"] == "2026-08-31", applied
    assert applied[0]["single"] == "", "a range filter must not carry a single value"
    print("PASS: an invalid typed date blocks the apply, is announced, and takes focus")


# ---------------------------------------------------------------------------
# The `hidden` attribute must actually hide (UI-20260831-02-A)
# ---------------------------------------------------------------------------
CSS_PATH = REPO_ROOT / "api" / "static" / "css" / "data-grid.css"


def _css_rules() -> list[tuple[str, str]]:
    """(selector, declarations) for every innermost rule in the grid stylesheet.

    Deliberately a tiny scanner rather than a CSS library: it only has to see
    which selectors set `display`, and an `@media` prelude simply rides along on
    the selector text of the first rule inside it, which does not affect the
    class names extracted from it.
    """
    import re  # noqa: PLC0415

    text = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(encoding="utf-8"), flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text)]


def _classes_in(selector: str) -> set[str]:
    import re  # noqa: PLC0415

    return set(re.findall(r"\.([A-Za-z0-9_-]+)", selector))


def test_the_hidden_attribute_beats_every_display_rule_that_could_override_it() -> None:
    """CA1/CA2/CA3 — the defect a DOM-only harness structurally cannot see.

    `[hidden] { display: none }` lives in the USER-AGENT origin, so any author
    `display` rule beats it however low its specificity. `.db-filter-value` and
    `.db-filter-range` did exactly that, so the operator's inactive value block
    kept its layout box and rendered a third date field on screen — while the
    harness, which has no layout engine, saw only the attribute and passed.

    The invariant asserted here is the general one, over the real stylesheet and
    the real rendered markup: any class that both carries an author `display`
    rule and appears on an element this app hides must have a `[hidden]`
    counterpart. A new `display` rule cannot reintroduce the bug silently.
    """
    import re  # noqa: PLC0415

    main = api_main
    column = {"column_name": "created_at", "data_type": "timestamp with time zone",
              "display_name": "Utworzono", "is_filterable": True}

    # Both modes, so both value blocks are seen in their hidden state.
    markup = [
        main._portal_database_filter_fieldset(column, entry=None, id_prefix="m-"),
        main._portal_database_filter_fieldset(
            column, entry={"operator": "older", "value": "2026-08-02T00:00"}, id_prefix="m-"),
    ]

    hidden_classes: set[str] = set()
    for html in markup:
        for tag in re.findall(r"<div[^>]*>", html):
            if not re.search(r"[\s\"']hidden(?=[\s>])", tag):
                continue
            found = re.search(r'class="([^"]*)"', tag)
            if found:
                hidden_classes.update(found.group(1).split())
    assert hidden_classes, "the fixture must actually contain hidden blocks"
    assert "db-filter-value" in hidden_classes, hidden_classes

    positive_display: set[str] = set()
    guarded: set[str] = set()
    for selector, decls in _css_rules():
        match = re.search(r"(?:^|;)\s*display\s*:\s*([a-z-]+)", decls)
        if not match:
            continue
        if match.group(1) == "none":
            for part in selector.split(","):
                if "[hidden]" in part:
                    guarded.update(_classes_in(part))
            continue
        for part in selector.split(","):
            if "[hidden]" not in part:
                positive_display.update(_classes_in(part))

    unguarded = sorted((hidden_classes & positive_display) - guarded)
    assert not unguarded, (
        "these classes set `display` and are used on elements hidden with the "
        f"attribute, but have no `[hidden]` counterpart: {unguarded}"
    )
    # And the two that caused this correction are positively covered.
    for name in ("db-filter-value", "db-filter-range"):
        assert name in guarded, (name, sorted(guarded))
    print(f"PASS: `hidden` wins over every display rule on the {len(hidden_classes)} hidden block classes")


def test_range_mode_shows_two_fields_and_no_calendar_until_focused() -> None:
    """UI-20260831-02 A1/R1/R4 — one field per endpoint, calendar on demand.

    The four-input arrangement (native pair + typed pair) is gone: the controls
    the server renders become hidden carriers of the wire value behind a single
    `dd.mm.rrrr gg:mm` field each.
    """
    result = _date("two-fields-and-collapsed-calendar")
    initial = result["initial"]
    assert initial["visibleFields"] == ["from", "to"], initial
    # R1: no visible native pair alongside — every carrier is hidden.
    assert initial["carrierTypes"] == ["hidden", "hidden", "hidden"], initial
    # R4: collapsed by default.
    assert initial["calendarVisible"] is False, initial
    assert initial["ariaExpanded"] == "false", initial
    # R3: the value is shown the way it is written here.
    assert initial["shown"] == ["01.08.2026 00:00", "02.08.2026 23:59"], initial
    # R6: and the wire is untouched underneath.
    assert initial["wire"] == {"from": "2026-08-01T00:00", "to": "2026-08-02T23:59"}, initial

    after = result["afterFocus"]
    assert after["calendarVisible"] is True, after
    assert after["ariaExpanded"] == "true", after
    assert after["dayButtons"] == 31, after
    print("PASS: range mode shows two fields, hides the carriers, and opens the calendar on focus")


def test_the_calendar_collapses_before_escape_reaches_the_menu() -> None:
    """R7 — the Escape decision.

    Escape collapses the calendar FIRST and consumes the key, so the column menu
    survives; a second Escape is not consumed here and reaches
    data-grid-filters.js, which closes the menu. That is the layered dismissal
    the rest of the grid already follows.
    """
    result = _date("calendar-collapse-contract")
    assert result["open"] is True, result
    first = result["afterFirstEscape"]
    assert first["calendarVisible"] is False, first
    assert first["consumed"] is True, "the first Escape must be consumed so the menu stays open"
    second = result["afterSecondEscape"]
    assert second["consumed"] is False, "the second Escape must fall through to the menu"
    # And the pointer path collapses too, after the click has landed.
    assert result["reopened"] is True, result
    assert result["afterOutsideClick"] is False, result
    print("PASS: Escape collapses the calendar first and the second press reaches the menu")


def test_single_mode_shows_one_field_and_writes_the_single_parameter() -> None:
    """R2/A5 — `przed`/`po` gets the same treatment as the range."""
    result = _date("single-mode")
    assert result["visibleFields"] == ["single"], result
    assert result["shown"] == "02.08.2026 00:00", result
    assert result["calendarVisible"] is False, result
    assert result["afterFocus"]["calendarVisible"] is True, result

    clicked = result["afterDayClick"]
    assert clicked["single"] == "2026-08-19T00:00", clicked
    # A single-mode pick must never write the range carriers.
    assert clicked["from"] == "" and clicked["to"] == "", clicked
    assert clicked["shown"] == "19.08.2026 00:00", clicked

    typed = result["afterTyping"]
    assert typed["single"] == "2026-09-07T08:45", typed
    assert typed["shown"] == "07.09.2026 08:45", typed
    assert result["submitted"]["single"] == "2026-09-07T08:45", result
    print("PASS: single mode shows one field, picks one date, and writes only date__")


def test_a_typed_date_survives_the_browser_change_then_blur_pair() -> None:
    """B1/B6 — the defect this replaced.

    A browser fires `input`, then `change`, then `blur` when you tab out of a
    text field. The first commit rendered the canonical value back into the
    field; the second read that rendered text and wrote it back, so a valid
    typed date was replaced by the value it was meant to replace — silently,
    with no rejection shown.
    """
    result = _date("typed-blur-real-order")
    assert result["beforeTyping"]["from"] == "2026-08-20T00:00", result
    assert result["afterBlur"]["nativeFrom"] == "2026-08-01T00:00", (
        "a typed date must reach the canonical input"
    )
    # UI-20260831-02: the field shows `dd.mm.rrrr gg:mm` on a column that
    # carries a time. The wire value above is unchanged.
    assert result["afterBlur"]["typedFrom"] == "01.08.2026 00:00", result
    assert result["afterBlur"]["invalid"] is False, result
    print("PASS: a typed date survives the change-then-blur pair a browser fires on tab-out")


def test_enter_in_a_typed_field_commits_and_a_later_blur_does_not_undo_it() -> None:
    """B3 — Enter used to set the canonical value and then have the following
    blur clear it again, which is how a later apply came to serialise empties."""
    result = _date("typed-enter-real-order")
    assert result["afterEnter"]["nativeTo"] == "2026-09-15T23:59", result
    assert result["afterEnter"]["typedTo"] == "15.09.2026 23:59", result
    assert result["afterBlur"]["nativeTo"] == "2026-09-15T23:59", (
        "a blur after Enter must not undo the committed value"
    )
    assert result["afterBlur"]["typedTo"] == "15.09.2026 23:59", result
    print("PASS: Enter commits the typed date and a following blur leaves it alone")


def test_what_the_inputs_show_is_what_an_apply_submits() -> None:
    """B5 — after any interleaving of typed edits and calendar clicks."""
    result = _date("typed-and-clicks-interleaved")
    # A typed end completes a range a click had opened.
    assert result["afterTypedEnd"] == {"from": "2026-08-10T00:00", "to": "2026-08-12T23:59"}, result
    # And the apply carries exactly what the inputs hold at that moment.
    submitted = result["submitted"]
    assert {"from": submitted["from"], "to": submitted["to"]} == result["shown"], result
    assert result["shown"] == {"from": "2026-08-25T00:00", "to": "2026-08-28T23:59"}, result
    print("PASS: after interleaved typing and clicking, the apply submits exactly what is shown")


def test_the_text_fields_and_the_calendar_never_disagree() -> None:
    result = _date("typed-sync")
    # Typing drives the calendar.
    typed = result["afterTyping"]
    assert typed["from"] == "2026-08-04" and typed["to"] == "2026-08-08", typed
    assert typed["start"] == ["2026-08-04"], typed
    assert typed["end"] == ["2026-08-08"], typed
    # Clicking drives the text.
    clicked = result["afterClicking"]
    assert clicked["shape"]["from"] == "2026-08-12", clicked
    assert clicked["shape"]["to"] == "2026-08-15", clicked
    # A date-only column shows `dd.mm.rrrr` with no time part.
    assert clicked["typed"] == ["12.08.2026", "15.08.2026"], clicked
    print("PASS: typing updates the calendar and the calendar updates the text")


# ===========================================================================
# 3. A5/A6 — the two-click range
# ===========================================================================
def test_two_clicks_make_a_range_and_the_days_between_are_marked() -> None:
    result = _date("calendar-range")
    first = result["afterFirst"]
    assert first["from"] == "2026-08-05" and first["to"] == "", first
    assert first["start"] == ["2026-08-05"] and first["inRange"] == [], first

    second = result["afterSecond"]
    assert second["from"] == "2026-08-05" and second["to"] == "2026-08-09", second
    assert second["start"] == ["2026-08-05"], second
    assert second["end"] == ["2026-08-09"], second
    # A5: every day strictly between the endpoints is marked in-range.
    assert second["inRange"] == ["2026-08-06", "2026-08-07", "2026-08-08"], second
    print("PASS: the first click sets the start, the second the end, and the span is marked")


def test_a_second_click_before_the_start_swaps_rather_than_inverting() -> None:
    # A6 — SWAP was chosen over restart: two clicks always produce a usable
    # range, and the stored value is always start <= end, so the request can
    # never carry an inverted range the server would read as empty.
    result = _date("calendar-swap")
    swapped = result["afterSwap"]
    assert swapped["from"] == "2026-08-14", swapped
    assert swapped["to"] == "2026-08-20", swapped
    assert swapped["from"] <= swapped["to"], swapped
    assert swapped["inRange"] == [
        "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18", "2026-08-19"
    ], swapped
    print("PASS: clicking before the start swaps the endpoints, never storing start > end")


def test_a_third_click_starts_a_new_range() -> None:
    result = _date("calendar-restart")
    assert result["complete"]["to"] == "2026-08-09", result
    third = result["afterThird"]
    assert third["from"] == "2026-08-21", third
    assert third["to"] == "", third
    assert third["start"] == ["2026-08-21"], third
    assert third["inRange"] == [], third
    print("PASS: a third click restarts the range from that day")


# ===========================================================================
# 4. I3 — the request did not change
# ===========================================================================
def test_the_enhancement_adds_no_field_and_no_submitting_control() -> None:
    result = _date("no-extra-fields")
    # R6 — the only named fields are the ones the server rendered: the operator
    # and the three value carriers. The visible `dd.mm.rrrr` fields and every
    # calendar control are deliberately `name`-less, so the request this filter
    # produces is byte-identical to the one the native pickers produced alone.
    assert result["namedFields"] == [
        "dateop__created_at",
        "date__created_at",
        "date_from__created_at",
        "date_to__created_at",
    ], result
    # A day cell that defaulted to type=submit would apply the filter on every
    # calendar click and would also make the range impossible to complete.
    assert result["allButtonsAreTypeButton"] is True, result
    assert result["buttonCount"] > 28, result
    print("PASS: the calendar adds no request parameter and no submitting control")


def test_a_timestamp_column_keeps_its_datetime_wire_format() -> None:
    result = _date("timestamp-wire-format")
    # A range over a timestamp column spans whole days: the start at 00:00 and
    # the end at the last minute, which is the convention the server-side date
    # presets already encode.
    assert result["range"]["from"] == "2026-08-05T00:00", result
    assert result["range"]["to"] == "2026-08-09T23:59", result
    # A typed time is preserved on a timestamp column rather than flattened.
    assert result["typedTime"] == "2026-08-06T07:15", result
    print("PASS: a timestamp column keeps datetime-local values and an explicitly typed time")


# ===========================================================================
# 5. A7 — keyboard
# ===========================================================================
def test_enter_in_a_typed_field_commits_and_applies() -> None:
    """CC1 — Enter must parse the text AND issue the apply.

    It used to lean on the browser's implicit form submission, which on the live
    page never happened: the key was inert. The apply is now issued explicitly,
    so the outcome no longer depends on a default action.
    """
    result = _date("enter-in-typed-field-applies")
    after = result["afterEnter"]
    assert after["nativeTo"] == "2026-08-27T23:59", after
    assert after["typedTo"] == "27.08.2026 23:59", after
    assert after["invalid"] is False, after
    # Exactly one apply, carrying the just-committed value.
    assert len(after["submissions"]) == 1, after
    assert after["submissions"][0]["to"] == "2026-08-27T23:59", after
    print("PASS: Enter in a typed field commits the value and issues exactly one apply")


def test_enter_on_invalid_text_is_blocked_and_recovers_on_correction() -> None:
    """CC2/CC3 — Enter on unparseable text behaves exactly like the blur path,
    and correcting the text re-evaluates the flag rather than staying stuck."""
    result = _date("enter-in-typed-field-invalid-blocks")
    bad = result["afterInvalidEnter"]
    assert bad["invalid"] is True and bad["ariaInvalid"] == "true", bad
    assert bad["status"], "the rejection must be announced"
    # The text the user typed is kept so they can see and correct it.
    assert bad["typedFrom"] == "32.13.2026", bad
    assert bad["nativeFrom"] == "2026-08-01", bad
    assert bad["submissions"] == 0, "an invalid Enter must not navigate"

    fixed = result["afterCorrection"]
    assert fixed["invalid"] is False, fixed
    assert fixed["nativeFrom"] == "2026-08-28T00:00", fixed
    assert len(fixed["submissions"]) == 1, fixed
    print("PASS: Enter on invalid text blocks the apply, and a correction clears it and applies")


def test_a_focused_calendar_day_activates_on_enter_and_space() -> None:
    """CC4 — the calendar must be operable by keyboard, not pointer-only.

    A focused button normally synthesises a click on Enter and Space; on the live
    page that click never arrived, leaving the grid inert. The grid now advances
    the range itself and suppresses the synthesised click, so the state machine
    runs exactly once however the browser behaves.
    """
    result = _date("calendar-day-keyboard-activation")
    first = result["afterEnterOnDay"]
    assert first["from"] == "2026-08-09" and first["to"] == "", first
    assert first["start"] == ["2026-08-09"], first

    second = result["afterSpaceOnDay"]
    assert second["from"] == "2026-08-09" and second["to"] == "2026-08-13", second
    assert second["inRange"] == ["2026-08-10", "2026-08-11", "2026-08-12"], second

    third = result["afterThirdActivation"]
    assert third["from"] == "2026-08-20" and third["to"] == "", third
    # Activating a day must never submit the filter form.
    assert result["submissions"] == 0, result
    print("PASS: a focused calendar day advances the range on Enter and on Space")


def test_the_calendar_is_one_tab_stop_navigated_by_arrows() -> None:
    result = _date("keyboard-roving")
    # Roving tabindex: a month of separate tab stops would bury the Apply button.
    assert result["initialTabStops"] == ["2026-08-10"], result
    assert result["afterArrowRight"] == ["2026-08-11"], result
    assert result["afterArrowDown"] == ["2026-08-18"], result
    print("PASS: the calendar is a single tab stop and the arrows move within it")


# ===========================================================================
# 6. The markup the server emits still carries the contract
# ===========================================================================
def test_the_server_marks_the_date_group_without_changing_its_fields() -> None:
    """The whole date family is one group: operator, single control, range pair.

    UI-20260831-02 moved the script's hooks onto the group so the single and the
    range modes get the same field treatment and share one calendar. What the
    group must never change is the wire: the names, types and values below are
    exactly what the filter submitted before any of this existed.
    """
    main = api_main

    column = {"column_name": "created_at", "data_type": "timestamp with time zone",
              "display_name": "Utworzono", "is_filterable": True}
    html = main._portal_database_filter_fieldset(column, entry=None, id_prefix="m-")

    # The script's hooks live on the group, once, for both modes.
    assert "data-db-date-group" in html, html
    assert 'data-db-date-type="datetime-local"' in html, html
    assert 'data-db-date-column="Utworzono"' in html, html
    assert "data-db-strings=" in html, html
    # All three carriers are addressable by the endpoint they hold.
    for role in ("single", "from", "to"):
        assert f'data-db-date-input="{role}"' in html, (role, html)
    # R6: the submitted names are exactly what they were before this task.
    for name in ("dateop__created_at", "date__created_at",
                 "date_from__created_at", "date_to__created_at"):
        assert f'name="{name}"' in html, (name, html)
    print("PASS: the server marks the date group and leaves its submitted fields untouched")


def test_both_filter_surfaces_render_the_same_date_group() -> None:
    """R8 — the column menu (`m-`) and the aggregate filter drawer (`p-`) are two
    call sites of ONE renderer, so the reshaped control cannot land on only one
    of them. Only the id prefix may differ."""
    main = api_main

    column = {"column_name": "created_at", "data_type": "timestamp with time zone",
              "display_name": "Utworzono", "is_filterable": True}
    menu = main._portal_database_filter_fieldset(column, entry=None, id_prefix="m-")
    drawer = main._portal_database_filter_fieldset(column, entry=None, id_prefix="p-")

    assert menu != drawer, "the two surfaces must still have distinct ids"
    assert menu.replace('"m-', '"p-') == drawer, (
        "menu and drawer must differ only by id prefix"
    )
    for marker in ("data-db-date-group", 'data-db-date-input="from"',
                   'name="date_from__created_at"'):
        assert marker in drawer, (marker, drawer)
    print("PASS: the column menu and the filter drawer render the identical date group")


def main_() -> None:
    test_using_a_column_menu_does_not_dismiss_it()
    test_the_panel_is_made_to_fit_the_viewport()
    test_the_panel_reveal_never_overshoots()
    test_a_caret_reveal_on_every_keystroke_does_not_dismiss()
    test_dismissal_follows_what_the_gesture_was_aimed_at()
    test_the_reveal_scrolls_the_panel_and_not_the_box_that_merely_overflows()
    test_the_two_modules_always_ship_together()
    test_focusing_a_control_on_open_does_not_dismiss_the_menu()
    test_focus_returned_on_dismissal_does_not_scroll_the_grid()
    test_every_calendar_focus_goes_through_the_shared_guard()
    test_focus_returned_to_a_rejected_field_is_guarded_on_both_paths()
    test_a_control_that_rerenders_itself_does_not_dismiss_its_menu()
    test_a_control_that_rerenders_itself_does_not_collapse_the_panel()
    test_the_filter_panel_dismisses_on_an_outside_click_only()
    test_the_surface_in_use_is_reopened_after_an_apply_navigates()
    test_the_apply_carries_the_grid_scroll_and_the_restore_does_not_self_dismiss()
    test_the_module_still_works_without_session_storage()

    test_bare_digits_grow_their_own_separators_as_they_are_typed()
    test_the_mask_does_not_fight_a_user_who_types_their_own_separators()
    test_backspace_removes_one_digit_and_steps_over_the_separators()
    test_pasted_digits_format_and_commit_and_an_impossible_date_is_flagged()
    test_the_parser_accepts_bare_digits_even_if_the_mask_never_ran()
    test_typed_dates_are_accepted_in_the_documented_forms()
    test_an_unparseable_typed_date_is_rejected_and_never_applied()
    test_a_rejected_typed_date_blocks_the_apply_rather_than_being_dropped()
    test_the_hidden_attribute_beats_every_display_rule_that_could_override_it()
    test_range_mode_shows_two_fields_and_no_calendar_until_focused()
    test_the_calendar_collapses_before_escape_reaches_the_menu()
    test_single_mode_shows_one_field_and_writes_the_single_parameter()
    test_a_typed_date_survives_the_browser_change_then_blur_pair()
    test_enter_in_a_typed_field_commits_and_a_later_blur_does_not_undo_it()
    test_what_the_inputs_show_is_what_an_apply_submits()
    test_the_text_fields_and_the_calendar_never_disagree()

    test_two_clicks_make_a_range_and_the_days_between_are_marked()
    test_a_second_click_before_the_start_swaps_rather_than_inverting()
    test_a_third_click_starts_a_new_range()

    test_the_enhancement_adds_no_field_and_no_submitting_control()
    test_a_timestamp_column_keeps_its_datetime_wire_format()
    test_enter_in_a_typed_field_commits_and_applies()
    test_enter_on_invalid_text_is_blocked_and_recovers_on_correction()
    test_a_focused_calendar_day_activates_on_enter_and_space()
    test_the_calendar_is_one_tab_stop_navigated_by_arrows()
    test_the_server_marks_the_date_group_without_changing_its_fields()
    test_both_filter_surfaces_render_the_same_date_group()

    print("\nALL DATE RANGE AND FILTER DISMISSAL TESTS PASSED")


if __name__ == "__main__":
    main_()
