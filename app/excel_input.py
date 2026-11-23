"""Translate complete client history into the Basispoints input vocabulary."""

from __future__ import annotations

import hashlib
import json

from excel_images import normalize_input_content
from excel_models import normalize_reasoning_effort
from excel_tool_catalog import CLIENT_TOOL_TRANSPORT_NAME, _additional_tool_specs
from responses_input import _summary_message_item, decode_fake_compaction


def _message_item(role: str, text: str) -> dict:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }


def _history_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        # Go's encoding/json uses these escapes in the reference envelopes and IDs.
        for character, escaped in (
            ("&", "\\u0026"),
            ("<", "\\u003c"),
            (">", "\\u003e"),
            ("\u2028", "\\u2028"),
            ("\u2029", "\\u2029"),
        ):
            encoded = encoded.replace(character, escaped)
        return encoded
    except (TypeError, ValueError) as exc:
        raise ValueError("Excel history tool arguments cannot be serialized.") from exc


def _history_fingerprint(value: str) -> str:
    return hashlib.sha256(_history_json(value).encode("utf-8")).hexdigest()[:32]


def history_configuration_effort(item: dict) -> str:
    config = item.get("reasoning")
    if (
        item.keys() - {"type", "id", "reasoning", "internal_chat_message_metadata_passthrough"}
        or not isinstance(config, dict)
        or config.keys() - {"mode", "effort"}
        or config.get("mode", "standard") != "standard"
    ):
        raise ValueError("Unsupported reasoning configuration_update in input history.")
    value = config.get("effort")
    effort = normalize_reasoning_effort(value) if value else None
    if effort is None:
        raise ValueError("Unsupported reasoning effort in configuration_update.")
    return effort


def _fallback_transport_call(item: dict, *, tool_specs: dict | None = None) -> dict:
    """Rebuild a native call using only the complete client-supplied history."""
    call_id, name = item.get("call_id"), item.get("name")
    if any(
        not isinstance(value, str) or not value or value.strip() != value
        for value in (call_id, name)
    ):
        raise ValueError("Excel history tool calls require nonempty call_id and name.")
    if "namespace" in item:
        namespace = item["namespace"]
        if not isinstance(namespace, str) or namespace.strip() != namespace:
            raise ValueError(
                "Excel history tool namespace must be a string without surrounding whitespace."
            )
        if namespace:
            name = namespace + "." + name
    envelope: dict[str, object] = {"name": name}
    if item.get("type") == "custom_tool_call":
        custom_input = item.get("input")
        if not isinstance(custom_input, str):
            raise ValueError("Excel history custom tool input must be a string.")
        envelope["input"] = custom_input
    elif item.get("type") == "function_call":
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "Excel history function arguments must contain one valid JSON object."
                ) from exc
        if not isinstance(arguments, dict):
            raise ValueError("Excel history function arguments must be a JSON object.")
        envelope["arguments"] = arguments
    else:
        raise ValueError("Excel history requires a function or custom tool call.")
    native_arguments = {
        "code": _history_json(envelope),
        "summary": "Replay a previously requested client tool",
        "extended_summary": "The supplied client history contains this tool call; consume its recorded result without repeating it.",
        "destructive": False,
        "references": [],
    }
    item_id = item.get("id")
    if (
        not isinstance(item_id, str)
        or not item_id.startswith("fc_")
        or len(item_id.encode("utf-8")) > 64
    ):
        item_id = "fc_" + _history_fingerprint(call_id)
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": CLIENT_TOOL_TRANSPORT_NAME,
        "arguments": _history_json(native_arguments),
        "status": "completed",
    }


def translate_input_items(
    raw_input: object,
    allowed_tools: dict[str, str] | None = None,
    *,
    tool_specs: dict | None = None,
) -> list:
    """Preserve supplied history and reconstruct its tool calls within this request."""
    if isinstance(raw_input, str):
        return [_message_item("user", raw_input)]
    if not isinstance(raw_input, list):
        raise ValueError("Excel input must be a string or an array of objects.")

    seen_calls: set[str] = set()
    result: list = []
    trigger = None
    for index, original in enumerate(raw_input):
        if not isinstance(original, dict):
            raise ValueError(f"Excel input items must be objects (path=input[{index}]).")
        item = dict(original)
        item.pop("internal_chat_message_metadata_passthrough", None)
        item_type = item.get("type") if isinstance(item.get("type"), str) else ""
        if item_type == "additional_tools":
            _additional_tool_specs(item)
            continue
        if item_type == "configuration_update":
            history_configuration_effort(item)
            continue
        if item_type == "item_reference":
            raise ValueError(
                "Excel requires full history; item_reference is unsupported "
                f"(path=input[{index}])."
            )
        if item_type == "compaction_trigger":
            trigger = item
            continue
        if item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                result.append(
                    {"type": "reasoning", "summary": [], "encrypted_content": encrypted}
                )
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            item = _fallback_transport_call(item)
            seen_calls.add(item["call_id"])
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id not in seen_calls:
                raise ValueError(
                    "Excel requires the original tool call before its result in the supplied history."
                )
            item["type"] = "function_call_output"
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                item_id = "fc_" + call_id
            if not item_id.startswith("fc_") or len(item_id.encode("utf-8")) > 64:
                item_id = "fc_" + _history_fingerprint(item_id)
            item["id"] = item_id
        elif item_type == "compaction":
            # The local /responses/compact endpoint emits a readable-summary
            # token. Native compaction and the history before it stay intact.
            summary = decode_fake_compaction(item.get("encrypted_content"))
            if summary is not None:
                item = _summary_message_item(summary)
        result.append(normalize_input_content(item, index=index))
    if trigger is not None:
        result.append(trigger)
    return result
