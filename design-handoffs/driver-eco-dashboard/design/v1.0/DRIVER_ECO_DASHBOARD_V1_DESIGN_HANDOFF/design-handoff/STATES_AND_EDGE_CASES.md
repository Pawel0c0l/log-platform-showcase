# STATES_AND_EDGE_CASES

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`

One system, many states. Nothing here introduces a second visual language: each state is the same layout with a component swapped, a block removed, or a status set to `neutral`.

Copy is given verbatim in Polish where it is contractual.

---

## 1. State matrix — the 14 required states

| # | State | Trigger (snapshot) | What changes |
|---|---|---|---|
| 1 | Ranked safe | `ranking_state=RANKED`, `score ≥ 85` | classification tint green, axis marker in the green band, coaching often only `BEST_OPPORTUNITY` + `MOST_IMPROVED` |
| 2 | Ranked acceptable | `RANKED`, `40 ≤ score < 85` | amber classification; the reference default |
| 3 | Ranked dangerous | `RANKED`, `score < 40` | red classification; copy stays factual — no warnings, no alarms, no exclamation marks `BC` |
| 4 | Not ranked by configuration (`EXCLUDED`) | `ranking_state=NOT_RANKED_BY_CONFIGURATION` | `C-05` → `C-06`; `C-07` removed; **everything else identical**. No rank, no rank delta, no group share `BC` |
| 5 | Newly ranked | `RANKED` and `previous_ranking_position == null` | rank shown; movement chip = `★ Nowo w rankingu`; note "Pierwszy okres z pozycją — nie pokazujemy zmiany, bo nie było poprzedniej pozycji." **No fabricated delta** `BC` |
| 6 | Left the ranking | `ranking_state=LEFT_RANKING` | `C-06` with: "W tym okresie nie jesteś w rankingu… W poprzednim okresie (01.07 – 12.07.2026) Twoja pozycja wynosiła 33. Wynik, obszary, trendy i wskazówki działają bez zmian." No delta, no fake rank `BC` |
| 7 | Category with strong deterioration | `coefficient_per_100km > previous_coefficient_per_100km` | amber-topped `MOST_DETERIORATED` card stating the coefficient movement (and the points consequence where one exists) + signed delta in the change section; the category's own status still comes from its own bucket |
| 8 | Category close to a better threshold | `marker_band_index > 0`, one of the two smallest coefficient distances | `◇` target marker on the axis, "blisko progu" badge, and a near-threshold card: current coefficient → target threshold → deterministic points gain. Never an event budget |
| 9 | Category with zero events | `count == 0`, `coefficient == 0` | status `green`, points `15 / 15`, "pełne punkty". Note the teaching case: a nonzero count can still be `green` (8 events over 3 418 km → coefficient 0) — this is intended and is why count and colour are visually separate `BC` |
| 10 | Day with no qualifying driving | `day.kilometers == 0` | every cell for that day is `neutral` with the accessible name "brak przejazdów kwalifikujących"; the day chip reads "brak jazdy"; the row still exists so the period calendar is complete. **No minimum-distance threshold is applied** — where the existing scoring contract yields a coefficient, it is used |

`BC` **Daily distance invariant:** once the whole reporting period is qualified (≥ 100 km total), every day in that period remains eligible for the Detailed view regardless of that day's distance. A day with 1–99 km is shown normally with its actual distance and raw event sums. If `day.kilometers > 0`, calculate each available daily coefficient from that day's own distance and use the existing scoring bucket for the category status/colour. There is no 50 km daily threshold and no 100 km daily threshold.
| 11 | First/early snapshot of a month | `period_sequence_in_month == 1` or no previous snapshot | `DeltaChip` = "brak bazy" (no unit); `C-01` explains why; `SnapshotTrend` shows a single bar; `MOST_IMPROVED`/`MOST_DETERIORATED` omitted |
| 12 | Closed end-of-month snapshot | final weekly snapshot ≡ the month | weekly view labels the range `01.07 – 31.07` and the sequence chip says "5. z 5"; the monthly view of the same month shows the same numbers. `BC` This identity is real (AS-IS §5.3) and must not be presented as two different results |
| 13 | No meaningful comparison | `comparison == null` or `comparable == false` | every delta becomes "brak bazy"; the change section shows a standalone sentence and no summary chips; `MOST_IMPROVED`/`MOST_DETERIORATED` are omitted; nothing is interpolated; rank comparison across the ranking-contract recalculation is suppressed (`DEP-04`) |
| 14 | Snapshot cannot satisfy the scoring contract | the generator cannot produce a complete score | **the dashboard is not published**: `REPORT_NOT_READY` replaces the content area (§5). No partial score, no partial classification, no coaching. `BC` Over-rev at full points is *not* this state — that snapshot is complete |

Plus the qualification state, which outranks all of the above:

| # | State | Trigger | What changes |
|---|---|---|---|
| 15 | Insufficient reporting-period distance | `qualification_status ∈ {LOW_DISTANCE, NO_DISTANCE}` because the **total qualifying distance for the entire reporting period is < 100 km** | **No Eco Driving data for that period is rendered or sent to the normal dashboard UI.** Show only the period identity/freshness needed to identify the closed report plus a neutral `INSUFFICIENT_DISTANCE` state explaining that the 100 km reporting-period gate was not met. Do **not** show actual distance, score, classification, rank, group share, categories, counts, trends, coaching or daily details. The 100 km rule is period-level only; it is never applied to an individual day. `BC` |

## 2. What never changes between states

`BC`:

1. The block order of the Summary view.
2. The period header and the freshness line.
3. The status semantics (`points` vs `points_max`).
4. The prohibition on empty slots: a block is either populated, replaced by a designed notice, or removed entirely — never rendered blank.
5. The prohibition on inventing a comparison.

## 3. Ranking state machine — exact behaviour

`BC`. The snapshot must carry a single derived `ranking_state`; the raw `ranking_group` / `ranking_included` values must never reach the browser (AS-IS `PRIVACY_DATA_BOUNDARIES` §3).

| `ranking_state` | Derived from (AS-IS §4.2) | Rank | Movement | Group share | Notice |
|---|---|---|---|---|---|
| `RANKED` | `ranking_group = INCLUDED` | `18 / 158` | delta or `★ Nowo w rankingu` | yes | — |
| `NOT_RANKED_BY_CONFIGURATION` | `ranking_group = EXCLUDED` | **absent from the payload** | none | none | "Ten okres jest bez rankingu — Twój wynik, obszary, trendy i wskazówki są pełne. Ten okres nie zawiera pozycji rankingowej ani udziału grupy — ranking obejmuje wybraną populację uczestników." |
| `NOT_ON_ROSTER` | `ranking_group = UNKNOWN_DRIVER` | absent | none | none | same wording as above (the driver must not be able to tell the two apart — the distinction is internal) |
| `LEFT_RANKING` | previously `INCLUDED`, now not ranked | absent | none | none | previous position may be named with its dates |

Transition rules `BC`:

- Ranked → Ranked: show current, population, previous, and the movement in places.
- Not ranked → Ranked: show current + population + `★ Nowo w rankingu`. **Never** compute a delta from an absent rank.
- Ranked → Not ranked: no delta, no current position; previous position may be shown as context with its dates.
- Not ranked → Not ranked: no ranking element at all; the dashboard stays useful via score, categories, trends and coaching.
- `EXCLUDED` in any transition: rank and group share are absent regardless of history.

## 4. Empty-value catalogue

| Value | Missing rendering | Never |
|---|---|---|
| score | normal dashboard is not published if score cannot be produced; use `REPORT_NOT_READY` | `0` or a partial dashboard |
| classification | "brak klasyfikacji" neutral pill | guessing from an incomplete score |
| delta | "brak bazy", unit suppressed | `+0`, `0 %`, `▲ 0` |
| rank | block replaced by `C-06` | `—/158`, `#0`, "poza czołówką" |
| group share | card removed | `0 %` |
| coefficient | `neutral` chip "brak" | `0 / 100 km` |
| count | "brak danych" | `0` |
| day distance | `—` + "brak jazdy" | `0 km` styled as a real value |
| trend series with 1 point | the single bar + explanation | a two-point line implying change |
| category previous value | "brak bazy" in the trend row | a zero-height bar that looks like an improvement |

