/* Local publisher-facing runtime for the Driver Eco Dashboard Worker.
 *
 *   node delivery/driver_eco_dashboard/local/publisher_serve.js [port]
 *
 * Mounts the REAL Worker on a Node HTTP server with in-memory D1/R2/ASSETS
 * bindings and a configured machine credential, so the host-side publisher can
 * be verified end to end against the actual deployed protocol handling rather
 * than a Python imitation of it. No wrangler, no Cloudflare account, no
 * credentials, no remote resource, no e-mail.
 *
 * Unlike `local/serve.js` this entrypoint pre-provisions NOTHING: there is no
 * dev grant and no seeded snapshot, because the host publisher is the thing
 * under test and it must create its own publication.
 *
 * The machine credential is read from `ECO_PUBLISHER_TOKEN` (or generated and
 * printed on the ready line, which is why this file may only ever be pointed
 * at a local instance).
 *
 * LOCAL-ONLY CONTROL SURFACE
 *
 * `/__local__/inspect` reports counts a verification suite needs — how many
 * publication operations, R2 objects, grants and live grants exist. It is
 * implemented by THIS development server, never by the Worker: the deployed
 * boundary exposes no administrative or enumeration route at all, and nothing
 * under `worker/` imports this file. It returns no capability, no digest of
 * one, and no snapshot bytes.
 */

import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import "./node_runtime.js";

import worker from "../worker/index.js";
import { MemoryD1, MemoryR2, createAssetsBinding, createEnv } from "./memory_bindings.js";
import { generateCapability } from "../worker/lib/capability.js";
import { publisherKeyDigest } from "../worker/lib/publisher_auth.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ASSET_ROOT = path.resolve(HERE, "..", "..", "..", "assets", "driver_eco_dashboard");
const PORT = Number(process.argv[2] || process.env.PORT || 0);
const PEPPER = process.env.ECO_CAPABILITY_PEPPER || "local-synthetic-pepper";
const PUBLISHER_TOKEN = process.env.ECO_PUBLISHER_TOKEN || generateCapability();

const db = new MemoryD1();
const bucket = new MemoryR2();
const env = createEnv({
  db,
  bucket,
  assets: createAssetsBinding(ASSET_ROOT),
  assetRoot: ASSET_ROOT,
  pepper: PEPPER,
  allowInsecureCookies: true,
});
env.PUBLISHER_KEY_DIGEST = await publisherKeyDigest(PUBLISHER_TOKEN, PEPPER);

function toWebRequest(nodeRequest, body, origin) {
  const headers = new Headers();
  for (const [name, value] of Object.entries(nodeRequest.headers)) {
    if (Array.isArray(value)) value.forEach((item) => headers.append(name, item));
    else if (value !== undefined) headers.set(name, value);
  }
  return new Request(new URL(nodeRequest.url, origin).toString(), {
    method: nodeRequest.method,
    headers,
    body: body && body.length ? body : undefined,
  });
}

/* Counts only. Deliberately never a capability, a capability digest, a subject
 * reference or an object body — a verification hook that leaked those would be
 * a worse oracle than no hook at all. */
function inspect() {
  const grants = [...db.capabilities.values()];
  const operations = [...db.publications.values()];
  return {
    operations: operations.length,
    operation_states: operations.map((row) => row.state).sort(),
    bearer_generations: operations.map((row) => Number(row.bearer_generation) || 0),
    objects: bucket.objects.size,
    r2_put_count: bucket.putLog.length,
    grants: grants.length,
    live_grants: grants.filter(
      (row) => !row.revoked_at && !row.rotated_to
    ).length,
    sessions: db.sessions.size,
  };
}

/* Answers "does this string appear anywhere in delivery-boundary state?" with
 * a BOOLEAN. It takes needles and returns verdicts, so a verification suite can
 * prove that a recipient address, a driver identity key or a client code never
 * reached D1 or R2 without the oracle itself having to emit any of that state.
 * The needle is compared against the serialised authorization rows and object
 * metadata, plus the object bodies. */
function leakScan(needles) {
  const haystack = [
    JSON.stringify([...db.capabilities.values()]),
    JSON.stringify([...db.sessions.values()]),
    JSON.stringify([...db.publications.values()]),
    JSON.stringify(db.statementLog),
    JSON.stringify([...bucket.objects.entries()].map(([key, value]) => [
      key,
      value && value.customMetadata ? value.customMetadata : null,
    ])),
    [...bucket.objects.values()]
      .map((value) => (value && value.body ? Buffer.from(value.body).toString("utf8") : ""))
      .join("\n"),
  ].join("\n");
  /* Keyed by POSITION, never by a prefix of the needle: two needles can share
   * a prefix, and a verdict whose label is ambiguous is not a verdict. */
  return needles.map((needle, index) => ({
    index: index,
    length: typeof needle === "string" ? needle.length : 0,
    present: typeof needle === "string" && needle.length >= 4
      && haystack.includes(needle),
  }));
}

const server = http.createServer((nodeRequest, nodeResponse) => {
  const chunks = [];
  nodeRequest.on("data", (chunk) => chunks.push(chunk));
  nodeRequest.on("end", async () => {
    const origin = `http://127.0.0.1:${server.address().port}`;
    if (nodeRequest.url === "/__local__/leakscan" && nodeRequest.method === "POST") {
      let verdicts = {};
      try {
        const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
        verdicts = leakScan(Array.isArray(parsed.needles) ? parsed.needles : []);
      } catch (error) {
        verdicts = { error: true };
      }
      nodeResponse.writeHead(200, { "Content-Type": "application/json" })
        .end(JSON.stringify(verdicts));
      return;
    }
    if (nodeRequest.url === "/__local__/inspect") {
      const body = JSON.stringify(inspect());
      nodeResponse.writeHead(200, { "Content-Type": "application/json" }).end(body);
      return;
    }
    if (nodeRequest.url === "/__local__/shutdown" && nodeRequest.method === "POST") {
      nodeResponse.writeHead(204).end();
      server.close();
      setTimeout(() => process.exit(0), 10);
      return;
    }
    try {
      const request = toWebRequest(nodeRequest, Buffer.concat(chunks), origin);
      const response = await worker.fetch(request, env, {});
      const headers = {};
      response.headers.forEach((value, name) => { headers[name] = value; });
      const payload = Buffer.from(await response.arrayBuffer());
      nodeResponse.writeHead(response.status, headers).end(payload);
    } catch (error) {
      nodeResponse.writeHead(500, { "Content-Type": "application/json" })
        .end(JSON.stringify({ error: "LOCAL_RUNTIME_FAILURE", name: error && error.name }));
    }
  });
});

server.listen(PORT, "127.0.0.1", () => {
  /* One machine-readable ready line. The token is printed because this
   * entrypoint exists to be driven by a local verification suite that has to
   * authenticate to it, and it never leaves this machine. */
  process.stdout.write(JSON.stringify({
    ready: true,
    port: server.address().port,
    base_url: `http://127.0.0.1:${server.address().port}`,
    publisher_token: PUBLISHER_TOKEN,
  }) + "\n");
});
