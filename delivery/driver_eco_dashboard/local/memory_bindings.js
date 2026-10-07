/* Local, in-memory Cloudflare bindings.
 *
 * Enough of D1, R2 and the static-asset binding to run the real Worker
 * deterministically with no remote resource, no wrangler login and no
 * credentials. Used by the security test harnesses and by local/serve.js.
 *
 * This is test/verification scaffolding. It is never deployed.
 *
 * THREE FIDELITY RULES THIS DOUBLE MUST OBEY
 *
 * 1. Model D1's PROJECTION. A row exposes only the columns the statement
 *    named. Returning the full stored row was how an omitted `rotated_to`
 *    stayed invisible in tests while breaking against the real database.
 *
 * 2. Model D1's CONSTRAINTS. The CHECK constraints and unique indexes in
 *    `schema/001_authorization.sql` are enforced here, so a test cannot pass
 *    against a state the real database would have refused to store. In
 *    particular: a grant-authoritative operation state without a
 *    `capability_id` is rejected here exactly as SQLite would reject it.
 *
 * 3. Never manufacture a guarantee production does not have. The stream
 *    fixtures in the body tests deliberately include a one-chunk multi-megabyte
 *    probe, because a fixture that only ever emits small chunks "proves" a
 *    per-read bound that the runtime does not actually provide.
 *
 * FORCED INTERLEAVINGS
 *
 * The Worker and its libraries contain no test hook. Deterministic races are
 * produced instead by pausing THIS double at named transition sites — see
 * `SITE` below — which is where the security-critical boundaries actually are.
 * `onSite` is awaited before and after each site, so a test can hold N callers
 * at exactly the same instruction and release them in a chosen order.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";

/* ------------------------------------------------------------------ D1 ---- */

/**
 * The transition sites a test may pause at or fail. These correspond 1:1 to
 * the security-critical boundaries named by the review.
 */
export const SITE = {
  OPERATION_CLAIM: "operation_claim",
  SNAPSHOT_WRITTEN: "snapshot_written",
  GRANT_TRANSACTION: "grant_transaction",
  RECOVERY_TRANSACTION: "recovery_transaction",
  DELIVERY_TRANSITION: "delivery_transition",
  OBJECT_PUT: "object_put",
  OBJECT_GET: "object_get",
  OTHER: "other",
};

/* A deliberately tiny SQL interpreter: it understands exactly the statements
 * the production stores issue, so the production code runs unmodified. */
class MemoryPreparedStatement {
  constructor(db, sql) {
    this.db = db;
    this.sql = sql.replace(/\s+/g, " ").trim();
    this.args = [];
  }

  bind(...args) {
    this.args = args;
    return this;
  }

  async first() {
    const rows = this.db._select(this.sql, this.args);
    return rows.length ? rows[0] : null;
  }

  async all() {
    return { results: this.db._select(this.sql, this.args) };
  }

  async run() {
    return this.db._runOne(this.sql, this.args);
  }

  /* D1 exposes affected rows on meta.changes; the emulator mirrors that. */
}

const GRANT_AUTHORITATIVE = ["GRANT_MINTED", "DELIVERY_INTENT_RECORDED", "DELIVERED"];
const PRE_GRANT = ["CREATED", "SNAPSHOT_WRITTEN"];

/* Row copy. Deliberately not object spread: the projection detector in
 * test_driver_eco_dashboard_prepublisher.py counts `{ ...row }` occurrences to
 * catch a double that returns whole rows instead of the named columns, and a
 * clone helper must not blunt that check. */
function cloneRow(row) {
  return Object.assign({}, row);
}

function withFields(row, patch) {
  return Object.assign({}, row, patch);
}

export class MemoryD1 {
  constructor() {
    this.capabilities = new Map();
    this.sessions = new Map();
    this.publications = new Map();
    this.statementLog = [];
    /* Forced-interleaving hook: async (site, phase, detail) => void. */
    this.onSite = null;
    /* Fault injection: site -> Error message, thrown before the site runs. */
    this.failAt = new Map();
  }

  prepare(sql) {
    this.statementLog.push(sql.replace(/\s+/g, " ").trim());
    return new MemoryPreparedStatement(this, sql);
  }

  /** Which security-critical transition this statement belongs to. */
  static siteOf(sql) {
    if (sql.startsWith("INSERT INTO eco_publication_operation")) return SITE.OPERATION_CLAIM;
    if (sql.startsWith("UPDATE eco_publication_operation")) {
      if (sql.includes("bearer_generation = bearer_generation + 1")) return SITE.RECOVERY_TRANSACTION;
      if (sql.includes("bearer_generation = 1")) return SITE.GRANT_TRANSACTION;
      if (sql.includes("SET state = ?5")) return SITE.SNAPSHOT_WRITTEN;
      if (sql.includes("SET state = ?3")) return SITE.DELIVERY_TRANSITION;
      return SITE.OTHER;
    }
    if (sql.startsWith("INSERT INTO eco_capability")) {
      /* Both publication-path grants guard on the operation row, and both now
       * compare `payload_digest` — recovery pins the object identity it just
       * proved into its own compare-and-set. The one thing only the INITIAL
       * grant asserts is that no capability is attached yet. */
      if (sql.includes("FROM eco_publication_operation")) {
        return sql.includes("capability_id IS NULL")
          ? SITE.GRANT_TRANSACTION
          : SITE.RECOVERY_TRANSACTION;
      }
      return SITE.OTHER;
    }
    return SITE.OTHER;
  }

