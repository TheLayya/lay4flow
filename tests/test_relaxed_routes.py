"""Exercise Relaxed through both public HTTP formats without generating media."""

import base64
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import routes
from src.core.auth import verify_api_key_flexible, verify_openai_api_key
from src.core.account_tiers import supports_model_for_tier


class RelaxedRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[verify_api_key_flexible] = lambda: "test-key"
        app.dependency_overrides[verify_openai_api_key] = lambda: "test-key"
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.collect = AsyncMock(return_value='{"choices":[{"message":{"content":"test-result"}}]}')
        patcher = patch.object(routes, "_collect_non_stream_result", self.collect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, protocol, video_config, image_count=0):
        encoded = base64.b64encode(b"test-image").decode("ascii")
        if protocol == "openai":
            content = [{"type": "text", "text": "A cat walking"}]
            content += [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}}
            ] * image_count
            return self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "veo_3_1",
                    "messages": [{"role": "user", "content": content}],
                    "stream": False,
                    "videoConfig": video_config,
                },
            )
        parts = [{"text": "A cat walking"}]
        parts += [{"inlineData": {"mimeType": "image/png", "data": encoded}}] * image_count
        return self.client.post(
            "/v1beta/models/veo_3_1:generateContent",
            json={
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"videoConfig": video_config},
            },
        )

    def test_relaxed_maps_all_inputs_and_aspects_to_existing_ultra_models(self):
        for protocol in ("openai", "gemini"):
            for aspect, portrait in (("16:9", ""), ("9:16", "_portrait")):
                for mode, count, kind in (
                    ("text_to_video", 0, "t2v"),
                    ("image_to_video", 1, "i2v_s"),
                    ("first_last_frame", 2, "i2v_s"),
                    ("reference_to_video", 3, "r2v"),
                ):
                    with self.subTest(protocol=protocol, aspect=aspect, mode=mode):
                        self.collect.reset_mock()
                        response = self.post(
                            protocol,
                            {
                                "mode": mode,
                                "priority": "relaxed",
                                "aspectRatio": aspect,
                            },
                            count,
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        self.collect.assert_awaited_once()
                        args, kwargs = self.collect.call_args
                        expected = f"veo_3_1_{kind}_fast{portrait}_ultra_relaxed"
                        self.assertEqual(args[0], expected)
                        self.assertEqual(len(args[2]), count)
                        self.assertEqual(kwargs["response_model"], "veo_3_1")
                        self.assertEqual(kwargs["generation_options"]["priority"], "relaxed")
                        self.assertFalse(supports_model_for_tier(expected, "PAYGATE_TIER_ONE"))
                        self.assertTrue(supports_model_for_tier(expected, "PAYGATE_TIER_TWO"))

    def test_invalid_relaxed_options_fail_before_generation(self):
        for protocol in ("openai", "gemini"):
            for invalid in (
                {"quality": "quality"},
                {"quality": "lite"},
                {"durationSeconds": 4},
                {"durationSeconds": 6},
                {"resolution": "1080p"},
                {"resolution": "4k"},
                {"priority": "invalid"},
                {"priority": True},
                {"relaxed": True},
            ):
                with self.subTest(protocol=protocol, invalid=invalid):
                    self.collect.reset_mock()
                    response = self.post(protocol, {"priority": "relaxed", **invalid})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.collect.assert_not_awaited()

    def test_existing_standard_4k_request_keeps_its_upscale_model(self):
        for protocol in ("openai", "gemini"):
            with self.subTest(protocol=protocol):
                self.collect.reset_mock()
                response = self.post(
                    protocol,
                    {
                        "mode": "text_to_video",
                        "quality": "quality",
                        "resolution": "4k",
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.collect.call_args.args[0], "veo_3_1_t2v_landscape_8s_4k")
                self.assertEqual(
                    self.collect.call_args.kwargs["generation_options"]["priority"], "standard"
                )


if __name__ == "__main__":
    unittest.main()
