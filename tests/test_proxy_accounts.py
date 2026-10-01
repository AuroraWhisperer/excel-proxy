"""Offline multi-account credential and routing contracts."""

import base64
import json
from pathlib import Path
import tempfile
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.responses import JSONResponse

from account_balances import BalanceError
import proxy_accounts as accounts
import proxy
import test_excel_contracts


def credential(account_id="account-a", *, expires=None, refresh="refresh-secret"):
    claims = {
        "exp": expires if expires is not None else time.time() + 3600,
        "sub": "user-" + account_id,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "https://api.openai.com/profile": {"email": account_id + "@example.com"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {
        "access_token": "header." + encoded + ".signature",
        "refresh_token": refresh,
    }


class LinkedAccountRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = accounts.ProxyAccountStore()
        self.balances = proxy.account_balances.AccountBalanceStore()
        self.enterContext(patch.object(accounts, "proxy_account_store", self.store))
        self.enterContext(
            patch.object(proxy.account_balances, "balance_store", self.balances)
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.ids = []
        for name in ("account-a", "account-b"):
            payload = credential(name)
            record_id = self.store.import_accounts(payload)["imported_ids"][0]
            self.assertEqual(
                self.balances.import_accounts(payload)["imported_ids"], [record_id]
            )
            self.ids.append(record_id)
        self.active, self.inactive = self.ids
        self.store.activate(
            self.active, self.store.headers_for(self.active)["authorization"]
        )

    async def remove(self, record_id, page):
        if page == "usage":
            return await self.client.post(
                f"/api/account-balances/{record_id}/remove", json={}
            )
        return await self.client.delete(f"/api/proxy-accounts/{record_id}")

    def assert_remaining(self, ids, active):
        for store in (self.store, self.balances):
            self.assertEqual([row["id"] for row in store.snapshot()["accounts"]], ids)
        self.assertEqual(self.store.snapshot()["active_id"], active)
        self.assertEqual(self.store.snapshot()["source"], "oauth" if active else "none")

    async def test_usage_removal_also_removes_saved_login_preserving_active(self):
        response = await self.remove(self.inactive, "usage")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [row["id"] for row in response.json()["accounts"]], [self.active]
        )
        self.assert_remaining([self.active], self.active)

    async def test_login_removal_also_removes_quota_account_preserving_active(self):
        response = await self.remove(self.inactive, "login")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_remaining([self.active], self.active)

    async def test_usage_removal_of_active_stops_proxy_without_fallback(self):
        response = await self.remove(self.active, "usage")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_remaining([self.inactive], None)
        with self.assertRaises(BalanceError):
            await proxy._selected_excel_headers()

    async def test_removal_works_when_only_one_list_contains_account(self):
        self.balances.remove(self.inactive)
        self.assertEqual((await self.remove(self.inactive, "login")).status_code, 200)
        self.store.remove(self.active)
        self.assertEqual((await self.remove(self.active, "usage")).status_code, 200)
        self.assert_remaining([], None)

    async def test_both_pages_refuse_removal_during_activation(self):
        async with proxy._proxy_activation_lock:
            for page in ("usage", "login"):
                response = await self.remove(self.inactive, page)
                self.assertEqual(response.status_code, 409, response.text)
        self.assert_remaining(self.ids, self.active)

    async def test_both_pages_refuse_removal_during_quota_reset(self):
        self.balances._resetting_id = self.active
        for page in ("usage", "login"):
            response = await self.remove(self.active, page)
            self.assertEqual(response.status_code, 409, response.text)
        self.assert_remaining(self.ids, self.active)

    async def test_balance_save_failure_does_not_remove_saved_login(self):
        with patch.object(
            self.balances, "_save", side_effect=BalanceError("save failed", 503)
        ):
            response = await self.remove(self.inactive, "login")
        self.assertEqual(response.status_code, 503, response.text)
        self.assert_remaining(self.ids, self.active)

    async def test_partial_failure_can_be_retried_from_same_page(self):
        with patch.object(
            self.store, "_commit", side_effect=BalanceError("save failed", 503)
        ):
            response = await self.remove(self.inactive, "usage")
        self.assertEqual(response.status_code, 503, response.text)
        response = await self.remove(self.inactive, "usage")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_remaining([self.active], self.active)


class ProxyJsonImportTests(unittest.TestCase):
    def test_supported_envelopes_preserve_nested_refresh_credentials(self):
        token = credential(expires=time.time() + 30)
        exported = {
            "platform": "openai",
            "type": "oauth",
            "credentials": {**token, "id_token": "unused-id-token"},
        }
        for payload in (
            token,
            {"type": "codex", "tokens": token},
            exported,
            [exported],
            {"accounts": [exported], "proxies": [], "x_revive_manifest": {}},
            {"data": {"accounts": [exported]}},
        ):
            with self.subTest(
                envelope=list(payload) if isinstance(payload, dict) else "array"
            ):
                store = accounts.ProxyAccountStore()
                result = store.import_accounts(payload)
                record_id = result["imported_ids"][0]
                self.assertTrue(result["accounts"][0]["renewable"])
                self.assertIsNone(result["active_id"])
                for secret in (*token.values(), "unused-id-token"):
                    self.assertNotIn(secret, json.dumps(result))
                self.assertNotIn("id_token", store._accounts[record_id])
                with patch.object(
                    accounts, "refresh_credentials", return_value=credential()
                ) as refresh:
                    headers = store.headers_for(record_id)
                refresh.assert_called_once_with("refresh-secret")
                self.assertEqual(headers["chatgpt-account-id"], "account-a")

    def test_nested_expiry_and_access_token_only_accounts(self):
        store = accounts.ProxyAccountStore()
        result = store.import_accounts(
            {
                "tokens": {
                    "access_token": "synthetic-opaque-token",
                    "account_id": "account-a",
                    "expires_at": time.time() + 3600,
                }
            }
        )
        record_id = result["imported_ids"][0]
        self.assertFalse(result["accounts"][0]["renewable"])
        self.assertEqual(
            store.headers_for(record_id)["authorization"],
            "Bearer synthetic-opaque-token",
        )

    def test_expired_renewable_import_refreshes_before_use(self):
        store = accounts.ProxyAccountStore()
        result = store.import_accounts(
            {"credentials": credential(expires=time.time() - 30)}
        )
        self.assertTrue(result["accounts"][0]["expired"])
        self.assertTrue(result["accounts"][0]["renewable"])
        replacement = credential(refresh="rotated-import-secret")
        with patch.object(
            accounts, "refresh_credentials", return_value=replacement
        ) as refresh:
            headers = store.headers_for(result["imported_ids"][0])
        refresh.assert_called_once_with("refresh-secret")
        self.assertEqual(
            headers["authorization"], "Bearer " + replacement["access_token"]
        )

    def test_invalid_batches_never_replace_saved_accounts(self):
        store = accounts.ProxyAccountStore()
        first = store.import_accounts(credential())["imported_ids"][0]
        store.activate(first, store.headers_for(first)["authorization"])
        previous = store.snapshot()
        for invalid in (
            {"credentials": {"refresh_token": "only-refresh"}},
            {"platform": "anthropic", "credentials": credential()},
            {"type": "api-key", "credentials": credential()},
            {"credentials": {**credential(), "chatgpt_account_id": "wrong-account"}},
            {"credentials": {**credential(), "refresh_token": "bad\nheader"}},
            {"tokens": credential(expires=time.time() - 30, refresh="")},
        ):
            with self.subTest(fields=list(invalid)):
                with self.assertRaises(BalanceError):
                    store.import_accounts([credential("account-b"), invalid])
                self.assertEqual(store.snapshot(), previous)
        for invalid in (
            [],
            {"accounts": []},
            {"accounts": "invalid"},
            [credential()] * 51,
            [credential(expires=time.time() - 30, refresh=""), credential()],
        ):
            with self.assertRaises(BalanceError):
                store.import_accounts(invalid)
            self.assertEqual(store.snapshot(), previous)

    def test_batch_deduplicates_and_stages_active_replacement(self):
        store = accounts.ProxyAccountStore()
        original = credential()
        first = store.import_accounts(original)["imported_ids"][0]
        store.activate(first, store.headers_for(first)["authorization"])
        replacement = credential(expires=time.time() + 7200)
        result = store.import_accounts(
            {
                "accounts": [
                    {"credentials": replacement},
                    credential("account-b"),
                    credential("account-b"),
                ]
            }
        )
        self.assertEqual(len(result["imported_ids"]), 2)
        self.assertEqual(result["active_id"], first)
        self.assertTrue(result["accounts"][0]["pending"])
        self.assertEqual(
            store.headers_for(first, active=True)["authorization"],
            "Bearer " + original["access_token"],
        )
        candidate = store.headers_for(first)["authorization"]
        self.assertEqual(candidate, "Bearer " + replacement["access_token"])
        store.activate(first, candidate)
        self.assertFalse(store.snapshot()["accounts"][0]["pending"])

    def test_batch_limit_and_save_failure_leave_state_unchanged(self):
        store = accounts.ProxyAccountStore()
        store.import_accounts([credential(f"account-{i}") for i in range(50)])
        previous = store.snapshot()
        with self.assertRaises(BalanceError):
            store.import_accounts([credential("account-0"), credential("account-new")])
        self.assertEqual(store.snapshot(), previous)
        with (
            patch.object(
                store, "_commit", side_effect=BalanceError("save failed", 503)
            ),
            self.assertRaises(BalanceError),
        ):
            store.import_accounts({"tokens": credential("account-0")})
        self.assertEqual(store.snapshot(), previous)


class ProxyAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = accounts.ProxyAccountStore()

    def add(self, payload=None):
        return self.store.import_accounts(payload or credential())["imported_ids"][0]

    def test_multiple_accounts_and_explicit_selection(self):
        first = self.add()
        second = self.add(credential("account-b"))
        self.assertEqual(len(self.store.snapshot()["accounts"]), 2)
        self.assertIsNone(self.store.snapshot()["active_id"])
        self.store.activate(first, self.store.headers_for(first)["authorization"])
        self.assertEqual(self.store.snapshot()["active_id"], first)
        self.store.activate(second, self.store.headers_for(second)["authorization"])
        self.assertEqual(self.store.snapshot()["active_id"], second)
        self.assertEqual(self.store.snapshot()["source"], "oauth")

    def test_public_state_never_exposes_credentials(self):
        payload = credential()
        self.add(payload)
        public = json.dumps(self.store.snapshot())
        for value in (payload["access_token"], payload["refresh_token"]):
            self.assertNotIn(value, public)
        self.assertNotIn("refresh_token", public)

    def test_remove_active_stops_instead_of_falling_back(self):
        first = self.add()
        self.add(credential("account-b"))
        self.store.activate(first, self.store.headers_for(first)["authorization"])
        self.store.remove(first)
        self.assertEqual(self.store.snapshot()["source"], "none")
        self.assertIsNone(self.store.snapshot()["active_id"])

    def test_refresh_rotates_credentials_and_preserves_identity(self):
        first = self.add(credential(expires=time.time() + 30))
        replacement = credential(refresh="rotated-secret")
        with patch.object(
            accounts, "refresh_credentials", return_value=replacement
        ) as refresh:
            headers = self.store.headers_for(first)
            self.store.headers_for(first)
        refresh.assert_called_once_with("refresh-secret")
        self.assertEqual(
            headers["authorization"], "Bearer " + replacement["access_token"]
        )
        self.assertEqual(headers["x-basispoints-auth-mode"], "chatgpt")

    def test_refresh_cannot_change_account(self):
        first = self.add(credential(expires=time.time() + 30))
        with patch.object(
            accounts, "refresh_credentials", return_value=credential("account-b")
        ):
            with self.assertRaises(BalanceError):
                self.store.headers_for(first)
        self.assertEqual(len(self.store.snapshot()["accounts"]), 1)

    def test_changed_credential_cannot_be_activated_after_validation(self):
        first = self.add()
        old = self.store.headers_for(first)["authorization"]
        self.add(credential(expires=time.time() + 7200))
        with self.assertRaises(BalanceError):
            self.store.activate(first, old)
        self.assertIsNone(self.store.snapshot()["active_id"])

    def test_encrypted_persistence_restores_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.dpapi"
            with (
                patch.object(
                    accounts,
                    "_protect_windows_data",
                    side_effect=lambda data: b"protected:" + data,
                ),
                patch.object(
                    accounts,
                    "_unprotect_windows_data",
                    side_effect=lambda data: data.removeprefix(b"protected:"),
                ),
            ):
                store = accounts.ProxyAccountStore(path)
                first = store.import_accounts(credential())["imported_ids"][0]
                store.activate(first, store.headers_for(first)["authorization"])
                self.assertTrue(path.read_bytes().startswith(b"protected:"))
                self.assertEqual(
                    accounts.ProxyAccountStore(path).snapshot()["active_id"], first
                )
                with patch.object(
                    accounts, "_protect_windows_data", side_effect=RuntimeError
                ):
                    with self.assertRaises(BalanceError):
                        store.remove(first)
                self.assertEqual(store.snapshot()["active_id"], first)

    def test_reauthentication_stages_replacement_without_changing_live_headers(self):
        first = self.add()
        original = self.store.headers_for(first)["authorization"]
        self.store.activate(first, original)
        updated = credential(expires=time.time() + 7200)
        self.add(updated)
        self.assertEqual(
            self.store.headers_for(first, active=True)["authorization"], original
        )
        candidate = self.store.headers_for(first)["authorization"]
        self.assertEqual(candidate, "Bearer " + updated["access_token"])
        self.assertTrue(self.store.snapshot()["accounts"][0]["pending"])
        self.store.activate(first, candidate)
        self.assertEqual(
            self.store.headers_for(first, active=True)["authorization"], candidate
        )
        self.assertFalse(self.store.snapshot()["accounts"][0]["pending"])

    def test_refresh_failure_is_visible_and_does_not_retry_revoked_grant(self):
        first = self.add(credential(expires=time.time() + 30))
        with patch.object(
            accounts, "refresh_credentials", side_effect=BalanceError("fixed", 401)
        ) as refresh:
            for _ in range(2):
                with self.assertRaises(BalanceError):
                    self.store.headers_for(first)
        refresh.assert_called_once()
        public = self.store.snapshot()["accounts"][0]
        self.assertTrue(public["needs_login"])
        self.assertTrue(public["expired"])
        self.assertFalse(public["renewable"])

    def test_transient_refresh_failure_does_not_discard_the_grant(self):
        first = self.add(credential(expires=time.time() + 30))
        with patch.object(
            accounts, "refresh_credentials", side_effect=BalanceError("network", 503)
        ):
            with self.assertRaises(BalanceError):
                self.store.headers_for(first)
        self.assertFalse(self.store.snapshot()["accounts"][0]["needs_login"])
        with patch.object(
            accounts, "refresh_credentials", return_value=credential()
        ) as refresh:
            self.store.headers_for(first)
        refresh.assert_called_once_with("refresh-secret")

    def test_refresh_preserves_grant_when_provider_does_not_rotate_it(self):
        first = self.add(credential(expires=time.time() + 30))
        renewed = credential()
        renewed.pop("refresh_token")
        with patch.object(accounts, "refresh_credentials", return_value=renewed):
            self.store.headers_for(first)
        self.assertTrue(self.store.snapshot()["accounts"][0]["renewable"])

    def test_parallel_requests_share_one_token_refresh(self):
        first = self.add(credential(expires=time.time() + 30))
        entered, release = threading.Event(), threading.Event()

        def refresh(_token):
            entered.set()
            self.assertTrue(release.wait(3))
            return credential()

        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(
                accounts, "refresh_credentials", side_effect=refresh
            ) as refresh_call,
        ):
            one = pool.submit(self.store.headers_for, first)
            self.assertTrue(entered.wait(3))
            two = pool.submit(self.store.headers_for, first)
            release.set()
            self.assertEqual(
                one.result()["authorization"], two.result()["authorization"]
            )
        refresh_call.assert_called_once()

    def test_rejected_unexpired_token_refreshes_once_for_concurrent_requests(self):
        first = self.add()
        rejected = self.store.headers_for(first)
        self.store.activate(first, rejected["authorization"])
        replacement = credential(expires=time.time() + 7200, refresh="rotated-secret")
        entered, release = threading.Event(), threading.Event()

        def refresh(_token):
            entered.set()
            self.assertTrue(release.wait(3))
            return replacement

        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(
                accounts, "refresh_credentials", side_effect=refresh
            ) as refresh_call,
        ):
            one = pool.submit(self.store.refresh_after_unauthorized, rejected)
            self.assertTrue(entered.wait(3))
            two = pool.submit(self.store.refresh_after_unauthorized, rejected)
            release.set()
            self.assertEqual(one.result(), two.result())
            self.assertEqual(
                one.result()["authorization"], "Bearer " + replacement["access_token"]
            )
        refresh_call.assert_called_once_with("refresh-secret")
        self.assertEqual(self.store.snapshot()["active_id"], first)

    def test_rejected_token_never_uses_another_account_or_pending_login(self):
        first = self.add()
        rejected = self.store.headers_for(first)
        self.store.activate(first, rejected["authorization"])
        self.add(credential(expires=time.time() + 7200, refresh="pending-secret"))
        with patch.object(
            accounts, "refresh_credentials", return_value=credential()
        ) as refresh:
            self.store.refresh_after_unauthorized(rejected)
        refresh.assert_called_once_with("refresh-secret")
        self.assertTrue(self.store.snapshot()["accounts"][0]["pending"])
        second = self.add(credential("account-b"))
        self.store.activate(second, self.store.headers_for(second)["authorization"])
        with patch.object(accounts, "refresh_credentials") as refresh:
            self.assertIsNone(self.store.refresh_after_unauthorized(rejected))
            self.store.use_excel()
            self.assertIsNone(self.store.refresh_after_unauthorized(rejected))
        refresh.assert_not_called()

    def test_rejected_token_without_refresh_grant_requires_login(self):
        first = self.add(credential(refresh=""))
        rejected = self.store.headers_for(first)
        self.store.activate(first, rejected["authorization"])
        with patch.object(accounts, "refresh_credentials") as refresh:
            with self.assertRaises(BalanceError) as caught:
                self.store.refresh_after_unauthorized(rejected)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertTrue(self.store.snapshot()["accounts"][0]["needs_login"])
        refresh.assert_not_called()

    def test_selection_change_during_forced_refresh_saves_rotation_but_stops_retry(
        self,
    ):
        first = self.add()
        rejected = self.store.headers_for(first)
        self.store.activate(first, rejected["authorization"])
        second = self.add(credential("account-b"))
        second_auth = self.store.headers_for(second)["authorization"]
        replacement = credential(expires=time.time() + 7200)

        def refresh(_token):
            self.store.activate(second, second_auth)
            return replacement

        with patch.object(accounts, "refresh_credentials", side_effect=refresh):
            with self.assertRaises(BalanceError) as caught:
                self.store.refresh_after_unauthorized(rejected)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.store.snapshot()["active_id"], second)
        self.assertEqual(
            self.store.headers_for(first)["authorization"],
            "Bearer " + replacement["access_token"],
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI contract")
    def test_real_dpapi_never_writes_plaintext_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.dpapi"
            store = accounts.ProxyAccountStore(path)
            payload = credential()
            first = store.import_accounts(payload)["imported_ids"][0]
            store.activate(first, store.headers_for(first)["authorization"])
            store.import_accounts(credential(expires=time.time() + 7200))
            data = path.read_bytes()
            self.assertNotIn(payload["access_token"].encode(), data)
            self.assertNotIn(payload["refresh_token"].encode(), data)
            reopened = accounts.ProxyAccountStore(path)
            self.assertEqual(reopened.snapshot()["active_id"], first)
            self.assertTrue(reopened.snapshot()["accounts"][0]["pending"])
            self.assertEqual(
                reopened.headers_for(first, active=True)["authorization"],
                "Bearer " + payload["access_token"],
            )

    def test_corrupt_store_never_falls_back_or_overwrites_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.dpapi"
            path.write_bytes(b"corrupt")
            with patch.object(
                accounts, "_unprotect_windows_data", side_effect=RuntimeError
            ):
                with self.assertRaises(BalanceError):
                    accounts.ProxyAccountStore(path).snapshot()
            self.assertEqual(path.read_bytes(), b"corrupt")


class ProxyAuthenticationHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_excel_contracts.ExcelHTTPContractTests.asyncSetUp(self)
        self.store = accounts.ProxyAccountStore()
        self.patches.enter_context(
            patch.object(accounts, "proxy_account_store", self.store)
        )
        self.first = self.store.import_accounts(credential())["imported_ids"][0]
        self.store.activate(
            self.first, self.store.headers_for(self.first)["authorization"]
        )
        self.responses = []
        self.statuses = [401, 200]

        def respond(request):
            if self.responses:
                self.assertTrue(self.responses[-1].is_closed)
            self.requests.append(request)
            status = self.statuses[min(len(self.requests) - 1, len(self.statuses) - 1)]
            if status == 200:
                payload = {
                    "id": "resp_auth",
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg_auth",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "OK"}],
                        }
                    ],
                }
                response = httpx.Response(
                    200,
                    content=proxy.responses_protocol.sse_encode(
                        "response.completed",
                        {"type": "response.completed", "response": payload},
                    ),
                    headers={"content-type": "text/event-stream"},
                )
            else:
                response = httpx.Response(
                    status, json={"error": {"message": "PRIVATE_TOKEN"}}
                )
            self.responses.append(response)
            return response

        remote = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(remote.aclose)
        self.patches.enter_context(
            patch.object(proxy, "_get_excel_upstream_client", return_value=remote)
        )

    async def request(self, stream):
        return await self.local.post(
            "/v1/responses",
            json={
                "model": proxy.excel_upstream.MODEL_ID,
                "input": "test",
                "stream": stream,
            },
        )

    async def test_oauth_tool_requests_select_the_excel_transport(self):
        def respond(request):
            self.requests.append(request)
            self.assertEqual(
                request.headers.get(
                    "x-openai-internal-basispoints-client-agent-profile"
                ),
                "excel",
            )
            self.assertEqual(
                request.headers.get("x-openai-internal-basispoints-client-editor"),
                "excel",
            )
            self.assertEqual(
                request.headers.get("x-openai-internal-basispoints-client-product"),
                "basispoints-excel-plugin",
            )
            self.assertEqual(request.headers["chatgpt-account-id"], "account-a")
            payload = {
                "id": "resp_profile",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_profile",
                        "call_id": "call_profile",
                        "name": "run_officejs",
                        "status": "completed",
                        "arguments": json.dumps(
                            {
                                "code": json.dumps(
                                    {
                                        "name": "mcp__codex_app.read_thread",
                                        "arguments": {"threadId": "probe-readonly"},
                                    }
                                )
                            }
                        ),
                    }
                ],
            }
            return httpx.Response(
                200,
                content=proxy.responses_protocol.sse_encode(
                    "response.completed",
                    {"type": "response.completed", "response": payload},
                ),
                headers={"content-type": "text/event-stream"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as remote:
            with patch.object(proxy, "_get_excel_upstream_client", return_value=remote):
                for stream in (False, True):
                    with self.subTest(stream=stream):
                        self.requests.clear()
                        result = await self.local.post(
                            "/v1/responses",
                            json={
                                "model": "gpt-6-astra-excel",
                                "input": "Read the saved conversation.",
                                "tools": [
                                    {
                                        "type": "namespace",
                                        "name": "mcp__codex_app",
                                        "tools": [
                                            {
                                                "type": "function",
                                                "name": "read_thread",
                                                "parameters": {
                                                    "type": "object",
                                                    "properties": {
                                                        "threadId": {"type": "string"}
                                                    },
                                                    "required": ["threadId"],
                                                    "additionalProperties": False,
                                                },
                                            }
                                        ],
                                    }
                                ],
                                "stream": stream,
                            },
                        )
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertIn("read_thread", result.text)
                        self.assertNotIn("response.failed", result.text)
                        self.assertNotIn("run_officejs", result.text)
                        self.assertEqual(len(self.requests), 1)

    async def test_401_renews_same_account_and_retries_identical_request_once(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.responses.clear()
                replacement = credential(expires=time.time() + 7200)
                with patch.object(
                    accounts, "refresh_credentials", return_value=replacement
                ) as refresh:
                    result = await self.request(stream)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertIn("OK", result.text)
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(self.requests[0].content, self.requests[1].content)
                self.assertEqual(
                    self.requests[1].headers["authorization"],
                    "Bearer " + replacement["access_token"],
                )
                self.assertTrue(
                    all(
                        req.headers["chatgpt-account-id"] == "account-a"
                        for req in self.requests
                    )
                )
                self.assertTrue(
                    all(
                        req.headers.get(
                            "x-openai-internal-basispoints-client-agent-profile"
                        )
                        == "excel"
                        for req in self.requests
                    )
                )
                self.assertEqual(self.store.snapshot()["active_id"], self.first)
                refresh.assert_called_once()

    async def test_revoked_grant_requires_login_and_does_not_loop(self):
        with patch.object(
            accounts,
            "refresh_credentials",
            side_effect=BalanceError("PRIVATE_TOKEN", 401),
        ) as refresh:
            for stream in (False, True):
                result = await self.request(stream)
                self.assertEqual(result.status_code, 401)
                self.assertNotIn("PRIVATE_TOKEN", result.text)
                self.assertIn("重新登录", result.text)
        refresh.assert_called_once()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.store.snapshot()["accounts"][0]["needs_login"])

    async def test_second_401_is_terminal_and_other_statuses_are_not_replayed(self):
        for status, expected in ((401, 2), (403, 1), (429, 1), (500, 1), (504, 1)):
            for stream in (False, True):
                with self.subTest(status=status, stream=stream):
                    self.requests.clear()
                    self.responses.clear()
                    self.statuses = [status]
                    with patch.object(
                        accounts, "refresh_credentials", return_value=credential()
                    ) as refresh:
                        result = await self.request(stream)
                    self.assertEqual(result.status_code, status)
                    self.assertEqual(len(self.requests), expected)
                    self.assertEqual(refresh.call_count, expected - 1)


class ProxyAccountRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = accounts.ProxyAccountStore()
        self.enterContext(patch.object(accounts, "proxy_account_store", self.store))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.config = self.enterContext(
            patch.object(
                proxy.client_proxy_config_service,
                "enable_target",
                return_value={"configured": True},
            )
        )
        self.balance = self.enterContext(
            patch.object(
                proxy.account_balances.balance_store, "import_accounts", return_value={}
            )
        )
        self.first = self.store.import_accounts(credential())["imported_ids"][0]
        self.second = self.store.import_accounts(credential("account-b"))[
            "imported_ids"
        ][0]
        self.store.activate(
            self.first, self.store.headers_for(self.first)["authorization"]
        )

    async def activate(self):
        return await self.client.post(
            "/api/proxy-accounts/" + self.second + "/activate",
            json={"model": proxy.excel_upstream.MODEL_ID},
        )

    async def test_json_import_can_use_existing_activation_flow(self):
        token = credential("account-imported")
        response = await self.client.post(
            "/api/proxy-accounts/import",
            json={
                "accounts": [
                    {"platform": "openai", "type": "oauth", "credentials": token}
                ]
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        for secret in token.values():
            self.assertNotIn(secret, response.text)
        imported = response.json()["imported_ids"][0]
        self.assertEqual(self.store.snapshot()["active_id"], self.first)
        self.config.assert_not_called()
        self.balance.assert_not_called()
        with patch.object(
            proxy.account_route_dependencies,
            "dispatch_response",
            new_callable=AsyncMock,
            return_value=JSONResponse(
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
            ),
        ) as send:
            response = await self.client.post(
                f"/api/proxy-accounts/{imported}/activate",
                json={"model": proxy.excel_upstream.MODEL_ID},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.snapshot()["active_id"], imported)
        self.assertEqual(
            send.call_args.kwargs["session_headers"]["authorization"],
            "Bearer " + token["access_token"],
        )
        self.config.assert_called_once_with("codex")
        self.assertEqual(
            self.balance.call_args.args[0]["access_token"], token["access_token"]
        )
        self.assertNotIn("refresh_token", self.balance.call_args.args[0])

    async def test_json_import_rejects_unsafe_or_invalid_requests(self):
        previous = self.store.snapshot()
        for kwargs, status in (
            ({"json": credential(), "headers": {"origin": "https://example.com"}}, 403),
            ({"content": "{}", "headers": {"content-type": "text/plain"}}, 415),
            ({"content": "{", "headers": {"content-type": "application/json"}}, 400),
            (
                {
                    "content": b" " * (1024 * 1024 + 1),
                    "headers": {"content-type": "application/json"},
                },
                413,
            ),
            ({"json": {"accounts": [credential("account-c"), {}]}}, 400),
        ):
            response = await self.client.post("/api/proxy-accounts/import", **kwargs)
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(self.store.snapshot(), previous)

    async def test_json_import_is_blocked_during_activation(self):
        previous = self.store.snapshot()
        async with proxy._proxy_activation_lock:
            response = await self.client.post(
                "/api/proxy-accounts/import", json={"tokens": credential("account-c")}
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.snapshot(), previous)

    async def test_activation_verifies_candidate_then_enables_config(self):
        completed = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
        }
        with patch.object(
            proxy.account_route_dependencies,
            "dispatch_response",
            new_callable=AsyncMock,
            return_value=JSONResponse(completed),
        ) as send:
            response = await self.activate()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.snapshot()["active_id"], self.second)
        self.assertEqual(
            send.call_args.kwargs["session_headers"]["chatgpt-account-id"], "account-b"
        )
        self.config.assert_called_once_with("codex")
        self.assertNotIn("refresh_token", self.balance.call_args.args[0])
        self.assertNotIn("Bearer ", response.text)

    async def test_failed_candidate_leaves_current_account_and_config_unchanged(self):
        with patch.object(
            proxy.account_route_dependencies,
            "dispatch_response",
            new_callable=AsyncMock,
            return_value=JSONResponse(
                {"error": {"message": "upstream-secret"}}, status_code=403
            ),
        ):
            response = await self.activate()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.snapshot()["active_id"], self.first)
        self.assertNotIn("upstream-secret", response.text)
        self.config.assert_not_called()

    async def test_duplicate_activation_does_not_send_another_probe(self):
        completed = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
        }
        with patch.object(
            proxy.account_route_dependencies,
            "dispatch_response",
            new_callable=AsyncMock,
            return_value=JSONResponse(completed),
        ) as send:
            self.assertEqual((await self.activate()).status_code, 200)
            self.assertEqual((await self.activate()).status_code, 200)
        send.assert_awaited_once()
        self.config.assert_called_once()

    async def test_http_success_without_complete_text_is_not_activation(self):
        with patch.object(
            proxy.account_route_dependencies,
            "dispatch_response",
            new_callable=AsyncMock,
            return_value=JSONResponse({"status": "completed", "output": []}),
        ):
            response = await self.activate()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.store.snapshot()["active_id"], self.first)

    async def test_failed_relogin_keeps_previous_active_credential(self):
        original = self.store.headers_for(self.first, active=True)["authorization"]
        self.store.import_accounts(credential(expires=time.time() + 7200))
        with patch.object(
            proxy.account_route_dependencies,
            "dispatch_response",
            new_callable=AsyncMock,
            return_value=JSONResponse({}, status_code=401),
        ):
            response = await self.client.post(
                "/api/proxy-accounts/" + self.first + "/activate",
                json={"model": proxy.excel_upstream.MODEL_ID},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            self.store.headers_for(self.first, active=True)["authorization"], original
        )
        self.config.assert_not_called()

    async def test_config_write_failure_is_reported_without_secret_details(self):
        completed = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
        }
        self.config.side_effect = OSError("private-location")
        with patch.object(
            proxy.account_route_dependencies,
            "dispatch_response",
            new_callable=AsyncMock,
            return_value=JSONResponse(completed),
        ):
            response = await self.activate()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["warnings"])
        self.assertNotIn("private-location", response.text)

    async def test_direct_headers_and_status_never_read_excel_cache(self):
        with (
            patch.object(
                proxy.excel_session_capture, "refresh_windows_excel_session"
            ) as windows,
            patch.object(
                proxy.excel_session_capture, "refresh_macos_excel_session"
            ) as mac,
        ):
            headers = await proxy._selected_excel_headers(stream=True)
            status = await self.client.get("/api/config/excel-session")
        self.assertEqual(headers["chatgpt-account-id"], "account-a")
        self.assertEqual(headers["accept"], "text/event-stream")
        self.assertEqual(status.json()["source"], "oauth")
        windows.assert_not_called()
        mac.assert_not_called()

    async def test_remove_active_keeps_proxy_disabled(self):
        result = await self.client.delete("/api/proxy-accounts/" + self.first)
        self.assertEqual(result.status_code, 200)
        with self.assertRaises(BalanceError):
            await proxy._selected_excel_headers()
        status = await self.client.get("/api/config/excel-session")
        self.assertFalse(status.json()["configured"])

    async def test_cross_origin_and_password_submission_are_rejected(self):
        with patch.object(proxy.proxy_login_service, "start") as start:
            response = await self.client.post(
                "/api/proxy-accounts/login/start",
                json={},
                headers={"Origin": "https://evil.example"},
            )
            self.assertEqual(response.status_code, 403)
            response = await self.client.post(
                "/api/proxy-accounts/login/start", json={"password": "not-accepted"}
            )
            self.assertEqual(response.status_code, 400)
            start.assert_not_called()
