"""SQLite ownership changes must preserve archive commit/rollback behavior."""

from contextlib import closing
import json
from pathlib import Path
import tempfile
from threading import Lock
import unittest
from unittest.mock import patch

import usage_storage
import usage_tracking
from test_module_boundaries import imported_modules


def event(index):
    return {
        "request_id": f"storage-{index}",
        "requested_model": "gpt-6-astra-excel",
        "started_at": f"2026-09-01T00:00:0{index}+00:00",
        "finished_at": f"2026-09-01T00:00:0{index}+00:00",
        "status_code": 200,
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


class UsageStorageBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="usage-storage-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.owner = usage_storage
        self.enterContext(
            patch.object(
                self.owner, "SQLITE_CACHE_FILE", str(self.path / "usage.sqlite3")
            )
        )
        self.enterContext(patch.object(self.owner, "_sqlite_cache_enabled", True))
        self.enterContext(patch.object(self.owner, "_sqlite_cache_error", None))
        self.enterContext(patch.object(self.owner, "_sqlite_cache_lock", Lock()))
        self.store = usage_storage.UsageCacheStore()
        self.assertTrue(self.store.initialize())
        self.tracker = usage_tracking.UsageTracker(
            archive_store=self.store.usage_archive_store(),
            usage_log_file=str(self.path / "usage.jsonl"),
        )
        self.enterContext(
            patch.object(usage_tracking, "DETAILED_REQUEST_HISTORY_LIMIT", 1)
        )

    def archive_payloads(self):
        with closing(self.store.connect()) as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM archived_usage_events ORDER BY recorded_at"
                )
            ]

    def test_reinitialization_keeps_archive_and_existing_legacy_tables(self):
        payload = event(1)
        self.store.usage_archive_store().insert_rows(
            [
                ("storage-1", payload["finished_at"], json.dumps(payload)),
            ]
        )
        with closing(self.store.connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_entries (cache_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO cache_entries VALUES (?, ?, ?)",
                ("old-cache", '{"count":7}', payload["finished_at"]),
            )
            connection.commit()
        reopened = usage_storage.UsageCacheStore()
        self.assertTrue(reopened.initialize())
        self.assertEqual(self.archive_payloads(), [payload])
        with closing(reopened.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM cache_entries WHERE cache_key = ?",
                ("old-cache",),
            ).fetchone()
        self.assertEqual(json.loads(row[0]), {"count": 7})

    def test_compaction_preserves_archive_and_recent_history(self):
        self.tracker.replace_history(recent_events=[event(1), event(2)])
        self.tracker.compact_history_if_needed()
        self.assertEqual(
            [row["request_id"] for row in self.archive_payloads()], ["storage-1"]
        )
        self.assertEqual(
            [row["request_id"] for row in self.tracker.snapshot_usage_events()],
            ["storage-2"],
        )
        restored = usage_tracking.UsageTracker(
            archive_store=self.store.usage_archive_store()
        )
        restored.load_archived_history()
        self.assertEqual(
            [row["request_id"] for row in restored.snapshot_archived_usage_events()],
            ["storage-1"],
        )

    def test_log_rewrite_failure_rolls_back_archive_and_keeps_memory(self):
        self.tracker.replace_history(recent_events=[event(1), event(2)])
        with patch.object(
            self.tracker, "_rewrite_usage_log", side_effect=OSError("synthetic failure")
        ):
            self.tracker.compact_history_if_needed()
        self.assertEqual(self.archive_payloads(), [])
        self.assertEqual(
            [row["request_id"] for row in self.tracker.snapshot_usage_events()],
            ["storage-1", "storage-2"],
        )

    def test_sql_write_failure_does_not_drop_recent_events(self):
        with closing(self.store.connect()) as connection:
            connection.execute(
                "CREATE TRIGGER reject_archive BEFORE INSERT ON archived_usage_events BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
            connection.commit()
        self.tracker.replace_history(recent_events=[event(1), event(2)])
        self.tracker.compact_history_if_needed()
        self.assertEqual(self.archive_payloads(), [])
        self.assertEqual(len(self.tracker.snapshot_usage_events()), 2)

    def test_read_failure_preserves_previously_loaded_archive(self):
        self.tracker.replace_history(archived_events=[event(1)])
        with patch.object(
            self.tracker.archive_store,
            "connect",
            side_effect=OSError("synthetic failure"),
        ):
            self.tracker.load_archived_history()
        self.assertEqual(
            [
                row["request_id"]
                for row in self.tracker.snapshot_archived_usage_events()
            ],
            ["storage-1"],
        )


class UsageStorageBoundaryTests(unittest.TestCase):
    def test_storage_owns_archive_without_importing_consumers(self):
        self.assertIs(usage_tracking.UsageArchiveStore, usage_storage.UsageArchiveStore)
        self.assertTrue(
            {"dashboard", "usage_tracking", "proxy"}.isdisjoint(
                imported_modules("usage_storage")
            )
        )
