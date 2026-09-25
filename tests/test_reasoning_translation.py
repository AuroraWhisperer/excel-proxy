import asyncio
import json
import unittest

import format_translation
import proxy


class ReasoningTranslationTests(unittest.IsolatedAsyncioTestCase):
    def test_ensure_codex_reasoning_header(self):
        self.assertEqual(
            format_translation.ensure_codex_reasoning_header("Analyzing codebase"),
            "**Thinking**\n\nAnalyzing codebase",
        )
        self.assertEqual(
            format_translation.ensure_codex_reasoning_header("**Thinking**\n\nAlready bold"),
            "**Thinking**\n\nAlready bold",
        )
        self.assertEqual(
            format_translation.ensure_codex_reasoning_header("# Header\n\nSome text"),
            "# Header\n\nSome text",
        )
        self.assertEqual(
            format_translation.ensure_codex_reasoning_header(""),
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
        format_translation.normalize_reasoning_item_for_client(item)
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
        format_translation.normalize_reasoning_item_for_client(encrypted_item)
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

        first = format_translation.sanitize_input(
            [tool_output("turn-one", "pwd")],
            native_responses_passthrough=True,
        )
        second = format_translation.sanitize_input(
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



    async def test_responses_reasoning_stream_transform(self):
        raw_events = [
            format_translation.sse_encode(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "reasoning", "id": "rs_upstream", "summary": [], "content": []},
                },
            ),
            format_translation.sse_encode(
                "response.reasoning_text.delta",
                {
                    "type": "response.reasoning_text.delta",
                    "item_id": "rs_upstream",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Thinking token 1. ",
                },
            ),
            format_translation.sse_encode(
                "response.reasoning_text.delta",
                {
                    "type": "response.reasoning_text.delta",
                    "item_id": "rs_upstream",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Thinking token 2.",
                },
            ),
            format_translation.sse_encode(
                "response.reasoning_text.done",
                {
                    "type": "response.reasoning_text.done",
                    "item_id": "rs_upstream",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "Thinking token 1. Thinking token 2.",
                },
            ),
            format_translation.sse_encode(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "reasoning",
                        "id": "rs_upstream",
                        "summary": [],
                        "content": [{"type": "reasoning_text", "text": "Thinking token 1. Thinking token 2."}],
                    },
                },
            ),
            b"data: [DONE]\n\n",
        ]

        async def byte_generator():
            for ev in raw_events:
                yield ev

        transform = proxy._responses_reasoning_stream_transform()
        transformed_events = []
        async for chunk in transform(byte_generator()):
            for block in chunk.decode().strip().split("\n\n"):
                if not block.strip() or block == "data: [DONE]":
                    continue
                lines = block.splitlines()
                ev_name = lines[0].replace("event: ", "").strip()
                data = json.loads(lines[1].replace("data: ", ""))
                transformed_events.append((ev_name, data))

        event_names = [name for name, _ in transformed_events]
        self.assertIn("response.reasoning_summary_part.added", event_names)
        self.assertIn("response.reasoning_summary_text.delta", event_names)
        self.assertIn("response.reasoning_summary_text.done", event_names)

        # Check that header delta was sent
        summary_deltas = [
            d["delta"] for name, d in transformed_events if name == "response.reasoning_summary_text.delta"
        ]
        self.assertEqual(summary_deltas[0], "**Thinking**\n\n")
        self.assertEqual(summary_deltas[1], "Thinking token 1. ")

        # Check output_item.done has populated summary
        done_item = next(d["item"] for name, d in transformed_events if name == "response.output_item.done")
        self.assertEqual(
            done_item["summary"],
            [{"type": "summary_text", "text": "**Thinking**\n\nThinking token 1. Thinking token 2."}],
        )


if __name__ == "__main__":
    unittest.main()
