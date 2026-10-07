/* Machine-to-machine authentication for the publisher write transport.
 *
 * This is a SEPARATE credential space from driver authorization. A driver
 * capability or session cookie is never accepted here, and a publisher
 * credential is never accepted on a driver route — the two paths do not share
 * a lookup, a table or a header.
 *
 * The credential is a high-entropy machine token held by the calculation host.
 * Only its digest is configured on the Worker (`PUBLISHER_KEY_DIGEST`), so the
 * deployed configuration never contains the usable value. Comparison is
 * constant-time.
 *
 * Fail closed: with no digest configured the transport refuses every request,
 * so an unconfigured environment cannot accidentally expose a write endpoint.
 */

import { capabilityDigest, timingSafeEqualHex } from "./capability.js";
import { readSingletonHeader } from "./protocol.js";

export const PUBLISHER_SCHEME = "Publisher";
/* Same shape as the driver capability: 256 bits, base64url, opaque. */
export const PUBLISHER_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export const PUBLISHER_AUTH = {
  OK: "OK",
  NOT_CONFIGURED: "NOT_CONFIGURED",
  MISSING: "MISSING",
  MALFORMED: "MALFORMED",
  REJECTED: "REJECTED",
};

/**
 * Extract the token from `Authorization: Publisher <token>`.
 *
 * A driver capability arrives in a JSON body and a driver session in a cookie,
 * so neither can be mistaken for this. A `Bearer` scheme is deliberately not
 * accepted: the scheme name itself keeps the two credential spaces apart.
 *
 * SINGLETON. A duplicated `Authorization` header is joined by the Fetch
 * runtime into one comma-separated value, and this reads it through the same
 * gate the publication control headers use, so a combined credential is
 * refused EXPLICITLY rather than incidentally by the split below. The
 * credential alphabet contains no comma, so the check cannot reject a
 * legitimate value. Nothing about the existing parsing was loosened: every
 * previously-rejected form is still rejected.
 */
export function readPublisherToken(request) {
  const singleton = readSingletonHeader(request.headers, "Authorization");
  if (!singleton.ok) return null;
  const parts = singleton.value.split(" ");
  if (parts.length !== 2) return null;
  if (parts[0] !== PUBLISHER_SCHEME) return null;
  return parts[1];
}

/**
 * Verify a publisher credential.
 *
 * `env.PUBLISHER_KEY_DIGEST` is the hex digest of the machine token, computed
 * exactly like a capability digest (HMAC-SHA-256 under `CAPABILITY_PEPPER` when
 * one is bound, SHA-256 otherwise), so the pepper rotation lever covers this
 * credential too.
 */
export async function verifyPublisher(request, env) {
  const configured = env && env.PUBLISHER_KEY_DIGEST;
  if (typeof configured !== "string" || configured.length !== 64) {
    return { ok: false, reason: PUBLISHER_AUTH.NOT_CONFIGURED };
  }
  const token = readPublisherToken(request);
  if (token === null) return { ok: false, reason: PUBLISHER_AUTH.MISSING };
  if (!PUBLISHER_TOKEN_PATTERN.test(token)) return { ok: false, reason: PUBLISHER_AUTH.MALFORMED };

  const presented = await capabilityDigest(token, env.CAPABILITY_PEPPER);
  if (!timingSafeEqualHex(presented, configured)) {
    return { ok: false, reason: PUBLISHER_AUTH.REJECTED };
  }
  return { ok: true, reason: PUBLISHER_AUTH.OK };
}

/**
 * Digest a machine token for configuration.
 *
 * Used by the local runtime and by operators preparing
 * `wrangler secret put PUBLISHER_KEY_DIGEST`. The raw token stays on the host.
 */
export async function publisherKeyDigest(token, pepper) {
  return capabilityDigest(token, pepper);
}
