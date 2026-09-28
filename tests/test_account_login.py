"""Offline login contracts: never open real sign-in or submit credentials."""

import base64
import hashlib
import json
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

import httpx

import account_balances as balances
import account_login as login
import account_login_browser as automation
import proxy


class AccountLoginTests(unittest.TestCase):
    def setUp(self):
        self.store = balances.AccountBalanceStore()
        self.service = login.AccountLoginService(self.store)
        self.enterContext(patch.object(login, "CALLBACK_PORTS", (0,)))
        self.browser = self.enterContext(patch.object(login, "PrivateLoginBrowser"))
        self.browser.return_value.process.poll.return_value = None
        self.addCleanup(self.service.close)

    def start(self):
        result = self.service.start()
        self.query = parse_qs(urlsplit(self.browser.call_args.args[0]).query)
        self.redirect = self.query["redirect_uri"][0]
        self.host = urlsplit(self.redirect).netloc
        self.path = "/auth/callback?" + urlencode(
            {"state": self.query["state"][0], "code": "test-code"}
        )
        return result

    def test_pkce_and_public_status_do_not_expose_private_material(self):
        result = self.start()
        verifier = self.service._pending["verifier"]
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        self.assertEqual(self.query["code_challenge"], [expected])
        self.assertEqual(self.query["code_challenge_method"], ["S256"])
        self.assertEqual(self.query["prompt"], ["login"])
        self.assertEqual(urlsplit(self.redirect).hostname, "127.0.0.1")
        self.assertNotIn(verifier, json.dumps(result))
        self.assertNotIn(self.query["state"][0], json.dumps(result))
        self.assertNotIn("authorize_url", result)
        with self.assertRaises(balances.BalanceError):
            self.service.start()
        self.browser.assert_called_once()

    def test_bad_host_state_and_duplicate_parameters_never_exchange(self):
        self.start()
        with patch.object(login, "exchange_code") as exchange:
            for path, host in (
                (self.path, "evil.example"),
                ("/auth/callback?state=wrong&code=x", self.host),
                (self.path + "&state=duplicate", self.host),
                ("/auth/callback?state=%E4%B8%AD&code=x", self.host),
                (self.path + "&code=duplicate", self.host),
            ):
                self.assertEqual(self.service.complete(path, host)[0], 400)
            exchange.assert_not_called()
        self.assertEqual(self.store.snapshot()["accounts"], [])

    def test_proxy_login_requests_offline_access_without_exposing_tokens(self):
        self.service.offline_access = True
        self.start()
        self.assertIn("offline_access", self.query["scope"][0].split())
        self.assertTrue(self.service._pending["offline_access"])
        self.assertNotIn("offline_access", self.service.snapshot())

    def test_success_imports_once_and_ignores_callback_replay(self):
        self.start()
        with patch.object(
            login, "exchange_code", return_value={"access_token": "fake-access-token"}
        ) as exchange:
            self.assertEqual(self.service.complete(self.path, self.host)[0], 200)
            self.assertEqual(self.service.complete(self.path, self.host)[0], 409)
        exchange.assert_called_once()
        self.assertEqual(len(self.store.snapshot()["accounts"]), 1)
        self.assertNotIn("fake-access-token", json.dumps(self.service.snapshot()))
        self.service.cancel()
        self.assertEqual(self.service.snapshot()["status"], "success")

    def test_cancel_during_exchange_prevents_import(self):
        self.start()

        def exchange(*_args):
            self.service.cancel()
            return {"access_token": "not-saved"}

        with patch.object(login, "exchange_code", side_effect=exchange):
            self.assertEqual(self.service.complete(self.path, self.host)[0], 409)
        self.assertEqual(self.store.snapshot()["accounts"], [])
        self.browser.return_value.close.assert_called()

    def test_cancelled_login_can_restart_immediately(self):
        self.start()
        previous_state = self.query["state"][0]
        self.assertEqual(self.service.cancel()["status"], "cancelled")
        self.assertIsNone(self.service._pending)
        self.assertEqual(self.start()["status"], "waiting")
        self.assertNotEqual(self.query["state"][0], previous_state)
        self.assertEqual(self.store.snapshot()["accounts"], [])

    def test_expired_attempt_does_not_import(self):
        self.start()
        self.service._pending["expires_at"] = 0
        with patch.object(login, "exchange_code") as exchange:
            self.assertEqual(self.service.complete(self.path, self.host)[0], 400)
            exchange.assert_not_called()

    def test_provider_denial_is_not_reflected_as_html(self):
        self.start()
        path = "/auth/callback?" + urlencode(
            {"state": self.query["state"][0], "error": "<script>secret</script>"}
        )
        status, message = self.service.complete(path, self.host)
        self.assertEqual(status, 400)
        self.assertNotIn("script", message)
        self.assertEqual(self.service.snapshot()["status"], "error")

    def test_callback_response_hides_code_and_releases_listener(self):
        self.start()
        with patch.object(
            login, "exchange_code", return_value={"access_token": "fake-token"}
        ):
            opener = build_opener(ProxyHandler({}))
            with opener.open("http://" + self.host + self.path, timeout=3) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                body = response.read().decode()
                self.assertIn("history.replaceState", body)
                self.assertNotIn("test-code", body)
                self.assertNotIn("fake-token", body)
        self.service._thread.join(timeout=2)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", urlsplit(self.redirect).port))

    def test_busy_ports_do_not_interrupt_existing_apps(self):
        with patch.object(login, "HTTPServer", side_effect=OSError("busy")):
            with self.assertRaises(balances.BalanceError):
                self.service.start()
        self.browser.assert_not_called()

    def test_window_launch_failure_cleans_listener(self):
        self.browser.side_effect = balances.BalanceError("no browser")
        with self.assertRaises(balances.BalanceError):
            self.service.start()
        self.assertIsNone(self.service._pending)


