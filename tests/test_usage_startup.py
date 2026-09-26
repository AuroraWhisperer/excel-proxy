"""Historical usage loading must not repeat optional importer work per row."""

import builtins
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import usage_tracking


class HistoryStartupTests(unittest.TestCase):
    def test_initial_history_load_reuses_archive_keys_but_reload_rebuilds(self):
        archived = {'request_id': 'codex-native:session:archived', 'native_source': 'codex_native', 'native_dedupe_key': 'archived'}
        recent = {'request_id': 'codex-native:session:recent', 'native_source': 'codex_native', 'native_dedupe_key': 'recent'}
        retry = {'request_id': 'retry', 'requested_model': 'gpt-6-astra-excel'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'usage.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in (archived, recent, recent, retry, retry)), encoding='utf-8')
            tracker = usage_tracking.UsageTracker(usage_log_file=str(path))
            tracker.replace_history(archived_events=[archived])
            with patch.object(tracker, '_rebuild_native_usage_event_dedupe_keys_locked', wraps=tracker._rebuild_native_usage_event_dedupe_keys_locked) as rebuild:
                tracker.load_history()
                rebuild.assert_not_called()
                self.assertEqual([row['request_id'] for row in tracker.snapshot_usage_events()], [recent['request_id'], 'retry', 'retry'])
                tracker.load_history()
                rebuild.assert_called_once()
                self.assertEqual([row['request_id'] for row in tracker.snapshot_all_usage_events()], [archived['request_id'], recent['request_id'], 'retry', 'retry'])
            self.assertEqual(len(path.read_text(encoding='utf-8').splitlines()), 5)

    def test_history_rows_do_not_retry_missing_native_ingestor_import(self):
        original_import = builtins.__import__
        attempts = []

        def import_module(name, *args, **kwargs):
            if name == 'codex_native_ingest':
                attempts.append(name)
                raise ModuleNotFoundError(name)
            return original_import(name, *args, **kwargs)

        event = {
            'request_id': 'codex-native:session:turn',
            'native_source': 'codex_native',
            'native_model_provider': 'openai',
            'native_rollout_path': 'old-rollout.jsonl',
            'native_turn_id': 'turn',
            'usage': {'input_tokens': 100, 'output_tokens': 10},
        }
        with patch.object(usage_tracking, '_native_turn_metadata_for_rollout', None), \
             patch('builtins.__import__', side_effect=import_module):
            rows = [
                usage_tracking._normalize_recorded_usage_event(event, refresh_native_tiers=False)
                for _ in range(3)
            ]
        self.assertEqual(attempts, [])
        self.assertTrue(all(row['request_id'] == event['request_id'] for row in rows))
        self.assertTrue(all(row['usage']['input_tokens'] == 100 for row in rows))

    def test_optional_metadata_reader_still_backfills_missing_fields(self):
        reader = Mock(return_value={'native_turn_duration_ms': 250, 'native_turn_started_at': '2026-09-25T01:00:00Z'})
        event = {
            'native_source': 'codex_native',
            'native_rollout_path': 'old-rollout.jsonl',
            'native_turn_id': 'turn',
        }
        with patch.object(usage_tracking, '_native_turn_metadata_for_rollout', reader):
            row = usage_tracking._normalize_recorded_usage_event(event, refresh_native_tiers=False)
        reader.assert_called_once_with('old-rollout.jsonl', 'turn')
        self.assertEqual(row['native_turn_duration_ms'], 250)
        self.assertNotIn('native_turn_duration_ms', event)

    def test_stored_lifecycle_metadata_is_preserved(self):
        event = {
            'native_source': 'codex_native',
            'native_turn_duration_ms': 125,
            'native_turn_started_at': '2026-09-25T01:00:00Z',
        }
        with patch.object(usage_tracking, '_native_turn_metadata_for_rollout') as reader:
            row = usage_tracking._normalize_recorded_usage_event(event, refresh_native_tiers=False)
        reader.assert_not_called()
        self.assertEqual(row['native_turn_duration_ms'], 125)

    def test_backfill_does_not_overwrite_stored_fields(self):
        reader = Mock(return_value={
            'native_turn_duration_ms': 250,
            'native_turn_started_at': '2026-09-25T01:00:00Z',
        })
        event = {
            'native_source': 'codex_native',
            'native_turn_duration_ms': 125,
        }
        with patch.object(usage_tracking, '_native_turn_metadata_for_rollout', reader):
            row = usage_tracking._normalize_recorded_usage_event(event, refresh_native_tiers=False)
        self.assertEqual(row['native_turn_duration_ms'], 125)
        self.assertEqual(row['native_turn_started_at'], '2026-09-25T01:00:00Z')
        self.assertNotIn('native_turn_started_at', event)

    def test_reader_failure_keeps_history_readable(self):
        event = {
            'request_id': 'codex-native:session:turn',
            'native_source': 'codex_native',
            'usage': {'input_tokens': 100, 'output_tokens': 10},
        }
        with patch.object(usage_tracking, '_native_turn_metadata_for_rollout', side_effect=OSError('unavailable')):
            row = usage_tracking._normalize_recorded_usage_event(event, refresh_native_tiers=False)
        self.assertEqual(row['request_id'], event['request_id'])
        self.assertEqual(row['usage']['input_tokens'], 100)

    def test_lifecycle_refresh_does_not_retry_missing_import(self):
        tracker = usage_tracking.UsageTracker()
        tracker.state.recent_usage_events.append({
            'native_source': 'codex_native',
            'native_rollout_path': 'old-rollout.jsonl',
            'native_turn_id': 'turn',
        })
        with patch.object(usage_tracking, '_native_turn_metadata_for_rollout', None), \
             patch('builtins.__import__', side_effect=AssertionError('unexpected import')):
            revisions = [tracker.native_lifecycle_revision() for _ in range(3)]
        self.assertEqual(revisions, [0, 0, 0])
        self.assertNotIn('native_turn_duration_ms', tracker.state.recent_usage_events[0])

    def test_lifecycle_refresh_backfills_archived_and_recent_rows_once(self):
        tracker = usage_tracking.UsageTracker()
        archived = {
            'native_source': 'codex_native',
            'native_rollout_path': 'old-rollout.jsonl',
            'native_turn_id': 'archived-turn',
        }
        recent = dict(archived, native_turn_id='recent-turn')
        tracker.state.archived_usage_events.append(archived)
        tracker.state.recent_usage_events.append(recent)
        reader = Mock(return_value={'native_turn_duration_ms': 250})
        with patch.object(usage_tracking, '_native_turn_metadata_for_rollout', reader), \
             patch.object(usage_tracking, '_usage_event_source', return_value='codex_native'):
            revisions = [tracker.native_lifecycle_revision() for _ in range(3)]
        self.assertEqual(revisions, [1, 1, 1])
        self.assertEqual(reader.call_count, 2)
        reader.assert_any_call('old-rollout.jsonl', 'archived-turn')
        reader.assert_any_call('old-rollout.jsonl', 'recent-turn')
        self.assertEqual(archived['native_turn_duration_ms'], 250)
        self.assertEqual(recent['native_turn_duration_ms'], 250)
