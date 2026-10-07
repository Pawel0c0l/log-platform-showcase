# Driver Eco Dashboard V1 — secure delivery

The public authorization boundary between a driver's e-mail link and their own
Eco Driving snapshot. Everything here is code and configuration only: **no
Cloudflare resource is created by this repository**, and nothing in it is
deployed.

```
capability link  https://<host>/#k=<43-char opaque secret>
   │  fragment — never transmitted in an HTTP request
   ▼
POST /api/session          same-origin, JSON body, Origin-checked
   │  digest lookup in D1 → grant (subject, object key, expiry, revocation)
   ▼
Set-Cookie __Host-eco_dash  opaque, HttpOnly, Secure, SameSite=Strict, ≤30 min
   │
   ▼
GET /api/snapshot          no parameters of any kind
   │  session → grant → snapshot_object_key (server-side only)
   ▼
private R2 bucket → one driver's schema-v1 snapshot
```

## Layout

| Path | Role |
|---|---|
| `worker/index.js` | Worker entrypoint and the only public surface: `/api/session`, `/api/session/end`, `/api/snapshot`, static asset allowlist. |
| `worker/lib/capability.js` | 256-bit CSPRNG capability/session generation, shape gate, digesting (SHA-256, or HMAC under `CAPABILITY_PEPPER`), constant-time compare. |
| `worker/lib/store.js` | `D1AuthorizationStore` and the grant/session state machine. Carries the storage-choice rationale. |
| `worker/lib/session.js` | Cookie construction and parsing. |
| `worker/lib/http.js` | Security headers, CSP, cache policy, the deny vocabulary. |
| `worker/lib/snapshot.js` | Contract gate: size, JSON well-formedness, the strict schema, the forbidden-name sweep kept as defence in depth, and the subject/object binding check. |
| `worker/lib/schema_v1.js` | The strict schema-v1 allowlist. Validation rebuilds the document, so stored bytes are never forwarded **to the browser**. The rebuild is a read-path sanitiser only — it is never what gets stored. |
| `worker/lib/digest.js` | The one publication payload-digest definition, shared by the write path, the read path and the R2 metadata. Octets only: it refuses to hash text. |
| `worker/lib/assets.js` | Static allowlist; `fixtures/` and `preview.html` are unreachable. |
| `worker/lib/log.js` | Structured logging that redacts anything shaped like a bearer secret. |
| `worker/lib/publisher.js` | Rotation / revocation, opaque object-key minting, the snapshot write. Deliberately has **no** unconditional grant-insert helper. |
| `worker/lib/publication.js` | The authoritative publication state machine: atomic grant issuance, atomic bearer recovery, delivery transitions. |
| `worker/lib/publisher_auth.js` | Machine-to-machine credential verification for the write transport. |
| `worker/lib/body.js` | Bounded request-body reading: the strict declared-size gate (`parseDeclaredLength` / `requireDeclaredLength`), the read bounded by `min(declared, ceiling)` with actual-versus-declared equality, BYOB fixed-size read where the runtime supports it, default-reader fallback. |
| `spec/subject_binding_v1.md` | Normative cross-language binding specification. |
| `spec/subject_binding_v1_vectors.json` | Fixed vectors asserted from both JavaScript and Python. |
| `schema/001_authorization.sql` | D1 schema. Authorization state only — no score, name, e-mail or client id. |
| `local/memory_bindings.js` | In-memory D1/R2/ASSETS so the real Worker runs with no remote resource. Models D1's projection **and** its CHECK/UNIQUE constraints, and provides the forced-interleaving barriers. |
| `local/serve.js` | Local runtime for browser verification. |
| `local/dev_grants.js` | **Local only.** Unconditional grant creation for verification scaffolding. No module under `worker/` imports it. |
| `local/node_runtime.js` | WebCrypto global shim for Node. Test scaffolding; never deployed. |
| `wrangler.toml` | Binding declarations with placeholder identifiers. Creates nothing. |

## Capability lifecycle

| Operation | Effect |
|---|---|
| **issue** | Mint a 256-bit capability, store only its digest against one `subject_ref` and one `snapshot_object_key`, with an explicit `expires_at`. The raw value is returned once and is unrecoverable afterwards. |
| **lifetime** | A property of the **reporting period**, not of the link: **weekly = 10 days, monthly = 60 days** (`worker/lib/capability_ttl.js`). There is no default — `/api/publish` and `/api/publish/recover` both require `X-Publication-Period`, and an unknown, empty, missing or duplicated value is refused `400` before any operation, object or grant exists. |
| **expire** | Enforced server-side on every exchange. An expired grant answers `410` → the frontend's `LINK_EXPIRED`. The grant ROW survives its expiry: it holds no secret, it already classifies as `EXPIRED`, and it is what makes an old link distinguishable from one that never existed. |
| **revoke** | Sets `revoked_at`. Every **subsequent authorization check** fails — including checks for sessions already derived from that grant, because session validation joins the grant. Answers `401`, byte-identical to an unknown capability, so revocation is never disclosed. Idempotent: a second call reports `ALREADY_REVOKED` and cannot move the timestamp. |
| **rotate** | One D1 batch guarded by a compare-and-set on the predecessor. See "Rotation contract" below. |
| **revoke sessions only** | Bumps `session_epoch`; live sessions stop, the link keeps working. |
| **re-point** | `updateSnapshotObjectKey` moves a grant to a newly published object without re-issuing the link. Not used on the publication path: a capability is pinned to one historical snapshot. |
| **compact** | `POST /api/publish/maintenance`, publisher-authenticated like every other write route and `404` to everyone else. Removes **expired sessions** in bounded, idempotent batches. It deletes no grant and no R2 object. |

### Period-scoped, snapshot-pinned — and what that means for an old e-mail

One capability belongs to one driver's one reporting period and resolves,
forever, to that period's immutable snapshot object. It is deliberately **not**
one rolling URL per driver.

Publishing a newer period therefore **does not revoke the previous one**. If W1
is published on day 0 and W2 on day 7, W1 keeps its remaining ~3 days and keeps
showing W1, while W2 starts its own 10 days showing W2; a monthly link
published the same day is independent again at 60 days. Consecutive reports
overlap on purpose, so a driver opening last week's e-mail sees last week's
report rather than a newer one silently substituted.

A resend of the SAME period converges on the same logical delivery: if its grant
is still usable it is reused unchanged, and if it has expired the host rotates
once through `POST /api/publish/recover` under the unchanged operation id,
subject binding and payload digest — so the replacement link still shows that
period's snapshot.

**Capability expiry is not report retention.** An expired bearer never deletes
the R2 object it pointed at; snapshot retention is a separate concern with a
separate owner decision.

### Rotation contract

Rotation is a **compare-and-set**, not a blind insert-then-update. Both
statements run in one D1 batch (a transaction) and both carry the same
eligibility predicate on the predecessor — `revoked_at IS NULL AND rotated_to IS
NULL`. The successor insert is an `INSERT ... SELECT ... WHERE EXISTS`, so an
ineligible predecessor inserts **zero rows** rather than leaving an orphan
successor behind.

Outcomes are explicit; rotation never throws for an expected state:

| Result | Meaning |
|---|---|
| `ROTATED` | the successor exists and its raw capability is returned, once |
| `ALREADY_ROTATED` | a successor already exists; its `capability_id` is returned and **nothing new is minted** |
| `REVOKED` | the predecessor was withdrawn first; nothing is minted |
| `UNKNOWN` | no such grant; nothing is minted |

**Idempotency is explicit conflict, not silent re-mint.** A retry returns
`ALREADY_ROTATED` and deliberately does *not* return the earlier raw
capability: that value was handed to the caller once and is unrecoverable by
design. A caller that lost it must revoke and issue afresh.

Proven by test at 2-, 8- and 32-way contention: exactly one `ROTATED`, the rest
`ALREADY_ROTATED`, one live grant, two rows. A rotation whose transaction fails
leaves the predecessor unrevoked, unrotated and still usable, and a later retry
succeeds. Revoke-versus-rotate is safe in both orders — whichever wins, at most
one grant is live and the predecessor is never one of them.

### Retry classification depends on the projection

`ALREADY_ROTATED` is distinguishable from `REVOKED` only because
`findCapabilityById` projects `rotated_to` — a rotated predecessor is *also*
revoked, so without that column every retry reads as `REVOKED`. That was a real
defect against D1 while the in-memory double, which returned whole stored rows,
reported the correct answer.

Two rules follow, and both are enforced by test:

* `store.js` names its columns explicitly through `CAPABILITY_COLUMNS`. `SELECT *`
  is not used, because an explicit list is what a test double can model.
