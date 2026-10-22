import asyncio
import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.services.browser.cloak_backend import CloakBrowserBackend
from src.services.browser.profile_session_refresher import ProfileSessionRefresher
from src.services.browser.profile_viewer import ProfileBrowserViewer, ViewerError
from src.services.browser.profiles import ProfileManager, ProfileNotReadyError


class ViewerBackend:
    def __init__(self):
        self.options = {"headless": False}
        self.page = SimpleNamespace(goto=AsyncMock())
        self.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        self.open = AsyncMock(return_value=self.context)
        self.close = AsyncMock()


class ProfileViewerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.enabled = patch.dict(os.environ, {"LAY4FLOW_PROFILE_VIEWER_ENABLED": "1"})
        self.enabled.start()
        self.addCleanup(self.enabled.stop)
        self.backend = ViewerBackend()
        self.manager = ProfileManager(self.root.name, self.backend)
        self.token = SimpleNamespace(id=1, email="user@example.com", browser_profile_source="cloak")
        spec = await self.manager.spec(self.token.email)
        (spec.profile_dir / "Default").mkdir()
        self.refresher = SimpleNamespace(
            manager=self.manager, _proxy_url=AsyncMock(return_value="http://proxy.invalid:8080")
        )
        self.token_manager = SimpleNamespace(
            db=SimpleNamespace(get_token=AsyncMock(return_value=self.token)),
            profile_refresher=self.refresher,
        )
        self.viewer = ProfileBrowserViewer(self.token_manager)
        self.addAsyncCleanup(self.viewer.close_all)

    def enable_live_refresh(self, refresh):
        self.token.at_expires = None
        self.token.credits = 17
        self.token.refresh_status = "ready"
        self.token_manager._refresh_at = AsyncMock(side_effect=refresh)
        self.token_manager._refresh_futures = {}
        self.refresher = ProfileSessionRefresher(
            self.manager,
            SimpleNamespace(verify_environment=AsyncMock(return_value="environment_stable")),
            self.token_manager,
        )
        self.refresher._proxy_url = AsyncMock(return_value="http://proxy.invalid:8080")
        self.token_manager.profile_refresher = self.refresher

    async def test_live_refresh_uses_normal_pipeline_without_reopening_browser(self):
        async def refresh(token_id, **kwargs):
            self.assertTrue(self.refresher.has_active_context(self.token.email))
            self.assertIs(self.viewer._context, self.backend.context)
            return True

        self.enable_live_refresh(refresh)
        await self.viewer.open(1, "admin")
        result = await self.viewer.refresh(1, "admin")
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["credits"], 17)
        self.assertNotIn("at", result)
        self.assertTrue(self.viewer.owns(1, "admin"))
        self.assertFalse(self.refresher.has_active_context(self.token.email))
        self.token_manager._refresh_at.assert_awaited_once_with(1, force=True, refresh_st=True)
        self.backend.open.assert_awaited_once()
        self.backend.close.assert_not_awaited()

    async def test_background_refresh_borrows_open_context(self):
        self.enable_live_refresh(lambda *args, **kwargs: True)
        self.viewer = ProfileBrowserViewer(self.token_manager)
        self.addAsyncCleanup(self.viewer.close_all)
        self.assertEqual(self.refresher.active_context_provider, self.viewer.borrow_context)
        self.refresher.refresh_in_context = AsyncMock(return_value={"session_token": "synthetic"})
        self.manager.credential_session = Mock(
            side_effect=AssertionError("opened a second Profile")
        )
        await self.viewer.open(1, "admin")
        self.assertEqual(
            await asyncio.create_task(self.refresher.refresh(self.token)),
            {"session_token": "synthetic"},
        )
        self.assertFalse(self.refresher.has_active_context(self.token.email))
        self.refresher.refresh_in_context.assert_awaited_once_with(self.token, self.backend.context)
        self.manager.credential_session.assert_not_called()
        self.backend.open.assert_awaited_once()

    async def test_login_refresh_passes_existing_session_intent_without_changing_manual_default(
        self,
    ):
        async def refresh(token_id, **kwargs):
            active = self.refresher._active_context.get()
            self.assertTrue(active[2])
            return True

        self.enable_live_refresh(refresh)
        await self.viewer.open(1, "admin")
        result = await self.viewer.refresh(1, "admin", accept_existing_session=True)
        self.assertTrue(result["success"])
        self.assertFalse(self.refresher.has_active_context(self.token.email))
        self.token_manager._refresh_at.assert_awaited_once_with(1, force=True, refresh_st=True)
        self.backend.open.assert_awaited_once()
        self.backend.close.assert_not_awaited()

    async def test_background_context_lease_waits_before_close_and_ttl(self):
        for end in ("close", "ttl"):
            with self.subTest(end=end):
                await self.viewer.close_all()
                self.enable_live_refresh(lambda *args, **kwargs: True)
                self.viewer = ProfileBrowserViewer(self.token_manager)
                self.addAsyncCleanup(self.viewer.close_all)
                self.viewer.ttl_seconds = 0.03 if end == "ttl" else 1800
                entered, release = asyncio.Event(), asyncio.Event()

                async def refresh(token, context):
                    self.assertIs(context, self.backend.context)
                    entered.set()
                    await release.wait()
                    return {"session_token": "synthetic"}

                self.refresher.refresh_in_context = refresh
                await self.viewer.open(1, "admin")
                operation = asyncio.create_task(self.refresher.refresh(self.token))
                await entered.wait()
                if end == "ttl":
                    await asyncio.sleep(0.05)
                    closing = self.viewer._task
                else:
                    closing = asyncio.create_task(self.viewer.close(1, "admin"))
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                closed_before = self.backend.close.await_count
                release.set()
                self.assertEqual(await operation, {"session_token": "synthetic"})
                await asyncio.wait_for(closing, 1)
                await asyncio.sleep(0)
                self.assertEqual(self.backend.close.await_count, closed_before + 1)
                self.assertEqual(self.viewer.state(1, "admin")["status"], "closed")

    async def test_context_lease_checks_config_and_releases_guard_without_match(self):
        async with self.viewer.borrow_context(self.token) as context:
            self.assertIsNone(context)
            self.assertFalse(self.viewer._guard.locked())
        await self.viewer.open(1, "admin")
        for changes in (
            {"email": "changed@example.com"},
            {"browser_profile_source": "cookies"},
        ):
            with self.subTest(changes=changes):
                token = SimpleNamespace(**{**vars(self.token), **changes})
                with self.assertRaises(ProfileNotReadyError) as caught:
                    async with self.viewer.borrow_context(token):
                        self.fail("configuration mismatch borrowed active context")
                self.assertEqual(caught.exception.reason, "transient_error")
        self.refresher._proxy_url.return_value = "http://changed.invalid:8080"
        with self.assertRaises(ProfileNotReadyError):
            async with self.viewer.borrow_context(self.token):
                self.fail("proxy mismatch borrowed active context")
        self.refresher._proxy_url.return_value = "http://proxy.invalid:8080"
        other_token = SimpleNamespace(
            id=2, email="other@example.com", browser_profile_source="cloak"
        )
        async with self.viewer.borrow_context(other_token) as context:
            self.assertIsNone(context)
            self.assertFalse(self.viewer._guard.locked())
        self.viewer._status = "closing"
        with self.assertRaises(ProfileNotReadyError):
            async with self.viewer.borrow_context(self.token):
                self.fail("closing context was borrowed")
        self.viewer._status = "ready"

    async def test_live_refresh_owner_config_and_pending_refresh_are_checked(self):
        self.enable_live_refresh(lambda *args, **kwargs: True)
        await self.viewer.open(1, "admin")
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.refresh(1, "other")
        self.assertEqual(caught.exception.status, 403)
        self.token.email = "changed@example.com"
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.refresh(1, "admin")
        self.assertEqual(caught.exception.status, 409)
        self.token.email = "user@example.com"
        self.refresher._proxy_url.return_value = "http://changed.invalid:8080"
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.refresh(1, "admin")
        self.assertEqual(caught.exception.status, 409)
        self.refresher._proxy_url.return_value = "http://proxy.invalid:8080"
        pending = asyncio.get_running_loop().create_future()
        self.token_manager._refresh_futures[1] = pending
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.refresh(1, "admin")
        self.assertEqual(caught.exception.status, 409)
        pending.cancel()
        self.token_manager._refresh_at.assert_not_awaited()

    async def test_live_refresh_close_and_ttl_wait_for_pipeline(self):
        for end in ("close", "ttl", "request_cancel"):
            with self.subTest(end=end):
                if end != "close":
                    await self.viewer.close_all()
                    self.viewer = ProfileBrowserViewer(self.token_manager)
                entered, release = asyncio.Event(), asyncio.Event()

                async def refresh(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return True

                self.enable_live_refresh(refresh)
                self.viewer.ttl_seconds = 0.03 if end == "ttl" else 1800
                await self.viewer.open(1, "admin")
                operation = asyncio.create_task(self.viewer.refresh(1, "admin"))
                await entered.wait()
                if end == "request_cancel":
                    operation.cancel()
                if end == "ttl":
                    await asyncio.sleep(0.05)
                    closing = self.viewer._task
                else:
                    closing = asyncio.create_task(self.viewer.close(1, "admin"))
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertIs(self.viewer._context, self.backend.context)
                closed_before = self.backend.close.await_count
                release.set()
                if end == "request_cancel":
                    with self.assertRaises(asyncio.CancelledError):
                        await operation
                else:
                    self.assertTrue((await operation)["success"])
                await asyncio.wait_for(closing, 1)
                await asyncio.sleep(0)
                self.assertEqual(self.backend.close.await_count, closed_before + 1)
                self.assertIsNone(self.viewer._context)
                self.assertEqual(self.viewer.state(1, "admin")["status"], "closed")

    async def test_open_reuses_profile_holds_manager_lock_and_closes(self):
        state = await self.viewer.open(1, "admin")
        self.assertEqual(state["status"], "ready")
        self.assertTrue(self.viewer.owns(1, "admin"))
        self.assertFalse(self.viewer.owns(1, "other"))
        self.backend.open.assert_awaited_once_with(
            self.manager.root_dir / self.manager._normalize_account_key(self.token.email),
            proxy_url="http://proxy.invalid:8080",
        )
        self.backend.page.goto.assert_awaited_once_with(
            "https://flow.google.com/", wait_until="domcontentloaded", timeout=30000
        )
        entered = asyncio.Event()

        async def competing_refresh():
            async with self.manager.session(self.token.email):
                entered.set()

        competing = asyncio.create_task(competing_refresh())
        await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        self.assertEqual((await self.viewer.close(1, "admin"))["status"], "closed")
        await asyncio.wait_for(competing, 1)
        self.assertTrue(entered.is_set())
        self.assertEqual(self.backend.close.await_count, 2)
        self.assertEqual(self.manager._capacity._value, 1)

    async def test_concurrent_same_owner_shares_startup_other_owner_is_busy(self):
        navigating = asyncio.Event()
        release = asyncio.Event()

        async def navigate(*args, **kwargs):
            navigating.set()
            await release.wait()

        self.backend.page.goto.side_effect = navigate
        first = asyncio.create_task(self.viewer.open(1, "admin"))
        await navigating.wait()
        repeated = asyncio.create_task(self.viewer.open(1, "admin"))
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.open(1, "other")
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.open(2, "admin")
        self.assertEqual(caught.exception.status, 404)
        release.set()
        self.assertEqual(await first, await repeated)
        self.backend.open.assert_awaited_once()

    async def test_owner_is_required_for_state_and_close(self):
        await self.viewer.open(1, "admin")
        with self.assertRaises(ViewerError) as caught:
            self.viewer.state(1, "other")
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.close(1, "other")
        self.assertEqual(caught.exception.status, 403)
        self.assertTrue(self.viewer.owns(1, "admin"))

    async def test_switch_saves_old_profile_and_releases_old_stream_before_opening_next(self):
        second = SimpleNamespace(id=2, email="second@example.com", browser_profile_source="cloak")
        spec = await self.manager.spec(second.email)
        (spec.profile_dir / "Default").mkdir()
        self.token_manager.db.get_token.side_effect = lambda token_id: (
            self.token if token_id == 1 else second
        )
        first_state = await self.viewer.open(1, "admin")
        old_id = first_state["viewer_id"]
        stream_entered, stream_stopped = asyncio.Event(), asyncio.Event()

        async def old_stream():
            async with self.viewer.stream_session(1, "admin", old_id) as stop:
                stream_entered.set()
                await stop.wait()
                stream_stopped.set()

        streaming = asyncio.create_task(old_stream())
        await stream_entered.wait()
        state = await self.viewer.open(2, "admin")
        await streaming
        self.assertTrue(stream_stopped.is_set())
        self.assertNotEqual(state["viewer_id"], old_id)
        self.assertTrue(self.viewer.owns(2, "admin"))
        self.assertFalse(self.viewer.owns(1, "admin"))
        self.assertEqual(self.backend.close.await_count, 1)
        self.assertEqual(self.backend.open.await_count, 2)
        self.assertEqual(self.manager._capacity._value, 1)
        self.assertEqual((await self.viewer.close(1, "admin", old_id))["status"], "closed")
        self.assertTrue(self.viewer.owns(2, "admin"))

    async def test_old_viewer_id_cannot_close_or_refresh_reopened_same_account(self):
        old_id = (await self.viewer.open(1, "admin"))["viewer_id"]
        await self.viewer.close(1, "admin")
        new_id = (await self.viewer.open(1, "admin"))["viewer_id"]
        self.assertNotEqual(old_id, new_id)
        await self.viewer.close(1, "admin", old_id)
        self.assertTrue(self.viewer.owns(1, "admin"))
        with self.assertRaises(ViewerError):
            await self.viewer.refresh(1, "admin", old_id)
        with self.assertRaises(ViewerError):
            async with self.viewer.stream_session(1, "admin", old_id):
                self.fail("stale stream was accepted")

    async def test_other_owner_or_invalid_target_cannot_replace_current_browser(self):
        await self.viewer.open(1, "admin")
        with self.assertRaises(ViewerError):
            await self.viewer.open(2, "other")
        with self.assertRaises(ViewerError):
            await self.viewer.open(2, "admin")
        self.assertTrue(self.viewer.owns(1, "admin"))
        self.backend.close.assert_not_awaited()

    async def test_new_login_opens_google_form_and_maximizes_existing_window(self):
        self.token.ban_reason = "profile_login"
        desktop = SimpleNamespace(
            send=AsyncMock(side_effect=[{"windowId": 42}, {}]), detach=AsyncMock()
        )
        self.backend.context.new_cdp_session = AsyncMock(return_value=desktop)
        await self.viewer.open(1, "admin")
        self.backend.page.goto.assert_awaited_once_with(
            "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fflow.google.com%2F",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self.assertEqual(
            desktop.send.await_args_list[1].args,
            (
                "Browser.setWindowBounds",
                {"windowId": 42, "bounds": {"windowState": "maximized"}},
            ),
        )
        desktop.detach.assert_awaited_once()
        self.backend.open.assert_awaited_once()

    async def test_window_presentation_failure_does_not_block_login(self):
        self.token.ban_reason = "profile_login"
        desktop = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError()), detach=AsyncMock())
        self.backend.context.new_cdp_session = AsyncMock(return_value=desktop)
        self.assertEqual((await self.viewer.open(1, "admin"))["status"], "ready")
        desktop.detach.assert_awaited_once()
        self.backend.page.goto.assert_awaited_once()

    async def test_native_window_reports_presentation_and_focuses_without_reopening(self):
        with patch.dict(os.environ, {"LAY4FLOW_PROFILE_PRESENTATION": "native"}):
            self.viewer = ProfileBrowserViewer(self.token_manager)
        self.addAsyncCleanup(self.viewer.close_all)
        self.backend.page.bring_to_front = AsyncMock()
        desktop = SimpleNamespace(
            send=AsyncMock(side_effect=[{"windowId": 42}, {}, {"windowId": 42}, {}]),
            detach=AsyncMock(),
        )
        self.backend.context.new_cdp_session = AsyncMock(return_value=desktop)
        opened = await self.viewer.open(1, "admin")
        self.assertEqual(opened["presentation"], "native")
        await self.viewer.focus(1, "admin", opened["viewer_id"])
        self.assertEqual(self.backend.page.bring_to_front.await_count, 2)
        self.backend.open.assert_awaited_once()
        self.backend.close.assert_not_awaited()
        with self.assertRaises(ViewerError) as wrong_owner:
            await self.viewer.focus(1, "other", opened["viewer_id"])
        self.assertEqual(wrong_owner.exception.status, 403)
        with self.assertRaises(ViewerError) as stale:
            await self.viewer.focus(1, "admin", "stale")
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(ViewerError):
            async with self.viewer.stream_session(1, "admin", opened["viewer_id"]):
                self.fail("native window was exposed through VNC")

    async def test_switch_waits_for_old_account_refresh_and_never_opens_before_close(self):
        second = SimpleNamespace(id=2, email="second@example.com", browser_profile_source="cloak")
        spec = await self.manager.spec(second.email)
        (spec.profile_dir / "Default").mkdir()
        self.token_manager.db.get_token.side_effect = lambda token_id: (
            self.token if token_id == 1 else second
        )
        self.enable_live_refresh(lambda *args, **kwargs: True)
        self.viewer = ProfileBrowserViewer(self.token_manager)
        self.addAsyncCleanup(self.viewer.close_all)
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(token, context):
            entered.set()
            await release.wait()
            return {"session_token": "synthetic"}

        self.refresher.refresh_in_context = refresh
        await self.viewer.open(1, "admin")
        refreshing = asyncio.create_task(self.refresher.refresh(self.token))
        await entered.wait()
        switching = asyncio.create_task(self.viewer.open(2, "admin"))
        await asyncio.sleep(0)
        self.assertFalse(switching.done())
        self.backend.close.assert_not_awaited()
        release.set()
        await refreshing
        self.assertEqual((await switching)["token_id"], 2)
        self.assertEqual(self.backend.close.await_count, 1)

    async def test_failed_close_prevents_switch_to_valid_second_account(self):
        second = SimpleNamespace(id=2, email="second@example.com", browser_profile_source="cloak")
        spec = await self.manager.spec(second.email)
        (spec.profile_dir / "Default").mkdir()
        self.token_manager.db.get_token.side_effect = lambda token_id: (
            self.token if token_id == 1 else second
        )
        await self.viewer.open(1, "admin")
        self.backend.close.side_effect = RuntimeError("synthetic close failure")
        with self.assertRaises(ViewerError):
            await self.viewer.open(2, "admin")
        self.backend.open.assert_awaited_once()
        self.assertEqual(self.viewer.state(1, "admin")["status"], "error")
        self.viewer._task = None
        for handle in self.manager._failed_handles.values():
            handle.file_lock.release()

    async def test_background_display_does_not_change_profile_launch_identity(self):
        import sys
        from unittest.mock import patch

        captured = []

        async def launch(*args, **options):
            captured.append(options)
            return object()

        spec = await self.manager.spec(self.token.email)
        fake = SimpleNamespace(launch_persistent_context_async=launch)
        backend = CloakBrowserBackend(headless=False)
        with (
            patch.dict(
                os.environ, {"LAY4FLOW_PROFILE_BACKGROUND_DISPLAY": ":98", "DISPLAY": ":99"}
            ),
            patch.dict(sys.modules, {"cloakbrowser": fake}),
        ):
            await backend.open_background(spec.profile_dir)
            await backend.open_interactive(spec.profile_dir)
        self.assertEqual(captured[0]["env"]["DISPLAY"], ":98")
        self.assertNotIn("env", captured[1])
        self.assertTrue(captured[0]["headless"])
        self.assertFalse(captured[1]["headless"])
        self.assertEqual(captured[0]["args"], captured[1]["args"])

    async def test_absent_profile_is_not_created(self):
        self.token.email = "absent@example.com"
        expected = self.manager.root_dir / self.manager._normalize_account_key(self.token.email)
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.open(1, "admin")
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(expected.exists())
        self.backend.open.assert_not_awaited()

    async def test_disabled_or_headless_viewer_does_not_open_browser(self):
        with patch.dict(os.environ, {"LAY4FLOW_PROFILE_VIEWER_ENABLED": "0"}):
            with self.assertRaises(ViewerError) as caught:
                await self.viewer.open(1, "admin")
            self.assertEqual(caught.exception.status, 503)
        self.backend.options["headless"] = True
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.open(1, "admin")
        self.assertEqual(caught.exception.status, 503)
        self.backend.open.assert_not_awaited()

    async def test_viewer_env_controls_interactive_windows(self):
        with patch.dict(os.environ, {"LAY4FLOW_PROFILE_VIEWER_ENABLED": "1"}):
            self.assertEqual((await self.viewer.open(1, "admin"))["status"], "ready")
            await self.viewer.close_all()
            os.environ["LAY4FLOW_PROFILE_VIEWER_ENABLED"] = "0"
            with self.assertRaises(ViewerError) as caught:
                await self.viewer.open(1, "admin")
            self.assertEqual(caught.exception.status, 503)

    async def test_background_display_is_explicit(self):
        backend = CloakBrowserBackend(headless=False)
        backend._open = AsyncMock()
        with patch.dict(os.environ, {"LAY4FLOW_PROFILE_BACKGROUND_DISPLAY": ":97"}):
            await backend.open_background(Path("synthetic-profile"))
            backend._open.assert_awaited_with(
                Path("synthetic-profile"), proxy_url=None, display=":97", headless=True
            )

    async def test_startup_failure_is_safe_and_releases_lock(self):
        self.backend.page.goto.side_effect = RuntimeError("cookie=private-token-value")
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.open(1, "admin")
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("private-token-value", str(caught.exception))
        self.backend.close.assert_awaited_once()
        self.assertEqual(self.viewer.state(1, "admin")["status"], "closed")
        self.assertEqual(self.manager._capacity._value, 1)

    async def test_close_during_startup_is_safe_and_waits_for_cleanup(self):
        navigating = asyncio.Event()

        async def navigate(*args, **kwargs):
            navigating.set()
            await asyncio.Event().wait()

        self.backend.page.goto.side_effect = navigate
        opened = asyncio.create_task(self.viewer.open(1, "admin"))
        await navigating.wait()
        await self.viewer.close(1, "admin")
        with self.assertRaises(ViewerError) as caught:
            await opened
        self.assertEqual(caught.exception.status, 409)
        self.backend.close.assert_awaited_once()
        self.assertEqual(self.manager._capacity._value, 1)

    async def test_ttl_closes_without_credential_updates(self):
        self.viewer.ttl_seconds = 0.02
        await self.viewer.open(1, "admin")
        await asyncio.wait_for(asyncio.shield(self.viewer._task), 1)
        self.assertEqual(self.viewer.state(1, "admin")["status"], "closed")
        self.backend.close.assert_awaited_once()
        self.assertEqual(set(vars(self.token)), {"id", "email", "browser_profile_source"})

    async def test_close_failure_is_safe_and_reserves_shared_slot(self):
        self.backend.close.side_effect = RuntimeError("Authorization=private-secret")
        await self.viewer.open(1, "admin")
        with self.assertRaises(ViewerError) as caught:
            await self.viewer.close(1, "admin")
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertEqual(self.viewer.state(1, "admin")["status"], "error")
        with self.assertRaises(ViewerError) as busy:
            await self.viewer.open(2, "admin")
        self.assertEqual(busy.exception.status, 409)
        # The fake backend raises permanently; the production shutdown caller
        # receives the same safe error and the slot stays reserved.
        self.viewer._task = None
        self.assertTrue(self.manager._failed_handles)
        for handle in self.manager._failed_handles.values():
            handle.file_lock.release()  # Synthetic backend has no live process to retain.

    async def test_close_before_start_task_runs_clears_pending_session(self):
        opened = asyncio.create_task(self.viewer.open(1, "admin"))
        await asyncio.sleep(0)
        await self.viewer.close(1, "admin")
        with self.assertRaises(ViewerError) as caught:
            await opened
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.viewer.state(1, "admin")["status"], "closed")

    async def test_close_while_backend_opens_waits_for_entry_and_cleanup(self):
        opening = asyncio.Event()
        release = asyncio.Event()

        async def launch(*args, **kwargs):
            opening.set()
            await release.wait()
            return self.backend.context

        self.backend.open.side_effect = launch
        opened = asyncio.create_task(self.viewer.open(1, "admin"))
        await opening.wait()
        closed = asyncio.create_task(self.viewer.close(1, "admin"))
        await asyncio.sleep(0)
        self.assertFalse(closed.done())
        release.set()
        await closed
        with self.assertRaises(ViewerError):
            await opened
        self.backend.page.goto.assert_not_awaited()
        self.backend.close.assert_awaited_once()
        self.assertEqual(self.manager._capacity._value, 1)

    async def test_shutdown_closes_active_browser(self):
        await self.viewer.open(1, "admin")
        await self.viewer.close_all()
        await self.viewer.close_all()
        self.backend.close.assert_awaited_once()
        self.assertFalse(self.viewer.owns(1, "admin"))
        with self.assertRaises(ViewerError):
            await self.viewer.open(1, "admin")


