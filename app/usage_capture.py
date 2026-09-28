"""Read usage and first-output markers from chat and Responses SSE events."""

import json

from usage_metrics import normalize_usage_payload
from util import extract_item_text


class SSEUsageCapture:
    def __init__(self, stream_type: str):
        self.stream_type = stream_type
        self.buffer = ""
        self.usage = None
        self.terminal_event_seen = False
        self.completed_event_seen = False
        self.terminal_event_type = None

    def _has_text(self, value) -> bool:
        if not isinstance(value, str):
            return False
        return bool(value)

    def _has_part_output(self, part) -> bool:
        return isinstance(part, dict) and any(
            self._has_text(part.get(key))
            for key in ("text", "input_text", "output_text", "refusal")
        )

    def _has_item_output(self, item) -> bool:
        if not isinstance(item, dict):
            return False
        if self._has_text(extract_item_text(item)):
            return True
        if item.get("type") in {"function_call", "custom_tool_call"}:
            return any(self._has_text(item.get(key)) for key in ("arguments", "input"))
        for key in ("content", "summary"):
            parts = item.get(key)
            if isinstance(parts, list) and any(
                self._has_part_output(part) for part in parts
            ):
                return True
        return False

    def _consume_chat_payload(self, payload: dict) -> bool:
        if isinstance(payload.get("usage"), dict):
            self.usage = normalize_usage_payload(payload["usage"])

        choices = payload.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        delta = first_choice.get("delta") if isinstance(first_choice, dict) else {}
        from responses_protocol import extract_text_from_chat_delta

        return self._has_text(extract_text_from_chat_delta(delta))

    def consume_responses_payload(self, payload: dict) -> bool:
        event_type = str(payload.get("type", "")).strip().lower()
        if event_type in {
            "response.completed",
            "response.failed",
            "response.incomplete",
        }:
            self.terminal_event_seen = True
            self.terminal_event_type = event_type
        if event_type == "response.completed":
            self.completed_event_seen = True
        response = payload.get("response")
        if isinstance(response, dict):
            if isinstance(response.get("usage"), dict):
                self.usage = normalize_usage_payload(response["usage"])
        elif isinstance(payload.get("usage"), dict):
            self.usage = normalize_usage_payload(payload["usage"])

        # Lifecycle events and empty item shells are not generated tokens.
        # Tools and reasoning are output too, even when no prose is emitted.
        if event_type in {
            "response.output_text.delta",
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
            "response.refusal.delta",
            "response.function_call_arguments.delta",
            "response.custom_tool_call_input.delta",
        }:
            return self._has_text(payload.get("delta"))
        if event_type in {
            "response.output_text.done",
            "response.reasoning_text.done",
            "response.reasoning_summary_text.done",
            "response.refusal.done",
            "response.function_call_arguments.done",
            "response.custom_tool_call_input.done",
        }:
            return any(
                self._has_text(payload.get(key))
                for key in ("text", "refusal", "arguments", "input")
            )
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            return self._has_item_output(payload.get("item"))
        if event_type in {
            "response.content_part.added",
            "response.content_part.done",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
        }:
            return self._has_part_output(payload.get("part"))
        if isinstance(response, dict) and isinstance(response.get("output"), list):
            return any(self._has_item_output(item) for item in response["output"])
        return False

    def feed(self, chunk) -> bool:
        if isinstance(chunk, bytes):
            text = chunk.decode("utf-8", errors="replace")
        else:
            text = str(chunk)

        self.buffer += text
        normalized = self.buffer.replace("\r\n", "\n")
        saw_output = False

        while "\n\n" in normalized:
            raw_block, normalized = normalized.split("\n\n", 1)
            from responses_protocol import parse_sse_block

            event_name, data = parse_sse_block(raw_block)
            if data == "[DONE]":
                self.terminal_event_seen = True
                if self.terminal_event_type is None:
                    self.terminal_event_type = "done"
                if self.stream_type != "responses":
                    self.completed_event_seen = True
                continue
            if not data:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if self.stream_type == "chat":
                saw_output = self._consume_chat_payload(payload) or saw_output
            else:
                if event_name:
                    payload["type"] = event_name
                saw_output = self.consume_responses_payload(payload) or saw_output

        self.buffer = normalized
        return saw_output
