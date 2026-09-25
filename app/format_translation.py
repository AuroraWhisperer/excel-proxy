"""Responses protocol helpers for Excel requests, reasoning and compaction."""

import base64
import codecs
import json
import re
import time
from uuid import uuid4

import httpx
from fastapi.responses import JSONResponse
import util

from constants import FAKE_COMPACTION_PREFIX, FAKE_COMPACTION_SUMMARY_LABEL, COMPACTION_SUMMARY_PROMPT

_SUBAGENT_NOTIFICATION_ONLY_RE = re.compile(
    r"\A\s*(?:(?:"
    r"<subagent[_-]notification>\s*.*?\s*</subagent[_-]notification>"
    r"|<task-notification>\s*.*?\s*</task-notification>"
    r")\s*)+\Z",
    re.DOTALL,
)


# ─── Error translation ───────────────────────────────────────────────────────

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


def openai_error_response(status_code: int, message: str, error_type: str | None = None, code=None, param=None, headers: dict | None = None) -> JSONResponse:
    payload = {
        "error": {
            "message": message,
            "type": error_type or _openai_error_type_for_status(status_code),
            "param": param,
            "code": code,
        }
    }
    return JSONResponse(content=payload, status_code=status_code, headers=headers)


def upstream_request_error_status_and_message(exc: httpx.RequestError) -> tuple[int, str]:
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
    return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n".encode("utf-8")


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
    async for chunk in byte_iter:
        if isinstance(chunk, bytes):
            buffer += decoder.decode(chunk)
        else:
            buffer += str(chunk)

        normalized = buffer.replace("\r\n", "\n")
        while "\n\n" in normalized:
            raw_block, normalized = normalized.split("\n\n", 1)
            event_name, data = parse_sse_block(raw_block)
            if data is not None:
                yield event_name, data
        buffer = normalized

    buffer += decoder.decode(b"", final=True)

    trailing = buffer.strip()
    if trailing:
        event_name, data = parse_sse_block(trailing)
        if data is not None:
            yield event_name, data


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


# ─── Compaction ───────────────────────────────────────────────────────────────

def encode_fake_compaction(summary_text: str) -> str:
    encoded = base64.urlsafe_b64encode(summary_text.encode("utf-8")).decode("ascii")
    return f"{FAKE_COMPACTION_PREFIX}{encoded}"


def decode_fake_compaction(encrypted_content: str) -> str | None:
    if not isinstance(encrypted_content, str):
        return None
    if not encrypted_content.startswith(FAKE_COMPACTION_PREFIX):
        return None

    encoded = encrypted_content[len(FAKE_COMPACTION_PREFIX) :]
    try:
        decoded = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    return decoded or None


def _summary_message_item(summary_text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": f"{FAKE_COMPACTION_SUMMARY_LABEL}\n{summary_text}",
            }
        ],
    }


def input_contains_compaction(input_items) -> bool:
    if not isinstance(input_items, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "compaction" for item in input_items)


def _latest_compaction_window(input_items):
    if not isinstance(input_items, list):
        return input_items

    latest_compaction_index = None
    for index, item in enumerate(input_items):
        if isinstance(item, dict) and item.get("type") == "compaction":
            latest_compaction_index = index

    if latest_compaction_index is None:
        return input_items

    pre_items = input_items[:latest_compaction_index]
    post_items = input_items[latest_compaction_index:]

    # Check if pre_items has any intermediate assistant turns or tool interactions
    has_intermediate_turns = any(
        isinstance(item, dict)
        and (
            item.get("type") in {
                "function_call",
                "custom_tool_call",
                "function_call_output",
                "custom_tool_call_output",
                "reasoning",
            }
            or (item.get("type") in (None, "message") and str(item.get("role", "")).lower() == "assistant")
        )
        for item in pre_items
    )

    if not has_intermediate_turns:
        # Pre-compaction items contain no intermediate conversation turns (e.g. Codex
        # already pruned them, keeping only developer/system instructions, environment
        # context, and the active user task). Keep all pre_items so context and user
        # instructions are not lost.
        return input_items

    # If there were intermediate turns, preserve preamble items (developer/system
    # messages, environment/skills context), plus the active user task if post_items
    # doesn't contain any user prompt.
    has_post_user_message = any(
        isinstance(item, dict)
        and item.get("type") in (None, "message")
        and str(item.get("role", "")).lower() == "user"
        for item in post_items[1:]  # skip the compaction item itself
    )

    preserved_pre = []
    latest_user_task = None
    for item in pre_items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).lower()
        item_type = str(item.get("type", "")).lower()
        if role in {"developer", "system"} or item_type in {"developer", "system"}:
            preserved_pre.append(item)
            continue
        text = _message_text(item) if item_type in {"", "message"} else ""
        if any(marker in text for marker in (
            "<environment_context>",
            "<permissions instructions>",
            "<skills_instructions>",
            "<instructions>",
            "# AGENTS.md",
        )):
            preserved_pre.append(item)
            continue
        if role == "user" or (item_type in {"", "message"} and not role):
            latest_user_task = item

    if not has_post_user_message and latest_user_task is not None and latest_user_task not in preserved_pre:
        preserved_pre.append(latest_user_task)

    return [*preserved_pre, *post_items]


