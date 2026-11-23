"""Platform-specific filesystem locations for ghcp_proxy runtime state."""

from __future__ import annotations

import errno
import os
import sys

APP_DIR_NAME = "ghcp_proxy"


def lock_service_state(directory: str):
    """Keep the returned file open for the lifetime of the standalone service."""
    os.makedirs(directory, exist_ok=True)
    handle = open(os.path.join(directory, "service.lock"), "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise RuntimeError("Another Excel proxy service owns this state directory.") from exc
        raise
    return handle


def _env_path(name: str) -> str | None:
    value = os.environ.get(name)
    if not value:
        return None
    return os.path.expanduser(value)


def user_config_dir() -> str:
    override = _env_path("GHCP_CONFIG_DIR")
    if override:
        return override

    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return os.path.join(root, APP_DIR_NAME)
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{APP_DIR_NAME}")

    root = _env_path("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(root, APP_DIR_NAME)


def user_state_dir() -> str:
    override = _env_path("GHCP_STATE_DIR")
    if override:
        return override

    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(root, APP_DIR_NAME)
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{APP_DIR_NAME}")

    root = _env_path("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(root, APP_DIR_NAME)


def user_cache_dir() -> str:
    override = _env_path("GHCP_CACHE_DIR")
    if override:
        return override

    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(root, APP_DIR_NAME, "Cache")
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Caches/{APP_DIR_NAME}")

    root = _env_path("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(root, APP_DIR_NAME)
