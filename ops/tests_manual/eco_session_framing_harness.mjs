/* Request-framing harness for `POST /api/session`.
 *
 * Executes the REAL Worker (delivery/driver_eco_dashboard/worker/index.js)
 * against in-memory D1/R2/ASSETS/rate-limit bindings and prints one JSON object
 * per scenario, so ops/tests_manual/test_driver_eco_dashboard_session_framing.py
 * can assert on it. No wrangler, no credentials, no remote Cloudflare resource,
 * no deployment.
 *
 * WHAT IT PROVES
 *
 * The endpoint's memory bound no longer depends on the runtime handing back a
 * BYOB reader — deployed verification established that Cloudflare's incoming
 * `Request.body` is not a byte stream, so it never will there. The bound comes
 * from HTTP framing instead:
 *
 *   1. exactly one canonical `Content-Length`, <= 512, is REQUIRED before the
 *      body is opened, and every other form fails closed;
 *   2. the read is then bounded by min(declared, 512) and the actual byte count
 *      is checked against the declaration in both directions;
 *   3. both reader modes satisfy the same invariant, so neither is a gate.
 *
 * The rate limiter must still run FIRST, and a framing refusal must cost no D1
 * statement, no session and no cookie.
 *
 * Capabilities and client addresses are synthetic and are NEVER printed.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";
import { edgeRequest } from "./eco_edge_framing.mjs";
import { canonicalFixture } from "./eco_canonical_fixture.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const DELIVERY = path.join(REPO, "delivery", "driver_eco_dashboard");
const ASSET_ROOT = path.join(REPO, "assets", "driver_eco_dashboard");

await import(path.join(DELIVERY, "local", "node_runtime.js"));

const worker = (await import(path.join(DELIVERY, "worker", "index.js"))).default;
const bindings = await import(path.join(DELIVERY, "local", "memory_bindings.js"));
const publisher = await import(path.join(DELIVERY, "worker", "lib", "publisher.js"));
const devGrants = await import(path.join(DELIVERY, "local", "dev_grants.js"));
const { D1AuthorizationStore } = await import(path.join(DELIVERY, "worker", "lib", "store.js"));
const bodyLib = await import(path.join(DELIVERY, "worker", "lib", "body.js"));

const ORIGIN = "https://dashboard.example.invalid";
const CLIENT_IP = "203.0.113.10";
const T0 = 1_800_000_000;
const MAX = 512;

const FIXTURE = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_acceptable.json"));

/* Well formed, never issued. Travels the whole read path and is then refused at
 * the authorization store, so no real capability is needed to exercise it. */
const UNKNOWN_CAPABILITY = "U".repeat(43);

/* ------------------------------------------------------------------ world -- */

function createWorld(options) {
  const settings = options || {};
  const db = new bindings.MemoryD1();
  const bucket = new bindings.MemoryR2();
  const logs = [];
  const clock = { now: settings.now || T0 };
  const limiter = settings.rateLimiter === null
    ? null
    : (settings.rateLimiter || new bindings.MemoryRateLimiter({ now: () => clock.now }));
  const env = bindings.createEnv({
    db,
    bucket,
    assets: bindings.createAssetsBinding(ASSET_ROOT),
    assetRoot: ASSET_ROOT,
    now: () => clock.now,
    rateLimiter: settings.rateLimiter === null ? null : limiter,
    logSink: { log: (line) => logs.push(line), error: (line) => logs.push(line) },
  });
  return { db, bucket, env, logs, clock, limiter, store: new D1AuthorizationStore(db) };
}

function servicesFor(world) {
  return { db: world.db, store: world.store, bucket: world.bucket, pepper: world.env.CAPABILITY_PEPPER };
}

async function seedGrant(world) {
  const key = publisher.mintObjectKey();
  const subject = "subject-" + key.slice(3, 11);
  await publisher.putSnapshotObject(servicesFor(world), {
    snapshot_object_key: key, subject_ref: subject, body: FIXTURE,
  });
  return devGrants.issueDevCapability(servicesFor(world), {
    subject_ref: subject, snapshot_object_key: key,
    now: world.clock.now, ttl_seconds: 3600,
  });
}

