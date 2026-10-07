# Screen Catalog

Every approved screen, with a stable ID. Use these IDs in implementation tasks, commit messages and review.

**Design files** (project root, `.dc.html` = interactive design component):

| Short name | File |
|---|---|
| `ARKUSZ` | `Arkusz - motyw jasny i ciemny.dc.html` |
| `ECO` | `Eco Driving - ranking i kierowca.dc.html` |
| `REP` | `Report Explorer.dc.html` |
| `UZUP` | `Uzupelnienia - wiersz, eksporty, artefakty, widoki waskie.dc.html` |
| `KIER` | `Log Platform - Kierunki v1.dc.html` (direction `1b`, approved; `1a` rejected — retained for decision rationale only) |
| `BASE` | `Baseline - Log Platform As-Is.dc.html` (as-is evidence, **not** a target) |

Status legend: **Approved** · **Approved (structure only)** — layout approved, data/logic from repo · **Reference** — not an implementation target.

---

## Shared shell

| ID | Module | Screen | Purpose | Entry point | Exit / navigation | Design reference | Status |
|---|---|---|---|---|---|---|---|
| `SHL-001` | Shell | App bar + client context, light | Establish nav, client context, theme control at 1920 px | Any authenticated route | Any module | `ARKUSZ` § *Arkusz wierszy* (light) | Approved |
| `SHL-002` | Shell | App bar + client context, dark | Same at `prefers-color-scheme: dark` | Any authenticated route | Any module | `ARKUSZ` § *Arkusz wierszy* (dark) | Approved |
| `SHL-003` | Shell | Theme override AUTO · ☀ · ☾ | Per-account theme override in app bar | App bar | Stays in place | `ARKUSZ` app bar, both themes | Approved |

## Database Explorer

| ID | Module | Screen | Purpose | Entry point | Exit / navigation | Design reference | Status |
|---|---|---|---|---|---|---|---|
| `DB-001` | Dane | Dataset catalogue, light | Choose among assigned datasets; compare row/column counts and permissions | Nav `Dane` | `DB-003` via `Otwórz arkusz`; saved view chip → `DB-003` filtered | `ARKUSZ` § *Katalog zbiorów* (light) | Approved |
| `DB-002` | Dane | Dataset catalogue, dark | Same, dark | Nav `Dane` | `DB-004` | `ARKUSZ` § *Katalog zbiorów* (dark) | Approved |
| `DB-003` | Dane | Row sheet + filter panel, light | The primary working surface | `DB-001`, saved view, global search, deep link | `DB-006` row panel; `DB-009` export; back to `DB-001` | `ARKUSZ` § *Arkusz wierszy* (light) | Approved |
| `DB-004` | Dane | Row sheet + filter panel, dark | Same, dark | as `DB-003` | as `DB-003` | `ARKUSZ` § *Arkusz wierszy* (dark) | Approved |
| `DB-005` | Dane | Column header menu, numeric | Per-column sort + filter + value distribution + pin/hide | Header caret on any column | Applies and closes; `Esc` discards | `ARKUSZ` § *Sterowanie*, both themes | Approved |
| `DB-006` | Dane | Row detail panel (520 px) | Inspect all 42 fields of one row without leaving the table | Row click | `Esc`; `↑`/`↓` moves selection | `UZUP` § *DB-006* | Approved |
| `DB-007` | Dane | Background exports list, 4 states | Track queued/ready/expired/failed exports | `Moje eksporty danych`; app-bar indicator | `Pobierz`; back to dataset | `UZUP` § *DB-007* | Approved |
| `DB-008` | Dane | Column visibility panel (400 px) | Show/hide, reorder, pin, save column sets | `Kolumny n/m` | `Zastosuj`; `Zapisz jako zestaw` | `ARKUSZ` § *Sterowanie*, both themes | Approved |
| `DB-009` | Dane | Export panel | Choose scope, columns, format; state the download path before commit | `Eksport` | Immediate download or `DB-007` | `KIER` § *1b · Ekran 3* (export panel) | Approved |
| `DB-010` | Dane | Empty state after filters | Name the filter that emptied the result and offer removal | Filters return 0 rows | Remove filter → `DB-003` | `ARKUSZ` § *Stany brzegowe*, both themes | Approved |
| `DB-011` | Dane | Data-source error | Distinguish permission-OK from source-failure; give a copyable reference | Query failure/timeout | `Ponów`; narrow filters | `ARKUSZ` § *Stany brzegowe*, both themes | Approved |

## Report Explorer

