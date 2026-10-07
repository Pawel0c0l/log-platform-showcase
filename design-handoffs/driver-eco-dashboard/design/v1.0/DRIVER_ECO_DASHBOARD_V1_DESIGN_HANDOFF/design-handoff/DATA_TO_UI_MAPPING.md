# DATA_TO_UI_MAPPING

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`

Every displayed element → the AS-IS data concept behind it → the snapshot field the UI expects. This does **not** redefine backend schemas; it defines what the driver snapshot must contain and what the frontend is allowed to assume.

Classes follow the AS-IS audit: `ALREADY STORED` · `DERIVABLE` · `NEW DERIVATION` · `NEW PERSISTENCE`.

---

## 0. DISPLAY VALUE vs SEMANTIC / SCORING VALUE — read this first

`BUSINESS/DATA CONTRACT`. Every number in this dashboard is one of two kinds, and they must never be conflated:

| | `DISPLAY VALUE` | `SEMANTIC / SCORING VALUE` |
|---|---|---|
| Example | `raw_count = 7` speeding events | `coefficient_per_100km = 2` for that exposure |
| Role | describes what happened, shown as plain text | determines the scoring bucket, the points and therefore the **colour** |
| May drive colour? | **never** | **always — it is the only source** |
| Where it appears | category rows, daily grid cells, distance, trip counts | status tints, chips, band markers, classification, coaching selection |

The full chain, identical everywhere in this package:

```
raw violations + qualifying exposure/distance
    → Eco violation coefficient (integer, ROUND_HALF_UP, computed on the host)
    → existing Eco scoring bucket / threshold
    → points vs points_max
    → green / yellow / red semantic state
```

Consequences the implementation must honour: two rows showing the same raw count may legitimately have different colours; a cell may show `8` and be green while another shows `3` and is red; no UI element may compute a status from a count; and no whole day is ever assigned one aggregate Eco colour (statuses exist per category only).

For each UI concept, the table in §2 states which of these it needs: current-period value, comparison value, coefficient, raw count, points earned/lost, scoring threshold/bucket, ranking eligibility, classification, group share, daily distance, per-category daily coefficient + status, or coaching inputs.

## 1. Snapshot contract the UI expects

One JSON document per driver per period type, **presentation-oriented**: it carries only what the dashboard renders. `BC` No `driver_key` and no `client_code` in the browser payload — the Worker resolves identity internally to pick the R2 object, and that resolution does not become a dashboard field. The browser receives **exactly one driver's snapshot** (AS-IS `TECHNICAL_PLATFORM_HANDOFF` §6).

```jsonc
{
  "schema_version": 1,
  "generated_at_utc": "2026-07-20T04:40:11Z",
  "timezone": "Europe/Warsaw",
  "locale": "pl-PL",
  "constants": {
    "min_qualifying_distance_km": 100,   // AS-IS MIN_QUALIFYING_DISTANCE_METERS / 1000
    "rating_thresholds": { "safe": 85, "acceptable": 40 },
    "score_max": 100
  },
  "periods": {
    "weekly":  { "current": <PeriodBlock>, "previous": <PeriodBlock|null>, "series": [<SeriesPoint>] },
    "monthly": { "current": <PeriodBlock>, "previous": <PeriodBlock|null>, "series": [<SeriesPoint>] }
  }
}
```

### `PeriodBlock`

```jsonc
{
  "period_type": "weekly" | "monthly",
  "period_label": "2026-07-W3",
  "period_start_date": "2026-07-01",
  "period_end_date_exclusive": "2026-07-20",
  "period_end_date_display": "2026-07-19",
  "period_sequence_in_month": 3,
  "closed_snapshots_in_month": 5,
  "is_partial_period": false,
  "scoring_complete": true,           // false ⇒ the snapshot is NOT published as a dashboard
  "snapshot_updated_at_utc": "2026-07-20T04:40:11Z",

  "qualification_status": "QUALIFIED" | "LOW_DISTANCE" | "NO_DISTANCE",
  "scoring_complete": true,
  "ranking_state": "RANKED" | "NOT_RANKED_BY_CONFIGURATION" | "NOT_ON_ROSTER"
                 | "LEFT_RANKING",

  "eco_score_total": 71,
  "rating_type": "safe" | "acceptable" | "dangerous" | null,
  "ranking_position": 18,                 // present ONLY when ranking_state == RANKED
  "ranking_total_participants": 158,       // same condition
  "rating_group_share_percent": 52.5,      // same condition
  "rating_group_distribution": { "safe": 39.2, "acceptable": 52.5, "dangerous": 8.3 },

  "total_kilometers": 3418,
  "trips_count": 214,

  "comparison": {
    "kind": "PREVIOUS_CUMULATIVE_SNAPSHOT" | "PREVIOUS_CLOSED_MONTH" | null,
    "basis_start_date": "2026-07-01",
    "basis_end_date_display": "2026-07-12",
    "comparable": true,                    // false across the ranking-contract recalculation (DEP-04)
    "previous_eco_score_total": 67,
    "previous_ranking_position": 24,       // null when the driver was not ranked then
    "previous_total_kilometers": 2106
  },

  "categories": [ <Category> ],            // always all 8, in REQUIRED_METRICS order
  "coaching": [ <Insight> ],               // 0–4 entries
  "days": [ <Day> ]                        // materialised at generation time
}
```

### `Category`

```jsonc
{
  "key": "idle",
  "label": "Postój na biegu jałowym",      // canonical label set (VISUAL_SYSTEM §6)
  "short_label": "Postój",
  "not_measured": false,                    // true for overrev at report_207 clients
  "count": 168,
  "coefficient_per_100km": 5,               // integer scoring coefficient, already rounded
  "points": 0,
  "points_max": 10,
  "points_lost": -10,
  "status": "green" | "yellow" | "red" | "neutral",
  "bands": [ { "label": "0", "upper_bound": 0, "points_lost": 0, "status": "green" }, … ],
  "marker_band_index": 3,                   // null → draw no marker
  "target_band_index": 2,                   // marker_band_index − 1, or null
  "target_points_gain": 4,
    "previous_points_lost": -10,              // null when unavailable (DEP-03)
  "previous_coefficient_per_100km": 5
}
```

### `Day`

```jsonc
{
  "date": "2026-07-14",
  "weekday_short": "Wt",
  "kilometers": 129,
  "trips_count": 5,
    "categories": [
    { "key": "idle", "count": 8, "coefficient_per_100km": 6,
      "band_label": "6", "status": "red" }
  ],
}
```

Note: `Day` carries **no points**. `BUSINESS/DATA CONTRACT` — daily points are non-additive and are not part of V1.

### `SeriesPoint` / `Insight`

```jsonc
{ "period_label": "2026-07-W2", "start_date": "2026-07-01",
  "end_date_display": "2026-07-12", "eco_score_total": 67, "is_current": false }

