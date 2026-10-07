# Screen State Matrix

States that **MUST** exist per screen. Only states the product genuinely needs are listed.

Conventions used throughout:
- "context preserved" = app bar + client context bar remain rendered with real values.
- "skeleton" = placeholder rows/blocks at final dimensions, so layout does not shift on resolve.

---

## `DB-001` / `DB-002` — Dataset catalogue

| State | Trigger / condition | Expected visual result | Available actions | Notes |
|---|---|---|---|---|
| Initial / loaded | Account has ≥1 assigned dataset | Rail grouped by client; comparison table with counts, permission badges, saved-view chips | `Otwórz arkusz`, saved-view chip, `Ostatnio używane`, `Moje eksporty` | Full width; no 1280 px cap |
| Loading | Route entered | Context bar real; rail and table as skeletons keeping row heights | none | Counts show `…` |
| Truly empty | Account has no assigned datasets | Context preserved; message stating no datasets are assigned and that an administrator grants access | `Poproś o dostęp` | Distinct from filtered-empty |
| Filtered empty | Rail filter text matches nothing | Table area states which filter text matched nothing | clear filter | Rail stays visible |
| Partial permissions | Dataset has filtering or export disabled | Neutral badge `Bez filtrów` / `Tylko podgląd`; the corresponding control is **absent** in `DB-003` | `Otwórz arkusz` | Never a red badge, never a disabled button |
| Backend unavailable | Catalogue query fails | Context preserved; failure-class badge; copyable reference | `Ponów`, `Skopiuj referencję` | |
| Narrow (≤1024 px) | Viewport | Rail collapses into a `Zbiory` selector above the table | as above | Table remains a table |

## `DB-003` / `DB-004` — Row sheet

| State | Trigger / condition | Expected visual result | Available actions | Notes |
|---|---|---|---|---|
| Initial | Dataset opened, no filters | Table from row 1, default sort on the dataset's date column desc, 100 rows, 12 of 42 columns | all toolbar controls | Table starts 168 px from viewport top |
| Loading | First load or any query change | Header + toolbar real; **skeleton rows** at current density and page size; counters `…` | `Anuluj` where the query is cancellable | Height **MUST NOT** collapse |
| Loaded / populated | Query returned rows | Rows rendered; counter `1 274 z 48 213 wierszy`; sort summary in words | sort, filter, columns, density, export, row click | |
| Filtered | ≥1 active filter | `Filtry` badge shows count; each filter is one chip/panel entry; filtered column header is tinted and carries `≡` | remove one, `Wyczyść wszystkie` | Filtered count vs total both visible |
| Sorted | Sort applied | Direction caret in accent on that header; header label in `text/primary` | change/clear sort | One sort column only |
| No matches | Filters return 0 rows | **Table header remains**; message names the culprit filter and the count without it | remove named filter, `Wyczyść wszystkie` | `DB-010` |
| Truly empty | Dataset itself has 0 rows | Table header remains; message states the dataset is empty and when it was last loaded | back to catalogue | Distinct from no-matches |
| Row selected | Row clicked | Row tinted; accent inset on pinned cell; `DB-006` panel open at 520 px | `↑`/`↓`, `Esc`, panel actions | Table keeps scroll position |
| Range selected | Drag or `Shift`+click across cells | Selection rectangle; footer states selected row count | `⌘C` copies as TSV | No cross-page selection in phase 1 |
| Permission denied | Deep link to a forbidden dataset, or grant revoked mid-session | Context preserved; states access is missing and who grants it | route to an accessible dataset, `Poproś o dostęp` | Never a blank table |
| Backend unavailable | Query timeout / connection failure | `BŁĄD ŹRÓDŁA DANYCH` badge; permission-OK stated explicitly; timestamp + ref + code | `Ponów`, `Zawęź filtrami`, `Skopiuj referencję` | `DB-011` |
| Validation error | Filter value invalid for column type | Error inline in the column menu, next to the value field; filter is **not** applied | correct or clear | Table state unchanged |
| Long-content stress | Cells with 300+ char strings, deep JSON | Ellipsis truncation at cell edge; row height unchanged; full value in `DB-006` and on copy | open row panel | Row height never varies within a page |
| Export processing | Background export queued from this screen | App-bar indicator `1 eksport w toku`; table unaffected | leave page freely | `DB-007` |
| Narrow 1024 / 768 | Viewport | `RSP-001` / `RSP-002` | as above | Filter panel → drawer at 768 |
| Below 768 | Viewport | `RSP-003` advisory | `Przejdź do Raportów`, `Otwórz mimo to` | |

