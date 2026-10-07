/* Harness for the publication transaction gate.
 *
 * Executes the REAL Worker and the REAL publication module against
 * projection- AND constraint-accurate in-memory D1/R2 bindings. No wrangler,
 * no credentials, no remote Cloudflare resource, no e-mail.
 *
 * WHAT MAKES THIS DIFFERENT FROM THE PREVIOUS "CONCURRENCY" TESTS
 *
 * They ran several promises and hoped the scheduler produced a race. It
 * usually did not, which is why a confirmed fan-out defect passed them. Here
 * every race is FORCED: the in-memory bindings park a caller at a named
 * transition site and the test releases callers in a chosen order, so the
 * interleaving under test is the one that is actually executed, every run.
 *
 * The Worker and its libraries contain no test hook of any kind — the barriers
 * live entirely in the local doubles — so the code exercised here is
 * byte-identical to the code that would deploy.
 *
 * Capabilities, session ids, machine credentials and object keys are synthetic
 * and are NEVER printed: scenarios report booleans, counts and lengths.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";
import { edgeRequest } from "./eco_edge_framing.mjs";
import { readFile } from "node:fs/promises";
import { canonicalFixture, canonicalText } from "./eco_canonical_fixture.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const DELIVERY = path.join(REPO, "delivery", "driver_eco_dashboard");
const ASSET_ROOT = path.join(REPO, "assets", "driver_eco_dashboard");

await import(path.join(DELIVERY, "local", "node_runtime.js"));

const worker = (await import(path.join(DELIVERY, "worker", "index.js"))).default;
const bindings = await import(path.join(DELIVERY, "local", "memory_bindings.js"));
const publisherAuth = await import(path.join(DELIVERY, "worker", "lib", "publisher_auth.js"));
const capabilityLib = await import(path.join(DELIVERY, "worker", "lib", "capability.js"));
const publication = await import(path.join(DELIVERY, "worker", "lib", "publication.js"));
const { D1AuthorizationStore } = await import(path.join(DELIVERY, "worker", "lib", "store.js"));

const { SITE, Barrier } = bindings;

const ORIGIN = "https://dashboard.example.invalid";
/* Cloudflare sets this on every dispatched request; the session rate limiter
 * keys on it. Synthetic TEST-NET-3 address, never a real peer. */
const CLIENT_IP = "203.0.113.10";
const T0 = 1_800_000_000;
const PUBLISHER_TOKEN = capabilityLib.generateCapability();
const FIXTURE_A = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_acceptable.json"));
const FIXTURE_B = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_safe.json"));

/* ------------------------------------------------------------------ world -- */

async function createWorld(options) {
  const settings = options || {};
  const db = new bindings.MemoryD1();
  const bucket = new bindings.MemoryR2();
  const logs = [];
  const clock = { now: settings.now || T0 };
  const env = bindings.createEnv({
    db, bucket,
    assets: bindings.createAssetsBinding(ASSET_ROOT),
    assetRoot: ASSET_ROOT,
    pepper: settings.pepper,
    now: () => clock.now,
    logSink: { log: (l) => logs.push(l), error: (l) => logs.push(l) },
  });
  env.PUBLISHER_KEY_DIGEST = await publisherAuth.publisherKeyDigest(PUBLISHER_TOKEN, settings.pepper);
  return { db, bucket, env, logs, clock, store: new D1AuthorizationStore(db) };
}

async function call(world, request) {
  const response = await worker.fetch(request, world.env, {});
  const text = await response.clone().text();
  const headers = {};
  for (const [name, value] of response.headers) headers[name.toLowerCase()] = value;
  let json = null;
  try { json = JSON.parse(text); } catch (error) { json = null; }
  return { status: response.status, headers, text, json };
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  const view = new Uint8Array(digest);
  let out = "";
  for (let i = 0; i < view.length; i += 1) out += view[i].toString(16).padStart(2, "0");
  return out;
}

function operationId(seed) {
  return ("op-" + seed + "-").padEnd(24, "x").slice(0, 24);
}

async function publishRequest(options) {
  const settings = options || {};
  const body = settings.body === undefined ? FIXTURE_A : settings.body;
  const headers = {
    "Content-Type": "application/json",
    Authorization: "Publisher " + PUBLISHER_TOKEN,
    "X-Publication-Operation": settings.operationId,
    "X-Publication-Subject": settings.subjectRef === undefined ? "subject-alpha" : settings.subjectRef,
    "X-Publication-Payload-Digest": await sha256Hex(body),
    /* The reporting period decides the capability lifetime and has no default.
     * These scenarios are about the atomicity of the publication transaction,
     * so a well-formed period is supplied; the period contract itself is
     * proved by test_driver_eco_dashboard_capability_lifecycle.py. */
    "X-Publication-Period": settings.periodType === undefined ? "weekly" : settings.periodType,
  };
  return edgeRequest(ORIGIN + "/api/publish", { method: "POST", headers, body });
}

