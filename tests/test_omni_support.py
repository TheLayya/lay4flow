import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock

from src.api.routes import _get_openai_model_catalog
from src.core.model_resolver import resolve_model_name
from src.services.flow_client import FlowClient
from src.services.generation_handler import MODEL_CONFIG


class OmniSupportTests(unittest.TestCase):
    def test_omni_config_has_text_and_reference_upstream_keys(self):
        for alias, aspect in (
            ("omni", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
            ("omni_portrait", "VIDEO_ASPECT_RATIO_PORTRAIT"),
        ):
            cfg = MODEL_CONFIG[alias]
            self.assertEqual(cfg["video_type"], "omni")
            self.assertEqual(cfg["aspect_ratio"], aspect)
            self.assertEqual(cfg["model_key"], "abra_t2v_8s")
            self.assertEqual(cfg["reference_model_key"], "abra_r2v_8s")
            self.assertEqual(cfg["reference_duration"], 8)

    def test_live_omni_duration_resolution_and_input_variants(self):
        for seconds in (4, 6, 8, 10):
            for resolution in ("", "_360p"):
                for family, upstream_prefix, video_type, min_images, max_images in (
                    ("omni", "abra_t2v", "t2v", 0, 0),
                    ("omni_r2v", "abra_r2v", "r2v", 1, 7),
                    ("omni_i2v", "abra_i2v", "i2v", 1, 1),
                ):
                    alias = f"{family}_{seconds}s{resolution}"
                    cfg = MODEL_CONFIG[alias]
                    self.assertEqual(cfg["model_key"], f"{upstream_prefix}_{seconds}s{resolution}")
                    self.assertEqual(
                        (cfg["video_type"], cfg["min_images"], cfg["max_images"]),
                        (video_type, min_images, max_images),
                    )

                alias = f"omni_i2v_{seconds}s_first_last{resolution}"
                cfg = MODEL_CONFIG[alias]
                self.assertEqual(
                    cfg["model_key"], f"omni_flash_i2v_{seconds}s_first_last{resolution}"
                )
                self.assertEqual(
                    (cfg["video_type"], cfg["min_images"], cfg["max_images"]), ("i2v", 2, 2)
                )

    def test_omni_is_listed_and_portrait_request_resolves(self):
        public_ids = {model["id"] for model in _get_openai_model_catalog(include_legacy=True)}
        self.assertTrue({"omni", "omni_portrait"}.issubset(public_ids))
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )
        self.assertEqual(
            resolve_model_name("omni", request=request, model_config=MODEL_CONFIG),
            "omni_portrait",
        )

    def test_legacy_upscale_alias_keeps_reference_dispatch(self):
        for resolution in ("1080p", "4k"):
            request = types.SimpleNamespace(
                generationConfig=types.SimpleNamespace(
                    aspectRatio="portrait",
                    imageSize=resolution,
                )
            )
            alias = resolve_model_name(
                "omni", request=request, model_config=MODEL_CONFIG, images=[b"reference"]
            )
            self.assertEqual(alias, f"omni_portrait_{resolution}")
            cfg = MODEL_CONFIG[alias]
            self.assertEqual(cfg["video_type"], "omni")
            self.assertEqual(cfg["model_key"], "abra_t2v_8s")
            self.assertEqual(cfg["reference_model_key"], "abra_r2v_8s")
            self.assertEqual(cfg["max_images"], 7)
            self.assertEqual(cfg["upsample"]["model_key"], f"veo_3_1_upsampler_{resolution}")

    def test_live_lite_image_and_r2v_models_use_observed_keys(self):
        for aspect in ("landscape", "portrait", "square", "four-three", "three-four"):
            cfg = MODEL_CONFIG[f"gemini-3.1-flash-image-lite-{aspect}"]
            self.assertEqual(cfg["model_name"], "HARBOR_SEAL")
            self.assertNotIn("upsample", cfg)
        for aspect in ("landscape", "portrait"):
            cfg = MODEL_CONFIG[f"veo_3_1_r2v_lite_{aspect}"]
            self.assertEqual(cfg["model_key"], "veo_3_1_r2v_lite")
            self.assertEqual(
                (cfg["video_type"], cfg["min_images"], cfg["max_images"]), ("r2v", 0, 3)
            )

    def test_new_upstream_keys_exist_in_captured_flow_catalog(self):
        evidence = Path(__file__).resolve().parents[1] / "tests/fixtures/flow-model-catalog.json"
        live = json.loads(evidence.read_text(encoding="utf-8-sig"))
        video_keys = {model["key"] for group in live["video"] for model in group["models"]}
        image_keys = {key for group in live["image"] for key in group["keys"]}
        for model_id, cfg in MODEL_CONFIG.items():
            if model_id.startswith(("omni", "veo_3_1_r2v_lite")):
                self.assertIn(cfg["model_key"], video_keys, model_id)
                if cfg.get("reference_model_key"):
                    self.assertIn(cfg["reference_model_key"], video_keys, model_id)
            elif model_id.startswith("gemini-3.1-flash-image-lite-"):
                self.assertIn(cfg["model_name"], image_keys, model_id)


class OmniDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = types.SimpleNamespace(
            upload_image=AsyncMock(side_effect=[f"media-{index}" for index in range(1, 11)]),
            upload_video=AsyncMock(return_value="video-asset"),
            generate_video_text=AsyncMock(
                return_value={"operations": [{"operation": {"name": "task-text"}}]}
            ),
            generate_video_reference_images=AsyncMock(
                return_value={"operations": [{"operation": {"name": "task-r2v"}}]}
            ),
            generate_video_start_end=AsyncMock(
                return_value={"operations": [{"operation": {"name": "task-i2v"}}]}
            ),
            generate_video_start_image=AsyncMock(
                return_value={"operations": [{"operation": {"name": "task-i2v-one"}}]}
            ),
            generate_video_edit=AsyncMock(
                return_value={"operations": [{"operation": {"name": "task-edit"}}]}
            ),
        )
        self.handler = __import__(
            "src.services.generation_handler", fromlist=["GenerationHandler"]
        ).GenerationHandler.__new__(
            __import__(
                "src.services.generation_handler", fromlist=["GenerationHandler"]
            ).GenerationHandler
        )
        self.handler.flow_client = self.client
        self.handler.db = types.SimpleNamespace(create_task=AsyncMock())
        self.handler._update_request_log_progress = AsyncMock()

        async def no_poll(*args, **kwargs):
            if False:
                yield None

        self.handler._poll_video_result = no_poll
        self.token = types.SimpleNamespace(
            id=1, at="access-token", user_paygate_tier="PAYGATE_TIER_NOT_PAID", video_concurrency=1
        )

    async def _run(self, config, images, *, video_media_id=None, video_bytes=None):
        return [
            chunk
            async for chunk in self.handler._handle_video_generation(
                self.token,
                "project-1",
                config,
                "A cat",
                images,
                False,
                video_media_id=video_media_id,
                video_bytes=video_bytes,
            )
        ]

    async def test_r2v_without_reference_image_is_rejected_locally(self):
        chunks = await self._run(MODEL_CONFIG["omni_r2v_4s"], [])
        self.assertTrue(any('"status_code": 400' in chunk for chunk in chunks))
        self.client.generate_video_text.assert_not_awaited()
        self.client.generate_video_reference_images.assert_not_awaited()

    async def test_first_last_uses_two_uploads_and_start_end_endpoint(self):
        await self._run(MODEL_CONFIG["omni_i2v_6s_first_last"], [b"first", b"last"])
        self.assertEqual(self.client.upload_image.await_count, 2)
        call = self.client.generate_video_start_end.await_args.kwargs
        self.assertEqual(call["model_key"], "omni_flash_i2v_6s_first_last")
        self.assertEqual((call["start_media_id"], call["end_media_id"]), ("media-1", "media-2"))
        self.client.generate_video_start_image.assert_not_awaited()

    async def test_360p_reference_image_route_uses_r2v_key(self):
        await self._run(MODEL_CONFIG["omni_r2v_8s_360p"], [b"reference"])
        call = self.client.generate_video_reference_images.await_args.kwargs
        self.assertEqual(call["model_key"], "abra_r2v_8s_360p")
        self.assertEqual(call["reference_images"][0]["mediaId"], "media-1")

    async def test_legacy_upscale_with_reference_uses_omni_reference_key(self):
        for resolution in ("1080p", "4k"):
            with self.subTest(resolution=resolution):
                await self._run(MODEL_CONFIG[f"omni_portrait_{resolution}"], [b"reference"])
                call = self.client.generate_video_reference_images.await_args.kwargs
                self.assertEqual(call["model_key"], "abra_r2v_8s")
                self.assertEqual(call["aspect_ratio"], "VIDEO_ASPECT_RATIO_PORTRAIT")
                self.client.generate_video_text.assert_not_awaited()

    async def test_edit_uploads_video_and_all_five_reference_images(self):
        await self._run(MODEL_CONFIG["omni_edit"], [b"image"] * 5, video_bytes=b"source-video")
        self.assertEqual(self.client.upload_image.await_count, 5)
        call = self.client.generate_video_edit.await_args.kwargs
        self.assertEqual(call["video_media_id"], "video-asset")
        self.assertEqual(
            call["reference_images"],
            [
                {"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": f"media-{index}"}
                for index in range(1, 6)
            ],
        )

    async def test_edit_rejects_a_sixth_reference_before_upload(self):
        chunks = await self._run(
            MODEL_CONFIG["omni_edit"], [b"image"] * 6, video_media_id="existing-video"
        )
        self.assertTrue(any('"status_code": 400' in chunk for chunk in chunks))
        self.client.upload_image.assert_not_awaited()
        self.client.generate_video_edit.assert_not_awaited()

    def test_explicit_duration_portrait_360p_resolves(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait", imageSize="360p")
        )
        self.assertEqual(
            resolve_model_name("omni_10s", request=request, model_config=MODEL_CONFIG),
            "omni_portrait_10s_360p",
        )


class OmniEditWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_wire_carries_reference_images(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("recaptcha-token", "browser-1"))
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._make_video_api_request = AsyncMock(
            return_value={"operations": [{"operation": {"name": "task-edit"}}]}
        )
        references = [
            {"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": f"media-{index}"}
            for index in range(1, 6)
        ]

        await client.generate_video_edit(
            at="access-token",
            project_id="project-1",
            prompt="Edit the scene",
            model_key="abra_edit",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            video_media_id="video-asset",
            reference_images=references,
            trim_start_ms=1000,
            trim_end_ms=6000,
        )

        payload = client._make_video_api_request.await_args.kwargs["json_data"]
        self.assertEqual(payload["requests"][0]["referenceImages"], references)
        self.assertEqual(payload["requests"][0]["videoInput"]["startFrameIndex"], 24)
        self.assertEqual(payload["requests"][0]["videoInput"]["endFrameIndex"], 144)

    async def test_uploaded_edit_uses_30fps_frame_range(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("recaptcha-token", "browser-1"))
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._make_video_api_request = AsyncMock(
            return_value={"operations": [{"operation": {"name": "task-edit"}}]}
        )
        await client.generate_video_edit(
            at="access-token",
            project_id="project-1",
            prompt="Edit",
            model_key="abra_edit",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            video_media_id="video-asset",
            trim_start_ms=0,
            trim_end_ms=10000,
            uploaded_video=True,
        )
        video_input = client._make_video_api_request.await_args.kwargs["json_data"]["requests"][0][
            "videoInput"
        ]
        self.assertEqual(
            video_input, {"mediaId": "video-asset", "startFrameIndex": 0, "endFrameIndex": 300}
        )


if __name__ == "__main__":
    unittest.main()
