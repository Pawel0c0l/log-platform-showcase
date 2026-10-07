/* Bounded request-body reading for the unauthenticated exchange endpoint.
 *
 * THE CONTRACT THIS MODULE NOW ENFORCES, AND WHY IT CHANGED
 *
 * The previous contract made a BYOB reader the thing that had to be true
 * before `POST /api/session` could be released: only a fixed-size read into a
 * buffer WE allocate bounds per-read memory independently of what the peer
 * chunks. Deployed verification of the real Cloudflare runtime settled that
 * question, definitively and negatively: hundreds of observations showed the
 * incoming `Request.body` is not a byte-oriented stream there, so
 * `getReader({ mode: "byob" })` never succeeds and `body_read_mode` is always
 * `"default"`. No compatibility flag changes that — see the README.
 *
 * A release gate that the platform cannot satisfy is not a safety property, so
 * the gate is replaced rather than waived. The bound now comes from REQUEST
 * FRAMING, which the runtime does expose, instead of from reader type, which
 * it does not:
 *
 *   1. DECLARED-SIZE GATE (`parseDeclaredLength` / `requireDeclaredLength`).
 *      Before the body is touched at all, the request must present exactly one
 *      canonical `Content-Length` whose value is within the endpoint ceiling.
 *      Missing, empty, duplicated/comma-joined, signed, fractional,
 *      exponential, non-decimal, out-of-safe-range or oversized declarations
 *      are all refused with ZERO bytes consumed and no reader created. This is
 *      FAIL-CLOSED: an invocation that does not declare an acceptable size is
 *      refused, never promoted to an unbounded read.
 *
 *   2. BOUNDED READ. Only after (1) is the body read, against the effective
 *      ceiling `min(declared, maxBytes)`. Both bounds are enforced while
 *      reading, so the total-bytes ceiling holds even if a caller supplies no
 *      declared length at all.
 *
 *   3. ACTUAL-VERSUS-DECLARED EQUALITY. A successful read must have produced
 *      exactly `declared` bytes. More is refused mid-read; fewer is refused on
 *      completion. A conforming HTTP peer cannot violate this — `Content-Length`
 *      IS the body length — so the equality costs nothing and removes the
 *      question of which of the two numbers downstream code should believe.
 *
 * WHY THIS IS SOUND WITHOUT TRUSTING THE HEADER
 *
 * The declared length is used only to REFUSE, never to authorise a read larger
 * than the endpoint ceiling. A peer that under-declares gains nothing: the read
 * is capped at `min(declared, maxBytes)` and the mismatch is refused. A peer
 * that over-declares is refused before the body is touched. So the memory
 * bound does not depend on the header being honest — only on its presence
 * being required. That is why the missing case fails closed: if Cloudflare ever
 * stopped presenting a length for some request shape, that shape would lose
 * availability, not memory safety.
 *
 * WHAT BYOB IS NOW
 *
 * An opportunistic optimisation and a telemetry signal, nothing more. When the
 * runtime does hand back a byte stream (the local Node byte-stream fixtures do)
 * the fixed-size path runs and bounds per-read cost further. When it does not,
 * the default reader runs and the framing gate above is what bounds the work.
 * NEITHER MODE IS A RELEASE BLOCKER; `mode` is reported for observability.
 *
 * EXACT BYTES
 *
 * A successful read returns `bytes` — the exact concatenated ingress octets,
 * untouched. `text` is a convenience decode of those same octets and is NEVER
 * the authoritative value for anything that is hashed or stored: a lossy
 * decode (`fatal: false`) turns an invalid sequence into U+FFFD, so
 * re-encoding `text` is not guaranteed to reproduce `bytes`. Callers that
 * hash, compare or persist a payload must use `bytes`.
 */

import { HEADER_REJECTION, readSingletonHeader } from "./protocol.js";

