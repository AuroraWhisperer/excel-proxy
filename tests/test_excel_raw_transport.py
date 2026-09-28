import copy
import json
import unittest
from unittest.mock import patch

import excel_upstream
import excel_tool_transport
import excel_input
import excel_tool_history
import test_excel_call_diagnostics
import test_excel_contracts
from test_excel_tool_compatibility import (
    CUSTOM_TOOL,
    FUNCTION_TOOL,
    PATCH_TEXT,
    transport,
)


COMMAND = (
    "Write-Output "
    + chr(34)
    + "中文"
    + chr(34)
    + "; $p = C:"
    + chr(92)
    + "tmp"
    + chr(10)
    + "echo done"
)
CODE_TOOL = {
    "type": "function",
    "name": "js",
    "parameters": {
        "type": "object",
        "properties": {"code": {"type": "string"}, "title": {"type": "string"}},
        "required": ["code"],
        "additionalProperties": False,
    },
}


def raw_call(name, field, text, metadata=None):
    native = transport({})
    native["arguments"] = json.dumps(
        {
            "summary": f"excel-proxy.raw/{field}/{name}",
            "code": text,
            "extended_summary": json.dumps(metadata or {}),
            "destructive": False,
            "references": [],
        }
    )
    return native


def unframed_call(text):
    native = transport({})
    native["arguments"] = json.dumps({"code": text})
    return native


