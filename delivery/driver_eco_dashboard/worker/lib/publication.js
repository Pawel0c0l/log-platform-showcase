/* Publication operations — one atomic D1 contract.
 *
 * WHAT THE REVIEW FOUND, AND WHY THE SHAPE CHANGED
 *
 * The previous version treated the publication row as a progress log: it
 * inserted a grant, then updated the row to point at it, as two independent
 * authoritative writes. Two consequences, both confirmed by forced barriers:
 *
 *   * N concurrent publishes of the same operation all made progress, so N
 *     raw bearers, N R2 objects and N live grants existed for one operation;
 *   * a crash between the two writes left a live grant that the ledger did not
 *     reference, so a retry answered ALREADY_PUBLISHED while recovery answered
 *     NO_GRANT_YET and the live bearer was orphaned from the contract.
 *
 * The row is now the LOCK, not the log. Every security-relevant transition is
 * a compare-and-set on it, and the capability write that accompanies a
 * transition runs in the SAME D1 batch — which is one transaction — as the
 * transition. `moved` booleans are never consulted after the fact to decide
 * whether it was safe to have already written something.
 *
 * STATE MACHINE
 *
 *   CREATED ─▶ SNAPSHOT_WRITTEN ─▶ GRANT_MINTED ─▶ DELIVERY_INTENT_RECORDED ─▶ DELIVERED
 *
 * BYTE IDENTITY
 *
 * `payload_digest` is SHA-256 of the EXACT canonical host bytes, and those
 * exact bytes are what R2 stores. The ledger digest, the request body, the
 * stored object and the host's canonical output are one byte sequence, not
 * four equal documents. `params.body` therefore travels through this module
 * as octets and is never parsed or re-serialised on the way to `putObject`.
 *
 * OBJECT OWNERSHIP
 *
 * The object key is minted by the Worker and written INTO the creating INSERT.
 * An operation therefore owns exactly one key from the instant it exists, and
 * every caller that loses the creating INSERT discards its own candidate key
 * without ever touching R2. R2 and D1 share no transaction, which is precisely
 * why ownership is committed to D1 first: an interrupted R2 write leaves an
 * operation whose owned key is either absent or correct, never ambiguous, and
 * never a second object.
 *
 * OBJECT INSPECTION — WHY A BOOLEAN `present` WAS NOT ENOUGH
 *
 * The earlier contract answered "is something there?" with a boolean and
 * reported an unreadable object as `present: false`. Two confirmed defects
 * followed, both of which made a publication authoritative over an object it
 * could not vouch for:
 *
 *   * an R2 `get()` that THREW was answered with another `put()`, overwriting
 *     an object nobody had managed to read;
 *   * an object whose body still hashed correctly but whose digest metadata or
 *     subject binding had been corrupted or removed advanced to GRANT_MINTED,
 *     returned 201, minted one live grant — and the driver read then failed
 *     503 for the life of the link.
 *
 * `services.inspectObject` now returns one of ABSENT / PRESENT_VALID /
 * PRESENT_INVALID / UNREADABLE (see `publisher.js`), and this module treats
 * them as four different things:
 *
 *   ABSENT          the ONLY state that permits writing the owned object, and
 *                   only while the ledger still says CREATED.
 *   PRESENT_VALID   reuse it. Zero puts, zero new keys, exact bytes preserved,
 *                   and the existing idempotent state machine continues.
 *   PRESENT_INVALID fail closed: OBJECT_INTEGRITY_FAILURE. No overwrite, no
 *                   grant, no bearer, no ledger advancement.
 *   UNREADABLE      fail closed: OBJECT_UNREADABLE. Same zero-effect
 *                   guarantee. AN ERROR IS NEVER AN ABSENCE.
 *
 * Ordinary retry therefore never repairs anything. A corrupted object stays
 * corrupted and stays visible, which is a precondition for investigating it;
 * an explicit operational repair path, if one is ever wanted, is a separate
 * milestone and is deliberately not reachable from `/api/publish`.
 *
 * ONE OBJECT INTEGRITY GATE, FOR EVERY OPERATION THAT MOVES AUTHORIZATION
 *
 * The follow-up review found that the gate above was reachable from
 * `publishSnapshot` only. `recoverLostBearer` — which revokes the predecessor
 * grant, mints a replacement, advances the bearer generation and hands out a
 * NEW raw bearer — consulted D1 alone. Confirmed: with R2 unreadable, recovery
 * returned RECOVERED, generation went 1 → 2 and the predecessor was superseded
 * while the authoritative snapshot object could not be proven to exist, let
 * alone be correct.
 *
 * There is now exactly ONE object-inspection contract — `services.inspectObject`,
 * i.e. `inspectSnapshotObject` — and BOTH operations that mutate or advance
 * authorization state for an existing publication go through it. There is
 * deliberately no second, weaker integrity path for recovery.
 *
 *   publication retry  ABSENT permits the FIRST object write, and only while
 *                      the ledger still says CREATED. PRESENT_VALID is
 *                      reusable. PRESENT_INVALID / UNREADABLE fail closed.
 *   bearer recovery    ONLY PRESENT_VALID may proceed. ABSENT, PRESENT_INVALID
 *                      and UNREADABLE all fail closed with ZERO authorization
 *                      mutation — no replacement grant, no supersession, no
 *                      generation increment, no bearer, ledger untouched.
 *
 * ABSENT is a refusal for recovery, not an opportunity: an operation that
 * claims an authoritative published snapshot whose object is gone cannot issue
 * a replacement link to it. And storage that cannot be read is never a reason
 * to rotate authorization.
 *
 * RAW BEARERS
 *
 * A raw capability is generated once and never stored — only its digest is. A
 * plain `publish` retry therefore NEVER mints a second bearer: it reports the
 * authoritative state and tells the host what to do next. Recovering a lost
 * bearer is a separate, explicit transition that supersedes the unusable grant
 * and installs its replacement in one transaction, so the operation never has
 * two live grants at any instant.
 *
 * DELIVERY IS TERMINAL
 *
 * Once DELIVERED the bearer may already be in the driver's mailbox. The
 * refusal to recover from there is a predicate INSIDE the recovery
 * transaction, not a SELECT before it, so a recovery that was decided while
 * the operation was still recoverable cannot commit after delivery won.
 * Symmetrically, a delivery transition names the capability the host is
 * actually delivering, so a delivery decided on a superseded bearer is
 * refused rather than terminalising the wrong grant.
 *
 * No e-mail is sent here. `DELIVERY_INTENT_RECORDED` and `DELIVERED` are host
 * assertions about its own delivery pipeline, recorded so a retry can tell how
 * far the previous attempt got.
 */

