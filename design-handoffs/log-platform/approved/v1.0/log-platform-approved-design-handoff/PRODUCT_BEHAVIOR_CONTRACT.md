# Product Behaviour Contract

The approved behavioural contract. Statements are normative: **MUST**, **MUST NOT**, **SHOULD**. Where a behaviour derives from an owner decision, the decision ID (`D-nnn`) is cited.

---

## 0. Governing product model

The platform has **three distinct user-facing data-access modes** plus one technical surface. They belong to one design system but **MUST NOT** be collapsed into one universal explorer.

| Mode | Purpose | Unit of work | Interaction model |
|---|---|---|---|
| **Dane** (Database Explorer) | Interactive inspection of approved, read-only client datasets | A **row** | Professional data grid; column-centric controls |
| **Raporty** (Report Explorer) | Consume generated/cyclical deliverables | A **report instance for a period** | Library of delivered files; grouped list |
| **Analizy** (Functional Analytics) | Purpose-built analytical applications | A **ranked entity** | Relational drill-down |
| **Artefakty** | Technical/admin artifact inspection | An **artifact** | Operator table; **not** a data-access mode |

Observable consequence: Report Explorer **MUST NOT** offer a column-visibility selector, row density control, or per-column sort/filter menus. Database Explorer **MUST NOT** group rows by month into cards.

---

## 1. Shared shell

### 1.1 Structure

The shell is **horizontal-nav**, not sidebar. Top to bottom:

1. **App bar** — height **56 px**, background `surface/appbar`. Contains: brand mark (6×18 px accent bar + wordmark "Log Platform"), primary navigation, global search, theme switcher, user name, avatar.
2. **Client context bar** — height **64 px**, background `surface/raised`. Contains: 4×26 px accent rule, client name + client code, module/page name, page-level metadata, page-level primary and secondary actions.
3. **Working area** — everything else; consumes the full remaining viewport height and the full viewport width.

The working area **MUST** use the full viewport width. The current 1280 px content cap **MUST** be removed.

### 1.2 Primary navigation

Five items, in this fixed order: **Raporty · Dane · Analizy · Artefakty · Administracja**.

- `Raporty`, `Dane`, `Analizy` are the three data-access modes and render in `text/secondary` when inactive.
- `Artefakty`, `Administracja` are the tools group and render in `text/faint` when inactive — one step quieter, communicating that they are not primary work surfaces.
- The active item renders in `text/primary-on-appbar`, weight 600, with a **2 px accent underline** (`inset 0 -2px 0 accent/base`).
- Active state **MUST** be communicated by both the underline and the weight/colour change — never colour alone (see `ACCESSIBILITY_SPEC.md`).
- `Analizy` is a group. When the active module is inside it, `Analizy` is active and the specific module (e.g. `Eco Driving`) is named in the client context bar.

### 1.3 Client context

**The active client MUST be visible on every screen of every module, including empty, loading, permission-denied and error states.** A user must never have to infer whose data is on screen.

- Rendered as: accent rule + client name (17 px, weight 600) + client code in a mono chip (11 px).
- In Database Explorer and Report Explorer the client context bar is a **selector** (chevron affordance) when the account has more than one client; a static label otherwise.
- In Eco Driving the client selector is a bordered control (`Klient` label + name + code + chevron) because client switching is a primary analytical action there.
- Changing client **MUST** reset module-local selections that cannot survive the switch (dataset, report type, driver) and **MUST** preserve module identity — switching client while in `Dane` lands in `Dane`, not on a home page.

### 1.4 Theme (`D-011`)

- Default theme follows the OS via `prefers-color-scheme`.
- A three-way segmented override sits in the app bar: **AUTO · ☀ · ☾**.
- The override is persisted **per account, server-side**, so it is consistent across browsers and devices.
- Switching theme **MUST NOT** reload the view, lose filters, or reset scroll position.
- Light and dark are the **same layout at the same density**. Only the colour layer changes. Row heights, control heights, radii, spacing, panel widths and column pinning are identical (`DESIGN_TOKENS.md`).

