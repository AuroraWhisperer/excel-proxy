"""Shared HTTP client configuration and lifetime, without real transports."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from threading import Barrier
import unittest
from unittest.mock import AsyncMock, Mock, patch, sentinel

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
            (False, {}),
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
        build.assert_called_once_with(http2_override=False)
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