import { capabilityDigest, generateCapability } from "./capability.js";
import { changedRows } from "./store.js";
import { OBJECT_STATE } from "./publisher.js";
import { requireTtlSeconds } from "./capability_ttl.js";

export const PUBLICATION_STATE = {
  CREATED: "CREATED",
  SNAPSHOT_WRITTEN: "SNAPSHOT_WRITTEN",
  GRANT_MINTED: "GRANT_MINTED",
  DELIVERY_INTENT_RECORDED: "DELIVERY_INTENT_RECORDED",
  DELIVERED: "DELIVERED",
};

/* States in which a grant is authoritative for the operation. */
const GRANT_AUTHORITATIVE_STATES = [
  PUBLICATION_STATE.GRANT_MINTED,
  PUBLICATION_STATE.DELIVERY_INTENT_RECORDED,
  PUBLICATION_STATE.DELIVERED,
];

/* States from which a lost bearer may still be recovered. DELIVERED is
 * deliberately absent: that is the whole point of the terminal rule. */
const RECOVERABLE_STATES = [
  PUBLICATION_STATE.GRANT_MINTED,
  PUBLICATION_STATE.DELIVERY_INTENT_RECORDED,
];

/**
 * Result vocabulary. Deliberately explicit: there is no value that means
 * "unclear, maybe just issue again".
 */
export const PUBLICATION_RESULT = {
  /* A raw bearer is in this response, exactly once, for this operation. */
  PUBLISHED: "PUBLISHED",
  /* The operation already holds an authoritative grant. No bearer is
   * replayable; the host uses its persisted one or recovers explicitly. */
  ALREADY_PUBLISHED: "ALREADY_PUBLISHED",
  /* Terminal. The link is with the driver; nothing further may be minted. */
  ALREADY_DELIVERED: "ALREADY_DELIVERED",
  /* The operation exists but has no grant yet; a plain retry converges. */
  IN_PROGRESS: "IN_PROGRESS",
  /* Same operation id, different subject or different payload. No mutation. */
  CONFLICT: "CONFLICT",
  UNKNOWN_OPERATION: "UNKNOWN_OPERATION",
  RECOVERED: "RECOVERED",
  NOT_RECOVERABLE: "NOT_RECOVERABLE",
  RECORDED: "RECORDED",
  ALREADY_RECORDED: "ALREADY_RECORDED",
  /* A delivery transition named a capability that is no longer the
   * authoritative one — the host must re-read and deliver the current bearer. */
  CAPABILITY_SUPERSEDED: "CAPABILITY_SUPERSEDED",
  /* The operation's owned R2 object EXISTS and at least one authoritative
   * invariant about it fails — its bytes against the ledger digest, its
   * `payload_digest` metadata against its own bytes, its digest algorithm, or
   * its subject binding. Nothing is overwritten and nothing is minted: a
   * silent repair here would let a corrupted or substituted object become
   * authoritative under a retry that looks routine. */
  OBJECT_INTEGRITY_FAILURE: "OBJECT_INTEGRITY_FAILURE",
  /* The owned object's state could not be DETERMINED — R2 threw, the body or
   * metadata could not be read, or the result could not be interpreted. This
   * is deliberately distinct from both absence and corruption: the previous
   * implementation collapsed it into absence and answered it with another
   * `put()` that overwrote an object nobody had been able to read. */
  OBJECT_UNREADABLE: "OBJECT_UNREADABLE",
};

/**
 * What the host must do next. The publish contract never leaves this implicit,
 * because "ambiguous" is exactly the state in which an operator re-issues a
 * link and fans out a second live capability.
 */
