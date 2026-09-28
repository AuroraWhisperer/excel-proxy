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
import account_balances
import account_login
import proxy_accounts
import atexit
import background_proxy
import codex_agent_compat
import dashboard as dashboard_module
from excel_responses import ExcelResponseProcessor
import excel_image_generation
import excel_images
import excel_session_capture
import excel_upstream
import responses_protocol
import hashlib
import migrate_runtime_paths
import json
import tempfile
import time
import request_trace_storage
import upstream_errors
import usage_tracking
import util
from contextlib import nullcontext
from threading import Lock, Thread
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from local_access import LocalAccessMiddleware
from upstream_request import UpstreamRequestPlan
from upstream_client import (
    get_excel_upstream_client as _get_excel_upstream_client,
)
from usage_storage import create_usage_archive_store
from responses_stream import (
    StreamDependencies,
    GracefulStreamingResponse,
    relay_streaming_response,
)
from request_diagnostics import (
    header_trace_subset as _header_trace_subset,
    trace_hash as _trace_hash,
    request_reasoning_effort as _request_reasoning_effort,
    trace_body_summary as _trace_body_summary,
    effective_trace_usage as _effective_trace_usage,
    trace_response_summary as _trace_response_summary,
    trim_trace_field as _trim_trace_field,
    trim_trace_text as _trim_trace_text,
    extract_prompt_preview as _extract_prompt_preview,
)
from config_routes import create_config_router
from account_routes import AccountRouteDependencies, create_account_router
from dashboard_routes import create_dashboard_router
from codex_config import (
    ProxyClientConfig,
    ProxyClientConfigService,
)

from constants import (
    CLIENT_PROXY_SETTINGS_FILE,
    CODEX_PRIMARY_CONFIG_FILE,
    CODEX_MANAGED_CONFIG_FILE,
    CODEX_PROXY_MODEL_CATALOG_FILE,
    CODEX_PROXY_CONFIG,
    CODEX_PROXY_MODEL_CONTEXT_WINDOW,
    CODEX_PROXY_MODEL_AUTO_COMPACT_TOKEN_LIMIT,
    PROXY_PID_FILE,
    REQUEST_TRACE_LOG_FILE,
    REQUEST_PROMPT_ARCHIVE_DIR,
)

from rate_limiting import throttled_client_send


# ─── App & Global State ──────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(LocalAccessMiddleware)
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
    print(
        f"runtime migration: copied {len(migrated_runtime_files)} legacy file(s)",
        flush=True,
    )
