/* LOCAL ONLY — unconditional grant creation for verification scaffolding.
 *
 * WHY THIS FILE EXISTS SEPARATELY
 *
 * Creating a capability without a publication operation is precisely the shape
 * that let a live bearer exist while the publication ledger did not reference
 * it. Production code therefore has no such helper: on the publisher path a
 * grant is only ever created by the conditional INSERT inside
 * `PublicationStore`'s atomic transaction.
 *
 * Local browser verification and the delivery-boundary test suites still need
 * to fabricate a grant directly — they are exercising the *driver* half of the
 * boundary (session exchange, snapshot read, revocation, rotation) and must be
 * able to set up a world without driving a whole publication.
 *
 * Nothing under `worker/` imports this module, and a test asserts that.
 * It is never deployed: `wrangler.toml` builds from `worker/index.js`.
 */

import { capabilityDigest, generateCapability } from "../worker/lib/capability.js";
import { capabilityTtlSeconds, requireTtlSeconds } from "../worker/lib/capability_ttl.js";

function randomId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) out += bytes[i].toString(16).padStart(2, "0");
  return out;
}

/** Insert a capability row unconditionally. Local scaffolding only. */
async function insertCapability(db, record) {
  await db
    .prepare(
      `INSERT INTO eco_capability
         (capability_id, capability_digest, subject_ref, snapshot_object_key,
          issued_at, expires_at, revoked_at, rotated_to, session_epoch)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, 0)`
    )
    .bind(
      record.capability_id, record.capability_digest, record.subject_ref,
      record.snapshot_object_key, record.issued_at, record.expires_at
    )
    .run();
}

/**
 * Mint a grant for one subject and one snapshot object, with no publication
 * operation behind it.
 *
 * Returns `{ capability, capability_id, expires_at }`. Local use only.
 */
export async function issueDevCapability(services, params) {
  const now = params.now;
  /* Explicit lifetime, or a period type the real policy answers for. There is
   * no universal fallback here either: scaffolding that mints a grant with an
   * unexplained expiry is scaffolding that tests the wrong thing. */
  const ttl = params.ttl_seconds !== undefined
    ? requireTtlSeconds(params.ttl_seconds)
    : capabilityTtlSeconds(params.period_type);
  const raw = generateCapability();
  const record = {
    capability_id: randomId(),
    capability_digest: await capabilityDigest(raw, services.pepper),
    subject_ref: params.subject_ref,
    snapshot_object_key: params.snapshot_object_key,
    issued_at: now,
    expires_at: now + ttl,
  };
  await insertCapability(services.db, record);
  return { capability: raw, capability_id: record.capability_id, expires_at: record.expires_at };
}
