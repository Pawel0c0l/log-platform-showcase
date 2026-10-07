# Telematics `/trips` pagination compatibility mode — design specification

**Status: design of record. The compatibility state machine (C7) is implemented and deployed.**

> **Status update (2026-08-03).** Three things have changed since this document was written as
> design-only: the configuration, stabilization, coverage and dispatcher/finalizer commits C1–C6 have
> shipped and are deployed (migrations `055`–`057` applied); **open decision D5 is resolved** by the
> accepted operator decision `docs/16_telematics_d5_total_policy_decision.md` (Option B — absent `total`
> permitted under data invariants, ACCEPTED 2026-08-03); and **C7 is now implemented** —
> `jobs/api/telematics/provider_client.py:_fetch_paginated_data_invariants_v1` plus the compatibility
> budgets in `provider_safety.py` and the minimum mode propagation in `sync_trips_and_speeding.py`.
> §7.2, §7.3 and the §8 `total` taxonomy are normative, not proposals.
>
> **Status update (2026-08-10). C7 is deployed and exercised.** The paragraph that previously stood
> here — "C7 is implemented but not deployed… `BRAVO00016` remains the sole armed, unexercised
> compatibility client; the other four remain `strict_meta`" — was stale on every count and is
> corrected as follows, verified against the control plane and run history:
>
> * all five clients run `trips_pagination_mode = data_invariants_v1`;
> * the C11 recovery **has** run for `ALPHA00001` on 2026-08-03 21:11 under approval
>   `TELEMATICS-C11-ALPHA00001-2026-08-PRODUCTION-REPORTING-1`, fetching 19,974 rows;
> * compatibility fires have occurred on every scheduled `trips_sync` run since 2026-08-04;
> * the strict-mode guard fired in production on 2026-08-01 and 2026-08-03 with
>   `PAGINATION_MISMATCH`, and correctly aborted before writing any row.
>
> Separately, live GET-only probes on 2026-08-10 established that the `/trips` **request**
> `start_timestamp` / `end_timestamp` parameters are Europe/Warsaw local wall-clock while response
> rows are UTC. That contract, and the DST handling it forces, is specified in
> `docs/18_telematics_trips_request_time_contract.md`. It does not alter anything in this document:
> pagination control flow is unchanged.

This document specifies an opt-in compatibility mode for Telematics `GET /trips`
pagination. Except where a section is explicitly marked as accepted or shipped, every item below is a
*proposal* that requires its own reviewed implementation task and its own validation gate.

**Path note.** The task requested `docs/10_telematics_trips_pagination_compatibility.md`. The
`10_` slot is already occupied twice (`docs/10_platform_architecture.md`,
`docs/10_scheduler_design.md`) and `11_` is taken by `docs/11_operational_readiness.md`, so the
nearest consistent path in the existing documentation map is `docs/12_…`, the next free ordinal.
No other documentation file is changed by this task.

Related canonical documents:

- `docs/07_operations.md` §5.4 (Telematics hard safety limits), §5.4.1 (`/trips` pagination
  diagnostic), §5.5 (dispatcher).
- `docs/05_jobs.md` § `jobs.api.telematics.sync_trips_and_speeding`.
- `docs/06_security.md` § Sekrety, § `suspected_bug` payload sanitization.
- `docs/09_disaster_recovery.md` § recovery/backfill boundaries.
- `docs/openapi.yaml` — external Telematics Fleet API spec, `/trips` and
  `#/components/schemas/pagination`.

Code of record for this design:

- `jobs/api/telematics/provider_client.py` (`_fetch_paginated`, `fetch_trips`,
  `_parse_pagination_meta`).
- `jobs/api/telematics/provider_safety.py` (`SafetyLimits`, `ProviderRunBudget`,
  `TelematicsProviderSafetyError`).
- `jobs/api/telematics/sync_trips_and_speeding.py` (`_fetch_trips_in_chunks`, trip parsing, the
  single client-business transaction).
- `ops/diagnose_telematics_trips_pagination.py` and
  `ops/tests_manual/test_telematics_trips_pagination_diagnostic.py`.

---

## 0. Evidence base and its exact limits

### 0.1 Confirmed production evidence

Repository commit `2bdcbe27641f53886bc1a9b88cd93964a33d28d4`. Client `DELTA00001`, closed
historical window `2026-07-28 16:00:00` → `2026-07-28 17:00:00` UTC.

| Run | Page | Rows | `current_page` | `per_page` | `last_page` | `from` | `to` | `total` |
|---|---|---|---|---|---|---|---|---|
| limit 1000 | 1 | 58 | 1 | 10 | 6 | — | — | 58 |
| limit 1000 | 2 | 0 | 1 | 10 | 6 | — | — | 58 |
| limit 25 | 1 | 25 | 1 | 10 | 6 | 1 | 25 | 58 |
| limit 25 | 2 | 25 | 1 | 10 | 6 | 1 | 25 | 58 |

For the limit-25 pair the two `meta` blocks were byte-identical while the two `data` arrays were
disjoint on `provider_trip_id`; intra-page duplicates were zero on both pages.

Classification: `TELEMATICS_LIMIT25_DIAGNOSTIC_COMPLETE_DISTINCT_CONTINUATION`.
Diagnostic tool result: `TELEMATICS_TRIPS_PAGES_DISTINCT_METADATA_BROKEN`.
Feasibility: `COMPATIBILITY_LAYER_FEASIBLE_FOR_DESIGN`.

`docs/openapi.yaml` declares `limit_without_validation` with `default: 10` and requires all six
`pagination` fields as integers. `per_page: 10` and `last_page: 6` (= `ceil(58 / 10)`) are exactly
what the *default* page size would produce, which is consistent with the hypothesis that the
metadata layer stopped seeing the requested `limit` while the data layer kept honouring it.
`to: 25` did follow the requested limit while `from: 1` did not advance, so the metadata block is
internally inconsistent as well as wrong — it is not a uniform "one page size behind" error and
must not be reinterpreted or repaired arithmetically.

### 0.2 Proven only for the observed closed window

1. The requested `limit` controls the returned `data` page size.
2. The requested `page` selects a distinct continuation.
3. Page 1 and page 2 carry disjoint `provider_trip_id` values.
4. `meta.total` stayed at 58 across both requests.
5. The remaining pagination metadata describes neither the request nor the response correctly.

### 0.3 Explicitly unproven — each is a rollout or validation gate

| # | Unproven assumption | Gate |
|---|---|---|
| U1 | Page 3 returns the remaining 8 rows | §12 page-3 probe, mandatory before enablement |
| U2 | A short page reliably terminates pagination | §5 termination, §12 probe |
| U3 | Ordering is stable across page requests | §6, cross-page invariants |
| U4 | Pages stay disjoint for larger or changing windows | §6, §11 staged rollout |
| U5 | `meta.total` is stable for every client and window | §7, abort on change |
| U6 | All Telematics regions/credentials behave identically | §3 per-client opt-in |
| U7 | Concurrent provider-side mutation cannot shift page boundaries | §6 eligibility rules |
| U8 | All clients can safely enable the mode immediately | §11 per-client rollout |

Nothing in this design may be implemented in a way that silently assumes U1–U8.

---

## 1. Problem statement

### 1.1 The provider contract change

Between 2026-07-29 and 2026-08-01 the Telematics `/trips` endpoint began emitting pagination
metadata that contradicts both the request and the response body. `page=N` is answered with
`meta.current_page=1` for every `N`; `meta.per_page` reports the OpenAPI default of `10`
regardless of the requested `limit`; `meta.last_page` is computed from that wrong page size;
`meta.from` does not advance with the page while `meta.to` tracks the requested limit.

### 1.2 Broken metadata fields

`current_page`, `per_page`, `last_page`, `from` are wrong. `to` is inconsistent (right magnitude,
wrong offset). `total` is the only field observed to be both present and plausible, and even that
is proven stable only for one window (U5).

### 1.3 The proven working data path

`data` is still a list; the requested `limit` still bounds it; the requested `page` still selects a
different slice; the slices observed were disjoint on the same business key production already
uses — `provider_trip_id = int(row["trip_id"])`, the conflict target of the
`client_trips` upsert (`ON CONFLICT (client_id, provider_trip_id)`).

### 1.4 Why `PAGINATION_MISMATCH` correctly stops ingestion

`jobs/api/telematics/provider_client.py:_fetch_paginated` requires
`meta.current_page == requested_page` whenever `meta` is present, and raises
`TelematicsProviderSafetyError("PAGINATION_MISMATCH", …)`. Per `CONVENTIONS.md` §4 that error is
fatal at the run level. In `sync_trips_and_speeding.py` the client-business connection is not even
opened until every provider fetch has completed, so a safety stop during `/trips` means the run
ends `FAILED` with **zero** business rows written. That is the correct outcome: the loop's entire
termination logic was derived from `current_page`/`last_page`, and once those became fiction the
loop had no trustworthy stopping rule. Stopping is the only safe response to a control channel
that has become unreliable.

### 1.5 Why deleting the check would be unsafe

Removing the `current_page` equality check without replacing it leaves `_fetch_paginated` with:

- `last_page` (wrong, currently 6 instead of 3) as the loop bound — it would request pages that do
  not exist, and if the provider answers an out-of-range page by repeating page 1, the run would
  ingest the same rows repeatedly;
- `current_page >= last_page` as the exit condition — with `current_page` pinned at 1 and
  `last_page` at 6, the loop never satisfies it and runs to the `MAX_PAGES_PER_SUBWINDOW` cap;
- the empty-streak guard requiring `current_page < last_page`, which the frozen metadata makes
  permanently true, so genuine emptiness would be misread;
- the fingerprint loop detector as the *only* remaining corruption guard — it compares a page only
  against its immediate predecessor, so an A/B/A/B alternation, or a repeat separated by one page,
  passes undetected.