export const PUBLICATION_NEXT_ACTION = {
  PERSIST_BEARER: "PERSIST_BEARER",
  RETRY_PUBLISH: "RETRY_PUBLISH",
  USE_PERSISTED_BEARER_OR_RECOVER: "USE_PERSISTED_BEARER_OR_RECOVER",
  OPEN_NEW_OPERATION: "OPEN_NEW_OPERATION",
  /* Deliberately not "retry": an object that exists and disagrees with an
   * authority is an integrity incident, and the next step is investigation. */
  INVESTIGATE_OBJECT_INTEGRITY: "INVESTIGATE_OBJECT_INTEGRITY",
  /* Storage could not be read. Unlike the above this MAY be transient, so a
   * later retry is legitimate — but only a retry, never a repair, and the
   * operation state is unchanged in the meantime. */
  RETRY_AFTER_STORAGE_RECOVERS: "RETRY_AFTER_STORAGE_RECOVERS",
  NONE: "NONE",
};

/** Why a recovery was refused. Never a reason to retry blindly. */
export const RECOVERY_REFUSAL = {
  ALREADY_DELIVERED: "ALREADY_DELIVERED",
  NO_GRANT_YET: "NO_GRANT_YET",
  SUPERSEDED: "SUPERSEDED",
  GRANT_NOT_ELIGIBLE: "GRANT_NOT_ELIGIBLE",
};

/* The one reason string recovery adds to the shared inspection vocabulary: the
 * ledger asserts an authoritative published snapshot and R2 definitively does
 * not have it. For a publication retry at SNAPSHOT_WRITTEN this same condition
 * is `OBJECT_VANISHED_AFTER_SNAPSHOT_WRITTEN`; naming it separately here keeps
 * the Worker's own log able to say which operation was refused and why. */
export const RECOVERY_OBJECT_ABSENT_REASON = "OBJECT_ABSENT_UNDER_AUTHORITATIVE_GRANT";

/* Ordering used to refuse backwards transitions. */
const STATE_ORDER = [
  PUBLICATION_STATE.CREATED,
  PUBLICATION_STATE.SNAPSHOT_WRITTEN,
  PUBLICATION_STATE.GRANT_MINTED,
  PUBLICATION_STATE.DELIVERY_INTENT_RECORDED,
  PUBLICATION_STATE.DELIVERED,
];

export const OPERATION_ID_PATTERN = /^[A-Za-z0-9_-]{16,64}$/;

export function isWellFormedOperationId(value) {
  return typeof value === "string" && OPERATION_ID_PATTERN.test(value);
}

const OPERATION_COLUMNS = [
  "operation_id",
  "subject_ref",
  "payload_digest",
  "snapshot_object_key",
  "capability_id",
  "bearer_generation",
  "state",
  "created_at",
  "updated_at",
].join(", ");

/* A publish call drives the state machine forward; each step is one CAS, so a
 * caller that keeps losing races is a caller whose work is already being done
 * by someone else. The bound exists so a pathological interleaving terminates
 * with a truthful answer instead of spinning. */
const MAX_PUBLISH_STEPS = 8;

export class PublicationStore {
  constructor(db) {
    this.db = db;
  }

  async findOperation(operationId) {
    const row = await this.db
      .prepare(`SELECT ${OPERATION_COLUMNS} FROM eco_publication_operation WHERE operation_id = ?1`)
      .bind(operationId)
      .first();
    return row || null;
  }

  /**
   * Claim the operation AND its object key in one conditional insert.
   *
   * The key is part of the claim, not a later update: whoever loses this
   * statement never owned a key and therefore never writes to R2. Reports
   * whether THIS caller is the one that created the operation.
   */
  async claimOperation(record) {
    const result = await this.db
      .prepare(
        `INSERT INTO eco_publication_operation
           (operation_id, subject_ref, payload_digest, snapshot_object_key,
            capability_id, bearer_generation, state, created_at, updated_at)
         SELECT ?1, ?2, ?3, ?4, NULL, 0, ?5, ?6, ?6
          WHERE NOT EXISTS (
            SELECT 1 FROM eco_publication_operation WHERE operation_id = ?1
          )`
      )
      .bind(record.operation_id, record.subject_ref, record.payload_digest,
            record.snapshot_object_key, PUBLICATION_STATE.CREATED, record.now)
      .run();
    return changedRows(result) === 1;
  }

  /**
   * CREATED -> SNAPSHOT_WRITTEN.
   *
   * Guarded on the full identity of the operation, so a transition can never
   * be applied to a row that has since been reused for different content. The
   * object key is compared, never written: ownership was settled at creation.
   */
  async markSnapshotWritten(params) {
    const result = await this.db
      .prepare(
        `UPDATE eco_publication_operation
            SET state = ?5, updated_at = ?6
          WHERE operation_id = ?1
            AND state = ?4
            AND subject_ref = ?2
            AND payload_digest = ?3
            AND snapshot_object_key = ?7`
      )
      .bind(params.operation_id, params.subject_ref, params.payload_digest,
            PUBLICATION_STATE.CREATED, PUBLICATION_STATE.SNAPSHOT_WRITTEN,
            params.now, params.snapshot_object_key)
      .run();
    return changedRows(result) === 1;
  }

