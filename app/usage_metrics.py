"""Usage normalization, model pricing, identity and deduplication rules."""

import glob
import hashlib
import json
import os
import re
import sqlite3

from constants import MODEL_PRICING_ALIASES, MODEL_PRICING
import excel_models
from util import _coerce_float, _coerce_int, _parse_iso_datetime


def _extract_payload_usage(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None

    usage = payload.get("usage")
    return normalize_usage_payload(usage)


def normalize_usage_payload(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None

    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        input_tokens = usage.get("prompt_tokens")

    output_tokens = usage.get("output_tokens")
    if output_tokens is None:
        output_tokens = usage.get("completion_tokens")

    cached_tokens = usage.get("cache_read_input_tokens")
    # OpenAI/Responses-style ``cached_input_tokens`` is a subset of gross
    # ``input_tokens``.  Keep this separate from Anthropic's
    # ``cache_read_input_tokens``, which is reported alongside fresh input.
    # Without an explicit fresh-input field, treating the former as additive
    # prices the same cached tokens twice.
    cached_tokens_are_subset_of_input = False
    if cached_tokens is None:
        cached_tokens = usage.get("cached_input_tokens")
        cached_tokens_are_subset_of_input = cached_tokens is not None
    if cached_tokens is None:
        for details_key in ("input_tokens_details", "prompt_tokens_details"):
            details = usage.get(details_key)
            if isinstance(details, dict):
                cached_tokens = details.get("cached_tokens")
                if cached_tokens is not None:
                    cached_tokens_are_subset_of_input = True
                    break

    cache_creation_tokens = usage.get("cache_creation_input_tokens")
    reasoning_tokens = usage.get("reasoning_output_tokens")
    if reasoning_tokens is None:
        for details_key in ("output_tokens_details", "completion_tokens_details"):
            details = usage.get(details_key)
            if isinstance(details, dict):
                reasoning_tokens = details.get("reasoning_tokens")
                if reasoning_tokens is not None:
                    break
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = usage.get("totalTokens")

    normalized_input_tokens = _coerce_int(input_tokens, default=0)
    normalized_output_tokens = _coerce_int(output_tokens, default=0)
    normalized_cached_tokens = _coerce_int(cached_tokens, default=0)
    normalized_cache_creation_tokens = _coerce_int(cache_creation_tokens, default=0)
    normalized_reasoning_tokens = _coerce_int(reasoning_tokens, default=0)

    normalized_total_tokens = _coerce_int(total_tokens, default=None)
    if normalized_total_tokens is None:
        normalized_total_tokens = normalized_input_tokens + normalized_output_tokens

    normalized = {
        "input_tokens": normalized_input_tokens,
        "output_tokens": normalized_output_tokens,
        "total_tokens": normalized_total_tokens,
        "cached_input_tokens": normalized_cached_tokens,
        "cache_creation_input_tokens": normalized_cache_creation_tokens,
        "reasoning_output_tokens": normalized_reasoning_tokens,
    }
    fresh_input_tokens = usage.get("fresh_input_tokens")
    if fresh_input_tokens is None:
        fresh_input_tokens = usage.get("billable_input_tokens")
    if fresh_input_tokens is not None:
        normalized["fresh_input_tokens"] = _coerce_int(fresh_input_tokens, default=0)
    elif cached_tokens_are_subset_of_input and normalized_cached_tokens > 0:
        normalized["fresh_input_tokens"] = max(
            0, normalized_input_tokens - normalized_cached_tokens
        )
    pricing_fresh_input_tokens = usage.get("pricing_fresh_input_tokens")
    if pricing_fresh_input_tokens is not None:
        normalized["pricing_fresh_input_tokens"] = _coerce_int(
            pricing_fresh_input_tokens, default=0
        )
    pricing_cached_input_tokens = usage.get("pricing_cached_input_tokens")
    if pricing_cached_input_tokens is not None:
        normalized["pricing_cached_input_tokens"] = _coerce_int(
            pricing_cached_input_tokens, default=0
        )
    pricing_cache_creation_input_tokens = usage.get(
        "pricing_cache_creation_input_tokens"
    )
    if pricing_cache_creation_input_tokens is not None:
        normalized["pricing_cache_creation_input_tokens"] = _coerce_int(
            pricing_cache_creation_input_tokens,
            default=0,
        )
    return normalized


def usage_display_input_tokens(usage: dict) -> int:
    """Return the input-token count shown in dashboard rollups.

    OpenAI/Codex response usage keeps ``input_tokens`` in upstream shape
    (gross input including cached tokens) and carries the old/net presentation
    value in ``fresh_input_tokens``.  Dashboard rollups should present that net
    count while leaving the recorded event payload untouched.
    """
    fresh_input_tokens = usage.get("fresh_input_tokens")
    if fresh_input_tokens is None:
        fresh_input_tokens = usage.get("billable_input_tokens")
    if fresh_input_tokens is not None:
        return max(0, _coerce_int(fresh_input_tokens))
    return max(0, _coerce_int(usage.get("input_tokens")))


# ---------------------------------------------------------------------------
# Request key / classification helpers
# ---------------------------------------------------------------------------


def _server_request_chain_key(
    session_id: str | None,
    client_request_id: str | None,
    subagent: str | None,
) -> tuple[str, str]:
    scope = None
    if isinstance(session_id, str) and session_id:
        scope = f"session:{session_id}"
    elif isinstance(client_request_id, str) and client_request_id:
        scope = f"client:{client_request_id}"
    else:
        scope = "global"
    normalized_subagent = (
        subagent if isinstance(subagent, str) and subagent else "__root__"
    )
    return (scope, normalized_subagent)


def _codex_native_session_id_from_request_id(request_id: str | None) -> str | None:
    if not isinstance(request_id, str):
        return None
    prefix = "codex-native:"
    if not request_id.startswith(prefix):
        return None
    remainder = request_id[len(prefix) :]
    if not remainder:
        return None
    # Newer native records include a per-rollout path fingerprint before their
    # per-file sequence number. Keep the session component intact so fallback
    # grouping does not turn one native session into many synthetic sessions.
    path_parts = remainder.rsplit(":", 2)
    if len(path_parts) == 3:
        session_id, path_fingerprint, turn_id = path_parts
        if (
            session_id
            and len(path_fingerprint) == 16
            and all(ch in "0123456789abcdefABCDEF" for ch in path_fingerprint)
            and turn_id.isdigit()
        ):
            return session_id
    session_id, separator, turn_id = remainder.rpartition(":")
    if not separator or not session_id or not turn_id:
        return None
    return session_id


FAST_SERVICE_TIERS = frozenset({"fast", "priority"})


_CODEX_LOGS_SERVICE_TIER_CACHE: dict[str, object] = {
    "path": None,
    "mtime": None,
    "size": None,
    "values": {},
}
_CODEX_LOGS_SERVICE_TIER_RE = re.compile(r'"service_tier"\s*:\s*"([^"]+)"')


def _codex_logs_db_path(codex_home: str | None = None) -> str | None:
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    if not isinstance(home, str) or not home:
        return None

    candidates: list[str] = []
    for pattern in ("logs_*.sqlite", "logs.sqlite"):
        candidates.extend(glob.glob(os.path.join(home, pattern)))
    if not candidates:
        return None

    def _sort_key(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    return max(candidates, key=_sort_key)


def _extract_codex_log_service_tier(feedback_log_body: str | None) -> str | None:
    if not isinstance(feedback_log_body, str) or not feedback_log_body:
        return None
    match = _CODEX_LOGS_SERVICE_TIER_RE.search(feedback_log_body)
    if not match:
        return None
    tier = match.group(1).strip().lower()
    return tier or None


def _codex_logs_service_tiers(
    session_id: str | None,
    turn_id: str | None = None,
    started_at: str | None = None,
    logs_db_path: str | None = None,
) -> dict[str, str | None]:
    empty = {
        "requested": None,
        "requested_source": None,
        "effective": None,
        "effective_source": None,
    }
    if not isinstance(session_id, str) or not session_id:
        return dict(empty)

    db_path = logs_db_path or _codex_logs_db_path()
    if not isinstance(db_path, str) or not db_path:
        return dict(empty)

    try:
        mtime = os.path.getmtime(db_path)
        size = os.path.getsize(db_path)
    except OSError:
        return dict(empty)

    cache_values = _CODEX_LOGS_SERVICE_TIER_CACHE.get("values")
    if not isinstance(cache_values, dict):
        cache_values = {}
    if (
        _CODEX_LOGS_SERVICE_TIER_CACHE.get("path") != db_path
        or _CODEX_LOGS_SERVICE_TIER_CACHE.get("mtime") != mtime
        or _CODEX_LOGS_SERVICE_TIER_CACHE.get("size") != size
    ):
        cache_values = {}
        _CODEX_LOGS_SERVICE_TIER_CACHE["path"] = db_path
        _CODEX_LOGS_SERVICE_TIER_CACHE["mtime"] = mtime
        _CODEX_LOGS_SERVICE_TIER_CACHE["size"] = size
        _CODEX_LOGS_SERVICE_TIER_CACHE["values"] = cache_values

    event_dt = _parse_iso_datetime(started_at)
    event_ts = int(event_dt.timestamp()) if event_dt else None
    cache_key = (session_id, turn_id or "", event_ts)
    if cache_key in cache_values:
        cached = cache_values.get(cache_key)
        return dict(cached) if isinstance(cached, dict) else dict(empty)

    def _lookup(query_turn_id: str | None) -> dict[str, str | None]:
        params: list[object] = [session_id]
        where_parts = [
            "thread_id = ?",
            "target = 'codex_api::endpoint::responses_websocket'",
            "instr(feedback_log_body, '\"service_tier\"') > 0",
            "("
            "instr(feedback_log_body, 'stream_request:model_client.stream_responses_websocket') > 0 "
            'or instr(feedback_log_body, \'websocket event: {"type":"response.created"\') > 0 '
            'or instr(feedback_log_body, \'websocket event: {"type":"response.in_progress"\') > 0 '
            'or instr(feedback_log_body, \'websocket event: {"type":"response.completed"\') > 0'
            ")",
        ]
        if isinstance(query_turn_id, str) and query_turn_id:
            where_parts.append("feedback_log_body like ?")
            params.append(f"%turn.id={query_turn_id}%")

        order_clause = "ts ASC, ts_nanos ASC, id ASC"
        if query_turn_id is None and event_ts is not None:
            order_clause = "abs(ts - ?) ASC, ts ASC, ts_nanos ASC, id ASC"
            params.append(event_ts)

        sql = (
            "SELECT feedback_log_body FROM logs "
            f"WHERE {' AND '.join(where_parts)} "
            f"ORDER BY {order_clause} LIMIT 50"
        )
        resolved = dict(empty)
        try:
            with sqlite3.connect(db_path, timeout=1.0) as conn:
                conn.execute("PRAGMA query_only = ON")
                for (feedback_log_body,) in conn.execute(sql, params):
                    tier = _extract_codex_log_service_tier(feedback_log_body)
                    if not tier:
                        continue
                    body = feedback_log_body or ""
                    if (
                        '"stream":true' in body
                        and "stream_request:model_client.stream_responses_websocket"
                        in body
                    ):
                        resolved["requested"] = tier
                        resolved["requested_source"] = "codex_logs_request"
                        continue
                    if 'websocket event: {"type":"response.completed"' in body:
                        resolved["effective"] = tier
                        resolved["effective_source"] = "codex_logs_response_completed"
                        continue
                    if resolved["effective"] is None and (
                        'websocket event: {"type":"response.in_progress"' in body
                        or 'websocket event: {"type":"response.created"' in body
                    ):
                        resolved["effective"] = tier
                        resolved["effective_source"] = "codex_logs_response_progress"
        except sqlite3.Error:
            return dict(empty)
        return resolved

    resolved_tiers = _lookup(turn_id)
    if (
        resolved_tiers["requested"] is None
        and resolved_tiers["effective"] is None
        and turn_id
    ):
        resolved_tiers = _lookup(None)

    cache_values[cache_key] = dict(resolved_tiers)
    return dict(resolved_tiers)


# ---------------------------------------------------------------------------
# Model name helpers
# ---------------------------------------------------------------------------


def _normalize_model_name(model_name: str | None) -> str | None:
    if not isinstance(model_name, str):
        return None
    normalized = model_name.strip().lower().replace("_", "-")
    if normalized.startswith("openai/"):
        normalized = normalized.split("/", 1)[1]
    normalized = MODEL_PRICING_ALIASES.get(normalized, normalized)
    if normalized not in MODEL_PRICING:
        undated = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", normalized)
        if undated != normalized:
            candidate = MODEL_PRICING_ALIASES.get(undated, undated)
            if candidate in MODEL_PRICING:
                normalized = candidate
    return normalized


def _usage_event_model_name(event: dict | None) -> str | None:
    if not isinstance(event, dict):
        return None

    # Preserve the Excel alias when the response reports only its base model,
    # so Excel requests keep their model identity in usage history.
    for key in ("resolved_model", "requested_model"):
        normalized = _normalize_model_name(event.get(key))
        if excel_models.is_excel_model(normalized):
            return normalized

    for key in ("response_model", "resolved_model", "requested_model"):
        model_name = event.get(key)
        normalized = _normalize_model_name(model_name)
        if normalized:
            return normalized
    return None


def _usage_event_source(event: dict | None) -> str:
    return "codex"


def _native_usage_event_dedupe_key(event: dict | None) -> str | None:
    """Return a stable idempotency key for one native Codex usage observation.

    Native rollout logs can contain the same completed turn in more than one
    rollout file.  The source file is deliberately not part of the normal key:
    identical observations of the same session/turn/model should count once,
    while a changed token snapshot for that turn must remain visible.
    """
    if not isinstance(event, dict):
        return None

    request_id = event.get("request_id")
    source = _usage_event_source(event)
    if source != "codex_native" and not (
        isinstance(request_id, str) and request_id.startswith("codex-native:")
    ):
        return None

    explicit_key = event.get("native_dedupe_key")
    if isinstance(explicit_key, str) and explicit_key.strip():
        return f"native-explicit:{explicit_key.strip()}"

    session_id = event.get("session_id")
    turn_id = event.get("native_turn_id")
    if (
        isinstance(session_id, str)
        and session_id.strip()
        and isinstance(turn_id, str)
        and turn_id.strip()
    ):
        usage = normalize_usage_payload(event.get("usage")) or {}
        model = (
            event.get("response_model")
            or event.get("resolved_model")
            or event.get("requested_model")
            or ""
        )
        identity = {
            "session_id": session_id.strip(),
            "turn_id": turn_id.strip(),
            "model": str(model).strip().lower(),
            "usage": {
                "input_tokens": _coerce_int(usage.get("input_tokens")),
                "cached_input_tokens": _coerce_int(usage.get("cached_input_tokens")),
                "cache_creation_input_tokens": _coerce_int(
                    usage.get("cache_creation_input_tokens")
                ),
                "output_tokens": _coerce_int(usage.get("output_tokens")),
                "reasoning_output_tokens": _coerce_int(
                    usage.get("reasoning_output_tokens")
                ),
                "total_tokens": _coerce_int(usage.get("total_tokens")),
            },
        }
        serialized = json.dumps(
            identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"native-turn:{digest}"

    source_event_key = event.get("native_source_event_key")
    if isinstance(source_event_key, str) and source_event_key.strip():
        return f"native-source:{source_event_key.strip()}"
    return None


def deduplicate_usage_events(events) -> list[dict]:
    """Keep the first occurrence of each duplicate native usage observation.

    This is intentionally native-only: proxied Responses requests do not have a
    comparable durable source-event identity, and collapsing those could hide a
    real retry that reached the upstream service.
    """
    deduplicated: list[dict] = []
    seen_native_keys: set[str] = set()
    for event in events or ():
        if not isinstance(event, dict):
            continue
        key = _native_usage_event_dedupe_key(event)
        if key:
            if key in seen_native_keys:
                continue
            seen_native_keys.add(key)
        deduplicated.append(event)
    return deduplicated


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------


def _pricing_entry_for_model(model_name: str | None) -> dict | None:
    normalized = _normalize_model_name(model_name)
    if not normalized:
        return None
    return MODEL_PRICING.get(normalized)


def _usage_event_cost_breakdown(
    model_name: str | None, usage: dict | None
) -> dict[str, float]:
    breakdown = {
        "input_fresh": 0.0,
        "cached_input": 0.0,
        "cache_creation": 0.0,
        "output": 0.0,
    }
    if not isinstance(usage, dict):
        return breakdown

    entry = _pricing_entry_for_model(model_name)
    if not isinstance(entry, dict):
        return breakdown

    fresh_input_tokens = usage.get("pricing_fresh_input_tokens")
    if fresh_input_tokens is None:
        fresh_input_tokens = usage.get("fresh_input_tokens")
    if fresh_input_tokens is None:
        fresh_input_tokens = usage.get("billable_input_tokens")
    # ``fresh_input_tokens`` is gross input minus cache reads.  A cache write
    # is therefore a subset of fresh input, not an additional input token.
    # Charge its tokens at the cache-creation rate instead of charging them a
    # second time at the normal fresh-input rate below.
    fresh_input_tokens = _coerce_int(
        fresh_input_tokens
        if fresh_input_tokens is not None
        else usage.get("input_tokens")
    )
    output_tokens = _coerce_int(usage.get("output_tokens"))
    cached_input_tokens = _coerce_int(
        usage.get("pricing_cached_input_tokens"), default=None
    )
    if cached_input_tokens is None:
        cached_input_tokens = _coerce_int(usage.get("cached_input_tokens"))
        if (
            cached_input_tokens == 0
            and usage.get("cache_read_input_tokens") is not None
        ):
            cached_input_tokens = _coerce_int(usage.get("cache_read_input_tokens"))
    cache_creation_input_tokens = _coerce_int(
        usage.get("pricing_cache_creation_input_tokens"), default=None
    )
    if cache_creation_input_tokens is None:
        cache_creation_input_tokens = _coerce_int(
            usage.get("cache_creation_input_tokens")
        )
    billed_input_tokens = fresh_input_tokens + cached_input_tokens
    long_context_threshold = _coerce_int(
        entry.get("long_context_threshold"), default=None
    )
    if (
        long_context_threshold is not None
        and billed_input_tokens > long_context_threshold
    ):
        entry = {
            **entry,
            "input_per_million": entry.get(
                "long_context_input_per_million", entry.get("input_per_million")
            ),
            "cached_input_per_million": entry.get(
                "long_context_cached_input_per_million",
                entry.get("cached_input_per_million"),
            ),
            "cache_write_per_million": entry.get(
                "long_context_cache_write_per_million",
                entry.get("cache_write_per_million"),
            ),
            "output_per_million": entry.get(
                "long_context_output_per_million", entry.get("output_per_million")
            ),
        }

    input_rate = _coerce_float(entry.get("input_per_million"))
    output_rate = _coerce_float(entry.get("output_per_million"))
    cached_rate = entry.get("cached_input_per_million")
    cached_rate = _coerce_float(cached_rate, default=input_rate)

    cache_creation_rate = _coerce_float(
        entry.get("cache_write_per_million"), default=None
    )
    if cache_creation_rate is None:
        cache_creation_rate = input_rate

    non_cache_creation_input_tokens = max(
        0, fresh_input_tokens - cache_creation_input_tokens
    )
    breakdown["input_fresh"] = (
        non_cache_creation_input_tokens * input_rate
    ) / 1_000_000.0
    breakdown["cached_input"] = (cached_input_tokens * cached_rate) / 1_000_000.0
    breakdown["cache_creation"] = (
        cache_creation_input_tokens * cache_creation_rate
    ) / 1_000_000.0
    # Responses includes reasoning in output_tokens. reasoning_output_tokens
    # is a diagnostic subset,
    # not an additional billable bucket.
    breakdown["output"] = (output_tokens * output_rate) / 1_000_000.0
    return breakdown


def _usage_event_cost(model_name: str | None, usage: dict | None) -> float:
    return sum(_usage_event_cost_breakdown(model_name, usage).values())


def _fast_service_tier_cost_multiplier(event: dict | None) -> float:
    if _usage_event_model_name(event) == "gpt-5.5":
        return 2.5
    return 2.0


def _usage_event_cost_multiplier(event: dict | None) -> float:
    if not isinstance(event, dict):
        return 1.0
    requested_service_tier = event.get("native_requested_service_tier")
    if (
        isinstance(requested_service_tier, str)
        and requested_service_tier.strip().lower() in FAST_SERVICE_TIERS
    ):
        return _fast_service_tier_cost_multiplier(event)
    service_tier = event.get("native_service_tier")
    if (
        isinstance(service_tier, str)
        and service_tier.strip().lower() in FAST_SERVICE_TIERS
    ):
        return _fast_service_tier_cost_multiplier(event)
    return 1.0


def _usage_event_estimated_cost(
    event: dict | None,
    *,
    model_name: str | None = None,
    usage: dict | None = None,
) -> float:
    resolved_model_name = model_name
    if resolved_model_name is None and isinstance(event, dict):
        resolved_model_name = _usage_event_model_name(event)
    resolved_usage = usage
    if resolved_usage is None and isinstance(event, dict):
        resolved_usage = event.get("usage")
    return _usage_event_cost(
        resolved_model_name, resolved_usage
    ) * _usage_event_cost_multiplier(event)
