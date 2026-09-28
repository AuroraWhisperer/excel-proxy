import unittest

import responses_replay_ids


class ResponsesReplayIDTests(unittest.TestCase):
    def test_function_item_id_preserves_short_ids(self):
        self.assertEqual(
            responses_replay_ids.function_item_id("call_123"), "fc_call_123"
        )

    def test_function_item_id_hashes_long_call_ids_within_upstream_limit(self):
        call_id = "call_" + "x" * 200

        item_id = responses_replay_ids.function_item_id(call_id)

        self.assertLessEqual(len(item_id), 64)
        self.assertEqual(item_id, responses_replay_ids.function_item_id(call_id))
        self.assertTrue(item_id.startswith("fc_"))

    def test_repair_missing_replay_ids_rewrites_oversized_function_ids(self):
        call_id = "call_" + "x" * 200
        body = {
            "prompt_cache_key": "excel-thread",
            "input": [
                {
                    "type": "function_call_output",
                    "id": "fc_" + call_id,
                    "call_id": call_id,
                    "output": "done",
                }
            ],
        }

        repaired, trace = responses_replay_ids.repair_missing_replay_ids(body)

        self.assertLessEqual(len(repaired["input"][0]["id"]), 64)
        self.assertEqual(
            repaired["input"][0]["id"], responses_replay_ids.function_item_id(call_id)
        )
        self.assertEqual(trace["input_items"], 1)
        self.assertEqual(trace["repaired_items"], 1)
        self.assertEqual(trace["repaired_by_type"], {"function_call_output": 1})
        self.assertEqual(trace["lineage_key_kind"], "prompt_cache")
        self.assertIsInstance(trace["lineage_key_sha256"], str)