export const BODY_REJECTION = {
  DECLARED_TOO_LARGE: "DECLARED_TOO_LARGE",
  STREAM_TOO_LARGE: "STREAM_TOO_LARGE",
  /* The body did not contain exactly `Content-Length` bytes: more was offered
   * (refused mid-read) or fewer arrived (refused on completion). */
  DECLARED_MISMATCH: "DECLARED_MISMATCH",
  UNREADABLE: "UNREADABLE",
};

/* Why the request was refused before any reader existed.
 *
 * Every value is short and underscore-separated on purpose: lib/log.js redacts
 * any opaque run of 24+ word characters as secret-shaped, so a longer name
 * would be scrubbed to "[redacted]" and the diagnostic would vanish in exactly
 * the failure it exists to report. `DECLARED_TOO_LARGE` deliberately reuses the
 * BODY_REJECTION spelling: it is the same refusal it always was.
 *
 * These are INTERNAL diagnostics. The external answer is one status for all of
 * them, so the vocabulary gives a caller no protocol distinction to probe. */
export const CONTENT_LENGTH_REJECTION = {
  MISSING: "DECLARED_MISSING",
  EMPTY: "DECLARED_EMPTY",
  /* Sent more than once, or comma-joined. Indistinguishable at the Fetch layer
   * and both refused: two declared lengths for one body is not a thing to
   * resolve by picking one. */
  AMBIGUOUS: "DECLARED_AMBIGUOUS",
  /* Not `1*DIGIT`: a sign, a decimal point, an exponent, whitespace inside the
   * value, a control character, or anything else that is not a decimal count. */
  MALFORMED: "DECLARED_MALFORMED",
  /* Decimal, but outside the range an exact integer can represent. */
  RANGE: "DECLARED_RANGE",
  /* Well-formed and honest, and larger than this endpoint accepts. */
  TOO_LARGE: "DECLARED_TOO_LARGE",
};

export const BODY_READ_MODE = {
  /* Refused from the declared size; the body was never read. */
  DECLARED: "declared",
  /* Fixed-size buffers we allocated; per-read cost is ours, not the peer's. */
  BYOB: "byob",
  /* Runtime-chosen chunks; logical limit only. */
  DEFAULT: "default",
  /* No stream to meter at all (some runtimes/tests). */
  BUFFERED: "buffered",
};

/* One BYOB read never allocates more than this, whatever the peer sends.
 * Sized so a 512-byte session body completes in one read. */
export const BYOB_BUFFER_BYTES = 1024;

/* RFC 9112 defines `Content-Length` as `1*DIGIT` — a bare decimal count with no
 * sign, no radix prefix, no fraction, no exponent and no units. Everything this
 * grammar excludes ("+1", "-1", "1.0", "1e3", "0x10", "512 ", "1, 1") is
 * excluded because it is not a length, not because it is unusual.
 *
 * LEADING ZEROS ARE ACCEPTED, deliberately. "0512" is `1*DIGIT` and denotes
 * exactly one value, so refusing it would buy no security while adding a way
 * for the browser path to break if an intermediary ever padded the field. The
 * ambiguity this gate exists to refuse is TWO declarations for one body, and
 * that is caught by the singleton/comma check, not by zero-padding. */
const DECIMAL_COUNT = /^[0-9]+$/;

/* Beyond this many significant digits a decimal string is past the range in
 * which a JavaScript number is an exact integer. 15 digits is comfortably
 * inside it; the endpoint ceilings are three and six digits. */
const MAX_SIGNIFICANT_DIGITS = 15;

/**
 * THE declared-body-size parser. Total function, no fallback value.
 *
 * Returns `{ ok: true, length }` for exactly one canonical non-negative
 * integer `Content-Length`, or `{ ok: false, reason }` from
 * `CONTENT_LENGTH_REJECTION`. It never returns a guess, and it never returns
 * "unknown, carry on" — a caller that must not read an undeclared body gets a
 * refusal it has to branch on.
 *
 * Duplication, control characters and surrounding whitespace are decided by
 * `readSingletonHeader` (lib/protocol.js), which is the same singleton contract
 * the publisher control headers use.
 */
