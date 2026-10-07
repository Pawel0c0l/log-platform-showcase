/* Driver Eco Dashboard V1 — Cloudflare Worker authorization boundary.
 *
 *   capability link (fragment)
 *     -> POST /api/session      exchange, once, never in a URL the server sees
 *     -> HttpOnly session cookie, bound server-side to exactly one grant
 *     -> GET /api/snapshot      no parameters at all
 *     -> private R2 object resolved server-side
 *
 * The browser can never name a driver, a client, a subject or an object key.
 * There is no endpoint that takes one.
 */

import {
  capabilityDigest, isWellFormedCapability, generateSessionId,
  isWellFormedSessionId, sessionDigest, timingSafeEqualHex,
} from "./lib/capability.js";
import { D1AuthorizationStore, CAPABILITY_STATE, classifyCapability, classifySession,
         deleteSnapshotsBefore } from "./lib/store.js";
import { HARD_RETENTION_MONTHS, HARD_RETENTION_POLICY_ID,
         HARD_RETENTION_ENFORCEMENT_LEAD_SECONDS,
         enforcementCutoffSeconds, hardRetentionCutoffSeconds } from "./lib/retention_policy.js";
import {
  DENY, denyResponse, emptyResponse, jsonResponse, withHeaders,
  isSecureRequest, PRIVATE_CACHE, ASSET_CACHE,
} from "./lib/http.js";
import {
  SESSION_TTL_SECONDS, buildClearCookie, buildSetCookie, cookieName, readCookie,
} from "./lib/session.js";
import {
  checkCanonicalJsonForm, validateSnapshotText, verifyStoredBytes, verifySubjectBinding,
} from "./lib/snapshot.js";
import {
  bodyReadEvidence, decodeUtf8Strict, readBoundedBody, requireDeclaredLength,
} from "./lib/body.js";
import {
  RATE_LIMIT_OUTCOME, SESSION_RATE_LIMIT_PERIOD_SECONDS, limitSessionExchange,
} from "./lib/rate_limit.js";
import { isPayloadDigest, payloadDigest } from "./lib/digest.js";
import { verifyPublisher } from "./lib/publisher_auth.js";
import {
  PUBLICATION_RESULT, isWellFormedOperationId, publishSnapshot,
  recordDeliveryPhase, recoverLostBearer,
} from "./lib/publication.js";
import { capabilityTtlSeconds, isSupportedPeriodType } from "./lib/capability_ttl.js";
import {
  inspectSnapshotObject, mintObjectKey, putSnapshotObject,
} from "./lib/publisher.js";
import {
  HEADER_REJECTION, parsePublisherContentType, readPublisherContentType, readSingletonHeader,
} from "./lib/protocol.js";
import { assetContentType, isAllowedAssetPath, normaliseAssetPath } from "./lib/assets.js";
import { logEvent } from "./lib/log.js";

const MAX_SESSION_REQUEST_BYTES = 512;
/* The publisher body is one canonical snapshot; the browser payload budget is
 * 60 kB, so this ceiling is generous while still bounded. */
const MAX_PUBLISH_REQUEST_BYTES = 256 * 1024;
const MAX_PUBLISH_CONTROL_BYTES = 1024;

function nowSeconds(env) {
  /* Tests inject a clock; production uses wall time. */
  return typeof env.__now === "function" ? env.__now() : Math.floor(Date.now() / 1000);
}

function allowInsecureCookies(env) {
  return String(env.ALLOW_INSECURE_COOKIES || "") === "1";
}

function contextFor(request, env) {
  const secure = isSecureRequest(request);
  return { secure, cookieSecure: secure };
}

/* Same-origin enforcement for the state-changing exchange. The session cookie
 * is SameSite=Strict and no CORS header is ever emitted, so a cross-origin page
 * cannot read a response; this additionally refuses to *act* on one. */
function originAllowed(request) {
  const origin = request.headers.get("Origin");
  if (!origin) return true; /* same-origin fetch may omit Origin on GET; POST below requires it */
  try {
    return new URL(origin).origin === new URL(request.url).origin;
  } catch (error) {
    return false;
  }
}

