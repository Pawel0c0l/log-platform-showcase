/* Payload digest — the publication identity function.
 *
 * ONE definition, used by every side of the byte-integrity contract:
 *
 *   host   `jobs/ecodriving_dashboard/publication.py::payload_digest`
 *          = sha256(canonical UTF-8 JSON bytes), lower-case hex
 *   worker `payloadDigest(bytes)` over the exact ingress octets
 *   ledger `eco_publication_operation.payload_digest`
 *   R2     `customMetadata.payload_digest` over the exact stored octets
 *
 * The input is ALWAYS octets. There is deliberately no string overload: the
 * defect this module exists to prevent is a digest taken over a decoded /
 * re-encoded / re-serialised representation of a payload rather than over the
 * payload itself. `sha256Hex` therefore refuses anything that is not a
 * `Uint8Array`/`ArrayBuffer` view, so a caller cannot accidentally hash text.
 */

export const PAYLOAD_DIGEST_ALGORITHM = "sha-256";

/** Bytes-only SHA-256, lower-case hex. Throws on a non-octet input. */
export async function sha256Hex(bytes) {
  const view = asBytes(bytes);
  const digest = await crypto.subtle.digest("SHA-256", view);
  const out = new Uint8Array(digest);
  let hex = "";
  for (let i = 0; i < out.length; i += 1) hex += out[i].toString(16).padStart(2, "0");
  return hex;
}

/** The publication payload digest. Same function, named for what it means. */
export const payloadDigest = sha256Hex;

export function isPayloadDigest(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

/**
 * Normalise a body to exact octets.
 *
 * A `Uint8Array` / `ArrayBuffer` passes through as the same octets. A string is
 * encoded as UTF-8 — accepted only because local verification scaffolding and
 * the dev server hand fixtures in as text; on the publication path the value is
 * already octets and is never re-encoded. Everything else throws rather than
 * being stringified, because `String(object)` producing `[object Object]` is
 * exactly the silent corruption this contract must not admit.
 */
export function toExactBytes(body) {
  if (body instanceof Uint8Array) return body;
  if (body instanceof ArrayBuffer) return new Uint8Array(body);
  if (ArrayBuffer.isView(body)) {
    return new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
  }
  if (typeof body === "string") return new TextEncoder().encode(body);
  throw new Error("PAYLOAD_BYTES_REQUIRED");
}

function asBytes(bytes) {
  if (bytes instanceof Uint8Array) return bytes;
  if (bytes instanceof ArrayBuffer) return new Uint8Array(bytes);
  if (ArrayBuffer.isView(bytes)) {
    return new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  }
  throw new Error("PAYLOAD_DIGEST_REQUIRES_BYTES");
}

/** Byte-for-byte equality. Not constant time; used on non-secret payloads. */
export function bytesEqual(a, b) {
  const left = asBytes(a);
  const right = asBytes(b);
  if (left.byteLength !== right.byteLength) return false;
  for (let i = 0; i < left.byteLength; i += 1) {
    if (left[i] !== right[i]) return false;
  }
  return true;
}
