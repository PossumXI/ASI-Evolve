import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from arobi_integrations import bridge
from arobi_integrations.approvals import (
    ROLE_FOUNDER,
    ROLE_GOVERNOR,
    mint_approval_token,
    task_payload_sha256,
)
from arobi_integrations.canonical import (
    JS_UNDEFINED,
    CanonicalizationError,
    locale_compare_key,
    sha256_canonical,
    stable_stringify,
)
from arobi_integrations.dispatch import (
    DISPATCH_HEADERS,
    PACKET_KEYS,
    SigningKey,
    immaculate_receipt_hash_verified,
    seal_receipt,
    sign_dispatch_packet,
    verify_dispatch_packet,
    verify_receipt,
)

FIXTURE = Path(__file__).parent / "fixtures" / "immaculate_asi_dispatch_vectors.json"
SUPPORT = Path(__file__).parent / "support"
SECRET = "unit-test-dispatch-secret"
KEY_ID = "unit-key"
FOUNDER_SECRET = "unit-founder-secret"
GOVERNOR_SECRET = "unit-governor-secret"
API_KEY = "unit-immaculate-api-key"
DISPATCH_ENV_NAMES = (
    "ASI_DISPATCH_HMAC_SECRET",
    "ASI_DISPATCH_HMAC_KEY_ID",
    "ASI_DISPATCH_ISSUER",
    "IMMACULATE_API_KEY",
    "IMMACULATE_HARNESS_URL",
    "AROBI_EVOLVE_FOUNDER_APPROVAL_SECRET",
    "AROBI_EVOLVE_GOVERNOR_APPROVAL_SECRET",
)


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class GoldenVectorTests(unittest.TestCase):
    """Pins the Python packet signer to vectors produced by Immaculate's own code."""

    def test_immaculate_test_vector_is_reproduced_byte_for_byte(self):
        vector = next(item for item in load_fixture()["packets"] if item["name"] == "immaculate-asi-dispatch-test")
        signed = sign_dispatch_packet(vector["unsignedBody"], SigningKey(vector["keyId"], vector["secret"]))
        self.assertEqual(stable_stringify(vector["unsignedBody"]).encode("utf-8"), vector["canonicalBody"].encode("utf-8"))
        self.assertEqual(signed["packetSha256"], vector["packetSha256"])
        self.assertEqual(signed["signature"], vector["signature"])
        self.assertEqual(signed, vector["signedPacket"])
        self.assertEqual(stable_stringify(signed).encode("utf-8"), vector["signedPacketCanonical"].encode("utf-8"))
        self.assertEqual(vector["immaculateVerdict"]["decision"], "ready")

    def test_non_ascii_vector_matches_and_differs_from_ensure_ascii_hash(self):
        vector = next(item for item in load_fixture()["packets"] if item["name"] == "non-ascii")
        signed = sign_dispatch_packet(vector["unsignedBody"], SigningKey(vector["keyId"], vector["secret"]))
        self.assertEqual(stable_stringify(signed).encode("utf-8"), vector["signedPacketCanonical"].encode("utf-8"))
        self.assertEqual(signed["signature"]["value"], vector["signature"]["value"])
        self.assertIn("🚀", vector["canonicalBody"])
        legacy = hashlib.sha256(
            json.dumps(vector["unsignedBody"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(legacy, vector["packetSha256"], "ensure_ascii hashing must not be what the bridge uses")

    def test_canonical_cases_numbers_strings_and_mixed_case_keys(self):
        for case in load_fixture()["canonicalCases"]:
            self.assertEqual(stable_stringify(case["value"]), case["canonical"])
            self.assertEqual(sha256_canonical(case["value"]), case["sha256"])

    def test_key_order_matches_locale_compare(self):
        keys = load_fixture()["keyOrder"]
        self.assertEqual(len(keys), 400)
        self.assertEqual(sorted(reversed(keys), key=locale_compare_key), keys)

    def test_immaculate_receipt_hash_uses_the_undefined_token(self):
        receipt = load_fixture()["immaculateReceipt"]
        self.assertTrue(immaculate_receipt_hash_verified(receipt))
        self.assertFalse(immaculate_receipt_hash_verified({**receipt, "decision": "review_only"}))
        body = {key: value for key, value in receipt.items() if key != "receiptPath"}
        self.assertIn('"receiptSha256":undefined', stable_stringify({**body, "receiptSha256": JS_UNDEFINED}))

    def test_non_ascii_keys_are_refused_rather_than_guessed(self):
        with self.assertRaises(CanonicalizationError):
            stable_stringify({"clé": 1})

    def test_fixture_documents_its_generator(self):
        generator = load_fixture()["generator"]
        self.assertIn("generate_immaculate_dispatch_vectors.mjs", generator["command"])
        self.assertTrue((Path(__file__).parents[1] / generator["script"]).exists())


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "site").mkdir()
        (self.tmp / "repo").mkdir()
        self.manifest = {
            "schemaVersion": 2,
            "mode": "test",
            "stateRoot": str(self.tmp / "state"),
            "website": {"publicUrl": "https://aura-genesis.org", "forbiddenDeployRoots": [str(self.tmp / "legacy")]},
            "localRoots": {"website": str(self.tmp / "site"), "immaculate": str(self.tmp / "repo")},
            "governance": {"branchPrefix": "agent/evolve/", "defaultLane": "private"},
        }
        self.paths = bridge.bridge_paths(self.manifest)
        cleared = {name: "" for name in DISPATCH_ENV_NAMES}
        self._env = mock.patch.dict(os.environ, cleared)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def set_env(self, **values: str) -> None:
        os.environ.update(values)

    def enqueue(self, task_id: str, title: str, objective: str, target: str = "site", approval_required: bool = False) -> dict:
        args = mock.Mock(
            id=task_id,
            kind="test",
            title=title,
            objective=objective,
            target_root=str(self.tmp / target),
            lane="private",
            allowed_write_path=[str(self.tmp / "state" / "candidate" / task_id)],
            evaluator=["echo", "ok"],
            timeout_sec=30,
            approval_required=approval_required,
        )
        return bridge.create_task(args, self.manifest, self.paths)["task"]

    def approvals_env(self) -> None:
        self.set_env(
            AROBI_EVOLVE_FOUNDER_APPROVAL_SECRET=FOUNDER_SECRET,
            AROBI_EVOLVE_GOVERNOR_APPROVAL_SECRET=GOVERNOR_SECRET,
        )

    def tokens(self, task: dict, founder: str = "founder@arobi", governor: str = "governor@arobi", **overrides) -> dict:
        common = {"task_id": task["id"], "payload_sha256": task["payloadSha256"], **overrides}
        return {
            ROLE_FOUNDER: mint_approval_token(role=ROLE_FOUNDER, approver=founder, secret=FOUNDER_SECRET, **common),
            ROLE_GOVERNOR: mint_approval_token(role=ROLE_GOVERNOR, approver=governor, secret=GOVERNOR_SECRET, **common),
        }


class PacketBuildTests(BridgeTestCase):
    def test_packet_has_exactly_the_immaculate_schema_keys(self):
        task = self.enqueue("route-smoke", "Route smoke", "Read-only public check")
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        self.set_env(ASI_DISPATCH_ISSUER="asi-evolve:unit")
        body = bridge.build_dispatch_packet(task, self.manifest, now=now, nonce="0123456789abcdef")
        self.assertEqual(set(body), set(PACKET_KEYS))
        signed = sign_dispatch_packet(body, SigningKey(KEY_ID, SECRET))
        self.assertEqual(verify_dispatch_packet(signed, {KEY_ID: SECRET}), [])
        self.assertEqual(signed["issuer"], "asi-evolve:unit")
        self.assertEqual(signed["createdAt"], "2026-09-27T12:00:00Z")
        self.assertEqual(signed["expiresAt"], "2026-09-28T12:00:00Z")
        self.assertRegex(signed["branch"], r"^agent/evolve/[a-z0-9-]+$")
        self.assertEqual(signed["taskPayloadSha256"], task_payload_sha256(task))
        self.assertEqual(signed["evaluator"], {"command": ["echo", "ok"], "timeoutSec": 30})
        self.assertIn("laasWebsite", signed["routes"])
        self.assertNotIn("discord", signed["routes"])

    def test_default_nonce_and_issuer(self):
        task = self.enqueue("route-smoke", "Route smoke", "Read-only public check")
        body = bridge.build_dispatch_packet(task, self.manifest)
        self.assertRegex(body["nonce"], r"^[0-9a-f]{32}$")
        self.assertTrue(body["issuer"].startswith("asi-evolve:"))

    def test_website_route_only_for_website_targets(self):
        task = self.enqueue("q-eval", "Q eval", "Benchmark receipts only", target="repo")
        body = bridge.build_dispatch_packet(task, self.manifest)
        self.assertNotIn("laasWebsite", body["routes"])
        self.assertEqual(body["routes"]["immaculate"]["endpoint"], "http://127.0.0.1:8787/api/asi/dispatch")

    def test_non_ascii_task_id_still_yields_valid_branch(self):
        self.assertEqual(bridge.safe_branch_suffix("Été-2026"), "t--2026")
        self.assertEqual(bridge.safe_branch_suffix("---"), "task")

    def test_extra_keys_are_refused(self):
        with self.assertRaises(ValueError):
            sign_dispatch_packet({"schemaVersion": 1, "approvals": []}, SigningKey(KEY_ID, SECRET))


class FakeImmaculate:
    """In-process HTTP stand-in for Immaculate at the network boundary."""

    def __init__(self, mode: str = "verify"):
        self.mode = mode
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, payload = fake.respond(self.path, self.headers, body)
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, path, headers, body):
        if self.mode.startswith("status:"):
            return int(self.mode.split(":", 1)[1]), {"error": "forced"}
        packet = json.loads(body.decode("utf-8"))
        if path != "/api/asi/dispatch" or headers.get("Authorization") != f"Bearer {API_KEY}":
            return 401, {"error": "unauthorized"}
        for name, value in DISPATCH_HEADERS.items():
            if headers.get(name) != value:
                return 403, {"error": f"missing {name}"}
        errors = verify_dispatch_packet(packet, {KEY_ID: SECRET})
        computed = sha256_canonical({k: v for k, v in packet.items() if k not in ("packetSha256", "signature")})
        receipt = {
            "schemaVersion": 1,
            "receivedAt": "2026-09-27T12:00:01.000Z",
            "decision": "rejected" if errors else "ready",
            "taskId": packet.get("taskId"),
            "packetSha256": packet.get("packetSha256") if self.mode != "foreign" else "f" * 64,
            "computedPacketSha256": computed,
            "mappedActionCount": 0 if errors else 1,
            "roundtableActions": [],
            "errors": errors,
            "warnings": [],
            "authority": {"productionDeployAllowed": False, "externalMutationAllowed": False, "secretsAllowed": False},
        }
        receipt["receiptSha256"] = sha256_canonical({**receipt, "receiptSha256": JS_UNDEFINED})
        receipt["receiptPath"] = "/runtime/asi-dispatch/receipt.json"
        return (422 if errors else 200), {"accepted": not errors, "receipt": receipt}


class DeliveryTests(BridgeTestCase):
    def ready_task(self, task_id: str = "route-smoke") -> dict:
        task = self.enqueue(task_id, "Route smoke", "Read-only public check")
        report = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(report["items"][0]["status"], "ready_for_delivery")
        return task

    def test_missing_secret_is_not_configured_and_signs_nothing(self):
        self.ready_task()
        self.set_env(IMMACULATE_API_KEY=API_KEY)
        report = bridge.deliver_dispatches(self.manifest, self.paths)
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual(report["sign"]["missing"], ["ASI_DISPATCH_HMAC_SECRET"])
        self.assertEqual(len(list(self.paths.ready.glob("*.json"))), 1)
        self.assertEqual(list(self.paths.dispatch_outbox.glob("*.json")), [])
        self.assertEqual(list(self.paths.dispatch_delivered.glob("*.json")), [])

    def test_missing_api_key_keeps_signed_packet_queued(self):
        self.ready_task()
        self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, ASI_DISPATCH_HMAC_KEY_ID=KEY_ID)
        report = bridge.deliver_dispatches(self.manifest, self.paths)
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual(report["sign"]["signedCount"], 1)
        self.assertEqual(report["send"]["missing"], ["IMMACULATE_API_KEY"])
        queued = list(self.paths.dispatch_outbox.glob("*.json"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(verify_dispatch_packet(json.loads(queued[0].read_text()), {KEY_ID: SECRET}), [])
        self.assertEqual(list(self.paths.dispatch_delivered.glob("*.json")), [])

    def test_delivery_posts_governed_headers_and_persists_the_receipt(self):
        self.ready_task()
        with FakeImmaculate() as fake:
            self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, ASI_DISPATCH_HMAC_KEY_ID=KEY_ID, IMMACULATE_API_KEY=API_KEY, IMMACULATE_HARNESS_URL=fake.url)
            report = bridge.deliver_dispatches(self.manifest, self.paths)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["send"]["counts"], {"delivered": 1})
        request = fake.requests[0]
        self.assertEqual(request["path"], "/api/asi/dispatch")
        self.assertEqual(request["headers"]["authorization"], f"Bearer {API_KEY}")
        self.assertEqual(request["headers"]["x-immaculate-purpose"], "cognitive-execution")
        self.assertEqual(request["headers"]["x-immaculate-consent-scope"], "system:intelligence:asi-evolve")
        self.assertEqual(request["headers"]["x-immaculate-actor"], "asi-evolve")
        delivered = list(self.paths.dispatch_delivered.glob("*.json"))
        self.assertEqual(len(delivered), 1)
        self.assertEqual(list(self.paths.dispatch_outbox.glob("*.json")), [])
        receipts = sorted(self.paths.receipts.glob("route-smoke-delivery-*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["immaculateReceipt"]["decision"], "ready")
        self.assertTrue(all(receipt["immaculateReceiptCheck"].values()))
        self.assertTrue(verify_receipt(receipt, {KEY_ID: SECRET})["signatureVerified"])
        self.assertEqual(len(list(self.paths.processed.glob("*.json"))), 1)

    def test_immaculate_rejection_moves_packet_to_rejected(self):
        self.ready_task()
        self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, ASI_DISPATCH_HMAC_KEY_ID=KEY_ID, IMMACULATE_API_KEY=API_KEY)
        bridge.sign_ready_tasks(self.manifest, self.paths, 10, now=datetime.now(timezone.utc))
        packet_path = next(self.paths.dispatch_outbox.glob("*.json"))
        packet = json.loads(packet_path.read_text())
        packet["objective"] = "Tampered after signing"
        bridge.write_json(packet_path, packet)
        with FakeImmaculate() as fake:
            self.set_env(IMMACULATE_HARNESS_URL=fake.url)
            report = bridge.deliver_dispatches(self.manifest, self.paths)
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["send"]["counts"], {"rejected": 1})
        self.assertEqual(len(list(self.paths.dispatch_rejected.glob("*.json"))), 1)
        self.assertIn("packetSha256 does not match the packet body", report["send"]["items"][0]["errors"])

    def test_unavailable_and_refused_keep_the_packet_queued(self):
        for mode, outcome in (("status:503", "unavailable"), ("status:401", "refused"), ("status:429", "unavailable")):
            with self.subTest(mode=mode):
                for folder in (self.paths.dispatch_outbox, self.paths.ready):
                    for leftover in folder.glob("*.json"):
                        leftover.unlink()
                self.ready_task(f"task-{mode.split(':')[1]}")
                with FakeImmaculate(mode) as fake:
                    self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, ASI_DISPATCH_HMAC_KEY_ID=KEY_ID, IMMACULATE_API_KEY=API_KEY, IMMACULATE_HARNESS_URL=fake.url)
                    report = bridge.deliver_dispatches(self.manifest, self.paths)
                self.assertEqual(report["status"], "warning")
                self.assertEqual(report["send"]["counts"], {outcome: 1})
                self.assertEqual(len(list(self.paths.dispatch_outbox.glob("*.json"))), 1)
                self.assertEqual(list(self.paths.dispatch_delivered.glob("*.json")), [])

    def test_receipt_for_another_packet_is_not_counted_as_delivered(self):
        self.ready_task()
        with FakeImmaculate("foreign") as fake:
            self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, ASI_DISPATCH_HMAC_KEY_ID=KEY_ID, IMMACULATE_API_KEY=API_KEY, IMMACULATE_HARNESS_URL=fake.url)
            report = bridge.deliver_dispatches(self.manifest, self.paths)
        self.assertEqual(report["send"]["counts"], {"unrecognized_response": 1})
        self.assertEqual(list(self.paths.dispatch_delivered.glob("*.json")), [])

    def test_unreachable_harness_is_unavailable(self):
        self.ready_task()
        self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, IMMACULATE_API_KEY=API_KEY, IMMACULATE_HARNESS_URL="http://127.0.0.1:9")
        report = bridge.deliver_dispatches(self.manifest, self.paths, timeout=2)
        self.assertEqual(report["send"]["counts"], {"unavailable": 1})

    def test_expired_packet_is_rejected_locally_without_a_request(self):
        self.ready_task()
        self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, ASI_DISPATCH_HMAC_KEY_ID=KEY_ID, IMMACULATE_API_KEY=API_KEY)
        bridge.sign_ready_tasks(self.manifest, self.paths, 10, now=datetime.now(timezone.utc) - timedelta(days=2))
        with FakeImmaculate() as fake:
            self.set_env(IMMACULATE_HARNESS_URL=fake.url)
            report = bridge.deliver_dispatches(self.manifest, self.paths)
        self.assertEqual(fake.requests, [])
        self.assertEqual(report["send"]["counts"], {"expired_undelivered": 1})
        self.assertEqual(len(list(self.paths.dispatch_rejected.glob("*.json"))), 1)

    def test_deliver_command_exit_codes(self):
        self.ready_task()
        self.assertEqual(bridge.command_deliver(self.manifest, self.paths, mock.Mock(limit=10, timeout=5)), 2)


