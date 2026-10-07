/* Node compatibility shim for the local runtime and the security harness.
 *
 * The Cloudflare Workers runtime provides `crypto` (WebCrypto) as a global.
 * Node 18 exposes it on `globalThis` only in some entry contexts, so this file
 * installs it before the Worker module is imported. It exists purely so the
 * unmodified Worker source can run under `node`; nothing here is deployed.
 */

import { webcrypto } from "node:crypto";

if (typeof globalThis.crypto === "undefined") {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
}

export const ready = true;
