/* End-to-end proof for the publication boundary.
 *
 * Reads the CANONICAL BYTES produced by the Python builder on stdin — not a
 * fixture file — so the chain under test starts at the real host contract:
 *
 *   canonical snapshot builder (Python)
 *     -> deterministic canonical bytes + payload digest
 *     -> authenticated publisher route
 *     -> one operation-owned R2 object with subject-binding metadata
 *     -> atomic operation/grant issuance
 *     -> one raw fragment bearer
 *     -> capability exchange
 *     -> session cookie
 *     -> parameterless snapshot read
 *     -> binding + strict schema validation
 *     -> frontend-safe JSON
 *
 * Then the two recovery narratives:
 *
 *   simulated lost issuance response -> explicit recovery -> old bearer denied,
 *   new bearer works, exactly one authoritative replacement;
 *   DELIVERED -> recovery denied, delivered bearer still valid.
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

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
/* base64 in, so the octets the Python serialiser produced arrive here without
 * passing through a JSON string decode/encode round trip. This is the byte
 * sequence the whole chain is supposed to preserve. */
const CANONICAL_BYTES = new Uint8Array(Buffer.from(input.canonical_base64, "base64"));
const CANONICAL = new TextDecoder("utf-8", { fatal: true }).decode(CANONICAL_BYTES);
const HOST_DIGEST = input.payload_digest;

function hex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256HexBytes(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return hex(new Uint8Array(digest));
}

function bytesEqual(a, b) {
  if (a.byteLength !== b.byteLength) return false;
  for (let i = 0; i < a.byteLength; i += 1) if (a[i] !== b[i]) return false;
  return true;
}

const ORIGIN = "https://dashboard.example.invalid";
const T0 = 1_800_000_000;
const PEPPER = "synthetic-e2e-pepper";
const TOKEN = capabilityLib.generateCapability();
const SUBJECT = "subject-e2e-synthetic";
const OPERATION = "op-e2e-synthetic-0001";

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

async function call(request) {
  const response = await worker.fetch(request, env, {});
  const text = await response.clone().text();
  const headers = {};
  for (const [name, value] of response.headers) headers[name.toLowerCase()] = value;
  let json = null;
  try { json = JSON.parse(text); } catch (error) { json = null; }
  return { status: response.status, headers, text, json };
}

const publish = () => call(edgeRequest(ORIGIN + "/api/publish", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: "Publisher " + TOKEN,
    "X-Publication-Operation": OPERATION,
    "X-Publication-Subject": SUBJECT,
    "X-Publication-Payload-Digest": HOST_DIGEST,
    "X-Publication-Period": "weekly",
  },
  body: CANONICAL_BYTES,
}));

const control = (pathname, extra) => call(edgeRequest(ORIGIN + pathname, {
  method: "POST",
  headers: Object.assign({
    "Content-Type": "application/json",
    Authorization: "Publisher " + TOKEN,
    "X-Publication-Operation": OPERATION,
    /* Only `/api/publish/recover` reads it; the delivery route ignores it. */
    "X-Publication-Period": "weekly",
  }, extra || {}),
  body: "",
}));

const exchange = (raw) => call(edgeRequest(ORIGIN + "/api/session", {
  method: "POST",
  /* Cloudflare sets this on every dispatched request; the session rate
   * limiter keys on it. Synthetic TEST-NET-3 address. */
  headers: { "CF-Connecting-IP": "203.0.113.10", "Content-Type": "application/json", Origin: ORIGIN },
  body: JSON.stringify({ capability: raw }),
}));

const readSnapshot = (cookie) => call(edgeRequest(ORIGIN + "/api/snapshot", {
  method: "GET", headers: { Cookie: cookie, Origin: ORIGIN },
}));

const out = {};

/* --- 1. the host digest really is the digest of the received bytes -------- */
out.host_digest_matches_body = HOST_DIGEST === await sha256HexBytes(CANONICAL_BYTES);