class RawTransportTests(unittest.TestCase):
    def convert(self, native, tools):
        return excel_upstream.extract_native_client_tool_call(
            {"output": [native]},
            {"tools": tools},
            remember=False,
        )

    def test_commands_round_trip_without_json_or_source_repair(self):
        for command in (
            COMMAND,
            "",
            "  echo trailing  " + chr(10),
            json.dumps({"name": "not_a_nested_call"}),
        ):
            with self.subTest(command=command):
                native = raw_call("exec_command", "cmd", command)
                before = copy.deepcopy(native)
                call = self.convert(native, [FUNCTION_TOOL])
                self.assertIsNotNone(call)
                self.assertEqual(json.loads(call["arguments"]), {"cmd": command})
                self.assertEqual(native, before)

    def test_namespaced_code_preserves_metadata(self):
        code = (
            "nodeRepl.write("
            + chr(34)
            + "literal "
            + chr(92)
            + "n"
            + chr(34)
            + ");"
            + chr(10)
        )
        tool = {"type": "namespace", "name": "node", "tools": [CODE_TOOL]}
        call = self.convert(
            raw_call("node.js", "code", code, {"title": "检查"}), [tool]
        )
        self.assertIsNotNone(call)
        self.assertEqual(call["namespace"], "node")
        self.assertEqual(call["name"], "js")
        self.assertEqual(json.loads(call["arguments"]), {"code": code, "title": "检查"})

    def test_custom_input_is_literal(self):
        call = self.convert(raw_call("apply_patch", "input", PATCH_TEXT), [CUSTOM_TOOL])
        self.assertIsNotNone(call)
        self.assertEqual(call["type"], "custom_tool_call")
        self.assertEqual(call["input"], PATCH_TEXT)

    def test_only_declared_exact_targets_and_supported_fields_are_allowed(self):
        for name, field in (
            ("absent", "cmd"),
            ("functions.exec_command", "cmd"),
            ("run_officejs", "code"),
            ("exec_command", "code"),
        ):
            with self.subTest(name=name, field=field):
                self.assertIsNone(
                    self.convert(raw_call(name, field, COMMAND), [FUNCTION_TOOL])
                )
        self.assertIsNone(
            self.convert(
                raw_call("exec_command", "cmd", COMMAND),
                [{"type": "function", "name": "exec_command"}],
            )
        )

    def test_unknown_raw_target_is_distinguished_from_invalid_raw_data(self):
        for name in ("absent", "functions.exec_command"):
            diagnostic = {}
            self.assertIsNone(
                excel_upstream.extract_native_client_tool_calls(
                    {"output": [raw_call(name, "cmd", "PRIVATE_COMMAND")]},
                    {"tools": [FUNCTION_TOOL]},
                    diagnostics=diagnostic,
                )
            )
            self.assertEqual(diagnostic["reason"], "unknown_tool")
            self.assertEqual(diagnostic["target_name"], name)
            self.assertEqual(diagnostic["target_source"], "raw")
            self.assertNotIn("PRIVATE_COMMAND", json.dumps(diagnostic))
        for field, metadata in (
            ("other", "{}"),
            ("cmd", "[]"),
            ("cmd", '{"cmd":"different"}'),
        ):
            native = raw_call("absent", field, "PRIVATE_COMMAND")
            args = json.loads(native["arguments"])
            args["extended_summary"] = metadata
            native["arguments"] = json.dumps(args)
            diagnostic = {}
            self.assertIsNone(
                excel_upstream.extract_native_client_tool_calls(
                    {"output": [native]},
                    {"tools": [FUNCTION_TOOL]},
                    diagnostics=diagnostic,
                )
            )
            self.assertEqual(diagnostic["reason"], "invalid_transport_envelope")

    def test_duplicate_source_metadata_must_be_identical(self):
        same = self.convert(
            raw_call("exec_command", "cmd", COMMAND, {"cmd": COMMAND}), [FUNCTION_TOOL]
        )
        self.assertIsNotNone(same)
        self.assertEqual(json.loads(same["arguments"])["cmd"], COMMAND)
        for duplicate in ("different", 1, None):
            self.assertIsNone(
                self.convert(
                    raw_call("exec_command", "cmd", COMMAND, {"cmd": duplicate}),
                    [FUNCTION_TOOL],
                )
            )

    def test_invalid_metadata_and_custom_extra_fields_fail_closed(self):
        for metadata in ("not JSON", "[]", "{} {}", 7):
            native = raw_call("exec_command", "cmd", COMMAND)
            args = json.loads(native["arguments"])
            args["extended_summary"] = metadata
            native["arguments"] = json.dumps(args)
            self.assertIsNone(self.convert(native, [FUNCTION_TOOL]))
        self.assertIsNone(
            self.convert(
                raw_call("apply_patch", "input", PATCH_TEXT, {"extra": 1}),
                [CUSTOM_TOOL],
            )
        )

    def test_raw_calls_still_validate_schema_and_whole_batch(self):
        valid = raw_call("exec_command", "cmd", COMMAND)
        invalid = raw_call("exec_command", "cmd", COMMAND, {"undeclared": True})
        invalid.update(id="fc_bad", call_id="call_bad")
        with patch.object(excel_tool_transport, "_remember_native_calls") as remember:
            self.assertIsNone(
                excel_upstream.extract_native_client_tool_calls(
                    {"output": [valid, invalid]},
                    {"tools": [FUNCTION_TOOL]},
                )
            )
        remember.assert_not_called()

    def test_native_raw_identity_replays_after_memory_reset(self):
        for tool, field, text in (
            (FUNCTION_TOOL, "cmd", COMMAND),
            (CUSTOM_TOOL, "input", PATCH_TEXT),
        ):
            with self.subTest(field=field):
                native = raw_call(tool["name"], field, text)
                native["call_id"] = "call_raw_" + field
                call = excel_upstream.extract_native_client_tool_call(
                    {"output": [native]}, {"tools": [tool]}
                )
                self.assertIsNotNone(call)
                with excel_tool_history._native_call_cache_lock:
                    excel_tool_history._native_call_cache.clear()
                self.assertEqual(
                    excel_upstream.translate_input_items(
                        [call], {tool["name"]: tool["type"]}
                    ),
                    [native],
                )
                changed = dict(call)
                if field == "input":
                    changed["input"] = text + "changed"
                else:
                    changed["arguments"] = json.dumps({field: text + "changed"})
                self.assertNotEqual(
                    excel_upstream.translate_input_items([changed]), [native]
                )

    def test_prompt_advertises_only_eligible_raw_tools_and_keeps_json_fallback(self):
        source = {
            "tools": [FUNCTION_TOOL, CUSTOM_TOOL, {"type": "function", "name": "other"}]
        }
        instructions = excel_upstream._client_tool_protocol_instructions(source)
        self.assertIn("excel-proxy.raw/", instructions)
        self.assertIn("extended_summary", instructions)
        self.assertIn("JSON-envelope", instructions)
        self.assertIn(
            "excel-proxy.raw/", excel_upstream._client_tool_protocol_reminder(source)
        )

    def test_all_protocol_prompts_prefer_raw_before_json_envelopes(self):
        source = {"tools": [FUNCTION_TOOL, CUSTOM_TOOL, CODE_TOOL]}
        prompts = {
            "initial": excel_upstream._client_tool_protocol_instructions(source),
            "reminder": excel_upstream._client_tool_protocol_reminder(source),
            "repair": excel_upstream._TRANSPORT_RETRY_GUIDANCE,
        }
        for label, prompt in prompts.items():
            with self.subTest(prompt=label):
                rule = (
                    "Use raw mode whenever the chosen catalog tool declares raw_fields"
                )
                self.assertIn(rule, prompt)
                self.assertLess(prompt.index(rule), prompt.index("JSON-envelope"))
                self.assertIn("excel-proxy.raw/FIELD/TOOL_NAME", prompt)
                self.assertIn("extended_summary", prompt)
                self.assertIn("outer function-call arguments", prompt)
                self.assertNotIn("By default put", prompt)
                self.assertNotIn("by default, the inner code value is JSON", prompt)
                self.assertNotIn(
                    '{"name":"exec_command","arguments":{"cmd":"pwd"}}', prompt
                )
        self.assertIn(
            "summary=excel-proxy.raw/input/apply_patch with code=the complete raw patch",
            prompts["reminder"],
        )


