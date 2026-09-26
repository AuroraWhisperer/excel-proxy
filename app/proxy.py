"""Local Responses API backed by the signed-in ChatGPT Excel add-in.

Run with the repository virtual environment and open http://127.0.0.1:8000.
"""

import os
import sys


def _prepare_standalone_process_file_descriptors():
    """Avoid inheriting a descriptor table that is already near the soft limit."""
    try:
        import resource
    except ImportError:
        return

    try:
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return

    target_limit = 4096
    if hard_limit != resource.RLIM_INFINITY:
        target_limit = min(target_limit, hard_limit)
    if soft_limit < target_limit:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target_limit, hard_limit))
            soft_limit = target_limit
        except (OSError, ValueError):
            pass

    try:
        close_until = int(soft_limit)
    except (OverflowError, ValueError):
        close_until = 4096
    os.closerange(3, max(3, close_until))


if __name__ == "__main__":
    _prepare_standalone_process_file_descriptors()


import asyncio
import account_quota
import atexit
import background_proxy
import codex_agent_compat
import dashboard as dashboard_module
from contextlib import aclosing
import excel_stream
import excel_image_generation
import excel_images
import excel_session_capture
import excel_upstream
import format_translation
import gzip
import hashlib
import migrate_runtime_paths
import json
import tempfile
import time
import threading
import upstream_errors
import usage_tracking
import util
from collections import OrderedDict, deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from threading import Lock, Thread
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn
from anyio import CancelScope
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect
from event_bus import EventBus
from local_access import LocalAccessMiddleware
from proxy_client_config import (
    ProxyClientConfig,
    ProxyClientConfigService,
    normalize_proxy_targets,
)

# ─── Import from new modules ─────────────────────────────────────────────────

from constants import (
    CLIENT_PROXY_SETTINGS_FILE,
    DASHBOARD_FILE,
    CODEX_PRIMARY_CONFIG_FILE,
    CODEX_MANAGED_CONFIG_FILE,
    CODEX_PROXY_MODEL_CATALOG_FILE,
    CODEX_PROXY_CONFIG,
    CODEX_PROXY_MODEL_CONTEXT_WINDOW,
    CODEX_PROXY_MODEL_AUTO_COMPACT_TOKEN_LIMIT,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    PROXY_PID_FILE,
    REQUEST_TRACE_LOG_FILE,
    REQUEST_PROMPT_ARCHIVE_DIR,
    REQUEST_TRACE_HISTORY_LIMIT,
    REQUEST_TRACE_RETENTION_SLACK,
    REQUEST_TRACE_BODY_MAX_BYTES,
    REQUEST_PROMPT_PREVIEW_MAX_CHARS,
    TOKEN_DIR,
)

from rate_limiting import throttled_client_send


# ─── App & Global State ──────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(LocalAccessMiddleware)
_REQUEST_TRACE_LOCK = Lock()
_REQUEST_PROMPT_LOCK = Lock()
_REQUEST_PROMPT_ACTIVE_IDS: set[str] = set()
_REQUEST_PROMPT_FILE_PREFIX = "request-prompt-"
# Prompt archives are retained for drill-downs, but pruning the directory on
# every completed request turns normal proxy traffic into a directory scan and
# delete workload.  Keep the cleanup opportunistic and amortized.
_REQUEST_PROMPT_PRUNE_INTERVAL_SECONDS = 60.0
_REQUEST_PROMPT_LAST_PRUNED_MONOTONIC = 0.0
_CLIENT_PROXY_STARTUP_RESTORE_LOCK = Lock()
_CLIENT_PROXY_STARTUP_RESTORE_COMPLETE = False
_CLIENT_PROXY_SHUTDOWN_REVERT_LOCK = Lock()
_CLIENT_PROXY_SHUTDOWN_REVERT_COMPLETE = False
migrated_runtime_files = migrate_runtime_paths.migrate_legacy_runtime_files()
if migrated_runtime_files:
    print(f"runtime migration: copied {len(migrated_runtime_files)} legacy file(s)", flush=True)
_TRACE_HEADER_ALLOWLIST = {
    "content-type",
    "user-agent",
    "openai-intent",
    "editor-version",
    "editor-plugin-version",
    "x-initiator",
    "session_id",
    "x-client-request-id",
    "x-openai-subagent",
    "x-interaction-id",
    "x-interaction-type",
    "x-agent-task-id",
    "x-parent-agent-id",
    "x-client-session-id",
    "x-client-machine-id",
    "x-stainless-retry-count",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "accept-language",
    "sec-fetch-mode",
    "x-request-id",
    "accept",
    "accept-encoding",
    "host",
    "connection",
    "content-length",
}
DEBUG_DETAIL_CONTEXT_REQUESTS = 10

_DEBUG_DETAIL_CAPTURE_LOCK = threading.Lock()
DEBUG_DETAIL_SESSION_BUFFER_LIMIT = 64
_DEBUG_DETAIL_REQUEST_SNAPSHOT_INDEX_MAXLEN = DEBUG_DETAIL_SESSION_BUFFER_LIMIT
DEBUG_DETAIL_SESSION_DETAIL_LIMIT = DEBUG_DETAIL_CONTEXT_REQUESTS
_DEBUG_DETAIL_SESSION_RECENT_REQUESTS: OrderedDict[str, deque[dict]] = OrderedDict()
_DEBUG_DETAIL_REQUEST_SNAPSHOTS_BY_ID: OrderedDict[str, dict] = OrderedDict()
_DEBUG_DETAIL_SESSION_CAPTURED_REQUEST_IDS: OrderedDict[str, set[str]] = OrderedDict()
_DEBUG_DETAIL_SNAPSHOT_SEQUENCE = 0


def _reset_debug_detail_capture_state() -> None:
    global _DEBUG_DETAIL_SNAPSHOT_SEQUENCE
    with _DEBUG_DETAIL_CAPTURE_LOCK:
        _DEBUG_DETAIL_SESSION_RECENT_REQUESTS.clear()
        _DEBUG_DETAIL_REQUEST_SNAPSHOTS_BY_ID.clear()
        _DEBUG_DETAIL_SESSION_CAPTURED_REQUEST_IDS.clear()
        _DEBUG_DETAIL_SNAPSHOT_SEQUENCE = 0


usage_event_bus = EventBus()


