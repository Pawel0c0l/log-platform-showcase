/* Snapshot contract gate.
 *
 * The Worker is not a second business-rules engine: the host already decided
 * what is publishable. What it does do is refuse to hand the browser an object
 * that is not exactly the v1 browser contract — a corrupt object, an
 * unsupported version, an unknown field, a wrong type, or a private marker
 * smuggled into an otherwise plausible structure.
 *
 * The structural allowlist lives in `schema_v1.js`. This module is the gate in
 * front of it: size, JSON well-formedness, the forbidden-name sweep kept as
 * defence in depth, and the subject/object binding check.
 *
 * TWO DIFFERENT BYTE SEQUENCES, DELIBERATELY
 *
 *   AUTHORITATIVE STORED BYTES  the exact canonical host bytes whose SHA-256
 *                               is the publication ledger's `payload_digest`.
 *                               Nothing in this file, or anywhere on the write
 *                               path, may transform them.
 *   BROWSER RESPONSE BYTES      rebuilt here from the validated allowlist, so
 *                               a field the schema does not know about can
 *                               never reach the page even if it somehow
 *                               reached the bucket. Semantically the same
 *                               document; not the same octets, and not the
 *                               thing the digest identifies.
 *
 * The rebuild is defence in depth for the READ path only. Publishing stores
 * the ingress octets unchanged — see `worker/lib/publisher.js`.
 */

import { subjectBindingDigest, timingSafeEqualHex } from "./capability.js";
import { isPayloadDigest, payloadDigest } from "./digest.js";
import { validateSnapshotDocument } from "./schema_v1.js";

export const CONTRACT_ID = "driver_eco_dashboard_snapshot";
export const SUPPORTED_SCHEMA_VERSIONS = [1];
export const MAX_SNAPSHOT_BYTES = 256 * 1024;

/* Kept as defence in depth behind the strict allowlist: an allowlisted field
 * name can never be one of these, so a hit here means the schema itself drifted.
 * Mirrors jobs/ecodriving_dashboard/snapshot_contract.py FORBIDDEN_FIELD_*. */
const FORBIDDEN_FIELD_NAMES = new Set([
  "assigned_id", "capability", "capability_id", "chassis_number", "client_code", "client_id",
  "day_status", "driver_email", "driver_id", "driver_key", "driver_name", "driver_surname",
  "driver_tag_description", "email", "email_address", "employee_id", "geofence", "latitude",
  "longitude", "min_daily_evaluation_km", "notification_email", "object_key", "odometer",
  "person_name", "person_name_group_key", "phone", "provider_trip_id", "r2_key", "ranking_group",
  "ranking_included", "record_id", "recipient_email", "registration", "session", "source_person_id",
  "subject_ref", "trip_end_ts", "trip_mode", "trip_start_ts", "vehicle_registration",
]);

const FORBIDDEN_FIELD_FRAGMENTS = [
  "capability", "chassis", "client_code", "day_status", "driver_key", "driver_name", "driver_tag",
  "email", "geofence", "latitude", "longitude", "odometer", "password", "person_name", "phone",
  "ranking_group", "ranking_included", "registration", "secret", "session", "smtp", "subject_ref",
  "surname", "token", "trip_mode",
];

export const SNAPSHOT_REJECTION = {
  NOT_JSON: "NOT_JSON",
  WRONG_CONTRACT: "WRONG_CONTRACT",
  UNSUPPORTED_VERSION: "UNSUPPORTED_VERSION",
  MALFORMED: "MALFORMED",
  FORBIDDEN_FIELD: "FORBIDDEN_FIELD",
  TOO_LARGE: "TOO_LARGE",
  SCHEMA: "SCHEMA",
  SUBJECT_BINDING: "SUBJECT_BINDING",
  NOT_CANONICAL: "NOT_CANONICAL",
  PAYLOAD_DIGEST: "PAYLOAD_DIGEST",
};

/* Canonical-form rejection detail. Each names one property of the host
 * serialiser that the received bytes did not have. */
export const CANONICAL_REJECTION = {
  INSIGNIFICANT_WHITESPACE: "INSIGNIFICANT_WHITESPACE",
  UNSORTED_KEYS: "UNSORTED_KEYS",
  DUPLICATE_KEY: "DUPLICATE_KEY",
  MALFORMED: "MALFORMED",
  MAX_DEPTH: "MAX_DEPTH",
};

function scanForbiddenKeys(node, depth) {
  if (depth > 24) return "MAX_DEPTH";
  if (Array.isArray(node)) {
    for (const item of node) {
      const found = scanForbiddenKeys(item, depth + 1);
      if (found) return found;
    }
    return null;
  }
  if (node && typeof node === "object") {
    for (const key of Object.keys(node)) {
      const lowered = key.toLowerCase();
      if (FORBIDDEN_FIELD_NAMES.has(lowered)) return key;
      for (const fragment of FORBIDDEN_FIELD_FRAGMENTS) {
        if (lowered.includes(fragment)) return key;
      }
      const found = scanForbiddenKeys(node[key], depth + 1);
      if (found) return found;
    }
  }
  return null;
}