## `DB-005` — Column header menu

| State | Trigger | Expected visual result | Actions | Notes |
|---|---|---|---|---|
| Closed | default | Caret visible in `text/muted` on hover/focus | open | Caret **MUST** be perceivable, not near-invisible |
| Open, text column | Caret click / `Enter` on header | Column name + physical name + type + non-null count; sort A→Z / Z→A; operators `zawiera` `=` `≠` `puste`; value field; distinct-value picker with counts and bars; pin/hide/autofit | `Zastosuj`, `Wyczyść` | `Enter` applies, `Esc` discards |
| Open, numeric column | as above | Operators `=` `≠` `>` `≥` `<` `≤` `od–do`; threshold field; **value distribution histogram** with min/max and the current threshold marked | `Zastosuj`, `Wyczyść` | Threshold visible before applying |
| Open, date column | as above | `przed` `po` `między`; from/to fields, either may be blank for an open-ended inclusive range; presets for the dataset's date column | `Zastosuj`, `Wyczyść` | |
| Open, boolean column | as above | `wszystko` / `tak` / `nie` / `puste` | `Zastosuj` | |
| Applied | `Zastosuj` / `Enter` | Menu closes; chip appears; table requeries; column header tinted | — | Applies immediately (`D-005`) |
| Dismissed | `Esc`, outside click, or scroll of the table | Menu closes; **pending edits discarded**; existing filter unchanged | — | Explicit non-destructive dismissal |

## `DB-006` — Row detail panel

| State | Trigger | Expected visual result | Actions | Notes |
|---|---|---|---|---|
| Open | Row click | 520 px panel; row identifier in header; 4 grouped sections with counts; 12 visible fields by default | `↑`/`↓`, `×`, `Esc`, field search, `Widoczne 12`/`Wszystkie 42`, per-field copy, `Kopiuj wiersz jako JSON`, `Filtruj po tej wartości` | Table stays visible |
| Loading | Row click before fields resolve | Panel opens with header + section skeletons | `Esc` | Panel width does not animate open more than once |
| Traversing | `↑`/`↓` | Selection and panel content move together; panel stays open | as above | Focus stays in the panel |
| NULL field | Value is SQL NULL | `brak wartości`, italic, `text/faint` | copy yields empty | **MUST** differ from empty string |
| Empty-string field | Value is `''` | `pusty tekst`, italic, `text/faint` | copy yields empty | |
| Long value | 300+ chars | Wraps inside the panel; no truncation | copy yields full value | Panel is the escape hatch for truncation |
| No row identifier configured | Production config without `is_row_identifier` | Panel still opens, keyed internally; header shows an approved identifying column instead | as above | Latent capability; do not require config |
| Closed | `Esc`, `×`, or navigating away | Panel removed; focus returns to the previously selected row | — | Focus return is required |

## `DB-007` — Background exports

| State | Trigger | Expected visual result | Actions | Notes |
|---|---|---|---|---|
| In progress | Export >20 000 rows queued | `W toku`; prepared-row count; progress bar; `Dostępny do` = `—` | `Anuluj` | App-bar live indicator |
| Ready | Generation finished | `Gotowy`; expiry date-time; file name + size | `Pobierz` | 3-day retention stated |
| Expired | Past retention | `Pliki wygasły`; expiry in the past; `plik usunięty po 3 dniach` | `Zleć ponownie` | No download action at all |
| Failed | Generation aborted | `Błąd`; cause + copyable ref | `Zleć ponownie`, `Kopiuj ref` | |
| Empty | No background exports ever requested | Message explaining the 20 000-row threshold and that small exports download directly | back to dataset | |