function controlRequest(pathname, operationId, extra) {
  const headers = Object.assign({
    "Content-Type": "application/json",
    Authorization: "Publisher " + PUBLISHER_TOKEN,
    "X-Publication-Operation": operationId,
    /* Read by `/api/publish/recover`, which mints a replacement grant and so
     * writes a fresh expiry. `/api/publish/delivery` ignores it. */
    "X-Publication-Period": "weekly",
  }, extra || {});
  return edgeRequest(ORIGIN + pathname, { method: "POST", headers, body: "" });
}

const publish = (world, op, options) =>
  publishRequest(Object.assign({ operationId: op }, options || {})).then((r) => call(world, r));
const recover = (world, op) => call(world, controlRequest("/api/publish/recover", op));
const delivery = (world, op, phase, capabilityId) =>
  call(world, controlRequest("/api/publish/delivery", op, {
    "X-Publication-Phase": phase,
    "X-Publication-Capability": capabilityId,
  }));

/* --- state readers -------------------------------------------------------- */

function liveGrants(world) {
  return [...world.db.capabilities.values()].filter((row) => !row.revoked_at && !row.rotated_to);
}

function distinctObjectKeys(world) {
  return new Set(world.bucket.putLog).size;
}

function operationRow(world, op) {
  return world.db.publications.get(op) || null;
}

/**
 * The one invariant every scenario asserts, expressed once.
 *
 * `authoritative_live_grants` counts grants that are simultaneously live AND
 * referenced by an operation ledger, which is the thing the review found could
 * exceed one. `orphan_live_grants` counts live grants no ledger references —
 * the state that made a bearer unrecoverable.
 */
function invariants(world, op) {
  const row = operationRow(world, op);
  const live = liveGrants(world);
  const referenced = new Set(
    [...world.db.publications.values()].map((r) => r.capability_id).filter(Boolean)
  );
  return {
    operations: world.db.publications.size,
    operation_state: row ? row.state : null,
    bearer_generation: row ? Number(row.bearer_generation) : null,
    owned_object_keys: new Set(
      [...world.db.publications.values()].map((r) => r.snapshot_object_key)
    ).size,
    distinct_objects_written: distinctObjectKeys(world),
    r2_objects: world.bucket.objects.size,
    total_grants: world.db.capabilities.size,
    live_grants: live.length,
    authoritative_live_grants: live.filter((g) => referenced.has(g.capability_id)).length,
    orphan_live_grants: live.filter((g) => !referenced.has(g.capability_id)).length,
    ledger_references_existing_grant:
      !row || !row.capability_id || world.db.capabilities.has(row.capability_id),
  };
}

/* -------------------------------------------------------------- scenarios -- */

const scenarios = {};

/* ============================ PART J: initial-publication contention ====== */

async function contention(count) {
  const world = await createWorld();
  const op = operationId("c" + count);
  const requests = [];
  for (let index = 0; index < count; index += 1) requests.push(await publishRequest({ operationId: op }));
  const responses = await Promise.all(requests.map((request) => call(world, request)));

  const created = responses.filter((r) => r.status === 201);
  const bearers = responses.filter((r) => r.json && r.json.capability);
  const bearerValues = new Set(bearers.map((r) => r.json.capability));
  return Object.assign({
    callers: count,
    status_201: created.length,
    status_200: responses.filter((r) => r.status === 200).length,
    other_status: responses.filter((r) => r.status !== 200 && r.status !== 201).length,
    raw_bearer_emissions: bearers.length,
    distinct_raw_bearers: bearerValues.size,
    idempotent_responses: responses.filter(
      (r) => r.json && r.json.status === publication.PUBLICATION_RESULT.ALREADY_PUBLISHED).length,
    published_result: created.length === 1 ? created[0].json.status : null,
    next_actions: [...new Set(responses.map((r) => r.json && r.json.next_action))].sort(),
  }, invariants(world, op));
}

scenarios.contention_2 = () => contention(2);
scenarios.contention_8 = () => contention(8);
scenarios.contention_32 = () => contention(32);
scenarios.contention_128 = () => contention(128);

/* Run the smallest contention case repeatedly: a race that only sometimes
 * loses is still a defect, so the assertion is over every repetition. */
scenarios.contention_repeated = async () => {
  const rounds = [];
  for (let round = 0; round < 25; round += 1) rounds.push(await contention(4));
  return {
    rounds: rounds.length,
    max_status_201: Math.max(...rounds.map((r) => r.status_201)),
    min_status_201: Math.min(...rounds.map((r) => r.status_201)),
    max_live_grants: Math.max(...rounds.map((r) => r.authoritative_live_grants)),
    max_distinct_objects: Math.max(...rounds.map((r) => r.distinct_objects_written)),
    max_bearers: Math.max(...rounds.map((r) => r.raw_bearer_emissions)),
    any_orphan: rounds.some((r) => r.orphan_live_grants > 0),
  };
};

/* ============================ PART I: forced interleavings ================ */

