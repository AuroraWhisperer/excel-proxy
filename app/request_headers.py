"""Resolve Responses conversation affinity for replay state."""

import hashlib
import json


def _responses_identity_scope(kind: str, *parts: str) -> str:
    """Encode typed identity fields without delimiter ambiguity."""
    return json.dumps(
        [kind.strip(), *(part.strip() for part in parts)],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _responses_subagent_affinity_scope(subagent: str, affinity_value: str) -> str:
    return _responses_identity_scope(
        "responses-subagent-affinity",
        subagent.lower(),
        affinity_value,
    )


def _responses_input_text(value) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for entry in content:
                if not isinstance(entry, dict):
                    continue
                for key in ("text", "input_text", "output_text"):
                    text = entry.get(key)
                    if isinstance(text, str):
                        parts.append(text)
                        break
            if parts:
                return "".join(parts)
        for key in ("text", "input_text", "output_text"):
            text = item.get(key)
            if isinstance(text, str):
                return text
    return None


def _codex_rollout_memory_affinity_value(payload, base_affinity: str) -> str | None:
    """Return an isolated affinity for Codex rollout-memory writer requests.

    Codex's background memory writer sends prompts like "Analyze this rollout..."
    using the active conversation's prompt_cache_key even though each rollout is
    unrelated to the interactive turn. If those requests reuse the interactive
    affinity, a post-interrupt rollout summary with <turn_aborted> content can
    look like a cache bust and can also churn the upstream prompt-cache bucket.
    Keep them stable for the same rollout, but isolate them from the main turn
    and from other rollout summaries.
    """
    if not isinstance(payload, dict) or not isinstance(base_affinity, str):
        return None
    text = _responses_input_text(payload.get("input"))
    if not isinstance(text, str):
        return None
    if not text.startswith("Analyze this rollout and produce JSON"):
        return None
    if "rollout_context:" not in text or "rendered conversation" not in text:
        return None
    digest = hashlib.sha256(f"{base_affinity}\n{text}".encode("utf-8")).hexdigest()[:32]
    return f"codex-rollout-memory:{digest}"


def _responses_affinity_value(payload, session_id: str | None = None) -> str | None:
    if isinstance(payload, dict):
        for key in ("prompt_cache_key", "promptCacheKey", "session_id", "sessionId"):
            value = payload.get(key)
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    isolated = _codex_rollout_memory_affinity_value(payload, normalized)
                    if isolated:
                        return isolated
                    return normalized
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in ("session_id", "sessionId"):
                value = metadata.get(key)
                if isinstance(value, str):
                    normalized = value.strip()
                    if normalized:
                        isolated = _codex_rollout_memory_affinity_value(payload, normalized)
                        if isolated:
                            return isolated
                        return normalized
        for key in ("previous_response_id", "previousResponseId"):
            value = payload.get(key)
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    # A bare previous-response chain has no durable session or
                    # prompt-cache key. Keep it out of the fallback task ID
                    # namespace, where identical user text can otherwise join
                    # unrelated conversations.
                    return f"previous_response:{normalized}"
    if isinstance(session_id, str):
        normalized = session_id.strip()
        if normalized:
            return normalized
    return None


def responses_affinity_value(payload, session_id: str | None = None) -> str | None:
    """Expose the effective Responses affinity used for upstream identity."""
    return _responses_affinity_value(payload, session_id)


def responses_replay_affinity_value(
    payload,
    session_id: str | None = None,
    subagent: str | None = None,
) -> str | None:
    """Return the same scope used by upstream headers for replay-ID state."""
    affinity_value = _responses_affinity_value(payload, session_id)
    if not affinity_value:
        return None
    if isinstance(subagent, str) and subagent.strip():
        return _responses_subagent_affinity_scope(subagent, affinity_value)
    return affinity_value
