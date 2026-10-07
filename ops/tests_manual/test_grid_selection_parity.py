#!/usr/bin/env python3
"""`UI-20260827-02` — the two grids answer the same gesture the same way.

The Database Explorer and the Eco Driving tables run the SAME selection module,
which paints the same class names on both. Their appearance was nevertheless
written twice — once in `data-grid.css` and once as a hand-made mirror in
`eco-driving.css` — and the mirror drifted in four places before anyone noticed:
the Eco tables never got row hover, their selection counter lost its accent
badge, their copy hint lost the monospace face, and their cells offered a `cell`
cursor where the other surface offered `copy`.

These checks pin the PARITY, not the four symptoms. They assert that there is
one stylesheet defining selection appearance, that both surfaces load it, and
that neither has grown a private copy of a rule that belongs to it. A fifth
divergence of the same kind fails here without anyone having predicted which
property it would be.

They also assert the pairing invariant in both directions, for every selectable
table: a grid emits a selection host if and only if its page ships the module.

Run:

    cd /opt/log-platform-worktrees/ui-format
    PYTHONPATH="$PWD" /opt/log-platform/.venv/bin/python \\
        ops/tests_manual/test_grid_selection_parity.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_eco_driving_presentation_s12 as s12  # noqa: E402

from api.eco_driving_explorer import html as H  # noqa: E402
from api.portal_ui import assets as portal_assets  # noqa: E402

CSS_DIR = ROOT / "api" / "static" / "css"
SHARED = CSS_DIR / "grid-selection.css"
DB_GRID = CSS_DIR / "data-grid.css"
ECO = CSS_DIR / "eco-driving.css"

SHARED_ASSET = "css/grid-selection.css"
SELECTION_MODULE = "js/data-grid-selection.js"

#: Every class the selection module paints or the selection footer relies on.
#: Read from the module itself below, so a new painted class joins this contract
#: without anyone updating a list.
PAINTED = ("db-cell-in-range", "db-cell-active", "db-range-t", "db-range-b",
           "db-range-l", "db-range-r")
FOOTER = ("db-select-count", "db-select-hint")

PASSES: list[str] = []


def ok(message: str) -> None:
    PASSES.append(message)
    print(f"PASS: {message}")


def _strip_comments(css: str) -> str:
    """CSS with `/* ... */` removed.

    Not cosmetic: these files document their own history, so a comment that
    merely NAMES a class would otherwise read as a selector and every check
    below would be asserting against prose.
    """

    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _blocks(css: str):
    """(selector, declarations) for each rule, comments already removed."""

    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_comments(css)):
        yield " ".join(block.group(1).split()), block.group(2)


def _rule_targets(css: str, token: str) -> list[str]:
    """Selectors in `css` that style `.token` — declarations, not mentions."""

    return [sel for sel, _decl in _blocks(css) if f".{token}" in sel]


# --- 1. one source of truth --------------------------------------------------


def test_the_selection_layer_is_one_stylesheet():
    assert SHARED.exists(), "the shared selection stylesheet is missing"
    css = SHARED.read_text(encoding="utf-8")
    for token in PAINTED + FOOTER:
        assert _rule_targets(css, token), f"{token} is not styled by the shared sheet"
    ok("one stylesheet defines every painted selection class and the footer")


def test_neither_surface_keeps_a_private_copy():
    """The drift is impossible when only one file may style these classes.

    This is the check that would have failed while the mirror existed, and the
    one that fails if somebody re-adds a rule to either surface stylesheet
    instead of to the shared one.
    """

    for path in (DB_GRID, ECO):
        css = path.read_text(encoding="utf-8")
        for token in PAINTED + FOOTER:
            offenders = _rule_targets(css, token)
            assert not offenders, f"{path.name} still styles .{token}: {offenders}"
    ok("neither data-grid.css nor eco-driving.css styles a selection class")


def test_every_class_the_module_paints_is_styled_by_the_shared_sheet():
    """Derived from the module, so a newly painted class cannot go unstyled."""

    js = (ROOT / "api" / "static" / "js" / "data-grid-selection.js").read_text(encoding="utf-8")
    painted = {m for m in re.findall(r'"(db-(?:cell|range)-[a-z-]+)"', js)}
    assert painted, "no painted classes found in the module — has it been renamed?"
    css = SHARED.read_text(encoding="utf-8")
    for token in sorted(painted):
        assert _rule_targets(css, token), f"the module paints .{token} and nothing styles it"
    ok(f"all {len(painted)} classes the module paints are styled by the shared sheet")


# --- 2. both surfaces consume it ---------------------------------------------


def test_both_surfaces_load_the_shared_sheet():
    assert SHARED_ASSET in portal_assets.PAGE_ASSETS
    assert SHARED_ASSET in H.ECO_DRIVING_SELECTABLE_PAGE_ASSETS

    main = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    # The Database Explorer's dataset page is the only Database surface that
    # ships the module; it must also ship the sheet.
    block = main.split('"js/data-grid-selection.js"')[0]
    tail = block[-800:]
    assert f'"{SHARED_ASSET}"' in tail, "the Database Explorer page does not load the shared sheet"
    ok("the Database Explorer page and the Eco selectable pages both load the shared sheet")


def test_the_shared_sheet_loads_after_the_surface_it_defends_against():
    """Order is load-bearing: several defences win on source order, not weight.

    They did when they lived at the bottom of `data-grid.css` too; extracting
    them did not change that, so the ordering is asserted rather than assumed.
    """

    # What matters is the order in each PAGE's asset list, not in the registry.
    eco = list(H.ECO_DRIVING_SELECTABLE_PAGE_ASSETS)
    assert eco.index("css/eco-driving.css") < eco.index(SHARED_ASSET), eco

    main = _strip_comments((ROOT / "api" / "main.py").read_text(encoding="utf-8"))
    page = main.split('"js/data-grid-selection.js"')[0][-600:]
    assert page.index('"css/data-grid.css"') < page.index(f'"{SHARED_ASSET}"'), page
    ok("each page loads the shared sheet after its own surface stylesheet")


def test_the_shared_sheet_is_table_agnostic():
    """It hangs off the selection host, so a grid opts in by declaring one.

    A rule naming a particular table would be the drift starting again: the next
    selectable grid would need its own copy.
    """

    css = SHARED.read_text(encoding="utf-8")
    for selector in _rule_targets(css, "db-cell-in-range") + _rule_targets(css, "db-cell-active"):
        assert "[data-db-sheet]" in selector, selector
        assert ".db-table" not in selector and ".eco-table" not in selector, selector
    ok("the shared selection rules name the host, never a table class")


# --- 3. row hover, the difference the user reported --------------------------


def test_both_surfaces_highlight_the_hovered_row_with_the_same_token():
    db = _strip_comments(DB_GRID.read_text(encoding="utf-8"))
    eco = _strip_comments(ECO.read_text(encoding="utf-8"))
    pattern = re.compile(r"tbody tr:hover td\s*\{([^}]*)\}")
    db_rule = pattern.search(db)
    eco_rule = pattern.search(eco)
    assert db_rule, "the Database Explorer lost its row hover"
    assert eco_rule, "the Eco tables have no row hover"
    assert "--lp-surface-subtle" in db_rule.group(1)
    assert "--lp-surface-subtle" in eco_rule.group(1), eco_rule.group(1)
    ok("both surfaces highlight the hovered row with var(--lp-surface-subtle)")


def test_the_copy_cursor_is_decided_in_one_place():
    css = SHARED.read_text(encoding="utf-8")
    cursor_blocks = [f"{sel} {{{decl}}}" for sel, decl in _blocks(css) if "cursor" in decl]
    assert cursor_blocks, "the shared sheet decides no cursor"
    joined = "\n".join(cursor_blocks)
    assert "copy" in joined
    # Both vocabularies meet here and nowhere else.
    assert ".db-copyable" in joined and "data-eco-column" in joined, joined
    eco = _strip_comments(ECO.read_text(encoding="utf-8"))
    assert "cursor: cell" not in eco, "the Eco tables still promise the old cursor"
    # And the surface stylesheet no longer decides it for its own vocabulary.
    for sel, decl in _blocks(DB_GRID.read_text(encoding="utf-8")):
        if ".db-copyable" in sel:
            assert "cursor" not in decl, f"{sel} {{{decl}}}"
    ok("the copy cursor for both vocabularies is decided only by the shared sheet")


# --- 4. the pairing, in both directions, for every selectable table ----------


def _rankings(**extra):
    controller, _ = s12.make(**extra)
    return s12.ranking(controller)


def test_host_and_module_travel_together_on_the_ranking():
    populated = _rankings()
    host = "eco-select-sheet" in populated.body_html and "data-db-sheet" in populated.body_html
    shipped = SELECTION_MODULE in populated.page_assets
    assert host and shipped, (host, shipped)
    assert SHARED_ASSET in populated.page_assets

    empty = _rankings(data=s12.responses(entries_list=[], entries_count=[{"total_count": 0}]))
    host = "data-db-sheet" in empty.body_html
    shipped = SELECTION_MODULE in empty.page_assets
    assert host == shipped, f"empty ranking: host={host} module={shipped}"
    assert not host, "an empty ranking emitted a selection host"
    ok("the ranking emits a host exactly when its page ships the module")


def test_the_action_column_is_outside_the_selection_universe():
    """Criterion 4, asserted structurally rather than by counting columns.

    The module's universe is `[data-eco-column]`. The details link must not be
    in it, and neither must its header, or a copied rectangle would carry a
    control into a spreadsheet.
    """

    body = _rankings().body_html
    details = s12.t("eco.col.details")
    for cell in re.findall(r"<td[^>]*>.*?</td>", body, re.S):
        if details in cell:
            assert "data-eco-column" not in cell, cell
            assert "data-eco-copy" not in cell, cell
    # The action column's header is the empty one, and it carries no mark.
    assert "<th></th>" in body, "the action column's header changed shape"
    ok("the details action column and its header are outside the selection universe")


def test_an_annotated_cell_copies_the_value_alone():
    """Criterion 5. The rendered cell text concatenates; the clipboard must not."""

    body = _rankings().body_html
    annotated = [
        cell for cell in re.findall(r"<td[^>]*data-severity=.*?</td>", body, re.S)
        if "data-eco-copy" in cell
    ]
    assert annotated, "no annotated metric cell was rendered"
    for cell in annotated:
        copy = re.search(r'data-eco-copy="([^"]*)"', cell).group(1)
        text = re.sub(r"<[^>]+>", "", cell)
        assert copy, cell
        assert copy != text.strip(), "the clipboard value is the concatenated text"
        assert text.strip().startswith(copy), (copy, text.strip())
        # The severity word rides along in the cell and must not ride along here.
        assert "straty" not in copy and "punkt" not in copy, copy
    ok("an annotated metric cell copies its value, not the value plus its annotation")


def test_every_copy_value_is_something_the_cell_already_shows():
    """The privacy boundary, as a check rather than as a comment.

    Selection makes visible data copyable. A `data-eco-copy` carrying anything
    the cell does not render would mean the DOM had gained a value to copy,
    which is the one thing this feature must never do.
    """

    body = _rankings().body_html
    checked = 0
    for cell in re.findall(r"<td[^>]*data-eco-copy=.*?</td>", body, re.S):
        copy = re.search(r'data-eco-copy="([^"]*)"', cell).group(1)
        shown = re.sub(r"<[^>]+>", "", cell)
        # Normalised: the printed form may add a unit, a thousands separator or
        # a trailing annotation, but it must CONTAIN what is copied.
        assert copy.replace(",", "") in shown.replace(",", "").replace(" ", ""), (copy, shown)
        checked += 1
    assert checked, "no marked cells were rendered"
    ok(f"all {checked} ranking clipboard values are substrings of what the cell prints")


def test_selection_did_not_spread_to_the_out_of_scope_tables():
    """Criterion 7: hover parity is allowed to be global, selection is not."""

    controller, _ = s12.make(trip=True)
    landing = controller.landing(user=s12.USER)
    detail = s12.detail(controller)
    for name, result in (("landing", landing), ("ranking-entry detail", detail)):
        assert "data-db-sheet" not in result.body_html, name
        assert "data-eco-column" not in result.body_html, name
        assert SELECTION_MODULE not in result.page_assets, name
    ok("the landing and ranking-entry detail tables gained no selection and no module")


# --- 5. clipboard conventions, pinned across both grids ----------------------


def _db_integer_copy(value: int) -> tuple:
    """(visible, data-db-copy) for a Database Explorer integer cell."""

    import html as _htmllib
    import api.main as _main

    cell = _main._portal_database_cell_html(value, {"data_type": "bigint"})
    visible = _htmllib.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
    copy = re.search(r'data-db-copy="([^"]*)"', cell).group(1)
    return visible, _htmllib.unescape(copy)


def _eco_integer_copy(count: int) -> tuple:
    """(visible, data-eco-copy) for an Eco ranking integer cell, in sum mode."""

    from api.eco_driving_explorer import eco_view as V

    metric = V.RANKING_METRIC_ORDER[0]
    cell = V.metric_cell(
        metric_key=metric,
        displayed_value=V.metric_display_value(unit="sum", event_count=count, rate=None),
        points=None, loss=None, rate=None, column=metric,
        copy=V.metric_copy_value(unit="sum", event_count=count, rate=None),
    )
    visible = re.sub(r"<[^>]+>", "", cell).strip()
    copy = re.search(r'data-eco-copy="([^"]*)"', cell).group(1)
    return visible, copy


def test_both_grids_copy_an_integer_the_same_way():
    """One gesture, two grids, one result — asserted against BOTH renderers.

    This is the third divergence between these two grids and the second on
    clipboard semantics: the Eco ranking already displayed `2 640` and copied
    `2640`, and the Database Explorer briefly displayed `9 000` and copied
    `9 000`. Neither implementation is derived from the other -- they cannot be,
    because `trip_export.text_cell` returns a dot decimal and rounds floats,
    which the Database Explorer's own decimals must not do -- so the convention
    is held in step here, by test, rather than by intention.
    """

    for value in (9000, 1234567, 48213):
        db_visible, db_copy = _db_integer_copy(value)
        eco_visible, eco_copy = _eco_integer_copy(value)

        # Both DISPLAY the grouping -- with their own separator character,
        # which is a presentation choice each surface is entitled to.
        separators = ("\u00a0", "\u202f", " ")
        assert any(c in db_visible for c in separators), (value, repr(db_visible))
        assert any(c in eco_visible for c in separators), (value, repr(eco_visible))
        # ...and neither puts any of them on the clipboard.
        for label, copied in (("database", db_copy), ("eco", eco_copy)):
            assert copied == str(value), (label, value, copied)
            assert not any(c in copied for c in separators), (label, repr(copied))
        # And they agree with each other, which is the property that matters.
        assert db_copy == eco_copy, (value, db_copy, eco_copy)
    ok("both grids display an integer's grouping and copy it without")


def test_a_grouped_decimal_copies_without_its_grouping():
    """The same rule, on the family that hid the defect until it grew.

    THE ECO SIDE HAS NO COMPARABLE CASE, so this half is asserted on the
    Database Explorer alone rather than as a two-grid comparison. Eco's
    `fmt_decimal` does not group at all -- `1234567,89`, no separator -- so
    there is no Eco grouped decimal to hold in step with. That absence is
    pinned below, because if Eco ever starts grouping decimals it acquires
    exactly the defect this task removed, and the pin should notice.
    """

    from decimal import Decimal

    from api.eco_driving_explorer import eco_view as V
    import api.main as _main
    import html as _htmllib

    cell = _main._portal_database_cell_html(Decimal("1234567.891"),
                                            {"data_type": "numeric"})
    visible = _htmllib.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
    copy = _htmllib.unescape(re.search(r'data-db-copy="([^"]*)"', cell).group(1))
    assert "\u00a0" in visible, repr(visible)
    assert copy == "1234567,891", repr(copy)

    # Eco: ungrouped today. If this changes, the line above needs a partner.
    assert V.fmt_decimal(Decimal("1234567.891"), 2) == "1234567,89", \
        "Eco began grouping decimals; it now needs the same clipboard rule"
    ok("a grouped decimal copies without grouping; Eco has no grouped decimal to match")


def test_the_two_grids_do_not_share_a_number_formatter():
    """Why the pin above is a test and not an import.

    `trip_export.text_cell` calls itself the definition of a Polish-Excel value
    and serves Eco. It cannot serve the Database Explorer: a `Decimal` comes
    back with a DOT, and a float is rounded to two places. Both would be wrong
    for a grid that renders arbitrary client numerics at full precision. If that
    ever stops being true, this check fails and the two can be merged.
    """

    from decimal import Decimal

    from api.eco_driving_explorer.trip_export import text_cell

    assert text_cell(Decimal("51.967419")) == "51.967419", "dot, not the Polish comma"
    assert text_cell(51.967419) == "51,97", "a float is rounded to two places"
    # The Database Explorer must not do either of those.
    _visible, copied = _db_integer_copy(9000)
    assert copied == "9000"
    ok("the two number conventions are held in step by test, not by a shared helper")


def main() -> None:
    for name, value in sorted(globals().items()):
        if name.startswith("test_"):
            value()
    print(f"\nOK - grid selection parity checks passed ({len(PASSES)} checks)")


if __name__ == "__main__":
    main()
