"""One global maintenance policy drives every Cloak account's own cycle."""

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
        await self.db.update_profile_maintenance_config(True, 120)
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
        await self.db.update_profile_maintenance_config(True, 10)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.browser_profile_next_at, self.anchor + timedelta(minutes=10))
        self.assertGreaterEqual(saved.next_refresh_at, before)
        self.assertLessEqual(saved.next_refresh_at, datetime.now(timezone.utc))
        self.assertFalse(self.manager._profile_session_fresh(saved))
        self.assertTrue(self.manager._profile_cycle_anchor_valid(saved))
        self.manager._refresh_credentials.assert_not_awaited()

    async def test_longer_interval_is_rescheduled_from_last_browser_renewal(self):
        await self.db.update_profile_maintenance_config(True, 180)
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

    async def test_global_interval_saved_while_account_disabled_waits_for_reenable(self):
        await self.manager.update_token(self.account_id, auto_refresh_enabled=False)
        await self.db.update_profile_maintenance_config(True, 40)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.refresh_interval_minutes, 120)
        self.assertIsNone(saved.next_refresh_at)
        await self.manager.update_token(self.account_id, auto_refresh_enabled=True)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.next_refresh_at, self.anchor + timedelta(minutes=40))

    async def test_interval_rejects_invalid_bounds_and_types_without_writing(self):
        original = await self.db.get_profile_maintenance_config()
        for enabled, interval in ((True, 0), (True, 1441), (True, 1.5), (True, "30"), (1, 30)):
            with self.subTest(enabled=enabled, interval=interval):
                with self.assertRaises(ValueError):
                    await self.db.update_profile_maintenance_config(enabled, interval)
                self.assertEqual(await self.db.get_profile_maintenance_config(), original)

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
                await self.db.update_profile_maintenance_config(True, 10)
                saved = await self.db.get_token(self.account_id)
                self.assertEqual(saved.refresh_status, state)
                self.assertEqual(saved.next_refresh_at, due if state == "retrying" else None)
        self.manager._refresh_credentials.assert_not_awaited()

    async def test_inflight_success_or_failure_cannot_restore_previous_schedule(self):
        for fail, edit in (
            (False, {"enabled": True, "refresh_interval_minutes": 15}),
            (True, {"enabled": True, "refresh_interval_minutes": 180}),
            (False, {"enabled": False, "refresh_interval_minutes": 30}),
            (True, {"enabled": False, "refresh_interval_minutes": 30}),
            (False, {"auto_refresh_enabled": False}),
            (True, {"auto_refresh_enabled": False}),
        ):
            with self.subTest(fail=fail, edit=edit):
                await self.db.update_profile_maintenance_config(True, 120)
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
                if "enabled" in edit:
                    await self.db.update_profile_maintenance_config(**edit)
                else:
                    await self.manager.update_token(self.account_id, **edit)
                expected = (await self.db.get_token(self.account_id)).model_dump()
                self.assertNotEqual(expected["refresh_status"], "refreshing")
                release.set()
                self.assertFalse(await task)
                self.assertEqual((await self.db.get_token(self.account_id)).model_dump(), expected)

    async def test_legacy_cookie_account_schedule_is_not_changed_by_cloak_controls(self):
        await self.db.update_token(self.account_id, browser_profile_source="cookies")
        before = await self.db.get_token(self.account_id)
        await self.db.update_profile_maintenance_config(True, 15)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.refresh_interval_minutes, 120)
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

    async def test_global_switch_skips_all_profiles_and_manual_refresh_stays_available(self):
        await self.db.update_profile_maintenance_config(False, 30)
        await self.manager.run_protocol_refresh_once()
        self.assertFalse(await self.manager._refresh_at(self.account_id))
        self.manager._refresh_credentials.assert_not_awaited()
        saved = await self.db.get_token(self.account_id)
        self.assertTrue(saved.auto_refresh_enabled)
        self.assertIsNone(saved.next_refresh_at)
        self.assertTrue(await self.manager._refresh_at(self.account_id, force=True))
        saved = await self.db.get_token(self.account_id)
        self.assertIsNone(saved.next_refresh_at)
        self.assertIsNone(saved.browser_profile_next_at)
        self.assertTrue(saved.is_active)

    async def test_account_legacy_interval_does_not_override_unified_interval(self):
        await self.manager.update_token(self.account_id, refresh_interval_minutes=600)
        await self.db.update_profile_maintenance_config(True, 10)
        before = datetime.now(timezone.utc)
        await self.manager.run_protocol_refresh_once()
        self.manager._refresh_credentials.assert_awaited_once()
        saved = await self.db.get_token(self.account_id)
        self.assertGreaterEqual(saved.next_refresh_at, before + timedelta(minutes=10))
        self.assertEqual(saved.next_refresh_at, saved.last_st_refresh_at + timedelta(minutes=10))

    async def test_restart_loads_saved_global_policy_and_keeps_account_staggering(self):
        second_anchor = self.anchor + timedelta(minutes=8)
        second = await self.db.add_token(
            Token(
                st="profile-session:second",
                email="second@example.invalid",
                browser_profile_source="cloak",
            )
        )
        await self.db.update_token(
            second,
            at=None,
            at_expires=None,
            browser_profile_status="ready",
            refresh_status="ready",
            last_st_refresh_at=second_anchor,
            last_st_refresh_result="success",
            browser_profile_next_at=second_anchor + timedelta(minutes=120),
            next_refresh_at=second_anchor + timedelta(minutes=120),
        )
        await self.db.update_profile_maintenance_config(True, 60)
        first_saved, second_saved = await asyncio.gather(
            self.db.get_token(self.account_id), self.db.get_token(second)
        )
        self.assertEqual(first_saved.next_refresh_at, self.anchor + timedelta(minutes=60))
        self.assertEqual(second_saved.next_refresh_at, second_anchor + timedelta(minutes=60))
        restarted = Database(self.db.db_path)
        await restarted.check_and_migrate_db()
        self.assertEqual(
            (await restarted.get_profile_maintenance_config())["refresh_interval_minutes"], 60
        )
        self.assertEqual(TokenManager(restarted, object())._profile_interval(), 60)

    async def test_late_admission_commit_uses_current_policy_instead_of_stale_deadline(self):
        original = await self.db.get_token(self.account_id)
        await self.db.update_profile_maintenance_config(False, 15)
        now = datetime.now(timezone.utc)
        self.assertTrue(
            await self.db.update_token_if_credentials_match(
                self.account_id,
                {"st": original.st, "email": original.email},
                refresh_status="ready",
                browser_profile_status="ready",
                last_st_refresh_at=now,
                last_st_refresh_result="success",
                browser_profile_verified_at=now,
                next_refresh_at=now + timedelta(minutes=120),
                browser_profile_next_at=now + timedelta(minutes=120),
            )
        )
        saved = await self.db.get_token(self.account_id)
        self.assertIsNone(saved.next_refresh_at)
        self.assertIsNone(saved.browser_profile_next_at)
        await self.db.update_profile_maintenance_config(True, 15)
        saved = await self.db.get_token(self.account_id)
        self.assertEqual(saved.next_refresh_at, now + timedelta(minutes=15))
