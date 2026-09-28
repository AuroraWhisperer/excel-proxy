"""Regression guards for independently importable shared foundations."""

import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1] / "app"


def imported_modules(name):
    tree = ast.parse((APP / f"{name}.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.models = importlib.import_module("excel_models")

    def test_model_catalog_preserves_aliases_and_limits(self):
        expected = {
            "gpt-6-astra-excel": ("gpt-6-astra", 272_000, 232_200),
            "gpt-5.6-luna-excel": ("gpt-5.6-luna", 200_000, 180_000),
            "gpt-5.6-terra-excel": ("gpt-5.6-terra", 272_000, 240_000),
            "gpt-5.6-sol-excel": ("gpt-5.6-sol", 272_000, 240_000),
            "gpt-6-sol-excel": ("gpt-6-sol", 272_000, 240_000),
            "gpt-6-luna-excel": ("gpt-6-luna", 272_000, 240_000),
        }
        self.assertEqual(self.models.MODEL_IDS, tuple(expected))
        self.assertEqual(self.models.MODEL_ID, "gpt-5.6-sol-excel")
        for model, (upstream, context, compact) in expected.items():
            with self.subTest(model=model):
                self.assertEqual(self.models.EXCEL_MODEL_UPSTREAMS[model], upstream)
                self.assertEqual(
                    self.models.excel_model_id(f" {model.upper()} "), model
                )
                self.assertEqual(
                    self.models.LOCAL_MODEL_CAPABILITIES[model]["context_window"],
                    context,
                )
                self.assertEqual(
                    self.models.LOCAL_MODEL_CAPABILITIES[model][
                        "auto_compact_token_limit"
                    ],
                    compact,
                )
        for invalid in (None, {}, "gpt-excel", "gpt-6-astra"):
            self.assertFalse(self.models.is_excel_model(invalid))

    def test_reasoning_normalization_stays_model_specific(self):
        for alias in ("xhigh", "x-high", "extra-high", "extra_high", " XHIGH "):
            self.assertEqual(self.models.normalize_reasoning_effort(alias), "xhigh")
        self.assertIsNone(
            self.models.normalize_reasoning_effort("low", "gpt-6-astra-excel")
        )
        self.assertEqual(
            self.models.normalize_reasoning_effort("low", "gpt-5.6-sol-excel"), "low"
        )
        for invalid in (None, 123, "max"):
            self.assertIsNone(self.models.normalize_reasoning_effort(invalid))

    def test_upstream_keeps_compatible_public_catalog_exports(self):
        import excel_upstream

        for name in (
            "EXCEL_MODEL_UPSTREAMS",
            "MODEL_IDS",
            "MODEL_ID",
            "LOCAL_MODEL_CAPABILITIES",
            "is_excel_model",
            "excel_model_id",
            "upstream_model_for",
            "local_model_payload",
            "merge_local_models_payload",
            "merge_local_model_capabilities",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(excel_upstream, name), getattr(self.models, name))

    def test_model_override_does_not_load_upstream_or_credentials(self):
        code = (
            "import json, sys; import excel_models as models; "
            "print(json.dumps([models.upstream_model_for('gpt-6-astra-excel'), "
            "models.UPSTREAM_MODEL, sorted(sys.modules)]))"
        )
        environment = {
            **os.environ,
            "GHCP_EXCEL_UPSTREAM_MODEL": " test-override ",
            "PYTHONPATH": str(APP),
        }
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        selected, default, modules = json.loads(result.stdout)
        self.assertEqual((selected, default), ("test-override", "test-override"))
        self.assertTrue(
            {"excel_upstream", "account_balances", "windows_dpapi", "util"}.isdisjoint(
                modules
            )
        )


class ModuleBoundaryTests(unittest.TestCase):
    def test_usage_record_and_capture_owners_load_without_runtime_services(self):
        forbidden = {
            "proxy",
            "dashboard",
            "usage_tracking",
            "usage_storage",
            "excel_upstream",
        }
        for name in ("usage_capture", "usage_records", "request_trace_storage"):
            with self.subTest(module=name):
                self.assertTrue(forbidden.isdisjoint(imported_modules(name)))
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import usage_capture, usage_records, request_trace_storage, json, sys; "
                "print(json.dumps(sorted(sys.modules)))",
            ],
            env={**os.environ, "PYTHONPATH": str(APP)},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(forbidden.isdisjoint(json.loads(result.stdout)))

    def test_usage_tracker_uses_shared_capture_and_record_implementations(self):
        import usage_capture
        import usage_records
        import usage_tracking

        self.assertIs(usage_tracking.SSEUsageCapture, usage_capture.SSEUsageCapture)
        self.assertIsInstance(
            usage_tracking.UsageTracker().create_sse_capture("responses"),
            usage_capture.SSEUsageCapture,
        )
        for name in ("_normalize_recorded_usage_event", "_usage_event_archive_summary"):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(usage_tracking, name), getattr(usage_records, name)
                )

    def test_tool_and_input_owners_do_not_import_request_or_runtime_services(self):
        forbidden = {"excel_upstream", "excel_responses", "proxy", "dashboard"}
        for name in (
            "excel_tool_catalog",
            "excel_tool_transport",
            "excel_tool_recovery",
            "excel_input",
            "responses_input",
            "responses_compaction",
        ):
            with self.subTest(module=name):
                self.assertTrue(forbidden.isdisjoint(imported_modules(name)))
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import excel_tool_catalog, responses_compaction, json, sys; "
                "print(json.dumps(sorted(sys.modules)))",
            ],
            env={**os.environ, "PYTHONPATH": str(APP)},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(
            (
                forbidden | {"excel_session", "excel_tool_history", "tool_schema"}
            ).isdisjoint(json.loads(result.stdout))
        )

    def test_upstream_keeps_tool_and_replay_entry_points(self):
        import excel_upstream

        owners = {
            "excel_tool_catalog": ("client_tool_types", "relay_tool_name"),
            "excel_tool_transport": (
                "extract_client_tool_call",
                "extract_native_client_tool_call",
                "extract_native_client_tool_calls",
                "response_payload_with_tool_call",
                "response_payload_with_tool_calls",
            ),
            "excel_tool_recovery": (
                "tool_call_failure_message",
                "tool_call_repair_request",
                "tool_call_repair_preserves_input",
                "unknown_tool_regeneration_request",
            ),
            "excel_input": ("translate_input_items",),
        }
        for owner, names in owners.items():
            module = importlib.import_module(owner)
            for name in names:
                with self.subTest(owner=owner, name=name):
                    self.assertIs(getattr(excel_upstream, name), getattr(module, name))

    def test_protocol_keeps_input_and_compaction_entry_points(self):
        import responses_protocol

        for owner, names in {
            "responses_input": (
                "encode_fake_compaction",
                "decode_fake_compaction",
                "input_contains_compaction",
                "sanitize_input",
            ),
            "responses_compaction": (
                "build_fake_compaction_request",
                "responses_to_compaction_response",
            ),
            "util": ("extract_response_output_text",),
        }.items():
            module = importlib.import_module(owner)
            for name in names:
                with self.subTest(owner=owner, name=name):
                    self.assertIs(
                        getattr(responses_protocol, name), getattr(module, name)
                    )

    def test_application_static_import_graph_has_no_cycles(self):
        modules = {path.stem for path in APP.glob("*.py")}
        graph = {name: imported_modules(name) & modules for name in modules}
        completed = set()

        def visit(name, path):
            self.assertNotIn(name, path, " -> ".join([*path, name]))
            if name in completed:
                return
            for dependency in sorted(graph[name]):
                visit(dependency, [*path, name])
            completed.add(name)

        for name in sorted(modules):
            visit(name, [])

    def test_session_and_history_owners_do_not_import_the_translator(self):
        for name in ("excel_session", "excel_tool_history", "account_identity"):
            with self.subTest(module=name):
                dependencies = imported_modules(name)
                self.assertTrue(
                    {"proxy", "excel_upstream", "account_balances"}.isdisjoint(
                        dependencies
                    )
                )

    def test_account_consumers_do_not_import_private_quota_helpers(self):
        for name in ("account_login", "account_login_browser", "proxy_accounts"):
            tree = ast.parse((APP / f"{name}.py").read_text(encoding="utf-8"))
            names = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module == "account_balances"
                for alias in node.names
            }
            with self.subTest(module=name):
                self.assertTrue(
                    {
                        "BalanceError",
                        "MAX_BYTES",
                        "MAX_ACCOUNTS",
                        "_claims",
                        "_account_email",
                        "_normalize_account",
                        "account_session_headers",
                    }.isdisjoint(names)
                )

    def test_foundations_do_not_import_application_modules(self):
        local_modules = {path.stem for path in APP.glob("*.py")}
        for name in ("excel_models", "windows_dpapi"):
            with self.subTest(module=name):
                self.assertFalse(imported_modules(name) & local_modules)

    def test_model_only_consumers_do_not_depend_on_upstream(self):
        for name in ("usage_metrics", "dashboard", "codex_config"):
            with self.subTest(module=name):
                dependencies = imported_modules(name)
                self.assertNotIn("excel_upstream", dependencies)
                self.assertIn("excel_models", dependencies)

    def test_generic_utilities_do_not_import_usage_or_model_rules(self):
        self.assertTrue(
            {"usage_metrics", "excel_models", "excel_upstream"}.isdisjoint(
                imported_modules("util")
            )
        )

    def test_dashboard_calculations_do_not_depend_on_runtime_services(self):
        forbidden = {
            "dashboard",
            "proxy",
            "usage_tracking",
            "usage_storage",
            "responses_stream",
        }
        for name in ("usage_aggregation", "api_cost_estimates"):
            with self.subTest(module=name):
                self.assertTrue(forbidden.isdisjoint(imported_modules(name)))
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import usage_aggregation, api_cost_estimates, json, sys; print(json.dumps(sorted(sys.modules)))",
            ],
            env={**os.environ, "PYTHONPATH": str(APP)},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(forbidden.isdisjoint(json.loads(result.stdout)))

    def test_dashboard_keeps_consumed_calculation_exports(self):
        import dashboard
        import usage_aggregation
        import api_cost_estimates

        self.assertIs(
            dashboard._prepare_usage_event, usage_aggregation.prepare_usage_event
        )
        self.assertIs(
            dashboard._build_api_cost_estimate,
            api_cost_estimates.build_api_cost_estimate,
        )
        self.assertIs(
            dashboard.attach_account_cycle_estimates,
            api_cost_estimates.attach_account_cycle_estimates,
        )

    def test_excel_response_owner_does_not_import_the_application(self):
        self.assertNotIn("proxy", imported_modules("excel_responses"))
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import excel_responses, json, sys; print(json.dumps(sorted(sys.modules)))",
            ],
            env={**os.environ, "PYTHONPATH": str(APP)},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(
            {"proxy", "dashboard", "account_routes", "dashboard_routes"}.isdisjoint(
                json.loads(result.stdout)
            )
        )

    def test_http_client_has_no_business_service_dependencies(self):
        local_modules = {path.stem for path in APP.glob("*.py")}
        self.assertEqual(
            imported_modules("upstream_client") & local_modules, {"constants"}
        )
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import upstream_client, json, sys; print(json.dumps(sorted(sys.modules)))",
            ],
            env={**os.environ, "PYTHONPATH": str(APP)},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(
            {
                "proxy",
                "excel_upstream",
                "excel_responses",
                "responses_stream",
            }.isdisjoint(json.loads(result.stdout))
        )

    def test_proxy_wires_shared_client_and_response_owner(self):
        import excel_responses
        import proxy
        import upstream_client

        self.assertIs(
            proxy._get_excel_upstream_client, upstream_client.get_excel_upstream_client
        )
        processor = proxy._excel_response_processor()
        self.assertIsInstance(processor, excel_responses.ExcelResponseProcessor)
        self.assertIs(
            processor.get_upstream_client, upstream_client.get_excel_upstream_client
        )
        self.assertIs(processor.usage_tracker, proxy.usage_tracker)

    def test_accounts_use_shared_encryption_owner(self):
        for name in ("account_balances", "proxy_accounts"):
            tree = ast.parse((APP / f"{name}.py").read_text(encoding="utf-8"))
            upstream_names = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "excel_upstream"
                for alias in node.names
            }
            with self.subTest(module=name):
                self.assertTrue(
                    {"_protect_windows_data", "_unprotect_windows_data"}.isdisjoint(
                        upstream_names
                    )
                )
                self.assertIn("windows_dpapi", imported_modules(name))


