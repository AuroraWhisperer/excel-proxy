"""Private OAuth login with optional, transient browser form automation.

OAuth constants follow openai/codex e72da2b53805894878023d01949a25a082e0a5cb.
This never reads or overwrites Codex/Excel login files. Balance-only login
discards refresh tokens; direct proxy login explicitly requests offline access.
"""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from account_balances import balance_store
from account_identity import (
    BalanceError,
    MAX_BYTES,
    account_email as _account_email,
    token_claims as _claims,
)
from account_login_browser import AutomatedLoginBrowser, parse_credentials

AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# Registered CLI callback ports; never stop another app to acquire either port.
CALLBACK_PORTS = (1455, 1457)
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
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            with client.stream("POST", TOKEN_URL, data=data) as response:
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
    payload = _token_response(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"],
        }
    )
    account = _proxy_credentials(payload)
    if not pending.get("offline_access"):
        account.pop("refresh_token", None)
        account.pop("expires_at", None)
    return account


def refresh_credentials(refresh_token):
    return _proxy_credentials(
        _token_response(
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
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
            service = self

            class CallbackHandler(BaseHTTPRequestHandler):
                def setup(self):
                    super().setup()
                    self.connection.settimeout(5)

                def log_message(self, *_args):
                    pass  # Authorization codes must never enter access logs.

                def do_GET(self):
                    status, message = service.complete(
                        self.path, self.headers.get("Host", "")
                    )
                    nonce = secrets.token_urlsafe(16)
                    body = (
                        f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>账号登录</title>'
                        f"<h1>账号登录</h1><p>{message}</p><p>请返回 Excel Proxy。</p>"
                        f'<script nonce="{nonce}">history.replaceState(null,"","/auth/complete");</script></html>'
                    ).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header(
                        "Content-Security-Policy",
                        f"default-src 'none'; script-src 'nonce-{nonce}'; frame-ancestors 'none'",
                    )
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass

            server = None
            for port in CALLBACK_PORTS:
                try:
                    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
                    break
                except OSError:
                    continue
            if server is None:
                raise BalanceError(
                    "登录回调端口 1455 和 1457 正被占用。请完成其他登录后重试，或使用 JSON 导入。",
                    409,
                )
            server.timeout = 0.25
            verifier = secrets.token_urlsafe(32)
            pending = {
                "state": secrets.token_urlsafe(32),
                "verifier": verifier,
                "redirect_uri": f"http://127.0.0.1:{server.server_port}/auth/callback",
                "expires_at": time.time() + LOGIN_SECONDS,
                "stop": threading.Event(),
                "offline_access": self.offline_access,
                "automated": credentials is not None,
            }
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            url = (
                AUTHORIZE_URL
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": CLIENT_ID,
                        "redirect_uri": pending["redirect_uri"],
                        "scope": "openid profile email"
                        + (" offline_access" if self.offline_access else ""),
                        "state": pending["state"],
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "id_token_add_organizations": "true",
                        "codex_cli_simplified_flow": "true",
                        "prompt": "login",
                    }
                )
            )
            self._pending = pending
            try:
                pending["browser"] = (
                    AutomatedLoginBrowser(url, PrivateLoginBrowser, credentials)
                    if credentials is not None
                    else PrivateLoginBrowser(url)
                )
            except BalanceError:
                server.server_close()
                self._pending = None
                raise
            self._status = {
                "status": "waiting",
                "message": "请在新打开的官方页面完成登录，账号会自动添加。",
                "expires_at": pending["expires_at"],
            }
            self._thread = threading.Thread(
                target=self._serve,
                args=(server, pending),
                daemon=True,
                name="account-login",
            )
            self._thread.start()
        return self.snapshot()

    def _serve(self, server, pending):
        try:
            while not pending["stop"].is_set():
                if not pending["browser"].is_alive():
                    with self._lock:
                        if (
                            self._pending is pending
                            and self._status["status"] == "waiting"
                        ):
                            error = (
                                pending["browser"].error
                                if pending.get("automated")
                                else None
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
                server.handle_request()
        finally:
            server.server_close()
            pending["browser"].close()
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def complete(self, path, host):
        with self._lock:
            pending = self._pending
            if not pending or self._status["status"] != "waiting":
                return 409, "这次登录已结束，请返回应用重新开始。"
            if time.time() >= pending["expires_at"]:
                return 400, "登录已超时，请返回应用重新开始。"
            try:
                parsed = urlsplit(path)
                if (
                    host != urlsplit(pending["redirect_uri"]).netloc
                    or parsed.path != "/auth/callback"
                    or len(path) > 16384
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
            with self._lock:
                if self._pending is not pending or pending["stop"].is_set():
                    return 409, "登录已取消，登录信息未保存。"
                if time.time() >= pending["expires_at"]:
                    self._status = {
                        "status": "expired",
                        "message": "登录已超时，请重新开始。",
                    }
                    return 400, self._status["message"]
                result = self.store.import_accounts(account)
                self._status = {
                    "status": "success",
                    "message": "账号已添加，登录窗口将自动关闭。",
                    "account_id": result["imported_ids"][0],
                }
                return 200, self._status["message"]
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
