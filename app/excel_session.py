"""Excel upstream endpoint constants and default client headers."""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlsplit


_endpoint = urlsplit(
    os.environ.get(
        "GHCP_EXCEL_RESPONSES_URL", "wss://bps.openai.com/basispoints/api/responses"
    ).strip()
)
if _endpoint.hostname == "bps.openai.com" and _endpoint.scheme == "https":
    _endpoint = _endpoint._replace(scheme="wss")
RESPONSES_URL = _endpoint.geturl()
HTTP_API_BASE_URL = _endpoint._replace(
    scheme={"wss": "https", "ws": "http"}.get(_endpoint.scheme, _endpoint.scheme),
    path=_endpoint.path.rsplit("/", 1)[0],
    query="",
    fragment="",
).geturl()

TOOLS_VERSION_METADATA_KEY = "bps_tools_version_id"

TOOLS_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,160}$")

_OFFICE_PLATFORM = "Mac" if sys.platform == "darwin" else "PC"

DEFAULT_CLIENT_HEADERS = {
    "x-basispoints-auth-mode": "chatgpt",
    "x-openai-internal-basispoints-client-agent-profile": "excel",
    "x-openai-internal-basispoints-client-editor": "excel",
    "x-openai-internal-basispoints-client-host": "office",
    "x-openai-internal-basispoints-client-platform": "excel",
    "x-openai-internal-basispoints-client-platform-class": _OFFICE_PLATFORM,
    "x-openai-internal-basispoints-client-product": "basispoints-excel-plugin",
    "x-openai-internal-basispoints-client-runtime": "desktop",
    "x-openai-internal-basispoints-office-host": "Excel",
    "x-openai-internal-basispoints-office-platform": _OFFICE_PLATFORM,
    "x-openai-internal-basispoints-browser-name": "unknown",
    "x-stainless-arch": "unknown",
    "x-stainless-lang": "js",
    "x-stainless-os": "Unknown",
    "x-stainless-package-version": "6.31.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "browser:chrome",
}
