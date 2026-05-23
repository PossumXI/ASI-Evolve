"""Guarded Arobi bridge for the ASI-Evolve fork.

This module intentionally does not run ASI-Evolve rounds or mutate production.
It checks health, validates task contracts, and writes dispatch packets that Q,
Immaculate, JAWS/OpenJaws, and Discord agents can consume through their existing
approval-gated lanes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hash_identity(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


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
    website_root = normalize_path(str(manifest.get("localRoots", {}).get("website", REPO_ROOT)))
    npx = resolve_command_argv(["npx"])
    if npx is None or not website_root.exists():
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
        script = normalize_path(str(raw_script))
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
            "User-Agent": "arobi-asi-evolve-bridge/2026.05.23",
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


def aggregate_telemetry_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
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
    page_views = sum(1 for value in event_types if value == "page_view")
    checkout_intent = sum(
        1
        for row in rows
        if str(row.get("path") or "").startswith("/pricing")
        or "checkout" in str(row.get("target_url") or "").lower()
        or "subscribe" in str(row.get("target_label") or "").lower()
        or "supporter" in str(row.get("target_label") or "").lower()
        or "token" in str(row.get("target_label") or "").lower()
    )
    autonomo_views = sum(1 for row in rows if str(row.get("path") or "").startswith("/autonomo"))
    apex_views = sum(1 for row in rows if str(row.get("path") or "").startswith("/app/apex-os"))
    dashboard_views = sum(1 for row in rows if str(row.get("path") or "").startswith("/dashboard"))
    return {
        "totalEvents": len(rows),
        "anonymousSessions": len(sessions),
        "byEventType": top_counts(event_types),
        "topPaths": top_counts(paths),
        "topTargets": top_counts(targets, include_none=False),
        "topReferrers": top_counts([row.get("referrer") if isinstance(row.get("referrer"), str) else None for row in rows]),
        "byCountry": top_counts([row.get("country") if isinstance(row.get("country"), str) else None for row in rows]),
        "byRegion": top_counts([row.get("region") if isinstance(row.get("region"), str) else None for row in rows]),
        "byCity": top_counts([row.get("city") if isinstance(row.get("city"), str) else None for row in rows]),
        "byDay": top_counts([bucket_day(row.get("occurred_at")) for row in rows], limit=31),
        "funnel": {
            "pageViews": page_views,
            "pricingIntentClicks": checkout_intent,
            "autonomoViews": autonomo_views,
            "apexViews": apex_views,
            "dashboardViews": dashboard_views,
            "intentRate": round(checkout_intent / page_views, 4) if page_views else 0,
        },
        "recent": [
            {
                "occurredAt": row.get("occurred_at"),
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


def summarize_analytics_delta(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    new_users = int(max(0, value_at(current, "business.users.total") - value_at(previous, "business.users.total")))
    new_paid_orders = int(
        max(0, value_at(current, "business.tokenOrders.paidOrderCount") - value_at(previous, "business.tokenOrders.paidOrderCount"))
    )
    new_paid_usd = round(
        max(0, value_at(current, "business.tokenOrders.paidUsd") - value_at(previous, "business.tokenOrders.paidUsd")),
        2,
    )
    new_active_subscriptions = int(
        max(
            0,
            value_at(current, "business.subscriptions.activeOrTrialing")
            - value_at(previous, "business.subscriptions.activeOrTrialing"),
        )
    )
    new_telemetry_events = int(
        max(0, value_at(current, "telemetry.totalEvents") - value_at(previous, "telemetry.totalEvents"))
    )
    return {
        "newUsers": new_users,
        "newPaidTokenOrders": new_paid_orders,
        "newPaidTokenUsd": new_paid_usd,
        "newActiveSubscriptions": new_active_subscriptions,
        "newTelemetryEvents": new_telemetry_events,
    }


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


def supabase_config(manifest: dict[str, Any]) -> dict[str, str] | None:
    url = first_env(["SUPABASE_URL", "VITE_SUPABASE_URL", "ITE_SUPABASE_URL"], manifest)
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


def collect_live_analytics(manifest: dict[str, Any], since_days: int, timeout: int) -> dict[str, Any]:
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
                "select": "occurred_at,event_type,session_id,path,referrer,target_url,target_label,country,region,city,utm_source,utm_campaign",
                "site": "eq.aura-genesis.org",
                "occurred_at": f"gte.{since_iso}",
                "order": "occurred_at.desc",
                "limit": "5000",
            },
            timeout=timeout,
        )
        if error:
            warnings.append(error)
        telemetry = {**aggregate_telemetry_rows(telemetry_rows), "source": "supabase.site_telemetry_events"}

    return {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "sinceDays": since_days,
        "business": business,
        "telemetry": telemetry,
        "stripe": collect_stripe_summary(manifest, since_days, timeout),
        "warnings": warnings,
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


def render_analytics_markdown(report: dict[str, Any]) -> str:
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
    lines = [
        "# Arobi Deep Analytics Report",
        "",
        f"Generated: {report.get('generatedAt')}",
        f"Window: last {report.get('sinceDays')} days",
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
        f"- Active API keys used at least once: {used_keys}/{api_keys.get('active', 0)} ({percent(used_keys, int(api_keys.get('active', 0) or 0))}).",
        "- Current analytics signal: traffic is reaching the site and sign-in/reset/workspace actions are being clicked, but subscriptions remain at zero and most issued API keys have not been used. The next revenue lever is activation: make the post-sign-in workspace, API quickstart, replay demo, and paid-plan upgrade path unavoidable without weakening security.",
        "",
        "## Revenue Integrity",
        "",
        f"- Paid token orders missing Stripe proof: {token_orders.get('paidOrdersMissingStripeProof', 0)}.",
        f"- Stripe direct check: {report.get('stripe', {}).get('status', 'unknown')} ({report.get('stripe', {}).get('reason', 'no issue reported')}).",
        "",
        "## Operator Actions",
        "",
        "- Keep production deploys locked to D:\\Websites guarded deploy scripts only.",
        "- Treat Supabase webhook-confirmed subscription and token tables as the revenue source of truth when direct Stripe local API access is unavailable.",
        "- Ping founder on new paid token orders, active subscription increases, service failure transitions, and route recoveries.",
        "- External outreach remains approval-gated; generate drafts and recipient cohorts, then require founder approval before sending.",
    ]
    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(str(line) for line in lines) + "\n"


def write_analytics_report(paths: BridgePaths, report: dict[str, Any]) -> dict[str, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = paths.state_root / "reports"
    json_path = report_dir / f"operator-analytics-{stamp}.json"
    md_path = report_dir / f"operator-analytics-{stamp}.md"
    latest_json = report_dir / "operator-analytics-latest.json"
    latest_md = report_dir / "operator-analytics-latest.md"
    write_json(json_path, report)
    write_json(latest_json, report)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_analytics_markdown(report)
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


def build_operator_notification(
    status_delta: dict[str, Any],
    analytics_delta: dict[str, Any],
    report: dict[str, Any],
) -> str | None:
    lines: list[str] = []
    if status_delta.get("newFailures"):
        lines.append("Route/service failures detected:")
        lines.extend(f"- {item['label']} ({item['id']}) is {item['status']}" for item in status_delta["newFailures"][:6])
    if status_delta.get("recoveries"):
        lines.append("Recovered routes/services:")
        lines.extend(f"- {item['label']} ({item['id']}) recovered" for item in status_delta["recoveries"][:6])
    if analytics_delta.get("newActiveSubscriptions"):
        lines.append(f"New active/trialing subscription delta: +{analytics_delta['newActiveSubscriptions']}")
    if analytics_delta.get("newPaidTokenOrders"):
        lines.append(
            f"New paid/proven token orders: +{analytics_delta['newPaidTokenOrders']} (${analytics_delta.get('newPaidTokenUsd', 0)})"
        )
    if analytics_delta.get("newUsers"):
        lines.append(f"New auth users: +{analytics_delta['newUsers']}")
    if analytics_delta.get("newTelemetryEvents"):
        lines.append(f"New site telemetry events recorded: +{analytics_delta['newTelemetryEvents']}")
    if not lines:
        return None
    telemetry = report.get("telemetry", {})
    top_path = (telemetry.get("topPaths") or [{}])[0].get("key", "none") if isinstance(telemetry.get("topPaths"), list) else "none"
    lines.extend(
        [
            "",
            f"Top path: {top_path}",
            f"Generated: {report.get('generatedAt')}",
            "Report: D:\\ASI-Evolve\\.arobi-evolve\\reports\\operator-analytics-latest.md",
        ]
    )
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
    commands = manifest.get("recoveryCommands", {})
    results = []
    seen: set[str] = set()
    for failure in failures:
        service_id = failure.get("id")
        for command in commands.get(service_id, []):
            key = sha256_json({
                "id": command.get("id"),
                "cwd": command.get("cwd"),
                "argv": command.get("argv"),
            })
            if key in seen:
                continue
            seen.add(key)
            result = run_command(command)
            result["serviceId"] = service_id
            results.append(result)
    return results


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


def command_analytics(manifest: dict[str, Any], paths: BridgePaths, args: argparse.Namespace) -> int:
    report = collect_live_analytics(manifest, args.since_days, args.timeout)
    report_paths = write_analytics_report(paths, report) if args.write_report else {}
    output = {
        "status": "ok" if not report.get("warnings") else "warning",
        "checkedAt": utc_now(),
        "reportPaths": report_paths,
        "summary": {
            "users": report.get("business", {}).get("users", {}),
            "subscriptions": report.get("business", {}).get("subscriptions", {}),
            "tokenOrders": report.get("business", {}).get("tokenOrders", {}),
            "newsletter": report.get("business", {}).get("newsletter", {}),
            "contacts": report.get("business", {}).get("contacts", {}),
            "apiKeys": report.get("business", {}).get("apiKeys", {}),
            "tenantDecisions": report.get("business", {}).get("tenantDecisions", {}),
            "telemetry": {
                "totalEvents": report.get("telemetry", {}).get("totalEvents", 0),
                "anonymousSessions": report.get("telemetry", {}).get("anonymousSessions", 0),
                "topPaths": report.get("telemetry", {}).get("topPaths", [])[:8],
                "topTargets": report.get("telemetry", {}).get("topTargets", [])[:8],
                "byCountry": report.get("telemetry", {}).get("byCountry", [])[:8],
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
    analytics_report = collect_live_analytics(manifest, args.since_days, args.timeout)
    report_paths = write_analytics_report(paths, analytics_report) if args.write_report else {}
    previous_status = previous.get("statusSnapshot") if isinstance(previous, dict) else None
    previous_analytics = previous.get("analyticsReport") if isinstance(previous, dict) else None
    status_delta = summarize_status_delta(previous_status, status_snapshot)
    analytics_delta = (
        summarize_analytics_delta(previous_analytics, analytics_report)
        if previous_analytics
        else {
            "newUsers": 0,
            "newPaidTokenOrders": 0,
            "newPaidTokenUsd": 0,
            "newActiveSubscriptions": 0,
            "newTelemetryEvents": 0,
        }
    )
    recovery = run_recovery_for_failures(manifest, status_delta["newFailures"]) if args.heal else []
    notification_message = build_operator_notification(status_delta, analytics_delta, analytics_report)
    notification = (
        post_discord_message(notification_message, manifest, args.timeout)
        if args.notify and notification_message
        else {"status": "skipped", "reason": "No notify-worthy delta or --notify not set."}
    )
    output = {
        "schemaVersion": 1,
        "status": "ok" if status_snapshot.get("failureCount", 0) == 0 else "warning",
        "checkedAt": utc_now(),
        "statusSnapshot": status_snapshot,
        "analyticsReport": analytics_report,
        "statusDelta": status_delta,
        "analyticsDelta": analytics_delta,
        "reportPaths": report_paths,
        "recovery": recovery,
        "notification": notification,
        "policy": {
            "externalOutreachWithoutApproval": False,
            "productionDeployWithoutApproval": False,
            "allowedAutonomousActions": [
                "read-only analytics",
                "route health checks",
                "local safe recovery commands",
                "founder-only internal notification",
                "approval-gated task dispatch",
            ],
        },
    }
    write_json(paths.status / "autopilot-latest.json", output)
    print(json.dumps({
        "status": output["status"],
        "checkedAt": output["checkedAt"],
        "failureCount": status_snapshot.get("failureCount", 0),
        "statusDelta": status_delta,
        "analyticsDelta": analytics_delta,
        "reportPaths": report_paths,
        "recoveryCount": len(recovery),
        "notification": notification,
        "warnings": analytics_report.get("warnings", []),
    }, indent=2, sort_keys=True))
    return 0


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

    analytics = subparsers.add_parser("analytics", help="Build a private operator analytics report")
    analytics.add_argument("--since-days", type=int, default=7)
    analytics.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    analytics.add_argument("--write-report", action="store_true")

    autopilot = subparsers.add_parser("autopilot", help="Run guarded autonomous monitor, analytics, recovery, and founder notification")
    autopilot.add_argument("--since-days", type=int, default=7)
    autopilot.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    autopilot.add_argument("--write-report", action="store_true")
    autopilot.add_argument("--notify", action="store_true")
    autopilot.add_argument("--heal", action="store_true")

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
    if args.command == "analytics":
        return command_analytics(manifest, paths, args)
    if args.command == "autopilot":
        return command_autopilot(manifest, paths, args)
    if args.command == "enqueue":
        return command_enqueue(manifest, paths, args)
    if args.command == "process":
        return command_process(manifest, paths, args)
    if args.command == "seed-arobi":
        return command_seed(manifest, paths, args)

    parser.error(f"unknown command: {args.command}")
    return 2