  /**
   * THE authoritative initial-grant transition.
   *
   * One D1 batch = one transaction. The capability row and the ledger
   * transition that makes it authoritative commit together or not at all.
   *
   * Statement 1 inserts the capability only while the operation is exactly in
   * the pre-grant state, for this subject, this payload and this owned object
   * key, with no capability already attached. Statement 2 performs the same
   * compare-and-set on the ledger and additionally requires that the
   * capability now exists — which, inside this transaction, is true if and
   * only if statement 1 fired. So the reachable outcomes are (1,1) and (0,0);
   * a half-applied grant is not expressible.
   *
   * Exactly one concurrent caller can observe (1,1). Every other caller writes
   * nothing and mints nothing.
   */
  async mintGrantTransactionally(params) {
    /* Every value in this guard is bound, including the expected state. No
     * part of any statement in this module is built by string interpolation. */
    const guard =
      `SELECT 1 FROM eco_publication_operation
        WHERE operation_id = ?7
          AND state = ?11
          AND capability_id IS NULL
          AND subject_ref = ?8
          AND payload_digest = ?9
          AND snapshot_object_key = ?10`;

    const results = await this.db.batch([
      this.db
        .prepare(
          `INSERT INTO eco_capability
             (capability_id, capability_digest, subject_ref, snapshot_object_key,
              issued_at, expires_at, revoked_at, rotated_to, session_epoch)
           SELECT ?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, 0
            WHERE EXISTS (${guard})`
        )
        .bind(
          params.record.capability_id, params.record.capability_digest,
          params.record.subject_ref, params.record.snapshot_object_key,
          params.record.issued_at, params.record.expires_at,
          params.operation_id, params.subject_ref, params.payload_digest,
          params.record.snapshot_object_key, PUBLICATION_STATE.SNAPSHOT_WRITTEN
        ),
      this.db
        .prepare(
          `UPDATE eco_publication_operation
              SET state = ?2,
                  capability_id = ?3,
                  bearer_generation = 1,
                  updated_at = ?4
            WHERE operation_id = ?1
              AND state = ?5
              AND capability_id IS NULL
              AND subject_ref = ?6
              AND payload_digest = ?7
              AND snapshot_object_key = ?8
              AND EXISTS (SELECT 1 FROM eco_capability WHERE capability_id = ?3)`
        )
        .bind(
          params.operation_id, PUBLICATION_STATE.GRANT_MINTED,
          params.record.capability_id, params.now,
          PUBLICATION_STATE.SNAPSHOT_WRITTEN, params.subject_ref,
          params.payload_digest, params.record.snapshot_object_key
        ),
    ]);

    const inserted = changedRows(results[0]);
    const updated = changedRows(results[1]);
    if (inserted === 1 && updated === 1) return true;
    /* The only other reachable pair is (0,0). Anything else would mean the
     * batch was not a transaction, which is a platform contract violation
     * rather than an application state to accommodate. */
    if (inserted !== 0 || updated !== 0) {
      throw new Error("PUBLICATION_GRANT_NOT_ATOMIC");
    }
    return false;
  }

  /**
   * THE authoritative bearer-recovery transition.
   *
   * One D1 batch = one transaction, three statements, one shared guard:
   *
   *   1. insert the replacement grant, only while the operation still
   *      references `predecessor_capability_id`, is in a recoverable (i.e. not
   *      DELIVERED) state, and the predecessor is itself still eligible;
   *   2. supersede the predecessor, only if the replacement now exists;
   *   3. move the ledger onto the replacement and count the generation, only
   *      if the replacement now exists.
   *
   * Statements 2 and 3 are chained to statement 1 by an existence test on the
   * replacement id, so the reachable outcomes are (1,1,1) and (0,0,0). In
   * particular the case the review found — predecessor independently revoked,
   * operation still eligible — now writes nothing at all instead of pointing
   * the ledger at a grant that was never created.
   *
   * The DELIVERED refusal is a predicate INSIDE this transaction. A recovery
   * that read a recoverable state and then lost to delivery commits nothing.
   *
   * THE INSPECTED IDENTITY IS PART OF THE COMPARE-AND-SET.
   *
   * `recoverLostBearer` proves the operation's owned object PRESENT_VALID
   * against two ledger columns — `snapshot_object_key` and `payload_digest` —
   * before calling this. Both are therefore bound into the guards below, so
   * the authorization mutation can only apply to the very row identity that
   * was proven. Those two columns are immutable for the life of an operation
   * (they are written by the creating INSERT and by no UPDATE in this module),
   * so this predicate cannot refuse a recovery that ought to succeed; it
   * exists so that the immutability is CHECKED rather than assumed, and so a
   * future path that did mutate them would break a recovery instead of
   * silently rotating a bearer over an object nobody proved.
   *
   * R2 and D1 share no transaction and no mechanism here claims otherwise. The
   * contract is narrower and achievable: the object was proven valid, and the
   * D1 mutation that follows is pinned to the identity under which it was
   * proven. An object corrupted after the proof is indistinguishable from one
   * corrupted immediately after commit, and is caught by the read-time object
   * integrity checks the driver path already performs.
   */
  async recoverGrantTransactionally(params) {
    /* Two recoverable states, both bound rather than interpolated. */
    const [recoverableA, recoverableB] = RECOVERABLE_STATES;
    const operationGuard =
      `SELECT 1 FROM eco_publication_operation
        WHERE operation_id = ?7
          AND subject_ref = ?8
          AND capability_id = ?9
          AND state IN (?10, ?11)
          AND payload_digest = ?12
          AND snapshot_object_key = ?13`;
    const predecessorGuard =
      `SELECT 1 FROM eco_capability
        WHERE capability_id = ?9
          AND revoked_at IS NULL
          AND rotated_to IS NULL`;

    const results = await this.db.batch([
      this.db
        .prepare(
          `INSERT INTO eco_capability
             (capability_id, capability_digest, subject_ref, snapshot_object_key,
              issued_at, expires_at, revoked_at, rotated_to, session_epoch)
           SELECT ?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, 0
            WHERE EXISTS (${operationGuard})
              AND EXISTS (${predecessorGuard})`
        )
        .bind(
          params.record.capability_id, params.record.capability_digest,
          params.record.subject_ref, params.record.snapshot_object_key,
          params.record.issued_at, params.record.expires_at,
          params.operation_id, params.subject_ref, params.predecessor_capability_id,
          recoverableA, recoverableB, params.payload_digest, params.snapshot_object_key
        ),
      this.db
        .prepare(
          `UPDATE eco_capability
              SET revoked_at = ?2, rotated_to = ?3
            WHERE capability_id = ?1
              AND revoked_at IS NULL
              AND rotated_to IS NULL
              AND EXISTS (SELECT 1 FROM eco_capability WHERE capability_id = ?3)`
        )
        .bind(params.predecessor_capability_id, params.now, params.record.capability_id),
      this.db
        .prepare(
          `UPDATE eco_publication_operation
              SET capability_id = ?2,
                  bearer_generation = bearer_generation + 1,
                  updated_at = ?3
            WHERE operation_id = ?1
              AND capability_id = ?4
              AND subject_ref = ?5
              AND state IN (?6, ?7)
              AND payload_digest = ?8
              AND snapshot_object_key = ?9
              AND EXISTS (SELECT 1 FROM eco_capability WHERE capability_id = ?2)`
        )
        .bind(
          params.operation_id, params.record.capability_id, params.now,
          params.predecessor_capability_id, params.subject_ref,
          recoverableA, recoverableB, params.payload_digest,
          params.snapshot_object_key
        ),
    ]);

    const counts = results.map(changedRows);
    if (counts[0] === 1 && counts[1] === 1 && counts[2] === 1) return true;
    if (counts.some((count) => count !== 0)) {
      throw new Error("PUBLICATION_RECOVERY_NOT_ATOMIC");
    }
    return false;
  }