The result would be silent duplication or silent truncation of business data, which is worse than
a `FAILED` run. The check may only be replaced by an equally strong set of data-derived
invariants, never simply deleted.

---

## 2. Goals and non-goals

### 2.1 Goals

- **G1** Continue ingesting safely when the *data* pagination works and only the *metadata* is
  broken.
- **G2** Preserve or strengthen every existing loop, budget and corruption protection. Compatibility
  mode adds checks; it removes only the ones that depend on fields proven to be false.
- **G3** Remain strictly opt-in, per client, with the strict behaviour as the default.
- **G4** Fail closed. Any unverifiable condition aborts the sub-window before any write.
- **G5** Retain auditable, privacy-safe evidence for every page decision.
- **G6** Avoid duplicate trips and silently missing trips, both of which corrupt downstream Eco
  Driving aggregates, snapshots and mailings.

### 2.2 Non-goals

- **N1** Trusting `meta.current_page`.
- **N2** Trusting `meta.per_page`, `meta.last_page`, `meta.from` or `meta.to`.
- **N3** Enabling all clients at once.
- **N4** Automatic recovery, catch-up or backfill of missed windows. Recovery stays a separately
  authorized operator action.
- **N5** Changing the external provider or working around provider escalation.
- **N6** Weakening any request, page, retry, timeout or byte budget. Compatibility mode may only
  tighten them.
- **N7** Applying the mode to any endpoint other than `/trips`. `/vehicles`, `/drivers`,
  `/vehicles/events`, `/alerts/notifications` and `/fuel/*` keep their current strict handling.
- **N8** Repairing or reinterpreting the broken metadata arithmetically.

---

## 3. Compatibility-mode activation

### 3.1 Model

Client-scoped configuration in the control plane, not a global environment fallback and not an
implicit "retry loosely on `PAGINATION_MISMATCH`" behaviour. Precedent:
`workflow_a_control.client_account.trip_metrics_population_source`, which is a per-client,
`NOT NULL`, CHECK-constrained behavioural selector loaded into the frozen `ClientAccountConfig`
by `jobs/api/telematics/control_plane.py`.

Rejected alternatives:

- *Environment variable* (`TELEMATICS_PROVIDER_TRIPS_PAGINATION_MODE`) — host-wide, invisible in the
  control plane, applies to every client on the host, and cannot be audited per client. Rejected.
- *Job parameter only* — would let any manual invocation opt in without review, and dispatcher
  fires would silently disagree with manual runs. Rejected as the primary switch (see §3.8 for
  the narrowly scoped dry-run parameter).
- *Schedule-scoped column* (`client_dataset_schedule`) — the behaviour is a property of the
  provider account, not of a schedule; a manual run must get the same treatment as a scheduled
  one. Rejected.

### 3.2 Proposed configuration field

| Item | Proposal |
|---|---|
| Storage | `workflow_a_control.client_account` (platform DB) |
| Column | `trips_pagination_mode TEXT NOT NULL DEFAULT 'strict_meta'` |
| Allowed values | `'strict_meta'`, `'data_invariants_v1'` |
| Constraint | `CHECK (trips_pagination_mode IN ('strict_meta','data_invariants_v1'))` |
| Migration | new platform migration, next free ordinal `055_workflow_a_trips_pagination_mode.sql` |
| Migration shape | additive column → backfill `'strict_meta'` → `SET DEFAULT` → `SET NOT NULL` → `CHECK`, exactly the pattern of `018_workflow_a_schedule_event_enrichment_mode.sql` |
| Loader | add to the `SELECT` and to the frozen `ClientAccountConfig` dataclass in `control_plane.py` |
| Consumer | `sync_trips_and_speeding.run()` passes it into `TelematicsFleetProviderClient`; only `fetch_trips` honours it |

`strict_meta` is the default and reproduces today's behaviour byte for byte, including the
`PAGINATION_MISMATCH` abort. A database row that predates the migration, a `NULL`, an unknown
string, or a failure to read the column must all resolve to `strict_meta` — never to the
permissive mode.

### 3.3 Validation

1. Database `CHECK` constraint rejects unknown values at write time.
2. `control_plane.load_client_account_config` re-validates the string against the allowlist in
   Python and raises on an unknown value, rather than defaulting silently — a value that passed
   the DB check but not the Python allowlist means the two have drifted and is a fail-closed
   condition.
3. The provider client accepts the mode as a typed value (enum/`Literal`), not a free string.
4. `data_invariants_v1` additionally requires, at run start, that the run's window satisfies the
   eligibility rules in §6.4. An ineligible window in `data_invariants_v1` aborts before the first
   request with `PAGINATION_COMPAT_WINDOW_INELIGIBLE`; it does **not** silently fall back to
   `strict_meta`, because a silent fallback would produce a `PAGINATION_MISMATCH` that operators
   would misread as a regression.

### 3.4 Audit trail

- The change is an operator `UPDATE` on `client_account`, reviewed like any other control-plane
  change; §11 requires the change to be recorded in the rollout ticket with client code, mode,
  operator and UTC timestamp.
- Every run logs the resolved mode (§10) in the run-start context and in every page log, so
  `runs`/`logs` in the platform DB carry the effective mode for every historical run.
- No new audit table is proposed. If a reviewer requires immutable history of the switch itself,
  that is an open decision (§17, D9) and would be an additional migration, not part of the first
  implementation commit.

### 3.5 Visibility in logs and runs

Every `/trips` page log and every run-level summary carries
`trips_pagination_mode = "strict_meta" | "data_invariants_v1"` as a flat scalar context key, per
`CONVENTIONS.md` §8. Operators can therefore answer "was this run permissive?" from
`logs.context` alone, without reading the control plane at the time of the incident.

### 3.6 Immediate disable

```sql
UPDATE workflow_a_control.client_account
   SET trips_pagination_mode = 'strict_meta'
 WHERE client_code = '<CODE>';
```

Takes effect on the next run start, because `control_plane` is loaded fresh at the beginning of
every run (`CONVENTIONS.md` §10 — no cross-run caches). No deploy, no restart, no timer change,
no migration rollback. A run already in flight is not affected; if an in-flight run must be
stopped, that is an operator kill of the runner process, and the single-transaction boundary in
§9 guarantees the killed run writes nothing.

### 3.7 Naming

`strict_meta` / `data_invariants_v1` are proposed because they are lowercase snake enum values
consistent with `enabled|disabled` and `strict|audited_best_effort` elsewhere in the repository,
and because the `_v1` suffix leaves room for a future `data_invariants_v2` once page-3 behaviour
and larger windows are proven. If review prefers different names, the semantics — not the
spelling — are what this design fixes.

### 3.8 Compare-only / dry-run parameter (optional, open decision D8)

A narrowly scoped job parameter `trips_pagination_compare_only=true` could, while the client is
still `strict_meta`, run the compatibility state machine's *validation* over the fetched pages and
log what it would have concluded — without changing which rows are ingested and without
suppressing the strict abort. This is a diagnostic aid for step 4 of the rollout and is
deliberately not a second way to enable ingestion.

---

## 4. Page-fetch state machine

### 4.1 Scope

Applies to `/trips` only, inside one sub-window (one `iter_31d_windows` slice of one
`_fetch_trips_in_chunks` chunk). Each sub-window is validated independently and completely.
Cross-page state is never carried across sub-window boundaries.

### 4.2 States

| State | Meaning | Exits |
|---|---|---|
| `INIT` | Validate mode, window eligibility, budgets; allocate per-sub-window state | `REQUEST`, `ABORT` |
| `REQUEST` | Consume budget, construct params, issue one bounded GET | `VALIDATE_RESPONSE`, `ABORT` |
| `VALIDATE_RESPONSE` | HTTP/JSON/shape/size checks | `EXTRACT_IDENTITY`, `ABORT` |
| `EXTRACT_IDENTITY` | Derive `provider_trip_id` for every row | `PAGE_LOCAL_CHECKS`, `ABORT` |
| `PAGE_LOCAL_CHECKS` | Intra-page duplicates, count vs limit, fingerprint | `CROSS_PAGE_CHECKS`, `ABORT` |
| `CROSS_PAGE_CHECKS` | Overlap, repeat, fingerprint history, `total` stability | `ACCUMULATE`, `ABORT` |
| `ACCUMULATE` | Append rows to the in-memory sub-window buffer | `TERMINATE?` |
| `TERMINATE?` | Short-page rule + budget checks | `CONTINUE`, `RECONCILE`, `ABORT` |
| `CONTINUE` | `page += 1` | `REQUEST` |
| `RECONCILE` | Post-termination `total` reconciliation (§7) | `SUCCESS`, `ABORT` |
| `SUCCESS` | Sub-window buffer released to the caller | — |
| `ABORT` | `TelematicsProviderSafetyError`; run fails; no write | — |

`meta.current_page`, `meta.per_page`, `meta.last_page`, `meta.from` and `meta.to` are **never**
read by any transition in `data_invariants_v1`. They are captured for logging only.

### 4.3 Pseudocode

