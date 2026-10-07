# Telematics `/trips` compatibility pagination — D5 advisory-`total` policy decision

**Status: ACCEPTED.**
**Decision date: 2026-08-03.**
**Decision: D5 Option B — absent `meta.total` permitted under data invariants.**

This document is the separate, operator-approved architecture decision that
`docs/14_telematics_trips_compatibility_implementation_plan.md` §2.2 required before C7 (the
`data_invariants_v1` pagination state machine) could be implemented. It resolves `docs/12_…` §17
open decision **D5** and, in the same decision, corrects the C7/C8 delivery boundary so that the
future C7 implementation can propagate the selected mode through the minimum runtime path needed to
reach the state machine at all.

It is a **decision and contract artifact only**. It changes no runtime code, no test, no migration,
no schedule, no systemd unit, no client configuration and no production state. C7 is **not**
implemented by this document.

Related documents of record:

- `docs/12_telematics_trips_pagination_compatibility.md` — compatibility-mode design (§4 state
  machine, §5 invariants, §7 `total` handling, §8 taxonomy, §10 observability, §13 tests).
- `docs/13_telematics_trips_stabilization_windows.md` — stabilization/coverage domain invariants.
- `docs/14_telematics_trips_compatibility_implementation_plan.md` — commit ownership and delivery
  sequence.
- `docs/15_telematics_coverage_mutation_contract.md` — the C6/C11 coverage-mutation contract.
- `docs/05_jobs.md`, `docs/07_operations.md` — job catalog and operator runbooks.

---

## 1. Scope

This decision governs the treatment of the advisory `meta.total` field of the Telematics
`GET /trips` response **only** when the normalized trips pagination mode is exactly:

```text
data_invariants_v1
```

It does **not** apply to, and changes nothing about:

- `strict_meta` — the production default, whose behavior remains unchanged in every respect;
- an unknown, missing, `NULL` or unreadable mode — which continues to fail closed to `strict_meta`
  or to raise per the existing configuration contract (`docs/12_…` §3.2–§3.3);
- any provider endpoint other than `/trips` — `/vehicles`, `/drivers`, `/vehicles/events`,
  `/alerts/notifications` and `/fuel/*` keep their current strict handling (`docs/12_…` N7);
- coverage state, the bootstrap gate, the C6 finalizers or the C11 recovery state machine.

**There is no dynamic fallback in either direction.** Compatibility mode is never entered because
of observed provider behavior, and a compatibility failure never retries as `strict_meta`.

---

## 2. Problem statement

Between 2026-07-29 and 2026-08-01 the provider's `/trips` pagination metadata began contradicting
both the request and the response body: `meta.current_page` is pinned to `1` for every requested
page, `meta.per_page` reports the OpenAPI default `10` regardless of the requested `limit`,
`meta.last_page` is derived from that wrong page size, and `meta.from` does not advance while
`meta.to` tracks the requested limit. The data channel still works: the requested `limit` bounds
`data`, and the requested `page` selects a distinct, disjoint slice (`docs/12_…` §0.1).

The compatibility state machine therefore has to derive every control decision from returned data
rather than from metadata. That leaves one field genuinely undecided: `meta.total`. It is the only
metadata field observed to be both present and plausible, so the design uses it as an advisory
consistency oracle — but the design never settled what must happen when it is **absent**.

`docs/12_…` §7.2 proposed permitting absence and terminating on the short page, while flagging that
a reviewer might prefer to abort. `docs/12_…` §17 recorded that ambiguity as **D5**, status
*blocking*, because it changes the failure taxonomy. `docs/14_…` §2.2 escalated it to a hard
prerequisite: no C7 code could be written while D5 was open, and neither C7 implementation nor C7
review was authorized to invent the answer. This document supplies the missing decision.

---

## 3. Considered options

### Option A — absent `total` fails closed

Require a valid, non-negative integer `total` on every compatibility page; abort the sub-window with
a dedicated `PAGINATION_COMPAT_TOTAL_ABSENT` classification when the field is missing.

- **For:** maximal alignment with `docs/12_…` G4 ("fail closed"); the provider's own OpenAPI schema
  marks all six `pagination` fields as required, so absence is a contract violation; it keeps a
  second, independent completeness oracle available on every fetch.
- **Against:** it makes ingestion depend on the *one metadata field the provider has already proven
  it mishandles*. The incident is precisely a metadata-layer regression; treating another metadata
  field as mandatory reintroduces the failure class the compatibility mode exists to survive. A
  provider-side change that drops `total` — plausible while they are actively changing this
  response — would take the canary down for a reason unrelated to data correctness, and the
  operator would again face a `FAILED` run with intact, correct data behind it.

