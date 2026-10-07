# 28. Driver Eco Dashboard V1 — snapshot/data foundation

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Command lines of the
> form `PYTHONPATH="$PWD" .venv/bin/python ops/runner.py …` are development /
> local / debug only — they execute the mutable working tree. The supported
> production entrypoint is the installed wrapper
> `/usr/local/bin/log-job-runner.sh <module> '<json>'`. See
> `docs/07_operations.md` -> *Release boundary*.

Status: `DRIVER_ECO_DASHBOARD_V1_SNAPSHOT_FOUNDATION_READY` (data) + `DRIVER_ECO_DASHBOARD_V1_FRONTEND_READY` (UI) + `DRIVER_ECO_DASHBOARD_V1_PRE_PUBLISHER_SECURITY_GATE_READY_FOR_REVIEW` (delivery) + `DRIVER_ECO_DASHBOARD_V1_HOST_PUBLISHER_LIFECYCLE_FINAL_REMEDIATION_READY_FOR_REVIEW` (host publisher).

This document is the routing entry and the durable contract summary for the internal Driver Eco
Driving Dashboard. It does **not** duplicate the design packages; it points at them and records what
the repository actually implements.

---

## 1. Authoritative sources

| Role | Location |
|---|---|
| **AS-IS facts** about the implemented Eco Driving subsystem (formulas, thresholds, period semantics, ranking, ingestion, privacy) | `design-handoffs/driver-eco-dashboard/as-is/v1.0/eco-driving-as-is-audit-handoff/` |
| **Target design + implementation contract** (owner decisions, invariants, snapshot schema, assertions, screens, copy) | `design-handoffs/driver-eco-dashboard/design/v1.0/DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF/design-handoff/` |
| Canonical visual sources | `design-handoffs/driver-eco-dashboard/design/v1.0/DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF/Eco Driving Dashboard.dc.html`, `... Eco Driving Design System.dc.html` |
| Withdrawn drafts — **never implement from these** | `.../design/v1.0/DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF/archive-non-authoritative/` |

Read order inside the design package: `FINAL_HANDOFF_MANIFEST.md` → `CLAUDE_CODE_IMPLEMENTATION_BRIEF.md`
→ the modular documents. Priority when anything conflicts: owner decisions and invariants (brief §1/§2)
→ AS-IS audit → canonical `.dc.html` → the rest of `design-handoff/` → engineering judgement.

> Path note: the design package was relocated from `as-is/v1.0/` to `design/v1.0/` after the snapshot
> milestone. The folder name `DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF` identifies it; `as-is/v1.0/`
> now holds only the AS-IS audit.

Repository source, SQL and tests remain authoritative for what is actually implemented today.

## 2. What this milestone delivers

| Component | Path |
|---|---|
| Versioned snapshot contract, category catalogue, band ladder, status rule, assertions, privacy ban list | `jobs/ecodriving_dashboard/snapshot_contract.py` |
| Deterministic snapshot derivation (period block, comparison, days, coaching, near-threshold, series) | `jobs/ecodriving_dashboard/snapshot_builder.py` |
| Per-pipeline-family source adapters and read-only SQL | `jobs/ecodriving_dashboard/sources.py` |
| Read-only generator job (`run(client, run_id, params)`, not yet registered in a schedule) | `jobs/ecodriving_dashboard/job_eco_dashboard_snapshot.py` |
| Synthetic fixtures for the 16 required states | `ops/tests_manual/eco_dashboard_fixtures.py` |
| Deterministic contract tests | `ops/tests_manual/test_eco_dashboard_snapshot.py` |

No migration, no schema change and no persisted snapshot table were required: every field the contract
needs already exists in `eco_*_{weekly,monthly}_stats` and `eco_*_trip_assignments`.

**Out of scope here and not implemented:** Cloudflare Pages/Worker/R2, capability links, token
generation or revocation, public routes, the dashboard frontend, e-mail integration, publication.

## 3. Contract identity and versioning

`contract_id = "driver_eco_dashboard_snapshot"`, `schema_version = 1`, both emitted in every document
and asserted before publication. The document is per driver **and** per period type: `periods.weekly`
and `periods.monthly` are both present as keys, and the type this run did not build is `null`.

Each `periods.<type>` entry carries `status ∈ {OK, INSUFFICIENT_DISTANCE, REPORT_NOT_READY}`,
a `period_identity` block, and — only when `status == OK` — `current`, `previous`, `series` and
`series_reference`.

## 4. Business invariants the code enforces

### 4.1 The 100 km gate is period-level only

`qualification_status` is computed by the existing rule (`MIN_QUALIFYING_DISTANCE_METERS = 100 000`,
inclusive at exactly 100 km) over the **whole closed reporting period**. Below it, the entry is
`INSUFFICIENT_DISTANCE` and carries no distance, score, rating, ranking, category, count, trend,
coaching or daily data — only the period identity and freshness needed to name the closed report.

There is **no daily distance threshold of any kind**. Once the period qualifies, a 5 km, 20 km, 49 km
or 99 km day is an ordinary Detailed row with its own distance, raw counts and per-category
coefficient. A day with `kilometers == 0` is the neutral no-driving state (`coefficient = null`,
`status = neutral`) — never a fabricated zero evaluation.

### 4.2 Status comes from the coefficient, never from the raw count

```
raw violations + qualifying exposure
  → integer coefficient per 100 km (ROUND_HALF_UP, computed on the host)
  → existing eco_scoring bucket
  → points vs points_max
  → green | yellow | red | neutral
```

`count` and `coefficient_per_100km` are distinct fields with distinct roles. Two identical raw counts
legitimately produce different statuses at different exposure. The band ladder is derived from
`eco_scoring.SCORING_RULES`; there is no dashboard-local threshold table, and no aggregate day status.

### 4.3 Weekly is cumulative month-to-date

`period_start_date` is always the first day of the month; only `period_end_date_exclusive` advances.
The comparison basis is the **previous closed cumulative period of the same month**
(`comparison.kind = PREVIOUS_CUMULATIVE_PERIOD`), e.g. `01–14 Aug` compares with `01–07 Aug`, never
with the isolated `08–14 Aug` segment. The series ends with the month; the next month starts a new
series. Monthly compares a closed calendar month with the previous closed month
(`PREVIOUS_CLOSED_MONTH`). Both carry full explicit date ranges so the frontend never infers them.

A previous period is a valid basis only if it also passed the 100 km gate and produced a complete
score; otherwise `comparison` is `null` and every delta degrades to "no basis". A series point for an
unqualified period is omitted rather than shown or interpolated.

### 4.4 Ranking eligibility and transitions

`ranking_state` is derived from the internal `ranking_group`, which never leaves the host:

| `ranking_group` | `ranking_state` |
|---|---|
| `INCLUDED` | `RANKED` |
| `EXCLUDED` | `NOT_RANKED_BY_CONFIGURATION` |
| `UNKNOWN_DRIVER`, previously ranked | `LEFT_RANKING` |
| `UNKNOWN_DRIVER`, otherwise | `NOT_ON_ROSTER` |

`ranking_position`, `ranking_total_participants`, `rating_group_share_percent` and
`rating_group_distribution` exist **if and only if** `ranking_state == RANKED` (assertion A5).
`ranking_transition ∈ {RANKED_TO_RANKED, NEWLY_RANKED, LEFT_RANKING, NOT_RANKED_TO_NOT_RANKED,
NO_COMPARISON_BASIS}` is modelled explicitly so the frontend never reconstructs eligibility rules, and
a rank delta is produced only for `RANKED_TO_RANKED`.

**EXCLUDED drivers keep the full useful dashboard** — score, classification, category values,
coefficients, points, trends, distance, daily detail and coaching — and receive no ranking position,
no rank movement and no group share. The persisted `EXCLUDED`-league position (a real number from a
second, meaningless league of 927 ALPHA00001 drivers) is filtered in the **generator**, and a driver
currently in that population is never given a historical position either. The word `EXCLUDED` itself
never appears in the payload.

### 4.5 Fail closed

A period whose score cannot be produced completely is `REPORT_NOT_READY` and is not published; there is
no partial dashboard. A persisted `eco_driving_score_total` that disagrees with the recomputation also
fails closed. Over-rev at full points is **complete data, not a gap**: it keeps its 15 points inside
the unchanged 100-point model and is only marked `deemphasize: true` for presentation, with no claim
about a technical cause. Assertions A1–A13 run before any document is returned; a failure raises
`SnapshotContractError` and the snapshot is not published.

### 4.6 Deterministic coaching

Four insight types, computed on the host, no model and no randomness, never padded to four, never
about over-rev, and never expressed as an allowed event budget:

| Code | Selected by |
|---|---|
| `LARGEST_LOSS` | most negative `points_lost`; ties by declaration order |
| `MOST_IMPROVED` | largest **coefficient** decrease; a scoring-bucket crossing is not required |
| `MOST_DETERIORATED` | largest **coefficient** increase |
| `BEST_OPPORTUNITY` | first near-threshold category: current coefficient vs the next better existing threshold plus its deterministic point gain |

The points consequence is reported when it exists but is never used for selection. Sentences are
composed in the frontend from `inputs`, so copy can be corrected without regenerating snapshots.

### 4.7 Privacy

The document is presentation-only. It contains no `driver_key`, `client_code`, `client_id`,
`assigned_id`, `person_name_group_key`, name, e-mail, phone, employee identifier, registration,
vehicle data, GPS, location, route, per-trip timestamp, raw ranking configuration, or any other
driver's score or rank. `assert_no_forbidden_fields` and `assert_no_forbidden_values` enforce this on
field names, on renamed variants (substring rules) and on values, and are exercised against a
deliberately poisoned document. Internal identifiers a future publisher needs live in
`DashboardSnapshot.internal`, outside the document.

Two additions closed defects an independent review demonstrated:

* **`banned_values` is required, with no default, at every layer**
  (`assert_snapshot_document`, `build_driver_snapshot`, and the publisher entry
  point, which derives it from a mandatory `PrivacyContext`). It previously
  defaulted to `()`, so forgetting it silently turned the value sweep into a
  no-op — the exact check that exists to catch a leaked driver identifier.
* **`assert_fixed_vocabulary` (A15)** inverts the question for strings. Every
  string in the document must belong to a field whose vocabulary the contract
  fixes (`label`, `short_label`, `band_label`, `key`, `status`, `rating_type`,
  `ranking_state`, `ranking_transition`, `code`, `selected_by`, `weekday_short`,
  …, all serialised from the canonical enums and the scoring ladder) or to a
  field whose deterministic format it fixes (dates, instants, period labels,
  timezone). There is no third category, so a newly added string-valued field
  fails A15 until someone decides which it is. The review's two demonstrated
  payloads — a person-like name and an HTML fragment in an allowlisted `label` —
  are refused by this rule regardless of what the caller declared.

A refusal names the assertion and the position, never the offending value.

### 4.8 Canonical serialisation and the payload budget

`canonical_json_bytes()` is **the** canonical encoding, and `serialize_document()`
is that plus the budget check. It is deterministic in every dimension:

* UTF-8, `ensure_ascii=False`, so Polish labels are one stable byte sequence;
* **object keys sorted**, so two semantically identical documents built with
  different insertion order produce identical bytes — the previous
  `sort_keys=False` did not guarantee this;
* compact separators (`separators=(",", ":")`), the form the design's 60 kB
  budget is measured against;
* `allow_nan=False` plus an explicit type sweep, so NaN/Infinity cannot be
  emitted;
* array order preserved, because array order is semantic here;
* `Decimal`, `datetime`, `set` and non-string keys are **refused** (A16), not
  coerced, because each has more than one plausible rendering.

Exceeding the budget raises `SnapshotContractError` rather than shipping an
oversized payload. Worst case in the fixture set — a 31-day monthly Detailed
snapshot — is **38 597 B**. The publication payload digest is computed over
exactly these bytes.

## 5. Per-client ingestion difference (preserved, never normalised)

| | ALPHA00001 | BRAVO00016 |
|---|---|---|
| pipeline family | `eco_driver` | `eco_person` |
| identity column | `assigned_id` | `person_name_group_key` |
| assignments table | `eco_trip_assignments` | `eco_person_trip_assignments` |
| private-designated trips | **excluded** (`is_private_trip IS FALSE`) | **included** |

`sources.resolve_pipeline_family()` fails closed for any client without an explicitly declared family.
The inclusion rule is stated once (`PipelineFamily.include_private_trips`) and drives both the SQL
predicate and its Python mirror, so the intentional difference cannot drift between them. One snapshot
contract, one adapter per family — not two dashboards.

## 6. Verification

`python3 ops/tests_manual/test_eco_dashboard_snapshot.py` — 17 deterministic checks covering the
period gate boundary (99.99 km vs 100.00 km, cross-checked against both aggregation jobs), sub-100 km
days remaining visible, identical counts producing different statuses at period and day level, the
EXCLUDED contract, the per-client private-trip difference, cumulative-MTD and closed-month comparison
semantics, ranking transitions, coefficient-movement coaching, `BEST_OPPORTUNITY` threshold
arithmetic, the 100-point model and A1–A4, coefficient-helper agreement with both aggregation jobs,
fail-closed completeness, series suppression for unqualified periods, and the privacy ban list against
every fixture plus a poisoned document.

`python3 ops/tests_manual/eco_dashboard_fixtures.py [--write-json <dir>]` prints or materialises the 16
synthetic state fixtures for frontend work. All fixture data is invented.

## 7. Open dependencies for the next milestones

`DEP-02` Eco schedules are all disabled today, so snapshot freshness is operator-driven — copy must not
promise a weekly cadence. `DEP-04` periods predating the ranking-contract recalculation are not
comparable for ranking; the builder accepts a `comparable=False` flag but the boundary itself is not
yet resolved from data. `DEP-05` is **partly resolved by milestone 3**: the capability→object mapping with expiry, revocation
and rotation, the R2 layout and the Worker gateway all exist (`delivery/driver_eco_dashboard/`); the
host-side publisher and the e-mail link delivery do not. `DEP-07` `eco_*_trip_assignments` retention
(365 days, currently disabled) bounds how far back `days[]` can be materialised.

## 8. Frontend (milestone 2)

`assets/driver_eco_dashboard/` — a static, framework-free driver-facing dashboard whose only
business input is one schema-v1 snapshot for one driver. See its `README.md` for the file map,
the local preview command and the rules the code must keep.

| Concern | Where |
|---|---|
| production shell | `index.html` (`#eco-root[data-snapshot-url]`, no fixture selector) |
| synthetic state preview | `preview.html?fixture=<name>#<period>/<view>` — needs a static server |
| design system | `css/dashboard.css` |
| snapshot → HTML (pure, DOM-free) | `js/render.js` |
| input boundary (worker / fixture / static) | `js/snapshot-source.js` |
| bootstrap, hash routing, disclosures | `js/app.js` |
| pl-PL formatting and layout maths | `js/format.js` |

**Screen matrix.** One component system covers all four experiences: Weekly Summary, Weekly
Detailed, Monthly Summary, Monthly Detailed. Period and view are independent controls
(`role="tablist"`, arrow keys, hash `#weekly/summary`); switching one never resets the other.

**The frontend contains no business logic.** It carries no band table, no coefficient arithmetic and
no rounding of a business value; every green/yellow/red comes from the snapshot's own `status` field.
`test_driver_eco_dashboard_frontend.py` asserts this at source level as well as in the output.

**Fail-closed rendering.** `INSUFFICIENT_DISTANCE` and `REPORT_NOT_READY` replace the content area
entirely: no score, rating, ranking, category, count, trend, coaching or day survives anywhere in
the DOM, in an `aria-label` or in page source.

**Responsive.** The production contract is the two-band one in §8.2: **>= 760 px** desktop,
**<= 759 px** the approved v3 mobile page. No horizontal scrolling at any width, verified
320–1440 px and at the 400 % zoom equivalent. Monthly Detailed keeps its 31 days grouped into five
Monday-anchored segments with one expanded on desktop.

*Historical (milestone 2, superseded by §8.2):* the original single-tree contract was ≥ 1024 px full
grid · 700–1023 px same grid with short labels · < 700 px day cards and a vertical scoring axis.

**Accessibility.** Zero axe-core AA violations on all four screens plus every data-gate state, at
1440 px and 390 px. Colour is never the only carrier: every status ships colour + glyph + word, the
daily grid's numbers-only cells keep all five contractual compensations, and every disclosure is a
real `<button aria-expanded aria-controls>`.

Two deliberate deviations from the visual source, both recorded here:

1. **Over-rev de-emphasis.** The design asks for 62 % row opacity; that drops the row's 11 px
   captions below the AA contrast floor. The row is de-emphasised with a sunken background and muted
   *decoration* instead — no text is dimmed.
2. **Day grid markup.** On the desktop tree the grid stays one real `<table>` with explicit ARIA
   roles at every width rather than switching to a `<ul>` of `<article>`s. The compact card
   presentation is pure CSS, so no data is duplicated in the DOM and every cell keeps its accessible
   name in both presentations. *This deviation is desktop-only since the v3 release:* below 760 px
   the approved design replaces the table with a vertical list of day cards (§8.2), and
   `renderDashboard` emits one tree or the other, never both.

### 8.2 Approved presentation authority — v3, production-live

`ECO_DASHBOARD_MOBILE_TABS_DEPLOYED_AND_VERIFIED` (2026-08-28).

**The design export is the authority for the presentation layer.** The approved export is
`design-handoffs/driver-eco-dashboard/driver-eco-dashboard-final-design-v3/production/`, and it owns
exactly five files: `index.html`, `css/dashboard.css`, `js/format.js`, `js/render.js`, `js/app.js`.
`test_driver_eco_dashboard_frontend.py` pins their SHA-256 digests and fails if a shipped file drifts
from the export, so a repository-side correction NEVER edits one of them — a contract that cannot be
met without changing one is a design-source correction and the design must re-export. The repository
owns `js/capability-bootstrap.js`, `js/boot.js` and `js/snapshot-source.js`, and the design never
touches those.

**Responsive contract.**

| band | presentation |
|---|---|
| **>= 760 px** | the desktop page, unchanged by the v3 release |
| **<= 759 px** | the approved mobile page (v3 variant 1a "Zakladki"): three bottom tabs — **Wynik / Wykroczenia / Dni** — each scrolling freely and remembering its position; eight category accordions ordered by point loss; a vertical list of day cards |

`renderDashboard` emits one tree or the other and never both. Below the boundary the gated slide
gesture, the navigation dots and `.ed-mover` are not rendered, and **no dashboard surface pans
horizontally** at any width or in any state. The route vocabulary (`#<weekly|monthly>/<1|2|3>`) is
unchanged, so driver links issued before the release still resolve. Both fail-closed states remain
full-screen, with no tab bar and no Eco payload.

**How this surface is released.** The frontend is served by the standalone `driver-eco-dashboard`
Worker, whose `[assets]` binding points at `assets/driver_eco_dashboard/`. A release is therefore
`wrangler versions upload` followed by `wrangler versions deploy …@100` — **never** a host
`ops/manage_release.py` activation, which governs a different boundary and would restart the Portal
API and the export worker for no functional effect. Pushing to `origin/main` does not deploy this
surface and no CI does it either. Wrangler 4.x needs Node >= 22.

`versions upload` + `versions deploy` deliberately do not apply triggers. `wrangler.toml` carries
`workers_dev = false`, so a plain `wrangler deploy` or a `wrangler triggers deploy` would withdraw
the live `workers.dev` endpoint. Use the two-step version flow.

**Preview and fixtures are not published.** `worker/lib/assets.js` allowlists the nine production
paths and denies `/fixtures/`, `/preview*` and `/README*`; with `run_worker_first = true` that holds
whatever the `ASSETS` binding happens to contain. Verified on the deployed origin: `/preview.html`,
`/preview`, `/README.md` and `/fixtures/*` all return 404.

**Release record.** The mobile-tabs release was source commit
`b020dfb6e1c0a9d13eac7498b220fe7c036b7bfc`; Worker version
`97cf3d4b-2d29-4578-8e7d-9ef16c1c7223` (tag `b020dfb`) at 100% traffic, superseding
`4a7ed9a4-328a-4364-8bc6-299b25af9adf`; no rollback required. It was then superseded by
`a5ea4f49-fd50-4f94-8a30-a8e6e9721da7` (tag `d49c7c7`, commit `d49c7c718c59…`, §13.9), which
carried those same presentation bytes unchanged.

**Current Worker version — `c20b2ede-8375-4931-8c26-6009bbfaf912`, tag `e36bab3`.** Uploaded
2026-09-03T22:14:55Z and deployed at 100% traffic 2026-09-03T22:15:25Z, from merge commit
`e36bab31bdff8cfe9b46c54753a10964929c97bf` (ECO-20260903-01/03/04/05: the monthly series
reference, the second trend tab, the sliding trend switch and the monthly comparison wording).
`72c67bec…` is the Worker rollback target. This is the first Worker release since `b020dfb` that
CHANGES the presentation bytes — three of the five design-owned files moved, and the pins in
`test_driver_eco_dashboard_frontend.py` moved with them:

| file | before (`b020dfb`…`d49c7c7`) | now (`e36bab3`) |
|---|---|---|
| `index.html` | `d50444ae62e8…` | unchanged |
| `js/format.js` | `6a4b834cb26d…` | unchanged |
| `css/dashboard.css` | `35c416ee2481…` | `204fde93aa0a…` |
| `js/render.js` | `ef0d3fc1b243…` | `d36fd6dadb9b…` |
| `js/app.js` | `6c0a74cdb397…` | `b244e43ad2d1…` |

Verified read-only on the live origin after deployment: all five served byte-identical to the
committed candidate, `/preview.html`, `/preview`, `/README.md` and `/fixtures/*` still 404 —
including the two files this upload itself pushed into the `ASSETS` bucket, so the allowlist holds
against its own asset directory — and the CSP/HSTS/`private, no-store`/COEP/COOP/CORP header set
unchanged. No host release activation, service restart, D1 migration, binding, secret, R2 or ledger
change occurred, and no capability link was used.

**`origin/main` HAS SINCE MOVED PAST WHAT IS DEPLOYED.** `fbb1c85`
(ECO-20260904-02/03/04: the comparison fallback for the previous-month bar, the
per-threshold axis bands, the hover-only band ranges) is on `origin/main` and is
NOT live. Pushing does not deploy this surface and no CI does it either, so a
driver's link still renders `69bcf203…`. Read the live version from
`wrangler deployments list`, never from a commit being on `main`.

**This deploy also carried `c6944d76b15d…` (hard retention) to the edge.** That commit landed on
2026-08-29, after the 2026-08-28 Worker deploy, so its Worker half —
`worker/lib/retention_policy.js`, the deletion queries in `worker/lib/store.js` and their wiring
into `POST /api/publish/maintenance` — was NOT live between 2026-08-29 and 2026-09-03. See
`docs/42_platform_retention_and_schedule_governance.md`. The D1 schema is unaffected: the DDL in
`schema/001_authorization.sql` is byte-identical across `d49c7c7`…`e36bab3` once comments are
stripped, so no migration was needed and none was run.

Verification of the mobile-tabs activation itself (2026-08-28, `b020dfb`): Verified read-only after activation:
all eight served files byte-identical to the committed candidate (`css/dashboard.css`
`35c416ee248190ec…`, `js/render.js` `ef0d3fc1b2436761…`, `js/app.js` `6c0a74cdb397e349…`, the rest
unchanged), `/` → 200, unauthenticated `/api/snapshot` → `401 INVALID_LINK` with `private, no-store`
and the full CSP/HSTS header set. No host release activation, service restart, DB migration, data
mutation, DNS or infrastructure change occurred, and no real driver identity was used.

### 8.1 Frontend verification

`python3 ops/tests_manual/test_driver_eco_dashboard_frontend.py` — 17 deterministic checks executed
through `ops/tests_manual/eco_dashboard_render_harness.js` (Node, no dependencies): the four
experiences render; `INSUFFICIENT_DISTANCE` and `REPORT_NOT_READY` leak nothing; 5/20/49/99 km days
stay visible; status is coefficient-derived and no scoring rule is duplicated; the EXCLUDED contract;
ranking transitions without fabrication; cumulative-MTD and closed-month semantics with the banned
relative-time strings absent; deterministic coaching including improvement without a bucket crossing;
over-rev inside the 100-point model; the privacy ban list across all 18 fixtures × period × view;
the accessibility/interaction wiring; the snapshot-v1 input gate; fixture freshness; pl-PL formatting.

