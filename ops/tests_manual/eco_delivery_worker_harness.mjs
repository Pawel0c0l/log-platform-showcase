/* Security harness for the Driver Eco Dashboard V1 delivery Worker.
 *
 * Executes the REAL Worker (delivery/driver_eco_dashboard/worker/index.js)
 * against in-memory D1/R2/ASSETS bindings, with no wrangler, no credentials and
 * no remote Cloudflare resource. Prints one JSON object describing every
 * scenario so ops/tests_manual/test_driver_eco_dashboard_delivery.py can assert
 * on it.
 *
 * Capability and session values are synthetic and are NEVER printed: scenarios
 * report booleans and lengths, or the digest-free string "present".
 */

import path from "node:path";
import { fileURLToPath } from "node:url";
import { edgeRequest } from "./eco_edge_framing.mjs";
import { readFile, readdir } from "node:fs/promises";
import { canonicalFixture, canonicalText } from "./eco_canonical_fixture.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const DELIVERY = path.join(REPO, "delivery", "driver_eco_dashboard");
const ASSET_ROOT = path.join(REPO, "assets", "driver_eco_dashboard");

/* WebCrypto global, needed by Node before the Worker module is evaluated. */
await import(path.join(DELIVERY, "local", "node_runtime.js"));

const worker = (await import(path.join(DELIVERY, "worker", "index.js"))).default;
const bindings = await import(path.join(DELIVERY, "local", "memory_bindings.js"));
const publisher = await import(path.join(DELIVERY, "worker", "lib", "publisher.js"));
/* LOCAL-ONLY grant fabrication: the Worker has no unconditional grant insert.
 * See delivery/driver_eco_dashboard/local/dev_grants.js. */
const devGrants = await import(path.join(DELIVERY, "local", "dev_grants.js"));
const { D1AuthorizationStore } = await import(path.join(DELIVERY, "worker", "lib", "store.js"));
const capabilityLib = await import(path.join(DELIVERY, "worker", "lib", "capability.js"));
const snapshotLib = await import(path.join(DELIVERY, "worker", "lib", "snapshot.js"));
const logLib = await import(path.join(DELIVERY, "worker", "lib", "log.js"));

const ORIGIN = "https://dashboard.example.invalid";
/* Synthetic TEST-NET-3 address; never a real peer. */
const CLIENT_IP = "203.0.113.10";
const T0 = 1_800_000_000;

const FIXTURE_A = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_acceptable.json"));
const FIXTURE_B = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_safe.json"));

/* The Worker rebuilds the payload from the strict allowlist, so comparisons are
 * semantic (key order and whitespace are not part of the contract). */
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.keys(value).sort().reduce((out, key) => {
      out[key] = canonical(value[key]);
      return out;
    }, {});
  }
  return value;
}

function sameDocument(text, fixtureText) {
  try {
    return JSON.stringify(canonical(JSON.parse(text))) === JSON.stringify(canonical(JSON.parse(fixtureText)));
  } catch (error) {
    return false;
  }
}

/* ------------------------------------------------------------------ world -- */

function createWorld(options) {
  const settings = options || {};
  const db = new bindings.MemoryD1();
  const bucket = new bindings.MemoryR2();
  const logs = [];
  const clock = { now: settings.now || T0 };
  const env = bindings.createEnv({
    db,
    bucket,
    assets: bindings.createAssetsBinding(ASSET_ROOT),
    assetRoot: ASSET_ROOT,
    pepper: settings.pepper,
    allowInsecureCookies: settings.allowInsecureCookies === true,
    now: () => clock.now,
    logSink: {
      log: (line) => logs.push(line),
      error: (line) => logs.push(line),
    },
  });
  return { db, bucket, env, logs, clock, store: new D1AuthorizationStore(db) };
}

function servicesFor(world) {
  return { db: world.db, store: world.store, bucket: world.bucket, pepper: world.env.CAPABILITY_PEPPER };
}

async function seedGrant(world, { body, ttl, objectKey, subjectRef, skipBinding }) {
  const key = objectKey || publisher.mintObjectKey();
  const subject = subjectRef || "subject-" + key.slice(3, 11);
  const payload = body === undefined ? FIXTURE_A : body;
  if (skipBinding) {
    /* Legacy/unbound object: written without the subject binding metadata. */
    await world.bucket.put(key, payload);
  } else {
    await publisher.putSnapshotObject(servicesFor(world), {
      snapshot_object_key: key, subject_ref: subject, body: payload,
    });
  }
  const grant = await devGrants.issueDevCapability(
    servicesFor(world),
    {
      subject_ref: subject,
      snapshot_object_key: key,
      now: world.clock.now,
      ttl_seconds: ttl === undefined ? 3600 : ttl,
    }
  );
  return { ...grant, objectKey: key, subjectRef: subject };
}

/* ---------------------------------------------------------------- requests -- */

function request(pathname, options) {
  const settings = options || {};
  const headers = new Headers(settings.headers || {});
  if (settings.cookie) headers.set("Cookie", settings.cookie);
  if (settings.origin !== null) headers.set("Origin", settings.origin || ORIGIN);
  /* The Cloudflare edge sets this on every dispatched request; the session rate
   * limiter keys on it. `clientIp: null` omits it, which is how the
   * no-trusted-address production failure is exercised. */
  if (settings.clientIp !== null) headers.set("CF-Connecting-IP", settings.clientIp || CLIENT_IP);
  return edgeRequest((settings.origin_url || ORIGIN) + pathname, {
    method: settings.method || "GET",
    headers,
    body: settings.body,
  });
}

function jsonPost(pathname, payload, options) {
  const settings = options || {};
  return request(pathname, {
    ...settings,
    method: "POST",
    headers: { "Content-Type": "application/json", ...(settings.headers || {}) },
    body: typeof payload === "string" ? payload : JSON.stringify(payload),
  });
}

async function call(world, req) {
  const response = await worker.fetch(req, world.env, {});
  const text = await response.clone().text();
  const headers = {};
  for (const [name, value] of response.headers) headers[name.toLowerCase()] = value;
  return { status: response.status, headers, text };
}

function cookieFrom(result) {
  const raw = result.headers["set-cookie"];
  if (!raw) return null;
  return raw.split(";")[0];
}

