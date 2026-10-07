# DESIGN_SOURCE_MANIFEST — v3 (approved mobile redesign)

**This package supersedes `driver-eco-dashboard-final-design-v2`.** It is not an
overwrite: v2 stays where it is, unmodified, so the two can be diffed. Copy
**this** folder's `production/*`.

**Statement of authority.** Two approved design results now share one
presentation layer, separated by one width:

* **`>= 760 px` — "Eco Dashboard Strona" (v2), unchanged.** Three slides
  (Twój wynik / Wykroczenia na osi / Podsumowanie dzienne), the gated
  slide-boundary gesture, the vertical dot tablist, the panning daily table,
  the 1180/980/760 px breakpoints, the height tiers and the once-per-visit
  entrance motion. v3 changes nothing about any of it.
* **`<= 759 px` — "Warianty mobilne", variant 1a "Zakładki" (v3).** Three tabs
  in a bottom bar, free scrolling, accordions in place, a vertical list of day
  cards.

Where the two disagree, the width decides, and neither is allowed to leak
across the boundary. That is asserted by
`test_the_presentation_boundary_is_the_approved_width`, which also proves the
renderer emits the **same bytes** for the desktop page whether the mobile
branch is asked for or not.

## Production files (exactly five)

| File | SHA-256 | vs v2 |
|---|---|---|
| `production/index.html` | `d50444ae62e8067d8d29111741a6c14dd0129d3b7fb346682ef4bf065b476ca7` | unchanged |
| `production/css/dashboard.css` | `35c416ee248190ec9f2ee66ba460459e1b2db718e11d34ccc6d981e96c2e6f93` | appended to |
| `production/js/format.js` | `6a4b834cb26d2acf5285b5f3f79f3989f5dc0e429e6d690509476c56be3aa998` | unchanged |
| `production/js/render.js` | `ef0d3fc1b24367611708dacd8a98319d5b33e1df87097b980ce0047d3ab1d9c2` | appended to |
| `production/js/app.js` | `6c0a74cdb397e3492c702bc306a415e428ff38b59b10e61f004652af501d66af` | changed |

`css/dashboard.css` and `js/render.js` are **strictly additive** against v2:
every byte v2 shipped is still there, at the same offset, and the mobile
material follows it. `js/app.js` is the only file with edited lines, and every
one of them is either a widened selector that resolves to the same nodes on the
desktop page, a value read from an attribute the desktop page does not carry,
or a branch guarded by `state.mobile`.

`js/capability-bootstrap.js`, `js/boot.js`, `js/snapshot-source.js` are
repo-owned and are NOT part of this export.

Boundary unchanged: `window.EcoApp.boot({ source })` and
`window.EcoRender.renderAccessState(code)`. No new file, no new asset, no new
request. CSP-clean: no inline `<script>`, no `<style>` element, no external
font/image/CDN.

---

## The mobile redesign, as implemented

### M1 · three tabs, no gated gesture

`renderMobile` emits `.edm-app` — a fixed-height flex column of header, three
`role="tabpanel"` scrollers and a `role="tablist"` bottom bar. `app.js` drives
it with the tablist code the dots already used: roving `tabindex`,
`aria-selected` on exactly one tab, the inactive panels `inert` +
`aria-hidden` + `display:none`. Arrow keys move and activate; Home/End jump;
Enter/Space activate.

`onWheel`, `onTouchStart` and `onTouchMove` return immediately when
`state.mobile`, so the boundary gating, the burst detector and the swipe
threshold are all inert below 760 px. `.ed-mover` and `.ed-dots` are not
rendered at all.

Routing is untouched: tab *i* is route `i + 1`, written with `replaceState`
exactly as before, so `#weekly/3` from a months-old e-mail opens the Dni tab.

### M2 · a vertical list of day cards

One `.edm-day` per day: date · weekday · distance · event total. A driving day
is a `<button>` rail with `aria-expanded`/`aria-controls` over a `hidden` body
that lists its non-zero categories (status dot straight from
`days[].categories[].status`) and its trip count. A `0 km` day is a plain card
with `—` for the distance, "dzień bez jazdy" for the events, and no accordion —
there is nothing behind it and it is not a gate.

This is what removes `.ed-scrollx` from the mobile page. The rule that made it
pan still exists, still scoped to that one class and still behind
`@media(max-width:759px)`, because the no-`matchMedia` fallback renders the
desktop tree at any width and must keep working; the *element* is simply never
rendered below the boundary any more.

