/* Retry-integrity harness for the Driver Eco Dashboard publication boundary.
 *
 * THE DEFECT UNDER TEST
 *
 * A publication retry used a boolean `present` as its whole object-state
 * contract, and an unreadable object was reported as `present: false`. So:
 *
 *   * an R2 `get()` that THREW was answered with another `put()`, overwriting
 *     an object nobody had been able to read;
 *   * an object whose body still hashed correctly but whose digest metadata or
 *     subject binding had been corrupted or removed advanced the ledger to
 *     GRANT_MINTED, returned 201, and minted a live grant whose driver read
 *     then failed 503 forever.
 *
 * WHAT EVERY SCENARIO BELOW RECORDS
 *
 * For the critical window — operation owns the key, R2 write has happened,
 * grant advancement has not — each case reports the response status, the R2
 * put count, the exact stored octets before and after (as hex), the operation
 * phase before and after, the grant row count, the live grant count and the
 * number of bearers returned. A fail-closed claim is only as good as the state
 * it is measured against.
 *
 * Canonical bytes arrive on stdin, base64, produced by the real Python
 * publisher. Nothing here builds a snapshot in JavaScript.
 *
 * Synthetic data only. No wrangler, no credentials, no remote resource, no
 * e-mail. No capability, session id, credential or object key is printed.
 */

import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
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
const publisherLib = await import(path.join(DELIVERY, "worker", "lib", "publisher.js"));
const protocolLib = await import(path.join(DELIVERY, "worker", "lib", "protocol.js"));
const publicationLib = await import(path.join(DELIVERY, "worker", "lib", "publication.js"));

/* ------------------------------------------------------------- mutation --- */

/**
 * Load a MUTANT copy of `publisher.js` with one rule reverted, so an assertion
 * can be shown to be load-bearing rather than incidentally satisfied.
 *
 * The copy lives in a temporary directory OUTSIDE the repository and its
 * relative specifiers are rewritten to the real modules, so nothing under
 * `delivery/` is modified and no other module sees the mutant.
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

/** The publication services bundle, exactly as `worker/index.js` builds it,
 *  but with object inspection supplied by a chosen publisher module. */
function publicationServicesWith(world, publisherModule) {
  const services = {
    db: world.db,
    bucket: world.bucket,
    pepper: PEPPER,
    mintObjectKey: publisherModule.mintObjectKey,
    mintGrantId: () => {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
    },
  };
  services.inspectObject = (params) => publisherModule.inspectSnapshotObject(services, params);
  services.putObject = (params) => publisherModule.putSnapshotObject(services, params);
  return services;
}

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
const PEPPER = "synthetic-retry-integrity-pepper";
const TOKEN = capabilityLib.generateCapability();
const SUBJECT = "subject-retry-integrity";

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
  return ("op-retry-" + seed + "-").padEnd(24, "x").slice(0, 24);
}

/* `headers` is built as an array of pairs so a DUPLICATED header can be
 * expressed: `new Headers([["X", "a"], ["X", "b"]])` is exactly how the Fetch
 * runtime combines repeated field lines, which is the representation the
 * server actually sees. */
function publishRequest(options) {
  const settings = options || {};
  const pairs = [];
  const add = (name, value) => { if (value !== undefined) pairs.push([name, value]); };
  if (settings.contentType === undefined) add("Content-Type", "application/json");
  else if (settings.contentType !== null) {
    for (const value of [].concat(settings.contentType)) add("Content-Type", value);
  }
  if (settings.authorization === undefined) add("Authorization", "Publisher " + TOKEN);
  else if (settings.authorization !== null) {
    for (const value of [].concat(settings.authorization)) add("Authorization", value);
  }
  for (const value of [].concat(settings.operationId === undefined ? [] : settings.operationId)) {
    add("X-Publication-Operation", value);
  }
  const subject = settings.subjectRef === undefined ? SUBJECT : settings.subjectRef;
  for (const value of [].concat(subject === null ? [] : subject)) {
    add("X-Publication-Subject", value);
  }
  for (const value of [].concat(settings.digest === undefined ? [] : settings.digest)) {
    add("X-Publication-Payload-Digest", value);
  }
  /* Same duplication-expressible shape as every other control header: the
   * period is a singleton, so a scenario can send it twice on purpose. */
  const period = settings.periodType === undefined ? "weekly" : settings.periodType;
  for (const value of [].concat(period === null ? [] : period)) {
    add("X-Publication-Period", value);
  }
  return edgeRequest(ORIGIN + "/api/publish", {
    method: "POST", headers: new Headers(pairs), body: settings.body,
  });
}

const publish = (world, options) => call(world, publishRequest(options));

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

const sha = (bytes) => digestLib.sha256Hex(bytes);

/** Everything a fail-closed claim has to be measured against. */
function snapshotState(world, op, objectKey) {
  const row = world.db.publications.get(op) || null;
  const stored = objectKey ? world.bucket.objects.get(objectKey) : null;
  const grants = [...world.db.capabilities.values()];
  return {
    phase: row ? row.state : null,
    ledger_digest: row ? row.payload_digest : null,
    capability_id_present: !!(row && row.capability_id),
    bearer_generation: row ? Number(row.bearer_generation) : null,
    puts: world.bucket.putLog.length,
    distinct_objects: world.bucket.objects.size,
    bytes_hex: stored ? hex(stored.bytes) : null,
    metadata: stored ? Object.assign({}, stored.customMetadata) : null,
    grant_rows: grants.length,
    live_grants: grants.filter((g) => !g.revoked_at && !g.rotated_to).length,
  };
}

/**
 * Drive an operation into THE critical window: the operation owns its key, the
 * object is written, and the ledger has not advanced past it.
 *
 * `stopAt` is the D1 site that is made to fail, which decides the phase:
 *   SNAPSHOT_WRITTEN  -> row stays CREATED, object on disk (post-R2/pre-ledger)
 *   GRANT_TRANSACTION -> row reaches SNAPSHOT_WRITTEN, no grant yet
 */
async function crashedWorld(seed, stopAt) {
  const world = await createWorld();
  const op = operationId(seed);
  const site = stopAt || bindings.SITE.SNAPSHOT_WRITTEN;
  world.db.failAt.set(site, "D1_UNAVAILABLE");
  const crashed = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  world.db.failAt.delete(site);
  const row = world.db.publications.get(op);
  const objectKey = row ? row.snapshot_object_key : null;
  return { world, op, objectKey, crashed, phase: row ? row.state : null };
}