export function parseDeclaredLength(headers) {
  const singleton = readSingletonHeader(headers, "Content-Length");
  if (!singleton.ok) {
    if (singleton.reason === HEADER_REJECTION.MISSING) {
      return { ok: false, reason: CONTENT_LENGTH_REJECTION.MISSING };
    }
    if (singleton.reason === HEADER_REJECTION.EMPTY) {
      return { ok: false, reason: CONTENT_LENGTH_REJECTION.EMPTY };
    }
    if (singleton.reason === HEADER_REJECTION.AMBIGUOUS) {
      return { ok: false, reason: CONTENT_LENGTH_REJECTION.AMBIGUOUS };
    }
    /* MALFORMED (control characters, unstripped whitespace) and TOO_LONG. */
    return { ok: false, reason: CONTENT_LENGTH_REJECTION.MALFORMED };
  }

  const raw = singleton.value;
  if (!DECIMAL_COUNT.test(raw)) {
    return { ok: false, reason: CONTENT_LENGTH_REJECTION.MALFORMED };
  }
  /* Compare significance, not string length, so a zero-padded small value is
   * still a small value. */
  const significant = raw.replace(/^0+(?=[0-9])/, "");
  if (significant.length > MAX_SIGNIFICANT_DIGITS) {
    return { ok: false, reason: CONTENT_LENGTH_REJECTION.RANGE };
  }
  const value = Number(significant);
  if (!Number.isSafeInteger(value) || value < 0) {
    return { ok: false, reason: CONTENT_LENGTH_REJECTION.RANGE };
  }
  return { ok: true, length: value };
}

/**
 * The production gate for a route that must not read an unbounded body.
 *
 * Adds the endpoint ceiling to `parseDeclaredLength`: the request must both
 * declare a size and declare one this route is willing to read. Callers apply
 * this BEFORE touching `request.body`; on `ok: false` nothing may be read.
 */
export function requireDeclaredLength(headers, maxBytes) {
  const parsed = parseDeclaredLength(headers);
  if (!parsed.ok) return parsed;
  if (parsed.length > maxBytes) {
    return { ok: false, reason: CONTENT_LENGTH_REJECTION.TOO_LARGE };
  }
  return parsed;
}

/**
 * Legacy convenience: the declared length as a number, or `null` when the
 * request does not present one acceptably.
 *
 * Retained for the routes that treat an absent length as "meter the stream
 * instead" (the authenticated publisher upload, which is credential-gated and
 * has its own 256 KiB ceiling). `POST /api/session` does NOT use this — it uses
 * `requireDeclaredLength`, because for an unauthenticated route "no declared
 * size" must be a refusal rather than a fallback.
 */
export function declaredLength(request) {
  const parsed = parseDeclaredLength(request.headers);
  return parsed.ok ? parsed.length : null;
}

/**
 * Obtain a BYOB reader if — and only if — this stream really supports one.
 * Returns `null` otherwise; never throws.
 */
export function tryByobReader(stream) {
  try {
    const reader = stream.getReader({ mode: "byob" });
    /* A runtime that silently ignored the mode would hand back a default
     * reader, whose reads take no buffer argument. Refuse to call that a BYOB
     * guarantee — a mis-detected mode would be a false telemetry signal, and
     * `readWithByob` would then call `read(view)` on a reader that ignores it. */
    if (!reader || typeof reader.read !== "function" || reader.read.length < 1) {
      if (reader && typeof reader.releaseLock === "function") reader.releaseLock();
      return null;
    }
    return reader;
  } catch (error) {
    return null;
  }
}

