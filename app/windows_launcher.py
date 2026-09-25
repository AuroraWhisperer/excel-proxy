"""Start and stop the Windows desktop proxy without a console window."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser

from constants import (
    PROXY_BASE_URL,
    PROXY_PID_FILE,
    PROXY_STDERR_LOG_FILE,
    PROXY_STDOUT_LOG_FILE,
)

REPO_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_URL = f"{PROXY_BASE_URL}/ui"
_OPENER = build_opener(ProxyHandler({}))
_STATE_ID = hashlib.sha256(os.path.normcase(os.path.abspath(PROXY_PID_FILE)).encode()).hexdigest()[:24]
_OBJECT_PREFIX = f"Local\\ExcelProxy-{_STATE_ID}"

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, arguments, result in (
        ("CreateMutexW", [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ("ReleaseMutex", [wintypes.HANDLE], wintypes.BOOL),
        ("CreateEventW", [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ("OpenEventW", [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetEvent", [wintypes.HANDLE], wintypes.BOOL),
        ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        function = getattr(_kernel32, name)
        function.argtypes = arguments
        function.restype = result


@contextmanager
def _handle(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield value
    finally:
        _kernel32.CloseHandle(value)


@contextmanager
def _launch_lock():
    with _handle(_kernel32.CreateMutexW(None, False, f"{_OBJECT_PREFIX}-launch")) as handle:
        if _kernel32.WaitForSingleObject(handle, 30000) not in (0, 0x80):
            raise RuntimeError("另一个启动或停止操作尚未完成，请稍后重试。")
        try:
            yield
        finally:
            _kernel32.ReleaseMutex(handle)


@contextmanager
def shutdown_listener(server):
    """Keep a local stop event alive until Uvicorn has finished shutting down."""
    with _handle(_kernel32.CreateEventW(None, True, False, f"{_OBJECT_PREFIX}-stop")) as handle:
        def wait_for_stop():
            if _kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
                server.should_exit = True

        thread = Thread(target=wait_for_stop, name="desktop-stop", daemon=True)
        thread.start()
        try:
            yield
        finally:
            _kernel32.SetEvent(handle)
            thread.join()


def _request_stop():
    with _handle(_kernel32.OpenEventW(0x0002, False, f"{_OBJECT_PREFIX}-stop")) as handle:
        if not _kernel32.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def proxy_running() -> bool:
    """Verify the service identity, including its runtime directory."""
    try:
        with _OPENER.open(f"{PROXY_BASE_URL}/api/config/background-proxy", timeout=5) as response:
            payload = json.load(response)
            if response.status == 200 and isinstance(payload, dict) and payload.get("pid_file") == PROXY_PID_FILE:
                return True
    except URLError as exc:
        if not isinstance(exc, HTTPError) and isinstance(exc.reason, ConnectionRefusedError):
            return False
        raise RuntimeError("无法确认 8000 端口上的服务是本项目的代理，请检查端口占用。") from exc
    except (ValueError, TimeoutError) as exc:
        raise RuntimeError("8000 端口上的服务没有返回有效的代理状态，请稍后重试。") from exc
    raise RuntimeError("8000 端口已被其他服务或使用不同数据目录的代理占用。")


def start_proxy() -> None:
    with _launch_lock():
        if proxy_running():
            return
        Path(PROXY_STDOUT_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(PROXY_STDOUT_LOG_FILE, "ab") as stdout, open(PROXY_STDERR_LOG_FILE, "ab") as stderr:
            process = subprocess.Popen(
                [str(REPO_DIR / ".venv" / "Scripts" / "python.exe"), "-B", str(REPO_DIR / "app" / "proxy.py")],
                cwd=REPO_DIR,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("代理启动后退出，请查看错误日志。")
                if proxy_running():
                    return
                time.sleep(0.2)
            raise RuntimeError("代理未能在 30 秒内启动，请查看错误日志。")
        except Exception:
            if process.poll() is None:
                try:
                    _request_stop()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=5)
            raise


def stop_proxy() -> None:
    with _launch_lock():
        if not proxy_running():
            return
        _request_stop()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not Path(PROXY_PID_FILE).exists() and not proxy_running():
                return
            time.sleep(0.2)
        raise RuntimeError("代理仍在关闭，请稍后重试并查看错误日志。")


def open_dashboard() -> None:
    with _OPENER.open(DASHBOARD_URL, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("代理已启动，但仪表盘暂时无法打开。")
    if not webbrowser.open(DASHBOARD_URL):
        raise RuntimeError(f"代理已启动，请在浏览器打开 {DASHBOARD_URL}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "start":
            start_proxy()
            if not args.no_browser:
                open_dashboard()
        else:
            stop_proxy()
        return 0
    except Exception as exc:
        try:
            Path(PROXY_STDERR_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(PROXY_STDERR_LOG_FILE, "a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(None, f"{exc}\n\n错误日志：{PROXY_STDERR_LOG_FILE}", "Excel Proxy", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
