"""Shared HTTP client configuration and lifetime, without real transports."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import os
import ssl
from threading import Barrier
import unittest
from unittest.mock import AsyncMock, Mock, patch, sentinel

import httpcore
import httpx

import excel_session
import excel_websocket
import upstream_client as client_module


class UpstreamClientTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(client_module, "_EXCEL_UPSTREAM_CLIENT", None))
        self.enterContext(
            patch.object(client_module, "_UPSTREAM_CLIENT_SHUTDOWN_REGISTERED", False)
        )
        self.register = self.enterContext(
            patch.object(client_module.atexit, "register")
        )
        self.enterContext(redirect_stdout(io.StringIO()))

    def test_timeout_defaults_and_explicit_seconds(self):
        self.assertEqual(client_module.configured_upstream_timeout_seconds(), 300)
        with patch.dict(os.environ, {"GHCP_UPSTREAM_TIMEOUT_SECONDS": " 42 "}):
            self.assertEqual(client_module.configured_upstream_timeout_seconds(), 42)

    def test_invalid_timeouts_fall_back_with_diagnostic(self):
        for value in ("not-a-number", "0", "-1"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"GHCP_UPSTREAM_TIMEOUT_SECONDS": value}),
            ):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(
                        client_module.configured_upstream_timeout_seconds(), 300
                    )
                self.assertIn("GHCP_UPSTREAM_TIMEOUT_SECONDS", stderr.getvalue())

    def test_direct_client_preserves_limits_timeout_and_tls_defaults(self):
        with patch.object(
            client_module.httpx, "AsyncClient", return_value=sentinel.client
        ) as create:
            result = client_module._build_upstream_client()
        self.assertIs(result, sentinel.client)
        create.assert_called_once()
        options = create.call_args.kwargs
        self.assertTrue(options["http2"])
        self.assertTrue(options["verify"])
        self.assertTrue(options["trust_env"])
        self.assertEqual(options["timeout"].read, 300)
        self.assertEqual(options["limits"].max_connections, 8)
        self.assertEqual(options["limits"].max_keepalive_connections, 4)
        self.assertEqual(options["limits"].keepalive_expiry, 300.0)

    def test_excel_transport_override_wins_over_http2_environment(self):
        with (
            patch.dict(
                os.environ,
                {"GHCP_UPSTREAM_HTTP2": "true", "GHCP_UPSTREAM_TIMEOUT_SECONDS": "75"},
            ),
            patch.object(
                client_module.httpx, "AsyncClient", return_value=sentinel.client
            ) as create,
        ):
            self.assertIs(client_module.get_excel_upstream_client(), sentinel.client)
        self.assertFalse(create.call_args.kwargs["http2"])
        self.assertEqual(create.call_args.kwargs["timeout"].read, 75)

    def test_proxy_aliases_and_tls_override_reach_client_constructor(self):
        for verify, configured in (
            (True, {}),
            (False, {"GHCP_UPSTREAM_TLS_VERIFY": "false"}),
            (True, {"GHCP_UPSTREAM_TLS_VERIFY": "true"}),
        ):
            with (
                self.subTest(verify=verify),
                patch.dict(
                    os.environ,
                    {"GHCP_UPSTREAM_PROXY": "http://proxy.example:8080", **configured},
                    clear=True,
                ),
                patch.object(
                    client_module.httpx, "AsyncClient", return_value=sentinel.client
                ) as create,
            ):
                client_module._build_upstream_client()
                self.assertEqual(os.environ["HTTPS_PROXY"], "http://proxy.example:8080")
                self.assertEqual(os.environ["HTTP_PROXY"], "http://proxy.example:8080")
                self.assertEqual(create.call_args.kwargs["verify"], verify)
                self.assertFalse(create.call_args.kwargs["http2"])

    def test_missing_http2_support_retries_constructor_without_http2(self):
        for error in (ImportError("h2 unavailable"), RuntimeError("h2 unavailable")):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(
                    client_module.httpx,
                    "AsyncClient",
                    side_effect=[error, sentinel.client],
                ) as create,
            ):
                self.assertIs(client_module._build_upstream_client(), sentinel.client)
                first, fallback = [call.kwargs for call in create.call_args_list]
                self.assertTrue(first["http2"])
                self.assertNotIn("http2", fallback)
                self.assertEqual(
                    {key: value for key, value in first.items() if key != "http2"},
                    fallback,
                )

    def test_concurrent_gets_build_one_client_and_register_shutdown_once(self):
        ready = Barrier(8)

        def get():
            ready.wait(timeout=3)
            return client_module.get_excel_upstream_client()

        with patch.object(
            client_module, "_build_upstream_client", return_value=sentinel.shared
        ) as build:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: get(), range(8)))
        self.assertTrue(all(client is sentinel.shared for client in results))
        build.assert_called_once_with(http2_override=False, mounts_for=None)
        self.register.assert_called_once_with(client_module.shutdown_upstream_client)

    def test_shutdown_closes_once_and_next_get_creates_a_fresh_client(self):
        first = Mock(aclose=AsyncMock())
        second = Mock(aclose=AsyncMock())
        with patch.object(
            client_module, "_build_upstream_client", side_effect=[first, second]
        ) as build:
            self.assertIs(client_module.get_excel_upstream_client(), first)
            client_module.shutdown_upstream_client()
            client_module.shutdown_upstream_client()
            first.aclose.assert_awaited_once()
            self.assertIsNone(client_module._EXCEL_UPSTREAM_CLIENT)
            self.assertIs(client_module.get_excel_upstream_client(), second)
            client_module.shutdown_upstream_client()
            second.aclose.assert_awaited_once()
        self.assertEqual(build.call_count, 2)
        self.register.assert_called_once()

    def test_close_failure_releases_stored_client(self):
        failed = Mock(aclose=AsyncMock(side_effect=RuntimeError("closed loop")))
        with patch.object(client_module, "_EXCEL_UPSTREAM_CLIENT", failed):
            client_module.shutdown_upstream_client()
            self.assertIsNone(client_module._EXCEL_UPSTREAM_CLIENT)
            failed.aclose.assert_awaited_once()

    def test_empty_shutdown_does_not_create_an_event_loop(self):
        with patch.object(client_module.asyncio, "new_event_loop") as new_loop:
            client_module.shutdown_upstream_client()
        new_loop.assert_not_called()
        self.register.assert_not_called()


class WebSocketRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))

        async def http_response(transport, request):
            return httpx.Response(200, json={
                "transport": "http",
                "proxy": isinstance(transport._pool, httpcore.AsyncHTTPProxy),
                "verify": transport._pool._ssl_context.verify_mode,
            })

        async def websocket_response(transport, request):
            return httpx.Response(200, json={
                "transport": "websocket", "verify": transport._verify,
            })

        self.enterContext(patch.object(
            httpx.AsyncHTTPTransport, "handle_async_request", http_response,
        ))
        self.enterContext(patch.object(
            excel_websocket.ExcelWebSocketTransport,
            "handle_async_request", websocket_response,
        ))

    def client(self, origin, no_proxy, *, verify=False):
        with (
            patch.dict(os.environ, {
                "GHCP_UPSTREAM_PROXY": "http://proxy.fixture.invalid:8111",
                "GHCP_NO_PROXY": no_proxy,
                "GHCP_UPSTREAM_TLS_VERIFY": str(verify),
            }, clear=True),
            patch.object(excel_session, "RESPONSES_URL", origin + "/basispoints/api/responses"),
        ):
            return client_module._build_upstream_client(
                http2_override=False, mounts_for=excel_websocket.websocket_mounts,
            )

    async def test_no_proxy_cannot_replace_the_websocket_transport(self):
        for origin, no_proxy in (
            ("wss://bps.openai.com", "bps.openai.com"),
            ("wss://bps.openai.com", ".openai.com"),
            ("wss://bps.openai.com", ".bps.openai.com"),
            ("wss://bps.openai.com:8443", "bps.openai.com:8443"),
            ("wss://bps.openai.com:443", "bps.openai.com:443"),
            ("ws://127.0.0.1:8765", "127.0.0.1"),
            ("ws://127.0.0.1:8765", "127.0.0.1:8765"),
            ("wss://bps.openai.com", "*"),
        ):
            with self.subTest(origin=origin, no_proxy=no_proxy):
                async with self.client(origin, no_proxy) as client:
                    response = await client.post(origin + "/basispoints/api/responses", json={"input": "hello"})
                self.assertEqual(response.json()["transport"], "websocket")

    async def test_only_the_configured_websocket_origin_uses_the_adapter(self):
        async with self.client("wss://bps.openai.com", "") as client:
            for url in (
                "https://bps.openai.com/basispoints/api/attachments",
                "ws://bps.openai.com/basispoints/api/responses",
                "wss://bps.openai.com:8443/basispoints/api/responses",
                "wss://child.bps.openai.com/basispoints/api/responses",
                "wss://other.fixture.invalid/basispoints/api/responses",
            ):
                with self.subTest(url=url):
                    response = await client.post(url, json={"input": "hello"})
                    self.assertEqual(response.json()["transport"], "http")

    async def test_explicit_default_ports_share_the_websocket_origin(self):
        for origin, port in (("wss://bps.openai.com", 443), ("ws://bps.openai.com", 80)):
            with self.subTest(origin=origin):
                async with self.client(origin, "bps.openai.com") as client:
                    response = await client.post(f"{origin}:{port}/basispoints/api/responses", json={})
                self.assertEqual(response.json()["transport"], "websocket")

    async def test_attachment_proxy_bypass_and_tls_settings_are_preserved(self):
        for verify in (True, False):
            with self.subTest(verify=verify):
                async with self.client("wss://bps.openai.com", "bps.openai.com", verify=verify) as client:
                    generation = await client.post("wss://bps.openai.com/basispoints/api/responses", json={})
                    direct = await client.get("https://bps.openai.com/basispoints/api/attachments")
                    proxied = await client.get("https://attachments.fixture.invalid/file")
                self.assertEqual(generation.json(), {"transport": "websocket", "verify": verify})
                expected_verify = ssl.CERT_REQUIRED if verify else ssl.CERT_NONE
                self.assertEqual(direct.json(), {"transport": "http", "proxy": False, "verify": expected_verify})
                self.assertEqual(proxied.json(), {"transport": "http", "proxy": True, "verify": expected_verify})

    async def test_client_close_releases_the_mounted_websocket_transport(self):
        with patch.object(excel_websocket.ExcelWebSocketTransport, "aclose", new_callable=AsyncMock) as close:
            client = self.client("wss://bps.openai.com", "bps.openai.com")
            await client.aclose()
            await client.aclose()
        close.assert_awaited_once()