/**
 * Read at most `maxBytes` of the request body.
 *
 * Returns `{ ok, mode, bytes, text, bytesRead, largestChunkBytes }` or
 * `{ ok: false, mode, reason, bytesRead, largestChunkBytes }`.
 *
 *   `bytes`              the EXACT ingress octets, in order, unmodified. This
 *                        is the value that must be hashed and stored.
 *   `text`               a lossy UTF-8 decode of `bytes`, for parsing only.
 *   `bytesRead`          bytes actually pulled from the stream.
 *   `largestChunkBytes`  the largest single delivery one read produced. In
 *                        `byob` mode this is bounded by the buffer WE chose; in
 *                        `default` mode it is whatever the runtime handed over,
 *                        which is why it is reported rather than assumed.
 *
 * `options.declaredLength` is the value a caller already validated with
 * `requireDeclaredLength`. Supplying it is what turns the read into an exact
 * contract:
 *
 *   - the effective ceiling becomes `min(declared, maxBytes)`, so BOTH the
 *     declaration and the endpoint ceiling bound the read, and the endpoint
 *     ceiling still holds when no declaration is supplied;
 *   - more bytes than declared is refused mid-read, the stream cancelled;
 *   - fewer bytes than declared is refused on completion. A conforming peer
 *     cannot produce a short body under `Content-Length`; a truncated transfer
 *     is a framing failure, and answering it with a partial document would be
 *     the one outcome worth avoiding.
 *
 * Omitting `declaredLength` preserves the previous behaviour — meter the stream
 * against `maxBytes` and refuse a declaration already known to exceed it — for
 * the credential-gated publisher upload.
 */
export async function readBoundedBody(request, maxBytes, options) {
  const settings = options || {};
  const supplied = settings.declaredLength;
  /* Only an EXPLICITLY supplied declaration activates the exact contract. A
   * route that does not pass one keeps the previous behaviour unchanged — the
   * header still refuses an oversized declaration before the read, but it does
   * not become a promise the body has to match. That keeps the credential-gated
   * publisher upload exactly as it was; the new invariant is scoped to the
   * unauthenticated session exchange that asked for it. */
  const declared = typeof supplied === "number" && Number.isSafeInteger(supplied) && supplied >= 0
    ? supplied
    : null;
  const headerDeclared = declared === null ? declaredLength(request) : declared;

  if (headerDeclared !== null && headerDeclared > maxBytes) {
    /* Nothing is read: the peer already told us it is too large. */
    return {
      ok: false, mode: BODY_READ_MODE.DECLARED,
      reason: BODY_REJECTION.DECLARED_TOO_LARGE, bytesRead: 0, largestChunkBytes: 0,
    };
  }

  /* Defence in depth, in one expression: the read can never exceed the
   * endpoint ceiling, and never exceed what the peer declared either. */
  const ceiling = declared === null ? maxBytes : Math.min(declared, maxBytes);

  const stream = request.body;
  let read = null;
  if (!stream || typeof stream.getReader !== "function") {
    /* No stream to meter: fall back to buffering, which is safe only because
     * the declared-size gate above already ran and the size is checked
     * immediately. */
    let merged = null;
    try {
      /* `arrayBuffer()` is what keeps this path byte-exact; `text()` would
       * already have applied a lossy decode before we could hash anything. */
      if (typeof request.arrayBuffer === "function") {
        merged = new Uint8Array(await request.arrayBuffer());
      } else {
        merged = new TextEncoder().encode(await request.text());
      }
    } catch (error) {
      return {
        ok: false, mode: BODY_READ_MODE.BUFFERED,
        reason: BODY_REJECTION.UNREADABLE, bytesRead: 0, largestChunkBytes: 0,
      };
    }
    const size = merged.byteLength;
    if (size > ceiling) {
      read = {
        ok: false, mode: BODY_READ_MODE.BUFFERED,
        reason: BODY_REJECTION.STREAM_TOO_LARGE, bytesRead: size, largestChunkBytes: size,
      };
    } else {
      read = decode([merged], size, size, BODY_READ_MODE.BUFFERED);
    }
  } else {
    /* BYOB stays opportunistic: where the runtime offers a byte stream the
     * per-read allocation is ours, and where it does not (every observed
     * Cloudflare incoming request) the default reader runs under the same
     * ceiling. Neither mode is a release condition. */
    const byob = tryByobReader(stream);
    read = byob
      ? await readWithByob(byob, ceiling)
      : await readWithDefaultReader(stream.getReader(), ceiling);
  }

  return declared === null ? read : reconcileWithDeclared(read, declared, maxBytes);
}

