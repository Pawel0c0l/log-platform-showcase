# VISUAL_REFERENCES

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`

High-fidelity references. Both files are self-contained HTML — open them in any browser, no build step, no network dependency beyond the Google Fonts link (they degrade to system fonts offline).

---

## 1. Files

| File | What it contains |
|---|---|
| `../Eco Driving Dashboard.dc.html` | the working product reference: all four screens, data/eligibility states, mobile references, access states, and the invariants panel |
| `../Eco Driving Design System.dc.html` | tokens and component anatomy: type scale, colour/status matrix, scoring-axis anatomy, chip and marker inventory, day-row variants |

## 2. How to drive the product reference

A dark control bar at the top — **labelled "Wzorzec projektowy / nie część produktu"** and not part of the design — switches:

- **Okres**: `Tygodniowy` / `Miesięczny`
- **Widok**: `Podsumowanie` / `Szczegóły`
- **Stan** (dropdown): nine data states

The four required desktop references are the four combinations of the first two controls, at the default state:

| Reference | Controls |
|---|---|
| Weekly Summary desktop | Tygodniowy + Podsumowanie |
| Weekly Detailed desktop | Tygodniowy + Szczegóły |
| Monthly Summary desktop | Miesięczny + Podsumowanie |
| Monthly Detailed desktop | Miesięczny + Szczegóły |

The mobile references (390 px Summary and 390 px Detailed) are rendered **below** the desktop dashboard in the section "Wzorzec mobilny · 390 px", and they follow the current period/state selection. Access states are in the section below that.

State selector coverage:

| Option | Demonstrates |
|---|---|
| Kierowca akceptowalny, w rankingu | the default: score 71, rank 18/158 ▲6, four coaching insights, two near-threshold cards |
| Kierowca bezpieczny, w rankingu | green classification, marker in the top band, opportunity-only coaching |
| Kierowca niebezpieczny, w rankingu | red classification, rank moving down, several red categories |
| Poza rankingiem (stan EXCLUDED) | `RankingNotice`, no rank, no group share, everything else intact |
| Nowo w rankingu | `★ Nowo w rankingu`, no fabricated delta |
| Wypadł z rankingu w tej migawce | previous position named with dates, no delta |
| Pierwszy zamknięty okres miesiąca | "brak bazy" deltas, single-bar trend, comparison explanation |
| Poniżej 100 km w całym okresie — dane ukryte | global `INSUFFICIENT_DISTANCE` state; no score, rank, category data, distance value, trends, coaching or daily rows are shown |
| Raport niegotowy — dashboard nie jest renderowany | the fail-closed `REPORT_NOT_READY` screen: no partial score anywhere |

## 3. Component references worth reading closely

| Where | Why it matters |
|---|---|
| Score card → "Oś wyniku całkowitego" | the redesigned total-score axis: true linear 0–100, ticks at 40/85, current marker, previous-period ghost marker, distance-to-next-band |
| Category table → any expanded row | the scoring axis: band labels, points-lost row, `▲` current marker, `◇` target marker with its gain, and the three explanation panels |
| Category row "Prędkość 160–170 km/h" | 8 raw events but coefficient 0 → **green**. The clearest demonstration that count ≠ status |
| Detailed → 06.07 | a day with no qualifying driving: neutral cells, "brak jazdy" chip, no aggregate day colour |
| Detailed → any two cells with the same count | different tints because their exposure differs — the count/colour separation in one glance |
| Detailed → any two rows with the same count | different colours because their distances differ |
| Coaching card 4 | the "highest-value opportunity" insight with its deterministic derivation in the value line + factual explanation sentence |

## 4. Data in the references

`BUSINESS/DATA CONTRACT`: **all values are synthetic.** No production driver name, e-mail address, vehicle registration, coordinate, driver identifier or token appears anywhere. The synthetic figures were chosen to be internally consistent with the real scoring tables:

- weekly current snapshot `01.07 – 19.07.2026`: score **71** = `100 + (0 −6 −5 −3 −10 −5 +0 +0)`, basis `01.07 – 12.07.2026` = **67**;
- monthly closed `01.07 – 31.07.2026`: score **75** = `100 + (0 −6 −5 −3 −6 −5 +0 +0)`, previous closed month **70**;
- every coefficient shown equals `ROUND_HALF_UP(count / km × 100)` for the distance shown, and every band/points pair is taken from `SCORING_RULES`;
- daily rows are generated from a fixed seed so the reference is reproducible, and include deliberate edge days: a zero-distance day, a 12 km day, and a high-idling day.

## 5. Portability

Both references are single HTML files with inline styles and no bundler, so they survive being copied out of this environment. If the Google Fonts request is blocked, the layout is unchanged and the type falls back to the system grotesque/monospace stack. Screenshots taken from these files are acceptable as handoff attachments; the files themselves are the higher-fidelity artifact.
