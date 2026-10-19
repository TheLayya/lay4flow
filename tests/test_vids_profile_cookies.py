import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from src.services.browser.profiles import ProfileManager, _ProfileFileLock
from src.services.vids.client import VidsTransportError, cookie_jar
from src.services.vids.profile_cookies import VidsProfileCookieSource

EDITOR = "https://docs.google.com/videos/d/synthetic-doc/edit?authuser=1"
ACCOUNT = "Synthetic.User@example.com"
COOKIE = {
    "name": "SAPISID",
    "value": "synthetic-secret-cookie",
    "domain": ".google.com",
    "path": "/",
    "expires": 4102444800.5,
    "secure": True,
    "httpOnly": True,
}


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.redirect = None
        self.error = None
        self.close_error = None
        self.blocked = None
        self.calls = []
        self.closed = False

    async def goto(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if self.blocked is not None:
            await self.blocked.wait()
        self.url = self.redirect or url

    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.rows = [dict(COOKIE)]
        self.error = None
        self.closed = False
        self.cookie_reads = 0

    async def new_page(self):
        return self.page

    async def cookies(self):
        self.cookie_reads += 1
        if self.error:
            raise self.error
        return self.rows


class FakeVerifier:
    timeout_seconds = 1

    def __init__(self):
        self.result = "environment_stable"
        self.calls = []

    async def verify_environment(self, context, directory):
        self.calls.append((context, directory))
        return self.result


class ProfileBackend:
    def __init__(self, context):
        self.context = context
        self.opens = 0

    async def open(self, profile_dir):
        self.opens += 1
        return self.context

    async def close(self, context):
        context.closed = True


class FakeManager:
    _normalize_account_key = staticmethod(ProfileManager._normalize_account_key)
    _validate_seed = staticmethod(ProfileManager._validate_seed)

    def __init__(self, root_dir):
        self.root_dir = Path(root_dir)
        self.context = FakeContext()
        self.calls = []
        self.error = None
        self.exit_error = None

    def save_profile(self, account=ACCOUNT):
        normalized = self._normalize_account_key(account)
        directory = self.root_dir / normalized
        (directory / "Default").mkdir(parents=True)
        (directory / "metadata.json").write_text(
            json.dumps(
                {
                    "account_key": normalized,
                    "profile_dir": str(directory),
                    "state": "stopped",
                    "fingerprint_seed": 12345,
                }
            ),
            encoding="utf-8",
        )
        (directory / "environment_snapshot.json").write_text("synthetic", encoding="utf-8")
        (directory / "launch_settings.sha256").write_text("a" * 64, encoding="utf-8")
        return directory

    async def spec(self, account_key, *, existing_only=False):
        if not existing_only:
            raise AssertionError("Vids attempted to initialize a Profile")
        return ProfileManager._existing_spec(self, self._normalize_account_key(account_key))

    @asynccontextmanager
    async def credential_session(
        self, account_key, verifier, *, proxy_url=None, existing_only=False
    ):
        if not existing_only:
            raise AssertionError("Vids attempted to initialize a Profile")
        self.calls.append((account_key, verifier, proxy_url))
        try:
            if self.error:
                raise self.error
            directory = self.root_dir / self._normalize_account_key(account_key)
            if (
                await verifier.verify_environment(self.context, str(directory))
                != "environment_stable"
            ):
                raise RuntimeError("synthetic environment changed")
            yield self.context
        finally:
            self.context.closed = True
            if self.exit_error:
                raise self.exit_error


class VidsProfileCookieSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = FakeManager(self.directory.name)
        self.profile = self.manager.save_profile()
        self.verifier = FakeVerifier()
        self.source = VidsProfileCookieSource(self.manager, ACCOUNT, self.verifier)

    async def test_existing_profile_cookie_scope_and_expiry_are_preserved(self):
        host_cookie = {
            **COOKIE,
            "name": "__Host-synthetic",
            "domain": "docs.google.com",
            "expires": -1,
        }
        self.manager.context.rows += [host_cookie, {**COOKIE, "domain": "accounts.google.com"}]
        rows = await self.source(EDITOR, "socks5://synthetic-proxy:1234")
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0], {**COOKIE, "hostOnly": False, "expirationDate": COOKIE["expires"]}
        )
        self.assertEqual(rows[1], {**host_cookie, "hostOnly": True})
        self.assertEqual(self.manager.context.rows[0], COOKIE)
        self.assertEqual(len(cookie_jar(rows)), 2)
        self.assertEqual(
            self.manager.calls, [(ACCOUNT, self.verifier, "socks5://synthetic-proxy:1234")]
        )
        self.assertEqual(self.verifier.calls, [(self.manager.context, str(self.profile))])
        self.assertEqual(
            self.manager.context.page.calls, [(EDITOR, {"wait_until": "domcontentloaded"})]
        )
        self.assertTrue(self.manager.context.page.closed)
        self.assertTrue(self.manager.context.closed)

    async def test_explicit_cookie_fields_are_preserved(self):
        cookie = {**COOKIE, "hostOnly": True, "expirationDate": 4102444900, "sameSite": "Lax"}
        self.manager.context.rows = [cookie]
        self.assertEqual(await self.source(EDITOR), [cookie])

    async def test_bad_editor_urls_do_not_open_a_profile(self):
        for url in (
            None,
            "http://docs.google.com/videos/d/doc/edit",
            "https://docs.google.com.attacker.example/videos/d/doc/edit",
            "https://synthetic-secret@docs.google.com/videos/d/doc/edit",
            "https://docs.google.com:444/videos/d/doc/edit",
            "https://docs.google.com:invalid/videos/d/doc/edit",
            "https://docs.google.com/videos/d/doc/edit/extra",
            "https://docs.google.com/videos/d/doc/edit?token=synthetic-secret",
            "https://docs.google.com/videos/d/doc/edit?authuser=-1",
            "https://docs.google.com/videos/d/doc/edit?authuser=0&authuser=1",
            "https://docs.google.com/videos/d/doc/edit#synthetic-secret",
            "\nhttps://docs.google.com/videos/d/doc/edit",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as raised:
                await self.source(url)
            self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertFalse(self.manager.calls)

    async def test_account_keys_must_be_explicit_strings(self):
        for account in (None, 123, "", " \t", "synthetic-secret\n"):
            with self.subTest(account=account), self.assertRaises(ValueError) as raised:
                VidsProfileCookieSource(self.manager, account, self.verifier)
            self.assertNotIn("synthetic-secret", str(raised.exception))

    async def test_missing_profile_components_do_not_create_or_open_a_profile(self):
        for name in (
            "metadata.json",
            "Default",
            "environment_snapshot.json",
            "launch_settings.sha256",
        ):
            with self.subTest(name=name):
                original = self.profile / name
                moved = self.profile / (name + ".saved")
                original.rename(moved)
                try:
                    with self.assertRaises(ValueError):
                        await self.source(EDITOR)
                    self.assertFalse(original.exists())
                finally:
                    moved.rename(original)
        self.assertFalse(self.manager.calls)

    async def test_wrong_account_and_invalid_seed_are_rejected_before_open(self):
        for metadata in (
            [],
            {"account_key": "synthetic-secret", "fingerprint_seed": 12345},
            {"account_key": self.profile.name},
            {"account_key": self.profile.name, "fingerprint_seed": True},
            {"account_key": self.profile.name, "fingerprint_seed": 9999},
        ):
            with self.subTest(metadata=metadata):
                (self.profile / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaises(ValueError) as raised:
                    await self.source(EDITOR)
                self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertFalse(self.manager.calls)

    async def test_unknown_profile_is_never_created(self):
        account = "Unknown.User@example.com"
        profile = self.manager.root_dir / self.manager._normalize_account_key(account)
        source = VidsProfileCookieSource(self.manager, account, self.verifier)
        with self.assertRaises(ValueError):
            await source(EDITOR)
        self.assertFalse(profile.exists())
        self.assertFalse(self.manager.calls)

    async def test_profile_deleted_while_waiting_for_account_lock_is_not_recreated(self):
        backend = ProfileBackend(self.manager.context)
        manager = ProfileManager(self.directory.name, backend)
        await manager.spec(ACCOUNT)
        source = VidsProfileCookieSource(manager, ACCOUNT, self.verifier)
        lock = await manager._account_lock(self.profile.name)
        await lock.acquire()
        checked = asyncio.Event()
        spec = manager.spec

        async def observed_spec(*args, **kwargs):
            result = await spec(*args, **kwargs)
            checked.set()
            return result

        with patch.object(manager, "spec", observed_spec):
            refresh = asyncio.create_task(source(EDITOR))
            await checked.wait()
            await ProfileManager(self.directory.name, backend).remove(ACCOUNT)
            lock.release()
            with self.assertRaises(VidsTransportError):
                await refresh
        self.assertFalse(self.profile.exists())
        self.assertEqual(backend.opens, 0)
        self.assertFalse(lock.locked())
        self.assertEqual(manager._capacity._value, 1)

    async def test_profile_changed_while_waiting_for_file_lock_is_not_opened_or_repaired(self):
        backend = ProfileBackend(self.manager.context)
        manager = ProfileManager(self.directory.name, backend)
        source = VidsProfileCookieSource(manager, ACCOUNT, self.verifier)
        file_lock = _ProfileFileLock(self.profile / ".profile.lock")
        await file_lock.acquire()
        entered = asyncio.Event()
        acquire = _ProfileFileLock.acquire

        async def observed_acquire(lock):
            entered.set()
            return await acquire(lock)

        try:
            with patch.object(_ProfileFileLock, "acquire", observed_acquire):
                refresh = asyncio.create_task(source(EDITOR))
                await entered.wait()
                metadata = self.profile / "metadata.json"
                metadata.unlink()
                file_lock.release()
                with self.assertRaises(VidsTransportError):
                    await refresh
        finally:
            file_lock.release()
        self.assertFalse(metadata.exists())
        self.assertEqual(backend.opens, 0)
        self.assertFalse((await manager._account_lock(self.profile.name)).locked())
        self.assertEqual(manager._capacity._value, 1)

    async def test_existing_only_profile_rejects_reset_before_open(self):
        backend = ProfileBackend(self.manager.context)
        manager = ProfileManager(self.directory.name, backend)
        with self.assertRaises(ValueError):
            async with manager.session(ACCOUNT, existing_only=True, reset_state=True):
                self.fail("Existing Profile was opened for reset")
        self.assertTrue((self.profile / "Default").is_dir())
        self.assertEqual(backend.opens, 0)

    async def test_existing_only_profile_seed_conflict_does_not_open_or_rewrite_metadata(self):
        backend = ProfileBackend(self.manager.context)
        manager = ProfileManager(self.directory.name, backend)
        manager._claim_seed(manager._normalize_account_key("Another.User@example.com"), 12345)
        metadata = self.profile / "metadata.json"
        original = metadata.read_text(encoding="utf-8")
        source = VidsProfileCookieSource(manager, ACCOUNT, self.verifier)
        with self.assertRaises(VidsTransportError):
            await source(EDITOR)
        self.assertEqual(metadata.read_text(encoding="utf-8"), original)
        self.assertEqual(backend.opens, 0)
        self.assertEqual(manager._capacity._value, 1)

    async def test_empty_or_malformed_cookies_are_sanitized_and_closed(self):
        for rows in (
            [],
            None,
            ["synthetic-secret"],
            [{**COOKIE, "value": "synthetic-secret\r\n"}],
            [{**COOKIE, "domain": "synthetic-secret\n"}],
            [{**COOKIE, "path": "synthetic-secret"}],
            [{**COOKIE, "hostOnly": "synthetic-secret"}],
            [{**COOKIE, "secure": "synthetic-secret"}],
            [{**COOKIE, "httpOnly": "synthetic-secret"}],
            [{**COOKIE, "expires": "synthetic-secret"}],
            [{**COOKIE, "expires": float("nan")}],
            [{**COOKIE, "expires": -2}],
            [{**COOKIE, "name": "__Host-synthetic", "hostOnly": False}],
            [{**COOKIE, "domain": "attacker.example"}],
            [{**COOKIE, "domain": "..google.com"}],
            [{**COOKIE, "partitionKey": "https://synthetic.example"}],
            [{**COOKIE, "partitioned": True}],
        ):
            with self.subTest(rows=rows):
                self.manager.context.rows = rows
                with self.assertRaises(VidsTransportError) as raised:
                    await self.source(EDITOR)
                self.assertEqual(str(raised.exception), "Vids Profile cookie refresh failed")
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertTrue(self.manager.context.page.closed)
                self.assertTrue(self.manager.context.closed)

    async def test_environment_change_never_navigates_to_editor(self):
        self.verifier.result = "environment_changed"
        with self.assertRaises(VidsTransportError):
            await self.source(EDITOR)
        self.assertFalse(self.manager.context.page.calls)
        self.assertEqual(self.manager.context.cookie_reads, 0)
        self.assertTrue(self.manager.context.closed)

    async def test_browser_and_cleanup_exceptions_never_expose_credentials(self):
        for owner, field in (
            (self.manager, "error"),
            (self.manager.context.page, "error"),
            (self.manager.context, "error"),
            (self.manager.context.page, "close_error"),
            (self.manager, "exit_error"),
        ):
            with self.subTest(field=field):
                setattr(owner, field, RuntimeError("synthetic-secret " + EDITOR + " " + ACCOUNT))
                try:
                    with self.assertRaises(VidsTransportError) as raised:
                        await self.source(EDITOR)
                    self.assertEqual(str(raised.exception), "Vids Profile cookie refresh failed")
                    self.assertTrue(self.manager.context.closed)
                finally:
                    setattr(owner, field, None)

    async def test_login_redirect_does_not_export_cookies(self):
        self.manager.context.page.redirect = (
            "https://accounts.google.com/signin?secret=synthetic-secret"
        )
        with self.assertRaises(VidsTransportError) as raised:
            await self.source(EDITOR)
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertEqual(self.manager.context.cookie_reads, 0)
        self.assertTrue(self.manager.context.page.closed)
        self.assertTrue(self.manager.context.closed)

    async def test_timeout_and_cancellation_close_page_and_context(self):
        self.manager.context.page.blocked = asyncio.Event()
        self.verifier.timeout_seconds = 0.01
        with self.assertRaises(VidsTransportError):
            await self.source(EDITOR)
        self.assertTrue(self.manager.context.page.closed)
        self.assertTrue(self.manager.context.closed)
        self.verifier.timeout_seconds = 1
        self.manager.context.page.closed = self.manager.context.closed = False
        task = asyncio.create_task(self.source(EDITOR))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.manager.context.page.closed)
        self.assertTrue(self.manager.context.closed)


if __name__ == "__main__":
    unittest.main()
