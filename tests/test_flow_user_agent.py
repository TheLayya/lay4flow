"""Keep configured and captcha-provided User-Agent values consistent, without HTTP."""

import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import DEFAULT_FLOW_USER_AGENT, config
from src.services.flow_client import FlowClient

CONFIGURED_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
RUNTIME_UA = CONFIGURED_UA.replace("Chrome/151.", "Chrome/152.")
FLOW_URL = "https://aisandbox-pa.googleapis.com/v1/projects/project-1/flowMedia:batchGenerateImages"


class _FakeAsyncSession:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        if url == FLOW_URL:
            payload = {"media": []}
        else:
            raise AssertionError(f"Unexpected request: {url}")
        return SimpleNamespace(
            status_code=200, headers={}, text=json.dumps(payload), json=lambda: payload
        )


class FlowUserAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = {
            "flow": {**config._config["flow"], "user_agent": CONFIGURED_UA},
            "captcha": {
                **config._config["captcha"],
                "captcha_method": "remote_browser",
                "remote_browser_api_key": "test-key",
                "remote_browser_base_url": "https://captcha.invalid",
            },
            "debug": {**config._config.get("debug", {}), "enabled": False},
        }
        config_patch = patch.dict(config._config, settings)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_missing_or_blank_user_agent_uses_chrome_150_default(self):
        self.assertIn("Chrome/150.", DEFAULT_FLOW_USER_AGENT)
        for value in (None, "", " \t "):
            with self.subTest(value=value):
                settings = dict(config._config["flow"])
                if value is None:
                    settings.pop("user_agent", None)
                else:
                    settings["user_agent"] = value
                with patch.dict(config._config, {"flow": settings}):
                    self.assertEqual(config.flow_user_agent, DEFAULT_FLOW_USER_AGENT)
                    self.assertEqual(
                        FlowClient(None)._generate_user_agent("account-1"),
                        DEFAULT_FLOW_USER_AGENT,
                    )

    def test_same_client_and_account_observe_configuration_changes(self):
        client = FlowClient(None)
        self.assertEqual(client._generate_user_agent("account-1"), CONFIGURED_UA)
        with patch.dict(config._config["flow"], {"user_agent": f"  {RUNTIME_UA}  "}):
            self.assertEqual(config.flow_user_agent, RUNTIME_UA)
            self.assertEqual(client._generate_user_agent("account-1"), RUNTIME_UA)
        self.assertEqual(client._generate_user_agent("account-1"), CONFIGURED_UA)

    async def test_captcha_and_submission_share_effective_user_agent(self):
        for solution_user_agent in (None, RUNTIME_UA):
            with self.subTest(solution_user_agent=solution_user_agent):
                client = FlowClient(None)
                session = _FakeAsyncSession()
                client._call_remote_browser_service = AsyncMock(
                    return_value={
                        "token": "test-captcha",
                        "session_id": "test-session",
                        "fingerprint": {"user_agent": solution_user_agent},
                    }
                )
                with patch("src.services.flow_client.AsyncSession", return_value=session):
                    token, browser_id = await client._get_recaptcha_token("project-1")
                    self.assertEqual(token, "test-captcha")
                    self.assertEqual(browser_id, "test-session")
                    await client._make_request(
                        method="POST",
                        url=FLOW_URL,
                        json_data={"clientContext": {"projectId": "project-1"}},
                        use_at=True,
                        at_token="test-access-token",
                    )

                self.assertEqual(len(session.calls), 1)
                self.assertEqual(
                    client._call_remote_browser_service.await_args.kwargs["json_data"],
                    {
                        "project_id": "project-1",
                        "action": "IMAGE_GENERATION",
                        "token_id": None,
                    },
                )
                headers = session.calls[-1][1]["headers"]
                effective_user_agent = solution_user_agent or CONFIGURED_UA
                self.assertEqual(headers["User-Agent"], effective_user_agent)
                # Remote client hints are exact worker values, never inferred here.
                self.assertNotIn("sec-ch-ua", headers)

    async def test_runtime_fingerprint_takes_priority_over_configuration(self):
        client = FlowClient(None)
        client._set_request_fingerprint(
            {
                "_source": "remote_browser",
                "user_agent": RUNTIME_UA,
                "sec_ch_ua": '"Chromium";v="152"',
            }
        )
        session = _FakeAsyncSession()
        with patch("src.services.flow_client.AsyncSession", return_value=session):
            await client._make_request(method="POST", url=FLOW_URL, json_data={})
        headers = session.calls[0][1]["headers"]
        self.assertEqual(headers["User-Agent"], RUNTIME_UA)
        self.assertIn('"Chromium";v="152"', headers["sec-ch-ua"])

    async def test_actual_request_uses_same_chrome_release_for_headers_and_transport(self):
        for major, runtime in ((150, False), (146, True)):
            ua = DEFAULT_FLOW_USER_AGENT.replace("Chrome/150.", f"Chrome/{major}.")
            client = FlowClient(None)
            if runtime:
                client._set_request_fingerprint(
                    {
                        "_source": "remote_browser",
                        "user_agent": ua,
                        "accept_language": "ja-JP,ja;q=0.9",
                        "sec_ch_ua": '"Chromium";v="146"',
                    }
                )
            session = _FakeAsyncSession()
            with (
                self.subTest(major=major),
                patch.dict(config._config["flow"], {"user_agent": DEFAULT_FLOW_USER_AGENT}),
                patch("src.services.flow_client.AsyncSession", return_value=session),
            ):
                await client._make_request(method="POST", url=FLOW_URL, json_data={})
            request = session.calls[0][1]
            self.assertEqual(request["impersonate"], f"chrome{major}")
            self.assertEqual(request["headers"]["User-Agent"], ua)
            if runtime:
                self.assertIn(f'"Chromium";v="{major}"', request["headers"]["sec-ch-ua"])
            else:
                self.assertNotIn("sec-ch-ua", request["headers"])
            if runtime:
                self.assertEqual(request["headers"]["Accept-Language"], "ja-JP,ja;q=0.9")
            self.assertEqual(client._resolve_runtime_impersonate(), f"chrome{major}")

    async def test_video_upload_start_and_finalize_share_the_current_identity(self):
        ua = DEFAULT_FLOW_USER_AGENT
        client = FlowClient(None)
        client._set_request_fingerprint(
            {
                "_source": "remote_browser",
                "user_agent": ua,
                "accept_language": "en-US,en;q=0.9",
            }
        )
        client._make_video_api_request = AsyncMock()
        media = {
            "name": "synthetic-video",
            "projectId": "project-1",
            "mediaMetadata": {
                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"}
            },
            "video": {"userUploadedVideo": {}},
        }
        post = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    status_code=200,
                    headers={"X-Goog-Upload-URL": "https://upload.invalid/finalize"},
                ),
                SimpleNamespace(status_code=200, json=lambda: {"media": media}),
            ]
        )
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=SimpleNamespace(post=post))
        session.__aexit__ = AsyncMock(return_value=False)
        with patch("src.services.flow_client.AsyncSession", return_value=session):
            await client.upload_video("synthetic-at", b"synthetic-video", project_id="project-1")
        self.assertEqual(post.await_count, 2)
        for call in post.await_args_list:
            self.assertEqual(call.kwargs["impersonate"], "chrome150")
            self.assertEqual(call.kwargs["headers"]["User-Agent"], ua)
            self.assertEqual(call.kwargs["headers"]["Accept-Language"], "en-US,en;q=0.9")


if __name__ == "__main__":
    unittest.main()