/**
 * Apply the actual-versus-declared contract to a completed read.
 *
 * Split out so the invariant is stated once for every reader mode, and so the
 * over-run reason names the bound that was actually crossed: crossing the
 * endpoint ceiling is `STREAM_TOO_LARGE`, crossing only the declaration is
 * `DECLARED_MISMATCH`. Both are refused; the distinction is diagnostic.
 */
function reconcileWithDeclared(read, declared, maxBytes) {
  if (!read.ok) {
    if (read.reason === BODY_REJECTION.STREAM_TOO_LARGE && read.bytesRead <= maxBytes) {
      return { ...read, reason: BODY_REJECTION.DECLARED_MISMATCH };
    }
    return read;
  }
  if (read.bytesRead !== declared) {
    /* Short body: the peer declared N and delivered fewer. `bytes` is dropped
     * rather than returned truncated. */
    return {
      ok: false, mode: read.mode, reason: BODY_REJECTION.DECLARED_MISMATCH,
      bytesRead: read.bytesRead, largestChunkBytes: read.largestChunkBytes,
    };
  }
  return read;
}

/**
 * Fixed-size reads into buffers we own. OPPORTUNISTIC: used where the runtime
 * offers a byte stream, and simply absent where it does not.
 *
 * This is the only mode in which the per-read memory cost is independent of
 * what the peer sends: `read(view)` fills at most `view.byteLength` bytes, so
 * one 5 MiB source chunk is delivered across many bounded reads and is refused
 * after the first buffer that crosses the ceiling. That is a genuine extra
 * guarantee where it applies — it is NOT what the endpoint's safety rests on,
 * because Cloudflare's incoming `Request.body` never provides it.
 */
async function readWithByob(reader, maxBytes) {
  const chunks = [];
  let bytesRead = 0;
  let largestChunkBytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read(new Uint8Array(BYOB_BUFFER_BYTES));
      if (done) break;
      if (!value || value.byteLength === 0) continue;
      const chunk = value.slice(0, value.byteLength);
      if (chunk.byteLength > largestChunkBytes) largestChunkBytes = chunk.byteLength;
      bytesRead += chunk.byteLength;
      if (bytesRead > maxBytes) {
        try { await reader.cancel(); } catch (error) { /* already closed */ }
        return {
          ok: false, mode: BODY_READ_MODE.BYOB,
          reason: BODY_REJECTION.STREAM_TOO_LARGE, bytesRead, largestChunkBytes,
        };
      }
      chunks.push(chunk);
    }
  } catch (error) {
    try { await reader.cancel(); } catch (cancelError) { /* already closed */ }
    return {
      ok: false, mode: BODY_READ_MODE.BYOB,
      reason: BODY_REJECTION.UNREADABLE, bytesRead, largestChunkBytes,
    };
  }
  return decode(chunks, bytesRead, largestChunkBytes, BODY_READ_MODE.BYOB);
}

/**
 * Runtime-chunked reads. THE production mode on Cloudflare, and an accepted
 * one.
 *
 * The running total is enforced and the stream is cancelled rather than
 * drained, but ONE read may deliver a chunk the runtime already materialised,
 * larger than the ceiling. `largestChunkBytes` reports exactly that, so no test
 * can accidentally claim a bound this mode does not provide on its own.
 *
 * What makes it safe here is the caller: `POST /api/session` reaches this
 * function only after a declared size <= 512 has been accepted, and a
 * conforming peer cannot then deliver more than it declared — a body longer
 * than `Content-Length` is a framing error, not a larger request. The bound is
 * the gate; this counter is the independent check behind it.
 */