/**
 * Park `count` callers at `site` and release them together.
 *
 * This is the deterministic reproduction of the former fan-out: before the
 * fix, N callers released from the pre-grant boundary each minted a bearer.
 */
async function forcedRace(site, count, options) {
  const settings = options || {};
  const world = await createWorld();
  const barrier = new Barrier();
  const phase = settings.phase || "before";
  /* Parking at "after" means parking at a point where the transition has
   * ALREADY mutated the row. A caller that is still upstream would then read
   * the mutated state and take a different branch, so it would never arrive
   * and the rendezvous would be decided by scheduling luck rather than forced.
   * Gate "before" as well, so every caller is provably inside the transition
   * before any of them commits it. */
  if (phase === "after") barrier.hold(site, "before");
  barrier.hold(site, phase);
  barrier.attach(world.db);
  barrier.attach(world.bucket);
  const op = operationId("f" + site.slice(0, 6));

  const requests = [];
  for (let index = 0; index < count; index += 1) requests.push(await publishRequest({ operationId: op }));
  const pending = requests.map((request) => call(world, request));

  if (phase === "after") {
    await barrier.waitFor(site, count, "before");
    barrier.open(site, "before");
  }
  await barrier.waitFor(site, count, phase);
  const parked = barrier.count(site, phase);
  barrier.releaseAll();
  const responses = await Promise.all(pending);

  return Object.assign({
    site: site,
    parked_together: parked,
    status_201: responses.filter((r) => r.status === 201).length,
    raw_bearer_emissions: responses.filter((r) => r.json && r.json.capability).length,
    statuses: [...new Set(responses.map((r) => r.json && r.json.status))].sort(),
  }, invariants(world, op));
}

scenarios.forced_race_at_operation_claim = () => forcedRace(SITE.OPERATION_CLAIM, 8);
scenarios.forced_race_at_object_write = () => forcedRace(SITE.OBJECT_PUT, 8);
scenarios.forced_race_at_snapshot_written = () => forcedRace(SITE.SNAPSHOT_WRITTEN, 8);
scenarios.forced_race_before_grant_transaction = () => forcedRace(SITE.GRANT_TRANSACTION, 8);
scenarios.forced_race_after_grant_transaction = () =>
  forcedRace(SITE.GRANT_TRANSACTION, 8, { phase: "after" });

/**
 * Serialised release: park 32 callers at the grant boundary and let them
 * through ONE AT A TIME. Every caller after the first therefore runs its
 * transaction against a state that has already advanced — the exact ordering
 * a "check then write" implementation gets wrong.
 */