function cookieAttributes(result) {
  const raw = result.headers["set-cookie"] || "";
  const parts = raw.split(";").map((p) => p.trim());
  return {
    name: parts[0] ? parts[0].split("=")[0] : null,
    httponly: parts.some((p) => p.toLowerCase() === "httponly"),
    secure: parts.some((p) => p.toLowerCase() === "secure"),
    samesite: (parts.find((p) => p.toLowerCase().startsWith("samesite=")) || "").split("=")[1] || null,
    path: (parts.find((p) => p.toLowerCase().startsWith("path=")) || "").split("=")[1] || null,
    max_age: Number((parts.find((p) => p.toLowerCase().startsWith("max-age=")) || "=0").split("=")[1]),
  };
}

/** Establish a session and return the Cookie header value. */
async function establish(world, capability) {
  const result = await call(world, jsonPost("/api/session", { capability }));
  return { result, cookie: cookieFrom(result) };
}

function leaks(haystack, secret) {
  return typeof haystack === "string" && haystack.includes(secret);
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

scenarios.capability_generation = async () => {
  const values = new Set();
  for (let i = 0; i < 200; i += 1) values.add(capabilityLib.generateCapability());
  const sample = capabilityLib.generateCapability();
  const digest = await capabilityLib.capabilityDigest(sample);
  const peppered = await capabilityLib.capabilityDigest(sample, "pepper-value");
  return {
    distinct: values.size,
    length: sample.length,
    charset_ok: /^[A-Za-z0-9_-]+$/.test(sample),
    entropy_bits: capabilityLib.CAPABILITY_BYTES * 8,
    wellformed_accepts_valid: capabilityLib.isWellFormedCapability(sample),
    wellformed_rejects: [
      capabilityLib.isWellFormedCapability(""),
      capabilityLib.isWellFormedCapability("short"),
      capabilityLib.isWellFormedCapability(sample + "x"),
      capabilityLib.isWellFormedCapability(sample.slice(0, 42) + "+"),
      capabilityLib.isWellFormedCapability(null),
      capabilityLib.isWellFormedCapability(12345),
    ],
    digest_length: digest.length,
    digest_is_not_capability: digest !== sample,
    pepper_changes_digest: digest !== peppered,
    digest_deterministic: digest === (await capabilityLib.capabilityDigest(sample)),
  };
};

scenarios.valid_capability_establishes_session = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { result } = await establish(world, grant.capability);
  const attributes = cookieAttributes(result);
  const cookieValue = (result.headers["set-cookie"] || "").split(";")[0].split("=").slice(1).join("=");
  return {
    status: result.status,
    body_empty: result.text === "",
    cookie: attributes,
    cookie_value_is_not_capability: cookieValue !== grant.capability,
    cookie_value_length: cookieValue.length,
    response_leaks_capability: leaks(result.text, grant.capability) ||
      leaks(JSON.stringify(result.headers), grant.capability),
    cache_control: result.headers["cache-control"],
  };
};

scenarios.invalid_capabilities_denied = async () => {
  const world = createWorld();
  await seedGrant(world, {});
  const unknown = capabilityLib.generateCapability();
  const cases = {};
  cases.random_valid_shape = (await call(world, jsonPost("/api/session", { capability: unknown }))).status;
  cases.empty = (await call(world, jsonPost("/api/session", { capability: "" }))).status;
  cases.too_short = (await call(world, jsonPost("/api/session", { capability: "abc" }))).status;
  cases.too_long = (await call(world, jsonPost("/api/session", { capability: unknown + "x" }))).status;
  cases.bad_charset = (await call(world, jsonPost("/api/session", { capability: unknown.slice(0, 42) + "%" }))).status;
  cases.wrong_type_number = (await call(world, jsonPost("/api/session", { capability: 12345 }))).status;
  cases.wrong_type_array = (await call(world, jsonPost("/api/session", { capability: [unknown] }))).status;
  cases.wrong_type_object = (await call(world, jsonPost("/api/session", { capability: { value: unknown } }))).status;
  cases.missing_field = (await call(world, jsonPost("/api/session", {}))).status;
  cases.body_is_array = (await call(world, jsonPost("/api/session", [unknown]))).status;
  cases.not_json = (await call(world, jsonPost("/api/session", "{oops"))).status;
  cases.sql_injection_shape = (await call(world, jsonPost("/api/session", { capability: "' OR 1=1 --" }))).status;
  cases.path_traversal_shape = (await call(world, jsonPost("/api/session", { capability: "../../etc/passwd" }))).status;
  const noCookie = Object.values(cases).every((status) => status !== 204);
  return { cases, none_established: noCookie };
};

scenarios.expired_capability_denied = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, { ttl: 60 });
  world.clock.now = T0 + 61;
  const { result } = await establish(world, grant.capability);
  return { status: result.status, set_cookie: !!result.headers["set-cookie"], body: result.text };
};

scenarios.revoked_capability_denied = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const before = await establish(world, grant.capability);
  await publisher.revokeCapability(
    servicesFor(world), { capability_id: grant.capability_id, now: world.clock.now }
  );
  const after = await establish(world, grant.capability);
  const unknown = await call(world, jsonPost("/api/session", { capability: capabilityLib.generateCapability() }));
  return {
    before_status: before.result.status,
    after_status: after.result.status,
    set_cookie_after: !!after.result.headers["set-cookie"],
    /* Revoked and unknown must be indistinguishable to a caller. */
    indistinguishable_from_unknown:
      after.result.status === unknown.status && after.result.text === unknown.text,
  };
};

scenarios.rotation_invalidates_predecessor = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const rotated = await rotateGrant(
    servicesFor(world), { capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600 }
  );
  const oldAttempt = await establish(world, grant.capability);
  const newAttempt = await establish(world, rotated.capability);
  const snapshot = await call(world, request("/api/snapshot", { cookie: newAttempt.cookie }));
  return {
    old_status: oldAttempt.result.status,
    new_status: newAttempt.result.status,
    new_session_serves_snapshot: snapshot.status,
    different_capability: grant.capability !== rotated.capability,
    same_subject_and_object: true,
  };
};

scenarios.rotation_kills_predecessor_sessions = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const before = await call(world, request("/api/snapshot", { cookie }));
  await rotateGrant(
    servicesFor(world), { capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600 }
  );
  const after = await call(world, request("/api/snapshot", { cookie }));
  return { before: before.status, after: after.status };
};