  async _enter(site, detail) {
    const failure = this.failAt.get(site);
    if (failure) throw new Error(failure);
    if (this.onSite) await this.onSite(site, "before", detail || {});
  }

  async _leave(site, detail) {
    if (this.onSite) await this.onSite(site, "after", detail || {});
  }

  /** A single statement outside a batch. */
  async _runOne(sql, args) {
    const site = MemoryD1.siteOf(sql);
    await this._enter(site, { sql });
    const result = this._mutate(sql, args);
    await this._leave(site, { sql, changes: result.meta ? result.meta.changes : 0 });
    return result;
  }

  /**
   * D1 batches run in one implicit transaction. All-or-nothing is emulated by
   * snapshotting every table and restoring on throw — which is what makes a
   * constraint violation inside a publication transaction roll the whole
   * transition back, exactly as the real database would.
   */
  async batch(statements) {
    const site = statements.length ? MemoryD1.siteOf(statements[0].sql) : SITE.OTHER;
    await this._enter(site, { batch: statements.length });

    const capabilitySnapshot = new Map();
    for (const [key, row] of this.capabilities) capabilitySnapshot.set(key, cloneRow(row));
    const sessionSnapshot = new Map();
    for (const [key, row] of this.sessions) sessionSnapshot.set(key, cloneRow(row));
    const publicationSnapshot = new Map();
    for (const [key, row] of this.publications) publicationSnapshot.set(key, cloneRow(row));

    try {
      const out = [];
      for (const statement of statements) {
        /* Fault injection INSIDE a transaction: proves the rollback path, not
         * merely the "refused before it started" path. */
        if (this.failInBatchAfter !== undefined && out.length >= this.failInBatchAfter) {
          throw new Error("D1_TRANSACTION_ABORTED");
        }
        out.push(this._mutate(statement.sql, statement.args));
      }
      await this._leave(site, { batch: statements.length, changes: out.map((r) => r.meta.changes) });
      return out;
    } catch (error) {
      this.capabilities = capabilitySnapshot;
      this.sessions = sessionSnapshot;
      this.publications = publicationSnapshot;
      throw error;
    }
  }

  _project(sql, row) {
    if (!row) return null;
    const match = /^\s*SELECT\s+([\s\S]*?)\s+FROM\s/i.exec(sql);
    if (!match) throw new Error("MemoryD1: cannot parse projection: " + sql);
    const list = match[1].trim();
    if (list === "*") return { ...row };
    const projected = {};
    for (const rawColumn of list.split(",")) {
      const column = rawColumn.trim().split(/\s+AS\s+/i)[0].trim().replace(/^[a-z]+\./i, "");
      if (!Object.prototype.hasOwnProperty.call(row, column)) {
        throw new Error("MemoryD1: unknown column in projection: " + column);
      }
      projected[column] = row[column];
    }
    return projected;
  }

  _select(sql, args) {
    if (sql.includes("SELECT min(issued_at) AS oldest FROM eco_capability")) {
      const cutoff = args[0];
      let oldest = null;
      for (const row of this.capabilities.values()) {
        if (!(row.issued_at < cutoff)) continue;
        if (oldest === null || row.issued_at < oldest) oldest = row.issued_at;
      }
      return [{ oldest }];
    }
    if (sql.includes("FROM eco_capability WHERE capability_digest")) {
      for (const row of this.capabilities.values()) {
        if (row.capability_digest === args[0]) return [this._project(sql, row)];
      }
      return [];
    }
    if (sql.includes("FROM eco_capability WHERE capability_id")) {
      const row = this.capabilities.get(args[0]);
      return row ? [this._project(sql, row)] : [];
    }
    if (sql.includes("FROM eco_publication_operation WHERE operation_id")) {
      const row = this.publications.get(args[0]);
      return row ? [this._project(sql, row)] : [];
    }
    if (sql.includes("FROM eco_publication_operation WHERE snapshot_object_key")) {
      /* uq_eco_publication_object_key makes this at most one row. */
      for (const row of this.publications.values()) {
        if (row.snapshot_object_key === args[0]) return [this._project(sql, row)];
      }
      return [];
    }
    if (sql.includes("FROM eco_session s")) {
      const session = this.sessions.get(args[0]);
      if (!session) return [];
      const capability = this.capabilities.get(session.capability_id);
      if (!capability) return [];
      return [{
        session_digest: session.session_digest,
        capability_id: session.capability_id,
        session_expires_at: session.expires_at,
        epoch: session.epoch,
        subject_ref: capability.subject_ref,
        snapshot_object_key: capability.snapshot_object_key,
        capability_expires_at: capability.expires_at,
        revoked_at: capability.revoked_at,
        session_epoch: capability.session_epoch,
      }];
    }
    throw new Error("MemoryD1: unsupported SELECT: " + sql);
  }

  /* --- constraint enforcement, mirroring schema/001_authorization.sql ------ */

