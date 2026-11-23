"""Account isolation, bounded waits and cancellation at the HTTP send seam."""

import asyncio
from contextlib import AsyncExitStack
import json
import os
import ssl
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Request

import rate_limiting as limits
import upstream_errors


class GenerationDefaultsTests(unittest.TestCase):
    def test_import_uses_reference_defaults_without_environment_overrides(self):
        environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith("GHCP_GENERATION_")
            and name != "GHCP_UPSTREAM_STARTS_PER_SECOND"
        }
        # Keep the runner's isolated paths and virtualenv; only import settings.
        result = subprocess.run(
            [sys.executable, "-B", "-c", (
                "import json, rate_limiting; "
                "print(json.dumps(rate_limiting.generation_admission.snapshot()))"
            )],
            env=environment, capture_output=True, text=True, timeout=10, check=True,
        )
        snapshot = json.loads(result.stdout)
        self.assertEqual(snapshot["max_active_per_account"], 4)
        self.assertEqual(snapshot["max_pending"], 64)
        self.assertEqual(snapshot["wait_seconds"], 30)
        self.assertEqual(snapshot["max_starts_per_second"], 0)


class OpenBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"complete"


class GenerationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = limits.GenerationAdmission(per_account=1, max_pending=2, wait_seconds=1)
        self.enterContext(patch.object(limits, "generation_admission", self.pool))
        self.pace = self.enterContext(patch.object(limits, "throttle_upstream_request", AsyncMock()))
        self.sent = []
        self.requests = []

        def upstream(request):
            self.sent.append(request.url.path)
            self.requests.append(request)
            return httpx.Response(200, stream=OpenBody())

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.client.aclose)
        self.bodies = await self.enterAsyncContext(AsyncExitStack())

    async def send(self, path, account="private-account", **kwargs):
        request = self.client.build_request(
            "POST", "https://example.test/" + path, headers={"chatgpt-account-id": account}
        )
        response = await limits.throttled_client_send(self.client, request, stream=True, **kwargs)
        self.bodies.push_async_callback(response.aclose)
        return response

    async def test_fifo_full_queue_and_independent_account(self):
        first = await self.send("first")
        second = asyncio.create_task(self.send("second"))
        third = asyncio.create_task(self.send("third"))
        await asyncio.sleep(0)
        self.assertEqual(self.pool.pending, 2)
        with self.assertRaises(limits.GenerationQueueError) as full:
            await self.send("full")
        self.assertEqual(full.exception.code, "excel_queue_full")
        other = await self.send("other", account="other-private-account")
        self.assertEqual(self.sent, ["/first", "/other"])
        self.assertEqual(self.pool.snapshot()["active"], 2)
        self.assertNotIn("private", str(self.pool.snapshot()))
        await other.aclose()
        await first.aclose()  # The stream owner can close before its first read.
        await first.aclose()
        next_body = await second
        self.assertFalse(third.done())
        await next_body.aread()  # EOF also returns the slot.
        await (await third).aclose()
        self.assertEqual(self.sent, ["/first", "/other", "/second", "/third"])
        self.assertEqual(self.pool.snapshot()["active"], 0)
        self.assertEqual(self.pool.pending, 0)

    async def test_cancellation_at_slot_handoff_allows_the_next_waiter(self):
        first = await self.send("holder")
        cancelled = asyncio.create_task(self.send("cancelled"))
        surviving = asyncio.create_task(self.send("surviving"))
        await asyncio.sleep(0)
        await first.aclose()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await (await surviving).aclose()
        self.assertEqual(self.sent, ["/holder", "/surviving"])
        self.assertEqual(self.pool._accounts, {})

    async def test_queue_timeout_and_pacing_timeout_do_not_send(self):
        self.pool.wait_seconds = 0.02
        first = await self.send("holder")
        with self.assertRaises(limits.GenerationQueueError) as timeout:
            await self.send("queued")
        self.assertEqual(timeout.exception.code, "excel_queue_timeout")
        await first.aclose()
        self.pace.side_effect = asyncio.Event().wait
        with self.assertRaises(limits.GenerationQueueError) as paced:
            await self.send("paced")
        self.assertEqual(paced.exception.code, "excel_queue_timeout")
        self.assertEqual(self.sent, ["/holder"])
        self.assertEqual(self.pool._accounts, {})

    async def test_disconnected_json_request_leaves_queue_without_sending(self):
        first = await self.send("holder")
        cancel = asyncio.Event()

        async def receive():
            await cancel.wait()
            return {"type": "http.disconnect"}

        request = Request({"type": "http"}, receive)
        queued = asyncio.create_task(self.send("cancelled", downstream_request=request))
        async with asyncio.timeout(1):
            while not self.pool.pending:
                await asyncio.sleep(0)
            cancel.set()
            with self.assertRaises(upstream_errors.ExcelResponseError) as disconnected:
                await queued
        self.assertEqual(disconnected.exception.status_code, 499)
        self.assertEqual(self.pool.pending, 0)
        self.assertEqual(self.sent, ["/holder"])
        await first.aclose()

    async def test_tls_verification_failure_is_not_retried(self):
        outer = httpx.ConnectError("private proxy address")
        outer.__cause__ = OSError()
        outer.__cause__.__cause__ = ssl.SSLCertVerificationError()
        with patch.object(self.client, "send", AsyncMock(side_effect=outer)) as send:
            with self.assertRaises(httpx.ConnectError):
                await self.send("tls")
        send.assert_awaited_once()
        self.pace.assert_awaited_once()
        self.assertEqual(self.pool._accounts, {})

    async def test_connect_retry_consumes_a_start_but_only_one_slot(self):
        with patch.object(self.client, "send", AsyncMock(side_effect=[
            httpx.ConnectTimeout("unavailable"), httpx.Response(200, content=b"done"),
        ])) as send:
            reply = await self.send("retry")
        self.assertEqual(reply.content, b"done")
        self.assertEqual(send.await_count, 2)
        self.assertEqual(self.pace.await_count, 2)
        self.assertEqual(self.pool._accounts, {})

    async def generate(self, model="gpt-6-astra", **kwargs):
        body = {
            "model": model,
            "input": [{"role": "user", "content": "same prompt"}],
            "metadata": {"task_id": "client-task", "turn_id": "client-turn", "agent_iteration": "1"},
        }
        request = self.client.build_request(
            "POST", "https://example.test/basispoints/api/responses",
            headers={"chatgpt-account-id": "account-a"}, json=body,
            extensions={"excel_body": body, "fixture_extension": "preserved"},
        )
        response = await limits.throttled_client_send(self.client, request, stream=True, **kwargs)
        self.bodies.push_async_callback(response.aclose)
        return response, body

    async def test_generations_reuse_slot_task_but_never_turn(self):
        first_response, first = await self.generate("gpt-5.6-luna")
        await first_response.aclose()
        second_response, second = await self.generate()
        await second_response.aclose()
        self.assertEqual(first["metadata"]["task_id"], second["metadata"]["task_id"])
        self.assertRegex(first["metadata"]["task_id"], r"^[0-9a-f]{64}$")
        self.assertRegex(first["metadata"]["turn_id"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(first["metadata"]["turn_id"], second["metadata"]["turn_id"])
        self.assertEqual([r["model"] for r in (first, second)], ["gpt-5.6-luna", "gpt-6-astra"])
        for request, body in zip(self.requests, (first, second)):
            self.assertEqual(json.loads(request.content), body)
            self.assertEqual(int(request.headers["content-length"]), len(request.content))
            self.assertEqual(request.extensions["fixture_extension"], "preserved")
            self.assertIn("timeout", request.extensions)

    async def test_concurrent_generations_occupy_distinct_task_slots(self):
        self.pool.per_account = 2
        first_response, first = await self.generate()
        second_response, second = await self.generate()
        self.assertNotEqual(first["metadata"]["task_id"], second["metadata"]["task_id"])
        await first_response.aclose()
        third_response, third = await self.generate()
        self.assertEqual(third["metadata"]["task_id"], first["metadata"]["task_id"])
        await second_response.aclose()
        await third_response.aclose()

    async def test_connect_retry_preserves_dispatched_identity(self):
        attempts = []

        async def connect(request, **kwargs):
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                raise httpx.ConnectTimeout("unavailable")
            return httpx.Response(200, content=b"done")

        with patch.object(self.client, "send", side_effect=connect):
            await self.generate()
        self.assertEqual(attempts[0], attempts[1])
        self.assertNotEqual(attempts[0]["metadata"]["turn_id"], "client-turn")
        self.assertEqual(self.pool._accounts, {})
