import asyncio
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx

import excel_images
import excel_upstream
import responses_protocol
import proxy


class ExcelHistoryCompatibilityTests(unittest.TestCase):
    def test_item_reference_requires_expanded_history(self):
        source = {"input": [
            {"role": "user", "content": "Continue using the earlier answer"},
            {"type": "item_reference", "id": "msg_earlier_answer"},
        ]}
        original = copy.deepcopy(source)
        with self.assertRaisesRegex(ValueError, "full history.*item_reference"):
            excel_upstream.prepare_responses_body(source)
        self.assertEqual(source, original)

    def test_history_catalog_updates_apply_to_new_calls_without_rewriting_old_calls(self):
        def tool(field):
            return {"type": "function", "name": "annotate", "parameters": {
                "type": "object", "required": [field], "properties": {field: {"type": "string"}},
            }}

        old_call = {"type": "function_call", "call_id": "call_old_note", "name": "annotate",
                    "arguments": '{"code":"blue"}'}
        source = {"input": [
            {"type": "additional_tools", "tools": [tool("code")]}, old_call,
            {"type": "function_call_output", "call_id": "call_old_note", "output": "ok"},
            {"type": "additional_tools", "tools": [tool("color")]},
            {"role": "user", "content": "continue"},
        ]}
        original = copy.deepcopy(source)
        prepared = excel_upstream.prepare_responses_body(source)
        replay = next(item for item in prepared["input"] if item.get("type") == "function_call")
        self.assertEqual(replay["call_id"], "call_old_note")
        self.assertEqual(json.loads(replay["arguments"])["summary"], "Replay a previously requested client tool")
        self.assertEqual(
            json.loads(json.loads(replay["arguments"])["code"]),
            {"name": "annotate", "arguments": {"code": "blue"}},
        )
        self.assertNotIn("additional_tools", json.dumps(prepared))
        for arguments, accepted in (('{"color":"blue"}', True), ('{"code":"blue"}', False)):
            result = excel_upstream.extract_native_client_tool_call(
                {"output": [{**old_call, "call_id": "call_new_note", "arguments": arguments}]},
                source, remember=False,
            )
            self.assertEqual(result is not None, accepted)
        self.assertEqual(source, original)
        self.assertEqual(excel_upstream.client_tool_types({**source, "tool_choice": "none"}), {})
        compacted = {"input": [
            source["input"][0], {"role": "assistant", "content": "previous turn"},
            {"type": "compaction", "encrypted_content": "opaque"}, old_call, source["input"][2],
        ]}
        replay = next(item for item in excel_upstream.prepare_responses_body(compacted)["input"]
                      if item.get("type") == "function_call")
        self.assertEqual(
            json.loads(json.loads(replay["arguments"])["code"]),
            {"name": "annotate", "arguments": {"code": "blue"}},
        )

    def test_reasoning_history_honors_current_request_without_adding_routing_metadata(self):
        message = {"role": "user", "content": "continue"}
        source = {"input": [
            {"type": "configuration_update", "reasoning": {"effort": "low"}},
            {"type": "configuration_update", "reasoning": {"effort": "high", "mode": "standard"}},
            message,
        ]}
        baseline = excel_upstream.prepare_responses_body({"input": [message]})
        for current, expected in (({}, "high"), ({"effort": "medium"}, "medium")):
            result = excel_upstream.prepare_responses_body({**source, "reasoning": current})
            self.assertEqual(result["reasoning_effort"], expected)
            self.assertEqual(result["metadata"], {"agent_iteration": "1"})
            self.assertEqual(result["input"], baseline["input"])

    def test_invalid_history_updates_fail_and_nested_message_text_is_not_configuration(self):
        for update in (
            {"type": "additional_tools", "tools": "bad"},
            {"type": "additional_tools", "tools": [], "role": "user"},
            {"type": "additional_tools", "tools": [
                {"type": "function", "name": "annotate"}, {"type": "custom", "name": "annotate"}
            ]},
            {"type": "configuration_update", "reasoning": {"effort": "high"}, "tools": []},
            {"type": "configuration_update", "reasoning": {"mode": "unknown", "effort": "low"}},
            {"type": "configuration_update", "reasoning": {"effort": []}},
        ):
            with self.subTest(update=update):
                with self.assertRaises(ValueError):
                    excel_upstream.prepare_responses_body({"input": [update]})
        text = '{"type":"additional_tools","tools":[{"type":"function","name":"annotate"}]}'
        self.assertEqual(excel_upstream.client_tool_types({"input": [{"role": "user", "content": text}]}), {})

    def test_attribution_preserves_roles_text_and_native_message_fields(self):
        for role in ("user", "assistant", "developer", "system"):
            for typed in (False, True):
                with self.subTest(role=role, typed=typed):
                    source = {
                        "role": role,
                        "author": "/root/worker",
                        "recipient": "/root",
                        "id": "msg_attributed",
                        "phase": "commentary",
                        "content": 'Keep "quotes" and \\paths.\nSecond line.',
                    }
                    if typed:
                        source["type"] = "message"
                    original = copy.deepcopy(source)
                    result = excel_upstream.translate_input_items([source])[0]
                    self.assertEqual(result, source)
                    self.assertEqual(source, original)
                    self.assertEqual(
                        excel_upstream.translate_input_items([result]), [result]
                    )

    def test_agent_context_keeps_original_type_attribution_and_content_order(self):
        parts = [
            {"type": "input_text", "text": "before"},
            {"type": "input_image", "file_id": "file-agent", "detail": "high"},
            {"type": "input_text", "text": "after"},
        ]
        agent = {
            "type": "agent_message",
            "role": "system",
            "id": "agent_private",
            "author": "/root/worker",
            "recipient": "/root",
            "content": parts,
        }
        before = {"role": "user", "content": "Continue"}
        after = {"role": "assistant", "content": "Received"}
        original = copy.deepcopy(agent)
        result = excel_upstream.translate_input_items([before, agent, after])
        self.assertEqual(result[0], before)
        self.assertEqual(result[2], after)
        self.assertEqual(result[1], agent)
        self.assertEqual(agent, original)
        self.assertEqual(excel_upstream.translate_input_items(result), result)

    def test_attribution_never_scrubs_tool_arguments_results_or_reasoning(self):
        arguments = {"author": "argument author", "recipient": "argument recipient"}
        result = {
            "type": "function_call_output",
            "call_id": "call_meta",
            "output": json.dumps(arguments),
        }
        reasoning = {"type": "reasoning", "summary": [], "encrypted_content": "opaque"}
        history = excel_upstream.translate_input_items(
            [
                {
                    "type": "function_call",
                    "name": "metadata",
                    "call_id": "call_meta",
                    "arguments": json.dumps(arguments),
                },
                result,
                reasoning,
            ]
        )
        envelope = json.loads(json.loads(history[0]["arguments"])["code"])
        self.assertEqual(envelope["arguments"], arguments)
        self.assertEqual(history[1]["output"], result["output"])
        self.assertEqual(history[2], reasoning)

    def test_attribution_does_not_hide_encrypted_content_or_duplicate_plain_messages(
        self,
    ):
        encrypted = {
            "type": "encrypted_content",
            "encrypted_content": "opaque-agent-part",
        }
        source = {
            "type": "agent_message",
            "author": "/root/worker",
            "content": [encrypted],
        }
        result = excel_upstream.translate_input_items([source])[0]
        self.assertEqual(result["content"], [encrypted])
        self.assertEqual(result["type"], "agent_message")
        self.assertEqual(result["author"], "/root/worker")
        plain = {
            "type": "message",
            "role": "assistant",
            "id": "msg_plain",
            "phase": "commentary",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "unchanged", "annotations": []}
            ],
        }
        self.assertEqual(excel_upstream.translate_input_items([plain]), [plain])

    def test_tool_images_keep_original_result_order_and_inline_screenshots(self):
        inline = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        file_image = {
            "type": "input_image",
            "file_id": "file-existing",
            "detail": "high",
        }
        remote_image = {
            "type": "input_image",
            "image_url": "https://example.com/picture.png",
        }
        for kind in ("function_call_output", "custom_tool_call_output"):
            with self.subTest(kind=kind):
                source = {
                    "type": kind,
                    "call_id": "call_picture",
                    "output": [
                        {"type": "input_text", "text": "before"},
                        file_image,
                        {"type": "input_text", "text": "between"},
                        inline,
                        remote_image,
                    ],
                }
                original = copy.deepcopy(source)
                call = {
                    "type": "custom_tool_call" if kind == "custom_tool_call_output" else "function_call",
                    "name": "view_image",
                    "call_id": "call_picture",
                }
                if kind == "custom_tool_call_output":
                    call["input"] = "picture.png"
                else:
                    call["arguments"] = '{"path":"picture.png"}'
                result = excel_upstream.translate_input_items([call, source])
                self.assertEqual(len(result), 2)
                output = result[-1]
                self.assertEqual(output["type"], "function_call_output")
                self.assertEqual(output["call_id"], "call_picture")
                self.assertEqual(output["output"], source["output"])
                self.assertFalse(any(item.get("type") == "message" for item in result))
                self.assertEqual(source, original)
                self.assertEqual(excel_upstream.translate_input_items([call, source]), result)


