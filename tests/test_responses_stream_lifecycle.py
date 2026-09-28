"""Record stream outcomes from terminal events, not iterator exhaustion."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import responses_protocol
import proxy
import responses_stream as streams


def frame(event, **payload):
    return responses_protocol.sse_encode(event, {"type": event, **payload})


class ResponsesStreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_stream(self, *chunks, transform=None):
        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in chunks:
                    yield chunk

        upstream = httpx.Response(200, stream=Source())
        plan = proxy.UpstreamRequestPlan(
            request_id="stream-lifecycle",
            upstream_url="https://example.test/responses",
            headers={},
            body={},
            usage_event=None,
            requested_model=None,
            resolved_model=None,
            trace_context={},
        )
        stream = streams._ManagedResponsesStreamBody(
            dependencies=streams.StreamDependencies(
                proxy.usage_tracker, proxy._finish_usage_and_trace
            ),
            upstream=upstream,
            body={},
            headers={},
            usage_event=None,
            stream_type="responses",
            trace_plan=plan,
            active_stream=None,
            stream_transform=transform
            or proxy._excel_response_processor().tool_stream_transform({}),
        )
        self.addAsyncCleanup(upstream.aclose)
        return stream, plan

    def completed_frame(self):
        return frame(
            "response.completed",
            response={
                "id": "resp_lifecycle",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_lifecycle",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Done"}],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        )

    async def test_close_after_completed_frame_records_success_once(self):
        stream, plan = self.make_stream(self.completed_frame())
        with patch.object(stream.dependencies, "finish_usage_and_trace") as finish:
            self.assertIn(b"event: response.completed", await anext(stream))
            self.assertFalse(stream.presentation_loop_completed)
            await stream.aclose()
            await stream.aclose()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], 200)
        self.assertEqual(finish.call_args.kwargs["usage"]["output_tokens"], 3)
        self.assertEqual(
            plan.trace_context["responses_stream_lifecycle"]["termination_cause"],
            "downstream_closed",
        )

    async def test_cancel_cleanup_after_completed_frame_records_success(self):
        async def transform(source):
            async for chunk in source:
                yield chunk
                await asyncio.Event().wait()

        stream, _ = self.make_stream(self.completed_frame(), transform=transform)
        with (
            patch.object(stream.dependencies, "finish_usage_and_trace") as finish,
            patch.object(
                stream, "request_transport_cancel", new_callable=AsyncMock
            ) as cancel,
        ):
            await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await stream.aclose()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], 200)
        cancel.assert_not_awaited()

    async def test_close_before_terminal_frame_is_still_cancelled(self):
        stream, _ = self.make_stream(
            frame("response.output_text.delta", delta="Partial")
            + self.completed_frame(),
        )
        with patch.object(stream.dependencies, "finish_usage_and_trace") as finish:
            self.assertIn(b"response.output_text.delta", await anext(stream))
            self.assertTrue(stream.capture.completed_event_seen)
            await stream.aclose()
        self.assertEqual(finish.call_args.args[1], 499)

    async def test_close_unstarted_stream_is_still_cancelled(self):
        stream, _ = self.make_stream(self.completed_frame())
        with patch.object(stream.dependencies, "finish_usage_and_trace") as finish:
            await stream.aclose()
        self.assertEqual(finish.call_args.args[1], 499)

    async def test_terminal_failure_and_incomplete_keep_their_outcomes(self):
        for event, status in (("response.failed", 502), ("response.incomplete", 200)):
            with self.subTest(event=event):
                stream, _ = self.make_stream(
                    frame(
                        event,
                        response={
                            "id": "resp_lifecycle",
                            "status": event.split(".")[1],
                            "output": [],
                            "error": {"code": "upstream_error", "message": "Failed"},
                        },
                    )
                )
                with patch.object(
                    stream.dependencies, "finish_usage_and_trace"
                ) as finish:
                    self.assertIn(event.encode(), await anext(stream))
                    await stream.aclose()
                self.assertEqual(finish.call_args.args[1], status)

    async def test_exhausted_stream_records_success_once(self):
        stream, _ = self.make_stream(self.completed_frame())
        with patch.object(stream.dependencies, "finish_usage_and_trace") as finish:
            async for _ in stream:
                pass
            await stream.aclose()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], 200)

    async def test_recovered_completion_records_success_before_eof(self):
        stream, plan = self.make_stream(
            frame("response.created", response={"id": "resp_recovered", "output": []}),
            frame(
                "response.output_item.done",
                output_index=0,
                item={
                    "id": "msg_recovered",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Done"}],
                },
            ),
        )
        with (
            patch.object(stream.dependencies, "finish_usage_and_trace") as finish,
            patch.object(
                stream, "request_transport_cancel", new_callable=AsyncMock
            ) as cancel,
        ):
            async for chunk in stream:
                if b"event: response.completed" in chunk:
                    break
            self.assertFalse(stream.capture.completed_event_seen)
            self.assertFalse(stream.presentation_loop_completed)
            await stream.aclose()
        self.assertEqual(finish.call_args.args[1], 200)
        cancel.assert_not_awaited()
        lifecycle = plan.trace_context["responses_stream_lifecycle"]
        self.assertTrue(lifecycle["presentation_completed_event_seen"])
        self.assertTrue(lifecycle["generation_end_confirmed"])

    async def test_bare_done_is_not_success(self):
        stream, _ = self.make_stream(b"data: [DONE]\n\n")
        with patch.object(stream.dependencies, "finish_usage_and_trace") as finish:
            self.assertIn(b"event: response.failed", await anext(stream))
            await stream.aclose()
        self.assertEqual(finish.call_args.args[1], 502)
