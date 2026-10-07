# DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF

**Classification:** `DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF_IMPLEMENTATION_READY`
**Package version:** `v3.1 · 2026-08-18` — single effective version (v1.0 and v2.0 withdrawn)
**Ship together with:** `eco-driving-as-is-audit-handoff/` (required companion, `v1.0`)

## Start here

1. `design-handoff/FINAL_HANDOFF_MANIFEST.md` — authority map: which artifact decides what.
2. `design-handoff/CLAUDE_CODE_IMPLEMENTATION_BRIEF.md` — the complete implementation contract.
3. `Eco Driving Dashboard.dc.html` — canonical Dashboard visual source. Open in a browser; the dark bar switches period, view and 9 data states.
4. `Eco Driving Design System.dc.html` — canonical Design System visual source (tokens, component anatomy).

`support.js` must sit next to the two `.dc.html` files for them to run. Both open offline (fonts fall back to system).

## Contents

```
design-handoff/                       13 Markdown contracts, all v3.1
Eco Driving Dashboard.dc.html         canonical dashboard (VISUAL authority)
Eco Driving Design System.dc.html     canonical design system (VISUAL authority)
support.js                            runtime required by the two .dc.html files
archive-non-authoritative/            superseded drafts — provenance only, DO NOT implement from
```

## The one rule that matters most

Every green / yellow / red state comes from:

```
raw violations + qualifying exposure/distance
  → Eco violation coefficient
  → existing Eco scoring bucket/threshold
  → points vs points_max
  → semantic colour
```

Never from a raw event count, and never aggregated into a whole-day colour. Two identical counts may legitimately differ in colour.

## Scope

Design and implementation handoff only. No production code, database, template, schedule, migration, commit, e-mail or Cloudflare resource was created or modified. All values in all visual artifacts are synthetic; no production PII.


## v3.1 qualification correction

The 100 km qualification threshold is **reporting-period-only**. A closed period below 100 km renders a minimal `INSUFFICIENT_DISTANCE` state with no Eco data. There is no per-day 50 km or 100 km gate; days below 100 km remain visible in Detailed after the whole period qualifies, with coefficients/statuses based on actual daily distance.