async function readWithDefaultReader(reader, maxBytes) {
  const chunks = [];
  let bytesRead = 0;
  let largestChunkBytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      if (chunk.byteLength > largestChunkBytes) largestChunkBytes = chunk.byteLength;
      bytesRead += chunk.byteLength;
      if (bytesRead > maxBytes) {
        /* Stop pulling immediately. `cancel` releases the rest of the body
         * without reading it, so the remaining megabytes are never pulled —
         * but the chunk already in hand was already materialised. */
        try { await reader.cancel(); } catch (error) { /* already closed */ }
        return {
          ok: false, mode: BODY_READ_MODE.DEFAULT,
          reason: BODY_REJECTION.STREAM_TOO_LARGE, bytesRead, largestChunkBytes,
        };
      }
      chunks.push(chunk);
    }
  } catch (error) {
    try { await reader.cancel(); } catch (cancelError) { /* already closed */ }
    return {
      ok: false, mode: BODY_READ_MODE.DEFAULT,
      reason: BODY_REJECTION.UNREADABLE, bytesRead, largestChunkBytes,
    };
  }
  return decode(chunks, bytesRead, largestChunkBytes, BODY_READ_MODE.DEFAULT);
}

function decode(chunks, bytesRead, largestChunkBytes, mode) {
  const merged = new Uint8Array(bytesRead);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let text = "";
  try {
    text = new TextDecoder("utf-8", { fatal: false }).decode(merged);
  } catch (error) {
    return { ok: false, mode, reason: BODY_REJECTION.UNREADABLE, bytesRead, largestChunkBytes };
  }
  /* `merged` is returned, not a re-encode of `text`: the decode above is lossy
   * by design and must never become the source of truth for a digest. */
  return { ok: true, mode, bytes: merged, text, bytesRead, largestChunkBytes };
}

/**
 * Strictly decode UTF-8, or report failure.
 *
 * Used where the octets are about to be parsed AND persisted: an invalid
 * sequence must be refused rather than silently replaced with U+FFFD, because
 * the replacement would make the parsed document disagree with the bytes that
 * were hashed and stored.
 */
export function decodeUtf8Strict(bytes) {
  try {
    /* `ignoreBOM: true` KEEPS a leading U+FEFF in the decoded string rather
     * than silently dropping it. Host canonical output never carries a BOM, and
     * dropping one would make the parsed document disagree with the octets that
     * were hashed and stored; keeping it makes `JSON.parse` reject, which is
     * the honest outcome. */
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch (error) {
    return null;
  }
}

/**
 * Privacy-safe structured evidence about how a body was read.
 *
 * This is what makes the deployed reader mode observable: `readBoundedBody`
 * has always reported `mode`, but nothing carried it into a log, so a runtime
 * where `Request.body` is not a byte stream was indistinguishable from one
 * where it is. Every field here is a bounded integer, a boolean or a fixed
 * vocabulary string — never a byte of the body.
 *
 * `body_read_mode` is `null` when the declared-size gate refused the request:
 * no reader was ever created, so naming a mode would be a claim about code
 * that did not run. `reader_entered` states that plainly rather than leaving
 * the null ambiguous.
 */
export function bodyReadEvidence(read) {
  if (!read) return { reader_entered: false, body_read_mode: null, bytes_read: 0, largest_chunk_bytes: 0 };
  const entered = read.mode !== BODY_READ_MODE.DECLARED;
  return {
    reader_entered: entered,
    body_read_mode: entered ? read.mode : null,
    bytes_read: Number(read.bytesRead) || 0,
    largest_chunk_bytes: Number(read.largestChunkBytes) || 0,
  };
}
