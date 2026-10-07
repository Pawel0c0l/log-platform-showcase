# VISUAL_SYSTEM

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`
**Canonical visual source:** `../Eco Driving Design System.dc.html` (tokens, component anatomy) and `../Eco Driving Dashboard.dc.html` (the product in context). Where a value in this document and the canonical sources disagree, the canonical sources win — they are the design.

Library-agnostic: plain CSS custom properties, Tailwind, or CSS-in-JS are all acceptable. No framework is required.

---

## 1. Direction

Analytical and modern in the register of a contemporary BI dashboard, driver-friendly rather than admin-oriented: soft neutral canvas, generously rounded surfaces (16–24 px), animated rings and bars, and hover/focus response on interactive elements. The Telematics programme alpha is the brand accent. Data carries colour; nothing decorative competes with the numbers.

The current monthly e-mail remains the **semantic** reference (scoring axis, band thresholds, improvement potential). Its Outlook-driven geometry — 620 px tables, fixed-pixel bars, `bgcolor` spacers — is explicitly **not** inherited (AS-IS `REPORTING_AND_EMAIL_UX` §6).

## 2. Typography

`VISUAL DESIGN DECISION`. One family: **Manrope** 400–800, Latin Extended subset (Polish diacritics required), `display=swap`, with a system grotesque fallback so first paint is never blank.

There is **no monospace family**. Numeric values are set in Manrope with `font-variant-numeric: tabular-nums`, which keeps columns comparable by eye without the technical register the owner rejected.

| Token | Size / line-height | Weight | Use |
|---|---|---|---|
| `display-score` | 52 / 1.0 | 800 | the Eco score inside the ring |
| `display-score-mobile` | 40 / 1.05 | 800 | the Eco score, ≤ 700 px |
| `metric-lg` | 28 / 1.1 | 600 | KPI tile values |
| `metric-md` | 25 / 1.15 | 600 | period range |
| `metric-sm` | 18–20 / 1.2 | 700–800 | secondary values, coaching values |
| `title` | 23 / 1.3 | 800 | product title |
| `section` | 17 / 1.35 | 800 | section headings |
| `body-strong` | 13 / 1.4 | 700 | row labels |
| `body` | 13 / 1.6 | 400 | explanatory prose |
| `meta` | 12 / 1.55 | 400–700 | chips, secondary values |
| `micro` | 11 / 1.5 | 400–700 | captions, legends |
| `eyebrow` | 10–11 / 1.3 | 800 | uppercase labels, `letter-spacing: 0.14em` |

Minimum text size anywhere: **11 px**, reserved for captions and legends.

`BUSINESS/DATA CONTRACT`-adjacent rule: every numeric value uses tabular figures. Substituting the family is allowed provided tabular figures survive.

## 3. Colour

### 3.1 Surfaces and text (cool neutrals)

| Token | Value | Use |
|---|---|---|
| `canvas` | `#F4F7FA` (top gradient `#EEF3F9`) | page background |
| `surface` | `#FFFFFF` | cards, table body |
| `surface-sunken` | `#F9FAFC` | panels, expansion areas |
| `surface-alt` | `#F2F4F7` | table headers, group headers, neutral chips |
| `hairline` | `#E9EDF5` | card borders |
| `hairline-2` | `#E3E8F0` | table lines, dividers |
| `ink` | `#101828` | primary text, dark totals row, markers |
| `ink-2` | `#344054` | secondary text |
| `ink-3` | `#475467` | descriptions, supporting values |
| `ink-4` | `#667085` | captions ≥ 11 px only |
| `ink-5` | `#98A2B3` | axis ticks, decorative labels (never a value a driver must read) |

### 3.2 Programme accent (Telematics)

| Token | Value | Use |
|---|---|---|
| `accent` | `#FF7300` | programme mark, active nav, target markers, opportunity fills |
| `accent-text` | `#C2410C` | accent text/links on white |
| `accent-deep` | `#8F3A04` | accent text on tinted surfaces |
| `accent-tint` | `#FFF3E8` / `#FFF7F0`, border `#FFD8B5` | cumulative chip, near-threshold section |

`VISUAL DESIGN DECISION`: the accent **never** encodes performance. It marks the programme, navigation and *opportunity*, so it can never be mistaken for a status.

### 3.3 Status colours — the only semantic palette

`BUSINESS/DATA CONTRACT`. Four states. Fill tokens and text tokens are **separate**: the vivid values are legible only as fills, the deep values only as text.

