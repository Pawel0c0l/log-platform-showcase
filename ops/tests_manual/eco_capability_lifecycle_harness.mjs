/* Period-scoped capability lifetimes, overlapping historical links, and the
 * retirement of dead authorization state — against the REAL Worker.
 *
 * Every scenario drives `worker/index.js` over the in-memory D1/R2/ASSETS
 * bindings, so what is measured is the deployed code path rather than a
 * restatement of the policy table. No wrangler, no credentials, no remote
 * Cloudflare resource, no production data.
 *
 * No capability, session id or object key is printed: expiries, counts and
 * booleans are, and comparisons that need a secret are made here and reported
 * as a boolean.
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
const publisherAuth = await import(path.join(DELIVERY, "worker", "lib", "publisher_auth.js"));
const capabilityLib = await import(path.join(DELIVERY, "worker", "lib", "capability.js"));
const ttlLib = await import(path.join(DELIVERY, "worker", "lib", "capability_ttl.js"));
const { D1AuthorizationStore, classifyCapability } =
  await import(path.join(DELIVERY, "worker", "lib", "store.js"));

const ORIGIN = "https://dashboard.example.invalid";
const T0 = 1_800_000_000;
const PEPPER = "synthetic-lifecycle-pepper";
const PUBLISHER_TOKEN = capabilityLib.generateCapability();
const DAY = 86400;

/* Three DIFFERENT reports, so "which snapshot did this link resolve to?" is a
 * question the harness can actually answer rather than assume. */
const REPORT_W1 = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_acceptable.json"));
const REPORT_W2 = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "ranked_safe.json"));
const REPORT_M1 = await canonicalFixture(path.join(ASSET_ROOT, "fixtures", "monthly_31_days.json"));

function hex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(text) {
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))));
}

async function createWorld(options) {
  const settings = options || {};
  const db = new bindings.MemoryD1();
  const bucket = new bindings.MemoryR2();
  const logs = [];
  const clock = { now: settings.now === undefined ? T0 : settings.now };
  const env = bindings.createEnv({
    db, bucket,
    assets: bindings.createAssetsBinding(ASSET_ROOT),
    assetRoot: ASSET_ROOT,
    pepper: PEPPER,
    now: () => clock.now,
    logSink: { log: (l) => logs.push(l), error: (l) => logs.push(l) },
  });
  if (settings.publisherConfigured !== false) {
    env.PUBLISHER_KEY_DIGEST = await publisherAuth.publisherKeyDigest(PUBLISHER_TOKEN, PEPPER);
  }
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

/* `periodType: null` OMITS the header entirely; an array sends it twice. */
function publishRequest(options) {
  const settings = options;
  const pairs = [["Content-Type", "application/json"],
                 ["Authorization", "Publisher " + PUBLISHER_TOKEN],
                 ["X-Publication-Operation", settings.operationId],
                 ["X-Publication-Subject", settings.subjectRef],
                 ["X-Publication-Payload-Digest", settings.digest]];
  if (settings.periodType !== null && settings.periodType !== undefined) {
    for (const value of [].concat(settings.periodType)) pairs.push(["X-Publication-Period", value]);
  }
  return edgeRequest(ORIGIN + "/api/publish", {
    method: "POST", headers: new Headers(pairs), body: settings.body,
  });
}

async function publish(world, options) {
  const body = options.body;
  return call(world, publishRequest({
    ...options,
    digest: options.digest === undefined ? await sha256Hex(body) : options.digest,
  }));
}

function recoverRequest(options) {
  const pairs = [["Authorization", "Publisher " + PUBLISHER_TOKEN],
                 ["X-Publication-Operation", options.operationId]];
  if (options.periodType !== null && options.periodType !== undefined) {
    for (const value of [].concat(options.periodType)) pairs.push(["X-Publication-Period", value]);
  }
  return edgeRequest(ORIGIN + "/api/publish/recover", { method: "POST", headers: new Headers(pairs) });
}

/** One published reporting period: its grant, its expiry and its exact bytes. */
async function publishPeriod(world, { operationId, subjectRef, body, periodType }) {
  const response = await publish(world, { operationId, subjectRef, body, periodType });
  if (response.status !== 201) {
    throw new Error("publish failed: " + response.status + " " + response.text.slice(0, 120));
  }
  return {
    capability: response.json.capability,
    capability_id: response.json.capability_id,
    expires_at: response.json.expires_at,
    issued_at: world.clock.now,
    body,
  };
}

/** Exchange a bearer for a session cookie. */
async function exchange(world, capability) {
  const response = await call(world, edgeRequest(ORIGIN + "/api/session", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: ORIGIN,
      "CF-Connecting-IP": "203.0.113.7",
    },
    body: JSON.stringify({ capability }),
  }));
  const setCookie = response.headers["set-cookie"] || "";
  const cookie = setCookie ? setCookie.split(";")[0] : null;
  return { response, cookie };
}

