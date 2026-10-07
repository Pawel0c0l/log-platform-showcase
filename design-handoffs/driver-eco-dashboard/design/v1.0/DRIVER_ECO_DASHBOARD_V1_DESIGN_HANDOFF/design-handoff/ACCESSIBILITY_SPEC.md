# ACCESSIBILITY_SPEC

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`

Target: WCAG 2.1 AA for all driver-facing content. The dashboard is opened on unknown devices from an e-mail, so nothing may depend on hover, pointer precision, or colour perception.

---

## 1. Colour independence — the core rule

`BUSINESS/DATA CONTRACT`: **colour is never the only carrier of meaning.** Every semantic state ships three redundant channels:

| Meaning | Colour | Glyph | Text / position |
|---|---|---|---|
| full points | green | `✓` | "pełne punkty" / `15 / 15` |
| points lost, still positive | amber + dot pattern | `!` | "podwyższony" / `−6 pkt` |
| category subtracting | red + 135° hatch | `✕` | "strata pkt" / `−10 pkt` |
| not evaluated | neutral grey | `–` | "brak oceny" / "brak jazdy" + the reason in words |
| improvement | green chip | `▲` | always a value **and** a unit: "+5 pkt", "−3 na 100 km", "6 miejsc w górę" — direction is never carried by the arrow or the colour alone |
| deterioration | red chip | `▼` | always a value **and** a unit: "−3 pkt", "+2 na 100 km", "6 miejsc w dół" |
| unchanged | neutral | `=` | "bez zmian" |
| classification | tint | `▬ ◦ ◤` | label + numeric range ("akceptowalny · próg 40–84 pkt") |
| scoring band | band fill + pattern | `▲` current, `◇` target | band label + points-lost number in the row below |

Patterns are part of the contract for the three band fills (green solid, amber dots, red hatch) because band cells are small and adjacent. `BC`

### 1.1 Exception: the daily grid's numbers-only cells

The owner decided the Excel-style daily grid carries **numbers only** — no glyph inside a cell, and no day-status column. Colour independence is therefore satisfied by compensations rather than by an in-cell glyph, and all five are contractual (`BC`):

1. the event count itself is always present as text;
2. every cell has an accessible name / tooltip naming the values in words — "Postój na biegu jałowym: 7 zdarzeń · 4 na 100 km · próg 3–4" (or "brak danych" / "brak przejazdów kwalifikujących" / "brak oceny dla tego dnia");
3. a legend under the table pairs each of the four tints with its meaning in words;
4. the mobile day card keeps per-category chips with short label + count (there is no aggregate day status to fall back on);
5. the expanded day panel states each category's coefficient and band in text.

Do not re-introduce glyphs in the cells; do not drop any of the five compensations.

**Daily distance rule:** in a reporting period that has qualified at the 100 km period-level gate, days with 1–99 km are normal Detailed rows. Never announce or imply a 50 km/100 km daily minimum. Only `0 km` means no qualifying driving for that day.

## 2. Contrast

| Pair | Ratio | Use |
|---|---|---|
| `ink #101828` on `surface #FFFFFF` | 17.0:1 | body, values |
| `ink-2 #344054` on `#FFFFFF` | 10.4:1 | secondary text |
| `ink-3 #475467` on `#FFFFFF` | 7.6:1 | descriptions, supporting values |
| `ink-4 #667085` on `#FFFFFF` | 4.9:1 | captions ≥ 11 px |
| `#14653B` on `#E6F4EE` | 6.7:1 | green chip/cell text |
| `#7C3A05` on `#FEF3E2` | 7.0:1 | amber chip/cell text |
| `#912018` on `#FDECEA` | 7.4:1 | red chip/cell text |
| `#7C3A05` on `#FFFFFF` | 7.6:1 | the score numeral in the acceptable band |
| `accent-text #C2410C` on `#FFFFFF` | 4.6:1 | links, accent labels |

### 2.1 Fill tokens vs text tokens

`BUSINESS/DATA CONTRACT`. The vivid status values (`#1F8A4C`, `#F5B700`, `#DC2626`) are **fills only** — rings, bars, band cells, borders. Text and numerals use the deep variants (`#14653B`, `#7C3A05`, `#912018`). Never set a value in a vivid token on a light surface.

Rules: text ≥ 4.5:1 (≥ 3:1 only for text ≥ 24 px bold); every chip and band cell carries a ≥ 3:1 border against its surface so shapes remain visible in greyscale and in high-contrast mode; `forced-colors` mode must keep glyphs and borders (do not paint state with `background` alone).

## 3. Semantics and structure