scenarios.forced_serialised_release_at_grant = async () => {
  const world = await createWorld();
  const barrier = new Barrier();
  barrier.hold(SITE.GRANT_TRANSACTION, "before");
  barrier.attach(world.db);
  const op = operationId("serial");

  const requests = [];
  for (let index = 0; index < 32; index += 1) requests.push(await publishRequest({ operationId: op }));
  const pending = requests.map((request) => call(world, request));
  await barrier.waitFor(SITE.GRANT_TRANSACTION, 32);

  const releaseOrder = [];
  while (barrier.count(SITE.GRANT_TRANSACTION) > 0) {
    releaseOrder.push(barrier.release(SITE.GRANT_TRANSACTION, 1));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  barrier.releaseAll();
  const responses = await Promise.all(pending);

  return Object.assign({
    released_one_at_a_time: releaseOrder.length,
    status_201: responses.filter((r) => r.status === 201).length,
    raw_bearer_emissions: responses.filter((r) => r.json && r.json.capability).length,
  }, invariants(world, op));
};

/* ============================ PART J: same-operation conflicts ============ */

scenarios.same_operation_conflicts = async () => {
  const world = await createWorld();
  const op = operationId("conflict");
  const first = await publish(world, op);
  const baseline = invariants(world, op);

  const differentSubject = await publish(world, op, { subjectRef: "subject-beta" });
  const differentPayload = await publish(world, op, { body: FIXTURE_B });
  const after = invariants(world, op);

  /* A conflicting claim on a brand-new operation id must also write nothing:
   * the claim insert is the only thing that ever ran. */
  const freshWorld = await createWorld();
  const freshOp = operationId("fresh");
  await publish(freshWorld, freshOp);
  const conflictOnFresh = await publish(freshWorld, freshOp, { subjectRef: "subject-other" });

  return {
    first_status: first.status,
    different_subject_status: differentSubject.status,
    different_subject_error: differentSubject.json && differentSubject.json.error,
    different_subject_next_action: differentSubject.json && differentSubject.json.next_action,
    different_payload_status: differentPayload.status,
    different_payload_error: differentPayload.json && differentPayload.json.error,
    nothing_mutated:
      after.total_grants === baseline.total_grants &&
      after.r2_objects === baseline.r2_objects &&
      after.operations === baseline.operations &&
      after.bearer_generation === baseline.bearer_generation,
    conflict_emitted_bearer:
      !!(differentSubject.json && differentSubject.json.capability) ||
      !!(differentPayload.json && differentPayload.json.capability),
    fresh_conflict_status: conflictOnFresh.status,
    fresh_world: invariants(freshWorld, freshOp),
  };
};

/* ============================ PART H/J: crash and failure windows ========= */

/**
 * Inject a failure at one window, then retry to convergence and assert the
 * end state. Each entry is one row of the host crash matrix.
 */
async function crashWindow(name, prepare) {
  const world = await createWorld();
  const op = operationId("cw" + name.slice(0, 4));
  const failed = await prepare(world, op);
  const during = invariants(world, op);
  /* Clear every injected fault and retry exactly as the host would. */
  world.db.failAt.clear();
  world.bucket.failAt.clear();
  world.db.failWrites = false;
  world.bucket.failPut = false;
  delete world.db.failInBatchAfter;
  const retry = await publish(world, op);
  const second = await publish(world, op);
  return {
    window: name,
    failure_status: failed ? failed.status : null,
    state_during: during.operation_state,
    grants_during: during.total_grants,
    orphan_during: during.orphan_live_grants,
    retry_status: retry.status,
    retry_result: retry.json && retry.json.status,
    retry_bearer: !!(retry.json && retry.json.capability),
    second_retry_result: second.json && second.json.status,
    second_retry_bearer: !!(second.json && second.json.capability),
    final: invariants(world, op),
  };
}

scenarios.crash_windows = async () => {
  const results = [];

  /* (3) Worker owns the operation and its key, then dies before the R2 write. */
  results.push(await crashWindow("before_r2_write", async (world, op) => {
    world.bucket.failAt.set(SITE.OBJECT_PUT, "R2_UNAVAILABLE");
    return publish(world, op);
  }));

  /* (4) R2 write succeeded; the process dies before the grant transaction. */
  results.push(await crashWindow("after_r2_before_grant", async (world, op) => {
    world.db.failAt.set(SITE.GRANT_TRANSACTION, "D1_UNAVAILABLE");
    return publish(world, op);
  }));

  /* (4b) The snapshot-written transition itself fails. */
  results.push(await crashWindow("at_snapshot_written", async (world, op) => {
    world.db.failAt.set(SITE.SNAPSHOT_WRITTEN, "D1_UNAVAILABLE");
    return publish(world, op);
  }));

  /* Rollback INSIDE the grant transaction: the first statement ran, then the
   * transaction aborted. Nothing may survive — not the capability, not the
   * ledger move. */
  results.push(await crashWindow("inside_grant_transaction", async (world, op) => {
    world.db.failInBatchAfter = 1;
    return publish(world, op);
  }));

  /* (1) The operation was never created at all. */
  results.push(await crashWindow("before_any_request", async () => null));

  return results;
};

/**
 * (5)/(6) The transaction committed and the response was lost — the one window
 * in which a live bearer exists that the host does not have. The server side
 * must be unambiguous: the operation is grant-authoritative, a retry mints
 * nothing, and recovery is the named next step.
 */
scenarios.response_loss_after_commit = async () => {
  const world = await createWorld();
  const barrier = new Barrier();
  barrier.hold(SITE.GRANT_TRANSACTION, "after");
  barrier.attach(world.db);
  const op = operationId("lost-resp");

  const pending = publish(world, op);
  await barrier.waitFor(SITE.GRANT_TRANSACTION, 1, "after");
  /* The transaction has committed; the caller has not yet been answered. This
   * is exactly the state a crashed host leaves behind. */
  const committed = invariants(world, op);
  barrier.releaseAll();
  await pending;

  /* The host lost the bearer. A plain retry must not mint another one. */
  const retry = await publish(world, op);
  return {
    state_at_commit: committed.operation_state,
    grants_at_commit: committed.total_grants,
    ledger_referenced_at_commit: committed.ledger_references_existing_grant,
    orphan_at_commit: committed.orphan_live_grants,
    retry_status: retry.status,
    retry_result: retry.json && retry.json.status,
    retry_next_action: retry.json && retry.json.next_action,
    retry_bearer: !!(retry.json && retry.json.capability),
    retry_bearer_recoverable: retry.json && retry.json.bearer_recoverable,
    final: invariants(world, op),
  };
};

/* ============================ PART K: recovery ============================ */

async function publishedWorld(seed) {
  const world = await createWorld();
  const op = operationId(seed);
  const published = await publish(world, op);
  return { world, op, published, capability: published.json.capability,
           capabilityId: published.json.capability_id };
}

scenarios.recovery_normal_and_retry = async () => {
  const { world, op, capability, capabilityId } = await publishedWorld("recov");

  const recovered = await recover(world, op);
  const afterFirst = invariants(world, op);

  /* The old bearer must be dead and the new one alive. */
  const exchange = async (raw) => call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: raw }),
  }));
  const oldExchange = await exchange(capability);
  const newExchange = await exchange(recovered.json.capability);

  /* A second explicit recovery is still exactly one replacement. */
  const again = await recover(world, op);

  return {
    recovered_status: recovered.status,
    recovered_result: recovered.json.status,
    recovered_bearer: !!recovered.json.capability,
    recovered_is_different: recovered.json.capability !== capability,
    superseded_predecessor: recovered.json.operation.capability_id !== capabilityId,
    generation_after_first: recovered.json.operation.bearer_generation,
    after_first: afterFirst,
    old_bearer_exchange: oldExchange.status,
    new_bearer_exchange: newExchange.status,
    second_recovery_status: again.status,
    second_recovery_result: again.json.status,
    generation_after_second: again.json.operation.bearer_generation,
    after_second: invariants(world, op),
  };
};

