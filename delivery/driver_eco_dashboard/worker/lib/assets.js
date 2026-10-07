/* Static asset boundary.
 *
 * Only the production dashboard files are reachable. The development preview
 * harness and the synthetic fixture bundle are explicitly not served, so a
 * deployed origin never exposes a fixture selector or a client-wide-looking
 * JSON directory.
 */

const ALLOWED_PATHS = new Set([
  "/",
  "/index.html",
  "/css/dashboard.css",
  "/js/format.js",
  "/js/render.js",
  "/js/snapshot-source.js",
  "/js/app.js",
  "/js/boot.js",
  "/js/capability-bootstrap.js",
]);

/* Never served, whatever the ASSETS binding happens to contain. */
const DENIED_PREFIXES = ["/fixtures/", "/preview", "/README"];

export function isAllowedAssetPath(pathname) {
  if (typeof pathname !== "string" || !pathname.startsWith("/")) return false;
  if (pathname.includes("..") || pathname.includes("//") || pathname.includes("\\")) return false;
  for (const prefix of DENIED_PREFIXES) {
    if (pathname.toLowerCase().startsWith(prefix.toLowerCase())) return false;
  }
  return ALLOWED_PATHS.has(pathname);
}

export function assetContentType(pathname) {
  if (pathname.endsWith(".css")) return "text/css; charset=utf-8";
  if (pathname.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (pathname.endsWith(".woff2")) return "font/woff2";
  return "text/html; charset=utf-8";
}

export function normaliseAssetPath(pathname) {
  return pathname === "/" ? "/index.html" : pathname;
}

export { ALLOWED_PATHS, DENIED_PREFIXES };
