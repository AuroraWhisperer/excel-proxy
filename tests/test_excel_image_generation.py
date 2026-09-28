import base64
import json
import unittest

import excel_image_generation
import test_excel_contracts


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class ExcelImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_excel_contracts.ExcelHTTPContractTests.asyncSetUp

    def success(self):
        self.upstream_status = 200
        self.upstream_body = {
            "created": 123,
            "data": [{"b64_json": base64.b64encode(PNG).decode()}],
        }

    async def test_generation_aliases_use_session_not_client_credentials(self):
        self.success()
        for path in ("/images/generations", "/v1/images/generations"):
            result = await self.local.post(
                path,
                json={"prompt": "A blue square", "n": 1},
                headers={
                    "authorization": "Bearer CLIENT_PRIVATE",
                    "x-openai-actor-authorization": "not-a-session",
                },
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), self.upstream_body)
            upstream = self.requests[-1]
            self.assertEqual(str(upstream.url), excel_image_generation.GENERATIONS_URL)
            self.assertEqual(
                upstream.headers["authorization"], "Bearer KNOWN_CREDENTIAL"
            )
            self.assertNotIn("x-openai-actor-authorization", upstream.headers)
            self.assertEqual(json.loads(upstream.content)["model"], "gpt-image-2")

    async def test_json_edit_aliases_create_multipart(self):
        self.success()
        for path in ("/images/edits", "/v1/images/edits"):
            for count in (1, 2):
                result = await self.local.post(
                    path,
                    json={
                        "prompt": "Make it red",
                        "images": [{"image_url": DATA_URL}] * count,
                    },
                )
                self.assertEqual(result.status_code, 200, result.text)
                upstream = self.requests[-1]
                self.assertEqual(str(upstream.url), excel_image_generation.EDITS_URL)
                self.assertIn(
                    "multipart/form-data; boundary=", upstream.headers["content-type"]
                )
                self.assertIn(PNG, upstream.content)
                field = b'name="image"' if count == 1 else b'name="image[]"'
                self.assertEqual(upstream.content.count(field), count)

    async def test_non_json_edit_is_rejected_with_instructions(self):
        result = await self.local.post(
            "/v1/images/edits",
            data={"prompt": "Red square"},
            files={"image": ("test.png", PNG, "image/png")},
        )
        self.assertEqual(result.status_code, 415)
        self.assertIn("JSON", result.text)
        self.assertFalse(self.requests)

    async def test_invalid_requests_never_reach_upstream(self):
        cases = [
            {},
            {"prompt": 3},
            {"prompt": "x", "n": True},
            {"prompt": "x", "n": 4},
            {"prompt": "x", "size": "bad"},
            {"prompt": "x", "model": "other"},
            {"prompt": "x", "stream": True},
            {"prompt": "x", "output_format": "jpeg"},
            {"prompt": "x", "background": "transparent"},
        ]
        for body in cases:
            with self.subTest(body=body):
                result = await self.local.post("/images/generations", json=body)
                self.assertEqual(result.status_code, 400, result.text)
        for images in (
            [],
            [{"image_url": "https://example.com/picture.png"}],
            [{"image_url": "data:image/png;base64,@@@"}],
        ):
            result = await self.local.post(
                "/images/edits", json={"prompt": "x", "images": images}
            )
            self.assertEqual(result.status_code, 400, result.text)
        self.assertFalse(self.requests)

    async def test_upstream_errors_and_redirects_do_not_leak(self):
        for status in (302, 401, 403, 429, 500):
            self.upstream_status = status
            result = await self.local.post("/images/generations", json={"prompt": "x"})
            self.assertEqual(result.status_code, status if status >= 400 else 502)
            self.assertNotIn("KNOWN_CREDENTIAL", result.text)
            self.assertNotIn("PRIVATE_PROMPT", result.text)
        self.assertEqual(len(self.requests), 5)

    async def test_missing_session_and_malformed_success_are_errors(self):
        self.headers.side_effect = RuntimeError("Session unavailable")
        result = await self.local.post("/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 401)
        self.assertFalse(self.requests)
        self.headers.side_effect = None
        self.upstream_status = 200
        for payload in ([], {"data": []}, {"data": [{"b64_json": "@@@"}]}):
            self.upstream_body = payload
            result = await self.local.post("/images/generations", json={"prompt": "x"})
            self.assertEqual(result.status_code, 502, result.text)