async function concurrentRecoveries(count) {
  const { world, op } = await publishedWorld("cr" + count);
  const responses = await Promise.all(
    Array.from({ length: count }, () => recover(world, op))
  );
  const winners = responses.filter((r) => r.status === 200 && r.json.capability);
  return Object.assign({
    callers: count,
    recovered: winners.length,
    distinct_replacement_bearers: new Set(winners.map((r) => r.json.capability)).size,
    refused: responses.filter((r) => r.status === 409).length,
    refusal_reasons: [...new Set(responses.filter((r) => r.status === 409)
      .map((r) => r.json && r.json.reason))].sort(),
  }, invariants(world, op));
}

scenarios.recovery_concurrent_2 = () => concurrentRecoveries(2);
scenarios.recovery_concurrent_8 = () => concurrentRecoveries(8);
scenarios.recovery_concurrent_32 = () => concurrentRecoveries(32);

/** Forced: N recoveries parked at the transaction boundary, released together. */
scenarios.recovery_forced_race = async () => {
  const { world, op } = await publishedWorld("recrace");
  const barrier = new Barrier();
  barrier.hold(SITE.RECOVERY_TRANSACTION, "before");
  barrier.attach(world.db);

  const pending = Array.from({ length: 16 }, () => recover(world, op));
  await barrier.waitFor(SITE.RECOVERY_TRANSACTION, 16);
  const parked = barrier.count(SITE.RECOVERY_TRANSACTION);
  barrier.releaseAll();
  const responses = await Promise.all(pending);

  return Object.assign({
    parked_together: parked,
    recovered: responses.filter((r) => r.json && r.json.capability).length,
    refused: responses.filter((r) => r.status === 409).length,
  }, invariants(world, op));
};

scenarios.recovery_edge_cases = async () => {
  const world = await createWorld();
  const cases = {};

  /* No operation at all. */
  cases.unknown_operation = (await recover(world, operationId("nothing"))).status;

  /* Operation exists but has no grant yet: recovery is refused with the
   * reason that tells the host to retry publish, not to issue afresh. */
  const noGrantWorld = await createWorld();
  const noGrantOp = operationId("nogrant");
  noGrantWorld.db.failAt.set(SITE.GRANT_TRANSACTION, "D1_UNAVAILABLE");
  await publish(noGrantWorld, noGrantOp);
  noGrantWorld.db.failAt.clear();
  const noGrant = await recover(noGrantWorld, noGrantOp);
  cases.no_grant_yet = {
    status: noGrant.status, reason: noGrant.json && noGrant.json.reason,
    next_action: noGrant.json && noGrant.json.next_action,
    grants: noGrantWorld.db.capabilities.size,
  };

  /* Recovery racing an independent revoke of the current grant. */
  const revokeWorld = await publishedWorld("revrace");
  await revokeWorld.world.store.revokeCapability(revokeWorld.capabilityId, T0 + 1);
  const afterRevoke = await recover(revokeWorld.world, revokeWorld.op);
  cases.racing_revoke = {
    status: afterRevoke.status,
    reason: afterRevoke.json && afterRevoke.json.reason,
    invariants: invariants(revokeWorld.world, revokeWorld.op),
  };

  /* Transaction failure during recovery, then a clean retry. */
  const failWorld = await publishedWorld("recfail");
  failWorld.world.db.failAt.set(SITE.RECOVERY_TRANSACTION, "D1_UNAVAILABLE");
  const failed = await recover(failWorld.world, failWorld.op);
  const during = invariants(failWorld.world, failWorld.op);
  failWorld.world.db.failAt.clear();
  const afterRetry = await recover(failWorld.world, failWorld.op);
  cases.transaction_failure = {
    failed_status: failed.status,
    grants_during: during.total_grants,
    orphan_during: during.orphan_live_grants,
    retry_status: afterRetry.status,
    retry_generation: afterRetry.json && afterRetry.json.operation.bearer_generation,
    invariants: invariants(failWorld.world, failWorld.op),
  };

  /* Rollback inside the recovery transaction. */
  const rollbackWorld = await publishedWorld("recroll");
  rollbackWorld.world.db.failInBatchAfter = 1;
  const rolled = await recover(rollbackWorld.world, rollbackWorld.op);
  const afterRollback = invariants(rollbackWorld.world, rollbackWorld.op);
  delete rollbackWorld.world.db.failInBatchAfter;
  const rollbackRetry = await recover(rollbackWorld.world, rollbackWorld.op);
  cases.rollback_inside_transaction = {
    status: rolled.status,
    grants_after_rollback: afterRollback.total_grants,
    generation_after_rollback: afterRollback.bearer_generation,
    orphan_after_rollback: afterRollback.orphan_live_grants,
    retry_status: rollbackRetry.status,
    invariants: invariants(rollbackWorld.world, rollbackWorld.op),
  };

  /* Response loss after a successful recovery, then a further retry. The
   * replacement is unrecoverable by design, so the next retry rotates again —
   * and still leaves exactly one live grant. */
  const lossWorld = await publishedWorld("recloss");
  const barrier = new Barrier();
  barrier.hold(SITE.RECOVERY_TRANSACTION, "after");
  barrier.attach(lossWorld.world.db);
  const pending = recover(lossWorld.world, lossWorld.op);
  await barrier.waitFor(SITE.RECOVERY_TRANSACTION, 1, "after");
  const atCommit = invariants(lossWorld.world, lossWorld.op);
  barrier.releaseAll();
  await pending;
  const afterLoss = await recover(lossWorld.world, lossWorld.op);
  cases.response_loss_after_recovery = {
    at_commit_live: atCommit.authoritative_live_grants,
    at_commit_orphan: atCommit.orphan_live_grants,
    at_commit_generation: atCommit.bearer_generation,
    retry_status: afterLoss.status,
    retry_generation: afterLoss.json && afterLoss.json.operation.bearer_generation,
    invariants: invariants(lossWorld.world, lossWorld.op),
  };

  /* Recovery while a plain publish retry is in flight. */
  const mixedWorld = await publishedWorld("recmix");
  const [publishRetry, recovery] = await Promise.all([
    publish(mixedWorld.world, mixedWorld.op),
    recover(mixedWorld.world, mixedWorld.op),
  ]);
  cases.recovery_during_publish_retry = {
    publish_status: publishRetry.status,
    publish_bearer: !!(publishRetry.json && publishRetry.json.capability),
    recovery_status: recovery.status,
    invariants: invariants(mixedWorld.world, mixedWorld.op),
  };

  return cases;
};

