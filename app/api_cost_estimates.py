"""Reference API cost estimates for supplied events and account billing cycles."""

from datetime import datetime, timezone

from util import _coerce_int, _parse_iso_datetime
from usage_metrics import (
    normalize_usage_payload,
    usage_display_input_tokens as _usage_display_input_tokens,
    _pricing_entry_for_model,
    _usage_event_model_name,
    _usage_event_cost_breakdown,
    _usage_event_cost_multiplier,
)


def build_api_cost_estimate(events: list[dict], start: datetime, end: datetime) -> dict:
    """Reprice recorded usage; stored costs and subscription quota are not bills."""

    def empty_bucket():
        return {
            "request_count": 0,
            "priced_requests": 0,
            "unpriced_requests": 0,
            "missing_usage_requests": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "cost_breakdown": dict.fromkeys(
                ("input_fresh", "cached_input", "cache_creation", "output"), 0.0
            ),
        }

    total = empty_bucket()
    models = {}
    for event in events:
        event_time = _parse_iso_datetime(
            event.get("finished_at") or event.get("started_at")
        )
        if event_time is None or not start <= event_time < end:
            continue
        raw_usage = event.get("usage")
        has_usage = (
            isinstance(raw_usage, dict)
            and (
                raw_usage.get("input_tokens") is not None
                or raw_usage.get("prompt_tokens") is not None
            )
            and (
                raw_usage.get("output_tokens") is not None
                or raw_usage.get("completion_tokens") is not None
            )
        )
        # Like Sub2API's usage records, cost samples exclude failed attempts
        # with no upstream measurement. Their request/error logs stay intact.
        if not has_usage and _coerce_int(event.get("status_code")) >= 400:
            continue
        model = _usage_event_model_name(event) or "unknown"
        rates = _pricing_entry_for_model(model)
        if model not in models:
            models[model] = {
                **empty_bucket(),
                "model": model,
                "rates": dict(rates) if rates else None,
            }
        row = models[model]
        usage = normalize_usage_payload(raw_usage) if has_usage else None
        breakdown = _usage_event_cost_breakdown(model, usage)
        multiplier = _usage_event_cost_multiplier(event)
        for bucket in (total, row):
            bucket["request_count"] += 1
            bucket["unpriced_requests"] += int(rates is None)
            bucket["missing_usage_requests"] += int(not has_usage)
            if usage is not None:
                cached = _coerce_int(usage.get("cached_input_tokens"))
                bucket["input_tokens"] += _usage_display_input_tokens(usage) + cached
                bucket["cached_input_tokens"] += cached
                bucket["output_tokens"] += _coerce_int(usage.get("output_tokens"))
            if rates is not None and has_usage:
                bucket["priced_requests"] += 1
                for key, value in breakdown.items():
                    bucket["cost_breakdown"][key] += value * multiplier

    for bucket in (total, *models.values()):
        bucket["complete"] = not (
            bucket["unpriced_requests"] or bucket["missing_usage_requests"]
        )
        bucket["cost_usd"] = (
            sum(bucket["cost_breakdown"].values())
            if bucket["priced_requests"] or not bucket["request_count"]
            else None
        )
    total["models"] = sorted(
        models.values(), key=lambda row: (-(row["cost_usd"] or 0), row["model"])
    )
    total["currency"] = "USD"
    total["pricing_basis"] = "reference_api_rates"
    return total


def attach_account_cycle_estimates(payload: dict, events: list[dict]) -> dict:
    """Sub2API-style cost / utilization, scoped to the actual account and period."""
    by_account = {}
    for event in events:
        account_key = event.get("quota_account_key")
        if account_key:
            by_account.setdefault(account_key, []).append(event)
    for account in payload.get("accounts", []):
        for window in account.get("cycles", {}).get("windows", {}).values():
            window["local_usage"] = None
            window["api_cost_estimate"] = None
            window["estimated_total_usd"] = None
            window["estimated_remaining_usd"] = None
            start, end = window.get("started_at"), window.get("checked_at")
            if (
                window.get("awaiting_refresh")
                or start is None
                or end is None
                or start >= end
            ):
                continue
            start_time, end_time = (
                datetime.fromtimestamp(start, timezone.utc),
                datetime.fromtimestamp(end, timezone.utc),
            )
            selected = []
            sessions = set()
            for event in by_account.get(account["id"], []):
                began = _parse_iso_datetime(event.get("started_at"))
                finished = _parse_iso_datetime(event.get("finished_at"))
                # A request crossing a reset/sampling boundary cannot be split honestly.
                if (
                    began is None
                    or finished is None
                    or not start_time <= began <= finished < end_time
                ):
                    continue
                selected.append(event)
                if event.get("session_id"):
                    sessions.add(event["session_id"])
            cost = build_api_cost_estimate(selected, start_time, end_time)
            window["api_cost_estimate"] = cost
            window["local_usage"] = {
                key: cost[key]
                for key in ("request_count", "priced_requests", "cost_usd", "complete")
            }
            window["local_usage"]["conversation_count"] = len(sessions)
            used, amount = window.get("used_percent"), cost["cost_usd"]
            # Missing usage/prices reduce coverage, not the value of recorded costs.
            # The detail view retains missing usage/price counts.
            if (
                amount is not None
                and amount > 0
                and used is not None
                and 0 < used <= 100
                and account.get("status") == "ready"
                and not account.get("stale")
            ):
                window["estimated_total_usd"] = amount * 100 / used
                window["estimated_remaining_usd"] = amount * (100 - used) / used
    return payload