class RawHistoryTests(unittest.TestCase):
    def test_rebuilt_history_uses_catalog_raw_format_and_preserves_results(self):
        cases = [
            (FUNCTION_TOOL, "cmd", COMMAND, {}),
            (
                CODE_TOOL,
                "code",
                'await tools.exec_command({cmd: "echo quoted"});\r\n',
                {"title": "检查"},
            ),
            (CUSTOM_TOOL, "input", PATCH_TEXT, {}),
        ]
        for tool, field, text, metadata in cases:
            for namespace in (None, "plugin"):
                with self.subTest(tool=tool["name"], namespace=namespace):
                    call = {
                        "type": "custom_tool_call"
                        if tool["type"] == "custom"
                        else "function_call",
                        "id": "client_item",
                        "call_id": "call_raw_history",
                        "name": tool["name"],
                    }
                    if field == "input":
                        call["input"] = text
                    else:
                        call["arguments"] = json.dumps({field: text, **metadata})
                    if namespace:
                        call["namespace"] = namespace
                    declared = (
                        {"type": "namespace", "name": namespace, "tools": [tool]}
                        if namespace
                        else tool
                    )
                    output = {
                        "type": "custom_tool_call_output"
                        if field == "input"
                        else "function_call_output",
                        "call_id": call["call_id"],
                        "output": "recorded result",
                    }
                    source = {
                        "model": excel_upstream.MODEL_ID,
                        "tools": [declared],
                        "input": [call, output],
                    }
                    before = copy.deepcopy(source)
                    with patch.object(
                        excel_input, "_remembered_native_calls", return_value={}
                    ):
                        body = excel_upstream.prepare_responses_body(source)
                        self.assertEqual(
                            body, excel_upstream.prepare_responses_body(source)
                        )
                    native, replay_output = body["input"][-2:]
                    args = json.loads(native["arguments"])
                    name = f"{namespace}.{tool['name']}" if namespace else tool["name"]
                    self.assertEqual(args["summary"], f"excel-proxy.raw/{field}/{name}")
                    self.assertEqual(args["code"], text)
                    self.assertEqual(json.loads(args["extended_summary"]), metadata)
                    restored = excel_upstream.extract_native_client_tool_call(
                        {"output": [native]},
                        source,
                        remember=False,
                    )
                    self.assertIsNotNone(restored)
                    self.assertEqual(restored["call_id"], call["call_id"])
                    self.assertEqual(restored["name"], call["name"])
                    self.assertEqual(restored.get("namespace"), namespace)
                    if field == "input":
                        self.assertEqual(restored["input"], text)
                    else:
                        self.assertEqual(
                            json.loads(restored["arguments"]),
                            json.loads(call["arguments"]),
                        )
                    self.assertEqual(replay_output["call_id"], call["call_id"])
                    self.assertEqual(replay_output["output"], output["output"])
                    self.assertEqual(source, before)

    def test_cached_native_calls_stay_verbatim_with_raw_catalog(self):
        for native in (
            transport({"name": "exec_command", "arguments": {"cmd": COMMAND}}),
            raw_call("exec_command", "cmd", COMMAND),
        ):
            with self.subTest(summary=json.loads(native["arguments"]).get("summary")):
                native["provider_extension"] = {"preserve": True}
                call = excel_upstream.extract_native_client_tool_call(
                    {"output": [native]},
                    {"tools": [FUNCTION_TOOL]},
                    remember=False,
                )
                source = {
                    "model": excel_upstream.MODEL_ID,
                    "tools": [FUNCTION_TOOL],
                    "input": [call],
                }
                with patch.object(
                    excel_input,
                    "_remembered_native_calls",
                    return_value={call["call_id"]: native},
                ):
                    body = excel_upstream.prepare_responses_body(source)
                self.assertEqual(body["input"][-1], native)

    def test_rebuilt_history_does_not_infer_raw_fields_without_current_schema(self):
        call = {
            "type": "function_call",
            "call_id": "call_schema_history",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": COMMAND}),
        }
        for tools in (
            [],
            [{"type": "function", "name": "exec_command"}],
            [dict(FUNCTION_TOOL, name="other")],
            [dict(FUNCTION_TOOL, type="custom")],
        ):
            with self.subTest(tools=tools):
                source = {
                    "model": excel_upstream.MODEL_ID,
                    "tools": tools,
                    "input": [call],
                }
                with patch.object(
                    excel_input, "_remembered_native_calls", return_value={}
                ):
                    body = excel_upstream.prepare_responses_body(source)
                args = json.loads(body["input"][-1]["arguments"])
                self.assertFalse(args["summary"].startswith("excel-proxy.raw/"))
                self.assertEqual(
                    json.loads(args["code"]),
                    {"name": "exec_command", "arguments": {"cmd": COMMAND}},
                )

    def test_catalog_gives_exact_raw_markers_that_round_trip(self):
        source = {
            "tools": [
                {
                    "type": "namespace",
                    "name": "functions",
                    "tools": [FUNCTION_TOOL, CODE_TOOL, CUSTOM_TOOL],
                }
            ]
        }
        instructions = excel_upstream._client_tool_protocol_instructions(source)
        catalog, _ = json.JSONDecoder().raw_decode(
            instructions.split("Available client tools:\n", 1)[1]
        )
        for entry, field, text in zip(
            catalog,
            ["cmd", "code", "input"],
            [COMMAND, 'console.log("quoted");', PATCH_TEXT],
        ):
            with self.subTest(name=entry["name"]):
                recipe = entry["transport"]
                self.assertEqual(recipe["mode"], "raw")
                self.assertEqual(recipe["field"], field)
                native = transport({})
                native["arguments"] = json.dumps(
                    {
                        "summary": recipe["summary"],
                        "code": text,
                        "extended_summary": "{}",
                    }
                )
                call = excel_upstream.extract_native_client_tool_call(
                    {"output": [native]}, source, remember=False
                )
                self.assertIsNotNone(call)
                self.assertEqual(call["namespace"] + "." + call["name"], entry["name"])
                self.assertEqual(
                    call["input"]
                    if field == "input"
                    else json.loads(call["arguments"])[field],
                    text,
                )


class RawRecoveryHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_excel_contracts.ExcelHTTPContractTests.asyncSetUp
    serve = test_excel_call_diagnostics.ToolRecoveryHTTPTests.serve
    request = test_excel_call_diagnostics.ToolRecoveryHTTPTests.request

    async def test_unknown_raw_first_call_uses_catalog_regeneration(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                self.serve(
                    [
                        [raw_call("absent", "cmd", "PRIVATE_COMMAND")],
                        [raw_call("exec_command", "cmd", "echo checked")],
                    ]
                )
                result = await self.request(stream)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertIn("echo checked", result.text)
                self.assertEqual(len(self.requests), 2)
                retry = json.loads(self.requests[1].content)
                self.assertNotIn("PRIVATE_COMMAND", json.dumps(retry))
                self.assertEqual(retry["input"][-1]["role"], "developer")
                trace = self.finish.call_args.args[0].trace_context
                self.assertEqual(
                    trace["tool_call_diagnostics"]["target_name"], "absent"
                )
                self.assertEqual(
                    trace["tool_call_recovery"]["mode"], "unknown_tool_regeneration"
                )

    async def test_column_163_command_uses_raw_without_correction_or_disconnect(self):
        prefix = '{"name":"exec_command","arguments":{"cmd":"'
        padding = 163 - len(prefix) - len("Write-Output ''; Write-Output ") - 2
        command = (
            "Write-Output '"
            + "x" * padding
            + "'; Write-Output "
            + '"中文"; $p = '
            + r"'D:\Work\ghcp_proxy'"
            + "\r\nWrite-Output $p\r\n"
        )
        diagnostics = {}
        self.assertIsNone(
            excel_upstream.extract_native_client_tool_call(
                {"output": [unframed_call(prefix + command + '"}}')]},
                {"tools": [FUNCTION_TOOL]},
                diagnostics=diagnostics,
                remember=False,
            )
        )
        self.assertEqual(diagnostics["reason"], "invalid_transport_envelope")
        self.assertEqual(diagnostics["transport_field"], "arguments.code")
        self.assertEqual(
            (diagnostics["json_line"], diagnostics["json_column"]), (1, 163)
        )
        self.assertEqual(diagnostics["json_error"], "missing_comma")
        for stream in (False, True):
            for sse in (True,) if stream else (False, True):
                with self.subTest(stream=stream, upstream_sse=sse):
                    self.requests.clear()
                    native = raw_call("exec_command", "cmd", command)
                    self.serve([[native]], sse=sse)
                    result = await self.request(stream)
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(len(self.requests), 1)
                    self.assertNotIn(
                        "tool_call_recovery",
                        self.finish.call_args.args[0].trace_context,
                    )
                    if stream:
                        self.assertEqual(
                            result.text.count("event: response.completed"), 1
                        )
                        self.assertNotIn("event: response.failed", result.text)
                        events = [
                            json.loads(line[6:])
                            for line in result.text.splitlines()
                            if line.startswith("data: {")
                        ]
                        payload = next(
                            event["response"]
                            for event in events
                            if event["type"] == "response.completed"
                        )
                    else:
                        payload = result.json()
                    calls = [
                        item
                        for item in payload["output"]
                        if item["type"] == "function_call"
                    ]
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(
                        json.loads(calls[0]["arguments"]), {"cmd": command}
                    )
                    self.assertEqual(
                        excel_upstream.translate_input_items(calls), [native]
                    )

    async def test_lossless_recovery_for_json_and_streaming_clients(self):
        for stream in (False, True):
            for marked in (False, True):
                with self.subTest(stream=stream, marked=marked):
                    self.requests.clear()
                    corrected = (
                        raw_call("exec_command", "cmd", COMMAND)
                        if marked
                        else transport(
                            {"name": "exec_command", "arguments": {"cmd": COMMAND}}
                        )
                    )
                    self.serve([[unframed_call(COMMAND)], [corrected]])
                    result = await self.request(stream)
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(len(self.requests), 2)
                    recovery = self.finish.call_args.args[0].trace_context[
                        "tool_call_recovery"
                    ]
                    self.assertEqual(recovery["outcome"], "succeeded")
                    self.assertEqual(recovery["usage"]["total_tokens"], 26)
                    if stream:
                        self.assertEqual(
                            result.text.count("event: response.completed"), 1
                        )
                        self.assertNotIn("event: response.failed", result.text)
                    else:
                        self.assertEqual(
                            json.loads(result.json()["output"][0]["arguments"])["cmd"],
                            COMMAND,
                        )

    async def test_correction_cannot_replace_source_text_or_cache_failed_batch(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.requests.clear()
                good = transport(
                    {"name": "exec_command", "arguments": {"cmd": "echo sibling"}}
                )
                good.update(id="fc_sibling", call_id="call_sibling")
                self.serve(
                    [
                        [good, unframed_call(COMMAND)],
                        [raw_call("exec_command", "cmd", "echo changed")],
                    ]
                )
                with patch.object(
                    excel_tool_transport, "_remember_native_calls"
                ) as remember:
                    result = await self.request(stream)
                remember.assert_not_called()
                self.assertIn("excel_untranslatable_tool_call", result.text)
                self.assertNotIn("echo changed", result.text)
                self.assertNotIn("echo sibling", result.text)
                recovery = self.finish.call_args.args[0].trace_context[
                    "tool_call_recovery"
                ]
                self.assertEqual(recovery["outcome"], "rejected")
                self.assertEqual(
                    recovery["repair_diagnostics"]["reason"], "repair_changed_input"
                )
                self.assertEqual(recovery["usage"]["total_tokens"], 26)

    def test_equivalence_requires_same_target_and_all_metadata(self):
        second = dict(FUNCTION_TOOL, name="other.exec_command")
        source = {"tools": [FUNCTION_TOOL, second]}
        original = raw_call("exec_command", "cmd", COMMAND, {"timeout": 30})
        for replacement in (
            raw_call("other.exec_command", "cmd", COMMAND),
            raw_call("exec_command", "cmd", COMMAND),
        ):
            self.assertFalse(
                excel_upstream.tool_call_repair_preserves_input(
                    original, replacement, source
                )
            )
        direct = transport({"name": "exec_command", "arguments": {"cmd": COMMAND}})
        self.assertTrue(
            excel_upstream.tool_call_repair_preserves_input(
                raw_call("exec_command", "cmd", COMMAND), direct, source
            )
        )
        malformed = '{"name":"exec_command","arguments":{"cmd":"echo "broken""}}'
        self.assertFalse(
            excel_upstream.tool_call_repair_preserves_input(
                unframed_call(malformed),
                raw_call("exec_command", "cmd", malformed),
                source,
            )
        )