/** The common shape of every fail-closed case: corrupt, retry, measure. */
async function failClosedCase(seed, corrupt, stopAt) {
  const { world, op, objectKey, crashed, phase } = await crashedWorld(seed, stopAt);
  const stored = world.bucket.objects.get(objectKey);
  const pristine = stored.bytes.slice();
  const pristineMetadata = Object.assign({}, stored.customMetadata);

  await corrupt({ world, stored, objectKey, op });

  const before = snapshotState(world, op, objectKey);
  const retry = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const after = snapshotState(world, op, objectKey);

  /* Restore the object exactly and confirm the operation still converges: a
   * fail-closed refusal that also poisoned the operation would be a different
   * bug wearing the same response code. */
  world.bucket.healObject(objectKey);
  const healed = world.bucket.objects.get(objectKey);
  if (healed) {
    healed.bytes = pristine.slice();
    healed.customMetadata = Object.assign({}, pristineMetadata);
  } else {
    world.bucket.objects.set(objectKey, {
      bytes: pristine.slice(),
      customMetadata: Object.assign({}, pristineMetadata),
      get body() { return new TextDecoder("utf-8").decode(this.bytes); },
    });
  }
  const resumed = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const resumedState = snapshotState(world, op, objectKey);

  return {
    crash_status: crashed.status,
    phase_after_crash: phase,
    retry_status: retry.status,
    retry_error: retry.json && retry.json.error,
    retry_result: retry.json && retry.json.status,
    retry_next_action: retry.json && retry.json.next_action,
    retry_bearer: !!(retry.json && retry.json.capability),
    /* The response must not name the failing invariant. */
    retry_body_names_invariant: /BINDING|DIGEST_META|LEDGER_DIGEST_MISMATCH|SUBJECT_BINDING|THREW/
      .test(retry.text),
    puts_before: before.puts,
    puts_after: after.puts,
    put_count_delta: after.puts - before.puts,
    bytes_before: before.bytes_hex,
    bytes_after: after.bytes_hex,
    bytes_unchanged: before.bytes_hex === after.bytes_hex,
    phase_before: before.phase,
    phase_after: after.phase,
    ledger_digest_before: before.ledger_digest,
    ledger_digest_after: after.ledger_digest,
    grant_rows_before: before.grant_rows,
    grant_rows_after: after.grant_rows,
    live_grants_after: after.live_grants,
    distinct_objects_after: after.distinct_objects,
    resumed_status: resumed.status,
    resumed_result: resumed.json && resumed.json.status,
    resumed_bearer: !!(resumed.json && resumed.json.capability),
    resumed_phase: resumedState.phase,
    resumed_live_grants: resumedState.live_grants,
    resumed_bytes_are_host_bytes: resumedState.bytes_hex === hex(CANONICAL_A),
  };
}

/* -------------------------------------------------------------- scenarios -- */

const scenarios = {};

/* --- A. valid existing object -------------------------------------------- */

scenarios.a_valid_existing_object_is_reused = async () => {
  const { world, op, objectKey, crashed, phase } = await crashedWorld("valid");
  const before = snapshotState(world, op, objectKey);
  const getsBefore = world.bucket.getLog.length;

  const retry = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const after = snapshotState(world, op, objectKey);

  /* And the grant that resulted is genuinely usable end to end. */
  const session = await exchange(world, retry.json && retry.json.capability);
  const cookie = (session.headers["set-cookie"] || "").split(";")[0];
  const read = await readSnapshot(world, cookie);

  return {
    crash_status: crashed.status,
    phase_after_crash: phase,
    retry_status: retry.status,
    retry_result: retry.json && retry.json.status,
    retry_bearer: !!(retry.json && retry.json.capability),
    puts_before: before.puts,
    puts_after: after.puts,
    put_count_delta: after.puts - before.puts,
    reads_happened: world.bucket.getLog.length > getsBefore,
    bytes_unchanged: before.bytes_hex === after.bytes_hex,
    bytes_are_host_bytes: after.bytes_hex === hex(CANONICAL_A),
    metadata_digest_after: after.metadata && after.metadata.payload_digest,
    phase_before: before.phase,
    phase_after: after.phase,
    distinct_objects: after.distinct_objects,
    grant_rows: after.grant_rows,
    live_grants: after.live_grants,
    session_status: session.status,
    snapshot_status: read.status,
  };
};

/* Same, but crashed one step later: the ledger already says SNAPSHOT_WRITTEN
 * and the retry has to prove the object before minting a grant over it. */
scenarios.a2_valid_object_at_snapshot_written = async () => {
  const { world, op, objectKey, phase } = await crashedWorld("vpre", bindings.SITE.GRANT_TRANSACTION);
  const before = snapshotState(world, op, objectKey);
  const retry = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const after = snapshotState(world, op, objectKey);
  return {
    phase_after_crash: phase,
    retry_status: retry.status,
    retry_result: retry.json && retry.json.status,
    put_count_delta: after.puts - before.puts,
    bytes_unchanged: before.bytes_hex === after.bytes_hex,
    phase_after: after.phase,
    live_grants: after.live_grants,
    distinct_objects: after.distinct_objects,
  };
};

/* --- B. body-only corruption --------------------------------------------- */

scenarios.b_body_only_corruption = () => failClosedCase("bodyc", async ({ stored }) => {
  const mutated = stored.bytes.slice();
  mutated[mutated.length - 2] = mutated[mutated.length - 2] === 0x31 ? 0x32 : 0x31;
  stored.bytes = mutated;
});

/* --- C. metadata-digest-only corruption ---------------------------------- */

scenarios.c_metadata_digest_corruption = () => failClosedCase("metac", async ({ stored }) => {
  stored.customMetadata = Object.assign({}, stored.customMetadata, {
    payload_digest: DIGEST_B,
  });
});

/* --- D. metadata digest missing ------------------------------------------ */

scenarios.d_metadata_digest_missing = () => failClosedCase("metad", async ({ stored }) => {
  const without = Object.assign({}, stored.customMetadata);
  delete without.payload_digest;
  stored.customMetadata = without;
});

/* --- E. body AND metadata rewritten consistently, ledger unchanged -------- */