## 5. Access states — verbatim copy

`BC`. Full-page for token failures; in-content for data failures. No login form, no e-mail field, no driver identity, no token echo, no statement about whether a link ever existed.

| Code | Title | Body |
|---|---|---|
| `INVALID_LINK` | Ten link nie jest prawidłowy | Otwórz panel z najnowszej wiadomości Programu Ecodriving. Linki są indywidualne i nie da się ich odtworzyć ręcznie. |
| `LINK_EXPIRED` | Link stracił ważność | Każde podsumowanie ma własny link. Najnowszy znajdziesz w ostatniej wiadomości e-mail z podsumowaniem okresu. |
| `REPORT_NOT_READY` | Raport tego okresu nie jest jeszcze gotowy | Wynik powstaje ze wszystkich obszarów punktacji razem. Gdy komplet danych będzie dostępny, zobaczysz pełne podsumowanie tego okresu. |
| `INSUFFICIENT_DISTANCE` | Za mały dystans do wyliczenia wyniku | Dla tego zamkniętego okresu łączny dystans kwalifikujący nie osiągnął 100 km. Zgodnie z zasadami Eco Driving nie pokazujemy danych ani wyniku dla tego okresu. |
| `SNAPSHOT_UNAVAILABLE` | Ten okres nie jest jeszcze dostępny | Panel pokazuje tylko zamknięte okresy sprawozdawcze. Gdy okres zostanie zamknięty i przeliczony, pojawi się tutaj. |
| `SERVICE_UNAVAILABLE` | Panel jest chwilowo niedostępny | Spróbuj ponownie za kilka minut. Twoje dane nie zostały utracone — nic nie jest liczone w tej przeglądarce. |

Implementation notes per state are printed on the reference cards in `../Eco Driving Dashboard.dc.html` (section "Stany dostępu i braku danych").

## 7. No aggregate day status, ever

`BUSINESS/DATA CONTRACT`. No state in this document produces a whole-day Eco colour. There is no `day_status` field, no worst-category rule, no average, no red-count rule, no weighted daily classification. Every colour on a daily row belongs to exactly one category and comes from that category's own coefficient and scoring bucket.

## 6. States deliberately not designed

`BC` — if a requirement for these appears later, it is a new product decision, not a gap in this package:

- in-flight / provisional current period (AS-IS `D-08`);
- a dashboard rendered from an incomplete scoring snapshot (replaced by `REPORT_NOT_READY`);
- any aggregate day classification;
- isolated week-over-week comparison (AS-IS `D-03`, resolved as cumulative);
- any leaderboard, peer list, or distribution of individual scores;
- any admin/fleet-manager view;
- account creation, login, password recovery, or link self-service.
