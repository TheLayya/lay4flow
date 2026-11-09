"""Login replacement reserves work without requiring old Vids session health."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.core.database import Database
from src.services.factory import create_services
from src.services.vids.gateway import GatewayError
from src.services.vids.runtime import VidsRuntime


class VidsLoginMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dispatcher = SimpleNamespace(
            _accounts={},
            _pool_pending=set(),
            _preflight_locks={},
            _account=Mock(side_effect=AssertionError("Do not probe old account health")),
        )
        self.runtime = VidsRuntime(self.dispatcher, object())

    async def test_failed_or_missing_vids_binding_allows_flow_login_replacement(self):
        self.runtime.binding_rows[7] = {"proxy": "http://fixed.invalid:8080"}
        self.runtime.account_errors[7] = {"error_code": "session_unavailable"}
        async with self.runtime.login_maintenance(7):
            self.assertTrue(self.runtime._lifecycle_lock.locked())
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        self.assertIn(7, self.runtime.binding_rows)
        self.assertIn(7, self.runtime.account_errors)
        self.dispatcher._account.assert_not_called()

    async def test_unhealthy_registered_gateway_is_locked_without_session_probe(self):
        gateway = SimpleNamespace(
            lock=asyncio.Lock(),
            _ready=False,
            persistence_failed=True,
            client=SimpleNamespace(initialization_valid=False),
        )
        self.dispatcher._accounts[7] = SimpleNamespace(gateway=gateway)
        async with self.runtime.login_maintenance(7):
            self.assertTrue(gateway.lock.locked())
        self.assertFalse(gateway.lock.locked())
        self.dispatcher._account.assert_not_called()

    async def test_busy_gateway_preflight_and_pool_selection_are_rejected(self):
        gateway = SimpleNamespace(lock=asyncio.Lock())
        self.dispatcher._accounts[7] = SimpleNamespace(gateway=gateway)
        preflight = self.dispatcher._preflight_locks[7] = asyncio.Lock()
        for reason in ("gateway", "preflight", "selection"):
            with self.subTest(reason=reason):
                if reason == "gateway":
                    await gateway.lock.acquire()
                elif reason == "preflight":
                    await preflight.acquire()
                else:
                    self.dispatcher._pool_pending.add(7)
                try:
                    with self.assertRaises(GatewayError) as failure:
                        async with self.runtime.login_maintenance(7):
                            self.fail("Busy account cannot enter maintenance")
                    self.assertEqual(failure.exception.code, "account_busy")
                finally:
                    if gateway.lock.locked():
                        gateway.lock.release()
                    if preflight.locked():
                        preflight.release()
                    self.dispatcher._pool_pending.discard(7)
        self.assertFalse(self.runtime._lifecycle_lock.locked())

    async def test_unrelated_lifecycle_work_is_reserved_once_without_revalidation(self):
        entered = asyncio.Event()

        async def reserve():
            async with self.runtime.login_maintenance(7):
                entered.set()

        async with self.runtime._lifecycle_lock:
            task = asyncio.create_task(reserve())
            await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
        await task
        self.assertTrue(entered.is_set())
        self.dispatcher._account.assert_not_called()

    async def test_failed_or_cancelled_update_releases_both_locks(self):
        gateway = SimpleNamespace(lock=asyncio.Lock())
        self.dispatcher._accounts[7] = SimpleNamespace(gateway=gateway)
        for error in (ValueError("synthetic update failure"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                async with self.runtime.login_maintenance(7):
                    raise error
            self.assertFalse(gateway.lock.locked())
            self.assertFalse(self.runtime._lifecycle_lock.locked())

    async def test_factory_reserves_flow_and_vids_without_old_session_validation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.services.generation_handler.FileCache"),
        ):
            services = create_services(
                Database(str(Path(directory) / "unused.db")), profile_refresher=object()
            )
            token = SimpleNamespace(id=7)
            services.vids_runtime.binding_rows[7] = {"proxy": "http://fixed.invalid:8080"}
            services.vids_runtime.account_errors[7] = {"error_code": "session_unavailable"}
            async with services.token_manager.account_login_maintenance(
                token, "http://fixed.invalid:8080"
            ):
                self.assertIn(7, services.load_balancer._maintenance)
                self.assertFalse(await services.concurrency_manager.can_use_image(7))
                self.assertTrue(services.vids_runtime._lifecycle_lock.locked())
            self.assertNotIn(7, services.load_balancer._maintenance)
            self.assertTrue(await services.concurrency_manager.can_use_image(7))
            self.assertFalse(services.vids_runtime._lifecycle_lock.locked())
            self.assertFalse(Path(directory, "unused.db").exists())


if __name__ == "__main__":
    unittest.main()
