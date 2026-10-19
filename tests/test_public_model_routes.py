import base64
import json
import unittest
from unittest.mock import patch

from src.api import routes
from src.core.models import ChatCompletionRequest, GeminiGenerateContentRequest


class PublicModelRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_flow_model_metadata_uses_lay4flow_brand(self):
        catalog = await routes.list_models("test-key")
        flow_models = [entry for entry in catalog["data"] if entry.get("provider") == "flow"]
        self.assertTrue(flow_models)
        self.assertEqual({entry["owned_by"] for entry in flow_models}, {"lay4flow"})
        aliases = await routes.list_model_aliases("test-key")
        self.assertEqual({entry["owned_by"] for entry in aliases["data"]}, {"lay4flow"})
        gemini = routes._build_gemini_model_resource("veo_3_1", "Synthetic description")
        self.assertEqual(gemini["version"], "lay4flow")

    def test_internal_response_fallback_and_debug_logger_use_lay4flow_brand(self):
        from src.core.logger import debug_logger
        from src.services.generation_handler import GenerationHandler

        handler = GenerationHandler.__new__(GenerationHandler)
        response = json.loads(handler._create_completion_response("Synthetic availability result"))
        chunk = json.loads(handler._create_stream_chunk("Synthetic progress")[6:])
        self.assertEqual(response["model"], "lay4flow")
        self.assertEqual(chunk["model"], "lay4flow")
        self.assertEqual(debug_logger.logger.name, "lay4flow.debug")

    async def test_public_models_advertise_responses_endpoint(self):
        catalog = await routes.list_models("test-key")
        for model in catalog["data"]:
            if model.get("provider") != "flow":
                continue
            self.assertIn("/v1/responses", model.get("supported_endpoints", []))
            self.assertTrue(model.get("usage"))
            self.assertNotIn("\ufffd", model["usage"])
            self.assertTrue(model.get("recommended_endpoint"))
            self.assertIn(model["recommended_endpoint"], model["supported_endpoints"])

    def test_public_catalog_and_legacy_opt_in(self):
        ids = {x["id"] for x in routes._get_openai_model_catalog()}
        self.assertEqual(
            ids,
            {
                "omni_1.1_flash",
                "veo_3_1",
                "gemini-3.1-flash-image",
                "gemini-3.1-flash-image-lite",
                "gemini-3.0-pro-image",
                "imagen-4.0-generate-preview",
            },
        )
        self.assertEqual(ids, set(routes._get_gemini_model_catalog()))
        veo = next(item for item in routes._get_openai_model_catalog() if item["id"] == "veo_3_1")
        self.assertEqual(
            set(veo["compatibilityAliases"]),
            {"veo_3_1_interpolation_lite", "veo_3_1_remix", "veo_3_1_extend"},
        )
        legacy_remix = next(
            item for item in routes._get_openai_model_catalog(True) if item["id"] == "veo_3_1_remix"
        )
        self.assertIn("历史请求", legacy_remix["usage"])
        self.assertNotIn("使用 extend://", legacy_remix["usage"])
        old_ids = {x["id"] for x in routes._get_openai_model_catalog(True)}
        self.assertTrue(
            {
                "omni",
                "omni_portrait",
                "omni_edit_360p",
                "veo_3_1_interpolation_lite",
                "veo_3_1_remix",
                "veo_3_1_extend",
            }
            <= old_ids
        )

    async def test_openai_and_gemini_choose_identical_variant(self):
        options = {
            "mode": "text_to_video",
            "durationSeconds": 6,
            "resolution": "360p",
            "aspectRatio": "9:16",
        }
        chat = ChatCompletionRequest.model_validate(
            {
                "model": "omni_1.1_flash",
                "messages": [{"role": "user", "content": "A cat"}],
                "extra_body": {"videoConfig": options},
            }
        )
        gemini = GeminiGenerateContentRequest.model_validate(
            {
                "contents": [{"parts": [{"text": "A cat"}]}],
                "generationConfig": {"videoConfig": options},
            }
        )
        a = await routes._normalize_openai_request(chat)
        b = await routes._normalize_gemini_request("omni_1.1_flash", gemini)
        self.assertEqual(a.model, "omni_portrait_6s_360p")
        self.assertEqual((a.model, a.generation_options), (b.model, b.generation_options))
        self.assertEqual(a.public_model, "omni_1.1_flash")

    async def test_uploaded_video_is_resolved_before_edit_validation(self):
        request = ChatCompletionRequest.model_validate(
            {
                "model": "omni_1.1_flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Make the cup pink"},
                            {
                                "type": "video_url",
                                "video_url": {
                                    "url": "data:video/mp4;base64,"
                                    + base64.b64encode(b"video").decode()
                                },
                            },
                        ],
                    }
                ],
                "generationConfig": {
                    "videoConfig": {
                        "mode": "edit",
                        "resolution": "360p",
                        "trimStartMs": 1000,
                        "trimEndMs": 5000,
                    }
                },
            }
        )
        normalized = await routes._normalize_openai_request(request)
        self.assertEqual(normalized.model, "omni_edit_360p")
        self.assertEqual(normalized.generation_options["trimStartMs"], 1000)
        self.assertEqual(normalized.video_bytes, b"video")

    async def test_openai_image_generation_accepts_ten_references_and_rejects_eleven(self):
        image_url = "data:image/png;base64," + base64.b64encode(b"image").decode()

        def request_with_images(count):
            return ChatCompletionRequest.model_validate(
                {
                    "model": "gemini-3.1-flash-image",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Make a collage"},
                                *(
                                    {"type": "image_url", "image_url": {"url": image_url}}
                                    for _ in range(count)
                                ),
                            ],
                        }
                    ],
                }
            )

        normalized = await routes._normalize_openai_request(request_with_images(10))
        self.assertEqual(len(normalized.images), 10)
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_openai_request(request_with_images(11))
        self.assertEqual(ctx.exception.status_code, 400)

        image_data = base64.b64encode(b"image").decode()

        def gemini_request(count):
            return GeminiGenerateContentRequest.model_validate(
                {
                    "contents": [
                        {
                            "parts": [
                                {"text": "Make a collage"},
                                *(
                                    {"inlineData": {"mimeType": "image/png", "data": image_data}}
                                    for _ in range(count)
                                ),
                            ]
                        }
                    ],
                }
            )

        normalized = await routes._normalize_gemini_request(
            "gemini-3.1-flash-image", gemini_request(10)
        )
        self.assertEqual(len(normalized.images), 10)
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_gemini_request("gemini-3.1-flash-image", gemini_request(11))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_openai_omni_reference_to_video_accepts_seven_and_rejects_eight(self):
        image_url = "data:image/png;base64," + base64.b64encode(b"image").decode()

        def request_with_images(count):
            return ChatCompletionRequest.model_validate(
                {
                    "model": "omni_1.1_flash",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Use these references"},
                                *(
                                    {"type": "image_url", "image_url": {"url": image_url}}
                                    for _ in range(count)
                                ),
                            ],
                        }
                    ],
                    "generationConfig": {
                        "videoConfig": {
                            "mode": "reference_to_video",
                            "durationSeconds": 8,
                            "resolution": "720p",
                        }
                    },
                }
            )

        normalized = await routes._normalize_openai_request(request_with_images(7))
        self.assertEqual(len(normalized.images), 7)
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_openai_request(request_with_images(8))
        self.assertEqual(ctx.exception.status_code, 400)

        image_data = base64.b64encode(b"image").decode()

        def gemini_request(count):
            return GeminiGenerateContentRequest.model_validate(
                {
                    "contents": [
                        {
                            "parts": [
                                {"text": "Use these references"},
                                *(
                                    {"inlineData": {"mimeType": "image/png", "data": image_data}}
                                    for _ in range(count)
                                ),
                            ]
                        }
                    ],
                    "generationConfig": {
                        "videoConfig": {
                            "mode": "reference_to_video",
                            "durationSeconds": 8,
                            "resolution": "720p",
                        }
                    },
                }
            )

        normalized = await routes._normalize_gemini_request("omni_1.1_flash", gemini_request(7))
        self.assertEqual(len(normalized.images), 7)
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_gemini_request("omni_1.1_flash", gemini_request(8))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_gemini_omni_edit_accepts_one_video_and_five_references(self):
        video = base64.b64encode(b"video").decode()
        image = base64.b64encode(b"image").decode()
        parts = [
            {"text": "Edit this video"},
            {"inlineData": {"mimeType": "video/mp4", "data": video}},
            *({"inlineData": {"mimeType": "image/png", "data": image}} for _ in range(5)),
        ]
        request = GeminiGenerateContentRequest.model_validate(
            {
                "contents": [{"parts": parts}],
                "generationConfig": {"videoConfig": {"mode": "edit", "resolution": "720p"}},
            }
        )
        normalized = await routes._normalize_gemini_request("omni_1.1_flash", request)
        self.assertEqual(normalized.video_bytes, b"video")
        self.assertEqual(len(normalized.images), 5)

        too_many = GeminiGenerateContentRequest.model_validate(
            {
                "contents": [
                    {
                        "parts": [
                            {"inlineData": {"mimeType": "video/mp4", "data": video}},
                            *(
                                {"inlineData": {"mimeType": "image/png", "data": image}}
                                for _ in range(6)
                            ),
                        ]
                    }
                ],
                "generationConfig": {"videoConfig": {"mode": "edit", "resolution": "720p"}},
            }
        )
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_gemini_request("omni_1.1_flash", too_many)
        self.assertEqual(ctx.exception.status_code, 400)

        video_url = "data:video/mp4;base64," + video
        image_url = "data:image/png;base64," + image
        openai = ChatCompletionRequest.model_validate(
            {
                "model": "omni_1.1_flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Edit this video"},
                            {"type": "video_url", "video_url": {"url": video_url}},
                            *(
                                {"type": "image_url", "image_url": {"url": image_url}}
                                for _ in range(5)
                            ),
                        ],
                    }
                ],
                "generationConfig": {"videoConfig": {"mode": "edit", "resolution": "720p"}},
            }
        )
        normalized = await routes._normalize_openai_request(openai)
        self.assertEqual(normalized.video_bytes, b"video")
        self.assertEqual(len(normalized.images), 5)

    async def test_openai_rejects_two_video_inputs(self):
        video_url = "data:video/mp4;base64," + base64.b64encode(b"video").decode()
        request = ChatCompletionRequest.model_validate(
            {
                "model": "omni_1.1_flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Edit"},
                            {"type": "video_url", "video_url": {"url": video_url}},
                            {"type": "video_url", "video_url": {"url": video_url}},
                        ],
                    }
                ],
                "generationConfig": {"videoConfig": {"mode": "edit"}},
            }
        )
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_openai_request(request)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_invalid_combination_is_http_400(self):
        request = ChatCompletionRequest.model_validate(
            {
                "model": "veo_3_1",
                "messages": [{"role": "user", "content": "A cat"}],
                "generationConfig": {"videoConfig": {"durationSeconds": 10}},
            }
        )
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_openai_request(request)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_response_identity_and_options_reach_both_paths(self):
        calls = []

        class Handler:
            async def handle_generation(self, **kwargs):
                calls.append(kwargs)
                yield json.dumps({"model": "lay4flow", "choices": []})

        options = {"mode": "edit", "trimStartMs": 1000, "trimEndMs": 5000}
        request = routes.NormalizedGenerationRequest(
            "omni_edit_360p",
            "pink",
            [],
            video_bytes=b"video",
            public_model="omni_1.1_flash",
            generation_options=options,
        )
        with patch.object(routes, "generation_handler", Handler()):
            result = await routes._collect_non_stream_result(
                request.model,
                request.prompt,
                [],
                video_bytes=b"video",
                generation_options=options,
                response_model=request.public_model,
            )
            chunks = [x async for x in routes._iterate_openai_stream(request)]
        self.assertEqual(json.loads(result)["model"], request.public_model)
        self.assertEqual(json.loads(chunks[0][6:])["model"], request.public_model)
        self.assertEqual([x["generation_options"] for x in calls], [options, options])

    async def test_legacy_extend_media_id_is_still_accepted(self):
        request = ChatCompletionRequest.model_validate(
            {
                "model": "veo_3_1_extend",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Continue"},
                            {"type": "image_url", "image_url": {"url": "extend://media-1"}},
                        ],
                    }
                ],
            }
        )
        normalized = await routes._normalize_openai_request(request)
        self.assertEqual(normalized.video_media_id, "media-1")
        self.assertEqual(normalized.model, "veo_3_1_extend")

    async def test_legacy_video_does_not_silently_discard_new_parameters(self):
        request = ChatCompletionRequest.model_validate(
            {
                "model": "omni_edit",
                "messages": [{"role": "user", "content": "pink"}],
                "generationConfig": {"videoConfig": {"trimStartMs": 2000, "trimEndMs": 6000}},
            }
        )
        with self.assertRaises(routes.HTTPException) as ctx:
            await routes._normalize_openai_request(request)
        self.assertEqual(ctx.exception.status_code, 400)
