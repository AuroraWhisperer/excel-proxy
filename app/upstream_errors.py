"""Safe client errors and redacted diagnostics for the Excel upstream."""

from __future__ import annotations

import re
from typing import Any, Sequence

import httpx


class ExcelResponseError(httpx.RemoteProtocolError):
    """A locally detected response-contract failure with a client-safe reason."""

    def __init__(self, code: str, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def diagnose_failure(
    status_code: int,
    *,
    code: str | None = None,
    error_type: str | None = None,
) -> dict:
    """Classify known signals only; never copy upstream text or unknown codes."""
    code = code if isinstance(code, str) else None
    error_type = error_type if isinstance(error_type, str) else None
    if status_code == 499:
        category, action = (
            "cancelled",
            "The request was cancelled; do not replay tool work automatically.",
        )
    elif status_code == 401 or code == "excel_auth_required":
        category, action = (
            "authentication",
            "Sign in again to the selected connection account, or refresh the selected Excel add-in session.",
        )
    elif status_code == 403 or code in {
        "excel_access_denied",
        "basispoints_model_access_changed",
    }:
        category, action = (
            "permission",
            "Check account and model access; retrying unchanged will not fix permission.",
        )
    elif status_code == 429 or code == "excel_rate_limited":
        category, action = (
            "rate_limit",
            "Wait for the retry-after interval and reduce concurrent requests.",
        )
    elif code == "excel_untranslatable_tool_call":
        category, action = (
            "tool_contract",
            (
                "Regenerate the tool envelope using JSON serialization and the declared schema. "
                "Check quotes and backslashes; do not guess or execute malformed commands."
            ),
        )
    elif code == "excel_stream_incomplete":
        category, action = (
            "incomplete_stream",
            "Check upstream connectivity; do not replay tools whose execution is unconfirmed.",
        )
    elif code in {
        "excel_invalid_response",
        "excel_invalid_output_item",
        "excel_missing_output_items",
        "excel_conflicting_output_items",
        "excel_incomplete_response",
        "excel_empty_response",
        "excel_invalid_structured_output",
    }:
        category, action = (
            "response_contract",
            "The upstream response is inconsistent; retain the request ID for diagnosis.",
        )
    elif status_code == 504 or error_type in {
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }:
        category, action = (
            "timeout",
            "Check latency and concurrency; a timeout does not prove the operation was not executed.",
        )
    elif code == "excel_repair_transport_error" or error_type in {
        "ConnectError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "ProxyError",
    }:
        category, action = (
            "connection",
            "Check connectivity to the network and local service; only pre-send connection failures are safe to replay.",
        )
    elif status_code in {400, 404, 405, 413, 415, 422}:
        category, action = (
            "request_validation",
            "Check the endpoint, input size and declared parameter schema before resending.",
        )
    elif status_code in {500, 502, 503, 507, 529}:
        category, action = (
            "upstream_service",
            "Check upstream availability and request ID; do not blindly repeat side-effecting work.",
        )
    else:
        category, action = (
            "unknown",
            "Inspect the request ID and safe diagnostics before deciding whether to retry.",
        )
    return {"category": category, "action": action}


def sanitized_error_details(
    payload: Any, *, secrets: Sequence[str] = ()
) -> dict | None:
    """Keep bounded diagnostic fields, never an upstream request/body echo."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    fields = {}
    for key in ("message", "code", "type", "param"):
        value = error.get(key)
        if not isinstance(value, str):
            continue
        for secret in secrets:
            if not isinstance(secret, str) or not secret:
                continue
            value = value.replace(secret, "[redacted]")
            if secret.lower().startswith("bearer ") and secret[7:]:
                value = value.replace(secret[7:], "[redacted]")
        value = re.sub(r"(?i)\bBearer\s+[^\s\"',;<>]+", "Bearer [redacted]", value)
        value = re.sub(
            r"(https?://)[^/\s@]+@", r"\1[redacted]@", value, flags=re.IGNORECASE
        )
        value = re.sub(
            r"""(?i)(\b(?:authorization|api[_-]?key|(?:access_|refresh_|id_)?token|secret|password|cookie|(?:recovery_)?ticket)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
            r"\1[redacted]",
            value,
        )
        fields[key] = value[:2048]
    return {"error": fields} if fields else None


def excel_error_payload(status_code: int, payload: Any = None) -> dict:
    """Return a client-safe error without forwarding upstream free text."""
    error = payload.get("error") if isinstance(payload, dict) else None
    upstream_code = error.get("code") if isinstance(error, dict) else None
    code, message = {
        401: (
            "excel_auth_required",
            "Sign in again to the selected connection account, or refresh the selected Excel add-in session.",
        ),
        403: (
            "excel_access_denied",
            "The Excel account cannot access this model or endpoint.",
        ),
        429: (
            "excel_rate_limited",
            "The Excel endpoint is rate limited. Try again later.",
        ),
    }.get(
        status_code,
        (
            "excel_upstream_error",
            f"Excel could not complete the request (HTTP {status_code}).",
        ),
    )
    if upstream_code == "basispoints_model_access_changed":
        code, message = (
            upstream_code,
            "This model is not available on the account's Excel endpoint.",
        )
    return {
        "error": {
            "type": "server_error" if status_code >= 500 else "invalid_request_error",
            "code": code,
            "message": message,
            "param": None,
            "diagnosis": diagnose_failure(status_code, code=code),
        }
    }
