"""Reference-history parity without cross-request replay state or real uploads."""

import copy
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import excel_images
import excel_input
import excel_tool_history
import excel_tool_transport
from responses_input import encode_fake_compaction


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()[:32]


def call(**updates):
    return {
        "type": "function_call",
        "call_id": "call_history",
        "name": "exec_command",
        "arguments": '{"cmd":"echo history"}',
        **updates,
    }


def tool_result(**updates):
    return {
        "type": "function_call_output",
        "call_id": "call_history",
        "output": "done",
        **updates,
    }


class BpsHistoryAlignmentTests(unittest.TestCase):
    def test_input_requires_string_or_object_array(self):
        for value in (None, 7, {}, True, [None], ["message"], [7], [[]]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                excel_input.translate_input_items(value)
        self.assertEqual(excel_input.translate_input_items([]), [])
        self.assertEqual(
            excel_input.translate_input_items("hello")[0]["content"],
            [{"type": "input_text", "text": "hello"}],
        )

    def test_orphan_results_are_rejected_even_if_cache_has_a_call(self):
        with patch.object(
            excel_tool_history, "remembered_native_calls", side_effect=AssertionError("cache read")
        ):
            for kind in ("function_call_output", "custom_tool_call_output"):
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    excel_input.translate_input_items([tool_result(type=kind)])

    def test_history_wrapper_and_ids_match_reference(self):
        native, result = excel_input.translate_input_items([call(), tool_result()])
        self.assertEqual(native["id"], "fc_" + fingerprint("call_history"))
        self.assertEqual(native["call_id"], "call_history")
        self.assertEqual(native["name"], "run_officejs")
        self.assertEqual(native["status"], "completed")
        arguments = json.loads(native["arguments"])
        self.assertEqual(arguments, {
            "code": '{"arguments":{"cmd":"echo history"},"name":"exec_command"}',
            "summary": "Replay a previously requested client tool",
            "extended_summary": "The supplied client history contains this tool call; consume its recorded result without repeating it.",
            "destructive": False,
            "references": [],
        })
        self.assertEqual(result, tool_result(id="fc_call_history"))

    def test_valid_native_item_ids_are_preserved_and_custom_ids_are_hashed(self):
        native, result = excel_input.translate_input_items([
            call(id="fc_original"), tool_result(id="ctco_result")
        ])
        self.assertEqual(native["id"], "fc_original")
        self.assertEqual(result["id"], "fc_" + fingerprint("ctco_result"))
        long_id = "fc_" + "x" * 65
        result = excel_input.translate_input_items([
            call(), tool_result(id=long_id)
        ])[-1]
        self.assertEqual(result["id"], "fc_" + fingerprint(long_id))

    def test_complete_history_does_not_depend_on_cache_or_catalog(self):
        history = [call(), tool_result(output=" ")]
        before = copy.deepcopy(history)
        with (
            patch.object(excel_tool_history, "remembered_native_calls", side_effect=AssertionError("cache read")),
            patch.object(excel_tool_history, "remember_native_calls", side_effect=AssertionError("cache write")),
        ):
            expected = excel_input.translate_input_items(history)
            actual = excel_input.translate_input_items(history, {"other_tool": "custom"})
        self.assertEqual(actual, expected)
        self.assertEqual(actual[-1]["output"], " ")
        self.assertEqual(history, before)

    def test_invalid_call_fields_are_rejected(self):
        changes = (
            {"name": ""}, {"name": " tool"}, {"name": 8},
            {"call_id": ""}, {"call_id": "call "}, {"call_id": 3},
            {"namespace": None}, {"namespace": "tools "},
            {"arguments": "broken"}, {"arguments": "{} {}"},
            {"arguments": "[]"}, {"arguments": {"value": float("nan")}},
            {"arguments": '{"value":NaN}'},
            {"type": "custom_tool_call", "input": {}},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                excel_input.translate_input_items([call(**change)])

    def test_namespaced_custom_history_uses_reference_json_envelope(self):
        native, result = excel_input.translate_input_items([
            call(type="custom_tool_call", name="apply_patch", namespace="tools", input="patch\ntext"),
            tool_result(type="custom_tool_call_output", output=""),
        ])
        envelope = json.loads(json.loads(native["arguments"])["code"])
        self.assertEqual(envelope, {"name": "tools.apply_patch", "input": "patch\ntext"})
        self.assertEqual(result["type"], "function_call_output")
        self.assertEqual(result["output"], "")

    def test_compaction_notifications_and_attribution_preserve_history(self):
        history = [
            {"type": "message", "role": "user", "content": "early"},
            {"type": "message", "role": "user", "content": "<subagent_notification>done</subagent_notification>"},
            {"type": "compaction", "id": "cmp_original", "encrypted_content": "opaque"},
            {"type": "message", "role": "assistant", "author": "a", "recipient": "b", "content": [{"type": "output_text", "text": "late"}]},
        ]
        self.assertEqual(excel_input.translate_input_items(history), history)

    def test_local_compaction_decodes_without_pruning_previous_messages(self):
        early = {"type": "message", "role": "user", "content": "early"}
        history = [early, {"type": "compaction", "encrypted_content": encode_fake_compaction("local summary")}]
        result = excel_input.translate_input_items(history)
        self.assertEqual(result[0], early)
        self.assertEqual(result[1]["type"], "message")
        self.assertIn("local summary", result[1]["content"][0]["text"])

    def test_last_compaction_trigger_is_moved_to_end(self):
        first = {"type": "compaction_trigger", "id": "first"}
        last = {"type": "compaction_trigger", "id": "last"}
        message = {"role": "user", "content": "continue"}
        self.assertEqual(excel_input.translate_input_items([first, last, message]), [message, last])

    def test_tool_outputs_are_preserved_without_plan_or_error_rewrites(self):
        for name in ("update_plan", "exec_command"):
            for output in ("", "Plan updated", "Unsupported call: run_officejs", None):
                with self.subTest(name=name, output=output):
                    result = excel_input.translate_input_items([call(name=name), tool_result(output=output)])
                    self.assertEqual(result[-1]["output"], output)
                    self.assertEqual(result[0]["name"], "run_officejs")

    def test_agent_content_keeps_opaque_state_and_metadata(self):
        item = {
            "type": "agent_message", "author": "worker", "recipient": "root",
            "content": ["visible", {"type": "encrypted_content", "encrypted_content": "opaque"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "unstable"},
        }
        original = copy.deepcopy(item)
        actual = excel_input.translate_input_items([item])[0]
        self.assertEqual(actual["type"], "agent_message")
        self.assertEqual(actual["author"], "worker")
        self.assertEqual(actual["recipient"], "root")
        self.assertEqual(actual["content"], [{"type": "input_text", "text": "visible"}, item["content"][1]])
        self.assertNotIn("internal_chat_message_metadata_passthrough", actual)
        self.assertEqual(item, original)
        for content in ("text", [{"type": "encrypted_content", "encrypted_content": ""}]):
            with self.subTest(content=content), self.assertRaises(ValueError):
                excel_input.translate_input_items([{**item, "content": content}])

    def test_encrypted_reasoning_remains_minimal(self):
        result = excel_input.translate_input_items([
            {"type": "reasoning", "id": "rs_1", "summary": ["discard"], "encrypted_content": "opaque"},
            {"type": "reasoning", "summary": "unreplayable"},
        ])
        self.assertEqual(result, [{"type": "reasoning", "summary": [], "encrypted_content": "opaque"}])

    def test_configuration_updates_are_validated_and_consumed(self):
        update = {"type": "configuration_update", "reasoning": {"effort": "high"}}
        self.assertEqual(excel_input.translate_input_items([update]), [])
        for invalid in (
            {"type": "configuration_update"},
            {**update, "extra": True},
            {**update, "reasoning": {"effort": "invalid"}},
            {**update, "reasoning": {"mode": "disabled", "effort": "high"}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                excel_input.translate_input_items([invalid])

    def test_response_conversion_does_not_write_native_history(self):
        native = {
            "type": "function_call", "call_id": "call_history", "id": "fc_upstream",
            "name": "run_officejs", "arguments": json.dumps({"code": '{"name":"exec_command","arguments":{"cmd":"echo history"}}'}),
        }
        source = {"tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}]}
        with (
            patch.object(excel_tool_history, "remember_native_calls", side_effect=AssertionError("cache write")),
            patch.object(excel_tool_transport, "_remember_native_call", side_effect=AssertionError("cache write")),
        ):
            single = excel_tool_transport.extract_native_client_tool_call({"output": [native]}, source)
            batch = excel_tool_transport.extract_native_client_tool_calls({"output": [native]}, source)
        self.assertEqual(batch, [single])
        self.assertEqual(single["name"], "exec_command")

    def test_new_custom_marker_preserves_literal_input_and_rejects_function_targets(self):
        literal = 'raw "patch"\n\\path'
        native = {
            "type": "function_call", "id": "fc_custom", "call_id": "call_custom", "name": "run_officejs",
            "arguments": json.dumps({"summary": "codex2api.custom/apply_patch", "code": literal}),
        }
        source = {"tools": [{"type": "custom", "name": "apply_patch"}]}
        result = excel_tool_transport.extract_native_client_tool_call({"output": [native]}, source)
        self.assertIsNotNone(result)
        self.assertEqual(result["input"], literal)
        source["tools"][0]["type"] = "function"
        self.assertIsNone(excel_tool_transport.extract_native_client_tool_call({"output": [native]}, source))

    def test_transport_aliases_preserve_client_arguments_and_reject_conflicts(self):
        literal = 'raw "patch"\n\\path\r\n\t  '
        source = {"tools": [
            {"type": "function", "name": "exec_command", "parameters": {
                "type": "object", "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"], "additionalProperties": False,
            }},
            {"type": "custom", "name": "apply_patch"},
        ]}
        fixtures = [
            ({"tool": "exec_command", "args": {"cmd": literal}}, "arguments", {"cmd": literal}),
            ({"name": "apply_patch", "args": literal}, "input", literal),
            ({"name": "exec_command", "tool": "other", "arguments": {"cmd": literal}}, None, None),
            ({"name": "exec_command", "arguments": {"cmd": literal}, "args": {"cmd": "different"}}, None, None),
            ({"name": "apply_patch", "input": literal, "args": literal}, None, None),
            ({"tool": "apply_patch", "args": literal, "arguments": {"patch": literal}}, None, None),
            ({"tool": "exec_command", "args": {"cmd": 7}}, None, None),
        ]
        for envelope, field, expected in fixtures:
            native = {
                "type": "function_call", "id": "fc_alias", "call_id": "call_alias",
                "name": "run_officejs", "arguments": json.dumps({"code": json.dumps(envelope)}),
            }
            original = copy.deepcopy(native)
            with self.subTest(envelope=envelope):
                actual = excel_tool_transport.extract_native_client_tool_call({"output": [native]}, source)
                if field is None:
                    self.assertIsNone(actual)
                else:
                    self.assertIsNotNone(actual)
                    value = json.loads(actual[field]) if field == "arguments" else actual[field]
                    self.assertEqual(value, expected)
                self.assertEqual(native, original)

    def test_nested_transport_aliases_use_the_same_unambiguous_contract(self):
        source = {"tools": [{"type": "function", "name": "demo"}]}
        inner = {"tool": "demo", "args": {"value": "literal\\path\n"}}
        wrapper = {"tool": "run_officejs", "args": {"code": json.dumps(inner)}}
        for conflict in (False, True):
            envelope = {**wrapper, **({"arguments": wrapper["args"]} if conflict else {})}
            native = {
                "type": "function_call", "id": "fc_nested", "call_id": "call_nested",
                "name": "run_officejs", "arguments": json.dumps({"code": json.dumps(envelope)}),
            }
            with self.subTest(conflict=conflict):
                actual = excel_tool_transport.extract_native_client_tool_call({"output": [native]}, source)
                if conflict:
                    self.assertIsNone(actual)
                else:
                    self.assertIsNotNone(actual)
                    self.assertEqual(json.loads(actual["arguments"]), inner["args"])

    def test_custom_marker_rejects_malformed_names_and_oversized_input(self):
        source = {"tools": [{"type": "custom", "name": "apply_patch"}]}
        for name, value in (
            ("", "patch"), ("apply_patch/extra", "patch"),
            ("apply_patch\\extra", "patch"), ("apply_patch ", "patch"),
            ("apply_patch\x80", "patch"), ("functions.apply_patch", "patch"),
            ("apply_patch", None), ("apply_patch", "x" * ((1 << 20) + 1)),
        ):
            native = {
                "type": "function_call", "id": "fc_custom", "call_id": "call_custom", "name": "run_officejs",
                "arguments": json.dumps({"summary": "codex2api.custom/" + name, "code": value}),
            }
            with self.subTest(name=name, size=len(value) if isinstance(value, str) else None):
                self.assertIsNone(excel_tool_transport.extract_native_client_tool_call({"output": [native]}, source))


class BpsContentAlignmentTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_image_variants_and_strings_are_normalized_without_mutation(self):
        parts = [
            "visible",
            {"type": "image_url", "detail": "low", "image_url": {"url": "https://example.com/a.png", "detail": "high"}},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/b.png"}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}},
            {"type": "image", "mimeType": "image/png", "data": "Yg=="},
            {"type": "input_image", "file_id": "file-existing"},
        ]
        expected = [
            {"type": "input_text", "text": "visible"},
            {"type": "input_image", "image_url": "https://example.com/a.png", "detail": "high"},
            {"type": "input_image", "image_url": "https://example.com/b.png"},
            {"type": "input_image", "image_url": "data:image/png;base64,YQ=="},
            {"type": "input_image", "image_url": "data:image/png;base64,Yg=="},
            {"type": "input_image", "file_id": "file-existing"},
        ]
        original = copy.deepcopy(parts)
        message = excel_input.translate_input_items([{"role": "user", "content": parts}])[0]
        output = excel_input.translate_input_items([call(), tool_result(output=parts)])[-1]
        self.assertEqual(message["content"], expected)
        self.assertEqual(output["output"], expected)
        self.assertEqual(parts, original)

    def test_bad_url_detail_and_content_types_fail_with_fixed_diagnostics(self):
        parts = [
            {"type": "input_image", "image_url": value}
            for value in ("", " https://example.com/a", "http://example.com/a", "https:relative", "https://user:password@example.com/a", "https://example.com:invalid/a", "https://example.com/a\n")
        ] + [
            {"type": "input_image", "image_url": "https://example.com/a", "detail": value}
            for value in ("medium", 3, [], {})
        ] + [
            {"type": value, "data": "PRIVATE_CONTENT"}
            for value in ("input_audio", "input_file", "resource", "PRIVATE_TYPE")
        ] + [None, 8]
        for part in parts:
            with self.subTest(part=part), self.assertRaises(ValueError) as caught:
                excel_input.translate_input_items([{"role": "user", "content": [part]}])
            self.assertNotIn("PRIVATE", str(caught.exception))

    async def test_all_content_is_validated_before_any_attachment_upload(self):
        first = {"role": "user", "content": [{"type": "image", "mimeType": "image/png", "data": "YQ=="}]}
        for invalid in (
            {"type": "input_audio", "data": "invalid"},
            {"type": "input_image", "image_url": "https://example.com/a", "detail": "wrong"},
            {"type": "input_image", "image_url": "http://example.com/a"},
            {"type": "image", "mimeType": "image/png", "data": "invalid"},
        ):
            body = {"input": [first, {"role": "user", "content": [invalid]}]}
            with self.subTest(invalid=invalid), patch.object(excel_images, "_upload", new_callable=AsyncMock) as upload:
                with self.assertRaises(ValueError):
                    await excel_images.ExcelImageUploads().rewrite(body, None, {})
                upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
