"""Prepare Excel upstream requests and expose established compatibility names."""

from __future__ import annotations

import hashlib
import json
import os
from uuid import NAMESPACE_URL, uuid5

from excel_models import (
    ASTRA_COMPACTION_TOKEN_LIMIT,
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
    ExcelSessionStore,
    RESPONSES_URL,
    SESSION_FILE,
    TOOLS_VERSION_METADATA_KEY,
    TOOLS_VERSION_PATTERN as _TOOLS_VERSION_PATTERN,
    excel_session_store,
)
from excel_tool_history import (
    remember_native_call as _remember_native_call,
    remember_native_calls as _remember_native_calls,
    remembered_native_calls as _remembered_native_calls,
)

import structured_output
from responses_input import _append_before_terminal_compaction_trigger
from excel_input import (
    _fallback_transport_call as _fallback_transport_call,
    _message_item,
    _strip_client_only_item_metadata,
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
    _client_tool_specs,
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

# prompt_cache_key is the documented OpenAI cache-routing control; set to 0
# only if the Basispoints gateway ever starts rejecting the parameter.
FORWARD_PROMPT_CACHE_KEY = os.environ.get(
    "GHCP_EXCEL_FORWARD_PROMPT_CACHE_KEY", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# Escape hatch back to the pre-cache-fix layout (full catalog as the prompt
# suffix) in case the stable-prefix reminder ever stops holding the model to
# the client-tool transport protocol. See _client_tool_protocol_reminder.
CATALOG_AT_PROMPT_END = os.environ.get(
    "GHCP_EXCEL_CATALOG_AT_PROMPT_END", "0"
).strip().lower() in {"1", "true", "yes", "on"}


def _conversation_fingerprint(input_items: list) -> str:
    """Stable conversation identity for clients that send no cache key.

    The first input item is the root of the conversation and does not change
    as turns are appended, so hashing it keeps one identity per conversation
    without inventing a random one per request.
    """
    for item in input_items:
        if isinstance(item, dict):
            rendered = json.dumps(item, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return "anonymous"


def _cache_key(source: dict) -> str | None:
    for key in ("prompt_cache_key", "promptCacheKey", "session_id", "sessionId"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Codex carries its stable root session in client_metadata. Prefer it only
    # as a fallback, leaving an explicit caller cache key authoritative.
    client_metadata = source.get("client_metadata")
    if isinstance(client_metadata, dict):
        for key in ("session_id", "sessionId"):
            value = client_metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _agent_turn_state(raw_input: object) -> tuple[str, str]:
    """Return a stable user-turn fingerprint and Excel agent iteration.

    The Excel add-in holds ``turn_id`` constant while it executes any number of
    tools for one user message. Only ``agent_iteration`` advances. Treating
    every tool output as a new turn makes Basispoints discard the prior plan
    state and start planning again.
    """
    if isinstance(raw_input, str):
        rendered = json.dumps(raw_input, ensure_ascii=False)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest(), "1"
    if not isinstance(raw_input, list):
        return "anonymous", "1"

    last_user_index = -1
    for index, item in enumerate(raw_input):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role == "user":
            last_user_index = index

    turn_prefix = (
        raw_input[: last_user_index + 1] if last_user_index >= 0 else raw_input[:1]
    )
    turn_prefix = [
        _strip_client_only_item_metadata(item) if isinstance(item, dict) else item
        for item in turn_prefix
    ]
    # Only the latest user's explicit turn ID identifies a new user turn.
    # Other private fields (such as executed-tool metadata) change on replay.
    if last_user_index >= 0:
        private = raw_input[last_user_index].get(
            "internal_chat_message_metadata_passthrough"
        )
        if isinstance(private, dict) and isinstance(private.get("turn_id"), str):
            turn_prefix[-1] = {
                **turn_prefix[-1],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": private["turn_id"]
                },
            }
    rendered = json.dumps(
        turn_prefix,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    iteration_outputs = 0
    in_result_batch = False
    for item in raw_input[last_user_index + 1 :]:
        if not isinstance(item, dict):
            continue
        is_result = item.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
        }
        if is_result and not in_result_batch:
            iteration_outputs += 1
        in_result_batch = is_result
    return fingerprint, str(iteration_outputs + 1)


class ExcelRequestError(ValueError):
    def __init__(self, message: str, param: str):
        super().__init__(message)
        self.param = param


def prepare_responses_body(
    source: dict,
    *,
    tools_version_id: str | None = None,
) -> dict:
    """Translate a standard Responses request to the Excel add-in wire shape."""
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
    output: dict[str, object] = {
        "model": upstream_model_for(source.get("model")),
        # Match the official Excel add-in wire shape. The add-in marks picker
        # choices as explicit so the Basispoints backend does not apply its
        # automatic/default model routing to an otherwise valid model slug.
        "model_selection": "explicit",
        "stream": bool(source.get("stream", False)),
        "store": False,
    }

    raw_input = source.get("input")
    input_items = translate_input_items(
        raw_input,
        client_tool_types(source),
        tool_specs=_client_tool_specs(source),
    )
    # Captured before the prologue is prepended: the injected instructions and
    # catalog are identical across conversations, so only the caller's own
    # first history item identifies this conversation. Use translated items:
    # Codex changes private per-turn metadata on a new user turn, but that
    # metadata must not create a new Excel task/session.
    history_root = _conversation_fingerprint(input_items)

    # Prompt layout is chosen for the upstream prompt cache: everything that is
    # stable across a conversation leads, so each turn only re-bills the newly
    # appended history plus the short trailing reminder. Putting the ~3.5k-token
    # catalog last instead (the old layout, still reachable through
    # GHCP_EXCEL_CATALOG_AT_PROMPT_END) forced the cache prefix to end at the
    # catalog's first byte and re-billed it on every request.
    prologue: list = []
    instructions = source.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        prologue.append(_message_item("developer", instructions))
    if output_format is not None:
        prologue.append(
            _message_item("developer", structured_output.instructions(output_format))
        )
    catalog = _message_item("developer", _client_tool_protocol_instructions(source))
    if CATALOG_AT_PROMPT_END:
        input_items = _append_before_terminal_compaction_trigger(
            prologue + input_items,
            [catalog],
        )
    else:
        prologue.append(catalog)
        reminder = _client_tool_protocol_reminder(source)
        if reminder:
            # Stable protocol instructions must precede conversation history.
            # A trailing reminder would become an insertion point on the next
            # turn and force the upstream cache to stop at that earlier byte.
            prologue.append(_message_item("developer", reminder))
        input_items = prologue + input_items
    output["input"] = input_items

    cache_key = _cache_key(source)
    if cache_key and FORWARD_PROMPT_CACHE_KEY:
        output["prompt_cache_key"] = cache_key

    reasoning = source.get("reasoning")
    requested_effort = (
        reasoning.get("effort")
        if isinstance(reasoning, dict)
        else source.get("reasoning_effort")
    )
    # Keep the public picker and the Basispoints wire value aligned. Unknown
    # or stale catalog values fall back to medium rather than producing a 422.
    output["reasoning_effort"] = (
        _normalize_reasoning_effort(requested_effort, source.get("model")) or "medium"
    )
    if isinstance(reasoning, dict) and reasoning.get("summary") in (
        "auto",
        "concise",
        "detailed",
    ):
        # Basispoints accepts auto and returns detailed summaries. Forwarding
        # Codex's detailed value directly is rejected with HTTP 422.
        output["reasoning"] = {"effort": output["reasoning_effort"], "summary": "auto"}

    context_management = source.get("context_management")
    # Start Astra's Codex compaction at 90%; reserve the full limit for the
    # upstream fallback instead of compacting there at the old 200k default.
    compact_threshold = (
        ASTRA_COMPACTION_TOKEN_LIMIT if output["model"] == "gpt-6-astra" else 200_000
    )
    output["context_management"] = (
        context_management
        if isinstance(context_management, list)
        else [{"type": "compaction", "compact_threshold": compact_threshold}]
    )

    metadata: dict[str, str] = {}
    raw_metadata = source.get("metadata")
    if isinstance(raw_metadata, dict):
        for key, value in raw_metadata.items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                metadata[key[:64]] = str(value)[:512]
    turn_fingerprint, iteration = _agent_turn_state(raw_input)
    metadata.setdefault("agent_iteration", iteration)
    # Identifiers are derived, never random: the same client request must
    # serialize to the same bytes every time. A conversation keeps one task
    # identity across turns, and a retried turn keeps its turn identity, so a
    # retry is recognisable as the same turn rather than as new work.
    conversation = cache_key or history_root
    metadata.setdefault(
        "task_id",
        str(uuid5(NAMESPACE_URL, f"ghcp-proxy/gpt-excel/{conversation}")),
    )
    metadata.setdefault(
        "turn_id",
        str(
            uuid5(
                NAMESPACE_URL,
                f"ghcp-proxy/gpt-excel/{conversation}/turn/{turn_fingerprint}",
            )
        ),
    )
    if tools_version_id and _TOOLS_VERSION_PATTERN.fullmatch(tools_version_id):
        metadata[TOOLS_VERSION_METADATA_KEY] = tools_version_id
    output["metadata"] = metadata
    return output
