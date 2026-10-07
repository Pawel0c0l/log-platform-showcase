/* Authorization store.
 *
 * STORAGE DECISION — D1 (SQLite), not KV.
 *
 * The deciding requirement is revocation. Workers KV is eventually consistent:
 * a revocation write can take up to ~60 s to be visible at every edge, so a
 * revoked link would keep working for a bounded but real window, and "revoke
 * this driver's link now" is exactly the operation this boundary exists to
 * support. D1 reads go to a single primary, so a revocation is visible on the
 * next request. D1 also gives one transactional batch for rotation (revoke the
 * old grant and insert its replacement together), which KV cannot express.
 *
 * Volume makes the choice cheap: the two audited clients have ~1 400 drivers,
 * so the authorization table is thousands of rows and single-digit reads per
 * dashboard open — far inside D1's free tier. Durable Objects would give the
 * same consistency at materially higher operational complexity for no benefit
 * at this size; R2 object metadata is not queryable and cannot express expiry
 * or revocation lookups at all.
 *
 * The interface below is the contract. `D1AuthorizationStore` is the production
 * implementation; `local/memory_bindings.js` provides an equivalent in-memory
 * implementation so tests never need a remote resource.
 */

export const CAPABILITY_STATE = {
  ACTIVE: "ACTIVE",
  UNKNOWN: "UNKNOWN",
  EXPIRED: "EXPIRED",
  REVOKED: "REVOKED",
};

/* Every column that grant-state interpretation reads, named explicitly.
 *
 * `rotated_to` belongs here: rotation distinguishes "already rotated" from
 * "independently revoked" by reading it, and a rotated predecessor is also
 * revoked. Omitting it from the projection made real D1 report ALREADY_ROTATED
 * retries as REVOKED — a defect the permissive in-memory double used to hide.
 * `SELECT *` is deliberately not used: an explicit list is what makes the test
 * double able to model the projection.
 */
export const CAPABILITY_COLUMNS = [
  "capability_id",
  "subject_ref",
  "snapshot_object_key",
  "issued_at",
  "expires_at",
  "revoked_at",
  "rotated_to",
  "session_epoch",
].join(", ");

/** D1 reports affected rows on `meta.changes`; tolerate a plain shape too. */
export function changedRows(result) {
  if (!result) return 0;
  if (result.meta && typeof result.meta.changes === "number") return result.meta.changes;
  if (typeof result.changes === "number") return result.changes;
  return 0;
}

export class D1AuthorizationStore {
  constructor(db) {
    this.db = db;
  }

  /** Look a capability up by digest. Returns null when nothing matches. */
  async findCapabilityByDigest(digest) {
    const row = await this.db
      .prepare(
        `SELECT ${CAPABILITY_COLUMNS}
           FROM eco_capability WHERE capability_digest = ?1`
      )
      .bind(digest)
      .first();
    return row || null;
  }

  async findCapabilityById(capabilityId) {
    const row = await this.db
      .prepare(
        `SELECT ${CAPABILITY_COLUMNS}
           FROM eco_capability WHERE capability_id = ?1`
      )
      .bind(capabilityId)
      .first();
    return row || null;
  }

  /* There is NO unconditional `insertCapability` here.
   *
   * Every grant this store creates is created by a statement whose INSERT is
   * conditional on the state transition that makes the grant authoritative —
   * `rotateCapability` below, and `PublicationStore`'s publication/recovery
   * transactions. Removing the unconditional helper is what makes "a live
   * grant with no ledger entry" unreachable from production code rather than
   * merely avoided by convention. The unconditional form lives in
   * `local/dev_grants.js`, which no module under `worker/` imports.
   */