DEBUG_DETAIL_CONTEXT_REQUESTS = 10


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
            fd, temp_path = tempfile.mkstemp(
                prefix="request-prompt-", suffix=".tmp", dir=archive_dir
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        record, separators=(",", ":"), default=util._json_default
                    )
                )
            os.replace(temp_path, archive_path)
    except OSError as exc:
        with _REQUEST_PROMPT_LOCK:
            _REQUEST_PROMPT_ACTIVE_IDS.discard(str(request_id))
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        print(
            f"Warning: failed to write request prompt archive: {exc}",
            file=sys.stderr,
            flush=True,
        )


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
        "request_id": payload.get("request_id")
        if isinstance(payload.get("request_id"), str)
        else request_id,
        "path": payload.get("path") if isinstance(payload.get("path"), str) else None,
        "stored_at": payload.get("stored_at")
        if isinstance(payload.get("stored_at"), str)
        else None,
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
        if not usage_tracker.state.history_loaded:
            return
        now = time.monotonic()
        with _REQUEST_PROMPT_LOCK:
            if (
                now - _REQUEST_PROMPT_LAST_PRUNED_MONOTONIC
                < _REQUEST_PROMPT_PRUNE_INTERVAL_SECONDS
            ):
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
                if not entry.startswith(
                    _REQUEST_PROMPT_FILE_PREFIX
                ) or not entry.endswith(".json"):
                    continue
                if entry in keep_files:
                    continue
                try:
                    os.unlink(os.path.join(archive_dir, entry))
                except OSError:
                    continue
    except OSError as exc:
        print(
            f"Warning: failed to prune request prompt archive: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _handle_usage_event_recorded(event: dict | None) -> None:
    request_id = event.get("request_id") if isinstance(event, dict) else None
    if isinstance(request_id, str) and request_id:
        with _REQUEST_PROMPT_LOCK:
            _REQUEST_PROMPT_ACTIVE_IDS.discard(request_id)
    _prune_request_prompt_archive()
    dashboard_service.notify_dashboard_stream_listeners()


usage_tracker = usage_tracking.UsageTracker(
    state=usage_tracking.UsageTrackingState(),
    archive_store=create_usage_archive_store(),
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
    return bool(
        _debug_prompt_logging_settings().get("debug_prompt_logging_enabled", False)
    )


def _prompt_logging_permitted() -> bool:
    return _debug_prompt_logging_enabled()


def _prompt_trace_value(value):
    return value


def _prompt_payload_for_dashboard(value):
    return value


def _client_proxy_settings_with_trace_status(
    payload: dict[str, object],
) -> dict[str, object]:
    return dict(payload)


def _save_client_proxy_settings(payload: dict) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    result = client_proxy_config_service.save_client_proxy_settings(
        {
            "revert_on_shutdown": bool(payload.get("revert_on_shutdown", True)),
            "debug_prompt_logging_enabled": bool(
                payload.get("debug_prompt_logging_enabled", False)
            ),
        }
    )
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
    ),
    utc_now=util.utc_now,
)


async def parse_json_request(request: Request) -> dict:
    return await util.parse_json_request(
        request, error_callback=usage_tracker.record_request_error
    )


