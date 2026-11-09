"""Captured model metadata and mocked HTTP paths for Omni 1.2; no provider calls."""

import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api.openai_videos import normalize_video_body
from src.services.browser.flow_generation import FlowProfileGeneration
from src.services.flow.angular_video import build_video_request, resolve_video_usage
from src.services.flow.video_edit import validate_video_trim, video_edit_frame_range
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler
from tests import test_generation_options as generation_fixtures


def models():
    return json.loads((Path(__file__).parent / "fixtures/flow_omni12_models.json").read_text())


class Omni12AdmissionTests(unittest.TestCase):
    def test_exact_native_modes_and_duration_tiers_from_current_catalog(self):
        current = models()
        for prefix, mode in (("t2v", "text"), ("i2v", "start"), ("r2v", "references")):
            for seconds in range(3, 16):
                for resolution, suffix in ((1, ""), (4, "_360p")):
                    key = f"macaron_cookie_{prefix}_{seconds}s{suffix}"
                    self.assertEqual(
                        resolve_video_usage(
                            current,
                            key,
                            "9:16",
                            2,
                            duration=seconds,
                            resolution=resolution,
                            mode=mode,
                        ),
                        key,
                    )
                    if seconds > 10:
                        with self.assertRaises(ValueError):
                            resolve_video_usage(current, key, "9:16", 1, mode=mode)
        with self.assertRaises(ValueError):
            resolve_video_usage(current, "macaron_cookie_i2v_fl_15s", "16:9", 3, mode="start_end")
        self.assertEqual(resolve_video_usage(current, "abra_t2v_8s", "16:9", 1), "abra_t2v_8s")

    def test_variable_edit_pricing_requires_actual_tier_duration_resolution_match(self):
        for tier, seconds, resolution in ((1, 10, 1), (2, 15, 1), (3, 15, 4)):
            key = "macaron_cookie_edit" + ("_360p" if resolution == 4 else "")
            self.assertEqual(
                resolve_video_usage(
                    models(),
                    key,
                    "16:9",
                    tier,
                    duration=seconds,
                    resolution=resolution,
                    mode="edit",
                ),
                key,
            )
        for tier, seconds, resolution in (
            (1, 15, 1),
            (1, 11, 4),
            (2, 16, 1),
            (2, 2, 1),
            (2, None, 1),
        ):
            key = "macaron_cookie_edit" + ("_360p" if resolution == 4 else "")
            with self.assertRaises(ValueError):
                resolve_video_usage(
                    models(),
                    key,
                    "16:9",
                    tier,
                    duration=seconds,
                    resolution=resolution,
                    mode="edit",
                )

    def test_extend_tier_and_reference_wire_keep_exact_source_and_model(self):
        key = "macaron_cookie_extend_7s_360p"
        self.assertEqual(
            resolve_video_usage(models(), key, "16:9", 2, duration=7, resolution=4, mode="extend"),
            key,
        )
        with self.assertRaises(ValueError):
            resolve_video_usage(models(), key, "16:9", 1, mode="extend")
        payload = build_video_request(
            mode="extend",
            project_id="synthetic",
            prompt="Continue",
            model_name=key,
            aspect_ratio="16:9",
            captcha_token="synthetic",
            video_media_id="source",
            reference_images=[{"mediaId": "one"}, {"mediaId": "two"}],
            resolution=4,
        )
        self.assertEqual(payload[0][0][0], [None, "source"])
        self.assertEqual(payload[0][0][2], key)
        self.assertEqual(payload[0][0][8], [[None, "one"], [None, "two"]])
        self.assertEqual(payload[0][0][12], [4])

    def test_upsampler_pricing_checks_completed_duration_when_supplied(self):
        for tier, resolution, key in (
            (2, 2, "veo_3_1_upsampler_1080p"),
            (3, 3, "veo_3_1_upsampler_4k"),
        ):
            self.assertEqual(
                resolve_video_usage(
                    models(), key, "16:9", tier, duration=15, resolution=resolution, mode="upsample"
                ),
                key,
            )
            with self.assertRaises(ValueError):
                resolve_video_usage(
                    models(), key, "16:9", tier, duration=16, resolution=resolution, mode="upsample"
                )

    def test_trim_limit_is_model_specific_and_cannot_bypass_encoded_duration(self):
        with self.assertRaises(ValueError):
            validate_video_trim(0, 15000)
        validate_video_trim(0, 15000, max_duration_ms=15000)
        config = {
            "video_type": "edit",
            "model_key": "macaron_cookie_edit",
            "max_trim_ms": 15000,
            "duration_seconds": 15,
        }
        GenerationHandler._validate_generation_options(
            config, {"trimStartMs": 0, "trimEndMs": 15000}, b"synthetic"
        )
        for duration, end in ((10, 15000), (3, 2000)):
            with self.assertRaises(ValueError):
                GenerationHandler._validate_generation_options(
                    {**config, "duration_seconds": duration},
                    {"trimStartMs": 0, "trimEndMs": end},
                    b"synthetic",
                )
        media = {"video": {"userUploadedVideo": {}, "dimensions": {"length": "15s"}}}
        self.assertEqual(
            video_edit_frame_range(media, 0, 15000, max_duration_ms=15000),
            {"startFrameIndex": 0, "endFrameIndex": 450},
        )


