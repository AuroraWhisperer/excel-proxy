"""Disposable browser automation for a single, explicitly supplied login."""

import base64
import importlib.util
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from account_identity import BalanceError


def parse_credentials(text):
    message = "请按格式填写：邮箱----密码----2FA密钥，用至少 3 个连字符分隔，每次一行。"
    if not isinstance(text, str) or len(text) > 8192:
        raise BalanceError(message)
    text = text.strip()
    separators = list(re.finditer(r"-{3,}", text))
    if len(separators) < 2 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise BalanceError(message)
    email = text[: separators[0].start()].strip()
    password = text[separators[0].end() : separators[-1].start()]
    secret = re.sub(r"\s+", "", text[separators[-1].end() :]).upper().rstrip("=")
    if (
        not re.fullmatch(r"[^\s@]{1,128}@[^\s@]{1,125}", email)
        or not 1 <= len(password) <= 4096
    ):
        raise BalanceError(message)
    try:
        if not re.fullmatch(r"[A-Z2-7]{16,256}", secret):
            raise ValueError()
        base64.b32decode(secret + "=" * (-len(secret) % 8))
    except ValueError:
        raise BalanceError(
            "第三项需要有效的 2FA 密钥，不能填写六位临时验证码。"
        ) from None
    return {"email": email, "password": password, "secret": secret}


def _at_origin(url, host):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == host
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


