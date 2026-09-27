"""Guarded Arobi bridge for the ASI-Evolve fork.

This module does not run ASI-Evolve rounds or mutate production. It checks
route health, reads operator analytics, validates task contracts, holds serious
tasks until signed founder and policy-governor approvals verify, and delivers
signed dispatch packets to Immaculate's governed ASI intake
(``POST /api/asi/dispatch``). Immaculate's roundtable is the fan-out to Q,
OpenJaws and Discord; the bridge keeps no per-lane outboxes of its own.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html as html_lib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .approvals import (
    DEFAULT_TOKEN_TTL,
    REQUIRED_ROLES,
    ROLE_FOUNDER,
    ROLE_GOVERNOR,
    ROLE_SECRET_ENV,
    approval_secret,
    evaluate_task_approvals,
    mint_approval_token,
    payload_integrity_errors,
    task_payload_sha256,
    verify_approval_token,
)
from .canonical import sha256_canonical
from .dispatch import (
    DEFAULT_PACKET_TTL,
    build_packet_body,
    classify_delivery,
    default_issuer,
    dispatch_url,
    immaculate_api_key,
    packet_expired,
    post_dispatch_packet,
    seal_receipt,
    sign_dispatch_packet,
    signing_key,
    verify_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "arobi_integrations" / "default_manifest.json"
DEFAULT_TIMEOUT = 8
DEFAULT_TELEMETRY_SITES = ("aura-genesis.org", "qline.site", "iorch.net")
DEFAULT_FUNNEL_PATHS = {
    "pricing": "/pricing",
    "autonomo": "/autonomo",
    "apex": "/app/apex-os",
    "dashboard": "/dashboard",
}
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:([\\/]|$)")
ENV_TEMPLATE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ROOT_TEMPLATE_PATTERN = re.compile(r"<root:([A-Za-z0-9_]+)>")
TITLE_PATTERN = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SCRIPT_TAG_PATTERN = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
ATTRIBUTE_PATTERN = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
TRUTHY = {"1", "true", "yes", "on"}
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
PAID_TOKEN_ORDER_STATUSES = {
    "paid_pending_governance",
    "governance_approved",
    "release_submitted",
    "settled",
}
ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}
SECRET_ENV_NAMES = {
    "DISCORD_BOT_TOKEN",
    "DISCORD_DEFAULT_CHANNEL_ID",
    "DISCORD_FOUNDER_USER_IDS",
    "DISCORD_OPERATOR_USER_ID",
    "DISCORD_WEBHOOK_URL",
    "RESEND_API_KEY",
    "SITE_TELEMETRY_READ_TOKEN",
    "AURA_TELEMETRY_READ_TOKEN",
    "STRIPE_SECRET_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_SERVICE_ROLE",
    "SUPABASE_SECRET_KEY",
}


class ForeignPathError(ValueError):
    """A path written for another operating system (e.g. ``D:/...`` on Linux)."""


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
    reports: Path
    logs: Path
    dispatch_outbox: Path
    dispatch_delivered: Path
    dispatch_rejected: Path


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


def env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get(name) or "").strip().lower() in TRUTHY


# ---------------------------------------------------------------------------
# Manifest paths and templates
# ---------------------------------------------------------------------------


def expand_env_template(raw: str, env: Mapping[str, str] | None = None) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}``; an unset VAR without default is ``""``."""
    source = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        value = source.get(match.group(1))
        if value is not None and value.strip():
            return value.strip()
        return match.group(2) if match.group(2) is not None else ""

    return ENV_TEMPLATE_PATTERN.sub(replace, raw)


def resolve_path_text(expanded: str, base: Path) -> Path:
    text = os.path.expanduser(expanded.strip())
    if not text:
        raise ValueError("path is empty")
    if os.name != "nt" and WINDOWS_DRIVE_PATTERN.match(text):
        raise ForeignPathError(
            f"{text} is a Windows drive path and cannot be used on this OS; set the matching "
            "AROBI_EVOLVE_* override or pass --manifest with local paths"
        )
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def normalize_path(raw: str) -> Path:
    """Resolve an operator-supplied path (CLI or task JSON) against the cwd."""
    return resolve_path_text(str(raw), Path.cwd())