```text
fetch_trips_subwindow_compat(path="/trips", base_params, sub_window, limit, budget, limits):

    # --- INIT ---
    assert mode == "data_invariants_v1"
    assert window_is_eligible(sub_window)          # §6.4, else PAGINATION_COMPAT_WINDOW_INELIGIBLE
    page                = 1
    rows                = []                        # in memory only
    seen_ids            = set()                     # provider_trip_id, whole sub-window
    seen_fingerprints   = set()                     # ordered+unordered page digests
    seen_id_sets        = set()                     # unordered identity-set digests
    first_total         = UNSET
    pages_fetched       = 0
    rows_total          = 0
    bytes_total         = 0
    started_at          = monotonic()

    while True:
        # --- REQUEST ---
        if pages_fetched >= limits.max_pages_per_subwindow:  abort(PAGE_BUDGET_EXCEEDED)
        if elapsed(started_at) > limits.max_elapsed_s:       abort(ELAPSED_BUDGET_EXCEEDED)
        budget.before_request(path, sub_window)              # existing ProviderRunBudget
        params = base_params | {"page": page, "limit": limit}
        payload, resp_bytes = request_json_bounded(          # bounded retries: timeout/conn only
            path, params, sub_window, max_bytes=limits.max_response_bytes)
        bytes_total += resp_bytes
        if bytes_total > limits.max_response_bytes_subwindow: abort(RESPONSE_BYTES_EXCEEDED)
        pages_fetched += 1
        budget.record_page_completed(path, sub_window, page)

        # --- VALIDATE_RESPONSE ---
        if not isinstance(payload, dict):        abort(MALFORMED_RESPONSE)
        data = payload.get("data")
        if data is None or not isinstance(data, list): abort(MALFORMED_RESPONSE)
        if not shape_compatible_with_previous(payload):     # §5.3
            abort(PAGINATION_COMPAT_SHAPE_UNSTABLE)

        # --- EXTRACT_IDENTITY ---
        page_ids = []
        for row in data:
            if not isinstance(row, dict):        abort(MALFORMED_RESPONSE)
            tid = strict_int(row.get("trip_id"))            # same rule as ingestion
            if tid is None:                      abort(PAGINATION_COMPAT_IDENTITY_MISSING)
            page_ids.append(tid)

        # --- PAGE_LOCAL_CHECKS ---
        if len(set(page_ids)) != len(page_ids):  abort(PAGINATION_COMPAT_DUPLICATE_IN_PAGE)
        if len(data) > limit:                    abort(PAGINATION_COMPAT_ROWS_EXCEED_LIMIT)
        fp_ordered   = sha256(canonical(page_ids))
        fp_unordered = sha256(canonical(sorted(set(page_ids))))

        # --- CROSS_PAGE_CHECKS ---
        if fp_ordered in seen_fingerprints:      abort(PAGINATION_COMPAT_PAGE_REPEATED)
        if fp_unordered in seen_id_sets:         abort(PAGINATION_COMPAT_PAGE_REPEATED)
        overlap = seen_ids & set(page_ids)
        if overlap:                              abort(PAGINATION_COMPAT_PAGE_OVERLAP)
        total = read_total(payload)              # §7; ABSENT is permitted (docs/16 §5.1)
        if total is INVALID:                     abort(PAGINATION_COMPAT_TOTAL_INVALID)
        if first_total is UNSET: first_total = total          # may be ABSENT
        elif total != first_total:               abort(PAGINATION_COMPAT_TOTAL_UNSTABLE)

        # --- ACCUMULATE ---
        rows.extend(data)
        rows_total += len(data)
        seen_ids |= set(page_ids)
        seen_fingerprints.add(fp_ordered)
        seen_id_sets.add(fp_unordered)
        if rows_total > limits.max_rows_per_subwindow:  abort(ROW_BUDGET_EXCEEDED)
        if first_total is a valid int and rows_total > first_total:
            abort(PAGINATION_COMPAT_TOTAL_EXCEEDED)
        log_page(...)                                        # §10

        # --- TERMINATE? ---
        if len(data) < limit:
            termination_reason = "short_page"
            break
        page += 1

    # --- RECONCILE ---
    reconcile_total(first_total, rows_total, termination_reason)   # §7.3
    return rows                                                    # still in memory only
```

`strict_meta` is untouched: `_fetch_paginated` keeps its current body verbatim, including
`PAGINATION_MISMATCH`, `PAGINATION_LOOP`, `PAGINATION_NON_PROGRESS`, `MALFORMED_PAGINATION` and
`INCONSISTENT_PAGINATION`.

---

## 5. Required replacement invariants

Every check below must pass **before** the page's rows are accepted into the sub-window buffer.
A failure raises `TelematicsProviderSafetyError` and ends the run.

### 5.1 Response shape

| # | Invariant | Abort code |
|---|---|---|
| S1 | HTTP 2xx after the existing bounded retry policy | `HTTP_ERROR` / `HTTP_RETRY_EXHAUSTED` (existing) |
| S2 | Body parses as JSON and the top level is an object | `MALFORMED_RESPONSE` (existing) |
| S3 | `data` present and is a list | `MALFORMED_RESPONSE` (existing) |
| S4 | Every element of `data` is an object | `MALFORMED_RESPONSE` (existing) |
| S5 | Every row yields a valid `provider_trip_id` via `int(row["trip_id"])` | `PAGINATION_COMPAT_IDENTITY_MISSING` |
| S6 | No row has a missing, null, boolean or non-integral `trip_id` | `PAGINATION_COMPAT_IDENTITY_MISSING` |
| S7 | Response body stays under the per-response and per-sub-window byte caps | `PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED` |

S5/S6 are stricter than today's ingestion, which logs a `WARNING` and raises only at parse time.
In compatibility mode identity is the *control channel*, so a row without a usable identity makes
the overlap proof impossible and must abort the fetch, not the parse.

### 5.2 Page-local checks

| # | Invariant | Abort code |
|---|---|---|
| P1 | No duplicate `provider_trip_id` inside one page | `PAGINATION_COMPAT_DUPLICATE_IN_PAGE` |
| P2 | Deterministic page fingerprint computed over the ordered identity list and over the sorted unique identity set | — (input to P3/C2) |
| P3 | `len(data) <= requested_limit` | `PAGINATION_COMPAT_ROWS_EXCEED_LIMIT` |
| P4 | A non-final page must contain exactly `requested_limit` rows — this is the contrapositive of the termination rule in §5.4 and is what makes a short page meaningful | (enforced as termination, §5.4) |

Fingerprints are computed over the **identity list**, not over the raw payload, so that a
cosmetic field change (a re-rendered address, a recomputed duration) cannot mask a genuine repeat.
The existing `_page_items_fingerprint` hashes the whole payload and is retained only for the
strict path.

### 5.3 Cross-page checks

| # | Invariant | Abort code |
|---|---|---|
| C1 | No `provider_trip_id` may appear on any previous page of the sub-window (full history, not just the previous page) | `PAGINATION_COMPAT_PAGE_OVERLAP` |
| C2 | No ordered page fingerprint and no unordered identity-set digest may repeat | `PAGINATION_COMPAT_PAGE_REPEATED` |
| C3 | Partial overlap is never tolerated — one shared identity is enough to abort | `PAGINATION_COMPAT_PAGE_OVERLAP` |
| C4 | An identical ordered sequence or identical unordered set aborts even if the raw payload differs | `PAGINATION_COMPAT_PAGE_REPEATED` |
| C5 | `total`, when present and valid, must be identical on every page of the sub-window; presence itself must not flip mid-sub-window | `PAGINATION_COMPAT_TOTAL_UNSTABLE` |
| C6 | Response shape must stay structurally compatible: `data` stays a list; the set of top-level keys must not lose `data`; `meta` must not change JSON type between pages; rows must stay objects | `PAGINATION_COMPAT_SHAPE_UNSTABLE` |

C1 is the single most important replacement for `PAGINATION_MISMATCH`: it is the invariant that
makes duplicate ingestion impossible within a sub-window. It costs one integer set of at most
`max_rows_per_subwindow` entries, which is bounded by §5.5.

### 5.4 Termination

**Authoritative rule:** terminate the sub-window when `len(data) < requested_limit`.

`docs/16_telematics_d5_total_policy_decision.md` §5.1–§5.2 makes this the **authoritative termination
signal** for `data_invariants_v1`, and the §12 page-2/3 probe has since been executed and confirmed
the expected short page for the probed window (`docs/13_…` §17). The paragraphs below record the
original evidence limits, which still gate *enablement breadth* (U2 for larger and more recent
windows) but no longer leave the termination rule itself undecided.

*Evidence as of the original design (superseded history, retained for context — at that point this
was a hypothesis rather than an accepted rule):*

- With `limit=1000`, page 1 returned 58 rows (a short page) and page 2 returned 0 — consistent
  with the rule, but this only shows that a short *first* page is followed by emptiness.
- With `limit=25`, page 1 and page 2 both returned exactly 25 rows — full pages, so the rule was
  never exercised mid-sequence.
- Page 3 had not been observed. It has since been probed and returned the expected 8 rows, confirming
  the rule for that one closed window (`TELEMATICS_PAGE3_VALIDATION_SHORT_PAGE_CONFIRMED`). Behaviour
  for larger and more recent windows is still U2.

Therefore the *breadth* of the rule stays a rollout gate: the probe covered one closed window, and
`data_invariants_v1` is enabled per client under §11 with its own evidence. Concretely, an empty page
(`len(data) == 0`) also terminates, and the reconciliation in §7.3 runs on every termination **when
`total` is present**. When `total` is absent, termination on the short page is sufficient and
reconciliation is skipped (`docs/16_…` §5.1).

`total` may contribute additional consistency checks but must never be the sole termination
mechanism, because a wrong or absent `total` would then decide how much data is ingested.

### 5.5 Budgets

All existing budgets remain in force unchanged and continue to be checked *before* each request by
`ProviderRunBudget.before_request` / `record_page_completed`.

| Budget | Source | Value | Compatibility-mode treatment |
|---|---|---|---|
| Requests per run | `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN` | 500 | unchanged |
| Requests per endpoint | `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_ENDPOINT` | 300 | unchanged |
| Requests per sub-window | `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_SUBWINDOW` | 80 | unchanged |
| Pages per sub-window | `TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW` | 50 | unchanged; hit ⇒ abort |
| Retries per HTTP call | `TELEMATICS_PROVIDER_MAX_RETRIES` | 2 | unchanged; timeout/connection only, never for pagination or shape anomalies |
| Timeout | `TELEMATICS_PROVIDER_TIMEOUT_S` | 60 | unchanged |
| Page size | `TELEMATICS_PROVIDER_PAGE_LIMIT` | 1000 | unchanged default (open decision D2) |
| Rows per sub-window | *new* `TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW` | default `page_limit × max_pages_per_subwindow` | abort `PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED` |
| Response bytes | *new* `TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES` | 32 MiB per response (mirrors the diagnostic default), ceiling 64 MiB | abort |
| Elapsed per sub-window | *new* `TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S` | 900 | abort |