def _write_proxy_pid_file() -> None:
    try:
        os.makedirs(os.path.dirname(PROXY_PID_FILE), exist_ok=True)
        with open(PROXY_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
            f.write("\n")
    except OSError as exc:
        print(
            f"Warning: failed to write proxy pid file: {exc}",
            file=sys.stderr,
            flush=True,
        )


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


usage_tracker.load_history(limit=20)
_usage_history_task: asyncio.Task | None = None


def _load_remaining_usage_history():
    try:
        usage_tracker.load_archived_history()
        usage_tracker.load_history()
    except Exception as exc:
        print(
            f"Warning: failed to restore usage history: {exc}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        dashboard_service.notify_dashboard_stream_listeners()


@app.on_event("startup")
async def _app_startup_load_usage_history():
    global _usage_history_task
    if _usage_history_task is None:
        _usage_history_task = asyncio.create_task(
            asyncio.to_thread(_load_remaining_usage_history)
        )


@app.on_event("shutdown")
async def _app_shutdown_wait_for_usage_history():
    if _usage_history_task is not None:
        await _usage_history_task


@app.on_event("startup")
async def _app_startup_restore_client_proxy_configs():
    excel_upstream.excel_session_store.load()
    try:
        legacy_source = (
            proxy_accounts.proxy_account_store.snapshot()["source"] == "excel"
        )
    except account_balances.BalanceError:
        legacy_source = (
            False  # A damaged account file must never select another identity.
        )
    if legacy_source:
        asyncio.create_task(
            asyncio.to_thread(
                excel_session_capture.refresh_macos_excel_session,
                excel_upstream.excel_session_store,
                force=True,
            )
        )
        asyncio.create_task(
            asyncio.to_thread(
                excel_session_capture.refresh_windows_excel_session,
                excel_upstream.excel_session_store,
                force=True,
            )
        )
    restore_client_proxy_configs_on_startup()
    client_proxy_config_service.refresh_client_model_metadata()


@app.on_event("shutdown")
async def _app_shutdown_revert_client_proxy_configs():
    await asyncio.to_thread(account_login.login_service.close)
    await asyncio.to_thread(proxy_login_service.close)
    revert_client_proxy_configs_on_shutdown()


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
        print(
            f"Client proxy startup restore: {json.dumps(result, default=str)}",
            flush=True,
        )
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
        print(
            f"Client proxy shutdown revert: {json.dumps(result, default=str)}",
            flush=True,
        )
    return result


def _header_value_case_insensitive(headers: dict | None, name: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    target = str(name).lower()
    for key, value in headers.items():
        if (
            isinstance(key, str)
            and key.lower() == target
            and isinstance(value, str)
            and value.strip()
        ):
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
    initiator = (
        str(_header_value_case_insensitive(outbound_headers, "x-initiator") or "")
        .strip()
        .lower()
    )
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
    if "user_initiated" in _debug_detail_always_capture_reasons(
        plan.headers, plan.trace_context
    ):
        return True
    return False


def _append_request_trace(payload: dict, *, force: bool = False) -> None:
    if not force and not (request_tracing_enabled() or _debug_prompt_logging_enabled()):
        return
    trace_path = request_trace_log_path()
    try:
        line = (
            json.dumps(payload, separators=(",", ":"), default=util._json_default)
            + "\n"
        )
        request_trace_storage.submit_trace_line(trace_path, line)
    except Exception as exc:
        print(
            f"Warning: failed to schedule request trace log write: {exc}",
            file=sys.stderr,
            flush=True,
        )


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
            "outbound_headers": dict(outbound_headers)
            if isinstance(outbound_headers, dict)
            else None,
            "request_body": _prompt_trace_value(request_body),
            "upstream_body": _prompt_trace_value(upstream_body),
        }
        if upstream_wire_bytes is not None:
            snapshot["upstream_body_wire"] = _prompt_trace_value(
                upstream_wire_bytes.decode("utf-8", errors="replace")
            )
            snapshot["upstream_body_wire_size"] = len(upstream_wire_bytes)
            snapshot["upstream_body_wire_sha256"] = hashlib.sha256(
                upstream_wire_bytes
            ).hexdigest()
        safe_rid = (
            "".join(ch for ch in str(request_id) if ch.isalnum() or ch in ("-", "_"))
            or "request"
        )
        out_path = os.path.join(dump_dir, f"{safe_rid}.json")
        request_trace_storage.submit_body_dump(out_path, dump_dir, snapshot)
    except Exception as exc:  # pragma: no cover - never let dump errors fail upstream
        print(
            f"Warning: failed to schedule request body dump: {exc}",
            file=sys.stderr,
            flush=True,
        )


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
    debug_detail_capture = None
    if _debug_prompt_logging_enabled():
        debug_detail_capture = _debug_detail_capture_info(
            reasons=["debug_prompt_logging"], phase="current"
        )
        context["debug_detail_capture"] = debug_detail_capture
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
            payload["source_body"] = _prompt_trace_value(
                _trim_trace_field(request_body)
            )
        if isinstance(upstream_body, dict):
            payload["upstream_body"] = _prompt_trace_value(
                _trim_trace_field(upstream_body)
            )
    if initiator_verdict is not None:
        payload["initiator_verdict"] = initiator_verdict
        context["initiator_verdict"] = initiator_verdict
    _append_request_trace(payload)
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
    return context


def _should_force_failure_trace(
    plan: UpstreamRequestPlan | None, status_code: int
) -> bool:
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
    error: Exception | None = None,
) -> None:
    if isinstance(plan, UpstreamRequestPlan):
        if not isinstance(plan.trace_context, dict):
            plan.trace_context = {}
        diagnostics = plan.trace_context.get("tool_call_diagnostics")
        if isinstance(diagnostics, dict) and isinstance(plan.usage_event, dict):
            plan.usage_event["tool_call_diagnostics"] = diagnostics
        recovery = plan.trace_context.get("tool_call_recovery")
        if isinstance(recovery, dict):
            if isinstance(plan.usage_event, dict):
                plan.usage_event["tool_call_recovery"] = recovery
            if isinstance(recovery.get("usage"), dict):
                usage = recovery["usage"]
        if status_code >= 400:
            payload_error = (
                response_payload.get("error")
                if isinstance(response_payload, dict)
                else None
            )
            lifecycle = plan.trace_context.get("responses_stream_lifecycle") or {}
            code = (
                error.code
                if isinstance(error, upstream_errors.ExcelResponseError)
                else payload_error.get("code")
                if isinstance(payload_error, dict)
                else lifecycle.get("upstream_error_code")
            )
            diagnosis = upstream_errors.diagnose_failure(
                status_code,
                code=code,
                error_type=type(error).__name__
                if error is not None
                else lifecycle.get("upstream_error_type"),
            )
            plan.trace_context["failure_diagnosis"] = diagnosis
            if isinstance(plan.usage_event, dict):
                plan.usage_event["failure_diagnosis"] = diagnosis
        usage_tracker.finish_event(
            plan.usage_event,
            status_code,
            upstream=upstream,
            response_payload=response_payload,
            response_text=response_text,
            reasoning_text=reasoning_text,
            usage=usage,
        )
        effective_usage = _effective_trace_usage(
            response_payload=response_payload, usage=usage
        )
        force_trace = _should_force_failure_trace(plan, status_code)
        if request_tracing_enabled() or _debug_prompt_logging_enabled() or force_trace:
            trace_context = dict(plan.trace_context or {"request_id": plan.request_id})
            error_details = None
            if status_code >= 400 and _plan_allows_full_debug_detail(plan):
                error_details = upstream_errors.sanitized_error_details(
                    response_payload,
                    secrets=tuple(
                        value
                        for key, value in plan.headers.items()
                        if key.lower() in {"authorization", "cookie", "x-api-key"}
                    ),
                )
            trace_payload = {
                "event": "request_finished",
                "time": util.utc_now_iso(),
                **trace_context,
                "requested_model": plan.requested_model,
                "resolved_model": plan.resolved_model,
                "response": _trace_response_summary(
                    upstream=upstream,
                    response_payload=error_details
                    if status_code >= 400
                    else response_payload,
                    usage=effective_usage,
                    status_code=status_code,
                ),
                "response_text_present": isinstance(response_text, str)
                and bool(response_text),
                "reasoning_text_present": isinstance(reasoning_text, str)
                and bool(reasoning_text),
            }
            if status_code < 400 and isinstance(reasoning_text, str) and reasoning_text:
                trace_payload["reasoning_text"] = _trim_trace_text(reasoning_text)
            if status_code >= 400:
                if _plan_allows_full_debug_detail(plan):
                    trace_payload["source_body"] = _prompt_trace_value(
                        _trim_trace_field(
                            plan.source_body
                            if isinstance(plan.source_body, dict)
                            else plan.body
                        )
                    )
                    trace_payload["upstream_body"] = _prompt_trace_value(
                        _trim_trace_field(plan.body)
                    )
                else:
                    trace_payload["source_body"] = _trace_body_summary(
                        plan.source_body
                        if isinstance(plan.source_body, dict)
                        else plan.body
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
    always_capture_reasons = _debug_detail_always_capture_reasons(
        headers, trace_metadata
    )
    prompt_preview = None
    stored_prompt_preview = None
    if always_capture_reasons:
        prompt_preview = _extract_prompt_preview(
            source_body if isinstance(source_body, dict) else body,
            truncate=False,
        )
        stored_prompt_preview = (
            _prompt_trace_value(prompt_preview) if prompt_preview else None
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
        initiator_verdict=initiator_verdict
        if isinstance(initiator_verdict, dict)
        else None,
    )
    quota_account_key = account_balances.account_key_from_headers(headers)
    if quota_account_key:
        usage_event["quota_account_key"] = quota_account_key
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
        ),
        None,
    )