* `local/memory_bindings.js` parses the statement's column list and returns
  **only** those columns, and throws on a column that does not exist. A narrowed
  projection therefore behaves in tests exactly as it would in production.

`test_narrowed_projection_still_reproduces_the_defect` re-runs rotation against
a deliberately pre-fix projection and asserts it still misclassifies. If the
double ever becomes permissive again, that test fails.

### Object keys

`mintObjectKey()` produces `<hex-shard>/<32-char base64url>.json`. The shard is
one CSPRNG byte rendered as lower-case hex, **not** a slice of the base64url
body: a base64url slice can contain `-` or `_`, which the validator rejects, so
the earlier generator emitted keys that failed its own contract about 6 % of the
time. Generator and validator now agree deterministically — 200 000 minted keys,
zero rejections. Neither half is derived from driver, client or period identity,
and identity-shaped keys are still refused.

## Storage decision — D1, not KV

Revocation decides it. Workers KV is eventually consistent, so a revoked link
would keep working at some edges for up to about a minute — and "revoke this
driver's link now" is the operation this boundary exists to serve. D1 reads hit
a single primary, so revocation is effective on the next request, and D1's
batch gives rotation a real transaction. The table is thousands of rows for the
two audited clients (~1 400 drivers) with single-digit reads per dashboard
open, which is far inside the free tier. Durable Objects would buy the same
consistency at materially higher complexity; R2 object metadata cannot express
expiry or revocation lookups at all.

The store is behind an interface, so the decision is reversible without
touching the Worker's request handling.

## Snapshot gate — strict schema v1

A valid `contract_id` is not enough. `worker/lib/schema_v1.js` is a strict
**allowlist**: `additionalProperties: false` at every object boundary, correct
scalar/array/object types, expected enums, bounded numeric ranges, bounded
string lengths and no control characters, plus the two cross-field privacy rules
the contract already states (ranking fields exist if and only if the driver is
ranked; a non-`OK` entry carries no Eco payload).

The response body is **rebuilt from the validated allowlist**. Stored bytes are
never forwarded, so a field the schema does not name cannot reach the browser
even if the publisher wrote it. Rebuilding is lossless: every synthetic fixture
round-trips semantically unchanged, and the suite asserts the schema in both
directions — every field the Python builder emits must be accepted, and every
field the schema names must actually occur — so the allowlist cannot drift from
the contract it mirrors.

This is a structural and privacy boundary only. It never scores, never bands a
coefficient and never checks that points add up; the numeric bounds are wide
sanity windows, not scoring assertions.

### Subject/object binding

At publish time `putSnapshotObject` writes `subject_binding` into the R2
object's **custom metadata**: a digest over `subject_ref` and the object key
(HMAC under `CAPABILITY_PEPPER` when one is bound). On read the Worker
recomputes it from the grant and compares in constant time **before parsing the
body**. Three mix-ups therefore become detectable and fatal:

* the right key holding another subject's document;
* an object copied to a different key;
* an object published without a binding at all (no proof, so no delivery).

Neither `subject_ref` nor the object key nor the binding itself ever reaches the
browser — the binding lives in object metadata, not in the snapshot JSON.

## Request-body bounds

`POST /api/session` is unauthenticated, so it must never buffer an arbitrary
body and only then decide it was too large.

### The invariant

> **`POST /api/session` must present an acceptable declared body size (exactly
> one canonical `Content-Length`, at most 512 bytes) BEFORE the Worker reads the
> body. The bytes actually read are then independently bounded during reading,
> by both that declaration and the endpoint ceiling.**

This is the whole safety contract, and it holds in every reader mode. It
replaces the previous gate — "the deployed runtime must prove BYOB" — which
deployed verification showed the platform cannot satisfy (see below). The
history is not rewritten: BYOB *was* tested on the real runtime and *is*
definitively unavailable for an incoming `Request.body`. What changed is that
this is no longer a release blocker, because the bound no longer rests on it.

### Order of operations

| # | Step | On failure |
|---|---|---|
| 1 | method, `Origin`, secure scheme | 405 / 401 |
| 2 | **rate limit** (`SESSION_RATE_LIMIT`) | 429 + `Retry-After: 60` |
| 3 | exact media type (`application/json`) | 400 |
| 4 | **declared-size gate** — `requireDeclaredLength(headers, 512)` | 400, **zero bytes read** |
| 5 | bounded read against `min(declared, 512)`, actual checked against declared | 400 |
| 6 | JSON parse and capability shape | 400 / 401 |
| 7 | capability digest → D1 → session insert | 401 / 410 / 503 |

Nothing at step 4 or below issues a D1 statement, computes a digest, creates a
session or emits `Set-Cookie`.

### Accepted and rejected declared sizes

RFC 9112 defines `Content-Length` as `1*DIGIT`. That is exactly what is
accepted, plus the endpoint ceiling.

| Declared value | Verdict | Internal diagnostic |
|---|---|---|
| absent | rejected | `DECLARED_MISSING` |
| empty / whitespace only | rejected | `DECLARED_EMPTY` |
| duplicated, or comma-joined (`60, 60`) | rejected | `DECLARED_AMBIGUOUS` |
| `+60`, `-1`, `60.0`, `6e1`, `0x3c`, `60 bytes`, `6 0`, `not-a-number` | rejected | `DECLARED_MALFORMED` |
| more than 15 significant digits | rejected | `DECLARED_RANGE` |
| `513` … `5242880` | rejected | `DECLARED_TOO_LARGE` |
| `0`, `60`, `00060`, `512` | **accepted** | — |

Leading zeros are accepted deliberately: `00060` is `1*DIGIT` and denotes one
value, so refusing it would buy no security while adding a way for the browser
path to break if an intermediary ever padded the field. The ambiguity this gate
exists to refuse is *two declarations for one body*, which the singleton/comma
check catches.

Every rejection answers `400 {"error":"INVALID_LINK"}` — the same answer a
malformed capability gets. There is no 411/413 split, because a status split
would tell a caller which of "no length", "broken length" and "too much" applied.
The vocabulary above is an internal structured-log field only.

### Fail-closed, and why the header need not be trusted

If a Worker invocation does not expose an acceptable declared length, **the body
is not read.** There is no fallback to an unbounded default read.

The declared length can only bound the read *downward*. It never authorises
reading more than 512 bytes, and `readBoundedBody` counts what it actually pulls
against both `declared` and the endpoint ceiling. So the memory bound does not
depend on the header being honest — only on its presence being required.

Cloudflare presents a `Content-Length` on every observed incoming request; the
deployed verification showed the edge **synthesises** one even for a chunked
transfer. If that ever changed for some request shape, that shape would lose
availability, not memory safety — which is the correct direction for an
unauthenticated route.

### Actual versus declared

| Case | Result |
|---|---|
| actual == declared | accepted |
| actual > declared, still ≤ 512 | refused mid-read, stream cancelled — `DECLARED_MISMATCH` |
| actual > 512 | refused mid-read, stream cancelled — `STREAM_TOO_LARGE` |
| actual < declared | refused on completion — `DECLARED_MISMATCH` |

A short body is a framing failure, not a small document: it is refused rather
than parsed truncated. A conforming HTTP peer cannot produce one, so the
equality costs nothing and removes the question of which number to believe.

The two reasons are kept distinct on purpose — one names the declaration, the
other the endpoint ceiling — so the independent defence-in-depth layer stays
visible in the logs.

### Reader mode is telemetry, not a gate

| Mode | What it adds | Status |
|---|---|---|
| `byob` | per-read allocation is a buffer *we* chose (1 024 B), so per-read cost is independent of the peer's chunking | **opportunistic optimisation.** Used where the runtime offers a byte stream. |
| `default` | runtime-chosen chunks; one read may deliver a chunk larger than the ceiling before the code can refuse it | **an accepted production mode**, once the framing gate above is satisfied |
| `buffered` | no stream to meter (some runtimes and tests) | accepted, same bound |

`worker/lib/body.js` still tries BYOB first and falls back. Both paths enforce
the same `min(declared, 512)` ceiling and the same actual-versus-declared
equality, and **neither mode is a deployment blocker.**

| Endpoint | Ceiling | Declared size required? |
|---|---|---|
| `POST /api/session` | 512 B — the protocol is one capability | **yes**, fail-closed |
| `POST /api/publish` | 256 KiB — one canonical snapshot (browser budget is 60 kB) | no — credential-gated; an oversized declaration is still refused before the read |

### What the deployed runtime actually showed

Recorded, not rewritten. The workers.dev verification of candidate `07411e9`
produced hundreds of observations:

