"""Exercise request-content alignment through the public Responses routes."""

import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import excel_images
import proxy
import rate_limiting


class BPSHTTPAlignmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.generations = []
        self.uploads = []
        self.output_text = "OK"

        def upstream(request):
            if str(request.url) == excel_images.ATTACHMENTS_URL:
                self.uploads.append(request)
                return httpx.Response(200, json={"openai_file_id": "file-fixture"})
            self.generations.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "resp_alignment",
                "object": "response",
                "model": "gpt-6-astra",
                "status": "completed",
                "output": [{
                    "type": "message", "id": "msg_alignment", "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": self.output_text}],
                }],
            })

        self.remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.local = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1",
        )
        self.addAsyncCleanup(self.remote.aclose)
        self.addAsyncCleanup(self.local.aclose)
        self.enterContext(patch.object(proxy, "_get_excel_upstream_client", return_value=self.remote))
        self.enterContext(patch.object(proxy, "_selected_excel_headers", new=AsyncMock(return_value={
            "authorization": "Bearer fixture-alignment", "chatgpt-account-id": "fixture-alignment",
        })))
        self.enterContext(patch.object(rate_limiting, "throttle_upstream_request", new=AsyncMock()))
        self.enterContext(patch.object(excel_images, "image_uploads", excel_images.ExcelImageUploads()))

    async def test_client_tool_declarations_are_not_rewritten_by_http_boundary(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                response = await self.local.post("/v1/responses", json={
                    "model": "gpt-6-astra", "input": "hello", "stream": stream,
                    "tools": [{
                        "type": "function", "name": "spawn_agent",
                        "description": "Spawn exactly as declared.",
                        "parameters": {"type": "object", "properties": {
                            "model": {"type": "string"}, "fork_context": {"type": "boolean"},
                        }},
                    }],
                })
                self.assertEqual(response.status_code, 200, response.text)
                sent = json.dumps(self.generations[-1]["input"])
                self.assertIn("Spawn exactly as declared.", sent)
                self.assertNotIn("ghcp_proxy multi-agent compatibility", sent)
                self.assertNotIn("gpt-5.4-mini", sent)

    async def test_invalid_input_is_rejected_without_upload_or_generation(self):
        for fields in ({}, {"input": None}, {"input": {}}, {"input": 7}, {"input": ["lost"]}, {"input": [None]}):
            for stream in (False, True):
                with self.subTest(fields=fields, stream=stream):
                    response = await self.local.post("/responses", json={
                        "model": "gpt-6-astra", "stream": stream, **fields,
                    })
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["error"]["param"], "input")
        self.assertEqual(self.generations, [])
        self.assertEqual(self.uploads, [])

    async def test_content_validation_precedes_attachment_upload(self):
        image = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        for invalid in (
            {"type": "input_audio", "data": "AAAA"},
            {"type": "input_file", "file_id": "file-document"},
            {"type": "input_image", "image_url": "http://example.test/image.png"},
            {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "invalid"},
        ):
            with self.subTest(invalid=invalid):
                response = await self.local.post("/v1/responses", json={
                    "model": "gpt-6-astra", "input": [{
                        "role": "user", "content": [image, invalid],
                    }],
                })
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.generations, [])
        self.assertEqual(self.uploads, [])

    async def test_compaction_preserves_submitted_history_and_notifications(self):
        history = [
            {"type": "message", "role": "user", "content": "Original task"},
            {"type": "message", "role": "assistant", "content": "Earlier answer"},
            {"type": "message", "role": "user", "content": "<subagent_notification>Finished</subagent_notification>"},
            {"type": "compaction", "encrypted_content": "opaque-native-summary"},
            {"type": "message", "role": "user", "content": "Continue"},
        ]
        original = copy.deepcopy(history)
        for stream in (False, True):
            with self.subTest(stream=stream):
                response = await self.local.post("/responses", json={
                    "model": "gpt-6-astra", "input": history, "stream": stream,
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.generations[-1]["input"][1:], original)
                self.assertEqual(history, original)

    async def test_array_schema_uses_the_same_protocol_message_and_accepts_array(self):
        self.output_text = "[1,2]"
        fmt = {"type": "json_schema", "name": "items", "schema": {"type": "array", "items": {"type": "integer"}}}
        for stream in (False, True):
            with self.subTest(stream=stream):
                response = await self.local.post("/responses", json={
                    "model": "gpt-6-astra", "input": "Return two integers", "stream": stream,
                    "text": {"format": fmt},
                })
                self.assertEqual(response.status_code, 200, response.text)
                sent = self.generations[-1]
                self.assertEqual(len(sent["input"]), 2)
                protocol = sent["input"][0]["content"][0]["text"]
                self.assertIn("exactly one JSON value", protocol)
                self.assertNotIn("exactly one JSON object", protocol)
                self.assertIn("[1,2]", response.text)
                self.assertEqual(sent["model"], "gpt-6-astra")
                self.assertEqual(sent["reasoning_effort"], "medium")
