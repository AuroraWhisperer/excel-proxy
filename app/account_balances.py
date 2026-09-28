"""Imported ChatGPT quota queries and explicitly confirmed reset-card actions.

Wire formats follow Sub2API's account_data.go, openai_quota_service.go and
openai_quota_reset_credits.go. No automatic reset-card consumption or token rotation.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import uuid4

import httpx

from app_paths import user_config_dir
from account_identity import (
    BalanceError,
    MAX_ACCOUNTS,
    MAX_BYTES,
    account_session_headers,
    normalize_account as _normalize_account,
)
from windows_dpapi import (
    protect_data as _protect_windows_data,
    unprotect_data as _unprotect_windows_data,
)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
RESET_CONSUME_URL = RESET_CREDITS_URL + "/consume"
CACHE_SECONDS = 60


def _number(value, maximum=1e15):
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        return None
    try:
        number = Decimal(str(value))
        return float(number) if number.is_finite() and 0 <= number <= maximum else None
    except InvalidOperation:
        return None


def normalize_reset_credits(payload):
    count, cards = None, None
    if isinstance(payload, list):
        cards = payload
    elif isinstance(payload, dict):
        count = _number(payload.get("available_count", payload.get("availableCount")))
        count = int(count) if count is not None and count.is_integer() else None
        for key in ("credits", "rate_limit_reset_credits", "items", "data"):
            if isinstance(payload.get(key), list):
                cards = payload[key]
                break
    if cards is not None and any(not isinstance(card, dict) for card in cards):
        cards = None
    dates, available = [], 0
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        if (
            card.get("status", "available") != "available"
            or card.get("reset_type", card.get("resetType", "codex_rate_limits"))
            != "codex_rate_limits"
        ):
            continue
        expiration = card.get("expires_at", card.get("expiresAt"))
        if isinstance(expiration, str):
            try:
                parsed = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    if parsed <= datetime.now(timezone.utc):
                        continue
                    dates.append(parsed.isoformat())
            except ValueError:
                pass
        available += 1
    return {
        "available_count": count
        if count is not None
        else available
        if cards is not None
        else None,
        "expires_at": sorted(dates),
    }


def normalize_usage(payload):
    if not isinstance(payload, dict):
        raise BalanceError("服务方返回了不支持的额度格式。")
    credits = payload.get("credits")
    credits = credits if isinstance(credits, dict) else {}
    windows = []
    limits = payload.get("rate_limit")
    for key, label in (
        ("primary_window", "主额度窗口"),
        ("secondary_window", "次额度窗口"),
    ):
        window = limits.get(key) if isinstance(limits, dict) else None
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"), 100)
        duration = _number(window.get("limit_window_seconds"))
        reset_at = _number(window.get("reset_at"), 253402300799)
        if reset_at is None:
            after = _number(window.get("reset_after_seconds"), 31536000)
            reset_at = time.time() + after if after is not None else None
        windows.append(
            {
                "kind": key,
                "label": label,
                "remaining_percent": 100 - used if used is not None else None,
                "window_seconds": duration,
                "resets_at": reset_at,
            }
        )
    plan = payload.get("plan_type")
    return {
        "balance": _number(credits.get("balance")),
        "unlimited": credits.get("unlimited") is True,
        "plan_type": plan[:40] if isinstance(plan, str) else None,
        "windows": windows,
        "reset_credits": normalize_reset_credits(
            payload.get("rate_limit_reset_credits")
        ),
        "warning": "",
    }


def account_key_from_headers(headers):
    """Tag real outbound requests without persisting account IDs or credentials."""
    headers = {str(key).lower(): value for key, value in (headers or {}).items()}
    authorization = headers.get("authorization", "")
    if not isinstance(authorization, str) or not authorization.lower().startswith(
        "bearer "
    ):
        return None
    try:
        key, _ = _normalize_account(
            {
                "access_token": authorization[7:],
                "account_id": headers.get("chatgpt-account-id")
                or headers.get("x-openai-account-id"),
            }
        )
        return key
    except BalanceError:
        return None


def quota_window_id(window):
    # Sub2API openAIAutoResetCycleSeed classifies by duration, not primary position.
    seconds = window.get("window_seconds")
    return (
        ("5h" if seconds <= 6 * 3600 else "7d")
        if seconds and seconds > 0
        else window.get("kind", "unknown")
    )


def advance_cycles(previous, quota, checked_at, reset_receipt=None):
    """A changed reset_at defines a new period; balance/percent drops do not."""
    state = copy.deepcopy(previous or {"windows": {}, "history": []})
    state.setdefault("windows", {})
    state.setdefault("history", [])
    for window in quota.get("windows", []):
        key = quota_window_id(window)
        reset_at, duration = window.get("resets_at"), window.get("window_seconds")
        old = state["windows"].get(key)
        remaining = window.get("remaining_percent")
        receipt_at = (reset_receipt or {}).get("redeemed_at")
        new_receipt = (
            (reset_receipt or {}).get("status") == "confirmed"
            and receipt_at
            and (not old or receipt_at > old.get("last_reset_receipt_at", 0))
        )
        changed = bool(
            old
            and reset_at
            and old.get("resets_at")
            and abs(reset_at - old["resets_at"]) > 2
        )
        recovered = bool(
            old
            and remaining is not None
            and old.get("remaining_percent") is not None
            and remaining > old["remaining_percent"]
        )
        manual = bool(
            new_receipt
            and reset_receipt.get("windows_reset", 0) > 0
            and (
                changed
                or recovered
                or reset_receipt["windows_reset"] >= len(quota.get("windows", []))
            )
        )
        # A retry may deliver the receipt after a refresh already saw that reset.
        # Upgrade its attribution rather than inventing another consumed card.
        acknowledged = False
        if (
            manual
            and changed
            and duration
            and receipt_at < reset_at - duration
            and checked_at >= old["resets_at"]
        ):
            acknowledged = True
        if manual and old and not changed:
            prior = next(
                (
                    entry
                    for entry in reversed(state["history"])
                    if entry["window_id"] == key
                ),
                None,
            )
            if old.get("started_at") is not None and receipt_at < old["started_at"]:
                acknowledged = True
            elif (
                old["reason"] in ("external_reset", "scheduled")
                and prior
                and prior["checked_at"] <= receipt_at <= old["observed_from"]
            ):
                old.update(
                    reason="manual_card",
                    started_at=max(old.get("started_at") or receipt_at, receipt_at),
                )
                prior["closed_by"] = "manual_card"
                acknowledged = True
        manual = manual and not acknowledged
        resized = bool(
            old
            and duration
            and old.get("window_seconds")
            and duration != old["window_seconds"]
        )
        new_period = old is None or changed or manual or resized
        if new_period:
            reason = "first_seen"
            if old:
                if manual:
                    reason = "manual_card"
                elif resized:
                    reason = "window_changed"
                elif checked_at >= old["resets_at"] and reset_at > old["resets_at"]:
                    reason = "scheduled"
                else:
                    reason = "external_reset"
                state["history"].append(
                    {
                        **old,
                        "window_id": key,
                        "closed_by": reason,
                        "detected_at": checked_at,
                    }
                )
            start = reset_at - duration if reset_at and duration else None
            if manual:
                start = max(start or receipt_at, receipt_at)
            elif old and changed and reason == "external_reset":
                # Never include old-cycle traffic when a provider keeps its old end.
                start = max(start or checked_at, old["checked_at"])
            current = {
                "number": old["number"] + 1 if old else 1,
                "reason": reason,
                "started_at": start,
                "observed_from": checked_at,
                "last_reset_receipt_at": receipt_at
                if manual
                else (old or {}).get("last_reset_receipt_at", 0),
            }
        else:
            current = old
            if current.get("started_at") is None and reset_at and duration:
                current["started_at"] = reset_at - duration
        if new_receipt:
            current["last_reset_receipt_at"] = receipt_at
        current.update(
            remaining_percent=remaining,
            used_percent=100 - remaining if remaining is not None else None,
            window_seconds=duration or current.get("window_seconds"),
            resets_at=reset_at or current.get("resets_at"),
            checked_at=checked_at,
        )
        state["windows"][key] = current
    state["history"] = state["history"][-20:]
    return state


def public_cycles(state, now=None):
    result = copy.deepcopy(state or {"windows": {}, "history": []})
    now = time.time() if now is None else now
    for current in result["windows"].values():
        expired = bool(current.get("resets_at") and now >= current["resets_at"])
        current["awaiting_refresh"] = expired
        if expired:
            # Same display rule as Sub2API buildCodexUsageProgressFromExtra.
            current["used_percent"] = 0
            current["remaining_percent"] = 100
    return result


def _client():
    return httpx.Client(timeout=20, follow_redirects=False)


def _fetch_json(client, url, headers, *, body=None):
    try:
        with client.stream(
            "POST" if body is not None else "GET",
            url,
            headers=headers,
            **({"json": body} if body is not None else {}),
        ) as response:
            if response.status_code != 200:
                message = {
                    401: "登录已过期，请重新导入账号 JSON。",
                    403: "服务方拒绝查询，请检查账号权限或网络后重试。",
                    429: "查询过于频繁，请稍后重试。",
                }.get(response.status_code, "服务方暂时无法提供额度，请稍后重试。")
                raise BalanceError(message)
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise BalanceError("服务方额度响应过大，已停止读取。")
            return json.loads(body)
    except httpx.HTTPError:
        raise BalanceError("无法连接额度服务，请检查网络后重试。") from None
    except (ValueError, UnicodeError, RecursionError):
        raise BalanceError("服务方没有返回有效的额度 JSON。") from None


def _quota_headers(account):
    headers = {
        "Authorization": "Bearer " + account["access_token"],
        "Accept": "application/json",
        "OpenAI-Beta": "codex-1",
        "originator": "Codex Desktop",
    }
    if account["account_id"]:
        headers["ChatGPT-Account-Id"] = account["account_id"]
    return headers


def fetch_quota(account):
    headers = _quota_headers(account)
    with _client() as client:
        payload = _fetch_json(client, USAGE_URL, headers)
        if (
            isinstance(payload, dict)
            and payload.get("account_id")
            and account["account_id"]
            and payload["account_id"] != account["account_id"]
        ):
            raise BalanceError("返回的账号与导入账号不一致，未显示余额。")
        quota = normalize_usage(payload)
        try:
            details = normalize_reset_credits(
                _fetch_json(client, RESET_CREDITS_URL, headers)
            )
            if details["available_count"] is not None:
                quota["reset_credits"] = details
            else:
                quota["warning"] = "重置卡详情未返回；如有数量，仅来自本次用量响应。"
        except BalanceError:
            quota["warning"] = "重置卡详情查询失败；如有数量，仅来自本次用量响应。"
        return quota


def consume_reset_credit(account, redeem_request_id):
    with _client() as client:
        payload = _fetch_json(
            client,
            RESET_CONSUME_URL,
            _quota_headers(account),
            body={"redeem_request_id": redeem_request_id},
        )
    if not isinstance(payload, dict):
        raise BalanceError("无法确认重置结果，请核对上次操作。")
    if payload.get("code") == "no_credit":
        return {"status": "no_credit", "windows_reset": 0}
    count = payload.get("windows_reset")
    if (
        payload.get("code") != "success"
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        raise BalanceError("服务方尚未确认重置成功，请核对上次操作。")
    redeemed_at = None
    credit = payload.get("credit")
    if isinstance(credit, dict) and isinstance(credit.get("redeemed_at"), str):
        try:
            timestamp = datetime.fromisoformat(
                credit["redeemed_at"].replace("Z", "+00:00")
            )
            if timestamp.tzinfo:
                redeemed_at = timestamp.timestamp()
        except ValueError:
            pass
    return {
        "status": "confirmed",
        "windows_reset": count,
        "redeemed_at": redeemed_at or time.time(),
    }


class AccountBalanceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._query_lock = threading.Lock()
        self._accounts = {}
        self._snapshots = {}
        self._cycles = {}
        self._resets = {}
        self._resetting_id = None
        self._next_check = {}
        self._rate = None
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        if self.path and self.path.exists():
            try:
                payload = json.loads(_unprotect_windows_data(self.path.read_bytes()))
                if payload.get("version") not in (1, 2):
                    raise ValueError()
                accounts = payload["accounts"]
                if not isinstance(accounts, list) or len(accounts) > MAX_ACCOUNTS:
                    raise ValueError()
                self._accounts = dict(_normalize_account(item) for item in accounts)
                self._rate = _number(payload.get("credit_unit_usd"), 1e6)
                self._cycles = {
                    key: value
                    for key, value in payload.get("cycles", {}).items()
                    if key in self._accounts
                }
                self._resets = {
                    key: value
                    for key, value in payload.get("resets", {}).items()
                    if key in self._accounts
                }
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                BalanceError,
            ):
                raise BalanceError(
                    "无法读取本机加密账号文件，原文件未被覆盖。请使用原 Windows 用户打开。",
                    503,
                ) from None
        self._loaded = True

    def _save(self, accounts, rate, *, cycles=None, resets=None):
        if not self.path:
            return
        temporary = None
        try:
            data = json.dumps(
                {
                    "version": 2,
                    "accounts": list(accounts.values()),
                    "credit_unit_usd": rate,
                    "cycles": {
                        key: value
                        for key, value in (
                            self._cycles if cycles is None else cycles
                        ).items()
                        if key in accounts
                    },
                    "resets": {
                        key: value
                        for key, value in (
                            self._resets if resets is None else resets
                        ).items()
                        if key in accounts
                    },
                }
            ).encode()
            protected = _protect_windows_data(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".account-balances-", dir=self.path.parent
            )
            with os.fdopen(fd, "wb") as file:
                file.write(protected)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except (OSError, RuntimeError):
            raise BalanceError(
                "账号未保存：无法加密或写入本机文件。请检查 Windows 用户权限。", 503
            ) from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def snapshot(self):
        with self._lock:
            self._load()
            rows = []
            for record_id, account in self._accounts.items():
                snapshot = copy.deepcopy(
                    self._snapshots.get(
                        record_id,
                        {
                            "status": "not_checked",
                            "quota": None,
                            "checked_at": None,
                            "stale": False,
                        },
                    )
                )
                quota = snapshot.get("quota") or {}
                balance = quota.get("balance")
                usd = (
                    float(Decimal(str(balance)) * Decimal(str(self._rate)))
                    if balance is not None
                    and self._rate is not None
                    and not quota.get("unlimited")
                    else None
                )
                rows.append(
                    {
                        "id": record_id,
                        "name": account["name"],
                        "account_hint": "…" + account["account_id"][-6:]
                        if account["account_id"]
                        else "未提供账号 ID",
                        **snapshot,
                        "balance_usd": usd,
                        "cycles": public_cycles(self._cycles.get(record_id)),
                        "reset_action": {
                            key: value
                            for key, value in self._resets.get(record_id, {}).items()
                            if key != "redeem_request_id"
                        },
                    }
                )
            return {
                "accounts": rows,
                "credit_unit_usd": self._rate,
                "persisted": self.path is not None,
            }

    def import_accounts(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        items = (
            payload.get("accounts", [payload]) if isinstance(payload, dict) else payload
        )
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ACCOUNTS:
            raise BalanceError("请选择包含 1–50 个账号的 JSON 文件。")
        imported = dict(_normalize_account(item) for item in items)
        with self._lock:
            self._load()
            if self._resetting_id in imported:
                raise BalanceError("该账号正在重置，请等待操作完成后再导入。", 409)
            accounts = {**self._accounts, **imported}
            if len(accounts) > MAX_ACCOUNTS:
                raise BalanceError("最多保存 50 个账号，请先移除不再使用的账号。")
            self._save(accounts, self._rate)
            self._accounts = accounts
            for record_id in imported:
                self._snapshots.pop(record_id, None)
                self._next_check.pop(record_id, None)
            return {**self.snapshot(), "imported_ids": list(imported)}

    def set_rate(self, value):
        rate = _number(value, 1e6) if value is not None else None
        if value is not None and (rate is None or rate <= 0):
            raise BalanceError(
                "请输入大于 0 且不超过 1,000,000 的美元单价，或留空关闭折算。"
            )
        with self._lock:
            self._load()
            self._save(self._accounts, rate)
            self._rate = rate
            return self.snapshot()

    def remove(self, record_id):
        with self._lock:
            self._load()
            if self._resetting_id == record_id:
                raise BalanceError("该账号正在重置，请等待操作完成后再移除。", 409)
            if record_id not in self._accounts:
                raise BalanceError("账号不存在，请刷新页面。", 404)
            accounts = {
                key: value for key, value in self._accounts.items() if key != record_id
            }
            self._save(accounts, self._rate)
            self._accounts = accounts
            self._snapshots.pop(record_id, None)
            self._next_check.pop(record_id, None)
            self._cycles.pop(record_id, None)
            self._resets.pop(record_id, None)
            return self.snapshot()

    def headers_for(self, record_id):
        with self._lock:
            self._load()
            if record_id not in self._accounts:
                raise BalanceError("账号不存在，请刷新页面。", 404)
            return account_session_headers(self._accounts[record_id])

    def refresh(self, record_id):
        if not self._query_lock.acquire(blocking=False):
            raise BalanceError("已有账号正在查询，请稍后再试。", 409)
        try:
            with self._lock:
                self._load()
                if record_id not in self._accounts:
                    raise BalanceError("账号不存在，请刷新页面。", 404)
                if time.monotonic() < self._next_check.get(record_id, 0):
                    return self.snapshot()
                account = self._accounts[record_id]
                previous = self._snapshots.get(record_id, {})
            try:
                result = {
                    "status": "ready",
                    "quota": fetch_quota(account),
                    "checked_at": time.time(),
                    "stale": False,
                }
            except BalanceError as exc:
                result = {
                    "status": "error",
                    "message": str(exc),
                    "quota": previous.get("quota"),
                    "checked_at": previous.get("checked_at"),
                    "stale": previous.get("quota") is not None,
                }
            with self._lock:
                if self._accounts.get(record_id) is account:
                    if result["status"] == "ready":
                        cycles = {
                            **self._cycles,
                            record_id: advance_cycles(
                                self._cycles.get(record_id),
                                result["quota"],
                                result["checked_at"],
                                self._resets.get(record_id),
                            ),
                        }
                        self._save(self._accounts, self._rate, cycles=cycles)
                        self._cycles = cycles
                    self._snapshots[record_id] = result
                    self._next_check[record_id] = time.monotonic() + CACHE_SECONDS
                return self.snapshot()
        finally:
            self._query_lock.release()

    def reset_credit(self, record_id, expected_checked_at):
        if not self._query_lock.acquire(blocking=False):
            raise BalanceError("已有查询或重置正在进行，请稍后再试。", 409)
        try:
            with self._lock:
                self._load()
                if record_id not in self._accounts:
                    raise BalanceError("账号不存在，请刷新页面。", 404)
                account = self._accounts[record_id]
                previous = self._snapshots.get(record_id, {})
                action = self._resets.get(record_id, {})
                if action.get("status") != "pending":
                    if (
                        expected_checked_at is None
                        or expected_checked_at != previous.get("checked_at")
                    ):
                        raise BalanceError(
                            "账号状态已更新，请刷新页面核对上次重置结果，再决定是否用卡。",
                            409,
                        )
                    count = (
                        (previous.get("quota") or {}).get("reset_credits") or {}
                    ).get("available_count")
                    if (
                        previous.get("status") != "ready"
                        or time.time() - previous.get("checked_at", 0) > CACHE_SECONDS
                        or count is None
                        or count <= 0
                    ):
                        raise BalanceError(
                            "请先查询额度，确认有可用重置卡后再重置。", 409
                        )
                    if (
                        action.get("status") == "confirmed"
                        and previous["checked_at"] <= action["redeemed_at"]
                    ):
                        raise BalanceError(
                            "上次用卡已成功，请先刷新额度，勿重复使用。", 409
                        )
                    action = {
                        "status": "pending",
                        "redeem_request_id": str(uuid4()),
                        "attempted_at": time.time(),
                    }
                    resets = {**self._resets, record_id: action}
                    self._save(self._accounts, self._rate, resets=resets)
                    self._resets = resets
                self._resetting_id = record_id
            try:
                receipt = consume_reset_credit(account, action["redeem_request_id"])
            except BalanceError:
                with self._lock:
                    self._snapshots[record_id] = {
                        **previous,
                        "status": "error",
                        "stale": True,
                        "message": "重置结果尚未确认，请选择「核对重置」。重试会沿用上次请求编号，避免重复用卡。",
                    }
                return self.snapshot()
            with self._lock:
                # Persist the receipt before querying again: a query failure must never trigger another spend.
                self._resets[record_id] = {**action, **receipt}
                self._save(self._accounts, self._rate)
            try:
                quota = fetch_quota(account)
                result = {
                    "status": "ready",
                    "quota": quota,
                    "checked_at": time.time(),
                    "stale": False,
                }
            except BalanceError:
                result = {
                    **previous,
                    "status": "error",
                    "stale": True,
                    "message": "用卡结果已保存，但额度更新失败。请重新查询额度，不要重复用卡。",
                }
            with self._lock:
                if result["status"] == "ready":
                    cycles = {
                        **self._cycles,
                        record_id: advance_cycles(
                            self._cycles.get(record_id),
                            result["quota"],
                            result["checked_at"],
                            self._resets[record_id],
                        ),
                    }
                    self._save(self._accounts, self._rate, cycles=cycles)
                    self._cycles = cycles
                self._snapshots[record_id] = result
                self._next_check.pop(record_id, None)
                return self.snapshot()
        finally:
            self._resetting_id = None
            self._query_lock.release()


balance_store = AccountBalanceStore(Path(user_config_dir()) / "account-balances.dpapi")
