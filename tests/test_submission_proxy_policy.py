"""Generation exits balance independently from fixed login bindings."""

import asyncio
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.database import Database
from src.core.models import Token
from src.services.generation_handler import GenerationHandler
from src.services.proxy_manager import ProxyManager


class SubmissionProxyPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / "pool.db"))
        await self.db.init_db()
        self.manager = ProxyManager(self.db)
        self.first = await self.manager.add_proxy_pool_entry("socks5h://one.invalid:1080")
        self.second = await self.manager.add_proxy_pool_entry("socks5h://two.invalid:1080")
        self.token = Token(
            id=7,
            st="synthetic",
            at="synthetic",
            email="account@example.invalid",
            proxy_pool_id=self.first,
            proxy_url="socks5h://one.invalid:1080",
            submission_proxy_mode="pool_random",
            user_paygate_tier="PAYGATE_TIER_TWO",
        )

    async def test_submission_rotates_least_recently_used_exits(self):
        one = await self.manager.acquire_submission_proxy(self.token)
        two = await self.manager.acquire_submission_proxy(self.token)
        three = await self.manager.acquire_submission_proxy(self.token)
        self.assertEqual(
            [one.entry_id, two.entry_id, three.entry_id],
            [self.first, self.second, self.first],
        )
        self.assertEqual(
            [entry.inflight for entry in await self.manager.list_proxy_pool()], [2, 1]
        )
        await self.manager.release_submission_proxy(one)
        await self.manager.release_submission_proxy(two)
        await self.manager.release_submission_proxy(three)
        self.assertEqual(
            [entry.inflight for entry in await self.manager.list_proxy_pool()], [0, 0]
        )

    async def test_non_transport_errors_and_duplicate_release_leave_health_unchanged(self):
        for error in (
            RuntimeError("HTTP Error 403: policy"),
            RuntimeError("HTTP Error 429: quota"),
            None,
        ):
            lease = await self.manager.acquire_submission_proxy(self.token)
            await self.manager.release_submission_proxy(lease, error=error)
            await self.manager.release_submission_proxy(lease, error=RuntimeError("ProxyError"))
        entries = await self.manager.list_proxy_pool()
        self.assertEqual(
            [
                (entry.failure_count, entry.consecutive_failures, entry.inflight)
                for entry in entries
            ],
            [(0, 0, 0), (0, 0, 0)],
        )

    async def test_manual_failure_is_not_a_permanent_blacklist_and_periodic_probe_recovers(self):
        response = SimpleNamespace(status_code=200)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get = AsyncMock(return_value=response)
        with patch("curl_cffi.requests.AsyncSession", return_value=session):
            await self.db.record_proxy_pool_result(self.first, self.token.proxy_url, False)
            await self.db.record_proxy_pool_result(self.first, self.token.proxy_url, False)
            await self.db.record_proxy_pool_result(self.first, self.token.proxy_url, False)
            cooling = await self.db.get_proxy_pool_entry(self.first)
            self.assertTrue(cooling.enabled)
            self.assertEqual(cooling.consecutive_failures, 3)
            self.assertGreater(cooling.cooldown_until, datetime.now(timezone.utc))
            await self.manager.probe_due_proxy_entries(limit=1)
            self.assertNotEqual(session.get.call_args.kwargs.get("proxy"), self.token.proxy_url)
            async with self.db._connect(write=True) as db:
                await db.execute(
                    "UPDATE proxy_pool SET cooldown_until = ? WHERE id = ?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), self.first),
                )
                await db.commit()
            session.get.reset_mock()
            await self.manager.probe_due_proxy_entries(limit=1)
            recovered = await self.db.get_proxy_pool_entry(self.first)
            self.assertEqual(recovered.consecutive_failures, 0)
            self.assertIsNone(recovered.cooldown_until)
            self.assertEqual(recovered.failure_count, 3)
            self.assertEqual(recovered.success_count, 0)
            self.assertTrue(recovered.enabled)

    async def test_stale_health_result_does_not_poison_a_replaced_exit(self):
        await self.manager.update_proxy_pool_entry(self.first, "http://changed.invalid:8080")
        await self.db.record_proxy_pool_result(self.first, self.token.proxy_url, False)
        saved = await self.db.get_proxy_pool_entry(self.first)
        self.assertEqual(saved.failure_count, 0)
        self.assertIsNone(saved.last_check_ok)

    async def test_probe_treats_rest_policy_status_as_connected_and_transport_failure_as_failed(self):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get = AsyncMock()
        with patch("curl_cffi.requests.AsyncSession", return_value=session):
            for status in (200, 401, 403, 404, 429):
                session.get.return_value = SimpleNamespace(status_code=status)
                self.assertTrue(await self.manager.check_proxy_pool_entry(self.first))
                self.assertEqual(session.get.call_args.args[0], "https://aisandbox-pa.googleapis.com/v1/credits")
            session.get.side_effect = RuntimeError("curl: (97) proxy closed connection")
            self.assertFalse(await self.manager.check_proxy_pool_entry(self.first))
        saved = await self.db.get_proxy_pool_entry(self.first)
        self.assertEqual(saved.consecutive_failures, 1)
        self.assertTrue(saved.enabled)

    async def test_cancellation_after_submission_releases_inflight_without_health_penalty(self):
        lease = await self.manager.acquire_submission_proxy(self.token)

        async def task():
            try:
                await asyncio.Event().wait()
            finally:
                await self.manager.release_submission_proxy(lease)

        running = asyncio.create_task(task())
        await asyncio.sleep(0)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        saved = await self.db.get_proxy_pool_entry(lease.entry_id)
        self.assertEqual(saved.failure_count, 0)
        self.assertEqual((await self.manager.list_proxy_pool())[lease.entry_id - 1].inflight, 0)

    async def test_account_strategy_persists_and_legacy_schema_defaults_to_fixed(self):
        token_id = await self.db.add_token(self.token)
        self.assertEqual((await self.db.get_token(token_id)).submission_proxy_mode, "pool_random")
        async with self.db._connect(write=True) as db:
            await db.execute("ALTER TABLE tokens DROP COLUMN submission_proxy_mode")
            await db.execute("ALTER TABLE proxy_pool DROP COLUMN cooldown_until")
            await db.commit()
        await self.db.check_and_migrate_db()
        self.assertEqual((await self.db.get_token(token_id)).submission_proxy_mode, "fixed")
        self.assertIsNone((await self.db.get_proxy_pool_entry(self.first)).cooldown_until)

    async def test_generation_scope_uses_selected_exit_and_releases_on_close_and_failure(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.proxy_manager = self.manager
        current = None
        observed = []

        @contextmanager
        def scope(proxy):
            nonlocal current
            previous, current = current, proxy
            try:
                yield
            finally:
                current = previous

        handler.flow_client = SimpleNamespace(
            account_request_proxy=scope, prefill_remote_browser_pool=AsyncMock()
        )
        handler.load_balancer = SimpleNamespace(
            select_token=AsyncMock(return_value=self.token), release_pending=AsyncMock()
        )
        handler.concurrency_manager = None
        handler.token_manager = SimpleNamespace(
            resolve_account_proxy=AsyncMock(return_value=self.token.proxy_url),
            ensure_valid_token=AsyncMock(return_value=self.token),
            ensure_project_exists=AsyncMock(return_value="project"),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        handler._log_request = AsyncMock()
        handler._update_request_log_progress = AsyncMock()

        async def pipeline(*args, **kwargs):
            observed.append(current)
            yield "first"
            observed.append(current)
            raise RuntimeError("Flow API request failed: proxy transport failed (curl: (97))")

        handler._handle_image_generation = pipeline
        with (
            patch("src.services.generation_handler.MODEL_CONFIG", {"test": {"type": "image"}}),
            patch("src.services.generation_handler.supports_model_for_tier", return_value=True),
        ):
            generator = handler.handle_generation("test", "synthetic")
            self.assertEqual(await anext(generator), "first")
            self.assertIsNone(current)
            await generator.aclose()
            self.assertEqual((await self.manager.list_proxy_pool())[1].inflight, 0)
            self.assertEqual((await self.db.get_proxy_pool_entry(self.second)).failure_count, 0)
            chunks = [chunk async for chunk in handler.handle_generation("test", "synthetic")]
        self.assertEqual(
            observed,
            [
                "socks5h://one.invalid:1080",
                "socks5h://two.invalid:1080",
                "socks5h://two.invalid:1080",
            ],
        )
        self.assertEqual((await self.db.get_proxy_pool_entry(self.second)).failure_count, 1)
        self.assertEqual((await self.manager.list_proxy_pool())[1].inflight, 0)
        self.assertEqual(self.token.proxy_pool_id, self.first)
        self.assertEqual(self.token.proxy_url, "socks5h://one.invalid:1080")
        handler.token_manager.record_error.assert_not_awaited()
        self.assertIn("proxy transport", chunks[-1])


if __name__ == "__main__":
    unittest.main()