class AccountAndSessionBoundaryTests(unittest.TestCase):
    def test_translator_reuses_the_history_owner(self):
        import excel_tool_history
        import excel_upstream

        for name in (
            "remember_native_call",
            "remember_native_calls",
            "remembered_native_calls",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(excel_upstream, "_" + name),
                    getattr(excel_tool_history, name),
                )

    def test_shared_identity_preserves_credential_store_keys(self):
        identity = importlib.import_module("account_identity")
        account = {
            "access_token": "synthetic-first",
            "account_id": "account-1",
            "user_id": "user-1",
            "email": "test@example.com",
        }
        first_key, first = identity.normalize_account(account)
        second_key, second = identity.normalize_account(
            {**account, "access_token": "synthetic-second"}
        )
        other_key, _ = identity.normalize_account({**account, "user_id": "user-2"})
        self.assertEqual(first_key, second_key)
        self.assertNotEqual(first_key, other_key)
        self.assertEqual(first["name"], "test@example.com")
        self.assertEqual(second["access_token"], "synthetic-second")
        self.assertEqual(
            identity.account_session_headers(first)["authorization"],
            "Bearer synthetic-first",
        )
        self.assertEqual(
            identity.account_session_headers(first, stream=True)["accept"],
            "text/event-stream",
        )

    def test_legacy_error_and_session_exports_are_same_objects(self):
        import account_balances
        import excel_upstream
        import proxy_accounts

        identity = importlib.import_module("account_identity")
        sessions = importlib.import_module("excel_session")
        self.assertIs(account_balances.BalanceError, identity.BalanceError)
        self.assertIs(proxy_accounts.BalanceError, identity.BalanceError)
        self.assertIs(excel_upstream.ExcelSessionStore, sessions.ExcelSessionStore)
        self.assertIs(excel_upstream.excel_session_store, sessions.excel_session_store)
        self.assertIs(account_balances._normalize_account, identity.normalize_account)

    def test_session_import_does_not_load_request_processing(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import excel_session, json, sys; print(json.dumps(sorted(sys.modules)))",
            ],
            env={**os.environ, "PYTHONPATH": str(APP)},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(
            {"excel_upstream", "proxy", "responses_protocol", "tool_schema"}.isdisjoint(
                json.loads(result.stdout)
            )
        )


class WindowsDpapiTests(unittest.TestCase):
    def setUp(self):
        self.dpapi = importlib.import_module("windows_dpapi")

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI integration")
    def test_round_trip_binary_data_without_credentials_or_files(self):
        for payload in (
            b"",
            b"synthetic-session",
            bytes(range(256)),
            "synthetic-\u6d4b\u8bd5".encode(),
        ):
            with self.subTest(size=len(payload)):
                protected = self.dpapi.protect_data(payload)
                self.assertNotEqual(protected, payload)
                self.assertEqual(self.dpapi.unprotect_data(protected), payload)

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI integration")
    def test_invalid_ciphertext_keeps_os_error(self):
        with self.assertRaises(OSError):
            self.dpapi.unprotect_data(b"not-an-encrypted-blob")

    def test_non_windows_keeps_explicit_failure(self):
        with patch.object(self.dpapi.sys, "platform", "linux"):
            for operation in (self.dpapi.protect_data, self.dpapi.unprotect_data):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaisesRegex(RuntimeError, "requires Windows DPAPI"):
                        operation(b"synthetic-session")
