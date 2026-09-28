"""Build summary requests and encode their Responses compaction results."""

import json
import time
from uuid import uuid4

import util
from util import extract_response_output_text
from constants import FAKE_COMPACTION_SUMMARY_LABEL, COMPACTION_SUMMARY_PROMPT
from responses_input import (
    decode_fake_compaction,
    encode_fake_compaction,
    sanitize_input,
)


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
        arguments_text = json.dumps(
            arguments, separators=(",", ":"), ensure_ascii=False
        )

    call_id = item.get("call_id") or item.get("id")
    call_suffix = f" ({call_id})" if isinstance(call_id, str) and call_id else ""
    return f"[Tool call{call_suffix}] {name}\n{arguments_text}"


def _format_compaction_tool_output(item: dict) -> str | None:
    call_id = item.get("call_id")
    label = (
        f"[Tool result ({call_id})]"
        if isinstance(call_id, str) and call_id
        else "[Tool result]"
    )

    output = item.get("output")
    if isinstance(output, list):
        output_text = "".join(
            util.extract_item_text(part) for part in output if isinstance(part, dict)
        )
    elif isinstance(output, str):
        output_text = output
    elif output is None:
        output_text = ""
    else:
        output_text = json.dumps(output, separators=(",", ":"), ensure_ascii=False)

    output_text = output_text.strip()
    return f"{label}\n{output_text}" if output_text else label


def _compaction_transcript_message_item(
    item: dict, *, force_user_role: bool
) -> dict | None:
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


def _compaction_transcript_input_items(
    input_items, *, force_user_role: bool = False
) -> list[dict]:
    transcript = []
    if not isinstance(input_items, list):
        return transcript

    for item in input_items:
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type", "")).lower()
        if item_type == "message":
            transcript_item = _compaction_transcript_message_item(
                item, force_user_role=force_user_role
            )
            if transcript_item is not None:
                transcript.append(transcript_item)
            continue

        if item_type == "function_call":
            transcript_role = "user" if force_user_role else "assistant"
            transcript_item = _compaction_message_item(
                transcript_role, _format_compaction_tool_call(item)
            )
            if transcript_item is not None:
                transcript.append(transcript_item)
            continue

        if item_type == "function_call_output":
            transcript_item = _compaction_message_item(
                "user", _format_compaction_tool_output(item)
            )
            if transcript_item is not None:
                transcript.append(transcript_item)
            continue

        if item_type == "compaction":
            encrypted_content = item.get("encrypted_content")
            summary_text = decode_fake_compaction(encrypted_content)
            if summary_text is not None:
                transcript_item = _compaction_message_item(
                    "user", f"{FAKE_COMPACTION_SUMMARY_LABEL}\n{summary_text}"
                )
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


def build_fake_compaction_request(
    body: dict, *, force_responses_safe_transcript: bool = False
) -> dict:
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

    input_items.append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": COMPACTION_SUMMARY_PROMPT}],
        }
    )

    compact_request = {"input": input_items}
    _apply_compaction_request_config(body, compact_request)
    if force_responses_safe_transcript:
        _strip_chat_transcript_compaction_fields(compact_request)
    return compact_request


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
        "id": (payload.get("id") if isinstance(payload, dict) else None)
        or f"resp_{uuid4().hex}",
        "object": "response",
        "created_at": payload.get("created_at")
        if isinstance(payload, dict)
        else int(time.time()),
        "status": "completed",
        "model": fallback_model
        or (payload.get("model") if isinstance(payload, dict) else None),
        "output": [
            {
                "type": "compaction",
                "encrypted_content": encode_fake_compaction(summary_text),
            }
        ],
        "output_text": summary_text,
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
    }