class Omni12ClientRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client.token_manager = object()
        self.token = SimpleNamespace(
            id=1, email="synthetic@example.invalid", browser_profile_source="cloak"
        )
        self.modern = SimpleNamespace(
            generate_video=AsyncMock(return_value={"operations": []}),
            check_video_status=AsyncMock(
                return_value={
                    "media": [
                        {
                            "name": "source",
                            "video": {"userUploadedVideo": {}, "dimensions": {"length": "15s"}},
                        }
                    ]
                }
            ),
            upload_video=AsyncMock(return_value="source"),
        )
        self.client._wait_for_uploaded_video = AsyncMock()
        self.enterContext(
            patch(
                "src.services.browser.flow_generation.FlowProfileGeneration",
                return_value=self.modern,
            )
        )

    async def test_processed_fifteen_second_edit_forwards_billed_duration_and_360p(self):
        with self.client.account_request_token(self.token):
            await self.client.generate_video_edit(
                "",
                "p",
                "edit",
                "macaron_cookie_edit_360p",
                "16:9",
                "source",
                trim_start_ms=0,
                trim_end_ms=15000,
                uploaded_video=True,
            )
        call = self.modern.generate_video.await_args.kwargs
        self.assertEqual(
            (call["trim_end_frame"], call["duration"], call["resolution"]), (450, 15, 4)
        )
        self.modern.check_video_status.assert_awaited_once()

    async def test_too_short_processed_source_fails_before_generation(self):
        self.modern.check_video_status.return_value["media"][0]["video"]["dimensions"]["length"] = (
            "2.9s"
        )
        with self.client.account_request_token(self.token), self.assertRaises(ValueError):
            await self.client.generate_video_edit(
                "",
                "p",
                "edit",
                "macaron_cookie_edit",
                "16:9",
                "source",
                trim_start_ms=0,
                trim_end_ms=15000,
            )
        self.modern.generate_video.assert_not_awaited()

    async def test_existing_media_edit_default_remains_ten_seconds(self):
        with self.client.account_request_token(self.token):
            await self.client.generate_video_edit(
                "", "p", "edit", "macaron_cookie_edit", "16:9", "source"
            )
        call = self.modern.generate_video.await_args.kwargs
        self.assertEqual((call["trim_end_frame"], call["duration"]), (300, 10))

    async def test_extend_keeps_reference_images_and_upload_keeps_explicit_limit(self):
        with self.client.account_request_token(self.token):
            await self.client.generate_video_extend(
                "",
                "p",
                "Continue",
                "macaron_cookie_extend_3s",
                "16:9",
                "source",
                reference_images=[{"mediaId": "ref"}],
            )
            await self.client.upload_video(
                "", b"synthetic", "video/mp4", "p", 0, 15000, max_duration_ms=15000
            )
        self.assertEqual(
            self.modern.generate_video.await_args.kwargs["reference_images"], [{"mediaId": "ref"}]
        )
        self.modern.upload_video.assert_awaited_once_with(
            "p", b"synthetic", "video/mp4", 0, 15000, max_duration_ms=15000
        )

    async def test_live_edit_permission_rejects_free_fifteen_seconds_before_captcha_or_write(self):
        client = SimpleNamespace(_get_recaptcha_token=AsyncMock())
        generation = FlowProfileGeneration(client, object(), self.token)

        @asynccontextmanager
        async def context(**kwargs):
            yield object(), "synthetic-proxy"

        generation._context = context
        generation._verified = AsyncMock()
        generation._call = AsyncMock(side_effect=[models(), [0, None, None, 1]])
        with self.assertRaises(ValueError):
            await generation.generate_video(
                "edit",
                "p",
                "edit",
                "macaron_cookie_edit",
                "16:9",
                duration=15,
                resolution=1,
                video_media_id="source",
                trim_start_frame=0,
                trim_end_frame=450,
            )
        client._get_recaptcha_token.assert_not_awaited()
        self.assertEqual(generation._call.await_count, 2)

    async def test_final_resolution_admission_rejects_stale_paid_tier_or_missing_upsampler_before_native_write(
        self,
    ):
        stage = {
            "model_key": "veo_3_1_upsampler_1080p",
            "aspect_ratio": "16:9",
            "duration": 8,
            "resolution": 2,
            "mode": "upsample",
        }
        for catalog, tier in ((models(), 1), ([models()[0][:4] + [models()[0][4][:2]]], 2)):
            generation = FlowProfileGeneration(
                SimpleNamespace(_get_recaptcha_token=AsyncMock()), object(), self.token
            )

            @asynccontextmanager
            async def context(**kwargs):
                yield object(), "synthetic-proxy"

            generation._context = context
            generation._verified = AsyncMock()
            generation._call = AsyncMock(side_effect=[catalog, [0, None, None, tier]])
            with self.assertRaises(ValueError):
                await generation.admit_video_stages([stage])
            generation.client._get_recaptcha_token.assert_not_awaited()
            self.assertEqual(generation._call.await_count, 2)

    async def test_native_submission_rechecks_all_required_output_stages_before_captcha(self):
        generation = FlowProfileGeneration(
            SimpleNamespace(_get_recaptcha_token=AsyncMock()), object(), self.token
        )

        @asynccontextmanager
        async def context(**kwargs):
            yield object(), "synthetic-proxy"

        generation._context = context
        generation._verified = AsyncMock()
        generation._call = AsyncMock(
            side_effect=[models(), [0, None, None, 2], models(), [0, None, None, 1]]
        )
        await generation.admit_video_stages(
            [
                {
                    "model_key": "veo_3_1_upsampler_1080p",
                    "aspect_ratio": "16:9",
                    "duration": 8,
                    "resolution": 2,
                    "mode": "upsample",
                }
            ]
        )
        with self.assertRaises(ValueError):
            await generation.generate_video_text("p", "synthetic", "macaron_cookie_t2v_8s", "16:9")
        generation.client._get_recaptcha_token.assert_not_awaited()

    async def test_video_api_accepts_new_odd_lengths_and_retains_legacy_rejection(self):
        for seconds in (3, 5, 11, 15):
            normalized, _ = await normalize_video_body(
                SimpleNamespace(),
                {"model": "omni_1.2_flash", "prompt": "Synthetic", "seconds": seconds},
                {},
            )
            self.assertEqual(normalized.generation_options["durationSeconds"], seconds)
        with self.assertRaises(Exception):
            await normalize_video_body(
                SimpleNamespace(),
                {"model": "omni_1.1_flash", "prompt": "Synthetic", "seconds": 15},
                {},
            )