class ApprovalTests(BridgeTestCase):
    def serious_task(self) -> dict:
        task = self.enqueue("billing-check", "Billing checkout investigation", "Inspect Stripe checkout without mutating billing.")
        report = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(report["items"][0]["status"], "pending_approval")
        return task

    def test_serious_task_is_held_and_founder_notification_is_honest(self):
        task = self.serious_task()
        self.assertTrue((self.paths.pending / "billing-check.json").exists())
        self.assertFalse((self.paths.state_root / "outbox").exists())
        receipt = json.loads((self.paths.receipts / "billing-check-pending-approval.json").read_text())
        self.assertEqual(receipt["founderNotification"]["status"], "skipped")
        self.assertEqual(receipt["missingRoles"], [ROLE_FOUNDER, ROLE_GOVERNOR])
        self.assertEqual(receipt["payloadSha256"], task["payloadSha256"])
        self.assertEqual(receipt["signature"]["status"], "unsigned")

    def test_rescan_without_approval_keeps_task_pending_without_new_receipts(self):
        self.serious_task()
        before = sorted(path.name for path in self.paths.receipts.iterdir())
        report = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(report["items"][0]["status"], "pending_approval")
        self.assertTrue(report["items"][0]["unchanged"])
        self.assertEqual(sorted(path.name for path in self.paths.receipts.iterdir()), before)

    def test_signed_approvals_move_the_task_to_ready(self):
        task = self.serious_task()
        self.approvals_env()
        result = bridge.approve_task(self.paths, task["id"], self.tokens(task))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["complete"])
        stored = json.loads((self.paths.pending / "billing-check.json").read_text())
        self.assertEqual({grant["approver"] for grant in stored["approval"]["grants"]}, {"founder@arobi", "governor@arobi"})
        self.assertEqual(stored["payloadSha256"], task_payload_sha256(stored))
        report = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(report["items"][0]["status"], "ready_for_delivery")
        self.assertTrue((self.paths.ready / "billing-check.json").exists())
        self.assertEqual({grant["role"] for grant in report["items"][0]["approval"]["grants"]}, {ROLE_FOUNDER, ROLE_GOVERNOR})

    def test_approvals_can_arrive_separately(self):
        task = self.serious_task()
        self.approvals_env()
        tokens = self.tokens(task)
        first = bridge.approve_task(self.paths, task["id"], {ROLE_FOUNDER: tokens[ROLE_FOUNDER]})
        self.assertFalse(first["complete"])
        self.assertEqual(bridge.process_tasks(self.manifest, self.paths, 10)["items"][0]["missingRoles"], [ROLE_GOVERNOR])
        second = bridge.approve_task(self.paths, task["id"], {ROLE_GOVERNOR: tokens[ROLE_GOVERNOR]})
        self.assertTrue(second["complete"])

    def test_bad_tokens_are_refused(self):
        task = self.serious_task()
        self.approvals_env()
        forged = mint_approval_token(role=ROLE_FOUNDER, task_id=task["id"], payload_sha256=task["payloadSha256"], approver="x", secret="wrong")
        other_payload = mint_approval_token(role=ROLE_FOUNDER, task_id=task["id"], payload_sha256="0" * 64, approver="x", secret=FOUNDER_SECRET)
        other_task = mint_approval_token(role=ROLE_FOUNDER, task_id="other", payload_sha256=task["payloadSha256"], approver="x", secret=FOUNDER_SECRET)
        expired = mint_approval_token(
            role=ROLE_FOUNDER, task_id=task["id"], payload_sha256=task["payloadSha256"], approver="x", secret=FOUNDER_SECRET,
            issued_at=datetime.now(timezone.utc) - timedelta(days=10), ttl=timedelta(hours=1),
        )
        wrong_role = mint_approval_token(role=ROLE_GOVERNOR, task_id=task["id"], payload_sha256=task["payloadSha256"], approver="x", secret=FOUNDER_SECRET)
        cases = {
            forged: "signature is invalid",
            other_payload: "different task payload hash",
            other_task: "is for task",
            expired: "expired",
            wrong_role: "role",
            "not-a-token": "not an aev1 token",
        }
        for token, expected in cases.items():
            with self.subTest(expected=expected):
                result = bridge.approve_task(self.paths, task["id"], {ROLE_FOUNDER: token})
                self.assertEqual(result["status"], "error")
                self.assertTrue(any(expected in error for error in result["errors"]), result["errors"])
        stored = json.loads((self.paths.pending / "billing-check.json").read_text())
        self.assertEqual(stored["approval"]["grants"], [])

    def test_same_person_cannot_approve_both_roles(self):
        task = self.serious_task()
        self.approvals_env()
        result = bridge.approve_task(self.paths, task["id"], self.tokens(task, founder="Operator-A", governor="operator-a"))
        self.assertEqual(result["status"], "error")
        self.assertIn("different approvers", result["errors"][0])

    def test_missing_role_secret_is_reported(self):
        task = self.serious_task()
        token = self.tokens(task)[ROLE_FOUNDER]
        result = bridge.approve_task(self.paths, task["id"], {ROLE_FOUNDER: token})
        self.assertEqual(result["status"], "error")
        self.assertIn("AROBI_EVOLVE_FOUNDER_APPROVAL_SECRET is not configured", result["errors"][0])

    def test_edited_task_fails_payload_recompute(self):
        task = self.serious_task()
        path = self.paths.pending / "billing-check.json"
        edited = json.loads(path.read_text())
        edited["approval"]["required"] = False
        bridge.write_json(path, edited)
        report = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(report["items"][0]["status"], "rejected")
        self.assertIn("payloadSha256 does not match", report["items"][0]["errors"][0])
        self.approvals_env()
        self.assertEqual(bridge.approve_task(self.paths, task["id"], self.tokens(task))["status"], "error")

    def test_serious_wording_requires_approval_even_if_flag_is_cleared(self):
        self.enqueue("deploy-x", "Deploy production site", "publish")
        path = self.paths.inbox / "deploy-x.json"
        stored = json.loads(path.read_text())
        stored["approval"]["required"] = False
        stored["payloadSha256"] = task_payload_sha256(stored)
        bridge.write_json(path, stored)
        report = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(report["items"][0]["status"], "pending_approval")

    def test_legacy_unverifiable_approval_ids_are_not_accepted(self):
        legacy = {
            "schemaVersion": 1,
            "id": "legacy-task",
            "title": "Billing checkout investigation",
            "objective": "Inspect Stripe checkout without mutating billing.",
            "targetRoot": str(self.tmp / "site"),
            "allowedWritePaths": [str(self.tmp / "state" / "candidate")],
            "evaluator": {"command": ["echo", "ok"], "timeoutSec": 30},
            "approval": {"required": True, "founderApprovalId": "ok", "policyGovernorApprovalId": "ok"},
        }
        legacy["payloadSha256"] = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        bridge.write_json(self.paths.inbox / "legacy-task.json", legacy)
        report = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(report["items"][0]["status"], "pending_approval")
        self.assertIn("legacy approval ids are not verifiable and are ignored", report["items"][0]["approvalErrors"][0])
        self.assertFalse((self.paths.ready / "legacy-task.json").exists())
        edited = json.loads((self.paths.pending / "legacy-task.json").read_text())
        edited["title"] = "Billing checkout investigation (edited)"
        bridge.write_json(self.paths.pending / "legacy-task.json", edited)
        rescan = bridge.process_tasks(self.manifest, self.paths, 10)
        self.assertEqual(rescan["items"][0]["status"], "rejected")
        self.assertIn("retired unverifiable approval fields", rescan["items"][0]["errors"][0])

    def test_delivery_rechecks_approval_before_signing(self):
        task = self.serious_task()
        self.approvals_env()
        bridge.approve_task(self.paths, task["id"], self.tokens(task))
        bridge.process_tasks(self.manifest, self.paths, 10)
        ready_path = self.paths.ready / "billing-check.json"
        stored = json.loads(ready_path.read_text())
        stored["approval"]["grants"] = [grant for grant in stored["approval"]["grants"] if grant["role"] == ROLE_FOUNDER]
        bridge.write_json(ready_path, stored)
        self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET)
        signing = bridge.sign_ready_tasks(self.manifest, self.paths, 10, now=datetime.now(timezone.utc))
        self.assertEqual(signing["items"][0]["status"], "pending_approval")
        self.assertEqual(list(self.paths.dispatch_outbox.glob("*.json")), [])
        self.assertTrue((self.paths.pending / "billing-check.json").exists())


