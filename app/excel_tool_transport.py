"""Decode and validate Excel relays before exposing a client tool batch."""

from __future__ import annotations

import hashlib
import json
import re
import time
from uuid import uuid4

import responses_replay_ids
import tool_schema
from excel_models import MODEL_ID
from excel_tool_history import (
    remember_native_call as _remember_native_call,
    remember_native_calls as _remember_native_calls,
)
from excel_tool_catalog import (
    CLIENT_MARKER_CALL_ID_PREFIX,
    NATIVE_FALLBACK_CALL_ID_PREFIX,
    TOOL_CALL_MARKER_OPEN,
    TOOL_CALL_MARKER_CLOSE,
    _RAW_TRANSPORT_PREFIX,
    _client_tool_specs,
    _is_transport_name,
    _original_client_tool_name,
    _raw_transport_fields,
    _tool_call_name,
    _tool_input_schema,
    client_tool_types,
)

_TOOL_CALL_PATTERN = re.compile(
    re.escape(TOOL_CALL_MARKER_OPEN)
    + r"\s*(\{.*?\})\s*"
    + re.escape(TOOL_CALL_MARKER_CLOSE),
    re.DOTALL,
)
_JSON_ESCAPE_CHARS = frozenset('"\\/bfnrt')


_PLAN_STATUS_BY_ALIAS = {
    "pending": "pending",
    "not_started": "pending",
    "todo": "pending",
    "planned": "pending",
    "queued": "pending",
    "blocked": "pending",
    "in_progress": "in_progress",
    "active": "in_progress",
    "started": "in_progress",
    "doing": "in_progress",
    "current": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "finished": "completed",
}


def _decode_transport_invocation(
    code: str, allowed_tools: dict[str, str]
) -> dict | None:
    # This is a data parser, not a JavaScript interpreter. The entire body must
    # be one catalog call with one JSON literal and no executable expressions.
    invocation = re.fullmatch(
        r"(?:return\s+)?(?:await\s+)?(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
        r"\s*\((?P<argument>.*)\)\s*;?",
        code,
        re.DOTALL | re.ASCII,
    )
    if invocation is None:
        return None
    name = _original_client_tool_name(invocation.group("name"), allowed_tools)
    if name is None:
        return None
    try:
        argument = json.loads(invocation.group("argument"), strict=False)
    except json.JSONDecodeError:
        return None
    if allowed_tools[name] == "function" and isinstance(argument, dict):
        return {"name": name, "arguments": argument}
    if allowed_tools[name] == "custom" and isinstance(argument, str):
        return {"name": name, "input": argument}
    return None


def _transport_decode_failure(
    diagnostics: dict | None,
    field: str,
    value: object,
    error: json.JSONDecodeError | None = None,
) -> None:
    if diagnostics is not None:
        diagnostics["transport_field"] = field
        diagnostics["transport_value_type"] = {
            str: "string",
            dict: "object",
            list: "array",
            type(None): "null",
            bool: "boolean",
            int: "number",
            float: "number",
        }.get(type(value), "unknown")
        if error is not None:
            # JSON exception text and source data may contain private content.
            diagnostics["json_line"] = error.lineno
            diagnostics["json_column"] = error.colno
            diagnostics["json_error"] = {
                "Invalid control character at": "unescaped_control_character",
                "Invalid \\escape": "invalid_escape",
                "Invalid \\uXXXX escape": "invalid_unicode_escape",
                "Unterminated string starting at": "unterminated_string",
                "Expecting ',' delimiter": "missing_comma",
                "Expecting ':' delimiter": "missing_colon",
                "Expecting property name enclosed in double quotes": "invalid_property_name",
                "Expecting value": "missing_value",
                "Extra data": "extra_data",
            }.get(error.msg, "invalid_json")
    return None


