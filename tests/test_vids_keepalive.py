import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from src.services.vids.client import VidsProtocolError, VidsTransportError, cookie_jar
from src.services.vids.dispatcher import VidsAccount, VidsProviderDispatcher
from src.services.vids.gateway import GatewayError, VidsGateway
from src.services.vids.keepalive import VidsKeepalive
from src.services.vids.session_store import SessionStore
from tests.test_vids_persistence_gateway import client, cookies


class VidsKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_protection_and_disabled_capabilities_skip_network(self):
        self.dispatcher.account_policy = SimpleNamespace(
            availability=AsyncMock(return_value="synthetic protection")
        )
        await self.due()
        self.upstream.bootstrap.assert_not_awaited()
        self.repair.assert_not_awaited()
        self.dispatcher.account_policy = None
        self.tokens[1].image_enabled = self.tokens[1].video_enabled = False
        self.advance(100)
        await self.worker.tick()
        self.upstream.bootstrap.assert_not_awaited()
        self.repair.assert_not_awaited()

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.elapsed = 100.0
        self.epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.tokens = {
            1: SimpleNamespace(
                id=1,
                email="synthetic@example.test",
                is_active=True,
                image_enabled=True,
                video_enabled=True,
                refresh_status="ready",
            )
        }
        self.db = SimpleNamespace(get_token=AsyncMock(side_effect=self.tokens.get))
        self.dispatcher = VidsProviderDispatcher(self.db)
        self.upstream = client()
        self.upstream.initialization_valid = True
        self.store = SessionStore(self.root, Fernet.generate_key())
        self.gateway = VidsGateway(self.upstream, self.root, session_store=self.store)
        await self.gateway.start()
        self.identity = hashlib.sha256(b"same-account").hexdigest()
        self.account = VidsAccount(1, self.gateway, self.identity)
        self.dispatcher.register(self.account)
        self.runtime = SimpleNamespace(
            dispatcher=self.dispatcher,
            binding_rows={1: {"token_id": 1}},
            account_errors={},
            _started=True,
            _lifecycle_lock=asyncio.Lock(),
        )
        self.repair = AsyncMock()
        self.worker = VidsKeepalive(
            self.runtime,
            self.repair,
            interval=100,
            retry_base=10,
            retry_max=40,
            busy_interval=5,
            monotonic=lambda: self.elapsed,
            utcnow=lambda: self.epoch + timedelta(seconds=self.elapsed),
        )
        self.upstream.bootstrap.reset_mock()
        self.upstream.quota.reset_mock()

    async def asyncTearDown(self):
        await self.worker.close()
        await self.gateway.close()
        self.directory.cleanup()

    def advance(self, seconds):
        self.elapsed += seconds

    async def due(self):
        await self.worker.tick()
        self.advance(100)
        await self.worker.tick()

    async def test_initial_interval_then_free_probe_persists_rotation_without_generation(self):
        await self.worker.tick()
        self.upstream.bootstrap.assert_not_awaited()
        self.assertEqual(self.worker.snapshot(1)["state"], "scheduled")
        self.assertIsNotNone(self.worker.snapshot(1)["next_attempt_at"])

        async def bootstrap():
            self.upstream.session.cookies.jar = cookie_jar(cookies("synthetic-rotation"))

        self.upstream.bootstrap.side_effect = bootstrap
        self.advance(100)
        await self.worker.tick()
        self.upstream.bootstrap.assert_awaited_once()
        self.upstream.quota.assert_awaited_once_with()
        self.upstream.generate_speech.assert_not_awaited()
        self.repair.assert_not_awaited()
        self.assertEqual(self.store.load()["cookies"][0]["value"], "synthetic-rotation")
        value = self.worker.snapshot(1)
        self.assertEqual(value["state"], "ready")
        self.assertEqual(value["consecutive_failures"], 0)
        self.assertEqual(value["last_attempt_at"], value["last_success_at"])
        self.assertIsNone(value["error"])
        value["error"] = "caller mutation"
        self.assertIsNone(self.worker.snapshot(1)["error"])
        await self.worker.tick()
        self.upstream.bootstrap.assert_awaited_once()

    async def test_unbound_account_is_not_onboarded_and_removed_binding_drops_diagnostics(self):
        self.tokens[2] = SimpleNamespace(
            id=2, is_active=True, image_enabled=True, video_enabled=True
        )
        await self.due()
        self.assertEqual(self.worker.snapshot(2)["state"], "unbound")
        self.repair.assert_not_awaited()
        self.runtime.binding_rows.clear()
        self.dispatcher._accounts.clear()
        await self.worker.tick()
        self.assertEqual(self.worker.snapshot(1)["state"], "unbound")
        self.assertEqual(self.worker._observations, {})

    async def test_manual_disable_and_missing_token_skip_without_network_or_reenable(self):
        self.tokens[1].is_active = False
        await self.due()
        self.upstream.bootstrap.assert_not_awaited()
        self.repair.assert_not_awaited()
        self.assertEqual(self.worker.snapshot(1)["state"], "disabled")
        self.assertIsNone(self.worker.snapshot(1)["last_attempt_at"])
        self.assertIsNone(self.worker.snapshot(1)["next_attempt_at"])
        self.assertFalse(self.tokens[1].is_active)
        self.tokens.pop(1)
        self.advance(5)
        await self.worker.tick()
        self.assertEqual(self.worker.snapshot(1)["state"], "missing")
        self.assertEqual(self.worker.snapshot(1)["consecutive_failures"], 0)

    async def test_reenabled_account_becomes_eligible_on_next_scan(self):
        self.tokens[1].is_active = False
        await self.due()
        self.tokens[1].is_active = True
        self.advance(5)
        await self.worker.tick()
        self.upstream.bootstrap.assert_awaited_once()
        self.assertEqual(self.worker.snapshot(1)["state"], "ready")

    async def test_busy_gateway_preflight_lifecycle_profile_pending_and_refresh_skip(self):
        await self.worker.tick()
        self.advance(100)
        profile_lock = asyncio.Lock()
        manager = SimpleNamespace(
            _normalize_account_key=lambda email: email,
            _account_locks={self.tokens[1].email: profile_lock},
        )
        self.worker.profile_manager = manager
        preflight = self.dispatcher._preflight_locks.setdefault(1, asyncio.Lock())
        for lock in (self.gateway.lock, preflight, self.runtime._lifecycle_lock, profile_lock):
            with self.subTest(lock=lock):
                async with lock:
                    await self.worker.tick()
                self.assertEqual(self.worker.snapshot(1)["state"], "busy")
                self.assertEqual(self.worker.snapshot(1)["consecutive_failures"], 0)
                self.advance(5)
        self.dispatcher._pool_pending.add(1)
        await self.worker.tick()
        self.dispatcher._pool_pending.clear()
        self.advance(5)
        self.tokens[1].refresh_status = "refreshing"
        await self.worker.tick()
        self.assertEqual(self.worker.snapshot(1)["state"], "busy")
        self.upstream.bootstrap.assert_not_awaited()
        self.repair.assert_not_awaited()

    async def test_failures_back_off_capped_and_success_resets_diagnostics(self):
        self.upstream.bootstrap.side_effect = VidsTransportError("private-secret-url")
        await self.due()
        for delay in (10, 20, 40, 40):
            value = self.worker.snapshot(1)
            self.assertEqual(value["state"], "backoff")
            self.assertEqual(value["error"], "transport_failed")
            self.assertNotIn("private", str(value))
            calls = self.upstream.bootstrap.await_count
            self.advance(delay - 1)
            await self.worker.tick()
            self.assertEqual(self.upstream.bootstrap.await_count, calls)
            self.advance(1)
            await self.worker.tick()
        self.upstream.bootstrap.side_effect = None
        self.advance(40)
        await self.worker.tick()
        value = self.worker.snapshot(1)
        self.assertEqual(value["state"], "ready")
        self.assertEqual(value["consecutive_failures"], 0)
        self.assertIsNone(value["error"])
        self.repair.assert_not_awaited()

    async def test_authentication_failure_repairs_once_without_replaying_any_receipt(self):
        receipt = self.root / ("vids_" + "a" * 64 + ".json")
        receipt.write_text('{"status":"unknown"}', encoding="utf-8")
        self.upstream.quota.side_effect = VidsProtocolError("private-url", 401)

        async def repair(token_id):
            self.assertEqual(token_id, 1)
            self.assertFalse(self.gateway.lock.locked())
            self.assertFalse(self.runtime._lifecycle_lock.locked())
            self.assertTrue(self.dispatcher._preflight_locks[1].locked())
            self.upstream.initialization_valid = True

        self.repair.side_effect = repair
        await self.due()
        self.repair.assert_awaited_once_with(1)
        self.upstream.generate_speech.assert_not_awaited()
        self.assertEqual(receipt.read_text(encoding="utf-8"), '{"status":"unknown"}')
        self.assertEqual(self.worker.snapshot(1)["state"], "ready")

    async def test_failed_saved_binding_repairs_with_backoff_without_initial_onboarding(self):
        self.dispatcher._accounts.clear()
        self.runtime.account_errors[1] = "session_authentication_failed"
        self.repair.side_effect = GatewayError(409, "login_required", "private-cookie-value")
        await self.due()
        self.repair.assert_awaited_once_with(1)
        self.assertEqual(self.worker.snapshot(1)["error"], "login_required")
        await self.worker.tick()
        self.repair.assert_awaited_once()

        async def repair(token_id):
            self.dispatcher._accounts[token_id] = self.account
            self.runtime.account_errors.pop(token_id, None)

        self.repair.side_effect = repair
        self.advance(10)
        await self.worker.tick()
        self.assertEqual(self.worker.snapshot(1)["state"], "ready")
        self.assertEqual(self.repair.await_count, 2)

    async def test_editor_login_redirect_uses_existing_repair_without_following_redirect(self):
        self.upstream.bootstrap.side_effect = VidsProtocolError("private-location", 302)

        async def repair(_token_id):
            self.upstream.initialization_valid = True

        self.repair.side_effect = repair
        await self.due()
        self.repair.assert_awaited_once_with(1)
        self.upstream.quota.assert_not_awaited()
        self.assertEqual(self.worker.snapshot(1)["state"], "ready")

    async def test_identity_change_never_persists_new_identity_or_opens_profile(self):
        encrypted = self.store.path.read_bytes()

        async def bootstrap():
            self.upstream.flags["docs-hue"] = "different-synthetic-account"

        self.upstream.bootstrap.side_effect = bootstrap
        await self.due()
        self.assertEqual(self.worker.snapshot(1)["error"], "account_mismatch")
        self.repair.assert_not_awaited()
        self.upstream.quota.assert_not_awaited()
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        self.upstream.flags["docs-hue"] = "same-account"

    async def test_rejected_saved_binding_requires_correction_before_profile_repair(self):
        self.dispatcher._accounts.clear()
        self.runtime.account_errors[1] = "account_mismatch"
        await self.due()
        self.repair.assert_not_awaited()
        self.assertEqual(self.worker.snapshot(1)["error"], "account_mismatch")

    async def test_cancelled_probe_releases_reservations_and_does_not_count_a_failure(self):
        entered = asyncio.Event()

        async def bootstrap():
            entered.set()
            self.assertTrue(self.gateway.lock.locked())
            self.assertTrue(self.dispatcher._preflight_locks[1].locked())
            self.assertFalse(self.runtime._lifecycle_lock.locked())
            await asyncio.Future()

        self.upstream.bootstrap.side_effect = bootstrap
        await self.worker.tick()
        self.advance(100)
        task = asyncio.create_task(self.worker.tick())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.gateway.lock.locked())
        self.assertFalse(self.dispatcher._preflight_locks[1].locked())
        self.assertEqual(self.worker.snapshot(1)["consecutive_failures"], 0)
        self.assertFalse(self.worker._tick_lock.locked())

    async def test_timeout_and_unknown_errors_are_safe_and_retried(self):
        async def bootstrap():
            await asyncio.Future()

        self.worker.attempt_timeout = 0.01
        self.upstream.bootstrap.side_effect = bootstrap
        await self.due()
        self.assertEqual(self.worker.snapshot(1)["error"], "maintenance_timeout")
        self.assertFalse(self.gateway.lock.locked())
        self.upstream.bootstrap.side_effect = ValueError("private-cookie-value")
        self.advance(10)
        await self.worker.tick()
        self.assertEqual(self.worker.snapshot(1)["error"], "maintenance_failed")
        self.assertNotIn("private", str(self.worker.snapshot(1)))

    async def test_second_account_still_probes_after_first_failure(self):
        self.tokens[2] = SimpleNamespace(
            id=2, is_active=True, image_enabled=True, video_enabled=True
        )
        self.runtime.binding_rows[2] = {"token_id": 2}
        self.dispatcher._accounts[2] = self.account
        original = self.worker._maintain

        async def maintain(token_id):
            if token_id == 1:
                raise VidsTransportError("private-first-account")
            self.assertEqual(token_id, 2)

        self.worker._maintain = AsyncMock(side_effect=maintain)
        await self.due()
        self.assertEqual(self.worker.snapshot(1)["state"], "backoff")
        self.assertEqual(self.worker.snapshot(2)["state"], "ready")
        self.worker._maintain = original

    async def test_worker_start_is_idempotent_and_close_waits_for_probe_cleanup(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def tick():
            try:
                entered.set()
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        self.worker.tick = tick
        self.worker.start()
        task = self.worker._task
        self.worker.start()
        self.assertIs(self.worker._task, task)
        await entered.wait()
        await self.worker.close()
        self.assertTrue(cleaned.is_set())
        self.assertTrue(task.done())
        self.worker.start()
        self.assertIsNone(self.worker._task)

    async def test_live_config_disable_interval_and_repair_cooldown_apply(self):
        settings = {
            "keepalive_enabled": False,
            "keepalive_interval_minutes": 1,
            "repair_cooldown_seconds": 30,
        }
        self.worker.get_config = AsyncMock(side_effect=lambda: dict(settings))
        await self.worker.tick()
        self.assertEqual(self.worker.public_states()[1]["status"], "disabled")
        self.assertIsNone(self.worker.public_states()[1]["next_probe_at"])
        self.advance(1000)
        await self.worker.tick()
        self.upstream.bootstrap.assert_not_awaited()
        settings["keepalive_enabled"] = True
        await self.worker.tick()
        self.upstream.bootstrap.assert_not_awaited()
        self.advance(60)
        await self.worker.tick()
        value = self.worker.public_states()[1]
        self.assertEqual(value["status"], "ready")
        self.assertIsNotNone(value["last_probe_at"])
        self.assertIsNotNone(value["next_probe_at"])
        self.upstream.bootstrap.side_effect = VidsTransportError("private")
        self.advance(60)
        await self.worker.tick()
        self.assertEqual(self.worker._observations[1].due, self.elapsed + 30)
        settings["repair_cooldown_seconds"] = 90
        self.advance(10)
        await self.worker.tick()
        self.assertEqual(self.worker._observations[1].due, self.elapsed + 80)
        self.assertEqual(self.worker.public_states()[1]["last_error"], "transport_failed")

    async def test_disabling_maintenance_does_not_clear_an_existing_retry_deadline(self):
        settings = {
            "keepalive_enabled": True,
            "keepalive_interval_minutes": 1,
            "repair_cooldown_seconds": 300,
        }
        self.worker.get_config = AsyncMock(side_effect=lambda: dict(settings))
        self.upstream.bootstrap.side_effect = VidsTransportError("private")
        await self.worker.tick()
        self.advance(60)
        await self.worker.tick()
        deadline = self.worker._observations[1].due
        settings["keepalive_enabled"] = False
        await self.worker.tick()
        settings["keepalive_enabled"] = True
        self.advance(1)
        await self.worker.tick()
        self.assertEqual(self.worker._observations[1].due, deadline)
        self.assertEqual(self.upstream.bootstrap.await_count, 1)

    async def test_cancelled_shutdown_waits_for_opened_profile_cleanup(self):
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

        async def tick():
            try:
                entered.set()
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        self.worker.tick = tick
        self.worker.start()
        await entered.wait()
        task = asyncio.create_task(self.worker.close())
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())


if __name__ == "__main__":
    unittest.main()
