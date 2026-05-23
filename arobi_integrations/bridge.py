"""Guarded Arobi bridge for the ASI-Evolve fork.

This module intentionally does not run ASI-Evolve rounds or mutate production.
It checks health, validates task contracts, and writes dispatch packets that Q,
Immaculate, JAWS/OpenJaws, and Discord agents can consume through their existing
approval-gated lanes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "arobi_integrations" / "default_manifest.json"
DEFAULT_TIMEOUT = 8
SERIOUS_ACTION_WORDS = {
    "deploy",
    "payment",
    "billing",
    "invoice",
    "schema",
    "migration",
    "secret",
    "credential",
    "email",
    "post",
    "dm",
    "comment",
    "calendar",
    "invite",
    "role",
    "infrastructure",
    "oci",
    "railway",
    "cloudflare",
    "stripe",
}


@dataclass(frozen=True)
class BridgePaths:
    state_root: Path
    inbox: Path
    pending: Path
    ready: Path
    processed: Path
    rejected: Path
    receipts: Path
    status: Path
    outbox: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def sha256_json(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_path(raw: str) -> Path:
    return Path(raw.replace("/", os.sep)).resolve()


def bridge_paths(manifest: dict[str, Any]) -> BridgePaths:
    state_root = normalize_path(str(manifest.get("stateRoot", REPO_ROOT / ".arobi-evolve")))
    paths = BridgePaths(
        state_root=state_root,
        inbox=state_root / "tasks" / "inbox",
        pending=state_root / "tasks" / "pending-approval",
        ready=state_root / "tasks" / "ready",
        processed=state_root / "tasks" / "processed",
        rejected=state_root / "tasks" / "rejected",
        receipts=state_root / "receipts",
        status=state_root / "status",
        outbox=state_root / "outbox",
    )
    for path in paths.__dict__.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def load_manifest(path: str | None) -> dict[str, Any]:
    manifest_path = normalize_path(path) if path else DEFAULT_MANIFEST
    manifest = load_json(manifest_path)
    manifest["_manifestPath"] = str(manifest_path)
    return manifest


def redacted_error(error: BaseException) -> str:
    return redact_text(str(error))[:400]


def redact_text(text: str) -> str:
    for marker in ("api_key", "apikey", "token", "secret", "password", "authorization"):
        text = text.replace(marker, "[redacted-keyword]")
    return text


def http_probe(service: dict[str, Any], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    request = urllib.request.Request(
        str(service["url"]),
        headers={"User-Agent": "arobi-asi-evolve-bridge/2026.05.23"},
        method="GET",
    )
    expected = set(int(value) for value in service.get("expectedStatuses", [200]))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            sample = response.read(512)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "id": service["id"],
            "label": service.get("label", service["id"]),
            "url": service["url"],
            "status": "ok" if status in expected else "warning",
            "httpStatus": status,
            "elapsedMs": elapsed_ms,
            "expectedStatuses": sorted(expected),
            "optional": bool(service.get("optional", False)),
            "sampleSha256": hashlib.sha256(sample).hexdigest(),
        }
    except urllib.error.HTTPError as error:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        status = int(error.code)
        return {
            "id": service["id"],
            "label": service.get("label", service["id"]),
            "url": service["url"],
            "status": "ok" if status in expected else ("optional_failed" if service.get("optional") else "failed"),
            "httpStatus": status,
            "elapsedMs": elapsed_ms,
            "expectedStatuses": sorted(expected),
            "optional": bool(service.get("optional", False)),
            "error": redacted_error(error),
        }
    except Exception as error:  # noqa: BLE001 - status reporting must not crash the bridge.
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "id": service["id"],
            "label": service.get("label", service["id"]),
            "url": service["url"],
            "status": "optional_failed" if service.get("optional") else "failed",
            "httpStatus": None,
            "elapsedMs": elapsed_ms,
            "expectedStatuses": sorted(expected),
            "optional": bool(service.get("optional", False)),
            "error": redacted_error(error),
        }


def check_local_roots(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    roots = manifest.get("localRoots", {})
    results = []
    for key, raw_path in sorted(roots.items()):
        path = normalize_path(str(raw_path))
        results.append(
            {
                "id": key,
                "path": str(path),
                "status": "ok" if path.exists() else "failed",
                "exists": path.exists(),
                "isDir": path.is_dir(),
            }
        )
    return results


def check_website_artifact(manifest: dict[str, Any]) -> dict[str, Any]:
    website = manifest.get("website", {})
    artifact_root = normalize_path(str(website.get("artifactRoot", "")))
    index_path = artifact_root / "index.html"
    required_title = str(website.get("requiredTitle", ""))
    required_bundle = str(website.get("requiredBundleMarker", ""))
    findings: list[str] = []

    if not index_path.exists():
        findings.append("dist index.html is missing")
        html = ""
    else:
        html = index_path.read_text(encoding="utf-8", errors="replace")
        if required_title and required_title not in html:
            findings.append("required root title marker missing")
        if required_bundle and required_bundle not in html:
            findings.append("protected May 19 bundle marker missing")

    forbidden = []
    canonical_root = normalize_path(str(website.get("canonicalRoot", "")))
    for raw_path in website.get("forbiddenDeployRoots", []):
        path = normalize_path(str(raw_path))
        if path == canonical_root:
            forbidden.append(str(path))

    if forbidden:
        findings.append("forbidden deploy roots include canonical root")

    return {
        "status": "ok" if not findings else "failed",
        "artifactRoot": str(artifact_root),
        "indexPath": str(index_path),
        "requiredProductionDeployId": website.get("requiredProductionDeployId"),
        "requiredTitle": required_title,
        "requiredBundleMarker": required_bundle,
        "findings": findings,
    }


def build_status(manifest: dict[str, Any], timeout: int) -> dict[str, Any]:
    service_results = [http_probe(service, timeout) for service in manifest.get("services", [])]
    root_results = check_local_roots(manifest)
    artifact = check_website_artifact(manifest)
    failures = [
        result
        for result in [*service_results, *root_results, artifact]
        if result["status"] not in {"ok", "optional_failed"}
    ]
    return {
        "schemaVersion": 1,
        "checkedAt": utc_now(),
        "mode": manifest.get("mode"),
        "manifestPath": manifest.get("_manifestPath"),
        "status": "ok" if not failures else "warning",
        "publicDataPolicy": manifest.get("governance", {}).get("publicDataPolicy"),
        "websiteArtifact": artifact,
        "services": service_results,
        "localRoots": root_results,
        "failureCount": len(failures),
    }


def resolve_command_argv(argv: list[str]) -> list[str] | None:
    if not argv:
        return None
    executable = shutil.which(argv[0])
    if executable is None:
        return None
    return [executable, *argv[1:]]


def command_available(argv: list[str]) -> bool:
    return resolve_command_argv(argv) is not None


def run_command(command: dict[str, Any]) -> dict[str, Any]:
    argv = [str(value) for value in command.get("argv", [])]
    cwd = normalize_path(str(command.get("cwd", REPO_ROOT)))
    timeout = int(command.get("timeoutSec", 120))
    resolved_argv = resolve_command_argv(argv)
    if resolved_argv is None:
        return {
            "id": command.get("id"),
            "status": "failed",
            "cwd": str(cwd),
            "argv": argv,
            "error": f"Executable not found: {argv[0] if argv else '[empty]'}",
        }
    started = time.perf_counter()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            resolved_argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        return {
            "id": command.get("id"),
            "status": "ok" if process.returncode == 0 else "failed",
            "cwd": str(cwd),
            "argv": argv,
            "resolvedExecutable": resolved_argv[0],
            "exitCode": process.returncode,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
            "stdoutTail": redact_text(stdout)[-2000:],
            "stderrTail": redact_text(stderr)[-2000:],
        }
    except subprocess.TimeoutExpired:
        if process is not None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                process.kill()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
        else:
            stdout, stderr = "", ""
        return {
            "id": command.get("id"),
            "status": "failed",
            "cwd": str(cwd),
            "argv": argv,
            "resolvedExecutable": resolved_argv[0],
            "timedOut": True,
            "timeoutSec": timeout,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
            "stdoutTail": redact_text(stdout)[-2000:],
            "stderrTail": redact_text(stderr)[-2000:],
        }
    except Exception as error:  # noqa: BLE001
        return {
            "id": command.get("id"),
            "status": "failed",
            "cwd": str(cwd),
            "argv": argv,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
            "error": redacted_error(error),
        }


def build_task_id(title: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in title).strip("-")
    slug = "-".join(part for part in slug.split("-") if part)[:48] or "task"
    digest = hashlib.sha256(f"{title}:{time.time_ns()}".encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def create_task(args: argparse.Namespace, manifest: dict[str, Any], paths: BridgePaths) -> dict[str, Any]:
    task_id = args.id or build_task_id(args.title)
    task = {
        "schemaVersion": 1,
        "id": task_id,
        "createdAt": utc_now(),
        "kind": args.kind,
        "title": args.title,
        "objective": args.objective,
        "targetRoot": str(normalize_path(args.target_root)),
        "lane": args.lane,
        "allowedWritePaths": [str(normalize_path(value)) for value in args.allowed_write_path],
        "evaluator": {
            "command": args.evaluator,
            "timeoutSec": args.timeout_sec,
        },
        "integrations": {
            "q": True,
            "immaculate": True,
            "jaws": True,
            "discordRoundtable": True,
            "laasWebsite": True,
        },
        "approval": {
            "required": args.approval_required or action_needs_approval(args.title, args.objective),
            "reason": "serious action or production-adjacent task" if action_needs_approval(args.title, args.objective) else "operator selected",
            "founderApprovalId": None,
            "policyGovernorApprovalId": None,
        },
        "governance": manifest.get("governance", {}),
    }
    task["payloadSha256"] = sha256_json(task)
    out_path = paths.inbox / f"{task_id}.json"
    write_json(out_path, task)
    return {"status": "ok", "taskPath": str(out_path), "task": task}


def action_needs_approval(title: str, objective: str) -> bool:
    haystack = f"{title} {objective}".lower()
    return any(word in haystack for word in SERIOUS_ACTION_WORDS)


def validate_task(task: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = ["id", "title", "objective", "targetRoot", "allowedWritePaths", "evaluator"]
    for key in required:
        if key not in task:
            errors.append(f"missing required field: {key}")
    target_root = normalize_path(str(task.get("targetRoot", "")))
    if not target_root.exists():
        errors.append(f"targetRoot does not exist: {target_root}")

    allowed_roots = [normalize_path(str(value)) for value in manifest.get("localRoots", {}).values()]
    if allowed_roots and not any(target_root == root or target_root.is_relative_to(root) for root in allowed_roots):
        errors.append(f"targetRoot is outside configured local roots: {target_root}")

    website = manifest.get("website", {})
    forbidden_roots = [normalize_path(str(value)) for value in website.get("forbiddenDeployRoots", [])]
    for write_path_raw in task.get("allowedWritePaths", []):
        write_path = normalize_path(str(write_path_raw))
        for forbidden in forbidden_roots:
            if write_path == forbidden:
                errors.append(f"allowedWritePath is inside forbidden deploy root: {write_path}")
                continue
            # A drive root such as D:\ is forbidden as a deploy source, but it
            # must not make every isolated workspace on that drive invalid.
            if forbidden.parent == forbidden:
                continue
            if write_path.is_relative_to(forbidden):
                errors.append(f"allowedWritePath is inside forbidden deploy root: {write_path}")

    evaluator = task.get("evaluator", {})
    timeout = evaluator.get("timeoutSec")
    if not isinstance(timeout, int) or timeout <= 0 or timeout > 3600:
        errors.append("evaluator.timeoutSec must be an integer from 1 to 3600")
    command = evaluator.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) and item for item in command):
        errors.append("evaluator.command must be a non-empty string array")

    return errors


def approval_is_complete(task: dict[str, Any]) -> bool:
    approval = task.get("approval", {})
    if not approval.get("required"):
        return True
    return bool(approval.get("founderApprovalId")) and bool(approval.get("policyGovernorApprovalId"))


def build_dispatch_packet(task: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    branch_prefix = manifest.get("governance", {}).get("branchPrefix", "agent/evolve/")
    task_id = str(task["id"])
    safe_branch = "".join(char.lower() if char.isalnum() else "-" for char in task_id).strip("-")[:48]
    packet = {
        "schemaVersion": 1,
        "createdAt": utc_now(),
        "taskId": task_id,
        "taskPayloadSha256": task.get("payloadSha256") or sha256_json(task),
        "title": task.get("title"),
        "objective": task.get("objective"),
        "lane": task.get("lane", manifest.get("governance", {}).get("defaultLane", "private")),
        "targetRoot": task.get("targetRoot"),
        "allowedWritePaths": task.get("allowedWritePaths", []),
        "branch": f"{branch_prefix}{safe_branch}",
        "authority": {
            "mode": "agent-branch-only",
            "productionDeployAllowed": False,
            "externalMutationAllowed": False,
            "secretsAllowed": False,
            "requiresFounderApprovalForSeriousActions": True,
        },
        "evaluator": task.get("evaluator"),
        "routes": {
            "q": {
                "type": "q-gateway-or-route-queue",
                "localHealth": "http://127.0.0.1:8897/health",
            },
            "immaculate": {
                "type": "roundtable-handoff",
                "root": manifest.get("localRoots", {}).get("immaculate"),
                "command": "npm run roundtable:runtime -- --json --handoff <packet>",
            },
            "jaws": {
                "type": "openjaws-guarded-workstation",
                "root": manifest.get("localRoots", {}).get("openjaws"),
                "preflight": ["bun run serious:approval:ready", "bun run orchestration:guardrails"],
            },
            "discord": {
                "type": "founder-review-first",
                "channel": "q-roundtable",
                "approvalButtonsRequired": True,
            },
            "laasWebsite": {
                "type": "protected-react-shell",
                "root": manifest.get("localRoots", {}).get("website"),
                "publicUrl": manifest.get("website", {}).get("publicUrl"),
            },
        },
    }
    packet["packetSha256"] = sha256_json(packet)
    return packet


def move_task(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        destination = destination_dir / f"{source.stem}-{int(time.time())}{source.suffix}"
    source.replace(destination)
    return destination


def process_tasks(manifest: dict[str, Any], paths: BridgePaths, limit: int) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    task_paths = sorted(paths.inbox.glob("*.json"))[:limit]
    for task_path in task_paths:
        task = load_json(task_path)
        errors = validate_task(task, manifest)
        if errors:
            receipt = {
                "status": "rejected",
                "taskPath": str(task_path),
                "taskId": task.get("id"),
                "checkedAt": utc_now(),
                "errors": errors,
            }
            receipt["receiptSha256"] = sha256_json(receipt)
            write_json(paths.receipts / f"{task_path.stem}-rejected.json", receipt)
            moved = move_task(task_path, paths.rejected)
            receipt["movedTo"] = str(moved)
            processed.append(receipt)
            continue

        if not approval_is_complete(task):
            packet = build_dispatch_packet(task, manifest)
            receipt = {
                "status": "pending_approval",
                "taskId": task["id"],
                "checkedAt": utc_now(),
                "summary": "Task is validated but held until exact founder and policy-governor approval is attached.",
                "packet": packet,
            }
            receipt["receiptSha256"] = sha256_json(receipt)
            write_json(paths.outbox / "discord" / f"{task['id']}.json", packet)
            write_json(paths.outbox / "immaculate" / f"{task['id']}.json", packet)
            write_json(paths.outbox / "jaws" / f"{task['id']}.json", packet)
            write_json(paths.outbox / "laas" / f"{task['id']}.json", packet)
            write_json(paths.receipts / f"{task['id']}-pending-approval.json", receipt)
            moved = move_task(task_path, paths.pending)
            receipt["movedTo"] = str(moved)
            processed.append(receipt)
            continue

        packet = build_dispatch_packet(task, manifest)
        receipt = {
            "status": "ready_for_agent_branch_work",
            "taskId": task["id"],
            "checkedAt": utc_now(),
            "summary": "Task is validated and ready for an agent-only branch. Production deploy and external mutation remain blocked.",
            "packet": packet,
        }
        receipt["receiptSha256"] = sha256_json(receipt)
        for outbox_name in ("q", "immaculate", "jaws", "discord", "laas"):
            write_json(paths.outbox / outbox_name / f"{task['id']}.json", packet)
        write_json(paths.receipts / f"{task['id']}-ready.json", receipt)
        moved = move_task(task_path, paths.ready)
        receipt["movedTo"] = str(moved)
        processed.append(receipt)

    return {
        "status": "ok",
        "checkedAt": utc_now(),
        "processedCount": len(processed),
        "items": processed,
    }


def command_status(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    snapshot = build_status(manifest, args.timeout)
    if args.write_snapshot:
        write_json(paths.status / "latest.json", snapshot)
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0 if snapshot["status"] == "ok" else 1


def command_doctor(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    groups = manifest.get("commands", {})
    selected = args.group or sorted(groups)
    results = []
    for group in selected:
        for command in groups.get(group, []):
            if args.execute:
                results.append(run_command(command))
            else:
                argv = [str(value) for value in command.get("argv", [])]
                cwd = normalize_path(str(command.get("cwd", REPO_ROOT)))
                results.append(
                    {
                        "id": command.get("id"),
                        "group": group,
                        "status": "ready" if cwd.exists() and command_available(argv) else "failed",
                        "cwd": str(cwd),
                        "argv": argv,
                        "execute": False,
                    }
                )
    report = {
        "status": "ok" if all(item["status"] in {"ok", "ready"} for item in results) else "warning",
        "checkedAt": utc_now(),
        "execute": bool(args.execute),
        "results": results,
    }
    write_json(paths.status / "doctor-latest.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def command_enqueue(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    result = create_task(args, manifest, paths)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_process(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    report = process_tasks(manifest, paths, args.limit)
    write_json(paths.status / "process-latest.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def command_seed(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    seeds = [
        {
            "kind": "website_guard",
            "title": "Protect Aura Genesis LaaS shell deploy route",
            "objective": "Continuously verify the live Aura Genesis site serves the protected React/LaaS/Arobi shell, route family, public APIs, and replay surface without deploying from legacy roots.",
            "target_root": manifest["localRoots"]["website"],
            "lane": "public",
            "allowed_write_path": [str(paths.state_root / "candidate-workspaces" / "aura-shell-guard")],
            "evaluator": ["npm", "run", "operator:handoff:check"],
            "timeout_sec": 120,
            "approval_required": False,
        },
        {
            "kind": "q_gateway_eval",
            "title": "Improve Q gateway substrate with benchmark receipts",
            "objective": "Search for guarded Q gateway prompt, routing, or timeout improvements using Immaculate benchmark receipts and no production mutation.",
            "target_root": manifest["localRoots"]["immaculate"],
            "lane": "private",
            "allowed_write_path": [str(paths.state_root / "candidate-workspaces" / "q-gateway")],
            "evaluator": ["npm", "run", "benchmark:q:substrate"],
            "timeout_sec": 600,
            "approval_required": True,
        },
        {
            "kind": "discord_workstation_eval",
            "title": "Harden Discord workstation document and approval flow",
            "objective": "Exercise document intake, governed web fetch, approval queue, and operator delivery with exact approval gates and no external send.",
            "target_root": manifest["localRoots"]["openjaws"],
            "lane": "private",
            "allowed_write_path": [str(paths.state_root / "candidate-workspaces" / "discord-workstation")],
            "evaluator": ["bun", "run", "serious:approval:ready"],
            "timeout_sec": 180,
            "approval_required": False,
        },
    ]
    created = []
    for seed in seeds:
        ns = argparse.Namespace(id=None, **seed)
        created.append(create_task(ns, manifest, paths)["taskPath"])
    report = {"status": "ok", "createdAt": utc_now(), "taskPaths": created}
    if args.process:
        report["process"] = process_tasks(manifest, paths, args.limit)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded ASI-Evolve bridge for Arobi systems")
    parser.add_argument("--manifest", default=None, help="Path to bridge manifest JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Check public/local route health")
    status.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    status.add_argument("--write-snapshot", action="store_true")

    doctor = subparsers.add_parser("doctor", help="Check or run configured local verification commands")
    doctor.add_argument("--group", action="append", choices=["website", "immaculate", "openjaws"])
    doctor.add_argument("--execute", action="store_true", help="Run commands instead of checking availability")

    enqueue = subparsers.add_parser("enqueue", help="Create a guarded evolution task")
    enqueue.add_argument("--id", default=None)
    enqueue.add_argument("--kind", required=True)
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--objective", required=True)
    enqueue.add_argument("--target-root", required=True)
    enqueue.add_argument("--lane", choices=["public", "private", "zero-zero"], default="private")
    enqueue.add_argument("--allowed-write-path", action="append", default=[], required=True)
    enqueue.add_argument("--evaluator", nargs="+", required=True)
    enqueue.add_argument("--timeout-sec", type=int, required=True)
    enqueue.add_argument("--approval-required", action="store_true")

    process = subparsers.add_parser("process", help="Validate inbox tasks and emit dispatch packets")
    process.add_argument("--limit", type=int, default=10)

    seed = subparsers.add_parser("seed-arobi", help="Seed initial Arobi task queue")
    seed.add_argument("--process", action="store_true")
    seed.add_argument("--limit", type=int, default=10)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    paths = bridge_paths(manifest)

    if args.command == "status":
        return command_status(manifest, paths, args)
    if args.command == "doctor":
        return command_doctor(manifest, paths, args)
    if args.command == "enqueue":
        return command_enqueue(manifest, paths, args)
    if args.command == "process":
        return command_process(manifest, paths, args)
    if args.command == "seed-arobi":
        return command_seed(manifest, paths, args)

    parser.error(f"unknown command: {args.command}")
    return 2