scenarios.e_coordinated_rewrite_against_ledger = () =>
  failClosedCase("coord", async ({ stored }) => {
    /* A different, perfectly valid canonical snapshot, self-consistently
     * stamped. Only the ledger digest disagrees — which is exactly why the
     * ledger has to be one of the authorities. */
    stored.bytes = CANONICAL_B.slice();
    stored.customMetadata = Object.assign({}, stored.customMetadata, {
      payload_digest: DIGEST_B,
    });
  });

/* --- F. subject binding corrupted ---------------------------------------- */

scenarios.f_subject_binding_corrupted = () => failClosedCase("bindc", async ({ stored }) => {
  const binding = stored.customMetadata.subject_binding;
  stored.customMetadata = Object.assign({}, stored.customMetadata, {
    subject_binding: (binding.charAt(0) === "a" ? "b" : "a") + binding.slice(1),
  });
});

/* --- G. subject binding missing ------------------------------------------ */

scenarios.g_subject_binding_missing = () => failClosedCase("bindm", async ({ stored }) => {
  const without = Object.assign({}, stored.customMetadata);
  delete without.subject_binding;
  stored.customMetadata = without;
});

/* --- H. both digest and subject-binding metadata missing ----------------- */

scenarios.h_all_integrity_metadata_missing = () => failClosedCase("bothm", async ({ stored }) => {
  stored.customMetadata = {};
});

/* --- I. R2 get throws ---------------------------------------------------- */

scenarios.i_get_throws = () => failClosedCase("getth", async ({ world, objectKey }) => {
  world.bucket.failGetFor(objectKey, "R2_GET_FAILED");
});

/* --- J. body read throws ------------------------------------------------- */

scenarios.j_body_read_throws = () => failClosedCase("bodyt", async ({ world, objectKey }) => {
  world.bucket.failBodyReadFor(objectKey, "R2_BODY_READ_FAILED");
});

/* Metadata access throwing is the same class and is kept separate because it
 * is reached AFTER the body read succeeds. */
scenarios.j2_metadata_read_throws = () => failClosedCase("metat", async ({ world, objectKey }) => {
  world.bucket.failMetadataFor(objectKey, "R2_METADATA_READ_FAILED");
});

/* --- K. definitive absence is the only state that permits a write -------- */

scenarios.k_definitive_absence_permits_the_write = async () => {
  const world = await createWorld();
  const op = operationId("absen");
  const before = snapshotState(world, op, null);
  const first = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const row = world.db.publications.get(op);
  const objectKey = row.snapshot_object_key;
  const after = snapshotState(world, op, objectKey);

  /* And absence at SNAPSHOT_WRITTEN is NOT a write opportunity: the ledger
   * already says the object exists, so its disappearance is an incident and
   * ordinary retry must not re-create it. */
  const vanished = await crashedWorld("vanis", bindings.SITE.GRANT_TRANSACTION);
  const vanishedBefore = snapshotState(vanished.world, vanished.op, vanished.objectKey);
  vanished.world.bucket.objects.delete(vanished.objectKey);
  const vanishedRetry = await publish(vanished.world,
    { operationId: vanished.op, digest: DIGEST_A, body: CANONICAL_A });
  const vanishedAfter = snapshotState(vanished.world, vanished.op, vanished.objectKey);

  return {
    puts_before_first_publish: before.puts,
    first_status: first.status,
    first_result: first.json && first.json.status,
    first_bearer: !!(first.json && first.json.capability),
    puts_after: after.puts,
    distinct_objects: after.distinct_objects,
    bytes_are_host_bytes: after.bytes_hex === hex(CANONICAL_A),
    phase_after: after.phase,
    live_grants: after.live_grants,

    vanished_phase_before: vanishedBefore.phase,
    vanished_status: vanishedRetry.status,
    vanished_error: vanishedRetry.json && vanishedRetry.json.error,
    vanished_bearer: !!(vanishedRetry.json && vanishedRetry.json.capability),
    vanished_put_delta: vanishedAfter.puts - vanishedBefore.puts,
    vanished_phase_after: vanishedAfter.phase,
    vanished_grant_rows: vanishedAfter.grant_rows,
  };
};

/* --- L..O. malformed R2 RESULTS ------------------------------------------ */

/*
 * A malformed result is neither a failure nor an absence: `get()` RESOLVES,
 * and what it resolves is a value this code cannot account for. The re-review
 * confirmed two of these reaching a mutating outcome — `undefined` classified
 * as absence and then written over, and a result with no echoed key classified
 * as a proven object. All four shapes are driven through the same measured
 * fail-closed case as every other integrity failure.
 */

/* `get()` resolves `undefined` while the object is present and healthy. */
scenarios.l_result_undefined_fails_closed = () =>
  failClosedCase("undef", async ({ world, objectKey }) => {
    world.bucket.resolveUndefinedFor(objectKey);
  });

/* Correct body, correct metadata, no echoed key at all: identity unprovable. */
scenarios.m_result_without_key_fails_closed = () =>
  failClosedCase("nokey", async ({ world, objectKey }) => {
    world.bucket.omitEchoedKeyFor(objectKey);
  });

/* An echoed key that is not a string cannot be compared to the owned key. */
scenarios.n_result_non_string_key_fails_closed = () =>
  failClosedCase("badky", async ({ world, objectKey }) => {
    world.bucket.echoKeyAs(objectKey, 12345);
  });

/* A well-formed key naming a DIFFERENT object: the pre-existing integrity
 * failure, which this change must not weaken. */
scenarios.o_result_wrong_string_key_fails_closed = () =>
  failClosedCase("wrgky", async ({ world, objectKey }) => {
    world.bucket.echoKeyAs(objectKey, "ff/" + "Z".repeat(32) + ".json");
  });

/* --- P..Q. an ARRAY result ------------------------------------------------ */

/*
 * `typeof [] === "object"`, so an array passes the generic object test, and an
 * array can carry a `key`, an `arrayBuffer()` and a `customMetadata` — every
 * property this code reads. The re-review confirmed a fully decorated array
 * proving itself PRESENT_VALID and carrying publication all the way to a
 * grant and a raw bearer.
 */

scenarios.p_bare_array_fails_closed = () =>
  failClosedCase("bararr", async ({ world, objectKey }) => {
    world.bucket.resolveBareArrayFor(objectKey);
  });

/* The critical one: real key, real bytes, real metadata, real binding. Every
 * content check would pass; only the container is wrong. */
scenarios.q_decorated_array_fails_closed = () =>
  failClosedCase("decarr", async ({ world, objectKey }) => {
    world.bucket.resolveDecoratedArrayFor(objectKey);
  });

