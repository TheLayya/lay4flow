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
        proxy = type("ProxyError", (Exception,), {})
        error = proxy("SOCKS5 connection failed")
        error.code = 97
        self.assertTrue(retry.is_proxy_connection_error(error))
        self.assertTrue(retry.is_proxy_handshake_error(error))
        self.assertTrue(retry.is_retryable_network_error("proxy handshake error (curl: (97))"))
        self.assertTrue(retry.should_fallback_to_urllib("curl: (7)"))
        self.assertFalse(retry.is_timeout_error(None))
        self.assertFalse(retry.is_retryable_network_error(None))
        self.assertFalse(retry.should_fallback_to_urllib(None))
        with self.assertRaises(AttributeError):
            self.client._get_retry_reason(None)

    def test_budget_follows_visible_setting_for_all_generation_errors(self):
        with patch("src.services.flow_client.config") as config:
            config.browser_captcha_generation_retries = 10
            for error in (None, "timeout", "PUBLIC_ERROR", "Failed to obtain reCAPTCHA token"):
                self.assertEqual(self.client._resolve_generation_retry_budget(3, error), 3)
            for error in ("reCAPTCHA evaluation failed", "recaptcha 验证失败"):
                self.assertEqual(self.client._resolve_generation_retry_budget(3, error), 3)
                self.assertEqual(self.client._resolve_generation_retry_budget(12, error), 12)
            self.assertEqual(
                self.client._resolve_generation_retry_budget(3, "RECAPTCHA 验证失败"), 3
            )
            config.browser_captcha_generation_retries = 0
            self.assertEqual(
                self.client._resolve_generation_retry_budget(1, "recaptcha evaluation failed"), 1
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
        self.assertEqual(self.client._get_retry_delay_seconds("HTTP 429 too many requests", 0), 1)
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
        self.config.video_timeout = 1500
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

    async def test_generation_errors_keep_configured_attempt_count(self):
        for error, attempts, delay in (
            ("reCAPTCHA evaluation failed", 2, 2),
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

    async def test_video_upsample_recaptcha_403_retries_with_new_captcha_and_trace(self):
        self.client._warmup_flow_video_frontend_context = AsyncMock()
        self.client._normalize_video_generation_response = Mock(return_value={"operations": []})
        self.client._make_video_api_request = AsyncMock(
            side_effect=[RuntimeError("HTTP Error 403: reCAPTCHA evaluation failed"), {}]
        )
        result = await self.video()
        self.assertEqual(result["operations"], [])
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        submitted = self.client._make_video_api_request.await_args_list
        self.assertNotEqual(
            submitted[0].kwargs["json_data"]["clientContext"]["recaptchaContext"]["token"],
            submitted[1].kwargs["json_data"]["clientContext"]["recaptchaContext"]["token"],
        )
        self.assertTrue(
            all(
                call.kwargs["url"].endswith("video:batchAsyncGenerateVideoUpsampleVideo")
                for call in submitted
            )
        )
        trace = result["flow_retry_trace"]
        self.assertEqual(trace["max_retries"], 2)
        self.assertEqual(len(trace["generation_attempts"]), 2)
        self.assertFalse(trace["generation_attempts"][0]["success"])
        self.assertTrue(trace["generation_attempts"][0]["recaptcha_ok"])
        self.assertTrue(trace["generation_attempts"][1]["success"])
        self.assertNotIn("token", repr(trace).lower())

    async def test_video_upsample_exhaustion_attaches_trace_without_regenerating(self):
        self.client._warmup_flow_video_frontend_context = AsyncMock()
        self.client._make_video_api_request = AsyncMock(
            side_effect=RuntimeError("HTTP Error 403: reCAPTCHA evaluation failed")
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP Error 403") as raised:
            await self.video()
        trace = raised.exception.flow_retry_trace
        self.assertEqual(len(trace["generation_attempts"]), 2)
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.assertEqual(self.client._make_video_api_request.await_count, 2)
        self.assertEqual(self.client._warmup_flow_video_frontend_context.await_count, 1)
        self.assertTrue(all(not item["success"] for item in trace["generation_attempts"]))

    async def test_video_upsample_transient_captcha_errors_exhaust_visible_budget(self):
        self.config.flow_max_retries = 6
        error = RuntimeError("provider-secret socks5://user:pass@private.test:1080 token=private")
        self.client._get_recaptcha_token.side_effect = error
        self.client._make_video_api_request = AsyncMock()
        self.client._report_mint_source_rejection = AsyncMock()
        self.client._release_video_launch_gate = AsyncMock()

        async def wait(_):
            # Backoff releases the scarce launch slot before waiting.
            self.assertEqual(
                self.client._release_video_launch_gate.await_count,
                self.client._get_recaptcha_token.await_count,
            )

        self.sleep.side_effect = wait
        with self.assertRaises(RuntimeError) as raised:
            await self.video()
        trace = raised.exception.flow_retry_trace
        self.assertEqual(self.client._get_recaptcha_token.await_count, 6)
        self.assertEqual(len(trace["generation_attempts"]), 6)
        self.assertEqual(self.sleep.await_count, 5)
        self.client._make_video_api_request.assert_not_awaited()
        self.client._report_mint_source_rejection.assert_not_awaited()
        for item in trace["generation_attempts"]:
            self.assertEqual(
                (item["stage"], item["submitted"], item["reason"]),
                ("captcha", False, "provider_error"),
            )
        self.assertNotIn("private", repr(trace))
        self.assertNotIn("provider-secret", repr(trace))

    async def test_video_upsample_captcha_pool_state_controls_wait_and_retry(self):
        self.config.flow_max_retries = 6
        for reason, retryable, expected_attempts, delay in (
            ("pool_empty", False, 1, None),
            ("pool_unhealthy", False, 1, None),
            ("pool_cooling", True, 6, 75),
            ("pool_busy", True, 6, 2),
        ):
            with self.subTest(reason=reason):
                self.client._get_recaptcha_token.reset_mock()
                self.sleep.reset_mock()
                error = RuntimeError("sanitized captcha provider unavailable")
                error.captcha_failure_reason = reason
                error.retryable = retryable
                if delay == 75:
                    error.retry_after = 75
                self.client._get_recaptcha_token.side_effect = error
                self.client._make_video_api_request = AsyncMock()
                with self.assertRaises(RuntimeError) as raised:
                    await self.video()
                trace = raised.exception.flow_retry_trace
                self.assertEqual(self.client._get_recaptcha_token.await_count, expected_attempts)
                self.assertEqual(len(trace["generation_attempts"]), expected_attempts)
                self.assertTrue(
                    all(item["reason"] == reason for item in trace["generation_attempts"])
                )
                self.client._make_video_api_request.assert_not_awaited()
                self.assertEqual(self.sleep.await_count, expected_attempts - 1)
                if delay is not None:
                    self.assertTrue(
                        all(call.args == (delay,) for call in self.sleep.await_args_list)
                    )

    async def test_video_upsample_cooling_does_not_wait_past_deadline(self):
        self.config.video_timeout = 10
        error = RuntimeError("captcha pool cooling")
        error.captcha_failure_reason = "pool_cooling"
        error.retryable = True
        error.retry_after = 60
        self.client._get_recaptcha_token.side_effect = error
        self.client._make_video_api_request = AsyncMock()
        with self.assertRaises(RuntimeError):
            await self.video()
        self.client._get_recaptcha_token.assert_awaited_once()
        self.client._make_video_api_request.assert_not_awaited()
        self.sleep.assert_not_awaited()

    async def test_video_upsample_retries_acquisition_then_submits_only_fresh_token(self):
        self.client._get_recaptcha_token.side_effect = [
            RuntimeError("transient captcha provider failure"),
            ("fresh-captcha", "session/fresh"),
        ]
        self.client._warmup_flow_video_frontend_context = AsyncMock()
        self.client._normalize_video_generation_response = Mock(return_value={"operations": []})
        self.client._make_video_api_request = AsyncMock(return_value={})
        self.client.generate_video_text = AsyncMock()
        result = await self.video()
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.client._make_video_api_request.assert_awaited_once()
        payload = self.client._make_video_api_request.await_args.kwargs["json_data"]
        self.assertEqual(payload["clientContext"]["recaptchaContext"]["token"], "fresh-captcha")
        self.assertEqual(payload["requests"][0]["videoInput"]["mediaId"], "media")
        self.client.generate_video_text.assert_not_awaited()
        history = result["flow_retry_trace"]["generation_attempts"]
        self.assertEqual(
            [(item["stage"], item["submitted"]) for item in history],
            [("captcha", False), ("submission", True)],
        )
        self.assertNotIn("fresh-captcha", repr(history))

    async def test_video_upsample_protocol_missing_token_is_acquisition_failure(self):
        self.config.captcha_method = "protocol"
        self.client._get_recaptcha_token.side_effect = [(None, None), ("fresh", None)]
        self.client._warmup_flow_video_frontend_context = AsyncMock()
        self.client._normalize_video_generation_response = Mock(return_value={"operations": []})
        self.client._make_video_api_request = AsyncMock(return_value={})
        result = await self.video()
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.client._make_video_api_request.assert_awaited_once()
        self.assertEqual(
            result["flow_retry_trace"]["generation_attempts"][0]["reason"], "provider_error"
        )

    async def test_video_upsample_submission_rejection_then_empty_pool_stops_without_third_submit(
        self,
    ):
        self.config.flow_max_retries = 6
        self.client._get_recaptcha_token.side_effect = [
            ("captcha-one", "session/one"),
            ("captcha-two", "session/two"),
            RuntimeError("Protocol captcha proxy pool is empty; add egress proxies first"),
        ]
        self.client._warmup_flow_video_frontend_context = AsyncMock()
        self.client._make_video_api_request = AsyncMock(
            side_effect=[
                RuntimeError("HTTP Error 403: reCAPTCHA evaluation failed"),
                RuntimeError("HTTP Error 403: reCAPTCHA evaluation failed"),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "proxy pool is empty") as raised:
            await self.video()
        trace = raised.exception.flow_retry_trace
        self.assertEqual(self.client._get_recaptcha_token.await_count, 3)
        self.assertEqual(self.client._make_video_api_request.await_count, 2)
        self.assertEqual(len(trace["generation_attempts"]), 3)
        self.assertEqual(
            [
                (item["stage"], item["submitted"], item["reason"])
                for item in trace["generation_attempts"]
            ],
            [
                ("submission", True, "recaptcha_rejected"),
                ("submission", True, "recaptcha_rejected"),
                ("captcha", False, "pool_empty"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
