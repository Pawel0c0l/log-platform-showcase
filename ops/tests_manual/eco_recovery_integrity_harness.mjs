/* Recovery-integrity harness for the Driver Eco Dashboard publication boundary.
 *
 * THE DEFECT UNDER TEST
 *
 * `recoverLostBearer()` consulted D1 alone. It revoked the predecessor grant,
 * inserted a replacement, advanced the bearer generation, moved the
 * publication's capability identity and returned a NEW raw bearer — all
 * without ever asking whether the authoritative snapshot object that bearer
 * would point at still existed, still hashed to the ledger digest, still
 * carried its digest metadata, or was even readable.
 *
 * Confirmed before the fix, and reproduced by `r_recurrence_*` below:
 *
 *     R2 unreadable -> recoverLostBearer() -> RECOVERED, generation 1 -> 2,
 *     predecessor superseded, one new bearer handed out.
 *
 * WHAT EVERY SCENARIO BELOW RECORDS
 *
 * A refusal is only as good as the state it is measured against, so each case
 * captures, on both sides of the recovery call: the publication state, the
 * capability/grant id, the bearer generation, the grant row count, the live
 * grant count, the predecessor's revocation and rotation columns, the exact
 * stored octets, the R2 put count and the number of raw bearers returned. It
 * also records whether any recovery-transaction statement reached D1 at all,
 * which is what makes "the gate is a PREcondition" a measurement rather than a
 * claim about source order.
 *
 * Canonical bytes arrive on stdin, base64, produced by the real Python
 * publisher. Nothing here builds a snapshot in JavaScript.
 *
 * Synthetic data only. No wrangler, no credentials, no remote resource, no
 * e-mail. No capability, session id, credential or object key is printed.
 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { edgeRequest } from "./eco_edge_framing.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const DELIVERY = path.join(REPO, "delivery", "driver_eco_dashboard");
const ASSET_ROOT = path.join(REPO, "assets", "driver_eco_dashboard");

await import(path.join(DELIVERY, "local", "node_runtime.js"));

const worker = (await import(path.join(DELIVERY, "worker", "index.js"))).default;
const bindings = await import(path.join(DELIVERY, "local", "memory_bindings.js"));
const publisherAuth = await import(path.join(DELIVERY, "worker", "lib", "publisher_auth.js"));
const capabilityLib = await import(path.join(DELIVERY, "worker", "lib", "capability.js"));
const digestLib = await import(path.join(DELIVERY, "worker", "lib", "digest.js"));
const publicationLib = await import(path.join(DELIVERY, "worker", "lib", "publication.js"));
const publisherLib = await import(path.join(DELIVERY, "worker", "lib", "publisher.js"));

/* ------------------------------------------------------------------ input -- */

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));

const b64 = (value) => new Uint8Array(Buffer.from(value, "base64"));
const CANONICAL_A = b64(input.canonical_a_base64);
const CANONICAL_B = b64(input.canonical_b_base64);
const DIGEST_A = input.digest_a;
const DIGEST_B = input.digest_b;

const ORIGIN = "https://dashboard.example.invalid";
const T0 = 1_800_000_000;
const PEPPER = "synthetic-recovery-integrity-pepper";
const TOKEN = capabilityLib.generateCapability();
const SUBJECT = "subject-recovery-integrity";

/* ------------------------------------------------------------- mutation --- */

/**
 * Load a MUTANT copy of `publisher.js` with one shape rule reverted, so an
 * assertion can be shown to be load-bearing rather than incidentally
 * satisfied. The copy lives in a temporary directory OUTSIDE the repository
 * and its relative specifiers are rewritten to the real modules, so nothing
 * under `delivery/` is modified and no other module sees the mutant.
 */
const LIB_DIR = path.join(DELIVERY, "worker", "lib");

