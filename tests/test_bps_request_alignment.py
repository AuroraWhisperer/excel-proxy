"""Request-contract fixtures from bps-local-public 203c716."""

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import excel_upstream
import structured_output


class BpsRequestAlignmentTests(unittest.TestCase):
    def test_bare_astra_preserves_explicit_routing_and_default_effort(self):
        body = excel_upstream.prepare_responses_body(
            {"model": "gpt-6-astra", "input": "hello"}
        )
        self.assertEqual(body, {
            "model": "gpt-6-astra",
            "model_selection": "explicit",
            "stream": False,
            "store": False,
            "input": [
                {"type": "message", "role": "developer", "content": [{
                    "type": "input_text",
                    "text": "This request comes from an external Responses client. "
                    "Return assistant text. Do not call Excel, Office, workbook or connector tools.",
                }]},
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "hello"},
                ]},
            ],
            "reasoning_effort": "medium",
            "context_management": [{"type": "compaction", "compact_threshold": 200000}],
            "metadata": {"agent_iteration": "1"},
        })

    def test_one_protocol_message_contains_catalog_then_structured_contract(self):
        source = {
            "model": "gpt-6-astra-excel", "input": "hello",
            "instructions": "  caller instructions\n",
            "reasoning": {"effort": "high"},
            "tools": [{"type": "function", "name": "update_plan"}],
            "text": {"format": {"type": "json_object"}},
        }
        body = excel_upstream.prepare_responses_body(source)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(len(body["input"]), 3)
        self.assertEqual(body["input"][0]["content"][0]["text"], source["instructions"])
        protocol = body["input"][1]["content"][0]["text"]
        self.assertIn("Call one client tool at a time, including update_plan through this transport.", protocol)
        self.assertIn('Client tool "update_plan" (function).', protocol)
        self.assertTrue(protocol.endswith("\n" + structured_output.instructions(
            structured_output.request_format(source)
        )))
        self.assertNotIn("excel-proxy.raw/", protocol)
        self.assertNotIn("Reminder:", protocol)
        self.assertEqual(body["input"][-1]["role"], "user")

    def test_whitespace_only_caller_instructions_are_preserved(self):
        body = excel_upstream.prepare_responses_body({"input": "hello", "instructions": " \n\t"})
        self.assertEqual(body["input"][0]["content"][0]["text"], " \n\t")

    def test_catalog_is_prose_and_preserves_nested_schema_constraints(self):
        schema = {
            "type": "object", "description": "Pick an entry.",
            "properties": {
                "z": {"type": "array", "items": {"$ref": "#/$defs/item"}, "minItems": 1},
                "a": {"type": "string", "enum": ["x", "y"], "maxLength": 4},
            },
            "required": ["a"], "additionalProperties": False,
            "$defs": {"item": {"type": "number", "minimum": 0}},
        }
        source = {"input": "hello", "tools": [{
            "type": "namespace", "name": "functions", "tools": [
                {"type": "function", "name": "choose", "description": "Choose <item>.", "parameters": schema},
                {"type": "custom", "name": "apply_patch", "description": "Apply a patch.",
                 "format": {"type": "grammar", "syntax": "lark", "definition": "start: PATCH"}},
            ],
        }]}
        body = excel_upstream.prepare_responses_body(source)
        protocol = body["input"][0]["content"][0]["text"]
        expected = (
            'Client tool "functions.choose" (function). Choose <item>. '
            "Pass a JSON object in the envelope's arguments field. Argument contract: "
            'Value type: "object". Pick an entry. Field "a" (required): Value type: "string". '
            'Additional constraints: {"enum":["x","y"],"maxLength":4}. '
            'Field "z" (optional): Value type: "array". Each array item: '
            'Additional constraints: {"$ref":"#/$defs/item"}. Additional constraints: {"minItems":1}. '
            'Additional constraints: {"$defs":{"item":{"minimum":0,"type":"number"}},'
            '"additionalProperties":false,"required":["a"]}.\n\n'
            'Client tool "functions.apply_patch" (custom). Apply a patch. '
            'Set run_officejs summary to "codex2api.custom/functions.apply_patch" '
            'and pass its exact raw text directly in code. Input format: '
            '{"definition":"start: PATCH","syntax":"lark","type":"grammar"}.'
        )
        self.assertIn("Client tool catalog:\n" + expected + "\nEnd of catalog.", protocol)

    def test_cache_key_only_uses_explicit_field_and_preserves_whitespace(self):
        source = {"input": "hello", "prompt_cache_key": "  key\n", "session_id": "ignored"}
        before = copy.deepcopy(source)
        identity = json.dumps(["local", source["prompt_cache_key"]], ensure_ascii=False, separators=(",", ":"))
        body = excel_upstream.prepare_responses_body(source)
        self.assertEqual(body["prompt_cache_key"], "bps-" + hashlib.sha256(identity.encode()).hexdigest()[:32])
        self.assertEqual(source, before)
        for extra in (
            {"promptCacheKey": "key"}, {"session_id": "key"}, {"sessionId": "key"},
            {"client_metadata": {"session_id": "key", "sessionId": "key"}},
            {"prompt_cache_key": ""}, {"prompt_cache_key": None},
        ):
            with self.subTest(extra=extra):
                self.assertNotIn("prompt_cache_key", excel_upstream.prepare_responses_body({"input": "hello", **extra}))
        self.assertIn("prompt_cache_key", excel_upstream.prepare_responses_body({"input": "hello", "prompt_cache_key": " "}))

    def test_cache_fingerprint_uses_go_json_escaping(self):
        body = excel_upstream.prepare_responses_body({"input": "hello", "prompt_cache_key": "<>&\u2028\u2029"})
        identity = b'["local","\\u003c\\u003e\\u0026\\u2028\\u2029"]'
        self.assertEqual(body["prompt_cache_key"], "bps-" + hashlib.sha256(identity).hexdigest()[:32])

    def test_old_environment_switches_do_not_change_protocol_or_cache(self):
        source = {"input": "hello", "prompt_cache_key": "key", "tools": [{"type": "function", "name": "demo"}]}
        baseline = excel_upstream.prepare_responses_body(source)
        with (
            patch.object(excel_upstream, "CATALOG_AT_PROMPT_END", True, create=True),
            patch.object(excel_upstream, "FORWARD_PROMPT_CACHE_KEY", False, create=True),
        ):
            self.assertEqual(excel_upstream.prepare_responses_body(source), baseline)

    def test_top_level_hosted_tools_and_forced_choices_are_sanitized(self):
        for kind in ("web_search", "web_search_2025_08_26_preview", "image_generation", "mcp"):
            source = {"input": "hello", "tools": [{"type": "  " + kind.upper() + "  "}], "tool_choice": {"type": kind}}
            before = copy.deepcopy(source)
            with self.subTest(kind=kind):
                body = excel_upstream.prepare_responses_body(source)
                self.assertEqual(body["input"][0]["content"][0]["text"], excel_upstream.EXTERNAL_CLIENT_INSTRUCTIONS)
                self.assertEqual(source, before)
        for choice in ({"type": "web_search"}, {"name": "image_generation_preview"}):
            excel_upstream.prepare_responses_body({"input": "hello", "tools": [], "tool_choice": choice})

    def test_forced_tool_choice_rewrite_requires_tools_array_and_hosted_choice(self):
        for extra in (
            {"tool_choice": {"type": "web_search"}},
            {"tools": [], "tool_choice": {"type": "mcp"}},
            {"tools": [], "tool_choice": {"type": "function", "name": "demo"}},
            {"tools": {}, "tool_choice": {"type": "web_search"}},
        ):
            with self.subTest(extra=extra), self.assertRaises(excel_upstream.ExcelRequestError) as caught:
                excel_upstream.prepare_responses_body({"input": "hello", **extra})
            self.assertEqual(caught.exception.param, "tool_choice")

    def test_nested_and_history_hosted_tools_produce_sorted_warning(self):
        source = {
            "input": [{"type": "additional_tools", "tools": [{"type": "tool_search"}]}],
            "tools": [{"type": "namespace", "name": "functions", "tools": [
                {"type": "web_search"}, {"type": "computer"},
            ]}],
        }
        protocol = excel_upstream.prepare_responses_body(source)["input"][0]["content"][0]["text"]
        self.assertEqual(protocol, excel_upstream.EXTERNAL_CLIENT_INSTRUCTIONS +
            "\nHosted tools unavailable through Basispoints: computer, tool_search, web_search. "
            "These declarations were omitted. Do not claim to have used them. "
            "If the task requires one, explain the limitation or use a suitable declared client tool.")
        source["tool_choice"] = "none"
        self.assertEqual(excel_upstream.prepare_responses_body(source)["input"][0]["content"][0]["text"],
                         excel_upstream.EXTERNAL_CLIENT_INSTRUCTIONS)

    def test_invalid_declarations_are_rejected_and_presentation_duplicates_match(self):
        for tools in ([None], [{"type": "unknown"}], [{"type": "namespace", "name": ""}], [{"type": "function", "name": ""}]):
            with self.subTest(tools=tools), self.assertRaises(ValueError):
                excel_upstream.prepare_responses_body({"input": "hello", "tools": tools})
        body = excel_upstream.prepare_responses_body({"input": "hello", "tools": [
            {"type": "function", "name": "demo", "description": "First.", "x-presentation": 1},
            {"type": "function", "name": "demo", "description": "Second.", "x-presentation": 2},
        ]})
        protocol = body["input"][0]["content"][0]["text"]
        self.assertEqual(protocol.count('Client tool "demo"'), 1)
        self.assertIn("First.", protocol)