class ProfilePresentationSettingsTests(unittest.TestCase):
    def test_socks5h_summary_migrates_only_the_same_proxy_endpoint(self):
        options = {
            "headless": False,
            "stealth_args": False,
            "proxy": "socks5://relay.invalid:1080",
            "args": ["--fingerprint=12345"],
        }
        with TemporaryDirectory() as root:
            path = Path(root)
            settings = path / "launch_settings.sha256"
            legacy = {key: value for key, value in options.items() if key != "headless"}
            legacy["proxy"] = "socks5h://relay.invalid:1080"
            settings.write_text(
                hashlib.sha256(
                    json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            CloakBrowserBackend._check_launch_settings(path, options)
            migrated = settings.read_text()
            CloakBrowserBackend._check_launch_settings(path, options)
            self.assertEqual(settings.read_text(), migrated)
            with self.assertRaisesRegex(ValueError, "settings changed"):
                CloakBrowserBackend._check_launch_settings(
                    path, {**options, "proxy": "socks5://another.invalid:1080"}
                )
            self.assertEqual(settings.read_text(), migrated)

    def test_cloak_browser_converts_remote_dns_proxy_scheme(self):
        backend = CloakBrowserBackend(headless=False)
        with TemporaryDirectory() as root:
            path = Path(root)
            (path / "metadata.json").write_text(json.dumps({"fingerprint_seed": 12345}))
            options = backend._launch_options(path, "socks5h://proxy.invalid:1080")
            self.assertEqual(options["proxy"], "socks5://proxy.invalid:1080")

    def test_headless_change_migrates_old_digest_and_all_other_options_stay_fixed(self):
        options = {
            "headless": True,
            "stealth_args": False,
            "locale": "zh-CN",
            "user_agent": "synthetic-UA",
            "proxy": "http://user:synthetic-password@proxy.invalid:8080",
            "args": ["--fingerprint=12345"],
        }
        for legacy_headless in (True, False):
            with self.subTest(legacy_headless=legacy_headless), TemporaryDirectory() as root:
                settings = Path(root) / "launch_settings.sha256"
                legacy = {**options, "headless": legacy_headless}
                settings.write_text(
                    hashlib.sha256(
                        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    encoding="utf-8",
                )
                CloakBrowserBackend._check_launch_settings(Path(root), options)
                canonical = settings.read_text(encoding="utf-8")
                CloakBrowserBackend._check_launch_settings(
                    Path(root), {**options, "headless": False}
                )
                self.assertEqual(settings.read_text(encoding="utf-8"), canonical)
                self.assertNotIn("synthetic-password", canonical)
                for key, value in (
                    ("stealth_args", True),
                    ("locale", "en-US"),
                    ("user_agent", "another-UA"),
                    ("proxy", "http://another.invalid:8080"),
                    ("args", ["--fingerprint=54321"]),
                ):
                    with (
                        self.subTest(key=key),
                        self.assertRaisesRegex(ValueError, "settings changed"),
                    ):
                        CloakBrowserBackend._check_launch_settings(
                            Path(root), {**options, key: value}
                        )
                    self.assertEqual(settings.read_text(encoding="utf-8"), canonical)


if __name__ == "__main__":
    unittest.main()
