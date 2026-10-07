# driver-eco-dashboard-final-design-v2

Final production presentation for the Driver Eco Dashboard — the approved Claude Design result
("Eco Dashboard Strona"), with the three required contract corrections implemented **in the design
source** and re-exported. This is the implementation that ships; engineering copies the five
production files verbatim.

**Supersedes `../driver-eco-dashboard-final-design` (v1), which is left untouched for diffing.**
Copy from *this* folder.

What changed vs v1, and nothing else:

* **A1** — **superseded 2026-08-24 by owner decision (`18717cf`).** The per-day disclosure it
  introduced on slide 3 has been withdrawn: the daily table reports counts only, and the
  normalised coefficient (`coefficient_per_100km`) is presented once, for the period, on slide 2
  at the axes. Count and rate remain two separately reachable facts — which is what A1 was
  always about — but the rate is no longer restated per day. See `DESIGN_SOURCE_MANIFEST.md`
  § A1 for the withdrawn implementation.
* **A2** — period-over-period distance movement lives inside the existing distance KPI tile.
* **A3** — the dot navigation is a real tablist/tabpanel set with full keyboard support, and the
  axis reveal exposes proper disclosure semantics. Since the daily detail was withdrawn, the
  slide-3 pan track (`.ed-scrollx`) is itself focusable and named, so the columns beyond a 320px
  viewport stay reachable without a pointer — the day rows are no longer what carries that.

The mobile daily-table pan and the embedded Manrope are **intentionally retained**. See
`DESIGN_SOURCE_MANIFEST.md` for hashes, the per-correction record, the measured visual deviations
and the verification evidence.

## Layout

```
production/
  index.html          shell — 4 contract invariants preserved (capability-bootstrap first,
                      #eco-root[data-snapshot-url], boot.js last, lang/robots/referrer,
                      zero inline scripts). Byte-identical to v1.
  css/dashboard.css   the only stylesheet (CSP: no <style> elements); Telematics mark AND the
                      Manrope variable font (400–800, latin + latin-ext) embedded as data: URIs
  js/format.js        pl-PL formatting helpers (pure, UMD). Byte-identical to v1.
  js/render.js        pure snapshot → HTML string + renderAccessState/renderSkeleton (UMD)
  js/app.js           EcoApp.boot, slide engine, tablist keyboard, disclosures, entrance
                      motion, hash routing (UMD)

preview/
  preview.html        local harness: fixture selector + access-state selector (never served —
                      Worker denies /preview*)
  fixtures/*.json     synthetic snapshots (never served)
  repo-owned/snapshot-source.js   verbatim copy, preview only — production uses the repo's file

reference/
  desktop-1440.png  laptop-1280.png  mobile-390.png   slide 1 at the three required viewports
  desktop-1440-slide2.png  desktop-1440-slide2-details.png  desktop-1440-slide3.png
  mobile-390-slide2.png  mobile-390-slide3.png        material interactive states
  desktop-1440-slide3-day-expanded.png  mobile-390-slide3-day-expanded.png   A1
  desktop-1440-no-comparison.png                      A2 no-basis state
  desktop-1440-tab-focus.png                          A3 keyboard focus
```

## Integration (summary — full map in the repo's REPO_INTEGRATION_MAP.md)

1. Copy `production/*` into `assets/driver_eco_dashboard/` (5 files, same paths). `index.html` and
   `js/format.js` are unchanged from v1; `css/dashboard.css`, `js/render.js` and `js/app.js` change.
2. Keep repo-owned `js/boot.js`, `js/capability-bootstrap.js`, `js/snapshot-source.js`
   byte-identical — this export does not touch them.
3. ⚠ CSP: amend `font-src 'self'` → `font-src 'self' data:` in the Worker's security headers so the
   embedded Manrope loads (still the one and only requested delivery change). Without it the
   dashboard still works, in system-sans.
4. Acceptance contract: the **document** must never scroll horizontally; the daily-table container
   (`.ed-scrollx`) intentionally does, inside its own bounds, below 760 px. This export was verified
   against exactly that rule.
5. Runtime boundary unchanged: `boot.js` calls `window.EcoApp.boot({source})` on success or
   `window.EcoRender.renderAccessState(code)` on failure. Nothing else is required.
6. Keep the repo's generated `fixtures/` for tests; the copies here are preview conveniences.

## Local preview

From this folder:

```
python3 -m http.server 8731 --bind 127.0.0.1
# then open
http://127.0.0.1:8731/preview/preview.html?fixture=weekly_ranked_sparse#weekly/1
```

Fixture and access-state selectors are in the top bar. Slides: scroll to a slide boundary and give
one fresh wheel gesture (or swipe >70 px on touch), click the dots, focus a dot and use
Arrow/Home/End, or edit the hash (`#weekly/1..3`, `#monthly/1` when the snapshot has a monthly
period). On slide 2 activate a category name to reveal the band details; on slide 3 activate a day
to reveal its "na 100 km" rates.