scenarios.revocation_kills_derived_session = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const before = await call(world, request("/api/snapshot", { cookie }));
  await publisher.revokeCapability(
    servicesFor(world), { capability_id: grant.capability_id, now: world.clock.now }
  );
  const after = await call(world, request("/api/snapshot", { cookie }));
  const bucketReadsAfter = world.bucket.getLog.length;
  return {
    before: before.status,
    after: after.status,
    r2_not_touched_after_revocation: bucketReadsAfter === 1,
  };
};

scenarios.session_epoch_revokes_sessions_only = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const before = await call(world, request("/api/snapshot", { cookie }));
  await publisher.revokeDerivedSessions(
    servicesFor(world), { capability_id: grant.capability_id }
  );
  const after = await call(world, request("/api/snapshot", { cookie }));
  const relink = await establish(world, grant.capability);
  const afterRelink = await call(world, request("/api/snapshot", { cookie: relink.cookie }));
  return {
    before: before.status, after: after.status,
    link_still_works: relink.result.status, after_relink: afterRelink.status,
  };
};

scenarios.expired_session_denied = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, { ttl: 3600 });
  const { cookie, result } = await establish(world, grant.capability);
  const ttl = cookieAttributes(result).max_age;
  const before = await call(world, request("/api/snapshot", { cookie }));
  world.clock.now = T0 + ttl + 1;
  const after = await call(world, request("/api/snapshot", { cookie }));
  return { session_ttl_seconds: ttl, before: before.status, after: after.status };
};

scenarios.session_bound_to_one_snapshot = async () => {
  const world = createWorld();
  const a = await seedGrant(world, { body: FIXTURE_A });
  const b = await seedGrant(world, { body: FIXTURE_B });
  const sessionA = await establish(world, a.capability);
  const sessionB = await establish(world, b.capability);
  const fromA = await call(world, request("/api/snapshot", { cookie: sessionA.cookie }));
  const fromB = await call(world, request("/api/snapshot", { cookie: sessionB.cookie }));
  return {
    a_matches_a: sameDocument(fromA.text, FIXTURE_A),
    b_matches_b: sameDocument(fromB.text, FIXTURE_B),
    a_is_not_b: fromA.text !== fromB.text,
    /* Object keys are never printed, only compared. */
    only_bound_keys_read:
      world.bucket.getLog.slice().sort().join(",") === [a.objectKey, b.objectKey].sort().join(","),
    reads: world.bucket.getLog.length,
  };
};

scenarios.no_parameter_selects_a_snapshot = async () => {
  const world = createWorld();
  const a = await seedGrant(world, { body: FIXTURE_A });
  const b = await seedGrant(world, { body: FIXTURE_B });
  const sessionA = await establish(world, a.capability);
  const readsBefore = world.bucket.getLog.length;
  const probes = {};
  const attempts = [
    "/api/snapshot?key=" + encodeURIComponent(b.objectKey),
    "/api/snapshot?object=" + encodeURIComponent(b.objectKey),
    "/api/snapshot?driver=other",
    "/api/snapshot?subject_ref=subject-x",
    "/api/snapshot?capability=" + b.capability,
    "/api/snapshot?snapshot_object_key=" + encodeURIComponent(b.objectKey),
    "/api/snapshot?a=1&a=2",
  ];
  for (const target of attempts) {
    const result = await call(world, request(target, { cookie: sessionA.cookie }));
    probes[target.split("?")[1].split("=")[0]] = result.status;
  }
  /* Header-based smuggling attempts. */
  const headerProbe = await call(world, request("/api/snapshot", {
    cookie: sessionA.cookie,
    headers: { "X-Object-Key": b.objectKey, "X-Subject-Ref": "subject-x", "X-Driver": "other" },
  }));
  const readsAfter = world.bucket.getLog;
  return {
    query_probes: probes,
    header_probe_status: headerProbe.status,
    header_probe_served_bound_object: sameDocument(headerProbe.text, FIXTURE_A),
    other_object_never_read: !readsAfter.slice(readsBefore).includes(b.objectKey),
    all_reads_were_bound: readsAfter.every((key) => key === a.objectKey),
  };
};

scenarios.snapshot_requires_session = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const anonymous = await call(world, request("/api/snapshot"));
  const r2ReadsAfterAnonymous = world.bucket.getLog.length;
  const withCapabilityAsCookie = await call(world, request("/api/snapshot", {
    cookie: "__Host-eco_dash=" + grant.capability,
  }));
  const withCapabilityAsBearer = await call(world, request("/api/snapshot", {
    headers: { Authorization: "Bearer " + grant.capability },
  }));
  return {
    anonymous_status: anonymous.status,
    r2_untouched: r2ReadsAfterAnonymous === 0,
    raw_capability_as_cookie_rejected: withCapabilityAsCookie.status,
    bearer_header_rejected: withCapabilityAsBearer.status,
  };
};

scenarios.session_cookie_tampering = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const value = cookie.split("=").slice(1).join("=");
  const name = cookie.split("=")[0];
  const flipped = value.slice(0, -1) + (value.slice(-1) === "A" ? "B" : "A");
  const cases = {};
  cases.flipped_last_char = (await call(world, request("/api/snapshot", { cookie: `${name}=${flipped}` }))).status;
  cases.truncated = (await call(world, request("/api/snapshot", { cookie: `${name}=${value.slice(0, 20)}` }))).status;
  cases.empty = (await call(world, request("/api/snapshot", { cookie: `${name}=` }))).status;
  cases.wrong_name = (await call(world, request("/api/snapshot", { cookie: `other_cookie=${value}` }))).status;
  cases.injected_second = (await call(world, request("/api/snapshot", {
    cookie: `${name}=${flipped}; ${name}=${value}`,
  }))).status;
  cases.original_still_valid = (await call(world, request("/api/snapshot", { cookie }))).status;
  return cases;
};

scenarios.missing_snapshot_object = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  await world.bucket.delete(grant.objectKey);
  const { cookie } = await establish(world, grant.capability);
  const result = await call(world, request("/api/snapshot", { cookie }));
  return { status: result.status, body: result.text };
};

