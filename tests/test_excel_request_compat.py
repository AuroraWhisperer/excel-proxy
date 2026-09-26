import asyncio
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import httpx

import excel_images
import excel_upstream
import format_translation
import proxy


def summary_stream(response_id):
    text = "**Checking input**\n\nChecking the request before answering."
    reasoning = {
        "type": "reasoning", "id": f"rs_{response_id}",
        "summary": [{"type": "summary_text", "text": text}],
        "encrypted_content": f"opaque_{response_id}",
    }
    message = {
        "type": "message", "id": f"msg_{response_id}", "role": "assistant",
        "content": [{"type": "output_text", "text": "OK"}],
    }
    fields = {"item_id": reasoning["id"], "output_index": 0, "summary_index": 0}
    events = [
        ("response.created", {"response": {"id": response_id}}),
        ("response.output_item.added", {"output_index": 0, "item": {
            "type": "reasoning", "id": reasoning["id"], "summary": [],
        }}),
        ("response.reasoning_summary_part.added", {**fields, "part": {"type": "summary_text", "text": ""}}),
        ("response.reasoning_summary_text.delta", {**fields, "delta": text}),
        ("response.reasoning_summary_text.done", {**fields, "text": text}),
        ("response.reasoning_summary_part.done", {**fields, "part": reasoning["summary"][0]}),
        ("response.output_item.done", {"output_index": 0, "item": reasoning}),
        ("response.output_item.done", {"output_index": 1, "item": message}),
        ("response.completed", {"response": {
            "id": response_id, "status": "completed", "output": [],
        }}),
    ]
    return b"".join(format_translation.sse_encode(name, {"type": name, **data}) for name, data in events)


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
                    return httpx.Response(self.upload_status, json={"error": {"message": "Upload rejected"}})
                return httpx.Response(200, json={"openai_file_id": f"file-{len(self.uploads)}"})
            body = json.loads(request.content)
            self.requests.append(body)
            await asyncio.sleep(0)
            if self.reject_file and self.reject_file in request.content.decode():
                return httpx.Response(422, json={"error": {"message": "Invalid image file"}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=summary_stream(body["prompt_cache_key"]))

        self.remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.local = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1")
        self.addAsyncCleanup(self.remote.aclose)
        self.addAsyncCleanup(self.local.aclose)
        patches = self.enterContext(ExitStack())
        patches.enter_context(patch.object(proxy, "_get_excel_upstream_client", return_value=self.remote))
        patches.enter_context(patch.object(proxy.excel_session_capture, "refresh_macos_excel_session"))
        patches.enter_context(patch.object(proxy.excel_session_capture, "refresh_windows_excel_session"))
        patches.enter_context(patch.object(excel_upstream.excel_session_store, "request_headers", return_value={
            "authorization": "Bearer test", "chatgpt-account-id": "account-test",
            "content-type": "application/json", "accept": "text/event-stream",
        }))
        patches.enter_context(patch.object(excel_upstream.excel_session_store, "tools_version_id", return_value=None))
        patches.enter_context(patch.object(excel_images, "image_uploads", excel_images.ExcelImageUploads()))

    def image_body(self, session="image-conversation", stream=True):
        return {
            "model": "gpt-6-astra-excel", "stream": stream, "prompt_cache_key": session,
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Explain this screenshot"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"},
            ]}],
        }

    async def test_image_requests_upload_and_reference_the_image_on_both_routes(self):
        for path in ("/responses", "/v1/responses"):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    response = await self.local.post(path, json=self.image_body(stream=stream))
                    self.assertEqual(response.status_code, 200)
                    image = self.requests[-1]["input"][-1]["content"][1]
                    self.assertEqual(image, {"type": "input_image", "file_id": "file-1", "detail": "high"})
                    self.assertNotIn("data:image", json.dumps(self.requests[-1]))
        self.assertEqual(len(self.uploads), 1)
        upload = self.uploads[0]
        self.assertEqual(upload.headers["authorization"], "Bearer test")
        self.assertEqual(upload.headers["chatgpt-account-id"], "account-test")
        self.assertEqual(upload.headers["accept"], "application/json")
        self.assertTrue(upload.headers["content-type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file";', upload.content)

    async def test_two_image_conversations_share_upload_but_keep_separate_task_identity(self):
        responses = await asyncio.gather(*[
            self.local.post("/responses", json=self.image_body(session=f"picture-{index}"))
            for index in range(2)
        ])
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(len({body["metadata"]["task_id"] for body in self.requests}), 2)

    async def test_image_continuation_reuses_file_and_preserves_history_prefix(self):
        body = self.image_body()
        first = await self.local.post("/responses", json=body)
        self.assertEqual(first.status_code, 200)
        body["input"].extend([
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Read it again"}]},
        ])
        second = await self.local.post("/responses", json=body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.requests[1]["input"][:len(self.requests[0]["input"])], self.requests[0]["input"])
        self.assertEqual(self.requests[1]["metadata"]["task_id"], self.requests[0]["metadata"]["task_id"])

    async def test_rejected_cached_file_is_uploaded_again_without_changing_turn_identity(self):
        body = self.image_body()
        first = await self.local.post("/responses", json=body)
        self.assertEqual(first.status_code, 200)
        self.reject_file = "file-1"
        second = await self.local.post("/responses", json=body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[-1]["input"][-1]["content"][1]["file_id"], "file-2")
        self.assertEqual(self.requests[0]["metadata"], self.requests[-1]["metadata"])

    async def test_failed_upload_preserves_status_without_sending_a_text_only_request(self):
        for status in (401, 413, 429, 500):
            with self.subTest(status=status):
                self.upload_status = status
                response = await self.local.post("/responses", json=self.image_body())
                self.assertEqual(response.status_code, status)
                self.assertIn("image upload failed", response.json()["error"]["message"])
        self.assertEqual(self.requests, [])

    async def test_malformed_image_is_rejected_before_upload(self):
        body = self.image_body()
        body["input"][0]["content"][1]["image_url"] = "data:image/png;base64,not-base64!"
        response = await self.local.post("/responses", json=body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["param"], "input")
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.requests, [])

    async def test_two_text_conversations_preserve_their_real_summaries_and_replay_state(self):
        responses = await asyncio.gather(*[
            self.local.post(path, json={
                "model": "gpt-6-astra-excel", "stream": True,
                "prompt_cache_key": f"conversation_{index}", "input": "Hello",
                "reasoning": {"effort": "xhigh", "summary": "detailed"},
            })
            for index, path in enumerate(("/responses", "/v1/responses"))
        ])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len({body["metadata"]["task_id"] for body in self.requests}), 2)
        for body in self.requests:
            self.assertEqual(body["reasoning"], {"effort": "xhigh", "summary": "auto"})

        for index, response in enumerate(responses):
            self.assertEqual(response.status_code, 200)
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
            deltas = [event["delta"] for event in events if event["type"] == "response.reasoning_summary_text.delta"]
            self.assertEqual(deltas, ["**Checking input**\n\nChecking the request before answering."])
            completed = next(event["response"] for event in events if event["type"] == "response.completed")
            self.assertEqual(completed["id"], f"conversation_{index}")
            item = completed["output"][0]
            self.assertEqual(item["summary"][0]["text"], deltas[0])
            self.assertEqual(item["encrypted_content"], f"opaque_conversation_{index}")
            self.assertNotIn("Thinking process completed", response.text)


if __name__ == "__main__":
    unittest.main()
