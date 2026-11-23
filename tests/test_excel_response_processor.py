"""Response processing owns protocol behavior without importing the application."""

import asyncio
import importlib
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

import responses_protocol
import rate_limiting
from upstream_request import UpstreamRequestPlan
from usage_tracking import SSEUsageCapture, UsageTracker


def request_plan(request_id):
    return UpstreamRequestPlan(
        request_id,
        "https://example.test/responses",
        {},
        {"input": "test"},
        {"request_id": request_id},
        "gpt-5.6-sol-excel",
        "gpt-5.6-sol-excel",
        trace_context={},
    )


def completed_response():
    return {
        "id": "resp_test",
        "status": "completed",
        "model": "gpt-5.6-sol",
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            }
        ],
        "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
    }


def tool_response():
    payload = completed_response()
    payload["output"] = [
        {
            "type": "function_call",
            "id": "fc_test",
            "call_id": "call_test",
            "name": "run_officejs",
            "status": "completed",
            "arguments": json.dumps(
                {
                    "summary": "Call demo",
                    "extended_summary": "",
                    "destructive": False,
                    "references": [],
                    "code": json.dumps({"name": "demo", "arguments": {}}),
                }
            ),
        }
    ]
    return payload


class ExcelResponseProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = importlib.import_module("excel_responses")
        self.enterContext(
            patch.object(rate_limiting, "throttle_upstream_request", AsyncMock())
        )
        self.requests = []
        self.upstream_payload = completed_response()

        def respond(request):
            self.requests.append(request)
            return httpx.Response(
                200, json=self.upstream_payload, headers={"x-request-id": "upstream-id"}
            )

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(self.client.aclose)
        self.get_client = Mock(return_value=self.client)
        self.finish = Mock()
        self.tracker = Mock(create_sse_capture=Mock(side_effect=SSEUsageCapture))
        self.processor = self.owner.ExcelResponseProcessor(
            usage_tracker=self.tracker,
            get_upstream_client=self.get_client,
            finish_usage_and_trace=self.finish,
        )

    async def test_json_success_preserves_identity_usage_and_single_dispatch(self):
        plan = request_plan("success")
        self.get_client.assert_not_called()
        result = await self.processor.post_non_streaming_request(
            plan, client_body={"model": plan.requested_model}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["x-request-id"], "upstream-id")
        payload = json.loads(result.body)
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(
            payload["usage"], {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
        )
        self.assertEqual(len(self.requests), 1)
        self.assertIs(self.requests[0].extensions["excel_body"], plan.body)
        self.finish.assert_called_once()
        self.assertEqual(self.finish.call_args.args, (plan, 200))
        self.tracker.mark_first_output.assert_called_once_with(plan.usage_event)

    async def test_json_preserves_reported_model_in_response_and_usage(self):
        tracker = UsageTracker()
        for with_tool in (False, True):
            with self.subTest(with_tool=with_tool):
                self.upstream_payload = (
                    tool_response() if with_tool else completed_response()
                )
                self.upstream_payload["model"] = "gpt-5.6-luna"
                plan = request_plan("model-mismatch")
                plan.requested_model = plan.resolved_model = "gpt-6-astra-excel"
                source = {
                    "model": plan.requested_model,
                    "tools": [{"type": "function", "name": "demo"}],
                }
                result = await self.processor.post_non_streaming_request(
                    plan, client_body=source
                )
                self.assertEqual(json.loads(result.body)["model"], "gpt-5.6-luna")
                reported = self.finish.call_args.kwargs["response_payload"]
                with patch.object(tracker, "_persist_event") as persist:
                    tracker.finish_event(plan.usage_event, 200, response_payload=reported)
                self.assertEqual(
                    persist.call_args.args[0]["response_model"], "gpt-5.6-luna"
                )

    async def test_stream_preserves_reported_model_after_tool_translation(self):
        for with_tool in (False, True):
            with self.subTest(with_tool=with_tool):
                payload = tool_response() if with_tool else completed_response()
                payload["model"] = "gpt-5.6-luna"
                plan = request_plan("stream-model-mismatch")
                source = {
                    "model": "gpt-6-astra-excel",
                    "tools": [{"type": "function", "name": "demo"}],
                }

                async def upstream():
                    yield responses_protocol.sse_encode(
                        "response.completed",
                        {"type": "response.completed", "response": payload},
                    )

                transform = self.processor.tool_stream_transform(source, trace_plan=plan)
                chunks = [chunk async for chunk in transform(upstream())]
                events = [
                    json.loads(line[6:])
                    for line in b"".join(chunks).decode().splitlines()
                    if line.startswith("data: ")
                ]
                completed = [
                    event["response"]
                    for event in events
                    if event["type"] == "response.completed"
                ]
                self.assertEqual(len(completed), 1)
                self.assertEqual(completed[0]["model"], "gpt-5.6-luna")
                self.assertEqual(plan.usage_event["response_model"], "gpt-5.6-luna")

    async def test_missing_upstream_model_uses_requested_alias(self):
        for with_tool in (False, True):
            with self.subTest(with_tool=with_tool):
                self.upstream_payload = (
                    tool_response() if with_tool else completed_response()
                )
                self.upstream_payload.pop("model")
                source = {
                    "model": "gpt-6-astra-excel",
                    "tools": [{"type": "function", "name": "demo"}],
                }
                result = await self.processor.post_non_streaming_request(
                    request_plan("missing-model"), client_body=source
                )
                self.assertEqual(json.loads(result.body)["model"], "gpt-6-astra-excel")

    async def test_processors_do_not_share_usage_callbacks(self):
        other_finish = Mock()
        other_tracker = Mock(create_sse_capture=Mock(side_effect=SSEUsageCapture))
        other = self.owner.ExcelResponseProcessor(
            usage_tracker=other_tracker,
            get_upstream_client=self.get_client,
            finish_usage_and_trace=other_finish,
        )
        first, second = request_plan("first"), request_plan("second")
        await asyncio.gather(
            self.processor.post_non_streaming_request(first, client_body={}),
            other.post_non_streaming_request(second, client_body={}),
        )
        self.finish.assert_called_once()
        other_finish.assert_called_once()
        self.assertIs(self.finish.call_args.args[0], first)
        self.assertIs(other_finish.call_args.args[0], second)
        self.tracker.mark_first_output.assert_called_once_with(first.usage_event)
        other_tracker.mark_first_output.assert_called_once_with(second.usage_event)
        self.assertEqual(len(self.requests), 2)

    async def test_reader_returns_at_terminal_frame_without_reading_broken_tail(self):
        class TerminalThenBroken(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield responses_protocol.sse_encode(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": completed_response(),
                    },
                )
                raise AssertionError("must not read after a completed response")

        upstream = httpx.Response(200, stream=TerminalThenBroken())
        self.addAsyncCleanup(upstream.aclose)
        usage_event = {"request_id": "terminal"}
        payload = await self.processor.read_response_payload(upstream, usage_event)
        self.assertEqual(payload["id"], "resp_test")
        self.tracker.mark_first_output.assert_called_once_with(usage_event)
        self.finish.assert_not_called()

    def test_http_error_keeps_retry_headers_auth_marker_and_sanitization(self):
        for status in (401, 429):
            with self.subTest(status=status):
                self.finish.reset_mock()
                plan = request_plan("rejected")
                upstream = httpx.Response(
                    status,
                    json={"error": {"message": "PRIVATE"}},
                    headers={"x-request-id": "error-id", "retry-after": "12"},
                )
                result = self.processor.handle_upstream_error(upstream, trace_plan=plan)
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.headers["retry-after"], "12")
                self.assertEqual(result.headers["x-request-id"], "error-id")
                self.assertEqual(result._excel_auth_rejected, status == 401)
                self.assertNotIn(b"PRIVATE", result.body)
                self.finish.assert_called_once()
                self.assertEqual(self.finish.call_args.args, (plan, status))

    async def test_cancelled_send_records_499_and_propagates(self):
        plan = request_plan("cancelled")
        with patch.object(
            self.client, "send", AsyncMock(side_effect=asyncio.CancelledError())
        ) as send:
            with self.assertRaises(asyncio.CancelledError):
                await self.processor.post_non_streaming_request(plan, client_body={})
        send.assert_awaited_once()
        self.finish.assert_called_once()
        self.assertEqual(self.finish.call_args.args, (plan, 499))

    async def test_error_while_reading_closes_response_without_replaying_request(self):
        closed = Mock()

        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.ReadError("PRIVATE")
                yield b""

            async def aclose(self):
                closed()

        upstream = httpx.Response(
            200, stream=BrokenStream(), headers={"content-type": "text/event-stream"}
        )
        with patch.object(
            self.client, "send", AsyncMock(return_value=upstream)
        ) as send:
            result = await self.processor.post_non_streaming_request(
                request_plan("broken"), client_body={}
            )
        self.assertEqual(result.status_code, 502)
        self.assertNotIn(b"PRIVATE", result.body)
        self.finish.assert_called_once()
        send.assert_awaited_once()
        closed.assert_called_once()
