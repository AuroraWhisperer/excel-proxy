"""Desktop controls must reuse the right server and allow graceful shutdown."""

from contextlib import nullcontext
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import windows_launcher as launcher


class DesktopLaunchTests(unittest.TestCase):
    def setUp(self):
        self.connect = self.enterContext(patch("socket.create_connection"))

    def test_unready_tcp_port_skips_the_slow_http_probe(self):
        for error in (ConnectionRefusedError(), TimeoutError()):
            with self.subTest(error=error), \
                 patch.object(launcher._OPENER, "open", side_effect=AssertionError("HTTP probe must not run")) as open_url:
                self.connect.side_effect = error
                self.assertFalse(launcher.proxy_running())
                open_url.assert_not_called()
        self.connect.assert_called_with(("127.0.0.1", 8000), timeout=0.2)

    def test_ready_tcp_port_still_checks_proxy_identity(self):
        response = io.BytesIO(json.dumps({"pid_file": launcher.PROXY_PID_FILE}).encode())
        response.status = 200
        with patch.object(launcher._OPENER, "open", return_value=response) as open_url:
            self.assertTrue(launcher.proxy_running())
        self.connect.assert_called_once_with(("127.0.0.1", 8000), timeout=0.2)
        self.connect.return_value.__exit__.assert_called_once()
        open_url.assert_called_once_with(
            f"{launcher.PROXY_BASE_URL}/api/config/background-proxy", timeout=5,
        )

    def test_start_reuses_existing_proxy(self):
        with patch.object(launcher, "_launch_lock", return_value=nullcontext()), \
             patch.object(launcher, "proxy_running", return_value=True), \
             patch.object(launcher.subprocess, "Popen") as spawn:
            launcher.start_proxy()
        spawn.assert_not_called()

    def test_identity_requires_matching_runtime_directory(self):
        for payload in ({"pid_file": "another-directory/proxy.pid"}, {"status": "ok"}, []):
            response = io.BytesIO(json.dumps(payload).encode())
            response.status = 200
            with self.subTest(payload=payload), patch.object(launcher._OPENER, "open", return_value=response):
                with self.assertRaises(RuntimeError):
                    launcher.proxy_running()

    def test_only_connection_refusal_means_proxy_is_stopped(self):
        with patch.object(launcher._OPENER, "open", side_effect=URLError(ConnectionRefusedError())):
            self.assertFalse(launcher.proxy_running())
        for error in (HTTPError(launcher.PROXY_BASE_URL, 401, "Unauthorized", {}, None), TimeoutError()):
            with self.subTest(error=error), patch.object(launcher._OPENER, "open", side_effect=error):
                with self.assertRaises(RuntimeError):
                    launcher.proxy_running()

    def test_closing_desktop_stops_the_proxy(self):
        webview = MagicMock()
        with patch.dict(sys.modules, {"webview": webview}), \
             patch.object(launcher, "_desktop_instance", return_value=nullcontext(123)), \
             patch.object(launcher, "_watch_activation"), \
             patch.object(launcher._OPENER, "open"), \
             patch.object(launcher, "start_proxy") as start, \
             patch.object(launcher, "stop_proxy") as stop:
            launcher._OPENER.open.return_value.__enter__.return_value.status = 200
            launcher.open_dashboard()
        start.assert_called_once()
        webview.start.assert_called_once()
        stop.assert_called_once()

    def test_failed_window_initialization_cleans_up_the_proxy(self):
        webview = MagicMock()
        webview.start.side_effect = RuntimeError("WebView2 initialization failed")
        with patch.dict(sys.modules, {"webview": webview}), \
             patch.object(launcher, "_desktop_instance", return_value=nullcontext(123)), \
             patch.object(launcher, "_watch_activation"), \
             patch.object(launcher._OPENER, "open"), \
             patch.object(launcher, "start_proxy"), \
             patch.object(launcher, "stop_proxy") as stop:
            launcher._OPENER.open.return_value.__enter__.return_value.status = 200
            with self.assertRaisesRegex(RuntimeError, "WebView2 initialization failed"):
                launcher.open_dashboard()
        stop.assert_called_once()

    def test_second_desktop_launch_leaves_the_existing_server_running(self):
        webview = MagicMock()
        with patch.dict(sys.modules, {"webview": webview}), \
             patch.object(launcher, "_desktop_instance", return_value=nullcontext(None)), \
             patch.object(launcher, "start_proxy") as start, \
             patch.object(launcher, "stop_proxy") as stop:
            launcher.open_dashboard()
        start.assert_not_called()
        webview.create_window.assert_not_called()
        stop.assert_not_called()

    def test_dashboard_is_checked_before_creating_the_window(self):
        webview = MagicMock()
        with patch.dict(sys.modules, {"webview": webview}), \
             patch.object(launcher, "_desktop_instance", return_value=nullcontext(123)), \
             patch.object(launcher, "start_proxy"), \
             patch.object(launcher, "stop_proxy") as stop, \
             patch.object(launcher._OPENER, "open", side_effect=HTTPError(launcher.DASHBOARD_URL, 404, "Not found", {}, None)):
            with self.assertRaises(HTTPError):
                launcher.open_dashboard()
        webview.create_window.assert_not_called()
        stop.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Windows hidden process launch")
    def test_early_server_exit_is_reported(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(launcher, "PROXY_STDOUT_LOG_FILE", str(Path(directory) / "stdout.log")), \
             patch.object(launcher, "PROXY_STDERR_LOG_FILE", str(Path(directory) / "stderr.log")), \
             patch.object(launcher, "_launch_lock", return_value=nullcontext()), \
             patch.object(launcher, "proxy_running", return_value=False), \
             patch.object(launcher.subprocess, "Popen") as spawn:
            spawn.return_value.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "启动后退出"):
                launcher.start_proxy()
            self.assertEqual(spawn.call_args.kwargs["creationflags"], launcher.subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(sys.platform == "win32", "Windows named stop event")
    def test_stop_event_requests_graceful_server_shutdown_and_is_cleaned_up(self):
        server = SimpleNamespace(should_exit=False)
        with launcher.shutdown_listener(server):
            launcher._request_stop()
            deadline = time.monotonic() + 2
            while not server.should_exit and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.should_exit)
        with self.assertRaises(OSError):
            launcher._request_stop()
