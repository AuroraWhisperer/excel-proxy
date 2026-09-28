"""Trace writers retain bounded history and isolate background I/O failures."""

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import request_trace_storage as storage


class RequestTraceStorageTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        self.enterContext(patch.object(storage, "_REQUEST_TRACE_EXECUTOR", None))
        self.enterContext(patch.object(storage, "_REQUEST_BODY_DUMP_EXECUTOR", None))
        self.addCleanup(storage.shutdown)

    def test_trace_queue_preserves_order_and_retains_latest_rows(self):
        path = self.directory / "traces" / "requests.jsonl"
        with (
            patch.object(storage, "REQUEST_TRACE_HISTORY_LIMIT", 2),
            patch.object(storage, "REQUEST_TRACE_RETENTION_SLACK", 1),
        ):
            writes = [
                storage.submit_trace_line(
                    str(path),
                    json.dumps({"request_id": index, "detail": "x" * 300}) + "\n",
                )
                for index in range(6)
            ]
            for write in writes:
                write.result(timeout=5)
        rows = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([row["request_id"] for row in rows], [4, 5])

    def test_body_dump_retention_preserves_latest_complete_snapshots(self):
        directory = self.directory / "bodies"
        with (
            patch.object(storage, "REQUEST_TRACE_HISTORY_LIMIT", 2),
            patch.object(storage, "REQUEST_TRACE_RETENTION_SLACK", 0),
        ):
            for index in range(4):
                path = directory / f"request-{index}.json"
                storage.submit_body_dump(
                    str(path), str(directory), {"request_id": index, "text": "测试\n"}
                ).result(timeout=5)
                os.utime(path, (index + 1, index + 1))
        self.assertEqual(
            {path.name for path in directory.iterdir()},
            {"request-2.json", "request-3.json"},
        )
        self.assertEqual(
            json.loads((directory / "request-3.json").read_text(encoding="utf-8")),
            {"request_id": 3, "text": "测试\n"},
        )

    def test_shutdown_drains_both_queues_before_runtime_cleanup(self):
        trace = self.directory / "requests.jsonl"
        dump = self.directory / "request.json"
        storage.submit_trace_line(str(trace), '{"request_id":"last"}\n')
        storage.submit_body_dump(str(dump), str(self.directory), {"request_id": "last"})
        storage.shutdown()
        self.assertEqual(
            json.loads(trace.read_text(encoding="utf-8")), {"request_id": "last"}
        )
        self.assertEqual(
            json.loads(dump.read_text(encoding="utf-8")), {"request_id": "last"}
        )

    def test_failed_writes_do_not_poison_workers_or_raise_to_request_handlers(self):
        blocked = self.directory / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors):
            storage.submit_trace_line(str(blocked / "trace.jsonl"), "{}\n").result(
                timeout=5
            )
            storage.submit_body_dump(
                str(blocked / "dump.json"), str(blocked), {}
            ).result(timeout=5)
        self.assertIn("failed to write request trace log", errors.getvalue())
        self.assertIn("failed to write request body dump", errors.getvalue())
        trace = self.directory / "recovered.jsonl"
        dump = self.directory / "recovered.json"
        storage.submit_trace_line(str(trace), "{}\n").result(timeout=5)
        storage.submit_body_dump(str(dump), str(self.directory), {}).result(timeout=5)
        self.assertEqual(trace.read_text(encoding="utf-8"), "{}\n")
        self.assertEqual(json.loads(dump.read_text(encoding="utf-8")), {})
