"""Create bounded tool retries and prove that corrections preserve input."""

from __future__ import annotations

import json

from excel_tool_catalog import (
    _RAW_TRANSPORT_GUIDANCE,
    _RAW_TRANSPORT_PREFIX,
    _TRANSPORT_RETRY_GUIDANCE,
    _client_tool_specs,
    _is_transport_name,
    _raw_transport_fields,
    _tool_call_name,
    client_tool_types,
)
from excel_tool_transport import _transport_envelope, extract_native_client_tool_call
from responses_input import _append_before_terminal_compaction_trigger


def tool_call_failure_message(diagnostics: dict) -> str:
    message = "Excel returned a tool call that cannot be translated to a client tool"
    reason = diagnostics.get("reason", "missing_completed_tool_call")
    if reason == "replay_cache_capacity":
        return (
            "Excel tool replay payload exceeds the local cache capacity. "
            "No client tool in this batch was executed. Reduce the tool input or batch size."
        )
    index = diagnostics.get("tool_call_index")
    detail = f"call {index + 1}: {reason}" if isinstance(index, int) else reason
    for key, label in (
        ("transport_field", "field"),
        ("transport_value_type", "type"),
        ("json_line", "line"),
        ("json_column", "column"),
        ("json_error", "json"),
    ):
        if key in diagnostics:
            detail += f"; {label}={diagnostics[key]}"
    if reason == "unknown_tool":
        return (
            f"{message} ({detail}). No client tool in this batch was executed. "
            "Select a tool declared in the current client catalog. "
            "Executor-internal helpers must be called through their declared executor."
        )
    return (
        f"{message} ({detail}). No client tool in this batch was executed. "
        "Use explicit raw mode for declared raw_fields; otherwise regenerate the envelope with a JSON serializer. "
        "Check embedded quotes, backslashes and the tool schema."
    )


def unknown_tool_regeneration_request(
    body: dict,
    response: dict,
    source: dict,
    diagnostics: dict,
) -> dict | None:
    """Regenerate a first unknown-tool draft from the original task only."""
    if diagnostics.get("reason") != "unknown_tool":
        return None
    if not client_tool_types(source):
        diagnostics["recovery_skipped"] = "no_client_tools"
        return None
    if response.get("status") != "completed":
        diagnostics["recovery_skipped"] = "response_not_completed"
        return None
    for history in (source.get("input"), body.get("input")):
        if isinstance(history, list) and any(
            isinstance(item, dict)
            and item.get("type")
            in {
                "function_call",
                "custom_tool_call",
                "function_call_output",
                "custom_tool_call_output",
                "compaction",
            }
            for item in history
        ):
            diagnostics["recovery_skipped"] = "tool_history"
            return None
    output = response.get("output", [])
    calls = [
        item
        for item in output
        if isinstance(item, dict)
        and item.get("type") in {"function_call", "custom_tool_call"}
    ]
    if len(calls) != 1:
        diagnostics["recovery_skipped"] = "tool_batch"
        return None
    if (
        calls[0] is not output[-1]
        or calls[0].get("type") != "function_call"
        or calls[0].get("status") not in (None, "completed")
    ):
        diagnostics["recovery_skipped"] = "response_shape"
        return None
    # Do not feed back rejected executable text or invent a tool result. The
    # model makes one new first-call decision under the unchanged user task.
    reminder = {
        "role": "developer",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "The previous draft selected a tool absent from the current client catalog. "
                    "No client tool was executed. Regenerate this first call once from the original task "
                    "using exactly one declared tool and its documented run_officejs transport. "
                    "Executor-internal helpers must be called through their declared executor. "
                    "Preserve the user request and all explicitly requested commands or code. "
                    "Use the current argument schema. " + _RAW_TRANSPORT_GUIDANCE
                ),
            }
        ],
    }
    metadata = dict(body.get("metadata", {}))
    iteration = metadata.get("agent_iteration")
    if isinstance(iteration, str) and iteration.isdecimal():
        metadata["agent_iteration"] = str(int(iteration) + 1)
    return {
        **body,
        "stream": True,
        "metadata": metadata,
        "input": _append_before_terminal_compaction_trigger(body["input"], [reminder]),
    }