| Probe | Observation |
|---|---|
| small declared body | `reader_entered = true`, `body_read_mode = "default"`, `bytes_read = 60` |
| small chunked body | `reader_entered = true`, `body_read_mode = "default"`, `bytes_read = 71` |
| oversized declared body | `DECLARED_TOO_LARGE`, `reader_entered = false`, `bytes_read = 0` |
| oversized chunked body | edge synthesised `Content-Length`; same `DECLARED_TOO_LARGE`, zero body bytes read |

`Request.body` on Cloudflare is **not a byte stream**, so
`getReader({ mode: "byob" })` never succeeds there. That finding stands.

**No compatibility flag fixes this.** `streams_byob_reader_detaches_buffer`
(default since 2021-11-10) governs whether a BYOB read detaches the caller's
`ArrayBuffer`; `internal_stream_byob_return_view` (default since 2024-05-13)
governs what a BYOB `read()` returns at end of stream. Both only alter the
behaviour of a BYOB reader that **already exists**; neither is documented as
converting a non-byte-oriented incoming `Request.body` into a byte stream.
`NO_COMPATIBILITY_FLAG_FIX_FOR_INCOMING_BODY_TYPE`. The compatibility date was
deliberately not bumped: a broad bump activates unrelated runtime changes and is
not justified by speculation.

### Deployment gates for this endpoint

| Gate | State |
|---|---|
| Bounded request framing on `POST /api/session` | **IN THE CANDIDATE.** The invariant above, with deterministic coverage in `ops/tests_manual/test_driver_eco_dashboard_session_framing.py`. |
| Worker-native rate limiting on `POST /api/session` | **IN THE CANDIDATE and MANDATORY.** `[[ratelimits]]`, 60/60 s per actor, verified on the deployed runtime. See below. |
| Cloudflare-native request-size control at the edge | **Defence in depth, no longer blocking.** A WAF rule bounding inbound body size needs a zone, so it cannot be exercised on workers.dev. The framing gate makes it an additional layer rather than the bound. |
| `workers_dev` | **DISABLED**, and stays disabled until this candidate is deployed and its bounded verification re-run. |

### Session-exchange rate limiting

`POST /api/session` is the only route reachable before any credential is
examined, and it is the entry point to a body read, a peppered digest and a D1
lookup. It is the only rate-limited route: limiting `GET /api/snapshot` would be
a denial of service on the driver's own dashboard, and limiting the asset bundle
would break the page.

| | |
|---|---|
| Binding | `SESSION_RATE_LIMIT` (`[[ratelimits]]` in `wrangler.toml`) |
| `namespace_id` | `1001` — account-unique. Two bindings sharing a namespace share counters **across Workers**, so this value is reserved for this endpoint. |
| Policy | 60 requests / 60 seconds, per actor, per Cloudflare location |
| Actor key | `session:` + the Cloudflare-reported client address (`CF-Connecting-IP`) |
| Over the limit | `429` with `{"error":"RATE_LIMITED"}` and `Retry-After: 60` |

**Why the limit is generous.** There is no user identity before the capability
is examined, so the only stable actor is the network peer — and Cloudflare
cautions that an IP key aggregates everyone behind one NAT or privacy proxy.
Sixty per minute is roughly one exchange per second for a whole shared egress
address. The control is sized against automated enumeration volume, not against
a second visitor in the same office.

**Fail-closed, in every direction.** A limit hit is `429`; a missing binding, a
binding that throws, a binding that answers in an unknown shape and a request
with no parseable trusted client address are all `503`, refused before the body
is read and before D1 is touched. In particular a deployment that lost
`[[ratelimits]]` refuses rather than quietly serving this endpoint unprotected —
there is no fail-open seam and no environment flag that creates one. The local
runtime and the test harnesses therefore supply a working in-memory limiter
instead of relying on an absence being tolerated.

**What it is not.** A volume control, not the authorization boundary. A caller
still needs a 256-bit capability to obtain anything, and the `429` is identical
whether the request carried a valid link, an unknown one or none at all — it
never becomes an oracle for whether a link exists.

**Known residual.** The key is the exact address Cloudflare reports, so a caller
holding an IPv6 prefix can rotate the low bits for a fresh bucket per address.
Narrowing IPv6 to its `/64` would close that and is the natural next iteration;
it is a change to the approved actor-key contract and was deliberately not made
here.

### Body-read observability

`readBoundedBody()` reports how the body was, or was not, read. That evidence is
what settled the BYOB question on the deployed runtime, and it remains useful
telemetry now that reader mode is no longer a gate. Every framing refusal and
every post-read rejection on the session exchange carries `bodyReadEvidence()`:

| Field | Meaning |
|---|---|
| `reader_entered` | whether any reader was created at all |
| `body_read_mode` | `byob` / `default` / `buffered`, or **`null`** when the request was refused before any reader existed |
| `bytes_read` | bytes actually pulled |
| `largest_chunk_bytes` | the largest single delivery one read produced — the number that distinguishes a real per-read bound from a fixture that merely sent small chunks |

The states worth recognising:

| Outcome | `reader_entered` | `body_read_mode` | `bytes_read` |
|---|---|---|---|
| refused by the framing gate (`DECLARED_*`, no reader) | `false` | `null` | `0` |
| accepted, bounded, default reader | `true` | `"default"` | ≤ 512 |
| accepted, bounded, BYOB reader | `true` | `"byob"` | ≤ 512 |

**Neither mode is a release failure by itself.** `body_read_mode` is `null`
rather than a guess whenever the gate refused: naming a mode there would be a
claim about code that did not run.

The declared size itself is not emitted as a separate field. On a successful
read it is already observable — `bytes_read` equals it, by the equality
invariant — and echoing a rejected one back into a log would add request data
for no diagnostic gain. The `DECLARED_*` reason is the diagnostic.

Every field is a bounded integer, a boolean or a fixed vocabulary string. No
byte of the body, no capability, no session id and no client address reaches a
log — the actor key is used to call the binding and nothing else. The reason
vocabulary is also deliberately kept under 24 characters per token, because
`lib/log.js` redacts longer opaque runs as secret-shaped and would otherwise
blind the very deployment failures these reasons exist to report.

The success path deliberately emits no new log line. A synthetic probe
establishes the mode by sending a small, well-formed body carrying an
**unknown** capability: the read succeeds, the authorization store refuses, and
the existing `session_exchange_denied` event carries the evidence without ever
naming the capability.

## Logout contract

A `204` from `POST /api/session/end` means the **server-side session is gone**
and a copied cookie can no longer be replayed. Clearing the browser cookie is not
logout: on its own it only hides the credential from one browser while the
session stays live everywhere else.

If server-side invalidation fails the response is `503` and the cookie is
deliberately **left in place**. The session genuinely still exists, the response
says so, and keeping the cookie is what makes a retry able to terminate it —
clearing it would strand a live session with no client able to end it. A retry
after the store recovers returns `204`, and the replay then fails.

## Publication operations — one atomic D1 contract

The publisher will run on the calculation host. It needs to retry safely, and
the hard part is the raw bearer: it is generated once and deliberately never
stored, so a lost response cannot be answered by "read it back".

**This section previously overstated the guarantee.** The earlier implementation
treated the publication row as a progress log — insert a grant, then update the
row to point at it, as two independent writes — and an independent review
demonstrated the consequences with forced barriers: 2, 8 and 32 concurrent
publishes each produced that many raw bearers, R2 objects and live grants, and a
crash between the two writes left a live grant the ledger did not reference.

The row is now the **lock**, not the log.

```
CREATED ──▶ SNAPSHOT_WRITTEN ──▶ GRANT_MINTED ──▶ DELIVERY_INTENT_RECORDED ──▶ DELIVERED
```

| State | Meaning |
|---|---|
| `CREATED` | the operation exists **and owns exactly one server-minted R2 object key**, written into the same INSERT that created the row |
| `SNAPSHOT_WRITTEN` | the owned object has been written to R2 |
| `GRANT_MINTED` | a capability exists **and** this row references it — both facts committed together |
| `DELIVERY_INTENT_RECORDED` | the host durably queued its own send intent |
| `DELIVERED` | terminal; recovery is refused from here by the recovery transaction's own predicate |

### Atomic transaction boundaries

Two D1 batches — each one transaction — carry every security-relevant
transition. Nothing else creates a grant.