def summary_stream(response_id):
    text = "**Checking input**\n\nChecking the request before answering."
    reasoning = {
        "type": "reasoning",
        "id": f"rs_{response_id}",
        "summary": [{"type": "summary_text", "text": text}],
        "encrypted_content": f"opaque_{response_id}",
    }
    message = {
        "type": "message",
        "id": f"msg_{response_id}",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "OK"}],
    }
    fields = {"item_id": reasoning["id"], "output_index": 0, "summary_index": 0}
    events = [
        ("response.created", {"response": {"id": response_id}}),
        (
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "type": "reasoning",
                    "id": reasoning["id"],
                    "summary": [],
                },
            },
        ),
        (
            "response.reasoning_summary_part.added",
            {**fields, "part": {"type": "summary_text", "text": ""}},
        ),
        ("response.reasoning_summary_text.delta", {**fields, "delta": text}),
        ("response.reasoning_summary_text.done", {**fields, "text": text}),
        (
            "response.reasoning_summary_part.done",
            {**fields, "part": reasoning["summary"][0]},
        ),
        ("response.output_item.done", {"output_index": 0, "item": reasoning}),
        ("response.output_item.done", {"output_index": 1, "item": message}),
        (
            "response.completed",
            {
                "response": {
                    "id": response_id,
                    "model": "gpt-6-astra",
                    "status": "completed",
                    "output": [],
                }
            },
        ),
    ]
    return b"".join(
        responses_protocol.sse_encode(name, {"type": name, **data})
        for name, data in events
    )


class ExcelRequestCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.uploads = []
        self.upload_status = 200
        self.reject_file = None

        async def upstream(request):
            if str(request.url) == excel_images.ATTACHMENTS_URL:
                self.uploads.append(request)
                await asyncio.sleep(0)
                if self.upload_status != 200:
                    return httpx.Response(
                        self.upload_status,
                        json={"error": {"message": "Upload rejected"}},
                    )
                return httpx.Response(
                    200, json={"openai_file_id": f"file-{len(self.uploads)}"}
                )
            body = json.loads(request.content)
            self.requests.append(body)
            await asyncio.sleep(0)
            if self.reject_file and self.reject_file in request.content.decode():
                return httpx.Response(
                    422, json={"error": {"message": "Invalid image file"}}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=summary_stream(body["prompt_cache_key"]),
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
                        "chatgpt-account-id": "account-test",
                        "content-type": "application/json",
                        "accept": "text/event-stream",
                    }
                ),
            )
        )
        patches.enter_context(
            patch.object(
                excel_images, "image_uploads", excel_images.ExcelImageUploads()
            )
        )

    def image_body(self, session="image-conversation", stream=True):
        return {
            "model": "gpt-6-astra-excel",
            "stream": stream,
            "prompt_cache_key": session,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Explain this screenshot"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AAAA",
                            "detail": "high",
                        },
                    ],
                }
            ],
        }

    async def test_image_requests_upload_and_reference_the_image_on_both_routes(self):
        for path in ("/responses", "/v1/responses"):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    response = await self.local.post(
                        path, json=self.image_body(stream=stream)
                    )
                    self.assertEqual(response.status_code, 200)
                    image = self.requests[-1]["input"][-1]["content"][1]
                    self.assertEqual(
                        image,
                        {"type": "input_image", "file_id": "file-1", "detail": "high"},
                    )
                    self.assertNotIn("data:image", json.dumps(self.requests[-1]))
        self.assertEqual(len(self.uploads), 1)
        upload = self.uploads[0]
        self.assertEqual(upload.headers["authorization"], "Bearer test")
        self.assertEqual(upload.headers["chatgpt-account-id"], "account-test")
        self.assertEqual(upload.headers["accept"], "application/json")
        self.assertTrue(
            upload.headers["content-type"].startswith("multipart/form-data; boundary=")
        )
        self.assertIn(b'name="file";', upload.content)

    async def test_agent_images_and_attribution_reach_both_routes_as_context(self):
        for path in ("/responses", "/v1/responses"):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    body = self.image_body(stream=stream)
                    body["input"][0].update(
                        type="agent_message",
                        role="developer",
                        author="/root/worker",
                        recipient="/root",
                    )
                    original = copy.deepcopy(body)
                    response = await self.local.post(path, json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    item = self.requests[-1]["input"][-1]
                    self.assertEqual(set(item), {"type", "role", "content", "author", "recipient"})
                    self.assertEqual((item["type"], item["role"]), ("agent_message", "developer"))
                    self.assertEqual(item["author"], "/root/worker")
                    self.assertEqual(item["recipient"], "/root")
                    self.assertEqual(item["content"][0], body["input"][0]["content"][0])
                    self.assertEqual(item["content"][1]["file_id"], "file-1")
                    self.assertEqual(body, original)
        self.assertEqual(len(self.uploads), 1)

    async def test_tool_images_stay_in_results_without_starting_a_new_agent_turn(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = self.image_body(stream=stream)
                body["input"] = [
                    {"role": "user", "content": "Inspect the screenshot"},
                    {
                        "type": "custom_tool_call",
                        "name": "view_image",
                        "call_id": "call_picture",
                        "input": "picture.png",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_picture",
                        "output": [{"type": "input_image", "file_id": "file-existing"}],
                    },
                ]
                prepared = excel_upstream.prepare_responses_body(body)
                self.assertEqual(prepared["metadata"], {"agent_iteration": "2"})
                response = await self.local.post("/responses", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                sent = self.requests[-1]
                self.assertEqual(set(sent["metadata"]), {"task_id", "turn_id", "agent_iteration"})
                self.assertRegex(sent["metadata"]["task_id"], r"^[0-9a-f]{64}$")
                self.assertRegex(sent["metadata"]["turn_id"], r"^[0-9a-f]{64}$")
                self.assertEqual(sent["metadata"]["agent_iteration"], "2")
                self.assertEqual(sent["model"], "gpt-6-astra")
                self.assertEqual(sent["input"][-1]["type"], "function_call_output")
                self.assertEqual(sent["input"][-1]["call_id"], "call_picture")
                self.assertEqual(
                    sent["input"][-1]["output"][0]["file_id"], "file-existing"
                )
                self.assertEqual(len(sent["input"]), len(body["input"]) + 1)
                self.assertEqual(self.uploads, [])

    async def test_item_reference_is_rejected_before_upload_or_generation(self):
        for path in ("/responses", "/v1/responses"):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    body = self.image_body(stream=stream)
                    body["input"].append({"type": "item_reference", "id": "msg_earlier_answer"})
                    response = await self.local.post(path, json=body)
                    self.assertEqual(response.status_code, 400, response.text)
                    error = response.json()["error"]
                    self.assertEqual(error["param"], "input")
                    self.assertIn("item_reference", error["message"])
                    self.assertIn("full history", error["message"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.uploads, [])

    async def test_malformed_attributed_content_is_rejected_before_network_io(self):
        for content in (None, True, {"private": "DO-NOT-EXPOSE"}):
            with self.subTest(content=content):
                response = await self.local.post(
                    "/responses",
                    json={
                        "model": "gpt-5.6-sol-excel",
                        "input": [
                            {
                                "type": "agent_message",
                                "author": "/root/worker",
                                "content": content,
                            }
                        ],
                    },
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn("DO-NOT-EXPOSE", response.text)
                self.assertIn("input[0].content", response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.uploads, [])

    async def test_agent_and_tool_images_share_the_request_image_limit(self):
        body = self.image_body()
        body["input"][0].update(type="agent_message", author="/root/worker")
        body["input"].extend([
            {"type": "function_call", "name": "view_image", "call_id": "call_image", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "call_image",
                "output": [{"type": "input_image", "file_id": "file-existing"}],
            },
        ])
        with patch.object(excel_images, "MAX_IMAGES", 1):
            response = await self.local.post("/responses", json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("at most 1 images", response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.uploads, [])

    async def test_concurrent_image_conversations_share_upload_but_use_separate_slots(
        self,
    ):
        responses = await asyncio.gather(
            *[
                self.local.post(
                    "/responses", json=self.image_body(session=f"picture-{index}")
                )
                for index in range(2)
            ]
        )
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(
            len({body["metadata"]["task_id"] for body in self.requests}), 2
        )
        self.assertEqual(len({body["metadata"]["turn_id"] for body in self.requests}), 2)
        self.assertTrue(all(body["model"] == "gpt-6-astra" for body in self.requests))

    async def test_image_continuation_reuses_file_and_preserves_history_prefix(self):
        body = self.image_body()
        first = await self.local.post("/responses", json=body)
        self.assertEqual(first.status_code, 200)
        body["input"].extend(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Read it again"}],
                },
            ]
        )
        second = await self.local.post("/responses", json=body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(
            self.requests[1]["input"][: len(self.requests[0]["input"])],
            self.requests[0]["input"],
        )
        self.assertEqual(
            self.requests[1]["metadata"]["task_id"],
            self.requests[0]["metadata"]["task_id"],
        )
        self.assertNotEqual(
            self.requests[1]["metadata"]["turn_id"],
            self.requests[0]["metadata"]["turn_id"],
        )

    async def test_rejected_cached_file_is_uploaded_again_with_a_new_generation_turn(
        self,
    ):
        body = self.image_body()
        first = await self.local.post("/responses", json=body)
        self.assertEqual(first.status_code, 200)
        self.reject_file = "file-1"
        second = await self.local.post("/responses", json=body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(
            self.requests[-1]["input"][-1]["content"][1]["file_id"], "file-2"
        )
        self.assertEqual(len({body["metadata"]["task_id"] for body in self.requests}), 1)
        self.assertEqual(len({body["metadata"]["turn_id"] for body in self.requests}), 3)
        self.assertTrue(all(body["metadata"]["agent_iteration"] == "1" for body in self.requests))
        self.assertTrue(all(body["model"] == "gpt-6-astra" for body in self.requests))
        self.assertEqual(self.requests[0]["input"], self.requests[1]["input"])

    async def test_failed_upload_preserves_status_without_sending_a_text_only_request(
        self,
    ):
        for status in (401, 413, 429, 500):
            with self.subTest(status=status):
                self.upload_status = status
                response = await self.local.post("/responses", json=self.image_body())
                self.assertEqual(response.status_code, status)
                self.assertIn(
                    "image upload failed", response.json()["error"]["message"]
                )
        self.assertEqual(self.requests, [])

    async def test_malformed_image_is_rejected_before_upload(self):
        body = self.image_body()
        body["input"][0]["content"][1]["image_url"] = (
            "data:image/png;base64,not-base64!"
        )
        response = await self.local.post("/responses", json=body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["param"], "input")
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.requests, [])

    async def test_two_text_conversations_preserve_their_real_summaries_and_replay_state(
        self,
    ):
        responses = await asyncio.gather(
            *[
                self.local.post(
                    path,
                    json={
                        "model": "gpt-6-astra-excel",
                        "stream": True,
                        "prompt_cache_key": f"conversation_{index}",
                        "input": "Hello",
                        "reasoning": {"effort": "xhigh", "summary": "detailed"},
                    },
                )
                for index, path in enumerate(("/responses", "/v1/responses"))
            ]
        )
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(
            len({body["metadata"]["task_id"] for body in self.requests}), 2
        )
        for body in self.requests:
            self.assertEqual(body["model"], "gpt-6-astra")
            self.assertEqual(body["reasoning_effort"], "xhigh")
            self.assertNotIn("reasoning", body)
        expected_keys = [
            "bps-b38089605d9c0d8832ca46709968a27d",
            "bps-47d2ff387a8ab9ba07f7d851f173324e",
        ]
        self.assertEqual({body["prompt_cache_key"] for body in self.requests}, set(expected_keys))

        for index, response in enumerate(responses):
            self.assertEqual(response.status_code, 200)
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: {")
            ]
            deltas = [
                event["delta"]
                for event in events
                if event["type"] == "response.reasoning_summary_text.delta"
            ]
            self.assertEqual(
                deltas, ["**Checking input**\n\nChecking the request before answering."]
            )
            completed = next(
                event["response"]
                for event in events
                if event["type"] == "response.completed"
            )
            self.assertEqual(completed["id"], expected_keys[index])
            self.assertEqual(completed["model"], "gpt-6-astra")
            item = completed["output"][0]
            self.assertEqual(item["summary"][0]["text"], deltas[0])
            self.assertEqual(item["encrypted_content"], f"opaque_{expected_keys[index]}")
            self.assertNotIn("Thinking process completed", response.text)


if __name__ == "__main__":
    unittest.main()