class Omni12HandlerTests(unittest.IsolatedAsyncioTestCase):
    setUp = generation_fixtures.GenerationOptionsTests.setUp

    async def test_long_edit_preserves_upload_trim_and_exact_upstream_family(self):
        self.token.user_paygate_tier = "PAYGATE_TIER_ONE"
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_1_2_edit_15s",
                "Edit",
                video_bytes=b"synthetic",
                generation_options={"trimStartMs": 1000, "trimEndMs": 16000},
                response_model="omni_1.2_flash",
            )
        ]
        self.assertEqual(chunks, ['{"result":"mock-video"}'])
        self.handler.flow_client.upload_video.assert_awaited_once_with(
            "test-at",
            b"synthetic",
            "video/mp4",
            project_id="project-1",
            trim_start_ms=1000,
            trim_end_ms=16000,
            max_duration_ms=15000,
        )
        submit = self.handler.flow_client.generate_video_edit.await_args.kwargs
        self.assertEqual(submit["model_key"], "macaron_cookie_edit")
        self.assertEqual(
            (submit["trim_start_ms"], submit["trim_end_ms"], submit["max_duration_ms"]),
            (0, 15000, 15000),
        )

    async def test_free_cannot_reach_long_edit_upload_or_captcha(self):
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_1_2_edit_15s",
                "Edit",
                video_bytes=b"synthetic",
                generation_options={"trimStartMs": 0, "trimEndMs": 15000},
            )
        ]
        self.assertTrue(any('"status_code": 403' in chunk for chunk in chunks))
        self.handler.flow_client.upload_video.assert_not_awaited()
        self.handler.flow_client.generate_video_edit.assert_not_awaited()

    async def test_extend_uploads_and_forwards_three_references(self):
        self.token.user_paygate_tier = "PAYGATE_TIER_ZERO"
        self.handler.flow_client.upload_image = AsyncMock(
            side_effect=["image-1", "image-2", "image-3"]
        )
        self.handler.flow_client.generate_video_extend = AsyncMock(
            return_value={"operations": [{"operation": {"name": "extend-task"}}]}
        )
        self.handler.flow_client.check_video_status = AsyncMock(
            return_value={
                "media": [
                    {
                        "name": "original",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {"dimensions": {"length": "8s"}},
                    }
                ]
            }
        )
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_1_2_extend_7s_360p",
                "Continue",
                images=[b"a", b"b", b"c"],
                video_media_id="original",
                response_model="omni_1.2_flash",
            )
        ]
        self.assertEqual(chunks, ['{"result":"mock-video"}'])
        self.assertEqual(self.handler.flow_client.upload_image.await_count, 3)
        call = self.handler.flow_client.generate_video_extend.await_args.kwargs
        self.assertEqual(call["model_key"], "macaron_cookie_extend_7s_360p")
        self.assertEqual(call["video_media_id"], "original")
        self.assertEqual(
            [r["mediaId"] for r in call["reference_images"]], ["image-1", "image-2", "image-3"]
        )

    async def test_requested_highres_admission_runs_before_native_generation(self):
        self.token.user_paygate_tier = "PAYGATE_TIER_ZERO"
        self.handler.flow_client.admit_video_stages = AsyncMock(
            side_effect=ValueError("live upsampler unavailable")
        )
        self.handler.flow_client.generate_video_text = AsyncMock()
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation("omni_1_2_8s_1080p", "Synthetic")
        ]
        self.assertTrue(any('"error"' in chunk for chunk in chunks))
        self.handler.flow_client.generate_video_text.assert_not_awaited()
        stage = self.handler.flow_client.admit_video_stages.await_args.args[0][0]
        self.assertEqual(
            (stage["model_key"], stage["resolution"], stage["duration"]),
            ("veo_3_1_upsampler_1080p", 2, 8),
        )


if __name__ == "__main__":
    unittest.main()
