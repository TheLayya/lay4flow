"""Cloak maintenance controls reschedule the real per-account cycle atomically."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from src.core.database import Database
from src.core.models import Token
from src.services.token_manager import TokenManager


class ProfileRefreshIntervalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(str(Path(temporary.name) / "synthetic.db"))
        await self.db.init_db()
        self.manager = TokenManager(self.db, object())
        self.anchor = datetime.now(timezone.utc) - timedelta(minutes=20)
        self.account_id = await self.db.add_token(
            Token(
                st="profile-session:synthetic",
                email="profile@example.invalid",
                browser_profile_source="cloak",
                browser_profile_status="ready",
                refresh_status="ready",
                at=None,
                at_expires=None,
                refresh_interval_minutes=120,
                auto_refresh_enabled=True,
                last_st_refresh_at=self.anchor,
                last_st_refresh_result="success",
                browser_profile_verified_at=self.anchor + timedelta(minutes=15),
                last_refresh_at=self.anchor + timedelta(minutes=15),
                next_refresh_at=self.anchor + timedelta(minutes=120),
                browser_profile_next_at=self.anchor + timedelta(minutes=120),
                google_cookies='{"SID":"synthetic-cookie"}',
            )
        )
        # Existing account creation stores authentication first; admission then
        # commits the Profile verification fields in its own update.
        await self.db.update_token(
            self.account_id,
            browser_profile_status="ready",
            browser_profile_verified_at=self.anchor + timedelta(minutes=15),
            browser_profile_next_at=self.anchor + timedelta(minutes=120),
        )
        self.manager._refresh_credentials = AsyncMock(
            return_value={
                "at": None,
                "at_expires": None,
                "credits": 55,
                "last_st_refresh_result": "success",
                "browser_profile_status": "ready",
                "_browser_renewed": True,
            }
        )
        self.manager.profile_refresher = Mock()

    async def test_shorter_interval_is_due_now_without_using_recent_http_probe_as_anchor(self):
        before = datetime.now(timezone.utc)
        await self.manager.update_token(self.account_id, refresh_interval_minutes=10)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.browser_profile_next_at, self.anchor + timedelta(minutes=10))
        self.assertGreaterEqual(saved.next_refresh_at, before)
        self.assertLessEqual(saved.next_refresh_at, datetime.now(timezone.utc))
        self.assertFalse(self.manager._profile_session_fresh(saved))
        self.assertTrue(self.manager._profile_cycle_anchor_valid(saved))
        self.manager._refresh_credentials.assert_not_awaited()

    async def test_longer_interval_is_rescheduled_from_last_browser_renewal(self):
        await self.manager.update_token(self.account_id, refresh_interval_minutes=180)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.next_refresh_at, self.anchor + timedelta(minutes=180))
        self.assertEqual(saved.browser_profile_next_at, saved.next_refresh_at)
        self.assertTrue(self.manager._profile_session_fresh(saved))
        self.manager._refresh_credentials.assert_not_awaited()

    async def test_disable_clears_background_deadline_and_reenable_restores_original_cycle(self):
        await self.manager.update_token(self.account_id, auto_refresh_enabled=False)
        saved = await self.db.get_token(self.account_id)
        self.assertIsNone(saved.next_refresh_at)
        self.assertIsNone(saved.browser_profile_next_at)
        self.assertEqual(saved.last_st_refresh_at, self.anchor)
        self.assertTrue(self.manager._profile_cycle_anchor_valid(saved))
        self.assertTrue(self.manager._profile_session_fresh(saved))
        await self.manager.update_token(self.account_id, auto_refresh_enabled=True)
        resumed = await self.db.get_token(self.account_id)
        self.assertEqual(resumed.next_refresh_at, self.anchor + timedelta(minutes=120))
        self.assertEqual(resumed.browser_profile_next_at, resumed.next_refresh_at)
        self.manager._refresh_credentials.assert_not_awaited()

    async def test_interval_saved_while_disabled_waits_for_reenable(self):
        await self.manager.update_token(self.account_id, auto_refresh_enabled=False)
        await self.manager.update_token(self.account_id, refresh_interval_minutes=40)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.refresh_interval_minutes, 40)
        self.assertIsNone(saved.next_refresh_at)
        await self.manager.update_token(self.account_id, auto_refresh_enabled=True)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.next_refresh_at, self.anchor + timedelta(minutes=40))

    async def test_interval_uses_local_ui_bounds(self):
        await self.manager.update_token(self.account_id, refresh_interval_minutes=2000)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.refresh_interval_minutes, 1440)
        self.assertEqual(saved.next_refresh_at, self.anchor + timedelta(minutes=1440))

    async def test_edit_does_not_recover_a_signed_out_profile_or_remove_backoff(self):
        due = datetime.now(timezone.utc) + timedelta(minutes=3)
        for state in ("login_required", "retrying"):
            with self.subTest(state=state):
                await self.db.update_token(
                    self.account_id,
                    refresh_interval_minutes=120,
                    browser_profile_status="login_required",
                    refresh_status=state,
                    next_refresh_at=due if state == "retrying" else None,
                    browser_profile_next_at=due if state == "retrying" else None,
                )
                await self.manager.update_token(self.account_id, refresh_interval_minutes=10)
                saved = await self.db.get_token(self.account_id)
                self.assertEqual(saved.refresh_status, state)
                self.assertEqual(saved.next_refresh_at, due if state == "retrying" else None)
        self.manager._refresh_credentials.assert_not_awaited()

    async def test_inflight_success_or_failure_cannot_restore_previous_schedule(self):
        for fail, edit in (
            (False, {"refresh_interval_minutes": 15}),
            (True, {"refresh_interval_minutes": 180}),
            (False, {"auto_refresh_enabled": False}),
            (True, {"auto_refresh_enabled": False}),
        ):
            with self.subTest(fail=fail, edit=edit):
                await self.db.update_token(
                    self.account_id,
                    auto_refresh_enabled=True,
                    refresh_interval_minutes=120,
                    refresh_status="ready",
                    browser_profile_status="ready",
                    last_st_refresh_at=self.anchor,
                    next_refresh_at=self.anchor + timedelta(minutes=120),
                    browser_profile_next_at=self.anchor + timedelta(minutes=120),
                )
                entered, release = asyncio.Event(), asyncio.Event()

                async def refresh(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    if fail:
                        raise TimeoutError("synthetic timeout")
                    return {
                        "at": None,
                        "at_expires": None,
                        "credits": 999,
                        "_browser_renewed": True,
                        "browser_profile_status": "ready",
                    }

                self.manager._refresh_credentials.side_effect = refresh
                task = asyncio.create_task(self.manager._refresh_at(self.account_id, force=True))
                await entered.wait()
                await self.manager.update_token(self.account_id, **edit)
                expected = (await self.db.get_token(self.account_id)).model_dump()
                self.assertNotEqual(expected["refresh_status"], "refreshing")
                release.set()
                self.assertFalse(await task)
                self.assertEqual((await self.db.get_token(self.account_id)).model_dump(), expected)

    async def test_legacy_cookie_account_schedule_is_not_changed_by_cloak_controls(self):
        await self.db.update_token(self.account_id, browser_profile_source="cookies")
        before = await self.db.get_token(self.account_id)
        await self.manager.update_token(self.account_id, refresh_interval_minutes=15)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.refresh_interval_minutes, 15)
        self.assertEqual(saved.next_refresh_at, before.next_refresh_at)
        self.assertEqual(saved.browser_profile_next_at, before.browser_profile_next_at)

    async def test_manual_refresh_of_auto_disabled_account_never_schedules_background_work(self):
        await self.manager.update_token(self.account_id, auto_refresh_enabled=False)
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.manager._refresh_credentials.side_effect = (
                    TimeoutError("synthetic timeout") if fail else None
                )
                await self.manager._refresh_at(self.account_id, force=True)
                saved = await self.db.get_token(self.account_id)
                self.assertFalse(saved.auto_refresh_enabled)
                self.assertIsNone(saved.next_refresh_at)
                self.assertIsNone(saved.browser_profile_next_at)