  _assertPublicationConstraints(row) {
    const grantState = GRANT_AUTHORITATIVE.indexOf(row.state) !== -1;
    const preGrant = PRE_GRANT.indexOf(row.state) !== -1;
    if (!grantState && !preGrant) {
      throw new Error("CHECK constraint failed: chk_eco_publication_state");
    }
    if (preGrant && row.capability_id !== null && row.capability_id !== undefined) {
      throw new Error("CHECK constraint failed: chk_eco_publication_grant_ledger");
    }
    if (grantState && (row.capability_id === null || row.capability_id === undefined)) {
      throw new Error("CHECK constraint failed: chk_eco_publication_grant_ledger");
    }
    const generation = Number(row.bearer_generation);
    if (row.capability_id === null || row.capability_id === undefined) {
      if (generation !== 0) throw new Error("CHECK constraint failed: chk_eco_publication_generation");
    } else if (generation < 1) {
      throw new Error("CHECK constraint failed: chk_eco_publication_generation");
    }
    if (typeof row.snapshot_object_key !== "string" || row.snapshot_object_key.length === 0) {
      throw new Error("NOT NULL constraint failed: eco_publication_operation.snapshot_object_key");
    }
    for (const other of this.publications.values()) {
      if (other.operation_id === row.operation_id) continue;
      if (other.snapshot_object_key === row.snapshot_object_key) {
        throw new Error("UNIQUE constraint failed: uq_eco_publication_object_key");
      }
      if (row.capability_id && other.capability_id === row.capability_id) {
        throw new Error("UNIQUE constraint failed: uq_eco_publication_capability");
      }
    }
  }

  _insertCapabilityRow(args) {
    for (const row of this.capabilities.values()) {
      if (row.capability_digest === args[1]) throw new Error("UNIQUE constraint failed: eco_capability.capability_digest");
    }
    if (this.capabilities.has(args[0])) {
      throw new Error("UNIQUE constraint failed: eco_capability.capability_id");
    }
    this.capabilities.set(args[0], {
      capability_id: args[0], capability_digest: args[1], subject_ref: args[2],
      snapshot_object_key: args[3], issued_at: args[4], expires_at: args[5],
      revoked_at: null, rotated_to: null, session_epoch: 0,
    });
  }

  _capabilityEligible(capabilityId) {
    const row = this.capabilities.get(capabilityId);
    return !!row && (row.revoked_at === null || row.revoked_at === undefined) && !row.rotated_to;
  }