New budgets may only ever *reduce* what is already permitted. Per `CURRENT_TASK_CONTEXT.md` §5.2,
the existing defaults are a contract; raising any of them to accommodate compatibility mode is out
of scope and would itself require review.

---

## 6. Ordering and mutation risk

### 6.1 Why disjointness is not sufficiency

Offset pagination reads a moving result set. Between the request for page N and page N+1 the
provider's underlying set can change. Disjoint pages prove that *no row was returned twice*; they
prove nothing about *rows that were never returned at all*. The compatibility mode's overlap check
(C1) is therefore a duplicate-prevention mechanism, not a completeness proof, and this document
must not be read as claiming otherwise.

### 6.2 Failure modes

| Mode | Mechanism | Consequence | Detected? |
|---|---|---|---|
| **Insert** | A trip is added before page N+1 with a sort position ≤ the current offset | Every subsequent row shifts one position later; the row previously at the boundary is returned twice | Yes — C1 aborts |
| **Delete/correct** | A trip is removed or its sort key changes | Everything shifts one position earlier; exactly one row is **skipped forever** | **No** — pages stay disjoint, counts look plausible; only the §7.3 `total` reconciliation may notice |
| **Unstable sort** | The provider has no deterministic tiebreaker on equal timestamps | Rows can be both duplicated and skipped in the same fetch | Duplicates: yes (C1). Skips: no |
| **Late-arriving telemetry** | A device uploads a trip for an already-fetched window mid-fetch | Behaves like Insert | Duplicate detected; the late trip may still be missed |
| **Boundary shifts** | Combination of the above across a chunk boundary | Rows near `chunk_exclusive_end_ts` are the most exposed | Partly — the existing 1-second `iter_31d_windows` overlap helps but is unrelated to offset drift |

The undetectable case (skip via delete/shift) is the reason the mode must be restricted to windows
whose contents are no longer changing.

### 6.3 Closed historical vs. recent windows

A *closed historical* window is one whose end timestamp is far enough in the past that the
provider's trip set for it is stable: all devices have uploaded, all trip closures have been
computed, no corrections are still in flight. A *recent* window is still accumulating and
correcting rows, so its offsets shift under a multi-page read. Only the first kind is eligible.

The evidence in §0.1 was collected on a closed window roughly three days old. There is no evidence
for recent windows, and U7 explicitly forbids assuming provider-side mutation is impossible.

### 6.4 Proposed eligibility rules

A sub-window is eligible for `data_invariants_v1` only if all hold:

1. **Closed window.** `sub_window_end_ts < now_utc - stabilization_delay`.
2. **Stabilization delay.** `TELEMATICS_PROVIDER_COMPAT_MIN_WINDOW_AGE_MINUTES`, proposed default
   **180** (open decision D1; a reviewer may prefer 24 h for the first clients).
3. **Rejection near now.** Any sub-window that fails (1) aborts the run with
   `PAGINATION_COMPAT_WINDOW_INELIGIBLE` rather than falling back to `strict_meta` (§3.3).
4. **Single-page fast path.** If the first page is already short, the sub-window completed in one
   request and never observed a moving offset. This case is inherently safe against offset drift
   and is the case that today's `limit=1000` production configuration will hit for almost every
   two-day chunk of a small fleet. It still runs every §5 check.
5. **Per-client rollout.** Eligibility is evaluated per client; a client is enabled only through
   §11.
6. **Overlap checks always on.** C1–C4 are not relaxed for eligible windows.

Note the interaction with `docs/05_jobs.md` §`trips_sync`: the dispatcher computes the window as
`scheduled_fire_ts - lookback_days → scheduled_fire_ts`. With `lookback_days ≥ 1` and a nightly
`run_time`, the *end* of a scheduled window is typically "now", so rule (1) would reject it. Either
the schedule's window must be shifted back by the stabilization delay, or the compatibility mode
must be limited to manual/recovery runs until that is decided. **This is a blocking open decision
(D1) and must be resolved before step 6 of the rollout.**

---

## 7. Total-field handling

### 7.1 Permitted uses

`meta.total` is **advisory**. It may be used for exactly three things:

1. **Stability check** — it must not change across the pages of one sub-window (C5).
2. **Upper bound** — accumulated rows must never exceed a present, valid `total`
   (`PAGINATION_COMPAT_TOTAL_EXCEEDED`).
3. **Post-termination reconciliation** — after the loop ends, compare `rows_total` against `total`
   (§7.3).

It must never be the sole pagination authority, must never compute a page count, and must never
extend a loop that the short-page rule has ended.

### 7.2 Cases

**D5 is resolved.** `docs/16_telematics_d5_total_policy_decision.md` is the accepted operator decision
(**Option B — absent `total` permitted under data invariants**, ACCEPTED 2026-08-03). The table below
is the normative form of that decision; §7.2 and §8 no longer contain an open question, and the
taxonomy names below are the accepted ones.

| Case | Behaviour |
|---|---|
| Absent (`meta` missing, or no `total` key) | **Permitted, and not a safety incident.** Termination relies solely on the short-page rule; reconciliation is skipped; log `total_present=false` and `total_reconciliation="absent"`. No `PAGINATION_COMPAT_TOTAL_ABSENT` code exists or may be created (`docs/16_…` §5.1) |
| Present but not an integer (string, float, null, bool, object) | `PAGINATION_COMPAT_TOTAL_INVALID`. A numeric string is *not* coerced: the strict path's tolerant `int()` coercion exists for `current_page`/`last_page` and must not be extended to a field used as a correctness oracle |
| Negative | `PAGINATION_COMPAT_TOTAL_INVALID` |
| Changes between pages, or appears/disappears mid-sub-window | `PAGINATION_COMPAT_TOTAL_UNSTABLE` — the result set moved under the read; this is exactly the §6.2 skip risk becoming observable |
| Lower than accumulated unique rows | `PAGINATION_COMPAT_TOTAL_EXCEEDED` — either duplication the overlap check missed, or a wrong `total` |
| Higher than accumulated unique rows at termination (short **or** empty page) | `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED` — the strongest available signal that the short-page hypothesis (U2) is wrong for this window |
| Zero with a non-empty first page | `PAGINATION_COMPAT_TOTAL_EXCEEDED` (accumulated rows exceed `0`) |
| Zero with an empty first page | Success: zero-row termination, reconciliation exact (`0 == 0`) |
| Implausibly large | **No dedicated code.** Under the accepted decision `total` has no control power, so an oversized advisory value cannot extend a loop or derive a page count; the row, page, request, byte and elapsed budgets bound the fetch and §7.3 reconciliation catches the disagreement. `PAGINATION_COMPAT_TOTAL_IMPLAUSIBLE` is withdrawn (`docs/16_…` §5.3) |

### 7.3 Reconciliation

On termination, when `total` is present and valid:

- `rows_total == total` → `total_reconciliation = "exact"`; proceed.
- `rows_total < total` → abort `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED`. Do **not**
  keep requesting pages to "close the gap"; the gap means the two channels disagree and the fetch
  is not trustworthy.
- `rows_total > total` → abort `PAGINATION_COMPAT_TOTAL_EXCEEDED`.

When `total` is absent, reconciliation is skipped and `total_reconciliation = "absent"`; that is a
successful outcome, not a degraded one (`docs/16_…` §5.1). A full page never terminates, so a
sub-window whose row count is an exact multiple of the requested limit legitimately costs one extra
request that returns an empty page (`docs/16_…` §5.2).

For the observed window this predicts: limit 25 → pages of 25, 25, 8 → `rows_total = 58 = total`
→ exact. That prediction is precisely what the §12 probe tests.

---

## 8. Failure taxonomy

All codes are `TelematicsProviderSafetyError` codes, uppercase snake, matching the existing
convention (`PAGINATION_LOOP`, `MAX_PAGES_PER_SUBWINDOW`, …). The `PAGINATION_COMPAT_` prefix keeps
new failures distinguishable from strict-path failures in `logs.context.abort_code`.

**Accepted `total` taxonomy.** The four `total` codes below are fixed by
`docs/16_telematics_d5_total_policy_decision.md` §5.3. The earlier spellings
`PAGINATION_COMPAT_TOTAL_CHANGED`, `PAGINATION_COMPAT_ROWS_EXCEED_TOTAL` and
`PAGINATION_COMPAT_SHORT_PAGE_INCONSISTENT_WITH_TOTAL` are superseded historical names with identical
meanings and severities; `PAGINATION_COMPAT_TOTAL_IMPLAUSIBLE` is withdrawn; and
`PAGINATION_COMPAT_TOTAL_ABSENT` must not be created.

