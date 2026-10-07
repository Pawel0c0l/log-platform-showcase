# Accessibility Spec

Design-level accessibility expectations. **No formal WCAG audit has been performed**, so no conformance level is claimed. These are the intentions the design was built to, and they are testable.

---

## 1. Contrast intentions

Measured against the surface each token actually sits on (see `DESIGN_TOKENS.md`).

| Token | Light | Dark | Intent |
|---|---|---|---|
| `text/primary` | 15.8 : 1 | 14.1 : 1 | Far above 7 : 1 |
| `text/secondary` | 10.4 : 1 | 9.6 : 1 | Above 7 : 1 |
| `text/muted` | 6.0 : 1 | 6.1 : 1 | Above 4.5 : 1 — safe for 11–12 px data and metadata |
| `text/faint` | 3.1 : 1 | 3.4 : 1 | **Below 4.5 : 1 — restricted use** |
| `accent/on-surface` | 5.0 : 1 | 6.4 : 1 | Above 4.5 : 1 at 12 px |
| `state/positive` fg on its bg | 5.6 : 1 | 8.1 : 1 | Above 4.5 : 1 |
| `state/warning` fg on its bg | 6.1 : 1 | 8.9 : 1 | Above 4.5 : 1 |
| `state/negative` fg on its bg | 6.4 : 1 | 7.2 : 1 | Above 4.5 : 1 |

**`text/faint` is permitted only for:** uppercase section eyebrows that duplicate adjacent structure, input placeholders, and the italic `brak wartości` / `pusty tekst` markers which are always paired with a full-contrast field label. It **MUST NOT** be used for data values, counts, column labels, or any text a user must read to act.

Two contrast defects were found and fixed during the design phase; both are recorded because the same mistake is easy to reintroduce:

1. `accent/on-surface` was `#c26a15` (3.86 : 1) and is now `#a85a0d` (5.0 : 1).
2. Table zeros and `Δ 0` were rendered in `text/faint` (3.1 : 1). Zero is **data**, not absence, and now uses `text/muted`.

## 2. Colour independence

**No state in this design is communicated by colour alone.** Every semantic state carries a second, non-colour signal:

| State | Colour | Second signal |
|---|---|---|
| Sorted column | accent caret | Direction glyph `↑`/`↓` **and** the sort stated in words in the toolbar (`sortowanie Start (UTC) ↓`) |
| Filtered column | `accent/tint` header | `≡` marker in the header **and** a chip in the filter strip |
| Selected row | `accent/tint` | Filled checkbox **and** a 2 px inset rule **and** the identifier at weight 600 |
| Boolean true/false | positive/warning badge | The words `TAK` / `NIE` |
| Report status | left edge + badge colour | The words `Gotowy` / `W generowaniu` / `Błąd generowania` / `Pliki wygasły` |
| Export status | badge colour | Status word, plus a different **action set** per state |
| Permission granted/denied | positive/neutral badge | The words `Filtrowanie` / `Bez filtrów`, `Eksport` / `Tylko podgląd` |
| Position change `Δ` | positive/negative | Sign `+` / `−` |
| Out-of-range value | `state/negative` | Weight 600 **and** the threshold is stated in the column menu |
| Active nav item | accent underline | Weight 600 **and** a colour step **and** `aria-current="page"` |
| Score band | bar colour | The band **word** badge (`dobra`, `przeciętna`, …) |
| Histogram highlight | accent bars | A numeric axis label at the highlighted position |

Data-visualization colour is always accompanied by a number. No chart conveys a value by colour alone.

## 3. Focus

- Every interactive element has a visible focus indicator: **2 px `accent/base` outline with a 2 px offset**.
- Focus indicators are **never** removed, and never replaced by the hover treatment alone.
- The focus ring reaches the required 3 : 1 against both the control and its surroundings in both themes.
- `:focus-visible` semantics: keyboard focus always shows the ring; a pointer press on a button need not.
- Focus **MUST NOT** be lost on re-render. After a filter applies, focus stays on the control that applied it. After a row-panel traverse, focus stays in the panel.

## 4. Keyboard reachability

- Every action available by pointer is available by keyboard. There is **no** pointer-only interaction in the approved design.
- Column resize is the one pointer-first affordance and therefore has a keyboard equivalent: `Dopasuj szerokość do treści` in the column menu, and explicit width entry is available from that menu.
- Cell range selection has a keyboard equivalent: `Shift` + arrow keys.
- Tab order follows visual order: app bar → context bar → toolbar → table → footer → open panel. Open layers append to the end of the order.
- Table headers are focusable and open their menu on `Enter`/`Space`. The caret is **not** a separate tab stop.
- The full key map is in `INTERACTION_SPEC.md` §2.

## 5. Focus containment for layers

| Layer | Containment | On close |
|---|---|---|
| Column menu | Trapped | Focus returns to the header |
| Row detail panel (docked) | Not trapped — part of the page | Focus returns to the selected row |
| Row detail (overlay, ≤768) | Trapped | Focus returns to the row |
| Column panel | Trapped | Focus returns to `Kolumny` |
| Export panel | Trapped | Focus returns to `Eksport` |
| Filter panel (docked) | Not trapped | — |
| Filter drawer (≤768) | Trapped | Focus returns to `Filtry` |
| Nav drawer (≤1024) | Trapped | Focus returns to the menu button |

Trapped layers are announced as dialogs with an accessible name taken from their visible heading.

## 6. Button vs link

