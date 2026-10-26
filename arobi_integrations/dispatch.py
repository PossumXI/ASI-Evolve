"""Signed ASI dispatch packets, sealed receipts, and delivery to Immaculate.

The packet contract is Immaculate's ``asiDispatchPacketSchema``
(``apps/harness/src/asi-dispatch.ts``). Immaculate strips unknown keys before
it hashes, so the packet carries exactly the schema keys: an extra key would
make every hash mismatch. ``packetSha256`` is Immaculate ``sha256Json`` over the
packet without ``packetSha256`` and ``signature``; the signature is
HMAC-SHA256 over that hex digest, keyed by ``ASI_DISPATCH_HMAC_SECRET`` under
the key id ``ASI_DISPATCH_HMAC_KEY_ID`` (Immaculate's default id is ``env``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .approvals import iso_utc, parse_iso_utc
from .canonical import JS_UNDEFINED, CanonicalizationError, sha256_canonical

SIGNATURE_ALG = "HMAC-SHA256"
DEFAULT_KEY_ID = "env"
DEFAULT_HARNESS_URL = "http://127.0.0.1:8787"
DISPATCH_PATH = "/api/asi/dispatch"
DEFAULT_PACKET_TTL = timedelta(hours=24)
DISPATCH_HEADERS = {
    "x-immaculate-purpose": "cognitive-execution",
    "x-immaculate-consent-scope": "system:intelligence:asi-evolve",
    "x-immaculate-actor": "asi-evolve",
}
PACKET_AUTHORITY = {
    "mode": "agent-branch-only",
    "productionDeployAllowed": False,
    "externalMutationAllowed": False,
    "secretsAllowed": False,
    "requiresFounderApprovalForSeriousActions": True,
}
PACKET_KEYS = (
    "schemaVersion",
    "createdAt",
    "expiresAt",
    "issuer",
    "nonce",
    "taskId",
    "taskPayloadSha256",
    "title",
    "objective",
    "lane",
    "targetRoot",
    "allowedWritePaths",
    "branch",
    "authority",
    "evaluator",
    "routes",
)


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    secret: str


def signing_key(env: Mapping[str, str] | None = None) -> SigningKey | None:
    """The dispatch HMAC key, read exactly like Immaculate reads it."""
    source = os.environ if env is None else env
    secret = (source.get("ASI_DISPATCH_HMAC_SECRET") or "").strip()
    if not secret:
        return None
    key_id = (source.get("ASI_DISPATCH_HMAC_KEY_ID") or "").strip() or DEFAULT_KEY_ID
    return SigningKey(key_id=key_id, secret=secret)


def hmac_sha256_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def default_issuer(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    configured = (source.get("ASI_DISPATCH_ISSUER") or "").strip()
    return configured or f"asi-evolve:{socket.gethostname()}"


def packet_body_for_integrity(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in packet.items() if key not in ("packetSha256", "signature")}


def sign_dispatch_packet(packet: Mapping[str, Any], key: SigningKey) -> dict[str, Any]:
    """Python twin of Immaculate ``signAsiDispatchPacket``."""
    body = packet_body_for_integrity(packet)
    unknown = sorted(set(body) - set(PACKET_KEYS))
    if unknown:
        raise ValueError(f"dispatch packet has keys outside the Immaculate schema: {', '.join(unknown)}")
    packet_sha256 = sha256_canonical(body)
    return {
        **body,
        "packetSha256": packet_sha256,
        "signature": {
            "alg": SIGNATURE_ALG,
            "keyId": key.key_id,
            "value": hmac_sha256_hex(key.secret, packet_sha256),
        },
    }


def verify_dispatch_packet(packet: Mapping[str, Any], secrets_by_key: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    computed = sha256_canonical(packet_body_for_integrity(packet))
    if packet.get("packetSha256") != computed:
        errors.append("packetSha256 does not match the packet body")
    signature = packet.get("signature") if isinstance(packet.get("signature"), Mapping) else {}
    secret = (secrets_by_key.get(str(signature.get("keyId", ""))) or "").strip()
    if signature.get("alg") != SIGNATURE_ALG:
        errors.append("signature alg is not HMAC-SHA256")
    elif not secret:
        errors.append(f"no secret for signature key {signature.get('keyId')!r}")
    elif not hmac.compare_digest(str(signature.get("value", "")), hmac_sha256_hex(secret, computed)):
        errors.append("signature does not match the packet body")
    return errors


def packet_expired(packet: Mapping[str, Any], now: datetime) -> bool:
    expires = parse_iso_utc(packet.get("expiresAt"))
    return expires is None or expires <= now


def build_packet_body(
    *,
    task_id: str,
    task_payload_sha256: str,
    title: str,
    objective: str,
    lane: str,
    target_root: str,
    allowed_write_paths: list[str],
    branch: str,
    evaluator_command: list[str],
    evaluator_timeout_sec: int,
    routes: dict[str, Any],
    issuer: str,
    nonce: str,
    now: datetime,
    ttl: timedelta = DEFAULT_PACKET_TTL,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "createdAt": iso_utc(now),
        "expiresAt": iso_utc(now + ttl),
        "issuer": issuer,
        "nonce": nonce,
        "taskId": task_id,
        "taskPayloadSha256": task_payload_sha256,
        "title": title,
        "objective": objective,
        "lane": lane,
        "targetRoot": target_root,
        "allowedWritePaths": list(allowed_write_paths),
        "branch": branch,
        "authority": dict(PACKET_AUTHORITY),
        "evaluator": {"command": list(evaluator_command), "timeoutSec": int(evaluator_timeout_sec)},
        "routes": routes,
    }


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def seal_receipt(receipt: Mapping[str, Any], key: SigningKey | None) -> dict[str, Any]:
    """Hash a bridge receipt and HMAC-sign it with the dispatch key.

    ``receiptSha256`` is Immaculate-canonical sha256 over the receipt without
    ``receiptSha256`` and ``signature``. Without a configured key the receipt
    says so in ``signature.status`` rather than pretending to be signed.
    """
    body = {key_name: value for key_name, value in receipt.items() if key_name not in ("receiptSha256", "signature")}
    digest = sha256_canonical(body)
    sealed = {**body, "receiptSha256": digest}
    if key is None:
        sealed["signature"] = {
            "status": "unsigned",
            "reason": "ASI_DISPATCH_HMAC_SECRET is not configured; the receipt carries only its sha256.",
        }
    else:
        sealed["signature"] = {"alg": SIGNATURE_ALG, "keyId": key.key_id, "value": hmac_sha256_hex(key.secret, digest)}
    return sealed


def verify_receipt(receipt: Mapping[str, Any], secrets_by_key: Mapping[str, str]) -> dict[str, Any]:
    body = {key: value for key, value in receipt.items() if key not in ("receiptSha256", "signature")}
    digest = sha256_canonical(body)
    hash_ok = receipt.get("receiptSha256") == digest
    signature = receipt.get("signature") if isinstance(receipt.get("signature"), Mapping) else {}
    if signature.get("alg") != SIGNATURE_ALG:
        return {"hashVerified": hash_ok, "signatureVerified": False, "signed": False, "reason": signature.get("reason") or "receipt is not signed"}
    secret = (secrets_by_key.get(str(signature.get("keyId", ""))) or "").strip()
    if not secret:
        return {"hashVerified": hash_ok, "signatureVerified": False, "signed": True, "reason": f"no secret for key {signature.get('keyId')!r}"}
    signature_ok = hash_ok and hmac.compare_digest(str(signature.get("value", "")), hmac_sha256_hex(secret, digest))
    return {"hashVerified": hash_ok, "signatureVerified": signature_ok, "signed": True, "keyId": signature.get("keyId")}


def immaculate_receipt_hash_verified(receipt: Mapping[str, Any]) -> bool:
    """Check an Immaculate intake receipt's ``receiptSha256``.

    Immaculate computes it as ``sha256Json({...receipt, receiptSha256: undefined})``
    and adds ``receiptPath`` afterwards (``writeAsiDispatchReceipt``).
    """
    claimed = receipt.get("receiptSha256")
    if not isinstance(claimed, str):
        return False
    body = {key: value for key, value in receipt.items() if key != "receiptPath"}
    body["receiptSha256"] = JS_UNDEFINED
    try:
        return hmac.compare_digest(claimed, sha256_canonical(body))
    except CanonicalizationError:
        return False


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def dispatch_url(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    base = (source.get("IMMACULATE_HARNESS_URL") or "").strip() or DEFAULT_HARNESS_URL
    return base.rstrip("/") + DISPATCH_PATH


def immaculate_api_key(env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    value = (source.get("IMMACULATE_API_KEY") or "").strip()
    return value or None


def post_dispatch_packet(
    url: str,
    api_key: str,
    packet: Mapping[str, Any],
    *,
    timeout: float,
    user_agent: str,
) -> dict[str, Any]:
    # Immaculate hashes the parsed values, so the wire form only has to parse
    # back identically; ASCII escapes also carry any lone surrogate safely.
    body = json.dumps(packet).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": user_agent,
            "Authorization": f"Bearer {api_key}",
            **DISPATCH_HEADERS,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        status = int(error.code)
        text = error.read().decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001 - network failures are reported, not raised.
        return {"httpStatus": None, "payload": None, "transportError": f"{type(error).__name__}: {str(error)[:300]}"}
    try:
        payload = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = None
    return {"httpStatus": status, "payload": payload, "bodySample": None if payload is not None else text[:300]}


def classify_delivery(packet: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    """Map an HTTP exchange to a delivery outcome without over-claiming.

    ``delivered``: Immaculate returned an intake receipt for this exact packet
    with decision ready or review_only. ``rejected``: Immaculate's intake
    returned a rejected receipt (HTTP 422). Everything else keeps the packet
    queued: ``unavailable`` (no response, 429 or 5xx), ``refused`` (401/403 or
    another 4xx without an intake verdict), ``unrecognized_response`` (a reply
    that is not an intake receipt for this packet).
    """
    status = response.get("httpStatus")
    payload = response.get("payload") if isinstance(response.get("payload"), Mapping) else None
    receipt = payload.get("receipt") if payload and isinstance(payload.get("receipt"), Mapping) else None
    if status is None:
        return {"outcome": "unavailable", "reason": response.get("transportError") or "no HTTP response"}
    if status == 429 or status >= 500:
        return {"outcome": "unavailable", "reason": f"Immaculate answered HTTP {status}"}
    if status in (401, 403):
        detail = payload.get("error") or payload.get("message") if payload else None
        return {"outcome": "refused", "reason": f"Immaculate refused the request with HTTP {status}" + (f": {detail}" if detail else "")}
    if receipt is None:
        if 200 <= status < 300:
            return {"outcome": "unrecognized_response", "reason": f"HTTP {status} without an ASI intake receipt"}
        return {"outcome": "refused", "reason": f"HTTP {status} without an ASI intake receipt"}
    decision = receipt.get("decision")
    same_packet = receipt.get("packetSha256") == packet.get("packetSha256")
    check = {
        "packetSha256Matches": same_packet,
        "computedPacketSha256Matches": receipt.get("computedPacketSha256") == packet.get("packetSha256"),
        "receiptSha256Verified": immaculate_receipt_hash_verified(receipt),
    }
    if status == 422 and decision == "rejected":
        return {"outcome": "rejected", "decision": decision, "immaculateReceiptCheck": check, "errors": list(receipt.get("errors") or [])}
    if 200 <= status < 300 and decision in ("ready", "review_only"):
        if not same_packet or not check["receiptSha256Verified"]:
            return {
                "outcome": "unrecognized_response",
                "reason": "intake receipt does not verify against this packet",
                "immaculateReceiptCheck": check,
            }
        return {"outcome": "delivered", "decision": decision, "immaculateReceiptCheck": check}
    return {"outcome": "unrecognized_response", "reason": f"HTTP {status} with receipt decision {decision!r}", "immaculateReceiptCheck": check}


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)
