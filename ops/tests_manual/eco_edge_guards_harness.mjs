/* Edge-guard harness for the Driver Eco Dashboard V1 delivery Worker.
 *
 * Executes the REAL Worker (delivery/driver_eco_dashboard/worker/index.js)
 * against in-memory D1/R2/ASSETS/rate-limit bindings and prints one JSON object
 * per scenario, so ops/tests_manual/test_driver_eco_dashboard_edge_guards.py
 * can assert on it. No wrangler, no credentials, no remote Cloudflare resource,
 * no deployment.
 *
 * Covers the two pre-real-traffic application-layer guards:
 *
 *   1. Worker-native rate limiting on POST /api/session, and on nothing else.
 *   2. Structured, privacy-safe evidence of which request-body reader mode the
 *      runtime actually used, so the deployed BYOB contract can be established
 *      from a log line rather than assumed.
 *
 * Capabilities and client addresses are synthetic and are NEVER printed: every
 * scenario reports booleans, counts, lengths or fixed vocabulary strings.
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
const rateLimitLib = await import(path.join(DELIVERY, "worker", "lib", "rate_limit.js"));
const bodyLib = await import(path.join(DELIVERY, "worker", "lib", "body.js"));
const logLib = await import(path.join(DELIVERY, "worker", "lib", "log.js"));

const ORIGIN = "https://dashboard.example.invalid";
/* Synthetic documentation addresses (TEST-NET-3 / the IPv6 documentation
 * prefix). Never a real peer, and never printed by a scenario. */
const CLIENT_IP = "203.0.113.10";
const OTHER_IP = "203.0.113.77";
const T0 = 1_800_000_000;

const FIXTURE = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_acceptable.json"));

/* A well-formed capability that was never issued. This is the synthetic value
 * the deployed workers.dev probe will use: it travels the whole body-read path
 * and is then refused at the authorization store. */
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

function headersFor(settings) {
  const headers = new Headers(settings.headers || {});
  if (settings.origin !== null) headers.set("Origin", settings.origin || ORIGIN);
  if (settings.clientIp !== null) headers.set("CF-Connecting-IP", settings.clientIp || CLIENT_IP);
  if (settings.cookie) headers.set("Cookie", settings.cookie);
  return headers;
}

function sessionPost(payload, options) {
  const settings = options || {};
  const headers = headersFor(settings);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return edgeRequest(ORIGIN + (settings.pathname || "/api/session"), {
    method: "POST",
    headers,
    body: typeof payload === "string" ? payload : JSON.stringify(payload),
  });
}

function streamRequest(stream, options) {
  const settings = options || {};
  const headers = headersFor(settings);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return edgeRequest(ORIGIN + "/api/session", {
    method: "POST", headers, body: stream, duplex: "half",
  });
}

/** A stream that reports exactly how many bytes were pulled out of it. */
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

/**
 * A body stream that hands over ONE small chunk and then never resolves.
 *
 * This is the strong form of "the body reader was not invoked": the first chunk
 * is prefetched into the stream's own queue by `ReadableStream` itself, but any
 * consumer that actually reads the body blocks forever on the second pull. A
 * scenario that returns a response promptly therefore proves the Worker never
 * entered `readBoundedBody`, rather than inferring it from a byte count that
 * the fixture's own queueing could explain.
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

/** Resolves to `{ status: "STALLED" }` rather than hanging the whole harness. */
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

async function call(world, request) {
  const response = await worker.fetch(request, world.env, {});
  const text = await response.clone().text();
  const headers = {};
  for (const [name, value] of response.headers) headers[name.toLowerCase()] = value;
  return { status: response.status, headers, text };
}

/** Parsed structured log lines, newest last. */
function events(world) {
  return world.logs.map((line) => {
    try { return JSON.parse(line); } catch (error) { return { unparsed: line }; }
  });
}

function lastEvent(world, name) {
  const matching = events(world).filter((entry) => entry.event === name);
  return matching.length ? matching[matching.length - 1] : null;
}

function leaks(world, secret) {
  return world.logs.join("\n").includes(secret);
}

/* -------------------------------------------------------------- scenarios -- */

const scenarios = {};

/* ------------------------------------------------------------ route scope -- */

/**
 * The limiter must fire for POST /api/session and for nothing else.
 *
 * Every other route is exercised against the SAME world, so the assertion is
 * not "a fresh limiter saw nothing" but "this limiter, which demonstrably works
 * for the session exchange, was never consulted for any of these".
 */
