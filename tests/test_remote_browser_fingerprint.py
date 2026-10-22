"""Check remote solve identity at the final REST boundary; all HTTP is mocked."""

import asyncio
import contextvars
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.config import config
from src.services.flow.remote_captcha import RemoteCaptchaError
from src.services.flow_client import FlowClient

FLOW_URL = "https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage"
REMOTE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.1.2 Safari/537.36"
)
REMOTE_CH = '"Chromium";v="149", "Unusual;Brand";v="13", "Google Chrome";v="150"'
LOCAL_PROXY = "http://account-proxy.invalid:8080"


class FakeSession:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        payload = {"encodedImage": "synthetic-image"}
        return SimpleNamespace(
            status_code=200, headers={}, text=json.dumps(payload), json=lambda: payload
        )


class RemoteBrowserFingerprintTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                config._config,
                {
                    "captcha": {
                        **config._config["captcha"],
                        "captcha_method": "remote_browser",
                        "remote_browser_base_url": "https://solver.invalid",
                        "remote_browser_api_key": "test-key",
                    },
                    "debug": {**config._config.get("debug", {}), "enabled": False},
                },
            )
        )
        self.proxy = SimpleNamespace(get_request_proxy_url=AsyncMock(return_value=LOCAL_PROXY))
        self.client = FlowClient(self.proxy)
        self.session = FakeSession()
        self.enterContext(patch("src.services.flow_client.AsyncSession", return_value=self.session))

    async def solve(self, fingerprint=None, **response_fields):
        self.client._call_remote_browser_service = AsyncMock(
            return_value={
                "success": True,
                "token": "solve-token",
                "session_id": "solve-session",
                "fingerprint": fingerprint,
                **response_fields,
            }
        )
        return await self.client._get_recaptcha_token("project-1", token_id=42)

    async def submit(self, url=FLOW_URL, **kwargs):
        await self.client._make_request(method="POST", url=url, json_data={}, **kwargs)
        return self.session.calls[-1][1]

    def headers(self, request):
        headers = request["headers"]
        self.assertEqual(
            len(headers), len({key.lower() for key in headers}), "duplicate header casing"
        )
        return {key.lower(): value for key, value in headers.items()}

    async def test_final_rest_uses_exact_remote_headers_and_local_proxy(self):
        await self.solve(
            {
                "user_agent": REMOTE_UA,
                "accept_language": "ja-JP,ja;q=0.8",
                "sec_ch_ua": REMOTE_CH,
                "sec_ch_ua_mobile": "?0",
                "sec_ch_ua_platform": '"Windows"',
                "proxy_url": "http://solver-proxy.invalid",
                "proxyUrl": "http://solver-proxy.invalid",
                "proxy": "http://solver-proxy.invalid",
            },
            proxy_url="http://top-level-proxy.invalid",
        )
        request = await self.submit(
            headers={"user-agent": "default-agent", "ACCEPT-LANGUAGE": "default-language"}
        )
        headers = self.headers(request)
        self.assertEqual(headers["user-agent"], REMOTE_UA)
        self.assertEqual(headers["accept-language"], "ja-JP,ja;q=0.8")
        self.assertEqual(headers["sec-ch-ua"], REMOTE_CH)
        self.assertEqual(headers["sec-ch-ua-mobile"], "?0")
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertEqual(request["proxy"], LOCAL_PROXY)
        self.assertFalse(request["default_headers"])
        self.assertFalse(request["allow_redirects"])
        self.proxy.get_request_proxy_url.assert_awaited_once()

    async def test_unsupported_config_override_cannot_redirect_account_proxy(self):
        with patch.dict(config._config["captcha"], {"captcha_method": "browser"}):
            self.client._set_request_fingerprint(
                {
                    "user_agent": "local-browser-agent",
                    "proxy_url": "http://local-browser-proxy.invalid",
                    "session_cookies": {"NID": "local-cookie"},
                }
            )
            request = await self.submit()
        self.assertEqual(request["proxy"], LOCAL_PROXY)
        self.assertEqual(self.headers(request)["cookie"], "NID=local-cookie")
        self.assertFalse(request["default_headers"])

    async def test_missing_remote_ua_uses_current_account_stable_ua(self):
        for fingerprint in (None, {}, {"user_agent": "  "}):
            with self.subTest(fingerprint=fingerprint):
                await self.solve(fingerprint)
                self.client._generate_user_agent = Mock(return_value="stable-account-agent")
                request = await self.submit(use_at=True, at_token="account-42-access-token")
                headers = self.headers(request)
                self.assertEqual(headers["user-agent"], "stable-account-agent")
                self.client._generate_user_agent.assert_called_with("account-42-access-token"[:16])
                self.assertTrue(
                    all(
                        call.args == ("account-42-access-token"[:16],)
                        for call in self.client._generate_user_agent.call_args_list
                    )
                )
                self.assertNotIn("cookie", headers)
                self.assertEqual(request["proxy"], LOCAL_PROXY)

    async def test_missing_remote_ua_ch_is_not_fabricated(self):
        await self.solve({"user_agent": REMOTE_UA})
        headers = self.headers(await self.submit())
        for key in ("sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"):
            self.assertNotIn(key, headers)

    async def test_cookie_domain_path_and_explicit_auth_precedence(self):
        cookies = [
            {"name": "NID", "value": "remote-nid", "domain": ".googleapis.com", "path": "/v1"},
            {
                "name": "SCOPE",
                "value": "matching",
                "domain": "aisandbox-pa.googleapis.com",
                "path": "/v1/flow",
            },
            {"name": "ROOT", "value": "root", "domain": ".googleapis.com", "path": "/"},
            {
                "name": "WRONG_DOMAIN",
                "value": "google-cookie",
                "domain": ".google.com",
                "path": "/",
            },
            {
                "name": "WRONG_PATH",
                "value": "elsewhere",
                "domain": ".googleapis.com",
                "path": "/v2",
            },
            {"name": "UNSCOPED", "value": "unsafe"},
        ]
        await self.solve({"session_cookies": cookies})
        request = await self.submit(
            headers={
                "cOoKiE": "NID=account-nid; ACCOUNT=account-cookie",
                "Authorization": "Bearer account-auth",
            }
        )
        headers = self.headers(request)
        self.assertEqual(headers["authorization"], "Bearer account-auth")
        cookie = dict(part.strip().split("=", 1) for part in headers["cookie"].split(";"))
        self.assertEqual(
            cookie,
            {
                "NID": "account-nid",
                "ACCOUNT": "account-cookie",
                "SCOPE": "matching",
                "ROOT": "root",
            },
        )
        request = await self.submit(url="https://aisandbox-pa.googleapis.com/v10/flow")
        self.assertEqual(self.headers(request).get("cookie"), "ROOT=root")
        request = await self.submit(url="https://evilgoogleapis.com/v1/flow")
        self.assertNotIn("cookie", self.headers(request))

    async def test_cookie_host_only_and_secure_flags_are_respected(self):
        await self.solve(
            {
                "session_cookies": [
                    {"name": "HOST", "value": "host-only", "domain": "googleapis.com", "path": "/"},
                    {
                        "name": "EXPLICIT_HOST",
                        "value": "host-only",
                        "domain": ".googleapis.com",
                        "path": "/",
                        "hostOnly": True,
                    },
                    {
                        "name": "SECURE",
                        "value": "https-only",
                        "domain": ".googleapis.com",
                        "path": "/",
                        "secure": True,
                    },
                ]
            }
        )
        self.assertEqual(self.headers(await self.submit()).get("cookie"), "SECURE=https-only")
        self.assertNotIn(
            "cookie", self.headers(await self.submit(url=FLOW_URL.replace("https:", "http:")))
        )

    async def test_remote_cookies_cannot_replace_explicit_login_token(self):
        await self.solve(
            {
                "session_cookies": [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "remote-login",
                        "domain": ".google.com",
                        "path": "/",
                    },
                    {"name": "NID", "value": "remote-nid", "domain": ".google.com", "path": "/"},
                ]
            }
        )
        request = await self.submit(
            url="https://labs.google.com/fx/api/auth/session", use_st=True, st_token="account-login"
        )
        cookie = self.headers(request)["cookie"]
        self.assertIn("__Secure-next-auth.session-token=account-login", cookie)
        self.assertNotIn("remote-login", cookie)
        self.assertIn("NID=remote-nid", cookie)

    async def test_text_rest_path_uses_the_same_headers_and_cookie_scope(self):
        await self.solve(
            {
                "user_agent": REMOTE_UA,
                "sec_ch_ua": REMOTE_CH,
                "session_cookies": [
                    {"name": "NID", "value": "remote", "domain": ".googleapis.com", "path": "/v1"}
                ],
            },
            proxy="http://ignored.invalid",
        )
        await self.client._make_text_request(
            method="POST",
            url=FLOW_URL,
            json_data={},
            headers={"User-Agent": "default-agent", "COOKIE": "NID=account"},
        )
        request = self.session.calls[-1][1]
        headers = self.headers(request)
        self.assertEqual(headers["user-agent"], REMOTE_UA)
        self.assertEqual(headers["sec-ch-ua"], REMOTE_CH)
        self.assertEqual(headers["cookie"], "NID=account")
        self.assertEqual(request["proxy"], LOCAL_PROXY)
        self.assertFalse(request["allow_redirects"])

    async def test_saved_bundle_is_independent_of_solve_payload_and_returned_snapshot(self):
        fingerprint = {
            "user_agent": "original-agent",
            "session_cookies": [
                {
                    "name": "NID",
                    "value": "original-cookie",
                    "domain": ".googleapis.com",
                    "path": "/",
                }
            ],
        }
        await self.solve(fingerprint)
        fingerprint["user_agent"] = "changed-payload-agent"
        fingerprint["session_cookies"][0]["value"] = "changed-payload-cookie"
        fingerprint["session_cookies"].append(
            {"name": "EXTRA", "value": "payload-extra", "domain": ".googleapis.com", "path": "/"}
        )
        snapshot = self.client.get_request_fingerprint()
        self.assertEqual(snapshot["user_agent"], "original-agent")
        self.assertEqual(snapshot["session_cookies"][0]["value"], "original-cookie")
        snapshot["user_agent"] = "changed-snapshot-agent"
        snapshot["session_cookies"][0]["value"] = "changed-snapshot-cookie"
        snapshot["session_cookies"].append(
            {"name": "EXTRA", "value": "snapshot-extra", "domain": ".googleapis.com", "path": "/"}
        )
        request = await self.submit()
        self.assertEqual(self.headers(request)["user-agent"], "original-agent")
        self.assertEqual(self.headers(request)["cookie"], "NID=original-cookie")

    async def test_every_solve_clears_previous_identity_before_waiting_and_after_failure(self):
        await self.solve(
            {
                "user_agent": "old-user-agent",
                "session_cookies": [
                    {
                        "name": "NID",
                        "value": "old-cookie",
                        "domain": ".googleapis.com",
                        "path": "/",
                    },
                ],
            }
        )
        observed_context = []

        async def next_response(**kwargs):
            observed_context.append(self.client.get_request_fingerprint())
            return {"token": "new-token", "session_id": "new-session", "fingerprint": None}

        self.client._call_remote_browser_service = AsyncMock(side_effect=next_response)
        self.assertEqual(
            await self.client._get_recaptcha_token("project-2", token_id=43),
            ("new-token", "new-session"),
        )
        self.assertEqual(observed_context, [None])
        request = await self.submit()
        self.assertNotEqual(self.headers(request)["user-agent"], "old-user-agent")
        self.assertNotIn("cookie", self.headers(request))

        async def fail_response(**kwargs):
            observed_context.append(self.client.get_request_fingerprint())
            raise RuntimeError("synthetic solver failure")

        self.client._call_remote_browser_service = AsyncMock(side_effect=fail_response)
        with self.assertRaises(RemoteCaptchaError):
            await self.client._get_recaptcha_token("project-3")
        self.assertEqual(observed_context, [None, None])
        self.assertIsNone(self.client.get_request_fingerprint())

    async def test_parallel_accounts_keep_token_headers_cookies_and_proxy_together(self):
        account_context = contextvars.ContextVar("test_account")
        self.proxy.get_request_proxy_url = AsyncMock(
            side_effect=lambda: f"http://proxy-{account_context.get()}.invalid"
        )
        ready = asyncio.Event()
        solve_count = 0

        async def solve_response(method, path, json_data, timeout_override):
            account = json_data["token_id"]
            return {
                "token": f"token-{account}",
                "session_id": f"session-{account}",
                "fingerprint": {
                    "user_agent": f"agent-{account}",
                    "accept_language": f"language-{account}",
                    "sec_ch_ua": f'"Brand-{account}";v="150"',
                    "session_cookies": [
                        {
                            "name": "NID",
                            "value": f"cookie-{account}",
                            "domain": ".googleapis.com",
                            "path": "/",
                        }
                    ],
                },
            }

        self.client._call_remote_browser_service = AsyncMock(side_effect=solve_response)

        async def submit_account(account):
            nonlocal solve_count
            account_context.set(account)
            token, _ = await self.client._get_recaptcha_token(
                f"project-{account}", token_id=account
            )
            solve_count += 1
            if solve_count == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=1)
            await self.client._make_request(
                method="POST",
                url=FLOW_URL,
                json_data={"clientContext": {"recaptchaContext": {"token": token}}},
                use_at=True,
                at_token=f"access-{account}",
            )

        await asyncio.gather(submit_account(1), submit_account(2))
        self.assertIsNone(self.client.get_request_fingerprint())
        self.assertEqual(len(self.session.calls), 2)
        for _, request in self.session.calls:
            account = request["json"]["clientContext"]["recaptchaContext"]["token"].removeprefix(
                "token-"
            )
            headers = self.headers(request)
            self.assertEqual(headers["user-agent"], f"agent-{account}")
            self.assertEqual(headers["accept-language"], f"language-{account}")
            self.assertEqual(headers["sec-ch-ua"], f'"Brand-{account}";v="150"')
            self.assertEqual(headers["cookie"], f"NID=cookie-{account}")
            self.assertEqual(headers["authorization"], f"Bearer access-{account}")
            self.assertEqual(request["proxy"], f"http://proxy-{account}.invalid")

    async def test_generation_retry_uses_new_solve_without_old_cookies(self):
        self.enterContext(patch.dict(config._config["flow"], {"max_retries": 2}))
        responses = iter(
            [
                {
                    "token": "first-token",
                    "session_id": "first-session",
                    "fingerprint": {
                        "user_agent": "first-agent",
                        "session_cookies": [
                            {
                                "name": "NID",
                                "value": "first-cookie",
                                "domain": ".googleapis.com",
                                "path": "/",
                            },
                        ],
                    },
                },
                {"token": "second-token", "session_id": "second-session", "fingerprint": None},
            ]
        )
        observed_context = []

        async def remote_response(**kwargs):
            observed_context.append(self.client.get_request_fingerprint())
            return next(responses)

        self.client._call_remote_browser_service = AsyncMock(side_effect=remote_response)
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        self.client._handle_retryable_generation_error = AsyncMock(return_value=True)
        self.client._generate_user_agent = Mock(return_value="stable-account-agent")
        submit_count = 0

        async def post(url, **kwargs):
            nonlocal submit_count
            submit_count += 1
            result = await FakeSession.post(self.session, url, **kwargs)
            if submit_count == 1:
                return SimpleNamespace(
                    status_code=403,
                    headers={},
                    text='{"error":{"message":"synthetic retry"}}',
                    json=lambda: {"error": {"message": "synthetic retry"}},
                )
            return result

        self.session.post = post
        result = await self.client.upsample_image(
            at="account-access",
            project_id="project-1",
            media_id="media-1",
            token_id=42,
        )
        self.assertEqual(result, "synthetic-image")
        self.assertEqual(observed_context, [None, None])
        self.assertEqual(len(self.session.calls), 2)
        first = self.session.calls[0][1]
        second = self.session.calls[1][1]
        self.assertEqual(first["json"]["clientContext"]["recaptchaContext"]["token"], "first-token")
        self.assertEqual(self.headers(first)["cookie"], "NID=first-cookie")
        self.assertEqual(self.headers(first)["user-agent"], "first-agent")
        self.assertEqual(
            second["json"]["clientContext"]["recaptchaContext"]["token"], "second-token"
        )
        self.assertEqual(self.headers(second)["user-agent"], "stable-account-agent")
        self.assertNotIn("cookie", self.headers(second))
        self.assertEqual(
            [
                call.args
                for call in self.client._notify_browser_captcha_request_finished.await_args_list
            ],
            [("first-session",), ("second-session",)],
        )

    async def test_debug_logs_do_not_contain_token_cookie_or_authorization(self):
        self.enterContext(patch.dict(config._config["debug"], {"enabled": True}))
        logger = self.enterContext(patch("src.services.flow_client.debug_logger"))
        await self.solve(
            {
                "session_cookies": [
                    {
                        "name": "NID",
                        "value": "private-cookie-value",
                        "domain": ".googleapis.com",
                        "path": "/",
                    },
                ]
            },
            token="private-recaptcha-token",
        )
        await self.client._make_request(
            method="POST",
            url=FLOW_URL,
            json_data={"clientContext": {"recaptchaContext": {"token": "private-recaptcha-token"}}},
            use_at=True,
            at_token="private-account-authorization",
        )
        logs = repr(logger.mock_calls)
        for secret in (
            "private-cookie-value",
            "private-recaptcha-token",
            "private-account-authorization",
            "test-key",
        ):
            self.assertNotIn(secret, logs)

    async def test_malformed_solve_does_not_log_partial_secrets(self):
        logger = self.enterContext(patch("src.services.flow_client.debug_logger"))
        self.client._call_remote_browser_service = AsyncMock(
            return_value={
                "token": "private-partial-token",
                "fingerprint": {
                    "session_cookies": [
                        {
                            "name": "NID",
                            "value": "private-partial-cookie",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                },
            }
        )
        with self.assertRaises(RemoteCaptchaError):
            await self.client._get_recaptcha_token("project-1")
        self.assertIsNone(self.client.get_request_fingerprint())
        logs = repr(logger.mock_calls)
        self.assertNotIn("private-partial-token", logs)
        self.assertNotIn("private-partial-cookie", logs)

    async def test_http_error_echoed_secrets_never_enter_logs_or_raised_exception(self):
        self.enterContext(patch.dict(config._config["debug"], {"enabled": True}))
        logger = self.enterContext(patch("src.services.flow_client.debug_logger"))
        secrets = (
            "private-error-token",
            "private-error-cookie",
            "private-error-api-key",
            "private-error-authorization",
        )
        await self.solve(
            {
                "session_cookies": [
                    {"name": "NID", "value": secrets[1], "domain": ".googleapis.com", "path": "/"},
                ]
            },
            token=secrets[0],
        )
        response_body = {"error": {"message": "reCAPTCHA evaluation failed: " + "; ".join(secrets)}}
        self.session.post = AsyncMock(
            return_value=SimpleNamespace(
                status_code=403,
                headers={"set-cookie": f"NID={secrets[1]}"},
                text=json.dumps(response_body),
                json=lambda: response_body,
            )
        )
        for method_name in ("_make_request", "_make_text_request"):
            with self.subTest(method=method_name):
                logger.reset_mock()
                with self.assertRaises(Exception) as failure:
                    await getattr(self.client, method_name)(
                        method="POST",
                        url=FLOW_URL,
                        json_data={"clientContext": {"recaptchaContext": {"token": secrets[0]}}},
                        headers={"x-api-key": secrets[2]},
                        use_at=True,
                        at_token=secrets[3],
                    )
                logs = repr(logger.mock_calls)
                self.assertIn("403", str(failure.exception))
                for secret in secrets:
                    self.assertNotIn(secret, logs)
                    self.assertNotIn(secret, str(failure.exception))


if __name__ == "__main__":
    unittest.main()