/* ---------------------------------------------------------------- requests -- */

/**
 * A session POST with FULL control over the declared length.
 *
 * `declared: undefined` means "let framing declare the truth", which is what a
 * browser and the Cloudflare edge both do. Any other value is written verbatim,
 * including values a browser could never produce — that is the seam the
 * negative cases need, and it exists only in this harness.
 */
function sessionPost(body, options) {
  const settings = options || {};
  const headers = new Headers({
    "Content-Type": "application/json",
    Origin: ORIGIN,
    "CF-Connecting-IP": settings.clientIp || CLIENT_IP,
  });
  /* A stream body needs `duplex: "half"` in undici; a string body must not
   * carry it. Cloudflare requires neither — this is a Node constructor detail. */
  const init = body instanceof ReadableStream
    ? { method: "POST", headers, body, duplex: "half" }
    : { method: "POST", headers, body };
  if (settings.declared === null) {
    /* Explicitly undeclared: no Content-Length reaches the Worker at all. */
    return new Request(ORIGIN + "/api/session", init);
  }
  if (settings.declared !== undefined) headers.set("Content-Length", String(settings.declared));
  return edgeRequest(ORIGIN + "/api/session", init);
}

async function call(world, request) {
  const response = await worker.fetch(request, world.env, {});
  const text = await response.clone().text();
  const headers = {};
  for (const [name, value] of response.headers) headers[name.toLowerCase()] = value;
  return { status: response.status, headers, text };
}

function events(world) {
  return world.logs.map((line) => {
    try { return JSON.parse(line); } catch (error) { return { unparsed: line }; }
  });
}

function lastEvent(world, name) {
  const matching = events(world).filter((entry) => entry.event === name);
  return matching.length ? matching[matching.length - 1] : null;
}

/**
 * One framing probe, measured rather than asserted.
 *
 * The body is a stream that yields one small chunk and then never resolves, so
 * "the reader did not run" is proved by the response arriving at all: any code
 * that actually read this body would block forever. D1 statements, cookies and
 * limiter calls are counted around the call.
 */
function stallingStream(counter) {
  return new ReadableStream({
    pull(controller) {
      counter.pulls += 1;
      if (counter.pulls === 1) { controller.enqueue(new Uint8Array(8)); return; }
      counter.stalled = true;
      return new Promise(() => {});
    },
    cancel() { counter.cancelled = true; },
  });
}

