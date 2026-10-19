import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.core.config import config
from src.services.generation_handler import GenerationHandler


class ImageUpsampleRetryTests(unittest.IsolatedAsyncioTestCase):
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
