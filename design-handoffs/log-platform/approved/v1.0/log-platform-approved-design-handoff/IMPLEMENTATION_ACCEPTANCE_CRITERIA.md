# Implementation Acceptance Criteria

Observable, testable criteria. Each is written so a person or an agent can look at the running product and say yes or no. This file is the definition of done.

Reference viewport: **1920 × 1080** unless stated. Verify every criterion in **both** themes.

---

## 1. Shared shell

| # | Criterion |
|---|---|
| SH-1 | The app bar is 56 px tall and the client context bar is 64 px tall on every authenticated screen. |
| SH-2 | The working area occupies the full viewport width; no content is capped at 1280 px and no unused right-hand gutter exists at 1920 px. |
| SH-3 | Primary navigation shows exactly five items in the order `Raporty · Dane · Analizy · Artefakty · Administracja`. |
| SH-4 | `Artefakty` and `Administracja` render in a visibly quieter colour than the three data-mode items when all are inactive. |
| SH-5 | The active nav item is identifiable with colour vision disabled: it has a 2 px underline **and** heavier weight, and carries `aria-current`. |
| SH-6 | The client name and client code are visible on every screen of every module — including empty, loading, permission-denied and error states. Reducing the viewport to 768 px does not hide them. |
| SH-7 | The theme switcher offers AUTO · ☀ · ☾. With AUTO selected, changing the OS colour scheme changes the app theme without a reload. |
| SH-8 | Selecting ☀ or ☾ persists across a logout, a different browser and a different device for the same account. |
| SH-9 | Switching theme while 3 filters are active and the table is scrolled preserves all filters and the scroll position. |
| SH-10 | Light and dark render the same layout: row height, control heights, panel widths and pinned-column count are byte-identical between themes. |
| SH-11 | `⌘K` / `Ctrl+K` focuses global search from any module. |
| SH-12 | Breadcrumbs appear only on `ECO-003` and `REP-003`, and carry the full analytical path including client and period. |
| SH-13 | Using `‹ Wróć do rankingu` or `‹ Wróć do biblioteki` returns to the list with the same filters, sort, page and scroll position that were active on leaving. |

## 2. Database Explorer

### 2.1 Structure

| # | Criterion |
|---|---|
| DB-1 | On the row sheet, the first pixel of the table header is **168 px** from the top of the viewport. Nothing scrolls above the table. |
| DB-2 | No explanatory prose about export limits, access rules or dataset metadata appears above the table on the row sheet. |
| DB-3 | The dataset catalogue presents datasets as rows of one comparison table, not as a grid of cards. Three datasets can be compared on row count, column count and permissions without scrolling. |
| DB-4 | The row sheet shows the `TYLKO ODCZYT` badge, and no screen presents an editing affordance for a dataset value. |
| DB-5 | With `Zwarta` density at 1080 px, at least 26 data rows are visible without scrolling. |
| DB-6 | The table header remains fixed while the body scrolls; the first two columns remain fixed while the body scrolls horizontally. |
| DB-7 | When the table is horizontally scrollable, a right-edge fade is visible; the last column is never clipped without that signal. |
| DB-8 | The sum of declared column widths equals the declared table width — no column is silently pushed out of view. |

### 2.2 Sorting and filtering