### Option B — absent `total` permitted under data invariants

Permit absence. Terminate solely on the authoritative short-page rule, and continue only while every
data-derived invariant and every budget holds. Record the absence as sanitized observability.

- **For:** it keeps control flow entirely inside the channel that is proven to work (returned
  identities and row counts), which is the stated purpose of `data_invariants_v1`. Absence removes an
  *optional cross-check*, not a *control input*, because under this design `total` never selects a
  page, never continues a loop and never terminates one. The remaining safety envelope (§6) is
  data-derived and independent of `total`.
- **Against:** without `total` there is no second oracle for the undetectable skip case
  (`docs/12_…` §6.2 delete/shift), so a silently missing row would not be caught by reconciliation.
  This is a real residual risk and is accepted explicitly in §8.

### Continued deferral

Keep D5 open and C7 blocked.

- **For:** no decision risk.
- **Against:** the incident is live. Every `/trips` scheduled fire for an affected client fails
  closed, `BRAVO00016` is an armed but unexercised canary, and the 2026-07-27 → 2026-08-03 interval
  is uncovered. Deferral does not preserve optionality; it preserves an outage.

---

## 4. Accepted option

**Option B — absent `total` permitted under data invariants.**

This is the accepted production architecture decision. It is **not** a preference, not provisional
and not a recommendation awaiting confirmation. `docs/14_…` §2.2's earlier non-binding preference for
Option A is **superseded** and must not be cited as an active rule.

### Rationale

1. **The decision follows the incident's own evidence.** The failure is metadata-layer. A mode whose
   entire premise is "derive control from data, not metadata" cannot make a metadata field
   mandatory without contradicting itself.
2. **`total` has no control power under this design.** `docs/12_…` §7.1 already restricts it to
   stability checking, an upper bound and post-termination reconciliation. Losing an advisory
   cross-check is a reduction in *evidence*, not a loss of *control*. Option A would have made the
   absence of evidence into a halt.
3. **The short-page rule is the authoritative signal and is now evidence-backed.** The page-2/3
   probe returned the expected short page with a stable `total` for the probed window
   (`TELEMATICS_PAGE3_VALIDATION_SHORT_PAGE_CONFIRMED`, `docs/13_…` §17). Termination therefore does
   not depend on `total` even when `total` is present.
4. **Fail-closed is preserved where it matters.** Every data-derived invariant in §6 still aborts
   the whole sub-window before any business write. Option B narrows exactly one condition — "the
   advisory field is missing" — and narrows nothing else.
5. **A present-but-wrong `total` is the more dangerous case, and it is tightened.** §5.2 makes a
   present `total` strictly validated, stability-checked, bound-checked and reconciled to exact
   equality at termination. Option B is deliberately *stricter* than the status quo whenever the
   field exists.

**Option B must not be described as "ignore `total` and trust the API".** It ignores `total` for
*control* and enforces it strictly for *verification* whenever the provider supplies it.

---

## 5. Exact normative rules

These rules are normative for the C7 implementation and for C7 review.

### 5.1 Absent `meta.total`

When `meta` is missing, or present without a `total` key, in `data_invariants_v1`:

- **do not** fail solely because it is absent;
- **do not** derive a page count from it;
- **do not** use it for continuation;
- **do not** use it for termination;
- **do not** invent, infer or substitute a replacement total from any other field, from
  `meta.last_page`, from `meta.per_page` or from accumulated counts;
- **do** record sanitized observability that the advisory total was absent (`total_present=false`,
  `total_reconciliation="absent"`), per `docs/12_…` §10.1;
- **do** continue only while every data-derived invariant of §6 passes;
- **do** terminate only through the authoritative short-page rule `len(data) < requested_limit`;
- **do** enforce every page, request, row, byte and elapsed budget;
- **do** enforce full-history identity uniqueness and non-progress protection.

Absence of `total` is an **accepted compatibility state, not a safety incident.** It is not a
`suspected_bug`, not an abort and not an escalation trigger.

**No machine failure code named `PAGINATION_COMPAT_TOTAL_ABSENT` may be created.**

### 5.2 Present `meta.total`

When `total` is present in `data_invariants_v1`:

- require coercion through the documented integer-like contract of `docs/12_…` §7.2 — a genuine JSON
  integer. A `bool`, a `float`, a numeric **string**, `null`, an object and an array are all
  invalid. The strict path's tolerant `int()` coercion for `current_page`/`last_page` is
  deliberately **not** extended to a field used as a correctness oracle;
- require a **non-negative** integer;
- require the **same normalized value on every page** of the sub-window. A value that changes
  between pages, and equally a `total` that appears or disappears part-way through the sub-window,
  is instability;