Browser-based verification (Playwright/Chromium, local tooling, not a repository dependency): 17
screenshots across the four screens, desktop/tablet/mobile and every data-gate state; interaction,
keyboard, disclosure, expand-all and compact-mode behaviour; no horizontal overflow at 320/360/390/
700/900/1024/1280/1440 px; reduced-motion honoured; no interactive target under 44 px on mobile;
axe-core AA clean.

## 9. Secure delivery (milestone 3)

`delivery/driver_eco_dashboard/` — the Cloudflare Worker authorization boundary between a driver's
e-mail link and their own snapshot. Full contract in that directory's `README.md`; this section
records only what other repository work must not break.

```
capability link  https://<host>/#k=<opaque 256-bit secret>   fragment, never sent to a server
  → POST /api/session      same-origin, Origin-checked, value in the body
  → __Host-eco_dash        opaque, HttpOnly, Secure, SameSite=Strict, ≤ 30 min
  → GET /api/snapshot      no parameters of any kind
  → private R2             object key resolved server-side from the grant
```

**Nothing is provisioned.** `wrangler.toml` declares bindings with placeholder identifiers; the R2
bucket, the D1 database and the `CAPABILITY_PEPPER` secret are separately authorized operator steps.

| Contract | Rule |
|---|---|
| capability | 256-bit CSPRNG, base64url, opaque; carries no driver, client, period or object information |
| at rest | only a digest is stored (SHA-256, or HMAC-SHA-256 under `CAPABILITY_PEPPER`); the raw value is unrecoverable |
| binding | one capability → one `subject_ref` → one `snapshot_object_key`; **no endpoint accepts a driver, subject or key** |
| lifetime | **period-scoped**, decided by one authoritative mapping in `worker/lib/capability_ttl.js`: **weekly 10 days, monthly 60 days**. There is no default and no fallback — `X-Publication-Period` is a required singleton control header on `/api/publish` and `/api/publish/recover`, and a missing, empty, unknown or duplicated value is refused `400` before an operation, an object or a grant exists |
| expiry | enforced server-side; `410` → the frontend's `LINK_EXPIRED`. Publishing a **newer** period never revokes an older still-valid grant: each is pinned to its own immutable snapshot, so consecutive reports deliberately overlap and an old e-mail keeps showing the report it was written about. The expired grant ROW is retained — it holds no secret, and it is what makes `LINK_EXPIRED` distinguishable from a link that never existed |
| revocation | immediate, and kills sessions already derived from the grant; answers `401` byte-identically to an unknown capability, so revocation is never disclosed |
| rotation | compare-and-set in one D1 batch: the successor insert is conditional on the predecessor still being eligible, so a retry or a concurrent attempt returns `ALREADY_ROTATED` and mints nothing. Proven at 2-, 8- and 32-way contention → exactly one live successor. Correct classification requires `rotated_to` in the projection; `store.js` names its columns explicitly and the in-memory double models the projection, with a permanent test that re-runs the pre-fix projection and asserts the defect is still detectable |
| storage | **D1, not KV** — KV's eventual consistency would leave a revoked link working at some edges for up to a minute; see `worker/lib/store.js` for the full rationale |
| object key | opaque and non-derivable (AS-IS `PRIVACY_DATA_BOUNDARIES` §4.4); hex shard + base64url body, so every minted key satisfies its own validator (200 000 minted, zero rejections); `assertOpaqueObjectKey` refuses identity-shaped keys |
| R2 | private; the Worker only ever calls `get()` on the bound key. No public URL, no signed URL, no listing, no browser-supplied key |
| contract gate | **strict schema-v1 allowlist** (`worker/lib/schema_v1.js`): `additionalProperties: false` everywhere, typed scalars, enums, bounded ranges, plus the ranking and fail-closed cross-field rules. The body is **rebuilt** from the allowlist, so stored bytes are never forwarded and an unknown field cannot pass through. Corrupt, mis-versioned or over-wide objects return `503` |
| subject binding | the publisher writes a `subject_binding` digest into R2 object metadata; the Worker recomputes it from the grant and compares before parsing the body, so a cross-subject or copied-object mix-up is detected and denied. The binding never reaches the browser |
| logout | `204` means the server-side session is actually gone; if invalidation fails the answer is `503` and the cookie is left in place so a retry can still terminate it |
| cache | `private, no-store` for every `/api/*` response and for `index.html`; static assets `public, max-age=3600` |
| CORS | none. No `Access-Control-Allow-*` header is ever emitted; delivery is same-origin |
| CSP | `default-src 'none'; script-src 'self'` with no `unsafe-eval`, no wildcard and no third-party origin. Inline style *attributes* are the single concession (layout maths); `style-src-elem 'self'` still forbids injected `<style>` |

**Frontend impact was minimal and must stay that way:** `js/capability-bootstrap.js` is the first
script in `<head>` and owns fragment capture plus `history.replaceState`, so URL cleanup does not
depend on the application bundle loading; `js/boot.js` consumes a one-shot handover and performs the
exchange; the inline `<script>` was removed from `index.html` so `script-src 'self'` holds; and
`createWorkerSource` sends `credentials: "same-origin"`. No renderer, no component and no business
rule changed.

**Revocation boundary, stated precisely:** revocation takes effect at the **next authorization
check**. It is not retroactive — a request that already passed the authorization decision completes.
The exposure window is one already-authorized response.

**The publisher write path was an interface only in this milestone.** Milestone 5 adds the narrow
machine-authenticated transport described in §10; `worker/lib/publisher.js` still holds the grant
operations themselves, and no administrative or enumeration route exists.

### 9.1 Delivery verification

`python3 ops/tests_manual/test_driver_eco_dashboard_delivery.py` — 41 deterministic security checks
driving `ops/tests_manual/eco_delivery_worker_harness.mjs`, which executes the real Worker against
in-memory D1/R2/ASSETS bindings (no wrangler, no credentials, no remote resource). Coverage:
capability entropy and opacity; unknown / malformed / expired / revoked / rotated grants; revocation
of derived sessions; session TTL, tampering and end; subject binding; the absence of any parameter
that can select another snapshot; unauthenticated access never reaching storage; corrupt and
unsupported objects failing closed; no enumeration or direct object access; opaque object keys; no
capability in any response, log or stored row; cache and security headers; CSP restrictiveness;
cross-origin and method abuse; secure-origin enforcement; the served payload's v1 privacy
guarantees; the access-state mapping; and that no secret or account identifier is committed.

Added by the post-review remediation: rotation atomicity and idempotency (retry, eight concurrent
attempts, rotate-after-revoke, revoke-racing-rotation in both orders, unknown predecessor,
transaction rollback with a coherent retry); 23 strict-schema rejection cases (unknown top-level and
nested fields, a private marker in an unknown field, PII in an otherwise valid object, wrong scalar /
array / object types, unsupported enums, unsupported version, missing required fields, cardinality,
range, bad date format, control characters, ranking fields without a rank, payload on a non-`OK`
entry) plus a bidirectional schema-versus-fixture derivation check; subject/object binding mismatch,
copied object and missing metadata; logout failure semantics and successful-logout replay denial;
and 20 000-key generator/validator determinism with fixed vectors.

Local browser verification (Playwright/Chromium against `local/serve.js`, synthetic data only), 32
checks, all passing: URL cleaned **with the entire application bundle blocked**; clean URL after a
normal exchange; `HttpOnly` `SameSite=Strict` cookie whose value is not the capability; the one-shot
handover spent after boot; authorized snapshot rendered through the unchanged frontend; Weekly and
Detailed navigation with no horizontal scroll; capability absent from URL, history, Back/Forward,
referrer, DOM, `localStorage`, `sessionStorage` and `document.cookie`; CSP blocking an injected
inline script; a failed logout returning `503` with the session honestly still valid; a successful
logout denying replay; an over-wide object rejected at the boundary with the private marker never
reaching the DOM; and revocation denying a live session. Zero axe-core AA violations on the
Worker-served dashboard at 1440 px and 390 px, both views.

**Release-blocking dependency added by the review:** rate limiting on `POST /api/session`. Now **in
the candidate** as a Worker-native `[[ratelimits]]` binding (`SESSION_RATE_LIMIT`, namespace `1001`,
60 requests / 60 seconds per pre-authentication client actor, `POST /api/session` only). It is
account-level rather than zone-level so it also applies on workers.dev. Contract in
`delivery/driver_eco_dashboard/README.md`; deterministic coverage in
`ops/tests_manual/test_driver_eco_dashboard_edge_guards.py`. The binding is carried by the active
Worker version — since the 2026-09-03 presentation deploy that is
`c20b2ede-8375-4931-8c26-6009bbfaf912` (tag `e36bab3`, §8.2), whose upload output confirms it
declares `SESSION_RATE_LIMIT` at 60 requests/60 s alongside `AUTHORIZATION_DB`, `SNAPSHOTS` and
`ASSETS`, as did `72c67bec…` (tag `d49c7c7`, §13.9), `74f026f0…` (tag `b020dfb`, §8.2)
and `0ba6a586…` (annotated `2fdac6a0…`, shipped by the authorized 2026-08-24 deploy) before it. The deployed script bundle has still never
been provider-hash-bound and no provider-side verification of the rate limit has been performed —
that deployed verification is still required before the boundary carries real driver traffic.

## 10. Pre-publisher security gate (milestone 5)

Closes the items two independent reviews required before publisher development
may begin. Full contract in `delivery/driver_eco_dashboard/README.md`; this
section records only what other repository work must not break.

| Concern | Contract |
|---|---|
| **rotation retry** | `ALREADY_ROTATED` is distinguishable from `REVOKED` only because the grant projection names `rotated_to`. `SELECT *` is not used, and `local/memory_bindings.js` returns only the columns a statement names (and throws on an unknown one), so a narrowed projection behaves in tests exactly as in production. |
| **request bodies** | `POST /api/session` requires an acceptable declared body size (one canonical `Content-Length` ≤ 512 B) **before** the body is read, then bounds the read by `min(declared, 512)` and requires the actual byte count to equal the declaration. BYOB is used where the runtime offers a byte stream and a default reader otherwise; neither mode is a release condition. See §10.2. |
| **publication is one atomic D1 contract** | the operation row is the **lock**, not a progress log. Phases `CREATED → SNAPSHOT_WRITTEN → GRANT_MINTED → DELIVERY_INTENT_RECORDED → DELIVERED`; the capability INSERT and the ledger transition that makes it authoritative run in **one D1 batch**, guarded by the same predicate (operation id, phase, subject, payload digest, owned object key, no existing capability). Exactly one concurrent caller can win; every other caller writes nothing and receives no bearer. |
| **one operation, one object** | the server-minted R2 key is written **into** the operation-creating INSERT, so a caller that loses that INSERT never touches R2, and retries reuse the owned key. `uq_eco_publication_object_key` and `uq_eco_publication_capability` enforce it in the schema. |
| **canonical byte identity** | the publication `payload_digest` identifies the EXACT canonical host bytes, and those exact bytes are what R2 stores. The Worker recomputes the digest over the ingress octets, refuses a mismatch, validates them, and writes **those octets unchanged**; the strict-schema rebuild decides acceptance and sanitises the browser response, and is never the stored payload. See §10.3. |
| **grant/ledger atomicity** | `chk_eco_publication_grant_ledger` makes "a grant-authoritative state with no `capability_id`" unrepresentable, so the reviewed failure — a live grant the ledger did not reference — cannot be stored at all. There is no unconditional grant-insert helper anywhere under `worker/`; the unconditional form lives in `local/dev_grants.js`, which no shipped module imports. |
| **raw-bearer response loss** | recovery is a separate explicit call. The replacement INSERT, the supersession of the predecessor and the ledger move are **one transaction**; it does not call the generic `rotateCapability` and then record the result separately. An operation never has two live grants; the lost bearer is dead immediately. |
| **delivery is terminal, and names its bearer** | the `DELIVERED` refusal is a predicate **inside** the recovery transaction, so a recovery decided while the operation was still recoverable commits nothing if delivery wins first. `/api/publish/delivery` requires `X-Publication-Capability`; a superseded id is `409 CAPABILITY_SUPERSEDED` rather than terminalising the operation on a grant the driver never received. |
| **result semantics** | every response carries `next_action` ∈ `PERSIST_BEARER` / `RETRY_PUBLISH` / `USE_PERSISTED_BEARER_OR_RECOVER` / `OPEN_NEW_OPERATION` / `NONE`. Nothing returns a value that could be read as "just issue again". |
| **subject binding** | length-prefixed, domain-separated material (`driver_eco_dashboard.subject_binding.v1`). Specified in `delivery/driver_eco_dashboard/spec/subject_binding_v1.md` with 21 fixed vectors asserted from both JavaScript and Python. |
| **canonical publication** | `jobs/ecodriving_dashboard/publication.py` is the only supported publisher data path, and its API is `build_publishable_snapshot(privacy=…) → PublishableSnapshot → serialize_publishable_snapshot(…) → bytes`. A mapping has no route to publishable bytes; the raw helper is private. HOST does business validity, value-level privacy, fixed vocabulary and deterministic serialisation; WORKER does authorization, binding and the structural allowlist, as defence in depth and never as the privacy authority. |
| **write transport** | `POST /api/publish`, `/api/publish/recover`, `/api/publish/delivery`, authenticated by a separate machine credential (`PUBLISHER_KEY_DIGEST`). No driver credential is ever accepted; no caller-chosen R2 key exists; every unauthenticated shape answers an identical `404`; an unset digest refuses everything. All three go through the authoritative transaction primitives, and a test enumerates the routes to prove it. |

**No Cloudflare resource is created.** `PUBLISHER_KEY_DIGEST` joins
`CAPABILITY_PEPPER` as a secret an operator sets out of band, and the D1 schema
gains `eco_publication_operation` with its CHECK constraints and unique indexes —
none of it is applied anywhere.

### 10.1 Verification

Three deterministic suites, no browser, no credentials, no remote resource.

`python3 ops/tests_manual/test_driver_eco_dashboard_prepublisher.py` — 29 checks
driving `ops/tests_manual/eco_prepublisher_harness.mjs`: projection correctness
and the permanent divergence detector; rotation retry at 2/8/32-way contention;
the body-limit behaviour **per read mode** (§10.2); the publication lifecycle
and conflict handling; bearer-loss recovery; operation ids being useless as any
credential; 21 cross-language binding vectors; the canonical publication
interface, mandatory privacy context, fixed-vocabulary refusals, ad-hoc-document
refusals and deterministic serialisation; canonical bytes round-tripping through
the Worker gate; the machine-auth boundary; and a local publisher-write →
driver-read proof. Test-double fidelity is audited in the same suite.

`python3 ops/tests_manual/test_driver_eco_dashboard_publication_transaction.py` —
18 checks driving `ops/tests_manual/eco_publication_transaction_harness.mjs`.
**Every race is forced, not hoped for**: the local D1/R2 doubles park callers at
named transition sites and the suite releases them in a chosen order. The Worker
and its libraries contain no test hook, so the code under test is byte-identical
to the code that would deploy.

| Proof | Result |
|---|---|
| 2 / 8 / 32 / 128 simultaneous publishes of one operation | 1×`201`, N−1×`200`, **1** raw bearer, **1** object, **1** live grant, **1** operation |
| 8 callers parked together at the operation-claim / object-write / snapshot-written / grant-transaction boundary (before **and** after) | 1 bearer in every case |
| 32 callers parked at the grant boundary, released one at a time | 1 bearer |
| 25 repetitions × 4 callers | max bearers 1, max objects 1, zero orphaned grants |
| crash windows: before R2 write, after R2 before grant, at snapshot-written, **rollback inside** the grant transaction, before any request | no grant created, no orphan, retry converges to exactly one of everything |
| response loss after commit | ledger already names the grant; a retry answers `ALREADY_PUBLISHED` + `USE_PERSISTED_BEARER_OR_RECOVER` and mints nothing |
| 2 / 8 / 32 concurrent recoveries, and 16 parked and released together | exactly **1** replacement, the rest `409 SUPERSEDED` |
| recovery vs revoke / no-grant-yet / transaction failure / rollback / response loss / concurrent publish retry | never more than one live grant, never a ledger pointing at a grant that was not created |
| recovery parked at its transaction, `DELIVERED` completed, recovery released | `409 ALREADY_DELIVERED`, generation unchanged, delivered bearer still exchanges `204` |
| delivery parked at its transition, recovery completed, delivery released | `409 CAPABILITY_SUPERSEDED`, operation stays at `DELIVERY_INTENT_RECORDED`, host succeeds once it names the current bearer |
| local E2E from real canonical bytes | publish → one owned object with binding → one bearer → exchange → parameterless snapshot → served JSON identical to the published canonical bytes; then lost-response recovery (old bearer `401`, new bearer `204`, one live grant, one object); then `DELIVERED` with recovery denied and the delivered bearer still valid |

`python3 ops/tests_manual/test_driver_eco_dashboard_byte_integrity.py` — 14
checks closing the canonical byte-integrity blocker; see §10.3.

### 10.2 `/api/session` body bound — exactly what is proven

**The invariant, as it stands:**

> `POST /api/session` must present an acceptable declared body size — exactly
> one canonical `Content-Length`, at most **512** bytes — BEFORE the Worker
> reads the body. The bytes actually read are then independently bounded during
> reading, by both that declaration and the endpoint ceiling.

**What this replaced, and why.** The bound used to be gated on obtaining a BYOB
reader, because only a fixed-size read into a buffer the Worker allocates makes
per-read cost independent of the peer's chunking. Deployed workers.dev
verification of candidate `07411e9` settled that negatively and definitively:
hundreds of observations on the real Cloudflare runtime showed the incoming
`Request.body` is **not a byte stream**, so `getReader({ mode: "byob" })` never
succeeds and `body_read_mode` is always `"default"` there.

| Deployed probe | Observation |
|---|---|
| small declared body | `reader_entered = true`, `body_read_mode = "default"`, `bytes_read = 60` |
| small chunked body | `reader_entered = true`, `body_read_mode = "default"`, `bytes_read = 71` |
| oversized declared body | `DECLARED_TOO_LARGE`, `reader_entered = false`, `bytes_read = 0` |
| oversized chunked body | edge **synthesised** a `Content-Length`; same `DECLARED_TOO_LARGE`, zero body bytes read |

That finding is preserved, not rewritten. What changed is that a release gate
the platform cannot satisfy is not a safety property, so the gate was replaced
rather than waived: the bound is now taken from request framing, which the
runtime does expose, instead of from reader type, which it does not.

**The order, and what each layer bounds.**

| # | Layer | Bounds | Depends on |
|---|---|---|---|
| 1 | rate limit (`SESSION_RATE_LIMIT`) | attempts per actor | the `[[ratelimits]]` binding; missing → 503, never unprotected service |
| 2 | declared-size gate | **everything below**: no acceptable `Content-Length` ≤ 512 → refused with **0 bytes read**, no reader, no D1, no cookie | the runtime exposing `Content-Length`, which Cloudflare always does |
| 3 | bounded read | `min(declared, 512)`, enforced while reading; over-run cancels the stream | nothing |
| 4 | actual-versus-declared | `bytes_read == declared` on success; more refused mid-read, fewer refused on completion | nothing |

**Accepted grammar:** RFC 9112 `1*DIGIT`, leading zeros permitted (one value, no
ambiguity), 15 significant digits maximum, value ≤ 512. **Refused:** absent,
empty, duplicated/comma-joined, signed, fractional, exponential, hexadecimal,
unit-suffixed, internally spaced, out of range, and over the ceiling. All eight
internal diagnostics (`DECLARED_MISSING`, `DECLARED_EMPTY`,
`DECLARED_AMBIGUOUS`, `DECLARED_MALFORMED`, `DECLARED_RANGE`,
`DECLARED_TOO_LARGE`, `DECLARED_MISMATCH`, `STREAM_TOO_LARGE`) answer the caller
identically — `400 {"error":"INVALID_LINK"}` — so no protocol distinction is
exposed.

**Fail-closed.** An absent declaration is a refusal, never a fallback to an
unbounded read. The header is REQUIRED, not TRUSTED: it can only bound the read
downward, and the reader counts what it actually pulls against both the
declaration and the ceiling. If Cloudflare ever stopped synthesising a length
for some request shape, that shape would lose availability, not memory safety.

**Reader mode is telemetry.** `byob` remains an opportunistic optimisation where
the runtime offers a byte stream (the local byte-stream fixtures still exercise
it, and a one-chunk 5 MiB byte stream is still refused after 1 024 bytes);
`default` is an accepted production mode once the framing gate is satisfied.
Both satisfy the same ceiling and the same equality. **Neither is a deployment
blocker.**

**Compatibility flags — investigated and rejected.**
`streams_byob_reader_detaches_buffer` (default since 2021-11-10) governs whether
a BYOB read detaches the caller's `ArrayBuffer`;
`internal_stream_byob_return_view` (default since 2024-05-13) governs what a
BYOB `read()` returns at end of stream. Both act only on a BYOB reader that
already exists, and neither is documented as converting a non-byte-oriented
incoming `Request.body` into a byte stream:
`NO_COMPATIBILITY_FLAG_FIX_FOR_INCOMING_BODY_TYPE`. `compatibility_date` was
deliberately not bumped — a broad bump activates unrelated runtime changes and
is not justified by speculation.

**Gate state:**

1. *Bounded request framing* — **in the candidate**, with deterministic coverage
   in `ops/tests_manual/test_driver_eco_dashboard_session_framing.py`.
2. *Worker-native rate limiting* — **in the candidate and mandatory** (see §9).
3. *Cloudflare-native request-size control at the edge* — defence in depth, no
   longer blocking; it needs a zone, so it cannot be exercised on workers.dev.
4. *`workers_dev`* — **disabled**, and stays disabled until this candidate is
   deployed and its bounded verification re-run.

The earlier claim that an oversized upload "costs the ceiling, not its own size"
was **false** and remains retracted: one `read()` on a default reader returns one
whole chunk, so no runtime-independent peak-memory bound is claimed for a
default reader. What bounds this endpoint is layer 2, not the reader.

### 10.3 Canonical byte integrity — HOST → ledger → R2

**What the independent review found.** `payload_digest` did not identify the
bytes R2 held. The host hashed its canonical output; the Worker validated it,
rebuilt it from the strict schema, and stored `JSON.stringify(rebuilt)` — a
semantically identical document with different octets. So
`SHA256(host bytes) == ledger digest` while `SHA256(R2 body) != ledger digest`,
and the digest named a byte sequence that existed nowhere.

**The invariant now enforced.** For every accepted publication one byte
sequence is:

```
host canonical output
  = bytes hashed for payload_digest
  = bytes accepted by /api/publish
  = bytes written to R2
  = bytes the ledger digest identifies
  = bytes read back before schema validation
```

| Side | Authority |
|---|---|
| **HOST** (`jobs/ecodriving_dashboard/publication.py`) | canonical serialisation. Its octets are the publication identity; nothing downstream may redefine them. |
| **WORKER (write)** | independently hashes the exact ingress octets, refuses a mismatch against `X-Publication-Payload-Digest` in constant time, strictly validates them, and stores **those octets unchanged**. |
| **LEDGER** | `eco_publication_operation.payload_digest` = SHA-256 of the authoritative R2 bytes. |
| **R2** | the authoritative object contains exactly the canonical host bytes; `customMetadata.payload_digest` is recomputed from what was written and is not browser-visible. |
| **DRIVER RESPONSE** | rebuilt by the strict schema for sanitisation. Same document, deliberately different octets, and **not** what the digest identifies. |

**Request shape.** Publication metadata travels in authenticated headers and the
raw canonical snapshot is the entire body. A single JSON envelope would make the
snapshot a nested value, and recovering an exact nested byte range after parsing
is not reliable — so the envelope shape would prevent byte identity rather than
merely complicate it.

**No JavaScript canonicaliser.** Reproducing `canonical_json_bytes` in JS means
reproducing Python's float repr (`1.0` versus `1`), and a second serialiser that
disagrees on one number is a publication outage, not a control. The Worker
instead preserves the octets it received and enforces only the two canonical
properties a token scanner can see without re-encoding anything: no
insignificant whitespace, and object keys in ascending order. Non-canonical or
invalid-UTF-8 ingress is `422` and creates no state.

