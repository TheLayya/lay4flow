import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient


class ImageUpsampleCaptchaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(proxy_manager=None, db=object())
        self.client._get_recaptcha_token = AsyncMock(return_value=("captcha-token", "browser-1"))
        self.client._make_request = AsyncMock(return_value={"encodedImage": "image-base64"})
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        self.client._handle_retryable_generation_error = AsyncMock(return_value=False)
        self.config = self.enterContext(patch("src.services.flow_client.config"))
        self.config.captcha_method = "remote_browser"
        self.config.flow_max_retries = 1
        self.config.upsample_timeout = 275
        self.config.flow_image_request_timeout = 40
        self.config.flow_image_timeout_retry_count = 3
        self.config.flow_image_timeout_retry_delay = 0

    async def upsample(self, resolution="UPSAMPLE_IMAGE_RESOLUTION_2K"):
        return await self.client.upsample_image(
            at="test-at",
            project_id="project-1",
            media_id="media-1",
            target_resolution=resolution,
            user_paygate_tier="PAYGATE_TIER_ONE",
            session_id=";session-1",
            token_id=7,
        )

    async def test_direct_captcha_modes_keep_context_headers_and_upsample_timeout(self):
        for method in ("remote_browser",):
            with self.subTest(method=method):
                self.config.captcha_method = method
                self.client._make_request.reset_mock()
                self.assertEqual(
                    await self.upsample("UPSAMPLE_IMAGE_RESOLUTION_4K"), "image-base64"
                )
                self.client._make_request.assert_awaited_once()
                request = self.client._make_request.await_args.kwargs
                self.assertEqual(
                    request["headers"], self.client._build_labs_request_context_headers("project-1")
                )
                self.assertEqual(request["headers"]["Origin"], self.client.flow_base_url)
                self.assertEqual(request["timeout"], 275)
                self.assertEqual(request["at_token"], "test-at")
                self.assertTrue(request["use_at"])
                self.assertEqual(
                    request["json_data"],
                    {
                        "mediaId": "media-1",
                        "targetResolution": "UPSAMPLE_IMAGE_RESOLUTION_4K",
                        "clientContext": {
                            "recaptchaContext": {
                                "token": "captcha-token",
                                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                            },
                            "sessionId": ";session-1",
                            "projectId": "project-1",
                            "tool": "PINHOLE",
                            "userPaygateTier": "PAYGATE_TIER_ONE",
                        },
                    },
                )

    async def test_remote_browser_keeps_captcha_fingerprint_route(self):
        self.config.captcha_method = "remote_browser"
        self.config.flow_image_timeout_use_media_proxy_fallback = True
        self.config.flow_image_prefer_media_proxy = True
        self.client.proxy_manager = SimpleNamespace(
            get_media_proxy_url=AsyncMock(return_value="http://media-proxy.test"),
        )
        fingerprint = {
            "user_agent": "captcha-browser-agent",
            "proxy_url": "http://captcha-proxy.test",
        }
        self.client._set_request_fingerprint(fingerprint)
        self.assertEqual(await self.upsample(), "image-base64")
        request = self.client._make_request.await_args.kwargs
        self.assertFalse(request["use_media_proxy"])
        self.assertTrue(request["respect_fingerprint_proxy"])
        self.assertEqual(self.client.get_request_fingerprint(), fingerprint)

    async def test_upsample_does_not_add_image_http_timeout_retries(self):
        self.config.captcha_method = "remote_browser"
        self.client._make_request.side_effect = TimeoutError("request timed out")
        with self.assertRaises(TimeoutError):
            await self.upsample()
        self.client._make_request.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