| Code | Trigger | Retryable | Severity | `suspected_bug`? | Operator action | DB writes |
|---|---|---|---|---|---|---|
| `PAGINATION_COMPAT_PAGE_REPEATED` | Ordered or unordered identity digest seen before | No | Critical | Yes — provider contract | Disable client → `strict_meta`; escalate (§15) | None |
| `PAGINATION_COMPAT_PAGE_OVERLAP` | Any identity seen on an earlier page | No | Critical | Yes | Disable; escalate; suspect mutation (§6) | None |
| `PAGINATION_COMPAT_DUPLICATE_IN_PAGE` | Duplicate identity inside one page | No | Critical | Yes | Disable; escalate | None |
| `PAGINATION_COMPAT_IDENTITY_MISSING` | Row without usable `trip_id` | No | Critical | Yes | Disable; escalate with row index only | None |
| `PAGINATION_COMPAT_TOTAL_UNSTABLE` | `total` differs between pages, or appears/disappears mid-sub-window | Yes, once, later | High | Yes | Retry the window later; if persistent, widen stabilization delay (D1) | None |
| `PAGINATION_COMPAT_TOTAL_INVALID` | `total` present but not a non-negative JSON integer | No | High | Yes | Escalate | None |
| `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED` | Short or empty page but `rows_total < total` | Yes, later | Critical | Yes | **Stop rollout** — U2 is likely false | None |
| `PAGINATION_COMPAT_TOTAL_EXCEEDED` | Accumulated unique rows above `total`, including any rows with `total == 0` | No | Critical | Yes | Disable; escalate | None |
| `PAGINATION_COMPAT_ROWS_EXCEED_LIMIT` | `len(data) > requested_limit` | No | Critical | Yes | Disable; escalate — the data channel is no longer honouring `limit` | None |
| `PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED` | Sub-window row cap | No | Medium | No | Narrow the window / raise chunking granularity | None |
| `MAX_REQUESTS_PER_SUBWINDOW` / `_ENDPOINT` / `_RUN` | Existing request budgets | No | Medium | No | Existing §5.4 runbook | None |
| `MAX_PAGES_PER_SUBWINDOW` | Existing page budget | No | Medium | No | Existing runbook | None |
| `PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED` | Per-response or per-sub-window byte cap | No | Medium | No | Reduce `limit`; investigate payload growth | None |
| `PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED` | Sub-window wall-clock cap | Yes, later | Low | No | Retry off-peak | None |
| `PAGINATION_COMPAT_SHAPE_UNSTABLE` | Structural drift between pages | No | High | Yes | Escalate | None |
| `PAGINATION_COMPAT_WINDOW_INELIGIBLE` | Window too recent / rules in §6.4 | Yes, later | Low | No | Re-run after the stabilization delay | None |
| `PAGINATION_COMPAT_CONFIG_INVALID` | Compatibility budget ENV is not a positive integer, or `TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES` is above the 64 MiB ceiling. Validated once, **before the first request** | No | Medium | No | Fix the ENV on the host; no provider request was issued | None |
| `PAGINATION_NON_PROGRESS` | Existing code, reused: the compatibility loop failed to advance the requested page by exactly one | No | Critical | Yes | Escalate — internal state defect | None |
| `HTTP_ERROR`, `HTTP_RETRY_EXHAUSTED`, `MALFORMED_RESPONSE` | Existing transport/shape errors | Per existing policy | Existing | No | Existing runbook | None |

**Implementation note (C7, 2026-08-03).** `PAGINATION_COMPAT_CONFIG_INVALID` is the only classification the
C7 implementation added beyond the list above; it is required because an invalid compatibility budget must
fail closed and no existing code carries that meaning. Two failure classes that this table deliberately
merges are distinguished in the sanitized abort context rather than by a second code, so no failure ever
has two active names:

| Code | Context discriminator | Values |
|---|---|---|
| `PAGINATION_COMPAT_IDENTITY_MISSING` | `identity_defect` | `missing` (absent or `null` `trip_id`) / `malformed` (boolean, non-integral or non-coercible `trip_id`) |
| `PAGINATION_COMPAT_PAGE_REPEATED` | `repeat_kind` | `ordered_fingerprint` (identical ordered identity sequence) / `unordered_identity_set` (same identity set in another order) |

**Invariant across the whole table: every failure before completion results in no
business-data commit for that sub-window — and, because of §9, for the entire run.**

`suspected_bug` reports use `client.report_suspected_bug(...)` and are subject to the existing
sanitizer in `api/suspected_bug.py` (`docs/06_security.md`). Pagination evidence attached to a
report must carry HMAC digests and counts, not raw trip identities (§10.3).

---

## 9. Transaction and write boundary

### 9.1 Current flow (verified in code, unchanged by this design)

1. `run()` resolves config, builds the provider client and the `ProviderRunBudget`.
2. `_fetch_trips_in_chunks` fetches `/trips` for every chunk; every row lives **only in memory**
   (`all_trips`). A `TelematicsProviderSafetyError` in any chunk is re-raised with chunk context and
   ends the run.
3. Trip rows are parsed into `trips_parsed` (memory).
4. `/vehicles/events` enrichment runs (memory).
5. Only then, at `sync_trips_and_speeding.py:4608`, is the client-business connection opened:
   `conn = _client_business_pg_conn(cfg)`.
6. All `executemany` upserts and the speeding-bucket `UPDATE` run inside one implicit transaction
   (`psycopg` default `autocommit=False`).
7. `conn.commit()` at line 4883 — **the only commit**.
8. `finally: conn.close()` at line 4924 — an exception before the commit closes the connection with
   the transaction open, so PostgreSQL rolls it back.

### 9.2 Specification for compatibility mode

- **Rows stay in memory** for the entire fetch. No page, no sub-window and no chunk is ever
  streamed to the database.
- **All pages of a sub-window must pass every §5 invariant before that sub-window's rows are even
  released to the caller.** A partially validated sub-window is discarded, not returned.
- **All sub-windows and all chunks must complete before any write begins.** This is already true
  and must be preserved: the connection is opened only after fetching and enrichment.
- **The single-commit boundary is preserved.** One transaction, one `conn.commit()`, no
  intermediate commits, no per-chunk commit, no savepoints.
- **Rollback:** any exception between opening the connection and the commit leaves the transaction
  uncommitted; `conn.close()` in the `finally` block discards it. Any exception *before* the
  connection is opened cannot write by construction.
- **Partial writes are prevented** by the combination of (a) fetch-before-connect ordering and
  (b) a single commit. Compatibility mode must not introduce a streaming/incremental write path;
  doing so would make a mid-fetch pagination abort leave half a window ingested, which is exactly
  the corruption this design exists to prevent.
- **Strengthening (proposed, non-blocking):** before the commit, assert that the prepared upsert
  rows contain no duplicate `(client_id, provider_trip_id)`. Today the `ON CONFLICT` clause would
  silently collapse an in-batch duplicate; in compatibility mode an in-batch duplicate means a
  cross-sub-window overlap that §5.3 could not see (C1 is per sub-window), and it should abort
  before the commit rather than be absorbed.

### 9.3 What is explicitly not proposed

No change to `overwrite_existing` semantics, no change to the `ON CONFLICT` targets, no new
staging table, no two-phase commit, no cross-database transaction.

---

## 10. Observability

### 10.1 Per-page structured log

One `INFO` record per page via
`client.log("INFO", "SCRIPT", JOB_SOURCE, "telematics_trips_compat_page", run_id=run_id, context={…})`,
with flat scalar keys per `CONVENTIONS.md` §8:

| Key | Meaning |
|---|---|
| `trips_pagination_mode` | `strict_meta` / `data_invariants_v1` |
| `endpoint` | `/trips` |
| `sub_window` | existing `sub_window_label` |
| `requested_page` | the page actually requested |
| `requested_limit` | the limit actually requested |
| `returned_count` | `len(data)` |
| `accumulated_count` | rows accumulated in this sub-window so far |
| `page_identity_fingerprint` | first 16 hex chars of the ordered identity digest |
| `page_identity_set_fingerprint` | first 16 hex chars of the unordered identity digest |
| `unique_identity_count` | distinct identities on this page |
| `overlap_count` | identities shared with earlier pages (must be 0) |
| `meta_total_value` | `total` as received (integer or `null`) |
| `meta_total_type` | JSON type name of `total` |
| `meta_current_page`, `meta_per_page`, `meta_last_page`, `meta_from`, `meta_to` | **diagnostics only**, never control flow |
| `requests_remaining_run`, `requests_remaining_subwindow`, `pages_remaining_subwindow` | budget headroom |
| `response_bytes` | bounded body size |
| `elapsed_seconds` | request duration |
| `termination_reason` | on the final page: `short_page` / `empty_page` / `abort:<code>` |

Plus one sub-window summary record: pages fetched, rows accumulated, `total`,
`total_reconciliation` (`exact` / `absent` / `mismatch`), termination reason, elapsed, request
count.

### 10.2 Never logged

Raw `provider_trip_id` values; registrations; coordinates; addresses; geofence names; driver names,
surnames, tags or restrictions; raw request or response payloads; `Authorization` headers, cookies,
credential-bearing URLs, secret refs or secret values; full DSNs.

### 10.3 Identity evidence: HMAC and redaction

Following the model already implemented in `ops/diagnose_telematics_trips_pagination.py`:

- A 32-byte salt is generated **per job execution** with `secrets.token_bytes(32)`, held in memory
  only, never persisted, never logged.
- Identity digests are `HMAC-SHA256(salt, "provider_trip_id\x1f<int>")`.
- Only the first 16 hex characters of any digest are emitted, and only as page/set fingerprints
  and counts — never as a per-row list.
- Because the salt changes per execution, digests are not correlatable across runs and not
  reversible by an observer of the logs. Within one run they are sufficient to prove or disprove
  overlap, which is all the invariants need.
- Counts (`unique_identity_count`, `overlap_count`, `returned_count`) are non-identifying and are
  logged in the clear.
- `suspected_bug` payloads follow the same rule: digests and counts only. The existing sanitizer's
  allowance for a bounded sample of `provider_trip_id` values (`docs/06_security.md`) must **not**
  be used for pagination-overlap evidence, because the overlap is provable from digests alone.

---

## 11. Feature rollout

### 11.1 Stages

1. **Implement behind disabled configuration.** Migration `055_*` lands with every existing row set
   to `strict_meta`. The state machine ships unused. Production behaviour is byte-identical.
2. **Tests and offline fixtures.** The full §13 matrix passes with recorded/synthetic payloads. No
   network. This gate is mandatory before any live step.
3. **Bounded page-3 diagnostic.** The §12 probe, separately authorized, two GET requests, no retry.
   Its outcome decides whether U1/U2 hold. **A failed or ambiguous probe stops the rollout here.**