/* ============================ PART E: delivery vs recovery ================ */

scenarios.delivery_then_recovery = async () => {
  const { world, op, capability, capabilityId } = await publishedWorld("delrec");
  const intent = await delivery(world, op, "INTENT", capabilityId);
  const delivered = await delivery(world, op, "DELIVERED", capabilityId);

  /* Terminal. Recovery must be refused, and the delivered bearer must keep
   * working — re-minting here would break a link the driver already has. */
  const refused = await recover(world, op);
  const exchange = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: capability }),
  }));
  const republish = await publish(world, op);

  return Object.assign({
    intent_result: intent.json.status,
    delivered_result: delivered.json.status,
    delivered_state: delivered.json.operation.state,
    recovery_status: refused.status,
    recovery_reason: refused.json && refused.json.reason,
    delivered_bearer_still_valid: exchange.status,
    republish_result: republish.json && republish.json.status,
    republish_next_action: republish.json && republish.json.next_action,
    republish_bearer: !!(republish.json && republish.json.capability),
  }, invariants(world, op));
};

/**
 * THE race the review demanded: a recovery that read a recoverable state and
 * then commits after delivery won. The recovery is parked at its transaction
 * boundary, DELIVERED is driven to completion, and only then is the recovery
 * released — so its predicate is evaluated against a DELIVERED operation.
 */
scenarios.forced_recovery_commits_after_delivery = async () => {
  const { world, op, capability, capabilityId } = await publishedWorld("racedel");
  await delivery(world, op, "INTENT", capabilityId);

  const barrier = new Barrier();
  barrier.hold(SITE.RECOVERY_TRANSACTION, "before");
  barrier.attach(world.db);

  const recovery = recover(world, op);
  await barrier.waitFor(SITE.RECOVERY_TRANSACTION, 1);
  const stateWhenRecoveryDecided = operationRow(world, op).state;

  /* Delivery wins while the recovery is held at its own transaction. */
  const delivered = await delivery(world, op, "DELIVERED", capabilityId);

  barrier.releaseAll();
  const recoveryResult = await recovery;

  const exchange = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: capability }),
  }));

  return Object.assign({
    state_when_recovery_decided: stateWhenRecoveryDecided,
    delivered_result: delivered.json.status,
    recovery_status: recoveryResult.status,
    recovery_reason: recoveryResult.json && recoveryResult.json.reason,
    recovery_emitted_bearer: !!(recoveryResult.json && recoveryResult.json.capability),
    delivered_bearer_still_valid: exchange.status,
  }, invariants(world, op));
};

/**
 * The mirror image: a delivery decided on a bearer that a recovery has since
 * superseded. Terminalising there would mark DELIVERED a grant the driver
 * never received, so it must be refused.
 */
