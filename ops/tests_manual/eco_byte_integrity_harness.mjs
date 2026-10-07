/* Byte-integrity harness for the Driver Eco Dashboard publication boundary.
 *
 * THE INVARIANT UNDER TEST
 *
 *   host canonical output bytes
 *     == bytes hashed for payload_digest
 *     == bytes accepted by /api/publish
 *     == bytes written to R2
 *     == bytes hashed into the publication ledger
 *     == bytes read back before schema validation
 *
 * Every assertion below is over OCTETS. "The same document" is not the claim
 * and is never accepted as evidence: the defect this suite exists to prevent
 * produced semantically identical documents with different bytes, and the
 * ledger digest therefore identified a byte sequence that existed nowhere.
 *
 * The canonical bytes arrive on stdin, base64, produced by the real Python
 * publisher (`jobs/ecodriving_dashboard/publication.py`). Nothing here builds
 * a snapshot in JavaScript.
 *
 * Synthetic data only. No wrangler, no credentials, no remote resource, no
 * e-mail. No capability, session id, credential or object key is printed.
 */

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
const snapshotLib = await import(path.join(DELIVERY, "worker", "lib", "snapshot.js"));
const digestLib = await import(path.join(DELIVERY, "worker", "lib", "digest.js"));

/* ------------------------------------------------------------------ input -- */

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));

const b64 = (value) => new Uint8Array(Buffer.from(value, "base64"));
const CANONICAL_A = b64(input.canonical_a_base64);
const CANONICAL_B = b64(input.canonical_b_base64);
const DIGEST_A = input.digest_a;
const DIGEST_B = input.digest_b;
/* Same document as A, pretty-printed and key-reordered by Python. */
const NON_CANONICAL_A = b64(input.non_canonical_a_base64);
/* Host canonical octets whose rating distribution names only the buckets that
 * have a population, plus structural counter-examples in the same encoding. */
const SPARSE = b64(input.sparse_base64);
const SPARSE_DIGEST = input.sparse_digest;
const INVALID_DISTRIBUTIONS = input.invalid_distributions;
/* A Python-canonical value whose JS re-serialisation differs. */
const DIVERGENT = b64(input.divergent_base64);

const ORIGIN = "https://dashboard.example.invalid";
const T0 = 1_800_000_000;
const PEPPER = "synthetic-byte-integrity-pepper";
const TOKEN = capabilityLib.generateCapability();
const SUBJECT = "subject-byte-integrity";

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
  return ("op-bytes-" + seed + "-").padEnd(24, "x").slice(0, 24);
}

