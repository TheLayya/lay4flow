"""Exercise media transports that bypass the shared JSON request helper, offline."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import config
from src.services.flow_client import FlowClient


class RemoteBrowserMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                config._config,
                {
                    "captcha": {**config._config["captcha"], "captcha_method": "remote_browser"},
                    "debug": {**config._config.get("debug", {}), "enabled": False},
                },
            )
        )
        self.proxy = SimpleNamespace(
            get_request_proxy_url=AsyncMock(return_value="http://account.proxy:8080")
        )
        self.client = FlowClient(self.proxy)
        self.client.labs_base_url = "https://labs.google/fx/api"
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.__aexit__ = AsyncMock(return_value=False)
        self.session.post = AsyncMock()
        self.session.get = AsyncMock()
        self.enterContext(patch("src.services.flow_client.AsyncSession", return_value=self.session))
        self.identity = {
            "user_agent": "Mozilla/5.0 Chrome/149.0.0.0",
            "accept_language": "en-GB,en;q=0.8",
            "sec_ch_ua": '"Odd Brand";v="88", "Chromium";v="149"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        }

    async def solve(self, cookies):
        self.client._call_remote_browser_service = AsyncMock(
            return_value={
                "success": True,
                "token": "synthetic-captcha",
                "session_id": "synthetic-session",
                "fingerprint": {
                    **self.identity,
                    "session_cookies": cookies,
                    "proxy_url": "http://solver.proxy:8080",
                    "proxyUrl": "http://solver.proxy:8080",
                    "proxy": "http://solver.proxy:8080",
                },
            }
        )
        await self.client._get_recaptcha_token("project-1", token_id=1)

    def assert_identity(self, request):
        headers = {key.lower(): value for key, value in request.kwargs["headers"].items()}
        for key, field in (
            ("user-agent", "user_agent"),
            ("accept-language", "accept_language"),
            ("sec-ch-ua", "sec_ch_ua"),
            ("sec-ch-ua-mobile", "sec_ch_ua_mobile"),
            ("sec-ch-ua-platform", "sec_ch_ua_platform"),
        ):
            self.assertEqual(headers[key], self.identity[field])
        self.assertEqual(request.kwargs["proxy"], "http://account.proxy:8080")
        self.assertFalse(request.kwargs["default_headers"])
        return headers

    async def test_video_upload_rescopes_bundle_for_start_and_finalize(self):
        await self.solve(
            [
                {
                    "name": "start",
                    "value": "start-only",
                    "domain": ".google.com",
                    "path": "/upload",
                },
                {
                    "name": "finalize",
                    "value": "finalize-only",
                    "domain": "storage.googleapis.com",
                    "path": "/finalize",
                },
                {
                    "name": "wrong_path",
                    "value": "exclude",
                    "domain": "storage.googleapis.com",
                    "path": "/other",
                },
            ]
        )
        upload_url = "https://storage.googleapis.com/finalize/video-1"
        self.session.post.side_effect = [
            SimpleNamespace(status_code=200, headers={"X-Goog-Upload-URL": upload_url}),
            SimpleNamespace(status_code=200, json=lambda: {"mediaId": "uploaded-media"}),
        ]
        self.assertEqual(
            await self.client.upload_video(
                "account-access-token",
                b"synthetic-video",
                project_id="project-1",
            ),
            "uploaded-media",
        )
        start, finish = self.session.post.await_args_list
        start_headers, finish_headers = self.assert_identity(start), self.assert_identity(finish)
        self.assertEqual(start_headers["cookie"], "start=start-only")
        self.assertEqual(finish_headers["cookie"], "finalize=finalize-only")
        for headers in (start_headers, finish_headers):
            self.assertEqual(headers["authorization"], "Bearer account-access-token")
        self.assertEqual(finish.args[0], upload_url)
        self.assertEqual(finish.kwargs["data"], b"synthetic-video")

    async def test_media_redirect_preserves_login_cookie_and_logs_no_cookie_values(self):
        await self.solve(
            [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "solver-login",
                    "domain": "labs.google",
                    "path": "/",
                },
                {
                    "name": "NID",
                    "value": "scoped-cookie",
                    "domain": "labs.google",
                    "path": "/fx/api",
                },
                {"name": "google_only", "value": "exclude", "domain": ".google.com", "path": "/"},
                {
                    "name": "wrong_path",
                    "value": "exclude",
                    "domain": "labs.google",
                    "path": "/fx/other",
                },
            ]
        )
        target = "https://storage.googleapis.com/media/video-1"
        self.session.get.return_value = SimpleNamespace(
            status_code=302,
            headers={"Location": target, "Set-Cookie": "secret-response-cookie"},
            text="secret-response-body",
        )
        with (
            patch.dict(config._config["debug"], {"enabled": True}),
            patch("src.services.flow_client.debug_logger") as logger,
        ):
            self.assertEqual(
                await self.client.get_media_url_redirect("account-login", "video-1"), target
            )
        headers = self.assert_identity(self.session.get.await_args)
        self.assertEqual(
            headers["cookie"], "__Secure-next-auth.session-token=account-login; NID=scoped-cookie"
        )
        self.assertFalse(self.session.get.await_args.kwargs["allow_redirects"])
        logged = str(logger.mock_calls)
        self.assertNotIn("secret-response-cookie", logged)
        self.assertNotIn("secret-response-body", logged)
        self.assertNotIn("account-login", logged)
        self.assertNotIn("scoped-cookie", logged)


if __name__ == "__main__":
    unittest.main()