| Transaction | Statements | Predicates checked *inside* the transaction |
|---|---|---|
| **initial grant** (`mintGrantTransactionally`) | conditional `INSERT` of the capability, then the ledger `UPDATE` | `operation_id`, phase `= SNAPSHOT_WRITTEN`, `capability_id IS NULL`, `subject_ref`, `payload_digest`, owned `snapshot_object_key`, and (on the UPDATE) that the capability now exists |
| **bearer recovery** (`recoverGrantTransactionally`) | conditional `INSERT` of the replacement, supersession of the predecessor, ledger move | `operation_id`, `subject_ref`, ledger still names the predecessor, state ∈ {`GRANT_MINTED`, `DELIVERY_INTENT_RECORDED`} (i.e. **not** `DELIVERED`), predecessor still eligible, and that the replacement now exists |

Each transaction's later statements are chained to its first by an existence
test on the row that first statement inserts, so the only reachable outcomes are
"all applied" and "none applied". A half-applied grant is not expressible.

**Stated dependency:** this rests on D1 executing a `batch()` as one
transaction, which Cloudflare documents. The code does not merely assume it —
if a batch ever reported a mixed result (some statements applied, some not) the
transition throws `PUBLICATION_GRANT_NOT_ATOMIC` / `PUBLICATION_RECOVERY_NOT_ATOMIC`
and the request answers `503` rather than proceeding on a half-applied state.
That branch is unreachable while the platform honours its contract, and it
exists so a violation surfaces as a loud failure instead of a silent orphan.

The database enforces the same thing independently:

```sql
CONSTRAINT chk_eco_publication_grant_ledger CHECK (
  (state IN ('CREATED','SNAPSHOT_WRITTEN') AND capability_id IS NULL)
  OR
  (state IN ('GRANT_MINTED','DELIVERY_INTENT_RECORDED','DELIVERED')
   AND capability_id IS NOT NULL))
CREATE UNIQUE INDEX uq_eco_publication_object_key ON eco_publication_operation (snapshot_object_key);
CREATE UNIQUE INDEX uq_eco_publication_capability ON eco_publication_operation (capability_id) WHERE capability_id IS NOT NULL;
```

### One operation, one object

R2 and D1 share no transaction, which is exactly why ownership is committed to
D1 first. The object key is minted by the Worker and written **into** the
creating INSERT, so:

* every caller that loses the creating INSERT discards its candidate key and
  never touches R2 — a losing publisher creates no orphan at all;
* a retry reuses the owned key and writes nothing at all when the existing
  object is proven coherent, so R2 converges on one object holding one byte
  sequence;
* a write that fails before the object exists leaves an operation whose owned
  key is **definitively absent** from R2, and only then does the retry write
  that same key.

### Object-state inspection — an error is never an absence

An earlier revision of this document said a retry "reuses the object when it is
already present and already hashes to the ledger digest". That was too weak in
two ways, both confirmed by review, and the contract below is the actual one.

Object inspection returns one of **four** states, never a boolean `present`:

| State | Meaning | What publication may do |
|---|---|---|
| `ABSENT` | R2 answered definitively that the key does not exist — `get()` resolved to exactly `null` without throwing. `null` is the **only** value that means this | the **only** state that permits writing the owned object, and only while the ledger still says `CREATED` |
| `PRESENT_VALID` | the object exists and **every** invariant below was proven | reuse it: zero puts, zero new keys, exact bytes preserved, the existing idempotent state machine continues |
| `PRESENT_INVALID` | the object exists and at least one invariant failed | fail closed: `OBJECT_INTEGRITY_FAILURE`, HTTP 409 |
| `UNREADABLE` | validity **and** absence were both impossible to establish | fail closed: `OBJECT_UNREADABLE`, HTTP 503 |

**Reuse requires all of these**, not just the body hash:

1. the body is readable;
2. `SHA256(actual object bytes) == ` the publication ledger's `payload_digest`;
3. `customMetadata.payload_digest` exists and is well formed;
4. that metadata digest equals `SHA256(actual body)`;
5. `customMetadata.payload_digest_algorithm` is the contracted `sha-256`;
6. `customMetadata.subject_binding` exists;
7. it is valid for the operation's `subject_ref`, the operation-owned object
   key and `binding_version = 1`;
8. the result is an object-shaped R2 result and **not an array**;
9. the result carries a **string** `key` and it is **equal** to the
   operation-owned key.

Every one of these must be **positively proven**, never merely
un-contradicted. Invariant 8 is a container check, not a formality:
`typeof [] === "object"`, and an array can carry a `key`, an `arrayBuffer()`
and a `customMetadata`, so a decorated array satisfies every content check
above while being something R2 never returns. It is refused for being an array,
before any property it supplies is trusted. Invariant 9 in particular: a result with no `key`, or with a
`key` that is not a string, cannot establish which object the response
describes, so it is `UNREADABLE` — the absence of a key is not evidence that it
matches. A well-formed string key naming a *different* object is a statement
about the object rather than about our ability to read it, and stays
`PRESENT_INVALID` (`KEY_MISMATCH`).

**Definitive absence versus failure.** The Workers R2 API defines `get(key)` as
resolving to `null` "if the key does not exist"; an error surfaces as a
rejected promise, and a conditional `get` whose precondition fails resolves to
an object with no body. These are kept strictly apart:

| R2 observation | State |
|---|---|
| `get()` resolves to `null` | `ABSENT` |
| `get()` throws | `UNREADABLE` |
| `get()` resolves `undefined` | `UNREADABLE` |
| result is not an object, or is otherwise malformed | `UNREADABLE` |
| result is an array — bare or decorated with a key, a body reader and metadata | `UNREADABLE` |
| result carries no `key`, or a non-string `key` | `UNREADABLE` |
| result carries a string `key` naming another object | `PRESENT_INVALID` |
| result cannot be interpreted (no readable body) | `UNREADABLE` |
| body read throws | `UNREADABLE` |
| metadata access throws | `UNREADABLE` |
| no bucket binding | `UNREADABLE` |
| metadata missing or malformed | `PRESENT_INVALID` |

`undefined` appears nowhere in the documented R2 contract, so a `get()` that
resolves it is a storage response this code cannot account for — and an
unaccountable response is the one thing that must never be answered with a
write. Anything the Worker cannot use to establish the object's authoritative
identity is `UNREADABLE`, never `ABSENT` and never `PRESENT_VALID`.

The defect this replaces reported an unreadable object as `present: false`, so
a `get()` that threw was answered with another `put()` that overwrote an object
nobody had been able to read — and an object whose body still hashed correctly
but whose digest metadata or subject binding had been removed advanced the
ledger to `GRANT_MINTED`, returned 201 and minted a live grant whose driver read
then failed 503 for the life of the link.

**Ordinary retry never repairs.** No path from `/api/publish` overwrites,
deletes or re-stamps an existing object whose integrity has not first been
proven. A corrupted object stays corrupted and stays visible, which is a
precondition for investigating it. If an operational repair path is ever
wanted, it is a separate explicit operation and a separate milestone.

`ABSENT` is also a **failure** once the ledger has reached `SNAPSHOT_WRITTEN`:
the ledger already records that the object was written, so its disappearance is
an integrity incident and re-creating it would be exactly that forbidden
repair.

Covered by `ops/tests_manual/test_driver_eco_dashboard_retry_integrity.py`,
which measures every case against the response status, the R2 put count, the
exact stored octets before and after, the operation phase before and after, the
grant row count, the live grant count and the bearer-return count.

**One gate, for every operation that moves authorization.** The four states
above are not a publication-path rule. `inspectSnapshotObject` — reached as
`services.inspectObject` on the one publication services bundle — is the single
authoritative object-inspection contract, and **both** operations that mutate
or advance authorization state for an existing publication go through it:
ordinary publication retry and lost-bearer recovery. There is deliberately no
separate, weaker integrity path for recovery, and both routes render its
refusals through one shared responder.

For **recovery**, only `PRESENT_VALID` may proceed. `ABSENT`, `PRESENT_INVALID`
and `UNREADABLE` all fail closed:

* `ABSENT` — an operation that claims an authoritative published snapshot whose
  object is missing cannot issue a replacement bearer for it. 409;
* `PRESENT_INVALID` — 409;
* `UNREADABLE` — 503. Storage failure is never a reason to rotate
  authorization.

A failed recovery integrity check performs **zero authorization mutation**. The
gate is a precondition evaluated before the D1 recovery transaction is
attempted, so in every refused case there is no replacement grant row, no
revocation or supersession of the predecessor, no bearer-generation increment,
no change of the publication's capability identity, no raw bearer, an unchanged
ledger, unchanged R2 bytes, an unchanged R2 put count, and not one recovery
statement issued to D1. The bearer the host already holds keeps working, and
restoring the exact valid object lets recovery succeed normally.

