"""Proxy maintenance excludes active work and stale selections without touching limits."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.models import Token
from src.services.concurrency_manager import ConcurrencyManager
from src.services.load_balancer import LoadBalancer


class ProxySwitchAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = Token(
            id=1,
            st="synthetic",
            email="one@example.invalid",
            image_concurrency=2,
            video_concurrency=2,
        )
        self.cm = ConcurrencyManager()
        await self.cm.initialize([self.token])
        self.manager = SimpleNamespace(
            get_active_tokens=AsyncMock(return_value=[self.token]),
            needs_at_refresh=lambda token: False,
            ensure_valid_token=AsyncMock(return_value=self.token),
        )
        self.lb = LoadBalancer(self.manager, self.cm)
        self.lb._check_extension_route = AsyncMock(return_value=(True, ""))

    async def test_busy_and_queued_accounts_are_rejected_without_losing_leases(self):
        self.assertTrue(await self.cm.acquire_image(1))
        with self.assertRaises(ValueError):
            async with self.lb.proxy_maintenance(1):
                self.fail("maintenance acquired over an active lease")
        self.assertEqual((await self.cm.snapshot())[1], (1, 0))
        await self.cm.release_image(1)
        await self.lb._add_pending(1, True, False)
        with self.assertRaises(ValueError):
            async with self.lb.proxy_maintenance(1):
                self.fail("maintenance acquired over queued work")
        await self.lb.release_pending(1, True, False)

    async def test_maintenance_blocks_immediate_and_waiting_leases_then_resumes(self):
        async with self.lb.proxy_maintenance(1):
            self.assertFalse(await self.cm.acquire_image(1))
            self.assertFalse(await self.cm.acquire_video(1))
            self.assertFalse(await self.lb._add_pending(1, True, False))
            waiting = asyncio.create_task(self.cm.wait_acquire_image(1, 2))
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
        self.assertTrue((await waiting)[0])
        await self.cm.release_image(1)
        self.assertTrue(await self.cm.acquire_video(1))
        await self.cm.release_video(1)

    async def test_selection_started_before_a_completed_switch_does_not_reuse_old_snapshot(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def validate(token):
            entered.set()
            await release.wait()
            return token

        self.manager.ensure_valid_token.side_effect = validate
        selecting = asyncio.create_task(
            self.lb.select_token(for_image_generation=True, track_pending=True)
        )
        await entered.wait()
        async with self.lb.proxy_maintenance(1):
            pass
        release.set()
        self.assertIsNone(await selecting)
        self.assertEqual(self.lb._image_pending, {})

    async def test_failed_switch_releases_maintenance_and_keeps_limits(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            async with self.lb.proxy_maintenance(1):
                raise RuntimeError("fixture")
        self.assertTrue(await self.cm.acquire_image(1))
        self.assertEqual(await self.cm.get_image_remaining(1), 1)
        await self.cm.release_image(1)