**Fail-closed integrity, both directions.**

* an ordinary retry may write the owned object **only** on a definitive R2
  absence, and may reuse an existing one **only** once every authoritative
  invariant is proven — body readable, `SHA256(body)` equal to the ledger
  digest, `payload_digest` metadata present and equal to that hash, the
  contracted digest algorithm, and a subject binding present and valid for the
  operation's subject, the owned key and `binding_version = 1`. Any existing
  object that fails one of those is `OBJECT_INTEGRITY_FAILURE` (409) and any
  object whose state cannot be determined at all is `OBJECT_UNREADABLE` (503);
  both write nothing, mint nothing and leave the ledger where it was. **An R2
  error is never an absence**, and ordinary retry never repairs — a silent
  repair would destroy the evidence. See §10.3a;
* the driver read hashes the stored octets on every request and refuses unless
  they match the object's own metadata digest **and**, when a publication owns
  the key, the ledger digest. Two authorities in two stores: R2 body and R2
  metadata are written together, so metadata alone cannot witness its own
  integrity. A grant issued outside the publication path (local scaffolding
  only) has no ledger row; the metadata authority still applies and the
  `snapshot_served` event records `ledger_verified`.

**Verification.** `python3 ops/tests_manual/test_driver_eco_dashboard_byte_integrity.py`
— 14 checks driving `ops/tests_manual/eco_byte_integrity_harness.mjs` over
canonical bytes produced by the real Python publisher (never a fixture file):

| Proof | Result |
|---|---|
| host bytes → publish → R2 | R2 body is byte-for-byte the host output; one SHA-256 equals the host digest, the ledger digest, the R2 body digest and the R2 metadata digest |
| recurrence detector | the strict-schema rebuild of real canonical output is a **different** byte sequence, and the test fails if it ever coincidentally matches; a Python-canonical float (`1.0`) is shown to be unreproducible by `JSON.stringify` |
| `digest(A)` + `body(B)`, both directions, plus a fabricated digest | `400`, no operation, no object, no grant, no bearer |
| same operation id, different canonical bytes | `409 CONFLICT`; object bytes, ledger digest and grant all unchanged; an identical retry stays idempotent |
| R2 body mutated / body+metadata rewritten together / metadata digest removed | `503` in all three; exact restoration resumes service |
| retry over a substituted object | `409 OBJECT_INTEGRITY_FAILURE`, nothing rewritten, ledger still `CREATED`, no grant; publication resumes once the correct bytes are back |
| pretty-printed same document / invalid UTF-8 | `422 PAYLOAD_NOT_CANONICAL`, no state created |
| body reader | returns exact octets in both stream and buffered modes; raw octets survive a lossy decode and strict decoding refuses them |
| static | no `JSON.stringify` on the write path, no `JSON.parse`/`JSON.stringify` in the publication module, the digest module refuses to hash text |

The publication/recovery concurrency suite additionally asserts that recovery
and delivery leave the stored bytes and both digests untouched.

### 10.3a R2 retry integrity and publisher protocol shape

Closes the two findings of the canonical-byte-integrity re-review.

**BLOCKER — an unreadable object was treated as an absent one.** Object
inspection returned a boolean `present` and reported an unreadable object as
`{ present: false, unreadable: true }`. Two confirmed consequences: an R2
`get()` that threw was answered with another `put()` that overwrote an object
nobody had read; and an object whose body still hashed correctly but whose
digest metadata or subject binding had been corrupted or removed advanced the
ledger to `GRANT_MINTED`, returned 201, minted one live grant, and left the
driver read failing 503 for the life of the link.

Inspection now returns one of four states and the publication path treats them
as four different things:

| State | R2 observation | Publication may |
|---|---|---|
| `ABSENT` | `get()` resolved to exactly `null` without throwing — the only value that means "no such object" | write the owned object — the **only** state that permits it, and only while the ledger still says `CREATED` |
| `PRESENT_VALID` | object exists and **all eight** invariants proven | reuse it: zero puts, zero new keys, exact bytes preserved, existing state machine continues |
| `PRESENT_INVALID` | object exists, an invariant failed | nothing — `OBJECT_INTEGRITY_FAILURE`, 409 |
| `UNREADABLE` | `get()` threw, resolved `undefined`, an array or another malformed result, the result carried no string `key` equal to the owned key, it carried no readable body, the body read threw, metadata access threw, or no bucket is bound | nothing — `OBJECT_UNREADABLE`, 503 |

The eight invariants required for reuse are listed in
`delivery/driver_eco_dashboard/README.md` § „Object-state inspection". Body
hash alone is explicitly not sufficient. `ABSENT` is itself a failure once the
ledger has reached `SNAPSHOT_WRITTEN`: the object's disappearance is an
integrity incident, and re-creating it would be the forbidden repair.

This is grounded in the documented Workers R2 contract: `get(key)` resolves to
`null` "if the key does not exist", an error surfaces as a rejected promise,
and a conditional `get` whose precondition fails resolves to an object with no
body. `undefined` appears nowhere in that contract, so it is a malformed
result, never an absence. Neither is an array: `typeof [] === "object"`, and an
array can carry a `key`, an `arrayBuffer()` and a `customMetadata`, so a
decorated one satisfies every content check while being something R2 never
returns — it is rejected as a malformed container before any property it
supplies is trusted. `PRESENT_VALID` further requires the returned result to
carry a **string** `key` **equal** to the operation-owned key — a missing or
non-string key cannot establish which object the response describes and is
`UNREADABLE`, while a well-formed key naming a different object stays
`PRESENT_INVALID`. The local R2 double models each distinctly — a failing `get` throws, a
body read failure rejects, and a metadata failure is an accessor that throws —
so "unreadable is not absent" is expressible in tests at all.

**MEDIUM — publisher request parsing was broader than documented.**
`application/jsonp` was accepted because the media-type check was
`startsWith("application/json")`, and a duplicated `X-Publication-Subject` was
joined by the runtime into `"a, a"` and accepted as a new subject. The media
type is now parsed and compared for equality (`application/json`, optionally
`; charset=utf-8`, every other parameter refused rather than ignored), and
every publication control header plus `Authorization` is read through a
singleton gate that refuses any combined form. Authentication runs first, then
the shape gate, then anything that can mutate — so an invalid-shape request
creates zero operations, zero objects, zero grants and zero bearers.

**Verification.** `python3 ops/tests_manual/test_driver_eco_dashboard_retry_integrity.py`
— 34 checks driving `ops/tests_manual/eco_retry_integrity_harness.mjs`. Every
case is measured against the response status, the R2 put count, the exact
stored octets before and after, the operation phase before and after, the grant
row count, the live grant count and the bearer-return count.

| Case | Result |
|---|---|
| valid existing object (at `CREATED` and at `SNAPSHOT_WRITTEN`) | 201 `PUBLISHED`, **zero** puts, bytes untouched, one object, one grant, session → snapshot 200 |
| body-only corruption | 409, zero put, zero grant, ledger unchanged |
| metadata-digest corruption / missing digest metadata | 409, zero put, zero grant |
| body + metadata rewritten consistently, ledger unchanged | 409 against the authoritative ledger digest |
| subject binding corrupted / missing / all integrity metadata missing | 409, zero put, zero grant |
| R2 `get()` throws / body read throws / metadata access throws | 503 `OBJECT_UNREADABLE`, zero put, zero grant |
| object vanished after `SNAPSHOT_WRITTEN` | 409; not silently re-created |
| definitive absence | the only state permitting the write; normal atomic state machine follows |
| **recurrence detector** | a healthy object with a broken `get()` at both critical states: R2 put count stays **0**, and the operation converges once storage recovers with one put in total |
| every failure case, after exact restoration | converges to 201 / one grant / host bytes — the refusal did not poison the operation |
| `application/jsonp`, `-patch+json`, `-seq`, `jsonfoo`, `text/json`, `text/application/json`, `charset=evil`, unknown parameter, missing, `*/*`, duplicated | 400, zero side effects |
| duplicate/comma-joined operation, subject and digest headers | 400 `AMBIGUOUS_CONTROL_HEADER`, zero side effects |
| duplicate/comma-joined `Authorization` | 404, unchanged fail-closed auth semantics |
| duplicate headers on `/api/publish/recover` and `/api/publish/delivery` | 400, no bearer, ledger and bearer generation unchanged |
| 2 / 8 / 32 / 128 concurrent publishes with the new gate | one operation, one object, one grant, one live grant, one bearer, zero spurious integrity refusals |

The two defects were mutation-tested: reintroducing "UNREADABLE takes the
ABSENT branch" and "media type back to `startsWith`" each makes this suite
fail.

### 10.3b Recovery object integrity — one gate for every authorization move

Closes the finding of the R2 retry-integrity re-review.

**MEDIUM — `recoverLostBearer()` bypassed object integrity inspection.** The
four-state gate in § 10.3a was reachable from `publishSnapshot` only. Recovery
consulted D1 alone, so it could change authorization state while the
authoritative snapshot object could not be proven valid. Confirmed:

    R2 unreadable → recoverLostBearer() → RECOVERED
                  → new bearer emitted
                  → bearer generation 1 → 2
                  → predecessor grant replaced.

**There is now exactly one object-inspection contract**, `inspectSnapshotObject`
reached as `services.inspectObject`, and **both** operations that mutate or
advance authorization state for an existing publication go through it. Recovery
has no separate, weaker integrity logic, and both routes render its refusals
through one shared responder.

| Object state | Normal publication retry | Lost-bearer recovery |
|---|---|---|
| `ABSENT` | permits the **first** object write, and only while the ledger still says `CREATED`; a failure once the ledger says `SNAPSHOT_WRITTEN` | **blocked**, 409 `OBJECT_INTEGRITY_FAILURE` — an operation claiming an authoritative published snapshot whose object is gone cannot issue a replacement bearer |
| `PRESENT_VALID` | reusable: zero puts, exact bytes preserved | **the only state that permits recovery** |
| `PRESENT_INVALID` | fails closed, 409 | **blocked**, 409 |
| `UNREADABLE` | fails closed, 503 | **blocked**, 503 `OBJECT_UNREADABLE` — storage failure is never a reason to rotate authorization |

The verdict required for recovery is the full contract, not the body hash: the
operation-owned R2 key, a readable body, `SHA256(body) == publication
payload_digest`, present `payload_digest` metadata equal to that same body
hash, the contracted digest algorithm, and subject-binding metadata that is
present and valid for the operation subject, the owned key and the current
binding version.

**A failed recovery integrity check performs zero authorization mutation.** The
gate is a precondition evaluated before the D1 recovery transaction is
attempted, so for `ABSENT`, `PRESENT_INVALID` and `UNREADABLE` alike: no
replacement grant row, no revocation or supersession of the predecessor, no
bearer-generation increment, no change of the publication's capability
identity, no raw bearer, an unchanged ledger, unchanged R2 bytes, an unchanged
R2 put count — and not one recovery statement issued to D1. The bearer the host
already holds keeps working throughout, and restoring the exact valid object
lets recovery succeed normally.

**The inspection/transaction boundary.** R2 and D1 share no transaction and
nothing here claims otherwise. The achievable contract is: (1) prove the
currently referenced object `PRESENT_VALID`; (2) only then attempt the existing
conditional D1 recovery transaction, which still verifies operation state,
current capability/grant identity, generation, not-`DELIVERED` and the
predecessor's eligibility; (3) if inspection fails, no D1 authorization
mutation occurs. The two ledger columns the object was proven against —
`snapshot_object_key` and `payload_digest` — are additionally bound into the
recovery compare-and-set, so the mutation can only apply to the identity that
was proven. Both columns are immutable for the life of an operation (written by
the creating INSERT, set by no UPDATE), so the predicate cannot refuse a
recovery that ought to succeed; it exists so the immutability is checked rather
than assumed. An object corrupted after the proof is indistinguishable from one
corrupted immediately after commit and is caught by the read-time object
integrity checks the driver path already performs.

**Protocol shape on the authorization-moving routes.** `Content-Type` is
optional on `/api/publish/recover` and `/api/publish/delivery` — they carry no
body — but when declared it must be one unambiguous value and must be the same
documented publisher media type, parsed for equality. A duplicated
`Content-Type` is 400 `AMBIGUOUS_CONTROL_HEADER`; `application/jsonp` and every
other lookalike is 400 on these routes exactly as on `/api/publish`.

**Verification.** `python3 ops/tests_manual/test_driver_eco_dashboard_recovery_integrity.py`
— 28 checks driving `ops/tests_manual/eco_recovery_integrity_harness.mjs`.
Every case is measured against the operation state, the capability/grant id,
the bearer generation, the grant row count, the live grant count, the
predecessor's revocation and rotation columns, the R2 bytes, the R2 put count,
the bearer-return count and the number of recovery statements that reached D1.

| Case | Result |
|---|---|
| healthy object | 200 `RECOVERED`, exactly one replacement grant, predecessor revoked and rotated, generation 1 → 2, one raw bearer, zero puts, replacement session → snapshot 200, predecessor session 401 |
| object absent | 409, zero authorization mutation, zero recovery statements |
| body corruption | 409, zero authorization mutation |
| metadata-digest corruption / missing digest metadata | 409, zero authorization mutation |
| body + metadata rewritten consistently, ledger unchanged | 409 against the authoritative ledger identity |
| subject binding corrupted / missing | 409, zero authorization mutation |
| R2 `get()` fails / body read fails / metadata access fails | 503 `OBJECT_UNREADABLE`, zero authorization mutation |
| every failure case, after exact restoration | 200 `RECOVERED`, one replacement, host bytes intact — the refusal did not poison the operation |
| shared-contract probe | `recoverLostBearer` calls the real `inspectSnapshotObject` with the operation-owned key, the operation subject and the ledger digest |
| **recurrence detector** | valid publication + injected R2 `get()` failure → generation unchanged, zero replacement grants, zero recovery statements. Mutation-proved: the same scenario against a copy of `publication.js` with the `PRESENT_VALID` gate deleted rotates authorization |
| 2 / 8 / 32 concurrent recoveries + 16 forced at the transaction | one replacement, one live grant, one generation step, one raw bearer, losers `NOT_RECOVERABLE`/`SUPERSEDED`, zero spurious integrity refusals |
| recovery vs `DELIVERED` / publication retry / independent revocation / transaction rollback / response loss | unchanged terminal, replay, `GRANT_NOT_ELIGIBLE` and all-or-nothing semantics |
| duplicate / comma-joined operation and `Authorization`; duplicated or lookalike `Content-Type` on recover and delivery | 400 (404 for auth), zero side effects, zero recovery statements |
| local end to end | publish → R2 → grant → session → 200; healthy recovery keeps the snapshot digest identical; every corrupted/unreadable state refuses; restore → recovery succeeds; `DELIVERED` → recovery denied and the delivered bearer still reads 200 |

### 10.4 Crash-matrix stages 8 and 9

These were the two open windows this milestone had to close: if the host died
between "provider accepted the message" and "host recorded acceptance", a
replay could send the driver two e-mails. They are now closed on the host side
by a provider idempotency identity scoped to the publication operation id, and
by committing "a submission may have happened" **before** the provider call.
See §11.5. Server-side behaviour for stages 1–7 and 10 was already unambiguous
and is proven above.

---

## 11. Host publisher and e-mail delivery lifecycle (milestone 6)

The host side of the secure-delivery contract: canonical snapshot → durable
local operation → publication → durable capability → recipient-bound e-mail →
idempotent provider submission → reconciliation → remote `DELIVERED` → bearer
cleanup. Verified locally and synthetically only. **No real e-mail was sent, no
Cloudflare resource was created or mutated, nothing was deployed and no
schedule was enabled.**

| Component | Path |
|---|---|
| durable state model, identity derivation, secret scrubbing | `jobs/ecodriving_dashboard/delivery_contract.py` |
| durable host ledger (PostgreSQL, client business DB) | `jobs/ecodriving_dashboard/delivery_ledger.py` |
| publisher-API client and transport seam | `jobs/ecodriving_dashboard/secure_delivery_client.py` |
| provider-neutral e-mail boundary, SMTP adapter, deterministic fake | `jobs/ecodriving_dashboard/email_provider.py` |
| capability link and message construction | `jobs/ecodriving_dashboard/dashboard_email.py` |
| the state machine | `jobs/ecodriving_dashboard/publisher.py` |
| callable job (`run(client, run_id, params)`, **not scheduled**) | `jobs/ecodriving_dashboard/job_eco_dashboard_publish.py` |
| ledger DDL (applied fleet-wide, immutable) | `db/client_business/049_eco_dashboard_delivery_operation.sql` |
| reviewed external-mailer ownership delta (**applied fleet-wide**) | `db/client_business/050_eco_dashboard_external_mailer_ownership.sql` |
| local Worker runtime for host verification | `delivery/driver_eco_dashboard/local/publisher_serve.js` |

### 11.1 One durable row per logical delivery

`public.eco_dashboard_delivery_operation` lives in the **client business**
database, beside `eco_driving_*_email_send_log`, because it binds a driver
identity, a reporting period and a recipient address — per-client business
facts. The Worker's D1 ledger holds the complementary half (subject reference,
object key, capability digest) and deliberately holds **no recipient address**.

The logical delivery identity is
`(client_id, identity_key, period_type, period_start_date, period_end_date, send_scope)`
and it is `UNIQUE`, so a duplicate logical send is not representable. From it
the host derives, by length-prefixed domain-separated SHA-256:

| Derived | Purpose | Property |
|---|---|---|
| `operation_id` | the Worker publication operation | 43 base64url chars, inside `^[A-Za-z0-9_-]{16,64}$` |
| `subject_ref` | the Worker's subject binding | opaque; contains no identity, client code or period |
| `provider_idempotency_key` | the provider/message identity | derived from `operation_id` and nothing else |
| `recipient_identity` | the non-secret name for the recipient in logs and reports | binds the address without containing it |

Two further identities are **bound, not derived from the delivery identity**,
because they describe the configuration a submission was made under rather than
the delivery itself (§11.13):

| Bound | Purpose | Property |
|---|---|---|
| `provider_backend_id` | which provider backend/account/endpoint scope the idempotency key was issued against | `pbk_` + SHA-256 of provider type, account scope and endpoint scope. **No credential is an input** |
| `provider_message_fingerprint` | the one exact outbound message that key names | SHA-256 of recipient, subject, `Message-ID` and both bodies. One-way, so it stores no bearer |

Derivation is **defence in depth behind the durable row**, not a replacement
for it: a host that lost its ledger entirely still converges on the same remote
publication instead of opening an unrelated second one.

### 11.2 The state model

Thirteen states, no ceremonial ones. Each answers exactly one question — *after a
restart, what is the one safe next action?*

| State | Safe next action | Meaning |
|---|---|---|
| `PREPARED` | `POST /api/publish` | the local operation is durable. Covers "not published yet", "request in flight", "response lost" and "bearer received but not persisted" — all four have the same safe action, so they are one state |
| `BEARER_RECOVERY_REQUIRED` | `POST /api/publish/recover` | the Worker holds a grant this host cannot present. Re-publishing is not a legal move |
| `CAPABILITY_PERSISTED` | record delivery intent | the raw bearer is durable |
| `EXTERNAL_MAILER_HANDOFF` | nothing — the external mailer owns the send | the capability was handed to the existing Eco weekly/monthly mailing lifecycle (§12), which is authoritative for whether a message was sent. Terminal for the send, permanently provider-unbound, and it retains the bearer **while the grant is live** so a rerun hands over the **same** link. An expired capability is neither returned nor kept. If somebody re-mails the period, the delivery re-enters `BEARER_RECOVERY_REQUIRED`, which destroys the bearer, and rotates through the explicit recovery operation (§12.4); if nobody ever does, the retirement sweep moves it to `CAPABILITY_RETIRED`, which destroys it anyway |
| `DELIVERY_INTENT_RECORDED` | submit to the provider | remote `INTENT` and local intent both recorded; no provider call made |
| `PROVIDER_SUBMISSION_PENDING` | reconcile under the stable key | a message **may** exist. Written *before* the call |
| `PROVIDER_ACCEPTED` | `POST /api/publish/delivery` `DELIVERED` | acceptance established, with a message id |
| `PROVIDER_AMBIGUOUS` | operator reconciliation | acceptance is unknowable under this provider's abstraction |
| `PROVIDER_REJECTED` | bounded retry, or an operator | the provider definitively created no message |
| `REMOTE_DELIVERED` | clean up the bearer | the remote operation is terminal |
| `FINALIZED` | nothing | terminal success, bearer destroyed |
| `CAPABILITY_RETIRED` | nothing, and nothing is wrong | the grant's validity window passed and its raw bearer was destroyed by the ordinary lifecycle — **without anybody re-mailing that historical period**. It claims nothing about whether a message was sent and nothing about the snapshot, which is retained independently. The non-secret audit identity (`capability_id`, `capability_digest`, `capability_expires_at`, `bearer_generation`, any bound submission identity) survives, and migration 051 makes a retired row holding a bearer unrepresentable. A resend of the same period may still leave for `BEARER_RECOVERY_REQUIRED` and rotate (§12.4) |
| `OPERATOR_REQUIRED` | investigate | automation cannot make safe progress |

Illegal transitions are refused in Python (`LEGAL_TRANSITIONS`) **and**
unrepresentable in the schema: CHECK constraints make "a bearer-holding state
with no bearer", "delivered without a provider message id" and "finalised with
a raw bearer still stored" impossible rows.

#### 11.2.1 Re-arming a publication that was refused before any remote effect

`OPERATOR_REQUIRED` stays terminal for automation. The one case a **reviewed
operator** may reverse is a delivery the Worker refused at `POST /api/publish`:
the route computes the digest, decodes strictly, validates the snapshot and
checks canonical form, and every one of those refusals happens *before*
`publishSnapshot`, so no publication row, no R2 object and no capability were
ever created. When the cause was a defect outside the delivery, the row is
still exactly the `PREPARED` operation it was before the attempt.

```bash
# dry run — the default, writes nothing
python3 ops/recover_eco_dashboard_operator_required_delivery.py \
  --client-code <CODE> --delivery-id <uuid>

# execute — every flag mandatory
python3 ops/recover_eco_dashboard_operator_required_delivery.py \
  --client-code <CODE> --delivery-id <uuid> \
  --expect-operation-id <from the dry run> \
  --expect-payload-digest <from the dry run> \
  --operator "<who>" --reason "<on what evidence>" --execute
```

The transition is authorised by `OPERATOR_RECOVERY_TRANSITIONS`, which is a
separate map from `LEGAL_TRANSITIONS` and is not consulted by
`assert_legal_transition` — automation still cannot make this move. Eligibility
is the full `OPERATOR_RECOVERY_GUARDS` list, and every guard is a predicate of
the same `UPDATE` that writes, so a row that changed since the dry run is
refused by PostgreSQL rather than by a stale snapshot. It fails closed on any
evidence of a capability, a bound provider submission identity, a send, a remote
delivery, a live lease, a failure phase other than `PUBLICATION`, or a failure
code outside `OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES` (today:
`PAYLOAD_NOT_CANONICAL` alone). It **never infers remote cleanup** — it acts
only when the host's own durable facts prove there was never anything remote to
clean up.

`external_mailer` is **not** among those facts. It is immutable ownership
metadata bound by the INSERT (migration 050) that names WHICH lifecycle would
send the delivery, never that any lifecycle did anything, so requiring it to be
NULL made every externally-owned dashboard delivery permanently unrecoverable
for a fact carrying no remote effect. The external branch's own activity is
still proven absent by the capability guards: `EXTERNAL_MAILER_HANDOFF` is the
only state in which a mailer has been given anything, and
`chk_..._bearer_present` makes it unreachable without a capability id, secret,
digest, `bearer_persisted_at` and `bearer_generation >= 1` — each separately
required here to be absent or zero. Ownership survives the recovery unchanged:
it appears in no `SET` list and the row guard trigger refuses a change to it,
so the retry is re-armed as the same external mailer's delivery.

The reset is the smallest thing that makes a valid `PREPARED` row: `state`,
`operator_action_required`, the three failure columns and the lease. The
delivery identity, operation id, subject reference, payload and snapshot
digests, recipient binding and bearer generation are absent from the `SET`
list; the operation id and payload digest the operator names are additionally
asserted in the `WHERE` clause, and the row guard trigger refuses a change to
any of them regardless. The attestation is stored under `metadata_json ->
operator_recovery`. Exit codes: `0` recovered or reported, `2` no such client,
`3` no such delivery, `4` not eligible, `5` refused at the write.

