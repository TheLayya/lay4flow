"""Direct onboarding keeps an unverified Profile out of generation scheduling."""

import asyncio
import hashlib
import os
import time
import unittest
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
from src.services.flow.flow_rpc import FlowRPCError
from src.services.token_manager import TokenManager


class LoginBackend:
    options = {"headless": False}

    def __init__(self):
        self.opens = 0
        self.closed = 0
        self.fail_close = False
        self.cookies = []
        self.identity = "user@example.invalid"
        self.signed_in = True
        self.flow_error = None
        self.flow_reads = []
        self.launch_proxies = []

    async def open(self, profile_dir, **kwargs):
        self.opens += 1
        self.launch_proxies.append(kwargs.get("proxy_url"))
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
            request=SimpleNamespace(
                get=AsyncMock(side_effect=AssertionError("unexpected network request"))
            ),
        )

    async def read_flow_session(self, context):
        self.flow_reads.append(context)
        if self.flow_error is not None:
            raise self.flow_error
        if not self.signed_in:
            raise FlowRPCError("login_required", "flow_bootstrap")
        return {"email": self.identity, "credits": 100, "user_paygate_tier": "PAYGATE_TIER_ONE"}

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
        self.flow_probe_patch = patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(side_effect=self.backend.read_flow_session),
        )
        self.flow_probe = self.flow_probe_patch.start()
        self.addCleanup(self.flow_probe_patch.stop)
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

    async def login(self):
        self.backend.signed_in = True
        self.backend.cookies = [
            {"name": "SID", "value": "synthetic-google-login", "domain": ".google.com", "path": "/"}
        ]

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
        self.assertTrue(ready.st.startswith("profile-login:"))
        self.assertIsNone(ready.at)
        self.assertIsNone(ready.at_expires)
        self.assertEqual(ready.browser_profile_status, "ready")
        self.assertEqual(seed, (await self.manager.spec(pending.email)).fingerprint_seed)
        self.assertEqual(fingerprint, hashlib.sha256(protected.read_bytes()).hexdigest())
        self.assertEqual(
            len(self.backend.flow_reads),
            2,
            "live and cold contexts both need fresh Flow validation",
        )
        self.assertIsNot(self.backend.flow_reads[0], self.backend.flow_reads[1])
        self.assertNotIn("at", result)
        self.assertEqual(
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"]),
            result,
        )
        self.tokens.ensure_project_exists.assert_awaited_once()

    async def test_logged_in_browser_completes_with_fresh_live_and_cold_probes(self):
        receipt = await self.start()
        await self.login()
        result = await self.viewer.login.complete(
            receipt["session_id"], "owner", receipt["viewer_id"]
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(self.backend.flow_reads), 2)
        self.assertIn(
            "synthetic-google-login", (await self.db.get_token(receipt["token_id"])).google_cookies
        )
        self.assertEqual(self.backend.opens, 3)
        self.assertEqual(self.backend.closed, 3)
        self.assertFalse(hasattr(self.refresher, "_renew_session"))

    async def test_existing_wrong_identity_is_reported_without_token_replacement(self):
        receipt = await self.start()
        self.backend.identity = "other@example.invalid"
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertEqual(caught.exception.detail["reason"], "identity_mismatch")
        self.assertEqual(caught.exception.detail["stage"], "session_probe")
        self.assertEqual(caught.exception.detail["network_step"], "flow_identity")
        self.assertTrue(caught.exception.detail["browser_open"])
        saved = await self.db.get_token(receipt["token_id"])
        self.assertTrue(saved.st.startswith("profile-login:"))
        self.assertFalse(saved.is_active)

    async def test_signed_out_profile_stays_pending_and_rechecks_after_manual_login(self):
        receipt = await self.start()
        self.backend.signed_in = False
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertEqual(caught.exception.detail["reason"], "login_required")
        self.assertEqual(caught.exception.detail["network_step"], "flow_bootstrap")
        self.assertFalse((await self.db.get_token(receipt["token_id"])).is_active)
        await self.login()
        result = await self.viewer.login.complete(
            receipt["session_id"], "owner", receipt["viewer_id"]
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(self.backend.flow_reads), 3)

    async def test_flow_credits_failure_reports_stage_and_keeps_live_profile_open(self):
        receipt = await self.start()
        self.backend.flow_error = FlowRPCError("transient_error", "flow_credits", 503)
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertEqual(caught.exception.detail["stage"], "credits_probe")
        self.assertEqual(caught.exception.detail["network_step"], "flow_credits")
        self.assertTrue(caught.exception.detail["browser_open"])
        self.assertFalse((await self.db.get_token(receipt["token_id"])).is_active)

    async def test_network_failure_has_staged_private_detail_and_same_session_can_retry(self):
        receipt = await self.start()
        self.backend.flow_error = FlowRPCError("transient_error", "flow_bootstrap", 503)
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        detail = caught.exception.detail
        self.assertEqual(detail["reason"], "transient_error")
        self.assertEqual(detail["stage"], "session_probe")
        self.assertEqual(detail["network_step"], "flow_bootstrap")
        self.assertTrue(detail["retryable"] and detail["browser_open"])
        self.assertNotIn("synthetic-google-login", str(detail))
        saved = await self.db.get_token(receipt["token_id"])
        self.assertEqual(saved.refresh_reason, "transient_error")
        self.assertEqual(saved.refresh_network_step, "flow_bootstrap")
        self.assertFalse(saved.is_active)
        self.backend.flow_error = None
        result = await self.viewer.login.complete(
            receipt["session_id"], "owner", receipt["viewer_id"]
        )
        self.assertTrue(result["success"])
        saved = await self.db.get_token(receipt["token_id"])
        self.assertEqual(
            (saved.refresh_reason, saved.refresh_stage, saved.refresh_network_step), ("", "", "")
        )

    async def test_owner_generation_and_existing_identity_are_checked_without_overwrite(self):
        receipt = await self.start()
        for owner, viewer_id in (("other", receipt["viewer_id"]), ("owner", "stale")):
            with self.assertRaises(ViewerError):
                await self.viewer.login.complete(receipt["session_id"], owner, viewer_id)
        self.assertEqual(self.backend.flow_reads, [])
        self.backend.identity = "different@example.invalid"
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

    async def test_selected_proxy_survives_anonymous_identity_adoption_and_cold_reopen(self):
        selected_url = "http://synthetic:secret@residential.invalid:8080"
        selected_id = await self.db.add_proxy_pool_entry(selected_url)
        self.refresher._proxy_url = ProfileSessionRefresher._proxy_url.__get__(self.refresher)

        receipt = await self.viewer.login.start(owner="owner", proxy_pool_id=selected_id)
        pending = await self.db.get_token(receipt["token_id"])
        self.assertEqual((pending.proxy_pool_id, pending.proxy_url), (selected_id, selected_url))
        self.assertFalse(pending.is_active)
        await self.login()

        result = await self.viewer.login.complete(
            receipt["session_id"], "owner", receipt["viewer_id"]
        )

        saved = await self.db.get_token(receipt["token_id"])
        self.assertTrue(result["success"] and saved.is_active)
        self.assertEqual(saved.email, self.backend.identity)
        self.assertEqual((saved.proxy_pool_id, saved.proxy_url), (selected_id, selected_url))
        self.assertEqual(self.backend.launch_proxies, [selected_url] * 3)
        self.assertEqual(self.backend.closed, 3)
        self.assertNotIn("secret", str(result))

    async def test_custom_proxy_is_registered_and_used_instead_of_automatic_pool_choice(self):
        selected_url = "socks5h://synthetic:secret@residential.invalid:1080"
        self.refresher._proxy_url = ProfileSessionRefresher._proxy_url.__get__(self.refresher)

        receipt = await self.viewer.login.start("user@example.invalid", selected_url, "owner")

        saved = await self.db.get_token(receipt["token_id"])
        self.assertNotEqual(saved.proxy_pool_id, self.proxy_id)
        self.assertEqual(saved.proxy_url, selected_url)
        entry = await self.db.get_proxy_pool_entry(saved.proxy_pool_id)
        self.assertEqual(entry.proxy_url, selected_url)
        self.assertEqual(self.backend.launch_proxies, [selected_url] * 2)
        await self.viewer.login.cancel(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertEqual(await self.db.get_all_tokens(), [])

    async def test_cold_identity_failure_and_manual_disable_do_not_enable_account(self):
        receipt = await self.start()
        refresh = self.viewer.refresh

        async def change_identity(*args, **kwargs):
            result = await refresh(*args, **kwargs)
            self.backend.identity = "different@example.invalid"
            return result

        self.viewer.refresh = change_identity
        with self.assertRaises(ViewerError):
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        self.tokens.ensure_project_exists.assert_not_awaited()
        self.backend.identity = "user@example.invalid"

        async def manually_disable(token_id):
            await self.db.update_token(token_id, ban_reason="manual")

        self.tokens.ensure_project_exists.side_effect = manually_disable
        with self.assertRaises(ViewerError):
            await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        saved = await self.db.get_token(receipt["token_id"])
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")

    async def test_explicit_cancel_removes_owned_pending_row_and_profile(self):
        receipt = await self.start()
        profile = (await self.manager.spec("user@example.invalid")).profile_dir
        await self.viewer.login.cancel(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertIsNone(await self.db.get_token(receipt["token_id"]))
        self.assertFalse(profile.exists())

    async def test_account_delete_closes_viewer_and_removes_pending_profile_and_timer(self):
        receipt = await self.start()
        token_id = receipt["token_id"]
        profile = (await self.manager.spec("user@example.invalid")).profile_dir
        timer = self.viewer.login._sessions[receipt["session_id"]]["timer"]
        await self.tokens.delete_token(token_id)
        self.assertIsNone(await self.db.get_token(token_id))
        self.assertFalse(profile.exists())
        self.assertIsNone(self.viewer._task)
        self.assertEqual(self.viewer.login._sessions, {})
        self.assertTrue(timer.cancelling() or timer.cancelled())

    async def test_account_delete_reports_profile_cleanup_failure_and_can_retry(self):
        receipt = await self.start()
        token_id = receipt["token_id"]
        profile = (await self.manager.spec("user@example.invalid")).profile_dir
        timer = self.viewer.login._sessions[receipt["session_id"]]["timer"]
        with patch.object(
            self.refresher, "remove_profile", AsyncMock(side_effect=OSError("cleanup failed"))
        ):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                await self.tokens.delete_token(token_id)
        self.assertIsNone(self.viewer._task)
        self.assertIsNotNone(await self.db.get_token(token_id))
        self.assertTrue(profile.exists())
        self.assertIn(receipt["session_id"], self.viewer.login._sessions)
        self.assertFalse(timer.cancelling() or timer.cancelled())
        await self.tokens.delete_token(token_id)
        self.assertIsNone(await self.db.get_token(token_id))
        self.assertFalse(profile.exists())
        self.assertEqual(self.viewer.login._sessions, {})
        self.assertTrue(timer.cancelling() or timer.cancelled())

    async def test_shutdown_preserves_pending_credentials_profile_and_can_resume(self):
        receipt = await self.start()
        token_id = receipt["token_id"]
        spec = await self.manager.spec("user@example.invalid")
        protected = spec.profile_dir / "Default" / "persistent-site-state"
        protected.write_bytes(b"synthetic logged-in state")
        await self.login()
        await self.db.update_token(token_id, st="preserved-session", at="preserved-access")
        timer = self.viewer.login._sessions[receipt["session_id"]]["timer"]
        await self.viewer.close_all()
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.st, "preserved-session")
        self.assertEqual(saved.at, "preserved-access")
        self.assertFalse(saved.is_active)
        self.assertEqual(protected.read_bytes(), b"synthetic logged-in state")
        self.assertTrue(timer.cancelling() or timer.cancelled())
        self.viewer = ProfileBrowserViewer(self.tokens)
        self.addAsyncCleanup(self.viewer.close_all)
        resumed = await self.viewer.login.start("user@example.invalid", "", "new-owner")
        self.assertEqual(resumed["token_id"], token_id)
        self.assertEqual(len(await self.db.get_all_tokens()), 1)
        self.assertEqual(
            spec.fingerprint_seed, (await self.manager.spec(saved.email)).fingerprint_seed
        )
        self.assertEqual(protected.read_bytes(), b"synthetic logged-in state")
        self.assertTrue(
            (
                await self.viewer.login.complete(
                    resumed["session_id"], "new-owner", resumed["viewer_id"]
                )
            )["success"]
        )

    async def test_expiry_preserves_profile_and_resumes_with_saved_proxy(self):
        receipt = await self.start()
        token_id = receipt["token_id"]
        profile = (await self.manager.spec("user@example.invalid")).profile_dir
        protected = profile / "Default" / "persistent-site-state"
        protected.write_bytes(b"synthetic login progress")
        pending = self.viewer.login._sessions[receipt["session_id"]]
        pending["timer"].cancel()
        pending["expires_at"] = time.time() - 1
        await self.viewer.login._expire(receipt["session_id"])
        self.assertIsNotNone(await self.db.get_token(token_id))
        self.assertEqual(protected.read_bytes(), b"synthetic login progress")
        self.assertIn(receipt["session_id"], self.viewer.login._sessions)
        other_proxy = await self.db.add_proxy_pool_entry("socks5h://other.invalid:1080")
        resumed = await self.viewer.login.start(
            "user@example.invalid", "", "owner", proxy_pool_id=other_proxy
        )
        saved = await self.db.get_token(token_id)
        self.assertEqual(resumed["token_id"], token_id)
        self.assertEqual(saved.proxy_pool_id, self.proxy_id)
        self.assertNotEqual(resumed["session_id"], receipt["session_id"])
        self.assertNotIn(receipt["session_id"], self.viewer.login._sessions)
        self.assertEqual(protected.read_bytes(), b"synthetic login progress")
        await self.viewer.login.cancel(resumed["session_id"], "owner", resumed["viewer_id"])
        self.assertIsNone(await self.db.get_token(token_id))
        self.assertFalse(profile.exists())

    async def test_failed_resume_keeps_saved_pending_row_and_profile(self):
        receipt = await self.start()
        await self.viewer.login.close_all()
        profile = (await self.manager.spec("user@example.invalid")).profile_dir
        with patch.object(
            self.viewer, "_open", AsyncMock(side_effect=ViewerError(503, "synthetic"))
        ):
            with self.assertRaises(ViewerError):
                await self.start()
        self.assertIsNotNone(await self.db.get_token(receipt["token_id"]))
        self.assertTrue(profile.exists())

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
        self.assertEqual(len(self.backend.flow_reads), 3)

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

    async def wait_login_status(self, receipt, expected, *, timeout=8):
        async with asyncio.timeout(timeout):
            while True:
                status = self.viewer.login.status(
                    receipt["session_id"], "owner", receipt["viewer_id"]
                )
                if status["status"] == expected:
                    return status
                if status.get("error"):
                    self.fail(status["error"])
                await asyncio.sleep(0.05)

    async def test_anonymous_login_automatically_discovers_email_and_keeps_profile_through_cold_reopen(
        self,
    ):
        receipt = await self.viewer.login.start(owner="owner")
        self.assertEqual(receipt["email"], "")
        token_id = receipt["token_id"]
        temporary = await self.db.get_token(token_id)
        self.assertTrue(temporary.email.endswith("@local.invalid"))
        original_spec = await self.manager.spec(temporary.email)
        protected = original_spec.profile_dir / "Default" / "persistent-site-state"
        protected.write_bytes(b"synthetic manually established Google login")
        pending = self.viewer.login._sessions[receipt["session_id"]]
        monitor, timer = pending["monitor"], pending["timer"]
        await self.login()
        self.backend.flow_error = None

        state = await self.wait_login_status(receipt, "ready")

        self.assertEqual(state["email"], self.backend.identity)
        self.assertEqual(state["result"]["token_id"], token_id)
        saved = await self.db.get_token(token_id)
        self.assertEqual(saved.email, self.backend.identity)
        self.assertEqual(saved.login_account, self.backend.identity)
        self.assertTrue(saved.is_active and saved.auto_refresh_enabled)
        adopted = await self.manager.spec(saved.email, existing_only=True)
        self.assertEqual(adopted.fingerprint_seed, original_spec.fingerprint_seed)
        self.assertFalse(original_spec.profile_dir.exists())
        self.assertEqual(
            (adopted.profile_dir / "Default" / "persistent-site-state").read_bytes(),
            b"synthetic manually established Google login",
        )
        self.assertEqual(self.backend.opens, 3)
        self.assertEqual(self.backend.closed, 3)
        self.assertEqual(len(self.backend.flow_reads), 2)

        await self.tokens.delete_token(token_id)
        self.assertIsNone(await self.db.get_token(token_id))
        self.assertFalse(adopted.profile_dir.exists())
        self.assertEqual(self.viewer.login._sessions, {})
        self.assertTrue(timer.cancelling() or timer.cancelled())
        self.assertTrue(monitor.done() or monitor.cancelling())

    async def test_anonymous_monitor_leaves_google_login_form_untouched_until_session_is_ready(
        self,
    ):
        receipt = await self.viewer.login.start(owner="owner")
        token_id = receipt["token_id"]
        self.viewer._context.pages = [
            SimpleNamespace(url="https://accounts.google.com/v3/signin/challenge/pwd"),
            SimpleNamespace(url="https://flow.google.com/"),
        ]
        pending = self.viewer.login._sessions[receipt["session_id"]]
        monitor, timer = pending["monitor"], pending["timer"]
        self.backend.signed_in = False

        await asyncio.sleep(3.2)

        self.assertTrue(all(context is self.viewer._context for context in self.backend.flow_reads))
        state = self.viewer.login.status(receipt["session_id"], "owner", receipt["viewer_id"])
        self.assertEqual(state["status"], "waiting")
        self.assertTrue(state["browser_open"])
        self.assertFalse((await self.db.get_token(token_id)).is_active)
        self.viewer._context.pages = [SimpleNamespace(url="https://flow.google.com/")]
        await self.login()

        await self.wait_login_status(receipt, "ready")

        saved = await self.db.get_token(token_id)
        profile = (await self.manager.spec(saved.email, existing_only=True)).profile_dir
        await self.tokens.delete_token(token_id)
        self.assertFalse(profile.exists())
        self.assertTrue(timer.cancelling() or timer.cancelled())
        self.assertTrue(monitor.done() or monitor.cancelling())

    async def test_account_delete_cleans_successful_login_receipt_and_saved_profile(self):
        receipt = await self.start()
        token_id = receipt["token_id"]
        await self.viewer.login.complete(receipt["session_id"], "owner", receipt["viewer_id"])
        profile = (await self.manager.spec("user@example.invalid")).profile_dir
        timer = self.viewer.login._sessions[receipt["session_id"]]["timer"]

        await self.tokens.delete_token(token_id)

        self.assertIsNone(await self.db.get_token(token_id))
        self.assertFalse(profile.exists())
        self.assertEqual(self.viewer.login._sessions, {})
        self.assertTrue(timer.cancelling() or timer.cancelled())


if __name__ == "__main__":
    unittest.main()
