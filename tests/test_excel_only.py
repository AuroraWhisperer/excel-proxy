"""HTTP boundaries for the single Excel upstream."""

import sys
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx

import excel_upstream
import proxy


class ExcelOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1",
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_model_catalog_needs_no_upstream_session(self):
        for path in ("/models", "/v1/models"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            models = response.json()["data"]
            self.assertEqual({model["id"] for model in models}, set(excel_upstream.MODEL_IDS))
            self.assertTrue(all(model["owned_by"] == "openai-excel" for model in models))

    async def test_dashboard_loads_from_application_static_directory(self):
        response = await self.client.get("/ui")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>Excel Proxy</title>", response.text)

    async def test_plain_aliases_and_default_route_to_excel(self):
        for path in ("/responses", "/v1/responses"):
            for model in (None, *excel_upstream.MODEL_IDS, *excel_upstream.EXCEL_MODEL_UPSTREAMS.values()):
                with self.subTest(path=path, model=model):
                    body = {"input": "hello"}
                    if model is not None:
                        body["model"] = model
                    handler = AsyncMock(return_value=proxy.JSONResponse({"status": "completed"}))
                    with patch.object(proxy, "_handle_excel_responses", handler):
                        response = await self.client.post(path, json=body)
                    self.assertEqual(response.status_code, 200)
                    handler.assert_awaited_once()
                    self.assertIn(handler.call_args.args[1]["model"], excel_upstream.MODEL_IDS)

    async def test_unknown_models_fail_before_session_discovery(self):
        for path in ("/v1/responses", "/v1/responses/compact"):
            for model in ("claude-opus-4.6", "gpt-unknown", "", 42, {"id": "bad"}):
                with self.subTest(path=path, model=model):
                    with patch.object(proxy, "_handle_excel_responses", new_callable=AsyncMock) as handler:
                        response = await self.client.post(path, json={"model": model, "input": "hello"})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["param"], "model")
                    handler.assert_not_awaited()

    async def test_removed_backend_routes_are_unavailable(self):
        for method, path in (
            ("GET", "/api/auth/status"), ("POST", "/api/auth/device"),
            ("GET", "/api/config/auto-update"), ("POST", "/api/config/auto-update"),
            ("GET", "/api/config/model-routing"), ("GET", "/api/config/safeguard"),
            ("POST", "/v1/chat/completions"), ("POST", "/v1/messages"),
        ):
            with self.subTest(path=path, method=method):
                response = await self.client.request(method, path, json={})
                self.assertEqual(response.status_code, 404)

    async def test_runtime_has_no_github_modules(self):
        for module in ("auth", "copilot_sdk_upstream", "auto_update", "protocol_bridge", "initiator_policy"):
            self.assertNotIn(module, sys.modules)

    async def test_client_setup_only_offers_codex(self):
        response = await self.client.get("/api/config/client-proxy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()["clients"]), {"codex"})
        response = await self.client.post("/api/config/client-proxy", json={"target": "claude"})
        self.assertEqual(response.status_code, 400)

    async def test_dashboard_ignores_historical_non_excel_requests(self):
        now = proxy.util.utc_now_iso()
        excel = {"request_id": "excel-history", "started_at": now, "finished_at": now,
                 "requested_model": "gpt-6-astra-excel", "response_model": "gpt-6-astra",
                 "status_code": 200, "usage": {"input_tokens": 120, "output_tokens": 10}}
        other = {**excel, "request_id": "old-history", "requested_model": "old-model", "response_model": "old-model"}
        with patch.object(proxy.usage_tracker, "snapshot_all_usage_events", return_value=[excel, other]), \
             patch.object(proxy.usage_tracker, "snapshot_usage_events", return_value=[excel, other]):
            dependencies = proxy.dashboard_module.DashboardDependencies(
                snapshot_all_usage_events=proxy.usage_tracker.snapshot_all_usage_events,
                snapshot_usage_events=proxy.usage_tracker.snapshot_usage_events,
            )
            service = proxy.dashboard_module.create_dashboard_service(dependencies=dependencies)
            payload = service.build_payload()
        self.assertEqual(payload["backend"], "excel")
        self.assertEqual(payload["current_month"]["proxy_requests"], 1)
        self.assertEqual([row["request_id"] for row in payload["recent_requests"]], ["excel-history"])
        self.assertNotIn("aic_quota", payload)

    async def test_dashboard_limits_recent_requests_without_truncating_totals(self):
        now = proxy.util.utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        events = [
            {
                "request_id": f"request-{index}",
                "started_at": (now + timedelta(seconds=index)).isoformat(),
                "finished_at": (now + timedelta(seconds=index)).isoformat(),
                "requested_model": "gpt-6-astra-excel",
                "status_code": 200,
                "usage": {"input_tokens": 120, "output_tokens": 10},
            }
            for index in range(101)
        ]
        dependencies = proxy.dashboard_module.DashboardDependencies(
            snapshot_all_usage_events=lambda: events,
            snapshot_usage_events=lambda: events,
        )
        service = proxy.dashboard_module.create_dashboard_service(dependencies=dependencies)
        payload = service.build_payload()
        self.assertEqual(payload["current_month"]["proxy_requests"], 101)
        self.assertEqual(
            [row["request_id"] for row in payload["recent_requests"]],
            [f"request-{index}" for index in range(100, 0, -1)],
        )

    async def test_missing_session_keeps_excel_error(self):
        with patch.object(proxy.excel_session_capture, "refresh_macos_excel_session"), \
             patch.object(proxy.excel_session_capture, "refresh_windows_excel_session"), \
             patch.object(excel_upstream.excel_session_store, "request_headers", side_effect=RuntimeError("Open the ChatGPT Excel add-in and sign in.")):
            response = await self.client.post("/v1/responses", json={"input": "hello"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Excel", response.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