/* --- THE RECURRENCE DETECTOR --------------------------------------------- */

/*
 * The one test that fails if UNREADABLE is ever again treated as ABSENT.
 *
 * The object logically EXISTS — it is in the bucket, with correct bytes and
 * correct metadata — and only the read is broken. Under the previous
 * implementation `snapshotObjectDigest` reported `{ present: false }`, the
 * retry took the "nothing there, write it" branch and the object was
 * overwritten. So the assertion is deliberately blunt: the put count does not
 * move, at either of the two states in the critical window.
 */
scenarios.recurrence_unreadable_is_not_absent = async () => {
  const cases = {};
  for (const [name, site] of [
    ["created", bindings.SITE.SNAPSHOT_WRITTEN],
    ["snapshot_written", bindings.SITE.GRANT_TRANSACTION],
  ]) {
    const { world, op, objectKey, phase } = await crashedWorld("rec" + name.slice(0, 2), site);
    const stored = world.bucket.objects.get(objectKey);
    const bytesBefore = hex(stored.bytes);
    const putsBefore = world.bucket.putLog.length;

    /* The object is present and perfectly healthy. Only `get()` is broken. */
    world.bucket.failGetFor(objectKey, "R2_GET_FAILED");
    const retry = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });

    const stillThere = world.bucket.objects.get(objectKey);
    cases[name] = {
      phase_after_crash: phase,
      object_logically_exists: !!stillThere,
      retry_status: retry.status,
      retry_error: retry.json && retry.json.error,
      retry_result: retry.json && retry.json.status,
      retry_bearer: !!(retry.json && retry.json.capability),
      /* THE assertion. */
      put_count_delta: world.bucket.putLog.length - putsBefore,
      bytes_unchanged: hex(stillThere.bytes) === bytesBefore,
      phase_after: world.db.publications.get(op).state,
      grant_rows: world.db.capabilities.size,
      /* And once storage recovers, the operation converges with no repair. */
      recovered: await (async () => {
        world.bucket.healObject(objectKey);
        const again = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
        return {
          status: again.status,
          result: again.json && again.json.status,
          bearer: !!(again.json && again.json.capability),
          puts_total: world.bucket.putLog.length,
          bytes_are_host_bytes: hex(world.bucket.objects.get(objectKey).bytes) === hex(CANONICAL_A),
          live_grants: [...world.db.capabilities.values()]
            .filter((g) => !g.revoked_at && !g.rotated_to).length,
        };
      })(),
    };
  }

  /* The unit-level statement of the same thing, so the distinction is proven
   * at the inspection function and not only through the route. */
  const world = await createWorld();
  const key = "ab/" + "A".repeat(32) + ".json";
  const absent = await publisherLib.inspectSnapshotObject(
    { bucket: world.bucket, pepper: PEPPER },
    { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A });
  world.bucket.failGetFor(key, "R2_GET_FAILED");
  const broken = await publisherLib.inspectSnapshotObject(
    { bucket: world.bucket, pepper: PEPPER },
    { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A });
  const noBucket = await publisherLib.inspectSnapshotObject(
    { bucket: null, pepper: PEPPER },
    { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A });

  cases.unit = {
    absent_state: absent.state,
    absent_writable: absent.writable,
    unreadable_state: broken.state,
    unreadable_writable: broken.writable,
    unreadable_reusable: broken.reusable,
    no_bucket_state: noBucket.state,
    no_bucket_writable: noBucket.writable,
    states_differ: absent.state !== broken.state,
  };
  return cases;
};

/*
 * RECURRENCE DETECTOR 2 — a RESOLVED `undefined` is not an absence.
 *
 * The confirmed defect: `get()` resolved `undefined`, the shape check said
 * `object === null || object === undefined` and answered ABSENT, and the
 * publication retry wrote over an object that was sitting in the bucket,
 * intact, all along — then granted, then handed out a raw bearer.
 *
 * `guarded` is the production module driven through the real route at both
 * states of the critical window. The assertion is deliberately blunt: the R2
 * put count does not move.
 *
 * `bypassed` runs the same scenario against a copy of `publisher.js` whose
 * ABSENT rule is reverted to `object === null || object === undefined` and
 * nothing else changed. It must WRITE. That is what proves the assertion above
 * is testing this rule and not some incidental refusal.
 */
