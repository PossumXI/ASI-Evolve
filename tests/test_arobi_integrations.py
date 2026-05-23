import json
import tempfile
import unittest
from pathlib import Path

from arobi_integrations.bridge import (
    action_needs_approval,
    bridge_paths,
    check_website_artifact,
    create_task,
    load_manifest,
    process_tasks,
    validate_task,
)


class ArobiIntegrationBridgeTests(unittest.TestCase):
    def manifest(self, tmp: Path) -> dict:
        return {
            "schemaVersion": 1,
            "mode": "test",
            "stateRoot": str(tmp / "state"),
            "website": {
                "canonicalRoot": str(tmp / "site"),
                "artifactRoot": str(tmp / "site" / "dist"),
                "requiredTitle": "Arobi | Accountable AI products",
                "requiredBundleMarker": "index-arobi-recovery-20260523-dashboard-20260523T1855Z.js",
                "forbiddenDeployRoots": [str(tmp / "legacy")],
            },
            "localRoots": {"site": str(tmp / "site")},
            "governance": {"branchPrefix": "agent/evolve/", "defaultLane": "private"},
        }

    def test_artifact_guard_requires_title_and_bundle_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            manifest = self.manifest(tmp)
            dist = tmp / "site" / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").write_text(
                '<title>Arobi | Accountable AI products</title><script src="/assets/index-arobi-recovery-20260523-dashboard-20260523T1855Z.js"></script>',
                encoding="utf-8",
            )
            self.assertEqual(check_website_artifact(manifest)["status"], "ok")
            (dist / "index.html").write_text("<title>Wrong</title>", encoding="utf-8")
            self.assertEqual(check_website_artifact(manifest)["status"], "failed")

    def test_serious_actions_require_approval(self):
        self.assertTrue(action_needs_approval("Deploy production", "publish site"))
        self.assertTrue(action_needs_approval("Stripe repair", "billing change"))
        self.assertFalse(action_needs_approval("Route smoke", "read-only public check"))

    def test_process_holds_serious_task_without_exact_approval(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            manifest = self.manifest(tmp)
            (tmp / "site").mkdir(parents=True)
            paths = bridge_paths(manifest)
            task = {
                "schemaVersion": 1,
                "id": "billing-check",
                "title": "Billing checkout investigation",
                "objective": "Inspect Stripe checkout without mutating billing.",
                "targetRoot": str(tmp / "site"),
                "lane": "private",
                "allowedWritePaths": [str(tmp / "state" / "candidate")],
                "evaluator": {"command": ["echo", "ok"], "timeoutSec": 30},
                "approval": {"required": True, "founderApprovalId": None, "policyGovernorApprovalId": None},
            }
            (paths.inbox / "billing-check.json").write_text(json.dumps(task), encoding="utf-8")
            report = process_tasks(manifest, paths, 10)
            self.assertEqual(report["processedCount"], 1)
            self.assertEqual(report["items"][0]["status"], "pending_approval")
            self.assertTrue((paths.outbox / "discord" / "billing-check.json").exists())

    def test_validate_rejects_forbidden_write_path(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            manifest = self.manifest(tmp)
            (tmp / "site").mkdir(parents=True)
            (tmp / "legacy").mkdir(parents=True)
            task = {
                "id": "bad",
                "title": "Bad task",
                "objective": "Touch legacy",
                "targetRoot": str(tmp / "site"),
                "allowedWritePaths": [str(tmp / "legacy" / "dist")],
                "evaluator": {"command": ["echo", "ok"], "timeoutSec": 30},
            }
            errors = validate_task(task, manifest)
            self.assertTrue(any("forbidden deploy root" in error for error in errors))

    def test_drive_root_forbidden_does_not_block_isolated_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            manifest = self.manifest(tmp)
            manifest["website"]["forbiddenDeployRoots"].append(Path(tmp.anchor).as_posix())
            (tmp / "site").mkdir(parents=True)
            isolated = tmp / "state" / "candidate"
            task = {
                "id": "good",
                "title": "Good task",
                "objective": "Use isolated candidate workspace",
                "targetRoot": str(tmp / "site"),
                "allowedWritePaths": [str(isolated)],
                "evaluator": {"command": ["echo", "ok"], "timeoutSec": 30},
            }
            errors = validate_task(task, manifest)
            self.assertFalse(any("forbidden deploy root" in error for error in errors))

    def test_default_manifest_loads(self):
        manifest = load_manifest(None)
        self.assertEqual(manifest["website"]["canonicalRoot"], "D:/Websites")


if __name__ == "__main__":
    unittest.main()
