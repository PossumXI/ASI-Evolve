import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from arobi_integrations import bridge
from arobi_integrations.approvals import task_payload_sha256
from arobi_integrations.bridge import (
    REPO_ROOT,
    ForeignPathError,
    aggregate_telemetry_rows,
    action_needs_approval,
    bridge_paths,
    check_live_deploy,
    check_website_artifact,
    collect_live_analytics,
    load_manifest,
    process_tasks,
    recoverable_failures,
    render_analytics_markdown,
    resolve_local_roots,
    summarize_analytics_delta,
    summarize_status_delta,
    status_failure_items,
    validate_task,
)


class JsonServer:
    """Records GET requests and answers each path with a canned JSON body."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                route = self.path.split("?", 1)[0]
                status, body = server.routes.get(route, (404, {"error": "not found"}))
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class ArobiIntegrationBridgeTests(unittest.TestCase):
    def manifest(self, tmp: Path) -> dict:
        return {
            "schemaVersion": 1,
            "mode": "test",
            "stateRoot": str(tmp / "state"),
            "website": {
                "publicUrl": "https://aura-genesis.org",
                "sourceIndex": "<root:website>/index.html",
                "artifactRoot": "<root:website>/dist",
                "forbiddenDeployRoots": [str(tmp / "legacy")],
            },
            "localRoots": {"website": str(tmp / "site")},
            "governance": {"branchPrefix": "agent/evolve/", "defaultLane": "private"},
        }

    def write_site(self, tmp: Path, source_title: str, dist_title: str, bundle: str = "index-abc123.js", ship_bundle: bool = True) -> Path:
        site = tmp / "site"
        (site / "dist" / "assets").mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text(f'<title data-seo="title">{source_title}</title>', encoding="utf-8")
        (site / "dist" / "index.html").write_text(
            f'<title data-seo="title">{dist_title}</title><script type="module" crossorigin src="/assets/{bundle}"></script>',
            encoding="utf-8",
        )
        if ship_bundle:
            (site / "dist" / "assets" / bundle).write_text("export {};", encoding="utf-8")
        return site

    def test_artifact_guard_reads_expected_values_at_check_time(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"AROBI_EVOLVE_REQUIRED_TITLE": "", "AROBI_EVOLVE_REQUIRED_BUNDLE_MARKER": ""}):
            tmp = Path(raw)
            manifest = self.manifest(tmp)
            self.write_site(tmp, "Arobi | Check AI actions first &amp; keep the proof", "Arobi | Check AI actions first &amp; keep the proof")
            artifact = check_website_artifact(manifest)
            self.assertEqual(artifact["status"], "ok", artifact["findings"])
            self.assertEqual(artifact["expectedTitle"], "Arobi | Check AI actions first & keep the proof")
            self.assertEqual(artifact["entryBundles"], ["/assets/index-abc123.js"])
            self.assertNotIn("latestVerifiedProductionDeployId", artifact)

            self.write_site(tmp, "New source title", "Old built title")
            self.assertIn("dist title does not match the expected title", check_website_artifact(manifest)["findings"])

            self.write_site(tmp, "Same", "Same", bundle="index-missing.js", ship_bundle=False)
            self.assertIn("dist entry bundle is missing from the artifact: /assets/index-missing.js", check_website_artifact(manifest)["findings"])

    def test_artifact_guard_env_overrides(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            manifest = self.manifest(tmp)
            self.write_site(tmp, "Source", "Pinned title")
            with mock.patch.dict(os.environ, {"AROBI_EVOLVE_REQUIRED_TITLE": "Pinned title", "AROBI_EVOLVE_REQUIRED_BUNDLE_MARKER": "index-abc123"}):
                artifact = check_website_artifact(manifest)
            self.assertEqual(artifact["status"], "ok", artifact["findings"])
            self.assertEqual(artifact["expectedTitleSource"], "env:AROBI_EVOLVE_REQUIRED_TITLE")
            with mock.patch.dict(os.environ, {"AROBI_EVOLVE_REQUIRED_TITLE": "", "AROBI_EVOLVE_REQUIRED_BUNDLE_MARKER": "index-zzz"}):
                findings = check_website_artifact(manifest)["findings"]
            self.assertIn("AROBI_EVOLVE_REQUIRED_BUNDLE_MARKER is not referenced by dist index.html", findings)

    def test_artifact_guard_without_source_or_dist_fails_honestly(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"AROBI_EVOLVE_REQUIRED_TITLE": ""}):
            tmp = Path(raw)
            (tmp / "site").mkdir()
            artifact = check_website_artifact(self.manifest(tmp))
            self.assertEqual(artifact["status"], "failed")
            self.assertIn("dist index.html is missing", artifact["findings"])
            self.assertTrue(any("expected title is unavailable" in finding for finding in artifact["findings"]))

    def test_live_deploy_is_read_from_netlify_or_reported_not_configured(self):
        with mock.patch.dict(os.environ, {"NETLIFY_AUTH_TOKEN": "", "NETLIFY_SITE_ID": ""}):
            self.assertEqual(check_live_deploy({}, 5)["status"], "not_configured")
        site = {"published_deploy": {"id": "deploy-live-1", "published_at": "2026-09-27T10:00:00Z", "commit_ref": "abc", "deploy_ssl_url": "https://deploy-live-1--aura.netlify.app"}}
        with JsonServer({"/api/v1/sites/site-123": (200, site)}) as server:
            env = {"NETLIFY_AUTH_TOKEN": "netlify-token", "NETLIFY_SITE_ID": "site-123", "NETLIFY_API_URL": server.url}
            live = check_live_deploy({}, 5, env)
        self.assertEqual(live["status"], "ok")
        self.assertEqual(live["publishedDeployId"], "deploy-live-1")
        self.assertEqual(server.requests[0]["headers"]["authorization"], "Bearer netlify-token")

    def test_serious_actions_require_approval(self):
        self.assertTrue(action_needs_approval("Deploy production", "publish site"))
        self.assertTrue(action_needs_approval("Stripe repair", "billing change"))
        self.assertFalse(action_needs_approval("Route smoke", "read-only public check"))

    def test_process_holds_serious_task_without_signed_approval(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            manifest = self.manifest(tmp)
            (tmp / "site").mkdir(parents=True)
            paths = bridge_paths(manifest)
            task = {
                "schemaVersion": 2,
                "id": "billing-check",
                "title": "Billing checkout investigation",
                "objective": "Inspect Stripe checkout without mutating billing.",
                "targetRoot": str(tmp / "site"),
                "lane": "private",
                "allowedWritePaths": [str(tmp / "state" / "candidate")],
                "evaluator": {"command": ["echo", "ok"], "timeoutSec": 30},
                "approval": {"required": True, "reason": "serious", "grants": []},
            }
            task["payloadSha256"] = task_payload_sha256(task)
            (paths.inbox / "billing-check.json").write_text(json.dumps(task), encoding="utf-8")
            report = process_tasks(manifest, paths, 10)
            self.assertEqual(report["processedCount"], 1)
            self.assertEqual(report["items"][0]["status"], "pending_approval")
            self.assertTrue((paths.pending / "billing-check.json").exists())
            self.assertEqual(list(paths.dispatch_outbox.glob("*.json")), [])

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

    def test_default_manifest_has_no_machine_paths_or_stale_literals(self):
        manifest = load_manifest(None)
        raw = (REPO_ROOT / "arobi_integrations" / "default_manifest.json").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", raw.replace("https:", "").replace("http:", "")))
        self.assertNotIn("Knight", raw)
        for stale in ("requiredTitle", "requiredBundleMarker", "ProductionDeployId", "latestVerifiedAt", "6a141f3a", "arobi.aura-genesis.org/api/v1/info"):
            self.assertNotIn(stale, raw)
        self.assertIsNone(re.search(r"(?<!V)ITE_SUPABASE_URL", (REPO_ROOT / "arobi_integrations" / "bridge.py").read_text(encoding="utf-8")))
        with mock.patch.dict(os.environ, {"AROBI_EVOLVE_ROOT_WEBSITE": "", "AROBI_EVOLVE_ROOT_ASI_EVOLVE": ""}):
            roots, errors = resolve_local_roots(manifest)
        self.assertEqual(errors, {})
        self.assertEqual(roots["asiEvolve"], REPO_ROOT)
        self.assertEqual(roots["website"], (REPO_ROOT.parent / "Asgard_Arobi" / "Websites").resolve())

    def test_manifest_env_overrides_and_foreign_paths(self):
        manifest = load_manifest(None)
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"AROBI_EVOLVE_ROOT_WEBSITE": raw, "AROBI_EVOLVE_STATE_ROOT": str(Path(raw) / "state")}):
            roots, _errors = resolve_local_roots(manifest)
            self.assertEqual(roots["website"], Path(raw).resolve())
            self.assertEqual(bridge.resolve_state_root(manifest), (Path(raw) / "state").resolve())
            services = {service["id"]: service["url"] for service in bridge.manifest_services(manifest)}
        self.assertEqual(services["arobi-public-info"], "https://aura-genesis.org/arobi/api/v1/info")
        for service_id in ("q-gateway-public", "laas-public", "hosted-q", "downloads-catalog", "qicr-verifier-health", "qicr-verifier-ready"):
            self.assertIn(service_id, services)
        if os.name != "nt":
            with self.assertRaises(ForeignPathError):
                bridge.resolve_path_text("D:/ASI-Evolve/.arobi-evolve", REPO_ROOT)
            with mock.patch.dict(os.environ, {"AROBI_EVOLVE_ROOT_WEBSITE": "D:/Websites"}):
                _roots, errors = resolve_local_roots(manifest)
            self.assertIn("website", errors)

    def test_recovery_commands_are_chosen_per_platform(self):
        manifest = load_manifest(None)
        commands = bridge.recovery_commands_for(manifest, "q-gateway-local")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["platforms"], [bridge.current_platform()])
        if os.name != "nt":
            self.assertEqual(bridge.recovery_commands_for(manifest, "discord-bridge"), [])

    def test_user_agent_is_not_a_pinned_date(self):
        self.assertNotIn("2026.05.23", bridge.bridge_user_agent().replace(bridge.__version__, ""))
        self.assertTrue(bridge.bridge_user_agent().startswith("arobi-asi-evolve-bridge/"))

    def test_operator_analytics_api_is_preferred_when_a_token_is_configured(self):
        summary = {
            "generatedAt": "2026-09-27T12:00:00Z",
            "commercialTruth": {"totalAccounts": 12, "verifiedPayingTenants": 1, "demoTenants": 7, "finalizedTokenDeliveries": 3, "uniqueTokenBuyers": 2},
            "unverifiedTelemetryEvents30d": 410,
            "publicSurfaces": [{"name": "Aura Genesis", "url": "https://aura-genesis.org", "status": "active"}],
            "diagnostics": {"usersError": None},
        }
        with JsonServer({"/.netlify/functions/operator-analytics/summary": (200, summary)}) as server:
            env = {"AROBI_OPERATOR_ANALYTICS_TOKEN": "operator-jwt", "AROBI_OPERATOR_ANALYTICS_URL": f"{server.url}/.netlify/functions/operator-analytics/summary"}
            report = collect_live_analytics({}, 7, 5, env=env)
        self.assertEqual(report["source"], "asgard-operator-analytics")
        self.assertEqual(server.requests[0]["headers"]["authorization"], "Bearer operator-jwt")
        self.assertEqual(report["business"]["users"]["total"], 12)
        self.assertEqual(report["business"]["subscriptions"]["verifiedPayingTenants"], 1)
        self.assertEqual(report["stripe"]["status"], "skipped")
        self.assertIn("Verified paying tenants: 1", render_analytics_markdown(report))

    def test_analytics_without_token_or_explicit_fallback_is_not_configured(self):
        report = collect_live_analytics({}, 7, 5, env={})
        self.assertEqual(report["source"], "not_configured")
        self.assertEqual(report["business"], {})
        self.assertIn("direct Supabase fallback is disabled", report["warnings"][0])

    def test_explicit_direct_fallback_queries_every_operated_site(self):
        with JsonServer({
            "/auth/v1/admin/users": (200, {"users": []}),
            "/rest/v1/token_purchase_orders": (200, []),
            "/rest/v1/newsletter_subscribers": (200, []),
            "/rest/v1/contact_inquiries": (200, []),
            "/rest/v1/api_keys": (200, []),
            "/rest/v1/tenant_decisions": (200, []),
            "/rest/v1/site_telemetry_events": (200, [{"site": "qline.site", "event_type": "page_view", "path": "/", "session_id": "s"}]),
        }) as server, mock.patch.dict(os.environ, {"SUPABASE_URL": server.url, "SUPABASE_SERVICE_ROLE_KEY": "service-role", "STRIPE_SECRET_KEY": "", "AROBI_EVOLVE_TELEMETRY_SITES": ""}):
            with mock.patch.object(bridge, "powershell_secret_env_value", return_value=None), mock.patch.object(bridge, "netlify_env_value", return_value=None):
                report = collect_live_analytics({"localRoots": {}}, 7, 5, allow_direct=True, env={})
        self.assertEqual(report["source"], "supabase-direct")
        self.assertEqual(report["fallbackReason"], "AROBI_OPERATOR_ANALYTICS_TOKEN is not configured")
        telemetry_request = next(request for request in server.requests if request["path"].startswith("/rest/v1/site_telemetry_events"))
        self.assertIn("site=in.(aura-genesis.org,qline.site,iorch.net)", telemetry_request["path"])
        self.assertEqual(report["telemetry"]["bySite"], [{"key": "qline.site", "count": 1}])

    def test_authenticated_probe_sends_the_key_or_reports_not_configured(self):
        service = {
            "id": "qicr-verifier-health",
            "url": "",
            "auth": {"env": "IMMACULATE_API_KEY", "header": "Authorization", "scheme": "Bearer"},
            "recoveryTarget": "immaculate-local-harness",
            "expectedStatuses": [200],
        }
        with JsonServer({"/api/qicr/verifier/healthz": (200, {"status": "ok"})}) as server:
            service["url"] = f"{server.url}/api/qicr/verifier/healthz"
            with mock.patch.dict(os.environ, {"IMMACULATE_API_KEY": ""}):
                unmeasured = bridge.http_probe(service, 5)
            self.assertEqual(server.requests, [])
            with mock.patch.dict(os.environ, {"IMMACULATE_API_KEY": "harness-key"}):
                measured = bridge.http_probe(service, 5)
        self.assertEqual(unmeasured["status"], "not_configured")
        self.assertEqual(unmeasured["missing"], ["IMMACULATE_API_KEY"])
        self.assertEqual(measured["status"], "ok")
        self.assertEqual(server.requests[0]["headers"]["authorization"], "Bearer harness-key")
        manifest = {"recoveryCommands": {"immaculate-local-harness": [{"id": "restart", "argv": ["echo", "ok"]}]}}
        self.assertEqual(recoverable_failures(manifest, [unmeasured]), [])
        self.assertEqual(len(recoverable_failures(manifest, [{**unmeasured, "status": "failed"}])), 1)

    def test_analytics_delta_is_not_compared_across_sources(self):
        previous = {"source": "supabase-direct", "business": {"users": {"total": 1}}}
        current = {"source": "asgard-operator-analytics", "business": {"users": {"total": 9}}}
        delta = summarize_analytics_delta(previous, current)
        self.assertFalse(delta["comparable"])
        self.assertEqual(delta["newUsers"], 0)

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
        self.assertNotIn("D:\\", markdown)
        self.assertIn("Signal: Traffic is reaching the site while active/trialing subscriptions are at zero.", markdown)
        self.assertIn("Confirmed users to active/trialing subscriptions: 0/3 (0.0%).", markdown)
        self.assertIn("Active API keys used at least once: 1/4 (25.0%).", markdown)


if __name__ == "__main__":
    unittest.main()