  _mutate(sql, args) {
    if (this.failWrites) throw new Error("D1_UNAVAILABLE");

    /* --- eco_capability inserts, all three conditional forms -------------- */

    if (sql.startsWith("INSERT INTO eco_capability") &&
        sql.includes("FROM eco_publication_operation") &&
        sql.includes("capability_id IS NULL")) {
      /* Initial grant: only while the operation is exactly in the pre-grant
       * state for this subject, payload and owned object key. */
      const operation = this.publications.get(args[6]);
      const eligible = !!operation &&
        operation.state === args[10] &&
        (operation.capability_id === null || operation.capability_id === undefined) &&
        operation.subject_ref === args[7] &&
        operation.payload_digest === args[8] &&
        operation.snapshot_object_key === args[9];
      if (!eligible) return { success: true, meta: { changes: 0 } };
      this._insertCapabilityRow(args);
      return { success: true, meta: { changes: 1 } };
    }

    if (sql.startsWith("INSERT INTO eco_capability") && sql.includes("FROM eco_publication_operation")) {
      /* Recovery replacement: operation still names the predecessor, is not
       * DELIVERED, still carries the object identity the caller proved, and
       * the predecessor is itself still eligible. */
      const operation = this.publications.get(args[6]);
      const operationEligible = !!operation &&
        operation.subject_ref === args[7] &&
        operation.capability_id === args[8] &&
        [args[9], args[10]].indexOf(operation.state) !== -1 &&
        operation.payload_digest === args[11] &&
        operation.snapshot_object_key === args[12];
      if (!operationEligible || !this._capabilityEligible(args[8])) {
        return { success: true, meta: { changes: 0 } };
      }
      this._insertCapabilityRow(args);
      return { success: true, meta: { changes: 1 } };
    }

    if (sql.startsWith("INSERT INTO eco_capability") && sql.includes("WHERE EXISTS")) {
      /* Generic rotation: predecessor still eligible. */
      if (!this._capabilityEligible(args[6])) return { success: true, meta: { changes: 0 } };
      this._insertCapabilityRow(args);
      return { success: true, meta: { changes: 1 } };
    }

    if (sql.startsWith("INSERT INTO eco_capability")) {
      /* Unconditional insert — local/dev_grants.js only. */
      this._insertCapabilityRow(args);
      return { success: true, meta: { changes: 1 } };
    }

    /* --- eco_capability updates ------------------------------------------ */

    if (sql.startsWith("UPDATE eco_capability SET revoked_at = ?2, rotated_to = ?3")) {
      const eligible = this._capabilityEligible(args[0]);
      /* The recovery form additionally requires the replacement to exist, which
       * is what chains it to its own transaction's INSERT. */
      const chained = !sql.includes("EXISTS") || this.capabilities.has(args[2]);
      if (!eligible || !chained) return { success: true, meta: { changes: 0 } };
      const row = this.capabilities.get(args[0]);
      row.revoked_at = args[1];
      row.rotated_to = args[2];
      return { success: true, meta: { changes: 1 } };
    }
    if (sql.startsWith("UPDATE eco_capability SET revoked_at")) {
      const row = this.capabilities.get(args[0]);
      if (row && (row.revoked_at === null || row.revoked_at === undefined)) {
        row.revoked_at = args[1];
        return { success: true, meta: { changes: 1 } };
      }
      return { success: true, meta: { changes: 0 } };
    }
    if (sql.startsWith("UPDATE eco_capability SET session_epoch")) {
      const row = this.capabilities.get(args[0]);
      if (row) row.session_epoch = Number(row.session_epoch) + 1;
      return { success: true, meta: { changes: 1 } };
    }
    if (sql.startsWith("UPDATE eco_capability SET snapshot_object_key")) {
      const row = this.capabilities.get(args[0]);
      if (row) row.snapshot_object_key = args[1];
      return { success: true };
    }

    /* --- eco_publication_operation --------------------------------------- */

    if (sql.startsWith("INSERT INTO eco_publication_operation")) {
      /* Claim: NOT EXISTS makes a concurrent duplicate a no-op, and the object
       * key is claimed with the row rather than assigned later. */
      if (this.publications.has(args[0])) return { success: true, meta: { changes: 0 } };
      const row = {
        operation_id: args[0], subject_ref: args[1], payload_digest: args[2],
        snapshot_object_key: args[3], capability_id: null, bearer_generation: 0,
        state: args[4], created_at: args[5], updated_at: args[5],
      };
      this._assertPublicationConstraints(row);
      this.publications.set(args[0], row);
      return { success: true, meta: { changes: 1 } };
    }

    if (sql.startsWith("UPDATE eco_publication_operation") && sql.includes("SET state = ?5")) {
      /* CREATED -> SNAPSHOT_WRITTEN, guarded on the full operation identity. */
      const row = this.publications.get(args[0]);
      const eligible = !!row && row.state === args[3] && row.subject_ref === args[1] &&
        row.payload_digest === args[2] && row.snapshot_object_key === args[6];
      if (!eligible) return { success: true, meta: { changes: 0 } };
      const next = withFields(row, { state: args[4], updated_at: args[5] });
      this._assertPublicationConstraints(next);
      this.publications.set(args[0], next);
      return { success: true, meta: { changes: 1 } };
    }

    if (sql.startsWith("UPDATE eco_publication_operation") && sql.includes("bearer_generation = 1")) {
      /* The authoritative initial-grant ledger move. */
      const row = this.publications.get(args[0]);
      const eligible = !!row && row.state === args[4] &&
        (row.capability_id === null || row.capability_id === undefined) &&
        row.subject_ref === args[5] && row.payload_digest === args[6] &&
        row.snapshot_object_key === args[7] &&
        this.capabilities.has(args[2]);
      if (!eligible) return { success: true, meta: { changes: 0 } };
      const next = withFields(row, { state: args[1], capability_id: args[2],
                                    bearer_generation: 1, updated_at: args[3] });
      this._assertPublicationConstraints(next);
      this.publications.set(args[0], next);
      return { success: true, meta: { changes: 1 } };
    }

    if (sql.startsWith("UPDATE eco_publication_operation") &&
        sql.includes("bearer_generation = bearer_generation + 1")) {
      /* The authoritative recovery ledger move. */
      const row = this.publications.get(args[0]);
      const eligible = !!row && row.capability_id === args[3] && row.subject_ref === args[4] &&
        [args[5], args[6]].indexOf(row.state) !== -1 &&
        row.payload_digest === args[7] && row.snapshot_object_key === args[8] &&
        this.capabilities.has(args[1]);
      if (!eligible) return { success: true, meta: { changes: 0 } };
      const next = withFields(row, {
        capability_id: args[1],
        bearer_generation: Number(row.bearer_generation) + 1,
        updated_at: args[2],
      });
      this._assertPublicationConstraints(next);
      this.publications.set(args[0], next);
      return { success: true, meta: { changes: 1 } };
    }

    if (sql.startsWith("UPDATE eco_publication_operation") && sql.includes("SET state = ?3")) {
      /* Delivery phase, bound to the capability the host is delivering. */
      const row = this.publications.get(args[0]);
      const eligible = !!row && row.state === args[1] && row.capability_id === args[4];
      if (!eligible) return { success: true, meta: { changes: 0 } };
      const next = withFields(row, { state: args[2], updated_at: args[3] });
      this._assertPublicationConstraints(next);
      this.publications.set(args[0], next);
      return { success: true, meta: { changes: 1 } };
    }

    /* --- eco_session ----------------------------------------------------- */

    if (sql.startsWith("INSERT INTO eco_session")) {
      this.sessions.set(args[0], {
        session_digest: args[0], capability_id: args[1],
        created_at: args[2], expires_at: args[3], epoch: args[4],
      });
      return { success: true, meta: { changes: 1 } };
    }
    /* Bounded expired-session compaction, modelled exactly as the real
     * statement behaves: at most `?2` rows, chosen from the expired set only,
     * and the affected count reported so the caller can tell a full batch from
     * an empty one. Insertion order stands in for SQLite's unbounded-but-
     * deterministic scan order; the operation's contract does not depend on
     * WHICH expired rows a batch removes, only that it removes no live one. */
    if (sql.includes("DELETE FROM eco_session")
        && sql.includes("SELECT session_digest FROM eco_session WHERE expires_at")) {
      const [now, limit] = args;
      let removed = 0;
      for (const [key, row] of this.sessions) {
        if (removed >= limit) break;
        if (row.expires_at <= now) {
          this.sessions.delete(key);
          removed += 1;
        }
      }
      return { success: true, meta: { changes: removed } };
    }
    /* Hard-retention sweeps. Same shape as the session compaction above: a
     * bounded batch whose predicate is a pure function of the cutoff, and whose
     * FOREIGN-KEY guards are modelled rather than assumed — a double that let a
     * referenced grant be removed would hide exactly the ordering defect the
     * real database would raise. */
    if (sql.includes("DELETE FROM eco_publication_operation")
        && sql.includes("WHERE created_at <")) {
      const [cutoff, limit] = args;
      let removed = 0;
      for (const [key, row] of this.publications) {
        if (removed >= limit) break;
        if (row.created_at < cutoff) {
          this.publications.delete(key);
          removed += 1;
        }
      }
      return { success: true, meta: { changes: removed } };
    }
    if (sql.includes("DELETE FROM eco_capability")
        && sql.includes("WHERE c.issued_at <")) {
      const [cutoff, now, limit] = args;
      let removed = 0;
      for (const [key, row] of this.capabilities) {
        if (removed >= limit) break;
        if (!(row.issued_at < cutoff)) continue;
        /* A LIVE grant is never retention's business, whatever its age. */
        if (!(row.expires_at < now)) continue;
        const sessionReferences = [...this.sessions.values()]
          .some((session) => session.capability_id === key);
        if (sessionReferences) continue;
        const publicationReferences = [...this.publications.values()]
          .some((publication) => publication.capability_id === key);
        if (publicationReferences) continue;
        this.capabilities.delete(key);
        removed += 1;
      }
      return { success: true, meta: { changes: removed } };
    }
    if (sql.startsWith("DELETE FROM eco_session WHERE session_digest")) {
      /* Fault injection for the logout-failure contract. */
      if (this.failSessionDelete) throw new Error("D1_UNAVAILABLE");
      const existed = this.sessions.delete(args[0]);
      return { success: true, meta: { changes: existed ? 1 : 0 } };
    }
    throw new Error("MemoryD1: unsupported statement: " + sql);
  }
}

