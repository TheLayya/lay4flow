import types
import unittest
from unittest.mock import AsyncMock, patch

from src.services.generation_handler import GenerationHandler


class VideoUpsampleTests(unittest.IsolatedAsyncioTestCase):
    CASES = (
        ("veo_3_1_t2v_fast", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
        ("abra_t2v_8s", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
        ("abra_t2v_8s", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
        ("abra_t2v_8s_360p", "VIDEO_RESOLUTION_720P", "omni_upsampler_360p"),
    )

    def make_handler(self, source_model):
        base_id = "11111111-1111-1111-1111-111111111111"
        upscaled_id = "22222222-2222-2222-2222-222222222222"
        self.base_url = f"https://example.com/video/{base_id}/original.mp4"
        self.upscaled_url = f"https://example.com/video/{upscaled_id}/upscaled.mp4"
        self.aspect = "VIDEO_ASPECT_RATIO_PORTRAIT"

        def operation(media_id, model):
            return {
                "operation": {
                    "name": media_id,
                    "metadata": {"video": {"aspectRatio": self.aspect, "model": model}},
                },
                "mediaName": media_id,
                "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
            }

        self.base_operation = operation(base_id, source_model)
        self.upscaled_operation = operation(upscaled_id, "upscaled-model")
        self.upscaled_operations = [self.upscaled_operation]
        self.token = types.SimpleNamespace(
            id=7,
            at="test-at",
            st="test-st",
            video_concurrency=2,
            user_paygate_tier=" PAYGATE_TIER_TWO ",
        )
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler.flow_client = types.SimpleNamespace(
            check_video_status=AsyncMock(
                side_effect=[
                    {"operations": [self.base_operation]},
                    {"operations": self.upscaled_operations},
                ]
            ),
            get_media_url_redirect=AsyncMock(side_effect=[self.base_url, self.upscaled_url]),
            upsample_video=AsyncMock(return_value={"operations": self.upscaled_operations}),
        )
        self.handler.db = types.SimpleNamespace(update_task=AsyncMock())
        self.handler._update_request_log_progress = AsyncMock()
        self.generation_result = self.handler._create_generation_result()
        self.response_state = self.handler._create_response_state()

    async def poll(self, resolution, upsampler):
        with (
            patch(
                "src.services.generation_handler.config",
                types.SimpleNamespace(
                    max_poll_attempts=3,
                    poll_interval=0,
                    cache_enabled=False,
                ),
            ),
            patch("src.services.generation_handler.asyncio.sleep", new_callable=AsyncMock),
        ):
            return [
                chunk
                async for chunk in self.handler._poll_video_result(
                    self.token,
                    "project-1",
                    [self.base_operation],
                    True,
                    {"resolution": resolution, "model_key": upsampler},
                    generation_result=self.generation_result,
                    response_state=self.response_state,
                )
            ]

    async def test_successful_video_submits_upsample_and_returns_upscaled_result(self):
        for source_model, resolution, upsampler in self.CASES:
            with self.subTest(source_model=source_model, resolution=resolution):
                self.make_handler(source_model)
                chunks = await self.poll(resolution, upsampler)

                self.handler.flow_client.upsample_video.assert_awaited_once_with(
                    at="test-at",
                    project_id="project-1",
                    video_media_id=self.base_operation["mediaName"],
                    aspect_ratio=self.aspect,
                    resolution=resolution,
                    model_key=upsampler,
                    user_paygate_tier="PAYGATE_TIER_TWO",
                    token_id=7,
                    token_video_concurrency=2,
                )
                self.assertEqual(self.handler.flow_client.check_video_status.await_count, 2)
                self.handler.flow_client.check_video_status.assert_awaited_with(
                    "test-at",
                    self.upscaled_operations,
                )
                self.assertIn(self.upscaled_url, chunks[-1])
                self.assertNotIn(self.base_url, chunks[-1])
                self.assertEqual(self.response_state["url"], self.upscaled_url)
                self.assertEqual(
                    self.response_state["generated_assets"]["mediaGenerationId"],
                    self.upscaled_operation["mediaName"],
                )
                self.assertTrue(self.generation_result["success"])
                self.assertEqual(
                    self.handler.db.update_task.await_args.args[0],
                    self.upscaled_operation["mediaName"],
                )

    async def test_upsample_submission_failure_returns_original_video(self):
        for source_model, resolution, upsampler in self.CASES:
            with self.subTest(source_model=source_model, resolution=resolution):
                self.make_handler(source_model)
                self.handler.flow_client.upsample_video.side_effect = RuntimeError(
                    "upstream rejected upscale"
                )
                chunks = await self.poll(resolution, upsampler)

                self.handler.flow_client.upsample_video.assert_awaited_once()
                self.handler.flow_client.check_video_status.assert_awaited_once()
                self.assertTrue(any("upstream rejected upscale" in chunk for chunk in chunks))
                self.assertIn(self.base_url, chunks[-1])
                self.assertNotIn(self.upscaled_url, chunks[-1])
                self.assertEqual(self.response_state["url"], self.base_url)
                self.assertTrue(self.generation_result["success"])
                self.assertEqual(
                    self.handler.db.update_task.await_args.args[0], self.base_operation["mediaName"]
                )


if __name__ == "__main__":
    unittest.main()
