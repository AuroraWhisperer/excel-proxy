"""Exercise the stream owner before a response body can take over cleanup."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import rate_limiting
import responses_stream as streams
from test_stream_ownership import plan
from usage_tracking import SSEUsageCapture


class Source(httpx.AsyncByteStream):
    def __init__(self, error=None):
        self.error = error
        self.closed = 0

    async def __aiter__(self):
        if self.error is not None:
            raise self.error
        yield b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'

    async def aclose(self):
        self.closed += 1


class StreamEstablishmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(streams, "_ACTIVE_RESPONSES_STREAMS", {}))
        self.enterContext(
            patch.object(rate_limiting, "throttle_upstream_request", AsyncMock())
        )
        self.request_plan = plan("establishment")
        self.finish = Mock()
        self.tracker = Mock(create_sse_capture=Mock(side_effect=SSEUsageCapture))
        self.dependencies = streams.StreamDependencies(self.tracker, self.finish)
        self.source = Source()
        self.upstream = httpx.Response(
            200, stream=self.source, extensions={"http_version": b"HTTP/1.1"}
        )
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: self.upstream)
        )
        self.addAsyncCleanup(self.client.aclose)
        self.get_client = Mock(return_value=self.client)

        def handle_error(upstream, *, trace_plan):
            self.finish(trace_plan, upstream.status_code, upstream=upstream)
            return JSONResponse({"error": "rejected"}, status_code=upstream.status_code)

        self.handle_error = Mock(side_effect=handle_error)

    async def relay(self, **kwargs):
        return await streams.relay_streaming_response(
            self.request_plan.upstream_url,
            {},
            {},
            dependencies=self.dependencies,
            get_upstream_client=self.get_client,
            handle_upstream_error=self.handle_error,
            trace_plan=self.request_plan,
            **kwargs,
        )

    def assert_finished(self, status):
        self.finish.assert_called_once()
        self.assertEqual(self.finish.call_args.args[1], status)
        self.assertEqual(streams._ACTIVE_RESPONSES_STREAMS, {})

    async def test_success_hands_cleanup_to_body_once(self):
        response = await self.relay()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.finish.assert_not_called()
        self.assertTrue(streams._ACTIVE_RESPONSES_STREAMS)
        async for _ in response.body_iterator:
            pass
        await response.body_iterator.aclose()
        self.assert_finished(200)
        self.assertEqual(self.source.closed, 1)

    async def test_supersession_blocks_before_client_creation(self):
        with patch.object(
            streams,
            "_supersede_active_responses_streams",
            AsyncMock(
                side_effect=streams._ResponsesSupersessionBlocked([]),
            ),
        ):
            response = await self.relay()
        self.assertEqual(response.status_code, 409)
        self.get_client.assert_not_called()
        self.assert_finished(409)

    async def test_connection_and_read_errors_keep_distinct_statuses(self):
        for error, status in (
            (httpx.ConnectError("private"), 502),
            (httpx.ReadTimeout("private"), 504),
        ):
            with self.subTest(error=type(error).__name__):
                self.finish.reset_mock()
                with patch.object(self.client, "send", AsyncMock(side_effect=error)):
                    response = await self.relay()
                self.assertEqual(response.status_code, status)
                self.assertNotIn(b"private", response.body)
                self.assert_finished(status)
                self.assertIs(self.finish.call_args.kwargs["error"], error)

    async def test_client_setup_error_finishes_and_unregisters(self):
        self.get_client.side_effect = ValueError("setup")
        with self.assertRaisesRegex(ValueError, "setup"):
            await self.relay()
        self.assert_finished(599)

    async def test_upstream_http_error_is_not_wrapped_in_successful_sse(self):
        self.upstream = httpx.Response(401, stream=self.source)
        response = await self.relay()
        self.assertEqual(response.status_code, 401)
        self.handle_error.assert_called_once_with(
            self.upstream, trace_plan=self.request_plan
        )
        self.assert_finished(401)
        self.assertEqual(self.source.closed, 1)

    async def test_error_body_read_failure_closes_and_records_diagnosis(self):
        self.source = Source(httpx.ReadError("private"))
        self.upstream = httpx.Response(500, stream=self.source)
        response = await self.relay()
        self.assertEqual(response.status_code, 502)
        self.handle_error.assert_not_called()
        self.assert_finished(502)
        self.assertEqual(self.source.closed, 1)
        lifecycle = self.request_plan.trace_context["responses_stream_lifecycle"]
        self.assertEqual(lifecycle["termination_cause"], "upstream_error_body_read")
        self.assertEqual(lifecycle["upstream_error_type"], "ReadError")

    async def test_error_body_cancellation_closes_and_propagates(self):
        self.source = Source(asyncio.CancelledError())
        self.upstream = httpx.Response(500, stream=self.source)
        with self.assertRaises(asyncio.CancelledError):
            await self.relay()
        self.assert_finished(499)
        self.assertEqual(self.source.closed, 1)
        self.assertEqual(
            self.request_plan.trace_context["responses_stream_lifecycle"][
                "termination_cause"
            ],
            "upstream_error_body_cancelled",
        )

    async def test_transform_setup_failure_closes_before_body_ownership(self):
        with self.assertRaisesRegex(ValueError, "transform"):
            await self.relay(
                stream_transform_factory=Mock(side_effect=ValueError("transform"))
            )
        self.assert_finished(599)
        self.assertEqual(self.source.closed, 1)

    async def test_disconnect_waits_for_headers_then_closes_wire_stream(self):
        started, disconnected = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            started.set()
            await disconnected.wait()
            return self.upstream

        async def receive():
            await started.wait()
            disconnected.set()
            return {"type": "http.disconnect"}

        downstream = Request({"type": "http"}, receive)
        with patch.object(self.client, "send", send):
            response = await asyncio.wait_for(
                self.relay(downstream_request=downstream), 1
            )
        self.assertEqual(response.status_code, 499)
        self.assert_finished(499)
        self.assertEqual(self.source.closed, 1)
        lifecycle = self.request_plan.trace_context["responses_stream_lifecycle"]
        self.assertEqual(
            lifecycle["termination_cause"], "downstream_disconnected_before_response"
        )
        self.assertTrue(lifecycle["transport_cancel_confirmed"])

    async def test_task_cancellation_keeps_send_alive_until_transport_can_close(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            started.set()
            await release.wait()
            return self.upstream

        with patch.object(self.client, "send", send):
            task = asyncio.create_task(self.relay())
            try:
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_finished(499)
        self.assertEqual(self.source.closed, 1)