### 1.5 Global search

- A single search field in the app bar, width 300–340 px, placeholder `Szukaj wszędzie`, keyboard shortcut `⌘K` / `Ctrl+K` shown as a hint chip.
- Scope: datasets, reports, drivers, artifacts — across the modules the account may access, scoped to accessible clients.
- Results are grouped by module. Selecting a result navigates to that object in its own module with correct client context.

### 1.6 Breadcrumbs

Breadcrumbs appear **only on detail pages** (`ECO-003`, `REP-003`), in a dedicated 56 px bar below the app bar, and **MUST** carry the full analytical path, e.g.:

`Analizy / Eco Driving / Acme Logistics / Lipiec 2026 · W1 + W2 / Kowalski Marek`

The bar also carries a back affordance (`‹ Wróć do rankingu`, `‹ Wróć do biblioteki`) and sibling navigation (`‹ 42. Nowak Piotr` / `44. Zielińska Ewa ›`).

**Breadcrumbs and the back affordance MUST return the user to the previously active list state with filters, sort, page and scroll position intact** (`D-007`). Detail pages carry a visible confirmation of this: `filtry biblioteki zachowane` / `kontekst okresu zachowany`.

### 1.7 Shell at narrower widths

See `RESPONSIVE_SPEC.md`. Summary: at ≤1024 px the primary nav collapses into a menu button, the context bar compresses, touch targets grow to 44 px. Below 768 px, Database Explorer shows a full-screen advisory (`RSP-003`); Raporty and Analizy remain fully usable.

---

## 2. Database Explorer

### 2.1 Dataset selection (`DB-001`, `DB-002`)

- Layout: 288 px left rail listing datasets grouped by client; main area is a **comparison table**, one row per dataset — not a card grid.
- Table columns: `Zbiór` (name + physical table name), `Klient` (name + code), `Wiersze`, `Kolumny`, `Uprawnienia`, `Zapisane widoki`, action.
- Only datasets assigned to the account are listed. Only approved columns are counted in `Kolumny`.
- `Uprawnienia` renders permission flags as badges: filtering enabled/disabled, export enabled / view-only. A disabled permission is a **neutral** badge, not a red one — it is a configuration fact, not an error.
- Primary action per row: `Otwórz arkusz`.
- Saved views for a dataset are listed inline as chips and are direct entry points — activating one opens the row sheet with that view applied.

### 2.2 Row sheet layout (`DB-003`, `DB-004`)

**The table is the first and dominant element of the working area.** Explanatory prose, access rules and export policy text **MUST NOT** sit above it; they live in the dataset catalogue and inside the export panel.

Vertical composition below the 56 px app bar:

| Band | Height | Contents |
|---|---|---|
| Client context bar | 64 px | client, dataset, physical table, `TYLKO ODCZYT` badge, approved-column count, saved-view selector, `Zapisz zmiany`, `Eksport` |
| Toolbar | 48 px | global text search, `Filtry n`, `Kolumny n/m`, density toggle, result counter, sort summary |
| Table | remaining | sticky header, sticky first two columns, rows |
| Footer | 44 px | pagination, page size, selection/copy hint |

The table therefore begins **168 px** from the top of the viewport and occupies all remaining height. This is the single most important structural requirement of the redesign.

A right-hand **filter panel** of **352 px** may be open alongside the table; it is pinnable and collapsible. Collapsed, only the `Filtry n` toolbar button remains.

### 2.3 Read-only semantics

- Every row-sheet screen **MUST** show the `TYLKO ODCZYT` badge in the client context bar.
- No screen may present an editing affordance for dataset values. Cell interaction is limited to selection and copy.
- The underlying connection remains read-only; no raw SQL surface is offered anywhere in the UI.