## `DB-008` — Column visibility panel · `DB-009` — Export panel

| Screen | State | Trigger | Expected result | Actions |
|---|---|---|---|---|
| `DB-008` | Open | `Kolumny n/m` | All approved columns with handle, checkbox, label, type badge, pin state; tabs `Wszystkie`/`Widoczne`/`Ukryte`; `Zestawy ▾` | `Zastosuj`, `Zapisz jako zestaw` |
| `DB-008` | Search active | Text in column search | List filters to matches; counts update | as above |
| `DB-008` | Reordering | Drag handle | Live insertion indicator; order applies on `Zastosuj` | cancel by `Esc` |
| `DB-008` | All hidden attempted | User unchecks every column | `Zastosuj` blocked with an inline message; at least one column **MUST** stay visible | correct selection |
| `DB-009` | Open | `Eksport` | Three scopes with their own row counts; column choice; format; **path notice** stating immediate vs background | `Pobierz XLSX`, `Anuluj` |
| `DB-009` | Above threshold | Scope count >20 000 | Notice switches to background-preparation wording with 3-day retention | `Przygotuj w tle` |
| `DB-009` | Export disabled | Dataset permission | Panel is not reachable; the `Eksport` action is absent from the context bar | — |

## `REP-001` / `REP-002` — Report library

| State | Trigger | Expected result | Actions | Notes |
|---|---|---|---|---|
| Loaded | Client has instances | Type rail with counts; instances grouped by generation month, newest first | open, download, filters | Grouping rule stated in the toolbar |
| Loading | Route entered | Rail real; instance rows as skeletons preserving group headings | none | |
| Filtered | Type/year/status filter | Chips with `×`; **every rendered instance matches the filter**; counter `n po filtrach · m w bibliotece` | remove filter | Pager total is the filtered total |
| Empty after filters | 0 matches | Names the filter that emptied the list; states the oldest available instance | corrective action, `Wyczyść filtry` | `REP-004` |
| Truly empty | Client has no reports | States that no reports are assigned for this client | switch client | |
| Instance generating | Status | Warning left edge; `pliki pojawią się po zakończeniu`; **no action buttons** | — | Not a greyed-out button |
| Instance failed | Status | Negative left edge; `brak plików` | `Zgłoś problem` | |
| Instance expired | Status | Neutral edge; no files, no actions | — | History retains the record |
| Access denied | No report-folder grant for the client | Context preserved; explains dataset vs report-folder grants | switch client, `Poproś o dostęp` | `REP-004` |
| File store unavailable | Infra | List renders; preview/download unavailable; copyable ref | `Ponów`, `Skopiuj referencję` | Distinguishes list vs file failure |
| Narrow | ≤1024 px | Rail collapses to a type selector; instance rows stack their regions; still rows, not cards | as above | |

## `REP-003` — Report detail

| State | Trigger | Expected result | Actions |
|---|---|---|---|
| Loaded | `Otwórz raport` | Header card + 8 metadata fields; preview; files panel with main file tinted; 8-period history | download all / per file, format switch, page nav, `Pełny ekran`, `Dane źródłowe`, period siblings |
| Preview loading | Page open | Preview area shows a placeholder at final size; page counter `…` | others available |
| Preview unavailable | Format not previewable, or file store down | Preview area explains why; files panel still lists downloads | `Pobierz` |
| Single file | Instance has one file | Files panel lists one entry; `Pobierz wszystkie` becomes `Pobierz` | download |
| History gap | An earlier period failed or expired | That row carries its own status badge inside the history table | open that period |
| Period edge | Newest or oldest period | The unavailable sibling control is **absent**, not disabled | the available sibling |

## `ECO-001` / `ECO-002` — Ranking