| ID | Module | Screen | Purpose | Entry point | Exit / navigation | Design reference | Status |
|---|---|---|---|---|---|---|---|
| `REP-001` | Raporty | Report library, light, filtered | Browse instances by client → type → period | Nav `Raporty` | `REP-003` via `Otwórz raport` | `REP` § *REP-001* | Approved |
| `REP-002` | Raporty | Report library, dark, unfiltered | Same, dark, mixed types | Nav `Raporty` | `REP-003` | `REP` § *REP-002* | Approved |
| `REP-003` | Raporty | Report detail with embedded preview | Preview, download files, see history of the same report | `Otwórz raport` | `‹ Wróć do biblioteki`; period siblings; `Dane źródłowe` → `DB-003` | `REP` § *REP-003* | Approved |
| `REP-004` | Raporty | Library states: empty · no access · file-store error | Explain and offer a route out | Filter/permission/infra condition | Corrective action | `REP` § *REP-004* | Approved |

## Functional Analytics — Eco Driving

| ID | Module | Screen | Purpose | Entry point | Exit / navigation | Design reference | Status |
|---|---|---|---|---|---|---|---|
| `ECO-001` | Analizy | Ranking, light, rates per 100 km | Compare drivers within client + month + selected weeks | Nav `Analizy` → `Eco Driving` | `ECO-003` via `Szczegóły` | `ECO` § *Ekran 1* | Approved (structure only) |
| `ECO-002` | Analizy | Ranking, dark, Σ sums | Same ranking with the unit toggle in Σ mode | Unit toggle | `ECO-003` | `ECO` § *Ekran 2* | Approved (structure only) |
| `ECO-003` | Analizy | Driver drill-down | Score → distribution → identity → components → weeks → trips | Ranking row | `‹ Wróć do rankingu`; driver siblings; trip → `DB-003` | `ECO` § *Ekran 3* | Approved (structure only) |

> `ECO-001`–`ECO-003` are **Approved (structure only)**: layout, hierarchy, interaction and column sets are binding; all thresholds, point weights, rating bands and scores are placeholder (`D-002`).

## Artifact Explorer

| ID | Module | Screen | Purpose | Entry point | Exit / navigation | Design reference | Status |
|---|---|---|---|---|---|---|---|
| `ART-001` | Artefakty | Artifact catalogue, dark | Operator inspection of system artifacts | Nav `Artefakty` | `Inspekcja` per artifact | `UZUP` § *ART-001* | Approved |

## Responsive variants

| ID | Module | Screen | Purpose | Entry point | Exit / navigation | Design reference | Status |
|---|---|---|---|---|---|---|---|
| `RSP-001` | Dane | Row sheet at 1024 px (tablet landscape) | Collapsed nav, chip filter strip, 44 px targets | Viewport ≤1024 px | as `DB-003` | `UZUP` § *RSP-001* | Approved |
| `RSP-002` | Dane | Row sheet at 768 px with filter drawer | Filter panel becomes an overlay drawer | Viewport ≤768 px | as `DB-003` | `UZUP` § *RSP-002* | Approved |
| `RSP-003` | Dane | Below 768 px advisory | Explain the limit; offer Raporty or proceed anyway | Viewport <768 px | `Przejdź do Raportów`; `Otwórz mimo to` | `UZUP` § *RSP-003* | Approved |

## As-is baseline — reference only

| ID | Module | Screen | Purpose | Design reference | Status |
|---|---|---|---|---|---|
| `ASIS-001` | Dane (as-is) | Current dataset landing | Evidence of the as-is card grid and panel stack | `BASE` § *Screen 04* | Reference |
| `ASIS-002` | Dane (as-is) | Current row browser | Evidence of the 42-field filter form and below-the-fold table | `BASE` § *Screen 05* | Reference |

---

## Counts

- **Approved screens:** 24 (`SHL` 3 · `DB` 11 · `REP` 4 · `ECO` 3 · `ART` 1 · `RSP` 3 — of which 2 are theme pairs counted separately)
- **Reference screens:** 2
- **Total catalogued:** 26

## Not catalogued — deliberately

| Area | Reason |
|---|---|
| Administration (8 sections) | Visual modernization deferred; inherits shell + table specs; no approved screens (`D-003`) |
| Authentication / login | Out of scope; the only direct runtime captures in the upstream evidence, unchanged |
| Saved-view management screen | Deferred (`D-003`); saved views are created inline and listed in `DB-001` |
| Eco Driving reconciliation panel | Removed this iteration by owner decision (`D-003`); returns with discrepancy tooling |
| Eco Driving score-definition panel | Removed this iteration by owner decision (`D-003`) |