### 2.4 Sorting

- Sorting is invoked from the **column header menu** only. There is no separate sort control.
- One sort column at a time. Multi-sort is **not** approved.
- The sorted column shows a direction caret (`↓` / `↑`) in `accent/on-surface` next to its label, and the header label renders in `text/primary` instead of `text/secondary`.
- The current sort is also stated in words in the toolbar: `sortowanie Start (UTC) ↓`.
- Changing sort resets to page 1 and preserves all filters.

### 2.5 Filtering (`D-005`)

Two entry points, one model:

1. **Column header menu** — per-column filter. **Applies immediately on confirmation** (operator + value committed with `Enter` or the `Zastosuj` button inside the menu). Closing the menu without confirming discards the pending change.
2. **Filter panel** — the aggregate view. Edits made in the panel are **staged** and apply only when `Zastosuj` is pressed.

In both cases:
- Removing a filter via its `×` **applies immediately**.
- `Wyczyść wszystkie` applies immediately.
- Sorting always applies immediately.

Every active filter is represented as **exactly one chip / one panel entry**, carrying column name, operator and value. There is **no permanently expanded filter form**; the 42-field form of the as-is product is removed.

The filter panel has three sections: `Aktywne — n` (list of active filters, each removable), `Dodaj filtr` (column search across all 42 approved columns + suggested columns), and a date-range section for the dataset's default date column with presets.

Filter counts are shown as a numeric badge on the `Filtry` toolbar button in `accent/base`.

### 2.6 Global text search

- Retained. One field in the toolbar, placeholder naming the scope: `Szukaj w 16 kolumnach tekstowych`.
- Searches only approved text columns. The exact count is dataset-specific and **MUST** be shown.
- Behaves as an additional predicate ANDed with active column filters. It is **not** represented as a chip; it is its own visible field.

### 2.7 Column visibility, order, width (`D-007`)

- Invoked from the `Kolumny n/m` toolbar button, opening a 400 px panel.
- The panel lists all approved columns with: drag handle, visibility checkbox, label, type badge (`text` / `num` / `ts` / `bool`), pin state.
- Supports: search-by-column-name, filter tabs (`Wszystkie` / `Widoczne` / `Ukryte`), named column sets (`Zestawy ▾`), drag reordering, pinning left.
- `Zastosuj` commits; `Zapisz jako zestaw` names the current selection.
- Column widths are user-resizable by dragging the header edge; `Dopasuj szerokość do treści` is available in the column menu.
- **Persistence:** visible-column selection, order, widths and density live in the **URL** for the current view and in a **server-side saved view** when the user names it. Density and theme additionally persist in the **browser**. Nothing else survives between visits (`D-007`).

### 2.8 Density

Two levels: `Zwarta` (32 px rows, default) and `Wygodna` (40 px rows). Segmented control in the toolbar. Persisted in the browser.

### 2.9 Pagination (`D-012`)

- **Phase 1 (this implementation): pagination.** Page sizes 25 / 50 / 100 / 200 / 500, default 100. Footer states `1–100 z 1 274`.
- The result counter **MUST** distinguish filtered from total: `1 274 z 48 213 wierszy`.
- **Phase 2 (later): virtualized continuous scrolling.** The API contract and table component **SHOULD** be shaped so that phase 2 does not require rebuilding the screen. Phase 2 behaviour is documented but **MUST NOT** be implemented now.

### 2.10 Row interaction (`D-006`) — `DB-006`

