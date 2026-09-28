import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import excel_upstream
import excel_tool_history
from test_excel_tool_compatibility import FUNCTION_TOOL, transport


def native(index, command="echo test"):
    item = transport({"name": "exec_command", "arguments": {"cmd": command}})
    item.update(id=f"fc_limit_{index}", call_id=f"call_limit_{index}")
    return item


def size(item):
    return len(json.dumps(item, ensure_ascii=False).encode("utf-8"))


class ReplayLimitsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="replay-limits-")
        self.addCleanup(directory.cleanup)
        self.database = str(Path(directory.name) / "calls.sqlite3")
        self.enterContext(
            patch.object(excel_tool_history, "_NATIVE_CALL_DB", self.database)
        )
        self.enterContext(
            patch.object(
                excel_tool_history,
                "_native_call_cache",
                excel_tool_history.OrderedDict(),
            )
        )

    def entries(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            return {
                key: json.loads(item)
                for key, item in db.execute("SELECT call_id, item FROM native_calls")
            }

    def test_old_database_migrates_without_losing_replay(self):
        first, second = native(1), native(2)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "CREATE TABLE native_calls (call_id TEXT PRIMARY KEY, item TEXT NOT NULL, last_used REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO native_calls VALUES (?, ?, ?)",
                (
                    first["call_id"],
                    json.dumps(first, ensure_ascii=False),
                    excel_tool_history.time.time(),
                ),
            )
        self.assertTrue(excel_upstream._remember_native_call(second))
        self.assertEqual(
            self.entries(), {item["call_id"]: item for item in (first, second)}
        )
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertTrue(
                all(
                    length == len(text.encode("utf-8"))
                    for text, length in db.execute(
                        "SELECT item, byte_size FROM native_calls"
                    )
                )
            )

    def test_count_and_byte_eviction_preserves_new_batch(self):
        for limit in ("_NATIVE_CALL_DB_LIMIT", "_NATIVE_CALL_DB_BYTES"):
            with self.subTest(limit=limit):
                first, second, third = native(1), native(2), native(3)
                budget = 2 if limit.endswith("LIMIT") else size(second) + size(third)
                with patch.object(excel_tool_history, limit, budget):
                    self.assertTrue(excel_upstream._remember_native_call(first))
                    self.assertTrue(
                        excel_upstream._remember_native_calls([second, third])
                    )
                    self.assertEqual(
                        set(self.entries()), {second["call_id"], third["call_id"]}
                    )
                    self.assertNotIn(
                        first["call_id"], excel_tool_history._native_call_cache
                    )

    def test_memory_budget_does_not_remove_persisted_calls(self):
        first, second = native(1, "中文" * 100), native(2, "中文" * 100)
        for item in (first, second):
            envelope = json.loads(json.loads(item["arguments"])["code"])
            item["arguments"] = json.dumps(
                {"code": json.dumps(envelope, ensure_ascii=False)}, ensure_ascii=False
            )
            self.assertGreater(size(item), len(json.dumps(item, ensure_ascii=False)))
        with patch.object(excel_tool_history, "_NATIVE_CALL_CACHE_BYTES", size(second)):
            self.assertTrue(excel_upstream._remember_native_calls([first, second]))
            self.assertEqual(
                list(excel_tool_history._native_call_cache), [second["call_id"]]
            )
            result = excel_upstream._remembered_native_calls(
                [first["call_id"], second["call_id"]]
            )
            self.assertEqual(
                result, {item["call_id"]: item for item in (first, second)}
            )
            result[second["call_id"]]["arguments"] = "changed outside cache"
            self.assertEqual(
                excel_upstream._remembered_native_calls([second["call_id"]])[
                    second["call_id"]
                ],
                second,
            )
            excel_tool_history._native_call_cache.clear()
            self.assertEqual(
                excel_upstream._remembered_native_calls([first["call_id"]])[
                    first["call_id"]
                ],
                first,
            )

    def test_oversize_entry_or_batch_is_rejected_before_any_write(self):
        first, second = native(1), native(2, "x" * 200)
        for limit, budget in (
            ("_NATIVE_CALL_ENTRY_BYTES", size(second) - 1),
            ("_NATIVE_CALL_DB_BYTES", size(first) + size(second) - 1),
            ("_NATIVE_CALL_DB_LIMIT", 1),
        ):
            with (
                self.subTest(limit=limit),
                patch.object(excel_tool_history, limit, budget),
            ):
                diagnostic = {}
                self.assertIsNone(
                    excel_upstream.extract_native_client_tool_calls(
                        {"output": [first, second]},
                        {"tools": [FUNCTION_TOOL]},
                        diagnostics=diagnostic,
                    )
                )
                self.assertEqual(diagnostic["reason"], "replay_cache_capacity")
                self.assertFalse(Path(self.database).exists())
                self.assertFalse(excel_tool_history._native_call_cache)

    def test_expired_entries_cannot_be_revived_by_reads(self):
        item = native(1)
        with patch.object(excel_tool_history.time, "time", return_value=100):
            self.assertTrue(excel_upstream._remember_native_call(item))
        later = 101 + excel_tool_history._NATIVE_CALL_RETENTION_SECONDS
        with patch.object(excel_tool_history.time, "time", return_value=later):
            self.assertEqual(
                excel_upstream._remembered_native_calls([item["call_id"]]), {}
            )
            excel_tool_history._native_call_cache.clear()
            self.assertEqual(
                excel_upstream._remembered_native_calls([item["call_id"]]), {}
            )
            self.assertTrue(excel_upstream._remember_native_call(native(2)))
            self.assertNotIn(item["call_id"], self.entries())

    def test_replacement_and_failed_transaction_preserve_consistency(self):
        first = native(1)
        self.assertTrue(excel_upstream._remember_native_call(first))
        replacement = native(1, "echo changed")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_second BEFORE INSERT ON native_calls WHEN NEW.call_id='call_limit_2' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
            )
        before = copy.deepcopy(excel_tool_history._native_call_cache)
        with self.assertRaises(sqlite3.IntegrityError):
            excel_upstream._remember_native_calls([replacement, native(2)])
        self.assertEqual(self.entries(), {first["call_id"]: first})
        self.assertEqual(excel_tool_history._native_call_cache, before)
        self.assertTrue(excel_upstream._remember_native_call(replacement))
        self.assertEqual(self.entries(), {replacement["call_id"]: replacement})