async function callWithin(world, request, milliseconds) {
  let timer = null;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ status: "STALLED", headers: {}, text: "" }), milliseconds);
  });
  try {
    return await Promise.race([call(world, request), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function framingProbe(declared) {
  const world = createWorld();
  await seedGrant(world);
  const statementsBefore = world.db.statementLog.length;
  const limiterBefore = world.limiter.calls.length;
  const counter = { pulls: 0, stalled: false, cancelled: false };
  const result = await callWithin(world, sessionPost(stallingStream(counter), {
    declared,
  }), 3000);
  const event = lastEvent(world, "session_exchange_rejected");
  return {
    status: result.status,
    body: result.text,
    /* 1 = the stream's own queue prefetch. More, or a stalled reader, means
     * the Worker opened a body it should have refused unread. */
    body_pulls: counter.pulls,
    body_reader_blocked: counter.stalled,
    d1_statements: world.db.statementLog.length - statementsBefore,
    /* The limiter must have been consulted BEFORE the framing gate. */
    limiter_calls: world.limiter.calls.length - limiterBefore,
    sessions: world.db.sessions.size,
    set_cookie: result.headers["set-cookie"] || null,
    cache_control: result.headers["cache-control"],
    csp_present: !!result.headers["content-security-policy"],
    reason: event && event.reason,
    reader_entered: event && event.reader_entered,
    body_read_mode: event && event.body_read_mode,
    bytes_read: event && event.bytes_read,
  };
}

/* -------------------------------------------------------------- scenarios -- */

const scenarios = {};

/**
 * The whole accepted/rejected grammar, one probe per form, each with a body
 * that cannot be read without hanging.
 */
scenarios.declared_length_grammar = async () => {
  const cases = {};

  /* --- refused before the reader ------------------------------------- */
  cases.missing = await framingProbe(null);
  cases.empty = await framingProbe("");
  cases.blank = await framingProbe("   ");
  cases.malformed_text = await framingProbe("not-a-number");
  cases.duplicated = await framingProbe("60, 60");
  cases.comma_joined_conflict = await framingProbe("60, 512");
  cases.signed_positive = await framingProbe("+60");
  cases.signed_negative = await framingProbe("-1");
  cases.decimal = await framingProbe("60.0");
  cases.exponent = await framingProbe("6e1");
  cases.hexadecimal = await framingProbe("0x3c");
  cases.trailing_unit = await framingProbe("60 bytes");
  cases.internal_space = await framingProbe("6 0");
  cases.out_of_range = await framingProbe("9".repeat(16));
  cases.over_ceiling = await framingProbe(String(MAX + 1));
  cases.far_over_ceiling = await framingProbe(String(5 * 1024 * 1024));

  /* --- accepted into the reader --------------------------------------- *
   * These declare a size this route allows, so the framing gate passes and
   * the stalling stream then blocks: STALLED is the PROOF that the reader
   * was entered, which is exactly the opposite of every case above. */
  cases.exactly_at_ceiling = await framingProbe(String(MAX));
  cases.small_valid = await framingProbe("60");
  cases.zero = await framingProbe("0");
  cases.zero_padded = await framingProbe("00060");

  return cases;
};

/** The pure parser, independent of any route. */
scenarios.declared_length_parser = async () => {
  const parse = (value) => {
    const headers = new Headers();
    if (value !== null) headers.set("Content-Length", value);
    return bodyLib.parseDeclaredLength(headers);
  };
  const bounded = (value) => {
    const headers = new Headers();
    if (value !== null) headers.set("Content-Length", value);
    return bodyLib.requireDeclaredLength(headers, MAX);
  };
  return {
    vocabulary: bodyLib.CONTENT_LENGTH_REJECTION,
    parse: {
      missing: parse(null),
      empty: parse(""),
      blank: parse("   "),
      zero: parse("0"),
      small: parse("60"),
      at_ceiling: parse(String(MAX)),
      above_ceiling: parse(String(MAX + 1)),
      zero_padded: parse("00060"),
      zero_padded_long: parse("0".repeat(40) + "60"),
      signed_plus: parse("+60"),
      signed_minus: parse("-60"),
      decimal: parse("60.0"),
      exponent: parse("6e1"),
      hex: parse("0x3c"),
      comma: parse("60, 60"),
      unit: parse("60 bytes"),
      internal_space: parse("6 0"),
      sixteen_digits: parse("9".repeat(16)),
      fifteen_digits: parse("9".repeat(15)),
    },
    bounded: {
      at_ceiling: bounded(String(MAX)),
      above_ceiling: bounded(String(MAX + 1)),
      missing: bounded(null),
    },
  };
};

/**
 * Actual-versus-declared, in both directions, at the reader.
 *
 * These call `readBoundedBody` directly with an explicit declaration, which is
 * exactly how the route calls it. Node's `Request` does not recompute
 * `Content-Length`, so a disagreeing pair can be constructed here even though
 * no conforming HTTP peer could send one.
 */
scenarios.actual_versus_declared = async () => {
  const read = async (bodyText, declared, maxBytes) => {
    const request = new Request(ORIGIN + "/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": String(declared) },
      body: bodyText,
    });
    const result = await bodyLib.readBoundedBody(request, maxBytes === undefined ? MAX : maxBytes, {
      declaredLength: declared,
    });
    return {
      ok: result.ok, mode: result.mode, reason: result.reason,
      bytes_read: result.bytesRead, largest_chunk_bytes: result.largestChunkBytes,
      actual: new TextEncoder().encode(bodyText).byteLength, declared,
    };
  };

  return {
    /* Honest framing: accepted, and the byte count equals the declaration. */
    exact_small: await read("x".repeat(60), 60),
    exact_at_ceiling: await read("x".repeat(MAX), MAX),
    exact_zero: await read("", 0),
    /* Over-run within the endpoint ceiling: the declaration is the bound. */
    longer_than_declared: await read("x".repeat(200), 64),
    /* Over-run past the endpoint ceiling: the ceiling is the bound, and the
     * reason names it, so the two layers stay distinguishable. */
    longer_than_ceiling: await read("x".repeat(4096), MAX),
    /* Short body: a framing failure, refused rather than parsed truncated. */
    shorter_than_declared: await read("x".repeat(10), 60),
    /* A declaration above the endpoint ceiling never reaches the reader. */
    declared_over_ceiling: await read("x".repeat(10), MAX + 1),
  };
};

/** Both reader modes satisfy the same invariant; neither is a gate. */
scenarios.both_reader_modes_are_bounded = async () => {
  const payload = JSON.stringify({ capability: UNKNOWN_CAPABILITY });
  const bytes = new TextEncoder().encode(payload);

  /* Whatever `Request.body` happens to be in the HOST runtime. Node 18 gives a
   * non-byte stream (default reader) and Node 22 gives a byte stream (BYOB);
   * Cloudflare gives a non-byte stream. The mode is REPORTED, never asserted —
   * that difference between three runtimes is exactly why it cannot be a gate. */
  const nativeRequest = new Request(ORIGIN + "/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Content-Length": String(bytes.byteLength) },
    body: payload,
  });
  const nativeRead = await bodyLib.readBoundedBody(nativeRequest, MAX, {
    declaredLength: bytes.byteLength,
  });

  /* BYOB: a real byte stream, which is where the opportunistic path applies.
   * A plain object is enough — `readBoundedBody` only needs `headers` and
   * `body`, and this keeps the byte-stream fixture from being normalised away
   * by the `Request` constructor. */
  const byteStream = new ReadableStream({
    type: "bytes",
    pull(controller) {
      controller.enqueue(bytes.slice());
      controller.close();
    },
  });
  const byobRequest = {
    headers: new Headers({
      "Content-Type": "application/json",
      "Content-Length": String(bytes.byteLength),
    }),
    body: byteStream,
  };
  const byobRead = await bodyLib.readBoundedBody(byobRequest, MAX, {
    declaredLength: bytes.byteLength,
  });

  const describe = (result) => ({
    ok: result.ok, mode: result.mode, reason: result.reason,
    bytes_read: result.bytesRead, largest_chunk_bytes: result.largestChunkBytes,
    text_matches: result.ok ? result.text === payload : null,
  });

  return {
    payload_bytes: bytes.byteLength,
    runtime: process.version,
    /* The mode this runtime chose for a plain `Request.body`. */
    native: describe(nativeRead),
    /* A forced byte stream: the opportunistic path, wherever it is available. */
    byob: describe(byobRead),
    byob_path_is_reachable: byobRead.mode === "byob",
    byob_buffer_bytes: bodyLib.BYOB_BUFFER_BYTES,
  };
};