async function loadMutantPublisher(name, mutate) {
  const source = await readFile(path.join(LIB_DIR, "publisher.js"), "utf8");
  const mutated = mutate(source);
  if (mutated === null) return { error: "mutation site not located" };
  const rewritten = mutated.replace(/from "\.\//g, `from "${path.join(LIB_DIR, "/")}`);
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "eco-publisher-mutant-"));
  const modulePath = path.join(tempDir, name + ".mjs");
  await writeFile(modulePath, rewritten, "utf8");
  const module = await import(modulePath);
  return { module: module, cleanup: () => rm(tempDir, { recursive: true, force: true }) };
}

/* ------------------------------------------------------------------ world -- */

async function createWorld() {
  const db = new bindings.MemoryD1();
  const bucket = new bindings.MemoryR2();
  const logs = [];
  const env = bindings.createEnv({
    db, bucket,
    assets: bindings.createAssetsBinding(ASSET_ROOT),
    assetRoot: ASSET_ROOT,
    pepper: PEPPER,
    now: () => T0,
    logSink: { log: (l) => logs.push(l), error: (l) => logs.push(l) },
  });
  env.PUBLISHER_KEY_DIGEST = await publisherAuth.publisherKeyDigest(TOKEN, PEPPER);
  return { db, bucket, env, logs };
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

function operationId(seed) {
  return ("op-recov-" + seed + "-").padEnd(24, "x").slice(0, 24);
}

const publish = (world, op, options) => {
  const settings = options || {};
  return call(world, edgeRequest(ORIGIN + "/api/publish", {
    method: "POST",
    headers: new Headers([
      ["Content-Type", "application/json"],
      ["Authorization", "Publisher " + TOKEN],
      ["X-Publication-Operation", op],
      ["X-Publication-Subject", settings.subjectRef || SUBJECT],
      ["X-Publication-Payload-Digest", settings.digest || DIGEST_A],
      ["X-Publication-Period", settings.periodType || "weekly"],
    ]),
    body: settings.body || CANONICAL_A,
  }));
};

const recover = (world, op) => call(world, edgeRequest(ORIGIN + "/api/publish/recover", {
  method: "POST",
  headers: new Headers([
    ["Authorization", "Publisher " + TOKEN],
    ["X-Publication-Operation", op],
    /* A rotation writes a fresh expiry, so recovery restates the period too. */
    ["X-Publication-Period", "weekly"],
  ]),
}));

const delivery = (world, op, phase, capabilityId) =>
  call(world, edgeRequest(ORIGIN + "/api/publish/delivery", {
    method: "POST",
    headers: new Headers([
      ["Authorization", "Publisher " + TOKEN],
      ["X-Publication-Operation", op],
      ["X-Publication-Phase", phase],
      ["X-Publication-Capability", capabilityId],
    ]),
  }));

const exchange = (world, raw) => call(world, edgeRequest(ORIGIN + "/api/session", {
  method: "POST",
  /* Cloudflare sets this on every dispatched request; the session rate
   * limiter keys on it. Synthetic TEST-NET-3 address. */
  headers: { "CF-Connecting-IP": "203.0.113.10", "Content-Type": "application/json", Origin: ORIGIN },
  body: JSON.stringify({ capability: raw }),
}));

const readSnapshot = (world, cookie) => call(world, edgeRequest(ORIGIN + "/api/snapshot", {
  method: "GET", headers: { Cookie: cookie, Origin: ORIGIN },
}));

function hex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* ------------------------------------------------------- state measurement -- */

/**
 * Everything a zero-authorization-mutation claim has to be measured against.
 *
 * Deliberately NOT just the HTTP status: the whole point of the finding is
 * that a 200 and a rotated generation were both produced over an object that
 * could not be proven, so every column the recovery transaction would touch is
 * read directly out of the doubles.
 */
function authorizationState(world, op, predecessorId) {
  const row = world.db.publications.get(op) || null;
  const key = row ? row.snapshot_object_key : null;
  const stored = key ? world.bucket.objects.get(key) : null;
  const grants = [...world.db.capabilities.values()];
  const predecessor = predecessorId ? world.db.capabilities.get(predecessorId) : null;
  return {
    state: row ? row.state : null,
    capability_id: row ? row.capability_id : null,
    bearer_generation: row ? Number(row.bearer_generation) : null,
    ledger_digest: row ? row.payload_digest : null,
    grant_rows: grants.length,
    live_grants: grants.filter((g) => !g.revoked_at && !g.rotated_to).length,
    predecessor_revoked_at: predecessor ? (predecessor.revoked_at === undefined
      ? null : predecessor.revoked_at) : null,
    predecessor_rotated_to_present: !!(predecessor && predecessor.rotated_to),
    predecessor_is_current: !!(row && predecessorId && row.capability_id === predecessorId),
    puts: world.bucket.putLog.length,
    bytes_hex: stored ? hex(stored.bytes) : null,
    metadata: stored ? Object.assign({}, stored.customMetadata) : null,
  };
}

/* Which recovery-transaction statements reached D1 during a window. If the
 * integrity gate is a genuine PREcondition this count is zero for every
 * refused case — the transaction is never even attempted. */
function recoveryStatementsSince(world, mark) {
  return world.db.statementLog.slice(mark).filter((sql) =>
    (sql.startsWith("INSERT INTO eco_capability") &&
      sql.includes("FROM eco_publication_operation") &&
      !sql.includes("capability_id IS NULL")) ||
    (sql.startsWith("UPDATE eco_publication_operation") &&
      sql.includes("bearer_generation = bearer_generation + 1")) ||
    sql.startsWith("UPDATE eco_capability SET revoked_at = ?2, rotated_to = ?3")
  ).length;
}

/** Publish one healthy operation and return everything the tests need. */
async function publishedWorld(seed) {
  const world = await createWorld();
  const op = operationId(seed);
  const published = await publish(world, op);
  const row = world.db.publications.get(op);
  return {
    world, op,
    objectKey: row.snapshot_object_key,
    capability: published.json.capability,
    capabilityId: published.json.capability_id,
    publishStatus: published.status,
  };
}

/** Restore an object to an exact byte/metadata image and clear every fault. */
function restoreObject(world, objectKey, bytes, metadata) {
  world.bucket.healObject(objectKey);
  const existing = world.bucket.objects.get(objectKey);
  if (existing) {
    existing.bytes = bytes.slice();
    existing.customMetadata = Object.assign({}, metadata);
  } else {
    world.bucket.objects.set(objectKey, {
      bytes: bytes.slice(),
      customMetadata: Object.assign({}, metadata),
      get body() { return new TextDecoder("utf-8").decode(this.bytes); },
    });
  }
}

/**
 * THE shape of every fail-closed recovery case.
 *
 * Publish healthy, break the object in exactly one way, attempt recovery,
 * measure every authorization column across the call, then restore the EXACT
 * valid object and prove recovery then succeeds normally — a refusal that also
 * poisoned the operation would be a different bug wearing the same status.
 */
async function failClosedRecovery(seed, breakObject) {
  const { world, op, objectKey, capability, capabilityId } = await publishedWorld(seed);
  const stored = world.bucket.objects.get(objectKey);
  const pristineBytes = stored.bytes.slice();
  const pristineMetadata = Object.assign({}, stored.customMetadata);

  await breakObject({ world, stored, objectKey, op });

  const before = authorizationState(world, op, capabilityId);
  const mark = world.db.statementLog.length;
  const refused = await recover(world, op);
  const recoveryStatements = recoveryStatementsSince(world, mark);
  const after = authorizationState(world, op, capabilityId);

  /* The predecessor bearer must still work: a refused recovery may not have
   * degraded the authorization the host already holds. */
  const session = await exchange(world, capability);
  const cookie = (session.headers["set-cookie"] || "").split(";")[0];

  restoreObject(world, objectKey, pristineBytes, pristineMetadata);
  const resumed = await recover(world, op);
  const resumedState = authorizationState(world, op, capabilityId);
  const replacementSession = await exchange(world, resumed.json && resumed.json.capability);
  const replacementCookie = (replacementSession.headers["set-cookie"] || "").split(";")[0];
  const replacementRead = replacementCookie
    ? await readSnapshot(world, replacementCookie) : { status: null };

  return {
    /* --- the refusal ---------------------------------------------------- */
    refused_status: refused.status,
    refused_error: refused.json && refused.json.error,
    refused_result: refused.json && refused.json.status,
    refused_next_action: refused.json && refused.json.next_action,
    refused_bearer: !!(refused.json && refused.json.capability),
    /* The response must not name the failing invariant of a private object. */
    refused_body_names_invariant:
      /BINDING|DIGEST_META|LEDGER_DIGEST_MISMATCH|SUBJECT_BINDING|THREW|ABSENT_UNDER/
        .test(refused.text),
    /* The gate is a precondition: the transaction was never attempted. */
    recovery_statements_issued: recoveryStatements,

    /* --- zero authorization mutation, measured -------------------------- */
    state_before: before.state,
    state_after: after.state,
    capability_id_unchanged: before.capability_id === after.capability_id,
    generation_before: before.bearer_generation,
    generation_after: after.bearer_generation,
    grant_rows_before: before.grant_rows,
    grant_rows_after: after.grant_rows,
    live_grants_before: before.live_grants,
    live_grants_after: after.live_grants,
    predecessor_revoked_before: before.predecessor_revoked_at,
    predecessor_revoked_after: after.predecessor_revoked_at,
    predecessor_rotated_before: before.predecessor_rotated_to_present,
    predecessor_rotated_after: after.predecessor_rotated_to_present,
    predecessor_is_current_after: after.predecessor_is_current,
    ledger_digest_before: before.ledger_digest,
    ledger_digest_after: after.ledger_digest,

    /* --- zero object mutation ------------------------------------------- */
    puts_before: before.puts,
    puts_after: after.puts,
    put_count_delta: after.puts - before.puts,
    bytes_unchanged: before.bytes_hex === after.bytes_hex,

    /* --- the predecessor authorization is intact ------------------------ */
    predecessor_session_status: session.status,

    /* --- and the operation converges once the object is restored -------- */
    resumed_status: resumed.status,
    resumed_result: resumed.json && resumed.json.status,
    resumed_bearer: !!(resumed.json && resumed.json.capability),
    resumed_generation: resumedState.bearer_generation,
    resumed_grant_rows: resumedState.grant_rows,
    resumed_live_grants: resumedState.live_grants,
    resumed_predecessor_revoked: resumedState.predecessor_revoked_at !== null,
    resumed_predecessor_rotated: resumedState.predecessor_rotated_to_present,
    resumed_bytes_are_host_bytes: resumedState.bytes_hex === hex(CANONICAL_A),
    replacement_session_status: replacementSession.status,
    replacement_snapshot_status: replacementRead.status,
  };
}

/* -------------------------------------------------------------- scenarios -- */

const scenarios = {};

/* --- A. healthy object ---------------------------------------------------- */

scenarios.a_healthy_object_recovers = async () => {
  const { world, op, objectKey, capability, capabilityId, publishStatus } =
    await publishedWorld("heal");
  const before = authorizationState(world, op, capabilityId);

  const recovered = await recover(world, op);
  const after = authorizationState(world, op, capabilityId);

  /* The replacement genuinely works and the predecessor genuinely does not. */
  const replacementSession = await exchange(world, recovered.json.capability);
  const replacementCookie = (replacementSession.headers["set-cookie"] || "").split(";")[0];
  const replacementRead = await readSnapshot(world, replacementCookie);
  const predecessorSession = await exchange(world, capability);

  const stored = world.bucket.objects.get(objectKey);
  return {
    publish_status: publishStatus,
    recover_status: recovered.status,
    recover_result: recovered.json.status,
    recover_bearer: !!recovered.json.capability,
    bearers_returned: recovered.json.capability ? 1 : 0,
    generation_before: before.bearer_generation,
    generation_after: after.bearer_generation,
    grant_rows_before: before.grant_rows,
    grant_rows_after: after.grant_rows,
    live_grants_after: after.live_grants,
    replacement_grants: after.grant_rows - before.grant_rows,
    predecessor_revoked: after.predecessor_revoked_at !== null,
    predecessor_rotated: after.predecessor_rotated_to_present,
    predecessor_is_current_after: after.predecessor_is_current,
    capability_changed: before.capability_id !== after.capability_id,
    state_before: before.state,
    state_after: after.state,
    /* Recovery reads the object; it never writes or repairs it. */
    put_count_delta: after.puts - before.puts,
    bytes_unchanged: before.bytes_hex === after.bytes_hex,
    bytes_are_host_bytes: hex(stored.bytes) === hex(CANONICAL_A),
    metadata_digest: stored.customMetadata.payload_digest,
    ledger_digest_after: after.ledger_digest,
    replacement_session_status: replacementSession.status,
    replacement_snapshot_status: replacementRead.status,
    replacement_snapshot_digest: replacementRead.status === 200
      ? await digestLib.sha256Hex(new TextEncoder().encode(replacementRead.text)) : null,
    predecessor_session_status: predecessorSession.status,
  };
};

/* --- B. object absent ----------------------------------------------------- */

scenarios.b_absent_object_blocks_recovery = () =>
  failClosedRecovery("absnt", async ({ world, objectKey }) => {
    world.bucket.objects.delete(objectKey);
  });

/* --- C. body corruption --------------------------------------------------- */

scenarios.c_body_corruption_blocks_recovery = () =>
  failClosedRecovery("bodyc", async ({ stored }) => {
    const mutated = stored.bytes.slice();
    mutated[mutated.length - 2] = mutated[mutated.length - 2] === 0x31 ? 0x32 : 0x31;
    stored.bytes = mutated;
  });

/* --- D. metadata digest corruption ---------------------------------------- */

scenarios.d_metadata_digest_corruption_blocks_recovery = () =>
  failClosedRecovery("metac", async ({ stored }) => {
    stored.customMetadata = Object.assign({}, stored.customMetadata,
                                          { payload_digest: DIGEST_B });
  });

/* --- E. metadata digest missing ------------------------------------------- */

scenarios.e_missing_metadata_digest_blocks_recovery = () =>
  failClosedRecovery("metad", async ({ stored }) => {
    const without = Object.assign({}, stored.customMetadata);
    delete without.payload_digest;
    stored.customMetadata = without;
  });

/* --- F. coordinated rewrite; only the ledger disagrees -------------------- */

scenarios.f_coordinated_rewrite_blocks_recovery = () =>
  failClosedRecovery("coord", async ({ stored }) => {
    /* A different, perfectly valid canonical snapshot, self-consistently
     * stamped. A self-consistency check alone would pass it, which is exactly
     * why the ledger digest is one of the authorities. */
    stored.bytes = CANONICAL_B.slice();
    stored.customMetadata = Object.assign({}, stored.customMetadata,
                                          { payload_digest: DIGEST_B });
  });

/* --- G. subject-binding corruption ---------------------------------------- */

scenarios.g_subject_binding_corruption_blocks_recovery = () =>
  failClosedRecovery("bindc", async ({ stored }) => {
    const binding = stored.customMetadata.subject_binding;
    stored.customMetadata = Object.assign({}, stored.customMetadata, {
      subject_binding: (binding.charAt(0) === "a" ? "b" : "a") + binding.slice(1),
    });
  });

/* --- H. subject-binding metadata missing ---------------------------------- */

scenarios.h_missing_subject_binding_blocks_recovery = () =>
  failClosedRecovery("bindm", async ({ stored }) => {
    const without = Object.assign({}, stored.customMetadata);
    delete without.subject_binding;
    stored.customMetadata = without;
  });

/* --- I. R2 get() fails ---------------------------------------------------- */

scenarios.i_get_failure_blocks_recovery = () =>
  failClosedRecovery("getth", async ({ world, objectKey }) => {
    world.bucket.failGetFor(objectKey, "R2_GET_FAILED");
  });

/* --- J. body read fails --------------------------------------------------- */

scenarios.j_body_read_failure_blocks_recovery = () =>
  failClosedRecovery("bodyt", async ({ world, objectKey }) => {
    world.bucket.failBodyReadFor(objectKey, "R2_BODY_READ_FAILED");
  });

/* --- K. metadata access fails --------------------------------------------- */

scenarios.k_metadata_failure_blocks_recovery = () =>
  failClosedRecovery("metat", async ({ world, objectKey }) => {
    world.bucket.failMetadataFor(objectKey, "R2_METADATA_READ_FAILED");
  });

/* --- L..O. malformed R2 RESULTS ------------------------------------------- */

/*
 * A malformed result is neither a storage failure nor an absence: `get()`
 * RESOLVES, and what it resolves is a value the Worker cannot account for. The
 * re-review confirmed a key-less result being classified as a PROVEN object,
 * which carried recovery all the way through: generation 1 -> 2, predecessor
 * superseded, one replacement grant, one new raw bearer. All four shapes are
 * driven through the same measured zero-mutation case as every other refusal.
 */

/* `get()` resolves `undefined` while the object is present and healthy. */
scenarios.l_undefined_result_blocks_recovery = () =>
  failClosedRecovery("undef", async ({ world, objectKey }) => {
    world.bucket.resolveUndefinedFor(objectKey);
  });

/* Correct body, correct metadata, no echoed key: identity unprovable. */
scenarios.m_result_without_key_blocks_recovery = () =>
  failClosedRecovery("nokey", async ({ world, objectKey }) => {
    world.bucket.omitEchoedKeyFor(objectKey);
  });

/* A key that is not a string cannot be compared to the owned key at all. */
scenarios.n_non_string_key_blocks_recovery = () =>
  failClosedRecovery("badky", async ({ world, objectKey }) => {
    world.bucket.echoKeyAs(objectKey, 12345);
  });

/* A well-formed key naming a DIFFERENT object: the pre-existing integrity
 * refusal, which this change must not weaken. */
scenarios.o_wrong_string_key_blocks_recovery = () =>
  failClosedRecovery("wrgky", async ({ world, objectKey }) => {
    world.bucket.echoKeyAs(objectKey, "ff/" + "Z".repeat(32) + ".json");
  });

/* --- P..Q. an ARRAY result ------------------------------------------------ */

/*
 * `typeof [] === "object"`, so an array passes the generic object test and can
 * carry a `key`, an `arrayBuffer()` and a `customMetadata` — every property
 * the inspector reads. The re-review confirmed a fully decorated array proving
 * itself PRESENT_VALID and carrying recovery through: generation 1 -> 2,
 * predecessor replaced, one replacement bearer.
 */

scenarios.p_bare_array_blocks_recovery = () =>
  failClosedRecovery("bararr", async ({ world, objectKey }) => {
    world.bucket.resolveBareArrayFor(objectKey);
  });

scenarios.q_decorated_array_blocks_recovery = () =>
  failClosedRecovery("decarr", async ({ world, objectKey }) => {
    world.bucket.resolveDecoratedArrayFor(objectKey);
  });

/* --- the gate is the SAME contract, not a second implementation ----------- */

scenarios.recovery_uses_the_shared_inspection_contract = async () => {
  /* Measured, not read off the source: the services bundle's `inspectObject`
   * is replaced by a counting wrapper around the REAL `inspectSnapshotObject`,
   * and `recoverLostBearer` is called directly. If recovery had its own
   * integrity logic the counter would stay at zero. */
  const { world, op, objectKey, capabilityId } = await publishedWorld("share");
  const row = world.db.publications.get(op);

  const services = {
    db: world.db,
    bucket: world.bucket,
    pepper: PEPPER,
    mintObjectKey: publisherLib.mintObjectKey,
  };
  const seen = [];
  services.inspectObject = async (params) => {
    const verdict = await publisherLib.inspectSnapshotObject(services, params);
    seen.push({
      key_is_operation_owned: params.snapshot_object_key === row.snapshot_object_key,
      subject_is_operation_subject: params.subject_ref === row.subject_ref,
      digest_is_ledger_digest: params.payload_digest === row.payload_digest,
      state: verdict.state,
    });
    return verdict;
  };

  const healthy = await publicationLib.recoverLostBearer(services, {
    operation_id: op, mint_id: "a".repeat(32), ttl_seconds: 3600, now: T0,
  });

  /* And the same bundle, with the object broken, must refuse. */
  world.bucket.failGetFor(objectKey, "R2_GET_FAILED");
  const stateBefore = authorizationState(world, op, capabilityId);
  const broken = await publicationLib.recoverLostBearer(services, {
    operation_id: op, mint_id: "b".repeat(32), ttl_seconds: 3600, now: T0,
  });
  const stateAfter = authorizationState(world, op, capabilityId);

  return {
    inspections: seen.length,
    inspection_calls: seen,
    healthy_status: healthy.status,
    healthy_bearer: !!healthy.capability,
    broken_status: broken.status,
    broken_bearer: !!broken.capability,
    broken_next_action: broken.next_action,
    generation_unchanged: stateBefore.bearer_generation === stateAfter.bearer_generation,
    grant_rows_unchanged: stateBefore.grant_rows === stateAfter.grant_rows,
  };
};

/* --- THE RECURRENCE DETECTOR ---------------------------------------------- */

/**
 * The one test that fails if recovery ever stops consulting the authoritative
 * object inspection — and, crucially, a MUTATION PROOF that it is load-bearing.
 *
 * `guarded` is the production module: valid publication, R2 get injected to
 * fail, recovery attempted, generation must not move and no replacement grant
 * may exist.
 *
 * `bypassed` runs the SAME scenario against a copy of `publication.js` with the
 * PRESENT_VALID gate deleted and nothing else changed. It must rotate — which
 * is what proves the assertions above are testing the guard rather than some
 * incidental refusal. The mutant lives in a temporary directory outside the
 * repository and its relative imports are rewritten to the real modules, so
 * nothing under `delivery/` is ever modified.
 */
scenarios.recurrence_recovery_consults_object_inspection = async () => {
  /* --- guarded: the production module ----------------------------------- */
  const { world, op, objectKey, capabilityId } = await publishedWorld("recur");
  const before = authorizationState(world, op, capabilityId);
  world.bucket.failGetFor(objectKey, "R2_GET_FAILED");
  const mark = world.db.statementLog.length;
  const refused = await recover(world, op);
  const after = authorizationState(world, op, capabilityId);

  const guarded = {
    object_logically_exists: !!world.bucket.objects.get(objectKey),
    status: refused.status,
    error: refused.json && refused.json.error,
    bearer: !!(refused.json && refused.json.capability),
    generation_before: before.bearer_generation,
    generation_after: after.bearer_generation,
    replacement_grants: after.grant_rows - before.grant_rows,
    live_grants_after: after.live_grants,
    predecessor_still_current: after.predecessor_is_current,
    recovery_statements_issued: recoveryStatementsSince(world, mark),
    /* Once storage recovers the operation converges with no repair. */
    healed: await (async () => {
      world.bucket.healObject(objectKey);
      const again = await recover(world, op);
      const healedState = authorizationState(world, op, capabilityId);
      return {
        status: again.status,
        result: again.json && again.json.status,
        bearer: !!(again.json && again.json.capability),
        generation: healedState.bearer_generation,
        live_grants: healedState.live_grants,
        puts_total: world.bucket.putLog.length,
      };
    })(),
  };

  /* --- bypassed: the same scenario against a mutant without the gate ----- */
  const libDir = path.join(DELIVERY, "worker", "lib");
  const source = await readFile(path.join(libDir, "publication.js"), "utf8");
  const start = source.indexOf("  const inspection = await services.inspectObject({\n    snapshot_object_key: operation.snapshot_object_key,");
  const end = source.indexOf("  const predecessorId = operation.capability_id;");
  if (start === -1 || end === -1 || end <= start) {
    return { guarded, bypassed: { mutation_error: "recovery gate block not located" } };
  }
  /* Relative specifiers must keep resolving to the REAL modules. */
  const mutantSource = (source.slice(0, start) + source.slice(end))
    .replace(/from "\.\//g, `from "${path.join(libDir, "/")}`);

  const tempDir = await mkdtemp(path.join(os.tmpdir(), "eco-recovery-mutant-"));
  let bypassed;
  try {
    const mutantPath = path.join(tempDir, "publication_without_gate.mjs");
    await writeFile(mutantPath, mutantSource, "utf8");
    const mutant = await import(mutantPath);

    const w = await publishedWorld("mutan");
    const services = {
      db: w.world.db, bucket: w.world.bucket, pepper: PEPPER,
      mintObjectKey: publisherLib.mintObjectKey,
    };
    services.inspectObject = (params) => publisherLib.inspectSnapshotObject(services, params);
    services.putObject = (params) => publisherLib.putSnapshotObject(services, params);

    const mutantBefore = authorizationState(w.world, w.op, w.capabilityId);
    w.world.bucket.failGetFor(w.objectKey, "R2_GET_FAILED");
    const result = await mutant.recoverLostBearer(services, {
      operation_id: w.op, mint_id: "c".repeat(32), ttl_seconds: 3600, now: T0,
    });
    const mutantAfter = authorizationState(w.world, w.op, w.capabilityId);
    bypassed = {
      /* A marker unique to the RECOVERY gate — `publishSnapshot` has its own
       * PRESENT_VALID comparison and must remain untouched by the mutation. */
      gate_removed: !mutantSource.includes("integrityFailure(operation, RECOVERY_OBJECT_ABSENT_REASON)"),
      publish_gate_intact: mutantSource.includes("inspection.state !== OBJECT_STATE.PRESENT_VALID"),
      status: result.status,
      bearer: !!result.capability,
      generation_before: mutantBefore.bearer_generation,
      generation_after: mutantAfter.bearer_generation,
      replacement_grants: mutantAfter.grant_rows - mutantBefore.grant_rows,
      predecessor_revoked: mutantAfter.predecessor_revoked_at !== null,
    };
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }

  return { guarded, bypassed };
};

/*
 * RECURRENCE DETECTOR — recovery requires a PROVEN object key.
 *
 * The confirmed defect: the returned object had a valid body and valid
 * metadata but no `key`, the comparison was conditional on a string key being
 * present, so it was skipped, PRESENT_VALID was returned and recovery ran to
 * completion — generation 1 -> 2, predecessor revoked and rotated, one
 * replacement grant, one new raw bearer handed out.
 *
 * `guarded` is the production module through the real route. `bypassed` is the
 * same scenario with object inspection supplied by a copy of `publisher.js`
 * whose key check is reverted to `typeof object.key === "string" &&
 * object.key !== objectKey`. It must ROTATE — which is what proves the guarded
 * assertions are testing this rule rather than some incidental refusal.
 */
scenarios.recurrence_recovery_requires_a_proven_key = async () => {
  /* --- guarded ------------------------------------------------------------ */
  const { world, op, objectKey, capability, capabilityId } = await publishedWorld("kyrec");
  const stored = world.bucket.objects.get(objectKey);
  const pristineBytes = stored.bytes.slice();
  const pristineMetadata = Object.assign({}, stored.customMetadata);

  const before = authorizationState(world, op, capabilityId);
  /* The object is present, its body and metadata are untouched and correct.
   * Only the echoed key is gone. */
  world.bucket.omitEchoedKeyFor(objectKey);
  const mark = world.db.statementLog.length;
  const refused = await recover(world, op);
  const after = authorizationState(world, op, capabilityId);

  /* The bearer the host already holds must still work. */
  const predecessorSession = await exchange(world, capability);
  const predecessorCookie = (predecessorSession.headers["set-cookie"] || "").split(";")[0];
  const predecessorRead = predecessorCookie
    ? await readSnapshot(world, predecessorCookie) : { status: null };

  const guarded = {
    object_logically_exists: !!world.bucket.objects.get(objectKey),
    body_and_metadata_intact:
      hex(world.bucket.objects.get(objectKey).bytes) === hex(pristineBytes) &&
      world.bucket.objects.get(objectKey).customMetadata.payload_digest ===
        pristineMetadata.payload_digest,
    status: refused.status,
    error: refused.json && refused.json.error,
    bearer: !!(refused.json && refused.json.capability),
    /* THE assertions. */
    generation_before: before.bearer_generation,
    generation_after: after.bearer_generation,
    replacement_grants: after.grant_rows - before.grant_rows,
    capability_id_unchanged: before.capability_id === after.capability_id,
    live_grants_before: before.live_grants,
    live_grants_after: after.live_grants,
    predecessor_revoked_before: before.predecessor_revoked_at,
    predecessor_revoked_after: after.predecessor_revoked_at,
    predecessor_rotated_after: after.predecessor_rotated_to_present,
    predecessor_still_current: after.predecessor_is_current,
    /* The gate is a PREcondition: the transaction was never attempted. */
    recovery_statements_issued: recoveryStatementsSince(world, mark),
    put_count_delta: after.puts - before.puts,
    predecessor_session_status: predecessorSession.status,
    predecessor_snapshot_status: predecessorRead.status,
  };

  /* Restore the exact object result and prove the operation is unpoisoned. */
  restoreObject(world, objectKey, pristineBytes, pristineMetadata);
  const resumed = await recover(world, op);
  const resumedState = authorizationState(world, op, capabilityId);
  const replacementSession = await exchange(world, resumed.json && resumed.json.capability);
  const replacementCookie = (replacementSession.headers["set-cookie"] || "").split(";")[0];
  const replacementRead = replacementCookie
    ? await readSnapshot(world, replacementCookie) : { status: null };
  const predecessorAfter = await exchange(world, capability);
  guarded.restored = {
    status: resumed.status,
    result: resumed.json && resumed.json.status,
    bearer: !!(resumed.json && resumed.json.capability),
    generation: resumedState.bearer_generation,
    replacement_grants: resumedState.grant_rows - before.grant_rows,
    live_grants: resumedState.live_grants,
    predecessor_revoked: resumedState.predecessor_revoked_at !== null,
    predecessor_rotated: resumedState.predecessor_rotated_to_present,
    predecessor_session_status: predecessorAfter.status,
    replacement_session_status: replacementSession.status,
    replacement_snapshot_status: replacementRead.status,
    put_count_delta: resumedState.puts - before.puts,
  };

  /* --- bypassed: the same scenario without the mandatory key -------------- */
  const mutant = await loadMutantPublisher("publisher_optional_key", (source) => {
    const start = source.indexOf("  let echoedKey = null;");
    const end = source.indexOf("  /* A conditional response carries no body;");
    if (start === -1 || end === -1 || end <= start) return null;
    return source.slice(0, start) +
      `  if (typeof object.key === "string" && object.key !== objectKey) {
    return invalid(OBJECT_INSPECTION_REASON.KEY_MISMATCH);
  }

` + source.slice(end);
  });

  let bypassed;
  if (mutant.error) {
    bypassed = { mutation_error: mutant.error };
  } else {
    try {
      const w = await publishedWorld("kymut");
      const services = {
        db: w.world.db, bucket: w.world.bucket, pepper: PEPPER,
        mintObjectKey: mutant.module.mintObjectKey,
      };
      services.inspectObject = (params) => mutant.module.inspectSnapshotObject(services, params);
      services.putObject = (params) => mutant.module.putSnapshotObject(services, params);

      const mutantBefore = authorizationState(w.world, w.op, w.capabilityId);
      w.world.bucket.omitEchoedKeyFor(w.objectKey);
      const verdict = await services.inspectObject({
        snapshot_object_key: w.objectKey,
        subject_ref: w.world.db.publications.get(w.op).subject_ref,
        payload_digest: w.world.db.publications.get(w.op).payload_digest,
      });
      const result = await publicationLib.recoverLostBearer(services, {
        operation_id: w.op, mint_id: "d".repeat(32), ttl_seconds: 3600, now: T0,
      });
      const mutantAfter = authorizationState(w.world, w.op, w.capabilityId);
      bypassed = {
        /* The mutation is narrow: only the mandatory-key requirement is gone. */
        mutant_verdict_state: verdict.state,
        mutant_verdict_reusable: verdict.reusable,
        status: result.status,
        bearer: !!result.capability,
        generation_before: mutantBefore.bearer_generation,
        generation_after: mutantAfter.bearer_generation,
        replacement_grants: mutantAfter.grant_rows - mutantBefore.grant_rows,
        predecessor_revoked: mutantAfter.predecessor_revoked_at !== null,
        predecessor_rotated: mutantAfter.predecessor_rotated_to_present,
      };
    } finally {
      await mutant.cleanup();
    }
  }

  return { guarded, bypassed };
};

/*
 * RECURRENCE DETECTOR — a decorated ARRAY cannot move authorization.
 *
 * The object is untouched and healthy; only the RESULT is an Array carrying
 * the operation-owned key, a reader over the real octets and the real
 * metadata. Every content check would pass. The confirmed defect classified it
 * PRESENT_VALID and ran recovery to completion.
 *
 * `bypassed` is the same scenario with inspection supplied by a copy of
 * `publisher.js` with ONLY the array rejection removed. It must ROTATE.
 */
scenarios.recurrence_recovery_rejects_decorated_array = async () => {
  const { world, op, objectKey, capability, capabilityId } = await publishedWorld("arrec");
  const stored = world.bucket.objects.get(objectKey);
  const pristineBytes = stored.bytes.slice();
  const pristineMetadata = Object.assign({}, stored.customMetadata);

  /* What the injected value really is, recorded rather than assumed. */
  world.bucket.resolveDecoratedArrayFor(objectKey);
  const value = await world.bucket.get(objectKey);
  const carried = {
    is_array: Array.isArray(value),
    typeof_is_object: typeof value === "object",
    key_matches: value.key === objectKey,
    has_body_reader: typeof value.arrayBuffer === "function",
    body_is_the_stored_octets:
      hex(new Uint8Array(await value.arrayBuffer())) === hex(pristineBytes),
    metadata_digest_is_correct:
      value.customMetadata.payload_digest === pristineMetadata.payload_digest,
    metadata_binding_present: typeof value.customMetadata.subject_binding === "string",
  };

  const before = authorizationState(world, op, capabilityId);
  const mark = world.db.statementLog.length;
  const refused = await recover(world, op);
  const after = authorizationState(world, op, capabilityId);

  const predecessorSession = await exchange(world, capability);
  const predecessorCookie = (predecessorSession.headers["set-cookie"] || "").split(";")[0];
  const predecessorRead = predecessorCookie
    ? await readSnapshot(world, predecessorCookie) : { status: null };

  const guarded = {
    object_logically_exists: !!world.bucket.objects.get(objectKey),
    status: refused.status,
    error: refused.json && refused.json.error,
    bearer: !!(refused.json && refused.json.capability),
    state_before: before.state,
    state_after: after.state,
    generation_before: before.bearer_generation,
    generation_after: after.bearer_generation,
    capability_id_unchanged: before.capability_id === after.capability_id,
    replacement_grants: after.grant_rows - before.grant_rows,
    live_grants_before: before.live_grants,
    live_grants_after: after.live_grants,
    predecessor_revoked_before: before.predecessor_revoked_at,
    predecessor_revoked_after: after.predecessor_revoked_at,
    predecessor_rotated_after: after.predecessor_rotated_to_present,
    predecessor_still_current: after.predecessor_is_current,
    recovery_statements_issued: recoveryStatementsSince(world, mark),
    put_count_delta: after.puts - before.puts,
    bytes_unchanged: before.bytes_hex === after.bytes_hex,
    predecessor_session_status: predecessorSession.status,
    predecessor_snapshot_status: predecessorRead.status,
  };

  /* Restore the exact valid result and prove recovery then proceeds once. */
  restoreObject(world, objectKey, pristineBytes, pristineMetadata);
  const resumed = await recover(world, op);
  const resumedState = authorizationState(world, op, capabilityId);
  const replacementSession = await exchange(world, resumed.json && resumed.json.capability);
  const replacementCookie = (replacementSession.headers["set-cookie"] || "").split(";")[0];
  const replacementRead = replacementCookie
    ? await readSnapshot(world, replacementCookie) : { status: null };
  const predecessorAfter = await exchange(world, capability);
  guarded.restored = {
    status: resumed.status,
    result: resumed.json && resumed.json.status,
    bearer: !!(resumed.json && resumed.json.capability),
    generation: resumedState.bearer_generation,
    replacement_grants: resumedState.grant_rows - before.grant_rows,
    live_grants: resumedState.live_grants,
    predecessor_revoked: resumedState.predecessor_revoked_at !== null,
    predecessor_rotated: resumedState.predecessor_rotated_to_present,
    predecessor_session_status: predecessorAfter.status,
    replacement_session_status: replacementSession.status,
    replacement_snapshot_status: replacementRead.status,
    put_count_delta: resumedState.puts - before.puts,
    bytes_are_host_bytes: resumedState.bytes_hex === hex(CANONICAL_A),
    ledger_digest_unchanged: before.ledger_digest === resumedState.ledger_digest,
  };

  /* --- the mutation proof ------------------------------------------------ */
  const mutant = await loadMutantPublisher("publisher_without_array_guard", (source) => {
    const site = `  if (Array.isArray(object)) {
    return unreadable(OBJECT_INSPECTION_REASON.RESULT_ARRAY);
  }
`;
    if (!source.includes(site)) return null;
    return source.replace(site, "");
  });

  let bypassed;
  if (mutant.error) {
    bypassed = { mutation_error: mutant.error };
  } else {
    try {
      const w = await publishedWorld("armut");
      const services = {
        db: w.world.db, bucket: w.world.bucket, pepper: PEPPER,
        mintObjectKey: mutant.module.mintObjectKey,
      };
      services.inspectObject = (params) => mutant.module.inspectSnapshotObject(services, params);
      services.putObject = (params) => mutant.module.putSnapshotObject(services, params);

      const mutantBefore = authorizationState(w.world, w.op, w.capabilityId);
      w.world.bucket.resolveDecoratedArrayFor(w.objectKey);
      const row = w.world.db.publications.get(w.op);
      const verdict = await services.inspectObject({
        snapshot_object_key: w.objectKey,
        subject_ref: row.subject_ref,
        payload_digest: row.payload_digest,
      });
      const result = await publicationLib.recoverLostBearer(services, {
        operation_id: w.op, mint_id: "e".repeat(32), ttl_seconds: 3600, now: T0,
      });
      const mutantAfter = authorizationState(w.world, w.op, w.capabilityId);
      bypassed = {
        mutant_verdict_state: verdict.state,
        mutant_verdict_reusable: verdict.reusable,
        status: result.status,
        bearer: !!result.capability,
        generation_before: mutantBefore.bearer_generation,
        generation_after: mutantAfter.bearer_generation,
        replacement_grants: mutantAfter.grant_rows - mutantBefore.grant_rows,
        predecessor_revoked: mutantAfter.predecessor_revoked_at !== null,
        predecessor_rotated: mutantAfter.predecessor_rotated_to_present,
      };
    } finally {
      await mutant.cleanup();
    }
  }

  return { carried, guarded, bypassed };
};

/* --- concurrency regression, with the gate in place ----------------------- */

scenarios.healthy_recovery_contention = async () => {
  const out = {};
  for (const callers of [2, 8, 32]) {
    const { world, op, objectKey, capabilityId } = await publishedWorld("cn" + callers);
    const before = authorizationState(world, op, capabilityId);
    const responses = await Promise.all(
      Array.from({ length: callers }, () => recover(world, op)));
    const after = authorizationState(world, op, capabilityId);
    const grants = [...world.db.capabilities.values()];
    out["callers_" + callers] = {
      recovered: responses.filter((r) => r.json && r.json.status === "RECOVERED").length,
      bearers_returned: responses.filter((r) => r.json && r.json.capability).length,
      safe_conflicts: responses.filter((r) =>
        r.status === 409 && r.json && r.json.status === "NOT_RECOVERABLE").length,
      conflict_reasons: [...new Set(responses
        .filter((r) => r.json && r.json.reason).map((r) => r.json.reason))].sort(),
      integrity_refusals: responses.filter((r) => r.json &&
        (r.json.error === "OBJECT_INTEGRITY_FAILURE" || r.json.error === "OBJECT_UNREADABLE")).length,
      generation_delta: after.bearer_generation - before.bearer_generation,
      grant_rows: grants.length,
      live_grants: grants.filter((g) => !g.revoked_at && !g.rotated_to).length,
      /* The gate is read-only: N concurrent inspections, zero writes. */
      put_count_delta: after.puts - before.puts,
      bytes_are_host_bytes: hex(world.bucket.objects.get(objectKey).bytes) === hex(CANONICAL_A),
      state_after: after.state,
    };
  }

  /* Forced: N callers held at the recovery transaction and released together,
   * all with a healthy object, so the gate cannot be what serialises them. */
  const forced = await publishedWorld("force");
  const barrier = new bindings.Barrier();
  barrier.hold(bindings.SITE.RECOVERY_TRANSACTION, "before").attach(forced.world.db);
  const inflight = Array.from({ length: 16 }, () => recover(forced.world, forced.op));
  await barrier.waitFor(bindings.SITE.RECOVERY_TRANSACTION, 16);
  barrier.releaseAll();
  const forcedResponses = await Promise.all(inflight);
  const forcedAfter = authorizationState(forced.world, forced.op, forced.capabilityId);
  out.forced_16 = {
    recovered: forcedResponses.filter((r) => r.json && r.json.status === "RECOVERED").length,
    bearers_returned: forcedResponses.filter((r) => r.json && r.json.capability).length,
    generation_after: forcedAfter.bearer_generation,
    grant_rows: forcedAfter.grant_rows,
    live_grants: forcedAfter.live_grants,
    put_count_delta: forcedAfter.puts - 1,
  };
  return out;
};

/* --- recovery against the rest of the state machine ----------------------- */

scenarios.recovery_against_terminal_and_rollback = async () => {
  /* DELIVERED is terminal even with a perfectly healthy object. */
  const delivered = await publishedWorld("deliv");
  await delivery(delivered.world, delivered.op, "INTENT", delivered.capabilityId);
  await delivery(delivered.world, delivered.op, "DELIVERED", delivered.capabilityId);
  const deliveredBefore = authorizationState(delivered.world, delivered.op, delivered.capabilityId);
  const deliveredMark = delivered.world.db.statementLog.length;
  const deliveredRecovery = await recover(delivered.world, delivered.op);
  const deliveredAfter = authorizationState(delivered.world, delivered.op, delivered.capabilityId);
  const deliveredSession = await exchange(delivered.world, delivered.capability);
  const deliveredCookie = (deliveredSession.headers["set-cookie"] || "").split(";")[0];
  const deliveredRead = deliveredCookie
    ? await readSnapshot(delivered.world, deliveredCookie) : { status: null };

  /* The recovery transaction rolling back mid-batch, with a healthy object:
   * the gate must not have converted a rollback into a partial mutation. */
  const rollback = await publishedWorld("rollb");
  const rollbackBefore = authorizationState(rollback.world, rollback.op, rollback.capabilityId);
  rollback.world.db.failInBatchAfter = 1;
  const rollbackResponse = await recover(rollback.world, rollback.op);
  delete rollback.world.db.failInBatchAfter;
  const rollbackAfter = authorizationState(rollback.world, rollback.op, rollback.capabilityId);
  const rollbackRetry = await recover(rollback.world, rollback.op);
  const rollbackRetryState = authorizationState(rollback.world, rollback.op, rollback.capabilityId);

  /* A publication retry arriving while the operation already holds a grant
   * must still be a replay, not a second bearer, with the gate in place. */
  const retryWorld = await publishedWorld("retry");
  const retryBefore = authorizationState(retryWorld.world, retryWorld.op, retryWorld.capabilityId);
  const retry = await publish(retryWorld.world, retryWorld.op);
  const recoverAfterRetry = await recover(retryWorld.world, retryWorld.op);
  const retryAfter = authorizationState(retryWorld.world, retryWorld.op, retryWorld.capabilityId);

  /* Recovery after the predecessor was independently revoked. */
  const revoked = await publishedWorld("revok");
  await revoked.world.db.prepare(
    "UPDATE eco_capability SET revoked_at = ?2 WHERE capability_id = ?1 AND revoked_at IS NULL"
  ).bind(revoked.capabilityId, T0).run();
  const revokedBefore = authorizationState(revoked.world, revoked.op, revoked.capabilityId);
  const revokedRecovery = await recover(revoked.world, revoked.op);
  const revokedAfter = authorizationState(revoked.world, revoked.op, revoked.capabilityId);

  return {
    delivered: {
      state_before: deliveredBefore.state,
      status: deliveredRecovery.status,
      result: deliveredRecovery.json && deliveredRecovery.json.status,
      reason: deliveredRecovery.json && deliveredRecovery.json.reason,
      bearer: !!(deliveredRecovery.json && deliveredRecovery.json.capability),
      generation_before: deliveredBefore.bearer_generation,
      generation_after: deliveredAfter.bearer_generation,
      grant_rows_unchanged: deliveredBefore.grant_rows === deliveredAfter.grant_rows,
      /* Terminal is decided before the object is even consulted. */
      recovery_statements_issued: recoveryStatementsSince(delivered.world, deliveredMark),
      delivered_bearer_session_status: deliveredSession.status,
      delivered_bearer_snapshot_status: deliveredRead.status,
    },
    rollback: {
      status: rollbackResponse.status,
      generation_before: rollbackBefore.bearer_generation,
      generation_after: rollbackAfter.bearer_generation,
      grant_rows_after: rollbackAfter.grant_rows,
      live_grants_after: rollbackAfter.live_grants,
      predecessor_revoked_after: rollbackAfter.predecessor_revoked_at !== null,
      retry_status: rollbackRetry.status,
      retry_result: rollbackRetry.json && rollbackRetry.json.status,
      retry_generation: rollbackRetryState.bearer_generation,
      retry_live_grants: rollbackRetryState.live_grants,
    },
    publish_retry: {
      retry_status: retry.status,
      retry_result: retry.json && retry.json.status,
      retry_bearer: !!(retry.json && retry.json.capability),
      recover_status: recoverAfterRetry.status,
      recover_result: recoverAfterRetry.json && recoverAfterRetry.json.status,
      generation_before: retryBefore.bearer_generation,
      generation_after: retryAfter.bearer_generation,
      live_grants_after: retryAfter.live_grants,
      grant_rows_after: retryAfter.grant_rows,
    },
    predecessor_revoked: {
      status: revokedRecovery.status,
      result: revokedRecovery.json && revokedRecovery.json.status,
      reason: revokedRecovery.json && revokedRecovery.json.reason,
      bearer: !!(revokedRecovery.json && revokedRecovery.json.capability),
      generation_before: revokedBefore.bearer_generation,
      generation_after: revokedAfter.bearer_generation,
      grant_rows_unchanged: revokedBefore.grant_rows === revokedAfter.grant_rows,
    },
  };
};

/* --- protocol shape on the bodyless publisher routes ---------------------- */

/*
 * The singleton control-header contract, measured on the routes that MOVE
 * authorization rather than only on `/api/publish`. Every rejected shape must
 * produce zero operation, object, grant and generation movement — the request
 * is refused before any store is touched.
 *
 * `Content-Type` is optional here (these routes carry no body) but, when
 * declared, must be ONE unambiguous value and must be the documented publisher
 * media type. `application/jsonp` must not pass a prefix test on any route.
 */
scenarios.recovery_route_protocol_shape = async () => {
  const results = {};

  async function attempt(name, pathname, pairs) {
    const { world, op, capabilityId } = await publishedWorld("p" + Object.keys(results).length);
    const before = authorizationState(world, op, capabilityId);
    const mark = world.db.statementLog.length;
    /* `/api/publish/recover` mints a replacement grant, so it now requires the
     * reporting period that decides the new lifetime. These scenarios are about
     * the SINGLETON and MEDIA-TYPE contract, so a well-formed period is supplied
     * unless the case is deliberately shaping that header itself — otherwise
     * every one of them would collapse into the same period refusal and stop
     * testing what it was written for. Malformed and duplicated periods are
     * covered by test_driver_eco_dashboard_capability_lifecycle.py. */
    const built = pairs(op, capabilityId);
    if (pathname === "/api/publish/recover"
        && !built.some(([name]) => name === "X-Publication-Period")) {
      built.push(["X-Publication-Period", "weekly"]);
    }
    const request = edgeRequest(ORIGIN + pathname, {
      method: "POST",
      headers: new Headers(built),
    });
    const response = await call(world, request);
    const after = authorizationState(world, op, capabilityId);
    results[name] = {
      status: response.status,
      error: response.json && response.json.error,
      bearer: !!(response.json && response.json.capability),
      generation_before: before.bearer_generation,
      generation_after: after.bearer_generation,
      capability_unchanged: before.capability_id === after.capability_id,
      grant_rows_before: before.grant_rows,
      grant_rows_after: after.grant_rows,
      live_grants_after: after.live_grants,
      state_after: after.state,
      put_count_delta: after.puts - before.puts,
      recovery_statements_issued: recoveryStatementsSince(world, mark),
    };
  }

  const auth = () => ["Authorization", "Publisher " + TOKEN];

  await attempt("recover_baseline", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op]]);
  await attempt("recover_operation_duplicate_same", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op], ["X-Publication-Operation", op]]);
  await attempt("recover_operation_duplicate_different", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op],
             ["X-Publication-Operation", operationId("other")]]);
  await attempt("recover_operation_comma_joined", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op + "," + op]]);
  await attempt("recover_operation_missing", "/api/publish/recover", () => [auth()]);
  await attempt("recover_authorization_duplicate_same", "/api/publish/recover",
    (op) => [auth(), auth(), ["X-Publication-Operation", op]]);
  await attempt("recover_authorization_duplicate_different", "/api/publish/recover",
    (op) => [auth(), ["Authorization", "Publisher " + capabilityLib.generateCapability()],
             ["X-Publication-Operation", op]]);
  await attempt("recover_content_type_absent_is_fine", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op]]);
  await attempt("recover_content_type_exact", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op], ["Content-Type", "application/json"]]);
  await attempt("recover_content_type_charset", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op],
             ["Content-Type", "application/json; charset=utf-8"]]);
  await attempt("recover_content_type_duplicated", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op],
             ["Content-Type", "application/json"], ["Content-Type", "application/json"]]);
  await attempt("recover_content_type_jsonp", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op], ["Content-Type", "application/jsonp"]]);
  await attempt("recover_content_type_json_seq", "/api/publish/recover",
    (op) => [auth(), ["X-Publication-Operation", op], ["Content-Type", "application/json-seq"]]);

  await attempt("delivery_content_type_duplicated", "/api/publish/delivery",
    (op, cap) => [auth(), ["X-Publication-Operation", op], ["X-Publication-Phase", "INTENT"],
                  ["X-Publication-Capability", cap],
                  ["Content-Type", "application/json"], ["Content-Type", "application/json"]]);
  await attempt("delivery_content_type_jsonp", "/api/publish/delivery",
    (op, cap) => [auth(), ["X-Publication-Operation", op], ["X-Publication-Phase", "INTENT"],
                  ["X-Publication-Capability", cap], ["Content-Type", "application/jsonp"]]);
  await attempt("delivery_baseline", "/api/publish/delivery",
    (op, cap) => [auth(), ["X-Publication-Operation", op], ["X-Publication-Phase", "INTENT"],
                  ["X-Publication-Capability", cap]]);

  return results;
};