scenarios.corrupt_snapshot_fails_closed = async () => {
  const cases = {};
  const bodies = {
    truncated_json: FIXTURE_A.slice(0, 400),
    empty: "",
    not_json: "<html>nope</html>",
    wrong_contract: JSON.stringify({ ...JSON.parse(FIXTURE_A), contract_id: "other_contract" }),
    unsupported_version: JSON.stringify({ ...JSON.parse(FIXTURE_A), schema_version: 2 }),
    no_periods: JSON.stringify({ ...JSON.parse(FIXTURE_A), periods: {} }),
    array_body: JSON.stringify([1, 2, 3]),
  };
  const forbidden = JSON.parse(FIXTURE_A);
  forbidden.periods.weekly.current.driver_name = "Jan Kowalski";
  bodies.forbidden_field = JSON.stringify(forbidden);

  for (const [name, body] of Object.entries(bodies)) {
    const world = createWorld();
    const grant = await seedGrant(world, { body });
    const { cookie } = await establish(world, grant.capability);
    const result = await call(world, request("/api/snapshot", { cookie }));
    cases[name] = {
      status: result.status,
      forwarded_body: result.text.length > 80,
      leaks_marker: result.text.includes("Kowalski") || result.text.includes("nope"),
    };
  }
  return cases;
};

scenarios.no_object_enumeration = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const targets = [
    "/api/objects", "/api/list", "/api/snapshots", "/api/r2", "/api/",
    "/fixtures/ranked_acceptable.json", "/fixtures/", "/preview.html",
    "/README.md", "/index.html/../fixtures/ranked_acceptable.json",
    "/js/../fixtures/ranked_acceptable.json", "/.well-known/x", "/wrangler.toml",
    "/api/snapshot/" + grant.objectKey, "/" + grant.objectKey,
  ];
  const statuses = {};
  for (const target of targets) {
    const result = await call(world, request(target, { cookie }));
    /* The probe list contains a real object key; report it under a stable
     * label rather than echoing the key. */
    const label = target.includes(grant.objectKey)
      ? target.replace(grant.objectKey, "<object-key>") : target;
    statuses[label] = { status: result.status, served_snapshot: result.text.includes("driver_eco_dashboard_snapshot") };
  }
  /* The bucket CAN be enumerated — the hard-retention sweep must find objects
   * past the 13-month ceiling — so the invariant is no longer "no list()
   * exists" but "no list() is reachable from here". Nothing a driver session or
   * an anonymous caller can send may reach it, and the double records every
   * call, so this is measured rather than asserted. */
  return {
    statuses,
    bucket_list_calls_from_request_paths: world.bucket.listLog.length,
    bucket_list_is_publisher_only: world.bucket.listLog.length === 0,
  };
};

scenarios.static_assets = async () => {
  const world = createWorld();
  const results = {};
  for (const target of ["/", "/index.html", "/css/dashboard.css", "/js/app.js", "/js/boot.js"]) {
    const result = await call(world, request(target));
    results[target] = {
      status: result.status,
      cache_control: result.headers["cache-control"],
      csp: !!result.headers["content-security-policy"],
      content_type: result.headers["content-type"],
    };
  }
  results["/index.html"].no_inline_script = !/<script>[^<]/.test(results["/index.html"].body || "");
  const index = await call(world, request("/index.html"));
  results.index_body_has_inline_script = /<script(?![^>]*\ssrc=)[^>]*>[\s\S]*?\S[\s\S]*?<\/script>/.test(index.text);
  return results;
};

scenarios.security_headers = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const snapshot = await call(world, request("/api/snapshot", { cookie }));
  const asset = await call(world, request("/css/dashboard.css"));
  const denied = await call(world, request("/api/snapshot"));
  const collect = (r) => ({
    csp: r.headers["content-security-policy"],
    xcto: r.headers["x-content-type-options"],
    xfo: r.headers["x-frame-options"],
    referrer: r.headers["referrer-policy"],
    robots: r.headers["x-robots-tag"],
    permissions: !!r.headers["permissions-policy"],
    coop: r.headers["cross-origin-opener-policy"],
    corp: r.headers["cross-origin-resource-policy"],
    hsts: r.headers["strict-transport-security"],
    cache: r.headers["cache-control"],
    vary: r.headers["vary"],
    cors: Object.keys(r.headers).filter((h) => h.startsWith("access-control-")),
  });
  return { snapshot: collect(snapshot), asset: collect(asset), denied: collect(denied) };
};

scenarios.cross_origin_rejected = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const cases = {};
  cases.foreign_origin = (await call(world, jsonPost("/api/session", { capability: grant.capability },
    { origin: "https://evil.example.invalid" }))).status;
  cases.null_origin = (await call(world, jsonPost("/api/session", { capability: grant.capability },
    { origin: "null" }))).status;
  cases.missing_origin = (await call(world, jsonPost("/api/session", { capability: grant.capability },
    { origin: null }))).status;
  cases.same_origin = (await call(world, jsonPost("/api/session", { capability: grant.capability }))).status;
  const preflight = await call(world, request("/api/snapshot", {
    method: "OPTIONS", origin: "https://evil.example.invalid",
  }));
  cases.options_status = preflight.status;
  cases.options_cors_headers = Object.keys(preflight.headers).filter((h) => h.startsWith("access-control-"));
  cases.wrong_content_type = (await call(world, request("/api/session", {
    method: "POST", headers: { "Content-Type": "text/plain" },
    body: JSON.stringify({ capability: grant.capability }),
  }))).status;
  cases.oversized_body = (await call(world, jsonPost("/api/session",
    { capability: grant.capability, padding: "x".repeat(2000) }))).status;
  cases.get_on_session = (await call(world, request("/api/session"))).status;
  return cases;
};

scenarios.insecure_origin_refused = async () => {
  const world = createWorld({ allowInsecureCookies: false });
  const grant = await seedGrant(world, {});
  const insecure = await call(world, jsonPost("/api/session", { capability: grant.capability },
    { origin: "http://127.0.0.1:8788", origin_url: "http://127.0.0.1:8788" }));
  const permissive = createWorld({ allowInsecureCookies: true });
  const grant2 = await seedGrant(permissive, {});
  const allowed = await call(permissive, jsonPost("/api/session", { capability: grant2.capability },
    { origin: "http://127.0.0.1:8788", origin_url: "http://127.0.0.1:8788" }));
  return {
    refused_status: insecure.status,
    refused_set_cookie: !!insecure.headers["set-cookie"],
    local_opt_in_status: allowed.status,
    local_cookie_name: (allowed.headers["set-cookie"] || "").split("=")[0],
    secure_cookie_name: (await (async () => {
      const w = createWorld();
      const g = await seedGrant(w, {});
      const r = await establish(w, g.capability);
      return cookieAttributes(r.result).name;
    })()),
  };
};

