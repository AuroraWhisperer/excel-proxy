"""Dashboard payload construction and SQLite cache."""

import asyncio
import excel_upstream
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread
from typing import Callable

from constants import (
    TOKEN_DIR, SQLITE_CACHE_FILE, DASHBOARD_RECENT_REQUEST_LIMIT,
)
from util import (
    _json_default,
    _coerce_float,
    _coerce_int,
    utc_now,
    utc_now_iso,
    _parse_iso_datetime,
    normalize_usage_payload,
    _pricing_entry_for_model,
    _usage_event_model_name,
    _usage_event_source,
    deduplicate_usage_events,
    _usage_event_cost,
    _usage_event_cost_breakdown,
    _usage_event_cost_multiplier,
    _month_key,
    month_key_for_source_row,
    _codex_native_session_id_from_request_id,
)


# ─── SQLite cache state ──────────────────────────────────────────────────────

_sqlite_cache_lock = Lock()
_sqlite_cache_enabled = True
_sqlite_cache_error = None


# ─── Dashboard SSE stream state ──────────────────────────────────────────────

_dashboard_stream_subscribers = set()
_dashboard_stream_lock = Lock()
_dashboard_stream_version = 0


# ─── Runtime dependencies ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class DashboardDependencies:
    snapshot_all_usage_events: Callable[[], list[dict]] = lambda: []
    snapshot_usage_events: Callable[[], list[dict]] = lambda: []
    native_lifecycle_revision: Callable[[], int] = lambda: 0
    snapshot_native_http_timings: Callable[[], list[dict]] = lambda: []
    native_http_timing_revision: Callable[[], int] = lambda: 0
    usage_snapshots_are_deduplicated: bool = False
    prompt_payload: Callable[[object], object] = lambda value: value


# ─── SQLite cache functions ──────────────────────────────────────────────────

