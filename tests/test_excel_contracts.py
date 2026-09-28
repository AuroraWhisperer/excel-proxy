import asyncio
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

import httpx

import excel_upstream
import responses_protocol
import excel_stream
import proxy
import responses_stream as streams
import rate_limiting
import upstream_errors
from test_excel_request_compat import summary_stream


class ExcelContractTests(unittest.TestCase):
    def history(self, result_id):
        call = {
            "type": "function_call",
            "id": "fc_original",
            "call_id": "call_contract",
            "name": "read_file",
            "arguments": "{}",
        }
        result = {
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": "exact output",
        }
        if result_id is not None:
            result["id"] = result_id
        return [call, result]

    def test_result_ids_are_stable_distinct_and_preserve_content(self):
        for result_id in (
            None,
            "fco_client",
            "ctco_client",
            "fc_" + "x" * 100,
            "fc_call_contract",
            "fc_valid_result",
        ):
            with self.subTest(result_id=result_id):
                source = self.history(result_id)
                original = copy.deepcopy(source)
                first = excel_upstream.translate_input_items(
                    source, {"read_file": "function"}
                )
                second = excel_upstream.translate_input_items(
                    source, {"read_file": "function"}
                )
                self.assertEqual(first, second)
                self.assertEqual(source, original)
                self.assertNotEqual(first[0]["id"], first[1]["id"])
                self.assertTrue(first[1]["id"].startswith("fc_"))
                self.assertLessEqual(len(first[1]["id"]), 64)
                self.assertEqual(first[1]["call_id"], original[1]["call_id"])
                self.assertEqual(first[1]["output"], original[1]["output"])
                if result_id == "fc_valid_result":
                    self.assertEqual(first[1]["id"], result_id)

    def test_custom_result_id_remains_distinct_after_history_recovery(self):
        history = self.history("ctco_client")
        history[0].update(
            type="custom_tool_call", name="apply_patch", input="exact patch"
        )
        history[1]["type"] = "custom_tool_call_output"
        output = excel_upstream.translate_input_items(
            history, {"apply_patch": "custom"}
        )
        self.assertEqual(output[1]["type"], "function_call_output")
        self.assertNotEqual(output[0]["id"], output[1]["id"])

    def test_unsupported_constraints_are_rejected(self):
        for constraint in (
            {"previous_response_id": "resp_old"},
            {"tool_choice": "required"},
            {"tool_choice": {"type": "function", "name": "read_file"}},
            {
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "schema": "invalid",
                    }
                }
            },
            {"text": {"format": {"type": "xml"}}},
        ):
            with self.subTest(constraint=constraint), self.assertRaises(ValueError):
                excel_upstream.prepare_responses_body(
                    {"input": "continue", **constraint}
                )


