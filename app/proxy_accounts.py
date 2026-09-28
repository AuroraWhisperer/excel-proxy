"""Encrypted, explicitly selected ChatGPT credentials for the BPS proxy."""

import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from account_identity import (
    BalanceError,
    MAX_ACCOUNTS,
    token_claims as _claims,
    normalize_account as _normalize_account,
    account_session_headers,
)
from account_login import refresh_credentials
from app_paths import user_config_dir
from windows_dpapi import (
    protect_data as _protect_windows_data,
    unprotect_data as _unprotect_windows_data,
)


def _normalize(payload):
    record_id, account = _normalize_account(payload)
    if not account["account_id"]:
        raise BalanceError("登录缺少 ChatGPT 账号标识，请重新登录。")
    claims = _claims(account["access_token"])
    auth = claims.get("https://api.openai.com/auth") or {}
    if isinstance(auth, dict) and auth.get("chatgpt_account_id") not in (
        None,
        account["account_id"],
    ):
        raise BalanceError("登录的账号标识不一致，请重新登录。")
    expires = claims.get("exp", payload.get("expires_at"))
    if (
        isinstance(expires, bool)
        or not isinstance(expires, (int, float))
        or not math.isfinite(expires)
        or expires <= 0
    ):
        raise BalanceError("登录缺少有效期，请重新登录。")
    refresh = payload.get("refresh_token", "")
    if not isinstance(refresh, str) or (
        refresh and not re.fullmatch(r"[!-~]{1,32768}", refresh)
    ):
        raise BalanceError("登录刷新凭据无效，请重新登录。")
    account.update(
        expires_at=expires,
        refresh_token=refresh,
        needs_login=payload.get("needs_login") is True,
    )
    return record_id, account


class ProxyAccountStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._loaded = False
        self._accounts = {}
        self._pending = {}
        self._active_id = None
        # Preserve the previous installation until the user explicitly selects
        # direct login or removes its active account. Never fall back thereafter.
        self._source = "excel"

    def _load(self):
        if self._loaded:
            return
        if self.path and self.path.exists():
            try:
                data = json.loads(_unprotect_windows_data(self.path.read_bytes()))
                if data["version"] != 1 or data["source"] not in (
                    "excel",
                    "oauth",
                    "none",
                ):
                    raise ValueError()
                rows = data["accounts"]
                if not isinstance(rows, list) or len(rows) > MAX_ACCOUNTS:
                    raise ValueError()
                accounts = dict(_normalize(row) for row in rows)
                pending_rows = data.get("pending", [])
                if (
                    not isinstance(pending_rows, list)
                    or len(pending_rows) > MAX_ACCOUNTS
                ):
                    raise ValueError()
                pending = dict(_normalize(row) for row in pending_rows)
                if not pending.keys() <= accounts.keys():
                    raise ValueError()
                active = data["active_id"]
                if (data["source"] == "oauth" and active not in accounts) or (
                    data["source"] != "oauth" and active is not None
                ):
                    raise ValueError()
                self._accounts, self._active_id, self._source = (
                    accounts,
                    active,
                    data["source"],
                )
                self._pending = pending
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                BalanceError,
            ):
                raise BalanceError(
                    "无法读取已保存的代理账号；不会自动改用其他账号。请检查本机存储权限。",
                    503,
                ) from None
        self._loaded = True

    def _commit(self, accounts, active_id, source, pending=None):
        pending = self._pending if pending is None else pending
        if self.path:
            temporary = None
            try:
                data = json.dumps(
                    {
                        "version": 1,
                        "accounts": list(accounts.values()),
                        "pending": list(pending.values()),
                        "active_id": active_id,
                        "source": source,
                    }
                ).encode()
                protected = _protect_windows_data(data)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(
                    prefix=".proxy-accounts-", dir=self.path.parent
                )
                with os.fdopen(fd, "wb") as file:
                    file.write(protected)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            except (OSError, RuntimeError):
                raise BalanceError(
                    "代理账号未保存：无法加密或写入本机文件。请检查 Windows 用户权限。",
                    503,
                ) from None
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
        self._accounts, self._active_id, self._source = accounts, active_id, source
        self._pending = pending

    def snapshot(self):
        with self._lock:
            self._load()
            return {
                "source": self._source,
                "active_id": self._active_id,
                "accounts": [
                    {
                        "id": key,
                        "name": row["name"],
                        "account_hint": "…" + row["account_id"][-6:],
                        "expires_at": row["expires_at"],
                        "expired": row["expires_at"] <= time.time()
                        or row["needs_login"],
                        "renewable": bool(row["refresh_token"])
                        and not row["needs_login"],
                        "active": key == self._active_id,
                        "pending": key in self._pending,
                        "needs_login": row["needs_login"],
                    }
                    for key, row in self._accounts.items()
                ],
            }

    def import_accounts(self, payload):
        key, row = _normalize(payload)
        if row["expires_at"] <= time.time():
            raise BalanceError("新登录凭据已过期，请重新登录。")
        with self._lock:
            self._load()
            updated = (
                self._accounts
                if key == self._active_id
                else {**self._accounts, key: row}
            )
            pending = {
                key_: value for key_, value in self._pending.items() if key_ != key
            }
            if key == self._active_id:
                pending[key] = row
            if len(updated) > MAX_ACCOUNTS:
                raise BalanceError("最多保存 50 个代理账号，请先移除不再使用的账号。")
            self._commit(updated, self._active_id, self._source, pending)
            return {**self.snapshot(), "imported_ids": [key]}

    def refresh_after_unauthorized(self, headers):
        """Renew only the selected identity whose access token was rejected."""
        with self._lock:
            self._load()
            record_id = self._active_id
            row = self._accounts.get(record_id)
            if (
                self._source != "oauth"
                or row is None
                or row["account_id"] != headers.get("chatgpt-account-id")
                or not headers.get("authorization")
            ):
                return None
        return self.headers_for(
            record_id,
            active=True,
            stream=headers.get("accept") == "text/event-stream",
            rejected_authorization=headers["authorization"],
        )

    def headers_for(
        self, record_id, *, stream=False, active=False, rejected_authorization=None
    ):
        # A single flight prevents rotating the same refresh token concurrently.
        # Network IO never holds the selection lock, so switching/removal works.
        with self._refresh_lock:
            with self._lock:
                self._load()
                if rejected_authorization is not None and (
                    self._source != "oauth" or self._active_id != record_id
                ):
                    raise BalanceError(
                        "代理账号已切换，请重试；不会使用其他账号重发。", 409
                    )
                staged = not active and record_id in self._pending
                original = (self._pending if staged else self._accounts).get(record_id)
                if original is None:
                    raise BalanceError("代理账号不存在，请刷新页面。", 404)
                row = dict(original)
            if row["needs_login"]:
                raise BalanceError("此账号授权已失效，请重新登录；不会切换账号。", 401)
            rejected = rejected_authorization == "Bearer " + row["access_token"]
            if rejected and not row["refresh_token"]:
                with self._lock:
                    if self._accounts.get(record_id) is original:
                        self._commit(
                            {**self._accounts, record_id: {**row, "needs_login": True}},
                            self._active_id,
                            self._source,
                        )
                raise BalanceError(
                    "此账号授权已失效且无法续期，请重新登录；不会切换账号。", 401
                )
            if (rejected or row["expires_at"] <= time.time() + 300) and row[
                "refresh_token"
            ]:
                try:
                    payload = refresh_credentials(row["refresh_token"])
                except BalanceError as exc:
                    if exc.status_code == 401:
                        with self._lock:
                            target = self._pending if staged else self._accounts
                            if target.get(record_id) is original:
                                updated = {
                                    **target,
                                    record_id: {**row, "needs_login": True},
                                }
                                self._commit(
                                    self._accounts if staged else updated,
                                    self._active_id,
                                    self._source,
                                    updated if staged else self._pending,
                                )
                    raise BalanceError(
                        "账号续期失败，请检查网络或重新登录此账号；不会切换账号。",
                        exc.status_code,
                    ) from None
                new_id, replacement = _normalize(
                    {
                        **payload,
                        "name": row["name"],
                        "refresh_token": payload.get("refresh_token")
                        or row["refresh_token"],
                    }
                )
                if new_id != record_id or replacement["expires_at"] <= time.time():
                    raise BalanceError(
                        "续期返回的账号或有效期不匹配，请重新登录。", 401
                    )
                with self._lock:
                    target = self._pending if staged else self._accounts
                    if target.get(record_id) is not original:
                        raise BalanceError("账号在续期期间发生变化，请重试。", 409)
                    updated = {**target, record_id: replacement}
                    self._commit(
                        self._accounts if staged else updated,
                        self._active_id,
                        self._source,
                        updated if staged else self._pending,
                    )
                row = replacement
            if row["expires_at"] <= time.time():
                raise BalanceError("当前代理账号已过期，请重新登录此账号。", 401)
            if rejected_authorization is not None:
                with self._lock:
                    if self._source != "oauth" or self._active_id != record_id:
                        raise BalanceError(
                            "代理账号在续期期间已切换，请重试；不会使用其他账号重发。",
                            409,
                        )
        return account_session_headers(row, stream=stream)

    def activate(self, record_id, authorization):
        with self._lock:
            self._load()
            row = self._pending.get(record_id) or self._accounts.get(record_id)
            if (
                row is None
                or row["needs_login"]
                or "Bearer " + row["access_token"] != authorization
                or row["expires_at"] <= time.time()
            ):
                raise BalanceError("待启用账号已变化，请重新验证后切换。", 409)
            self._commit(
                {**self._accounts, record_id: row},
                record_id,
                "oauth",
                {
                    key: value
                    for key, value in self._pending.items()
                    if key != record_id
                },
            )
            return self.snapshot()

    def use_excel(self):
        with self._lock:
            self._load()
            self._commit(self._accounts, None, "excel")
            return self.snapshot()

    def remove(self, record_id):
        with self._lock:
            self._load()
            if record_id not in self._accounts:
                raise BalanceError("代理账号不存在，请刷新页面。", 404)
            active = self._active_id == record_id
            self._commit(
                {key: row for key, row in self._accounts.items() if key != record_id},
                None if active else self._active_id,
                "none" if active else self._source,
                {key: row for key, row in self._pending.items() if key != record_id},
            )
            return self.snapshot()


proxy_account_store = ProxyAccountStore(
    Path(user_config_dir()) / "proxy-accounts.dpapi"
)