| # | Criterion |
|---|---|
| DB-9 | Opening the header control for a **numeric** column exposes `=` `≠` `>` `≥` `<` `≤` `od–do` `puste` **without leaving the dataset table page**. |
| DB-10 | The numeric column menu shows a value-distribution histogram with min and max labelled, and marks the entered threshold **before** the filter is applied. |
| DB-11 | The text column menu offers `zawiera` `=` `≠` `puste` plus a distinct-value picker showing occurrence counts. |
| DB-12 | The date column menu accepts a blank `Od` or `Do` and treats the result as an open-ended inclusive range. |
| DB-13 | Confirming a filter in a column menu updates the table immediately, without a separate Apply step elsewhere. |
| DB-14 | Editing a filter in the filter panel does **not** change the table until `Zastosuj` is pressed. |
| DB-15 | Pressing `Esc` in an open column menu closes it and leaves the previously applied filter unchanged. |
| DB-16 | Every active filter appears exactly once, as a chip or panel entry, showing column, operator and value. |
| DB-17 | Clicking a chip's `×` removes that one filter and requeries immediately. |
| DB-18 | `Wyczyść wszystkie` removes all column filters and the global text search in one action. |
| DB-19 | The `Filtry` button shows a numeric count badge equal to the number of active filters. |
| DB-20 | No screen contains a permanently expanded multi-field filter form. Opening the row sheet shows zero filter input fields until the user opens a menu or the panel. |
| DB-21 | A filtered column's header is visually distinguished **and** carries a non-colour marker. |
| DB-22 | The sorted column shows a direction caret **and** the sort is stated in words in the toolbar. |
| DB-23 | Only one column can be sorted at a time. |
| DB-24 | The result counter distinguishes filtered from total, e.g. `1 274 z 48 213 wierszy`. |
| DB-25 | The global search placeholder states how many text columns are searched, and that number matches the dataset's approved text columns. |

### 2.3 Columns, density, pagination

| # | Criterion |
|---|---|
| DB-26 | `Kolumny` opens a panel listing every approved column with a visibility checkbox, a type badge and a drag handle. |
| DB-27 | Columns can be reordered by dragging and the order persists after `Zastosuj`. |
| DB-28 | Attempting to hide every column is refused with an inline message; at least one column stays visible. |
| DB-29 | A column can be resized by dragging its header edge, and double-clicking the edge fits it to the widest value on the current page. |
| DB-30 | A named column set can be saved and reapplied in a later session by the same account. |
| DB-31 | Switching density changes row height and survives a page reload in the same browser. |
| DB-32 | Page sizes 25 / 50 / 100 / 200 / 500 are offered, with 100 default. |
| DB-33 | No infinite scrolling or virtualization is implemented; pagination is the only browsing model (phase 1, `D-012`). |
| DB-34 | Copying the URL and opening it in a new tab reproduces the same dataset, filters, sort, page, page size, visible columns and open row. |
| DB-35 | Browser Back after applying a filter restores the previous filter set without leaving the module. |

### 2.4 Row detail, copy, values

| # | Criterion |
|---|---|
| DB-36 | Clicking a table row opens a 520 px right-hand panel; the table remains visible and keeps its scroll position. |
| DB-37 | With the panel open, `↑` and `↓` move the selected row and update the panel without closing it. |
| DB-38 | `Esc` closes the panel and returns keyboard focus to the previously selected row. |
| DB-39 | The panel groups fields into labelled sections with counts, defaults to the visible columns, and can switch to all approved columns. |
| DB-40 | A SQL `NULL` and an empty string render as **two different** markers, and the difference is legible without hovering. |
| DB-41 | Numbers are right-aligned, monospace, and thousands-separated; timestamps are monospace and ISO-like. |
| DB-42 | Booleans render as a badge containing a word, not as a bare colour or icon. |
| DB-43 | A zero value is visibly dimmer than a non-zero value but still meets 4.5 : 1 contrast. |
| DB-44 | Selecting a rectangular cell range and pressing `⌘C` produces clipboard content that pastes into a spreadsheet as separate cells. |
| DB-45 | Long text truncates with an ellipsis without changing row height; the full value is retrievable from the row panel. |
| DB-46 | A structured value renders as a collapsed summary in the cell and pretty-printed in the row panel — never raw JSON in a cell. |

### 2.5 Export