  /**
   * Withdraw a grant. The `revoked_at IS NULL` guard makes this a
   * compare-and-set, so a retry or a concurrent caller cannot move an existing
   * revocation timestamp. Reports which call actually performed it.
   */
  async revokeCapability(capabilityId, revokedAt) {
    const result = await this.db
      .prepare(`UPDATE eco_capability SET revoked_at = ?2 WHERE capability_id = ?1 AND revoked_at IS NULL`)
      .bind(capabilityId, revokedAt)
      .run();
    const changed = changedRows(result);
    if (changed > 0) return { status: "REVOKED", changes: changed };
    const row = await this.findCapabilityById(capabilityId);
    return { status: row ? "ALREADY_REVOKED" : "UNKNOWN", changes: 0 };
  }

  /**
   * Atomic rotation: install the successor and withdraw the predecessor, or do
   * neither.
   *
   * Both statements run in one D1 batch, which is a transaction, and both are
   * guarded by the same eligibility predicate on the predecessor
   * (`revoked_at IS NULL AND rotated_to IS NULL`). The INSERT is an
   * `INSERT ... SELECT ... WHERE EXISTS`, so an ineligible predecessor inserts
   * zero rows rather than producing an orphan successor — which is exactly the
   * defect this shape exists to prevent. Batches are serialised against the
   * same primary, so a competing rotation finds `rotated_to` already set and
   * inserts nothing.
   *
   * Returns `{ status }`, one of ROTATED / ALREADY_ROTATED / REVOKED / UNKNOWN.
   */
  async rotateCapability(oldCapabilityId, record, rotatedAt) {
    const results = await this.db.batch([
      this.db
        .prepare(
          `INSERT INTO eco_capability
             (capability_id, capability_digest, subject_ref, snapshot_object_key,
              issued_at, expires_at, revoked_at, rotated_to, session_epoch)
           SELECT ?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, 0
            WHERE EXISTS (
              SELECT 1 FROM eco_capability
               WHERE capability_id = ?7
                 AND revoked_at IS NULL
                 AND rotated_to IS NULL
            )`
        )
        .bind(
          record.capability_id, record.capability_digest, record.subject_ref,
          record.snapshot_object_key, record.issued_at, record.expires_at,
          oldCapabilityId
        ),
      this.db
        .prepare(
          `UPDATE eco_capability
              SET revoked_at = ?2, rotated_to = ?3
            WHERE capability_id = ?1
              AND revoked_at IS NULL
              AND rotated_to IS NULL`
        )
        .bind(oldCapabilityId, rotatedAt, record.capability_id),
    ]);

    const inserted = changedRows(results[0]);
    const updated = changedRows(results[1]);
    if (inserted === 1 && updated === 1) return { status: "ROTATED" };

    /* Nothing was written. Report why, from the predecessor's current state. */
    const row = await this.findCapabilityById(oldCapabilityId);
    if (!row) return { status: "UNKNOWN" };
    if (row.rotated_to) return { status: "ALREADY_ROTATED", successor_capability_id: row.rotated_to };
    return { status: "REVOKED" };
  }

  /** Invalidate every session derived from a capability without revoking it. */
  async bumpSessionEpoch(capabilityId) {
    await this.db
      .prepare(`UPDATE eco_capability SET session_epoch = session_epoch + 1 WHERE capability_id = ?1`)
      .bind(capabilityId)
      .run();
  }

  async updateSnapshotObjectKey(capabilityId, objectKey) {
    await this.db
      .prepare(`UPDATE eco_capability SET snapshot_object_key = ?2 WHERE capability_id = ?1`)
      .bind(capabilityId, objectKey)
      .run();
  }

  async insertSession(record) {
    await this.db
      .prepare(
        `INSERT INTO eco_session (session_digest, capability_id, created_at, expires_at, epoch)
         VALUES (?1, ?2, ?3, ?4, ?5)`
      )
      .bind(record.session_digest, record.capability_id, record.created_at, record.expires_at, record.epoch)
      .run();
  }