| Status | Meaning (derived) | Chip bg / border / text | Band fill | Pattern | Glyph |
|---|---|---|---|---|---|
| `green` | `points == points_max` | `#E6F4EE` / `#B4DCC8` / `#14653B` | `#C6E7D6` (vivid `#1F8A4C`) | none | `✓` |
| `yellow` | `0 <= points < points_max` | `#FEF3E2` / `#F6DCAE` / `#7C3A05` | `#FBE3B8` (vivid `#F5B700`) | 6 px dot grid | `!` |
| `red` | `points < 0` | `#FDECEA` / `#F3C4BE` / `#912018` | `#F7C9C3` (vivid `#DC2626`) | 135° hatch | `✕` |
| `neutral` | no coefficient is derivable under the existing scoring contract (e.g. no qualifying driving) | `#F2F4F7` / `#E3E8F0` / `#475467` | `#E4E7EC` | none | `–` |

**Hard invariants** (identical wording in `PRODUCT_UX_CONTRACT`, `COMPONENT_SPECIFICATIONS`, `SCREEN_SPECIFICATIONS`, `DATA_TO_UI_MAPPING`, `STATES_AND_EDGE_CASES`, `IMPLEMENTATION_HANDOFF`, `CLAUDE_CODE_IMPLEMENTATION_BRIEF`):

1. Status is **never** derived from a raw event count. The chain is
   `raw violations + qualifying exposure/distance → Eco violation coefficient → existing scoring bucket/threshold → semantic colour`.
   Two identical counts may legitimately differ in colour because their exposure differs.
2. There is exactly **one** status rule in the product, used for period figures and daily figures alike. The e-mail's hand-maintained `LOST_POINTS_COLOR_RULES` lookup is not used (AS-IS `G-04`).
3. Every status instance carries fill **and** border **and** glyph **and** a word (label or accessible name). Colour alone never carries meaning.
4. `neutral` is a designed state, not an error — it is what the product shows instead of guessing. There is **no dashboard-specific minimum-distance threshold**: where the existing Eco scoring contract yields a coefficient, it is used.
5. Vivid status values are fills only. Text uses the deep variants (contrast table in `ACCESSIBILITY_SPEC.md` §2).

The AS-IS e-mail palette (`#8BD450` / `#FFF200` / `#FF0000`) is intentionally not reused: those values cannot carry text at AA. Band semantics are preserved; hues are re-tuned. `VISUAL DESIGN DECISION`

### 3.4 Classification tints

| Rating | Score range | Fill / ring | Tint | Border | Text | Glyph |
|---|---|---|---|---|---|---|
| `bezpieczny` | 85–100 | `#1F8A4C` → `#5FCB92` | `#E7F7EE` | `#B4DCC8` | `#14653B` | `▬` |
| `akceptowalny` | 40–84 | `#F5B700` → `#FFD666` | `#FEF3C7` | `#F6DCAE` | `#7C3A05` | `◦` |
| `niebezpieczny` | < 40 | `#DC2626` → `#F87171` | `#FEE4E2` | `#F3C4BE` | `#912018` | `◤` |
| none (no score) | — | `#C7CEDB` | `#F2F4F7` | `#E3E8F0` | `#475467` | `–` |

The score ring, the score numeral, the classification pill and the 0–100 band track all take the colour of the driver's current band.

### 3.5 Category-loss distribution palette

`VISUAL DESIGN DECISION` (owner-approved): three hues in two saturations — the two largest losses in reds (`#9F1208`, `#F0563F`), the next two in ambers (`#E3A008`, `#FFDE7A`), the smallest in green (`#1F8A4C`). Categories with **equal** points lost share one colour: the colour encodes the size of the loss, not the identity of the category.

## 4. Spacing, radius, elevation

`VISUAL DESIGN DECISION`. 2 px base, 4 px rhythm: `2 · 4 · 6 · 8 · 10 · 12 · 14 · 16 · 18 · 20 · 24 · 26 · 40`.

| Token | Value |
|---|---|
| card padding | 18–24 px (14–16 px ≤ 700 px) |
| section gap | 16 px between cards, 40 px between reference sections |
| radius | shell/section 24 px · card 18–22 px · KPI tile 20 px · chip 8–12 px · pill 999 px · cell 4–10 px |
| border | 1 px `hairline`; 2 px `ink` only for the current-band and current-period markers; 2 px dashed `accent` for a target band |
| elevation | `0 1px 3px rgba(16,24,40,.06)` resting; `0 10px 24px rgba(16,24,40,.10)` on hover for interactive tiles; mobile frames `0 8px 28px rgba(16,24,40,.10)` |

Layout: max content width **1240 px**; internal grids use `repeat(auto-fit, minmax(…, 1fr))` so reflow happens without media queries.

## 5. Chart language

`VISUAL DESIGN DECISION` on form; `BUSINESS/DATA CONTRACT` on meaning. This section is authoritative and supersedes any earlier prohibition on rings or connecting lines.

