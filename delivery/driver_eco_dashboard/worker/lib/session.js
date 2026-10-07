/* Browser session: an opaque, HttpOnly, same-origin cookie bound server-side to
 * one capability grant.
 *
 * The raw capability never becomes the session credential — it is exchanged
 * once and discarded, so the long-lived bearer secret in the driver's mailbox
 * is not the thing sitting in the browser for the rest of the visit.
 */

export const SESSION_TTL_SECONDS = 30 * 60;
export const SECURE_COOKIE_NAME = "__Host-eco_dash";
/* Local verification only; requires an explicit env opt-in that production
 * configuration never sets. `__Host-` cookies require Secure, which a plain
 * http://127.0.0.1 origin cannot satisfy. */
export const INSECURE_COOKIE_NAME = "eco_dash_local";

export function cookieName(secure) {
  return secure ? SECURE_COOKIE_NAME : INSECURE_COOKIE_NAME;
}

export function readCookie(request, name) {
  const header = request.headers.get("Cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    if (part.slice(0, index).trim() === name) return part.slice(index + 1).trim();
  }
  return null;
}

export function buildSetCookie(value, { secure, maxAge }) {
  const attributes = [
    `${cookieName(secure)}=${value}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Strict",
    `Max-Age=${maxAge}`,
  ];
  if (secure) attributes.push("Secure");
  return attributes.join("; ");
}

export function buildClearCookie({ secure }) {
  const attributes = [`${cookieName(secure)}=`, "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"];
  if (secure) attributes.push("Secure");
  return attributes.join("; ");
}