def _excel_response_processor() -> ExcelResponseProcessor:
    """Bind request processing to the application's current runtime services."""
    return ExcelResponseProcessor(
        usage_tracker=usage_tracker,
        get_upstream_client=_get_excel_upstream_client,
        finish_usage_and_trace=_finish_usage_and_trace,
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
    stream_transform_factory=None,
    upstream_client: httpx.AsyncClient | None = None,
) -> Response:
    """Bind application services to the Responses stream lifecycle owner."""
    return await relay_streaming_response(
        upstream_url,
        headers,
        body,
        timeout=timeout,
        usage_event=usage_event,
        stream_type=stream_type,
        trace_plan=trace_plan,
        downstream_request=downstream_request,
        stream_transform=stream_transform,
        trace_details_factory=trace_details_factory,
        stream_transform_factory=stream_transform_factory,
        upstream_client=upstream_client,
        dependencies=StreamDependencies(usage_tracker, _finish_usage_and_trace),
        get_upstream_client=_get_excel_upstream_client,
        handle_upstream_error=_excel_response_processor().handle_upstream_error,
    )


# ─── Dashboard routes ─────────────────────────────────────────────────────────


def _load_request_prompt_payload(request_id: str) -> dict:
    if not isinstance(request_id, str) or not request_id:
        return {"available": False}
    _prune_request_prompt_archive()
    target = None
    for event in reversed(usage_tracker.snapshot_usage_events()):
        if isinstance(event, dict) and any(
            event.get(key) == request_id
            for key in ("request_id", "client_request_id", "server_request_id")
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
        if (
            isinstance(target_request_id, str)
            and target_request_id
            and target_request_id not in archive_ids
        ):
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


# ─── Config API routes ────────────────────────────────────────────────────────


# ─── Route: /v1/responses  (Codex / Responses API) ───────────────────────────


proxy_login_service = account_login.AccountLoginService(
    proxy_accounts.proxy_account_store, offline_access=True
)
_proxy_activation_lock = asyncio.Lock()


async def _selected_excel_headers(*, stream=False):
    selection = await asyncio.to_thread(proxy_accounts.proxy_account_store.snapshot)
    if selection["source"] == "oauth":
        return await asyncio.to_thread(
            proxy_accounts.proxy_account_store.headers_for,
            selection["active_id"],
            stream=stream,
            active=True,
        )
    if selection["source"] == "none":
        raise account_balances.BalanceError(
            "未启用代理账号，请在首页登录或选择已保存的账号。", 401
        )
    for refresh in (
        excel_session_capture.refresh_macos_excel_session,
        excel_session_capture.refresh_windows_excel_session,
    ):
        await asyncio.to_thread(refresh, excel_upstream.excel_session_store, force=True)
    return excel_upstream.excel_session_store.request_headers(stream=stream)


_EXCEL_CONNECTION_TEST_TIMEOUT_SECONDS = 30
_excel_connection_test_lock = asyncio.Lock()


async def _run_excel_connection_test(
    request: Request, model: str, *, session_headers=None
):
    response_headers = {"Cache-Control": "no-store"}
    if _excel_connection_test_lock.locked():
        return JSONResponse(
            status_code=429,
            headers=response_headers,
            content={
                "ok": False,
                "model": model,
                "category": "busy",
                "message": "An Excel connection test is already running.",
            },
        )
    started = time.monotonic()
    async with _excel_connection_test_lock:
        try:
            async with asyncio.timeout(_EXCEL_CONNECTION_TEST_TIMEOUT_SECONDS):
                header_args = (
                    {"session_headers": session_headers}
                    if session_headers is not None
                    else {}
                )
                response = await _handle_excel_responses(
                    request,
                    {
                        "model": model,
                        "stream": False,
                        "input": "Reply with exactly OK.",
                        "tool_choice": "none",
                        "reasoning": {"effort": "medium"},
                    },
                    **header_args,
                )
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                headers=response_headers,
                content={
                    "ok": False,
                    "model": model,
                    "category": "timeout",
                    "message": "The Excel connection test timed out. Check the connection and try again.",
                },
            )
    result = json.loads(response.body)
    text = responses_protocol.extract_response_output_text(result) or ""
    ok = (
        response.status_code == 200
        and result.get("status") == "completed"
        and bool(text.strip())
    )
    error = result.get("error") or {}
    code = error.get("code") if isinstance(error, dict) else None
    category = {
        401: "authentication",
        403: "access",
        429: "rate_limit",
        504: "timeout",
        400: "request",
        404: "request",
        422: "request",
    }.get(
        response.status_code,
        "upstream" if code == "excel_upstream_error" else "protocol",
    )
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
        "ok": ok,
        "model": model,
        "category": "success" if ok else category,
        "message": "The selected model returned a completed text response."
        if ok
        else messages[category],
        "status_code": response.status_code,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    if ok:
        content["response_text"] = text.strip()[:500]
    if response.headers.get("x-request-id"):
        content["request_id"] = response.headers["x-request-id"]
    status_code = (
        200 if ok else response.status_code if response.status_code >= 400 else 502
    )
    return JSONResponse(
        status_code=status_code, content=content, headers=response_headers
    )


