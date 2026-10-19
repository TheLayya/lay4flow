"""Direct onboarding keeps an unverified Profile out of generation scheduling."""

import hashlib
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.database import Database
from src.core.models import Token
from src.services.browser.profile_session_refresher import ProfileSessionRefresher
from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.profile_viewer import ProfileBrowserViewer, ViewerError
from src.services.browser.profiles import ProfileManager
from src.services.token_manager import TokenManager


class LoginBackend:
    options = {"headless": False}

    def __init__(self):
        self.opens = 0
        self.closed = 0
        self.fail_close = False
        self.cookies = []
        self.identity = "user@example.invalid"
        self.session = {
            "access_token": "synthetic-access-token",
            "expires": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
            "user": {"email": self.identity},
        }
        self.gets = []

    async def open(self, profile_dir, **kwargs):
        self.opens += 1
        (profile_dir / "Default").mkdir(exist_ok=True)
        (profile_dir / "launch_settings.sha256").write_text("synthetic-options")
        page = SimpleNamespace(
            goto=AsyncMock(),
            close=AsyncMock(),
            evaluate=AsyncMock(
                return_value={
                    "userAgent": "SyntheticBrowser/1.0",
                    "language": "en-US",
                    "languages": ["en-US"],
                    "timezone": "UTC",
                    "platform": "Linux x86_64",
                    "width": 1280,
                    "height": 720,
                }
            ),
        )
        return SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            cookies=AsyncMock(side_effect=lambda: self.cookies),
            add_cookies=AsyncMock(side_effect=AssertionError("direct login injected Cookies")),
            request=SimpleNamespace(get=AsyncMock(side_effect=self.request)),
        )

    async def request(self, url, **kwargs):
        self.gets.append(url)
        body = {"credits": 100} if url.endswith("/credits") else self.session
        return SimpleNamespace(status=200, json=AsyncMock(return_value=body))

    async def close(self, context):
        if self.fail_close:
            raise RuntimeError("synthetic failed close")
        self.closed += 1


class DirectProfileLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db = Database(str(root / "accounts.db"))
        await self.db.init_db()
        self.proxy_id = await self.db.add_proxy_pool_entry("socks5h://relay.invalid:1080")
        self.backend = LoginBackend()
        self.manager = ProfileManager(root / "profiles", self.backend)
        flow = SimpleNamespace(api_base_url="https://example.invalid")
        self.tokens = TokenManager(self.db, flow)
        self.refresher = ProfileSessionRefresher(self.manager, ProfileVerifier(), self.tokens)
        self.refresher._proxy_url = AsyncMock(return_value="")
        self.refresher._renew_session = AsyncMock(side_effect=self.renew)
        self.tokens.profile_refresher = self.refresher
        self.tokens.ensure_project_exists = AsyncMock(return_value="synthetic-project")

        async def flow_call(token, call):
            return await call()

        self.tokens._flow_call_for_token = AsyncMock(side_effect=flow_call)
        self.tokens.notify_profile_ready = Mock()
        self.environment = patch.dict(os.environ, {"LAY4FLOW_PROFILE_VIEWER_ENABLED": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.viewer = ProfileBrowserViewer(self.tokens)
        self.addAsyncCleanup(self.viewer.close_all)

    async def renew(self, context):
        self.backend.cookies = [
            {
                "name": "__Secure-next-auth.session-token",
                "value": "synthetic-session-token",
                "domain": "labs.google",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        ]
        return "session_renewed"

    async def start(self):
        return await self.viewer.login.start(" User@Example.Invalid ", "", "owner")

    async def test_close_cold_probe_and_project_must_pass_before_new_account_is_enabled(self):
        receipt = await self.start()
        token_id = receipt["token_id"]
        pending = await self.db.get_token(token_id)
        self.assertEqual(pending.email, "user@example.invalid")
        self.assertTrue(pending.st.startswith("profile-login:"))
        self.assertEqual(pending.login_password, "")
        self.assertFalse(pending.is_active)
        self.assertFalse(pending.auto_refresh_enabled)
        self.assertEqual(pending.proxy_pool_id, self.proxy_id)
        self.assertEqual(pending.proxy_url, "socks5h://relay.invalid:1080")
        self.assertEqual(await self.db.get_active_tokens(), [])
        self.assertEqual(receipt["viewer_url"], f"/api/profile-browser/{token_id}")
        spec = await self.manager.spec(pending.email)
        seed = spec.fingerprint_seed
        protected = spec.profile_dir / "Default" / "persistent-site-state"
        protected.write_bytes(b"synthetic Google login state")
        fingerprint = hashlib.sha256(protected.read_bytes()).hexdigest()

        async def prepare(token_id):
            self.assertFalse((await self.db.get_token(token_id)).is_active)
            self.assertEqual(self.backend.opens, 3, "cold probe must open the saved Profile")
            self.assertEqual(self.backend.closed, 3)
            await self.db.update_token(token_id, current_project_id="synthetic-project")
            return "synthetic-project"

        self.tokens.ensure_project_exists.side_effect = prepare
        result = await self.viewer.login.complete(
            receipt["session_id"], "owner", receipt["viewer_id"]
        )
        ready = await self.db.get_token(token_id)
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["credits"], 100)
        self.assertTrue(ready.is_active)
        self.assertTrue(ready.auto_refresh_enabled)
        self.assertTrue(ready.image_enabled and ready.video_enabled)
        self.assertEqual(ready.current_project_id, "synthetic-project")
        self.assertEqual(ready.st, "synthetic-session-token")
        self.assertEqual(ready.browser_profile_status, "ready")
        self.assertEqual(seed, (await self.manager.spec(pending.email)).fingerprint_seed)
        self.assertEqual(fingerprint, hashlib.sha256(protected.read_bytes()).hexdigest())
        self.assertEqual(
            self.refresher._renew_session.await_count, 1, "cold check must be read-only"
        )
        self.assertEqual(self.backend.gets.count("https://example.invalid/credits"), 2)
        self.assertNotIn("at", result)
        self.assertEqual(
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"]),
            result,
        )
        self.tokens.ensure_project_exists.assert_awaited_once()

    async def test_owner_generation_and_existing_identity_are_checked_without_overwrite(self):
        receipt = await self.start()
        for owner, viewer_id in (("other", receipt["viewer_id"]), ("owner", "stale")):
            with self.assertRaises(ViewerError):
                await self.viewer.login.complete(receipt["session_id"], owner, viewer_id)
        self.refresher._renew_session.assert_not_awaited()
        self.backend.session["user"]["email"] = "different@example.invalid"
        with self.assertRaises(ViewerError) as mismatch:
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertEqual(mismatch.exception.status, 422)
        self.assertFalse((await self.db.get_token(receipt["token_id"])).is_active)
        self.tokens.ensure_project_exists.assert_not_awaited()

    async def test_rejects_busy_existing_email_and_existing_profile_before_writes(self):
        self.viewer._task = object()
        with self.assertRaises(ViewerError):
            await self.start()
        self.viewer._task = None
        self.assertEqual(await self.db.get_all_tokens(), [])
        existing = await self.db.add_token(
            Token(st="synthetic-existing", email="user@example.invalid")
        )
        with self.assertRaises(ViewerError):
            await self.start()
        self.assertEqual(len(await self.db.get_all_tokens()), 1)
        await self.db.delete_token(existing)
        spec = await self.manager.spec("user@example.invalid")
        with self.assertRaises(ViewerError):
            await self.start()
        self.assertEqual(await self.db.get_all_tokens(), [])
        self.assertTrue((spec.profile_dir / "metadata.json").exists())

    async def test_explicit_pool_binding_and_unavailable_pool_fail_before_account_write(self):
        second = await self.db.add_proxy_pool_entry("socks5h://other.invalid:1080")
        receipt = await self.viewer.login.start(
            "user@example.invalid", "", "owner", proxy_pool_id=second
        )
        account = await self.db.get_token(receipt["token_id"])
        self.assertEqual(account.proxy_pool_id, second)
        self.assertEqual(account.proxy_url, "socks5h://other.invalid:1080")
        await self.viewer.login.cancel(receipt["session_id"], "owner", receipt["viewer_id"])
        await self.db.set_proxy_pool_enabled(second, False)
        with self.assertRaises(ViewerError):
            await self.viewer.login.start("user@example.invalid", "", "owner", proxy_pool_id=second)
        self.assertEqual(await self.db.get_all_tokens(), [])

    async def test_cold_identity_failure_and_manual_disable_do_not_enable_account(self):
        receipt = await self.start()
        refresh = self.viewer.refresh

        async def change_identity(*args, **kwargs):
            result = await refresh(*args, **kwargs)
            self.backend.session["user"]["email"] = "different@example.invalid"
            return result

        self.viewer.refresh = change_identity
        with self.assertRaises(ViewerError):
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        self.tokens.ensure_project_exists.assert_not_awaited()
        self.backend.session["user"]["email"] = "user@example.invalid"

        async def manually_disable(token_id):
            await self.db.update_token(token_id, ban_reason="manual")

        self.tokens.ensure_project_exists.side_effect = manually_disable
        with self.assertRaises(ViewerError):
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        saved = await self.db.get_token(receipt["token_id"])
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")

    async def test_cancel_and_expiry_remove_only_owned_pending_row_and_profile(self):
        for operation in ("cancel", "expire"):
            receipt = await self.start()
            profile = (await self.manager.spec("user@example.invalid")).profile_dir
            if operation == "cancel":
                await self.viewer.login.cancel(receipt["session_id"], "owner", receipt["viewer_id"])
            else:
                pending = self.viewer.login._sessions[receipt["session_id"]]
                pending["timer"].cancel()
                pending["expires_at"] = time.time() - 1
                await self.viewer.login._expire(receipt["session_id"])
            self.assertIsNone(await self.db.get_token(receipt["token_id"]))
            self.assertFalse(profile.exists())

    async def test_invalid_email_proxy_and_startup_failure_leave_no_pending_account(self):
        for email in ("invalid", "user\n@example.invalid", "user@example.invalid/path"):
            with self.subTest(email=email):
                with self.assertRaises(ViewerError):
                    await self.viewer.login.start(email, "", "owner")
        with self.assertRaises(ViewerError):
            await self.viewer.login.start("user@example.invalid", "socks5://bad.invalid:0", "owner")
        with patch.object(
            self.viewer, "_open", AsyncMock(side_effect=ViewerError(503, "synthetic"))
        ):
            with self.assertRaises(ViewerError):
                await self.start()
        self.assertEqual(await self.db.get_all_tokens(), [])
        self.assertEqual(self.viewer.login._sessions, {})

    async def test_project_failure_stays_disabled_and_can_be_retried_without_new_login(self):
        receipt = await self.start()
        self.tokens.ensure_project_exists.side_effect = RuntimeError("synthetic project failure")
        with self.assertRaises(ViewerError):
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertFalse((await self.db.get_token(receipt["token_id"])).is_active)
        self.tokens.ensure_project_exists.side_effect = None
        result = await self.viewer.login.complete(
            receipt["session_id"], "owner", receipt["viewer_id"]
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.refresher._renew_session.await_count, 1)

    async def test_failed_close_retains_pending_identity_and_profile_lock(self):
        receipt = await self.start()
        self.backend.fail_close = True
        try:
            with self.assertRaises(ViewerError):
                await self.viewer.login.cancel(receipt["session_id"], "owner", receipt["viewer_id"])
            self.assertIsNotNone(await self.db.get_token(receipt["token_id"]))
            self.assertEqual(self.viewer._status, "error")
            self.assertTrue(self.manager._failed_handles)
        finally:
            self.backend.fail_close = False
            # The production failure state deliberately retains ownership until restart.
            # Release only this synthetic handle so the temporary directory is disposable.
            for handle in self.manager._failed_handles.values():
                handle.file_lock.release()
            self.viewer._task = None
            self.viewer.login._sessions[receipt["session_id"]]["timer"].cancel()
            self.viewer.login._sessions.clear()


if __name__ == "__main__":
    unittest.main()
