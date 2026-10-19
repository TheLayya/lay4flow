"""Image HTTP contracts using synthetic upstream results and image bytes only."""

import base64
import json
import struct
import unittest
import zlib
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import openai_images, routes
from src.core.auth import verify_openai_api_key


def png(width=32, height=32):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00" * (width * 3)) * height))
        + chunk(b"IEND", b"")
    )


class FakeGeneration:
    def __init__(self):
        self.calls = []
        self.result = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "deliberately not Markdown",
                        "media": {"type": "image", "url": "https://asset.example.test/image.png"},
                    }
                }
            ],
        }

    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        yield json.dumps(self.result)


class OpenAIImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = FakeGeneration()
        binding = patch.object(routes, "generation_handler", self.handler)
        binding.start()
        self.addCleanup(binding.stop)
        self.asset_loader = AsyncMock(return_value=(png(), "image/png"))
        loader = patch("src.api.openai_common.load_generated_asset", self.asset_loader)
        loader.start()
        self.addCleanup(loader.stop)
        self.app = FastAPI()
        self.app.dependency_overrides[verify_openai_api_key] = lambda: "synthetic-key"
        self.app.include_router(openai_images.create_images_router())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://api.example.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def generate(self, **kwargs):
        return await self.client.post(
            "/v1/images/generations", json={"prompt": "Synthetic cat", **kwargs}
        )

    async def test_sdk_shaped_json_returns_images_contract_and_uses_public_resolver(self):
        response = await self.generate(
            generationConfig={"imageConfig": {"aspectRatio": "1:1", "imageSize": "2k"}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], [{"url": "https://asset.example.test/image.png"}])
        self.assertIsInstance(response.json()["created"], int)
        call = self.handler.calls[0]
        self.assertEqual(call["model"], "gemini-3.1-flash-image-square-2k")
        self.assertFalse(call["stream"])
        self.assertEqual(call["response_model"], "gemini-3.1-flash-image")
        self.asset_loader.assert_not_called()

    async def test_base64_auto_size_uses_actual_asset_bytes_and_rejects_invalid_output(self):
        response = await self.generate(response_format="b64_json", size="auto")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(base64.b64decode(response.json()["data"][0]["b64_json"]), png())
        self.asset_loader.assert_awaited_once_with("https://asset.example.test/image.png", "image")
        self.asset_loader.return_value = (b"not an image", "image/png")
        invalid = await self.generate(response_format="b64_json")
        self.assertEqual(invalid.status_code, 502, invalid.text)
        self.assertIn("not a supported image", invalid.json()["error"]["message"])

    async def test_inline_result_can_be_returned_as_url_or_base64(self):
        uri = "data:image/png;base64," + base64.b64encode(png()).decode()
        self.handler.result["choices"][0]["message"]["media"]["url"] = uri
        response = await self.generate()
        self.assertEqual(response.json()["data"][0]["url"], uri)
        self.asset_loader.assert_not_called()
        response = await self.generate(response_format="b64_json")
        self.assertEqual(response.status_code, 200)
        self.asset_loader.assert_awaited_once_with(uri, "image")

    async def test_edits_accept_ordered_multipart_image_arrays(self):
        first, second = png(32, 32), png(64, 32)
        response = await self.client.post(
            "/v1/images/edits",
            data={
                "prompt": "Synthetic edit",
                "n": "1",
                "generationConfig": json.dumps({"imageConfig": {"imageSize": "1k"}}),
            },
            files=[
                ("image[]", ("first.png", first, "image/png")),
                ("image[]", ("second.png", second, "image/png")),
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.handler.calls[0]["images"], [first, second])

    async def test_invalid_requests_never_submit_to_generation(self):
        invalid = [
            {"model": "veo_3_1"},
            {"model": "gpt-image-1"},
            {"model": None},
            {"prompt": "  "},
            {"prompt": 4},
            {"prompt": "x" * 32001},
            {"n": True},
            {"n": 2},
            {"n": "1"},
            {"size": "invalid"},
            {"size": "32x32"},
            {"size": "1024x1024"},
            {"size": None},
            {"quality": "high"},
            {"response_format": "bytes"},
            {"mask": None},
            {"background": "transparent"},
            {"output_format": "png"},
            {"stream": False},
            {"unknown": 1},
            {"generationConfig": {"videoConfig": {}}},
            {"generationConfig": {"imageConfig": {"quality": "high"}}},
            {"generationConfig": {"imageConfig": {"aspectRatio": 123}}},
            {"generationConfig": {"imageConfig": {"imageSize": None}}},
            {"generationConfig": {"imageConfig": {"imageSize": "8k"}}},
            {
                "model": "gemini-3.1-flash-image-lite",
                "generationConfig": {"imageConfig": {"imageSize": "2k"}},
            },
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                response = await self.generate(**fields)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("error", response.json())
        response = await self.client.post(
            "/v1/images/generations", content="{", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/v1/images/generations", json=[])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.calls, [])

    async def test_edits_require_valid_uploads_and_bounded_count_and_bytes(self):
        for files, data, status in [
            ([("image", ("fake.png", b"not an image", "image/png"))], {"prompt": "edit"}, 400),
            ([("mask", ("mask.png", png(), "image/png"))], {"prompt": "edit"}, 400),
            ([("image[]", ("i.png", png(), "image/png"))] * 11, {"prompt": "edit"}, 400),
            (
                [("image", ("i.png", png(), "image/png"))],
                {"prompt": "edit", "generationConfig": "{"},
                400,
            ),
        ]:
            response = await self.client.post("/v1/images/edits", data=data, files=files)
            self.assertEqual(response.status_code, status, response.text)
            self.assertIn("error", response.json())
        with patch.object(openai_images, "IMAGE_LIMIT", 40):
            response = await self.client.post(
                "/v1/images/edits",
                data={"prompt": "edit"},
                files={"image": ("i.png", png(), "image/png")},
            )
            self.assertEqual(response.status_code, 413, response.text)
        with patch.object(openai_images, "TOTAL_IMAGE_LIMIT", len(png()) + 1):
            response = await self.client.post(
                "/v1/images/edits",
                data={"prompt": "edit"},
                files=[("image[]", ("i.png", png(), "image/png"))] * 2,
            )
            self.assertEqual(response.status_code, 413, response.text)
        response = await self.client.post("/v1/images/edits", json={"prompt": "edit"})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.handler.calls, [])

    async def test_upscale_fallback_and_missing_structured_media_are_not_success(self):
        self.handler.result["warnings"] = [
            {"code": "upscale_failed", "message": "synthetic failure"}
        ]
        response = await self.generate(generationConfig={"imageConfig": {"imageSize": "2k"}})
        self.assertEqual(response.status_code, 502)
        self.assertIn("upscaling failed", response.json()["error"]["message"])
        del self.handler.result["warnings"]
        del self.handler.result["choices"][0]["message"]["media"]
        self.handler.result["choices"][0]["message"]["content"] = (
            "![Generated Image](https://asset.example.test/image.png)"
        )
        response = await self.generate()
        self.assertEqual(
            response.status_code, 502, "must not parse Markdown as the new internal contract"
        )

    async def test_upstream_error_preserves_http_failure_status(self):
        self.handler.result = {
            "error": {"message": "Synthetic capacity exhausted", "status_code": 503}
        }
        response = await self.generate()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["message"], "Synthetic capacity exhausted")

    async def test_unexpected_failure_logs_request_id_without_exception_secrets(self):
        async def fail(**kwargs):
            raise RuntimeError("Bearer synthetic-private-token Cookie: SID=synthetic-secret")
            yield ""

        with patch.object(self.handler, "handle_generation", fail):
            with self.assertLogs("src.api.openai_common", level="ERROR") as captured:
                response = await self.generate()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["message"], "Internal server error")
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        request_id = response.headers["x-request-id"]
        self.assertTrue(request_id.startswith("req_"))
        output = "\n".join(captured.output)
        self.assertIn(request_id, output)
        self.assertIn("route=/v1/images/generations", output)
        self.assertIn("exception_type=RuntimeError", output)
        for secret in ("synthetic-private-token", "synthetic-secret", "Bearer", "Cookie"):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, response.text)
        self.assertIsNone(captured.records[0].exc_info)
        self.asset_loader.assert_not_called()

    async def test_pixel_size_is_rejected_before_edit_generation(self):
        response = await self.client.post(
            "/v1/images/edits",
            data={"prompt": "Synthetic edit", "size": "1024x1024"},
            files={"image": ("i.png", png(), "image/png")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("generationConfig", response.json()["error"]["message"])
        self.assertEqual(self.handler.calls, [])

    async def test_openapi_and_upload_wire_limit_are_enforced(self):
        schema = self.app.openapi()["paths"]
        request_body = schema["/v1/images/generations"]["post"]["requestBody"]
        self.assertEqual(
            request_body["content"]["application/json"]["schema"]["properties"]["size"]["enum"],
            ["auto"],
        )
        self.assertIn("prompt", request_body["content"]["application/json"]["schema"]["required"])
        self.assertIn(
            "multipart/form-data", schema["/v1/images/edits"]["post"]["requestBody"]["content"]
        )
        response = await self.client.post(
            "/v1/images/edits",
            data={"prompt": "edit"},
            files={"image": ("i.png", png(), "image/png")},
            headers={"Content-Length": str(openai_images.TOTAL_IMAGE_LIMIT + 2 * 1024 * 1024)},
        )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.handler.calls, [])

    def test_supported_image_headers_and_rejected_truncations(self):
        self.assertEqual(openai_images.image_dimensions(png(64, 32)), (64, 32, "image/png"))
        jpeg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x20\x00\x40\x01\x01\x11\x00\xff\xd9"
        self.assertEqual(openai_images.image_dimensions(jpeg), (64, 32, "image/jpeg"))
        webp = (
            b"RIFF"
            + (22).to_bytes(4, "little")
            + b"WEBPVP8X"
            + (10).to_bytes(4, "little")
            + b"\x00" * 4
            + (63).to_bytes(3, "little")
            + (31).to_bytes(3, "little")
        )
        self.assertEqual(openai_images.image_dimensions(webp), (64, 32, "image/webp"))
        for data in (b"", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF", b"<svg/>"):
            self.assertIsNone(openai_images.image_dimensions(data))


if __name__ == "__main__":
    unittest.main()
