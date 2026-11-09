"""Original-video uploads drive Omni Extend without exposing technical IDs in UI."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import compat, routes
from src.api.compat_videos import execute_sku_video, normalize_sku_video_body
from src.api.openai_common import APIError
from src.api.openai_videos import normalize_video_body
from src.core.media_skus import MEDIA_SKUS
from src.core.model_catalog import MODEL_CONFIG
from src.core.public_models import PUBLIC_MODELS
from src.services.browser.flow_video_upload import upload_profile_video
from src.services.flow.video_edit import validate_video_trim, validate_video_upload_range
from src.services.flow.video_extend import uploaded_extension_duration_ms
from src.services.flow_client import FlowClient
from tests import test_flow_video_upload as upload_fixtures, test_generation_options as fixtures
from tests.test_media_sku_api import movie
from tests.test_vids_video_input import mp4


class Omni12UploadExtendTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_source_normalizes_public_and_sku_without_manual_media_id(self):
        for model, normalize in (
            ("omni_1.2_flash", normalize_video_body),
            ("flow-omni-1-2-flash-extend-3s-720p", normalize_sku_video_body),
        ):
            normalized, _ = await normalize(
                SimpleNamespace(),
                {
                    "model": model,
                    "prompt": "Continue",
                    "generationConfig": {"videoConfig": {"mode": "extend", "durationSeconds": 3}},
                },
                {"input_video": (mp4(seconds=25), "video/mp4")},
            )
            self.assertEqual(normalized.video_bytes, mp4(seconds=25))
            self.assertIsNone(normalized.video_media_id)
            self.assertEqual(normalized.generation_options["mode"], "extend")
            self.assertNotIn("trimEndMs", normalized.generation_options)

    async def test_invalid_original_video_fails_before_upload(self):
        body = {
            "model": "omni_1.2_flash",
            "prompt": "Continue",
            "generationConfig": {"videoConfig": {"mode": "extend", "durationSeconds": 3}},
        }
        for data, mime in (
            (b"not-an-mp4", "video/mp4"),
            (mp4(seconds=2), "video/mp4"),
            (mp4(seconds=61), "video/mp4"),
            (mp4(seconds=8), "video/webm"),
        ):
            with self.subTest(mime=mime), self.assertRaises((routes.HTTPException, APIError)):
                await normalize_video_body(SimpleNamespace(), body, {"input_video": (data, mime)})
        with self.assertRaises((routes.HTTPException, APIError)):
            await normalize_video_body(
                SimpleNamespace(),
                {**body, "model": "omni_1.1_flash"},
                {"input_video": (mp4(seconds=8), "video/mp4")},
            )

    async def test_uploaded_settlement_uses_local_bytes_and_processed_server_marker(self):
        sku = "flow-omni-1-2-flash-extend-3s-720p"
        normalized, _ = await normalize_sku_video_body(
            SimpleNamespace(),
            {"model": sku, "prompt": "Continue"},
            {"input_video": (mp4(seconds=8), "video/mp4")},
        )
        marker = {
            "source_media_id": "uploaded-source",
            "source_duration_ms": 8000,
            "added_seconds": 3,
            "expected_duration_ms": 11000,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ("c" * 32 + ".mp4")
            media = {
                "type": "video",
                "url": "https://synthetic.invalid/tmp/" + path.name,
                "extension": marker,
            }

            async def stream(*_):
                yield "data: " + json.dumps({"media": media}) + "\n\n"

            with (
                patch.object(routes, "_iterate_openai_stream", stream),
                patch.object(
                    routes,
                    "generation_handler",
                    SimpleNamespace(file_cache=SimpleNamespace(cache_dir=root)),
                ),
            ):
                path.write_bytes(movie(seconds=11))
                result = [
                    event async for event in execute_sku_video(normalized, "http://synthetic")
                ]
                self.assertEqual(result[-1]["type"], "completed")
                with patch.object(
                    compat,
                    "load_generated_asset",
                    AsyncMock(return_value=(movie(seconds=11), "video/mp4")),
                ):
                    self.assertEqual(
                        (
                            await compat._validated_media(
                                SimpleNamespace(), {"media": media}, MEDIA_SKUS[sku], normalized
                            )
                        )["extension"],
                        marker,
                    )
                    for bad in (
                        {**marker, "source_duration_ms": 4000, "expected_duration_ms": 7000},
                        {**marker, "added_seconds": 4, "expected_duration_ms": 12000},
                        {**marker, "source_duration_ms": "8000"},
                    ):
                        with self.assertRaises(APIError):
                            await compat._validated_media(
                                SimpleNamespace(),
                                {"media": {**media, "extension": bad}},
                                MEDIA_SKUS[sku],
                                normalized,
                            )

    def test_edit_limit_stays_fifteen_and_full_source_upload_is_a_distinct_range(self):
        self.assertEqual(uploaded_extension_duration_ms(mp4(seconds=60), "video/mp4"), 60000)
        validate_video_upload_range(0, 60000, max_duration_ms=60000)
        with self.assertRaises(ValueError):
            validate_video_trim(0, 60000, max_duration_ms=60000)
        caps = PUBLIC_MODELS["omni_1.2_flash"]["capabilities"]["modes"]["extend"]
        self.assertTrue(caps["videoInput"])
        self.assertEqual(caps["maxInputVideoSeconds"], 60)
        self.assertTrue(MODEL_CONFIG["omni_1_2_extend_3s"]["supports_video"])

    async def test_full_source_upload_forwards_range_through_client_and_cookie_uploader(self):
        client = FlowClient(proxy_manager=None)
        modern = SimpleNamespace(upload_video=AsyncMock(return_value="source"))
        client._wait_for_uploaded_video = AsyncMock()
        with patch.object(client, "_modern_generation", return_value=modern):
            await client.upload_video(
                "", mp4(seconds=60), "video/mp4", "project", 0, 60000, max_duration_ms=60000
            )
        modern.upload_video.assert_awaited_once_with(
            "project", mp4(seconds=60), "video/mp4", 0, 60000, max_duration_ms=60000
        )
        fixture = upload_fixtures.FlowVideoUploadTests()
        context = fixture.context(
            start=upload_fixtures.Response(headers={"X-Goog-Upload-URL": "/upload/session"}),
            final=upload_fixtures.Response(text='{"mediaId":"source"}'),
        )
        self.assertEqual(
            await upload_profile_video(
                context, "project", mp4(seconds=60), "video/mp4", 0, 60000, max_duration_ms=60000
            ),
            "source",
        )
        headers = context.request.post.await_args_list[0].kwargs["headers"]
        self.assertEqual((headers["x-trim-start"], headers["x-trim-end"]), ("0", "60000"))


class Omni12UploadExtendHandlerTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.GenerationOptionsTests.setUp

    async def run_extend(
        self, *, source_seconds=25, processed_seconds=25, resolution="", mime="video/mp4"
    ):
        self.token.user_paygate_tier = "PAYGATE_TIER_ONE"
        self.handler.flow_client.upload_image = AsyncMock(side_effect=["image-1", "image-2"])
        self.handler.flow_client.generate_video_extend = AsyncMock(
            return_value={"operations": [{"operation": {"name": "task"}}]}
        )
        self.handler.flow_client.check_video_status = AsyncMock(
            return_value={
                "media": [
                    {
                        "name": "video-asset",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {
                            "userUploadedVideo": {},
                            "dimensions": {"length": f"{processed_seconds}s"},
                        },
                    }
                ]
            }
        )
        return [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_1_2_extend_3s" + resolution,
                "Continue",
                images=[b"one", b"two"],
                video_bytes=mp4(seconds=source_seconds),
                video_mime_type=mime,
                generation_options={"mode": "extend", "durationSeconds": 3},
            )
        ]

    async def test_full_original_upload_processed_metadata_and_references_are_used(self):
        chunks = await self.run_extend()
        self.assertEqual(chunks, ['{"result":"mock-video"}'])
        kwargs = self.handler.flow_client.upload_video.await_args.kwargs
        self.assertEqual(
            (kwargs["trim_start_ms"], kwargs["trim_end_ms"], kwargs["max_duration_ms"]),
            (0, 25000, 60000),
        )
        call = self.handler.flow_client.generate_video_extend.await_args.kwargs
        self.assertEqual(call["video_media_id"], "video-asset")
        self.assertEqual(len(call["reference_images"]), 2)

    async def test_high_resolution_total_rejected_before_upload(self):
        chunks = await self.run_extend(source_seconds=15, processed_seconds=15, resolution="_1080p")
        self.assertTrue(any('"error"' in chunk for chunk in chunks))
        self.handler.flow_client.upload_video.assert_not_awaited()
        self.handler.flow_client.upload_image.assert_not_awaited()
        self.handler.flow_client.generate_video_extend.assert_not_awaited()

    async def test_changed_processed_duration_rejected_before_paid_submission(self):
        chunks = await self.run_extend(source_seconds=25, processed_seconds=10)
        self.assertTrue(any('"error"' in chunk for chunk in chunks))
        self.handler.flow_client.upload_video.assert_awaited_once()
        self.handler.flow_client.generate_video_extend.assert_not_awaited()
        self.handler.flow_client.upload_image.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