### M3 · category cards with an in-place axis

Eight `.edm-cat` cards, ordered by point loss (most negative first, then the
full pools, snapshot order breaking every tie). Collapsed: status dot, name,
points lost, a change chip, a chevron. Expanded: two sunken stat tiles
(coefficient now; previous period's coefficient and points lost, or "brak
porównania") and the band axis — labels above, 18 px segments in band-status
colours, point losses below, a solid "now" marker and a dashed "previously"
marker at `(index + 0.5) / bands`.

The change chip carries the **size** of the move and the arrow its direction,
so no minus sign appears in it; a screen reader gets the direction spelled out
in an `sr-only` phrase beside it. With no comparison basis the chip is `—`, the
previous-period tile says so, and the dashed marker is not drawn.

### M4 · entrance motion

Same contract, new rhythm. 1600 ms window, `easeOut(p) = 1 − (1 − p)³`, start
200 ms after the tab is shown, failsafe at 1600 + 600 ms. Cards reveal almost
immediately (0.02 / 0.05 / 0.08 / 0.11 / 0.14 → hero badge, quick win, losses,
distribution, trend) over 0.3 s and 8 px; list rows cascade at
`0.02 + i × 0.014` over 0.25 s. The cascade constants are read from
`data-casc-base` / `data-casc-step` on the panel — absent, as on the desktop
page, the original 0.06 / 0.042 apply unchanged.

Numbers count up, the ring sweeps and takes its colour from the value currently
shown, bars grow, and the 78 → 75 comparison keeps its two-phase sequence. The
rendered string always contains the final values, every counted number keeps its
`sr-only` twin from the first frame, and under
`prefers-reduced-motion: reduce` the animation never starts —
`test_reduced_motion_shows_the_final_values_immediately` proves that in a
browser with the preference genuinely set.

---

## States

Derived from the same snapshot fields the desktop page reads; nothing new is
required of the host.

| State | Mobile shape |
|---|---|
| `INSUFFICIENT_DISTANCE`, `REPORT_NOT_READY` | full screen, **no tab bar**, no Eco payload — the v2 markup, unchanged |
| four access states, loading skeleton | unchanged |
| `ranking_state ≠ RANKED` | no ranking tile, no group distribution; the distance tile takes the whole row |
| `NEWLY_RANKED` | ranking tile with the "nowość w rankingu" chip |
| `NO_COMPARISON_BASIS` | no "Teraz vs poprzedni raport" card, no change chips; every chip in its no-basis variant; category cards show `—` and "brak porównania"; no dashed marker |
| period with no violations | "Gratulacje!" instead of a quick win; full-points banner |
| `0 km` days | neutral cards, no accordion, no aggregate status |

Verified in a real browser against `ranked_acceptable`, `ranked_dangerous`,
`ranked_safe`, `newly_ranked`, `left_ranking`, `not_on_roster`,
`not_ranked_by_configuration`, `no_comparison`, `first_closed_period_of_month`,
`end_of_month_period`, `monthly_31_days`, `skipped_period_in_series`,
`zero_event_category`, `no_driving_day`, `insufficient_period_distance` and
`report_not_ready` — the whole 16-fixture matrix — at 320, 390 and 759 px.

## Accessibility

One `<h1>` per page, in the shared header. Each tab panel is labelled by its
tab and carries an `sr-only` `<h2>`; each section its own `sr-only` `<h3>`.
Disclosures are real buttons with `aria-expanded` and `aria-controls`, and a
collapsed body is `hidden`, so nothing a driver cannot see is readable to a
screen reader. Bars and the band axis are `role="img"` with text alternatives.
Focus rings on full-bleed rails are inset so a card with `overflow:hidden`
cannot clip them. `forced-colors: active` outlines every mobile element that
communicates by colour alone.

## Verification

* `ops/tests_manual/test_driver_eco_dashboard_frontend.py` — the full contract
  suite, including the three new mobile tests and the reduced-motion test.
* `ops/tests_manual/test_driver_eco_dashboard_viewport_sweep.py` — 8 viewports
  from 320 px to 1920 px across 7 states and 3 routes; below 760 px it now
  asserts that **nothing** pans.
* A DOM/geometry/computed-style capture of every fixture × route × desktop
  viewport, taken before and after the change and compared byte for byte, is
  how "desktop did not change" was established rather than asserted.
