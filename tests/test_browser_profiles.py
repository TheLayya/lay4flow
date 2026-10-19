import asyncio
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.services.browser.cloak_backend import CloakBrowserBackend
from src.services.browser.profile_verifier import AccountVerification
from src.services.browser.profiles import (
    PROFILE_CACHE_PATHS,
    ProfileManager,
    ProfileNotReadyError,
    _ProfileFileLock,
    _prune_closed_profile_cache,
)


class FakeBackend:
    def __init__(self):
        self.opened = []
        self.closed = []

    async def open(self, profile_dir: Path):
        context = object()
        self.opened.append((profile_dir, context))
        return context

    async def close(self, context):
        self.closed.append(context)


class ProxyBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.proxies = []

    async def open(self, profile_dir: Path, *, proxy_url=None):
        self.proxies.append(proxy_url)
        return await super().open(profile_dir)


class CookieBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.cookies = {}

    async def open(self, profile_dir: Path):
        context = type("Context", (), {})()
        context.request = type("Request", (), {"get": lambda *args, **kwargs: None})()
        context.add_cookies = lambda rows: self._add(profile_dir, rows)
        context.cookies = lambda: self._get(profile_dir)
        self.opened.append((profile_dir, context))
        return context

    async def _add(self, profile_dir, rows):
        self.cookies[str(profile_dir)] = list(rows)

    async def _get(self, profile_dir):
        return list(self.cookies.get(str(profile_dir), []))


class FakeVerifier:
    def __init__(self, ready=True, reason="workspace_visible"):
        self.ready = ready
        self.reason = reason
        self.calls = []

    async def verify_environment(self, context, profile_dir):
        return "environment_stable"

    async def verify_ready(self, context, profile_dir):
        self.calls.append((context, profile_dir))
        return type("Result", (), {"workspace_visible": self.ready, "reason": self.reason})()

    async def verify_account(self, expected_email, account_probe):
        value = await account_probe.verify(expected_email)
        if value["email"].casefold() != expected_email.casefold():
            return AccountVerification(False, False, value["credits"], "identity_mismatch")
        return AccountVerification(True, True, value["credits"], "account_ready")


class FakeAccountProbe:
    def __init__(self, ready=True):
        self.ready = ready

    def set_context(self, context):
        self.context = context

    async def verify(self, expected_email):
        return {
            "email": expected_email if self.ready else "other@example.com",
            "session_valid": self.ready,
            "credits": 7,
        }


class ProfileManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_prunes_only_regenerable_cache_and_reopens_original_profile(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend)
            spec = await manager.spec("same@example.invalid")
            retained = (
                "Default/Cookies",
                "Default/Network/Cookies",
                "Default/Local Storage/leveldb/state",
                "Default/IndexedDB/state",
                "Default/Session Storage/state",
                "Default/Service Worker/CacheStorage/state",
                "Default/Service Worker/ScriptCache/state",
                "Default/WebStorage/state",
                "Default/Preferences",
                "Default/Secure Preferences",
                "Local State",
                "environment_snapshot.json",
                "launch_settings.sha256",
                "CustomCache/state",
            )
            for name in retained:
                path = spec.profile_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"persistent-account-state")
            for name in PROFILE_CACHE_PATHS:
                path = spec.profile_dir / name / "cache-entry"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"regenerable")
            async with manager.session("same@example.invalid"):
                self.assertTrue(all((spec.profile_dir / p).exists() for p in PROFILE_CACHE_PATHS))
            self.assertFalse(any((spec.profile_dir / p).exists() for p in PROFILE_CACHE_PATHS))
            self.assertTrue(backend.closed)
            async with manager.session("same@example.invalid"):
                self.assertEqual(backend.opened[-1][0], spec.profile_dir)
                for name in retained:
                    self.assertEqual(
                        (spec.profile_dir / name).read_bytes(), b"persistent-account-state"
                    )
            self.assertEqual(
                (await manager.spec("same@example.invalid")).fingerprint_seed, spec.fingerprint_seed
            )

    async def test_warm_cache_is_pruned_only_on_final_close_and_interactive_uses_same_path(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend(), warm_slots=1)
            spec = await manager.spec("same")
            cache = spec.profile_dir / "BrowserMetrics"
            cache.mkdir()
            (cache / "metrics.pma").write_bytes(b"metrics")
            async with manager.warm_session("same"):
                pass
            self.assertTrue(cache.exists())
            await manager.close_warm()
            self.assertFalse(cache.exists())
            async with manager.session("same", interactive=True):
                cache.mkdir()
                (cache / "metrics.pma").write_bytes(b"metrics")
            self.assertFalse(cache.exists())

    async def test_failed_browser_close_does_not_prune_cache(self):
        class FailingBackend(FakeBackend):
            async def close(self, context):
                raise RuntimeError("synthetic close failure")

        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FailingBackend())
            spec = await manager.spec("same")
            cache = spec.profile_dir / "BrowserMetrics"
            cache.mkdir()
            (cache / "metrics.pma").write_bytes(b"metrics")
            try:
                with self.assertRaises(RuntimeError):
                    async with manager.session("same"):
                        pass
                self.assertTrue(cache.exists())
                self.assertEqual(manager._capacity._value, 0)
            finally:
                for handle in manager._failed_handles.values():
                    handle.file_lock.release()

    async def test_cancelled_cleanup_holds_file_lock_until_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()

        def slow_cleanup(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("cleanup test timed out")

        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend())
            spec = await manager.spec("same")

            async def close_session():
                async with manager.session("same"):
                    pass

            with patch("src.services.browser.profiles._prune_closed_profile_cache", slow_cleanup):
                task = asyncio.create_task(close_session())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertEqual(manager._capacity._value, 0)
                    other = _ProfileFileLock(spec.profile_dir / ".profile.lock", timeout=0.02)
                    with self.assertRaises(TimeoutError):
                        await other.acquire()
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertEqual(manager._capacity._value, 1)
            other = _ProfileFileLock(spec.profile_dir / ".profile.lock", timeout=0.02)
            await other.acquire()
            other.release()

    async def test_cleanup_io_failure_is_logged_without_credentials_and_releases_capacity(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend())
            spec = await manager.spec("private-account@example.invalid")
            cache = spec.profile_dir / "BrowserMetrics"
            cache.mkdir()
            with patch(
                "src.services.browser.profiles.shutil.rmtree", side_effect=PermissionError("SECRET")
            ):
                with self.assertLogs("src.services.browser.profiles", level="WARNING") as logs:
                    async with manager.session("private-account@example.invalid"):
                        pass
            self.assertTrue(cache.exists())
            self.assertEqual(manager._capacity._value, 1)
            self.assertEqual(
                (await manager.status("private-account@example.invalid")).state, "stopped"
            )
            self.assertNotIn("private-account", " ".join(logs.output))
            self.assertNotIn("SECRET", " ".join(logs.output))

    async def test_shutdown_cancelling_close_task_waits_for_cache_worker(self):
        entered, release = threading.Event(), threading.Event()

        def slow_cleanup(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("cleanup test timed out")

        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend(), warm_slots=1)
            spec = await manager.spec("same")
            async with manager.warm_session("same"):
                pass
            handle = manager._warm_handles[spec.account_key]
            with patch("src.services.browser.profiles._prune_closed_profile_cache", slow_cleanup):
                task = asyncio.create_task(manager.drop_warm("same"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    handle.close_task.cancel()
                    await asyncio.sleep(0)
                    self.assertEqual(manager._capacity._value, 0)
                    other = _ProfileFileLock(spec.profile_dir / ".profile.lock", timeout=0.02)
                    with self.assertRaises(TimeoutError):
                        await other.acquire()
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertEqual(manager._capacity._value, 1)
            other = _ProfileFileLock(spec.profile_dir / ".profile.lock", timeout=0.02)
            await other.acquire()
            other.release()

    async def test_cleanup_rejects_profile_default_and_target_links(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside:
            base = Path(root)
            protected = Path(outside)
            (protected / "state").write_bytes(b"keep")
            for kind in ("profile", "Default", "target"):
                with self.subTest(kind=kind):
                    profile = base / kind
                    try:
                        if kind == "profile":
                            profile.symlink_to(protected, target_is_directory=True)
                        else:
                            profile.mkdir()
                            if kind == "Default":
                                (profile / "Default").symlink_to(
                                    protected, target_is_directory=True
                                )
                            else:
                                (profile / "BrowserMetrics").symlink_to(
                                    protected, target_is_directory=True
                                )
                    except OSError:
                        if os.name != "nt":
                            raise
                        # Directory junctions need no Windows symlink privilege.
                        import subprocess

                        link = (
                            profile
                            if kind == "profile"
                            else profile / ("Default" if kind == "Default" else "BrowserMetrics")
                        )
                        subprocess.run(
                            ["cmd", "/c", "mklink", "/J", str(link), str(protected)],
                            check=True,
                            capture_output=True,
                        )
                    with self.assertLogs("src.services.browser.profiles", level="WARNING"):
                        _prune_closed_profile_cache(profile, base)
                    self.assertEqual((protected / "state").read_bytes(), b"keep")
                    link = (
                        profile
                        if kind == "profile"
                        else profile / ("Default" if kind == "Default" else "BrowserMetrics")
                    )
                    if link.is_symlink():
                        link.unlink()
                    else:
                        link.rmdir()

    async def test_cleanup_does_not_follow_links_inside_cache(self):
        with TemporaryDirectory() as root:
            profile = Path(root) / "same"
            cache = profile / "Default/Cache"
            cache.mkdir(parents=True)
            state = profile / "Default/Service Worker/CacheStorage"
            state.mkdir(parents=True)
            (state / "state").write_bytes(b"keep")
            link = cache / "linked-state"
            try:
                link.symlink_to(state, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                import subprocess

                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(state)],
                    check=True,
                    capture_output=True,
                )
            _prune_closed_profile_cache(profile, Path(root))
            self.assertFalse(cache.exists())
            self.assertEqual((state / "state").read_bytes(), b"keep")

    async def test_account_paths_are_isolated_and_closed(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend, max_concurrent=2)
            async with manager.session("user@example.com") as first:
                async with manager.session("other@example.com") as second:
                    self.assertIsNot(first, second)
            self.assertEqual(len({path.name for path, _ in backend.opened}), 2)
            self.assertEqual(len(backend.closed), 2)

    async def test_fingerprint_seeds_are_stable_and_distinct(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend(), max_concurrent=2)
            first = await manager.spec("user@example.com")
            second = await manager.spec("other@example.com")
            reopened = await manager.spec("user@example.com")
            self.assertEqual(first.fingerprint_seed, reopened.fingerprint_seed)
            self.assertNotEqual(first.fingerprint_seed, second.fingerprint_seed)

    async def test_concurrent_new_accounts_do_not_share_seed(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend(), max_concurrent=4)
            specs = await asyncio.gather(*(manager.spec(f"user-{index}") for index in range(20)))
            self.assertEqual(len({spec.fingerprint_seed for spec in specs}), len(specs))

    async def test_seed_collision_is_rejected(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend())
            first = await manager.spec("first")
            second = await manager.spec("second")
            metadata = Path(second.profile_dir) / "metadata.json"
            values = json.loads(metadata.read_text(encoding="utf-8"))
            values["fingerprint_seed"] = first.fingerprint_seed
            metadata.write_text(json.dumps(values), encoding="utf-8")
            with self.assertRaises(ValueError):
                await manager.spec("second")

    async def test_seed_survives_new_manager_and_directory_move(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(Path(root) / "original", FakeBackend())
            first = await manager.spec("user")
            await manager.mark_verified("user")
            manager.root_dir.rename(Path(root) / "moved")
            reopened = await ProfileManager(Path(root) / "moved", FakeBackend()).spec("user")
            self.assertEqual(first.fingerprint_seed, reopened.fingerprint_seed)

    async def test_existing_browser_profile_requires_migration(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend())
            profile = Path(root) / manager._normalize_account_key("user")
            (profile / "Default").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "migration"):
                await manager.spec("user")

    def test_launch_settings_are_fixed_without_persisting_proxy_password(self):
        with TemporaryDirectory() as root:
            profile = Path(root)
            options = {
                "proxy": "http://user:synthetic-password@proxy.invalid:8080",
                "locale": "zh-CN",
            }
            CloakBrowserBackend._check_launch_settings(profile, options)
            CloakBrowserBackend._check_launch_settings(profile, dict(options))
            saved = (profile / "launch_settings.sha256").read_text(encoding="utf-8")
            self.assertNotIn("synthetic-password", saved)
            with self.assertRaisesRegex(ValueError, "settings changed"):
                CloakBrowserBackend._check_launch_settings(profile, {**options, "locale": "en-US"})

    async def test_status_is_persisted_without_credentials(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend)
            async with manager.session("user@example.com"):
                status = await manager.status("user@example.com")
                self.assertEqual(status.state, "starting")
                self.assertIsNotNone(status.last_started_at)
                await manager.mark_verified("user@example.com")
            status = await manager.status("user@example.com")
            self.assertEqual(status.state, "stopped")
            self.assertIsNotNone(status.last_verified_at)
            self.assertNotIn("value", (Path(status.profile_dir) / "metadata.json").read_text())

    async def test_profile_key_hash_prevents_sanitized_name_collision(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend())
            first = await manager.spec("a/b")
            second = await manager.spec("a:b")
            self.assertNotEqual(first.profile_dir, second.profile_dir)

    async def test_open_failure_is_recorded_as_safe_summary(self):
        class FailingBackend(FakeBackend):
            async def open(self, profile_dir):
                raise RuntimeError("secret-cookie-value")

        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FailingBackend())
            with self.assertRaisesRegex(RuntimeError, "secret-cookie-value"):
                async with manager.session("user"):
                    pass
            status = await manager.status("user")
            self.assertEqual(status.state, "error")
            self.assertNotIn("secret-cookie-value", status.last_error)

    async def test_same_account_is_serialized(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend, max_concurrent=2)
            active = 0
            peak = 0

            async def worker():
                nonlocal active, peak
                async with manager.session("same"):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0)
                    active -= 1

            await asyncio.gather(worker(), worker())
            self.assertEqual(peak, 1)

    async def test_interactive_viewer_does_not_consume_background_capacity(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend, max_concurrent=1)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def viewer():
                async with manager.session("viewer", interactive=True):
                    entered.set()
                    await release.wait()

            async def refresh():
                await entered.wait()
                async with manager.session("refresh"):
                    return True

            view_task = asyncio.create_task(viewer())
            self.assertTrue(await asyncio.wait_for(refresh(), 1))
            release.set()
            await view_task

    async def test_opt_in_warm_session_reuses_imported_profile_without_reopening(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(
                root, backend, max_concurrent=1, warm_slots=1, warm_idle_seconds=60
            )
            imported = await manager.spec("same@example.com")
            (imported.profile_dir / "Default").mkdir()
            (imported.profile_dir / "Default" / "extension-cookie-state").write_text("synthetic")
            async with manager.warm_session("same@example.com") as first:
                pass
            async with manager.warm_session("same@example.com") as second:
                self.assertIs(first, second)
            self.assertEqual(len(backend.opened), 1)
            self.assertEqual(backend.opened[0][0], imported.profile_dir)
            self.assertTrue((imported.profile_dir / "Default" / "extension-cookie-state").is_file())
            self.assertEqual(len(list(Path(root).glob("*/metadata.json"))), 1)
            self.assertEqual(len(backend.closed), 0)
            await manager.close_warm()
            self.assertEqual(len(backend.closed), 1)

    async def test_warm_and_cold_accounts_evict_idle_browser_without_waiting_for_ttl(self):
        for next_session in ("warm_session", "session"):
            with self.subTest(next_session=next_session), TemporaryDirectory() as root:
                backend = FakeBackend()
                manager = ProfileManager(root, backend, warm_slots=1, warm_idle_seconds=60)
                async with manager.warm_session("first") as first:
                    pass

                async def refresh():
                    async with getattr(manager, next_session)("second") as second:
                        self.assertIsNot(first, second)
                        self.assertIn(first, backend.closed)

                await asyncio.wait_for(refresh(), 1)
                self.assertEqual(len(manager._warm_handles), int(next_session == "warm_session"))
                await manager.close_warm()
                self.assertEqual(manager._capacity._value, 1)

    async def test_warm_pool_reserves_slots_during_concurrent_launch_and_serializes_accounts(self):
        class CountingBackend(FakeBackend):
            peak = 0
            active = 0

            async def open(self, profile_dir):
                await asyncio.sleep(0)
                context = await super().open(profile_dir)
                self.active += 1
                self.peak = max(self.peak, self.active)
                return context

            async def close(self, context):
                await super().close(context)
                self.active -= 1

        with TemporaryDirectory() as root:
            backend = CountingBackend()
            manager = ProfileManager(root, backend, max_concurrent=2, warm_slots=1)
            active_accounts = set()

            async def refresh(key):
                async with manager.warm_session(key) as context:
                    self.assertNotIn(key, active_accounts)
                    active_accounts.add(key)
                    self.assertNotIn(context, backend.closed)
                    self.assertLessEqual(len(manager._warm_handles), 1)
                    await asyncio.sleep(0.01)
                    active_accounts.remove(key)

            await asyncio.wait_for(
                asyncio.gather(*(refresh(key) for key in ("first", "second", "first", "third"))),
                2,
            )
            self.assertLessEqual(backend.peak, 2)
            self.assertLessEqual(len(manager._warm_handles), 1)
            await manager.close_warm()
            self.assertEqual(backend.active, 0)

    async def test_warm_failure_and_cancellation_close_profile_and_release_capacity(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend, warm_slots=1)
            for failure in (RuntimeError("synthetic failure"), asyncio.CancelledError()):
                with self.subTest(failure=type(failure)), self.assertRaises(type(failure)):
                    async with manager.warm_session("first"):
                        raise failure
                self.assertFalse(manager._warm_handles)
                self.assertEqual(manager._capacity._value, 1)
            self.assertEqual(len(backend.closed), 2)

    async def test_warm_bootstrap_and_viewer_close_heat_before_reopening_same_directory(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend, warm_slots=1)
            spec = await manager.spec("first")
            for interactive in (False, True):
                async with manager.warm_session("first") as warm:
                    pass
                async with manager.session("first", interactive=interactive) as current:
                    self.assertIsNot(current, warm)
                    self.assertIn(warm, backend.closed)
                    self.assertFalse(manager._warm_handles)
                    self.assertEqual(backend.opened[-1][0], spec.profile_dir)
            self.assertEqual(manager._capacity._value, 1)

    async def test_warm_expiry_waits_for_borrower_and_does_not_close_replacement(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            manager = ProfileManager(root, backend, warm_slots=1, warm_idle_seconds=0.03)
            try:
                async with manager.warm_session("first") as first:
                    pass
                async with manager.warm_session("first") as reused:
                    self.assertIs(first, reused)
                    await asyncio.sleep(0.05)
                    self.assertNotIn(first, backend.closed)
                async with manager.warm_session("second") as second:
                    await asyncio.sleep(0.05)
                    self.assertNotIn(second, backend.closed)
                expiry = next(iter(manager._warm_handles.values())).expiry_task
                await asyncio.wait_for(asyncio.shield(expiry), 2)
                self.assertIn(second, backend.closed)
                self.assertFalse(manager._warm_handles)
                self.assertEqual(manager._capacity._value, 1)
            finally:
                await manager.close_warm()

    async def test_shutdown_waits_for_inflight_launch_and_prevents_late_heat(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowBackend(FakeBackend):
            async def open(self, profile_dir):
                entered.set()
                await release.wait()
                return await super().open(profile_dir)

        with TemporaryDirectory() as root:
            backend = SlowBackend()
            manager = ProfileManager(root, backend, warm_slots=1)

            async def refresh():
                async with manager.warm_session("first"):
                    pass

            refreshing = asyncio.create_task(refresh())
            await entered.wait()
            shutdown = asyncio.create_task(manager.close_warm())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(refreshing, shutdown), 1)
            self.assertEqual(len(backend.closed), 1)
            self.assertFalse(manager._warm_handles)
            self.assertFalse(manager._warm_opening)
            with self.assertRaises(ProfileNotReadyError):
                async with manager.warm_session("second"):
                    pass

    async def test_cancel_during_launch_waits_for_close_and_holds_profile_lock(self):
        entered, release, closing, closed = (asyncio.Event() for _ in range(4))

        class SlowBackend(FakeBackend):
            async def open(self, profile_dir):
                entered.set()
                await release.wait()
                return await super().open(profile_dir)

            async def close(self, context):
                closing.set()
                await closed.wait()
                await super().close(context)

        with TemporaryDirectory() as root:
            backend = SlowBackend()
            manager = ProfileManager(root, backend, warm_slots=1)

            async def refresh():
                async with manager.warm_session("first"):
                    self.fail("cancelled launch was yielded")

            refreshing = asyncio.create_task(refresh())
            await entered.wait()
            refreshing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(refreshing.done())
            release.set()
            await closing.wait()
            self.assertEqual(manager._capacity._value, 0)
            self.assertTrue(
                (await manager._account_lock(manager._normalize_account_key("first"))).locked()
            )
            refreshing.cancel()
            closed.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(refreshing, 1)
            self.assertEqual(manager._capacity._value, 1)
            self.assertFalse(manager._warm_handles)

    async def test_warm_proxy_change_does_not_borrow_wrong_environment(self):
        with TemporaryDirectory() as root:
            backend = ProxyBackend()
            manager = ProfileManager(root, backend, warm_slots=1)
            async with manager.warm_session("first", proxy_url="http://one.invalid"):
                pass
            with self.assertRaises(ProfileNotReadyError):
                async with manager.warm_session("first", proxy_url="http://two.invalid"):
                    self.fail("borrowed a browser with the wrong proxy")
            self.assertEqual(len(backend.opened), 1)
            await manager.close_warm()

    async def test_close_failure_retains_browser_capacity_and_file_lock(self):
        class FailingBackend(FakeBackend):
            async def close(self, context):
                raise RuntimeError("synthetic close failure")

        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FailingBackend(), warm_slots=1)
            async with manager.warm_session("first"):
                pass
            with self.assertRaises(RuntimeError):
                await manager.drop_warm("first")
            self.assertEqual(manager._capacity._value, 0)
            with self.assertRaises(ProfileNotReadyError):
                async with manager.warm_session("first"):
                    self.fail("reused a browser whose close failed")
            # The synthetic backend has no process; clean its retained handle
            # explicitly so Windows can remove the temporary test directory.
            handle = next(iter(manager._failed_handles.values()))
            handle.file_lock.release()

    async def test_shutdown_closes_healthy_heat_even_if_another_close_fails(self):
        class FailingBackend(FakeBackend):
            async def close(self, context):
                if context is self.opened[0][1]:
                    raise RuntimeError("synthetic close failure")
                await super().close(context)

        with TemporaryDirectory() as root:
            backend = FailingBackend()
            manager = ProfileManager(root, backend, max_concurrent=2, warm_slots=2)
            async with manager.warm_session("first"):
                pass
            async with manager.warm_session("second") as second:
                pass
            try:
                with self.assertRaises(RuntimeError):
                    await manager.close_warm()
                self.assertIn(second, backend.closed)
                self.assertEqual(manager._capacity._value, 1)
            finally:
                for handle in manager._failed_handles.values():
                    handle.file_lock.release()

    async def test_same_account_is_serialized_across_managers(self):
        with TemporaryDirectory() as root:
            first = ProfileManager(root, FakeBackend(), max_concurrent=2)
            second = ProfileManager(root, FakeBackend(), max_concurrent=2)
            active = 0
            peak = 0

            async def worker(manager):
                nonlocal active, peak
                async with manager.session("same"):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.15)
                    active -= 1

            await asyncio.gather(worker(first), worker(second))
            self.assertEqual(peak, 1)

            lock_file = Path(root) / first._normalize_account_key("same") / ".profile.lock"
            self.assertEqual(lock_file.read_bytes(), b"\0" if os.name == "nt" else b"")

    async def test_verified_session_blocks_and_closes_unready_profile(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            verifier = FakeVerifier(False, "environment_changed")
            manager = ProfileManager(root, backend)
            with self.assertRaises(ProfileNotReadyError) as raised:
                async with manager.verified_session(
                    "same",
                    verifier,
                    expected_email="same@example.com",
                    account_probe=FakeAccountProbe(),
                ):
                    self.fail("unready profile was yielded")
            self.assertEqual(raised.exception.reason, "environment_changed")
            self.assertEqual(len(backend.closed), 1)
            status = await manager.status("same")
            self.assertEqual(status.state, "blocked")
            self.assertEqual(status.last_error, "environment_changed")

    async def test_verified_session_yields_ready_profile(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            verifier = FakeVerifier()
            manager = ProfileManager(root, backend)
            async with manager.verified_session(
                "same",
                verifier,
                expected_email="same@example.com",
                account_probe=FakeAccountProbe(),
            ) as context:
                self.assertIs(context, backend.opened[0][1])
            self.assertEqual(len(verifier.calls), 1)
            self.assertEqual(len(backend.closed), 1)

    async def test_bootstrap_profile_can_reopen_same_persistent_directory(self):
        with TemporaryDirectory() as root:
            backend = CookieBackend()
            manager = ProfileManager(root, backend)
            verifier = FakeVerifier()
            rows = [{"name": "SID", "value": "same-account", "domain": "google.com"}]
            async with manager.bootstrap_session(
                "same@example.com", verifier, rows, account_probe=FakeAccountProbe()
            ) as context:
                self.assertEqual(await context.cookies(), rows)
            first = await manager.spec("same@example.com")
            async with manager.ready_session("same@example.com", verifier) as context:
                self.assertEqual(await context.cookies(), rows)
            reopened = await manager.spec("same@example.com")
            self.assertEqual(first.profile_dir, reopened.profile_dir)
            self.assertEqual(first.fingerprint_seed, reopened.fingerprint_seed)

    async def test_credential_session_does_not_require_workspace_page(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, CookieBackend())

            class Verifier:
                async def verify_environment(self, context, profile_dir):
                    return "environment_stable"

            async with manager.credential_session("same@example.com", Verifier()):
                pass

    async def test_bootstrap_clears_stale_browser_state_but_keeps_identity_settings(self):
        with TemporaryDirectory() as root:
            backend = CookieBackend()
            manager = ProfileManager(root, backend)
            spec = await manager.spec("same@example.com")
            settings = Path(spec.profile_dir) / "launch_settings.sha256"
            settings.write_text("stable", encoding="utf-8")
            (Path(spec.profile_dir) / "stale-cookie-state").write_text("old", encoding="utf-8")
            (Path(spec.profile_dir) / "Default").mkdir()
            seed = spec.fingerprint_seed
            verifier = FakeVerifier()
            rows = [{"name": "SID", "value": "new-account", "domain": "google.com"}]
            async with manager.bootstrap_session(
                "same@example.com", verifier, rows, account_probe=FakeAccountProbe()
            ):
                pass
            self.assertFalse((Path(spec.profile_dir) / "stale-cookie-state").exists())
            self.assertFalse((Path(spec.profile_dir) / "Default").exists())
            self.assertEqual(
                (Path(spec.profile_dir) / "launch_settings.sha256").read_text(), "stable"
            )
            self.assertEqual((await manager.spec("same@example.com")).fingerprint_seed, seed)

    async def test_remove_deletes_profile_and_releases_seed(self):
        with TemporaryDirectory() as root:
            manager = ProfileManager(root, FakeBackend())
            spec = await manager.spec("same@example.com")
            profile_dir = Path(spec.profile_dir)
            (profile_dir / "Default").mkdir()
            await manager.remove("same@example.com")
            self.assertFalse(profile_dir.exists())
            recreated = await manager.spec("same@example.com")
            self.assertEqual(recreated.fingerprint_seed, spec.fingerprint_seed)

    async def test_verified_session_blocks_account_probe_failure(self):
        with TemporaryDirectory() as root:
            backend = FakeBackend()
            verifier = FakeVerifier()
            manager = ProfileManager(root, backend)
            with self.assertRaises(ProfileNotReadyError) as raised:
                async with manager.verified_session(
                    "same",
                    verifier,
                    expected_email="same@example.com",
                    account_probe=FakeAccountProbe(False),
                ):
                    self.fail("invalid account was yielded")
            self.assertEqual(raised.exception.reason, "identity_mismatch")
            self.assertEqual((await manager.status("same")).last_error, "identity_mismatch")

    async def test_invalid_capacity_and_empty_key_are_rejected(self):
        with TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                ProfileManager(root, FakeBackend(), max_concurrent=0)
            manager = ProfileManager(root, FakeBackend())
            with self.assertRaises(ValueError):
                await manager.spec("...///...")

    async def test_proxy_is_bound_to_each_profile_session(self):
        with TemporaryDirectory() as root:
            backend = ProxyBackend()
            manager = ProfileManager(root, backend)
            async with manager.session("user", proxy_url="http://proxy.invalid:8080"):
                pass
            self.assertEqual(backend.proxies, ["http://proxy.invalid:8080"])

    def test_cloak_seed_is_stable_and_in_range(self):
        with TemporaryDirectory() as root:
            profile = Path(root) / "user"
            profile.mkdir()
            (profile / "metadata.json").write_text('{"fingerprint_seed": 12345}', encoding="utf-8")
            first = CloakBrowserBackend._profile_seed(profile)
            second = CloakBrowserBackend._profile_seed(profile)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 10000)
        self.assertLessEqual(first, 99999)

    async def test_cloak_backend_adds_container_safe_crash_reporter_flag(self):
        import sys
        from unittest.mock import patch

        with TemporaryDirectory() as root:
            profile = Path(root)
            (profile / "metadata.json").write_text(
                json.dumps({"fingerprint_seed": 12345}), encoding="utf-8"
            )
            captured = {}

            async def launch(*args, **options):
                captured.update(options)
                return object()

            fake_cloak = type("FakeCloak", (), {"launch_persistent_context_async": launch})()
            with patch.dict(sys.modules, {"cloakbrowser": fake_cloak}):
                await CloakBrowserBackend().open(profile)

            self.assertIn("--disable-breakpad", captured["args"])
            self.assertIn("--disable-crash-reporter", captured["args"])
            self.assertIn("--fingerprint=12345", captured["args"])


if __name__ == "__main__":
    unittest.main()