scenarios.recurrence_undefined_result_is_not_absent = async () => {
  const guarded = {};
  for (const [name, site] of [
    ["created", bindings.SITE.SNAPSHOT_WRITTEN],
    ["snapshot_written", bindings.SITE.GRANT_TRANSACTION],
  ]) {
    const { world, op, objectKey, phase } = await crashedWorld("un" + name.slice(0, 3), site);
    const stored = world.bucket.objects.get(objectKey);
    const bytesBefore = hex(stored.bytes);
    const putsBefore = world.bucket.putLog.length;

    /* The object is present and perfectly healthy. Only the RESULT is
     * malformed: `get()` resolves `undefined`. */
    world.bucket.resolveUndefinedFor(objectKey);
    const retry = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });

    const stillThere = world.bucket.objects.get(objectKey);
    guarded[name] = {
      phase_after_crash: phase,
      object_logically_exists: !!stillThere,
      retry_status: retry.status,
      retry_error: retry.json && retry.json.error,
      retry_bearer: !!(retry.json && retry.json.capability),
      /* THE assertion. */
      put_count_delta: world.bucket.putLog.length - putsBefore,
      bytes_unchanged: hex(stillThere.bytes) === bytesBefore,
      phase_after: world.db.publications.get(op).state,
      grant_rows: world.db.capabilities.size,
      live_grants: [...world.db.capabilities.values()]
        .filter((g) => !g.revoked_at && !g.rotated_to).length,
      /* Restore the exact result behaviour: the operation converges, unpoisoned. */
      restored: await (async () => {
        world.bucket.healObject(objectKey);
        const again = await publish(world,
          { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
        return {
          status: again.status,
          result: again.json && again.json.status,
          bearer: !!(again.json && again.json.capability),
          puts_total: world.bucket.putLog.length,
          bytes_are_host_bytes: hex(world.bucket.objects.get(objectKey).bytes) === hex(CANONICAL_A),
          live_grants: [...world.db.capabilities.values()]
            .filter((g) => !g.revoked_at && !g.rotated_to).length,
        };
      })(),
    };
  }

  /* --- the mutation proof ------------------------------------------------ */
  const mutant = await loadMutantPublisher("publisher_undefined_is_absent", (source) => {
    const site = `  if (object === null) {
    return { state: OBJECT_STATE.ABSENT, reason: null, reusable: false, writable: true };
  }
  if (object === undefined) {
    return unreadable(OBJECT_INSPECTION_REASON.RESULT_UNDEFINED);
  }`;
    if (!source.includes(site)) return null;
    return source.replace(site, `  if (object === null || object === undefined) {
    return { state: OBJECT_STATE.ABSENT, reason: null, reusable: false, writable: true };
  }`);
  });

  let bypassed;
  if (mutant.error) {
    bypassed = { mutation_error: mutant.error };
  } else {
    try {
      const { world, op, objectKey } = await crashedWorld("mutun", bindings.SITE.SNAPSHOT_WRITTEN);
      const putsBefore = world.bucket.putLog.length;
      world.bucket.resolveUndefinedFor(objectKey);
      const services = publicationServicesWith(world, mutant.module);

      /* The unit-level verdict the mutant reaches... */
      const verdict = await services.inspectObject({
        snapshot_object_key: objectKey, subject_ref: SUBJECT, payload_digest: DIGEST_A });
      /* ...and what the publication path then does with it. */
      const result = await publicationLib.publishSnapshot(services, {
        operation_id: op, subject_ref: SUBJECT, payload_digest: DIGEST_A,
        body: CANONICAL_A, ttl_seconds: 3600, now: T0,
      });
      bypassed = {
        mutant_verdict_state: verdict.state,
        mutant_verdict_writable: verdict.writable,
        result_status: result.status,
        bearer: !!result.capability,
        /* The mutant OVERWRITES: this is the defect, reproduced. */
        put_count_delta: world.bucket.putLog.length - putsBefore,
        grant_rows: world.db.capabilities.size,
      };
    } finally {
      await mutant.cleanup();
    }
  }

  /* And the same statement at the inspection function, both ways round. */
  const world = await createWorld();
  const key = "ab/" + "A".repeat(32) + ".json";
  const services = { bucket: world.bucket, pepper: PEPPER };
  const params = { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A };
  const genuineAbsence = await publisherLib.inspectSnapshotObject(services, params);
  await publisherLib.putSnapshotObject(services,
    { snapshot_object_key: key, subject_ref: SUBJECT, body: CANONICAL_A, payload_digest: DIGEST_A });
  world.bucket.resolveUndefinedFor(key);
  const malformed = await publisherLib.inspectSnapshotObject(services, params);

  return {
    guarded: guarded,
    bypassed: bypassed,
    unit: {
      null_state: genuineAbsence.state,
      null_writable: genuineAbsence.writable,
      undefined_state: malformed.state,
      undefined_reason: malformed.reason,
      undefined_writable: malformed.writable,
      undefined_reusable: malformed.reusable,
      states_differ: genuineAbsence.state !== malformed.state,
    },
  };
};

/*
 * RECURRENCE DETECTOR 3 — a result with no provable key is not a proven object.
 *
 * The confirmed defect: the comparison was `typeof object.key === "string" &&
 * object.key !== objectKey`, so a result carrying no key at all skipped it and
 * could still be classified PRESENT_VALID. The mutant restores exactly that
 * conditional; it must reach PRESENT_VALID where production refuses.
 */
scenarios.recurrence_key_equality_is_mandatory = async () => {
  const world = await createWorld();
  const services = { bucket: world.bucket, pepper: PEPPER };
  const key = publisherLib.mintObjectKey();
  await publisherLib.putSnapshotObject(services,
    { snapshot_object_key: key, subject_ref: SUBJECT, body: CANONICAL_A, payload_digest: DIGEST_A });
  const params = { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A };

  const inspectWith = async (module, mutate) => {
    world.bucket.healObject(key);
    mutate(world.bucket);
    const verdict = await module.inspectSnapshotObject(
      { bucket: world.bucket, pepper: PEPPER }, params);
    return { state: verdict.state, reason: verdict.reason,
             reusable: verdict.reusable, writable: verdict.writable };
  };

  const guarded = {
    healthy: await inspectWith(publisherLib, () => {}),
    key_missing: await inspectWith(publisherLib, (b) => b.omitEchoedKeyFor(key)),
    key_non_string: await inspectWith(publisherLib, (b) => b.echoKeyAs(key, 12345)),
    key_wrong_string: await inspectWith(publisherLib,
      (b) => b.echoKeyAs(key, "ff/" + "Z".repeat(32) + ".json")),
  };

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
      bypassed = {
        healthy: await inspectWith(mutant.module, () => {}),
        key_missing: await inspectWith(mutant.module, (b) => b.omitEchoedKeyFor(key)),
        key_non_string: await inspectWith(mutant.module, (b) => b.echoKeyAs(key, 12345)),
        key_wrong_string: await inspectWith(mutant.module,
          (b) => b.echoKeyAs(key, "ff/" + "Z".repeat(32) + ".json")),
      };
    } finally {
      await mutant.cleanup();
    }
  }

  world.bucket.healObject(key);
  return { guarded: guarded, bypassed: bypassed };
};

/*
 * RECURRENCE DETECTOR 4 — a decorated ARRAY is not an R2 object.
 *
 * The confirmed defect: `typeof [] === "object"`, so an array passed the shape
 * test, and every later check reads properties an array can carry. An Array
 * given the operation-owned key, a body reader over the real stored octets and
 * the real custom metadata proved itself PRESENT_VALID — publication then
 * PUBLISHED, GRANT_MINTED, one grant, one raw bearer.
 *
 * `guarded` is the production module through the real route. `bypassed` is the
 * same scenario with inspection supplied by a copy of `publisher.js` with ONLY
 * the array rejection removed. It must reach PRESENT_VALID and mutate.
 */
