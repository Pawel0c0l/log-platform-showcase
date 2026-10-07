/* Harness for the pre-publisher security gate.
 *
 * Executes the REAL Worker and the REAL publisher/publication modules against
 * projection-accurate in-memory D1/R2 bindings. No wrangler, no credentials, no
 * remote Cloudflare resource, no e-mail.
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
const publisher = await import(path.join(DELIVERY, "worker", "lib", "publisher.js"));
const publication = await import(path.join(DELIVERY, "worker", "lib", "publication.js"));
const publisherAuth = await import(path.join(DELIVERY, "worker", "lib", "publisher_auth.js"));
const capabilityLib = await import(path.join(DELIVERY, "worker", "lib", "capability.js"));
const bodyLib = await import(path.join(DELIVERY, "worker", "lib", "body.js"));
/* LOCAL-ONLY grant fabrication. The Worker has no unconditional grant insert:
 * see delivery/driver_eco_dashboard/local/dev_grants.js. */
const devGrants = await import(path.join(DELIVERY, "local", "dev_grants.js"));
const { D1AuthorizationStore, CAPABILITY_COLUMNS } = await import(path.join(DELIVERY, "worker", "lib", "store.js"));

const ORIGIN = "https://dashboard.example.invalid";
/* Cloudflare sets this on every dispatched request; the session rate limiter
 * keys on it. Synthetic TEST-NET-3 address, never a real peer. */
const CLIENT_IP = "203.0.113.10";
const T0 = 1_800_000_000;
const PUBLISHER_TOKEN = capabilityLib.generateCapability();
const FIXTURE_A = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_acceptable.json"));

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
  if (settings.publisherConfigured !== false) {
    env.PUBLISHER_KEY_DIGEST = await publisherAuth.publisherKeyDigest(PUBLISHER_TOKEN, settings.pepper);
  }
  return { db, bucket, env, logs, clock, store: new D1AuthorizationStore(db) };
}