| # | Criterion |
|---|---|
| DB-47 | The export panel offers `Bieżący widok`, `Cały zbiór danych` and `Zaznaczone wiersze`, each showing its own row count. |
| DB-48 | Before committing, the panel states whether the export will download immediately or be prepared in the background, and repeats the 3-day retention. |
| DB-49 | An export above 20 000 rows appears in the background list and shows a live indicator in the app bar; the user can navigate away without losing it. |
| DB-50 | The four export states each present a **different action set**; no state shows a greyed-out button. |
| DB-51 | An expired export offers `Zleć ponownie` and offers no download action at all. |
| DB-52 | A failed export exposes a copyable reference. |
| DB-53 | A completed background export is reachable from Report Explorer as the type `Eksporty danych`. |
| DB-54 | For a dataset without export permission, the `Eksport` action is **absent**, not disabled. |

### 2.6 States

| # | Criterion |
|---|---|
| DB-55 | When filters return zero rows, the table header remains rendered. |
| DB-56 | The zero-result message names the specific filter that reduced the result to zero and states the row count without it. |
| DB-57 | The zero-result state offers a button that removes that named filter. |
| DB-58 | A dataset that is genuinely empty produces a different message from a filtered-empty result. |
| DB-59 | While loading, the table shows skeleton rows at the current density and page size; the table area does not change height and the page does not jump. |
| DB-60 | An error state keeps the client context visible, names the failure class, states explicitly that permissions are correct, and exposes a copyable timestamp and reference. |
| DB-61 | A dataset the account may not access does not appear in the catalogue; a deep link to it produces a permission state, not a blank table or a stack trace. |
| DB-62 | For a dataset with filtering disabled, filter controls are **absent** rather than present-and-disabled. |

## 3. Report Explorer

| # | Criterion |
|---|---|
| RP-1 | Report Explorer has no column-visibility control, no density control and no per-column sort menus — it is visibly not a data grid. |
| RP-2 | Report types occupy a left rail, each showing its generation cycle and instance count. |
| RP-3 | Instances are grouped under headings that state the grouping rule in words, and the toolbar restates it. |
| RP-4 | Every instance rendered in a filtered list matches the active filter; setting `Typ = Raport 207` shows no instance of another type in any group. |
| RP-5 | The footer counter distinguishes the filtered count from the library total. |
| RP-6 | Each group heading's count equals the number of instance rows rendered in that group. |
| RP-7 | The rail's per-type counts sum to the library total stated in the context bar. |
| RP-8 | An instance in `W generowaniu` shows no download or open action at all. |
| RP-9 | A failed instance states that no files exist and why. |
| RP-10 | An instance with three files shows three format badges with individual sizes in the library. |
| RP-11 | Opening a report navigates to a dedicated page — not a modal and not a new browser tab. |
| RP-12 | The detail page shows the preview embedded in the layout, with page navigation and a full-screen option. |
| RP-13 | The detail page's file list distinguishes the main file visually, not merely by ordering. |
| RP-14 | A non-previewable file offers download only. |
| RP-15 | The detail page shows the same report across previous periods with each period's own status. |
| RP-16 | `‹ previous period` / `next period ›` move within the same report type and preserve library filters. |
| RP-17 | At the newest period the forward control is absent rather than disabled. |
| RP-18 | `Dane źródłowe` navigates to Database Explorer with the report's dataset and its period applied as a filter, when the account has access. |
| RP-19 | An account with dataset access but no report-folder access sees a state that explains the distinction and keeps client context. |
| RP-20 | A file-store failure leaves the instance list usable and states that only preview and download are unavailable. |
| RP-21 | An instance with a single file shows `Pobierz`, not `Pobierz wszystkie (1)`. |

## 4. Eco Driving

> Criteria about **numbers** are deliberately absent — scoring values come from the repository (`D-002`). These criteria test structure, presentation and interaction.

