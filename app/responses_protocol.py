"""Responses SSE, errors, reasoning output and tool discovery helpers."""

import codecs
import json

import httpx
from fastapi.responses import JSONResponse
import upstream_errors

# Keep the established protocol entry points available to existing callers.
from responses_input import (
    decode_fake_compaction as decode_fake_compaction,
    encode_fake_compaction as encode_fake_compaction,
    input_contains_compaction as input_contains_compaction,
    sanitize_input as sanitize_input,
)
from responses_compaction import (
    build_fake_compaction_request as build_fake_compaction_request,
    responses_to_compaction_response as responses_to_compaction_response,
)
from util import extract_response_output_text as extract_response_output_text


def _openai_error_type_for_status(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request_error"
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    return "server_error"


def openai_error_response(
    status_code: int,
    message: str,
    error_type: str | None = None,
    code=None,
    param=None,
    headers: dict | None = None,
) -> JSONResponse:
    payload = {
        "error": {
            "message": message,
            "type": error_type or _openai_error_type_for_status(status_code),
            "param": param,
            "code": code,
            "diagnosis": upstream_errors.diagnose_failure(status_code, code=code),
        }
    }
    return JSONResponse(content=payload, status_code=status_code, headers=headers)


def upstream_request_error_status_and_message(
    exc: httpx.RequestError,
) -> tuple[int, str]:
    if isinstance(exc, httpx.TimeoutException):
        return 504, "Upstream request timed out"
    if isinstance(exc, httpx.ConnectError):
        return 502, "Upstream connection failed"
    return 502, "Upstream request failed"


def http_exception_detail_to_message(detail) -> str:
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    return "Request failed"


# ─── SSE helpers ──────────────────────────────────────────────────────────────


def sse_encode(event_name: str, payload: dict) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n".encode(
        "utf-8"
    )


def response_message_events(item: dict, output_index: int):
    """Emit a message lifecycle from its completed text or refusal parts."""
    yield sse_encode(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {**item, "status": "in_progress", "content": []},
        },
    )
    for index, part in enumerate(item.get("content", [])):
        if not isinstance(part, dict) or part.get("type") not in {
            "output_text",
            "refusal",
        }:
            continue
        field, prefix = (
            ("refusal", "response.refusal")
            if part["type"] == "refusal"
            else ("text", "response.output_text")
        )
        if not isinstance(part.get(field), str):
            continue
        fields = {
            "item_id": item.get("id"),
            "output_index": output_index,
            "content_index": index,
        }
        for kind, data in (
            ("response.content_part.added", {"part": {**part, field: ""}}),
            (prefix + ".delta", {"delta": part[field]}),
            (prefix + ".done", {field: part[field]}),
            ("response.content_part.done", {"part": part}),
        ):
            yield sse_encode(kind, {"type": kind, **fields, **data})
    yield sse_encode(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": item,
        },
    )


def parse_sse_block(raw_block: str) -> tuple[str | None, str | None]:
    event_name = None
    data_lines = []
    for line in raw_block.replace("\r\n", "\n").split("\n"):
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return event_name, None
    return event_name, "\n".join(data_lines)


async def iter_sse_messages(byte_iter):
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")()
    transport_error = None
    try:
        async for chunk in byte_iter:
            buffer += decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
            normalized = buffer.replace("\r\n", "\n")
            while "\n\n" in normalized:
                raw_block, normalized = normalized.split("\n\n", 1)
                event_name, data = parse_sse_block(raw_block)
                if data is not None:
                    yield event_name, data
            buffer = normalized
    except httpx.TransportError as exc:
        transport_error = exc

    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        event_name, data = parse_sse_block(buffer.strip())
        if data is not None:
            yield event_name, data
    if transport_error is not None:
        raise transport_error


def extract_text_from_chat_delta(delta) -> str:
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
    return ""


_CODEX_THINKING_SUMMARY_HEADER = "**Thinking**\n\n"


def ensure_codex_reasoning_header(text: str) -> str:
    """Ensure reasoning summary text begins with a bold header for Codex/ChatGPT desktop."""
    if not isinstance(text, str) or not text.strip():
        return text
    stripped = text.lstrip()
    if stripped.startswith("**") or stripped.startswith("#"):
        return text
    return f"{_CODEX_THINKING_SUMMARY_HEADER}{text}"


def normalize_reasoning_item_for_client(item: dict, fallback_text: str = "") -> dict:
    """Normalize a Responses reasoning item so the ChatGPT/Codex Electron app displays it.

    Codex and the ChatGPT desktop app only render reasoning items from the
    ``summary`` array (ignoring ``content``). Items with empty ``summary`` are
    discarded. When upstream text is available, this helper ensures:
    1. ``summary`` is populated with summary_text.
    2. ``content`` is populated with reasoning_text.
    3. Text begins with a bold header if none was present.

    Encrypted-only items retain their replay state without fabricated text.
    """
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return item

    summary_parts = []
    raw_summary = item.get("summary")
    if isinstance(raw_summary, list):
        for p in raw_summary:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                summary_parts.append(p["text"])
            elif isinstance(p, str):
                summary_parts.append(p)
    summary_text = "".join(summary_parts)

    content_parts = []
    raw_content = item.get("content")
    if isinstance(raw_content, list):
        for p in raw_content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                content_parts.append(p["text"])
            elif isinstance(p, str):
                content_parts.append(p)
    content_text = "".join(content_parts)

    text = summary_text or content_text or fallback_text

    if text:
        formatted = ensure_codex_reasoning_header(text)
        item["summary"] = [{"type": "summary_text", "text": formatted}]
        item["content"] = [{"type": "reasoning_text", "text": formatted}]

    return item


def normalize_response_reasoning_for_client(payload: dict) -> dict:
    """Normalize all reasoning items in a completed Responses payload."""
    if not isinstance(payload, dict):
        return payload
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                normalize_reasoning_item_for_client(item)
    return payload


# ─── Responses tool discovery ───────────────────────────────────────────────


def _responses_tool_is_tool_search(tool: dict) -> bool:
    tool_type = str(tool.get("type", "")).strip().lower()
    if tool_type == "tool_search":
        return True
    if "tool_search" in tool:
        return True
    name = _responses_tool_name(tool)
    if not isinstance(name, str):
        return False
    normalized = name.strip().lower()
    return normalized in _TOOL_SEARCH_TOOL_NAMES or normalized.endswith("__tool_search")


def responses_tools_have_tool_search(tools) -> bool:
    """Return whether a Responses ``tools`` tree exposes a tool-search tool."""
    if isinstance(tools, list):
        return any(responses_tools_have_tool_search(tool) for tool in tools)
    if not isinstance(tools, dict):
        return False
    if _responses_tool_is_tool_search(tools):
        return True
    nested = tools.get("tools")
    if isinstance(nested, (list, dict)):
        return responses_tools_have_tool_search(nested)
    return False


_TOOL_SEARCH_TOOL_NAMES = {"tool_search", "tools.tool_search"}


def _responses_tool_name(tool: dict) -> str | None:
    name = tool.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    function = tool.get("function") if isinstance(tool.get("function"), dict) else None
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None
