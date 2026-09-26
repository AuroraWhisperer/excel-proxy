"""Run selected offline regressions without using the user's runtime state."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


TEST_MODULES = (
    "test_local_access",
    "test_request_validation",
    "test_excel_only",
    "test_dashboard_pages",
    "test_api_cost_estimate",
    "test_account_quota",
    "test_excel_upstream",
    "test_excel_tool_compatibility",
    "test_excel_continuity",
    "test_excel_stream_recovery",
    "test_excel_image_generation",
    "test_excel_images",
    "test_excel_request_compat",
    "test_excel_contracts",
    "test_reasoning_translation",
    "test_request_prompt_archive",
    "test_proxy_client_config",
    "test_usage_model_identity",
    "test_usage_timing",
    "test_first_output_probe",
    "test_usage_startup",
    "test_excel_session_capture",
    "test_proxy_env",
    "test_windows_launcher",
)


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repo / "app"), str(repo / "tests")]
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="ghcp-contracts-") as directory:
        isolated = {
            "GHCP_CONFIG_DIR": str(Path(directory) / "config"),
            "GHCP_STATE_DIR": str(Path(directory) / "state"),
            "GHCP_CACHE_DIR": str(Path(directory) / "cache"),
            "GHCP_CACHE_DB_PATH": str(Path(directory) / "cache" / "dashboard.sqlite3"),
            "GHCP_TRACE_LOG_FILE": str(Path(directory) / "state" / "request-trace.jsonl"),
            "GHCP_REQUEST_BODY_DUMP_DIR": str(Path(directory) / "request-bodies"),
            "GHCP_REQUEST_PROMPT_ARCHIVE_DIR": str(Path(directory) / "prompts"),
            "GHCP_TRACE_REQUESTS": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(repo / "app"),
        }
        with patch.dict(os.environ, isolated):
            import httpx

            with patch.object(httpx.HTTPTransport, "handle_request", side_effect=AssertionError("Real HTTP is disabled in offline regressions")), \
                 patch.object(httpx.AsyncHTTPTransport, "handle_async_request", side_effect=AssertionError("Real HTTP is disabled in offline regressions")):
                try:
                    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or TEST_MODULES)
                    result = unittest.TextTestRunner(verbosity=2).run(suite)
                    return 0 if result.wasSuccessful() and result.testsRun else 1
                finally:
                    proxy = sys.modules.get("proxy")
                    for name in ("_REQUEST_TRACE_EXECUTOR", "_REQUEST_BODY_DUMP_EXECUTOR"):
                        executor = getattr(proxy, name, None)
                        if executor is not None:
                            executor.shutdown(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
