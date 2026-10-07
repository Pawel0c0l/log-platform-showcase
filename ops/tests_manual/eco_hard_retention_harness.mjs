/* The 13-calendar-month hard-retention ceiling, against the REAL Worker.
 *
 * Every scenario drives `worker/index.js` over the in-memory D1/R2/ASSETS
 * bindings, so what is measured is the deployed maintenance route rather than a
 * restatement of the policy. No wrangler, no credentials, no remote Cloudflare
 * resource, no production data.
 *
 * The question this harness exists to answer is the one the previous contract
 * answered with "forever": an expired capability keeps its tombstone and its
 * historical snapshot, but for how long? Here the answer is measured at the
 * boundary — one second before the ceiling and one second after — rather than
 * asserted somewhere in the middle where any wrong horizon would also pass.
 *
 * No capability, session id or object key is printed.
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
const retentionLib = await import(path.join(DELIVERY, "worker", "lib", "retention_policy.js"));
const { D1AuthorizationStore, classifyCapability } =
  await import(path.join(DELIVERY, "worker", "lib", "store.js"));

const ORIGIN = "https://dashboard.example.invalid";
const PEPPER = "synthetic-retention-pepper";
const PUBLISHER_TOKEN = capabilityLib.generateCapability();
const DAY = 86400;

/* 2027-01-15T08:00:00Z. Publication time for everything below. */
const T0 = Date.UTC(2027, 0, 15, 8, 0, 0) / 1000;

/* THE ENFORCEMENT LEAD. Maintenance is weekly, so the sweep must delete
 * everything whose deadline falls before the next guaranteed call — otherwise a
 * grant published one minute after a sweep outlives its deadline by a week.
 * Mirrors the Worker's own constant; the Python driver pins both to the
 * registry. */
const LEAD = 7 * 86400;

/* The DEADLINE of something published at T0 is 13 calendar months later. The
 * sweep that collects it is therefore one LEAD earlier than that, and
 * eligibility is strict `<`, so at exactly this instant nothing is collected
 * yet. */
const DEADLINE = Date.UTC(2028, 1, 15, 8, 0, 0) / 1000;
const AT_CEILING = DEADLINE - LEAD;
const ONE_SECOND_BEFORE = AT_CEILING - 1;
const ONE_SECOND_AFTER = AT_CEILING + 1;

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
  const bucket = new bindings.MemoryR2(settings.now === undefined ? T0 : settings.now);
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
  env.PUBLISHER_KEY_DIGEST = await publisherAuth.publisherKeyDigest(PUBLISHER_TOKEN, PEPPER);
  const world = { db, bucket, env, logs, clock, store: new D1AuthorizationStore(db) };
  /* One clock, two bindings. Advancing only `clock` would leave every R2 object
   * stamped at the world's creation instant while D1 moved on — the exact
   * mismatch that would make an object-age test prove nothing. */
  world.advanceTo = (seconds) => { clock.now = seconds; bucket.now = seconds; };
  return world;
}

async function call(world, request) {
  const response = await worker.fetch(request, world.env, {});
  const text = await response.clone().text();
  let json = null;
  try { json = JSON.parse(text); } catch (error) { json = null; }
  return { status: response.status, text, json };
}

async function publishPeriod(world, { operationId, subjectRef, body, periodType }) {
  const headers = new Headers([
    ["Content-Type", "application/json"],
    ["Authorization", "Publisher " + PUBLISHER_TOKEN],
    ["X-Publication-Operation", operationId],
    ["X-Publication-Subject", subjectRef],
    ["X-Publication-Payload-Digest", await sha256Hex(body)],
    ["X-Publication-Period", periodType],
  ]);
  const response = await call(world, edgeRequest(ORIGIN + "/api/publish", {
    method: "POST", headers, body,
  }));
  if (response.status !== 201) {
    throw new Error("publish failed: " + response.status + " " + response.text.slice(0, 160));
  }
  return {
    capability: response.json.capability,
    capability_id: response.json.capability_id,
    expires_at: response.json.expires_at,
    issued_at: world.clock.now,
  };
}

async function maintain(world, options) {
  const settings = options || {};
  const headers = settings.anonymous
    ? {} : { Authorization: "Publisher " + PUBLISHER_TOKEN };
  return call(world, edgeRequest(ORIGIN + "/api/publish/maintenance", {
    method: "POST", headers,
  }));
}

function snapshot(world) {
  return {
    capabilities: world.db.capabilities.size,
    sessions: world.db.sessions.size,
    publications: world.db.publications.size,
    objects: world.bucket.objects.size,
  };
}

const scenarios = {};

/* -------------------------------------- 1. the ceiling is calendar-based -- */