function services(world) {
  return {
    db: world.db,
    store: world.store,
    bucket: world.bucket,
    pepper: world.env.CAPABILITY_PEPPER,
  };
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

/* The host is required to name the capability it is delivering; in these
 * scenarios the host's record is simply the current ledger value. */
function currentCapabilityId(world, op) {
  const row = world.db.publications.get(op);
  return row ? row.capability_id : "0".repeat(32);
}

async function publishRequest(options) {
  const settings = options || {};
  const body = settings.body === undefined ? FIXTURE_A : settings.body;
  const headers = {
    "Content-Type": settings.contentType || "application/json",
    "X-Publication-Operation": settings.operationId,
    "X-Publication-Subject": settings.subjectRef === undefined ? "subject-alpha" : settings.subjectRef,
    "X-Publication-Payload-Digest": settings.digest === undefined ? await sha256Hex(body) : settings.digest,
    /* The reporting period decides the capability lifetime and has no default;
     * `null` here expresses "omit it entirely" for the scenarios that check the
     * route fails closed without it. */
    "X-Publication-Period": settings.periodType === undefined ? "weekly"
      : (settings.periodType === null ? undefined : settings.periodType),
  };
  if (settings.credential !== null) {
    headers.Authorization = "Publisher " + (settings.credential || PUBLISHER_TOKEN);
  }
  for (const [name, value] of Object.entries(settings.extraHeaders || {})) headers[name] = value;
  const clean = {};
  for (const [name, value] of Object.entries(headers)) if (value !== undefined) clean[name] = value;
  return edgeRequest(ORIGIN + (settings.path || "/api/publish"), {
    method: settings.method || "POST", headers: clean, body: settings.method === "GET" ? undefined : body,
  });
}

/* Rotation writes a NEW `expires_at`, and `rotateCapability` no longer carries
 * a default lifetime — an unstated one is refused rather than silently filled
 * in. These scenarios exercise the rotation compare-and-set, not the
 * period-scoped lifetime policy, so they all state the same explicit hour. */
const ROTATION_TTL_SECONDS = 3600;
const rotateGrant = (services, params) =>
  publisher.rotateCapability(services, { ttl_seconds: ROTATION_TTL_SECONDS, ...params });

/* -------------------------------------------------------------- scenarios -- */

const scenarios = {};

/* ------------------------------------------ FINDING 1: D1 projection ------ */

scenarios.projection_includes_rotation_state = async () => {
  const world = await createWorld();
  const grant = await devGrants.issueDevCapability(services(world), {
    subject_ref: "subject-alpha", snapshot_object_key: publisher.mintObjectKey(),
    now: world.clock.now, ttl_seconds: 3600,
  });
  const row = await world.store.findCapabilityById(grant.capability_id);
  return {
    projected_columns: Object.keys(row).sort(),
    declared_columns: CAPABILITY_COLUMNS.split(",").map((c) => c.trim()).sort(),
    includes_rotated_to: Object.prototype.hasOwnProperty.call(row, "rotated_to"),
    no_select_star: !CAPABILITY_COLUMNS.includes("*"),
  };
};

scenarios.emulator_models_projection = async () => {
  const world = await createWorld();
  const grant = await devGrants.issueDevCapability(services(world), {
    subject_ref: "subject-alpha", snapshot_object_key: publisher.mintObjectKey(),
    now: world.clock.now, ttl_seconds: 3600,
  });
  /* A narrowed projection must expose ONLY those columns — the emulator used to
   * return the whole stored row, which is what hid the missing `rotated_to`. */
  const narrow = await world.db
    .prepare("SELECT capability_id, revoked_at FROM eco_capability WHERE capability_id = ?1")
    .bind(grant.capability_id)
    .first();
  let unknownColumnRejected = false;
  try {
    await world.db
      .prepare("SELECT capability_id, no_such_column FROM eco_capability WHERE capability_id = ?1")
      .bind(grant.capability_id)
      .first();
  } catch (error) {
    unknownColumnRejected = true;
  }
  return {
    narrow_columns: Object.keys(narrow).sort(),
    narrow_hides_rotated_to: !Object.prototype.hasOwnProperty.call(narrow, "rotated_to"),
    narrow_hides_subject: !Object.prototype.hasOwnProperty.call(narrow, "subject_ref"),
    unknown_column_rejected: unknownColumnRejected,
  };
};

/* Regression detector: simulate the OLD projection and prove the contract test
 * would fail. If this stops reproducing the defect, the emulator has become
 * permissive again. */
scenarios.narrowed_projection_reproduces_the_defect = async () => {
  const world = await createWorld();
  const objectKey = publisher.mintObjectKey();
  const grant = await devGrants.issueDevCapability(services(world), {
    subject_ref: "subject-alpha", snapshot_object_key: objectKey,
    now: world.clock.now, ttl_seconds: 3600,
  });
  await rotateGrant(services(world), {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });

  const brokenStore = new D1AuthorizationStore(world.db);
  /* Exactly the pre-fix projection: no `rotated_to`. */
  brokenStore.findCapabilityById = async (capabilityId) => {
    const row = await world.db
      .prepare(
        `SELECT capability_id, subject_ref, snapshot_object_key, expires_at, revoked_at, session_epoch
           FROM eco_capability WHERE capability_id = ?1`
      )
      .bind(capabilityId)
      .first();
    return row || null;
  };
  const misclassified = await rotateGrant(
    { store: brokenStore, bucket: world.bucket, pepper: world.env.CAPABILITY_PEPPER },
    { capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600 }
  );
  const correct = await rotateGrant(services(world), {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });
  return {
    narrowed_projection_status: misclassified.status,
    correct_projection_status: correct.status,
    defect_is_detectable: misclassified.status !== correct.status,
    narrowed_minted_nothing: !Object.prototype.hasOwnProperty.call(misclassified, "capability"),
  };
};

scenarios.rotation_retry_matrix = async () => {
  const cases = {};

  const world = await createWorld();
  const grant = await devGrants.issueDevCapability(services(world), {
    subject_ref: "subject-alpha", snapshot_object_key: publisher.mintObjectKey(),
    now: world.clock.now, ttl_seconds: 3600,
  });
  const first = await rotateGrant(services(world), {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });
  const rowsAfterFirst = world.db.capabilities.size;
  const retry = await rotateGrant(services(world), {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });
  const laterRetry = await rotateGrant(services(world), {
    capability_id: grant.capability_id, now: world.clock.now + 5000, ttl_seconds: 3600,
  });
  cases.first = first.status;
  cases.retry = retry.status;
  cases.later_retry = laterRetry.status;
  cases.retry_names_successor = retry.successor_capability_id === first.capability_id;
  cases.later_retry_names_successor = laterRetry.successor_capability_id === first.capability_id;
  cases.retry_minted_zero_rows = world.db.capabilities.size === rowsAfterFirst;

  /* Response-loss shape: the caller never saw the first answer and simply
   * repeats the same call. It must learn ALREADY_ROTATED, not REVOKED. */
  cases.response_loss_retry = retry.status === "ALREADY_ROTATED" ? "ALREADY_ROTATED" : retry.status;

  for (const width of [2, 8, 32]) {
    const contended = await createWorld();
    const seed = await devGrants.issueDevCapability(services(contended), {
      subject_ref: "subject-alpha", snapshot_object_key: publisher.mintObjectKey(),
      now: contended.clock.now, ttl_seconds: 3600,
    });
    const attempts = await Promise.all(
      Array.from({ length: width }, () =>
        rotateGrant(services(contended), {
          capability_id: seed.capability_id, now: contended.clock.now, ttl_seconds: 3600,
        })
      )
    );
    const live = [...contended.db.capabilities.values()].filter(
      (r) => (r.revoked_at === null || r.revoked_at === undefined) && !r.rotated_to
    );
    cases["contention_" + width] = {
      rotated: attempts.filter((a) => a.status === "ROTATED").length,
      conflicts: attempts.filter((a) => a.status === "ALREADY_ROTATED").length,
      live: live.length,
      rows: contended.db.capabilities.size,
    };
  }

  const revokedWorld = await createWorld();
  const revokedGrant = await devGrants.issueDevCapability(services(revokedWorld), {
    subject_ref: "subject-alpha", snapshot_object_key: publisher.mintObjectKey(),
    now: revokedWorld.clock.now, ttl_seconds: 3600,
  });
  await publisher.revokeCapability(services(revokedWorld), {
    capability_id: revokedGrant.capability_id, now: revokedWorld.clock.now,
  });
  cases.revoke_before_rotate = (await rotateGrant(services(revokedWorld), {
    capability_id: revokedGrant.capability_id, now: revokedWorld.clock.now, ttl_seconds: 3600,
  })).status;

  const orderWorld = await createWorld();
  const orderGrant = await devGrants.issueDevCapability(services(orderWorld), {
    subject_ref: "subject-alpha", snapshot_object_key: publisher.mintObjectKey(),
    now: orderWorld.clock.now, ttl_seconds: 3600,
  });
  await rotateGrant(services(orderWorld), {
    capability_id: orderGrant.capability_id, now: orderWorld.clock.now, ttl_seconds: 3600,
  });
  const revokeAfter = await publisher.revokeCapability(services(orderWorld), {
    capability_id: orderGrant.capability_id, now: orderWorld.clock.now + 1,
  });
  cases.rotate_before_revoke = {
    revoke_status: revokeAfter.status,
    live: [...orderWorld.db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to).length,
  };
  return cases;
};

/* ------------------------------------- FINDING 2: bounded request body ----- */
/*
 * The previous fixture emitted ~1 KiB chunks and its assertions therefore
 * "proved" a ~1 KiB consumption ceiling that the code does not provide: one
 * read() returns one whole chunk, so a peer that sends one 5 MiB chunk hands
 * the reader 5 MiB. Every probe below now reports `mode` and
 * `largest_chunk_bytes`, and the Python assertions are written per mode so no
 * test can claim a bound the runtime does not give.
 */

function countingStream(totalBytes, chunkBytes, counter) {
  let sent = 0;
  return new ReadableStream({
    pull(controller) {
      if (sent >= totalBytes) { controller.close(); return; }
      const size = Math.min(chunkBytes, totalBytes - sent);
      sent += size;
      counter.pulled += size;
      controller.enqueue(new Uint8Array(size));
    },
    cancel() { counter.cancelled = true; },
  });
}

/* A real byte stream, which is the only kind a BYOB reader can attach to. */
function byteStream(totalBytes, chunkBytes, counter) {
  let sent = 0;
  return new ReadableStream({
    type: "bytes",
    pull(controller) {
      if (sent >= totalBytes) { controller.close(); return; }
      const size = Math.min(chunkBytes, totalBytes - sent);
      sent += size;
      counter.pulled += size;
      controller.enqueue(new Uint8Array(size));
    },
    cancel() { counter.cancelled = true; },
  });
}

function bodyRequest(stream, headers) {
  return edgeRequest(ORIGIN + "/api/session", {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, headers || {}),
    body: stream,
    duplex: "half",
  });
}

