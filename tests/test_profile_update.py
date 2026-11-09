"""Synthetic existing-login replacement preserves identity and rolls back failures."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.browser.cloak_backend import CloakBrowserBackend
from src.services.browser.profile_update import update_profile_login
from src.services.browser.profiles import ProfileManager

EMAIL = "existing@example.invalid"
PROXY = "http://fixed.invalid:8080"


class Backend(CloakBrowserBackend):
    def __init__(self):
        super().__init__()
        self.opened = []
        self.closed = []
        self.fail_close = False

    async def open_background(self, profile_dir, *, proxy_url=None):
        self._check_launch_settings(profile_dir, self._launch_options(profile_dir, proxy_url))
        context = SimpleNamespace(profile_dir=profile_dir)
        self.opened.append((profile_dir, proxy_url))
        return context

    async def close(self, context):
        if self.fail_close:
            raise RuntimeError("synthetic close error")
        self.closed.append(context)


def snapshot(path):
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file() and item.name != ".profile.lock"
    }


class ProfileLoginUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = Backend()
        self.manager = ProfileManager(Path(directory.name) / "profiles", self.backend)
        self.spec = await self.manager.spec(EMAIL)
        (self.spec.profile_dir / "Default").mkdir()
        (self.spec.profile_dir / "Default" / "Cookies").write_text("original-cookie")
        (self.spec.profile_dir / "Default" / "Local Storage").mkdir()
        (self.spec.profile_dir / "Default" / "Local Storage" / "keep").write_text("keep-site-state")
        (self.spec.profile_dir / "environment_snapshot.json").write_text("synthetic-environment")
        self.backend._check_launch_settings(
            self.spec.profile_dir, self.backend._launch_options(self.spec.profile_dir, PROXY)
        )
        self.verifier = SimpleNamespace(
            verify_environment=AsyncMock(return_value="environment_stable")
        )
        self.before = snapshot(self.spec.profile_dir)

        async def validate(manager, context):
            self.assertEqual(snapshot(self.spec.profile_dir), self.before)
            (context.profile_dir / "Default" / "Cookies").write_text("new-cookie")
            return {"credits": 41, "google_cookies": "synthetic-new-cookie"}

        self.validate = AsyncMock(side_effect=validate)
        self.commit = AsyncMock()

    async def update(self, proxy=PROXY):
        return await update_profile_login(
            self.manager, EMAIL, proxy, self.verifier, self.validate, self.commit
        )

    async def test_success_opens_once_closes_before_commit_and_retains_identity(self):
        async def commit(values):
            self.assertEqual(len(self.backend.closed), 1)
            self.assertEqual(
                (self.spec.profile_dir / "Default" / "Cookies").read_text(), "new-cookie"
            )
            self.assertEqual(values["credits"], 41)

        self.commit.side_effect = commit
        result = await self.update()
        self.assertEqual(result["credits"], 41)
        self.assertEqual([proxy for _, proxy in self.backend.opened], [PROXY])
        self.validate.assert_awaited_once()
        self.commit.assert_awaited_once_with(result)
        metadata = json.loads((self.spec.profile_dir / "metadata.json").read_text())
        self.assertEqual(metadata["fingerprint_seed"], self.spec.fingerprint_seed)
        self.assertEqual(metadata["profile_dir"], str(self.spec.profile_dir))
        self.assertEqual(metadata["state"], "ready")
        self.assertTrue(self.backend._session_restore_enabled(self.spec.profile_dir))
        self.backend._check_launch_settings(
            self.spec.profile_dir, self.backend._launch_options(self.spec.profile_dir, PROXY)
        )
        self.assertEqual(
            (self.spec.profile_dir / "environment_snapshot.json").read_bytes(),
            self.before["environment_snapshot.json"],
        )
        self.assertEqual(
            (self.spec.profile_dir / "Default" / "Local Storage" / "keep").read_text(),
            "keep-site-state",
        )
        self.assertFalse(list(self.manager.root_dir.glob(".login-update-*")))

    async def test_validation_failure_does_not_touch_original_or_retry(self):
        self.validate.side_effect = ValueError("synthetic identity mismatch")
        with self.assertRaises(ValueError):
            await self.update()
        self.validate.assert_awaited_once()
        self.commit.assert_not_awaited()
        self.assertEqual(len(self.backend.opened), 1)
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)
        self.assertFalse(list(self.manager.root_dir.glob(".login-update-*")))

    async def test_commit_error_or_false_restores_every_original_byte(self):
        for failure in (RuntimeError("synthetic CAS failure"), False):
            with self.subTest(failure=type(failure).__name__):
                self.commit.side_effect = failure if isinstance(failure, Exception) else None
                self.commit.return_value = failure
                with self.assertRaises((RuntimeError, ValueError)):
                    await self.update()
                self.assertEqual(snapshot(self.spec.profile_dir), self.before)
                self.assertFalse(list(self.manager.root_dir.glob(".login-update-*")))

    async def test_busy_viewer_rejects_without_open_or_mutation(self):
        lock = await self.manager._account_lock(self.spec.account_key)
        async with lock:
            with self.assertRaises(ValueError):
                await self.update()
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)

    async def test_changed_proxy_or_launch_is_rejected_before_browser(self):
        with self.assertRaises(ValueError):
            await self.update("http://changed.invalid:8080")
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)

    async def test_failed_stage_close_retains_stage_and_blocks_another_browser(self):
        self.backend.fail_close = True
        with self.assertRaises(RuntimeError):
            await self.update()
        self.commit.assert_not_awaited()
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)
        self.assertEqual(len(self.manager._proxy_switch_failed_stages), 1)
        self.assertTrue(list(self.manager.root_dir.glob(".login-update-*")))
        with self.assertRaises(ValueError):
            await self.update()
        self.assertEqual(len(self.backend.opened), 1)
        for failed in self.manager._proxy_switch_failed_stages:
            for handle in failed._failed_handles.values():
                handle.file_lock.release()

    async def test_failed_file_rollback_retains_stage_and_blocks_original(self):
        original_replace = Path.replace

        def replace(source, target):
            if source.parent.name == "original" and source.name == "launch_settings.sha256":
                raise OSError("synthetic rollback failure")
            return original_replace(source, target)

        self.commit.side_effect = ValueError("synthetic CAS failure")
        with patch.object(Path, "replace", replace), self.assertRaises(OSError):
            await self.update()
        self.assertIn(self.spec.account_key, self.manager._failed_handles)
        self.assertTrue(list(self.manager.root_dir.glob(".login-update-*")))
        with self.assertRaises(ValueError):
            await self.update()

    async def test_copy_cancellation_waits_for_worker_before_cleanup(self):
        from src.services.browser.proxy_switch import _copy_profile

        entered, release = threading.Event(), threading.Event()

        def copying(source, target):
            entered.set()
            release.wait(5)
            _copy_profile(source, target)

        with patch("src.services.browser.profile_update._copy_profile", side_effect=copying):
            task = asyncio.create_task(self.update())
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)
        self.assertEqual(self.backend.opened, [])
        self.assertFalse(list(self.manager.root_dir.glob(".login-update-*")))

    async def test_commit_cancellation_finishes_success_without_files_db_split(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def commit(values):
            entered.set()
            await release.wait()

        self.commit.side_effect = commit
        task = asyncio.create_task(self.update())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        result = await task
        self.assertEqual(result["credits"], 41)
        self.assertEqual((self.spec.profile_dir / "Default" / "Cookies").read_text(), "new-cookie")
        self.assertFalse(list(self.manager.root_dir.glob(".login-update-*")))

    async def test_commit_cancellation_with_rejection_rolls_back_after_result(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def commit(values):
            entered.set()
            await release.wait()
            return False

        self.commit.side_effect = commit
        task = asyncio.create_task(self.update())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(ValueError):
            await task
        self.assertEqual(snapshot(self.spec.profile_dir), self.before)
        self.assertFalse(list(self.manager.root_dir.glob(".login-update-*")))

    async def test_symlink_is_rejected_without_following_its_data(self):
        outside = self.manager.root_dir / "outside.txt"
        outside.write_text("private-unrelated-data")
        link = self.spec.profile_dir / "untrusted-link"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink privilege is unavailable")
        try:
            with self.assertRaises(ValueError):
                await self.update()
            self.assertEqual(outside.read_text(), "private-unrelated-data")
            self.assertEqual(self.backend.opened, [])
        finally:
            link.unlink()


if __name__ == "__main__":
    unittest.main()
