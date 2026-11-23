"""Prepare Excel upstream requests and expose established compatibility names."""

from __future__ import annotations

import hashlib

from excel_models import (
    EXCEL_MODEL_UPSTREAMS,
    EXCEL_MODEL_REASONING_EFFORTS,
    EXCEL_REASONING_EFFORTS,
    LOCAL_MODEL_CAPABILITIES,
    MODEL_ID,
    MODEL_IDS,
    UPSTREAM_MODEL,
    excel_model_id,
    is_excel_model,
    local_model_payload,
    merge_local_model_capabilities,
    merge_local_models_payload,
    normalize_reasoning_effort as _normalize_reasoning_effort,
    upstream_model_for,
)
from excel_session import (
    RESPONSES_URL,
    TOOLS_VERSION_METADATA_KEY,
    TOOLS_VERSION_PATTERN as _TOOLS_VERSION_PATTERN,
)
from excel_tool_history import (
    remember_native_call as _remember_native_call,
    remember_native_calls as _remember_native_calls,
    remembered_native_calls as _remembered_native_calls,
)

import structured_output
from excel_input import (
    _fallback_transport_call as _fallback_transport_call,
    _message_item,
    history_configuration_effort,
    translate_input_items,
)
from excel_tool_catalog import (
    EXTERNAL_CLIENT_INSTRUCTIONS as EXTERNAL_CLIENT_INSTRUCTIONS,
    TOOL_CALL_MARKER_OPEN as TOOL_CALL_MARKER_OPEN,
    TOOL_CALL_MARKER_CLOSE as TOOL_CALL_MARKER_CLOSE,
    CLIENT_TOOL_RELAY_PREFIX as CLIENT_TOOL_RELAY_PREFIX,
    CLIENT_MARKER_CALL_ID_PREFIX as CLIENT_MARKER_CALL_ID_PREFIX,
    NATIVE_FALLBACK_CALL_ID_PREFIX as NATIVE_FALLBACK_CALL_ID_PREFIX,
    CLIENT_TOOL_TRANSPORT_NAME as CLIENT_TOOL_TRANSPORT_NAME,
    CLIENT_TOOL_TRANSPORT_ALIASES as CLIENT_TOOL_TRANSPORT_ALIASES,
    _TRANSPORT_RETRY_GUIDANCE as _TRANSPORT_RETRY_GUIDANCE,
    _client_tool_protocol_instructions,
    _client_tool_protocol_reminder,
    _quoted,
    _sanitize_hosted_tools,
    _tool_batch_specs,
    client_tool_types,
    relay_tool_name as relay_tool_name,
)
from excel_tool_transport import (
    _transport_decode_failure as _transport_decode_failure,
    _value_matches_schema as _value_matches_schema,
    extract_client_tool_call as extract_client_tool_call,
    extract_native_client_tool_call as extract_native_client_tool_call,
    extract_native_client_tool_calls as extract_native_client_tool_calls,
    response_payload_with_tool_call as response_payload_with_tool_call,
    response_payload_with_tool_calls as response_payload_with_tool_calls,
)
from excel_tool_recovery import (
    tool_call_failure_message as tool_call_failure_message,
    tool_call_repair_request as tool_call_repair_request,
    tool_call_repair_preserves_input as tool_call_repair_preserves_input,
    unknown_tool_regeneration_request as unknown_tool_regeneration_request,
)


def _cache_key(source: dict) -> str | None:
    value = source.get("prompt_cache_key")
    return value if isinstance(value, str) and value else None


def _agent_iteration(raw_input: object) -> str:
    """Count completed tool calls since the latest user message, as BPS does."""
    if not isinstance(raw_input, list):
        return "1"
    iteration = 1
    for item in reversed(raw_input):
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            break
        kind = item.get("type")
        if isinstance(kind, str) and kind.endswith("_call_output"):
            iteration += 1
    return str(iteration)


class ExcelRequestError(ValueError):
    def __init__(self, message: str, param: str):
        super().__init__(message)
        self.param = param


