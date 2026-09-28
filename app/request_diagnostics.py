"""Stateless, credential-filtered request diagnostics; no archive or writer ownership."""

import hashlib
import json

import httpx

from constants import REQUEST_TRACE_BODY_MAX_BYTES, REQUEST_PROMPT_PREVIEW_MAX_CHARS
import responses_protocol
import util
import usage_metrics


TRACE_HEADER_ALLOWLIST = {
    "content-type",
    "user-agent",
    "openai-intent",
    "editor-version",
    "editor-plugin-version",
    "x-initiator",
    "session_id",
    "x-client-request-id",
    "x-openai-subagent",
    "x-interaction-id",
    "x-interaction-type",
    "x-agent-task-id",
    "x-parent-agent-id",
    "x-client-session-id",
    "x-client-machine-id",
    "x-stainless-retry-count",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "accept-language",
    "sec-fetch-mode",
    "x-request-id",
    "accept",
    "accept-encoding",
    "host",
    "connection",
    "content-length",
}


def header_trace_subset(headers: dict | None) -> dict:
    if not isinstance(headers, dict):
        return {}
    subset = {}
    for key, value in headers.items():
        normalized_key = str(key).strip()
        if not normalized_key or normalized_key.lower() not in TRACE_HEADER_ALLOWLIST:
            continue
        subset[normalized_key] = value
    return subset


def sorted_counts(values: dict[str, int]) -> dict[str, int]:
    return {key: values[key] for key in sorted(values)}


