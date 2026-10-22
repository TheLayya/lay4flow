"""Cloak routes stay bound to their account and never enter legacy Bearer paths."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient


class FlowClientModernRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client.token_manager = object()
        self.client._make_request = AsyncMock(side_effect=AssertionError("legacy REST request"))
        self.client._make_video_api_request = AsyncMock(
            side_effect=AssertionError("legacy video request")
        )
        self.client._get_recaptcha_token = AsyncMock(
            side_effect=AssertionError("client acquired legacy captcha")
        )
        self.modern = SimpleNamespace(
            generate_video=AsyncMock(return_value={"operations": [{"modern": True}]}),
            generate_video_text=AsyncMock(return_value={"operations": [{"text": True}]}),
            get_media=AsyncMock(
                return_value={
                    "name": "media",
                    "video": {"generatedVideo": {"fifeUrl": "https://media.example/a.mp4"}},
                }
            ),
            check_video_status=AsyncMock(return_value={"operations": []}),
            upload_video=AsyncMock(return_value="uploaded-media"),
        )
        self.token = SimpleNamespace(
            id=7, email="fixture@example.test", browser_profile_source="cloak"
        )

    async def test_all_modern_video_routes_bypass_legacy_at_and_keep_inputs(self):
        common = dict(
            at="", project_id="project", prompt="prompt", model_key="model", aspect_ratio="16:9"
        )
        with patch(
            "src.services.browser.flow_generation.FlowProfileGeneration", return_value=self.modern
        ) as factory:
            with self.client.account_request_token(self.token):
                await self.client.generate_video_reference_images(
                    **common, reference_images=[{"mediaId": "ref"}]
                )
                await self.client.generate_video_start_end(
                    **common, start_media_id="first", end_media_id="last"
                )
                await self.client.generate_video_start_image(**common, start_media_id="first")
                await self.client.generate_video_extend(**common, video_media_id="source")
                await self.client.upsample_video(
                    at="",
                    project_id="project",
                    model_key="up",
                    aspect_ratio="16:9",
                    video_media_id="source",
                    resolution="VIDEO_RESOLUTION_1080P",
                )
                await self.client.generate_omni_reference_video(
                    at="",
                    st="",
                    project_id="project",
                    prompt="prompt",
                    aspect_ratio="16:9",
                    reference_media_ids=["ref"],
                    model_usage_key="abra_r2v_8s",
                )
                await self.client.generate_video_text(**common)
        calls = self.modern.generate_video.await_args_list
        self.assertEqual(
            [call.kwargs["mode"] for call in calls],
            ["references", "start_end", "start", "extend", "upsample", "references"],
        )
        self.assertEqual(calls[0].kwargs["reference_images"], [{"mediaId": "ref"}])
        self.assertEqual(calls[1].kwargs["start_media_id"], "first")
        self.assertEqual(calls[1].kwargs["end_media_id"], "last")
        self.assertEqual(calls[3].kwargs["video_media_id"], "source")
        self.assertEqual(calls[4].kwargs["resolution"], 2)
        self.assertEqual(calls[5].kwargs["model_name"], "abra_r2v_8s")
        self.modern.generate_video_text.assert_awaited_once_with(
            "project", "prompt", "model", "16:9"
        )
        self.assertTrue(all(call.args[2] is self.token for call in factory.call_args_list))
        self.client._make_request.assert_not_awaited()
        self.client._make_video_api_request.assert_not_awaited()
        self.client._get_recaptcha_token.assert_not_awaited()

    async def test_edit_uses_precise_existing_metadata_frame_range(self):
        self.modern.check_video_status.return_value = {
            "media": [
                {
                    "name": "source",
                    "mediaMetadata": {
                        "mediaStatus": {
                            "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                        }
                    },
                    "video": {"generatedVideo": {}, "dimensions": {"length": "4s"}},
                }
            ]
        }
        with patch(
            "src.services.browser.flow_generation.FlowProfileGeneration", return_value=self.modern
        ):
            with self.client.account_request_token(self.token):
                await self.client.generate_video_edit(
                    at="",
                    project_id="p",
                    prompt="edit",
                    model_key="m",
                    aspect_ratio="16:9",
                    video_media_id="source",
                    trim_start_ms=1000,
                    trim_end_ms=7000,
                )
        call = self.modern.generate_video.await_args.kwargs
        self.assertEqual(call["mode"], "edit")
        self.assertEqual(call["trim_start_frame"], 24)
        self.assertEqual(call["trim_end_frame"], 96)
        self.modern.check_video_status.assert_awaited_once_with(
            [{"mediaName": "source", "projectId": "p"}]
        )

    async def test_video_upload_uses_profile_and_waits_for_modern_processing(self):
        self.client._wait_for_uploaded_video = AsyncMock()
        with patch(
            "src.services.browser.flow_generation.FlowProfileGeneration", return_value=self.modern
        ):
            with self.client.account_request_token(self.token):
                result = await self.client.upload_video(
                    None, b"fixture-video", "video/mp4", "project", 0, 1000
                )
        self.assertEqual(result, "uploaded-media")
        self.modern.upload_video.assert_awaited_once_with(
            "project", b"fixture-video", "video/mp4", 0, 1000
        )
        self.client._wait_for_uploaded_video.assert_awaited_once_with(
            None, "project", "uploaded-media"
        )
        self.client._make_request.assert_not_awaited()

    async def test_modern_media_lookup_redirect_and_poll_never_touch_old_endpoints(self):
        with patch(
            "src.services.browser.flow_generation.FlowProfileGeneration", return_value=self.modern
        ):
            with self.client.account_request_token(self.token):
                self.assertEqual((await self.client.get_media("", "media"))["name"], "media")
                self.assertEqual(
                    await self.client.get_media_url_redirect("", "media"),
                    "https://media.example/a.mp4",
                )
                await self.client.check_video_status("", [{"mediaName": "media", "projectId": "p"}])
        self.assertEqual(self.modern.get_media.await_count, 2)
        self.client._make_request.assert_not_awaited()
        self.client._make_video_api_request.assert_not_awaited()

    async def test_account_context_is_task_local_and_restored_after_nested_scope(self):
        other = SimpleNamespace(id=9, email="other@example.test", browser_profile_source="cloak")
        seen = []

        class Adapter:
            def __init__(inner, client, manager, token):
                inner.token = token

            async def generate_video(inner, **kwargs):
                await asyncio.sleep(0)
                seen.append(
                    (
                        inner.token.id,
                        self.client._account_token_ctx.get().id,
                        self.client._account_proxy_ctx.get(),
                    )
                )
                return {"token": inner.token.id}

        async def run(token, proxy):
            with self.client.account_request_token(token), self.client.account_request_proxy(proxy):
                return await self.client.generate_video_extend("", "p", "x", "m", "16:9", "source")

        with patch("src.services.browser.flow_generation.FlowProfileGeneration", Adapter):
            first, second = await asyncio.gather(
                run(self.token, "http://first.test:8080"), run(other, "http://other.test:8080")
            )
        self.assertEqual(first, {"token": 7})
        self.assertEqual(second, {"token": 9})
        self.assertCountEqual(
            seen, [(7, 7, "http://first.test:8080"), (9, 9, "http://other.test:8080")]
        )
        self.assertIsNone(self.client._account_token_ctx.get())
        self.assertIsNone(self.client._account_proxy_ctx.get())
        with self.client.account_request_token(self.token):
            with self.client.account_request_token(other):
                self.assertIs(self.client._account_token_ctx.get(), other)
            self.assertIs(self.client._account_token_ctx.get(), self.token)
        self.assertIsNone(self.client._account_token_ctx.get())

    def test_non_cloak_and_unscoped_calls_keep_legacy_contract(self):
        self.assertIsNone(self.client._modern_generation())
        with self.client.account_request_token(SimpleNamespace(browser_profile_source="cookies")):
            self.assertIsNone(self.client._modern_generation())


if __name__ == "__main__":
    unittest.main()