async def _handle_excel_responses(
    request: Request,
    body: dict,
    *,
    source_body: dict | None = None,
    session_headers: dict | None = None,
) -> Response:
    try:
        excel_headers = (
            dict(session_headers)
            if session_headers is not None
            else await _selected_excel_headers(stream=bool(body.get("stream")))
        )
    except account_balances.BalanceError as exc:
        return responses_protocol.openai_error_response(exc.status_code, str(exc))
    except RuntimeError as exc:
        return responses_protocol.openai_error_response(401, str(exc))
    for attempt in range(2):
        response = await _send_excel_responses(
            request,
            body,
            source_body=source_body,
            excel_headers=excel_headers,
        )
        # A 401 before downstream streaming is an explicit rejection, so
        # one same-account renewal cannot duplicate accepted model work.
        # Candidate login probes must keep their exact tested credential.
        if (
            not getattr(response, "_excel_auth_rejected", False)
            or attempt
            or session_headers is not None
        ):
            return response
        try:
            renewed = await asyncio.to_thread(
                proxy_accounts.proxy_account_store.refresh_after_unauthorized,
                excel_headers,
            )
        except account_balances.BalanceError as exc:
            return responses_protocol.openai_error_response(exc.status_code, str(exc))
        if renewed is None:
            return response
        excel_headers = renewed


