"""Offline login contracts: never open real sign-in or submit credentials."""

import base64
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

import account_balances as balances
import account_login as login
import account_login_browser as automation
import proxy


class AccountLoginTests(unittest.TestCase):
    CREDS = "example@example.com----example----JBSWY3DPEHPK3PXP"

    def setUp(self):
        self.store = balances.AccountBalanceStore()
        self.service = login.AccountLoginService(self.store)
        self.browser = self.enterContext(patch.object(login, "AutomatedLoginBrowser"))
        self.browser.return_value.is_alive.return_value = True
        self.browser.return_value.callback_url = None
        self.browser.return_value.error = None
        self.browser.return_value.message = "正在填写账号…"
        self.addCleanup(self.service.close)

    def start(self, *, offline_access=False):
        self.service.offline_access = offline_access
        result = self.service.start(self.CREDS)
        self.query = parse_qs(urlsplit(self.browser.call_args.args[0]).query)
        self.redirect = self.query["redirect_uri"][0]
        self.host = urlsplit(self.redirect).netloc
        self.callback_path = urlsplit(self.redirect).path
        self.path = self.callback_path + "?" + urlencode(
            {"state": self.query["state"][0], "code": "test-code"}
        )
        self.callback_url = f"https://{self.host}{self.path}"
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
        self.assertEqual(self.query["client_id"], [login.bps_device_auth.CLIENT_ID])
        self.assertEqual(self.redirect, login.bps_device_auth.BROWSER_REDIRECT_URI)
        self.assertEqual(
            self.browser.call_args.kwargs["callback_uri"],
            login.bps_device_auth.BROWSER_REDIRECT_URI,
        )
        self.assertNotIn("offline_access", self.query["scope"][0].split())
        self.assertIn("organization.read", self.query["scope"][0].split())
        self.assertNotIn(verifier, json.dumps(result))
        self.assertNotIn(self.query["state"][0], json.dumps(result))
        self.assertNotIn("authorize_url", result)
        with self.assertRaises(balances.BalanceError):
            self.service.start(self.CREDS)
        self.browser.assert_called_once()

    def test_bad_host_state_and_duplicate_parameters_never_exchange(self):
        self.start()
        with patch.object(login, "exchange_code") as exchange:
            for path, host in (
                (self.path, "evil.example"),
                (self.callback_path + "?state=wrong&code=x", self.host),
                (self.path + "&state=duplicate", self.host),
                (self.callback_path + "?state=%E4%B8%AD&code=x", self.host),
                (self.path + "#fragment", self.host),
                (self.path + "&code=duplicate", self.host),
            ):
                self.assertEqual(self.service.complete(path, host)[0], 400)
            exchange.assert_not_called()
        self.assertEqual(self.store.snapshot()["accounts"], [])

    def test_automated_proxy_login_uses_bps_browser_oauth_grant(self):
        self.start(offline_access=True)
        scopes = self.query["scope"][0].split()
        self.assertIn("offline_access", scopes)
        self.assertIn("organization.read", scopes)
        self.assertEqual(self.query["client_id"], [login.bps_device_auth.CLIENT_ID])
        self.assertEqual(self.redirect, login.bps_device_auth.BROWSER_REDIRECT_URI)
        self.assertTrue(self.service._pending["offline_access"])
        self.assertEqual(
            self.service._pending["client_id"], login.bps_device_auth.CLIENT_ID
        )
        self.assertNotIn("offline_access", self.service.snapshot())

    def test_success_imports_once_and_ignores_callback_replay(self):
        self.start()
        with patch.object(
            login, "exchange_code", return_value={"access_token": "fake-access-token"}
        ) as exchange:
            self.assertEqual(self.service.complete_callback(self.callback_url)[0], 200)
            self.assertEqual(self.service.complete_callback(self.callback_url)[0], 409)
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
            self.assertEqual(self.service.complete_callback(self.callback_url)[0], 409)
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
        path = self.callback_path + "?" + urlencode(
            {"state": self.query["state"][0], "error": "<script>secret</script>"}
        )
        status, message = self.service.complete(path, self.host)
        self.assertEqual(status, 400)
        self.assertNotIn("script", message)
        self.assertEqual(self.service.snapshot()["status"], "error")

    def test_captured_callback_completes_without_opening_localhost_port(self):
        self.start(offline_access=True)
        with patch.object(
            login,
            "exchange_code",
            return_value={
                "access_token": "fake-token",
                "client_id": login.bps_device_auth.CLIENT_ID,
            },
        ) as exchange:
            self.browser.return_value.callback_url = self.callback_url
            self.service._thread.join(timeout=2)
        self.assertFalse(self.service._thread.is_alive())
        exchange.assert_called_once()
        self.assertEqual(self.service.snapshot()["status"], "success")
        self.browser.return_value.close.assert_called()
        self.assertEqual(self.start()["status"], "waiting")

    def test_invalid_captured_callback_marks_error_and_stops_waiting(self):
        self.start()
        with patch.object(login, "exchange_code") as exchange:
            self.browser.return_value.callback_url = (
                "http://evil.example/auth/callback?state=wrong&code=x"
            )
            self.service._thread.join(timeout=2)
        exchange.assert_not_called()
        self.assertEqual(self.service.snapshot()["status"], "error")
        self.assertIsNone(self.service._pending)

    def test_window_launch_failure_cleans_pending_state(self):
        self.browser.side_effect = balances.BalanceError("no browser")
        with self.assertRaises(balances.BalanceError):
            self.service.start(self.CREDS)
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
    def test_manual_proxy_login_uses_bps_device_approval_and_saves_its_issuer(self):
        verifier = "v" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        calls = []

        def respond(request):
            calls.append(request)
            if request.url.path.endswith("/usercode"):
                return httpx.Response(200, json={"user_code": "TEST-1234", "device_auth_id": "fixture", "interval": 1})
            if request.url.path.endswith("/deviceauth/token"):
                if len(calls) == 2:
                    return httpx.Response(403, json={"error": {"code": "deviceauth_authorization_pending"}})
                return httpx.Response(200, json={"authorization_code": "approved", "code_verifier": verifier, "code_challenge": challenge})
            return httpx.Response(200, json={"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 3600})

        create_client = httpx.Client
        store = Mock()
        store.import_accounts.return_value = {"imported_ids": ["account"]}
        service = login.AccountLoginService(store, offline_access=True)
        with (
            patch.object(login, "PrivateLoginBrowser") as browser,
            patch.object(login.httpx, "Client", side_effect=lambda **kw: create_client(transport=httpx.MockTransport(respond), **kw)),
        ):
            service.start()
            service._thread.join(5)
            self.assertEqual(service.snapshot()["status"], "success")
            browser.assert_called_once_with(login.bps_device_auth.APPROVAL_URL)
            browser.return_value.close.assert_called_once()
        self.assertEqual(json.loads(calls[0].content)["client_id"], login.bps_device_auth.CLIENT_ID)
        self.assertEqual(str(calls[-1].url), login.TOKEN_URL + "?unified=true")
        self.assertEqual(parse_qs(calls[-1].content.decode())["code_verifier"], [verifier])
        saved = store.import_accounts.call_args.args[0]
        self.assertEqual(saved["client_id"], login.bps_device_auth.CLIENT_ID)
        self.assertEqual(saved["access_token"], "fixture-access")
        self.assertNotIn("fixture", str(service.snapshot()))

    def test_manual_balance_login_uses_bps_device_approval_without_refresh_token(self):
        verifier = "v" * 43
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        def respond(request):
            if request.url.path.endswith("/usercode"):
                return httpx.Response(
                    200,
                    json={
                        "user_code": "TEST-5678",
                        "device_auth_id": "fixture",
                        "interval": 1,
                    },
                )
            if request.url.path.endswith("/deviceauth/token"):
                return httpx.Response(
                    200,
                    json={
                        "authorization_code": "approved",
                        "code_verifier": verifier,
                        "code_challenge": challenge,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                    "expires_in": 3600,
                },
            )

        create_client = httpx.Client
        store = Mock()
        store.import_accounts.return_value = {"imported_ids": ["account"]}
        service = login.AccountLoginService(store, offline_access=False)
        with (
            patch.object(login, "PrivateLoginBrowser"),
            patch.object(
                login.httpx,
                "Client",
                side_effect=lambda **kw: create_client(
                    transport=httpx.MockTransport(respond), **kw
                ),
            ),
        ):
            service.start()
            service._thread.join(5)
        self.assertEqual(service.snapshot()["status"], "success")
        saved = store.import_accounts.call_args.args[0]
        self.assertEqual(saved, {"access_token": "fixture-access"})

    def test_bps_browser_code_exchange_uses_unified_token_endpoint_and_issuer(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": "bps-access",
                    "refresh_token": "bps-refresh",
                    "id_token": "id-secret",
                    "expires_in": 3600,
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(login.httpx, "Client", return_value=client):
            result = login.exchange_code(
                "bps-code",
                {
                    "client_id": login.bps_device_auth.CLIENT_ID,
                    "redirect_uri": login.bps_device_auth.BROWSER_REDIRECT_URI,
                    "verifier": "verifier",
                    "offline_access": True,
                },
            )
        self.assertEqual(str(seen[0].url), login.TOKEN_URL + "?unified=true")
        form = parse_qs(seen[0].content.decode())
        self.assertEqual(form["client_id"], [login.bps_device_auth.CLIENT_ID])
        self.assertEqual(
            form["redirect_uri"], [login.bps_device_auth.BROWSER_REDIRECT_URI]
        )
        self.assertEqual(result["client_id"], login.bps_device_auth.CLIENT_ID)
        self.assertEqual(result["refresh_token"], "bps-refresh")
        self.assertNotIn("id_token", result)

    def test_cancel_during_device_code_request_never_opens_browser_or_saves_account(self):
        entered, release = threading.Event(), threading.Event()
        def get_code(_client):
            entered.set()
            release.wait(5)
            return {"user_code": "test"}, 1

        service = login.AccountLoginService(Mock(), offline_access=True)
        with (
            patch.object(login.bps_device_auth, "request_code", side_effect=get_code),
            patch.object(login.httpx, "Client"),
            patch.object(login, "PrivateLoginBrowser") as browser,
        ):
            service.start()
            self.assertTrue(entered.wait(3))
            service.cancel()
            release.set()
            service.close()
        browser.assert_not_called()
        service.store.import_accounts.assert_not_called()
        self.assertEqual(service.snapshot()["status"], "cancelled")

    def test_device_grant_rejects_an_unmatched_challenge(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "authorization_code": "code", "code_verifier": "x" * 43, "code_challenge": "wrong",
        }))) as client:
            with self.assertRaisesRegex(ValueError, "challenge"):
                login.bps_device_auth.poll_grant(client, {"device_auth_id": "fixture"})

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
    def test_automated_login_captures_bps_callback_and_clears_secrets_from_public_state(
        self,
    ):
        service = login.AccountLoginService(balances.AccountBalanceStore())
        self.addCleanup(service.close)
        with (
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
            browser.return_value.callback_url = None
            browser.return_value.error = None
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
            callback_url = (
                login.bps_device_auth.BROWSER_REDIRECT_URI
                + "?"
                + urlencode({"state": query["state"][0], "code": "test-code"})
            )
            with patch.object(
                login,
                "exchange_code",
                return_value={"access_token": "fake-access-token"},
            ):
                browser.return_value.callback_url = callback_url
                self.assertTrue(
                    closed.wait(2),
                    "Successful callback must close the window without explicit cancellation",
                )
            service._thread.join(timeout=2)
            self.assertEqual(len(service.store.snapshot()["accounts"]), 1)
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

    def test_cdp_fetch_capture_aborts_callback_and_accepts_main_frame_only(self):
        callback_uri = login.bps_device_auth.BROWSER_REDIRECT_URI
        handlers = {}
        cdp = Mock()
        cdp.send.side_effect = lambda method, params=None: (
            {"frameTree": {"frame": {"id": "main-frame"}}}
            if method == "Page.getFrameTree"
            else {}
        )
        cdp.on.side_effect = lambda event, fn: handlers.__setitem__(event, fn)
        self.page.context = self.context
        self.context.new_cdp_session.return_value = cdp

        def fire_paused(*_args, **_kwargs):
            paused = handlers["Fetch.requestPaused"]
            # Non-matching URL continues normally.
            paused(
                {
                    "requestId": "req-other",
                    "frameId": "main-frame",
                    "request": {"url": callback_uri + "-extra"},
                }
            )
            # Subframe callback is aborted and ignored.
            paused(
                {
                    "requestId": "req-subframe",
                    "frameId": "sub-frame",
                    "request": {"url": callback_uri + "?code=bad&state=s"},
                }
            )
            # Main-frame callback is aborted before network delivery and captured.
            paused(
                {
                    "requestId": "req-main",
                    "frameId": "main-frame",
                    "request": {"url": callback_uri + "?code=good&state=s"},
                }
            )
            raise RuntimeError("net::ERR_ABORTED")

        self.page.goto.side_effect = fire_paused
        self.browser.is_connected.return_value = True
        self.page.is_closed.return_value = False
        self.worker._run(
            login.AUTHORIZE_URL,
            self.factory,
            self.credentials,
            callback_uri=callback_uri,
        )
        self.assertEqual(self.worker.callback_url, callback_uri + "?code=good&state=s")
        cdp.send.assert_any_call(
            "Fetch.continueRequest", {"requestId": "req-other"}
        )
        cdp.send.assert_any_call(
            "Fetch.failRequest",
            {"requestId": "req-subframe", "errorReason": "Aborted"},
        )
        cdp.send.assert_any_call(
            "Fetch.failRequest",
            {"requestId": "req-main", "errorReason": "Aborted"},
        )
        self.page.wait_for_timeout.assert_not_called()
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
            get_code.assert_called_once_with("JBSWY3DPEHPK3PXP")
        self.page.context.new_page.assert_not_called()
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
        for url in (
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "https://auth.openai.com/workspace",
        ):
            with self.subTest(url=url):
                self.page.url = url
                creds = dict(self.credentials)
                with patch.object(self.browser, "_submit") as submit:
                    self.assertFalse(
                        self.browser._advance(
                            self.page, creds, {"email", "password", "code"}
                        )
                    )
                self.assertEqual(creds, {})
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

    def test_local_codes_match_rfc6238_vectors_including_leading_zeroes(self):
        secret = base64.b32encode(b"12345678901234567890").decode("ascii")
        for timestamp, expected in ((1111111111, "050471"), (1234567890, "005924")):
            with patch.object(automation.time, "time", return_value=timestamp):
                self.assertEqual(self.browser._verification_code(secret), expected)

    def test_cancel_while_waiting_for_a_fresh_code(self):
        with (
            patch.object(automation.time, "time", return_value=58),
            patch.object(self.browser._stop, "wait", return_value=True) as wait,
            self.assertRaises(balances.BalanceError),
        ):
            self.browser._verification_code("JBSWY3DPEHPK3PXP")
        wait.assert_called_once_with(2)

    def test_cancel_after_code_lookup_does_not_submit_it(self):
        self.code.is_visible.return_value = True

        def cancel(*_args):
            self.browser._stop.set()
            return "123456"

        with patch.object(self.browser, "_verification_code", side_effect=cancel):
            self.assertFalse(self.browser._advance(self.page, self.credentials, set()))
        self.code.fill.assert_not_called()
