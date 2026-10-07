/* Publisher-side capability primitives.
 *
 * WHAT IS DELIBERATELY ABSENT
 *
 * There is no `issueCapability` here any more. An unconditional
 * "insert a grant" helper is exactly the shape that let a live capability
 * exist without the publication ledger referencing it, so the only way a grant
 * is created on the publication path is now `PublicationStore`'s atomic
 * transaction, whose INSERT is conditional on the ledger transition committing
 * with it. The unconditional variant survives only in `local/dev_grants.js`,
 * which is local verification scaffolding and is never imported by any module
 * under `worker/`.
 *
 * What remains here is rotation (a compare-and-set that cannot fan out),
 * revocation, session-epoch invalidation, the object-key generator and the
 * snapshot write. Every function that returns a raw capability returns it
 * exactly once, to its caller, and never stores, logs or echoes it.
 */

import { capabilityDigest, generateCapability, subjectBindingDigest, timingSafeEqualHex } from "./capability.js";
import { PAYLOAD_DIGEST_ALGORITHM, isPayloadDigest, payloadDigest, toExactBytes } from "./digest.js";
import { requireTtlSeconds } from "./capability_ttl.js";

/* THERE IS NO DEFAULT CAPABILITY LIFETIME HERE ANY MORE.
 *
 * `DEFAULT_CAPABILITY_TTL_SECONDS` used to live on this line as a universal
 * 45 days, and every caller that omitted `ttl_seconds` silently inherited it.
 * Capability lifetime is now a property of the REPORTING PERIOD
 * (`lib/capability_ttl.js`: weekly 10 days, monthly 60 days), so an omitted
 * lifetime is a caller that has not said which period it is minting for — a
 * protocol failure, not a value to substitute. Every function below that
 * writes an `expires_at` therefore demands one explicitly.
 */

function randomId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) out += bytes[i].toString(16).padStart(2, "0");
  return out;
}

/**
 * Withdraw a grant. Sessions derived from it stop working on the next request.
 * Idempotent: revoking an already-revoked grant reports `ALREADY_REVOKED`
 * rather than moving `revoked_at`, so a retry cannot rewrite history.
 */
export async function revokeCapability(services, params) {
  const outcome = await services.store.revokeCapability(params.capability_id, params.now);
  return { capability_id: params.capability_id, revoked_at: params.now, status: outcome.status };
}

/** Invalidate live sessions while leaving the link itself usable. */
export async function revokeDerivedSessions(services, params) {
  await services.store.bumpSessionEpoch(params.capability_id);
  return { capability_id: params.capability_id };
}

/* Outcome vocabulary for `rotateCapability`. Rotation never throws for an
 * expected state; storage failures still propagate. */
export const ROTATION = {
  ROTATED: "ROTATED",
  ALREADY_ROTATED: "ALREADY_ROTATED",
  REVOKED: "REVOKED",
  UNKNOWN: "UNKNOWN",
};

/**
 * Replace a grant.
 *
 * IDEMPOTENCY CONTRACT — explicit conflict, not silent re-mint.
 *
 * Rotation is a compare-and-set on the predecessor: it only proceeds while the
 * predecessor is still eligible (not revoked, not already rotated). The insert
 * of the successor is itself conditional on that eligibility and runs in the
 * same D1 batch as the update, so a retry or a concurrent rotation cannot
 * produce a second live successor.
 *
 * A retry therefore returns `ALREADY_ROTATED` with the existing successor's
 * `capability_id` rather than minting another link. It deliberately does NOT
 * return the earlier raw capability: that value was handed to the caller once
 * and is unrecoverable by design. A caller that lost it must revoke and issue
 * afresh.
 *
 * Returns one of:
 *   { status: "ROTATED", capability, capability_id, expires_at }
 *   { status: "ALREADY_ROTATED", capability_id, successor_capability_id }
 *   { status: "REVOKED", capability_id }
 *   { status: "UNKNOWN", capability_id }
 */