async def _send_excel_responses(
    request: Request,
    body: dict,
    *,
    source_body: dict | None,
    excel_headers: dict,
) -> Response:
    excel_model_id = (
        excel_upstream.excel_model_id(body.get("model")) or excel_upstream.MODEL_ID
    )
    try:
        upstream_body = excel_upstream.prepare_responses_body(
            body,
            tools_version_id=excel_upstream.excel_session_store.tools_version_id(),
        )
    except ValueError as exc:
        return responses_protocol.openai_error_response(
            400, str(exc), param=getattr(exc, "param", "input")
        )

    client = _get_excel_upstream_client()
    for attempt in range(2):
        try:
            # Preserve task/turn identity from the original image data, even
            # when an expired attachment needs to be uploaded with a new ID.
            image_body, reused_images = await excel_images.image_uploads.rewrite(
                upstream_body,
                client,
                excel_headers,
            )
        except ValueError as exc:
            return responses_protocol.openai_error_response(
                400, str(exc), param="input"
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            response = responses_protocol.openai_error_response(
                status if status >= 400 else 502,
                f"Excel image upload failed (HTTP {status}). Retry the request.",
                param="input",
            )
            response._excel_auth_rejected = status == 401
            return response
        except httpx.RequestError as exc:
            status, message = (
                responses_protocol.upstream_request_error_status_and_message(exc)
            )
            return responses_protocol.openai_error_response(
                status,
                f"Excel image upload failed: {message}.",
                param="input",
            )

        plan, error_response = _prepare_upstream_request(
            request,
            body=image_body,
            requested_model=excel_model_id,
            resolved_model=excel_model_id,
            upstream_path="/basispoints/api/responses",
            upstream_url=excel_upstream.RESPONSES_URL,
            header_builder=lambda _api_key, _request_id: dict(excel_headers),
            error_response=responses_protocol.openai_error_response,
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
        response_processor = _excel_response_processor()
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
                stream_transform_factory=lambda upstream: (
                    response_processor.tool_stream_transform(
                        body,
                        trace_plan=plan,
                        upstream=upstream,
                    )
                ),
                upstream_client=client,
            )
        else:
            response = await response_processor.post_non_streaming_request(
                plan, client_body=body
            )
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
        raise HTTPException(
            status_code=400, detail="Unsupported model. Select a model from /v1/models."
        )
    return {**body, "model": model}


async def _handle_excel_image_request(request: Request, *, edit: bool) -> Response:
    if "application/json" not in request.headers.get("content-type", "").lower():
        return responses_protocol.openai_error_response(
            415,
            "Use a JSON image request; edits take images as inline image_url data URLs.",
        )
    try:
        body = await request.json()
        send = excel_image_generation.prepare_request(body, edit=edit)
    except (ValueError, UnicodeDecodeError) as exc:
        return responses_protocol.openai_error_response(400, str(exc))
    try:
        session_headers = await _selected_excel_headers(stream=False)
    except account_balances.BalanceError as exc:
        return responses_protocol.openai_error_response(exc.status_code, str(exc))
    except RuntimeError:
        return responses_protocol.openai_error_response(
            401, "Refresh the signed-in ChatGPT Excel add-in session."
        )
    headers = {
        key: value
        for key, value in session_headers.items()
        if key.lower() not in {"content-type", "content-length", "accept"}
    }
    headers["accept"] = "application/json"
    url = (
        excel_image_generation.EDITS_URL
        if edit
        else excel_image_generation.GENERATIONS_URL
    )
    client = _get_excel_upstream_client()
    try:
        upstream_request = client.build_request(
            "POST",
            url,
            headers=headers,
            timeout=httpx.Timeout(600.0, connect=30.0),
            **send,
        )
        upstream = await throttled_client_send(
            client, upstream_request, follow_redirects=False
        )
    except httpx.RequestError as exc:
        status, message = responses_protocol.upstream_request_error_status_and_message(
            exc
        )
        return responses_protocol.openai_error_response(status, message)
    if upstream.status_code >= 300:
        status = upstream.status_code if upstream.status_code >= 400 else 502
        return JSONResponse(
            status_code=status, content=upstream_errors.excel_error_payload(status)
        )
    try:
        payload = excel_image_generation.validate_response(upstream.json())
    except (ValueError, UnicodeDecodeError):
        return responses_protocol.openai_error_response(
            502, "Excel returned an invalid image response."
        )
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
        return responses_protocol.openai_error_response(
            exc.status_code,
            responses_protocol.http_exception_detail_to_message(exc.detail),
            param="model"
            if exc.status_code == 400
            and str(exc.detail).startswith("Unsupported model")
            else None,
        )
    body = codex_agent_compat.normalize_codex_agent_tools(body)
    return await _handle_excel_responses(request, body)


@app.post("/responses/compact")
@app.post("/v1/responses/compact")
async def responses_compact(request: Request):
    try:
        body = await parse_json_request(request)
    except HTTPException as exc:
        return responses_protocol.openai_error_response(
            exc.status_code,
            responses_protocol.http_exception_detail_to_message(exc.detail),
        )

    try:
        body = _excel_request_body(body)
    except HTTPException as exc:
        return responses_protocol.openai_error_response(
            exc.status_code, str(exc.detail), param="model"
        )
    summary_request = responses_protocol.build_fake_compaction_request(body)
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
    if payload.get(
        "status"
    ) != "completed" or not responses_protocol.extract_response_output_text(payload):
        return responses_protocol.openai_error_response(
            502,
            "Excel compaction did not produce a complete summary; keep the existing history",
        )
    compacted = responses_protocol.responses_to_compaction_response(
        payload, fallback_model=body.get("model")
    )
    source_input = body.get("input", [])
    if isinstance(source_input, str):
        source_input = [{"type": "message", "role": "user", "content": source_input}]
    elif not isinstance(source_input, list):
        source_input = []
    # Codex can replace its entire context with compact.output. Preserve
    # the user's instructions alongside the summary in that handoff.
    retained = [
        item
        for item in source_input
        if isinstance(item, dict)
        and item.get("role") in {"system", "developer", "user"}
        and item.get("type") in (None, "message")
    ]
    return JSONResponse(
        content={
            "id": compacted["id"],
            "object": "response.compaction",
            "created_at": compacted.get("created_at") or int(time.time()),
            "output": retained + compacted["output"],
            "usage": compacted["usage"],
        }
    )


# ─── Excel model catalog ────────────────────────────────────────────────────


@app.get("/models")
@app.get("/v1/models")
async def models():
    return JSONResponse(
        content={
            "object": "list",
            "data": [
                excel_upstream.local_model_payload(model)
                for model in excel_upstream.MODEL_IDS
            ],
        }
    )


# ─── Entrypoint ───────────────────────────────────────────────────────────────


@app.get("/api/request-prompt/{request_id}")
async def request_prompt_api(request_id: str):
    payload = await asyncio.to_thread(_load_request_prompt_payload, request_id)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


account_route_dependencies = AccountRouteDependencies(
    usage_tracker=usage_tracker,
    proxy_login_service=proxy_login_service,
    client_proxy_config_service=client_proxy_config_service,
    activation_lock=_proxy_activation_lock,
    parse_json_request=parse_json_request,
    dispatch_response=_handle_excel_responses,
    run_connection_test=_run_excel_connection_test,
)
app.include_router(create_account_router(account_route_dependencies))

app.include_router(
    create_dashboard_router(
        dashboard_service=dashboard_service,
        streaming_response_class=GracefulStreamingResponse,
    )
)
app.include_router(
    create_config_router(
        client_proxy_config_service=client_proxy_config_service,
        background_proxy_manager=background_proxy_manager,
        save_settings=_save_client_proxy_settings,
        decorate_settings=_client_proxy_settings_with_trace_status,
        parse_json_request=parse_json_request,
    )
)


def _prewarm_dashboard_payload() -> None:
    """Materialize the dashboard while the proxy is finishing startup."""
    try:
        dashboard_service.build_payload()
    except Exception as exc:  # pragma: no cover - best effort startup work
        print(f"Dashboard prewarm skipped: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    Thread(
        target=_prewarm_dashboard_payload, name="dashboard-prewarm", daemon=True
    ).start()
    print("Starting Excel proxy on http://127.0.0.1:8000 (loopback only)", flush=True)
    print("  Sign in to the ChatGPT Excel add-in, then open the dashboard.", flush=True)
    print("  Responses API: POST /v1/responses", flush=True)
    print("  Compaction:    POST /v1/responses/compact", flush=True)
    _write_proxy_pid_file()
    atexit.register(_remove_proxy_pid_file)
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=8000,
                proxy_headers=False,
                access_log=False,
                timeout_graceful_shutdown=2,
            )
        )
        shutdown_context = nullcontext()
        if sys.platform == "win32":
            from windows_launcher import shutdown_listener

            shutdown_context = shutdown_listener(server)
        with shutdown_context:
            server.run()
    finally:
        revert_client_proxy_configs_on_shutdown()
        _remove_proxy_pid_file()
