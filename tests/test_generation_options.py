import json
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler, MODEL_CONFIG


class GenerationOptionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.token = types.SimpleNamespace(
            id=1,
            email="test@example.com",
            at="test-at",
            user_paygate_tier="PAYGATE_TIER_NOT_PAID",
            video_concurrency=1,
        )
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler.flow_client = types.SimpleNamespace(
            upload_video=AsyncMock(return_value="video-asset"),
            generate_video_edit=AsyncMock(
                return_value={"operations": [{"operation": {"name": "edit-task"}}]}
            ),
            prefill_remote_browser_pool=AsyncMock(),
        )
        self.handler.db = types.SimpleNamespace(create_task=AsyncMock())
        self.handler.load_balancer = types.SimpleNamespace(
            select_token=AsyncMock(return_value=self.token),
            release_pending=AsyncMock(),
        )
        self.handler.token_manager = types.SimpleNamespace(
            ensure_valid_token=AsyncMock(return_value=self.token),
            ensure_project_exists=AsyncMock(return_value="project-1"),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        self.handler._update_request_log_progress = AsyncMock()
        self.handler._log_request = AsyncMock()

        async def finish_poll(*args, **kwargs):
            args[5]["success"] = True
            yield '{"result":"mock-video"}'

        self.handler._poll_video_result = finish_poll

    async def test_invalid_trim_is_rejected_before_account_or_captcha(self):
        for model, options in (
            ("omni_4s", {"trimStartMs": 0, "trimEndMs": 4000}),
            ("omni_edit", {"trimStartMs": -1, "trimEndMs": 4000}),
            ("omni_edit", {"trimStartMs": 4000, "trimEndMs": 4000}),
            ("omni_edit", {"trimStartMs": 0, "trimEndMs": 10001}),
            ("omni_edit", {"trimStartMs": True, "trimEndMs": 4000}),
            ("omni_edit", {"trimStartMs": 0, "trimEndMs": "4000"}),
        ):
            with self.subTest(model=model, options=options):
                chunks = [
                    chunk
                    async for chunk in self.handler.handle_generation(
                        model,
                        "Edit the video",
                        video_bytes=b"video",
                        generation_options=options,
                    )
                ]
                self.assertEqual(json.loads(chunks[0])["error"]["status_code"], 400)
        self.handler.load_balancer.select_token.assert_not_awaited()
        self.handler.flow_client.prefill_remote_browser_pool.assert_not_awaited()
        self.handler.flow_client.upload_video.assert_not_awaited()

    async def test_edit_dispatch_preserves_trim_and_public_model_in_logs(self):
        options = {"trimStartMs": 2000, "trimEndMs": 6000}
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_edit_360p",
                "Change the cup to pink",
                video_bytes=b"source",
                video_mime_type="video/webm",
                generation_options=options,
                response_model="omni_1.1_flash",
            )
        ]
        self.assertEqual(chunks, ['{"result":"mock-video"}'])
        self.handler.flow_client.upload_video.assert_awaited_once_with(
            "test-at",
            b"source",
            "video/webm",
            project_id="project-1",
            trim_start_ms=2000,
            trim_end_ms=6000,
        )
        submit = self.handler.flow_client.generate_video_edit.await_args.kwargs
        self.assertEqual(submit["model_key"], "abra_edit_360p")
        self.assertEqual(submit["video_media_id"], "video-asset")
        self.assertEqual(self.handler.db.create_task.await_args.args[0].model, "omni_1.1_flash")
        logged_request = self.handler._log_request.await_args.args[2]
        logged_response = self.handler._log_request.await_args.args[3]
        for payload in (logged_request, logged_response, logged_response["performance"]):
            self.assertEqual(payload["model"], "omni_1.1_flash")
            self.assertEqual(payload["resolved_model"], "omni_edit_360p")
        self.assertEqual(logged_request["generation_options"], options)
        self.assertEqual(options, {"trimStartMs": 2000, "trimEndMs": 6000})

    async def test_default_edit_trim_remains_first_ten_seconds(self):
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_edit",
                "Edit",
                video_bytes=b"source",
            )
        ]
        self.assertEqual(chunks, ['{"result":"mock-video"}'])
        self.assertEqual(
            self.handler.flow_client.upload_video.await_args.kwargs,
            {"project_id": "project-1", "trim_start_ms": 0, "trim_end_ms": 10000},
        )

    async def test_upload_trim_uses_relative_30fps_edit_range(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("recaptcha-token", "browser-1"))
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._make_video_api_request = AsyncMock(
            return_value={"operations": [{"operation": {"name": "edit-task"}}]}
        )
        self.handler.flow_client.generate_video_edit = client.generate_video_edit
        for options, upload_range, end_frame in (
            ({"trimStartMs": 0, "trimEndMs": 10000}, (0, 10000), 300),
            ({"trimStartMs": 2000, "trimEndMs": 8000}, (2000, 8000), 180),
            ({"trimStartMs": 0, "trimEndMs": 6750}, (0, 6750), 203),
            (None, (0, 10000), 300),
        ):
            with self.subTest(options=options):
                chunks = [
                    chunk
                    async for chunk in self.handler.handle_generation(
                        "omni_edit_360p",
                        "Edit",
                        video_bytes=b"source",
                        generation_options=options,
                    )
                ]
                self.assertEqual(chunks, ['{"result":"mock-video"}'])
                upload = self.handler.flow_client.upload_video.await_args.kwargs
                self.assertEqual((upload["trim_start_ms"], upload["trim_end_ms"]), upload_range)
                payload = client._make_video_api_request.await_args.kwargs["json_data"]
                self.assertEqual(
                    payload["requests"][0]["videoInput"],
                    {
                        "mediaId": "video-asset",
                        "startFrameIndex": 0,
                        "endFrameIndex": end_frame,
                    },
                )

    async def test_existing_video_id_keeps_default_edit_range(self):
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_edit",
                "Edit",
                video_media_id="existing-video",
            )
        ]
        self.assertEqual(chunks, ['{"result":"mock-video"}'])
        self.handler.flow_client.upload_video.assert_not_awaited()
        submit = self.handler.flow_client.generate_video_edit.await_args.kwargs
        self.assertEqual(submit["video_media_id"], "existing-video")
        self.assertEqual((submit["trim_start_ms"], submit["trim_end_ms"]), (0, 10000))
        self.assertFalse(submit.get("uploaded_video", False))

    def _use_processing_upload(self, initial_status="PENDING"):
        client = FlowClient(proxy_manager=None)
        client._build_current_flow_media_headers = MagicMock(return_value={})
        client._resolve_runtime_impersonate = MagicMock(return_value="chrome")
        client._make_video_api_request = AsyncMock()
        media = {
            "name": "video-asset",
            "projectId": "project-1",
            "mediaMetadata": {
                "mediaStatus": {
                    "mediaGenerationStatus": f"MEDIA_GENERATION_STATUS_{initial_status}"
                }
            },
            "video": {"userUploadedVideo": {}},
        }
        post = AsyncMock(
            side_effect=[
                types.SimpleNamespace(
                    status_code=200, headers={"X-Goog-Upload-URL": "https://example.com/upload"}
                ),
                types.SimpleNamespace(status_code=200, json=lambda: {"media": media}),
            ]
        )
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=types.SimpleNamespace(post=post))
        session.__aexit__ = AsyncMock(return_value=False)
        upload_patch = patch("src.services.flow_client.AsyncSession", return_value=session)
        upload_patch.start()
        self.addCleanup(upload_patch.stop)
        sleep_patch = patch("src.services.flow_client.asyncio.sleep", new_callable=AsyncMock)
        sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        output_patch = patch("builtins.print")
        output_patch.start()
        self.addCleanup(output_patch.stop)
        self.handler.flow_client.upload_video = client.upload_video
        return client, media, sleep

    async def test_upload_processing_finishes_before_edit_submission(self):
        client, media, sleep = self._use_processing_upload()
        ready = {
            **media,
            "mediaMetadata": {
                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"}
            },
        }
        client._make_video_api_request.side_effect = [{"media": [media]}, {"media": [ready]}]

        async def submit_edit(**kwargs):
            self.assertEqual(client._make_video_api_request.await_count, 2)
            self.assertEqual(sleep.await_count, 2)
            return {"operations": [{"operation": {"name": "edit-task"}}]}

        self.handler.flow_client.generate_video_edit.side_effect = submit_edit
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_edit",
                "Edit",
                video_bytes=b"source",
            )
        ]
        self.assertEqual(chunks, ['{"result":"mock-video"}'])
        self.handler.flow_client.generate_video_edit.assert_awaited_once()
        for poll in client._make_video_api_request.await_args_list:
            self.assertTrue(
                poll.kwargs["url"].endswith("/video:batchCheckAsyncVideoGenerationStatus")
            )
            self.assertEqual(
                poll.kwargs["json_data"],
                {"media": [{"name": "video-asset", "projectId": "project-1"}]},
            )

    async def test_failed_upload_processing_prevents_edit_submission(self):
        client, media, sleep = self._use_processing_upload()
        client._make_video_api_request.return_value = {
            "media": [
                {
                    **media,
                    "mediaMetadata": {
                        "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_FAILED"}
                    },
                }
            ]
        }
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_edit",
                "Edit",
                video_bytes=b"source",
            )
        ]
        self.assertTrue(any("upload processing failed" in chunk for chunk in chunks))
        self.handler.flow_client.generate_video_edit.assert_not_awaited()
        self.assertEqual(client._make_video_api_request.await_count, 1)
        sleep.assert_awaited_once_with(5)

    async def test_cancelled_upload_fails_without_polling(self):
        client, _, sleep = self._use_processing_upload("CANCELED")
        with self.assertRaisesRegex(RuntimeError, "upload processing failed"):
            await client.upload_video("test-at", b"source", project_id="project-1")
        client._make_video_api_request.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_upload_processing_timeout_is_bounded(self):
        client, media, sleep = self._use_processing_upload("UNSPECIFIED")
        client._make_video_api_request.return_value = {"media": [media]}
        with self.assertRaisesRegex(TimeoutError, "60 status checks"):
            await client.upload_video("test-at", b"source", project_id="project-1")
        self.assertEqual(client._make_video_api_request.await_count, 60)
        self.assertEqual(sleep.await_count, 60)

    async def test_uploaded_media_without_status_waits_for_playable_url(self):
        client, media, sleep = self._use_processing_upload()
        media.pop("mediaMetadata")
        client._make_video_api_request.return_value = {
            "media": [
                {
                    **media,
                    "video": {"userUploadedVideo": {"fifeUrl": "https://example.com/video.mp4"}},
                }
            ]
        }
        self.assertEqual(
            await client.upload_video("test-at", b"source", project_id="project-1"), "video-asset"
        )
        self.assertEqual(client._make_video_api_request.await_count, 1)
        sleep.assert_awaited_once_with(5)

    async def test_upload_processing_operations_keep_project_for_next_poll(self):
        client, _, sleep = self._use_processing_upload()
        client._make_video_api_request.side_effect = [
            {
                "operations": [
                    {
                        "operation": {"name": "video-asset"},
                        "status": f"MEDIA_GENERATION_STATUS_{status}",
                    }
                ]
            }
            for status in ("PENDING", "SUCCESSFUL")
        ]
        self.assertEqual(
            await client.upload_video("test-at", b"source", project_id="project-1"), "video-asset"
        )
        self.assertEqual(sleep.await_count, 2)
        for poll in client._make_video_api_request.await_args_list:
            self.assertEqual(
                poll.kwargs["json_data"],
                {"media": [{"name": "video-asset", "projectId": "project-1"}]},
            )

    async def test_upload_thumbnail_url_does_not_skip_processing(self):
        client, media, sleep = self._use_processing_upload()
        media["thumbnail"] = {"fifeUrl": "https://example.com/thumbnail.jpg"}
        client._make_video_api_request.side_effect = [
            {"media": [media]},
            {
                "media": [
                    {
                        **media,
                        "video": {
                            "userUploadedVideo": {"fifeUrl": "https://example.com/video.mp4"}
                        },
                    }
                ]
            },
        ]
        self.assertEqual(
            await client.upload_video("test-at", b"source", project_id="project-1"), "video-asset"
        )
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(client._make_video_api_request.await_count, 2)

    async def test_existing_video_id_cannot_silently_ignore_trim(self):
        chunks = [
            chunk
            async for chunk in self.handler.handle_generation(
                "omni_edit",
                "Edit",
                video_media_id="existing-video",
                generation_options={"trimStartMs": 0, "trimEndMs": 4000},
            )
        ]
        self.assertEqual(json.loads(chunks[0])["error"]["status_code"], 400)
        self.handler.load_balancer.select_token.assert_not_awaited()

    async def test_edit_rejects_more_than_five_references_before_upload(self):
        chunks = [
            chunk
            async for chunk in self.handler._handle_video_generation(
                self.token,
                "project-1",
                MODEL_CONFIG["omni_edit"],
                "Edit",
                [b"image"] * 6,
                False,
                video_bytes=b"source",
            )
        ]
        self.assertEqual(json.loads(chunks[0])["error"]["status_code"], 400)
        self.handler.flow_client.upload_video.assert_not_awaited()
        self.handler.flow_client.generate_video_edit.assert_not_awaited()


class VideoUploadTrimTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_trim_never_starts_upload(self):
        client = FlowClient(proxy_manager=None)
        with patch("src.services.flow_client.AsyncSession") as session:
            for start, end in (
                (-1, 1000),
                (0, -1),
                (1000, 1000),
                (2000, 1000),
                (0, 10001),
                (False, 1000),
                (0.5, 1000),
                (0, "1000"),
            ):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    await client.upload_video(
                        "at", b"v", project_id="project-1", trim_start_ms=start, trim_end_ms=end
                    )
            session.assert_not_called()

    async def test_valid_trim_reaches_upload_headers_without_clamping(self):
        client = FlowClient(proxy_manager=None)
        client._build_current_flow_media_headers = MagicMock(return_value={})
        client._resolve_runtime_impersonate = MagicMock(return_value="chrome")
        post = AsyncMock(
            side_effect=[
                types.SimpleNamespace(
                    status_code=200, headers={"X-Goog-Upload-URL": "https://example.com/upload"}
                ),
                types.SimpleNamespace(status_code=200, json=lambda: {"mediaId": "uploaded-video"}),
            ]
        )
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=types.SimpleNamespace(post=post))
        session.__aexit__ = AsyncMock(return_value=False)
        with patch("src.services.flow_client.AsyncSession", return_value=session):
            media_id = await client.upload_video(
                "at", b"v", project_id="project-1", trim_start_ms=12000, trim_end_ms=16000
            )
        self.assertEqual(media_id, "uploaded-video")
        headers = post.await_args_list[0].kwargs["headers"]
        self.assertEqual((headers["x-trim-start"], headers["x-trim-end"]), ("12000", "16000"))
        self.assertEqual(post.await_args_list[1].kwargs["data"], b"v")

    async def test_upload_retries_tls_handshake_once_per_stage(self):
        client = FlowClient(proxy_manager=None)
        client._build_current_flow_media_headers = MagicMock(return_value={})
        client._resolve_runtime_impersonate = MagicMock(return_value="chrome")
        post = AsyncMock(
            side_effect=[
                RuntimeError("Failed to perform, curl: (35) SSL_ERROR_SYSCALL"),
                types.SimpleNamespace(
                    status_code=200, headers={"X-Goog-Upload-URL": "https://example.com/upload"}
                ),
                RuntimeError("Failed to perform, curl: (35) SSL_ERROR_SYSCALL"),
                types.SimpleNamespace(status_code=200, json=lambda: {"mediaId": "uploaded-video"}),
            ]
        )
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=types.SimpleNamespace(post=post))
        session.__aexit__ = AsyncMock(return_value=False)
        with patch("src.services.flow_client.AsyncSession", return_value=session):
            self.assertEqual(
                await client.upload_video("at", b"v", project_id="project-1"), "uploaded-video"
            )
        self.assertEqual(post.await_count, 4)


if __name__ == "__main__":
    unittest.main()
