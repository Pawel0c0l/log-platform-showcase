# Eco Driving — Analytics Spec

> **Scoring logic is UNRESOLVED (`D-002`).** Every threshold, point weight, maximum and rating band in the design files is **placeholder data invented for layout**. The owner has confirmed the numbers are not real. This document specifies the **structure, presentation and interaction** — which are approved — and marks every point where a real value must come from the repository as `⟨FROM REPO⟩`. **Do not implement the numbers from the design files.**

The drill-down chain:

`Klient` → `Miesiąc` → `Wybrane tygodnie` → `Ranking` → `Kierowca / ID` → `Metryki składowe` → `Przejazdy źródłowe`

---

## Level 1 — Client

| Aspect | Specification |
|---|---|
| Visible context | Bordered selector in the context bar: `Klient` eyebrow + client name (`type/client-name`) + client code in a mono chip + chevron. Followed by the module name `Eco Driving`, the ranking-family badge `RANKING STANDARDOWY`, and the lineage qualifier. |
| Primary information | Which client's ranking is on screen. Each client has an **independent** ranking; a driver's position is meaningless across clients. |
| Secondary information | Ranking family / provider, data-lineage qualifier. |
| Interactions | Open the selector; choose among clients the account may access and for which Eco Driving is enabled. |
| Navigation | Changing client stays in Eco Driving. Month and week selection are preserved if the same period exists for the new client; otherwise the newest available month is selected and the basis line states the substitution. |
| Context carried forward | Client is carried into every deeper level and appears in the drill-down breadcrumb. |
| Context on return | Returning from a driver restores the same client. |

The client selector is a **bordered control** here, unlike Database Explorer's plain label, because switching client is a primary analytical action in this module.

## Level 2 — Month

| Aspect | Specification |
|---|---|
| Visible context | Stepper in the period bar: `‹ Lipiec 2026 ›`, 32 px, month name + year at `type/control` weight 600. |
| Primary information | The reporting period is a **complete calendar month**. |
| Interactions | `‹` and `›` step one month. The forward control is **absent** (not disabled) at the newest month with data. |
| Navigation | Changing month recomputes the ranking and **resets week selection to `Cały miesiąc`**, because week identity is month-relative. |
| Context carried forward | Month appears in the basis line and in the drill-down breadcrumb. |
| Context on return | Preserved exactly. |

## Level 3 — Selected weeks (`D-008`)

| Aspect | Specification |
|---|---|
| Visible context | A row of toggle cards, each 32 px tall and ≥104 px wide, showing the week label (`W1`) on line one and its date range (`01–07.07`) on line two. Selected cards use `accent/tint` background, `accent/on-surface` border and `accent/text-on-tint` text. Above them: `Cały miesiąc` and `Wyczyść` shortcuts. |
| Primary information | Which weeks form the ranking basis. |
| Semantics | **Selected weeks are summed into one combined ranking.** Never parallel per-week rankings. |
| Valid selections | One or more weeks. **Non-contiguous selection (W1 + W3) is permitted with an inline, non-blocking warning** stating that the basis has a gap. Zero weeks is an empty state prompting selection, not an error. |
| Partial weeks | A trailing week shorter than 7 days is labelled `częściowy` in its date caption and its border steps to `border/checkbox` weight. The basis line states the true day count. |
| The basis line | A single sentence is **the sole source of truth** for what is on screen: `Podstawa rankingu: **W1 + W2** zsumowane · 01–14.07.2026 · 14 dni`, followed by `498 z 1 512 kierowców spełnia próg kwalifikacji · 21 407 przejazdów · 1 284 916 km`. It **MUST** be present on `ECO-001`/`ECO-002` and echoed in the `ECO-003` breadcrumb as `Lipiec 2026 · W1 + W2`. |
| Interactions | Toggling a card recomputes immediately. |
| Context carried forward | Week set is carried into the drill-down and into the trip table's period filter. |

**Do not fix the interaction to a specific widget beyond what is drawn here.** The approved solution is toggle cards with visible date ranges, chosen because dropdowns hide the selection, tabs imply mutual exclusivity, and a segmented control cannot express "some of these".

## Level 4 — Ranking

