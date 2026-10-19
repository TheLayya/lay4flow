"""The extracted log routes keep auth, late-bound dependencies and payload rules."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin


class AdminLogRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(
            get_logs=AsyncMock(
                return_value=[
                    {
                        "id": 1,
                        "status_code": "500",
                        "response_body_excerpt": '{"error":"Synthetic error"}',
                        "request_body": "not-for-list",
                        "response_body": "not-for-list",
                    }
                ]
            ),
            get_log_detail=AsyncMock(return_value=None),
            clear_all_logs=AsyncMock(),
        )
        self.enterContext(patch.multiple(admin, db=self.db, active_admin_tokens={"test-session"}))
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_auth_and_list_detail_clear_contract(self):
        self.assertEqual(self.client.get("/api/logs").status_code, 401)
        self.db.get_logs.assert_not_awaited()
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "test-session")
        response = self.client.get("/api/logs?limit=999")
        self.assertEqual(response.status_code, 200)
        self.db.get_logs.assert_awaited_once_with(limit=100, include_payload=False)
        row = response.json()[0]
        self.assertEqual(row["status_code"], 500)
        self.assertEqual(row["error_summary"], "Synthetic error")
        self.assertNotIn("request_body", row)
        self.assertNotIn("response_body", row)
        self.assertEqual(self.client.get("/api/logs/8").status_code, 404)
        self.db.get_log_detail.return_value = {
            "id": 8,
            "request_body": "request",
            "response_body": '{"error":{"message":"detail"}}',
        }
        self.assertEqual(self.client.get("/api/logs/8").json()["error_summary"], "detail")
        self.assertTrue(self.client.delete("/api/logs").json()["success"])
        self.db.clear_all_logs.assert_awaited_once()
        self.db.clear_all_logs.side_effect = RuntimeError("synthetic storage failure")
        self.assertEqual(self.client.delete("/api/logs").status_code, 500)

    def test_database_and_summary_are_resolved_at_request_time(self):
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "test-session")
        replacement = SimpleNamespace(get_logs=AsyncMock(return_value=[{"status_code": 500}]))
        with (
            patch.object(admin, "db", replacement),
            patch.object(admin, "_extract_error_summary", return_value="custom"),
        ):
            self.assertEqual(self.client.get("/api/logs").json()[0]["error_summary"], "custom")
        self.db.get_logs.assert_not_awaited()


class AdminLogDirectCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_function_imports_remain_callable(self):
        db = SimpleNamespace(get_logs=AsyncMock(return_value=[]))
        with patch.object(admin, "db", db):
            self.assertEqual(await admin.get_logs(limit=0, token="unused"), [])
        db.get_logs.assert_awaited_once_with(limit=1, include_payload=False)