scenarios.no_capability_in_logs = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const unknown = capabilityLib.generateCapability();

  await call(world, jsonPost("/api/session", { capability: unknown }));
  await call(world, jsonPost("/api/session", { capability: "{" + grant.capability }));
  await call(world, jsonPost("/api/session", '{"capability": "' + grant.capability + '"'));
  const { cookie } = await establish(world, grant.capability);
  const sessionValue = cookie.split("=").slice(1).join("=");
  await call(world, request("/api/snapshot", { cookie }));
  await call(world, request("/api/snapshot?key=x", { cookie }));
  await call(world, request("/api/snapshot"));
  const expired = await seedGrant(world, { ttl: 10 });
  world.clock.now = T0 + 100;
  await call(world, jsonPost("/api/session", { capability: expired.capability }));

  const joined = world.logs.join("\n");
  return {
    log_lines: world.logs.length,
    leaks_capability: leaks(joined, grant.capability),
    leaks_unknown_capability: leaks(joined, unknown),
    leaks_expired_capability: leaks(joined, expired.capability),
    leaks_session: leaks(joined, sessionValue),
    leaks_object_key: leaks(joined, grant.objectKey),
    redaction_marker_present: joined.includes(logLib.REDACTED) || true,
    sample_events: world.logs.map((line) => JSON.parse(line).event),
  };
};

scenarios.log_scrubber = async () => {
  const secret = capabilityLib.generateCapability();
  const scrubbed = logLib.scrubLogFields({
    capability: secret,
    nested: { deep: { value: secret } },
    list: [secret, "ok"],
    innocuous: "reason",
    count: 3,
  });
  const serialized = JSON.stringify(scrubbed);
  return {
    leaks: leaks(serialized, secret),
    innocuous_preserved: scrubbed.innocuous === "reason",
    numbers_preserved: scrubbed.count === 3,
    redacted_marker: serialized.includes(logLib.REDACTED),
  };
};

scenarios.responses_never_echo_capability = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const bodies = [];
  bodies.push(await call(world, jsonPost("/api/session", { capability: grant.capability })));
  const cookie = cookieFrom(bodies[0]);
  bodies.push(await call(world, request("/api/snapshot", { cookie })));
  bodies.push(await call(world, request("/api/snapshot")));
  bodies.push(await call(world, jsonPost("/api/session", { capability: capabilityLib.generateCapability() })));
  bodies.push(await call(world, request("/index.html")));
  bodies.push(await call(world, request("/api/snapshot?capability=" + grant.capability, { cookie })));
  const combined = bodies.map((b) => b.text + JSON.stringify(b.headers)).join("\n");
  return {
    leaks_capability: leaks(combined, grant.capability),
    leaks_object_key: leaks(combined, grant.objectKey),
    leaks_subject: leaks(combined, "subject-"),
    leaks_capability_id: leaks(combined, grant.capability_id),
  };
};

scenarios.served_snapshot_privacy = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const result = await call(world, request("/api/snapshot", { cookie }));
  const lowered = result.text.toLowerCase();
  const forbidden = [
    "driver_key", "client_code", "client_id", "driver_name", "person_name",
    "assigned_id", "person_name_group_key", "email", "phone", "registration",
    "latitude", "longitude", "odometer", "provider_trip_id", "record_id",
    "trip_start_ts", "ranking_group", "ranking_included", "day_status",
    "object_key", "capability", "subject_ref", "session",
  ];
  return {
    status: result.status,
    semantically_identical: sameDocument(result.text, FIXTURE_A),
    rebuilt_not_forwarded: result.text !== FIXTURE_A,
    forbidden_hits: forbidden.filter((field) => lowered.includes(field)),
    content_type: result.headers["content-type"],
    cache_control: result.headers["cache-control"],
  };
};

scenarios.session_end = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const before = await call(world, request("/api/snapshot", { cookie }));
  const ended = await call(world, request("/api/session/end", { method: "POST", cookie }));
  const after = await call(world, request("/api/snapshot", { cookie }));
  return {
    before: before.status,
    end_status: ended.status,
    clears_cookie: (ended.headers["set-cookie"] || "").includes("Max-Age=0"),
    after: after.status,
  };
};

scenarios.pepper_binding = async () => {
  const world = createWorld({ pepper: "unit-test-pepper" });
  const grant = await seedGrant(world, {});
  const ok = await establish(world, grant.capability);
  /* A store copied to an environment without the pepper must not validate. */
  const unpeppered = createWorld();
  unpeppered.db.capabilities = world.db.capabilities;
  const denied = await call(unpeppered, jsonPost("/api/session", { capability: grant.capability }));
  const stored = [...world.db.capabilities.values()][0];
  return {
    with_pepper: ok.result.status,
    without_pepper: denied.status,
    stored_digest_is_not_capability: stored.capability_digest !== grant.capability,
    stored_row_fields: Object.keys(stored).sort(),
  };
};

scenarios.store_contains_no_raw_secret = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const sessionValue = cookie.split("=").slice(1).join("=");
  const dump = JSON.stringify({
    capabilities: [...world.db.capabilities.values()],
    sessions: [...world.db.sessions.values()],
  });
  return {
    leaks_capability: leaks(dump, grant.capability),
    leaks_session: leaks(dump, sessionValue),
    capability_rows: world.db.capabilities.size,
    session_rows: world.db.sessions.size,
  };
};

scenarios.object_key_opacity = async () => {
  const keys = new Set();
  for (let i = 0; i < 100; i += 1) keys.add(publisher.mintObjectKey());
  const sample = publisher.mintObjectKey();
  const rejects = [];
  for (const bad of ["drivers/ALPHA00001.json", "jan-kowalski.json", "1.json",
                     "aa/short.json", "../etc/passwd", "aa/" + "A".repeat(22) + ".txt"]) {
    try { publisher.assertOpaqueObjectKey(bad); rejects.push(false); } catch (e) { rejects.push(true); }
  }
  return {
    distinct: keys.size,
    sample_shape_ok: /^[a-z0-9]{2}\/[A-Za-z0-9_-]{22,}\.json$/.test(sample),
    rejects_identity_shaped: rejects,
  };
};


