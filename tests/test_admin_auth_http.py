"""HTTP cookie roundtrip against the real admin routes and auth dependency."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin


class AdminAuthHttpTests(unittest.TestCase):
    def setUp(self):
        db = Mock()
        db.get_admin_config = AsyncMock(return_value=Mock(username="test-admin"))
        db.get_dashboard_stats = AsyncMock(return_value={"total_tokens": 2})
        patcher = patch.multiple(admin, db=db, active_admin_tokens=set())
        patcher.start()
        self.addCleanup(patcher.stop)
        verifier = patch.object(admin.AuthManager, "verify_admin", return_value=True)
        self.verify_credentials = verifier.start()
        self.addCleanup(verifier.stop)
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_cookie_only_login_stats_logout_roundtrip(self):
        missing = self.client.get("/api/stats")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.headers["X-Admin-Session"], "expired")
        login = self.client.post(
            "/api/login", json={"username": "test-admin", "password": "test-password"}
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["success"])
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertIn("SameSite=lax", login.headers["set-cookie"])
        self.assertIn("Max-Age=2592000", login.headers["set-cookie"])
        self.assertTrue(self.client.cookies.get(admin.ADMIN_SESSION_COOKIE_NAME))
        self.verify_credentials.assert_called_once_with("test-admin", "test-password")
        for _ in range(3):
            stats = self.client.get("/api/stats")
            self.assertNotIn("Authorization", stats.request.headers)
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(stats.json()["total_tokens"], 2)
        # Cached old bearer must not override the current valid cookie.
        self.assertEqual(
            self.client.get("/api/stats", headers={"Authorization": "Bearer stale"}).status_code,
            200,
        )
        session = self.client.cookies.get(admin.ADMIN_SESSION_COOKIE_NAME)
        logout = self.client.post("/api/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])
        self.assertIsNone(self.client.cookies.get(admin.ADMIN_SESSION_COOKIE_NAME))
        self.assertEqual(self.client.get("/api/stats").status_code, 401)
        revoked = self.client.get("/api/stats", headers={"Authorization": f"Bearer {session}"})
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.headers["X-Admin-Session"], "expired")

    def test_invalid_login_creates_no_session_cookie(self):
        self.verify_credentials.return_value = False
        response = self.client.post(
            "/api/login", json={"username": "test-admin", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("X-Admin-Session", response.headers)
        self.assertFalse(admin.active_admin_tokens)


if __name__ == "__main__":
    unittest.main()
