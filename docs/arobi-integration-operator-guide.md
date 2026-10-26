# Arobi ASI-Evolve Integration Operator Guide

This fork is wired as a guarded research and improvement lane for Arobi systems. It is not a production deploy tool.

## Canonical Connections

Every local path comes from `arobi_integrations/default_manifest.json`. Relative defaults resolve against this checkout, and each one has an environment override:

| Connection | Default | Override |
|---|---|---|
| State root | `.arobi-evolve` | `AROBI_EVOLVE_STATE_ROOT` |
| This checkout | `.` | `AROBI_EVOLVE_ROOT_ASI_EVOLVE` |
| LaaS/Arobi website shell | `../Asgard_Arobi/Websites` | `AROBI_EVOLVE_ROOT_WEBSITE` |
| Immaculate | `../Immaculate` | `AROBI_EVOLVE_ROOT_IMMACULATE` |
| OpenJaws (legacy JAWS name kept for compatibility) | `../OpenJaws` | `AROBI_EVOLVE_ROOT_OPENJAWS` |
| Legacy Asgard root (forbidden deploy source) | unset | `AROBI_EVOLVE_LEGACY_ASGARD_ROOT` |
| Discord secret env script | `<openjaws>/local-command-station/discord-q-agent.env.ps1` | `AROBI_EVOLVE_SECRET_ENV_SCRIPT` |

A Windows drive path (`D:/...`) is refused on Linux and macOS instead of being resolved into a literal `D:` directory; the status report names the root that needs an override.

Public and service endpoints:

- Site: `https://aura-genesis.org` (`AROBI_EVOLVE_PUBLIC_URL`).
- Arobi spine public node info: `https://aura-genesis.org/arobi/api/v1/info`. The `arobi.aura-genesis.org` host is admin-only; only its `/api/fabric/public-status` path is public.
- Immaculate harness: `${IMMACULATE_HARNESS_URL:-http://127.0.0.1:8787}`; the QICR verifier `healthz`/`readyz` routes need `IMMACULATE_API_KEY`, and the harness only serves them with `IMMACULATE_Q_API_ENABLED=1`.
- Q gateway: local `${IMMACULATE_Q_GATEWAY_URL:-http://127.0.0.1:8897}/health`, public `https://q.aura-genesis.org/health`.
- LaaS API `https://laas.aura-genesis.org/health`, hosted Q `https://hosted-q.aura-genesis.org/health`, signed release catalog `https://downloads.aura-genesis.org/jaws/latest.json`, Superbrain, Discord bridge, LinkedIn bridge.

## Guardrails

- ASI-Evolve may create tasks, health snapshots, evaluator specs, and signed dispatch packets.
- ASI-Evolve must not deploy production, send external messages, mutate Stripe, mutate databases, change infrastructure, change Discord roles, expose secrets, or create agents.
- Work is routed to `agent/evolve/*` branches or local candidate workspaces only.
- `aura-genesis.org` stays protected by the website's own guarded deploy scripts. This fork never deploys.

## Daily Commands

```bash
python -m arobi_integrations status --write-snapshot
python -m arobi_integrations analytics --write-report
python -m arobi_integrations autopilot --write-report --notify --heal --deliver
python -m arobi_integrations doctor
python -m arobi_integrations seed-arobi --process
python -m arobi_integrations process --notify
python -m arobi_integrations deliver
```

Autopilot recovery behavior:

- Recovery targets any current failed service that has a recovery command for this OS (`platforms`: `windows` or `posix`). A route that was not probed because its credential is missing (`not_configured`) never triggers a restart.
- Recovery runs before the slower analytics pass so harness repair is not blocked by analytics reads.
- Route probes run concurrently, so one slow endpoint does not stall the monitor.
- When a recovery command starts a background process, autopilot verifies the affected health routes before writing the final status snapshot. The default post-heal wait is `150` seconds; use `--post-heal-wait 30` for a faster manual pass.

## Website Artifact Guard

`status` checks the built site against its own source at check time. Nothing is a remembered literal:

- The expected title is read from the website source `index.html`, or from `AROBI_EVOLVE_REQUIRED_TITLE` when set.
- `dist/index.html` must carry that title and reference module entry bundles that exist in `dist/` (and the `dist/.vite/manifest.json` entry when the build emits one). `AROBI_EVOLVE_REQUIRED_BUNDLE_MARKER` optionally pins a bundle name.
- The live entry bundles at the public URL are compared with the local `dist/` (`websiteLive.entry.matchesLocalDist`).
- The published deploy id is read from the Netlify API when `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID` are set; otherwise `websiteLive.deploy.status` is `not_configured`. No deploy id is ever printed from memory.

