import copy
import importlib
import json
import unittest

import httpx

from test_module_boundaries import imported_modules


class RequestDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.diagnostics = importlib.import_module("request_diagnostics")

    def test_headers_exclude_authentication_and_cookies(self):
        self.assertEqual(
            self.diagnostics.header_trace_subset(
                {
                    "Authorization": "synthetic-secret",
                    "Cookie": "synthetic-cookie",
                    "x-request-id": "request-1",
                    "Content-Type": "application/json",
                }
            ),
            {"x-request-id": "request-1", "Content-Type": "application/json"},
        )

    def test_body_summary_does_not_mutate_or_expose_prompt_text(self):
        body = {
            "model": "gpt-5.6-sol-excel",
            "stream": True,
            "input": [{"role": "user", "content": "do-not-leak-synthetic-text"}],
            "reasoning": {"effort": "medium"},
            "metadata": {"private": "synthetic-metadata"},
        }
        original = copy.deepcopy(body)
        summary = self.diagnostics.trace_body_summary(body)
        self.assertEqual(body, original)
        self.assertEqual(summary["reasoning_effort"], "medium")
        self.assertEqual(summary["metadata_keys"], ["private"])
        self.assertEqual(len(summary["body_fingerprint"]), 16)
        self.assertNotIn("do-not-leak-synthetic-text", json.dumps(summary))
        self.assertNotIn("synthetic-metadata", json.dumps(summary))

    def test_response_summary_keeps_status_and_safe_error_fields(self):
        response = httpx.Response(
            503,
            headers={"content-type": "application/json", "x-request-id": "upstream-1"},
        )
        summary = self.diagnostics.trace_response_summary(
            upstream=response,
            status_code=502,
            response_payload={
                "id": "response-1",
                "output": [{"type": "message"}],
                "error": {
                    "type": "upstream_error",
                    "code": "unavailable",
                    "message": "synthetic-secret",
                },
            },
        )
        self.assertEqual(summary["status_code"], 502)
        self.assertEqual(summary["upstream_status_code"], 503)
        self.assertEqual(summary["upstream_request_id"], "upstream-1")
        self.assertEqual(
            summary["error"], {"type": "upstream_error", "code": "unavailable"}
        )
        self.assertNotIn("synthetic-secret", json.dumps(summary))

    def test_proxy_uses_the_shared_implementations(self):
        import proxy

        for name in (
            "trace_hash",
            "trace_body_summary",
            "trace_response_summary",
            "header_trace_subset",
            "extract_prompt_preview",
        ):
            self.assertIs(getattr(proxy, "_" + name), getattr(self.diagnostics, name))

    def test_diagnostics_do_not_import_proxy_or_upstream(self):
        self.assertTrue(
            {"proxy", "excel_upstream"}.isdisjoint(
                imported_modules("request_diagnostics")
            )
        )
