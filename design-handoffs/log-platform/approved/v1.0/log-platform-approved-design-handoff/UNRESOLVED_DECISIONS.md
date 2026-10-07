# Decisions

Every decision the owner was asked for during handoff preparation, with its resolution. Decisions were collected in two rounds via structured forms on 2026-08-18.

**Final status: `NO_MATERIAL_UNRESOLVED_DECISIONS`** — with one explicitly scoped exception, `D-002`, which is *resolved as a decision* (the owner decided the values are placeholder and must come from the repository) while the underlying business logic remains outside this package's authority.

---

## Resolved

### `D-001` — Should Report Explorer be designed before the handoff is closed?
**Why it mattered:** The handoff requires a Report Explorer behaviour contract, screen catalog entries and acceptance criteria. Only the shell, Database Explorer and Eco Driving had been approved.
**Options:** design it now · describe it in prose only · exclude from scope · design a minimum.
**Resolution:** **Design it now, then close the package.**
**Affected:** `REP-001`–`REP-004`, `PRODUCT_BEHAVIOR_CONTRACT.md` §3, `COPY_AND_TERMINOLOGY.md` §6.
**Status:** Resolved · implemented in `Report Explorer.dc.html`.

### `D-002` — Are the Eco Driving scoring numbers real?
**Why it mattered:** The design shows per-metric thresholds (`≤ 0,35 / 100 km`), point weights (`maks. 20 pkt`), rating bands (`≥ 70 → dobra`) and a 100-point base. Freezing invented numbers into a specification would have made a coding agent implement fabricated business logic.
**Options:** placeholder → mark unresolved · placeholder but structure is right · partially real · real.
**Resolution:** **Placeholder — document as UNRESOLVED; the logic comes from the repository.**
**Consequence:** `ECO_DRIVING_ANALYTICS_SPEC.md` marks every numeric business value `⟨FROM REPO⟩`. The **structure** is binding: N metrics, each with a threshold, points awarded, a maximum, points lost, and each metric's share of total loss; sorted by points lost descending; with a stated base and total. The **values, thresholds, band boundaries, minimum qualifying distance and aggregation formula are not.**
**Affected:** `ECO-001`–`ECO-003`, `ECO_DRIVING_ANALYTICS_SPEC.md`, `IMPLEMENTATION_ACCEPTANCE_CRITERIA.md` §4.
**Status:** Resolved as a decision. **The underlying business logic remains to be read from the repository — this is the one place where the package deliberately does not specify a value.**

### `D-003` — Which undesigned surfaces need visual designs before closing?
**Why it mattered:** Several surfaces existed in the as-is product or were implied but had no approved screen.
**Offered:** row detail · saved-view management · loading states · dataset-permission-denied state · background export states · narrow/tablet views · Administration (8 sections) · Artifact Explorer · none.
**Resolution:** **Design: row detail · background export states · narrow and tablet views · Artifact Explorer.** The rest are specified in prose only.
**Consequence:** Saved-view management, table loading skeletons, the dataset-permission-denied state and Administration are fully specified behaviourally (`SCREEN_STATE_MATRIX.md`) but have no reference screenshot. The Eco Driving reconciliation panel and score-definition panel were separately removed (see `D-016`).
**Affected:** `DB-006`, `DB-007`, `ART-001`, `RSP-001`–`RSP-003`.
**Status:** Resolved · implemented in `Uzupelnienia - wiersz, eksporty, artefakty, widoki waskie.dc.html`.

### `D-004` — Ranking groups: tabs or filter?
**Why it mattered:** The as-is product splits Eco Driving into three groups (ranked / excluded / unknown driver). Tabs imply three co-equal rankings and change the mental model.
**Resolution:** **One ranking; excluded and unknown-driver populations are reachable through a filter with visible counts.**
**Consequence:** `Grupa = w rankingu` is a removable chip; `Wykluczeni 982 · Nieznany kierowca 32` remain visible as counts.
**Affected:** `ECO-001`, `ECO-002`.
**Status:** Resolved.