- require **accumulated unique rows never to exceed it**;
- **do not** derive the requested page count from it;
- **do not** terminate merely because accumulated rows equal it;
- continue until the short-page termination signal;
- at successful short-page termination require exact reconciliation:
  `accumulated_unique_rows == advisory_total`.

Because a full page never terminates, a sub-window whose row count is an exact multiple of the
requested limit costs one additional request: the loop requests the next page, receives an empty
page, terminates on it, and reconciles `accumulated_unique_rows == advisory_total`. That extra
request is intended and is bounded by the existing per-sub-window request and page budgets.

### 5.3 Approved failure classifications

The compatibility `total` taxonomy is exactly these four codes, with these four distinct meanings.
They already carry the repository's `PAGINATION_COMPAT_` prefix convention, so no prefix variant is
required:

| Code | Meaning |
|---|---|
| `PAGINATION_COMPAT_TOTAL_INVALID` | `total` is present but not a non-negative JSON integer (`bool`, `float`, numeric string, `null`, object, array, or a negative integer) |
| `PAGINATION_COMPAT_TOTAL_UNSTABLE` | the normalized `total` is not identical on every page of the sub-window, including presence appearing or disappearing mid-sub-window |
| `PAGINATION_COMPAT_TOTAL_EXCEEDED` | accumulated unique rows exceed a present, valid `total` |
| `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED` | at termination a present, valid `total` exceeds accumulated unique rows |

These four codes supersede the earlier `docs/12_…` §8 spellings. The mapping is:

| Superseded name | Replaced by |
|---|---|
| `PAGINATION_COMPAT_TOTAL_CHANGED` | `PAGINATION_COMPAT_TOTAL_UNSTABLE` |
| `PAGINATION_COMPAT_ROWS_EXCEED_TOTAL` | `PAGINATION_COMPAT_TOTAL_EXCEEDED` |
| `PAGINATION_COMPAT_SHORT_PAGE_INCONSISTENT_WITH_TOTAL` | `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED` |
| `PAGINATION_COMPAT_TOTAL_IMPLAUSIBLE` | **withdrawn** (see below) |

`PAGINATION_COMPAT_TOTAL_IMPLAUSIBLE` is withdrawn as a consequence of this decision, not as an
independent judgement: under Option B an oversized advisory `total` has no control power at all — it
cannot extend the loop, cannot derive a page count and cannot terminate anything — while the row,
page, request, byte and elapsed budgets of §6 bound the fetch regardless of its value, and
reconciliation catches any disagreement at termination. Re-introducing a separate plausibility guard
would be a new decision and requires its own review.

### 5.4 Empty and short-page cases, stated explicitly

| Case | Outcome |
|---|---|
| Empty first page, `total` absent | Successful zero-row termination, provided all response-shape and budget invariants pass |
| Empty first page, `total == 0` | Successful zero-row termination; reconciliation is exact (`0 == 0`) |
| Empty or short page, present `total` greater than accumulated unique rows | `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED` |
| Non-empty `data` with `total == 0` | `PAGINATION_COMPAT_TOTAL_EXCEEDED` |
| A page containing exactly the requested limit | Never terminates; in particular it never terminates merely because accumulated rows equal `total` |
| A short page (`len(data) < requested_limit`, including an empty page) | Authoritative termination signal, subject to final `total` reconciliation when `total` is present |

---

## 6. Required data-only safety envelope

Option B is accepted **only together with the complete C7 safeguard set**. None of these is
optional, and none may be relaxed because `total` happens to be present:

1. a stable trip identity is required for every row, through the approved `trip_id` identity
   contract;
2. no duplicate identity within a page;
3. no identity overlap with **any** prior page of the sub-window (full history, not just the
   previous page);
4. no repeated ordered page fingerprint;
5. no repeated unordered identity-set fingerprint;
6. no page-size overflow (`len(data) <= requested_limit`);
7. exact one-by-one local request-page progression (`1, 2, 3, …`);
8. no empty/non-empty non-progress oscillation;
9. enforced page, request, row, byte and elapsed-time budgets, checked before issuing the next
   request;
10. provider page order preserved in the accumulated result;
11. all pagination validation completes before any business-data mutation begins.

Any failure aborts the entire sub-window before business-data writes, and — because the sync job
opens the client-business connection only after every fetch completes and commits exactly once
(`docs/12_…` §9) — before any row is written for the whole run.

---

## 7. C7/C8 ownership decision

