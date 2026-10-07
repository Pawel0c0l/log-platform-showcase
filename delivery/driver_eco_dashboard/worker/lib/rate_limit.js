/* Worker-native rate limiting for the unauthenticated session exchange.
 *
 * WHY THIS EXISTS, AND WHAT IT IS NOT
 *
 * `POST /api/session` is the only route a caller can reach before proving
 * anything. Everything behind it — a bounded body read, a peppered digest and
 * a D1 lookup — is cheap per request but not free, and the endpoint is public.
 * This module bounds how often one pre-authentication actor may reach that
 * work. It is a volume control, NOT the authorization boundary: a caller still
 * needs a 256-bit capability to obtain anything, and nothing here makes a
 * wrong capability more or less wrong.
 *
 * WHY THE NATIVE BINDING RATHER THAN A WAF RULE
 *
 * Zone-level WAF rate limiting does not exist on `*.workers.dev`, and this
 * Worker must be verifiable there before it is ever put behind a hostname. The
 * Workers Rate Limiting binding is account-level and runs wherever the Worker
 * runs, so the control travels with the code rather than with the zone.
 *
 * THE ACTOR KEY
 *
 * Before the capability is examined there is no user identity, so the only
 * stable actor available is the network peer. `CF-Connecting-IP` is set by the
 * Cloudflare edge on every request it dispatches and is overwritten rather
 * than forwarded, so a caller cannot choose its own value; the same is not
 * true of `X-Forwarded-For`, which is why that header is never consulted here.
 *
 * Cloudflare cautions that an IP key aggregates everyone behind one NAT or
 * privacy proxy, so the configured policy is deliberately generous (60 calls
 * per 60 seconds — roughly one per second for a whole shared egress address)
 * rather than tight. The failure this control is sized to prevent is automated
 * enumeration volume, not a second visitor in the same office.
 *
 * KNOWN RESIDUAL: the key is the exact address Cloudflare reports. A caller
 * holding an IPv6 prefix can rotate the low bits and obtain a fresh bucket per
 * address. Narrowing IPv6 to its /64 would close that, and is the natural next
 * iteration, but it is a change to the approved actor-key contract and is
 * deliberately not made here.
 *
 * FAIL-CLOSED, ALWAYS
 *
 * Every outcome other than an explicit allow refuses the exchange before the
 * body is read or D1 is touched. In particular a MISSING BINDING refuses:
 * a deployment that lost `[[ratelimits]]` must fail visibly rather than
 * quietly serve an unprotected endpoint. The local runtime and the test
 * harnesses therefore supply a real in-memory limiter rather than relying on
 * an absence being tolerated.
 */

/** Binding name declared in `wrangler.toml` under `[[ratelimits]]`. */
export const SESSION_RATE_LIMIT_BINDING = "SESSION_RATE_LIMIT";

/* Must match `[[ratelimits]].simple` in wrangler.toml. Kept here so the 429's
 * `Retry-After` cannot drift away from the configured window. */
export const SESSION_RATE_LIMIT_REQUESTS = 60;
export const SESSION_RATE_LIMIT_PERIOD_SECONDS = 60;

/* The key is namespaced by operation. If the namespace is ever shared with
 * another binding, `session:` keeps this endpoint's counters separate from
 * anything else that might key on the same address. */
export const SESSION_RATE_LIMIT_KEY_PREFIX = "session:";

/** Trusted client address. Set by the Cloudflare edge; never caller-chosen. */
export const CLIENT_ADDRESS_HEADER = "CF-Connecting-IP";

export const RATE_LIMIT_OUTCOME = {
  /* Under the limit; the exchange may continue. */
  ALLOWED: "ALLOWED",
  /* Over the limit for this actor. 429. */
  LIMITED: "RATE_LIMITED",
  /* No trusted client address, so no per-actor key can be formed. Refused
   * rather than collapsed onto one shared key. */
  NO_ACTOR: "NO_CLIENT_ADDRESS",
  /* The binding is absent from `env`. A misconfigured deployment.
   *
   * Deliberately under 24 characters: lib/log.js redacts any opaque run of 24+
   * word characters as secret-shaped, so a longer name would be scrubbed to
   * "[redacted]" and this outcome would become invisible in exactly the
   * deployment failure it exists to report. The edge-guard suite asserts that
   * every reason in this vocabulary survives the scrubber. */
  UNAVAILABLE: "RATE_LIMITER_MISSING",
  /* The binding threw, or answered in a shape we refuse to interpret. */
  FAILED: "RATE_LIMITER_FAILED",
};

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
/* Hex groups, separators, and the dotted tail of an IPv4-mapped address. A
 * full textual IPv6 address is at most 45 characters. */
const IPV6 = /^[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*$/;

/**
 * The trusted client address, normalised, or `null` when it is absent or not
 * something we are willing to treat as one address.
 *
 * Deliberately conservative in the same way `declaredLength()` is: a header
 * that arrived duplicated is comma-joined, and guessing which half is the peer
 * would either merge two actors onto one key or split one actor across two.
 * Neither is acceptable for a limiter, so it is refused instead.
 */
export function clientActorAddress(request) {
  const header = request.headers.get(CLIENT_ADDRESS_HEADER);
  if (header === null || header === undefined) return null;
  const trimmed = String(header).trim();
  if (trimmed.length === 0 || trimmed.length > 45) return null;
  /* Any whitespace or comma means more than one value reached us. */
  if (/[\s,]/.test(trimmed)) return null;

  const v4 = IPV4.exec(trimmed);
  if (v4) {
    for (let i = 1; i <= 4; i += 1) {
      const octet = v4[i];
      /* "01" and "1" are the same host but different strings, and a leading
       * zero is not a form Cloudflare emits. Refuse rather than normalise. */
      if (octet.length > 1 && octet[0] === "0") return null;
      if (Number(octet) > 255) return null;
    }
    return trimmed;
  }

  if (trimmed.includes(":") && IPV6.test(trimmed)) {
    /* Case is not significant in IPv6 hex, so one host must not be able to
     * produce two buckets by varying it. */
    return trimmed.toLowerCase();
  }
  return null;
}

/** The exact string handed to the binding. Never logged. */
export function sessionRateLimitKey(address) {
  return SESSION_RATE_LIMIT_KEY_PREFIX + address;
}

/**
 * Apply the session-exchange rate limit.
 *
 * Returns `{ outcome }` and nothing else: the actor key is deliberately not
 * part of the result, so a caller cannot log it by spreading the return value
 * into a structured event.
 */
export async function limitSessionExchange(request, env) {
  const binding = env && env[SESSION_RATE_LIMIT_BINDING];
  if (!binding || typeof binding.limit !== "function") {
    return { outcome: RATE_LIMIT_OUTCOME.UNAVAILABLE };
  }

  const address = clientActorAddress(request);
  if (address === null) return { outcome: RATE_LIMIT_OUTCOME.NO_ACTOR };

  let verdict = null;
  try {
    verdict = await binding.limit({ key: sessionRateLimitKey(address) });
  } catch (error) {
    return { outcome: RATE_LIMIT_OUTCOME.FAILED };
  }
  /* `success` must be a real boolean. An undefined or truthy-but-not-true
   * answer is a contract change, not permission to continue. */
  if (!verdict || typeof verdict.success !== "boolean") {
    return { outcome: RATE_LIMIT_OUTCOME.FAILED };
  }
  return { outcome: verdict.success ? RATE_LIMIT_OUTCOME.ALLOWED : RATE_LIMIT_OUTCOME.LIMITED };
}