scenarios.forced_delivery_on_superseded_bearer = async () => {
  const { world, op, capabilityId } = await publishedWorld("racesup");
  await delivery(world, op, "INTENT", capabilityId);

  const barrier = new Barrier();
  barrier.hold(SITE.DELIVERY_TRANSITION, "before");
  barrier.attach(world.db);

  /* The host decided to deliver the ORIGINAL bearer. */
  const deliveryCall = delivery(world, op, "DELIVERED", capabilityId);
  await barrier.waitFor(SITE.DELIVERY_TRANSITION, 1);

  /* Recovery wins first, superseding that bearer. It needs the D1 barrier
   * lifted for its own site, which it already is: only DELIVERY_TRANSITION is
   * held. */
  const recovered = await recover(world, op);

  barrier.releaseAll();
  const deliveryResult = await deliveryCall;

  /* The host, told its bearer was superseded, delivers the current one. */
  const corrected = await delivery(world, op, "DELIVERED", recovered.json.capability_id);

  return Object.assign({
    recovery_status: recovered.status,
    stale_delivery_status: deliveryResult.status,
    stale_delivery_result: deliveryResult.json && deliveryResult.json.status,
    state_after_stale_delivery: deliveryResult.json && deliveryResult.json.operation.state,
    corrected_delivery_result: corrected.json && corrected.json.status,
    corrected_state: corrected.json && corrected.json.operation.state,
  }, invariants(world, op));
};

/* ============================ PART B/S: structural guarantees ============= */

/**
 * The grant/ledger atomicity gap, asserted against the database contract
 * rather than against the order of two writes: the state the review observed —
 * a live grant with the operation still in SNAPSHOT_WRITTEN — is now
 * unrepresentable.
 */