def _decode_transport_code(
    code: object,
    allowed_tools: dict[str, str],
    diagnostics: dict | None = None,
) -> dict | None:
    if isinstance(code, dict):
        return code
    if not isinstance(code, str):
        return _transport_decode_failure(diagnostics, "arguments.code", code)

    # Unwrap only known decorations and bounded extra JSON encoding. Searching
    # for an inner object can select an argument or silently discard a second
    # call; decode the entire payload instead, without evaluating JavaScript.
    for _ in range(3):
        code = code.strip()
        if code.startswith("```"):
            fence = re.fullmatch(
                r"```(?:json|javascript|js)?[ \t]*\r?\n(.*?)\r?\n```", code, re.DOTALL
            )
            if fence is None:
                return _transport_decode_failure(diagnostics, "arguments.code", code)
            code = fence.group(1).strip()
        invocation = _decode_transport_invocation(code, allowed_tools)
        if invocation is not None:
            return invocation
        statement = re.match(
            r"(?:(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*|return\s+)", code
        )
        if statement is not None:
            code = code[statement.end() :].strip()
            if code.endswith(";"):
                code = code[:-1].rstrip()
        try:
            # Raw line breaks/tabs in tool literals are data, not missing JSON
            # structure. Preserve them without guessing quotes or delimiters.
            envelope = json.loads(code, strict=False)
        except json.JSONDecodeError:
            try:
                envelope = json.loads(
                    _repair_invalid_json_backslashes(code), strict=False
                )
            except json.JSONDecodeError as exc:
                return _transport_decode_failure(
                    diagnostics, "arguments.code", code, exc
                )
        if isinstance(envelope, dict):
            return envelope
        if not isinstance(envelope, str):
            return _transport_decode_failure(diagnostics, "arguments.code", envelope)
        code = envelope
    return _transport_decode_failure(diagnostics, "arguments.code", code)


def _repair_invalid_json_backslashes(text: str) -> str:
    """Double invalid backslashes inside JSON string values."""
    repaired: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        character = text[index]
        if not in_string:
            repaired.append(character)
            if character == '"':
                in_string = True
            index += 1
            continue
        if character == '"':
            repaired.append(character)
            in_string = False
            index += 1
            continue
        if character != "\\":
            repaired.append(character)
            index += 1
            continue

        next_character = text[index + 1] if index + 1 < len(text) else ""
        valid_escape = next_character in _JSON_ESCAPE_CHARS
        if next_character == "u":
            valid_escape = index + 5 < len(text) and all(
                digit in "0123456789abcdefABCDEF"
                for digit in text[index + 2 : index + 6]
            )
        if valid_escape:
            repaired.extend((character, next_character))
            index += 2
        else:
            repaired.extend((character, character))
            index += 1
    return "".join(repaired)


