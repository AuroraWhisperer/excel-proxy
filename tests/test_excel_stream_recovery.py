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
import test_excel_contracts
import test_excel_upstream
from test_excel_continuity import native_call


class ExcelStreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    source_body = test_excel_upstream.ExcelStreamTransformTests.SOURCE_BODY

    def frame(self, event, **payload):
        return responses_protocol.sse_encode(event, {"type": event, **payload})

    async def collect(self, chunks, *, broken=False, body=None):
        async def source():
            for chunk in chunks:
                yield chunk
            if broken:
                raise httpx.RemoteProtocolError("PRIVATE transport detail")

        result = []
        async for chunk in proxy._excel_response_processor().tool_stream_transform(
            body or self.source_body
        )(source()):
            result.append(chunk)
        events = []
        for block in b"".join(result).decode().split(chr(10) * 2):
            name, data = responses_protocol.parse_sse_block(block)
            if data:
                events.append((name, json.loads(data)))
        return events

    def done_items(self, items):
        chunks = [
            self.frame(
                "response.created",
                response={"id": "resp_recovery", "status": "in_progress", "output": []},
            )
        ]
        for index, item in enumerate(items):
            chunks += [
                self.frame(
                    "response.output_item.added",
                    output_index=index,
                    item={**item, "status": "in_progress"},
                ),
                self.frame("response.output_item.done", output_index=index, item=item),
            ]
        return chunks

    async def test_all_native_calls_are_converted_in_order(self):
        reasoning = {
            "type": "reasoning",
            "id": "rs_two",
            "summary": [],
            "encrypted_content": "opaque",
        }
        items = [reasoning, native_call(1), native_call(2)]
        events = await self.collect(
            self.done_items(items)
            + [
                self.frame(
                    "response.completed",
                    response={"status": "completed", "output": items},
                )
            ]
        )
        calls = [p for n, p in events if n == "response.function_call_arguments.done"]
        self.assertEqual([c["output_index"] for c in calls], [1, 2])
        completed = [p for n, p in events if n == "response.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(
            [c["name"] for c in completed[0]["response"]["output"][1:]],
            ["shell_command", "shell_command"],
        )
        self.assertNotIn("run_officejs", json.dumps(events))

    def test_singular_payload_replaces_only_first_call(self):
        originals = [native_call(1), native_call(2)]
        converted = excel_upstream.extract_native_client_tool_call(
            {"output": [originals[0]]},
            self.source_body,
        )
        payload = excel_upstream.response_payload_with_tool_call(
            {"output": originals},
            converted,
        )
        self.assertEqual(payload["output"][0], {**converted, "status": "completed"})
        self.assertEqual(payload["output"][1], originals[1])

    async def test_invalid_second_call_does_not_release_first(self):
        items = [native_call(3), {**native_call(4), "name": "unknown_tool"}]
        emitted = []

        async def source():
            yield self.frame(
                "response.completed", response={"status": "completed", "output": items}
            )

        with self.assertRaises(httpx.RemoteProtocolError):
            async for chunk in proxy._excel_response_processor().tool_stream_transform(
                self.source_body
            )(source()):
                emitted.append(chunk)
        self.assertNotIn(b"response.function_call_arguments.done", b"".join(emitted))

    async def test_parallel_false_rejects_multiple_calls_without_dropping_work(self):
        with self.assertRaises(httpx.RemoteProtocolError):
            await self.collect(
                [
                    self.frame(
                        "response.completed",
                        response={"output": [native_call(1), native_call(2)]},
                    )
                ],
                body={**self.source_body, "parallel_tool_calls": False},
            )

    async def test_unterminated_terminal_frame_survives_transport_error(self):
        frame = self.frame(
            "response.completed",
            response={"status": "completed", "output": [native_call()]},
        )
        events = await self.collect([frame.rstrip(bytes([10]))], broken=True)
        self.assertEqual(events[-1][0], "response.completed")

    async def test_completed_calls_recover_with_response_identity(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                events = await self.collect(
                    self.done_items([native_call(5), native_call(6)]), broken=broken
                )
                self.assertEqual(events[-1][0], "response.completed")
                self.assertEqual(len(events[-1][1]["response"]["output"]), 2)

    async def test_commentary_or_unfinished_items_do_not_recover(self):
        commentary = {
            "type": "message",
            "id": "msg_comment",
            "status": "completed",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "I will do it"}],
        }
        cases = [
            self.done_items([commentary]),
            self.done_items([native_call()])[:-1],
            self.done_items([native_call()])
            + [
                self.frame(
                    "response.output_item.added", output_index=1, item=native_call(1)
                )
            ],
        ]
        for chunks in cases:
            with (
                self.subTest(chunks=len(chunks)),
                self.assertRaises(httpx.RemoteProtocolError),
            ):
                await self.collect(chunks)

    async def test_idle_stream_heartbeats_and_closes_pending_read(self):
        closed = asyncio.Event()

        async def source():
            try:
                yield self.frame(
                    "response.created",
                    response={"id": "resp_idle", "status": "in_progress", "output": []},
                )
                await asyncio.Event().wait()
            finally:
                closed.set()

        with patch.object(
            excel_responses, "EXCEL_STREAM_HEARTBEAT_SECONDS", 0.01, create=True
        ):
            stream = proxy._excel_response_processor().tool_stream_transform(
                self.source_body
            )(source())
            await anext(stream)
            try:
                heartbeat = await asyncio.wait_for(anext(stream), 0.2)
                self.assertIn(b"response.in_progress", heartbeat)
            finally:
                await stream.aclose()
            await asyncio.wait_for(closed.wait(), 0.2)

    def test_parallel_results_count_as_one_agent_iteration(self):
        body = {
            "input": [
                {"role": "user", "content": "test"},
                native_call(1),
                native_call(2),
                {
                    "type": "function_call_output",
                    "call_id": "call_continuity_1",
                    "output": "one",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_continuity_2",
                    "output": "two",
                },
            ]
        }
        self.assertEqual(
            excel_upstream.prepare_responses_body(body)["metadata"]["agent_iteration"],
            "2",
        )

    def test_plural_conversion_replays_each_original_identity(self):
        originals = [native_call(10), native_call(11)]
        converted = excel_upstream.extract_native_client_tool_calls(
            {"output": originals}, self.source_body
        )
        self.assertEqual(len(converted), 2)
        body = {
            **self.source_body,
            "input": [
                {"role": "user", "content": "test"},
                *converted,
                *[
                    {
                        "type": "function_call_output",
                        "call_id": c["call_id"],
                        "output": "ok",
                    }
                    for c in converted
                ],
            ],
        }
        replay = excel_upstream.prepare_responses_body(body)
        calls = [c for c in replay["input"] if c.get("type") == "function_call"]
        self.assertEqual(
            [c["arguments"] for c in calls], [c["arguments"] for c in originals]
        )

    async def test_incomplete_and_duplicate_calls_are_not_executable(self):
        first, second = native_call(20), native_call(21)
        cases = [
            [first, {**second, "call_id": first["call_id"]}],
            [first, {**second, "id": first["id"]}],
            [{**first, "status": "in_progress"}],
        ]
        for items in cases:
            with (
                self.subTest(items=items),
                self.assertRaises(httpx.RemoteProtocolError),
            ):
                await self.collect(
                    [self.frame("response.completed", response={"output": items})]
                )

    async def test_mixed_custom_and_function_calls_keep_indices(self):
        body = copy.deepcopy(self.source_body)
        body["tools"].append(
            {"type": "custom", "name": "custom_probe", "format": {"type": "text"}}
        )
        custom = {
            "type": "custom_tool_call",
            "name": "custom_probe",
            "input": "check",
            "call_id": "call_custom_probe",
            "id": "ctc_custom_probe",
            "status": "completed",
        }
        events = await self.collect(
            [
                self.frame(
                    "response.completed", response={"output": [native_call(22), custom]}
                )
            ],
            body=body,
        )
        done = [data for event, data in events if event == "response.output_item.done"]
        self.assertEqual([item["output_index"] for item in done], [0, 1])
        self.assertEqual(
            [item["item"]["type"] for item in done],
            ["function_call", "custom_tool_call"],
        )
        self.assertEqual(sum(event == "response.completed" for event, _ in events), 1)

    async def test_recovered_response_preserves_id_and_final_answer(self):
        message = {
            "type": "message",
            "id": "msg_final",
            "role": "assistant",
            "status": "completed",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "All done"}],
        }
        for broken in (False, True):
            events = await self.collect(self.done_items([message]), broken=broken)
            response = events[-1][1]["response"]
            self.assertEqual(response["id"], "resp_recovery")
            self.assertEqual(response["output"][0]["content"][0]["text"], "All done")

    async def test_failed_terminal_never_releases_completed_calls(self):
        chunks = self.done_items([native_call(23)])
        chunks.append(
            self.frame(
                "response.failed",
                response={
                    "status": "failed",
                    "error": {"message": "PRIVATE"},
                    "output": [native_call(23)],
                },
            )
        )
        events = await self.collect(chunks)
        self.assertEqual(events[-1][0], "response.failed")
        self.assertNotIn("PRIVATE", json.dumps(events))
        self.assertFalse(
            any(
                event in {"response.completed", "response.function_call_arguments.done"}
                for event, _ in events
            )
        )

    async def test_missing_identity_gaps_and_malformed_tail_do_not_recover(self):
        chunks = self.done_items([native_call(24)])
        cases = [
            chunks[1:],
            chunks + [b"data: {partial"],
            [
                chunks[0],
                self.frame(
                    "response.output_item.done", output_index=1, item=native_call(24)
                ),
            ],
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(httpx.RemoteProtocolError):
                await self.collect(case)

    async def test_heartbeat_before_response_and_cancellation_close_source(self):
        closed = asyncio.Event()

        async def source():
            try:
                await asyncio.Event().wait()
                yield b""
            finally:
                closed.set()

        with patch.object(excel_responses, "EXCEL_STREAM_HEARTBEAT_SECONDS", 0.01):
            stream = proxy._excel_response_processor().tool_stream_transform(
                self.source_body
            )(source())
            try:
                heartbeat = await asyncio.wait_for(anext(stream), 1)
                self.assertTrue(heartbeat.startswith(b": keep-alive"))
            finally:
                await stream.aclose()
            await asyncio.wait_for(closed.wait(), 1)

    async def test_nonstream_parser_recovers_only_completed_items(self):
        raw = b"".join(self.done_items([native_call(25), native_call(26)]))
        response = await proxy._excel_response_processor().read_response_payload(
            httpx.Response(200, content=raw)
        )
        self.assertEqual(response["id"], "resp_recovery")
        self.assertEqual(len(response["output"]), 2)


class ExcelBatchHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_excel_contracts.ExcelHTTPContractTests.asyncSetUp

    async def test_json_and_sse_modes_convert_the_entire_batch(self):
        self.upstream_status = 200
        self.upstream_body = {
            "id": "resp_batch_http",
            "status": "completed",
            "output": [native_call(30), native_call(31)],
        }
        body = {**ExcelStreamRecoveryTests.source_body, "input": "Run two checks"}
        for upstream_sse, downstream_stream in (
            (False, False),
            (True, False),
            (True, True),
            (False, True),
        ):
            with self.subTest(upstream_sse=upstream_sse, stream=downstream_stream):
                self.upstream_sse = (
                    responses_protocol.sse_encode(
                        "response.completed",
                        {"type": "response.completed", "response": self.upstream_body},
                    )
                    if upstream_sse
                    else None
                )
                result = await self.local.post(
                    "/v1/responses", json={**body, "stream": downstream_stream}
                )
                self.assertEqual(result.status_code, 200, result.text)
                if downstream_stream:
                    self.assertTrue(
                        result.headers["content-type"].startswith("text/event-stream")
                    )
                    completed = [
                        json.loads(data)
                        for block in result.text.split(chr(10) * 2)
                        for event, data in [responses_protocol.parse_sse_block(block)]
                        if event == "response.completed"
                    ]
                    self.assertEqual(len(completed), 1)
                    response = completed[0]["response"]
                else:
                    response = result.json()
                self.assertEqual(
                    [item["name"] for item in response["output"]],
                    ["shell_command", "shell_command"],
                )
                self.assertNotIn("run_officejs", json.dumps(response))

    async def test_json_upstream_emits_text_and_tracks_completion(self):
        self.upstream_status = 200
        self.upstream_body = {
            "id": "resp_json",
            "status": "completed",
            "output": [
                {
                    "id": "msg_json",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "JSON answer",
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        }
        result = await self.local.post(
            "/v1/responses",
            json={
                "model": excel_upstream.MODEL_ID,
                "input": "test",
                "stream": True,
            },
        )
        self.assertTrue(result.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(result.text.count("event: response.completed"), 1)
        self.assertEqual(result.text.count("event: response.output_text.delta"), 1)
        self.assertIn("JSON answer", result.text)
        self.assertNotIn("event: response.failed", result.text)
        self.assertEqual(self.finish.call_args.args[1], 200)
        self.assertEqual(self.finish.call_args.kwargs["usage"]["total_tokens"], 7)

    async def test_json_failed_or_invalid_payload_never_dispatches_calls(self):
        self.upstream_status = 200
        for payload in (
            [],
            {"status": "in_progress", "output": [native_call(34)]},
            {
                "status": "failed",
                "output": [native_call(34)],
                "error": {"message": "PRIVATE"},
            },
            {"status": "incomplete", "output": [native_call(34)]},
        ):
            with self.subTest(payload_type=type(payload).__name__):
                self.upstream_body = payload
                result = await self.local.post(
                    "/v1/responses",
                    json={
                        **ExcelStreamRecoveryTests.source_body,
                        "input": "test",
                        "stream": True,
                    },
                )
                self.assertTrue(
                    result.headers["content-type"].startswith("text/event-stream")
                )
                self.assertNotIn("event: response.completed", result.text)
                self.assertNotIn("response.function_call_arguments.done", result.text)
                self.assertNotIn("PRIVATE", result.text)
                self.assertNotIn("run_officejs", result.text)

    async def test_invalid_batch_returns_failure_without_partial_tool_work(self):
        self.upstream_status = 200
        payload = {
            "id": "resp_bad_batch",
            "status": "completed",
            "output": [native_call(32), {**native_call(33), "name": "unknown"}],
        }
        self.upstream_sse = responses_protocol.sse_encode(
            "response.completed", {"response": payload}
        )
        for stream in (False, True):
            result = await self.local.post(
                "/responses",
                json={
                    **ExcelStreamRecoveryTests.source_body,
                    "input": "test",
                    "stream": stream,
                },
            )
            if stream:
                self.assertIn("response.failed", result.text)
                self.assertNotIn("response.function_call_arguments.done", result.text)
                self.assertNotIn("response.completed", result.text)
            else:
                self.assertEqual(result.status_code, 502)
