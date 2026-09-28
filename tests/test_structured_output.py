"""Codex metadata requests must receive validated JSON through the Excel bridge."""

import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx

import excel_upstream
import responses_protocol
import proxy
import rate_limiting


TITLE_FORMAT = {
    "type": "json_schema",
    "name": "codex_metadata",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 36},
            "description": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["title", "description"],
        "additionalProperties": False,
    },
}
TITLE = {"title": "修复自动标题", "description": "兼容 Excel 对话自动命名"}


def message(text):
    return {
        "type": "message",
        "id": "msg_title",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def response_stream(output, *, status="completed", terminal=True):
    response = {"id": "resp_title", "status": status, "output": output}
    events = [
        (
            "response.created",
            {"response": {**response, "status": "in_progress", "output": []}},
        )
    ]
    for index, item in enumerate(output):
        events.extend(
            [
                (
                    "response.output_item.added",
                    {"output_index": index, "item": {**item, "content": []}},
                ),
                (
                    "response.output_text.delta",
                    {
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "delta": "UNVALIDATED_DELTA",
                    },
                ),
                ("response.output_item.done", {"output_index": index, "item": item}),
            ]
        )
    if terminal:
        events.append((f"response.{status}", {"response": response}))
    return b"".join(
        responses_protocol.sse_encode(kind, {"type": kind, **data})
        for kind, data in events
    )


class StructuredRequestTests(unittest.TestCase):
    def test_title_schema_is_translated_without_changing_the_client_request(self):
        source = {
            "model": "gpt-6-astra-excel",
            "input": "Give this task a title.",
            "text": {"format": copy.deepcopy(TITLE_FORMAT)},
        }
        before = copy.deepcopy(source)
        body = excel_upstream.prepare_responses_body(source)
        self.assertEqual(source, before)
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertNotIn("text", body)
        instructions = "\n".join(
            part["text"]
            for item in body["input"]
            if item.get("role") == "developer"
            for part in item["content"]
            if part.get("type") == "input_text"
        )
        self.assertIn("JSON", instructions)
        self.assertIn(
            json.dumps(TITLE_FORMAT, ensure_ascii=False, separators=(",", ":")),
            instructions,
        )

    def test_invalid_formats_are_rejected_before_generation(self):
        invalid = [
            "json",
            {"type": "xml"},
            {"type": "json_schema", "name": "title"},
            {**TITLE_FORMAT, "strict": "true"},
            {**TITLE_FORMAT, "name": "bad name"},
            {**TITLE_FORMAT, "schema": {"type": "not-a-type"}},
            {**TITLE_FORMAT, "schema": {"$ref": "https://example.invalid/schema"}},
            {**TITLE_FORMAT, "schema": {"$ref": "file:///private/schema.json"}},
        ]
        for fmt in invalid:
            with (
                self.subTest(format=fmt),
                self.assertRaises(excel_upstream.ExcelRequestError) as caught,
            ):
                excel_upstream.prepare_responses_body(
                    {"input": "title", "text": {"format": fmt}}
                )
            self.assertEqual(caught.exception.param, "text.format")

    def test_local_schema_references_and_metadata_summary_fields_are_supported(self):
        fmt = {
            **TITLE_FORMAT,
            "schema": {
                "type": "object",
                "$defs": {"short_text": {"type": "string", "maxLength": 60}},
                "properties": {
                    "summary": {"type": "string"},
                    "compactSummary": {"$ref": "#/$defs/short_text"},
                },
                "required": ["summary", "compactSummary"],
                "additionalProperties": False,
            },
        }
        excel_upstream.prepare_responses_body(
            {"input": "Summarize the turn.", "text": {"format": fmt}}
        )
        import structured_output

        structured_output.validate_response(
            {
                "output": [
                    message(
                        json.dumps(
                            {
                                "summary": "修复标题请求并验证",
                                "compactSummary": "标题修复",
                            }
                        )
                    )
                ]
            },
            fmt,
        )


class StructuredHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.output = [message(json.dumps(TITLE, ensure_ascii=False))]
        self.status = "completed"
        self.terminal = True
        self.json_upstream = False

        def upstream(request):
            self.requests.append(json.loads(request.content))
            if self.json_upstream:
                return httpx.Response(
                    200,
                    json={
                        "id": "resp_title",
                        "status": self.status,
                        "output": self.output,
                    },
                )
            return httpx.Response(
                200,
                content=response_stream(
                    self.output, status=self.status, terminal=self.terminal
                ),
                headers={"content-type": "text/event-stream"},
            )

        self.remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.local = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.remote.aclose)
        self.addAsyncCleanup(self.local.aclose)
        patches = self.enterContext(ExitStack())
        patches.enter_context(
            patch.object(proxy, "_get_excel_upstream_client", return_value=self.remote)
        )
        patches.enter_context(
            patch.object(
                proxy,
                "_selected_excel_headers",
                new=AsyncMock(
                    return_value={
                        "authorization": "Bearer test",
                        "chatgpt-account-id": "test-account",
                    }
                ),
            )
        )
        patches.enter_context(
            patch.object(
                excel_upstream.excel_session_store,
                "tools_version_id",
                return_value=None,
            )
        )
        patches.enter_context(patch.object(proxy, "_finish_usage_and_trace"))
        patches.enter_context(
            patch.object(rate_limiting, "throttle_upstream_request", new=AsyncMock())
        )

    async def request(
        self, *, stream=False, path="/responses", fmt=TITLE_FORMAT, **kwargs
    ):
        return await self.local.post(
            path,
            json={
                "model": "gpt-6-astra-excel",
                "input": "Generate a title and description for fixing Excel titles.",
                "stream": stream,
                "text": {"format": fmt},
                **kwargs,
            },
        )

    def events(self, response):
        return [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]

    async def test_title_is_returned_on_both_routes_and_response_modes(self):
        for path in ("/responses", "/v1/responses"):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    response = await self.request(path=path, stream=stream)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertNotIn("UNVALIDATED_DELTA", response.text)
                    if stream:
                        events = self.events(response)
                        payload = next(
                            e["response"]
                            for e in events
                            if e["type"] == "response.completed"
                        )
                        deltas = [
                            e["delta"]
                            for e in events
                            if e["type"] == "response.output_text.delta"
                        ]
                        self.assertEqual(deltas, [self.output[0]["content"][0]["text"]])
                        self.assertEqual(
                            sum(
                                e["type"] == "response.output_item.done" for e in events
                            ),
                            1,
                        )
                    else:
                        payload = response.json()
                    self.assertEqual(
                        json.loads(
                            responses_protocol.extract_response_output_text(payload)
                        ),
                        TITLE,
                    )
        self.assertEqual(len(self.requests), 4)

    async def test_invalid_answers_are_never_exposed_or_retried(self):
        for answer in (
            "PRIVATE_INVALID",
            '{"title":"missing description"}',
            json.dumps({**TITLE, "extra": "PRIVATE_INVALID"}),
            json.dumps({**TITLE, "title": "x" * 37}),
            '{"title":NaN,"description":"bad"}',
        ):
            self.output = [message(answer)]
            for stream in (False, True):
                with self.subTest(answer=answer, stream=stream):
                    before = len(self.requests)
                    response = await self.request(stream=stream)
                    self.assertEqual(
                        response.status_code, 200 if stream else 502, response.text
                    )
                    self.assertIn("excel_invalid_structured_output", response.text)
                    self.assertNotIn("PRIVATE_INVALID", response.text)
                    self.assertNotIn("UNVALIDATED_DELTA", response.text)
                    self.assertNotIn("response.completed", response.text)
                    if stream:
                        self.assertFalse(
                            any(
                                e["type"].startswith("response.output_text.")
                                for e in self.events(response)
                            )
                        )
                    self.assertEqual(len(self.requests), before + 1)

    async def test_json_upstream_and_json_object_mode_are_validated(self):
        self.json_upstream = True
        for stream in (False, True):
            for fmt in (TITLE_FORMAT, {"type": "json_object"}):
                with self.subTest(format=fmt, stream=stream):
                    result = await self.request(fmt=fmt, stream=stream)
                    self.assertEqual(result.status_code, 200, result.text)
                    if stream:
                        self.assertEqual(
                            sum(
                                e["type"] == "response.output_text.delta"
                                for e in self.events(result)
                            ),
                            1,
                        )
        self.output = [message("[]")]
        result = await self.request(fmt={"type": "json_object"})
        self.assertEqual(result.status_code, 502)

    async def test_refusal_remains_a_refusal(self):
        self.output[0]["content"] = [
            {"type": "refusal", "refusal": "Cannot fulfill this request."}
        ]
        for stream in (False, True):
            result = await self.request(stream=stream)
            self.assertEqual(result.status_code, 200, result.text)
            payload = (
                next(
                    e["response"]
                    for e in self.events(result)
                    if e["type"] == "response.completed"
                )
                if stream
                else result.json()
            )
            self.assertEqual(payload["output"][0]["content"], self.output[0]["content"])

    async def test_structured_requests_keep_native_tool_continuations(self):
        self.output = [
            {
                "type": "function_call",
                "id": "fc_read",
                "call_id": "call_read",
                "name": "run_officejs",
                "arguments": json.dumps(
                    {
                        "code": json.dumps(
                            {"name": "read_file", "arguments": {"path": "README.md"}}
                        )
                    }
                ),
            }
        ]
        tools = [
            {
                "type": "function",
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        for stream in (False, True):
            result = await self.request(stream=stream, tools=tools)
            self.assertEqual(result.status_code, 200, result.text)
            payload = (
                next(
                    e["response"]
                    for e in self.events(result)
                    if e["type"] == "response.completed"
                )
                if stream
                else result.json()
            )
            self.assertEqual(payload["output"][0]["name"], "read_file")
            self.assertEqual(
                json.loads(payload["output"][0]["arguments"]), {"path": "README.md"}
            )
        self.assertEqual(len(self.requests), 2)

    async def test_failed_and_interrupted_streams_do_not_leak_partial_json(self):
        self.output = [message("PRIVATE_PARTIAL")]
        for status, terminal in (
            ("failed", True),
            ("incomplete", True),
            ("in_progress", False),
        ):
            self.status, self.terminal = status, terminal
            with self.subTest(status=status):
                result = await self.request(stream=True)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertNotIn("PRIVATE_PARTIAL", result.text)
                self.assertNotIn("UNVALIDATED_DELTA", result.text)
                self.assertNotIn("response.completed", result.text)

    async def test_structured_messages_wait_for_completion_but_reasoning_passes_through(
        self,
    ):
        reached_terminal = False
        summary = {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_title",
            "output_index": 0,
            "summary_index": 0,
            "delta": "**Checking title**\n\nChecking the format.",
        }

        async def source():
            nonlocal reached_terminal
            yield responses_protocol.sse_encode(summary["type"], summary)
            yield responses_protocol.sse_encode(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "delta": "PRIVATE_PENDING",
                    "item_id": "msg_title",
                    "output_index": 1,
                    "content_index": 0,
                },
            )
            reached_terminal = True
            yield responses_protocol.sse_encode(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_title",
                        "status": "completed",
                        "output": self.output,
                    },
                },
            )

        transform = proxy._excel_response_processor().tool_stream_transform(
            {"text": {"format": TITLE_FORMAT}}
        )
        chunks = []
        async for chunk in transform(source()):
            if b"response.output_text." in chunk:
                self.assertTrue(reached_terminal)
            chunks.append(chunk)
        output = b"".join(chunks).decode()
        self.assertNotIn("PRIVATE_PENDING", output)
        self.assertEqual(output.count("Checking title"), 1)


if __name__ == "__main__":
    unittest.main()