def _summarize_inline_data_image(image_url: str, *, detail=None) -> str | None:
    if not isinstance(image_url, str) or not image_url.startswith("data:"):
        return None

    media_type = "inline image"
    if ";" in image_url and image_url.startswith("data:"):
        media_type = image_url[5:].partition(";")[0] or media_type

    summary = f"[inline tool image omitted: {media_type}, {len(image_url)} chars]"
    if isinstance(detail, str) and detail.strip():
        summary = f"{summary[:-1]}, detail={detail.strip()}]"
    return summary


def _sanitize_function_call_output_item(item: dict) -> dict:
    if not isinstance(item, dict) or item.get("type") != "function_call_output":
        return item

    # GHCP rejects a stray ``content`` array on function_call_output items.
    if "content" in item:
        item = {k: v for k, v in item.items() if k != "content"}

    output = item.get("output")
    if not isinstance(output, list):
        return item

    changed = False
    sanitized_output = []
    for part in output:
        if not isinstance(part, dict):
            sanitized_output.append(part)
            continue

        if str(part.get("type", "")).lower() != "input_image":
            sanitized_output.append(part)
            continue

        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            image_url = image_url.get("url")
        summary_text = _summarize_inline_data_image(image_url, detail=part.get("detail"))
        if summary_text is None:
            sanitized_output.append(part)
            continue

        changed = True
        sanitized_output.append({"type": "input_text", "text": summary_text})

    if not changed:
        return item
    return {
        **item,
        "output": sanitized_output,
    }


def _reasoning_summary_has_text(summary) -> bool:
    if isinstance(summary, str):
        return bool(summary.strip())
    if not isinstance(summary, list):
        return False
    for part in summary:
        if isinstance(part, str) and part.strip():
            return True
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return True
    return False


