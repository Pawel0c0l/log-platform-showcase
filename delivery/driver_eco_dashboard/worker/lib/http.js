/* Response construction: security headers, cache policy and the error map.
 *
 * Every response the Worker emits goes through here, so no route can forget a
 * header and no error can accidentally describe internal state.
 */

/* Content-Security-Policy.
 *
 * The frontend makes zero external requests, so everything is 'self' and there
 * is no 'unsafe-eval' and no inline <script> (the bootstrap lives in
 * js/boot.js precisely so this stays true).
 *
 * The one concession is inline *style attributes*: the renderer emits layout
 * maths as `style="width:75%"` on bars, rings and axis cells. `style-src-elem`
 * still forbids injected <style> elements, and the fallback `style-src` keeps
 * the page working in browsers without the -elem/-attr split. Allowing style
 * attributes executes no script; every value that reaches one is a number the
 * host computed, and all text is HTML-escaped by the renderer.
 */
const CSP_DIRECTIVES = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "style-src-elem 'self'",
  "style-src-attr 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "manifest-src 'none'",
  "worker-src 'none'",
  "upgrade-insecure-requests",
].join("; ");

const PERMISSIONS_POLICY = [
  "accelerometer=()", "camera=()", "geolocation=()", "gyroscope=()",
  "magnetometer=()", "microphone=()", "payment=()", "usb=()",
  "interest-cohort=()", "browsing-topics=()",
].join(", ");

export function securityHeaders({ secure }) {
  const headers = {
    "Content-Security-Policy": CSP_DIRECTIVES,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    /* No Access-Control-Allow-* header is ever emitted: this boundary is
     * same-origin only, so a cross-origin page cannot read any response. */
  };
  if (secure) {
    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload";
  }
  return headers;
}

/** Sensitive, per-driver, authenticated. Must never reach a shared cache. */
export const PRIVATE_CACHE = "private, no-store, max-age=0, must-revalidate";
/** Non-sensitive, identical for everyone, safe to cache. */
export const ASSET_CACHE = "public, max-age=3600, must-revalidate";

export function withHeaders(response, extra, context) {
  const headers = new Headers(response.headers);
  const base = securityHeaders(context);
  for (const [name, value] of Object.entries(base)) headers.set(name, value);
  for (const [name, value] of Object.entries(extra || {})) headers.set(name, value);
  headers.set("Vary", "Cookie");
  return new Response(response.body, { status: response.status, headers });
}

export function jsonResponse(payload, status, context, extra) {
  return withHeaders(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    }),
    { "Cache-Control": PRIVATE_CACHE, ...(extra || {}) },
    context
  );
}

export function emptyResponse(status, context, extra) {
  return withHeaders(new Response(null, { status }), { "Cache-Control": PRIVATE_CACHE, ...(extra || {}) }, context);
}

/* The driver-facing error vocabulary. The frontend maps these HTTP statuses
 * onto its access states; the body carries a code and nothing else.
 *
 * A revoked capability deliberately answers exactly like an unknown one: the
 * boundary does not confirm that a link ever existed. Likewise a missing R2
 * object answers "unavailable" without saying whether a key was resolved. */
export const DENY = {
  INVALID: { status: 401, code: "INVALID_LINK" },
  EXPIRED: { status: 410, code: "LINK_EXPIRED" },
  UNAVAILABLE: { status: 404, code: "SNAPSHOT_UNAVAILABLE" },
  SERVICE: { status: 503, code: "SERVICE_UNAVAILABLE" },
  BAD_REQUEST: { status: 400, code: "INVALID_LINK" },
  NOT_FOUND: { status: 404, code: "SNAPSHOT_UNAVAILABLE" },
  METHOD: { status: 405, code: "INVALID_LINK" },
  /* Too many pre-authentication exchange attempts from one actor. Deliberately
   * says nothing about the capability that was (or was not) supplied: the
   * limiter runs before the body is read, so this answer is identical whether
   * the request carried a valid link, an invalid one or none at all. The
   * frontend maps an unrecognised status onto SERVICE_UNAVAILABLE, which is
   * the honest access state for "not now". */
  RATE_LIMITED: { status: 429, code: "RATE_LIMITED" },
};

export function denyResponse(kind, context, extra) {
  return jsonResponse({ error: kind.code }, kind.status, context, extra);
}

export function isSecureRequest(request) {
  try {
    return new URL(request.url).protocol === "https:";
  } catch (error) {
    return false;
  }
}
