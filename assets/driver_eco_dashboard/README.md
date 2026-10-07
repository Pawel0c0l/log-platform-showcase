# Driver Eco Dashboard — frontend

Static, framework-free driver-facing dashboard. Its only business input is one
`driver_eco_dashboard_snapshot` **schema v1** document for **one** driver,
produced by `jobs/ecodriving_dashboard/`.

Contract and invariants: `docs/28_driver_eco_dashboard_v1_snapshot_foundation.md`.
Design authority:
`design-handoffs/driver-eco-dashboard/driver-eco-dashboard-final-design-v3/`.

Two approved presentations share these files, separated by one width. At
**>= 760 px** the page is the v2 desktop design: three slides, the gated
slide-boundary gesture and the vertical dot tablist. At **<= 759 px** it is the
v3 mobile design ("Zakladki", variant 1a): three tabs — Wynik / Wykroczenia /
Dni — in a bottom bar, free scrolling inside a tab, accordions in place and a
vertical list of day cards. `renderDashboard` emits one tree or the other and
never both; the route (`#<weekly|monthly>/<1|2|3>`) is the same in each, so a
deep link resolves to the same section on either.

**The presentation layer is design-owned.** `index.html`, `css/dashboard.css`,
`js/format.js`, `js/render.js` and `js/app.js` are copied verbatim from that
package's `production/` directory and must stay byte-identical to it. The
package is a delivered input rather than a repository dependency, so their
SHA-256 digests are recorded in-tree as well —
`test_the_shipped_presentation_is_the_approved_design_export` fails the moment
one of the five is edited here, with or without the package present.
Repository-side corrections belong in the acceptance layer, in the repo-owned
files below or in the delivery Worker, never in a local fork of the design; if
a product or security contract genuinely cannot be met without changing one of
the five, the design source is corrected and re-exported.

The integration boundary is two functions — `window.EcoApp.boot({ source })` and
`window.EcoRender.renderAccessState(code)` — plus the eight asset paths the
delivery Worker allowlists. Everything below them (DOM, CSS, charts, copy,
section order) belongs to the design.

## Layout

| Path | Role |
|---|---|
| `index.html` | production shell. Reads the snapshot URL from `#eco-root[data-snapshot-url]`; contains no fixture selector. |
| `preview.html` | development/verification harness with a synthetic-state selector. Not part of the product. |
| `css/dashboard.css` | the whole design system: tokens, components, breakpoints, forced-colors and reduced-motion. |
| `js/format.js` | pl-PL formatting and layout maths. Never scores. |
| `js/render.js` | snapshot → HTML string. Pure, DOM-free, no business logic. |
| `js/snapshot-source.js` | the input boundary: worker / fixture / static sources, structural validation, HTTP status → access state. |
| `js/app.js` | slide transitions (desktop) and tab switching (mobile), hash routing (`#<weekly\|monthly>/<1\|2\|3>`), tablist keyboard handling, axis, category and day disclosures. |
| `js/capability-bootstrap.js` | First script in `<head>`: captures the capability fragment and clears it from the URL and history, independently of the rest of the bundle. |
| `js/boot.js` | Capability exchange and dashboard start. No inline script exists anywhere, so the delivery CSP can be `script-src 'self'`. |
| `fixtures/*.json` | 16 synthetic snapshots, generated from `ops/tests_manual/eco_dashboard_fixtures.py`. |

Plain `<script>` tags, no build step, no bundler, no external request — the page
makes exactly one network call, for its own snapshot.

## Running it locally

The page uses `fetch()`, so it needs an origin (a `file://` open cannot load the
fixtures):

```bash
cd assets/driver_eco_dashboard
python3 -m http.server 8731 --bind 127.0.0.1
# then open http://127.0.0.1:8731/preview.html
```

`preview.html?fixture=<name>#<weekly|monthly>/<1|2|3>` renders any of the 16
states directly, on any of the three slides. All fixture data is invented; no production value,
identity or token appears anywhere in this directory.