def _reasoning_item_has_replay_value(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    encrypted = item.get("encrypted_content")
    if isinstance(encrypted, str) and encrypted:
        return True
    return _reasoning_summary_has_text(item.get("summary"))


def _message_text(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        for key in ("text", "input_text", "output_text"):
            text = part.get(key)
            if isinstance(text, str):
                parts.append(text)
                break
    return "".join(parts)


def _is_subagent_notification_message(item: dict) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    return _SUBAGENT_NOTIFICATION_ONLY_RE.match(_message_text(item)) is not None


def sanitize_input(
    input_items,
    *,
    preserve_encrypted_content: bool = True,
    drop_reasoning_items: bool = False,
    native_responses_passthrough: bool = False,
):
    """
    Preserve encrypted_content in reasoning items for normal multi-turn correctness.
    Callers may disable preservation for forked/subagent contexts where encrypted
    reasoning blobs came from a different prompt-cache lineage and upstream will
    reject them as unverifiable.
    Treat the latest compaction item as the active handoff boundary so older
    pre-compaction prompt items are not replayed upstream.
    Expand locally synthesized compaction items into a readable summary message.
    Convert other compaction items into reasoning items for GHCP compatibility.
    Strip status=None which GHCP rejects.
    Pass everything else through unchanged.
    """
    if not isinstance(input_items, list):
        return input_items  # plain string — pass through untouched

    window_items = _latest_compaction_window(input_items)

    result = []
    for item in window_items:
        if not isinstance(item, dict):
            result.append(item)
            continue

        # Codex Electron includes this client-only envelope on replayed input
        # items.  Its contents (including executed tool-call details and turn
        # IDs) are rewritten after a tool completes, so forwarding it changes
        # historical function_call_output items on the next request.  It is
        # not part of the Responses item schema and must never influence the
        # upstream prompt-cache prefix, including on native Codex passthrough.
        if "internal_chat_message_metadata_passthrough" in item:
            item = dict(item)
            item.pop("internal_chat_message_metadata_passthrough", None)

        if _is_subagent_notification_message(item):
            continue

        item_type = item.get("type")
        preserve_item_encrypted_content = preserve_encrypted_content
        if item_type == "compaction":
            encrypted_content = item.get("encrypted_content")
            summary_text = decode_fake_compaction(encrypted_content)
            if summary_text is not None:
                result.append(_summary_message_item(summary_text))
                continue
            if isinstance(encrypted_content, str) and encrypted_content:
                if preserve_item_encrypted_content:
                    if native_responses_passthrough:
                        result.append(item)
                    else:
                        result.append(
                            {
                                "type": "reasoning",
                                "encrypted_content": encrypted_content,
                            }
                        )
                # If the caller disabled encrypted replay, an opaque compaction
                # token has no local text we can safely forward. Drop it rather
                # than sending unverifiable ciphertext upstream.
                continue
            result.append(item)
            continue

        if item_type == "function_call_output" and not native_responses_passthrough:
            result.append(_sanitize_function_call_output_item(item))
            continue

        if item_type == "reasoning" and drop_reasoning_items:
            continue

        if item_type != "reasoning":
            # GHCP's Responses API rejects a non-empty ``content`` array on
            # non-message items (e.g. function_call, function_call_output).
            # ``agent_message`` is a message-like collaboration item used by
            # current Codex multi-agent requests and requires its ``content``
            # array. Codex sometimes echoes a stray empty/legacy ``content``
            # field on other items; strip it defensively so upstream does not
            # 400 with "Invalid 'input[N].content': array too long".
            if (
                isinstance(item, dict)
                and "content" in item
                and item_type not in (None, "message", "agent_message")
            ):
                cleaned = {k: v for k, v in item.items() if k != "content"}
                result.append(cleaned)
            else:
                result.append(item)
            continue

        filtered = {}
        for k, v in item.items():
            if k == "encrypted_content":
                if v is not None and preserve_item_encrypted_content:
                    filtered[k] = v   # preserve during normal same-lineage replay
                continue
            if k == "content":
                # GHCP's Responses API rejects ``content`` on reasoning items
                # ("array too long. Expected ... maximum length 0"). Reasoning
                # text belongs in ``summary`` / ``encrypted_content``; drop any
                # stray ``content`` payload here.
                continue
            if v is not None:
                filtered[k] = v
        if preserve_item_encrypted_content or _reasoning_item_has_replay_value(filtered):
            result.append(filtered)
    return result


def _compaction_message_item(role: str, text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    part_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": part_type, "text": text}],
    }


def _is_fake_compaction_summary_message(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type.lower() != "message":
        return False
    text = util.extract_item_text(item)
    return text.startswith(f"{FAKE_COMPACTION_SUMMARY_LABEL}\n")


def _format_compaction_tool_call(item: dict) -> str | None:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None

    arguments = item.get("arguments")
    if isinstance(arguments, str):
        arguments_text = arguments.strip() or "{}"
    elif arguments is None:
        arguments_text = "{}"
    else:
        arguments_text = json.dumps(arguments, separators=(",", ":"), ensure_ascii=False)

    call_id = item.get("call_id") or item.get("id")
    call_suffix = f" ({call_id})" if isinstance(call_id, str) and call_id else ""
    return f"[Tool call{call_suffix}] {name}\n{arguments_text}"


def _format_compaction_tool_output(item: dict) -> str | None:
    call_id = item.get("call_id")
    label = f"[Tool result ({call_id})]" if isinstance(call_id, str) and call_id else "[Tool result]"

    output = item.get("output")
    if isinstance(output, list):
        output_text = "".join(util.extract_item_text(part) for part in output if isinstance(part, dict))
    elif isinstance(output, str):
        output_text = output
    elif output is None:
        output_text = ""
    else:
        output_text = json.dumps(output, separators=(",", ":"), ensure_ascii=False)

    output_text = output_text.strip()
    return f"{label}\n{output_text}" if output_text else label


def _compaction_transcript_message_item(item: dict, *, force_user_role: bool) -> dict | None:
    role = str(item.get("role", "user")).lower()
    if role not in {"system", "developer", "user", "assistant"}:
        return None

    text = util.extract_item_text(item)
    if _is_fake_compaction_summary_message(item):
        return _compaction_message_item("user", text)
    if not force_user_role:
        return item
    if role != "user":
        text = f"[{role} message]\n{text}"
    return _compaction_message_item("user", text)


def _compaction_transcript_input_items(input_items, *, force_user_role: bool = False) -> list[dict]:
    transcript = []
    if not isinstance(input_items, list):
        return transcript

    for item in input_items:
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type", "")).lower()
        if item_type == "message":
            transcript_item = _compaction_transcript_message_item(item, force_user_role=force_user_role)
            if transcript_item is not None:
                transcript.append(transcript_item)
            continue

        if item_type == "function_call":
            transcript_role = "user" if force_user_role else "assistant"
            transcript_item = _compaction_message_item(transcript_role, _format_compaction_tool_call(item))
            if transcript_item is not None:
                transcript.append(transcript_item)
            continue

        if item_type == "function_call_output":
            transcript_item = _compaction_message_item("user", _format_compaction_tool_output(item))
            if transcript_item is not None:
                transcript.append(transcript_item)
            continue

        if item_type == "compaction":
            encrypted_content = item.get("encrypted_content")
            summary_text = decode_fake_compaction(encrypted_content)
            if summary_text is not None:
                transcript_item = _compaction_message_item("user", f"{FAKE_COMPACTION_SUMMARY_LABEL}\n{summary_text}")
                if transcript_item is not None:
                    transcript.append(transcript_item)
            continue

        # reasoning, item_reference, and unknown item types do not add useful
        # user-visible context for summarization.

    return transcript

def _apply_compaction_request_config(source: dict, target: dict) -> None:
    if not isinstance(source, dict) or not isinstance(target, dict):
        return

    for key, value in source.items():
        if key == "input":
            continue
        target[key] = value


def _strip_chat_transcript_compaction_fields(target: dict) -> None:
    if not isinstance(target, dict):
        return

    # GHCP keeps tools in compact requests for cache affinity but sets
    # tool_choice to "none" so the model cannot invoke them during
    # summarization.  Match that behaviour here.
    if isinstance(target.get("tools"), list) and target["tools"]:
        target["tool_choice"] = "none"
    else:
        for key in ("tools", "tool_choice"):
            target.pop(key, None)
    target.pop("parallel_tool_calls", None)


def build_fake_compaction_request(body: dict, *, force_responses_safe_transcript: bool = False) -> dict:
    request_input = body.get("input")
    if isinstance(request_input, list):
        request_input = sanitize_input(request_input)

    if force_responses_safe_transcript:
        if isinstance(request_input, list):
            input_items = _compaction_transcript_input_items(
                request_input,
                force_user_role=force_responses_safe_transcript,
            )
        elif isinstance(request_input, str):
            input_items = [_compaction_message_item("user", request_input)]
            input_items = [item for item in input_items if item is not None]
        else:
            input_items = []
    elif isinstance(request_input, list):
        input_items = list(request_input)
    elif isinstance(request_input, str):
        input_items = [_compaction_message_item("user", request_input)]
        input_items = [item for item in input_items if item is not None]
    else:
        input_items = []

    input_items.append({
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": COMPACTION_SUMMARY_PROMPT}],
    })

    compact_request = {"input": input_items}
    _apply_compaction_request_config(body, compact_request)
    if force_responses_safe_transcript:
        _strip_chat_transcript_compaction_fields(compact_request)
    return compact_request


def extract_response_output_text(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if not isinstance(output, list):
        return None

    parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        if str(item.get("role", "")).lower() != "assistant":
            continue
        text = util.extract_item_text(item).strip()
        if text:
            parts.append(text)

    if not parts:
        return None
    return "\n\n".join(parts)


def responses_to_compaction_response(payload: dict, fallback_model=None) -> dict:
    """Wrap a Responses summary as a Responses compact payload.

    If the payload already contains a native compaction item in ``output``, it is
    passed through. Otherwise, the assistant text is extracted and encoded in the
    proxy's local fake compaction format for downstream expansion.
    """
    if not isinstance(payload, dict):
        return {}
    output = payload.get("output")
    if isinstance(output, list) and any(
        isinstance(item, dict) and item.get("type") == "compaction" for item in output
    ):
        return payload
    summary_text = (extract_response_output_text(payload) or "").strip()
    if not summary_text:
        summary_text = "(no summary available)"

    return {
        "id": (payload.get("id") if isinstance(payload, dict) else None) or f"resp_{uuid4().hex}",
        "object": "response",
        "created_at": payload.get("created_at") if isinstance(payload, dict) else int(time.time()),
        "status": "completed",
        "model": fallback_model or (payload.get("model") if isinstance(payload, dict) else None),
        "output": [
            {
                "type": "compaction",
                "encrypted_content": encode_fake_compaction(summary_text),
            }
        ],
        "output_text": summary_text,
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
    }

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