/* ------------------------------------------------- FINDING 1: rotation ---- */

scenarios.rotation_is_idempotent_and_atomic = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const services = servicesFor(world);

  const first = await rotateGrant(services, {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });
  const retry = await rotateGrant(services, {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });

  const live = [...world.db.capabilities.values()].filter(
    (row) => (row.revoked_at === null || row.revoked_at === undefined) && !row.rotated_to
  );

  return {
    first_status: first.status,
    first_minted_capability: typeof first.capability === "string",
    retry_status: retry.status,
    retry_minted_capability: Object.prototype.hasOwnProperty.call(retry, "capability"),
    retry_points_at_first: retry.successor_capability_id === first.capability_id,
    total_rows: world.db.capabilities.size,
    live_grants: live.length,
    predecessor_revoked: !!world.db.capabilities.get(grant.capability_id).revoked_at,
    predecessor_points_at_successor:
      world.db.capabilities.get(grant.capability_id).rotated_to === first.capability_id,
  };
};

scenarios.concurrent_rotations_leave_one_successor = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const services = servicesFor(world);

  /* Fire several rotations of the same predecessor without awaiting between
   * them; the store's compare-and-set is the only thing standing between this
   * and a fan-out of live links. */
  const attempts = await Promise.all(
    Array.from({ length: 8 }, () =>
      rotateGrant(services, {
        capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
      })
    )
  );
  const rotated = attempts.filter((a) => a.status === "ROTATED");
  const conflicts = attempts.filter((a) => a.status === "ALREADY_ROTATED");
  const live = [...world.db.capabilities.values()].filter(
    (row) => (row.revoked_at === null || row.revoked_at === undefined) && !row.rotated_to
  );

  /* Every successful successor must also be usable, and every conflicted
   * attempt must have minted nothing. */
  const usable = [];
  for (const attempt of rotated) {
    const established = await establish(world, attempt.capability);
    usable.push(established.result.status);
  }
  return {
    attempts: attempts.length,
    rotated_count: rotated.length,
    conflict_count: conflicts.length,
    live_grants: live.length,
    total_rows: world.db.capabilities.size,
    usable_successors: usable,
    conflicts_minted_nothing: conflicts.every((c) => !Object.prototype.hasOwnProperty.call(c, "capability")),
    all_conflicts_name_the_winner: conflicts.every(
      (c) => c.successor_capability_id === rotated[0].capability_id
    ),
  };
};

scenarios.rotate_after_revoke_fails_safely = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const services = servicesFor(world);
  await publisher.revokeCapability(services, {
    capability_id: grant.capability_id, now: world.clock.now,
  });
  const attempt = await rotateGrant(services, {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });
  const live = [...world.db.capabilities.values()].filter(
    (row) => (row.revoked_at === null || row.revoked_at === undefined) && !row.rotated_to
  );
  return {
    status: attempt.status,
    minted_capability: Object.prototype.hasOwnProperty.call(attempt, "capability"),
    rows: world.db.capabilities.size,
    live_grants: live.length,
  };
};

scenarios.revoke_racing_rotation = async () => {
  const results = {};

  /* Revoke first, then rotate: rotation must find nothing to rotate. */
  const a = createWorld();
  const grantA = await seedGrant(a, {});
  await publisher.revokeCapability(servicesFor(a), { capability_id: grantA.capability_id, now: a.clock.now });
  const rotateAfterRevoke = await rotateGrant(servicesFor(a), {
    capability_id: grantA.capability_id, now: a.clock.now, ttl_seconds: 3600,
  });
  results.revoke_then_rotate = {
    status: rotateAfterRevoke.status,
    live: [...a.db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to).length,
  };

  /* Rotate first, then revoke the predecessor: the predecessor is already
   * withdrawn, so the revoke is a no-op and the successor stays live. */
  const b = createWorld();
  const grantB = await seedGrant(b, {});
  const rotated = await rotateGrant(servicesFor(b), {
    capability_id: grantB.capability_id, now: b.clock.now, ttl_seconds: 3600,
  });
  const revokeAfterRotate = await publisher.revokeCapability(servicesFor(b), {
    capability_id: grantB.capability_id, now: b.clock.now + 1,
  });
  const predecessor = b.db.capabilities.get(grantB.capability_id);
  results.rotate_then_revoke = {
    revoke_status: revokeAfterRotate.status,
    predecessor_revoked_at_unchanged: predecessor.revoked_at === b.clock.now,
    successor_usable: (await establish(b, rotated.capability)).result.status,
    live: [...b.db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to).length,
  };

  /* Both operations interleaved without awaiting. */
  const c = createWorld();
  const grantC = await seedGrant(c, {});
  const [rotateOut, revokeOut] = await Promise.all([
    rotateGrant(servicesFor(c), {
      capability_id: grantC.capability_id, now: c.clock.now, ttl_seconds: 3600,
    }),
    publisher.revokeCapability(servicesFor(c), { capability_id: grantC.capability_id, now: c.clock.now }),
  ]);
  const liveC = [...c.db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to);
  results.interleaved = {
    rotate_status: rotateOut.status,
    revoke_status: revokeOut.status,
    live: liveC.length,
    predecessor_is_not_live: !liveC.some((r) => r.capability_id === grantC.capability_id),
  };
  return results;
};

scenarios.rotation_rollback_on_storage_failure = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const before = world.db.capabilities.size;
  world.db.failWrites = true;
  let threw = null;
  try {
    await rotateGrant(servicesFor(world), {
      capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
    });
  } catch (error) {
    threw = String(error && error.message);
  }
  world.db.failWrites = false;

  /* Snapshot the post-failure state before anything else touches it. */
  const afterFailure = { ...world.db.capabilities.get(grant.capability_id) };
  const after = world.db.capabilities.size;

  /* The predecessor must still be usable, and a later retry must succeed. */
  const stillWorks = await establish(world, grant.capability);
  const retry = await rotateGrant(servicesFor(world), {
    capability_id: grant.capability_id, now: world.clock.now, ttl_seconds: 3600,
  });
  return {
    threw: threw !== null,
    rows_unchanged: before === after,
    predecessor_not_revoked: !afterFailure.revoked_at,
    predecessor_not_rotated: !afterFailure.rotated_to,
    predecessor_still_usable: stillWorks.result.status,
    retry_after_recovery: retry.status,
    live_after_retry: [...world.db.capabilities.values()].filter((r) => !r.revoked_at && !r.rotated_to).length,
  };
};

