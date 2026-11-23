"""Persistent, account-scoped identity for the BPS generation slot pool."""

import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

import excel_task_identity as identity


def grant(account, subject, expiration):
    claims = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
        "sub": subject,
        "exp": expiration,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "Bearer fixture." + encoded + ".signature"


class ExcelTaskIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "task.key"

    def test_task_namespace_survives_reload_and_isolates_slots_and_accounts(self):
        store = identity.TaskIdentityStore(self.path)
        first = store.task_id("account-a", 1)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(identity.TaskIdentityStore(self.path).task_id("account-a", 1), first)
        self.assertNotEqual(store.task_id("account-a", 2), first)
        self.assertNotEqual(store.task_id("account-b", 1), first)

    def test_task_id_matches_the_reference_hmac(self):
        key = "a" * 64
        self.path.write_text(key, encoding="ascii")
        expected = hmac.new(key.encode(), b"bps-account-slot-v1\0account-a\0" + b"1", hashlib.sha256).hexdigest()
        self.assertEqual(identity.TaskIdentityStore(self.path).task_id("account-a", 1), expected)

    def test_invalid_existing_key_is_not_replaced(self):
        self.path.write_text("invalid", encoding="ascii")
        with self.assertRaisesRegex(RuntimeError, "task key"):
            identity.TaskIdentityStore(self.path).task_id("account-a", 1)
        self.assertEqual(self.path.read_text(encoding="ascii"), "invalid")

    def test_grant_renewal_keeps_identity_and_workspace_members_are_isolated(self):
        first = {"authorization": grant("workspace-a", "user-a", 100), "chatgpt-account-id": "stale"}
        renewed = {**first, "authorization": grant("workspace-a", "user-a", 200)}
        other_user = {**first, "authorization": grant("workspace-a", "user-b", 100)}
        expected = "bps-user-v1:" + hashlib.sha256(b"user-a\0workspace-a").hexdigest()
        self.assertEqual(identity.account_key(first), expected)
        self.assertEqual(identity.account_key(renewed), expected)
        self.assertNotEqual(identity.account_key(other_user), expected)
        self.assertEqual(identity.account_key({"chatgpt-account-id": "legacy"}), "legacy")
