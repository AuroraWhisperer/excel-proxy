"""Normalize replayed Responses input and preserve compaction boundaries."""

import base64
import re

from constants import FAKE_COMPACTION_PREFIX, FAKE_COMPACTION_SUMMARY_LABEL

_SUBAGENT_NOTIFICATION_ONLY_RE = re.compile(
    r"\A\s*(?:(?:"
    r"<subagent[_-]notification>\s*.*?\s*</subagent[_-]notification>"
    r"|<task-notification>\s*.*?\s*</task-notification>"
    r")\s*)+\Z",
    re.DOTALL,
)


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
    return any(
        isinstance(item, dict) and item.get("type") == "compaction"
        for item in input_items
    )


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
            item.get("type")
            in {
                "function_call",
                "custom_tool_call",
                "function_call_output",
                "custom_tool_call_output",
                "reasoning",
            }
            or (
                item.get("type") in (None, "message")
                and str(item.get("role", "")).lower() == "assistant"
            )
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
        if any(
            marker in text
            for marker in (
                "<environment_context>",
                "<permissions instructions>",
                "<skills_instructions>",
                "<instructions>",
                "# AGENTS.md",
            )
        ):
            preserved_pre.append(item)
            continue
        if role == "user" or (item_type in {"", "message"} and not role):
            latest_user_task = item

    if (
        not has_post_user_message
        and latest_user_task is not None
        and latest_user_task not in preserved_pre
    ):
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
        summary_text = _summarize_inline_data_image(
            image_url, detail=part.get("detail")
        )
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
                    filtered[k] = v  # preserve during normal same-lineage replay
                continue
            if k == "content":
                # GHCP's Responses API rejects ``content`` on reasoning items
                # ("array too long. Expected ... maximum length 0"). Reasoning
                # text belongs in ``summary`` / ``encrypted_content``; drop any
                # stray ``content`` payload here.
                continue
            if v is not None:
                filtered[k] = v
        if preserve_item_encrypted_content or _reasoning_item_has_replay_value(
            filtered
        ):
            result.append(filtered)
    return result


def _append_before_terminal_compaction_trigger(
    input_items: list,
    injected_items: list,
) -> list:
    """Insert proxy-authored items without displacing a compact trigger."""
    if (
        input_items
        and isinstance(input_items[-1], dict)
        and input_items[-1].get("type") == "compaction_trigger"
    ):
        return input_items[:-1] + injected_items + [input_items[-1]]
    return input_items + injected_items