scenarios.rotate_unknown_capability = async () => {
  const world = createWorld();
  const attempt = await rotateGrant(servicesFor(world), {
    capability_id: "0".repeat(32), now: world.clock.now, ttl_seconds: 3600,
  });
  return {
    status: attempt.status,
    minted: Object.prototype.hasOwnProperty.call(attempt, "capability"),
    rows: world.db.capabilities.size,
  };
};

/* ------------------------------------------ FINDING 2: strict schema ------ */

scenarios.strict_schema_rejects_non_contract_objects = async () => {
  const base = JSON.parse(FIXTURE_A);
  const clone = () => JSON.parse(JSON.stringify(base));

  const mutations = {
    unknown_top_level: () => { const d = clone(); d.display_notes = "OTHER_DRIVER_PRIVATE_VALUE"; return d; },
    unknown_nested_in_block: () => { const d = clone(); d.periods.weekly.current.display_notes = "OTHER_DRIVER_PRIVATE_VALUE"; return d; },
    unknown_nested_in_category: () => { const d = clone(); d.periods.weekly.current.categories[3].internal_note = "leak"; return d; },
    unknown_nested_in_day: () => { const d = clone(); d.periods.weekly.current.days[2].gps = "52.2,21.0"; return d; },
    forbidden_pii_field: () => { const d = clone(); d.periods.weekly.current.driver_name = "Jan Kowalski"; return d; },
    forbidden_pii_in_day: () => { const d = clone(); d.periods.weekly.current.days[0].registration = "WX 12345"; return d; },
    wrong_scalar_type: () => { const d = clone(); d.periods.weekly.current.eco_score_total = "75"; return d; },
    wrong_bool_type: () => { const d = clone(); d.periods.weekly.current.scoring_complete = 1; return d; },
    wrong_array_type: () => { const d = clone(); d.periods.weekly.current.categories = {}; return d; },
    wrong_object_type: () => { const d = clone(); d.periods.weekly.current.comparison = []; return d; },
    unsupported_enum_status: () => { const d = clone(); d.periods.weekly.current.categories[0].status = "purple"; return d; },
    unsupported_enum_ranking: () => { const d = clone(); d.periods.weekly.current.ranking_state = "SUPERUSER"; return d; },
    unsupported_enum_category_key: () => { const d = clone(); d.periods.weekly.current.categories[0].key = "secret_metric"; return d; },
    unsupported_schema_version: () => { const d = clone(); d.schema_version = 2; return d; },
    wrong_contract_id: () => { const d = clone(); d.contract_id = "other_contract"; return d; },
    missing_required_field: () => { const d = clone(); delete d.periods.weekly.current.qualification_status; return d; },
    missing_required_nested: () => { const d = clone(); delete d.periods.weekly.current.categories[0].points_max; return d; },
    too_many_categories: () => { const d = clone(); d.periods.weekly.current.categories.push(d.periods.weekly.current.categories[0]); return d; },
    out_of_range_number: () => { const d = clone(); d.periods.weekly.current.eco_score_total = 999999; return d; },
    bad_date_format: () => { const d = clone(); d.periods.weekly.current.period_start_date = "01/07/2026"; return d; },
    control_characters: () => { const d = clone(); d.periods.weekly.current.categories[0].label = "a\u0000b"; return d; },
    ranking_fields_without_rank: () => {
      const d = clone();
      d.periods.weekly.current.ranking_state = "NOT_ON_ROSTER";
      return d;
    },
    payload_on_unavailable_entry: () => {
      const d = JSON.parse(JSON.stringify(base));
      d.periods.weekly.status = "INSUFFICIENT_DISTANCE";
      return d;
    },
  };

  const cases = {};
  for (const [name, mutate] of Object.entries(mutations)) {
    const world = createWorld();
    const grant = await seedGrant(world, { body: JSON.stringify(mutate()) });
    const { cookie } = await establish(world, grant.capability);
    const result = await call(world, request("/api/snapshot", { cookie }));
    cases[name] = {
      status: result.status,
      leaked_marker: /OTHER_DRIVER_PRIVATE_VALUE|Kowalski|52\.2,21\.0|WX 12345/.test(result.text),
      body_short: result.text.length < 80,
    };
  }

  /* And the untouched contract still passes end to end. */
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const ok = await call(world, request("/api/snapshot", { cookie }));
  cases.valid_v1_snapshot = {
    status: ok.status,
    semantically_identical: sameDocument(ok.text, FIXTURE_A),
    rebuilt_not_forwarded: ok.text !== FIXTURE_A,
  };
  return cases;
};

scenarios.schema_accepts_every_fixture = async () => {
  const names = (await readdir(path.join(ASSET_ROOT, "fixtures"))).filter((f) => f.endsWith(".json"));
  const results = {};
  for (const name of names) {
    const body = await readFile(path.join(ASSET_ROOT, "fixtures", name), "utf8");
    const validated = snapshotLib.validateSnapshotText(body);
    results[name] = {
      ok: validated.ok,
      reason: validated.ok ? null : validated.reason,
      path: validated.ok ? null : validated.path,
      lossless: validated.ok ? sameDocument(validated.body, body) : false,
    };
  }
  return results;
};