| # | Criterion |
|---|---|
| EC-1 | The current client, month and selected weeks remain visible on **both** the ranking and the driver-detail screens. |
| EC-2 | A single ranking-basis sentence states which weeks are summed, the calendar range, the day count, and the qualified-versus-total driver counts. |
| EC-3 | Selecting a second week changes that sentence and produces **one** combined ranking, not two side-by-side rankings. |
| EC-4 | Selecting W1 and W3 without W2 is allowed, and produces a visible non-blocking warning that the basis has a gap. |
| EC-5 | Deselecting all weeks produces a prompt to select a week, not an error state. |
| EC-6 | A trailing partial week is labelled as partial, and the basis sentence states the true day count. |
| EC-7 | Changing month resets week selection to the whole month. |
| EC-8 | Position and score are presented at equal visual weight; neither is a subordinate caption of the other. |
| EC-9 | A fleet score-distribution histogram is present with median and mean stated numerically. |
| EC-10 | Metric columns default to events per 100 km, and each column header states its unit. |
| EC-11 | The `/ 100 km ⇄ Σ suma` toggle switches **all** metric columns at once, and every header caption updates. |
| EC-12 | Excluded drivers and unknown-driver trips are reachable via a filter with visible counts, and are **not** presented as tabs. |
| EC-13 | The ranking supports column sorting and filtering with the same chip vocabulary as Database Explorer. |
| EC-14 | Selecting a driver opens a page that presents, in order: score and position, trend, entry identity, score composition, week contribution, contributing trips. |
| EC-15 | The score-composition table shows, per metric, the event sum, the rate, the threshold, points awarded, the maximum, points lost, and that metric's share of total loss. |
| EC-16 | The composition table is sorted by points lost descending. |
| EC-17 | The composition header states the point base and the total lost. |
| EC-18 | The entry-identity section presents the persisted fields including an **exclusive** period end and the assigned ID marked as opaque. |
| EC-19 | A statement distinguishes persisted ranking values from the driver's live display name. |
| EC-20 | The week-contribution table carries a note stating that the ranking is computed on summed data and the breakdown is diagnostic. |
| EC-21 | Trip-level violation values are sums for that trip, and column captions mark them as sums. |
| EC-22 | The trip table exposes: trip start and end date-time, distance, driving time, registration, driver, driver tag ID, dispatcher ID, harsh braking, acceleration, cornering, long idling, high RPM, over-140, over-160, over-170, and contribution to score. |
| EC-23 | The trip table can be sorted and filtered from its column headers, with removable filter chips. |
| EC-24 | The trip table's default filtered state states both the filtered count and the total, and offers a one-click path to all trips. |
| EC-25 | A trip identifier navigates to the same row in Database Explorer with the correct dataset and permission checks applied. |
| EC-26 | Where the account lacks the trip-evidence grant, the trips section renders an explanatory message rather than disappearing. |
| EC-27 | Changing client, month or weeks on a driver page keeps the user on that driver when the driver exists in the new context. |
| EC-28 | When the driver does not exist in the new context, the user lands on the ranking with a message naming the driver and the reason. |
| EC-29 | The data-lineage qualifier appears **once** in the context bar and is not repeated on every ranking row. |
| EC-30 | No reconciliation panel and no score-definition panel are present (`D-016`). |
| EC-31 | A trend with fewer than eight comparable periods renders only the periods that exist, with no zero-filled bars. |

## 5. Artifact Explorer

| # | Criterion |
|---|---|
| AR-1 | The screen carries an operator-mode marker and a neutral, non-accent context rule. |
| AR-2 | Copy on the screen states that Artifacts is not one of the client-data access modes. |
| AR-3 | Artifact kinds appear in a left rail with counts. |
| AR-4 | An account without the operator permission does not see the `Artefakty` nav item, and a direct URL yields a permission state. |
| AR-5 | No artifact is editable; every action is read or inspect. |

## 6. Responsive

