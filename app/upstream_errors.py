"""Safe client errors and redacted diagnostics for the Excel upstream."""

from __future__ import annotations

import re
from typing import Any, Sequence


def sanitized_error_details(payload: Any, *, secrets: Sequence[str] = ()) -> dict | None:
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
        value = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", value, flags=re.IGNORECASE)
        value = re.sub(
            r'''(?i)(\b(?:authorization|api[_-]?key|(?:access_|refresh_|id_)?token|secret|password|cookie|(?:recovery_)?ticket)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
            r"\1[redacted]", value,
        )
        fields[key] = value[:2048]
    return {"error": fields} if fields else None


def excel_error_payload(status_code: int, payload: Any = None) -> dict:
    """Return a client-safe error without forwarding upstream free text."""
    error = payload.get("error") if isinstance(payload, dict) else None
    upstream_code = error.get("code") if isinstance(error, dict) else None
    code, message = {
        401: ("excel_auth_required", "Refresh the signed-in ChatGPT Excel add-in session and try again."),
        403: ("excel_access_denied", "The Excel account cannot access this model or endpoint."),
        429: ("excel_rate_limited", "The Excel endpoint is rate limited. Try again later."),
    }.get(status_code, ("excel_upstream_error", f"Excel could not complete the request (HTTP {status_code})."))
    if upstream_code == "basispoints_model_access_changed":
        code, message = upstream_code, "This model is not available on the account's Excel endpoint."
    return {"error": {
        "type": "server_error" if status_code >= 500 else "invalid_request_error",
        "code": code, "message": message, "param": None,
    }}