/* --- 2. publish ---------------------------------------------------------- */
const published = await publish();
out.publish = {
  status: published.status,
  result: published.json && published.json.status,
  next_action: published.json && published.json.next_action,
  state: published.json && published.json.operation.state,
  bearer_returned: !!(published.json && published.json.capability),
  response_omits_object_key: !published.text.includes([...bucket.objects.keys()][0]),
  response_omits_subject: !published.text.includes(SUBJECT),
};
const firstBearer = published.json.capability;
const firstCapabilityId = published.json.capability_id;

/* --- 3. the single owned object, with its binding ------------------------ */
const objectKey = db.publications.get(OPERATION).snapshot_object_key;
const stored = bucket.objects.get(objectKey);
out.object = {
  distinct_keys_written: new Set(bucket.putLog).size,
  r2_objects: bucket.objects.size,
  key_is_opaque: /^[0-9a-f]{2}\/[A-Za-z0-9_-]{32}\.json$/.test(objectKey),
  binding_matches: stored.customMetadata.subject_binding ===
    await capabilityLib.subjectBindingDigest(SUBJECT, objectKey, PEPPER),
  binding_version: stored.customMetadata.binding_version,
  /* THE byte-identity assertion. Not "the same document" — the same octets. */
  stored_bytes_equal_host_bytes: bytesEqual(stored.bytes, CANONICAL_BYTES),
  stored_bytes_hex_equals_host: hex(stored.bytes) === hex(CANONICAL_BYTES),
  stored_digest: await sha256HexBytes(stored.bytes),
  ledger_digest: db.publications.get(OPERATION).payload_digest,
  metadata_digest: stored.customMetadata.payload_digest,
  host_digest: HOST_DIGEST,
  /* THE RECURRENCE DETECTOR.
   *
   * `validateSnapshotText(...).body` is the strict-schema rebuild the previous
   * implementation stored. For real canonical host output it is a DIFFERENT
   * byte sequence — the allowlist emits fields in schema order, not sorted
   * order — so storing it made the ledger digest describe bytes that existed
   * nowhere. If this ever reports false, the detector has stopped detecting
   * and the test says so rather than passing quietly. */
  schema_rebuild_differs_from_host_bytes:
    snapshotLib.validateSnapshotText(CANONICAL).body !== CANONICAL,
  /* A plain parse→stringify round trip may or may not differ, depending on the
   * document; reported, never relied on. */
  restringify_differs_from_host_bytes:
    JSON.stringify(JSON.parse(CANONICAL)) !== CANONICAL,
};

/* --- 4. driver flow ------------------------------------------------------ */
const session = await exchange(firstBearer);
const cookie = (session.headers["set-cookie"] || "").split(";")[0];
const snapshot = await readSnapshot(cookie);
const parametered = await call(edgeRequest(ORIGIN + "/api/snapshot?key=" + objectKey, {
  method: "GET", headers: { Cookie: cookie, Origin: ORIGIN },
}));

const canonicalise = (value) => Array.isArray(value)
  ? value.map(canonicalise)
  : (value && typeof value === "object"
      ? Object.keys(value).sort().reduce((acc, key) => { acc[key] = canonicalise(value[key]); return acc; }, {})
      : value);

out.driver = {
  exchange_status: session.status,
  session_cookie_set: !!session.headers["set-cookie"],
  cookie_is_httponly: (session.headers["set-cookie"] || "").includes("HttpOnly"),
  cookie_is_samesite_strict: (session.headers["set-cookie"] || "").includes("SameSite=Strict"),
  snapshot_status: snapshot.status,
  snapshot_cache_control: snapshot.headers["cache-control"],
  snapshot_content_type: snapshot.headers["content-type"],
  snapshot_matches_canonical:
    JSON.stringify(canonicalise(JSON.parse(snapshot.text))) ===
    JSON.stringify(canonicalise(JSON.parse(CANONICAL))),
  snapshot_omits_subject: !snapshot.text.includes(SUBJECT),
  snapshot_omits_object_key: !snapshot.text.includes(objectKey),
  parameterised_read_refused: parametered.status,
};

/* --- 5. simulated lost issuance response -> explicit recovery ------------- */
const blindRetry = await publish();
const recovered = await control("/api/publish/recover");
const secondBearer = recovered.json.capability;

