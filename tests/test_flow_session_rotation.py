"""Session rotation stays local to each trusted Labs HTTP response."""

import asyncio
import io
import json
import traceback
import unittest
import urllib.error
from email.message import Message
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from curl_cffi.requests.headers import Headers

from src.core.config import config
from src.services.flow_client import FlowClient

SESSION_URL = "https://labs.google/fx/api/auth/session"
COOKIE = "__Secure-next-auth.session-token"
PAYLOAD = {
    "access_token": "synthetic-at",
    "expires": "2030-01-01T00:00:00Z",
    "user": {"email": "account@example.test"},
    "extra": {"unknown": True},
}


def response(cookies=(), *, url=SESSION_URL, status=200, payload=None, secure=True):
    body = dict(PAYLOAD if payload is None else payload)
    return SimpleNamespace(
        status_code=status,
        url=url,
        headers=Headers(
            [("Set-Cookie", cookie + ("; Secure" if secure else "")) for cookie in cookies]
        ),
        text=json.dumps(body),
        json=lambda: body,
    )


class FlowSessionRotationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                config._config,
                {
                    "captcha": {"captcha_method": "remote_browser"},
                    "debug": {"enabled": False},
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
        self.session.get = AsyncMock(return_value=response())
        self.session.post = AsyncMock(return_value=response())
        self.enterContext(patch("src.services.flow_client.AsyncSession", return_value=self.session))

    async def test_rotated_token_preserves_json_and_remote_proxy_options(self):
        original = {**PAYLOAD, "_session_token": "json-cannot-supply-token"}
        self.session.get.return_value = response(
            [f"{COOKIE}=rotated-st; Path=/; Secure; HttpOnly"], payload=original
        )
        self.client._set_request_fingerprint({"proxy_url": "http://solver.proxy:8080"})
        result = await self.client.st_to_at("old-st")
        self.assertEqual(result, {**PAYLOAD, "_session_token": "rotated-st"})
        self.assertEqual(original["_session_token"], "json-cannot-supply-token")
        call = self.session.get.await_args
        self.assertEqual(call.args, (SESSION_URL,))
        self.assertEqual(call.kwargs["headers"]["Cookie"], f"{COOKIE}=old-st")
        self.assertEqual(call.kwargs["proxy"], "http://account.proxy:8080")
        self.assertFalse(call.kwargs["default_headers"])
        self.assertFalse(call.kwargs["allow_redirects"])

    async def test_missing_cookie_does_not_return_json_supplied_private_token(self):
        for cookies in ([], ["unrelated=value; Path=/"]):
            with self.subTest(cookies=cookies):
                self.session.get.return_value = response(
                    cookies, payload={**PAYLOAD, "_session_token": "untrusted"}
                )
                self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)

    async def test_deleting_a_cookie_after_setting_it_discards_the_value(self):
        self.session.get.return_value = response(
            [f"{COOKIE}=old-value; Path=/", f"{COOKIE}=; Max-Age=0; Path=/"]
        )
        self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)

    async def test_chunks_join_by_numeric_index_and_ignore_cleanup_cookies(self):
        cookies = [f"{COOKIE}.{index}=part{index}-; Path=/" for index in reversed(range(12))]
        cookies += [f"{COOKIE}=; Max-Age=0; Path=/", "other=value; Path=/"]
        self.session.get.return_value = response(cookies)
        result = await self.client.st_to_at("old-st")
        self.assertEqual(result["_session_token"], "".join(f"part{i}-" for i in range(12)))

    async def test_incomplete_or_ambiguous_chunks_are_not_a_rotated_token(self):
        for cookies in (
            [f"{COOKIE}.1=tail"],
            [f"{COOKIE}.0=head", f"{COOKIE}.2=tail"],
            [f"{COOKIE}=whole", f"{COOKIE}.0=head"],
            [f"{COOKIE}.01=tail"],
        ):
            with self.subTest(cookies=cookies):
                self.session.get.return_value = response(cookies)
                self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)

    async def test_deletion_expiry_foreign_scope_and_malformed_cookies_are_ignored(self):
        for cookie in (
            f"{COOKIE}=; Path=/",
            f"{COOKIE}=delete; Max-Age=0; Path=/",
            f"{COOKIE}=delete; Max-Age=-1; Path=/",
            f"{COOKIE}=expired; Expires=Wed, 01 Jan 2020 00:00:00 GMT; Path=/",
            f"{COOKIE}=invalid; Max-Age=invalid; Path=/",
            f"{COOKIE}=invalid; Expires=invalid; Path=/",
            f"{COOKIE}=foreign; Domain=evil.test; Path=/",
            f"{COOKIE}=broad; Domain=.google; Path=/",
            f"{COOKIE}=foreign; Domain=google.com; Path=/",
            f"{COOKIE}=foreign; Domain=..labs.google; Path=/",
            f"{COOKIE}=wrong-path; Path=/fx/other",
            f"{COOKIE}=wrong-path; Path=/fx/api/auth/session-child",
            f"{COOKIE}=wrong-path; Path=/fx/api/auth//",
            f"{COOKIE}.invalid=value; Path=/",
        ):
            with self.subTest(cookie=cookie):
                self.session.get.return_value = response([cookie])
                self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)

    async def test_labs_domain_and_max_age_precedence_are_valid(self):
        self.session.get.return_value = response(
            [
                f'{COOKIE}="rotated-st"; Domain=.labs.google; Path=/fx/api/auth; '
                "Max-Age=600; Expires=Wed, 01 Jan 2020 00:00:00 GMT"
            ]
        )
        self.assertEqual((await self.client.st_to_at("old-st"))["_session_token"], "rotated-st")

    async def test_secure_prefix_cookie_requires_secure_attribute(self):
        self.session.get.return_value = response([f"{COOKIE}=insecure-st; Path=/"], secure=False)
        self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)

    async def test_only_exact_session_http_200_response_is_trusted(self):
        for url, status in (
            ("https://evil.test/fx/api/auth/session", 200),
            ("https://labs.google.evil.test/fx/api/auth/session", 200),
            ("https://labs.google/fx/api/auth/callback/google", 200),
            ("http://labs.google/fx/api/auth/session", 200),
            ("https://labs.google:8443/fx/api/auth/session", 200),
            ("https://name@labs.google/fx/api/auth/session", 200),
            (SESSION_URL + "?other=1", 200),
            (SESSION_URL + "/", 200),
            (SESSION_URL, 302),
            (SESSION_URL, 201),
            ("", 200),
        ):
            with self.subTest(url=url, status=status):
                self.session.get.return_value = response(
                    [f"{COOKIE}=untrusted"], url=url, status=status
                )
                self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)
        self.session.get.return_value = response([f"{COOKIE}=untrusted"])
        del self.session.get.return_value.url
        self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)
        self.client.labs_base_url = "https://alternate.test/fx/api"
        self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)

    async def test_other_requests_keep_original_json_without_cookie_capture(self):
        payload = {**PAYLOAD, "_session_token": "original-json-field"}
        self.session.get.return_value = response([f"{COOKIE}=new-token"], payload=payload)
        self.assertEqual(await self.client._make_request("GET", SESSION_URL), payload)
        self.session.post.return_value = response([f"{COOKIE}=new-token"], payload=payload)
        self.assertEqual(
            await self.client._make_request("POST", SESSION_URL, capture_session_token=True),
            PAYLOAD,
        )

    async def test_concurrent_session_responses_cannot_exchange_tokens(self):
        both_started = asyncio.Event()
        calls = 0

        async def get(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
            await both_started.wait()
            old_token = kwargs["headers"]["Cookie"].split("=", 1)[1]
            cookies = [f"{COOKIE}=rotated-{old_token}"] if old_token == "account-a" else []
            return response(cookies, payload={**PAYLOAD, "access_token": f"at-{old_token}"})

        self.session.get.side_effect = get
        first, second = await asyncio.gather(
            self.client.st_to_at("account-a"), self.client.st_to_at("account-b")
        )
        self.assertEqual(first["_session_token"], "rotated-account-a")
        self.assertEqual(first["access_token"], "at-account-a")
        self.assertNotIn("_session_token", second)
        self.assertEqual(second["access_token"], "at-account-b")

    async def test_urllib_fallback_keeps_proxy_and_extracts_repeated_headers(self):
        self.session.get.side_effect = RuntimeError("curl: (7) proxy unavailable")
        headers = Message()
        headers.add_header("Set-Cookie", f"{COOKIE}.1=second; Path=/; Secure")
        headers.add_header("Set-Cookie", f"{COOKIE}.0=first; Path=/; Secure")
        upstream = MagicMock()
        upstream.__enter__.return_value = upstream
        upstream.read.return_value = json.dumps(PAYLOAD).encode()
        upstream.getcode.return_value = 200
        upstream.geturl.return_value = SESSION_URL
        upstream.headers = headers
        opener = MagicMock()
        opener.open.return_value = upstream
        with patch(
            "src.services.flow_client.urllib.request.build_opener", return_value=opener
        ) as build:
            result = await self.client.st_to_at("old-st")
        self.assertEqual(result, {**PAYLOAD, "_session_token": "firstsecond"})
        handlers = build.call_args.args
        proxy_handler = next(handler for handler in handlers if hasattr(handler, "proxies"))
        self.assertEqual(
            proxy_handler.proxies,
            {"http": "http://account.proxy:8080", "https": "http://account.proxy:8080"},
        )
        self.session.get.assert_awaited_once()
        opener.open.assert_called_once()

        for url, status, cookies in (
            ("https://evil.test/fx/api/auth/session", 200, headers),
            (SESSION_URL, 201, headers),
            (SESSION_URL, 200, Message()),
        ):
            with self.subTest(url=url, status=status, has_cookie=bool(cookies)):
                upstream.geturl.return_value = url
                upstream.getcode.return_value = status
                upstream.headers = cookies
                upstream.read.return_value = json.dumps(
                    {**PAYLOAD, "_session_token": "untrusted-json"}
                ).encode()
                with patch(
                    "src.services.flow_client.urllib.request.build_opener", return_value=opener
                ):
                    self.assertEqual(await self.client.st_to_at("old-st"), PAYLOAD)

    async def test_proxy_failure_never_retries_directly(self):
        self.session.get.side_effect = RuntimeError("curl: (7) proxy unavailable")
        with patch.object(
            self.client, "_sync_json_request_via_urllib", side_effect=RuntimeError("proxy failed")
        ) as fallback:
            with self.assertRaisesRegex(Exception, "proxy error"):
                await self.client.st_to_at("old-st")
        self.session.get.assert_awaited_once()
        self.assertEqual(self.session.get.await_args.kwargs["proxy"], "http://account.proxy:8080")
        self.assertEqual(fallback.call_args.args[4], "http://account.proxy:8080")

    async def test_urllib_fallback_installs_exactly_one_explicit_proxy_handler(self):
        import urllib.request

        proxy_handler = urllib.request.ProxyHandler
        for proxy in ("http://account.proxy:8080", None):
            with self.subTest(proxy=proxy):
                upstream = MagicMock()
                upstream.__enter__.return_value = upstream
                upstream.read.return_value = b"{}"
                upstream.getcode.return_value = 200
                upstream.headers = Message()
                opener = MagicMock()
                opener.open.return_value = upstream
                with patch(
                    "src.services.flow_client.urllib.request.build_opener", return_value=opener
                ) as build_opener:
                    self.client._sync_json_request_via_urllib(
                        "GET", SESSION_URL, {}, None, proxy, 5
                    )
                handlers = [h for h in build_opener.call_args.args if isinstance(h, proxy_handler)]
                self.assertEqual(len(handlers), 1)
                expected = {"http": proxy, "https": proxy} if proxy else {}
                self.assertEqual(handlers[0].proxies, expected)

    async def test_capture_logging_does_not_include_rotated_token(self):
        self.session.get.return_value = response([f"{COOKIE}=rotated-sensitive-st"])
        for method in ("remote_browser", "browser", "yescaptcha"):
            with self.subTest(method=method):
                with patch.dict(
                    config._config,
                    {"debug": {"enabled": True}, "captcha": {"captcha_method": method}},
                ):
                    with patch("src.services.flow_client.debug_logger") as logger:
                        await self.client.st_to_at("old-sensitive-st")
                self.assertNotIn("rotated-sensitive-st", repr(logger.mock_calls))
                self.assertNotIn("old-sensitive-st", repr(logger.mock_calls))

    async def test_nonremote_http_error_and_invalid_json_do_not_expose_response_secrets(self):
        secret = "synthetic-response-secret"
        for method in ("browser", "yescaptcha"):
            for invalid_json in (False, True):
                with self.subTest(method=method, invalid_json=invalid_json):
                    upstream = response(
                        [f"{COOKIE}={secret}"],
                        status=200 if invalid_json else 400,
                        payload={"error": {"message": secret}},
                    )
                    if invalid_json:
                        upstream.text = secret
                        upstream.json = MagicMock(side_effect=ValueError(secret))
                    self.session.get.return_value = upstream
                    with patch.dict(
                        config._config,
                        {"debug": {"enabled": True}, "captcha": {"captcha_method": method}},
                    ):
                        with patch("src.services.flow_client.debug_logger") as logger:
                            with self.assertRaises(Exception) as caught:
                                await self.client.st_to_at("old-st")
                    self.assertNotIn(secret, repr(logger.mock_calls))
                    self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))
                    if not invalid_json:
                        self.assertIn("HTTP Error 400", str(caught.exception))

    async def test_nonremote_urllib_failures_never_expose_response_secrets(self):
        secret = "synthetic-urllib-secret"
        self.session.get.side_effect = RuntimeError("curl: (7) proxy unavailable")
        for failure in ("http-error", "http-status", "invalid-json", "transport"):
            with self.subTest(failure=failure):
                upstream = MagicMock()
                upstream.__enter__.return_value = upstream
                upstream.read.return_value = secret.encode()
                upstream.getcode.return_value = 400 if failure == "http-status" else 200
                upstream.geturl.return_value = SESSION_URL
                upstream.headers = Message()
                opener = MagicMock()
                opener.open.return_value = upstream
                if failure == "http-error":
                    opener.open.side_effect = urllib.error.HTTPError(
                        SESSION_URL, 400, secret, Message(), io.BytesIO(secret.encode())
                    )
                elif failure == "transport":
                    opener.open.side_effect = RuntimeError(secret)
                with patch.dict(
                    config._config,
                    {"debug": {"enabled": True}, "captcha": {"captcha_method": "yescaptcha"}},
                ):
                    with (
                        patch("src.services.flow_client.debug_logger") as logger,
                        patch(
                            "src.services.flow_client.urllib.request.build_opener",
                            return_value=opener,
                        ),
                    ):
                        with self.assertRaises(Exception) as caught:
                            await self.client.st_to_at("old-st")
                self.assertNotIn(secret, repr(logger.mock_calls))
                self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))


if __name__ == "__main__":
    unittest.main()