def _consume_reasoning_updates(raw_input):
    if not isinstance(raw_input, list):
        return raw_input, None
    history = []
    effort = None
    for item in raw_input:
        if not isinstance(item, dict) or item.get("type") != "configuration_update":
            history.append(item)
            continue
        try:
            effort = history_configuration_effort(item)
        except ValueError as exc:
            raise ExcelRequestError(str(exc), "input") from None
    return history, effort


def prepare_responses_body(
    source: dict,
    *,
    tools_version_id: str | None = None,
    cache_scope: str = "local",
) -> dict:
    """Translate a standard Responses request to the Excel add-in wire shape."""
    source = _sanitize_hosted_tools(source)
    if source.get("previous_response_id"):
        raise ExcelRequestError(
            "Excel requires complete input history instead of previous_response_id.",
            "previous_response_id",
        )
    if source.get("tool_choice") not in (None, "auto", "none"):
        raise ExcelRequestError(
            "Excel supports tool_choice auto or none only.", "tool_choice"
        )
    try:
        output_format = structured_output.request_format(source)
    except ValueError as exc:
        raise ExcelRequestError(str(exc), "text.format") from None
    requested_model = source.get("model")
    model = upstream_model_for(requested_model)
    if isinstance(requested_model, str) and requested_model.strip():
        if not is_excel_model(requested_model):
            model = requested_model.strip()
    output: dict[str, object] = {
        "model": model,
        # Match the official Excel add-in wire shape. The add-in marks picker
        # choices as explicit so the Basispoints backend does not apply its
        # automatic/default model routing to an otherwise valid model slug.
        "model_selection": "explicit",
        "stream": bool(source.get("stream", False)),
        "store": False,
    }

    raw_input, history_effort = _consume_reasoning_updates(source.get("input"))
    try:
        initial_specs = (
            _tool_batch_specs(source.get("tools"))
            if source.get("tool_choice") != "none" else {}
        )
    except ValueError as exc:
        raise ExcelRequestError(str(exc), "tools") from None
    input_items = translate_input_items(
        raw_input,
        tool_specs=initial_specs,
    )
    prologue: list = []
    instructions = source.get("instructions")
    if isinstance(instructions, str) and instructions:
        prologue.append(_message_item("developer", instructions))
    protocol = _client_tool_protocol_instructions(source)
    if output_format is not None:
        protocol += "\n" + structured_output.instructions(output_format)
    prologue.append(_message_item("developer", protocol))
    output["input"] = prologue + input_items

    cache_key = _cache_key(source)
    if cache_key:
        cache_identity = _quoted([cache_scope, cache_key])
        output["prompt_cache_key"] = (
            "bps-" + hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()[:32]
        )

    reasoning = source.get("reasoning")
    requested_effort = (
        reasoning.get("effort")
        if isinstance(reasoning, dict)
        else source.get("reasoning_effort")
    )
    if isinstance(reasoning, dict) and reasoning.get("mode") not in (None, "", "standard"):
        raise ExcelRequestError("Unsupported reasoning mode.", "reasoning.mode")
    if requested_effort in (None, ""):
        requested_effort = history_effort or "medium"
    effort = _normalize_reasoning_effort(requested_effort, source.get("model"))
    if effort is None:
        raise ExcelRequestError(
            "Unsupported reasoning effort.",
            "reasoning.effort" if isinstance(reasoning, dict) else "reasoning_effort",
        )
    output["reasoning_effort"] = effort

    context_management = source.get("context_management")
    output["context_management"] = (
        context_management
        if isinstance(context_management, list)
        else [{"type": "compaction", "compact_threshold": 200_000}]
    )

    # Task and turn IDs belong to the admitted account slot, not caller metadata.
    metadata = {"agent_iteration": _agent_iteration(raw_input)}
    if tools_version_id and _TOOLS_VERSION_PATTERN.fullmatch(tools_version_id):
        metadata[TOOLS_VERSION_METADATA_KEY] = tools_version_id
    output["metadata"] = metadata
    return output