Proved on disposable PostgreSQL by
`ops/tests_manual/test_eco_dashboard_operator_recovery_postgres.py`.

### 11.3 Ordering invariants

```
provider/config preflight      BEFORE any external effect at all
durable local operation        BEFORE any remote effect
raw bearer durably persisted   BEFORE any provider attempt is eligible
submission identity bound      BEFORE the first provider submission
"a submission may exist"       BEFORE the provider call
recorded provider acceptance   BEFORE the remote DELIVERED transition
remote DELIVERED confirmed     BEFORE the raw bearer is destroyed
```

The remote operation is never marked `DELIVERED` because a request was
attempted; only a recorded acceptance unlocks it, and `PROVIDER_ACCEPTED` is
reachable only from a submission that produced a message id.

### 11.4 Exact canonical bytes

`build_delivery_snapshot()` in `job_eco_dashboard_snapshot.py` is now the ONE
host path from Eco Driving data to publishable bytes, and both the read-only
generator job and the delivery job call it. Its `payload` is the exact
canonical octet sequence; the publisher sends those octets as the whole request
body and declares `SHA256` of them. The suite asserts octet equality between
the host output, the transmitted body, the declared digest and the ledger
digest — never semantic JSON equality, because the defect this contract exists
to prevent produced equal documents with different octets.

Different canonical bytes for a logical delivery that is already bound to a
digest is a local `PAYLOAD_CONFLICT`, refused before the Worker has to.

**The bytes are a function of the DATA, never of the clock.** The document's
`generated_at_utc` is derived from the persisted Eco stats rows the snapshot was
built from — the newest `updated_at` of the periods that actually contributed —
and never from `datetime.now()`. That is what makes the digest an identity
rather than a timestamp: the same driver, period and source data rebuilt an hour
or a month later produces byte-identical output, so a legitimate delayed rerun
(a recovery, a retry, an operator re-running one period) converges on the
existing publication instead of being refused as a payload conflict for data
that never changed. A genuine recalculation moves `updated_at`, which moves the
digest — which is exactly the conflict a changed payload is supposed to raise.
When a stats row carries no `updated_at` at all, the fallback is the period's
own closing boundary: also a stable fact about the data, and never the clock.

### 11.5 Provider boundary, idempotency and reconciliation

The abstraction declares two capabilities explicitly rather than assuming them:

| Provider | `supports_idempotent_submit` | `supports_reconciliation` |
|---|---|---|
| `SmtpEmailProvider` (adapter over the existing `jobs/common/emailer.py`) | **false** — a second `send_message` is a second message | **false** — nothing in SMTP answers "did you accept message X" |
| `FakeEmailProvider` (verification only) | configurable | configurable |

The host reads those flags instead of assuming them, so the reconciliation
policy is:

1. ask the provider about the idempotency key, if it can be asked;
2. replay under the same key **only** when the provider deduplicates by it — a
   replay that cannot duplicate is safe even if the lookup answer was wrong;
3. otherwise record `PROVIDER_AMBIGUOUS` and stop for a human.

Step 3 is the design, not a gap. A provider with neither property cannot be
replayed safely, and the alternative — resending on a guess — is precisely the
failure mode that mails a driver twice. **Under SMTP alone, a lost response
after submission ends in `PROVIDER_AMBIGUOUS` and needs an operator.** A
provider with real request idempotency is therefore a live-integration
requirement, not a nicety; §11.9 states exactly what such an adapter must
guarantee.

The `Message-ID` is derived from the idempotency key, so even a duplicate that
a non-idempotent provider might produce is identifiable as the same logical
message rather than looking like a second legitimate send.

An idempotency key alone is **not** idempotency. It means "the same message"
only relative to a backend that has heard of it and to the content it named,
and both are pinned before the first submission — see §11.13.

### 11.6 Recipient binding

One logical delivery is bound to one recipient before any provider submission.
A rerun naming a different address is `RECIPIENT_CONFLICT` and mutates nothing
— a retry must never silently redirect a driver's dashboard link. Domain case
is insignificant (RFC 5321) and reconciles; **local-part case is significant**
and conflicts, because two mailboxes differing only in local-part case are two
mailboxes.

The address and the driver identity key never leave the host: they are not in
any request the publisher transmits, and the suite proves it both by inspecting
every transmitted request and by scanning the local Worker's D1/R2 state.

### 11.7 The link

```
<dashboard-base-url>#k=<raw capability>
```

Fragment only. `build_capability_url` refuses an unconfigured base URL (no
hard-coded production domain — nothing is provisioned), a non-HTTPS base
(loopback excepted, for local verification), and any base that already carries
a query or a fragment. The message carries the link and concise report context
and **no** `subject_ref`, object key, operation id, client id, driver identity
key or authorization metadata. There is no attachment: the delivery artefact is
the authorized live view, and an attached snapshot would be an unauthenticated
copy of the same data sitting in a mailbox forever.

Subject and tone follow the existing Eco Driving notification convention so a
driver receives one recognisable family of messages.

### 11.8 Raw bearer lifecycle

`capability_secret` is ephemeral delivery material, not an audit record.

* it exists from the moment the publication (or recovery) returns it until the
  delivery is terminal;
* it is retained while a delivery is genuinely unresolved — including
  `PROVIDER_AMBIGUOUS`, because an operator reconciling that state may still
  need to establish which link the message carried;
* it is destroyed at finalisation. `capability_id`, `capability_digest` and
  `bearer_generation` survive, so which grant was delivered stays provable
  without the value existing anywhere;
* **and it is destroyed once its grant expires, whether or not anything else
  ever happens to that delivery.** Finalisation, an operator escalation and a
  rerun that rotates were the only three ways a bearer used to stop existing,
  and all three require something to happen *to* the row — so a historical
  period nobody re-mailed kept live secret material for as long as the row
  existed. `DeliveryLedger.retire_expired_capabilities()` closes that: see §13.

Three independent controls keep it out of diagnostics: the application scrubs
every persisted and logged string against every secret it holds;
`chk_eco_dashboard_delivery_operation_no_bearer_in_diagnostics` makes a stored
bearer inside `failure_code`, `failure_detail` or `metadata_json` an
unrepresentable row; and `trg_eco_dashboard_delivery_operation_guard` covers the
case the CHECK structurally cannot — an UPDATE that copies the bearer into a
diagnostic field **while clearing it**, whose resulting row no longer contains
the value to compare against (§11.13). Both database controls are exercised
directly, including at finalisation, which is the one moment clearing the
bearer is legal.

After an explicit bearer recovery the replacement overwrites the capability
identity in one statement, so a restart reads the replacement or nothing. The
suite proves the revoked predecessor appears in no constructed message.

### 11.9 What a live provider adapter must guarantee

Unresolved dependency, recorded rather than assumed away. A future adapter must:

* transmit the host's `idempotency_key` in whatever field that provider uses
  for request deduplication;
* return `ACCEPTED` only when the provider has taken responsibility for the
  message — never because an HTTP call completed;
* return `REJECTED` only when it definitively created no message;
* map every timeout, 5xx and dropped connection to `AMBIGUOUS`, never to
  `REJECTED`;
* set `supports_idempotent_submit` only if resubmitting the same key provably
  yields one message;
* implement `reconcile()` as a provider-side lookup of that key, never from
  local state.

No provider account was created and no provider was chosen as part of this
milestone.

### 11.10 Scheduler boundary

`job_eco_dashboard_publish.run(client, run_id, params)` is the standard job
contract, safe for scheduler retry and restart because it is re-entrant: each
invocation reads durable state, performs the one safe next action, and returns.
**It is registered in no schedule.** All Eco schedules remain disabled and
enabling one is a separate, separately authorized decision.

Concurrency is handled by a durable lease on the row (compare-and-set), not an
in-memory mutex, and every state mutation re-asserts
`(state, lease_owner, lease still unexpired)` in the statement that writes, so
neither a decision made against a row that has moved on nor one made by a holder
whose lease has since expired can be applied. `renew()` re-asserts the same
predicate immediately before each remote effect, so a stale invocation does not
initiate one either (§11.13). An expired lease is reclaimable, so a crashed
owner cannot park a delivery forever.

Mode defaults to `render_only`: it builds the canonical snapshot and the
message from a shape-only placeholder capability, prints the link **redacted**,
and performs no publication and no provider call. `execute` fails closed on
missing configuration rather than guessing an endpoint, a credential or a
domain.

### 11.11 Verification

`python3 ops/tests_manual/test_driver_eco_dashboard_email_contract.py` — 14
checks, no infrastructure at all: the state model and its illegal transitions,
identity determinism and length-prefix unambiguity, the fragment-only link and
its redaction, message content and its refusals, HTML escaping, the SMTP
adapter's honest capability declaration (exercised through an injected sender,
never a live server), every fake-provider outcome the crash matrix needs,
secret scrubbing, the `render_only` path, and source-level guards that no
schedule is registered and no live provider or deployment endpoint is
referenced.

`python3 ops/tests_manual/test_driver_eco_dashboard_publisher_lifecycle.py` —
17 checks against a **disposable** PostgreSQL instance and the **real Worker**,
mounted by `local/publisher_serve.js` on in-memory D1/R2 bindings and driven
over real HTTP. It refuses any DSN that is not loopback. The provider, the
driver data, the recipient and the machine credential are synthetic; the
publication, recovery and delivery semantics are the deployed ones.

| Proof | Result |
|---|---|
| operation durable before any remote call | operation id, subject, exact digest and recipient binding committed; zero remote operations exist; a restart finds the same row |
| normal lifecycle | 1 publication, 1 object, 1 grant, 1 live grant, remote `DELIVERED`, **1** accepted message, 1 submission, bearer destroyed, digest and grant identity retained |
| exact bytes | transmitted body ≡ host canonical octets ≡ declared digest ≡ ledger digest |
| publication response loss | retry answers `ALREADY_PUBLISHED`, host recovers explicitly; **no second publication, no second object**, one live grant, generation 1 → 2, one message |
| crash before bearer persistence | explicit recovery used, not a re-publish; the lost predecessor appears in **no** constructed message |
| provider accepted, response lost | restart reconciles under the same key: **one** message, one submission, acceptance recorded as `RECONCILED` |
| provider rejection | explicit `PROVIDER_REJECTED`, no message, the remote `DELIVERED` transition never attempted; a transient rejection retries under the same key and converges to one message |
| provider ambiguous (no idempotency, no lookup) | `PROVIDER_AMBIGUOUS`, operator required, **no second message** across repeated invocations — against a fake that would have duplicated |
| recipient conflict | refused, nothing mutated, no message to the other address; domain case reconciles, local-part case conflicts |
| host-only data | recipient address, identity key and client id absent from every transmitted request and from Worker/D1/R2 state, with a control probe proving the scan finds what IS there |
| crash matrix, boundaries 1–12 | every boundary has one safe next action, and every one converges without a duplicate message |
| concurrency 2 / 8 / 32 | 1 host row, 1 publication, 1 object, 1 live capability, 1 provider identity, ≤ 1 accepted message, exactly 1 remote `DELIVERED`, and **exactly one invocation performed any durable step** |
| expired lease | reclaimed; a live lease blocks, an expired one does not |
| bearer retention | held at every unresolved state, destroyed at finalisation, no historical bearer retained for audit; an ambiguous delivery keeps its bearer |
| diagnostics | the CHECK constraint refuses a raw bearer in `failure_code`, `failure_detail` and `metadata_json`; a non-secret diagnostic is accepted |
| secret leakage | no capability and no machine credential on the event log, the job summary, the record repr, the ledger (outside the one permitted column) or the Worker inspection; the operator summary still answers every operational question and names the recipient by identity, not address |

### 11.12 Remaining dependencies

Everything in §10.4's release list still applies, plus:

* a live e-mail provider adapter meeting §11.9, and the operator decision of
  which provider. Until one exists, **an ambiguous submission needs a human**
  (§11.13.9);
* `ECO_DASHBOARD_BASE_URL`, `ECO_DASHBOARD_PUBLISHER_URL` and the machine
  credential are unset, so `execute` mode refuses to run;
* the client-business schema dependency is **closed**.
  `db/client_business/049_eco_dashboard_delivery_operation.sql` was applied on
  all five enabled client business databases (2026-08-19 ~19:55:44 CEST) in its
  pre-review physical form, and the forward migration
  `db/client_business/050_eco_dashboard_external_mailer_ownership.sql` — what
  establishes the reviewed contract and what `db/schema_requirements.json` keys
  the requirement on (§11.13.5, §11.16) — **is now applied on all five as well**
  (§11.16.0). Schema is no longer what blocks activation;
* activation of the currently disabled Eco schedules remains a separate,
  separately authorized decision.

### 11.13 Host lifecycle remediation (milestone 6a)

Eight defects found by independent review of §11. Each is stated as the
invariant that now holds, with the failure it closes.

#### 11.13.1 The provider backend is bound, not assumed

**Was:** the ledger stored `provider_name` and nothing compared it. Backend A
accepted a message, the response was lost, the host restarted onto backend B,
and B — which had never heard of the idempotency key — created a second one.

**Now:** `record_delivery_intent()` binds `provider_backend_id` before anything
may submit, and every provider-facing step re-derives the configured backend and
refuses a mismatch **before** contacting anyone. The scope is provider type +
account/tenant + endpoint/environment, so a staging endpoint and a second
account are different backends. **No credential is an input**, so a password
rotation is the same backend and no secret enters a persisted identity.

| Situation | Behaviour |
|---|---|
| same backend, response lost | normal reconciliation under the bound key |
| different backend | `PROVIDER_BACKEND_CONFLICT`, `OPERATOR_REQUIRED`, **zero** submissions and **zero** reconciliation calls to the new backend |
| same provider type, different account or endpoint | same refusal |
| credential rotated, account unchanged | same backend; the delivery continues |

#### 11.13.2 One idempotency key names one immutable message

**Was:** the message was rebuilt from live configuration at submit time. Changing
`dashboard_base_url` after a restart produced different outbound content under
the same stable key.

**Now:** `provider_message_fingerprint` — SHA-256 over recipient, subject,
`Message-ID` and both bodies — is bound with the key. Because the capability
link lives in the bodies, one comparison covers a changed base URL, a changed
template, a changed `Message-ID` domain and a replacement bearer generation.
Any mismatch is `MESSAGE_CONFLICT`: operator required, **zero** provider
submissions, and the key is never reused with different content. A new key is
never minted to "solve" drift — one logical delivery keeps its provider identity
for life. The check is made before **every** provider interaction, not only
before a first submit; see §11.14.1, which is the gap the second pass closed.

**The immutability boundary.** Before any submission may occur, the current
capability is durable, the backend is bound and the message identity is bound.
After that point all three are immutable — enforced in Python and, independently,
by `trg_eco_dashboard_delivery_operation_guard`, so not even a direct SQL edit
can repoint a key at another backend or another message. Bearer recovery is
reachable only from `PREPARED`, i.e. strictly *before* the boundary, so a
replacement bearer may legitimately become the message; after it, a changed
bearer is a message conflict rather than a silent replay — including on the
reconciliation path (§11.14.1).

#### 11.13.3 Only real loopback may receive a plaintext credential

**Was:** `base.startswith("http://localhost")`. `http://localhost.attacker.invalid`
and `http://127.0.0.1.attacker.invalid` both passed, and the machine credential
rides on every request.

**Now:** `validate_publisher_endpoint()` parses the URL and asks the parser which
part is the host. Plain HTTP is accepted only for `localhost` or an address
`ipaddress` reports as loopback (so all of `127.0.0.0/8` and `::1`); userinfo, a
query, a fragment, whitespace, a malformed port and a missing host are refused
outright; everything else must be HTTPS. HTTPS is necessary but not sufficient:
the host must also be a syntactically valid destination (§11.14.4). Validation happens in the transport
constructor — before a `SecureDeliveryClient` holding the credential exists — and
the job repeats it before constructing anything, so a refused destination never
has a credential attached to a request aimed at it.

#### 11.13.4 Ownership is fenced in both directions

**Was:** every transition matched `lease_owner` but not lease expiry. Owner
equality is not ownership: between expiry and takeover the row still names the
stale holder, which is exactly the window a crashed process wakes up in. An
expired holder committed a transition, and another owner then claimed the row.

**Now:** every fenced write carries
`state = … AND lease_owner = … AND lease_expires_at > now()` in the same
statement that writes. `renew()` re-asserts that predicate — and extends the
lease — immediately before each remote effect is *initiated*: publication,
lost-bearer recovery, the remote `INTENT` transition, the provider submission,
provider reconciliation and remote `DELIVERED`. `holds_lease()` answers the same
question read-only. Provider reconciliation was added to that list by the second
pass (§11.14.2); it is an external call like any other.

No claim is made that a database lease is atomic with an HTTP call. A lease can
still expire mid-flight; that residual window is covered by ordering, not
locking. `PROVIDER_SUBMISSION_PENDING` is committed before the call, so a
taking-over owner reconciles under the bound identity rather than resending, and
against a provider that cannot deduplicate it stops at `PROVIDER_AMBIGUOUS`
instead.

#### 11.13.5 Migration 049 is usable by the runtime role, on both rollout paths

**Was:** the migration granted nothing, so the per-client runtime role the job
connects as had no access at all; and new-client onboarding did not apply it.

**Now:**

* the migration mirrors the DML grantees of `public.client_trips` — the
  established way a client-business migration names "the roles of this database"
  without knowing the role name — and grants **`SELECT, INSERT, UPDATE` only**.
  `DELETE`, `TRUNCATE`, `REFERENCES` and ownership are deliberately withheld:
  nothing in the lifecycle removes a row. `EXECUTE` on the guard function is not
  granted either, because PostgreSQL checks that privilege when a trigger is
  *created*, not when it fires;
* `scripts/onboard_workflow_a_client.py` applies 049 to a new client, records it
  in that database's `public.schema_migrations`, **and names the table in its own
  grant list**. That last part is load-bearing and easy to miss: onboarding runs
  `apply_client_ddl` before `apply_grants`, so when 049 executes for a new client
  the `client_trips` grants it mirrors from do not exist yet and its own block is
  a no-op. A migration-list entry alone would leave a freshly onboarded client
  with a table its runtime role cannot touch — the same defect, moved. Both paths
  land on the same `SELECT, INSERT, UPDATE`;
* `scripts/apply_client_business_migrations.py` reaches existing clients by
  directory scan, which 049 already satisfies;
* `db/schema_requirements.json` declares 049 in the `client_business` scope, so
  release activation refuses a fleet that has not received it. What that
  declaration has to contain is §11.15: a recorded migration is a claim, and the
  requirement states the physical schema instead.

**Operational consequence, stated plainly.** A release built from this tree
therefore requires 049 to be applied to every enabled client business database
**before** it can be activated. That is the repository-standard order — migrate
the fleet, then activate — and the same sequencing 047 and 048 used. **049 has
not been applied to any persistent, staging or production database, and doing so
is a separate, separately authorized operation.**

#### 11.13.6 The bearer cannot be copied into diagnostics

**Was:** one UPDATE set `failure_detail = capability_secret` and
`capability_secret = NULL` together. The resulting row no longer contained the
value the CHECK compares against, so it passed.

**Now:** `trg_eco_dashboard_delivery_operation_guard` is a `BEFORE INSERT OR
UPDATE` trigger that checks the diagnostic fields against the bearer in **both**
the NEW and the OLD row. The CHECK is retained; two controls covering one
invariant from different directions. The guarantee is bounded and checkable — it
is not a universal secret scanner: the raw bearer *this row holds or held* cannot
reach `failure_code`, `failure_phase`, `failure_detail` or `metadata_json` by any
supported write, alone, embedded in surrounding text, or nested inside the
metadata document. Legitimate finalisation still succeeds.

#### 11.13.7 Incomplete lifecycle states are unrepresentable

**Was:** the schema accepted rows describing a guess rather than one safe next
action — provider intent with no provider identity, `PROVIDER_AMBIGUOUS` without
the bearer its own operator path needs, a lease owner with no expiry.

**Now:**

| Rule | Constraint |
|---|---|
| every state from `DELIVERY_INTENT_RECORDED` onwards carries the full submission identity | `chk_…_provider_key_present` |
| the binding is written as one unit, and cannot exist before the binding step | `chk_…_binding_coherent`, `chk_…_provider_unbound` (§11.14.3) |
| `PROVIDER_AMBIGUOUS` retains the bearer its operator reconciliation needs | `chk_…_bearer_present` |
| lease ownership is a pair, and a named owner is non-blank | `chk_…_lease_pairing` |
| backend id, fingerprint, bound capability and bound generation are well-formed | four format CHECKs |

Every state the code can persist has an accepted fixture, asserted explicitly:
over-constraining a legitimate crash-recovery state would be its own defect.

#### 11.13.8 Provider configuration is preflighted before any external effect

**Was:** a missing SMTP host was discovered inside `submit()` — after the
snapshot was published, after a capability was issued and after
`PROVIDER_SUBMISSION_PENDING` was committed. A restart then landed in
`PROVIDER_AMBIGUOUS` for a provider that had never opened a socket: a definite
local failure recorded as a possible acceptance.

**Now** `preflight()` runs before anything else in `advance_delivery`, and the
job repeats it before opening a connection. It establishes the machine
credential, that the dashboard base URL can carry a link, that the `Message-ID`
domain is usable, and that the provider adapter is configured and names a stable
backend scope. **It contacts nothing** — SMTP has no non-effecting validation
call, and connecting to prove one could connect would be a remote effect
performed by the check meant to precede remote effects.

Provider outcomes are now four, not three:

| Outcome | Meaning | State |
|---|---|---|
| pre-submit configuration failure | established before any network submission | preflight refusal, or `PROVIDER_REJECTED` if the marker was already committed |
| definite rejection | the provider created no message | `PROVIDER_REJECTED` |
| acceptance ambiguous | a message may exist | `PROVIDER_AMBIGUOUS` |
| accepted | the provider took responsibility | `PROVIDER_ACCEPTED` |

A failure before any provider network submission can no longer become
`PROVIDER_AMBIGUOUS`.

#### 11.13.9 What did not change

`SmtpEmailProvider` still declares `supports_idempotent_submit = False` and
`supports_reconciliation = False`, because that is the truth about SMTP.
**SMTP is not safe for automatic ambiguous retry, and nothing here pretends
otherwise**: an ambiguous SMTP submission stops at `PROVIDER_AMBIGUOUS`, keeps
its bearer and provider identity for the operator, and is not resubmitted by any
restart. `render_only` remains free of external effects. No live provider was
called, no e-mail was sent and no provider account exists.

#### 11.13.10 Verification

`ECO_DASHBOARD_PUBLISHER_TEST_DSN=… python3 ops/tests_manual/test_driver_eco_dashboard_host_remediation.py`
— 37 checks against a disposable PostgreSQL instance and the real Worker (25 at
this milestone, extended by §11.14).