/* Establishes, from the runtime itself, whether an inbound Request.body can
 * give a BYOB reader here. Nothing downstream is allowed to assume it. */
scenarios.byob_platform_probe = async () => {
  const counter = { pulled: 0, cancelled: false };
  const request = bodyRequest(countingStream(4096, 4096, counter));
  const reader = bodyLib.tryByobReader(request.body);
  if (reader) { try { await reader.cancel(); } catch (error) { /* ignore */ } }

  /* Control: a stream that IS a byte stream must give one, which proves the
   * BYOB code path is exercised rather than dead. */
  const byteCounter = { pulled: 0, cancelled: false };
  const byteReader = bodyLib.tryByobReader(byteStream(4096, 4096, byteCounter));
  if (byteReader) { try { await byteReader.cancel(); } catch (error) { /* ignore */ } }

  return {
    request_body_supports_byob: reader !== null,
    byte_stream_supports_byob: byteReader !== null,
    runtime: process.version,
    byob_buffer_bytes: bodyLib.BYOB_BUFFER_BYTES,
  };
};

scenarios.session_body_is_bounded = async () => {
  const LIMIT = 512;
  const cases = {};

  /* (Q1) Declared oversize: nothing is read at all. Unconditional. */
  const declaredCounter = { pulled: 0, cancelled: false };
  const declaredResult = await bodyLib.readBoundedBody(
    bodyRequest(countingStream(5 * 1024 * 1024, 64 * 1024, declaredCounter),
                { "Content-Length": String(5 * 1024 * 1024) }), LIMIT);
  cases.declared_oversize = {
    mode: declaredResult.mode, reason: declaredResult.reason,
    bytes_read: declaredResult.bytesRead, pulled: declaredCounter.pulled,
  };

  /* (Q2) ONE multi-megabyte chunk, no Content-Length. This is the probe the
   * previous suite lacked. In default mode a single read hands us the whole
   * 5 MiB; the result records that truthfully instead of hiding it. */
  const oneChunkCounter = { pulled: 0, cancelled: false };
  const oneChunkResult = await bodyLib.readBoundedBody(
    bodyRequest(countingStream(5 * 1024 * 1024, 5 * 1024 * 1024, oneChunkCounter)), LIMIT);
  cases.one_chunk_five_mib = {
    mode: oneChunkResult.mode, reason: oneChunkResult.reason,
    bytes_read: oneChunkResult.bytesRead,
    largest_chunk_bytes: oneChunkResult.largestChunkBytes,
    pulled: oneChunkCounter.pulled,
    total_offered: 5 * 1024 * 1024,
  };

  /* (Q2b) The same one-chunk attack against a real BYTE stream, where the BYOB
   * path applies. Here the per-read cost is ours, so `largest_chunk_bytes`
   * must not exceed our own buffer no matter how big the source chunk was. */
  const byobCounter = { pulled: 0, cancelled: false };
  const byobStream = byteStream(5 * 1024 * 1024, 5 * 1024 * 1024, byobCounter);
  const byobRequest = { headers: new Headers({ "Content-Type": "application/json" }), body: byobStream };
  const byobResult = await bodyLib.readBoundedBody(byobRequest, LIMIT);
  cases.one_chunk_five_mib_byte_stream = {
    mode: byobResult.mode, reason: byobResult.reason,
    bytes_read: byobResult.bytesRead,
    largest_chunk_bytes: byobResult.largestChunkBytes,
    buffer_bytes: bodyLib.BYOB_BUFFER_BYTES,
  };

  /* (Q3) Tiny chunks, oversized total: the ceiling trips mid-stream. */
  const chunkyCounter = { pulled: 0, cancelled: false };
  const chunkyResult = await bodyLib.readBoundedBody(
    bodyRequest(countingStream(1024 * 1024, 17, chunkyCounter)), LIMIT);
  cases.chunked_oversize = {
    mode: chunkyResult.mode, reason: chunkyResult.reason,
    bytes_read: chunkyResult.bytesRead,
    largest_chunk_bytes: chunkyResult.largestChunkBytes,
    pulled: chunkyCounter.pulled, cancelled: chunkyCounter.cancelled,
    total_offered: 1024 * 1024,
  };

  /* Boundary behaviour. */
  const exact = "x".repeat(LIMIT);
  const exactResult = await bodyLib.readBoundedBody(
    edgeRequest(ORIGIN + "/api/session", { method: "POST", body: exact }), LIMIT);
  const over = "x".repeat(LIMIT + 1);
  const overResult = await bodyLib.readBoundedBody(
    edgeRequest(ORIGIN + "/api/session", { method: "POST", body: over }), LIMIT);
  cases.exactly_at_limit = { ok: exactResult.ok, bytes_read: exactResult.bytesRead };
  cases.limit_plus_one = { ok: overResult.ok, reason: overResult.reason };

  /* Malformed Content-Length must not be trusted as a bypass. */
  const malformedCounter = { pulled: 0, cancelled: false };
  const malformedResult = await bodyLib.readBoundedBody(
    bodyRequest(countingStream(64 * 1024, 1024, malformedCounter),
                { "Content-Length": "not-a-number" }), LIMIT);
  cases.malformed_content_length = {
    mode: malformedResult.mode, reason: malformedResult.reason,
    bytes_read: malformedResult.bytesRead, pulled: malformedCounter.pulled,
  };
  cases.declared_length_parse = {
    absent: bodyLib.declaredLength(edgeRequest(ORIGIN + "/x", { method: "GET" })),
    malformed: bodyLib.declaredLength(edgeRequest(ORIGIN + "/x", {
      method: "POST", headers: { "Content-Length": "1,1" }, body: "" })),
  };
  return cases;
};