class ExcelErrorTests(unittest.TestCase):
    def plan(self):
        return proxy.UpstreamRequestPlan(
            request_id="contract-test",
            upstream_url=excel_upstream.RESPONSES_URL,
            headers={"authorization": "Bearer KNOWN_CREDENTIAL"},
            body={"input": "test"},
            usage_event=None,
            requested_model=excel_upstream.MODEL_ID,
            resolved_model=excel_upstream.MODEL_ID,
            trace_context={"bridge": True},
        )

    def test_failure_trace_omits_error_content_without_debug(self):
        payload = {
            "error": {"code": "PRIVATE_PROMPT", "message": "PRIVATE_PROMPT"},
            "input": "PRIVATE_PROMPT",
        }
        upstream = httpx.Response(
            422, json=payload, headers={"x-request-id": "upstream-contract"}
        )
        with (
            patch.object(proxy, "request_tracing_enabled", return_value=False),
            patch.object(proxy, "_debug_prompt_logging_enabled", return_value=False),
            patch.object(proxy, "_plan_allows_full_debug_detail", return_value=False),
            patch.object(proxy.usage_tracker, "finish_event"),
            patch.object(proxy, "_append_request_trace") as append,
        ):
            proxy._finish_usage_and_trace(
                self.plan(),
                422,
                upstream=upstream,
                response_payload=payload,
                response_text=upstream.text,
            )
        trace = append.call_args.args[0]
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(trace))
        self.assertEqual(trace["response"]["status_code"], 422)
        self.assertEqual(trace["response"]["upstream_request_id"], "upstream-contract")

    def test_error_details_use_bounded_redacted_allowlist(self):
        payload = {
            "input": "PRIVATE_PROMPT",
            "error": {
                "message": 'KNOWN_CREDENTIAL Bearer OTHER_CREDENTIAL https://user:URL_PASSWORD@example.test/path token="NAMED_CREDENTIAL"',
                "code": "invalid_request",
                "param": "input",
                "type": "invalid_request_error",
                "request": "PRIVATE_PROMPT",
            },
        }
        details = upstream_errors.sanitized_error_details(
            payload, secrets=("KNOWN_CREDENTIAL",)
        )
        rendered = json.dumps(details)
        for secret in (
            "KNOWN_CREDENTIAL",
            "OTHER_CREDENTIAL",
            "URL_PASSWORD",
            "NAMED_CREDENTIAL",
            "PRIVATE_PROMPT",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(set(details["error"]), {"message", "code", "param", "type"})
        long_error = upstream_errors.sanitized_error_details(
            {"error": {"message": "x" * 10000}}
        )
        self.assertLessEqual(len(long_error["error"]["message"]), 2048)

    def test_debug_error_trace_keeps_safe_fields_and_masks_credentials(self):
        payload = {
            "error": {"code": "invalid_input", "message": "Rejected KNOWN_CREDENTIAL"},
            "input": "PRIVATE_PROMPT",
        }
        with (
            patch.object(proxy, "request_tracing_enabled", return_value=True),
            patch.object(proxy, "_plan_allows_full_debug_detail", return_value=True),
            patch.object(proxy.usage_tracker, "finish_event"),
            patch.object(proxy, "_append_request_trace") as append,
        ):
            proxy._finish_usage_and_trace(
                self.plan(),
                422,
                response_payload=payload,
                response_text=json.dumps(payload),
            )
        trace = append.call_args.args[0]
        self.assertEqual(trace["response_payload"]["error"]["code"], "invalid_input")
        self.assertNotIn("KNOWN_CREDENTIAL", json.dumps(trace))
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(trace))

    def test_output_identity_failures_have_distinct_safe_codes(self):
        cases = (
            (
                {"status": "completed", "output": []},
                {1: {"type": "reasoning", "id": "rs_gap"}},
                "excel_missing_output_items",
            ),
            (
                {
                    "status": "completed",
                    "output": [{"type": "reasoning", "id": "rs_new"}],
                },
                {0: {"type": "reasoning", "id": "rs_old"}},
                "excel_conflicting_output_items",
            ),
            (
                {"status": "completed", "output": []},
                {0: {"type": "reasoning", "id": []}},
                "excel_invalid_output_item",
            ),
        )
        for response, finished_items, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(upstream_errors.ExcelResponseError) as caught:
                    excel_stream.completed_response(response, finished_items)
                self.assertEqual(caught.exception.code, code)


class ExcelHTTPContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.upstream_status = 422
        self.upstream_body = {
            "error": {
                "message": "PRIVATE_PROMPT KNOWN_CREDENTIAL",
                "code": "invalid_request",
            },
            "input": "PRIVATE_PROMPT",
        }
        self.upstream_sse = None

        def upstream(request):
            self.requests.append(request)
            headers = {"x-request-id": "upstream-contract"}
            if self.upstream_sse is not None:
                return httpx.Response(
                    self.upstream_status,
                    content=self.upstream_sse,
                    headers={**headers, "content-type": "text/event-stream"},
                )
            return httpx.Response(
                self.upstream_status, json=self.upstream_body, headers=headers
            )

        self.remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.local = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.remote.aclose)
        self.addAsyncCleanup(self.local.aclose)
        self.patches = self.enterContext(ExitStack())
        self.patches.enter_context(
            patch.object(rate_limiting, "throttle_upstream_request", new=AsyncMock())
        )
        for name in ("refresh_macos_excel_session", "refresh_windows_excel_session"):
            self.patches.enter_context(patch.object(proxy.excel_session_capture, name))
        self.patches.enter_context(
            patch.object(proxy, "_get_excel_upstream_client", return_value=self.remote)
        )
        self.headers = self.patches.enter_context(
            patch.object(
                excel_upstream.excel_session_store,
                "request_headers",
                return_value={
                    "authorization": "Bearer KNOWN_CREDENTIAL",
                    "chatgpt-account-id": "test-account",
                },
            )
        )
        self.patches.enter_context(
            patch.object(
                excel_upstream.excel_session_store,
                "tools_version_id",
                return_value=None,
            )
        )
        self.finish = self.patches.enter_context(
            patch.object(proxy, "_finish_usage_and_trace")
        )

    async def test_http_errors_are_safe_on_both_response_modes(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                result = await self.local.post(
                    "/v1/responses",
                    json={
                        "model": excel_upstream.MODEL_ID,
                        "stream": stream,
                        "input": "test",
                    },
                )
                self.assertEqual(result.status_code, 422)
                self.assertNotIn("PRIVATE_PROMPT", result.text)
                self.assertNotIn("KNOWN_CREDENTIAL", result.text)
                self.assertEqual(
                    result.headers.get("x-request-id"), "upstream-contract"
                )
        self.assertEqual(len(self.requests), 2)

    async def test_invalid_contract_is_rejected_before_upstream(self):
        for param, value in (
            ("previous_response_id", "resp_old"),
            ("tool_choice", "required"),
            ("text", {"format": {"type": "xml"}}),
        ):
            with self.subTest(param=param):
                result = await self.local.post(
                    "/responses",
                    json={
                        "model": excel_upstream.MODEL_ID,
                        "input": "test",
                        param: value,
                    },
                )
                self.assertEqual(result.status_code, 400)
                self.assertTrue(result.json()["error"]["param"].startswith(param))
        self.assertEqual(self.requests, [])

    async def test_partial_generation_is_not_replayed(self):
        self.upstream_status = 200
        self.upstream_sse = responses_protocol.sse_encode(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "delta": "Already generated",
                "output_index": 0,
            },
        )
        result = await self.local.post(
            "/responses",
            json={"model": excel_upstream.MODEL_ID, "input": "test", "stream": False},
        )
        self.assertEqual(result.status_code, 502)
        self.assertEqual(len(self.requests), 1)

    async def test_first_output_is_marked_for_streaming_and_buffered_responses(self):
        self.upstream_status = 200
        self.upstream_body = {
            "id": "resp_timing",
            "status": "completed",
            "output": [
                {
                    "id": "msg_timing",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello"}],
                }
            ],
        }
        for response_mode in ("stream", "buffered_sse", "json"):
            with self.subTest(response_mode=response_mode):
                self.upstream_sse = (
                    None
                    if response_mode == "json"
                    else (
                        responses_protocol.sse_encode(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "delta": "Hello",
                            },
                        )
                        + responses_protocol.sse_encode(
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": self.upstream_body,
                            },
                        )
                    )
                )
                self.finish.reset_mock()
                result = await self.local.post(
                    "/responses",
                    json={
                        "model": excel_upstream.MODEL_ID,
                        "input": "test",
                        "stream": response_mode == "stream",
                    },
                )
                self.assertEqual(result.status_code, 200)
                event = self.finish.call_args.args[0].usage_event
                self.assertGreaterEqual(
                    event["_first_output_monotonic"], event["_started_monotonic"]
                )

    async def test_terminal_errors_are_safe_on_both_response_modes(self):
        self.upstream_status = 200
        self.upstream_sse = responses_protocol.sse_encode(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "output": [],
                    "error": self.upstream_body["error"],
                },
            },
        )
        for stream in (False, True):
            with self.subTest(stream=stream):
                result = await self.local.post(
                    "/responses",
                    json={
                        "model": excel_upstream.MODEL_ID,
                        "input": "test",
                        "stream": stream,
                    },
                )
                self.assertEqual(result.status_code, 200 if stream else 502)
                self.assertNotIn("PRIVATE_PROMPT", result.text)
                if stream:
                    self.assertEqual(result.text.count("event: response.failed"), 1)

    async def test_invalid_completed_stream_emits_safe_error_without_replay(self):
        cases = (
            (None, "excel_invalid_response"),
            ({"status": "completed", "output": 1}, "excel_invalid_response"),
            ({"status": "completed", "output": {}}, "excel_invalid_response"),
            (
                {"status": "completed", "output": [{"id": []}]},
                "excel_invalid_output_item",
            ),
            (
                {"status": "completed", "output": ["PRIVATE_PROMPT KNOWN_CREDENTIAL"]},
                "excel_invalid_output_item",
            ),
            ({"status": "completed", "output": []}, "excel_empty_response"),
            (
                {
                    "status": "completed",
                    "output": [{"type": "reasoning", "summary": []}],
                },
                "excel_empty_response",
            ),
            (
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc_invalid",
                            "call_id": "call_invalid",
                            "name": "unknown_excel_tool",
                            "arguments": "PRIVATE_PROMPT KNOWN_CREDENTIAL",
                        }
                    ],
                },
                "excel_untranslatable_tool_call",
            ),
        )
        self.upstream_status = 200
        for response, code in cases:
            with self.subTest(code=code, response=response):
                self.finish.reset_mock()
                before = len(self.requests)
                self.upstream_sse = responses_protocol.sse_encode(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": response,
                    },
                )
                result = await self.local.post(
                    "/responses",
                    json={
                        "model": excel_upstream.MODEL_ID,
                        "input": "test",
                        "stream": True,
                    },
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.text.count("event: response.failed"), 1)
                event, data = responses_protocol.parse_sse_block(result.text.strip())
                self.assertEqual(event, "response.failed")
                payload = json.loads(data)
                self.assertEqual(payload["type"], "response.failed")
                self.assertEqual(payload["response"]["status"], "failed")
                self.assertEqual(payload["response"]["output"], [])
                payload = payload["response"]["error"]
                self.assertEqual(payload["code"], code)
                self.assertTrue(payload["message"])
                for private in (
                    "event: response.completed",
                    "unknown_excel_tool",
                    "PRIVATE_PROMPT",
                    "KNOWN_CREDENTIAL",
                ):
                    self.assertNotIn(private, result.text)
                self.assertEqual(len(self.requests), before + 1)
                self.finish.assert_called_once()
                self.assertEqual(self.finish.call_args.args[1], 502)
                lifecycle = self.finish.call_args.args[0].trace_context[
                    "responses_stream_lifecycle"
                ]
                self.assertEqual(
                    lifecycle["termination_cause"], "response_validation_error"
                )
                self.assertEqual(lifecycle["upstream_error_code"], code)
                self.assertEqual(
                    lifecycle["upstream_error_message"], payload["message"]
                )

    async def test_incomplete_stream_does_not_release_tools_or_retry(self):
        self.upstream_status = 200
        self.upstream_sse = (
            responses_protocol.sse_encode(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_partial",
                        "call_id": "call_partial",
                        "name": "unknown_excel_tool",
                        "arguments": "PRIVATE_PROMPT KNOWN_CREDENTIAL",
                    },
                },
            )
            + b"data: [DONE]"
            + bytes([10, 10])
        )
        result = await self.local.post(
            "/responses",
            json={
                "model": excel_upstream.MODEL_ID,
                "input": "test",
                "stream": True,
            },
        )
        self.assertEqual(result.status_code, 200)
        event, data = responses_protocol.parse_sse_block(result.text.strip())
        self.assertEqual(event, "response.failed")
        self.assertEqual(
            json.loads(data)["response"]["error"]["code"], "excel_stream_incomplete"
        )
        for private in ("function_call", "PRIVATE_PROMPT", "KNOWN_CREDENTIAL"):
            self.assertNotIn(private, result.text)
        self.assertEqual(len(self.requests), 1)
        self.finish.assert_called_once()
        self.assertEqual(self.finish.call_args.args[1], 502)

    async def test_non_streaming_validation_failure_preserves_safe_error_code(self):
        self.upstream_status = 200
        self.upstream_sse = responses_protocol.sse_encode(
            "response.completed",
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": []},
            },
        )
        result = await self.local.post(
            "/responses",
            json={
                "model": excel_upstream.MODEL_ID,
                "input": "test",
                "stream": False,
            },
        )
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()["error"]["code"], "excel_empty_response")
        self.assertEqual(len(self.requests), 1)
        self.finish.assert_called_once()

    async def test_failed_stream_closes_upstream_after_partial_output(self):
        class Stream(httpx.AsyncByteStream):
            close_count = 0

            async def __aiter__(self):
                yield responses_protocol.sse_encode(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "delta": "Partial output",
                        "output_index": 0,
                    },
                )
                yield responses_protocol.sse_encode(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {"status": "completed", "output": []},
                    },
                )
                raise AssertionError(
                    "Do not consume the HTTP tail after a rejected terminal event"
                )

            async def aclose(self):
                self.close_count += 1

        stream = Stream()
        response = httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream
        )
        with patch.object(
            self.remote, "send", new=AsyncMock(return_value=response)
        ) as send:
            result = await self.local.post(
                "/responses",
                json={
                    "model": excel_upstream.MODEL_ID,
                    "input": "test",
                    "stream": True,
                },
            )
        self.assertEqual(result.status_code, 200)
        self.assertIn("Partial output", result.text)
        self.assertEqual(result.text.count("event: response.failed"), 1)
        self.assertNotIn("event: response.completed", result.text)
        self.assertTrue(response.is_closed)
        self.assertEqual(stream.close_count, 1)
        send.assert_awaited_once()
        self.finish.assert_called_once()
        self.assertEqual(self.finish.call_args.args[1], 502)

    async def test_transport_failure_is_not_mislabeled_as_response_validation(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield responses_protocol.sse_encode(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "delta": "Partial output",
                        "output_index": 0,
                    },
                )
                raise httpx.RemoteProtocolError("PRIVATE_PROMPT KNOWN_CREDENTIAL")

        response = httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )
        with patch.object(
            self.remote, "send", new=AsyncMock(return_value=response)
        ) as send:
            with self.assertRaises(httpx.RemoteProtocolError):
                await self.local.post(
                    "/responses",
                    json={
                        "model": excel_upstream.MODEL_ID,
                        "input": "test",
                        "stream": True,
                    },
                )
        self.assertTrue(response.is_closed)
        send.assert_awaited_once()
        self.finish.assert_called_once()
        lifecycle = self.finish.call_args.args[0].trace_context[
            "responses_stream_lifecycle"
        ]
        self.assertEqual(lifecycle["termination_cause"], "upstream_error")
        self.assertEqual(lifecycle["upstream_error_type"], "RemoteProtocolError")
        self.assertIsNone(lifecycle["upstream_error_code"])
        self.assertIsNone(lifecycle["upstream_error_message"])
        self.assertNotIn("KNOWN_CREDENTIAL", json.dumps(lifecycle))

    async def test_initial_stream_errors_preserve_diagnostic_type(self):
        for error in (
            httpx.ConnectError("PRIVATE"),
            httpx.ReadError("PRIVATE"),
            httpx.ReadTimeout("PRIVATE"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.remote, "send", AsyncMock(side_effect=error)):
                    result = await self.local.post(
                        "/responses",
                        json={
                            "model": excel_upstream.MODEL_ID,
                            "input": "test",
                            "stream": True,
                        },
                    )
                self.assertEqual(
                    result.status_code,
                    504 if isinstance(error, httpx.ReadTimeout) else 502,
                )
                self.assertIs(self.finish.call_args.kwargs.get("error"), error)
                self.assertNotIn("PRIVATE", result.text)

    async def test_supersession_block_is_409_without_an_unbound_exception(self):
        error = streams._ResponsesSupersessionBlocked([])
        with patch.object(
            streams, "_supersede_active_responses_streams", AsyncMock(side_effect=error)
        ):
            result = await self.local.post(
                "/responses",
                json={
                    "model": excel_upstream.MODEL_ID,
                    "input": "test",
                    "stream": True,
                },
            )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.requests, [])
        self.assertIs(self.finish.call_args.kwargs.get("error"), error)

    async def test_connect_failure_retries_but_ambiguous_failure_does_not(self):
        for exception, expected_count in (
            (httpx.ConnectError, 2),
            (httpx.ReadError, 1),
            (httpx.WriteTimeout, 1),
        ):
            with self.subTest(exception=exception):
                send = AsyncMock(
                    side_effect=[
                        exception("test failure"),
                        httpx.Response(
                            200,
                            content=summary_stream("connected"),
                            headers={"content-type": "text/event-stream"},
                        ),
                    ]
                )
                with patch.object(self.remote, "send", send):
                    result = await self.local.post(
                        "/responses",
                        json={"model": excel_upstream.MODEL_ID, "input": "test"},
                    )
                self.assertEqual(send.await_count, expected_count)
                self.assertEqual(
                    result.status_code,
                    200
                    if expected_count == 2
                    else 502
                    if exception is httpx.ReadError
                    else 504,
                )

    async def test_connection_test_uses_selected_model_and_real_bps_contract(self):
        self.upstream_status = 200
        self.upstream_sse = summary_stream("connection-test")
        result = await self.local.post(
            "/api/config/excel-session/test", json={"model": "gpt-6-astra-excel"}
        )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["ok"])
        self.assertEqual(result.json()["model"], "gpt-6-astra-excel")
        self.assertEqual(result.json()["model_display_name"], "6-Astra Excel")
        self.assertEqual(result.json()["request_id"], "upstream-contract")
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), excel_upstream.RESPONSES_URL)
        self.assertEqual(request.headers["authorization"], "Bearer KNOWN_CREDENTIAL")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertEqual(body["model_selection"], "explicit")
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)
        self.assertEqual(
            body["input"][-1]["content"][0]["text"], "Reply with exactly OK."
        )

    async def test_connection_test_classifies_upstream_errors_without_echo(self):
        for status, category in (
            (401, "authentication"),
            (403, "access"),
            (429, "rate_limit"),
            (422, "request"),
            (500, "upstream"),
            (503, "upstream"),
        ):
            with self.subTest(status=status):
                self.upstream_status = status
                result = await self.local.post(
                    "/api/config/excel-session/test",
                    json={"model": excel_upstream.MODEL_ID},
                )
                self.assertEqual(result.status_code, status)
                self.assertFalse(result.json()["ok"])
                self.assertEqual(result.json()["category"], category)
                self.assertNotIn("PRIVATE_PROMPT", result.text)
                self.assertNotIn("KNOWN_CREDENTIAL", result.text)
        self.upstream_status = 422
        self.upstream_body["error"]["code"] = "basispoints_model_access_changed"
        result = await self.local.post(
            "/api/config/excel-session/test", json={"model": excel_upstream.MODEL_ID}
        )
        self.assertEqual(result.json()["category"], "access")

    async def test_connection_test_requires_completed_text(self):
        self.upstream_status = 200
        self.upstream_sse = b"data: [DONE]\n\n"
        result = await self.local.post(
            "/api/config/excel-session/test", json={"model": excel_upstream.MODEL_ID}
        )
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()["category"], "protocol")
        self.assertEqual(len(self.requests), 1)

    async def test_account_connection_test_sends_its_own_credentials_and_preserves_http_status(
        self,
    ):
        store = proxy.account_balances.AccountBalanceStore()
        record_id = store.import_accounts(
            {"access_token": "ACCOUNT_CREDENTIAL", "account_id": "clicked-account"}
        )["imported_ids"][0]
        with patch.object(proxy.account_balances, "balance_store", store):
            self.upstream_status = 200
            self.upstream_sse = summary_stream("OK")
            response = await self.local.post(
                f"/api/account-balances/{record_id}/test", json={}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["response_text"], "OK")
            for status in (401, 503):
                self.upstream_status = status
                response = await self.local.post(
                    f"/api/account-balances/{record_id}/test", json={}
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["status_code"], status)
                self.assertNotIn("KNOWN_CREDENTIAL", response.text)
                self.assertNotIn("ACCOUNT_CREDENTIAL", response.text)
        self.assertEqual(len(self.requests), 3)
        for request in self.requests:
            self.assertEqual(str(request.url), excel_upstream.RESPONSES_URL)
            self.assertEqual(
                request.headers["authorization"], "Bearer ACCOUNT_CREDENTIAL"
            )
            self.assertEqual(request.headers["chatgpt-account-id"], "clicked-account")
        self.headers.assert_not_called()

    async def test_connection_test_missing_session_does_not_send(self):
        self.headers.side_effect = RuntimeError("No Excel session")
        result = await self.local.post(
            "/api/config/excel-session/test", json={"model": excel_upstream.MODEL_ID}
        )
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.json()["category"], "authentication")
        self.assertEqual(self.requests, [])

    async def test_connection_test_timeout_closes_upstream_and_finishes_usage(self):
        closed = Mock()

        class HangingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed()

        response = httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=HangingStream()
        )
        with (
            patch.object(self.remote, "send", AsyncMock(return_value=response)),
            patch.object(proxy, "_EXCEL_CONNECTION_TEST_TIMEOUT_SECONDS", 0.05),
        ):
            result = await self.local.post(
                "/api/config/excel-session/test",
                json={"model": excel_upstream.MODEL_ID},
            )
        self.assertEqual(result.status_code, 504)
        self.assertEqual(result.json()["category"], "timeout")
        closed.assert_called_once()
        self.assertEqual(self.finish.call_args.args[1], 499)
        self.assertFalse(proxy._excel_connection_test_lock.locked())

    async def test_connection_test_rejects_unknown_models_and_cross_site_requests(self):
        for headers, model, expected in (
            ({}, "unknown-model", 400),
            ({"origin": "https://unrelated.example"}, excel_upstream.MODEL_ID, 403),
            ({"host": "unrelated.example"}, excel_upstream.MODEL_ID, 403),
            ({"content-type": "text/plain"}, excel_upstream.MODEL_ID, 415),
        ):
            with self.subTest(headers=headers):
                result = await self.local.post(
                    "/api/config/excel-session/test",
                    json={"model": model},
                    headers=headers,
                )
                self.assertEqual(result.status_code, expected)
        self.assertEqual(self.requests, [])

    async def test_connection_test_rejects_overlapping_runs(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def hold(*args):
            started.set()
            await release.wait()
            return proxy.JSONResponse(
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

        with patch.object(proxy, "_handle_excel_responses", side_effect=hold):
            first = asyncio.create_task(
                self.local.post(
                    "/api/config/excel-session/test",
                    json={"model": excel_upstream.MODEL_ID},
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 1)
                second = await self.local.post(
                    "/api/config/excel-session/test",
                    json={"model": excel_upstream.MODEL_ID},
                )
                self.assertEqual(second.status_code, 429)
                self.assertEqual(second.json()["category"], "busy")
            finally:
                release.set()
                await first


if __name__ == "__main__":
    unittest.main()