- **Clicking a row opens a 520 px right-hand detail panel.** The table stays visible and keeps its scroll position.
- The selected row is marked by: row tint (`accent/tint`), a 2 px accent inset on the pinned identifier cell, the identifier rendered in `text/primary` weight 600, and a filled checkbox.
- `↑` / `↓` move the selection and the panel contents together, without closing the panel.
- `Esc` closes the panel and returns focus to the previously selected row.
- The panel URL includes the row identifier, so the panel state is linkable.
- The panel groups all 42 fields into labelled sections with counts (`Tożsamość`, `Czas i trasa`, `Metryki jazdy`, `Klasyfikacja i pochodzenie`), defaults to the 12 visible columns, and offers a toggle to all 42 plus a field search.
- Panel actions: `Kopiuj wiersz jako JSON`, `Filtruj po tej wartości`.
- Where the dataset has no configured row identifier (the current production configuration), the panel still opens and is keyed by the internal row position; the header shows the dataset's own identifying column if one is approved.

### 2.11 Cell interaction and copy

- Single click on a cell selects it. Click-and-drag or `Shift`+click selects a rectangular range.
- `⌘C` / `Ctrl+C` copies the selection as TSV, preserving cell boundaries so the result pastes into a spreadsheet as cells, not as one string.
- Copying is hinted in the footer: `zaznacz zakres i ⌘C, aby skopiować do arkusza`.
- Selection **MUST NOT** cross page boundaries in phase 1; the footer states the selection count.

### 2.12 Value rendering

Full rules in `TABLE_AND_DATA_GRID_SPEC.md`. Contract-level requirements:

- Numbers right-aligned, mono, thousands-separated with a non-breaking space.
- Timestamps mono, ISO-like, never localized into ambiguous formats.
- Booleans as badges with words (`TAK` / `NIE`), coloured but also lexically distinct.
- **`NULL` and empty string MUST be visually distinguishable** — `brak wartości` and `pusty tekst`, both italic in `text/faint`. The as-is product renders both through one `str()` path and this is a real data-verification defect.
- Long strings truncate with ellipsis at the cell edge; the full value is available in the row detail panel and on copy.

### 2.13 Export (`DB-009`, `DB-007`)

The export panel offers three scopes, three column choices and two formats:

- **Scope:** `Bieżący widok` (filtered count), `Cały zbiór danych` (total count), `Zaznaczone wiersze` (selection count). Each option shows its own row count.
- **Columns:** `Jak na ekranie` (n) or `Wszystkie zatwierdzone` (m).
- **Format:** `XLSX` or `CSV`.
- The panel states which path will be taken **before** the user commits: up to 20 000 rows downloads immediately; above that it is prepared in the background with 3-day retention.

Background exports (`DB-007`) have **four** states, each with a different action set — never a greyed-out button:

| State | Progress | Available action |
|---|---|---|
| `W toku` | row count + progress bar | `Anuluj` |
| `Gotowy` | expiry date + file name and size | `Pobierz` |
| `Pliki wygasły` | expiry date in the past | `Zleć ponownie` |
| `Błąd` | copyable reference | `Zleć ponownie`, `Kopiuj ref` |

An in-progress export shows a live indicator in the app bar (`1 eksport w toku`) so the user may leave the page. Completed background exports appear in Report Explorer as the system report type `Eksporty danych`.

### 2.14 Empty state (`DB-010`)

Triggered when active filters return zero rows.

- The table header **MUST** remain rendered — the user must still see the shape of the data.
- The message **MUST** name the filter that reduced the result to zero and state what removing it would yield: *"Zbiór ma 48 213 wierszy. Filtr `Depot = KRK-02` zawęża wynik do zera — bez niego zobaczysz 1 274 wiersze."*
- Two actions: remove the culprit filter (named in the button), or `Wyczyść wszystkie`.

### 2.15 Loading state

- The client context bar and toolbar render immediately with known values.
- The table renders its header immediately and shows **skeleton rows** at the current density for the current page size — not a spinner, not a blank area.
- The result counter shows `…` in place of counts until they resolve.
- Loading **MUST NOT** collapse layout height; the table area keeps its size so the page does not jump.

### 2.16 Error state (`DB-011`)

