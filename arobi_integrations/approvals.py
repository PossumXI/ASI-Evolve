"""Signed approval tokens for serious ASI-Evolve tasks.

A task that needs approval is held in ``tasks/pending-approval`` until it
carries two verifiable grants: one from the founder and one from the policy
governor. Each grant is an HMAC-SHA256 token bound to the exact task id and
task payload hash, so an approval cannot be replayed onto another task or onto
an edited task.

Token format (``aev1``)::

    aev1.<base64url(canonical JSON claims)>.<hex HMAC-SHA256 over "aev1.<claims>">

Claims: ``v``, ``role``, ``taskId``, ``payloadSha256``, ``approver``,
``issuedAt``, ``expiresAt``, ``nonce``. Each role has its own secret
(``AROBI_EVOLVE_FOUNDER_APPROVAL_SECRET`` and
``AROBI_EVOLVE_GOVERNOR_APPROVAL_SECRET``), and the two grants must name
different approvers.

HMAC is symmetric: whoever holds a role secret can mint that role's tokens,
including the bridge host that verifies them. Keep each role secret with its
approver and on the verifying bridge only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .canonical import canonical_bytes, sha256_canonical

TOKEN_PREFIX = "aev1"
ROLE_FOUNDER = "founder"
ROLE_GOVERNOR = "policy-governor"
ROLE_SECRET_ENV = {
    ROLE_FOUNDER: "AROBI_EVOLVE_FOUNDER_APPROVAL_SECRET",
    ROLE_GOVERNOR: "AROBI_EVOLVE_GOVERNOR_APPROVAL_SECRET",
}
REQUIRED_ROLES = (ROLE_FOUNDER, ROLE_GOVERNOR)
LEGACY_APPROVAL_FIELDS = ("founderApprovalId", "policyGovernorApprovalId")
DEFAULT_TOKEN_TTL = timedelta(hours=72)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def task_payload_sha256(task: Mapping[str, Any]) -> str:
    """Hash of the task contract, excluding the stored hash and approval grants.

    Approval grants are added after enqueue, so they cannot be part of the
    payload they approve. ``approval.required`` and ``approval.reason`` stay in
    the hash, so a task cannot be edited to drop its approval requirement.
    """
    body = {key: value for key, value in task.items() if key != "payloadSha256"}
    approval = body.get("approval")
    if isinstance(approval, Mapping):
        body["approval"] = {key: value for key, value in approval.items() if key != "grants"}
    return sha256_canonical(body)


def payload_integrity_errors(task: Mapping[str, Any]) -> list[str]:
    stored = task.get("payloadSha256")
    approval = task.get("approval") if isinstance(task.get("approval"), Mapping) else {}
    legacy = [field for field in LEGACY_APPROVAL_FIELDS if field in approval]
    if not isinstance(stored, str) or not stored:
        return ["payloadSha256 is missing; enqueue the task with this bridge so its payload is hashed"]
    computed = task_payload_sha256(task)
    if hmac.compare_digest(stored, computed):
        return []
    if legacy:
        return [
            "payloadSha256 does not match the task body; the task uses the retired unverifiable "
            f"approval fields ({', '.join(legacy)}); re-enqueue it and attach signed approvals"
        ]
    return ["payloadSha256 does not match the task body; the task was modified after enqueue"]


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _token_mac(secret: str, signing_input: str) -> str:
    return hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).hexdigest()


def approval_secret(role: str, env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    name = ROLE_SECRET_ENV.get(role)
    if not name:
        return None
    value = (source.get(name) or "").strip()
    return value or None


def mint_approval_token(
    *,
    role: str,
    task_id: str,
    payload_sha256: str,
    approver: str,
    secret: str,
    issued_at: datetime | None = None,
    ttl: timedelta = DEFAULT_TOKEN_TTL,
    nonce: str | None = None,
) -> str:
    if role not in ROLE_SECRET_ENV:
        raise ValueError(f"unknown approval role: {role}")
    if not approver.strip():
        raise ValueError("approver identity is required")
    if not secret:
        raise ValueError(f"{ROLE_SECRET_ENV[role]} is required to mint a {role} approval")
    issued = issued_at or datetime.now(timezone.utc)
    claims = {
        "v": 1,
        "role": role,
        "taskId": task_id,
        "payloadSha256": payload_sha256,
        "approver": approver.strip(),
        "issuedAt": iso_utc(issued),
        "expiresAt": iso_utc(issued + ttl),
        "nonce": nonce or secrets.token_hex(8),
    }
    encoded = _b64url_encode(canonical_bytes(claims))
    signing_input = f"{TOKEN_PREFIX}.{encoded}"
    return f"{signing_input}.{_token_mac(secret, signing_input)}"


def decode_approval_token(token: str) -> tuple[dict[str, Any] | None, str | None]:
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None, f"approval token is not an {TOKEN_PREFIX} token"
    try:
        claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "approval token claims are not valid base64url JSON"
    if not isinstance(claims, dict):
        return None, "approval token claims must be a JSON object"
    return claims, None


def verify_approval_token(
    token: str,
    *,
    role: str,
    task_id: str,
    payload_sha256: str,
    secret: str | None,
    now: datetime | None = None,
    check_expiry: bool = True,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return ``(claims, errors)``; claims are only returned when errors is empty."""
    if not secret:
        return None, [f"{ROLE_SECRET_ENV.get(role, role)} is not configured; {role} approvals cannot be verified"]
    claims, decode_error = decode_approval_token(token)
    if decode_error or claims is None:
        return None, [decode_error or "approval token could not be decoded"]
    signing_input = token.strip().rsplit(".", 1)[0]
    presented = token.strip().rsplit(".", 1)[1]
    if not hmac.compare_digest(presented, _token_mac(secret, signing_input)):
        return None, [f"{role} approval token signature is invalid"]
    errors: list[str] = []
    if claims.get("v") != 1:
        errors.append("approval token version is not 1")
    if claims.get("role") != role:
        errors.append(f"approval token role is {claims.get('role')!r}, expected {role!r}")
    if claims.get("taskId") != task_id:
        errors.append(f"approval token is for task {claims.get('taskId')!r}, not {task_id!r}")
    if claims.get("payloadSha256") != payload_sha256:
        errors.append("approval token is bound to a different task payload hash")
    approver = claims.get("approver")
    if not isinstance(approver, str) or not approver.strip():
        errors.append("approval token does not name an approver")
    issued = parse_iso_utc(claims.get("issuedAt"))
    expires = parse_iso_utc(claims.get("expiresAt"))
    if issued is None or expires is None:
        errors.append("approval token issuedAt/expiresAt are not valid UTC timestamps")
    elif check_expiry:
        current = now or datetime.now(timezone.utc)
        if expires <= current:
            errors.append(f"{role} approval token expired at {claims.get('expiresAt')}")
        if issued > current + timedelta(minutes=5):
            errors.append(f"{role} approval token issuedAt is in the future")
    if errors:
        return None, errors
    return claims, []


