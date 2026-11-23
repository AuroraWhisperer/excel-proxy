"""Interactive device and browser OAuth helpers for the Excel/BPS application."""

import base64
import hashlib
import secrets
from urllib.parse import urlencode, urlsplit

from account_identity import BalanceError


CLIENT_ID = "app_fnr0pYvVwwFDocDumLG3H2Bp"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
DEVICE_API = "https://auth.openai.com/api/accounts/deviceauth"
APPROVAL_URL = f"{DEVICE_API}/authorize?client_id={CLIENT_ID}"
BROWSER_REDIRECT_URI = (
    "https://bps.openai.com/basispoints/extension/"
    "360590d7-f8f9-4d88-bf75-0edfe0a4b9f3/auth/callback"
)
_BROWSER_CALLBACK_PARTS = urlsplit(BROWSER_REDIRECT_URI)
MAX_CALLBACK_URL_LEN = 16384


def browser_authorize_url(state, code_challenge, *, offline_access=True):
    scope = (
        "openid offline_access email profile organization.read"
        if offline_access
        else "openid email profile organization.read"
    )
    return (
        AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": BROWSER_REDIRECT_URI,
                "response_type": "code",
                "scope": scope,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
            }
        )
    )


def matches_browser_callback(url):
    if not isinstance(url, str) or len(url) > MAX_CALLBACK_URL_LEN or "#" in url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == _BROWSER_CALLBACK_PARTS.scheme
        and parsed.hostname == _BROWSER_CALLBACK_PARTS.hostname
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == _BROWSER_CALLBACK_PARTS.path
    )


def request_code(client):
    response = client.post(f"{DEVICE_API}/usercode", json={"client_id": CLIENT_ID})
    if response.status_code != 200:
        raise BalanceError(f"Excel 设备授权暂时无法启动（HTTP {response.status_code}）。")
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid device authorization response")
    device, code = data.get("device_auth_id"), data.get("user_code")
    if not all(isinstance(value, str) and value.strip() for value in (device, code)):
        raise ValueError("Missing device authorization code")
    interval = int(str(data.get("interval", 5)))
    if not 1 <= interval <= 300:
        raise ValueError("Invalid device polling interval")
    return {"client_id": CLIENT_ID, "device_auth_id": device, "user_code": code}, interval


def poll_grant(client, device):
    response = client.post(f"{DEVICE_API}/token", json=device)
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Invalid device grant response")
    if response.status_code != 200:
        error = body.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        if response.status_code == 403 and code == "deviceauth_authorization_pending":
            return None
        raise BalanceError(f"Excel 设备授权未获批准（HTTP {response.status_code}），请重新登录。")
    verifier, challenge, code = (body.get(key) for key in ("code_verifier", "code_challenge", "authorization_code"))
    if not all(isinstance(value, str) and value for value in (verifier, challenge, code)) or not 43 <= len(verifier) <= 128:
        raise ValueError("Incomplete device grant")
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    if not secrets.compare_digest(digest, challenge.encode("ascii")):
        raise ValueError("Invalid device grant challenge")
    return {
        "grant_type": "authorization_code", "client_id": CLIENT_ID, "code": code,
        "redirect_uri": "https://auth.openai.com/basispoints/deviceauth/callback", "code_verifier": verifier,
    }