/**
 * Validate raw object bytes against the v1 browser contract.
 *
 * Returns `{ ok: true, body }` where `body` is JSON re-serialised from the
 * validated allowlist — never the stored bytes — or `{ ok: false, reason }`.
 */
export function validateSnapshotText(text) {
  if (typeof text !== "string" || text.length === 0) {
    return { ok: false, reason: SNAPSHOT_REJECTION.NOT_JSON };
  }
  if (text.length > MAX_SNAPSHOT_BYTES) {
    return { ok: false, reason: SNAPSHOT_REJECTION.TOO_LARGE };
  }
  let candidate;
  try {
    candidate = JSON.parse(text);
  } catch (error) {
    return { ok: false, reason: SNAPSHOT_REJECTION.NOT_JSON };
  }
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
    return { ok: false, reason: SNAPSHOT_REJECTION.MALFORMED };
  }

  /* Answer the two coarse questions first so their reasons stay specific. */
  if (candidate.contract_id !== CONTRACT_ID) {
    return { ok: false, reason: SNAPSHOT_REJECTION.WRONG_CONTRACT };
  }
  if (!SUPPORTED_SCHEMA_VERSIONS.includes(candidate.schema_version)) {
    return { ok: false, reason: SNAPSHOT_REJECTION.UNSUPPORTED_VERSION };
  }

  const validated = validateSnapshotDocument(candidate);
  if (!validated.ok) {
    return { ok: false, reason: SNAPSHOT_REJECTION.SCHEMA, detail: validated.reason, path: validated.path };
  }

  /* Defence in depth: the allowlist cannot admit a forbidden name, so a hit
   * here would mean the schema drifted rather than the object being wrong. */
  const forbidden = scanForbiddenKeys(validated.document, 0);
  if (forbidden) return { ok: false, reason: SNAPSHOT_REJECTION.FORBIDDEN_FIELD };

  return { ok: true, body: JSON.stringify(validated.document), document: validated.document };
}

/**
 * Verify that the stored object was published for the subject this grant
 * authorises. Neither `subject_ref` nor the object key ever reaches the
 * browser; only the comparison result matters.
 *
 * Fails closed when the metadata is absent: an object without a binding cannot
 * be proven to belong to this subject.
 */
export async function verifySubjectBinding(object, grant, pepper) {
  const metadata = (object && object.customMetadata) || null;
  const stored = metadata && metadata.subject_binding;
  if (typeof stored !== "string" || stored.length === 0) {
    return { ok: false, reason: SNAPSHOT_REJECTION.SUBJECT_BINDING, detail: "MISSING" };
  }
  const expected = await subjectBindingDigest(grant.subject_ref, grant.snapshot_object_key, pepper);
  if (!timingSafeEqualHex(stored, expected)) {
    return { ok: false, reason: SNAPSHOT_REJECTION.SUBJECT_BINDING, detail: "MISMATCH" };
  }
  return { ok: true };
}

/* ------------------------------------------------------- canonical form -- */

/*
 * WHY THIS IS A SCANNER AND NOT A SERIALISER
 *
 * The host is the canonical serialisation authority
 * (`jobs/ecodriving_dashboard/snapshot_contract.py::canonical_json_bytes`:
 * sorted keys, `(",", ":")` separators, `ensure_ascii=False`, UTF-8,
 * `allow_nan=False`). Reproducing that in JavaScript would mean reproducing
 * Python's float repr and string escaping too — `JSON.stringify(1.0)` is `1`
 * where Python writes `1.0` — and a second serialiser that disagrees on one
 * number is a publication outage, not a security control.
 *
 * So this checks only the two properties that are observable from the token
 * stream and that no serialiser-specific formatting can affect:
 *
 *   * no insignificant whitespace anywhere outside a string literal
 *     (`separators=(",", ":")`);
 *   * object keys strictly ascending (`sort_keys=True`), which also makes a
 *     duplicate key detectable.
 *
 * Numbers, escapes and string contents are deliberately NOT re-encoded or
 * compared. Byte identity is guaranteed by storing the ingress octets, not by
 * this function; this function only refuses an obviously non-canonical
 * representation at ingress so a pretty-printed or reordered payload cannot
 * become an authoritative snapshot.
 *
 * Keys in this contract are ASCII identifiers from the schema allowlist, so
 * comparing them with JavaScript's UTF-16 ordering agrees with Python's
 * code-point ordering for every key the schema can admit.
 */

const MAX_CANONICAL_DEPTH = 64;
const WHITESPACE = new Set([" ", "\t", "\n", "\r"]);

/** Read one JSON string literal starting at the opening quote. */
function readJsonString(text, start) {
  let out = "";
  let i = start + 1;
  while (i < text.length) {
    const c = text[i];
    if (c === '"') return { value: out, next: i + 1 };
    if (c === "\\") {
      const esc = text[i + 1];
      if (esc === undefined) return null;
      if (esc === "u") {
        const hex = text.slice(i + 2, i + 6);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) return null;
        out += String.fromCharCode(parseInt(hex, 16));
        i += 6;
        continue;
      }
      const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
      if (!Object.prototype.hasOwnProperty.call(simple, esc)) return null;
      out += simple[esc];
      i += 2;
      continue;
    }
    out += c;
    i += 1;
  }
  return null;
}