4. **One client in dry-run / compare-only mode.** `DELTA00001` stays `strict_meta`; the run uses
   `trips_pagination_compare_only=true` (§3.8) so the invariants are evaluated and logged while the
   strict abort still governs ingestion. Review the logged decisions.
5. **One closed-window manual validation.** `DELTA00001` set to `data_invariants_v1`; a single
   manual `ops/runner.py` invocation over one closed historical window that is known to require
   more than one page. Verify row counts, `total` reconciliation, zero overlap, and the ingested
   `client_trips` rows against an independent count.
6. **One scheduled client.** Enable for `DELTA00001`'s scheduled `trips_sync` only, after D1 (window
   eligibility vs. dispatcher windows) is resolved. Observe several consecutive fires.
7. **Broader rollout only after evidence review.** Each additional client is an explicit decision
   with its own review of that client's fleet size, window length and observed page behaviour.
   There is no "enable for all" step in this plan.

### 11.2 Rollback

- **Primary:** set the client back to `strict_meta` (§3.6). Effective on the next run start. No
  deploy, no restart.
- **Schema:** no migration rollback. The column is additive and inert when every row is
  `strict_meta`. Per `CONVENTIONS.md` §12 an applied migration is never edited; a down-migration is
  proposed only if a schema change turns out to be unavoidable, which it is not for this design.
- **History:** existing terminal `FAILED` rows in `workflow_a_control.client_schedule_run_history`
  and in `runs` remain immutable. They are the audit record of the incident.
- **No automatic retries of historical fires.** The dispatcher considers only the latest fire per
  schedule and has no multi-fire catch-up (`CURRENT_TASK_CONTEXT.md` §5.6). Recovering a missed
  window is a separately authorized manual action (§14), never an automatic consequence of
  enabling the mode.

### 11.3 Rollback triggers

Any of: a `PAGINATION_COMPAT_PAGE_OVERLAP`, `_PAGE_REPEATED`, `_ROWS_EXCEED_LIMIT`,
`_TOTAL_EXCEEDED` or `_TOTAL_RECONCILIATION_FAILED` abort in production; a duplicate
`(client_id, provider_trip_id)` detected in the pre-commit assertion (§9.2); an unexplained
divergence between ingested row counts and independent counts; or the provider changing `/trips`
behaviour again. (Open decision D9 fixes the exact automatic-vs-manual trigger policy.)

---

## 12. Required page-3 validation

### 12.1 Does the current diagnostic support pages 2 and 3?

**Yes — no tool change is required.**

`ops/diagnose_telematics_trips_pagination.py:_parse_pages` accepts any comma-separated page numbers
in `1..MAX_PAGE_NUMBER (1000)`, rejects duplicates, and caps the list at
`MAX_PAGES_PER_EXECUTION = 2`. `build_plan` additionally requires exactly
`LIVE_REQUIRED_PAGE_COUNT = 2` pages for a live run. Nothing pins the pages to `1,2`:
`compare_pages` is called with `plan.pages[0]` and `plan.pages[1]`, both pages are summarized with
the same in-memory salt allocated once per execution in `run_live`, and the arbitrary-page case is
already covered by `ops/tests_manual/test_telematics_trips_pagination_diagnostic.py`, which asserts
`make_plan(pages="3,7").pages == (3, 7)`.

Consequently `--pages 2,3` is a valid live invocation today, uses exactly two GET requests, and
produces `page_2_summary.json`, `page_3_summary.json` and `cross_page_comparison.json` with
`first_page: 2`, `second_page: 3`.

Two cosmetic limitations, neither blocking, both to be handled by reading the bundle rather than by
changing the tool:

- The result classification vocabulary is page-agnostic. A disjoint 25/8 pair reports
  `TELEMATICS_TRIPS_PAGES_DISTINCT_METADATA_BROKEN`, the same string the 1/2 probe produced. The
  page numbers are in `diagnostic_summary.json.requested_pages` and in the comparison document.
- The tool does not itself assert `total` stability across the two pages. The values are recorded
  per page in `page_N_summary.json → pagination_meta.fields.total.value`, so the check is a manual
  comparison of two numbers in the bundle.

If a future task wants those automated, the smallest safe change would be an
assertion-only addition (expected counts, expected intersection, expected constant `total` supplied
as optional CLI expectations, surfaced as a boolean in the summary) that changes no request
behaviour, no budget and no page selection. **That change is not made here and is not required for
the probe.**

### 12.2 Proposed future probe — do not execute

Separately authorized, single execution, exactly two GET requests, no retry, one shared in-memory
HMAC salt, dry-run first.

```bash
# STEP 1 — dry run (no socket, no credentials, no control-plane read)
PYTHONPATH="$PWD" .venv/bin/python ops/diagnose_telematics_trips_pagination.py \
  --client-code DELTA00001 \
  --start-timestamp "2026-07-28 16:00:00" \
  --end-timestamp "2026-07-28 17:00:00" \
  --pages 2,3 \
  --limit 25 \
  --include-private true \
  --output-dir /var/tmp/telematics-pagination-evidence/<UTC-timestamp>-pages-2-3

# STEP 2 — review request_plan.json, then re-run the identical command with:
#   --allow-live-request
# and WITHOUT --allow-timeout-retry, so the request budget is exactly 2.
```

Expected results if U1/U2 hold:

| Assertion | Expected |
|---|---|
| `page_2_summary.json → data_identity.row_count` | 25 |
| `page_3_summary.json → data_identity.row_count` | 8 |
| `cross_page_comparison.json → intersection_count` | 0 |
| `cross_page_comparison.json → verdict` | `DISTINCT_CONTINUATION` |
| `identity_contract_resolved` on both pages | `true` |
| `duplicate_identity_count` on both pages | 0 |
| `pagination_meta.fields.total.value` on both pages | 58 |
| `diagnostic_summary.json → request_count` | 2 |
| Result classification | `TELEMATICS_TRIPS_PAGES_DISTINCT_METADATA_BROKEN` |

Interpretation:

- **25 / 8 / disjoint / total 58** → U1 and U2 supported for this window; rollout may proceed to
  step 4.
- **25 rows on page 3** → the provider is not slicing as assumed; the short-page rule is wrong;
  **stop**, escalate, do not implement termination on `len(data) < limit`.
- **0 rows on page 3** → short-page termination is untested but empty-page termination works; a
  further probe with a window whose row count is not a multiple of the limit is required.
- **Any intersection > 0** → C1 would have aborted in production; the compatibility mode is not
  viable for this provider behaviour; escalate.

**This probe must not be executed as part of the present task.**

---

## 13. Test strategy

Repository testing convention (`CONVENTIONS.md` §11): no CI, no pytest config; focused manual
scripts in `ops/tests_manual/` run by hand. New tests follow that pattern — a single
`ops/tests_manual/test_telematics_trips_pagination_compat.py` with a `main()` that runs every case
against a fake session, no network, no database, no secrets. **No new third-party dependency is
introduced for this document; property-based sequence generation is done with the standard library
`random`/`itertools` over a fixed seed, not with a new library.**

| # | Case | Expected |
|---|---|---|
| T1 | `strict_meta` against the current broken metadata | `PAGINATION_MISMATCH`, byte-identical to today |
| T2 | `strict_meta` against a healthy legacy payload | Unchanged multi-page success |
| T3 | Mode absent / `NULL` / unknown string in config | Resolves to `strict_meta` |
| T4 | Two-page continuation, limit 25, 25+25, then short 8 | Success, 58 rows, `total_reconciliation=exact` |
| T5 | Three-page continuation with a short final page | Success, correct order and count |
| T6 | Short first page (58 rows, limit 1000) | Success in one request |
| T7 | Empty first page | Success, zero rows, one request |
| T8 | Full page followed by an empty page | Success; termination on the empty page |
| T9 | `total` absent (multi-page and single-page) | Success; `total_reconciliation=absent`; termination by short page |
| T9a | Empty first page with `total` absent | Success; zero rows; one request |
| T9b | Empty first page with `total == 0` | Success; zero rows; reconciliation exact |
| T9c | Row count an exact multiple of the limit, `total` equal to it | Success; the full page does **not** terminate; the next page is empty and terminates; reconciliation exact |
| T10 | `total` changes between pages, and `total` present then absent mid-sub-window | `PAGINATION_COMPAT_TOTAL_UNSTABLE` |
| T11 | `total` non-integer (bool, float, numeric string, object, array) / negative | `PAGINATION_COMPAT_TOTAL_INVALID` |
| T11a | `total == 0` with non-empty `data` | `PAGINATION_COMPAT_TOTAL_EXCEEDED` |
| T12 | Short or empty page with `rows_total < total` | `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED` |
| T12a | Accumulated unique rows above `total` | `PAGINATION_COMPAT_TOTAL_EXCEEDED` |
| T13 | Duplicate page (identical payload) | `PAGINATION_COMPAT_PAGE_REPEATED` |
| T14 | Same identity set, different order | `PAGINATION_COMPAT_PAGE_REPEATED` |
| T15 | Same identity set, cosmetic field changed | `PAGINATION_COMPAT_PAGE_REPEATED` (identity-based fingerprint) |
| T16 | Partial overlap (1 shared identity of 25) | `PAGINATION_COMPAT_PAGE_OVERLAP` |
| T17 | Duplicate identity within one page | `PAGINATION_COMPAT_DUPLICATE_IN_PAGE` |
| T18 | Identity repeated across non-adjacent pages (1 and 4) | `PAGINATION_COMPAT_PAGE_OVERLAP` — proves full-history tracking |
| T19 | A/B/A/B alternation | Aborts on the third page |
| T20 | Page returns more rows than the limit | `PAGINATION_COMPAT_ROWS_EXCEED_LIMIT` |
| T21 | Missing / null / boolean / non-integral `trip_id` | `PAGINATION_COMPAT_IDENTITY_MISSING` |
| T22 | `data` not a list; row not an object; `meta` type changes mid-fetch | `MALFORMED_RESPONSE` / `PAGINATION_COMPAT_SHAPE_UNSTABLE` |
| T23 | Page budget exhausted (51 full pages) | `MAX_PAGES_PER_SUBWINDOW`, no write |
| T24 | Request budget exhausted | `MAX_REQUESTS_PER_SUBWINDOW` / `_RUN`, no write |
| T25 | Row / byte / elapsed budgets exhausted | Corresponding `PAGINATION_COMPAT_*` abort |
| T26 | Transport timeout on page 2 | Bounded retry per existing policy, then `HTTP_RETRY_EXHAUSTED` |
| T27 | Retry never fires for a pagination or shape anomaly | Immediate abort, request count unchanged |
| T28 | Closed eligible window vs. window ending "now" | Success vs. `PAGINATION_COMPAT_WINDOW_INELIGIBLE` |
| T29 | Ineligible window does not fall back to `strict_meta` | Abort code is the eligibility code |
| T30 | Abort on the last sub-window of a multi-chunk run | Zero rows in the client DB; transaction rolled back |
| T31 | Abort after the connection is opened but before commit | Zero rows; connection closed with the transaction open |
| T32 | Duplicate `(client_id, provider_trip_id)` in the prepared batch | Pre-commit assertion aborts (§9.2) |
| T33 | Log capture across every case | No raw trip ID, registration, coordinate, address, driver name, payload or credential appears in any emitted context |
| T34 | Salt handling | Salt never appears in any log; digests differ between two executions over identical data |
| T35 | Generated sequences (fixed seed, stdlib only): random page-size sequences, random insert/delete perturbations, random duplicate injection | Every sequence either succeeds with a complete, duplicate-free row set, or aborts — never a silent partial success |

