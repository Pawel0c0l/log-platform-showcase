# DESIGN_SOURCE_MANIFEST — v2 (corrected final export)

**This package supersedes `driver-eco-dashboard-final-design` (v1).** It is not an overwrite: v1
stays where it is, unmodified, so the two can be diffed. Copy **this** folder's `production/*`.

**Statement of authority:** the CURRENT Claude Design result ("Eco Dashboard Strona" — 3-slide
page: Twój wynik / Wykroczenia na osi / Podsumowanie dzienne; green/alpha visual language;
slide-boundary scroll transitions; once-per-visit entrance motion; 1180/980/760 px responsive
breakpoints) remains the sole visual and interaction source of truth. v2 changes **nothing** about
that language. It adds the three missing product/accessibility contracts (A1, A2, A3) inside the
approved composition, in the design source, and re-exports.

## Production files (exactly five)

| File | SHA-256 | vs v1 |
|---|---|---|
| `production/index.html` | `d50444ae62e8067d8d29111741a6c14dd0129d3b7fb346682ef4bf065b476ca7` | unchanged |
| `production/css/dashboard.css` | `ab8095786b4aa0932c19bdede9e6c524af15b16912ebf8cc73c683bfa45ff2fe` | changed |
| `production/js/format.js` | `6a4b834cb26d2acf5285b5f3f79f3989f5dc0e429e6d690509476c56be3aa998` | unchanged |
| `production/js/render.js` | `f5435d3ca7d81d02b41b56d6d1eb365d1981317dd270efafbdb376d5daf3b709` | changed |
| `production/js/app.js` | `2670ebefa0edd35dcbce24de487370018fc6838cca5c8b91bffb2ec65ec22ecd` | changed |

`js/capability-bootstrap.js`, `js/boot.js`, `js/snapshot-source.js` are repo-owned and are NOT part
of this export (index.html references them; engineering retains its own byte-exact copies).
`preview/repo-owned/snapshot-source.js` is a verbatim copy included ONLY so the local preview runs.

Boundary unchanged: `window.EcoApp.boot({ source })` and `window.EcoRender.renderAccessState(code)`;
no other integration surface, no new file, no new asset, no new request. Every dynamic
number/state still comes from the snapshot document — no hardcoded fixture values. CSP-clean: no
inline `<script>`, no `<style>` elements, no external fonts/images/CDNs — zero external requests.

---

## Required corrections — implemented in the design source

### A1 · daily coefficient implemented ✅ — **WITHDRAWN 2026-08-24**

> **Superseded by owner decision (`18717cf`).** Everything described in this section shipped and
> then was deliberately removed: slide 3 no longer has a per-day disclosure, caret, detail panel or
> coefficient chip, and the slide-3 footnote reverted to pointing at the previous slide. The
> normalised coefficient is presented once, for the period, on slide 2 at the axes; the daily table
> reports counts only. The surviving requirement is that a count and a rate stay two separately
> reachable facts, and it is asserted by
> `test_counts_and_coefficients_stay_separately_available`, which now also asserts that the
> per-day disclosure is **not** reintroduced. The record below is kept for diffing, not to be
> implemented.
>
> One consequence needed its own correction: the day rows were the only focusable content inside
> the mobile pan track, so removing them made the daily table unreachable by keyboard at 320px.
> `.ed-scrollx` is now itself focusable (`tabindex="0"`), named and given an inset focus ring —
> see `test_the_day_table_pan_track_is_keyboard_reachable`.

Slide 3 ("Dzień po dniu"): each **driving** day row is now an expandable disclosure. Expanding it
reveals `Wskaźnik na 100 km · <dzień> · <km>` followed by one compact chip per applicable category:
`<krótka nazwa>` + `<wartość> na 100 km`.

* value is `days[].categories[].coefficient_per_100km`, printed verbatim — **nothing is derived in
  the browser** (no division, no rescaling, no rounding beyond the shared `pl-PL` integer format
  already used by the axes on slide 2, so the two slides read identically);
