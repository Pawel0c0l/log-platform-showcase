# DESIGN_SOURCE_MANIFEST

**Statement of authority:** the CURRENT Claude Design result ("Eco Dashboard Strona" — 3-slide
page: Twój wynik / Wykroczenia na osi / Podsumowanie dzienne; green/alpha visual language;
slide-boundary scroll transitions; once-per-visit entrance motion; 1180/980/760 px responsive
breakpoints) is the sole visual and interaction source of truth for this export. The old
production frontend contributed **no** presentation code. The engineering template contract was
used only for: snapshot input shape, required product states, security boundary, production
filenames, CSP restrictions, accessibility requirements and integration entrypoints.

## Production files (exactly five)

| File | SHA-256 |
|---|---|
| `production/index.html` | `d50444ae62e8067d8d29111741a6c14dd0129d3b7fb346682ef4bf065b476ca7` |
| `production/css/dashboard.css` | `80dc5d02775455fd2855d666d41b2388e03e10a9928fcaf231eb2bc6a5134dab` |
| `production/js/format.js` | `6a4b834cb26d2acf5285b5f3f79f3989f5dc0e429e6d690509476c56be3aa998` |
| `production/js/render.js` | `36f3e217e90f426dc67a77f91f5f3e7b24104bf0d53d23445fced0b7cf19c78f` |
| `production/js/app.js` | `4d062c0af41afaa166347f212ad2fdad498681061946310f789107500fa6f8cf` |

`js/capability-bootstrap.js`, `js/boot.js`, `js/snapshot-source.js` are repo-owned and are NOT
part of this export (index.html references them; engineering retains its own byte-exact copies).
`preview/repo-owned/snapshot-source.js` is a verbatim copy included ONLY so the local preview runs.

Boundary honoured: `window.EcoApp.boot({ source })` and
`window.EcoRender.renderAccessState(code)`; no other integration surface. Every dynamic
number/state comes from the snapshot document — no hardcoded fixture values. CSP-clean: no inline
`<script>`, no `<style>` elements, no external fonts/images/CDNs — zero external requests.
The logo and the Manrope typeface (variable 400–800, latin + latin-ext) are embedded as
`data:` URIs inside `dashboard.css`, so the approved typography renders 1:1 everywhere.
⚠ The embedded font requires a one-line CSP amendment: `font-src 'self' data:`
(today's policy is `font-src 'self'`). This is the single requested delivery change.

## Fixtures tested

weekly_ranked_sparse (primary — all 3 slides, all viewports) · weekly_ranked_dangerous (slides
1–2) · weekly_not_ranked_by_configuration · weekly_no_comparison · monthly_31_days (slides 1 and
3, monthly period) · weekly_insufficient_distance · weekly_report_not_ready · access state
LINK_EXPIRED (renderAccessState; INVALID_LINK / SNAPSHOT_UNAVAILABLE / SERVICE_UNAVAILABLE share
the same panel component).

Sparse distribution: absent rating buckets render nothing — no zeros, nulls, dashes, empty
segments or legend items (verified on weekly_ranked_sparse: 2-band bar; weekly_ranked_dangerous:
3-band bar). Ranking surfaces absent outside RANKED (verified on
weekly_not_ranked_by_configuration). No fabricated comparison when `comparison: null`
("pierwszy zamknięty okres — brak porównania" chip, no delta, no previous marker).

## Viewports tested

320 · 390 · 768 · 1280 · 1440 px. Responsive transformations follow the approved breakpoints
(1179/979/759/419 max-width bands): tiles collapse 2→1 columns, axes go vertical with
tap-to-reveal segment details, the daily table pans sideways inside its card (<760 px — the only
sanctioned horizontal scroll), dot navigation compacts.

## Interactive behaviours verified

Slide navigation (fresh wheel gesture >180 ms gap at slide boundary; >70 px swipe started at
boundary; dot clicks; hash routes `#<weekly|monthly>/<1|2|3>` via replaceState, no pushState) ·
axis-row hover reveals segment labels/points on desktop, tap toggles on touch (`data-action="axtap"`)
· card hover lift (@media(hover:hover)) · retry action on access states · period switch via hash
when both periods exist (weekly wins by default).

## Motion behaviours verified

Number count-ups (final values in DOM from first frame; aria/sr text never animated) · ring draw
with threshold-driven colour during growth · bar growth (`data-aw`/`data-ah`/`data-ax`) ·
section fades (`.ed-fade`) and cascading table rows (`.ed-casc`) · once per slide per page load ·
800 ms slide transition + lock · `prefers-reduced-motion: reduce` disables all motion while
keeping final values visible · rAF-failsafe (`finishAllVisible`) guarantees visibility in
throttled/background tabs.

## Reference screenshots (final exported implementation, weekly_ranked_sparse)

`reference/desktop-1440.png` · `reference/laptop-1280.png` · `reference/mobile-390.png`
(slide 1 at the three required viewports), plus material states:
`desktop-1440-slide2.png`, `desktop-1440-slide2-details.png` (axis segment details revealed),
`desktop-1440-slide3.png`, `mobile-390-slide2.png`, `mobile-390-slide3.png` (sideways-pannable
daily table). Captured from the exported production `render.js` + `dashboard.css` with Manrope
available.

## Known deviations from the current Design

1. **Font delivery mechanism (not appearance):** the Design session loads Manrope from Google
   Fonts; production embeds the identical Manrope variable font (400–800, latin + latin-ext) as
   `data:` URIs in `dashboard.css` — typography is 1:1. Requires the CSP amendment
   `font-src 'self' data:` (one line, Worker-owned); without it browsers fall back to the
   system-sans stack.
2. **Logo asset:** the Design used `assets/telematics-mark.png`; production embeds the same mark as
   a `data:` URI inside `dashboard.css` (CSP/allowlist — no image files).
3. **Data:** production renders snapshot values; the Design's demo copy differs wherever demo data
   differed from the engineering fixtures. Composition, geometry, colours and motion are 1:1.

No behaviour was downgraded. Deviations of the approved design from
`CLAUDE_DESIGN_TEMPLATE_SPEC.md` (product decisions: no period switcher, slide routing
`#<period>/<1|2|3>`, boundary scroll transitions, mobile table pan, presentational client-side
arithmetic, axis-based ladder visualisation, host-supplied "Razem" row) are catalogued with test
consequences in the repo handoff notes (`HANDOFF_REPO_NOTES.md`, §3).
