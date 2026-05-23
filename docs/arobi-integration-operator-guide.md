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
python -m arobi_integrations doctor
python -m arobi_integrations seed-arobi --process
python -m arobi_integrations process
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

- `python -m unittest discover -s tests -v`: 6 tests passed.
- `python -m arobi_integrations status --write-snapshot`: route and artifact status passed with zero required failures.
- `python -m arobi_integrations doctor --group website --execute`: `operator:handoff:check` and `test:production-smokes` passed through the bridge.
- `python -m arobi_integrations doctor --group openjaws --execute`: serious-action approval, orchestration guardrails, and OpenJaws roundtable status passed.
- `python -m arobi_integrations doctor --group immaculate --execute`: Immaculate operator readiness and live operator activity exited successfully.
- Immaculate readiness still reported warnings for the public projection guard and OpenJaws runtime coherence. Treat these as production-readiness warnings before enabling external Discord/OpenJaws action delivery.
- Local Q gateway was started from Immaculate and verified at `http://127.0.0.1:8897/health`.
- Windows Scheduled Task `Arobi ASI-Evolve Guarded Bridge` was installed and manually started once. `LastTaskResult` was `0`; next run was scheduled by Windows.

## 24/7 Mode

Use the scheduled task wrapper in `scripts/start-arobi-evolve-bridge.ps1` for local-only unattended checks. The scheduled loop only writes health snapshots and validates queued tasks. It does not run ASI-Evolve rounds or execute serious actions.

```powershell
powershell -ExecutionPolicy Bypass -File D:\ASI-Evolve\scripts\start-arobi-evolve-bridge.ps1 -Once
```

Install a Windows Scheduled Task only after confirming this command works locally:

```powershell
powershell -ExecutionPolicy Bypass -File D:\ASI-Evolve\scripts\install-arobi-evolve-bridge-task.ps1
```
