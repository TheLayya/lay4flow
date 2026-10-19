"""Account slots cover the public async-generator lifecycle without upstream calls."""

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from src.services.concurrency_manager import ConcurrencyManager
from src.services.generation_handler import GenerationHandler


class GenerationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.token = types.SimpleNamespace(
            id=7,
            email="synthetic@example.invalid",
            at="synthetic",
            is_active=True,
            image_concurrency=1,
            video_concurrency=1,
            user_paygate_tier="PAYGATE_TIER_TWO",
        )
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler.concurrency_manager = self.cm = ConcurrencyManager()
        self.handler.flow_client = types.SimpleNamespace(prefill_remote_browser_pool=AsyncMock())
        self.handler.load_balancer = types.SimpleNamespace(
            select_token=AsyncMock(return_value=self.token),
            release_pending=AsyncMock(),
        )
        self.handler.token_manager = types.SimpleNamespace(
            get_token=AsyncMock(return_value=self.token),
            ensure_valid_token=AsyncMock(return_value=self.token),
            ensure_project_exists=AsyncMock(return_value="synthetic-project"),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        self.handler._update_request_log_progress = AsyncMock()
        self.handler._log_request = AsyncMock()
        self.handler._handle_image_generation = self.pipeline
        self.handler._handle_video_generation = self.pipeline
        models = patch(
            "src.services.generation_handler.MODEL_CONFIG",
            {
                "image-test": {"type": "image"},
                "video-test": {"type": "video"},
            },
        )
        models.start()
        self.addCleanup(models.stop)
        tiers = patch("src.services.generation_handler.supports_model_for_tier", return_value=True)
        tiers.start()
        self.addCleanup(tiers.stop)

    async def pipeline(self, *args, **kwargs):
        kwargs["generation_result"]["success"] = True
        yield "result"

    async def collect(self, kind="image"):
        return [chunk async for chunk in self.handler.handle_generation(f"{kind}-test", "prompt")]

    async def test_image_and_video_success_release_exactly_once(self):
        await self.cm.initialize([self.token])
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                with patch.object(
                    self.cm, f"release_{kind}", wraps=getattr(self.cm, f"release_{kind}")
                ) as release:
                    self.assertEqual(await self.collect(kind), ["result"])
                    release.assert_awaited_once_with(7)
                self.assertEqual(await self.cm.snapshot(), {7: (0, 0)})
        self.assertEqual(self.handler.load_balancer.release_pending.await_count, 2)

    async def test_unlimited_skips_all_slot_calls_and_preserves_other_work(self):
        await self.cm.initialize([self.token])
        await self.cm.acquire_image(7)
        for limit in (-1, 0, None):
            self.token.image_concurrency = limit
            with patch.object(
                self.cm, "wait_acquire_image", wraps=self.cm.wait_acquire_image
            ) as wait:
                with patch.object(self.cm, "release_image", wraps=self.cm.release_image) as release:
                    self.assertEqual(await self.collect(), ["result"])
                    wait.assert_not_awaited()
                    release.assert_not_awaited()
        self.assertEqual(await self.cm.get_image_inflight(7), 1)

    async def test_invalid_disabled_or_deleted_account_releases_stable_id(self):
        await self.cm.initialize([self.token])
        for latest in (None, types.SimpleNamespace(id=7, is_active=False)):
            self.handler.token_manager.get_token.return_value = latest
            response = await self.collect()
            self.assertEqual(json.loads(response[0])["error"]["status_code"], 503)
            self.assertEqual(await self.cm.get_image_inflight(7), 0)
        self.handler.token_manager.get_token.return_value = self.token
        self.handler.token_manager.ensure_valid_token.return_value = None
        response = await self.collect()
        self.assertEqual(json.loads(response[0])["error"]["status_code"], 503)
        self.assertEqual(await self.cm.get_image_inflight(7), 0)
        self.handler.token_manager.record_error.assert_not_awaited()

    async def test_timeout_does_not_submit_release_unowned_slot_or_count_account_error(self):
        await self.cm.initialize([self.token])
        await self.cm.acquire_image(7)
        with patch.object(self.cm, "wait_acquire_image", return_value=(False, 1234)):
            response = await self.collect()
        self.assertEqual(json.loads(response[0])["error"]["status_code"], 503)
        self.assertEqual(await self.cm.get_image_inflight(7), 1)
        self.handler.token_manager.ensure_project_exists.assert_not_awaited()
        self.handler.token_manager.record_error.assert_not_awaited()
        self.handler.load_balancer.release_pending.assert_awaited_once()

    async def test_exception_and_generator_close_release_slot(self):
        await self.cm.initialize([self.token])
        self.handler.token_manager.ensure_project_exists.side_effect = RuntimeError("synthetic")
        response = await self.collect()
        self.assertEqual(json.loads(response[0])["error"]["status_code"], 500)
        self.assertEqual(await self.cm.get_image_inflight(7), 0)
        self.handler.token_manager.ensure_project_exists.side_effect = None
        generator = self.handler.handle_generation("image-test", "prompt")
        self.assertEqual(await anext(generator), "result")
        self.assertEqual(await self.cm.get_image_inflight(7), 1)
        await generator.aclose()
        self.assertEqual(await self.cm.get_image_inflight(7), 0)

    async def test_cancel_waiting_does_not_release_running_request(self):
        await self.cm.initialize([self.token])
        await self.cm.acquire_image(7)
        waiting = asyncio.Event()
        original = self.cm.wait_acquire_image

        async def wait(*args):
            waiting.set()
            return await original(*args)

        with patch.object(self.cm, "wait_acquire_image", side_effect=wait):
            task = asyncio.create_task(self.collect())
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self.cm.get_image_inflight(7), 1)
        self.handler.load_balancer.release_pending.assert_awaited_once()
        self.handler.token_manager.record_error.assert_not_awaited()

    async def test_cancel_running_and_progress_failure_release_resources(self):
        await self.cm.initialize([self.token])
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield "unreachable"

        self.handler._handle_image_generation = blocked
        task = asyncio.create_task(self.collect())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.cm.get_image_inflight(7), 0)
        self.handler._update_request_log_progress.side_effect = RuntimeError("synthetic-progress")
        response = await self.collect()
        self.assertEqual(json.loads(response[0])["error"]["status_code"], 500)
        self.assertEqual(await self.cm.get_image_inflight(7), 0)
        self.assertEqual(self.handler.load_balancer.release_pending.await_count, 2)

    async def test_same_account_pipeline_never_exceeds_configured_limit(self):
        await self.cm.initialize([self.token])
        active = peak = 0

        async def pipeline(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                kwargs["generation_result"]["success"] = True
                yield "result"
            finally:
                active -= 1

        self.handler._handle_image_generation = pipeline
        results = await asyncio.wait_for(asyncio.gather(*(self.collect() for _ in range(10))), 3)
        self.assertEqual(results, [["result"]] * 10)
        self.assertEqual(peak, 1)
        self.assertEqual(await self.cm.get_image_inflight(7), 0)

    async def test_real_manager_different_accounts_are_isolated(self):
        second = types.SimpleNamespace(**{**vars(self.token), "id": 8})
        await self.cm.initialize([self.token, second])
        acquired = await asyncio.gather(
            self.cm.wait_acquire_image(7, 1), self.cm.wait_acquire_image(8, 1)
        )
        self.assertEqual([item[0] for item in acquired], [True, True])
        self.assertEqual(await self.cm.snapshot(), {7: (1, 0), 8: (1, 0)})
        await self.cm.release_image(7)
        await self.cm.release_image(8)
        self.assertEqual(await self.cm.snapshot(), {7: (0, 0), 8: (0, 0)})

    async def test_disable_generation_type_after_wait_is_terminal_and_logged(self):
        await self.cm.initialize([self.token])
        await self.cm.acquire_image(7)
        disabled = types.SimpleNamespace(**{**vars(self.token), "image_enabled": False})
        self.handler.token_manager.get_token.return_value = disabled
        with patch.object(self.cm, "wait_acquire_image", return_value=(True, 0)):
            response = await self.collect()
        self.assertEqual(json.loads(response[0])["error"]["status_code"], 503)
        self.handler._log_request.assert_awaited()
        self.assertEqual(self.handler._log_request.await_args.args[4], 503)
        self.assertEqual(await self.cm.get_image_inflight(7), 0)


if __name__ == "__main__":
    unittest.main()
