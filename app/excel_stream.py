"""Conservative recovery and liveness for Excel Responses SSE streams."""

import asyncio
from contextlib import suppress
import json

import httpx

import responses_protocol
import upstream_errors


async def iter_events(byte_iter, *, heartbeat_seconds=15.0):
    events = responses_protocol.iter_sse_messages(byte_iter)
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
                    yield (
                        "response.in_progress",
                        json.dumps(
                            {
                                "type": "response.in_progress",
                                "response": {
                                    **response,
                                    "status": "in_progress",
                                    "output": [],
                                },
                            }
                        ),
                    )
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
                    response.update(
                        {
                            key: metadata[key]
                            for key in ("id", "object", "created_at", "model")
                            if key in metadata
                        }
                    )
            elif event_type == "response.output_item.done":
                index, item = payload.get("output_index"), payload.get("item")
                if isinstance(index, int) and index >= 0 and isinstance(item, dict):
                    finished[index] = item
                    active.discard(index)
            elif event_type == "response.output_item.added" or event_type.endswith(
                ".delta"
            ):
                active.add(payload.get("output_index"))
            if event_type != "response.in_progress":
                last_event = event_type
            yield event_name, data
            if event_type in {
                "response.completed",
                "response.failed",
                "response.incomplete",
                "error",
            }:
                return

        # A finished final answer or a fully completed tool batch can survive
        # a missing terminal frame. Commentary and partial calls cannot.
        items = [finished[index] for index in sorted(finished)]
        executable = any(
            item.get("type") in {"function_call", "custom_tool_call"} for item in items
        )
        final_answer = any(
            item.get("type") == "message" and item.get("phase") == "final_answer"
            for item in items
        )
        complete = all(
            item.get("type") == "reasoning" or item.get("status") == "completed"
            for item in items
        )
        if (
            response.get("id")
            and items
            and not active
            and complete
            and sorted(finished) == list(range(len(finished)))
            and last_event == "response.output_item.done"
            and (executable or final_answer)
        ):
            yield (
                "response.completed",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            **response,
                            "status": "completed",
                            "output": items,
                        },
                    }
                ),
            )
            return
        if transport_error is not None:
            raise transport_error
        raise upstream_errors.ExcelResponseError(
            "excel_stream_incomplete",
            "Excel stream ended before a terminal Responses event",
        )
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(
                asyncio.CancelledError, StopAsyncIteration, httpx.TransportError
            ):
                await pending
        await events.aclose()
        if hasattr(byte_iter, "aclose"):
            await byte_iter.aclose()


def extract_upstream_json_payload(upstream: httpx.Response) -> dict | None:
    content_type = upstream.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return None
    try:
        payload = upstream.json()
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def completed_response(response: object, finished_items: dict[int, dict]) -> dict:
    if not isinstance(response, dict) or response.get("status") not in (
        None,
        "completed",
    ):
        raise upstream_errors.ExcelResponseError(
            "excel_invalid_response",
            "Excel response.completed has no valid response payload",
        )
    result = dict(response)
    terminal_output = response.get("output")
    if terminal_output is None:
        terminal_output = []
    if not isinstance(terminal_output, list):
        raise upstream_errors.ExcelResponseError(
            "excel_invalid_response", "Excel response has an invalid output list"
        )
    for item in list(finished_items.values()) + terminal_output:
        if not isinstance(item, dict) or (
            item.get("id") is not None and not isinstance(item["id"], str)
        ):
            raise upstream_errors.ExcelResponseError(
                "excel_invalid_output_item",
                "Excel response has an invalid output item",
            )
    output = dict(finished_items)
    item_indices = {
        item["id"]: index for index, item in finished_items.items() if item.get("id")
    }
    for index, item in enumerate(terminal_output):
        index = item_indices.get(item.get("id"), index)
        previous = output.get(index, {})
        if previous.get("id") and item.get("id") and previous["id"] != item["id"]:
            raise upstream_errors.ExcelResponseError(
                "excel_conflicting_output_items",
                "Excel response has conflicting output item identities",
            )
        output[index] = {**previous, **item}
    if sorted(output) != list(range(len(output))):
        raise upstream_errors.ExcelResponseError(
            "excel_missing_output_items",
            "Excel response is missing completed output items",
        )
    result["output"] = [output[index] for index in sorted(output)]
    if not responses_protocol.extract_response_output_text(result) and not any(
        item.get("type") in {"function_call", "custom_tool_call", "compaction"}
        or (
            item.get("type") == "message"
            and any(
                isinstance(part, dict)
                and part.get("type") == "refusal"
                and part.get("refusal")
                for part in item.get("content", [])
            )
        )
        for item in result["output"]
    ):
        raise upstream_errors.ExcelResponseError(
            "excel_empty_response",
            "Excel completed without assistant text, a refusal, a tool call, or compaction",
        )
    return result
