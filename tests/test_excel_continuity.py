import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

import excel_upstream
import format_translation
import test_excel_upstream


def native_call(index=0):
    return {
        "type": "function_call",
        "id": f"fc_continuity_{index}",
        "call_id": f"call_continuity_{index}",
        "name": "run_officejs",
        "status": "completed",
        "arguments": json.dumps({
            "summary": f"Read project step {index}",
            "extended_summary": f"Continue project step {index}",
            "code": json.dumps({"name": "shell_command", "arguments": {"command": f"read {index}"}}),
            "destructive": False,
            "references": [],
        }),
    }


class ExcelContinuityTests(unittest.TestCase):
    def setUp(self):
        self.streams = test_excel_upstream.ExcelStreamTransformTests()

    def completed_chunks(self, final_output=None):
        reasoning = {"type": "reasoning", "id": "rs_continuity", "summary": [], "encrypted_content": "opaque"}
        items = [reasoning, native_call()]
        chunks = [self.streams._sse("response.output_item.done", {
            "type": "response.output_item.done", "output_index": index, "item": item,
        }) for index, item in enumerate(items)]
        chunks.append(self.streams._sse("response.completed", {
            "type": "response.completed", "response": {
                "id": "resp_continuity", "status": "completed",
                "output": final_output if final_output is not None else [],
            },
        }))
        return chunks

    def test_stream_recovers_finished_items_missing_from_terminal_output(self):
        for final_output in ([], [native_call()]):
            with self.subTest(final_output=bool(final_output)):
                events = self.streams._collect(self.completed_chunks(final_output))
                output = dict(events)["response.completed"]["response"]["output"]
                self.assertEqual(output[0]["encrypted_content"], "opaque")
                self.assertEqual(output[1]["name"], "shell_command")
                self.assertNotIn("run_officejs", json.dumps(events))
                calls = [payload for name, payload in events if name == "response.function_call_arguments.done"]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["output_index"], 1)

    def test_stream_does_not_read_malformed_tail_after_completion(self):
        import proxy

        async def source():
            for chunk in self.completed_chunks([{"type": "reasoning", "id": "rs_continuity"}, native_call()]):
                yield chunk
            raise httpx.RemoteProtocolError("malformed HTTP tail")

        async def run():
            transform = proxy._excel_tool_stream_transform(self.streams.SOURCE_BODY)
            return b"".join([chunk async for chunk in transform(source())])

        raw = asyncio.run(run())
        self.assertEqual(raw.count(b"event: response.completed"), 1)

    def test_unfinished_stream_never_releases_native_tool_calls(self):
        import proxy

        async def run():
            async def source():
                for chunk in self.completed_chunks()[:-1]:
                    yield chunk
                yield b"data: [DONE]\n\n"

            chunks = []
            with self.assertRaises(httpx.RemoteProtocolError):
                async for chunk in proxy._excel_tool_stream_transform(self.streams.SOURCE_BODY)(source()):
                    chunks.append(chunk)
            raw = b"".join(chunks)
            self.assertNotIn(b"run_officejs", raw)
            self.assertNotIn(b"event: response.completed", raw)

        asyncio.run(run())

    def test_unknown_native_tool_is_a_protocol_error(self):
        unknown = {**native_call(), "name": "unknown_excel_tool"}
        chunks = [self.streams._sse("response.completed", {
            "response": {"status": "completed", "output": [unknown]},
        })]
        with self.assertRaises(httpx.RemoteProtocolError):
            self.streams._collect(chunks)

    def test_reasoning_only_completion_is_not_silent_success(self):
        for output in ([], [{"type": "reasoning", "summary": [], "encrypted_content": "opaque"}]):
            with self.subTest(output=bool(output)):
                chunks = [self.streams._sse("response.completed", {
                    "response": {"status": "completed", "output": output},
                })]
                with self.assertRaises(httpx.RemoteProtocolError):
                    self.streams._collect(chunks)

    def test_failed_response_does_not_execute_native_tool(self):
        events = self.streams._collect(self.completed_chunks()[:-1] + [self.streams._sse("response.failed", {
            "response": {"status": "failed", "output": [native_call()], "error": {"message": "upstream failed"}},
        })])
        self.assertEqual(dict(events)["response.failed"]["response"]["status"], "failed")
        self.assertNotIn("run_officejs", json.dumps(events))
        self.assertNotIn("response.completed", dict(events))

    def test_no_tool_stream_still_requires_terminal_event(self):
        with self.assertRaises(httpx.RemoteProtocolError):
            self.streams._collect(self.streams._delta_chunks("unfinished"), source_body={})

    def test_non_streaming_recovers_items_and_ignores_tail(self):
        import proxy

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(inner_self):
                for chunk in self.completed_chunks():
                    yield chunk
                raise httpx.RemoteProtocolError("malformed HTTP tail")

        async def run():
            response = httpx.Response(200, stream=Stream())
            try:
                return await proxy._read_excel_non_streaming_response_payload(response)
            finally:
                await response.aclose()

        payload = asyncio.run(run())
        self.assertEqual(payload["output"][1], native_call())
        self.assertEqual(payload["output"][0]["encrypted_content"], "opaque")

    def test_non_streaming_eof_is_a_protocol_error(self):
        import proxy

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: [DONE]\n\n"

        async def run():
            response = httpx.Response(200, stream=Stream())
            try:
                with self.assertRaises(httpx.RemoteProtocolError):
                    await proxy._read_excel_non_streaming_response_payload(response)
            finally:
                await response.aclose()

        asyncio.run(run())

    def test_non_streaming_protocol_error_closes_without_replay(self):
        import proxy

        class Stream(httpx.AsyncByteStream):
            def __init__(self, chunks):
                self.chunks = chunks

            async def __aiter__(self):
                for chunk in self.chunks:
                    yield chunk

        plan = proxy.UpstreamRequestPlan(
            request_id="continuity-test", upstream_url=excel_upstream.RESPONSES_URL,
            headers={}, body={"input": "same request"}, usage_event=None,
            requested_model=excel_upstream.MODEL_ID, resolved_model=excel_upstream.MODEL_ID,
        )
        client = Mock()
        for chunks in ([], [b"data: [DONE]\n\n"]):
            with self.subTest(chunks=chunks):
                reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(chunks))
                send = AsyncMock(return_value=reply)
                with patch.object(proxy, "_get_excel_upstream_client", return_value=client), \
                     patch.object(proxy, "throttled_client_send", send), \
                     patch.object(proxy, "_finish_usage_and_trace"):
                    result = asyncio.run(proxy._post_excel_non_streaming_request(plan, client_body=self.streams.SOURCE_BODY))
                self.assertEqual(send.await_count, 1)
                self.assertTrue(reply.is_closed)
                self.assertEqual(result.status_code, 502)
                requests = client.build_request.call_args_list
                self.assertTrue(all(call.kwargs["json"] == plan.body for call in requests))

    def compact(self, payload, status_code=200):
        import proxy

        body = {"model": "gpt-5.6-sol-excel", "stream": True,
                "input": [{"role": "user", "content": "Finish the project"}],
                "tools": self.streams.SOURCE_BODY["tools"]}
        handler = AsyncMock(return_value=JSONResponse(payload, status_code=status_code))
        request = Request({"type": "http", "method": "POST", "path": "/responses/compact", "headers": []})
        with patch.object(proxy, "parse_json_request", AsyncMock(return_value=body)), \
             patch.object(proxy, "_handle_excel_responses", handler):
            response = asyncio.run(proxy.responses_compact(request))
        return response, handler.call_args.args[1]

    def test_compaction_returns_replayable_summary_without_tools(self):
        summary = "Read source and fixed parser. Next: run tests and finish UI."
        response, summary_request = self.compact({
            "id": "resp_summary", "status": "completed", "output": [{
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": summary}],
            }],
        })
        self.assertFalse(summary_request["stream"])
        self.assertFalse(summary_request.get("tools"))
        compacted = json.loads(response.body)
        self.assertEqual(compacted["object"], "response.compaction")
        self.assertEqual(compacted["output"][0]["content"], "Finish the project")
        compact = compacted["output"][-1]
        self.assertEqual(compact["type"], "compaction")
        self.assertEqual(format_translation.decode_fake_compaction(compact["encrypted_content"]), summary)
        history = [
            {"role": "user", "content": "Finish the project"},
            {"role": "assistant", "content": "old details"},
            compact,
            {"role": "user", "content": "Continue with the tests"},
        ]
        prepared = excel_upstream.prepare_responses_body({"input": history})
        rendered = json.dumps(prepared["input"])
        self.assertIn(summary, rendered)
        self.assertNotIn("old details", rendered)
        self.assertNotIn(compact["encrypted_content"], rendered)

    def test_failed_or_empty_compaction_does_not_discard_history(self):
        for payload in (
            {"status": "completed", "output": []},
            {"status": "incomplete", "output_text": "partial summary"},
            {"status": "failed", "error": {"message": "upstream failed"}},
        ):
            with self.subTest(status=payload["status"]):
                response, _ = self.compact(payload)
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("compaction", [item.get("type") for item in json.loads(response.body).get("output", [])])

    def test_compaction_preserves_authentication_failure(self):
        response, _ = self.compact({"error": {"message": "session expired"}}, status_code=401)
        self.assertEqual(response.status_code, 401)

    def test_http_tool_compaction_and_continuation_round_trip(self):
        import proxy

        seen = []
        summary = "Inspected the parser. Next run the tests, then finish."

        def upstream(request):
            self.assertEqual(str(request.url), excel_upstream.RESPONSES_URL)
            body = json.loads(request.content)
            seen.append(body)
            if len(seen) == 2:
                self.assertFalse(body["stream"])
                output = [{"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": summary}]}]
            else:
                if len(seen) == 3:
                    self.assertIn(summary, json.dumps(body["input"]))
                    self.assertFalse(any(item.get("type") == "compaction" for item in body["input"]))
                output = [native_call(1000 + len(seen))]
            chunks = [self.streams._sse("response.output_item.done", {
                "output_index": index, "item": item,
            }) for index, item in enumerate(output)]
            chunks.append(self.streams._sse("response.completed", {
                "response": {"id": f"resp_http_{len(seen)}", "status": "completed", "output": []},
            }))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(chunks))

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote, \
                       httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1") as local:
                with patch.object(proxy, "_get_excel_upstream_client", return_value=remote), \
                     patch.object(proxy.excel_session_capture, "refresh_macos_excel_session"), \
                     patch.object(proxy.excel_session_capture, "refresh_windows_excel_session"), \
                     patch.object(excel_upstream.excel_session_store, "request_headers", return_value={}), \
                     patch.object(excel_upstream.excel_session_store, "tools_version_id", return_value=None):
                    body = {"model": excel_upstream.MODEL_ID, "stream": True, "tools": self.streams.SOURCE_BODY["tools"],
                            "prompt_cache_key": "http-continuity", "input": [{"type": "message", "role": "user", "content": "Finish the project"}]}
                    first = await local.post("/v1/responses", json=body)
                    self.assertEqual(first.status_code, 200)
                    completed = await proxy._read_excel_non_streaming_response_payload(first)
                    tool = completed["output"][0]
                    self.assertEqual(tool["name"], "shell_command")
                    body["input"].extend([tool, {"type": "function_call_output", "call_id": tool["call_id"], "output": "read completed"}])
                    compact = await local.post("/v1/responses/compact", json=body)
                    self.assertEqual(compact.status_code, 200)
                    self.assertEqual(compact.json()["object"], "response.compaction")
                    body["input"] = compact.json()["output"]
                    continued = await local.post("/v1/responses", json=body)
                    self.assertEqual(continued.status_code, 200)
                    completed = await proxy._read_excel_non_streaming_response_payload(continued)
                    self.assertEqual(completed["output"][0]["name"], "shell_command")
                    self.assertEqual(len(seen), 3)
                    self.assertEqual(seen[0]["metadata"]["task_id"], seen[2]["metadata"]["task_id"])

        asyncio.run(run())

    def test_turn_identity_ignores_changing_client_metadata(self):
        source = {"input": [{"role": "user", "content": "Finish the project",
                             "internal_chat_message_metadata_passthrough": {"turn_id": "first", "executed_tools": []}}]}
        initial = excel_upstream.prepare_responses_body(source)
        source["input"][0]["internal_chat_message_metadata_passthrough"] = {"turn_id": "first", "executed_tools": ["read_file"]}
        source["input"].append({"type": "function_call_output", "call_id": "call_result", "output": "done"})
        continued = excel_upstream.prepare_responses_body(source)
        self.assertEqual(initial["metadata"]["turn_id"], continued["metadata"]["turn_id"])
        self.assertEqual(initial["metadata"]["task_id"], continued["metadata"]["task_id"])
        self.assertEqual(continued["metadata"]["agent_iteration"], "2")

    def test_600_calls_replay_exactly_after_eviction_and_memory_reset(self):
        native_items = [native_call(index) for index in range(600)]
        client_items = []
        for item in native_items:
            call = excel_upstream.extract_native_client_tool_call({"output": [item]}, self.streams.SOURCE_BODY)
            self.assertIsNotNone(call)
            client_items.append(call)
        snapshot = copy.deepcopy(client_items)
        self.assertLessEqual(len(excel_upstream._native_call_cache), 512)
        self.assertEqual(excel_upstream.translate_input_items(client_items), native_items)
        with excel_upstream._native_call_cache_lock:
            excel_upstream._native_call_cache.clear()
        self.assertEqual(excel_upstream.translate_input_items(client_items), native_items)
        self.assertEqual(client_items, snapshot)
        code = (
            "import hashlib,json,sys,excel_upstream; "
            "items=excel_upstream.translate_input_items(json.load(sys.stdin)); "
            "print(hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest())"
        )
        process = subprocess.run([sys.executable, "-c", code], input=json.dumps(client_items),
                                 capture_output=True, text=True, check=True, timeout=30)
        expected = hashlib.sha256(json.dumps(native_items, sort_keys=True).encode()).hexdigest()
        self.assertEqual(process.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