async function handleSessionExchange(request, env, context) {
  if (request.method !== "POST") return denyResponse(DENY.METHOD, context);

  const origin = request.headers.get("Origin");
  if (!origin || !originAllowed(request)) {
    logEvent(env, "WARN", "session_exchange_rejected", { reason: "ORIGIN" });
    return denyResponse(DENY.INVALID, context);
  }
  /* A session may only be issued over a secure origin. The insecure cookie
   * name exists for local verification and requires an explicit opt-in that no
   * deployed environment sets. */
  if (!context.cookieSecure && !allowInsecureCookies(env)) {
    logEvent(env, "ERROR", "session_exchange_rejected", { reason: "INSECURE_ORIGIN" });
    return denyResponse(DENY.SERVICE, context);
  }

  /* RATE LIMIT. Placed here on purpose: the request is now known to be a
   * POST to /api/session from our own origin over a secure scheme, and NOTHING
   * expensive has happened yet — no body has been touched, no digest computed,
   * no D1 query issued. Everything below this point either reads the body or
   * queries the authorization store, so this is the last point at which a
   * refusal costs the isolate nothing.
   *
   * It also sits ABOVE the content-type check deliberately: an attacker who
   * sends the wrong media type would otherwise get an uncounted 400 and could
   * probe at any rate. Every well-formed attempt at this endpoint is counted.
   *
   * Fail-closed in every direction. A missing binding is a broken deployment,
   * not permission to serve the endpoint unprotected, and answers 503 rather
   * than quietly continuing. Only the exceeded-limit case is 429. */
  const limit = await limitSessionExchange(request, env);
  if (limit.outcome === RATE_LIMIT_OUTCOME.LIMITED) {
    /* The actor key is a client address and is never logged. */
    logEvent(env, "WARN", "session_exchange_rejected", { reason: RATE_LIMIT_OUTCOME.LIMITED });
    return denyResponse(DENY.RATE_LIMITED, context, {
      "Retry-After": String(SESSION_RATE_LIMIT_PERIOD_SECONDS),
    });
  }
  if (limit.outcome !== RATE_LIMIT_OUTCOME.ALLOWED) {
    logEvent(env, "ERROR", "session_exchange_rejected", { reason: limit.outcome });
    return denyResponse(DENY.SERVICE, context);
  }

  /* Same strict parser the publisher route uses: an exact media type, not a
   * prefix. This route's body is a small JSON object rather than canonical
   * snapshot octets, but there is no reason for it to be the looser of the
   * two, and `application/jsonp` is not JSON here either. */
  if (!readPublisherContentType(request.headers).ok) {
    return denyResponse(DENY.BAD_REQUEST, context);
  }

  /* REQUEST-FRAMING GATE. This is the memory-safety boundary for the endpoint,
   * and it runs BEFORE `request.body` is touched in any way.
   *
   * The request must present exactly one canonical `Content-Length` no larger
   * than MAX_SESSION_REQUEST_BYTES. Missing, empty, duplicated/comma-joined,
   * signed, fractional, exponential, non-decimal, out-of-range and oversized
   * declarations are all refused here, with zero bytes consumed, no reader
   * created, no digest computed, no D1 statement issued and no Set-Cookie.
   *
   * FAIL-CLOSED. An absent declaration is a refusal, never a fallback to an
   * unbounded read. Cloudflare presents a `Content-Length` on every observed
   * incoming request — it synthesises one even for a chunked transfer — so the
   * browser flow is unaffected; if that ever changed, the affected request
   * shape would lose availability rather than the endpoint losing its bound.
   *
   * The header is not TRUSTED, only REQUIRED: it can bound the read downward
   * but never upward, and `readBoundedBody` independently counts what it
   * actually pulls against both this value and the endpoint ceiling.
   *
   * Every failure answers with the same status as any other malformed
   * exchange, so the vocabulary below stays an internal diagnostic rather than
   * a protocol distinction an attacker can probe. */
  const framing = requireDeclaredLength(request.headers, MAX_SESSION_REQUEST_BYTES);
  if (!framing.ok) {
    /* `bodyReadEvidence(null)` is the honest record of a request whose body was
     * never opened: reader_entered=false, body_read_mode=null, bytes_read=0. */
    logEvent(env, "WARN", "session_exchange_rejected", {
      reason: framing.reason, ...bodyReadEvidence(null),
    });
    return denyResponse(DENY.BAD_REQUEST, context);
  }

  /* Bounded read, now under a size the peer already committed to:
   *   - the effective ceiling is min(declared, MAX_SESSION_REQUEST_BYTES);
   *   - more bytes than declared is refused mid-read and the stream cancelled;
   *   - fewer is refused on completion, so no partial document is parsed;
   *   - BYOB is used when the runtime offers a byte stream and the default
   *     reader when it does not. Both satisfy the same ceiling, and NEITHER is
   *     a release condition — the framing gate above is. */
  const read = await readBoundedBody(request, MAX_SESSION_REQUEST_BYTES, {
    declaredLength: framing.length,
  });
  /* How the body was (or was not) read, as bounded integers and a fixed
   * vocabulary — never a byte of it. Carried onto every rejection below so a
   * synthetic probe against the deployed runtime can establish which reader
   * mode actually ran there. See `bodyReadEvidence` in lib/body.js. */
  const readEvidence = bodyReadEvidence(read);
  if (!read.ok) {
    logEvent(env, "WARN", "session_exchange_rejected", { reason: read.reason, ...readEvidence });
    return denyResponse(DENY.BAD_REQUEST, context);
  }

  let raw = null;
  try {
    const body = JSON.parse(read.text);
    /* Exactly one capability. A duplicated/array parameter is refused rather
     * than resolved to "the first one". */
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      logEvent(env, "WARN", "session_exchange_rejected", { reason: "BODY_SHAPE", ...readEvidence });
      return denyResponse(DENY.BAD_REQUEST, context);
    }
    raw = body.capability;
    if (typeof raw !== "string") {
      logEvent(env, "WARN", "session_exchange_rejected", { reason: "BODY_SHAPE", ...readEvidence });
      return denyResponse(DENY.BAD_REQUEST, context);
    }
  } catch (error) {
    /* The parse failure is never logged with its input. */
    logEvent(env, "WARN", "session_exchange_rejected", { reason: "BODY", ...readEvidence });
    return denyResponse(DENY.BAD_REQUEST, context);
  }

  if (!isWellFormedCapability(raw)) {
    logEvent(env, "WARN", "session_exchange_rejected", { reason: "MALFORMED", ...readEvidence });
    return denyResponse(DENY.INVALID, context);
  }

  const store = new D1AuthorizationStore(env.AUTHORIZATION_DB);
  const now = nowSeconds(env);
  let record = null;
  try {
    const digest = await capabilityDigest(raw, env.CAPABILITY_PEPPER);
    record = await store.findCapabilityByDigest(digest);
  } catch (error) {
    logEvent(env, "ERROR", "authorization_store_failure", { stage: "lookup", name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }

  const state = classifyCapability(record, now);
  if (state === CAPABILITY_STATE.EXPIRED) {
    logEvent(env, "INFO", "session_exchange_denied", { reason: "EXPIRED", ...readEvidence });
    return denyResponse(DENY.EXPIRED, context);
  }
  if (state !== CAPABILITY_STATE.ACTIVE) {
    /* UNKNOWN and REVOKED answer identically: the boundary never confirms that
     * a link existed or that it was withdrawn. The read evidence attached here
     * is what a synthetic unknown-capability probe reads back to prove the
     * deployed reader mode: the body was read successfully and bounded, and
     * the capability behind it is still never named. */
    logEvent(env, "INFO", "session_exchange_denied", { reason: "NOT_ACTIVE", ...readEvidence });
    return denyResponse(DENY.INVALID, context);
  }

  const sessionId = generateSessionId();
  const ttl = Math.min(
    SESSION_TTL_SECONDS,
    Math.max(0, Number(record.expires_at) - now)
  );
  if (ttl <= 0) return denyResponse(DENY.EXPIRED, context);

  try {
    await store.insertSession({
      session_digest: await sessionDigest(sessionId, env.CAPABILITY_PEPPER),
      capability_id: record.capability_id,
      created_at: now,
      expires_at: now + ttl,
      epoch: Number(record.session_epoch) || 0,
    });
  } catch (error) {
    logEvent(env, "ERROR", "authorization_store_failure", { stage: "session_insert", name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }

  logEvent(env, "INFO", "session_established", { ttl_seconds: ttl });
  return emptyResponse(204, context, {
    "Set-Cookie": buildSetCookie(sessionId, { secure: context.cookieSecure, maxAge: ttl }),
  });
}

async function resolveSession(request, env, context) {
  const value = readCookie(request, cookieName(context.cookieSecure));
  if (!value || !isWellFormedSessionId(value)) return { ok: false, deny: DENY.INVALID };

  const store = new D1AuthorizationStore(env.AUTHORIZATION_DB);
  let row = null;
  try {
    row = await store.findSessionByDigest(await sessionDigest(value, env.CAPABILITY_PEPPER));
  } catch (error) {
    logEvent(env, "ERROR", "authorization_store_failure", { stage: "session_lookup", name: error && error.name });
    return { ok: false, deny: DENY.SERVICE };
  }

  const state = classifySession(row, nowSeconds(env));
  if (state === CAPABILITY_STATE.ACTIVE) return { ok: true, grant: row, store };
  if (state === CAPABILITY_STATE.EXPIRED) return { ok: false, deny: DENY.INVALID, state };
  return { ok: false, deny: DENY.INVALID, state };
}

async function handleSnapshot(request, env, context) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return denyResponse(DENY.METHOD, context);
  }
  /* The endpoint takes no input. Any query string is a probe, not a request. */
  const url = new URL(request.url);
  if (url.search) {
    logEvent(env, "WARN", "snapshot_rejected", { reason: "UNEXPECTED_QUERY" });
    return denyResponse(DENY.INVALID, context);
  }

  const session = await resolveSession(request, env, context);
  if (!session.ok) {
    logEvent(env, "INFO", "snapshot_denied", { reason: session.state || "NO_SESSION" });
    return denyResponse(session.deny, context);
  }

  /* The object key comes from the grant row, never from the request. */
  const objectKey = session.grant.snapshot_object_key;
  if (typeof objectKey !== "string" || objectKey.length === 0) {
    logEvent(env, "ERROR", "snapshot_unavailable", { reason: "NO_KEY_BOUND" });
    return denyResponse(DENY.SERVICE, context);
  }

  let object = null;
  try {
    object = await env.SNAPSHOTS.get(objectKey);
  } catch (error) {
    logEvent(env, "ERROR", "snapshot_storage_failure", { name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }
  if (!object) {
    logEvent(env, "INFO", "snapshot_unavailable", { reason: "NOT_FOUND" });
    return denyResponse(DENY.UNAVAILABLE, context);
  }

  /* The object must prove it was published for the subject this grant
   * authorises. A right-key/wrong-person mix-up, or an object copied to
   * another key, fails here before a single byte is parsed. */
  const binding = await verifySubjectBinding(object, session.grant, env.CAPABILITY_PEPPER);
  if (!binding.ok) {
    logEvent(env, "ERROR", "snapshot_rejected", { reason: binding.reason, detail: binding.detail });
    return denyResponse(DENY.SERVICE, context);
  }

  /* Read the OCTETS, not `object.text()`: the integrity check below is only
   * meaningful over the bytes R2 actually holds. */
  let storedBytes = null;
  try {
    storedBytes = new Uint8Array(await object.arrayBuffer());
  } catch (error) {
    logEvent(env, "ERROR", "snapshot_storage_failure", { name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }

  /* Stored-byte integrity, against two independent authorities:
   *   1. the object's own `payload_digest` metadata (required);
   *   2. the publication ledger's digest for whichever operation owns this
   *      key, when one does — a different store, so a rewrite of body plus
   *      metadata does not satisfy it.
   * A grant issued outside the publication path has no ledger row; authority 1
   * still applies and the served event records which authorities ran. */
  let ledgerRow = null;
  try {
    ledgerRow = await session.store.findPublicationDigestByObjectKey(objectKey);
  } catch (error) {
    logEvent(env, "ERROR", "authorization_store_failure", { stage: "publication_lookup", name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }
  const integrity = await verifyStoredBytes(
    storedBytes, object, ledgerRow ? ledgerRow.payload_digest : null
  );
  if (!integrity.ok) {
    logEvent(env, "ERROR", "snapshot_rejected", { reason: integrity.reason, detail: integrity.detail });
    return denyResponse(DENY.SERVICE, context);
  }

  const text = decodeUtf8Strict(storedBytes);
  if (text === null) {
    logEvent(env, "ERROR", "snapshot_rejected", { reason: "NOT_UTF8" });
    return denyResponse(DENY.SERVICE, context);
  }

  const validation = validateSnapshotText(text);
  if (!validation.ok) {
    /* Fail closed: a corrupt, mis-versioned or over-wide object is never
     * forwarded, and the reason is not disclosed to the browser. */
    logEvent(env, "ERROR", "snapshot_rejected", { reason: validation.reason, path: validation.path });
    return denyResponse(DENY.SERVICE, context);
  }

  logEvent(env, "INFO", "snapshot_served", { ledger_verified: !!integrity.ledger_verified });
  return withHeaders(
    new Response(request.method === "HEAD" ? null : validation.body, {
      status: 200,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    }),
    { "Cache-Control": PRIVATE_CACHE },
    context
  );
}

/**
 * End a session.
 *
 * CONTRACT: a 204 means the server-side session is gone, so a copied cookie
 * cannot be replayed. Clearing the browser cookie is not logout — it only hides
 * the credential from this browser while the grant stays live elsewhere.
 *
 * If server-side invalidation fails the response is 503 and the cookie is
 * deliberately LEFT IN PLACE: the session is genuinely still valid, and keeping
 * the cookie is what makes a retry able to terminate it. Clearing it would
 * strand a live session with no client able to end it.
 */
async function handleSessionEnd(request, env, context) {
  if (request.method !== "POST") return denyResponse(DENY.METHOD, context);
  if (!originAllowed(request)) return denyResponse(DENY.INVALID, context);

  const value = readCookie(request, cookieName(context.cookieSecure));
  if (!value || !isWellFormedSessionId(value)) {
    /* Nothing to invalidate; clearing a malformed cookie is honest. */
    return emptyResponse(204, context, {
      "Set-Cookie": buildClearCookie({ secure: context.cookieSecure }),
    });
  }

  try {
    const store = new D1AuthorizationStore(env.AUTHORIZATION_DB);
    await store.deleteSession(await sessionDigest(value, env.CAPABILITY_PEPPER));
  } catch (error) {
    logEvent(env, "ERROR", "authorization_store_failure", { stage: "session_delete", name: error && error.name });
    /* No Set-Cookie: the credential must survive so the client can retry. */
    return denyResponse(DENY.SERVICE, context);
  }

  logEvent(env, "INFO", "session_ended", {});
  return emptyResponse(204, context, {
    "Set-Cookie": buildClearCookie({ secure: context.cookieSecure }),
  });
}

async function handleAsset(request, env, context) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return denyResponse(DENY.METHOD, context);
  }
  const url = new URL(request.url);
  if (!isAllowedAssetPath(url.pathname)) return denyResponse(DENY.NOT_FOUND, context);

  const path = normaliseAssetPath(url.pathname);
  let response = null;
  try {
    response = await env.ASSETS.fetch(new Request(new URL(path, url.origin).toString(), { method: "GET" }));
  } catch (error) {
    logEvent(env, "ERROR", "asset_failure", { name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }
  if (!response || response.status !== 200) return denyResponse(DENY.NOT_FOUND, context);

  /* index.html is the bootstrap page and must never be cached publicly; the
   * static asset bundle is identical for everyone and may be. */
  const isDocument = path === "/index.html";
  return withHeaders(
    new Response(request.method === "HEAD" ? null : response.body, {
      status: 200,
      headers: { "Content-Type": assetContentType(path) },
    }),
    { "Cache-Control": isDocument ? PRIVATE_CACHE : ASSET_CACHE },
    context
  );
}


/* ------------------------------------------------------------- publisher -- */
/*
 * Write transport for the calculation host. Narrow on purpose:
 *   - a separate machine credential, never a driver capability or session;
 *   - no caller-chosen R2 key — the Worker mints it;
 *   - no caller-supplied binding — the Worker derives it from the grant;
 *   - the payload must pass the same strict schema gate the driver path uses;
 *   - every write is scoped to one publication operation id, so retries are
 *     idempotent and a conflicting payload under a reused id is refused.
 *
 * REQUEST BYTE CONTRACT
 *
 *   POST /api/publish
 *   Authorization:                  Publisher <machine credential>
 *   Content-Type:                   application/json
 *                                   (or `application/json; charset=utf-8`)
 *   X-Publication-Operation:        opaque operation id, host-chosen
 *   X-Publication-Subject:          opaque subject ref, never an identity
 *   X-Publication-Payload-Digest:   sha256 hex of the body, lower case
 *   <body>                          THE canonical snapshot bytes, raw
 *
 * PROTOCOL SHAPE IS EXACT, NOT APPROXIMATE
 *
 * The media type is PARSED and compared for equality — the previous
 * `startsWith("application/json")` accepted `application/jsonp`,
 * `application/json-patch+json` and `application/json-seq`. Only the two forms
 * above are accepted; every parameter other than `charset=utf-8` is refused
 * rather than ignored.
 *
 * Every header above is a SINGLETON. A duplicated header is joined by the
 * Fetch runtime into one comma-separated value, and a duplicated
 * `X-Publication-Subject` therefore became the subject `"a, a"` — a subject
 * nobody asked to publish under. Each is now read through
 * `readSingletonHeader`, which refuses any combined form. See `lib/protocol.js`
 * for why a comma is conclusive for these particular fields and is not applied
 * anywhere a comma is legitimate.
 *
 * All of this is resolved BEFORE any store is touched, so a request with an
 * invalid shape creates zero operations, zero R2 objects, zero grants and zero
 * bearers.
 *
 * The body is the snapshot and nothing else. Publication metadata travels in
 * authenticated headers precisely so the snapshot never becomes a nested JSON
 * value: recovering an exact nested byte range after parsing is not reliable,
 * and "close enough" is what the byte-integrity defect was made of.
 *
 * The Worker recomputes the digest over the exact ingress octets, refuses a
 * mismatch, validates those octets, and stores THOSE OCTETS — never a
 * re-serialisation of what it parsed.
 */

async function authorisePublisher(request, env, context) {
  const verdict = await verifyPublisher(request, env);
  if (verdict.ok) return null;
  logEvent(env, "WARN", "publisher_auth_denied", { reason: verdict.reason });
  /* Every failure answers identically: the boundary does not say whether the
   * transport is configured, whether a credential was presented, or whether it
   * was merely wrong. */
  return denyResponse(DENY.NOT_FOUND, context);
}

function publisherJson(payload, status, context) {
  return jsonResponse(payload, status, context);
}

/* One services bundle for every publication route, so no route can reach a
 * different set of primitives than the others. */
function publicationServices(env) {
  const bucket = env.SNAPSHOTS;
  const services = {
    db: env.AUTHORIZATION_DB,
    store: new D1AuthorizationStore(env.AUTHORIZATION_DB),
    bucket: bucket,
    pepper: env.CAPABILITY_PEPPER,
    mintObjectKey: mintObjectKey,
    mintGrantId: mintGrantId,
  };
  /* Returns an explicit ABSENT / PRESENT_VALID / PRESENT_INVALID / UNREADABLE
   * verdict. There is deliberately no boolean-existence probe on this bundle
   * any more: a route that could ask "is it there?" could act on an error as
   * if it were an absence, which is the defect this replaced. */
  services.inspectObject = (params) => inspectSnapshotObject(services, params);
  services.putObject = (params) => putSnapshotObject(services, params);
  return services;
}

/* ---------------------------------------------------------- request shape -- */

/* Every security- or state-controlling publisher header, read as a singleton.
 * The route resolves ALL of them before it touches any store, so an ambiguous
 * request has zero side effects — no operation row, no R2 access, no grant. */
function readPublicationHeaders(request, names) {
  const values = {};
  for (const [field, header] of names) {
    const verdict = readSingletonHeader(request.headers, header);
    if (!verdict.ok) {
      return { ok: false, header: header, reason: verdict.reason };
    }
    values[field] = verdict.value;
  }
  return { ok: true, values: values };
}

/* `Content-Type` on a BODYLESS publisher route.
 *
 * `/api/publish/recover` and `/api/publish/delivery` carry no body, so a media
 * type is not required and requiring one would refuse conforming clients. What
 * is refused is an AMBIGUOUS one: two `Content-Type` field lines are joined by
 * the runtime into a comma-separated value, and no request that reaches a
 * state-changing route may carry a control header the server cannot read as
 * exactly one value. When a media type IS declared here it must also be the
 * one documented publisher media type, parsed for equality — the same contract
 * `/api/publish` applies, never a prefix match.
 *
 * Returns a rejection in the `readPublicationHeaders` shape, or `null`. */
function checkOptionalPublisherContentType(request) {
  const raw = request.headers.get("Content-Type");
  if (raw === null || raw === undefined) return null;
  const singleton = readSingletonHeader(request.headers, "Content-Type");
  if (!singleton.ok) {
    return { header: "Content-Type", reason: singleton.reason };
  }
  const media = parsePublisherContentType(singleton.value);
  if (!media.ok) return { header: "Content-Type", reason: media.reason };
  return null;
}

/* One error shape for every rejected control header. The caller learns that
 * the request shape was refused, not which header nor what the server saw. */
function controlHeaderRejection(env, route, rejection, context) {
  logEvent(env, "WARN", "publisher_request_shape_rejected", {
    route: route, header: rejection.header, reason: rejection.reason,
  });
  return publisherJson({
    error: rejection.reason === HEADER_REJECTION.AMBIGUOUS
      ? "AMBIGUOUS_CONTROL_HEADER"
      : "INVALID_CONTROL_HEADER",
  }, 400, context);
}

/* THE one response mapping for a failed object-integrity gate.
 *
 * Both operations that mutate or advance authorization state for an existing
 * publication — `/api/publish` retry and `/api/publish/recover` — run the same
 * `inspectSnapshotObject` contract and answer its refusals identically. A
 * second, route-local rendering of these verdicts is exactly how the two paths
 * drifted apart in the first place, so there is only this one.
 *
 * `integrity_reason` is logged and NEVER returned: which invariant failed
 * describes a private object, and the publisher's next step is the same either
 * way. Returns `null` when the result is not an object-state refusal.
 */
function objectStateRefusal(result, env, route, context) {
  if (result.status === PUBLICATION_RESULT.OBJECT_INTEGRITY_FAILURE) {
    /* The owned object is absent-under-an-authoritative-grant, or exists and
     * fails an authoritative invariant — ledger digest, metadata digest,
     * digest algorithm or subject binding. Nothing was rewritten, no grant was
     * created or superseded, no generation advanced, no bearer exists. */
    logEvent(env, "ERROR", "publication_object_integrity_failure", {
      route: route,
      operation_state: result.operation && result.operation.state,
      integrity_reason: result.integrity_reason || null,
    });
    return publisherJson({
      error: "OBJECT_INTEGRITY_FAILURE",
      status: result.status,
      next_action: result.next_action,
      operation: result.operation,
      bearer_available: false,
    }, 409, context);
  }
  if (result.status === PUBLICATION_RESULT.OBJECT_UNREADABLE) {
    /* The object's state could NOT be determined. Distinct from absence and
     * distinct from corruption: zero puts, zero grants, zero supersessions,
     * zero ledger movement, and the operation is left exactly where it was. */
    logEvent(env, "ERROR", "publication_object_unreadable", {
      route: route,
      operation_state: result.operation && result.operation.state,
      integrity_reason: result.integrity_reason || null,
    });
    return publisherJson({
      error: "OBJECT_UNREADABLE",
      status: result.status,
      next_action: result.next_action,
      operation: result.operation,
      bearer_available: false,
    }, 503, context);
  }
  return null;
}

async function handlePublish(request, env, context) {
  if (request.method !== "POST") return denyResponse(DENY.METHOD, context);
  const denied = await authorisePublisher(request, env, context);
  if (denied) return denied;

  /* PROTOCOL SHAPE FIRST. Nothing below this block reaches D1 or R2, so a
   * request with an ambiguous or unsupported shape creates no operation, no
   * object, no grant and no bearer. */
  const headers = readPublicationHeaders(request, [
    ["operationId", "X-Publication-Operation"],
    ["subjectRef", "X-Publication-Subject"],
    ["declaredDigest", "X-Publication-Payload-Digest"],
    /* WHICH REPORTING PERIOD THIS GRANT BELONGS TO. It decides the capability
     * lifetime (`lib/capability_ttl.js`) and there is no default, so it is a
     * singleton control header like the other three: absent, empty or
     * duplicated is a refusal, never a guess. */
    ["periodType", "X-Publication-Period"],
  ]);
  if (!headers.ok) return controlHeaderRejection(env, "publish", headers, context);
  const { operationId, subjectRef, declaredDigest, periodType } = headers.values;

  if (!isWellFormedOperationId(operationId)) {
    return publisherJson({ error: "INVALID_OPERATION_ID" }, 400, context);
  }
  if (subjectRef.length > MAX_PUBLISH_CONTROL_BYTES) {
    return publisherJson({ error: "INVALID_SUBJECT" }, 400, context);
  }
  if (!isPayloadDigest(declaredDigest)) {
    return publisherJson({ error: "INVALID_PAYLOAD_DIGEST" }, 400, context);
  }
  /* FAIL CLOSED ON AN UNKNOWN PERIOD. A period type this build does not
   * implement has no lifetime, and inventing one — weekly because it is
   * shorter, monthly because it is safer — would hand a driver a link whose
   * expiry nobody decided. Refused here, before the operation exists, before
   * R2 is touched and before any grant can be minted. */
  if (!isSupportedPeriodType(periodType)) {
    logEvent(env, "WARN", "publisher_request_shape_rejected", {
      route: "publish", header: "X-Publication-Period", reason: "UNKNOWN_PERIOD_TYPE",
    });
    return publisherJson({ error: "INVALID_PERIOD_TYPE" }, 400, context);
  }
  /* Exact media type, parsed — not `startsWith`. `application/jsonp` and every
   * other JSON-prefix lookalike is refused here. */
  const media = readPublisherContentType(request.headers);
  if (!media.ok) {
    logEvent(env, "WARN", "publisher_request_shape_rejected", {
      route: "publish", header: "Content-Type", reason: media.reason,
    });
    return publisherJson({ error: "INVALID_CONTENT_TYPE" }, 400, context);
  }

  const read = await readBoundedBody(request, MAX_PUBLISH_REQUEST_BYTES);
  if (!read.ok || !(read.bytes instanceof Uint8Array)) {
    logEvent(env, "WARN", "publisher_body_rejected", { reason: read.reason, bytes_read: read.bytesRead });
    return publisherJson({ error: "PAYLOAD_REJECTED" }, 400, context);
  }

  /* THE canonical snapshot octets. From here to R2 this value is never
   * decoded-and-re-encoded, never parsed-and-re-serialised, and never replaced
   * by a validated rebuild. Everything below either reads it or refuses it. */
  const snapshotBytes = read.bytes;

  /* Computed here, over the exact ingress octets — never taken from the
   * caller. A request carrying digest(A) with body(B) is refused, in either
   * direction, before any state exists. */
  const actualDigest = await payloadDigest(snapshotBytes);
  if (!timingSafeEqualHex(actualDigest, declaredDigest)) {
    logEvent(env, "WARN", "publisher_payload_rejected", { reason: "DIGEST_MISMATCH" });
    return publisherJson({ error: "PAYLOAD_DIGEST_MISMATCH" }, 400, context);
  }

  /* Strict UTF-8. A lossy decode would substitute U+FFFD and make the parsed
   * document disagree with the octets that were hashed and are about to be
   * stored, which is precisely the class of divergence this route now forbids. */
  const snapshotText = decodeUtf8Strict(snapshotBytes);
  if (snapshotText === null) {
    logEvent(env, "WARN", "publisher_payload_rejected", { reason: "NOT_UTF8" });
    return publisherJson({ error: "PAYLOAD_NOT_CANONICAL" }, 422, context);
  }

  /* Defence in depth: the host already ran value-level privacy validation and
   * canonical serialisation; the delivery boundary still refuses anything that
   * is not exactly the v1 browser contract. The rebuild it produces is used to
   * DECIDE, never to store. */
  const validation = validateSnapshotText(snapshotText);
  if (!validation.ok) {
    logEvent(env, "WARN", "publisher_payload_rejected", { reason: validation.reason, path: validation.path });
    return publisherJson({ error: "PAYLOAD_NOT_CANONICAL" }, 422, context);
  }

  /* The host is the canonical serialisation authority; this refuses ingress
   * that is plainly not in its canonical form (insignificant whitespace,
   * unsorted keys). It deliberately does not re-encode numbers or strings —
   * a second serialiser that disagreed on one float would be an outage, not a
   * control. See `checkCanonicalJsonForm`. */
  const canonical = checkCanonicalJsonForm(snapshotText);
  if (!canonical.ok) {
    logEvent(env, "WARN", "publisher_payload_rejected", { reason: "NOT_CANONICAL_FORM", detail: canonical.detail });
    return publisherJson({ error: "PAYLOAD_NOT_CANONICAL" }, 422, context);
  }

  let result = null;
  try {
    result = await publishSnapshot(publicationServices(env), {
      operation_id: operationId,
      subject_ref: subjectRef,
      payload_digest: actualDigest,
      /* The exact verified ingress octets. NOT `validation.body`: that is a
       * re-serialisation of the parsed document and, for canonical host output,
       * is not the same byte sequence — which is exactly how the ledger digest
       * came to identify bytes that existed nowhere. */
      body: snapshotBytes,
      /* THE one authoritative mapping, applied at the only moment a lifetime
       * is chosen: weekly 10 days, monthly 60 days. */
      ttl_seconds: capabilityTtlSeconds(periodType),
      now: nowSeconds(env),
    });
  } catch (error) {
    logEvent(env, "ERROR", "publication_failure", { name: error && error.name, message: error && error.message });
    return denyResponse(DENY.SERVICE, context);
  }

  const objectRefusal = objectStateRefusal(result, env, "publish", context);
  if (objectRefusal) return objectRefusal;

  if (result.status === PUBLICATION_RESULT.CONFLICT) {
    logEvent(env, "WARN", "publication_conflict", { operation_state: result.operation && result.operation.state });
    return publisherJson({
      error: "OPERATION_CONFLICT",
      status: result.status,
      next_action: result.next_action,
    }, 409, context);
  }

  if (result.status === PUBLICATION_RESULT.PUBLISHED) {
    logEvent(env, "INFO", "publication_completed", { operation_state: result.operation.state });
    /* The raw capability is returned exactly once, here, to exactly one of any
     * number of concurrent callers. */
    return publisherJson({
      status: result.status,
      next_action: result.next_action,
      operation: result.operation,
      capability: result.capability,
      capability_id: result.capability_id,
      expires_at: result.expires_at,
      bearer_available: true,
    }, 201, context);
  }

  /* Every other outcome is a replay. It never carries a bearer, and it always
   * says what the host must do instead of leaving "issue again" available as a
   * plausible reading. */
  logEvent(env, "INFO", "publication_replayed", {
    result: result.status, operation_state: result.operation && result.operation.state,
  });
  return publisherJson({
    status: result.status,
    next_action: result.next_action,
    operation: result.operation,
    bearer_available: false,
    bearer_recoverable: !!result.bearer_recoverable,
  }, 200, context);
}

async function handlePublishRecover(request, env, context) {
  if (request.method !== "POST") return denyResponse(DENY.METHOD, context);
  const denied = await authorisePublisher(request, env, context);
  if (denied) return denied;

  const headers = readPublicationHeaders(request, [
    ["operationId", "X-Publication-Operation"],
    /* RECOVERY MINTS A REPLACEMENT GRANT, so it writes a fresh `expires_at`
     * and needs the same period-aware policy the original publication used.
     *
     * WHY THE HOST IS TRUSTED TO RESTATE IT, AND WHY THAT IS NOT A HOLE. The
     * operation id is a SHA-256 over the logical delivery identity, and the
     * period type is one of the hashed fields
     * (`delivery_contract.derive_operation_id`). An operation published as
     * weekly therefore CANNOT be recovered as monthly without being a
     * different operation id — a different operation, with its own snapshot
     * and its own grant. The header restates a fact the operation id already
     * binds; it does not choose one. */
    ["periodType", "X-Publication-Period"],
  ]);
  if (!headers.ok) return controlHeaderRejection(env, "recover", headers, context);
  const contentType = checkOptionalPublisherContentType(request);
  if (contentType) return controlHeaderRejection(env, "recover", contentType, context);
  const { operationId, periodType } = headers.values;
  if (!isWellFormedOperationId(operationId)) {
    return publisherJson({ error: "INVALID_OPERATION_ID" }, 400, context);
  }
  if (!isSupportedPeriodType(periodType)) {
    logEvent(env, "WARN", "publisher_request_shape_rejected", {
      route: "recover", header: "X-Publication-Period", reason: "UNKNOWN_PERIOD_TYPE",
    });
    return publisherJson({ error: "INVALID_PERIOD_TYPE" }, 400, context);
  }

  try {
    const result = await recoverLostBearer(publicationServices(env), {
      operation_id: operationId,
      mint_id: mintGrantId(),
      ttl_seconds: capabilityTtlSeconds(periodType),
      now: nowSeconds(env),
    });
    if (result.status === PUBLICATION_RESULT.RECOVERED) {
      logEvent(env, "INFO", "publication_bearer_recovered", {
        bearer_generation: result.operation.bearer_generation,
      });
      return publisherJson({
        status: result.status,
        next_action: result.next_action,
        operation: result.operation,
        capability: result.capability,
        capability_id: result.capability_id,
        expires_at: result.expires_at,
        bearer_available: true,
      }, 200, context);
    }
    if (result.status === PUBLICATION_RESULT.UNKNOWN_OPERATION) {
      return publisherJson({ error: "UNKNOWN_OPERATION" }, 404, context);
    }
    /* The object-integrity gate refused: the authoritative snapshot object is
     * absent, incoherent or unreadable, so no replacement bearer may be issued
     * for it. Zero D1 authorization mutation happened — the refusal is a
     * precondition, evaluated before the recovery transaction is attempted. */
    const objectRefusal = objectStateRefusal(result, env, "recover", context);
    if (objectRefusal) return objectRefusal;
    logEvent(env, "WARN", "publication_bearer_not_recoverable", { reason: result.reason });
    return publisherJson({
      status: result.status,
      reason: result.reason,
      next_action: result.next_action || null,
      operation: result.operation || null,
      bearer_available: false,
    }, 409, context);
  } catch (error) {
    logEvent(env, "ERROR", "publication_store_failure", { stage: "recover", name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }
}

async function handlePublishDelivery(request, env, context) {
  if (request.method !== "POST") return denyResponse(DENY.METHOD, context);
  const denied = await authorisePublisher(request, env, context);
  if (denied) return denied;

  /* All three are state-controlling, so all three are singletons. The
   * capability header in particular decides WHICH grant is terminalised. */
  const headers = readPublicationHeaders(request, [
    ["operationId", "X-Publication-Operation"],
    ["phase", "X-Publication-Phase"],
    /* The host must name the capability it is actually delivering. Without it,
     * a delivery decided before a recovery could terminalise the operation on
     * a bearer the driver never received. */
    ["capabilityId", "X-Publication-Capability"],
  ]);
  if (!headers.ok) return controlHeaderRejection(env, "delivery", headers, context);
  const contentType = checkOptionalPublisherContentType(request);
  if (contentType) return controlHeaderRejection(env, "delivery", contentType, context);
  const { operationId, phase, capabilityId } = headers.values;
  if (!isWellFormedOperationId(operationId)) {
    return publisherJson({ error: "INVALID_OPERATION_ID" }, 400, context);
  }
  if (phase !== "INTENT" && phase !== "DELIVERED") {
    return publisherJson({ error: "INVALID_PHASE" }, 400, context);
  }
  if (typeof capabilityId !== "string" || !/^[0-9a-f]{32}$/.test(capabilityId)) {
    return publisherJson({ error: "INVALID_CAPABILITY_ID" }, 400, context);
  }

  try {
    const result = await recordDeliveryPhase(publicationServices(env), {
      operation_id: operationId,
      phase: phase,
      capability_id: capabilityId,
      now: nowSeconds(env),
    });
    if (result.status === PUBLICATION_RESULT.UNKNOWN_OPERATION) {
      return publisherJson({ error: "UNKNOWN_OPERATION" }, 404, context);
    }
    if (result.status === PUBLICATION_RESULT.CAPABILITY_SUPERSEDED ||
        result.status === PUBLICATION_RESULT.CONFLICT) {
      logEvent(env, "WARN", "publication_delivery_refused", {
        result: result.status, operation_state: result.operation && result.operation.state,
      });
      return publisherJson({ status: result.status, operation: result.operation }, 409, context);
    }
    return publisherJson({ status: result.status, operation: result.operation }, 200, context);
  } catch (error) {
    logEvent(env, "ERROR", "publication_store_failure", { stage: "delivery", name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }
}

/* One compaction batch. Large enough that a normal weekly run clears the whole
 * backlog in one call, small enough that the statement always finishes inside
 * D1's budget on a table nobody has compacted before. */
const MAX_SESSION_COMPACTION_BATCH = 1000;

/* Hard-retention batch sizes. Deliberately smaller than the session batch:
 * these statements carry `NOT EXISTS` subqueries and, for R2, network round
 * trips, and the sweep is designed to make bounded progress on every call
 * rather than to finish in one. */
const MAX_PUBLICATION_RETENTION_BATCH = 500;
const MAX_CAPABILITY_RETENTION_BATCH = 500;
const MAX_SNAPSHOT_RETENTION_OBJECTS = 1000;

/**
 * Retire dead authorization state. PUBLISHER-AUTHENTICATED HOUSEKEEPING.
 *
 * WHAT IT REMOVES, AND WHY ONLY THAT.
 *
 * EXPIRED SESSIONS, yes. A session row authorises nothing once its own expiry
 * or its grant's expiry has passed — `classifySession` refuses it — and it is
 * the one table here that grows with driver traffic rather than with
 * publications. Removing dead rows changes no request's outcome and keeps live
 * authorization state bounded by the validity windows that produced it.
 *
 * EXPIRED CAPABILITY GRANTS, ONLY ONCE THEY ARE PAST THE HARD-RETENTION
 * CEILING. The tombstone below is why they are not removed at expiry; the
 * global 13-calendar-month retention policy is why "not at expiry" no longer
 * means "never". A grant is removed when its expiry is older than
 * `hardRetentionCutoffSeconds` — roughly seven months after even a monthly link
 * stopped working — together with its publication ledger row and its snapshot
 * object. Until then:
 *
 *   * an `eco_capability` row holds NO secret. The raw bearer was returned once
 *     to the publisher and never stored; the row keeps a digest (an HMAC under
 *     the pepper, when one is bound), an opaque `subject_ref` and an opaque
 *     object key. There is nothing here for a cleanup to destroy — the secret
 *     material that DOES need destroying is the host-side copy, and the host
 *     retires that on its own schedule;
 *   * the row is already not live authorization. `classifyCapability` reads
 *     `expires_at` and answers EXPIRED, so the grant stops working at its
 *     expiry regardless of whether the row still exists;
 *   * the row is what makes `LINK_EXPIRED` distinguishable. Deleting it turns
 *     "this link has expired" into "this link never existed", which is a worse
 *     answer for the driver holding a two-month-old e-mail and a worse answer
 *     for an operator asked what happened to it;
 *   * `eco_session` references `eco_capability`, so a deletion would also have
 *     to reason about the rows this same call is removing.
 *
 * So expiry is ENFORCED, then TOMBSTONED, then — at the ceiling — erased. See
 * `schema/001_authorization.sql` and `ops/retention_registry.py`.
 *
 * THE SNAPSHOT GOES WITH IT. `deleteSnapshotsBefore` removes R2 objects
 * uploaded before the same cutoff. This is the one place the product decision
 * "an expired capability never deletes the historical report it pointed at"
 * meets the owner decision "nothing is kept beyond 13 calendar months": both
 * hold, because the report outlives the link by months and the platform by
 * nothing.
 *
 * DETERMINISTIC AND IDEMPOTENT. The batch is a pure function of the clock: it
 * can only remove rows that were already unusable, so repeating it, running two
 * concurrently, or interrupting one midway removes a subset of the same dead
 * set and never touches a live grant or a live session. `batch_full` tells the
 * caller whether another call would do more work.
 */
async function handlePublishMaintenance(request, env, context) {
  if (request.method !== "POST") return denyResponse(DENY.METHOD, context);
  /* The same machine credential as every other publisher route, and the same
   * 404 for everyone else: this is not a public maintenance endpoint. */
  const denied = await authorisePublisher(request, env, context);
  if (denied) return denied;

  const contentType = checkOptionalPublisherContentType(request);
  if (contentType) return controlHeaderRejection(env, "maintenance", contentType, context);

  try {
    const store = new D1AuthorizationStore(env.AUTHORIZATION_DB);
    const now = nowSeconds(env);
    const removed = await store.deleteExpiredSessions(
      now, MAX_SESSION_COMPACTION_BATCH);

    /* ORDER IS THE CONTRACT, not a preference. Sessions first (they reference
     * grants), then publication ledger rows (they reference grants), then the
     * grants themselves, then the R2 objects. Every step is independently
     * bounded and independently idempotent, so a failure at any point leaves a
     * consistent database and the next call resumes exactly where this one
     * stopped. */
    /* The ENFORCEMENT cutoff, not the bare deadline: maintenance is weekly, so
     * anything whose deadline falls before the next guaranteed call must go on
     * this one. Both are reported, so an operator can see the policy and the
     * lead that makes a periodic sweep satisfy it. */
    const deadline = hardRetentionCutoffSeconds(now);
    const cutoff = enforcementCutoffSeconds(now);
    const publicationsRemoved = await store.deletePublicationsBefore(
      cutoff, MAX_PUBLICATION_RETENTION_BATCH);
    const capabilitiesRemoved = await store.deleteRetiredCapabilitiesBefore(
      cutoff, now, MAX_CAPABILITY_RETENTION_BATCH);
    const oldestRetainedIssue = await store.oldestRetainedCapabilityIssue(cutoff);

    /* R2 is a separate service: a bucket outage must not undo the D1 work that
     * has already committed, and must not make the whole maintenance call look
     * like a failure. It is reported as its own outcome instead. */
    let snapshots = { examined: 0, deleted: 0, truncated: false, error: null };
    if (env.SNAPSHOTS && typeof env.SNAPSHOTS.list === "function") {
      try {
        snapshots = {
          ...(await deleteSnapshotsBefore(env.SNAPSHOTS, cutoff, {
            maxObjects: MAX_SNAPSHOT_RETENTION_OBJECTS,
          })),
          error: null,
        };
      } catch (error) {
        snapshots = {
          examined: 0, deleted: 0, truncated: false,
          error: (error && error.name) || "R2_LIST_FAILED",
        };
        logEvent(env, "ERROR", "snapshot_retention_failed",
                 { stage: "maintenance", name: snapshots.error });
      }
    }

    logEvent(env, "INFO", "authorization_state_compacted", {
      expired_sessions_removed: removed,
      policy_id: HARD_RETENTION_POLICY_ID,
      retention_months: HARD_RETENTION_MONTHS,
      hard_retention_cutoff: cutoff,
      hard_retention_deadline: deadline,
      publications_removed: publicationsRemoved,
      capabilities_removed: capabilitiesRemoved,
      snapshots_examined: snapshots.examined,
      snapshots_removed: snapshots.deleted,
    });
    return publisherJson({
      status: "COMPACTED",
      expired_sessions_removed: removed,
      batch_full: removed >= MAX_SESSION_COMPACTION_BATCH,
      /* Stated in the response so a host operator never has to infer it from an
       * absent number: an expired grant is retained as a tombstone until the
       * ceiling, and this is the call that finally removes it. */
      expired_capabilities_retained: true,
      hard_retention: {
        policy_id: HARD_RETENTION_POLICY_ID,
        retention_months: HARD_RETENTION_MONTHS,
        cutoff: cutoff,
        deadline: deadline,
        enforcement_lead_seconds: HARD_RETENTION_ENFORCEMENT_LEAD_SECONDS,
        publications_removed: publicationsRemoved,
        capabilities_removed: capabilitiesRemoved,
        snapshots_examined: snapshots.examined,
        snapshots_removed: snapshots.deleted,
        snapshots_truncated: snapshots.truncated,
        snapshot_error: snapshots.error,
        batch_full: (
          publicationsRemoved >= MAX_PUBLICATION_RETENTION_BATCH
          || capabilitiesRemoved >= MAX_CAPABILITY_RETENTION_BATCH
          || snapshots.truncated === true
        ),
        /* Oldest grant ISSUE instant still present past the cutoff. `null` is
         * the compliant steady state; a value means something is protected by a
         * foreign key and the next call has more to do. */
        oldest_retained_issue: oldestRetainedIssue,
      },
    }, 200, context);
  } catch (error) {
    logEvent(env, "ERROR", "publication_store_failure",
             { stage: "maintenance", name: error && error.name });
    return denyResponse(DENY.SERVICE, context);
  }
}

/* 128 CSPRNG bits, hex. Used for capability ids on the publication path. */
function mintGrantId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) out += bytes[i].toString(16).padStart(2, "0");
  return out;
}

export default {
  async fetch(request, env, ctx) {
    const context = contextFor(request, env);
    let url = null;
    try {
      url = new URL(request.url);
    } catch (error) {
      return denyResponse(DENY.BAD_REQUEST, context);
    }

    try {
      if (url.pathname === "/api/session") return await handleSessionExchange(request, env, context);
      if (url.pathname === "/api/session/end") return await handleSessionEnd(request, env, context);
      if (url.pathname === "/api/snapshot") return await handleSnapshot(request, env, context);
      /* Publisher transport. Machine-authenticated, never reachable with a
       * driver credential, and answering 404 to every unauthenticated caller. */
      if (url.pathname === "/api/publish") return await handlePublish(request, env, context);
      if (url.pathname === "/api/publish/recover") return await handlePublishRecover(request, env, context);
      if (url.pathname === "/api/publish/delivery") return await handlePublishDelivery(request, env, context);
      if (url.pathname === "/api/publish/maintenance") return await handlePublishMaintenance(request, env, context);
      if (url.pathname.startsWith("/api/")) return denyResponse(DENY.NOT_FOUND, context);
      return await handleAsset(request, env, context);
    } catch (error) {
      /* Last-resort guard: an unexpected throw must not surface a stack, a
       * request body or a binding name to the browser. */
      logEvent(env, "ERROR", "unhandled_failure", { name: error && error.name });
      return denyResponse(DENY.SERVICE, context);
    }
  },
};