function publishRequest(options) {
  const settings = options || {};
  const headers = {
    "Content-Type": settings.contentType || "application/json",
    Authorization: "Publisher " + TOKEN,
    "X-Publication-Operation": settings.operationId,
    "X-Publication-Subject": settings.subjectRef === undefined ? SUBJECT : settings.subjectRef,
    "X-Publication-Payload-Digest": settings.digest,
    "X-Publication-Period": settings.periodType === undefined ? "weekly" : settings.periodType,
  };
  return edgeRequest(ORIGIN + "/api/publish", {
    method: "POST", headers, body: settings.body,
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

function sameDocument(left, right) {
  const order = (value) => Array.isArray(value)
    ? value.map(order)
    : (value && typeof value === "object"
        ? Object.keys(value).sort().reduce((acc, k) => { acc[k] = order(value[k]); return acc; }, {})
        : value);
  return JSON.stringify(order(JSON.parse(left))) === JSON.stringify(order(JSON.parse(right)));
}

/** Publish A under a fresh operation and return the world plus useful handles. */
async function publishedWorld(seed) {
  const world = await createWorld();
  const op = operationId(seed);
  const response = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const row = world.db.publications.get(op);
  return { world, op, response, row, objectKey: row ? row.snapshot_object_key : null };
}

/* -------------------------------------------------------------- scenarios -- */

const scenarios = {};

/* --- 1. the canonical happy path, asserted on octets --------------------- */

scenarios.canonical_happy_path = async () => {
  const { world, op, response, objectKey } = await publishedWorld("happy");
  const stored = world.bucket.objects.get(objectKey);
  const row = world.db.publications.get(op);

  const session = await exchange(world, response.json.capability);
  const cookie = (session.headers["set-cookie"] || "").split(";")[0];
  const snapshot = await readSnapshot(world, cookie);

  return {
    publish_status: response.status,
    publish_result: response.json.status,

    /* Step by step, every link of the chain as a hex digest. */
    host_digest: DIGEST_A,
    host_bytes_sha256: await sha(CANONICAL_A),
    ledger_digest: row.payload_digest,
    r2_bytes_sha256: await sha(stored.bytes),
    r2_metadata_digest: stored.customMetadata.payload_digest,
    r2_metadata_algorithm: stored.customMetadata.payload_digest_algorithm,

    /* And the octets themselves, not just their digests. */
    r2_bytes_equal_host_bytes: digestLib.bytesEqual(stored.bytes, CANONICAL_A),
    r2_bytes_hex_equals_host_hex: hex(stored.bytes) === hex(CANONICAL_A),
    r2_byte_length: stored.bytes.byteLength,
    host_byte_length: CANONICAL_A.byteLength,

    /* The browser response is a rebuild: same document, different octets. */
    snapshot_status: snapshot.status,
    snapshot_same_document: sameDocument(snapshot.text,
      new TextDecoder("utf-8").decode(CANONICAL_A)),
    snapshot_is_a_rebuild_not_the_stored_bytes:
      snapshot.text !== new TextDecoder("utf-8").decode(CANONICAL_A),

    objects: world.bucket.objects.size,
    puts: world.bucket.putLog.length,
  };
};

/* --- 1b. a SPARSE rating distribution is a valid snapshot ---------------- */

/*
 * The deployed validator required all three rating buckets. The host emits only
 * the buckets that have a population, so a client with nobody in `dangerous`
 * produced canonical bytes the Worker refused with 422 PAYLOAD_NOT_CANONICAL
 * before `publishSnapshot` ran — no publication, no object, no grant.
 *
 * This scenario runs the REAL host octets through the real publish route and
 * the real read path, and then re-runs the same route for every structural
 * counter-example, so "sparse is accepted" and "the boundary still fails
 * closed" are established against the same encoder in one place.
 */
scenarios.sparse_rating_distribution = async () => {
  const world = await createWorld();
  const op = operationId("sparse");
  const response = await publish(world, { operationId: op, digest: SPARSE_DIGEST, body: SPARSE });
  const row = world.db.publications.get(op);
  const stored = row ? world.bucket.objects.get(row.snapshot_object_key) : null;

  const sparseText = new TextDecoder("utf-8").decode(SPARSE);
  const validated = snapshotLib.validateSnapshotText(sparseText);
  const canonical = snapshotLib.checkCanonicalJsonForm(sparseText);

  let servedKeys = null;
  let snapshotStatus = null;
  if (response.json && response.json.capability) {
    const session = await exchange(world, response.json.capability);
    const cookie = (session.headers["set-cookie"] || "").split(";")[0];
    const snapshot = await readSnapshot(world, cookie);
    snapshotStatus = snapshot.status;
    if (snapshot.status === 200) {
      const served = JSON.parse(snapshot.text);
      servedKeys = Object.keys(
        served.periods.weekly.current.rating_group_distribution).sort();
    }
  }

  /* Every counter-example under its own fresh world, so a refusal cannot be
   * explained by state a previous case left behind. */
  const refusals = {};
  for (const [name, entry] of Object.entries(INVALID_DISTRIBUTIONS)) {
    const isolated = await createWorld();
    const bytes = b64(entry.body_base64);
    const attempt = await publish(isolated, {
      operationId: operationId("bad-" + name).slice(0, 24),
      digest: entry.digest, body: bytes,
    });
    const verdict = snapshotLib.validateSnapshotText(
      new TextDecoder("utf-8").decode(bytes));
    refusals[name] = {
      status: attempt.status,
      error: attempt.json ? attempt.json.error : null,
      schema_ok: verdict.ok,
      schema_detail: verdict.ok ? null : verdict.detail,
      schema_path: verdict.ok ? null : verdict.path,
      /* Refused BEFORE any publication state could exist. */
      operations: isolated.db.publications.size,
      objects: isolated.bucket.objects.size,
      grants: isolated.db.capabilities.size,
    };
  }

  return {
    publish_status: response.status,
    publish_result: response.json ? response.json.status : null,
    publish_error: response.json ? response.json.error || null : null,
    schema_ok: validated.ok,
    schema_reason: validated.ok ? null : validated.reason,
    schema_path: validated.ok ? null : validated.path,
    canonical_ok: canonical.ok,
    /* The stored object is the host's own octets, unchanged. */
    r2_bytes_equal_host_bytes: stored ? digestLib.bytesEqual(stored.bytes, SPARSE) : false,
    ledger_digest: row ? row.payload_digest : null,
    host_digest: SPARSE_DIGEST,
    /* No zero-filling anywhere: the browser sees the two buckets that exist. */
    snapshot_status: snapshotStatus,
    served_distribution_keys: servedKeys,
    refusals,
  };
};

/* --- 2. the recurrence detector ------------------------------------------ */

scenarios.reserialisation_detector = async () => {
  const canonicalText = new TextDecoder("utf-8").decode(CANONICAL_A);
  const rebuild = snapshotLib.validateSnapshotText(canonicalText);
  const divergentText = new TextDecoder("utf-8").decode(DIVERGENT);
  const { world, objectKey } = await publishedWorld("detect");
  const stored = world.bucket.objects.get(objectKey);

  return {
    /* What the previous implementation stored. */
    schema_rebuild_valid: rebuild.ok,
    schema_rebuild_differs: rebuild.body !== canonicalText,
    schema_rebuild_is_same_document: sameDocument(rebuild.body, canonicalText),
    /* A Python-canonical value JS cannot reproduce with JSON.stringify. */
    divergent_python_bytes: divergentText,
    divergent_js_restringify: JSON.stringify(JSON.parse(divergentText)),
    divergent_serialisers_disagree:
      JSON.stringify(JSON.parse(divergentText)) !== divergentText,
    /* And R2 still holds the Python bytes, not either rebuild. */
    r2_holds_host_bytes: digestLib.bytesEqual(stored.bytes, CANONICAL_A),
    r2_is_not_the_schema_rebuild:
      new TextDecoder("utf-8").decode(stored.bytes) !== rebuild.body,
  };
};

/* --- 3. digest(A) + body(B) is refused, in both directions --------------- */

scenarios.digest_mismatch = async () => {
  const world = await createWorld();
  const op = operationId("mism");
  const forward = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_B });
  const backward = await publish(world, { operationId: operationId("mism2"),
                                          digest: DIGEST_B, body: CANONICAL_A });
  /* A caller that supplies a well-formed but unrelated digest. */
  const fabricated = await publish(world, { operationId: operationId("mism3"),
                                            digest: "0".repeat(64), body: CANONICAL_A });

  const grants = [...world.db.capabilities.values()];
  return {
    forward_status: forward.status,
    forward_error: forward.json && forward.json.error,
    backward_status: backward.status,
    backward_error: backward.json && backward.json.error,
    fabricated_status: fabricated.status,
    fabricated_error: fabricated.json && fabricated.json.error,
    operations: world.db.publications.size,
    objects: world.bucket.objects.size,
    puts: world.bucket.putLog.length,
    grants: grants.length,
    bearers_returned: [forward, backward, fabricated]
      .filter((r) => r.json && r.json.capability).length,
    logs_leak_digest: world.logs.map((l) => JSON.stringify(l)).join("\n").includes(DIGEST_A),
  };
};

/* --- 4. same operation, different canonical bytes ------------------------ */

scenarios.operation_conflict_on_different_bytes = async () => {
  const { world, op, response, objectKey } = await publishedWorld("conf");
  const before = {
    ledger_digest: world.db.publications.get(op).payload_digest,
    bytes: hex(world.bucket.objects.get(objectKey).bytes),
    capability_id: world.db.publications.get(op).capability_id,
  };
  /* Same operation id, same subject, genuinely different canonical bytes. */
  const conflicting = await publish(world, { operationId: op, digest: DIGEST_B, body: CANONICAL_B });
  const after = {
    ledger_digest: world.db.publications.get(op).payload_digest,
    bytes: hex(world.bucket.objects.get(objectKey).bytes),
    capability_id: world.db.publications.get(op).capability_id,
  };
  /* An identical retry, on the other hand, is idempotent. */
  const identicalRetry = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });

  return {
    first_status: response.status,
    conflict_status: conflicting.status,
    conflict_result: conflicting.json && conflicting.json.status,
    conflict_next_action: conflicting.json && conflicting.json.next_action,
    conflict_bearer: !!(conflicting.json && conflicting.json.capability),
    ledger_digest_unchanged: before.ledger_digest === after.ledger_digest,
    stored_bytes_unchanged: before.bytes === after.bytes,
    grant_unchanged: before.capability_id === after.capability_id,
    objects: world.bucket.objects.size,
    puts_after_conflict: world.bucket.putLog.length,
    identical_retry_status: identicalRetry.status,
    identical_retry_result: identicalRetry.json && identicalRetry.json.status,
    identical_retry_bearer: !!(identicalRetry.json && identicalRetry.json.capability),
    live_grants: [...world.db.capabilities.values()]
      .filter((r) => !r.revoked_at && !r.rotated_to).length,
  };
};