scenarios.session_endpoint_rejects_oversized_bodies = async () => {
  const world = await createWorld();
  const objectKey = publisher.mintObjectKey();
  await publisher.putSnapshotObject(services(world), {
    snapshot_object_key: objectKey, subject_ref: "subject-alpha", body: FIXTURE_A,
  });
  const grant = await devGrants.issueDevCapability(services(world), {
    subject_ref: "subject-alpha", snapshot_object_key: objectKey,
    now: world.clock.now, ttl_seconds: 3600,
  });

  const counter = { pulled: 0, cancelled: false };
  const oversized = edgeRequest(ORIGIN + "/api/session", {
    method: "POST",
    headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: countingStream(5 * 1024 * 1024, 4096, counter),
    duplex: "half",
  });
  const rejected = await call(world, oversized);

  /* Declared oversize is refused without reading a byte. */
  const declaredCounter = { pulled: 0, cancelled: false };
  const declared = edgeRequest(ORIGIN + "/api/session", {
    method: "POST",
    headers: {
      "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN,
      "Content-Length": String(5 * 1024 * 1024),
    },
    body: countingStream(5 * 1024 * 1024, 4096, declaredCounter),
    duplex: "half",
  });
  const declaredRejected = await call(world, declared);

  const padded = JSON.stringify({ capability: grant.capability, padding: "x".repeat(2000) });
  const paddedResult = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN }, body: padded,
  }));

  const normal = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: grant.capability }),
  }));

  return {
    oversized_status: rejected.status,
    oversized_bytes_pulled: counter.pulled,
    oversized_stream_cancelled: counter.cancelled,
    oversized_total_offered: 5 * 1024 * 1024,
    declared_oversize_status: declaredRejected.status,
    declared_oversize_bytes_pulled: declaredCounter.pulled,
    oversized_body_echoes_capability: rejected.text.includes(grant.capability),
    padded_status: paddedResult.status,
    normal_status: normal.status,
    normal_sets_cookie: !!normal.headers["set-cookie"],
    logs_leak_capability: world.logs.join("\n").includes(grant.capability),
  };
};

