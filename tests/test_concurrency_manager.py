"""Slot contention, cancellation and live account changes without upstream calls."""

import asyncio
import unittest
from types import SimpleNamespace

from src.services.concurrency_manager import ConcurrencyManager


class ConcurrencyManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ConcurrencyManager()
        self.token = SimpleNamespace(id=1, image_concurrency=2, video_concurrency=1)
        await self.manager.initialize([self.token])

    async def wait_until_queued(self, kind):
        async with asyncio.timeout(2):
            while (kind, 1) not in self.manager._slot_conditions:
                await asyncio.sleep(0)

    async def test_contended_tasks_never_exceed_account_limits(self):
        active = {"image": 0, "video": 0}
        peak = dict(active)

        async def work(kind):
            acquired, _ = await getattr(self.manager, f"wait_acquire_{kind}")(1, 5)
            self.assertTrue(acquired)
            try:
                active[kind] += 1
                peak[kind] = max(peak[kind], active[kind])
                await asyncio.sleep(0)
                active[kind] -= 1
            finally:
                await getattr(self.manager, f"release_{kind}")(1)

        await asyncio.wait_for(
            asyncio.gather(*(work(kind) for _ in range(50) for kind in active)), 5
        )
        self.assertEqual(peak, {"image": 2, "video": 1})
        self.assertEqual(await self.manager.snapshot(), {1: (0, 0)})
        self.assertFalse(self.manager._slot_conditions)

    async def test_cancelled_waiter_and_release_do_not_lose_capacity(self):
        self.assertTrue(await self.manager.acquire_video(1))
        waiter = asyncio.create_task(self.manager.wait_acquire_video(1, 5))
        await self.wait_until_queued("video")
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(await self.manager.get_video_inflight(1), 1)
        self.assertFalse(self.manager._slot_conditions)
        await self.manager.release_video(1)
        self.assertTrue((await self.manager.wait_acquire_video(1, 1))[0])
        await self.manager.release_video(1)

    async def test_timeout_does_not_acquire_or_leave_waiter_state(self):
        await self.manager.acquire_video(1)
        acquired, waited_ms = await asyncio.wait_for(self.manager.wait_acquire_video(1, 1), 3)
        self.assertFalse(acquired)
        self.assertGreaterEqual(waited_ms, 900)
        self.assertEqual(await self.manager.get_video_inflight(1), 1)
        self.assertFalse(self.manager._slot_conditions)

    async def test_limit_reload_keeps_live_slots_and_wakes_waiters(self):
        await self.manager.acquire_video(1)
        await self.manager.initialize([self.token])
        self.assertEqual(await self.manager.get_video_inflight(1), 1)
        waiter = asyncio.create_task(self.manager.wait_acquire_video(1, 5))
        await self.wait_until_queued("video")
        await self.manager.reset_token(1, image_concurrency=2, video_concurrency=2)
        self.assertTrue((await asyncio.wait_for(waiter, 1))[0])
        self.assertEqual(await self.manager.get_video_inflight(1), 2)
        await self.manager.release_video(1)
        await self.manager.release_video(1)

    async def test_deleted_account_cannot_resume_queued_work(self):
        await self.manager.acquire_video(1)
        waiter = asyncio.create_task(self.manager.wait_acquire_video(1, 5))
        await self.wait_until_queued("video")
        await self.manager.remove_token(1)
        self.assertFalse((await asyncio.wait_for(waiter, 1))[0])
        await self.manager.release_video(1)
        self.assertEqual(await self.manager.snapshot(), {})
        self.assertFalse(self.manager._slot_conditions)


if __name__ == "__main__":
    unittest.main()
