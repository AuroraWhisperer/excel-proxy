"""Conservative recovery and liveness for Excel Responses SSE streams."""

import asyncio
from contextlib import suppress
import json

import httpx

import format_translation
import upstream_errors


async def iter_events(byte_iter, *, heartbeat_seconds=15.0):
    events = format_translation.iter_sse_messages(byte_iter)
    pending = None
    response = {}
    finished = {}
    active = set()
    last_event = None
    transport_error = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(events))
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                if response.get("id"):
                    yield "response.in_progress", json.dumps({
                        "type": "response.in_progress",
                        "response": {**response, "status": "in_progress", "output": []},
                    })
                else:
                    yield None, ""  # SSE comment until an upstream identity exists.
                continue
            try:
                event_name, data = pending.result()
            except StopAsyncIteration:
                break
            except httpx.TransportError as exc:
                transport_error = exc
                break
            finally:
                pending = None
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                last_event = None
                continue
            if not isinstance(payload, dict):
                last_event = None
                continue
            event_type = str(event_name or payload.get("type") or "").lower()
            if event_type in {"response.created", "response.in_progress"}:
                metadata = payload.get("response")
                if isinstance(metadata, dict):
                    response.update({key: metadata[key] for key in ("id", "object", "created_at", "model") if key in metadata})
            elif event_type == "response.output_item.done":
                index, item = payload.get("output_index"), payload.get("item")
                if isinstance(index, int) and index >= 0 and isinstance(item, dict):
                    finished[index] = item
                    active.discard(index)
            elif event_type == "response.output_item.added" or event_type.endswith(".delta"):
                active.add(payload.get("output_index"))
            if event_type != "response.in_progress":
                last_event = event_type
            yield event_name, data
            if event_type in {"response.completed", "response.failed", "response.incomplete", "error"}:
                return

        # A finished final answer or a fully completed tool batch can survive
        # a missing terminal frame. Commentary and partial calls cannot.
        items = [finished[index] for index in sorted(finished)]
        executable = any(item.get("type") in {"function_call", "custom_tool_call"} for item in items)
        final_answer = any(
            item.get("type") == "message" and item.get("phase") == "final_answer"
            for item in items
        )
        complete = all(
            item.get("type") == "reasoning"
            or item.get("status") == "completed"
            for item in items
        )
        if (response.get("id") and items and not active and complete
                and sorted(finished) == list(range(len(finished)))
                and last_event == "response.output_item.done" and (executable or final_answer)):
            yield "response.completed", json.dumps({
                "type": "response.completed",
                "response": {**response, "status": "completed", "output": items},
            })
            return
        if transport_error is not None:
            raise transport_error
        raise upstream_errors.ExcelResponseError(
            "excel_stream_incomplete", "Excel stream ended before a terminal Responses event",
        )
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration, httpx.TransportError):
                await pending
        await events.aclose()
        if hasattr(byte_iter, "aclose"):
            await byte_iter.aclose()