  /**
   * A delivery-phase transition, bound to the capability the host is actually
   * delivering. Naming a superseded capability changes nothing, which is what
   * stops a stale delivery from terminalising the wrong grant.
   */
  async advanceDelivery(params) {
    const result = await this.db
      .prepare(
        `UPDATE eco_publication_operation
            SET state = ?3, updated_at = ?4
          WHERE operation_id = ?1
            AND state = ?2
            AND capability_id = ?5`
      )
      .bind(params.operation_id, params.from_state, params.to_state,
            params.now, params.capability_id)
      .run();
    return changedRows(result) === 1;
  }
}

/** Never exposes a digest, a bearer or an object key. */
function publicView(row) {
  if (!row) return null;
  return {
    operation_id: row.operation_id,
    state: row.state,
    bearer_generation: Number(row.bearer_generation),
    capability_id: row.capability_id || null,
  };
}

function hasAuthoritativeGrant(row) {
  return !!row && GRANT_AUTHORITATIVE_STATES.indexOf(row.state) !== -1 && !!row.capability_id;
}

function identityMatches(row, params) {
  return row.subject_ref === params.subject_ref && row.payload_digest === params.payload_digest;
}

/* The two fail-closed outcomes of object inspection.
 *
 * Both write nothing: no R2 put, no capability row, no ledger transition. The
 * operation is left exactly where it was, which is what makes it recoverable
 * through an explicit future operational path rather than through a retry that
 * quietly destroys the evidence.
 *
 * `integrity_reason` names the failing invariant for the Worker's OWN log. It
 * is not part of `publicView` and the route must not put it in a response:
 * which invariant failed is information about a private object. */
function integrityFailure(row, reason) {
  return {
    status: PUBLICATION_RESULT.OBJECT_INTEGRITY_FAILURE,
    next_action: PUBLICATION_NEXT_ACTION.INVESTIGATE_OBJECT_INTEGRITY,
    operation: publicView(row),
    integrity_reason: reason || null,
    bearer_available: false,
    bearer_recoverable: false,
  };
}

function unreadableFailure(row, reason) {
  return {
    status: PUBLICATION_RESULT.OBJECT_UNREADABLE,
    next_action: PUBLICATION_NEXT_ACTION.RETRY_AFTER_STORAGE_RECOVERS,
    operation: publicView(row),
    integrity_reason: reason || null,
    bearer_available: false,
    bearer_recoverable: false,
  };
}

/** Classify an existing operation for a caller that will not mint anything. */
function replayResult(row) {
  if (row.state === PUBLICATION_STATE.DELIVERED) {
    return {
      status: PUBLICATION_RESULT.ALREADY_DELIVERED,
      next_action: PUBLICATION_NEXT_ACTION.NONE,
      operation: publicView(row),
      bearer_available: false,
      bearer_recoverable: false,
    };
  }
  if (hasAuthoritativeGrant(row)) {
    return {
      status: PUBLICATION_RESULT.ALREADY_PUBLISHED,
      next_action: PUBLICATION_NEXT_ACTION.USE_PERSISTED_BEARER_OR_RECOVER,
      operation: publicView(row),
      bearer_available: false,
      bearer_recoverable: true,
    };
  }
  return {
    status: PUBLICATION_RESULT.IN_PROGRESS,
    next_action: PUBLICATION_NEXT_ACTION.RETRY_PUBLISH,
    operation: publicView(row),
    bearer_available: false,
    bearer_recoverable: false,
  };
}

