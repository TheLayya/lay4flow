"""Orphan browser caches are reclaimed only under proven idle ownership."""

import asyncio
import json
import os
import subprocess
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.services.browser.profiles import (
    PROFILE_CACHE_PATHS,
    ProfileManager,
    _browser_profile_processes,
    _profile_argument,
    _ProfileFileLock,
    _prune_closed_profile_cache,
    _remember_chromium_owner,
)


class NoBrowserBackend:
    async def open(self, *args, **kwargs):
        raise AssertionError("Cache recovery must never open a browser")

    async def close(self, *args, **kwargs):
        raise AssertionError("Cache recovery must never close a foreign browser")


class ProfileCacheRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manager = ProfileManager(self.root, NoBrowserBackend())
        self.spec = await self.profile("synthetic@example.invalid")
        self.inventory = self.enterContext(
            patch("src.services.browser.profiles._browser_profile_processes", return_value=set())
        )

    async def profile(self, key):
        spec = await self.manager.spec(key)
        (spec.profile_dir / "Default").mkdir()
        for name in ("environment_snapshot.json", "launch_settings.sha256"):
            (spec.profile_dir / name).write_text("synthetic", encoding="utf-8")
        return spec

    def cache(self, name="Default/Cache", spec=None):
        path = (spec or self.spec).profile_dir / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "entry").write_bytes(b"regenerable-cache")
        return path

    async def test_abnormal_profile_cache_recovery_preserves_all_site_state_and_status(self):
        retained = (
            "Default/Network/Cookies",
            "Default/IndexedDB/state",
            "Default/Local Storage/state",
            "Default/Session Storage/state",
            "Default/Service Worker/CacheStorage/state",
            "Default/Service Worker/ScriptCache/state",
            "Default/Preferences",
            "Local State",
        )
        for name in retained:
            path = self.spec.profile_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"persisted-login-state")
        metadata = self.spec.profile_dir / "metadata.json"
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value.update(state="error", last_error="abnormal-close")
        metadata.write_text(json.dumps(value), encoding="utf-8")
        before = metadata.read_bytes()
        for name in PROFILE_CACHE_PATHS:
            self.cache(name)
        result = await self.manager.prune_idle_caches()
        self.assertEqual(
            result,
            {
                "scanned": 1,
                "cleaned": 1,
                "skipped": 0,
                "bytes_reclaimed": len(PROFILE_CACHE_PATHS) * len(b"regenerable-cache"),
            },
        )
        self.assertFalse(
            any((self.spec.profile_dir / name).exists() for name in PROFILE_CACHE_PATHS)
        )
        self.assertEqual(metadata.read_bytes(), before)
        for name in retained:
            self.assertEqual((self.spec.profile_dir / name).read_bytes(), b"persisted-login-state")
        self.assertEqual((await self.manager.prune_idle_caches())["cleaned"], 0)

    async def test_manager_account_and_foreign_file_locks_skip_without_waiting(self):
        cache = self.cache()
        lock = await self.manager._account_lock(self.spec.account_key)
        await lock.acquire()
        try:
            self.assertEqual((await self.manager.prune_idle_caches())["skipped"], 1)
        finally:
            lock.release()
        file_lock = _ProfileFileLock(self.spec.profile_dir / ".profile.lock", timeout=0)
        await file_lock.acquire()
        try:
            self.assertEqual((await self.manager.prune_idle_caches())["skipped"], 1)
        finally:
            file_lock.release()
        self.assertTrue(cache.exists())

    async def test_warm_opening_borrowed_and_failed_close_profiles_are_never_pruned(self):
        cache = self.cache()
        key = self.spec.account_key
        for name in ("_warm_handles", "_warm_opening", "_warm_sessions", "_failed_handles"):
            with self.subTest(name=name):
                collection = getattr(self.manager, name)
                if isinstance(collection, dict):
                    collection[key] = object()
                else:
                    collection.add(key)
                try:
                    self.assertEqual((await self.manager.prune_idle_caches())["skipped"], 1)
                    self.assertTrue(cache.exists())
                finally:
                    if isinstance(collection, dict):
                        collection.pop(key)
                    else:
                        collection.remove(key)

    async def test_external_browser_or_unreadable_process_inventory_skips(self):
        cache = self.cache()
        for inventory in (None, {os.path.normcase(str(self.spec.profile_dir.resolve()))}):
            with self.subTest(inventory=inventory):
                self.inventory.return_value = inventory
                self.assertEqual((await self.manager.prune_idle_caches())["skipped"], 1)
                self.assertTrue(cache.exists())

    async def test_second_process_snapshot_catches_a_browser_that_appeared_during_scan(self):
        cache = self.cache()
        self.inventory.side_effect = [
            set(),
            {os.path.normcase(str(self.spec.profile_dir.resolve()))},
        ]
        self.assertEqual((await self.manager.prune_idle_caches())["skipped"], 1)
        self.assertTrue(cache.exists())

    async def test_uncertain_singleton_markers_are_preserved(self):
        cache = self.cache()
        marker = self.spec.profile_dir / "SingletonLock"
        marker.write_bytes(b"foreign-or-uncertain-owner")
        self.assertEqual((await self.manager.prune_idle_caches())["skipped"], 1)
        self.assertTrue(cache.exists())
        self.assertEqual(marker.read_bytes(), b"foreign-or-uncertain-owner")

    async def test_proven_dead_owned_linux_singletons_allow_orphan_cache_cleanup(self):
        cache = self.cache()
        targets = {
            "SingletonLock": "owned-host-987654321",
            "SingletonCookie": "123456789",
            "SingletonSocket": str(self.root / "dead-browser" / "SingletonSocket"),
        }
        try:
            for name, target in targets.items():
                (self.spec.profile_dir / name).symlink_to(target)
        except OSError:
            self.skipTest("Symbolic links require privileges on this platform")
        with (
            patch("src.services.browser.profiles.sys.platform", "linux"),
            patch("src.services.browser.profiles.socket.gethostname", return_value="owned-host"),
            patch(
                "src.services.browser.profiles._linux_boot_id",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch("src.services.browser.profiles._linux_pid_namespace", return_value="pid:[123]"),
        ):
            _remember_chromium_owner(self.spec)
            with patch("src.services.browser.profiles.os.kill", side_effect=ProcessLookupError):
                self.assertEqual((await self.manager.prune_idle_caches())["cleaned"], 1)
        self.assertFalse(cache.exists())
        self.assertEqual(len(list((self.spec.profile_dir / ".singleton-recovery").iterdir())), 1)

    async def test_invalid_profile_identity_seed_or_required_sidecar_is_skipped(self):
        cache = self.cache()
        metadata = self.spec.profile_dir / "metadata.json"
        original = metadata.read_text(encoding="utf-8")
        for field, value in (
            ("account_key", "foreign"),
            ("fingerprint_seed", 12345),
            ("profile_dir", str(self.root)),
        ):
            with self.subTest(field=field):
                changed = json.loads(original)
                changed[field] = value if changed.get(field) != value else 54321
                metadata.write_text(json.dumps(changed), encoding="utf-8")
                self.assertEqual((await self.manager.prune_idle_caches())["scanned"], 0)
                self.assertTrue(cache.exists())
        metadata.write_text(original, encoding="utf-8")
        (self.spec.profile_dir / "launch_settings.sha256").unlink()
        self.assertEqual((await self.manager.prune_idle_caches())["scanned"], 0)

    async def test_linked_profile_default_cache_and_lock_are_not_followed(self):
        with TemporaryDirectory() as outside:
            protected = Path(outside)
            (protected / "private-state").write_bytes(b"keep")
            for relative in ("Default/Cache", ".profile.lock"):
                with self.subTest(relative=relative):
                    link = self.spec.profile_dir / relative
                    try:
                        if relative == ".profile.lock":
                            link.unlink(missing_ok=True)
                            link.symlink_to(protected / "private-state")
                        else:
                            link.symlink_to(protected, target_is_directory=True)
                    except OSError:
                        self.skipTest("Symbolic links require privileges on this platform")
                    try:
                        with (
                            self.assertLogs("src.services.browser.profiles", level="WARNING")
                            if relative != ".profile.lock"
                            else nullcontext()
                        ):
                            result = await self.manager.prune_idle_caches()
                        self.assertEqual(result["cleaned"], 0)
                        self.assertEqual((protected / "private-state").read_bytes(), b"keep")
                    finally:
                        link.unlink()
            # Root ancestry itself is checked, including junction-style redirection.
            with patch("src.services.browser.profiles._plain_directory", return_value=False):
                self.assertEqual((await self.manager.prune_idle_caches())["scanned"], 0)

    async def test_linked_profile_and_default_directory_do_not_reach_outside_state(self):
        with TemporaryDirectory() as outside:
            protected = Path(outside)
            (protected / "state").write_bytes(b"keep")
            default = self.spec.profile_dir / "Default"
            default.rmdir()
            try:
                default.symlink_to(protected, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                # Directory junctions exercise the Windows reparse-point path.
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(default), str(protected)],
                    check=True,
                    capture_output=True,
                )
            try:
                self.assertEqual((await self.manager.prune_idle_caches())["scanned"], 0)
                self.assertEqual((protected / "state").read_bytes(), b"keep")
            finally:
                default.unlink() if default.is_symlink() else default.rmdir()
            link = self.root / ("other-profile-" + "a" * 12)
            try:
                link.symlink_to(protected, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(protected)],
                    check=True,
                    capture_output=True,
                )
            try:
                self.assertEqual((await self.manager.prune_idle_caches())["scanned"], 0)
                self.assertEqual((protected / "state").read_bytes(), b"keep")
            finally:
                link.unlink() if link.is_symlink() else link.rmdir()

    async def test_hardlinked_identity_sidecar_is_skipped(self):
        cache = self.cache()
        metadata = self.spec.profile_dir / "metadata.json"
        backup = self.root / "metadata-copy"
        os.link(metadata, backup)
        self.assertEqual((await self.manager.prune_idle_caches())["scanned"], 0)
        self.assertTrue(cache.exists())

    async def test_cancelled_worker_keeps_account_and_file_locks_until_cleanup_finishes(self):
        cache = self.cache()
        started, release = threading.Event(), threading.Event()

        def delayed(profile, root):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return _prune_closed_profile_cache(profile, root)

        with patch(
            "src.services.browser.profiles._prune_closed_profile_cache", side_effect=delayed
        ):
            task = asyncio.create_task(self.manager.prune_idle_caches())
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            lock = await self.manager._account_lock(self.spec.account_key)
            self.assertTrue(lock.locked())
            contender = _ProfileFileLock(self.spec.profile_dir / ".profile.lock", timeout=0)
            with self.assertRaises(TimeoutError):
                await contender.acquire()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(cache.exists())
        self.assertFalse(lock.locked())
        await contender.acquire()
        contender.release()

    async def test_duplicate_sweep_is_nonblocking(self):
        await self.manager._cache_recovery_lock.acquire()
        try:
            self.assertEqual((await self.manager.prune_idle_caches())["scanned"], 0)
        finally:
            self.manager._cache_recovery_lock.release()


