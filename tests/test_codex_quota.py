"""Offline quota normalization, subprocess lifecycle, and HTTP boundaries."""

import io
import json
import sys
import unittest
from unittest.mock import Mock, patch

import httpx

import codex_quota
import proxy


def limits():
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "limitName": "Codex",
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 300,
                    "resetsAt": 2000000000,
                },
                "secondary": {
                    "usedPercent": 0,
                    "windowDurationMins": 10080,
                    "resetsAt": 2000500000,
                },
                "credits": {
                    "balance": "12.5",
                    "unlimited": False,
                    "resetCreditIds": ["secret"],
                },
            }
        }
    }


class QuotaNormalizationTests(unittest.TestCase):
    def test_official_windows_zero_and_credits_are_preserved(self):
        result = codex_quota.normalize_quota(limits(), "pro")
        self.assertEqual(
            [row["remaining_percent"] for row in result["windows"]], [75, 100]
        )
        self.assertEqual(result["credits"], {"balance": 12.5, "unlimited": False})
        self.assertNotIn("secret", json.dumps(result))

    def test_legacy_payload_and_missing_windows(self):
        legacy = limits()["rateLimitsByLimitId"]["codex"]
        del legacy["primary"]
        result = codex_quota.normalize_quota({"rateLimits": legacy})
        self.assertIsNone(result["windows"][0]["remaining_percent"])
        self.assertEqual(result["windows"][1]["remaining_percent"], 100)

    def test_malformed_percentages_never_become_zero_or_unlimited(self):
        for value in (-1, 101, float("nan"), float("inf"), True, "25", None):
            payload = limits()
            payload["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = value
            self.assertIsNone(
                codex_quota.normalize_quota(payload)["windows"][0]["remaining_percent"]
            )

    def test_spark_does_not_fill_missing_codex_windows(self):
        payload = {
            "rateLimitsByLimitId": {"spark": limits()["rateLimitsByLimitId"]["codex"]}
        }
        result = codex_quota.normalize_quota(payload)
        self.assertTrue(all(row["limit_id"] == "spark" for row in result["windows"]))
        self.assertIsNone(result["credits"])
        self.assertEqual(codex_quota.normalize_quota({})["windows"], [])

    def test_expired_reset_remains_expired_not_replaced_with_current_window(self):
        payload = limits()
        payload["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"] = 1
        self.assertEqual(
            codex_quota.normalize_quota(payload)["windows"][0]["resets_at"], 1
        )


class QuotaProtocolTests(unittest.TestCase):
    def run_rpc(self, messages):
        self.input = io.StringIO()
        self.output = io.StringIO(
            "".join(json.dumps(item) + chr(10) for item in messages)
        )
        # Keep a copy for assertions after the production cleanup closes the pipe.
        self.sent = []
        original_write = self.input.write
        self.input.write = lambda value: (
            self.sent.append(json.loads(value)),
            original_write(value),
        )[1]
        self.child = Mock(stdin=self.input, stdout=self.output)
        self.child.poll.return_value = None
        with (
            patch.object(
                codex_quota, "find_codex_executable", return_value="codex.exe"
            ),
            patch.object(
                codex_quota.subprocess, "Popen", return_value=self.child
            ) as popen,
        ):
            result = codex_quota.read_account_quota()
            self.assertEqual(popen.call_args.args[0], ["codex.exe", "app-server"])
            self.assertNotIn("shell", popen.call_args.kwargs)
            if sys.platform == "win32":
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    codex_quota.subprocess.CREATE_NO_WINDOW,
                )
            return result

    def replies(self, account=None):
        return [
            {"id": 1, "result": {}},
            {
                "id": 2,
                "result": {
                    "account": account
                    or {
                        "type": "chatgpt",
                        "planType": "pro",
                        "email": "private@example.test",
                    }
                },
            },
            {"id": 3, "result": limits()},
        ]

    def test_only_readonly_methods_are_sent_and_child_is_cleaned_up(self):
        result = self.run_rpc(self.replies())
        self.assertEqual(
            [item["method"] for item in self.sent],
            ["initialize", "initialized", "account/read", "account/rateLimits/read"],
        )
        self.assertEqual(self.sent[2]["params"], {"refreshToken": False})
        self.assertNotIn("private@", json.dumps(result))
        self.child.kill.assert_called_once()
        self.child.wait.assert_called_once()
        self.assertTrue(self.input.closed and self.output.closed)

    def test_server_requests_are_rejected_not_executed(self):
        messages = self.replies()
        messages.insert(
            0, {"id": "approval", "method": "command/execute", "params": {}}
        )
        self.run_rpc(messages)
        self.assertEqual(self.sent[1]["error"]["code"], -32601)

    def test_not_logged_in_does_not_query_limits(self):
        with self.assertRaisesRegex(codex_quota.QuotaError, "未使用 ChatGPT"):
            self.run_rpc(self.replies({"type": "apiKey"}))
        self.assertNotIn(
            "account/rateLimits/read", [item["method"] for item in self.sent]
        )
        self.child.kill.assert_called_once()

    def test_upstream_errors_are_sanitized_and_process_is_cleaned_up(self):
        with self.assertRaises(codex_quota.QuotaError) as caught:
            self.run_rpc([{"id": 1, "error": {"message": "Bearer sensitive-token"}}])
        self.assertNotIn("sensitive-token", str(caught.exception))
        self.child.kill.assert_called_once()

    def test_broken_pipe_cleanup_preserves_safe_error(self):
        child = Mock(stdin=Mock(), stdout=io.StringIO())
        child.poll.return_value = None
        child.stdin.write.side_effect = BrokenPipeError("private-pipe-details")
        child.stdin.close.side_effect = BrokenPipeError("private-pipe-details")
        with (
            patch.object(
                codex_quota, "find_codex_executable", return_value="codex.exe"
            ),
            patch.object(codex_quota.subprocess, "Popen", return_value=child),
        ):
            with self.assertRaisesRegex(codex_quota.QuotaError, "连接中断"):
                codex_quota.read_account_quota()
        child.kill.assert_called_once()
        self.assertTrue(child.stdout.closed)

    def test_timeout_and_early_exit_are_cleaned_up(self):
        with patch.object(codex_quota, "QUERY_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(codex_quota.QuotaError, "超时"):
                self.run_rpc(self.replies())
        self.child.kill.assert_called_once()
        with self.assertRaisesRegex(codex_quota.QuotaError, "已退出"):
            self.run_rpc([])
        self.child.kill.assert_called_once()


class QuotaCacheTests(unittest.TestCase):
    def test_get_never_queries_and_refresh_is_cached(self):
        service = codex_quota.QuotaService()
        with patch.object(
            codex_quota,
            "read_account_quota",
            return_value=codex_quota.normalize_quota(limits()),
        ) as read:
            self.assertEqual(service.snapshot()["status"], "not_checked")
            read.assert_not_called()
            first = service.refresh()
            second = service.refresh()
            self.assertEqual(first, second)
            read.assert_called_once()
            self.assertFalse(first["checking"])

    def test_failed_refresh_does_not_show_previous_account_balance(self):
        service = codex_quota.QuotaService()
        with (
            patch.object(codex_quota, "CACHE_SECONDS", 0),
            patch.object(
                codex_quota,
                "read_account_quota",
                side_effect=[
                    codex_quota.normalize_quota(limits()),
                    codex_quota.QuotaError("登录已失效"),
                ],
            ),
        ):
            self.assertTrue(service.refresh()["windows"])
            failed = service.refresh()
            self.assertEqual(failed["status"], "error")
            self.assertEqual(failed["windows"], [])
            self.assertNotIn("credits", failed)

    def test_concurrent_refresh_does_not_spawn_second_process(self):
        service = codex_quota.QuotaService()
        with (
            service._refresh_lock,
            patch.object(codex_quota, "read_account_quota") as read,
        ):
            self.assertTrue(service.refresh()["checking"])
            read.assert_not_called()


class QuotaRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.service = codex_quota.QuotaService()
        patcher = patch.object(codex_quota, "quota_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_get_only_reads_memory_and_post_refreshes(self):
        with patch.object(
            codex_quota,
            "read_account_quota",
            return_value=codex_quota.normalize_quota(limits()),
        ) as read:
            response = await self.client.get("/api/account-quota")
            self.assertEqual(response.json()["status"], "not_checked")
            read.assert_not_called()
            response = await self.client.post("/api/account-quota", json={})
            self.assertEqual(response.json()["status"], "ready")
            self.assertEqual(response.headers["cache-control"], "no-store")
            read.assert_called_once()

    async def test_cross_origin_host_and_form_requests_never_start_query(self):
        with patch.object(codex_quota, "read_account_quota") as read:
            for headers in (
                {"Origin": "https://evil.example"},
                {"Host": "evil.example"},
            ):
                response = await self.client.post(
                    "/api/account-quota", json={}, headers=headers
                )
                self.assertEqual(response.status_code, 403)
                response = await self.client.get("/api/account-quota", headers=headers)
                self.assertEqual(response.status_code, 403)
            response = await self.client.post(
                "/api/account-quota", data={"refresh": "true"}
            )
            self.assertEqual(response.status_code, 415)
            read.assert_not_called()