/* ------------------------------------------------------------------ R2 ---- */

/* A private bucket. Note what is NOT here: no public URL, no signed URL, no
 * list() reachable from the Worker — the Worker only ever calls get(), put()
 * and head(). */
/* Exactly the normalisation a real bucket applies: bytes stay bytes, a string
 * is UTF-8 encoded. Anything else throws rather than being stringified. */
function toStoredBytes(body) {
  if (body instanceof Uint8Array) return body.slice();
  if (body instanceof ArrayBuffer) return new Uint8Array(body.slice(0));
  if (ArrayBuffer.isView(body)) {
    return new Uint8Array(body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength));
  }
  if (typeof body === "string") return new TextEncoder().encode(body);
  throw new Error("MemoryR2: unsupported body type " + typeof body);
}

/* WHAT THIS DOUBLE MUST NOT NORMALISE
 *
 * The review found a publication retry that treated an unreadable object as an
 * absent one and overwrote it. A double that answers every unhappy path with
 * `null` cannot express that defect at all, so these five states are kept
 * strictly distinct here and each has its own injection point:
 *
 *   definitive absence   `get()` resolves to `null` — and ONLY when the key
 *                        genuinely is not in the map.
 *   present              `get()` resolves to a readable R2ObjectBody.
 *   get failure          `get()` REJECTS. Never `null`.
 *   body read failure    `get()` resolves, `arrayBuffer()`/`text()` REJECT —
 *                        exactly the shape of a truncated or failed stream.
 *   metadata failure     `get()` resolves and the `customMetadata` accessor
 *                        THROWS, which a plain data property cannot model.
 *   malformed result     `get()` RESOLVES a value that is not `null` and not a
 *                        well-formed object result: `undefined`, an ARRAY
 *                        (bare, or decorated with every property a real result
 *                        carries — `typeof [] === "object"`, so nothing else in
 *                        the Worker's shape test catches one), or an object
 *                        whose echoed `key` is missing, not a string, or names
 *                        a different object. The re-review found `undefined`
 *                        classified as absence and a key-less result classified
 *                        as a proven object, so this double must be able to
 *                        produce each shape EXACTLY as R2 would hand it over —
 *                        normalising any of them back into `null` or into the
 *                        healthy object would make both defects untestable.
 *
 * Metadata corruption and missing metadata are NOT failures of this kind: the
 * object is readable and its contents are wrong, so tests mutate the stored
 * `customMetadata` directly and the double reports it faithfully.
 */
