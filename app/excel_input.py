"""Translate client conversation history into replayable Excel input items."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import responses_replay_ids
from responses_input import sanitize_input
from excel_tool_catalog import (
    CLIENT_MARKER_CALL_ID_PREFIX,
    CLIENT_TOOL_TRANSPORT_NAME,
    NATIVE_FALLBACK_CALL_ID_PREFIX,
    _RAW_TRANSPORT_PREFIX,
    _TRANSPORT_RETRY_GUIDANCE,
    _is_transport_name,
    _raw_transport_fields,
    _tool_call_name,
    relay_tool_name,
)
from excel_tool_history import remembered_native_calls as _remembered_native_calls
from excel_tool_transport import (
    _restore_native_function_arguments,
    extract_native_client_tool_call,
)


def _message_item(role: str, text: str) -> dict:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }


def _item_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def _normalized_tool_output(
    item: dict,
    call_origins: dict[str, str],
    call_item_ids: set[str],
) -> dict:
    call_id = item.get("call_id")
    origin = call_origins.get(call_id) if isinstance(call_id, str) else None
    normalized = item
    if origin in {"update_plan", "functions.update_plan"}:
        # The Basispoints update_plan executor returns this object. Codex's
        # client-side status tool instead returns the display string
        # "Plan updated"; replaying that string leaves the server-native tool
        # state unresolved and makes the model plan again.
        normalized = {**normalized, "output": '{"status":"ok"}'}
    if (
        _is_transport_name(origin)
        and isinstance(call_id, str)
        and call_id
        and normalized.get("type") == "custom_tool_call_output"
    ):
        normalized = {**normalized, "type": "function_call_output"}
    if (
        isinstance(call_id, str)
        and call_id
        and normalized.get("type") == "function_call_output"
    ):
        item_id = normalized.get("id")
        if (
            not isinstance(item_id, str)
            or not item_id.startswith("fc_")
            or len(item_id) > 64
            or item_id in call_item_ids
        ):
            identity = json.dumps(
                [call_id, item_id], separators=(",", ":"), ensure_ascii=False
            )
            result_id = (
                "fc_output_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:54]
            )
            normalized = {**normalized, "id": result_id}
    output_text = _item_text(normalized.get("output"))
    if _is_transport_name(origin) and output_text.strip().lower().startswith(
        "unsupported call: run_officejs"
    ):
        return {**normalized, "output": _TRANSPORT_RETRY_GUIDANCE}
    if not output_text.strip() and isinstance(
        normalized.get("output"), (str, type(None))
    ):
        # A blank body reads as a failed call and provokes retries; make
        # success explicit.
        return {**normalized, "output": "(tool call succeeded with no output)"}
    return normalized


def _fallback_transport_call(item: dict, *, tool_specs: dict | None = None) -> dict:
    """Rebuild a transport call if the proxy restarted between call and result."""
    name = _tool_call_name(item) or ""
    if item.get("type") == "custom_tool_call":
        envelope: dict[str, object] = {
            "name": name,
            "input": item.get("input") if isinstance(item.get("input"), str) else "",
        }
    else:
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        envelope = {"name": name, "arguments": arguments}
    call_id = str(
        item.get("call_id") or f"{NATIVE_FALLBACK_CALL_ID_PREFIX}{uuid4().hex}"
    )
    native_arguments = {
        "summary": f"Run client tool {name}",
        "extended_summary": f"Relay {name} through the external Codex client",
        "code": json.dumps(envelope, separators=(",", ":"), ensure_ascii=False),
        "destructive": False,
        "references": [],
    }
    # Rebuilt history is also an example for the next generation. Match the
    # current catalog's raw transport instead of reintroducing nested source JSON.
    info = (tool_specs or {}).get(name)
    kind = "custom" if item.get("type") == "custom_tool_call" else "function"
    if info is not None and info["type"] == kind:
        payload = (
            {"input": envelope["input"]} if kind == "custom" else envelope["arguments"]
        )
        for field in _raw_transport_fields(info):
            if isinstance(payload.get(field), str):
                native_arguments.update(
                    summary=f"{_RAW_TRANSPORT_PREFIX}{field}/{name}",
                    code=payload[field],
                    extended_summary=json.dumps(
                        {key: value for key, value in payload.items() if key != field},
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                )
                break
    return {
        "type": "function_call",
        "id": responses_replay_ids.function_item_id(call_id),
        "call_id": call_id,
        "name": CLIENT_TOOL_TRANSPORT_NAME,
        "arguments": json.dumps(
            native_arguments,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        "status": "completed",
    }


def _strip_client_only_item_metadata(item: dict) -> dict:
    """Remove caller transport metadata that destabilizes prompt caching.

    Codex stamps every input item with an
    ``internal_chat_message_metadata_passthrough.turn_id``. The value is not
    part of the Responses item vocabulary Basispoints needs, and identical
    developer/environment messages receive a different value in every new
    conversation. Forwarding it therefore makes byte-identical prompt content
    diverge immediately after the cached tool catalog.

    Native calls remembered from the Basispoints response bypass this helper
    and are replayed exactly, preserving the server item identity required by
    encrypted reasoning.
    """
    if "internal_chat_message_metadata_passthrough" not in item:
        return item
    sanitized = dict(item)
    sanitized.pop("internal_chat_message_metadata_passthrough", None)
    return sanitized


def _native_call_matches_history(native: dict, item: dict) -> bool:
    """A reused call ID must never replace the client's recorded tool input."""
    name = _tool_call_name(item)
    tool_type = {"function_call": "function", "custom_tool_call": "custom"}.get(
        item.get("type")
    )
    if not name or not tool_type:
        return False
    declaration = {"type": tool_type, "name": name}
    if tool_type == "function":
        # Only replay matching uses this synthetic declaration. Live dispatch
        # always requires the caller's explicit field schema.
        declaration["parameters"] = {
            "properties": {field: {"type": "string"} for field in ("cmd", "code")}
        }
    converted = extract_native_client_tool_call(
        {"output": [native]},
        {"tools": [declaration]},
        remember=False,
    )
    if converted is None or converted["call_id"] != item.get("call_id"):
        return False
    if tool_type == "custom":
        return converted["input"] == item.get("input")
    try:
        # Sorting keys ignores JSON formatting while preserving integer precision
        # and distinguishing booleans from numbers (True == 1 in Python).
        supplied = json.loads(item["arguments"])
        expected = json.loads(converted["arguments"])
        return json.dumps(supplied, sort_keys=True) == json.dumps(
            expected, sort_keys=True
        )
    except (KeyError, TypeError, ValueError):
        return False