def approval_required(task: Mapping[str, Any], serious: bool) -> bool:
    approval = task.get("approval") if isinstance(task.get("approval"), Mapping) else {}
    return bool(approval.get("required")) or serious


def evaluate_task_approvals(
    task: Mapping[str, Any],
    *,
    serious: bool,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    check_expiry: bool = True,
) -> dict[str, Any]:
    """Verify every recorded grant against the task's current payload hash."""
    required = approval_required(task, serious)
    approval = task.get("approval") if isinstance(task.get("approval"), Mapping) else {}
    legacy = [field for field in LEGACY_APPROVAL_FIELDS if approval.get(field)]
    if not required:
        return {"required": False, "complete": True, "grants": [], "errors": [], "missingRoles": []}
    payload_hash = task_payload_sha256(task)
    task_id = str(task.get("id", ""))
    grants_raw = approval.get("grants") if isinstance(approval.get("grants"), list) else []
    verified: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if legacy:
        errors.append(
            "legacy approval ids are not verifiable and are ignored: " + ", ".join(legacy)
        )
    for grant in grants_raw:
        if not isinstance(grant, Mapping):
            errors.append("approval grant is not an object")
            continue
        role = str(grant.get("role", ""))
        if role not in ROLE_SECRET_ENV:
            errors.append(f"approval grant has unknown role {role!r}")
            continue
        claims, grant_errors = verify_approval_token(
            str(grant.get("token", "")),
            role=role,
            task_id=task_id,
            payload_sha256=payload_hash,
            secret=approval_secret(role, env),
            now=now,
            check_expiry=check_expiry,
        )
        if grant_errors or claims is None:
            errors.extend(grant_errors)
            continue
        verified[role] = {
            "role": role,
            "approver": claims["approver"],
            "issuedAt": claims["issuedAt"],
            "expiresAt": claims["expiresAt"],
            "tokenSha256": hashlib.sha256(str(grant.get("token", "")).encode("utf-8")).hexdigest(),
        }
    missing = [role for role in REQUIRED_ROLES if role not in verified]
    approvers = {grant["approver"].strip().lower() for grant in verified.values()}
    if not missing and len(approvers) < len(REQUIRED_ROLES):
        errors.append("founder and policy-governor approvals must come from different approvers")
    complete = not missing and len(approvers) == len(REQUIRED_ROLES)
    return {
        "required": True,
        "complete": complete,
        "grants": [verified[role] for role in REQUIRED_ROLES if role in verified],
        "errors": errors,
        "missingRoles": missing,
    }