/* --- 5. R2 corruption is detected on the driver read --------------------- */

scenarios.r2_corruption_detected_on_read = async () => {
  const { world, response, objectKey } = await publishedWorld("corr");
  const session = await exchange(world, response.json.capability);
  const cookie = (session.headers["set-cookie"] || "").split(";")[0];
  const healthy = await readSnapshot(world, cookie);

  const stored = world.bucket.objects.get(objectKey);
  const original = stored.bytes.slice();

  /* (a) body mutated, metadata and ledger untouched. */
  const mutated = original.slice();
  mutated[mutated.length - 2] = mutated[mutated.length - 2] === 0x31 ? 0x32 : 0x31;
  stored.bytes = mutated;
  const afterBodyMutation = await readSnapshot(world, cookie);

  /* (b) body AND metadata rewritten together — only the ledger disagrees.
   * This is what makes the check more than a self-consistent checksum. */
  stored.customMetadata = Object.assign({}, stored.customMetadata, {
    payload_digest: await sha(mutated),
  });
  const afterCoordinatedRewrite = await readSnapshot(world, cookie);

  /* (c) metadata digest removed entirely. */
  const withoutDigest = Object.assign({}, stored.customMetadata);
  delete withoutDigest.payload_digest;
  stored.bytes = original.slice();
  stored.customMetadata = withoutDigest;
  const afterDigestRemoved = await readSnapshot(world, cookie);

  /* (d) restored exactly -> serving resumes. */
  stored.bytes = original.slice();
  stored.customMetadata = Object.assign({}, withoutDigest, { payload_digest: DIGEST_A });
  const afterRestore = await readSnapshot(world, cookie);

  return {
    healthy_status: healthy.status,
    body_mutation_status: afterBodyMutation.status,
    body_mutation_body: afterBodyMutation.text,
    coordinated_rewrite_status: afterCoordinatedRewrite.status,
    digest_removed_status: afterDigestRemoved.status,
    restored_status: afterRestore.status,
    restored_same_document: afterRestore.status === 200 && sameDocument(
      afterRestore.text, new TextDecoder("utf-8").decode(CANONICAL_A)),
    reasons: world.logs
      .map((line) => (typeof line === "string" ? line : JSON.stringify(line)))
      .filter((line) => line.includes("PAYLOAD_DIGEST"))
      .length,
  };
};