/**
 * Is `text` in the host's canonical representation?
 *
 * `text` must already be known-valid JSON (this runs after `JSON.parse`), so
 * the scan is a shape check, not a parser. Returns `{ ok: true }` or
 * `{ ok: false, detail }` naming which canonical property failed.
 */
export function checkCanonicalJsonForm(text) {
  if (typeof text !== "string" || text.length === 0) {
    return { ok: false, detail: CANONICAL_REJECTION.MALFORMED };
  }
  /* Frames: { object: bool, expectKey: bool, lastKey: string|null } */
  const frames = [];
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (WHITESPACE.has(c)) {
      return { ok: false, detail: CANONICAL_REJECTION.INSIGNIFICANT_WHITESPACE };
    }
    if (c === '"') {
      const top = frames.length ? frames[frames.length - 1] : null;
      const literal = readJsonString(text, i);
      if (!literal) return { ok: false, detail: CANONICAL_REJECTION.MALFORMED };
      if (top && top.object && top.expectKey) {
        if (top.lastKey !== null) {
          if (literal.value === top.lastKey) {
            return { ok: false, detail: CANONICAL_REJECTION.DUPLICATE_KEY };
          }
          if (literal.value < top.lastKey) {
            return { ok: false, detail: CANONICAL_REJECTION.UNSORTED_KEYS };
          }
        }
        top.lastKey = literal.value;
      }
      i = literal.next;
      continue;
    }
    if (c === "{" || c === "[") {
      if (frames.length + 1 > MAX_CANONICAL_DEPTH) {
        return { ok: false, detail: CANONICAL_REJECTION.MAX_DEPTH };
      }
      frames.push({ object: c === "{", expectKey: c === "{", lastKey: null });
      i += 1;
      continue;
    }
    if (c === "}" || c === "]") {
      if (!frames.length) return { ok: false, detail: CANONICAL_REJECTION.MALFORMED };
      frames.pop();
      i += 1;
      continue;
    }
    if (c === ",") {
      const top = frames.length ? frames[frames.length - 1] : null;
      if (top && top.object) top.expectKey = true;
      i += 1;
      continue;
    }
    if (c === ":") {
      const top = frames.length ? frames[frames.length - 1] : null;
      if (top && top.object) top.expectKey = false;
      i += 1;
      continue;
    }
    /* Number / true / false / null body. Format is the host's business. */
    i += 1;
  }
  if (frames.length) return { ok: false, detail: CANONICAL_REJECTION.MALFORMED };
  return { ok: true };
}

/* ----------------------------------------------- stored-byte integrity -- */

/**
 * Prove that the octets just read from R2 are the octets the publication
 * recorded.
 *
 * TWO INDEPENDENT AUTHORITIES, BOTH FAIL-CLOSED:
 *
 *   1. `customMetadata.payload_digest`, written by `putSnapshotObject` from
 *      the exact bytes it stored. Required: an object with no digest cannot
 *      prove anything about itself, so it is refused rather than trusted.
 *   2. `eco_publication_operation.payload_digest`, the publication ledger's
 *      authoritative value for the operation that OWNS this object key. When
 *      a publication row exists it must also agree; when it does not (a grant
 *      issued outside the publication path, i.e. local scaffolding), only
 *      authority 1 applies and the caller is told so.
 *
 * Authority 2 is what makes this more than a checksum: R2 metadata and R2 body
 * are written together, so a rewrite of both would satisfy authority 1 alone.
 * The ledger lives in a different store, under a different credential.
 */
export async function verifyStoredBytes(bytes, object, ledgerDigest) {
  const metadata = (object && object.customMetadata) || null;
  const declared = metadata && metadata.payload_digest;
  if (!isPayloadDigest(declared)) {
    return { ok: false, reason: SNAPSHOT_REJECTION.PAYLOAD_DIGEST, detail: "MISSING" };
  }
  const computed = await payloadDigest(bytes);
  if (!timingSafeEqualHex(declared, computed)) {
    return { ok: false, reason: SNAPSHOT_REJECTION.PAYLOAD_DIGEST, detail: "OBJECT_MISMATCH" };
  }
  if (ledgerDigest !== null && ledgerDigest !== undefined) {
    if (!isPayloadDigest(ledgerDigest)) {
      return { ok: false, reason: SNAPSHOT_REJECTION.PAYLOAD_DIGEST, detail: "LEDGER_MALFORMED" };
    }
    if (!timingSafeEqualHex(ledgerDigest, computed)) {
      return { ok: false, reason: SNAPSHOT_REJECTION.PAYLOAD_DIGEST, detail: "LEDGER_MISMATCH" };
    }
    return { ok: true, digest: computed, ledger_verified: true };
  }
  return { ok: true, digest: computed, ledger_verified: false };
}
