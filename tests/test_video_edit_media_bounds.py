import asyncio
import unittest
from unittest.mock import AsyncMock

from src.services.flow_client import FlowClient


class VideoEditMediaBoundsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client.check_video_status = AsyncMock()
        self.client._get_recaptcha_token = AsyncMock(return_value=("test-captcha", "test-session"))
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        self.client._make_video_api_request = AsyncMock(return_value={"operations": []})

    async def edit(self, video, **options):
        self.client.check_video_status.return_value = {
            "media": [{"name": "source", "video": video}]
        }
        await self.client.generate_video_edit(
            at="test-at",
            project_id="test-project",
            prompt="Edit",
            model_key="abra_edit",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            video_media_id="source",
            **options,
        )
        return self.client._make_video_api_request.await_args.kwargs["json_data"]["requests"][0][
            "videoInput"
        ]

    async def test_short_upload_clamps_ten_second_request_to_processed_duration(self):
        async def solve(*args, **kwargs):
            self.client.check_video_status.assert_awaited_once_with(
                "test-at",
                [{"mediaName": "source", "projectId": "test-project"}],
            )
            return "test-captcha", "test-session"

        self.client._get_recaptcha_token.side_effect = solve
        result = await self.edit(
            {"userUploadedVideo": {}, "dimensions": {"length": "5.330s"}},
            trim_start_ms=0,
            trim_end_ms=10000,
        )
        self.assertEqual(result, {"mediaId": "source", "startFrameIndex": 0, "endFrameIndex": 160})
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with(
            "test-session"
        )

    async def test_actual_generated_type_overrides_upload_hint_and_keeps_nonzero_start(self):
        result = await self.edit(
            {"generatedVideo": {}, "dimensions": {"length": "4s"}},
            trim_start_ms=2000,
            trim_end_ms=10000,
            uploaded_video=True,
        )
        self.assertEqual(result, {"mediaId": "source", "startFrameIndex": 48, "endFrameIndex": 96})

    async def test_offset_duration_is_end_minus_start_and_requested_end_can_be_shorter(self):
        for requested_end, expected_frame in ((10000, 186), (5000, 150)):
            with self.subTest(requested_end=requested_end):
                result = await self.edit(
                    {
                        "userUploadedVideo": {},
                        "videoOffset": {"startOffset": "2s", "endOffset": "8.210s"},
                    },
                    trim_start_ms=0,
                    trim_end_ms=requested_end,
                )
                self.assertEqual(result["endFrameIndex"], expected_frame)

    async def test_missing_trim_uses_actual_duration_with_ten_second_limit(self):
        for duration, expected_frame in (("4s", 96), ("20s", 240)):
            with self.subTest(duration=duration):
                result = await self.edit({"generatedVideo": {}, "dimensions": {"length": duration}})
                self.assertEqual(result["endFrameIndex"], expected_frame)

    async def test_missing_metadata_preserves_legacy_range_and_upload_hint(self):
        result = await self.edit({}, trim_start_ms=0, trim_end_ms=6750, uploaded_video=True)
        self.assertEqual(result["endFrameIndex"], 203)
        self.assertEqual(await self.edit({}), {"mediaId": "source"})

    async def test_empty_or_invalid_source_range_never_solves_or_submits(self):
        for duration, start in (("5s", 5000), ("4s", 5000), ("0s", 0), ("NaNs", 0), ("n/a", 0)):
            with self.subTest(duration=duration, start=start), self.assertRaises(ValueError):
                await self.edit(
                    {"userUploadedVideo": {}, "dimensions": {"length": duration}},
                    trim_start_ms=start,
                    trim_end_ms=10000,
                )
        self.client._get_recaptcha_token.assert_not_awaited()
        self.client._make_video_api_request.assert_not_awaited()

    async def test_invalid_trim_is_rejected_before_metadata_or_captcha(self):
        with self.assertRaises(ValueError):
            await self.edit({}, trim_start_ms=0, trim_end_ms=10001)
        self.client.check_video_status.assert_not_awaited()
        self.client._get_recaptcha_token.assert_not_awaited()

    async def test_metadata_lookup_failure_is_not_silently_ignored(self):
        self.client.check_video_status.side_effect = RuntimeError("metadata unavailable")
        with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
            await self.edit({}, trim_start_ms=0, trim_end_ms=10000)
        self.client._get_recaptcha_token.assert_not_awaited()

    async def test_failed_or_processing_source_never_submits_edit(self):
        for status in ("FAILED", "PENDING"):
            self.client.check_video_status.return_value = {
                "media": [
                    {
                        "name": "source",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": f"MEDIA_GENERATION_STATUS_{status}",
                            }
                        },
                    }
                ]
            }
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "not ready"):
                await self.client._get_video_edit_frame_range(
                    "test-at", "project", "source", 0, 10000, False
                )
        self.client._get_recaptcha_token.assert_not_awaited()

    async def test_concurrent_sources_use_matching_media_only(self):
        async def lookup(at, operations):
            await asyncio.sleep(0)
            return {
                "media": [
                    {
                        "name": "other",
                        "video": {"userUploadedVideo": {}, "dimensions": {"length": "1s"}},
                    },
                    {
                        "name": "short",
                        "video": {"userUploadedVideo": {}, "dimensions": {"length": "5.330s"}},
                    },
                    {
                        "name": "long",
                        "video": {"generatedVideo": {}, "dimensions": {"length": "8s"}},
                    },
                ]
            }

        self.client.check_video_status.side_effect = lookup
        ranges = await asyncio.gather(
            *[
                self.client._get_video_edit_frame_range(
                    "test-at", "project", media_id, 0, 10000, False
                )
                for media_id in ("short", "long")
            ]
        )
        self.assertEqual(
            ranges,
            [
                {"startFrameIndex": 0, "endFrameIndex": 160},
                {"startFrameIndex": 0, "endFrameIndex": 192},
            ],
        )


if __name__ == "__main__":
    unittest.main()