* the raw `count` stays exactly where it was, in the collapsed row — the two facts are separately
  readable, and the coefficient always carries the `na 100 km` unit so it can never be mistaken for
  a count;
* reachable for **every applicable day/category**: a chip exists for each day-category the snapshot
  scores. A day with no applicable category (a zero-kilometre day, or a day whose categories are
  all `neutral`) has **no** disclosure and **no** panel — nothing is invented for it;
* default view is untouched: the row collapsed is pixel-identical to v1 except for one 9 px caret
  glyph in the day cell (`▾`, `--ghost`), which is the affordance;
* on mobile the panel sticks to the visible edge of the approved pan track, so all chips are
  readable without panning sideways (see `reference/mobile-390-slide3-day-expanded.png`);
* interaction: click/tap anywhere on the row, or activate the day-cell `<button>` with
  Enter/Space. `aria-expanded` + `aria-controls` on that button; the panel carries the `hidden`
  attribute while collapsed.

One wording change was unavoidable — the slide-3 footnote said the "na 100 km" rates live on the
previous slide only. It now reads: *"Rozwiń dzień, aby zobaczyć jego wskaźniki „na 100 km". Wskaźniki
całego okresu znajdziesz na poprzednim slajdzie, przy osiach wykroczeń."*

### A2 · distance movement implemented ✅

The "Dystans w tym okresie" tile on slide 1 now carries the period-over-period movement, in the
**same** chip + caption treatment the ranking tile already uses (no new card, no new component, no
new colour):

```
1 100 km
▲ 443 km więcej   poprzednio 657 km · 37 przejazdów
```

* current distance: `current.total_kilometers` (unchanged, still the animated headline number);
* previous distance: `current.comparison.previous_total_kilometers`, printed as `poprzednio <n> km`;
* direction: `▲` / `▼` / `=`, and the magnitude is the difference of those two **rounded snapshot
  integers** — the same class of presentational arithmetic the approved design already uses for
  the "Top n %" line and the distribution bar. Direction and previous value are both present, so
  the delta is never the only evidence;
* movement is deliberately **valence-neutral** (`ed-chip-flat` in all three directions): more
  kilometres is neither good nor bad, and colouring it would have invented a judgement the product
  does not make;
* screen readers get "▲ 443 km więcej **niż w poprzednim okresie**" via `sr-only` text;
* **no comparison / no previous distance** → the existing no-basis language, never a zero, never a
  neutral arrow, never a fabricated previous value: `brak porównania dystansu`
  (`ed-chip-nobasis`, the dashed chip already used by the score card). Verified on
  `weekly_no_comparison` and `weekly_first_closed_period`
  (`reference/desktop-1440-no-comparison.png`).

### A3 · tab/disclosure semantics implemented ✅

**Slide navigation.** The dots keep their exact visual design (13 px, `--ghost` → `--ink`, hover
scale, vertical rail, the 800 ms transform transition). Underneath:

* `.ed-dots` is `role="tablist" aria-orientation="vertical" aria-label="Sekcje raportu"`;
* each dot is `role="tab"`, `id="ed-tab-<i>"`, `aria-selected`, `aria-controls="ed-panel-<i>"`,
  roving `tabindex` (`0` on the selected tab, `-1` on the others);
* each slide is `role="tabpanel"`, `id="ed-panel-<i>"`, `aria-labelledby="ed-tab-<i>"`;
* all three panels stay in the DOM (the transition moves them); the two inactive ones are
  `aria-hidden="true"` **and** `inert`, so keyboard users cannot tab into off-screen content. If
  focus is inside a panel when it leaves, focus moves to the newly selected panel
  (`preventScroll`), never into an `aria-hidden` subtree;