/** What does this session actually see? Returns the served body text. */
async function readSnapshot(world, cookie) {
  return call(world, edgeRequest(ORIGIN + "/api/snapshot", {
    method: "GET", headers: { Origin: ORIGIN, Cookie: cookie },
  }));
}

const scenarios = {};

/* ------------------------------------------------- 1. the TTL contract ---- */

scenarios.period_ttl_is_exact = async () => {
  const world = await createWorld();
  const weekly = await publishPeriod(world, {
    operationId: "op-ttl-weekly-000001", subjectRef: "subject-w", body: REPORT_W1,
    periodType: "weekly",
  });
  const monthly = await publishPeriod(world, {
    operationId: "op-ttl-monthly-00001", subjectRef: "subject-m", body: REPORT_M1,
    periodType: "monthly",
  });
  return {
    weekly_lifetime_seconds: weekly.expires_at - weekly.issued_at,
    monthly_lifetime_seconds: monthly.expires_at - monthly.issued_at,
    weekly_days: (weekly.expires_at - weekly.issued_at) / DAY,
    monthly_days: (monthly.expires_at - monthly.issued_at) / DAY,
    /* The policy module's own table, so a drift between the table and what the
     * route actually stamps is visible as a disagreement here. */
    policy_weekly: ttlLib.CAPABILITY_TTL_SECONDS.weekly,
    policy_monthly: ttlLib.CAPABILITY_TTL_SECONDS.monthly,
    policy_is_frozen: Object.isFrozen(ttlLib.CAPABILITY_TTL_SECONDS),
  };
};

scenarios.unknown_period_fails_closed = async () => {
  const cases = {};
  const attempted = [
    ["missing", null],
    ["empty", ""],
    ["unknown_word", "quarterly"],
    ["wrong_case", "WEEKLY"],
    ["numeric", "10"],
    ["duplicated", ["weekly", "monthly"]],
    ["duplicated_same", ["weekly", "weekly"]],
  ];
  for (const [label, periodType] of attempted) {
    const world = await createWorld();
    const response = await publish(world, {
      operationId: "op-bad-" + label.padEnd(10, "x").slice(0, 10) + "-0001",
      subjectRef: "subject-bad", body: REPORT_W1, periodType,
    });
    cases[label] = {
      status: response.status,
      error: response.json && response.json.error,
      /* THE POINT: a refused publication creates NOTHING. */
      operations: world.db.publications.size,
      capabilities: world.db.capabilities.size,
      objects: world.bucket.objects.size,
    };
  }
  return cases;
};

scenarios.recovery_uses_the_same_policy = async () => {
  const out = {};
  for (const periodType of ["weekly", "monthly"]) {
    const world = await createWorld();
    const op = "op-recov-" + periodType.padEnd(9, "x").slice(0, 9) + "-001";
    const first = await publishPeriod(world, {
      operationId: op, subjectRef: "subject-" + periodType,
      body: periodType === "weekly" ? REPORT_W1 : REPORT_M1, periodType,
    });
    /* Time passes; the host lost the bearer and recovers it. */
    world.clock.now = T0 + 3 * DAY;
    const recovered = await call(world, recoverRequest({ operationId: op, periodType }));
    out[periodType] = {
      status: recovered.status,
      lifetime_seconds: recovered.json.expires_at - world.clock.now,
      /* The replacement is a FRESH full lifetime, not the remainder. */
      later_than_predecessor: recovered.json.expires_at > first.expires_at,
      predecessor_superseded: recovered.json.capability_id !== first.capability_id,
      bearer_differs: recovered.json.capability !== first.capability,
    };
  }
  const bad = await (async () => {
    const world = await createWorld();
    const op = "op-recov-badperiod-1";
    await publishPeriod(world, {
      operationId: op, subjectRef: "subject-bad", body: REPORT_W1, periodType: "weekly",
    });
    const before = world.db.capabilities.size;
    const response = await call(world, recoverRequest({ operationId: op, periodType: "quarterly" }));
    return {
      status: response.status,
      error: response.json && response.json.error,
      capabilities_unchanged: world.db.capabilities.size === before,
    };
  })();
  const missing = await (async () => {
    const world = await createWorld();
    const op = "op-recov-noperiod-01";
    await publishPeriod(world, {
      operationId: op, subjectRef: "subject-none", body: REPORT_W1, periodType: "weekly",
    });
    const before = world.db.capabilities.size;
    const response = await call(world, recoverRequest({ operationId: op, periodType: null }));
    return {
      status: response.status,
      capabilities_unchanged: world.db.capabilities.size === before,
    };
  })();
  return { ...out, unknown_period: bad, missing_period: missing };
};