class BrowserProcessInventoryTests(unittest.TestCase):
    def test_profile_arguments_accept_both_forms_and_reject_uncertain_paths(self):
        path = str(Path("synthetic-profile").resolve())
        expected = os.path.normcase(path)
        self.assertEqual(_profile_argument(["chrome", "--user-data-dir=" + path]), expected)
        self.assertEqual(_profile_argument(["chrome", "--user-data-dir", path]), expected)
        self.assertIsNone(_profile_argument(["chrome", "--type=renderer"]))
        for args in (
            ["--user-data-dir"],
            ["--user-data-dir="],
            ["--user-data-dir=relative"],
            ["--user-data-dir=" + path, "--user-data-dir=" + path],
        ):
            with self.assertRaises(ValueError):
                _profile_argument(args)

    def test_linux_inventory_reads_nul_arguments_without_printing_them(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "123"
            process.mkdir()
            (process / "comm").write_text("custom-browser", encoding="utf-8")
            profile = str(root / "synthetic profile")
            (process / "cmdline").write_bytes(
                b"/opt/chrome\0--user-data-dir\0" + profile.encode() + b"\0--private=secret\0"
            )
            original_path = Path
            with (
                patch("src.services.browser.profiles.sys.platform", "linux"),
                patch(
                    "src.services.browser.profiles.Path",
                    side_effect=lambda value: root if value == "/proc" else original_path(value),
                ),
            ):
                self.assertEqual(
                    _browser_profile_processes(), {os.path.normcase(str(Path(profile).resolve()))}
                )
                (process / "cmdline").write_bytes(b"/opt/chrome\0--user-data-dir=relative\0")
                self.assertIsNone(_browser_profile_processes())

    @unittest.skipUnless(os.name == "nt", "Windows process command-line parser")
    def test_windows_inventory_parses_quoted_paths_and_is_hidden_and_fail_closed(self):
        profile = str(Path("synthetic profile").resolve())
        result = SimpleNamespace(
            stdout=json.dumps(
                [
                    {
                        "Name": "chrome.exe",
                        "CommandLine": '"C:\\Browser\\chrome.exe" --user-data-dir="'
                        + profile
                        + '"',
                    }
                ]
            )
        )
        with patch("src.services.browser.profiles.subprocess.run", return_value=result) as run:
            self.assertEqual(_browser_profile_processes(), {os.path.normcase(profile)})
            self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        with patch(
            "src.services.browser.profiles.subprocess.run",
            return_value=SimpleNamespace(stdout='[{"CommandLine":null}]'),
        ):
            self.assertIsNone(_browser_profile_processes())
        with patch(
            "src.services.browser.profiles.subprocess.run",
            side_effect=subprocess.TimeoutExpired("powershell", 15),
        ):
            self.assertIsNone(_browser_profile_processes())


if __name__ == "__main__":
    unittest.main()