/* -------------------------- FINDING 3: publication idempotency ------------ */

scenarios.publication_operation_lifecycle = async () => {
  const world = await createWorld();
  const op = operationId("life");
  const first = await call(world, await publishRequest({ operationId: op }));
  const rowsAfterFirst = world.db.capabilities.size;

  const retry = await call(world, await publishRequest({ operationId: op }));
  const intent = await call(world, await publishRequest({
    operationId: op, path: "/api/publish/delivery", body: "",
    extraHeaders: { "X-Publication-Phase": "INTENT", "X-Publication-Capability": currentCapabilityId(world, op) },
  }));
  const intentRepeat = await call(world, await publishRequest({
    operationId: op, path: "/api/publish/delivery", body: "",
    extraHeaders: { "X-Publication-Phase": "INTENT", "X-Publication-Capability": currentCapabilityId(world, op) },
  }));
  const delivered = await call(world, await publishRequest({
    operationId: op, path: "/api/publish/delivery", body: "",
    extraHeaders: { "X-Publication-Phase": "DELIVERED", "X-Publication-Capability": currentCapabilityId(world, op) },
  }));
  const retryAfterDelivered = await call(world, await publishRequest({ operationId: op }));

  return {
    first_status: first.status,
    first_state: first.json && first.json.operation.state,
    first_returned_bearer: !!(first.json && first.json.capability),
    retry_status: retry.status,
    retry_result: retry.json && retry.json.status,
    retry_returned_bearer: !!(retry.json && retry.json.capability),
    retry_minted_zero_rows: world.db.capabilities.size === rowsAfterFirst,
    intent_status: intent.json && intent.json.status,
    intent_repeat_status: intentRepeat.json && intentRepeat.json.status,
    delivered_state: delivered.json && delivered.json.operation.state,
    retry_after_delivered: retryAfterDelivered.json && retryAfterDelivered.json.status,
    objects_written: world.bucket.objects.size,
    live_grants: [...world.db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to).length,
  };
};

scenarios.publication_conflict_and_isolation = async () => {
  const world = await createWorld();
  const op = operationId("conf");
  const first = await call(world, await publishRequest({ operationId: op }));

  /* Same operation id, different bytes → conflict, nothing mutated. */
  const other = JSON.stringify({ ...JSON.parse(FIXTURE_A), generated_at_utc: "2026-07-21T04:40:11Z" });
  const conflicting = await call(world, await publishRequest({ operationId: op, body: other }));
  /* Same operation id, different subject → conflict too. */
  const otherSubject = await call(world, await publishRequest({ operationId: op, subjectRef: "subject-beta" }));

  const objectsAfterConflicts = world.bucket.objects.size;

  /* Two distinct operation ids stay independent. */
  const second = await call(world, await publishRequest({ operationId: operationId("conf2") }));

  /* Concurrent execution of the same logical operation. */
  const concurrentWorld = await createWorld();
  const concurrentOp = operationId("race");
  const request = () => publishRequest({ operationId: concurrentOp });
  const attempts = await Promise.all([
    call(concurrentWorld, await request()),
    call(concurrentWorld, await request()),
    call(concurrentWorld, await request()),
    call(concurrentWorld, await request()),
  ]);

  return {
    first_status: first.status,
    conflicting_payload_status: conflicting.status,
    conflicting_payload_error: conflicting.json && conflicting.json.error,
    conflicting_subject_status: otherSubject.status,
    conflicts_wrote_nothing: objectsAfterConflicts === 1,
    distinct_operation_status: second.status,
    distinct_operations_independent: world.db.publications.size === 2,
    concurrent_published: attempts.filter((a) => a.status === 201).length,
    concurrent_replayed: attempts.filter((a) => a.status === 200).length,
    concurrent_bearers: attempts.filter((a) => a.json && a.json.capability).length,
    concurrent_objects: concurrentWorld.bucket.objects.size,
    concurrent_live_grants: [...concurrentWorld.db.capabilities.values()]
      .filter((r) => !r.revoked_at && !r.rotated_to).length,
  };
};

