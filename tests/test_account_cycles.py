"""Sub2API period identity, explicit reset receipts, and local price projection."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import account_balances as balances
from dashboard import attach_account_cycle_estimates
import proxy
from usage_tracking import _usage_event_archive_summary


def sample(remaining=60, reset=18000, balance=100, cards=3, duration=18000):
    return {
        "balance": balance,
        "unlimited": False,
        "plan_type": "pro",
        "reset_credits": {"available_count": cards, "expires_at": []},
        "windows": [
            {
                "kind": "primary_window",
                "label": "主额度窗口",
                "remaining_percent": remaining,
                "resets_at": reset,
                "window_seconds": duration,
            }
        ],
    }


def account():
    return {"name": "Test", "access_token": "secret-token", "account_id": "account-one"}


class AccountCycleTests(unittest.TestCase):
    def test_cycle_starts_at_reset_minus_window_like_sub2api(self):
        state = balances.advance_cycles({}, sample(), 100)
        window = state["windows"]["5h"]
        self.assertEqual(window["number"], 1)
        self.assertEqual(window["reason"], "first_seen")
        self.assertEqual(window["started_at"], 0)
        self.assertEqual(window["used_percent"], 40)

    def test_zero_balance_card_expiry_and_percent_corrections_do_not_change_cycle(self):
        state = balances.advance_cycles({}, sample(), 100)
        for i, quota in enumerate(
            (
                sample(balance=0, remaining=0),
                sample(balance=120, cards=2),
                sample(remaining=100),
            )
        ):
            state = balances.advance_cycles(state, quota, 200 + i)
        self.assertEqual(state["windows"]["5h"]["number"], 1)
        self.assertEqual(state["history"], [])

    def test_natural_rollover_and_expired_display_zeroing(self):
        state = balances.advance_cycles({}, sample(), 17000)
        expired = balances.public_cycles(state, now=18000)["windows"]["5h"]
        self.assertTrue(expired["awaiting_refresh"])
        self.assertEqual(expired["used_percent"], 0)
        self.assertEqual(state["windows"]["5h"]["used_percent"], 40)
        state = balances.advance_cycles(state, sample(reset=36000, remaining=95), 18500)
        current = state["windows"]["5h"]
        self.assertEqual(current["reason"], "scheduled")
        self.assertEqual(current["number"], 2)
        self.assertEqual(current["started_at"], 18000)
        self.assertEqual(len(state["history"]), 1)

    def test_manual_receipt_resets_even_when_end_timestamp_does_not_change(self):
        state = balances.advance_cycles({}, sample(), 100)
        receipt = {"status": "confirmed", "windows_reset": 1, "redeemed_at": 200}
        state = balances.advance_cycles(state, sample(remaining=100), 210, receipt)
        self.assertEqual(state["windows"]["5h"]["reason"], "manual_card")
        self.assertEqual(state["windows"]["5h"]["started_at"], 200)
        again = balances.advance_cycles(state, sample(remaining=99), 220, receipt)
        self.assertEqual(again["windows"]["5h"]["number"], 2)

    def test_external_boundary_change_restarts_without_fake_local_receipt(self):
        state = balances.advance_cycles({}, sample(), 100)
        state = balances.advance_cycles(
            state, sample(reset=18200, remaining=95, cards=2), 300
        )
        self.assertEqual(state["windows"]["5h"]["reason"], "external_reset")
        self.assertEqual(state["windows"]["5h"]["started_at"], 200)

    def test_late_idempotent_receipt_upgrades_observed_reset_without_double_counting(
        self,
    ):
        state = balances.advance_cycles({}, sample(), 100)
        state = balances.advance_cycles(state, sample(reset=18200, remaining=100), 210)
        receipt = {"status": "confirmed", "windows_reset": 1, "redeemed_at": 200}
        state = balances.advance_cycles(
            state, sample(reset=18200, remaining=99), 220, receipt
        )
        self.assertEqual(state["windows"]["5h"]["number"], 2)
        self.assertEqual(state["windows"]["5h"]["reason"], "manual_card")
        self.assertEqual(state["windows"]["5h"]["started_at"], 200)
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["closed_by"], "manual_card")

    def test_receipt_from_prior_period_does_not_restart_current_period(self):
        state = balances.advance_cycles({}, sample(), 100)
        state = balances.advance_cycles(state, sample(reset=36000), 19000)
        receipt = {"status": "confirmed", "windows_reset": 1, "redeemed_at": 200}
        state = balances.advance_cycles(state, sample(reset=36000), 19100, receipt)
        self.assertEqual(state["windows"]["5h"]["number"], 2)
        self.assertEqual(state["windows"]["5h"]["started_at"], 18000)
        self.assertEqual(state["windows"]["5h"]["reason"], "scheduled")

    def test_old_receipt_received_with_natural_rollover_is_not_a_manual_cycle(self):
        state = balances.advance_cycles({}, sample(), 100)
        receipt = {"status": "confirmed", "windows_reset": 1, "redeemed_at": 200}
        state = balances.advance_cycles(state, sample(reset=36000), 19000, receipt)
        self.assertEqual(state["windows"]["5h"]["reason"], "scheduled")
        self.assertEqual(state["windows"]["5h"]["number"], 2)

    def test_zero_window_receipt_does_not_create_manual_reset(self):
        state = balances.advance_cycles({}, sample(), 100)
        receipt = {"status": "confirmed", "windows_reset": 0, "redeemed_at": 200}
        state = balances.advance_cycles(state, sample(remaining=100), 210, receipt)
        self.assertEqual(state["windows"]["5h"]["number"], 1)
        state = balances.advance_cycles(state, sample(reset=18200), 220, receipt)
        self.assertEqual(state["windows"]["5h"]["reason"], "external_reset")

    def test_first_known_boundary_initializes_start_without_extra_cycle(self):
        state = balances.advance_cycles({}, sample(reset=None), 100)
        state = balances.advance_cycles(state, sample(), 200)
        self.assertEqual(state["windows"]["5h"]["started_at"], 0)
        self.assertEqual(state["windows"]["5h"]["number"], 1)
        self.assertEqual(state["history"], [])

    def test_windows_are_classified_by_duration_not_primary_secondary_order(self):
        quota = sample(reset=604800, duration=604800)
        quota["windows"] += sample()["windows"]
        state = balances.advance_cycles({}, quota, 100)
        self.assertEqual(set(state["windows"]), {"5h", "7d"})
        quota["windows"][1]["resets_at"] = 36000
        state = balances.advance_cycles(state, quota, 18500)
        self.assertEqual(state["windows"]["5h"]["number"], 2)
        self.assertEqual(state["windows"]["7d"]["number"], 1)

    def test_missing_windows_preserve_history_and_long_gaps_do_not_invent_events(self):
        state = balances.advance_cycles({}, sample(), 100)
        state = balances.advance_cycles(state, {"windows": []}, 200)
        state = balances.advance_cycles(state, sample(reset=108000), 100000)
        self.assertEqual(state["windows"]["5h"]["started_at"], 90000)
        self.assertEqual(state["windows"]["5h"]["number"], 2)
        self.assertEqual(len(state["history"]), 1)

    def test_history_is_bounded_and_input_is_not_mutated(self):
        original = state = balances.advance_cycles({}, sample(), 100)
        for cycle in range(1, 30):
            state = balances.advance_cycles(
                state, sample(reset=(cycle + 1) * 18000), cycle * 18000 + 100
            )
        self.assertEqual(original["windows"]["5h"]["number"], 1)
        self.assertEqual(len(state["history"]), 20)


class CyclePriceTests(unittest.TestCase):
    def event(self, **kwargs):
        return {
            "quota_account_key": "one",
            "request_id": "r1",
            "session_id": "chat-one",
            "started_at": "1970-01-01T00:01:40Z",
            "finished_at": "1970-01-01T00:01:50Z",
            "resolved_model": "gpt-5.6-sol-excel",
            "usage": {
                "input_tokens": 1000000,
                "input_tokens_details": {"cached_tokens": 800000},
                "output_tokens": 100000,
            },
            **kwargs,
        }

    def estimate(self, events, used=40, start=0, end=200):
        payload = {
            "accounts": [
                {
                    "id": "one",
                    "status": "ready",
                    "cycles": {
                        "windows": {
                            "5h": {
                                "used_percent": used,
                                "started_at": start,
                                "checked_at": end,
                            }
                        }
                    },
                }
            ]
        }
        return attach_account_cycle_estimates(payload, events)["accounts"][0]["cycles"][
            "windows"
        ]["5h"]

    def test_sub2_formula_reuses_existing_prices_not_credit_exchange_rate(self):
        result = self.estimate([self.event()])
        self.assertAlmostEqual(result["local_usage"]["cost_usd"], 5.24)
        self.assertAlmostEqual(result["estimated_total_usd"], 13.1)
        self.assertAlmostEqual(result["estimated_remaining_usd"], 7.86)
        self.assertEqual(result["local_usage"]["conversation_count"], 1)
        cost = result["api_cost_estimate"]
        self.assertAlmostEqual(cost["cost_usd"], result["local_usage"]["cost_usd"])
        self.assertEqual(cost["input_tokens"], 1000000)
        self.assertEqual(cost["cached_input_tokens"], 800000)
        self.assertAlmostEqual(sum(cost["cost_breakdown"].values()), 5.24)
        self.assertEqual(len(cost["models"]), 1)

    def test_newly_imported_account_does_not_inherit_previous_account_costs(self):
        store = balances.AccountBalanceStore()
        with (
            patch.object(balances, "fetch_quota", return_value=sample()),
            patch.object(balances.time, "time", return_value=200),
        ):
            old_id = store.import_accounts(
                {"access_token": "old-token", "account_id": "old-account"}
            )["imported_ids"][0]
            store.refresh(old_id)
            new_id = store.import_accounts(
                {"access_token": "new-token", "account_id": "new-account"}
            )["imported_ids"][0]
            payload = store.refresh(new_id)
        events = [self.event(quota_account_key=old_id)]
        rows = attach_account_cycle_estimates(payload, events)["accounts"]
        costs = {
            row["id"]: row["cycles"]["windows"]["5h"]["api_cost_estimate"]
            for row in rows
        }
        self.assertAlmostEqual(costs[old_id]["cost_usd"], 5.24)
        self.assertEqual(costs[new_id]["cost_usd"], 0)
        self.assertEqual(costs[new_id]["request_count"], 0)
        self.assertEqual(costs[new_id]["models"], [])

    def test_natural_and_manual_resets_clear_lower_totals_and_restart_same_chat(self):
        old = balances.advance_cycles({}, sample(), 150)
        for reason, quota, checked_at, receipt, began, finished in (
            (
                "scheduled",
                sample(reset=36000, remaining=100),
                18100,
                None,
                "1970-01-01T05:00:01Z",
                "1970-01-01T05:00:02Z",
            ),
            (
                "manual_card",
                sample(remaining=100),
                300,
                {"status": "confirmed", "windows_reset": 1, "redeemed_at": 200},
                "1970-01-01T00:03:21Z",
                "1970-01-01T00:03:22Z",
            ),
        ):
            with self.subTest(reason=reason):
                cycles = balances.advance_cycles(old, quota, checked_at, receipt)
                payload = {
                    "accounts": [{"id": "one", "status": "ready", "cycles": cycles}]
                }
                window = attach_account_cycle_estimates(payload, [self.event()])[
                    "accounts"
                ][0]["cycles"]["windows"]["5h"]
                self.assertEqual(window["reason"], reason)
                self.assertEqual(window["api_cost_estimate"]["cost_usd"], 0)
                self.assertEqual(window["api_cost_estimate"]["models"], [])
                self.assertIsNone(window["estimated_total_usd"])
                event = self.event(
                    request_id="new-period", started_at=began, finished_at=finished
                )
                window["used_percent"] = 10
                attach_account_cycle_estimates(payload, [self.event(), event])
                self.assertEqual(window["local_usage"]["request_count"], 1)
                self.assertEqual(window["local_usage"]["conversation_count"], 1)
                self.assertAlmostEqual(window["api_cost_estimate"]["cost_usd"], 5.24)
                self.assertAlmostEqual(window["estimated_total_usd"], 52.4)

    def test_new_period_excludes_old_unknown_other_account_and_crossing_requests(self):
        events = [
            self.event(),
            self.event(quota_account_key="other"),
            self.event(quota_account_key=None),
            self.event(
                started_at="1970-01-01T00:00:30Z", finished_at="1970-01-01T00:02:00Z"
            ),
        ]
        result = self.estimate(events, start=90)
        self.assertEqual(result["local_usage"]["request_count"], 1)
        self.assertIsNone(self.estimate(events, start=150)["estimated_total_usd"])

    def test_zero_percent_and_missing_prices_wait_for_new_sampling(self):
        self.assertIsNone(self.estimate([self.event()], used=0)["estimated_total_usd"])
        self.assertIsNone(
            self.estimate([self.event(resolved_model="unpriced")])[
                "estimated_total_usd"
            ]
        )

    def test_partial_pricing_keeps_estimate_and_marks_usage_incomplete(self):
        for missing in ({"resolved_model": "unpriced"}, {"usage": None}):
            with self.subTest(missing=missing):
                result = self.estimate(
                    [self.event(), self.event(request_id="r2", **missing)], used=23
                )
                self.assertFalse(result["local_usage"]["complete"])
                self.assertEqual(result["local_usage"]["priced_requests"], 1)
                self.assertEqual(result["local_usage"]["request_count"], 2)
                self.assertAlmostEqual(result["estimated_total_usd"], 5.24 / 0.23)
                self.assertAlmostEqual(
                    result["estimated_remaining_usd"], 5.24 / 0.23 * 0.77
                )

    def test_cycle_pricing_excludes_unmetered_failures_but_keeps_metered_failures(self):
        result = self.estimate(
            [
                self.event(),
                self.event(request_id="cancelled", status_code=499, usage=None),
                self.event(request_id="partial", status_code=502),
            ]
        )
        self.assertEqual(result["local_usage"]["request_count"], 2)
        self.assertEqual(result["local_usage"]["priced_requests"], 2)
        self.assertTrue(result["local_usage"]["complete"])
        self.assertAlmostEqual(result["local_usage"]["cost_usd"], 10.48)
        self.assertAlmostEqual(result["estimated_total_usd"], 26.2)
        self.assertAlmostEqual(result["estimated_remaining_usd"], 15.72)

    def test_stale_or_failed_quota_does_not_produce_a_current_estimate(self):
        for flags in ({"status": "error"}, {"status": "ready", "stale": True}):
            with self.subTest(flags=flags):
                payload = {
                    "accounts": [
                        {
                            "id": "one",
                            **flags,
                            "cycles": {
                                "windows": {
                                    "5h": {
                                        "used_percent": 23,
                                        "started_at": 0,
                                        "checked_at": 200,
                                    }
                                }
                            },
                        }
                    ]
                }
                result = attach_account_cycle_estimates(payload, [self.event()])[
                    "accounts"
                ][0]["cycles"]["windows"]["5h"]
                self.assertIsNone(result["estimated_total_usd"])
                self.assertIsNone(result["estimated_remaining_usd"])

    def test_same_conversation_is_counted_by_its_requests_in_each_period(self):
        old = self.event()
        new = self.event(
            request_id="r2",
            started_at="1970-01-01T00:05:00Z",
            finished_at="1970-01-01T00:05:10Z",
        )
        result = self.estimate([old, new], start=200, end=400)
        self.assertEqual(result["local_usage"]["request_count"], 1)
        self.assertEqual(result["local_usage"]["conversation_count"], 1)

    def test_account_identity_matches_import_and_survives_archival_without_secrets(
        self,
    ):
        imported = balances.AccountBalanceStore().import_accounts(account())[
            "accounts"
        ][0]["id"]
        key = balances.account_key_from_headers(
            {
                "Authorization": "Bearer secret-token",
                "ChatGPT-Account-Id": "account-one",
            }
        )
        self.assertEqual(key, imported)
        saved = _usage_event_archive_summary(self.event(quota_account_key=key))
        self.assertEqual(saved["quota_account_key"], key)
        self.assertNotIn("secret-token", json.dumps(saved))


class ResetActionTests(unittest.TestCase):
    def setUp(self):
        self.store = balances.AccountBalanceStore()
        self.record_id = self.store.import_accounts(account())["accounts"][0]["id"]
        self.reset_at = balances.time.time() + 17000
        with patch.object(
            balances, "fetch_quota", return_value=sample(reset=self.reset_at)
        ):
            self.store.refresh(self.record_id)
        self.checked_at = self.store.snapshot()["accounts"][0]["checked_at"]

    def test_successful_reset_consumes_once_then_refreshes(self):
        receipt = {
            "status": "confirmed",
            "windows_reset": 1,
            "redeemed_at": balances.time.time(),
        }
        with (
            patch.object(
                balances, "consume_reset_credit", return_value=receipt
            ) as consume,
            patch.object(
                balances,
                "fetch_quota",
                return_value=sample(reset=self.reset_at, remaining=100, cards=2),
            ) as query,
        ):
            result = self.store.reset_credit(self.record_id, self.checked_at)
        consume.assert_called_once()
        query.assert_called_once()
        row = result["accounts"][0]
        self.assertEqual(row["cycles"]["windows"]["5h"]["reason"], "manual_card")
        self.assertEqual(row["reset_action"]["status"], "confirmed")
        self.assertNotIn("redeem_request_id", json.dumps(result))

    def test_pending_retry_reuses_the_same_idempotency_key(self):
        with patch.object(
            balances,
            "consume_reset_credit",
            side_effect=balances.BalanceError("timeout"),
        ) as consume:
            self.store.reset_credit(self.record_id, self.checked_at)
            self.store.reset_credit(self.record_id, self.checked_at)
        self.assertEqual(
            consume.call_args_list[0].args[1], consume.call_args_list[1].args[1]
        )

    def test_post_reset_query_failure_does_not_consume_another_card(self):
        receipt = {
            "status": "confirmed",
            "windows_reset": 1,
            "redeemed_at": balances.time.time(),
        }
        with (
            patch.object(
                balances, "consume_reset_credit", return_value=receipt
            ) as consume,
            patch.object(
                balances, "fetch_quota", side_effect=balances.BalanceError("network")
            ),
        ):
            result = self.store.reset_credit(self.record_id, self.checked_at)
            with self.assertRaises(balances.BalanceError):
                self.store.reset_credit(self.record_id, self.checked_at)
        consume.assert_called_once()
        self.assertEqual(result["accounts"][0]["reset_action"]["status"], "confirmed")

    def test_replaying_old_browser_snapshot_cannot_spend_second_card(self):
        receipt = {
            "status": "confirmed",
            "windows_reset": 1,
            "redeemed_at": balances.time.time(),
        }
        with (
            patch.object(
                balances, "consume_reset_credit", return_value=receipt
            ) as consume,
            patch.object(
                balances,
                "fetch_quota",
                return_value=sample(reset=self.reset_at, remaining=100, cards=2),
            ),
        ):
            self.store.reset_credit(self.record_id, self.checked_at)
            with self.assertRaises(balances.BalanceError):
                self.store.reset_credit(self.record_id, self.checked_at)
        consume.assert_called_once()

    def test_actual_post_is_fixed_and_receipt_ids_are_not_returned(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "code": "success",
                    "windows_reset": 2,
                    "credit": {
                        "id": "private-card-id",
                        "redeemed_at": "2026-09-26T09:00:00Z",
                    },
                },
            )

        with patch.object(
            balances,
            "_client",
            side_effect=lambda: httpx.Client(transport=httpx.MockTransport(handle)),
        ):
            result = balances.consume_reset_credit(
                {"access_token": "secret", "account_id": "one"}, "request-key"
            )
        self.assertEqual(str(calls[0].url), balances.RESET_CONSUME_URL)
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(
            json.loads(calls[0].content), {"redeem_request_id": "request-key"}
        )
        self.assertEqual(result["windows_reset"], 2)
        self.assertNotIn("private-card-id", json.dumps(result))

    def test_cycles_persist_across_restart_and_reimport(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                balances, "_protect_windows_data", side_effect=lambda raw: raw[::-1]
            ),
            patch.object(
                balances, "_unprotect_windows_data", side_effect=lambda raw: raw[::-1]
            ),
        ):
            path = Path(directory) / "accounts.bin"
            store = balances.AccountBalanceStore(path)
            key = store.import_accounts(account())["accounts"][0]["id"]
            with patch.object(
                balances, "fetch_quota", return_value=sample(reset=self.reset_at)
            ):
                store.refresh(key)
            loaded = balances.AccountBalanceStore(path)
            result = loaded.import_accounts(account())
            self.assertEqual(
                result["accounts"][0]["cycles"]["windows"]["5h"]["number"], 1
            )
            self.assertIsNone(result["accounts"][0]["quota"])
            loaded.remove(key)
            self.assertEqual(
                balances.AccountBalanceStore(path).snapshot()["accounts"], []
            )


class ResetRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_and_same_origin_are_required(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        ) as client:
            with patch.object(
                balances.balance_store, "reset_credit", return_value={"accounts": []}
            ) as reset:
                for payload in ({}, {"confirm": "true"}, {"confirm": False}):
                    response = await client.post(
                        "/api/account-balances/one/reset", json=payload
                    )
                    self.assertEqual(response.status_code, 400)
                response = await client.post(
                    "/api/account-balances/one/reset",
                    json={"confirm": True},
                    headers={"Origin": "https://evil.example"},
                )
                self.assertEqual(response.status_code, 403)
                reset.assert_not_called()
                response = await client.post(
                    "/api/account-balances/one/reset", json={"confirm": True}
                )
                self.assertEqual(response.status_code, 400)
                response = await client.post(
                    "/api/account-balances/one/reset",
                    json={"confirm": True, "checked_at": 100},
                )
                self.assertEqual(response.status_code, 200)
                reset.assert_called_once_with("one", 100)
