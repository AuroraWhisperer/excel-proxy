"""Dashboard rollup and service cache contracts across module extraction."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
import unittest
from unittest.mock import Mock

import dashboard
from usage_aggregation import UsageAccumulator


def event(request_id, **overrides):
    return {
        "request_id": request_id,
        "session_id": "session-a",
        "resolved_model": "gpt-5.6-sol-excel",
        "status_code": 200,
        "started_at": "2026-09-01T12:00:00Z",
        "finished_at": "2026-09-01T12:00:01Z",
        "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10},
        "duration_ms": 1000,
        **overrides,
    }


class UsageAggregationTests(unittest.TestCase):
    def setUp(self):
        self.accumulator = UsageAccumulator()

    def test_append_and_recreated_history_do_not_double_count(self):
        first = event("first")
        second = event("second", finished_at="2026-09-02T12:00:01Z")
        self.accumulator.update([first])
        self.accumulator.update(deepcopy([first]))
        self.accumulator.update([first, second])
        result = self.accumulator.collect_local_usage()
        month = result["month_history"][0]
        self.assertEqual(month["request_count"], 2)
        self.assertEqual(
            (
                month["input_tokens"],
                month["cached_input_tokens"],
                month["total_tokens"],
            ),
            (40, 160, 60),
        )
        self.assertEqual(result["session_count"], 1)
        # Session context is a peak request, while month totals sum fresh usage.
        self.assertEqual(result["recent_sessions"][0]["total_tokens"], 110)
        self.assertEqual(result["recent_sessions"][0]["api_duration_ms"], 2000)

    def test_replaced_or_cleared_history_discards_prior_rollups(self):
        self.accumulator.update([event("old", session_id="old-session")])
        self.accumulator.update([event("new", session_id="new-session")])
        result = self.accumulator.collect_local_usage()
        self.assertEqual(result["month_history"][0]["request_count"], 1)
        self.assertEqual(result["recent_sessions"][0]["session_id"], "new-session")
        self.accumulator.update([])
        self.assertEqual(self.accumulator.collect_local_usage()["month_history"], [])
        self.assertEqual(self.accumulator.collect_local_usage()["session_count"], 0)

    def test_daily_range_uses_utc_and_excludes_end(self):
        self.accumulator.update(
            [
                event("before", finished_at="2026-08-31T23:59:59Z"),
                event("inside", finished_at="2026-09-02T00:30:00+08:00"),
                event("end", finished_at="2026-10-01T00:00:00Z"),
            ]
        )
        rows = self.accumulator.collect_daily_usage(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [(row["day_key"], row["request_count"]) for row in rows],
            [("2026-09-01", 1)],
        )
        self.assertEqual(
            [
                row["month_key"]
                for row in self.accumulator.collect_local_usage()["month_history"]
            ],
            ["2026-10", "2026-09", "2026-08"],
        )

    def test_native_turn_duration_is_counted_once(self):
        self.accumulator.update(
            [
                event(
                    "native-1",
                    native_source="codex_native",
                    native_turn_id="turn",
                    native_turn_duration_ms=3000,
                ),
                event(
                    "native-2",
                    native_source="codex_native",
                    native_turn_id="turn",
                    native_turn_duration_ms=3000,
                ),
            ]
        )
        session = self.accumulator.collect_local_usage()["recent_sessions"][0]
        self.assertEqual(session["request_count"], 2)
        self.assertEqual(session["api_duration_ms"], 3000)

    def test_aggregation_preserves_source_events(self):
        events = [event("first"), event("invalid", started_at="bad", finished_at="bad")]
        original = deepcopy(events)
        self.accumulator.update(events)
        self.assertEqual(
            self.accumulator.collect_local_usage()["month_history"][0]["request_count"],
            1,
        )
        self.assertEqual(events, original)


class DashboardCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.events = [event("first", request_prompt={"input": "private"})]
        self.snapshot = Mock(side_effect=lambda: deepcopy(self.events))
        self.revision = Mock(return_value=0)
        self.timing_revision = Mock(return_value=0)
        self.broker = Mock(current_version=Mock(return_value=0))
        self.service = dashboard.DashboardService(
            dependencies=dashboard.DashboardDependencies(
                snapshot_all_usage_events=self.snapshot,
                snapshot_usage_events=self.snapshot,
                native_lifecycle_revision=self.revision,
                native_http_timing_revision=self.timing_revision,
                usage_snapshots_are_deduplicated=True,
            ),
            utc_now=lambda: self.now,
            stream_broker=self.broker,
        )

    def test_payload_keeps_totals_zero_days_and_prompt_lazy_loading(self):
        self.events.append(event("outside", resolved_model="gpt-5.5"))
        payload = self.service.build_payload()
        self.assertEqual(payload["all_time"]["proxy_requests"], 1)
        self.assertEqual(payload["current_month"]["usage"]["input_tokens"], 20)
        self.assertEqual(
            payload["current_month"]["api_cost_estimate"]["input_tokens"], 100
        )
        self.assertEqual(
            [row["request_count"] for row in payload["current_month"]["daily_history"]],
            [1, 0, 0],
        )
        self.assertNotIn("request_prompt", payload["recent_requests"][0])
        self.assertTrue(payload["recent_requests"][0]["request_prompt_available"])
        self.assertIn("request_prompt", self.events[0])

    def test_unchanged_payload_is_reused_but_force_refresh_rebuilds(self):
        first = self.service.build_payload()
        self.snapshot.reset_mock()
        self.assertIs(self.service.build_payload(), first)
        self.assertIs(
            self.service.build_payload(force_refresh=True, prefer_cached=True), first
        )
        self.snapshot.assert_not_called()
        self.assertEqual(self.service.build_payload(force_refresh=True), first)
        self.assertEqual(self.snapshot.call_count, 2)

    def test_new_usage_and_calendar_rollover_invalidate_cache(self):
        self.service.build_payload()
        self.events.append(event("second"))
        self.broker.current_version.return_value = 1
        payload = self.service.build_payload()
        self.assertEqual(payload["current_month"]["proxy_requests"], 2)
        self.now += timedelta(days=1)
        self.assertEqual(
            len(self.service.build_payload()["current_month"]["daily_history"]), 4
        )
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        payload = self.service.build_payload()
        self.assertEqual(payload["current_month"]["proxy_requests"], 0)
        self.assertEqual(payload["all_time"]["proxy_requests"], 2)

    def test_native_revision_rebuilds_changed_history_and_timing_revision_refreshes(
        self,
    ):
        self.service.build_payload()
        self.events[0]["usage"]["input_tokens"] = 200
        self.revision.return_value = 1
        payload = self.service.build_payload()
        self.assertEqual(payload["current_month"]["usage"]["input_tokens"], 120)
        self.snapshot.reset_mock()
        self.timing_revision.return_value = 1
        self.service.build_payload()
        self.assertEqual(self.snapshot.call_count, 2)

    def test_concurrent_refreshes_share_one_build(self):
        started, release, second_started = Event(), Event(), Event()
        original = self.service._build_payload_uncached

        def slow_build():
            started.set()
            if not release.wait(2):
                raise AssertionError("build was not released")
            return original()

        def second_refresh():
            second_started.set()
            return self.service.build_payload()

        self.service._build_payload_uncached = Mock(side_effect=slow_build)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.build_payload)
            try:
                self.assertTrue(started.wait(2))
                second = pool.submit(second_refresh)
                self.assertTrue(second_started.wait(2))
            finally:
                release.set()
            self.assertIs(first.result(timeout=2), second.result(timeout=2))
        self.service._build_payload_uncached.assert_called_once()
