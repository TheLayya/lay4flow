"""A new exit is admitted only after staged identity verification and cold reopen."""

import asyncio
import json
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.database import Database
from src.core.models import Token
from src.services.browser.cloak_backend import CloakBrowserBackend
from src.services.browser.profiles import ProfileManager
from src.services.proxy_manager import ProxyManager
from src.services.token_manager import TokenManager

OLD = "http://old.invalid:8080"
NEW = "http://new.invalid:8080"
EMAIL = "account@example.invalid"


class Backend(CloakBrowserBackend):
    def __init__(self):
        super().__init__()
        self.opened = []
        self.fail_close = False

    async def open_background(self, profile_dir, *, proxy_url=None):
        self._check_launch_settings(profile_dir, self._launch_options(profile_dir, proxy_url))
        context = SimpleNamespace(profile_dir=profile_dir)
        self.opened.append((profile_dir, proxy_url))
        return context

    async def close(self, context):
        if self.fail_close:
            raise RuntimeError("synthetic close error")


def snapshot(path):
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in path.rglob("*")
        if p.is_file() and p.name != ".profile.lock"
    }


class ProfileProxySwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = Backend()
        self.manager = ProfileManager(Path(directory.name) / "profiles", self.backend)
        self.spec = await self.manager.spec(EMAIL)
        (self.spec.profile_dir / "Default").mkdir()
        (self.spec.profile_dir / "Default" / "Cookies").write_text("original-cookie")
        (self.spec.profile_dir / "environment_snapshot.json").write_text("synthetic-environment")
        self.backend._check_launch_settings(
            self.spec.profile_dir, self.backend._launch_options(self.spec.profile_dir, OLD)
        )
        self.verifier = SimpleNamespace(
            verify_environment=AsyncMock(return_value="environment_stable")
        )
        self.before = snapshot(self.spec.profile_dir)
        self.validate = AsyncMock(return_value={"st": "renewed"})
        self.commit = AsyncMock()

    async def switch(self):
        return await self.manager.switch_proxy(
            EMAIL, OLD, NEW, self.verifier, self.validate, self.commit
        )

    async def test_success_preserves_storage_seed_environment_and_cold_opens_twice(self):
        async def verify(staged_manager, context):
            self.assertEqual(snapshot(self.spec.profile_dir), self.before)
            expected = "original-cookie" if self.validate.await_count == 1 else "renewed-cookie"
            self.assertEqual((context.profile_dir / "Default" / "Cookies").read_text(), expected)
            (context.profile_dir / "Default" / "Cookies").write_text("renewed-cookie")
            return {"st": "renewed"}

        self.validate.side_effect = verify
        await self.switch()
        self.assertEqual([proxy for _, proxy in self.backend.opened], [NEW, NEW])
        self.commit.assert_awaited_once_with({"st": "renewed"})
        self.assertEqual(
            (self.spec.profile_dir / "Default" / "Cookies").read_text(), "renewed-cookie"
        )
        self.assertEqual(
            (self.spec.profile_dir / "environment_snapshot.json").read_bytes(),
            self.before["environment_snapshot.json"],
        )
        metadata = json.loads((self.spec.profile_dir / "metadata.json").read_text())
        self.assertEqual(metadata["fingerprint_seed"], self.spec.fingerprint_seed)
        self.assertEqual(metadata["profile_dir"], str(self.spec.profile_dir))
        self.backend._check_launch_settings(
            self.spec.profile_dir, self.backend._launch_options(self.spec.profile_dir, NEW)
        )
        with self.assertRaises(ValueError):
            self.backend._check_launch_settings(
                self.spec.profile_dir, self.backend._launch_options(self.spec.profile_dir, OLD)
            )

    async def test_validation_cold_reopen_and_cas_failures_restore_every_original_byte(self):
        for failure in ("first_probe", "cold_reopen", "commit"):
            with self.subTest(failure=failure):
                self.validate.reset_mock()
                self.commit.reset_mock()
                self.validate.side_effect = (
                    [RuntimeError("probe failed")]
                    if failure == "first_probe"
                    else [{"st": "renewed"}, RuntimeError("cold failed")]
                    if failure == "cold_reopen"
                    else None
                )
                self.commit.side_effect = (
                    RuntimeError("CAS rejected") if failure == "commit" else None
                )
                with self.assertRaises(RuntimeError):
                    await self.switch()
                self.assertEqual(snapshot(self.spec.profile_dir), self.before)
                self.assertFalse(list(self.manager.root_dir.glob(".proxy-switch-*")))

    async def test_viewer_lock_rejects_without_copy_or_open(self):
        lock = await self.manager._account_lock(self.spec.account_key)
        async with lock:
            with self.assertRaises(ValueError):
                await self.switch()
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)

    async def test_old_launch_hash_mismatch_is_never_accepted(self):
        with self.assertRaises(ValueError):
            await self.manager.switch_proxy(
                EMAIL, "http://wrong.invalid:8080", NEW, self.verifier, self.validate, self.commit
            )
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)

    async def test_failed_stage_close_keeps_original_and_retains_live_stage(self):
        self.backend.fail_close = True
        with self.assertRaises(RuntimeError):
            await self.switch()
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)
        self.commit.assert_not_awaited()
        self.assertEqual(len(self.manager._proxy_switch_failed_stages), 1)
        staged_manager = self.manager._proxy_switch_failed_stages[0]
        self.assertTrue(staged_manager._failed_handles)
        with self.assertRaises(ValueError):
            await self.switch()
        for handle in staged_manager._failed_handles.values():
            handle.file_lock.release()

    async def test_filesystem_rollback_failure_retains_recovery_files_and_blocks_profile(self):
        original_replace = Path.replace

        def replace(source, target):
            if source.parent.name == "original" and source.name == "launch_settings.sha256":
                raise OSError("synthetic restore failure")
            return original_replace(source, target)

        self.commit.side_effect = ValueError("synthetic CAS rejection")
        with patch.object(Path, "replace", replace):
            with self.assertRaises(OSError):
                await self.switch()
        self.assertIn(self.spec.account_key, self.manager._failed_handles)
        self.assertTrue(list(self.manager.root_dir.glob(".proxy-switch-*")))
        with self.assertRaises(ValueError):
            await self.switch()
        with self.assertRaises(Exception):
            async with self.manager.session(EMAIL, proxy_url=OLD):
                self.fail("partially restored Profile launched")

    async def test_cancelled_copy_waits_for_worker_then_retains_original(self):
        from src.services.browser.proxy_switch import _copy_profile

        entered, release = threading.Event(), threading.Event()

        def copy(source, target):
            entered.set()
            release.wait(5)
            _copy_profile(source, target)

        with patch("src.services.browser.proxy_switch._copy_profile", side_effect=copy):
            task = asyncio.create_task(self.switch())
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)
        self.assertFalse(list(self.manager.root_dir.glob(".proxy-switch-*")))

    async def test_cancelled_commit_finishes_before_releasing_binding_transaction(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def commit(values):
            started.set()
            await release.wait()

        self.commit.side_effect = commit
        task = asyncio.create_task(self.switch())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        # Once DB commit has started, complete it as one unit with the new Profile.
        await task
        self.backend._check_launch_settings(
            self.spec.profile_dir, self.backend._launch_options(self.spec.profile_dir, NEW)
        )
        self.assertFalse(list(self.manager.root_dir.glob(".proxy-switch-*")))


class TokenProxySwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "tokens.db"))
        await self.db.init_db()
        proxy = ProxyManager(self.db)
        self.proxy_id = await proxy.add_proxy_pool_entry(NEW)
        self.flow = SimpleNamespace(proxy_manager=proxy)
        self.manager = TokenManager(self.db, self.flow)
        self.token_id = await self.db.add_token(
            Token(
                st="old-st",
                at="old-at",
                email=EMAIL,
                proxy_url=OLD,
                browser_profile_source="cloak",
                is_active=False,
            )
        )
        await self.db.update_token(self.token_id, ban_reason="manual")
        self.before = await self.db.get_token(self.token_id)
        self.profile = SimpleNamespace(
            switch_proxy=AsyncMock(), _wait_uninterrupted=ProfileManager._wait_uninterrupted
        )
        self.manager.profile_refresher = SimpleNamespace(manager=self.profile, verifier=object())

        @asynccontextmanager
        async def maintenance(token, proxy):
            self.assertEqual(token.id, self.token_id)
            self.assertEqual(proxy, NEW)
            yield

        self.manager.account_proxy_maintenance = maintenance

    async def test_success_commits_validated_credentials_and_preserves_manual_disable(self):
        values = {
            "st": "new-st",
            "at": "new-at",
            "at_expires": datetime.now(timezone.utc) + timedelta(hours=5),
            "credits": 17,
        }

        async def switch(email, old, new, verifier, validate, commit):
            await commit(values)

        self.profile.switch_proxy.side_effect = switch
        await self.manager.update_token(self.token_id, proxy_pool_id=self.proxy_id)
        saved = await self.db.get_token(self.token_id)
        self.assertEqual(
            (saved.proxy_pool_id, saved.proxy_url, saved.st, saved.at),
            (self.proxy_id, NEW, "new-st", "new-at"),
        )
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")
        self.assertEqual(saved.refresh_status, "ready")

    async def test_changed_snapshot_rejects_commit_without_replacing_binding(self):
        async def switch(email, old, new, verifier, validate, commit):
            await self.db.update_token(self.token_id, remark="changed concurrently")
            await commit(
                {
                    "st": "new-st",
                    "at": "new-at",
                    "at_expires": datetime.now(timezone.utc) + timedelta(hours=5),
                }
            )

        self.profile.switch_proxy.side_effect = switch
        with self.assertRaises(ValueError):
            await self.manager.update_token(self.token_id, proxy_pool_id=self.proxy_id)
        saved = await self.db.get_token(self.token_id)
        self.assertEqual((saved.proxy_url, saved.st), (OLD, "old-st"))

    async def test_target_proxy_disabled_during_validation_never_commits(self):
        async def switch(email, old, new, verifier, validate, commit):
            await self.db.set_proxy_pool_enabled(self.proxy_id, False)
            await commit(
                {
                    "st": "new-st",
                    "at": "new-at",
                    "at_expires": datetime.now(timezone.utc) + timedelta(hours=5),
                }
            )

        self.profile.switch_proxy.side_effect = switch
        with self.assertRaises(ValueError):
            await self.manager.update_token(self.token_id, proxy_pool_id=self.proxy_id)
        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved, self.before)

    async def test_target_pool_change_between_precheck_and_cas_is_atomic(self):
        original = self.db.update_token_if_credentials_match

        async def change_pool(token_id, expected, **kwargs):
            await self.db.update_proxy_pool_entry(
                self.proxy_id, proxy_url="http://different.invalid:8080"
            )
            return await original(token_id, expected, **kwargs)

        async def switch(email, old, new, verifier, validate, commit):
            await commit(
                {
                    "st": "new-st",
                    "at": "new-at",
                    "at_expires": datetime.now(timezone.utc) + timedelta(hours=5),
                }
            )

        self.profile.switch_proxy.side_effect = switch
        with patch.object(self.db, "update_token_if_credentials_match", side_effect=change_pool):
            with self.assertRaises(ValueError):
                await self.manager.update_token(self.token_id, proxy_pool_id=self.proxy_id)
        self.assertEqual(await self.db.get_token(self.token_id), self.before)

    async def test_mixed_credentials_or_unwired_maintenance_never_switches(self):
        with self.assertRaises(ValueError):
            await self.manager.update_token(
                self.token_id, proxy_pool_id=self.proxy_id, google_cookies="different"
            )
        self.profile.switch_proxy.assert_not_awaited()
        self.manager.account_proxy_maintenance = None
        with self.assertRaises(ValueError):
            await self.manager.update_token(self.token_id, proxy_pool_id=self.proxy_id)
        self.assertEqual(await self.db.get_token(self.token_id), self.before)


if __name__ == "__main__":
    unittest.main()
