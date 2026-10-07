# Repository Implementation Guidance

How to reconcile this approved design with the actual repository. **This document deliberately does not claim to know the current implementation in detail** — only the repository can establish that.

---

## 0. Two rules that govern everything

> **Rule 1 — Do not silently revert.** When the approved design and the current implementation differ, do not fall back to the old UI. Investigate the repository constraints, preserve business and security invariants, and implement the approved behaviour. Escalate only on a material technical conflict.

> **Rule 2 — Concurrency is normal.** Multiple other coding agents may be working on this repository simultaneously. Unrelated HEAD movement, new commits, dirty files, or branch and worktree changes are **expected** and **MUST NOT** be treated as defects. Stay within the authorized task scope, preserve unrelated concurrent work, and **never** reset, revert, stash, clean or overwrite another agent's changes.

> **Authorization.** Git push, merge, deployment, production mutation, migrations and other irreversible operations require **separate explicit authorization**. The existence of this design handoff does not grant it.

---

## 1. Sequence

### Step 1 — Read the repository's own instructions first
Before anything else, read the repository's `CLAUDE.md`, `README`, `CONTRIBUTING`, and any `docs/` conventions. **The repository's own instructions outrank this package on anything architectural.** This package outranks the repository only on *what the product should look like and how it should behave*.

### Step 2 — Locate the current implementation for each Screen ID
Build a mapping from `SCREEN_CATALOG.md` IDs to real files: routes, templates, view functions, CSS, and any JS. Record it as a table in your working notes. Expect the upstream evidence's structure — a FastAPI app with per-module CSS extracted into `ui-source/` — but **verify**; the repository has moved on since that snapshot.

For each ID note: the route, the template, where the CSS lives, and whether any JS is involved.

### Step 3 — Establish the invariants before changing anything
Identify and write down, from the code:

- How dataset access is authorized (which datasets an account may list and open).
- How approved columns are determined, and where that list is enforced.
- Where the read-only guarantee is enforced (connection level, query level, or both).
- How permission flags (filtering enabled, export enabled) gate behaviour.
- The export thresholds and retention actually implemented.
- Whether `default_date_column` and `is_row_identifier` are configured for any dataset in production. **The upstream evidence says neither is configured** — some design capabilities are therefore latent and must degrade gracefully, not break.
- The real Eco Driving scoring implementation (`D-002`).

**These invariants are not negotiable by the design.** If an approved behaviour would weaken one, escalate.

### Step 4 — Compare behaviour against the contract
For each Screen ID, diff current behaviour against `PRODUCT_BEHAVIOR_CONTRACT.md` and `SCREEN_STATE_MATRIX.md`. Classify each gap:

| Class | Meaning | Example |
|---|---|---|
| **Presentational** | Same behaviour, different appearance | Panel radii, shadows, spacing |
| **Structural** | Same capability, different placement or hierarchy | The table moving from fifth block to first |
| **Interactional** | The capability exists but is reached differently | 42-field filter form → column menus |
| **New surface** | No current equivalent | Row detail panel, background-export states, saved views |
| **Backend-dependent** | Needs an API or schema change | Server-side saved views, per-account theme, distinct-value picker, value histogram |

### Step 5 — Propose the minimum coherent stages
Do **not** attempt the whole design in one change. Propose stages that each leave the product working. A defensible order:

1. **Tokens and shell.** Introduce the token layer; rebuild the app bar, client context bar and full-width working area. Removes the 1280 px cap. Touches every screen but changes no behaviour.
2. **Table structure.** Move the table to first position on the row sheet; sticky header; sticky identity columns; density; the toolbar; the filtered-vs-total counter. Keep the existing filter form working, just relocated, so nothing is lost mid-flight.
3. **Column-centric controls.** Column header menus for sort and filter; the active-filter chip strip; the filter panel; retire the 42-field form. This is the largest interactional change and the point of the redesign.
4. **Column management.** Visibility, order, widths, sets, and URL/server persistence per `D-007`.
5. **Row detail panel** (`D-006`), including graceful behaviour where no row identifier is configured.
6. **Export.** The export panel with its three scopes and pre-commit path notice; then the four background-export states and the app-bar indicator.
7. **Empty, loading and error states** across Database Explorer.
8. **Report Explorer.** Library, then the detail page with preview, files and history.
9. **Eco Driving.** Period model and ranking first; then the drill-down. **Read the real scoring logic first** (`D-002`).
10. **Responsive.** 1024 and 768 behaviour, the advisory below 768.
11. **Artifact Explorer.** Visual modernization only.

Stages 1–2 are prerequisites for everything else. Stages 8–11 are independent of each other.