class PrivateBrowserTests(unittest.TestCase):
    def test_automated_private_window_exposes_only_a_loopback_debug_port(self):
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with (
            patch.object(
                login,
                "find_login_browser",
                return_value=(Path("edge.exe"), "--inprivate"),
            ),
            patch.object(login.subprocess, "Popen", return_value=process) as spawn,
        ):
            browser = login.PrivateLoginBrowser(None, debug_port=12345)
            try:
                args = spawn.call_args.args[0]
                self.assertIn("--inprivate", args)
                self.assertIn("--remote-debugging-address=127.0.0.1", args)
                self.assertIn("--remote-debugging-port=12345", args)
                self.assertIn(f"--app={browser.initial_url}", args)
                self.assertEqual(urlsplit(browser.initial_url).scheme, "file")
                self.assertTrue(
                    (Path(browser.profile.name) / "login-start.html").is_file()
                )
                self.assertNotIn("--enable-automation", args)
            finally:
                browser.close()

    def test_each_login_gets_small_incognito_window_and_unique_profile(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with (
            patch.dict(login.os.environ, {"ProgramFiles": "C:/Program Files"}),
            patch.object(Path, "is_file", return_value=True),
            patch.object(login.subprocess, "Popen", return_value=process) as spawn,
            patch.object(login.subprocess, "run") as stop,
        ):
            one = login.PrivateLoginBrowser(login.AUTHORIZE_URL)
            two = login.PrivateLoginBrowser(login.AUTHORIZE_URL)
            profiles = (Path(one.profile.name), Path(two.profile.name))
            try:
                self.assertNotEqual(*profiles)
                for call in spawn.call_args_list:
                    args = call.args[0]
                    self.assertIn("--inprivate", args)
                    self.assertIn("--window-size=560,780", args)
                    self.assertTrue(
                        any(arg.startswith("--user-data-dir=") for arg in args)
                    )
                    self.assertNotIn("shell", call.kwargs)
            finally:
                one.close()
                two.close()
            self.assertTrue(all(not path.exists() for path in profiles))
            self.assertEqual(
                stop.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"]
            )

    def test_missing_browser_never_falls_back_to_default(self):
        with (
            patch.object(Path, "is_file", return_value=False),
            patch.object(login.subprocess, "Popen") as spawn,
        ):
            with self.assertRaises(balances.BalanceError):
                login.PrivateLoginBrowser(login.AUTHORIZE_URL)
            spawn.assert_not_called()


class TokenExchangeTests(unittest.TestCase):
    def test_proxy_login_retains_refresh_token_but_never_id_token(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "access_token": "access",
                        "refresh_token": "refresh-secret",
                        "id_token": "id-secret",
                        "expires_in": 3600,
                    },
                )
            )
        )
        with patch.object(login.httpx, "Client", return_value=client):
            result = login.exchange_code(
                "code",
                {
                    "redirect_uri": "local",
                    "verifier": "verifier",
                    "offline_access": True,
                },
            )
        self.assertEqual(result["refresh_token"], "refresh-secret")
        self.assertGreater(result["expires_at"], login.time.time())
        self.assertNotIn("id_token", result)

    def test_refresh_uses_only_the_official_token_endpoint(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, json={"access_token": "renewed", "refresh_token": "rotated"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(login.httpx, "Client", return_value=client):
            self.assertEqual(
                login.refresh_credentials("old-refresh")["access_token"], "renewed"
            )
        self.assertEqual(str(seen[0].url), login.TOKEN_URL)
        self.assertEqual(
            parse_qs(seen[0].content.decode())["grant_type"], ["refresh_token"]
        )

    def test_login_preserves_email_before_discarding_id_token(self):
        claims = {"email": "login@example.com"}
        id_token = (
            "header."
            + login.base64.urlsafe_b64encode(json.dumps(claims).encode())
            .decode()
            .rstrip("=")
            + ".signature"
        )
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "access_token": "access",
                        "id_token": id_token,
                        "refresh_token": "refresh-secret",
                    },
                )
            )
        )
        with patch.object(login.httpx, "Client", return_value=client):
            result = login.exchange_code(
                "code", {"redirect_uri": "local", "verifier": "verifier"}
            )
        self.assertEqual(
            result, {"access_token": "access", "email": "login@example.com"}
        )
        self.assertEqual(
            balances.AccountBalanceStore().import_accounts(result)["accounts"][0][
                "name"
            ],
            "login@example.com",
        )

    def test_fixed_form_exchange_discards_refresh_and_id_tokens(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh-secret",
                    "id_token": "id-secret",
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(login.httpx, "Client", return_value=client):
            result = login.exchange_code(
                "code",
                {
                    "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                    "verifier": "verifier",
                },
            )
        self.assertEqual(result, {"access_token": "access"})
        self.assertEqual(str(requests[0].url), login.TOKEN_URL)
        self.assertEqual(
            parse_qs(requests[0].content.decode())["code_verifier"], ["verifier"]
        )

    def test_upstream_errors_never_leak_response_secrets(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(400, text="secret-body")
            )
        )
        with patch.object(login.httpx, "Client", return_value=client):
            with self.assertRaises(balances.BalanceError) as error:
                login.exchange_code(
                    "code", {"redirect_uri": "local", "verifier": "secret-verifier"}
                )
        self.assertNotIn("secret", str(error.exception))


class LoginRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_json_actions_reject_passwords_and_cross_origin(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        ) as client:
            with patch.object(
                login.login_service, "start", return_value={"status": "waiting"}
            ) as start:
                response = await client.post(
                    "/api/account-balances/login/start",
                    json={},
                    headers={"Origin": "https://evil.example"},
                )
                self.assertEqual(response.status_code, 403)
                response = await client.post(
                    "/api/account-balances/login/start",
                    json={"password": "never-accept"},
                )
                self.assertEqual(response.status_code, 400)
                start.assert_not_called()
                response = await client.post(
                    "/api/account-balances/login/start", json={}
                )
                self.assertEqual(response.status_code, 200)
                start.assert_called_once()

    async def test_automated_login_uses_existing_local_routes_without_echoing_secrets(
        self,
    ):
        value = "user@example.com----private-password---JBSWY3DPEHPK3PXP"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        ) as client:
            for prefix, service in (
                ("/api/account-balances", login.login_service),
                ("/api/proxy-accounts", proxy.proxy_login_service),
            ):
                with (
                    self.subTest(prefix=prefix),
                    patch.object(
                        service, "start", return_value={"status": "waiting"}
                    ) as start,
                ):
                    denied = await client.post(
                        prefix + "/login/start",
                        json={"credentials": value},
                        headers={"Origin": "https://evil.example"},
                    )
                    self.assertEqual(denied.status_code, 403)
                    start.assert_not_called()
                    response = await client.post(
                        prefix + "/login/start", json={"credentials": value}
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertNotIn("private-password", response.text)
                    start.assert_called_once_with(value)
                    for payload in (
                        {"credentials": 123},
                        {"credentials": value, "extra": True},
                    ):
                        invalid = await client.post(
                            prefix + "/login/start", json=payload
                        )
                        self.assertEqual(invalid.status_code, 400)
                    invalid = await client.post(
                        prefix + "/login/cancel", json={"credentials": value}
                    )
                    self.assertEqual(invalid.status_code, 400)


class CredentialFormatTests(unittest.TestCase):
    def test_mixed_delimiters_preserve_password_and_normalize_secret(self):
        for text in (
            "user@example.com----p-a---ss----jbsw y3dp ehpk 3pxp",
            "user@example.com---p-a---ss---JBSWY3DPEHPK3PXP",
        ):
            self.assertEqual(
                automation.parse_credentials(text),
                {
                    "email": "user@example.com",
                    "password": "p-a---ss",
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            )

    def test_invalid_or_multiple_records_never_echo_input(self):
        for text in (
            None,
            {},
            "private-password",
            "a@b.com----private-password---123456",
            "a@b.com----private-password---bad-secret",
            "a@b.com------JBSWY3DPEHPK3PXP",
            "a@b.com----private-password---JBSWY3DPEHPK3PXP\na@b.com----p---JBSWY3DPEHPK3PXP",
        ):
            with (
                self.subTest(text=type(text).__name__),
                self.assertRaises(balances.BalanceError) as error,
            ):
                automation.parse_credentials(text)
            self.assertNotIn("private-password", str(error.exception))

    def test_only_exact_https_origins_can_receive_credentials(self):
        self.assertTrue(
            automation._at_origin(
                "https://auth.openai.com/log-in/password", "auth.openai.com"
            )
        )
        for url in (
            "http://auth.openai.com/",
            "https://auth.openai.com.evil.example/",
            "https://auth.openai.com:444/",
            "https://evil.example/auth.openai.com",
            "https://user@auth.openai.com/",
        ):
            self.assertFalse(automation._at_origin(url, "auth.openai.com"))


class AutomatedServiceTests(unittest.TestCase):
    def test_automated_login_reuses_callback_and_clears_secrets_from_public_state(self):
        service = login.AccountLoginService(balances.AccountBalanceStore())
        self.addCleanup(service.close)
        with (
            patch.object(login, "CALLBACK_PORTS", (0,)),
            patch.object(
                login,
                "find_login_browser",
                return_value=(Path("edge.exe"), "--inprivate"),
            ),
            patch.object(login, "AutomatedLoginBrowser") as browser,
            patch.object(login, "PrivateLoginBrowser") as manual,
        ):
            closed = threading.Event()
            browser.return_value.close.side_effect = closed.set
            browser.return_value.is_alive.return_value = True
            browser.return_value.message = "正在填写账号…"
            result = service.start(
                "user@example.com----private-password---JBSWY3DPEHPK3PXP"
            )
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(result["message"], "正在填写账号…")
            self.assertNotIn("private-password", json.dumps(result))
            self.assertNotIn("JBSWY3DPEHPK3PXP", repr(service._pending))
            manual.assert_not_called()
            query = parse_qs(urlsplit(browser.call_args.args[0]).query)
            path = "/auth/callback?" + urlencode(
                {"state": query["state"][0], "code": "test-code"}
            )
            with patch.object(
                login,
                "exchange_code",
                return_value={"access_token": "fake-access-token"},
            ):
                self.assertEqual(
                    service.complete(path, urlsplit(query["redirect_uri"][0]).netloc)[
                        0
                    ],
                    200,
                )
            self.assertEqual(len(service.store.snapshot()["accounts"]), 1)
            self.assertTrue(
                closed.wait(2),
                "Successful callback must close the window without explicit cancellation",
            )
            browser.return_value.close.assert_called()

    def test_invalid_credentials_never_open_a_browser(self):
        service = login.AccountLoginService(balances.AccountBalanceStore())
        with (
            patch.object(login, "AutomatedLoginBrowser") as browser,
            self.assertRaises(balances.BalanceError),
        ):
            service.start("invalid-private-password")
        browser.assert_not_called()
        self.assertIsNone(service._pending)


class AutomatedBrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.worker = object.__new__(automation.AutomatedLoginBrowser)
        self.worker._stop = threading.Event()
        self.worker.error = None
        self.page = Mock()
        self.context = Mock(pages=[self.page])
        self.context.new_page.return_value = self.page
        self.browser = Mock(contexts=[self.context])
        self.browser.new_context.return_value = self.context
        self.browser.is_connected.return_value = False
        self.native = Mock()
        self.native.is_alive.return_value = True

        def open_private(_url, **_kwargs):
            self.page.url = self.native.initial_url = (
                "file:///private-profile/login-start.html"
            )
            return self.native

        self.factory = Mock(side_effect=open_private)
        manager = self.enterContext(patch("playwright.sync_api.sync_playwright"))
        self.playwright = manager.return_value.__enter__.return_value
        self.playwright.chromium.connect_over_cdp.return_value = self.browser
        self.playwright.chromium.launch.return_value = self.browser
        self.credentials = {
            "password": "private-password",
            "secret": "JBSWY3DPEHPK3PXP",
        }

    def test_automation_reuses_native_private_window_and_closes_its_process(self):
        self.worker._run(login.AUTHORIZE_URL, self.factory, self.credentials)
        self.factory.assert_called_once()
        port = self.factory.call_args.kwargs["debug_port"]
        self.assertGreater(port, 0)
        self.playwright.chromium.launch.assert_not_called()
        self.browser.new_context.assert_not_called()
        self.context.new_page.assert_not_called()
        self.assertEqual(
            self.playwright.chromium.connect_over_cdp.call_args.args[0],
            f"http://127.0.0.1:{port}",
        )
        self.page.goto.assert_called_once_with(
            login.AUTHORIZE_URL, wait_until="domcontentloaded", timeout=15000
        )
        self.native.close.assert_called_once()
        self.assertEqual(self.credentials, {})

    def test_cancel_while_connecting_closes_native_window_without_sending_credentials(
        self,
    ):
        def cancel(*_args, **_kwargs):
            self.worker._stop.set()
            raise RuntimeError("private-password")

        self.playwright.chromium.connect_over_cdp.side_effect = cancel
        self.worker._run(login.AUTHORIZE_URL, self.factory, self.credentials)
        self.page.goto.assert_not_called()
        self.native.close.assert_called_once()
        self.assertIsNone(self.worker.error)
        self.assertEqual(self.credentials, {})

    def test_unrelated_debugging_page_never_receives_login_or_credentials(self):
        self.context.pages = [Mock(url="https://unrelated.example/")]
        self.native.is_alive.side_effect = [True, False]
        self.worker._run(login.AUTHORIZE_URL, self.factory, self.credentials)
        self.page.goto.assert_not_called()
        self.native.close.assert_called_once()
        self.browser.close.assert_not_called()
        self.assertIsNotNone(self.worker.error)
        self.assertNotIn("private-password", self.worker.error)
        self.assertEqual(self.credentials, {})

    def test_browser_disconnect_error_still_closes_owned_native_process(self):
        self.browser.close.side_effect = RuntimeError("connection closed")
        self.worker._run(login.AUTHORIZE_URL, self.factory, self.credentials)
        self.native.close.assert_called_once()
        self.assertEqual(self.credentials, {})

    def test_connection_retries_while_the_native_browser_starts(self):
        self.playwright.chromium.connect_over_cdp.side_effect = [
            OSError("not ready"),
            self.browser,
        ]
        self.worker._run(login.AUTHORIZE_URL, self.factory, self.credentials)
        self.assertEqual(self.playwright.chromium.connect_over_cdp.call_count, 2)
        self.assertIsNone(self.worker.error)
        self.native.close.assert_called_once()


class AutomatedFormTests(unittest.TestCase):
    def setUp(self):
        self.browser = object.__new__(automation.AutomatedLoginBrowser)
        self.browser._stop = threading.Event()
        self.page = Mock(url="https://auth.openai.com/log-in")
        self.email, self.password, self.code = Mock(), Mock(), Mock()
        for field in (self.email, self.password, self.code):
            field.is_visible.return_value = False
        self.alerts = Mock()
        self.alerts.count.return_value = 0

        def locator(selector):
            if selector.startswith('[role="alert"]'):
                return self.alerts
            if selector.startswith('input[type="email"]'):
                return Mock(first=self.email)
            if selector.startswith('input[type="password"]'):
                return Mock(first=self.password)
            if selector.startswith('input[autocomplete="one-time-code"]'):
                return Mock(first=self.code)
            return Mock(inner_text=Mock(return_value="Authenticator code"))

        self.page.locator.side_effect = locator
        self.credentials = {
            "email": "user@example.com",
            "password": "private-password",
            "secret": "JBSWY3DPEHPK3PXP",
        }

    def test_each_step_submits_once_and_password_is_not_sent_to_code_site(self):
        submitted = set()
        self.email.is_visible.return_value = True
        self.assertTrue(self.browser._advance(self.page, self.credentials, submitted))
        self.assertFalse(self.browser._advance(self.page, self.credentials, submitted))
        self.email.fill.assert_called_once_with("user@example.com")
        self.assertNotIn("email", self.credentials)
        self.email.is_visible.return_value = False
        self.password.is_visible.return_value = True
        self.assertTrue(self.browser._advance(self.page, self.credentials, submitted))
        self.assertFalse(self.browser._advance(self.page, self.credentials, submitted))
        self.password.fill.assert_called_once_with("private-password")
        self.assertNotIn("password", self.credentials)
        self.password.is_visible.return_value = False
        self.code.is_visible.return_value = True
        with patch.object(
            self.browser, "_verification_code", return_value="123456"
        ) as get_code:
            self.assertTrue(
                self.browser._advance(self.page, self.credentials, submitted)
            )
            get_code.assert_called_once_with(self.page.context, "JBSWY3DPEHPK3PXP")
        self.code.fill.assert_called_once_with("123456")
        self.assertEqual(self.credentials, {})

    def test_cancel_or_untrusted_page_never_fills_credentials(self):
        for cancelled in (False, True):
            self.page.url = (
                "https://auth.openai.com/" if cancelled else "https://evil.example/"
            )
            if cancelled:
                self.browser._stop.set()
            self.assertFalse(self.browser._advance(self.page, self.credentials, set()))
        self.page.locator.assert_not_called()

    def test_workspace_selection_and_consent_remain_manual(self):
        self.page.url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        with patch.object(self.browser, "_submit") as submit:
            self.assertFalse(
                self.browser._advance(
                    self.page, self.credentials, {"email", "password", "code"}
                )
            )
        submit.assert_not_called()
        self.page.get_by_role.assert_not_called()
        for field in (self.email, self.password, self.code):
            field.fill.assert_not_called()

    def test_email_verification_and_challenges_require_manual_input(self):
        self.code.is_visible.return_value = True
        self.page.url = "https://auth.openai.com/email-verification"
        self.page.locator("body").inner_text.return_value = "Check your email"
        with patch.object(self.browser, "_verification_code") as get_code:
            # Use one stable body node so the test models an email-code screen.
            original = self.page.locator.side_effect
            self.page.locator.side_effect = lambda selector: (
                Mock(inner_text=Mock(return_value="Check your email"))
                if selector == "body"
                else original(selector)
            )
            with self.assertRaises(balances.BalanceError):
                self.browser._advance(self.page, self.credentials, set())
            self.alerts.count.return_value = 1
            with self.assertRaises(balances.BalanceError):
                self.browser._advance(self.page, self.credentials, set())
            get_code.assert_not_called()
        self.code.fill.assert_not_called()

    def test_code_is_read_from_2fa_fun_and_tab_is_closed(self):
        page = Mock(url="https://2fa.fun/")
        field = page.locator.return_value
        field.count.return_value = 1
        field.is_visible.return_value = True
        field.input_value.return_value = "123456"
        context = Mock(new_page=Mock(return_value=page))
        with patch.object(automation.time, "time", return_value=60):
            self.assertEqual(
                self.browser._verification_code(context, "JBSWY3DPEHPK3PXP"), "123456"
            )
        page.goto.assert_called_once_with(
            "https://2fa.fun/", wait_until="domcontentloaded", timeout=15000
        )
        field.fill.assert_called_once_with("JBSWY3DPEHPK3PXP")
        page.close.assert_called_once()

    def test_code_site_redirect_or_cancel_does_not_receive_secret(self):
        page = Mock(url="https://evil.example/")
        context = Mock(new_page=Mock(return_value=page))
        with self.assertRaises(balances.BalanceError):
            self.browser._verification_code(context, "JBSWY3DPEHPK3PXP")
        page.locator.assert_not_called()
        page.close.assert_called_once()

    def test_cancel_after_code_lookup_does_not_submit_it(self):
        self.code.is_visible.return_value = True

        def cancel(*_args):
            self.browser._stop.set()
            return "123456"

        with patch.object(self.browser, "_verification_code", side_effect=cancel):
            self.assertFalse(self.browser._advance(self.page, self.credentials, set()))
        self.code.fill.assert_not_called()