### `D-005` — When does a filter reach the query?
**Why it mattered:** The design shows `Zastosuj` buttons in both the column menu and the filter panel, while chips have an immediate `×`. Ambiguity here changes the request pattern and the perceived responsiveness.
**Options:** column menu immediate + panel on Apply · everything immediate · everything on Apply · mixed.
**Resolution:** **Column menu applies immediately on confirmation; the filter panel stages edits and applies on `Zastosuj`.**
**Consequence:** Removing a filter, clearing all filters and sorting always apply immediately. Dismissing the column menu discards pending edits; dismissing the mobile drawer keeps them.
**Affected:** `DB-003`–`DB-005`, `INTERACTION_SPEC.md` §4, §3.
**Status:** Resolved.

### `D-006` — What does clicking a table row do?
**Why it mattered:** Determines routing, URL shape and whether a detail view is a page or a layer. The current production configuration has no `is_row_identifier` column, so row identity is a latent capability.
**Options:** nothing · right-hand panel · dedicated page · expand in place · explicit action only.
**Resolution:** **Clicking a row opens a right-hand detail panel.**
**Consequence:** `DB-006` at 520 px; table stays visible; `↑`/`↓` traverse; `Esc` closes; the row identifier is part of the URL. The panel must work even without a configured row-identifier column.
**Affected:** `DB-003`, `DB-006`.
**Status:** Resolved.

### `D-007` — Where does view state live?
**Why it mattered:** Directly determines API surface and schema. Not inferable from a design.
**Options:** URL only · URL + server-side saved views · URL + server views + browser for density/theme · all server-side · all browser.
**Resolution:** **URL for view state; server for saved views; browser for density and theme.**
**Consequence:** Filters, sort, page, page size, visible columns, order, widths and the open row live in the URL. Named saved views and named column sets are server-side per account. Density is per browser. Theme is server-side per account (see `D-011`). Nothing else persists between visits.
**Affected:** `TABLE_AND_DATA_GRID_SPEC.md` §5.4, `PRODUCT_BEHAVIOR_CONTRACT.md` §2.18.
**Status:** Resolved.

### `D-008` — Is non-contiguous week selection (W1 + W3) permitted?
**Why it mattered:** Changes both the selection widget's validation and the meaning of the ranking basis.
**Options:** any combination · contiguous ranges only · permitted with a warning.
**Resolution:** **Permitted, with a warning.**
**Consequence:** An inline, non-blocking advisory states that the basis has a gap. The ranking still computes. Zero weeks is an empty state, not an error.
**Affected:** `ECO-001`, `ECO_DRIVING_ANALYTICS_SPEC.md` level 3.
**Status:** Resolved.

### `D-009` — What happens when client, month or weeks change while on a driver detail page?
**Why it mattered:** The driver may not qualify or exist in the new context; silently showing an empty page or silently redirecting are both wrong.
**Options:** stay and recompute · stay if the driver exists, otherwise ranking with a message · always return to the ranking · disable the switchers on detail pages.
**Resolution:** **Stay if the driver exists in the new context; otherwise return to the ranking with an explanatory message.**
**Consequence:** The message names the driver and the reason. Verbatim copy in `COPY_AND_TERMINOLOGY.md` §9.3.
**Affected:** `ECO-003`.
**Status:** Resolved.

### `D-010` — UI language
**Why it mattered:** The owner stated Polish-only, but every string in the as-is production templates is English. Left unresolved, the implementation would have mixed both.
**Options:** Polish hard-coded · Polish through translation keys · Polish + English · Polish UI with English column names.
**Resolution:** **Polish only, but routed through translation keys.**
**Consequence:** `<html lang="pl">`; a single `pl` locale ships; every UI string has a key. Physical database identifiers stay in English as data.
**Affected:** All screens, `COPY_AND_TERMINOLOGY.md`, `ACCESSIBILITY_SPEC.md` §12.
**Status:** Resolved.

### `D-011` — Theme detection and persistence
**Why it mattered:** Whether a user preference needs server-side storage is an API and schema question.
**Resolution:** **Follow `prefers-color-scheme` by default; a three-way override (AUTO · ☀ · ☾) persisted server-side per account.**
**Consequence:** Consistent across browsers and devices. Switching must not reload the view or lose filters.
**Affected:** `SHL-003`, `PRODUCT_BEHAVIOR_CONTRACT.md` §1.4.
**Status:** Resolved.