/**
 * Publish one snapshot under one operation id.
 *
 * `services` supplies `db`, `pepper`, `mintObjectKey`, `mintGrantId`,
 * `inspectObject` and `putObject`. The whole sequence is idempotent under
 * retry and safe under concurrency; see the module header for why each step
 * is shaped as it is.
 *
 * There is no test hook in this file. Forced interleavings are produced by
 * pausing the LOCAL D1/R2 doubles at the statements below, so the code that
 * runs under test is byte-identical to the code that would deploy.
 *
 * Returns `{ status, next_action, operation, ... }`; only the single winner of
 * the grant transaction ever receives `capability`.
 */
export async function publishSnapshot(services, params) {
  /* THE CAPABILITY LIFETIME, BEFORE ANY STATE EXISTS. The route derives it
   * from the reporting period (`lib/capability_ttl.js`) and there is no
   * fallback anywhere below, so an absent or nonsensical lifetime must stop
   * the publication here — before the operation row is claimed, before R2 is
   * touched and before a grant can be minted with an expiry nobody chose. */
  const ttlSeconds = requireTtlSeconds(params.ttl_seconds);
  const store = new PublicationStore(services.db);

  /* Set once this CALL has established the owned object's coherence — either
   * by writing it itself under the digest guard, or by proving the existing
   * one against every authority. It is deliberately per-call state and is
   * never persisted: a later call proves the object again. */
  let objectProven = false;

  /* Claim the operation together with the one object key it will ever own.
   * A caller that loses this insert never touches R2 with its candidate key. */
  const candidateKey = services.mintObjectKey();
  const created = await store.claimOperation({
    operation_id: params.operation_id,
    subject_ref: params.subject_ref,
    payload_digest: params.payload_digest,
    snapshot_object_key: candidateKey,
    now: params.now,
  });

  let row = await store.findOperation(params.operation_id);
  if (!row) {
    /* The row cannot be absent after a successful claim, and cannot be absent
     * after a lost claim either. Report rather than guess. */
    return { status: PUBLICATION_RESULT.CONFLICT,
             next_action: PUBLICATION_NEXT_ACTION.OPEN_NEW_OPERATION, operation: null };
  }
  if (!identityMatches(row, params)) {
    /* Reusing an id for different content is a caller bug, not a retry.
     * Nothing has been mutated: the claim above was a no-op. */
    return { status: PUBLICATION_RESULT.CONFLICT,
             next_action: PUBLICATION_NEXT_ACTION.OPEN_NEW_OPERATION,
             operation: publicView(row) };
  }

  for (let step = 0; step < MAX_PUBLISH_STEPS; step += 1) {
    if (row.state === PUBLICATION_STATE.DELIVERED || hasAuthoritativeGrant(row)) {
      return replayResult(row);
    }

    if (row.state === PUBLICATION_STATE.CREATED) {
      /* THE object-state gate. See the OBJECT INSPECTION section of the module
       * header: only a DEFINITIVE absence permits a write, and reuse requires
       * every authoritative invariant to have been proven, not just the body
       * hash. Anything else fails closed without touching R2. */
      const inspection = await services.inspectObject({
        snapshot_object_key: row.snapshot_object_key,
        subject_ref: row.subject_ref,
        payload_digest: row.payload_digest,
      });

      if (inspection.state === OBJECT_STATE.PRESENT_INVALID) {
        return integrityFailure(row, inspection.reason);
      }
      if (inspection.state === OBJECT_STATE.UNREADABLE) {
        return unreadableFailure(row, inspection.reason);
      }
      if (inspection.state === OBJECT_STATE.ABSENT) {
        /* The one and only write. The digest guard inside `putObject` refuses
         * if the bytes in hand are not the ones this operation claims. */
        await services.putObject({
          snapshot_object_key: row.snapshot_object_key,
          subject_ref: row.subject_ref,
          body: params.body,
          payload_digest: row.payload_digest,
        });
      } else if (inspection.state !== OBJECT_STATE.PRESENT_VALID) {
        /* An unrecognised verdict is not a licence to proceed. */
        return unreadableFailure(row, "UNRECOGNISED_OBJECT_STATE");
      }
      /* Either this call wrote the object under the digest guard, or it proved
       * the existing one coherent against every authority. Both are proof; a
       * later step in THIS call therefore need not re-read R2. */
      objectProven = true;

      await store.markSnapshotWritten({
        operation_id: params.operation_id,
        subject_ref: params.subject_ref,
        payload_digest: params.payload_digest,
        snapshot_object_key: row.snapshot_object_key,
        now: params.now,
      });
      row = await store.findOperation(params.operation_id);
      if (!row) break;
      continue;
    }

    if (row.state === PUBLICATION_STATE.SNAPSHOT_WRITTEN) {
      /* A grant is the point at which the object becomes reachable by a
       * driver, so it may not be minted over an object this call has not
       * proven. A retry that arrives here after a crash between the write and
       * the grant has proven nothing yet and must inspect.
       *
       * ABSENT is a FAILURE in this state, not a write opportunity: the ledger
       * already records that the object was written, so its disappearance is
       * an integrity incident, and re-creating it here would be exactly the
       * silent repair this contract forbids. */
      if (!objectProven) {
        const inspection = await services.inspectObject({
          snapshot_object_key: row.snapshot_object_key,
          subject_ref: row.subject_ref,
          payload_digest: row.payload_digest,
        });
        if (inspection.state === OBJECT_STATE.UNREADABLE) {
          return unreadableFailure(row, inspection.reason);
        }
        if (inspection.state !== OBJECT_STATE.PRESENT_VALID) {
          return integrityFailure(
            row,
            inspection.state === OBJECT_STATE.ABSENT
              ? "OBJECT_VANISHED_AFTER_SNAPSHOT_WRITTEN"
              : inspection.reason
          );
        }
        objectProven = true;
      }

      const raw = generateCapability();
      const record = {
        capability_id: services.mintGrantId(),
        capability_digest: await capabilityDigest(raw, services.pepper),
        subject_ref: row.subject_ref,
        snapshot_object_key: row.snapshot_object_key,
        issued_at: params.now,
        expires_at: params.now + ttlSeconds,
      };
      const won = await store.mintGrantTransactionally({
        operation_id: params.operation_id,
        subject_ref: params.subject_ref,
        payload_digest: params.payload_digest,
        record: record,
        now: params.now,
      });
      row = await store.findOperation(params.operation_id);
      if (!won) {
        /* Lost the transition. Nothing was inserted and `raw` is discarded
         * here, unreturned and unstored. */
        if (!row) break;
        continue;
      }
      return {
        status: PUBLICATION_RESULT.PUBLISHED,
        next_action: PUBLICATION_NEXT_ACTION.PERSIST_BEARER,
        operation: publicView(row),
        capability: raw,
        capability_id: record.capability_id,
        expires_at: record.expires_at,
        bearer_available: true,
        bearer_recoverable: true,
      };
    }

    /* DELIVERY_INTENT_RECORDED and DELIVERED are handled by the guard at the
     * top of the loop; any other value is a schema violation. */
    break;
  }

  row = await store.findOperation(params.operation_id);
  if (!row) {
    return { status: PUBLICATION_RESULT.CONFLICT,
             next_action: PUBLICATION_NEXT_ACTION.OPEN_NEW_OPERATION, operation: null };
  }
  return replayResult(row);
}