class ReceiptTests(BridgeTestCase):
    def test_sealed_receipts_verify_and_detect_tampering(self):
        sealed = seal_receipt({"status": "ready_for_delivery", "taskId": "t", "note": "déjà vu"}, SigningKey(KEY_ID, SECRET))
        self.assertEqual(sealed["signature"]["alg"], "HMAC-SHA256")
        self.assertEqual(verify_receipt(sealed, {KEY_ID: SECRET}), {"hashVerified": True, "signatureVerified": True, "signed": True, "keyId": KEY_ID})
        tampered = {**sealed, "status": "delivered"}
        self.assertFalse(verify_receipt(tampered, {KEY_ID: SECRET})["hashVerified"])
        self.assertFalse(verify_receipt(sealed, {KEY_ID: "other"})["signatureVerified"])

    def test_unsigned_receipts_say_so(self):
        sealed = seal_receipt({"status": "rejected"}, None)
        self.assertEqual(sealed["signature"]["status"], "unsigned")
        result = verify_receipt(sealed, {})
        self.assertTrue(result["hashVerified"])
        self.assertFalse(result["signed"])

    def test_process_receipts_are_signed_when_the_key_is_configured(self):
        self.set_env(ASI_DISPATCH_HMAC_SECRET=SECRET, ASI_DISPATCH_HMAC_KEY_ID=KEY_ID)
        self.enqueue("route-smoke", "Route smoke", "Read-only public check")
        bridge.process_tasks(self.manifest, self.paths, 10)
        receipt = json.loads((self.paths.receipts / "route-smoke-ready.json").read_text())
        self.assertTrue(verify_receipt(receipt, {KEY_ID: SECRET})["signatureVerified"])
        args = mock.Mock(path=str(self.paths.receipts / "route-smoke-ready.json"))
        self.assertEqual(bridge.command_verify_receipt(self.manifest, self.paths, args), 0)