R2 and D1 share no transaction and nothing here pretends otherwise. The
contract is: prove the currently referenced object `PRESENT_VALID`, then run
the existing conditional D1 recovery transaction — which still verifies
operation state, current capability/grant identity, generation, not-`DELIVERED`
and predecessor eligibility. The two ledger columns the object was proven
against, `snapshot_object_key` and `payload_digest`, are also bound into that
compare-and-set, so the mutation can only apply to the identity that was
proven. Both are immutable for the life of an operation, so the predicate never
refuses a recovery that ought to succeed; it exists so that immutability is
checked rather than assumed.

Covered by `ops/tests_manual/test_driver_eco_dashboard_recovery_integrity.py`,
including a recurrence detector that is mutation-proved: with the
`PRESENT_VALID` guard deleted, an injected R2 `get()` failure rotates
authorization again.

**Orphan semantics.** The only orphan this design can produce is an R2 object
whose operation is later abandoned by the host (never retried, never delivered).
That is an operational cleanup concern, not an ambiguity: the authoritative
object of an operation is always the single key its row names. Concurrent
callers may each *write* the one owned key with identical bytes; that costs
redundant writes, never a second object, and the tests assert the distinct-key
count rather than the write count.

### Result semantics

Nothing returns a value that could be read as "unclear, maybe just issue again".
Every response carries `next_action`.

| Result | HTTP | `next_action` | Meaning |
|---|---|---|---|
| `PUBLISHED` | 201 | `PERSIST_BEARER` | this call won the grant transaction; the raw bearer is in this response, once |
| `ALREADY_PUBLISHED` | 200 | `USE_PERSISTED_BEARER_OR_RECOVER` | an authoritative grant exists; no bearer is replayable |
| `IN_PROGRESS` | 200 | `RETRY_PUBLISH` | the operation exists with no grant yet; a plain retry converges |
| `ALREADY_DELIVERED` | 200 | `NONE` | terminal; the link is with the driver |
| `OPERATION_CONFLICT` | 409 | `OPEN_NEW_OPERATION` | same id, different subject or payload; nothing written |
| `RECOVERED` | 200 | `PERSIST_BEARER` | this call won the recovery transaction |
| `NOT_RECOVERABLE` | 409 | — | with `reason` ∈ `ALREADY_DELIVERED` / `NO_GRANT_YET` / `SUPERSEDED` / `GRANT_NOT_ELIGIBLE` |
| `CAPABILITY_SUPERSEDED` | 409 | — | a delivery transition named a bearer that is no longer authoritative |
| `OBJECT_INTEGRITY_FAILURE` | 409 | `INVESTIGATE_OBJECT_INTEGRITY` | the owned object fails an authoritative invariant, or — on `/api/publish/recover` and on a retry past `SNAPSHOT_WRITTEN` — is definitively absent under an authoritative grant; nothing overwritten, no grant, no bearer, no supersession, no generation change, ledger unchanged |
| `OBJECT_UNREADABLE` | 503 | `RETRY_AFTER_STORAGE_RECOVERS` | the owned object's state could **not be determined**; same zero-effect guarantee. Distinct from both absence and corruption |

Both are reachable from `/api/publish` **and** `/api/publish/recover`: the two
operations share one object-integrity gate and one response mapping.

Neither integrity response names the failing invariant: which check failed is
information about a private object, and it is logged Worker-side only.

### Concurrency, as measured

`ops/tests_manual/test_driver_eco_dashboard_publication_transaction.py` forces
the interleavings rather than hoping for them: the local D1/R2 doubles park
callers at named transition sites and the suite releases them in a chosen order.

| Scenario | Result |
|---|---|
| 2 / 8 / 32 / 128 simultaneous publishes, one operation id | 1×`201`, N−1×`200`, **1** raw bearer, **1** object, **1** live grant, **1** operation |
| 8 callers parked together at the operation claim / object write / snapshot-written / grant-transaction boundary (before **and** after) | 1 bearer in every case |
| 32 callers parked at the grant boundary, released **one at a time** | 1 bearer |
| 25 repetitions × 4 callers | max bearers 1, max objects 1, zero orphaned grants |
| 2 / 8 / 32 concurrent recoveries, and 16 parked and released together | exactly **1** replacement; the rest `409 SUPERSEDED` |

### Raw-bearer response loss

A plain retry never mints a second bearer — that is the whole point of the
separation. Recovery is a distinct call, so "already minted" can never be
mistaken for "safe to issue again".

`POST /api/publish/recover` supersedes the unusable grant and installs its
replacement **in one transaction with the ledger move**, incrementing
`bearer_generation` (1 after the initial mint, +1 per recovery). It does not
call the generic `rotateCapability` and then record the result separately —
that split was the atomicity gap. The operation therefore never has two live
grants at any instant, and the lost bearer is dead immediately: exchanging it
returns `401`, exchanging the replacement returns `204`.

The replacement raw bearer is returned only to the winning call and is not
stored server-side.

### Delivery is terminal, and delivery names its bearer

Once the state is `DELIVERED` the link is in the driver's mailbox. The refusal
to recover from there is a predicate **inside** the recovery transaction, not a
`SELECT` before it, so a recovery that was decided while the operation was still
recoverable commits nothing if delivery wins first. Proven by a forced
interleaving in both directions:

* recovery parked at its transaction, `DELIVERED` driven to completion, recovery
  released → `409 ALREADY_DELIVERED`, generation unchanged, delivered bearer
  still exchanges `204`;
* delivery parked at its transition, recovery completed, delivery released →
  `409 CAPABILITY_SUPERSEDED`, the operation stays at `DELIVERY_INTENT_RECORDED`,
  and the host succeeds once it names the current bearer.

`POST /api/publish/delivery` therefore requires `X-Publication-Capability`: the
host must say which bearer it is delivering, and a stale one is refused rather
than terminalising the operation on a grant the driver never received.

`eco_publication_operation` holds no raw bearer, no Eco data, no driver name and
no e-mail address. `bearer_generation` makes a recovery auditable without ever
storing the value.

### Host crash matrix

| # | Window | Safe next action | Proven |
|---|---|---|---|
| 1 | before the publish request | `POST /api/publish` | yes |
| 2 | request sent, Worker never received it | retry `/api/publish` — no operation exists | yes |
| 3 | Worker owns the operation and its key, dies before the R2 write | retry `/api/publish`; the owned key is reused, no second object | yes |
| 4 | R2 write done, dies before the grant transaction | retry `/api/publish`; state is `SNAPSHOT_WRITTEN`, no grant exists. The retry **re-proves the object** before minting a grant over it | yes |
| 4a | R2 write done, object later corrupted or its metadata removed | retry `/api/publish` fails closed with `OBJECT_INTEGRITY_FAILURE`; zero overwrite, zero grant, ledger unchanged. Recovery is an explicit investigation, never a retry | yes |
| 4c | R2 write done, R2 unreadable at retry | retry `/api/publish` fails closed with `OBJECT_UNREADABLE` (503); zero overwrite, zero grant. A later retry converges once storage recovers, still without repairing anything | yes |
| 4b | transaction aborted **mid-batch** | retry `/api/publish`; rollback left no capability and no ledger move | yes |
| 5 | grant+ledger committed, response lost | `POST /api/publish/recover`; a retry answers `ALREADY_PUBLISHED` + `USE_PERSISTED_BEARER_OR_RECOVER` and mints nothing | yes |
| 6 | response received, host died before persisting the bearer | `POST /api/publish/recover` | yes |
| 7 | host recorded `DELIVERY_INTENT` | continue delivery with the named `capability_id`; recovery is still permitted until `DELIVERED` | yes |
| 8 | `DELIVERY_INTENT` recorded, provider request not yet sent | the host commits "a submission may have happened" **before** the provider call, then reconciles under an idempotency identity derived from the operation id | yes — host side, `docs/28` §11 |
| 9 | provider accepted, host died before recording acceptance | reconcile or replay under that same identity; where the provider offers neither lookup nor request idempotency, the host records an explicit ambiguous state instead of resending | yes — host side, `docs/28` §11 |
| 10 | after local `DELIVERED` | nothing; the operation is terminal and recovery is refused | yes |

Stages 8 and 9 were out of scope when this document was first written and are
now closed on the **host** side; the Worker's part of them was already
unambiguous. The host implementation, its durable state model and its evidence
live in `docs/28_driver_eco_dashboard_v1_snapshot_foundation.md` §11 — this
boundary itself did not change to accommodate them.

### What the host must persist

Mirrored in `jobs/ecodriving_dashboard/publication.py::HOST_PERSISTENCE_CONTRACT`.