/* --- 6. a retry never silently repairs a mismatched object --------------- */

scenarios.retry_over_corrupted_object_fails_closed = async () => {
  const world = await createWorld();
  const op = operationId("repa");

  /* Crash the operation after the object is written but before the ledger
   * records SNAPSHOT_WRITTEN, so the row stays CREATED and the retry has to
   * re-examine the object. */
  world.db.failAt.set(bindings.SITE.SNAPSHOT_WRITTEN, "D1_UNAVAILABLE");
  const crashed = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  world.db.failAt.delete(bindings.SITE.SNAPSHOT_WRITTEN);

  const row = world.db.publications.get(op);
  const objectKey = row.snapshot_object_key;
  const stored = world.bucket.objects.get(objectKey);
  const original = stored.bytes.slice();

  /* Substitute a different, perfectly valid canonical snapshot at the owned
   * key. Its bytes do not hash to the operation's digest. */
  stored.bytes = CANONICAL_B.slice();
  stored.customMetadata = Object.assign({}, stored.customMetadata, {
    payload_digest: DIGEST_B,
  });
  const putsBefore = world.bucket.putLog.length;
  const retry = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });
  const afterRetry = world.bucket.objects.get(objectKey);
  /* Captured HERE, before the repair below touches anything. */
  const bytesAfterRetry = hex(afterRetry.bytes);
  const putsAfterRetry = world.bucket.putLog.length;
  const stateAfterRetry = world.db.publications.get(op).state;
  const grantsAfterRetry = world.db.capabilities.size;

  /* Put the correct object back and retry again: the operation must resume. */
  afterRetry.bytes = original.slice();
  afterRetry.customMetadata = Object.assign({}, afterRetry.customMetadata, {
    payload_digest: DIGEST_A,
  });
  const resumed = await publish(world, { operationId: op, digest: DIGEST_A, body: CANONICAL_A });

  return {
    crash_status: crashed.status,
    state_after_crash: row.state,
    retry_status: retry.status,
    retry_error: retry.json && retry.json.error,
    retry_result: retry.json && retry.json.status,
    retry_next_action: retry.json && retry.json.next_action,
    retry_bearer: !!(retry.json && retry.json.capability),
    retry_overwrote_object: putsAfterRetry !== putsBefore,
    bytes_after_retry_still_substituted: bytesAfterRetry === hex(CANONICAL_B),
    state_after_retry: stateAfterRetry,
    grants_after_retry: grantsAfterRetry,
    resumed_status: resumed.status,
    resumed_result: resumed.json && resumed.json.status,
    resumed_bearer: !!(resumed.json && resumed.json.capability),
    resumed_bytes_are_host_bytes:
      hex(world.bucket.objects.get(objectKey).bytes) === hex(CANONICAL_A),
    resumed_ledger_digest: world.db.publications.get(op).payload_digest,
  };
};

