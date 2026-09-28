"""Historical usage loading must not repeat optional importer work per row."""

import asyncio
import builtins
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

import dashboard
import proxy
import usage_tracking
import usage_records


class HistoryStartupTests(unittest.TestCase):
    def history_rows(self, count):
        now = usage_tracking.utc_now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return [
            {
                "request_id": f"request-{index}",
                "requested_model": "gpt-6-astra-excel",
                "started_at": (now + timedelta(seconds=index)).isoformat(),
                "finished_at": (now + timedelta(seconds=index)).isoformat(),
                "status_code": 200,
                "usage": {"input_tokens": 100, "output_tokens": 10},
            }
            for index in range(count)
        ]

    def test_initial_load_reads_and_normalizes_only_recent_tail(self):
        rows = self.history_rows(100)
        for row in rows:
            row["request_prompt"] = "中文提示词" * 5000
        raw = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode(
            "utf-8"
        )

        class CountingFile(io.BytesIO):
            bytes_read = 0

            def read(self, size=-1):
                data = super().read(size)
                self.bytes_read += len(data)
                return data

            def readline(self, size=-1):
                data = super().readline(size)
                self.bytes_read += len(data)
                return data

        stream = CountingFile(raw)
        tracker = usage_tracking.UsageTracker()
        with (
            patch.object(usage_tracking, "open", return_value=stream, create=True),
            patch.object(
                usage_tracking,
                "_normalize_recorded_usage_event",
                wraps=usage_tracking._normalize_recorded_usage_event,
            ) as normalize,
            patch.object(tracker, "_compact_if_needed") as compact,
        ):
            tracker.load_history(limit=20)
        self.assertEqual(normalize.call_count, 20)
        self.assertLess(stream.bytes_read, len(raw) // 2)
        self.assertEqual(
            [row["request_id"] for row in tracker.snapshot_usage_events()],
            [row["request_id"] for row in rows[-20:]],
        )
        compact.assert_not_called()

    def test_initial_load_skips_malformed_and_empty_rows(self):
        rows = self.history_rows(25)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            path.write_text(
                "\r\n".join(json.dumps(row) for row in rows)
                + "\r\n\r\nnot-json\nnull\n",
                encoding="utf-8",
            )
            tracker = usage_tracking.UsageTracker(usage_log_file=str(path))
            tracker.load_history(limit=20)
            self.assertEqual(
                [row["request_id"] for row in tracker.snapshot_usage_events()],
                [row["request_id"] for row in rows[-20:]],
            )

    def test_missing_and_short_history_complete_without_duplicates(self):
        for count in (None, 0, 3, 20):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "usage.jsonl"
                rows = self.history_rows(count or 0)
                if count is not None:
                    path.write_text(
                        "".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8",
                    )
                tracker = usage_tracking.UsageTracker(usage_log_file=str(path))
                tracker.load_history(limit=20)
                self.assertEqual(len(tracker.snapshot_usage_events()), count or 0)
                self.assertFalse(tracker.state.history_loaded)
                tracker.load_history()
                self.assertEqual(len(tracker.snapshot_usage_events()), count or 0)
                self.assertTrue(tracker.state.history_loaded)

    def test_background_load_keeps_snapshots_responsive_and_preserves_live_requests(
        self,
    ):
        rows = self.history_rows(100)
        parsing = Event()
        release = Event()
        normalize = usage_tracking._normalize_recorded_usage_event

        def slow_normalize(event, **kwargs):
            if event.get("request_id") == "request-0":
                parsing.set()
                if not release.wait(5):
                    raise AssertionError("background parser was not released")
            return normalize(event, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            archive = usage_tracking.UsageArchiveStore(
                init_storage=Mock(return_value=False)
            )
            tracker = usage_tracking.UsageTracker(
                usage_log_file=str(path), archive_store=archive
            )
            tracker.load_history(limit=20)
            dependencies = dashboard.DashboardDependencies(
                snapshot_all_usage_events=tracker.snapshot_all_usage_events,
                snapshot_usage_events=tracker.snapshot_usage_events,
                native_lifecycle_revision=tracker.native_lifecycle_revision,
            )
            service = dashboard.create_dashboard_service(dependencies)
            self.assertEqual(len(service.build_payload()["recent_requests"]), 20)
            live = dict(
                rows[-1], request_id="live-request", server_request_id="live-chain"
            )
            with (
                ThreadPoolExecutor(max_workers=2) as pool,
                patch.object(usage_tracking, "DETAILED_REQUEST_HISTORY_LIMIT", 20),
                patch.object(
                    usage_tracking,
                    "_normalize_recorded_usage_event",
                    side_effect=slow_normalize,
                ),
            ):
                loading = pool.submit(tracker.load_history)
                try:
                    self.assertTrue(parsing.wait(2))
                    snapshot = pool.submit(tracker.snapshot_usage_events).result(
                        timeout=2
                    )
                    self.assertEqual(len(snapshot), 20)
                    pool.submit(tracker._persist_event, live).result(timeout=2)
                    self.assertEqual(len(tracker.snapshot_usage_events()), 21)
                    archive.init_storage.assert_not_called()
                finally:
                    release.set()
                loading.result(timeout=5)
            archive.init_storage.assert_called_once()
            final = tracker.snapshot_usage_events()
            self.assertEqual(
                [row["request_id"] for row in final],
                [row["request_id"] for row in rows] + ["live-request"],
            )
            self.assertEqual(
                tracker._get_latest_server_request_id(None, None, None), "live-chain"
            )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 101)
            payload = service.build_payload()
            self.assertEqual(payload["current_month"]["proxy_requests"], 101)
            self.assertEqual(len(payload["recent_requests"]), 100)
            self.assertIn(
                "live-request",
                [row["request_id"] for row in payload["recent_requests"]],
            )

    def test_initial_history_load_reuses_archive_keys_but_reload_rebuilds(self):
        archived = {
            "request_id": "codex-native:session:archived",
            "native_source": "codex_native",
            "native_dedupe_key": "archived",
        }
        recent = {
            "request_id": "codex-native:session:recent",
            "native_source": "codex_native",
            "native_dedupe_key": "recent",
        }
        retry = {"request_id": "retry", "requested_model": "gpt-6-astra-excel"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(row) for row in (archived, recent, recent, retry, retry)
                ),
                encoding="utf-8",
            )
            tracker = usage_tracking.UsageTracker(usage_log_file=str(path))
            tracker.replace_history(archived_events=[archived])
            with patch.object(
                tracker,
                "_rebuild_native_usage_event_dedupe_keys_locked",
                wraps=tracker._rebuild_native_usage_event_dedupe_keys_locked,
            ) as rebuild:
                tracker.load_history()
                rebuild.assert_not_called()
                self.assertEqual(
                    [row["request_id"] for row in tracker.snapshot_usage_events()],
                    [recent["request_id"], "retry", "retry"],
                )
                tracker.load_history()
                rebuild.assert_called_once()
                self.assertEqual(
                    [row["request_id"] for row in tracker.snapshot_all_usage_events()],
                    [archived["request_id"], recent["request_id"], "retry", "retry"],
                )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 5)

    def test_history_rows_do_not_retry_missing_native_ingestor_import(self):
        original_import = builtins.__import__
        attempts = []

        def import_module(name, *args, **kwargs):
            if name == "codex_native_ingest":
                attempts.append(name)
                raise ModuleNotFoundError(name)
            return original_import(name, *args, **kwargs)

        event = {
            "request_id": "codex-native:session:turn",
            "native_source": "codex_native",
            "native_model_provider": "openai",
            "native_rollout_path": "old-rollout.jsonl",
            "native_turn_id": "turn",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        with (
            patch.object(usage_records, "_native_turn_metadata_for_rollout", None),
            patch("builtins.__import__", side_effect=import_module),
        ):
            rows = [
                usage_tracking._normalize_recorded_usage_event(
                    event, refresh_native_tiers=False
                )
                for _ in range(3)
            ]
        self.assertEqual(attempts, [])
        self.assertTrue(all(row["request_id"] == event["request_id"] for row in rows))
        self.assertTrue(all(row["usage"]["input_tokens"] == 100 for row in rows))

    def test_optional_metadata_reader_still_backfills_missing_fields(self):
        reader = Mock(
            return_value={
                "native_turn_duration_ms": 250,
                "native_turn_started_at": "2026-09-25T01:00:00Z",
            }
        )
        event = {
            "native_source": "codex_native",
            "native_rollout_path": "old-rollout.jsonl",
            "native_turn_id": "turn",
        }
        with patch.object(usage_records, "_native_turn_metadata_for_rollout", reader):
            row = usage_tracking._normalize_recorded_usage_event(
                event, refresh_native_tiers=False
            )
        reader.assert_called_once_with("old-rollout.jsonl", "turn")
        self.assertEqual(row["native_turn_duration_ms"], 250)
        self.assertNotIn("native_turn_duration_ms", event)

    def test_stored_lifecycle_metadata_is_preserved(self):
        event = {
            "native_source": "codex_native",
            "native_turn_duration_ms": 125,
            "native_turn_started_at": "2026-09-25T01:00:00Z",
        }
        with patch.object(usage_records, "_native_turn_metadata_for_rollout") as reader:
            row = usage_tracking._normalize_recorded_usage_event(
                event, refresh_native_tiers=False
            )
        reader.assert_not_called()
        self.assertEqual(row["native_turn_duration_ms"], 125)

    def test_backfill_does_not_overwrite_stored_fields(self):
        reader = Mock(
            return_value={
                "native_turn_duration_ms": 250,
                "native_turn_started_at": "2026-09-25T01:00:00Z",
            }
        )
        event = {
            "native_source": "codex_native",
            "native_turn_duration_ms": 125,
        }
        with patch.object(usage_records, "_native_turn_metadata_for_rollout", reader):
            row = usage_tracking._normalize_recorded_usage_event(
                event, refresh_native_tiers=False
            )
        self.assertEqual(row["native_turn_duration_ms"], 125)
        self.assertEqual(row["native_turn_started_at"], "2026-09-25T01:00:00Z")
        self.assertNotIn("native_turn_started_at", event)

    def test_reader_failure_keeps_history_readable(self):
        event = {
            "request_id": "codex-native:session:turn",
            "native_source": "codex_native",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        with patch.object(
            usage_records,
            "_native_turn_metadata_for_rollout",
            side_effect=OSError("unavailable"),
        ):
            row = usage_tracking._normalize_recorded_usage_event(
                event, refresh_native_tiers=False
            )
        self.assertEqual(row["request_id"], event["request_id"])
        self.assertEqual(row["usage"]["input_tokens"], 100)

    def test_lifecycle_refresh_does_not_retry_missing_import(self):
        tracker = usage_tracking.UsageTracker()
        tracker.state.recent_usage_events.append(
            {
                "native_source": "codex_native",
                "native_rollout_path": "old-rollout.jsonl",
                "native_turn_id": "turn",
            }
        )
        with (
            patch.object(usage_records, "_native_turn_metadata_for_rollout", None),
            patch(
                "builtins.__import__", side_effect=AssertionError("unexpected import")
            ),
        ):
            revisions = [tracker.native_lifecycle_revision() for _ in range(3)]
        self.assertEqual(revisions, [0, 0, 0])
        self.assertNotIn(
            "native_turn_duration_ms", tracker.state.recent_usage_events[0]
        )

    def test_lifecycle_refresh_backfills_archived_and_recent_rows_once(self):
        tracker = usage_tracking.UsageTracker()
        archived = {
            "native_source": "codex_native",
            "native_rollout_path": "old-rollout.jsonl",
            "native_turn_id": "archived-turn",
        }
        recent = dict(archived, native_turn_id="recent-turn")
        tracker.state.archived_usage_events.append(archived)
        tracker.state.recent_usage_events.append(recent)
        reader = Mock(return_value={"native_turn_duration_ms": 250})
        with (
            patch.object(usage_records, "_native_turn_metadata_for_rollout", reader),
            patch.object(
                usage_tracking, "_usage_event_source", return_value="codex_native"
            ),
        ):
            revisions = [tracker.native_lifecycle_revision() for _ in range(3)]
        self.assertEqual(revisions, [1, 1, 1])
        self.assertEqual(reader.call_count, 2)
        reader.assert_any_call("old-rollout.jsonl", "archived-turn")
        reader.assert_any_call("old-rollout.jsonl", "recent-turn")
        self.assertEqual(archived["native_turn_duration_ms"], 250)
        self.assertEqual(recent["native_turn_duration_ms"], 250)


class HistoryStartupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_does_not_wait_or_schedule_duplicate_history_loads(self):
        started = Event()
        release = Event()
        calls = []

        def load_archive():
            calls.append("archive")
            started.set()
            if not release.wait(5):
                raise AssertionError("archive loader was not released")

        with (
            patch.object(proxy, "_usage_history_task", None),
            patch.object(
                proxy.usage_tracker, "load_archived_history", side_effect=load_archive
            ),
            patch.object(
                proxy.usage_tracker,
                "load_history",
                side_effect=lambda: calls.append("history"),
            ) as load,
            patch.object(
                proxy.dashboard_service,
                "notify_dashboard_stream_listeners",
                side_effect=lambda: calls.append("notify"),
            ) as notify,
        ):
            await proxy._app_startup_load_usage_history()
            task = proxy._usage_history_task
            shutdown = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertFalse(task.done())
                load.assert_not_called()
                notify.assert_not_called()
                await proxy._app_startup_load_usage_history()
                self.assertIs(proxy._usage_history_task, task)
                shutdown = asyncio.create_task(
                    proxy._app_shutdown_wait_for_usage_history()
                )
                await asyncio.sleep(0)
                self.assertFalse(shutdown.done())
            finally:
                release.set()
                await asyncio.wait_for(task, 5)
                if shutdown is not None:
                    await asyncio.wait_for(shutdown, 5)
            self.assertEqual(calls, ["archive", "history", "notify"])

    async def test_background_failure_is_reported_without_failing_shutdown(self):
        with (
            patch.object(proxy, "_usage_history_task", None),
            patch.object(
                proxy.usage_tracker,
                "load_archived_history",
                side_effect=RuntimeError("unreadable history"),
            ),
            patch.object(
                proxy.dashboard_service, "notify_dashboard_stream_listeners"
            ) as notify,
            patch.object(proxy.sys, "stderr", new_callable=io.StringIO) as errors,
        ):
            await proxy._app_startup_load_usage_history()
            await proxy._app_shutdown_wait_for_usage_history()
            self.assertIn(
                "failed to restore usage history: unreadable history", errors.getvalue()
            )
            notify.assert_called_once()