scenarios.recurrence_decorated_array_is_not_an_object = async () => {
  /* --- the shape gate is ordered BEFORE any content check ---------------- */
  const world = await createWorld();
  const services = { bucket: world.bucket, pepper: PEPPER };
  const key = publisherLib.mintObjectKey();
  await publisherLib.putSnapshotObject(services,
    { snapshot_object_key: key, subject_ref: SUBJECT, body: CANONICAL_A, payload_digest: DIGEST_A });
  const params = { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A };

  /* What the decorated array actually carries, recorded so the case is not
   * trusting the double's description of itself. */
  world.bucket.resolveDecoratedArrayFor(key);
  const value = await world.bucket.get(key);
  const carried = {
    is_array: Array.isArray(value),
    typeof_is_object: typeof value === "object",
    key_matches: value.key === key,
    has_body_reader: typeof value.arrayBuffer === "function",
    body_is_the_stored_octets:
      hex(new Uint8Array(await value.arrayBuffer())) === hex(CANONICAL_A),
    metadata_digest_is_correct: value.customMetadata.payload_digest === DIGEST_A,
    metadata_binding_present: typeof value.customMetadata.subject_binding === "string",
    binding_version: value.customMetadata.binding_version,
  };

  const inspectWith = async (module, mutate) => {
    world.bucket.healObject(key);
    mutate(world.bucket);
    const verdict = await module.inspectSnapshotObject(
      { bucket: world.bucket, pepper: PEPPER }, params);
    return { state: verdict.state, reason: verdict.reason,
             reusable: verdict.reusable, writable: verdict.writable };
  };

  const guardedUnit = {
    healthy: await inspectWith(publisherLib, () => {}),
    bare_array: await inspectWith(publisherLib, (b) => b.resolveBareArrayFor(key)),
    decorated_array: await inspectWith(publisherLib, (b) => b.resolveDecoratedArrayFor(key)),
    after_restoration: await inspectWith(publisherLib, () => {}),
  };

  /* --- the same, through the real publication route ---------------------- */
  const routed = {};
  for (const [name, site] of [
    ["created", bindings.SITE.SNAPSHOT_WRITTEN],
    ["snapshot_written", bindings.SITE.GRANT_TRANSACTION],
  ]) {
    const crashed = await crashedWorld("ar" + name.slice(0, 3), site);
    const stored = crashed.world.bucket.objects.get(crashed.objectKey);
    const bytesBefore = hex(stored.bytes);
    const before = snapshotState(crashed.world, crashed.op, crashed.objectKey);

    crashed.world.bucket.resolveDecoratedArrayFor(crashed.objectKey);
    const retry = await publish(crashed.world,
      { operationId: crashed.op, digest: DIGEST_A, body: CANONICAL_A });
    const after = snapshotState(crashed.world, crashed.op, crashed.objectKey);

    routed[name] = {
      phase_after_crash: crashed.phase,
      object_logically_exists: !!crashed.world.bucket.objects.get(crashed.objectKey),
      retry_status: retry.status,
      retry_error: retry.json && retry.json.error,
      retry_bearer: !!(retry.json && retry.json.capability),
      /* THE assertions. */
      put_count_delta: after.puts - before.puts,
      bytes_unchanged: after.bytes_hex === bytesBefore,
      phase_before: before.phase,
      phase_after: after.phase,
      ledger_digest_unchanged: before.ledger_digest === after.ledger_digest,
      grant_rows: after.grant_rows,
      live_grants: after.live_grants,
      distinct_objects: after.distinct_objects,
      /* Restore the exact valid result: the operation is unpoisoned. */
      restored: await (async () => {
        crashed.world.bucket.healObject(crashed.objectKey);
        const again = await publish(crashed.world,
          { operationId: crashed.op, digest: DIGEST_A, body: CANONICAL_A });
        const state = snapshotState(crashed.world, crashed.op, crashed.objectKey);
        return {
          status: again.status,
          result: again.json && again.json.status,
          bearer: !!(again.json && again.json.capability),
          puts_total: crashed.world.bucket.putLog.length,
          distinct_objects: state.distinct_objects,
          bytes_are_host_bytes: state.bytes_hex === hex(CANONICAL_A),
          phase: state.phase,
          grant_rows: state.grant_rows,
          live_grants: state.live_grants,
        };
      })(),
    };
  }

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
      const unit = {
        healthy: await inspectWith(mutant.module, () => {}),
        decorated_array: await inspectWith(mutant.module, (b) => b.resolveDecoratedArrayFor(key)),
        bare_array: await inspectWith(mutant.module, (b) => b.resolveBareArrayFor(key)),
      };
      const crashed = await crashedWorld("armut", bindings.SITE.SNAPSHOT_WRITTEN);
      const putsBefore = crashed.world.bucket.putLog.length;
      crashed.world.bucket.resolveDecoratedArrayFor(crashed.objectKey);
      const mutantServices = publicationServicesWith(crashed.world, mutant.module);
      const result = await publicationLib.publishSnapshot(mutantServices, {
        operation_id: crashed.op, subject_ref: SUBJECT, payload_digest: DIGEST_A,
        body: CANONICAL_A, ttl_seconds: 3600, now: T0,
      });
      bypassed = {
        unit: unit,
        /* The mutant reuses the array as a proven object and grants over it. */
        result_status: result.status,
        bearer: !!result.capability,
        put_count_delta: crashed.world.bucket.putLog.length - putsBefore,
        grant_rows: crashed.world.db.capabilities.size,
        phase: crashed.world.db.publications.get(crashed.op).state,
      };
    } finally {
      await mutant.cleanup();
    }
  }

  world.bucket.healObject(key);
  return { carried: carried, guarded: guardedUnit, routed: routed, bypassed: bypassed };
};

/* --- object-state model, exhaustively ------------------------------------ */