* keyboard: **Left/Right** *and* **Up/Down** move (wrapping) and activate, **Home** selects the
  first, **End** the last, **Enter/Space** activate the focused tab. Focus stays visible — the
  global `:focus-visible` alpha ring, offset for the dot rail
  (`reference/desktop-1440-tab-focus.png`);
* explicit activation (click or key) bypasses the wheel-inertia transition lock, so arrow-key
  navigation is never swallowed; the lock still guards wheel/swipe exactly as approved;
* `aria-current` was replaced by `aria-selected` (a tab may not use both); the CSS selector moved
  with it, so the painted result is identical.

**Axis tap-to-reveal.** The interaction and appearance are unchanged: hover reveals on desktop,
tap toggles on touch, `data-action="axtap"` still drives the row. The disclosure semantics now sit
on the category-name element, which became a real `<button>` (same font, weight, colour and box —
`text-align:left`, no chrome):

* `aria-expanded="true|false"`, `aria-controls="ed-axtips-t-<i> ed-axtips-b-<i>"` (band labels and
  band points — the two things the reveal actually shows);
* Enter/Space activate it natively; Space also works on the row itself for pointer-parity;
* expanding flips the revealed tips from `aria-hidden="true"` to `"false"`, so the disclosure is
  honest — the content really does become available, not just visible.

  *Why the control is the name and not the row:* ARIA makes the children of a `button` (and of an
  `img`) presentational, so a control may not wrap the content it reveals. For the same reason the
  axis segments moved into their own `role="img"` layer (`.ed-axis-segs`, `position:absolute;
  inset:0`) — geometry measured identical (`230px 750px 150px 90px`, axis 750.00 px, segment
  107.14 px, at 1440 px), and slide 2 renders **pixel-identical** to v1 (0 differing pixels at both
  1440 and 390 px).

**A second real disclosure** — the A1 daily detail — carries the same contract, so the requirement
is met twice over.

---

## Decisions carried forward, deliberately

* **Mobile internal table pan intentionally retained.** The daily table still pans sideways inside
  its own bounded container below 760 px, exactly as approved. Verified at 320/390/768/1280/1440 px
  across all 14 fixtures × 3 slides: `document.documentElement.scrollWidth` and
  `document.body.scrollWidth` never exceed `window.innerWidth`, and `.ed-scrollx` is the **only**
  element with a horizontal overflow — no other component escapes the viewport. The new daily
  detail panel does not widen it (it sticks to the visible edge instead).
* **Embedded Manrope intentionally retained.** `dashboard.css` still embeds the Manrope variable
  font (400–800, latin + latin-ext) and the Telematics mark as `data:` URIs. No system font, no
  external request, no CDN. ⚠ Still requires the agreed one-line CSP amendment
  `font-src 'self'` → `font-src 'self' data:`. No other CSP relaxation is needed or requested.

## Visual deviations from the previously approved design

Measured by pixel diff of the exported implementation against v1, same fixture
(`weekly_ranked_sparse`), same viewports, same settled animation state:

| Screen | Differing pixels | What differs |
|---|---|---|
| `desktop-1440-slide2.png` | **0 (0.000 %)** | — |
| `mobile-390-slide2.png` | **0 (0.000 %)** | — |
| `desktop-1440.png` | 4 359 (0.336 %) | A2 movement chip in the distance tile + the reflow it causes below it |
| `laptop-1280.png` | 4 329 (0.423 %) | same |
| `mobile-390.png` | 39 547 (12.015 %) | same chip; at 390 px the tile grows ~14 px and everything under it shifts down — no layout or style change, pure translation |
| `desktop-1440-slide3.png` | 14 434 (1.114 %) | A1 caret glyph in each driving-day cell + the reworded footnote |
| `mobile-390-slide3.png` | 8 798 (2.673 %) | same |

