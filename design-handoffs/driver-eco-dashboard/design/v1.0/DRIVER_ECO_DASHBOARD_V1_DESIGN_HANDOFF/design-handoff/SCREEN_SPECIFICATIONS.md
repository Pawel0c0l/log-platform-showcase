# SCREEN_SPECIFICATIONS

**Global qualification precondition:** the four primary screens exist only for reporting periods with total qualifying distance `>= 100 km`. A whole-period total `< 100 km` renders only `INSUFFICIENT_DISTANCE` and no Eco data or daily detail. The threshold is never applied per day; 1–99 km days remain visible once the whole period qualifies.

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`

The four primary experiences, block by block. Components are referenced by their `C-nn` ids from `COMPONENT_SPECIFICATIONS.md`. Live reference: `../Eco Driving Dashboard.dc.html` (state selector + period/view switches reproduce every screen below).

Grid: max width 1240 px, app shell = one card, 24 px internal padding, section gap 28–30 px.

---

## 1. Shell (all four screens)

```
┌ app header ─────────────────────────────────────────────────────────────┐
│ [■] PROGRAM ECODRIVING            (chip) Dane z zamkniętego okresu …    │
│     Twoje Eco Driving                    Dane zaktualizowane: …      │
├ nav ────────────────────────────────────────────────────────────────────┤
│ Podsumowanie | Szczegóły dzienne                  ( Tygodniowy Miesięczny )│
├ C-01 PeriodHeader ──────────────────────────────────────────────────────┤
│ OKRES … — LICZONY OD 1. DNIA MIESIĄCA            BAZA PORÓWNANIA                │
│ 01.07 – 19.07.2026                       01.07 – 12.07.2026             │
│ [Narastająco od 01.07 …] [3. z 5 …]      Poprzedni zamknięty okres …  │
└─────────────────────────────────────────────────────────────────────────┘
```

- No driver name anywhere. `BC`
- The freshness chip and the `snapshot_updated_at` line are always present, on every screen, in the same place. `BC`
- Header height is fixed across screens so switching views does not shift the page.

## 2. `W-SUM` — Weekly Summary

Block order (top to bottom):

| # | Block | Layout | Notes |
|---|---|---|---|
| 1 | `C-01` PeriodHeader | full width | weekly variant: cumulative chip + sequence chip + the "19 dni vs 12 dni" sentence |
| 2 | `C-02` ScoreCard + `C-03` TotalScoreAxis + `C-04` DeltaChip | left column of a 2-column `auto-fit minmax(310px)` grid | classification tint fills the card; axis sits below a dashed divider inside the same card |
| 3 | `C-05`/`C-06` Ranking, `C-07` GroupShare, `C-08` Distance | right column, stacked | order fixed: ranking → group → distance |
| 4 | `C-13` CoachingGrid | full width, 4 columns | section heading "Co teraz najbardziej wpływa na Twój wynik" + the line "Wnioski wyliczone deterministycznie z tabeli punktowej — bez prognoz." |
| 5 | `C-09` CategoryTable (8 rows) | full width | heading "Punkty według obszarów" + the line "Liczba zdarzeń → wskaźnik na 100 km → próg punktowy. Kolor pochodzi z progu, nie z liczby zdarzeń." `LARGEST_LOSS` row expanded showing `C-10` |
| 6 | `C-11` SnapshotTrend + `C-12` CategoryTrendList | 2 columns | trend title "Kolejne zamknięte okresy lipca"; footnote states the cumulative model |
| 7 | Status legend | full width | four tint swatches with words: pełne punkty · podwyższony · strata punktów · brak oceny |

Reference values in the visual artifact (synthetic): score 71 (akceptowalny), basis 67, rank 18 / 158 (▲ 6), group share 52,5 %, distance 3 418 km (▲ 1 312), snapshots 58 → 67 → 71.

Weekly-specific requirements:
1. The word "tydzień" appears only inside dated segment labels in the Detailed view, never as a claim about the headline number. `BC`
2. The trend footnote must state that snapshots do not sum. `BC`
3. The comparison basis must show both ranges, not "vs poprzednio". `BC`

## 3. `W-DET` — Weekly Detailed

| # | Block | Notes |
|---|---|---|
| 1 | `C-01` PeriodHeader | identical to `W-SUM` |
| 2 | Intro line | "Okres tygodniowy obejmuje wszystkie dni od 01.07 do 19.07. Najnowszy odcinek jest rozwinięty, wcześniejsze tygodnie pozostają zwinięte i policzone." |
| 3 | Controls | "Rozwiń wszystkie tygodnie" |
| 4 | Day KPIs (4) | dystans w okresie · dni z jazdą · dni z wyliczonym wskaźnikiem · wskaźnik okresu ("liczone raz dla całego okresu") |
| 5 | Day grid | Excel-style: one column per category (event sums), week-group sum rows, dark period-totals row; no day-status column, no glyphs in cells |
| 6 | Legend | count-vs-colour · neutral threshold · daily points not additive |

Scope rule (owner answer): the table lists **all days of the MTD snapshot**, grouped by Monday-anchored segments within the month; the newest segment is expanded by default and the earlier ones are collapsed with a summary. `BC`

## 4. `M-SUM` — Monthly Summary

Same block order as `W-SUM`, with three differences (`BC`):

1. `C-01` eyebrow reads "Okres miesięczny — zamknięty miesiąc"; the chip reads "Miesiąc zamknięty 01.08.2026"; the comparison basis is `01.06 – 30.06.2026` with the sentence "Oba okresy są pełnymi miesiącami, ale mają różny dystans — porównuj wskaźniki, nie liczby zdarzeń."
2. `C-11` becomes "Przebieg wewnątrz zamkniętego miesiąca": the MTD snapshots of that month as the within-month shape, subtitled with the previous month's final score, which also appears as a reference line — never as a bar in the same series.
3. `C-04` compares closed month vs closed month.

Reference values: score 75 (akceptowalny), previous month 70, rank 15 / 158 (▲ 6), distance 5 902 km (▲ 688), series 58 → 67 → 71 → 73 → 75.

If a month's weekly snapshots were never produced (AS-IS: `2026-08-W1` was skipped), `C-11` renders only the snapshots that exist, with the footnote "Przebieg pokazuje tylko zamknięte okres, które zostały wyliczone." `BC`

## 5. `M-DET` — Monthly Detailed

Same structure as `W-DET`, tuned for 28–31 rows:

| Aspect | Behaviour |
|---|---|
| Grouping | five Monday-anchored segments; **all collapsed** except the segment with the most `red` days |
| Group header | range · segment distance · reporting-day count · "N dni ze stratą" |
| Compact mode | hides zero-count green chips, leaving only what cost points |
| Sticky | column header sticks below the app nav while scrolling |
| Intro line | "Zamknięty miesiąc to 31 dni. Tygodnie są zwinięte; rozwiń odcinek, aby zobaczyć dni. Kolor każdej komórki pochodzi ze wskaźnika dnia na 100 km, nie z liczby zdarzeń." |
| Never | a flat 31-row grid with no grouping, and no horizontal scrolling on any viewport |

## 7. Status colour on every screen

`BUSINESS/DATA CONTRACT`, identical on all four screens: every green/yellow/red pixel comes from
`raw violations + qualifying exposure/distance → Eco violation coefficient → existing scoring bucket/threshold → semantic colour`.
A displayed count is never the source of a colour, two identical counts may differ in colour, and no screen assigns a whole day one Eco colour.

## 6. Screen-level acceptance checks

For each of the four screens, an implementation is correct when:

1. The period header states the range **and** the comparison basis with explicit dates.
2. The score, classification and axis agree: the marker sits at `score` % and inside the band that the 85/40 thresholds imply.
3. Exactly one of `C-05` / `C-06` is present.
4. `C-07` is present only with a rank and a score.
5. Every category row shows count, coefficient and points — and the colour comes from the coefficient's band.
6. Coaching shows between one and four explainable cards; each exposes the triggering metric/change and scoring consequence through its value line + factual sentence, with no literal "Dlaczego" field, and never a card whose inputs are missing.
7. Detailed rows never display daily points; a day with no qualifying driving is `neutral`; no row carries an aggregate day status.
8. Nothing on the screen updates itself over time (no polling, no live clocks).


## 8a. `INSUFFICIENT_DISTANCE` — reporting-period eligibility gate

`BUSINESS/DATA CONTRACT`. If the **total qualifying distance of the entire closed reporting period is below 100 km**, none of Weekly Summary, Weekly Detailed, Monthly Summary or Monthly Detailed render. Keep only the minimal shell needed to identify the period/freshness and replace the content with one neutral centred panel:

- title **"Za mały dystans do wyliczenia wyniku"**;
- body: **"Dla tego zamkniętego okresu łączny dystans kwalifikujący nie osiągnął 100 km. Zgodnie z zasadami Eco Driving nie pokazujemy danych ani wyniku dla tego okresu."**;
- do not show the actual period distance, score, classification, rank, group share, categories, raw event counts, trends, coaching, or daily rows.

The 100 km rule applies only to the entire reporting period. It must **never** suppress a daily row inside an otherwise qualified period: days with 1–99 km remain visible with their distance and raw counts, and their per-category status uses the daily coefficient.

## 8. `REPORT_NOT_READY` — the fail-closed screen

`BUSINESS/DATA CONTRACT`. When the period's snapshot cannot satisfy the complete Eco scoring contract, none of the four experiences render. The shell (header, freshness, period card) stays and the content area is replaced by a single centred panel:

- title **"Raport tego okresu nie jest jeszcze gotowy"**;
- one paragraph: the score is built from all scoring areas together, the complete data for this period is not available yet, so no partial score is shown;
- an implementation note visible in the reference: a snapshot that fails the scoring contract is **not published** as a dashboard.

No partial score, no partial classification, no coaching, no ranking, no category table. Over-rev at full points is **not** this state — that snapshot is complete.