class DashboardCacheStore:
    """Owns dashboard SQLite cache lifecycle and adapts it for runtime consumers."""

    def __init__(self):
        # Schema creation and WAL setup are process-level work.  Running them
        # from every dashboard read turns a read-only page refresh into a
        # SQLite write/metadata storm, especially on Windows.
        self._initialized = False
        self._cache_dir_ready = False

    @property
    def lock(self) -> Lock:
        return _sqlite_cache_lock

    def mark_unavailable(self, error: str):
        global _sqlite_cache_enabled, _sqlite_cache_error
        if _sqlite_cache_enabled:
            print(f"[sqlite] cache disabled: {error}", flush=True)
            _sqlite_cache_enabled = False
            _sqlite_cache_error = error

    def connect(self) -> sqlite3.Connection:
        if not _sqlite_cache_enabled:
            raise RuntimeError("sqlite cache disabled")
        if not self._cache_dir_ready:
            cache_dir = os.path.dirname(SQLITE_CACHE_FILE) or TOKEN_DIR
            os.makedirs(cache_dir, exist_ok=True)
            self._cache_dir_ready = True
        connection = sqlite3.connect(SQLITE_CACHE_FILE, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> bool:
        if not _sqlite_cache_enabled:
            return False
        if self._initialized:
            return True
        try:
            with self.lock:
                if self._initialized:
                    return True
                cache_dir = os.path.dirname(SQLITE_CACHE_FILE) or TOKEN_DIR
                os.makedirs(cache_dir, exist_ok=True)
                self._cache_dir_ready = True
                with closing(self.connect()) as connection:
                    # These are deliberately process-startup operations, not
                    # per-request connection setup.  NORMAL is sufficient for
                    # this cache and avoids making every cache write wait for
                    # the strongest possible fsync behavior.
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=NORMAL")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS cache_entries (
                            cache_key TEXT PRIMARY KEY,
                            payload_json TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS archived_usage_events (
                            archive_key TEXT PRIMARY KEY,
                            recorded_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_archived_usage_events_recorded_at
                        ON archived_usage_events (recorded_at DESC)
                        """
                    )
                    connection.commit()
                self._initialized = True
            return True
        except Exception as exc:
            self.mark_unavailable(str(exc))
            return False

    def get(self, cache_key: str) -> dict | None:
        if not self.initialize():
            return None
        try:
            with self.lock:
                with closing(self.connect()) as connection:
                    row = connection.execute(
                        "SELECT payload_json, updated_at FROM cache_entries WHERE cache_key = ?",
                        (cache_key,),
                    ).fetchone()
        except Exception as exc:
            self.mark_unavailable(str(exc))
            return None
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("loaded_at", row["updated_at"])
        return payload

    def get_latest(self, cache_key_prefix: str) -> dict | None:
        if not self.initialize():
            return None
        try:
            with self.lock:
                with closing(self.connect()) as connection:
                    row = connection.execute(
                        "SELECT payload_json, updated_at FROM cache_entries WHERE cache_key LIKE ? ORDER BY updated_at DESC LIMIT 1",
                        (f"{cache_key_prefix}%",),
                    ).fetchone()
        except Exception as exc:
            self.mark_unavailable(str(exc))
            return None
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("loaded_at", row["updated_at"])
        return payload

    def put(self, cache_key: str, payload: dict):
        if not isinstance(payload, dict):
            return
        if not self.initialize():
            return
        updated_at = utc_now_iso()
        serialized = json.dumps(payload, separators=(",", ":"), default=_json_default)
        try:
            with self.lock:
                with closing(self.connect()) as connection:
                    connection.execute(
                        """
                        INSERT INTO cache_entries (cache_key, payload_json, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (cache_key, serialized, updated_at),
                    )
                    connection.commit()
        except Exception as exc:
            self.mark_unavailable(f"failed to write cache key '{cache_key}': {exc}")

    def usage_archive_store(self):
        from usage_tracking import UsageArchiveStore

        return UsageArchiveStore(
            init_storage=self.initialize,
            lock=self.lock,
            connect=self.connect,
            mark_unavailable=self.mark_unavailable,
        )


dashboard_cache_store = DashboardCacheStore()


def _current_billing_month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or utc_now()
    start = datetime(current.year, current.month, 1, tzinfo=timezone.utc)
    if current.month == 12:
        end = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    return start, end


# ─── Dashboard SSE stream ────────────────────────────────────────────────────

class DashboardStreamBroker:
    """Coordinates dashboard SSE subscriptions without exposing module internals."""

    def current_version(self) -> int:
        return _dashboard_stream_version

    def register_listener(self) -> asyncio.Queue[int]:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        with _dashboard_stream_lock:
            _dashboard_stream_subscribers.add(queue)
        return queue

    def unregister_listener(self, queue: asyncio.Queue[int]):
        with _dashboard_stream_lock:
            _dashboard_stream_subscribers.discard(queue)

    def notify_listeners(self):
        global _dashboard_stream_version

        _dashboard_stream_version += 1
        with _dashboard_stream_lock:
            if not _dashboard_stream_subscribers:
                return
            subscribers = list(_dashboard_stream_subscribers)

        for queue in subscribers:
            try:
                queue.put_nowait(_dashboard_stream_version)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(_dashboard_stream_version)
                except asyncio.QueueEmpty:
                    pass
                except RuntimeError:
                    self.unregister_listener(queue)


dashboard_stream_broker = DashboardStreamBroker()


# ─── Usage event session descriptors ─────────────────────────────────────────

def _usage_event_group_key(event: dict | None) -> tuple[str, str]:
    if not isinstance(event, dict):
        return ("codex", "unknown")

    source = _usage_event_source(event)
    group_id = None
    for key in ("session_id", "client_request_id", "request_id", "server_request_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            group_id = value
            break

    if not isinstance(group_id, str) or not group_id:
        group_id = "unknown"
    return (source, group_id)


def _usage_event_session_descriptor(event: dict | None) -> dict[str, str]:
    source, group_id = _usage_event_group_key(event)

    actual_session_id = event.get("session_id") if isinstance(event, dict) else None
    if (not isinstance(actual_session_id, str) or not actual_session_id) and isinstance(event, dict):
        actual_session_id = _codex_native_session_id_from_request_id(event.get("request_id"))
    if isinstance(actual_session_id, str) and actual_session_id:
        group_id = actual_session_id
        return {
            "source": source,
            "group_id": group_id,
            "session_id": actual_session_id,
            "session_kind": "session",
            "session_display_id": actual_session_id,
        }

    client_request_id = event.get("client_request_id") if isinstance(event, dict) else None
    if isinstance(client_request_id, str) and client_request_id:
        return {
            "source": source,
            "group_id": group_id,
            "session_id": "",
            "session_kind": "session",
            "session_display_id": client_request_id,
        }

    request_id = event.get("request_id") if isinstance(event, dict) else None
    if isinstance(request_id, str) and request_id:
        return {
            "source": source,
            "group_id": group_id,
            "session_id": "",
            "session_kind": "session",
            "session_display_id": request_id,
        }

    server_request_id = event.get("server_request_id") if isinstance(event, dict) else None
    if isinstance(server_request_id, str) and server_request_id:
        return {
            "source": source,
            "group_id": group_id,
            "session_id": "",
            "session_kind": "session",
            "session_display_id": server_request_id,
        }

    return {
        "source": source,
        "group_id": group_id,
        "session_id": "",
        "session_kind": "unknown",
        "session_display_id": "unknown",
    }


def seed_cached_payloads_from_sqlite():
    """No-op shim retained for backward compatibility.

    Usage is calculated from locally recorded token counts. There is no legacy
    billing payload to seed from SQLite.
    """
    return None


# ─── Usage aggregation and dashboard building ────────────────────────────────

def _new_usage_aggregate_bucket() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_usd": 0.0,
        "cost_breakdown": {
            "input_fresh": 0.0,
            "cached_input": 0.0,
            "cache_creation": 0.0,
            "output": 0.0,
        },
        "_models": {},
        "_last_activity_dt": None,
        "_first_activity_dt": None,
        "_api_duration_ms": 0,
        "_api_duration_keys": set(),
        "project_path": None,
        "request_count": 0,
        "session_id": None,
        "session_kind": "unknown",
        "session_display_id": None,
    }


def _usage_display_input_tokens(usage: dict) -> int:
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


def _usage_display_total_tokens(usage: dict, *, input_tokens: int, output_tokens: int) -> int:
    """Return the total token volume shown in dashboard rollups.

    Keep the existing fresh-input accounting for stored rollups. The dashboard
    adds cached input separately when displaying the full input volume.
    """
    # A stored ``total_tokens`` value may have been calculated from gross
    # input before the explicit fresh-input field was added. Prefer the
    # normalized display input whenever that field is present so old events do
    # not reintroduce cached reads into the dashboard total.
    if usage.get("fresh_input_tokens") is not None or usage.get("billable_input_tokens") is not None:
        return max(0, input_tokens) + max(0, output_tokens)

    total_tokens = _coerce_int(usage.get("total_tokens"), default=None)
    if total_tokens is not None:
        return max(0, total_tokens)

    gross_input_tokens = _coerce_int(usage.get("input_tokens"), default=None)
    if gross_input_tokens is None:
        gross_input_tokens = max(0, input_tokens)
        gross_input_tokens += max(0, _coerce_int(usage.get("cached_input_tokens")))
        gross_input_tokens += max(0, _coerce_int(usage.get("cache_creation_input_tokens")))
    return max(0, gross_input_tokens) + max(0, output_tokens)


def _usage_request_context_tokens(usage: dict, *, output_tokens: int) -> int:
    """Return gross tokens in one model request for session context display.

    Session rows should not sum the same cached prompt on every turn, but they
    also must not collapse to fresh-only pricing tokens.  Track the largest
    gross request instead, which represents the session's peak context.
    """
    raw_input = _coerce_int(usage.get("input_tokens"), default=None)
    fresh_input = usage.get("fresh_input_tokens")
    if fresh_input is None:
        fresh_input = usage.get("billable_input_tokens")
    cached_input = max(0, _coerce_int(usage.get("cached_input_tokens")))

    candidates = []
    if raw_input is not None:
        candidates.append(max(0, raw_input))
    if fresh_input is not None:
        candidates.append(max(0, _coerce_int(fresh_input)) + cached_input)
    if not candidates:
        candidates.append(cached_input + max(0, _coerce_int(usage.get("cache_creation_input_tokens"))))
    return max(candidates) + max(0, output_tokens)


def _prepare_usage_event(event: dict) -> dict | None:
    """Compute the expensive, event-local dashboard fields once.

    A single event contributes to month, session, and day rollups.  Keeping
    this preparation separate from bucket ingestion avoids normalizing usage,
    parsing timestamps, resolving model pricing, and calculating costs three
    times for every request.
    """
    if not isinstance(event, dict):
        return None

    usage = normalize_usage_payload(event.get("usage")) or {}
    event_cost = event.get("cost_usd")
    cost_multiplier = _usage_event_cost_multiplier(event)
    model_name = _usage_event_model_name(event) or "unknown"
    if not isinstance(event_cost, (int, float)) or not event_cost:
        recomputed_cost = _usage_event_cost(model_name, usage) * cost_multiplier
        if recomputed_cost:
            event_cost = recomputed_cost
        elif not isinstance(event_cost, (int, float)):
            event_cost = 0.0

    input_tokens = _usage_display_input_tokens(usage)
    output_tokens = _coerce_int(usage.get("output_tokens"))
    total_tokens = _usage_display_total_tokens(usage, input_tokens=input_tokens, output_tokens=output_tokens)
    request_context_tokens = _usage_request_context_tokens(usage, output_tokens=output_tokens)
    cached_input_tokens = _coerce_int(usage.get("cached_input_tokens"))
    cache_creation_tokens = _coerce_int(usage.get("cache_creation_input_tokens"))
    reasoning_output_tokens = _coerce_int(usage.get("reasoning_output_tokens"))
    cost_breakdown = _usage_event_cost_breakdown(model_name, usage)
    if cost_multiplier != 1.0:
        cost_breakdown = {key: value * cost_multiplier for key, value in cost_breakdown.items()}

    event_time = _parse_iso_datetime(event.get("finished_at") or event.get("started_at"))
    if event_time is None:
        return None

    return {
        "event_time": event_time,
        "source": _usage_event_source(event),
        "descriptor": _usage_event_session_descriptor(event),
        "project_path": event.get("project_path"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "request_context_tokens": request_context_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "event_cost": event_cost,
        "cost_breakdown": cost_breakdown,
        "model_name": model_name,
    }


def _ingest_usage_event(bucket: dict, event: dict, prepared: dict | None = None):
    if not isinstance(bucket, dict) or not isinstance(event, dict):
        return

    prepared = prepared or _prepare_usage_event(event)
    if not isinstance(prepared, dict):
        return

    event_time = prepared["event_time"]
    current_last = bucket.get("_last_activity_dt")
    if not isinstance(current_last, datetime) or event_time > current_last:
        bucket["_last_activity_dt"] = event_time
    current_first = bucket.get("_first_activity_dt")
    if not isinstance(current_first, datetime) or event_time < current_first:
        bucket["_first_activity_dt"] = event_time

    duration_value = event.get("native_turn_duration_ms")
    if not isinstance(duration_value, (int, float)) or isinstance(duration_value, bool):
        duration_value = event.get("duration_ms")
    if isinstance(duration_value, (int, float)) and not isinstance(duration_value, bool):
        duration_ms = max(0, int(round(float(duration_value))))
        if duration_ms:
            source = _usage_event_source(event)
            if source == "codex_native" or event.get("native_source") == "codex_native":
                duration_key = (
                    event.get("native_turn_id")
                    or event.get("native_source_event_key")
                    or event.get("request_id")
                )
            else:
                duration_key = event.get("request_id") or event.get("client_request_id")
            duration_keys = bucket.setdefault("_api_duration_keys", set())
            if duration_key is None or duration_key not in duration_keys:
                if duration_key is not None:
                    duration_keys.add(duration_key)
                bucket["_api_duration_ms"] = bucket.get("_api_duration_ms", 0) + duration_ms

    project_path = prepared.get("project_path")
    if bucket.get("project_path") is None and isinstance(project_path, str) and project_path:
        bucket["project_path"] = project_path

    input_tokens = prepared["input_tokens"]
    output_tokens = prepared["output_tokens"]
    total_tokens = prepared["total_tokens"]
    cached_input_tokens = prepared["cached_input_tokens"]
    cache_creation_tokens = prepared["cache_creation_tokens"]
    reasoning_output_tokens = prepared["reasoning_output_tokens"]
    model_name = prepared["model_name"]
    cost_breakdown = prepared["cost_breakdown"]

    bucket["input_tokens"] += input_tokens
    bucket["output_tokens"] += output_tokens
    bucket["total_tokens"] += total_tokens
    bucket["cached_input_tokens"] += cached_input_tokens
    bucket["cache_creation_tokens"] += cache_creation_tokens
    bucket["reasoning_output_tokens"] += reasoning_output_tokens
    bucket["cost_usd"] += _coerce_float(prepared["event_cost"])
    bucket_cost_breakdown = bucket.setdefault(
        "cost_breakdown",
        {"input_fresh": 0.0, "cached_input": 0.0, "cache_creation": 0.0, "output": 0.0},
    )
    for key, value in cost_breakdown.items():
        bucket_cost_breakdown[key] = bucket_cost_breakdown.get(key, 0.0) + _coerce_float(value)
    bucket["request_count"] += 1
    model_bucket = bucket["_models"].setdefault(model_name, {"inputTokens": 0})
    model_bucket["inputTokens"] += input_tokens


def _build_api_cost_estimate(events: list[dict], start: datetime, end: datetime) -> dict:
    """Reprice recorded usage; stored costs and subscription quota are not bills."""
    def empty_bucket():
        return {
            "request_count": 0, "priced_requests": 0,
            "unpriced_requests": 0, "missing_usage_requests": 0,
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "cost_breakdown": dict.fromkeys(
                ("input_fresh", "cached_input", "cache_creation", "output"), 0.0
            ),
        }

    total = empty_bucket()
    models = {}
    for event in events:
        event_time = _parse_iso_datetime(event.get("finished_at") or event.get("started_at"))
        if event_time is None or not start <= event_time < end:
            continue
        model = _usage_event_model_name(event) or "unknown"
        rates = _pricing_entry_for_model(model)
        if model not in models:
            models[model] = {**empty_bucket(), "model": model,
                             "rates": dict(rates) if rates else None}
        row = models[model]
        raw_usage = event.get("usage")
        has_usage = isinstance(raw_usage, dict) and (
            raw_usage.get("input_tokens") is not None or raw_usage.get("prompt_tokens") is not None
        ) and (
            raw_usage.get("output_tokens") is not None or raw_usage.get("completion_tokens") is not None
        )
        usage = normalize_usage_payload(raw_usage) if has_usage else None
        breakdown = _usage_event_cost_breakdown(model, usage)
        multiplier = _usage_event_cost_multiplier(event)
        for bucket in (total, row):
            bucket["request_count"] += 1
            bucket["unpriced_requests"] += int(rates is None)
            bucket["missing_usage_requests"] += int(not has_usage)
            if usage is not None:
                cached = _coerce_int(usage.get("cached_input_tokens"))
                bucket["input_tokens"] += _usage_display_input_tokens(usage) + cached
                bucket["cached_input_tokens"] += cached
                bucket["output_tokens"] += _coerce_int(usage.get("output_tokens"))
            if rates is not None and has_usage:
                bucket["priced_requests"] += 1
                for key, value in breakdown.items():
                    bucket["cost_breakdown"][key] += value * multiplier

    for bucket in (total, *models.values()):
        bucket["complete"] = not (bucket["unpriced_requests"] or bucket["missing_usage_requests"])
        bucket["cost_usd"] = (
            sum(bucket["cost_breakdown"].values())
            if bucket["priced_requests"] or not bucket["request_count"] else None
        )
    total["models"] = sorted(
        models.values(), key=lambda row: (-(row["cost_usd"] or 0), row["model"])
    )
    total["currency"] = "USD"
    total["pricing_basis"] = "reference_api_rates"
    return total


def _dashboard_event_key(event: dict) -> tuple:
    """Return a stable key for an event across recent-history compaction.

    UsageTracker moves the oldest detailed rows into SQLite and recreates the
    in-memory summary dictionaries.  Object identity therefore cannot be used
    for incremental aggregation.  Request IDs are stable for proxied rows;
    native rows also include their token snapshot so a changed observation is
    not accidentally treated as the old one.
    """
    if not isinstance(event, dict):
        return ("invalid", id(event))

    request_id = event.get("request_id")
    native_source = event.get("native_source")
    is_native = native_source == "codex_native" or (
        isinstance(request_id, str) and request_id.startswith("codex-native:")
    )
    if is_native:
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        return (
            "native",
            request_id,
            event.get("native_turn_id"),
            event.get("native_source_event_key"),
            event.get("native_dedupe_key"),
            event.get("resolved_model") or event.get("requested_model"),
            event.get("session_id"),
            event.get("finished_at") or event.get("started_at"),
            _coerce_int(usage.get("input_tokens")),
            _coerce_int(usage.get("cached_input_tokens")),
            _coerce_int(usage.get("cache_creation_input_tokens")),
            _coerce_int(usage.get("output_tokens")),
            _coerce_int(usage.get("reasoning_output_tokens")),
            _coerce_int(usage.get("total_tokens")),
        )

    if isinstance(request_id, str) and request_id:
        return (
            "request",
            request_id,
            event.get("finished_at") or event.get("started_at"),
            event.get("path"),
        )

    # Events without an ID are not normal proxy records.  Retain the old
    # behavior rather than collapsing two independent attempts that happen to
    # have the same sparse fields.
    return ("object", id(event))


class _DashboardUsageAccumulator:
    """Incremental all-time/month/session/day usage materialization.

    The archive can contain hundreds of thousands of immutable native events.
    Rebuilding every aggregate from that archive for each browser refresh is
    needlessly expensive.  This accumulator processes only newly-seen events
    and keeps the finalized dashboard rollups in memory for the process.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.month_buckets: dict[str, dict[str, dict]] = {}
        self.session_buckets: dict[str, dict[str, dict]] = {}
        self.day_buckets: dict[str, dict[str, dict]] = {}
        self._event_keys: set[tuple] = set()
        self._first_event_key: tuple | None = None
        self._local_usage_cache: dict | None = None
        self._daily_usage_cache: dict[tuple[str, str], list[dict]] = {}

    def update(self, usage_events: list[dict]):
        events = [event for event in usage_events if isinstance(event, dict)]
        if not events:
            if self._event_keys:
                self.reset()
            return

        first_event_key = _dashboard_event_key(events[0])
        if self._event_keys and first_event_key != self._first_event_key:
            # History was replaced/reloaded rather than appended.  Rebuild
            # once so callers never see stale totals.
            self.reset()

        if not self._event_keys:
            new_events = [(_dashboard_event_key(event), event) for event in events]
        else:
            # Normal history updates append at the end.  Walk backward until
            # the first already-materialized event instead of deriving a
            # 160K-entry key set on every SSE notification.
            new_events = []
            for event in reversed(events):
                event_key = _dashboard_event_key(event)
                if event_key in self._event_keys:
                    break
                new_events.append((event_key, event))
            new_events.reverse()

        if new_events:
            self._local_usage_cache = None
            self._daily_usage_cache.clear()
        for event_key, event in new_events:
            prepared = _prepare_usage_event(event)
            if not isinstance(prepared, dict):
                self._event_keys.add(event_key)
                continue

            source = prepared["source"]
            event_time = prepared["event_time"]
            descriptor = prepared["descriptor"]
            month_key = _month_key(event_time)
            day_key = event_time.astimezone(timezone.utc).strftime("%Y-%m-%d")

            month_bucket = self.month_buckets.setdefault(source, {}).setdefault(
                month_key, _new_usage_aggregate_bucket()
            )
            _ingest_usage_event(month_bucket, event, prepared)

            group_id = descriptor.get("group_id") if isinstance(descriptor, dict) else None
            if not isinstance(group_id, str) or not group_id:
                group_id = event.get("server_request_id") or event.get("request_id") or "unknown"
            session_bucket = self.session_buckets.setdefault(source, {}).setdefault(
                group_id, _new_usage_aggregate_bucket()
            )
            if session_bucket.get("session_display_id") is None and descriptor.get("session_display_id"):
                session_bucket["session_display_id"] = descriptor["session_display_id"]
            if not session_bucket.get("session_id") and descriptor.get("session_id"):
                session_bucket["session_id"] = descriptor["session_id"]
            if session_bucket.get("session_kind") in {None, "", "unknown"} and descriptor.get("session_kind"):
                session_bucket["session_kind"] = descriptor["session_kind"]
            _ingest_usage_event(session_bucket, event, prepared)
            session_bucket["_peak_request_context_tokens"] = max(
                int(session_bucket.get("_peak_request_context_tokens") or 0),
                int(prepared.get("request_context_tokens") or 0),
            )

            day_bucket = self.day_buckets.setdefault(source, {}).setdefault(
                day_key, _new_usage_aggregate_bucket()
            )
            _ingest_usage_event(day_bucket, event, prepared)
            self._event_keys.add(event_key)

        self._first_event_key = first_event_key

    def collect_local_usage(self) -> dict:
        if self._local_usage_cache is not None:
            return self._local_usage_cache

        normalized_months = []
        normalized_sessions = []
        for source in sorted(set(self.month_buckets) | set(self.session_buckets)):
            for month_key, bucket in self.month_buckets.get(source, {}).items():
                normalized_months.append(
                    _normalize_month_row(
                        source,
                        _finalize_usage_bucket(bucket, source, month=month_key),
                    )
                )
            for _session_key, bucket in self.session_buckets.get(source, {}).items():
                normalized_sessions.append(
                    normalize_session(
                        source,
                        _finalize_usage_bucket(
                            bucket,
                            source,
                            session_id=bucket.get("session_id"),
                            session_rollup=True,
                        ),
                    )
                )

        normalized_sessions.sort(key=lambda item: item.get("last_activity") or "", reverse=True)
        self._local_usage_cache = {
            "month_rows": normalized_months,
            "session_count": len(normalized_sessions),
            "recent_sessions": normalized_sessions[:20],
            "month_history": _combine_month_rows(normalized_months),
            "errors": [],
        }
        return self._local_usage_cache

    def collect_daily_usage(self, start_at: datetime, end_at: datetime) -> list[dict]:
        start_day = start_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        end_day = end_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        cache_key = (start_day, end_day)
        cached = self._daily_usage_cache.get(cache_key)
        if cached is not None:
            return cached
        normalized_days = []
        for source, source_days in self.day_buckets.items():
            for day_key, bucket in source_days.items():
                if day_key < start_day or day_key >= end_day:
                    continue
                normalized_days.append(
                    _normalize_day_row(
                        source,
                        _finalize_usage_bucket(bucket, source),
                        day_key,
                    )
                )
        result = _combine_day_rows(normalized_days)
        self._daily_usage_cache[cache_key] = result
        return result


def _finalize_usage_bucket(
    bucket: dict,
    source: str,
    *,
    session_id: str | None = None,
    month: str | None = None,
    session_rollup: bool = False,
) -> dict:
    if not isinstance(bucket, dict):
        return {}

    last_activity_dt = bucket.get("_last_activity_dt")
    last_activity = last_activity_dt.isoformat() if isinstance(last_activity_dt, datetime) else None
    first_activity_dt = bucket.get("_first_activity_dt")
    wall_duration_ms = None
    if isinstance(first_activity_dt, datetime) and isinstance(last_activity_dt, datetime):
        wall_duration_ms = max(0, int(round((last_activity_dt - first_activity_dt).total_seconds() * 1000)))
    models = bucket.get("_models") if isinstance(bucket.get("_models"), dict) else {}
    effective_session_id = session_id if session_id is not None else bucket.get("session_id")
    if not isinstance(effective_session_id, str):
        effective_session_id = None
    effective_display_id = bucket.get("session_display_id")
    if not isinstance(effective_display_id, str) or not effective_display_id:
        effective_display_id = effective_session_id
    cost_breakdown = bucket.get("cost_breakdown") if isinstance(bucket.get("cost_breakdown"), dict) else {}
    result = {
        "sessionId": effective_session_id,
        "sessionKind": bucket.get("session_kind") or "unknown",
        "sessionDisplayId": effective_display_id,
        "lastActivity": last_activity,
        "wallDurationMs": wall_duration_ms,
        "apiDurationMs": max(0, int(bucket.get("_api_duration_ms") or 0)),
        "projectPath": bucket.get("project_path"),
        "inputTokens": bucket.get("input_tokens", 0),
        "outputTokens": bucket.get("output_tokens", 0),
        "totalTokens": (
            bucket.get("_peak_request_context_tokens", 0)
            if session_rollup
            else bucket.get("total_tokens", 0)
        ),
        "requestCount": bucket.get("request_count", 0),
        "costBreakdown": {
            "input_fresh": round(_coerce_float(cost_breakdown.get("input_fresh")), 6),
            "cached_input": round(_coerce_float(cost_breakdown.get("cached_input")), 6),
            "cache_creation": round(_coerce_float(cost_breakdown.get("cache_creation")), 6),
            "output": round(_coerce_float(cost_breakdown.get("output")), 6),
        },
    }

    if month is not None:
        result["month"] = month

    result["cachedInputTokens"] = bucket.get("cached_input_tokens", 0)
    result["reasoningOutputTokens"] = bucket.get("reasoning_output_tokens", 0)
    result["costUSD"] = bucket.get("cost_usd", 0.0)
    result["models"] = {name: value for name, value in models.items()}

    return result


def _normalize_usage_rollup(source: str, row: dict) -> dict:
    models = list((row.get("models") or {}).keys())
    cost_usd = _coerce_float(row.get("costUSD"))
    cached_tokens = _coerce_int(row.get("cachedInputTokens"))
    cache_creation_tokens = 0
    reasoning_tokens = _coerce_int(row.get("reasoningOutputTokens"))
    raw_cost_breakdown = row.get("costBreakdown")
    cost_breakdown = {
        "input_fresh": 0.0,
        "cached_input": 0.0,
        "cache_creation": 0.0,
        "output": 0.0,
    }
    if isinstance(raw_cost_breakdown, dict):
        cost_breakdown["input_fresh"] = _coerce_float(raw_cost_breakdown.get("input_fresh"))
        cost_breakdown["cached_input"] = _coerce_float(raw_cost_breakdown.get("cached_input"))
        cost_breakdown["cache_creation"] = _coerce_float(raw_cost_breakdown.get("cache_creation"))
        cost_breakdown["output"] = _coerce_float(raw_cost_breakdown.get("output"))
    input_cost_usd = round(
        cost_breakdown["input_fresh"]
        + cost_breakdown["cached_input"]
        + cost_breakdown["cache_creation"],
        6,
    )
    output_cost_usd = round(cost_breakdown["output"], 6)

    return {
        "source": source,
        "input_tokens": _coerce_int(row.get("inputTokens")),
        "output_tokens": _coerce_int(row.get("outputTokens")),
        "total_tokens": _coerce_int(row.get("totalTokens")),
        "cached_input_tokens": cached_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "request_count": _coerce_int(row.get("requestCount")),
        "cost_usd": cost_usd,
        "cost_breakdown": cost_breakdown,
        "input_cost_usd": input_cost_usd,
        "output_cost_usd": output_cost_usd,
        "models": models,
    }


def normalize_session(source: str, session: dict) -> dict:
    return {
        **_normalize_usage_rollup(source, session),
        "session_id": session.get("sessionId"),
        "session_kind": session.get("sessionKind") or "unknown",
        "session_display_id": session.get("sessionDisplayId") or session.get("sessionId"),
        "last_activity": session.get("lastActivity"),
        "wall_duration_ms": session.get("wallDurationMs"),
        "api_duration_ms": session.get("apiDurationMs"),
        "project_path": session.get("projectPath"),
    }


def _normalize_month_row(source: str, row: dict) -> dict:
    return {
        **_normalize_usage_rollup(source, row),
        "month_key": month_key_for_source_row(source, row),
        "month_label": row.get("month"),
    }


def _normalize_day_row(source: str, row: dict, day_key: str) -> dict:
    return {
        **_normalize_usage_rollup(source, row),
        "day_key": day_key,
        "day_label": day_key,
    }


def _combine_month_rows(rows: list[dict]) -> list[dict]:
    grouped = {}
    for row in rows:
        month_key = row.get("month_key")
        if not month_key:
            continue
        current = grouped.setdefault(
            month_key,
            {
                "month_key": month_key,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_output_tokens": 0,
                "request_count": 0,
                "cost_usd": 0.0,
                "cost_breakdown": {
                    "input_fresh": 0.0,
                    "cached_input": 0.0,
                    "cache_creation": 0.0,
                    "output": 0.0,
                },
                "sources": {},
            },
        )
        current["input_tokens"] += row.get("input_tokens", 0)
        current["output_tokens"] += row.get("output_tokens", 0)
        current["total_tokens"] += row.get("total_tokens", 0)
        current["cached_input_tokens"] += row.get("cached_input_tokens", 0)
        current["cache_creation_tokens"] += row.get("cache_creation_tokens", 0)
        current["reasoning_output_tokens"] += row.get("reasoning_output_tokens", 0)
        current["request_count"] += row.get("request_count", 0)
        current["cost_usd"] += row.get("cost_usd", 0.0)
        for key, value in (row.get("cost_breakdown") or {}).items():
            current["cost_breakdown"][key] = current["cost_breakdown"].get(key, 0.0) + _coerce_float(value)
        current["sources"][row["source"]] = row

    return [
        grouped[key] | {"cost_usd": round(grouped[key]["cost_usd"], 4)}
        for key in sorted(grouped.keys(), reverse=True)
    ]


def _combine_day_rows(rows: list[dict]) -> list[dict]:
    grouped = {}
    for row in rows:
        day_key = row.get("day_key")
        if not day_key:
            continue
        current = grouped.setdefault(
            day_key,
            {
                "day_key": day_key,
                "day_label": row.get("day_label") or day_key,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_output_tokens": 0,
                "request_count": 0,
                "cost_usd": 0.0,
                "cost_breakdown": {
                    "input_fresh": 0.0,
                    "cached_input": 0.0,
                    "cache_creation": 0.0,
                    "output": 0.0,
                },
                "sources": {},
            },
        )
        current["input_tokens"] += row.get("input_tokens", 0)
        current["output_tokens"] += row.get("output_tokens", 0)
        current["total_tokens"] += row.get("total_tokens", 0)
        current["cached_input_tokens"] += row.get("cached_input_tokens", 0)
        current["cache_creation_tokens"] += row.get("cache_creation_tokens", 0)
        current["reasoning_output_tokens"] += row.get("reasoning_output_tokens", 0)
        current["request_count"] += row.get("request_count", 0)
        current["cost_usd"] += row.get("cost_usd", 0.0)
        for key, value in (row.get("cost_breakdown") or {}).items():
            current["cost_breakdown"][key] = current["cost_breakdown"].get(key, 0.0) + _coerce_float(value)
        current["sources"][row["source"]] = row

    return [
        grouped[key] | {"cost_usd": round(grouped[key]["cost_usd"], 4)}
        for key in sorted(grouped.keys())
    ]


def _combine_usage_rows(rows: list[dict], *, month_key: str | None = None) -> dict:
    per_source = {}
    for row in rows:
        source = row.get("source")
        if not isinstance(source, str) or not source:
            continue
        current = per_source.setdefault(
            source,
            {
                "source": source,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_output_tokens": 0,
                "request_count": 0,
                "cost_usd": 0.0,
                "cost_breakdown": {
                    "input_fresh": 0.0,
                    "cached_input": 0.0,
                    "cache_creation": 0.0,
                    "output": 0.0,
                },
                "models": [],
            },
        )
        current["input_tokens"] += row.get("input_tokens", 0)
        current["output_tokens"] += row.get("output_tokens", 0)
        current["total_tokens"] += row.get("total_tokens", 0)
        current["cached_input_tokens"] += row.get("cached_input_tokens", 0)
        current["cache_creation_tokens"] += row.get("cache_creation_tokens", 0)
        current["reasoning_output_tokens"] += row.get("reasoning_output_tokens", 0)
        current["request_count"] += row.get("request_count", 0)
        current["cost_usd"] += row.get("cost_usd", 0.0)
        for key, value in (row.get("cost_breakdown") or {}).items():
            current["cost_breakdown"][key] = current["cost_breakdown"].get(key, 0.0) + _coerce_float(value)
        current["models"] = sorted(set(current["models"]) | set(row.get("models") or []))

    combined = {
        "input_tokens": sum(item.get("input_tokens", 0) for item in per_source.values()),
        "output_tokens": sum(item.get("output_tokens", 0) for item in per_source.values()),
        "total_tokens": sum(item.get("total_tokens", 0) for item in per_source.values()),
        "cached_input_tokens": sum(item.get("cached_input_tokens", 0) for item in per_source.values()),
        "cache_creation_tokens": sum(item.get("cache_creation_tokens", 0) for item in per_source.values()),
        "reasoning_output_tokens": sum(item.get("reasoning_output_tokens", 0) for item in per_source.values()),
        "request_count": sum(item.get("request_count", 0) for item in per_source.values()),
        "cost_usd": round(sum(item.get("cost_usd", 0.0) for item in per_source.values()), 4),
        "cost_breakdown": {
            "input_fresh": round(sum(item.get("cost_breakdown", {}).get("input_fresh", 0.0) for item in per_source.values()), 6),
            "cached_input": round(sum(item.get("cost_breakdown", {}).get("cached_input", 0.0) for item in per_source.values()), 6),
            "cache_creation": round(sum(item.get("cost_breakdown", {}).get("cache_creation", 0.0) for item in per_source.values()), 6),
            "output": round(sum(item.get("cost_breakdown", {}).get("output", 0.0) for item in per_source.values()), 6),
        },
        "sources": {
            source: item
            | {
                "cost_usd": round(item.get("cost_usd", 0.0), 4),
                "cost_breakdown": {
                    "input_fresh": round(item.get("cost_breakdown", {}).get("input_fresh", 0.0), 6),
                    "cached_input": round(item.get("cost_breakdown", {}).get("cached_input", 0.0), 6),
                    "cache_creation": round(item.get("cost_breakdown", {}).get("cache_creation", 0.0), 6),
                    "output": round(item.get("cost_breakdown", {}).get("output", 0.0), 6),
                },
            }
            for source, item in per_source.items()
        },
    }
    if month_key is not None:
        combined["month_key"] = month_key
    return combined


def _empty_day_history_row(day_key: str) -> dict:
    return {
        "day_key": day_key,
        "day_label": day_key,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_output_tokens": 0,
        "request_count": 0,
        "cost_usd": 0.0,
        "cost_breakdown": {
            "input_fresh": 0.0,
            "cached_input": 0.0,
            "cache_creation": 0.0,
            "output": 0.0,
        },
        "sources": {},
    }


class DashboardService:
    """Owns dashboard aggregation with explicit usage callbacks and runtime hooks."""

    def __init__(
        self,
        *,
        dependencies: DashboardDependencies | None = None,
        utc_now: Callable[[], datetime] = utc_now,
        utc_now_iso: Callable[[], str] = utc_now_iso,
        sqlite_cache_put: Callable[[str, dict], None] = dashboard_cache_store.put,
        notify_dashboard_stream_listeners: Callable[[], None] = dashboard_stream_broker.notify_listeners,
        stream_broker: "DashboardStreamBroker | None" = None,
        thread_class: Callable[[], type] | type = Thread,
    ):
        self.dependencies = dependencies or DashboardDependencies()
        self.utc_now = utc_now
        self.utc_now_iso = utc_now_iso
        self.sqlite_cache_put = sqlite_cache_put
        self.notify_dashboard_stream_listeners = notify_dashboard_stream_listeners
        self._stream_broker = stream_broker or dashboard_stream_broker
        self.thread_class = thread_class
        # Coalesce concurrent build_payload() calls and keep the materialized
        # payload until new data arrives.  The UI asks for ``refresh=1`` on manual
        # refreshes; that must not turn an unchanged 160K-row archive into a
        # full CPU/SSD workload.  Direct callers can still use
        # force_refresh=True to explicitly bypass this policy.
        self._payload_cache_value: dict | None = None
        self._payload_cache_stream_version: int = -1
        self._payload_cache_calendar_key: tuple[int, int, int] | None = None
        self._payload_cache_native_lifecycle_revision: int = -1
        self._payload_cache_native_http_timing_revision: int = -1
        self._payload_cache_lock = Lock()
        self._payload_build_lock = Lock()
        self._usage_accumulator = _DashboardUsageAccumulator()
        self._usage_accumulator_native_lifecycle_revision: int = -1

    def _resolved_thread_class(self):
        resolved_thread_class = self.thread_class
        if callable(resolved_thread_class) and not isinstance(resolved_thread_class, type):
            resolved_thread_class = resolved_thread_class()
        return resolved_thread_class

    def build_payload(self, force_refresh: bool = False, *, prefer_cached: bool = False) -> dict:
        stream_version = self._stream_broker.current_version()
        native_lifecycle_revision = self.dependencies.native_lifecycle_revision()
        native_http_timing_revision = self.dependencies.native_http_timing_revision()
        now = self.utc_now()
        calendar_key = (now.year, now.month, now.day)
        with self._payload_cache_lock:
            cached = self._payload_cache_value
            cached_version = self._payload_cache_stream_version
            cached_calendar_key = self._payload_cache_calendar_key
            cached_native_lifecycle_revision = self._payload_cache_native_lifecycle_revision
            cached_native_http_timing_revision = self._payload_cache_native_http_timing_revision

        if (
            cached is not None
            and cached_version == stream_version
            and cached_calendar_key == calendar_key
            and cached_native_lifecycle_revision == native_lifecycle_revision
            and cached_native_http_timing_revision == native_http_timing_revision
        ):
            if not force_refresh or prefer_cached:
                return cached
        with self._payload_build_lock:
            # A concurrent preload, API refresh, and SSE update should share
            # one expensive build rather than each scanning the archive.
            stream_version = self._stream_broker.current_version()
            native_lifecycle_revision = self.dependencies.native_lifecycle_revision()
            native_http_timing_revision = self.dependencies.native_http_timing_revision()
            now = self.utc_now()
            calendar_key = (now.year, now.month, now.day)
            with self._payload_cache_lock:
                cached = self._payload_cache_value
                cached_version = self._payload_cache_stream_version
                cached_calendar_key = self._payload_cache_calendar_key
                cached_native_lifecycle_revision = self._payload_cache_native_lifecycle_revision
                cached_native_http_timing_revision = self._payload_cache_native_http_timing_revision
            if (
                cached is not None
                and cached_version == stream_version
                and cached_calendar_key == calendar_key
                and cached_native_lifecycle_revision == native_lifecycle_revision
                and cached_native_http_timing_revision == native_http_timing_revision
            ):
                if not force_refresh or prefer_cached:
                    return cached
            build_version = stream_version
            result = self._build_payload_uncached()
            with self._payload_cache_lock:
                self._payload_cache_value = result
                self._payload_cache_stream_version = build_version
                self._payload_cache_calendar_key = calendar_key
                self._payload_cache_native_lifecycle_revision = native_lifecycle_revision
                self._payload_cache_native_http_timing_revision = native_http_timing_revision
            return result

    def _build_payload_uncached(self) -> dict:
        now = self.utc_now()
        month_start, month_end = _current_billing_month_bounds(now)
        current_month_key = _month_key(now)
        current_day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        usage_events = [event for event in self.dependencies.snapshot_all_usage_events()
                        if excel_upstream.is_excel_model(_usage_event_model_name(event))]
        detailed_usage_events = [event for event in self.dependencies.snapshot_usage_events()
                                 if excel_upstream.is_excel_model(_usage_event_model_name(event))]
        native_lifecycle_revision = self.dependencies.native_lifecycle_revision()
        native_http_timings = self.dependencies.snapshot_native_http_timings()
        if self._usage_accumulator_native_lifecycle_revision != native_lifecycle_revision:
            self._usage_accumulator.reset()
            self._usage_accumulator_native_lifecycle_revision = native_lifecycle_revision
        if not self.dependencies.usage_snapshots_are_deduplicated:
            usage_events = deduplicate_usage_events(usage_events)
            detailed_usage_events = deduplicate_usage_events(detailed_usage_events)
        self._usage_accumulator.update(usage_events)
        local_usage = self._usage_accumulator.collect_local_usage()
        month_rows = list(local_usage.get("month_rows") or [])
        current_month_usage = _combine_usage_rows(
            [row for row in month_rows if row.get("month_key") == current_month_key],
            month_key=current_month_key,
        )
        all_time_usage = _combine_usage_rows(month_rows)
        all_time_usage["months_tracked"] = len(local_usage.get("month_history") or [])
        daily_history = self._usage_accumulator.collect_daily_usage(month_start, month_end)
        daily_history_by_key = {row["day_key"]: row for row in daily_history if isinstance(row.get("day_key"), str)}
        filled_daily_history = []
        day_cursor = month_start
        while day_cursor <= current_day_start:
            day_key = day_cursor.strftime("%Y-%m-%d")
            filled_daily_history.append(daily_history_by_key.get(day_key) or _empty_day_history_row(day_key))
            day_cursor += timedelta(days=1)

        # Sort newest-first and trim to DASHBOARD_RECENT_REQUEST_LIMIT before
        # constructing dashboard rows, so we don't pay copy/sort cost on
        # events that won't ship. Older detailed rows still live in the
        # in-memory deque and stay reachable via /api/request-prompt.
        sorted_events = sorted(
            detailed_usage_events,
            key=lambda item: item.get("finished_at") or item.get("started_at") or "",
            reverse=True,
        )[:DASHBOARD_RECENT_REQUEST_LIMIT]
        recent_requests = [self._dashboard_request_event(event) for event in sorted_events]

        return {
            "generated_at": now.isoformat(),
            "backend": "excel",
            "current_month": {
                "label": current_month_key,
                "start_at": month_start.isoformat(),
                "end_at": month_end.isoformat(),
                "proxy_requests": current_month_usage.get("request_count", 0),
                "sessions": local_usage.get("session_count", 0),
                "usage": current_month_usage,
                "api_cost_estimate": _build_api_cost_estimate(usage_events, month_start, month_end),
                "daily_history": filled_daily_history,
            },
            "all_time": {
                "proxy_requests": len(usage_events),
                "archived_requests": max(len(usage_events) - len(detailed_usage_events), 0),
                "detailed_requests": len(detailed_usage_events),
                "sessions": local_usage.get("session_count", 0),
                "usage": all_time_usage,
            },
            "recent_sessions": local_usage.get("recent_sessions") or [],
            "recent_requests": recent_requests,
            "native_http_timings": native_http_timings,
            "month_history": (local_usage.get("month_history") or [])[:12],
        }

    def _dashboard_request_event(self, event: dict) -> dict:
        # Strip the prompt from the bulk payload. Returning up to
        # DETAILED_REQUEST_HISTORY_LIMIT full prompts per refresh dominated server
        # CPU and inflated the response by tens of MB. The UI only renders the
        # prompt for the currently-selected row, so it lazy-fetches via
        # /api/request-prompt/{request_id} when the user opens a row.
        if not isinstance(event, dict):
            return {}
        result = dict(event)
        if "request_prompt" in result:
            result.pop("request_prompt", None)
            result["request_prompt_available"] = True
        prepared = _prepare_usage_event(event)
        if isinstance(prepared, dict):
            cost_breakdown = prepared.get("cost_breakdown")
            if isinstance(cost_breakdown, dict):
                normalized_breakdown = {
                    "input_fresh": round(_coerce_float(cost_breakdown.get("input_fresh")), 6),
                    "cached_input": round(_coerce_float(cost_breakdown.get("cached_input")), 6),
                    "cache_creation": round(_coerce_float(cost_breakdown.get("cache_creation")), 6),
                    "output": round(_coerce_float(cost_breakdown.get("output")), 6),
                }
                result.setdefault("cost_breakdown", normalized_breakdown)
                result.setdefault(
                    "input_cost_usd",
                    round(
                        normalized_breakdown["input_fresh"]
                        + normalized_breakdown["cached_input"]
                        + normalized_breakdown["cache_creation"],
                        6,
                    ),
                )
                result.setdefault("output_cost_usd", normalized_breakdown["output"])
        return result

    # ─── Stream broker delegation ─────────────────────────────────────────────

    def register_stream_listener(self) -> asyncio.Queue:
        return self._stream_broker.register_listener()

    def unregister_stream_listener(self, queue: asyncio.Queue):
        self._stream_broker.unregister_listener(queue)

    def current_stream_version(self) -> int:
        return self._stream_broker.current_version()


# ─── Public factory API ───────────────────────────────────────────────────────


def create_dashboard_service(
    dependencies: DashboardDependencies,
    **kwargs,
) -> DashboardService:
    """Create a fully-wired DashboardService. Encapsulates cache/broker setup."""
    return DashboardService(
        dependencies=dependencies,
        sqlite_cache_put=dashboard_cache_store.put,
        notify_dashboard_stream_listeners=dashboard_stream_broker.notify_listeners,
        stream_broker=dashboard_stream_broker,
        **kwargs,
    )


def create_usage_archive_store():
    """Create a usage archive store backed by the dashboard cache."""
    return dashboard_cache_store.usage_archive_store()


def initialize():
    """Seed cached payloads from SQLite. Call once at startup."""
    seed_cached_payloads_from_sqlite()