(Run-to-run noise on the score ring's antialiasing is ~34 px; anything at that scale is not a
change.) There are **no** other deviations: no colour, type, spacing, radius, shadow, chart,
donut/ring, animation, 800 ms transition, count-up, bar growth, breakpoint, hover effect, wording
or hierarchy change. One v1 accident was preserved on purpose rather than "fixed": the right end of
the axis bar stays square, because in the approved design the last segment was never `:last-child`
of its container. Rounding it would have been a 1:1 regression, so the CSS keeps it off explicitly.

Deviations inherited from v1 and unchanged: font delivery mechanism (`data:` URI vs the Design
session's Google Fonts request — typography is 1:1), logo as a `data:` URI, and snapshot data in
place of the Design session's demo copy.

---

## Verification performed on this export

**Renderer / behaviour suite (jsdom, 3 994 assertions, all pass).** Every fixture × every present
period × all 3 slides, plus a driven-DOM interaction pass:

* no `undefined`, no `NaN`, no literal `null` in text or in any attribute;
* A1 — for every day the snapshot scores, the panel exists and contains exactly the applicable
  categories, each with the snapshot's own coefficient; days with no applicable category have no
  panel at all (no invented bucket);
* A2 — movement chip matches the snapshot pair where a comparison exists; where it does not, the
  no-basis chip is present and no previous distance appears anywhere;
* A3 — exactly 3 tabs / 3 tabpanels, exactly one `aria-selected="true"` (the routed one), exactly
  one panel in the tab order, exactly two `aria-hidden + inert` panels, every `aria-controls`
  resolving to a real id; keyboard Left/Right/Up/Down/Home/End/Enter/Space drive selection, focus
  and `--seg` together; roving tabindex and inert state survive slide changes and re-renders;
  disclosures toggle `aria-expanded`, the `hidden` attribute and the tips' `aria-hidden`, from both
  the button and the row, by pointer and by key;
* the four access states still render (`role="alert"`, retry only on `SERVICE_UNAVAILABLE`).

**Browser suite (Chromium 151 headless, 1 272 assertions, all pass).** The exported
`production/index.html` loaded exactly as production loads it (repo-owned scripts stubbed with
their real behaviour, snapshot served over `/api/snapshot`), for all 14 fixtures × 5 viewports
(320/390/768/1280/1440) × 3 slides:

* **zero** console errors, page errors, warnings and failed requests;
* **zero** document-level horizontal overflow; only `.ed-scrollx` pans;
* no `undefined`/`NaN`/`null` in painted text.

**Fixtures covered** (all 14 ship in `preview/fixtures/`): weekly_ranked_sparse ·
weekly_ranked_dangerous · weekly_ranked_safe_zero_events · weekly_not_ranked_by_configuration ·
weekly_not_on_roster · weekly_left_ranking · weekly_newly_ranked · weekly_no_comparison ·
weekly_no_driving_day · weekly_first_closed_period · weekly_skipped_period_in_series ·
weekly_insufficient_distance · weekly_report_not_ready · monthly_31_days.

Sparse distribution still holds: absent rating buckets render nothing — no zeros, nulls, dashes,
empty segments or legend items. Ranking surfaces stay absent outside `RANKED`. No fabricated
comparison when `comparison: null`. `INSUFFICIENT_DISTANCE` and `REPORT_NOT_READY` still render the
single state panel with no dashboard data behind it (fail-closed).

## Reference screenshots (this export, `weekly_ranked_sparse` unless noted)

`desktop-1440.png` · `laptop-1280.png` · `mobile-390.png` — slide 1 at the three required viewports
`desktop-1440-slide2.png` · `desktop-1440-slide2-details.png` (axis reveal) · `desktop-1440-slide3.png`
`mobile-390-slide2.png` · `mobile-390-slide3.png`
`desktop-1440-slide3-day-expanded.png` · `mobile-390-slide3-day-expanded.png` — **A1**
`desktop-1440-no-comparison.png` (`weekly_no_comparison`) — **A2** no-basis state
`desktop-1440-tab-focus.png` — **A3** keyboard focus on the tablist
