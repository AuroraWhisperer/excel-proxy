"""Responses stream setup, registration, cancellation and one-time finalization."""

import asyncio
from dataclasses import dataclass, field
import os
import threading
from typing import Callable
from urllib.parse import urlsplit

from anyio import CancelScope
import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.requests import ClientDisconnect

import responses_protocol
import upstream_errors
from excel_stream import (
    completed_response as _excel_completed_response,
    extract_upstream_json_payload as _extract_upstream_json_payload,
)
from rate_limiting import throttled_client_send
from request_diagnostics import trace_hash as _trace_hash
from upstream_request import UpstreamRequestPlan
from usage_tracking import UsageTracker


@dataclass
class StreamDependencies:
    usage_tracker: UsageTracker
    finish_usage_and_trace: Callable[..., None]


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
    if responses_protocol.input_contains_compaction(plan.body.get("input")):
        return False
    upstream_path = str(trace_context.get("upstream_path") or "").strip()
    if not upstream_path:
        upstream_path = urlsplit(plan.upstream_url).path
    return upstream_path.rstrip("/").lower().endswith("/responses")


def _responses_active_stream_identity(
    plan: "UpstreamRequestPlan | None",
) -> tuple[str, str] | None:
    if not _responses_plan_uses_native_upstream(plan) or not isinstance(
        plan, UpstreamRequestPlan
    ):
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
        hasattr(connection, attr) for attr in ("_h2_state", "_write_outgoing_data")
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
    if cancel_generation and http_version in {
        b"HTTP/1.0",
        b"HTTP/1.1",
        "HTTP/1.0",
        "HTTP/1.1",
    }:
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
            and (current_entry is None or entry.sequence < current_entry.sequence)
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
            request_cancel = getattr(
                entry.stream_body, "request_transport_cancel", None
            )
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