  /**
   * Resolve a session digest to its grant. The join is deliberate: a session is
   * only ever as valid as the capability it came from, so revoking the
   * capability kills the session on the very next request.
   */
  async findSessionByDigest(digest) {
    const row = await this.db
      .prepare(
        `SELECT s.session_digest, s.capability_id, s.expires_at AS session_expires_at, s.epoch,
                c.subject_ref, c.snapshot_object_key, c.expires_at AS capability_expires_at,
                c.revoked_at, c.session_epoch
           FROM eco_session s
           JOIN eco_capability c ON c.capability_id = s.capability_id
          WHERE s.session_digest = ?1`
      )
      .bind(digest)
      .first();
    return row || null;
  }

  async deleteSession(digest) {
    await this.db.prepare(`DELETE FROM eco_session WHERE session_digest = ?1`).bind(digest).run();
  }

  /**
   * The publication ledger's authoritative payload digest for one object key.
   *
   * `uq_eco_publication_object_key` makes this at most one row, so the read is
   * a unique-index lookup, not a scan. It exists so the driver read path can
   * check the octets it just pulled from R2 against a value held in a
   * DIFFERENT store — R2 body and R2 metadata are written together, so
   * metadata alone cannot witness its own integrity.
   *
   * Returns `null` when no publication owns the key. That is not an error: a
   * grant issued outside the publication path (local scaffolding) has no
   * ledger row, and the caller is told which authorities actually applied.
   */
  async findPublicationDigestByObjectKey(objectKey) {
    const row = await this.db
      .prepare(
        `SELECT operation_id, payload_digest, state
           FROM eco_publication_operation WHERE snapshot_object_key = ?1`
      )
      .bind(objectKey)
      .first();
    return row || null;
  }

  /**
   * Compact expired browser sessions. BOUNDED, IDEMPOTENT, AND NOT A REVOCATION.
   *
   * WHAT IT IS FOR. A session row stops authorising anything the moment it
   * expires — `classifySession` refuses it, and it is additionally capped at
   * its grant's own expiry — so every row this removes is dead authorization
   * state that was otherwise kept forever. Nothing about which requests
   * succeed changes; what changes is that live authorization state stays
   * bounded by the validity windows that produced it instead of growing
   * monotonically with every dashboard open.
   *
   * WHY A LIMIT. D1 statements have a time budget, and an unbounded
   * `DELETE ... WHERE expires_at <= ?` over a table that has never been
   * compacted is exactly the statement that times out and therefore never
   * compacts anything. A bounded batch always makes progress, and the caller
   * repeats it — the operation is a pure function of the current time, so
   * repeating it, interrupting it or running two at once removes rows that
   * are already unusable and can never remove a live one.
   *
   * The subquery is what makes the bound possible: SQLite compiles
   * `DELETE ... LIMIT` only with the optional `SQLITE_ENABLE_UPDATE_DELETE_LIMIT`
   * build flag, which is not a guarantee this code may rely on, while
   * `DELETE ... WHERE pk IN (SELECT ... LIMIT ?)` is portable SQL.
   *
   * Returns the number of rows removed, so a caller can tell "nothing left to
   * do" from "batch full, call again".
   */
  async deleteExpiredSessions(now, limit) {
    const batch = Number.isInteger(limit) && limit > 0 ? limit : 1000;
    const result = await this.db
      .prepare(
        `DELETE FROM eco_session
           WHERE session_digest IN (
             SELECT session_digest FROM eco_session WHERE expires_at <= ?1 LIMIT ?2
           )`
      )
      .bind(now, batch)
      .run();
    return changedRows(result);
  }

