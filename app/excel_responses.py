"""Excel response conversion and corrective generation for both response modes.

Low-level SSE recovery belongs to excel_stream; transport lifetime belongs to
responses_stream. Application services enter only through explicit callbacks.
"""

import asyncio
from contextlib import aclosing
import json
from typing import Callable

from anyio import CancelScope
import httpx
from fastapi.responses import JSONResponse, Response

import excel_stream
import excel_models
import excel_tool_catalog
import excel_tool_transport
import excel_tool_recovery
import responses_protocol
import structured_output
import upstream_errors
import usage_metrics
from excel_stream import (
    completed_response as _excel_completed_response,
    extract_upstream_json_payload as _extract_upstream_json_payload,
)
from rate_limiting import throttled_client_send
from responses_stream import _close_upstream_response
from upstream_request import UpstreamRequestPlan
from usage_tracking import UsageTracker


EXCEL_STREAM_HEARTBEAT_SECONDS = 15.0


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
        responses_protocol.sse_encode(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            },
        ),
        responses_protocol.sse_encode(
            delta_event,
            {
                "type": delta_event,
                "output_index": output_index,
                "item_id": tool_call["id"],
                "delta": tool_call[value_key],
            },
        ),
        responses_protocol.sse_encode(
            done_event,
            {
                "type": done_event,
                "output_index": output_index,
                "item_id": tool_call["id"],
                value_key: tool_call[value_key],
            },
        ),
        responses_protocol.sse_encode(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": {**tool_call, "status": "completed"},
            },
        ),
    ]