export async function rotateCapability(services, params) {
  const { store, pepper } = services;
  const previous = await store.findCapabilityById(params.capability_id);
  if (!previous) return { status: ROTATION.UNKNOWN, capability_id: params.capability_id };
  if (previous.rotated_to) {
    return {
      status: ROTATION.ALREADY_ROTATED,
      capability_id: params.capability_id,
      successor_capability_id: previous.rotated_to,
    };
  }
  if (previous.revoked_at !== null && previous.revoked_at !== undefined) {
    return { status: ROTATION.REVOKED, capability_id: params.capability_id };
  }

  const now = params.now;
  /* Explicit or nothing. See the note where the removed default used to be:
   * a rotation with an unstated lifetime is a rotation whose expiry nobody
   * chose, and this primitive must not invent one. */
  const ttl = requireTtlSeconds(params.ttl_seconds);
  const raw = generateCapability();
  const record = {
    capability_id: randomId(),
    capability_digest: await capabilityDigest(raw, pepper),
    subject_ref: params.subject_ref || previous.subject_ref,
    snapshot_object_key: params.snapshot_object_key || previous.snapshot_object_key,
    issued_at: now,
    expires_at: now + ttl,
  };

  /* The read above is advisory only; this call is the authoritative
   * compare-and-set, so a rotation that lost a race inserts nothing. */
  const outcome = await store.rotateCapability(params.capability_id, record, now);
  if (outcome.status === ROTATION.ROTATED) {
    return {
      status: ROTATION.ROTATED,
      capability: raw,
      capability_id: record.capability_id,
      expires_at: record.expires_at,
    };
  }
  return { ...outcome, capability_id: params.capability_id };
}

/** Point an existing grant at a newly published snapshot object. */
export async function updateSnapshotReference(services, params) {
  await services.store.updateSnapshotObjectKey(params.capability_id, params.snapshot_object_key);
  return { capability_id: params.capability_id };
}

/**
 * Store one driver's snapshot. The object key must be opaque and non-derivable
 * from driver identity (AS-IS PRIVACY_DATA_BOUNDARIES §4.4) — the caller mints
 * it; this function refuses anything that looks like an identity.
 */
/* One shard byte rendered as lower-case hex, then a 32-character base64url
 * body. The shard alphabet is deliberately hex rather than a slice of the
 * base64url body: a base64url slice can contain `-` or `_`, which the validator
 * rejects, so the previous generator produced keys that failed its own contract
 * about 6 % of the time. Both halves are CSPRNG output and neither is derived
 * from driver, client or period identity. */
export const OBJECT_KEY_PATTERN = /^[0-9a-f]{2}\/[A-Za-z0-9_-]{32}\.json$/;

export function assertOpaqueObjectKey(objectKey) {
  if (typeof objectKey !== "string" || !OBJECT_KEY_PATTERN.test(objectKey)) {
    throw new Error("OBJECT_KEY_NOT_OPAQUE");
  }
  return objectKey;
}

