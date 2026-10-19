"""Concurrent refresh callers must receive the strongest requested final result."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from src.services.token_manager import TokenManager


class RefreshSingleflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Mock()
        self.manager = TokenManager(self.db, Mock())
        self.addAsyncCleanup(self.manager.stop_protocol_refresher)

    def assert_clean(self):
        self.assertFalse(self.manager._refresh_futures)
        self.assertFalse(self.manager._refresh_intents)

    async def test_all_callers_wait_for_one_strong_followup_and_get_its_failure(self):
        initial_started, release_initial = asyncio.Event(), asyncio.Event()
        strong_started, release_strong = asyncio.Event(), asyncio.Event()

        async def inner(token_id, *, force, refresh_st):
            if not refresh_st:
                initial_started.set()
                await release_initial.wait()
                return True
            self.assertTrue(force)
            strong_started.set()
            await release_strong.wait()
            return False

        self.manager._refresh_at_inner = AsyncMock(side_effect=inner)
        first = asyncio.create_task(self.manager._refresh_at(1))
        await initial_started.wait()
        shared = self.manager._refresh_futures[1]
        strong = [
            asyncio.create_task(self.manager._refresh_at(1, force=True, refresh_st=True))
            for _ in range(4)
        ]
        await asyncio.sleep(0)
        release_initial.set()
        await strong_started.wait()
        self.assertIs(self.manager._refresh_futures[1], shared)
        self.assertTrue(all(not waiter.done() for waiter in [first, *strong]))
        release_strong.set()
        self.assertEqual(await asyncio.gather(first, *strong), [False] * 5)
        self.assertEqual(self.manager._refresh_at_inner.await_count, 2)
        self.assert_clean()

    async def test_equal_strong_intents_share_one_call(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def inner(*args, **kwargs):
            started.set()
            await release.wait()
            return False

        self.manager._refresh_at_inner = AsyncMock(side_effect=inner)
        first = asyncio.create_task(self.manager._refresh_at(1, force=True, refresh_st=True))
        await started.wait()
        others = [
            asyncio.create_task(self.manager._refresh_at(1, force=True, refresh_st=True))
            for _ in range(3)
        ]
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(first, *others), [False] * 4)
        self.assertEqual(self.manager._refresh_at_inner.await_count, 1)
        self.assert_clean()

    async def test_force_upgrade_is_not_lost_and_does_not_retry_failure(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def inner(token_id, *, force, refresh_st):
            self.assertFalse(refresh_st)
            if not force:
                started.set()
                await release.wait()
            return force

        self.manager._refresh_at_inner = AsyncMock(side_effect=inner)
        first = asyncio.create_task(self.manager._refresh_at(1))
        await started.wait()
        forced = asyncio.create_task(self.manager._refresh_at(1, force=True))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(first, forced), [True, True])
        self.assertEqual(self.manager._refresh_at_inner.await_count, 2)
        self.assert_clean()

    async def test_cancelled_strong_waiter_does_not_cancel_its_registered_intent(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def inner(token_id, *, force, refresh_st):
            if not refresh_st:
                started.set()
                await release.wait()
                return True
            self.assertTrue(force)
            return False

        self.manager._refresh_at_inner = AsyncMock(side_effect=inner)
        first = asyncio.create_task(self.manager._refresh_at(1))
        await started.wait()
        strong = asyncio.create_task(self.manager._refresh_at(1, refresh_st=True))
        await asyncio.sleep(0)
        strong.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await strong
        self.assertFalse(self.manager._refresh_futures[1].done())
        release.set()
        self.assertFalse(await first)
        self.assertEqual(self.manager._refresh_at_inner.await_count, 2)
        self.assert_clean()

    async def test_shutdown_cancels_shared_runner_without_starting_pending_upgrade(self):
        started = asyncio.Event()

        async def inner(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.manager._refresh_at_inner = AsyncMock(side_effect=inner)
        first = asyncio.create_task(self.manager._refresh_at(1))
        await started.wait()
        strong = asyncio.create_task(self.manager._refresh_at(1, force=True, refresh_st=True))
        await asyncio.sleep(0)
        await self.manager.stop_protocol_refresher()
        results = await asyncio.gather(first, strong, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertEqual(self.manager._refresh_at_inner.await_count, 1)
        self.assert_clean()

    async def test_delete_cancels_runner_and_clears_intent(self):
        self.db.get_token = AsyncMock(return_value=None)
        self.db.get_projects_by_token = AsyncMock(return_value=[])
        self.db.delete_token = AsyncMock()
        started = asyncio.Event()

        async def inner(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.manager._refresh_at_inner = AsyncMock(side_effect=inner)
        waiter = asyncio.create_task(self.manager._refresh_at(1, refresh_st=True))
        await started.wait()
        await self.manager.delete_token(1)
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assert_clean()

    async def test_shutdown_before_runner_starts_still_cleans_metadata(self):
        self.manager._refresh_at_inner = AsyncMock(return_value=True)
        waiter = asyncio.create_task(self.manager._refresh_at(1, refresh_st=True))
        # This resumes after the waiter registers the runner, before that runner starts.
        await asyncio.sleep(0)
        await self.manager.stop_protocol_refresher()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.manager._refresh_at_inner.assert_not_awaited()
        self.assert_clean()

    async def test_inner_exception_is_shared_and_does_not_leak_metadata(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def inner(*args, **kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("synthetic failure")

        self.manager._refresh_at_inner = AsyncMock(side_effect=inner)
        first = asyncio.create_task(self.manager._refresh_at(1, refresh_st=True))
        await started.wait()
        other = asyncio.create_task(self.manager._refresh_at(1, refresh_st=True))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, other, return_exceptions=True)
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))
        self.assertEqual(self.manager._refresh_at_inner.await_count, 1)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
