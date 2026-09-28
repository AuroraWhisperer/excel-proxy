import asyncio
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import httpx

import excel_images
import excel_upstream
import responses_protocol
import proxy


class ExcelHistoryCompatibilityTests(unittest.TestCase):
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
                    self.assertNotIn("author", result)
                    self.assertNotIn("recipient", result)
                    for field in ("role", "id", "phase"):
                        self.assertEqual(result[field], source[field])
                    parts = result["content"]
                    self.assertEqual(len(parts), 2)
                    self.assertIn("/root/worker", parts[0]["text"])
                    self.assertIn("context only", parts[0]["text"])
                    self.assertEqual(parts[1]["text"], source["content"])
                    self.assertEqual(
                        {part["type"] for part in parts},
                        {"output_text" if role == "assistant" else "input_text"},
                    )
                    self.assertEqual(source, original)
                    self.assertEqual(
                        excel_upstream.translate_input_items([result]), [result]
                    )

    def test_agent_context_keeps_content_order_without_promoting_authority(self):
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
        self.assertEqual(set(result[1]), {"type", "role", "content"})
        self.assertEqual((result[1]["type"], result[1]["role"]), ("message", "user"))
        self.assertIn("not a new user instruction", result[1]["content"][0]["text"])
        self.assertIn("agent_private", result[1]["content"][0]["text"])
        self.assertEqual(result[1]["content"][1:], parts)
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
        self.assertEqual(result["content"][1:], [encrypted])
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

    def test_tool_image_references_keep_labels_order_and_inline_screenshots(self):
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
                result = excel_upstream.translate_input_items([source])
                self.assertEqual(len(result), 2)
                output, message = result
                self.assertEqual(output["call_id"], "call_picture")
                self.assertEqual(len(output["output"]), 5)
                self.assertEqual(output["output"][0], source["output"][0])
                self.assertEqual(output["output"][2:4], source["output"][2:4])
                self.assertEqual(
                    (message["type"], message["role"]), ("message", "user")
                )
                self.assertIn(
                    "not a new user instruction", message["content"][0]["text"]
                )
                self.assertEqual(message["content"][2], file_image)
                self.assertEqual(message["content"][4], remote_image)
                for output_index, content_index in ((1, 1), (4, 3)):
                    label = message["content"][content_index]["text"]
                    self.assertIn("call_picture", label)
                    self.assertIn(label, output["output"][output_index]["text"])
                self.assertEqual(source, original)
                self.assertEqual(excel_upstream.translate_input_items(result), result)


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
            patch.object(proxy.excel_session_capture, "refresh_macos_excel_session")
        )
        patches.enter_context(
            patch.object(proxy.excel_session_capture, "refresh_windows_excel_session")
        )
        patches.enter_context(
            patch.object(
                excel_upstream.excel_session_store,
                "request_headers",
                return_value={
                    "authorization": "Bearer test",
                    "chatgpt-account-id": "account-test",
                    "content-type": "application/json",
                    "accept": "text/event-stream",
                },
            )
        )
        patches.enter_context(
            patch.object(
                excel_upstream.excel_session_store,
                "tools_version_id",
                return_value=None,
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
                    self.assertEqual(set(item), {"type", "role", "content"})
                    self.assertEqual((item["type"], item["role"]), ("message", "user"))
                    self.assertIn("/root/worker", item["content"][0]["text"])
                    self.assertEqual(item["content"][2]["file_id"], "file-1")
                    self.assertEqual(body, original)
        self.assertEqual(len(self.uploads), 1)

    async def test_tool_attachment_messages_do_not_start_a_new_agent_turn(self):
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
                expected = excel_upstream.prepare_responses_body(body)["metadata"]
                response = await self.local.post("/responses", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                sent = self.requests[-1]
                self.assertEqual(sent["metadata"], expected)
                self.assertEqual(sent["metadata"]["agent_iteration"], "2")
                self.assertEqual(sent["input"][-2]["type"], "function_call_output")
                self.assertEqual(sent["input"][-2]["call_id"], "call_picture")
                self.assertEqual(
                    sent["input"][-1]["content"][2]["file_id"], "file-existing"
                )
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
        body["input"].append(
            {
                "type": "function_call_output",
                "call_id": "call_image",
                "output": [{"type": "input_image", "file_id": "file-existing"}],
            }
        )
        with patch.object(excel_images, "MAX_IMAGES", 1):
            response = await self.local.post("/responses", json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.uploads, [])

    async def test_two_image_conversations_share_upload_but_keep_separate_task_identity(
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

    async def test_rejected_cached_file_is_uploaded_again_without_changing_turn_identity(
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
        self.assertEqual(self.requests[0]["metadata"], self.requests[-1]["metadata"])

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
            self.assertEqual(body["reasoning"], {"effort": "xhigh", "summary": "auto"})

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
            self.assertEqual(completed["id"], f"conversation_{index}")
            item = completed["output"][0]
            self.assertEqual(item["summary"][0]["text"], deltas[0])
            self.assertEqual(item["encrypted_content"], f"opaque_conversation_{index}")
            self.assertNotIn("Thinking process completed", response.text)


if __name__ == "__main__":
    unittest.main()
