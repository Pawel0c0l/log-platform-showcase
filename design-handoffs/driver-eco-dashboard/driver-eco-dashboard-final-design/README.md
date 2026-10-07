# driver-eco-dashboard-final-design

> **SUPERSEDED — do not copy from this folder.** The corrected final export is
> `../driver-eco-dashboard-final-design-v2`, which adds the A1 daily coefficient, the A2
> period distance movement and the A3 tab/disclosure semantics to this same approved design.
> This package is kept unmodified only so the two can be diffed.

Final production presentation for the Driver Eco Dashboard — a 1:1 source-code export of the
approved Claude Design result ("Eco Dashboard Strona"). This is the implementation that ships;
engineering copies the five production files verbatim. See `DESIGN_SOURCE_MANIFEST.md` for
hashes, tested fixtures/viewports/behaviours and the (three, cosmetic) known deviations.

## Layout

```
production/
  index.html          shell — 4 contract invariants preserved (capability-bootstrap first,
                      #eco-root[data-snapshot-url], boot.js last, lang/robots/referrer,
                      zero inline scripts)
  css/dashboard.css   the only stylesheet (CSP: no <style> elements); Telematics mark AND the
                      Manrope variable font (400–800, latin + latin-ext) embedded as data: URIs
  js/format.js        pl-PL formatting helpers (pure, UMD)
  js/render.js        pure snapshot → HTML string + renderAccessState/renderSkeleton (UMD)
  js/app.js           EcoApp.boot, slide engine, entrance motion, hash routing (UMD)

preview/
  preview.html        local harness: fixture selector + access-state selector (never served —
                      Worker denies /preview*)
  fixtures/*.json     synthetic snapshots (never served)
  repo-owned/snapshot-source.js   verbatim copy, preview only — production uses the repo's file

reference/
  desktop-1440.png  laptop-1280.png  mobile-390.png   slide 1 at the three required viewports
  desktop-1440-slide2.png  desktop-1440-slide2-details.png  desktop-1440-slide3.png
  mobile-390-slide2.png  mobile-390-slide3.png        material interactive/expanded states
```

## Integration (summary — full map in the repo's REPO_INTEGRATION_MAP.md)

1. Copy `production/*` into `assets/driver_eco_dashboard/` (5 files, same paths).
2. Keep repo-owned `js/boot.js`, `js/capability-bootstrap.js`, `js/snapshot-source.js`
   byte-identical — this export does not touch them.
3. ⚠ CSP: amend `font-src 'self'` → `font-src 'self' data:` in the Worker's security headers
   so the embedded Manrope loads (the one requested delivery change; everything else fits the
   existing allowlist and policy). Without it the dashboard still works, in system-sans.
4. Runtime boundary: `boot.js` calls `window.EcoApp.boot({source})` on success or
   `window.EcoRender.renderAccessState(code)` on failure. Nothing else is required.
5. Keep the repo's generated `fixtures/` for tests; the copies here are preview conveniences.

## Local preview

From this folder:

```
python3 -m http.server 8731 --bind 127.0.0.1
# then open
http://127.0.0.1:8731/preview/preview.html?fixture=weekly_ranked_sparse#weekly/1
```

Fixture and access-state selectors are in the top bar. Slides: scroll to a slide boundary and
give one fresh wheel gesture (or swipe >70 px on touch), click the dots, or edit the hash
(`#weekly/1..3`, `#monthly/1` when the snapshot has a monthly period).