export class MemoryR2 {
  constructor(nowSeconds) {
    this.objects = new Map();
    /* The clock every `put` stamps `uploaded` with. Seconds, to match the
     * Worker's own `nowSeconds(env)`. */
    this.now = Number.isFinite(nowSeconds) ? nowSeconds : Math.floor(Date.now() / 1000);
    this.failList = false;
    this.listLog = [];
    this.getLog = [];
    /* Every put, including redundant rewrites of the same owned key. The count
     * and the DISTINCT key count are different assertions and the tests make
     * that distinction explicitly. */
    this.putLog = [];
    this.onSite = null;
    this.failAt = new Map();
    /* Per-key failure injection. Keyed by object key so a test can break
     * exactly the object under test and leave the rest of the bucket healthy. */
    this.getFailures = new Map();
    this.bodyFailures = new Map();
    this.metadataFailures = new Map();
    /* Malformed RESULTS, as opposed to failures: `get()` resolves, and what it
     * resolves is a shape the Worker must refuse rather than interpret. */
    this.undefinedResults = new Set();
    this.arrayResults = new Map();
    this.keyOmissions = new Set();
    this.keyOverrides = new Map();
  }

  /** `get(key)` rejects. It must never be observed as an absence. */
  failGetFor(key, message) {
    this.getFailures.set(key, message || "R2_GET_FAILED");
    return this;
  }

  /** `get(key)` resolves; reading the body rejects. */
  failBodyReadFor(key, message) {
    this.bodyFailures.set(key, message || "R2_BODY_READ_FAILED");
    return this;
  }

  /** `get(key)` resolves; touching `customMetadata` throws. */
  failMetadataFor(key, message) {
    this.metadataFailures.set(key, message || "R2_METADATA_READ_FAILED");
    return this;
  }

  /* `get(key)` RESOLVES `undefined`. Not `null`, and not a throw: this is the
   * storage response R2 does not document at all, and the one the re-review
   * found being read as "the key does not exist". The object stays in the map,
   * so a caller that answers this with a write overwrites a real object. */
  resolveUndefinedFor(key) {
    this.undefinedResults.add(key);
    return this;
  }

  /* `get(key)` resolves a bare `[]`. */
  resolveBareArrayFor(key) {
    this.arrayResults.set(key, "bare");
    return this;
  }

  /* `get(key)` resolves an ARRAY carrying the real object's exact key, body
   * reader and custom metadata — everything the Worker reads from a result.
   * Deliberately NOT normalised into an ordinary object: the whole point is a
   * value that satisfies every content check and is still not an R2 object. */
  resolveDecoratedArrayFor(key) {
    this.arrayResults.set(key, "decorated");
    return this;
  }

  /* `get(key)` resolves a result with a readable body and correct metadata and
   * NO `key` property at all. The property is deleted, not set to `undefined`:
   * "the field is absent" and "the field is present and empty" are different
   * shapes and a validator may legitimately distinguish them. */
  omitEchoedKeyFor(key) {
    this.keyOmissions.add(key);
    return this;
  }

  /* `get(key)` resolves an otherwise healthy result whose echoed `key` is
   * exactly `value` — a non-string for the shape cases, or a different
   * well-formed key for the wrong-object case. */
  echoKeyAs(key, value) {
    this.keyOverrides.set(key, value);
    return this;
  }

  /** Clear every injected failure for a key, healthy object left intact. */
  healObject(key) {
    this.getFailures.delete(key);
    this.bodyFailures.delete(key);
    this.metadataFailures.delete(key);
    this.undefinedResults.delete(key);
    this.arrayResults.delete(key);
    this.keyOmissions.delete(key);
    this.keyOverrides.delete(key);
    return this;
  }

  /* Stores OCTETS, deliberately.
   *
   * The previous version coerced every body with `String(body)`, so a
   * `Uint8Array` became "123,34,115,…" and a byte-identity test could not even
   * be expressed. R2 stores bytes; so does this. `body` remains readable as a
   * decoded string for the scenarios that only care about the document. */
  async put(key, body, options) {
    const failure = this.failAt.get(SITE.OBJECT_PUT);
    if (failure) throw new Error(failure);
    if (this.onSite) await this.onSite(SITE.OBJECT_PUT, "before", { key });
    if (this.failPut) throw new Error("R2_UNAVAILABLE");
    const settings = options || {};
    this.putLog.push(key);
    this.objects.set(key, {
      bytes: toStoredBytes(body),
      customMetadata: settings.customMetadata || {},
      /* R2 stamps every object with an `uploaded` Date, and the retention sweep
       * reads exactly that field. A double that omitted it would let the sweep
       * pass its tests while treating every real object as unaged. `this.now`
       * is injectable so a test can age an object without waiting. */
      uploaded: new Date((settings.uploadedSeconds ?? this.now) * 1000),
      get body() { return new TextDecoder("utf-8").decode(this.bytes); },
    });
    if (this.onSite) await this.onSite(SITE.OBJECT_PUT, "after", { key });
    return { key };
  }

  async head(key) {
    if (this.failHead) throw new Error("R2_UNAVAILABLE");
    if (this.failAt.get(SITE.OBJECT_GET)) throw new Error(this.failAt.get(SITE.OBJECT_GET));
    if (this.getFailures.has(key)) throw new Error(this.getFailures.get(key));
    return this.objects.has(key) ? { key } : null;
  }

