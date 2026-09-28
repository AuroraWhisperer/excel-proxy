"""Keep the local API and dashboard accessible only from this computer."""

import ipaddress
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def _origin(value: str) -> tuple | None:
    if any(character.isspace() for character in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


class LocalAccessMiddleware:
    """Validate peer, Host and browser origin without wrapping response streams."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [
            value.decode("latin-1") for key, value in headers if key.lower() == b"host"
        ]
        origins = [
            value.decode("latin-1")
            for key, value in headers
            if key.lower() == b"origin"
        ]
        sites = [
            value.lower() for key, value in headers if key.lower() == b"sec-fetch-site"
        ]
        peer = scope.get("client")
        expected = (
            _origin(f"{scope.get('scheme', 'http')}://{hosts[0]}")
            if len(hosts) == 1
            else None
        )
        allowed = (
            peer
            and _is_loopback(peer[0])
            and expected
            and _is_loopback(expected[1])
            and (not origins or (len(origins) == 1 and _origin(origins[0]) == expected))
            and all(site in {b"same-origin", b"none"} for site in sites)
        )
        if allowed:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            response = JSONResponse(
                {
                    "error": {
                        "message": "Use the local Excel connection dashboard or a client on this computer.",
                        "type": "permission_error",
                        "code": "local_access_required",
                        "param": None,
                    }
                },
                status_code=403,
            )
            await response(scope, receive, send)
