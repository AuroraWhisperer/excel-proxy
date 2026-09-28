"""Dashboard pages and read-only APIs with explicit runtime dependencies."""

import asyncio
import gzip
import hashlib
import json
import os
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from constants import CONNECTION_PAGE_FILE
import responses_protocol
import util


def create_dashboard_router(
    *, dashboard_service, streaming_response_class
) -> APIRouter:
    router = APIRouter()
    # Each router owns its HTML cache; requests reuse compressed representations.
    _DASHBOARD_HTML_LOCK = threading.Lock()
    _DASHBOARD_HTML_CACHE: dict[str, tuple[int, int, bytes, bytes, str]] = {}

    @router.get("/", response_class=HTMLResponse)
    async def dashboard_root():
        return RedirectResponse(url="/ui", status_code=307)

    def _load_dashboard_html_bytes(
        page: str = "connection.html",
    ) -> tuple[bytes, bytes, str]:
        path = os.path.join(os.path.dirname(CONNECTION_PAGE_FILE), page)
        with _DASHBOARD_HTML_LOCK:
            stat = os.stat(path)
            cached = _DASHBOARD_HTML_CACHE.get(page)
            if cached is None or cached[:2] != (stat.st_mtime_ns, stat.st_size):
                with open(path, "rb") as f:
                    raw = f.read()
                gzipped = gzip.compress(raw, compresslevel=9)
                digest = hashlib.sha256(raw).hexdigest()[:16]
                etag = f'"dash-{digest}"'
                cached = (stat.st_mtime_ns, stat.st_size, raw, gzipped, etag)
                _DASHBOARD_HTML_CACHE[page] = cached
            return cached[2:]

    @router.get("/ui/shared.css")
    async def dashboard_styles():
        return FileResponse(
            os.path.join(os.path.dirname(CONNECTION_PAGE_FILE), "shared.css"),
            media_type="text/css",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/ui/account-login.js")
    async def account_login_script():
        return FileResponse(
            os.path.join(os.path.dirname(CONNECTION_PAGE_FILE), "account-login.js"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/ui/api.js")
    @router.get("/ui/connection.js")
    @router.get("/ui/requests.js")
    @router.get("/ui/usage.js")
    async def page_script(request: Request):
        # Only the explicitly registered filenames above can reach this handler.
        filename = request.url.path.rsplit("/", 1)[-1]
        return FileResponse(
            os.path.join(os.path.dirname(CONNECTION_PAGE_FILE), filename),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/ui/usage", response_class=HTMLResponse)
    @router.get("/ui/requests", response_class=HTMLResponse)
    @router.get("/ui", response_class=HTMLResponse)
    async def dashboard(request: Request):
        page = {"/ui/requests": "requests.html", "/ui/usage": "usage.html"}.get(
            request.url.path, "connection.html"
        )
        raw, gzipped, etag = _load_dashboard_html_bytes(page)
        if request.headers.get("if-none-match") == etag:
            return Response(
                status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"}
            )
        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" in accept_encoding.lower():
            return Response(
                content=gzipped,
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Encoding": "gzip",
                    "Vary": "Accept-Encoding",
                    "Cache-Control": "no-cache",
                    "ETag": etag,
                },
            )
        return Response(
            content=raw,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "ETag": etag,
            },
        )

    def _build_dashboard_response_body(
        refresh: bool, gzip_body: bool = False
    ) -> tuple[bytes, bool]:
        # Browser refreshes are advisory: the stream version already invalidates
        # the in-memory materialized payload when data changes.  Rebuilding an
        # unchanged archive just because the URL contains refresh=1 defeats the
        # dashboard cache.
        payload = dashboard_service.build_payload(refresh, prefer_cached=True)
        body = json.dumps(
            payload, separators=(",", ":"), default=util._json_default
        ).encode("utf-8")
        if gzip_body and len(body) >= 1024:
            return gzip.compress(body, compresslevel=6), True
        return body, False

    def _build_dashboard_sse_event(event_name: str) -> bytes:
        payload = dashboard_service.build_payload(False)
        return responses_protocol.sse_encode(event_name, payload)

    @router.get("/api/dashboard")
    async def dashboard_api(request: Request):
        refresh = request.query_params.get("refresh", "").lower() in {
            "1",
            "true",
            "yes",
        }
        accept_encoding = request.headers.get("accept-encoding", "")
        accepts_gzip = "gzip" in accept_encoding.lower()
        body, encoded = await asyncio.to_thread(
            _build_dashboard_response_body, refresh, accepts_gzip
        )
        headers = {"Cache-Control": "no-store"}
        if encoded:
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        return Response(
            content=body,
            media_type="application/json",
            headers=headers,
        )

    @router.get("/api/dashboard/stream")
    async def dashboard_stream(request: Request):
        heartbeat_seconds = 20
        poll_seconds = 1.0
        queue = dashboard_service.register_stream_listener()
        last_version = dashboard_service.current_stream_version()

        async def stream():
            nonlocal last_version
            # Emit an initial heartbeat so EventSource clients see the stream is
            # live immediately. The page concurrently fetches /api/dashboard, so
            # we deliberately skip a redundant initial dashboard build here and
            # only stream payloads when the version changes.
            yield responses_protocol.sse_encode("heartbeat", {"at": util.utc_now_iso()})
            last_heartbeat = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected():
                        break

                    try:
                        version = await asyncio.wait_for(
                            queue.get(), timeout=poll_seconds
                        )
                    except asyncio.TimeoutError:
                        now = time.monotonic()
                        if now - last_heartbeat >= heartbeat_seconds:
                            last_heartbeat = now
                            yield responses_protocol.sse_encode(
                                "heartbeat", {"at": util.utc_now_iso()}
                            )
                        continue

                    if version == last_version:
                        continue
                    last_version = version
                    chunk = await asyncio.to_thread(
                        _build_dashboard_sse_event, "dashboard"
                    )
                    yield chunk
            finally:
                dashboard_service.unregister_stream_listener(queue)

        return streaming_response_class(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    return router
