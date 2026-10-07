/* Structured logging that cannot emit a bearer secret.
 *
 * Every log call goes through `scrubLogFields`, which redacts any value shaped
 * like a capability or session id regardless of which field it was put in. That
 * makes secret-free logging a property of the logger rather than a rule each
 * call site has to remember.
 */

import { CAPABILITY_PATTERN } from "./capability.js";

export const REDACTED = "[redacted]";

/* Anything that looks like one of our 256-bit base64url handles, or any long
 * opaque run that could be one, is redacted. */
const SECRET_LIKE = /(^|[^A-Za-z0-9_-])[A-Za-z0-9_-]{24,}([^A-Za-z0-9_-]|$)/;

function scrubValue(value, depth) {
  if (typeof value === "string") {
    if (CAPABILITY_PATTERN.test(value)) return REDACTED;
    if (SECRET_LIKE.test(value)) return REDACTED;
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean" || value === null) return value;
  if (depth > 4) return REDACTED;
  if (Array.isArray(value)) return value.map((item) => scrubValue(item, depth + 1));
  if (typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value)) out[key] = scrubValue(value[key], depth + 1);
    return out;
  }
  return REDACTED;
}

export function scrubLogFields(fields) {
  return scrubValue(fields || {}, 0);
}

/**
 * Emit one structured line. `event` is a fixed vocabulary; `fields` is scrubbed.
 * Errors are logged by name and message only — never by passing an object that
 * might close over a request body.
 */
export function logEvent(env, level, event, fields) {
  const sink = (env && env.__logSink) || console;
  const line = { level, event, ...scrubLogFields(fields) };
  if (level === "ERROR" && typeof sink.error === "function") sink.error(JSON.stringify(line));
  else if (typeof sink.log === "function") sink.log(JSON.stringify(line));
  return line;
}
