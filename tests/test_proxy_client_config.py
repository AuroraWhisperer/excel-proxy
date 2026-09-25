import unittest
import json
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import excel_upstream
from constants import CODEX_PROXY_CONFIG
from proxy_client_config import ProxyClientConfig, ProxyClientConfigService


class ExcelConfigLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.primary = self.root / "config.toml"
        self.managed = self.root / "managed_config.toml"
        self.catalog = self.root / "models.json"
        self.settings = self.root / "client-proxy.json"
        self.service = ProxyClientConfigService(ProxyClientConfig(
            codex_primary_config_file=str(self.primary),
            codex_managed_config_file=str(self.managed),
            codex_model_catalog_file=str(self.catalog),
            codex_proxy_config=CODEX_PROXY_CONFIG,
            codex_model_context_window=272000,
            codex_model_auto_compact_token_limit=240000,
            client_proxy_settings_file=str(self.settings),
        ))

    def test_enable_refresh_disable_preserves_original_configuration(self):
        original = 'model = "old-model"\nmodel_provider = "openai"\n[features]\nexample = true\n'
        self.primary.write_text(original, encoding="utf-8")
        result = self.service.write_codex_proxy_config()
        self.assertTrue(result["configured"])
        parsed = tomllib.loads(self.primary.read_text(encoding="utf-8"))
        self.assertEqual(parsed["model"], excel_upstream.MODEL_ID)
        self.assertTrue(parsed["features"]["example"])
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        self.assertEqual({model["slug"] for model in catalog["models"]}, set(excel_upstream.MODEL_IDS))
        backups = set(self.root.glob("*.bak.*"))
        self.service.write_codex_proxy_config()
        self.assertEqual(set(self.root.glob("*.bak.*")), backups)
        self.service.disable_codex_proxy_config()
        self.assertEqual(self.primary.read_text(encoding="utf-8"), original)
        self.assertFalse(self.catalog.exists())

    def test_existing_proxy_upgrade_replaces_old_model_and_keeps_backup(self):
        self.primary.write_text('model = "old-model"\n', encoding="utf-8")
        self.service.write_codex_proxy_config()
        backups = set(self.root.glob("*.bak.*"))
        current = self.primary.read_text(encoding="utf-8").replace(excel_upstream.MODEL_ID, "old-model")
        self.primary.write_text(current, encoding="utf-8")
        self.service.write_codex_proxy_config()
        self.assertEqual(tomllib.loads(self.primary.read_text(encoding="utf-8"))["model"], excel_upstream.MODEL_ID)
        self.assertEqual(set(self.root.glob("*.bak.*")), backups)

    def test_shutdown_and_startup_restore_only_codex(self):
        self.primary.write_text('model = "original"\n', encoding="utf-8")
        self.service.write_codex_proxy_config()
        result = self.service.revert_proxy_configs_on_shutdown()
        self.assertEqual(set(result["clients"]), {"codex"})
        self.assertEqual(self.primary.read_text(encoding="utf-8"), 'model = "original"\n')
        self.assertTrue(self.service.restore_proxy_configs_on_startup()["restored"])
        self.settings.write_text(json.dumps({"pending_restore_targets": ["claude"]}), encoding="utf-8")
        self.assertEqual(self.service.load_client_proxy_settings()["pending_restore_targets"], [])


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
            Path(__file__).resolve().parents[1] / "app" / "prompts" / "codex-excel.md"
        ).read_text(encoding="utf-8").strip()
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
                self.assertEqual(models[model_id]["input_modalities"], ["text", "image"])

    def test_excel_catalog_uses_full_codex_prompt_only_for_excel_models(self):
        self.assertTrue(self.prompt.startswith("You are Codex, an agent based on GPT-6."))
        self.assertGreater(len(self.prompt), 20_000)
        self.assertIn("\n# Autonomy and persistence\n", self.prompt)
        models = self._models()
        for model_id in excel_upstream.MODEL_IDS:
            with self.subTest(model=model_id):
                self.assertEqual(models[model_id]["base_instructions"], self.prompt)
        self.assertEqual(set(models), set(excel_upstream.MODEL_IDS))


    def test_catalog_prompt_survives_request_and_tool_result_replay(self):
        instructions = self._models()["gpt-5.6-sol-excel"]["base_instructions"]
        source = {
            "model": "gpt-5.6-sol-excel",
            "instructions": instructions,
            "input": [excel_upstream._message_item("user", "Read the project files.")],
            "tools": [{"type": "function", "name": "exec_command"}],
        }
        first = excel_upstream.prepare_responses_body(source)
        self.assertEqual(first["input"][0], excel_upstream._message_item("developer", self.prompt))
        self.assertIn('"name":"exec_command"', first["input"][1]["content"][0]["text"])
        source["input"].extend([
            {"type": "function_call", "call_id": "call_prompt_test", "name": "exec_command", "arguments": '{"cmd":"pwd"}'},
            {"type": "function_call_output", "call_id": "call_prompt_test", "output": "workspace"},
        ])
        replay = excel_upstream.prepare_responses_body(source)
        self.assertEqual(replay["input"][0], first["input"][0])
        self.assertEqual(replay["input"][-2]["name"], "run_officejs")
        self.assertEqual(replay["input"][-1]["output"], "workspace")
        self.assertEqual(replay["metadata"]["agent_iteration"], "2")


if __name__ == "__main__":
    unittest.main()