  async get(key) {
    this.getLog.push(key);
    /* A failing get THROWS. Returning `null` here would be the emulator
     * silently manufacturing "the object does not exist" out of "the storage
     * layer broke", which is the exact conflation under test. */
    if (this.failGet) throw new Error("R2_UNAVAILABLE");
    if (this.failAt.get(SITE.OBJECT_GET)) throw new Error(this.failAt.get(SITE.OBJECT_GET));
    if (this.getFailures.has(key)) throw new Error(this.getFailures.get(key));
    if (this.onSite) await this.onSite(SITE.OBJECT_GET, "before", { key });
    /* A malformed RESOLVED result. Deliberately checked BEFORE the map lookup:
     * the object exists, so nothing here is a legitimate absence, and the
     * double must hand back the malformed value verbatim. */
    if (this.undefinedResults.has(key)) return undefined;
    if (this.arrayResults.has(key)) {
      const stored = this.objects.get(key);
      if (this.arrayResults.get(key) === "bare" || !stored) return [];
      /* An Array instance, decorated with EXACTLY what a healthy result
       * carries. Nothing is faked: the body reader returns the real stored
       * octets and the metadata is the real stored metadata, so every
       * content-level check would pass. Only the container is wrong. */
      const decorated = [];
      decorated.key = key;
      decorated.arrayBuffer = async () => stored.bytes.slice().buffer;
      decorated.text = async () => new TextDecoder("utf-8").decode(stored.bytes);
      decorated.customMetadata = stored.customMetadata;
      return decorated;
    }
    /* THE one definitive absence. Nothing else in this method returns null. */
    if (!this.objects.has(key)) return null;
    const stored = this.objects.get(key);
    const bodyFailure = this.bodyFailures.get(key);
    const metadataFailure = this.metadataFailures.get(key);
    const object = {
      key,
      /* A fresh copy per read, exactly as a real R2 body would be: a caller
       * that mutated the returned view must not be able to mutate the bucket. */
      arrayBuffer: async () => {
        if (bodyFailure) throw new Error(bodyFailure);
        return stored.bytes.slice().buffer;
      },
      text: async () => {
        if (bodyFailure) throw new Error(bodyFailure);
        return new TextDecoder("utf-8").decode(stored.bytes);
      },
    };
    /* The echoed key, malformed on demand. `delete` and "assign a non-string"
     * are distinct shapes and both are reproduced exactly. */
    if (this.keyOmissions.has(key)) {
      delete object.key;
    } else if (this.keyOverrides.has(key)) {
      object.key = this.keyOverrides.get(key);
    }
    if (metadataFailure) {
      /* An accessor, not a data property: a metadata read that fails is a
       * throw at access time and a plain value could not represent it. */
      Object.defineProperty(object, "customMetadata", {
        get() { throw new Error(metadataFailure); },
        enumerable: true,
      });
    } else {
      object.customMetadata = stored.customMetadata;
    }
    return object;
  }

  /* WHY THE DOUBLE NOW HAS `list()`. The comment above still holds for every
   * REQUEST path: nothing a browser can reach lists this bucket. Retention is
   * the one publisher-authenticated maintenance operation that must enumerate
   * objects to find the ones past the ceiling, and a sweep that cannot be
   * tested against paging, a truncated page or a missing `uploaded` stamp is a
   * sweep whose bounds are unverified.
   *
   * Paging is modelled the way R2 models it: an opaque cursor, a `truncated`
   * flag, and a page no larger than `limit`. Insertion order stands in for R2's
   * lexicographic key order — the sweep's contract does not depend on WHICH
   * objects a page contains, only that every object is eventually seen and that
   * no page exceeds the limit. */
  async list(options) {
    if (this.failList) throw new Error("R2_LIST_FAILED");
    const settings = options || {};
    const limit = Number.isInteger(settings.limit) && settings.limit > 0
      ? settings.limit : 1000;
    const keys = [...this.objects.keys()];
    const start = settings.cursor ? Number(settings.cursor) : 0;
    const slice = keys.slice(start, start + limit);
    this.listLog.push({ limit, cursor: settings.cursor ?? null, returned: slice.length });
    const end = start + slice.length;
    return {
      objects: slice.map((key) => ({ key, uploaded: this.objects.get(key).uploaded })),
      truncated: end < keys.length,
      cursor: end < keys.length ? String(end) : undefined,
    };
  }

  /* R2 accepts a key or an array of keys, and deleting an absent key is not an
   * error. Both matter: the sweep deletes a whole page at once and reruns
   * freely. */
  async delete(key) {
    if (Array.isArray(key)) {
      for (const item of key) this.objects.delete(item);
      return;
    }
    this.objects.delete(key);
  }
}

/* -------------------------------------------------------------- ASSETS ---- */

export function createAssetsBinding(rootDir) {
  return {
    async fetch(request) {
      const url = new URL(request.url);
      const relative = url.pathname.replace(/^\/+/, "");
      /* Path containment: the Worker allowlist already gates this, but the
       * emulator must not become the weak link in a traversal test. */
      const resolved = path.resolve(rootDir, relative);
      if (!resolved.startsWith(path.resolve(rootDir) + path.sep)) {
        return new Response(null, { status: 404 });
      }
      try {
        const body = await readFile(resolved);
        return new Response(body, { status: 200 });
      } catch (error) {
        return new Response(null, { status: 404 });
      }
    },
  };
}

