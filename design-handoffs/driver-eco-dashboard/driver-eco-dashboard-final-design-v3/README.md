# driver-eco-dashboard-final-design-v3

Final production presentation for the Driver Eco Dashboard. **v3 changes the
mobile presentation only.** At `<= 759 px` the approved redesign
("Warianty mobilne", variant **1a — Zakładki") replaces the three gated slides
with an application-style three-tab page: **Wynik · Wykroczenia · Dni** in a
bottom tab bar, free scrolling inside each tab, disclosure in place, and a
vertical list of day cards where the horizontally panning daily table used to
be.

**At `>= 760 px` nothing changes.** The desktop page is the approved
`driver-eco-dashboard-final-design-v2` result, reproduced byte for byte by the
same renderer. `index.html` and `js/format.js` are byte-identical to v2 as
files; `css/dashboard.css` and `js/render.js` are strictly additive; `js/app.js`
gains the mobile branches and nothing else.

**Supersedes `../driver-eco-dashboard-final-design-v2`, which is left untouched
for diffing.** Copy from *this* folder.

## What v3 adds, and nothing else

* **M1 — three tabs instead of three gated slides.** Below the boundary the
  `.ed-mover` transform, the navigation dots, the wheel-gesture gating and the
  swipe-to-cross gesture are all gone. Each tab is its own scroller, so the
  reading position of the tab you left is still there when you come back, and
  switching is immediate.
* **M2 — the daily table became a list of day cards.** One card per day, with
  the day's distance and its event total on the rail and its per-category
  counts behind an accordion. A `0 km` day is a quieter card with no accordion,
  exactly as before: still a normal, neutral day, still no aggregate status of
  its own. **This removes the only horizontally panning surface in the
  product** — below 760 px nothing pans, at any width, in any state.
* **M3 — the axes became category cards.** Eight cards ordered by point loss,
  each collapsing to `status dot · name · points lost · change chip · chevron`
  and expanding in place to the coefficient, the previous period, and the band
  axis with its "now" and "previously" markers.
* **M4 — a new entrance rhythm on mobile.** Same contract as ever (the DOM
  carries the final values from the first frame, counted numbers keep their
  `sr-only` twin, `prefers-reduced-motion: reduce` skips the animation
  entirely), with the approved 1600 ms window, a 200 ms start, near-immediate
  card reveals and a fast list cascade.

The route vocabulary is unchanged: `#<weekly|monthly>/<1|2|3>` maps to the three
tabs, so every deep link already sent to a driver still resolves to the same
section.

## Hard contracts — unchanged

No scoring in the browser. Status colours come from the snapshot's `status`,
never from a count. `INSUFFICIENT_DISTANCE` and `REPORT_NOT_READY` render their
full-screen states with **no tab bar and no Eco payload**; the four access
states and the loading skeleton are untouched. Ranking stays snapshot-driven and
`EXCLUDED`/`INCLUDED`/`UNKNOWN_DRIVER` never reach the UI. pl-PL formatting is
`js/format.js`, unchanged. No `<style>` element, no inline script, no new asset
and no external request.

## Layout

```
production/
  index.html          shell — byte-identical to v2 (and to v1)
  css/dashboard.css   the only stylesheet; the mobile block is appended, so
                      every desktop rule is at the byte offset it had in v2
  js/format.js        byte-identical to v2 (and to v1)
  js/render.js        pure snapshot → HTML; the desktop composition is
                      untouched and `renderMobile` is added beside it
  js/app.js           EcoApp.boot; slide engine, tablist keyboard, disclosures,
                      entrance motion, hash routing, plus the mobile branches

preview/
  preview.html        local harness: fixture selector + access-state selector
  fixtures/*.json     synthetic snapshots (never served)
  repo-owned/snapshot-source.js   verbatim copy, preview only

reference/
  mobile-390-*.png    the approved mobile page: all three tabs, both accordions
                      expanded, and the states that change its shape
  mobile-320-*.png    the narrowest supported phone
  desktop-*.png laptop-1280.png   carried over from v2 unchanged, because the
                      desktop presentation is unchanged
```

## Integration

1. Copy `production/*` into `assets/driver_eco_dashboard/` (5 files, same
   paths). `index.html` and `js/format.js` do not change.
2. Keep repo-owned `js/boot.js`, `js/capability-bootstrap.js`,
   `js/snapshot-source.js` byte-identical — this export does not touch them.
3. CSP: `font-src 'self' data:` is still required for the embedded Manrope, as
   in v2. Nothing new is requested.
4. **Acceptance contract, changed by this export:** the document must never
   scroll horizontally *and*, below 760 px, no element may scroll horizontally
   inside its own box either. The `.ed-scrollx` exception now applies only to
   the desktop page, and to the no-`matchMedia` fallback that still renders it.
5. Runtime boundary unchanged: `boot.js` calls `window.EcoApp.boot({source})` on
   success or `window.EcoRender.renderAccessState(code)` on failure.
6. Update the approved-design digests in the acceptance layer in the same move
   (`test_the_shipped_presentation_is_the_approved_design_export`).

## Local preview

From this folder:

```
python3 -m http.server 8731 --bind 127.0.0.1
# then open
http://127.0.0.1:8731/preview/preview.html?fixture=weekly_ranked_sparse#weekly/1
```

Narrow the window below 760 px to get the mobile page; the tab bar, the
category accordions and the day accordions are all live. Above 760 px the
desktop slide page behaves exactly as it did in v2.