| Proof | Result |
|---|---|
| loopback boundary | 6 accepted forms, 38 refused with exact codes (16 at this milestone, extended by §11.14.4), including both reproduced lookalikes, userinfo tricks and malformed hosts; **no request is attempted** to any refused endpoint |
| backend swap after accepted-response-lost | backend B receives **zero** submissions and **zero** reconciliations; explicit `PROVIDER_BACKEND_CONFLICT`; A still holds exactly one message |
| same backend after accepted-response-lost | reconciles to `FINALIZED` with **one** message and no new submission |
| account/endpoint scope | a different account or endpoint is a different backend and is refused; a rotated credential is not |
| message drift before a first submit | base URL, `Message-ID` domain, subject template and bearer generation each produce `MESSAGE_CONFLICT` with **zero** submissions; an unchanged configuration still sends normally |
| recipient drift | `RECIPIENT_CONFLICT` before any work; the stored recipient is unchanged |
| lease expiry | a valid owner writes and renews; at and after expiry the write is refused, the state is untouched, and renewal fails; after takeover the stale holder is refused again |
| stale remote effects | all five boundaries — publish, recover, remote `INTENT`, provider submit, remote `DELIVERED` — refuse **before** the effect; zero requests transmitted, zero provider calls |
| lease lost mid-submission | the taking-over owner reconciles under the bound key against the bound backend; **exactly one** message exists |
| takeover, non-deduplicating provider | zero submissions, `PROVIDER_AMBIGUOUS`, still one message — against a fake that would gladly have made a second |
| transaction rollback | no phantom lease survives; the row is claimable |
| 2 / 8 / 32 concurrent with a stale holder present | one lifecycle, one provider attempt, one submission for the key, `FINALIZED` |
| missing provider configuration | four broken configurations each fail preflight: **zero** publication, **zero** object, **zero** capability, nothing transmitted, no durable row |
| provider preflight raises / no backend scope | same, reported as a preflight failure rather than an ambiguity |
| configuration broken after binding | preflight refusal with the delivery untouched and zero attempts; repairing it completes the delivery |
| configuration broken between check and call | `PROVIDER_REJECTED` — definite, no acceptance, remote `DELIVERED` never attempted |
| SMTP capability declaration | both flags false; a reconciliation request is answered `UNSUPPORTED` |
| SMTP definite rejection | `PROVIDER_REJECTED`, no message, never `DELIVERED` |
| SMTP ambiguity | `PROVIDER_AMBIGUOUS`, exactly one attempt, bearer and provider identity retained, and a **restart makes no further submission** |
| new crash boundaries | the submission identity is durable before any submit, and a restart completes the delivery |

`ECO_DASHBOARD_SCHEMA_TEST_DSN=… python3 ops/tests_manual/test_driver_eco_dashboard_delivery_schema_postgres.py`
— 9 checks that attack the schema directly, **as the runtime role**.

| Proof | Result |
|---|---|
| runtime-role lifecycle | every INSERT and UPDATE shape the ledger issues runs to `FINALIZED` as the runtime role, not the owner |
| minimum privileges | exactly `SELECT, INSERT, UPDATE`; `DELETE`, `TRUNCATE`, constraint DDL and a direct call to the guard function are all refused |
| bearer diagnostics | copy-and-clear refused in every generic column **and at finalisation**, prefixed, suffixed and nested forms refused, no refused write partially applied, clean finalisation still accepted |
| state completeness | every durable state has an accepted fixture; 19 incomplete or malformed permutations are rejected |
| binding immutability | six bound fields and four identity fields refuse any change; a first binding from `NULL` is permitted |
| rollout paths | 049 is in the onboarding DDL list, the onboarding ledger list, **onboarding's grant list**, the existing-client directory scan and `db/schema_requirements.json` |
| requirement fidelity | the declared requirement is decided against a really-applied 049 by `requirement_defects`, the function activation itself uses (§11.15) |

Mutation checks, run to prove the suites detect the defects rather than merely
describing them: removing the lease-expiry predicate, ignoring the bound
backend, ignoring the bound fingerprint, dropping the guard trigger and dropping
the grant block each reproduce the original finding and fail the suites.

Regression: the §11.11 lifecycle suite (17 checks) and the §11.11 contract suite
(14 checks) pass unchanged in behaviour, together with the byte-integrity,
retry-integrity, recovery-integrity, prepublisher, publication-transaction,
delivery, snapshot and frontend suites, and the release preflight, activation
fence and M-LAG PostgreSQL suites.

### 11.14 Second-pass host lifecycle remediation (milestone 6b)

Five defects found by a second independent review of §11.13. §11.13's accepted
areas — backend binding, the secure-delivery Worker/R2 foundation, migration 049
integration, provider preflight and SMTP honesty — are unchanged; each item below
states the invariant that now holds and the failure it closes.

#### 11.14.1 The bound message is verified before *every* provider interaction

**Was:** `_bound_message()` ran before a first submit and before a replay, but
not before provider **reconciliation**. So: provider A accepted the message, the
response was lost, the operation became `PROVIDER_SUBMISSION_PENDING`, an input
changed, and the restarted host verified only the backend. The lookup answered
"a message exists under key K", the host adopted that acceptance and finalised —
recording that a driver had received content the driver was never sent.

**Now:** one helper, `_provider_guard()`, is the single contract every
provider-facing step passes through, in one order:

```
live lease  →  bound provider backend  →  bound immutable message  →  provider
```

It governs `_record_intent`, `_submit`, `_reconcile` and `_mark_remote_delivered`
— that is, the initial submission, the retry after a definite rejection, the
reconciliation lookup, the idempotent replay, the adoption of provider acceptance
and the progression to remote `DELIVERED`. Provider existence alone never
legitimises changed content: the host adopts a lookup result only while the
current operation, recipient, capability and persisted fingerprint still identify
the exact message the key was issued for.

A mismatch is `MESSAGE_CONFLICT` before the provider is contacted: zero
reconciliation calls, zero submissions, no acceptance adopted, no remote
`DELIVERED`, the persisted fingerprint untouched, no fresh idempotency key, and
the operation left inspectable for an operator.

#### 11.14.2 Provider reconciliation is lease-fenced before the call

**Was:** §11.13.4 fenced publication, recovery, remote `INTENT`, submission and
remote `DELIVERED` — but not reconciliation. A worker whose lease had expired
still issued one provider lookup; only its subsequent ledger write was refused.
A post-effect fence is not enough when the effect is the thing the stale process
then acts on.

**Now:** reconciliation renews-and-checks `(state, lease_owner, lease still
unexpired)` before it initiates anything, so an expired or superseded holder makes
**zero** provider calls. The residual window is unchanged and stated as before: a
lease can still expire during an in-flight call, which ordering — not locking —
covers, and a late answer's durable write is refused by the same predicate.

#### 11.14.3 Provider binding is all-or-none, and cannot exist early

**Was:** `chk_…_binding_coherent` anchored coherence on `provider_backend_id`, so
a row could carry `provider_idempotency_key` and/or `provider_name` alone. The
guard trigger then froze those columns, and the legitimate atomic bind that should
have followed was refused for the rest of the row's life: a durable state with no
valid next action.

**Now:** two constraints, and the trigger extended to `provider_name`:

| Invariant | Control |
|---|---|
| the six binding columns — `provider_name`, `provider_idempotency_key`, `provider_backend_id`, `provider_message_fingerprint`, `provider_bound_capability_id`, `provider_bound_bearer_generation` — are all present or all absent | `chk_…_binding_coherent` |
| none of them may exist in `PREPARED`, `BEARER_RECOVERY_REQUIRED` or `CAPABILITY_PERSISTED` | `chk_…_provider_unbound` |
| every state from `DELIVERY_INTENT_RECORDED` onwards carries all of them | `chk_…_provider_key_present` |
| once written, none of them changes | `trg_…_guard` |

`CAPABILITY_PERSISTED → complete binding → PROVIDER_SUBMISSION_PENDING` remains a
normal transition, and every previously valid state is still representable. The
schema and `record_delivery_intent()` therefore agree: no supported SQL write can
produce a state Python cannot safely continue from.

#### 11.14.4 A malformed HTTPS host is refused before a credential-bearing request

**Was:** the HTTP loopback-lookalike fix of §11.13.3 left HTTPS trusted on scheme
alone. `https://-`, `https://_` and `https://%zz` all parse to a non-empty
hostname, which was the only test applied, so a request carrying the machine
credential was constructed for a destination that can never name a host.

**Now:** `_validated_host()` decides the question from syntax, resolving nothing,
before the scheme branches and therefore before any client holding a credential
exists. Three legitimate shapes, two of them decided by `ipaddress` rather than by
a regular expression: a bracketed IPv6 literal (zone ids refused — they name an
interface, not a destination), an IPv4 literal, or a DNS hostname of LDH labels
(RFC 1123 §2.1) within 253 octets. Punycode passes unchanged, so an
internationalised Cloudflare custom domain is not overrejected.

#### 11.14.5 The e-mail contract suite is infrastructure-independent in fact

**Was:** the suite claimed injection-only isolation while constructing
`SmtpEmailProvider(sender=…)` with no config, which resolves `AUTOMATION_SMTP_*`
from the ambient environment. It passed on a configured operator machine and
failed on a clean one.

**Now:** every SMTP check injects an explicit synthetic `SmtpConfig` and an
injected sender, and one check watches `os.getenv` to assert that no
`AUTOMATION_SMTP_*` variable is read at all. The suite passes in a deliberately
empty environment, opens no socket, and reads no real credential.

#### 11.14.6 Verification

| Proof | Result |
|---|---|
| post-response-loss drift: base URL, `Message-ID` domain, subject, text body, HTML body, bearer generation | each `MESSAGE_CONFLICT` **before** the lookup: zero reconciliations, zero submissions, no `provider_message_id`, no `remote_delivered_at`, one accepted message still on the backend |
| post-response-loss drift: recipient | `RECIPIENT_CONFLICT` earlier still, zero provider calls |
| post-response-loss, unchanged | reconciles to `FINALIZED`: the lookup happens, no new submission, exactly one message, the bound fingerprint intact |
| reconciliation lease fence | a live owner reconciles and adopts the acceptance; an expired holder, a stale holder after takeover, and a holder that expired mid-lifecycle each make **zero** reconciliation calls and leave the state untouched |
| lease expiring during the lookup | the late writer is refused; the new owner converges on the same message; exactly one accepted message survives |
| 2 / 8 / 32 concurrent reconciliations with a stale holder present | one lifecycle, one submission, one accepted message, one remote `DELIVERED` |
| partial provider binding | every non-empty proper subset of the six binding columns, in all three pre-binding states, plus a complete *early* binding, is rejected — by INSERT and by UPDATE |
| legitimate atomic bind | `CAPABILITY_PERSISTED` → all six columns in one statement → `DELIVERY_INTENT_RECORDED` accepted; every bound column then refuses change |
| malformed HTTPS hosts | 22 refused forms including `https://-`, `https://_`, `https://%zz`, malformed percent-encoding, invalid labels, invalid IPv4/IPv6 literals, zone ids, userinfo, query and fragment: **zero** transport calls and the synthetic credential marker absent from every refusal, log, `repr` and exception |
| valid endpoints | 9 HTTPS forms (custom domain, `workers.dev`, port, deep path, punycode, root label, IPv4, bracketed IPv6) and the 6 permitted loopback forms still construct a usable transport |
| clean-environment e-mail contract | 15 checks pass with the SMTP environment removed entirely and identically in the normal shell; zero `AUTOMATION_SMTP_*` reads, zero sockets |

Suites: `test_driver_eco_dashboard_host_remediation.py` (37 checks),
`test_driver_eco_dashboard_delivery_schema_postgres.py` (9 checks),
`test_driver_eco_dashboard_publisher_lifecycle.py` (17 checks) and
`test_driver_eco_dashboard_email_contract.py` (15 checks), all repeated to check
for flakiness, on a task-owned disposable PostgreSQL 16 and the local Worker.
Regression unchanged: byte-integrity, delivery, prepublisher,
publication-transaction, recovery-integrity, retry-integrity, snapshot and
frontend suites, plus release schema preflight, activation fence and the M-LAG
bridge/race/closure/rollback suites.

**Status after this pass.** `SCHEMA_INTEGRATION_READY: YES` was claimed here and
**was premature** — independent review then built a database that recorded 049
while its physical schema was malformed and watched activation pass. §11.15 is
that finding and its closure; the readiness claim belongs there, not here.
`SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY: NO` — unchanged and not addressed here;
an ambiguous SMTP submission still ends with an operator. No live provider adapter
exists, and §11.12's release prerequisites are all still open.

### 11.15 Migration 049 is proved physically at activation (milestone 6c)

#### 11.15.1 The finding

A client database recorded `049_eco_dashboard_delivery_operation.sql` in
`public.schema_migrations`, had `provider_name` dropped, and carried every
required CHECK as a same-named `CHECK (true)`. **Release schema preflight
passed.** The publisher then failed on its first `DeliveryLedger.load()` with
`UndefinedColumn: provider_name`.

The declaration was the whole defect. It named the relation, a *subset* of
columns that did not include `provider_name`, and six constraints **by name**.
Every one of those assertions held against that database, so the gate was
correct about what it was asked and the question was wrong.

**A migration-ledger entry is necessary and not sufficient.** It records an
intent that a hand-edited row, a restored dump or a half-applied file can
contradict, and it is exactly what a broken rollout leaves looking healthy.

#### 11.15.2 What activation now proves

The requirement states the physical contract; the validator generically decides
whether PostgreSQL satisfies it. Nothing in the release code branches on the
number 049.

| Object | What is verified |
|---|---|
| columns | all 43 of `delivery_ledger.COLUMNS` — the projection every statement names explicitly — with `data_type`, nullability and the **exact default state, absence included**: each column declares either the canonical `column_default` it must carry or that it must carry none at all |
| CHECK / UNIQUE / PRIMARY KEY | all 32, each against its canonical `pg_get_constraintdef` **definition** plus `convalidated`; a name alone proves nothing |
| guard trigger | timing, event set, row level, the schema-qualified function it executes, its enabled state, the absence of a `WHEN` condition and of an `UPDATE OF` column list — read structurally from `pg_trigger`, not from `pg_get_triggerdef`, whose output depends on `search_path` |
| guard function | language, result type and **SHA-256 of `pg_proc.prosrc`**. Structure cannot see a body replaced by `BEGIN RETURN NEW; END`, and that replacement removes both invariants no CHECK can express |

Non-unique indexes are deliberately not declared: they are access paths. The
exclusion is bounded rather than assumed — a unique index backing no constraint
would be a uniqueness contract, and its appearance fails the fidelity test until
it is declared.

Requirement-model additions, all generic and all optional, so no existing
declaration is narrowed or widened: `columns[].default`, `relations[].triggers`
and a requirement-level `functions` list.

**Absence of a default is load-bearing schema.** `DeliveryLedger.ensure_operation()`
names 13 of the 43 columns; the other 30 are either filled from a declared
default or must stay implicitly NULL. Independent review defeated the first
version of this gate by ADDING one — `provider_name DEFAULT 'fake'` — which
passed preflight and then broke that INSERT on
`chk_..._binding_coherent`, because a `default` of `None` could only mean "not
checked" and never "must have no default". `columns[].default` therefore has
three distinguishable states and no overloaded one:

| Declaration | Meaning |
|---|---|
| key omitted | the requirement does not check this column's default |
| `{"state": "absent"}` | the column must physically carry **no** default |
| `{"state": "expression", "expression": "…"}`, or the equivalent string shorthand | the column must carry exactly that canonical default |

`null` is refused outright, precisely because it is the spelling that used to
mean two things. All 43 columns of migration 049 declare their exact state.

#### 11.15.3 Failure semantics

A malformed physical object is refused with the repository-standard
`RELEASE_SCHEMA_CLIENT_OBJECT_MISSING`, naming the failing object and defect
kind and nothing secret. An unrecorded 049 remains
`RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING`. A declaration the gate cannot fully
parse is `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED` and refuses the activation
before any schema is inspected. There is no separate activation path for the
Eco Dashboard, and **activation is now blocked when 049 is absent, when it is
recorded but physically incomplete or weakened, and when it carries a default
the migration never created.**

The declaration source has its own two-way split, because the two answers send
an operator to different places. `RELEASE_SCHEMA_REQUIREMENTS_UNREADABLE` means
the source could not be obtained at all — a permission or I/O failure, or a
directory where the file must be — and is about the filesystem.
`RELEASE_SCHEMA_REQUIREMENTS_MALFORMED` means the bytes arrived and are wrong:
invalid or truncated JSON, an empty file, a non-UTF-8 file, or a syntactically
valid document making a declaration the parser refuses. Independent review found
these grouped under `UNREADABLE`, which reported a perfectly readable truncated
file as a filesystem problem; they are now distinct. A release tree carrying no
requirements file at all is unchanged and still passes as
`release_predates_schema_requirements`, because refusing it would make every
historical release un-rollback-able.

The full classification sequence is therefore, in order and mutually distinct:
unreadable source → `RELEASE_SCHEMA_REQUIREMENTS_UNREADABLE`; malformed JSON or
malformed declaration → `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`; valid
requirements with 049 unrecorded → `RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING`;
recorded but physically defective → `RELEASE_SCHEMA_CLIENT_OBJECT_MISSING`; the
exact 049 state → PASS. No malformed declaration can fall through into a
physical-schema result, because parsing completes before any database is opened.

#### 11.15.4 Declarations themselves are parsed fail-closed

The declaration is the gate, so a typo in it is a weaker gate. Independent
review demonstrated all three shapes of that on this file: `"defualt"` and
`"enabeld"` were unknown keys that were silently ignored — removing the
assertion with no diagnostic — and `"nullable": "false"` was coerced by
`bool("false")` into `True`, i.e. enforced as the opposite of what it read.

`parse_requirements` now validates every declaration object against an explicit
key set and every value against its exact JSON type. Unknown or misspelled
keys, wrong primitives (including any string, integer or `null` where a JSON
boolean is required), unsupported enum values, structurally incomplete default
declarations and duplicate object declarations are all
`RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`. There is no permissive coercion
anywhere: booleans must be JSON booleans. `_comment` is reserved on every
object and asserts nothing, and an optional property is expressed by omitting
it, never by `null`. Every historical declaration form — name-only constraints,
requirements without `functions`, relations without `indexes` or `triggers` —
still parses, so rolling back to an older release stays possible.

Duplicate rejection reaches inside `constraint_alternatives` as well. A group is
satisfied only when **all** of its constraints hold, so two declarations of one
constraint identity in one group are redundant when they agree and unsatisfiable
when they do not; independent review duplicated the 048 EXPAND constraint, saw
the document parse and saw physical matching report no defect. Duplicate
identities within a group are now `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`, by
the same canonical identity — the constraint name — every other duplicate check
uses. The scope is deliberately one group: an expand-contract span may name the
same constraint in both states, and two distinct relations may carry identically
named constraints, and both remain valid. The 048 EXPAND and CONTRACT
declarations parse unchanged.

#### 11.15.5 Verification

| Proof | Result |
|---|---|
| control | the exact untouched migration satisfies the requirement, before and after the whole corruption matrix |
| the reviewed defect, replayed | the pre-fix declaration accepts `provider_name` removed + six same-named `CHECK (true)`; the current one refuses it, naming `provider_name` |
| columns | all 43 projected columns removed one at a time, a retype and both nullability directions — every one refused |
| column defaults | the complete 43-column matrix: each of the 34 no-default columns given a type-appropriate default (`provider_name DEFAULT 'fake'` named explicitly), each of the 9 explicit defaults dropped and materially changed — all refused; each explicit default re-spelled in an equivalent source form PostgreSQL canonicalizes identically — all still pass |
| runtime agreement | on the exact migration `DeliveryLedger.ensure_operation()` succeeds; with `provider_name DEFAULT 'fake'` preflight refuses **before** runtime is attempted, and the same corruption is confirmed to break that INSERT |
| declaration parsing | 102 checks over unknown/misspelled keys at every level, wrong primitives, `"false"` as a boolean, enum values, malformed default declarations, duplicate objects and duplicate constraint identities inside one `constraint_alternatives` group — all `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`; plus the source split (unreadable file → `UNREADABLE`; invalid, truncated, empty, non-UTF-8 or semantically invalid document → `MALFORMED`; no file → the historical `release_predates_schema_requirements` pass); the repository's own declaration, both 048 alternative states and every historical form still parse |
| CHECK definitions | each of the 28 CHECKs replaced by a same-named `CHECK (true)` and separately dropped; one materially weaker predicate; one same-named recreation `NOT VALID` — all refused |
| uniqueness | each of the three UNIQUE contracts removed, replaced by a same-named **non-unique index**, and replaced by a same-named UNIQUE over the wrong columns; the primary key re-keyed — all refused |
| guard trigger | removed, disabled, `ENABLE REPLICA`, rebound to a same-shaped no-op function, retimed to AFTER, narrowed to INSERT, narrowed to `UPDATE OF state`, given `WHEN (false)`, demoted to STATEMENT — all refused |
| guard function | body replaced by a no-op, and dropped `CASCADE` — both refused |
| release classification | a fleet recording 049 with any of those corruptions — including an added default — is refused `RELEASE_SCHEMA_CLIENT_OBJECT_MISSING`; a release whose declaration is malformed is refused `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`; an unrecorded 049 stays `RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING`; the exact fleet passes |
| requirement fidelity | the declaration equals the catalog 049 itself produces — columns with type, nullability and **exact default state including absence**, every CHECK/UNIQUE/PK definition, trigger binding and function digest — so a migration edit that adds, removes or changes any default without synchronizing the declaration fails the suite |
| new client | onboarding's own DDL, ledger and grant lists produce a database whose runtime role can INSERT/SELECT/UPDATE the ledger, holds no DELETE/TRUNCATE/DDL, and which passes the physical requirement |
| existing client | a pre-049 client is refused activation, `scripts/apply_client_business_migrations.py --apply` applies and records 049, the runtime role can then use the ledger, and the physical requirement passes |

The gate is also proved read-only by the database rather than by a source
scan: `test_B11_the_gate_mutates_nothing` runs the whole preflight against
sessions started with `default_transaction_read_only=on`, checks every
statement the cursor actually sent, and includes a recurrence detector in which
a preflight step is temporarily replaced by one assembling `"UP" + "DATE …"` at
runtime — PostgreSQL refuses it, so the proof covers dynamically assembled SQL
that no literal scan can see, while prose mentioning mutating verbs cannot
produce a false positive.

**That evidence used to stop at the test's own connections.** Independent review
established that `default_platform_conn` and `default_client_conn` — the
factories `manage_release.py activate` actually calls — connected with no
read-only option at all, so every production preflight ran on a session
PostgreSQL would have allowed to write; only the injected `_readonly_connect`
path was protected. Both default factories now pass
`options=-c default_transaction_read_only=on` as a libpq **startup** option, so
the mode is in force from the first statement of the first transaction, `BEGIN`
does not reset it, and a runtime-assembled write is refused with SQLSTATE 25006
whichever way it was built. Every client session goes through
`default_client_conn`, including the state-guard read the activation fence
performs, so no client database is opened by this module on a writable session.

The activation fence is deliberately **not** included in that change. It keeps
its own factory, `default_fence_conn`, because its contract is locking — a
transaction holding `pg_advisory_xact_lock` and `LOCK TABLE … IN SHARE MODE`
across the caller's pointer swap — and not catalog inspection.
`schema_transition_lock` uses the same factory. Schema inspection is read-only
at the database; the fence session is unchanged.

The rollout suite runs on synthetic loopback configuration only. The onboarding
and migration-runner scripts load `.env` from their `main()` rather than at
import, so importing them has no environment side effect; the subprocess is
given a minimal synthetic environment plus `LOG_PLATFORM_NO_DOTENV=1`; and the
suite asserts, in-process and in a child, that the repository `.env` is never
opened — while separately proving an operator invocation still resolves its
`.env` exactly as before.

Suites: `test_migration_049_physical_preflight_postgres.py`,
`test_migration_049_rollout_paths_postgres.py`,
`test_release_preflight_readonly_defaults_postgres.py` (44 checks: the real
default factories against a disposable instance — read-only sessions, seven
runtime-assembled write and DDL forms refused with SQLSTATE 25006 on both the
platform and a client session, `verify_schema_prerequisites` driven with no
injected factory at all, a mutating preflight step refused on each leg, and the
fence session asserted separate and unchanged),
`test_release_schema_requirements_parser.py` (no database; now also the
alternative-group identity rule and the unreadable/malformed source split) and
`test_driver_eco_dashboard_delivery_schema_postgres.py` (9 checks), on a
task-owned disposable PostgreSQL 16. Regression unchanged: release schema
preflight, activation fence, the M-LAG bridge/race/closure/rollback suites, the
release boundary and runtime-isolation suites, and the host publisher lifecycle,
remediation and e-mail contract suites.

#### 11.15.6 Status (HISTORICAL — as of milestone 6c; superseded by §11.16.0)

**Superseded.** Migrations 049 and 050 have since been applied fleet-wide; read
§11.16.0 for the current fleet state. The paragraph below is the milestone-6c
record and is kept unrewritten.

Migration 049 is applied to **no** persistent, staging or production database.
Fleet-wide application remains an operational prerequisite of activation and a
separate, separately authorized operation; it was not performed here.
`SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY: NO`, unchanged and not addressed. No
live provider adapter exists, and §11.12's release prerequisites are still open.