class ExcelResponseProcessor:
    """Process Excel responses using the caller's client and accounting services."""

    def __init__(
        self,
        *,
        usage_tracker: UsageTracker,
        get_upstream_client: Callable[[], httpx.AsyncClient],
        finish_usage_and_trace: Callable[..., None],
    ):
        self.usage_tracker = usage_tracker
        self.get_upstream_client = get_upstream_client
        self.finish_usage_and_trace = finish_usage_and_trace

    def handle_upstream_error(
        self,
        upstream: httpx.Response,
        *,
        trace_plan: UpstreamRequestPlan | None,
    ) -> Response:
        payload = _extract_upstream_json_payload(upstream)
        self.finish_usage_and_trace(
            trace_plan,
            upstream.status_code,
            upstream=upstream,
            response_payload=payload,
        )
        headers = {
            name: upstream.headers[name]
            for name in ("x-request-id", "retry-after")
            if name in upstream.headers
        }
        response = JSONResponse(
            content=upstream_errors.excel_error_payload(upstream.status_code, payload),
            status_code=upstream.status_code,
            headers=headers,
        )
        # Internal only: a later corrective generation's 401 must not cause the
        # original, already accepted request to be sent again.
        response._excel_auth_rejected = upstream.status_code == 401
        return response

    def tool_stream_transform(
        self,
        source_body: dict,
        *,
        trace_plan: UpstreamRequestPlan | None = None,
        upstream: httpx.Response | None = None,
    ):
        allowed_tools = excel_tool_catalog.client_tool_types(source_body)
        output_format = structured_output.request_format(source_body)
        marker_open = excel_tool_catalog.TOOL_CALL_MARKER_OPEN

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
                    responses_protocol.sse_encode(
                        "response.output_text.delta",
                        {
                            **delta_template,
                            "type": "response.output_text.delta",
                            "delta": pending,
                        },
                    )
                ]

            async with aclosing(
                excel_stream.iter_events(
                    byte_iter,
                    heartbeat_seconds=EXCEL_STREAM_HEARTBEAT_SECONDS,
                )
            ) as events:
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
                    event_type = (
                        str(event_name or payload.get("type") or "").strip().lower()
                    )
                    event_output_index = payload.get("output_index")
                    if output_format is not None and structured_output.is_message_event(
                        event_type, payload
                    ):
                        if (
                            event_type == "response.output_item.done"
                            and isinstance(event_output_index, int)
                            and event_output_index >= 0
                        ):
                            finished_items[event_output_index] = dict(payload["item"])
                        continue
                    if output_format is not None and event_type in {
                        "response.created",
                        "response.in_progress",
                    }:
                        metadata = payload.get("response")
                        if isinstance(metadata, dict):
                            payload["response"] = {**metadata, "output": []}
                            payload["response"].pop("output_text", None)
                    encoded = responses_protocol.sse_encode(
                        event_type or "message", payload
                    )

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
                                yield responses_protocol.sse_encode(
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
                            yield responses_protocol.sse_encode(
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

                    if event_type in {
                        "response.output_item.added",
                        "response.output_item.done",
                    }:
                        item = payload.get("item")
                        item_type = item.get("type") if isinstance(item, dict) else None
                        if event_type == "response.output_item.done" and isinstance(
                            item, dict
                        ):
                            if (
                                isinstance(event_output_index, int)
                                and event_output_index >= 0
                            ):
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
                            responses_protocol.normalize_reasoning_item_for_client(item)
                            yield responses_protocol.sse_encode(event_type, payload)
                            continue
                        yield encoded
                        continue

                    if event_type in {
                        "response.completed",
                        "response.failed",
                        "response.incomplete",
                    }:
                        response = payload.get("response")
                        response = response if isinstance(response, dict) else None
                        if event_type != "response.completed":
                            # A partial native call is never executable client work.
                            if response and isinstance(response.get("output"), list):
                                response["output"] = [
                                    item
                                    for item in response["output"]
                                    if not isinstance(item, dict)
                                    or (
                                        item.get("type")
                                        not in {"function_call", "custom_tool_call"}
                                        and (
                                            output_format is None
                                            or item.get("type") != "message"
                                        )
                                    )
                                ]
                            safe_response = {
                                key: response[key]
                                for key in ("id", "status", "output", "usage")
                                if response and key in response
                            }
                            safe_response["error"] = (
                                upstream_errors.excel_error_payload(502, response)[
                                    "error"
                                ]
                            )
                            yield responses_protocol.sse_encode(
                                event_type,
                                {"type": event_type, "response": safe_response},
                            )
                            return
                        response = _excel_completed_response(response, finished_items)
                        structured_output.validate_response(response, output_format)
                        payload["response"] = response
                        responses_protocol.normalize_response_reasoning_for_client(
                            response
                        )
                        completed_text = (
                            full_text
                            or responses_protocol.extract_response_output_text(response)
                        )
                        tool_call = (
                            excel_tool_transport.extract_client_tool_call(
                                completed_text or "", allowed_tools
                            )
                            if output_format is None
                            else None
                        )
                        tool_diagnostics: dict = {}
                        tool_calls = (
                            excel_tool_transport.extract_native_client_tool_calls(
                                response,
                                source_body,
                                diagnostics=tool_diagnostics,
                            )
                        )
                        has_native_calls = any(
                            item.get("type") in {"function_call", "custom_tool_call"}
                            for item in response.get("output", [])
                            if isinstance(item, dict)
                        )
                        if (
                            tool_calls is None
                            and has_native_calls
                            and trace_plan is not None
                            and output_format is None
                        ):
                            if upstream is not None:
                                await upstream.aclose()
                            repair_task = asyncio.create_task(
                                self.repair_tool_response(
                                    trace_plan,
                                    response,
                                    source_body,
                                    tool_diagnostics,
                                )
                            )
                            try:
                                while not repair_task.done():
                                    done, _ = await asyncio.wait(
                                        {repair_task},
                                        timeout=EXCEL_STREAM_HEARTBEAT_SECONDS,
                                    )
                                    if not done:
                                        yield b": keep-alive\n\n"
                                response = repair_task.result()
                            finally:
                                if not repair_task.done():
                                    repair_task.cancel()
                                    try:
                                        await repair_task
                                    except asyncio.CancelledError:
                                        pass
                            tool_calls = (
                                excel_tool_transport.extract_native_client_tool_calls(
                                    response,
                                    source_body,
                                    diagnostics=tool_diagnostics,
                                )
                            )
                        if not has_native_calls and tool_call is not None:
                            tool_calls = [tool_call]
                        if tool_calls is not None:
                            held_events.clear()
                            emitted_upto = len(full_text)
                            response_payload = (
                                excel_tool_transport.response_payload_with_tool_calls(
                                    response,
                                    tool_calls,
                                    model_id=excel_models.excel_model_id(
                                        source_body.get("model")
                                    )
                                    or excel_models.MODEL_ID,
                                )
                            )
                            for index, item in enumerate(response_payload["output"]):
                                if (
                                    output_format is not None
                                    and item.get("type") == "message"
                                ):
                                    for (
                                        chunk
                                    ) in responses_protocol.response_message_events(
                                        item, index
                                    ):
                                        yield chunk
                                if item.get("type") not in {
                                    "function_call",
                                    "custom_tool_call",
                                }:
                                    continue
                                for chunk in _excel_tool_call_event_bytes(
                                    item, output_index=index
                                ):
                                    yield chunk
                            yield responses_protocol.sse_encode(
                                "response.completed",
                                {
                                    "type": "response.completed",
                                    "response": response_payload,
                                },
                            )
                            return
                        if native_call_seen or any(
                            isinstance(item, dict)
                            and item.get("type")
                            in {"function_call", "custom_tool_call"}
                            for item in response.get("output", [])
                        ):
                            raise upstream_errors.ExcelResponseError(
                                "excel_untranslatable_tool_call",
                                excel_tool_recovery.tool_call_failure_message(
                                    tool_diagnostics
                                ),
                            )
                        # Not a tool call after all: release everything that was held
                        # back so the client still receives the full assistant text.
                        for chunk in flush_text():
                            yield chunk
                        for held in held_events:
                            yield held
                        held_events.clear()
                        marker_mode = False
                        if output_format is not None:
                            for index, item in enumerate(response["output"]):
                                if item.get("type") == "message":
                                    for (
                                        chunk
                                    ) in responses_protocol.response_message_events(
                                        item, index
                                    ):
                                        yield chunk
                        yield responses_protocol.sse_encode(event_type, payload)
                        return

                    if event_type == "error":
                        yield responses_protocol.sse_encode(
                            "error",
                            {
                                "type": "error",
                                **upstream_errors.excel_error_payload(502, payload),
                            },
                        )
                        return

                    yield encoded

            # EOF (even [DONE]) is not proof of a completed model response. Leave
            # calls unexposed so the client can retry without repeating tool work.
            raise upstream_errors.ExcelResponseError(
                "excel_stream_incomplete",
                "Excel stream ended before a terminal Responses event",
            )

        return transform

    async def read_response_payload(
        self,
        upstream: httpx.Response,
        usage_event: dict | None = None,
    ) -> dict | None:
        finished_items: dict[int, dict] = {}
        capture = self.usage_tracker.create_sse_capture("responses")
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
                    self.usage_tracker.mark_first_output(usage_event)
                if event_type == "response.output_item.done":
                    index, item = parsed.get("output_index"), parsed.get("item")
                    if isinstance(index, int) and index >= 0 and isinstance(item, dict):
                        finished_items[index] = item
                elif event_type == "response.completed":
                    # Do not wait for EOF: BPS can send a malformed HTTP tail after
                    # the valid terminal SSE event, or leave the connection open.
                    return _excel_completed_response(
                        parsed.get("response"), finished_items
                    )
                elif event_type in {"response.failed", "response.incomplete"}:
                    return parsed.get("response")
                elif event_type == "error":
                    return {"status": "failed", "error": parsed.get("error", parsed)}
        raise upstream_errors.ExcelResponseError(
            "excel_stream_incomplete",
            "Excel stream ended before a terminal Responses event",
        )

    async def repair_tool_response(
        self,
        plan: UpstreamRequestPlan,
        response: dict,
        client_body: dict,
        diagnostics: dict,
    ) -> dict:
        """One corrective generation after a completed, unexposed tool failure."""
        repair_body = excel_tool_recovery.unknown_tool_regeneration_request(
            plan.body, response, client_body, diagnostics
        )
        regeneration = repair_body is not None
        if repair_body is None:
            repair_body = excel_tool_recovery.tool_call_repair_request(
                plan.body, response, diagnostics
            )
        if diagnostics.get("reason") != "missing_completed_tool_call":
            if not isinstance(plan.trace_context, dict):
                plan.trace_context = {}
            plan.trace_context["tool_call_diagnostics"] = dict(diagnostics)
        if repair_body is None:
            return response
        if not isinstance(plan.trace_context, dict):
            plan.trace_context = {}
        if plan.trace_context.get("tool_call_recovery"):
            return response
        recovery = {
            "attempts": 1,
            "outcome": "started",
            "diagnostics": dict(diagnostics),
        }
        if regeneration:
            recovery["mode"] = "unknown_tool_regeneration"
        original_usage = usage_metrics.normalize_usage_payload(response.get("usage"))
        if original_usage is not None:
            recovery["usage"] = original_usage
        plan.trace_context["tool_call_recovery"] = recovery
        client = self.get_upstream_client()
        upstream = None
        ended = False
        try:
            request = client.build_request(
                "POST", plan.upstream_url, headers=plan.headers, json=repair_body
            )
            upstream = await throttled_client_send(client, request, stream=True)
            if upstream.status_code >= 400:
                ended = True
                safe_error = upstream_errors.excel_error_payload(upstream.status_code)[
                    "error"
                ]
                raise upstream_errors.ExcelResponseError(
                    safe_error["code"],
                    safe_error["message"],
                    status_code=upstream.status_code,
                )
            if "text/event-stream" in upstream.headers.get("content-type", "").lower():
                repaired = await self.read_response_payload(upstream)
            else:
                await upstream.aread()
                repaired = _extract_upstream_json_payload(upstream)
            ended = True
            repaired_usage = (
                usage_metrics.normalize_usage_payload(repaired.get("usage"))
                if isinstance(repaired, dict)
                else None
            )
            if repaired_usage is not None:
                first = original_usage or {}
                # Normalized fresh-input fields must include the uncached request too.
                if (
                    "fresh_input_tokens" in first
                    or "fresh_input_tokens" in repaired_usage
                ):
                    first = {
                        **first,
                        "fresh_input_tokens": first.get(
                            "fresh_input_tokens", first.get("input_tokens", 0)
                        ),
                    }
                    repaired_usage.setdefault(
                        "fresh_input_tokens", repaired_usage["input_tokens"]
                    )
                recovery["usage"] = {
                    key: first.get(key, 0) + repaired_usage.get(key, 0)
                    for key in first.keys() | repaired_usage.keys()
                }
            repaired = _excel_completed_response(repaired, {})
            corrected = [
                item
                for item in repaired["output"]
                if item.get("type") in {"function_call", "custom_tool_call"}
            ]
            if len(corrected) != 1:
                recovery["outcome"] = "rejected"
                return response
            output = list(response["output"])
            positions = [
                index
                for index, item in enumerate(output)
                if item.get("type") in {"function_call", "custom_tool_call"}
            ]
            rejected = output[positions[diagnostics["tool_call_index"]]]
            if (
                not regeneration
                and not excel_tool_recovery.tool_call_repair_preserves_input(
                    rejected, corrected[0], client_body
                )
            ):
                recovery.update(
                    outcome="rejected",
                    repair_diagnostics={"reason": "repair_changed_input"},
                )
                return response
            output[positions[diagnostics["tool_call_index"]]] = corrected[0]
            candidate = {**response, "output": output}
            corrected_id = corrected[0].get("id")
            if (
                corrected_id
                and sum(item.get("id") == corrected_id for item in output) != 1
            ):
                recovery.update(
                    outcome="rejected",
                    repair_diagnostics={"reason": "duplicate_tool_identity"},
                )
                return response
            checked = {}
            if (
                excel_tool_transport.extract_native_client_tool_calls(
                    candidate, client_body, diagnostics=checked
                )
                is None
            ):
                recovery.update(outcome="rejected", repair_diagnostics=checked)
                return response
            recovery["outcome"] = "succeeded"
            if isinstance(recovery.get("usage"), dict):
                candidate["usage"] = recovery["usage"]
            return candidate
        except asyncio.CancelledError:
            recovery["outcome"] = "cancelled"
            raise
        except httpx.RequestError as exc:
            recovery["outcome"] = "failed"
            if isinstance(exc, upstream_errors.ExcelResponseError):
                status, code, message = exc.status_code, exc.code, str(exc)
            else:
                status, message = (
                    responses_protocol.upstream_request_error_status_and_message(exc)
                )
                code = "excel_repair_transport_error"
            recovery["failure_diagnosis"] = upstream_errors.diagnose_failure(
                status,
                code=code,
                error_type=type(exc).__name__,
            )
            raise upstream_errors.ExcelResponseError(
                code, message, status_code=status
            ) from exc
        finally:
            if upstream is not None:
                with CancelScope(shield=True):
                    await _close_upstream_response(
                        upstream, cancel_generation=not ended
                    )

    async def post_non_streaming_request(
        self,
        plan: UpstreamRequestPlan,
        *,
        client_body: dict,
    ) -> Response:
        excel_model_id = (
            excel_models.excel_model_id(client_body.get("model"))
            or excel_models.MODEL_ID
        )
        output_format = structured_output.request_format(client_body)
        client = self.get_upstream_client()
        upstream: httpx.Response | None = None
        response_payload: dict | None = None
        try:
            request = client.build_request(
                "POST",
                plan.upstream_url,
                headers=plan.headers,
                json=plan.body,
            )
            # Only the shared connect-only retry policy is safe for a model POST.
            upstream = await throttled_client_send(client, request, stream=True)
            if upstream.status_code >= 400:
                await upstream.aread()
                return self.handle_upstream_error(
                    upstream,
                    trace_plan=plan,
                )
            if "text/event-stream" in upstream.headers.get("content-type", "").lower():
                response_payload = await self.read_response_payload(
                    upstream, plan.usage_event
                )
            else:
                await upstream.aread()
                response_payload = _extract_upstream_json_payload(upstream)
                if isinstance(response_payload, dict) and response_payload.get(
                    "status"
                ) not in {"failed", "incomplete"}:
                    response_payload = _excel_completed_response(response_payload, {})
                    # JSON has no observable token stream; record output on arrival.
                    capture = self.usage_tracker.create_sse_capture("responses")
                    if capture.consume_responses_payload(
                        {"response": response_payload}
                    ):
                        self.usage_tracker.mark_first_output(plan.usage_event)
            if isinstance(response_payload, dict) and response_payload.get(
                "status"
            ) not in {"failed", "incomplete"}:
                structured_output.validate_response(response_payload, output_format)
                diagnostics = {}
                calls = excel_tool_transport.extract_native_client_tool_calls(
                    response_payload, client_body, diagnostics=diagnostics
                )
                if calls is None and output_format is None:
                    await upstream.aclose()
                    response_payload = await self.repair_tool_response(
                        plan, response_payload, client_body, diagnostics
                    )
        except asyncio.CancelledError:
            self.finish_usage_and_trace(plan, 499, upstream=upstream)
            raise
        except upstream_errors.ExcelResponseError as exc:
            payload = {
                "error": {
                    "type": "server_error"
                    if exc.status_code >= 500
                    else "invalid_request_error",
                    "code": exc.code,
                    "message": str(exc),
                    "param": None,
                    "diagnosis": upstream_errors.diagnose_failure(
                        exc.status_code, code=exc.code
                    ),
                }
            }
            self.finish_usage_and_trace(
                plan,
                exc.status_code,
                upstream=upstream,
                response_payload=payload,
                error=exc,
            )
            return JSONResponse(status_code=exc.status_code, content=payload)
        except httpx.RequestError as exc:
            status_code, message = (
                responses_protocol.upstream_request_error_status_and_message(exc)
            )
            self.finish_usage_and_trace(
                plan, status_code, upstream=upstream, response_text=message, error=exc
            )
            return responses_protocol.openai_error_response(status_code, message)
        except Exception:
            self.finish_usage_and_trace(plan, 599, upstream=upstream)
            raise
        finally:
            if upstream is not None:
                await upstream.aclose()

        if not isinstance(response_payload, dict):
            message = "Upstream response did not include a completed Responses payload"
            self.finish_usage_and_trace(plan, 502, response_text=message)
            return responses_protocol.openai_error_response(502, message)

        translated_payload = dict(response_payload)
        translated_payload["model"] = excel_model_id
        if response_payload.get("status") in {"failed", "incomplete"}:
            self.finish_usage_and_trace(
                plan, 502, upstream=upstream, response_payload=response_payload
            )
            return JSONResponse(
                status_code=502,
                content=upstream_errors.excel_error_payload(502, response_payload),
            )
        response_text = responses_protocol.extract_response_output_text(
            response_payload
        )
        tool_call = (
            excel_tool_transport.extract_client_tool_call(
                response_text,
                excel_tool_catalog.client_tool_types(client_body),
            )
            if output_format is None
            else None
        )
        tool_diagnostics: dict = {}
        tool_calls = excel_tool_transport.extract_native_client_tool_calls(
            response_payload,
            client_body,
            diagnostics=tool_diagnostics,
        )
        has_native_calls = any(
            isinstance(item, dict)
            and item.get("type") in {"function_call", "custom_tool_call"}
            for item in response_payload.get("output", [])
        )
        if not has_native_calls and tool_call is not None:
            tool_calls = [tool_call]
        if tool_calls is not None:
            translated_payload = excel_tool_transport.response_payload_with_tool_calls(
                response_payload,
                tool_calls,
                model_id=excel_model_id,
            )
            responses_protocol.normalize_response_reasoning_for_client(
                translated_payload
            )
        elif any(
            isinstance(item, dict)
            and item.get("type") in {"function_call", "custom_tool_call"}
            for item in response_payload.get("output", [])
        ):
            message = excel_tool_recovery.tool_call_failure_message(tool_diagnostics)
            error_payload = {
                "error": {
                    "type": "server_error",
                    "code": "excel_untranslatable_tool_call",
                    "message": message,
                    "param": None,
                    "diagnosis": upstream_errors.diagnose_failure(
                        502, code="excel_untranslatable_tool_call"
                    ),
                }
            }
            self.finish_usage_and_trace(
                plan,
                502,
                upstream=upstream,
                response_payload=error_payload,
                response_text=message,
            )
            return JSONResponse(status_code=502, content=error_payload)

        self.finish_usage_and_trace(
            plan,
            upstream.status_code,
            upstream=upstream,
            response_payload=(
                translated_payload if isinstance(translated_payload, dict) else None
            ),
            response_text=(
                responses_protocol.extract_response_output_text(translated_payload)
                if isinstance(translated_payload, dict)
                else _extract_upstream_text(upstream)
            ),
        )
        if isinstance(translated_payload, dict):
            return JSONResponse(
                content=translated_payload,
                status_code=upstream.status_code,
                headers={"x-request-id": upstream.headers["x-request-id"]}
                if "x-request-id" in upstream.headers
                else {},
            )
        return proxy_non_streaming_response(upstream)
