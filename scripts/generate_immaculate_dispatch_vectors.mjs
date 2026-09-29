// Generates tests/fixtures/immaculate_asi_dispatch_vectors.json from Immaculate's
// own ASI intake code, so the Python bridge is pinned to Immaculate's bytes.
//
// Run from an Immaculate checkout's harness directory (it needs Immaculate's
// node_modules for tsx and zod):
//
//   cd "$IMMACULATE_ROOT/apps/harness"
//   node --import tsx /path/to/ASI-Evolve/scripts/generate_immaculate_dispatch_vectors.mjs \
//     > /path/to/ASI-Evolve/tests/fixtures/immaculate_asi_dispatch_vectors.json
//
// The vectors only change when Immaculate's canonicalization or signing changes.
// Regenerate them then, and let the Python golden test show what moved.

import { execFileSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const harnessRoot = process.cwd();
const load = (relative) => import(pathToFileURL(path.join(harnessRoot, relative)).href);
const dispatch = await load("src/asi-dispatch.ts");
const utils = await load("src/utils.ts");

const KEY_ID = "test-key";
const SECRET = "asi-dispatch-test-secret";
const NOW = new Date("2026-05-25T00:01:00Z");

// Byte-for-byte the unsignedPacket() body in apps/harness/src/asi-dispatch.test.ts.
const immaculateTestBody = {
  schemaVersion: 1,
  createdAt: "2026-05-25T00:00:00Z",
  expiresAt: "2026-05-25T00:10:00Z",
  issuer: "asi-evolve:test",
  nonce: "nonce-aura-shell-guard",
  taskId: "aura-shell-guard",
  taskPayloadSha256: "a".repeat(64),
  title: "Protect Aura Genesis LaaS shell deploy route",
  objective:
    "Verify the live Aura Genesis site serves the protected React/LaaS/Arobi shell without production mutation.",
  lane: "public",
  targetRoot: "D:/Websites",
  allowedWritePaths: ["D:/ASI-Evolve/.arobi-evolve/candidate-workspaces/aura-shell-guard"],
  branch: "agent/evolve/aura-shell-guard",
  authority: {
    mode: "agent-branch-only",
    productionDeployAllowed: false,
    externalMutationAllowed: false,
    secretsAllowed: false,
    requiresFounderApprovalForSeriousActions: true
  },
  evaluator: {
    command: ["npm", "run", "operator:handoff:check"],
    timeoutSec: 120
  },
  routes: {
    laasWebsite: {
      type: "protected-react-shell",
      root: "D:/Websites",
      publicUrl: "https://aura-genesis.org"
    }
  }
};

// Non-ASCII, escaping and mixed-case keys: the cases where Python's json.dumps
// defaults diverge from Immaculate.
const nonAsciiBody = {
  ...immaculateTestBody,
  nonce: "nonce-non-ascii-vector",
  taskId: "non-ascii-vector",
  title: "Protéger la coque LaaS — 守护 🚀 \"quoted\" \\ back\tslash",
  objective: "Line one\u2028line two \u0001 control, ünïcödé, emoji 🧪, and a solidus / kept raw.",
  branch: "agent/evolve/non-ascii-vector",
  routes: {
    laasWebsite: { type: "protected-react-shell", root: "D:/Websites", publicUrl: "https://aura-genesis.org" },
    q: { type: "q-gateway", health: "http://127.0.0.1:8897/health", Zeta: 1, alpha: 2, Alpha: 3, a_b: 4, "a-b": 5, aB: 6 }
  }
};

function signVector(name, body) {
  const signed = dispatch.signAsiDispatchPacket(body, { keyId: KEY_ID, secret: SECRET });
  const verdict = dispatch.inspectAsiDispatchPacket(signed, {
    signatureSecrets: { [KEY_ID]: SECRET },
    now: NOW
  });
  return {
    name,
    keyId: KEY_ID,
    secret: SECRET,
    unsignedBody: body,
    canonicalBody: utils.stableStringify(body),
    packetSha256: signed.packetSha256,
    signature: signed.signature,
    signedPacket: signed,
    signedPacketCanonical: utils.stableStringify(signed),
    immaculateVerdict: { now: NOW.toISOString(), decision: verdict.decision, errors: verdict.errors }
  };
}

const canonicalValues = [
  { floats: [1.5, 0.1, 1e21, 1e-7, 0.000001, 5e-324, 1.5e300, 123456789012345680000, 9007199254740993, -2.5, 100] },
  { nested: { Beta: [true, false, null], beta: "x", "": "empty key", "key with space": 1, "a.b": 2, "a:b": 3 } },
  { strings: ["\u0000\u001f\u007f", "\"\\/\b\f\n\r\t", "\u2028\u2029", "é", "😀"] }
];

// Deterministic pseudo-random keys over the printable-ASCII alphabet.
let seed = 0x5eed1234;
function nextRandom() {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed / 2 ** 32;
}
const alphabet = [];
for (let code = 0x20; code < 0x7f; code += 1) alphabet.push(String.fromCharCode(code));
const weighted = [...alphabet, ..."aAbBtTsS09_-.".split(""), ..."aAbBtTsS09_-.".split("")];
const randomKeys = new Set(["a", "A", "ab", "aB", "Ab", "AB", "a1", "a10", "a9", "taskId", "taskPayloadSha256"]);
while (randomKeys.size < 400) {
  const length = 1 + Math.floor(nextRandom() * 8);
  let key = "";
  for (let index = 0; index < length; index += 1) key += weighted[Math.floor(nextRandom() * weighted.length)];
  randomKeys.add(key);
}
const keyOrder = [...randomKeys].sort((left, right) => left.localeCompare(right));

const receiptRoot = await mkdtemp(path.join(os.tmpdir(), "asi-vector-receipt-"));
const immaculateReceipt = await dispatch.processAsiDispatchPacket(
  dispatch.signAsiDispatchPacket(immaculateTestBody, { keyId: KEY_ID, secret: SECRET }),
  { repoRoot: harnessRoot, receiptRoot, signatureSecrets: { [KEY_ID]: SECRET }, now: NOW }
);
await rm(receiptRoot, { recursive: true, force: true });
// The HTTP intake returns the receipt with receiptPath added after hashing; keep
// that shape but drop the throwaway temp directory from the fixture.
immaculateReceipt.receiptPath = path.join("<receiptRoot>", path.basename(immaculateReceipt.receiptPath));

let immaculateCommit = "unknown";
try {
  immaculateCommit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: harnessRoot, encoding: "utf8" }).trim();
} catch {
  // Leave "unknown" when git is not available.
}

const fixture = {
  schemaVersion: 1,
  generator: {
    script: "scripts/generate_immaculate_dispatch_vectors.mjs",
    command:
      'cd "$IMMACULATE_ROOT/apps/harness" && node --import tsx "$ASI_EVOLVE_ROOT/scripts/generate_immaculate_dispatch_vectors.mjs" > "$ASI_EVOLVE_ROOT/tests/fixtures/immaculate_asi_dispatch_vectors.json"',
    immaculateCommit,
    immaculateSources: ["apps/harness/src/asi-dispatch.ts", "apps/harness/src/utils.ts"],
    node: process.version,
    collatorLocale: new Intl.Collator().resolvedOptions().locale
  },
  packets: [signVector("immaculate-asi-dispatch-test", immaculateTestBody), signVector("non-ascii", nonAsciiBody)],
  canonicalCases: canonicalValues.map((value) => ({ value, canonical: utils.stableStringify(value), sha256: utils.sha256Json(value) })),
  keyOrder,
  immaculateReceipt
};

process.stdout.write(`${JSON.stringify(fixture, null, 2)}\n`);
