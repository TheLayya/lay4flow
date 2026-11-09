"""The recovery worker runs independently of account/provider maintenance."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.browser.profile_cache_maintenance import ProfileCacheMaintenance


class ProfileCacheMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_runs_at_start_and_deduplicates_overlapping_ticks(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def prune():
            entered.set()
            await release.wait()
            return {"cleaned": 1, "bytes_reclaimed": 123}

        manager = SimpleNamespace(prune_idle_caches=AsyncMock(side_effect=prune))
        worker = ProfileCacheMaintenance(manager)
        worker.start()
        first = worker._task
        worker.start()
        self.assertIs(worker._task, first)
        await entered.wait()
        self.assertIsNone(await worker.tick())
        release.set()
        await asyncio.sleep(0)
        await worker.close()
        manager.prune_idle_caches.assert_awaited_once()
        self.assertEqual(worker.last_result["bytes_reclaimed"], 123)
        worker.start()
        self.assertIsNone(worker._task)

    async def test_close_waits_for_cancelled_cleanup_to_release_ownership(self):
        entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def prune():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                closing.set()
                await release.wait()
                raise

        worker = ProfileCacheMaintenance(SimpleNamespace(prune_idle_caches=prune))
        worker.start()
        await entered.wait()
        shutdown = asyncio.create_task(worker.close())
        await closing.wait()
        shutdown.cancel()
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await shutdown


if __name__ == "__main__":
    unittest.main()