| Aspect | Specification |
|---|---|
| Visible context | Client selector, module name, ranking family, lineage qualifier, month stepper, week cards, basis line, fleet distribution histogram with median and mean. |
| Primary information | **Position and score, equally prominent.** Position: first column, `type/table-position` (16 px mono 600), sticky-left. Score: `type/table-metric` (17 px mono 600) with a proportional bar in `viz/score-*` on a `viz/bar-track`, in an `accent/tint` column. |
| Identity | Driver display name (`type/table-cell` sans, weight 500) plus `Driver tag` (mono). The **tag is the stable identifier**; the name comes from the current driver record and is not a historical record. |
| Position change | `Δ` column: `+n` in `state/positive`, `−n` in `state/negative`, `0` in `text/muted`. Compares to the previous comparable period. |
| Rating band | Word badge (`bardzo dobra` / `dobra` / `przeciętna` / `wymaga uwagi`) with semantic colour. Band thresholds ⟨FROM REPO⟩. |
| Volume | `Dystans (km)`, `Przejazdy` — right-aligned mono. These are context for interpreting the rates. |
| Metric columns | Eight, in this order: `Ostre hamowania`, `Przyspieszenia`, `Ostre skręty`, `Długi postój`, `Wysokie obroty`, `> 140`, `> 160`, `> 170`. |
| Metric unit | **Events per 100 km by default.** A single `/ 100 km ⇄ Σ suma` toggle in the toolbar switches **all eight columns at once**; each header's second line states the current unit (`/ 100 km` or `Σ suma`). Per-column unit toggles are explicitly rejected — eight independent states would be unreadable. |
| Qualification | `Kwalifikacja` column states whether the driver met the minimum-distance threshold. Threshold value ⟨FROM REPO⟩. |
| Fleet distribution | 24-bucket histogram in the period bar with median and mean stated numerically. Required because a bare `43 / 498` is not interpretable. |
| Row interaction | Rows are **not** wholly clickable. `Szczegóły` in the last column navigates to `ECO-003`. |
| Sorting / filtering | Same vocabulary as Database Explorer: column-header menus, active-filter chips, text search over driver name, tag ID and registration. Default sort: score descending. |
| Groups (`D-004`) | **One ranking.** `Grupa = w rankingu` is a removable chip; `Wykluczeni 982 · Nieznany kierowca 32` are visible counts, reachable by changing the filter. Not tabs — tabs would imply three equal rankings. |
| Pagination | Same model as Database Explorer (`D-012`): pages of 100 by default, plus a `skocz do kierowcy` field accepting a surname or a position number. |
| Footer metadata | `ranking przeliczony <timestamp>`. |

### Rate vs sum — the rule

- **Ranking level: rate per 100 km by default.** Drivers in the sample span roughly 400–6 000 km per period; a raw event count is not comparable across them.
- **Trip level: always Σ sum.** A trip is a single event in time; a rate for one trip is meaningless.
- The toggle exists so a user can see one in terms of the other without leaving the screen. Its state is part of the saved view.

## Level 5 — Driver / person / ID (`ECO-003`)

A single scrollable page with six sections in a fixed order. Each answers one question.

### 5.1 Score and position — *"where do I stand?"*

- Driver name (`type/detail-title`), then `driver tag <n> · dysponent <id> · <registration>` in mono.
- Score: `type/metric-hero` (52 px mono 600) with `/ 100` suffix. Position: same treatment with `/ <participants>`. Deliberately identical weight — neither dominates.
- Rating band badge and a percentile statement (`górne 9% floty`).
- The fleet distribution histogram repeated with **this driver's bucket marked** in `viz/bar-accent` and the axis labelling the driver's score.

### 5.2 Trend — *"am I improving?"*

- The same driver's score across the last 8 comparable periods, as a bar per period with the value above and the period label below. The current period is `viz/bar-accent`; earlier periods `viz/bar-neutral`.
- Footnotes: change versus the previous period, best period, worst period, and a `Porównaj z flotą` action.
- Fewer than 8 comparable periods renders only what exists. **No synthetic zeros.**

### 5.3 Entry identity and period — *"what exactly am I looking at?"*

A four-column grid of 16 persisted fields:

`Klient` · `Dostawca / rodzina rankingu` · `Typ okresu` · `Utrwalona etykieta okresu` · `Początek okresu` · `Koniec okresu (wyłączny)` · `Stan częściowości` · `Numer okresu w miesiącu` · `Przypisane ID (nieprzejrzyste)` · `Grupa rankingowa` · `Status kwalifikacji` · `Status obliczeń` · `Źródło metadanych kierowcy` · `Uczestnicy rankingu` · `Udział pasma oceny` · `Dystans łącznie`

Requirements:
- The period end is **exclusive** and the label says so.
- The assigned ID is an **opaque** value and the label says so.
- A closing statement **MUST** distinguish persisted from live data: ranking group, position and score are persisted; the driver's display name comes from the current driver record and is **not** an immutable historical record.
- Distance is given in both metres (the stored unit) and kilometres.

### 5.4 Score composition — *"what is hurting my score?"*

One row per metric, sorted by **points lost, descending**:

