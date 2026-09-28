"""Excel session headers and encrypted local persistence, independent of translation."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import threading
import time

from app_paths import user_state_dir
from excel_models import MODEL_ID, UPSTREAM_MODEL
from windows_dpapi import (
    protect_data as _protect_windows_data,
    unprotect_data as _unprotect_windows_data,
)


RESPONSES_URL = os.environ.get(
    "GHCP_EXCEL_RESPONSES_URL",
    "https://bps.openai.com/basispoints/api/responses",
).strip()

SESSION_FILE = (
    os.path.join(user_state_dir(), "excel-session.dpapi")
    if sys.platform == "win32"
    else None
)

TOOLS_VERSION_METADATA_KEY = "bps_tools_version_id"

TOOLS_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,160}$")

_ALLOWED_CAPTURED_HEADERS = frozenset(
    {
        "authorization",
        "chatgpt-account-id",
        "user-agent",
        "x-basispoints-auth-mode",
        "x-openai-account-id",
        "x-openai-account-user-id",
        "x-openai-internal-basispoints-browser-name",
        "x-openai-internal-basispoints-browser-ua-brands",
        "x-openai-internal-basispoints-browser-ua-mobile",
        "x-openai-internal-basispoints-browser-ua-platform",
        "x-openai-internal-basispoints-client-agent-profile",
        "x-openai-internal-basispoints-client-editor",
        "x-openai-internal-basispoints-client-host",
        "x-openai-internal-basispoints-client-platform",
        "x-openai-internal-basispoints-client-platform-class",
        "x-openai-internal-basispoints-client-product",
        "x-openai-internal-basispoints-client-runtime",
        "x-openai-internal-basispoints-office-host",
        "x-openai-internal-basispoints-office-platform",
        "x-stainless-arch",
        "x-stainless-lang",
        "x-stainless-os",
        "x-stainless-package-version",
        "x-stainless-retry-count",
        "x-stainless-runtime",
        "x-stainless-runtime-version",
    }
)

DEFAULT_CLIENT_HEADERS = {
    "x-basispoints-auth-mode": "chatgpt",
    "x-openai-internal-basispoints-client-agent-profile": "excel",
    "x-openai-internal-basispoints-client-editor": "excel",
    "x-openai-internal-basispoints-client-host": "office",
    "x-openai-internal-basispoints-client-platform": "excel",
    "x-openai-internal-basispoints-client-platform-class": "PC",
    "x-openai-internal-basispoints-client-product": "basispoints-excel-plugin",
    "x-openai-internal-basispoints-client-runtime": "desktop",
    "x-openai-internal-basispoints-office-host": "Excel",
    "x-openai-internal-basispoints-office-platform": "PC",
    "x-stainless-arch": "unknown",
    "x-stainless-lang": "js",
    "x-stainless-os": "Unknown",
    "x-stainless-package-version": "6.31.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "browser:chrome",
}


def _decode_jwt_exp(authorization: str) -> float | None:
    token = authorization.split(None, 1)[1]
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        expiration = payload.get("exp")
        return float(expiration) if isinstance(expiration, (int, float)) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


class ExcelSessionStore:
    def __init__(self, persistence_file: str | None = None):
        self._lock = threading.Lock()
        self._headers: dict[str, str] = {}
        self._tools_version_id: str | None = None
        self._configured_at: float | None = None
        self._expires_at: float | None = None
        self._persistence_file = persistence_file
        self._persistence_error = ""

    def configure(
        self,
        raw_headers: object,
        *,
        tools_version_id: object = None,
        persist: bool = True,
        allow_expired: bool = False,
    ) -> dict[str, object]:
        if not isinstance(raw_headers, dict):
            raise ValueError("headers must be a JSON object")
        if tools_version_id is not None and (
            not isinstance(tools_version_id, str)
            or not TOOLS_VERSION_PATTERN.fullmatch(tools_version_id.strip())
        ):
            raise ValueError("tools_version_id is not a valid Basispoints version ID")
        normalized_tools_version_id = (
            tools_version_id.strip() if isinstance(tools_version_id, str) else None
        )

        headers: dict[str, str] = {}
        for raw_name, raw_value in raw_headers.items():
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                continue
            name = raw_name.strip().lower()
            value = raw_value.strip()
            if name in _ALLOWED_CAPTURED_HEADERS and value and len(value) <= 32_768:
                headers[name] = value

        authorization = headers.get("authorization", "")
        if (
            not authorization.lower().startswith("bearer ")
            or len(authorization.split(None, 1)) != 2
        ):
            raise ValueError("a Bearer authorization header is required")

        chatgpt_account = headers.get("chatgpt-account-id")
        openai_account = headers.get("x-openai-account-id")
        if not chatgpt_account and not openai_account:
            raise ValueError("a ChatGPT account ID header is required")
        if chatgpt_account and openai_account and chatgpt_account != openai_account:
            raise ValueError("captured account ID headers do not match")
        account_id = chatgpt_account or openai_account
        headers["chatgpt-account-id"] = account_id
        headers["x-openai-account-id"] = account_id

        for name, value in DEFAULT_CLIENT_HEADERS.items():
            headers.setdefault(name, value)

        expires_at = _decode_jwt_exp(authorization)
        now = time.time()
        if expires_at is not None and expires_at <= now and not allow_expired:
            raise ValueError("the captured ChatGPT bearer token is already expired")

        with self._lock:
            self._headers = headers
            self._tools_version_id = normalized_tools_version_id
            self._configured_at = now
            self._expires_at = expires_at
            self._persistence_error = ""
        if persist and self._persistence_file:
            try:
                self._save()
            except (OSError, RuntimeError) as exc:
                with self._lock:
                    self._persistence_error = str(exc)
        return self.status()

    def clear(self) -> dict[str, object]:
        with self._lock:
            self._headers = {}
            self._tools_version_id = None
            self._configured_at = None
            self._expires_at = None
            self._persistence_error = ""
        if self._persistence_file:
            try:
                os.remove(self._persistence_file)
            except FileNotFoundError:
                pass
            except OSError as exc:
                with self._lock:
                    self._persistence_error = str(exc)
        return self.status()

    def load(self) -> dict[str, object]:
        if not self._persistence_file or not os.path.isfile(self._persistence_file):
            return self.status()
        try:
            with open(self._persistence_file, "rb") as handle:
                protected = handle.read()
            payload = json.loads(_unprotect_windows_data(protected))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported encrypted Excel session format")
            self.configure(
                payload.get("headers"),
                tools_version_id=payload.get("tools_version_id"),
                persist=False,
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ) as exc:
            with self._lock:
                self._headers = {}
                self._tools_version_id = None
                self._configured_at = None
                self._expires_at = None
                self._persistence_error = str(exc)
        return self.status()

    def _save(self) -> None:
        if not self._persistence_file:
            return
        with self._lock:
            payload = {
                "version": 1,
                "headers": dict(self._headers),
                "tools_version_id": self._tools_version_id,
            }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        protected = _protect_windows_data(raw)
        directory = os.path.dirname(self._persistence_file)
        os.makedirs(directory, exist_ok=True)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".excel-session-",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._persistence_file)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def status(self) -> dict[str, object]:
        with self._lock:
            configured = bool(self._headers)
            tools_version_id = self._tools_version_id
            configured_at = self._configured_at
            expires_at = self._expires_at
            persistence_error = self._persistence_error
        expired = expires_at is not None and expires_at <= time.time()
        return {
            "configured": configured,
            "expired": expired,
            "configured_at": configured_at,
            "expires_at": expires_at,
            "model": MODEL_ID,
            "upstream_model": UPSTREAM_MODEL,
            "upstream_url": RESPONSES_URL,
            "tools_version_id": tools_version_id,
            "storage": (
                "memory-and-windows-dpapi"
                if self._persistence_file and os.path.isfile(self._persistence_file)
                else "memory-only"
            ),
            "persistence_supported": sys.platform == "win32",
            "persisted": bool(
                self._persistence_file and os.path.isfile(self._persistence_file)
            ),
            "persistence": "windows-dpapi"
            if sys.platform == "win32"
            else "unavailable",
            "persistence_error": persistence_error,
        }

    def tools_version_id(self) -> str | None:
        with self._lock:
            return self._tools_version_id

    def request_headers(self, *, stream: bool) -> dict[str, str]:
        with self._lock:
            headers = dict(self._headers)
            expires_at = self._expires_at
        if not headers:
            if sys.platform == "darwin":
                raise RuntimeError(
                    "ChatGPT Excel sign-in was not found. Open the ChatGPT task pane in Excel and sign in."
                )
            raise RuntimeError(
                "GPT Excel is not configured. Read the cached session from the signed-in Excel add-in, then retry."
            )
        if expires_at is not None and expires_at <= time.time():
            if sys.platform == "darwin":
                raise RuntimeError(
                    "The ChatGPT Excel token has expired. Refresh the ChatGPT Excel task pane, then retry."
                )
            raise RuntimeError(
                "The GPT Excel session has expired. Refresh the signed-in Excel add-in session, then read it again."
            )
        headers.update(
            {
                "accept": "text/event-stream" if stream else "application/json",
                "accept-encoding": "identity",
                "content-type": "application/json",
                "origin": "https://bps.openai.com",
            }
        )
        return headers


excel_session_store = ExcelSessionStore(SESSION_FILE)