scenarios.bearer_loss_recovery = async () => {
  const world = await createWorld();
  const op = operationId("lost");
  const published = await call(world, await publishRequest({ operationId: op }));
  const originalCapability = published.json.capability;

  /* The caller lost the response. A plain retry must NOT mint a bearer. */
  const blindRetry = await call(world, await publishRequest({ operationId: op }));

  /* Explicit recovery mints a replacement and kills the unusable one. */
  const recovered = await call(world, await publishRequest({
    operationId: op, path: "/api/publish/recover", body: "",
  }));
  const liveAfterRecovery = [...world.db.capabilities.values()]
    .filter((r) => !r.revoked_at && !r.rotated_to);

  /* The original bearer must now be dead, the replacement alive. */
  const oldExchange = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: originalCapability }),
  }));
  const newExchange = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: recovered.json.capability }),
  }));

  /* A second recovery is still safe: one live grant at all times. */
  const secondRecovery = await call(world, await publishRequest({
    operationId: op, path: "/api/publish/recover", body: "",
  }));
  const liveAfterSecond = [...world.db.capabilities.values()]
    .filter((r) => !r.revoked_at && !r.rotated_to);

  /* Once delivered, recovery is refused: the driver already has a link. */
  await call(world, await publishRequest({
    operationId: op, path: "/api/publish/delivery", body: "",
    extraHeaders: { "X-Publication-Phase": "INTENT", "X-Publication-Capability": currentCapabilityId(world, op) },
  }));
  await call(world, await publishRequest({
    operationId: op, path: "/api/publish/delivery", body: "",
    extraHeaders: { "X-Publication-Phase": "DELIVERED", "X-Publication-Capability": currentCapabilityId(world, op) },
  }));
  const afterDelivered = await call(world, await publishRequest({
    operationId: op, path: "/api/publish/recover", body: "",
  }));

  const unknown = await call(world, await publishRequest({
    operationId: operationId("nope"), path: "/api/publish/recover", body: "",
  }));

  return {
    blind_retry_status: blindRetry.status,
    blind_retry_bearer: !!(blindRetry.json && blindRetry.json.capability),
    recovered_status: recovered.status,
    recovered_bearer: !!(recovered.json && recovered.json.capability),
    recovered_is_different: recovered.json.capability !== originalCapability,
    bearer_generation: recovered.json.operation.bearer_generation,
    live_after_recovery: liveAfterRecovery.length,
    old_bearer_exchange: oldExchange.status,
    new_bearer_exchange: newExchange.status,
    second_recovery_status: secondRecovery.status,
    second_generation: secondRecovery.json.operation.bearer_generation,
    live_after_second: liveAfterSecond.length,
    after_delivered_status: afterDelivered.status,
    after_delivered_reason: afterDelivered.json && afterDelivered.json.reason,
    unknown_operation_status: unknown.status,
  };
};

scenarios.operation_ids_cannot_reach_dashboard_data = async () => {
  const world = await createWorld();
  const op = operationId("access");
  await call(world, await publishRequest({ operationId: op }));

  const asCapability = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: op }),
  }));
  const asCookie = await call(world, edgeRequest(ORIGIN + "/api/snapshot", {
    method: "GET", headers: { Cookie: "__Host-eco_dash=" + op, Origin: ORIGIN },
  }));
  const asPublisherToken = await call(world, await publishRequest({
    operationId: operationId("x"), credential: op,
  }));
  return {
    as_capability: asCapability.status,
    as_session_cookie: asCookie.status,
    as_publisher_credential: asPublisherToken.status,
    operation_row_has_no_digest:
      !Object.prototype.hasOwnProperty.call([...world.db.publications.values()][0], "capability_digest"),
  };
};

/* ---------------------------- FINDING 6: publisher write transport -------- */