| Column | Content |
|---|---|
| `Metryka` | Metric name |
| `Σ zdarzeń` | Event sum in the basis |
| `/ 100 km` | Rate, 2 decimals, comma separator; in `state/negative` when over threshold |
| `Próg` | The threshold that applies ⟨FROM REPO⟩ |
| `Punkty` | Points awarded ⟨FROM REPO⟩ |
| `Maks` | Maximum for this metric ⟨FROM REPO⟩ |
| `Utracone` | **Points lost**, in an `accent/tint` column, weight 600, coloured by magnitude |
| `Udział w utraconych punktach` | Proportional bar + percentage of total loss |

The header states the arithmetic: `100 pkt bazowo · utracone 22 pkt · sortowanie po utraconych`. A `/ 100 km ⇄ Σ suma` toggle mirrors the ranking toggle.

**Sorting by points lost, not by points earned, is deliberate**: the user's question is "what is hurting me", not "what did I score".

The number of metrics, their names, thresholds, weights and the aggregation formula are all ⟨FROM REPO⟩. The *structure* — per-metric threshold → points awarded → points lost → share of total loss, with a stated base and total — is approved.

### 5.5 Week contribution — *"which week caused this?"*

- A compact table: metric rows × `W1` / `W2` / `Δ` columns, covering score, distance and each violation metric.
- `Δ` is coloured by whether the change is good or bad for the score, not by sign.
- A closing note **MUST** state: the ranking is computed on the **summed** data; the per-week breakdown is diagnostic and is **not** a separate ranking.

### 5.6 Contributing trips — *"show me the evidence"*

| Column | Unit |
|---|---|
| `Start podróży` | date + time |
| `Koniec podróży` | date + time |
| `Dystans` | km |
| `Czas jazdy` | h : mm |
| `Nr rej.` | vehicle |
| `Kierowca` | current driver record |
| `Driver tag` | ID |
| `Dysponent` | ID |
| `Ostre ham.` | Σ |
| `Przysp.` | Σ |
| `Skręty` | Σ |
| `Długi postój` | Σ |
| `Wys. obroty` | Σ |
| `> 140` | Σ |
| `> 160` | Σ |
| `> 170` | Σ |
| `Wkład w wynik` | points, `accent/tint` column |

- **All violation values are Σ sums for that trip.** Column captions state `Σ`.
- The table is **fully sortable and filterable from its column headers**, with active-filter chips, a text search (registration, trip ID, dispatcher) and `Dodaj filtr` — the same vocabulary as Database Explorer.
- **Default filter:** `Wkład w wynik < 0` plus the period range, with the count stated (`18 z 64 przejazdów po filtrach`) and `Pokaż wszystkie 64 przejazdy` one click away.
- The trip identifier links to the same row in Database Explorer — same dataset, same permission model. This closes the chain to the raw record.
- `Eksport przejazdów` exports the current trip view.
- Footer states that the same rows are available in Database Explorer, read-only.

## Level 6 — Raw row

Handled by `DB-003` + `DB-006`. Eco Driving's only obligation is to link there with the correct dataset, row identity and period filter, and to respect that the target may require a permission the account does not hold.

---

## Context visibility guarantee

The user **MUST** be able to answer "which client, which month, which weeks" at every level without navigating:

| Level | Client | Month | Weeks |
|---|---|---|---|
| Ranking | Context-bar selector | Period-bar stepper | Week cards + basis line |
| Driver detail | Breadcrumb + identity grid | Breadcrumb + identity grid | Breadcrumb + identity grid + week-contribution table |
| Trip table | Inherited from the page | Basis line in the section header | Section header states the range |
| Raw row in `DB-003` | Database Explorer context bar | Applied as a filter chip | Applied as a filter chip |

## States

See `SCREEN_STATE_MATRIX.md` for `ECO-001`–`ECO-003`. Two deserve emphasis:

- **Row-evidence permission.** The current product separates access to the ranking summary from access to contributing trips. Without the trip grant, section 5.6 renders an explanatory message **in place of** the table. It **MUST NOT** silently disappear.
- **Context switch from the drill-down (`D-009`).** Driver present in the new context → stay and recompute. Driver absent → return to the ranking with a message naming the driver and the reason.

## Deliberately removed this iteration (`D-003`)

| Removed | Reason | Return condition |
|---|---|---|
| Reconciliation panel (persisted vs reconstructed, per-field discrepancy, diagnostics) | No tooling exists yet to track discrepancies | When discrepancy tracking exists |
| Score-definition panel (threshold rules per metric, rating bands) | Superseded by `D-002` — the real rules are unknown | With the real scoring logic |
| Per-row lineage badge | Constant for a period; moved to the context bar once | — |

These were present in the as-is product. Their removal is an owner decision, not an oversight, and the specification records it so that Claude Code does not "restore" them as a perceived regression.
