"""Normalize saved usage records and preserve their archive identity/metadata."""

import hashlib
import json
import os

from util import _coerce_float, _json_default
from usage_metrics import (
    normalize_usage_payload,
    _codex_native_session_id_from_request_id,
    _codex_logs_service_tiers,
    _native_usage_event_dedupe_key,
    _usage_event_estimated_cost,
)

try:
    from codex_native_ingest import (
        native_turn_metadata_for_rollout as _native_turn_metadata_for_rollout,
    )
except Exception:
    # Excel-only installs omit this module; do not search for it per history row.
    _native_turn_metadata_for_rollout = None


def _normalize_recorded_usage_event(
    payload: dict | None,
    *,
    refresh_native_tiers: bool = True,
) -> dict | None:
    if not isinstance(payload, dict):
        return None

    normalized_event = dict(payload)

    native_session_id = _codex_native_session_id_from_request_id(
        normalized_event.get("request_id")
    )
    # Backfill native_source for events that were archived before the
    # marker was preserved through compaction. The codex_native ingestor
    # uses request_ids of the form "codex-native:<session>:<turn>" and the
    # synthetic path "/native/codex/responses", so either is a reliable
    # signal that this row originated from a Codex CLI rollout file.
    if not normalized_event.get("native_source"):
        request_id = normalized_event.get("request_id")
        path = normalized_event.get("path")
        if (
            isinstance(request_id, str) and request_id.startswith("codex-native:")
        ) or path == "/native/codex/responses":
            normalized_event["native_source"] = "codex_native"
    native_model_provider = normalized_event.get("native_model_provider")
    if (
        normalized_event.get("native_source") == "codex_native"
        and isinstance(native_model_provider, str)
        and native_model_provider.strip().lower() == "custom"
    ):
        return None
    if not normalized_event.get("session_id") and native_session_id:
        normalized_event["session_id"] = native_session_id
        normalized_event.setdefault("session_id_origin", "codex_native_request_id")
    if not normalized_event.get("server_request_id"):
        effective_native_session_id = (
            normalized_event.get("session_id") or native_session_id
        )
        if (
            normalized_event.get("native_source") == "codex_native"
            and isinstance(effective_native_session_id, str)
            and effective_native_session_id
        ):
            normalized_event["server_request_id"] = effective_native_session_id
    if normalized_event.get("native_source") == "codex_native":
        requested_source = normalized_event.get("native_requested_service_tier_source")
        effective_source = normalized_event.get("native_service_tier_source")
        should_refresh_native_tiers = refresh_native_tiers or str(
            os.environ.get("GHCP_REFRESH_CODEX_LOG_TIERS_ON_LOAD", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
        native_service_tiers = (
            _codex_logs_service_tiers(
                normalized_event.get("session_id") or native_session_id,
                normalized_event.get("native_turn_id"),
                normalized_event.get("started_at"),
            )
            if should_refresh_native_tiers
            else {
                "requested": normalized_event.get("native_requested_service_tier"),
                "requested_source": requested_source,
                "effective": normalized_event.get("native_service_tier"),
                "effective_source": effective_source,
            }
        )
        requested_native_service_tier = native_service_tiers.get("requested")
        if (
            isinstance(requested_native_service_tier, str)
            and requested_native_service_tier
        ):
            normalized_event["native_requested_service_tier"] = (
                requested_native_service_tier
            )
            normalized_event["native_requested_service_tier_source"] = (
                native_service_tiers.get("requested_source")
            )
        elif (
            should_refresh_native_tiers
            and normalized_event.get("native_requested_service_tier_source")
            != "codex_logs_request"
        ):
            normalized_event.pop("native_requested_service_tier", None)
            normalized_event.pop("native_requested_service_tier_source", None)

        exact_native_service_tier = native_service_tiers.get("effective")
        if isinstance(exact_native_service_tier, str) and exact_native_service_tier:
            normalized_event["native_service_tier"] = exact_native_service_tier
            normalized_event["native_service_tier_source"] = native_service_tiers.get(
                "effective_source"
            )
        elif should_refresh_native_tiers and not str(
            normalized_event.get("native_service_tier_source") or ""
        ).startswith("codex_logs_response"):
            normalized_event.pop("native_service_tier", None)
            normalized_event.pop("native_service_tier_source", None)

        if _native_turn_metadata_for_rollout is not None and (
            not isinstance(
                normalized_event.get("native_turn_duration_ms"), (int, float)
            )
            or not normalized_event.get("native_turn_started_at")
        ):
            try:
                native_turn_metadata = _native_turn_metadata_for_rollout(
                    normalized_event.get("native_rollout_path"),
                    normalized_event.get("native_turn_id"),
                )
            except Exception:
                native_turn_metadata = {}
            for key, value in native_turn_metadata.items():
                if value is not None and normalized_event.get(key) is None:
                    normalized_event[key] = value

    normalized_usage = normalize_usage_payload(normalized_event.get("usage"))
    if isinstance(normalized_usage, dict):
        normalized_event["usage"] = normalized_usage
        # Costs are estimates derived from the normalized usage shape. Rebuild
        # them on load so historical rows pick up accounting corrections (for
        # example, reasoning tokens being a subset of output tokens) as well as
        # current native service-tier metadata.
        normalized_event["cost_usd"] = _usage_event_estimated_cost(
            normalized_event,
            usage=normalized_usage,
        )
    return normalized_event


def _usage_event_archive_summary(event: dict) -> dict:
    summary = {
        "request_id": event.get("request_id"),
        "started_at": event.get("started_at"),
        "finished_at": event.get("finished_at"),
        "path": event.get("path"),
        "requested_model": event.get("requested_model"),
        "resolved_model": event.get("resolved_model"),
        "initiator": event.get("initiator"),
        "session_id": event.get("session_id"),
        "project_path": event.get("project_path"),
        "client_request_id": event.get("client_request_id"),
        "subagent": event.get("subagent"),
        "server_request_id": event.get("server_request_id"),
        "status_code": event.get("status_code"),
        "success": event.get("success"),
        "cost_usd": round(_coerce_float(event.get("cost_usd")), 6),
    }

    # Preserve native-source markers across compaction so codex_native (and
    # any future ingested-source) traffic doesn't silently fall back to the
    # model-name heuristic in _usage_event_source after archival.
    for native_key in (
        "native_source",
        "native_origin",
        "native_cli_version",
        "native_model_provider",
        "native_plan_type",
        "native_requested_service_tier",
        "native_requested_service_tier_source",
        "native_service_tier",
        "native_service_tier_source",
        "native_reasoning_effort",
        "native_turn_id",
        "native_rollout_path",
        "native_turn_started_at",
        "native_turn_completed_at",
        "native_turn_duration_ms",
        "native_source_event_key",
        "native_dedupe_key",
        "reasoning_effort",
        "failure_diagnosis",
        "tool_call_recovery",
        "tool_call_diagnostics",
        "quota_account_key",
    ):
        value = event.get(native_key)
        if value is not None:
            summary[native_key] = value

    normalized_usage = normalize_usage_payload(event.get("usage"))
    if isinstance(normalized_usage, dict):
        summary["usage"] = normalized_usage

    return summary


def _usage_event_archive_key(summary: dict) -> str:
    native_dedupe_key = _native_usage_event_dedupe_key(summary)
    if native_dedupe_key:
        return f"native:{native_dedupe_key}"
    request_id = summary.get("request_id")
    if isinstance(request_id, str) and request_id:
        return f"request:{request_id}"
    serialized = json.dumps(
        summary, sort_keys=True, separators=(",", ":"), default=_json_default
    )
    return f"summary:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"