T35 is the property test that matters most: *for any generated page sequence, the state machine
never returns a row set containing a duplicate identity, and never returns a partially validated
sub-window.*

---

## 14. Backfill and recovery

### 14.1 Ordered sequence after a future fix

1. **Verify compatibility against the provider** — the §12 page-3 probe, separately authorized.
2. **Deploy disabled** — migration `055_*` plus the state machine, every client `strict_meta`.
   Verify production behaviour is unchanged.
3. **Enable only for approved clients** — start with `DELTA00001` (§11 steps 4–6).
4. **Prove a current scheduled run** — at least one scheduled `trips_sync` fire must complete
   `SUCCESS` with the expected row counts before any historical work begins. Recovering the past
   with an unproven path would multiply the damage.
5. **Inventory missing windows** — read-only. For each affected client determine, from
   `workflow_a_control.client_schedule_run_history` and `runs`, which fires failed and which never
   ran, and translate them into concrete `[window_start_ts, window_end_ts]` ranges. Produce a
   reviewed inventory document; change nothing.
6. **Prepare dry-run recovery** — for each range, a manual `ops/runner.py` invocation plan. Prefer
   the existing manual repair path documented in `docs/05_jobs.md` (`insert_only=true`, explicit
   `client_id`, `client_code`, `window_start_ts`, `window_end_ts`) over a plain re-run where the
   goal is to fill gaps without touching existing rows.
7. **Review deduplication and row counts** — the upsert is keyed on
   `(client_id, provider_trip_id)` and is idempotent, so a range that overlaps already-ingested
   data is safe by construction; nevertheless the expected insert/update counts are reviewed before
   execution, and `overwrite_existing` semantics are decided explicitly per range.
8. **Execute separately authorized backfills** — one range at a time, each its own authorization,
   each verified before the next. Backfill windows are by definition closed historical windows, so
   they are the *best* case for §6.4 eligibility.
9. **Verify downstream** — `client_vehicle_daily_fuel` / `client_vehicle_driver_daily_fuel`
   aggregates, Eco Driving weekly/monthly stats and rankings, any snapshot that consumed the gap
   period, and the mailing dependencies described below.

### 14.2 Explicit separation of cases

| Case | State | Action |
|---|---|---|
| **Failed 2026-08-01 fire rows** | Terminal `FAILED` rows exist in `client_schedule_run_history` and `runs`, with `PAGINATION_MISMATCH` in `logs.context.abort_code` | **Immutable.** Do not update, delete, re-status or "retry" the history row. They are the incident record. Data recovery for their window happens through a *new* manual run, which creates its own new run row |
| **Missing 2026-07-30 and 2026-07-31 fire rows** | No history row at all — the dispatcher considers only the latest fire per schedule and performs no catch-up | These windows will never be picked up automatically. They exist only in the §14.1 step 5 inventory and are recovered by explicit, separately authorized manual runs |
| **Future scheduled fires** | Not yet fired | Unaffected by any backfill. They begin succeeding only once the client is `data_invariants_v1` **and** D1 (window eligibility vs. dispatcher window) is resolved. Until then they continue to fail closed, which is correct |
| **BRAVO00016 weekly behavior** | A distinct client and a distinct dataset family (Eco Person weekly aggregation + weekly email, `BRAVO_ECO_WEEKLY_EMAIL_*`) | Two separate concerns: (a) if BRAVO00016's `trips_sync` is also affected, its trip gaps must be backfilled **before** the weekly aggregation period is finalized, or the weekly stats are computed on incomplete distance/event data; (b) weekly emails are **not** a recovery mechanism — sends are idempotent by design and a delivered message must never be re-sent to "correct" it; `archive_only=true` exists solely to append an already-sent MIME to the Sent folder. If a weekly email has already gone out over incomplete data, that is a communication issue to be handled by the operator, not by re-running the sender |

### 14.3 Prohibited

Modifying existing terminal history rows; deleting `client_trips` rows to "clean up" before a
backfill; automatic catch-up of missed fires; re-sending Eco Driving emails as a recovery step;
backfilling before step 4 has proven a current scheduled run.

---

## 15. Provider escalation

Compatibility mode is an **operational mitigation for our side of the integration. It does not
replace, delay or reduce the priority of provider escalation.** The provider's `/trips` response
still violates its own published contract, and every day it remains broken is a day the ingestion
path depends on undocumented behaviour that can change again without notice.

### 15.1 Evidence package contents

1. **Sanitized request parameters** — endpoint `GET /trips`; `start_timestamp`, `end_timestamp`,
   `incl_private`, `page`, `limit` exactly as sent; the sanitized origin (scheme + host + optional
   port + base path), with any URI userinfo stripped. No credentials, no `Authorization` header,
   no cookies.
2. **Incorrect metadata** — the full `meta` block as received for each request, which contains no
   customer data: `current_page`, `per_page`, `last_page`, `from`, `to`, `total`, plus the
   observation that the two limit-25 blocks were byte-identical.
3. **Row counts** — 58/0 for limit 1000; 25/25 for limit 25; 0 duplicates within pages.
4. **Disjointness evidence** — that page 1 and page 2 shared zero trip identities, expressed as
   HMAC-SHA256 digests under a per-execution salt plus intersection counts and ratios. No raw trip
   IDs.
5. **Timestamps** — UTC execution timestamps of each request, the closed window under test, and the
   date range over which the behaviour appeared (2026-07-29 … 2026-08-01).
6. **HTTP statuses** — status code, `content-type`, `content-length` and response byte length per
   request.
7. **Correlation IDs when available** — `x-request-id`, `x-correlation-id`, `x-trace-id`,
   `traceparent`, `cf-ray`, and any rate-limit headers, taken from the diagnostic's header
   allowlist.
8. **OpenAPI inconsistency** — `docs/openapi.yaml` `#/components/schemas/pagination` requires
   `from`, `to`, `current_page`, `per_page`, `last_page`, `total` as integers describing the
   response; the observed `per_page: 10` matches the documented *default* of
   `limit_without_validation` rather than the requested `limit`, and `last_page: 6` equals
   `ceil(total / 10)`. State this as an observation, not as a diagnosis of their implementation.
9. **No customer trip payload** — no `trip_id`, registration, VIN/chassis, coordinates, addresses,
   geofence names, driver names, tags or timestamps of individual trips.

### 15.2 Asks

Confirm whether the `page` parameter is still applied to `data`; state the intended semantics of
`per_page`/`current_page`/`last_page`/`from`/`to` under an explicit `limit`; give a fix date; and
state whether the behaviour is region- or account-specific (U6).

---

## 16. Security and privacy

| Area | Assessment |
|---|---|
| **Credentials** | Unchanged. `workflow_a_control.client_account` stores only refs (`provider_basic_auth_password_secret_ref`); `secret_resolver.resolve_secret` reads them as late as possible in the job process. The new `trips_pagination_mode` column is non-secret configuration. No credential enters logs, `runs.params`, evidence or the escalation package |
| **Personal trip data** | Trips contain registrations, driver names, tags, coordinates and addresses — personal data. Compatibility mode adds no new storage of it: rows live in process memory and are written only to the client business DB exactly as today. Observability is digest-and-count only (§10.2, §10.3) |
| **Evidence retention** | Diagnostic bundles live outside the repository working tree (enforced by `OUTPUT_DIR_INSIDE_REPOSITORY`) under an operator-chosen path such as `/var/tmp/telematics-pagination-evidence/<UTC>`. They contain no raw payload and no credential, but they do contain operational metadata; retain only as long as the incident and the provider escalation need them, then delete |
| **Filesystem permissions** | Evidence files `0600` inside a `0700` directory, written via `os.open(..., O_CREAT|O_TRUNC, 0o600)` with an explicit `chmod`, symlinked directories rejected. Unchanged by this design |
| **Log redaction** | Provider-error contexts already restrict response headers to a fixed allowlist and truncate bodies. Compatibility-mode contexts add no free-form provider text — only enumerated scalars and digests |
| **Provider escalation redaction** | §15 — sanitized parameters, metadata and digests only; no customer payload; no credentials |
| **Per-execution HMAC salts** | 32 bytes from `secrets.token_bytes`, in memory for the lifetime of one process, never persisted, never logged, never reused across runs. Cross-run correlation of trip identities from logs is therefore impossible by construction |
| **Operator access** | Enabling the mode requires write access to the platform control-plane database, which is already the highest-privilege operational action in Workflow A. No new privilege, role, grant or sudo boundary is introduced. The disable path requires the same access, so a compromised operator account is not made more dangerous by this feature — but it is also not made safer, which is why §11 requires ticketed, reviewed enablement |

