"""Account recovery must survive failures without crossing account boundaries."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.core.config import config
from src.core.database import Database
from src.core.models import Token
from src.services.browser.profiles import ProfileNotReadyError
from src.services.protocol_login import protocol_loginer
from src.services.token_manager import TokenManager


class TokenRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / "refresh.db"))
        await self.db.init_db()
        await self.db.update_token_refresh_config(enabled=True)
        self.manager = TokenManager(self.db, Mock())
        self.manager._st_to_at_for_token = AsyncMock(side_effect=self.session)
        self.manager._get_credits_for_token = AsyncMock(return_value={"credits": 100})
        self.login = AsyncMock(
            return_value={
                "success": True,
                "session_token": "new-st",
                "google_cookies": '{"SID":"rotated-cookie"}',
            }
        )
        login_patch = patch.object(protocol_loginer, "login", self.login)
        login_patch.start()
        self.addCleanup(login_patch.stop)

    async def test_explicit_viewer_context_uses_live_profile_without_old_protocol_cookies(self):
        token = Token(
            st="old-st",
            email="test@example.invalid",
            browser_profile_source="cloak",
            protocol_mode="protocol",
            google_cookies="SID=old-cookie",
        )
        refresher = Mock()
        refresher.has_active_context = Mock(return_value=True)
        refresher.refresh = AsyncMock(return_value={"session_token": "live-profile-session"})
        self.manager.profile_refresher = refresher
        result = await self.manager._try_refresh_st(1, token)
        self.assertEqual(result["session_token"], "live-profile-session")
        self.login.assert_not_awaited()
        refresher.refresh.assert_awaited_once_with(token)

    async def test_cloak_profile_source_uses_injected_profile_refresher(self):
        token = Token(st="old-st", email="test@example.invalid", browser_profile_source="cloak")
        refresher = Mock()
        refresher.refresh = AsyncMock(return_value={"session_token": "profile-session"})
        self.manager.profile_refresher = refresher
        result = await self.manager._try_refresh_st(token.id or 1, token)
        self.assertEqual(result, {"session_token": "profile-session", "_profile_used": True})
        refresher.refresh.assert_awaited_once_with(token)

    async def test_cloak_refresh_uses_profile_for_at_only_refresh(self):
        """A Cloak account has one recovery path even when ST rotation is not requested."""
        token_id = await self.add(
            browser_profile_source="cloak",
            protocol_mode="session",
            google_cookies="",
        )
        token = await self.db.get_token(token_id)
        self.manager._st_to_at_for_token.side_effect = self.session
        profile_session = {
            "st": "profile-session",
            "at": "new-at",
            "at_expires": datetime.now(timezone.utc) + timedelta(hours=8),
            "credits": 100,
            "user_paygate_tier": "PAYGATE_TIER_ONE",
        }
        refresher = Mock()
        refresher.refresh = AsyncMock(
            return_value={
                "session_token": "profile-session",
                "_validated_session": profile_session,
            }
        )
        self.manager.profile_refresher = refresher
        self.manager.profile_ready_callback = Mock()

        self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=False))

        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.st, "profile-session")
        self.assertEqual(saved.at, "new-at")
        self.assertEqual(saved.refresh_status, "ready")
        refresher.refresh.assert_awaited_once_with(token, force=True)
        self.login.assert_not_awaited()
        self.manager.profile_ready_callback.assert_called_once_with(token_id)

    async def test_signed_out_profile_stops_automatic_retries(self):
        self.manager._st_to_at_for_token.side_effect = None
        self.manager._st_to_at_for_token.return_value = {
            "access_token": "expired",
            "expires": "2000-01-01T00:00:00Z",
        }
        token_id = await self.add(
            browser_profile_source="cloak", protocol_mode="session", google_cookies=""
        )
        self.manager.profile_refresher = Mock()
        self.manager.profile_refresher.refresh = AsyncMock(
            side_effect=ProfileNotReadyError("synthetic", "login_required")
        )
        self.assertFalse(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.refresh_status, "login_required")
        self.assertEqual(saved.browser_profile_status, "login_required")
        self.assertIsNone(saved.next_refresh_at)
        self.assertIn("账号列表打开该账号浏览器", saved.refresh_error)
        self.assertFalse(saved.is_active)
        await self.manager.run_protocol_refresh_once()
        self.assertEqual(self.manager.profile_refresher.refresh.await_count, 1)

    async def test_profile_network_probe_failure_keeps_retry(self):
        token_id = await self.add(
            browser_profile_source="cloak", protocol_mode="session", google_cookies=""
        )
        self.manager.profile_refresher = Mock()
        self.manager.profile_refresher.refresh = AsyncMock(
            side_effect=ProfileNotReadyError("synthetic", "transient_error")
        )
        self.assertFalse(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.refresh_status, "retrying")
        self.assertIsNotNone(saved.next_refresh_at)

    async def test_signed_out_profile_with_usable_old_at_pauses_retries(self):
        token_id = await self.add(
            browser_profile_source="cloak", protocol_mode="session", google_cookies=""
        )
        self.manager.profile_refresher = Mock()
        self.manager.profile_refresher.refresh = AsyncMock(
            side_effect=ProfileNotReadyError("synthetic", "login_required")
        )
        self.assertFalse(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.browser_profile_status, "login_required")
        self.assertIsNone(saved.next_refresh_at)
        await self.manager.run_protocol_refresh_once()
        self.assertEqual(self.manager.profile_refresher.refresh.await_count, 1)

    async def test_profile_refresh_marks_profile_ready(self):
        token_id = await self.add(
            browser_profile_source="cloak", protocol_mode="session", google_cookies=""
        )
        self.manager.profile_refresher = Mock()
        self.manager.profile_refresher.refresh = AsyncMock(
            return_value={"session_token": "profile-session"}
        )
        self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.browser_profile_status, "ready")

    async def test_cloak_profile_is_canonical_even_with_stale_protocol_cookies(self):
        token = Token(
            st="old-st",
            email="test@example.invalid",
            browser_profile_source="cloak",
            protocol_mode="protocol",
            google_cookies="SID=expired",
        )
        refresher = Mock()
        refresher.refresh = AsyncMock(return_value={"session_token": "profile-session"})
        self.manager.profile_refresher = refresher
        self.login.return_value = {"success": False, "error_code": "invalid_cookies"}
        result = await self.manager._try_refresh_st(token.id or 1, token)
        self.assertEqual(result, {"session_token": "profile-session", "_profile_used": True})
        self.login.assert_not_awaited()
        refresher.refresh.assert_awaited_once_with(token)

    async def test_cloak_uses_session_validated_in_profile_without_server_requests(self):
        token = Token(
            st="old-st",
            email="test@example.invalid",
            browser_profile_source="cloak",
            protocol_mode="protocol",
            google_cookies="SID=valid-looking",
        )
        refresher = Mock()
        refresher.refresh = AsyncMock(
            return_value={
                "session_token": "profile-session",
                "_validated_session": {
                    "st": "profile-session",
                    "at": "new-at",
                    "at_expires": datetime.now(timezone.utc) + timedelta(hours=8),
                    "credits": 100,
                },
            }
        )
        self.manager.profile_refresher = refresher
        result = await self.manager._refresh_credentials(token, refresh_st=True)
        self.assertEqual(result["st"], "profile-session")
        self.manager._st_to_at_for_token.assert_not_awaited()
        refresher.refresh.assert_awaited_once_with(token)

    async def test_protocol_st_network_validation_failure_does_not_start_profile(self):
        token = Token(
            st="old-st",
            email="test@example.invalid",
            browser_profile_source="cookies",
            protocol_mode="protocol",
            google_cookies="SID=valid-looking",
        )
        refresher = Mock()
        refresher.refresh = AsyncMock()
        self.manager.profile_refresher = refresher
        self.manager._st_to_at_for_token.return_value = await self.session(
            token, "protocol-session"
        )
        self.manager._get_credits_for_token.side_effect = TimeoutError("network")
        with self.assertRaises(Exception):
            await self.manager._refresh_credentials(token, refresh_st=True)
        refresher.refresh.assert_not_awaited()

    async def test_cloak_profile_source_without_refresher_is_config_error(self):
        token = Token(st="old-st", email="test@example.invalid", browser_profile_source="cloak")
        with self.assertRaisesRegex(Exception, "Cloak Profile"):
            await self.manager._try_refresh_st(token.id or 1, token)

    async def test_profile_environment_change_is_not_reported_as_login_required(self):
        from src.services.browser.profiles import ProfileNotReadyError

        token = Token(st="old-st", email="test@example.invalid", browser_profile_source="cloak")
        refresher = Mock()
        refresher.refresh = AsyncMock(
            side_effect=ProfileNotReadyError(token.email, "environment_changed")
        )
        self.manager.profile_refresher = refresher
        with self.assertRaisesRegex(Exception, "environment_changed") as raised:
            await self.manager._try_refresh_st(token.id or 1, token)
        self.assertEqual(raised.exception.code, "config_error")

    async def test_browser_profile_source_rejects_unknown_value(self):
        with self.assertRaisesRegex(ValueError, "browser_profile_source"):
            self.manager._normalize_browser_profile_source("native")

    async def session(self, token, st):
        return {
            "access_token": "new-at",
            "user": {"email": token.email},
            "expires": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
        }

    async def test_supplied_session_keeps_identity_expiry_and_credits_checks(self):
        token = Token(st="old-st", email="test@example.invalid")
        valid = await self.session(token, token.st)
        for response in (
            {},
            {**valid, "access_token": ""},
            {**valid, "expires": "2000-01-01T00:00:00Z"},
            {**valid, "user": {"email": "other@example.invalid"}},
        ):
            with self.subTest(fields=list(response)):
                with self.assertRaises(Exception):
                    await self.manager._validate_refresh_session(token, token.st, session=response)
        self.manager._st_to_at_for_token.assert_not_awaited()
        self.manager._get_credits_for_token.assert_not_awaited()
        self.manager._get_credits_for_token.side_effect = ValueError("HTTP 401")
        with self.assertRaises(Exception):
            await self.manager._validate_refresh_session(token, token.st, session=valid)
        self.manager._st_to_at_for_token.assert_not_awaited()
        self.manager._get_credits_for_token.assert_awaited_once_with(token, valid["access_token"])

    async def test_session_googlemail_alias_preserves_stored_email_and_credit_validation(self):
        token = Token(st="old-st", email="user@gmail.com")
        response = {**await self.session(token, token.st), "user": {"email": "user@googlemail.com"}}
        result = await self.manager._validate_refresh_session(token, token.st, session=response)
        self.assertEqual(result["credits"], 100)
        self.assertEqual(token.email, "user@gmail.com")
        self.manager._get_credits_for_token.assert_awaited_once_with(
            token, response["access_token"]
        )

    async def test_session_googlemail_alias_does_not_accept_other_mailboxes(self):
        token = Token(st="old-st", email="user@gmail.com")
        for email in (
            "other@googlemail.com",
            "user+tag@googlemail.com",
            "u.ser@googlemail.com",
            "user@example.com",
        ):
            with self.subTest(email=email):
                response = {**await self.session(token, token.st), "user": {"email": email}}
                with self.assertRaises(Exception) as failure:
                    await self.manager._validate_refresh_session(token, token.st, session=response)
                self.assertEqual(failure.exception.code, "invalid_cookies")
        self.manager._get_credits_for_token.assert_not_awaited()

    async def add(self, **values):
        values = dict(
            {
                "st": "old-st",
                "at": "old-at",
                "email": "test@example.invalid",
                "at_expires": datetime.now(timezone.utc) - timedelta(minutes=1),
                "protocol_mode": "protocol",
                "google_cookies": '{"SID":"cookie"}',
            },
            **values,
        )
        return await self.db.add_token(Token(**values))

    async def test_legacy_browser_settings_do_not_claim_protocol_refresh(self):
        token_id = await self.add()
        with patch.dict(
            config._config,
            {
                "host_agent": {"enabled": True, "expected_email": "test@example.invalid"},
                "browser_recovery": {"enabled": True},
            },
        ):
            self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.st, "new-st")
        self.assertEqual(saved.refresh_status, "ready")
        self.login.assert_awaited_once()

    async def test_expired_at_from_session_triggers_protocol_oauth_recovery(self):
        token_id = await self.add()

        async def session_by_st(token, st):
            if st == "old-st":
                return {
                    "access_token": "expired-at",
                    "user": {"email": token.email},
                    "expires": "2000-01-01T00:00:00Z",
                }
            return {
                "access_token": "recovered-at",
                "user": {"email": token.email},
                "expires": "2030-01-01T00:00:00Z",
            }

        self.manager._st_to_at_for_token.side_effect = session_by_st
        self.login.return_value = {
            "success": True,
            "session_token": "recovered-st",
            "google_cookies": '{"SID":"rotated-cookie"}',
        }

        self.assertTrue(await self.manager._refresh_at(token_id, force=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.st, "recovered-st")
        self.assertEqual(saved.at, "recovered-at")
        self.assertEqual(saved.refresh_status, "ready")
        self.assertEqual(saved.google_cookies, '{"SID":"rotated-cookie"}')
        self.login.assert_awaited_once()

    async def test_expired_at_from_session_mode_uses_google_cookie_oauth_recovery(self):
        """Cookie-backed accounts recover expired ST even if imported as session mode."""
        token_id = await self.add(protocol_mode="session", google_cookies='{"SID":"cookie"}')

        async def session_by_st(token, st):
            if st == "old-st":
                return {
                    "access_token": "expired-at",
                    "user": {"email": token.email},
                    "expires": "2000-01-01T00:00:00Z",
                }
            return {
                "access_token": "recovered-at",
                "user": {"email": token.email},
                "expires": "2030-01-01T00:00:00Z",
            }

        self.manager._st_to_at_for_token.side_effect = session_by_st
        self.login.return_value = {
            "success": True,
            "session_token": "recovered-st",
            "google_cookies": '{"SID":"rotated-cookie"}',
        }

        self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=False))

        saved = await self.db.get_token(token_id)
        self.assertEqual((saved.st, saved.at), ("recovered-st", "recovered-at"))
        self.assertEqual(saved.refresh_status, "ready")
        self.assertEqual(saved.google_cookies, '{"SID":"rotated-cookie"}')
        self.login.assert_awaited_once()

    async def test_expiring_cookie_account_does_not_replay_st_session(self):
        """The scheduled AT-only call must promote to Cookie OAuth before expiry."""
        token_id = await self.add(protocol_mode="session", google_cookies='{"SID":"cookie"}')

        async def session_by_st(token, st):
            session = await self.session(token, st)
            if st == "old-st":
                session["expires"] = (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat()
            return session

        self.manager._st_to_at_for_token.side_effect = session_by_st
        self.login.return_value = {
            "success": True,
            "session_token": "renewed-st",
            "google_cookies": '{"SID":"rotated-cookie"}',
        }
        await self.db.update_token(
            token_id,
            at_expires=datetime.now(timezone.utc) + timedelta(minutes=30),
            next_refresh_at=datetime.now(timezone.utc),
        )

        self.assertTrue(await self.manager._refresh_at(token_id, force=False, refresh_st=False))

        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.st, "renewed-st")
        self.login.assert_awaited_once()

    async def test_transient_failure_backoff_survives_restart_and_recovers(self):
        token_id = await self.add()
        self.login.return_value = {
            "success": False,
            "error": "HTTP 503",
            "error_code": "transient_error",
        }
        await self.manager.run_protocol_refresh_once()
        failed = await self.db.get_token(token_id)
        self.assertFalse(failed.is_active)
        self.assertEqual(failed.ban_reason, "refresh_failed")
        self.assertEqual(failed.refresh_status, "retrying")
        self.assertEqual(failed.st, "old-st")
        restarted = TokenManager(self.db, Mock())
        restarted._refresh_at = AsyncMock()
        await restarted.run_protocol_refresh_once()
        restarted._refresh_at.assert_not_awaited()
        await self.db.update_token(
            token_id, next_refresh_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        self.login.return_value = {"success": True, "session_token": "new-st"}
        await self.manager.run_protocol_refresh_once()
        ready = await self.db.get_token(token_id)
        self.assertTrue(ready.is_active)
        self.assertIsNone(ready.ban_reason)
        self.assertEqual(ready.refresh_failures, 0)
        self.assertEqual(ready.refresh_status, "ready")
        self.assertEqual(ready.st, "new-st")

    async def test_manual_disabled_and_429_accounts_are_never_auto_enabled(self):
        for index, reason in enumerate(("manual", "429_rate_limit", "error_threshold", None)):
            token_id = await self.add(is_active=False, st=f"old-st-{index}")
            await self.db.update_token(token_id, ban_reason=reason)
        await self.manager.run_protocol_refresh_once()
        self.login.assert_not_awaited()
        self.assertTrue(await self.manager._refresh_at(token_id, force=True))
        self.assertFalse((await self.db.get_token(token_id)).is_active)

    async def test_manual_disabled_account_stays_disabled_after_explicit_refresh(self):
        token_id = await self.add(
            is_active=False,
            protocol_mode="session",
            google_cookies="",
        )
        await self.db.update_token(token_id, ban_reason="manual")

        self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=False))

        saved = await self.db.get_token(token_id)
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")
        self.assertEqual(saved.refresh_status, "ready")

    async def test_expiring_session_at_refreshes_even_with_protocol_switch_off(self):
        token_id = await self.add(protocol_mode="session", google_cookies="")
        await self.db.update_token_refresh_config(enabled=False)
        await self.manager.run_protocol_refresh_once()
        self.assertEqual((await self.db.get_token(token_id)).at, "new-at")
        self.login.assert_not_awaited()

    async def test_invalid_old_st_falls_back_to_protocol_and_commits_atomically(self):
        token_id = await self.add(browser_profile_source="cookies")
        token = await self.db.get_token(token_id)
        self.manager._st_to_at_for_token.side_effect = [
            ValueError("HTTP 401 UNAUTHENTICATED"),
            await self.session(token, "new-st"),
        ]
        self.assertTrue(await self.manager._refresh_at(token_id, force=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual((saved.st, saved.at), ("new-st", "new-at"))
        self.assertEqual(saved.google_cookies, '{"SID":"rotated-cookie"}')
        self.assertEqual(saved.last_st_refresh_result, "success")

    async def test_scoped_cookie_roundtrip_is_used_by_the_next_refresh(self):
        token_id = await self.add()
        rows = [
            {
                "name": "SID",
                "value": "rotated-root",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "hostOnly": False,
                "expirationDate": 2000000000,
            },
            {
                "name": "SID",
                "value": "account-path",
                "domain": "accounts.google.com",
                "path": "/oauth",
                "secure": True,
                "hostOnly": True,
            },
            {
                "name": "OSID",
                "value": "flow-only",
                "domain": "flow.google.com",
                "path": "/",
                "secure": True,
                "hostOnly": True,
            },
        ]
        self.login.return_value["google_cookies"] = json.dumps(rows)
        self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        reopened = Database(self.db.db_path)
        saved = await reopened.get_token(token_id)
        self.assertEqual(json.loads(saved.google_cookies), rows)
        restarted = TokenManager(reopened, Mock())
        restarted._st_to_at_for_token = AsyncMock(side_effect=self.session)
        restarted._get_credits_for_token = AsyncMock(return_value={"credits": 100})
        self.assertTrue(await restarted._refresh_at(token_id, force=True, refresh_st=True))
        self.assertEqual(json.loads(self.login.call_args.args[0]), rows)
        self.assertEqual(json.loads((await reopened.get_token(token_id)).google_cookies), rows)

    async def test_wrong_account_or_rejected_at_does_not_replace_credentials(self):
        for wrong_identity in (True, False):
            token_id = await self.add(st=f"old-st-{wrong_identity}")
            token = await self.db.get_token(token_id)
            session = await self.session(token, "new-st")
            if wrong_identity:
                session["user"]["email"] = "other@example.invalid"
            else:
                self.manager._get_credits_for_token.side_effect = ValueError("HTTP 401")
            self.manager._st_to_at_for_token.side_effect = None
            self.manager._st_to_at_for_token.return_value = session
            self.assertFalse(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
            saved = await self.db.get_token(token_id)
            if wrong_identity:
                self.assertEqual((saved.st, saved.at), (token.st, "old-at"))
            else:
                self.assertEqual(saved.st, token.st)
                self.assertEqual(saved.at, "old-at")
            self.assertEqual(saved.email, token.email)
            if wrong_identity:
                self.assertEqual(saved.refresh_status, "config_error")

    async def test_terminal_error_waits_for_repaired_material(self):
        token_id = await self.add()
        self.login.return_value = {
            "success": False,
            "error": "Sign in required",
            "error_code": "login_required",
        }
        self.manager._st_to_at_for_token.side_effect = ValueError("HTTP 401")
        await self.manager.run_protocol_refresh_once()
        self.assertEqual((await self.db.get_token(token_id)).refresh_status, "login_required")
        await self.manager.run_protocol_refresh_once()
        self.assertEqual(self.login.await_count, 1)
        await self.manager.update_token(token_id, google_cookies='{"SID":"new-cookie"}')
        self.manager._st_to_at_for_token.side_effect = self.session
        self.login.return_value = {"success": True, "session_token": "new-st"}
        await self.manager.run_protocol_refresh_once()
        self.assertTrue((await self.db.get_token(token_id)).is_active)

    async def test_early_failure_retains_usable_at_and_manual_disable_during_refresh(self):
        token_id = await self.add(at_expires=datetime.now(timezone.utc) + timedelta(minutes=30))
        self.manager._st_to_at_for_token.side_effect = TimeoutError("upstream timeout")
        retained = await self.manager.ensure_valid_token(await self.db.get_token(token_id))
        self.assertIsNotNone(retained)
        self.assertEqual(retained.at, "old-at")

        async def disable_while_reading(token, st):
            await self.manager.disable_token(token.id)
            return await self.session(token, st)

        self.manager._st_to_at_for_token.side_effect = disable_while_reading
        self.assertTrue(await self.manager._refresh_at(token_id, force=True))
        saved = await self.db.get_token(token_id)
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")

    async def test_four_workers_isolate_accounts_and_coalesce_same_account(self):
        for index in range(9):
            await self.add(email=f"account{index}@example.invalid", st=f"old-st-{index}")
        active = peak = calls = 0
        first_batch = asyncio.Event()

        async def login(*args, **kwargs):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            if peak == 4:
                first_batch.set()
            await asyncio.wait_for(first_batch.wait(), 2)
            await asyncio.sleep(0)
            active -= 1
            return {"success": True, "session_token": "new-st-" + kwargs["email"]}

        self.login.side_effect = login
        await self.manager.run_protocol_refresh_once()
        self.assertEqual((calls, peak), (9, 4))
        self.assertTrue(all(t.refresh_status == "ready" for t in await self.db.get_all_tokens()))
        calls = 0
        await asyncio.gather(
            *(self.manager._refresh_at(1, force=True, refresh_st=True) for _ in range(5))
        )
        self.assertEqual(calls, 1)

    async def test_500_healthy_cloak_accounts_skip_fixed_st_maintenance(self):
        now = datetime.now(timezone.utc)
        tokens = [
            Token(
                id=index + 1,
                email=f"account{index}@example.invalid",
                st=f"st-{index}",
                at=f"at-{index}",
                at_expires=now + timedelta(hours=8),
                browser_profile_source="cloak",
                protocol_mode="protocol",
                last_st_refresh_at=now - timedelta(hours=12),
                next_refresh_at=now - timedelta(minutes=1),
            )
            for index in range(500)
        ]
        self.manager._refresh_at = AsyncMock()
        with patch.object(self.db, "get_all_tokens", AsyncMock(return_value=tokens)):
            await self.manager.run_protocol_refresh_once()
        self.manager._refresh_at.assert_not_awaited()
        self.login.assert_not_awaited()

    async def test_healthy_cloak_at_uses_existing_validation_without_opening_profile(self):
        token_id = await self.add(
            browser_profile_source="cloak",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=8),
        )
        self.manager.profile_refresher = Mock(refresh=AsyncMock())
        await self.manager.run_protocol_refresh_once()
        valid = await self.manager.ensure_valid_token(await self.db.get_token(token_id))
        self.assertEqual(valid.at, "old-at")
        self.manager.profile_refresher.refresh.assert_not_awaited()
        self.manager._st_to_at_for_token.assert_not_awaited()
        self.login.assert_not_awaited()

    async def test_cloak_renewal_uses_original_profile_and_schedules_actual_expiry(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=36)
        self.manager.profile_refresher = Mock(refresh=AsyncMock())

        async def refresh(token, **kwargs):
            return {
                "session_token": f"profile-st-{token.id}",
                "_validated_session": {
                    "st": f"profile-st-{token.id}",
                    "at": "profile-at",
                    "at_expires": expiry,
                    "credits": 100,
                },
            }

        self.manager.profile_refresher.refresh.side_effect = refresh
        for mode in ("protocol", "session"):
            token_id = await self.add(
                st=f"old-{mode}",
                email=f"{mode}@example.invalid",
                browser_profile_source="cloak",
                protocol_mode=mode,
                at_expires=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
            await self.manager.run_protocol_refresh_once()
            saved = await self.db.get_token(token_id)
            self.assertEqual(saved.at, "profile-at")
            self.assertEqual(saved.next_refresh_at, expiry - timedelta(hours=1))
            self.assertFalse(self.manager.profile_refresher.refresh.call_args.kwargs)
        self.manager._st_to_at_for_token.assert_not_awaited()
        self.login.assert_not_awaited()

    async def test_rejected_cloak_at_forces_original_profile_even_before_scheduled_refresh(self):
        token_id = await self.add(
            browser_profile_source="cloak",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=8),
            next_refresh_at=datetime.now(timezone.utc) + timedelta(hours=7),
        )
        token = await self.db.get_token(token_id)
        self.manager._get_credits_for_token.side_effect = ValueError("HTTP 401")
        self.manager.profile_refresher = Mock(
            refresh=AsyncMock(
                return_value={
                    "session_token": "profile-st",
                    "_validated_session": {
                        "st": "profile-st",
                        "at": "profile-at",
                        "at_expires": datetime.now(timezone.utc) + timedelta(hours=8),
                        "credits": 100,
                    },
                }
            )
        )
        valid = await self.manager.ensure_valid_token(token)
        self.assertEqual(valid.at, "profile-at")
        self.manager.profile_refresher.refresh.assert_awaited_once_with(token, force=True)
        self.login.assert_not_awaited()

    async def test_cookie_account_keeps_fixed_st_maintenance_and_interval(self):
        token_id = await self.add(
            at_expires=datetime.now(timezone.utc) + timedelta(hours=8),
            last_st_refresh_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        before = datetime.now(timezone.utc)
        await self.manager.run_protocol_refresh_once()
        saved = await self.db.get_token(token_id)
        self.login.assert_awaited_once()
        self.assertEqual(saved.st, "new-st")
        self.assertGreaterEqual(saved.next_refresh_at, before + timedelta(minutes=120))
        self.assertLessEqual(
            saved.next_refresh_at, datetime.now(timezone.utc) + timedelta(minutes=120)
        )

    async def test_disabled_cloak_skips_scheduler_and_stays_disabled_after_force(self):
        token_id = await self.add(browser_profile_source="cloak", is_active=False)
        await self.db.update_token(token_id, ban_reason="manual")
        self.manager.profile_refresher = Mock(
            refresh=AsyncMock(return_value={"session_token": "profile-st"})
        )
        await self.manager.run_protocol_refresh_once()
        self.manager.profile_refresher.refresh.assert_not_awaited()
        self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")
        self.assertEqual(saved.st, "profile-st")

    async def test_500_due_profiles_use_manager_capacity_and_separate_cookie_workers(self):
        for slots in (1, 2):
            with self.subTest(slots=slots):
                tokens = [
                    Token(
                        id=index + 1,
                        email=f"account{index}@example.invalid",
                        st=f"st-{index}",
                        browser_profile_source="cloak" if index < 500 else "cookies",
                        protocol_mode="protocol",
                    )
                    for index in range(509)
                ]
                active, peak, calls = [0, 0], [0, 0], [0, 0]
                first_batch = asyncio.Event()

                async def refresh(token_id, *, refresh_st):
                    kind = int(token_id > 500)
                    self.assertEqual(refresh_st, bool(kind))
                    active[kind] += 1
                    calls[kind] += 1
                    peak[kind] = max(peak[kind], active[kind])
                    if peak == [slots, 4]:
                        first_batch.set()
                    try:
                        await asyncio.wait_for(first_batch.wait(), 2)
                        await asyncio.sleep(0)
                    finally:
                        active[kind] -= 1
                    return True

                self.manager.profile_refresher = Mock(manager=Mock(max_concurrent=slots))
                self.manager._refresh_at = AsyncMock(side_effect=refresh)
                with patch.object(self.db, "get_all_tokens", AsyncMock(return_value=tokens)):
                    await self.manager.run_protocol_refresh_once()
                self.assertEqual(calls, [500, 9])
                self.assertEqual(peak, [slots, 4])
                self.assertEqual(active, [0, 0])

    async def test_stronger_st_refresh_waiting_behind_at_refresh_runs_followup(self):
        gate = asyncio.Event()
        calls = []

        async def refresh(token_id, *, force=False, refresh_st=False):
            calls.append((force, refresh_st))
            if len(calls) == 1:
                await gate.wait()
            return True

        self.manager._refresh_at_inner = refresh
        first = asyncio.create_task(self.manager._refresh_at(1, refresh_st=False))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.manager._refresh_at(1, refresh_st=True))
        await asyncio.sleep(0)
        gate.set()
        self.assertTrue(await first)
        self.assertTrue(await second)
        self.assertEqual(calls, [(False, False), (True, True)])
        self.assertFalse(self.manager._refresh_intents)

    async def test_google_login_expired_does_not_block_old_st_renewal(self):
        token_id = await self.add()
        self.login.return_value = {
            "success": False,
            "error": "Sign in required",
            "error_code": "login_required",
        }
        await self.manager.run_protocol_refresh_once()
        saved = await self.db.get_token(token_id)
        self.assertTrue(saved.is_active)
        self.assertEqual(saved.refresh_status, "login_required")
        self.assertEqual(saved.at, "new-at")
        await self.db.update_token(
            token_id, at_expires=datetime.now(timezone.utc), next_refresh_at=None
        )
        await self.manager.run_protocol_refresh_once()
        self.assertEqual(self.login.await_count, 1)
        self.assertEqual(self.manager._st_to_at_for_token.await_count, 2)
        self.assertTrue((await self.db.get_token(token_id)).is_active)

    async def test_session_rotation_survives_restart_without_claiming_cookie_recovery(self):
        token_id = await self.add()
        account = await self.db.get_token(token_id)
        session = await self.session(account, account.st)
        session["_session_token"] = "rotated-session-st"
        self.manager._st_to_at_for_token.side_effect = None
        self.manager._st_to_at_for_token.return_value = session
        self.login.return_value = {
            "success": False,
            "error": "Sign in required",
            "error_code": "login_required",
        }
        self.assertFalse(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        reopened = Database(self.db.db_path)
        saved = await reopened.get_token(token_id)
        self.assertEqual(saved.st, "rotated-session-st")
        self.assertEqual(saved.google_cookies, account.google_cookies)
        self.assertEqual(saved.refresh_status, "login_required")
        self.assertIsNone(saved.last_st_refresh_at)
        self.assertEqual(saved.at_expires, self.manager._parse_at_expires(session["expires"]))

    async def test_add_token_keeps_the_session_cookie_returned_during_validation(self):
        flow = Mock()
        flow.st_to_at = AsyncMock(
            return_value={
                "access_token": "synthetic-at",
                "user": {"email": "new@example.invalid"},
                "expires": "2030-01-01T00:00:00Z",
                "_session_token": "rotated-import-st",
            }
        )
        flow.get_credits = AsyncMock(return_value={"credits": 100})
        flow.proxy_manager = None
        manager = TokenManager(self.db, flow)
        manager._get_project_pool_size = Mock(return_value=1)
        created = await manager.add_token("supplied-st", project_id="synthetic-project")
        self.assertEqual((await self.db.get_token(created.id)).st, "rotated-import-st")

    async def test_manual_add_cannot_create_uninitialized_cloak_profile(self):
        with self.assertRaisesRegex(ValueError, "initialized through direct browser login"):
            await self.manager.add_token("synthetic-st", browser_profile_source="cloak")

    async def test_existing_cloak_account_can_update_non_profile_fields(self):
        token_id = await self.add(browser_profile_source="cloak")
        await self.manager.update_token(token_id, remark="updated without changing source")
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.remark, "updated without changing source")
        self.assertEqual(saved.browser_profile_source, "cloak")

    async def test_rotated_session_never_overwrites_unverified_or_replaced_credentials(self):
        for failure in ("identity", "credits", "replacement"):
            with self.subTest(failure=failure):
                token_id = await self.add(st=f"old-{failure}")
                account = await self.db.get_token(token_id)
                session = await self.session(account, account.st)
                session["_session_token"] = "uncommitted-rotation"
                if failure == "identity":
                    session["user"]["email"] = "other@example.invalid"
                self.manager._get_credits_for_token.side_effect = (
                    ValueError("HTTP 401") if failure == "credits" else None
                )

                async def respond(token, st):
                    if failure == "replacement":
                        await self.db.update_token(token.id, st="manually-replaced")
                    return session

                self.manager._st_to_at_for_token.side_effect = respond
                self.assertFalse(await self.manager._refresh_at(token_id, force=True))
                saved = await self.db.get_token(token_id)
                expected_st = {
                    "identity": account.st,
                    "credits": "new-st",
                    "replacement": "manually-replaced",
                }[failure]
                if failure == "credits":
                    expected_st = account.st
                self.assertEqual(saved.st, expected_st)

    async def test_temporary_credits_failure_keeps_verified_rotated_material(self):
        token_id = await self.add()
        account = await self.db.get_token(token_id)
        session = await self.session(account, "new-st")
        session["_session_token"] = "latest-rotated-st"
        self.manager._st_to_at_for_token.side_effect = None
        self.manager._st_to_at_for_token.return_value = session
        self.manager._get_credits_for_token.side_effect = TimeoutError("credits timeout")
        self.login.return_value = {
            "success": True,
            "session_token": "new-st",
            "google_cookies": json.dumps(
                [{"name": "SID", "value": "new-cookie", "domain": ".google.com", "path": "/"}]
            ),
        }
        self.assertFalse(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.st, "latest-rotated-st")
        self.assertEqual(json.loads(saved.google_cookies)[0]["value"], "new-cookie")
        self.assertEqual(saved.at, "old-at")
        self.assertEqual(saved.refresh_status, "retrying")

    async def test_concurrent_session_rotations_remain_bound_to_their_accounts(self):
        ids = [
            await self.add(st=f"st-{n}", email=f"user{n}@example.invalid", google_cookies="")
            for n in range(2)
        ]

        async def rotate(token, st):
            await asyncio.sleep(0)
            result = await self.session(token, st)
            result["_session_token"] = f"rotated-{token.id}"
            return result

        self.manager._st_to_at_for_token.side_effect = rotate
        self.assertEqual(
            await asyncio.gather(*(self.manager._refresh_at(i, force=True) for i in ids)),
            [True, True],
        )
        for token_id in ids:
            self.assertEqual((await self.db.get_token(token_id)).st, f"rotated-{token_id}")
        self.login.assert_not_awaited()

    async def test_protocol_renewal_uses_account_proxy_then_global_proxy(self):
        await self.db.update_proxy_config(enabled=True, proxy_url="http://global.test:8080")
        token_id = await self.add()
        account = await self.db.get_token(token_id)
        await self.manager._try_protocol_refresh_st(token_id, account)
        self.assertEqual(self.login.call_args.kwargs["proxy"], "http://global.test:8080")
        account.proxy_url = "http://account.test:8080"
        await self.manager._try_protocol_refresh_st(token_id, account)
        self.assertEqual(self.login.call_args.kwargs["proxy"], "http://account.test:8080")
        self.login.reset_mock()
        account.proxy_url = "ftp://invalid.test:80"
        with self.assertRaises(Exception) as failed:
            await self.manager._try_protocol_refresh_st(token_id, account)
        self.assertEqual(failed.exception.code, "invalid_proxy")
        self.login.assert_not_awaited()

    async def test_login_warning_with_temporary_st_failure_still_retries(self):
        token_id = await self.add(refresh_status="login_required", refresh_error="Sign in required")
        self.manager._st_to_at_for_token.side_effect = TimeoutError("temporary network failure")
        await self.manager.run_protocol_refresh_once()
        failed = await self.db.get_token(token_id)
        self.assertEqual(failed.refresh_status, "retrying")
        self.assertIsNotNone(failed.next_refresh_at)
        await self.db.update_token(token_id, next_refresh_at=None)
        self.manager._st_to_at_for_token.side_effect = self.session
        await self.manager.run_protocol_refresh_once()
        self.assertTrue((await self.db.get_token(token_id)).is_active)

    async def test_refresh_timeout_records_retry_without_replacing_credentials(self):
        token_id = await self.add()

        async def timeout_without_waiting(coroutine, seconds):
            self.assertEqual(seconds, 120)
            coroutine.close()
            raise asyncio.TimeoutError()

        with patch(
            "src.services.token_manager.asyncio.wait_for", side_effect=timeout_without_waiting
        ):
            self.assertFalse(await self.manager._refresh_at(token_id, force=True, refresh_st=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.refresh_status, "retrying")
        self.assertEqual(saved.refresh_failures, 1)
        self.assertGreater(saved.next_refresh_at, datetime.now(timezone.utc))
        self.assertEqual((saved.st, saved.at), ("old-st", "old-at"))
        self.assertEqual(saved.ban_reason, "refresh_failed")
        self.assertFalse(self.manager._refresh_futures)

    async def test_shutdown_cancels_refresh_after_its_waiter_was_cancelled(self):
        token_id = await self.add()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def pending_refresh(token, refresh_st, *, force=False):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.manager._refresh_credentials = AsyncMock(side_effect=pending_refresh)
        self.manager.start_protocol_refresher()
        loop = self.manager._protocol_refresher_task
        waiter = asyncio.create_task(self.manager._refresh_at(token_id, force=True))
        await asyncio.wait_for(started.wait(), 2)
        refresh = self.manager._refresh_futures[token_id]
        try:
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertFalse(refresh.done())
            await asyncio.wait_for(self.manager.stop_protocol_refresher(), 2)
            self.assertTrue(cancelled.is_set())
            self.assertTrue(refresh.cancelled())
            self.assertTrue(loop.cancelled())
            self.assertFalse(self.manager._refresh_futures)
            self.assertIsNone(self.manager._protocol_refresher_task)
        finally:
            for task in (waiter, refresh, loop):
                task.cancel()
            await asyncio.gather(waiter, refresh, loop, return_exceptions=True)

    async def test_final_write_cannot_overwrite_new_credentials_or_manual_disable(self):
        original = self.db.update_token_if_credentials_match
        for change, failed in (("credentials", False), ("manual", False), ("manual", True)):
            with self.subTest(change=change, failed=failed):
                token_id = await self.add(st=f"race-{change}-{failed}", ban_reason="refresh_failed")
                self.manager._st_to_at_for_token.side_effect = (
                    TimeoutError("network timeout") if failed else self.session
                )

                async def race(tid, expected, **updates):
                    if updates.get("refresh_status") != "refreshing":
                        if change == "credentials":
                            await self.db.update_token(
                                tid, st="manual-st", google_cookies="SID=manual"
                            )
                        else:
                            await self.manager.disable_token(tid)
                    return await original(tid, expected, **updates)

                with patch.object(self.db, "update_token_if_credentials_match", side_effect=race):
                    self.assertFalse(await self.manager._refresh_at(token_id, force=True))
                saved = await self.db.get_token(token_id)
                if change == "credentials":
                    self.assertEqual((saved.st, saved.google_cookies), ("manual-st", "SID=manual"))
                else:
                    self.assertFalse(saved.is_active)
                    self.assertEqual(saved.ban_reason, "manual")

    async def test_network_errors_with_auth_like_text_do_not_disable_valid_at(self):
        for text in (
            "proxy http://example.invalid:4010 connection timed out",
            "access_token request timed out",
            "HTTP Error 503 while fetching access token",
            "request id 401 did not complete",
        ):
            with self.subTest(text=text):
                token_id = await self.add(
                    st=text,
                    google_cookies="",
                    at_expires=datetime.now(timezone.utc) + timedelta(hours=8),
                )
                self.manager._st_to_at_for_token.side_effect = TimeoutError(text)
                self.assertFalse(await self.manager._refresh_at(token_id, force=True))
                self.assertTrue((await self.db.get_token(token_id)).is_active)
                self.login.assert_not_awaited()
        for text in ("HTTP 401", "HTTP Error 403", "401 Unauthorized", "UNAUTHENTICATED"):
            self.assertTrue(self.manager._is_auth_error(ValueError(text)))

    async def test_session_only_rotation_survives_temporary_credits_error(self):
        token_id = await self.add(protocol_mode="session", google_cookies="")
        account = await self.db.get_token(token_id)
        session = await self.session(account, account.st)
        session["_session_token"] = "latest-session-only"
        self.manager._st_to_at_for_token.side_effect = None
        self.manager._st_to_at_for_token.return_value = session
        self.manager._get_credits_for_token.side_effect = TimeoutError("credits timeout")
        self.assertFalse(await self.manager._refresh_at(token_id, force=True))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.st, "latest-session-only")
        self.assertEqual(saved.at, "old-at")
        self.assertFalse(saved.is_active, "expired old AT is not reactivated by credential salvage")
        self.assertEqual(saved.refresh_status, "retrying")
        self.login.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