/* ------------------------------------- 2. overlapping historical links ---- */

scenarios.consecutive_periods_overlap = async () => {
  const world = await createWorld();
  /* W1 on day 0. */
  const w1 = await publishPeriod(world, {
    operationId: "op-overlap-w1-00001", subjectRef: "subject-driver-1",
    body: REPORT_W1, periodType: "weekly",
  });
  /* W2 on day 7, while W1 still has three days left. */
  world.clock.now = T0 + 7 * DAY;
  const w2 = await publishPeriod(world, {
    operationId: "op-overlap-w2-00001", subjectRef: "subject-driver-1",
    body: REPORT_W2, periodType: "weekly",
  });
  /* M1 the same day. */
  const m1 = await publishPeriod(world, {
    operationId: "op-overlap-m1-00001", subjectRef: "subject-driver-1",
    body: REPORT_M1, periodType: "monthly",
  });

  const w1Row = await world.store.findCapabilityById(w1.capability_id);
  const w1State = classifyCapability(w1Row, world.clock.now);

  /* Each link is opened independently and asked what it shows.
   *
   * The Worker REBUILDS the response body from the validated document — it
   * never streams the stored octets, precisely so no R2 metadata can ride along
   * — so the served text is not byte-equal to what was published. What must
   * hold is that each link shows ITS OWN report and no other, which is decided
   * here by comparing the three served documents against each other and against
   * a re-read of the same link. */
  const seen = {};
  const served = {};
  for (const [label, grant] of [["w1", w1], ["w2", w2], ["m1", m1]]) {
    const { response, cookie } = await exchange(world, grant.capability);
    if (!cookie) {
      seen[label] = { exchanged: false, status: response.status };
      continue;
    }
    const snapshot = await readSnapshot(world, cookie);
    /* Re-open the SAME link through a second, independent session: a
     * snapshot-pinned grant answers identically every time. */
    const reopened = await exchange(world, grant.capability);
    const again = reopened.cookie ? await readSnapshot(world, reopened.cookie) : null;
    served[label] = snapshot.text;
    seen[label] = {
      exchanged: true,
      status: snapshot.status,
      stable_across_sessions: again !== null && again.text === snapshot.text,
    };
  }
  const bodies = ["w1", "w2", "m1"].map((k) => served[k]);
  const distinct_served_reports = new Set(bodies).size;

  return {
    w1_state_after_w2: w1State,
    w1_not_revoked: w1Row.revoked_at === null || w1Row.revoked_at === undefined,
    w1_not_rotated: w1Row.rotated_to === null || w1Row.rotated_to === undefined,
    w1_remaining_days: (Number(w1Row.expires_at) - world.clock.now) / DAY,
    w2_lifetime_days: (w2.expires_at - world.clock.now) / DAY,
    m1_lifetime_days: (m1.expires_at - world.clock.now) / DAY,
    distinct_capability_ids: new Set([w1.capability_id, w2.capability_id, m1.capability_id]).size,
    live_grants: world.db.capabilities.size,
    /* Three links, three different reports. If publishing W2 had re-pointed
     * W1 — the rejected rolling-URL model — this would be 2 or 1. */
    distinct_served_reports,
    seen,
  };
};