| Before | Persist |
|---|---|
| calling `/api/publish` | `operation_id`, `subject_ref`, `payload_digest` over the exact canonical bytes |
| acknowledging the response | `capability_id`, **the raw `capability`**, `expires_at` — this is the only moment the bearer exists |
| sending the message | its own durable delivery intent, then `POST /api/publish/delivery` with `INTENT` and the `capability_id` it is delivering |
| considering the job done | `POST /api/publish/delivery` with `DELIVERED` and the same `capability_id` |

If the host crashes between the response and persisting the bearer, the correct
move is `POST /api/publish/recover`, not a re-publish.

The host side of this table is implemented in `jobs/ecodriving_dashboard/`
(`delivery_ledger.py`, `publisher.py`) and its durable ledger is
`db/client_business/049_eco_dashboard_delivery_operation.sql` plus its forward
delta `050_eco_dashboard_external_mailer_ownership.sql`. That ledger holds
the recipient e-mail address and the driver identity key; **neither ever
reaches this boundary**, and `local/publisher_serve.js` exposes a local-only
leak-scan hook so a verification suite can prove it without emitting any of the
state it scans.

## Canonical snapshot publication — the trust split

| Side | Responsibility |
|---|---|
| **HOST** (`jobs/ecodriving_dashboard/publication.py`) | business validity, **value-level privacy**, **fixed-vocabulary enforcement**, canonical deterministic serialisation |
| **WORKER** (`worker/lib/schema_v1.js`) | authorization, subject/object binding, **structural** schema allowlist |

The Worker's schema gate is defence in depth, not the privacy authority: it can
see that `label` is a bounded string, but it cannot know whether a particular
string is a driver's name. That judgement needs the source data, so it lives on
the host.

**This boundary was previously too permissive, in two specific ways, and both
are closed.**

### The supported API accepts only the builder's own result

`canonical_bytes(mapping, banned_values=())` used to be the publisher contract.
Any structurally valid mapping was publishable, so a copied-and-mutated document
— a person-like string in an allowlisted `label`, markup in a fixed-vocabulary
field — passed. There is now one entry point and one serialiser:

```python
from jobs.ecodriving_dashboard.publication import (
    PrivacyContext, build_publishable_snapshot, serialize_publishable_snapshot,
)

publishable = build_publishable_snapshot(
    privacy=PrivacyContext(identity_key=..., client_code=..., person_names=(...,)),
    generated_at_utc=..., period_type=..., current=..., days=..., series=...,
)
body = serialize_publishable_snapshot(publishable)   # bytes
digest = publishable.payload_digest                  # over exactly those bytes
```

`generated_at_utc` must be a **stable fact about the source data**, never the
current time. The host path (`build_delivery_snapshot_from_cursor`) derives it
from the newest `updated_at` of the persisted Eco stats rows that contributed,
so unchanged data rebuilds to identical octets and therefore an identical
digest — which is what lets a delayed rerun converge on its existing
publication instead of colliding with it.

`PublishableSnapshot` carries a module-private construction witness, so it
cannot be instantiated outside `build_publishable_snapshot`, and
`serialize_publishable_snapshot` accepts nothing else — not a dict, not a
look-alike object, not a subclass, and not an instance whose `body` was
tampered with afterwards (the digest is recomputed). The raw
mapping-to-bytes helper still exists as `_canonical_bytes`, privately, because
the gate still has to run over the finished document; it is not the publisher
contract and the tests assert it is not exported.

Python cannot make misuse literally impossible. The boundary is therefore drawn
where it *can* be enforced — a witness the caller has no access to — and
`ops/tests_manual/test_driver_eco_dashboard_prepublisher.py` asserts that this
is the only supported interface.

### Privacy context is mandatory, not an optional argument

`banned_values` used to default to `()`, so forgetting it silently turned the
A12 value-level sweep into a no-op — the exact check that exists to catch a
leaked driver identifier. It is now derived from a required `PrivacyContext`
and has no default at any layer: `build_publishable_snapshot(privacy=...)`,
`build_driver_snapshot(banned_values=...)` and
`assert_snapshot_document(banned_values=...)` all require it, and an empty
`PrivacyContext` is refused at construction.

The values were already available in the builder context (`identity_key`,
`client_code`), so binding them into the result costs nothing and removes the
opportunity to forget.

### Fixed vocabulary (A15)

A12 refuses forbidden field *names* and forbidden source *values*. Neither stops
an allowlisted string field carrying something it was never meant to. A15
inverts the question: **every string in the document must belong to a field
whose vocabulary the contract fixes, or to a field whose format it fixes.**

| Kind | Fields | Source of truth |
|---|---|---|
| fixed UI vocabulary | `label`, `short_label`, `band_label`, `target_band_label`, `key`, `category_key`, `status`, `rating_type`, `ranking_state`, `ranking_transition`, `qualification_status`, `kind`, `code`, `selected_by`, `weekday_short`, `period_type`, `locale`, `contract_id` | serialised from the canonical enums and the scoring ladder in `snapshot_contract.py` — never from a source string |
| deterministic format | `period_label`, `basis_period_label`, every `*_date`, `date`, `generated_at_utc`, `snapshot_updated_at_utc`, `timezone` | validated by shape, because these are genuinely dynamic |

There is no third category: a newly added string-valued field fails A15 until
someone decides which it is. Coaching output is unaffected — its dynamic parts
are numbers and category keys, and its `code`/`selected_by` values are enums.

### Deterministic serialisation (A16)

`canonical_json_bytes` is the one canonical encoding:

* UTF-8, `ensure_ascii=False`, so Polish labels are one stable byte sequence;
* **object keys sorted**, so semantically identical documents built with
  different insertion order produce identical bytes;
* compact separators — also the form the 60 kB browser budget is measured
  against;
* `allow_nan=False` plus an explicit sweep, so NaN/Infinity cannot be emitted;
* array order preserved, because array order is semantic here;
* `Decimal`, `datetime`, `set` and non-string keys are **refused**, not coerced,
  because each has more than one plausible rendering.

Proven: the same validated snapshot built twice is byte-identical; every mapping
in the document shuffled yields identical bytes; one semantic field change moves
the digest; a fresh interpreter under a different `PYTHONHASHSEED` reproduces
the digest; and every canonical payload round-trips through the Worker's strict
schema gate.

The publication payload digest is computed over exactly these bytes, which is
what makes `X-Publication-Payload-Digest` bind a request body to an operation.

### Canonical byte identity — HOST → ledger → R2 → driver

One byte sequence, named at every boundary it crosses:

```
build_publishable_snapshot(privacy=…)  ->  PublishableSnapshot
serialize_publishable_snapshot(...)    ->  canonical UTF-8 JSON OCTETS   <- identity
sha256(octets)                         ->  payload_digest
POST /api/publish  body = octets       ->  Worker recomputes sha256(ingress octets)
                                          and refuses any mismatch
R2.put(owned key, ingress octets)      ->  stored object IS those octets
eco_publication_operation.payload_digest ==  sha256(R2 body)
GET /api/snapshot                      ->  reads the octets, verifies their digest,
                                          THEN rebuilds the response from the schema
```

| Side | Authority |
|---|---|
| **HOST** | canonical serialisation. Its octets are the publication identity. |
| **WORKER (write)** | independently hashes the exact ingress octets, strictly validates them, and stores **those octets unchanged**. |
| **LEDGER** | `payload_digest` = SHA-256 of the authoritative R2 bytes. |
| **R2** | the authoritative snapshot object contains exactly the canonical host bytes; `customMetadata.payload_digest` is recomputed from what was written. |
| **WORKER (read)** | verifies the stored octets against the object metadata digest *and* the ledger digest, then rebuilds the browser response from the strict schema. The response is deliberately a different byte sequence — same document, sanitised — and it is not what the digest identifies. |

**What changed and why.** The Worker used to store
`JSON.stringify(validatedDocument)`: a semantically identical document with
different octets. `SHA256(host bytes)` equalled the ledger digest while
`SHA256(R2 body)` did not, so `payload_digest` identified a byte sequence that
existed nowhere. Storing the ingress octets is the fix; `ops/tests_manual/test_driver_eco_dashboard_byte_integrity.py`
asserts the equality on octets, and carries a detector that fails if the
strict-schema rebuild ever coincidentally matches canonical host output.

**Why there is no JavaScript canonicaliser.** Reproducing
`canonical_json_bytes` in JS would mean reproducing Python's float repr:
`json.dumps(1.0)` is `1.0`, `JSON.stringify(1)` is `1`. A second serialiser that
disagrees on one number is a publication outage, not a control. So the Worker
preserves the octets it received and enforces only the two canonical properties
a token scanner can see without re-encoding anything — no insignificant
whitespace, and object keys in ascending order.