| State | Trigger | Expected result | Actions | Notes |
|---|---|---|---|---|
| Loaded | Client + month + ≥1 week | Ranking basis line; fleet histogram with median/mean; ranking with position, Δ, score, band, 8 metric columns | sort, filter, unit toggle, columns, search, `Szczegóły` | Rates per 100 km by default |
| Σ mode | Unit toggle | All 8 metric columns switch to sums; every header caption reads `Σ suma` | toggle back | One toggle, whole table |
| Loading | Any context change | Context + period bar real; ranking rows as skeletons; histogram renders after counts **without shifting layout** | change context | |
| No data | Month/weeks have no ranking | States the requested period and which periods do have data | jump to nearest available period | |
| Zero weeks selected | User cleared all weeks | Prompt to select at least one week | `Cały miesiąc`, week cards | Not an error |
| Non-contiguous selection | e.g. W1 + W3 | Inline non-blocking warning that the basis has a gap; ranking still computes | proceed or adjust | `D-008` |
| Partial week included | Trailing partial week selected | Week caption says `częściowy`; basis line states the true day count | deselect | |
| Filtered | Group or metric filter | Chips; `Wykluczeni n · Nieznany kierowca m` counts remain visible | remove filter | One ranking, not tabs |
| Client access denied | Eco Driving not enabled for client | Shell + client selector preserved; explains the grant | switch client | |
| Backend unavailable | Query failure | Failure-class badge + copyable ref; period selection preserved | `Ponów` | |
| Narrow | ≤1024 px | Metric columns beyond the first two scroll horizontally; position/driver pinned | as above | Never converts to cards |

## `ECO-003` — Driver drill-down

| State | Trigger | Expected result | Actions | Notes |
|---|---|---|---|---|
| Loaded | Ranking row `Szczegóły` | Score + position + band + percentile; fleet position marked; 8-period trend; 16 identity fields; component table sorted by points lost; week contribution; trips | trip sort/filter, sibling drivers, back, trip → `DB-003` | Breadcrumb carries client + month + weeks |
| Loading | Page open | Breadcrumb + score card real; component and trip tables as skeletons | back | |
| Trips filtered | Default | `Wkład w wynik < 0` chip; `18 z 64 przejazdów po filtrach` | `Pokaż wszystkie 64`, remove chip | Full-table sort/filter available |
| Trip evidence not permitted | Account lacks the trip grant | Section renders an explanatory message in place of the table | request access | **MUST NOT** vanish silently |
| No trips in basis | Driver qualified but has no trips in the selected weeks | Table header remains; states zero trips for this basis | widen weeks | |
| Context switched, driver exists | Client/month/week changed here | Stay on this page; recompute for the new basis; breadcrumb updates | as above | `D-009` |
| Context switched, driver absent | Driver not in new context | Return to `ECO-001` for the new context with a message naming the driver and reason | pick another driver | `D-009` |
| Trend incomplete | Fewer than 8 comparable periods | Only available periods render; no synthetic zeros | — | |
| Narrow | ≤1024 px | Score card and trend stack; component and trip tables scroll horizontally | as above | |

## `ART-001` — Artifact Explorer

| State | Trigger | Expected result | Actions |
|---|---|---|---|
| Loaded | Nav `Artefakty` with operator permission | `TRYB OPERATORA` badge; kind rail with counts; dense artifact table | `Inspekcja`, search, client/date filters |
| Not permitted | Account lacks operator permission | Nav item is **absent**; direct URL yields a permission state | route to an accessible module |
| Artifact removed | Retention or cleanup | `USUNIĘTY` state badge; inspection shows metadata only | — |
| Loading | Route entered | Rail real; table as skeletons | — |

## `RSP-001` – `RSP-003`

| ID | State | Expected result | Notes |
|---|---|---|---|
| `RSP-001` | 1024 px, filter chips inline | Nav → menu button; context bar two-line; chip strip scrolls horizontally; 44 px targets; table 1180 px scrolls with first column pinned | Density stays `Zwarta` |
| `RSP-002` | 768 px, drawer open | Filter panel is an overlay drawer over a scrim; `Zastosuj` / `Wyczyść` pinned at the drawer bottom; 44 px rows in the drawer | Table below keeps pinned first column |
| `RSP-003` | <768 px | Full-screen advisory naming the reason; two routes out | `Otwórz mimo to` renders `RSP-002` layout at that width |