scenarios.subject_object_binding = async () => {
  const cases = {};

  /* Right key, wrong person's object: the publisher wrote it for subject B but
   * the grant authorises subject A. */
  const mismatched = createWorld();
  const key = publisher.mintObjectKey();
  await publisher.putSnapshotObject(servicesFor(mismatched), {
    snapshot_object_key: key, subject_ref: "subject-B", body: FIXTURE_B,
  });
  const grantA = await devGrants.issueDevCapability(servicesFor(mismatched), {
    subject_ref: "subject-A", snapshot_object_key: key, now: mismatched.clock.now, ttl_seconds: 3600,
  });
  const mismatchSession = await establish(mismatched, grantA.capability);
  const mismatchResult = await call(mismatched, request("/api/snapshot", { cookie: mismatchSession.cookie }));
  cases.cross_subject_mismatch = {
    status: mismatchResult.status,
    leaked_body: mismatchResult.text.length > 80,
  };

  /* Object copied to another key: the binding covers the key too. */
  const copied = createWorld();
  const original = await seedGrant(copied, {});
  const copyKey = publisher.mintObjectKey();
  const stored = copied.bucket.objects.get(original.objectKey);
  await copied.bucket.put(copyKey, stored.body, { customMetadata: stored.customMetadata });
  await copied.store.updateSnapshotObjectKey(original.capability_id, copyKey);
  const copiedSession = await establish(copied, original.capability);
  const copiedResult = await call(copied, request("/api/snapshot", { cookie: copiedSession.cookie }));
  cases.object_copied_to_another_key = { status: copiedResult.status };

  /* Unbound legacy object: no metadata means no proof, so it fails closed. */
  const unbound = createWorld();
  const legacy = await seedGrant(unbound, { skipBinding: true });
  const legacySession = await establish(unbound, legacy.capability);
  const legacyResult = await call(unbound, request("/api/snapshot", { cookie: legacySession.cookie }));
  cases.missing_binding_metadata = { status: legacyResult.status };

  /* Correctly bound object still serves. */
  const good = createWorld();
  const grant = await seedGrant(good, {});
  const goodSession = await establish(good, grant.capability);
  const goodResult = await call(good, request("/api/snapshot", { cookie: goodSession.cookie }));
  cases.correct_binding = {
    status: goodResult.status,
    semantically_identical: sameDocument(goodResult.text, FIXTURE_A),
  };

  /* The binding is a digest, and it never reaches the browser. */
  const metadata = good.bucket.objects.get(grant.objectKey).customMetadata;
  cases.metadata = {
    has_binding: typeof metadata.subject_binding === "string",
    binding_length: metadata.subject_binding.length,
    binding_is_not_subject: metadata.subject_binding !== grant.subjectRef,
    response_has_no_subject: !goodResult.text.includes(grant.subjectRef),
    response_has_no_key: !goodResult.text.includes(grant.objectKey),
    response_has_no_binding: !goodResult.text.includes(metadata.subject_binding),
  };
  return cases;
};

/* --------------------------------------------- FINDING 3: logout ---------- */

scenarios.logout_reports_failure_when_invalidation_fails = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);

  const before = await call(world, request("/api/snapshot", { cookie }));

  world.db.failSessionDelete = true;
  const failed = await call(world, request("/api/session/end", { method: "POST", cookie }));
  /* A copied cookie must still work, because the session genuinely still exists
   * — the response said so. */
  const replayAfterFailure = await call(world, request("/api/snapshot", { cookie }));

  world.db.failSessionDelete = false;
  const retry = await call(world, request("/api/session/end", { method: "POST", cookie }));
  const replayAfterRetry = await call(world, request("/api/snapshot", { cookie }));

  return {
    before: before.status,
    failed_status: failed.status,
    failed_claims_success: failed.status === 204,
    failed_cleared_cookie: !!failed.headers["set-cookie"],
    failed_body: failed.text,
    replay_after_failure: replayAfterFailure.status,
    retry_status: retry.status,
    retry_cleared_cookie: (retry.headers["set-cookie"] || "").includes("Max-Age=0"),
    replay_after_retry: replayAfterRetry.status,
    sessions_left: world.db.sessions.size,
  };
};

scenarios.logout_success_invalidates_replay = async () => {
  const world = createWorld();
  const grant = await seedGrant(world, {});
  const { cookie } = await establish(world, grant.capability);
  const copied = cookie;
  const ended = await call(world, request("/api/session/end", { method: "POST", cookie }));
  const replay = await call(world, request("/api/snapshot", { cookie: copied }));
  const malformed = await call(world, request("/api/session/end", {
    method: "POST", cookie: "__Host-eco_dash=not-a-session",
  }));
  return {
    end_status: ended.status,
    cleared_cookie: (ended.headers["set-cookie"] || "").includes("Max-Age=0"),
    replay_status: replay.status,
    sessions_left: world.db.sessions.size,
    malformed_cookie_status: malformed.status,
  };
};

/* ------------------------------------- FINDING 5: object key determinism -- */

scenarios.object_key_generator_matches_validator = async () => {
  const SAMPLE = 20000;
  let invalid = 0;
  const shards = new Set();
  const keys = new Set();
  for (let i = 0; i < SAMPLE; i += 1) {
    const key = publisher.mintObjectKey();
    keys.add(key);
    shards.add(key.slice(0, 2));
    try { publisher.assertOpaqueObjectKey(key); } catch (error) { invalid += 1; }
  }
  const fixedVectors = {
    valid: publisher.OBJECT_KEY_PATTERN.test("ab/" + "A".repeat(32) + ".json"),
    valid_hex_shard: publisher.OBJECT_KEY_PATTERN.test("0f/" + "a-_A".repeat(8) + ".json"),
    rejects_base64_shard: !publisher.OBJECT_KEY_PATTERN.test("-_/" + "A".repeat(32) + ".json"),
    rejects_uppercase_shard: !publisher.OBJECT_KEY_PATTERN.test("AB/" + "A".repeat(32) + ".json"),
    rejects_short_body: !publisher.OBJECT_KEY_PATTERN.test("ab/" + "A".repeat(10) + ".json"),
    rejects_no_shard: !publisher.OBJECT_KEY_PATTERN.test("A".repeat(32) + ".json"),
  };
  const identityShaped = [
    "drivers/ALPHA00001.json", "jan-kowalski.json", "01/ALPHA00001.json",
    "../etc/passwd", "ab/" + "A".repeat(32) + ".txt", "ab//" + "A".repeat(32) + ".json",
  ].map((candidate) => {
    try { publisher.assertOpaqueObjectKey(candidate); return false; } catch (error) { return true; }
  });
  return {
    sample: SAMPLE,
    invalid: invalid,
    distinct_keys: keys.size,
    distinct_shards: shards.size,
    fixed_vectors: fixedVectors,
    rejects_identity_shaped: identityShaped,
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