def tool_call_repair_request(
    body: dict, response: dict, diagnostics: dict
) -> dict | None:
    """Ask for one corrected call, never replay or repair command text locally."""
    if diagnostics.get("reason") not in {
        "invalid_transport_envelope",
        "invalid_function_arguments",
        "tool_type_mismatch",
        "arguments_schema_mismatch",
        "invalid_custom_input",
    }:
        return None
    index = diagnostics.get("tool_call_index")
    calls = [
        item
        for item in response.get("output", [])
        if item.get("type") in {"function_call", "custom_tool_call"}
    ]
    if not isinstance(index, int) or not 0 <= index < len(calls):
        return None
    native = calls[index]
    if (
        native.get("type") != "function_call"
        or not _is_transport_name(_tool_call_name(native))
        or not isinstance(native.get("call_id"), str)
        or not native["call_id"]
    ):
        return None
    guidance = (
        tool_call_failure_message(diagnostics)
        + " "
        + _TRANSPORT_RETRY_GUIDANCE
        + " Return only a format-corrected version of this one rejected call. "
        "Preserve the exact tool target, command/code/custom-input text and all arguments; "
        "never substitute a different command. For unframed raw source use the explicit "
        "raw marker with the original text unchanged. "
        "Do not repeat other calls in the batch or claim that any tool has executed."
    )
    metadata = dict(body.get("metadata", {}))
    iteration = metadata.get("agent_iteration")
    if isinstance(iteration, str) and iteration.isdecimal():
        metadata["agent_iteration"] = str(int(iteration) + 1)
    return {
        **body,
        "stream": True,
        "metadata": metadata,
        "input": [
            *body["input"],
            native,
            {
                "type": "function_call_output",
                "call_id": native["call_id"],
                "output": guidance,
            },
        ],
    }


def tool_call_repair_preserves_input(
    original: dict, corrected: dict, source: dict
) -> bool:
    """Permit representation repair, never an unprovable change to executable input."""
    replacement = extract_native_client_tool_call(
        {"output": [corrected]},
        source,
        remember=False,
    )
    if replacement is None:
        return False
    specs = _client_tool_specs(source)
    declarations = []
    for name, info in specs.items():
        declarations.append(
            {
                "name": name,
                "type": info["type"],
                "parameters": {
                    "properties": {
                        field: {"type": "string"}
                        for field in _raw_transport_fields(info)
                    }
                },
            }
        )
    # Decode even schema-invalid originals for comparison, not for dispatch.
    previous = extract_native_client_tool_call(
        {"output": [original]},
        {"tools": declarations},
        remember=False,
    )
    name = _tool_call_name(replacement)
    if previous is not None:
        if previous["type"] != replacement["type"] or _tool_call_name(previous) != name:
            return False
        if replacement["type"] == "custom_tool_call":
            return previous["input"] == replacement["input"]
        return json.dumps(
            json.loads(previous["arguments"]), sort_keys=True
        ) == json.dumps(json.loads(replacement["arguments"]), sort_keys=True)
    if not _is_transport_name(_tool_call_name(original)):
        return False
    try:
        arguments = original.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            return False
        summary = arguments.get("summary", "")
        if isinstance(summary, str) and summary.startswith(_RAW_TRANSPORT_PREFIX):
            return False
        if (
            _transport_envelope(original, client_tool_types(source), specs=specs)
            is not None
        ):
            return False
        text = arguments.get("code")
        metadata = json.loads(arguments.get("extended_summary", "{}"))
        if not isinstance(text, str) or not isinstance(metadata, dict):
            return False
        if text.lstrip().startswith(("{", "[", "```")):
            # A damaged envelope must not become executable text merely by
            # wrapping the entire undecodable JSON in a raw source field.
            return False
        # An unframed source has no provable destination. The correction must
        # explicitly choose a declared raw-capable tool and preserve all input.
        for field in _raw_transport_fields(specs[name]):
            if field in metadata and metadata[field] != text:
                continue
            if replacement["type"] == "custom_tool_call":
                return not metadata.keys() - {"input"} and replacement["input"] == text
            expected = json.dumps({**metadata, field: text}, sort_keys=True)
            if expected == json.dumps(
                json.loads(replacement["arguments"]), sort_keys=True
            ):
                return True
    except (KeyError, TypeError, ValueError):
        return False
    return False
