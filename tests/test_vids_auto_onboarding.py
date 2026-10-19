"""Automatic onboarding never retries document creation or blocks Flow work."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.token_manager import TokenManager
from src.services.vids.auto_onboarding import VidsAutoOnboarding, read_auto_onboarding_state
from src.services.vids.bindings import write_bindings
from src.services.vids.gateway import GatewayError


class VidsAutoOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = SimpleNamespace(
            id=1,
            email="synthetic@example.test",
            is_active=True,
            image_enabled=True,
            video_enabled=True,
            browser_profile_source="cloak",
            browser_profile_status="ready",
            browser_profile_verified_at=datetime.now(timezone.utc),
        )
        self.profile_lock = asyncio.Lock()
        self.service = SimpleNamespace(
            db=SimpleNamespace(get_token=AsyncMock(side_effect=lambda _: self.token)),
            registry_path=Path(self.directory.name) / "bindings.json",
            runtime=SimpleNamespace(
                _started=True,
                last_error_code=None,
                _lifecycle_lock=asyncio.Lock(),
                dispatcher=SimpleNamespace(_accounts={}),
            ),
            profile_manager=SimpleNamespace(
                _account_lock=AsyncMock(return_value=self.profile_lock),
                _normalize_account_key=lambda email: email.casefold(),
            ),
            _lock=asyncio.Lock(),
            onboard=AsyncMock(return_value={"state": "ready"}),
            reconnect=AsyncMock(return_value={"state": "ready"}),
        )
        self.automatic = VidsAutoOnboarding(self.service)
        self.addAsyncCleanup(self.automatic.close)

    async def drain(self, automatic=None):
        await asyncio.gather(*(automatic or self.automatic)._tasks.values())
        await asyncio.sleep(0)

    async def test_duplicate_events_share_one_attempt_and_failure_survives_restart(self):
        self.service.onboard.side_effect = RuntimeError("private cookie/url")
        self.automatic.schedule(1)
        self.automatic.schedule(1)
        await self.drain()
        self.service.onboard.assert_awaited_once_with(1, create_blank=True)
        marker = self.service.registry_path.parent / "auto-onboarding" / "1"
        self.assertEqual(
            json.loads(marker.read_bytes()), {"state": "failed", "code": "onboarding_failed"}
        )
        self.assertNotIn("private", marker.read_text())
        self.assertEqual(
            read_auto_onboarding_state(self.service.registry_path, 1),
            {"state": "failed", "code": "onboarding_failed"},
        )
        self.automatic.schedule(1)
        await self.drain()
        restarted = VidsAutoOnboarding(self.service)
        self.addAsyncCleanup(restarted.close)
        restarted.schedule(1)
        await self.drain(restarted)
        self.service.onboard.assert_awaited_once()

    async def test_disabled_unverified_and_non_profile_accounts_never_open_vids(self):
        for field, value in (
            ("is_active", False),
            ("browser_profile_source", "cookies"),
            ("browser_profile_status", "login_required"),
            ("browser_profile_verified_at", None),
        ):
            original = getattr(self.token, field)
            setattr(self.token, field, value)
            self.automatic.schedule(1)
            await self.drain()
            setattr(self.token, field, original)
        self.token.image_enabled = self.token.video_enabled = False
        self.automatic.schedule(1)
        await self.drain()
        self.service.onboard.assert_not_awaited()
        self.assertFalse((self.service.registry_path.parent / "auto-onboarding").exists())

    async def test_multiple_imports_connect_serially_without_losing_second_account(self):
        active = 0
        maximum = 0

        async def onboard(*args, **kwargs):
            nonlocal active, maximum
            async with self.service._lock:
                async with self.service.runtime._lifecycle_lock:
                    active += 1
                    maximum = max(maximum, active)
                    await asyncio.sleep(0)
                    active -= 1

        self.service.onboard.side_effect = onboard
        self.automatic.schedule(1)
        self.automatic.schedule(2)
        await self.drain()
        self.assertEqual(self.service.onboard.await_count, 2)
        self.assertEqual(maximum, 1)

    async def test_existing_binding_and_busy_profile_do_not_consume_attempt(self):
        self.service.runtime.dispatcher._accounts[1] = object()
        self.automatic.schedule(1)
        await self.drain()
        self.service.runtime.dispatcher._accounts.clear()
        write_bindings(self.service.registry_path, [{"token_id": 1}])
        self.automatic.schedule(1)
        await self.drain()
        write_bindings(self.service.registry_path, [])
        await self.profile_lock.acquire()
        self.automatic.schedule(1)
        await self.drain()
        self.profile_lock.release()
        self.service.onboard.assert_not_awaited()
        self.automatic.schedule(1)
        await self.drain()
        self.service.onboard.assert_awaited_once_with(1, create_blank=True)

    async def test_runtime_busy_skips_and_corrupt_registry_fails_closed(self):
        await self.service.runtime._lifecycle_lock.acquire()
        self.automatic.schedule(1)
        await self.drain()
        self.service.runtime._lifecycle_lock.release()
        self.service.registry_path.write_text("not-json", encoding="utf-8")
        self.automatic.schedule(1)
        await self.drain()
        self.service.onboard.assert_not_awaited()
        self.assertFalse((self.service.registry_path.parent / "auto-onboarding").exists())

    async def test_shutdown_cancels_and_drains_attempt_before_provider_shutdown(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def pending(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        self.service.onboard.side_effect = pending
        self.automatic.schedule(1)
        await started.wait()
        await self.automatic.close()
        self.assertTrue(finished.is_set())
        self.assertFalse(self.automatic._tasks)
        self.automatic.schedule(2)
        self.assertFalse(self.automatic._tasks)
        self.assertEqual(
            read_auto_onboarding_state(self.service.registry_path, 1),
            {"state": "unknown", "code": "connection_unknown"},
        )

    async def test_safe_failure_code_and_legacy_malformed_marker_cannot_leak_private_values(self):
        self.service.onboard.side_effect = GatewayError(
            409, "identity_mismatch", "private cookie/url"
        )
        self.automatic.schedule(1)
        await self.drain()
        self.assertEqual(
            read_auto_onboarding_state(self.service.registry_path, 1),
            {"state": "failed", "code": "identity_mismatch"},
        )
        marker = self.service.registry_path.parent / "auto-onboarding" / "1"
        for value in (
            "",
            "private-cookie",
            '{"state":"failed","code":"private-cookie"}',
            '{"state":"failed","code":[]}',
        ):
            marker.write_text(value)
            self.assertEqual(
                read_auto_onboarding_state(self.service.registry_path, 1),
                {"state": "unknown", "code": "connection_unknown"},
            )
        self.assertEqual(
            read_auto_onboarding_state(self.service.registry_path, 2),
            {"state": "unbound", "code": None},
        )

    async def test_configured_startup_auth_failure_reconnects_same_account_once_per_event(self):
        self.service.get_settings = lambda: {"accounts": [{"token_id": 1}]}
        self.service.runtime._started = False
        self.service.runtime.last_error_code = "session_authentication_failed"
        self.automatic.schedule(1)
        self.automatic.schedule(1)
        await self.drain()
        self.service.reconnect.assert_awaited_once_with(1)
        self.service.onboard.assert_not_awaited()
        self.assertFalse((self.service.registry_path.parent / "auto-onboarding").exists())
        self.service.runtime.last_error_code = "invalid_configuration"
        self.automatic.schedule(1)
        await self.drain()
        self.service.reconnect.assert_awaited_once()

    async def test_valid_bound_skips_and_invalid_bound_reconnects_without_generation_marker(self):
        gateway = SimpleNamespace(
            _ready=True,
            client=SimpleNamespace(initialization_valid=True),
            lock=asyncio.Lock(),
        )
        self.service.runtime.dispatcher._accounts[1] = SimpleNamespace(gateway=gateway)
        self.automatic.schedule(1)
        await self.drain()
        self.service.reconnect.assert_not_awaited()
        gateway.client.initialization_valid = False
        self.service.reconnect.side_effect = GatewayError(
            409, "login_required", "private cookie/url"
        )
        self.automatic.schedule(1)
        await self.drain()
        self.service.reconnect.assert_awaited_once_with(1)
        self.service.onboard.assert_not_awaited()

    async def test_failed_startup_account_repairs_after_profile_ready_without_touching_healthy_peer(
        self,
    ):
        self.service.get_settings = lambda: {"accounts": [{"token_id": 1}, {"token_id": 2}]}
        healthy = object()
        self.service.runtime.dispatcher._accounts[2] = healthy
        self.service.runtime.account_errors = {1: "session_authentication_failed"}
        self.automatic.schedule(1)
        self.automatic.schedule(1)
        await self.drain()
        self.service.reconnect.assert_awaited_once_with(1)
        self.service.onboard.assert_not_awaited()
        self.assertIs(self.service.runtime.dispatcher._accounts[2], healthy)
        self.assertFalse((self.service.registry_path.parent / "auto-onboarding").exists())
        await asyncio.sleep(0)
        self.service.reconnect.assert_awaited_once()

    async def test_profile_callback_is_nonblocking_and_errors_cannot_fail_flow(self):
        manager = TokenManager(self.service.db, SimpleNamespace())
        manager.profile_ready_callback = self.automatic.schedule
        manager.notify_profile_ready(1)
        self.service.onboard.assert_not_awaited()
        await self.drain()
        self.service.onboard.assert_awaited_once_with(1, create_blank=True)

        def broken_callback(token_id):
            raise RuntimeError("private cookie/url")

        manager.profile_ready_callback = broken_callback
        manager.notify_profile_ready(1)