**Read-time integrity.** `GET /api/snapshot` hashes the stored octets on every
read and refuses to serve unless they match both the object's own metadata
digest and, when a publication owns the key, the ledger digest. Two authorities
in two different stores: R2 body and R2 metadata are written together, so
metadata alone cannot witness its own integrity, whereas D1 is a separate store
under a separate credential. A grant issued outside the publication path (local
scaffolding only) has no ledger row; the metadata authority still applies and
the served log event records which authorities ran.

## Publisher write transport

`POST /api/publish`, `/api/publish/recover`, `/api/publish/delivery` — narrow,
machine-authenticated, and entirely separate from driver authorization.

**Authentication.** `Authorization: Publisher <token>`, a 256-bit machine token
held by the host. Only its digest is configured on the Worker
(`PUBLISHER_KEY_DIGEST`, HMAC under `CAPABILITY_PEPPER` when bound), compared in
constant time. The scheme name keeps the credential spaces apart: a driver
capability arrives in a JSON body, a driver session in a cookie, and `Bearer` is
not accepted at all.

**Fail closed.** With no digest configured every request is refused. All failure
shapes — missing, wrong, malformed, driver capability, driver session, `Bearer`,
unconfigured — answer an identical `404`, so the transport does not reveal that
it exists.

| Property | How |
|---|---|
| no caller-chosen R2 key | the Worker mints the key; there is no parameter for one, and header smuggling attempts are simply ignored |
| binding preserved | the Worker derives `subject_binding` from the declared subject and its own key |
| canonical payload only | the body must be valid UTF-8, pass the strict schema gate, and be in the host's canonical form (no insignificant whitespace, keys sorted); anything else gets `422` and is never stored |
| payload bound to the operation | the digest is recomputed over the **exact ingress octets** and compared against `X-Publication-Payload-Digest` in constant time; a mismatch is `400`, a different payload under a used id is `409`. The supplied value is never the value recorded |
| the stored object is the request body | the verified ingress octets are written to R2 **unchanged**. The strict-schema rebuild decides whether to accept; it is never what gets stored. `payload_digest` is recomputed from the octets actually written and kept as non-browser-visible R2 metadata |
| grants only via the transaction | no handler creates a capability; the only grant-inserting statements in `worker/` are the conditional INSERTs inside the two publication transactions and inside `rotateCapability` |
| delivery names its bearer | `X-Publication-Capability` is required on `/api/publish/delivery`; a superseded id is `409 CAPABILITY_SUPERSEDED` |
| no enumeration | no listing route, no grant lookup route, no object route |
| credentials never logged | the structured logger redacts anything bearer-shaped |
| rotation | generate a new token on the host, set the new digest, retire the old one |

Request shape:

```
POST /api/publish
Authorization: Publisher <machine token>
Content-Type: application/json
X-Publication-Operation: <operation id, 16-64 chars of [A-Za-z0-9_-]>
X-Publication-Subject:   <opaque subject ref>
X-Publication-Payload-Digest: <sha256 hex of the exact body octets>

<canonical snapshot bytes — the whole body, nothing else>
```

The snapshot is the body, and publication metadata travels in authenticated
headers. That is deliberate: in a single JSON envelope the snapshot would be a
nested value, and recovering an exact nested byte range after parsing is not
reliable — so the envelope shape would make byte identity unachievable rather
than merely awkward.

#### Accepted media type — exactly two forms

```
Content-Type: application/json
Content-Type: application/json; charset=utf-8
```

and nothing else. The header is **parsed and compared for equality**, not
prefix-matched. Case is folded, the `charset` value may be quoted, and any
parameter other than `charset=utf-8` is **refused rather than ignored**.

Rejected, by name and by test: `application/jsonp`,
`application/json-patch+json`, `application/json-seq`, `application/jsonfoo`,
`text/json`, `text/application/json`, `application/json;charset=evil`, `*/*`, a
missing header, and any duplicated representation. The earlier check was
`startsWith("application/json")`, which accepted the whole first group.

#### Publication control headers are singletons

`X-Publication-Operation`, `X-Publication-Subject`,
`X-Publication-Payload-Digest`, `X-Publication-Phase`,
`X-Publication-Capability`, `Content-Type` and `Authorization` must each carry
exactly one unambiguous value.

The Fetch `Headers` object joins repeated field lines with a comma, so a
duplicated `X-Publication-Subject: a` reached the server as the subject
`"a, a"` — a subject nobody asked to publish under — and duplicate operation
and digest headers failed only because their downstream validators happened to
be narrow. Every one of these headers is now read through a singleton gate that
refuses any comma-containing or combined form, which is conclusive because each
is a protocol-control identifier drawn from an alphabet that has no comma in
it. That reasoning is deliberately **not** generalised to bodies or to any
field where a comma is legitimate.

A refused header answers `400` with `AMBIGUOUS_CONTROL_HEADER` or
`INVALID_CONTROL_HEADER` and never says which header or what the server saw. A
refused `Authorization` answers `404`, exactly like every other publisher-auth
failure.

`Content-Type` is **required** on `/api/publish`, which carries the snapshot
octets, and **optional** on `/api/publish/recover` and `/api/publish/delivery`,
which carry no body. Optional does not mean unchecked: when one of those
bodyless routes declares a media type it must be one unambiguous value and must
be one of the exactly two accepted forms above, parsed for equality. A
duplicated `Content-Type` is `400 AMBIGUOUS_CONTROL_HEADER` and
`application/jsonp` is `400` on every publisher route, not only on
`/api/publish`.

#### Ordering: shape before mutation

Machine authentication runs first, then the protocol-shape gate, and only then
anything that can touch a store. A request with an invalid media type or an
ambiguous control header therefore creates **zero** operations, **zero** R2
objects, **zero** grants and **zero** bearers — asserted directly rather than
inferred.

```
POST /api/publish/delivery
Authorization: Publisher <machine token>
X-Publication-Operation:  <operation id>
X-Publication-Phase:      INTENT | DELIVERED
X-Publication-Capability: <the capability_id the host is delivering>
```

**The write transport exists.** Earlier revisions of this document said the
publisher operations were "deliberately not bound to any HTTP route". That is no
longer true and the statement has been removed: the three routes above are
present, machine-authenticated, and covered by
`test_driver_eco_dashboard_publication_transaction.py`, which enumerates every
`/api/publish*` route and asserts each one goes through the authoritative
transaction primitives.

## Local integration procedure

```bash
# unit + integration security suites (no browser, no credentials)
python3 ops/tests_manual/test_driver_eco_dashboard_delivery.py
python3 ops/tests_manual/test_driver_eco_dashboard_prepublisher.py
python3 ops/tests_manual/test_driver_eco_dashboard_publication_transaction.py
python3 ops/tests_manual/test_driver_eco_dashboard_byte_integrity.py
python3 ops/tests_manual/test_driver_eco_dashboard_retry_integrity.py

# interactive: local Worker with in-memory D1/R2 and a synthetic bootstrap link
node delivery/driver_eco_dashboard/local/serve.js 8788
```

The pre-publisher suite includes a full local write→read proof: canonical
snapshot → authenticated publisher write → private object with binding metadata
→ grant issuance → fragment capability → session exchange → Worker binding
verification → strict schema gate → snapshot response.

The byte-integrity suite proves the same chain on **octets**: the object R2
holds is byte-for-byte the canonical output of the Python publisher, and one
SHA-256 value identifies the host bytes, the ledger entry, the R2 body and the
R2 metadata.

## Secrets at rest

Raw capabilities and raw session ids are **never stored**. The authorization
table holds a SHA-256 digest — or an HMAC-SHA-256 digest when the optional
`CAPABILITY_PEPPER` secret is bound, which is recommended for production: with
a pepper, a leaked authorization table cannot be probed offline against a
candidate list. A slow KDF is deliberately not used: the input is 256 bits of
uniform randomness, so a single hash is already non-invertible, and a slow hash
on every request would add a denial-of-service surface.

## URL and log hygiene

The capability travels in the URL **fragment**, which browsers do not transmit,
so it cannot reach an origin access log, a CDN log, a `Referer` header or an
analytics beacon.

Capture and cleanup happen in `js/capability-bootstrap.js`, the **first script
in `<head>`** — before the stylesheet, before the renderer, before anything that
could fail. It reads the fragment, clears it with `history.replaceState`, and
hands the value on through a one-shot accessor that nulls itself on first read.
Cleanup therefore does not depend on the application bundle loading: with every
later asset blocked, the URL is still clean (proven by browser test). It is an
external file, not an inline `<script>`, so the CSP stays `script-src 'self'`.

`js/boot.js` only consumes that handover and performs the exchange — a
same-origin `POST` with the value in the body, never in a path or query.

