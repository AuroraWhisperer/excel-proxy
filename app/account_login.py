"""Private Excel/BPS OAuth login with optional, transient browser form automation.

This never reads or overwrites Codex/Excel login files. Balance-only login
discards refresh tokens; direct proxy login explicitly requests offline access.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx

import bps_device_auth
from account_balances import balance_store
from account_identity import (
    BalanceError,
    MAX_BYTES,
    account_email as _account_email,
    token_claims as _claims,
)
from account_login_browser import AutomatedLoginBrowser, parse_credentials

AUTHORIZE_URL = bps_device_auth.AUTHORIZE_URL
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
LOGIN_SECONDS = 600


def find_login_browser():
    candidates = [
        (Path(root) / relative, flag)
        for root in (
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramFiles"),
            os.environ.get("LOCALAPPDATA"),
        )
        if root
        for relative, flag in (
            ("Microsoft/Edge/Application/msedge.exe", "--inprivate"),
            ("Google/Chrome/Application/chrome.exe", "--incognito"),
        )
    ]
    browser = next(((path, flag) for path, flag in candidates if path.is_file()), None)
    if browser is None:
        raise BalanceError(
            "请安装 Microsoft Edge 或 Chrome 后登录，也可改用 JSON 导入账号。", 503
        )
    return browser


class PrivateLoginBrowser:
    """Own only the process tree and temporary profile created for this login."""

    def __init__(self, url, *, debug_port=None):
        self._close_lock = threading.Lock()
        self._closed = False
        browser = find_login_browser()
        self.profile = tempfile.TemporaryDirectory(
            prefix="ghcp-account-login-", ignore_cleanup_errors=True
        )
        try:
            if url is None:
                # Edge replaces an about:blank app URL with its new-tab page.
                start_page = Path(self.profile.name) / "login-start.html"
                start_page.write_text(
                    '<!doctype html><meta charset="utf-8"><title>账号登录</title><p>正在打开登录页面…</p>',
                    encoding="utf-8",
                )
                url = start_page.as_uri()
            self.initial_url = url
            debugging = (
                [
                    f"--remote-debugging-port={debug_port}",
                    "--remote-debugging-address=127.0.0.1",
                ]
                if debug_port is not None
                else []
            )
            self.process = subprocess.Popen(
                [
                    str(browser[0]),
                    browser[1],
                    f"--user-data-dir={self.profile.name}",
                    f"--app={url}",
                    "--window-size=560,780",
                    "--no-first-run",
                    "--no-default-browser-check",
                    *debugging,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self.profile.cleanup()
            raise BalanceError(
                "无法打开登录窗口，请检查 Edge 或 Chrome 是否已安装。", 503
            ) from None

    def is_alive(self):
        return self.process.poll() is None

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._close_owned_process()

    def _close_owned_process(self):
        if self.process.poll() is None:
            try:
                # Never terminate by executable name: existing user windows are unrelated.
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    self.process.terminate()
        self.profile.cleanup()


def _token_response(data):
    url = TOKEN_URL + "?unified=true" if data["client_id"] == bps_device_auth.CLIENT_ID else TOKEN_URL
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            with client.stream("POST", url, data=data) as response:
                if response.status_code != 200:
                    raise BalanceError(
                        "登录授权未能完成，请重新登录；不会保存密码或验证码。",
                        401 if response.status_code in (400, 401) else 503,
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise BalanceError("登录响应过大，请重新登录。")
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(
            payload.get("access_token"), str
        ):
            raise BalanceError("官方登录未返回账号访问凭据，请重新登录。")
        return payload
    except (httpx.HTTPError, ValueError, RecursionError):
        raise BalanceError("无法完成登录授权，请检查网络后重新登录。", 503) from None


def _proxy_credentials(payload):
    account = {"access_token": payload["access_token"]}
    if payload.get("refresh_token"):
        account["refresh_token"] = payload["refresh_token"]
    expires = payload.get("expires_in")
    if (
        isinstance(expires, (int, float))
        and not isinstance(expires, bool)
        and 0 < expires <= 366 * 86400
    ):
        account["expires_at"] = time.time() + expires
    email = _account_email(payload, payload, _claims(payload["access_token"]))
    if email:
        account["email"] = email
    return account


def exchange_code(code, pending):
    client_id = pending.get("client_id", CLIENT_ID)
    payload = _token_response(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"],
        }
    )
    account = _proxy_credentials(payload)
    if client_id == bps_device_auth.CLIENT_ID and pending.get("offline_access"):
        account["client_id"] = client_id
    if not pending.get("offline_access"):
        account.pop("refresh_token", None)
        account.pop("expires_at", None)
    return account


def refresh_credentials(refresh_token, *, client_id=CLIENT_ID):
    return _proxy_credentials(
        _token_response(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
        )
    )


class AccountLoginService:
    def __init__(self, store, *, offline_access=False):
        self.store = store
        self.offline_access = offline_access
        self._lock = threading.RLock()
        self._pending = None
        self._thread = None
        self._status = {"status": "idle", "message": ""}

    def snapshot(self):
        with self._lock:
            result = dict(self._status)
            if (
                self._pending
                and self._pending.get("automated")
                and result["status"] == "waiting"
            ):
                result["message"] = self._pending["browser"].message
            return result

    def start(self, credentials=None):
        credentials = (
            parse_credentials(credentials) if credentials is not None else None
        )
        with self._lock:
            if self._pending is not None:
                raise BalanceError("已有登录正在进行，请完成或取消后再添加账号。", 409)
            if credentials is None:
                pending = {
                    "stop": threading.Event(),
                    "browser": None,
                    "expires_at": time.time() + LOGIN_SECONDS,
                }
                self._pending = pending
                self._status = {
                    "status": "waiting",
                    "message": "正在获取 Excel 设备码…",
                }
                self._thread = threading.Thread(
                    target=self._device_login,
                    args=(pending,),
                    daemon=True,
                    name="bps-login",
                )
                self._thread.start()
                return self.snapshot()
            verifier = secrets.token_urlsafe(32)
            state = secrets.token_urlsafe(32)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            redirect_uri = bps_device_auth.BROWSER_REDIRECT_URI
            url = bps_device_auth.browser_authorize_url(
                state, challenge, offline_access=self.offline_access
            )
            pending = {
                "state": state,
                "verifier": verifier,
                "client_id": bps_device_auth.CLIENT_ID,
                "redirect_uri": redirect_uri,
                "expires_at": time.time() + LOGIN_SECONDS,
                "stop": threading.Event(),
                "offline_access": self.offline_access,
                "automated": True,
            }
            self._pending = pending
            try:
                pending["browser"] = AutomatedLoginBrowser(
                    url,
                    PrivateLoginBrowser,
                    credentials,
                    callback_uri=redirect_uri,
                )
            except BalanceError:
                self._pending = None
                raise
            self._status = {
                "status": "waiting",
                "message": "请在新打开的官方页面完成登录，账号会自动添加。",
                "expires_at": pending["expires_at"],
            }
            self._thread = threading.Thread(
                target=self._serve_browser,
                args=(pending,),
                daemon=True,
                name="account-login",
            )
            self._thread.start()
        return self.snapshot()

    def _device_login(self, pending):
        browser = None
        stop = pending["stop"]
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                device, interval = bps_device_auth.request_code(client)
                with self._lock:
                    if stop.is_set():
                        return
                    browser = pending["browser"] = PrivateLoginBrowser(
                        bps_device_auth.APPROVAL_URL
                    )
                    self._status = {
                        "status": "waiting",
                        "expires_at": pending["expires_at"],
                        "message": f"请在已打开的 OpenAI 授权页输入设备码 {device['user_code']}，并确认本次 Excel 登录。",
                    }
                while True:
                    remaining = pending["expires_at"] - time.time()
                    if stop.wait(max(0, min(interval, remaining))):
                        return
                    if time.time() >= pending["expires_at"] or not browser.is_alive():
                        raise BalanceError("授权已超时或登录窗口已关闭，请重新登录。")
                    form = bps_device_auth.poll_grant(client, device)
                    if form is not None:
                        break
            with self._lock:
                if stop.is_set():
                    return
                self._status = {
                    "status": "exchanging",
                    "message": "正在保存 Excel 授权…",
                }
            account = _proxy_credentials(_token_response(form))
            if self.offline_access:
                account["client_id"] = bps_device_auth.CLIENT_ID
            else:
                account.pop("refresh_token", None)
                account.pop("expires_at", None)
            self._save_login(pending, account)
        except (BalanceError, httpx.HTTPError, ValueError) as exc:
            with self._lock:
                if not stop.is_set():
                    self._status = {
                        "status": "error",
                        "message": str(exc)
                        if isinstance(exc, BalanceError)
                        else "无法完成 Excel 设备授权，请检查网络后重新登录。",
                    }
        finally:
            if browser is not None:
                browser.close()
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def _serve_browser(self, pending):
        try:
            browser = pending["browser"]
            while not pending["stop"].is_set():
                callback_url = getattr(browser, "callback_url", None)
                if callback_url is not None:
                    self.complete_callback(callback_url)
                    break
                if not browser.is_alive():
                    callback_url = getattr(browser, "callback_url", None)
                    if callback_url is not None:
                        self.complete_callback(callback_url)
                        break
                    with self._lock:
                        if (
                            self._pending is pending
                            and self._status["status"] == "waiting"
                        ):
                            error = (
                                browser.error if pending.get("automated") else None
                            )
                            self._status = {
                                "status": "error" if error else "cancelled",
                                "message": error
                                or "登录窗口已关闭，本次登录信息未保存。",
                            }
                    break
                if time.time() >= pending["expires_at"]:
                    with self._lock:
                        if self._pending is pending:
                            self._status = {
                                "status": "expired",
                                "message": "登录已超时，请重新开始。",
                            }
                    break
                pending["stop"].wait(0.1)
        finally:
            pending["browser"].close()
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def complete_callback(self, callback_url):
        with self._lock:
            pending = self._pending
            if not pending or self._status["status"] != "waiting":
                return 409, "这次登录已结束，请返回应用重新开始。"
            if not bps_device_auth.matches_browser_callback(callback_url):
                self._status = {
                    "status": "error",
                    "message": "登录返回地址不正确，请返回应用重新登录。",
                }
                pending["stop"].set()
                return 400, self._status["message"]
        parsed = urlsplit(callback_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        status, message = self.complete(path, parsed.netloc)
        if status >= 400:
            with self._lock:
                if (
                    self._pending is pending
                    and self._status["status"] == "waiting"
                    and not pending["stop"].is_set()
                ):
                    self._status = {"status": "error", "message": message}
                    pending["stop"].set()
        return status, message

    def complete(self, path, host):
        with self._lock:
            pending = self._pending
            if not pending or self._status["status"] != "waiting":
                return 409, "这次登录已结束，请返回应用重新开始。"
            if time.time() >= pending["expires_at"]:
                return 400, "登录已超时，请返回应用重新开始。"
            try:
                expected = urlsplit(pending["redirect_uri"])
                parsed = urlsplit(path)
                if (
                    not isinstance(path, str)
                    or "#" in path
                    or host != expected.netloc
                    or parsed.path != expected.path
                    or len(path) > bps_device_auth.MAX_CALLBACK_URL_LEN
                ):
                    return 400, "登录返回地址不正确，请返回应用重新登录。"
                query = parse_qs(parsed.query, max_num_fields=20)
            except ValueError:
                return 400, "登录返回信息不完整，请返回应用重新登录。"
            states, codes = query.get("state", []), query.get("code", [])
            if len(states) != 1 or not secrets.compare_digest(
                states[0].encode(), pending["state"].encode()
            ):
                return 400, "登录校验未通过，请使用刚刚打开的官方登录页面。"
            if "error" in query or len(codes) != 1 or not codes[0]:
                self._status = {
                    "status": "error",
                    "message": "登录未获授权，请重新登录。",
                }
                pending["stop"].set()
                return 400, self._status["message"]
            self._status = {"status": "exchanging", "message": "正在保存账号…"}
        try:
            account = exchange_code(codes[0], pending)
            return self._save_login(pending, account)
        except BalanceError:
            with self._lock:
                if self._pending is pending and not pending["stop"].is_set():
                    self._status = {
                        "status": "error",
                        "message": "无法保存登录信息，请检查网络和本机文件写入权限。",
                    }
            return 400, "登录未能完成，请返回应用重试。"
        finally:
            pending["stop"].set()

    def _save_login(self, pending, account):
        with self._lock:
            if self._pending is not pending or pending["stop"].is_set():
                return 409, "登录已取消，登录信息未保存。"
            if time.time() >= pending["expires_at"]:
                self._status = {"status": "expired", "message": "登录已超时，请重新开始。"}
                return 400, self._status["message"]
            result = self.store.import_accounts(account)
            self._status = {
                "status": "success", "message": "账号已添加，登录窗口将自动关闭。",
                "account_id": result["imported_ids"][0],
            }
            return 200, self._status["message"]

    def cancel(self):
        browser = None
        thread = None
        with self._lock:
            if self._pending is not None and self._status["status"] in (
                "waiting",
                "exchanging",
            ):
                self._pending["stop"].set()
                self._status = {
                    "status": "cancelled",
                    "message": "登录已取消，本次登录信息未保存。",
                }
                browser = self._pending["browser"]
                thread = self._thread
        if browser is not None:
            browser.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        return self.snapshot()

    def close(self):
        self.cancel()
        if self._thread is not None:
            self._thread.join(timeout=1)


login_service = AccountLoginService(balance_store)