def _raw_transport_envelope(
    arguments: dict, specs: dict, diagnostics: dict | None = None
) -> dict | None:
    marker = arguments.get("summary", "").split("/", 2)
    if len(marker) != 3:
        return None
    _, field, name = marker
    if not name or field not in {"cmd", "code", "input"}:
        return None
    text = arguments.get("code")
    if not isinstance(text, str):
        return None
    try:
        metadata = json.loads(arguments.get("extended_summary", "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    if field in metadata and metadata[field] != text:
        return None
    if name not in specs:
        if diagnostics is not None:
            diagnostics.update(
                _unknown_tool_diagnostics(
                    name, {key: info["type"] for key, info in specs.items()}, "raw"
                )
            )
        return _reject_native_tool_call(diagnostics, "unknown_tool")
    if field not in _raw_transport_fields(specs[name]):
        return None
    if specs[name]["type"] == "custom":
        if metadata.keys() - {"input"}:
            return None
        return {"name": name, "input": text}
    return {"name": name, "arguments": {**metadata, field: text}}


def _transport_envelope(
    native: dict,
    allowed_tools: dict[str, str],
    diagnostics: dict | None = None,
    *,
    specs: dict | None = None,
) -> dict | None:
    if native.get("type") != "function_call" or not _is_transport_name(
        _tool_call_name(native)
    ):
        return None
    arguments = native.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return _transport_decode_failure(diagnostics, "arguments", arguments, exc)
    if not isinstance(arguments, dict):
        return _transport_decode_failure(diagnostics, "arguments", arguments)
    summary = arguments.get("summary")
    if isinstance(summary, str) and summary.startswith(_RAW_TRANSPORT_PREFIX):
        raw_specs = {
            key: info for key, info in (specs or {}).items() if key in allowed_tools
        }
        envelope = _raw_transport_envelope(arguments, raw_specs, diagnostics)
        if envelope is None and (
            diagnostics is None or diagnostics.get("reason") != "unknown_tool"
        ):
            return _transport_decode_failure(diagnostics, "raw_transport", None)
        return envelope
    envelope = _decode_transport_code(arguments.get("code"), allowed_tools, diagnostics)
    for _ in range(2):
        if envelope is None or not _is_transport_name(_tool_call_name(envelope)):
            break
        nested_arguments = envelope.get("arguments")
        if isinstance(nested_arguments, str):
            try:
                nested_arguments = json.loads(nested_arguments)
            except json.JSONDecodeError as exc:
                return _transport_decode_failure(
                    diagnostics, "nested.arguments", nested_arguments, exc
                )
        if not isinstance(nested_arguments, dict):
            return _transport_decode_failure(
                diagnostics, "nested.arguments", nested_arguments
            )
        envelope = _decode_transport_code(
            nested_arguments.get("code"), allowed_tools, diagnostics
        )
    if envelope is not None and _is_transport_name(_tool_call_name(envelope)):
        return _transport_decode_failure(
            diagnostics, "nested.transport_limit", envelope
        )
    return envelope


def _value_matches_schema(value: object, schema: object) -> bool:
    return tool_schema.arguments_match_schema(value, schema)


def _normalize_plan_status(status: object) -> str | None:
    if not isinstance(status, str):
        return None
    key = status.strip().lower().replace("-", "_").replace(" ", "_")
    return _PLAN_STATUS_BY_ALIAS.get(key, status)


def _normalize_native_function_arguments(name: str, arguments: dict) -> dict:
    if name != "update_plan":
        return arguments
    plan = arguments.get("plan")
    if not isinstance(plan, list):
        return arguments
    normalized_plan = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        if not isinstance(step, str):
            step = item.get("description")
        if not isinstance(step, str):
            step = item.get("title")
        status = _normalize_plan_status(item.get("status"))
        if isinstance(step, str) and isinstance(status, str):
            normalized_plan.append({"step": step, "status": status})
    normalized: dict[str, object] = {"plan": normalized_plan}
    explanation = arguments.get("explanation")
    if not isinstance(explanation, str):
        explanation = arguments.get("summary")
    if isinstance(explanation, str) and explanation:
        normalized["explanation"] = explanation
    return normalized


def _restore_native_function_arguments(name: str, arguments: object) -> object:
    """Restore the Basispoints schema after a native call visits Codex."""
    if name != "update_plan":
        return arguments
    parsed = arguments
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return arguments
    if not isinstance(parsed, dict) or not isinstance(parsed.get("plan"), list):
        return arguments

    native_plan: list[dict[str, str]] = []
    for index, item in enumerate(parsed["plan"]):
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        status = item.get("status")
        if not isinstance(step, str) or not isinstance(status, str):
            continue
        native_plan.append(
            {
                "id": f"step{index + 1}",
                "description": step,
                "status": status,
                "result": "",
            }
        )
    explanation = parsed.get("explanation")
    native = {
        "summary": (
            explanation
            if isinstance(explanation, str) and explanation
            else "Update task plan"
        ),
        "plan": native_plan,
    }
    return json.dumps(native, separators=(",", ":"), ensure_ascii=False)


def _reject_native_tool_call(diagnostics: dict | None, reason: str) -> None:
    if diagnostics is not None:
        diagnostics["reason"] = reason
    return None


def _unknown_tool_diagnostics(
    name: object, allowed_tools: dict[str, str], origin: str
) -> dict:
    """Log bounded identifiers and catalog identity, never arguments or schemas."""

    def identifier(value):
        return (
            value
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", value)
            else None
        )

    names = sorted(allowed_tools)
    catalog = json.dumps(
        [(key, allowed_tools[key]) for key in names], separators=(",", ":")
    )
    return {
        "target_name": identifier(name),
        "target_fingerprint": hashlib.sha256(json.dumps(name).encode()).hexdigest()[
            :16
        ],
        "target_source": origin,
        "declared_tools": [key for key in names if identifier(key) is not None][:64],
        "declared_tool_count": len(names),
        "catalog_fingerprint": hashlib.sha256(catalog.encode()).hexdigest()[:16],
    }


def extract_native_client_tool_calls(
    response: dict | None,
    source: dict,
    *,
    diagnostics: dict | None = None,
) -> list[dict[str, str]] | None:
    """Validate the whole tool batch before exposing any executable work."""
    if diagnostics is not None:
        diagnostics.clear()
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        return _reject_native_tool_call(diagnostics, "missing_completed_tool_call")
    native_calls = [
        item
        for item in response["output"]
        if isinstance(item, dict)
        and item.get("type") in {"function_call", "custom_tool_call"}
    ]
    if not native_calls:
        return _reject_native_tool_call(diagnostics, "missing_completed_tool_call")
    if source.get("parallel_tool_calls") is False and len(native_calls) > 1:
        return _reject_native_tool_call(diagnostics, "parallel_tools_disabled")
    converted = []
    for index, native in enumerate(native_calls):
        if native.get("status") not in (None, "completed"):
            if diagnostics is not None:
                diagnostics["tool_call_index"] = index
            return _reject_native_tool_call(diagnostics, "incomplete_tool_call")
        call = extract_native_client_tool_call(
            {"output": [native]}, source, diagnostics=diagnostics, remember=False
        )
        if call is None:
            if diagnostics is not None:
                diagnostics["tool_call_index"] = index
            return None
        converted.append(call)
    if any(
        len({call[key] for call in converted}) != len(converted)
        for key in ("call_id", "id")
    ):
        return _reject_native_tool_call(diagnostics, "duplicate_tool_identity")
    if not _remember_native_calls(native_calls):
        return _reject_native_tool_call(diagnostics, "replay_cache_capacity")
    return converted


def extract_native_client_tool_call(
    response: dict | None,
    source: dict,
    *,
    diagnostics: dict | None = None,
    remember: bool = True,
) -> dict[str, str] | None:
    if not isinstance(response, dict):
        return _reject_native_tool_call(diagnostics, "missing_completed_tool_call")
    specs = _client_tool_specs(source)
    output = response.get("output")
    if not isinstance(output, list):
        return _reject_native_tool_call(diagnostics, "missing_completed_tool_call")
    native_calls = [
        item
        for item in output
        if isinstance(item, dict)
        and item.get("type") in {"function_call", "custom_tool_call"}
    ]
    if len(native_calls) != 1:
        return _reject_native_tool_call(diagnostics, "invalid_tool_call_count")
    native = native_calls[0]
    allowed_tools = client_tool_types(source)
    transport_diagnostics = {}
    envelope = _transport_envelope(
        native, allowed_tools, transport_diagnostics, specs=specs
    )
    if diagnostics is not None:
        diagnostics.update(transport_diagnostics)
    if _is_transport_name(_tool_call_name(native)) and envelope is None:
        return _reject_native_tool_call(
            diagnostics,
            transport_diagnostics.get("reason", "invalid_transport_envelope"),
        )
    name = (
        _original_client_tool_name(_tool_call_name(envelope), allowed_tools)
        if envelope is not None
        else _original_client_tool_name(_tool_call_name(native), allowed_tools)
    )
    if name is None or name not in specs:
        if diagnostics is not None:
            diagnostics.update(
                _unknown_tool_diagnostics(
                    _tool_call_name(envelope if envelope is not None else native),
                    allowed_tools,
                    "envelope" if envelope is not None else "native",
                )
            )
        return _reject_native_tool_call(diagnostics, "unknown_tool")
    tool_info = specs[name]
    spec = tool_info["spec"]
    expected_type = tool_info["type"]
    if expected_type == "function":
        if envelope is not None:
            arguments = envelope.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    return _reject_native_tool_call(
                        diagnostics, "invalid_function_arguments"
                    )
        else:
            if native.get("type") != "function_call":
                return _reject_native_tool_call(diagnostics, "tool_type_mismatch")
            raw_arguments = native.get("arguments")
            if not isinstance(raw_arguments, str):
                return _reject_native_tool_call(
                    diagnostics, "invalid_function_arguments"
                )
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return _reject_native_tool_call(
                    diagnostics, "invalid_function_arguments"
                )
        if not isinstance(arguments, dict):
            return _reject_native_tool_call(diagnostics, "invalid_function_arguments")
        if envelope is None:
            arguments = _normalize_native_function_arguments(name, arguments)
        input_schema = _tool_input_schema(spec)
        if not _value_matches_schema(arguments, input_schema):
            return _reject_native_tool_call(diagnostics, "arguments_schema_mismatch")
        native_call_id = native.get("call_id")
        call_id = (
            native_call_id
            if isinstance(native_call_id, str) and native_call_id
            else f"{NATIVE_FALLBACK_CALL_ID_PREFIX}{uuid4().hex}"
        )
        native_item_id = native.get("id")
        if remember and not _remember_native_call(native):
            return _reject_native_tool_call(diagnostics, "replay_cache_capacity")
        result = {
            "type": "function_call",
            "id": (
                native_item_id
                if isinstance(native_item_id, str) and native_item_id
                else responses_replay_ids.function_item_id(call_id)
            ),
            "call_id": call_id,
            "name": tool_info["name"],
            "arguments": json.dumps(
                arguments,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        }
        if tool_info["namespace"]:
            result["namespace"] = tool_info["namespace"]
        return result
    if expected_type == "custom":
        custom_input = (
            envelope.get("input") if envelope is not None else native.get("input")
        )
        # Some native function relays wrap the freeform patch as a single
        # argument. Unwrap only this exact shape, without rewriting the patch.
        if envelope is not None and "input" not in envelope and name == "apply_patch":
            arguments = envelope.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    return _reject_native_tool_call(diagnostics, "invalid_custom_input")
            if isinstance(arguments, dict) and set(arguments) == {"patch"}:
                custom_input = arguments["patch"]
        if envelope is None and native.get("type") != "custom_tool_call":
            return _reject_native_tool_call(diagnostics, "tool_type_mismatch")
        if not isinstance(custom_input, str):
            return _reject_native_tool_call(diagnostics, "invalid_custom_input")
        native_call_id = native.get("call_id")
        call_id = (
            native_call_id
            if isinstance(native_call_id, str) and native_call_id
            else f"{NATIVE_FALLBACK_CALL_ID_PREFIX}{uuid4().hex}"
        )
        native_item_id = native.get("id")
        if remember and not _remember_native_call(native):
            return _reject_native_tool_call(diagnostics, "replay_cache_capacity")
        result = {
            "type": "custom_tool_call",
            "id": (
                native_item_id
                if (
                    envelope is None
                    and isinstance(native_item_id, str)
                    and native_item_id
                )
                else f"ctc_{call_id}"
            ),
            "call_id": call_id,
            "name": tool_info["name"],
            "input": custom_input,
        }
        if tool_info["namespace"]:
            result["namespace"] = tool_info["namespace"]
        return result
    return _reject_native_tool_call(diagnostics, "tool_type_mismatch")


def extract_client_tool_call(
    text: str,
    allowed_tools: dict[str, str],
) -> dict[str, str] | None:
    if not isinstance(text, str) or not allowed_tools:
        return None
    match = _TOOL_CALL_PATTERN.search(text)
    if match is None:
        return None
    try:
        marker = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(marker, dict):
        return None
    marker_name = marker.get("name")
    if not isinstance(marker_name, str):
        return None
    name = _original_client_tool_name(marker_name.strip(), allowed_tools)
    if name is None:
        return None
    tool_type = allowed_tools.get(name)
    if tool_type == "function":
        arguments = marker.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(arguments, dict):
            return None
        call_id = f"{CLIENT_MARKER_CALL_ID_PREFIX}{uuid4().hex}"
        return {
            "type": "function_call",
            "id": responses_replay_ids.function_item_id(call_id),
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(
                arguments,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        }
    if tool_type == "custom":
        custom_input = marker.get("input")
        if not isinstance(custom_input, str):
            return None
        call_id = f"{CLIENT_MARKER_CALL_ID_PREFIX}{uuid4().hex}"
        return {
            "type": "custom_tool_call",
            "id": f"ctc_{call_id}",
            "call_id": call_id,
            "name": name,
            "input": custom_input,
        }
    return None


def response_payload_with_tool_calls(
    response: dict | None,
    tool_calls: list[dict[str, str]],
    *,
    model_id: str = MODEL_ID,
) -> dict[str, object]:
    result = dict(response or {})
    result.setdefault("id", f"resp_{uuid4().hex}")
    result.setdefault("object", "response")
    result.setdefault("created_at", int(time.time()))
    result["status"] = "completed"
    result["model"] = model_id
    completed_calls = [{**call, "status": "completed"} for call in tool_calls]
    existing_output = result.get("output")
    output = (
        [item for item in existing_output if isinstance(item, dict)]
        if isinstance(existing_output, list)
        else []
    )
    native_indices = [
        index
        for index, item in enumerate(output)
        if item.get("type") in {"function_call", "custom_tool_call"}
    ]
    for index, call in zip(native_indices, completed_calls):
        output[index] = call
    result["output"] = output if native_indices else completed_calls
    result["error"] = None
    result["incomplete_details"] = None
    return result


def response_payload_with_tool_call(
    response: dict | None,
    tool_call: dict[str, str],
    *,
    model_id: str = MODEL_ID,
) -> dict[str, object]:
    return response_payload_with_tool_calls(response, [tool_call], model_id=model_id)