class GracefulStreamingResponse(StreamingResponse):
    """Suppress shutdown/disconnect cancellation noise for long-lived streams."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except (asyncio.CancelledError, ClientDisconnect):
            return
        finally:
            # ASGI 2.3 can return normally after its disconnect task cancels
            # the streaming task, while ASGI 2.4 raises outside the iterator.
            # Always close the body owner, including when response.start fails
            # before the first body iteration.
            close_iterator = getattr(self.body_iterator, "aclose", None)
            if callable(close_iterator):
                with CancelScope(shield=True):
                    await close_iterator()


def request_prompt_archive_dir() -> str:
    configured = str(os.environ.get("GHCP_REQUEST_PROMPT_ARCHIVE_DIR", "")).strip()
    return os.path.expanduser(configured or REQUEST_PROMPT_ARCHIVE_DIR)


def _request_prompt_file_name(request_id: str | None) -> str | None:
    if not isinstance(request_id, str):
        return None
    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        return None
    safe_request_id = "".join(
        ch if ch.isalnum() or ch in {"-", "_", "."} else "_"
        for ch in normalized_request_id
    )
    if not safe_request_id:
        return None
    return f"{_REQUEST_PROMPT_FILE_PREFIX}{safe_request_id}.json"


def _request_prompt_file_path(request_id: str | None) -> str | None:
    filename = _request_prompt_file_name(request_id)
    if filename is None:
        return None
    return os.path.join(request_prompt_archive_dir(), filename)


def _save_request_prompt_record(
    request_id: str | None,
    request_path: str | None,
    request_body: dict | None,
) -> None:
    archive_path = _request_prompt_file_path(request_id)
    if archive_path is None or not isinstance(request_body, dict):
        return

    prompt_text = util.extract_request_prompt_text(request_body)
    if not prompt_text:
        return

    record = {
        "request_id": request_id,
        "path": request_path,
        "stored_at": util.utc_now_iso(),
        "char_count": len(prompt_text),
        "prompt_text": prompt_text,
    }
    archive_dir = os.path.dirname(archive_path)
    temp_path = None
    try:
        os.makedirs(archive_dir, exist_ok=True)
        with _REQUEST_PROMPT_LOCK:
            _REQUEST_PROMPT_ACTIVE_IDS.add(str(request_id))
            fd, temp_path = tempfile.mkstemp(prefix="request-prompt-", suffix=".tmp", dir=archive_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":"), default=util._json_default))
            os.replace(temp_path, archive_path)
    except OSError as exc:
        with _REQUEST_PROMPT_LOCK:
            _REQUEST_PROMPT_ACTIVE_IDS.discard(str(request_id))
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        print(f"Warning: failed to write request prompt archive: {exc}", file=sys.stderr, flush=True)


def _load_request_prompt_record(request_id: str | None) -> dict | None:
    archive_path = _request_prompt_file_path(request_id)
    if archive_path is None or not os.path.exists(archive_path):
        return None
    try:
        with open(archive_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    prompt_text = payload.get("prompt_text")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        return None

    char_count = payload.get("char_count")
    if not isinstance(char_count, int):
        char_count = len(prompt_text)
    return {
        "request_id": payload.get("request_id") if isinstance(payload.get("request_id"), str) else request_id,
        "path": payload.get("path") if isinstance(payload.get("path"), str) else None,
        "stored_at": payload.get("stored_at") if isinstance(payload.get("stored_at"), str) else None,
        "char_count": char_count,
        "prompt_text": prompt_text,
    }


def _recent_request_prompt_ids() -> set[str]:
    keep_ids = {
        request_id
        for event in usage_tracker.snapshot_usage_events()
        if isinstance(event, dict)
        for request_id in [event.get("request_id")]
        if isinstance(request_id, str) and request_id
    }
    with _REQUEST_PROMPT_LOCK:
        keep_ids.update(_REQUEST_PROMPT_ACTIVE_IDS)
    return keep_ids


def _prune_request_prompt_archive(request_ids: set[str] | None = None) -> None:
    global _REQUEST_PROMPT_LAST_PRUNED_MONOTONIC
    if request_ids is None:
        now = time.monotonic()
        with _REQUEST_PROMPT_LOCK:
            if now - _REQUEST_PROMPT_LAST_PRUNED_MONOTONIC < _REQUEST_PROMPT_PRUNE_INTERVAL_SECONDS:
                return
            _REQUEST_PROMPT_LAST_PRUNED_MONOTONIC = now

    archive_dir = request_prompt_archive_dir()
    if not os.path.isdir(archive_dir):
        return

    keep_ids = set(request_ids or _recent_request_prompt_ids())
    keep_files = {
        filename
        for request_id in keep_ids
        if (filename := _request_prompt_file_name(request_id)) is not None
    }
    try:
        with _REQUEST_PROMPT_LOCK:
            for entry in os.listdir(archive_dir):
                if not entry.startswith(_REQUEST_PROMPT_FILE_PREFIX) or not entry.endswith(".json"):
                    continue
                if entry in keep_files:
                    continue
                try:
                    os.unlink(os.path.join(archive_dir, entry))
                except OSError:
                    continue
    except OSError as exc:
        print(f"Warning: failed to prune request prompt archive: {exc}", file=sys.stderr, flush=True)


def _handle_usage_event_recorded(event: dict | None) -> None:
    request_id = event.get("request_id") if isinstance(event, dict) else None
    if isinstance(request_id, str) and request_id:
        with _REQUEST_PROMPT_LOCK:
            _REQUEST_PROMPT_ACTIVE_IDS.discard(request_id)
    _prune_request_prompt_archive()
    dashboard_service.notify_dashboard_stream_listeners()


usage_tracker = usage_tracking.UsageTracker(
    state=usage_tracking.UsageTrackingState(),
    archive_store=dashboard_module.create_usage_archive_store(),
    event_bus=usage_event_bus,
    on_usage_event_recorded=_handle_usage_event_recorded,
)

client_proxy_config_service = ProxyClientConfigService(
    ProxyClientConfig(
        codex_primary_config_file=CODEX_PRIMARY_CONFIG_FILE,
        codex_managed_config_file=CODEX_MANAGED_CONFIG_FILE,
        codex_model_catalog_file=CODEX_PROXY_MODEL_CATALOG_FILE,
        codex_proxy_config=CODEX_PROXY_CONFIG,
        codex_model_context_window=CODEX_PROXY_MODEL_CONTEXT_WINDOW,
        codex_model_auto_compact_token_limit=CODEX_PROXY_MODEL_AUTO_COMPACT_TOKEN_LIMIT,
        client_proxy_settings_file=CLIENT_PROXY_SETTINGS_FILE,
    ),
)
background_proxy_manager = background_proxy.BackgroundProxyManager()
def _debug_prompt_logging_settings() -> dict[str, object]:
    try:
        settings = client_proxy_config_service.load_client_proxy_settings()
    except Exception:
        return {}
    return settings if isinstance(settings, dict) else {}


def _debug_prompt_logging_enabled() -> bool:
    return bool(_debug_prompt_logging_settings().get("debug_prompt_logging_enabled", False))


def _prompt_logging_permitted() -> bool:
    return _debug_prompt_logging_enabled()


def _prompt_trace_value(value):
    return value


def _prompt_payload_for_dashboard(value):
    return value


def _client_proxy_settings_with_trace_status(payload: dict[str, object]) -> dict[str, object]:
    return dict(payload)


def _save_client_proxy_settings(payload: dict) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    result = client_proxy_config_service.save_client_proxy_settings({
        "revert_on_shutdown": bool(payload.get("revert_on_shutdown", True)),
        "debug_prompt_logging_enabled": bool(payload.get("debug_prompt_logging_enabled", False)),
    })
    try:
        dashboard_service.notify_dashboard_stream_listeners()
    except NameError:
        pass
    return _client_proxy_settings_with_trace_status(result)


dashboard_service = dashboard_module.create_dashboard_service(
    dependencies=dashboard_module.DashboardDependencies(
        snapshot_all_usage_events=usage_tracker.snapshot_all_usage_events,
        snapshot_usage_events=usage_tracker.snapshot_usage_events,
        native_lifecycle_revision=usage_tracker.native_lifecycle_revision,
        usage_snapshots_are_deduplicated=True,
        prompt_payload=_prompt_payload_for_dashboard,
    ),
    utc_now=util.utc_now,
    utc_now_iso=util.utc_now_iso,
    thread_class=Thread,
)


async def parse_json_request(request: Request) -> dict:
    return await util.parse_json_request(request, error_callback=usage_tracker.record_request_error)


def configured_upstream_timeout_seconds() -> int:
    raw = str(os.environ.get("GHCP_UPSTREAM_TIMEOUT_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        print(
            f"Warning: ignoring invalid GHCP_UPSTREAM_TIMEOUT_SECONDS={raw!r}; using {DEFAULT_UPSTREAM_TIMEOUT_SECONDS}",
            file=sys.stderr,
            flush=True,
        )
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    if value <= 0:
        print(
            f"Warning: GHCP_UPSTREAM_TIMEOUT_SECONDS must be > 0; using {DEFAULT_UPSTREAM_TIMEOUT_SECONDS}",
            file=sys.stderr,
            flush=True,
        )
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    return value


def _first_non_empty_env(names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for name in names:
        raw = os.environ.get(name)
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if value:
            return value, name
    return None, None


def _apply_upstream_proxy_env_aliases() -> tuple[str, ...]:
    """Apply GHCP-specific proxy aliases to standard HTTP(S)_PROXY keys.

    httpx reads standard proxy environment variables when ``trust_env=True``.
    This helper lets operators provide GHCP-specific aliases (for example in a
    launchd plist) without overwriting already-defined standard values.
    """

    applied: list[str] = []
    https_proxy, _ = _first_non_empty_env(("HTTPS_PROXY", "https_proxy"))
    http_proxy, _ = _first_non_empty_env(("HTTP_PROXY", "http_proxy"))
    no_proxy, _ = _first_non_empty_env(("NO_PROXY", "no_proxy"))
    ghcp_proxy, _ = _first_non_empty_env(("GHCP_UPSTREAM_PROXY",))
    ghcp_https_proxy, _ = _first_non_empty_env(("GHCP_HTTPS_PROXY",))
    ghcp_http_proxy, _ = _first_non_empty_env(("GHCP_HTTP_PROXY",))
    ghcp_no_proxy, _ = _first_non_empty_env(("GHCP_NO_PROXY",))

    if https_proxy is None:
        chosen_https = ghcp_https_proxy or ghcp_proxy
        if chosen_https:
            os.environ["HTTPS_PROXY"] = chosen_https
            os.environ.setdefault("https_proxy", chosen_https)
            applied.append("HTTPS_PROXY")
    if http_proxy is None:
        chosen_http = ghcp_http_proxy or ghcp_proxy
        if chosen_http:
            os.environ["HTTP_PROXY"] = chosen_http
            os.environ.setdefault("http_proxy", chosen_http)
            applied.append("HTTP_PROXY")
    if no_proxy is None and ghcp_no_proxy:
        os.environ["NO_PROXY"] = ghcp_no_proxy
        os.environ.setdefault("no_proxy", ghcp_no_proxy)
        applied.append("NO_PROXY")

    return tuple(applied)


def _optional_env_bool(name: str) -> bool | None:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    print(
        f"Warning: ignoring invalid {name}={raw!r}; expected true/false",
        file=sys.stderr,
        flush=True,
    )
    return None


def _upstream_proxy_configured() -> bool:
    https_proxy, _ = _first_non_empty_env(
        ("HTTPS_PROXY", "https_proxy", "GHCP_HTTPS_PROXY", "GHCP_UPSTREAM_PROXY"),
    )
    http_proxy, _ = _first_non_empty_env(
        ("HTTP_PROXY", "http_proxy", "GHCP_HTTP_PROXY", "GHCP_UPSTREAM_PROXY"),
    )
    return bool(https_proxy or http_proxy)


def _configured_upstream_tls_verify(proxy_configured: bool) -> tuple[bool, str]:
    explicit = _optional_env_bool("GHCP_UPSTREAM_TLS_VERIFY")
    if explicit is not None:
        return explicit, "GHCP_UPSTREAM_TLS_VERIFY"
    if proxy_configured:
        # Enterprise HTTPS interception proxies often terminate TLS with an
        # internal CA that isn't present in certifi-based trust stores.
        return False, "proxy_default"
    return True, "default"


def _configured_upstream_http2(proxy_configured: bool) -> tuple[bool, str]:
    explicit = _optional_env_bool("GHCP_UPSTREAM_HTTP2")
    if explicit is not None:
        return explicit, "GHCP_UPSTREAM_HTTP2"
    if proxy_configured:
        return False, "proxy_default"
    return True, "default"


_EXCEL_UPSTREAM_CLIENT: "httpx.AsyncClient | None" = None
_UPSTREAM_CLIENT_LOCK = threading.Lock()
_UPSTREAM_CLIENT_SHUTDOWN_REGISTERED = False


def _build_upstream_client(
    *,
    http2_override: bool | None = None,
) -> "httpx.AsyncClient":
    proxy_aliases = _apply_upstream_proxy_env_aliases()
    if proxy_aliases:
        print(
            f"Configured upstream proxy environment aliases: {', '.join(proxy_aliases)}",
            flush=True,
        )
    proxy_configured = _upstream_proxy_configured()
    tls_verify, tls_verify_source = _configured_upstream_tls_verify(proxy_configured)
    if http2_override is None:
        upstream_http2, upstream_http2_source = _configured_upstream_http2(proxy_configured)
    else:
        upstream_http2, upstream_http2_source = http2_override, "client_override"
    if not tls_verify and tls_verify_source == "proxy_default":
        print(
            "Upstream proxy detected: defaulting GHCP upstream TLS verification off. "
            "Set GHCP_UPSTREAM_TLS_VERIFY=1 once a trusted proxy CA bundle is configured.",
            flush=True,
        )
    elif not tls_verify:
        print(
            "GHCP_UPSTREAM_TLS_VERIFY disabled: upstream TLS certificates will not be validated.",
            flush=True,
        )
    if not upstream_http2 and upstream_http2_source == "proxy_default":
        print(
            "Upstream proxy detected: defaulting GHCP upstream HTTP/2 off for compatibility.",
            flush=True,
        )
    timeout = httpx.Timeout(configured_upstream_timeout_seconds())
    limits = httpx.Limits(
        max_connections=8,
        max_keepalive_connections=4,
        keepalive_expiry=300.0,
    )
    try:
        return httpx.AsyncClient(
            http2=upstream_http2,
            timeout=timeout,
            limits=limits,
            verify=tls_verify,
            trust_env=True,
        )
    except (ImportError, RuntimeError):
        return httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            verify=tls_verify,
            trust_env=True,
        )


def _ensure_upstream_client_shutdown_registered() -> None:
    global _UPSTREAM_CLIENT_SHUTDOWN_REGISTERED
    if not _UPSTREAM_CLIENT_SHUTDOWN_REGISTERED:
        atexit.register(_shutdown_upstream_client)
        _UPSTREAM_CLIENT_SHUTDOWN_REGISTERED = True


def _get_excel_upstream_client() -> "httpx.AsyncClient":
    """Reuse the Excel HTTP/1.1 transport across requests."""
    global _EXCEL_UPSTREAM_CLIENT
    if _EXCEL_UPSTREAM_CLIENT is not None:
        return _EXCEL_UPSTREAM_CLIENT
    with _UPSTREAM_CLIENT_LOCK:
        if _EXCEL_UPSTREAM_CLIENT is None:
            _EXCEL_UPSTREAM_CLIENT = _build_upstream_client(http2_override=False)
            _ensure_upstream_client_shutdown_registered()
    return _EXCEL_UPSTREAM_CLIENT


class _DownstreamDisconnectedBeforeResponse(RuntimeError):
    def __init__(self, transport_close: str):
        super().__init__("downstream disconnected before the upstream response started")
        self.transport_close = transport_close


async def _wait_for_downstream_disconnect(request: Request) -> None:
    """Wait on the ASGI receive channel after the request body was consumed."""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def _open_streaming_upstream(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    trace_plan: "UpstreamRequestPlan | None",
    downstream_request: Request | None,
    active_stream: "_ActiveResponsesStream | None" = None,
) -> httpx.Response:
    """Open an upstream stream while observing pre-response disconnects.

    Starlette cannot monitor the downstream until a Response object is
    returned. A client can cancel while this function is still waiting for
    upstream headers, so own that earlier ASGI window here.
    Once the upstream send has begun, wait for its response handle and cancel
    the actual wire stream instead of abandoning an untracked generation.
    """
    send_started = False

    async def open_upstream() -> httpx.Response:
        nonlocal send_started
        send_started = True
        if active_stream is not None:
            active_stream.send_started = True
        upstream = await throttled_client_send(client, request, stream=True)
        if active_stream is not None:
            active_stream.upstream = upstream
        return upstream

    upstream_task = asyncio.create_task(open_upstream())
    disconnect_task = (
        asyncio.create_task(_wait_for_downstream_disconnect(downstream_request))
        if downstream_request is not None
        else None
    )
    try:
        if disconnect_task is None:
            return await asyncio.shield(upstream_task)
        done, _pending = await asyncio.wait(
            {upstream_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task not in done:
            disconnect_task.cancel()
            with CancelScope(shield=True):
                try:
                    await disconnect_task
                except asyncio.CancelledError:
                    pass
            return await upstream_task

        if not send_started:
            upstream_task.cancel()
            with CancelScope(shield=True):
                try:
                    await upstream_task
                except (asyncio.CancelledError, Exception):
                    pass
            raise _DownstreamDisconnectedBeforeResponse("not_sent")

        # Do not cancel httpx while it is waiting for headers.  On HTTP/2 that
        # can discard the only object through which we can send RST_STREAM.
        # Wait for the handle, then explicitly end the server-side generation.
        try:
            upstream = await asyncio.shield(upstream_task)
        except httpx.RequestError:
            raise _DownstreamDisconnectedBeforeResponse(
                "pre_response_request_error"
            )
        transport_close = await _close_upstream_response(
            upstream,
            cancel_generation=True,
        )
        raise _DownstreamDisconnectedBeforeResponse(transport_close)
    except asyncio.CancelledError:
        # Preserve the same ownership guarantee if the ASGI server cancels the
        # route task directly instead of delivering http.disconnect.
        with CancelScope(shield=True):
            if not send_started:
                upstream_task.cancel()
            try:
                upstream = await upstream_task
            except (asyncio.CancelledError, Exception):
                upstream = None
            if upstream is not None and active_stream is None:
                await _close_upstream_response(upstream, cancel_generation=True)
        raise
    finally:
        if disconnect_task is not None and not disconnect_task.done():
            disconnect_task.cancel()
            with CancelScope(shield=True):
                try:
                    await disconnect_task
                except asyncio.CancelledError:
                    pass


def _shutdown_upstream_client() -> None:
    global _EXCEL_UPSTREAM_CLIENT
    clients = [
        client
        for client in (_EXCEL_UPSTREAM_CLIENT,)
        if client is not None
    ]
    _EXCEL_UPSTREAM_CLIENT = None
    if not clients:
        return
    try:
        loop = asyncio.new_event_loop()
        try:
            for client in clients:
                loop.run_until_complete(client.aclose())
        finally:
            loop.close()
    except Exception:
        pass


def _write_proxy_pid_file() -> None:
    try:
        os.makedirs(os.path.dirname(PROXY_PID_FILE), exist_ok=True)
        with open(PROXY_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
            f.write("\n")
    except OSError as exc:
        print(f"Warning: failed to write proxy pid file: {exc}", file=sys.stderr, flush=True)


def _remove_proxy_pid_file() -> None:
    try:
        with open(PROXY_PID_FILE, encoding="utf-8") as f:
            recorded_pid = f.read().strip()
    except OSError:
        return
    if recorded_pid != str(os.getpid()):
        return
    try:
        os.remove(PROXY_PID_FILE)
    except OSError:
        pass


usage_tracker.load_archived_history()
usage_tracker.load_history()
dashboard_module.initialize()


@app.on_event("startup")
async def _app_startup_restore_client_proxy_configs():
    excel_upstream.excel_session_store.load()
    asyncio.create_task(asyncio.to_thread(
        excel_session_capture.refresh_macos_excel_session,
        excel_upstream.excel_session_store,
        force=True,
    ))
    asyncio.create_task(asyncio.to_thread(
        excel_session_capture.refresh_windows_excel_session,
        excel_upstream.excel_session_store,
        force=True,
    ))
    restore_client_proxy_configs_on_startup()
    client_proxy_config_service.refresh_client_model_metadata()


@app.on_event("shutdown")
async def _app_shutdown_revert_client_proxy_configs():
    revert_client_proxy_configs_on_shutdown()


def _extract_upstream_json_payload(upstream: httpx.Response) -> dict | None:
    content_type = upstream.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return None
    try:
        payload = upstream.json()
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_upstream_text(upstream: httpx.Response) -> str | None:
    try:
        text = upstream.text
    except Exception:
        return None
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return text[:4096]


@dataclass
class UpstreamRequestPlan:
    request_id: str
    upstream_url: str
    headers: dict
    body: dict
    usage_event: dict | None
    requested_model: str | None
    resolved_model: str | None
    source_body: dict | None = None
    replay_subagent: str | None = None
    trace_context: dict | None = None
    debug_detail_session_key: str | None = None
    request_affinity: str | None = None


@dataclass
class _ActiveResponsesStream:
    identity: tuple[str, str]
    request_id: str
    sequence: int
    plan: UpstreamRequestPlan
    task: asyncio.Task
    upstream: httpx.Response | None = None
    superseded_by: str | None = None
    transport_cancel: str | None = None
    cancel_requested: bool = False
    send_started: bool = False
    response_ready: asyncio.Event = field(default_factory=asyncio.Event)
    stream_body: object | None = None
    completed_event_seen: bool = False
    transport_cancel_attempt: str | None = None
    teardown_confirmed: bool = False
    teardown_complete: asyncio.Event = field(default_factory=asyncio.Event)


class _ResponsesSupersessionBlocked(RuntimeError):
    def __init__(self, results: list[dict]):
        super().__init__("prior same-lineage generation cancellation was not confirmed")
        self.results = results


_ACTIVE_RESPONSES_STREAMS_LOCK = threading.Lock()
_ACTIVE_RESPONSES_STREAM_SEQUENCE = 0
_ACTIVE_RESPONSES_STREAMS: dict[
    tuple[str, str],
    dict[str, _ActiveResponsesStream],
] = {}


def _task_is_cancelling(task: asyncio.Task | None) -> bool:
    if task is None:
        return False
    cancelling = getattr(task, "cancelling", None)
    return bool(cancelling()) if callable(cancelling) else False


def _responses_plan_is_user_steering(plan: "UpstreamRequestPlan | None") -> bool:
    if not isinstance(plan, UpstreamRequestPlan):
        return False
    trace_context = plan.trace_context if isinstance(plan.trace_context, dict) else {}
    verdict = trace_context.get("initiator_verdict")
    if not isinstance(verdict, dict):
        return False
    # The candidate reflects the actual latest input shape.
    return str(verdict.get("candidate_initiator") or "").strip().lower() == "user"


def _responses_plan_header_value(
    plan: "UpstreamRequestPlan | None",
    header_name: str,
) -> str | None:
    if not isinstance(plan, UpstreamRequestPlan):
        return None
    headers = plan.headers if isinstance(plan.headers, dict) else None
    if not headers:
        return None
    wanted = header_name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _responses_plan_lineage(plan: "UpstreamRequestPlan | None") -> str | None:
    if not isinstance(plan, UpstreamRequestPlan):
        return None
    agent_task_id = _responses_plan_header_value(plan, "x-agent-task-id")
    if agent_task_id:
        return agent_task_id
    body = plan.body if isinstance(plan.body, dict) else None
    if isinstance(body, dict):
        pck = body.get("prompt_cache_key") or body.get("promptCacheKey")
        if isinstance(pck, str):
            normalized = pck.strip()
            if len(normalized) >= 36 and normalized[8:9] == "-":
                return normalized
    return None


def _responses_plan_uses_native_upstream(plan: "UpstreamRequestPlan | None") -> bool:
    if not isinstance(plan, UpstreamRequestPlan) or not isinstance(plan.body, dict):
        return False
    trace_context = plan.trace_context if isinstance(plan.trace_context, dict) else {}
    client_path = str(trace_context.get("client_path") or "").rstrip("/").lower()
    if client_path.endswith("/responses/compact"):
        return False
    if format_translation.input_contains_compaction(plan.body.get("input")):
        return False
    upstream_path = str(trace_context.get("upstream_path") or "").strip()
    if not upstream_path:
        upstream_path = urlsplit(plan.upstream_url).path
    return upstream_path.rstrip("/").lower().endswith("/responses")


def _responses_active_stream_identity(
    plan: "UpstreamRequestPlan | None",
) -> tuple[str, str] | None:
    if not _responses_plan_uses_native_upstream(plan) or not isinstance(plan, UpstreamRequestPlan):
        return None
    # The fallback task ID hashes the latest user text and can collide across
    # unrelated no-affinity requests. Only coordinate requests carrying a
    # durable conversation affinity, then key by the derived task lineage
    # without the model so steering across a model switch still stops the old
    # generation.
    explicit_affinity = (
        plan.request_affinity.strip()
        if isinstance(plan.request_affinity, str) and plan.request_affinity.strip()
        else None
    )
    for candidate in (plan.source_body, plan.body):
        if explicit_affinity is not None:
            break
        if not isinstance(candidate, dict):
            continue
        for key in ("prompt_cache_key", "promptCacheKey", "session_id", "sessionId"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                explicit_affinity = value.strip()
                break
        if explicit_affinity is None:
            metadata = candidate.get("metadata")
            if isinstance(metadata, dict):
                for key in ("session_id", "sessionId"):
                    value = metadata.get(key)
                    if isinstance(value, str) and value.strip():
                        explicit_affinity = value.strip()
                        break
        if explicit_affinity is not None:
            break
    if explicit_affinity is None and isinstance(plan.usage_event, dict):
        event_session_id = plan.usage_event.get("session_id")
        if isinstance(event_session_id, str) and event_session_id.strip():
            explicit_affinity = event_session_id.strip()
    if explicit_affinity is None:
        return None
    lineage = _responses_plan_lineage(plan)
    return "responses", lineage or _trace_hash(explicit_affinity)


def _httpcore_http2_stream(upstream: httpx.Response):
    """Best-effort access to httpcore's HTTP/2 response stream.

    httpx/httpcore currently release local HTTP/2 stream state on
    ``Response.aclose()`` without sending RST_STREAM.  Keep this isolated and
    defensive so a dependency layout change falls back to ordinary close.
    """
    http_version = upstream.extensions.get("http_version")
    if http_version not in {b"HTTP/2", "HTTP/2"}:
        return None
    bound_stream = getattr(upstream, "stream", None)
    transport_stream = getattr(bound_stream, "_stream", None)
    pool_stream = getattr(transport_stream, "_httpcore_stream", None)
    core_stream = getattr(pool_stream, "_stream", None)
    if not all(
        hasattr(core_stream, attr)
        for attr in ("_connection", "_request", "_stream_id", "_closed")
    ):
        return None
    connection = core_stream._connection
    if not all(
        hasattr(connection, attr)
        for attr in ("_h2_state", "_write_outgoing_data")
    ):
        return None
    return core_stream


async def _reset_http2_upstream_stream(upstream: httpx.Response) -> tuple[bool, str]:
    core_stream = _httpcore_http2_stream(upstream)
    if core_stream is None:
        return False, "unavailable"
    if core_stream._closed:
        return False, "already_closed"
    try:
        # RFC 7540 error code 0x8 is CANCEL. Sending it matters: httpcore's
        # normal response close only forgets the local stream and can leave the
        # model generating on the server.
        core_stream._connection._h2_state.reset_stream(
            core_stream._stream_id,
            error_code=0x8,
        )
        await core_stream._connection._write_outgoing_data(core_stream._request)
        return True, "sent"
    except Exception:
        return False, "failed"


async def _close_upstream_response(
    upstream: httpx.Response,
    *,
    cancel_generation: bool = False,
) -> str:
    """Close an upstream response even when its ASGI body task was cancelled."""
    reset_sent = False
    reset_status = None
    close_failed = False
    http_version = upstream.extensions.get("http_version")
    with CancelScope(shield=True):
        if cancel_generation:
            reset_sent, reset_status = await _reset_http2_upstream_stream(upstream)
        try:
            await upstream.aclose()
        except Exception:
            # Finalization and trace bookkeeping still need to run if the
            # transport itself is already broken.
            close_failed = True
    if reset_sent:
        return "http2_rst_cancel"
    if close_failed:
        return "response_close_failed"
    if cancel_generation and http_version in {b"HTTP/1.0", b"HTTP/1.1", "HTTP/1.0", "HTTP/1.1"}:
        # httpcore closes an HTTP/1.x socket when a response body is abandoned,
        # which is the wire-level cancellation mechanism for that protocol.
        return "http1_connection_close"
    if cancel_generation and http_version in {b"HTTP/2", "HTTP/2"}:
        return f"http2_reset_{reset_status or 'unknown'}"
    if cancel_generation:
        return "cancel_transport_unconfirmed"
    return "response_close"


def _register_active_responses_stream(
    plan: "UpstreamRequestPlan | None",
) -> _ActiveResponsesStream | None:
    global _ACTIVE_RESPONSES_STREAM_SEQUENCE
    identity = _responses_active_stream_identity(plan)
    task = asyncio.current_task()
    if identity is None or task is None or not isinstance(plan, UpstreamRequestPlan):
        return None
    with _ACTIVE_RESPONSES_STREAMS_LOCK:
        _ACTIVE_RESPONSES_STREAM_SEQUENCE += 1
        entry = _ActiveResponsesStream(
            identity=identity,
            request_id=plan.request_id,
            sequence=_ACTIVE_RESPONSES_STREAM_SEQUENCE,
            plan=plan,
            task=task,
        )
        _ACTIVE_RESPONSES_STREAMS.setdefault(identity, {})[plan.request_id] = entry
    return entry


def _unregister_active_responses_stream(entry: _ActiveResponsesStream | None) -> None:
    if entry is None:
        return
    with _ACTIVE_RESPONSES_STREAMS_LOCK:
        streams = _ACTIVE_RESPONSES_STREAMS.get(entry.identity)
        if not streams or streams.get(entry.request_id) is not entry:
            return
        streams.pop(entry.request_id, None)
        if not streams:
            _ACTIVE_RESPONSES_STREAMS.pop(entry.identity, None)


def _complete_active_responses_teardown(
    entry: _ActiveResponsesStream | None,
    *,
    transport_cancel: str,
    confirmed: bool,
    completed: bool = False,
) -> None:
    if entry is None:
        return
    entry.transport_cancel = transport_cancel
    entry.completed_event_seen = completed
    entry.teardown_confirmed = confirmed
    entry.response_ready.set()
    entry.teardown_complete.set()
    # This registry coordinates streams that this process can still stop; it
    # must not become a permanent deny-list for a lineage.  In particular, a
    # pre-response transport error can leave us unable to prove what happened
    # upstream, but the owning route has already finished and there is no
    # remaining stream handle on which a later follow-up could improve that
    # outcome.  Keep ``teardown_confirmed`` for diagnostics while retiring all
    # completed entries so retries are not rejected forever.
    _unregister_active_responses_stream(entry)


def _responses_supersession_timeout_seconds() -> float:
    raw_value = os.environ.get("GHCP_PROXY_RESPONSES_SUPERSESSION_TIMEOUT_SECONDS")
    if raw_value is None:
        return 2.0
    try:
        return max(0.1, float(str(raw_value).strip()))
    except (TypeError, ValueError):
        return 2.0


def _cancel_active_responses_task(entry: _ActiveResponsesStream) -> None:
    if (
        not entry.task.done()
        and not entry.cancel_requested
        and not _task_is_cancelling(entry.task)
    ):
        entry.cancel_requested = True
        entry.task.cancel()


async def _wait_for_active_responses_event(
    event: asyncio.Event,
    timeout_seconds: float,
) -> bool:
    if event.is_set():
        return True
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _supersede_active_responses_streams(
    plan: "UpstreamRequestPlan | None",
    current_entry: _ActiveResponsesStream | None = None,
) -> list[dict]:
    """Stop active same-lineage generations before sending fresh steering."""
    if not _responses_plan_is_user_steering(plan):
        return []
    identity = _responses_active_stream_identity(plan)
    if identity is None or not isinstance(plan, UpstreamRequestPlan):
        return []
    current_task = asyncio.current_task()
    with _ACTIVE_RESPONSES_STREAMS_LOCK:
        prior_entries = [
            entry
            for entry in _ACTIVE_RESPONSES_STREAMS.get(identity, {}).values()
            if entry.task is not current_task
            and not (entry.teardown_complete.is_set() and entry.teardown_confirmed)
            and (
                current_entry is None
                or entry.sequence < current_entry.sequence
            )
        ]
        for entry in prior_entries:
            entry.superseded_by = plan.request_id

    timeout_seconds = _responses_supersession_timeout_seconds()

    # Requests that have not entered httpx are safe to cancel immediately.
    # A request awaiting response headers is different: cancellation at that
    # point provides no Response stream handle with which to send HTTP/2
    # RST_STREAM, so wait briefly for the handle instead of guessing that task
    # cancellation stopped server-side generation.
    for entry in prior_entries:
        if not entry.send_started:
            _cancel_active_responses_task(entry)

    pre_response_timeouts: set[str] = set()
    for entry in prior_entries:
        if (
            entry.send_started
            and entry.upstream is None
            and not entry.response_ready.is_set()
            and not await _wait_for_active_responses_event(
                entry.response_ready,
                timeout_seconds,
            )
        ):
            pre_response_timeouts.add(entry.request_id)

    # Once a response handle exists, issue wire cancellation *before* task
    # cancellation. Otherwise httpcore catches CancelledError first, drops its
    # local HTTP/2 stream object without RST_STREAM, and removes our only handle
    # for stopping server-side generation.
    for entry in prior_entries:
        if entry.upstream is not None:
            request_cancel = getattr(entry.stream_body, "request_transport_cancel", None)
            cancel_confirmed = False
            if callable(request_cancel):
                cancel_mode, cancel_confirmed = await request_cancel()
                entry.transport_cancel_attempt = cancel_mode
            if cancel_confirmed:
                _cancel_active_responses_task(entry)

    for entry in prior_entries:
        if (
            entry.task.done()
            and not entry.teardown_complete.is_set()
            and entry.stream_body is not None
        ):
            close_body = getattr(entry.stream_body, "aclose", None)
            if callable(close_body):
                await close_body()

    results: list[dict] = []
    for entry in prior_entries:
        teardown_waited = await _wait_for_active_responses_event(
            entry.teardown_complete,
            timeout_seconds,
        )
        blocked_reason = None
        if not teardown_waited:
            blocked_reason = (
                "response_handle_timeout"
                if entry.request_id in pre_response_timeouts
                else "teardown_timeout"
            )
        elif not entry.teardown_confirmed:
            blocked_reason = "transport_cancel_unconfirmed"
        results.append(
            {
                "request_id": entry.request_id,
                "send_started": entry.send_started,
                "response_ready": entry.response_ready.is_set(),
                "task_done": entry.task.done(),
                "completed_event_seen": entry.completed_event_seen,
                "transport_cancel_attempt": entry.transport_cancel_attempt,
                "transport_cancel": entry.transport_cancel,
                "teardown_complete": entry.teardown_complete.is_set(),
                "teardown_confirmed": entry.teardown_confirmed,
                "blocked_reason": blocked_reason,
            }
        )
    if results and isinstance(plan.trace_context, dict):
        plan.trace_context["superseded_active_responses"] = results
    if any(result.get("blocked_reason") for result in results):
        for entry in prior_entries:
            if (
                entry.request_id in pre_response_timeouts
                and not entry.cancel_requested
                and entry.superseded_by == plan.request_id
            ):
                entry.superseded_by = None
        raise _ResponsesSupersessionBlocked(results)
    return results


class _ManagedResponsesStreamBody:
    """Own a Responses stream lifecycle independently of lazy iteration.

    Starlette may observe a disconnect before it asks for the first body chunk.
    An async generator's ``finally`` block does not run when an unstarted
    generator is closed, so this concrete iterator owns teardown explicitly and
    makes ``aclose()`` effective before, during, and after iteration.
    """

    def __init__(
        self,
        *,
        upstream: httpx.Response,
        body: dict,
        headers: dict,
        usage_event: dict | None,
        stream_type: str,
        trace_plan: UpstreamRequestPlan | None,
        active_stream: _ActiveResponsesStream | None,
        stream_transform=None,
        trace_details_factory=None,

    ):
        self.upstream = upstream
        self.usage_event = usage_event
        self.stream_type = stream_type
        self.trace_plan = trace_plan
        self.active_stream = active_stream
        self.trace_details_factory = trace_details_factory
        self._stream_transform_enabled = callable(stream_transform)
        self.capture = usage_tracker.create_sse_capture(stream_type)
        self.source_loop_completed = False
        self.presentation_loop_completed = False
        self._source_task: asyncio.Task | None = None
        self._finalizing = False
        self._finalized = False
        self._finalized_event = asyncio.Event()
        self._preemptive_transport_cancel: str | None = None
        self._transport_cancel_attempt: str | None = None
        self._transport_cancel_task: asyncio.Task | None = None

        raw_source_iter = upstream.aiter_bytes()

        async def capture_source():
            async for chunk in raw_source_iter:
                if self.capture.feed(chunk):
                    usage_tracker.mark_first_output(self.usage_event)
                yield chunk
            self.source_loop_completed = True

        source_iter = capture_source()
        if self._stream_transform_enabled:
            source_iter = stream_transform(source_iter)
        self._source_iter = source_iter.__aiter__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._finalized:
            raise StopAsyncIteration
        # Keep task cancellation from reaching httpcore before we can emit
        # RST_STREAM. httpcore otherwise closes and discards its private stream
        # state while leaving the server-side generation alive.
        source_task = asyncio.create_task(self._source_iter.__anext__())
        self._source_task = source_task
        try:
            chunk = await asyncio.shield(source_task)
        except StopAsyncIteration:
            self.presentation_loop_completed = True
            await self._finalize("source_eof")
            raise
        except asyncio.CancelledError:
            if self.active_stream is not None:
                self.active_stream.cancel_requested = True
            with CancelScope(shield=True):
                await self.request_transport_cancel()
                if not source_task.done():
                    source_task.cancel()
                try:
                    await source_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._finalize("downstream_cancelled")
            raise
        except upstream_errors.ExcelResponseError as exc:
            # Headers are already sent. End with a Responses error event, not
            # a broken HTTP body that the client mistakes for a network retry.
            await self._finalize("response_validation_error", error=exc)
            return format_translation.sse_encode("response.failed", {
                "type": "response.failed",
                "response": {
                    "status": "failed", "output": [],
                    "error": {"code": exc.code, "message": str(exc), "type": "server_error"},
                },
            })
        except Exception as exc:
            await self._finalize("upstream_error", error=exc)
            raise
        finally:
            if self._source_task is source_task:
                self._source_task = None

        return chunk

    async def aclose(self) -> None:
        try:
            if not self._finalized and not self.capture.terminal_event_seen:
                await self.request_transport_cancel()

            # Wire cancellation must happen first. Then stop the presentation
            # adapter before reading its partial payload for tracing so it
            # cannot mutate translator state concurrently with finalization.
            source_task = self._source_task
            if source_task is not None and not source_task.done():
                source_task.cancel()
                with CancelScope(shield=True):
                    try:
                        await source_task
                    except (asyncio.CancelledError, Exception):
                        pass
            close_source = getattr(self._source_iter, "aclose", None)
            if callable(close_source):
                with CancelScope(shield=True):
                    try:
                        await close_source()
                    except (asyncio.CancelledError, Exception):
                        pass
        finally:
            await self._finalize("downstream_closed")

    async def request_transport_cancel(self) -> tuple[str, bool]:
        """Cancel the wire stream before the owning ASGI task is cancelled."""
        if self._preemptive_transport_cancel is not None:
            return self._preemptive_transport_cancel, True
        if self._transport_cancel_attempt is not None:
            return self._transport_cancel_attempt, False
        if self._transport_cancel_task is None:
            cancel_owner_on_success = (
                self.active_stream is not None
                and asyncio.current_task() is not self.active_stream.task
            )
            self._transport_cancel_task = asyncio.create_task(
                self._perform_transport_cancel(
                    cancel_owner_on_success=cancel_owner_on_success,
                )
            )
        done, _pending = await asyncio.wait(
            {self._transport_cancel_task},
            timeout=_responses_supersession_timeout_seconds(),
        )
        if not done:
            if self.active_stream is not None:
                self.active_stream.transport_cancel_attempt = "transport_cancel_timeout"
            return "transport_cancel_timeout", False
        return self._transport_cancel_task.result()

    async def _confirm_transport_cancel_after_finalize(self, mode: str) -> None:
        await self._finalized_event.wait()
        await _close_upstream_response(self.upstream)
        _complete_active_responses_teardown(
            self.active_stream,
            transport_cancel=mode,
            confirmed=True,
        )

    async def _perform_transport_cancel(
        self,
        *,
        cancel_owner_on_success: bool,
    ) -> tuple[str, bool]:
        http_version = self.upstream.extensions.get("http_version")
        if http_version in {b"HTTP/2", "HTTP/2"}:
            reset_sent, reset_status = await _reset_http2_upstream_stream(self.upstream)
            mode = "http2_rst_cancel" if reset_sent else f"http2_reset_{reset_status}"
        elif http_version in {b"HTTP/1.0", b"HTTP/1.1", "HTTP/1.0", "HTTP/1.1"}:
            mode = await _close_upstream_response(
                self.upstream,
                cancel_generation=True,
            )
            reset_sent = mode == "http1_connection_close"
        else:
            mode = "cancel_transport_unconfirmed"
            reset_sent = False

        self._transport_cancel_attempt = mode
        if self.active_stream is not None:
            self.active_stream.transport_cancel_attempt = mode
        if reset_sent:
            self._preemptive_transport_cancel = mode
            if cancel_owner_on_success and self.active_stream is not None:
                _cancel_active_responses_task(self.active_stream)
            if self._finalized:
                await _close_upstream_response(self.upstream)
                _complete_active_responses_teardown(
                    self.active_stream,
                    transport_cancel=mode,
                    confirmed=True,
                )
            elif self._finalizing:
                asyncio.create_task(
                    self._confirm_transport_cancel_after_finalize(mode)
                )
        return mode, reset_sent

    async def _finalize(self, cause: str, *, error: Exception | None = None) -> None:
        with CancelScope(shield=True):
            if self._finalized:
                return
            if self._finalizing:
                await self._finalized_event.wait()
                return
            self._finalizing = True

            completed = self.capture.completed_event_seen
            terminal_eof = (
                self.capture.terminal_event_seen
                and self.source_loop_completed
                and cause == "source_eof"
            )
            generation_ended = completed or self.capture.terminal_event_seen
            if (
                self._stream_transform_enabled
                and cause in {"downstream_cancelled", "downstream_closed"}
                and not self.presentation_loop_completed
            ):
                trace_status = 499
            elif cause in {"upstream_error", "response_validation_error"} and self._stream_transform_enabled:
                if isinstance(error, httpx.RequestError):
                    trace_status, _message = (
                        format_translation.upstream_request_error_status_and_message(error)
                    )
                else:
                    trace_status = 502
            elif completed:
                trace_status = self.upstream.status_code
            elif self.capture.terminal_event_type == "response.incomplete":
                # Max-output/content-filter termination is a valid HTTP 200
                # Responses outcome, so preserve its HTTP status.
                trace_status = self.upstream.status_code
            elif self.capture.terminal_event_seen:
                # response.failed or a bare [DONE] prove the generation ended,
                # but not successfully.
                trace_status = 502
            elif self.active_stream is not None and self.active_stream.superseded_by:
                trace_status = 499
            elif cause in {"downstream_cancelled", "downstream_closed"}:
                trace_status = 499
            elif isinstance(error, httpx.RequestError):
                trace_status, _message = format_translation.upstream_request_error_status_and_message(error)
            else:
                trace_status = 502

            try:
                if self._preemptive_transport_cancel is not None:
                    await _close_upstream_response(self.upstream)
                    transport_close = self._preemptive_transport_cancel
                elif (
                    self._transport_cancel_task is not None
                    and not self._transport_cancel_task.done()
                ):
                    # A single background owner is still attempting the wire
                    # cancel. Do not race it with a second Response.aclose().
                    transport_close = "transport_cancel_pending"
                elif self._transport_cancel_task is not None:
                    await _close_upstream_response(self.upstream)
                    transport_close = (
                        self._transport_cancel_attempt
                        or "cancel_transport_unconfirmed"
                    )
                else:
                    transport_close = await _close_upstream_response(
                        self.upstream,
                        cancel_generation=not generation_ended,
                    )
            except asyncio.CancelledError:
                # Repeated task cancellation can pierce library-level shields.
                # Lifecycle state still must be committed synchronously.
                transport_close = "transport_close_cancelled"

            transport_cancel_confirmed = transport_close in {
                "http2_rst_cancel",
                "http1_connection_close",
            }
            teardown_confirmed = generation_ended or transport_cancel_confirmed
            lifecycle = {
                "termination_cause": cause,
                "terminal_event_seen": self.capture.terminal_event_seen,
                "terminal_event_type": self.capture.terminal_event_type,
                "completed_event_seen": completed,
                "terminal_eof": terminal_eof,
                "generation_end_confirmed": generation_ended,
                "source_loop_completed": self.source_loop_completed,
                "presentation_loop_completed": self.presentation_loop_completed,
                "superseded_by": (
                    self.active_stream.superseded_by
                    if self.active_stream is not None
                    else None
                ),
                "transport_close": transport_close,
                "transport_cancel_confirmed": transport_cancel_confirmed,
                "teardown_confirmed": teardown_confirmed,
                "upstream_error_type": type(error).__name__ if error is not None else None,
                "upstream_error_code": error.code if isinstance(error, upstream_errors.ExcelResponseError) else None,
                "upstream_error_message": str(error) if isinstance(error, upstream_errors.ExcelResponseError) else None,
                "presentation_transform": self._stream_transform_enabled,
            }
            trace_details = {}
            if callable(self.trace_details_factory):
                try:
                    candidate = self.trace_details_factory()
                    if isinstance(candidate, dict):
                        trace_details = candidate
                except Exception as exc:
                    # Presentation adapters must never prevent the managed
                    # stream owner from recording lifecycle state and
                    # completing teardown.
                    lifecycle["trace_details_error_type"] = type(exc).__name__
            if isinstance(self.trace_plan, UpstreamRequestPlan) and isinstance(self.trace_plan.trace_context, dict):
                self.trace_plan.trace_context["responses_stream_lifecycle"] = lifecycle

            try:
                captured_usage = (
                    self.capture.usage
                    if isinstance(self.capture.usage, dict)
                    else None
                )
                trace_usage = trace_details.get("usage")
                if (
                    self._stream_transform_enabled
                    and cause
                    in {"upstream_error", "downstream_cancelled", "downstream_closed"}
                    and not self.presentation_loop_completed
                    and captured_usage is not None
                ):
                    trace_usage = captured_usage
                elif not isinstance(trace_usage, dict):
                    trace_usage = captured_usage
                _finish_usage_and_trace(
                    self.trace_plan,
                    trace_status,
                    upstream=self.upstream,
                    response_payload=(
                        trace_details.get("response_payload")
                        if isinstance(trace_details.get("response_payload"), dict)
                        else None
                    ),
                    response_text=(
                        trace_details.get("response_text")
                        if isinstance(trace_details.get("response_text"), str)
                        else None
                    ),
                    reasoning_text=(
                        trace_details.get("reasoning_text")
                        if isinstance(trace_details.get("reasoning_text"), str)
                        else None
                    ),
                    usage=trace_usage,
                )
            finally:
                _complete_active_responses_teardown(
                    self.active_stream,
                    transport_cancel=transport_close,
                    confirmed=teardown_confirmed,
                    completed=completed,
                )
                self._finalized = True
                self._finalizing = False
                self._finalized_event.set()


def _env_flag_default(name: str, *, default: bool) -> bool:
    """``_env_flag`` variant that defaults to True unless explicitly disabled.

    Accepts 0/false/no/off (case-insensitive) as opt-out when ``default`` is
    True. Any other value — including unset — keeps the default.
    """
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return default


def request_tracing_enabled() -> bool:
    # Default-on: request tracing is always a rolling window bounded by
    # REQUEST_TRACE_HISTORY_LIMIT, so it's cheap to leave on. Users can still
    # opt out with GHCP_TRACE_REQUESTS=0 (accepts 0/false/no/off).
    return _env_flag_default("GHCP_TRACE_REQUESTS", default=True)


def request_trace_log_path() -> str:
    configured = str(os.environ.get("GHCP_TRACE_LOG_FILE", "")).strip()
    return os.path.expanduser(configured or REQUEST_TRACE_LOG_FILE)


def request_body_dump_enabled() -> bool:
    # Body dumps are still gated by debug_prompt_logging_enabled; this flag only
    # controls whether approved full-detail captures are written.
    return _env_flag_default("GHCP_DUMP_REQUEST_BODIES", default=True)


def request_body_dump_dir() -> str:
    configured = str(os.environ.get("GHCP_REQUEST_BODY_DUMP_DIR", "")).strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.dirname(request_trace_log_path()), "request-bodies")


def restore_client_proxy_configs_on_startup() -> dict[str, object]:
    global _CLIENT_PROXY_STARTUP_RESTORE_COMPLETE
    with _CLIENT_PROXY_STARTUP_RESTORE_LOCK:
        if _CLIENT_PROXY_STARTUP_RESTORE_COMPLETE:
            return {
                "attempted": False,
                "restored": False,
                "reason": "already-ran",
                "clients": {},
            }
        _CLIENT_PROXY_STARTUP_RESTORE_COMPLETE = True

    try:
        result = client_proxy_config_service.restore_proxy_configs_on_startup()
    except Exception as exc:  # pragma: no cover - best effort
        print(f"client proxy startup restore failed: {exc}", flush=True)
        return {
            "attempted": True,
            "restored": False,
            "reason": "error",
            "error": str(exc),
            "clients": {},
        }

    if result.get("attempted"):
        print(f"Client proxy startup restore: {json.dumps(result, default=str)}", flush=True)
    return result


def revert_client_proxy_configs_on_shutdown() -> dict[str, object]:
    global _CLIENT_PROXY_SHUTDOWN_REVERT_COMPLETE
    with _CLIENT_PROXY_SHUTDOWN_REVERT_LOCK:
        if _CLIENT_PROXY_SHUTDOWN_REVERT_COMPLETE:
            return {
                "attempted": False,
                "reverted": False,
                "reason": "already-ran",
                "clients": {},
            }
        _CLIENT_PROXY_SHUTDOWN_REVERT_COMPLETE = True

    try:
        result = client_proxy_config_service.revert_proxy_configs_on_shutdown()
    except Exception as exc:  # pragma: no cover - best effort
        print(f"client proxy shutdown revert failed: {exc}", flush=True)
        return {
            "attempted": True,
            "reverted": False,
            "reason": "error",
            "error": str(exc),
            "clients": {},
        }

    if result.get("attempted"):
        print(f"Client proxy shutdown revert: {json.dumps(result, default=str)}", flush=True)
    return result


def _header_trace_subset(headers: dict | None) -> dict:
    if not isinstance(headers, dict):
        return {}
    subset = {}
    for key, value in headers.items():
        normalized_key = str(key).strip()
        if not normalized_key or normalized_key.lower() not in _TRACE_HEADER_ALLOWLIST:
            continue
        subset[normalized_key] = value
    return subset


def _sorted_counts(values: dict[str, int]) -> dict[str, int]:
    return {key: values[key] for key in sorted(values)}


def _count_trace_items(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(items, list):
        return counts
    for item in items:
        if isinstance(item, dict):
            item_type = str(item.get("type", "dict")).strip() or "dict"
        else:
            item_type = type(item).__name__
        counts[item_type] = counts.get(item_type, 0) + 1
    return _sorted_counts(counts)


def _count_trace_roles(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(items, list):
        return counts
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        if not role:
            continue
        counts[role] = counts.get(role, 0) + 1
    return _sorted_counts(counts)


def _trace_messages_summary(messages) -> dict:
    if isinstance(messages, str):
        return {"kind": "string", "chars": len(messages)}
    if not isinstance(messages, list):
        return {"kind": type(messages).__name__}

    part_counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    part_type = str(part.get("type", "dict")).strip() or "dict"
                else:
                    part_type = type(part).__name__
                part_counts[part_type] = part_counts.get(part_type, 0) + 1
        elif isinstance(content, str) and content:
            part_counts["text"] = part_counts.get("text", 0) + 1

    return {
        "kind": "list",
        "count": len(messages),
        "roles": _count_trace_roles(messages),
        "content_part_types": _sorted_counts(part_counts),
    }


def _trace_input_summary(input_value) -> dict:
    if isinstance(input_value, str):
        return {"kind": "string", "chars": len(input_value)}
    if not isinstance(input_value, list):
        return {"kind": type(input_value).__name__}

    encrypted_reasoning_items = 0
    for item in input_value:
        if isinstance(item, dict) and item.get("type") == "reasoning" and isinstance(item.get("encrypted_content"), str):
            encrypted_reasoning_items += 1

    return {
        "kind": "list",
        "count": len(input_value),
        "item_types": _count_trace_items(input_value),
        "roles": _count_trace_roles(input_value),
        "has_compaction": format_translation.input_contains_compaction(input_value),
        "encrypted_reasoning_items": encrypted_reasoning_items,
        "sequence": _trace_input_sequence(input_value),
    }


def _trace_hash(value) -> str | None:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()[:16]


def _trace_text_chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_trace_text_chars(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for key in ("text", "input_text", "output_text"):
            text = value.get(key)
            if isinstance(text, str):
                total += len(text)
        for key in ("content", "output"):
            nested = value.get(key)
            if isinstance(nested, (list, dict, str)):
                total += _trace_text_chars(nested)
        return total
    return 0


def _trace_input_sequence(input_value: list) -> list[dict]:
    sequence = []
    for index, item in enumerate(input_value):
        if not isinstance(item, dict):
            sequence.append({"index": index, "type": type(item).__name__, "item_hash": _trace_hash(item)})
            continue
        entry = {
            "index": index,
            "type": item.get("type"),
            "item_hash": _trace_hash(item),
        }
        for key in ("role", "name", "status"):
            value = item.get(key)
            if isinstance(value, str) and value:
                entry[key] = value
        for key in ("id", "call_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                entry[f"{key}_hash"] = _trace_hash(value)
        if "content" in item:
            entry["content_chars"] = _trace_text_chars(item.get("content"))
            entry["content_hash"] = _trace_hash(item.get("content"))
        if "output" in item:
            entry["output_chars"] = _trace_text_chars(item.get("output"))
            entry["output_hash"] = _trace_hash(item.get("output"))
        if "arguments" in item:
            entry["arguments_hash"] = _trace_hash(item.get("arguments"))
        encrypted = item.get("encrypted_content")
        if isinstance(encrypted, str) and encrypted:
            entry["encrypted_content_chars"] = len(encrypted)
            entry["encrypted_content_hash"] = _trace_hash(encrypted)
        sequence.append(entry)
    return sequence


def _trace_tools_deferred_count(tools) -> int:
    if isinstance(tools, list):
        return sum(_trace_tools_deferred_count(tool) for tool in tools)
    if not isinstance(tools, dict):
        return 0
    count = 1 if "defer_loading" in tools else 0
    nested = tools.get("tools")
    if isinstance(nested, (list, dict)):
        count += _trace_tools_deferred_count(nested)
    return count


def _request_reasoning_effort(body: dict | None) -> str | None:
    """Return the requested reasoning level from any supported request shape."""
    if not isinstance(body, dict):
        return None

    candidates = [body.get("reasoning_effort")]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        candidates.append(reasoning.get("effort"))
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        candidates.append(output_config.get("effort"))

    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized:
                return normalized
    return None


def _trace_body_summary(body: dict | None) -> dict | None:
    if not isinstance(body, dict):
        return None

    summary = {
        "keys": sorted(body.keys()),
        "model": body.get("model"),
        "stream": body.get("stream"),
    }

    reasoning_effort = _request_reasoning_effort(body)
    if reasoning_effort is not None:
        summary["reasoning_effort"] = reasoning_effort
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        snapshot: dict = {}
        t_type = thinking.get("type")
        if isinstance(t_type, str):
            snapshot["type"] = t_type
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int):
            snapshot["budget_tokens"] = budget
        if snapshot:
            summary["thinking"] = snapshot

    for source_key, target_key in (
        ("session_id", "session_id"),
        ("sessionId", "session_id"),
    ):
        value = body.get(source_key)
        if isinstance(value, str) and value.strip():
            summary[target_key] = value.strip()

    tools = body.get("tools")
    if isinstance(tools, list):
        summary["tool_count"] = len(tools)
        deferred_tool_count = _trace_tools_deferred_count(tools)
        if deferred_tool_count:
            summary["deferred_tool_count"] = deferred_tool_count
        if format_translation.responses_tools_have_tool_search(tools):
            summary["tool_search_present"] = True
    elif isinstance(tools, dict):
        deferred_tool_count = _trace_tools_deferred_count(tools)
        if deferred_tool_count:
            summary["deferred_tool_count"] = deferred_tool_count
        if format_translation.responses_tools_have_tool_search(tools):
            summary["tool_search_present"] = True

    if "input" in body:
        summary["input"] = _trace_input_summary(body.get("input"))
    if "messages" in body:
        summary["messages"] = _trace_messages_summary(body.get("messages"))
    body_fingerprint = _trace_hash(body)
    if body_fingerprint:
        summary["body_fingerprint"] = body_fingerprint

    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        summary["metadata_keys"] = sorted(metadata.keys())
    for key in sorted(body.keys()):
        if key in ("input", "messages"):
            continue
        fingerprint = _trace_hash(body.get(key))
        if fingerprint:
            summary[f"{key}_fingerprint"] = fingerprint

    return summary


def _debug_detail_normalized_string(value) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _debug_detail_body_session_id(body: dict | None) -> str | None:
    return usage_tracking.request_body_session_id(body)


def _debug_detail_header_value(headers: dict | None, header_name: str) -> str | None:
    return _debug_detail_normalized_string(_header_value_case_insensitive(headers, header_name))


def _debug_detail_session_key(
    *,
    request: Request | None = None,
    request_body: dict | None = None,
    upstream_body: dict | None = None,
    resolved_model: str | None = None,
    outbound_headers: dict | None = None,
) -> tuple[str, str] | None:
    """Resolve the session bucket used for debug prompt logging."""

    if request is not None:
        session_id = usage_tracking.request_session_id(
            request,
            request_body if isinstance(request_body, dict) else upstream_body,
        )
        if session_id:
            return f"session:{session_id}", "request_session_id"

    for body in (request_body, upstream_body):
        session_id = _debug_detail_body_session_id(body)
        if session_id:
            return f"session:{session_id}", "body_session_id"

    for header_name, source in (
        ("session_id", "header_session_id"),
        ("session-id", "header_session_id"),
        ("x-claude-code-session-id", "header_session_id"),
        ("x-session-affinity", "header_session_id"),
        ("x-opencode-session", "header_session_id"),
        ("x-client-request-id", "header_client_request_id"),
        ("x-client-session-id", "header_client_session_id"),
        ("x-interaction-id", "header_interaction_id"),
        ("x-parent-agent-id", "header_parent_agent_id"),
        ("x-agent-task-id", "header_agent_task_id"),
    ):
        value = _debug_detail_header_value(outbound_headers, header_name)
        if value:
            return f"{source}:{value}", source

    return None


def _header_value_case_insensitive(headers: dict | None, name: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    target = str(name).lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _trace_metadata_verdict(trace_metadata: dict | None) -> dict:
    if not isinstance(trace_metadata, dict):
        return {}
    verdict = trace_metadata.get("initiator_verdict")
    return dict(verdict) if isinstance(verdict, dict) else {}


def _debug_detail_always_capture_reasons(
    outbound_headers: dict | None,
    trace_metadata: dict | None,
) -> list[str]:
    if not _prompt_logging_permitted():
        return []
    reasons: list[str] = []
    initiator = str(_header_value_case_insensitive(outbound_headers, "x-initiator") or "").strip().lower()
    verdict = _trace_metadata_verdict(trace_metadata)
    resolved_initiator = str(verdict.get("resolved_initiator") or "").strip().lower()
    if initiator == "user" or resolved_initiator == "user":
        reasons.append("user_initiated")
    return reasons


def _debug_detail_capture_info(
    *,
    reasons: list[str],
    phase: str | None = None,
    incident_id: str | None = None,
    context_window: int = DEBUG_DETAIL_CONTEXT_REQUESTS,
) -> dict:
    info = {
        "enabled": True,
        "reasons": list(dict.fromkeys(reason for reason in reasons if reason)),
        "context_window": context_window,
    }
    if phase:
        info["phase"] = phase
    if incident_id:
        info["incident_id"] = incident_id
    return info


def _outbound_json_wire_bytes(body: dict | None) -> bytes | None:
    if body is None:
        return None
    try:
        return json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            default=util._json_default,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _build_debug_detail_snapshot(
    *,
    request_id: str,
    context: dict,
    request: Request,
    requested_model: str | None,
    resolved_model: str | None,
    request_body: dict | None,
    upstream_body: dict | None,
    outbound_headers: dict | None,
) -> dict:
    full_prompt_preview = _extract_prompt_preview(
        request_body if isinstance(request_body, dict) else upstream_body,
        truncate=False,
    )
    session_key_pair = _debug_detail_session_key(
        request=request,
        request_body=request_body,
        upstream_body=upstream_body,
        resolved_model=resolved_model,
        outbound_headers=outbound_headers,
    )
    snapshot = {
        "event": "request_debug_detail",
        "time": util.utc_now_iso(),
        "request_id": request_id,
        "client_path": context.get("client_path") or getattr(getattr(request, "url", None), "path", None),
        "upstream_host": context.get("upstream_host"),
        "upstream_path": context.get("upstream_path"),
        "method": getattr(request, "method", None),
        "requested_model": requested_model,
        "resolved_model": resolved_model,
        "request_body_summary": _trace_body_summary(request_body),
        "upstream_body_summary": _trace_body_summary(upstream_body),
        "outbound_headers": _header_trace_subset(outbound_headers),
    }
    if session_key_pair is not None:
        snapshot["_session_key"], snapshot["_session_key_source"] = session_key_pair
    if full_prompt_preview:
        snapshot["request_prompt"] = _prompt_trace_value(full_prompt_preview)
    if isinstance(request_body, dict):
        snapshot["source_body"] = _prompt_trace_value(request_body)
    if isinstance(upstream_body, dict):
        snapshot["upstream_body"] = _prompt_trace_value(upstream_body)
        upstream_wire_bytes = _outbound_json_wire_bytes(upstream_body)
        if upstream_wire_bytes is not None:
            snapshot["upstream_body_wire"] = _prompt_trace_value(
                upstream_wire_bytes.decode("utf-8", errors="replace")
            )
            snapshot["upstream_body_wire_size"] = len(upstream_wire_bytes)
            snapshot["upstream_body_wire_sha256"] = hashlib.sha256(upstream_wire_bytes).hexdigest()
    return snapshot


def _register_debug_detail_snapshot(snapshot: dict) -> tuple[dict | None, list[dict]]:
    """Persist full prompt/body detail for every request when debug prompt logging is enabled."""
    if not _debug_prompt_logging_enabled():
        return None, []
    return _debug_detail_capture_info(reasons=["debug_prompt_logging"], phase="current"), []


def _trace_context_allows_full_debug_detail(trace_context: dict | None) -> bool:
    if not isinstance(trace_context, dict):
        return False
    capture = trace_context.get("debug_detail_capture")
    return isinstance(capture, dict) and capture.get("enabled") is True


def _plan_allows_full_debug_detail(plan: "UpstreamRequestPlan | None") -> bool:
    if not isinstance(plan, UpstreamRequestPlan):
        return False
    if not _prompt_logging_permitted():
        return False
    if _trace_context_allows_full_debug_detail(plan.trace_context):
        return True
    if "user_initiated" in _debug_detail_always_capture_reasons(plan.headers, plan.trace_context):
        return True
    return False


def _effective_trace_usage(response_payload: dict | None = None, usage: dict | None = None) -> dict | None:
    normalized_usage = util.normalize_usage_payload(usage)
    if isinstance(normalized_usage, dict):
        return normalized_usage
    if isinstance(response_payload, dict):
        normalized_usage = util.normalize_usage_payload(response_payload.get("usage"))
        if isinstance(normalized_usage, dict):
            return normalized_usage
    return None


def _trace_response_summary(
    upstream: httpx.Response | None = None,
    response_payload: dict | None = None,
    usage: dict | None = None,
    status_code: int | None = None,
) -> dict:
    summary: dict = {}
    if status_code is not None:
        summary["status_code"] = status_code
    if upstream is not None:
        if status_code is None:
            summary["status_code"] = upstream.status_code
        elif upstream.status_code != status_code:
            summary["upstream_status_code"] = upstream.status_code
        content_type = upstream.headers.get("content-type")
        if content_type:
            summary["content_type"] = content_type
        for header_name in ("x-request-id", "request-id"):
            header_value = upstream.headers.get(header_name)
            if header_value:
                summary["upstream_request_id"] = header_value
                break
    if isinstance(response_payload, dict):
        for key in ("id", "object", "model"):
            value = response_payload.get(key)
            if isinstance(value, str) and value:
                summary[key] = value
        output = response_payload.get("output")
        if isinstance(output, list):
            summary["output_item_types"] = _count_trace_items(output)

    normalized_usage = _effective_trace_usage(response_payload=response_payload, usage=usage)
    if isinstance(normalized_usage, dict):
        summary["usage"] = normalized_usage

    error_payload = None
    if isinstance(response_payload, dict):
        maybe_error = response_payload.get("error")
        if isinstance(maybe_error, dict):
            error_payload = maybe_error
    if isinstance(error_payload, dict):
        error_summary = {}
        for key in ("type", "code", "param"):
            value = error_payload.get(key)
            if value is not None:
                error_summary[key] = value
        if error_summary:
            summary["error"] = error_summary

    return summary


def _append_request_trace(payload: dict, *, force: bool = False) -> None:
    if not force and not (request_tracing_enabled() or _debug_prompt_logging_enabled()):
        return
    trace_path = request_trace_log_path()
    try:
        line = json.dumps(payload, separators=(",", ":"), default=util._json_default) + "\n"
        executor = _get_request_trace_executor()
        executor.submit(_write_request_trace_line, trace_path, line)
    except Exception as exc:
        print(f"Warning: failed to schedule request trace log write: {exc}", file=sys.stderr, flush=True)


def _write_request_trace_line(trace_path: str, line: str) -> None:
    try:
        log_dir = os.path.dirname(trace_path) or TOKEN_DIR
        os.makedirs(log_dir, exist_ok=True)
        with _REQUEST_TRACE_LOCK:
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(line)
            _enforce_trace_retention_locked(trace_path)
    except OSError as exc:
        print(f"Warning: failed to write request trace log: {exc}", file=sys.stderr, flush=True)


def _trim_trace_field(value, *, max_bytes: int = REQUEST_TRACE_BODY_MAX_BYTES):
    """Cap body-ish trace fields so retained rows stay bounded in size."""
    if value is None or max_bytes <= 0:
        return value
    try:
        serialized = json.dumps(value, separators=(",", ":"), default=util._json_default)
    except (TypeError, ValueError):
        return value
    encoded = serialized.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    return {
        "_truncated": True,
        "original_bytes": len(encoded),
        "preview": encoded[:max_bytes].decode("utf-8", errors="replace"),
        "original_type": type(value).__name__,
    }


def _trim_trace_text(value, *, max_chars: int = REQUEST_TRACE_BODY_MAX_BYTES):
    if not isinstance(value, str) or max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n...[truncated; original {len(value)} chars]"


def _enforce_body_dump_retention_locked(dump_dir: str) -> None:
    """Cap body-dump directory at REQUEST_TRACE_HISTORY_LIMIT files."""
    limit = REQUEST_TRACE_HISTORY_LIMIT
    if limit <= 0:
        return
    try:
        entries = os.listdir(dump_dir)
    except OSError:
        return
    if len(entries) <= limit + max(REQUEST_TRACE_RETENTION_SLACK, 0):
        return
    paths = []
    for name in entries:
        full = os.path.join(dump_dir, name)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        paths.append((mtime, full))
    paths.sort()
    for _, path in paths[: max(0, len(paths) - limit)]:
        try:
            os.unlink(path)
        except OSError:
            pass


def _enforce_trace_retention_locked(trace_path: str) -> None:
    """Keep the trace log bounded at REQUEST_TRACE_HISTORY_LIMIT rows."""
    limit = REQUEST_TRACE_HISTORY_LIMIT
    if limit <= 0:
        return
    threshold = limit + max(REQUEST_TRACE_RETENTION_SLACK, 0)
    try:
        size = os.path.getsize(trace_path)
    except OSError:
        return
    if size < threshold * 256:
        return
    try:
        with open(trace_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= threshold:
        return
    try:
        with open(trace_path, "w", encoding="utf-8") as f:
            f.writelines(lines[-limit:])
    except OSError as exc:
        print(f"Warning: trace retention rewrite failed: {exc}", file=sys.stderr, flush=True)


_REQUEST_BODY_DUMP_LOCK = threading.Lock()
_REQUEST_BODY_DUMP_EXECUTOR: "concurrent.futures.ThreadPoolExecutor | None" = None
_REQUEST_BODY_DUMP_EXECUTOR_LOCK = threading.Lock()
_REQUEST_TRACE_EXECUTOR: "concurrent.futures.ThreadPoolExecutor | None" = None
_REQUEST_TRACE_EXECUTOR_LOCK = threading.Lock()


def _get_request_trace_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _REQUEST_TRACE_EXECUTOR
    if _REQUEST_TRACE_EXECUTOR is not None:
        return _REQUEST_TRACE_EXECUTOR
    with _REQUEST_TRACE_EXECUTOR_LOCK:
        if _REQUEST_TRACE_EXECUTOR is None:
            import concurrent.futures
            _REQUEST_TRACE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ghcp-trace"
            )
    return _REQUEST_TRACE_EXECUTOR


def _get_request_body_dump_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _REQUEST_BODY_DUMP_EXECUTOR
    if _REQUEST_BODY_DUMP_EXECUTOR is not None:
        return _REQUEST_BODY_DUMP_EXECUTOR
    with _REQUEST_BODY_DUMP_EXECUTOR_LOCK:
        if _REQUEST_BODY_DUMP_EXECUTOR is None:
            import concurrent.futures
            _REQUEST_BODY_DUMP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="ghcp-body-dump"
            )
    return _REQUEST_BODY_DUMP_EXECUTOR


def _dump_outbound_request_body(
    *,
    request_id: str,
    context: dict,
    request: Request,
    requested_model: str | None,
    resolved_model: str | None,
    request_body: dict | None,
    upstream_body: dict | None,
    outbound_headers: dict | None,
) -> None:
    """Persist the exact outbound body and full headers for an approved capture."""
    if not request_body_dump_enabled():
        return
    try:
        dump_dir = request_body_dump_dir()
        # Build a snapshot on the caller's thread so we don't race the request
        # handler mutating the body after we hand off; serialization happens
        # in the background thread.
        upstream_wire_bytes = _outbound_json_wire_bytes(upstream_body)
        snapshot = {
            "request_id": request_id,
            "time": util.utc_now_iso(),
            "method": request.method,
            "client_path": context.get("client_path"),
            "upstream_host": context.get("upstream_host"),
            "upstream_path": context.get("upstream_path"),
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "outbound_headers": dict(outbound_headers) if isinstance(outbound_headers, dict) else None,
            "request_body": _prompt_trace_value(request_body),
            "upstream_body": _prompt_trace_value(upstream_body),
        }
        if upstream_wire_bytes is not None:
            snapshot["upstream_body_wire"] = _prompt_trace_value(
                upstream_wire_bytes.decode("utf-8", errors="replace")
            )
            snapshot["upstream_body_wire_size"] = len(upstream_wire_bytes)
            snapshot["upstream_body_wire_sha256"] = hashlib.sha256(upstream_wire_bytes).hexdigest()
        safe_rid = "".join(ch for ch in str(request_id) if ch.isalnum() or ch in ("-", "_")) or "request"
        out_path = os.path.join(dump_dir, f"{safe_rid}.json")
        executor = _get_request_body_dump_executor()
        executor.submit(_write_request_body_dump, out_path, dump_dir, snapshot)
    except Exception as exc:  # pragma: no cover - never let dump errors fail upstream
        print(f"Warning: failed to schedule request body dump: {exc}", file=sys.stderr, flush=True)


def _write_request_body_dump(out_path: str, dump_dir: str, snapshot: dict) -> None:
    """Background worker: serialize the snapshot and persist it.

    Runs on the body-dump executor so the event loop is never blocked by
    disk I/O. Catches every exception so a malformed payload cannot leak
    out of the worker.
    """
    try:
        with _REQUEST_BODY_DUMP_LOCK:
            os.makedirs(dump_dir, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, default=util._json_default)
            _enforce_body_dump_retention_locked(dump_dir)
    except Exception as exc:  # pragma: no cover - dump must never raise
        print(f"Warning: failed to write request body dump: {exc}", file=sys.stderr, flush=True)


def _protect_plan_prompt_trace_state(plan: UpstreamRequestPlan) -> None:
    return None


def _extract_prompt_preview(
    body: dict | None,
    *,
    truncate: bool = True,
    max_chars: int = REQUEST_PROMPT_PREVIEW_MAX_CHARS,
) -> dict | None:
    """Pull a human-readable prompt preview out of a request body.

    Returns a dict with ``system`` (concatenated system/developer prompts),
    ``user`` (most recent user turn text) and ``truncated`` flags. ``None``
    is returned when the body carries no recognizable prompt material.
    Accepts current Responses input and legacy archived message shapes.
    """
    if not isinstance(body, dict) or max_chars <= 0:
        return None

    system_parts: list[str] = []
    user_parts: list[str] = []

    raw_system = body.get("system")
    if isinstance(raw_system, str) and raw_system.strip():
        system_parts.append(raw_system)
    elif isinstance(raw_system, list):
        for entry in raw_system:
            text = util.extract_item_text(entry) if isinstance(entry, dict) else ""
            if not text and isinstance(entry, dict) and isinstance(entry.get("text"), str):
                text = entry["text"]
            if isinstance(text, str) and text.strip():
                system_parts.append(text)

    def _collect(items):
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("type") or "").strip().lower()
            text = util.extract_item_text(item)
            if not isinstance(text, str) or not text.strip():
                continue
            if role in ("system", "developer"):
                system_parts.append(text)
            elif role in ("user", "human", "message", ""):
                user_parts.append(text)

    _collect(body.get("messages"))
    input_value = body.get("input")
    if isinstance(input_value, str) and input_value.strip():
        user_parts.append(input_value)
    else:
        _collect(input_value)

    if not system_parts and not user_parts:
        return None

    def _finalize(parts: list[str]) -> tuple[str, bool]:
        combined = "\n\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        if not combined:
            return "", False
        # Keep the most recent context for user prompts (tail) and the
        # leading context for system prompts (head) since the head carries
        # the instructions.
        if not truncate or max_chars <= 0 or len(combined) <= max_chars:
            return combined, False
        return combined[:max_chars] + f"\n…[truncated; original {len(combined)} chars]", True

    system_text, system_truncated = _finalize(system_parts)
    # For user prompts, prefer the latest turn when truncating.
    user_combined = "\n\n".join(part.strip() for part in user_parts if isinstance(part, str) and part.strip())
    user_truncated = False
    if truncate and max_chars > 0 and len(user_combined) > max_chars:
        user_combined = "…[truncated; original " + str(len(user_combined)) + " chars]\n" + user_combined[-max_chars:]
        user_truncated = True

    preview: dict = {}
    if system_text:
        preview["system"] = system_text
        if system_truncated:
            preview["system_truncated"] = True
    if user_combined:
        preview["user"] = user_combined
        if user_truncated:
            preview["user_truncated"] = True
    return preview or None


def _emit_request_trace_start(
    *,
    request_id: str,
    request: Request,
    upstream_url: str,
    upstream_path: str | None,
    requested_model: str | None,
    resolved_model: str | None,
    request_body: dict | None,
    upstream_body: dict | None,
    outbound_headers: dict | None,
    trace_metadata: dict | None = None,
    prompt_preview: dict | None = None,
) -> dict:
    parsed_upstream = urlsplit(upstream_url)
    context = {
        "request_id": request_id,
        "client_path": request.url.path,
        "upstream_host": parsed_upstream.netloc,
        "upstream_path": upstream_path or parsed_upstream.path,
    }
    # Snapshot the initiator verdict at emit time — the caller may still be
    # mutating the shared sink (it's populated during header_builder and again
    # on subsequent requests using the same dict reference in tests).
    initiator_verdict = None
    if isinstance(trace_metadata, dict):
        raw_verdict = trace_metadata.get("initiator_verdict")
        if isinstance(raw_verdict, dict):
            initiator_verdict = dict(raw_verdict)
    trace_details = dict(trace_metadata) if isinstance(trace_metadata, dict) else {}
    debug_detail_snapshot = _build_debug_detail_snapshot(
        request_id=request_id,
        context=context,
        request=request,
        requested_model=requested_model,
        resolved_model=resolved_model,
        request_body=request_body,
        upstream_body=upstream_body,
        outbound_headers=outbound_headers,
    )
    debug_detail_session_key = _debug_detail_normalized_string(debug_detail_snapshot.get("_session_key"))
    debug_detail_capture, debug_detail_events = _register_debug_detail_snapshot(debug_detail_snapshot)
    if debug_detail_capture is not None:
        context["debug_detail_capture"] = debug_detail_capture
        if "request_prompt" in debug_detail_snapshot:
            context["request_prompt"] = debug_detail_snapshot["request_prompt"]
    upstream_summary = _trace_body_summary(upstream_body)
    payload = {
        "event": "request_started",
        "time": util.utc_now_iso(),
        **context,
        "method": request.method,
        "requested_model": requested_model,
        "resolved_model": resolved_model,
        "request_body": _trace_body_summary(request_body),
        "upstream_body": upstream_summary,
        # Debug previews below are bounded, but must not replace the only
        # complete upstream item fingerprints needed for prefix comparison.
        "upstream_body_summary": upstream_summary,
        "outbound_headers": _header_trace_subset(outbound_headers),
        "trace": trace_details,
    }
    if debug_detail_capture is not None and prompt_preview is None:
        prompt_preview = _extract_prompt_preview(
            request_body if isinstance(request_body, dict) else upstream_body,
            truncate=False,
        )
    if debug_detail_capture is not None and prompt_preview:
        protected_prompt_preview = _prompt_trace_value(prompt_preview)
        payload["request_prompt"] = protected_prompt_preview
        context["request_prompt"] = protected_prompt_preview
    if debug_detail_capture is not None:
        if isinstance(request_body, dict):
            payload["source_body"] = _prompt_trace_value(_trim_trace_field(request_body))
        if isinstance(upstream_body, dict):
            payload["upstream_body"] = _prompt_trace_value(_trim_trace_field(upstream_body))
    if initiator_verdict is not None:
        payload["initiator_verdict"] = initiator_verdict
        context["initiator_verdict"] = initiator_verdict
    _append_request_trace(payload)
    for debug_detail_event in debug_detail_events:
        _append_request_trace(debug_detail_event)
    if debug_detail_capture is not None:
        _dump_outbound_request_body(
            request_id=request_id,
            context=context,
            request=request,
            requested_model=requested_model,
            resolved_model=resolved_model,
            request_body=request_body,
            upstream_body=upstream_body,
            outbound_headers=outbound_headers,
        )
    if debug_detail_session_key:
        context["_debug_detail_session_key"] = debug_detail_session_key
    return context


def _should_force_failure_trace(plan: UpstreamRequestPlan | None, status_code: int) -> bool:
    if not isinstance(plan, UpstreamRequestPlan) or status_code < 400:
        return False
    trace = plan.trace_context if isinstance(plan.trace_context, dict) else {}
    return trace.get("bridge") is True


def _finish_usage_and_trace(
    plan: UpstreamRequestPlan | None,
    status_code: int,
    *,
    upstream: httpx.Response | None = None,
    response_payload: dict | None = None,
    response_text: str | None = None,
    reasoning_text: str | None = None,
    usage: dict | None = None,
) -> None:
    if isinstance(plan, UpstreamRequestPlan):
        _protect_plan_prompt_trace_state(plan)
        usage_tracker.finish_event(
            plan.usage_event,
            status_code,
            upstream=upstream,
            response_payload=response_payload,
            response_text=response_text,
            reasoning_text=reasoning_text,
            usage=usage,
        )
        effective_usage = _effective_trace_usage(response_payload=response_payload, usage=usage)
        force_trace = _should_force_failure_trace(plan, status_code)
        if request_tracing_enabled() or _debug_prompt_logging_enabled() or force_trace:
            trace_context = dict(plan.trace_context or {"request_id": plan.request_id})
            trace_context.pop("_debug_detail_session_key", None)
            error_details = None
            if status_code >= 400 and _plan_allows_full_debug_detail(plan):
                error_details = upstream_errors.sanitized_error_details(
                    response_payload,
                    secrets=tuple(value for key, value in plan.headers.items()
                                  if key.lower() in {"authorization", "cookie", "x-api-key"}),
                )
            trace_payload = {
                "event": "request_finished",
                "time": util.utc_now_iso(),
                **trace_context,
                "requested_model": plan.requested_model,
                "resolved_model": plan.resolved_model,
                "response": _trace_response_summary(
                    upstream=upstream,
                    response_payload=error_details if status_code >= 400 else response_payload,
                    usage=effective_usage,
                    status_code=status_code,
                ),
                "response_text_present": isinstance(response_text, str) and bool(response_text),
                "reasoning_text_present": isinstance(reasoning_text, str) and bool(reasoning_text),
            }
            if status_code < 400 and isinstance(reasoning_text, str) and reasoning_text:
                trace_payload["reasoning_text"] = _trim_trace_text(reasoning_text)
            if status_code >= 400:
                if _plan_allows_full_debug_detail(plan):
                    trace_payload["source_body"] = _prompt_trace_value(
                        _trim_trace_field(plan.source_body if isinstance(plan.source_body, dict) else plan.body)
                    )
                    trace_payload["upstream_body"] = _prompt_trace_value(_trim_trace_field(plan.body))
                else:
                    trace_payload["source_body"] = _trace_body_summary(
                        plan.source_body if isinstance(plan.source_body, dict) else plan.body
                    )
                    trace_payload["upstream_body"] = _trace_body_summary(plan.body)
                trace_payload["outbound_headers"] = _header_trace_subset(plan.headers)
                if error_details is not None:
                    trace_payload["response_payload"] = _trim_trace_field(error_details)
            _append_request_trace(trace_payload, force=force_trace)
        return

    usage_tracker.finish_event(
        None,
        status_code,
        upstream=upstream,
        response_payload=response_payload,
        response_text=response_text,
        reasoning_text=reasoning_text,
        usage=usage,
    )


def _prepare_upstream_request(
    request: Request,
    *,
    body: dict,
    requested_model: str | None,
    resolved_model: str | None,
    upstream_path: str,
    upstream_url: str,
    header_builder,
    error_response,
    api_key: str | None = None,
    source_body: dict | None = None,
    trace_metadata: dict | None = None,
    replay_subagent: str | None = None,
    force_initiator: str | None = None,
) -> tuple[UpstreamRequestPlan | None, Response | None]:
    request_id = uuid4().hex

    effective_api_key = api_key

    def header_value(name: str):
        if not isinstance(headers, dict):
            return None
        value = headers.get(name)
        if value is not None:
            return value
        target = name.lower()
        for key, candidate in headers.items():
            if isinstance(key, str) and key.lower() == target:
                return candidate
        return None

    headers = header_builder(effective_api_key, request_id)
    initiator_header = header_value("X-Initiator")
    # Callers that already know the initiator (e.g. compaction turns, which
    # carry no X-Initiator header) can override what the client sent.
    if isinstance(force_initiator, str) and force_initiator.strip():
        initiator_header = force_initiator.strip()
    initiator = str(initiator_header or "").strip().lower()
    initiator_verdict = None
    if isinstance(trace_metadata, dict):
        initiator_verdict = trace_metadata.get("initiator_verdict")
    always_capture_reasons = _debug_detail_always_capture_reasons(headers, trace_metadata)
    prompt_preview = None
    stored_prompt_preview = None
    if always_capture_reasons:
        prompt_preview = _extract_prompt_preview(
            source_body if isinstance(source_body, dict) else body,
            truncate=False,
        )
        stored_prompt_preview = (
            _prompt_trace_value(prompt_preview)
            if prompt_preview
            else None
        )
    usage_event = usage_tracker.start_event(
        request,
        requested_model,
        resolved_model,
        initiator_header,
        request_id=request_id,
        request_body=body,
        upstream_path=upstream_path,
        outbound_headers=headers,
        prompt_preview=stored_prompt_preview,
        initiator_verdict=initiator_verdict if isinstance(initiator_verdict, dict) else None,
    )
    if isinstance(trace_metadata, dict):
        for key in ("approval_agent", "subagent"):
            value = trace_metadata.get(key)
            if value is not None:
                usage_event[key] = value
    reasoning_effort = _request_reasoning_effort(body)
    if reasoning_effort is None:
        reasoning_effort = _request_reasoning_effort(source_body)
    if isinstance(reasoning_effort, str) and reasoning_effort:
        usage_event["reasoning_effort"] = reasoning_effort
    _save_request_prompt_record(
        request_id,
        request.url.path,
        source_body if isinstance(source_body, dict) else body,
    )
    trace_context = {
        "request_id": request_id,
        "client_path": request.url.path,
        "upstream_path": upstream_path,
        **(trace_metadata or {}),
    }
    if initiator_verdict is not None:
        trace_context["initiator_verdict"] = initiator_verdict
    debug_detail_session_key = None
    if request_tracing_enabled() or _debug_prompt_logging_enabled():
        trace_context = _emit_request_trace_start(
            request_id=request_id,
            request=request,
            upstream_url=upstream_url,
            upstream_path=upstream_path,
            requested_model=requested_model,
            resolved_model=resolved_model,
            request_body=source_body if isinstance(source_body, dict) else body,
            upstream_body=body,
            outbound_headers=headers,
            trace_metadata=trace_metadata,
            prompt_preview=stored_prompt_preview,
        )
        if isinstance(trace_context, dict):
            debug_detail_session_key = _debug_detail_normalized_string(
                trace_context.pop("_debug_detail_session_key", None)
            )
        if (
            isinstance(usage_event, dict)
            and "request_prompt" not in usage_event
            and isinstance(trace_context, dict)
            and "request_prompt" in trace_context
        ):
            usage_event["request_prompt"] = trace_context["request_prompt"]
    return (
        UpstreamRequestPlan(
            request_id=request_id,
            upstream_url=upstream_url,
            headers=headers,
            body=body,
            usage_event=usage_event,
            requested_model=requested_model,
            resolved_model=resolved_model,
            source_body=source_body if isinstance(source_body, dict) else body,
            request_affinity=usage_tracking.request_session_id(
                request,
                source_body if isinstance(source_body, dict) else body,
            ),
            replay_subagent=(
                replay_subagent.strip()
                if isinstance(replay_subagent, str) and replay_subagent.strip()
                else None
            ),
            trace_context=trace_context,
            debug_detail_session_key=debug_detail_session_key,
        ),
        None,
    )


def proxy_non_streaming_response(upstream: httpx.Response) -> Response:
    """
    Preserve the upstream status code and body shape.

    Most endpoints return JSON, but compaction can return non-JSON payloads
    such as SSE-style frames. When JSON parsing fails, fall back to relaying
    the raw body with the upstream content type instead of crashing.
    """
    headers = {}
    for name in ("content-type", "cache-control", "retry-after"):
        value = upstream.headers.get(name)
        if value:
            headers[name] = value

    content_type = upstream.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            return JSONResponse(
                content=upstream.json(),
                status_code=upstream.status_code,
                headers=headers,
            )
        except json.JSONDecodeError:
            pass

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )


def _handle_upstream_error(
    upstream: httpx.Response,
    *,
    trace_plan: UpstreamRequestPlan | None,
) -> Response:
    payload = _extract_upstream_json_payload(upstream)
    _finish_usage_and_trace(trace_plan, upstream.status_code, upstream=upstream, response_payload=payload)
    headers = {name: upstream.headers[name] for name in ("x-request-id", "retry-after") if name in upstream.headers}
    return JSONResponse(
        content=upstream_errors.excel_error_payload(upstream.status_code, payload),
        status_code=upstream.status_code, headers=headers,
    )


async def proxy_streaming_response(
    upstream_url: str,
    headers: dict,
    body: dict,
    timeout: int = 300,
    usage_event: dict | None = None,
    stream_type: str = "responses",
    trace_plan: UpstreamRequestPlan | None = None,
    downstream_request: Request | None = None,
    stream_transform=None,
    trace_details_factory=None,
    upstream_client: httpx.AsyncClient | None = None,
) -> Response:
    """
    Relay an upstream SSE response while preserving upstream error statuses.

    If the upstream request fails before the stream starts, return the upstream
    error body as a normal HTTP response instead of masking it as 200 SSE.
    """
    active_stream = _register_active_responses_stream(trace_plan)
    try:
        await _supersede_active_responses_streams(trace_plan, active_stream)
        client = upstream_client or _get_excel_upstream_client()
        request = client.build_request("POST", upstream_url, headers=headers, json=body)
        try:
            upstream = await _open_streaming_upstream(
                client,
                request,
                trace_plan=trace_plan,
                downstream_request=downstream_request,
                active_stream=active_stream,
            )
        finally:
            if active_stream is not None:
                active_stream.response_ready.set()
    except _ResponsesSupersessionBlocked:
        status_code = 409
        message = (
            "The previous same-lineage generation could not be confirmed stopped; "
            "this follow-up was not sent upstream to prevent duplicate token spend."
        )
        try:
            _finish_usage_and_trace(trace_plan, status_code, response_text=message)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel="not_sent_supersession_blocked",
                confirmed=True,
            )
        return format_translation.openai_error_response(status_code, message)
    except _DownstreamDisconnectedBeforeResponse as exc:
        teardown_confirmed = exc.transport_close in {
            "http2_rst_cancel",
            "http1_connection_close",
            "not_sent",
        }
        if active_stream is not None:
            active_stream.cancel_requested = True
        if isinstance(trace_plan, UpstreamRequestPlan) and isinstance(
            trace_plan.trace_context,
            dict,
        ):
            trace_plan.trace_context["responses_stream_lifecycle"] = {
                "termination_cause": "downstream_disconnected_before_response",
                "terminal_event_seen": False,
                "terminal_event_type": None,
                "completed_event_seen": False,
                "generation_end_confirmed": False,
                "source_loop_completed": False,
                "transport_close": exc.transport_close,
                "transport_cancel_confirmed": teardown_confirmed,
                "teardown_confirmed": teardown_confirmed,
            }
        try:
            _finish_usage_and_trace(trace_plan, 499)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel=exc.transport_close,
                confirmed=teardown_confirmed,
            )
        return Response(status_code=499)
    except asyncio.CancelledError:
        transport_cancel = "not_sent_task_cancel"
        teardown_confirmed = active_stream is None or not active_stream.send_started
        if active_stream is not None:
            active_stream.cancel_requested = True
            if active_stream.upstream is not None:
                transport_cancel = await _close_upstream_response(
                    active_stream.upstream,
                    cancel_generation=True,
                )
                teardown_confirmed = transport_cancel in {
                    "http2_rst_cancel",
                    "http1_connection_close",
                }
            elif active_stream.send_started:
                transport_cancel = "pre_response_cancel_unconfirmed"
        try:
            _finish_usage_and_trace(trace_plan, 499)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel=transport_cancel,
                confirmed=teardown_confirmed,
            )
        raise
    except httpx.RequestError as exc:
        status_code, message = format_translation.upstream_request_error_status_and_message(exc)
        teardown_confirmed = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        try:
            _finish_usage_and_trace(trace_plan, status_code, response_text=message)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel=(
                    "connect_failed_before_request"
                    if teardown_confirmed
                    else "pre_response_request_error_unconfirmed"
                ),
                confirmed=teardown_confirmed,
            )
        return format_translation.openai_error_response(status_code, message)
    except Exception:
        try:
            _finish_usage_and_trace(trace_plan, 599)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel=(
                    "not_sent_setup_error"
                    if active_stream is None or not active_stream.send_started
                    else "pre_response_exception_unconfirmed"
                ),
                confirmed=active_stream is None or not active_stream.send_started,
            )
        raise

    if upstream.status_code >= 400:
        try:
            await upstream.aread()
            return _handle_upstream_error(
                upstream,
                trace_plan=trace_plan,
            )
        except asyncio.CancelledError:
            transport_cancel = await _close_upstream_response(
                upstream,
                cancel_generation=True,
            )
            transport_confirmed = transport_cancel in {
                "http2_rst_cancel",
                "http1_connection_close",
            }
            if isinstance(trace_plan, UpstreamRequestPlan) and isinstance(trace_plan.trace_context, dict):
                trace_plan.trace_context["responses_stream_lifecycle"] = {
                    "termination_cause": "upstream_error_body_cancelled",
                    "terminal_event_seen": False,
                    "terminal_event_type": None,
                    "completed_event_seen": False,
                    "generation_end_confirmed": False,
                    "transport_close": transport_cancel,
                    "transport_cancel_confirmed": transport_confirmed,
                    "teardown_confirmed": transport_confirmed,
                }
            try:
                _finish_usage_and_trace(trace_plan, 499, upstream=upstream)
            finally:
                _complete_active_responses_teardown(
                    active_stream,
                    transport_cancel=transport_cancel,
                    confirmed=transport_confirmed,
                )
            raise
        except httpx.RequestError as exc:
            status_code, message = format_translation.upstream_request_error_status_and_message(exc)
            transport_cancel = await _close_upstream_response(
                upstream,
                cancel_generation=True,
            )
            transport_confirmed = transport_cancel in {
                "http2_rst_cancel",
                "http1_connection_close",
            }
            if isinstance(trace_plan, UpstreamRequestPlan) and isinstance(trace_plan.trace_context, dict):
                trace_plan.trace_context["responses_stream_lifecycle"] = {
                    "termination_cause": "upstream_error_body_read",
                    "terminal_event_seen": False,
                    "terminal_event_type": None,
                    "completed_event_seen": False,
                    "generation_end_confirmed": False,
                    "transport_close": transport_cancel,
                    "transport_cancel_confirmed": transport_confirmed,
                    "teardown_confirmed": transport_confirmed,
                    "upstream_error_type": type(exc).__name__,
                }
            try:
                _finish_usage_and_trace(
                    trace_plan,
                    status_code,
                    upstream=upstream,
                    response_text=message,
                )
            finally:
                _complete_active_responses_teardown(
                    active_stream,
                    transport_cancel=transport_cancel,
                    confirmed=transport_confirmed,
                )
            return format_translation.openai_error_response(status_code, message)
        finally:
            if active_stream is None or not active_stream.teardown_complete.is_set():
                transport_close = await _close_upstream_response(upstream)
                _complete_active_responses_teardown(
                    active_stream,
                    transport_cancel=transport_close,
                    confirmed=True,
                )

    response_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    content_type = upstream.headers.get("content-type")
    if content_type:
        response_headers["content-type"] = content_type

    try:
        stream_body = _ManagedResponsesStreamBody(
            upstream=upstream,
            body=body,
            headers=headers,
            usage_event=usage_event,
            stream_type=stream_type,
            trace_plan=trace_plan,
            active_stream=active_stream,
            stream_transform=stream_transform,
            trace_details_factory=trace_details_factory,

        )
    except Exception:
        transport_cancel = await _close_upstream_response(
            upstream,
            cancel_generation=True,
        )
        try:
            _finish_usage_and_trace(trace_plan, 599, upstream=upstream)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel=transport_cancel,
                confirmed=transport_cancel in {
                    "http2_rst_cancel",
                    "http1_connection_close",
                },
            )
        raise
    if active_stream is not None:
        active_stream.stream_body = stream_body

    return GracefulStreamingResponse(
        stream_body,
        status_code=upstream.status_code,
        headers=response_headers,
    )


# ─── Responses reasoning streaming ──────────────────────────────────────────


def _responses_reasoning_stream_transform():
    """Stream transform adapting upstream Responses SSE reasoning events for Codex/Electron."""
    async def transform(byte_iter):
        reasoning_states: dict[str, dict] = {}

        async for event_name, data in format_translation.iter_sse_messages(byte_iter):
            if data == "[DONE]":
                yield b"data: [DONE]\n\n"
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                yield format_translation.sse_encode(event_name or "message", data)
                continue
            if not isinstance(payload, dict):
                yield format_translation.sse_encode(event_name or "message", payload)
                continue

            event_type = str(event_name or payload.get("type") or "").strip().lower()

            if event_type == "response.output_item.added":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    item_id = item.get("id") or "rs"
                    out_idx = payload.get("output_index", 0)
                    reasoning_states[item_id] = {
                        "output_index": out_idx,
                        "summary_started": False,
                        "header_sent": False,
                        "text_parts": [],
                    }
                    item.setdefault("summary", [])
                    item.setdefault("content", [])
                yield format_translation.sse_encode(event_type, payload)
                continue

            if event_type == "response.reasoning_summary_part.added":
                item_id = payload.get("item_id")
                if item_id in reasoning_states:
                    reasoning_states[item_id]["summary_started"] = True
                yield format_translation.sse_encode(event_type, payload)
                continue

            if event_type == "response.reasoning_text.delta":
                item_id = payload.get("item_id")
                delta = payload.get("delta")
                out_idx = payload.get("output_index", 0)
                state = reasoning_states.setdefault(
                    item_id,
                    {
                        "output_index": out_idx,
                        "summary_started": False,
                        "header_sent": False,
                        "text_parts": [],
                    },
                )
                if not state["summary_started"]:
                    state["summary_started"] = True
                    yield format_translation.sse_encode(
                        "response.reasoning_summary_part.added",
                        {
                            "type": "response.reasoning_summary_part.added",
                            "item_id": item_id,
                            "output_index": out_idx,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": ""},
                        },
                    )
                if isinstance(delta, str) and delta:
                    if not state["header_sent"]:
                        state["header_sent"] = True
                        if not delta.lstrip().startswith("**") and not delta.lstrip().startswith("#"):
                            header = format_translation._CODEX_THINKING_SUMMARY_HEADER
                            state["text_parts"].append(header)
                            yield format_translation.sse_encode(
                                "response.reasoning_summary_text.delta",
                                {
                                    "type": "response.reasoning_summary_text.delta",
                                    "item_id": item_id,
                                    "output_index": out_idx,
                                    "summary_index": 0,
                                    "delta": header,
                                },
                            )
                    state["text_parts"].append(delta)
                    yield format_translation.sse_encode(
                        "response.reasoning_summary_text.delta",
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "item_id": item_id,
                            "output_index": out_idx,
                            "summary_index": 0,
                            "delta": delta,
                        },
                    )
                yield format_translation.sse_encode(event_type, payload)
                continue

            if event_type == "response.reasoning_summary_text.delta":
                item_id = payload.get("item_id")
                delta = payload.get("delta")
                out_idx = payload.get("output_index", 0)
                state = reasoning_states.setdefault(
                    item_id,
                    {
                        "output_index": out_idx,
                        "summary_started": True,
                        "header_sent": False,
                        "text_parts": [],
                    },
                )
                if isinstance(delta, str) and delta:
                    if not state["header_sent"]:
                        state["header_sent"] = True
                        if not delta.lstrip().startswith("**") and not delta.lstrip().startswith("#"):
                            header = format_translation._CODEX_THINKING_SUMMARY_HEADER
                            state["text_parts"].append(header)
                            yield format_translation.sse_encode(
                                "response.reasoning_summary_text.delta",
                                {
                                    "type": "response.reasoning_summary_text.delta",
                                    "item_id": item_id,
                                    "output_index": out_idx,
                                    "summary_index": 0,
                                    "delta": header,
                                },
                            )
                    state["text_parts"].append(delta)
                yield format_translation.sse_encode(event_type, payload)
                continue

            if event_type == "response.reasoning_text.done":
                item_id = payload.get("item_id")
                out_idx = payload.get("output_index", 0)
                state = reasoning_states.get(item_id)
                full_text = "".join(state["text_parts"]) if state else (payload.get("text") or "")
                yield format_translation.sse_encode(
                    "response.reasoning_summary_text.done",
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": item_id,
                        "output_index": out_idx,
                        "summary_index": 0,
                        "text": full_text,
                    },
                )
                yield format_translation.sse_encode(
                    "response.reasoning_summary_part.done",
                    {
                        "type": "response.reasoning_summary_part.done",
                        "item_id": item_id,
                        "output_index": out_idx,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": full_text},
                    },
                )
                yield format_translation.sse_encode(event_type, payload)
                continue

            if event_type == "response.output_item.done":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    item_id = item.get("id")
                    state = reasoning_states.get(item_id)
                    text = "".join(state["text_parts"]) if (state and state["text_parts"]) else ""
                    format_translation.normalize_reasoning_item_for_client(item, fallback_text=text)
                yield format_translation.sse_encode(event_type, payload)
                continue

            if event_type in {"response.completed", "response.failed", "response.incomplete"}:
                resp = payload.get("response")
                if isinstance(resp, dict):
                    format_translation.normalize_response_reasoning_for_client(resp)
                yield format_translation.sse_encode(event_type, payload)
                continue

            yield format_translation.sse_encode(event_type or "message", payload)

    return transform


# ─── Dashboard routes ─────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard_root():
    return RedirectResponse(url="/ui", status_code=307)


# Cache the dashboard HTML and its compressed representation so requests do
# not repeat disk reads or compression.
_DASHBOARD_HTML_LOCK = threading.Lock()
_DASHBOARD_HTML_CACHE: dict[str, tuple[int, int, bytes, bytes, str]] = {}


def _load_dashboard_html_bytes(page: str = "dashboard.html") -> tuple[bytes, bytes, str]:
    path = os.path.join(os.path.dirname(DASHBOARD_FILE), page)
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


@app.get("/ui/dashboard.css")
async def dashboard_styles():
    return FileResponse(
        os.path.join(os.path.dirname(DASHBOARD_FILE), "dashboard.css"),
        media_type="text/css", headers={"Cache-Control": "no-cache"},
    )


@app.get("/ui/requests", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
async def dashboard(request: Request):
    page = "requests.html" if request.url.path == "/ui/requests" else "dashboard.html"
    raw, gzipped, etag = _load_dashboard_html_bytes(page)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
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


def _build_dashboard_response_body(refresh: bool, gzip_body: bool = False) -> tuple[bytes, bool]:
    # Browser refreshes are advisory: the stream version already invalidates
    # the in-memory materialized payload when data changes.  Rebuilding an
    # unchanged archive just because the URL contains refresh=1 defeats the
    # dashboard cache.
    payload = dashboard_service.build_payload(refresh, prefer_cached=True)
    body = json.dumps(payload, separators=(",", ":"), default=util._json_default).encode("utf-8")
    if gzip_body and len(body) >= 1024:
        return gzip.compress(body, compresslevel=6), True
    return body, False


def _build_dashboard_sse_event(event_name: str) -> bytes:
    payload = dashboard_service.build_payload(False)
    return format_translation.sse_encode(event_name, payload)


def _require_local_quota_request(request: Request):
    origin = request.headers.get("origin")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if request.url.hostname not in local_hosts or (
        request.client and request.client.host not in local_hosts
    ) or (origin and origin != f"{request.url.scheme}://{request.url.netloc}"):
        raise HTTPException(status_code=403, detail="Open quota checking from the local dashboard.")


@app.get("/api/account-quota")
async def account_quota_status_api(request: Request):
    _require_local_quota_request(request)
    return JSONResponse(account_quota.quota_service.snapshot(), headers={"Cache-Control": "no-store"})


@app.post("/api/account-quota")
async def account_quota_refresh_api(request: Request):
    _require_local_quota_request(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Use application/json for quota checks.")
    await parse_json_request(request)
    payload = await asyncio.to_thread(account_quota.quota_service.refresh)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/dashboard")
async def dashboard_api(request: Request):
    refresh = request.query_params.get("refresh", "").lower() in {"1", "true", "yes"}
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


@app.get("/api/dashboard/stream")
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
        yield format_translation.sse_encode("heartbeat", {"at": util.utc_now_iso()})
        last_heartbeat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    version = await asyncio.wait_for(queue.get(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - last_heartbeat >= heartbeat_seconds:
                        last_heartbeat = now
                        yield format_translation.sse_encode("heartbeat", {"at": util.utc_now_iso()})
                    continue

                if version == last_version:
                    continue
                last_version = version
                chunk = await asyncio.to_thread(_build_dashboard_sse_event, "dashboard")
                yield chunk
        finally:
            dashboard_service.unregister_stream_listener(queue)

    return GracefulStreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def _load_request_prompt_payload(request_id: str) -> dict:
    if not isinstance(request_id, str) or not request_id:
        return {"available": False}
    _prune_request_prompt_archive()
    target = None
    for event in reversed(usage_tracker.snapshot_usage_events()):
        if (
            isinstance(event, dict)
            and any(event.get(key) == request_id for key in ("request_id", "client_request_id", "server_request_id"))
        ):
            target = event
            break
    if target is not None:
        raw_prompt = target.get("request_prompt")
        if isinstance(raw_prompt, dict):
            prompt = _prompt_payload_for_dashboard(raw_prompt)
            if isinstance(prompt, dict):
                return {"available": True, "request_prompt": prompt}
            return {"available": True, "locked": True}

    archive_ids = [request_id]
    if isinstance(target, dict):
        target_request_id = target.get("request_id")
        if isinstance(target_request_id, str) and target_request_id and target_request_id not in archive_ids:
            archive_ids.append(target_request_id)
    for archive_id in archive_ids:
        archived_prompt = _load_request_prompt_record(archive_id)
        if not isinstance(archived_prompt, dict):
            continue
        prompt_text = archived_prompt.get("prompt_text")
        if isinstance(prompt_text, str) and prompt_text.strip():
            return {
                "available": True,
                "request_prompt": {"user": prompt_text},
                "prompt_text": prompt_text,
                "path": archived_prompt.get("path"),
                "stored_at": archived_prompt.get("stored_at"),
                "char_count": archived_prompt.get("char_count"),
            }

    if target is None:
        return {"available": False, "not_found": True}
    return {"available": False}


@app.get("/api/request-prompt/{request_id}")
async def request_prompt_api(request_id: str):
    payload = await asyncio.to_thread(_load_request_prompt_payload, request_id)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


# ─── Config API routes ────────────────────────────────────────────────────────


@app.get("/api/config/client-proxy")
async def client_proxy_status_api():
    payload = client_proxy_config_service.proxy_client_status_payload()
    settings = payload.get("settings")
    if isinstance(settings, dict):
        payload["settings"] = _client_proxy_settings_with_trace_status(settings)
    return JSONResponse(content=payload)


@app.post("/api/config/client-proxy/settings")
async def client_proxy_settings_api(request: Request):
    payload = await parse_json_request(request)
    result = _save_client_proxy_settings(payload)
    return JSONResponse(content=result)


@app.post("/api/config/client-proxy")
async def client_proxy_install_api(request: Request):
    payload = await parse_json_request(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    targets = normalize_proxy_targets(payload)
    action = payload.get("action", "enable")
    if not isinstance(action, str):
        raise HTTPException(status_code=400, detail='Action must be "enable" or "disable".')

    action = action.strip().lower()
    if action == "install":
        action = "enable"
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=400, detail='Unsupported action. Use "enable" or "disable".')

    clients = {}

    for target in targets:
        try:
            if action == "disable":
                clients[target] = client_proxy_config_service.disable_target(target)
            else:
                clients[target] = client_proxy_config_service.enable_target(target)
        except Exception as exc:
            clients[target] = client_proxy_config_service.empty_proxy_status(target)
            clients[target]["error"] = str(exc)
            clients[target]["status_message"] = "failed to write config"

    return JSONResponse(
        content={
            "clients": clients,
            "message": (
                "Proxy enabled for: "
                if action == "enable"
                else "Proxy disabled for: "
            )
            + (
                ", ".join(
                    target
                    for target, payload in sorted(clients.items())
                    if not payload.get("error")
                )
                or "none"
            ),
        }
    )


# ─── Route: /v1/responses  (Codex / Responses API) ───────────────────────────

@app.get("/api/config/background-proxy")
async def background_proxy_status_api():
    return JSONResponse(content=background_proxy_manager.status_payload())


@app.get("/api/config/excel-session")
async def excel_session_status_api():
    excel_session_capture.refresh_macos_excel_session(
        excel_upstream.excel_session_store,
    )
    excel_session_capture.refresh_windows_excel_session(
        excel_upstream.excel_session_store,
    )
    return JSONResponse(
        content={
            **excel_upstream.excel_session_store.status(),
            "capture": excel_session_capture.cached_session_reader_status(),
            "models": [
                {"id": model_id, "display_name": excel_upstream.LOCAL_MODEL_CAPABILITIES[model_id]["display_name"]}
                for model_id in excel_upstream.MODEL_IDS
            ],
            "default_model": excel_upstream.MODEL_ID,
        }
    )


_EXCEL_CONNECTION_TEST_TIMEOUT_SECONDS = 30
_excel_connection_test_lock = asyncio.Lock()


@app.post("/api/config/excel-session/test")
async def excel_session_test_api(request: Request):
    # This manual, quota-consuming action is available only to the local UI
    # and JSON API clients, never cross-site browser forms or DNS rebinding.
    origin = request.headers.get("origin")
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"} or (
        origin and origin != f"{request.url.scheme}://{request.url.netloc}"
    ):
        raise HTTPException(status_code=403, detail="Open the connection test from the local dashboard.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Use application/json for connection tests.")
    payload = await parse_json_request(request)
    model = excel_upstream.excel_model_id(payload.get("model"))
    if model is None:
        raise HTTPException(status_code=400, detail="Select a listed Excel model.")
    if _excel_connection_test_lock.locked():
        return JSONResponse(status_code=429, content={
            "ok": False, "model": model, "category": "busy", "message": "An Excel connection test is already running.",
        })
    async with _excel_connection_test_lock:
        try:
            async with asyncio.timeout(_EXCEL_CONNECTION_TEST_TIMEOUT_SECONDS):
                response = await _handle_excel_responses(request, {
                    "model": model, "stream": False, "input": "Reply with exactly OK.",
                    "tool_choice": "none", "reasoning": {"effort": "medium"},
                })
        except TimeoutError:
            return JSONResponse(status_code=504, content={
                "ok": False, "model": model, "category": "timeout",
                "message": "The Excel connection test timed out. Check the connection and try again.",
            })
    result = json.loads(response.body)
    text = format_translation.extract_response_output_text(result) or ""
    ok = response.status_code == 200 and result.get("status") == "completed" and bool(text.strip())
    error = result.get("error") or {}
    code = error.get("code") if isinstance(error, dict) else None
    category = {
        401: "authentication", 403: "access", 429: "rate_limit", 504: "timeout",
        400: "request", 404: "request", 422: "request",
    }.get(response.status_code, "upstream" if code == "excel_upstream_error" else "protocol")
    if code == "basispoints_model_access_changed":
        category = "access"
    messages = {
        "authentication": "Refresh the signed-in ChatGPT Excel add-in session and test again.",
        "access": "This Excel account cannot access the selected model or endpoint.",
        "rate_limit": "The Excel endpoint is rate limited. Test again later.",
        "timeout": "The Excel connection test timed out. Check the connection and try again.",
        "request": "Excel rejected the test request. Check model access and proxy compatibility.",
        "upstream": "The Excel service could not complete the test. Try again later.",
        "protocol": "Excel did not return a completed text response. Check proxy compatibility.",
    }
    content = {
        "ok": ok, "model": model, "category": "success" if ok else category,
        "message": "The selected model returned a completed text response." if ok else messages[category],
        "status_code": response.status_code,
    }
    if response.headers.get("x-request-id"):
        content["request_id"] = response.headers["x-request-id"]
    status_code = 200 if ok else response.status_code if response.status_code >= 400 else 502
    return JSONResponse(status_code=status_code, content=content)


@app.post("/api/config/excel-session")
async def excel_session_config_api(request: Request):
    payload = await parse_json_request(request)
    action = str(payload.get("action") or "").strip().lower()
    if action in {"cancel_capture", "cancel_read"}:
        return JSONResponse(
            content={
                **excel_upstream.excel_session_store.status(),
                "capture": excel_session_capture.cached_session_reader_status(),
            }
        )
    if action in {"capture", "read_cached"}:
        excel_session_capture.refresh_macos_excel_session(
            excel_upstream.excel_session_store,
            force=True,
        )
        excel_session_capture.refresh_windows_excel_session(
            excel_upstream.excel_session_store,
            force=True,
        )
        return JSONResponse(
            content={
                **excel_upstream.excel_session_store.status(),
                "capture": excel_session_capture.cached_session_reader_status(),
            }
        )
    try:
        status = excel_upstream.excel_session_store.configure(
            payload.get("headers"),
            tools_version_id=payload.get("tools_version_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        content={
            **status,
            "capture": excel_session_capture.cached_session_reader_status(),
        }
    )


@app.delete("/api/config/excel-session")
async def excel_session_clear_api():
    return JSONResponse(
        content={
            **excel_upstream.excel_session_store.clear(),
            "capture": excel_session_capture.cached_session_reader_status(),
        }
    )


@app.post("/api/config/background-proxy")
async def background_proxy_config_api(request: Request):
    payload = await parse_json_request(request)
    action = payload.get("action")
    try:
        if action == "enable_startup":
            result = background_proxy_manager.enable_startup()
            message = "Background startup enabled."
        elif action == "disable_startup":
            result = background_proxy_manager.disable_startup()
            message = "Background startup disabled."
        elif action == "install_shell_commands":
            result = background_proxy_manager.install_shell_commands()
            message = "Shell commands installed."
        elif action == "uninstall_shell_commands":
            result = background_proxy_manager.uninstall_shell_commands()
            message = "Shell commands removed."
        else:
            raise HTTPException(status_code=400, detail="Unsupported background proxy action.")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update background proxy setup: {exc}") from exc
    return JSONResponse(content={**result, "message": message})


def _excel_tool_call_event_bytes(
    tool_call: dict,
    *,
    output_index: int,
) -> list[bytes]:
    item = dict(tool_call)
    item["status"] = "in_progress"
    if tool_call["type"] == "function_call":
        item["arguments"] = ""
        value_key = "arguments"
        delta_event = "response.function_call_arguments.delta"
        done_event = "response.function_call_arguments.done"
    else:
        item["input"] = ""
        value_key = "input"
        delta_event = "response.custom_tool_call_input.delta"
        done_event = "response.custom_tool_call_input.done"
    return [
        format_translation.sse_encode(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            },
        ),
        format_translation.sse_encode(
            delta_event,
            {
                "type": delta_event,
                "output_index": output_index,
                "item_id": tool_call["id"],
                "delta": tool_call[value_key],
            },
        ),
        format_translation.sse_encode(
            done_event,
            {
                "type": done_event,
                "output_index": output_index,
                "item_id": tool_call["id"],
                value_key: tool_call[value_key],
            },
        ),
        format_translation.sse_encode(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": {**tool_call, "status": "completed"},
            },
        ),
    ]


def _excel_completed_response(response: object, finished_items: dict[int, dict]) -> dict:
    if not isinstance(response, dict) or response.get("status") not in (None, "completed"):
        raise upstream_errors.ExcelResponseError(
            "excel_invalid_response", "Excel response.completed has no valid response payload",
        )
    result = dict(response)
    terminal_output = response.get("output")
    if terminal_output is None:
        terminal_output = []
    if not isinstance(terminal_output, list):
        raise upstream_errors.ExcelResponseError("excel_invalid_response", "Excel response has an invalid output list")
    for item in list(finished_items.values()) + terminal_output:
        if not isinstance(item, dict) or (item.get("id") is not None and not isinstance(item["id"], str)):
            raise upstream_errors.ExcelResponseError(
                "excel_invalid_output_item", "Excel response has an invalid output item",
            )
    output = dict(finished_items)
    item_indices = {item["id"]: index for index, item in finished_items.items() if item.get("id")}
    for index, item in enumerate(terminal_output):
        index = item_indices.get(item.get("id"), index)
        previous = output.get(index, {})
        if previous.get("id") and item.get("id") and previous["id"] != item["id"]:
            raise upstream_errors.ExcelResponseError(
                "excel_conflicting_output_items", "Excel response has conflicting output item identities",
            )
        output[index] = {**previous, **item}
    if sorted(output) != list(range(len(output))):
        raise upstream_errors.ExcelResponseError(
            "excel_missing_output_items", "Excel response is missing completed output items",
        )
    result["output"] = [output[index] for index in sorted(output)]
    if not format_translation.extract_response_output_text(result) and not any(
        item.get("type") in {"function_call", "custom_tool_call", "compaction"}
        for item in result["output"]
    ):
        raise upstream_errors.ExcelResponseError(
            "excel_empty_response", "Excel completed without assistant text, a tool call, or compaction",
        )
    return result


EXCEL_STREAM_HEARTBEAT_SECONDS = 15.0


def _excel_tool_stream_transform(source_body: dict):
    allowed_tools = excel_upstream.client_tool_types(source_body)
    marker_open = excel_upstream.TOOL_CALL_MARKER_OPEN

    def _marker_hold_length(text: str) -> int:
        """Length of the text suffix that could still become a marker open tag."""
        max_probe = min(len(marker_open) - 1, len(text))
        for probe in range(max_probe, 0, -1):
            if text.endswith(marker_open[:probe]):
                return probe
        return 0

    async def transform(byte_iter):
        full_text = ""
        emitted_upto = 0
        marker_mode = False
        held_events: list[bytes] = []
        delta_template: dict = {}
        finished_items: dict[int, dict] = {}
        native_call_seen = False

        def flush_text() -> list[bytes]:
            nonlocal emitted_upto
            pending = full_text[emitted_upto:]
            if not pending:
                return []
            emitted_upto = len(full_text)
            return [
                format_translation.sse_encode(
                    "response.output_text.delta",
                    {**delta_template, "type": "response.output_text.delta", "delta": pending},
                )
            ]

        async with aclosing(excel_stream.iter_events(
            byte_iter, heartbeat_seconds=EXCEL_STREAM_HEARTBEAT_SECONDS,
        )) as events:
            async for event_name, data in events:
                if not data:
                    yield b": keep-alive" + bytes([10, 10])
                    continue
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                event_type = str(event_name or payload.get("type") or "").strip().lower()
                event_output_index = payload.get("output_index")
                encoded = format_translation.sse_encode(event_type or "message", payload)

                if event_type == "response.output_text.delta":
                    delta = payload.get("delta")
                    if isinstance(delta, str):
                        full_text += delta
                    delta_template = {
                        key: payload[key]
                        for key in ("item_id", "output_index", "content_index")
                        if key in payload
                    }
                    if marker_mode:
                        continue
                    search_start = max(0, emitted_upto - len(marker_open) + 1)
                    marker_pos = full_text.find(marker_open, search_start)
                    if marker_pos != -1:
                        marker_mode = True
                        pending = full_text[emitted_upto:marker_pos]
                        emitted_upto = marker_pos
                        if pending:
                            yield format_translation.sse_encode(
                                "response.output_text.delta",
                                {
                                    **delta_template,
                                    "type": "response.output_text.delta",
                                    "delta": pending,
                                },
                            )
                        continue
                    boundary = len(full_text) - _marker_hold_length(full_text)
                    if boundary > emitted_upto:
                        pending = full_text[emitted_upto:boundary]
                        emitted_upto = boundary
                        yield format_translation.sse_encode(
                            "response.output_text.delta",
                            {
                                **delta_template,
                                "type": "response.output_text.delta",
                                "delta": pending,
                            },
                        )
                    continue

                if event_type == "response.output_text.done":
                    if marker_mode:
                        held_events.append(encoded)
                        continue
                    for chunk in flush_text():
                        yield chunk
                    yield encoded
                    continue

                # Native tool-call events must never reach Codex raw: their
                # arguments follow the upstream's server-tool schema, and Codex
                # executing the un-normalized call fails and provokes retry
                # loops. Convert the completed item after a real terminal event.
                if event_type in {
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                    "response.custom_tool_call_input.delta",
                    "response.custom_tool_call_input.done",
                }:
                    native_call_seen = True
                    continue

                if event_type in {"response.output_item.added", "response.output_item.done"}:
                    item = payload.get("item")
                    item_type = (
                        item.get("type") if isinstance(item, dict) else None
                    )
                    if event_type == "response.output_item.done" and isinstance(item, dict):
                        if isinstance(event_output_index, int) and event_output_index >= 0:
                            finished_items[event_output_index] = dict(item)
                    if item_type in {"function_call", "custom_tool_call"}:
                        native_call_seen = True
                        continue
                    if (
                        event_type == "response.output_item.done"
                        and marker_mode
                        and item_type == "message"
                    ):
                        held_events.append(encoded)
                        continue
                    if item_type == "reasoning":
                        format_translation.normalize_reasoning_item_for_client(item)
                        yield format_translation.sse_encode(event_type, payload)
                        continue
                    yield encoded
                    continue

                if event_type in {"response.completed", "response.failed", "response.incomplete"}:
                    response = payload.get("response")
                    response = response if isinstance(response, dict) else None
                    if event_type != "response.completed":
                        # A partial native call is never executable client work.
                        if response and isinstance(response.get("output"), list):
                            response["output"] = [item for item in response["output"]
                                                  if not isinstance(item, dict)
                                                  or item.get("type") not in {"function_call", "custom_tool_call"}]
                        safe_response = {key: response[key] for key in ("id", "status", "output", "usage")
                                         if response and key in response}
                        safe_response["error"] = upstream_errors.excel_error_payload(502, response)["error"]
                        yield format_translation.sse_encode(event_type, {"type": event_type, "response": safe_response})
                        return
                    response = _excel_completed_response(response, finished_items)
                    payload["response"] = response
                    format_translation.normalize_response_reasoning_for_client(response)
                    completed_text = full_text or format_translation.extract_response_output_text(response)
                    tool_call = excel_upstream.extract_client_tool_call(completed_text or "", allowed_tools)
                    tool_diagnostics: dict = {}
                    tool_calls = excel_upstream.extract_native_client_tool_calls(
                        response, source_body, diagnostics=tool_diagnostics,
                    )
                    has_native_calls = any(
                        item.get("type") in {"function_call", "custom_tool_call"}
                        for item in response.get("output", []) if isinstance(item, dict)
                    )
                    if not has_native_calls and tool_call is not None:
                        tool_calls = [tool_call]
                    if tool_calls is not None:
                        held_events.clear()
                        emitted_upto = len(full_text)
                        response_payload = excel_upstream.response_payload_with_tool_calls(
                            response, tool_calls,
                            model_id=excel_upstream.excel_model_id(source_body.get("model"))
                            or excel_upstream.MODEL_ID,
                        )
                        for index, item in enumerate(response_payload["output"]):
                            if item.get("type") not in {"function_call", "custom_tool_call"}:
                                continue
                            for chunk in _excel_tool_call_event_bytes(item, output_index=index):
                                yield chunk
                        yield format_translation.sse_encode(
                            "response.completed", {"type": "response.completed", "response": response_payload},
                        )
                        return
                    if native_call_seen or any(isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}
                           for item in response.get("output", [])):
                        raise upstream_errors.ExcelResponseError(
                            "excel_untranslatable_tool_call",
                            excel_upstream.tool_call_failure_message(tool_diagnostics),
                        )
                    # Not a tool call after all: release everything that was held
                    # back so the client still receives the full assistant text.
                    for chunk in flush_text():
                        yield chunk
                    for held in held_events:
                        yield held
                    held_events.clear()
                    marker_mode = False
                    yield format_translation.sse_encode(event_type, payload)
                    return

                if event_type == "error":
                    yield format_translation.sse_encode("error", {"type": "error", **upstream_errors.excel_error_payload(502, payload)})
                    return

                yield encoded

        # EOF (even [DONE]) is not proof of a completed model response. Leave
        # calls unexposed so the client can retry without repeating tool work.
        raise upstream_errors.ExcelResponseError(
            "excel_stream_incomplete", "Excel stream ended before a terminal Responses event",
        )

    return transform


async def _read_excel_non_streaming_response_payload(
    upstream: httpx.Response,
    usage_event: dict | None = None,
) -> dict | None:
    finished_items: dict[int, dict] = {}
    capture = usage_tracker.create_sse_capture("responses")
    async with aclosing(excel_stream.iter_events(upstream.aiter_bytes())) as events:
        async for event_name, data in events:
            if data == "[DONE]":
                break
            try:
                parsed = json.loads(data or "")
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            event_type = str(event_name or parsed.get("type") or "").strip().lower()
            parsed["type"] = event_type
            if capture.consume_responses_payload(parsed):
                usage_tracker.mark_first_output(usage_event)
            if event_type == "response.output_item.done":
                index, item = parsed.get("output_index"), parsed.get("item")
                if isinstance(index, int) and index >= 0 and isinstance(item, dict):
                    finished_items[index] = item
            elif event_type == "response.completed":
                # Do not wait for EOF: BPS can send a malformed HTTP tail after
                # the valid terminal SSE event, or leave the connection open.
                return _excel_completed_response(parsed.get("response"), finished_items)
            elif event_type in {"response.failed", "response.incomplete"}:
                return parsed.get("response")
            elif event_type == "error":
                return {"status": "failed", "error": parsed.get("error", parsed)}
    raise upstream_errors.ExcelResponseError(
        "excel_stream_incomplete", "Excel stream ended before a terminal Responses event",
    )


async def _post_excel_non_streaming_request(
    plan: UpstreamRequestPlan,
    *,
    client_body: dict,
) -> Response:
    excel_model_id = (
        excel_upstream.excel_model_id(client_body.get("model"))
        or excel_upstream.MODEL_ID
    )
    client = _get_excel_upstream_client()
    upstream: httpx.Response | None = None
    response_payload: dict | None = None
    try:
        request = client.build_request(
            "POST", plan.upstream_url, headers=plan.headers, json=plan.body,
        )
        # Only the shared connect-only retry policy is safe for a model POST.
        upstream = await throttled_client_send(client, request, stream=True)
        if upstream.status_code >= 400:
            await upstream.aread()
            return _handle_upstream_error(
                upstream, trace_plan=plan,
            )
        if "text/event-stream" in upstream.headers.get("content-type", "").lower():
            response_payload = await _read_excel_non_streaming_response_payload(upstream, plan.usage_event)
        else:
            await upstream.aread()
            response_payload = _extract_upstream_json_payload(upstream)
            if isinstance(response_payload, dict) and response_payload.get("status") not in {"failed", "incomplete"}:
                response_payload = _excel_completed_response(response_payload, {})
                # JSON has no observable token stream; record output on arrival.
                capture = usage_tracker.create_sse_capture("responses")
                if capture.consume_responses_payload({"response": response_payload}):
                    usage_tracker.mark_first_output(plan.usage_event)
    except asyncio.CancelledError:
        _finish_usage_and_trace(plan, 499, upstream=upstream)
        raise
    except upstream_errors.ExcelResponseError as exc:
        payload = {"error": {
            "type": "server_error", "code": exc.code, "message": str(exc), "param": None,
        }}
        _finish_usage_and_trace(plan, 502, upstream=upstream, response_payload=payload)
        return JSONResponse(status_code=502, content=payload)
    except httpx.RequestError as exc:
        status_code, message = format_translation.upstream_request_error_status_and_message(exc)
        _finish_usage_and_trace(plan, status_code, upstream=upstream, response_text=message)
        return format_translation.openai_error_response(status_code, message)
    except Exception:
        _finish_usage_and_trace(plan, 599, upstream=upstream)
        raise
    finally:
        if upstream is not None:
            await upstream.aclose()

    if not isinstance(response_payload, dict):
        message = "Upstream response did not include a completed Responses payload"
        _finish_usage_and_trace(plan, 502, response_text=message)
        return format_translation.openai_error_response(502, message)

    translated_payload = dict(response_payload)
    translated_payload["model"] = excel_model_id
    if response_payload.get("status") in {"failed", "incomplete"}:
        _finish_usage_and_trace(plan, 502, upstream=upstream, response_payload=response_payload)
        return JSONResponse(status_code=502, content=upstream_errors.excel_error_payload(502, response_payload))
    response_text = format_translation.extract_response_output_text(response_payload)
    tool_call = excel_upstream.extract_client_tool_call(
        response_text,
        excel_upstream.client_tool_types(client_body),
    )
    tool_diagnostics: dict = {}
    tool_calls = excel_upstream.extract_native_client_tool_calls(
        response_payload, client_body, diagnostics=tool_diagnostics,
    )
    has_native_calls = any(
        isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}
        for item in response_payload.get("output", [])
    )
    if not has_native_calls and tool_call is not None:
        tool_calls = [tool_call]
    if tool_calls is not None:
        translated_payload = excel_upstream.response_payload_with_tool_calls(
            response_payload, tool_calls, model_id=excel_model_id,
        )
        format_translation.normalize_response_reasoning_for_client(translated_payload)
    elif any(isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}
             for item in response_payload.get("output", [])):
        message = excel_upstream.tool_call_failure_message(tool_diagnostics)
        error_payload = {"error": {
            "type": "server_error", "code": "excel_untranslatable_tool_call",
            "message": message, "param": None,
        }}
        _finish_usage_and_trace(plan, 502, upstream=upstream,
                                response_payload=error_payload, response_text=message)
        return JSONResponse(status_code=502, content=error_payload)

    _finish_usage_and_trace(
        plan,
        upstream.status_code,
        upstream=upstream,
        response_payload=(
            translated_payload if isinstance(translated_payload, dict) else None
        ),
        response_text=(
            format_translation.extract_response_output_text(translated_payload)
            if isinstance(translated_payload, dict)
            else _extract_upstream_text(upstream)
        ),
    )
    if isinstance(translated_payload, dict):
        return JSONResponse(
            content=translated_payload,
            status_code=upstream.status_code,
            headers={"x-request-id": upstream.headers["x-request-id"]} if "x-request-id" in upstream.headers else {},
        )
    return proxy_non_streaming_response(upstream)


async def _handle_excel_responses(
    request: Request,
    body: dict,
    *,
    source_body: dict | None = None,
) -> Response:
    excel_model_id = (
        excel_upstream.excel_model_id(body.get("model")) or excel_upstream.MODEL_ID
    )
    await asyncio.to_thread(
        excel_session_capture.refresh_macos_excel_session,
        excel_upstream.excel_session_store,
        force=True,
    )
    await asyncio.to_thread(
        excel_session_capture.refresh_windows_excel_session,
        excel_upstream.excel_session_store,
        force=True,
    )
    try:
        excel_headers = excel_upstream.excel_session_store.request_headers(
            stream=bool(body.get("stream")),
        )
    except RuntimeError as exc:
        return format_translation.openai_error_response(401, str(exc))
    try:
        upstream_body = excel_upstream.prepare_responses_body(
            body,
            tools_version_id=excel_upstream.excel_session_store.tools_version_id(),
        )
    except ValueError as exc:
        return format_translation.openai_error_response(400, str(exc), param=getattr(exc, "param", "input"))

    client = _get_excel_upstream_client()
    for attempt in range(2):
        try:
            # Preserve task/turn identity from the original image data, even
            # when an expired attachment needs to be uploaded with a new ID.
            image_body, reused_images = await excel_images.image_uploads.rewrite(
                upstream_body, client, excel_headers,
            )
        except ValueError as exc:
            return format_translation.openai_error_response(400, str(exc), param="input")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return format_translation.openai_error_response(
                status if status >= 400 else 502,
                f"Excel image upload failed (HTTP {status}). Retry the request.",
                param="input",
            )
        except httpx.RequestError as exc:
            status, message = format_translation.upstream_request_error_status_and_message(exc)
            return format_translation.openai_error_response(
                status, f"Excel image upload failed: {message}.", param="input",
            )

        plan, error_response = _prepare_upstream_request(
            request,
            body=image_body,
            requested_model=excel_model_id,
            resolved_model=excel_model_id,
            upstream_path="/basispoints/api/responses",
            upstream_url=excel_upstream.RESPONSES_URL,
            header_builder=lambda _api_key, _request_id: dict(excel_headers),
            error_response=format_translation.openai_error_response,
            api_key="excel-session",
            source_body=source_body if isinstance(source_body, dict) else body,
            trace_metadata={
                "bridge": True,
                "strategy_name": "responses_to_excel_responses",
                "caller_protocol": "responses",
                "upstream_protocol": "responses",
                "header_kind": "excel-session",
            },
        )
        if error_response is not None:
            return error_response
        if bool(upstream_body.get("stream")):
            response = await proxy_streaming_response(
                plan.upstream_url,
                plan.headers,
                plan.body,
                timeout=300,
                usage_event=plan.usage_event,
                stream_type="responses",
                trace_plan=plan,
                downstream_request=request,
                stream_transform=_excel_tool_stream_transform(body),
                upstream_client=client,
            )
        else:
            response = await _post_excel_non_streaming_request(plan, client_body=body)
        if attempt == 0 and reused_images and response.status_code in {400, 404, 422}:
            excel_images.image_uploads.forget(reused_images)
            continue
        return response


def _excel_request_body(body: dict) -> dict:
    requested = body.get("model", excel_upstream.MODEL_ID)
    model = excel_upstream.excel_model_id(requested)
    if model is None and isinstance(requested, str):
        model = excel_upstream.excel_model_id(requested.strip().lower() + "-excel")
    if model is None:
        raise HTTPException(status_code=400, detail="Unsupported model. Select a model from /v1/models.")
    return {**body, "model": model}


async def _handle_excel_image_request(request: Request, *, edit: bool) -> Response:
    if "application/json" not in request.headers.get("content-type", "").lower():
        return format_translation.openai_error_response(
            415, "Use a JSON image request; edits take images as inline image_url data URLs.",
        )
    try:
        body = await request.json()
        send = excel_image_generation.prepare_request(body, edit=edit)
    except (ValueError, UnicodeDecodeError) as exc:
        return format_translation.openai_error_response(400, str(exc))
    for refresh in (excel_session_capture.refresh_macos_excel_session,
                    excel_session_capture.refresh_windows_excel_session):
        refresh(excel_upstream.excel_session_store, force=True)
    try:
        session_headers = excel_upstream.excel_session_store.request_headers(stream=False)
    except RuntimeError:
        return format_translation.openai_error_response(401, "Refresh the signed-in ChatGPT Excel add-in session.")
    headers = {key: value for key, value in session_headers.items()
               if key.lower() not in {"content-type", "content-length", "accept"}}
    headers["accept"] = "application/json"
    url = excel_image_generation.EDITS_URL if edit else excel_image_generation.GENERATIONS_URL
    client = _get_excel_upstream_client()
    try:
        upstream_request = client.build_request(
            "POST", url, headers=headers, timeout=httpx.Timeout(600.0, connect=30.0), **send,
        )
        upstream = await throttled_client_send(client, upstream_request, follow_redirects=False)
    except httpx.RequestError as exc:
        status, message = format_translation.upstream_request_error_status_and_message(exc)
        return format_translation.openai_error_response(status, message)
    if upstream.status_code >= 300:
        status = upstream.status_code if upstream.status_code >= 400 else 502
        return JSONResponse(status_code=status, content=upstream_errors.excel_error_payload(status))
    try:
        payload = excel_image_generation.validate_response(upstream.json())
    except (ValueError, UnicodeDecodeError):
        return format_translation.openai_error_response(502, "Excel returned an invalid image response.")
    return JSONResponse(content=payload)


@app.post("/images/generations")
@app.post("/v1/images/generations")
async def images_generate(request: Request):
    return await _handle_excel_image_request(request, edit=False)


@app.post("/images/edits")
@app.post("/v1/images/edits")
async def images_edit(request: Request):
    return await _handle_excel_image_request(request, edit=True)


@app.post("/responses")
@app.post("/v1/responses")
async def responses(request: Request):
    try:
        body = await parse_json_request(request)
        body = _excel_request_body(body)
    except HTTPException as exc:
        return format_translation.openai_error_response(
            exc.status_code, format_translation.http_exception_detail_to_message(exc.detail),
            param="model" if exc.status_code == 400 and str(exc.detail).startswith("Unsupported model") else None,
        )
    body = codex_agent_compat.normalize_codex_agent_tools(body)
    return await _handle_excel_responses(request, body)


@app.post("/responses/compact")
@app.post("/v1/responses/compact")
async def responses_compact(request: Request):
    try:
        body = await parse_json_request(request)
    except HTTPException as exc:
        return format_translation.openai_error_response(
            exc.status_code,
            format_translation.http_exception_detail_to_message(exc.detail),
        )

    try:
        body = _excel_request_body(body)
    except HTTPException as exc:
        return format_translation.openai_error_response(exc.status_code, str(exc.detail), param="model")
    summary_request = format_translation.build_fake_compaction_request(body)
    summary_request["stream"] = False
    summary_request.pop("tools", None)
    summary_request.pop("tool_choice", None)
    summary_request.pop("parallel_tool_calls", None)
    response = await _handle_excel_responses(
        request,
        summary_request,
        source_body=body,
    )
    if response.status_code >= 400:
        return response
    payload = json.loads(response.body)
    if payload.get("status") != "completed" or not format_translation.extract_response_output_text(payload):
        return format_translation.openai_error_response(502, "Excel compaction did not produce a complete summary; keep the existing history")
    compacted = format_translation.responses_to_compaction_response(payload, fallback_model=body.get("model"))
    source_input = body.get("input", [])
    if isinstance(source_input, str):
        source_input = [{"type": "message", "role": "user", "content": source_input}]
    elif not isinstance(source_input, list):
        source_input = []
    # Codex can replace its entire context with compact.output. Preserve
    # the user's instructions alongside the summary in that handoff.
    retained = [item for item in source_input if isinstance(item, dict)
                and item.get("role") in {"system", "developer", "user"}
                and item.get("type") in (None, "message")]
    return JSONResponse(content={
        "id": compacted["id"],
        "object": "response.compaction",
        "created_at": compacted.get("created_at") or int(time.time()),
        "output": retained + compacted["output"],
        "usage": compacted["usage"],
    })


# ─── Excel model catalog ────────────────────────────────────────────────────


@app.get("/models")
@app.get("/v1/models")
async def models():
    return JSONResponse(content={
        "object": "list",
        "data": [excel_upstream.local_model_payload(model) for model in excel_upstream.MODEL_IDS],
    })


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def _prewarm_dashboard_payload() -> None:
    """Materialize the dashboard while the proxy is finishing startup."""
    try:
        dashboard_service.build_payload()
    except Exception as exc:  # pragma: no cover - best effort startup work
        print(f"Dashboard prewarm skipped: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    Thread(target=_prewarm_dashboard_payload, name="dashboard-prewarm", daemon=True).start()
    print("Starting Excel proxy on http://127.0.0.1:8000 (loopback only)", flush=True)
    print("  Sign in to the ChatGPT Excel add-in, then open the dashboard.", flush=True)
    print("  Responses API: POST /v1/responses", flush=True)
    print("  Compaction:    POST /v1/responses/compact", flush=True)
    _write_proxy_pid_file()
    atexit.register(_remove_proxy_pid_file)
    try:
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000, proxy_headers=False, access_log=False, timeout_graceful_shutdown=2))
        shutdown_context = nullcontext()
        if sys.platform == "win32":
            from windows_launcher import shutdown_listener
            shutdown_context = shutdown_listener(server)
        with shutdown_context:
            server.run()
    finally:
        revert_client_proxy_configs_on_shutdown()
        _remove_proxy_pid_file()