scenarios.rate_limit_applies_only_to_the_session_exchange = async () => {
  const world = createWorld();
  const grant = await seedGrant(world);

  const probes = [];
  const record = async (label, request) => {
    const before = world.limiter.calls.length;
    const result = await call(world, request);
    probes.push({
      label, status: result.status,
      limiter_calls: world.limiter.calls.length - before,
    });
    return result;
  };

  const exchange = await record("post_session", sessionPost({ capability: grant.capability }));
  const cookie = (exchange.headers["set-cookie"] || "").split(";")[0] || null;

  await record("get_session", edgeRequest(ORIGIN + "/api/session", {
    method: "GET", headers: headersFor({}),
  }));
  await record("post_session_end", edgeRequest(ORIGIN + "/api/session/end", {
    method: "POST", headers: headersFor({ cookie }),
  }));
  await record("get_snapshot", edgeRequest(ORIGIN + "/api/snapshot", {
    method: "GET", headers: headersFor({ cookie }),
  }));
  await record("post_publish", edgeRequest(ORIGIN + "/api/publish", {
    method: "POST", headers: headersFor({ headers: { "Content-Type": "application/json" } }),
    body: "{}",
  }));
  await record("post_publish_recover", edgeRequest(ORIGIN + "/api/publish/recover", {
    method: "POST", headers: headersFor({}),
  }));
  await record("post_publish_delivery", edgeRequest(ORIGIN + "/api/publish/delivery", {
    method: "POST", headers: headersFor({}),
  }));
  await record("unknown_api", edgeRequest(ORIGIN + "/api/nope", {
    method: "POST", headers: headersFor({}),
  }));
  await record("asset_document", edgeRequest(ORIGIN + "/", {
    method: "GET", headers: headersFor({}),
  }));
  await record("asset_bundle", edgeRequest(ORIGIN + "/js/boot.js", {
    method: "GET", headers: headersFor({}),
  }));

  return {
    probes,
    total_limiter_calls: world.limiter.calls.length,
    /* The one call that happened carried the namespaced per-actor key. */
    key_prefixed: world.limiter.calls.every((key) => key.startsWith("session:")),
  };
};

/* ------------------------------------------------------- allowed / limited -- */

/** Under the limit, the exchange behaves exactly as it always has. */
scenarios.allowed_request_is_unchanged = async () => {
  const world = createWorld();
  const grant = await seedGrant(world);
  const result = await call(world, sessionPost({ capability: grant.capability }));
  const cookie = (result.headers["set-cookie"] || "").split(";")[0] || null;
  const snapshot = await call(world, edgeRequest(ORIGIN + "/api/snapshot", {
    method: "GET", headers: headersFor({ cookie }),
  }));
  return {
    status: result.status,
    sets_cookie: !!result.headers["set-cookie"],
    cache_control: result.headers["cache-control"],
    sessions: world.db.sessions.size,
    snapshot_status: snapshot.status,
    limiter_calls: world.limiter.calls.length,
    logs_leak_capability: leaks(world, grant.capability),
  };
};

/**
 * Over the limit, NOTHING behind the limiter runs.
 *
 * The body is a counting stream, so "the reader was not invoked" is measured
 * rather than asserted from reading the code; `statementLog` proves no D1
 * statement was prepared.
 */
scenarios.limited_request_does_no_work = async () => {
  const world = createWorld({ rateLimiter: new bindings.MemoryRateLimiter({ forceSuccess: false }) });
  const grant = await seedGrant(world);
  const statementsBefore = world.db.statementLog.length;
  const logsBefore = world.logs.length;

  /* If the limiter did not short-circuit, this read would block forever. */
  const counter = { pulls: 0, stalled: false, cancelled: false };
  const limited = await callWithin(world, streamRequest(stallingStream(counter), {}), 3000);

  /* A VALID capability must produce exactly the same answer as an unknown one:
   * the 429 may not become an oracle for whether a link exists. */
  const withValid = await call(world, sessionPost({ capability: grant.capability }));
  const withUnknown = await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }));
  const withNoCapability = await call(world, sessionPost({}));

  const emitted = events(world).slice(logsBefore);
  return {
    status: limited.status,
    /* 1 = the stream's own queue prefetch. Anything more means the Worker read. */
    body_pulls: counter.pulls,
    body_reader_blocked: counter.stalled,
    d1_statements: world.db.statementLog.length - statementsBefore,
    sessions: world.db.sessions.size,
    set_cookie: limited.headers["set-cookie"] || null,
    cache_control: limited.headers["cache-control"],
    retry_after: limited.headers["retry-after"],
    csp_present: !!limited.headers["content-security-policy"],
    nosniff: limited.headers["x-content-type-options"],
    body: limited.text,
    valid_status: withValid.status,
    unknown_status: withUnknown.status,
    no_capability_status: withNoCapability.status,
    /* Identical bytes, not merely identical status. */
    valid_matches_unknown: withValid.text === withUnknown.text
      && withValid.status === withUnknown.status,
    valid_sets_cookie: !!withValid.headers["set-cookie"],
    logs_leak_capability: leaks(world, grant.capability),
    /* Every emitted event, so the suite can prove none of them named an actor. */
    events: emitted,
    limiter_calls: world.limiter.calls.length,
  };
};

