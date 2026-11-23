"""Drive the desktop API through a real loopback WebSocket with a fixture grant."""

import asyncio
from http import HTTPStatus
import json
import os
import runpy
import sys
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from websockets.asyncio.server import serve

import excel_image_generation
import excel_images
import excel_session
import excel_upstream
import proxy
import rate_limiting
import responses_stream
import upstream_client


class ExcelWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frames = []
        self.handshakes = []
        self.closed = asyncio.Event()
        self.reject = None
        self.reply = [
            {"type": "basispoints.response.heartbeat"},
            {"type": "response.output_text.delta", "delta": "OK", "basispoints_replay_cursor": "private"},
            {"type": "response.completed", "response": {
                "id": "resp_ws", "status": "completed", "output": [{
                    "type": "message", "id": "msg_ws", "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }],
            }},
        ]

        def handshake(socket, request):
            self.handshakes.append(request)
            if self.reject:
                response = socket.respond(self.reject, "private upstream body")
                response.headers["Retry-After"] = "7"
                response.headers["Location"] = "/redirect-target"
                return response

        async def respond(socket):
            self.frames.append(json.loads(await socket.recv()))
            for event in self.reply:
                await socket.send(json.dumps(event))
            if self.reply:
                await socket.close()
            else:
                await socket.wait_closed()
            self.closed.set()

        server = await self.enterAsyncContext(serve(
            respond, "127.0.0.1", 0, subprotocols=["responses"], process_request=handshake,
        ))
        port = server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/basispoints/api/responses"
        self.enterContext(patch.object(excel_session, "RESPONSES_URL", self.url))
        self.enterContext(patch.object(excel_upstream, "RESPONSES_URL", self.url))
        self.enterContext(patch.object(upstream_client, "_EXCEL_UPSTREAM_CLIENT", None))
        self.client = proxy._get_excel_upstream_client()
        self.addAsyncCleanup(self.client.aclose)
        self.enterContext(patch.object(rate_limiting, "throttle_upstream_request", new=AsyncMock()))
        self.headers = {
            "authorization": "Bearer fixture.grant",
            "chatgpt-account-id": "fixture-account",
            "x-openai-internal-basispoints-client-platform": "excel",
        }
        self.enterContext(patch.object(proxy, "_selected_excel_headers", new=AsyncMock(return_value=self.headers)))
        self.local = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1",
        ))

    async def test_desktop_stream_and_json_use_one_create_frame_each(self):
        for stream in (True, False):
            response = await self.local.post("/v1/responses", json={"input": "hello", "stream": stream})
            self.assertEqual(response.status_code, 200, response.text)
            if stream:
                self.assertIn("event: response.output_text.delta", response.text)
                self.assertIn("event: response.completed", response.text)
            else:
                self.assertEqual(response.json()["output"][0]["content"][0]["text"], "OK")
            self.assertNotIn("heartbeat", response.text)
            self.assertNotIn("basispoints_replay_cursor", response.text)
        self.assertEqual(len(self.frames), 2)
        for handshake, frame in zip(self.handshakes, self.frames):
            self.assertEqual(frame["type"], "response.create")
            self.assertTrue(frame["stream"])
            self.assertIn("model", frame)
            query = parse_qs(urlsplit(handshake.path).query)
            self.assertEqual(query["bps_ws_affinity"], [frame["basispoints_request_id"]])
            self.assertTrue(frame["basispoints_resume_token"].startswith("resume_"))
            self.assertIn("fixture.grant", handshake.headers["Sec-WebSocket-Protocol"])
            self.assertNotIn("fixture.grant", handshake.path)
            self.assertEqual(json.loads(query["bps_client_info"][0])["X-Openai-Internal-Basispoints-Client-Platform"], "excel")

    async def test_no_proxy_with_a_port_keeps_the_websocket_create_frame(self):
        with patch.dict(os.environ, {
            "GHCP_UPSTREAM_PROXY": "http://proxy.fixture.invalid:8111",
            "GHCP_NO_PROXY": urlsplit(self.url).netloc,
            "GHCP_UPSTREAM_TLS_VERIFY": "false",
        }, clear=True):
            async with upstream_client._build_upstream_client(
                http2_override=False, mounts_for=proxy.websocket_mounts,
            ) as client:
                response = await client.post(self.url, headers=self.headers, json={
                    "model": "gpt-6-astra", "input": "hello", "stream": False,
                })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(self.frames[0]["type"], "response.create")
        self.assertTrue(self.frames[0]["stream"])
        self.assertEqual(self.frames[0]["model"], "gpt-6-astra")
        self.assertEqual(
            self.handshakes[0].headers["Sec-WebSocket-Protocol"],
            "responses, openai-bearer.fixture.grant",
        )

    async def test_dashboard_model_probe_traverses_the_same_socket(self):
        response = await self.local.post("/api/config/excel-session/test", json={"model": "gpt-6-astra-excel"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.frames[0]["model"], "gpt-6-astra")

    async def test_repeated_requests_use_a_server_task_and_fresh_turns(self):
        for stream in (True, False):
            for _ in range(2):
                response = await self.local.post("/v1/responses", json={
                    "model": "gpt-6-astra-excel", "input": "same question", "stream": stream,
                    "metadata": {"task_id": "caller-task", "turn_id": "caller-turn"},
                })
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.frames), 4)
        tasks = []
        turns = []
        for frame in self.frames:
            self.assertEqual(frame["model"], "gpt-6-astra")
            metadata = frame["metadata"]
            self.assertRegex(metadata["task_id"], r"^[0-9a-f]{64}$")
            self.assertRegex(metadata["turn_id"], r"^[0-9a-f]{64}$")
            tasks.append(metadata["task_id"])
            turns.append(metadata["turn_id"])
        self.assertEqual(len(set(tasks)), 1)
        self.assertEqual(len(set(turns)), 4)

    async def test_handshake_matches_bps_browser_profile(self):
        self.headers.update({
            "user-agent": "python-httpx/fixture",
            "origin": "https://rest-client.invalid",
            "cookie": "private-rest-cookie",
            "x-openai-internal-basispoints-client-host": "rest-client",
        })
        response = await self.local.post("/v1/responses", json={"input": "hello"})
        self.assertEqual(response.status_code, 200, response.text)
        handshake = self.handshakes[0]
        self.assertEqual(handshake.headers.get("User-Agent"), "Mozilla/5.0")
        self.assertEqual(handshake.headers.get("Accept"), "*/*")
        self.assertEqual(handshake.headers.get("Origin"), "https://bps.openai.com")
        self.assertNotIn("Sec-WebSocket-Extensions", handshake.headers)
        self.assertEqual(
            handshake.headers.get("Sec-WebSocket-Protocol"),
            "responses, openai-bearer.fixture.grant",
        )
        for name in ("Authorization", "Cookie", "Chatgpt-Account-Id", "X-Openai-Account-Id"):
            self.assertNotIn(name, handshake.headers)
        query = parse_qs(urlsplit(handshake.path).query)
        self.assertEqual(query["bps_auth_mode"], ["chatgpt"])
        self.assertEqual(query["bps_control_frames"], ["upstream_sent,heartbeat"])
        profile = json.loads(query["bps_client_info"][0])
        platform = "Mac" if sys.platform == "darwin" else "PC"
        self.assertEqual(profile, {
            "X-Openai-Internal-Basispoints-Client-Product": "basispoints-excel-plugin",
            "X-Openai-Internal-Basispoints-Client-Platform": "excel",
            "X-Openai-Internal-Basispoints-Client-Agent-Profile": "excel",
            "X-Openai-Internal-Basispoints-Client-Editor": "excel",
            "X-Openai-Internal-Basispoints-Client-Host": "office",
            "X-Openai-Internal-Basispoints-Client-Runtime": "desktop",
            "X-Openai-Internal-Basispoints-Client-Platform-Class": platform,
            "X-Openai-Internal-Basispoints-Office-Host": "Excel",
            "X-Openai-Internal-Basispoints-Office-Platform": platform,
            "X-Openai-Internal-Basispoints-Browser-Name": "unknown",
        })

    async def test_handshake_status_and_retry_after_reach_the_api(self):
        self.reject = HTTPStatus.TOO_MANY_REQUESTS
        response = await self.local.post("/v1/responses", json={"input": "hello"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "7")
        self.assertNotIn("private upstream body", response.text)
        self.assertEqual(len(self.handshakes), 1)
        self.assertFalse(self.frames)

    async def test_redirect_never_resends_authorized_handshake(self):
        self.reject = HTTPStatus.FOUND
        response = await self.local.post("/v1/responses", json={"input": "hello"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.handshakes), 1)

    async def test_disconnect_after_create_does_not_replay(self):
        self.reply = [{"type": "basispoints.response.upstream_sent"}]
        response = await self.local.post("/v1/responses", json={"input": "hello"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.frames), 1)
        streamed = await self.local.post("/v1/responses", json={"input": "hello", "stream": True})
        self.assertIn("event: response.failed", streamed.text)
        self.assertIn("excel_ws_disconnected", streamed.text)
        self.assertEqual(len(self.frames), 2)

    async def test_cancelling_open_generation_closes_its_socket(self):
        self.reply = []
        request = self.client.build_request("POST", self.url, headers=self.headers, json={"input": "hello"})
        response = await self.client.send(request, stream=True)
        mode = await responses_stream._close_upstream_response(response, cancel_generation=True)
        self.assertEqual(mode, "websocket_close")
        await asyncio.wait_for(self.closed.wait(), 2)

    async def test_request_rejected_event_surfaces_access_rejection_without_leaking_text(self):
        self.reply = [
            {
                "type": "error",
                "code": "request_rejected",
                "message": "private upstream policy detail",
                "error": {"code": "request_rejected", "message": "private upstream policy detail"},
            }
        ]
        response = await self.local.post("/v1/responses", json={"input": "hello"})
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["code"], "request_rejected")
        self.assertEqual(error["diagnosis"]["category"], "permission")
        self.assertNotIn("private upstream policy detail", response.text)
        probe = await self.local.post("/api/config/excel-session/test", json={"model": "gpt-6-astra-excel"})
        self.assertEqual(probe.status_code, 502)
        self.assertEqual(probe.json()["category"], "access")


class DesktopEndpointsTests(unittest.TestCase):
    def test_client_profile_uses_the_host_platform(self):
        for platform, expected in (("win32", "PC"), ("darwin", "Mac"), ("linux", "PC")):
            with self.subTest(platform=platform), patch("sys.platform", platform):
                headers = runpy.run_path(excel_session.__file__)["DEFAULT_CLIENT_HEADERS"]
                self.assertEqual(headers["x-openai-internal-basispoints-client-platform-class"], expected)
                self.assertEqual(headers["x-openai-internal-basispoints-office-platform"], expected)

    def test_default_generation_and_image_protocols(self):
        self.assertEqual(excel_session.RESPONSES_URL, "wss://bps.openai.com/basispoints/api/responses")
        for url in (excel_images.ATTACHMENTS_URL, excel_image_generation.GENERATIONS_URL, excel_image_generation.EDITS_URL):
            self.assertEqual(urlsplit(url).scheme, "https")
