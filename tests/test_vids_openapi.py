"""Vids schema contracts and wire examples use synthetic sessions and media only."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api.vids import create_vids_app
from src.api.vids_media import PREFIX, PUBLIC_PREFIX, create_vids_media_router
from src.services.vids.dispatcher import VidsAccount, VidsProviderDispatcher
from src.services.vids.gateway import (
    SUPPORTED_VIDEO_SPECIFICATIONS,
    VidsGateway,
)
from tests.test_vids_gateway import KEY, client
from tests.test_vids_images_api import JPEG
from tests.test_vids_video_gateway import video_response
from tests.test_vids_video_input import mp4


class VidsOpenAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.schemas = [create_vids_app(SimpleNamespace(), KEY).openapi()]
        accounts = FastAPI()
        accounts.include_router(create_vids_media_router(SimpleNamespace()))
        self.schemas.append(accounts.openapi())
        pool = FastAPI()
        pool.include_router(
            create_vids_media_router(SimpleNamespace(), prefix=PUBLIC_PREFIX, auto_pool=True)
        )
        self.schemas.append(pool.openapi())

    def operations(self, suffix):
        for schema, prefix in zip(self.schemas, ("", PREFIX, PUBLIC_PREFIX)):
            yield schema["paths"][prefix + suffix]

    def test_every_media_route_has_success_errors_and_bearer_security(self):
        for schema in self.schemas:
            self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
            for path, methods in schema["paths"].items():
                for method, operation in methods.items():
                    with self.subTest(path=path, method=method):
                        self.assertEqual(operation["security"], [{"HTTPBearer": []}])
                        for status in ("200", "400", "401", "409", "429", "502", "503"):
                            self.assertIn("content", operation["responses"][status])
                        error = operation["responses"]["400"]["content"]["application/json"][
                            "schema"
                        ]
                        self.assertEqual(
                            error["properties"]["error"]["properties"]["param"]["type"],
                            ["string", "null"],
                        )
                        self.assertNotIn("422", operation["responses"])

    def test_create_requests_have_required_idempotency_header_and_closed_json_fields(self):
        for suffix in (
            "/v1/vids/generations",
            "/v1/images/generations",
            "/v1/audio/speech",
            "/v1/videos",
        ):
            for methods in self.operations(suffix):
                operation = methods["post"]
                header = next(field for field in operation["parameters"] if field["in"] == "header")
                self.assertEqual(header["name"], "Idempotency-Key")
                self.assertTrue(header["required"])
                self.assertEqual(
                    (header["schema"]["minLength"], header["schema"]["maxLength"]), (1, 200)
                )
                self.assertTrue(operation["requestBody"]["required"])
                shape = operation["requestBody"]["content"]["application/json"]["schema"]
                for variant in shape.get("oneOf", [shape]):
                    self.assertFalse(variant["additionalProperties"])
        for methods in self.operations("/v1/images/generations"):
            shape = methods["post"]["requestBody"]["content"]["application/json"]["schema"]
            self.assertEqual(shape["required"], ["model", "prompt"])
            self.assertEqual(
                shape["properties"]["size"]["enum"], ["auto", "1024x1024", "1:1", "16:9", "9:16"]
            )
            self.assertEqual(shape["properties"]["response_format"]["enum"], ["url", "b64_json"])

    def test_low_level_modes_match_verified_specs_and_exclude_music(self):
        for methods in self.operations("/v1/vids/generations"):
            shape = methods["post"]["requestBody"]["content"]["application/json"]["schema"]
            videos = {}
            for variant in shape["oneOf"]:
                kind = variant["properties"]["kind"]["enum"][0]
                self.assertIn(kind, {"image", "speech", "video"})
                if kind != "video":
                    continue
                mode = variant["properties"]["mode"]["enum"][0]
                videos[mode] = tuple(
                    (
                        item["properties"]["seconds"]["enum"][0],
                        item["properties"]["size"]["enum"][0],
                    )
                    for item in variant["anyOf"]
                )
                if mode in {"edit", "extend", "upscale"}:
                    self.assertIn("video_base64", variant["required"])
                self.assertNotIn("source_id", variant["properties"])
                if mode == "animate":
                    self.assertIn("image_base64", variant["required"])
                if mode == "ingredients":
                    self.assertIn("images_base64", variant["required"])
            self.assertEqual(videos, SUPPORTED_VIDEO_SPECIFICATIONS)

    def test_image_edits_documents_one_file_and_generic_automatic_upload(self):
        for methods in self.operations("/v1/images/edits"):
            operation = methods["post"]
            header = next(field for field in operation["parameters"] if field["in"] == "header")
            self.assertEqual(header["name"], "Idempotency-Key")
            self.assertTrue(header["required"])
            content = operation["requestBody"]["content"]
            self.assertEqual(set(content), {"multipart/form-data"})
            shape = content["multipart/form-data"]["schema"]
            self.assertFalse(shape["additionalProperties"])
            self.assertEqual(shape["required"], ["model", "prompt"])
            self.assertEqual(
                set(shape["properties"]),
                {"model", "prompt", "image", "image[]", "n", "size", "response_format"},
            )
            self.assertEqual(shape["properties"]["image"]["format"], "binary")
            self.assertEqual(shape["properties"]["image[]"]["maxItems"], 1)
            self.assertEqual(shape["oneOf"][0]["required"], ["image"])
            self.assertEqual(shape["oneOf"][1]["required"], ["image[]"])
        for methods in self.operations("/v1/vids/generations"):
            shape = methods["post"]["requestBody"]["content"]["application/json"]["schema"]
            edit = next(
                variant
                for variant in shape["oneOf"]
                if variant["properties"]["kind"]["enum"] == ["image"]
                and variant["properties"]["mode"]["enum"] == ["edit"]
            )
            self.assertIn("image_base64", edit["properties"])
            self.assertEqual(edit["required"], ["kind", "prompt", "mode"])
            self.assertEqual(edit["oneOf"][1]["required"], ["image_base64"])

    def test_video_json_and_multipart_have_exact_supported_fields(self):
        for methods in self.operations("/v1/videos"):
            content = methods["post"]["requestBody"]["content"]
            self.assertEqual(set(content), {"application/json", "multipart/form-data"})
            for media_type, item in content.items():
                shape = item["schema"]
                self.assertTrue(
                    {
                        "model",
                        "prompt",
                        "seconds",
                        "size",
                        "mode",
                        "shot_mode",
                        "image_base64",
                        "images_base64",
                        "video_base64",
                    }.issubset(shape["properties"])
                )
                self.assertEqual(shape["required"], ["model", "prompt"])
                self.assertFalse(shape["additionalProperties"])
                self.assertNotIn("source_id", shape["properties"])
                if media_type == "multipart/form-data":
                    self.assertEqual(shape["properties"]["input_video"]["format"], "binary")
                    self.assertEqual(shape["properties"]["seconds"]["type"], "string")
                    self.assertEqual(
                        shape["properties"]["seconds"]["enum"],
                        [str(value) for value in range(3, 11)],
                    )
                self.assertEqual(
                    shape["properties"]["size"]["enum"],
                    ["1280x720", "720x1280", "1920x1080", "1080x1920"],
                )
            for status in ("200", "202"):
                shape = methods["post"]["responses"][status]["content"]["application/json"][
                    "schema"
                ]
                self.assertIn("unknown", shape["properties"]["status"]["enum"])
                self.assertIn("receipt_url", shape["properties"])
            query = {
                field["name"] for field in methods["get"]["parameters"] if field["in"] == "query"
            }
            self.assertEqual(query, {"limit", "after", "order"})

    def test_file_routes_describe_binary_output_and_speech_error_envelope(self):
        for suffix, expected in (
            ("/v1/audio/speech", {"audio/wav"}),
            ("/v1/videos/{video_id}/content", {"video/mp4"}),
            (
                "/v1/vids/generations/{generation_id}/content",
                {"image/jpeg", "audio/wav", "video/mp4"},
            ),
        ):
            for methods in self.operations(suffix):
                operation = methods.get("post", methods.get("get"))
                content = operation["responses"]["200"]["content"]
                self.assertEqual(set(content), expected)
                self.assertTrue(
                    all(item["schema"]["format"] == "binary" for item in content.values())
                )
                self.assertIn(
                    "error",
                    operation["responses"]["409"]["content"]["application/json"]["schema"][
                        "properties"
                    ],
                )


class VidsOpenAPIWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_documented_media_shapes_match_synthetic_wire_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            upstream = client()
            image = ["private-resource", "https://lh3.googleusercontent.com/private", 1024, 1024]
            upstream.generate_image = AsyncMock(return_value=[[None] * 11 + [[None] * 6 + [image]]])
            upstream.submit_text = AsyncMock(return_value=video_response(1280, 720, 3))

            async def download_image(resource, path):
                Path(path).write_bytes(JPEG)

            async def download_video(resource, path):
                Path(path).write_bytes(
                    mp4(resource["duration"], resource["width"], resource["height"])
                )

            upstream.download_image = AsyncMock(side_effect=download_image)
            upstream.download_video = AsyncMock(side_effect=download_video)
            gateway = VidsGateway(upstream, directory)
            await gateway.start()
            dispatcher = VidsProviderDispatcher(
                SimpleNamespace(
                    get_token=AsyncMock(
                        return_value=SimpleNamespace(
                            is_active=True, image_enabled=True, video_enabled=True
                        )
                    )
                )
            )
            dispatcher.register(
                VidsAccount(1, gateway, hashlib.sha256(gateway.identity.encode()).hexdigest())
            )
            account_app = FastAPI()
            account_app.include_router(create_vids_media_router(dispatcher))
            pool_app = FastAPI()
            pool_app.include_router(
                create_vids_media_router(dispatcher, prefix=PUBLIC_PREFIX, auto_pool=True)
            )
            try:
                with patch(
                    "src.core.auth.AuthManager.verify_api_key", side_effect=lambda key: key == KEY
                ):
                    for app, prefix, schema_prefix in (
                        (create_vids_app(gateway, KEY), "", ""),
                        (account_app, PREFIX.format(token_id=1), PREFIX),
                        (pool_app, PUBLIC_PREFIX, PUBLIC_PREFIX),
                    ):
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app),
                            base_url="https://vids.example.test",
                            headers={"Authorization": "Bearer " + KEY},
                        ) as http:
                            paths = app.openapi()["paths"]
                            for suffix, body, media_type in (
                                (
                                    "/v1/images/generations",
                                    {
                                        "model": "vids-image",
                                        "prompt": "Blue boat",
                                        "response_format": "b64_json",
                                    },
                                    "application/json",
                                ),
                                (
                                    "/v1/videos",
                                    {"model": "vids-video", "prompt": "Boat moves"},
                                    "application/json",
                                ),
                                (
                                    "/v1/videos",
                                    {"model": "vids-video", "prompt": "Boat moves", "seconds": "3"},
                                    "multipart/form-data",
                                ),
                            ):
                                kwargs = (
                                    {"json": body}
                                    if media_type == "application/json"
                                    else {
                                        "files": {
                                            name: (None, value) for name, value in body.items()
                                        }
                                    }
                                )
                                response = await http.post(
                                    prefix + suffix,
                                    headers={"Idempotency-Key": prefix + suffix + media_type},
                                    **kwargs,
                                )
                                self.assertEqual(response.status_code, 200, response.text)
                                shape = paths[schema_prefix + suffix]["post"]["responses"]["200"][
                                    "content"
                                ]["application/json"]["schema"]
                                self.assertTrue(set(shape["required"]).issubset(response.json()))
                            error = await http.post(
                                prefix + "/v1/videos",
                                json={"model": "vids-video", "prompt": "Boat moves"},
                            )
                            self.assertEqual(error.status_code, 400)
                            self.assertEqual(error.json()["error"]["code"], "idempotency_required")
                            self.assertIsNone(error.json()["error"]["param"])
            finally:
                await gateway.close()


if __name__ == "__main__":
    unittest.main()
