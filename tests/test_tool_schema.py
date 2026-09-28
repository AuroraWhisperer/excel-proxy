import json
import unittest
from unittest.mock import patch

import excel_upstream
import excel_tool_transport
import tool_schema
from test_excel_tool_compatibility import transport


class ToolSchemaTests(unittest.TestCase):
    def test_declared_drafts_and_nested_schema_cannot_escape_budget(self):
        drafts = (
            "http://json-schema.org/draft-07/schema#",
            "https://json-schema.org/draft/2020-12/schema",
        )
        for draft in drafts:
            schema = {
                "allOf": [{"$schema": draft, "type": "integer"} for _ in range(8)]
            }
            before = json.dumps(schema)
            with self.subTest(draft=draft):
                self.assertTrue(tool_schema.arguments_match_schema(1, schema))
                with patch.object(tool_schema, "MAX_VALIDATION_STEPS", 6):
                    self.assertFalse(tool_schema.arguments_match_schema(1, schema))
                self.assertEqual(json.dumps(schema), before)
        draft4 = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "type": "number",
            "minimum": 1,
            "exclusiveMinimum": True,
        }
        self.assertTrue(tool_schema.arguments_match_schema(2, draft4))
        self.assertFalse(tool_schema.arguments_match_schema(1, draft4))

    def test_constraints_accept_valid_values_and_reject_invalid_values(self):
        cases = [
            ({"type": "integer", "minimum": 1, "maximum": 3}, 2, 9),
            ({"type": "number", "exclusiveMinimum": 1}, 1.5, 1),
            ({"type": "integer", "multipleOf": 2}, 4, 3),
            ({"type": "array", "minItems": 1, "maxItems": 2}, [1], []),
            ({"type": "array", "uniqueItems": True}, [1, 2], [1, 1]),
            ({"type": "string", "minLength": 2, "maxLength": 4}, "ok", "x"),
            ({"type": "string", "pattern": "^[a-z]+$"}, "ok", "123"),
            ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, "ok", False),
            ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, -1, 1),
            ({"allOf": [{"type": "integer"}, {"maximum": 3}]}, 2, 9),
            ({"not": {"type": "string"}}, 1, "x"),
            ({"const": "ok"}, "ok", "wrong"),
            ({"enum": [1]}, 1, True),
            ({"type": "object", "additionalProperties": False}, {}, {"extra": 1}),
            (
                {"properties": {"x": {"type": "string"}}, "required": ["x"]},
                {"x": "ok"},
                {},
            ),
            (
                {"type": "object", "additionalProperties": {"type": "integer"}},
                {"x": 1},
                {"x": "bad"},
            ),
        ]
        for schema, valid, invalid in cases:
            with self.subTest(schema=schema):
                self.assertTrue(excel_upstream._value_matches_schema(valid, schema))
                self.assertFalse(excel_upstream._value_matches_schema(invalid, schema))

    def test_local_references_preserve_large_integers(self):
        exact = 9007199254740993
        schema = {
            "$defs": {"id": {"type": "integer", "const": exact}},
            "$ref": "#/$defs/id",
        }
        self.assertTrue(excel_upstream._value_matches_schema(exact, schema))
        self.assertFalse(excel_upstream._value_matches_schema(exact - 1, schema))

    def test_boolean_schemas_are_not_treated_as_missing(self):
        self.assertTrue(excel_upstream._value_matches_schema({}, True))
        self.assertFalse(excel_upstream._value_matches_schema({}, False))

    def test_bad_schemas_and_external_references_fail_closed(self):
        for schema in (
            {"type": "not-a-type"},
            {"$ref": "https://example.invalid/private-schema"},
            {"$ref": "file:///PRIVATE_SCHEMA"},
            {"$ref": "#/$defs/missing"},
            {"$ref": "#"},
        ):
            with (
                self.subTest(schema=schema),
                patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("No remote schemas"),
                ),
            ):
                self.assertFalse(excel_upstream._value_matches_schema({}, schema))

    def test_nonfinite_numbers_and_excessive_depth_fail_closed(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertFalse(
                    excel_upstream._value_matches_schema(value, {"type": "number"})
                )
        value = {}
        for _ in range(140):
            value = {"nested": value}
        self.assertFalse(excel_upstream._value_matches_schema(value, {}))

    def test_schema_size_is_bounded(self):
        schema = {"description": "x" * (1024 * 1024 + 1)}
        self.assertFalse(excel_upstream._value_matches_schema({}, schema))

    def test_invalid_arguments_reject_the_entire_batch_without_cache_writes(self):
        tool = {
            "type": "function",
            "name": "bounded",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "maximum": 3}},
                "required": ["count"],
            },
        }
        valid = transport({"name": "bounded", "arguments": {"count": 2}})
        invalid = transport({"name": "bounded", "arguments": {"count": 99}})
        invalid.update(id="fc_invalid", call_id="call_invalid")
        diagnostics = {}
        with patch.object(excel_tool_transport, "_remember_native_calls") as remember:
            self.assertIsNone(
                excel_upstream.extract_native_client_tool_calls(
                    {"output": [valid, invalid]},
                    {"tools": [tool]},
                    diagnostics=diagnostics,
                )
            )
        remember.assert_not_called()
        self.assertEqual(diagnostics["reason"], "arguments_schema_mismatch")
        self.assertNotIn("99", json.dumps(diagnostics))

    def test_declared_false_schema_rejects_function_calls(self):
        native = transport({"name": "disabled", "arguments": {}})
        with patch.object(excel_tool_transport, "_remember_native_calls") as remember:
            self.assertIsNone(
                excel_upstream.extract_native_client_tool_call(
                    {"output": [native]},
                    {
                        "tools": [
                            {
                                "type": "function",
                                "name": "disabled",
                                "parameters": False,
                            }
                        ]
                    },
                )
            )
        remember.assert_not_called()
