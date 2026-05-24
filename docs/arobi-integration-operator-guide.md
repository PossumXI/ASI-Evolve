# Arobi ASI-Evolve Integration Operator Guide

This fork is wired as a guarded research and improvement lane for Arobi systems. It is not a production deploy tool.

## Canonical Connections

- LaaS/Arobi website shell: `D:\Websites`, artifact `D:\Websites\dist`, public URL `https://aura-genesis.org`.
- Arobi public node: `https://arobi.aura-genesis.org`.
- Superbrain/Immaculate public lane: `https://superbrain.aura-genesis.org/api/health`.
- Immaculate local harness: `C:\Users\Knight\Desktop\Immaculate`, local health `http://127.0.0.1:8787/api/health`.
- Q gateway: `http://127.0.0.1:8897/health` when the dedicated gateway is running.
- JAWS/OpenJaws: `D:\openjaws\OpenJaws`.
- Discord agent lane: OpenJaws `discord-agent-supervisor`, `roundtable-runtime`, document workstation, and serious-action approval scripts.

## Guardrails

- ASI-Evolve may create tasks, health snapshots, evaluator specs, and dispatch packets.
- ASI-Evolve must not deploy production, send external messages, mutate Stripe, mutate databases, change infrastructure, change Discord roles, expose secrets, or create agents without exact founder and policy-governor approval.
- Work is routed to `agent/evolve/*` branches or local candidate workspaces only.
- `aura-genesis.org` stays protected by the `D:\Websites` deploy guard. This fork never deploys the retired static Aura archive.

## Daily Commands

```powershell
cd D:\ASI-Evolve
python -m arobi_integrations status --write-snapshot
python -m arobi_integrations analytics --write-report
python -m arobi_integrations autopilot --write-report --notify --heal
python -m arobi_integrations doctor
python -m arobi_integrations seed-arobi --process
python -m arobi_integrations process
```

Autopilot recovery behavior:

- Recovery targets any current failed service that has a configured recovery command, not only failures that are new compared with the previous run.
- Recovery runs before the slower analytics pass so Immaculate/Superbrain repair is not blocked by Supabase, Stripe, or telemetry reads.
- When a recovery command starts a background supervisor, autopilot verifies the affected health routes before writing the final status snapshot.
- Default post-heal verification wait is `150` seconds. For a faster manual pass, use `--post-heal-wait 30`.

```powershell
python -m arobi_integrations autopilot --write-report --heal --timeout 12 --post-heal-wait 30
```

Run deeper command checks only when the machine has time:

```powershell
python -m arobi_integrations doctor --group website --execute
python -m arobi_integrations doctor --group immaculate --execute
python -m arobi_integrations doctor --group openjaws --execute
```

## State Layout

The bridge writes local state under `D:\ASI-Evolve\.arobi-evolve`:

- `status/latest.json`: latest route and artifact health.
- `status/doctor-latest.json`: latest local command readiness or execution report.
- `status/analytics-latest.json`: latest private analytics summary.
- `status/autopilot-latest.json`: latest autonomous monitor, delta, recovery, and notification receipt.
- `reports/operator-analytics-latest.md`: private website/user/revenue/API telemetry report.
- `tasks/inbox`: new proposed evolution tasks.
- `tasks/pending-approval`: validated tasks that require exact approval.
- `tasks/ready`: validated tasks that can run on an agent-only branch.
- `outbox/q`, `outbox/immaculate`, `outbox/jaws`, `outbox/discord`, `outbox/laas`: dispatch packets for each lane.
- `receipts`: signed JSON receipts for validation, holds, and ready packets.

## First Work Queue

The seeded queue starts with:

1. Protect the Aura Genesis LaaS shell route family and public API/replay visibility.
2. Improve Q gateway substrate behavior using Immaculate benchmark receipts.
3. Harden Discord workstation document intake, governed web fetch, approval queue, and operator delivery.

Each task has an explicit evaluator command and timeout. Tasks with production, billing, infrastructure, database, secret, external-communication, or regulated-safety implications are held until exact approvals are attached to the task JSON.

## Verification From Initial Install

Initial verification on May 23, 2026:

- `python -m unittest discover -s tests -v`: 10 tests passed.
- `python -m arobi_integrations status --write-snapshot`: route and artifact status passed with zero required failures.
- `python -m arobi_integrations doctor --group website --execute`: `operator:handoff:check` and `test:production-smokes` passed through the bridge.
- `python -m arobi_integrations doctor --group openjaws --execute`: serious-action approval, orchestration guardrails, and OpenJaws roundtable status passed.
- `python -m arobi_integrations doctor --group immaculate --execute`: Immaculate operator readiness and live operator activity exited successfully.
- Immaculate readiness still reported warnings for the public projection guard and OpenJaws runtime coherence. Treat these as production-readiness warnings before enabling external Discord/OpenJaws action delivery.
- Local Q gateway was started from Immaculate and verified at `http://127.0.0.1:8897/health`.
- Windows Scheduled Task `Arobi ASI-Evolve Guarded Bridge` was installed and manually started once. `LastTaskResult` was `0`; next run was scheduled by Windows.

## 24/7 Mode

Use the scheduled task wrapper in `scripts/start-arobi-evolve-bridge.ps1` for local-only unattended checks. The scheduled loop runs the guarded autopilot, writes private analytics reports, attempts only configured safe local recovery commands, notifies the founder on material deltas, and validates queued tasks. It does not run production deploys, send external outreach, mutate billing, mutate databases, change roles, or execute serious infrastructure changes without approvals.

```powershell
powershell -ExecutionPolicy Bypass -File D:\ASI-Evolve\scripts\start-arobi-evolve-bridge.ps1 -Once
```

Install a Windows Scheduled Task only after confirming this command works locally:

```powershell
powershell -ExecutionPolicy Bypass -File D:\ASI-Evolve\scripts\install-arobi-evolve-bridge-task.ps1
```

## Autonomy Policy

Allowed without interactive approval:

- Read-only checks against `https://aura-genesis.org`, `https://arobi.aura-genesis.org`, Superbrain, Q gateway, Immaculate harness, Discord bridge, and LinkedIn bridge.
- Read-only Supabase aggregate analytics for auth counts, newsletter/contact counts, token order status, API-key usage counts, tenant decision volume, and site telemetry.
- Conversion/drop-off reporting for sessions to confirmed users, users to active subscriptions, sessions to paid token orders, newsletter leads to users, and API-key activation.
- Read-only Stripe checks when the local/Netlify key is accepted. If Stripe returns `401`, Supabase webhook-confirmed records remain the revenue source of truth.
- Local safe recovery commands listed in `arobi_integrations/default_manifest.json`, currently the Q gateway restart path.
- Founder-only Discord notifications with counts and report paths. The notifier reads only named Discord values from `D:\openjaws\OpenJaws\local-command-station\discord-q-agent.env.ps1` at runtime; no secret values are copied into this repo or written to reports.
- Safe local recovery currently covers Q gateway restart and the OpenJaws-supervised Immaculate harness launcher. The harness recovery also clears the related Superbrain public 502 path because that public route depends on the local harness being healthy.

Still approval-gated:

- Production deploys, Stripe mutations, refunds, discount creation, subscription changes, database migrations, RLS changes, external outreach, LinkedIn posts/comments, calendar sends, Discord role/invite changes, Cloudflare/OCI/Railway mutations, credential changes, and regulated/defense operational actions.

The live deep analytics report is private local state, not a public GitHub artifact:

```powershell
Get-Content D:\ASI-Evolve\.arobi-evolve\reports\operator-analytics-latest.md
```
