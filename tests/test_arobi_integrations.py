import json
import tempfile
import unittest
from pathlib import Path

from arobi_integrations.bridge import (
    aggregate_telemetry_rows,
    action_needs_approval,
    bridge_paths,
    check_website_artifact,
    create_task,
    load_manifest,
    process_tasks,
    recoverable_failures,
    render_analytics_markdown,
    summarize_analytics_delta,
    summarize_status_delta,
    status_failure_items,
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
                "baselineProductionDeployId": "baseline-deploy",
                "requiredProductionDeployId": "current-deploy",
                "latestVerifiedProductionDeployId": "current-deploy",
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
            artifact = check_website_artifact(manifest)
            self.assertEqual(artifact["status"], "ok")
            self.assertEqual(artifact["baselineProductionDeployId"], "baseline-deploy")
            self.assertEqual(artifact["requiredProductionDeployId"], "current-deploy")
            self.assertEqual(artifact["latestVerifiedProductionDeployId"], "current-deploy")
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
        self.assertEqual(
            manifest["website"]["requiredProductionDeployId"],
            manifest["website"]["latestVerifiedProductionDeployId"],
        )
        self.assertNotEqual(
            manifest["website"]["baselineProductionDeployId"],
            manifest["website"]["latestVerifiedProductionDeployId"],
        )

    def test_status_delta_detects_failures_and_recoveries(self):
        previous = {
            "services": [
                {"id": "aura-root", "status": "ok"},
                {"id": "q-gateway-local", "status": "failed"},
            ]
        }
        current = {
            "services": [
                {"id": "aura-root", "label": "Aura", "status": "failed"},
                {"id": "q-gateway-local", "label": "Q", "status": "ok"},
            ]
        }
        delta = summarize_status_delta(previous, current)
        self.assertEqual(delta["newFailures"], [{"id": "aura-root", "label": "Aura", "status": "failed"}])
        self.assertEqual(delta["recoveries"], [{"id": "q-gateway-local", "label": "Q", "status": "ok"}])

    def test_status_failure_items_include_current_recoverable_failures(self):
        snapshot = {
            "services": [
                {"id": "superbrain-health", "label": "Superbrain", "status": "failed"},
                {"id": "q-gateway-local", "label": "Q", "status": "ok"},
            ],
            "localRoots": [{"id": "immaculate", "label": "Immaculate root", "status": "ok"}],
            "websiteArtifact": {"status": "ok"},
        }
        manifest = {
            "recoveryCommands": {
                "superbrain-health": [{"id": "start-harness", "argv": ["echo", "ok"]}],
            }
        }
        failures = status_failure_items(snapshot)
        self.assertEqual([item["id"] for item in failures], ["superbrain-health"])
        self.assertEqual([item["id"] for item in recoverable_failures(manifest, failures)], ["superbrain-health"])

    def test_telemetry_aggregation_keeps_clicks_locations_and_dropoff(self):
        rows = [
            {
                "occurred_at": "2026-05-23T00:00:00Z",
                "event_type": "page_view",
                "session_id": "s1",
                "path": "/",
                "country": "US",
                "referrer": "https://google.com",
            },
            {
                "occurred_at": "2026-05-23T00:01:00Z",
                "event_type": "cta_click",
                "session_id": "s1",
                "path": "/pricing",
                "target_label": "Start Supporter",
                "country": "US",
            },
            {
                "occurred_at": "2026-05-23T00:02:00Z",
                "event_type": "page_view",
                "session_id": "s2",
                "path": "/autonomo",
                "country": "CA",
            },
        ]
        telemetry = aggregate_telemetry_rows(rows)
        self.assertEqual(telemetry["totalEvents"], 3)
        self.assertEqual(telemetry["anonymousSessions"], 2)
        self.assertEqual(telemetry["topPaths"][0], {"key": "/", "count": 1})
        self.assertEqual(telemetry["topTargets"][0], {"key": "Start Supporter", "count": 1})
        self.assertEqual(telemetry["funnel"]["pricingIntentClicks"], 1)

    def test_analytics_delta_flags_new_users_and_paid_activity_without_pii(self):
        previous = {
            "business": {
                "users": {"total": 4},
                "tokenOrders": {"paidOrderCount": 1, "paidUsd": 25.0},
                "subscriptions": {"activeOrTrialing": 0},
            }
        }
        current = {
            "business": {
                "users": {"total": 6},
                "tokenOrders": {"paidOrderCount": 2, "paidUsd": 75.0},
                "subscriptions": {"activeOrTrialing": 1},
            }
        }
        delta = summarize_analytics_delta(previous, current)
        self.assertEqual(delta["newUsers"], 2)
        self.assertEqual(delta["newPaidTokenOrders"], 1)
        self.assertEqual(delta["newPaidTokenUsd"], 50.0)
        self.assertEqual(delta["newActiveSubscriptions"], 1)

    def test_analytics_markdown_includes_conversion_and_dropoff(self):
        report = {
            "generatedAt": "2026-05-23T00:00:00Z",
            "sinceDays": 7,
            "business": {
                "users": {"total": 3, "confirmed": 3, "newLast24h": 0},
                "subscriptions": {"activeOrTrialing": 0},
                "tokenOrders": {"totalOrders": 6, "paidOrderCount": 4, "paidUsd": 20.0, "paidOrdersMissingStripeProof": 4},
                "newsletter": {"total": 21, "active": 21},
                "contacts": {"total": 4},
                "apiKeys": {"total": 4, "active": 4, "usedAtLeastOnce": 1},
                "tenantDecisions": {"total": 0},
            },
            "telemetry": {
                "totalEvents": 655,
                "anonymousSessions": 332,
                "topPaths": [{"key": "/", "count": 212}],
                "topTargets": [{"key": "Sign in", "count": 20}],
                "topReferrers": [{"key": "(none)", "count": 624}],
                "byCountry": [{"key": "US", "count": 629}],
                "funnel": {
                    "pageViews": 522,
                    "pricingIntentClicks": 22,
                    "autonomoViews": 10,
                    "apexViews": 80,
                    "dashboardViews": 72,
                },
            },
            "stripe": {"status": "skipped", "reason": "test"},
            "warnings": [],
        }
        markdown = render_analytics_markdown(report)
        self.assertIn("## Conversion And Drop-Off", markdown)
        self.assertIn("Confirmed users to active/trialing subscriptions: 0/3 (0.0%).", markdown)
        self.assertIn("Active API keys used at least once: 1/4 (25.0%).", markdown)


if __name__ == "__main__":
    unittest.main()