scenarios.ceiling_is_thirteen_calendar_months = async () => {
  const vectors = {};
  for (const iso of [
    "2026-03-31T12:00:00Z", "2028-03-29T00:00:00Z", "2025-03-29T00:00:00Z",
    "2026-01-31T23:59:59Z", "2027-01-15T08:00:00Z",
  ]) {
    const seconds = Math.floor(Date.parse(iso) / 1000);
    vectors[iso] = new Date(
      retentionLib.hardRetentionCutoffSeconds(seconds) * 1000
    ).toISOString().replace(".000", "");
  }
  return {
    months: retentionLib.HARD_RETENTION_MONTHS,
    policy_id: retentionLib.HARD_RETENTION_POLICY_ID,
    vectors,
    /* A day approximation would land on a different instant for at least one of
     * the vectors above; stating the difference makes that explicit. */
    days_between_t0_and_deadline: (DEADLINE - T0) / DAY,
    enforcement_lead_seconds: retentionLib.HARD_RETENTION_ENFORCEMENT_LEAD_SECONDS,
    /* The two horizons, measured on the same instant, so their relationship is
     * evidence rather than assertion. A later cutoff collects MORE. */
    deadline_cutoff_at_t0: retentionLib.hardRetentionCutoffSeconds(T0),
    enforcement_cutoff_at_t0: retentionLib.enforcementCutoffSeconds(T0),
    enforcement_is_ahead_of_deadline:
      retentionLib.enforcementCutoffSeconds(T0)
        > retentionLib.hardRetentionCutoffSeconds(T0),
    /* A publication made one second after a sweep must not outlive its deadline
     * waiting for the next one. */
    published_just_after_a_sweep_is_collected_next_time:
      T0 < retentionLib.enforcementCutoffSeconds(DEADLINE - LEAD + 1),
    /* The shorter access lifetimes are untouched by any of this. */
    weekly_ttl_days: ttlLib.CAPABILITY_TTL_SECONDS.weekly / DAY,
    monthly_ttl_days: ttlLib.CAPABILITY_TTL_SECONDS.monthly / DAY,
  };
};

/* ------------------------- 2. the tombstone, and where it finally ends ----- */

scenarios.tombstone_survives_expiry_and_ends_at_the_ceiling = async () => {
  const world = await createWorld();
  const weekly = await publishPeriod(world, {
    operationId: "op-ceiling-weekly001", subjectRef: "subject-ceiling",
    body: REPORT_W1, periodType: "weekly",
  });

  /* Long past the 10-day grant, nowhere near the ceiling. */
  world.advanceTo(T0 + 40 * DAY);
  const early = await maintain(world);
  const afterEarly = snapshot(world);
  const rowEarly = await world.store.findCapabilityById(weekly.capability_id);

  /* One second before the ceiling: eligibility is strict, so still nothing. */
  world.advanceTo(ONE_SECOND_BEFORE);
  const justBefore = await maintain(world);
  const afterJustBefore = snapshot(world);

  /* One second after: grant, ledger row and snapshot object all go. */
  world.advanceTo(ONE_SECOND_AFTER);
  const justAfter = await maintain(world);
  const afterJustAfter = snapshot(world);
  const rowAfter = await world.store.findCapabilityById(weekly.capability_id);

  /* And again, on unchanged state. */
  const repeat = await maintain(world);

  return {
    early_status: early.status,
    early_classification: classifyCapability(rowEarly, T0 + 40 * DAY),
    after_early: afterEarly,
    early_removed: early.json.hard_retention,
    reports_both_horizons:
      early.json.hard_retention.deadline < early.json.hard_retention.cutoff,
    reported_lead_seconds: early.json.hard_retention.enforcement_lead_seconds,
    just_before_status: justBefore.status,
    after_just_before: afterJustBefore,
    just_before_removed: justBefore.json.hard_retention,
    just_after_status: justAfter.status,
    after_just_after: afterJustAfter,
    just_after_removed: justAfter.json.hard_retention,
    grant_row_gone: rowAfter === null,
    repeat_removed: repeat.json.hard_retention,
    repeat_status: repeat.status,
  };
};

/* --------------------------------- 3. younger state is never collected ---- */

scenarios.younger_state_is_untouched = async () => {
  const world = await createWorld();
  await publishPeriod(world, {
    operationId: "op-old-weekly-000001", subjectRef: "subject-old",
    body: REPORT_W1, periodType: "weekly",
  });

  /* A second publication made one day before the sweep. Its grant is LIVE. */
  world.advanceTo(ONE_SECOND_AFTER - DAY);
  const fresh = await publishPeriod(world, {
    operationId: "op-new-monthly-00001", subjectRef: "subject-new",
    body: REPORT_M1, periodType: "monthly",
  });

  world.advanceTo(ONE_SECOND_AFTER);
  const swept = await maintain(world);
  const rowFresh = await world.store.findCapabilityById(fresh.capability_id);

  return {
    status: swept.status,
    removed: swept.json.hard_retention,
    remaining: snapshot(world),
    fresh_grant_retained: rowFresh !== null,
    fresh_grant_classification: classifyCapability(rowFresh, ONE_SECOND_AFTER),
  };
};

/* ------------------------------- 4. foreign keys decide the order --------- */

