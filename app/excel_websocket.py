"""Carry one prepared Excel Responses request over one upstream WebSocket."""

import asyncio
import json
import re
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

import excel_session
from responses_protocol import sse_encode
from upstream_errors import ExcelResponseError


def websocket_mounts(verify):
    endpoint = httpx.URL(excel_session.RESPONSES_URL)
    if endpoint.scheme not in {"ws", "wss"}:
        return {}
    origin = f"{endpoint.scheme}://{endpoint.netloc.decode('ascii')}"
    return {origin: ExcelWebSocketTransport(verify)}


class _ConnectToEndpoint(connect):
    def process_redirect(self, exc):
        # The subprotocol carries the selected account's bearer grant.
        return exc


_CONTROL_EVENTS = {
    "basispoints.response.heartbeat", "basispoints.response.upstream_sent",
    "basispoints.response.metadata", "basispoints.response.resume_token",
}
_TERMINAL_EVENTS = {"error", "response.completed", "response.failed", "response.incomplete"}


class _EventStream(httpx.AsyncByteStream):
    def __init__(self, socket, read_timeout):
        self.socket = socket
        self.read_timeout = read_timeout
        self.terminal = False

    async def __aiter__(self):
        while not self.terminal:
            try:
                async with asyncio.timeout(self.read_timeout):
                    message = await self.socket.recv()
            except TimeoutError:
                raise ExcelResponseError("excel_ws_timeout", "Excel WebSocket timed out waiting for an event.", status_code=504) from None
            except ConnectionClosed:
                raise ExcelResponseError("excel_ws_disconnected", "Excel WebSocket closed before a terminal event.") from None
            if not isinstance(message, str):
                raise ExcelResponseError("excel_ws_invalid_event", "Expected a text frame from Excel.")
            try:
                event = json.loads(message)
            except ValueError:
                raise ExcelResponseError("excel_ws_invalid_event", "Invalid JSON event from Excel.") from None
            kind = event.get("type") if isinstance(event, dict) else None
            if not isinstance(kind, str):
                raise ExcelResponseError("excel_ws_invalid_event", "Missing Excel WebSocket event type.")
            if kind in _CONTROL_EVENTS:
                continue
            if kind == "basispoints.response.server_draining":
                raise ExcelResponseError("excel_ws_draining", "Excel closed the generation for server maintenance.")
            if kind != "error" and not re.fullmatch(r"response\.[a-z0-9_.]{1,119}", kind):
                raise ExcelResponseError("excel_ws_invalid_event", "Unexpected Excel WebSocket event type.")
            event.pop("basispoints_replay_cursor", None)
            self.terminal = kind in _TERMINAL_EVENTS
            yield sse_encode(kind, event)

    async def aclose(self):
        if self.terminal:
            await self.socket.close()
        else:
            self.socket.transport.abort()
            await self.socket.wait_closed()


class ExcelWebSocketTransport(httpx.AsyncBaseTransport):
    """Expose BPS frames to the existing Responses stream and tool translators."""

    def __init__(self, verify=True):
        self._verify = verify
        self._ssl = None

    def _ssl_context(self):
        if self._ssl is None:
            self._ssl = httpx.create_ssl_context(verify=self._verify)
        return self._ssl

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return httpx.Response(401)
        request_id = str(uuid4())
        profile = {
            key.title(): value for key, value in excel_session.DEFAULT_CLIENT_HEADERS.items()
            if key.startswith("x-openai-internal-basispoints-")
        }
        url = request.url.copy_merge_params({
            "bps_client_info": json.dumps(profile, separators=(",", ":")),
            "bps_auth_mode": "chatgpt",
            "bps_ws_affinity": request_id,
            "bps_control_frames": "upstream_sent,heartbeat",
        })
        payload = json.loads(await request.aread())
        payload.update(
            type="response.create", stream=True, basispoints_request_id=request_id,
            basispoints_resume_token="resume_" + uuid4().hex,
        )
        frame = json.dumps(payload, separators=(",", ":"))
        timeouts = request.extensions.get("timeout", {})
        try:
            socket = await _ConnectToEndpoint(
                str(url), origin="https://bps.openai.com",
                subprotocols=["responses", "openai-bearer." + token],
                user_agent_header="Mozilla/5.0", additional_headers={"Accept": "*/*"},
                compression=None,
                ssl=self._ssl_context() if url.scheme == "wss" else None,
                open_timeout=min(timeouts.get("connect") or 30, 30),
                close_timeout=1, ping_interval=None, max_size=16 * 1024 * 1024, max_queue=4,
            )
        except InvalidStatus as exc:
            rejected = exc.response
            headers = {
                name: value for name, value in rejected.headers.raw_items()
                if name.lower() in {"retry-after", "x-request-id"}
            }
            status = rejected.status_code if 400 <= rejected.status_code <= 599 else 502
            return httpx.Response(status, headers=headers)
        except TimeoutError:
            raise httpx.ConnectTimeout("Excel WebSocket handshake timed out.") from None
        except OSError as exc:
            raise httpx.ConnectError("Could not connect to Excel WebSocket.") from exc
        except (WebSocketException, ValueError):
            raise httpx.RemoteProtocolError("Excel WebSocket handshake failed.") from None

        stream = _EventStream(socket, timeouts.get("read"))
        try:
            async with asyncio.timeout(timeouts.get("write")):
                await socket.send(frame)
        except BaseException as exc:
            await stream.aclose()
            if isinstance(exc, (TimeoutError, ConnectionClosed, OSError)):
                # The upstream may already have accepted the frame.
                raise httpx.WriteError("Could not send the Excel generation frame.") from None
            raise
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream,
            extensions={"excel_websocket": True},
        )