export function mintObjectKey() {
  const shardByte = new Uint8Array(1);
  crypto.getRandomValues(shardByte);
  const shard = shardByte[0].toString(16).padStart(2, "0");

  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  const body = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${shard}/${body}.json`;
}

/**
 * Store one driver's snapshot together with the subject binding the Worker
 * checks on read. The binding lives in R2 custom metadata, not in the snapshot
 * JSON, so no subject identifier is ever part of the browser payload.
 *
 * BYTE CONTRACT — the whole point of this function.
 *
 * `params.body` is written to R2 UNCHANGED. It is never parsed, never
 * re-serialised and never round-tripped through a string on the publication
 * path: `handlePublish` hands over the exact ingress octets, and those exact
 * octets are what the object holds. The previous version stored
 * `JSON.stringify(validatedDocument)` instead, which is a semantically equal
 * but byte-different document — so the ledger's `payload_digest` identified a
 * byte sequence that existed nowhere.
 *
 * `payload_digest` is recomputed HERE from the bytes actually being stored,
 * not copied from the caller, and `params.payload_digest` (when supplied) must
 * equal it or the write is refused before it happens. A string body is encoded
 * as UTF-8 first — accepted only for local scaffolding and the dev server; the
 * digest still describes the stored octets exactly.
 */
export async function putSnapshotObject(services, params) {
  assertOpaqueObjectKey(params.snapshot_object_key);
  if (!params.subject_ref) throw new Error("SUBJECT_REF_REQUIRED");
  const bytes = toExactBytes(params.body);
  const digest = await payloadDigest(bytes);
  if (params.payload_digest !== undefined && params.payload_digest !== null) {
    if (!isPayloadDigest(params.payload_digest) ||
        !timingSafeEqualHex(params.payload_digest, digest)) {
      /* Refusing here is what stops an object whose metadata claims one digest
       * while its bytes hash to another from ever existing. */
      throw new Error("SNAPSHOT_PAYLOAD_DIGEST_MISMATCH");
    }
  }
  const binding = await subjectBindingDigest(
    params.subject_ref, params.snapshot_object_key, services.pepper
  );
  await services.bucket.put(params.snapshot_object_key, bytes, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: {
      subject_binding: binding,
      binding_version: "1",
      /* Not browser-visible: the Worker rebuilds the response body and copies
       * no R2 metadata into it. */
      payload_digest: digest,
      payload_digest_algorithm: PAYLOAD_DIGEST_ALGORITHM,
    },
  });
  return { snapshot_object_key: params.snapshot_object_key, payload_digest: digest };
}

/**
 * WHAT STATE IS THE OPERATION'S OWNED OBJECT IN?
 *
 * THE DEFECT THIS REPLACES
 *
 * The previous `snapshotObjectDigest` returned a boolean `present`, and
 * signalled an unreadable object as `{ present: false, unreadable: true }`.
 * The publication retry read only `present`, so BOTH of these held:
 *
 *   * an R2 `get()` that threw was indistinguishable from "no object here",
 *     and the retry answered it with another `put()` that OVERWROTE an object
 *     nobody had been able to read;
 *   * an object whose body happened to hash correctly advanced the ledger even
 *     when its digest metadata or its subject binding had been removed or
 *     corrupted, so a publication became authoritative over an object already
 *     known to be internally inconsistent — 201, one live grant, and a driver
 *     read that then failed 503 forever.
 *
 * AN ERROR IS NOT AN ABSENCE. That is the whole contract below.
 *
 * THE STATE MODEL
 *
 *   ABSENT          R2 definitively answered "no such object" — `get()`
 *                   returned exactly `null` without throwing. `null` is the
 *                   ONLY value that means this; `undefined`, a non-object or
 *                   any other unexpected result is a malformed response and is
 *                   UNREADABLE, never an absence. This is the ONLY state in
 *                   which publication may write the owned object.
 *   PRESENT_VALID   the object exists and EVERY authoritative invariant was
 *                   PROVEN — never merely "not contradicted": the result
 *                   carries a string `key` EQUAL to the operation-owned key,
 *                   body readable, SHA-256(body) == the ledger digest,
 *                   `payload_digest` metadata present and equal to the hash of
 *                   the actual body, digest algorithm as contracted, subject
 *                   binding present and valid for this subject, this key and
 *                   the current binding version. Retry may reuse it, writing
 *                   nothing.
 *   PRESENT_INVALID the object exists and at least one of those failed.
 *                   Publication fails closed: no overwrite, no grant, no
 *                   ledger advancement. Repair is deliberately NOT part of
 *                   ordinary retry.
 *   UNREADABLE      neither validity nor absence could be established: `get()`
 *                   threw, the result was `undefined`, an array or otherwise
 *                   malformed,
 *                   it carried no usable `key` so the object's identity could
 *                   not be established, it could not be interpreted (a
 *                   conditional response carries no body), the body read threw,
 *                   the metadata could not be read, or no bucket is bound. Also
 *                   fails closed, for the same reason — a caller that cannot
 *                   read an object certainly cannot prove it is safe to
 *                   replace.
 *
 * WHY THE R2 CONTRACT SUPPORTS THIS
 *
 * The Workers R2 API (`developers.cloudflare.com/r2/api/workers/workers-api-reference/`)
 * defines `get(key)` as resolving to `null` "if the key does not exist" and to
 * an `R2ObjectBody` otherwise, and documents that a conditional `get` whose
 * precondition fails resolves to an `R2Object` with `body` undefined. So:
 * `null` is the one definitive absence signal, a rejected promise is an error
 * and never an absence, and a resolved value with no readable body is
 * uninterpretable rather than empty. Each is mapped to a distinct state here.
 * `undefined` appears nowhere in that contract, so a `get()` that resolves it
 * is a storage response this code cannot account for — and an unaccountable
 * response is the one thing that must never be answered with a write.
 *
 * Nothing in this function writes, deletes or repairs anything.
 */
export const OBJECT_STATE = {
  ABSENT: "ABSENT",
  PRESENT_VALID: "PRESENT_VALID",
  PRESENT_INVALID: "PRESENT_INVALID",
  UNREADABLE: "UNREADABLE",
};

/* Why an object was refused, or why its state could not be established. These
 * are for the Worker's own logs and for tests. They are never returned to the
 * caller of `/api/publish`: which invariant failed is information about the
 * private object, and the publisher is told only that integrity failed. */
export const OBJECT_INSPECTION_REASON = {
  NO_BUCKET_BINDING: "NO_BUCKET_BINDING",
  GET_THREW: "GET_THREW",
  RESULT_UNDEFINED: "RESULT_UNDEFINED",
  RESULT_MALFORMED: "RESULT_MALFORMED",
  RESULT_ARRAY: "RESULT_ARRAY",
  KEY_MISSING: "KEY_MISSING",
  KEY_NOT_STRING: "KEY_NOT_STRING",
  KEY_READ_THREW: "KEY_READ_THREW",
  UNINTERPRETABLE_RESULT: "UNINTERPRETABLE_RESULT",
  BODY_READ_THREW: "BODY_READ_THREW",
  BODY_NOT_BYTES: "BODY_NOT_BYTES",
  METADATA_READ_THREW: "METADATA_READ_THREW",
  BINDING_COMPUTATION_THREW: "BINDING_COMPUTATION_THREW",
  KEY_MISMATCH: "KEY_MISMATCH",
  METADATA_MISSING: "METADATA_MISSING",
  PAYLOAD_DIGEST_METADATA_MISSING: "PAYLOAD_DIGEST_METADATA_MISSING",
  PAYLOAD_DIGEST_METADATA_MISMATCH: "PAYLOAD_DIGEST_METADATA_MISMATCH",
  DIGEST_ALGORITHM_MISMATCH: "DIGEST_ALGORITHM_MISMATCH",
  LEDGER_DIGEST_MISMATCH: "LEDGER_DIGEST_MISMATCH",
  LEDGER_DIGEST_MALFORMED: "LEDGER_DIGEST_MALFORMED",
  SUBJECT_BINDING_MISSING: "SUBJECT_BINDING_MISSING",
  SUBJECT_BINDING_VERSION: "SUBJECT_BINDING_VERSION",
  SUBJECT_BINDING_MISMATCH: "SUBJECT_BINDING_MISMATCH",
  SUBJECT_REF_MISSING: "SUBJECT_REF_MISSING",
};

/* The binding version this Worker can verify. An object stamped with any other
 * version is not "probably fine": it was written under a contract this code
 * does not implement, so it cannot be proven and is refused. */
export const SUBJECT_BINDING_VERSION = "1";

function unreadable(reason) {
  return { state: OBJECT_STATE.UNREADABLE, reason: reason, reusable: false, writable: false };
}

function invalid(reason) {
  return { state: OBJECT_STATE.PRESENT_INVALID, reason: reason, reusable: false, writable: false };
}

/**
 * Inspect the operation-owned object.
 *
 * `params` = `{ snapshot_object_key, subject_ref, payload_digest }`, where
 * `payload_digest` is the AUTHORITATIVE ledger digest for that key. Every
 * check below is required for reuse; there is no "good enough" outcome and no
 * path that returns a reusable verdict from the body hash alone.
 */
export async function inspectSnapshotObject(services, params) {
  const objectKey = params && params.snapshot_object_key;
  const subjectRef = params && params.subject_ref;
  const ledgerDigest = params && params.payload_digest;

  /* No bucket bound is an inability to determine state, NOT an absence. The
   * previous version returned `{ present: false }` here, i.e. "go ahead and
   * write", from a services bundle that could not write. */
  if (!services || !services.bucket || typeof services.bucket.get !== "function") {
    return unreadable(OBJECT_INSPECTION_REASON.NO_BUCKET_BINDING);
  }
  if (typeof subjectRef !== "string" || subjectRef.length === 0) {
    return invalid(OBJECT_INSPECTION_REASON.SUBJECT_REF_MISSING);
  }
  if (!isPayloadDigest(ledgerDigest)) {
    return invalid(OBJECT_INSPECTION_REASON.LEDGER_DIGEST_MALFORMED);
  }

  let object = null;
  try {
    object = await services.bucket.get(objectKey);
  } catch (error) {
    /* A thrown get proves nothing at all. */
    return unreadable(OBJECT_INSPECTION_REASON.GET_THREW);
  }

  /* THE definitive absence, and the ONLY one. R2 documents exactly one
   * value for "the key does not exist": `null`. `undefined` is not that
   * value — it is a result this code cannot interpret, and treating it as an
   * absence is a licence to overwrite an object that may well exist. */
  if (object === null) {
    return { state: OBJECT_STATE.ABSENT, reason: null, reusable: false, writable: true };
  }
  if (object === undefined) {
    return unreadable(OBJECT_INSPECTION_REASON.RESULT_UNDEFINED);
  }
  if (typeof object !== "object") {
    return unreadable(OBJECT_INSPECTION_REASON.RESULT_MALFORMED);
  }
  /* `typeof [] === "object"`, so an array walks straight through the check
   * above — and an array can carry a `key`, an `arrayBuffer()` and a
   * `customMetadata`, which is every property the rest of this function reads.
   * A decorated array therefore used to prove itself PRESENT_VALID. R2 never
   * resolves an array, so one is a malformed result: refused HERE, before any
   * property it supplies is trusted for anything. */
  if (Array.isArray(object)) {
    return unreadable(OBJECT_INSPECTION_REASON.RESULT_ARRAY);
  }

  /* STRUCTURAL SHAPE BEFORE CONTENT.
   *
   * The key R2 echoes must be PROVEN equal to the operation-owned key before
   * any body, digest-metadata or subject-binding check is worth running: those
   * checks answer "is this object coherent?", and they are meaningless until
   * "is this the object we asked about?" has been answered YES. The previous
   * version compared only when a string key happened to be present, so a
   * result carrying no key at all skipped the comparison entirely and could
   * still reach PRESENT_VALID. Absence of evidence was read as equality. */
  let echoedKey = null;
  try {
    echoedKey = object.key;
  } catch (error) {
    return unreadable(OBJECT_INSPECTION_REASON.KEY_READ_THREW);
  }
  if (echoedKey === undefined && !("key" in object)) {
    return unreadable(OBJECT_INSPECTION_REASON.KEY_MISSING);
  }
  if (typeof echoedKey !== "string") {
    return unreadable(OBJECT_INSPECTION_REASON.KEY_NOT_STRING);
  }
  /* A well-formed key that names a DIFFERENT object is a statement about the
   * object, not about our ability to read it: the response describes something
   * this operation does not own. That stays an object-integrity failure. */
  if (echoedKey !== objectKey) {
    return invalid(OBJECT_INSPECTION_REASON.KEY_MISMATCH);
  }

  /* A conditional response carries no body; so does any result this code does
   * not understand. Neither is an empty object and neither is an absence. */
  if (typeof object.arrayBuffer !== "function") {
    return unreadable(OBJECT_INSPECTION_REASON.UNINTERPRETABLE_RESULT);
  }

  let bytes = null;
  try {
    const buffer = await object.arrayBuffer();
    if (buffer instanceof ArrayBuffer) {
      bytes = new Uint8Array(buffer);
    } else if (ArrayBuffer.isView(buffer)) {
      bytes = new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
    } else {
      return unreadable(OBJECT_INSPECTION_REASON.BODY_NOT_BYTES);
    }
  } catch (error) {
    /* A body that cannot be read cannot be proven correct AND cannot be proven
     * absent. Fail closed rather than overwrite it. */
    return unreadable(OBJECT_INSPECTION_REASON.BODY_READ_THREW);
  }

  let metadata = null;
  try {
    metadata = object.customMetadata;
  } catch (error) {
    return unreadable(OBJECT_INSPECTION_REASON.METADATA_READ_THREW);
  }
  /* Metadata that is simply absent is a missing REQUIRED contract, not an
   * access failure: the object exists and is incoherent. */
  if (metadata === null || metadata === undefined || typeof metadata !== "object") {
    return invalid(OBJECT_INSPECTION_REASON.METADATA_MISSING);
  }

  /* Computed over the octets actually stored — never read from metadata,
   * because metadata is one of the things a corruption moves. */
  const actualDigest = await payloadDigest(bytes);

  /* (1) the ledger is authoritative for what these bytes must be. This is what
   * catches a body and its metadata rewritten together. */
  if (!timingSafeEqualHex(actualDigest, ledgerDigest)) {
    return invalid(OBJECT_INSPECTION_REASON.LEDGER_DIGEST_MISMATCH);
  }

  /* (2) the object must carry its own digest, and it must be the digest of
   * the body it is attached to. The driver read requires this metadata, so an
   * object without it is unservable even though its bytes are correct. */
  const declared = metadata.payload_digest;
  if (!isPayloadDigest(declared)) {
    return invalid(OBJECT_INSPECTION_REASON.PAYLOAD_DIGEST_METADATA_MISSING);
  }
  if (!timingSafeEqualHex(declared, actualDigest)) {
    return invalid(OBJECT_INSPECTION_REASON.PAYLOAD_DIGEST_METADATA_MISMATCH);
  }
  if (metadata.payload_digest_algorithm !== PAYLOAD_DIGEST_ALGORITHM) {
    return invalid(OBJECT_INSPECTION_REASON.DIGEST_ALGORITHM_MISMATCH);
  }

  /* (3) subject binding: this object belongs to this subject at this key. The
   * driver read enforces it, so reuse must prove it too. */
  const storedBinding = metadata.subject_binding;
  if (typeof storedBinding !== "string" || storedBinding.length === 0) {
    return invalid(OBJECT_INSPECTION_REASON.SUBJECT_BINDING_MISSING);
  }
  if (metadata.binding_version !== SUBJECT_BINDING_VERSION) {
    return invalid(OBJECT_INSPECTION_REASON.SUBJECT_BINDING_VERSION);
  }
  let expectedBinding = null;
  try {
    expectedBinding = await subjectBindingDigest(subjectRef, objectKey, services.pepper);
  } catch (error) {
    /* Cannot compute the expectation, so cannot prove the object. Not a
     * verdict about the object; a verdict about our ability to judge it. */
    return unreadable(OBJECT_INSPECTION_REASON.BINDING_COMPUTATION_THREW);
  }
  if (!timingSafeEqualHex(storedBinding, expectedBinding)) {
    return invalid(OBJECT_INSPECTION_REASON.SUBJECT_BINDING_MISMATCH);
  }

  /* Every authoritative invariant proven. Only now is reuse permitted. */
  return {
    state: OBJECT_STATE.PRESENT_VALID,
    reason: null,
    reusable: true,
    writable: false,
    digest: actualDigest,
    bytes_length: bytes.byteLength,
  };
}