- One `<h1>`: "Twoje Eco Driving". Sections use `<h2>`; category and day groups use `<h3>`. No skipped levels.
- View switch: `role="tablist"` with `role="tab"` + `aria-selected` + `aria-controls`; panels `role="tabpanel"` with `aria-labelledby`. Period switch: same pattern, second tablist with its own `aria-label` ("Wybór okresu").
- The day table is a real `<table>` at `wide` with `<caption>` (the period range), `<th scope="col">`, and `<th scope="row">` on the date cell. At `compact` it becomes a `<ul>` of cards, each an `<article>` with an accessible name "Dzień 14.07, wtorek".
- Disclosure controls are `<button aria-expanded>` with `aria-controls`; never a clickable `<div>`.
- Decorative bars and rails: `aria-hidden="true"`. Every chart has a text equivalent (see §5).
- Live regions: none. Nothing changes without user action, so nothing needs announcing.

## 4. Keyboard and focus

| Element | Keys |
|---|---|
| Tabs (view, period) | `Tab` to the list, `←``→` between tabs, `Home`/`End`, activation on focus with `Enter`/`Space` confirmation semantics |
| Category row disclosure | `Tab`, `Enter`/`Space`; on expand, focus stays on the button and the panel becomes the next tab stop |
| Day row / week group disclosure | same |
| Coaching card (when linked to a category) | `Tab`, `Enter`; moves focus to the target category's disclosure button after expanding |
| Compact / expand-all buttons | `Tab`, `Enter`/`Space` |
| Retry button | `Tab`, `Enter` |

- Focus ring: 2 px `#101828` outline + 2 px offset, visible on every surface; never `outline: none` without an equally visible replacement.
- Logical DOM order equals visual order at every breakpoint (the narrow-viewport coaching re-order is done in the data array, not with CSS `order`, so focus order matches).
- A "Przejdź do treści" skip link precedes the header.
- No keyboard trap: no modals in V1.

## 5. Tooltips, touch, and chart labelling

`BC`: **no information exists only in a tooltip.** Hover tooltips are optional enrichment; each one duplicates text already printed on the page (e.g. the axis marker's tooltip repeats the "Do progu 85 pkt: 14 pkt" line already visible).

- Disclosure, not hover, is the primary mechanism for the coefficient behind a day's colour — it works on touch, keyboard and screen readers identically.
- Any tooltip is also reachable on `focus` and dismissible with `Escape`, per WCAG 1.4.13.
- Touch targets ≥ 44 × 44 px: day disclosure buttons, tabs, group headers, category disclosures, retry. Chips are not touch targets unless they are also the disclosure.
- Charts:
  - `ScoreRing` / `TotalScoreTrack`: `role="img"` with `aria-label` "Wynik 71 punktów na skali 0–100. Próg akceptowalny 40, próg bezpieczny 85. Poprzedni okres: 67 punktów."
  - `ScoringAxis`: each band cell is a list item with the accessible name "przedział 5–6 zdarzeń na 100 km, utrata 8 punktów" and, where applicable, ", Twój obecny przedział" / ", próg docelowy, +4 punkty".
  - `SnapshotTrend`: `role="list"`; each bar "Okres 01.07–12.07: 67 punktów"; the current bar adds ", okres bieżący".
  - `GroupShareBar`: `role="img"`, label naming all three percentages in order.
  - `CategoryTrendList`: the signed delta is text; the bars are `aria-hidden`.

### 5.1 The daily grid at narrow widths

`BC` The grid never becomes a horizontal scroll container. Below 700 px it becomes one `<article>` per day with an accessible name "Dzień 14.07, wtorek", a neutral distance/trips chip, and per-category chips whose accessible names carry count, coefficient, band and state in words. Information is preserved, not truncated; the same transformation serves 400 % zoom.

## 6. Language, zoom, motion

- `<html lang="pl">`. All copy is Polish; category labels use the canonical set. Any number formatted `pl-PL` (decimal comma, narrow no-break space thousands).
- Reflow: usable at 320 px width and at 400 % zoom with no horizontal scrolling (WCAG 1.4.10). The vertical `ScoringAxis` and the day cards exist for exactly this.
- Text spacing (WCAG 1.4.12) tolerated: no fixed-height text containers; chips and cards grow with content.
- `prefers-reduced-motion`: all transitions 0 ms.
- `prefers-color-scheme: dark` is **not** implemented in V1. `VD` — the status palette would need re-derivation and re-verification; a light-only dashboard is the safer contract. Document, do not improvise.

## 7. Verification checklist for the implementer

1. Greyscale screenshot test: every state still readable (glyphs + labels + patterns).
2. Keyboard-only pass through `W-SUM`, `W-DET`, `M-SUM`, `M-DET`: every disclosure reachable and operable, focus always visible.
3. Screen-reader pass: score, classification, rank, each category row, one expanded axis, three day rows including a `neutral` day.
4. 320 px and 400 % zoom: no horizontal scrolling on any screen, including `M-DET`.
5. Touch pass: all targets ≥ 44 px; nothing requires hover.
6. Automated: zero axe-core violations at AA on all four screens plus all six access/data-gate states.
