"""Read-only imported account balances never alter the proxy session."""

import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import account_balances as balances
import proxy


def account(token="test-secret-token", account_id="account-one"):
    return {
        "name": "Test account",
        "platform": "openai",
        "credentials": {
            "access_token": token,
            "chatgpt_account_id": account_id,
            "refresh_token": "do-not-store",
        },
    }


class AccountBalanceTests(unittest.TestCase):
    def setUp(self):
        self.store = balances.AccountBalanceStore()

    def test_import_formats_and_deduplication(self):
        for payload in (
            account(),
            [account()],
            {"accounts": [account()]},
            {"data": {"accounts": [account()]}},
            {"tokens": {"access_token": "new-token", "account_id": "account-one"}},
            {"access_token": "flat-token", "account_id": "account-one"},
        ):
            result = self.store.import_accounts(payload)
            self.assertEqual(len(result["accounts"]), 1)
        self.assertNotIn("token", json.dumps(self.store.snapshot()))

    def test_invalid_batch_is_atomic(self):
        self.store.import_accounts(account())
        for invalid in (
            {"access_token": "bad\nheader"},
            {"platform": "anthropic"},
            {"credentials": {"refresh_token": "only-refresh"}},
        ):
            with self.assertRaises(balances.BalanceError):
                self.store.import_accounts([account(account_id="account-two"), invalid])
            self.assertEqual(len(self.store.snapshot()["accounts"]), 1)

    def test_account_names_use_email_from_every_import_source(self):
        def jwt(claims):
            encoded = (
                base64.urlsafe_b64encode(json.dumps(claims).encode())
                .decode()
                .rstrip("=")
            )
            return f"header.{encoded}.signature"

        email = "member+work@example.com"
        sources = [
            {"email": email},
            {"credentials": {"email": email}},
            {"credentials": {"access_token": jwt({"email": email})}},
            {
                "credentials": {
                    "access_token": jwt(
                        {"https://api.openai.com/profile": {"email": email}}
                    )
                }
            },
            {"credentials": {"id_token": jwt({"email": email})}},
            {"id_token": jwt({"email": email})},
            {"name": email},
        ]
        for source in sources:
            with self.subTest(source=source):
                item = account()
                item.update(
                    {
                        key: value
                        for key, value in source.items()
                        if key != "credentials"
                    }
                )
                item["credentials"].update(source.get("credentials", {}))
                result = balances.AccountBalanceStore().import_accounts(item)
                self.assertEqual(result["accounts"][0]["name"], email)
                self.assertNotIn("id_token", json.dumps(result))

    def test_account_without_email_never_displays_arbitrary_name(self):
        for name in (
            "Imported ChatGPT account",
            "<script>@example.com",
            "not an@email",
            None,
        ):
            with self.subTest(name=name):
                item = account()
                item["name"] = name
                result = balances.AccountBalanceStore().import_accounts(item)
                self.assertEqual(result["accounts"][0]["name"], "邮箱未提供")

    def test_saved_accounts_resolve_email_without_reimport(self):
        claims = {"https://api.openai.com/profile": {"email": "saved@example.com"}}
        token = (
            "header."
            + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            + ".signature"
        )
        saved = {
            "version": 2,
            "accounts": [
                {
                    "name": "导入的 ChatGPT 账号",
                    "access_token": token,
                    "account_id": "saved-account",
                }
            ],
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                balances,
                "_unprotect_windows_data",
                return_value=json.dumps(saved).encode(),
            ),
        ):
            path = Path(directory) / "accounts.dat"
            path.write_bytes(b"encrypted")
            self.assertEqual(
                balances.AccountBalanceStore(path).snapshot()["accounts"][0]["name"],
                "saved@example.com",
            )

    def test_shared_workspace_users_are_distinct_and_jwt_hints_are_not_exposed(self):
        def jwt(user):
            claims = {
                "sub": user,
                "https://api.openai.com/auth": {"chatgpt_account_id": "workspace"},
            }
            return (
                "header."
                + base64.urlsafe_b64encode(json.dumps(claims).encode())
                .decode()
                .rstrip("=")
                + ".signature"
            )

        result = self.store.import_accounts(
            [{"access_token": jwt("user-one")}, {"access_token": jwt("user-two")}]
        )
        self.assertEqual(len(result["accounts"]), 2)
        self.assertNotIn("user-one", json.dumps(result))
        self.assertNotIn("signature", json.dumps(result))

    def test_limits_and_invalid_conversion(self):
        with self.assertRaises(balances.BalanceError):
            self.store.import_accounts([account()] * 51)
        for value in (True, -1, 0, "NaN", "Infinity", {}, 1e100):
            with self.subTest(value=value), self.assertRaises(balances.BalanceError):
                self.store.set_rate(value)
        self.assertIsNone(self.store.snapshot()["credit_unit_usd"])

    def test_normalization_distinguishes_unknown_zero_and_unlimited(self):
        self.assertIsNone(balances.normalize_usage({})["balance"])
        self.assertEqual(
            balances.normalize_usage({"credits": {"balance": "0"}})["balance"], 0
        )
        self.assertIsNone(
            balances.normalize_usage({"credits": {"balance": "NaN"}})["balance"]
        )
        self.assertIsNone(
            balances.normalize_usage({"credits": {"balance": True}})["balance"]
        )
        self.assertTrue(
            balances.normalize_usage({"credits": {"unlimited": True}})["unlimited"]
        )

    def test_reset_cards_only_expose_count_and_expiration(self):
        result = balances.normalize_reset_credits(
            {
                "credits": [
                    {
                        "id": "secret-id",
                        "status": "available",
                        "reset_type": "codex_rate_limits",
                        "expires_at": "2099-01-01T00:00:00Z",
                    },
                    {"status": "consumed"},
                    {"reset_type": "other"},
                ]
            }
        )
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(result["expires_at"], ["2099-01-01T00:00:00+00:00"])
        self.assertNotIn("secret-id", json.dumps(result))
        self.assertIsNone(balances.normalize_reset_credits({})["available_count"])
        self.assertEqual(balances.normalize_reset_credits([])["available_count"], 0)
        self.assertIsNone(
            balances.normalize_reset_credits(["invalid"])["available_count"]
        )

    def test_reset_expiration_and_invalid_windows(self):
        result = balances.normalize_reset_credits(
            [
                {"expiresAt": "2000-01-01T00:00:00Z"},
                {"expiresAt": "2099-01-01T00:00:00Z", "resetType": "codex_rate_limits"},
            ]
        )
        self.assertEqual(result["available_count"], 1)
        quota = balances.normalize_usage(
            {
                "rate_limit": {
                    "primary_window": {"used_percent": 101},
                    "secondary_window": {"used_percent": 0, "reset_after_seconds": 30},
                }
            }
        )
        self.assertIsNone(quota["windows"][0]["remaining_percent"])
        self.assertEqual(quota["windows"][1]["remaining_percent"], 100)
        self.assertIsNotNone(quota["windows"][1]["resets_at"])

    def test_refresh_uses_only_fixed_get_endpoints_and_caches(self):
        record_id = self.store.import_accounts(account())["accounts"][0]["id"]
        urls = []

        def handle(request):
            urls.append(str(request.url))
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["chatgpt-account-id"], "account-one")
            return httpx.Response(
                200,
                json={"credits": {"balance": "125.5"}}
                if request.url.path.endswith("/usage")
                else {"available_count": 2},
            )

        with patch.object(
            balances,
            "_client",
            side_effect=lambda: httpx.Client(transport=httpx.MockTransport(handle)),
        ):
            result = self.store.refresh(record_id)
            self.store.refresh(record_id)
        self.assertEqual(urls, [balances.USAGE_URL, balances.RESET_CREDITS_URL])
        self.assertEqual(result["accounts"][0]["quota"]["balance"], 125.5)
        self.assertIsNone(result["accounts"][0]["balance_usd"])
        result = self.store.set_rate("0.04")
        self.assertEqual(result["accounts"][0]["balance_usd"], 5.02)
        self.assertNotIn("test-secret", json.dumps(result))

    def test_failed_refresh_preserves_but_marks_old_snapshot(self):
        record_id = self.store.import_accounts(account())["accounts"][0]["id"]
        with patch.object(
            balances, "fetch_quota", return_value={"balance": 10, "unlimited": False}
        ):
            self.store.refresh(record_id)
        with (
            patch.object(balances.time, "monotonic", return_value=1e20),
            patch.object(
                balances,
                "fetch_quota",
                side_effect=balances.BalanceError("登录已过期，请重新导入。"),
            ),
        ):
            row = self.store.refresh(record_id)["accounts"][0]
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["stale"])
        self.assertEqual(row["quota"]["balance"], 10)
        self.assertIsNotNone(row["checked_at"])
        self.assertIsNone(
            self.store.import_accounts(account(token="replacement"))["accounts"][0][
                "quota"
            ]
        )

    def test_partial_reset_error_keeps_balance_but_unknown_card_count(self):
        def handle(request):
            return (
                httpx.Response(200, json={"credits": {"balance": "12"}})
                if request.url.path.endswith("/usage")
                else httpx.Response(403, text="secret response")
            )

        with patch.object(
            balances,
            "_client",
            side_effect=lambda: httpx.Client(transport=httpx.MockTransport(handle)),
        ):
            result = balances.fetch_quota(
                {"access_token": "secret", "account_id": "one"}
            )
        self.assertEqual(result["balance"], 12)
        self.assertIsNone(result["reset_credits"]["available_count"])
        self.assertIn("重置卡", result["warning"])
        self.assertNotIn("secret", json.dumps(result))

    def test_upstream_failures_never_echo_raw_bodies_or_follow_redirects(self):
        for status in (302, 401, 403, 429, 500):
            requests = []

            def handle(request):
                requests.append(request)
                return httpx.Response(
                    status,
                    text="secret-response-body",
                    headers={"Location": "https://evil.example"},
                )

            with patch.object(
                balances,
                "_client",
                side_effect=lambda: httpx.Client(transport=httpx.MockTransport(handle)),
            ):
                with self.assertRaises(balances.BalanceError) as error:
                    balances.fetch_quota(
                        {"access_token": "secret-token", "account_id": "one"}
                    )
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(len(requests), 1)
        with balances._client() as client:
            self.assertFalse(client.follow_redirects)

    def test_network_bad_json_and_account_mismatch_fail_safely(self):
        for response in (
            httpx.Response(200, text="not-json-secret"),
            httpx.Response(200, content=b" " * (balances.MAX_BYTES + 1)),
            httpx.Response(200, json={"account_id": "wrong-account"}),
        ):
            with patch.object(
                balances,
                "_client",
                side_effect=lambda: httpx.Client(
                    transport=httpx.MockTransport(lambda request: response)
                ),
            ):
                with self.assertRaises(balances.BalanceError) as error:
                    balances.fetch_quota(
                        {"access_token": "secret-token", "account_id": "one"}
                    )
            self.assertNotIn("secret", str(error.exception))

        def timeout(request):
            raise httpx.ReadTimeout("secret timeout")

        with patch.object(
            balances,
            "_client",
            side_effect=lambda: httpx.Client(transport=httpx.MockTransport(timeout)),
        ):
            with self.assertRaises(balances.BalanceError) as error:
                balances.fetch_quota(
                    {"access_token": "secret-token", "account_id": "one"}
                )
        self.assertNotIn("secret", str(error.exception))

    def test_reimport_during_query_does_not_restore_old_balance(self):
        record_id = self.store.import_accounts(account())["accounts"][0]["id"]
        entered, release = threading.Event(), threading.Event()

        def query(value):
            entered.set()
            release.wait(2)
            return {"balance": 999}

        with patch.object(balances, "fetch_quota", side_effect=query) as fetch:
            thread = threading.Thread(target=self.store.refresh, args=(record_id,))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(balances.BalanceError):
                    self.store.refresh(record_id)
                self.store.import_accounts(account(token="new-secret"))
            finally:
                release.set()
                thread.join(3)
            fetch.assert_called_once()
        self.assertIsNone(self.store.snapshot()["accounts"][0]["quota"])

    def test_failed_write_keeps_previous_credentials(self):
        self.store.import_accounts(account())
        with patch.object(
            self.store, "_save", side_effect=balances.BalanceError("账号未保存")
        ):
            with self.assertRaises(balances.BalanceError):
                self.store.import_accounts(account(account_id="two"))
        self.assertEqual(len(self.store.snapshot()["accounts"]), 1)

    def test_persistence_is_encrypted_atomic_and_separate(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                balances,
                "_protect_windows_data",
                side_effect=lambda raw: b"encrypted:" + raw[::-1],
            ),
            patch.object(
                balances,
                "_unprotect_windows_data",
                side_effect=lambda raw: raw[10:][::-1],
            ),
        ):
            path = Path(directory) / "accounts.bin"
            store = balances.AccountBalanceStore(path)
            record_id = store.import_accounts(account())["accounts"][0]["id"]
            self.assertNotIn(b"test-secret-token", path.read_bytes())
            self.assertNotIn(b"do-not-store", path.read_bytes()[10:][::-1])
            loaded = balances.AccountBalanceStore(path)
            self.assertEqual(loaded.snapshot()["accounts"][0]["id"], record_id)
            loaded.remove(record_id)
            self.assertEqual(
                balances.AccountBalanceStore(path).snapshot()["accounts"], []
            )


class AccountBalanceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.client.aclose)
        patcher = patch.object(
            balances, "balance_store", balances.AccountBalanceStore()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_connection_uses_clicked_account_without_switching(self):
        first, second = [
            balances.balance_store.import_accounts(account(token, identity))[
                "imported_ids"
            ][0]
            for token, identity in (
                ("first-secret", "first"),
                ("second-secret", "second"),
            )
        ]
        reply = proxy.JSONResponse(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
            }
        )
        with (
            patch.object(
                proxy, "_handle_excel_responses", AsyncMock(return_value=reply)
            ) as send,
            patch.object(
                proxy.proxy_accounts.proxy_account_store, "activate"
            ) as activate,
            patch.object(
                proxy.excel_upstream.excel_session_store, "configure"
            ) as configure,
        ):
            response = await self.client.post(
                f"/api/account-balances/{second}/test", json={}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["status_code"], 200)
        self.assertEqual(response.json()["response_text"], "OK")
        self.assertGreaterEqual(response.json()["elapsed_ms"], 0)
        self.assertEqual(response.headers["cache-control"], "no-store")
        headers = send.call_args.kwargs["session_headers"]
        self.assertEqual(headers["authorization"], "Bearer second-secret")
        self.assertEqual(headers["chatgpt-account-id"], "second")
        self.assertEqual(
            headers["x-openai-internal-basispoints-client-agent-profile"], "excel"
        )
        self.assertEqual(send.call_args.args[1]["input"], "Reply with exactly OK.")
        self.assertEqual(send.call_args.args[1]["tool_choice"], "none")
        self.assertNotIn("secret", response.text)
        activate.assert_not_called()
        configure.assert_not_called()
        self.assertEqual(len(balances.balance_store.snapshot()["accounts"]), 2)

    async def test_connection_preserves_http_failures_without_leaking_upstream_body(
        self,
    ):
        record_id = balances.balance_store.import_accounts(account())["imported_ids"][0]
        for status in (401, 403, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                reply = proxy.JSONResponse(
                    {
                        "error": {
                            "code": "excel_upstream_error",
                            "message": "private-secret",
                        }
                    },
                    status_code=status,
                )
                with patch.object(
                    proxy, "_handle_excel_responses", AsyncMock(return_value=reply)
                ):
                    response = await self.client.post(
                        f"/api/account-balances/{record_id}/test", json={}
                    )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["status_code"], status)
                self.assertFalse(response.json()["ok"])
                self.assertNotIn("private-secret", response.text)
                self.assertNotIn("response_text", response.json())

    async def test_connection_rejects_missing_accounts_and_unsafe_requests(self):
        with patch.object(proxy, "_handle_excel_responses", AsyncMock()) as send:
            for headers, body, expected in (
                ({}, "{}", 404),
                ({"Origin": "https://evil.example"}, "{}", 403),
                ({"Host": "evil.example"}, "{}", 403),
                ({"Content-Type": "text/plain"}, "{}", 415),
                ({}, "[]", 400),
                ({}, '{"model":"unknown"}', 400),
            ):
                with self.subTest(headers=headers, body=body):
                    response = await self.client.post(
                        "/api/account-balances/missing/test",
                        content=body,
                        headers={"Content-Type": "application/json", **headers},
                    )
                    self.assertEqual(response.status_code, expected)
            send.assert_not_called()

    async def test_connection_times_out_and_rejects_empty_completed_response(self):
        record_id = balances.balance_store.import_accounts(account())["imported_ids"][0]
        with patch.object(
            proxy, "_handle_excel_responses", AsyncMock(side_effect=TimeoutError)
        ):
            response = await self.client.post(
                f"/api/account-balances/{record_id}/test", json={}
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["category"], "timeout")
        self.assertFalse(proxy._excel_connection_test_lock.locked())
        with patch.object(
            proxy,
            "_handle_excel_responses",
            AsyncMock(
                return_value=proxy.JSONResponse({"status": "completed", "output": []})
            ),
        ):
            response = await self.client.post(
                f"/api/account-balances/{record_id}/test", json={}
            )
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["ok"])

    async def test_connection_does_not_send_while_another_test_is_running(self):
        record_id = balances.balance_store.import_accounts(account())["imported_ids"][0]
        with patch.object(proxy, "_handle_excel_responses", AsyncMock()) as send:
            async with proxy._excel_connection_test_lock:
                response = await self.client.post(
                    f"/api/account-balances/{record_id}/test", json={}
                )
            self.assertEqual(response.status_code, 429)
            send.assert_not_called()

    async def test_import_get_rate_and_remove(self):
        with patch.object(
            proxy.excel_upstream.excel_session_store, "configure"
        ) as configure:
            response = await self.client.post(
                "/api/account-balances/import", json=account()
            )
            configure.assert_not_called()
        self.assertEqual(response.status_code, 200)
        record_id = response.json()["accounts"][0]["id"]
        self.assertNotIn("test-secret", response.text)
        response = await self.client.get("/api/account-balances")
        self.assertEqual(response.headers["cache-control"], "no-store")
        response = await self.client.post(
            "/api/account-balances/rate", json={"credit_unit_usd": 0.04}
        )
        self.assertEqual(response.json()["credit_unit_usd"], 0.04)
        response = await self.client.post(
            f"/api/account-balances/{record_id}/remove", json={}
        )
        self.assertEqual(response.json()["accounts"], [])
        response = await self.client.post(
            f"/api/account-balances/{record_id}/refresh", json={}
        )
        self.assertEqual(response.status_code, 404)

    async def test_refresh_action_works_and_secret_fields_stay_server_side(self):
        response = await self.client.post(
            "/api/account-balances/import", json=account()
        )
        record_id = response.json()["accounts"][0]["id"]
        with patch.object(
            balances,
            "fetch_quota",
            return_value=balances.normalize_usage({"credits": {"balance": "25"}}),
        ) as fetch:
            response = await self.client.post(
                f"/api/account-balances/{record_id}/refresh", json={}
            )
            fetch.assert_called_once()
        self.assertEqual(response.json()["accounts"][0]["quota"]["balance"], 25)
        self.assertNotIn("test-secret", response.text)

    async def test_security_and_size_limits(self):
        for headers, expected in (
            ({"Origin": "https://evil.example"}, 403),
            ({"Content-Type": "text/plain"}, 415),
        ):
            response = await self.client.post(
                "/api/account-balances/import",
                content=json.dumps(account()),
                headers=headers,
            )
            self.assertEqual(response.status_code, expected)
        response = await self.client.post(
            "/api/account-balances/import",
            content=b" " * (1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        response = await self.client.post(
            "/api/account-balances/import",
            content="{invalid",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        with patch.object(balances, "fetch_quota") as fetch:
            await self.client.get("/api/account-balances")
            fetch.assert_not_called()