/* --------------------------------------------------------------- actor key -- */

/** One key per client address, deterministically, and never a shared bucket. */
scenarios.actor_key_is_per_client_and_never_logged = async () => {
  const world = createWorld();
  const grant = await seedGrant(world);

  await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }, { clientIp: CLIENT_IP }));
  await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }, { clientIp: CLIENT_IP }));
  await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }, { clientIp: OTHER_IP }));

  const calls = world.limiter.calls;
  return {
    call_count: calls.length,
    distinct_keys: new Set(calls).size,
    /* Same actor -> same key, twice. Different actor -> a different key. */
    repeat_is_stable: calls[0] === calls[1],
    other_actor_differs: calls[2] !== calls[0],
    key_shape_ok: calls.every((key) => key === "session:" + CLIENT_IP || key === "session:" + OTHER_IP),
    /* The address is personal data and must not reach a log line. */
    logs_leak_client_ip: leaks(world, CLIENT_IP) || leaks(world, OTHER_IP),
    logs_leak_key: leaks(world, "session:" + CLIENT_IP),
    logs_leak_capability: leaks(world, grant.capability),
  };
};

/** The pure key derivation, including the forms that must be refused. */
scenarios.client_address_parsing = async () => {
  const derive = (value) => {
    const headers = new Headers();
    if (value !== null) headers.set("CF-Connecting-IP", value);
    return rateLimitLib.clientActorAddress(edgeRequest(ORIGIN + "/api/session", {
      method: "POST", headers,
    }));
  };
  return {
    absent: derive(null),
    empty: derive(""),
    blank: derive("   "),
    ipv4: derive("203.0.113.10"),
    ipv4_padded: derive("  203.0.113.10  "),
    ipv4_leading_zero: derive("203.0.113.010"),
    ipv4_out_of_range: derive("203.0.113.999"),
    ipv4_short: derive("203.0.113"),
    ipv6: derive("2001:db8::1"),
    ipv6_uppercase: derive("2001:DB8::AB"),
    ipv6_mapped: derive("::ffff:203.0.113.10"),
    duplicated_header: derive("203.0.113.10, 198.51.100.4"),
    spaced_pair: derive("203.0.113.10 198.51.100.4"),
    hostname: derive("client.example.invalid"),
    overlong: derive("2001:db8:".repeat(12)),
    /* An honest key, and nothing else, is handed to the binding. */
    key_for_ipv4: rateLimitLib.sessionRateLimitKey("203.0.113.10"),
  };
};

/**
 * Fail-closed when the trusted metadata or the binding itself is missing.
 *
 * Each case must refuse BEFORE the body is read and BEFORE D1 is touched, and
 * none of them may be a 429 — that status is reserved for a real limit hit.
 */
