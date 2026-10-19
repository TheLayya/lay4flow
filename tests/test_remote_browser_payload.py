"""Verify the deployed captcha-proxy protocol without network requests."""

import unittest
from unittest.mock import AsyncMock, patch

from src.core.config import REMOTE_BROWSER_BASE_URL, Config, config
from src.services.flow_client import FlowClient


class RemoteBrowserPayloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                config._config,
                {
                    "captcha": {
                        **config._config["captcha"],
                        "captcha_method": "remote_browser",
                        "remote_browser_base_url": "http://solver.example:8881",
                        "remote_browser_api_key": "synthetic-solver-key",
                        "remote_browser_timeout": 60,
                    },
                    "debug": {**config._config.get("debug", {}), "enabled": False},
                },
            )
        )
        self.client = FlowClient(None)

    def test_legacy_file_and_database_urls_keep_the_fixed_provider(self):
        with patch.object(
            Config,
            "_load_config",
            return_value={"captcha": {"remote_browser_base_url": "https://old.invalid"}},
        ):
            settings = Config()
        self.assertEqual(settings.remote_browser_base_url, REMOTE_BROWSER_BASE_URL)
        settings.set_remote_browser_base_url("https://database.invalid")
        self.assertEqual(settings.remote_browser_base_url, REMOTE_BROWSER_BASE_URL)
        self.assertEqual(
            settings.get_raw_config()["captcha"]["remote_browser_base_url"], REMOTE_BROWSER_BASE_URL
        )

    async def solve(self, response):
        self.client._call_remote_browser_service = AsyncMock(return_value=response)
        return await self.client._get_recaptcha_token(
            "project-123", action="IMAGE_GENERATION", token_id=42
        )

    async def test_exact_solve_payload_and_bearer_authentication(self):
        response = {
            "success": True,
            "token": "remote-token",
            "session_id": "remote-session",
            "fingerprint": {"user_agent": "remote-user-agent"},
        }
        self.client._sync_json_http_request = AsyncMock(return_value=(200, response, ""))
        result = await self.client._get_recaptcha_token(
            "project-123", action="IMAGE_GENERATION", token_id=42
        )
        self.assertEqual(result, ("remote-token", "remote-session"))
        self.client._sync_json_http_request.assert_awaited_once_with(
            method="POST",
            url=f"{REMOTE_BROWSER_BASE_URL}/api/v1/solve",
            headers={"Authorization": "Bearer synthetic-solver-key"},
            payload={"project_id": "project-123", "action": "IMAGE_GENERATION", "token_id": 42},
            timeout=35,
        )

    async def test_nested_valid_values_win_and_empty_values_fall_back_to_top_level(self):
        cookies = [{"name": "NID", "value": "new-cookie", "domain": ".google.com", "path": "/"}]
        await self.solve(
            {
                "success": True,
                "token": "remote-token",
                "session_id": "remote-session",
                "fingerprint": {
                    "user_agent": "nested-user-agent",
                    "accept_language": " \t ",
                    "sec_ch_ua": None,
                    "sec_ch_ua_mobile": "?0",
                    "sec_ch_ua_platform": "",
                    "session_cookies": [],
                    "proxy_url": "http://nested.invalid",
                    "proxyUrl": "http://nested.invalid",
                    "proxy": "http://nested.invalid",
                },
                "user_agent": "flat-user-agent",
                "accept_language": "en-GB,en;q=0.8",
                "sec_ch_ua": '"Chromium";v="149", "Odd Brand";v="88"',
                "sec_ch_ua_mobile": "?1",
                "sec_ch_ua_platform": '"Windows"',
                "session_cookies": cookies,
                "proxy_url": "http://flat.invalid",
                "proxyUrl": "http://flat.invalid",
                "proxy": "http://flat.invalid",
            }
        )
        fingerprint = self.client.get_request_fingerprint()
        self.assertEqual(fingerprint["user_agent"], "nested-user-agent")
        self.assertEqual(fingerprint["accept_language"], "en-GB,en;q=0.8")
        self.assertEqual(fingerprint["sec_ch_ua"], '"Chromium";v="149", "Odd Brand";v="88"')
        self.assertEqual(fingerprint["sec_ch_ua_mobile"], "?0")
        self.assertEqual(fingerprint["sec_ch_ua_platform"], '"Windows"')
        self.assertEqual(fingerprint["session_cookies"], cookies)
        for key in ("proxy_url", "proxyUrl", "proxy"):
            self.assertNotIn(key, fingerprint)

    async def test_nonempty_nested_cookies_take_precedence(self):
        nested = [{"name": "NID", "value": "nested", "domain": ".google.com", "path": "/"}]
        await self.solve(
            {
                "token": "remote-token",
                "session_id": "remote-session",
                "fingerprint": {"session_cookies": nested},
                "session_cookies": [
                    {"name": "NID", "value": "flat", "domain": ".google.com", "path": "/"}
                ],
            }
        )
        self.assertEqual(self.client.get_request_fingerprint()["session_cookies"], nested)

    async def test_flat_fields_with_missing_or_null_fingerprint(self):
        for envelope in ({}, {"fingerprint": None}):
            with self.subTest(envelope=envelope):
                fields = {
                    "user_agent": "flat-user-agent",
                    "accept_language": "de-DE,de;q=0.9",
                    "sec_ch_ua": '"Chromium";v="150"',
                    "sec_ch_ua_mobile": "?0",
                    "sec_ch_ua_platform": '"Windows"',
                    "session_cookies": [
                        {"name": "NID", "value": "flat", "domain": ".google.com", "path": "/"}
                    ],
                }
                result = await self.solve(
                    {"token": "remote-token", "session_id": "remote-session", **envelope, **fields}
                )
                self.assertEqual(result, ("remote-token", "remote-session"))
                for field, value in fields.items():
                    self.assertEqual(self.client.get_request_fingerprint()[field], value)

    async def test_invalid_nested_types_do_not_hide_valid_top_level_fields(self):
        await self.solve(
            {
                "token": "remote-token",
                "session_id": "remote-session",
                "fingerprint": {"user_agent": [], "accept_language": 9, "sec_ch_ua": {}},
                "user_agent": "flat-user-agent",
                "accept_language": "fr-FR",
                "sec_ch_ua": '"Chromium";v="150"',
            }
        )
        fingerprint = self.client.get_request_fingerprint()
        self.assertEqual(fingerprint["user_agent"], "flat-user-agent")
        self.assertEqual(fingerprint["accept_language"], "fr-FR")
        self.assertEqual(fingerprint["sec_ch_ua"], '"Chromium";v="150"')

    async def test_session_error_and_finish_callbacks_keep_the_same_session(self):
        _, session_id = await self.solve({"token": "remote-token", "session_id": "session/one"})
        self.client._call_remote_browser_service.reset_mock()
        await self.client._notify_browser_captcha_error(session_id, error_reason="recaptcha_failed")
        await self.client._notify_browser_captcha_request_finished(session_id)
        calls = self.client._call_remote_browser_service.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].kwargs,
            {
                "method": "POST",
                "path": "/api/v1/sessions/session%2Fone/error",
                "json_data": {"error_reason": "recaptcha_failed"},
                "timeout_override": 2,
            },
        )
        self.assertEqual(
            calls[1].kwargs,
            {
                "method": "POST",
                "path": "/api/v1/sessions/session%2Fone/finish",
                "json_data": {"status": "success"},
                "timeout_override": 2,
            },
        )


if __name__ == "__main__":
    unittest.main()
