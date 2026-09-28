"""Read Codex subscription limits without starting model turns or exposing credentials."""

from __future__ import annotations

from contextlib import suppress
import copy
import json
import math
import os
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time


QUERY_TIMEOUT_SECONDS = 20
CACHE_SECONDS = 60


class QuotaError(Exception):
    """A safe, user-facing failure; never include raw subprocess output."""


def find_codex_executable() -> str:
    override = os.environ.get("CODEX_BIN")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and (
            sys.platform != "win32" or candidate.suffix.lower() == ".exe"
        ):
            return str(candidate.resolve())
        raise QuotaError("CODEX_BIN 无效，请将它设置为 Codex 原生程序的完整路径。")
    executable = shutil.which("codex.exe" if sys.platform == "win32" else "codex")
    if executable:
        return executable
    if sys.platform == "win32":
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        triple = "aarch64" if arch == "arm64" else "x86_64"
        suffix = Path(
            f"@openai/codex-win32-{arch}/vendor/{triple}-pc-windows-msvc/bin/codex.exe"
        )
        for directory in os.get_exec_path():
            for prefix in ("node_modules/@openai/codex/node_modules", "node_modules"):
                candidate = Path(directory) / prefix / suffix
                if candidate.is_file():
                    return str(candidate)
    raise QuotaError(
        "未找到 Codex CLI。请安装并使用订阅账号登录，或设置 CODEX_BIN 后重启服务。"
    )


def _number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def normalize_quota(response: dict, plan_type=None) -> dict:
    buckets = response.get("rateLimitsByLimitId")
    if buckets is None:
        legacy = response.get("rateLimits")
        buckets = (
            {legacy.get("limitId") or "codex": legacy}
            if isinstance(legacy, dict)
            else {}
        )
    if not isinstance(buckets, dict):
        raise QuotaError("Codex 返回了不支持的额度格式，请更新 Codex CLI 后重试。")
    windows = []
    for limit_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        label = bucket.get("limitName") or limit_id
        for kind in ("primary", "secondary"):
            window = bucket.get(kind)
            window = window if isinstance(window, dict) else {}
            used, duration, resets = (
                window.get(key)
                for key in ("usedPercent", "windowDurationMins", "resetsAt")
            )
            valid = (
                _number(used)
                and 0 <= used <= 100
                and _number(duration)
                and duration > 0
            )
            windows.append(
                {
                    "limit_id": str(limit_id)[:100],
                    "limit_name": str(label)[:100],
                    "kind": kind,
                    "used_percent": used if valid else None,
                    "remaining_percent": 100 - used if valid else None,
                    "window_minutes": duration if valid else None,
                    "resets_at": resets if _number(resets) and resets > 0 else None,
                }
            )
    core = buckets.get("codex") or {}
    credits = core.get("credits") if isinstance(core, dict) else None
    credit_summary = None
    if isinstance(credits, dict):
        balance = credits.get("balance")
        try:
            balance = float(balance) if not isinstance(balance, bool) else None
        except (ValueError, TypeError, OverflowError):
            balance = None
        credit_summary = {
            "balance": balance if _number(balance) and balance >= 0 else None,
            "unlimited": credits.get("unlimited") is True,
        }
    return {
        "plan_type": plan_type[:40] if isinstance(plan_type, str) else None,
        "windows": windows,
        "credits": credit_summary,
    }


def read_account_quota() -> dict:
    executable = find_codex_executable()
    try:
        child = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError:
        raise QuotaError("无法启动 Codex 额度查询进程，请检查 CODEX_BIN。") from None
    messages = queue.Queue()

    def read_lines():
        try:
            while line := child.stdout.readline(1024 * 1024):
                if len(line) >= 1024 * 1024:
                    break
                messages.put(line)
        except (OSError, ValueError):
            pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_lines, name="codex-quota-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    next_id = 0

    def send(message):
        try:
            child.stdin.write(json.dumps(message) + chr(10))
            child.stdin.flush()
        except (OSError, ValueError):
            raise QuotaError("Codex 查询连接中断，请检查 CLI 后重试。") from None

    def request(method, params=None):
        nonlocal next_id
        next_id += 1
        request_id = next_id
        send({"id": request_id, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QuotaError("额度检测超时，请检查网络与 Codex 登录状态后重试。")
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty:
                raise QuotaError(
                    "额度检测超时，请检查网络与 Codex 登录状态后重试。"
                ) from None
            if line is None:
                raise QuotaError(
                    "Codex 额度查询进程已退出，请更新 CLI 并检查登录状态。"
                )
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") and message.get("id") is not None:
                send(
                    {
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Read-only client"},
                    }
                )
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error") or not isinstance(message.get("result"), dict):
                raise QuotaError(
                    "Codex 额度查询失败，请确认 CLI 已使用订阅账号登录并检查网络。"
                )
            return message["result"]

    try:
        request(
            "initialize",
            {"clientInfo": {"name": "excel_proxy_quota", "version": "1.0.0"}},
        )
        send({"method": "initialized"})
        account = request("account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise QuotaError(
                "当前 Codex CLI 未使用订阅账号登录，无法检测订阅额度。"
            )
        return normalize_quota(
            request("account/rateLimits/read"), account.get("planType")
        )
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        reader.join(timeout=1)
        for pipe in (child.stdin, child.stdout):
            with suppress(OSError):
                pipe.close()


class QuotaService:
    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._result = {
            "status": "not_checked",
            "source": "codex",
            "checked_at": None,
            "windows": [],
        }
        self._next_check = 0.0

    def snapshot(self) -> dict:
        return {
            **copy.deepcopy(self._result),
            "checking": self._refresh_lock.locked(),
            "retry_after": max(0, math.ceil(self._next_check - time.monotonic())),
        }

    def refresh(self) -> dict:
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            if time.monotonic() >= self._next_check:
                try:
                    result = {"status": "ready", **read_account_quota()}
                except QuotaError as exc:
                    result = {"status": "error", "message": str(exc), "windows": []}
                self._result = {**result, "source": "codex", "checked_at": time.time()}
                self._next_check = time.monotonic() + CACHE_SECONDS
        finally:
            self._refresh_lock.release()
        return self.snapshot()


quota_service = QuotaService()