| Chart | Form | Rules |
|---|---|---|
| `ScoreRing` (C-02) | dual concentric ring, outer = current closed period in its band colour, inner grey = previous period, tick at the 85-point threshold | animated draw-in on mount; the numeral inside is the value of record; a ring is never the only place a number appears |
| `TotalScoreTrack` (C-03) | linear 0–100 track with ticks at 40 and 85 | true linear: 1 point = 1 % of width, so marker and band edges cannot disagree. Negative score pins left **and** is stated numerically |
| `ScoringAxis` (C-10) | ordinal ladder, one equal-width cell per existing scoring band | **intentionally ordinal** — the bands are unequal and open-ended, so equal widths are the honest encoding. Rows: band label / colour cell / points lost / marker / caption |
| `PeriodTrend` (C-11) | one bar per closed reporting period, each in its own band colour, plus a **connector line through the bar tops** and a dot at each top; reference lines at 85 and 40 | `BC` Every point is a discrete closed period, individually labelled with its full date range and its value. The connector shows direction only. Nothing between two bars is data: no smoothing, no interpolated intermediate points, no area fill, no live/streaming metaphor |
| `LossDistributionRing` (C-07b) | ring of category losses with a live centre readout, driven by the legend | `BC` The ring is enrichment: segments are `aria-hidden`, the legend rows are the real controls, and every category + its points lost is present as text. Palette per §3.5 |
| `CategoryChangeRows` (C-12) | per category: previous → current coefficient as text, a pair of points-lost bars with printed values, and a signed delta chip | bar height is scaled by the largest loss on screen and clamped inside its track |

Banned: gauges/speedometers, 3-D, decorative gradient fills on data, animated counting numerals, sparklines without labels, any chart whose unit is not stated.

## 6. Canonical category labels

`BUSINESS/DATA CONTRACT` — resolves AS-IS `G-03`. The `AREA_SCORE_METRICS` label set is canonical; `VALIDATION_LABELS` strings (`top_1_validation`, `top_2_validation`) must be **mapped** to these, never rendered.

| `key` | Canonical label (UI) | Short label (grid column) | `points_max` |
|---|---|---|---|
| `overrev` | Nadmierne obroty | Obroty | 15 |
| `harsh_braking` | Gwałtowne hamowania | Hamowania | 10 |
| `harsh_acceleration` | Gwałtowne przyspieszenia | Przyspieszenia | 10 |
| `harsh_turning` | Gwałtowne skręty | Skręty | 10 |
| `idle` | Postój na biegu jałowym | Postój | 10 |
| `speeding_140_160` | Prędkość 140–160 km/h | 140–160 | 15 |
| `speeding_160_170` | Prędkość 160–170 km/h | 160–170 | 15 |
| `speeding_170_plus` | Prędkość powyżej 170 km/h | >170 | 15 |

Over-rev stays in the 100-point model and in the list, visually de-emphasised (62 % opacity) with the neutral caption **"pełne 15 pkt w obecnym okresie"**. `BC` Do not state a technical cause the audit did not establish (no "brak pomiaru", no "measurement unavailable"), and do not include the category in coaching.

## 7. Iconography

No icon library. Typographic glyphs only, so they render identically everywhere and survive copy-paste into plain text:

`✓` full points · `!` elevated · `✕` losing points · `–` not evaluated · `▲`/`▼` direction and current marker · `◇` target band · `▸`/`▾` disclosure · `★` newly ranked · `◎` score · `≡` distance.

The programme mark is a 42 px rounded square with the accent gradient. `VISUAL DESIGN DECISION`: no logo file required; a supplied logo replaces it at the same optical size.

## 8. Number formatting

`BUSINESS/DATA CONTRACT`:

- Locale `pl-PL`; decimal comma; thousands separated by a narrow no-break space (`3 418`).
- Dates `DD.MM.YYYY`; ranges `DD.MM – DD.MM.YYYY`. The end date shown is always `period_end_date − 1 day`.
- Scores and points are integers. Coefficients are the integer scoring coefficients already rounded on the host (AS-IS `G-06`).
- Percentages: one decimal (`52,5 %`), non-breaking space before `%`.
- Deltas always signed with a direction glyph **and** a unit: `▲ 4 pkt`, `▼ 3 na 100 km`, `= bez zmian`.
- Typographic minus (`−5`), never a hyphen, for negative point values.
- Distance in whole kilometres, unit always present.
- **Never recompute a score or a coefficient in the browser.** Numbers are computed with `Decimal`/`ROUND_HALF_UP` on the host and shipped (AS-IS `TECHNICAL_PLATFORM_HANDOFF` §5.8). The frontend formats and lays out; the only client-side arithmetic permitted is layout maths (bar heights, ring offsets, marker positions).

## 9. Motion

`VISUAL DESIGN DECISION`, deliberately restrained: card/section entrance `fadeUp` 0.5–0.9 s staggered; ring draw-in 1.4–1.7 s; trend bars grow in sequence (150 ms apart) and the connector fades in after the last bar; disclosure 140 ms; hover 100–180 ms. No counting numerals, no looping animation, no chart redraw on scroll. `prefers-reduced-motion: reduce` → all transitions and animations 0 ms, final state rendered immediately.
