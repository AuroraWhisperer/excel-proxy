import asyncio
import copy
import unittest
from unittest.mock import patch

import httpx

import excel_images


class ExcelImageUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uploads = excel_images.ExcelImageUploads()
        self.requests = []

        async def upstream(request):
            self.requests.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"openai_file_id": f"file-{len(self.requests)}"})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.client.aclose)
        self.body = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": "Read the image"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ]}]}

    async def rewrite(self, account="account-a", body=None):
        return await self.uploads.rewrite(
            body if body is not None else self.body, self.client,
            {"authorization": "Bearer test", "chatgpt-account-id": account},
        )

    async def test_upload_cache_is_account_scoped_and_does_not_mutate_the_original(self):
        original = copy.deepcopy(self.body)
        first, reused = await self.rewrite()
        self.assertFalse(reused)
        again, reused = await self.rewrite()
        self.assertTrue(reused)
        other, _ = await self.rewrite(account="account-b")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.body, original)
        self.assertEqual(first["input"][0]["content"][1], {
            "type": "input_image", "file_id": "file-1", "detail": "auto",
        })

    async def test_inline_tool_images_and_existing_references_are_preserved(self):
        image = self.body["input"][0]["content"][1]
        body = {"input": [
            {"type": "function_call_output", "call_id": "call_1", "output": [image]},
            {"type": "custom_tool_call_output", "call_id": "call_2", "output": [image]},
            {"type": "message", "role": "user", "content": [
                {"type": "input_image", "file_id": "file-existing", "detail": "high"},
                {"type": "input_image", "image_url": "https://example.com/image.png"},
            ]},
        ]}
        rewritten, reused = await self.rewrite(body=body)
        self.assertEqual(rewritten, body)
        self.assertEqual(self.requests, [])
        self.assertFalse(reused)

    async def test_repeated_picture_in_one_message_is_only_uploaded_once(self):
        self.body["input"][0]["content"].append(copy.deepcopy(self.body["input"][0]["content"][1]))
        rewritten, reused = await self.rewrite()
        parts = rewritten["input"][0]["content"]
        self.assertEqual(parts[1], parts[2])
        self.assertFalse(reused)
        self.assertEqual(len(self.requests), 1)

    async def test_cache_eviction_allows_reupload(self):
        with patch.object(excel_images, "_CACHE_SIZE", 1):
            await self.rewrite(account="account-a")
            await self.rewrite(account="account-b")
            _, reused = await self.rewrite(account="account-a")
        self.assertFalse(reused)
        self.assertEqual(len(self.requests), 3)

    async def test_missing_file_id_does_not_cache_a_broken_upload(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"filename": "picture.png"}),
        )) as client:
            with self.assertRaisesRegex(httpx.RemoteProtocolError, "no file ID"):
                await self.uploads.rewrite(self.body, client, {"chatgpt-account-id": "account-a"})
        await self.rewrite()
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
