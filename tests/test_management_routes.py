"""Management routers keep their HTTP contracts without owning proxy state."""

import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


DASHBOARD_ROUTES = {
    ("/", "GET"),
    ("/ui", "GET"),
    ("/ui/requests", "GET"),
    ("/ui/usage", "GET"),
    ("/ui/shared.css", "GET"),
    ("/ui/account-login.js", "GET"),
    ("/ui/api.js", "GET"),
    ("/ui/connection.js", "GET"),
    ("/ui/requests.js", "GET"),
    ("/ui/usage.js", "GET"),
    ("/api/dashboard", "GET"),
    ("/api/dashboard/stream", "GET"),
}
CONFIG_ROUTES = {
    ("/api/config/client-proxy", "GET"),
    ("/api/config/client-proxy", "POST"),
    ("/api/config/client-proxy/settings", "POST"),
    ("/api/config/background-proxy", "GET"),
    ("/api/config/background-proxy", "POST"),
}


def routes(router):
    for route in router.routes:
        # FastAPI may retain included routers rather than flattening them.
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from routes(original)
        else:
            yield route


def route_keys(router):
    return [
        (route.path, method) for route in routes(router) for method in route.methods
    ]


class ManagementRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dashboard_routes = importlib.import_module("dashboard_routes")
        config_routes = importlib.import_module("config_routes")
        self.dashboard_service = SimpleNamespace(
            build_payload=Mock(return_value={"requests": [{"id": "synthetic"}]})
        )
        self.config = SimpleNamespace(
            proxy_client_status_payload=Mock(
                return_value={"settings": {"debug_prompt_logging_enabled": False}}
            ),
            enable_target=Mock(return_value={"configured": True}),
            disable_target=Mock(return_value={"configured": False}),
            empty_proxy_status=Mock(return_value={"configured": False}),
        )
        self.background = SimpleNamespace(
            status_payload=Mock(return_value={"installed": False})
        )
        self.save_settings = Mock(side_effect=lambda payload: payload)
        self.decorate_settings = Mock(side_effect=lambda payload: payload)
        self.parsed = []

        async def parse_request(request: Request):
            self.parsed.append(request.url.path)
            return await request.json()

        self.dashboard_router = dashboard_routes.create_dashboard_router(
            dashboard_service=self.dashboard_service,
            streaming_response_class=StreamingResponse,
        )
        self.config_router = config_routes.create_config_router(
            client_proxy_config_service=self.config,
            background_proxy_manager=self.background,
            save_settings=self.save_settings,
            decorate_settings=self.decorate_settings,
            parse_json_request=parse_request,
        )
        app = FastAPI()
        app.include_router(self.dashboard_router)
        app.include_router(self.config_router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.client.aclose)

    def test_router_paths_and_methods_are_preserved(self):
        self.assertEqual(set(route_keys(self.dashboard_router)), DASHBOARD_ROUTES)
        self.assertEqual(set(route_keys(self.config_router)), CONFIG_ROUTES)
        self.assertEqual(len(route_keys(self.dashboard_router)), len(DASHBOARD_ROUTES))
        self.assertEqual(len(route_keys(self.config_router)), len(CONFIG_ROUTES))
        self.dashboard_service.build_payload.assert_not_called()
        self.config.proxy_client_status_payload.assert_not_called()

    async def test_dashboard_uses_injected_service(self):
        response = await self.client.get("/api/dashboard?refresh=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"requests": [{"id": "synthetic"}]})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.dashboard_service.build_payload.assert_called_once_with(
            True, prefer_cached=True
        )

    async def test_settings_keep_parser_and_settings_callbacks(self):
        response = await self.client.get("/api/config/client-proxy")
        self.assertEqual(response.status_code, 200)
        self.decorate_settings.assert_called_once_with(
            {"debug_prompt_logging_enabled": False}
        )
        response = await self.client.post(
            "/api/config/client-proxy/settings",
            json={"debug_prompt_logging_enabled": True},
        )
        self.assertEqual(response.json(), {"debug_prompt_logging_enabled": True})
        self.save_settings.assert_called_once_with(
            {"debug_prompt_logging_enabled": True}
        )
        self.assertEqual(self.parsed, ["/api/config/client-proxy/settings"])

    async def test_invalid_config_action_has_no_side_effects(self):
        response = await self.client.post(
            "/api/config/client-proxy", json={"target": "codex", "action": "invalid"}
        )
        self.assertEqual(response.status_code, 400)
        self.config.enable_target.assert_not_called()
        self.config.disable_target.assert_not_called()

    async def test_config_enable_and_disable_keep_result_shapes(self):
        for action, configured in (("enable", True), ("disable", False)):
            with self.subTest(action=action):
                response = await self.client.post(
                    "/api/config/client-proxy",
                    json={"target": "codex", "action": action},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["clients"], {"codex": {"configured": configured}}
                )
        self.config.enable_target.assert_called_once_with("codex")
        self.config.disable_target.assert_called_once_with("codex")

    def test_application_registers_each_management_endpoint_once(self):
        import proxy

        keys = route_keys(proxy.app)
        for key in DASHBOARD_ROUTES | CONFIG_ROUTES:
            with self.subTest(route=key):
                self.assertEqual(keys.count(key), 1)
                endpoint = next(
                    route.endpoint
                    for route in routes(proxy.app)
                    if route.path == key[0] and key[1] in route.methods
                )
                self.assertEqual(
                    endpoint.__module__,
                    "dashboard_routes" if key in DASHBOARD_ROUTES else "config_routes",
                )


class AccountRouterBoundaryTests(unittest.TestCase):
    def test_router_has_explicit_dependencies_and_no_reverse_import(self):
        from dataclasses import fields
        from test_module_boundaries import imported_modules

        module = importlib.import_module("account_routes")
        names = {field.name for field in fields(module.AccountRouteDependencies)}
        self.assertEqual(
            names,
            {
                "usage_tracker",
                "proxy_login_service",
                "client_proxy_config_service",
                "activation_lock",
                "parse_json_request",
                "dispatch_response",
                "run_connection_test",
            },
        )
        router = module.create_account_router(
            module.AccountRouteDependencies(**dict.fromkeys(names))
        )
        keys = route_keys(router)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(("/api/proxy-accounts/{record_id}/activate", "POST"), keys)
        self.assertIn(("/api/config/excel-session", "DELETE"), keys)
        self.assertTrue(
            {"proxy", "excel_upstream"}.isdisjoint(imported_modules("account_routes"))
        )

    def test_application_registers_account_routes_once(self):
        import proxy

        keys = route_keys(proxy.app)
        account_routes = [
            route
            for route in routes(proxy.app)
            if getattr(route.endpoint, "__module__", "") == "account_routes"
        ]
        self.assertTrue(account_routes)
        for route in account_routes:
            for method in route.methods:
                self.assertEqual(keys.count((route.path, method)), 1)