def count_trace_items(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(items, list):
        return counts
    for item in items:
        if isinstance(item, dict):
            item_type = str(item.get("type", "dict")).strip() or "dict"
        else:
            item_type = type(item).__name__
        counts[item_type] = counts.get(item_type, 0) + 1
    return sorted_counts(counts)


def count_trace_roles(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(items, list):
        return counts
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        if not role:
            continue
        counts[role] = counts.get(role, 0) + 1
    return sorted_counts(counts)


def trace_messages_summary(messages) -> dict:
    if isinstance(messages, str):
        return {"kind": "string", "chars": len(messages)}
    if not isinstance(messages, list):
        return {"kind": type(messages).__name__}

    part_counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    part_type = str(part.get("type", "dict")).strip() or "dict"
                else:
                    part_type = type(part).__name__
                part_counts[part_type] = part_counts.get(part_type, 0) + 1
        elif isinstance(content, str) and content:
            part_counts["text"] = part_counts.get("text", 0) + 1

    return {
        "kind": "list",
        "count": len(messages),
        "roles": count_trace_roles(messages),
        "content_part_types": sorted_counts(part_counts),
    }


def trace_input_summary(input_value) -> dict:
    if isinstance(input_value, str):
        return {"kind": "string", "chars": len(input_value)}
    if not isinstance(input_value, list):
        return {"kind": type(input_value).__name__}

    encrypted_reasoning_items = 0
    for item in input_value:
        if (
            isinstance(item, dict)
            and item.get("type") == "reasoning"
            and isinstance(item.get("encrypted_content"), str)
        ):
            encrypted_reasoning_items += 1

    return {
        "kind": "list",
        "count": len(input_value),
        "item_types": count_trace_items(input_value),
        "roles": count_trace_roles(input_value),
        "has_compaction": responses_protocol.input_contains_compaction(input_value),
        "encrypted_reasoning_items": encrypted_reasoning_items,
        "sequence": trace_input_sequence(input_value),
    }


def trace_hash(value) -> str | None:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()[:16]


def trace_text_chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(trace_text_chars(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for key in ("text", "input_text", "output_text"):
            text = value.get(key)
            if isinstance(text, str):
                total += len(text)
        for key in ("content", "output"):
            nested = value.get(key)
            if isinstance(nested, (list, dict, str)):
                total += trace_text_chars(nested)
        return total
    return 0


def trace_input_sequence(input_value: list) -> list[dict]:
    sequence = []
    for index, item in enumerate(input_value):
        if not isinstance(item, dict):
            sequence.append(
                {
                    "index": index,
                    "type": type(item).__name__,
                    "item_hash": trace_hash(item),
                }
            )
            continue
        entry = {
            "index": index,
            "type": item.get("type"),
            "item_hash": trace_hash(item),
        }
        for key in ("role", "name", "status"):
            value = item.get(key)
            if isinstance(value, str) and value:
                entry[key] = value
        for key in ("id", "call_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                entry[f"{key}_hash"] = trace_hash(value)
        if "content" in item:
            entry["content_chars"] = trace_text_chars(item.get("content"))
            entry["content_hash"] = trace_hash(item.get("content"))
        if "output" in item:
            entry["output_chars"] = trace_text_chars(item.get("output"))
            entry["output_hash"] = trace_hash(item.get("output"))
        if "arguments" in item:
            entry["arguments_hash"] = trace_hash(item.get("arguments"))
        encrypted = item.get("encrypted_content")
        if isinstance(encrypted, str) and encrypted:
            entry["encrypted_content_chars"] = len(encrypted)
            entry["encrypted_content_hash"] = trace_hash(encrypted)
        sequence.append(entry)
    return sequence


def trace_tools_deferred_count(tools) -> int:
    if isinstance(tools, list):
        return sum(trace_tools_deferred_count(tool) for tool in tools)
    if not isinstance(tools, dict):
        return 0
    count = 1 if "defer_loading" in tools else 0
    nested = tools.get("tools")
    if isinstance(nested, (list, dict)):
        count += trace_tools_deferred_count(nested)
    return count


def request_reasoning_effort(body: dict | None) -> str | None:
    """Return the requested reasoning level from any supported request shape."""
    if not isinstance(body, dict):
        return None

    candidates = [body.get("reasoning_effort")]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        candidates.append(reasoning.get("effort"))
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        candidates.append(output_config.get("effort"))

    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized:
                return normalized
    return None


def trace_body_summary(body: dict | None) -> dict | None:
    if not isinstance(body, dict):
        return None

    summary = {
        "keys": sorted(body.keys()),
        "model": body.get("model"),
        "stream": body.get("stream"),
    }

    reasoning_effort = request_reasoning_effort(body)
    if reasoning_effort is not None:
        summary["reasoning_effort"] = reasoning_effort
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        snapshot: dict = {}
        t_type = thinking.get("type")
        if isinstance(t_type, str):
            snapshot["type"] = t_type
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int):
            snapshot["budget_tokens"] = budget
        if snapshot:
            summary["thinking"] = snapshot

    for source_key, target_key in (
        ("session_id", "session_id"),
        ("sessionId", "session_id"),
    ):
        value = body.get(source_key)
        if isinstance(value, str) and value.strip():
            summary[target_key] = value.strip()

    tools = body.get("tools")
    if isinstance(tools, list):
        summary["tool_count"] = len(tools)
        deferred_tool_count = trace_tools_deferred_count(tools)
        if deferred_tool_count:
            summary["deferred_tool_count"] = deferred_tool_count
        if responses_protocol.responses_tools_have_tool_search(tools):
            summary["tool_search_present"] = True
    elif isinstance(tools, dict):
        deferred_tool_count = trace_tools_deferred_count(tools)
        if deferred_tool_count:
            summary["deferred_tool_count"] = deferred_tool_count
        if responses_protocol.responses_tools_have_tool_search(tools):
            summary["tool_search_present"] = True

    if "input" in body:
        summary["input"] = trace_input_summary(body.get("input"))
    if "messages" in body:
        summary["messages"] = trace_messages_summary(body.get("messages"))
    body_fingerprint = trace_hash(body)
    if body_fingerprint:
        summary["body_fingerprint"] = body_fingerprint

    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        summary["metadata_keys"] = sorted(metadata.keys())
    for key in sorted(body.keys()):
        if key in ("input", "messages"):
            continue
        fingerprint = trace_hash(body.get(key))
        if fingerprint:
            summary[f"{key}_fingerprint"] = fingerprint

    return summary


def effective_trace_usage(
    response_payload: dict | None = None, usage: dict | None = None
) -> dict | None:
    normalized_usage = usage_metrics.normalize_usage_payload(usage)
    if isinstance(normalized_usage, dict):
        return normalized_usage
    if isinstance(response_payload, dict):
        normalized_usage = usage_metrics.normalize_usage_payload(
            response_payload.get("usage")
        )
        if isinstance(normalized_usage, dict):
            return normalized_usage
    return None


def trace_response_summary(
    upstream: httpx.Response | None = None,
    response_payload: dict | None = None,
    usage: dict | None = None,
    status_code: int | None = None,
) -> dict:
    summary: dict = {}
    if status_code is not None:
        summary["status_code"] = status_code
    if upstream is not None:
        if status_code is None:
            summary["status_code"] = upstream.status_code
        elif upstream.status_code != status_code:
            summary["upstream_status_code"] = upstream.status_code
        content_type = upstream.headers.get("content-type")
        if content_type:
            summary["content_type"] = content_type
        for header_name in ("x-request-id", "request-id"):
            header_value = upstream.headers.get(header_name)
            if header_value:
                summary["upstream_request_id"] = header_value
                break
    if isinstance(response_payload, dict):
        for key in ("id", "object", "model"):
            value = response_payload.get(key)
            if isinstance(value, str) and value:
                summary[key] = value
        output = response_payload.get("output")
        if isinstance(output, list):
            summary["output_item_types"] = count_trace_items(output)

    normalized_usage = effective_trace_usage(
        response_payload=response_payload, usage=usage
    )
    if isinstance(normalized_usage, dict):
        summary["usage"] = normalized_usage

    error_payload = None
    if isinstance(response_payload, dict):
        maybe_error = response_payload.get("error")
        if isinstance(maybe_error, dict):
            error_payload = maybe_error
    if isinstance(error_payload, dict):
        error_summary = {}
        for key in ("type", "code", "param"):
            value = error_payload.get(key)
            if value is not None:
                error_summary[key] = value
        if error_summary:
            summary["error"] = error_summary

    return summary


def trim_trace_field(value, *, max_bytes: int = REQUEST_TRACE_BODY_MAX_BYTES):
    """Cap body-ish trace fields so retained rows stay bounded in size."""
    if value is None or max_bytes <= 0:
        return value
    try:
        serialized = json.dumps(
            value, separators=(",", ":"), default=util._json_default
        )
    except (TypeError, ValueError):
        return value
    encoded = serialized.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    return {
        "_truncated": True,
        "original_bytes": len(encoded),
        "preview": encoded[:max_bytes].decode("utf-8", errors="replace"),
        "original_type": type(value).__name__,
    }


def trim_trace_text(value, *, max_chars: int = REQUEST_TRACE_BODY_MAX_BYTES):
    if not isinstance(value, str) or max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n...[truncated; original {len(value)} chars]"


def extract_prompt_preview(
    body: dict | None,
    *,
    truncate: bool = True,
    max_chars: int = REQUEST_PROMPT_PREVIEW_MAX_CHARS,
) -> dict | None:
    """Pull a human-readable prompt preview out of a request body.

    Returns a dict with ``system`` (concatenated system/developer prompts),
    ``user`` (most recent user turn text) and ``truncated`` flags. ``None``
    is returned when the body carries no recognizable prompt material.
    Accepts current Responses input and legacy archived message shapes.
    """
    if not isinstance(body, dict) or max_chars <= 0:
        return None

    system_parts: list[str] = []
    user_parts: list[str] = []

    raw_system = body.get("system")
    if isinstance(raw_system, str) and raw_system.strip():
        system_parts.append(raw_system)
    elif isinstance(raw_system, list):
        for entry in raw_system:
            text = util.extract_item_text(entry) if isinstance(entry, dict) else ""
            if (
                not text
                and isinstance(entry, dict)
                and isinstance(entry.get("text"), str)
            ):
                text = entry["text"]
            if isinstance(text, str) and text.strip():
                system_parts.append(text)

    def _collect(items):
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("type") or "").strip().lower()
            text = util.extract_item_text(item)
            if not isinstance(text, str) or not text.strip():
                continue
            if role in ("system", "developer"):
                system_parts.append(text)
            elif role in ("user", "human", "message", ""):
                user_parts.append(text)

    _collect(body.get("messages"))
    input_value = body.get("input")
    if isinstance(input_value, str) and input_value.strip():
        user_parts.append(input_value)
    else:
        _collect(input_value)

    if not system_parts and not user_parts:
        return None

    def _finalize(parts: list[str]) -> tuple[str, bool]:
        combined = "\n\n".join(
            part.strip() for part in parts if isinstance(part, str) and part.strip()
        )
        if not combined:
            return "", False
        # Keep the most recent context for user prompts (tail) and the
        # leading context for system prompts (head) since the head carries
        # the instructions.
        if not truncate or max_chars <= 0 or len(combined) <= max_chars:
            return combined, False
        return combined[
            :max_chars
        ] + f"\n…[truncated; original {len(combined)} chars]", True

    system_text, system_truncated = _finalize(system_parts)
    # For user prompts, prefer the latest turn when truncating.
    user_combined = "\n\n".join(
        part.strip() for part in user_parts if isinstance(part, str) and part.strip()
    )
    user_truncated = False
    if truncate and max_chars > 0 and len(user_combined) > max_chars:
        user_combined = (
            "…[truncated; original "
            + str(len(user_combined))
            + " chars]\n"
            + user_combined[-max_chars:]
        )
        user_truncated = True

    preview: dict = {}
    if system_text:
        preview["system"] = system_text
        if system_truncated:
            preview["system_truncated"] = True
    if user_combined:
        preview["user"] = user_combined
        if user_truncated:
            preview["user_truncated"] = True
    return preview or None