scenarios.w1_expires_on_its_own_schedule = async () => {
  const world = await createWorld();
  const w1 = await publishPeriod(world, {
    operationId: "op-expiry-w1-000001", subjectRef: "subject-driver-2",
    body: REPORT_W1, periodType: "weekly",
  });
  world.clock.now = T0 + 7 * DAY;
  const w2 = await publishPeriod(world, {
    operationId: "op-expiry-w2-000001", subjectRef: "subject-driver-2",
    body: REPORT_W2, periodType: "weekly",
  });

  /* Day 10 + 1 second: W1 is over, W2 has a week left. */
  world.clock.now = T0 + 10 * DAY + 1;
  const w1Attempt = await exchange(world, w1.capability);
  const w2Attempt = await exchange(world, w2.capability);
  const w1Row = await world.store.findCapabilityById(w1.capability_id);
  return {
    w1_exchange_status: w1Attempt.response.status,
    w1_sets_no_cookie: w1Attempt.cookie === null,
    w1_classification: classifyCapability(w1Row, world.clock.now),
    /* An EXPIRED link is not an unknown one: the row survives so the frontend
     * can say LINK_EXPIRED rather than "this never existed". */
    w1_row_retained: w1Row !== null,
    w1_row_holds_no_raw_bearer:
      !JSON.stringify(w1Row).includes(w1.capability),
    w2_exchange_status: w2Attempt.response.status,
    w2_still_works: w2Attempt.cookie !== null,
    sessions_after: world.db.sessions.size,
  };
};

/* -------------------------------------------------------- 3. sessions ----- */

scenarios.session_never_outlives_its_grant = async () => {
  const world = await createWorld();
  const grant = await publishPeriod(world, {
    operationId: "op-session-cap-00001", subjectRef: "subject-session",
    body: REPORT_W1, periodType: "weekly",
  });
  /* Ten minutes before the grant dies — less than the 30-minute session TTL. */
  const exchangedAt = grant.expires_at - 600;
  world.clock.now = exchangedAt;
  const { response, cookie } = await exchange(world, grant.capability);
  const session = [...world.db.sessions.values()][0];
  const maxAge = /Max-Age=(\d+)/.exec(response.headers["set-cookie"] || "");
  const sessionsBefore = world.db.sessions.size;

  /* And after expiry, no session may be established at all. */
  world.clock.now = grant.expires_at + 1;
  const afterExpiry = await exchange(world, grant.capability);
  const readWithOldCookie = cookie ? await readSnapshot(world, cookie) : null;

  return {
    exchanged_before_expiry: cookie !== null,
    session_expiry: session ? Number(session.expires_at) : null,
    grant_expiry: grant.expires_at,
    session_capped_at_grant: session ? Number(session.expires_at) <= grant.expires_at : null,
    cookie_max_age: maxAge ? Number(maxAge[1]) : null,
    cookie_max_age_within_grant: maxAge
      ? exchangedAt + Number(maxAge[1]) <= grant.expires_at : null,
    expired_exchange_status: afterExpiry.response.status,
    expired_exchange_sets_no_cookie: afterExpiry.cookie === null,
    /* No NEW session row: the one already there was issued while the grant was
     * alive, and the read below shows it no longer works. */
    no_new_session_after_expiry: world.db.sessions.size === sessionsBefore,
    /* A session issued while the grant was alive stops working with it. */
    stale_session_read_status: readWithOldCookie ? readWithOldCookie.status : null,
  };
};

/* -------------------------------------------- 4. authorization cleanup ---- */