## Analytics

1. **Preferred:** the auth-gated Asgard operator analytics API, `GET /.netlify/functions/operator-analytics/summary`, with `AROBI_OPERATOR_ANALYTICS_TOKEN` (an operator's Supabase access token; the function checks operator access). `AROBI_OPERATOR_ANALYTICS_URL` overrides the URL.
2. **Explicit fallback only:** direct Supabase REST with the service-role key, plus read-only Stripe, enabled with `--direct-supabase-fallback` or `AROBI_EVOLVE_ANALYTICS_DIRECT_FALLBACK=1`. It queries telemetry for every operated site (`aura-genesis.org`, `qline.site`, `iorch.net`; override with `AROBI_EVOLVE_TELEMETRY_SITES`).

With neither available, the report says `not_configured` and contains no numbers. Deltas are only computed between reports from the same source.

## Approvals

A task is serious when its title or objective names a serious action (deploy, billing, secret, email, post, infrastructure, ...) or it was enqueued with `--approval-required`. A serious task is held in `tasks/pending-approval` until two signed tokens verify, one per role, from two different approvers:

| Role | Secret the approver and the bridge share |
|---|---|
| `founder` | `AROBI_EVOLVE_FOUNDER_APPROVAL_SECRET` |
| `policy-governor` | `AROBI_EVOLVE_GOVERNOR_APPROVAL_SECRET` |

Each token (`aev1.<claims>.<HMAC-SHA256>`) is bound to the task id and the task's `payloadSha256`, names the approver, and expires (72 hours by default). Editing a task after enqueue changes its payload hash, so the task is rejected and earlier approvals no longer match.

```bash
# The approver mints a token (the payload hash is in the held task and in the Discord notice):
python -m arobi_integrations approval-token --role founder --task <id> --approver <name>
# The operator records both tokens, then re-scans:
python -m arobi_integrations approve --task <id> --founder-approval <token> --governor-approval <token>
python -m arobi_integrations process
```

`process` re-scans `tasks/pending-approval` on every run, verifies the recorded tokens, and moves the task to `tasks/ready` once both verify. The approval receipt records who approved and a hash of each token. HMAC is symmetric: whoever holds a role secret can mint that role's tokens, so keep each secret with its approver and on the bridge host only. Discord notices are plain text; there are no approval buttons.

## Delivery To Immaculate

`deliver` (also `autopilot --deliver`, which re-scans tasks first) has two steps:

1. **Sign.** Each task in `tasks/ready` is re-verified (payload hash, validation, approvals), built into a packet in exactly Immaculate's `asiDispatchPacketSchema`, and signed: `issuer` (`ASI_DISPATCH_ISSUER`, default `asi-evolve:<hostname>`), a random 128-bit `nonce`, `expiresAt` 24 hours out, `packetSha256` over Immaculate's canonical JSON (`localeCompare` key order, raw UTF-8), and `signature = HMAC-SHA256(ASI_DISPATCH_HMAC_SECRET, packetSha256)` under key id `ASI_DISPATCH_HMAC_KEY_ID` (default `env`, Immaculate's default). The packet lands in `dispatch/outbox`.
2. **Send.** Each packet is POSTed to `${IMMACULATE_HARNESS_URL:-http://127.0.0.1:8787}/api/asi/dispatch` with `Authorization: Bearer ${IMMACULATE_API_KEY}`, `x-immaculate-purpose: cognitive-execution`, `x-immaculate-consent-scope: system:intelligence:asi-evolve` and `x-immaculate-actor: asi-evolve`.

| Outcome | Meaning | Packet |
|---|---|---|
| `delivered` | Immaculate returned an intake receipt for this exact packet (hash-checked) with decision `ready` or `review_only` | `dispatch/delivered` |
| `rejected` | Immaculate's intake rejected it (HTTP 422); its errors are in the receipt | `dispatch/rejected` |
| `expired_undelivered` | It expired before any intake receipt came back; re-enqueue to retry | `dispatch/rejected` |
| `unavailable` | No response, 429 or 5xx | stays in `dispatch/outbox` |
| `refused` | 401/403 or another 4xx without an intake verdict | stays in `dispatch/outbox` |
| `unrecognized_response` | A reply that is not an intake receipt for this packet | stays in `dispatch/outbox` |

A missing `ASI_DISPATCH_HMAC_SECRET` leaves tasks in `tasks/ready`; a missing `IMMACULATE_API_KEY` leaves signed packets in `dispatch/outbox`. Both report `not_configured` and exit with code 2. Immaculate keeps each packet's `issuer:nonce` until its `expiresAt`, so a resent packet that already landed is rejected as a replay rather than processed twice.

`tests/fixtures/immaculate_asi_dispatch_vectors.json` was generated by Immaculate's own `signAsiDispatchPacket`; the golden tests hold the Python signer to it byte for byte. Regenerate it with the command recorded in the fixture when Immaculate's canonicalization changes.

## State Layout

Under the state root:

- `status/latest.json`, `status/doctor-latest.json`, `status/analytics-latest.json`, `status/autopilot-latest.json`, `status/process-latest.json`, `status/deliver-latest.json`: latest reports.
- `status/bridge-heartbeat.json`, `logs/`: scheduled wrapper heartbeat and logs.
- `reports/operator-analytics-latest.md`: private analytics report.
- `tasks/inbox`, `tasks/pending-approval`, `tasks/ready`, `tasks/processed`, `tasks/rejected`: the task lifecycle. `processed` holds tasks whose packet has been signed.
- `dispatch/outbox`, `dispatch/delivered`, `dispatch/rejected`: signed packets.
- `receipts`: JSON receipts for rejections, holds, approvals, signing and delivery attempts. Each carries `receiptSha256` (Immaculate-canonical sha256 of the receipt) and, when `ASI_DISPATCH_HMAC_SECRET` is set, an HMAC-SHA256 `signature` with the packet key; without the key, `signature.status` says `unsigned`. Check one with `python -m arobi_integrations verify-receipt --path <file>`.

## Seeded Work Queue

`seed-arobi` creates three tasks:

1. Protect the Aura Genesis LaaS shell route family and public API/replay visibility.
2. Improve Q gateway substrate behavior using Immaculate benchmark receipts (approval required).
3. Harden Discord workstation document intake, governed web fetch, approval queue, and operator delivery.

## Scheduled Operation

Windows: `scripts/start-arobi-evolve-bridge.ps1` runs one guarded pass (autopilot with `--deliver`, then `process`), with a machine-wide mutex, a heartbeat file, bounded logs, a `25` second route timeout, a `180` second post-heal window, and hard stops after `540` seconds (autopilot) and `120` seconds (process).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-arobi-evolve-bridge.ps1 -Once -DisableNotify -TimeoutSeconds 25 -PostHealWaitSeconds 30
powershell -ExecutionPolicy Bypass -File scripts\install-arobi-evolve-bridge-task.ps1
```

Linux (systemd user units in `scripts/systemd/`, every 15 minutes):

```bash
mkdir -p ~/.config/systemd/user ~/.config/arobi-evolve
cp scripts/systemd/arobi-evolve-bridge.{service,timer} ~/.config/systemd/user/
# Set WorkingDirectory in the service to this checkout, and put the bridge's
# environment (keys, roots) in ~/.config/arobi-evolve/bridge.env (mode 600).
systemctl --user daemon-reload
systemctl --user enable --now arobi-evolve-bridge.timer
```

## Autonomy Policy

Allowed without interactive approval:

- Read-only probes of the routes above.
- Operator analytics through the auth-gated Asgard API; direct Supabase/Stripe reads only when the fallback is explicitly enabled.
- Local recovery commands listed in the manifest for this OS: Q gateway start, Immaculate harness start (the OpenJaws supervisor on Windows, `npm run harness:serve` elsewhere), and the Windows Discord and LinkedIn bridge launchers.
- Founder-only Discord notifications with counts and report paths. The notifier reads only named Discord values from the environment or the Discord secret env script; no secret values are copied into this repo or written to reports.
- Signed delivery of approved tasks to Immaculate's governed ASI intake.

Still approval-gated: production deploys, Stripe mutations, refunds, discounts, subscription changes, database migrations, RLS changes, external outreach, LinkedIn posts/comments, calendar sends, Discord role/invite changes, Cloudflare/OCI/Railway mutations, credential changes, and regulated/defense operational actions.

## Verification

```bash
python -m pytest -q
```

The live intake tests start Immaculate's real ASI intake from a sibling `../Immaculate` checkout (or `IMMACULATE_ROOT`) with its `node_modules`, and skip when none is present. The earlier record of the initial Windows install (May 23, 2026: scheduled task installed, `doctor` groups passing on the founder machine) describes that machine only and is not re-verified by these tests.