### 11.16 Migration 050 — the reviewed contract arrives forward, never as an edit

The persistent fleet rollout of Migration 050 is **COMPLETED**. §11.16.0 states the
current production/fleet state. §11.16.1–§11.16.6 remain the design record of why
050 exists and what it does. §11.16.7 and §11.16.8 are retained as the
**historical rollout runbook and recovery envelope**, which still govern future
environments, disaster recovery and newly restored historical databases — not the
five current clients.

#### 11.16.0 CURRENT PRODUCTION/FLEET STATE (rollout COMPLETED)

`MIGRATION_050_APPLIED_FLEET_WIDE`, at release identity
`4f3f829bc0e4dbb9c5ce29ca874fcb1618e6b155` (branch `main`, `HEAD == origin/main`,
worktree clean before and after).

All five current client business databases — `alpha_main` (ALPHA00001),
`telematics_main` (BRAVO00016), `foxtrot_main` (FOXTROT00001), `delta_main`
(DELTA00001), `echogallery_main` (ECHO00001) — now satisfy the reviewed Driver
Eco Dashboard delivery contract:

* migration 049 remains recorded, and 050 is recorded **exactly once** on each;
* 44 columns and 35 constraints on `eco_dashboard_delivery_operation`;
* `external_mailer` is `TEXT`, nullable, with no default;
* the reviewed external-mailer ownership constraints are present;
* `EXTERNAL_MAILER_HANDOFF` is admitted by the required state constraints;
* the guard function carries the reviewed body digest `b219334ae212…`, and its
  trigger is bound and enabled;
* `eco_dashboard_delivery_operation` row count remained **0** throughout;
* the fleet-wide release schema prerequisite verification **PASSED**.

No unrelated migration was applied: the rollout named 050 explicitly, per client,
so the unrelated pending files listed in §11.16.7 remain pending and untouched.

**Future clients need no manual 050 step.** `scripts/onboard_workflow_a_client.py`
applies and records the current client-business chain including 049 → 050, so a
freshly onboarded client reaches the same reviewed physical schema as the five
current clients. Verified on disposable PostgreSQL by comparing the historical
049 → 050 path against the fresh standard onboarding chain: the relevant catalogs
were physically identical (§11.16.6 is the deterministic form of that check).

**Cloudflare and runtime activation remain separate and NOT yet done.** An R2
bucket, a D1 database and a temporary `workers.dev` Worker endpoint now exist
and `wrangler.toml` carries their real binding identifiers, but that endpoint is
a technical edge only (§12.12): no custom domain, DNS, WAF or cron trigger is
provisioned, `ECO_DASHBOARD_BASE_URL` / `ECO_DASHBOARD_PUBLISHER_URL` and the
machine credential are unset on the host, and the Eco schedules remain disabled.
Migration 050 closed the **schema** prerequisite only; the dashboard is not
activated and **no dashboard mailing rollout has occurred** (§12.11).

#### 11.16.1 What the fleet physically contained before 050 (historical)

`049_eco_dashboard_delivery_operation.sql` was applied to every enabled client
business database on **2026-08-19 between 19:55:44.44 and 19:55:44.67 CEST** —
`telematics_main`, `alpha_main`, `foxtrot_main`, `delta_main`, `echogallery_main`.
What it installed there was the **pre-review** form of the file: 43 columns, 32
constraints, no `external_mailer`, no `EXTERNAL_MAILER_HANDOFF`, and guard body
digest `3e83442051120379…`. `eco_dashboard_delivery_operation` held **0 rows**
on all five (verified read-only), and still does.

The approved runtime names `external_mailer` in its explicit projection, so
`DeliveryLedger.load()` failed against that schema with `UndefinedColumn`. That
is what 050 corrected, and 050 is now applied on all five (§11.16.0).

#### 11.16.2 Why 049 was restored rather than kept as edited

`scripts/apply_client_business_migrations.py` records and skips by **filename**.
An edited 049 is a file that will never execute again on any database that
already ran it, so editing it produces exactly one outcome: fresh installs carry
the reviewed contract, the five migrated clients silently do not, and no
migration exists that can reconcile them. `AGENTS.md` §4 and `CONVENTIONS.md` §12
say the same thing as a rule; this is the mechanism behind it.

049 is therefore restored byte-for-byte to its applied definition — SHA-256
`110322e319240dfd99c99317594e7975c4c8fbffb2d9d524b1a75b3f448ccf133`, pinned as a
constant in `ops/tests_manual/test_migration_050_external_mailer_rollout_postgres.py`
so any future edit fails a test instead of a fleet.

#### 11.16.3 What 050 does

`050_eco_dashboard_external_mailer_ownership.sql` is a pure forward delta:

* `external_mailer TEXT` (nullable, no default) — send-accounting ownership,
  bound at INSERT;
* `chk_..._external_mailer` (non-blank), `chk_..._external_mailer_unbound` (an
  owned row can carry no provider submission identity or lifecycle field in any
  state) and `chk_..._handoff_owned` (the handoff state is the consequence of
  ownership, never its source);
* the three 049 CHECKs that must admit `EXTERNAL_MAILER_HANDOFF` — `chk_..._state`,
  `chk_..._bearer_present`, `chk_..._provider_unbound` — dropped and recreated
  with their 049 text plus that state, and nothing else changed;
* `CREATE OR REPLACE` of `eco_dashboard_delivery_operation_guard()` onto the
  reviewed body (`b219334ae2128b86…`), which refuses any change to
  `external_mailer` in **both** directions. Replacing preserves the function OID,
  so the trigger 049 created stays bound and the relation is never unguarded;
* the ownership comment on the new column.

It creates no relation, drops no data, rewrites no row and changes no grant (049
granted on the TABLE, and a table-level grant covers a column added later).

#### 11.16.4 Safety with rows, and atomicity

Every pre-050 row has `external_mailer IS NULL` and a state the 049 vocabulary
allowed, so all three new CHECKs are vacuously satisfied and the three replaced
ones classify every existing row exactly as before. Each is added **VALIDATED**,
so PostgreSQL verifies the existing rows itself and a contradicting row aborts
the migration rather than being coerced. Zero rows is therefore a convenience,
not the reason the design is safe.

The runner executes the file in one transaction; the first `ALTER TABLE` takes
ACCESS EXCLUSIVE and holds it to commit, so the interval in which a replaced
CHECK is dropped and not yet re-added is unobservable, and a failure at any
statement leaves the applied 049 contract untouched. `lock_timeout = '5s'` makes
that lock fail fast rather than queue.

#### 11.16.5 What the release gate now decides

`db/schema_requirements.json` keys the Driver Eco Dashboard requirement on
**050**, because 050 is the migration whose presence makes the declared contract
true. The three classifications are therefore:

| database state | preflight |
|---|---|
| historical 049 only | `RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING` (050 unrecorded) |
| 050 recorded, schema uncorrected | `RELEASE_SCHEMA_CLIENT_OBJECT_MISSING` |
| 049 + 050, or a fresh chain through 050 | pass |

Nothing was weakened to reach that: the declaration is still the complete
44-column projection with exact default states, all 35 constraints by canonical
`pg_get_constraintdef`, the trigger by its complete binding and the guard
function by body digest.

#### 11.16.6 Both rollout paths converge

`ops/tests_manual/test_migration_050_external_mailer_rollout_postgres.py` builds
the historical state from onboarding's own declarations, upgrades it through a
real `scripts/apply_client_business_migrations.py --apply` run, builds a fresh
client through the full chain, and compares the two catalogs — columns,
constraints, indexes, triggers, function digest and column comments — for
physical identity rather than for two independent green results.

#### 11.16.7 The rollout matrix, and why a bare `--apply` is the wrong command

**HISTORICAL/FUTURE ROLLOUT PROCEDURE — executed and COMPLETED for the five
current clients (§11.16.0).** Nothing below is an outstanding action for them.
It is retained because the same procedure applies to a future environment, a
disaster-recovery rebuild, or a historical database restored at 049 or earlier.

Read-only inspection on 2026-08-19, *before* the rollout (no mutation, no
`--apply`) — the state a newly restored pre-050 database would also be in:

| client | database | 049 | 050 | rows | OTHER pending client-business files |
|---|---|---|---|---|---|
| Telematics `BRAVO00016` | `telematics_main` | applied 19:55:44.44 | pending → **applied** | 0 | 4 (`017`, `022`, `023`, `042`) |
| ALPHA `ALPHA00001` | `alpha_main` | applied 19:55:44.51 | pending → **applied** | 0 | 0 |
| FOXTROT `FOXTROT00001` | `foxtrot_main` | applied 19:55:44.56 | pending → **applied** | 0 | 9 (`038`–`046`) |
| DELTA `DELTA00001` | `delta_main` | applied 19:55:44.62 | pending → **applied** | 0 | 9 (`038`–`046`) |
| ECHO `ECHO00001` | `echogallery_main` | applied 19:55:44.67 | pending → **applied** | 0 | 9 (`038`–`046`) |

The `050` column records the rollout that has since completed. The final column
is unchanged: those unrelated files were **not** applied and remain pending.

**That last column is the whole rollout decision.** `scripts/apply_client_business_migrations.py`
applies EVERY pending file in the directory unless `--migration` narrows it, so a bare
`--apply` would additionally execute between four and nine unrelated migrations on four of the five
clients, in one unattended pass. The repository tooling *supports* fleet-wide execution; for this
change it must not be used that way.

The rollout is therefore **one client at a time, one named migration**, each step verified before
the next begins:

```
# 0. Confirm the candidate is the intended tree, and re-read the fleet.
python3 scripts/apply_client_business_migrations.py --migration 050_eco_dashboard_external_mailer_ownership.sql --list

# 1..5. Per client, in this order. Nothing else is named, so nothing else can run.
python3 scripts/apply_client_business_migrations.py \
  --client-name ALPHA \
  --migration 050_eco_dashboard_external_mailer_ownership.sql --apply
#   then repeat with --client-name FOXTROT, DELTA, ECHO, Telematics

# After EACH client, before moving on: 44 columns, 35 constraints,
# guard body b219334ae2128b86…, and the ledger row for 050.
psql "<client dsn>" -c "SELECT count(*) FROM information_schema.columns
   WHERE table_schema='public' AND table_name='eco_dashboard_delivery_operation'"
psql "<client dsn>" -c "SELECT count(*) FROM pg_constraint
   WHERE conrelid='public.eco_dashboard_delivery_operation'::regclass"
psql "<client dsn>" -c "SELECT encode(sha256(convert_to(prosrc,'UTF8')),'hex')
   FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='public' AND p.proname='eco_dashboard_delivery_operation_guard'"

# 6. Fleet-wide gate, read-only, only after all five are green.
python3 ops/manage_release.py activate --release <release-id>     # NO --execute
```

ALPHA first deliberately: it is the only client whose pending set is exactly `050`, so the first
execution is the one with nothing else in the directory that could behave unexpectedly.

The final step is `activate` **without** `--execute`: it runs the identical
`verify_schema_prerequisites` the execute path enforces, fleet-wide, and issues only SELECTs. A
non-zero exit there means at least one client is uncorrected and activation would be refused. For
the five current clients that gate has been run and **passed** (§11.16.0).

#### 11.16.8 Rollback and recovery envelope

**050 is not "instantly reversible", and the reason is the application, not the DDL.**

While the ledger is empty and no release carrying the dashboard code is active, a reversal is
mechanically available: drop the three new CHECKs, restore the three replaced ones to their 049 text,
`DROP COLUMN external_mailer`, `CREATE OR REPLACE` the guard back onto the 049 body, and delete the
`050_…` row from `public.schema_migrations`. There is no data to lose and no dependent object — no
view, index, foreign key or generated column references `external_mailer`.

**That stops being true the moment application code creates ledger rows.** After that,
`DROP COLUMN external_mailer` destroys the only durable record of WHO owns each delivery's send
accounting, and the provider lifecycle becomes entitled to adopt rows the Eco send log already
accounts for — a second dashboard e-mail to a driver. Reverting the state CHECK also makes any row in
`EXTERNAL_MAILER_HANDOFF` unrepresentable, so the `ALTER TABLE` would fail on validation and leave a
half-reverted schema behind. **Once the dashboard has written a single row, forward-fix, never roll
back.**

**A partial fleet rollout is safe, and is the recommended shape.** Each client business database is
independent; 050 touches one relation in one database and shares nothing across clients. A client
that has 050 and a client that has only 049 are both internally consistent.

**What a partial fleet cannot do is activate.** `db/schema_requirements.json` keys the requirement on
050 and `_verify_fleet` refuses on the FIRST client that has not recorded it — with no filter
parameter, no eligibility flag and an unreachable client counted as a refusal. So an incomplete
rollout cannot be activated past by accident; it can only be finished or left alone.

**If 050 succeeds on client N and fails on client N+1:** stop. Do not roll back client N — it is
correct and self-consistent, and reverting it only widens the inconsistency. The runner is
transactional per file, so client N+1 either has the whole migration or none of it; there is no
partial schema to repair. Diagnose N+1 (`lock_timeout` under a long-running reader is the expected
benign failure; re-run when it is idle), fix, re-run the identical single-client command, and only
then continue. Activation stays blocked for everyone until the fleet is whole, which is the correct
state — not an outage.

## 12. Integration with the existing Eco Driving mailings (milestone 7)

> **Current-state correction (see §12.11).** This section originally read "the
> dashboard link is now part of the existing Eco Driving weekly and monthly
> e-mails". That is no longer the contract and must not be read as one. Dashboard
> mailing is **opt-in and additionally gated per client**: the default path of
> all four mailing commands is legacy-only, and **no production client is
> enabled**. What §12 describes is the integration that runs *when both
> conditions hold*; §12.11 owns when they hold.

The integration exists inside the **existing** Eco Driving weekly and monthly
mailings. No fleet orchestrator was built, no parallel mailing pipeline exists,
and no second e-mail sender was introduced. Verified locally and synthetically
only. **No real e-mail was sent, no SMTP connection was opened, no Cloudflare
resource was created or mutated, nothing was deployed and no schedule was
enabled.**

### 12.1 Why there is no new orchestrator

The four existing jobs already are one:

| Job | Client family |
|---|---|
| `jobs/ecodriving/job_eco_driving_weekly_email_notifications.py` | ALPHA00001 |
| `jobs/ecodriving/job_eco_driving_monthly_email_notifications.py` | ALPHA00001 |
| `jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py` | BRAVO00016 |
| `jobs/ecodriving_person/job_eco_driving_person_monthly_email_notifications.py` | BRAVO00016 |

They own client selection, driver enumeration, driver identity, recipient
resolution, reporting-period selection, per-driver processing, failure
isolation, run accounting, rendering, send-log idempotency and the example.invalid SMTP
send. Every one of those stays exactly where it is. The integration adds the one
thing they did not have — a per-driver dashboard link — and nothing else.

```
existing weekly/monthly Eco job
  -> its already-selected candidate, client_id, driver identity, recipient
     and reporting period
  -> privacy-minimised snapshot from the SAME persisted Eco data
  -> publication + capability (publication-only seam)
  -> capability URL injected into the EXISTING Eco template
  -> existing reservation / send-log lifecycle
  -> existing example.invalid SMTP send
```

| Component | Path |
|---|---|
| the integration seam used by all four jobs | `jobs/ecodriving_dashboard/eco_mailing_integration.py` |
| publication-only lifecycle driver | `publisher.ensure_capability()` |
| terminal handoff transition | `DeliveryLedger.record_external_mailer_handoff()` |
| fleet-shaped snapshot build | `job_eco_dashboard_snapshot.build_delivery_snapshot_from_cursor()` |
| template insertion points | `{eco_dashboard_section_html}` and `{eco_dashboard_link_html}` in 24 of the 28 existing templates; the 4 below-threshold `Niezakwalifikowani` templates carry neither and show no dashboard |

### 12.2 The reporting period is an input, never a decision

`EcoDashboardLinkService` is constructed with the `period_start_date` /
`period_end_date` the job's own `select_period_for_send` already chose, and it
selects nothing. `period_identities_for()` looks up the canonical
month-bounded bucket that ends at that exact boundary — which is what supplies
`period_sequence_in_month`, `is_partial_period` and the comparison basis — and
then **verifies** the cumulative start against the one the job persisted. A
disagreement raises `DashboardPeriodMismatch`; nothing near-by is substituted.

The W1 = MTD-through-W1, W2 = W1+W2, W3 = W1+W2+W3, cut-at-month-end model is
read from `jobs/ecodriving/job_eco_driving_aggregate.py` and is not recomputed:
the comparison basis is the preceding **cumulative** snapshot, not an
incremental week, and W1 has no in-month predecessor. Monthly consumes the
closed calendar month verbatim and compares against the preceding closed month.
No dashboard-side period resolver is reachable from this path — the suite
asserts that `resolve_previous_completed_weekly_snapshot`,
`resolve_previous_completed_month` and any clock reference are absent from it.

### 12.3 Fleet-shaped database access

`build_delivery_snapshot()` opens its own connection, which is right for a
one-driver invocation and wrong for a fleet run: at ALPHA scale it would be one
new session per driver. The body moved to
`build_delivery_snapshot_from_cursor()`, which reads on a cursor the CALLER
owns, and the Eco jobs lend it their own already-open client-business
connection.

Sharing the job's connection makes one thing mandatory: **each driver's
snapshot read runs inside its own subtransaction**. PostgreSQL aborts the whole
transaction on any statement error, so catching one driver's database failure in
Python is not isolation — without a `SAVEPOINT` the next statement on that
connection fails with `25P02` (`current transaction is aborted`), which is the
failing driver's own send-log row and then every remaining driver's work. The
savepoint is taken and released around the read only; it writes nothing, rolls
nothing of the job's back, opens no second session, and `ROLLBACK TO SAVEPOINT`
is legal precisely in the aborted state it has to undo. A connection already in
autocommit skips it, having no surrounding transaction to poison.

The delivery ledger cannot share that connection — it requires autocommit,
because "durable before the remote call" is a statement about the call and not
about a commit the Eco job makes later — so **exactly one** additional
connection is opened lazily per run and reused for every driver. The
population-level rating-group distribution, which is identical for every driver
in a period, is fetched once per run rather than once per driver. The client
account configuration is resolved once by the job rather than once per driver.

No connection lifecycle is exposed to an operator, and no interactive shell,
IDE or maintained session is involved anywhere: manual invocation and scheduled
invocation are the same `run(client, run_id, params)` code path, with the
existing execution-contract safety modes as the operational brake.

### 12.4 The publication-only seam

`publisher.ensure_capability()` is the supported integration contract, and it is
deliberately not `advance_delivery(max_steps=1)` — that call stops after one
step as an implementation detail of a loop, would still record a *delivery
intent* as its one step, and returns no link.

```
publication preflight (credential + dashboard base URL; no provider)
  -> durable local operation (operation id, subject ref, payload digest,
     recipient binding) BEFORE any remote effect
  -> durable lease claim
  -> publish, or recover a lost bearer
  -> capability persisted
  -> EXTERNAL_MAILER_HANDOFF recorded
  -> capability URL returned to the caller
```

It has no provider-facing step at all: `_record_intent`, `_submit`,
`_reconcile` and `_mark_remote_delivered` are unreachable from it, and
`PublisherServices.provider` may legitimately be `None`. A delivery already in
any provider state is refused rather than progressed, so one logical delivery is
never owned by both lifecycles.

**Rerun behaviour.** Identical inputs converge. The operation id, subject
reference and provider-free identity are derived from the logical delivery
tuple; a rerun that changes the recipient or the canonical bytes is refused
(`RECIPIENT_CONFLICT` / `PAYLOAD_CONFLICT`) with nothing mutated; and a delivery
already in `EXTERNAL_MAILER_HANDOFF` returns the **same** link from the retained
bearer instead of minting or rotating a capability. A `force_resend` maps onto
the `normal` delivery scope precisely so that it is the same logical delivery
sent again, not a second dashboard for one driver and one period.

**Why the bearer is retained here.** Answering "what is this driver's link for
this period?" identically on every rerun is this state's entire contract, and a
row that had forgotten the bearer could only answer by rotating the capability.
It is the same reasoning that retains it in `PROVIDER_AMBIGUOUS`. Minimisation
is unchanged everywhere else: `FINALIZED` still destroys it, and the CHECK
constraints and the row guard still make a stored bearer in a diagnostic field
an unrepresentable row.

**Retention is bounded by the grant, not by the state.** A capability past
`capability_expires_at` is not "the same link" — it is a URL that answers `410`,
and handing it over would put a dead link in a real e-mail while recording a
handoff that delivered nothing. So an expired capability is never returned:

```
EXTERNAL_MAILER_HANDOFF with an expired (or near-expired) capability
  -> BEARER_RECOVERY_REQUIRED   (the expired bearer is DESTROYED in that
                                 same statement; bearer_cleared_at stamped)
  -> POST /api/publish/recover  (the EXISTING explicit rotation primitive:
                                 revoke predecessor + mint replacement in one
                                 Worker transaction, same operation id, same
                                 subject binding, same payload digest)
  -> CAPABILITY_PERSISTED
  -> EXTERNAL_MAILER_HANDOFF    (fresh capability, same logical delivery)
  -> the fresh link is returned
```

At most **one** such rotation per invocation; a replacement that is still not
usable is a refusal (`CAPABILITY_EXPIRED`, no link, that driver's e-mail
withheld), never a loop. A recovery the Worker refuses — the operation is
`DELIVERED` remotely, its object no longer verifies, the grant was superseded —
lands in `OPERATOR_REQUIRED` with no bearer retained. Nothing on this path binds
a provider, sends anything, or claims an SMTP outcome: a rotation obtains a
fresh *link*, and whether the driver was mailed remains a fact
`eco_*_email_send_log` owns. A usable margin
(`PublisherConfig.capability_min_remaining_seconds`, 1 h) is required rather
than bare non-expiry, because a link has to survive composition, sending and
being opened.

Normal expiry is therefore handled **autonomously** by the weekly/monthly runs
rather than parked on an operator. This needed no schema change: the transition
graph is host-side (`delivery_contract.LEGAL_TRANSITIONS`), and migration 049
already requires `BEARER_RECOVERY_REQUIRED` to hold no bearer at all.

**The boundary the rotation must not weaken, and where ownership is bound.**
`BEARER_RECOVERY_REQUIRED` is a state whose documented next action belongs to
the *provider* lifecycle, so an invocation that died mid-rotation would leave
behind exactly the row `advance_delivery()` is entitled to pick up — and turn
into a second, dashboard-specific message to a driver the Eco jobs already
mailed.

Ownership is therefore **bound in the INSERT that creates the operation**, not
annotated when the handoff is recorded. `ensure_capability()` passes its
`mailer` to `prepare_delivery()` -> `DeliveryLedger.ensure_operation()`, which
writes `eco_dashboard_delivery_operation.external_mailer` in the first durable
statement about the delivery. Recording ownership later would leave a real
window — `PREPARED` and `CAPABILITY_PERSISTED`, before `_handoff()` commits — in
which an Eco-created row exists without saying who owns its send, and the
provider path could claim its lease, count an attempt, bind an idempotency key
and submit. There is no such window if the first committed row already answers
the question.

The column is enforced by migration 049 rather than by convention:

* `chk_..._external_mailer_unbound` — an externally-owned row may hold **no**
  provider identity, message id, submission/acceptance timestamp, remote
  delivery timestamp or provider attempt, in any state. Combined with
  `chk_..._provider_key_present`, every provider-driven state is physically
  unreachable for it;
* `chk_..._handoff_owned` — `EXTERNAL_MAILER_HANDOFF` cannot be reached without
  ownership. The state is the consequence of ownership, never its source;
* the guard trigger refuses **any** change to `external_mailer`, including
  `NULL -> value` and `value -> NULL`
  (`ECO_DASHBOARD_DELIVERY_OWNERSHIP_IMMUTABLE`). Ownership cannot be acquired,
  lost or edited after creation.

`external_mailer_owns()` reads that column first, so `advance_delivery()`
refuses in every state and **before it claims a lease** — nothing is mutated:
`EXTERNAL_MAILER_OWNS_DELIVERY`. A caller that presents different ownership for
an existing row is refused by `ensure_operation()` with
`EXTERNAL_OWNERSHIP_CONFLICT` before any write, in both directions. One logical
delivery is never owned by both lifecycles, and an ordinary provider-owned
dashboard delivery (`external_mailer IS NULL`) is unaffected and still runs its
own lifecycle.

