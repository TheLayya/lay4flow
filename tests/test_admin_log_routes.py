"""The extracted log routes keep auth, late-bound dependencies and payload rules."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.api.admin_routes.logs import _extract_attempt_history


class AdminLogRouteTests(unittest.TestCase):
    def test_version_two_attempt_history_has_one_budget_and_ignores_legacy_account_cap(self):
        result = _extract_attempt_history(
            {
                "attempt_history": {
                    "version": 2,
                    "limit": 20,
                    "total_attempts": 20,
                    "retry_count": 19,
                    "account_count": 20,
                    "account_allocations": 20,
                    "accounts_limit": 3,
                    "attempts": [
                        {"attempt": 20, "account_id": 20, "stage": "submit", "status": "failed"}
                    ],
                }
            }
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["limit"], 20)
        self.assertEqual(result["account_allocations"], 20)
        self.assertNotIn("accounts_limit", result)

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
        self.assertIsNone(row["attempt_count"])
        self.assertIsNone(row["retry_count"])
        self.assertIsNone(row["account_count"])
        self.assertEqual(self.client.get("/api/logs/8").status_code, 404)
        self.db.get_log_detail.return_value = {
            "id": 8,
            "request_body": "request",
            "response_body": '{"error":{"message":"detail"}}',
        }
        self.assertEqual(self.client.get("/api/logs/8").json()["error_summary"], "detail")
        self.assertIsNone(self.client.get("/api/logs/8").json()["attempt_history"])
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

    def test_retry_summary_stays_lightweight_and_detail_retains_success_and_failed_attempts(self):
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "test-session")
        history = {
            "version": 1,
            "total_attempts": 2,
            "retry_count": 1,
            "account_count": 2,
            "limit": 3,
            "accounts_limit": 3,
            "attempts": [
                {
                    "attempt": 1,
                    "account_id": 7,
                    "account_email": "first@example.invalid",
                    "stage": "submit",
                    "status": "failed",
                    "started_at": "2026-11-09T12:00:00+00:00",
                    "duration_ms": 1200,
                    "error": "PUBLIC_ERROR_USER_QUOTA_REACHED",
                    "error_code": "rpc_rejected",
                    "rule_action": "LIMIT",
                    "rule_keyword": "PUBLIC_ERROR_USER_QUOTA_REACHED",
                    "rule_seconds": 86400,
                    "delay_seconds": 0,
                    "next_action": "switch_account",
                },
                {
                    "attempt": 2,
                    "account_id": 8,
                    "account_email": "second@example.invalid",
                    "stage": "submit",
                    "status": "success",
                    "duration_ms": 800,
                    "next_action": "completed",
                },
            ],
        }
        body = json.dumps({"performance": {"attempt_history": history}, "large": "x" * 100_000})
        log = {
            "id": 9,
            "status_code": 200,
            "attempt_count": 2,
            "retry_count": 1,
            "account_count": 2,
            "response_body": body,
            "response_body_excerpt": body[:2048],
        }
        self.db.get_logs.return_value = [log]
        self.db.get_log_detail.return_value = log
        listing = self.client.get("/api/logs").json()[0]
        self.assertEqual(
            (listing["attempt_count"], listing["retry_count"], listing["account_count"]), (2, 1, 2)
        )
        self.assertNotIn("attempt_history", listing)
        self.assertNotIn("response_body", listing)
        self.assertLess(len(json.dumps(listing)), 1000)
        detail = self.client.get("/api/logs/9").json()
        self.assertEqual(detail["attempt_history"], history)
        self.assertEqual(detail["response_body"], body)

    def test_attempt_history_is_explicit_bounded_and_drops_unknown_fields(self):
        record = {
            "attempt": 1,
            "account_id": 7,
            "stage": "submit",
            "status": "failed",
            "error": "Bearer private-credential https://user:password@proxy.invalid/path",
            "rule_action": "LIMIT",
            "delay_seconds": 3,
            "duration_ms": 12,
            "cookie": "private-cookie-value",
            "proxy_url": "socks5://private-proxy",
        }
        result = _extract_attempt_history(
            {
                "attempt_history": {
                    "version": 1,
                    "total_attempts": 101,
                    "attempts": [record] * 101,
                }
            }
        )
        self.assertEqual(len(result["attempts"]), 100)
        self.assertTrue(result["truncated"])
        row = result["attempts"][0]
        self.assertNotIn("cookie", row)
        self.assertNotIn("proxy_url", row)
        self.assertNotIn("private-credential", row["error"])
        self.assertNotIn("password", row["error"])
        self.assertIsNone(_extract_attempt_history({"retry_trace": {"attempts": 3}}))
        self.assertIsNone(_extract_attempt_history("invalid JSON"))
        self.assertIsNone(
            _extract_attempt_history({"attempt_history": {"version": True, "attempts": []}})
        )

    def test_attempt_history_redacts_proxy_and_opaque_credentials_but_keeps_public_error(self):
        public_error = "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC_PLEASE_TRY_AGAIN_LATER"
        credential = "SyntheticOpaqueCredential0123456789" * 2
        result = _extract_attempt_history(
            {
                "attempt_history": {
                    "version": 1,
                    "attempts": [
                        {
                            "attempt": 1,
                            "error": f"{public_error} socks5h://user:private@proxy.invalid {credential}",
                            "error_code": public_error,
                        }
                    ],
                }
            }
        )
        row = result["attempts"][0]
        self.assertEqual(row["error_code"], public_error)
        self.assertIn(public_error, row["error"])
        self.assertNotIn("private", row["error"])
        self.assertNotIn("proxy.invalid", row["error"])
        self.assertNotIn(credential, row["error"])


class AdminLogDirectCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_function_imports_remain_callable(self):
        db = SimpleNamespace(get_logs=AsyncMock(return_value=[]))
        with patch.object(admin, "db", db):
            self.assertEqual(await admin.get_logs(limit=0, token="unused"), [])
        db.get_logs.assert_awaited_once_with(limit=1, include_payload=False)
