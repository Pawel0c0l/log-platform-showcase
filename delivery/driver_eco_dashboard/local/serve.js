/* Local runtime for the Driver Eco Dashboard delivery Worker.
 *
 *   node delivery/driver_eco_dashboard/local/serve.js [port]
 *
 * Mounts the real Worker on a Node HTTP server with in-memory D1/R2/ASSETS
 * bindings and one synthetic snapshot, then prints the bootstrap link. No
 * Cloudflare account, no wrangler, no credentials, no remote mutation.
 *
 * The capability is printed ONLY by this local development entrypoint, which
 * exists to be opened by hand; nothing in the Worker or the tests prints one.
 */

import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";

import "./node_runtime.js";

import worker from "../worker/index.js";
import { MemoryD1, MemoryR2, createAssetsBinding, createEnv } from "./memory_bindings.js";
import { payloadDigest } from "../worker/lib/digest.js";
import {
  mintObjectKey, putSnapshotObject, revokeCapability,
} from "../worker/lib/publisher.js";
/* Local scaffolding: a grant with no publication operation behind it. The
 * Worker has no such helper — see local/dev_grants.js. */
import { issueDevCapability } from "./dev_grants.js";
import { D1AuthorizationStore } from "../worker/lib/store.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ASSET_ROOT = path.resolve(HERE, "..", "..", "..", "assets", "driver_eco_dashboard");
const FIXTURE = process.env.ECO_FIXTURE || "ranked_acceptable";
const PORT = Number(process.argv[2] || process.env.PORT || 8788);

const db = new MemoryD1();
const bucket = new MemoryR2();
const env = createEnv({
  db,
  bucket,
  assets: createAssetsBinding(ASSET_ROOT),
  assetRoot: ASSET_ROOT,
  allowInsecureCookies: true,
});

const store = new D1AuthorizationStore(db);
const objectKey = mintObjectKey();
const snapshotText = await readFile(path.join(ASSET_ROOT, "fixtures", `${FIXTURE}.json`), "utf8");
const services = { db, store, bucket, pepper: env.CAPABILITY_PEPPER };
await putSnapshotObject(services, {
  snapshot_object_key: objectKey,
  subject_ref: "local-synthetic-subject",
  body: snapshotText,
});
const grant = await issueDevCapability(
  services,
  { subject_ref: "local-synthetic-subject", snapshot_object_key: objectKey,
    period_type: "weekly", now: Math.floor(Date.now() / 1000) }
);

/* A second grant/snapshot so cross-subject isolation can be exercised by hand. */
const otherKey = mintObjectKey();
await putSnapshotObject(services, {
  snapshot_object_key: otherKey,
  subject_ref: "local-synthetic-subject-b",
  body: await readFile(path.join(ASSET_ROOT, "fixtures", "ranked_safe.json"), "utf8"),
});
const otherGrant = await issueDevCapability(
  services,
  { subject_ref: "local-synthetic-subject-b", snapshot_object_key: otherKey,
    period_type: "monthly", now: Math.floor(Date.now() / 1000) }
);

function toWebRequest(nodeRequest, body, origin) {
  const headers = new Headers();
  for (const [name, value] of Object.entries(nodeRequest.headers)) {
    if (Array.isArray(value)) value.forEach((item) => headers.append(name, item));
    else if (value !== undefined) headers.set(name, value);
  }
  /* Emulate the one piece of edge metadata the Worker depends on. On
   * Cloudflare, CF-Connecting-IP is set by the edge and OVERWRITES whatever the
   * caller sent; this does the same, so a locally supplied header cannot
   * masquerade as a trusted peer address and the session rate limiter sees a
   * real per-connection actor here exactly as it would in production. */
  const peer = nodeRequest.socket && nodeRequest.socket.remoteAddress;
  if (peer) headers.set("CF-Connecting-IP", String(peer).replace(/^::ffff:/, ""));
  else headers.delete("CF-Connecting-IP");
  return new Request(new URL(nodeRequest.url, origin).toString(), {
    method: nodeRequest.method,
    headers,
    body: body && body.length ? body : undefined,
  });
}

const server = http.createServer((nodeRequest, nodeResponse) => {
  const chunks = [];
  nodeRequest.on("data", (chunk) => chunks.push(chunk));
  nodeRequest.on("end", async () => {
    const origin = `http://127.0.0.1:${PORT}`;
    /* LOCAL-ONLY control hooks. These are implemented by this development
     * server, never by the Worker: the deployed boundary exposes no
     * administrative route at all. They let a browser verification script
     * exercise revocation without a remote resource. */
    if (nodeRequest.url === "/__local__/corrupt-b" && nodeRequest.method === "POST") {
      /* Overwrite the second driver's object with an over-wide document that
       * still carries a valid contract_id, to exercise the strict gate. */
      const stored = bucket.objects.get(otherKey);
      const document = JSON.parse(stored.body);
      document.display_notes = "OTHER_DRIVER_PRIVATE_VALUE";
      const replacement = new TextEncoder().encode(JSON.stringify(document));
      /* Recompute the object's payload digest for the replacement bytes. The
       * read path now verifies stored octets against it, and leaving a stale
       * digest here would make this hook exercise the integrity check instead
       * of the strict schema gate it exists to exercise. */
      await bucket.put(otherKey, replacement, {
        customMetadata: Object.assign({}, stored.customMetadata, {
          payload_digest: await payloadDigest(replacement),
        }),
      });
      nodeResponse.writeHead(204).end();
      return;
    }
    if (nodeRequest.url === "/__local__/fail-session-delete" && nodeRequest.method === "POST") {
      db.failSessionDelete = true;
      nodeResponse.writeHead(204).end();
      return;
    }
    if (nodeRequest.url === "/__local__/heal-session-delete" && nodeRequest.method === "POST") {
      db.failSessionDelete = false;
      nodeResponse.writeHead(204).end();
      return;
    }
    if (nodeRequest.url === "/__local__/revoke" && nodeRequest.method === "POST") {
      await revokeCapability(services,
        { capability_id: grant.capability_id, now: Math.floor(Date.now() / 1000) });
      nodeResponse.writeHead(204).end();
      return;
    }

    const request = toWebRequest(nodeRequest, Buffer.concat(chunks), origin);
    const response = await worker.fetch(request, env, {});
    const headers = {};
    for (const [name, value] of response.headers) {
      if (name.toLowerCase() === "set-cookie") {
        headers["set-cookie"] = (headers["set-cookie"] || []).concat([value]);
      } else headers[name] = value;
    }
    nodeResponse.writeHead(response.status, headers);
    const buffer = Buffer.from(await response.arrayBuffer());
    nodeResponse.end(buffer);
  });
});

server.listen(PORT, "127.0.0.1", () => {
  process.stdout.write(
    `Driver Eco Dashboard delivery Worker (local, synthetic data only)\n` +
    `  origin        http://127.0.0.1:${PORT}\n` +
    `  fixture       ${FIXTURE}\n` +
    `  bootstrap     http://127.0.0.1:${PORT}/#k=${grant.capability}\n` +
    `  second driver http://127.0.0.1:${PORT}/#k=${otherGrant.capability}\n` +
    `  (synthetic capabilities, in-memory store; nothing is persisted)\n`
  );
});