### 12.5 The e-mail, and what the ledger does not claim

The link is injected through the existing template architecture: **two**
placeholders in 24 of the 28 existing templates, rendered by one shared helper
rather than four implementations, and always present in the context so a
deployment without the dashboard renders exactly as it does today.

The remaining 4 — the `Niezakwalifikowani` below-threshold variant, one per
period type per client family — carry **neither** placeholder. A recipient whose
period distance did not reach the qualifying distance is offered no dashboard,
so that message has no insertion point, requests no link, and causes no
publication and no capability. They are named by
`eco_mailing_integration.TEMPLATES_WITHOUT_DASHBOARD`, and the run-level
preflight holds them to the OPPOSITE rule via
`assert_no_dashboard_placeholders()`: a below-threshold template that grows a
CTA back is refused just as a dashboard template that loses one is. This is a
statement about those template files only — it is not a second eligibility rule,
and the existing template-selection logic is unchanged.

| placeholder | position | content |
|---|---|---|
| `{eco_dashboard_section_html}` | directly before the `Podsumowanie` section | the `Twój Panel EcoDriving` card: title, description, CTA |
| `{eco_dashboard_link_html}` | after the last recommendation/action section — `Obszar do poprawy` where the variant has one, `Rekomendacja` otherwise — and before the legacy programme CTA and the footer | the same CTA on its own, no title, no description |

Both carry the CTA `Zobacz szczegóły swojej jazdy` in the **existing ALPHA
programme button's** treatment: an MSO conditional `v:roundrect` for Outlook
desktop and a downlevel-revealed `<a>` pill for everything else, `#1F8A4C`,
white 16px bold, `padding:15px 28px`, `border-radius:999px`, 48px VML height and
`arcsize="50%"`. Only the VML width differs — 360px rather than 260px — because
Outlook renders that box at a fixed width and the longer label measures ~233px
against the programme label's ~144px. The existing `Dowiedz się więcej`
programme CTA is unchanged and still sits where it did.

**Two positions, ONE capability.** `dashboard_link_context()` takes a single URL
and builds both fragments from it, so the duplication is presentation only:
there is still exactly one publication, one capability and one delivery
operation per logical delivery, and no second URL, rotation, redirect or
tracking wrapper can be introduced by showing the link twice.

Both fragments are whole `<tr>` elements, so the disabled case substitutes the
empty string and leaves no row, gap or placeholder residue.

* the URL is attribute-escaped before it enters `href`;
* no click-tracking, redirector, URL rewriting or image beacon is introduced;
* the fail-closed unresolved-placeholder guard is untouched, and a template that
  **lost** the placeholder is caught by checking the rendered output — a link
  produced but not landed is a technical failure, not a quiet omission. A
  template that carries only ONE of the two is still a dashboard template and is
  refused; only a template carrying neither is dashboard-free, and only the
  declared below-threshold files may be that.

**Placement is validated as a position, before anything is published.**
Substring presence is not enough: `<!-- {eco_dashboard_link_html} -->`
substitutes cleanly, satisfies both a template scan and a rendered-output scan,
and reaches the driver inside a comment — every remote effect fired and the link
is invisible. `assert_link_placeholder_placement()` therefore classifies each
occurrence with a linear scan (text content, HTML comment, inside a tag,
`<script>`/`<style>`, an unterminated region) and accepts **exactly one
occurrence in text content, per placeholder**. A comment-only placement, an
Outlook conditional comment, an attribute or tag position, a raw-text element,
an unterminated region, a duplicate, an absent or brace-malformed placeholder
are all refused — and so is a template that carries only one of the two
placeholders.

It runs in two places, both ahead of any effect: each job validates its whole
template inventory once, next to the existing inventory check and before the
first candidate; and `render_with_dashboard_link()` validates the template it is
about to render **before** requesting the link, so a bad template costs no
snapshot upload, no capability and no ledger row
(`DASHBOARD_LINK_PLACEHOLDER_INVALID`). The post-render presence check is kept:
the two answer different questions — position, then presence — and neither
replaces the other.

`eco_*_email_send_log` remains the **sole** authority for e-mail accounting.
The dashboard ledger owns the snapshot/publication/capability lifecycle and
stops at the handoff: it records no acceptance, binds no provider identity and
never marks the remote operation `DELIVERED`. The existing example.invalid SMTP sender
identities and namespaces are unchanged, and
`SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY: NO` is unchanged — no automatic retry
was added around any ambiguous SMTP outcome.

### 12.6 Product state vs technical failure

| Case | Result |
|---|---|
| `INSUFFICIENT_DISTANCE` (below the period-level 100 km gate) | a legitimate dashboard: published, linked, e-mail sent |
| `REPORT_NOT_READY` | a legitimate dashboard: published, linked, e-mail sent |
| snapshot construction exception (including a database error, which is contained by the per-driver savepoint), integrity/publication refusal, capability persistence failure, preflight failure, lease not owned, base URL unusable, template placeholder in an unrenderable position, template lost the placeholder, an expired capability that could not be rotated | **no link and no e-mail for that driver** |

A technical failure records a bounded, non-secret code through the job's own
run accounting (`status='failed'`, `dashboard_link_blocked_count`, a `failed`
send-log row with `send_scope='skipped'`) and processing continues with the next
driver unless the existing `fail_fast` semantics say otherwise. A technical
dashboard failure is never converted into a successful-looking Eco e-mail.

### 12.7 Secrets

The capability leaves `EcoDashboardLinkService` only as the template context
value. `DashboardLinkOutcome.audit()`, `CapabilityResult.summary()`, the run
summary, the job logs, the send-log metadata and every failure code are counts,
states and bounded codes; the suite asserts no `#k=` fragment reaches any of
them. `capability_url_present` is how a summary says a link exists.

### 12.8 Verification

`python3 ops/tests_manual/test_eco_mailing_dashboard_link_integration.py`