/**
 * The real frontend request shape, end to end.
 *
 * `assets/driver_eco_dashboard/js/boot.js` sends exactly this: a same-origin
 * POST, `Content-Type: application/json`, body `JSON.stringify({capability})`.
 * The browser supplies `Content-Length` itself — script is FORBIDDEN to set it —
 * and `edgeRequest` reproduces that and nothing else.
 */
scenarios.frontend_bootstrap_is_compatible = async () => {
  const world = createWorld();
  const grant = await seedGrant(world);

  const bootSource = await (await import("node:fs/promises"))
    .readFile(path.join(ASSET_ROOT, "js", "boot.js"), "utf8");

  /* Byte-for-byte the body boot.js builds. */
  const payload = JSON.stringify({ capability: grant.capability });
  const request = edgeRequest(ORIGIN + "/api/session", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      Origin: ORIGIN,
      "CF-Connecting-IP": CLIENT_IP,
    },
    body: payload,
  });
  const declaredHeader = request.headers.get("Content-Length");
  const result = await call(world, request);

  const established = lastEvent(world, "session_established");

  /* And the same shape with an unknown capability, which is what the deployed
   * workers.dev probe sends: it must read the body and refuse at the store. */
  const probeWorld = createWorld();
  const probePayload = JSON.stringify({ capability: UNKNOWN_CAPABILITY });
  const probeResult = await call(probeWorld, edgeRequest(ORIGIN + "/api/session", {
    method: "POST",
    headers: {
      "Content-Type": "application/json", Origin: ORIGIN, "CF-Connecting-IP": CLIENT_IP,
    },
    body: probePayload,
  }));

  return {
    /* The frontend never sets the header itself; the browser does. */
    boot_sets_content_length: /content-length/i.test(bootSource),
    boot_uses_json_stringify: bootSource.includes("JSON.stringify({ capability: capability })"),
    boot_endpoint: bootSource.includes('var SESSION_ENDPOINT = "/api/session";'),
    payload_bytes: new TextEncoder().encode(payload).byteLength,
    declared_header: declaredHeader,
    declared_matches_payload:
      declaredHeader === String(new TextEncoder().encode(payload).byteLength),
    status: result.status,
    sets_cookie: !!result.headers["set-cookie"],
    sessions: world.db.sessions.size,
    established_ttl: established && established.ttl_seconds,
    logs_leak_capability: world.logs.join("\n").includes(grant.capability),
    probe_status: probeResult.status,
    probe_event: lastEvent(probeWorld, "session_exchange_denied"),
    probe_payload_bytes: new TextEncoder().encode(probePayload).byteLength,
    ceiling: MAX,
  };
};