- **Buttons** perform an action in place: apply, clear, export, download, cancel, toggle.
- **Links** navigate to a different URL: `Otwórz arkusz`, `Otwórz raport`, `Szczegóły`, breadcrumb entries, the trip identifier that jumps to Database Explorer, `Dane źródłowe`, saved-view chips.
- Quiet accent text that navigates (`Pokaż wszystkie 64 przejazdy`, `Otwórz w Przeglądarce danych`) **MUST** be a real link with an `href`, so it opens in a new tab on middle-click and is announced as a link.
- Quiet accent text that acts (`Wyczyść wszystkie`, `Cały miesiąc`) **MUST** be a button. Both look alike by design; the semantics still differ and must be correct.

## 7. Icon-only controls

Every icon-only control needs an accessible name. Required names:

| Glyph | Context | Accessible name |
|---|---|---|
| `☰` | collapsed nav | `Otwórz nawigację` |
| `⌕` | search icon button | `Szukaj` |
| `×` | panel close | `Zamknij panel szczegółów` |
| `×` | filter chip | `Usuń filtr: <kolumna> <operator> <wartość>` |
| `↑` / `↓` | row panel traverse | `Poprzedni wiersz` / `Następny wiersz` |
| `‹` / `›` | pagination | `Poprzednia strona` / `Następna strona` |
| `‹` / `›` | month stepper | `Poprzedni miesiąc` / `Następny miesiąc` |
| `▾` | column header | Not separately named — the header is the control, named `<kolumna>, menu kolumny` |
| `⧉` | field copy | `Kopiuj wartość: <pole>` |
| `⠿` | drag handle | `Zmień kolejność kolumny <nazwa>` |
| `⋯` | overflow | `Więcej akcji` |
| `≡` | filtered marker | Decorative — the filter state is announced via the header's name |
| status dot | attention / export | Decorative — the adjacent text carries the meaning |

## 8. Table semantics

- Real `<table>` with `<thead>`, `<tbody>`, `<th scope="col">`. Not a grid of `<div>`s.
- The table has an accessible name: the dataset name plus client, e.g. `Przejazdy klienta — Acme Logistics`.
- A caption or description conveys the current result scope: `1 274 z 48 213 wierszy, 3 aktywne filtry, sortowanie Start (UTC) malejąco`.
- **Sort state** is exposed with `aria-sort="ascending" | "descending" | "none"` on the `<th>`, in addition to the visible caret and the toolbar sentence.
- **Filter state** is part of the header's accessible name: `Kierowca, filtr aktywny: zawiera Kowal, menu kolumny`.
- Row selection uses real checkboxes with names derived from the row identifier.
- Sticky columns must not break the header/cell association — `scope` and `headers` relationships are unaffected by the visual pinning.
- Skeleton rows are `aria-hidden` and the table region carries `aria-busy="true"` while loading.

## 9. Live regions and announcements

| Event | Announcement | Politeness |
|---|---|---|
| Filter applied | `Zastosowano filtr. 1 274 z 48 213 wierszy.` | polite |
| Filter removed | `Usunięto filtr <nazwa>. <n> wierszy.` | polite |
| All filters cleared | `Wyczyszczono filtry. <n> wierszy.` | polite |
| Sort changed | `Sortowanie: <kolumna>, <kierunek>.` | polite |
| Page changed | `Strona <n> z <m>. Wiersze <a>–<b>.` | polite |
| Zero results | `Brak wierszy dla aktywnych filtrów.` | polite |
| Row panel opened | Panel name + row identifier | polite |
| Export queued | `Eksport przygotowywany w tle.` | polite |
| Export ready | `Eksport gotowy do pobrania.` | polite |
| Data-source error | The failure-class badge text + reference | **assertive** |
| Non-contiguous weeks | `Wybrane tygodnie nie są ciągłe. Podstawa rankingu ma przerwę.` | polite |

The result counter is itself a polite live region; it is the primary feedback that a query changed.

## 10. Reduced motion

`prefers-reduced-motion: reduce` **MUST** suppress: panel open/close, drawer slide, menu rise, and the scrim fade. Layers appear and disappear instantly.

It **MUST NOT** suppress the background-export progress bar — that motion carries information. If motion is fully suppressed, replace it with a discrete percentage that updates.

There is no shimmer on skeletons in any mode.

## 11. Zoom and text scaling

- The layout **MUST** remain usable at 200 % browser zoom. At 1920 px this is equivalent to a 960 px viewport, which resolves to the `bp/tablet-portrait` behaviour — filter drawer, collapsed nav, horizontally scrolling table.
- Text-only scaling to 200 % **MUST NOT** clip labels. Fixed-height controls (32/34 px) grow with their content; the fixed values in `DESIGN_TOKENS.md` are minimums, not caps.
- Table row heights may grow with text scaling; row height stays **uniform within a page**.

## 12. Language and reading

- `<html lang="pl">`. All UI strings are Polish (`D-010`, `COPY_AND_TERMINOLOGY.md`).
- Physical database identifiers (`client_trips_normalized`, `max_speed_kph`) stay in English as data, and are marked `lang="en"` where they appear as prose rather than as code.
- Numbers use the Polish locale: comma decimal separator, non-breaking space thousands separator.
- Timestamps are deliberately ISO-like (`2026-07-06 09:21`) rather than localized prose, so they are unambiguous and sortable by eye.

## 13. Known gaps

Recorded honestly rather than claimed as solved:

1. **No screen-reader audit** has been performed on the approved design. The announcements in §9 are specifications, not verified behaviour.
2. **Cell range selection** is disabled at ≤768 px because touch drag conflicts with scroll. The keyboard equivalent remains, but touch users lose the feature.
3. **The document preview** in `REP-003` inherits the accessibility of the underlying rendered file. A scanned PDF without a text layer will not be readable; the specification requires that the download path stays available as the alternative.
4. **Colour-blind verification** was reasoned about (every state has a non-colour signal) but not tested with simulation.