  /**
   * Remove publication ledger rows past the HARD-RETENTION ceiling.
   *
   * WHY THIS ONE GOES FIRST. `eco_publication_operation.capability_id`
   * references `eco_capability`, so a grant cannot be removed while its
   * operation still names it. Child before parent is the only order that does
   * not depend on foreign keys being off.
   *
   * WHY `created_at` AND NOT THE GRANT'S EXPIRY. The operation is the record of
   * a publication having happened; the grant is one of its effects. Anchoring
   * the ledger on when it was created keeps its lifetime independent of whether
   * a recovery ever rotated its bearer.
   *
   * BOUNDED AND IDEMPOTENT for the same reason `deleteExpiredSessions` is: a
   * `DELETE ... WHERE pk IN (SELECT ... LIMIT ?)` always finishes inside D1's
   * statement budget, and the predicate is a pure function of the cutoff, so
   * repeating, interrupting or overlapping the call removes a subset of the
   * same already-dead set.
   */
  async deletePublicationsBefore(cutoffSeconds, limit) {
    const batch = Number.isInteger(limit) && limit > 0 ? limit : 500;
    const result = await this.db
      .prepare(
        `DELETE FROM eco_publication_operation
           WHERE operation_id IN (
             SELECT operation_id FROM eco_publication_operation
              WHERE created_at < ?1 LIMIT ?2
           )`
      )
      .bind(cutoffSeconds, batch)
      .run();
    return changedRows(result);
  }

  /**
   * Remove EXPIRED capability grants past the hard-retention ceiling.
   *
   * The tombstone is deliberate up to this point: an `eco_capability` row that
   * has expired keeps answering `410 LINK_EXPIRED` instead of leaving a driver
   * with a link indistinguishable from one that never existed. That answer is
   * worth keeping for the life of the e-mail it was sent in — it is not worth
   * keeping forever, and "forever" is what the schema previously said.
   *
   * FOUR PREDICATES, EACH LOAD-BEARING:
   *
   *   `issued_at < ?1` — THE retention anchor, and deliberately not
   *     `expires_at`. The ceiling limits how long a record may be STORED, and
   *     the row has existed since it was issued. Anchoring on expiry instead
   *     would keep a monthly grant for thirteen months plus sixty days, which
   *     is a lifetime longer than the ceiling by exactly the TTL — a breach
   *     that would have looked like a conservative choice.
   *
   *   `expires_at < ?2` (now) — never touch a LIVE grant. The ceiling is more
   *     than six times the longest TTL, so this can only ever be true of a
   *     grant that stopped working long ago; it is here so a clock skew, a
   *     hand-inserted row or a future longer TTL can never turn retention into
   *     revocation.
   *
   *   no session references it — `eco_session.capability_id` is a foreign key.
   *     Expired sessions are compacted by the same maintenance call moments
   *     earlier, so this is normally already true; when a batch limit left some
   *     behind, the grant simply waits for the next call instead of failing.
   *
   *   no publication references it — same argument, same table as above.
   */
  async deleteRetiredCapabilitiesBefore(cutoffSeconds, nowSeconds, limit) {
    const batch = Number.isInteger(limit) && limit > 0 ? limit : 500;
    const result = await this.db
      .prepare(
        `DELETE FROM eco_capability
           WHERE capability_id IN (
             SELECT c.capability_id FROM eco_capability c
              WHERE c.issued_at < ?1
                AND c.expires_at < ?2
                AND NOT EXISTS (
                      SELECT 1 FROM eco_session s
                       WHERE s.capability_id = c.capability_id)
                AND NOT EXISTS (
                      SELECT 1 FROM eco_publication_operation p
                       WHERE p.capability_id = c.capability_id)
              LIMIT ?3
           )`
      )
      .bind(cutoffSeconds, nowSeconds, batch)
      .run();
    return changedRows(result);
  }

  /** Oldest issue instant still present past the cutoff. The compliance number. */
  async oldestRetainedCapabilityIssue(cutoffSeconds) {
    const row = await this.db
      .prepare(
        `SELECT min(issued_at) AS oldest FROM eco_capability WHERE issued_at < ?1`
      )
      .bind(cutoffSeconds)
      .first();
    const value = row && row.oldest;
    return typeof value === "number" ? value : null;
  }
}