def locate_immaculate_harness() -> Path | None:
    configured = os.environ.get("IMMACULATE_ROOT")
    root = Path(configured) if configured else Path(__file__).resolve().parents[2] / "Immaculate"
    harness = root / "apps" / "harness"
    if not (harness / "src" / "asi-dispatch.ts").exists():
        return None
    if not any((candidate / "node_modules" / "tsx").exists() for candidate in (harness, root)):
        return None
    if shutil.which("node") is None:
        return None
    return harness


class ImmaculateLiveIntakeTests(BridgeTestCase):
    """Python-signed packets through Immaculate's real intake code (skipped without a checkout)."""

    def setUp(self):
        super().setUp()
        harness = locate_immaculate_harness()
        if harness is None:
            self.skipTest("Immaculate checkout with node_modules not found (set IMMACULATE_ROOT)")
        env = {**os.environ, "IMMACULATE_API_KEY": API_KEY, "ASI_DISPATCH_HMAC_SECRET": SECRET, "ASI_DISPATCH_HMAC_KEY_ID": KEY_ID}
        self.server = subprocess.Popen(
            ["node", "--import", "tsx", str(SUPPORT / "immaculate_intake_server.mjs")],
            cwd=str(harness),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_server)
        line = self.server.stdout.readline()
        if not line:
            self.fail(f"Immaculate intake server did not start: {self.server.stderr.read()[-2000:]}")
        self.info = json.loads(line)
        self.set_env(
            IMMACULATE_API_KEY=API_KEY,
            IMMACULATE_HARNESS_URL=f"http://127.0.0.1:{self.info['port']}",
            ASI_DISPATCH_HMAC_SECRET=SECRET,
            ASI_DISPATCH_HMAC_KEY_ID=KEY_ID,
        )

    def stop_server(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
        for stream in (self.server.stdout, self.server.stderr):
            if stream:
                stream.close()

    def queue_packet(self, task_id: str, title: str) -> dict:
        # Immaculate's default root policy admits the founder-machine roots, so the
        # packet uses them; the ASI-side task validation is covered elsewhere.
        task = {
            "schemaVersion": 2,
            "id": task_id,
            "title": title,
            "objective": "Verify the protected shell without production mutation.",
            "targetRoot": "D:/Websites",
            "lane": "public",
            "allowedWritePaths": [f"D:/ASI-Evolve/.arobi-evolve/candidate-workspaces/{task_id}"],
            "evaluator": {"command": ["npm", "run", "operator:handoff:check"], "timeoutSec": 120},
            "approval": {"required": False, "reason": "operator selected", "grants": []},
        }
        task["payloadSha256"] = task_payload_sha256(task)
        packet = sign_dispatch_packet(bridge.build_dispatch_packet(task, self.manifest), SigningKey(KEY_ID, SECRET))
        bridge.write_json(self.paths.dispatch_outbox / f"{task_id}.json", packet)
        return packet

    def send(self) -> dict:
        return bridge.send_outbox_packets(self.paths, 10, timeout=15, now=datetime.now(timezone.utc))

    def test_python_signed_packets_pass_the_real_intake(self):
        self.queue_packet("live-ascii", "Protect Aura Genesis LaaS shell deploy route")
        self.queue_packet("live-unicode", "Protéger la coque — 守护 🚀")
        result = self.send()
        self.assertEqual(result["counts"], {"delivered": 2}, json.dumps(result, indent=2)[:3000])
        for item in result["items"]:
            self.assertEqual(item["immaculateReceipt"]["decision"], "ready")
            self.assertTrue(all(item["immaculateReceiptCheck"].values()))

    def test_tampered_packet_is_rejected_by_the_real_intake(self):
        packet = self.queue_packet("live-tampered", "Protect the shell")
        bridge.write_json(self.paths.dispatch_outbox / "live-tampered.json", {**packet, "title": "Changed"})
        result = self.send()
        self.assertEqual(result["counts"], {"rejected": 1})
        self.assertIn("packetSha256 does not match the packet body", result["items"][0]["errors"])

    def test_replayed_packet_is_rejected_when_immaculate_keeps_nonces(self):
        if not self.info.get("nonceStore"):
            self.skipTest("this Immaculate checkout has no persistent ASI nonce store")
        packet = self.queue_packet("live-replay", "Protect the shell")
        self.assertEqual(self.send()["counts"], {"delivered": 1})
        bridge.write_json(self.paths.dispatch_outbox / "live-replay.json", packet)
        replay = self.send()
        self.assertEqual(replay["counts"], {"rejected": 1})
        self.assertTrue(any(re.search("replay", error) for error in replay["items"][0]["errors"]))


if __name__ == "__main__":
    unittest.main()