scenarios.object_state_model = async () => {
  const world = await createWorld();
  const services = { bucket: world.bucket, pepper: PEPPER };
  const key = publisherLib.mintObjectKey();
  await publisherLib.putSnapshotObject(
    Object.assign({}, services),
    { snapshot_object_key: key, subject_ref: SUBJECT, body: CANONICAL_A, payload_digest: DIGEST_A });
  const stored = world.bucket.objects.get(key);
  const pristine = Object.assign({}, stored.customMetadata);

  const inspect = (overrides) => publisherLib.inspectSnapshotObject(
    services,
    Object.assign({ snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A },
                  overrides || {}));

  const out = {};
  out.valid = await inspect();

  stored.customMetadata = Object.assign({}, pristine, { payload_digest_algorithm: "md5" });
  out.wrong_algorithm = await inspect();
  stored.customMetadata = Object.assign({}, pristine, { binding_version: "2" });
  out.wrong_binding_version = await inspect();
  stored.customMetadata = Object.assign({}, pristine);
  /* A binding that is valid for a DIFFERENT subject: the object is coherent
   * with itself but not with this operation. */
  out.wrong_subject = await inspect({ subject_ref: SUBJECT + "-other" });
  out.wrong_ledger_digest = await inspect({ payload_digest: DIGEST_B });
  out.malformed_ledger_digest = await inspect({ payload_digest: "not-a-digest" });
  out.missing_subject_ref = await inspect({ subject_ref: "" });

  /* And a get result that resolves but cannot be interpreted — a conditional
   * response carries no body. This must be UNREADABLE, never ABSENT. */
  const conditional = await publisherLib.inspectSnapshotObject(
    { bucket: { get: async () => ({ key: key }) }, pepper: PEPPER },
    { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A });
  out.body_undefined = conditional;

  /* Every RESOLVED result shape, at the inspection function itself. A stub
   * bucket is used for the ones the storage double cannot produce at all
   * (a non-object result); the rest are driven through the real double below
   * so the route-level and unit-level statements describe the same shapes. */
  stored.customMetadata = Object.assign({}, pristine);
  const withBucket = (get) => publisherLib.inspectSnapshotObject(
    { bucket: { get: get }, pepper: PEPPER },
    { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A });

  out.result_null = await withBucket(async () => null);
  out.result_undefined = await withBucket(async () => undefined);
  out.result_missing_return = await withBucket(async () => {});
  out.result_string = await withBucket(async () => "an object, honestly");
  out.result_number = await withBucket(async () => 0);
  out.result_false = await withBucket(async () => false);
  out.result_true = await withBucket(async () => true);

  /* The real double, with the real stored object behind it. */
  const real = (mutate) => {
    world.bucket.healObject(key);
    mutate(world.bucket);
    return publisherLib.inspectSnapshotObject(services,
      { snapshot_object_key: key, subject_ref: SUBJECT, payload_digest: DIGEST_A });
  };
  out.double_result_undefined = await real((b) => b.resolveUndefinedFor(key));
  out.double_key_missing = await real((b) => b.omitEchoedKeyFor(key));
  out.double_key_undefined_property = await real((b) => b.echoKeyAs(key, undefined));
  out.double_key_number = await real((b) => b.echoKeyAs(key, 12345));
  out.double_key_null = await real((b) => b.echoKeyAs(key, null));
  out.double_key_object = await real((b) => b.echoKeyAs(key, { toString: () => key }));
  out.double_key_wrong_string = await real((b) =>
    b.echoKeyAs(key, "ff/" + "Z".repeat(32) + ".json"));
  out.double_bare_array = await real((b) => b.resolveBareArrayFor(key));
  out.double_decorated_array = await real((b) => b.resolveDecoratedArrayFor(key));
  /* And, restored exactly, the healthy object is still PRESENT_VALID: none of
   * the above is allowed to have changed the object itself. */
  out.after_restoration = await real(() => {});

  return Object.fromEntries(Object.entries(out).map(([name, verdict]) => [name, {
    state: verdict.state, reason: verdict.reason,
    reusable: verdict.reusable, writable: verdict.writable,
  }]));
};

/* --- protocol shape: media type ------------------------------------------ */

scenarios.content_type_contract = async () => {
  const cases = {
    exact: "application/json",
    charset_utf8: "application/json; charset=utf-8",
    charset_utf8_no_space: "application/json;charset=utf-8",
    charset_uppercase: "Application/JSON; Charset=UTF-8",
    charset_quoted: 'application/json; charset="utf-8"',
    jsonp: "application/jsonp",
    json_patch: "application/json-patch+json",
    json_seq: "application/json-seq",
    jsonfoo: "application/jsonfoo",
    text_json: "text/json",
    text_application_json: "text/application/json",
    charset_evil: "application/json;charset=evil",
    unknown_parameter: "application/json; boundary=x",
    empty: "",
    wildcard: "*/*",
  };

  const results = {};
  let index = 0;
  for (const [name, contentType] of Object.entries(cases)) {
    const world = await createWorld();
    index += 1;
    const response = await publish(world, {
      operationId: operationId("ct" + index), digest: DIGEST_A,
      body: CANONICAL_A, contentType: contentType === "" ? null : contentType,
    });
    results[name] = {
      status: response.status,
      error: response.json && response.json.error,
      operations: world.db.publications.size,
      objects: world.bucket.objects.size,
      puts: world.bucket.putLog.length,
      grants: world.db.capabilities.size,
      bearer: !!(response.json && response.json.capability),
      parser: protocolLib.parsePublisherContentType(contentType),
    };
  }

  /* Two Content-Type field lines: the runtime joins them, and one body cannot
   * declare two media types. */
  const world = await createWorld();
  const duplicated = await publish(world, {
    operationId: operationId("ctdup"), digest: DIGEST_A, body: CANONICAL_A,
    contentType: ["application/json", "application/json"],
  });
  results.duplicated = {
    status: duplicated.status,
    error: duplicated.json && duplicated.json.error,
    operations: world.db.publications.size,
    objects: world.bucket.objects.size,
    puts: world.bucket.putLog.length,
    grants: world.db.capabilities.size,
    bearer: !!(duplicated.json && duplicated.json.capability),
    /* What the runtime actually handed the server. */
    combined_value: new Headers([["Content-Type", "application/json"],
                                 ["Content-Type", "application/json"]]).get("Content-Type"),
  };

  const conflicting = await createWorld();
  const conflict = await publish(conflicting, {
    operationId: operationId("ctcon"), digest: DIGEST_A, body: CANONICAL_A,
    contentType: ["application/json", "application/jsonp"],
  });
  results.duplicated_conflicting = {
    status: conflict.status,
    error: conflict.json && conflict.json.error,
    operations: conflicting.db.publications.size,
    objects: conflicting.bucket.objects.size,
    puts: conflicting.bucket.putLog.length,
    grants: conflicting.db.capabilities.size,
    bearer: !!(conflict.json && conflict.json.capability),
  };
  return results;
};

/* --- protocol shape: singleton control headers --------------------------- */