scenarios.grant_ledger_atomicity = async () => {
  const world = await createWorld();
  const op = operationId("atomic");
  await publish(world, op);

  /* Try to reproduce the reviewed state directly against the store. */
  let unrepresentable = null;
  try {
    await world.db.prepare(
      `UPDATE eco_publication_operation SET state = ?3, updated_at = ?4
        WHERE operation_id = ?1 AND state = ?2 AND capability_id = ?5`
    ).bind(op, "GRANT_MINTED", "SNAPSHOT_WRITTEN", T0, operationRow(world, op).capability_id).run();
    unrepresentable = "accepted";
  } catch (error) {
    unrepresentable = String(error.message).includes("chk_eco_publication_grant_ledger")
      ? "refused_by_check" : "refused:" + error.message;
  }

  /* Two operations may not claim the same capability or the same object key. */
  const secondOp = operationId("atomic2");
  await publish(world, secondOp, { subjectRef: "subject-beta" });
  const rowOne = operationRow(world, op);
  let duplicateCapability = null;
  try {
    /* Byte-identical to the production recovery ledger move, so the probe
     * exercises the real statement rather than a paraphrase of it. That
     * statement now also pins the object identity the recovery gate proved
     * (`payload_digest`, `snapshot_object_key`), so the probe binds the second
     * operation's real values — otherwise the guard, not the unique index,
     * would be what refused the write and the probe would prove nothing. */
    const rowTwo = operationRow(world, secondOp);
    await world.db.prepare(
      `UPDATE eco_publication_operation
          SET capability_id = ?2,
              bearer_generation = bearer_generation + 1,
              updated_at = ?3
        WHERE operation_id = ?1
          AND capability_id = ?4
          AND subject_ref = ?5
          AND state IN (?6, ?7)
          AND payload_digest = ?8
          AND snapshot_object_key = ?9
          AND EXISTS (SELECT 1 FROM eco_capability WHERE capability_id = ?2)`
    ).bind(secondOp, rowOne.capability_id, T0, rowTwo.capability_id,
           "subject-beta", "GRANT_MINTED", "DELIVERY_INTENT_RECORDED",
           rowTwo.payload_digest, rowTwo.snapshot_object_key).run();
    duplicateCapability = "accepted";
  } catch (error) {
    duplicateCapability = String(error.message).includes("uq_eco_publication_capability")
      ? "refused_by_unique_index" : "refused:" + error.message;
  }

  const source = await readFile(path.join(DELIVERY, "worker", "lib", "publication.js"), "utf8");
  const storeSource = await readFile(path.join(DELIVERY, "worker", "lib", "store.js"), "utf8");
  const publisherSource = await readFile(path.join(DELIVERY, "worker", "lib", "publisher.js"), "utf8");
  const indexSource = await readFile(path.join(DELIVERY, "worker", "index.js"), "utf8");

  return {
    ledger_without_grant: unrepresentable,
    duplicate_capability_reference: duplicateCapability,
    /* Both authoritative transitions go through db.batch — one transaction. */
    grant_transaction_uses_batch: /mintGrantTransactionally[\s\S]*?this\.db\.batch\(/.test(source),
    recovery_transaction_uses_batch: /recoverGrantTransactionally[\s\S]*?this\.db\.batch\(/.test(source),
    /* No unconditional grant insert survives anywhere under worker/. */
    store_has_no_insert_capability: !/async insertCapability\s*\(/.test(storeSource),
    publisher_has_no_issue_capability: !/export async function issueCapability/.test(publisherSource),
    worker_never_imports_dev_grants: !indexSource.includes("dev_grants"),
    /* The recovery path does not go via the generic rotate + separate update. */
    recovery_avoids_generic_rotation: !/rotateCapability/.test(source),
  };
};

/**
 * PART S — every publisher route, and what each one is allowed to reach.
 */
scenarios.publisher_routes_use_the_contract = async () => {
  const indexSource = await readFile(path.join(DELIVERY, "worker", "index.js"), "utf8");
  const routes = [...indexSource.matchAll(/url\.pathname === "([^"]+)"/g)].map((m) => m[1]);
  const publisherRoutes = routes.filter((r) => r.startsWith("/api/publish"));

  const world = await createWorld();
  const reachable = {};
  for (const route of publisherRoutes) {
    /* Unauthenticated: every publisher route must be indistinguishable from a
     * route that does not exist. */
    const anonymous = await call(world, edgeRequest(ORIGIN + route, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "",
    }));
    reachable[route] = anonymous.status;
  }

  /* Handler bodies must reach the publication contract and nothing else. */
  const handlers = {
    handlePublish: indexSource.slice(indexSource.indexOf("async function handlePublish("),
                                     indexSource.indexOf("async function handlePublishRecover(")),
    handlePublishRecover: indexSource.slice(indexSource.indexOf("async function handlePublishRecover("),
                                            indexSource.indexOf("async function handlePublishDelivery(")),
    handlePublishDelivery: indexSource.slice(indexSource.indexOf("async function handlePublishDelivery("),
                                             indexSource.indexOf("async function sha256Hex(")),
  };

  return {
    publisher_routes: publisherRoutes,
    anonymous_status: reachable,
    publish_uses_transaction: /publishSnapshot\(/.test(handlers.handlePublish),
    recover_uses_transaction: /recoverLostBearer\(/.test(handlers.handlePublishRecover),
    delivery_uses_transaction: /recordDeliveryPhase\(/.test(handlers.handlePublishDelivery),
    delivery_requires_capability: /X-Publication-Capability/.test(handlers.handlePublishDelivery),
    /* No handler may create a grant, mint a key or write an object itself. */
    no_handler_inserts_a_grant: Object.values(handlers)
      .every((body) => !/insertCapability|issueCapability|issueDevCapability/.test(body)),
    no_handler_puts_an_object: Object.values(handlers)
      .every((body) => !/putSnapshotObject\(/.test(body)),
    all_handlers_authorise: Object.values(handlers)
      .every((body) => /authorisePublisher\(/.test(body)),
  };
};

/**
 * The object-ownership model, stated as observable facts.
 */
scenarios.object_ownership = async () => {
  const world = await createWorld();
  const op = operationId("owner");
  const first = await publish(world, op);
  const ownedKey = operationRow(world, op).snapshot_object_key;

  /* Retries reuse the owned key; they never mint another object. */
  await publish(world, op);
  await publish(world, op);
  const keysAfterRetries = new Set(world.bucket.putLog);

  /* A failed write BEFORE ownership is committed cannot orphan anything,
   * because the key is claimed in the same statement that creates the row. */
  const failedWorld = await createWorld();
  const failedOp = operationId("orphan");
  failedWorld.bucket.failAt.set(SITE.OBJECT_PUT, "R2_UNAVAILABLE");
  await publish(failedWorld, failedOp);
  const orphanState = {
    objects: failedWorld.bucket.objects.size,
    operations: failedWorld.db.publications.size,
    owned_key_present: !!operationRow(failedWorld, failedOp).snapshot_object_key,
  };
  failedWorld.bucket.failAt.clear();
  const recoveredPublish = await publish(failedWorld, failedOp);
  const reusedKey = operationRow(failedWorld, failedOp).snapshot_object_key;

  return {
    publish_status: first.status,
    key_is_opaque: /^[0-9a-f]{2}\/[A-Za-z0-9_-]{32}\.json$/.test(ownedKey),
    keys_written_after_three_publishes: keysAfterRetries.size,
    r2_objects: world.bucket.objects.size,
    response_leaks_object_key: first.text.includes(ownedKey),
    orphan_state: orphanState,
    retry_reused_owned_key: reusedKey === operationRow(failedWorld, failedOp).snapshot_object_key,
    retry_status: recoveredPublish.status,
    after_retry: invariants(failedWorld, failedOp),
  };
};

/* ------------------------------------------------------------------- run --- */

const only = process.argv[2];
const output = {};
for (const [name, fn] of Object.entries(scenarios)) {
  if (only && only !== name) continue;
  try {
    output[name] = await fn();
  } catch (error) {
    output[name] = { harness_error: String(error && error.message), stack: String(error && error.stack).slice(0, 600) };
  }
}
process.stdout.write(JSON.stringify(output, null, 1));