- Client context is preserved and visible.
- A mono badge names the failure class (e.g. `BŁĄD ŹRÓDŁA DANYCH`).
- The body distinguishes **the user's permissions being fine** from **the data source failing**, and states the concrete cause where known (e.g. a 15 s statement timeout on the client database).
- A copyable diagnostic trio: timestamp, reference id, error code.
- Actions: `Ponów`, `Zawęź filtrami`, `Skopiuj referencję`.

### 2.17 Access-disabled state

- A dataset the account may not open **MUST NOT** appear in the catalogue at all.
- A permission revoked mid-session, or a deep link to a forbidden dataset, produces a dedicated state that keeps client context, states plainly that access is missing and who grants it, and offers a route back to an accessible dataset.
- Feature-level restrictions (filtering disabled, export disabled) are shown as neutral badges in the catalogue and the corresponding controls are **absent**, not disabled.

### 2.18 State preservation

- All view state (dataset, filters, sort, page, page size, visible columns, density, open row) lives in the **URL**. Every view is linkable and correct under browser Back.
- Saved views are **named server-side records** that resolve to such a URL.
- Browser Back **MUST** step through view states, not out of the module.

---

## 3. Report Explorer

### 3.1 Structure and ordering axis

Axis: **Klient → typ raportu → okres**.

- Client is in the context bar.
- **Report types** occupy the 288 px left rail, each showing name, generation cycle (`tygodniowy · pon. 04:00`), instance count, and an attention dot when the latest instance failed.
- The working area lists **report instances grouped by generation month**, newest first. Group headings state the rule explicitly: `Wygenerowane w lipcu 2026`, with a per-group count.
- Ordering within a group is descending by generation timestamp. The toolbar states the rule: `grupowanie po miesiącu wygenerowania, najnowsze u góry`.

### 3.2 Report instance row

Each instance is a card-row with a left accent edge carrying its status colour, and six regions: name (+ `NOWY` badge when unseen), report type, `Okres raportowania`, `Status` + timestamp, `Pliki` (format badges with sizes), actions.

### 3.3 Status model

Four states. **Status governs the available actions**; there are no greyed-out buttons.

| Status | Left edge | Files | Actions |
|---|---|---|---|
| `Gotowy` | neutral | format badges | `Otwórz raport`, `Pobierz` |
| `W generowaniu` | warning | *"pliki pojawią się po zakończeniu"* | none |
| `Błąd generowania` | negative | *"brak plików — generowanie nie ukończyło się"* | `Zgłoś problem` |
| `Pliki wygasły` | neutral | none | none (history retains the record) |

### 3.4 Filtering and pagination

- Toolbar: text search over report name and period, `Typ` filter chip, `Rok` selector, `Status` selector.
- Active filters are chips with `×`, same vocabulary as Database Explorer.
- The footer counter **MUST** distinguish filtered from library total: `1–14 z 14 po filtrach · 40 w bibliotece`.
- **The rendered list MUST obey the active filters.** A type filter of `Raport 207` means no instance of another type appears in any group.

### 3.5 Multiple files per instance (`D-013`)

A report instance may carry **one file or many** — either the same content in several formats, or genuinely different attachments.

- In the library, formats are shown as badges with individual sizes.
- On the detail page, a `Pliki w tej pozycji` panel lists every file with format, filename, size, a semantic note (`dokument główny`, `dane szczegółowe`, `dane surowe`), and per-file actions.
- The **main file is visually distinguished** by a tinted row, not merely by being first.
- Non-previewable files (e.g. CSV) expose `Pobierz` only.

### 3.6 Report detail page (`REP-003`) (`D-014`)

Preview happens on a **dedicated detail page**, embedded in the layout — **not** a modal, **not** a new browser tab.

The page has four regions:

1. **Header card** — report title, description of what the report covers, an 8-field metadata grid (`Klient`, `Typ raportu`, `Cykl`, `Okres raportowania`, `Numer okresu`, `Wygenerowano`, `Wiersze w raporcie`, `Retencja plików`), status badge, `Pobierz wszystkie (n)`, `Dane źródłowe`.
2. **Preview** — format switcher (only for previewable formats), file name / page count / size, page navigation, `Pełny ekran`.
3. **Files panel** — as §3.5.
4. **History panel** — the same report across the last 8 periods with period, status, generation time and file count. The current period is highlighted. `Pokaż wszystkie n okresów` expands.

`Dane źródłowe` links to Database Explorer, to the dataset behind this report, with the report's period pre-applied as a filter — when the account has access to that dataset.

### 3.7 Period navigation

`‹ Tydzień 27` / `Tydzień 29 ›` in the breadcrumb bar move to the adjacent period **of the same report type**, preserving library filters. Same pattern as Eco Driving driver navigation.

### 3.8 States (`REP-004`)

- **Empty after filters** — names the filter that emptied the list and states the oldest available instance; offers a corrective action and `Wyczyść filtry`.
- **No access to this client's reports** — keeps client context; explains that dataset access and report-folder access are separate grants; offers a route to an accessible client and `Poproś o dostęp`.
- **File-store failure** — distinguishes *"the list loaded but the file store is down"* from a list failure; preview and download are unavailable, the list is not; carries a copyable reference.
- **Loading** — rail and context render immediately; instance rows render as skeletons preserving group structure.

---

## 4. Eco Driving

Full specification in `ECO_DRIVING_ANALYTICS_SPEC.md`. Contract-level requirements here.

### 4.1 Client context

Each client has an independent ranking. The analysed client **MUST** be unambiguous at all times, rendered as a bordered selector labelled `Klient` in the context bar, showing name and code.

### 4.2 Period model (`D-008`)

- The primary period is a **complete calendar month**, chosen with a stepper (`‹ Lipiec 2026 ›`). Future months are unavailable.
- Within the month, the user selects **one or more weeks** as toggle cards showing the week label and its date range (`W1 · 01–07.07`). A partial trailing week is labelled as such in its date caption.
- **Non-contiguous selection (e.g. W1 + W3) is permitted, with a warning.** The warning is inline and non-blocking, stating that the ranking basis has a gap.
- `Cały miesiąc` selects all weeks; `Wyczyść` clears to none. Zero weeks selected is an empty state, not an error.
- Multiple weeks are **summed** into one combined ranking — never displayed as parallel per-week rankings.
- A single sentence, the **ranking basis line**, is the sole source of truth for what is on screen: *"Podstawa rankingu: **W1 + W2** zsumowane · 01–14.07.2026 · 14 dni"*, plus qualification and volume counts. It **MUST** be present on the ranking screen and echoed in the drill-down breadcrumb.

### 4.3 Ranking

- Position and score are **equally prominent**: position is the first column at 16 px mono weight 600; score is 17 px mono weight 600 with a proportional bar.
- `Δ` shows change in position versus the previous comparable period.
- A **fleet score distribution histogram** sits in the period bar with median and mean, because a bare position among ~500 ranked drivers is not interpretable alone.
- Metric columns show **events per 100 km by default**, with a single `/ 100 km ⇄ Σ suma` toggle that switches **all** metric columns at once. Each column header carries a unit caption stating the current mode.
- Eight metric columns: ostre hamowania, przyspieszenia, ostre skręty, długi postój, wysokie obroty, > 140, > 160, > 170.
- Ranking rows support column sorting and filtering with the same vocabulary as Database Explorer, plus a text search over driver, tag ID and registration.

### 4.4 Ranking groups (`D-004`)

One ranking. Drivers excluded from ranking and unattributed trips are **filter values with visible counts**, not tabs: `Grupa = w rankingu` chip plus `Wykluczeni 982 · Nieznany kierowca 32`.

### 4.5 Driver drill-down (`ECO-003`)

Four layers of evidence on one scrollable page, in this order:

1. **Score and position** — score / 100, position / participants, rating band, percentile, plus the driver's marked location in the fleet distribution.
2. **Trend** — the same driver's score across the last 8 comparable periods.
3. **Entry identity and period** — 16 persisted fields (provider, ranking family, persisted period label, inclusive start, **exclusive** end, partiality state, period ordinal, opaque assigned ID, ranking group, qualification status, calculation status, driver-metadata source, participants, band share, total distance in metres and km). The panel **MUST** state that ranking values are persisted while the driver's display name comes from the current driver record and is not an immutable historical record.
4. **Score composition** — one row per metric with event sum, per-100 km rate, threshold, points awarded, maximum, **points lost**, and each metric's share of total loss. Sorted by points lost descending.
5. **Week contribution** — per-week breakdown for diagnosis, explicitly labelled as diagnostic: the ranking is computed on the summed data.
6. **Contributing trips** — the underlying evidence.

### 4.6 Contributing trips

- Columns: start and end date-time, distance, driving time, registration, driver, driver tag ID, dispatcher ID, ostre hamowania, przyspieszenia, skręty, długi postój, wysokie obroty, > 140, > 160, > 170, contribution to score.
- **On the trip level, violation values are always Σ sums for that trip** — a trip is a single event in time, so a rate would be meaningless. Column captions state `Σ`.
- The trip table is **fully sortable and filterable from column headers**, with active-filter chips and a text search, exactly like Database Explorer.
- Default filter narrows to trips with a negative contribution, with the count stated (`18 z 64 przejazdów po filtrach`) and a one-click path to all trips.
- The trip identifier links to the same row in Database Explorer, same dataset, same permission model — closing the chain ranking → driver → components → trip → raw row.

### 4.7 Context switching from the drill-down (`D-009`)

When the user changes client, month or week selection while on a driver detail page:

- If the driver **exists in the new context**, stay on the driver detail page and recompute for the new period.
- If the driver **does not exist** (not qualified, not present, or different client), return to the ranking for the new context and show an explanatory message naming the driver and the reason.

### 4.8 Data lineage

The lineage qualifier (`Linia danych: zrekonstruowana ze stanu bieżącego`) is **constant for a period** and therefore belongs in the context bar **once** — not repeated per ranking row as the as-is product does.

The reconciliation panel of the as-is product is **deliberately removed** from this iteration (`D-003`) and returns when discrepancy-tracking tooling exists.

### 4.9 States

- **No data** — the selected month/weeks have no ranking. States which period was requested and which periods do have data; offers the nearest available period.
- **Partial data** — a trailing partial week is included: the week card caption says `częściowy`, and the ranking basis line states the true day count.
- **Zero weeks selected** — prompts to select at least one week; `Cały miesiąc` is offered.
- **Loading** — context bar and period selector render immediately; ranking rows are skeletons; the histogram renders after counts resolve and **MUST NOT** shift layout.
- **Client access denied** — keeps the shell and the client selector, states that Eco Driving is not enabled for this client or the account lacks access, offers an accessible client.
- **Row-evidence permission** — the current product separates access to the ranking summary from access to contributing trips. Without the trip grant, sections 6 renders an explanatory message in place of the table; it **MUST NOT** disappear silently.

---

## 5. Artifact Explorer (`ART-001`)

- Visual modernization on the shared tokens only. **No change to the product model.**
- Marked as a technical surface: an `TRYB OPERATORA` badge in the app bar and a neutral (non-accent) context rule.
- Left rail lists artifact kinds with counts. Main area is a dense operator table: `Utworzono`, `Rodzaj`, `Nazwa`, `Klient`, `Hash`, `Rozmiar`, `Stan`, action `Inspekcja`.
- Artifacts are immutable and read-only. The rail states the operator-permission requirement.
- **Artifact Explorer MUST NOT influence the product's navigation model**, and is not one of the three data-access modes.