async def _excel_stream_response_bytes(upstream: httpx.Response):
    """Normalize a buffered JSON response before the common SSE validator."""
    if "application/json" not in upstream.headers.get("content-type", "").lower():
        async for chunk in upstream.aiter_bytes():
            yield chunk
        return
    await upstream.aread()
    response = _extract_upstream_json_payload(upstream)
    if not isinstance(response, dict) or response.get("status") not in (
        None,
        "completed",
        "failed",
        "incomplete",
    ):
        raise upstream_errors.ExcelResponseError(
            "excel_invalid_response", "Excel returned an invalid JSON response"
        )
    event = "response." + (response.get("status") or "completed")
    if event == "response.completed":
        response = _excel_completed_response(response, {})
        yield responses_protocol.sse_encode(
            "response.created",
            {
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            },
        )
        for index, item in enumerate(response["output"]):
            if item.get("type") != "message":
                continue  # Native calls are dispatched only after whole-batch validation.
            for chunk in responses_protocol.response_message_events(item, index):
                yield chunk
    yield responses_protocol.sse_encode(event, {"type": event, "response": response})


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
        dependencies: StreamDependencies,
        stream_transform=None,
        trace_details_factory=None,
    ):
        self.dependencies = dependencies
        self.upstream = upstream
        self.usage_event = usage_event
        self.stream_type = stream_type
        self.trace_plan = trace_plan
        self.active_stream = active_stream
        self.trace_details_factory = trace_details_factory
        self._stream_transform_enabled = callable(stream_transform)
        self.capture = self.dependencies.usage_tracker.create_sse_capture(stream_type)
        self.presentation_capture = self.dependencies.usage_tracker.create_sse_capture(
            stream_type
        )
        self.source_loop_completed = False
        self.presentation_loop_completed = False
        self._source_task: asyncio.Task | None = None
        self._finalizing = False
        self._finalized = False
        self._finalized_event = asyncio.Event()
        self._preemptive_transport_cancel: str | None = None
        self._transport_cancel_attempt: str | None = None
        self._transport_cancel_task: asyncio.Task | None = None

        raw_source_iter = (
            _excel_stream_response_bytes(upstream)
            if stream_type == "responses"
            else upstream.aiter_bytes()
        )

        async def capture_source():
            async for chunk in raw_source_iter:
                if self.capture.feed(chunk):
                    self.dependencies.usage_tracker.mark_first_output(self.usage_event)
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
                if (
                    not self.capture.terminal_event_seen
                    and not self.presentation_capture.terminal_event_seen
                ):
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
            return responses_protocol.sse_encode(
                "response.failed",
                {
                    "type": "response.failed",
                    "response": {
                        "status": "failed",
                        "output": [],
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            "type": "server_error"
                            if exc.status_code >= 500
                            else "invalid_request_error",
                            "diagnosis": upstream_errors.diagnose_failure(
                                exc.status_code, code=exc.code
                            ),
                        },
                    },
                },
            )
        except Exception as exc:
            await self._finalize("upstream_error", error=exc)
            raise
        finally:
            if self._source_task is source_task:
                self._source_task = None

        # Clients may close as soon as the terminal frame is yielded, before
        # another __anext__ call can mark the presentation loop exhausted.
        self.presentation_capture.feed(chunk)
        return chunk

    async def aclose(self) -> None:
        try:
            if (
                not self._finalized
                and not self.capture.terminal_event_seen
                and not self.presentation_capture.terminal_event_seen
            ):
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
                asyncio.create_task(self._confirm_transport_cancel_after_finalize(mode))
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
            presentation_completed = self.presentation_capture.completed_event_seen
            presentation_terminal = self.presentation_capture.terminal_event_seen
            terminal_eof = (
                self.capture.terminal_event_seen
                and self.source_loop_completed
                and cause == "source_eof"
            )
            generation_ended = (
                completed or self.capture.terminal_event_seen or presentation_terminal
            )
            if (
                self._stream_transform_enabled
                and cause in {"downstream_cancelled", "downstream_closed"}
                and not self.presentation_loop_completed
                and not presentation_terminal
            ):
                trace_status = 499
            elif (
                cause in {"upstream_error", "response_validation_error"}
                and self._stream_transform_enabled
            ):
                if isinstance(error, upstream_errors.ExcelResponseError):
                    trace_status = error.status_code
                elif isinstance(error, httpx.RequestError):
                    trace_status, _message = (
                        responses_protocol.upstream_request_error_status_and_message(
                            error
                        )
                    )
                else:
                    trace_status = 502
            elif completed or presentation_completed:
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
                trace_status, _message = (
                    responses_protocol.upstream_request_error_status_and_message(error)
                )
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
                        self._transport_cancel_attempt or "cancel_transport_unconfirmed"
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
                "presentation_terminal_event_type": self.presentation_capture.terminal_event_type,
                "presentation_completed_event_seen": presentation_completed,
                "superseded_by": (
                    self.active_stream.superseded_by
                    if self.active_stream is not None
                    else None
                ),
                "transport_close": transport_close,
                "transport_cancel_confirmed": transport_cancel_confirmed,
                "teardown_confirmed": teardown_confirmed,
                "upstream_error_type": type(error).__name__
                if error is not None
                else None,
                "upstream_error_code": error.code
                if isinstance(error, upstream_errors.ExcelResponseError)
                else None,
                "upstream_error_message": str(error)
                if isinstance(error, upstream_errors.ExcelResponseError)
                else None,
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
            if isinstance(self.trace_plan, UpstreamRequestPlan) and isinstance(
                self.trace_plan.trace_context, dict
            ):
                self.trace_plan.trace_context["responses_stream_lifecycle"] = lifecycle

            try:
                captured_usage = (
                    self.capture.usage if isinstance(self.capture.usage, dict) else None
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
                self.dependencies.finish_usage_and_trace(
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
                    error=error,
                )
            finally:
                _complete_active_responses_teardown(
                    self.active_stream,
                    transport_cancel=transport_close,
                    confirmed=teardown_confirmed,
                    completed=completed or presentation_completed,
                )
                self._finalized = True
                self._finalizing = False
                self._finalized_event.set()


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
            raise _DownstreamDisconnectedBeforeResponse("pre_response_request_error")
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


async def relay_streaming_response(
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
    *,
    dependencies: StreamDependencies,
    get_upstream_client: Callable[[], httpx.AsyncClient],
    handle_upstream_error: Callable[..., Response],
) -> Response:
    """
    Relay an upstream SSE response while preserving upstream error statuses.

    If the upstream request fails before the stream starts, return the upstream
    error body as a normal HTTP response instead of masking it as 200 SSE.
    """
    active_stream = _register_active_responses_stream(trace_plan)
    try:
        await _supersede_active_responses_streams(trace_plan, active_stream)
        client = upstream_client or get_upstream_client()
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
    except _ResponsesSupersessionBlocked as exc:
        status_code = 409
        message = (
            "The previous same-lineage generation could not be confirmed stopped; "
            "this follow-up was not sent upstream to prevent duplicate token spend."
        )
        try:
            dependencies.finish_usage_and_trace(
                trace_plan, status_code, response_text=message, error=exc
            )
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel="not_sent_supersession_blocked",
                confirmed=True,
            )
        return responses_protocol.openai_error_response(status_code, message)
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
            dependencies.finish_usage_and_trace(trace_plan, 499)
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
            dependencies.finish_usage_and_trace(trace_plan, 499)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel=transport_cancel,
                confirmed=teardown_confirmed,
            )
        raise
    except httpx.RequestError as exc:
        status_code, message = (
            responses_protocol.upstream_request_error_status_and_message(exc)
        )
        teardown_confirmed = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        try:
            dependencies.finish_usage_and_trace(
                trace_plan, status_code, response_text=message, error=exc
            )
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
        return responses_protocol.openai_error_response(status_code, message)
    except Exception:
        try:
            dependencies.finish_usage_and_trace(trace_plan, 599)
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
            return handle_upstream_error(
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
            if isinstance(trace_plan, UpstreamRequestPlan) and isinstance(
                trace_plan.trace_context, dict
            ):
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
                dependencies.finish_usage_and_trace(trace_plan, 499, upstream=upstream)
            finally:
                _complete_active_responses_teardown(
                    active_stream,
                    transport_cancel=transport_cancel,
                    confirmed=transport_confirmed,
                )
            raise
        except httpx.RequestError as exc:
            status_code, message = (
                responses_protocol.upstream_request_error_status_and_message(exc)
            )
            transport_cancel = await _close_upstream_response(
                upstream,
                cancel_generation=True,
            )
            transport_confirmed = transport_cancel in {
                "http2_rst_cancel",
                "http1_connection_close",
            }
            if isinstance(trace_plan, UpstreamRequestPlan) and isinstance(
                trace_plan.trace_context, dict
            ):
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
                dependencies.finish_usage_and_trace(
                    trace_plan,
                    status_code,
                    upstream=upstream,
                    response_text=message,
                    error=exc,
                )
            finally:
                _complete_active_responses_teardown(
                    active_stream,
                    transport_cancel=transport_cancel,
                    confirmed=transport_confirmed,
                )
            return responses_protocol.openai_error_response(status_code, message)
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
    if stream_type == "responses":
        response_headers["content-type"] = "text/event-stream"
    elif content_type:
        response_headers["content-type"] = content_type

    try:
        stream_body = _ManagedResponsesStreamBody(
            dependencies=dependencies,
            upstream=upstream,
            body=body,
            headers=headers,
            usage_event=usage_event,
            stream_type=stream_type,
            trace_plan=trace_plan,
            active_stream=active_stream,
            stream_transform=stream_transform_factory(upstream)
            if stream_transform_factory
            else stream_transform,
            trace_details_factory=trace_details_factory,
        )
    except Exception:
        transport_cancel = await _close_upstream_response(
            upstream,
            cancel_generation=True,
        )
        try:
            dependencies.finish_usage_and_trace(trace_plan, 599, upstream=upstream)
        finally:
            _complete_active_responses_teardown(
                active_stream,
                transport_cancel=transport_cancel,
                confirmed=transport_cancel
                in {
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
