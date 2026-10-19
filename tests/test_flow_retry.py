"""Retry policy keeps legacy decisions, override hooks, and session callbacks."""

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.services.flow import retry
from src.services.flow_client import FlowClient


class FlowRetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.client = FlowClient(None)

    def test_classification_order_and_network_boundaries(self):
        for error, expected in (
            ("", None),
            ("ordinary failure", None),
            ("ERROR_NO_SLOT_AVAILABLE_BLOCK 403", "打码服务资源阻塞"),
            ("ERROR_NO_SLOT_AVAILABLE 429", "打码服务资源不足"),
            ("HTTP 403 timeout recaptcha", "403错误"),
            ("too many requests timeout", "429限流"),
            ("curl: (35) recaptcha", "网络/TLS错误"),
            ("request timed out", "网络/TLS错误"),
            ("PUBLIC_ERROR: reCAPTCHA evaluation failed", "reCAPTCHA 验证失败"),
            ("Failed to obtain reCAPTCHA token", "reCAPTCHA 错误"),
            ("PUBLIC_ERROR", "500/内部错误"),
            ('{"reason":"internal"}', "500/内部错误"),
        ):
            with self.subTest(error=error):
                self.assertEqual(retry.get_retry_reason(error), expected)
                self.assertEqual(self.client._get_retry_reason(error), expected)
        self.assertFalse(retry.is_retryable_network_error("curl: (7)"))
        self.assertTrue(retry.is_proxy_connection_error(Exception("curl: (7)")))
        self.assertTrue(retry.should_fallback_to_urllib("curl: (7)"))
        self.assertFalse(retry.is_timeout_error(None))
        self.assertFalse(retry.is_retryable_network_error(None))
        self.assertFalse(retry.should_fallback_to_urllib(None))
        with self.assertRaises(AttributeError):
            self.client._get_retry_reason(None)

    def test_budget_only_expands_for_evaluation_rejection_and_keeps_fallback(self):
        with patch("src.services.flow_client.config") as config:
            config.browser_captcha_generation_retries = 10
            for error in (None, "timeout", "PUBLIC_ERROR", "Failed to obtain reCAPTCHA token"):
                self.assertEqual(self.client._resolve_generation_retry_budget(3, error), 3)
            for error in ("reCAPTCHA evaluation failed", "recaptcha 验证失败"):
                self.assertEqual(self.client._resolve_generation_retry_budget(3, error), 10)
                self.assertEqual(self.client._resolve_generation_retry_budget(12, error), 12)
            self.assertEqual(
                self.client._resolve_generation_retry_budget(3, "RECAPTCHA 验证失败"), 3
            )
            config.browser_captcha_generation_retries = 0
            self.assertEqual(
                self.client._resolve_generation_retry_budget(1, "recaptcha evaluation failed"), 6
            )
            for base in (None, 0, -2):
                self.assertEqual(self.client._resolve_generation_retry_budget(base), 1)
        # Non-captcha paths did not previously read captcha configuration.
        with patch("src.services.flow_client.config", SimpleNamespace()):
            self.assertEqual(self.client._resolve_generation_retry_budget(2, "timeout"), 2)

    def test_wrapper_overrides_and_slot_backoff_are_preserved(self):
        self.client._is_retryable_network_error = Mock(return_value=True)
        self.assertEqual(self.client._get_retry_reason("custom failure"), "网络/TLS错误")
        self.client._is_retryable_network_error.assert_called_once_with("custom failure")
        self.client._is_retryable_network_error.reset_mock()
        self.assertEqual(self.client._get_retry_reason("403"), "403错误")
        self.client._is_retryable_network_error.assert_not_called()
        for attempt, expected in ((-1, 3), (0, 3), (1, 5), (20, 20)):
            self.assertEqual(
                self.client._get_retry_delay_seconds("ERROR_NO_SLOT_AVAILABLE", attempt), expected
            )
        self.assertEqual(
            self.client._get_retry_delay_seconds("ERROR_NO_SLOT_AVAILABLE_BLOCK", 0), 20
        )
        self.assertEqual(self.client._get_retry_delay_seconds("recaptcha", 0), 2)
        self.assertEqual(self.client._get_retry_delay_seconds(None, 0), 1)

    def test_policy_import_does_not_load_config_or_transport(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['src.core.config'] = None; "
                "sys.modules['curl_cffi'] = None; from src.services.flow.retry import get_retry_reason; "
                "assert get_retry_reason('ordinary failure') is None",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class FlowRetryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(None)
        self.config = self.enterContext(patch("src.services.flow_client.config"))
        self.config.captcha_method = "remote_browser"
        self.config.flow_max_retries = 2
        self.config.browser_captcha_generation_retries = 4
        self.config.upsample_timeout = 275
        self.sleep = self.enterContext(
            patch("src.services.flow_client.asyncio.sleep", new_callable=AsyncMock)
        )
        self.events = []

        async def callback(**kwargs):
            self.events.append(kwargs["path"])
            return {}

        self.client._call_remote_browser_service = AsyncMock(side_effect=callback)

        async def solve(*args, **kwargs):
            index = self.client._get_recaptcha_token.await_count
            self.events.append(f"solve-{index}")
            return f"synthetic-token-{index}", f"session/{index}"

        self.client._get_recaptcha_token = AsyncMock(side_effect=solve)

    async def image(self):
        return await self.client.upsample_image(
            at="synthetic-at",
            project_id="project",
            media_id="media",
            target_resolution="UPSAMPLE_IMAGE_RESOLUTION_2K",
        )

    async def video(self):
        return await self.client.upsample_video(
            at="synthetic-at",
            project_id="project",
            video_media_id="media",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            resolution="VIDEO_RESOLUTION_1080P",
            model_key="veo_3_1_upsampler_1080p",
        )

    async def test_image_retry_reports_error_and_finishes_each_solve(self):
        self.client._make_image_generation_request = AsyncMock(
            side_effect=[TimeoutError("request timed out"), {"encodedImage": "encoded"}]
        )
        self.assertEqual(await self.image(), "encoded")
        self.assertEqual(
            self.events,
            [
                "solve-1",
                "/api/v1/sessions/session%2F1/error",
                "/api/v1/sessions/session%2F1/finish",
                "solve-2",
                "/api/v1/sessions/session%2F2/finish",
            ],
        )
        self.sleep.assert_awaited_once_with(1)
        tokens = [
            call.kwargs["json_data"]["clientContext"]["recaptchaContext"]["token"]
            for call in self.client._make_image_generation_request.await_args_list
        ]
        self.assertEqual(tokens, ["synthetic-token-1", "synthetic-token-2"])

    async def test_recaptcha_error_expands_exact_attempt_count_but_other_errors_do_not(self):
        for error, attempts, delay in (
            ("reCAPTCHA evaluation failed", 4, 2),
            ("PUBLIC_ERROR", 2, 1),
            ("ordinary failure", 1, None),
        ):
            with self.subTest(error=error):
                self.client._get_recaptcha_token.reset_mock()
                self.client._call_remote_browser_service.reset_mock()
                self.sleep.reset_mock()
                self.client._make_image_generation_request = AsyncMock(
                    side_effect=RuntimeError(error)
                )
                with self.assertRaisesRegex(RuntimeError, error):
                    await self.image()
                self.assertEqual(self.client._get_recaptcha_token.await_count, attempts)
                self.assertEqual(self.client._make_image_generation_request.await_count, attempts)
                self.assertEqual(self.client._call_remote_browser_service.await_count, attempts * 2)
                self.assertEqual(self.sleep.await_count, attempts - 1)
                if delay is not None:
                    self.assertTrue(
                        all(call.args == (delay,) for call in self.sleep.await_args_list)
                    )

    async def test_missing_token_retries_without_submission_or_finish(self):
        self.client._get_recaptcha_token.side_effect = [(None, "empty/1"), (None, "empty/2")]
        self.client._make_image_generation_request = AsyncMock()
        with self.assertRaisesRegex(Exception, "Failed to obtain reCAPTCHA token"):
            await self.image()
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.client._make_image_generation_request.assert_not_awaited()
        self.assertEqual(
            self.events,
            [
                "/api/v1/sessions/empty%2F1/error",
                "/api/v1/sessions/empty%2F2/error",
            ],
        )
        self.sleep.assert_awaited_once_with(2)

    async def test_video_keeps_deferred_error_notification_and_finishes_each_attempt(self):
        self.client._warmup_flow_video_frontend_context = AsyncMock()
        self.client._make_video_api_request = AsyncMock(side_effect=RuntimeError("PUBLIC_ERROR"))
        with self.assertRaisesRegex(RuntimeError, "PUBLIC_ERROR"):
            await self.video()
        self.assertEqual(
            self.events,
            [
                "solve-1",
                "/api/v1/sessions/session%2F1/finish",
                "solve-2",
                "/api/v1/sessions/session%2F2/error",
                "/api/v1/sessions/session%2F2/finish",
            ],
        )
        self.assertEqual(self.client._make_video_api_request.await_count, 2)
        self.client._warmup_flow_video_frontend_context.assert_awaited_once()
        self.sleep.assert_awaited_once_with(1)


if __name__ == "__main__":
    unittest.main()