scenarios.maintenance_compacts_only_dead_state = async () => {
  const world = await createWorld();
  const weekly = await publishPeriod(world, {
    operationId: "op-maint-weekly-0001", subjectRef: "subject-maint-w",
    body: REPORT_W1, periodType: "weekly",
  });
  const monthly = await publishPeriod(world, {
    operationId: "op-maint-monthly-001", subjectRef: "subject-maint-m",
    body: REPORT_M1, periodType: "monthly",
  });
  await exchange(world, weekly.capability);
  await exchange(world, monthly.capability);
  const sessionsBefore = world.db.sessions.size;
  const capabilitiesBefore = world.db.capabilities.size;

  const maintenance = (w) => call(w, edgeRequest(ORIGIN + "/api/publish/maintenance", {
    method: "POST", headers: { Authorization: "Publisher " + PUBLISHER_TOKEN },
  }));

  /* Nothing is expired yet: a sweep must remove nothing at all. */
  const early = await maintenance(world);
  const sessionsAfterEarly = world.db.sessions.size;

  /* An hour later both sessions are long dead; the weekly grant is not. */
  world.clock.now = T0 + 3600;
  const first = await maintenance(world);
  const second = await maintenance(world);

  /* Unauthenticated callers must not see the route at all. */
  const anonymous = await call(world, edgeRequest(ORIGIN + "/api/publish/maintenance", {
    method: "POST", headers: {},
  }));
  const wrongToken = await call(world, edgeRequest(ORIGIN + "/api/publish/maintenance", {
    method: "POST", headers: { Authorization: "Publisher " + capabilityLib.generateCapability() },
  }));
  const driverCookie = (await exchange(world, weekly.capability)).cookie;
  const withDriverSession = await call(world, edgeRequest(ORIGIN + "/api/publish/maintenance", {
    method: "POST", headers: { Cookie: driverCookie || "x=y" },
  }));
  const wrongMethod = await call(world, edgeRequest(ORIGIN + "/api/publish/maintenance", {
    method: "GET", headers: { Authorization: "Publisher " + PUBLISHER_TOKEN },
  }));

  /* The still-live grants must be usable afterwards, unchanged. */
  const weeklyStillWorks = (await exchange(world, weekly.capability)).cookie !== null;
  const monthlyStillWorks = (await exchange(world, monthly.capability)).cookie !== null;

  return {
    sessions_before: sessionsBefore,
    early_removed: early.json && early.json.expired_sessions_removed,
    sessions_unchanged_while_live: sessionsAfterEarly === sessionsBefore,
    first_status: first.status,
    first_removed: first.json && first.json.expired_sessions_removed,
    /* Idempotence: the second pass finds nothing left to do. */
    second_removed: second.json && second.json.expired_sessions_removed,
    batch_full: first.json && first.json.batch_full,
    capabilities_before: capabilitiesBefore,
    capabilities_after: world.db.capabilities.size,
    expired_capabilities_retained: first.json && first.json.expired_capabilities_retained,
    anonymous_status: anonymous.status,
    wrong_token_status: wrongToken.status,
    driver_session_status: withDriverSession.status,
    wrong_method_status: wrongMethod.status,
    weekly_still_works: weeklyStillWorks,
    monthly_still_works: monthlyStillWorks,
    response_names_no_secret: !first.text.includes(weekly.capability)
      && !first.text.includes(monthly.capability)
      && !first.text.includes(PUBLISHER_TOKEN),
  };
};

scenarios.expired_grants_survive_compaction = async () => {
  const world = await createWorld();
  const weekly = await publishPeriod(world, {
    operationId: "op-dead-weekly-00001", subjectRef: "subject-dead",
    body: REPORT_W1, periodType: "weekly",
  });
  await exchange(world, weekly.capability);

  /* Long past the grant's own expiry. */
  world.clock.now = T0 + 40 * DAY;
  const swept = await call(world, edgeRequest(ORIGIN + "/api/publish/maintenance", {
    method: "POST", headers: { Authorization: "Publisher " + PUBLISHER_TOKEN },
  }));
  const row = await world.store.findCapabilityById(weekly.capability_id);
  const attempt = await exchange(world, weekly.capability);
  return {
    status: swept.status,
    sessions_removed: swept.json.expired_sessions_removed,
    sessions_left: world.db.sessions.size,
    /* THE deliberate retention: the grant row is still there, still says
     * EXPIRED, and is still what makes an old link answer LINK_EXPIRED. */
    grant_row_retained: row !== null,
    grant_classification: classifyCapability(row, world.clock.now),
    expired_exchange_status: attempt.response.status,
    /* And the historical snapshot object is untouched by bearer expiry. */
    objects_retained: world.bucket.objects.size,
  };
};

/* ------------------------------------------------------------- run ------- */

const out = {};
for (const [name, scenario] of Object.entries(scenarios)) {
  try {
    out[name] = await scenario();
  } catch (error) {
    out[name] = { harness_error: String(error && error.message || error),
                  stack: String(error && error.stack || "").split("\n").slice(0, 4) };
  }
}
process.stdout.write(JSON.stringify(out, null, 2));
