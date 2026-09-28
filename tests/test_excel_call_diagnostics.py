import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

import excel_responses
import excel_upstream
import responses_protocol
import proxy
import upstream_errors
import usage_tracking
import test_excel_contracts
from test_excel_tool_compatibility import FUNCTION_TOOL, transport


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        self.closed = True


class CallDiagnosisTests(unittest.TestCase):
    def test_unknown_tool_diagnostics_identify_target_and_catalog_without_arguments(
        self,
    ):
        source = {
            "tools": [
                {
                    "type": "namespace",
                    "name": "functions",
                    "tools": [
                        {"type": "custom", "name": "exec", "format": {"type": "text"}},
                    ],
                }
            ]
        }
        for native, origin in (
            (
                transport(
                    {"name": "exec_command", "arguments": {"cmd": "PRIVATE_COMMAND"}}
                ),
                "envelope",
            ),
            (
                {
                    **transport({}),
                    "name": "exec_command",
                    "arguments": '{"cmd":"PRIVATE_COMMAND"}',
                },
                "native",
            ),
        ):
            with self.subTest(origin=origin):
                diagnostic = {}
                self.assertIsNone(
                    excel_upstream.extract_native_client_tool_calls(
                        {"output": [native]},
                        source,
                        diagnostics=diagnostic,
                    )
                )
                self.assertEqual(diagnostic["target_name"], "exec_command")
                self.assertEqual(diagnostic["target_source"], origin)
                self.assertEqual(diagnostic["declared_tools"], ["functions.exec"])
                self.assertEqual(diagnostic["declared_tool_count"], 1)
                self.assertNotIn("PRIVATE_COMMAND", json.dumps(diagnostic))
                self.assertNotIn(
                    "exec_command", excel_upstream.tool_call_failure_message(diagnostic)
                )

    def test_unknown_tool_diagnostic_names_are_bounded_identifiers(self):
        source = {
            "tools": [{"type": "function", "name": "t" + str(i)} for i in range(80)]
        }
        for name in ("PRIVATE\nCOMMAND", "x" * 5000, 'exec_command({"cmd":"PRIVATE"})'):
            diagnostic = {}
            excel_upstream.extract_native_client_tool_calls(
                {
                    "output": [
                        transport({"name": name, "arguments": {"cmd": "PRIVATE"}})
                    ]
                },
                source,
                diagnostics=diagnostic,
            )
            self.assertIsNone(diagnostic["target_name"])
            self.assertEqual(len(diagnostic["target_fingerprint"]), 16)
            self.assertEqual(len(diagnostic["declared_tools"]), 64)
            self.assertEqual(diagnostic["declared_tool_count"], 80)
            self.assertNotIn("PRIVATE", json.dumps(diagnostic))
            reordered = {}
            excel_upstream.extract_native_client_tool_calls(
                {"output": [transport({"name": name, "arguments": {}})]},
                {"tools": list(reversed(source["tools"]))},
                diagnostics=reordered,
            )
            self.assertEqual(
                diagnostic["catalog_fingerprint"], reordered["catalog_fingerprint"]
            )

    def test_unknown_tool_recovery_respects_disabled_tools(self):
        source = {
            "input": "Answer without tools",
            "tools": [FUNCTION_TOOL],
            "tool_choice": "none",
        }
        diagnostic = {"reason": "unknown_tool", "tool_call_index": 0}
        retry = excel_upstream.unknown_tool_regeneration_request(
            excel_upstream.prepare_responses_body(source),
            {
                "status": "completed",
                "output": [transport({"name": "unknown", "arguments": {}})],
            },
            source,
            diagnostic,
        )
        self.assertIsNone(retry)
        self.assertEqual(diagnostic["recovery_skipped"], "no_client_tools")

    def test_unknown_tool_feedback_does_not_blame_json_escaping(self):
        message = excel_upstream.tool_call_failure_message(
            {"reason": "unknown_tool", "tool_call_index": 0}
        )
        self.assertIn("current client catalog", message)
        self.assertNotIn("quotes", message)
        self.assertNotIn("JSON serializer", message)
        self.assertIn("No client tool in this batch was executed", message)

    def test_format_correction_advances_iteration_with_same_task_and_turn(self):
        body = excel_upstream.prepare_responses_body(
            {
                "model": excel_upstream.MODEL_ID,
                "input": "test",
                "tools": [FUNCTION_TOOL],
                "metadata": {"agent_iteration": "7"},
            }
        )
        original = copy.deepcopy(body)
        native = transport({})
        native["arguments"] = json.dumps({"code": "echo original"})
        repaired = excel_upstream.tool_call_repair_request(
            body,
            {"output": [native]},
            {"reason": "invalid_transport_envelope", "tool_call_index": 0},
        )
        self.assertEqual(
            repaired["metadata"], {**body["metadata"], "agent_iteration": "8"}
        )
        self.assertEqual(repaired["model"], body["model"])
        self.assertEqual(repaired["input"][:-2], body["input"])
        self.assertEqual(body, original)

    def test_unknown_tool_regeneration_uses_original_task_without_fabricated_results(
        self,
    ):
        source = {"input": "Inspect the requested file", "tools": [FUNCTION_TOOL]}
        body = excel_upstream.prepare_responses_body(source)
        body["input"].append({"type": "compaction_trigger"})
        original = copy.deepcopy(body)
        native = transport(
            {"name": "PRIVATE_TOOL", "arguments": {"cmd": "PRIVATE_COMMAND"}}
        )
        retry = excel_upstream.unknown_tool_regeneration_request(
            body,
            {"status": "completed", "output": [native]},
            source,
            {"reason": "unknown_tool", "tool_call_index": 0},
        )
        self.assertIsNotNone(retry)
        self.assertEqual(retry["input"][:-2], body["input"][:-1])
        self.assertEqual(retry["input"][-1], {"type": "compaction_trigger"})
        self.assertEqual(retry["input"][-2]["role"], "developer")
        self.assertNotIn("PRIVATE", json.dumps(retry))
        self.assertFalse(
            any(item.get("type") == "function_call_output" for item in retry["input"])
        )
        self.assertEqual(
            retry["metadata"], {**body["metadata"], "agent_iteration": "2"}
        )
        self.assertEqual(body, original)

    def test_failures_have_safe_categories_and_actions(self):
        cases = [
            (401, None, None, "authentication"),
            (403, None, None, "permission"),
            (429, None, None, "rate_limit"),
            (422, None, None, "request_validation"),
            (502, "excel_untranslatable_tool_call", None, "tool_contract"),
            (502, "excel_stream_incomplete", None, "incomplete_stream"),
            (502, "excel_invalid_output_item", None, "response_contract"),
            (504, None, "ReadTimeout", "timeout"),
            (502, None, "ConnectError", "connection"),
            (499, None, None, "cancelled"),
            (503, None, None, "upstream_service"),
            (599, "PRIVATE_CODE", "PRIVATE_ERROR", "unknown"),
        ]
        for status, code, error_type, category in cases:
            with self.subTest(category=category):
                result = upstream_errors.diagnose_failure(
                    status, code=code, error_type=error_type
                )
                self.assertEqual(result["category"], category)
                self.assertTrue(result["action"])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_untrusted_diagnostic_fields_cannot_break_error_reporting(self):
        for code in ({"PRIVATE": "value"}, ["PRIVATE"], False, 123):
            result = upstream_errors.diagnose_failure(502, code=code, error_type=code)
            self.assertEqual(result["category"], "upstream_service")
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_archived_requests_preserve_safe_diagnostics(self):
        event = {
            "failure_diagnosis": upstream_errors.diagnose_failure(429),
            "tool_call_recovery": {"attempts": 1, "outcome": "failed"},
            "tool_call_diagnostics": {
                "reason": "unknown_tool",
                "target_name": "exec_command",
            },
        }
        archived = usage_tracking._usage_event_archive_summary(event)
        for key, value in event.items():
            self.assertEqual(archived[key], value)

    def test_failure_trace_and_usage_keep_diagnosis_without_debug_prompts(self):
        plan = proxy.UpstreamRequestPlan(
            "diagnostic",
            "https://example.test",
            {},
            {},
            {},
            None,
            None,
            trace_context={"bridge": True},
        )
        with (
            patch.object(proxy, "_plan_allows_full_debug_detail", return_value=False),
            patch.object(proxy, "_debug_prompt_logging_enabled", return_value=False),
            patch.object(proxy, "request_tracing_enabled", return_value=False),
            patch.object(proxy.usage_tracker, "finish_event") as finish,
            patch.object(proxy, "_append_request_trace") as trace,
        ):
            proxy._finish_usage_and_trace(
                plan,
                429,
                response_payload={
                    "error": {"message": "PRIVATE_PROMPT", "code": "PRIVATE_CODE"},
                },
            )
        diagnosis = trace.call_args.args[0]["failure_diagnosis"]
        self.assertEqual(diagnosis["category"], "rate_limit")
        self.assertEqual(finish.call_args.args[0]["failure_diagnosis"], diagnosis)
        self.assertNotIn("PRIVATE", json.dumps(trace.call_args.args[0]))

    def test_unknown_target_is_logged_without_debug_prompts_or_recovery(self):
        diagnostic = {
            "reason": "unknown_tool",
            "target_name": "exec_command",
            "declared_tools": ["functions.exec"],
            "recovery_skipped": "tool_history",
        }
        plan = proxy.UpstreamRequestPlan(
            "unknown",
            "https://example.test",
            {},
            {},
            {},
            None,
            None,
            trace_context={"bridge": True, "tool_call_diagnostics": diagnostic},
        )
        with (
            patch.object(proxy, "_plan_allows_full_debug_detail", return_value=False),
            patch.object(proxy, "_debug_prompt_logging_enabled", return_value=False),
            patch.object(proxy, "request_tracing_enabled", return_value=False),
            patch.object(proxy.usage_tracker, "finish_event") as finish,
            patch.object(proxy, "_append_request_trace") as trace,
        ):
            proxy._finish_usage_and_trace(
                plan,
                502,
                response_payload={
                    "error": {
                        "code": "excel_untranslatable_tool_call",
                        "message": "PRIVATE_COMMAND",
                    }
                },
            )
        self.assertEqual(trace.call_args.args[0]["tool_call_diagnostics"], diagnostic)
        self.assertEqual(finish.call_args.args[0]["tool_call_diagnostics"], diagnostic)
        self.assertNotIn("PRIVATE_COMMAND", json.dumps(trace.call_args.args[0]))


class ToolRecoveryHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_excel_contracts.ExcelHTTPContractTests.asyncSetUp

    def malformed(self):
        native = transport({})
        native["arguments"] = json.dumps(
            {
                "code": '{"name":"exec_command","arguments":{"cmd":"echo "PRIVATE_ARGUMENT""}}'
            }
        )
        return native

    def serve(self, outputs, *, sse=True):
        self.responses = list(outputs)
        self.streams = []

        def respond(request):
            if self.streams:
                self.assertTrue(
                    self.streams[-1].closed,
                    "Previous response must close before correction",
                )
            self.requests.append(request)
            output = self.responses[
                min(len(self.requests) - 1, len(self.responses) - 1)
            ]
            if isinstance(output, Exception):
                raise output
            if isinstance(output, int):
                return httpx.Response(
                    output, json={"error": {"message": "PRIVATE_UPSTREAM"}}
                )
            payload = {
                "id": "resp_recovery",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            }
            if not sse:
                return httpx.Response(200, json=payload)
            stream = TrackedStream(
                responses_protocol.sse_encode(
                    "response.completed",
                    {"type": "response.completed", "response": payload},
                )
            )
            self.streams.append(stream)
            return httpx.Response(
                200, stream=stream, headers={"content-type": "text/event-stream"}
            )

        remote = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(remote.aclose)
        self.patches.enter_context(
            patch.object(proxy, "_get_excel_upstream_client", return_value=remote)
        )

    async def request(self, stream):
        return await self.local.post(
            "/v1/responses",
            json={
                "model": excel_upstream.MODEL_ID,
                "input": "test",
                "tools": [FUNCTION_TOOL],
                "stream": stream,
            },
        )

    async def test_ambiguous_bad_json_cannot_be_replaced_with_another_command(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.serve(
                    [
                        [self.malformed()],
                        [
                            transport(
                                {
                                    "name": "exec_command",
                                    "arguments": {"cmd": "echo recovered"},
                                }
                            )
                        ],
                    ]
                )
                result = await self.request(stream)
                self.assertEqual(result.status_code, 200 if stream else 502)
                self.assertEqual(len(self.requests), 2)
                self.assertNotIn("echo recovered", result.text)
                self.assertIn("excel_untranslatable_tool_call", result.text)
                self.assertNotIn("PRIVATE_ARGUMENT", result.text)
                retry = json.loads(self.requests[1].content)
                feedback = retry["input"][-1]
                self.assertEqual(feedback["type"], "function_call_output")
                self.assertIn("missing_comma", feedback["output"])
                self.assertNotIn("PRIVATE_ARGUMENT", feedback["output"])
                recovery = self.finish.call_args.args[0].trace_context[
                    "tool_call_recovery"
                ]
                self.assertEqual(recovery["outcome"], "rejected")
                self.assertEqual(
                    recovery["repair_diagnostics"]["reason"], "repair_changed_input"
                )
                self.assertEqual(recovery["attempts"], 1)
                self.assertEqual(recovery["usage"]["total_tokens"], 26)
                if stream:
                    self.assertEqual(result.text.count("event: response.failed"), 1)
                    self.assertNotIn("event: response.completed", result.text)

    async def test_second_bad_call_is_terminal_and_never_exposed(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.serve([[self.malformed()]])
                result = await self.request(stream)
                self.assertEqual(len(self.requests), 2)
                self.assertIn("excel_untranslatable_tool_call", result.text)
                self.assertNotIn("PRIVATE_ARGUMENT", result.text)
                self.assertNotIn("event: response.function_call_arguments", result.text)
                recovery = self.finish.call_args.args[0].trace_context[
                    "tool_call_recovery"
                ]
                self.assertEqual(recovery["outcome"], "rejected")
                if stream:
                    self.assertEqual(result.text.count("event: response.failed"), 1)
                else:
                    self.assertEqual(result.status_code, 502)

    async def test_all_reported_json_positions_reject_unprovable_source_changes(self):
        for line, column in ((1, 163), (1, 1269), (1, 1509), (7, 115), (14, 26)):
            prefix = '{"name":"exec_command","arguments":{"cmd":"' + "\n" * (line - 1)
            padding = column - 2 - (len(prefix) if line == 1 else 0)
            code = prefix + "x" * padding + '"PRIVATE_ARGUMENT"}}'
            native = {**self.malformed(), "arguments": json.dumps({"code": code})}
            diagnostic = {}
            self.assertIsNone(
                excel_upstream.extract_native_client_tool_calls(
                    {"output": [native]},
                    {"tools": [FUNCTION_TOOL]},
                    diagnostics=diagnostic,
                )
            )
            self.assertEqual(
                (diagnostic["json_line"], diagnostic["json_column"]), (line, column)
            )
            self.assertEqual(diagnostic["json_error"], "missing_comma")
            for stream in (False, True):
                with self.subTest(line=line, column=column, stream=stream):
                    self.requests.clear()
                    self.serve(
                        [
                            [native],
                            [
                                transport(
                                    {
                                        "name": "exec_command",
                                        "arguments": {
                                            "cmd": "line one\nline two\nline three"
                                        },
                                    }
                                )
                            ],
                        ]
                    )
                    result = await self.request(stream)
                    self.assertEqual(result.status_code, 200 if stream else 502)
                    self.assertIn("excel_untranslatable_tool_call", result.text)
                    self.assertNotIn("line one", result.text)
                    self.assertEqual(len(self.requests), 2)
                    feedback = json.loads(self.requests[1].content)["input"][-1][
                        "output"
                    ]
                    self.assertIn(
                        f"line={line}; column={column}; json=missing_comma", feedback
                    )
                    self.assertNotIn("PRIVATE_ARGUMENT", result.text)

    async def test_json_upstream_can_also_be_corrected(self):
        original = transport({})
        original["arguments"] = json.dumps({"code": "echo repaired"})
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.serve(
                    [
                        [original],
                        [
                            transport(
                                {
                                    "name": "exec_command",
                                    "arguments": {"cmd": "echo repaired"},
                                }
                            )
                        ],
                    ],
                    sse=False,
                )
                result = await self.request(stream)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(self.requests), 2)
                self.assertIn("echo repaired", result.text)
                if stream:
                    self.assertEqual(result.text.count("event: response.completed"), 1)

    async def test_valid_calls_do_not_trigger_corrective_requests(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.serve(
                    [
                        [
                            transport(
                                {
                                    "name": "exec_command",
                                    "arguments": {"cmd": "echo valid"},
                                }
                            )
                        ]
                    ]
                )
                result = await self.request(stream)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(self.requests), 1)
                self.assertNotIn(
                    "tool_call_recovery", self.finish.call_args.args[0].trace_context
                )

    async def test_unknown_first_tool_regenerates_once_against_current_catalog(self):
        unknown = transport(
            {"name": "PRIVATE_TOOL", "arguments": {"cmd": "PRIVATE_COMMAND"}}
        )
        direct = {
            **unknown,
            "name": "PRIVATE_NATIVE_TOOL",
            "arguments": '{"cmd":"PRIVATE_COMMAND"}',
        }
        corrected = transport(
            {"name": "exec_command", "arguments": {"cmd": "echo checked"}}
        )
        for rejected in (unknown, direct):
            for stream in (False, True):
                with self.subTest(direct=rejected is direct, stream=stream):
                    self.requests.clear()
                    self.serve([[rejected], [corrected]])
                    result = await self.request(stream)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(len(self.requests), 2)
                    self.assertIn("echo checked", result.text)
                    self.assertNotIn("PRIVATE", result.text)
                    first, retry = [
                        json.loads(request.content) for request in self.requests
                    ]
                    self.assertEqual(retry["input"][:-1], first["input"])
                    self.assertEqual(retry["model"], first["model"])
                    self.assertEqual(
                        self.requests[0].headers["authorization"],
                        self.requests[1].headers["authorization"],
                    )
                    self.assertNotIn("PRIVATE", json.dumps(retry))
                    recovery = self.finish.call_args.args[0].trace_context[
                        "tool_call_recovery"
                    ]
                    self.assertEqual(recovery["mode"], "unknown_tool_regeneration")
                    self.assertEqual(recovery["outcome"], "succeeded")
                    self.assertEqual(recovery["usage"]["total_tokens"], 26)
                    if stream:
                        self.assertEqual(
                            result.text.count(
                                "event: response.function_call_arguments.done"
                            ),
                            1,
                        )

    async def test_unknown_regeneration_is_disabled_after_tool_history_or_in_batches(
        self,
    ):
        unknown = transport({"name": "PRIVATE_TOOL", "arguments": {}})
        good = transport({"name": "exec_command", "arguments": {"cmd": "echo checked"}})
        good.update(id="fc_history_good", call_id="call_history_good")
        history = [
            {"role": "user", "content": "test"},
            {
                "type": "function_call",
                "id": "fc_previous",
                "call_id": "call_previous",
                "name": "exec_command",
                "arguments": '{"cmd":"echo done"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_previous",
                "output": "done",
            },
        ]
        for output, input_items in (([unknown], history), ([good, unknown], "test")):
            for stream in (False, True):
                with self.subTest(batch=len(output), stream=stream):
                    self.requests.clear()
                    self.serve([output, [good]])
                    result = await self.local.post(
                        "/v1/responses",
                        json={
                            "model": excel_upstream.MODEL_ID,
                            "input": input_items,
                            "tools": [FUNCTION_TOOL],
                            "stream": stream,
                        },
                    )
                    self.assertEqual(len(self.requests), 1)
                    self.assertIn("unknown_tool", result.text)
                    self.assertNotIn("echo checked", result.text)
                    self.assertNotIn(
                        "tool_call_recovery",
                        self.finish.call_args.args[0].trace_context,
                    )
                    diagnostic = self.finish.call_args.args[0].trace_context[
                        "tool_call_diagnostics"
                    ]
                    self.assertEqual(diagnostic["target_name"], "PRIVATE_TOOL")
                    self.assertEqual(
                        diagnostic["recovery_skipped"],
                        "tool_history" if len(output) == 1 else "tool_batch",
                    )

    async def test_unknown_internal_helper_regenerates_via_declared_custom_executor(
        self,
    ):
        code = 'text(await tools.exec_command({cmd: "echo checked"}));'
        unknown = {
            **transport({}),
            "name": "exec_command",
            "arguments": '{"cmd":"echo checked"}',
        }
        corrected = transport({})
        corrected["arguments"] = json.dumps(
            {
                "summary": "excel-proxy.raw/input/functions.exec",
                "code": code,
                "extended_summary": "{}",
            }
        )
        self.serve([[unknown], [corrected]])
        result = await self.local.post(
            "/v1/responses",
            json={
                "model": excel_upstream.MODEL_ID,
                "input": "Run the requested check",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "tools": [
                            {
                                "type": "custom",
                                "name": "exec",
                                "format": {"type": "text"},
                            },
                        ],
                    }
                ],
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.requests), 2)
        call = result.json()["output"][0]
        self.assertEqual(
            (call["type"], call["namespace"], call["name"], call["input"]),
            ("custom_tool_call", "functions", "exec", code),
        )

    async def test_unknown_regeneration_still_rejects_invalid_schema_and_multiple_calls(
        self,
    ):
        unknown = transport({"name": "PRIVATE_TOOL", "arguments": {}})
        invalid = transport({"name": "exec_command", "arguments": {"cmd": 123}})
        good = transport({"name": "exec_command", "arguments": {"cmd": "echo checked"}})
        for correction in ([unknown], [invalid], [good, good]):
            for stream in (False, True):
                with self.subTest(calls=len(correction), stream=stream):
                    self.requests.clear()
                    self.serve([[unknown], correction])
                    result = await self.request(stream)
                    self.assertEqual(len(self.requests), 2)
                    self.assertIn("unknown_tool", result.text)
                    self.assertNotIn("echo checked", result.text)
                    self.assertEqual(
                        self.finish.call_args.args[0].trace_context[
                            "tool_call_recovery"
                        ]["outcome"],
                        "rejected",
                    )

    async def test_duplicate_identities_are_rejected_without_repair(self):
        good = transport(
            {"name": "exec_command", "arguments": {"cmd": "PRIVATE_ARGUMENT"}}
        )
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.serve([[good, good]])
                result = await self.request(stream)
                self.assertEqual(len(self.requests), 1)
                self.assertIn("duplicate_tool_identity", result.text)
                self.assertNotIn("PRIVATE_ARGUMENT", result.text)

    async def test_repair_keeps_heartbeats_and_is_cancelled_with_stream(self):
        cancelled = asyncio.Event()

        async def repair(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def source():
            yield responses_protocol.sse_encode(
                "response.completed",
                {
                    "response": {"status": "completed", "output": [self.malformed()]},
                },
            )

        plan = proxy.UpstreamRequestPlan("heartbeat", "", {}, {}, {}, None, None)
        with (
            patch.object(
                excel_responses.ExcelResponseProcessor,
                "repair_tool_response",
                side_effect=repair,
            ),
            patch.object(excel_responses, "EXCEL_STREAM_HEARTBEAT_SECONDS", 0.01),
        ):
            stream = proxy._excel_response_processor().tool_stream_transform(
                {"tools": [FUNCTION_TOOL]}, trace_plan=plan
            )(source())
            heartbeat = await asyncio.wait_for(anext(stream), timeout=1)
            self.assertEqual(heartbeat, b": keep-alive\n\n")
            await stream.aclose()
        self.assertTrue(cancelled.is_set())

    async def test_cancelled_repair_closes_its_upstream_response(self):
        reading = asyncio.Event()
        closed = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                reading.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        remote = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    stream=WaitingStream(),
                    headers={"content-type": "text/event-stream"},
                )
            )
        )
        self.addAsyncCleanup(remote.aclose)
        source = {
            "model": excel_upstream.MODEL_ID,
            "input": "test",
            "tools": [FUNCTION_TOOL],
        }
        plan = proxy.UpstreamRequestPlan(
            "cancel",
            "https://example.test/responses",
            {},
            excel_upstream.prepare_responses_body(source),
            {},
            None,
            None,
        )
        response = {"status": "completed", "output": [self.malformed()]}
        diagnostic = {}
        excel_upstream.extract_native_client_tool_calls(
            response, source, diagnostics=diagnostic
        )
        with patch.object(proxy, "_get_excel_upstream_client", return_value=remote):
            task = asyncio.create_task(
                proxy._excel_response_processor().repair_tool_response(
                    plan, response, source, diagnostic
                )
            )
            try:
                await asyncio.wait_for(reading.wait(), timeout=1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(closed.is_set())
        self.assertEqual(
            plan.trace_context["tool_call_recovery"]["outcome"], "cancelled"
        )

    async def test_repair_does_not_replace_valid_sibling_calls(self):
        good = transport(
            {"name": "exec_command", "arguments": {"cmd": "echo original"}}
        )
        good.update(id="fc_good", call_id="call_good")
        corrected = transport(
            {"name": "exec_command", "arguments": {"cmd": "echo fixed"}}
        )
        original = transport({})
        original["arguments"] = json.dumps({"code": "echo fixed"})
        self.serve([[good, original], [corrected]])
        result = await self.request(False)
        self.assertEqual(result.status_code, 200)
        calls = result.json()["output"]
        self.assertEqual(
            [json.loads(call["arguments"])["cmd"] for call in calls],
            ["echo original", "echo fixed"],
        )

    async def test_repair_http_failure_is_not_retried_or_leaked(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.serve([[self.malformed()], 429])
                result = await self.request(stream)
                self.assertEqual(len(self.requests), 2)
                self.assertNotIn("PRIVATE_UPSTREAM", result.text)
                self.assertIn("excel_rate_limited", result.text)
                self.assertEqual(self.finish.call_args.args[1], 429)

    async def test_correction_401_does_not_replay_the_already_accepted_generation(self):
        self.serve([[self.malformed()], 401])
        with patch.object(
            proxy.proxy_accounts.proxy_account_store,
            "refresh_after_unauthorized",
            return_value=None,
        ) as refresh:
            result = await self.request(False)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(len(self.requests), 2)
        refresh.assert_not_called()

    async def test_repair_connection_and_timeout_failures_remain_distinct(self):
        for error, category in (
            (httpx.ConnectError("PRIVATE_ERROR"), "connection"),
            (httpx.ReadTimeout("PRIVATE_ERROR"), "timeout"),
        ):
            for stream in (False, True):
                with self.subTest(category=category, stream=stream):
                    self.requests.clear()
                    self.serve([[self.malformed()], error])
                    result = await self.request(stream)
                    self.assertNotIn("PRIVATE_ERROR", result.text)
                    self.assertIn(category, result.text)
                    recovery = self.finish.call_args.args[0].trace_context[
                        "tool_call_recovery"
                    ]
                    self.assertEqual(
                        recovery["failure_diagnosis"]["category"], category
                    )
