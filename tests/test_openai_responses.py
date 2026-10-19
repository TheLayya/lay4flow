import base64
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api import routes
from src.api.openai_responses import create_responses_router
from src.core.auth import AuthManager


class SyntheticHandler:
    async def handle_generation(self, **kwargs):
        if kwargs["stream"]:
            yield "data: " + json.dumps(
                {
                    "choices": [{"message": {"content": "streamed"}}],
                    "media": {
                        "type": "image",
                        "url": "https://asset.test/image.png",
                        "mime_type": "image/png",
                    },
                }
            )
            return
        yield json.dumps(
            {
                "choices": [{"message": {"content": "generated"}}],
                "media": {
                    "type": "image",
                    "url": "https://asset.test/image.png",
                    "mime_type": "image/png",
                },
            }
        )


class FailingStreamHandler:
    async def handle_generation(self, **kwargs):
        yield "data: " + json.dumps(
            {"error": {"message": "Synthetic failure", "code": "upstream_failed"}}
        )


class RepeatedMediaStreamHandler:
    async def handle_generation(self, **kwargs):
        media = {
            "type": "image",
            "url": "https://asset.test/repeated.png",
            "mime_type": "image/png",
        }
        for text in ("first", "second"):
            yield "data: " + json.dumps(
                {"choices": [{"message": {"content": text}}], "media": media}
            )


class VideoStreamHandler:
    async def handle_generation(self, **kwargs):
        yield "data: " + json.dumps(
            {
                "choices": [{"message": {"content": "<video src='https://asset.test/x.mp4'>"}}],
                "media": {
                    "type": "video",
                    "url": "https://asset.test/x.mp4",
                    "mime_type": "video/mp4",
                    "media_id": "v1",
                },
            }
        )


class VideoResponseHandler:
    async def handle_generation(self, **kwargs):
        yield json.dumps(
            {
                "choices": [{"message": {"content": "<video src='https://asset.test/x.mp4'>"}}],
                "media": {
                    "type": "video",
                    "url": "https://asset.test/x.mp4",
                    "mime_type": "video/mp4",
                    "media_id": "v1",
                },
            }
        )


class RaisingStreamHandler:
    async def handle_generation(self, **kwargs):
        yield "data: " + json.dumps({"choices": [{"message": {"content": "before failure"}}]})
        raise RuntimeError("secret upstream detail")


class FailingResponseHandler:
    async def handle_generation(self, **kwargs):
        yield json.dumps(
            {
                "error": {
                    "message": "Synthetic capacity exhausted",
                    "code": "capacity_exhausted",
                    "status_code": 503,
                }
            }
        )


class OpenAIResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(create_responses_router())
        self.patches = [
            patch.object(routes, "generation_handler", SyntheticHandler()),
            patch.object(AuthManager, "verify_api_key", side_effect=lambda key: key == "key"),
        ]
        for binding in self.patches:
            binding.start()
        self.addCleanup(lambda: [binding.stop() for binding in self.patches])
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://api.example.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_text_and_image_input_returns_response_shape(self):
        result = await self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer key"},
            json={
                "model": "gemini-3.1-flash-image",
                "instructions": "Be concise",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "cat"}],
                    }
                ],
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["output_text"], "generated")
        self.assertEqual(body["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(body["output"][1]["type"], "image_generation_call")
        self.assertEqual(body["output"][1]["result"], "https://asset.test/image.png")

    async def test_top_level_input_items_are_accepted(self):
        result = await self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer key"},
            json={
                "model": "gemini-3.1-flash-image",
                "input": [
                    {"type": "input_text", "text": "cat"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,"
                        + base64.b64encode(b"reference").decode(),
                    },
                ],
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["output_text"], "generated")

    async def test_stream_returns_response_events(self):
        result = await self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer key"},
            json={"model": "gemini-3.1-flash-image", "input": "cat", "stream": True},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("response.created", result.text)
        self.assertIn("response.output_text.delta", result.text)
        self.assertIn("response.output_text.done", result.text)
        self.assertIn("response.output_item.added", result.text)
        self.assertIn("image_generation_call", result.text)
        self.assertIn("response.completed", result.text)
        completed = json.loads(
            result.text.split("event: response.completed\ndata: ", 1)[1].split("\n\n", 1)[0]
        )
        self.assertEqual(
            completed["response"]["output"][0]["content"][0]["text"],
            "streamed",
        )

    async def test_unsupported_tools_are_rejected(self):
        result = await self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer key"},
            json={
                "model": "gemini-3.1-flash-image",
                "input": "cat",
                "tools": [{"type": "function"}],
            },
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("tools", result.json()["error"]["message"])

    async def test_media_generation_parameters_are_rejected_instead_of_ignored(self):
        for field, value in (("max_output_tokens", 20), ("temperature", 0.2)):
            with self.subTest(field=field):
                result = await self.client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer key"},
                    json={"model": "gemini-3.1-flash-image", "input": "cat", field: value},
                )
                self.assertEqual(result.status_code, 400)
                self.assertIn(field, result.json()["error"]["message"])

    async def test_unknown_responses_fields_are_rejected_instead_of_ignored(self):
        result = await self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer key"},
            json={
                "model": "gemini-3.1-flash-image",
                "input": "cat",
                "audio": {"voice": "alloy"},
            },
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("audio", result.json()["error"]["message"])

    def test_openapi_rejects_unknown_responses_fields(self):
        schema = self.app.openapi()["components"]["schemas"]["ResponsesRequest"]
        self.assertFalse(schema["additionalProperties"])

    async def test_empty_tools_are_rejected_as_unsupported(self):
        result = await self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer key"},
            json={"model": "gemini-3.1-flash-image", "input": "cat", "tools": []},
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("tools", result.json()["error"]["message"])

    async def test_stream_failure_emits_failed_response_not_completed(self):
        with patch.object(routes, "generation_handler", FailingStreamHandler()):
            result = await self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer key"},
                json={"model": "gemini-3.1-flash-image", "input": "cat", "stream": True},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("response.failed", result.text)
        self.assertIn("event: error", result.text)
        self.assertNotIn("response.completed", result.text)

    async def test_non_stream_failure_preserves_http_status(self):
        with patch.object(routes, "generation_handler", FailingResponseHandler()):
            result = await self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer key"},
                json={"model": "gemini-3.1-flash-image", "input": "cat"},
            )
        self.assertEqual(result.status_code, 503, result.text)
        self.assertEqual(result.json()["error"]["code"], "capacity_exhausted")

    async def test_stream_deduplicates_repeated_media_items(self):
        with patch.object(routes, "generation_handler", RepeatedMediaStreamHandler()):
            result = await self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer key"},
                json={"model": "gemini-3.1-flash-image", "input": "cat", "stream": True},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.text.count("event: response.output_item.added"), 1)
        self.assertEqual(result.text.count("https://asset.test/repeated.png"), 3)

    async def test_stream_exception_emits_safe_failed_response(self):
        with patch.object(routes, "generation_handler", RaisingStreamHandler()):
            result = await self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer key"},
                json={"model": "gemini-3.1-flash-image", "input": "cat", "stream": True},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("response.failed", result.text)
        self.assertIn("Internal server error", result.text)
        self.assertNotIn("secret upstream detail", result.text)
        self.assertNotIn("response.completed", result.text)

    async def test_non_stream_maps_video_media_to_output_item(self):
        with patch.object(routes, "generation_handler", VideoResponseHandler()):
            result = await self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer key"},
                json={"model": "veo_3_1", "input": "cat"},
            )
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        video = next(item for item in body["output"] if item["type"] == "video_generation_call")
        self.assertEqual(video["result"], "https://asset.test/x.mp4")
        self.assertEqual(video["media_id"], "v1")

    async def test_stream_preserves_video_media_extension(self):
        with patch.object(routes, "generation_handler", VideoStreamHandler()):
            result = await self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer key"},
                json={"model": "veo_3_1", "input": "cat", "stream": True},
            )
        self.assertEqual(result.status_code, 200, result.text)
        completed = json.loads(
            result.text.split("event: response.completed\ndata: ", 1)[1].split("\n\n", 1)[0]
        )
        self.assertEqual(completed["response"]["media"]["type"], "video")
        self.assertEqual(completed["response"]["media"]["media_id"], "v1")


if __name__ == "__main__":
    unittest.main()