### Step 6 — Avoid architectural rewrites
- Nothing in this package requires a SPA, a React migration, or a client-side router.
- Equally, the current server-rendered architecture is **not** a reason to weaken the approved UX. Column menus, chips, a row panel, drag-reordering and range copy all need some JavaScript; adding it is expected. Adding a framework is not.
- **URL-first state (`D-007`) is deliberately friendly to server rendering.** Every view is a URL; the existing product already works this way, and that is a strength to keep.
- Prefer progressive enhancement: sort and filter should work as form submissions with JS off, and become in-place updates with JS on.

### Step 7 — Verify with deterministic evidence
For each stage:

- Verify against `IMPLEMENTATION_ACCEPTANCE_CRITERIA.md` — the criteria are written to be observable.
- Capture browser evidence at **1920 × 1080** and compare against `reference/screens/<SCREEN-ID>.png`. Match structure, hierarchy, density and token usage — not pixels.
- Verify both themes by toggling `prefers-color-scheme`.
- Verify **states**, not just the happy path: loading, empty-after-filters, error, permission-denied.
- Verify keyboard paths from `INTERACTION_SPEC.md` §2 and focus return from §3.
- Verify the responsive breakpoints at 1024, 768 and 390 px.
- Add regression tests for the behaviours in `TRACEABILITY_MATRIX.md` — those are the ones most likely to be lost.

---

## 2. Capability preservation checklist

The redesign changes *how* these are exposed. **None may be lost.** Verify each explicitly.

| Capability | Where it now lives |
|---|---|
| Dataset access restrictions | `DB-001`: only assigned datasets listed; forbidden deep links produce a permission state |
| Approved-columns-only visibility | `DB-003`: column count stated in the context bar; the column panel lists only approved columns |
| Global text search | `DB-003` toolbar, with the searched-column count in the placeholder |
| Per-column filters | `DB-005` column menus + `DB-003` filter panel |
| Sorting | `DB-005` column menu; state shown as a caret **and** in words |
| Selectable visible columns | `DB-008` |
| Comfortable / compact density | `DB-003` toolbar segmented control |
| Pagination and page size | `DB-003` footer, 25–500 |
| Export | `DB-009` panel; `DB-007` background states |
| Current-filter visibility and reset | Chips with `×` + `Wyczyść wszystkie` + count badge |
| Read-only semantics | `TYLKO ODCZYT` badge on every row-sheet screen; no editing affordance anywhere |
| Export row thresholds and retention | Stated in `DB-009` before commit and in `DB-007` per row |
| Eco Driving period model | `ECO-001` month stepper + week cards + basis line |
| Eco Driving reconciliation | **Deliberately removed** (`D-016`) — do not restore |
| Per-row lineage badge | **Consolidated** into the context bar (`D-016`) — do not restore per row |
| URL-linkable view state | Preserved and extended (`D-007`) |
| Background export → artifact → report | `DB-007` → Report Explorer type `Eksporty danych` |

## 3. Latent capabilities — must degrade, not break

The upstream evidence is explicit that in current production:

- **No `default_date_column` is configured for any dataset.** Therefore: date presets in the filter panel are **absent** for such datasets; the default sort falls back to the primary approved column ascending; and the toolbar states the actual sort. The design **MUST NOT** assume presets exist.
- **No `is_row_identifier` column is configured.** Therefore: the row detail panel (`D-006`) must open keyed by internal row position, with an approved identifying column shown in its header if one exists. It **MUST NOT** require the configuration.

Some screenshots in the upstream evidence demonstrate these capabilities deliberately. **Do not read a screenshot as proof that a configuration exists in production.**

## 4. What is a design source of truth, and what is not

| Authoritative for | Source |
|---|---|
| Layout, hierarchy, density, spacing, colour, typography, component states | This package + `reference/` |
| Interaction semantics, keyboard, focus, dismissal, state transitions | `INTERACTION_SPEC.md` |
| Which screens and states must exist | `SCREEN_CATALOG.md`, `SCREEN_STATE_MATRIX.md` |
| Polish UI strings | `COPY_AND_TERMINOLOGY.md` |
| Application architecture, framework, module layout | **Repository** |
| Backend, API shape, persistence, migrations | **Repository** |
| Security, authorization, RBAC, read-only enforcement | **Repository** |
| Business rules, including all Eco Driving scoring | **Repository** (`D-002`) |
| Infrastructure, deployment | **Repository** |

`reference/prototype-source/` is labelled `REFERENCE_PROTOTYPE_ONLY`. It preserves layout intent, proportions, visual composition and interaction examples. **It must not be copied into production.** It is a design-component prototype, not application code: it has no data layer, no authorization, and no tests.

## 5. Escalate rather than decide

Stop and ask the owner if you find any of the triggers listed in `UNRESOLVED_DECISIONS.md` § "Escalation triggers" — most importantly, if the repository's real scoring logic has a **different structure** from the one specified, or if a security invariant contradicts an approved behaviour.

Do not resolve a material product question by choosing what is easiest to implement.