### `D-012` — Large-table browsing model
**Why it mattered:** Pagination and virtualization imply different API contracts, different selection semantics and different testability. The owner asked for the trade-offs to be explained before deciding.
**Options:** pagination permanently · pagination now with virtualization as phase 2 · virtualization immediately.
**Resolution:** **Pagination now; virtualization as phase 2.**
**Consequence:** Implement pages of 25–500 (default 100) with a filtered-vs-total counter. Shape the API contract and the table component so phase 2 does not require rebuilding the screen. **Do not implement virtualization now.** Cell range selection does not cross page boundaries in phase 1.
**Affected:** `DB-003`, `ECO-001`, `REP-001`, `TABLE_AND_DATA_GRID_SPEC.md`.
**Status:** Resolved.

### `D-013` — Can one report instance carry multiple files?
**Why it mattered:** Determines whether the library row and the detail page model one file or a collection.
**Resolution:** **Both cases occur; the design must handle one file and many.**
**Consequence:** Format badges with sizes in the library; a full file list with per-file actions on the detail page; the main file visually distinguished; `Pobierz wszystkie (n)` collapses to `Pobierz` for a single file.
**Affected:** `REP-001`–`REP-003`.
**Status:** Resolved.

### `D-014` — How does report preview work?
**Why it mattered:** Modal, drawer, page and new-tab are four different routing and focus models.
**Options:** right-hand panel · dedicated detail page · new browser tab · download only.
**Resolution:** **A dedicated report detail page with an embedded preview.**
**Consequence:** No modal, no new tab. The detail page also carries the file list and the report's history across periods.
**Affected:** `REP-003`.
**Status:** Resolved.

### `D-015` — Narrowest supported width and responsive strategy
**Why it mattered:** The owner selected narrow and tablet views for design but left the breakpoint question unanswered twice, then delegated it.
**Delegated to the designer.** **Decision taken: full support down to 768 px; the table never converts into cards; below 768 px a full-screen advisory with two routes out; touch targets 44 px from 1024 px down.**
**Rationale given to the owner:** comparing values across rows is the work; a card list destroys the column alignment that makes 42 columns scannable. An advisory with an `Otwórz mimo to` escape hatch respects the user's judgement without pretending the layout works.
**Affected:** `RSP-001`–`RSP-003`, `RESPONSIVE_SPEC.md`.
**Status:** Resolved by delegation · **owner may still override**; if so, only `RESPONSIVE_SPEC.md` and `RSP-001`–`RSP-003` change.

### `D-016` — Should the Eco Driving reconciliation and score-definition panels be kept?
**Why it mattered:** Both exist in the as-is product. Removing them without a record would look like a regression to anyone comparing old and new.
**Resolution:** **Remove both for this iteration.** Reconciliation returns when tooling exists to track discrepancies; the score-definition panel is moot until `D-002` resolves.
**Consequence:** The per-row lineage badge is also consolidated into a single context-bar qualifier instead of repeating on all ~500 ranking rows.
**Affected:** `ECO-001`–`ECO-003`, `ECO_DRIVING_ANALYTICS_SPEC.md` "Deliberately removed".
**Status:** Resolved. **Claude Code MUST NOT restore these as a perceived regression.**

---

## Explicitly out of scope

| Item | Reason |
|---|---|
| Administration (8 sections) | Visual modernization deferred (`D-003`). Inherits shell, table and token specs. No approved screens. |
| Authentication screens | Unchanged; the only direct runtime captures in the upstream evidence. |
| Saved-view management screen | Deferred (`D-003`). Saved views are created inline and listed in `DB-001`. |
| Print styling | Not a designed target. Report files are the print artifact. |
| Eco Driving scoring logic | `D-002`. Read from the repository. |

## Escalation triggers

Claude Code **MUST** stop and escalate to the owner rather than decide, if it finds that:

1. The repository's actual scoring logic has a **different structure** from the one specified (not merely different numbers) — for example a multiplicative model, or a per-client configurable metric set.
2. `D-007` persistence cannot be met without a schema migration the owner has not authorized.
3. Row identity (`D-006`) cannot be established for a dataset even internally, making the row panel unimplementable for it.
4. A security or authorization invariant in the repository **contradicts** an approved behaviour — for example if URL-encoded filter state would leak a column the account may not see.
5. The 20 000-row immediate-export threshold or the 3-day retention differs in the repository from what the design states.
