/* Request-shape gate for the publisher transport.
 *
 * WHAT THE REVIEW FOUND
 *
 * Two ways an ambiguous request reached the publication path:
 *
 *   * the media-type check was `startsWith("application/json")`, so
 *     `application/jsonp`, `application/json-patch+json`, `application/json-seq`
 *     and `application/jsonfoo` were all accepted as "JSON";
 *   * a DUPLICATED control header is joined by the Fetch `Headers` object into
 *     one comma-separated value, and `X-Publication-Subject: a` twice became
 *     the subject `"a, a"` — a value the server had never been told to publish
 *     under. Duplicate operation and digest headers happened to fail only
 *     because their downstream validators are narrow, which is luck, not a
 *     contract.
 *
 * SO THIS MODULE IS THE CONTRACT
 *
 * Every security- or state-controlling header on the publisher routes is read
 * through `readSingletonHeader`, and the media type through
 * `parsePublisherContentType`. Both are total functions returning an explicit
 * verdict; neither ever returns a "best guess" value.
 *
 * WHY COMMA REJECTION IS SOUND HERE, AND ONLY HERE
 *
 * The Fetch runtime does not expose the individual field lines of a repeated
 * header (`Headers.getAll` exists only for `Set-Cookie`), so the ONLY runtime
 * evidence of duplication is the comma-joined value. Every header this module
 * guards is a protocol-control identifier drawn from an alphabet that has no
 * comma in it: an operation id (`[A-Za-z0-9_-]{16,64}`), a subject ref, a
 * lower-case hex digest, a hex capability id, a fixed phase word and an
 * `Authorization` credential. So "contains a comma" and "was sent more than
 * once" are the same statement for these fields, and rejecting the comma
 * rejects the ambiguity.
 *
 * This reasoning does NOT generalise: it is deliberately not applied to bodies,
 * to arbitrary request headers, or to any field where a comma is legitimate.
 */

/** Verdicts. A caller must branch on `ok`; there is no fallback value. */
export const HEADER_REJECTION = {
  MISSING: "MISSING",
  /* Sent more than once, or carrying a comma-joined list. Indistinguishable
   * at the Fetch layer, and both are refused. */
  AMBIGUOUS: "AMBIGUOUS",
  /* Control characters, or surrounding whitespace the runtime did not strip. */
  MALFORMED: "MALFORMED",
  EMPTY: "EMPTY",
  TOO_LONG: "TOO_LONG",
};

/* Generous; each field has its own narrower validator downstream. This bound
 * exists so a pathological header cannot be scanned at length. */
const MAX_CONTROL_HEADER_LENGTH = 8192;

/* Anything below 0x20, plus DEL. A header value may not contain these; a
 * runtime that let one through is not something to normalise around. */
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

/**
 * Read exactly one unambiguous value for a protocol-control header.
 *
 * Returns `{ ok: true, value }` or `{ ok: false, reason }`. The value is
 * returned verbatim — no trimming, no case folding, no splitting — because a
 * caller that has to repair a control header is a caller accepting ambiguity.
 */
export function readSingletonHeader(headers, name) {
  const raw = headers && typeof headers.get === "function" ? headers.get(name) : null;
  if (raw === null || raw === undefined) {
    return { ok: false, reason: HEADER_REJECTION.MISSING };
  }
  if (typeof raw !== "string") return { ok: false, reason: HEADER_REJECTION.MALFORMED };
  if (raw.length > MAX_CONTROL_HEADER_LENGTH) {
    return { ok: false, reason: HEADER_REJECTION.TOO_LONG };
  }
  if (raw.length === 0) return { ok: false, reason: HEADER_REJECTION.EMPTY };
  if (CONTROL_CHARACTERS.test(raw)) return { ok: false, reason: HEADER_REJECTION.MALFORMED };
  /* THE duplicate-header refusal. See the module header for why a comma is
   * conclusive for these particular fields. */
  if (raw.indexOf(",") !== -1) return { ok: false, reason: HEADER_REJECTION.AMBIGUOUS };
  /* `Headers` normalises surrounding whitespace; if any survives, the value did
   * not come from a conforming runtime and is not repaired here. */
  if (raw !== raw.trim()) return { ok: false, reason: HEADER_REJECTION.MALFORMED };
  return { ok: true, value: raw };
}

/* ------------------------------------------------------------ media type -- */

/**
 * THE accepted publisher media type. The request body is the exact canonical
 * snapshot octets, so there is one correct answer and no negotiation.
 */
export const PUBLISHER_MEDIA_TYPE = "application/json";

/**
 * The one optional parameter. `charset` is redundant for `application/json`
 * (RFC 8259 fixes the encoding as UTF-8) but publishing clients emit it
 * routinely, so it is accepted — and ONLY with the one value that agrees with
 * the strict UTF-8 decode the route performs anyway.
 */
const ACCEPTED_CHARSET = "utf-8";