scenarios.singleton_header_contract = async () => {
  const validOperation = operationId("single");
  const results = {};

  async function attempt(name, settings) {
    const world = await createWorld();
    const response = await publish(world, Object.assign({
      digest: DIGEST_A, body: CANONICAL_A, operationId: validOperation,
    }, settings));
    results[name] = {
      status: response.status,
      error: response.json && response.json.error,
      operations: world.db.publications.size,
      objects: world.bucket.objects.size,
      puts: world.bucket.putLog.length,
      grants: world.db.capabilities.size,
      bearer: !!(response.json && response.json.capability),
    };
  }

  await attempt("baseline_valid", {});

  /* Operation. */
  await attempt("operation_duplicate_same", { operationId: [validOperation, validOperation] });
  await attempt("operation_duplicate_different",
    { operationId: [validOperation, operationId("other")] });
  await attempt("operation_comma_joined", { operationId: validOperation + "," + validOperation });
  await attempt("operation_missing", { operationId: undefined });

  /* Subject — the confirmed defect: `"a, a"` became a NEW subject. */
  await attempt("subject_duplicate_same", { subjectRef: [SUBJECT, SUBJECT] });
  await attempt("subject_duplicate_different", { subjectRef: [SUBJECT, SUBJECT + "-b"] });
  await attempt("subject_comma_joined", { subjectRef: SUBJECT + ", " + SUBJECT });
  await attempt("subject_missing", { subjectRef: null });

  /* Payload digest. */
  await attempt("digest_duplicate_same", { digest: [DIGEST_A, DIGEST_A] });
  await attempt("digest_duplicate_different", { digest: [DIGEST_A, DIGEST_B] });
  await attempt("digest_comma_joined", { digest: DIGEST_A + "," + DIGEST_A });
  await attempt("digest_missing", { digest: undefined });

  /* Authorization: existing guarantees must hold, and a combined credential
   * must fail closed exactly like every other auth failure (404). */
  await attempt("authorization_duplicate_same",
    { authorization: ["Publisher " + TOKEN, "Publisher " + TOKEN] });
  await attempt("authorization_duplicate_different",
    { authorization: ["Publisher " + TOKEN, "Publisher " + capabilityLib.generateCapability()] });
  await attempt("authorization_comma_joined",
    { authorization: "Publisher " + TOKEN + ",Publisher " + TOKEN });
  await attempt("authorization_missing", { authorization: null });
  await attempt("authorization_wrong_scheme", { authorization: "Bearer " + TOKEN });

  /* What the runtime really produces for a duplicated header, recorded so the
   * test is asserting against the actual representation and not a guess. */
  results.runtime_combination = {
    subject: new Headers([["X-Publication-Subject", SUBJECT],
                          ["X-Publication-Subject", SUBJECT]]).get("X-Publication-Subject"),
    authorization: new Headers([["Authorization", "Publisher a"],
                                ["Authorization", "Publisher b"]]).get("Authorization"),
  };
  return results;
};

/* --- delivery/recover routes carry the same contract --------------------- */

scenarios.other_routes_reject_ambiguous_headers = async () => {
  const world = await createWorld();
  const op = operationId("routes");
  const published = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const capabilityId = published.json.capability_id;
  const before = snapshotState(world, op, world.db.publications.get(op).snapshot_object_key);

  const post = (pathname, pairs) => call(world, edgeRequest(ORIGIN + pathname, {
    method: "POST",
    headers: new Headers([["Authorization", "Publisher " + TOKEN]].concat(pairs)),
  }));

  const recoverDuplicate = await post("/api/publish/recover",
    [["X-Publication-Operation", op], ["X-Publication-Operation", op]]);
  const deliveryDuplicateOperation = await post("/api/publish/delivery",
    [["X-Publication-Operation", op], ["X-Publication-Operation", op],
     ["X-Publication-Phase", "INTENT"], ["X-Publication-Capability", capabilityId]]);
  const deliveryDuplicateCapability = await post("/api/publish/delivery",
    [["X-Publication-Operation", op], ["X-Publication-Phase", "INTENT"],
     ["X-Publication-Capability", capabilityId], ["X-Publication-Capability", capabilityId]]);
  const deliveryDuplicatePhase = await post("/api/publish/delivery",
    [["X-Publication-Operation", op], ["X-Publication-Phase", "INTENT"],
     ["X-Publication-Phase", "DELIVERED"], ["X-Publication-Capability", capabilityId]]);
  const after = snapshotState(world, op, world.db.publications.get(op).snapshot_object_key);

  /* The same routes still work with singleton headers. */
  const intent = await post("/api/publish/delivery",
    [["X-Publication-Operation", op], ["X-Publication-Phase", "INTENT"],
     ["X-Publication-Capability", capabilityId]]);

  return {
    recover_duplicate_status: recoverDuplicate.status,
    recover_duplicate_error: recoverDuplicate.json && recoverDuplicate.json.error,
    recover_duplicate_bearer: !!(recoverDuplicate.json && recoverDuplicate.json.capability),
    delivery_duplicate_operation_status: deliveryDuplicateOperation.status,
    delivery_duplicate_capability_status: deliveryDuplicateCapability.status,
    delivery_duplicate_phase_status: deliveryDuplicatePhase.status,
    phase_before: before.phase,
    phase_after: after.phase,
    grant_rows_unchanged: before.grant_rows === after.grant_rows,
    bearer_generation_unchanged: before.bearer_generation === after.bearer_generation,
    singleton_intent_status: intent.status,
    singleton_intent_result: intent.json && intent.json.status,
  };
};

/* --- contention regression, with the new gate in place ------------------- */

scenarios.contention_after_the_fix = async () => {
  const out = {};
  for (const callers of [2, 8, 32, 128]) {
    const world = await createWorld();
    const op = operationId("con" + callers);
    const responses = await Promise.all(Array.from({ length: callers }, () =>
      publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A })));
    const row = world.db.publications.get(op);
    const grants = [...world.db.capabilities.values()];
    out["callers_" + callers] = {
      operations: world.db.publications.size,
      distinct_objects: world.bucket.objects.size,
      grant_rows: grants.length,
      live_grants: grants.filter((g) => !g.revoked_at && !g.rotated_to).length,
      bearers_returned: responses.filter((r) => r.json && r.json.capability).length,
      created_statuses: responses.filter((r) => r.status === 201).length,
      integrity_failures: responses.filter((r) => r.json && (
        r.json.error === "OBJECT_INTEGRITY_FAILURE" || r.json.error === "OBJECT_UNREADABLE")).length,
      state: row.state,
      bytes_are_host_bytes: hex(world.bucket.objects.get(row.snapshot_object_key).bytes) ===
        hex(CANONICAL_A),
      ledger_digest: row.payload_digest,
      /* Redundant puts of the SAME key with the SAME bytes are permitted and
       * converge; a second key would not be. */
      puts: world.bucket.putLog.length,
      distinct_put_keys: new Set(world.bucket.putLog).size,
    };
  }
  return out;
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