scenarios.publisher_transport_authentication = async () => {
  const world = await createWorld();
  const objectKey = publisher.mintObjectKey();
  await publisher.putSnapshotObject(services(world), {
    snapshot_object_key: objectKey, subject_ref: "subject-driver", body: FIXTURE_A,
  });
  const driverGrant = await devGrants.issueDevCapability(services(world), {
    subject_ref: "subject-driver", snapshot_object_key: objectKey,
    now: world.clock.now, ttl_seconds: 3600,
  });
  const session = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: driverGrant.capability }),
  }));
  const cookie = (session.headers["set-cookie"] || "").split(";")[0];

  const cases = {};
  cases.no_credential = (await call(world, await publishRequest({
    operationId: operationId("noc"), credential: null,
  }))).status;
  cases.wrong_credential = (await call(world, await publishRequest({
    operationId: operationId("wrong"), credential: capabilityLib.generateCapability(),
  }))).status;
  cases.driver_capability_as_publisher = (await call(world, await publishRequest({
    operationId: operationId("dcap"), credential: driverGrant.capability,
  }))).status;
  cases.driver_session_as_publisher = (await call(world, edgeRequest(ORIGIN + "/api/publish", {
    method: "POST",
    headers: {
      "Content-Type": "application/json", Cookie: cookie,
      "X-Publication-Operation": operationId("dses"),
      "X-Publication-Subject": "subject-alpha",
      "X-Publication-Payload-Digest": await sha256Hex(FIXTURE_A),
    },
    body: FIXTURE_A,
  }))).status;
  cases.bearer_scheme_rejected = (await call(world, edgeRequest(ORIGIN + "/api/publish", {
    method: "POST",
    headers: {
      "Content-Type": "application/json", Authorization: "Bearer " + PUBLISHER_TOKEN,
      "X-Publication-Operation": operationId("sch"),
      "X-Publication-Subject": "subject-alpha",
      "X-Publication-Payload-Digest": await sha256Hex(FIXTURE_A),
    },
    body: FIXTURE_A,
  }))).status;
  cases.malformed_credential = (await call(world, await publishRequest({
    operationId: operationId("mal"), credential: "short",
  }))).status;
  cases.valid_credential = (await call(world, await publishRequest({
    operationId: operationId("ok"),
  }))).status;

  /* An unconfigured transport must refuse everything. */
  const unconfigured = await createWorld({ publisherConfigured: false });
  cases.unconfigured_transport = (await call(unconfigured, await publishRequest({
    operationId: operationId("unc"),
  }))).status;

  /* The publisher credential must not work on a driver route. */
  cases.publisher_token_as_driver_capability = (await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: PUBLISHER_TOKEN }),
  }))).status;

  const joined = world.logs.join("\n") + JSON.stringify(cases);
  cases.leaks_credential = joined.includes(PUBLISHER_TOKEN);
  cases.methods = {
    get_publish: (await call(world, await publishRequest({
      operationId: operationId("get"), method: "GET",
    }))).status,
  };
  return cases;
};

scenarios.publisher_transport_payload_rules = async () => {
  const world = await createWorld();
  const cases = {};

  cases.malformed_json = (await call(world, await publishRequest({
    operationId: operationId("mj"), body: "{oops",
  }))).status;
  const overWide = JSON.parse(FIXTURE_A);
  overWide.display_notes = "OTHER_DRIVER_PRIVATE_VALUE";
  cases.non_canonical_payload = (await call(world, await publishRequest({
    operationId: operationId("ncp"), body: JSON.stringify(overWide),
  }))).status;
  const withPii = JSON.parse(FIXTURE_A);
  withPii.periods.weekly.current.driver_name = "Jan Kowalski";
  cases.privacy_invalid_payload = (await call(world, await publishRequest({
    operationId: operationId("pii"), body: JSON.stringify(withPii),
  }))).status;
  cases.digest_mismatch = (await call(world, await publishRequest({
    operationId: operationId("dm"), digest: "0".repeat(64),
  }))).status;
  cases.bad_operation_id = (await call(world, await publishRequest({
    operationId: "short",
  }))).status;
  cases.missing_subject = (await call(world, await publishRequest({
    operationId: operationId("nos"), subjectRef: "",
  }))).status;
  cases.wrong_content_type = (await call(world, await publishRequest({
    operationId: operationId("wct"), contentType: "text/plain",
  }))).status;

  /* No caller-chosen R2 key is accepted anywhere. */
  const before = world.bucket.objects.size;
  cases.arbitrary_key_header = (await call(world, await publishRequest({
    operationId: operationId("key"),
    extraHeaders: { "X-Object-Key": "aa/attacker.json", "X-Snapshot-Object-Key": "aa/attacker.json" },
  }))).status;
  const keys = [...world.bucket.objects.keys()];
  cases.caller_key_never_used = !keys.includes("aa/attacker.json");
  cases.keys_are_minted = keys.every((k) => publisher.OBJECT_KEY_PATTERN.test(k));
  cases.objects_written_by_probes = world.bucket.objects.size - before;

  /* The stored object always carries the binding for the declared subject. */
  const ok = await call(world, await publishRequest({ operationId: operationId("bind"), subjectRef: "subject-gamma" }));
  const storedKey = [...world.bucket.objects.keys()].pop();
  const stored = world.bucket.objects.get(storedKey);
  const expected = await capabilityLib.subjectBindingDigest(
    "subject-gamma", storedKey, world.env.CAPABILITY_PEPPER);
  cases.binding_written = stored.customMetadata.subject_binding === expected;
  cases.publish_status = ok.status;
  cases.response_has_no_object_key = !ok.text.includes(storedKey);
  cases.response_has_no_subject = !ok.text.includes("subject-gamma");
  return cases;
};

/* ------------------------------------------------- FINDING 7: E2E --------- */