/* --- 7. ingress that is not in host canonical form ----------------------- */

scenarios.non_canonical_ingress_is_refused = async () => {
  const world = await createWorld();
  const prettyDigest = await sha(NON_CANONICAL_A);
  const pretty = await publish(world, {
    operationId: operationId("pret"), digest: prettyDigest, body: NON_CANONICAL_A,
  });

  /* Invalid UTF-8: 0xFF can never appear in a UTF-8 sequence. A lossy decode
   * would turn it into U+FFFD and parse "successfully" around it. */
  const invalid = new Uint8Array(CANONICAL_A.byteLength + 1);
  invalid.set(CANONICAL_A, 0);
  invalid[CANONICAL_A.byteLength] = 0xff;
  const invalidUtf8 = await publish(world, {
    operationId: operationId("utf8"), digest: await sha(invalid), body: invalid,
  });

  return {
    pretty_status: pretty.status,
    pretty_error: pretty.json && pretty.json.error,
    pretty_is_same_document: sameDocument(
      new TextDecoder("utf-8").decode(NON_CANONICAL_A),
      new TextDecoder("utf-8").decode(CANONICAL_A)),
    invalid_utf8_status: invalidUtf8.status,
    invalid_utf8_error: invalidUtf8.json && invalidUtf8.json.error,
    operations: world.db.publications.size,
    objects: world.bucket.objects.size,
    grants: world.db.capabilities.size,
    /* The scanner's own verdicts, so the reason is asserted and not guessed. */
    canonical_verdict_host: snapshotLib.checkCanonicalJsonForm(
      new TextDecoder("utf-8").decode(CANONICAL_A)),
    canonical_verdict_pretty: snapshotLib.checkCanonicalJsonForm(
      new TextDecoder("utf-8").decode(NON_CANONICAL_A)),
  };
};

/* --- 8. the body reader hands back exact octets -------------------------- */

scenarios.body_reader_returns_exact_octets = async () => {
  const bodyLib = await import(path.join(DELIVERY, "worker", "lib", "body.js"));
  const results = {};

  for (const [name, source] of [["stream", "stream"], ["buffered", "buffered"]]) {
    const request = source === "stream"
      ? edgeRequest(ORIGIN + "/x", { method: "POST", body: CANONICAL_A })
      : { headers: new Headers(), arrayBuffer: async () => CANONICAL_A.slice().buffer };
    const read = await bodyLib.readBoundedBody(request, 512 * 1024);
    results[name] = {
      ok: read.ok,
      mode: read.mode,
      bytes_present: read.bytes instanceof Uint8Array,
      bytes_equal_source: read.ok && digestLib.bytesEqual(read.bytes, CANONICAL_A),
      digest_equals_host: read.ok ? await sha(read.bytes) : null,
    };
  }

  /* Invalid UTF-8 survives as octets and is refused by the strict decoder,
   * where a lossy decode would silently rewrite it. */
  const invalid = new Uint8Array([0x7b, 0x22, 0x61, 0x22, 0x3a, 0x22, 0xff, 0x22, 0x7d]);
  const invalidRead = await bodyLib.readBoundedBody(
    edgeRequest(ORIGIN + "/x", { method: "POST", body: invalid }), 1024);
  results.invalid_utf8 = {
    ok: invalidRead.ok,
    bytes_preserved: invalidRead.ok && digestLib.bytesEqual(invalidRead.bytes, invalid),
    lossy_text_differs: invalidRead.ok &&
      !digestLib.bytesEqual(new TextEncoder().encode(invalidRead.text), invalid),
    strict_decode_refuses: bodyLib.decodeUtf8Strict(invalid) === null,
  };
  return results;
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
                     stack: String(error && error.stack).slice(0, 800) };
  }
}
process.stdout.write(JSON.stringify(output, null, 1));