/**
 * Recover from a lost raw bearer.
 *
 * EXPLICIT on purpose. A plain `publish` retry never mints a second bearer;
 * only this call does, and only while the operation is not DELIVERED. The
 * replacement grant, the supersession of its predecessor and the ledger move
 * are ONE transaction, so the operation never has two live grants and never
 * references a grant that does not exist.
 *
 * OBJECT INTEGRITY IS A PRECONDITION, NOT A POST-CONDITION.
 *
 * This call hands a driver a working link to the operation's authoritative
 * snapshot object, so it may not run while that object cannot be PROVEN. It
 * uses the SAME `services.inspectObject` contract the publication path uses —
 * there is no second, weaker integrity rule for recovery — and only
 * PRESENT_VALID proceeds:
 *
 *   ABSENT          refused. The ledger claims an authoritative published
 *                   snapshot; R2 definitively does not have it. A replacement
 *                   bearer would point at nothing.
 *   PRESENT_INVALID refused. Bytes, digest metadata, digest algorithm or
 *                   subject binding disagree with an authority.
 *   UNREADABLE      refused. Storage failure is never a reason to rotate
 *                   authorization. AN ERROR IS NEVER AN ABSENCE.
 *
 * Every refusal happens BEFORE the D1 recovery transaction is attempted, so a
 * refused recovery performs zero authorization mutation: no replacement grant
 * row, no supersession of the predecessor, no generation increment, no change
 * of the publication's capability identity and no raw bearer. The ledger is
 * left byte-for-byte as it was, and the R2 object is neither written nor
 * repaired — inspection is read-only.
 *
 * The inspection reads `snapshot_object_key` and `payload_digest` from the
 * ledger row, and both are bound into the recovery transaction's guards, so
 * the mutation can only apply to the identity that was proven. See
 * `recoverGrantTransactionally` for why that is the achievable contract across
 * two services that cannot share a transaction.
 *
 * The replacement raw bearer is returned only to the winning call and is not
 * stored server-side.
 */