scenarios.publisher_write_to_driver_read = async () => {
  const world = await createWorld({ pepper: "synthetic-e2e-pepper" });
  const op = operationId("e2e");

  /* 1. publisher writes canonical bytes through the authenticated transport */
  const published = await call(world, await publishRequest({
    operationId: op, subjectRef: "subject-e2e", body: FIXTURE_A,
  }));

  /* 2. driver bootstraps with the returned capability */
  const exchange = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers: { "CF-Connecting-IP": CLIENT_IP, "Content-Type": "application/json", Origin: ORIGIN },
    body: JSON.stringify({ capability: published.json.capability }),
  }));
  const cookie = (exchange.headers["set-cookie"] || "").split(";")[0];

  /* 3. driver reads the protected snapshot */
  const snapshot = await call(world, edgeRequest(ORIGIN + "/api/snapshot", {
    method: "GET", headers: { Cookie: cookie, Origin: ORIGIN },
  }));

  const canonical = (x) => Array.isArray(x) ? x.map(canonical)
    : (x && typeof x === "object"
        ? Object.keys(x).sort().reduce((o, k) => { o[k] = canonical(x[k]); return o; }, {})
        : x);
  const same = JSON.stringify(canonical(JSON.parse(snapshot.text))) ===
               JSON.stringify(canonical(JSON.parse(FIXTURE_A)));

  const storedKey = [...world.bucket.objects.keys()][0];
  const joined = world.logs.join("\n");
  return {
    publish_status: published.status,
    exchange_status: exchange.status,
    snapshot_status: snapshot.status,
    snapshot_matches_published: same,
    objects: world.bucket.objects.size,
    grants: world.db.capabilities.size,
    operations: world.db.publications.size,
    snapshot_cache_control: snapshot.headers["cache-control"],
    response_has_no_subject: !snapshot.text.includes("subject-e2e"),
    response_has_no_object_key: !snapshot.text.includes(storedKey),
    logs_leak_bearer: joined.includes(published.json.capability),
    logs_leak_publisher_token: joined.includes(PUBLISHER_TOKEN),
  };
};

/* --------------------------------- test-double fidelity audit ------------- */

scenarios.test_double_fidelity = async () => {
  const world = await createWorld();
  const key = publisher.mintObjectKey();
  await publisher.putSnapshotObject(services(world), {
    snapshot_object_key: key, subject_ref: "subject-alpha", body: FIXTURE_A,
  });

  /* R2: metadata round-trip and a genuine miss. */
  const object = await world.bucket.get(key);
  const missing = await world.bucket.get(publisher.mintObjectKey());

  /* Uniqueness: a duplicate digest must be refused, not silently overwrite. */
  const grant = await devGrants.issueDevCapability(services(world), {
    subject_ref: "subject-alpha", snapshot_object_key: key, now: world.clock.now, ttl_seconds: 3600,
  });
  const row = await world.store.findCapabilityById(grant.capability_id);
  let duplicateRejected = false;
  try {
    await world.db.prepare(
      `INSERT INTO eco_capability
         (capability_id, capability_digest, subject_ref, snapshot_object_key,
          issued_at, expires_at, revoked_at, rotated_to, session_epoch)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, 0)`
    ).bind("duplicate", [...world.db.capabilities.values()][0].capability_digest,
           "subject-alpha", key, world.clock.now, world.clock.now + 60).run();
  } catch (error) { duplicateRejected = true; }

  /* Defaults: a fresh grant must start un-revoked, un-rotated, epoch zero. */
  const defaults = {
    revoked_at_is_null: row.revoked_at === null,
    rotated_to_is_null: row.rotated_to === null,
    session_epoch_zero: Number(row.session_epoch) === 0,
  };

  /* Affected-row counts drive every compare-and-set. */
  const noop = await world.store.revokeCapability("no-such-capability", world.clock.now);
  const real = await world.store.revokeCapability(grant.capability_id, world.clock.now);
  const repeat = await world.store.revokeCapability(grant.capability_id, world.clock.now + 1);

  /* Batch rollback must restore every table it touched. */
  const rollbackWorld = await createWorld();
  const seed = await devGrants.issueDevCapability(services(rollbackWorld), {
    subject_ref: "subject-alpha", snapshot_object_key: publisher.mintObjectKey(),
    now: rollbackWorld.clock.now, ttl_seconds: 3600,
  });
  const before = rollbackWorld.db.capabilities.size;
  rollbackWorld.db.failWrites = true;
  let threw = false;
  try {
    await rotateGrant(services(rollbackWorld), {
      capability_id: seed.capability_id, now: rollbackWorld.clock.now, ttl_seconds: 3600,
    });
  } catch (error) { threw = true; }
  rollbackWorld.db.failWrites = false;

  return {
    r2_metadata_round_trip: object.customMetadata.subject_binding.length === 64,
    r2_missing_returns_null: missing === null,
    duplicate_digest_rejected: duplicateRejected,
    defaults: defaults,
    revoke_unknown_changes: noop.status,
    revoke_real_changes: real.status,
    revoke_repeat_changes: repeat.status,
    rollback_threw: threw,
    rollback_restored: rollbackWorld.db.capabilities.size === before,
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
    output[name] = { harness_error: String(error && error.message) };
  }
}
process.stdout.write(JSON.stringify(output, null, 1));
