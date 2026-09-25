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
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import windows_launcher as launcher


class DesktopLaunchTests(unittest.TestCase):
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

    def test_dashboard_is_checked_before_opening_browser(self):
        with patch.object(launcher._OPENER, "open", side_effect=HTTPError(launcher.DASHBOARD_URL, 404, "Not found", {}, None)), \
             patch.object(launcher.webbrowser, "open") as browser:
            with self.assertRaises(HTTPError):
                launcher.open_dashboard()
        browser.assert_not_called()

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