/* --------------------------------------------------------------- helper --- */

/* ---------------------------------------------------------- rate limiter --- */

/**
 * Stand-in for a Cloudflare Workers Rate Limiting binding.
 *
 * Deliberately a REAL limiter rather than an always-allow stub, and deliberately
 * supplied by `createEnv` by default. The Worker treats a missing binding as a
 * broken deployment and refuses the exchange, which is the whole point of the
 * production contract; if the local runtime and the harnesses relied on that
 * absence being tolerated, the fail-closed behaviour could not exist.
 *
 * Semantics match the documented `simple` policy: a fixed window per period,
 * counted per key, answering `{ success }`. Every key it is asked about is
 * recorded in `calls` so a test can prove which requests reached the limiter
 * and that distinct actors got distinct keys.
 */
export class MemoryRateLimiter {
  constructor(options) {
    const settings = options || {};
    this.limit_ = settings.limit === undefined ? 60 : settings.limit;
    this.period = settings.period === undefined ? 60 : settings.period;
    this.now = settings.now || (() => Math.floor(Date.now() / 1000));
    /* Every key passed to limit(), in order. Tests assert on this. */
    this.calls = [];
    this.counts = new Map();
    /* Test levers: force an answer, or make the binding throw. */
    this.forceSuccess = settings.forceSuccess;
    this.failWith = settings.failWith || null;
    this.answerWith = settings.answerWith;
  }

  async limit(options) {
    const key = options && options.key;
    this.calls.push(key);
    if (this.failWith) throw this.failWith;
    if (this.answerWith !== undefined) return this.answerWith;
    if (this.forceSuccess !== undefined) return { success: this.forceSuccess };
    const window = Math.floor(this.now() / this.period);
    const bucket = `${window}\u0000${key}`;
    const used = (this.counts.get(bucket) || 0) + 1;
    this.counts.set(bucket, used);
    return { success: used <= this.limit_ };
  }
}

export function createEnv(options) {
  const settings = options || {};
  return {
    AUTHORIZATION_DB: settings.db || new MemoryD1(),
    SNAPSHOTS: settings.bucket || new MemoryR2(),
    ASSETS: settings.assets || createAssetsBinding(settings.assetRoot || "."),
    CAPABILITY_PEPPER: settings.pepper,
    ALLOW_INSECURE_COOKIES: settings.allowInsecureCookies ? "1" : undefined,
    /* `rateLimiter: null` means "deliberately absent", which is how the
     * missing-binding production failure is exercised. Anything else gets a
     * working limiter, so no unrelated scenario silently loses the guard. */
    SESSION_RATE_LIMIT: settings.rateLimiter === null
      ? undefined
      : (settings.rateLimiter || new MemoryRateLimiter({ now: settings.now })),
    __now: settings.now,
    __logSink: settings.logSink,
  };
}

/* ------------------------------------------------------------- barriers --- */

/**
 * A deterministic rendezvous for forced interleavings.
 *
 * `gate(site, phase)` returns a promise that a caller blocks on. A test can
 * wait until exactly N callers are parked at a site and then release them in a
 * chosen order — which is what turns "we ran two promises and hoped" into a
 * reproducible race.
 */
export class Barrier {
  constructor() {
    this.parked = [];
    this.enabled = new Set();
    this.log = [];
  }

  /** Pause every caller reaching `site` at `phase` until released. */
  hold(site, phase) {
    this.enabled.add(`${site}:${phase || "before"}`);
    return this;
  }

  /** Attach to a MemoryD1 / MemoryR2 instance. */
  attach(binding) {
    binding.onSite = async (site, phase, detail) => {
      this.log.push(`${site}:${phase}`);
      if (!this.enabled.has(`${site}:${phase}`)) return;
      await new Promise((resolve) => {
        this.parked.push({ site, phase, resolve, detail });
      });
    };
    return binding;
  }

  count(site, phase) {
    return this.parked.filter((entry) => entry.site === site &&
      entry.phase === (phase || "before")).length;
  }

  /** Resolve once `count` callers are parked at the site. */
  async waitFor(site, count, phase) {
    for (let spin = 0; spin < 200000; spin += 1) {
      if (this.count(site, phase) >= count) return true;
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    throw new Error(`Barrier: only ${this.count(site, phase)} of ${count} reached ${site}`);
  }

  /** Release `count` parked callers (all of them by default), in arrival order. */
  release(site, count, phase) {
    const wanted = phase || "before";
    let released = 0;
    for (let index = 0; index < this.parked.length && (count === undefined || released < count); ) {
      const entry = this.parked[index];
      if (entry.site === site && entry.phase === wanted) {
        this.parked.splice(index, 1);
        entry.resolve();
        released += 1;
        continue;
      }
      index += 1;
    }
    return released;
  }

  /** Stop holding a site and release anything still parked there. */
  open(site, phase) {
    this.enabled.delete(`${site}:${phase || "before"}`);
    return this.release(site, undefined, phase);
  }

  releaseAll() {
    this.enabled.clear();
    const parked = this.parked.splice(0, this.parked.length);
    for (const entry of parked) entry.resolve();
    return parked.length;
  }
}