Regenerate the fixtures after a snapshot-builder change:

```bash
python3 ops/tests_manual/eco_dashboard_fixtures.py --write-json assets/driver_eco_dashboard/fixtures
```

`ops/tests_manual/test_driver_eco_dashboard_frontend.py` fails if they drift.

## How the real snapshot arrives

`js/capability-bootstrap.js` is the **first script in `<head>`**. If the URL
carries a capability fragment (`#k=<secret>`) it captures the value and clears
the fragment with `history.replaceState` immediately — before the stylesheet,
before the renderer, before anything that could fail — so cleanup never depends
on the rest of the bundle loading. The value is handed on through a one-shot
accessor that nulls itself on first read.

`js/boot.js` then runs last on `index.html`, consumes that handover, exchanges
the value for an `HttpOnly` session cookie over a same-origin
`POST /api/session`, drops it, and starts the dashboard. With no capability it
starts the dashboard immediately and an existing session (or its absence)
decides what renders.

`createWorkerSource` then fetches `/api/snapshot` with
`credentials: "same-origin"` and **no parameters** — the Worker resolves which
snapshot the session is entitled to. The capability never becomes a field on the
page, in the DOM, in storage, in a log or in the URL the renderer sees.

The delivery boundary itself lives in `delivery/driver_eco_dashboard/`.

## Rules this code must keep

- **Status is never derived from a raw count.** Every green/yellow/red comes
  from the snapshot's `status`, which the host derived through
  `raw violations + qualifying exposure → coefficient → existing scoring bucket
  → points vs points_max`. No selector keys on a number; no renderer branches on
  `count`.
- **No scoring in the browser.** No band table, no coefficient arithmetic, no
  rounding of a business value. The only client-side maths is layout maths.
- **Fail closed.** A period below the 100 km reporting-period gate renders only
  `INSUFFICIENT_DISTANCE`; an incomplete score renders only `REPORT_NOT_READY`.
  Neither leaves any Eco value in the DOM, in an `aria-label` or in page source.
- **No daily distance gate.** Once a period qualifies, a 1–99 km day is an
  ordinary Detailed row; `0 km` is the neutral no-driving state.
- **No aggregate day status.** Days carry per-category statuses only.
- **Ranking is snapshot-driven.** `ranking_state` / `ranking_transition` decide
  what renders; a rank, a rank delta or a group share is never invented, and the
  internal words `EXCLUDED` / `INCLUDED` / `UNKNOWN_DRIVER` never appear.
- **No DOCUMENT-level horizontal scrolling at any width**, including 320 px and
  400 % zoom, and no ordinary component may escape the viewport. **Below 760 px
  nothing pans sideways at all**: the mobile design replaced the wide daily
  table with a vertical list of day cards, so the one surface that used to be
  allowed its own horizontal scroll is not rendered there. The desktop page
  keeps that exception for the daily table (`.ed-scrollx`), which carries a
  column per event category; its own box stays inside the viewport.
  `test_driver_eco_dashboard_viewport_sweep.py` proves the document rule at
  every width, asserts that nothing pans below the boundary, and fails if any
  *other* element starts scrolling sideways.
- **Light only.** `prefers-color-scheme: dark` is deliberately not implemented in
  V1 (the status palette would need re-derivation and re-verification).

## Typography note

The design specifies Manrope. It is **embedded** in `css/dashboard.css` as a
`data:` URI (variable weight 400–800, latin + latin-ext) rather than fetched: a
third-party font call from a driver's browser would break the one-fetch privacy
posture, and a `data:` font makes no request at all. That is the only reason the
delivery CSP carries `font-src 'self' data:` — no origin is allowed, and
`test_content_security_policy_is_restrictive` fails if that directive ever grows
further. Tabular figures are enforced through `font-variant-numeric`.