export const MEDIA_TYPE_REJECTION = {
  MISSING: "MISSING",
  AMBIGUOUS: "AMBIGUOUS",
  MALFORMED: "MALFORMED",
  /* The confirmed defect: `application/jsonp` under a `startsWith` check. */
  UNSUPPORTED_TYPE: "UNSUPPORTED_TYPE",
  UNSUPPORTED_PARAMETER: "UNSUPPORTED_PARAMETER",
};

/* RFC 9110 token: no separators, no whitespace, no quotes. */
const TOKEN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;

/**
 * Parse and accept a media type against the exact documented contract.
 *
 * Accepted, and nothing else:
 *
 *   application/json
 *   application/json; charset=utf-8            (any case, optional quotes,
 *                                               optional surrounding spaces)
 *
 * Rejected, explicitly and by name in the tests: `application/jsonp`,
 * `application/json-patch+json`, `application/json-seq`, `application/jsonfoo`,
 * `text/json`, `text/application/json`, `application/json;charset=evil`, a
 * missing header, and any duplicated/comma-joined representation.
 *
 * This is a parser, not a prefix test. The type and subtype are compared for
 * EQUALITY after case folding, which is the whole point: no lookalike subtype
 * can satisfy an equality check.
 */
export function parsePublisherContentType(raw) {
  if (raw === null || raw === undefined) {
    return { ok: false, reason: MEDIA_TYPE_REJECTION.MISSING };
  }
  if (typeof raw !== "string") return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };
  if (raw.length === 0) return { ok: false, reason: MEDIA_TYPE_REJECTION.MISSING };
  if (raw.length > MAX_CONTROL_HEADER_LENGTH) {
    return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };
  }
  if (CONTROL_CHARACTERS.test(raw)) return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };
  /* Two Content-Type field lines are joined with a comma. One media type is
   * being declared for one body; two is not a thing to resolve. */
  if (raw.indexOf(",") !== -1) return { ok: false, reason: MEDIA_TYPE_REJECTION.AMBIGUOUS };

  const segments = raw.split(";");
  const essence = segments[0].trim().toLowerCase();
  if (essence.length === 0) return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };

  const slash = essence.indexOf("/");
  if (slash === -1) return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };
  const type = essence.slice(0, slash);
  const subtype = essence.slice(slash + 1);
  /* `text/application/json` dies here: `application/json` is not a token. */
  if (!TOKEN.test(type) || !TOKEN.test(subtype)) {
    return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };
  }
  /* EQUALITY. Not `startsWith`. */
  if (essence !== PUBLISHER_MEDIA_TYPE) {
    return { ok: false, reason: MEDIA_TYPE_REJECTION.UNSUPPORTED_TYPE, essence: essence };
  }

  let charset = null;
  for (let index = 1; index < segments.length; index += 1) {
    const parameter = segments[index].trim();
    if (parameter.length === 0) return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };
    const equals = parameter.indexOf("=");
    if (equals === -1) return { ok: false, reason: MEDIA_TYPE_REJECTION.MALFORMED };
    const key = parameter.slice(0, equals).trim().toLowerCase();
    let value = parameter.slice(equals + 1).trim();
    if (value.length >= 2 && value.charAt(0) === '"' && value.charAt(value.length - 1) === '"') {
      value = value.slice(1, -1);
    }
    /* Exactly one parameter is supported, and only one value for it. An
     * unknown parameter is refused rather than ignored: ignoring is how
     * `application/json;charset=evil` would have been "accepted anyway". */
    if (key !== "charset") {
      return { ok: false, reason: MEDIA_TYPE_REJECTION.UNSUPPORTED_PARAMETER, parameter: key };
    }
    if (charset !== null) return { ok: false, reason: MEDIA_TYPE_REJECTION.AMBIGUOUS };
    if (value.toLowerCase() !== ACCEPTED_CHARSET) {
      return { ok: false, reason: MEDIA_TYPE_REJECTION.UNSUPPORTED_PARAMETER, parameter: "charset" };
    }
    charset = ACCEPTED_CHARSET;
  }

  return { ok: true, media_type: PUBLISHER_MEDIA_TYPE, charset: charset };
}

/** Convenience for a route that only needs the verdict. */
export function readPublisherContentType(headers) {
  const singleton = readSingletonHeader(headers, "Content-Type");
  if (!singleton.ok) {
    return {
      ok: false,
      reason: singleton.reason === HEADER_REJECTION.AMBIGUOUS
        ? MEDIA_TYPE_REJECTION.AMBIGUOUS
        : (singleton.reason === HEADER_REJECTION.MISSING || singleton.reason === HEADER_REJECTION.EMPTY
            ? MEDIA_TYPE_REJECTION.MISSING
            : MEDIA_TYPE_REJECTION.MALFORMED),
    };
  }
  return parsePublisherContentType(singleton.value);
}
