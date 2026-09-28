"""Shared account identity, validation and request-header contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import re

from excel_session import DEFAULT_CLIENT_HEADERS


MAX_BYTES = 1024 * 1024

MAX_ACCOUNTS = 50


class BalanceError(Exception):
    """Only fixed, credential-free messages may cross the API boundary."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def token_claims(token):
    # JWT claims are unverified display/routing hints, never authentication.
    try:
        segment = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        )
        return claims if isinstance(claims, dict) else {}
    except (ValueError, IndexError, UnicodeError, RecursionError):
        return {}


def account_email(item, credentials, claims):
    candidates = []
    id_token = credentials.get("id_token") or item.get("id_token")
    for source in (claims, token_claims(id_token) if isinstance(id_token, str) else {}):
        profile = source.get("https://api.openai.com/profile")
        if isinstance(profile, dict):
            candidates.append(profile.get("email"))
        candidates.append(source.get("email"))
    candidates.extend((credentials.get("email"), item.get("email"), item.get("name")))
    for value in candidates:
        if isinstance(value, str):
            email = value.strip()
            if len(email) <= 254 and re.fullmatch(
                r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email
            ):
                return email
    return ""


def normalize_account(item):
    if not isinstance(item, dict) or item.get("platform", "openai") != "openai":
        raise BalanceError(
            "仅支持 OpenAI 账号 JSON，请移除其他平台的账号后重试。"
        )
    credentials = item.get("credentials", item.get("tokens", item))
    if not isinstance(credentials, dict):
        raise BalanceError("账号凭据必须是 JSON 对象。")
    token = credentials.get("access_token")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,32768}", token):
        raise BalanceError(
            "账号缺少有效 access_token；只含 refresh_token 或 API Key 的文件不支持余额查询。"
        )
    if token.startswith("sk-"):
        raise BalanceError("API Key 不支持订阅余额查询，请导入 OpenAI OAuth 账号。")
    claims = token_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    account_id = (
        credentials.get("chatgpt_account_id")
        or credentials.get("account_id")
        or item.get("account_id")
        or auth.get("chatgpt_account_id", "")
    )
    if not isinstance(account_id, str) or (
        account_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", account_id)
    ):
        raise BalanceError("账号 ID 格式无效，请检查导出文件。")
    name = account_email(item, credentials, claims) or "邮箱未提供"
    user_id = (
        credentials.get("chatgpt_user_id")
        or credentials.get("user_id")
        or auth.get("chatgpt_user_id")
        or claims.get("sub", "")
    )
    if not isinstance(user_id, str) or len(user_id) > 200:
        raise BalanceError("用户 ID 格式无效，请检查导出文件。")
    identity = json.dumps([account_id, user_id]) if account_id else token
    record_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return record_id, {
        "name": name,
        "access_token": token,
        "account_id": account_id,
        "user_id": user_id,
    }


def account_session_headers(account, *, stream=False):
    """Build the Excel/BPS request profile without selecting a proxy account."""
    headers = {
        **DEFAULT_CLIENT_HEADERS,
        "authorization": "Bearer " + account["access_token"],
        "chatgpt-account-id": account["account_id"],
        "x-openai-account-id": account["account_id"],
        "x-basispoints-auth-mode": "chatgpt",
        "content-type": "application/json",
        "origin": "https://bps.openai.com",
        "accept-encoding": "identity",
        "accept": "text/event-stream" if stream else "application/json",
    }
    auth = token_claims(account["access_token"]).get("https://api.openai.com/auth")
    user_id = auth.get("chatgpt_account_user_id") if isinstance(auth, dict) else None
    if isinstance(user_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", user_id):
        headers["x-openai-account-user-id"] = user_id
    return headers