/**
 * Ordering: the limiter decides BEFORE any framing or body work.
 *
 * A limited actor sending a request with no declared length at all must get the
 * 429, not the framing refusal — otherwise the framing gate would have become
 * an uncounted, unlimited pre-filter.
 */
scenarios.rate_limit_still_runs_first = async () => {
  const world = createWorld({
    rateLimiter: new bindings.MemoryRateLimiter({ forceSuccess: false }),
  });
  const statementsBefore = world.db.statementLog.length;

  const counter = { pulls: 0, stalled: false, cancelled: false };
  const undeclared = await callWithin(world, sessionPost(stallingStream(counter), {
    declared: null,
  }), 3000);
  const oversizedDeclared = await call(world, sessionPost("{}", { declared: String(5 * 1024 * 1024) }));
  const malformedDeclared = await call(world, sessionPost("{}", { declared: "nonsense" }));

  return {
    undeclared_status: undeclared.status,
    undeclared_retry_after: undeclared.headers["retry-after"],
    oversized_declared_status: oversizedDeclared.status,
    malformed_declared_status: malformedDeclared.status,
    /* Every one of them was counted, which is the point. */
    limiter_calls: world.limiter.calls.length,
    d1_statements: world.db.statementLog.length - statementsBefore,
    sessions: world.db.sessions.size,
    body_pulls: counter.pulls,
    events: events(world).filter((entry) => entry.event === "session_exchange_rejected"),
  };
};

/** No framing diagnostic may name a body, a capability or an actor. */
scenarios.framing_diagnostics_are_private = async () => {
  const world = createWorld();
  const grant = await seedGrant(world);
  const marker = "MARKER_BODY_VALUE_THAT_MUST_NOT_BE_LOGGED";
  const body = JSON.stringify({ capability: grant.capability, note: marker });

  /* Every framing refusal, over a body that carries a real capability. */
  await call(world, sessionPost(body, { declared: null }));
  await call(world, sessionPost(body, { declared: "" }));
  await call(world, sessionPost(body, { declared: "-1" }));
  await call(world, sessionPost(body, { declared: "60, 60" }));
  await call(world, sessionPost(body, { declared: String(5 * 1024 * 1024) }));
  /* And a mismatch that DID enter the reader. */
  await call(world, sessionPost(body, { declared: "10" }));

  const all = world.logs.join("\n");
  return {
    log_lines: world.logs.length,
    reasons: events(world).map((entry) => entry.reason).filter(Boolean),
    leaks_capability: all.includes(grant.capability),
    leaks_body_marker: all.includes(marker),
    leaks_client_ip: all.includes(CLIENT_IP),
    leaks_rate_limit_key: all.includes("session:" + CLIENT_IP),
    /* Fixed-vocabulary reasons only: nothing echoed back from the request. */
    reason_charset_ok: events(world)
      .map((entry) => entry.reason)
      .filter(Boolean)
      .every((reason) => /^[A-Z_]+$/.test(reason)),
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