/**
 * Delete R2 snapshot objects uploaded before the hard-retention ceiling.
 *
 * WHY THE SNAPSHOT IS NOT COUPLED TO ITS GRANT. A weekly link lives 10 days and
 * a monthly one 60; the report itself is a driver's own historical record and
 * deliberately outlives the link that pointed at it. What changed is only the
 * end of that sentence: it outlives the link, not the platform.
 *
 * BOUNDED BY BOTH ENDS. `list()` is paged with an explicit `limit`, and the
 * function stops after `maxObjects` regardless of how many pages remain — a
 * bucket nobody has ever swept must not turn the first maintenance call into a
 * request that never returns. `truncated` in the result tells the caller
 * another call would do more work.
 *
 * IDEMPOTENT. Deleting an absent key is not an error in R2, and every decision
 * is a pure function of the object's own `uploaded` timestamp.
 *
 * ORDER RELATIVE TO D1. Runs AFTER the D1 rows are removed. Either order is
 * safe — a capability past the ceiling expired months ago and can no longer
 * resolve anything — but this way a failure part-way leaves orphaned objects,
 * which the next run collects by age, rather than live-looking rows pointing at
 * bytes that are gone.
 */
export async function deleteSnapshotsBefore(bucket, cutoffSeconds, options) {
  const settings = options || {};
  const pageSize = Number.isInteger(settings.pageSize) && settings.pageSize > 0
    ? settings.pageSize : 200;
  const maxObjects = Number.isInteger(settings.maxObjects) && settings.maxObjects > 0
    ? settings.maxObjects : 1000;
  const dryRun = settings.dryRun === true;

  let examined = 0;
  let deleted = 0;
  let cursor = undefined;
  let truncated = false;
  let oldestRetained = null;

  while (examined < maxObjects) {
    const page = await bucket.list({ limit: pageSize, cursor });
    const objects = (page && page.objects) || [];
    const expired = [];
    for (const object of objects) {
      examined += 1;
      const uploadedMs = object && object.uploaded
        ? new Date(object.uploaded).getTime() : NaN;
      if (!Number.isFinite(uploadedMs)) {
        /* An object whose age cannot be established is never deleted. Guessing
         * is the one thing a retention sweep must not do. */
        continue;
      }
      const uploadedSeconds = Math.floor(uploadedMs / 1000);
      if (uploadedSeconds >= cutoffSeconds) continue;
      if (dryRun) {
        if (oldestRetained === null || uploadedSeconds < oldestRetained) {
          oldestRetained = uploadedSeconds;
        }
        continue;
      }
      expired.push(object.key);
    }
    if (expired.length) {
      await bucket.delete(expired);
      deleted += expired.length;
    }
    if (!page || !page.truncated || !page.cursor) break;
    cursor = page.cursor;
    if (examined >= maxObjects) {
      truncated = true;
      break;
    }
  }
  return { examined, deleted, truncated, oldest_retained_seconds: oldestRetained };
}

/** Classify a capability row without revealing which reason to the caller. */
export function classifyCapability(row, nowSeconds) {
  if (!row) return CAPABILITY_STATE.UNKNOWN;
  if (row.revoked_at !== null && row.revoked_at !== undefined) return CAPABILITY_STATE.REVOKED;
  if (Number(row.expires_at) <= nowSeconds) return CAPABILITY_STATE.EXPIRED;
  return CAPABILITY_STATE.ACTIVE;
}

/** A session is valid only while its parent grant is active and same-epoch. */
export function classifySession(row, nowSeconds) {
  if (!row) return CAPABILITY_STATE.UNKNOWN;
  if (row.revoked_at !== null && row.revoked_at !== undefined) return CAPABILITY_STATE.REVOKED;
  if (Number(row.capability_expires_at) <= nowSeconds) return CAPABILITY_STATE.EXPIRED;
  if (Number(row.session_expires_at) <= nowSeconds) return CAPABILITY_STATE.EXPIRED;
  if (Number(row.epoch) !== Number(row.session_epoch)) return CAPABILITY_STATE.REVOKED;
  return CAPABILITY_STATE.ACTIVE;
}