scenarios.missing_actor_or_binding_fails_closed = async () => {
  const cases = {};

  const probe = async (label, world, clientIp) => {
    const statementsBefore = world.db.statementLog.length;
    const counter = { pulls: 0, stalled: false, cancelled: false };
    const result = await callWithin(world, streamRequest(stallingStream(counter), {
      clientIp,
    }), 3000);
    const event = lastEvent(world, "session_exchange_rejected");
    cases[label] = {
      status: result.status,
      body: result.text,
      body_pulls: counter.pulls,
      body_reader_blocked: counter.stalled,
      d1_statements: world.db.statementLog.length - statementsBefore,
      set_cookie: result.headers["set-cookie"] || null,
      cache_control: result.headers["cache-control"],
      level: event && event.level,
      reason: event && event.reason,
      /* No body-read evidence can exist: the reader never ran. */
      has_read_evidence: !!(event && Object.prototype.hasOwnProperty.call(event, "body_read_mode")),
    };
  };

  await probe("no_client_address", createWorld(), null);
  await probe("malformed_client_address", createWorld(), "not-an-address");
  await probe("duplicated_client_address", createWorld(), "203.0.113.10, 198.51.100.4");
  await probe("binding_absent", createWorld({ rateLimiter: null }), CLIENT_IP);
  await probe("binding_throws", createWorld({
    rateLimiter: new bindings.MemoryRateLimiter({ failWith: new Error("limiter down") }),
  }), CLIENT_IP);
  await probe("binding_answers_nonsense", createWorld({
    rateLimiter: new bindings.MemoryRateLimiter({ answerWith: { allowed: "yes" } }),
  }), CLIENT_IP);

  /* Every reason in this vocabulary must survive lib/log.js's secret scrubber:
   * a 24+ character opaque token is redacted, which would silently blind the
   * exact deployment failure these outcomes exist to report. */
  cases.reason_vocabulary = Object.values(rateLimitLib.RATE_LIMIT_OUTCOME).map((reason) => ({
    reason,
    survives_scrubber: logLib.scrubLogFields({ reason }).reason === reason,
  }));

  return cases;
};

/** The configured policy is the one the code and the config both state. */
scenarios.policy_is_sixty_per_sixty = async () => {
  const world = createWorld();
  await seedGrant(world);
  let allowed = 0;
  let refused = 0;
  for (let i = 0; i < 65; i += 1) {
    const result = await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }));
    if (result.status === 429) refused += 1; else allowed += 1;
  }
  /* A different actor is untouched by the first actor's exhaustion. */
  const otherActor = await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }, { clientIp: OTHER_IP }));
  /* And the window rolls. */
  world.clock.now += 60;
  const nextWindow = await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }));
  return {
    attempts: 65,
    allowed,
    refused,
    other_actor_status: otherActor.status,
    next_window_status: nextWindow.status,
    module_limit: rateLimitLib.SESSION_RATE_LIMIT_REQUESTS,
    module_period: rateLimitLib.SESSION_RATE_LIMIT_PERIOD_SECONDS,
    binding_name: rateLimitLib.SESSION_RATE_LIMIT_BINDING,
  };
};

/* ------------------------------------------------------- body observability -- */

/**
 * The three body-read outcomes, each as the structured event a deployed probe
 * would read back.
 *
 * `declared_too_large` is the one that must NOT claim a mode: no reader was
 * created, so naming one would be a claim about code that did not run.
 */