export async function recoverLostBearer(services, params) {
  /* Same gate as publication, and for the same reason: a rotation writes a new
   * `expires_at`, so the replacement grant's lifetime must be one the caller
   * stated for this operation's reporting period. Checked before the operation
   * is even read, so a refused recovery mutates nothing. */
  const ttlSeconds = requireTtlSeconds(params.ttl_seconds);
  const store = new PublicationStore(services.db);
  const operation = await store.findOperation(params.operation_id);
  if (!operation) return { status: PUBLICATION_RESULT.UNKNOWN_OPERATION };

  /* Advisory only — every one of these facts is re-checked inside the
   * transaction. The read exists to produce a truthful refusal reason, not to
   * decide whether it is safe to write. */
  if (operation.state === PUBLICATION_STATE.DELIVERED) {
    return { status: PUBLICATION_RESULT.NOT_RECOVERABLE,
             reason: RECOVERY_REFUSAL.ALREADY_DELIVERED,
             operation: publicView(operation) };
  }
  if (!hasAuthoritativeGrant(operation)) {
    return { status: PUBLICATION_RESULT.NOT_RECOVERABLE,
             reason: RECOVERY_REFUSAL.NO_GRANT_YET,
             next_action: PUBLICATION_NEXT_ACTION.RETRY_PUBLISH,
             operation: publicView(operation) };
  }

  /* THE recovery object-integrity gate. Nothing below this block is reached
   * unless the operation's owned object was proven PRESENT_VALID against every
   * authority: the owned key, a readable body, SHA-256(body) == the ledger
   * `payload_digest`, present digest metadata equal to that same body hash,
   * the contracted digest algorithm, and a subject binding valid for this
   * subject, this key and the current binding version. */
  const inspection = await services.inspectObject({
    snapshot_object_key: operation.snapshot_object_key,
    subject_ref: operation.subject_ref,
    payload_digest: operation.payload_digest,
  });
  if (inspection.state === OBJECT_STATE.UNREADABLE) {
    return unreadableFailure(operation, inspection.reason);
  }
  if (inspection.state !== OBJECT_STATE.PRESENT_VALID) {
    /* ABSENT, PRESENT_INVALID and any verdict this code does not recognise
     * are all refusals. An unrecognised verdict in particular must never fall
     * through to "proceed": that is the shape of the original defect. */
    if (inspection.state === OBJECT_STATE.ABSENT) {
      return integrityFailure(operation, RECOVERY_OBJECT_ABSENT_REASON);
    }
    if (inspection.state === OBJECT_STATE.PRESENT_INVALID) {
      return integrityFailure(operation, inspection.reason);
    }
    return unreadableFailure(operation, "UNRECOGNISED_OBJECT_STATE");
  }

  const predecessorId = operation.capability_id;
  const raw = generateCapability();
  const record = {
    capability_id: params.mint_id,
    capability_digest: await capabilityDigest(raw, services.pepper),
    subject_ref: operation.subject_ref,
    snapshot_object_key: operation.snapshot_object_key,
    issued_at: params.now,
    expires_at: params.now + ttlSeconds,
  };
  const won = await store.recoverGrantTransactionally({
    operation_id: params.operation_id,
    subject_ref: operation.subject_ref,
    predecessor_capability_id: predecessorId,
    /* The identity the object was just proven under, pinned into the CAS. */
    payload_digest: operation.payload_digest,
    snapshot_object_key: operation.snapshot_object_key,
    record: record,
    now: params.now,
  });

  const current = await store.findOperation(params.operation_id);
  if (!won) {
    /* Nothing was written. Say why, from the state that actually won. */
    let reason = RECOVERY_REFUSAL.GRANT_NOT_ELIGIBLE;
    if (current && current.state === PUBLICATION_STATE.DELIVERED) {
      reason = RECOVERY_REFUSAL.ALREADY_DELIVERED;
    } else if (current && current.capability_id !== predecessorId) {
      reason = RECOVERY_REFUSAL.SUPERSEDED;
    }
    return { status: PUBLICATION_RESULT.NOT_RECOVERABLE, reason: reason,
             operation: publicView(current) };
  }

  return {
    status: PUBLICATION_RESULT.RECOVERED,
    next_action: PUBLICATION_NEXT_ACTION.PERSIST_BEARER,
    capability: raw,
    capability_id: record.capability_id,
    expires_at: record.expires_at,
    superseded_capability_id: predecessorId,
    operation: publicView(current),
  };
}

/**
 * Record a delivery phase against the capability the host is delivering.
 *
 * `phase` is INTENT or DELIVERED. The transition is a compare-and-set on both
 * the expected state AND the named capability, so:
 *   * repeating a phase is idempotent (ALREADY_RECORDED);
 *   * naming a superseded bearer is refused (CAPABILITY_SUPERSEDED) rather
 *     than terminalising an operation on a grant the driver never received.
 */
export async function recordDeliveryPhase(services, params) {
  const store = new PublicationStore(services.db);
  const before = await store.findOperation(params.operation_id);
  if (!before) return { status: PUBLICATION_RESULT.UNKNOWN_OPERATION };

  const fromState = params.phase === "INTENT"
    ? PUBLICATION_STATE.GRANT_MINTED
    : PUBLICATION_STATE.DELIVERY_INTENT_RECORDED;
  const toState = params.phase === "INTENT"
    ? PUBLICATION_STATE.DELIVERY_INTENT_RECORDED
    : PUBLICATION_STATE.DELIVERED;
  const moved = await store.advanceDelivery({
    operation_id: params.operation_id,
    from_state: fromState,
    to_state: toState,
    capability_id: params.capability_id,
    now: params.now,
  });
  const after = await store.findOperation(params.operation_id);

  if (moved) {
    return { status: PUBLICATION_RESULT.RECORDED, operation: publicView(after) };
  }
  if (after && after.capability_id !== params.capability_id) {
    /* The bearer the host was about to send is no longer authoritative. */
    return { status: PUBLICATION_RESULT.CAPABILITY_SUPERSEDED, operation: publicView(after) };
  }
  if (after && stateRank(after.state) >= stateRank(toState)) {
    return { status: PUBLICATION_RESULT.ALREADY_RECORDED, operation: publicView(after) };
  }
  return { status: PUBLICATION_RESULT.CONFLICT, operation: publicView(after) };
}

export function isTerminal(state) {
  return state === PUBLICATION_STATE.DELIVERED;
}

export function stateRank(state) {
  return STATE_ORDER.indexOf(state);
}
