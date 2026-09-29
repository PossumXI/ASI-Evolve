// Test-only HTTP front for Immaculate's real ASI intake code.
//
// Run from an Immaculate checkout's apps/harness directory with tsx:
//   node --import tsx /path/to/ASI-Evolve/tests/support/immaculate_intake_server.mjs
//
// It serves POST /api/asi/dispatch with the same bearer-key and
// purpose/consent-scope header checks the harness applies, then hands the body
// to the intake function server.ts uses (createAsiDispatchIntake: schema, hash,
// HMAC signature, freshness, root policy and the persistent nonce store). On an
// older Immaculate without it, it falls back to processAsiDispatchPacket with no
// nonce store. The first stdout line is JSON: {"port": ..., "nonceStore": bool}.

import http from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const harnessRoot = process.cwd();
const dispatch = await import(pathToFileURL(path.join(harnessRoot, "src/asi-dispatch.ts")).href);
const apiKey = process.env.IMMACULATE_API_KEY ?? "";
const runtimeRoot = await mkdtemp(path.join(os.tmpdir(), "asi-intake-runtime-"));
const intake =
  typeof dispatch.createAsiDispatchIntake === "function"
    ? dispatch.createAsiDispatchIntake({ repoRoot: runtimeRoot })
    : async (body) => {
        const receipt = await dispatch.processAsiDispatchPacket(body, { repoRoot: runtimeRoot });
        const accepted = receipt.decision !== "rejected";
        return { statusCode: accepted ? 200 : 422, body: { accepted, receipt } };
      };
const nonceStore = typeof dispatch.createAsiDispatchIntake === "function";
const PURPOSES = new Set(["cognitive-execution", "cognitive-reasoning"]);
const CONSENT_PREFIXES = ["system:intelligence", "session:", "subject:"];

function send(response, status, body) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(body));
}

const server = http.createServer(async (request, response) => {
  if (request.method !== "POST" || request.url !== "/api/asi/dispatch") {
    send(response, 404, { error: "not_found" });
    return;
  }
  if (!apiKey || request.headers.authorization !== `Bearer ${apiKey}`) {
    send(response, 401, { error: "unauthorized" });
    return;
  }
  const purpose = String(request.headers["x-immaculate-purpose"] ?? "");
  const consent = String(request.headers["x-immaculate-consent-scope"] ?? "");
  if (!PURPOSES.has(purpose) || !CONSENT_PREFIXES.some((prefix) => consent.startsWith(prefix))) {
    send(response, 403, { error: "governance_denied" });
    return;
  }
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  let body;
  try {
    body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    send(response, 400, { error: "invalid_json" });
    return;
  }
  const result = await intake(body);
  send(response, result.statusCode, result.body);
});

process.on("SIGTERM", () => {
  server.close();
  rm(runtimeRoot, { recursive: true, force: true }).finally(() => process.exit(0));
});

server.listen(0, "127.0.0.1", () => {
  process.stdout.write(`${JSON.stringify({ port: server.address().port, nonceStore })}\n`);
});
