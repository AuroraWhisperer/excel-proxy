"""Shared Excel HTTP client configuration, lazy creation and process shutdown."""

import asyncio
import atexit
import os
import sys
import threading

import httpx

from constants import DEFAULT_UPSTREAM_TIMEOUT_SECONDS


def configured_upstream_timeout_seconds() -> int:
    raw = str(os.environ.get("GHCP_UPSTREAM_TIMEOUT_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        print(
            f"Warning: ignoring invalid GHCP_UPSTREAM_TIMEOUT_SECONDS={raw!r}; using {DEFAULT_UPSTREAM_TIMEOUT_SECONDS}",
            file=sys.stderr,
            flush=True,
        )
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    if value <= 0:
        print(
            f"Warning: GHCP_UPSTREAM_TIMEOUT_SECONDS must be > 0; using {DEFAULT_UPSTREAM_TIMEOUT_SECONDS}",
            file=sys.stderr,
            flush=True,
        )
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    return value


def _first_non_empty_env(names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for name in names:
        raw = os.environ.get(name)
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if value:
            return value, name
    return None, None


def _apply_upstream_proxy_env_aliases() -> tuple[str, ...]:
    """Apply GHCP-specific proxy aliases to standard HTTP(S)_PROXY keys.

    httpx reads standard proxy environment variables when ``trust_env=True``.
    This helper lets operators provide GHCP-specific aliases (for example in a
    launchd plist) without overwriting already-defined standard values.
    """

    applied: list[str] = []
    https_proxy, _ = _first_non_empty_env(("HTTPS_PROXY", "https_proxy"))
    http_proxy, _ = _first_non_empty_env(("HTTP_PROXY", "http_proxy"))
    no_proxy, _ = _first_non_empty_env(("NO_PROXY", "no_proxy"))
    ghcp_proxy, _ = _first_non_empty_env(("GHCP_UPSTREAM_PROXY",))
    ghcp_https_proxy, _ = _first_non_empty_env(("GHCP_HTTPS_PROXY",))
    ghcp_http_proxy, _ = _first_non_empty_env(("GHCP_HTTP_PROXY",))
    ghcp_no_proxy, _ = _first_non_empty_env(("GHCP_NO_PROXY",))

    if https_proxy is None:
        chosen_https = ghcp_https_proxy or ghcp_proxy
        if chosen_https:
            os.environ["HTTPS_PROXY"] = chosen_https
            os.environ.setdefault("https_proxy", chosen_https)
            applied.append("HTTPS_PROXY")
    if http_proxy is None:
        chosen_http = ghcp_http_proxy or ghcp_proxy
        if chosen_http:
            os.environ["HTTP_PROXY"] = chosen_http
            os.environ.setdefault("http_proxy", chosen_http)
            applied.append("HTTP_PROXY")
    if no_proxy is None and ghcp_no_proxy:
        os.environ["NO_PROXY"] = ghcp_no_proxy
        os.environ.setdefault("no_proxy", ghcp_no_proxy)
        applied.append("NO_PROXY")

    return tuple(applied)


def _optional_env_bool(name: str) -> bool | None:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    print(
        f"Warning: ignoring invalid {name}={raw!r}; expected true/false",
        file=sys.stderr,
        flush=True,
    )
    return None


def _upstream_proxy_configured() -> bool:
    https_proxy, _ = _first_non_empty_env(
        ("HTTPS_PROXY", "https_proxy", "GHCP_HTTPS_PROXY", "GHCP_UPSTREAM_PROXY"),
    )
    http_proxy, _ = _first_non_empty_env(
        ("HTTP_PROXY", "http_proxy", "GHCP_HTTP_PROXY", "GHCP_UPSTREAM_PROXY"),
    )
    return bool(https_proxy or http_proxy)


def _configured_upstream_tls_verify(proxy_configured: bool) -> tuple[bool, str]:
    explicit = _optional_env_bool("GHCP_UPSTREAM_TLS_VERIFY")
    if explicit is not None:
        return explicit, "GHCP_UPSTREAM_TLS_VERIFY"
    if proxy_configured:
        # Enterprise HTTPS interception proxies often terminate TLS with an
        # internal CA that isn't present in certifi-based trust stores.
        return False, "proxy_default"
    return True, "default"


def _configured_upstream_http2(proxy_configured: bool) -> tuple[bool, str]:
    explicit = _optional_env_bool("GHCP_UPSTREAM_HTTP2")
    if explicit is not None:
        return explicit, "GHCP_UPSTREAM_HTTP2"
    if proxy_configured:
        return False, "proxy_default"
    return True, "default"


_EXCEL_UPSTREAM_CLIENT: "httpx.AsyncClient | None" = None
_UPSTREAM_CLIENT_LOCK = threading.Lock()
_UPSTREAM_CLIENT_SHUTDOWN_REGISTERED = False


def _build_upstream_client(
    *,
    http2_override: bool | None = None,
) -> "httpx.AsyncClient":
    proxy_aliases = _apply_upstream_proxy_env_aliases()
    if proxy_aliases:
        print(
            f"Configured upstream network environment aliases: {', '.join(proxy_aliases)}",
            flush=True,
        )
    proxy_configured = _upstream_proxy_configured()
    tls_verify, tls_verify_source = _configured_upstream_tls_verify(proxy_configured)
    if http2_override is None:
        upstream_http2, upstream_http2_source = _configured_upstream_http2(
            proxy_configured
        )
    else:
        upstream_http2, upstream_http2_source = http2_override, "client_override"
    if not tls_verify and tls_verify_source == "proxy_default":
        print(
            "Upstream network forwarding detected: defaulting GHCP upstream TLS verification off. "
            "Set GHCP_UPSTREAM_TLS_VERIFY=1 once a trusted network CA bundle is configured.",
            flush=True,
        )
    elif not tls_verify:
        print(
            "GHCP_UPSTREAM_TLS_VERIFY disabled: upstream TLS certificates will not be validated.",
            flush=True,
        )
    if not upstream_http2 and upstream_http2_source == "proxy_default":
        print(
            "Upstream network forwarding detected: defaulting GHCP upstream HTTP/2 off for compatibility.",
            flush=True,
        )
    timeout = httpx.Timeout(configured_upstream_timeout_seconds())
    limits = httpx.Limits(
        max_connections=8,
        max_keepalive_connections=4,
        keepalive_expiry=300.0,
    )
    try:
        return httpx.AsyncClient(
            http2=upstream_http2,
            timeout=timeout,
            limits=limits,
            verify=tls_verify,
            trust_env=True,
        )
    except (ImportError, RuntimeError):
        return httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            verify=tls_verify,
            trust_env=True,
        )


def _ensure_upstream_client_shutdown_registered() -> None:
    global _UPSTREAM_CLIENT_SHUTDOWN_REGISTERED
    if not _UPSTREAM_CLIENT_SHUTDOWN_REGISTERED:
        atexit.register(shutdown_upstream_client)
        _UPSTREAM_CLIENT_SHUTDOWN_REGISTERED = True


def get_excel_upstream_client() -> "httpx.AsyncClient":
    """Reuse the Excel HTTP/1.1 transport across requests."""
    global _EXCEL_UPSTREAM_CLIENT
    if _EXCEL_UPSTREAM_CLIENT is not None:
        return _EXCEL_UPSTREAM_CLIENT
    with _UPSTREAM_CLIENT_LOCK:
        if _EXCEL_UPSTREAM_CLIENT is None:
            _EXCEL_UPSTREAM_CLIENT = _build_upstream_client(http2_override=False)
            _ensure_upstream_client_shutdown_registered()
    return _EXCEL_UPSTREAM_CLIENT


def shutdown_upstream_client() -> None:
    global _EXCEL_UPSTREAM_CLIENT
    clients = [client for client in (_EXCEL_UPSTREAM_CLIENT,) if client is not None]
    _EXCEL_UPSTREAM_CLIENT = None
    if not clients:
        return
    try:
        loop = asyncio.new_event_loop()
        try:
            for client in clients:
                loop.run_until_complete(client.aclose())
        finally:
            loop.close()
    except Exception:
        pass
