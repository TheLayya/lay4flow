"""Modern Flow Profile validation never invents an AT or replays retired Labs auth."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.database import Database
from src.core.models import Token
from src.services.browser.profiles import ProfileNotReadyError
from src.services.protocol_login import protocol_loginer
from src.services.token_manager import RefreshFailure, TokenManager


class FlowProfileCredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "synthetic.db"))
        await self.db.init_db()
        self.flow = SimpleNamespace(
            st_to_at=AsyncMock(side_effect=AssertionError("retired session endpoint called")),
            get_credits=AsyncMock(side_effect=AssertionError("legacy bearer request called")),
        )
        self.refresher = SimpleNamespace(refresh=AsyncMock(return_value=self.result()))
        self.manager = TokenManager(self.db, self.flow, self.refresher)
        self.login = AsyncMock(side_effect=AssertionError("retired protocol login called"))
        login_patch = patch.object(protocol_loginer, "login", self.login)
        login_patch.start()
        self.addCleanup(login_patch.stop)

    @staticmethod
    def result(credits=37):
        return {
            "_validated_session": {
                "at": None,
                "at_expires": None,
                "credits": credits,
                "user_paygate_tier": "PAYGATE_TIER_ONE",
            },
            "google_cookies": '{"SID":"synthetic-updated-cookie"}',
        }

    async def add(self, **updates):
        values = {
            "email": "account@example.invalid",
            "st": "profile-placeholder-" + updates.get("email", "account@example.invalid"),
            "browser_profile_source": "cloak",
            "browser_profile_epoch": "synthetic-profile-epoch",
            "protocol_mode": "protocol",
            "google_cookies": '{"SID":"synthetic-old-cookie"}',
            "refresh_interval_minutes": 120,
        }
        values.update(updates)
        if values.get("last_st_refresh_result") == "success" and values.get("last_st_refresh_at"):
            values.setdefault(
                "browser_profile_next_at",
                values["last_st_refresh_at"]
                + timedelta(minutes=values["refresh_interval_minutes"]),
            )
        token_id = await self.db.add_token(Token(**values))
        persisted_only = {
            key: values[key]
            for key in (
                "browser_profile_status",
                "browser_profile_verified_at",
                "browser_profile_next_at",
                "browser_profile_epoch",
                "refresh_status",
                "next_refresh_at",
                "last_refresh_at",
            )
            if key in values
        }
        if persisted_only:
            await self.db.update_token(token_id, **persisted_only)
        return token_id

    def assert_no_legacy_requests(self):
        self.flow.st_to_at.assert_not_awaited()
        self.flow.get_credits.assert_not_awaited()
        self.login.assert_not_awaited()

    async def test_refresh_saves_verified_browser_session_without_at_or_st_rotation(self):
        token_id = await self.add(at="obsolete-at", at_expires=datetime.now(timezone.utc))
        callback = Mock()
        self.manager.profile_ready_callback = callback
        before = datetime.now(timezone.utc)

        self.assertTrue(await self.manager._refresh_at(token_id, force=True, refresh_st=True))

        saved = await self.db.get_token(token_id)
        self.assertIsNone(saved.at)
        self.assertIsNone(saved.at_expires)
        self.assertTrue(saved.st.startswith("profile-placeholder-"))
        self.assertEqual(saved.google_cookies, self.result()["google_cookies"])
        self.assertEqual((saved.credits, saved.user_paygate_tier), (37, "PAYGATE_TIER_ONE"))
        self.assertTrue(saved.is_active)
        self.assertIsNone(saved.ban_reason)
        self.assertEqual((saved.refresh_status, saved.browser_profile_status), ("ready", "ready"))
        self.assertGreaterEqual(saved.browser_profile_verified_at, before)
        self.assertGreaterEqual(saved.next_refresh_at, before + timedelta(minutes=120))
        self.assertEqual(saved.next_refresh_at, saved.browser_profile_next_at)
        self.assertGreaterEqual(saved.last_st_refresh_at, before)
        self.assertEqual(saved.last_st_refresh_result, "success")
        self.assertTrue(self.manager.is_profile_validation_recent(saved))
        callback.assert_called_once_with(token_id)
        self.assert_no_legacy_requests()

    async def test_fresh_profile_survives_restart_and_reuses_stale_caller_snapshot(self):
        token_id = await self.add()
        old_snapshot = await self.db.get_token(token_id)
        self.assertTrue(await self.manager._refresh_at(token_id, force=True))
        restarted = TokenManager(self.db, self.flow, self.refresher)

        token = await restarted.ensure_valid_token(old_snapshot)
        self.assertIsNotNone(token)
        self.assertIsNone(token.at)
        self.assertFalse(restarted.needs_at_refresh(token))
        self.assertTrue(await restarted.is_at_valid(token_id, old_snapshot))
        self.assertEqual(self.refresher.refresh.await_count, 2)
        self.assert_no_legacy_requests()

    async def test_stale_ready_profile_is_verified_once_then_reused(self):
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            browser_profile_verified_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        self.assertIsNotNone(
            await self.manager.ensure_valid_token(await self.db.get_token(token_id))
        )
        self.assertIsNotNone(
            await self.manager.ensure_valid_token(await self.db.get_token(token_id))
        )
        self.assertEqual(self.refresher.refresh.await_count, 1)
        self.assert_no_legacy_requests()

    async def test_explicit_credits_refresh_uses_profile_despite_future_schedule(self):
        now = datetime.now(timezone.utc)
        token_id = await self.add(
            credits=19,
            browser_profile_status="ready",
            browser_profile_verified_at=now,
            refresh_status="ready",
            next_refresh_at=now + timedelta(hours=2),
        )
        self.refresher.refresh.return_value = self.result(credits=0)

        self.assertEqual(await self.manager.refresh_credits(token_id), 0)

        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.credits, 0)
        self.assertEqual(saved.refresh_status, "ready")
        self.refresher.refresh.assert_awaited_once()
        self.assertEqual(self.refresher.refresh.call_args.kwargs, {"force": True})
        self.assert_no_legacy_requests()

    async def test_scheduler_skips_fresh_profiles_without_at_and_refreshes_only_due_profile(self):
        now = datetime.now(timezone.utc)
        for index in range(3):
            await self.add(
                email=f"account{index}@example.invalid",
                st=f"scheduler-{index}",
                browser_profile_status="ready",
                browser_profile_verified_at=now,
                refresh_status="ready",
            )
        stale_id = await self.add(
            st="scheduler-stale",
            browser_profile_status="ready",
            browser_profile_verified_at=now - timedelta(hours=3),
            refresh_status="ready",
        )
        await self.manager.run_protocol_refresh_once()
        await self.manager.run_protocol_refresh_once()
        self.refresher.refresh.assert_awaited_once()
        self.assertEqual(self.refresher.refresh.call_args.args[0].id, stale_id)
        self.assert_no_legacy_requests()

    async def test_network_failure_preserves_active_profile_and_uses_backoff(self):
        verified = datetime.now(timezone.utc) - timedelta(hours=3)
        token_id = await self.add(
            browser_profile_status="ready",
            browser_profile_verified_at=verified,
            refresh_status="ready",
        )
        self.refresher.refresh.side_effect = TimeoutError("synthetic network failure")

        self.assertFalse(await self.manager._refresh_at(token_id, force=True))

        saved = await self.db.get_token(token_id)
        self.assertTrue(saved.is_active)
        self.assertIsNone(saved.ban_reason)
        self.assertEqual(saved.refresh_status, "retrying")
        self.assertEqual(saved.browser_profile_status, "ready")
        self.assertEqual(saved.browser_profile_verified_at, verified)
        self.assertIsNotNone(saved.next_refresh_at)
        self.assertEqual(saved.browser_profile_next_at, saved.next_refresh_at)
        await self.manager.run_protocol_refresh_once()
        self.assertEqual(self.refresher.refresh.await_count, 1)
        self.assert_no_legacy_requests()

    async def test_signed_out_profile_is_disabled_and_stops_automatic_retries(self):
        token_id = await self.add(browser_profile_status="ready")
        self.refresher.refresh.side_effect = ProfileNotReadyError(
            "account@example.invalid", "login_required"
        )
        self.assertFalse(await self.manager._refresh_at(token_id, force=True))
        saved = await self.db.get_token(token_id)
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "refresh_failed")
        self.assertEqual(saved.browser_profile_status, "login_required")
        self.assertIsNone(saved.next_refresh_at)
        self.assertIsNone(await self.manager.ensure_valid_token(saved))
        await self.manager.run_protocol_refresh_once()
        self.assertEqual(self.refresher.refresh.await_count, 1)
        self.assert_no_legacy_requests()

    async def test_failed_attempt_does_not_extend_legacy_profile_verification_freshness(self):
        verified = datetime.now(timezone.utc) - timedelta(hours=3)
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            last_refresh_at=verified,
        )
        self.refresher.refresh.side_effect = TimeoutError("synthetic network failure")

        self.assertFalse(await self.manager._refresh_at(token_id, force=True))

        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.browser_profile_verified_at, verified)
        self.assertTrue(self.manager.needs_at_refresh(saved))
        self.assertIsNone(await self.manager.ensure_valid_token(saved))
        self.assertEqual(self.refresher.refresh.await_count, 1)
        self.assert_no_legacy_requests()

    async def test_missing_or_malformed_profile_result_never_falls_back_to_legacy_auth(self):
        for index, result in enumerate(
            ({"session_token": "old-contract"}, self.result(True), self.result(-1))
        ):
            with self.subTest(result=result):
                self.refresher.refresh.return_value = result
                token_id = await self.add(
                    email=f"malformed{index}@example.invalid",
                    st=f"malformed-st-{index}",
                )
                self.assertFalse(await self.manager._refresh_at(token_id, force=True))
                saved = await self.db.get_token(token_id)
                self.assertEqual(saved.refresh_status, "retrying")
                self.assertIsNone(saved.at)
                self.assertTrue(saved.st.startswith("malformed-st-"))
        self.assert_no_legacy_requests()

    async def test_changed_identity_profile_epoch_or_source_discards_inflight_result(self):
        for index, change in enumerate(
            (
                {"email": "other@example.invalid"},
                {"browser_profile_epoch": "replacement"},
                {"browser_profile_source": "cookies"},
            )
        ):
            with self.subTest(change=change):
                token_id = await self.add(
                    email=f"cas{index}@example.invalid",
                    st=f"cas-st-{index}",
                )

                async def changed(token, **kwargs):
                    await self.db.update_token(token.id, **change)
                    return self.result(91)

                self.refresher.refresh.side_effect = changed
                self.assertFalse(await self.manager._refresh_at(token_id, force=True))
                saved = await self.db.get_token(token_id)
                self.assertEqual(saved.credits, 0)
                self.assertEqual(saved.st, f"cas-st-{index}")
        self.assert_no_legacy_requests()

    async def test_concurrent_profile_refreshes_share_one_verification(self):
        token_id = await self.add()
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(token, **kwargs):
            entered.set()
            await release.wait()
            return self.result()

        self.refresher.refresh.side_effect = refresh
        first = asyncio.create_task(self.manager._refresh_at(token_id, force=True))
        await entered.wait()
        second = asyncio.create_task(self.manager._refresh_at(token_id, force=True))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [True, True])
        self.refresher.refresh.assert_awaited_once()
        self.assert_no_legacy_requests()

    async def test_cloak_rejects_direct_legacy_helpers(self):
        token = Token(
            st="placeholder", email="account@example.invalid", browser_profile_source="cloak"
        )
        for operation in (
            self.manager._st_to_at_for_token(token, token.st),
            self.manager._validate_refresh_session(token, token.st, session={}),
            self.manager._try_protocol_refresh_st(1, token),
        ):
            with self.assertRaises(RefreshFailure):
                await operation
        self.assert_no_legacy_requests()

    async def test_cookie_account_keeps_existing_bearer_validation_path(self):
        now = datetime.now(timezone.utc)
        token_id = await self.add(
            browser_profile_source="cookies", at="synthetic-at", at_expires=now + timedelta(hours=8)
        )
        self.flow.get_credits.side_effect = None
        self.flow.get_credits.return_value = {"credits": 12, "userPaygateTier": "cookie-tier"}
        self.manager.resolve_account_proxy = AsyncMock(return_value=None)

        valid = await self.manager.ensure_valid_token(await self.db.get_token(token_id))

        self.assertEqual((valid.at, valid.credits), ("synthetic-at", 12))
        self.flow.get_credits.assert_awaited_once_with("synthetic-at")
        self.refresher.refresh.assert_not_awaited()
        self.flow.st_to_at.assert_not_awaited()
        self.login.assert_not_awaited()

    async def test_profile_cache_covers_full_credentials_and_proxy_binding(self):
        token_id = await self.add()
        token = await self.db.get_token(token_id)
        self.manager.mark_profile_validated(token)
        self.assertTrue(self.manager.is_profile_validation_recent(token))
        for field, value in (
            ("email", "other@example.invalid"),
            ("login_account", "other@example.invalid"),
            ("st", "new-placeholder"),
            ("google_cookies", "changed-cookies"),
            ("at", "old-at"),
            ("at_expires", datetime.now(timezone.utc)),
            ("proxy_pool_id", 9),
            ("proxy_url", "http://changed.invalid:8080"),
            ("browser_profile_source", "cookies"),
            ("browser_profile_epoch", "new-epoch"),
        ):
            with self.subTest(field=field):
                self.manager.mark_profile_validated(token)
                self.assertFalse(
                    self.manager.is_profile_validation_recent(
                        token.model_copy(update={field: value})
                    )
                )
        self.manager.mark_profile_validated(token)
        with patch("src.services.token_manager.time.monotonic", return_value=10**15):
            self.assertFalse(self.manager.is_profile_validation_recent(token))

    async def test_validation_cache_expiry_probes_once_without_sliding_refresh_cycle(self):
        now = datetime.now(timezone.utc)
        last_refresh = now - timedelta(minutes=20)
        next_refresh = now + timedelta(minutes=100)
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            browser_profile_verified_at=last_refresh,
            last_st_refresh_at=last_refresh,
            last_st_refresh_result="success",
            next_refresh_at=next_refresh,
        )
        token = await self.db.get_token(token_id)
        self.assertFalse(self.manager.needs_at_refresh(token))
        self.assertFalse(self.manager.is_profile_validation_recent(token))
        self.assertIsNotNone(await self.manager.ensure_valid_token(token))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.last_st_refresh_at, last_refresh)
        self.assertEqual(saved.next_refresh_at, next_refresh)
        self.assertTrue(self.manager.is_profile_validation_recent(saved))
        self.assertIsNotNone(await self.manager.ensure_valid_token(saved))
        self.refresher.refresh.assert_awaited_once()
        self.assert_no_legacy_requests()

    async def test_validation_failure_keeps_backoff_and_last_success(self):
        now = datetime.now(timezone.utc)
        last_success = now - timedelta(minutes=20)
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            browser_profile_verified_at=last_success,
            last_st_refresh_at=last_success,
            last_st_refresh_result="success",
            next_refresh_at=now + timedelta(minutes=100),
        )
        self.refresher.refresh.side_effect = TimeoutError("synthetic")
        token = await self.db.get_token(token_id)
        self.assertIsNone(await self.manager.ensure_valid_token(token))
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.last_st_refresh_at, last_success)
        self.assertEqual(saved.last_st_refresh_result, "success")
        self.assertIsNone(await self.manager.ensure_valid_token(saved))
        self.refresher.refresh.assert_awaited_once()
        self.assert_no_legacy_requests()

    async def test_late_cookie_update_after_success_cas_does_not_gain_validation_cache(self):
        token_id = await self.add()
        original = self.db.update_token_if_credentials_match

        async def changed_after_commit(identifier, expected, **updates):
            saved = await original(identifier, expected, **updates)
            if saved and updates.get("browser_profile_status") == "ready":
                await self.db.update_token(identifier, google_cookies="new-unverified-cookie")
            return saved

        with patch.object(
            self.db, "update_token_if_credentials_match", side_effect=changed_after_commit
        ):
            self.assertTrue(await self.manager._refresh_at(token_id, force=True))
        latest = await self.db.get_token(token_id)
        self.assertFalse(self.manager.is_profile_validation_recent(latest))

    async def test_manual_refresh_promotes_running_validation_and_records_success_time(self):
        now = datetime.now(timezone.utc)
        last_success = now - timedelta(minutes=20)
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            browser_profile_verified_at=last_success,
            last_st_refresh_at=last_success,
            last_st_refresh_result="success",
            next_refresh_at=now + timedelta(minutes=100),
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(token, **kwargs):
            if not kwargs:
                entered.set()
                await release.wait()
            return self.result()

        self.refresher.refresh.side_effect = refresh
        validation = asyncio.create_task(
            self.manager.ensure_valid_token(await self.db.get_token(token_id))
        )
        await entered.wait()
        manual = asyncio.create_task(self.manager._refresh_at(token_id, force=True))
        await asyncio.sleep(0)
        release.set()
        valid, saved = await asyncio.gather(validation, manual)
        self.assertIsNotNone(valid)
        self.assertTrue(saved)
        self.assertEqual(self.refresher.refresh.await_count, 2)
        self.assertGreater((await self.db.get_token(token_id)).last_st_refresh_at, last_success)

    async def test_recent_modern_profile_wins_over_legacy_st_success_timestamp(self):
        now = datetime.now(timezone.utc)
        legacy_st = now - timedelta(days=1)
        next_refresh = now + timedelta(hours=2)
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            browser_profile_verified_at=now - timedelta(minutes=1),
            last_refresh_at=now - timedelta(minutes=1),
            last_st_refresh_at=legacy_st,
            last_st_refresh_result="success",
            browser_profile_next_at=next_refresh,
            next_refresh_at=next_refresh,
        )
        token = await self.db.get_token(token_id)
        self.assertFalse(self.manager.needs_at_refresh(token))
        self.assertIsNotNone(await self.manager.ensure_valid_token(token))
        saved = await self.db.get_token(token_id)
        self.assertGreater(saved.last_st_refresh_at, legacy_st)
        self.assertEqual(saved.last_st_refresh_result, "success")
        self.refresher.refresh.assert_awaited_once()
        self.assert_no_legacy_requests()

    async def test_stale_profile_with_future_schedule_can_validate_without_waiting(self):
        now = datetime.now(timezone.utc)
        stale = now - timedelta(days=1)
        next_refresh = now + timedelta(hours=2)
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            browser_profile_verified_at=stale,
            last_refresh_at=stale,
            last_st_refresh_at=stale,
            last_st_refresh_result="success",
            next_refresh_at=next_refresh,
        )
        self.assertIsNotNone(
            await self.manager.ensure_valid_token(await self.db.get_token(token_id))
        )
        saved = await self.db.get_token(token_id)
        self.assertGreater(saved.last_st_refresh_at, stale)
        self.assertGreater(saved.next_refresh_at, now)
        self.assertTrue(self.manager.is_profile_validation_recent(saved))
        self.refresher.refresh.assert_awaited_once()
        self.assert_no_legacy_requests()

    async def test_repeated_validation_probes_do_not_postpone_cookie_renewal_cycle(self):
        now = datetime.now(timezone.utc)
        anchor = now - timedelta(minutes=119)
        token_id = await self.add(
            browser_profile_status="ready",
            refresh_status="ready",
            browser_profile_verified_at=anchor,
            last_st_refresh_at=anchor,
            last_st_refresh_result="success",
            next_refresh_at=anchor + timedelta(minutes=120),
        )
        self.assertIsNotNone(
            await self.manager.ensure_valid_token(await self.db.get_token(token_id))
        )
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.last_st_refresh_at, anchor)
        self.manager._clear_at_validation_cache(token_id)
        with patch("src.services.token_manager.datetime") as clock:
            clock.now.return_value = now + timedelta(minutes=2)
            self.assertIsNotNone(await self.manager.ensure_valid_token(saved))
        renewed = await self.db.get_token(token_id)
        self.assertGreater(renewed.last_st_refresh_at, anchor)
        self.assertEqual(renewed.last_st_refresh_result, "success")
        self.assertEqual(self.refresher.refresh.await_count, 2)
        self.assert_no_legacy_requests()


if __name__ == "__main__":
    unittest.main()