`docs/14_…` previously forbade `sync_trips_and_speeding.py` in C7 and assigned all sync integration
to C8, while also making C8 depend on C7. The consequence was that C7 could be merged but never
executed: nothing would pass the client's frozen mode into the provider client, so the state machine
was unreachable and untestable end to end. That contradiction is resolved here.

### 7.1 C7 owns minimum executable mode propagation

C7 must include the minimum runtime integration required to make the new state machine reachable:

```text
dispatcher / config snapshot
   → sync_trips_and_speeding job parameters
      → normalized trips pagination mode
         → provider_client /trips fetch
```

The dispatcher side of that path already exists and is **not** changed by C7: `dispatcher.py`
already selects `client_account.trips_pagination_mode`, normalizes it, writes it as claim-time
history evidence and emits it in `_build_job_params` for a compatibility `trips_sync` fire, and
`ops/recover_telematics_trips_window.py` (C11) already passes the same parameter. The missing link is
only the sync job → provider client hand-off.

C7 may therefore modify, **when strictly necessary** for that path:

| File | Permitted C7 change |
|---|---|
| `jobs/api/telematics/provider_client.py` | the compatibility state machine, mode dispatch in `fetch_trips`, a typed mode on the client constructor; `_fetch_paginated` body unchanged |
| `jobs/api/telematics/provider_safety.py` | new bounded compatibility budgets, new taxonomy constants/helpers |
| `jobs/api/telematics/sync_trips_and_speeding.py` | **propagation only** — resolve the frozen/normalized mode and pass it to the provider client |
| `jobs/trips_pagination_mode.py` | narrow extension of the existing normalized API only, if required |
| narrowly related provider/pagination tests | the focused compatibility suite plus necessary updates to existing pagination/provider tests |
| narrowly related sync propagation tests | proof that the frozen client mode reaches the provider `/trips` path |

C7 remains limited to `/trips`. Every other provider endpoint stays unchanged.

**C7 must not mutate coverage by itself.** Coverage advancement remains owned exclusively by the
reviewed C6 and C11 finalization surfaces (`docs/15_…`). Provider and sync code remain forbidden
coverage writers, and the C7 sync edit must not touch the write boundary: no streaming writes, no
additional commit, no change to `ON CONFLICT` targets, no change to `overwrite_existing` semantics.

### 7.2 C8 remains separate

C8 keeps ownership of broader integration and rollout work: any additional cross-job integration,
operational rollout automation, wider production verification, the pre-commit duplicate assertion
and write-boundary hardening, and any functionality not necessary for the first executable
`BRAVO00016` canary.

**C8 is no longer a prerequisite for the minimal mode propagation** required to exercise C7 through
the existing dispatcher and C11 runner paths.

### 7.3 Corrected module path

The pagination-mode constants and normalizer live at:

```text
jobs/trips_pagination_mode.py
```

Any reference to `jobs/api/telematics/trips_pagination_mode.py` is stale; that file has never existed.

### 7.4 Compatibility budgets and their documentation

C7 owns the implementation of its compatibility safety budgets **and** their documentation. The C7
documentation scope therefore includes `docs/02_infrastructure.md` for the exact
`TELEMATICS_PROVIDER_COMPAT_*` environment variables already approved by `docs/12_…` §5.5.

This decision introduces no variable and changes no ENV. It records only that those variables are
part of the authorized C7 implementation scope.

### 7.5 C7 commit message

The future C7 implementation commit message is exactly:

```text
feat: implement Telematics trips compatibility pagination
```

Earlier alternative spellings for C7 are superseded.

---

## 8. Consequences

1. **C7 is formally unblocked and may be implemented and reviewed.** The prerequisite of
   `docs/14_…` §2.2 is satisfied by this document.
2. **The failure taxonomy is fixed** to the four codes of §5.3, and `PAGINATION_COMPAT_TOTAL_ABSENT`
   must not be created.
3. **`docs/12_…` §7.2/§8 spellings change** as mapped in §5.3; the three renames carry identical
   meanings and identical severities, so no runbook triage decision changes.
4. **`strict_meta` is unaffected.** Its metadata parsing, `current_page` equality requirement,
   malformed/inconsistent-metadata checks, termination, loop and non-progress detection, failure
   codes and request counts stay as they are today.
5. **Accepted residual risk.** With `total` absent, the undetectable provider-side skip case
   (`docs/12_…` §6.2 delete/shift) loses its only observable signal. The mitigations remain the
   stabilization delay `D`, closed-window eligibility, and per-client review of the first fires
   (`docs/13_…`). This risk is accepted for the canary and must be restated in the C12 runbook.