No new secret, no new network listener, no new privileged helper, no new systemd unit, no change to
the environment-identity boundary.

---

## 17. Open decisions

| ID | Decision | Proposed default | Alternatives | Evidence required | Status |
|---|---|---|---|---|---|
| D1 | Stabilization delay, and how it reconciles with dispatcher windows that end at "now" | 180 minutes; scheduled windows shifted back by the delay | 24 h; compatibility mode restricted to manual/recovery runs only; provider-confirmed settlement time | Provider statement on when a window stops changing; observed `total` stability across repeated reads of the same recent window | **Resolved at design level** by `docs/13_telematics_trips_stabilization_windows.md`; see the note below. Implementation still blocked. |
| D2 | Page limit in compatibility mode | 1000 (unchanged `TELEMATICS_PROVIDER_PAGE_LIMIT`) | 25, to match the window where multi-page behaviour is proven; 100 as a compromise | Page-3 probe (§12); whether larger limits still slice correctly | Non-blocking for steps 1–5; **blocking** for step 6 |
| D3 | Page budget per sub-window | 50 (existing `MAX_PAGES_PER_SUBWINDOW`) | Lower dedicated compat cap (e.g. 20) | Observed page counts for the largest enabled fleet | Non-blocking |
| D4 | Request budget | Existing 500 / 300 / 80, unchanged | A dedicated, lower compat sub-budget | Request counts from steps 4–6 | Non-blocking |
| D5 | Treatment of a missing `total` | Permit; terminate on short page; skip reconciliation | *(rejected)* abort with `PAGINATION_COMPAT_TOTAL_ABSENT` — that code must not be created | Whether `/trips` ever omits `total`; the OpenAPI spec marks it required | **RESOLVED — ACCEPTED 2026-08-03: Option B (permit).** `docs/16_telematics_d5_total_policy_decision.md`. No longer blocking; C7 is unblocked |
| D6 | Is page-3 validation mandatory before enablement? | **Yes, mandatory** | Enable on the 1/2 evidence alone | — (this is a policy decision, not an evidence question) | **Blocking** |
| D7 | Initial client allowlist | `DELTA00001` only | DELTA00001 + one larger fleet; all Telematics clients | Per-client fleet size, window length, observed metadata behaviour (U6) | **Blocking** for step 7 |
| D8 | Compare-only / dry-run mode (§3.8) | Implement it — it makes rollout step 4 possible without ingesting | Skip it; go straight from tests to a live enabled run | Reviewer judgement on rollout risk | Non-blocking |
| D9 | Rollback trigger policy, and whether an immutable audit row is required for the switch itself | Manual rollback on any critical abort in §11.3; no extra audit table (run logs carry the mode) | Automatic self-disable on the first critical abort; dedicated `workflow_a_control` audit table | Whether an auto-disable could mask a systematic problem by silently reverting | Non-blocking, but must be answered before step 7 |

### 17.1 D1 — status note

**D1 is resolved at design level only**, by `docs/13_telematics_trips_stabilization_windows.md`. That
document selects the full-window shift (`E_end = F − D`, `E_start = (F − L) − D − O`), which makes
the §6.4 eligibility rule a theorem for every scheduled run rather than a check that scheduled runs
fail. Nothing in this document's own design is changed by it.

**The initial version of `docs/13` failed independent review on its bootstrap semantics**
(`TELEMATICS_STABILIZATION_ARCH_REVIEW_BLOCKED_BOOTSTRAP_UNSAFE`). Its window mathematics,
missed-fire expansion, transaction ordering, DST analysis and overlap model were accepted; its
coverage state was not, because a single unbounded watermark — and in particular seeding one from
the most recent `SUCCESS` schedule-history row — would have implicitly claimed contiguity back
through all client history and permanently concealed the 2026-07-30 / 2026-07-31 hole described in
§14. **The corrected bootstrap rules are `docs/13` §5.2, §5.2.1, §5.5 and §13**: coverage is an
explicitly bounded closed interval `[coverage_start_ts, covered_through_ts]` with a
`bootstrap_status`, seeded only from a reviewed evidence bundle, and compatibility mode fails closed
with `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` — before any provider request — until it is `READY`.

### 17.2 D5 — resolved

**D5 is resolved and accepted:** `docs/16_telematics_d5_total_policy_decision.md` (Option B — absent
`total` permitted under data invariants, ACCEPTED 2026-08-03). It fixes §7.2, §7.3 and the §8 `total`
taxonomy, makes short-page termination authoritative, and removes D5 as a C7 prerequisite. C7
implementation and C7 review are therefore authorized; the same decision also assigns C7 the minimum
mode-propagation runtime path (`docs/14_…` §3/C7).

### 17.3 Current gate status

**C7 implementation status (2026-08-03).** The state machine, the compatibility budgets, the accepted
D5 Option B `total` policy and the minimum mode propagation are implemented and covered by
`ops/tests_manual/test_telematics_trips_pagination_compat.py` (strict regressions, compatibility
successes and failures, propagation, and the T35 generated-sequence property). Implementation changes
**no** gate below: it is not a deployment, not an enablement, not a coverage mutation and not a
provider request.


Since the C1–C6 runtime and migrations `055`–`057` were deployed, `BRAVO00016` is the **sole**
`data_invariants_v1` client (armed, unexercised) and the other four clients remain `strict_meta`.
What still gates **enablement breadth** — never merge — is the provider-contract set §0.3 U3–U8, the
per-client bootstrap gates of `docs/13` §13, and the rollout gates of §11 and §18. The accepted D5
decision closes none of those; it removes only the D5 prerequisite on C7. Resolving D1 at design level
advanced none of them either.

---

## 18. Implementation plan

Small, individually reviewable commits. **None of them is performed by this task.**

| # | Commit | Contents | Gate |
|---|---|---|---|
| 1 | Configuration contract and validation | Migration `055_workflow_a_trips_pagination_mode.sql` (additive → backfill → default → NOT NULL → CHECK); `trips_pagination_mode` in the `control_plane` SELECT and in `ClientAccountConfig`; Python allowlist validation; fail-closed default | Migration reviewed; no behaviour change (every row `strict_meta`) |
| 2 | Provider compatibility state machine | New `_fetch_paginated_data_invariants_v1` in `provider_client.py`; `fetch_trips` dispatches on mode; `strict_meta` path untouched; new abort codes; new budgets in `provider_safety.py`; minimum mode propagation through `sync_trips_and_speeding.py` (`docs/14_…` §3/C7) | **D5 resolved** (`docs/16_…`, Option B); §5 invariants complete |
| 3 | Safety and generated sequence tests | `ops/tests_manual/test_telematics_trips_pagination_compat.py` covering T1–T35, stdlib only, no network, no DB | Full matrix green |
| 4 | Structured observability | Per-page and per-sub-window logs, HMAC identity digests, per-execution salt, mode in run context; redaction assertions in the test suite | T33/T34 green |
| 5 | Operator documentation | `docs/07_operations.md` §5.4.2 runbook (enable/disable, abort-code triage, rollback); `docs/05_jobs.md` note on the mode; `docs/02_infrastructure.md` entries for the new ENV budgets; this document linked as the design of record | Docs match code |
| 6 | Disabled deployment | Deploy commits 1–5 with every client `strict_meta`; verify unchanged production behaviour over at least one scheduled cycle | Production behaviour byte-identical |
| 7 | Page-3 validation | Execute the §12 probe under separate authorization; record the bundle; decide U1/U2 | Probe result reviewed |
| 8 | Controlled client enablement | Compare-only run → closed-window manual run → one scheduled client; per §11 steps 4–6 | D1, D2, D6, D7 resolved |
| 9 | Recovery/backfill task | Read-only inventory of failed and missing fires; dry-run recovery plans; separately authorized executions; downstream verification per §14 | Step 4 of §14.1 proven first |

---

## 19. Design decisions stated explicitly

1. **Compatibility mode is disabled by default.** `trips_pagination_mode` defaults to
   `strict_meta`, and any unknown, absent or unreadable value resolves to `strict_meta`.
2. **Strict mode remains unchanged.** `_fetch_paginated` keeps its current body, including the
   `PAGINATION_MISMATCH` abort. No client is silently migrated off it.
3. **Broken metadata is retained only for diagnostics.** `current_page`, `per_page`, `last_page`,
   `from` and `to` are logged and never influence control flow in compatibility mode.
4. **Page control comes from the requested page and from returned-data invariants** — never from
   provider pagination metadata.
5. **No page is written before the complete required validation boundary passes.** All pages of all
   sub-windows of all chunks are validated in memory before the client-business connection is
   opened, and there is exactly one commit.
6. **Any repeated or overlapping identity aborts the fetch** — full cross-page history within the
   sub-window, ordered and unordered, partial overlap included.
7. **Short-page termination is the authoritative compatibility termination signal**
   (`docs/16_…` §5.1–§5.2). Page 3 has since been observed for one closed window; the breadth of that
   evidence (U2 for larger and more recent windows) still gates per-client enablement, not the rule.
8. **`total` is advisory and consistency-checking, never authoritative.** It may not terminate the
   loop on its own and may not extend it. **An absent `total` is an accepted compatibility state**
   and never an abort (accepted D5 Option B, `docs/16_…`); a present `total` is strictly validated,
   stability-checked, bound-checked and reconciled to exact equality at termination.
9. **Rollout is per client**, staged, reversible by a single control-plane `UPDATE`, with no
   "enable everywhere" step.
10. **Provider escalation remains required.** Compatibility mode mitigates; it does not fix the
    provider's contract violation.