/* --- local end-to-end ----------------------------------------------------- */

scenarios.local_end_to_end = async () => {
  const world = await createWorld();
  const op = operationId("e2e");

  /* 1. canonical bytes -> publisher route -> R2 -> grant -> session -> 200. */
  const published = await publish(world, op);
  const row = world.db.publications.get(op);
  const objectKey = row.snapshot_object_key;
  const firstSession = await exchange(world, published.json.capability);
  const firstCookie = (firstSession.headers["set-cookie"] || "").split(";")[0];
  const firstRead = await readSnapshot(world, firstCookie);
  const firstDigest = await digestLib.sha256Hex(new TextEncoder().encode(firstRead.text));

  /* 2. healthy recovery: replacement works, predecessor denied, bytes stable. */
  const recovered = await recover(world, op);
  const secondSession = await exchange(world, recovered.json.capability);
  const secondCookie = (secondSession.headers["set-cookie"] || "").split(";")[0];
  const secondRead = await readSnapshot(world, secondCookie);
  const secondDigest = await digestLib.sha256Hex(new TextEncoder().encode(secondRead.text));
  const predecessorAfterRecovery = await exchange(world, published.json.capability);

  /* 3. every invalid/unreadable object state refuses, then restore and succeed. */
  const stored = world.bucket.objects.get(objectKey);
  const pristineBytes = stored.bytes.slice();
  const pristineMetadata = Object.assign({}, stored.customMetadata);
  const currentBearer = recovered.json.capability;

  const corruptions = {
    absent: ({ b }) => { b.objects.delete(objectKey); },
    body: ({ o }) => {
      const mutated = o.bytes.slice();
      mutated[mutated.length - 2] = mutated[mutated.length - 2] === 0x31 ? 0x32 : 0x31;
      o.bytes = mutated;
    },
    metadata_digest: ({ o }) => {
      o.customMetadata = Object.assign({}, o.customMetadata, { payload_digest: DIGEST_B });
    },
    metadata_missing: ({ o }) => { o.customMetadata = {}; },
    binding: ({ o }) => {
      const binding = o.customMetadata.subject_binding;
      o.customMetadata = Object.assign({}, o.customMetadata, {
        subject_binding: (binding.charAt(0) === "a" ? "b" : "a") + binding.slice(1),
      });
    },
    get_failure: ({ b }) => { b.failGetFor(objectKey, "R2_GET_FAILED"); },
    body_failure: ({ b }) => { b.failBodyReadFor(objectKey, "R2_BODY_READ_FAILED"); },
    metadata_failure: ({ b }) => { b.failMetadataFor(objectKey, "R2_METADATA_READ_FAILED"); },
  };

  const refusals = {};
  const currentId = world.db.publications.get(op).capability_id;
  for (const [name, corrupt] of Object.entries(corruptions)) {
    restoreObject(world, objectKey, pristineBytes, pristineMetadata);
    const object = world.bucket.objects.get(objectKey);
    corrupt({ b: world.bucket, o: object });
    const before = authorizationState(world, op, currentId);
    const response = await recover(world, op);
    const after = authorizationState(world, op, currentId);
    /* The bearer the host already holds is untouched by the refusal. */
    const session = await exchange(world, currentBearer);
    refusals[name] = {
      status: response.status,
      error: response.json && response.json.error,
      bearer: !!(response.json && response.json.capability),
      generation_unchanged: before.bearer_generation === after.bearer_generation,
      capability_unchanged: before.capability_id === after.capability_id,
      grant_rows_unchanged: before.grant_rows === after.grant_rows,
      live_grants_unchanged: before.live_grants === after.live_grants,
      predecessor_revoked_unchanged:
        before.predecessor_revoked_at === after.predecessor_revoked_at,
      put_count_delta: after.puts - before.puts,
      current_bearer_session_status: session.status,
    };
  }

  /* 4. restore the valid object: recovery succeeds again. */
  restoreObject(world, objectKey, pristineBytes, pristineMetadata);
  const restoredRecovery = await recover(world, op);
  const restoredSession = await exchange(world, restoredRecovery.json.capability);
  const restoredCookie = (restoredSession.headers["set-cookie"] || "").split(";")[0];
  const restoredRead = await readSnapshot(world, restoredCookie);
  const restoredDigest = await digestLib.sha256Hex(new TextEncoder().encode(restoredRead.text));

  /* 5. DELIVERED: recovery denied, delivered bearer still valid. */
  const deliveredId = world.db.publications.get(op).capability_id;
  await delivery(world, op, "INTENT", deliveredId);
  await delivery(world, op, "DELIVERED", deliveredId);
  const afterDelivery = authorizationState(world, op, deliveredId);
  const deniedRecovery = await recover(world, op);
  const afterDenied = authorizationState(world, op, deliveredId);
  const deliveredSession = await exchange(world, restoredRecovery.json.capability);
  const deliveredCookie = (deliveredSession.headers["set-cookie"] || "").split(";")[0];
  const deliveredRead = deliveredCookie ? await readSnapshot(world, deliveredCookie)
                                        : { status: null };

  return {
    publish_status: published.status,
    publish_result: published.json.status,
    first_session_status: firstSession.status,
    first_snapshot_status: firstRead.status,
    first_snapshot_digest: firstDigest,
    ledger_digest: row.payload_digest,
    stored_bytes_are_host_bytes: hex(pristineBytes) === hex(CANONICAL_A),

    recover_status: recovered.status,
    recover_result: recovered.json.status,
    replacement_session_status: secondSession.status,
    replacement_snapshot_status: secondRead.status,
    replacement_snapshot_digest: secondDigest,
    snapshot_digest_stable: firstDigest === secondDigest,
    predecessor_session_after_recovery: predecessorAfterRecovery.status,

    refusals: refusals,

    restored_recover_status: restoredRecovery.status,
    restored_recover_result: restoredRecovery.json.status,
    restored_session_status: restoredSession.status,
    restored_snapshot_status: restoredRead.status,
    restored_snapshot_digest: restoredDigest,

    delivered_state: afterDelivery.state,
    delivered_recovery_status: deniedRecovery.status,
    delivered_recovery_result: deniedRecovery.json && deniedRecovery.json.status,
    delivered_recovery_reason: deniedRecovery.json && deniedRecovery.json.reason,
    delivered_recovery_bearer: !!(deniedRecovery.json && deniedRecovery.json.capability),
    delivered_generation_unchanged:
      afterDelivery.bearer_generation === afterDenied.bearer_generation,
    delivered_bearer_session_status: deliveredSession.status,
    delivered_bearer_snapshot_status: deliveredRead.status,
    total_puts: world.bucket.putLog.length,
    distinct_objects: world.bucket.objects.size,
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
    output[name] = { harness_error: String(error && error.message),
                     stack: String(error && error.stack).slice(0, 900) };
  }
}
process.stdout.write(JSON.stringify(output, null, 1));