scenarios.a_referenced_grant_is_never_orphaned = async () => {
  const world = await createWorld();
  const old = await publishPeriod(world, {
    operationId: "op-fk-weekly-000001", subjectRef: "subject-fk",
    body: REPORT_W2, periodType: "weekly",
  });

  /* Make the LEDGER row young while its grant stays old. This cannot arise
   * from the publish path — an operation and its grant are created together —
   * so it is crafted directly. It is the shape a partial sweep leaves behind,
   * and the point is that the next pass must refuse to strand the parent. */
  for (const row of world.db.publications.values()) {
    row.created_at = ONE_SECOND_AFTER;
  }

  world.advanceTo(ONE_SECOND_AFTER);
  const swept = await maintain(world);
  const stillThere = await world.store.findCapabilityById(old.capability_id);

  /* Now let the ledger row age out too, and sweep again. */
  for (const row of world.db.publications.values()) {
    row.created_at = T0;
  }
  const second = await maintain(world);
  const gone = await world.store.findCapabilityById(old.capability_id);

  return {
    first_removed: swept.json.hard_retention,
    grant_protected_by_reference: stillThere !== null,
    oldest_retained_issue_reported:
      swept.json.hard_retention.oldest_retained_issue !== null,
    second_removed: second.json.hard_retention,
    grant_removed_once_unreferenced: gone === null,
  };
};

/* ------------------------------- 5. R2 failure does not undo D1 work ------ */

scenarios.bucket_failure_is_reported_not_swallowed = async () => {
  const world = await createWorld();
  await publishPeriod(world, {
    operationId: "op-r2fail-weekly001", subjectRef: "subject-r2fail",
    body: REPORT_W1, periodType: "weekly",
  });
  world.advanceTo(ONE_SECOND_AFTER);
  world.bucket.failList = true;

  const swept = await maintain(world);
  const remaining = snapshot(world);

  /* Heal the bucket and sweep again: the orphaned object is collected by age. */
  world.bucket.failList = false;
  const recovered = await maintain(world);

  return {
    status: swept.status,
    removed: swept.json.hard_retention,
    snapshot_error_reported: swept.json.hard_retention.snapshot_error !== null,
    d1_work_committed: remaining.capabilities === 0 && remaining.publications === 0,
    objects_after_failure: remaining.objects,
    recovered_removed: recovered.json.hard_retention,
    objects_after_recovery: world.bucket.objects.size,
  };
};

/* ------------------------- 6. an object with no age is never deleted ------ */

scenarios.an_unageable_object_is_never_deleted = async () => {
  const world = await createWorld();
  await publishPeriod(world, {
    operationId: "op-noage-weekly-001", subjectRef: "subject-noage",
    body: REPORT_W1, periodType: "weekly",
  });
  /* Every stored object loses its upload stamp: the sweep cannot establish an
   * age, and guessing one is the single thing it must never do. */
  for (const object of world.bucket.objects.values()) {
    object.uploaded = undefined;
  }
  world.advanceTo(ONE_SECOND_AFTER);
  const swept = await maintain(world);
  return {
    status: swept.status,
    removed: swept.json.hard_retention,
    objects_retained: world.bucket.objects.size,
  };
};

/* --------------------------------------- 7. bounded, and it says so ------- */

scenarios.the_sweep_is_bounded_and_pages = async () => {
  const world = await createWorld();
  for (let index = 0; index < 7; index += 1) {
    await publishPeriod(world, {
      operationId: "op-bulk-weekly-" + String(index).padStart(4, "0") + "0",
      subjectRef: "subject-bulk-" + index,
      body: index % 2 ? REPORT_W1 : REPORT_W2,
      periodType: "weekly",
    });
  }
  const before = snapshot(world);
  world.advanceTo(ONE_SECOND_AFTER);
  const swept = await maintain(world);
  const after = snapshot(world);
  const again = await maintain(world);
  return {
    before,
    removed: swept.json.hard_retention,
    after,
    /* R2 `list()` is paged; the sweep must have asked for a bounded page rather
     * than "everything". */
    list_page_limits: world.bucket.listLog.map((entry) => entry.limit),
    repeat_removed: again.json.hard_retention,
    repeat_is_a_no_op:
      again.json.hard_retention.capabilities_removed === 0
      && again.json.hard_retention.publications_removed === 0
      && again.json.hard_retention.snapshots_removed === 0,
  };
};

/* ------------------------- 8. still not a public maintenance endpoint ----- */

scenarios.retention_requires_the_publisher_credential = async () => {
  const world = await createWorld();
  await publishPeriod(world, {
    operationId: "op-auth-weekly-0001", subjectRef: "subject-auth",
    body: REPORT_W1, periodType: "weekly",
  });
  world.advanceTo(ONE_SECOND_AFTER);
  const anonymous = await maintain(world, { anonymous: true });
  const stateAfterAnonymous = snapshot(world);
  const authorised = await maintain(world);
  return {
    anonymous_status: anonymous.status,
    anonymous_changed_nothing:
      stateAfterAnonymous.capabilities === 1
      && stateAfterAnonymous.publications === 1
      && stateAfterAnonymous.objects === 1,
    authorised_status: authorised.status,
    authorised_removed: authorised.json.hard_retention,
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
