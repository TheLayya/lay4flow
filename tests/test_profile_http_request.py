"""Synthetic Curl transport/Profile Cookie merge contract; no real requests."""

import io
import logging
import time
import unittest
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.services.flow.browser_request import profile_http_request

ORIGIN = "https://flow.google.com"
PROXY = "http://fixture-user:fixture-password@proxy.invalid:8080"
SECRET = "fixture-private-cookie-value"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36"
)


class Headers(dict):
    def __init__(self, cookies=()):
        super().__init__({"Content-Type": "application/json"})
        self.cookies = list(cookies)

    def get_list(self, name):
        return self.cookies if name.lower() == "set-cookie" else []


def upstream(*cookies, url=ORIGIN + "/", status=200, text="{}"):
    return SimpleNamespace(status_code=status, url=url, headers=Headers(cookies), text=text)


def browser_cookie(name, value, domain=".google.com", path="/", *, expires=-1, secure=True):
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": secure,
        "httpOnly": True,
        "expires": expires,
        "sameSite": "Lax",
    }


class ProfileHTTPRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = SimpleNamespace(
            is_closed=Mock(return_value=False),
            evaluate=AsyncMock(
                return_value={
                    "user_agent": USER_AGENT,
                    "language": "zh-CN",
                    "languages": ["zh-CN", "zh", "en-US", "en"],
                }
            ),
            close=AsyncMock(),
        )
        self.context = SimpleNamespace(
            cookies=AsyncMock(return_value=[]),
            add_cookies=AsyncMock(),
            clear_cookies=AsyncMock(),
            pages=[self.page],
            new_page=AsyncMock(),
        )
        self.session = SimpleNamespace(request=AsyncMock(return_value=upstream()))
        self.session_closed = Mock()
        self.proxy_closed = Mock()

        @asynccontextmanager
        async def proxy(value):
            self.assertEqual(value, PROXY)
            try:
                yield "socks5://local-fixture:18080"
            finally:
                self.proxy_closed()

        @asynccontextmanager
        async def session(**kwargs):
            self.assertEqual(kwargs, {"trust_env": False})
            try:
                yield self.session
            finally:
                self.session_closed()

        self.proxy_patch = patch(
            "src.services.flow.browser_request.transport_proxy", side_effect=proxy
        )
        self.session_patch = patch(
            "src.services.flow.browser_request.AsyncSession", side_effect=session
        )
        self.proxy_patch.start()
        self.session_patch.start()
        self.addCleanup(self.proxy_patch.stop)
        self.addCleanup(self.session_patch.stop)

    async def test_fixed_proxy_scoped_cookies_and_request_shape_no_bearer(self):
        future, past = int(time.time()) + 3600, int(time.time()) - 3600
        self.context.cookies.return_value = [
            browser_cookie("SID", SECRET),
            browser_cookie("FLOW", "flow-host", "flow.google.com"),
            browser_cookie("ACCOUNTS", "account-host", "accounts.google.com"),
            browser_cookie("PROJECT_AUTH", "project-only", "flow.google.com", "/project"),
            browser_cookie("FUTURE", "valid", "flow.google.com", expires=future),
            browser_cookie("EXPIRED", "stale", "flow.google.com", expires=past),
            browser_cookie("FOREIGN", "no", "evil.invalid"),
        ]
        async with profile_http_request(self.context, PROXY) as transport:
            response = await transport.request.get(
                ORIGIN + "/project/demo", timeout=45000, max_redirects=0
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {})
        call = self.session.request.call_args
        self.assertEqual(call.args, ("GET", ORIGIN + "/project/demo"))
        self.assertEqual(call.kwargs["proxy"], "socks5://local-fixture:18080")
        self.assertEqual(call.kwargs["timeout"], 45)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["discard_cookies"])
        self.assertFalse(call.kwargs["default_headers"])
        self.assertEqual(call.kwargs["impersonate"], "chrome150")
        self.assertEqual(call.kwargs["headers"]["User-Agent"], USER_AGENT)
        self.assertEqual(
            call.kwargs["headers"]["Accept-Language"], "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
        )
        cookie = call.kwargs["headers"]["Cookie"]
        self.assertIn("SID=" + SECRET, cookie)
        self.assertIn("FLOW=flow-host", cookie)
        self.assertIn("PROJECT_AUTH=project-only", cookie)
        self.assertIn("FUTURE=valid", cookie)
        self.assertNotIn("ACCOUNTS=", cookie)
        self.assertNotIn("EXPIRED=", cookie)
        self.assertNotIn("FOREIGN=", cookie)
        self.assertNotIn("Authorization", call.kwargs["headers"])
        self.session_closed.assert_called_once()
        self.proxy_closed.assert_called_once()
        self.context.new_page.assert_not_awaited()
        self.page.close.assert_not_awaited()

    async def test_profile_identity_overrides_request_defaults_without_changing_page(self):
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.post(
                ORIGIN + "/_/rpc",
                headers={
                    "User-Agent": "old-request-ua",
                    "accept-language": "en-US,en;q=0.9",
                },
            )
        headers = self.session.request.await_args.kwargs["headers"]
        self.assertEqual(headers["User-Agent"], USER_AGENT)
        self.assertEqual(headers["Accept-Language"], "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7")
        self.assertNotIn("accept-language", headers)
        self.page.close.assert_not_awaited()
        self.context.new_page.assert_not_awaited()

    async def test_no_existing_page_reads_identity_on_an_owned_about_blank_page(self):
        self.context.pages = []
        self.context.new_page.return_value = self.page
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.get(ORIGIN + "/")
        self.context.new_page.assert_awaited_once()
        self.page.close.assert_awaited_once()
        self.assertFalse(hasattr(self.page, "goto"))

    async def test_invalid_profile_identity_stops_before_network_and_omits_secret(self):
        self.page.evaluate.return_value = {
            "user_agent": "private-user-agent\r\nheader",
            "languages": ["en-US"],
        }
        with self.assertRaises(Exception) as caught:
            async with profile_http_request(self.context, PROXY):
                self.fail("entered transport with invalid identity")
        self.assertEqual(caught.exception.network_step, "flow_http_identity")
        self.assertNotIn("private-user-agent", str(caught.exception))
        self.session.request.assert_not_awaited()

    async def test_cookie_rotation_is_available_to_next_request_then_saved_with_scope(self):
        self.context.cookies.return_value = [
            browser_cookie("SID", "before"),
            browser_cookie("HOST", "before", "flow.google.com"),
        ]
        self.session.request.side_effect = [
            upstream(
                "SID=rotated; Domain=.google.com; Path=/; Secure; HttpOnly; SameSite=Lax",
                "HOST=host-rotated; Path=/; Secure; HttpOnly",
            ),
            upstream(),
        ]
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.post(
                ORIGIN + "/_/rpc", form={"f.req": "[]"}, headers={"Origin": ORIGIN}
            )
            await transport.request.get(ORIGIN + "/")
        first, second = self.session.request.await_args_list
        self.assertEqual(first.kwargs["data"], {"f.req": "[]"})
        self.assertNotIn("form", first.kwargs)
        self.assertIn("SID=rotated", second.kwargs["headers"]["Cookie"])
        self.assertIn("HOST=host-rotated", second.kwargs["headers"]["Cookie"])
        saved = self.context.add_cookies.call_args.args[0]
        self.assertEqual(
            {(row["name"], row["domain"]): row["value"] for row in saved},
            {("SID", ".google.com"): "rotated", ("HOST", "flow.google.com"): "host-rotated"},
        )
        self.context.clear_cookies.assert_not_awaited()

    async def test_response_expiry_deletion_clears_only_matching_cookie_scope(self):
        self.context.cookies.return_value = [
            browser_cookie("SID", "root"),
            browser_cookie("SID", "project", ".google.com", "/project"),
        ]
        self.session.request.return_value = upstream(
            "SID=deleted; Domain=.google.com; Path=/project; Max-Age=0; Secure"
        )
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.get(ORIGIN + "/project/demo")
        self.context.clear_cookies.assert_awaited_once_with(
            name="SID", domain=".google.com", path="/project"
        )
        self.context.add_cookies.assert_not_awaited()

    async def test_untrusted_response_cookie_and_foreign_deletion_never_reach_profile(self):
        self.context.cookies.return_value = [browser_cookie("SID", "before")]
        self.session.request.return_value = upstream(
            "SID=deleted; Domain=evil.invalid; Path=/; Max-Age=0",
            "FOREIGN=x; Domain=evil.invalid; Path=/",
            url="https://evil.invalid/",
        )
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.get(ORIGIN + "/")
        self.context.add_cookies.assert_not_awaited()
        self.context.clear_cookies.assert_not_awaited()

    async def test_response_cookies_survive_body_failure_and_session_closes(self):
        self.session.request.return_value = upstream(
            "SID=rotated; Domain=.google.com; Path=/; Secure", text="private-invalid-json"
        )
        with self.assertRaises(ValueError):
            async with profile_http_request(self.context, PROXY) as transport:
                result = await transport.request.get(ORIGIN + "/")
                await result.json()
        self.context.add_cookies.assert_awaited_once()
        self.assertEqual(self.context.add_cookies.call_args.args[0][0]["value"], "rotated")
        self.session_closed.assert_called_once()
        self.proxy_closed.assert_called_once()

    async def test_unknown_submission_failure_is_not_retried_and_does_not_log_secrets(self):
        self.context.cookies.return_value = [browser_cookie("SID", SECRET)]
        self.session.request.side_effect = TimeoutError("unknown " + SECRET)
        stdout, stderr = io.StringIO(), io.StringIO()
        logger = logging.getLogger("src.services.flow.browser_request")
        handler = logging.StreamHandler(stderr)
        logger.addHandler(handler)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(TimeoutError):
                    async with profile_http_request(self.context, PROXY) as transport:
                        await transport.request.post(ORIGIN + "/_/rpc", form={"secret": SECRET})
        finally:
            logger.removeHandler(handler)
        self.assertEqual(self.session.request.await_count, 1)
        self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(PROXY, stdout.getvalue() + stderr.getvalue())
        self.context.add_cookies.assert_not_awaited()
        self.context.clear_cookies.assert_not_awaited()
        self.session_closed.assert_called_once()
        self.proxy_closed.assert_called_once()

    async def test_cookie_view_filters_url_domain_host_only_path_secure_and_expiry(self):
        self.context.cookies.return_value = [
            browser_cookie("SID", "global"),
            browser_cookie("HOST", "flow", "flow.google.com"),
            browser_cookie("PROJ", "path", "flow.google.com", "/project"),
            browser_cookie("ACCOUNTS", "account", "accounts.google.com"),
            browser_cookie("OLD", "expired", "flow.google.com", expires=int(time.time()) - 60),
        ]
        async with profile_http_request(self.context, PROXY) as transport:

            def names(rows):
                return {row["name"] for row in rows}

            self.assertEqual(
                names(await transport.cookies(ORIGIN + "/project/demo")), {"SID", "HOST", "PROJ"}
            )
            self.assertEqual(names(await transport.cookies(ORIGIN + "/other")), {"SID", "HOST"})
            self.assertEqual(
                names(await transport.cookies("https://accounts.google.com/")), {"SID", "ACCOUNTS"}
            )
            self.assertEqual(await transport.cookies("http://flow.google.com/"), [])
            self.assertEqual(await transport.cookies("https://evil.invalid/"), [])
            self.assertEqual(await transport.cookies("https://flow.google.com:444/"), [])
            self.assertEqual(await transport.cookies("https://flow.google.com:invalid/"), [])
            self.assertEqual(
                names(await transport.cookies([ORIGIN + "/", "https://accounts.google.com/"])),
                {"SID", "HOST", "ACCOUNTS"},
            )

    async def test_snapshot_does_not_overwrite_concurrent_gui_cookie_rotation(self):
        before = [browser_cookie("SID", "before")]
        current = [browser_cookie("SID", "gui-rotated")]
        self.context.cookies.side_effect = [before, current]
        self.session.request.return_value = upstream(
            "SID=http-rotated; Domain=.google.com; Path=/; Secure"
        )
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.get(ORIGIN + "/")
        self.context.add_cookies.assert_not_awaited()
        self.context.clear_cookies.assert_not_awaited()

    async def test_snapshot_deletion_does_not_remove_concurrent_gui_cookie(self):
        before = [browser_cookie("SID", "before")]
        current = [browser_cookie("SID", "gui-rotated")]
        self.context.cookies.side_effect = [before, current]
        self.session.request.return_value = upstream(
            "SID=deleted; Domain=.google.com; Path=/; Max-Age=0; Secure"
        )
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.get(ORIGIN + "/")
        self.context.add_cookies.assert_not_awaited()
        self.context.clear_cookies.assert_not_awaited()

    async def test_new_http_cookie_does_not_replace_same_key_created_by_gui(self):
        self.context.cookies.side_effect = [[], [browser_cookie("SID", "gui-new")]]
        self.session.request.return_value = upstream(
            "SID=http-new; Domain=.google.com; Path=/; Secure"
        )
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.get(ORIGIN + "/")
        self.context.add_cookies.assert_not_awaited()
        self.context.clear_cookies.assert_not_awaited()

    async def test_domain_cookie_without_set_cookie_dot_uses_browser_key_for_deletion(self):
        self.context.cookies.return_value = [browser_cookie("SID", "before")]
        self.session.request.side_effect = [
            upstream("SID=rotated; Domain=google.com; Path=/; Secure"),
            upstream("SID=deleted; Domain=google.com; Path=/; Max-Age=0; Secure"),
        ]
        async with profile_http_request(self.context, PROXY) as transport:
            await transport.request.get(ORIGIN + "/")
            await transport.request.get(ORIGIN + "/")
        self.context.clear_cookies.assert_awaited_once_with(
            name="SID", domain=".google.com", path="/"
        )


if __name__ == "__main__":
    unittest.main()
