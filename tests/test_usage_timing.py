"""First-output timing includes generated content beyond assistant text."""

import json
import asyncio
import threading
import unittest
from unittest.mock import patch

import httpx

import proxy
from usage_tracking import SSEUsageCapture, UsageTracker


def event_bytes(event_type, **payload):
    return f"event: {event_type}\ndata: {json.dumps({'type': event_type, **payload})}\n\n".encode()


class FirstOutputCaptureTests(unittest.TestCase):
    def test_generated_deltas_count_as_output(self):
        for event_type in (
            "response.output_text.delta",
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
            "response.function_call_arguments.delta",
            "response.custom_tool_call_input.delta",
            "response.refusal.delta",
        ):
            for delta in ("content", " "):
                with self.subTest(event_type=event_type, delta=delta):
                    capture = SSEUsageCapture("responses")
                    self.assertTrue(capture.feed(event_bytes(event_type, delta=delta)))

    def test_tool_and_reasoning_items_count_without_text_deltas(self):
        items = (
            {"type": "function_call", "name": "run_officejs", "arguments": "{}"},
            {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Thinking"}]},
            {"type": "message", "content": [{"type": "refusal", "refusal": "Cannot comply"}]},
        )
        for item in items:
            for event_type in ("response.output_item.added", "response.output_item.done"):
                with self.subTest(item=item, event_type=event_type):
                    self.assertTrue(SSEUsageCapture("responses").feed(event_bytes(event_type, item=item)))

    def test_lifecycle_and_empty_deltas_do_not_count_as_output(self):
        chunks = (
            b": keepalive\n\n",
            event_bytes("response.created", response={"status": "in_progress", "output": []}),
            event_bytes("response.in_progress"),
            event_bytes("response.output_text.delta", delta=""),
            event_bytes("response.function_call_arguments.delta", delta=""),
            event_bytes("response.output_item.added", item={"type": "function_call", "name": "run_officejs", "arguments": ""}),
            event_bytes("response.output_item.added", item={"type": "custom_tool_call", "name": "apply_patch", "input": ""}),
            event_bytes("response.output_item.added", item={"type": "reasoning", "summary": []}),
            event_bytes("response.output_item.added", item={"type": "message", "content": []}),
            event_bytes("response.failed", response={"error": {"message": "failed"}}),
            b"data: [DONE]\n\n",
        )
        for chunk in chunks:
            with self.subTest(chunk=chunk):
                self.assertFalse(SSEUsageCapture("responses").feed(chunk))

    def test_completed_only_response_counts_output_and_keeps_usage(self):
        capture = SSEUsageCapture("responses")
        self.assertTrue(capture.feed(event_bytes(
            "response.completed", response={
                "output": [{"type": "function_call", "name": "run_officejs", "arguments": "{}"}],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        )))
        self.assertTrue(capture.completed_event_seen)
        self.assertEqual(capture.usage["output_tokens"], 3)

    def test_split_sse_frame_with_event_name_only(self):
        capture = SSEUsageCapture("responses")
        self.assertFalse(capture.feed(b'event: response.function_call_arguments.delta\r\ndata: {"delta":'))
        self.assertTrue(capture.feed(b'"{}"}\r\n\r\n'))


class FirstOutputLifecycleTests(unittest.TestCase):
    def test_first_output_is_recorded_once_and_persisted(self):
        tracker = UsageTracker()
        event = {"request_id": "timing-test", "_started_monotonic": 100.0}
        with patch("usage_tracking.time.perf_counter", return_value=101.25):
            tracker.mark_first_output(event)
        with patch("usage_tracking.time.perf_counter", return_value=103.0):
            tracker.mark_first_output(event)
            with patch.object(tracker, "_persist_event") as persist:
                tracker.finish_event(event, 200)
        finished = persist.call_args.args[0]
        self.assertEqual(finished["time_to_first_token_ms"], 1250)
        self.assertEqual(finished["duration_ms"], 3000)
        self.assertNotIn("_first_output_monotonic", finished)

    def test_no_output_does_not_invent_first_token_time(self):
        tracker = UsageTracker()
        with patch.object(tracker, "_persist_event") as persist:
            tracker.finish_event({"request_id": "empty-test", "_started_monotonic": 100.0}, 200)
        self.assertNotIn("time_to_first_token_ms", persist.call_args.args[0])


class BufferedFirstOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_refresh_does_not_block_other_streams(self):
        loop = asyncio.get_running_loop()
        released = threading.Event()
        results = []

        def refresh(*args, **kwargs):
            loop.call_soon_threadsafe(released.set)
            results.append(released.wait(timeout=1))

        with patch("proxy.excel_session_capture.refresh_macos_excel_session"), \
             patch("proxy.excel_session_capture.refresh_windows_excel_session", side_effect=refresh), \
             patch.object(proxy.excel_upstream.excel_session_store, "request_headers", side_effect=RuntimeError("test stop before upstream")):
            response = await proxy._handle_excel_responses(None, {"model": "gpt-6-astra-excel", "stream": True})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(results, [True], "Session discovery blocked the event loop")

    async def test_buffering_preserves_first_output_time_before_completion(self):
        clock = [100.0]

        class TimedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                clock[0] = 101.0
                yield event_bytes("response.created", response={"output": []})
                clock[0] = 102.5
                yield event_bytes("response.output_text.delta", delta="Hello")
                clock[0] = 109.0
                yield event_bytes("response.completed", response={
                    "id": "resp_timing", "status": "completed", "output": [{
                        "id": "msg_timing", "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Hello"}],
                    }],
                })

        event = {"request_id": "buffered-test", "_started_monotonic": 100.0}
        upstream = httpx.Response(200, stream=TimedStream())
        self.addAsyncCleanup(upstream.aclose)
        with patch("usage_tracking.time.perf_counter", side_effect=lambda: clock[0]):
            result = await proxy._read_excel_non_streaming_response_payload(upstream, event)
            with patch.object(proxy.usage_tracker, "_persist_event") as persist:
                proxy.usage_tracker.finish_event(event, 200)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(persist.call_args.args[0]["time_to_first_token_ms"], 2500)
        self.assertEqual(persist.call_args.args[0]["duration_ms"], 9000)
