import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.core.config import config
from src.services.generation_handler import GenerationHandler


class ImageUpsampleRetryTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_retry_trace_keeps_only_public_attempt_fields(self):
        trace = GenerationHandler._safe_retry_trace(
            {
                "image_generation": {
                    "upstream_trace": {
                        "max_retries": 4,
                        "generation_attempts": [
                            {
                                "attempt": 1,
                                "success": False,
                                "recaptcha_ok": True,
                                "duration_ms": 123,
                                "error": "HTTP Error 403: reCAPTCHA evaluation failed",
                                "proxy": "socks5h://secret",
                                "token": "secret-token",
                            }
                        ],
                    }
                }
            }
        )
        self.assertEqual(trace["max_attempts"], 4)
        self.assertEqual(trace["attempts"], 1)
        self.assertEqual(trace["history"][0]["duration_ms"], 123)
        self.assertNotIn("proxy", trace["history"][0])
        self.assertNotIn("token", trace["history"][0])

    async def test_failed_generation_keeps_retry_trace_in_request_performance(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        retry_trace = {
            "max_retries": 3,
            "generation_attempts": [
                {"attempt": 1, "success": False, "error": "reCAPTCHA evaluation failed"},
                {"attempt": 2, "success": False, "error": "reCAPTCHA evaluation failed"},
                {"attempt": 3, "success": False, "error": "reCAPTCHA evaluation failed"},
            ],
        }
        error = RuntimeError("reCAPTCHA evaluation failed")
        error.flow_retry_trace = retry_trace
        handler.flow_client = types.SimpleNamespace(generate_image=AsyncMock(side_effect=error))
        handler._update_request_log_progress = AsyncMock()
        token = types.SimpleNamespace(
            id=1,
            at="synthetic-at",
            image_concurrency=1,
            user_paygate_tier="PAYGATE_TIER_NOT_PAID",
        )
        perf_trace = {}

        with self.assertRaisesRegex(RuntimeError, "reCAPTCHA evaluation failed"):
            async for _ in handler._handle_image_generation(
                token,
                "synthetic-project",
                {"model_name": "NARWHAL", "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"},
                "A test image",
                None,
                True,
                perf_trace=perf_trace,
            ):
                pass

        image_trace = perf_trace["image_generation"]
        self.assertIs(image_trace["upstream_trace"], retry_trace)
        self.assertEqual(image_trace["generation_error"], "reCAPTCHA evaluation failed")
        self.assertGreaterEqual(image_trace["generate_api_ms"], 0)

    async def test_handler_does_not_retry_client_upsample_or_claim_success(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = types.SimpleNamespace(
            generate_image=AsyncMock(
                return_value=(
                    {
                        "media": [
                            {
                                "name": "image-asset",
                                "image": {
                                    "generatedImage": {"fifeUrl": "https://example/image.jpg"}
                                },
                            }
                        ],
                    },
                    ";session-1",
                    {},
                )
            ),
            upsample_image=AsyncMock(side_effect=RuntimeError("reCAPTCHA evaluation failed")),
            _get_retry_reason=Mock(return_value="reCAPTCHA 验证失败"),
        )
        handler._update_request_log_progress = AsyncMock()
        handler.file_cache = types.SimpleNamespace(
            download_and_cache=AsyncMock(),
            cache_base64_image=AsyncMock(),
        )
        token = types.SimpleNamespace(
            id=1,
            at="access-token",
            image_concurrency=1,
            user_paygate_tier="PAYGATE_TIER_NOT_PAID",
        )
        generation_result = handler._create_generation_result()
        response_state = handler._create_response_state()

        with patch.dict(
            config._config,
            {
                "cache": {**config._config.get("cache", {}), "enabled": False},
                "flow": {**config._config["flow"], "max_retries": 3},
            },
        ):
            chunks = [
                chunk
                async for chunk in handler._handle_image_generation(
                    token,
                    "project-1",
                    {
                        "model_name": "NARWHAL",
                        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
                        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K",
                    },
                    "A test image",
                    None,
                    True,
                    generation_result=generation_result,
                    response_state=response_state,
                )
            ]

        handler.flow_client.upsample_image.assert_awaited_once()
        self.assertEqual(sum("返回原图" in chunk for chunk in chunks), 1)
        self.assertFalse(any("已放大到 2K" in chunk for chunk in chunks))
        self.assertIn("![Generated Image](https://example/image.jpg)", chunks[-1])
        handler.file_cache.cache_base64_image.assert_not_awaited()
        self.assertNotIn("upscaled_image", response_state["generated_assets"])
        self.assertEqual(response_state["url"], "https://example/image.jpg")
        self.assertTrue(generation_result["success"])


if __name__ == "__main__":
    unittest.main()
