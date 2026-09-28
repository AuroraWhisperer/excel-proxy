import asyncio
import base64
import copy
import unittest
from unittest.mock import patch

import httpx

import excel_images


class ExcelImageUploadTests(unittest.IsolatedAsyncioTestCase):
    def picture(self, data=b"picture", media_type="image/png"):
        return {
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{base64.b64encode(data).decode()}",
        }

    async def test_invalid_later_picture_is_rejected_before_any_upload(self):
        for image in (
            self.picture(media_type="image/svg+xml"),
            self.picture(b""),
            {"type": "input_image", "image_url": "data:image/png;base64,broken!"},
        ):
            with self.subTest(image=image):
                body = copy.deepcopy(self.body)
                body["input"][0]["content"].append(image)
                with self.assertRaises(ValueError):
                    await self.rewrite(body=body)
        self.assertEqual(self.requests, [])

    async def test_image_byte_and_count_limits_include_inline_tool_results(self):
        for tool_output in (False, True):
            for images, limits in (
                ([self.picture(b"1234")], {"MAX_IMAGE_BYTES": 3}),
                (
                    [self.picture(b"12"), self.picture(b"34")],
                    {"MAX_TOTAL_IMAGE_BYTES": 3},
                ),
                ([self.picture(), self.picture()], {"MAX_IMAGES": 1}),
            ):
                with self.subTest(tool_output=tool_output, limits=limits):
                    item = (
                        {
                            "type": "function_call_output",
                            "call_id": "call_image",
                            "output": images,
                        }
                        if tool_output
                        else {"role": "user", "content": images}
                    )
                    with (
                        patch.multiple(excel_images, create=True, **limits),
                        self.assertRaises(ValueError),
                    ):
                        await self.rewrite(body={"input": [item]})
        self.assertEqual(self.requests, [])

    async def test_independent_pictures_do_not_wait_for_each_other(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def upload(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return "file-slow"
            return "file-fast"

        other = {"input": [{"role": "user", "content": [self.picture(b"different")]}]}
        with patch.object(excel_images, "_upload", side_effect=upload):
            slow = asyncio.create_task(self.rewrite())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                fast, _ = await asyncio.wait_for(self.rewrite(body=other), 1)
                self.assertEqual(fast["input"][0]["content"][0]["file_id"], "file-fast")
            finally:
                release.set()
                await slow

    async def test_concurrent_identical_pictures_share_one_upload(self):
        result = await asyncio.gather(self.rewrite(), self.rewrite())
        self.assertEqual(result[0][0], result[1][0])
        self.assertEqual(len(self.requests), 1)

    async def test_cancelled_upload_does_not_block_the_next_request(self):
        entered = asyncio.Event()

        async def upload(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(excel_images, "_upload", side_effect=upload):
            task = asyncio.create_task(self.rewrite())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(self.rewrite(), 1)
        self.assertEqual(len(self.requests), 1)

    async def asyncSetUp(self):
        self.uploads = excel_images.ExcelImageUploads()
        self.requests = []

        async def upstream(request):
            self.requests.append(request)
            await asyncio.sleep(0)
            return httpx.Response(
                200, json={"openai_file_id": f"file-{len(self.requests)}"}
            )

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.client.aclose)
        self.body = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Read the image"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AAAA",
                        },
                    ],
                }
            ]
        }

    async def rewrite(self, account="account-a", body=None):
        return await self.uploads.rewrite(
            body if body is not None else self.body,
            self.client,
            {"authorization": "Bearer test", "chatgpt-account-id": account},
        )

    async def test_upload_cache_is_account_scoped_and_does_not_mutate_the_original(
        self,
    ):
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
        self.assertEqual(
            first["input"][0]["content"][1],
            {
                "type": "input_image",
                "file_id": "file-1",
                "detail": "auto",
            },
        )

    async def test_inline_tool_images_and_existing_references_are_preserved(self):
        image = self.body["input"][0]["content"][1]
        body = {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": [image],
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_2",
                    "output": [image],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "file_id": "file-existing",
                            "detail": "high",
                        },
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/image.png",
                        },
                    ],
                },
            ]
        }
        rewritten, reused = await self.rewrite(body=body)
        self.assertEqual(rewritten, body)
        self.assertEqual(self.requests, [])
        self.assertFalse(reused)

    async def test_repeated_picture_in_one_message_is_only_uploaded_once(self):
        self.body["input"][0]["content"].append(
            copy.deepcopy(self.body["input"][0]["content"][1])
        )
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
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"filename": "picture.png"}),
            )
        ) as client:
            with self.assertRaisesRegex(httpx.RemoteProtocolError, "no file ID"):
                await self.uploads.rewrite(
                    self.body, client, {"chatgpt-account-id": "account-a"}
                )
        await self.rewrite()
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
