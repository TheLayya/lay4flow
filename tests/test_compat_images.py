"""SKU Images contracts with synthetic files; no Flow calls or runtime database."""

import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import compat, compat_images, routes
from src.core.auth import verify_openai_api_key
from tests.test_openai_images import png

SKU = "flow-banana-pro-4k"
MEDIA = {"type": "image", "url": "https://asset.example.test/generated.png"}


class CompatImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        async def collect(*args, **kwargs):
            self.calls.append((args, kwargs))
            return json.dumps(
                {
                    "media": MEDIA,
                    "choices": [{"message": {"media": MEDIA}, "finish_reason": "stop"}],
                }
            )

        for binding in (
            patch.object(routes, "_collect_non_stream_result", side_effect=collect),
            patch.object(
                compat,
                "load_generated_asset",
                AsyncMock(return_value=(png(4096, 4096), "image/png")),
            ),
            patch.object(
                compat_images,
                "load_generated_asset",
                AsyncMock(return_value=(png(4096, 4096), "image/png")),
            ),
        ):
            binding.start()
            self.addCleanup(binding.stop)
        app = FastAPI()
        app.dependency_overrides[verify_openai_api_key] = lambda: "synthetic-image-key"
        app.include_router(compat_images.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def generate(self, **fields):
        return await self.client.post(
            "/compat/v1/images/generations",
            json={"model": SKU, "prompt": "A synthetic image", **fields},
        )

    async def test_common_pixel_size_and_quality_preserve_locked_resolution(self):
        response = await self.generate(size="1024x1024", quality="high", n=1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], [{"url": MEDIA["url"]}])
        self.assertEqual(response.json()["usage"], compat.SETTLEMENT_USAGE)
        args, kwargs = self.calls[0]
        self.assertEqual(args[0], "gemini-3.0-pro-image-square-4k")
        self.assertEqual(kwargs["generation_options"]["imageSize"], "4k")
        self.assertEqual(kwargs["response_model"], SKU)

    async def test_auto_size_and_base64_response(self):
        response = await self.generate(
            size="auto",
            response_format="b64_json",
            generationConfig={"imageConfig": {"aspectRatio": "1:1"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(base64.b64decode(response.json()["data"][0]["b64_json"]), png(4096, 4096))

    async def test_multipart_edit_preserves_reference_images_and_sku(self):
        data = png()
        response = await self.client.post(
            "/compat/v1/images/edits",
            data={"model": SKU, "prompt": "Synthetic edit", "n": "1", "size": "1024x1024"},
            files={"image": ("reference.png", data, "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        args, _ = self.calls[0]
        self.assertEqual(args[2], [data])

    async def test_bad_options_never_submit(self):
        for fields in (
            {"n": 2},
            {"model": "flow-veo-3-1-lite-8s-720p"},
            {"generationConfig": {"imageConfig": {"imageSize": "2k"}}},
            {"output_format": "jpeg"},
        ):
            with self.subTest(fields=fields):
                response = await self.generate(**fields)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("error", response.json())
                self.assertNotIn("usage", response.json())
        self.assertEqual(self.calls, [])

    async def test_low_resolution_returns_http_error_without_success_usage(self):
        with patch.object(
            compat, "load_generated_asset", AsyncMock(return_value=(png(1024, 8), "image/png"))
        ):
            response = await self.generate()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "output_size_mismatch")
        self.assertNotIn("usage", response.json())


if __name__ == "__main__":
    unittest.main()
