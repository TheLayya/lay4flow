"""SKU gateway contracts with synthetic media, authentication and ASGI responses."""

import asyncio
import base64
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import compat, routes
from src.api.openai_common import load_generated_asset
from src.core.auth import AuthManager
from src.core.media_skus import MEDIA_SKUS
from tests.test_openai_images import png

API_KEY = "synthetic-media-sku-key"
IMAGE = png(1024, 576)


def sku(kind, **spec):
    return next(
        name
        for name, entry in MEDIA_SKUS.items()
        if entry["type"] == kind
        and all(entry["locked_spec"].get(key) == value for key, value in spec.items())
    )


def movie(width=1280, height=720, seconds=8):
    def box(kind, data):
        return struct.pack(">I4s", len(data) + 8, kind) + data

    mvhd = bytes(12) + struct.pack(">II", 1000, round(seconds * 1000)) + bytes(80)
    tkhd = bytes(76) + struct.pack(">II", width << 16, height << 16)
    return box(b"moov", box(b"mvhd", mvhd) + box(b"trak", box(b"tkhd", tkhd)))


class SyntheticGeneration:
    def __init__(self, directory):
        self.calls = []
        self.error = None
        self.warnings = []
        self.media = {"type": "image", "url": "https://asset.example.test/image.png"}
        self.file_cache = SimpleNamespace(cache_dir=Path(directory))
        self.gate = None
        self.started = asyncio.Event()

    def _get_base_url(self, state):
        return state.get("base_url") or "https://api.example.test"

    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.gate:
            await self.gate.wait()
        if self.error is not None:
            yield json.dumps({"error": self.error})
            return
        yield json.dumps(
            {
                "id": "chatcmpl-synthetic",
                "model": "internal-upstream-model",
                "media": self.media,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "upstream progress and text"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 555, "completion_tokens": 777, "total_tokens": 1332},
                "warnings": self.warnings,
            }
        )


class MediaSKUAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.handler = SyntheticGeneration(directory.name)
        self.asset_loader = AsyncMock(return_value=(IMAGE, "image/png"))
        for binding in (
            patch.object(routes, "generation_handler", self.handler),
            patch.object(AuthManager, "verify_api_key", side_effect=lambda key: key == API_KEY),
            patch.object(compat, "load_generated_asset", self.asset_loader),
        ):
            binding.start()
            self.addCleanup(binding.stop)
        self.app = FastAPI()
        self.app.include_router(compat.router)
        self.app.include_router(routes.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://api.example.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def generate(self, **values):
        body = {
            "model": sku("image", imageSize="1k"),
            "messages": [{"role": "user", "content": "A synthetic cat"}],
            **values,
        }
        return await self.client.post(
            "/compat/v1/chat/completions", json=body, headers={"Authorization": "Bearer " + API_KEY}
        )

    async def test_catalog_is_authenticated_and_separate_from_aggregate_catalog(self):
        unauthorized = await self.client.get("/compat/v1/models")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json()["error"]["code"], "invalid_api_key")
        headers = {"Authorization": "Bearer " + API_KEY}
        catalog = await self.client.get("/compat/v1/models", headers=headers)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual({entry["owned_by"] for entry in catalog.json()["data"]}, {"lay4flow"})
        names = {entry["id"] for entry in catalog.json()["data"]}
        self.assertEqual(names, set(MEDIA_SKUS))
        self.assertEqual(len(names), 37)
        self.assertFalse(any(name.endswith("-standard") for name in names))
        self.assertIn("flow-veo-3-1-fast-8s-720p-relaxed", names)
        aggregate = await self.client.get("/v1/models", headers=headers)
        aggregate_names = {entry["id"] for entry in aggregate.json()["data"]}
        self.assertFalse(names & aggregate_names)
        for name in ("gemini-3.0-pro-image", "gemini-3.1-flash-image", "veo_3_1"):
            self.assertIn(name, aggregate_names)
        detail = await self.client.get("/compat/v1/models/" + next(iter(names)), headers=headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["owned_by"], "lay4flow")

    async def test_legacy_standard_model_detail_keeps_alias_and_locked_metadata(self):
        headers = {"Authorization": "Bearer " + API_KEY}
        for name, entry in MEDIA_SKUS.items():
            if entry["type"] != "video" or entry["locked_spec"]["priority"] != "standard":
                continue
            alias = name + "-standard"
            with self.subTest(model=alias):
                detail = await self.client.get("/compat/v1/models/" + alias, headers=headers)
                canonical = await self.client.get("/compat/v1/models/" + name, headers=headers)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json(), {**canonical.json(), "id": alias})
                self.assertEqual(detail.json()["locked_spec"]["priority"], "standard")
                self.assertEqual(detail.json()["capabilities"]["priorities"], ["standard"])
        for invalid in (
            "flow-banana-pro-1k-standard",
            "flow-veo-3-1-fast-8s-720p-relaxed-standard",
        ):
            with self.subTest(model=invalid):
                detail = await self.client.get("/compat/v1/models/" + invalid, headers=headers)
                self.assertEqual(detail.status_code, 404, detail.text)

    async def test_legacy_standard_chat_and_stream_keep_requested_model_identity(self):
        alias = "flow-veo-3-1-fast-8s-720p-standard"
        self.handler.media = {"type": "video", "url": "https://asset.example.test/v.mp4"}
        self.asset_loader.return_value = (movie(), "video/mp4")
        canonical = await self.generate(model=alias.removesuffix("-standard"))
        self.assertEqual(canonical.status_code, 200, canonical.text)
        expected_options = self.handler.calls[-1]["generation_options"]
        for stream in (False, True):
            with self.subTest(stream=stream):
                response = await self.generate(model=alias, stream=stream)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.handler.calls[-1]["response_model"], alias)
                self.assertEqual(self.handler.calls[-1]["generation_options"], expected_options)
                self.assertEqual(expected_options["priority"], "standard")
                if stream:
                    events = [value[6:] for value in response.text.split("\n\n") if value]
                    self.assertEqual(events[-1], "[DONE]")
                    chunks = [json.loads(value) for value in events[:-1]]
                    self.assertEqual({chunk["model"] for chunk in chunks}, {alias})
                    self.assertEqual(chunks[-1]["usage"], compat.SETTLEMENT_USAGE)
                else:
                    self.assertEqual(response.json()["model"], alias)
                    self.assertEqual(response.json()["usage"], compat.SETTLEMENT_USAGE)
        calls = len(self.handler.calls)
        conflicting = await self.generate(model=alias, priority="relaxed")
        self.assertEqual(conflicting.status_code, 400, conflicting.text)
        self.assertEqual(len(self.handler.calls), calls)

    async def test_one_success_has_fixed_usage_and_independent_sku_identity(self):
        name = sku("image", imageSize="2k")
        self.asset_loader.return_value = (png(2048, 2048), "image/png")
        response = await self.generate(
            model=name,
            messages=[{"role": "user", "content": "A synthetic cat " * 200}],
            generationConfig={"imageConfig": {"aspectRatio": "1:1"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["model"], name)
        self.assertEqual(result["usage"], compat.SETTLEMENT_USAGE)
        self.assertEqual(result["media"], self.handler.media)
        self.assertEqual(result["choices"][0]["message"]["media"], self.handler.media)
        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "![Generated Image](https://asset.example.test/image.png)",
        )
        self.assertTrue(response.headers["x-request-id"].startswith("req_"))
        call = self.handler.calls[0]
        self.assertFalse(call["stream"])
        self.assertEqual(call["response_model"], name)
        self.assertEqual(call["generation_options"]["imageSize"], "2k")
        self.assertEqual(call["generation_options"]["aspectRatio"], "1:1")

    async def test_conflicting_price_parameters_and_invalid_inputs_never_generate(self):
        for values in (
            {"model": "gemini-3.0-pro-image"},
            {"model": "unknown-sku"},
            {"generationConfig": {"imageConfig": {"imageSize": "4k"}}},
            {"extra_body": {"generationConfig": {"imageConfig": {"imageSize": "2k"}}}},
            {"n": 2},
            {"n": True},
            {"stream": "true"},
            {"messages": []},
            {"messages": [{"role": "user", "content": ""}]},
            {"messages": [{"role": "user", "content": [{"type": "text", "text": 5}]}]},
            {
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": "bad"}]}
                ]
            },
            {
                "messages": [
                    {"role": "user", "content": [{"type": "video_url", "video_url": {"url": None}}]}
                ]
            },
            {
                "messages": None,
                "contents": [{"parts": [{"inlineData": {"mimeType": "image/png"}}]}],
            },
        ):
            with self.subTest(values=values):
                response = await self.generate(**values)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("error", response.json())
                self.assertNotIn("usage", response.json())
        self.assertEqual(self.handler.calls, [])

    async def test_failed_stream_is_http_error_before_any_settlement(self):
        for error, expected in (
            ({"message": "upstream failed", "status_code": 503}, 503),
            ({"message": "bad status", "status_code": 200}, 502),
            ("bad upstream error", 502),
        ):
            with self.subTest(error=error):
                self.handler.error = error
                response = await self.generate(stream=True)
                self.assertEqual(response.status_code, expected, response.text)
                self.assertIn("application/json", response.headers["content-type"])
                self.assertIn("error", response.json())
                self.assertNotIn("usage", response.json())

    async def test_incomplete_or_downgraded_image_has_no_usage(self):
        for media, warnings, data in (
            ({"type": "image", "url": ""}, [], IMAGE),
            ({"type": "video", "url": "https://asset.example.test/v.mp4"}, [], IMAGE),
            (self.handler.media, [{"code": "upscale_failed"}], IMAGE),
            (self.handler.media, [], b"invalid image"),
            (self.handler.media, [], png(512, 512)),
            (self.handler.media, [], png(1024, 100)),
            (self.handler.media, [], png(1024, 1024)),
        ):
            with self.subTest(media=media, warnings=warnings, data_length=len(data)):
                self.handler.media = media
                self.handler.warnings = warnings
                self.asset_loader.return_value = (data, "image/png")
                response = await self.generate(stream=True)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertNotIn("usage", response.json())

    async def test_video_requires_sku_dimensions_and_duration(self):
        self.handler.media = {
            "type": "video",
            "url": "https://asset.example.test/v.mp4",
            "media_id": "synthetic-id",
        }
        name = sku("video", durationSeconds=8, resolution="720p", priority="standard")
        self.asset_loader.return_value = (movie(), "video/mp4")
        response = await self.generate(model=name)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("<video", response.json()["choices"][0]["message"]["content"])
        self.assertEqual(response.json()["usage"], compat.SETTLEMENT_USAGE)
        self.assertEqual(response.json()["media"]["media_id"], "synthetic-id")
        for data in (movie(640, 360), movie(seconds=4), movie(seconds=8.3), b"invalid video"):
            self.asset_loader.return_value = (data, "video/mp4")
            failed = await self.generate(model=name, stream=True)
            self.assertEqual(failed.status_code, 502, failed.text)
            self.assertNotIn("usage", failed.json())

    async def test_single_typed_image_selects_video_mode_and_explicit_mode_is_preserved(self):
        name = sku("video", quality="quality", durationSeconds=8, resolution="720p")
        self.handler.media = {"type": "video", "url": "https://asset.example.test/v.mp4"}
        self.asset_loader.return_value = (movie(), "video/mp4")
        reference = "data:image/png;base64," + base64.b64encode(IMAGE).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Synthetic animation"},
                    {"type": "image_url", "image_url": {"url": reference}},
                ],
            }
        ]
        response = await self.generate(model=name, messages=messages)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.handler.calls[-1]["generation_options"]["mode"], "image_to_video")

        self.assertEqual(self.handler.calls[-1]["images"], [IMAGE])
        explicit = await self.generate(
            model=name,
            messages=messages,
            generationConfig={"videoConfig": {"mode": "text_to_video"}},
        )
        self.assertEqual(explicit.status_code, 400, explicit.text)
        self.assertEqual(len(self.handler.calls), 1)
        gemini = await self.generate(
            model=name,
            messages=None,
            contents=[
                {
                    "role": "user",
                    "parts": [
                        {"text": "Synthetic animation"},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(IMAGE).decode(),
                            }
                        },
                    ],
                }
            ],
        )
        self.assertEqual(gemini.status_code, 200, gemini.text)
        self.assertEqual(self.handler.calls[-1]["generation_options"]["mode"], "image_to_video")

    async def test_editing_sku_validates_trim_duration_and_returns_fixed_settlement(self):
        name = "flow-omni-1-1-flash-edit-720p"
        source = movie(seconds=8)
        body = {
            "model": name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Synthetic video edit"},
                        {
                            "type": "video_url",
                            "video_url": {
                                "url": "data:video/mp4;base64," + base64.b64encode(source).decode()
                            },
                        },
                    ],
                }
            ],
            "generationConfig": {"videoConfig": {"trimStartMs": 0, "trimEndMs": 8000}},
        }
        self.handler.media = {"type": "video", "url": "https://asset.example.test/edit.mp4"}
        self.asset_loader.return_value = (movie(), "video/mp4")
        response = await self.generate(**body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], name)
        self.assertEqual(response.json()["usage"], compat.SETTLEMENT_USAGE)
        self.assertEqual(self.handler.calls[-1]["generation_options"]["mode"], "edit")
        self.assertNotIn("durationSeconds", self.handler.calls[-1]["generation_options"])
        default_body = {key: value for key, value in body.items() if key != "generationConfig"}
        default_result = await self.generate(**default_body)
        self.assertEqual(default_result.status_code, 200, default_result.text)
        self.assertEqual(self.handler.calls[-1]["generation_options"]["trimEndMs"], 8000)
        too_long = await self.generate(
            **{
                **default_body,
                "generationConfig": {"videoConfig": {"trimEndMs": 9000}},
            }
        )
        self.assertEqual(too_long.status_code, 400, too_long.text)
        self.asset_loader.return_value = (movie(seconds=4), "video/mp4")
        failed = await self.generate(**body)
        self.assertEqual(failed.status_code, 502, failed.text)
        self.assertNotIn("usage", failed.json())

    async def test_generator_cannot_change_locked_spec_during_generation(self):
        async def mutated(**kwargs):
            kwargs["generation_options"]["imageSize"] = "4k"
            yield json.dumps({"media": self.handler.media})

        with patch.object(self.handler, "handle_generation", mutated):
            response = await self.generate()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn("usage", response.json())
        self.asset_loader.assert_not_awaited()

    async def test_inline_asset_is_saved_to_short_url_before_sse(self):
        self.handler.media["url"] = "data:image/png;base64," + base64.b64encode(IMAGE).decode()
        with patch.object(compat, "load_generated_asset", load_generated_asset):
            response = await self.generate(stream=True)
        self.assertEqual(response.status_code, 200, response.text)
        events = [value[6:] for value in response.text.split("\n\n") if value]
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(value) for value in events[:-1]]
        self.assertEqual(len(chunks), 4)
        url = chunks[1]["media"]["url"]
        self.assertTrue(url.startswith("https://api.example.test/tmp/"), url)
        self.assertNotIn("base64", response.text)
        saved = self.handler.file_cache.cache_dir / url.rsplit("/", 1)[1]
        self.assertEqual(saved.read_bytes(), IMAGE)
        self.assertEqual(chunks[-1]["choices"], [])
        self.assertEqual(chunks[-1]["usage"], compat.SETTLEMENT_USAGE)
        self.assertEqual(len({chunk["id"] for chunk in chunks}), 1)
        self.assertEqual(len({chunk["model"] for chunk in chunks}), 1)
        self.assertFalse(self.handler.calls[0]["stream"])

    async def test_stream_does_not_send_headers_until_generation_finishes(self):
        self.handler.gate = asyncio.Event()
        body = json.dumps(
            {
                "model": sku("image", imageSize="1k"),
                "messages": [{"role": "user", "content": "A synthetic cat"}],
                "stream": True,
            }
        ).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/compat/v1/chat/completions",
            "raw_path": b"/compat/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "server": ("api.example.test", 443),
            "client": ("127.0.0.1", 1234),
            "headers": [
                (b"host", b"api.example.test"),
                (b"content-type", b"application/json"),
                (b"authorization", ("Bearer " + API_KEY).encode()),
            ],
        }
        sent = []
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(copy.deepcopy(message))

        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(self.handler.started.wait(), 2)
            self.assertEqual(sent, [])
            self.handler.gate.set()
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sent[0]["type"], "http.response.start")
        self.assertEqual(sent[0]["status"], 200)
        self.assertIn(b"[DONE]", b"".join(message.get("body", b"") for message in sent))

    async def test_openai_sdk_uses_standard_completions_and_usage_chunks(self):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            self.skipTest("Optional OpenAI SDK is not installed")
        client = AsyncOpenAI(
            api_key=API_KEY,
            base_url="https://api.example.test/compat/v1",
            http_client=self.client,
            max_retries=0,
        )
        self.addAsyncCleanup(client.close)
        name = sku("image", imageSize="1k")
        completion = await client.chat.completions.create(
            model=name, messages=[{"role": "user", "content": "Synthetic cat"}]
        )
        self.assertEqual(completion.model, name)
        self.assertEqual(completion.usage.prompt_tokens, 1000)
        self.assertEqual(completion.usage.completion_tokens, 0)
        self.assertEqual(completion.usage.total_tokens, 1000)
        self.assertEqual(completion.media, self.handler.media)
        stream = await client.chat.completions.create(
            model=name,
            messages=[{"role": "user", "content": "Synthetic cat"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = [chunk async for chunk in stream]
        self.assertEqual(chunks[-1].usage.total_tokens, 1000)
        self.assertEqual(chunks[-1].choices, [])
        self.assertEqual(chunks[2].choices[0].finish_reason, "stop")
        self.assertEqual(chunks[1].media, self.handler.media)
        self.assertTrue(all(chunk.model == name for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