Every Worker log call passes through `lib/log.js`, which redacts any value
shaped like a bearer secret regardless of the field it was placed in.

## Revocation boundary — stated precisely

Revocation takes effect **at the next authorization check**. It is not
retroactive: a request that has already passed the authorization decision and
started reading from R2 will complete, because by then there is nothing left to
deny. The exposure window is one already-authorized response, bounded by that
request's own lifetime.

Everything after the decision point is denied — the next snapshot request, the
next page load, and every other live session derived from the same grant.

## Response policy

| Response | Cache-Control |
|---|---|
| `/api/*` (any status) | `private, no-store, max-age=0, must-revalidate` |
| `/` and `/index.html` | `private, no-store, max-age=0, must-revalidate` |
| `/css/*`, `/js/*` | `public, max-age=3600, must-revalidate` (identical for every driver) |

Every response also carries `Content-Security-Policy`, `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Robots-Tag` with
`noindex, noarchive`, a deny-all `Permissions-Policy`, `Cross-Origin-*:
same-origin`, `Strict-Transport-Security` on HTTPS and `Vary: Cookie`.

**No `Access-Control-Allow-*` header is ever emitted.** Delivery is same-origin;
a cross-origin page can neither read a response nor drive the exchange (the POST
additionally requires a matching `Origin`).

### CSP

```
default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';
style-src-elem 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:;
font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none';
frame-ancestors 'none'; object-src 'none'; manifest-src 'none';
worker-src 'none'; upgrade-insecure-requests
```

No `unsafe-eval`, no `unsafe-inline` for scripts, no wildcard, no third-party
origin — the frontend makes zero external requests and the bootstrap was moved
out of an inline `<script>` into `js/boot.js` specifically to keep
`script-src 'self'` achievable.

`font-src` carries `data:` for one reason: `css/dashboard.css` embeds its
typeface (Manrope, latin + latin-ext) as a `data:` URI, so the page renders its
own typography without asking any third party for it. That is the *opposite* of
a network relaxation — no origin is allowed, and the exact policy string is
asserted by `test_content_security_policy_is_restrictive()`, which fails if
the directive ever grows beyond `'self' data:`. The single concession is inline **style
attributes**, which the renderer uses for layout maths (bar widths, ring
offsets); `style-src-elem 'self'` still forbids injected `<style>` elements, and
allowing a style attribute executes no script.

## Local verification

```bash
node delivery/driver_eco_dashboard/local/serve.js 8788
# prints a synthetic bootstrap link for a synthetic driver; open it in a browser
```

In-memory D1/R2/ASSETS, one synthetic snapshot, no credentials, no remote
resource, nothing persisted. `ECO_FIXTURE=<name>` selects a different synthetic
state. The local runtime is the only place a capability is ever printed, because
it exists to be opened by hand.

Deterministic security suite:

```bash
python3 ops/tests_manual/test_driver_eco_dashboard_delivery.py
```

It drives `ops/tests_manual/eco_delivery_worker_harness.mjs`, which executes the
real Worker against the emulated bindings. Neither prints a capability, a
session id or an object key.

## Deployment bindings still required

None of this exists yet; each is a separately authorized operator step.

| Resource | Requirement |
|---|---|
| R2 bucket | Private, EU jurisdiction. No public access, no custom domain, no `r2.dev` URL, no signed URLs. Bind as `SNAPSHOTS`. |
| D1 database | Apply `schema/001_authorization.sql`. Bind as `AUTHORIZATION_DB`. |
| `CAPABILITY_PEPPER` | `wrangler secret put CAPABILITY_PEPPER`. Recommended, not required. Rotating it invalidates every existing capability **and the publisher credential digest**, so it is a deliberate mass-revocation lever. |
| `PUBLISHER_KEY_DIGEST` | `wrangler secret put PUBLISHER_KEY_DIGEST`. **Required before publishing.** The digest of the host's machine token; the usable value never leaves the host. Unset means the write transport refuses everything. |
| Route / custom domain | Same origin for the static assets and the API. `workers_dev = false` is already set. |
| Scheduled cleanup | `deleteExpiredSessions` exists; a cron trigger for it is a deployment-time decision. |
| **Request framing on `/api/session`** | **CLOSED, in the candidate.** An acceptable declared body size (≤ 512 B) is required before the body is read, and the bytes read are independently bounded — see „Request-body bounds". This replaced the former BYOB release gate. |
| **BYOB on the deployed runtime** | **SETTLED, NOT BLOCKING.** Verified on the real runtime: the incoming `Request.body` is not a byte stream, so the reader mode is always `default` there. It stays an opportunistic optimisation and a telemetry signal. No compatibility flag changes it (`NO_COMPATIBILITY_FLAG_FIX_FOR_INCOMING_BODY_TYPE`). |
| **Edge request-size control** | **Defence in depth, not blocking.** A WAF rule bounding inbound body size in front of the Worker. It needs a zone, so it cannot be exercised on workers.dev; the framing gate is the bound in the meantime. |
| **Rate limiting** | **CLOSED, in the candidate and MANDATORY.** Worker-native `[[ratelimits]]` on `POST /api/session`, 60 requests / 60 s per pre-authentication actor, verified on the deployed runtime. A deployment that loses the binding refuses the endpoint rather than serving it unprotected. |
| `workers_dev` | **DISABLED**, and remains disabled until this candidate is deployed and its bounded verification re-run. |
| Cache rules | The Worker sets `private, no-store` on every sensitive response; any zone-level cache rule must be inspected so it cannot override that. |

`ALLOW_INSECURE_COOKIES` must never be set in a deployed environment. Without a
secure origin the Worker refuses to issue a session at all.

## Not in this milestone

The host-side publisher (snapshot upload, capability issuance at scale, e-mail
link delivery), Pages deployment, DNS, production R2/D1 creation, production
capability issuance and any scheduler change.

The **remote write transport is present**, not absent: `/api/publish`,
`/api/publish/recover` and `/api/publish/delivery` exist and are
machine-authenticated. What is missing is the host process that calls them at
scale, the e-mail provider integration, and — recorded in the crash matrix as
stages 8 and 9 — provider-side send idempotency, without which a host crash
between "provider accepted" and "host recorded acceptance" can send a driver two
messages. That is the next milestone's work and is not implemented here.

**Superseded in part — current state.** The host publisher milestone has since
landed (`jobs/ecodriving_dashboard/`, contract in
`docs/28_driver_eco_dashboard_v1_snapshot_foundation.md` § 11): the host process
that drives `/api/publish`, `/api/publish/recover` and `/api/publish/delivery`
exists, and crash-matrix stages 8 and 9 are closed **on the host side** by a
provider idempotency identity scoped to the publication operation id and by
committing "a submission may have happened" before the provider call. Still open
and unchanged: no live e-mail provider has been selected or implemented (`SMTP`
declares `supports_idempotent_submit = false` and `supports_reconciliation =
false`, so an ambiguous submission stops for an operator), there is no fleet
orchestrator that enumerates eligible drivers, and every deployment gate in
„Deployment bindings still required" above is still open.

**Superseded again — the mailing question is closed.** The dashboard link is now
delivered by the **existing** Eco Driving weekly/monthly e-mail jobs
(`docs/28_driver_eco_dashboard_v1_snapshot_foundation.md` § 12). Those jobs
already were the fleet orchestrator, so none was built: they enumerate the
drivers, resolve the recipients, own the reporting period and send over example.invalid
SMTP with their existing `eco_*_email_send_log` idempotency. The dashboard side
stops at a publication-only seam (`publisher.ensure_capability()`), records a
terminal `EXTERNAL_MAILER_HANDOFF` and hands the capability URL over; the
dashboard-specific `SmtpEmailProvider` is not on that path and no live e-mail
provider needs choosing. A handoff whose capability has **expired** is not
handed over: the host destroys the dead bearer, rotates through the existing
`POST /api/publish/recover` (same operation id, same subject binding, same
payload digest) and hands the fresh capability over under the unchanged
delivery identity, so a delayed rerun stays autonomous without a second
mechanism and without any SMTP claim.

Two invariants make that boundary structural rather than conventional. The
ledger row records `external_mailer` in the INSERT that **creates** it, and
migration 049 makes the column immutable in both directions and refuses a
provider identity on any row that carries it — so there is no state, not even a
crash between publication and handoff, that the provider lifecycle may adopt.
And an Eco send whose SMTP result was **ambiguous** blocks the next run before
the dashboard step, so a message the send ledger has ruled out causes no
publication and no capability rotation; see `docs/28_…` § 12.10 and
`docs/07_operations.md`.

Still open and unchanged: every deployment gate in
„Deployment bindings still required" above, and migration 049 on any persistent
database.
