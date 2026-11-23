import unittest
from contextlib import redirect_stdout
import io
import os
import json
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import excel_upstream
import excel_models
from constants import CODEX_PROXY_CONFIG
from codex_config import ProxyClientConfig, ProxyClientConfigService


class ExcelConfigLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.primary = self.root / "config.toml"
        self.managed = self.root / "managed_config.toml"
        self.catalog = self.root / "models.json"
        self.settings = self.root / "client-proxy.json"
        self.service = ProxyClientConfigService(
            ProxyClientConfig(
                codex_primary_config_file=str(self.primary),
                codex_managed_config_file=str(self.managed),
                codex_model_catalog_file=str(self.catalog),
                codex_proxy_config=CODEX_PROXY_CONFIG,
                codex_model_context_window=272000,
                codex_model_auto_compact_token_limit=240000,
                client_proxy_settings_file=str(self.settings),
            )
        )

    def test_image_header_is_added_without_losing_custom_headers(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                original = "[model_providers.custom]" + chr(10)
                if nested:
                    original += (
                        "[model_providers.custom.http_headers]"
                        + chr(10)
                        + '"X-Local" = "keep"'
                    )
                else:
                    original += 'http_headers = { "X-Local" = "keep" }'
                merged = self.service._merged_codex_primary_config(original)
                provider = tomllib.loads(merged)["model_providers"]["custom"]
                self.assertEqual(provider["http_headers"]["X-Local"], "keep")
                self.assertEqual(
                    provider["http_headers"]["x-openai-actor-authorization"],
                    "excel-proxy",
                )
                self.assertEqual(
                    tomllib.loads(self.service._merged_codex_primary_config(merged)),
                    tomllib.loads(merged),
                )
        managed = tomllib.loads(self.service._render_codex_proxy_config())
        self.assertEqual(
            managed["model_providers"]["custom"]["http_headers"],
            {"x-openai-actor-authorization": "excel-proxy"},
        )

    def test_enable_refresh_disable_preserves_original_configuration(self):
        original = 'model = "old-model"\nmodel_provider = "openai"\n[features]\nexample = true\n'
        self.primary.write_text(original, encoding="utf-8")
        result = self.service.write_codex_proxy_config()
        self.assertTrue(result["configured"])
        parsed = tomllib.loads(self.primary.read_text(encoding="utf-8"))
        self.assertEqual(parsed["model"], excel_models.UPSTREAM_MODEL)
        self.assertTrue(parsed["features"]["example"])
        self.assertFalse(self.catalog.exists())
        for content in (parsed, tomllib.loads(self.managed.read_text(encoding="utf-8"))):
            for key in ("model_catalog_json", "model_context_window", "model_auto_compact_token_limit"):
                self.assertNotIn(key, content)
        backups = set(self.root.glob("*.bak.*"))
        primary = self.primary.read_bytes()
        managed = self.managed.read_bytes()
        self.service.write_codex_proxy_config()
        self.assertEqual(set(self.root.glob("*.bak.*")), backups)
        self.assertEqual(self.primary.read_bytes(), primary)
        self.assertEqual(self.managed.read_bytes(), managed)
        self.service.disable_codex_proxy_config()
        self.assertEqual(self.primary.read_text(encoding="utf-8"), original)
        self.assertFalse(self.catalog.exists())

    def test_startup_refresh_migrates_owned_references_without_rewriting_catalog(self):
        legacy = (
            f"model_catalog_json = {json.dumps(str(self.catalog))}\n"
            "model_context_window = 272000\n"
            "model_auto_compact_token_limit = 240000\n"
            + CODEX_PROXY_CONFIG.replace("gpt-5.6-sol-excel", "gpt-6-astra-excel")
        )
        self.primary.write_text(legacy, encoding="utf-8")
        self.managed.write_text(legacy, encoding="utf-8")
        self.catalog.write_text("legacy catalog remains untouched", encoding="utf-8")
        self.assertEqual(self.service.refresh_client_model_metadata(), {"codex": True})
        for path in (self.primary, self.managed):
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["model"], "gpt-6-astra")
            self.assertNotIn("model_catalog_json", parsed)
            backups = list(self.root.glob(f"{path.name}.ghcp-proxy.migration.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), legacy)
            self.assertIsNone(self.service._latest_backup_path(str(path)))
        primary = tomllib.loads(self.primary.read_text(encoding="utf-8"))
        self.assertEqual(primary["model_context_window"], 272000)
        managed = tomllib.loads(self.managed.read_text(encoding="utf-8"))
        self.assertNotIn("model_context_window", managed)
        self.assertNotIn("model_auto_compact_token_limit", managed)
        self.assertTrue(self.service.codex_proxy_status()["configured"])
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(self.service.refresh_client_model_metadata(), {"codex": False})
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_existing_proxy_upgrade_replaces_old_model_and_keeps_backup(self):
        self.primary.write_text('model = "old-model"\n', encoding="utf-8")
        self.service.write_codex_proxy_config()
        backups = set(self.root.glob("*.bak.*"))
        current = self.primary.read_text(encoding="utf-8").replace(
            excel_models.UPSTREAM_MODEL, "old-model"
        )
        self.primary.write_text(current, encoding="utf-8")
        self.service.write_codex_proxy_config()
        self.assertEqual(
            tomllib.loads(self.primary.read_text(encoding="utf-8"))["model"],
            excel_models.UPSTREAM_MODEL,
        )
        self.assertEqual(set(self.root.glob("*.bak.*")), backups)

    def test_shutdown_and_startup_restore_only_codex(self):
        self.primary.write_text('model = "original"\n', encoding="utf-8")
        self.service.write_codex_proxy_config()
        result = self.service.revert_proxy_configs_on_shutdown()
        self.assertEqual(set(result["clients"]), {"codex"})
        self.assertEqual(
            self.primary.read_text(encoding="utf-8"), 'model = "original"\n'
        )
        self.assertTrue(self.service.restore_proxy_configs_on_startup()["restored"])
        self.settings.write_text(
            json.dumps({"pending_restore_targets": ["claude"]}), encoding="utf-8"
        )
        self.assertEqual(
            self.service.load_client_proxy_settings()["pending_restore_targets"], []
        )

    def test_install_and_repair_preserve_custom_catalog_settings_and_selected_model(self):
        custom_catalog = self.root / "custom-models.json"
        custom_catalog.write_text("user catalog", encoding="utf-8")
        original = (
            'model = "gpt-6-astra-excel"\n'
            f"model_catalog_json = {json.dumps(str(custom_catalog))}\n"
            'model_verbosity = "high"\n'
            'base_instructions = """custom instructions\n[not a section]\nmodel = "text"\n"""\n'
            'approvals_reviewer = "auto"\n'
            'truncation_policy = { mode = "tokens", limit = 55555 }\n'
            "model_context_window = 123456\n"
            "model_auto_compact_token_limit = 100000\n"
        )
        self.primary.write_text(original, encoding="utf-8")
        self.service.write_codex_proxy_config()
        managed = self.managed.read_text(encoding="utf-8") + '\n[features]\ncustom_feature = true\n'
        self.managed.write_text(managed, encoding="utf-8")
        backups = {path.name: path.read_bytes() for path in self.root.glob("*.bak.*")}
        self.service.write_codex_proxy_config()
        primary = tomllib.loads(self.primary.read_text(encoding="utf-8"))
        for key, value in tomllib.loads(original).items():
            self.assertEqual(primary[key], "gpt-6-astra" if key == "model" else value)
        repaired = tomllib.loads(self.managed.read_text(encoding="utf-8"))
        self.assertEqual(repaired["model"], "gpt-6-astra")
        self.assertTrue(repaired["features"]["custom_feature"])
        self.assertEqual({path.name: path.read_bytes() for path in self.root.glob("*.bak.*")}, backups)
        self.assertEqual(custom_catalog.read_text(encoding="utf-8"), "user catalog")
        self.assertFalse(self.catalog.exists())

    def test_fresh_install_uses_bare_model_without_a_catalog(self):
        result = self.service.write_codex_proxy_config()
        self.assertTrue(result["configured"])
        managed = tomllib.loads(self.managed.read_text(encoding="utf-8"))
        self.assertEqual(managed["model"], excel_models.UPSTREAM_MODEL)
        for key in ("model_catalog_json", "base_instructions", "model_verbosity", "truncation_policy"):
            self.assertNotIn(key, managed)
        self.assertFalse(self.catalog.exists())

    def test_supported_bare_model_choice_survives_installation(self):
        for model in excel_models.EXCEL_MODEL_UPSTREAMS.values():
            with self.subTest(model=model):
                merged = self.service._merged_codex_primary_config(f"model = {json.dumps(model)}\n")
                self.assertEqual(tomllib.loads(merged)["model"], model)

    def test_migration_retains_custom_managed_limits_and_existing_backups(self):
        original = (
            f"model_catalog_json = {json.dumps(str(self.catalog))}\n"
            "model_context_window = 500000\n"
            "model_auto_compact_token_limit = 450000\n"
            + CODEX_PROXY_CONFIG
        )
        self.managed.write_text(original, encoding="utf-8")
        backup = Path(self.service._backup_config_file(str(self.managed)))
        before = backup.read_bytes()
        self.assertTrue(self.service.refresh_codex_model_catalog())
        managed = tomllib.loads(self.managed.read_text(encoding="utf-8"))
        self.assertEqual(managed["model_context_window"], 500000)
        self.assertEqual(managed["model_auto_compact_token_limit"], 450000)
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.bak.*")), [backup])

    def test_migration_backup_is_not_restored_as_an_original_config(self):
        legacy = f"model_catalog_json = {json.dumps(str(self.catalog))}\n" + CODEX_PROXY_CONFIG
        self.managed.write_text(legacy, encoding="utf-8")
        self.assertTrue(self.service.refresh_codex_model_catalog())
        backup = next(self.root.glob("*.migration.bak.*"))
        self.assertIsNone(self.service._latest_backup_path(str(self.managed)))
        self.assertFalse(self.service.disable_codex_proxy_config()["configured"])
        self.assertFalse(self.managed.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), legacy)

    def test_new_install_backs_up_current_user_config_without_overwriting_old_backup(self):
        self.primary.write_text('model = "user-old"\n', encoding="utf-8")
        old_backup = Path(self.service._backup_config_file(str(self.primary)))
        os.utime(old_backup, (1, 1))
        original = 'model = "user-now"\n'
        self.primary.write_text(original, encoding="utf-8")
        self.service.write_codex_proxy_config()
        self.assertEqual(len(list(self.root.glob("*.ghcp-proxy.bak.*"))), 2)
        self.service.disable_codex_proxy_config()
        self.assertEqual(self.primary.read_text(encoding="utf-8"), original)
        self.assertEqual(old_backup.read_text(encoding="utf-8"), 'model = "user-old"\n')

    def test_startup_refresh_leaves_unrelated_configuration_unchanged(self):
        original = 'model = "user-model"\nmodel_catalog_json = "custom.json"\n'
        self.primary.write_text(original, encoding="utf-8")
        self.managed.write_text(original, encoding="utf-8")
        self.catalog.write_text("old unused file", encoding="utf-8")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(self.service.refresh_client_model_metadata(), {"codex": False})
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_invalid_existing_config_is_preserved_during_startup_and_install(self):
        self.primary.write_text("model = [", encoding="utf-8")
        original = self.primary.read_bytes()
        diagnostic = io.StringIO()
        with redirect_stdout(diagnostic):
            self.assertEqual(self.service.refresh_client_model_metadata(), {"codex": False})
        self.assertIn("could not migrate", diagnostic.getvalue())
        result = self.service.write_codex_proxy_config()
        self.assertTrue(result["error"])
        self.assertEqual(self.primary.read_bytes(), original)
        self.assertFalse(self.managed.exists())
        self.assertFalse(list(self.root.glob("*.bak.*")))

    def test_provider_comments_and_literal_dotted_names_are_preserved(self):
        original = (
            '[model_providers."custom"] # own provider\n'
            'http_headers = { "X-Local" = "keep" }\n'
            '[model_providers."custom.http_headers"]\n'
            'name = "different provider"\n'
        )
        parsed = tomllib.loads(self.service._merged_codex_primary_config(original))
        self.assertEqual(parsed["model_providers"]["custom.http_headers"], {"name": "different provider"})
        self.assertEqual(parsed["model_providers"]["custom"]["http_headers"]["X-Local"], "keep")

    def test_disabling_keeps_the_unused_legacy_catalog_file(self):
        self.primary.write_text('model = "original"\n', encoding="utf-8")
        self.service.write_codex_proxy_config()
        self.catalog.write_text("old unused file", encoding="utf-8")
        self.service.disable_codex_proxy_config()
        self.assertEqual(self.catalog.read_text(encoding="utf-8"), "old unused file")
        self.assertFalse(self.service.codex_proxy_status()["configured"])
        self.assertFalse(self.service.codex_proxy_status()["exists"])


class ReasoningLevelTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(ProxyClientConfigService)

    def _effort_names(self, model_name, raw_efforts):
        levels, _ = self.service._resolve_reasoning_levels(
            "gpt", raw_efforts, model_name=model_name
        )
        return [level["effort"] for level in levels]

    def test_excel_models_never_expose_max(self):
        raw_efforts = ["low", "medium", "high", "xhigh", "max"]
        for model_name in (
            "gpt-5.6-luna-excel",
            "gpt-5.6-terra-excel",
            "gpt-5.6-sol-excel",
        ):
            with self.subTest(model_name=model_name):
                self.assertEqual(
                    self._effort_names(model_name, raw_efforts),
                    ["low", "medium", "high", "xhigh"],
                )


class ExcelCodexInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.prompt = (
            (Path(__file__).resolve().parents[1] / "app" / "prompts" / "codex-excel.md")
            .read_text(encoding="utf-8")
            .strip()
        )
        self.service = ProxyClientConfigService(
            SimpleNamespace(
                codex_model_context_window=272_000,
                codex_model_auto_compact_token_limit=240_000,
            ),
        )

    def _models(self):
        return {
            entry["slug"]: entry
            for entry in self.service._build_codex_model_catalog_payload()["models"]
        }

    def test_excel_catalog_advertises_image_input(self):
        models = self._models()
        for model_id in excel_upstream.MODEL_IDS:
            with self.subTest(model=model_id):
                self.assertEqual(
                    models[model_id]["input_modalities"], ["text", "image"]
                )

    def test_excel_catalog_compaction_matches_model_window(self):
        models = self._models()
        for model_id in excel_upstream.MODEL_IDS:
            with self.subTest(model=model_id):
                expected_window = (
                    200_000 if model_id == "gpt-5.6-luna-excel" else 272_000
                )
                expected_limit = {
                    "gpt-6-astra-excel": 232_200,
                    "gpt-5.6-luna-excel": 180_000,
                }.get(model_id, 240_000)
                self.assertEqual(models[model_id]["context_window"], expected_window)
                self.assertEqual(
                    models[model_id]["auto_compact_token_limit"], expected_limit
                )
                self.assertEqual(
                    models[model_id]["effective_context_window_percent"], 95
                )
                self.assertEqual(
                    excel_upstream.LOCAL_MODEL_CAPABILITIES[model_id][
                        "auto_compact_token_limit"
                    ],
                    expected_limit,
                )

    def test_excel_catalog_uses_compact_workspace_prompt(self):
        self.assertTrue(self.prompt.startswith("You are Codex, an assistant"))
        self.assertGreater(len(self.prompt), 0)
        self.assertLess(len(self.prompt), 1500)
        self.assertIn("AGENTS.md", self.prompt)
        self.assertIn("tools and schemas supplied for this request", self.prompt)
        for stale_interface in (
            "functions.exec",
            "request_user_input_async",
            "skills.list",
        ):
            self.assertNotIn(stale_interface, self.prompt)
        models = self._models()
        for model_id in excel_upstream.MODEL_IDS:
            with self.subTest(model=model_id):
                self.assertEqual(models[model_id]["base_instructions"], self.prompt)
        self.assertEqual(set(models), set(excel_upstream.MODEL_IDS))

    def test_explicit_client_instructions_survive_request_and_tool_result_replay(self):
        instructions = "Native client model instructions. Preserve the user's workspace rules."
        environment = excel_upstream._message_item(
            "developer", "Workspace: /repo. Respect the current permission rules."
        )
        source = {
            "model": "gpt-5.6-sol-excel",
            "instructions": instructions,
            "input": [
                environment,
                excel_upstream._message_item("user", "Read the project files."),
            ],
            "tools": [{"type": "function", "name": "exec_command"}],
        }
        first = excel_upstream.prepare_responses_body(source)
        self.assertEqual(
            first["input"][0], excel_upstream._message_item("developer", instructions)
        )
        self.assertIn(environment, first["input"])
        self.assertIn('Client tool "exec_command" (function).', first["input"][1]["content"][0]["text"])
        source["input"].extend(
            [
                {
                    "type": "function_call",
                    "call_id": "call_prompt_test",
                    "name": "exec_command",
                    "arguments": '{"cmd":"pwd"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_prompt_test",
                    "output": "workspace",
                },
            ]
        )
        replay = excel_upstream.prepare_responses_body(source)
        self.assertEqual(replay["input"][0], first["input"][0])
        self.assertIn(environment, replay["input"])
        self.assertEqual(replay["input"][-2]["name"], "run_officejs")
        self.assertEqual(replay["input"][-1]["output"], "workspace")
        self.assertEqual(replay["metadata"]["agent_iteration"], "2")


if __name__ == "__main__":
    unittest.main()
