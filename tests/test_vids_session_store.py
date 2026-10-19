import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from src.services.vids.client import cookie_jar
from src.services.vids.session_store import MAX_SESSION_BYTES, SessionStore, SessionStoreError


class VidsSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.key = Fernet.generate_key()
        self.store = SessionStore(self.root, self.key)
        self.payload = {
            "editor_url": "https://docs.google.com/videos/d/synthetic-doc/edit",
            "auth_user": 0,
            "account_hash": hashlib.sha256(b"synthetic-account").hexdigest(),
            "cookies": [
                {
                    "name": "SAPISID",
                    "value": "synthetic-private-cookie",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "hostOnly": False,
                    "expires": 4000000000,
                    "session": False,
                },
                {
                    "name": "__Host-session",
                    "value": "synthetic-session",
                    "domain": "docs.google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "hostOnly": True,
                    "expires": None,
                    "session": True,
                },
                {
                    "name": "expired",
                    "value": "synthetic-expired",
                    "domain": "docs.google.com",
                    "path": "/videos",
                    "secure": False,
                    "httpOnly": False,
                    "hostOnly": True,
                    "expires": 1,
                    "session": False,
                },
            ],
        }

    def tearDown(self):
        self.directory.cleanup()

    def assertSafeError(self, callback):
        with self.assertRaises(SessionStoreError) as caught:
            callback()
        message = str(caught.exception)
        for private in (
            str(self.root),
            self.key.decode(),
            "synthetic-private-cookie",
            "synthetic-doc",
        ):
            self.assertNotIn(private, message)

    def test_restart_restores_scoped_and_session_cookies_without_plaintext(self):
        self.assertIsNone(self.store.load())
        self.assertEqual(list(self.root.iterdir()), [])
        self.store.save(**self.payload)
        encrypted = self.store.path.read_bytes()
        for private in (b"synthetic-private-cookie", b"synthetic-doc", b"account_hash"):
            self.assertNotIn(private, encrypted)
        result = SessionStore(self.root, self.key.decode()).load()
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["cookies"], self.payload["cookies"])
        self.assertEqual(result["editor_url"], self.payload["editor_url"] + "?authuser=0")
        jar = cookie_jar(result["cookies"])
        self.assertEqual({cookie.name for cookie in jar}, {"SAPISID", "__Host-session"})
        host = next(cookie for cookie in jar if cookie.name == "__Host-session")
        self.assertFalse(host.domain_specified)
        self.assertTrue(host.discard)
        if os.name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_wrong_key_tampering_and_oversized_files_fail_without_overwrite(self):
        self.store.save(**self.payload)
        encrypted = self.store.path.read_bytes()
        self.assertSafeError(SessionStore(self.root, Fernet.generate_key()).load)
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        tampered = encrypted[:-8] + b"tampered"
        self.store.path.write_bytes(tampered)
        self.assertSafeError(self.store.load)
        self.assertEqual(self.store.path.read_bytes(), tampered)
        self.store.path.write_bytes(b"x" * (MAX_SESSION_BYTES + 1))
        self.assertSafeError(self.store.load)

    def test_invalid_key_and_authenticated_invalid_payload_are_sanitized(self):
        self.assertSafeError(lambda: SessionStore(self.root, "synthetic-private-cookie"))
        payload = {"version": 1, **self.payload, "proxy": "synthetic-private-cookie"}
        self.store.path.write_bytes(Fernet(self.key).encrypt(json.dumps(payload).encode()))
        self.assertSafeError(self.store.load)
        payload.pop("proxy")
        for field, value in (
            ("version", True),
            ("auth_user", True),
            ("account_hash", "bad"),
            ("editor_url", "https://evil.example/videos/d/synthetic-doc/edit"),
        ):
            invalid = {**payload, field: value}
            self.store.path.write_bytes(Fernet(self.key).encrypt(json.dumps(invalid).encode()))
            self.assertSafeError(self.store.load)

    def test_invalid_cookie_or_save_failure_preserves_previous_snapshot(self):
        self.store.save(**self.payload)
        encrypted = self.store.path.read_bytes()
        for changes in (
            {"domain": ".evil.example"},
            {"secure": "true"},
            {"value": "bad;value"},
            {"expires": float("nan")},
            {"name": "__Host-bad", "hostOnly": False},
            {"path": "relative"},
            {"partitioned": True},
        ):
            payload = {**self.payload, "cookies": [{**self.payload["cookies"][0], **changes}]}
            self.assertSafeError(lambda: self.store.save(**payload))
            self.assertEqual(self.store.path.read_bytes(), encrypted)
        rotated = {**self.payload, "cookies": [{**self.payload["cookies"][0], "value": "rotated"}]}
        with patch(
            "src.services.vids.session_store.os.replace", side_effect=OSError("private-path")
        ):
            self.assertSafeError(lambda: self.store.save(**rotated))
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        self.assertEqual([path.name for path in self.root.iterdir()], [".session.enc"])
        self.store.save(**rotated)
        self.assertEqual(self.store.load()["cookies"][0]["value"], "rotated")


if __name__ == "__main__":
    unittest.main()