def resolve_local_roots(manifest: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[dict[str, Path], dict[str, str]]:
    roots: dict[str, Path] = {}
    errors: dict[str, str] = {}
    for name, raw in sorted((manifest.get("localRoots") or {}).items()):
        try:
            roots[name] = resolve_path_text(expand_env_template(str(raw), env), REPO_ROOT)
        except ValueError as error:
            errors[name] = str(error)
    return roots, errors


def resolve_state_root(manifest: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Path:
    raw = str(manifest.get("stateRoot") or "${AROBI_EVOLVE_STATE_ROOT:-.arobi-evolve}")
    return resolve_path_text(expand_env_template(raw, env), REPO_ROOT)


def expand_manifest_value(raw: str, manifest: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """Expand env templates, then ``<root:NAME>``, ``<state>`` and ``<filesystem-root>``."""
    text = expand_env_template(str(raw), env)
    if "<root:" in text:
        roots, errors = resolve_local_roots(manifest, env)

        def replace_root(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in roots:
                return str(roots[name])
            raise ValueError(errors.get(name) or f"manifest references unknown local root {name!r}")

        text = ROOT_TEMPLATE_PATTERN.sub(replace_root, text)
    if "<state>" in text:
        text = text.replace("<state>", str(resolve_state_root(manifest, env)))
    if "<filesystem-root>" in text:
        text = text.replace("<filesystem-root>", REPO_ROOT.anchor)
    return text


def resolve_manifest_path(raw: str, manifest: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Path:
    return resolve_path_text(expand_manifest_value(raw, manifest, env), REPO_ROOT)


def bridge_paths(manifest: Mapping[str, Any]) -> BridgePaths:
    state_root = resolve_state_root(manifest)
    paths = BridgePaths(
        state_root=state_root,
        inbox=state_root / "tasks" / "inbox",
        pending=state_root / "tasks" / "pending-approval",
        ready=state_root / "tasks" / "ready",
        processed=state_root / "tasks" / "processed",
        rejected=state_root / "tasks" / "rejected",
        receipts=state_root / "receipts",
        status=state_root / "status",
        reports=state_root / "reports",
        logs=state_root / "logs",
        dispatch_outbox=state_root / "dispatch" / "outbox",
        dispatch_delivered=state_root / "dispatch" / "delivered",
        dispatch_rejected=state_root / "dispatch" / "rejected",
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


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hash_identity(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def bridge_version() -> str:
    """The checked-out commit of this bridge, or the package version without git metadata."""
    git_path = REPO_ROOT / ".git"
    try:
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_path = (REPO_ROOT / pointer.split(":", 1)[1].strip()).resolve()
        head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:12]
        ref = head.split(":", 1)[1].strip()
        ref_file = git_path / ref
        if ref_file.exists():
            return ref_file.read_text(encoding="utf-8").strip()[:12]
        common = git_path / "commondir"
        base = (git_path / common.read_text(encoding="utf-8").strip()).resolve() if common.exists() else git_path
        for candidate in (base / ref, base / "packed-refs"):
            if candidate.name == "packed-refs" and candidate.exists():
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    if line.endswith(f" {ref}"):
                        return line.split(" ", 1)[0][:12]
            elif candidate.exists():
                return candidate.read_text(encoding="utf-8").strip()[:12]
    except OSError:
        pass
    return __version__


def bridge_user_agent() -> str:
    return f"arobi-asi-evolve-bridge/{bridge_version()}"


def is_usable_env_value(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip()
    return bool(
        normalized
        and not normalized.startswith("****")
        and not normalized.lower().startswith("no value set")
        and normalized.lower() not in {"undefined", "null"}
        and "not found" not in normalized.lower()
    )


def netlify_env_value(name: str, manifest: dict[str, Any]) -> str | None:
    roots, _errors = resolve_local_roots(manifest)
    website_root = roots.get("website")
    npx = resolve_command_argv(["npx"])
    if npx is None or website_root is None or not website_root.exists():
        return None
    for context in ("production", "dev"):
        try:
            result = subprocess.run(
                [*npx, "netlify", "env:get", name, "--context", context],
                cwd=str(website_root),
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        except Exception:
            continue
        if result.returncode == 0 and is_usable_env_value(result.stdout):
            return result.stdout.strip()
    return None


def powershell_secret_env_value(name: str, manifest: dict[str, Any]) -> str | None:
    powershell = resolve_command_argv(["powershell"])
    if powershell is None:
        return None
    for raw_script in manifest.get("secretEnvScripts", []):
        try:
            script = resolve_manifest_path(str(raw_script), manifest)
        except ValueError:
            continue
        if not script.exists():
            continue
        escaped_script = str(script).replace("'", "''")
        command = (
            f". '{escaped_script}'; "
            f"$value = [Environment]::GetEnvironmentVariable('{name}', 'Process'); "
            "if ($value) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($value)) }"
        )
        try:
            result = subprocess.run(
                [*powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            decoded = base64.b64decode(result.stdout.strip()).decode("utf-8")
        except Exception:
            continue
        if is_usable_env_value(decoded):
            return decoded.strip()
    return None


def env_value(name: str, manifest: dict[str, Any], allow_netlify: bool = True) -> str | None:
    value = os.environ.get(name)
    if is_usable_env_value(value):
        return value.strip()
    if name in SECRET_ENV_NAMES:
        secret_value = powershell_secret_env_value(name, manifest)
        if secret_value:
            return secret_value
    if allow_netlify:
        return netlify_env_value(name, manifest)
    return None


def first_env(names: list[str], manifest: dict[str, Any], allow_netlify: bool = True) -> str | None:
    for name in names:
        value = env_value(name, manifest, allow_netlify=allow_netlify)
        if value:
            return value
    return None


def http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "User-Agent": bridge_user_agent(),
            **(headers or {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            payload = json.loads(text) if text.strip() else None
            return {"ok": True, "status": int(response.status), "payload": payload}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "status": int(error.code), "error": redact_text(detail or str(error))}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "status": None, "error": redacted_error(error)}


def http_text(url: str, *, timeout: int, max_bytes: int = 262144) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": bridge_user_agent()}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ok": True, "status": int(response.status), "text": response.read(max_bytes).decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status": int(error.code), "error": redacted_error(error)}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "status": None, "error": redacted_error(error)}


def top_counts(items: list[str | None], limit: int = 12, include_none: bool = True) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        key = item.strip() if isinstance(item, str) and item.strip() else "(none)"
        if key == "(none)" and not include_none:
            continue
        counts[key] = counts.get(key, 0) + 1
    return [
        {"key": key, "count": count}
        for key, count in sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))[:limit]
    ]


def bucket_day(value: Any) -> str:
    parsed = parse_iso(value)
    return parsed.date().isoformat() if parsed else "unknown"


def funnel_paths(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    configured = ((manifest or {}).get("analytics") or {}).get("funnelPaths") or {}
    return {**DEFAULT_FUNNEL_PATHS, **{key: str(value) for key, value in configured.items() if value}}


def telemetry_sites(manifest: Mapping[str, Any] | None, env: Mapping[str, str] | None = None) -> list[str]:
    source = os.environ if env is None else env
    override = (source.get("AROBI_EVOLVE_TELEMETRY_SITES") or "").strip()
    if override:
        return [site.strip() for site in override.split(",") if site.strip()]
    configured = ((manifest or {}).get("analytics") or {}).get("telemetrySites")
    if isinstance(configured, list) and configured:
        return [str(site) for site in configured]
    return list(DEFAULT_TELEMETRY_SITES)


def aggregate_telemetry_rows(rows: list[dict[str, Any]], manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    paths_config = funnel_paths(manifest)
    sessions = {
        str(row.get("session_id")).strip()
        for row in rows
        if isinstance(row.get("session_id"), str) and str(row.get("session_id")).strip()
    }
    event_types = [row.get("event_type") if isinstance(row.get("event_type"), str) else None for row in rows]
    paths = [row.get("path") if isinstance(row.get("path"), str) else None for row in rows]
    targets = [
        (row.get("target_label") if isinstance(row.get("target_label"), str) and row.get("target_label") else row.get("target_url"))
        if isinstance(row.get("target_label") or row.get("target_url"), str)
        else None
        for row in rows
    ]

    def path_views(prefix: str) -> int:
        return sum(1 for row in rows if str(row.get("path") or "").startswith(prefix))

    page_views = sum(1 for value in event_types if value == "page_view")
    checkout_intent = sum(
        1
        for row in rows
        if str(row.get("path") or "").startswith(paths_config["pricing"])
        or "checkout" in str(row.get("target_url") or "").lower()
        or "subscribe" in str(row.get("target_label") or "").lower()
        or "supporter" in str(row.get("target_label") or "").lower()
        or "token" in str(row.get("target_label") or "").lower()
    )
    return {
        "totalEvents": len(rows),
        "anonymousSessions": len(sessions),
        "bySite": top_counts([row.get("site") if isinstance(row.get("site"), str) else None for row in rows], include_none=False),
        "byEventType": top_counts(event_types),
        "topPaths": top_counts(paths),
        "topTargets": top_counts(targets, include_none=False),
        "topReferrers": top_counts([row.get("referrer") if isinstance(row.get("referrer"), str) else None for row in rows]),
        "byCountry": top_counts([row.get("country") if isinstance(row.get("country"), str) else None for row in rows]),
        "byRegion": top_counts([row.get("region") if isinstance(row.get("region"), str) else None for row in rows]),
        "byCity": top_counts([row.get("city") if isinstance(row.get("city"), str) else None for row in rows]),
        "byDay": top_counts([bucket_day(row.get("occurred_at")) for row in rows], limit=31),
        "funnel": {
            "paths": paths_config,
            "pageViews": page_views,
            "pricingIntentClicks": checkout_intent,
            "autonomoViews": path_views(paths_config["autonomo"]),
            "apexViews": path_views(paths_config["apex"]),
            "dashboardViews": path_views(paths_config["dashboard"]),
            "intentRate": round(checkout_intent / page_views, 4) if page_views else 0,
        },
        "recent": [
            {
                "occurredAt": row.get("occurred_at"),
                "site": row.get("site"),
                "eventType": row.get("event_type"),
                "path": row.get("path"),
                "target": row.get("target_label") or row.get("target_url") or None,
                "country": row.get("country"),
                "region": row.get("region"),
                "city": row.get("city"),
                "referrer": row.get("referrer"),
            }
            for row in rows[:25]
        ],
    }


def summarize_status_delta(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous_services = {
        service.get("id"): service
        for service in (previous or {}).get("services", [])
        if isinstance(service, dict) and service.get("id")
    }
    new_failures = []
    recoveries = []
    for service in current.get("services", []):
        if not isinstance(service, dict):
            continue
        service_id = service.get("id")
        current_status = service.get("status")
        previous_status = previous_services.get(service_id, {}).get("status")
        normalized = {
            "id": service_id,
            "label": service.get("label", service_id),
            "status": current_status,
        }
        if current_status not in {"ok", "optional_failed"} and previous_status in {None, "ok", "optional_failed"}:
            new_failures.append(normalized)
        if current_status == "ok" and previous_status not in {None, "ok", "optional_failed"}:
            recoveries.append(normalized)
    return {"newFailures": new_failures, "recoveries": recoveries}


def value_at(source: dict[str, Any] | None, dotted: str, default: float = 0) -> float:
    value: Any = source or {}
    for key in dotted.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return default
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


ANALYTICS_DELTA_METRICS = {
    "supabase-direct": {
        "newUsers": ("business.users.total", "auth users"),
        "newPaidTokenOrders": ("business.tokenOrders.paidOrderCount", "paid/proven token orders"),
        "newPaidTokenUsd": ("business.tokenOrders.paidUsd", "paid token order USD"),
        "newActiveSubscriptions": ("business.subscriptions.activeOrTrialing", "active/trialing subscriptions"),
        "newTelemetryEvents": ("telemetry.totalEvents", "site telemetry events in the window"),
    },
    "asgard-operator-analytics": {
        "newUsers": ("business.users.total", "registered accounts"),
        "newPaidTokenOrders": ("business.tokenOrders.finalizedTokenDeliveries", "finalized AURA token deliveries"),
        "newActiveSubscriptions": ("business.subscriptions.verifiedPayingTenants", "verified paying tenants"),
        "newTelemetryEvents": ("telemetry.totalEvents30d", "unverified telemetry events (30d)"),
    },
}
ANALYTICS_DELTA_KEYS = ("newUsers", "newPaidTokenOrders", "newPaidTokenUsd", "newActiveSubscriptions", "newTelemetryEvents")


def empty_analytics_delta(reason: str) -> dict[str, Any]:
    return {**{key: 0 for key in ANALYTICS_DELTA_KEYS}, "comparable": False, "reason": reason, "labels": {}}


def summarize_analytics_delta(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    current_source = current.get("source", "supabase-direct")
    previous_source = (previous or {}).get("source", "supabase-direct")
    if previous is None:
        return empty_analytics_delta("no previous analytics report")
    if current_source != previous_source:
        return empty_analytics_delta(f"analytics source changed from {previous_source} to {current_source}")
    metrics = ANALYTICS_DELTA_METRICS.get(current_source)
    if metrics is None:
        return empty_analytics_delta(f"analytics source {current_source} has no comparable metrics")
    delta: dict[str, Any] = {key: 0 for key in ANALYTICS_DELTA_KEYS}
    for key, (path, _label) in metrics.items():
        change = max(0.0, value_at(current, path) - value_at(previous, path))
        delta[key] = round(change, 2) if key == "newPaidTokenUsd" else int(change)
    delta["comparable"] = True
    delta["labels"] = {key: label for key, (_path, label) in metrics.items()}
    return delta


# ---------------------------------------------------------------------------
# Route health
# ---------------------------------------------------------------------------


def probe_auth_headers(service: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[dict[str, str], str | None]:
    """Headers for an authenticated probe, or the env var that is missing."""
    auth = service.get("auth")
    if not isinstance(auth, Mapping):
        return {}, None
    source = os.environ if env is None else env
    env_name = str(auth.get("env", ""))
    value = (source.get(env_name) or "").strip() if env_name else ""
    if not value:
        return {}, env_name or "auth.env"
    scheme = str(auth.get("scheme", "")).strip()
    return {str(auth.get("header", "Authorization")): f"{scheme} {value}" if scheme else value}, None


def http_probe(service: dict[str, Any], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    expected = set(int(value) for value in service.get("expectedStatuses", [200]))
    base = {
        "id": service["id"],
        "label": service.get("label", service["id"]),
        "url": service.get("url"),
        "expectedStatuses": sorted(expected),
        "optional": bool(service.get("optional", False)),
        "probeGroup": service.get("probeGroup"),
        "recoveryTarget": service.get("recoveryTarget"),
    }
    if not service.get("url"):
        return {**base, "status": "optional_failed" if service.get("optional") else "failed", "httpStatus": None, "elapsedMs": 0, "error": "service URL is empty after env expansion"}
    auth_headers, missing_auth = probe_auth_headers(service)
    if missing_auth:
        # Probing without the credential would only measure the 401 and report
        # a false outage, so the route is reported as unmeasured instead.
        return {
            **base,
            "status": "not_configured",
            "httpStatus": None,
            "elapsedMs": 0,
            "missing": [missing_auth],
            "error": f"{missing_auth} is not configured; this authenticated route was not probed",
        }
    request = urllib.request.Request(
        str(service["url"]),
        headers={"User-Agent": bridge_user_agent(), **auth_headers},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            sample = response.read(512)
        return {
            **base,
            "status": "ok" if status in expected else "warning",
            "httpStatus": status,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
            "sampleSha256": hashlib.sha256(sample).hexdigest(),
        }
    except urllib.error.HTTPError as error:
        status = int(error.code)
        return {
            **base,
            "status": "ok" if status in expected else ("optional_failed" if service.get("optional") else "failed"),
            "httpStatus": status,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
            "error": redacted_error(error),
        }
    except Exception as error:  # noqa: BLE001 - status reporting must not crash the bridge.
        return {
            **base,
            "status": "optional_failed" if service.get("optional") else "failed",
            "httpStatus": None,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
            "error": redacted_error(error),
        }


def manifest_services(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    services = []
    for service in manifest.get("services", []):
        if not isinstance(service, dict):
            continue
        services.append({**service, "url": expand_env_template(str(service.get("url", "")))})
    return services


def check_local_roots(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    roots, errors = resolve_local_roots(manifest)
    results = []
    for key in sorted({*roots, *errors}):
        if key in errors:
            results.append({"id": key, "path": None, "status": "failed", "exists": False, "isDir": False, "error": errors[key]})
            continue
        path = roots[key]
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


def check_operator_state_dirs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Presence of operator-machine state directories. Informational, never a failure."""
    results = []
    for key, raw in sorted((manifest.get("operatorStateDirs") or {}).items()):
        try:
            path = resolve_manifest_path(str(raw), manifest)
        except ValueError as error:
            results.append({"id": key, "path": None, "present": False, "error": str(error)})
            continue
        results.append({"id": key, "path": str(path), "present": path.is_dir()})
    return results


def extract_title(document: str) -> str | None:
    match = TITLE_PATTERN.search(document)
    return html_lib.unescape(match.group(1)).strip() if match else None


def extract_module_entries(document: str) -> list[str]:
    entries = []
    for match in SCRIPT_TAG_PATTERN.finditer(document):
        attributes = {
            name.lower(): value.strip("\"'") for name, value in ATTRIBUTE_PATTERN.findall(match.group(1))
        }
        if attributes.get("type", "").lower() == "module" and attributes.get("src"):
            entries.append(attributes["src"])
    return entries


def check_website_artifact(manifest: dict[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Check the built website against its own source, read at check time.

    The expected title comes from ``AROBI_EVOLVE_REQUIRED_TITLE`` or, by default,
    the website source ``index.html``; entry bundles come from the built
    ``dist/index.html`` (and ``dist/.vite/manifest.json`` when present).
    Nothing here is a remembered literal that can go stale.
    """
    source = os.environ if env is None else env
    website = manifest.get("website", {})
    findings: list[str] = []
    try:
        artifact_root = resolve_manifest_path(str(website.get("artifactRoot", "<root:website>/dist")), manifest, env)
        source_index = resolve_manifest_path(str(website.get("sourceIndex", "<root:website>/index.html")), manifest, env)
    except ValueError as error:
        return {"status": "failed", "findings": [f"website paths cannot be resolved: {error}"], "checkedAt": utc_now()}
    index_path = artifact_root / "index.html"
    roots, _errors = resolve_local_roots(manifest, env)
    website_root = roots.get("website")

    env_title = (source.get("AROBI_EVOLVE_REQUIRED_TITLE") or "").strip()
    expected_title: str | None = None
    expected_title_source: str | None = None
    if env_title:
        expected_title, expected_title_source = env_title, "env:AROBI_EVOLVE_REQUIRED_TITLE"
    elif source_index.exists():
        expected_title = extract_title(source_index.read_text(encoding="utf-8", errors="replace"))
        expected_title_source = str(source_index)
        if not expected_title:
            findings.append("website source index.html has no <title>")
    else:
        findings.append("expected title is unavailable: source index.html is missing and AROBI_EVOLVE_REQUIRED_TITLE is unset")

    dist_title: str | None = None
    entries: list[str] = []
    if not index_path.exists():
        findings.append("dist index.html is missing")
    else:
        dist_html = index_path.read_text(encoding="utf-8", errors="replace")
        dist_title = extract_title(dist_html)
        if expected_title and dist_title != expected_title:
            findings.append("dist title does not match the expected title")
        entries = extract_module_entries(dist_html)
        if not entries:
            findings.append("dist index.html references no module entry bundle")
        for entry in entries:
            if urllib.parse.urlparse(entry).scheme:
                findings.append(f"dist entry bundle is not served from the artifact: {entry}")
                continue
            if not (artifact_root / entry.lstrip("/")).is_file():
                findings.append(f"dist entry bundle is missing from the artifact: {entry}")
        vite_manifest = artifact_root / ".vite" / "manifest.json"
        if vite_manifest.exists():
            try:
                entry_file = load_json(vite_manifest).get("index.html", {}).get("file")
            except (ValueError, OSError) as error:
                findings.append(f"dist .vite/manifest.json is unreadable: {error}")
            else:
                if entry_file and f"/{entry_file}" not in entries and entry_file not in entries:
                    findings.append("dist index.html does not reference the Vite manifest entry bundle")
        required_marker = (source.get("AROBI_EVOLVE_REQUIRED_BUNDLE_MARKER") or "").strip()
        if required_marker and required_marker not in dist_html:
            findings.append("AROBI_EVOLVE_REQUIRED_BUNDLE_MARKER is not referenced by dist index.html")

    forbidden = []
    for raw_path in website.get("forbiddenDeployRoots", []):
        try:
            path = resolve_manifest_path(str(raw_path), manifest, env)
        except ValueError:
            continue
        if website_root is not None and path == website_root:
            forbidden.append(str(path))
    if forbidden:
        findings.append("forbidden deploy roots include canonical root")

    return {
        "status": "ok" if not findings else "failed",
        "checkedAt": utc_now(),
        "websiteRoot": str(website_root) if website_root else None,
        "artifactRoot": str(artifact_root),
        "indexPath": str(index_path),
        "expectedTitle": expected_title,
        "expectedTitleSource": expected_title_source,
        "distTitle": dist_title,
        "entryBundles": entries,
        "findings": findings,
    }


def check_live_deploy(manifest: Mapping[str, Any], timeout: int, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The currently published Netlify deploy, read from the Netlify API at check time."""
    source = os.environ if env is None else env
    token = (source.get("NETLIFY_AUTH_TOKEN") or "").strip()
    site_id = (source.get("NETLIFY_SITE_ID") or "").strip()
    missing = [name for name, value in (("NETLIFY_AUTH_TOKEN", token), ("NETLIFY_SITE_ID", site_id)) if not value]
    if missing:
        return {"status": "not_configured", "missing": missing, "reason": "The live deploy id is only reported when read from the Netlify API."}
    api_base = (source.get("NETLIFY_API_URL") or "https://api.netlify.com").rstrip("/")
    result = http_json(
        f"{api_base}/api/v1/sites/{urllib.parse.quote(site_id, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    payload = result.get("payload") if result["ok"] else None
    deploy = payload.get("published_deploy") if isinstance(payload, dict) else None
    if not isinstance(deploy, dict) or not deploy.get("id"):
        return {"status": "unavailable", "httpStatus": result.get("status"), "error": result.get("error") or "Netlify site has no published_deploy"}
    return {
        "status": "ok",
        "source": "netlify-api",
        "checkedAt": utc_now(),
        "siteId": site_id,
        "publishedDeployId": deploy.get("id"),
        "publishedAt": deploy.get("published_at"),
        "commitRef": deploy.get("commit_ref"),
        "deployUrl": deploy.get("deploy_ssl_url") or deploy.get("deploy_url"),
    }


def check_live_entry(manifest: Mapping[str, Any], local_entries: list[str], timeout: int) -> dict[str, Any]:
    public_url = expand_env_template(str((manifest.get("website") or {}).get("publicUrl", "")))
    if not public_url:
        return {"status": "not_configured", "reason": "website.publicUrl is empty"}
    result = http_text(public_url, timeout=timeout)
    if not result["ok"]:
        return {"status": "unavailable", "url": public_url, "httpStatus": result.get("status"), "error": result.get("error")}
    live_entries = extract_module_entries(result["text"])
    return {
        "status": "ok",
        "url": public_url,
        "checkedAt": utc_now(),
        "liveEntryBundles": live_entries,
        "matchesLocalDist": bool(local_entries) and sorted(live_entries) == sorted(local_entries),
    }


def build_status(manifest: dict[str, Any], timeout: int) -> dict[str, Any]:
    services = manifest_services(manifest)
    service_results: list[dict[str, Any] | None] = [None] * len(services)
    if services:
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, service in enumerate(services):
            group = str(service.get("probeGroup") or f"service:{index}")
            groups.setdefault(group, []).append((index, service))

        def probe_group(items: list[tuple[int, dict[str, Any]]]) -> list[tuple[int, dict[str, Any]]]:
            return [(index, http_probe(service, timeout)) for index, service in items]

        max_workers = max(1, min(int(manifest.get("statusProbeWorkers", 6)), len(groups)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="arobi-status") as executor:
            future_map = {executor.submit(probe_group, items): items for items in groups.values()}
            for future in as_completed(future_map):
                try:
                    for index, result in future.result():
                        service_results[index] = result
                except Exception as error:  # noqa: BLE001
                    for index, service in future_map[future]:
                        service_results[index] = {
                            "id": service.get("id", f"service-{index}"),
                            "label": service.get("label", service.get("id", f"service-{index}")),
                            "url": service.get("url"),
                            "status": "failed",
                            "httpStatus": None,
                            "elapsedMs": 0,
                            "error": redacted_error(error),
                        }
    root_results = check_local_roots(manifest)
    artifact = check_website_artifact(manifest)
    live = {
        "deploy": check_live_deploy(manifest, timeout),
        "entry": check_live_entry(manifest, artifact.get("entryBundles", []), timeout),
    }
    failures = [
        result
        for result in [*(item for item in service_results if item is not None), *root_results, artifact]
        if result["status"] not in {"ok", "optional_failed"}
    ]
    return {
        "schemaVersion": 2,
        "checkedAt": utc_now(),
        "bridgeVersion": bridge_version(),
        "mode": manifest.get("mode"),
        "manifestPath": manifest.get("_manifestPath"),
        "status": "ok" if not failures else "warning",
        "publicDataPolicy": manifest.get("governance", {}).get("publicDataPolicy"),
        "websiteArtifact": artifact,
        "websiteLive": live,
        "services": [item for item in service_results if item is not None],
        "localRoots": root_results,
        "operatorStateDirs": check_operator_state_dirs(manifest),
        "failureCount": len(failures),
    }


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def supabase_config(manifest: dict[str, Any]) -> dict[str, str] | None:
    url = first_env(["SUPABASE_URL", "VITE_SUPABASE_URL"], manifest)
    secret = first_env(["SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_ROLE", "SUPABASE_SECRET_KEY"], manifest)
    if not url or not secret:
        return None
    return {"url": url.rstrip("/"), "secretKey": secret}


def supabase_rest_get(
    config: dict[str, str],
    table: str,
    params: dict[str, str],
    *,
    timeout: int = 25,
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params, safe="(),.*:")
    url = f"{config['url']}/rest/v1/{urllib.parse.quote(table, safe='')}"
    if query:
        url = f"{url}?{query}"
    return http_json(
        url,
        headers={
            "apikey": config["secretKey"],
            "Authorization": f"Bearer {config['secretKey']}",
        },
        timeout=timeout,
    )


def collect_supabase_table(
    config: dict[str, str],
    table: str,
    params: dict[str, str],
    *,
    timeout: int = 25,
) -> tuple[list[dict[str, Any]], str | None]:
    result = supabase_rest_get(config, table, params, timeout=timeout)
    if not result["ok"]:
        return [], f"{table}: {result.get('status') or 'network'} {result.get('error') or 'query failed'}"
    payload = result.get("payload")
    if not isinstance(payload, list):
        return [], f"{table}: unexpected payload"
    return [row for row in payload if isinstance(row, dict)], None


def collect_supabase_auth_users(config: dict[str, str], *, timeout: int = 25, max_pages: int = 20) -> tuple[list[dict[str, Any]], str | None]:
    users: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode({"page": page, "per_page": 100})
        result = http_json(
            f"{config['url']}/auth/v1/admin/users?{query}",
            headers={
                "apikey": config["secretKey"],
                "Authorization": f"Bearer {config['secretKey']}",
            },
            timeout=timeout,
        )
        if not result["ok"]:
            return users, f"auth.users: {result.get('status') or 'network'} {result.get('error') or 'query failed'}"
        payload = result.get("payload")
        page_users = payload.get("users") if isinstance(payload, dict) else None
        if not isinstance(page_users, list):
            return users, "auth.users: unexpected payload"
        users.extend(row for row in page_users if isinstance(row, dict))
        if len(page_users) < 100:
            break
    return users, None


def metadata(user: dict[str, Any], key: str) -> Any:
    app = user.get("app_metadata") if isinstance(user.get("app_metadata"), dict) else {}
    raw = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
    return app.get(key) if key in app else raw.get(key)


def summarize_users(users: list[dict[str, Any]], since_days: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_period = now - timedelta(days=since_days)
    confirmed = [user for user in users if user.get("email_confirmed_at")]
    active_subscription_users = []
    paid_tiers = {"supporter": 0, "commander": 0}
    government = 0
    admins = 0
    recent_hashes = []
    for user in users:
        created_at = parse_iso(user.get("created_at"))
        email = user.get("email") if isinstance(user.get("email"), str) else ""
        if created_at and created_at >= since_period and email:
            recent_hashes.append({"emailHash": hash_identity(email), "createdAt": created_at.isoformat().replace("+00:00", "Z")})
        tier = str(metadata(user, "subscription_tier") or "observer")
        if tier in paid_tiers:
            paid_tiers[tier] += 1
        status = str(metadata(user, "stripe_subscription_status") or "")
        if status in ACTIVE_SUBSCRIPTION_STATUSES:
            active_subscription_users.append(user)
        if metadata(user, "is_government") is True or metadata(user, "organization_type") == "government":
            government += 1
        roles = metadata(user, "roles")
        if metadata(user, "super_admin") is True or (isinstance(roles, list) and any(role in {"admin", "founder", "mission_control"} for role in roles)):
            admins += 1
    return {
        "total": len(users),
        "confirmed": len(confirmed),
        "unconfirmed": max(0, len(users) - len(confirmed)),
        "newLast24h": sum(1 for user in users if (created := parse_iso(user.get("created_at"))) and created >= since_24h),
        f"newLast{since_days}d": sum(1 for user in users if (created := parse_iso(user.get("created_at"))) and created >= since_period),
        "signedInAtLeastOnce": sum(1 for user in users if user.get("last_sign_in_at")),
        "governmentTagged": government,
        "adminTagged": admins,
        "paidTiers": paid_tiers,
        "recentUserHashes": sorted(recent_hashes, key=lambda row: row["createdAt"], reverse=True)[:20],
    }


def summarize_token_orders(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = top_counts([row.get("status") if isinstance(row.get("status"), str) else None for row in rows], limit=20)
    paid_rows = [row for row in rows if row.get("status") in PAID_TOKEN_ORDER_STATUSES]
    pending_rows = [row for row in rows if row.get("status") in {"checkout_pending", "checkout_expired"}]
    missing_proof = [
        row
        for row in paid_rows
        if not row.get("stripe_checkout_session_id") or not row.get("stripe_payment_intent_id") or not row.get("stripe_event_id")
    ]
    return {
        "totalOrders": len(rows),
        "paidOrderCount": len(paid_rows),
        "paidUsd": round(sum(float(row.get("usd_amount_cents") or 0) for row in paid_rows) / 100, 2),
        "pendingCheckoutCount": len(pending_rows),
        "byStatus": by_status,
        "paidOrdersMissingStripeProof": len(missing_proof),
        "recentPaidOrderHashes": [
            {
                "orderHash": hash_identity(str(row.get("id", ""))),
                "status": row.get("status"),
                "usd": round(float(row.get("usd_amount_cents") or 0) / 100, 2),
                "updatedAt": row.get("updated_at") or row.get("created_at"),
            }
            for row in paid_rows[:20]
        ],
    }


def summarize_newsletter(rows: list[dict[str, Any]]) -> dict[str, Any]:
    active = [row for row in rows if not row.get("unsubscribed_at")]
    return {
        "total": len(rows),
        "active": len(active),
        "unsubscribed": max(0, len(rows) - len(active)),
        "bySource": top_counts([row.get("source") if isinstance(row.get("source"), str) else None for row in active], limit=20),
    }


def summarize_contact(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(rows),
        "bySource": top_counts([row.get("source") if isinstance(row.get("source"), str) else None for row in rows], limit=20),
        "recentContactHashes": [
            {
                "emailHash": hash_identity(str(row.get("email", ""))),
                "source": row.get("source"),
                "createdAt": row.get("created_at"),
            }
            for row in rows[:20]
            if row.get("email")
        ],
    }


def summarize_api_keys(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(rows),
        "active": sum(1 for row in rows if not row.get("revoked_at")),
        "usedAtLeastOnce": sum(1 for row in rows if row.get("last_used_at")),
    }


def summarize_decisions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(rows),
        "bySource": top_counts([row.get("source") if isinstance(row.get("source"), str) else None for row in rows], limit=20),
        "byType": top_counts([row.get("decision_type") if isinstance(row.get("decision_type"), str) else None for row in rows], limit=20),
        "byReviewStatus": top_counts([row.get("review_status") if isinstance(row.get("review_status"), str) else None for row in rows], limit=20),
    }


def collect_stripe_summary(manifest: dict[str, Any], since_days: int, timeout: int) -> dict[str, Any]:
    key = env_value("STRIPE_SECRET_KEY", manifest)
    if not key:
        return {"status": "skipped", "reason": "STRIPE_SECRET_KEY is not available to the local operator."}
    since_unix = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
    headers = {"Authorization": f"Bearer {key}"}
    sessions = http_json(
        f"https://api.stripe.com/v1/checkout/sessions?limit=100&created%5Bgte%5D={since_unix}",
        headers=headers,
        timeout=timeout,
    )
    subscriptions = http_json(
        "https://api.stripe.com/v1/subscriptions?limit=100&status=all",
        headers=headers,
        timeout=timeout,
    )
    if not sessions["ok"] and sessions.get("status") == 401:
        return {
            "status": "warning",
            "reason": "Stripe rejected the local key with HTTP 401; using Supabase webhook records as payment source of truth.",
            "sessionsStatus": 401,
        }
    session_payload = sessions.get("payload") if sessions["ok"] else {}
    subscription_payload = subscriptions.get("payload") if subscriptions["ok"] else {}
    session_rows = session_payload.get("data", []) if isinstance(session_payload, dict) else []
    subscription_rows = subscription_payload.get("data", []) if isinstance(subscription_payload, dict) else []
    return {
        "status": "ok" if sessions["ok"] else "warning",
        "recentCheckoutSessions": len(session_rows) if isinstance(session_rows, list) else 0,
        "activeOrTrialingSubscriptions": sum(
            1
            for row in (subscription_rows if isinstance(subscription_rows, list) else [])
            if isinstance(row, dict) and row.get("status") in ACTIVE_SUBSCRIPTION_STATUSES
        ),
        "sessionsStatus": sessions.get("status"),
        "subscriptionsStatus": subscriptions.get("status"),
    }


def collect_direct_supabase_analytics(manifest: dict[str, Any], since_days: int, timeout: int) -> dict[str, Any]:
    """Explicit fallback: read Supabase with the service-role key and Stripe directly."""
    warnings: list[str] = []
    config = supabase_config(manifest)
    since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat().replace("+00:00", "Z")
    business: dict[str, Any] = {
        "users": {},
        "subscriptions": {},
        "tokenOrders": {},
        "newsletter": {},
        "contacts": {},
        "apiKeys": {},
        "tenantDecisions": {},
    }
    telemetry: dict[str, Any] = {
        "totalEvents": 0,
        "anonymousSessions": 0,
        "source": "unavailable",
    }
    sites = telemetry_sites(manifest)

    if not config:
        warnings.append("Supabase service configuration is unavailable; live user/payment analytics could not be collected.")
    else:
        users, error = collect_supabase_auth_users(config, timeout=timeout)
        if error:
            warnings.append(error)
        business["users"] = summarize_users(users, since_days)
        business["subscriptions"] = {
            "activeOrTrialing": sum(
                1
                for user in users
                if str(metadata(user, "stripe_subscription_status") or "") in ACTIVE_SUBSCRIPTION_STATUSES
            )
        }

        table_specs = {
            "tokenOrders": (
                "token_purchase_orders",
                {
                    "select": "id,status,customer_email,source,created_at,updated_at,usd_amount_cents,stripe_checkout_session_id,stripe_payment_intent_id,stripe_event_id",
                    "order": "updated_at.desc",
                    "limit": "5000",
                },
            ),
            "newsletter": (
                "newsletter_subscribers",
                {"select": "email,source,subscribed_at,unsubscribed_at", "order": "subscribed_at.desc", "limit": "5000"},
            ),
            "contacts": (
                "contact_inquiries",
                {"select": "email,source,created_at", "order": "created_at.desc", "limit": "5000"},
            ),
            "apiKeys": (
                "api_keys",
                {"select": "id,created_at,last_used_at", "order": "created_at.desc", "limit": "5000"},
            ),
            "tenantDecisions": (
                "tenant_decisions",
                {
                    "select": "id,timestamp,source,decision_type,review_status",
                    "timestamp": f"gte.{since_iso}",
                    "order": "timestamp.desc",
                    "limit": "5000",
                },
            ),
        }
        collected: dict[str, list[dict[str, Any]]] = {}
        for key, (table, params) in table_specs.items():
            rows, error = collect_supabase_table(config, table, params, timeout=timeout)
            if error:
                warnings.append(error)
            collected[key] = rows
        business["tokenOrders"] = summarize_token_orders(collected["tokenOrders"])
        business["newsletter"] = summarize_newsletter(collected["newsletter"])
        business["contacts"] = summarize_contact(collected["contacts"])
        business["apiKeys"] = summarize_api_keys(collected["apiKeys"])
        business["tenantDecisions"] = summarize_decisions(collected["tenantDecisions"])

        telemetry_rows, error = collect_supabase_table(
            config,
            "site_telemetry_events",
            {
                "select": "occurred_at,site,event_type,session_id,path,referrer,target_url,target_label,country,region,city,utm_source,utm_campaign",
                "site": f"in.({','.join(sites)})",
                "occurred_at": f"gte.{since_iso}",
                "order": "occurred_at.desc",
                "limit": "5000",
            },
            timeout=timeout,
        )
        if error:
            warnings.append(error)
        telemetry = {**aggregate_telemetry_rows(telemetry_rows, manifest), "source": "supabase.site_telemetry_events", "sites": sites}

    return {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "sinceDays": since_days,
        "source": "supabase-direct",
        "business": business,
        "telemetry": telemetry,
        "stripe": collect_stripe_summary(manifest, since_days, timeout),
        "warnings": warnings,
    }


def operator_analytics_url(manifest: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    configured = (source.get("AROBI_OPERATOR_ANALYTICS_URL") or "").strip()
    if configured:
        return configured
    public_url = expand_env_template(str((manifest.get("website") or {}).get("publicUrl", "https://aura-genesis.org")), env)
    return public_url.rstrip("/") + "/.netlify/functions/operator-analytics/summary"


def int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def operator_api_report(payload: dict[str, Any], url: str, since_days: int) -> dict[str, Any]:
    truth = payload.get("commercialTruth") if isinstance(payload.get("commercialTruth"), dict) else {}
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    warnings = [f"operator analytics {key}: {value}" for key, value in sorted(diagnostics.items()) if value]
    business: dict[str, Any] = {"users": {}, "subscriptions": {}, "tokenOrders": {}}
    if int_or_none(truth.get("totalAccounts")) is not None:
        business["users"]["total"] = truth["totalAccounts"]
    for key in ("verifiedPayingTenants", "demoTenants"):
        if int_or_none(truth.get(key)) is not None:
            business["subscriptions"][key] = truth[key]
    for key in ("finalizedTokenDeliveries", "uniqueTokenBuyers"):
        if int_or_none(truth.get(key)) is not None:
            business["tokenOrders"][key] = truth[key]
    telemetry: dict[str, Any] = {"source": "asgard-operator-analytics"}
    if int_or_none(payload.get("unverifiedTelemetryEvents30d")) is not None:
        telemetry["totalEvents30d"] = payload["unverifiedTelemetryEvents30d"]
    return {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "sinceDays": since_days,
        "source": "asgard-operator-analytics",
        "sourceUrl": url,
        "sourceGeneratedAt": payload.get("generatedAt"),
        "business": business,
        "telemetry": telemetry,
        "operatorAnalytics": {
            "commercialTruth": truth,
            "publicSurfaces": payload.get("publicSurfaces"),
            "seoStatus": payload.get("seoStatus"),
            "networkVersion": payload.get("networkVersion"),
        },
        "stripe": {
            "status": "skipped",
            "reason": "The operator analytics API is the revenue source; direct Stripe reads run only in the explicit direct fallback.",
        },
        "warnings": warnings,
    }


def collect_live_analytics(
    manifest: dict[str, Any],
    since_days: int,
    timeout: int,
    *,
    allow_direct: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Prefer the auth-gated Asgard operator analytics API.

    Direct Supabase/Stripe reads (service-role key on the operator machine) run
    only when explicitly enabled with ``--direct-supabase-fallback`` or
    ``AROBI_EVOLVE_ANALYTICS_DIRECT_FALLBACK=1``.
    """
    source = os.environ if env is None else env
    direct_allowed = env_flag("AROBI_EVOLVE_ANALYTICS_DIRECT_FALLBACK", source) if allow_direct is None else allow_direct
    token = (source.get("AROBI_OPERATOR_ANALYTICS_TOKEN") or "").strip()
    fallback_reason: str
    api_warning: str | None = None
    if token:
        url = operator_analytics_url(manifest, source)
        result = http_json(url, headers={"Authorization": f"Bearer {token}"}, timeout=max(timeout, 15))
        if result["ok"] and isinstance(result.get("payload"), dict):
            return operator_api_report(result["payload"], url, since_days)
        api_warning = f"operator analytics API failed: HTTP {result.get('status') or 'network'} {result.get('error') or ''}".strip()
        fallback_reason = api_warning
    else:
        fallback_reason = "AROBI_OPERATOR_ANALYTICS_TOKEN is not configured"
    if direct_allowed:
        report = collect_direct_supabase_analytics(manifest, since_days, timeout)
        report["fallbackReason"] = fallback_reason
        if api_warning:
            report["warnings"] = [api_warning, *report["warnings"]]
        return report
    return {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "sinceDays": since_days,
        "source": "not_configured" if not token else "unavailable",
        "business": {},
        "telemetry": {"source": "unavailable"},
        "stripe": {"status": "skipped", "reason": "No analytics source is available."},
        "warnings": [
            f"{fallback_reason}; the direct Supabase fallback is disabled "
            "(enable it explicitly with --direct-supabase-fallback or AROBI_EVOLVE_ANALYTICS_DIRECT_FALLBACK=1)."
        ],
    }


def render_count_rows(rows: list[dict[str, Any]], key_label: str = "Key") -> list[str]:
    if not rows:
        return ["No rows."]
    output = [f"| {key_label} | Count |", "| --- | ---: |"]
    for row in rows[:12]:
        key = str(row.get("key", "(none)")).replace("|", "\\|")
        if len(key) > 96:
            key = f"{key[:93]}..."
        output.append(f"| {key} | {row.get('count', 0)} |")
    return output


def percent(numerator: float, denominator: float) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def activation_signals(page_views: int, confirmed_users: int, active_subscriptions: int, active_keys: int, used_keys: int) -> list[str]:
    """Signals computed from this report's numbers, never a remembered sentence."""
    signals = []
    if page_views > 0 and active_subscriptions == 0:
        signals.append("Traffic is reaching the site while active/trialing subscriptions are at zero.")
    if active_keys > 0 and used_keys * 2 < active_keys:
        signals.append(f"Fewer than half of active API keys have been used ({used_keys}/{active_keys}).")
    if page_views > 0 and confirmed_users == 0:
        signals.append("No confirmed users in the auth store despite recorded page views.")
    return signals or ["No activation gap stands out in this window's numbers."]


def render_operator_api_markdown(report: dict[str, Any]) -> str:
    truth = (report.get("operatorAnalytics") or {}).get("commercialTruth") or {}

    def shown(value: Any) -> str:
        return "unavailable" if value is None else str(value)

    lines = [
        "# Arobi Deep Analytics Report",
        "",
        f"Generated: {report.get('generatedAt')}",
        f"Source: Asgard operator analytics API ({report.get('sourceUrl')}), generated {report.get('sourceGeneratedAt')}",
        "",
        "## Commercial Truth",
        "",
        f"- Registered accounts: {shown(truth.get('totalAccounts'))}.",
        f"- Verified paying tenants: {shown(truth.get('verifiedPayingTenants'))} (demo tenants, not revenue: {shown(truth.get('demoTenants'))}).",
        f"- Finalized AURA token deliveries: {shown(truth.get('finalizedTokenDeliveries'))}; unique buyers: {shown(truth.get('uniqueTokenBuyers'))}.",
        f"- Revenue status: {shown(truth.get('revenueStatus'))}.",
        f"- Unverified telemetry events (30d, aura-genesis.org): {shown((report.get('telemetry') or {}).get('totalEvents30d'))}.",
        "",
        "## Public Surfaces",
        "",
    ]
    surfaces = (report.get("operatorAnalytics") or {}).get("publicSurfaces") or []
    lines.extend(f"- {item.get('name')}: {item.get('status')} ({item.get('url')})" for item in surfaces if isinstance(item, dict))
    if not surfaces:
        lines.append("- Not reported.")
    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(str(line) for line in lines) + "\n"


def render_analytics_markdown(report: dict[str, Any], manifest: Mapping[str, Any] | None = None) -> str:
    if report.get("source") == "asgard-operator-analytics":
        return render_operator_api_markdown(report)
    if report.get("source") in {"not_configured", "unavailable"}:
        lines = ["# Arobi Deep Analytics Report", "", f"Generated: {report.get('generatedAt')}", "", "No analytics source was available.", ""]
        lines.extend(f"- {warning}" for warning in report.get("warnings", []))
        return "\n".join(lines) + "\n"
    business = report.get("business", {})
    telemetry = report.get("telemetry", {})
    users = business.get("users", {})
    token_orders = business.get("tokenOrders", {})
    subscriptions = business.get("subscriptions", {})
    newsletter = business.get("newsletter", {})
    contacts = business.get("contacts", {})
    api_keys = business.get("apiKeys", {})
    decisions = business.get("tenantDecisions", {})
    funnel = telemetry.get("funnel", {})
    page_views = int(funnel.get("pageViews", 0) or 0)
    pricing_clicks = int(funnel.get("pricingIntentClicks", 0) or 0)
    dashboard_views = int(funnel.get("dashboardViews", 0) or 0)
    sessions = int(telemetry.get("anonymousSessions", 0) or 0)
    confirmed_users = int(users.get("confirmed", 0) or 0)
    active_subscriptions = int(subscriptions.get("activeOrTrialing", 0) or 0)
    paid_orders = int(token_orders.get("paidOrderCount", 0) or 0)
    active_newsletter = int(newsletter.get("active", 0) or 0)
    used_keys = int(api_keys.get("usedAtLeastOnce", 0) or 0)
    active_keys = int(api_keys.get("active", 0) or 0)
    website_root = None
    if manifest is not None:
        roots, _errors = resolve_local_roots(manifest)
        website_root = roots.get("website")
    lines = [
        "# Arobi Deep Analytics Report",
        "",
        f"Generated: {report.get('generatedAt')}",
        f"Window: last {report.get('sinceDays')} days",
        f"Source: direct Supabase fallback ({report.get('fallbackReason', 'explicitly enabled')})",
        f"Telemetry sites: {', '.join(telemetry.get('sites', [])) or 'unavailable'}",
        "",
        "## Executive Snapshot",
        "",
        f"- Auth users: {users.get('total', 0)} total, {users.get('confirmed', 0)} confirmed, {users.get('newLast24h', 0)} new in 24h.",
        f"- Active or trialing subscriptions: {subscriptions.get('activeOrTrialing', 0)}.",
        f"- Token orders: {token_orders.get('totalOrders', 0)} total, {token_orders.get('paidOrderCount', 0)} paid/proven, ${token_orders.get('paidUsd', 0)} paid value.",
        f"- Newsletter: {newsletter.get('active', 0)} active of {newsletter.get('total', 0)} total.",
        f"- Contact inquiries: {contacts.get('total', 0)}.",
        f"- API keys: {api_keys.get('active', 0)} active of {api_keys.get('total', 0)} total, {api_keys.get('usedAtLeastOnce', 0)} used at least once.",
        f"- LaaS/tenant decision events: {decisions.get('total', 0)} in this window.",
        f"- Site telemetry: {telemetry.get('totalEvents', 0)} events across {telemetry.get('anonymousSessions', 0)} anonymous sessions.",
        "",
        "## Traffic And Clicks",
        "",
        "### By Site",
        *render_count_rows(telemetry.get("bySite", []), "Site"),
        "",
        "### Top Paths",
        *render_count_rows(telemetry.get("topPaths", []), "Path"),
        "",
        "### Top Click Targets",
        *render_count_rows(telemetry.get("topTargets", []), "Target"),
        "",
        "### Referrers",
        *render_count_rows(telemetry.get("topReferrers", []), "Referrer"),
        "",
        "### Locations",
        *render_count_rows(telemetry.get("byCountry", []), "Country"),
        "",
        "## Funnel Signals",
        "",
        f"- Page views: {page_views}.",
        f"- Pricing/checkout intent clicks: {pricing_clicks} ({percent(pricing_clicks, page_views)} of page views).",
        f"- Autonomo views: {funnel.get('autonomoViews', 0)}.",
        f"- ApexOS views: {funnel.get('apexViews', 0)}.",
        f"- Dashboard views: {dashboard_views} ({percent(dashboard_views, page_views)} of page views).",
        "",
        "## Conversion And Drop-Off",
        "",
        f"- Anonymous sessions to confirmed users: {confirmed_users}/{sessions} ({percent(confirmed_users, sessions)}).",
        f"- Confirmed users to active/trialing subscriptions: {active_subscriptions}/{confirmed_users} ({percent(active_subscriptions, confirmed_users)}).",
        f"- Anonymous sessions to paid/proven token orders: {paid_orders}/{sessions} ({percent(paid_orders, sessions)}).",
        f"- Newsletter leads to confirmed users: {confirmed_users}/{active_newsletter} ({percent(confirmed_users, active_newsletter)}).",
        f"- Active API keys used at least once: {used_keys}/{active_keys} ({percent(used_keys, active_keys)}).",
        *(f"- Signal: {signal}" for signal in activation_signals(page_views, confirmed_users, active_subscriptions, active_keys, used_keys)),
        "",
        "## Revenue Integrity",
        "",
        f"- Paid token orders missing Stripe proof: {token_orders.get('paidOrdersMissingStripeProof', 0)}.",
        f"- Stripe direct check: {report.get('stripe', {}).get('status', 'unknown')} ({report.get('stripe', {}).get('reason', 'no issue reported')}).",
        "",
        "## Operator Actions",
        "",
        f"- Keep production deploys locked to the guarded deploy scripts in the canonical website root ({website_root or 'not resolved'}).",
        "- Treat Supabase webhook-confirmed subscription and token tables as the revenue source of truth when direct Stripe local API access is unavailable.",
        "- Ping founder on new paid token orders, active subscription increases, service failure transitions, and route recoveries.",
        "- External outreach remains approval-gated; generate drafts and recipient cohorts, then require founder approval before sending.",
    ]
    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(str(line) for line in lines) + "\n"


def write_analytics_report(paths: BridgePaths, report: dict[str, Any], manifest: Mapping[str, Any] | None = None) -> dict[str, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = paths.reports
    json_path = report_dir / f"operator-analytics-{stamp}.json"
    md_path = report_dir / f"operator-analytics-{stamp}.md"
    latest_json = report_dir / "operator-analytics-latest.json"
    latest_md = report_dir / "operator-analytics-latest.md"
    write_json(json_path, report)
    write_json(latest_json, report)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_analytics_markdown(report, manifest)
    md_path.write_text(md, encoding="utf-8", newline="\n")
    latest_md.write_text(md, encoding="utf-8", newline="\n")
    return {"json": str(json_path), "markdown": str(md_path), "latestJson": str(latest_json), "latestMarkdown": str(latest_md)}


def read_previous_autopilot(paths: BridgePaths) -> dict[str, Any] | None:
    path = paths.status / "autopilot-latest.json"
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception:
        return None


def status_failure_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for section in ("services", "localRoots"):
        for item in snapshot.get(section, []):
            if not isinstance(item, dict):
                continue
            if item.get("status") not in {"ok", "optional_failed"}:
                failures.append(item)

    artifact = snapshot.get("websiteArtifact")
    if isinstance(artifact, dict) and artifact.get("status") not in {"ok", None}:
        failures.append(
            {
                "id": "website-artifact",
                "label": "Website artifact guard",
                "status": artifact.get("status"),
                "findings": artifact.get("findings", []),
            }
        )
    return failures


def current_platform() -> str:
    return "windows" if os.name == "nt" else "posix"


def recovery_commands_for(manifest: Mapping[str, Any], target: str) -> list[dict[str, Any]]:
    platform_name = current_platform()
    return [
        command
        for command in (manifest.get("recoveryCommands", {}) or {}).get(target, [])
        if platform_name in command.get("platforms", ["windows", "posix"])
    ]


def recoverable_failures(manifest: dict[str, Any], failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A route that was not probed (missing credential) is not evidence of an
    # outage, so it never triggers a restart.
    return [
        failure
        for failure in failures
        if failure.get("status") != "not_configured"
        and recovery_commands_for(manifest, str(failure.get("recoveryTarget") or failure.get("id")))
    ]


def build_operator_notification(
    status_delta: dict[str, Any],
    analytics_delta: dict[str, Any],
    report: dict[str, Any],
    report_path: str | None = None,
) -> str | None:
    lines: list[str] = []
    labels = analytics_delta.get("labels") or {}
    if status_delta.get("newFailures"):
        lines.append("Route/service failures detected:")
        lines.extend(f"- {item['label']} ({item['id']}) is {item['status']}" for item in status_delta["newFailures"][:6])
    if status_delta.get("recoveries"):
        lines.append("Recovered routes/services:")
        lines.extend(f"- {item['label']} ({item['id']}) recovered" for item in status_delta["recoveries"][:6])
    if analytics_delta.get("newActiveSubscriptions"):
        lines.append(f"New {labels.get('newActiveSubscriptions', 'active/trialing subscriptions')}: +{analytics_delta['newActiveSubscriptions']}")
    if analytics_delta.get("newPaidTokenOrders"):
        usd = f" (${analytics_delta.get('newPaidTokenUsd', 0)})" if "newPaidTokenUsd" in labels else ""
        lines.append(f"New {labels.get('newPaidTokenOrders', 'paid/proven token orders')}: +{analytics_delta['newPaidTokenOrders']}{usd}")
    if analytics_delta.get("newUsers"):
        lines.append(f"New {labels.get('newUsers', 'auth users')}: +{analytics_delta['newUsers']}")
    if analytics_delta.get("newTelemetryEvents"):
        lines.append(f"New {labels.get('newTelemetryEvents', 'site telemetry events')}: +{analytics_delta['newTelemetryEvents']}")
    if not lines:
        return None
    telemetry = report.get("telemetry", {})
    top_path = (telemetry.get("topPaths") or [{}])[0].get("key", "none") if isinstance(telemetry.get("topPaths"), list) else "none"
    lines.extend(["", f"Top path: {top_path}", f"Generated: {report.get('generatedAt')}"])
    if report_path:
        lines.append(f"Report: {report_path}")
    content = "\n".join(lines)
    return content[:1800]


def post_discord_message(content: str, manifest: dict[str, Any], timeout: int) -> dict[str, Any]:
    webhook = env_value("DISCORD_WEBHOOK_URL", manifest, allow_netlify=False)
    payload = {
        "username": "Q_agent",
        "content": content,
        "allowed_mentions": {"parse": []},
    }
    operator_id = env_value("DISCORD_OPERATOR_USER_ID", manifest, allow_netlify=False)
    if not operator_id:
        founder_ids = env_value("DISCORD_FOUNDER_USER_IDS", manifest, allow_netlify=False)
        operator_id = founder_ids.split(",", 1)[0].strip() if founder_ids else None
    if operator_id:
        payload["content"] = f"<@{operator_id}> {payload['content']}"
        payload["allowed_mentions"] = {"users": [operator_id]}
    body = json.dumps(payload).encode("utf-8")
    if webhook:
        url = webhook if "?" in webhook else f"{webhook}?wait=true"
        result = http_json(url, method="POST", headers={"Content-Type": "application/json"}, body=body, timeout=timeout)
        return {"status": "sent" if result["ok"] else "failed", "route": "webhook", "httpStatus": result.get("status")}

    bot_token = env_value("DISCORD_BOT_TOKEN", manifest, allow_netlify=False)
    channel_id = env_value("DISCORD_DEFAULT_CHANNEL_ID", manifest, allow_netlify=False)
    if bot_token and channel_id:
        result = http_json(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bot {bot_token}"},
            body=body,
            timeout=timeout,
        )
        return {"status": "sent" if result["ok"] else "failed", "route": "bot", "httpStatus": result.get("status")}
    return {"status": "skipped", "reason": "Discord webhook or bot route is not configured for the local operator."}


def run_recovery_for_failures(manifest: dict[str, Any], failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    seen: set[str] = set()
    for failure in failures:
        service_id = failure.get("id")
        recovery_id = str(failure.get("recoveryTarget") or service_id)
        for command in recovery_commands_for(manifest, recovery_id):
            key = sha256_canonical({
                "id": command.get("id"),
                "cwd": command.get("cwd"),
                "argv": command.get("argv"),
            })
            if key in seen:
                continue
            seen.add(key)
            result = run_command(command, manifest)
            result["serviceId"] = service_id
            result["recoveryTarget"] = recovery_id
            results.append(result)
    return results


def verify_recovery_targets(
    manifest: dict[str, Any],
    target_ids: set[str],
    timeout: int,
    wait_seconds: int,
) -> dict[str, Any]:
    if not target_ids:
        return {
            "status": "skipped",
            "reason": "No recoverable targets were provided.",
            "targetIds": [],
            "attempts": [],
            "remainingFailures": [],
        }

    services = [service for service in manifest_services(manifest) if service.get("id") in target_ids]
    attempts: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(0, wait_seconds)
    remaining: list[dict[str, Any]] = []

    while True:
        remaining = []
        probe_results = []
        for service in services:
            result = http_probe(service, timeout)
            probe_results.append(
                {
                    "id": result.get("id"),
                    "label": result.get("label"),
                    "status": result.get("status"),
                    "httpStatus": result.get("httpStatus"),
                    "elapsedMs": result.get("elapsedMs"),
                }
            )
            if result.get("status") not in {"ok", "optional_failed"}:
                remaining.append(result)

        attempts.append(
            {
                "checkedAt": utc_now(),
                "probeResults": probe_results,
                "remainingIds": [str(item.get("id")) for item in remaining],
            }
        )

        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(min(5, max(0.1, deadline - time.monotonic())))

    return {
        "status": "ok" if not remaining else "warning",
        "targetIds": sorted(target_ids),
        "waitSeconds": wait_seconds,
        "attempts": attempts,
        "remainingFailures": [
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "status": item.get("status"),
                "httpStatus": item.get("httpStatus"),
                "error": item.get("error"),
            }
            for item in remaining
        ],
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


def expand_command(command: Mapping[str, Any], manifest: Mapping[str, Any]) -> tuple[Path, list[str]]:
    cwd = resolve_manifest_path(str(command.get("cwd", ".")), manifest)
    argv = [expand_manifest_value(str(value), manifest) for value in command.get("argv", [])]
    return cwd, argv


def run_command(command: dict[str, Any], manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        cwd, argv = expand_command(command, manifest or {})
    except ValueError as error:
        return {"id": command.get("id"), "status": "failed", "cwd": command.get("cwd"), "argv": command.get("argv"), "error": str(error)}
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
    if not cwd.is_dir():
        return {"id": command.get("id"), "status": "failed", "cwd": str(cwd), "argv": argv, "error": "cwd does not exist"}
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


# ---------------------------------------------------------------------------
# Tasks, approvals and dispatch
# ---------------------------------------------------------------------------


def build_task_id(title: str) -> str:
    slug = "".join(char.lower() if char.isascii() and char.isalnum() else "-" for char in title).strip("-")
    slug = "-".join(part for part in slug.split("-") if part)[:48] or "task"
    digest = hashlib.sha256(f"{title}:{time.time_ns()}".encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def action_needs_approval(title: str, objective: str) -> bool:
    haystack = f"{title} {objective}".lower()
    return any(word in haystack for word in SERIOUS_ACTION_WORDS)


def create_task(args: argparse.Namespace, manifest: dict[str, Any], paths: BridgePaths) -> dict[str, Any]:
    task_id = args.id or build_task_id(args.title)
    if not TASK_ID_PATTERN.match(task_id):
        raise ValueError(f"task id {task_id!r} must match {TASK_ID_PATTERN.pattern}")
    serious = action_needs_approval(args.title, args.objective)
    task = {
        "schemaVersion": 2,
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
        "approval": {
            "required": bool(args.approval_required or serious),
            "reason": "serious action or production-adjacent task" if serious else "operator selected",
            "grants": [],
        },
        "governance": manifest.get("governance", {}),
    }
    task["payloadSha256"] = task_payload_sha256(task)
    out_path = paths.inbox / f"{task_id}.json"
    write_json(out_path, task)
    return {"status": "ok", "taskPath": str(out_path), "task": task}


def validate_task(task: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = ["id", "title", "objective", "targetRoot", "allowedWritePaths", "evaluator"]
    for key in required:
        if key not in task:
            errors.append(f"missing required field: {key}")
    if "id" in task and not TASK_ID_PATTERN.match(str(task.get("id"))):
        errors.append(f"id must match {TASK_ID_PATTERN.pattern}")
    try:
        target_root: Path | None = normalize_path(str(task.get("targetRoot", "")))
    except ValueError as error:
        target_root = None
        errors.append(f"targetRoot cannot be resolved: {error}")
    if target_root is not None and not target_root.exists():
        errors.append(f"targetRoot does not exist: {target_root}")

    roots, root_errors = resolve_local_roots(manifest)
    allowed_roots = list(roots.values())
    if target_root is not None and (allowed_roots or root_errors) and not any(
        target_root == root or target_root.is_relative_to(root) for root in allowed_roots
    ):
        errors.append(f"targetRoot is outside configured local roots: {target_root}")

    website = manifest.get("website", {})
    forbidden_roots = []
    for value in website.get("forbiddenDeployRoots", []):
        try:
            forbidden_roots.append(resolve_manifest_path(str(value), manifest))
        except ValueError:
            continue
    for write_path_raw in task.get("allowedWritePaths", []):
        try:
            write_path = normalize_path(str(write_path_raw))
        except ValueError as error:
            errors.append(f"allowedWritePath cannot be resolved: {error}")
            continue
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
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0 or timeout > 3600:
        errors.append("evaluator.timeoutSec must be an integer from 1 to 3600")
    command = evaluator.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        errors.append("evaluator.command must be a non-empty string array")

    return errors


def task_approval_state(
    task: dict[str, Any],
    *,
    now: datetime | None = None,
    check_expiry: bool = True,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return evaluate_task_approvals(
        task,
        serious=action_needs_approval(str(task.get("title", "")), str(task.get("objective", ""))),
        env=env,
        now=now,
        check_expiry=check_expiry,
    )


def approval_is_complete(task: dict[str, Any]) -> bool:
    return bool(task_approval_state(task)["complete"])


def safe_branch_suffix(task_id: str) -> str:
    safe = "".join(char.lower() if char.isascii() and char.isalnum() else "-" for char in task_id).strip("-")[:48].strip("-")
    return safe or "task"


def path_within(candidate: str, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path = normalize_path(candidate)
    except ValueError:
        return False
    return path == root or path.is_relative_to(root)


def dispatch_routes(task: Mapping[str, Any], manifest: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Routes the packet describes. Only routes something actually serves are listed."""
    endpoints = manifest.get("endpoints") or {}
    roots, _errors = resolve_local_roots(manifest, env)
    routes: dict[str, Any] = {
        "immaculate": {"type": "asi-dispatch-intake", "endpoint": dispatch_url(env)},
        "q": {
            "type": "q-gateway",
            "health": expand_env_template(str(endpoints.get("qGatewayHealth", "${IMMACULATE_Q_GATEWAY_URL:-http://127.0.0.1:8897}/health")), env),
        },
    }
    target_root = str(task.get("targetRoot", ""))
    if path_within(target_root, roots.get("website")):
        routes["laasWebsite"] = {
            "type": "protected-react-shell",
            "root": str(roots["website"]),
            "publicUrl": expand_env_template(str((manifest.get("website") or {}).get("publicUrl", "")), env),
        }
    if path_within(target_root, roots.get("openjaws")):
        routes["jaws"] = {
            "type": "openjaws-guarded-workstation",
            "root": str(roots["openjaws"]),
            "preflight": ["bun run serious:approval:ready", "bun run orchestration:guardrails"],
        }
    return routes


def build_dispatch_packet(
    task: dict[str, Any],
    manifest: dict[str, Any],
    *,
    now: datetime | None = None,
    nonce: str | None = None,
    issuer: str | None = None,
    ttl: timedelta = DEFAULT_PACKET_TTL,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Unsigned packet body in exactly Immaculate's schema (sign before delivery)."""
    branch_prefix = manifest.get("governance", {}).get("branchPrefix", "agent/evolve/")
    task_id = str(task["id"])
    evaluator = task.get("evaluator") or {}
    return build_packet_body(
        task_id=task_id,
        task_payload_sha256=task_payload_sha256(task),
        title=str(task.get("title", "")),
        objective=str(task.get("objective", "")),
        lane=str(task.get("lane") or manifest.get("governance", {}).get("defaultLane", "private")),
        target_root=str(task.get("targetRoot", "")),
        allowed_write_paths=[str(value) for value in task.get("allowedWritePaths", [])],
        branch=f"{branch_prefix}{safe_branch_suffix(task_id)}",
        evaluator_command=[str(value) for value in evaluator.get("command", [])],
        evaluator_timeout_sec=int(evaluator.get("timeoutSec", 0)),
        routes=dispatch_routes(task, manifest, env),
        issuer=issuer or default_issuer(env),
        nonce=nonce or secrets.token_hex(16),
        now=now or datetime.now(timezone.utc),
        ttl=ttl,
    )


def move_task(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        destination = destination_dir / f"{source.stem}-{int(time.time())}{source.suffix}"
    source.replace(destination)
    return destination


def write_receipt(paths: BridgePaths, name: str, receipt: dict[str, Any]) -> dict[str, Any]:
    sealed = seal_receipt(receipt, signing_key())
    write_json(paths.receipts / name, sealed)
    return sealed


def receipt_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def pending_approval_message(task: dict[str, Any], missing: list[str]) -> str:
    title = redact_text(str(task.get("title", "")))[:200]
    return "\n".join(
        [
            f"ASI-Evolve task held for approval: {task.get('id')}",
            f"Title: {title}",
            f"payloadSha256: {task.get('payloadSha256')}",
            f"Missing signed approvals: {', '.join(missing) or 'none'}",
            "Each approver mints a token with: python -m arobi_integrations approval-token "
            f"--role <founder|policy-governor> --task {task.get('id')} --payload-sha256 {task.get('payloadSha256')} --approver <name>",
            f"Then: python -m arobi_integrations approve --task {task.get('id')} --founder-approval <token> --governor-approval <token>",
        ]
    )[:1800]


def process_tasks(
    manifest: dict[str, Any],
    paths: BridgePaths,
    limit: int,
    *,
    notify: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Validate inbox tasks and re-evaluate held tasks.

    New tasks come from ``tasks/inbox``; tasks already held in
    ``tasks/pending-approval`` are re-scanned so a task moves on once its signed
    approvals verify. Approved tasks go to ``tasks/ready`` for ``deliver``.
    """
    processed: list[dict[str, Any]] = []
    candidates = [(path, "inbox") for path in sorted(paths.inbox.glob("*.json"))]
    candidates += [(path, "pending") for path in sorted(paths.pending.glob("*.json"))]
    for task_path, origin in candidates[:limit]:
        try:
            task = load_json(task_path)
        except (ValueError, OSError) as error:
            receipt = write_receipt(paths, f"{task_path.stem}-rejected.json", {
                "status": "rejected",
                "taskPath": str(task_path),
                "checkedAt": utc_now(),
                "errors": [f"task file is not a readable JSON object: {error}"],
            })
            receipt["movedTo"] = str(move_task(task_path, paths.rejected))
            processed.append(receipt)
            continue
        errors = payload_integrity_errors(task) + validate_task(task, manifest)
        if errors:
            receipt = write_receipt(paths, f"{task_path.stem}-rejected.json", {
                "status": "rejected",
                "taskPath": str(task_path),
                "taskId": task.get("id"),
                "checkedAt": utc_now(),
                "errors": errors,
            })
            receipt["movedTo"] = str(move_task(task_path, paths.rejected))
            processed.append(receipt)
            continue

        approval = task_approval_state(task)
        if not approval["complete"]:
            if origin == "pending":
                processed.append({
                    "status": "pending_approval",
                    "taskId": task["id"],
                    "checkedAt": utc_now(),
                    "unchanged": True,
                    "missingRoles": approval["missingRoles"],
                    "approvalErrors": approval["errors"],
                })
                continue
            notification = (
                post_discord_message(pending_approval_message(task, approval["missingRoles"]), manifest, timeout)
                if notify
                else {"status": "skipped", "reason": "--notify not set"}
            )
            receipt = write_receipt(paths, f"{task['id']}-pending-approval.json", {
                "status": "pending_approval",
                "taskId": task["id"],
                "payloadSha256": task["payloadSha256"],
                "checkedAt": utc_now(),
                "summary": "Task is validated but held until signed founder and policy-governor approvals verify.",
                "missingRoles": approval["missingRoles"],
                "approvalErrors": approval["errors"],
                "founderNotification": notification,
            })
            receipt["movedTo"] = str(move_task(task_path, paths.pending))
            processed.append(receipt)
            continue

        receipt = write_receipt(paths, f"{task['id']}-ready.json", {
            "status": "ready_for_delivery",
            "taskId": task["id"],
            "payloadSha256": task["payloadSha256"],
            "checkedAt": utc_now(),
            "summary": "Task is validated and approved. `deliver` signs it and posts it to Immaculate's ASI intake; production deploy and external mutation remain blocked.",
            "approval": {"required": approval["required"], "grants": approval["grants"]},
        })
        receipt["movedTo"] = str(move_task(task_path, paths.ready))
        processed.append(receipt)

    return {
        "status": "ok",
        "checkedAt": utc_now(),
        "processedCount": len(processed),
        "items": processed,
    }


def sign_ready_tasks(
    manifest: dict[str, Any],
    paths: BridgePaths,
    limit: int,
    *,
    now: datetime,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    key = signing_key(env)
    ready = sorted(paths.ready.glob("*.json"))
    if key is None:
        return {
            "status": "not_configured",
            "missing": ["ASI_DISPATCH_HMAC_SECRET"],
            "reason": "Packets cannot be signed, so ready tasks stay in tasks/ready.",
            "waitingTasks": len(ready),
            "items": [],
        }
    items = []
    for task_path in ready[:limit]:
        task = load_json(task_path)
        errors = payload_integrity_errors(task) + validate_task(task, manifest)
        approval = task_approval_state(task, check_expiry=False, env=env)
        if errors or not approval["complete"]:
            destination = paths.rejected if errors else paths.pending
            receipt = write_receipt(paths, f"{task_path.stem}-sign-refused-{receipt_stamp()}.json", {
                "status": "rejected" if errors else "pending_approval",
                "taskId": task.get("id"),
                "checkedAt": utc_now(),
                "summary": "Task failed re-verification at signing time; no packet was signed.",
                "errors": errors + approval["errors"],
                "missingRoles": approval["missingRoles"],
            })
            receipt["movedTo"] = str(move_task(task_path, destination))
            items.append(receipt)
            continue
        packet = sign_dispatch_packet(build_dispatch_packet(task, manifest, now=now, env=env), key)
        packet_path = paths.dispatch_outbox / f"{task['id']}.json"
        if packet_path.exists():
            packet_path = paths.dispatch_outbox / f"{task['id']}-{packet['nonce'][:8]}.json"
        write_json(packet_path, packet)
        receipt = write_receipt(paths, f"{task['id']}-signed-{receipt_stamp()}.json", {
            "status": "signed_for_delivery",
            "taskId": task["id"],
            "checkedAt": utc_now(),
            "packetPath": str(packet_path),
            "packetSha256": packet["packetSha256"],
            "nonce": packet["nonce"],
            "issuer": packet["issuer"],
            "expiresAt": packet["expiresAt"],
            "keyId": packet["signature"]["keyId"],
        })
        receipt["movedTo"] = str(move_task(task_path, paths.processed))
        items.append(receipt)
    return {"status": "ok", "keyId": key.key_id, "signedCount": sum(1 for item in items if item["status"] == "signed_for_delivery"), "items": items}


def send_outbox_packets(
    paths: BridgePaths,
    limit: int,
    *,
    timeout: int,
    now: datetime,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    url = dispatch_url(env)
    api_key = immaculate_api_key(env)
    queued = sorted(paths.dispatch_outbox.glob("*.json"))
    if api_key is None:
        return {
            "status": "not_configured",
            "missing": ["IMMACULATE_API_KEY"],
            "endpoint": url,
            "reason": "Signed packets stay in dispatch/outbox until the Immaculate API key is configured.",
            "queuedPackets": len(queued),
            "items": [],
        }
    items = []
    for packet_path in queued[:limit]:
        packet = load_json(packet_path)
        task_id = str(packet.get("taskId", packet_path.stem))
        base = {
            "taskId": task_id,
            "packetSha256": packet.get("packetSha256"),
            "nonce": packet.get("nonce"),
            "endpoint": url,
            "attemptedAt": utc_now(),
        }
        if packet_expired(packet, now):
            receipt = write_receipt(paths, f"{task_id}-delivery-{receipt_stamp()}.json", {
                **base,
                "status": "expired_undelivered",
                "rejectedBy": "asi-evolve",
                "summary": "The packet expired before Immaculate returned an intake receipt; it was not delivered. Re-enqueue the task to dispatch it again.",
            })
            receipt["movedTo"] = str(move_task(packet_path, paths.dispatch_rejected))
            items.append(receipt)
            continue
        response = post_dispatch_packet(url, api_key, packet, timeout=timeout, user_agent=bridge_user_agent())
        outcome = classify_delivery(packet, response)
        payload = response.get("payload") if isinstance(response.get("payload"), dict) else None
        receipt_body = {
            **base,
            "status": outcome["outcome"],
            "httpStatus": response.get("httpStatus"),
            **{key: value for key, value in outcome.items() if key != "outcome"},
        }
        if payload and isinstance(payload.get("receipt"), dict):
            receipt_body["immaculateReceipt"] = payload["receipt"]
        receipt = write_receipt(paths, f"{task_id}-delivery-{receipt_stamp()}.json", receipt_body)
        if outcome["outcome"] == "delivered":
            receipt["movedTo"] = str(move_task(packet_path, paths.dispatch_delivered))
        elif outcome["outcome"] == "rejected":
            receipt["movedTo"] = str(move_task(packet_path, paths.dispatch_rejected))
        items.append(receipt)
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"status": "ok", "endpoint": url, "counts": counts, "items": items}


def deliver_dispatches(
    manifest: dict[str, Any],
    paths: BridgePaths,
    *,
    limit: int = 10,
    timeout: int = 15,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Sign approved tasks and post signed packets to Immaculate.

    ``status`` is ``ok`` only when every attempted packet was delivered;
    ``not_configured`` when the signing secret or the Immaculate API key is
    missing (nothing is marked delivered); ``warning`` otherwise.
    """
    current = now or datetime.now(timezone.utc)
    signing = sign_ready_tasks(manifest, paths, limit, now=current, env=env)
    sending = send_outbox_packets(paths, limit, timeout=timeout, now=current, env=env)
    not_configured = [step for step in (signing, sending) if step["status"] == "not_configured"]
    problems = [
        item
        for item in [*signing.get("items", []), *sending.get("items", [])]
        if item.get("status") not in {"signed_for_delivery", "delivered"}
    ]
    status = "not_configured" if not_configured else ("warning" if problems else "ok")
    return {
        "schemaVersion": 1,
        "status": status,
        "checkedAt": utc_now(),
        "sign": signing,
        "send": sending,
    }


def find_task_file(paths: BridgePaths, task_id: str) -> tuple[Path | None, str | None]:
    for label, directory in (("pending", paths.pending), ("inbox", paths.inbox), ("ready", paths.ready), ("processed", paths.processed)):
        candidate = directory / f"{task_id}.json"
        if candidate.exists():
            return candidate, label
    return None, None


def approve_task(
    paths: BridgePaths,
    task_id: str,
    tokens: dict[str, str],
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify signed approval tokens and record them (and who approved) on the task."""
    current = now or datetime.now(timezone.utc)
    task_path, location = find_task_file(paths, task_id)
    if task_path is None:
        return {"status": "error", "taskId": task_id, "errors": [f"task {task_id} is not in inbox or pending-approval"]}
    if location in {"ready", "processed"}:
        return {"status": "error", "taskId": task_id, "errors": [f"task {task_id} is already approved ({location})"]}
    if not tokens:
        return {"status": "error", "taskId": task_id, "errors": ["no approval token was provided"]}
    task = load_json(task_path)
    integrity = payload_integrity_errors(task)
    if integrity:
        return {"status": "error", "taskId": task_id, "errors": integrity}
    payload_hash = task["payloadSha256"]
    new_grants: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for role, token in tokens.items():
        claims, token_errors = verify_approval_token(
            token,
            role=role,
            task_id=task_id,
            payload_sha256=payload_hash,
            secret=approval_secret(role, env),
            now=current,
        )
        if token_errors or claims is None:
            errors.extend(token_errors)
            continue
        new_grants[role] = {
            "role": role,
            "approver": claims["approver"],
            "issuedAt": claims["issuedAt"],
            "expiresAt": claims["expiresAt"],
            "token": token.strip(),
            "verifiedAt": utc_now(),
        }
    if errors:
        return {"status": "error", "taskId": task_id, "errors": errors}
    approval = dict(task.get("approval") or {})
    grants = [grant for grant in approval.get("grants", []) if isinstance(grant, dict) and grant.get("role") not in new_grants]
    grants.extend(new_grants[role] for role in REQUIRED_ROLES if role in new_grants)
    approvers = [str(grant.get("approver", "")).strip().lower() for grant in grants]
    if len(approvers) != len(set(approvers)):
        return {"status": "error", "taskId": task_id, "errors": ["founder and policy-governor approvals must come from different approvers"]}
    approval["grants"] = grants
    updated = {**task, "approval": approval}
    write_json(task_path, updated)
    state = evaluate_task_approvals(
        updated,
        serious=action_needs_approval(str(updated.get("title", "")), str(updated.get("objective", ""))),
        env=env,
        now=current,
    )
    receipt = write_receipt(paths, f"{task_id}-approval-{receipt_stamp()}.json", {
        "status": "approval_recorded",
        "taskId": task_id,
        "payloadSha256": payload_hash,
        "recordedAt": utc_now(),
        "recorded": [
            {"role": grant["role"], "approver": grant["approver"], "issuedAt": grant["issuedAt"], "expiresAt": grant["expiresAt"],
             "tokenSha256": hashlib.sha256(grant["token"].encode("utf-8")).hexdigest()}
            for grant in new_grants.values()
        ],
        "complete": state["complete"],
        "missingRoles": state["missingRoles"],
        "next": "Run `process` to move the task to tasks/ready." if state["complete"] else "Waiting for the remaining signed approval.",
    })
    return {"status": "ok", "taskId": task_id, "taskPath": str(task_path), "complete": state["complete"], "receipt": receipt}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


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
                results.append(run_command(command, manifest))
                continue
            try:
                cwd, argv = expand_command(command, manifest)
            except ValueError as error:
                results.append({"id": command.get("id"), "group": group, "status": "failed", "error": str(error), "execute": False})
                continue
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


def command_analytics(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    report = collect_live_analytics(manifest, args.since_days, args.timeout, allow_direct=args.direct_supabase_fallback or None)
    report_paths = write_analytics_report(paths, report, manifest) if args.write_report else {}
    output = {
        "status": "ok" if not report.get("warnings") else "warning",
        "checkedAt": utc_now(),
        "source": report.get("source"),
        "reportPaths": report_paths,
        "summary": {
            "business": report.get("business", {}),
            "telemetry": {
                key: value
                for key, value in (report.get("telemetry") or {}).items()
                if key in {"source", "sites", "totalEvents", "totalEvents30d", "anonymousSessions", "bySite", "topPaths", "topTargets", "byCountry"}
            },
            "stripe": report.get("stripe", {}),
            "warnings": report.get("warnings", []),
        },
    }
    write_json(paths.status / "analytics-latest.json", output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def command_autopilot(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    previous = read_previous_autopilot(paths)
    status_snapshot = build_status(manifest, args.timeout)
    previous_status = previous.get("statusSnapshot") if isinstance(previous, dict) else None
    pre_recovery_status_snapshot = status_snapshot
    pre_recovery_status_delta = summarize_status_delta(previous_status, pre_recovery_status_snapshot)
    current_failures = status_failure_items(pre_recovery_status_snapshot)
    recovery_targets = recoverable_failures(manifest, current_failures)
    recovery = run_recovery_for_failures(manifest, recovery_targets) if args.heal else []
    recovery_verification = {
        "status": "skipped",
        "reason": "--heal not set or no recovery commands matched current failures.",
        "targetIds": [],
        "attempts": [],
        "remainingFailures": [],
    }
    if recovery:
        target_ids = {str(failure.get("id")) for failure in recovery_targets if failure.get("id")}
        recovery_verification = verify_recovery_targets(manifest, target_ids, args.timeout, args.post_heal_wait)
        status_snapshot = build_status(manifest, args.timeout)
    status_delta = summarize_status_delta(previous_status, status_snapshot)

    analytics_report = collect_live_analytics(manifest, args.since_days, args.timeout, allow_direct=args.direct_supabase_fallback or None)
    report_paths = write_analytics_report(paths, analytics_report, manifest) if args.write_report else {}
    previous_analytics = previous.get("analyticsReport") if isinstance(previous, dict) else None
    analytics_delta = summarize_analytics_delta(previous_analytics, analytics_report)
    notification_status_delta = {
        **status_delta,
        "newFailures": pre_recovery_status_delta.get("newFailures", []) or status_delta.get("newFailures", []),
        "recoveries": [
            *pre_recovery_status_delta.get("recoveries", []),
            *status_delta.get("recoveries", []),
        ],
    }
    notification_message = build_operator_notification(
        notification_status_delta,
        analytics_delta,
        analytics_report,
        str(paths.reports / "operator-analytics-latest.md") if report_paths else None,
    )
    notification = (
        post_discord_message(notification_message, manifest, args.timeout)
        if args.notify and notification_message
        else {"status": "skipped", "reason": "No notify-worthy delta or --notify not set."}
    )
    dispatch: dict[str, Any] = {"status": "skipped", "reason": "--deliver not set."}
    if args.deliver:
        dispatch = {
            "process": process_tasks(manifest, paths, args.limit, notify=args.notify, timeout=args.timeout),
            **deliver_dispatches(manifest, paths, limit=args.limit, timeout=max(args.timeout, 15)),
        }
        write_json(paths.status / "deliver-latest.json", dispatch)
    output = {
        "schemaVersion": 1,
        "status": "ok" if status_snapshot.get("failureCount", 0) == 0 else "warning",
        "checkedAt": utc_now(),
        "statusSnapshot": status_snapshot,
        "analyticsReport": analytics_report,
        "statusDelta": status_delta,
        "preRecoveryStatusDelta": pre_recovery_status_delta,
        "analyticsDelta": analytics_delta,
        "reportPaths": report_paths,
        "recovery": recovery,
        "recoveryVerification": recovery_verification,
        "notification": notification,
        "dispatch": dispatch,
        "policy": {
            "externalOutreachWithoutApproval": False,
            "productionDeployWithoutApproval": False,
            "allowedAutonomousActions": [
                "read-only analytics",
                "route health checks",
                "local safe recovery commands",
                "founder-only internal notification",
                "signed delivery of approved tasks to Immaculate's governed ASI intake",
            ],
        },
    }
    write_json(paths.status / "autopilot-latest.json", output)
    print(json.dumps({
        "status": output["status"],
        "checkedAt": output["checkedAt"],
        "failureCount": status_snapshot.get("failureCount", 0),
        "statusDelta": status_delta,
        "preRecoveryFailureCount": pre_recovery_status_snapshot.get("failureCount", 0),
        "analyticsSource": analytics_report.get("source"),
        "analyticsDelta": analytics_delta,
        "reportPaths": report_paths,
        "recoveryCount": len(recovery),
        "recoveryVerification": {
            "status": recovery_verification.get("status"),
            "targetIds": recovery_verification.get("targetIds", []),
            "remainingFailures": recovery_verification.get("remainingFailures", []),
        },
        "notification": notification,
        "dispatch": {
            "status": dispatch.get("status"),
            "sign": {key: value for key, value in (dispatch.get("sign") or {}).items() if key != "items"},
            "send": {key: value for key, value in (dispatch.get("send") or {}).items() if key != "items"},
        },
        "warnings": analytics_report.get("warnings", []),
    }, indent=2, sort_keys=True))
    return 0


def command_enqueue(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    try:
        result = create_task(args, manifest, paths)
    except ValueError as error:
        print(json.dumps({"status": "error", "errors": [str(error)]}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_process(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    report = process_tasks(manifest, paths, args.limit, notify=args.notify, timeout=args.timeout)
    write_json(paths.status / "process-latest.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def command_deliver(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    report = deliver_dispatches(manifest, paths, limit=args.limit, timeout=args.timeout)
    write_json(paths.status / "deliver-latest.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return {"ok": 0, "warning": 1}.get(report["status"], 2)


def command_approve(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    tokens = {}
    if args.founder_approval:
        tokens[ROLE_FOUNDER] = args.founder_approval
    if args.governor_approval:
        tokens[ROLE_GOVERNOR] = args.governor_approval
    result = approve_task(paths, args.task, tokens)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


def command_approval_token(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    payload_hash = args.payload_sha256
    if not payload_hash:
        task_path, _location = find_task_file(paths, args.task)
        if task_path is None:
            print(json.dumps({"status": "error", "errors": [f"task {args.task} not found; pass --payload-sha256"]}, indent=2))
            return 1
        payload_hash = load_json(task_path).get("payloadSha256")
    secret = approval_secret(args.role)
    if not secret:
        print(json.dumps({"status": "not_configured", "missing": [ROLE_SECRET_ENV[args.role]]}, indent=2))
        return 2
    token = mint_approval_token(
        role=args.role,
        task_id=args.task,
        payload_sha256=str(payload_hash),
        approver=args.approver,
        secret=secret,
        ttl=timedelta(hours=args.ttl_hours),
    )
    print(token)
    return 0


def command_verify_receipt(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    key = signing_key()
    secrets_by_key = {key.key_id: key.secret} if key else {}
    receipt = load_json(normalize_path(args.path))
    result = verify_receipt(receipt, secrets_by_key)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["hashVerified"] and result["signatureVerified"] else 1


def command_seed(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    roots, errors = resolve_local_roots(manifest)
    missing = [name for name in ("website", "immaculate", "openjaws") if name not in roots]
    if missing:
        print(json.dumps({"status": "error", "errors": [errors.get(name, f"local root {name} is not configured") for name in missing]}, indent=2))
        return 1
    seeds = [
        {
            "kind": "website_guard",
            "title": "Protect Aura Genesis LaaS shell deploy route",
            "objective": "Continuously verify the live Aura Genesis site serves the protected React/LaaS/Arobi shell, route family, public APIs, and replay surface without deploying from legacy roots.",
            "target_root": str(roots["website"]),
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
            "target_root": str(roots["immaculate"]),
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
            "target_root": str(roots["openjaws"]),
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

    analytics = subparsers.add_parser("analytics", help="Build a private operator analytics report")
    analytics.add_argument("--since-days", type=int, default=7)
    analytics.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    analytics.add_argument("--write-report", action="store_true")
    analytics.add_argument("--direct-supabase-fallback", action="store_true", help="Allow direct Supabase/Stripe reads when the operator analytics API is unavailable")

    autopilot = subparsers.add_parser("autopilot", help="Run guarded autonomous monitor, analytics, recovery, founder notification and dispatch")
    autopilot.add_argument("--since-days", type=int, default=7)
    autopilot.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    autopilot.add_argument("--write-report", action="store_true")
    autopilot.add_argument("--notify", action="store_true")
    autopilot.add_argument("--heal", action="store_true")
    autopilot.add_argument("--post-heal-wait", type=int, default=150)
    autopilot.add_argument("--deliver", action="store_true", help="Re-scan tasks, then sign and deliver approved tasks to Immaculate")
    autopilot.add_argument("--limit", type=int, default=10)
    autopilot.add_argument("--direct-supabase-fallback", action="store_true")

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

    process = subparsers.add_parser("process", help="Validate inbox tasks and re-evaluate held tasks")
    process.add_argument("--limit", type=int, default=10)
    process.add_argument("--notify", action="store_true", help="Notify the founder on Discord when a task is newly held for approval")
    process.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)

    deliver = subparsers.add_parser("deliver", help="Sign approved tasks and POST them to Immaculate /api/asi/dispatch")
    deliver.add_argument("--limit", type=int, default=10)
    deliver.add_argument("--timeout", type=int, default=15)

    approve = subparsers.add_parser("approve", help="Verify and record signed approval tokens on a held task")
    approve.add_argument("--task", required=True)
    approve.add_argument("--founder-approval", default=None)
    approve.add_argument("--governor-approval", default=None)

    token = subparsers.add_parser("approval-token", help="Mint a signed approval token (run by the approver)")
    token.add_argument("--role", required=True, choices=[ROLE_FOUNDER, ROLE_GOVERNOR])
    token.add_argument("--task", required=True)
    token.add_argument("--approver", required=True)
    token.add_argument("--payload-sha256", default=None)
    token.add_argument("--ttl-hours", type=float, default=DEFAULT_TOKEN_TTL.total_seconds() / 3600)

    verify = subparsers.add_parser("verify-receipt", help="Verify a bridge receipt's hash and HMAC signature")
    verify.add_argument("--path", required=True)

    seed = subparsers.add_parser("seed-arobi", help="Seed initial Arobi task queue")
    seed.add_argument("--process", action="store_true")
    seed.add_argument("--limit", type=int, default=10)

    return parser


COMMANDS = {
    "status": command_status,
    "doctor": command_doctor,
    "analytics": command_analytics,
    "autopilot": command_autopilot,
    "enqueue": command_enqueue,
    "process": command_process,
    "deliver": command_deliver,
    "approve": command_approve,
    "approval-token": command_approval_token,
    "verify-receipt": command_verify_receipt,
    "seed-arobi": command_seed,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    try:
        paths = bridge_paths(manifest)
    except ValueError as error:
        print(json.dumps({"status": "error", "errors": [f"stateRoot cannot be used: {error}"]}, indent=2), file=sys.stderr)
        return 2
    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2
    return handler(manifest, paths, args)
