import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import proxy
import request_diagnostics
import util


class RequestPromptArchiveTests(unittest.TestCase):
    def setUp(self):
        self._old_archive_dir = os.environ.get("GHCP_REQUEST_PROMPT_ARCHIVE_DIR")
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["GHCP_REQUEST_PROMPT_ARCHIVE_DIR"] = self._temp_dir.name

    def tearDown(self):
        if self._old_archive_dir is None:
            os.environ.pop("GHCP_REQUEST_PROMPT_ARCHIVE_DIR", None)
        else:
            os.environ["GHCP_REQUEST_PROMPT_ARCHIVE_DIR"] = self._old_archive_dir
        with proxy._REQUEST_PROMPT_LOCK:
            proxy._REQUEST_PROMPT_ACTIVE_IDS.clear()
        self._temp_dir.cleanup()

    def test_truncated_preview_does_not_destroy_upstream_prefix_fingerprints(self):
        body = {
            "model": "gpt-test",
            "input": [
                {"type": "message", "role": "developer", "content": "Stable " * 2000},
                {
                    "type": "function_call",
                    "call_id": "call",
                    "name": "inspect",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call",
                    "output": "late result",
                },
            ],
        }
        request = SimpleNamespace(
            url=SimpleNamespace(path="/v1/responses"), method="POST", headers={}
        )
        with (
            patch.object(proxy, "_debug_prompt_logging_enabled", return_value=True),
            patch.object(proxy, "_prompt_trace_value", side_effect=lambda value: value),
            patch.object(proxy, "_dump_outbound_request_body"),
            patch.object(proxy, "_append_request_trace") as append,
        ):
            proxy._emit_request_trace_start(
                request_id="synthetic",
                request=request,
                upstream_url="https://example.invalid/responses",
                upstream_path="/responses",
                requested_model="gpt-test",
                resolved_model="gpt-test",
                request_body=body,
                upstream_body=body,
                outbound_headers={},
                prompt_preview={"user": "fixture"},
            )
        row = append.call_args.args[0]
        self.assertTrue(row["upstream_body"]["_truncated"])
        sequence = row["upstream_body_summary"]["input"]["sequence"]
        self.assertEqual(len(sequence), 3)
        self.assertEqual(
            sequence[1]["arguments_hash"], request_diagnostics.trace_hash("{}")
        )
        self.assertEqual(
            sequence[2]["output_hash"], request_diagnostics.trace_hash("late result")
        )

    def test_trace_prompt_and_body_capture_follow_debug_setting(self):
        body = {"model": "gpt-test", "input": 'PRIVATE_PROMPT\n原文和 "引号"'}
        request = SimpleNamespace(
            url=SimpleNamespace(path="/v1/responses"), method="POST", headers={}
        )
        for enabled in (False, True):
            with (
                self.subTest(enabled=enabled),
                patch.object(
                    proxy, "_debug_prompt_logging_enabled", return_value=enabled
                ),
                patch.object(proxy, "_dump_outbound_request_body") as dump,
                patch.object(proxy, "_append_request_trace") as append,
            ):
                context = proxy._emit_request_trace_start(
                    request_id="synthetic",
                    request=request,
                    upstream_url="https://example.invalid/responses",
                    upstream_path="/responses",
                    requested_model="gpt-test",
                    resolved_model="gpt-test",
                    request_body=body,
                    upstream_body=body,
                    outbound_headers={},
                )
            append.assert_called_once()
            row = append.call_args.args[0]
            self.assertEqual(dump.called, enabled)
            if enabled:
                self.assertEqual(row["source_body"], body)
                self.assertEqual(row["upstream_body"], body)
                self.assertEqual(row["request_prompt"]["user"], body["input"])
                self.assertEqual(context["request_prompt"], row["request_prompt"])
                self.assertEqual(
                    context["debug_detail_capture"],
                    {
                        "enabled": True,
                        "reasons": ["debug_prompt_logging"],
                        "context_window": 10,
                        "phase": "current",
                    },
                )
                self.assertEqual(dump.call_args.kwargs["upstream_body"], body)
            else:
                self.assertNotIn("PRIVATE_PROMPT", json.dumps(row))
                self.assertNotIn("request_prompt", context)
                self.assertNotIn("debug_detail_capture", context)

    def test_extract_request_prompt_text_formats_readable_transcript(self):
        body = {
            "instructions": "Be concise.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Explain the latest error."}
                    ],
                },
                {
                    "type": "custom_tool_call",
                    "name": "Read",
                    "input": {"path": "/tmp/example.py"},
                },
            ],
        }

        transcript = util.extract_request_prompt_text(body)

        self.assertIn("INSTRUCTIONS:\nBe concise.", transcript)
        self.assertIn("USER:\nExplain the latest error.", transcript)
        self.assertIn("TOOL CALL READ:\n/tmp/example.py", transcript)

    def test_request_prompt_api_falls_back_to_archived_prompt_text(self):
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Archive this exact prompt."}
                    ],
                }
            ]
        }
        proxy._save_request_prompt_record(
            "request/with unsafe chars", "/v1/responses", body
        )

        response = proxy.asyncio.run(
            proxy.request_prompt_api("request/with unsafe chars")
        )
        payload = json.loads(response.body)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["path"], "/v1/responses")
        self.assertIn("Archive this exact prompt.", payload["prompt_text"])
        self.assertIn("Archive this exact prompt.", payload["request_prompt"]["user"])

    def test_request_prompt_api_resolves_client_request_id_to_archived_prompt(self):
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Resolve this client request."}
                    ],
                }
            ]
        }
        proxy._save_request_prompt_record("server-request-id", "/v1/responses", body)
        original_snapshot = proxy.usage_tracker.snapshot_usage_events
        proxy.usage_tracker.snapshot_usage_events = lambda: [
            {
                "request_id": "server-request-id",
                "client_request_id": "client-request-id",
            }
        ]
        try:
            response = proxy.asyncio.run(proxy.request_prompt_api("client-request-id"))
        finally:
            proxy.usage_tracker.snapshot_usage_events = original_snapshot

        payload = json.loads(response.body)
        self.assertTrue(payload["available"])
        self.assertIn("Resolve this client request.", payload["prompt_text"])

    def test_prune_request_prompt_archive_keeps_only_recent_request_ids(self):
        proxy._save_request_prompt_record("keep-me", "/v1/responses", {"input": "keep"})
        proxy._save_request_prompt_record("drop-me", "/v1/responses", {"input": "drop"})
        unrelated_json = os.path.join(self._temp_dir.name, "unrelated-settings.json")
        with open(unrelated_json, "w", encoding="utf-8") as handle:
            json.dump({"owner": "not request prompt archive"}, handle)

        proxy._prune_request_prompt_archive({"keep-me"})

        self.assertIsNotNone(proxy._load_request_prompt_record("keep-me"))
        self.assertIsNone(proxy._load_request_prompt_record("drop-me"))
        self.assertTrue(os.path.exists(unrelated_json))

    def test_automatic_pruning_waits_for_complete_history(self):
        proxy._save_request_prompt_record(
            "recent-request", "/v1/responses", {"input": "recent"}
        )
        proxy._save_request_prompt_record(
            "older-request", "/v1/responses", {"input": "older"}
        )
        with (
            patch.object(proxy.usage_tracker.state, "history_loaded", False),
            patch.object(
                proxy.usage_tracker,
                "snapshot_usage_events",
                return_value=[{"request_id": "recent-request"}],
            ),
            patch.object(proxy, "_REQUEST_PROMPT_ACTIVE_IDS", set()),
            patch.object(proxy, "_REQUEST_PROMPT_LAST_PRUNED_MONOTONIC", 0),
        ):
            proxy._prune_request_prompt_archive()
            self.assertIsNotNone(proxy._load_request_prompt_record("older-request"))
            proxy.usage_tracker.state.history_loaded = True
            proxy._prune_request_prompt_archive()
            self.assertIsNone(proxy._load_request_prompt_record("older-request"))
            self.assertIsNotNone(proxy._load_request_prompt_record("recent-request"))
