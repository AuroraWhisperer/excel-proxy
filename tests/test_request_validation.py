import gzip
import io
import json
import unittest
import zlib
from contextlib import redirect_stdout

import httpx
from fastapi import HTTPException, Request

import proxy
import util


def request_with_body(body, encoding=""):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [(b"content-encoding", encoding.encode())],
        },
        receive,
    )


class RequestValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonobject_json_is_a_client_error_on_model_and_settings_routes(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app, raise_app_exceptions=False),
            base_url="http://127.0.0.1",
        ) as client:
            for path in (
                "/v1/responses",
                "/v1/images/generations",
                "/api/config/client-proxy/settings",
            ):
                for value in ([], [1], "text", 42, False, None):
                    with self.subTest(path=path, value=value):
                        response = await client.post(
                            path,
                            content=json.dumps(value),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(response.status_code, 400)
                        self.assertIn("JSON object", response.text)

    async def test_invalid_json_diagnostics_do_not_include_request_content(self):
        private = b'{"private-prompt":"DO-NOT-LOG-THIS'
        diagnostics = []
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(HTTPException) as raised:
            await util.parse_json_request(
                request_with_body(private), diagnostics.append
            )
        self.assertEqual(raised.exception.status_code, 400)
        rendered = output.getvalue() + json.dumps(diagnostics)
        self.assertNotIn("DO-NOT-LOG-THIS", rendered)
        self.assertNotIn(private[:24].hex(), rendered)
        self.assertNotIn("preview_text", rendered)
        self.assertNotIn("preview_hex", rendered)
        self.assertEqual(diagnostics[0]["body_len"], len(private))

    async def test_supported_compressed_json_objects_still_decode(self):
        value = {"input": "hello"}
        raw = json.dumps(value).encode()
        for encoding, body in (
            ("", raw),
            ("gzip", gzip.compress(raw)),
            ("deflate", zlib.compress(raw)),
            ("zstd", util.zstd_compress(raw)),
        ):
            with self.subTest(encoding=encoding):
                self.assertEqual(
                    await util.parse_json_request(request_with_body(body, encoding)),
                    value,
                )
