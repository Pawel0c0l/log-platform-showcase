/* Build a Request the way the Cloudflare edge presents one to the Worker.
 *
 * WHY THIS EXISTS
 *
 * `POST /api/session` now refuses any request that does not carry exactly one
 * canonical `Content-Length` no larger than 512 bytes, BEFORE it opens the
 * body. That is a property of HTTP framing, and the deployed Worker always sees
 * it: a browser `fetch` with a string body emits `Content-Length` itself (the
 * header is forbidden to script precisely because the browser owns it), and the
 * Cloudflare edge synthesises one even for a chunked transfer — both confirmed
 * by the deployed workers.dev verification of this Worker.
 *
 * Node's `new Request(url, { body: "..." })` does NOT populate that header: in
 * undici the length is computed by the HTTP client at dispatch time, and these
 * harnesses never dispatch — they hand the Request object straight to
 * `worker.fetch`. So a raw `new Request` in a harness models a request shape the
 * deployed runtime does not produce, and would make every session test look
 * like a framing failure.
 *
 * `edgeRequest` closes exactly that gap and nothing else.
 *
 * WHAT IT DOES, AND DELIBERATELY DOES NOT, DO
 *
 *   * A body whose exact size is already known (a string, a `Uint8Array`, an
 *     `ArrayBuffer`) gets the `Content-Length` the browser would have sent.
 *     This is the ONLY normalisation.
 *   * An explicit `Content-Length` in `headers` is never overwritten. That is
 *     the seam the negative tests use: a declaration that disagrees with the
 *     body, or is malformed, or is oversized, is constructed here and cannot be
 *     constructed through a browser.
 *   * A `ReadableStream` body is left UNDECLARED unless the caller supplies a
 *     length, because "the peer sent a body with no declared size" is a real
 *     shape the gate has to refuse, and a helper that silently declared one
 *     would make that case untestable.
 *
 * No network, no wrangler, no credentials: this constructs an object.
 */

const ENCODER = new TextEncoder();

/** Exact octet count for a materialised body, or `null` when it is a stream. */
export function bodyByteLength(body) {
  if (body === undefined || body === null) return 0;
  if (typeof body === "string") return ENCODER.encode(body).byteLength;
  if (body instanceof Uint8Array) return body.byteLength;
  if (body instanceof ArrayBuffer) return body.byteLength;
  if (ArrayBuffer.isView(body)) return body.byteLength;
  return null;
}

/**
 * `new Request`, plus the `Content-Length` HTTP framing would already have
 * supplied. Same signature; safe to use for every route.
 */
export function edgeRequest(url, init) {
  const settings = init || {};
  const headers = new Headers(settings.headers || {});
  if (!headers.has("Content-Length")) {
    const size = bodyByteLength(settings.body);
    /* `null` means a stream: left undeclared on purpose (see the header). */
    if (size !== null && settings.body !== undefined && settings.body !== null) {
      headers.set("Content-Length", String(size));
    }
  }
  return new Request(url, { ...settings, headers });
}
