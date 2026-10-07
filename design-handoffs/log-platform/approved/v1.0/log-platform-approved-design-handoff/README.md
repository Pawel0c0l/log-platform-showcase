# Log Platform — Approved Design · Engineering Handoff

**Package version:** `v1.0 · 2026-08-18`
**Approved design snapshot:** Claude Design project "Database Explorer handoff"
**Source-of-truth design files:** see `SCREEN_CATALOG.md`
**Upstream as-is evidence:** handoff snapshot `20260817T2000Z-b0afb2a`

---

## What this package is

This is the **engineering handoff for a design that has already been approved** by the product owner — visually and product-wise. It is not a proposal, not a set of options, and not a redesign brief.

Its job is to make every implementation-relevant UI, UX, interaction, responsive and state decision **explicit**, so that a coding agent working against the real production repository does not have to guess.

The design was approved in five iterations:

| Iteration | Scope | Outcome |
|---|---|---|
| 1 | Two shared-shell / Database Explorer directions (`1a Warsztat`, `1b Arkusz`) | Owner selected **1b Arkusz** |
| 2 | `1b` rendered as light + dark themes driven by `prefers-color-scheme` | Approved |
| 3 | Eco Driving ranking + driver drill-down | Approved |
| 4 | Report Explorer | Approved |
| 5 | Row detail, background exports, Artifact Explorer, narrow/tablet views | Approved |

## The rule that matters most

> **When the approved design and the current repository implementation differ, do not silently revert to the old UI.** Investigate the repository constraints, preserve business/security invariants, and implement the approved product behaviour unless a material technical conflict requires owner escalation.

## Concurrency

The production repository may be changing concurrently because other agents are working on it. **Unrelated HEAD movement, new commits, dirty files, or branch/worktree changes are expected and must not be treated as defects.** See `REPOSITORY_IMPLEMENTATION_GUIDANCE.md` for the full concurrency and authorization rules.

## What is in scope

| Module | Status | Screens |
|---|---|---|
| Shared shell (nav, client context, theme) | Approved | `SHL-001`–`SHL-003` |
| Database Explorer | Approved | `DB-001`–`DB-011` |
| Report Explorer | Approved | `REP-001`–`REP-004` |
| Functional Analytics — Eco Driving | Approved (scoring logic **unresolved**, see below) | `ECO-001`–`ECO-003` |
| Artifact Explorer | Approved — visual modernization only | `ART-001` |
| Responsive / narrow | Approved | `RSP-001`–`RSP-003` |
| As-is baseline (reference only) | Not a target | `ASIS-001`–`ASIS-002` |

**Out of scope for this package:** the eight Administration sections (visual modernization deferred; they inherit shell + table specs but have no approved screens), authentication screens, and any backend/API design.

## Areas with explicitly unresolved decisions

There is exactly **one** material unresolved area:

- **Eco Driving scoring logic** (`D-002`). Every threshold, point weight and rating band shown in `ECO-001`–`ECO-003` is **placeholder data invented for layout purposes**. The owner confirmed the numbers are not real. The *structure* (N metrics → per-metric threshold → points awarded → points lost → share of total loss) is approved; the *values and formula* must be read from the repository. **Do not implement the numbers in the design files.** See `UNRESOLVED_DECISIONS.md`.

Everything else material is resolved. `UNRESOLVED_DECISIONS.md` records all 14 decisions the owner made, with dates.

## How Claude Code should use this package

Read in this order:

1. **`README.md`** (this file) — orientation and the two governing rules.
2. **`REPOSITORY_IMPLEMENTATION_GUIDANCE.md`** — how to reconcile design with the actual repo; read *before* touching code.
3. **`PRODUCT_BEHAVIOR_CONTRACT.md`** — the behavioural contract. This is the primary specification.
4. **`SCREEN_CATALOG.md`** + **`SCREEN_STATE_MATRIX.md`** — what screens and states must exist.
5. **`TABLE_AND_DATA_GRID_SPEC.md`** — the Database Explorer grid, in detail. The single densest surface.
6. **`ECO_DRIVING_ANALYTICS_SPEC.md`** — the relational drill-down.
7. **`INTERACTION_SPEC.md`**, **`RESPONSIVE_SPEC.md`**, **`ACCESSIBILITY_SPEC.md`** — cross-cutting semantics.
8. **`DESIGN_TOKENS.md`** + **`COMPONENT_CATALOG.md`** — the visual system contract.
9. **`COPY_AND_TERMINOLOGY.md`** — canonical Polish UI vocabulary. Do not invent alternative names.
10. **`IMPLEMENTATION_ACCEPTANCE_CRITERIA.md`** — observable criteria; use as the definition of done.
11. **`TRACEABILITY_MATRIX.md`** — requirement → screen → component → spec → criterion.

## Design source of truth vs. repository implementation

- **This package defines WHAT the finished product must look like and how it must behave.**
- **The repository remains authoritative for** application architecture, backend, security, authorization, business rules, data access, persistence and infrastructure.

This package deliberately does **not** prescribe a frontend framework, component architecture, state-management library, or rendering strategy. The current implementation being server-rendered FastAPI is **not** a reason to weaken the approved UX; equally, nothing here requires a SPA or a React migration. Choose the smallest architecture that delivers the approved behaviour.

## Reference exports

`reference/` contains **visual and interaction references, not production source**. See `reference/README.md`.

- `reference/screens/` — 20 PNG exports named by Screen ID, captured at authored frame size.
- `reference/prototype-source/` — the six design files plus `support.js`. These are **self-contained HTML documents that open directly in a browser** and are therefore also the *interactive* reference — there is no separate `html/` build. **Labelled `REFERENCE_PROTOTYPE_ONLY`: must not be copied into production.** Their purpose is to preserve layout intent, proportions, visual composition and interaction examples.

No PDF is included. The PNG set plus the interactive files cover review, and a PDF would add a third artifact that can drift out of sync with the other two.

## Documentation language

These specification documents are written in **English** because they are consumed by engineers and coding agents. **All user-facing UI strings are Polish** and are given verbatim, in Polish, in `COPY_AND_TERMINOLOGY.md`. Per decision `D-010`, the implementation must route all UI strings through translation keys even though only the Polish locale ships.
