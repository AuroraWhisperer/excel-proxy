"""Persistent task namespaces for server-owned BPS generation slots."""

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

from account_identity import token_claims
from app_paths import user_state_dir


def account_key(headers):
    """Use grant identity for local scheduling; BPS still authenticates the grant."""
    scheme, _, token = headers.get("authorization", "").partition(" ")
    claims = token_claims(token) if scheme.lower() == "bearer" else {}
    auth = claims.get("https://api.openai.com/auth")
    account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    if not isinstance(account, str) or not account:
        account = headers.get("chatgpt-account-id") or headers.get("x-openai-account-id", "")
    subject = claims.get("sub")
    if account and isinstance(subject, str) and subject:
        return "bps-user-v1:" + hashlib.sha256(f"{subject}\0{account}".encode()).hexdigest()
    return account


class TaskIdentityStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self._key = None
        self._lock = threading.Lock()

    def _load_key(self):
        with self._lock:
            if self._key is not None:
                return self._key
            path = self.path or Path(user_state_dir()) / "task.key"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                # Publish a complete key without replacing another process's key.
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".task-key-", delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                    try:
                        temporary.write(secrets.token_hex(32).encode("ascii"))
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    except BaseException:
                        temporary.close()
                        temporary_path.unlink(missing_ok=True)
                        raise
                try:
                    os.link(temporary_path, path)
                except FileExistsError:
                    pass
                finally:
                    temporary_path.unlink(missing_ok=True)
            key = path.read_bytes()
            if re.fullmatch(rb"[0-9a-f]{64}", key) is None:
                raise RuntimeError("Excel task key is invalid; restore the existing task.key.")
            self._key = key
            return key

    def task_id(self, account, slot):
        message = f"bps-account-slot-v1\0{account}\0{slot}".encode()
        return hmac.new(self._load_key(), message, hashlib.sha256).hexdigest()


task_identity_store = TaskIdentityStore()