const oldBearer = await exchange(firstBearer);
const oldSession = await readSnapshot(cookie);
const newSession = await exchange(secondBearer);
const newCookie = (newSession.headers["set-cookie"] || "").split(";")[0];
const newSnapshot = await readSnapshot(newCookie);

const liveGrants = [...db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to);
out.recovery = {
  blind_retry_status: blindRetry.status,
  blind_retry_result: blindRetry.json && blindRetry.json.status,
  blind_retry_next_action: blindRetry.json && blindRetry.json.next_action,
  blind_retry_bearer: !!(blindRetry.json && blindRetry.json.capability),
  recover_status: recovered.status,
  recover_result: recovered.json.status,
  replacement_is_different: secondBearer !== firstBearer,
  superseded_capability_id: recovered.json.operation.capability_id !== firstCapabilityId,
  bearer_generation: recovered.json.operation.bearer_generation,
  live_grants: liveGrants.length,
  old_bearer_exchange: oldBearer.status,
  old_session_snapshot: oldSession.status,
  new_bearer_exchange: newSession.status,
  new_snapshot_status: newSnapshot.status,
  new_snapshot_matches_canonical:
    JSON.stringify(canonicalise(JSON.parse(newSnapshot.text))) ===
    JSON.stringify(canonicalise(JSON.parse(CANONICAL))),
  objects_still: bucket.objects.size,
};

/* --- 6. DELIVERED -> recovery denied, delivered bearer still valid -------- */
const currentCapabilityId = db.publications.get(OPERATION).capability_id;
const intent = await control("/api/publish/delivery",
  { "X-Publication-Phase": "INTENT", "X-Publication-Capability": currentCapabilityId });
const delivered = await control("/api/publish/delivery",
  { "X-Publication-Phase": "DELIVERED", "X-Publication-Capability": currentCapabilityId });
const deniedRecovery = await control("/api/publish/recover");
const deliveredExchange = await exchange(secondBearer);
const deliveredCookie = (deliveredExchange.headers["set-cookie"] || "").split(";")[0];
const deliveredSnapshot = await readSnapshot(deliveredCookie);
const afterDeliveredPublish = await publish();

out.delivery = {
  intent_result: intent.json.status,
  delivered_result: delivered.json.status,
  delivered_state: delivered.json.operation.state,
  recovery_status: deniedRecovery.status,
  recovery_reason: deniedRecovery.json && deniedRecovery.json.reason,
  recovery_bearer: !!(deniedRecovery.json && deniedRecovery.json.capability),
  delivered_bearer_exchange: deliveredExchange.status,
  delivered_bearer_snapshot: deliveredSnapshot.status,
  republish_result: afterDeliveredPublish.json && afterDeliveredPublish.json.status,
  republish_next_action: afterDeliveredPublish.json && afterDeliveredPublish.json.next_action,
  bearer_generation: delivered.json.operation.bearer_generation,
  final_live_grants:
    [...db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to).length,
  final_objects: bucket.objects.size,
  final_operations: db.publications.size,
  /* Neither recovery nor delivery may have touched the authoritative bytes. */
  stored_bytes_unchanged: bytesEqual(bucket.objects.get(objectKey).bytes, CANONICAL_BYTES),
  stored_digest_unchanged:
    (await sha256HexBytes(bucket.objects.get(objectKey).bytes)) === HOST_DIGEST,
  ledger_digest_unchanged: db.publications.get(OPERATION).payload_digest === HOST_DIGEST,
};

/* --- 7. hygiene ---------------------------------------------------------- */
const joined = logs.map((l) => JSON.stringify(l)).join("\n");
out.hygiene = {
  logs_leak_first_bearer: joined.includes(firstBearer),
  logs_leak_second_bearer: joined.includes(secondBearer),
  logs_leak_publisher_token: joined.includes(TOKEN),
  logs_leak_object_key: joined.includes(objectKey),
  logs_leak_subject: joined.includes(SUBJECT),
};

process.stdout.write(JSON.stringify(out, null, 1));
