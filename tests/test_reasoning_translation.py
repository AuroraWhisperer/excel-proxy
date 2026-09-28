import unittest

import responses_protocol


class ReasoningTranslationTests(unittest.TestCase):
    def test_ensure_codex_reasoning_header(self):
        self.assertEqual(
            responses_protocol.ensure_codex_reasoning_header("Analyzing codebase"),
            "**Thinking**\n\nAnalyzing codebase",
        )
        self.assertEqual(
            responses_protocol.ensure_codex_reasoning_header(
                "**Thinking**\n\nAlready bold"
            ),
            "**Thinking**\n\nAlready bold",
        )
        self.assertEqual(
            responses_protocol.ensure_codex_reasoning_header("# Header\n\nSome text"),
            "# Header\n\nSome text",
        )
        self.assertEqual(
            responses_protocol.ensure_codex_reasoning_header(""),
            "",
        )

    def test_normalize_reasoning_item_for_client(self):
        # Item with only content (e.g. from standard OpenAI / Copilot)
        item = {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "Step 1 reasoning"}],
        }
        responses_protocol.normalize_reasoning_item_for_client(item)
        self.assertEqual(
            item["summary"],
            [{"type": "summary_text", "text": "**Thinking**\n\nStep 1 reasoning"}],
        )
        self.assertEqual(
            item["content"],
            [{"type": "reasoning_text", "text": "**Thinking**\n\nStep 1 reasoning"}],
        )

        # Item with encrypted_content but empty summary (e.g. gpt-excel)
        encrypted_item = {
            "type": "reasoning",
            "id": "rs_2",
            "summary": [],
            "encrypted_content": "gAAAAABk...",
        }
        responses_protocol.normalize_reasoning_item_for_client(encrypted_item)
        self.assertEqual(encrypted_item["summary"], [])
        self.assertEqual(encrypted_item["encrypted_content"], "gAAAAABk...")
        self.assertNotIn("content", encrypted_item)

    def test_native_responses_sanitization_keeps_replayed_tool_output_stable(self):
        def tool_output(turn_id, executed_command):
            return {
                "type": "function_call_output",
                "id": "fco_1",
                "call_id": "call_1",
                "output": "done",
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn_id,
                    "executed_tool_calls": [{"arguments": {"cmd": executed_command}}],
                },
            }

        first = responses_protocol.sanitize_input(
            [tool_output("turn-one", "pwd")],
            native_responses_passthrough=True,
        )
        second = responses_protocol.sanitize_input(
            [
                tool_output("turn-two", "git status"),
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "continue"}],
                },
            ],
            native_responses_passthrough=True,
        )

        self.assertEqual(second[: len(first)], first)
        self.assertNotIn("internal_chat_message_metadata_passthrough", first[0])


if __name__ == "__main__":
    unittest.main()