* **Tier A — 23 checks, no infrastructure**: the authoritative period is
  consumed and a period outside the Eco model is refused rather than
  substituted; W1/W2/W3 cumulative semantics are read, not recomputed; the link
  is escaped and carries no tracking; the 24 dashboard templates carry the
  insertion point between table rows and the 4 below-threshold templates carry
  none, request no link and render no dashboard while the eligible ones still
  do; per-driver link binding (A's capability appears only
  in A's message); each job binds its own family identity column, mailer name,
  report type and already-selected period; a render-only run opens no ledger
  connection and publishes nothing while still proving the link shape; a
  below-threshold dashboard still produces a link; each
  technical failure class withholds only that driver's message; a template that
  lost the placeholder blocks the send; an unconfigured deployment renders
  exactly as before; the four jobs reach no dashboard `SmtpEmailProvider`,
  `email_provider`, `advance_delivery` or `dashboard_email`, asserted over the
  module AST rather than its prose; the reserve → send → mark order is intact
  with the dashboard step strictly before it and no retry loop around SMTP; no
  capability on any printable surface; `force_resend` is one logical delivery;
  the integration is off unless configured and a loopback-lookalike publisher
  endpoint is refused; no schedule and no second driver-enumeration loop; the
  insertion point is validated as a POSITION (all 24 dashboard templates
  accepted, the 4 below-threshold templates held to the opposite rule;
  comment-only, conditional-comment, attribute, tag, `<script>`/`<style>`,
  unterminated, duplicate, absent and brace-malformed placements refused) and
  the refusal is proved to happen before the link is requested, in the code
  order and in every job's start-up inventory check.
* **Tier B — 9 checks against a disposable PostgreSQL 16 prepared through
  migrations 049 → 050, exercising the REAL `DeliveryLedger`** (fake publisher
  transport; the Worker's own semantics are proved elsewhere): a fleet of 8 drivers opens **one**
  additional connection and causes exactly one publication per driver; the
  builder receives the job's period and the preceding cumulative period; a rerun
  returns the same link, publishes nothing new and leaves one row per logical
  delivery in `EXTERNAL_MAILER_HANDOFF` with the bearer retained, no provider
  identity bound and the mailer named; a publication failure and a snapshot
  exception each isolate to one driver (the snapshot failure creating no ledger
  row at all); a recipient change is refused, not redirected; a **real** failed
  statement on the lent connection leaves the shared transaction immediately
  usable — the failing driver's own send-log write and the next driver both
  succeed, earlier uncommitted work survives, and no connection is opened per
  candidate; an **expired** capability is rotated through
  `/api/publish/recover` rather than handed over — the returned URL carries a
  live bearer, the operation id, subject ref, payload digest, recipient binding
  and mailer are unchanged, `bearer_generation` advances by one, the expired
  bearer exists nowhere in the table, and an unrecoverable expiry escalates to
  `OPERATOR_REQUIRED` with the bearer destroyed and no SMTP claim made; and
  `advance_delivery()` refuses a handed-over delivery both in
  `EXTERNAL_MAILER_HANDOFF` and in the mid-rotation
  `BEARER_RECOVERY_REQUIRED` row a crashed rotation leaves behind — no
  provider identity bound, no lease even claimed, zero steps.
* **Tier C — 3 checks with the REAL snapshot builder over REAL Eco tables** on
  the same disposable instance: one qualifying cumulative weekly period is
  seeded into `eco_trip_assignments` / `eco_driver_weekly_stats`,
  `build_delivery_snapshot_from_cursor()` is run, wall-clock time is allowed to
  advance past a second boundary, and the rebuild produces **byte-identical**
  canonical output and an identical SHA-256; the document's `generated_at_utc`
  equals the stats row's `updated_at`; changed trip data and a changed
  `updated_at` each move the digest; and at the ledger the unchanged rerun
  reuses the same operation while the changed bytes are refused as
  `PAYLOAD_CONFLICT`.

**Status — `DASHBOARD_OPT_IN_LEDGER_BACKED_VERIFICATION_COMPLETE`.** Tiers B and
C are reported as SKIPPED, never as a pass, when no DSN is exported — and they
are **no longer skipped**. Run with a DSN against candidate
`552f4a38377fdc9059f2b4d0482b1df24b3401f7`, the file reports **33 checks passed
and no SKIPPED line** (21 + 9 + 3). The offline half and the ledger-backed half
are both closed and no regression was found; nothing in §12 depends on evidence
that has not actually executed.

**The disposable fixture contract is 050, not 049.** Tier B prepares its
instance by applying `049_eco_dashboard_delivery_operation.sql` and then
`050_eco_dashboard_external_mailer_ownership.sql`, so the physical form these
tests exercise is the reviewed final one — **44 columns, 35 constraints,
`external_mailer` present, four constraints admitting `EXTERNAL_MAILER_HANDOFF`,
a guard function referencing `external_mailer`, and one non-internal trigger** —
the same contract §11.16.0 records fleet-wide. The historical applied 049 form
is immutable and distinct (**43 columns, 32 constraints, no `external_mailer`**)
and is **not** the fixture contract for ledger-backed Driver Eco Dashboard
tests; §11.16.2 is why 049 was restored byte-for-byte rather than edited. The
run used two disposable loopback-only PostgreSQL 16.11 instances provisioned by
`ops/tests_manual/disposable_postgres.py`, both removed afterwards: no
persistent, client or production database was read or written, and no DSN came
from the host environment, `/etc` or client database configuration.

Regression, all green on the same disposable instance:
`test_driver_eco_dashboard_email_contract.py` (15),
`test_driver_eco_dashboard_publisher_lifecycle.py` (17),
`test_driver_eco_dashboard_host_remediation.py` (37),
`test_driver_eco_dashboard_delivery_schema_postgres.py` (9),
`test_migration_049_physical_preflight_postgres.py`,
`test_driver_eco_dashboard_prepublisher.py`,
`test_driver_eco_dashboard_byte_integrity.py`,
`test_eco_dashboard_snapshot.py`,
`test_eco_driving_weekly_email_notifications.py`,
`test_eco_driving_monthly_email_notifications.py`,
`test_eco_person_weekly_email_delivery.py`,
`test_eco_driving_arbitrary_week_selection.py` (458),
`test_eco_driving_presentation_s12.py` (34),
`test_eco_driving_runner_integration.py`, `test_eco_driving_scoring.py`.

`test_driver_eco_dashboard_host_remediation.py` (37 checks — the
loopback-lookalike credential boundary, backend/idempotency-key immutability,
the expired/stale lease boundaries under genuinely concurrent connections, and
a pre-submit provider failure never becoming ambiguous) was re-run **after** the
explicit product User-Agent and the `PUBLISHER_EDGE_FORBIDDEN` classification of
§12.12 and stayed green, so the transport change is covered rather than assumed.

### 12.9 Remaining prerequisites

Aligned with §11.12 and §11.16.0 (§11.15.6 is the superseded milestone-6c
record), and none of them is addressed here:

* **the client-business schema prerequisite is CLOSED.** Migrations 049 and
  050 are applied on all five enabled client business databases (§11.16.0), so
  the reviewed contract — `EXTERNAL_MAILER_HANDOFF`, the `external_mailer`
  ownership column with its CHECKs and its guard-trigger immutability rule — is
  physically present fleet-wide. `db/schema_requirements.json` keys the
  requirement on **050** and the physical-preflight suite proves declaration and
  schema agree. Schema is no longer what blocks activation;
* **Cloudflare** — DNS, custom domain, assets, rate limits, WAF and cron
  triggers remain unprovisioned; a temporary `workers.dev` technical endpoint
  exists (§12.12) and is **not** a mailing rollout. `ECO_DASHBOARD_BASE_URL`,
  `ECO_DASHBOARD_PUBLISHER_URL` and the machine credential are unset on the host;
* **rollout permission** — the integration is off for every production client
  and, independently, off for every invocation that does not pass
  `--with-dashboard`. See §12.11. Configuration alone no longer enables it;
* **schedule activation** remains a separate, separately authorized decision.
  All Eco schedules are still disabled;
* the external e-mail provider question is **closed**: example.invalid SMTP through the
  existing Eco mailing lifecycle is authoritative, and the dashboard-specific
  `SmtpEmailProvider` is not on this path.

### 12.10 Ambiguous SMTP results, and why they never rotate a capability

example.invalid SMTP has no idempotency key, so a submission whose result is ambiguous —
attempted, with remote acceptance impossible to exclude — must never be resent
by an automatic run. The Eco send log owns that fact (`docs/05_jobs.md` §5.3.1,
`docs/07_operations.md`): the reservation stays `pending` and is marked
`metadata_json.smtp_submission_result='AMBIGUOUS'`, which `reserve_send()`
refuses under both `normal` and `forced` scopes until an operator reconciles it
through `ops/reconcile_eco_email_ambiguous_send.py`.

What this section owns is the **interaction** with the dashboard:

* each mailer establishes eligibility **before** `render_with_dashboard_link()`,
  so a blocked message causes no publication and — the part that matters — no
  capability rotation. A rotation is a real remote effect, and an automatic run
  that is not allowed to send must not cause one;
* the dashboard ledger asserts nothing about SMTP either way. After a handoff
  followed by an ambiguous submission the row is still
  `EXTERNAL_MAILER_HANDOFF` with no provider identity, no acceptance, no remote
  delivery and no operator flag of its own: whether a driver was mailed is a
  fact `eco_*_email_send_log` owns, and it currently answers "unknown, pending a
  human";
* a later **operator-authorized** resend goes through the ordinary path, so if
  the capability has expired by then the existing expiry rotation (§12.4)
  produces a fresh link under the unchanged logical delivery identity. That
  stays true after the retirement sweep has moved the row to
  `CAPABILITY_RETIRED` (§13.4): the retired state's one non-terminal successor
  is `BEARER_RECOVERY_REQUIRED`, so the resend takes exactly the same rotation,
  and the sweep never touches a `PROVIDER_AMBIGUOUS` row at all;
* a technical dashboard failure still withholds a fresh eligible e-mail. The
  ordering change only prevents dashboard work for a message the send ledger has
  already ruled out.

### 12.11 Opt-in rollout contract (milestone 7a)

**The default is legacy.** All four existing Eco Driving mailing commands run
their pre-dashboard path unless the invocation explicitly asks otherwise.
"Legacy" here is a behavioural statement, not a cosmetic one. Without
`--with-dashboard` there is:

* no dashboard snapshot build caused by the mailing integration;
* no publication request, no R2 write, no D1 capability or publication state;
* no capability generation and no capability recovery;
* no dashboard link in the message;
* **no dependency on publisher configuration or availability whatsoever** —
  `DashboardLinkSettings.from_params` returns before it reads
  `ECO_DASHBOARD_BASE_URL` / `ECO_DASHBOARD_PUBLISHER_URL` /
  `ECO_DASHBOARD_PUBLISHER_TOKEN`, so a missing, broken or unreachable publisher
  cannot change whether the ordinary e-mail is sent;
* no change to template selection or to any existing mailing behaviour.

**What changed, and why.** The seam previously enabled itself whenever the
deployment happened to be fully configured. That made provisioning a publisher
URL a silent mailing rollout. Configuration and permission are now separate
decisions with separate owners.

**Two conditions, neither sufficient alone.**

```
dashboard-enabled sending
  = explicit --with-dashboard on the invocation
  AND explicit client-level rollout permission
```

| Condition | Where it lives | Refusal |
|---|---|---|
| the opt-in | `ops/runner.py --with-dashboard` -> `params["with_dashboard"]` -> `DashboardLinkSettings.from_params` | absent -> legacy path, silently and by design |
| client rollout permission | `ops/eco_dashboard_mailing_rollout.json` -> `jobs/ecodriving_dashboard/dashboard_rollout.py` -> `eco_mailing_integration.authorize_dashboard_mailing()` | not enabled -> `ECO_DASHBOARD_MAILING_ROLLOUT_NOT_ENABLED`, the run stops |

The permission is deliberately **not** derived from the publisher being
configured, and the two are checked in different places so neither can imply the
other.

**The command surface.** One boolean option on the existing entry point; no
second mailing command and no parallel orchestrator:

```bash
# legacy — unchanged, and what every undeclared schedule does
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \
  jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<CLIENT_UUID>","execution_mode":"normal_send"}'

# dashboard-enabled — permitted only where the rollout declaration enables it
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \
  jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<CLIENT_UUID>","execution_mode":"normal_send"}' --with-dashboard
```

The option is accepted only on the four mailing modules; anywhere else it is a
usage error, as is a mistyped variant, so a typo cannot become a silent legacy
run that looks like it did what was asked.

**Schedulers acquire it only through a reviewed declaration.** The Workflow A
dispatcher builds `[python, ops/runner.py, <job_module>, <params_json>]`
(`jobs/api/telematics/dispatcher.py::_launch_job`) and appends an option only when
`jobs/ecodriving/scheduled_mailing_contract.py` resolved one — which requires
both a declared (client, dataset) entry in
`ops/eco_mailing_production_schedule.json` and rollout permission for that client
in `ops/eco_dashboard_mailing_rollout.json`. The dispatcher spells no option
itself and refuses any outside the declared allowlist. Every other fire, and
every non-mailing dataset, still carries no option at all. No unit, timer or
shell script mentions the flag. All Eco schedule rows remain disabled
regardless. The production contract, the retry matrix and the operator controls
are in `docs/07_operations.md`.

**The rollout declaration.** `ops/eco_dashboard_mailing_rollout.json`, read by
`jobs/ecodriving_dashboard/dashboard_rollout.py`, in the same repository-JSON
convention as `db/schema_requirements.json` and `ops/watchdog_expectations.json`.
It is consulted **only** when a run has already opted in, so a legacy run does
not require it to exist.

| Client | State | Meaning |
|---|---|---|
| `ALPHA00001` | `enabled: false` | Owner decision: **dashboard mailing disabled**. No ALPHA00001 Eco e-mail may include or publish a dashboard at this stage. Reversible by a future explicit owner decision; it is not encoded as impossible. |
| `BRAVO00016` | `enabled: false` | Dashboard **capable**, production rollout **not yet authorized**. Enabling is a separate authorized task. |
| anything else | not declared | **Disabled.** New and unknown clients never inherit a rollout. |

Fail-closed parsing, with no permissive coercion: a wildcard entry (`*`, `ALL`,
`ANY`, `DEFAULT`, `%`) is **malformed, not "everyone"**, so "all clients enabled"
is not expressible and cannot become the default; a duplicated client, a
non-boolean `enabled`, an unknown key, a wrong contract identity or invalid JSON
are all `..._MALFORMED`; a source that cannot be read is `..._UNREADABLE`, kept
distinct so an operator knows whether to look at the filesystem or at the
declaration. Every one of those outcomes refuses. None produces "enabled".

Enabling BRAVO00016 later is one value change in that file — `false` -> `true` —
reviewable as a one-line diff, with **no change to any mailing business logic**.
No mailing module decides a rollout by client code.

**Where the refusal happens.** `authorize_dashboard_mailing()` is called by each
of the four jobs immediately after the client account is resolved and **before**
`EcoDashboardLinkService` is constructed, before the candidate loop, and
therefore before the first snapshot build, the first publication, the first
capability, the first send-log reservation and the first SMTP connection. It
does **not** fall back to a dashboard-less e-mail: a run that was explicitly
asked for dashboard mailing and may not do it stops.

**SMTP safety is untouched.** `SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY: NO`
stands. The Eco send log remains the sole mail-send authority; the dashboard
lifecycle storage remains publication/capability authority only; and no retry of
any kind was introduced around the integration.

### 12.12 Host transport — product User-Agent and edge-403 diagnosis

**The measured defect.** `HttpSecureDeliveryTransport` shipped `urllib`'s default
`User-Agent: Python-urllib/3.x`. Against the current Cloudflare `workers.dev`
edge that receives an **empty HTTP 403 before the Worker executes**, so normal
Lenovo-host dashboard publishing could not have worked at all as shipped. The
first synthetic public publication E2E succeeded only because its task-local
transport supplied an explicit User-Agent.

**The User-Agent contract.**

```
User-Agent: log-platform-eco-dashboard-publisher/1
```

`secure_delivery_client.PUBLISHER_USER_AGENT`, in the repository's established
`"<component-slug>/<n>"` contract-identity form — which is also exactly a valid
HTTP `product/version` token, so no version infrastructure was invented for it.
It is applied to **every** request this transport makes (`/api/publish`,
`/api/publish/recover`, `/api/publish/delivery`), set last so no caller-supplied
header can displace it, and it is a module constant: it therefore carries no host
identity, no client or driver identity and no credential, and does not vary per
request, per client, per credential, per endpoint or per run. Authentication
semantics are unchanged.

Measured against the live endpoint, unauthenticated and read-only, on a
nonexistent path:

| User-Agent | Result |
|---|---|
| `Python-urllib/3.12` (the old default) | HTTP **403**, body `error code: 1010` — Cloudflare edge, the Worker never ran |
| `log-platform-eco-dashboard-publisher/1` | HTTP **404**, body `{"error":"SNAPSHOT_UNAVAILABLE"}` — Worker-level response vocabulary |
| `curl/8.5.0` | HTTP **404**, same Worker vocabulary |

**The 403 classification.** A 403 used to fall through to
`SecureDeliveryError(error or "PROTOCOL_ERROR")`, and an edge refusal carries no
publisher body, so the measured Cloudflare failure was indistinguishable from a
publisher protocol defect. It now has its own outcome:

```
PUBLISHER_EDGE_FORBIDDEN — the request was forbidden at the edge/provider
                           boundary, before the publisher application saw it
```

`delivery/driver_eco_dashboard/worker/` answers 400, 401, 404, 409 and 503 and
never 403, so a 403 observed by the host is by construction a boundary refusal
and not a publisher application response.

**The safety invariant that governs it.** HTTP 403 is a *definite observed HTTP
response*. It is raised as a `SecureDeliveryError` and **never** as
`TransportOutcomeUnknown`, so it does not acquire the "the request may have
committed — ask the publisher, then retry" semantics that exist only for
transport ambiguity. It routes to `_operator(...)` like any other definite
refusal. Every other mapping is unchanged: 200/201 results, 409 conflicts, 404,
503 `OBJECT_UNREADABLE`, `>= 500` and transport-level exceptions all behave
exactly as before, and the transport still performs no retry of its own.

**Operator reading.** `PUBLISHER_EDGE_FORBIDDEN` means *something in front of the
publisher refused us* — a Cloudflare browser-integrity/WAF rule, a bot-management
decision, an access policy — and the next action is to look at the edge
configuration, not at the publisher, the ledger or the credential. It is not
`PROTOCOL_ERROR` (the publisher spoke something unmappable), not `NOT_FOUND` (the
Worker's deliberately ambiguous unknown-operation-or-unauthenticated answer) and
not an ambiguous outcome.

**`workers.dev` is a temporary technical endpoint.** Its existence is not a
mailing rollout, does not enable any client and does not authorize any send.

**Verification of §12.11 and §12.12.**
`ops/tests_manual/test_eco_dashboard_optin_rollout_and_transport.py` — **17/17
groups green, offline**: the explicit product User-Agent is sent on every
publisher request, HTTP 403 is a distinct **definite** outcome and never an
ambiguous one, `--with-dashboard` alone is insufficient, the production
declaration in `ops/eco_dashboard_mailing_rollout.json` enables **no** client,
and no scheduled or automatic invocation can carry the flag. The ledger-backed
half of the same candidate is closed in §12.8, and
`test_driver_eco_dashboard_host_remediation.py` (37 checks) was re-run after
these transport changes and stayed green.

### 12.13 A render-only rehearsal needs no delivery scope (milestone 7b)

**The defect.** All four Eco mailing jobs construct `EcoDashboardLinkService`
with `send_scope=execution.send_scope`, and for
`execution_mode=render_only` — the DEFAULT execution mode — that value is the
EXECUTION scope `"render_only"`, which names no delivery.
`EcoDashboardLinkService.__post_init__` normalised every scope through
`DELIVERY_SEND_SCOPE_BY_EXECUTION_SCOPE` eagerly, at construction, so
`--with-dashboard` + `execution_mode=render_only` raised

```
DeliveryContractError: no dashboard delivery scope for execution scope 'render_only'
```

before the candidate loop, for every client and all four mailing families. The
dashboard rehearsal path the opt-in exists to make safe was the one path that
could not run. The previous render-only coverage constructed the service with
`send_scope="normal"` and therefore never met the real caller value.

**The invariant, in both directions.** The execution scopes and the delivery
scopes are different vocabularies, and normalisation between them happens only
where publication is actually possible:

* a render-only run keeps its execution scope verbatim, and construction
  **requires** exactly `RENDER_ONLY_SEND_SCOPE` (`"render_only"`) when
  `settings.render_only` holds — so a rehearsal can never be constructed under
  `normal`/`test`/`forced` and never borrows a real delivery's publication
  identity (the previous test blind spot is now unwritable);
* `render_only -> normal` and `render_only -> test` were deliberately NOT
  added: either would bind the rehearsal to a real driver's publication
  identity. The mapping stays exactly three entries wide;
* `publication_send_scope()` is the ONLY producer of a delivery send scope,
  reachable only from the publishing path, and refuses under render-only;
* in `_link_for` the render-only return now precedes the `DeliveryIdentity`
  construction: a `DeliveryIdentity` is the publication's identity and a
  rehearsal never mints one. The real snapshot is still built from the real
  source data, no ledger connection is opened and nothing is published;
* a delivery run under `"render_only"` (the inverse defect) and any undeclared
  scope still refuse at construction with the original error; `normal`,
  `forced -> normal` and `test` map exactly as before, and the publication
  identity on the publishing path is unchanged.

An equivalent fix existed earlier on the local branch
`fix/dashboard-render-only-scope` (`7d13bc126478`) against a pre-050
architecture; the current fix re-established the invariant against current
`main` rather than porting that commit, and the coverage below supersedes its
historical test.

**Verification of §12.13.**
`ops/tests_manual/test_eco_dashboard_render_only_real_caller.py` — **7/7 groups
green, offline** — reads the real caller value from the real
`ExecutionContract` (never a literal invented by the suite), proves
construction + link production + rendering for all four mailing families under
that value, proves by AST that each job passes `execution.send_scope` into the
one shared construction, and proves every refusal direction above. On the
unfixed tree the suite fails with the exact production error. The render-only
group of `test_eco_mailing_dashboard_link_integration.py` now constructs with
`RENDER_ONLY_SEND_SCOPE` (the blind spot is closed); the full ledger-backed
suite (37 checks, Tiers A+B+C on disposable PostgreSQL 16) and
`test_eco_dashboard_optin_rollout_and_transport.py` (17/17) stayed green.

**Status of §12.13: DEPLOYED.** Committed and pushed as
`92c53ece27dab77c2f706fe1c3eef3d14700365f`
(`fix(eco-dashboard): support render-only real caller scope`), prepared and
activated as production release `92c53ece27da` on 2026-08-25 13:30 UTC
(15:30 Europe/Warsaw), and verified live read-only: the active sealed release
carries the fixed module byte-identical to the commit and passed the 7/7
read-only real-caller regression. No service restart was required (the delta
does not touch the API/export-worker import surface), no DB migration occurred,
and all Eco schedules remain disabled. With this deployment the historical
branch fix `fix/dashboard-render-only-scope` (`7d13bc126478`) has no remaining
correctness or preservation value; that retirement was executed under separate
authorization on 2026-08-25 — the five historical Eco branches, their five
scratchpad worktrees and the five stale release directories (including
`7d13bc126478`) are gone, and the release root holds exactly `current`
(`92c53ece27da`) and `previous` (`023e6452fd5b`).

## 13. Period-scoped capability lifetimes and expired-authorization retirement

Owner-approved, 2026-08-28. Two decisions, and one consequence that had to be
built rather than assumed.

### 13.1 The decisions

**A capability's lifetime is a property of its reporting period.** Weekly grants
live **10 days**, monthly grants **60**. The single universal 45-day lifetime is
gone.

**The capability model does not change.** A link stays **period-scoped and
snapshot-pinned**: one grant, one driver, one closed reporting period, one
immutable R2 object, forever. The alternative — one mutable rolling URL per
driver, so an old e-mail silently starts showing a newer report — was considered
and **rejected**.

### 13.2 Where the policy lives, and why the boundary carries a period rather than a number

`delivery/driver_eco_dashboard/worker/lib/capability_ttl.js` is the one
authoritative mapping. It has **no default**: `capabilityTtlSeconds()` answers
for `weekly` and `monthly` and throws for everything else, because a default is
something a caller can omit and every omission produces a lifetime nobody chose
for that period. The removed `DEFAULT_CAPABILITY_TTL_SECONDS`, and the
`params.ttl_seconds || DEFAULT` idiom that made an omission invisible, are both
gone; `rotateCapability`, `publishSnapshot` and `recoverLostBearer` all demand an
explicit, positive, whole-second lifetime and refuse before writing anything.

The host states the **fact**, the Worker owns the **policy**. `X-Publication-Period`
is a required singleton control header on `/api/publish` and
`/api/publish/recover`; a missing, empty, unknown, differently cased or
duplicated value is `400` with zero operations, zero grants and zero R2 objects
created. The host never computes a lifetime, so the two ends cannot drift apart
by disagreeing about a number they both hold.

The value comes from `eco_dashboard_delivery_operation.period_type`, which the
table constrains to `weekly`/`monthly`, which is part of the logical delivery
identity, and which is one of the fields hashed into the operation id
(`delivery_contract.derive_operation_id`). **An operation published as weekly
cannot be recovered as monthly without being a different operation id** — a
different publication, with its own snapshot and its own grant. That is why
recovery may restate the period on the request without the Worker needing to
store it: the header restates a fact the operation id already binds.

Nothing is ever derived from presentation text, a template, a subject line or a
URL — none of which carry the period, by design.

### 13.3 Overlapping historical links

Publishing a newer period **does not revoke the previous one**. Worked example,
proved end-to-end against the real Worker:

| | published | expires | shows |
|---|---|---|---|
| W1 | day 0 | day 10 | W1's snapshot |
| W2 | day 7 | day 17 | W2's snapshot |
| M1 | day 7 | day 67 | M1's snapshot |

On day 7 all three are live, all three are distinct grants, and each still
resolves to its own report. The overlap is intentional: a driver opening last
week's e-mail must see last week's report.

A **resend of the same period** converges on the same logical delivery. If its
capability is still usable — past the one-hour minimum-remaining margin — it is
reused unchanged, including under force-resend. If it has expired, the host
rotates **once** through the explicit recovery operation, under the unchanged
operation id, subject binding and payload digest, so the replacement link still
shows *that* period's snapshot. Recovery never retargets a delivery at a newer
report.

### 13.4 Expired authorization stops being live, secret-bearing and open

Period-scoped lifetimes make expiry the ordinary end of every link rather than a
rare corner, which exposed something the 049/050 state model could not say.

**The gap.** `EXTERNAL_MAILER_HANDOFF` retains the raw bearer deliberately, so a
rerun hands over the identical link — and the only thing that ever ended that
retention was a rerun. A period nobody re-mails kept a live bearer forever. It
also kept counting as open operational work (`idx_..._open` excluded only
`FINALIZED`), with nothing anybody could do about it.

**`CAPABILITY_RETIRED`** (migration `051_eco_dashboard_capability_retirement.sql`)
is the missing statement: *mailing ownership completed; the capability expired;
no operator action is required; the audit metadata is retained.* It is
deliberately not `FINALIZED`, which would assert a completed provider delivery
this ledger may never have made.

`DeliveryLedger.retire_expired_capabilities()` runs in the **ordinary lifecycle**
— `EcoDashboardLinkService.close()`, which all four Eco mailing jobs call from a
`finally`, after every per-driver decision, with every failure contained so
housekeeping can never cost a driver an e-mail. With weekly grants living 10 days
and the weekly job running weekly, an expired delivery is reached within days.

**It does not depend on the run publishing anything, and that is the point.**
The first version of this sweep ran only when a ledger connection already
existed — which happens only when some candidate reaches the publisher — so a
run whose drivers were all already sent, whose roster was empty, whose snapshots
were all refused, or that was simply filtered down to nothing swept nothing at
all. A bearer that expired months ago then survived every such run and would
have been destroyed only if somebody happened to re-mail that exact historical
period, which is precisely the dependency the owner decision forbids. The sweep
therefore sits on the maintenance boundary itself and opens its own ledger
connection when the run has none — counted separately from
`dashboard_ledger_connections_opened`, which keeps its meaning of "the publish
path needed a connection".

It does not depend on the **publisher** being usable either: `_maintenance_ledger()`
deliberately avoids `_publisher_services()`, so a missing machine credential
cannot stop expired secret material being destroyed in the client's own
database. The Worker-side session compaction is attempted only when the run
already holds a publisher client — a session exists only where a driver opened a
link, which means that client publishes — so the host-side guarantee is never
placed behind publisher configuration it does not need.

A **render-only** rehearsal is excluded, and that is not an exception to the
rule: it has no delivery identity, publishes nothing and must open no ledger
connection at all, so sweeping from one would make a dry run mutate the durable
state it exists to avoid touching.

The housekeeping numbers are deliberately **not** in `summary()`. All four jobs
read `summary()` immediately before `close()`, so anything the sweep recorded
there would be read one moment too early and reported as zero for ever; they
live in `EcoDashboardLinkService.maintenance` and are reported through the
`eco_dashboard_capabilities_retired` log event, which `close()` does emit to the
job's logger.

It is one statement per batch, and every safety property is a predicate of that
statement rather than of the caller:

| Never touched | Why |
|---|---|
| a grant that has not expired | `capability_expires_at <= now()`, evaluated by PostgreSQL in the writing statement |
| a grant with an unknown expiry | `IS NOT NULL` required — not knowing when a grant dies is not knowing that it has |
| a delivery under a live lease | excluded, and `FOR UPDATE SKIP LOCKED` serialises a concurrent sweep or `claim()` |
| `PROVIDER_SUBMISSION_PENDING`, `PROVIDER_ACCEPTED` | a message may exist; automation still owes reconciliation or terminalisation |
| `PROVIDER_AMBIGUOUS` | a human owes the answer. Already flagged operator-actionable, keeps its bearer, and §12.10 is untouched |
| `OPERATOR_REQUIRED` | likewise a human's |
| `REMOTE_DELIVERED` | its correct terminal condition is `FINALIZED`, one legal automatic transition away |
| a `DELIVERY_INTENT_RECORDED` row that was submitted | "never submitted" is **proven** per row (`provider_attempts = 0 AND provider_submitted_at IS NULL`), never assumed from the state name |

Retired: `CAPABILITY_PERSISTED`, `EXTERNAL_MAILER_HANDOFF`, an unsubmitted
`DELIVERY_INTENT_RECORDED`, and `PROVIDER_REJECTED` — a definite refusal whose
retry would now mail a link answering `410`. Each edge is authorised by
`LEGAL_TRANSITIONS`, and `delivery_contract` asserts at import that the retirable
set and the transition map agree.

It is **idempotent and interruptible**: retirement moves a row out of every
retirable state, so a second pass returns nothing; `bearer_cleared_at` is
`COALESCE`d so a repeat cannot rewrite when the secret stopped existing; and one
autocommitted statement per batch means an interruption did less work, never
half a row.

**What survives, and why.** `capability_id`, `capability_digest`,
`capability_expires_at`, `bearer_generation`, any bound provider submission
identity, the handoff record and a `capability_retirement` metadata entry — so
an expired link is still explainable months later. What does not survive is the
one value that could open a driver's dashboard.
`chk_..._bearer_absent` makes a retired row holding a bearer **unrepresentable**,
and `chk_..._retired_audit` stops the minimisation from also erasing which grant
it was.

`open_operations()` and `idx_..._open` both exclude `CLOSED_STATES`
(`FINALIZED`, `CAPABILITY_RETIRED`) and nothing else, so a delivery an external
mailer owns whose capability is still live stays visible — a rerun may
legitimately ask for that link again.

### 13.5 D1: expiry is enforced, not erased

`POST /api/publish/maintenance` — publisher-authenticated like every other write
route, `404` to everyone else, POST-only — compacts **expired sessions** in
bounded idempotent batches. It is called from the same place as the host sweep,
with failures contained.

It deliberately **deletes no `eco_capability` row**. Those rows hold no secret
(only a digest, an opaque `subject_ref` and an opaque object key); they already
stop authorising anything at `expires_at` (`classifyCapability` answers
`EXPIRED`); and they are what makes an old link answer `LINK_EXPIRED` rather
than looking like a link that never existed — a worse answer for the driver
holding a two-month-old e-mail and a worse answer for the operator asked what
happened to it.

**Capability expiry is not report retention.** No R2 object is deleted by
anything in this change. Snapshot retention remains a separate concern with a
separate owner decision, and is deliberately not invented here.

### 13.6 Previously issued grants

The policy decides a lifetime at the moment a grant is **minted** (publication)
or **replaced** (recovery rotation). Nothing reads, shortens or lengthens the
`expires_at` of a grant that already exists, so a link issued under the old
universal 45-day rule keeps exactly the validity its recipient was given. The
retirement sweep reads `capability_expires_at`; it never recomputes it. A legacy
45-day grant is therefore live for its own 45 days and is then retired by the
same rule as any other. Covered by
`test_a_legacy_forty_five_day_grant_keeps_the_validity_it_was_given`.

### 13.7 SMTP safety is unchanged

Capability publication still happens **before** SMTP submission, so an ordinary
send failure can still leave an unused live capability. That is acceptable for
its TTL, and once the TTL passes the sweep retires it — a published-but-never-sent
delivery no longer holds secret material or open work indefinitely. Nothing here
introduces an automatic retry of an ambiguous non-idempotent submission, nothing
regenerates a capability because SMTP was ambiguous, and `PROVIDER_AMBIGUOUS`
remains untouched, bearer-holding and operator-flagged (§12.10).

### 13.8 Verification

* `ops/tests_manual/test_driver_eco_dashboard_capability_lifecycle.py` — **11
  checks**, driving the REAL Worker over in-memory D1/R2: exact 10/60-day
  lifetimes measured on the route, fail-closed for every malformed period,
  recovery under the same policy, W1/W2/M1 overlap with three distinct served
  reports and snapshot pinning across sessions, expiry as `LINK_EXPIRED` rather
  than unknown, session capping, and the maintenance route's authentication and
  idempotence.
* `ops/tests_manual/test_eco_dashboard_capability_retirement_postgres.py` —
  **17 checks** on disposable PostgreSQL 16 against the real 049 + 050 + 051
  chain: bearer destruction without a resend, live bearers untouched, unknown
  expiry refused, idempotence with unmoved audit timestamps, bounded batches,
  lease fencing, two concurrent sweeps with no double retirement, the open and
  operator surfaces, SMTP-failed-but-published retirement, submitted vs
  unsubmitted intents, the untouched ambiguous contract, resend rotation that
  stays period-scoped, legacy 45-day preservation, the two physical CHECKs, and
  **051 applied over a POPULATED 050 ledger** — one row in every state that
  contract can hold, every constraint VALIDATED by PostgreSQL against them, no
  row reclassified and no expiry rewritten.
* `ops/tests_manual/test_eco_mailing_dashboard_link_integration.py` — **39
  checks** (Tiers A+B+C), including the wiring the suites above cannot see:
  closing an ordinary mailing run retires the expired delivery, leaves the
  still-valid one alone and asks the Worker for its session compaction; and
  `tier_b: a run that publishes nothing still retires expired capabilities and
  spares valid ones`, which calls `link_for` **zero times**, proves the quiet
  run opened its own ledger connection without touching the publish counter,
  retired the historical expired delivery, left the still-valid one reusable by
  a same-period resend without rotating it, and rewrote nothing on a second
  pass. That check fails on the pre-fix condition. Its ledger fixture is the
  049 + 050 + 051 chain.
* the release requirement in `db/schema_requirements.json` is re-keyed onto 051
  and verified against a real applied chain by `requirement_defects` — the same
  function release activation decides with.

### 13.9 Production rollout — deployed and verified 2026-08-28

The candidate of §13 is **committed, deployed and verified**. It is no longer a
candidate, a pending review or a blocked rollout.

| identity | value |
|---|---|
| implementation commit | `d49c7c718c59785fb4b04a1765e885caa5e5e137` (`feat(eco-dashboard): give a link the lifetime of its period, and retire it when it dies`), on `origin/main` |
| host release | `d49c7c718c59`, activated 2026-08-28 19:08:55 UTC (21:08 Europe/Warsaw) |
| host rollback target | `adb9750ca5d9` (`previous`) |
| Worker version | `a5ea4f49-fd50-4f94-8a30-a8e6e9721da7`, tag `d49c7c7`, deployed 2026-08-28 19:28:25 UTC at **100% traffic**. Superseded 2026-09-03 by `69bcf203…` (tag `e36bab3`, §8.2); this row records the §13 rollout, not the current edge |
| Worker rollback target | `97cf3d4b-2d29-4578-8e7d-9ef16c1c7223` — not used; no rollback was required. The rollback target for the CURRENT version is `72c67bec…` |
| migration 051 | recorded and **physically conformant** on all five enabled client business databases |

**Rollout ordering was respected.** §13's own constraint — the Worker fails
closed on a missing `X-Publication-Period`, so it must not lead the host — was
satisfied by ordering: migration 051 first, then the host release at 19:08:55
UTC, then the Worker at 19:28:25 UTC. The activation gate recorded the schema
evidence itself: `activations.log` for `d49c7c718c59` carries
`051_eco_dashboard_capability_retirement.sql` with `ledger_recorded: true` and
`physical_defects: []` for ECHO00001, DELTA00001, FOXTROT00001, BRAVO00016 and
ALPHA00001, against `enabled_account_count: 5` and fleet fingerprint
`dc413743506ca66213113b196237aa328958bc633ff59c9a7df6440318afbc70`. Re-running
`verify_schema_prerequisites` read-only after activation reproduces it.

**Read-only production verification after activation.**

* release integrity — `manage_release.py verify` on `d49c7c718c59`:
  `verified: true`, `verified_against_source_repository: true`, 1124 files,
  source tree digest `sha256:6e42667…`; `pointer_matches_running_release: true`;
  both `log-platform-api.service` (PID 153117) and
  `database-export-worker.service` (PID 153104) observed executing
  `releases/d49c7c718c59`; wrapper `release` variant, `wrapper_up_to_date: true`
  (SHA-256 `e6ab8204…`); cutover fence `STATE=VERIFIED`;
* Worker identity — `wrangler deployments list` shows the 2026-08-28T19:28:25Z
  deployment as the current one, `72c67bec…` at 100%; `wrangler versions view`
  confirms tag `d49c7c7`, the commit in the version message, both secrets
  (`CAPABILITY_PEPPER`, `PUBLISHER_KEY_DIGEST`) present, and the four expected
  bindings including `SESSION_RATE_LIMIT` at 60 requests/60 s;
* served surface — `/` → 200 with the full CSP/HSTS/COEP/COOP/CORP header set
  and `private, no-store`; **all eight served static paths byte-identical** to
  `assets/driver_eco_dashboard/` at `d49c7c718c59…` (`index.html`
  `d50444ae62e8067d…`, `css/dashboard.css` `35c416ee248190ec…`, `js/format.js`
  `6a4b834cb26d2acf…`, `js/render.js` `ef0d3fc1b2436761…`,
  `js/snapshot-source.js` `07524dd37dffac01…`, `js/app.js` `6c0a74cdb397e349…`,
  `js/boot.js` `f0fc4a3668202147…`, `js/capability-bootstrap.js`
  `aa9e10ed6efc4d67…`), and `/` serves those `index.html` bytes;
* fail-closed — unauthenticated `GET /api/snapshot` → `401 {"error":"INVALID_LINK"}`;
* publisher boundary — anonymous `POST /api/publish/maintenance`,
  `POST /api/publish` and `POST /api/publish/recover` all → `404
  {"error":"SNAPSHOT_UNAVAILABLE"}`, byte-identical to one another, so an
  unauthenticated caller cannot distinguish the maintenance route from any
  other publisher route and no capability, operation or period data is exposed.
  `authorisePublisher()` runs before any D1 or R2 access, so the probe performed
  no compaction and no mutation;
* dev-only surfaces — `/preview.html`, `/preview`, `/README.md`, `/fixtures/`,
  `/fixtures/snapshot.json`, `/spec/`, `/local/`, `/worker/index.js`,
  `/wrangler.toml`, `/schema/001_authorization.sql`, `/package.json` and
  `/.dev.vars` all → 404.

**The deployment itself published nothing and retired nothing.** On the
BRAVO00016 ledger (the only enabled client with rows — 71, all
`EXTERNAL_MAILER_HANDOFF`, all `weekly`): **zero** rows created or updated after
2026-08-28T19:00Z, **zero** `CAPABILITY_RETIRED` rows, and the newest row's
`created_at` is 2026-08-28T12:40:05+02:00 — hours before either activation. D1
agrees: 75 capabilities, 72 publication operations, 68 sessions, newest
`issued_at` and newest publication `created_at`/`updated_at` all
2026-08-28T12:40:05Z. ECHO00001, DELTA00001, FOXTROT00001 and ALPHA00001 ledgers
remain at 0 rows.

**No legacy expiry was rewritten.** Every one of the 71 existing grants still
carries exactly **45 days** between `created_at` and `capability_expires_at`
(`array_agg(distinct …)` returns the single value `45`), spanning
2026-10-04T23:18:36+02:00 to 2026-10-12T14:40:05+02:00; D1's newest grant shows
the same 45-day span. None is expired yet and all 71 still hold their
`capability_secret`, so the sweep correctly had nothing to retire — §13.6 holds
in production, not only in test.

**What this rollout did not do.** No capability was generated, no Eco e-mail was
sent, no real driver data was read, no R2 object was written or deleted, no
maintenance compaction was executed, no DNS or infrastructure change was made
and no credential was rotated. R2 snapshot retention remains a separate,
undecided concern (§13.5): nothing in this change deletes a report.

**Classification unchanged.** `CLOUDFLARE_WORKER_GIT_IDENTITY_UNPROVEN_BUT_CONTENT_EQUIVALENT`
still stands for `72c67bec…`. The eight served static paths are byte-bound to
the commit, but the deployed **script bundle** has never been provider-hash-bound
to a repository build and the version tag/message is operator-supplied, so exact
SCM binding of the Worker code must not be claimed from it. The period-header
enforcement and the 10/60-day policy are therefore evidenced by the deterministic
suites of §13.8 and by deployment provenance — not by an unauthenticated
production probe, because every route that could exhibit them is behind
`authorisePublisher()`.
