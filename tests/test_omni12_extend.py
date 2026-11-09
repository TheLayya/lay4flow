"""Extend source normalization and duration settlement use server-owned metadata."""

import base64
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
from src.services.flow.video_extend import extension_metadata
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler
from src.services.video_jobs import VideoJobError
from tests import test_generation_options as fixtures
from tests.test_media_sku_api import movie
from tests.test_openai_images import png

SKU = "flow-omni-1-2-flash-extend-3s-720p"


def metadata():
    return {
        "source_media_id": "source",
        "source_duration_ms": 8000,
        "added_seconds": 3,
        "expected_duration_ms": 11000,
    }


class Omni12ExtendAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_public_json_extend_accepts_three_references_and_rejects_four(self):
        reference = {"image_url": "data:image/png;base64," + base64.b64encode(png(32, 32)).decode()}
        body = {
            "model": "omni_1.2_flash",
            "prompt": "Continue",
            "input_video_media_id": "source",
            "generationConfig": {"videoConfig": {"mode": "extend", "durationSeconds": 3}},
            "input_reference": [reference] * 3,
        }
        normalized, _ = await normalize_video_body(SimpleNamespace(), body, {})
        self.assertEqual(len(normalized.images), 3)
        self.assertEqual(normalized.video_media_id, "source")
        with self.assertRaises(APIError):
            await normalize_video_body(
                SimpleNamespace(), {**body, "input_reference": [reference] * 4}, {}
            )

    async def test_public_and_sku_videos_accept_explicit_media_id_with_separate_images(self):
        for model, normalizer in (
            ("omni_1.2_flash", normalize_video_body),
            (SKU, normalize_sku_video_body),
        ):
            body = {
                "model": model,
                "prompt": "Continue",
                "input_video_media_id": "source",
                "generationConfig": {"videoConfig": {"mode": "extend", "durationSeconds": 3}},
            }
            normalized, params = await normalizer(
                SimpleNamespace(), body, {"input_reference": [(png(32, 32), "image/png")] * 3}
            )
            self.assertEqual(normalized.video_media_id, "source")
            self.assertEqual(len(normalized.images), 3)
            self.assertEqual(normalized.generation_options["mode"], "extend")
            self.assertEqual(params["seconds"], "3")

    async def test_source_id_rejects_urls_conflicts_wrong_models_and_modes(self):
        base = {
            "model": "omni_1.2_flash",
            "prompt": "Continue",
            "input_video_media_id": "source",
            "generationConfig": {"videoConfig": {"mode": "extend", "durationSeconds": 3}},
        }
        for change, files in (
            ({"input_video_media_id": "https://foreign.invalid/source"}, {}),
            ({"model": "omni_1.1_flash"}, {}),
            ({"generationConfig": {"videoConfig": {"mode": "text_to_video"}}}, {}),
            ({}, {"input_video": (b"synthetic", "video/mp4")}),
            ({"input_video_media_id": "../source"}, {}),
        ):
            with self.subTest(change=change), self.assertRaises(APIError):
                await normalize_video_body(SimpleNamespace(), {**base, **change}, files)

    async def normalized(self):
        normalized, _ = await normalize_sku_video_body(
            SimpleNamespace(),
            {"model": SKU, "prompt": "Continue", "input_video_media_id": "source"},
            {},
        )
        return normalized

    async def test_chat_settlement_checks_total_source_plus_added_length_and_trusted_metadata(self):
        normalized = await self.normalized()
        for seconds, extension, code in (
            (11, metadata(), None),
            (3, metadata(), "output_duration_mismatch"),
            (11, None, "extension_metadata_invalid"),
            (11, {**metadata(), "source_media_id": "other"}, "extension_metadata_invalid"),
        ):
            payload = {
                "media": {
                    "type": "video",
                    "url": "https://synthetic.invalid/result.mp4",
                    **({"extension": extension} if extension is not None else {}),
                }
            }
            with patch.object(
                compat,
                "load_generated_asset",
                AsyncMock(return_value=(movie(seconds=seconds), "video/mp4")),
            ):
                if code:
                    with self.assertRaises(APIError) as error:
                        await compat._validated_media(
                            SimpleNamespace(), payload, MEDIA_SKUS[SKU], normalized
                        )
                    self.assertEqual(error.exception.code, code)
                else:
                    result = await compat._validated_media(
                        SimpleNamespace(), payload, MEDIA_SKUS[SKU], normalized
                    )
                    self.assertEqual(result["extension"]["expected_duration_ms"], 11000)

    async def test_video_job_settlement_accepts_eleven_second_combined_output_not_three(self):
        normalized = await self.normalized()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ("a" * 32 + ".mp4")
            media = {
                "type": "video",
                "url": "https://synthetic.invalid/tmp/" + path.name,
                "extension": metadata(),
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
                result = [x async for x in execute_sku_video(normalized, "http://synthetic")]
                self.assertEqual(result[-1]["type"], "completed")
                path.write_bytes(movie(seconds=3))
                with self.assertRaises(VideoJobError) as error:
                    _ = [x async for x in execute_sku_video(normalized, "http://synthetic")]
                self.assertEqual(error.exception.code, "output_duration_mismatch")

    def test_public_completion_exposes_only_server_extension_metadata(self):
        chunk = json.dumps({"choices": [{"message": {}, "finish_reason": "stop"}]})
        result = json.loads(
            GenerationHandler._attach_public_media(
                chunk,
                {
                    "url": "https://synthetic.invalid/result",
                    "generated_assets": {"extension": metadata()},
                },
                {},
                "video",
            )
        )
        self.assertEqual(result["media"]["extension"], metadata())


class Omni12ExtendPreflightTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.GenerationOptionsTests.setUp

    async def test_bad_source_metadata_or_excessive_upscale_total_rejects_before_upload_submit(
        self,
    ):
        self.token.user_paygate_tier = "PAYGATE_TIER_ONE"
        self.handler.flow_client.upload_image = AsyncMock()
        self.handler.flow_client.generate_video_extend = AsyncMock()
        self.handler.flow_client.check_video_status = AsyncMock()
        for model, source in (
            ("omni_1_2_extend_3s", {"media": []}),
            (
                "omni_1_2_extend_3s_1080p",
                {
                    "media": [
                        {
                            "name": "source",
                            "mediaMetadata": {
                                "mediaStatus": {
                                    "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                                }
                            },
                            "video": {"dimensions": {"length": "15s"}},
                        }
                    ]
                },
            ),
        ):
            self.handler.flow_client.check_video_status.return_value = source
            chunks = [
                chunk
                async for chunk in self.handler.handle_generation(
                    model, "Continue", images=[b"synthetic"], video_media_id="source"
                )
            ]
            self.assertTrue(any('"error"' in x for x in chunks))
        self.handler.flow_client.upload_image.assert_not_awaited()
        self.handler.flow_client.generate_video_extend.assert_not_awaited()

    def test_source_metadata_requires_matching_completed_bounded_video(self):
        source = {
            "name": "source",
            "mediaMetadata": {
                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"}
            },
            "video": {"dimensions": {"length": "8s"}},
        }
        self.assertEqual(extension_metadata({"media": [source]}, "source", 3), metadata())
        for change in (
            {"name": "other"},
            {"video": {"dimensions": {"length": "NaNs"}}},
            {
                "mediaMetadata": {
                    "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_PENDING"}
                }
            },
        ):
            with self.assertRaises(ValueError):
                extension_metadata({"media": [{**source, **change}]}, "source", 3)

    async def test_non_cloak_extend_keeps_reference_images_in_submitted_request(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("synthetic", "synthetic-session"))
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._make_video_api_request = AsyncMock(return_value={"operations": []})
        with (
            patch.object(
                client, "_acquire_video_launch_gate", AsyncMock(return_value=(True, None, None))
            ),
            patch.object(client, "_release_video_launch_gate", AsyncMock()),
            patch.object(client, "_warmup_flow_video_frontend_context", AsyncMock()),
        ):
            await client.generate_video_extend(
                "synthetic",
                "p",
                "Continue",
                "macaron_cookie_extend_3s",
                "16:9",
                "source",
                reference_images=[{"mediaId": "ref"}],
            )
        request = client._make_video_api_request.await_args.kwargs["json_data"]["requests"][0]
        self.assertEqual(request["referenceImages"], [{"mediaId": "ref"}])


if __name__ == "__main__":
    unittest.main()