def translate_input_items(
    raw_input: object,
    allowed_tools: dict[str, str] | None = None,
    *,
    tool_specs: dict | None = None,
) -> list:
    """Map Codex Responses input items onto the Excel wire vocabulary.

    Tool-call history keeps the native Responses items that Basispoints needs
    for its state machine. New client calls use Basispoints' declared
    ``run_officejs`` function as an intercepted transport. The original native
    item is retained on disk and in a bounded memory cache because Codex drops its server item ID
    when submitting the tool result; replaying only the name and call_id makes
    encrypted reasoning treat the result as unrelated. Native update_plan
    results are translated from Codex's display text to the ``{"status":"ok"}``
    object returned by Excel's real executor. Calls created by older proxy
    versions retain their namespaced marker representation.
    Reasoning items that carry ``encrypted_content`` (which the upstream issues
    by default) are replayed unchanged for turn-to-turn continuity; bare
    reasoning items are dropped because with ``store: false`` the upstream
    rejects them. The rendering is deterministic so replayed turns serialize
    identically on every request and keep the upstream prompt-cache prefix
    stable.
    """
    if isinstance(raw_input, str):
        return [_message_item("user", raw_input)]
    if not isinstance(raw_input, list):
        return []

    if any(
        isinstance(item, dict) and item.get("type") == "compaction"
        for item in raw_input
    ):
        raw_input = sanitize_input(raw_input, native_responses_passthrough=True)

    remembered_calls = _remembered_native_calls(
        [
            item["call_id"]
            for item in raw_input
            if isinstance(item, dict)
            and item.get("type") in {"function_call", "custom_tool_call"}
            and isinstance(item.get("call_id"), str)
            and item["call_id"]
        ]
    )

    call_origins: dict[str, str] = {}
    call_item_ids: set[str] = set()
    result: list = []
    for item in raw_input:
        if not isinstance(item, dict):
            continue
        item = _strip_client_only_item_metadata(item)
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"function_call", "custom_tool_call"}:
            name = _tool_call_name(item)
            call_id = item.get("call_id")
            marker_relay = isinstance(call_id, str) and call_id.startswith(
                CLIENT_MARKER_CALL_ID_PREFIX
            )
            remembered = (
                remembered_calls.get(call_id) if isinstance(call_id, str) else None
            )
            if remembered is not None and _native_call_matches_history(
                remembered, item
            ):
                native_name = _tool_call_name(remembered)
                if isinstance(call_id, str) and isinstance(native_name, str):
                    call_origins[call_id] = native_name
                result.append(remembered)
            elif isinstance(name, str) and name and marker_relay:
                upstream_name = relay_tool_name(name)
                if isinstance(call_id, str):
                    call_origins[call_id] = upstream_name
                result.append({**item, "name": upstream_name})
            elif isinstance(name, str) and name:
                if name == "update_plan":
                    if isinstance(call_id, str):
                        call_origins[call_id] = name
                    result.append(
                        {
                            **item,
                            "arguments": _restore_native_function_arguments(
                                name,
                                item.get("arguments"),
                            ),
                        }
                    )
                else:
                    fallback = _fallback_transport_call(item, tool_specs=tool_specs)
                    if isinstance(call_id, str):
                        call_origins[call_id] = CLIENT_TOOL_TRANSPORT_NAME
                    result.append(fallback)
            else:
                result.append(item)
            if isinstance(result[-1].get("id"), str):
                call_item_ids.add(result[-1]["id"])
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            result.append(_normalized_tool_output(item, call_origins, call_item_ids))
            continue
        if item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                result.append(
                    {
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": encrypted,
                    }
                )
            continue
        if item_type == "item_reference":
            continue
        result.append(item)
    return result
