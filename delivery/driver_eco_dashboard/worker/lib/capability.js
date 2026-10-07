/* Capability (bearer link) primitives.
 *
 * A capability is a 256-bit CSPRNG value rendered base64url. It carries no
 * driver, client, period or object information: it is a pure lookup handle that
 * only means something against the server-side authorization store.
 *
 * At rest we keep a digest, never the raw value. Because the raw value is
 * 256 bits of uniform randomness, a single SHA-256 (or HMAC-SHA-256 when a
 * pepper binding is configured) is non-invertible in practice — a slow KDF
 * would only add a request-time DoS surface without adding real strength.
 */

export const CAPABILITY_BYTES = 32;
/* base64url of 32 bytes, unpadded */
export const CAPABILITY_PATTERN = /^[A-Za-z0-9_-]{43}$/;

const encoder = new TextEncoder();

function toBase64Url(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function toHex(buffer) {
  const view = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < view.length; i += 1) out += view[i].toString(16).padStart(2, "0");
  return out;
}

/** Mint a new raw capability. The caller must hand it to the driver and forget it. */
export function generateCapability() {
  const bytes = new Uint8Array(CAPABILITY_BYTES);
  crypto.getRandomValues(bytes);
  return toBase64Url(bytes);
}

/** Cheap shape gate so an obviously malformed value never reaches the store. */
export function isWellFormedCapability(value) {
  return typeof value === "string" && CAPABILITY_PATTERN.test(value);
}

/**
 * Digest used as the storage lookup key.
 * `pepper` is an optional secret binding; when present the digest becomes an
 * HMAC, so a leaked authorization table alone cannot be probed offline against
 * a candidate capability list.
 */
export async function capabilityDigest(raw, pepper) {
  const data = encoder.encode(raw);
  if (pepper) {
    const key = await crypto.subtle.importKey(
      "raw", encoder.encode(pepper), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
    );
    return toHex(await crypto.subtle.sign("HMAC", key, data));
  }
  return toHex(await crypto.subtle.digest("SHA-256", data));
}

/* --------------------------------------------------- subject binding ---- */

/* Domain-separation label. Any change to the encoding below MUST change this
 * label, so an old digest can never validate under new rules. */
export const SUBJECT_BINDING_DOMAIN = "driver_eco_dashboard.subject_binding.v1";
/* U+001F UNIT SEPARATOR. Never valid inside a subject ref or an object key. */
const UNIT_SEPARATOR = "\u001F";

/**
 * Canonical, length-prefixed binding material.
 *
 * Plain `a|b|c` concatenation is ambiguous: subject "a", key "b|c" and subject
 * "a|b", key "c" produce identical material and therefore identical digests.
 * Length prefixes remove that entirely — the byte length of each field is part
 * of the material, so no arrangement of separator characters inside a value can
 * imitate a different pair.
 *
 *   DOMAIN US len(subject_utf8) US subject_utf8 US len(key_utf8) US key_utf8
 *
 * Lengths are UTF-8 BYTE counts in decimal, not code-point counts. Inputs are
 * used verbatim: no case folding, no trimming and no Unicode normalisation —
 * the publisher is responsible for supplying already-normalised (NFC) values,
 * because normalising here would let two different stored refs collide.
 *
 * The exact specification and its fixed vectors live in
 * `delivery/driver_eco_dashboard/spec/subject_binding_v1.md` and
 * `.../spec/subject_binding_v1_vectors.json`, and are asserted from both
 * JavaScript and Python.
 */
export function subjectBindingMaterial(subjectRef, objectKey) {
  const subject = String(subjectRef);
  const key = String(objectKey);
  if (subject.includes(UNIT_SEPARATOR) || key.includes(UNIT_SEPARATOR)) {
    throw new Error("SUBJECT_BINDING_INVALID_INPUT");
  }
  const encoder = new TextEncoder();
  const subjectBytes = encoder.encode(subject).length;
  const keyBytes = encoder.encode(key).length;
  return (
    SUBJECT_BINDING_DOMAIN + UNIT_SEPARATOR +
    String(subjectBytes) + UNIT_SEPARATOR + subject + UNIT_SEPARATOR +
    String(keyBytes) + UNIT_SEPARATOR + key
  );
}

/**
 * Binding digest tying a stored object to the subject it was published for.
 *
 * The publisher writes this into the R2 object's custom metadata; the Worker
 * recomputes it from the grant and refuses the object when they disagree. That
 * makes a cross-subject object mix-up — the right key holding the wrong
 * person's document, or an object copied to another key — detectable at the
 * delivery boundary. Neither input ever reaches the browser.
 */
export async function subjectBindingDigest(subjectRef, objectKey, pepper) {
  return capabilityDigest(subjectBindingMaterial(subjectRef, objectKey), pepper);
}

/** Length-independent constant-time comparison for hex digests. */
export function timingSafeEqualHex(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Session identifiers use the same generation and digesting rules. */
export const generateSessionId = generateCapability;
export const isWellFormedSessionId = isWellFormedCapability;
export const sessionDigest = capabilityDigest;