class AutomatedLoginBrowser:
    """All Playwright handles stay on one worker thread; no persistent profile."""

    def __init__(self, url, browser_factory, credentials):
        if importlib.util.find_spec("playwright") is None:
            raise BalanceError(
                "缺少自动登录组件 Playwright，请先安装 requirements.txt 中的依赖。", 503
            )
        self.message = "正在打开登录窗口…"
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(url, browser_factory, credentials),
            daemon=True,
            name="account-login-browser",
        )
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def close(self):
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=30)

    def _run(self, url, browser_factory, credentials):
        browser = None
        native = None
        ready = False
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                try:
                    if self._stop.is_set():
                        return
                    # Reuse the manual login's native InPrivate/Incognito window.
                    # Playwright's browser launch defaults can trigger login challenges.
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        port = listener.getsockname()[1]
                    native = browser_factory(None, debug_port=port)
                    deadline = time.monotonic() + 15
                    page = None
                    while (
                        not self._stop.is_set()
                        and native.is_alive()
                        and time.monotonic() < deadline
                    ):
                        if browser is None:
                            try:
                                browser = playwright.chromium.connect_over_cdp(
                                    f"http://127.0.0.1:{port}", timeout=1000
                                )
                            except Exception:
                                self._stop.wait(0.1)
                                continue
                        # A port collision must never attach login to another window.
                        page = next(
                            (
                                page
                                for context in browser.contexts
                                for page in context.pages
                                if page.url == native.initial_url
                            ),
                            None,
                        )
                        if page is not None:
                            break
                        self._stop.wait(0.1)
                    if self._stop.is_set():
                        return
                    if page is None:
                        raise BalanceError("无法连接本次独立登录窗口。", 503)
                    ready = True
                    page.set_default_timeout(2500)
                    submitted = set()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        self.message = "正在等待登录页面加载，也可在窗口中手动继续。"
                    last_progress = time.monotonic()
                    while (
                        not self._stop.is_set()
                        and browser.is_connected()
                        and not page.is_closed()
                    ):
                        if credentials:
                            try:
                                if self._advance(page, credentials, submitted):
                                    last_progress = time.monotonic()
                                elif time.monotonic() - last_progress > 20:
                                    credentials.clear()
                                    self.message = (
                                        "请在登录窗口完成验证，账号会自动保存。"
                                    )
                            except Exception:
                                # Browser errors may contain filled values; never publish/log them.
                                credentials.clear()
                                self.message = "自动填写已暂停，请在登录窗口继续。"
                        page.wait_for_timeout(200)
                finally:
                    if browser is not None and ready:
                        browser.close()
        except Exception:
            if not ready and not self._stop.is_set():
                self.error = "自动登录窗口未能打开，请检查 Edge/Chrome 与 Playwright 安装后重试。"
        finally:
            credentials.clear()
            if native is not None:
                native.close()

    def _submit(self, page, field):
        if self._stop.is_set() or not _at_origin(page.url, "auth.openai.com"):
            return
        form = field.locator("xpath=ancestor::form")
        button = form.get_by_role(
            "button",
            name=re.compile(
                r"^(Continue|Next|Log in|Sign in|Verify|继续|下一步|登录|验证)$", re.I
            ),
        ).first
        if button.is_visible():
            button.click()
        else:
            field.press("Enter")

    def _advance(self, page, credentials, submitted):
        if self._stop.is_set() or not _at_origin(page.url, "auth.openai.com"):
            return False
        # Challenges, rejected credentials and enrollment need the user's input.
        if page.locator(
            '[role="alert"]:visible, iframe[src*="challenges.cloudflare.com"]:visible, iframe[src*="recaptcha"]:visible, iframe[src*="hcaptcha"]:visible'
        ).count():
            raise BalanceError("需要手动验证。")
        path = urlsplit(page.url).path.lower()
        if any(
            part in path
            for part in ("enroll", "register", "reset-password", "recovery")
        ):
            raise BalanceError("需要手动验证。")
        email = page.locator(
            'input[type="email"]:visible, input[name="username"]:visible, input[autocomplete="username"]:visible'
        ).first
        password = page.locator('input[type="password"]:visible').first
        if "email" not in submitted and email.is_visible():
            self.message = "正在填写账号…"
            email.fill(credentials.pop("email"))
            submitted.add("email")
            if password.is_visible():
                password.fill(credentials.pop("password"))
                submitted.add("password")
                self._submit(page, password)
            else:
                self._submit(page, email)
            return True
        if "password" not in submitted and password.is_visible():
            self.message = "正在填写密码…"
            password.fill(credentials.pop("password"))
            submitted.add("password")
            self._submit(page, password)
            return True
        code = page.locator(
            'input[autocomplete="one-time-code"]:visible, input[name="code"]:visible, input[name="otp"]:visible'
        ).first
        if "code" not in submitted and code.is_visible():
            text = page.locator("body").inner_text()
            if "mfa" not in path and not re.search(
                r"authenticator|authentication app|身份验证器|身份驗證器", text, re.I
            ):
                raise BalanceError("需要手动完成额外验证。")
            self.message = "正在从 2fa.fun 获取验证码…"
            value = self._verification_code(page.context, credentials.pop("secret"))
            if self._stop.is_set() or not _at_origin(page.url, "auth.openai.com"):
                return False
            page.bring_to_front()
            self.message = "正在填写 2FA 验证码…"
            code.fill(value)
            submitted.add("code")
            self._submit(page, code)
            credentials.clear()
            self.message = "验证码已提交。如需选择工作区或确认授权，请在登录窗口继续。"
            return True
        return False

    def _verification_code(self, context, secret):
        if self._stop.is_set():
            raise BalanceError("登录已取消。")
        page = context.new_page()
        try:
            page.set_default_timeout(2500)
            page.goto("https://2fa.fun/", wait_until="domcontentloaded", timeout=15000)
            if self._stop.is_set() or not _at_origin(page.url, "2fa.fun"):
                raise BalanceError("验证码页面不可用。")
            page.locator("#SECRET2FA").fill(secret)
            if self._stop.is_set() or not _at_origin(page.url, "2fa.fun"):
                raise BalanceError("验证码页面不可用。")
            page.locator('#form_secret button[type="submit"]').click()
            deadline = time.monotonic() + 15
            while not self._stop.is_set() and time.monotonic() < deadline:
                if not _at_origin(page.url, "2fa.fun"):
                    break
                field = page.locator("#toggle-2fa-list input.faotp")
                if field.count() == 1 and field.is_visible():
                    value = field.input_value().strip()
                    # Avoid a code that is about to roll over while switching tabs.
                    if re.fullmatch(r"\d{6}", value) and time.time() % 30 < 25:
                        return value
                page.wait_for_timeout(200)
            raise BalanceError("未获得有效验证码。")
        finally:
            page.close()
