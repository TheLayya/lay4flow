import base64
import unittest

from src.api import routes
from src.core.models import (
    ChatCompletionRequest,
    ChatMessage,
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
    GeminiInlineData,
)


VIDEO = base64.b64encode(b"fake-video").decode("ascii")


class _Handler:
    def __init__(self):
        self.calls = []

    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        yield '{"choices": [{"message": {"content": "ok"}}]}'


class VideoEditRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_mp4_trim_cannot_exceed_source_duration(self):
        movie_header = bytearray(32)
        movie_header[4:8] = b"mvhd"
        movie_header[8] = 0
        movie_header[20:24] = (1000).to_bytes(4, "big")
        movie_header[24:28] = (4000).to_bytes(4, "big")
        request = GeminiGenerateContentRequest(
            contents=[
                GeminiContent(
                    parts=[
                        GeminiPart(text="edit"),
                        GeminiPart(
                            inlineData=GeminiInlineData(
                                mimeType="video/mp4",
                                data=base64.b64encode(bytes(movie_header)).decode(),
                            )
                        ),
                    ]
                )
            ],
            generationConfig={
                "videoConfig": {
                    "mode": "edit",
                    "trimStartMs": 0,
                    "trimEndMs": 5000,
                }
            },
        )
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_gemini_request("omni_1.1_flash", request)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("4000 ms", str(ctx.exception.detail))

    async def test_gemini_keeps_text_before_and_after_video(self):
        request = GeminiGenerateContentRequest(
            contents=[
                GeminiContent(
                    parts=[
                        GeminiPart(text="before"),
                        GeminiPart(inlineData=GeminiInlineData(mimeType="video/mp4", data=VIDEO)),
                        GeminiPart(text="after"),
                    ]
                )
            ]
        )
        normalized = await routes._normalize_gemini_request("omni_edit", request)
        self.assertEqual(normalized.prompt, "before\nafter")
        self.assertEqual(normalized.video_bytes, b"fake-video")
        self.assertEqual(normalized.video_mime_type, "video/mp4")

    async def test_two_gemini_videos_are_rejected(self):
        request = GeminiGenerateContentRequest(
            contents=[
                GeminiContent(
                    parts=[
                        GeminiPart(inlineData=GeminiInlineData(mimeType="video/mp4", data=VIDEO)),
                        GeminiPart(inlineData=GeminiInlineData(mimeType="video/mp4", data=VIDEO)),
                    ]
                )
            ]
        )
        with self.assertRaises(routes.HTTPException):
            await routes._normalize_gemini_request("omni_edit", request)

    async def test_openai_video_url_is_rejected_for_non_edit_model(self):
        request = ChatCompletionRequest(
            model="omni",
            messages=[
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "edit"},
                        {
                            "type": "video_url",
                            "video_url": {"url": f"data:video/mp4;base64,{VIDEO}"},
                        },
                    ],
                )
            ],
        )
        with self.assertRaises(routes.HTTPException):
            await routes._normalize_openai_request(request)

    async def test_stream_and_nonstream_forward_video(self):
        handler = _Handler()
        routes.set_generation_handler(handler)
        normalized = routes.NormalizedGenerationRequest(
            "omni_edit", "edit", [], video_bytes=b"v", video_mime_type="video/mp4"
        )
        await routes._collect_non_stream_result(
            normalized.model,
            normalized.prompt,
            normalized.images,
            video_bytes=normalized.video_bytes,
            video_mime_type=normalized.video_mime_type,
        )
        chunks = [chunk async for chunk in routes._iterate_openai_stream(normalized)]
        self.assertEqual(len(handler.calls), 2)
        self.assertTrue(all(call["video_bytes"] == b"v" for call in handler.calls))
        self.assertTrue(all(call["video_mime_type"] == "video/mp4" for call in handler.calls))


if __name__ == "__main__":
    unittest.main()
