import copy
import json
import unittest
from unittest.mock import patch

import excel_upstream
import excel_tool_transport
import excel_tool_history
import responses_protocol
import test_excel_contracts


PATCH_TEXT = '*** Begin Patch\n*** Add File: sample.txt\n+literal \\n and "quotes"\n*** End Patch\n'
FUNCTION_TOOL = {
    "type": "function",
    "name": "exec_command",
    "parameters": {
        "type": "object",
        "properties": {"cmd": {"type": "string"}},
        "required": ["cmd"],
        "additionalProperties": False,
    },
}
CUSTOM_TOOL = {"type": "custom", "name": "apply_patch"}


def transport(envelope, name="run_officejs"):
    return {
        "type": "function_call",
        "name": name,
        "status": "completed",
        "id": "fc_compat",
        "call_id": "call_compat",
        "arguments": json.dumps({"code": json.dumps(envelope)}),
    }


class ExcelToolCompatibilityTests(unittest.TestCase):
    def test_reused_call_id_does_not_substitute_another_calls_arguments(self):
        native = transport(
            {"name": "exec_command", "arguments": {"cmd": "read account-a.txt"}}
        )
        call = self.convert(native, [FUNCTION_TOOL])
        changed = {**call, "arguments": json.dumps({"cmd": "read account-b.txt"})}
        for clear_memory in (False, True):
            with self.subTest(clear_memory=clear_memory):
                if clear_memory:
                    excel_tool_history._native_call_cache.clear()
                replay = excel_upstream.translate_input_items(
                    [changed], {"exec_command": "function"}
                )
                envelope = json.loads(json.loads(replay[0]["arguments"])["code"])
                self.assertEqual(envelope["arguments"], {"cmd": "read account-b.txt"})

    def test_semantically_equal_history_preserves_exact_native_item(self):
        native = transport(
            {"name": "files.read", "arguments": {"n": 9007199254740993, "path": "a"}}
        )
        native["references"] = [{"id": "native-reference"}]
        call = self.convert(
            native,
            [
                {
                    "type": "namespace",
                    "name": "files",
                    "tools": [
                        {"type": "function", "name": "read"},
                    ],
                }
            ],
        )
        call["arguments"] = ' { "path": "a", "n": 9007199254740993 } '
        call["id"] = "client-rewritten-id"
        with patch.object(excel_tool_transport, "_remember_native_calls") as remember:
            self.assertEqual(excel_upstream.translate_input_items([call]), [native])
        remember.assert_not_called()
        call["arguments"] = '{"path":"a","n":9007199254740992}'
        self.assertNotEqual(excel_upstream.translate_input_items([call]), [native])
        call["arguments"] = '{"path":"a","n":9007199254740993}'
        call["namespace"] = "other"
        self.assertNotEqual(excel_upstream.translate_input_items([call]), [native])

    def test_custom_history_collision_preserves_the_supplied_input(self):
        native = transport({"name": "apply_patch", "input": PATCH_TEXT})
        call = self.convert(native, [CUSTOM_TOOL])
        changed = {**call, "input": PATCH_TEXT.replace("sample.txt", "other.txt")}
        replay = excel_upstream.translate_input_items(
            [changed], {"apply_patch": "custom"}
        )
        envelope = json.loads(json.loads(replay[0]["arguments"])["code"])
        self.assertEqual(envelope["input"], changed["input"])

    def test_rejected_batch_does_not_change_the_replay_cache(self):
        valid = transport({"name": "exec_command", "arguments": {"cmd": "pwd"}})
        invalid = {
            **valid,
            "call_id": "call_invalid",
            "id": "fc_invalid",
            "arguments": "broken",
        }
        with patch.object(excel_tool_transport, "_remember_native_calls") as remember:
            self.assertIsNone(
                excel_upstream.extract_native_client_tool_calls(
                    {"output": [valid, invalid]},
                    {"tools": [FUNCTION_TOOL]},
                )
            )
        remember.assert_not_called()

    def convert(self, native, tools):
        return excel_upstream.extract_native_client_tool_call(
            {"output": [native]},
            {"tools": tools},
        )

    def test_host_prefix_resolves_only_registered_tool(self):
        native = transport(
            {"name": "functions.exec_command", "arguments": {"cmd": "pwd"}}
        )
        original = copy.deepcopy(native)
        call = self.convert(native, [FUNCTION_TOOL])
        self.assertIsNotNone(call)
        self.assertEqual(call["name"], "exec_command")
        self.assertEqual(json.loads(call["arguments"]), {"cmd": "pwd"})
        self.assertEqual(native, original)
        replay = excel_upstream.translate_input_items(
            [call], {"exec_command": "function"}
        )
        self.assertEqual(replay, [original])
        self.assertIsNone(self.convert(native, []))

    def test_exact_catalog_name_wins_over_prefix_alias(self):
        native = transport(
            {"name": "functions.exec_command", "arguments": {"cmd": "pwd"}}
        )
        namespaced = {
            "type": "namespace",
            "name": "functions",
            "tools": [FUNCTION_TOOL],
        }
        call = self.convert(native, [FUNCTION_TOOL, namespaced])
        self.assertEqual(call["namespace"], "functions")

    def test_prefix_alias_still_validates_arguments(self):
        for name in ("functions.exec_command", "other.exec_command"):
            native = transport({"name": name, "arguments": {"command": "pwd"}})
            self.assertIsNone(self.convert(native, [FUNCTION_TOOL]))

    def test_native_plan_alias_preserves_executor_result(self):
        native = {
            "type": "function_call",
            "name": "functions.update_plan",
            "id": "fc_compat_plan",
            "call_id": "call_compat_plan",
            "arguments": json.dumps(
                {
                    "summary": "Check project",
                    "plan": [
                        {"description": "Run checks", "status": "completed"},
                    ],
                }
            ),
        }
        call = self.convert(native, [{"type": "function", "name": "update_plan"}])
        self.assertEqual(call["name"], "update_plan")
        replay = excel_upstream.translate_input_items(
            [
                call,
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": "Plan updated",
                },
            ],
            {"update_plan": "function"},
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(json.loads(replay[1]["output"]), {"status": "ok"})

    def test_patch_argument_wrapper_preserves_exact_input_and_replay(self):
        for outer_name in ("run_officejs", "functions.run_officejs"):
            for arguments in ({"patch": PATCH_TEXT}, json.dumps({"patch": PATCH_TEXT})):
                with self.subTest(
                    outer_name=outer_name, arguments_type=type(arguments).__name__
                ):
                    native = transport(
                        {"name": "apply_patch", "arguments": arguments}, outer_name
                    )
                    original = copy.deepcopy(native)
                    call = self.convert(native, [CUSTOM_TOOL])
                    self.assertIsNotNone(call)
                    self.assertEqual(call["type"], "custom_tool_call")
                    self.assertEqual(call["input"], PATCH_TEXT)
                    replay = excel_upstream.translate_input_items(
                        [
                            call,
                            {
                                "type": "custom_tool_call_output",
                                "call_id": call["call_id"],
                                "output": "patch applied",
                            },
                        ],
                        {"apply_patch": "custom"},
                    )
                    self.assertEqual(replay[0], original)
                    self.assertEqual(replay[1]["type"], "function_call_output")
                    self.assertEqual(replay[1]["output"], "patch applied")

    def test_ambiguous_patch_wrappers_are_rejected(self):
        envelopes = [
            {"name": "apply_patch", "arguments": {"patch": PATCH_TEXT, "extra": True}},
            {"name": "apply_patch", "arguments": {"patch": [PATCH_TEXT]}},
            {"name": "apply_patch", "input": None, "arguments": {"patch": PATCH_TEXT}},
            {"name": "apply_patch", "arguments": "not JSON"},
            {"name": "another_custom_tool", "arguments": {"patch": PATCH_TEXT}},
        ]
        tools = [CUSTOM_TOOL, {"type": "custom", "name": "another_custom_tool"}]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                self.assertIsNone(self.convert(transport(envelope), tools))

    def test_prefixed_transport_replays_custom_output(self):
        native = transport(
            {"name": "apply_patch", "input": PATCH_TEXT}, "functions.run_officejs"
        )
        call = self.convert(native, [CUSTOM_TOOL])
        replay = excel_upstream.translate_input_items(
            [
                call,
                {
                    "type": "custom_tool_call_output",
                    "call_id": call["call_id"],
                    "output": "done",
                },
            ],
            {"apply_patch": "custom"},
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["type"], "function_call_output")

    def test_batch_rejection_identifies_stage_without_argument_values(self):
        valid = transport({"name": "exec_command", "arguments": {"cmd": "pwd"}})
        invalid = transport({"name": "exec_command", "arguments": {"cmd": 42}})
        diagnostics = {}
        result = excel_upstream.extract_native_client_tool_calls(
            {"output": [valid, invalid]},
            {"tools": [FUNCTION_TOOL]},
            diagnostics=diagnostics,
        )
        self.assertIsNone(result)
        self.assertEqual(
            diagnostics, {"tool_call_index": 1, "reason": "arguments_schema_mismatch"}
        )

    def test_split_namespace_resolves_registered_function(self):
        tools = [{"type": "namespace", "name": "plugin", "tools": [FUNCTION_TOOL]}]
        envelope = {
            "name": "exec_command",
            "namespace": "plugin",
            "arguments": {"cmd": "pwd"},
        }
        natives = [
            transport(envelope),
            {
                "type": "function_call",
                "name": "exec_command",
                "namespace": "plugin",
                "id": "fc_split_namespace",
                "call_id": "call_split_namespace",
                "arguments": json.dumps({"cmd": "pwd"}),
            },
        ]
        for native in natives:
            with self.subTest(native_name=native["name"]):
                call = self.convert(native, tools)
                self.assertIsNotNone(call)
                self.assertEqual(call["name"], "exec_command")
                self.assertEqual(call["namespace"], "plugin")
                self.assertEqual(json.loads(call["arguments"]), {"cmd": "pwd"})

    def test_explicit_unknown_namespace_does_not_select_global_tool(self):
        native = transport(
            {
                "name": "exec_command",
                "namespace": "unknown",
                "arguments": {"cmd": "pwd"},
            }
        )
        self.assertIsNone(self.convert(native, [FUNCTION_TOOL]))

    def test_namespaced_history_fallback_preserves_tool_identity(self):
        item = {
            "type": "function_call",
            "name": "exec_command",
            "namespace": "plugin",
            "call_id": "call_uncached_namespace",
            "arguments": '{"cmd":"pwd"}',
        }
        native = excel_upstream._fallback_transport_call(item)
        envelope = json.loads(json.loads(native["arguments"])["code"])
        self.assertEqual(envelope["name"], "plugin.exec_command")
        self.assertEqual(envelope["arguments"], {"cmd": "pwd"})
        self.assertEqual(native["call_id"], item["call_id"])

    def test_redundant_namespace_does_not_duplicate_qualified_name(self):
        native = transport(
            {
                "name": "plugin.exec_command",
                "namespace": "plugin",
                "arguments": {"cmd": "pwd"},
            }
        )
        tools = [{"type": "namespace", "name": "plugin", "tools": [FUNCTION_TOOL]}]
        call = self.convert(native, tools)
        self.assertIsNotNone(call)
        self.assertEqual(call["namespace"], "plugin")

    def test_nested_catalog_namespaces_keep_parent_path(self):
        source = {
            "tools": [
                {
                    "type": "namespace",
                    "name": "plugin",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "shell",
                            "tools": [FUNCTION_TOOL],
                        },
                    ],
                }
            ]
        }
        self.assertEqual(
            excel_upstream.client_tool_types(source),
            {"plugin.shell.exec_command": "function"},
        )
        call = self.convert(
            transport(
                {"name": "plugin.shell.exec_command", "arguments": {"cmd": "pwd"}}
            ),
            source["tools"],
        )
        self.assertIsNotNone(call)
        self.assertEqual(call["namespace"], "plugin.shell")

    def test_double_encoded_transport_code_preserves_command(self):
        envelope = {
            "name": "exec_command",
            "arguments": {"cmd": r'Write-Output "C:\tmp\file.txt"'},
        }
        native = transport(envelope)
        native["arguments"] = json.dumps({"code": json.dumps(json.dumps(envelope))})
        call = self.convert(native, [FUNCTION_TOOL])
        self.assertIsNotNone(call)
        self.assertEqual(json.loads(call["arguments"]), envelope["arguments"])

    def test_single_invocation_preserves_arguments_and_native_replay(self):
        arguments = {
            "cmd": 'echo "quoted"',
            "number": 9007199254740993,
            "nested": {"name": "apply_patch", "arguments": {"enabled": True}},
        }
        tool = {"type": "function", "name": "exec_command"}
        for prefix in ("", "await ", "return ", "return await "):
            for outer_object in (False, True):
                with self.subTest(prefix=prefix, outer_object=outer_object):
                    native = transport({})
                    outer = {
                        "code": prefix
                        + "functions.exec_command("
                        + json.dumps(arguments)
                        + " ) ; "
                    }
                    native["arguments"] = outer if outer_object else json.dumps(outer)
                    original = copy.deepcopy(native)
                    call = self.convert(native, [tool])
                    self.assertIsNotNone(call)
                    self.assertEqual(call["name"], "exec_command")
                    self.assertEqual(json.loads(call["arguments"]), arguments)
                    replay = excel_upstream.translate_input_items(
                        [
                            call,
                            {
                                "type": "function_call_output",
                                "call_id": call["call_id"],
                                "output": "done",
                            },
                        ],
                        {"exec_command": "function"},
                    )
                    self.assertEqual(replay[0], original)
                    self.assertEqual(replay[1]["output"], "done")
                    self.assertEqual(native, original)

    def test_single_custom_invocation_preserves_literal_input(self):
        native = transport({})
        native["arguments"] = json.dumps(
            {"code": "await plugin.apply_patch(" + json.dumps(PATCH_TEXT) + " );"}
        )
        tools = [{"type": "namespace", "name": "plugin", "tools": [CUSTOM_TOOL]}]
        call = self.convert(native, tools)
        self.assertIsNotNone(call)
        self.assertEqual(call["namespace"], "plugin")
        self.assertEqual(call["input"], PATCH_TEXT)
        replay = excel_upstream.translate_input_items(
            [
                call,
                {
                    "type": "custom_tool_call_output",
                    "call_id": call["call_id"],
                    "output": "patched",
                },
            ],
            {"plugin.apply_patch": "custom"},
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["type"], "function_call_output")
        self.assertEqual(replay[1]["output"], "patched")

    def test_object_transport_arguments_preserve_json_envelope(self):
        envelope = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        for code in (envelope, json.dumps(envelope)):
            with self.subTest(code_type=type(code).__name__):
                native = transport({})
                native["arguments"] = {"code": code}
                call = self.convert(native, [FUNCTION_TOOL])
                self.assertIsNotNone(call)
                self.assertEqual(json.loads(call["arguments"]), envelope["arguments"])

    def test_invocation_recovery_rejects_ambiguous_or_executable_code(self):
        call = 'functions.exec_command({"cmd":"pwd"})'
        codes = [
            call + "; " + call,
            call[:-1],
            call + "; other()",
            'functions.exec_command({"cmd":"pwd"}, {})',
            'functions.exec_command({cmd: "pwd"})',
            'functions.exec_command({"cmd": other()})',
            'functions.exec_command("not an object")',
            'functions.apply_patch({"patch":"not raw input"})',
            'functions.apply_patch("exact"); other()',
            'functions.exec_command({"cmd": 42})',
            'unknown({"name":"exec_command","arguments":{"cmd":"pwd"}})',
            "Excel.run(() => " + call + ")",
        ]
        for code in codes:
            with self.subTest(code=code):
                native = transport({})
                native["arguments"] = json.dumps({"code": code})
                self.assertIsNone(self.convert(native, [FUNCTION_TOOL, CUSTOM_TOOL]))

    def test_transport_failures_report_only_structure_and_json_position(self):
        cases = [
            ("PRIVATE_ARGUMENT", "arguments", "string", True),
            ({"code": ["PRIVATE_ARGUMENT"]}, "arguments.code", "array", False),
            (
                {"code": '{"name":"PRIVATE_TOOL","arguments":'},
                "arguments.code",
                "string",
                True,
            ),
            ({}, "arguments.code", "null", False),
        ]
        for arguments, field, value_type, invalid_json in cases:
            with self.subTest(field=field, value_type=value_type):
                native = {**transport({}), "arguments": arguments}
                diagnostics = {}
                calls = excel_upstream.extract_native_client_tool_calls(
                    {"output": [native]},
                    {"tools": [FUNCTION_TOOL]},
                    diagnostics=diagnostics,
                )
                self.assertIsNone(calls)
                self.assertEqual(diagnostics["reason"], "invalid_transport_envelope")
                self.assertEqual(diagnostics["transport_field"], field)
                self.assertEqual(diagnostics["transport_value_type"], value_type)
                if invalid_json:
                    self.assertGreaterEqual(diagnostics["json_line"], 1)
                    self.assertGreaterEqual(diagnostics["json_column"], 1)
                message = excel_upstream.tool_call_failure_message(diagnostics)
                self.assertIn("field=" + field, message)
                self.assertIn("type=" + value_type, message)
                for private in ("PRIVATE_ARGUMENT", "PRIVATE_TOOL"):
                    self.assertNotIn(private, message + json.dumps(diagnostics))

    def test_backslash_repair_keeps_outer_envelope(self):
        code = r'{"name":"exec_command","arguments":{"cmd":"rg \d"},"metadata":{}}'
        for prefix in ("", "first\n"):
            with self.subTest(prefix=prefix):
                native = transport({})
                native["arguments"] = json.dumps(
                    {"code": code.replace('"rg ', '"' + prefix + "rg ", 1)}
                )
                call = self.convert(native, [FUNCTION_TOOL])
                self.assertIsNotNone(call)
                self.assertEqual(
                    json.loads(call["arguments"]), {"cmd": prefix + r"rg \d"}
                )

    def test_literal_control_characters_preserve_arguments_and_replay(self):
        literal = (
            'first\r\n\tsecond\nquote: "value"; literal: \\n; path: C:\\tmp\\file.txt'
        )
        encoded = json.dumps(literal)
        raw_literal = encoded.replace(r"\r\n\t", "\r\n\t", 1).replace(
            r"\nquote", "\nquote", 1
        )
        for tool in (FUNCTION_TOOL, CUSTOM_TOOL):
            custom = tool["type"] == "custom"
            envelope = {"name": tool["name"]}
            envelope.update(
                {"input": literal} if custom else {"arguments": {"cmd": literal}}
            )
            # The outer protocol is valid JSON; only the inner literal has raw controls.
            raw = json.dumps(envelope).replace(encoded, raw_literal)
            argument = raw_literal if custom else '{"cmd":' + raw_literal + "}"
            invocation = (
                "return await functions." + tool["name"] + "(" + argument + " );"
            )
            for code in (
                raw,
                "```json\n" + raw + "\n```",
                "const request = " + raw + ";",
                json.dumps(raw),
                invocation,
            ):
                with self.subTest(tool=tool["name"], code=code[:30]):
                    native = transport({})
                    native["arguments"] = json.dumps({"code": code})
                    original = copy.deepcopy(native)
                    call = self.convert(native, [tool])
                    self.assertIsNotNone(call)
                    actual = (
                        call["input"]
                        if custom
                        else json.loads(call["arguments"])["cmd"]
                    )
                    self.assertEqual(actual, literal)
                    replay = excel_upstream.translate_input_items(
                        [
                            call,
                            {
                                "type": "custom_tool_call_output"
                                if custom
                                else "function_call_output",
                                "call_id": call["call_id"],
                                "output": "done",
                            },
                        ],
                        {tool["name"]: tool["type"]},
                    )
                    self.assertEqual(replay[0], original)
                    self.assertEqual(replay[1]["output"], "done")
                    self.assertEqual(native, original)

    def test_multiline_transport_rejects_incomplete_or_ambiguous_payloads(self):
        raw = '{"name":"exec_command","arguments":{"cmd":"first\nsecond"}}'
        invocation = 'functions.exec_command({"cmd":"first\nsecond"})'
        codes = [
            raw[:-1],
            raw[:-3],
            raw + raw,
            "[" + raw + "," + raw + "]",
            raw.replace("second", '"second"'),
            invocation + "; " + invocation,
            invocation[:-1],
            raw.replace('"first\nsecond"', "42"),
        ]
        for code in codes:
            with self.subTest(code=code):
                native = transport({})
                native["arguments"] = json.dumps({"code": code})
                self.assertIsNone(self.convert(native, [FUNCTION_TOOL]))

    def test_json_error_categories_do_not_expose_argument_contents(self):
        cases = [
            ('{"name":"PRIVATE_TOOL","input":"PRIVATE_ARGUMENT', "unterminated_string"),
            (
                '{"name":"PRIVATE_TOOL","input":"PRIVATE_ARGUMENT"oops"}',
                "missing_comma",
            ),
            ('{"name":"PRIVATE_TOOL","input":"PRIVATE_ARGUMENT"}{}', "extra_data"),
        ]
        for code, category in cases:
            with self.subTest(category=category):
                native = transport({})
                native["arguments"] = json.dumps({"code": code})
                diagnostics = {}
                self.assertIsNone(
                    excel_upstream.extract_native_client_tool_calls(
                        {"output": [native]},
                        {"tools": [CUSTOM_TOOL]},
                        diagnostics=diagnostics,
                    )
                )
                self.assertEqual(diagnostics["json_error"], category)
                message = excel_upstream.tool_call_failure_message(diagnostics)
                self.assertIn("json=" + category, message)
                for private in ("PRIVATE_TOOL", "PRIVATE_ARGUMENT"):
                    self.assertNotIn(private, message + json.dumps(diagnostics))
        diagnostics = {}
        excel_upstream._transport_decode_failure(
            diagnostics,
            "arguments.code",
            "PRIVATE_ARGUMENT",
            json.JSONDecodeError("PRIVATE_PARSER_MESSAGE", "PRIVATE_ARGUMENT", 0),
        )
        self.assertEqual(diagnostics["json_error"], "invalid_json")
        self.assertNotIn("PRIVATE", json.dumps(diagnostics))

    def test_transport_wrappers_preserve_complete_custom_input(self):
        envelope = {"name": "apply_patch", "input": PATCH_TEXT}
        text = json.dumps(envelope)
        wrappers = [
            text,
            "```json\n" + text + "\n```",
            "const request = " + text + ";",
            "return " + text + ";",
        ]
        for code in wrappers:
            with self.subTest(code=code[:20]):
                native = transport({})
                native["arguments"] = json.dumps({"code": code})
                call = self.convert(native, [CUSTOM_TOOL])
                self.assertIsNotNone(call)
                self.assertEqual(call["input"], PATCH_TEXT)

    def test_ambiguous_code_never_executes_only_the_first_call(self):
        text = json.dumps({"name": "exec_command", "arguments": {"cmd": "pwd"}})
        codes = [
            text + "\n" + text,
            "[" + text + "," + text + "]",
            "const first = " + text + "; const second = " + text + ";",
            "function relay() { return " + text + "; }",
        ]
        for code in codes:
            with self.subTest(code=code[:20]):
                native = transport({})
                native["arguments"] = json.dumps({"code": code})
                self.assertIsNone(self.convert(native, [FUNCTION_TOOL]))

    def test_protocol_reminder_respects_serial_tool_requests(self):
        source = {"tools": [FUNCTION_TOOL], "parallel_tool_calls": False}
        reminder = excel_upstream._client_tool_protocol_reminder(source)
        self.assertIn("at most one tool call per response", reminder)
        self.assertIn("wait for its result", reminder)
        self.assertNotIn(
            "at most one tool call per response",
            excel_upstream._client_tool_protocol_reminder(
                {**source, "parallel_tool_calls": True}
            ),
        )

    def test_split_native_transport_namespace_replays_custom_result(self):
        native = {
            **transport({"name": "apply_patch", "input": PATCH_TEXT}),
            "namespace": "functions",
        }
        call = self.convert(native, [CUSTOM_TOOL])
        self.assertIsNotNone(call)
        replay = excel_upstream.translate_input_items(
            [
                call,
                {
                    "type": "custom_tool_call_output",
                    "call_id": call["call_id"],
                    "output": "done",
                },
            ],
            {"apply_patch": "custom"},
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["type"], "function_call_output")

    def test_namespaced_custom_tool_preserves_output_and_replay_identity(self):
        tools = [{"type": "namespace", "name": "plugin", "tools": [CUSTOM_TOOL]}]
        native = transport({"name": "plugin.apply_patch", "input": PATCH_TEXT})
        call = self.convert(native, tools)
        self.assertIsNotNone(call)
        self.assertEqual(call["name"], "apply_patch")
        self.assertEqual(call["namespace"], "plugin")
        self.assertEqual(call["input"], PATCH_TEXT)
        rebuilt = excel_upstream._fallback_transport_call(call)
        envelope = json.loads(json.loads(rebuilt["arguments"])["code"])
        self.assertEqual(envelope, {"name": "plugin.apply_patch", "input": PATCH_TEXT})

    def test_history_does_not_treat_plugin_plan_as_native_plan(self):
        original = {"client_plan": "keep exact arguments"}
        item = {
            "type": "function_call",
            "name": "update_plan",
            "namespace": "plugin",
            "call_id": "call_plugin_plan_fallback",
            "arguments": json.dumps(original),
        }
        replay = excel_upstream.translate_input_items(
            [item], {"plugin.update_plan": "function"}
        )
        self.assertEqual(replay[0]["name"], "run_officejs")
        envelope = json.loads(json.loads(replay[0]["arguments"])["code"])
        self.assertEqual(
            envelope, {"name": "plugin.update_plan", "arguments": original}
        )


class ExcelToolCompatibilityHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_excel_contracts.ExcelHTTPContractTests.asyncSetUp

    async def test_rejected_calls_report_safe_reasons_in_both_modes(self):
        cases = [
            (
                transport(
                    {
                        "name": "PRIVATE_TOOL",
                        "arguments": {"secret": "PRIVATE_ARGUMENT"},
                    }
                ),
                [FUNCTION_TOOL],
                "unknown_tool",
            ),
            (
                transport(
                    {
                        "name": "exec_command",
                        "arguments": {"secret": "PRIVATE_ARGUMENT"},
                    }
                ),
                [FUNCTION_TOOL],
                "arguments_schema_mismatch",
            ),
            (
                transport(
                    {
                        "name": "apply_patch",
                        "arguments": {"patch": ["PRIVATE_ARGUMENT"]},
                    }
                ),
                [CUSTOM_TOOL],
                "invalid_custom_input",
            ),
            (
                {**transport({}), "arguments": "PRIVATE_ARGUMENT"},
                [FUNCTION_TOOL],
                "invalid_transport_envelope",
            ),
        ]
        self.upstream_status = 200
        for native, tools, reason in cases:
            for stream in (False, True):
                with self.subTest(reason=reason, stream=stream):
                    self.finish.reset_mock()
                    self.upstream_sse = responses_protocol.sse_encode(
                        "response.completed",
                        {
                            "response": {
                                "id": "resp_rejected",
                                "status": "completed",
                                "output": [native],
                            },
                        },
                    )
                    before = len(self.requests)
                    result = await self.local.post(
                        "/v1/responses",
                        json={
                            "model": excel_upstream.MODEL_ID,
                            "input": "test",
                            "tools": tools,
                            "stream": stream,
                        },
                    )
                    self.assertEqual(len(self.requests), before + 2)
                    recovery = self.finish.call_args.args[0].trace_context[
                        "tool_call_recovery"
                    ]
                    self.assertEqual(recovery["attempts"], 1)
                    self.assertEqual(recovery["outcome"], "rejected")
                    self.assertIn(reason, result.text)
                    for private in (
                        "PRIVATE_TOOL",
                        "PRIVATE_ARGUMENT",
                        "KNOWN_CREDENTIAL",
                    ):
                        self.assertNotIn(private, result.text)
                    if stream:
                        self.assertEqual(result.text.count("event: response.failed"), 1)
                        self.assertNotIn("event: response.completed", result.text)
                        lifecycle = self.finish.call_args.args[0].trace_context[
                            "responses_stream_lifecycle"
                        ]
                        self.assertIn(reason, lifecycle["upstream_error_message"])
                    else:
                        self.assertEqual(result.status_code, 502)
                        self.assertEqual(
                            result.json()["error"]["code"],
                            "excel_untranslatable_tool_call",
                        )
                        self.assertIn(
                            reason,
                            self.finish.call_args.kwargs["response_payload"]["error"][
                                "message"
                            ],
                        )

    async def test_compatible_calls_complete_in_json_and_stream_modes(self):
        wrapped_namespace_call = transport({})
        wrapped_namespace_call["arguments"] = json.dumps(
            {
                "code": json.dumps(
                    json.dumps(
                        {
                            "name": "exec_command",
                            "namespace": "plugin",
                            "arguments": {"cmd": "pwd"},
                        }
                    )
                )
            }
        )
        invocation = transport({})
        invocation["arguments"] = json.dumps(
            {"code": 'return await functions.exec_command({"cmd":"pwd"});'}
        )
        custom_invocation = transport({})
        custom_invocation["arguments"] = {
            "code": "plugin.apply_patch(" + json.dumps(PATCH_TEXT) + " );"
        }
        multiline_custom = transport({"name": "apply_patch", "input": PATCH_TEXT})
        outer = json.loads(multiline_custom["arguments"])
        outer["code"] = outer["code"].replace(r"\n", "\n", 2)
        multiline_custom["arguments"] = json.dumps(outer)
        multiline_invocation = copy.deepcopy(custom_invocation)
        multiline_invocation["arguments"]["code"] = multiline_invocation["arguments"][
            "code"
        ].replace(r"\n", "\n", 2)
        cases = [
            (
                transport(
                    {"name": "functions.exec_command", "arguments": {"cmd": "pwd"}}
                ),
                FUNCTION_TOOL,
                "function_call",
                None,
            ),
            (
                transport({"name": "apply_patch", "arguments": {"patch": PATCH_TEXT}}),
                CUSTOM_TOOL,
                "custom_tool_call",
                None,
            ),
            (wrapped_namespace_call, FUNCTION_TOOL, "function_call", "plugin"),
            (
                transport({"name": "plugin.apply_patch", "input": PATCH_TEXT}),
                CUSTOM_TOOL,
                "custom_tool_call",
                "plugin",
            ),
            (invocation, FUNCTION_TOOL, "function_call", None),
            (custom_invocation, CUSTOM_TOOL, "custom_tool_call", "plugin"),
            (multiline_custom, CUSTOM_TOOL, "custom_tool_call", None),
            (multiline_invocation, CUSTOM_TOOL, "custom_tool_call", "plugin"),
        ]
        self.upstream_status = 200
        for native, tool, item_type, namespace in cases:
            for upstream_sse, stream in ((False, False), (True, False), (True, True)):
                with self.subTest(
                    item_type=item_type,
                    namespace=namespace,
                    upstream_sse=upstream_sse,
                    stream=stream,
                ):
                    self.upstream_body = {
                        "id": "resp_compat",
                        "status": "completed",
                        "output": [native],
                    }
                    self.upstream_sse = (
                        responses_protocol.sse_encode(
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": self.upstream_body,
                            },
                        )
                        if upstream_sse
                        else None
                    )
                    before = len(self.requests)
                    result = await self.local.post(
                        "/v1/responses",
                        json={
                            "model": excel_upstream.MODEL_ID,
                            "input": "Perform the requested action",
                            "tools": [
                                {
                                    "type": "namespace",
                                    "name": namespace,
                                    "tools": [tool],
                                }
                            ]
                            if namespace
                            else [tool],
                            "stream": stream,
                        },
                    )
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(len(self.requests), before + 1)
                    if stream:
                        events = [
                            json.loads(data)
                            for block in result.text.split("\n\n")
                            for event, data in [
                                responses_protocol.parse_sse_block(block)
                            ]
                            if event == "response.completed"
                        ]
                        self.assertEqual(len(events), 1, result.text)
                        payload = events[0]["response"]
                    else:
                        payload = result.json()
                    self.assertEqual(payload["output"][0]["type"], item_type)
                    self.assertEqual(payload["output"][0]["name"], tool["name"])
                    self.assertEqual(payload["output"][0].get("namespace"), namespace)
                    self.assertNotIn("run_officejs", json.dumps(payload))
                    item = payload["output"][0]
                    if item_type == "custom_tool_call":
                        self.assertEqual(item["input"], PATCH_TEXT)
                    else:
                        self.assertEqual(json.loads(item["arguments"]), {"cmd": "pwd"})

    async def test_invalid_invocation_batch_never_releases_a_partial_tool(self):
        valid = transport({})
        valid["arguments"] = json.dumps(
            {
                "code": '{"name":"exec_command","arguments":{"cmd":"PRIVATE_COMMAND\nsecond"}}',
            }
        )
        invalid = {**transport({}), "id": "fc_invalid", "call_id": "call_invalid"}
        invalid["arguments"] = json.dumps(
            {
                "code": 'functions.exec_command({"cmd":"PRIVATE_COMMAND"}); functions.exec_command({"cmd":"second"})',
            }
        )
        self.upstream_status = 200
        for upstream_sse, stream in ((False, False), (True, False), (True, True)):
            with self.subTest(upstream_sse=upstream_sse, stream=stream):
                self.finish.reset_mock()
                self.upstream_body = {
                    "id": "resp_ambiguous",
                    "status": "completed",
                    "output": [valid, invalid],
                }
                self.upstream_sse = (
                    b"".join(
                        [
                            responses_protocol.sse_encode(
                                "response.output_item.done",
                                {
                                    "type": "response.output_item.done",
                                    "output_index": index,
                                    "item": item,
                                },
                            )
                            for index, item in enumerate([valid, invalid])
                        ]
                    )
                    + responses_protocol.sse_encode(
                        "response.completed",
                        {
                            "type": "response.completed",
                            "response": self.upstream_body,
                        },
                    )
                    if upstream_sse
                    else None
                )
                before = len(self.requests)
                result = await self.local.post(
                    "/v1/responses",
                    json={
                        "model": excel_upstream.MODEL_ID,
                        "input": "test",
                        "tools": [FUNCTION_TOOL],
                        "stream": stream,
                    },
                )
                self.assertEqual(len(self.requests), before + 2)
                self.assertIn("invalid_transport_envelope", result.text)
                self.assertIn("field=arguments.code", result.text)
                self.assertIn("call 2:", result.text)
                self.assertIn("json=missing_value", result.text)
                self.assertNotIn("PRIVATE_COMMAND", result.text)
                self.assertNotIn("response.function_call_arguments", result.text)
                self.assertNotIn("event: response.output_item", result.text)
                if stream:
                    self.assertEqual(result.text.count("event: response.failed"), 1)
                    self.assertNotIn("event: response.completed", result.text)
                else:
                    self.assertEqual(result.status_code, 502)
