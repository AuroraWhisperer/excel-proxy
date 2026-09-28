"""Dashboard payload construction and cache integration."""

import asyncio
import excel_models
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Callable

from constants import (
    DASHBOARD_RECENT_REQUEST_LIMIT,
)
from util import (
    _coerce_float,
    utc_now,
    _month_key,
)
from usage_metrics import _usage_event_model_name, deduplicate_usage_events
from usage_aggregation import (
    UsageAccumulator,
    prepare_usage_event as _prepare_usage_event,
    combine_usage_rows,
    empty_day_history_row,
)
from api_cost_estimates import (
    build_api_cost_estimate as _build_api_cost_estimate,
    attach_account_cycle_estimates,
)


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


def _current_billing_month_bounds(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
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


# ─── Dashboard payload cache and service ─────────────────────────────────────


class DashboardService:
    """Assemble dashboard snapshots and own payload caching and notifications."""

    def __init__(
        self,
        *,
        dependencies: DashboardDependencies | None = None,
        utc_now: Callable[[], datetime] = utc_now,
        notify_dashboard_stream_listeners: Callable[
            [], None
        ] = dashboard_stream_broker.notify_listeners,
        stream_broker: "DashboardStreamBroker | None" = None,
    ):
        self.dependencies = dependencies or DashboardDependencies()
        self.utc_now = utc_now
        self.notify_dashboard_stream_listeners = notify_dashboard_stream_listeners
        self._stream_broker = stream_broker or dashboard_stream_broker
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
        self._usage_accumulator = UsageAccumulator()
        self._usage_accumulator_native_lifecycle_revision: int = -1

    def build_payload(
        self, force_refresh: bool = False, *, prefer_cached: bool = False
    ) -> dict:
        stream_version = self._stream_broker.current_version()
        native_lifecycle_revision = self.dependencies.native_lifecycle_revision()
        native_http_timing_revision = self.dependencies.native_http_timing_revision()
        now = self.utc_now()
        calendar_key = (now.year, now.month, now.day)
        with self._payload_cache_lock:
            cached = self._payload_cache_value
            cached_version = self._payload_cache_stream_version
            cached_calendar_key = self._payload_cache_calendar_key
            cached_native_lifecycle_revision = (
                self._payload_cache_native_lifecycle_revision
            )
            cached_native_http_timing_revision = (
                self._payload_cache_native_http_timing_revision
            )

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
            native_http_timing_revision = (
                self.dependencies.native_http_timing_revision()
            )
            now = self.utc_now()
            calendar_key = (now.year, now.month, now.day)
            with self._payload_cache_lock:
                cached = self._payload_cache_value
                cached_version = self._payload_cache_stream_version
                cached_calendar_key = self._payload_cache_calendar_key
                cached_native_lifecycle_revision = (
                    self._payload_cache_native_lifecycle_revision
                )
                cached_native_http_timing_revision = (
                    self._payload_cache_native_http_timing_revision
                )
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
                self._payload_cache_native_lifecycle_revision = (
                    native_lifecycle_revision
                )
                self._payload_cache_native_http_timing_revision = (
                    native_http_timing_revision
                )
            return result

    def _build_payload_uncached(self) -> dict:
        now = self.utc_now()
        month_start, month_end = _current_billing_month_bounds(now)
        current_month_key = _month_key(now)
        current_day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        usage_events = [
            event
            for event in self.dependencies.snapshot_all_usage_events()
            if excel_models.is_excel_model(_usage_event_model_name(event))
        ]
        detailed_usage_events = [
            event
            for event in self.dependencies.snapshot_usage_events()
            if excel_models.is_excel_model(_usage_event_model_name(event))
        ]
        native_lifecycle_revision = self.dependencies.native_lifecycle_revision()
        native_http_timings = self.dependencies.snapshot_native_http_timings()
        if (
            self._usage_accumulator_native_lifecycle_revision
            != native_lifecycle_revision
        ):
            self._usage_accumulator.reset()
            self._usage_accumulator_native_lifecycle_revision = (
                native_lifecycle_revision
            )
        if not self.dependencies.usage_snapshots_are_deduplicated:
            usage_events = deduplicate_usage_events(usage_events)
            detailed_usage_events = deduplicate_usage_events(detailed_usage_events)
        self._usage_accumulator.update(usage_events)
        local_usage = self._usage_accumulator.collect_local_usage()
        month_rows = list(local_usage.get("month_rows") or [])
        current_month_usage = combine_usage_rows(
            [row for row in month_rows if row.get("month_key") == current_month_key],
            month_key=current_month_key,
        )
        all_time_usage = combine_usage_rows(month_rows)
        all_time_usage["months_tracked"] = len(local_usage.get("month_history") or [])
        daily_history = self._usage_accumulator.collect_daily_usage(
            month_start, month_end
        )
        daily_history_by_key = {
            row["day_key"]: row
            for row in daily_history
            if isinstance(row.get("day_key"), str)
        }
        filled_daily_history = []
        day_cursor = month_start
        while day_cursor <= current_day_start:
            day_key = day_cursor.strftime("%Y-%m-%d")
            filled_daily_history.append(
                daily_history_by_key.get(day_key) or empty_day_history_row(day_key)
            )
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
        recent_requests = [
            self._dashboard_request_event(event) for event in sorted_events
        ]

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
                "api_cost_estimate": _build_api_cost_estimate(
                    usage_events, month_start, month_end
                ),
                "daily_history": filled_daily_history,
            },
            "all_time": {
                "proxy_requests": len(usage_events),
                "archived_requests": max(
                    len(usage_events) - len(detailed_usage_events), 0
                ),
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
                    "input_fresh": round(
                        _coerce_float(cost_breakdown.get("input_fresh")), 6
                    ),
                    "cached_input": round(
                        _coerce_float(cost_breakdown.get("cached_input")), 6
                    ),
                    "cache_creation": round(
                        _coerce_float(cost_breakdown.get("cache_creation")), 6
                    ),
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
        notify_dashboard_stream_listeners=dashboard_stream_broker.notify_listeners,
        stream_broker=dashboard_stream_broker,
        **kwargs,
    )
