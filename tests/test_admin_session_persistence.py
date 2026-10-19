"""Admin login survives process replacement and remains explicitly revocable."""

import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.admin_sessions import ADMIN_SESSION_MAX_AGE, AdminSessionStore


class AdminSessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "admin_sessions.sqlite3"
        self.sessions = AdminSessionStore(lambda: self.path)

    def test_reading_without_login_creates_no_runtime_files(self):
        self.assertNotIn("unknown", self.sessions)
        self.assertFalse(self.sessions)
        self.sessions.discard("unknown")
        self.sessions.clear()
        self.assertFalse(self.path.exists())

    def test_session_survives_new_process_with_only_hash_persisted(self):
        token = "admin-synthetic-long-random-session"
        self.sessions.add(token)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from src.core.admin_sessions import AdminSessionStore; "
                "assert sys.argv[2] in AdminSessionStore(lambda: Path(sys.argv[1]))",
                str(self.path),
                token,
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.path)) as database:
            rows = database.execute("SELECT token_hash FROM admin_sessions").fetchall()
        self.assertEqual(rows, [(hashlib.sha256(token.encode()).hexdigest(),)])
        self.assertNotIn(token.encode(), self.path.read_bytes())

    def test_expiry_and_new_login_purge_expired_sessions(self):
        with patch("src.core.admin_sessions.time.time", return_value=100):
            self.sessions.add("old")
        with patch("src.core.admin_sessions.time.time", return_value=100 + ADMIN_SESSION_MAX_AGE):
            self.assertNotIn("old", self.sessions)
            self.assertFalse(self.sessions)
            self.sessions.add("fresh")
            self.assertIn("fresh", self.sessions)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM admin_sessions").fetchone(), (1,)
            )

    def test_logout_and_password_change_revoke_persisted_http_and_viewer_sessions(self):
        db = SimpleNamespace(
            db_path=str(self.path.parent / "flow.db"),
            get_admin_config=AsyncMock(return_value=SimpleNamespace(username="fixture-admin")),
            get_dashboard_stats=AsyncMock(return_value={"total_tokens": 0}),
            update_admin_config=AsyncMock(),
            reload_config_to_memory=AsyncMock(),
        )
        app = FastAPI()
        app.include_router(admin.router)
        with (
            patch.multiple(admin, db=db, active_admin_tokens=self.sessions),
            patch.object(admin.AuthManager, "verify_admin", return_value=True),
            TestClient(app) as client,
        ):
            first = client.post(
                "/api/login", json={"username": "fixture-admin", "password": "fixture-password"}
            )
            token = first.json()["token"]
            self.assertIn("Max-Age=2592000", first.headers["set-cookie"])
            self.assertIn(admin.ADMIN_SESSION_COOKIE_NAME, first.headers["set-cookie"])
            self.assertTrue(admin.is_admin_session_token_valid(token))
            # Recreate the store just as a replacement server process would.
            replacement = AdminSessionStore(lambda: self.path)
            with patch.object(admin, "active_admin_tokens", replacement):
                self.assertEqual(client.get("/api/stats").status_code, 200)
                self.assertEqual(client.post("/api/logout").status_code, 200)
                self.assertFalse(admin.is_admin_session_token_valid(token))
                self.assertNotIn(token, self.sessions)
                self.assertEqual(client.get("/api/stats").status_code, 401)
                second = client.post(
                    "/api/login",
                    json={"username": "fixture-admin", "password": "fixture-password"},
                ).json()["token"]
                replacement.add("another-device-session")
                changed = client.post(
                    "/api/admin/change-password",
                    json={"old_password": "fixture-password", "new_password": "updated-password"},
                )
                self.assertEqual(changed.status_code, 200)
                self.assertFalse(admin.is_admin_session_token_valid(second))
                self.assertNotIn("another-device-session", self.sessions)
                self.assertEqual(client.get("/api/stats").status_code, 401)

    def test_legacy_cookie_from_another_local_service_is_ignored(self):
        self.sessions.add("our-session")
        app = FastAPI()
        app.include_router(admin.router)
        with patch.object(admin, "active_admin_tokens", self.sessions), TestClient(app) as client:
            client.cookies.set("admin_session", "our-session")
            self.assertEqual(client.get("/api/stats").status_code, 401)


if __name__ == "__main__":
    unittest.main()
