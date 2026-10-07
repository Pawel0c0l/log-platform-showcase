# Reference exports

## `REFERENCE_PROTOTYPE_ONLY`

Everything in this folder is a **visual and interaction reference**. None of it is production-ready source code. It has no data layer, no authorization, no error handling and no tests.

Its purpose is to preserve, unambiguously:

- layout and proportions,
- visual hierarchy and density,
- component composition,
- interaction examples and state appearance.

**Claude Code must not copy these files into the production repository.** The repository remains authoritative for architecture, backend, security, authorization, business rules, data access, persistence and infrastructure. See `../REPOSITORY_IMPLEMENTATION_GUIDANCE.md` §4.

---

## `screens/` — static references by Screen ID

Captured at the authored frame size, in the theme the Screen ID denotes. Compare **structure, hierarchy, density and token usage** — not pixels.

| File | Screen ID | Size | Theme |
|---|---|---|---|
| `DB-001-dataset-catalogue-light.png` | `DB-001` | 1920 × 700 | light |
| `DB-002-dataset-catalogue-dark.png` | `DB-002` | 1920 × 700 | dark |
| `DB-003-row-sheet-light.png` | `DB-003` | 1920 × 1080 | light |
| `DB-004-row-sheet-dark.png` | `DB-004` | 1920 × 1080 | dark |
| `DB-005-column-menu-numeric.png` | `DB-005` | 620 × 740 | light |
| `DB-006-row-detail-panel.png` | `DB-006` | 1920 × 1080 | light |
| `DB-007-background-exports.png` | `DB-007` | 1920 × 760 | light |
| `DB-008-column-panel.png` | `DB-008` | 400 × 740 | light |
| `DB-010-empty-after-filters.png` | `DB-010` | 711 × 360 | light |
| `DB-011-data-source-error.png` | `DB-011` | 711 × 362 | light |
| `REP-001-library-light-filtered.png` | `REP-001` | 1920 × 1080 | light |
| `REP-002-library-dark.png` | `REP-002` | 1920 × 1080 | dark |
| `REP-003-report-detail.png` | `REP-003` | 1920 × 1320 | light |
| `ECO-001-ranking-light-rate.png` | `ECO-001` | 1920 × 1080 | light, `/ 100 km` mode |
| `ECO-002-ranking-dark-sum.png` | `ECO-002` | 1920 × 1080 | dark, `Σ suma` mode |
| `ECO-003-driver-detail.png` | `ECO-003` | 1920 × 2020 | light |
| `ART-001-artifact-explorer.png` | `ART-001` | 1920 × 860 | dark |
| `RSP-001-tablet-landscape-1024.png` | `RSP-001` | 1024 × 820 | light |
| `RSP-002-tablet-portrait-768-drawer.png` | `RSP-002` | 768 × 820 | light |
| `RSP-003-below-768-advisory.png` | `RSP-003` | 390 × 820 | light |

**Screen IDs without a dedicated PNG:**

| ID | Where to see it |
|---|---|
| `SHL-001`, `SHL-002`, `SHL-003` | The shell chrome at the top of `DB-003`, `DB-004`; the theme switcher is in the app bar of both |
| `DB-009` | Export panel — in the interactive file `Log Platform - Kierunki v1.dc.html`, direction `1b`, section *Ekran 3* |
| `REP-004` | Three state frames in `Report Explorer.dc.html`, section *REP-004* |
| `ASIS-001`, `ASIS-002` | `Baseline - Log Platform As-Is.dc.html` |

Dark-theme counterparts of `DB-005`, `DB-008`, `DB-010` and `DB-011` exist in the interactive file (`Arkusz`, section *Sterowanie i stany brzegowe*, both themes) and are specified in `../DESIGN_TOKENS.md`; they were not exported separately because the token mapping is one-to-one.

## `prototype-source/` — the interactive reference

| File | Contains |
|---|---|
| `Arkusz - motyw jasny i ciemny.dc.html` | `SHL-001`–`SHL-003`, `DB-001`–`DB-005`, `DB-008`, `DB-010`, `DB-011`, both themes, plus the 18-token light↔dark map |
| `Report Explorer.dc.html` | `REP-001`–`REP-004` |
| `Eco Driving - ranking i kierowca.dc.html` | `ECO-001`–`ECO-003` |
| `Uzupelnienia - wiersz, eksporty, artefakty, widoki waskie.dc.html` | `DB-006`, `DB-007`, `ART-001`, `RSP-001`–`RSP-003` |
| `Log Platform - Kierunki v1.dc.html` | The two iteration-1 directions. **`1b Arkusz` was approved; `1a Warsztat` was rejected.** Retained only to explain the decision, and because `DB-009` (export panel) was drawn here. |
| `Baseline - Log Platform As-Is.dc.html` | `ASIS-001`, `ASIS-002` — the current product, rebuilt from the extracted production CSS. **Not a target.** |
| `support.js` | Runtime required by the files above. Keep it in the same folder. |

### How to open them

Open any `.dc.html` directly in a browser, with `support.js` in the same folder. Each file is one long canvas of framed screens with a heading above each frame; scroll and zoom out to see the set. Each frame carries a `data-screen-label` attribute holding its Screen ID, so `document.querySelector('[data-screen-label="DB-003"]')` finds it.

Every frame is real HTML: text is selectable, tables scroll, and the layout reflows exactly as specified. What is **not** live: nothing is wired to data, no control actually filters or sorts, and no navigation happens. Interaction *states* are drawn as separate frames rather than being triggerable.

### Reading the files

Each file ends with a section of design notes stating what a decision optimizes for and what it trades away. Those notes are commentary, not specification — where they and the `.md` documents disagree, **the `.md` documents win.**

## What is not here

| Not included | Reason |
|---|---|
| PDF | The PNG set plus the interactive files cover review; a third artifact would drift |
| PowerPoint | No presentation use case was stated |
| Video / animation captures | The approved design has four small motions, all specified in `../INTERACTION_SPEC.md` §9 |
| Figma or design-tool files | The design was authored as HTML; there is no separate design-tool source |
| Font files | IBM Plex Sans / Mono are loaded from Google Fonts in the reference files. **Self-host them in production** — see `../ASSET_MANIFEST.md` §1 |