6. **The provider unknowns remain open.** `docs/12_…` §0.3 U3–U8 — including U5 (`total` stability
   across clients and windows) and U7 (provider mutation during a read) — continue to gate
   *enablement*, never merge. This decision closes **D5 only**, together with the row that
   `docs/14_…` §17 numbers **U8 — missing or malformed `total`**. Note the numbering divergence
   between the two documents: `docs/12_…` §0.3 uses U8 for the separate "all clients can safely enable
   the mode immediately" unknown, which stays open.
7. **One extra request** may be issued for sub-windows whose row count is an exact multiple of the
   requested limit (§5.2), bounded by existing budgets.

---

## 9. Rejected alternatives

| Rejected | Why |
|---|---|
| Option A — abort on absent `total` | Makes ingestion depend on the metadata layer that is already known to be broken; converts a missing optional cross-check into an outage |
| A dedicated `PAGINATION_COMPAT_TOTAL_ABSENT` code | Would encode Option A's semantics in the taxonomy after Option B was accepted |
| Continued deferral | Preserves a live outage rather than optionality |
| Deriving a substitute `total` from `last_page × per_page`, or from accumulated counts | `docs/12_…` N8 forbids arithmetic repair of broken metadata; the derived value would be fiction presented as an oracle |
| Terminating when accumulated rows equal `total` | Makes `total` an authoritative terminator, contradicting `docs/12_…` §7.1 and §19 item 8 |
| Tolerating `rows_total < total` at termination as "acceptable provider inconsistency" | The two channels disagreeing is exactly the signal that the fetch is untrustworthy; the accepted rule is exact equality |
| Coercing a numeric-string `total` | The tolerant `int()` coercion is a strict-path allowance for `current_page`/`last_page`; extending it to a correctness oracle weakens the oracle |
| Making the absent-`total` policy runtime-configurable | A third behavior selector would defer the architecture decision into production configuration and multiply the states a reviewer must verify |
| Leaving sync integration entirely in C8 | Produced an unreachable, unexercisable C7 (§7) |
| Letting C7 change the write boundary or coverage while it is in the sync file | Write-boundary review is C8/G-WRITE; coverage mutation is C6/C11 under G-COV |

---

## 10. Rollout effect

This decision changes **no** rollout gate other than removing D5/U8 as a C7 prerequisite.

- Every client except `BRAVO00016` remains `strict_meta`. `BRAVO00016` remains the sole armed,
  **unexercised** compatibility canary, with coverage `READY`, `coverage_start_ts`
  `2026-07-01T00:00:00Z` and `covered_through_ts` `2026-07-27T00:00:00Z`.
- Migration `058_telematics_trips_manual_recovery.sql` remains **unapplied** in production; the
  production migration ceiling stays `057_workflow_a_trips_coverage_state.sql`.
- No C11 recovery has run. No compatibility fire has occurred. No provider request was made for this
  decision.
- Fleet-wide enablement remains forbidden. Each additional client still requires its own inventory,
  interval, evidence bundle and separate authorization.
- The remaining enablement gates of `docs/12_…` §11, `docs/13_…` §13 and `docs/14_…` §10 are
  untouched, as are the observation minimums P9 and P10.

**This decision authorizes C7 design and implementation work. It deploys nothing.** It does not
authorize a migration application, a deployment, a mode flip, a coverage mutation, a bootstrap, a
recovery, a backfill, a provider request, a schedule change, a systemd change or a push of runtime
code. Each of those remains its own separately authorized gate.

---

## 11. Implementation status

**2026-08-03 — implemented as accepted, not deployed.** C7 has been implemented in the repository
exactly as this decision specifies. Option B is realized in
`jobs/api/telematics/provider_client.py:_fetch_paginated_data_invariants_v1`: an absent `meta.total` is a
permitted state that is neither failed nor substituted, no `PAGINATION_COMPAT_TOTAL_ABSENT` code
exists, termination is the short-page rule alone, and a present `total` is validated as a genuine
non-negative JSON integer, required to be identical on every page, never exceeded by accumulated
unique rows, never used to derive a page count or to terminate, and reconciled to exact equality at
short-page termination. The taxonomy is exactly the four §5.3 codes; the three superseded spellings and
the withdrawn `PAGINATION_COMPAT_TOTAL_IMPLAUSIBLE` appear nowhere in runtime code. The §7.1 minimum
mode propagation is in place, the §6 data-only safety envelope is enforced, and the compatibility
budgets are documented in `docs/02_infrastructure.md` per §7.4.

This status note records a repository fact only. **Nothing in §10 has changed:** no deployment, no
push, no migration `058`, no recovery, no compatibility fire, no provider request, no mode change and
no coverage mutation has occurred, and the independent code-first C7 review (G-SM) remains outstanding.
