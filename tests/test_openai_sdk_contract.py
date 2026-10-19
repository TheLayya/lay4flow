"""Run the installed OpenAI SDK against real ASGI routes with synthetic generation."""

import hashlib
import json
import unittest
import warnings
from unittest.mock import patch

import httpx
from fastapi import FastAPI

try:
    from openai import APIError, AsyncOpenAI, AuthenticationError, BadRequestError
except ImportError:
    AsyncOpenAI = None

from src.api import openai_images, routes
from src.api.openai_videos import create_videos_router
from src.core.auth import AuthManager
from tests.test_openai_images import png

API_KEY = "synthetic-sdk-key"
MODEL = "gemini-3.1-flash-image"
MEDIA = {"type": "image", "url": "https://asset.example.test/generated.png"}
VIDEO_MEDIA = {
    "type": "video",
    "url": "https://asset.example.test/generated.mp4",
    "mime_type": "video/mp4",
    "media_id": "video-1",
}


class SyntheticGeneration:
    def __init__(self):
        self.calls = []
        self.error = None

    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            yield json.dumps({"error": self.error})
            return
        if not kwargs["stream"]:
            yield json.dumps(
                {
                    "id": "chatcmpl-synthetic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal-upstream-name",
                    "media": MEDIA,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "![Generated Image](https://asset.example.test/generated.png)",
                                "media": MEDIA,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            return
        for index in range(4):
            final = index == 3
            delta = (
                {"content": "final synthetic image", "media": MEDIA}
                if final
                else {"reasoning_content": f"Synthetic progress {index}"}
            )
            payload = {
                "id": f"different-upstream-id-{index}",
                "object": "chat.completion.chunk",
                "created": index,
                "model": f"different-upstream-model-{index}",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": "stop" if final else None}
                ],
            }
            if final:
                payload["media"] = MEDIA
            yield "data: " + json.dumps(payload) + "\n\n"


class SyntheticVideoGeneration(SyntheticGeneration):
    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        yield json.dumps(
            {
                "id": "chatcmpl-video-synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "internal-upstream-name",
                "media": VIDEO_MEDIA,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "<video src='https://asset.example.test/generated.mp4'>",
                            "media": VIDEO_MEDIA,
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )


class SyntheticVideoJobs:
    def __init__(self):
        self.calls = []
        self.rows = {}

    async def submit(self, owner, normalized, params, base_url, idempotency_key=None):
        self.calls.append((owner, normalized, params, base_url, idempotency_key))
        row = {
            "id": f"video_synthetic_{len(self.calls)}",
            "object": "video",
            "created_at": 1,
            "status": "queued",
            "progress": 0,
            "error": None,
            **params,
        }
        self.rows[row["id"]] = (owner, row)
        return row

    async def get(self, video_id, owner):
        saved_owner, row = self.rows.get(video_id, (None, None))
        return row if owner == saved_owner else None


@unittest.skipUnless(AsyncOpenAI, "Optional OpenAI SDK is not installed")
class OpenAISDKContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = SyntheticGeneration()
        self.jobs = SyntheticVideoJobs()
        for binding in (
            patch.object(routes, "generation_handler", self.handler),
            patch.object(AuthManager, "verify_api_key", side_effect=lambda key: key == API_KEY),
        ):
            binding.start()
            self.addCleanup(binding.stop)
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.include_router(openai_images.create_images_router())
        self.app.include_router(create_videos_router(lambda: self.jobs))
        self.wire = []

        async def capture(request):
            self.wire.append(
                {
                    "path": request.url.path,
                    "type": request.headers.get("content-type", ""),
                    "body": await request.aread(),
                }
            )

        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://api.example.test",
            event_hooks={"request": [capture]},
        )
        self.sdk = AsyncOpenAI(
            api_key=API_KEY,
            base_url="https://api.example.test/v1",
            max_retries=0,
            http_client=self.http,
        )
        self.addAsyncCleanup(self.sdk.close)

    async def test_sdk_chat_preserves_public_model_and_structured_media(self):
        result = await self.sdk.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Synthetic prompt"}],
            extra_body={
                "generationConfig": {"imageConfig": {"aspectRatio": "1:1", "imageSize": "2k"}}
            },
        )
        self.assertEqual(result.model, MODEL)
        self.assertEqual(result.choices[0].message.media, MEDIA)
        self.assertEqual(result.media, MEDIA)
        self.assertTrue(result._request_id.startswith("req_"))
        sent = json.loads(self.wire[-1]["body"])
        self.assertIn("generationConfig", sent)
        self.assertNotIn("extra_body", sent)
        self.assertEqual(self.handler.calls[0]["model"], MODEL + "-square-2k")

    async def test_sdk_responses_parses_standard_image_output(self):
        result = await self.sdk.responses.create(
            model=MODEL,
            input="Synthetic prompt",
        )
        self.assertEqual(
            result.output_text, "![Generated Image](https://asset.example.test/generated.png)"
        )
        self.assertEqual(result.output[-1].type, "image_generation_call")
        self.assertEqual(result.output[-1].result, "https://asset.example.test/generated.png")

    async def test_sdk_responses_parses_standard_video_output(self):
        video_handler = SyntheticVideoGeneration()
        with patch.object(routes, "generation_handler", video_handler):
            result = await self.sdk.responses.create(
                model="veo_3_1",
                input="Synthetic prompt",
            )
        self.assertEqual(result.output[-1].type, "video_generation_call")
        self.assertEqual(result.output[-1].result, VIDEO_MEDIA["url"])
        self.assertEqual(result.output[-1].media_id, VIDEO_MEDIA["media_id"])

    async def test_sdk_stream_fixes_identity_and_exposes_progress_and_final_media(self):
        stream = await self.sdk.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Synthetic prompt"}], stream=True
        )
        self.assertTrue(stream.response.headers["x-request-id"].startswith("req_"))
        async with stream:
            chunks = [chunk async for chunk in stream]
        self.assertEqual(len(chunks), 4)
        self.assertEqual(len({chunk.id for chunk in chunks}), 1)
        self.assertEqual(len({chunk.created for chunk in chunks}), 1)
        self.assertEqual({chunk.model for chunk in chunks}, {MODEL})
        self.assertEqual(chunks[0].choices[0].delta.reasoning_content, "Synthetic progress 0")
        self.assertEqual(chunks[-1].choices[0].delta.content, "final synthetic image")
        self.assertEqual(chunks[-1].choices[0].delta.media, MEDIA)
        self.assertEqual(chunks[-1].media, MEDIA)
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    async def test_sdk_responses_stream_preserves_video_media_extension(self):
        result = await self.sdk.responses.create(model=MODEL, input="Synthetic prompt", stream=True)
        async with result:
            events = [event async for event in result]
        completed = next(event for event in events if event.type == "response.completed")
        self.assertEqual(completed.response.media, MEDIA)

    async def test_sdk_images_generate_and_edit_use_actual_json_and_multipart(self):
        generated = await self.sdk.images.generate(
            model=MODEL,
            prompt="Synthetic image",
            n=1,
            response_format="url",
            extra_body={"generationConfig": {"imageConfig": {"imageSize": "1k"}}},
        )
        self.assertEqual(generated.data[0].url, MEDIA["url"])
        self.assertTrue(generated._request_id.startswith("req_"))
        self.assertEqual(self.wire[-1]["type"], "application/json")
        image = png()
        edited = await self.sdk.images.edit(
            model=MODEL, prompt="Synthetic edit", image=("sample.png", image, "image/png"), n=1
        )
        self.assertEqual(edited.data[0].url, MEDIA["url"])
        self.assertTrue(self.wire[-1]["type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(self.handler.calls[-1]["images"], [image])

    async def test_sdk_and_json_video_requests_use_same_normalized_job_contract(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = await self.sdk.videos.create(
                model="omni_1.1_flash",
                prompt="Synthetic clip",
                seconds="4",
                size="1280x720",
                extra_headers={"Idempotency-Key": "synthetic-video-key"},
            )
            retrieved = await self.sdk.videos.retrieve(result.id)
        self.assertEqual(result.object, "video")
        self.assertEqual(result.status, "queued")
        self.assertEqual(retrieved.id, result.id)
        self.assertEqual(result.seconds, "4")
        self.assertTrue(result._request_id.startswith("req_"))
        owner, normalized, params, base_url, key = self.jobs.calls[0]
        self.assertEqual(owner, hashlib.sha256(API_KEY.encode()).hexdigest())
        self.assertEqual(normalized.public_model, "omni_1.1_flash")
        self.assertEqual(normalized.generation_options["durationSeconds"], 4)
        self.assertEqual(key, "synthetic-video-key")
        self.assertEqual(base_url, "https://api.example.test")
        response = await self.http.post(
            "/v1/videos",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "omni_1.1_flash",
                "prompt": "Synthetic clip",
                "seconds": "4",
                "size": "1280x720",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.jobs.calls[-1][2], params)

    async def test_sdk_video_upload_uses_multipart_and_keeps_reference(self):
        image = png()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = await self.sdk.videos.create(
                model="omni_1.1_flash",
                prompt="Synthetic reference clip",
                seconds="4",
                size="1280x720",
                input_reference=("reference.png", image, "image/png"),
            )
        self.assertEqual(result.status, "queued")
        self.assertTrue(self.wire[-1]["type"].startswith("multipart/form-data; boundary="))
        normalized = self.jobs.calls[-1][1]
        self.assertEqual(normalized.images, [image])
        self.assertEqual(normalized.generation_options["mode"], "image_to_video")

    async def test_sdk_auth_validation_and_stream_errors_remain_errors(self):
        with self.assertRaises(AuthenticationError) as failed:
            await self.sdk.with_options(api_key="synthetic-invalid").images.generate(
                model=MODEL, prompt="Synthetic"
            )
        self.assertEqual(failed.exception.status_code, 401)
        self.assertTrue(failed.exception.request_id.startswith("req_"))
        self.assertEqual(failed.exception.body["type"], "authentication_error")
        with self.assertRaises(BadRequestError) as failed:
            await self.sdk.images.generate(model=MODEL, prompt="Synthetic", n=2)
        self.assertTrue(failed.exception.request_id.startswith("req_"))
        self.assertEqual(self.handler.calls, [])
        self.handler.error = {
            "message": "Synthetic stream failure",
            "type": "server_error",
            "code": "generation_failed",
            "status_code": 502,
        }
        stream = await self.sdk.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Synthetic prompt"}], stream=True
        )
        async with stream:
            with self.assertRaises(APIError):
                async for _ in stream:
                    self.fail("The error event must not become a successful chunk")

    async def test_chunked_multipart_body_limit_returns_413_without_generation(self):
        # No Content-Length: exercise the receive limit after multipart parsing starts.
        async def body():
            yield b'--synthetic\r\nContent-Disposition: form-data; name="prompt"\r\n\r\nedit\r\n'
            yield b'--synthetic\r\nContent-Disposition: form-data; name="image"; filename="synthetic.png"\r\nContent-Type: image/png\r\n\r\n'
            yield png() + b"x" * (1024 * 1024 + 1024)
            yield b"\r\n--synthetic--\r\n"

        # Avoid the SDK capture hook buffering this deliberately chunked request.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://api.example.test"
        ) as transport:
            with patch.object(openai_images, "TOTAL_IMAGE_LIMIT", 128):
                response = await transport.post(
                    "/v1/images/edits",
                    content=body(),
                    headers={
                        "Authorization": f"Bearer {API_KEY}",
                        "Content-Type": "multipart/form-data; boundary=synthetic",
                    },
                )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("error", response.json())
        self.assertTrue(response.headers["x-request-id"].startswith("req_"))
        self.assertEqual(self.handler.calls, [])


if __name__ == "__main__":
    unittest.main()