| # | Criterion |
|---|---|
| RS-1 | At 1024 px the primary navigation is a menu button and the active module name is still visible. |
| RS-2 | At 1024 px every interactive control has a hit area of at least 44 px in its smaller dimension. |
| RS-3 | At 1024 px and at 768 px the table is still a table: no card-per-row conversion, no column stacking. |
| RS-4 | At 1024 px and 768 px the identifier column remains pinned while the table scrolls horizontally. |
| RS-5 | At 768 px the filter panel is an overlay drawer over a scrim, with `Zastosuj` and `Wyczyść` pinned at its bottom. |
| RS-6 | Dismissing the filter drawer by scrim click or `Esc` **keeps** staged edits; reopening shows them. |
| RS-7 | Below 768 px Database Explorer shows an advisory that names the reason and offers both `Przejdź do Raportów` and `Otwórz mimo to`. |
| RS-8 | `Otwórz mimo to` renders the working table layout at that width — it is not a dead end. |
| RS-9 | Report Explorer and Eco Driving remain fully operable below 768 px. |
| RS-10 | The client name and code are visible at every width down to 390 px. |
| RS-11 | At 200 % browser zoom on a 1920 px viewport, the layout behaves as the 768–1023 px band and no control is clipped. |

## 7. Accessibility

| # | Criterion |
|---|---|
| AC-1 | Every interactive element shows a visible focus ring of 2 px with a 2 px offset; no element removes it. |
| AC-2 | Every pointer action has a keyboard equivalent, including column resize (via the column menu) and range selection (via `Shift` + arrows). |
| AC-3 | Tab order follows visual order; an opened panel or menu appends to the end of the order. |
| AC-4 | Focus returns to the triggering element when any menu, panel or drawer closes. |
| AC-5 | Column-menu, export-panel, column-panel and mobile-drawer layers trap focus while open and expose an accessible name from their visible heading. |
| AC-6 | Tables are real `<table>` elements with `<th scope="col">`, and sort state is exposed via `aria-sort`. |
| AC-7 | A filtered column's accessible name includes the active filter. |
| AC-8 | Every icon-only control has an accessible name; a filter chip's remove button names the filter it removes. |
| AC-9 | No state is conveyed by colour alone: sorted, filtered, selected, boolean, status, permission, and position-change all carry a second signal. |
| AC-10 | `text/faint` is used only for eyebrows, placeholders and the null/empty markers — never for a data value or a control label. |
| AC-11 | Applying a filter, changing sort, changing page and reaching zero results are announced politely; a data-source error is announced assertively. |
| AC-12 | With `prefers-reduced-motion: reduce`, panels, drawers and menus appear without animation, while the export progress indicator still conveys progress. |
| AC-13 | `<html lang="pl">`; numbers use a comma decimal separator and a non-breaking space thousands separator. |
| AC-14 | Skeleton rows are hidden from assistive technology and the table region reports a busy state while loading. |

## 8. Visual fidelity

| # | Criterion |
|---|---|
| VF-1 | Only `IBM Plex Sans` and `IBM Plex Mono` are used, with the declared fallback stacks. |
| VF-2 | No font weight above 600 appears anywhere. |
| VF-3 | Monospace is used for every identifier, number in a table cell, timestamp, hash, physical column name and error reference — and for no prose or button label. |
| VF-4 | No border radius exceeds 4 px, except status dots. |
| VF-5 | No docked panel, card or table carries a drop shadow; shadows appear only on menus, the overlay drawer and the scrim. |
| VF-6 | Alpha appears only as a state colour: active-nav underline, context rule, filter-count badge, selected-row marker, filtered-column tint, selected period, focus ring, and accent text/links. |
| VF-7 | No primary button is alpha. In light theme the primary fill is near-black; in dark theme it is near-white. |
| VF-8 | Every colour used resolves to a token in `DESIGN_TOKENS.md`; there are no ad-hoc hex values. |
| VF-9 | Sibling groups are laid out with flex or grid `gap`, not per-element margins or whitespace text nodes. |
| VF-10 | The rendered screens match `reference/screens/<SCREEN-ID>.png` in structure, hierarchy, density and token usage at 1920 × 1080 in both themes. |
