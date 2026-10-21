"""Refresh persistence and authenticated account-repair regressions."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Request, Response

from src.api import admin
from src.core.database import Database
from src.core.models import Token
from src.services.token_manager import TokenManager


class RefreshApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / "test.db"))
        await self.db.init_db()
        self.manager = TokenManager(self.db, Mock())
        self.manager._st_to_at_for_token = AsyncMock(
            return_value={
                "access_token": "fresh-at",
                "expires": "2030-01-01T00:00:00Z",
                "user": {"email": "test@example.com"},
            }
        )
        self.manager.enable_token = AsyncMock()
        self.manager.add_token = AsyncMock()
        self.manager._get_credits_for_token = AsyncMock(return_value={"credits": 100})
        api_patch = patch.multiple(
            admin, db=self.db, token_manager=self.manager, concurrency_manager=None
        )
        api_patch.start()
        self.addCleanup(api_patch.stop)
        self.token_id = await self.db.add_token(
            Token(
                st="expired-st",
                at="expired-at",
                email="test@example.com",
                at_expires=datetime.now(timezone.utc) - timedelta(hours=1),
                is_active=False,
                refresh_status="retry_wait",
                refresh_error="Previous error",
                refresh_failures=3,
                next_refresh_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await self.db.update_token(self.token_id, ban_reason="manual")

    async def test_expired_unchanged_st_allows_repair_and_preserves_manual_disable(self):
        result = await admin.update_token(
            self.token_id,
            admin.UpdateTokenRequest(
                st="expired-st", protocol_mode="protocol", google_cookies='{"SID":"test-cookie"}'
            ),
            token="test-admin",
        )
        saved = await self.db.get_token(self.token_id)
        self.assertTrue(result["success"])
        self.assertEqual(saved.google_cookies, '{"SID":"test-cookie"}')
        self.assertEqual(saved.at, "expired-at")
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")
        self.assertEqual(saved.refresh_failures, 0)
        self.assertEqual(saved.refresh_error, "")
        self.assertLessEqual(saved.next_refresh_at, datetime.now(timezone.utc))
        self.manager._st_to_at_for_token.assert_not_awaited()

    async def test_changed_st_cannot_replace_another_accounts_identity(self):
        self.manager._st_to_at_for_token.return_value["user"] = {"email": "other@example.com"}
        with self.assertRaises(HTTPException) as caught:
            await admin.update_token(self.token_id, admin.UpdateTokenRequest(st="other-account-st"))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual((await self.db.get_token(self.token_id)).st, "expired-st")

    async def test_changed_st_accepts_matching_googlemail_alias_and_keeps_saved_address(self):
        await self.db.update_token(self.token_id, email="test@gmail.com")
        self.manager._st_to_at_for_token.return_value["user"] = {"email": "test@googlemail.com"}
        result = await admin.update_token(
            self.token_id, admin.UpdateTokenRequest(st="googlemail-session"), token="test-admin"
        )
        self.assertTrue(result["success"])
        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.email, "test@gmail.com")
        self.assertEqual(saved.st, "googlemail-session")
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")

    async def test_manual_update_keeps_verified_rotation(self):
        self.manager._st_to_at_for_token.return_value["_session_token"] = "latest-st"
        result = await admin.update_token(
            self.token_id, admin.UpdateTokenRequest(st="new-user-supplied-st"), token="test-admin"
        )
        self.assertTrue(result["success"])
        self.assertEqual((await self.db.get_token(self.token_id)).st, "latest-st")
        self.assertFalse((await self.db.get_token(self.token_id)).is_active)

    async def test_bulk_import_and_direct_conversion_do_not_leak_private_rotation_field(self):
        self.manager.flow_client.st_to_at = AsyncMock(
            return_value={
                "access_token": "fresh-at",
                "expires": "2030-01-01T00:00:00Z",
                "user": {"email": "test@example.com"},
                "_session_token": "batch-latest-st",
            }
        )
        converted = await admin.st_to_at(admin.ST2ATRequest(st="supplied-st"), token="test-admin")
        self.assertNotIn("_session_token", converted)
        result = await admin.import_tokens(
            admin.ImportTokensRequest(tokens=[{"session_token": "supplied-st"}]), token="test-admin"
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual((await self.db.get_token(self.token_id)).st, "batch-latest-st")

    async def test_manual_refresh_forces_retry_and_returns_persisted_failure(self):
        self.manager._refresh_at = AsyncMock(return_value=False)
        response = await admin.refresh_at(self.token_id, token="test-admin")
        self.manager._refresh_at.assert_awaited_once_with(
            self.token_id, force=True, refresh_st=True
        )
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body["refresh_status"], "retry_wait")
        self.assertEqual(body["detail"], "Previous error")
        self.assertEqual(body["refresh_failures"], 3)
        self.assertNotIn("expired-st", response.body.decode())

    async def test_manual_protocol_refresh_verifies_cookies(self):
        await self.db.update_token(self.token_id, protocol_mode="protocol")
        self.manager._refresh_at = AsyncMock(return_value=True)
        await admin.refresh_at(self.token_id, token="test-admin")
        self.manager._refresh_at.assert_awaited_once_with(
            self.token_id, force=True, refresh_st=True
        )

    async def test_enable_validation_failure_returns_safe_bad_request(self):
        self.manager.enable_token.side_effect = ValueError("secret cookie from upstream")
        with self.assertRaises(HTTPException) as caught:
            await admin.enable_token(self.token_id, token="test-admin")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertNotIn("secret", caught.exception.detail)

    async def test_preserved_admin_cookie_login_and_logout(self):
        response = Response()
        with (
            patch.object(admin.AuthManager, "verify_admin", return_value=True),
            patch.object(
                self.db, "get_admin_config", AsyncMock(return_value=Mock(username="test"))
            ),
        ):
            result = await admin.login(
                admin.LoginRequest(username="test", password="test"), response
            )
        token = result["token"]
        self.addCleanup(admin.active_admin_tokens.discard, token)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        request = Request(
            {
                "type": "http",
                "headers": [(b"cookie", f"{admin.ADMIN_SESSION_COOKIE_NAME}={token}".encode())],
            }
        )
        self.assertEqual(await admin.verify_admin_token(request, authorization=None), token)
        self.assertEqual(
            await admin.verify_admin_token(request, authorization="Bearer stale"), token
        )
        self.assertTrue(admin.is_admin_session_token_valid(token))
        logout_response = Response()
        await admin.logout(logout_response, token)
        self.assertFalse(admin.is_admin_session_token_valid(token))
        self.assertIn("Max-Age=0", logout_response.headers["set-cookie"])
        with self.assertRaises(HTTPException):
            await admin.verify_admin_token(request, authorization=None)

    async def test_refresh_fields_persist_and_migrate_existing_accounts(self):
        timestamp = datetime.now(timezone.utc)
        await self.db.update_token(self.token_id, last_refresh_at=timestamp)
        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.last_refresh_at, timestamp)
        self.assertEqual(saved.refresh_failures, 3)
        # Recreate the pre-refresh schema, preserving its disabled account row.
        fields = (
            "refresh_status",
            "refresh_error",
            "refresh_failures",
            "next_refresh_at",
            "last_refresh_at",
        )
        with closing(sqlite3.connect(self.db.db_path)) as connection:
            with connection:
                for field in fields:
                    connection.execute(f"ALTER TABLE tokens DROP COLUMN {field}")
        with redirect_stdout(StringIO()):
            await self.db.check_and_migrate_db()
            await self.db.check_and_migrate_db()
        restored = await self.db.get_token(self.token_id)
        self.assertEqual(restored.refresh_status, "idle")
        self.assertEqual(restored.refresh_failures, 0)
        self.assertEqual(restored.st, "expired-st")
        self.assertFalse(restored.is_active)
        self.assertEqual(restored.ban_reason, "manual")
        row = (await admin.get_tokens(token="test-admin"))[0]
        self.assertTrue(set(fields).issubset(row))


if __name__ == "__main__":
    unittest.main()
