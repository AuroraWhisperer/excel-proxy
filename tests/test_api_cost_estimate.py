"""API-equivalent costs use recorded usage, not subscription quota."""

import unittest
from datetime import datetime, timezone

from dashboard import _build_api_cost_estimate


class ApiCostEstimateTests(unittest.TestCase):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def event(self, model="gpt-5.6-sol-excel", **overrides):
        return {"resolved_model": model, "finished_at": "2026-09-25T12:00:00Z",
                "usage": {"input_tokens": 1000000, "input_tokens_details": {"cached_tokens": 800000},
                          "output_tokens": 100000}, **overrides}

    def estimate(self, *events):
        return _build_api_cost_estimate(events, self.start, self.end)

    def test_cached_input_is_not_charged_twice(self):
        result = self.estimate(self.event(cost_usd=999))
        self.assertAlmostEqual(result["cost_usd"], 5.24)
        self.assertEqual(result["input_tokens"], 1000000)
        self.assertEqual(result["cached_input_tokens"], 800000)
        self.assertAlmostEqual(result["cost_breakdown"]["input_fresh"], 1.6)
        self.assertAlmostEqual(result["cost_breakdown"]["cached_input"], 0.64)
        self.assertAlmostEqual(result["cost_breakdown"]["output"], 3)

    def test_mixed_models_are_priced_separately(self):
        result = self.estimate(self.event(), self.event("gpt-5.6-terra-excel"))
        # Long-context rates apply per request, never to the monthly sum.
        self.assertAlmostEqual(result["cost_usd"], 5.24 + 3.65)
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["request_count"], 2)

    def test_missing_model_price_is_explicit_not_free(self):
        result = self.estimate(self.event(), self.event("unknown-excel"))
        self.assertEqual(result["unpriced_requests"], 1)
        self.assertFalse(result["complete"])
        unknown = next(row for row in result["models"] if row["model"] == "unknown-excel")
        self.assertIsNone(unknown["cost_usd"])
        self.assertIsNone(unknown["rates"])
        self.assertAlmostEqual(result["cost_usd"], 5.24)

    def test_only_current_month_is_included(self):
        result = self.estimate(self.event(), self.event(finished_at="2026-08-31T23:59:59Z"),
                               self.event(finished_at="2026-10-01T00:00:00Z"))
        self.assertEqual(result["request_count"], 1)

    def test_empty_usage_is_zero_but_missing_usage_is_not_free(self):
        empty = self.estimate()
        self.assertEqual(empty["cost_usd"], 0)
        self.assertTrue(empty["complete"])
        missing = self.estimate(self.event(usage=None))
        self.assertEqual(missing["missing_usage_requests"], 1)
        self.assertFalse(missing["complete"])

    def test_cache_writes_and_reasoning_are_not_double_counted(self):
        event = self.event(usage={"input_tokens": 1000, "fresh_input_tokens": 800,
                                 "cached_input_tokens": 200, "cache_creation_input_tokens": 300,
                                 "output_tokens": 100, "reasoning_output_tokens": 80})
        result = self.estimate(event)
        self.assertAlmostEqual(result["cost_usd"], .00558)
        self.assertAlmostEqual(result["cost_breakdown"]["cache_creation"], .0015)


if __name__ == "__main__":
    unittest.main()