{ "code": "LARGEST_LOSS" | "MOST_IMPROVED" | "MOST_DETERIORATED" | "BEST_OPPORTUNITY",
  "category_key": "idle",
  "value_points": -10,
  "inputs": { "coefficient": 5, "band_label": "5", "points_max": 10,
              "previous_points_lost": -10, "kilometers": 3418,
              "previous_kilometers": 2106,
              "target_band_label": "3–4", "target_upper_bound": 4 } }
```

`BC`: coaching **sentences are composed in the UI from `inputs`**, not shipped as prose, so copy can be corrected without a snapshot regeneration — but the *selection* of the four insights happens on the host, deterministically.

## 2. Element-level mapping

| UI element | Component | Snapshot field | AS-IS source | Class |
|---|---|---|---|---|
| Period range | C-01 | `period_start_date`, `period_end_date_display` | `period_start_date`/`period_end_date` (exclusive; display = −1 day) | ALREADY STORED |
| Cumulative chip / sequence chip | C-01 | `period_type`, `period_sequence_in_month`, `closed_snapshots_in_month` | `period_sequence_in_month`, `_month_bounded_weekly_periods` | ALREADY STORED / DERIVABLE |
| Freshness line | shell | `snapshot_updated_at_utc` | `updated_at` on the stats row | ALREADY STORED |
| Comparison basis | C-01, C-04 | `comparison.basis_*`, `comparison.kind` | trend views `LAG()`; dates from the previous row | DERIVABLE |
| Eco score | C-02 | `eco_score_total` | `eco_driving_score_total` | ALREADY STORED |
| Classification pill | C-02 | `rating_type` | `ecodriving_rating_type` (85/40) | ALREADY STORED |
| Score delta | C-04 | `comparison.previous_eco_score_total` | `score_delta_abs` / `previous_snapshot_score` | ALREADY STORED |
| Score axis marker | C-03 | `eco_score_total` | linear 0–100 (design decision replaces the pixel bar) | ALREADY STORED |
| Ghost marker | C-03 | `comparison.previous_eco_score_total` | trend view | ALREADY STORED |
| Rank | C-05 | `ranking_position` | `ranking_position` — export only when `INCLUDED` | ALREADY STORED (conditional) |
| Population | C-05 | `ranking_total_participants` | persisted, never shown today (AS-IS `G-05`) | ALREADY STORED |
| Rank movement | C-04/C-05 | `comparison.previous_ranking_position` | `ranking_position_delta` (previous − current) | ALREADY STORED |
| Ranking notice | C-06 | `ranking_state` | derived from `ranking_group` after the reporting period has passed the 100 km qualification gate | NEW DERIVATION (trivial) |
| Group share | C-07 | `rating_group_share_percent` | `ecodriving_rating_type_share_percent` | ALREADY STORED |
| Group distribution | C-07 | `rating_group_distribution` | same column across the period's rows | DERIVABLE (period-level query — `DEP-06`) |
| Distance | C-08 | `total_kilometers` | `total_kilometers` (qualifying only) | ALREADY STORED |
| Distance delta | C-08 | `comparison.previous_total_kilometers` | `kilometers_delta_abs` | ALREADY STORED |
| Trip count | C-08 | `trips_count` | `trips_count` | ALREADY STORED |
| Category count | C-09 | `categories[].count` | 8 count columns | ALREADY STORED |
| Category coefficient | C-09/C-10 | `categories[].coefficient_per_100km` | `*_events_per_100km` (already rounded — AS-IS `G-06`) | ALREADY STORED |
| Category points / lost | C-09 | `points`, `points_max`, `points_lost` | `*_points`, `METRIC_MAX_POINTS`, `*_maxpoints_subtract` | ALREADY STORED |
| Category status colour | C-09/C-15 | `categories[].status` | derived: coefficient → existing bucket → `points` vs `points_max` | NEW DERIVATION (rule exists) |
| Band ladder | C-10 | `categories[].bands` | `SCORING_RULES` / `monthly-axis-contract.json` | ALREADY STORED (constant) |
| Axis marker | C-10 | `marker_band_index` | `area_marker_segment_index_from_subtract` | ALREADY STORED (logic exists) |
| Target band + gain | C-10 | `target_band_index`, `target_points_gain` | arithmetic on the existing band table; **no event-budget figure** | NEW DERIVATION |
| Category coefficient movement | C-09/C-12/C-13 | `previous_coefficient_per_100km` (scalar, never a band label) + `previous_points_lost` | per-category `LAG()` over `*_events_per_100km` and `*_maxpoints_subtract` | NEW DERIVATION (`DEP-03`, AS-IS `G-02`) |
| Snapshot trend | C-11 | `series[]` | weekly stats rows of the month | DERIVABLE |
| Coaching | C-13 | `coaching[]` | `LARGEST_LOSS` from points lost; `MOST_IMPROVED`/`MOST_DETERIORATED` from **coefficient movement**; `BEST_OPPORTUNITY` from coefficient vs next existing threshold + deterministic points gain | NEW DERIVATION (AS-IS §6.2) |
| Day rows | C-14 | `days[]` | `eco_*_trip_assignments` grouped by `(trip_start_ts AT TIME ZONE 'Europe/Warsaw')::date` | DERIVABLE + **NEW PERSISTENCE** (materialise — AS-IS `G-09`) |
| Day per-category coefficient + status | C-14 | `days[].categories[]` | daily count ÷ daily km × 100, banded with the same existing scoring table; **per category only, never aggregated into a day status** | NEW DERIVATION |
| Access states | C-17 | Worker response status | — | NEW (greenfield — AS-IS `G-16`) |

## 3. Fields the snapshot must NOT contain

`BUSINESS/DATA CONTRACT` — AS-IS `PRIVACY_DATA_BOUNDARIES` §3. If any of these reaches the browser, the implementation is wrong regardless of what the UI does with it:

`driver_key` · `client_code` · `driver_name` · `person_name` · `driver_surname` · `person_name_group_key` · `assigned_id` · `source_person_id` · `email` / `recipient_email` · phone · employee identifier · `ranking_included` (raw) · `ranking_group` (raw) · `ranking_position` for non-`INCLUDED` rows · `client_id` · `provider_trip_id` · `record_id` · per-trip `trip_start_ts`/`trip_end_ts` · `driver_tag_description` · `trip_mode` · `registration` / `vehicle_*` / `chassis_number` · latitude / longitude / locations / geofences / odometer · DB hosts, users, secret refs, SMTP config · **any other driver's score, rank or identity**.

Two specific traps:
1. The trend views join the roster and expose `driver_name` and `email` — any snapshot query built on them must project those away explicitly.
2. `ranking_position` for `EXCLUDED` drivers is a real number with no real meaning (a second, shadow league of 927 drivers at ALPHA00001). Filter it out in the **generator**, not in the UI.

## 4. Consistency rules the UI relies on

`BC`:

1. `eco_score_total == 100 + Σ categories[].points_lost` for all eight categories. If it does not hold, the snapshot must not be published.
2. `categories[].points == points_max + points_lost`.
3. `status` follows exactly: `points == points_max` → green; `0 <= points < points_max` → yellow; `points < 0` → red; `coefficient == null` → neutral.
4. `marker_band_index` is the band whose `points_lost` equals `points_lost`; if no band matches, it is `null` and no marker is drawn.
5. `ranking_position`, `ranking_total_participants` and `rating_group_share_percent` are present **only** when `ranking_state == "RANKED"`.
6. `period_end_date_display == period_end_date_exclusive − 1 day`.
7. Weekly `period_start_date` is always the first day of `period_label`'s month.
8. `days[]` distances sum to `total_kilometers` and, per category, `Σ days[].categories[key].count == categories[key].count` (verified in AS-IS §2.1; allocate with largest-remainder rounding). **Scores do not sum and are not present per day.**
9. There is **no** `day_status` field and no `min_daily_evaluation_km` constant. The 100 km threshold is whole-period-only. Once the reporting period qualifies, every 1–99 km day is shown; for `day.kilometers > 0` use the coefficient derived from that day's actual distance. `0 km` is the neutral no-driving state.
10. `scoring_complete == false` ⇒ the snapshot is **not published** as a dashboard; the Worker serves `REPORT_NOT_READY`.
11. The payload contains no `driver_key`, no `client_code`, and nothing from §3.
12. All numbers are final: computed with `Decimal`/`ROUND_HALF_UP` on the host. The frontend formats only.