scenarios.body_read_mode_is_observable = async () => {
  const cases = {};

  /* Layer 1: refused from the declared size, before the body is touched. */
  {
    const world = createWorld();
    const counter = { pulled: 0, cancelled: false };
    const result = await call(world, streamRequest(countingStream(5 * 1024 * 1024, 4096, counter), {
      headers: { "Content-Length": String(5 * 1024 * 1024) },
    }));
    cases.declared_too_large = {
      status: result.status,
      bytes_pulled: counter.pulled,
      event: lastEvent(world, "session_exchange_rejected"),
    };
  }

  /* No declared size at all. Under the framing contract this is refused BEFORE
   * a reader exists, so the stream is never pulled from — the strongest form of
   * "the body was not read". */
  {
    const world = createWorld();
    /* The stalling stream is the strong form: its first chunk is prefetched by
     * `ReadableStream`'s own queue, but a second pull never resolves. A prompt
     * answer therefore proves the Worker never entered the reader, rather than
     * inferring it from a byte count the fixture's queueing could explain. */
    const counter = { pulls: 0, stalled: false, cancelled: false };
    const result = await callWithin(world, streamRequest(stallingStream(counter), {}), 3000);
    cases.undeclared_stream = {
      status: result.status,
      body_pulls: counter.pulls,
      body_reader_blocked: counter.stalled,
      event: lastEvent(world, "session_exchange_rejected"),
    };
  }

  /* A declaration this route accepts, over a stream that then delivers far
   * more. The reader DOES run here, and the independent byte ceiling is what
   * stops it: refused mid-stream and cancelled rather than drained. This is the
   * defence-in-depth layer that no longer depends on the reader being BYOB. */
  {
    const world = createWorld();
    const counter = { pulled: 0, cancelled: false };
    const result = await call(world, streamRequest(countingStream(5 * 1024 * 1024, 4096, counter), {
      headers: { "Content-Length": "512" },
    }));
    cases.declared_small_but_oversized_stream = {
      status: result.status,
      stream_cancelled: counter.cancelled,
      total_offered: 5 * 1024 * 1024,
      event: lastEvent(world, "session_exchange_rejected"),
    };
  }

  /* The success path a deployed probe uses: a small, well-formed body that
   * reads cleanly and is then refused at the authorization store. */
  {
    const world = createWorld();
    await seedGrant(world);
    const payload = JSON.stringify({ capability: UNKNOWN_CAPABILITY });
    const result = await call(world, sessionPost(payload));
    cases.read_then_unknown_capability = {
      status: result.status,
      payload_bytes: new TextEncoder().encode(payload).byteLength,
      event: lastEvent(world, "session_exchange_denied"),
      logs_leak_capability: leaks(world, UNKNOWN_CAPABILITY),
      log_lines: world.logs.length,
    };
  }

  /* A malformed capability that still read cleanly, and an unparseable body:
   * both must carry the same evidence, so the probe has more than one way to
   * establish the mode without ever presenting a real capability. */
  {
    const world = createWorld();
    await call(world, sessionPost({ capability: "short" }));
    cases.malformed_capability = { event: lastEvent(world, "session_exchange_rejected") };
  }
  {
    const world = createWorld();
    await call(world, sessionPost("{not json"));
    cases.unparseable_body = { event: lastEvent(world, "session_exchange_rejected") };
  }
  {
    const world = createWorld();
    await call(world, sessionPost([1, 2, 3]));
    cases.wrong_body_shape = { event: lastEvent(world, "session_exchange_rejected") };
  }

  cases.modes = bodyLib.BODY_READ_MODE;
  cases.byob_buffer_bytes = bodyLib.BYOB_BUFFER_BYTES;
  return cases;
};

/** `bodyReadEvidence` itself, over every mode the reader can report. */
scenarios.read_evidence_is_honest_about_every_mode = async () => {
  const of = (mode, bytesRead, largest) => bodyLib.bodyReadEvidence({
    mode, bytesRead, largestChunkBytes: largest,
  });
  return {
    declared: of(bodyLib.BODY_READ_MODE.DECLARED, 0, 0),
    byob: of(bodyLib.BODY_READ_MODE.BYOB, 61, 61),
    default: of(bodyLib.BODY_READ_MODE.DEFAULT, 61, 61),
    buffered: of(bodyLib.BODY_READ_MODE.BUFFERED, 61, 61),
    missing: bodyLib.bodyReadEvidence(null),
  };
};

/**
 * Nothing sensitive reaches a log on ANY of these paths.
 *
 * The whole log stream from a world that exercised the oversized, malformed,
 * unknown, limited and successful paths is searched for the capability, the
 * session id, the client address and the request body.
 */
scenarios.no_sensitive_value_reaches_a_log = async () => {
  const world = createWorld();
  const grant = await seedGrant(world);
  const marker = "MARKER_BODY_VALUE_THAT_MUST_NOT_BE_LOGGED";

  await call(world, sessionPost({ capability: UNKNOWN_CAPABILITY }));
  await call(world, sessionPost({ capability: "short", note: marker }));
  await call(world, sessionPost("{not json " + marker.slice(0, 20)));
  const established = await call(world, sessionPost({ capability: grant.capability }));
  const cookie = (established.headers["set-cookie"] || "").split(";")[0] || "";
  const sessionId = cookie.includes("=") ? cookie.slice(cookie.indexOf("=") + 1) : "";

  const limitedWorld = createWorld({
    rateLimiter: new bindings.MemoryRateLimiter({ forceSuccess: false }),
  });
  await call(limitedWorld, sessionPost({ capability: grant.capability }));

  const all = world.logs.concat(limitedWorld.logs).join("\n");
  return {
    log_lines: world.logs.length + limitedWorld.logs.length,
    leaks_capability: all.includes(grant.capability),
    leaks_unknown_capability: all.includes(UNKNOWN_CAPABILITY),
    leaks_session_id: sessionId.length > 0 && all.includes(sessionId),
    leaks_client_ip: all.includes(CLIENT_IP),
    leaks_body_marker: all.includes(marker),
    established_status: established.status,
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
