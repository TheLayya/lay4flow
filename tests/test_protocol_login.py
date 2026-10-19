"""Protocol login regressions; all sessions and credentials are synthetic."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from curl_cffi.requests.exceptions import ProxyError

from src.services.protocol_login import ProtocolLogin, _parse_google_cookies


class Headers(dict):
    def __init__(self, cookies=(), **values):
        super().__init__(values)
        self.cookies = cookies

    def getlist(self, name):
        return list(self.cookies) if name.lower() == "set-cookie" else []


def response(status=200, data=None, text="", cookies=(), **headers):
    return SimpleNamespace(
        status_code=status,
        json=lambda: data,
        text=text,
        headers=Headers(cookies, **headers),
    )


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.cookies = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        if self.cookies:
            raise AssertionError("Implicit transport cookie jar was not cleared")
        self.requests.append((url, kwargs))
        self.cookies["implicit-poison"] = "must-not-be-sent"
        await asyncio.sleep(0)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    post = get


def prefix():
    return [
        response(data={"csrfToken": "test-csrf"}, cookies=["labs-csrf=csrf; Path=/"]),
        response(data={"url": "https://accounts.google.com/o/oauth2/auth"}),
    ]


class ProtocolLoginTests(unittest.IsolatedAsyncioTestCase):
    def test_export_containers_preserve_cookies_without_account_metadata(self):
        expected = {"SID": "test-sid", "HSID": "test-hsid"}
        formats = [
            expected,
            [{"name": k, "value": v} for k, v in expected.items()],
            "SID=test-sid; HSID=test-hsid",
        ]
        for cookies in formats:
            with self.subTest(cookies=cookies):
                wrapped = {
                    "email": "synthetic@example.test",
                    "projectId": "test-project",
                    "refreshCookie": cookies,
                    "sessionToken": "old-session",
                }
                self.assertEqual(_parse_google_cookies(json.dumps(wrapped)), expected)
                self.assertEqual(_parse_google_cookies(json.dumps({"cookies": cookies})), expected)
        for cookies in formats:
            raw = cookies if isinstance(cookies, str) else json.dumps(cookies)
            self.assertEqual(_parse_google_cookies(raw), expected)
        self.assertEqual(_parse_google_cookies('{"refreshCookie":{},"email":"x"}'), {})
        self.assertEqual(
            _parse_google_cookies(
                json.dumps(
                    [
                        {"name": "SID", "value": "test-sid", "domain": ".google.com"},
                        {"name": "other", "value": "not-google", "domain": "labs.google"},
                    ]
                )
            ),
            {"SID": "test-sid"},
        )

    def test_modern_flow_cookie_is_preserved_from_cookie_export(self):
        self.assertEqual(
            _parse_google_cookies(
                [
                    {"name": "OSID", "value": "modern", "domain": ".flow.google.com"},
                    {"name": "SID", "value": "account", "domain": ".google.com"},
                ]
            ),
            {"OSID": "modern", "SID": "account"},
        )

    async def login_with(self, responses, raw="SID=test-sid", proxy=None):
        session = FakeSession(responses)
        with patch("src.services.protocol_login.AsyncSession", return_value=session) as factory:
            result = await ProtocolLogin().login(raw, proxy=proxy)
            if factory.called:
                self.assertEqual(factory.call_args.kwargs["timeout"], 30)
        return result, session

    async def test_callback_joins_chunks_and_ignores_deleted_whole_cookie(self):
        result, _ = await self.login_with(
            prefix()
            + [
                response(302, location="https://labs.google/fx/api/auth/callback/google?code=test"),
                response(
                    302,
                    cookies=[
                        "__Secure-next-auth.session-token.1=tail; Path=/; Secure",
                        "__Secure-next-auth.session-token=; Max-Age=0; Path=/; Secure",
                        "__Secure-next-auth.session-token.0=head; Path=/; Secure",
                    ],
                ),
            ]
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["session_token"], "headtail")

    async def test_callback_rejects_deleted_invalid_and_ambiguous_tokens(self):
        name = "__Secure-next-auth.session-token"
        for cookies in (
            [f"{name}=deleted; Max-Age=0; Path=/; Secure"],
            [f"{name}=expired; Expires=Wed, 01 Jan 2020 00:00:00 GMT; Path=/; Secure"],
            [f"{name}=wrong-domain; Domain=example.test; Path=/; Secure"],
            [f"{name}=wrong-path; Path=/other; Secure"],
            [f"{name}=insecure; Path=/"],
            [f"{name}.0=head; Path=/; Secure", f"{name}.2=tail; Path=/; Secure"],
            [f"{name}=whole; Path=/; Secure", f"{name}.0=head; Path=/; Secure"],
        ):
            with self.subTest(cookies=cookies):
                result, _ = await self.login_with(
                    prefix()
                    + [
                        response(
                            302,
                            location="https://labs.google/fx/api/auth/callback/google?code=test",
                        ),
                        response(302, cookies=cookies),
                    ]
                )
                self.assertFalse(result["success"])

    async def test_callback_uses_latest_set_cookie_and_preserves_oauth_session(self):
        result, session = await self.login_with(
            [
                response(
                    data={"csrfToken": "same-csrf"}, cookies=["labs-csrf=csrf; Path=/; Secure"]
                ),
                response(
                    data={
                        "url": "https://accounts.google.com/o/oauth2/auth?state=same-state&code_challenge=challenge"
                    },
                    cookies=[
                        "__Secure-next-auth.state=same-state; Path=/; Secure",
                        "__Secure-next-auth.pkce.code_verifier=verifier; Path=/; Secure",
                    ],
                ),
                response(
                    302,
                    location="https://labs.google/fx/api/auth/callback/google?code=test&state=same-state",
                ),
                response(
                    302,
                    cookies=[
                        "__Secure-next-auth.session-token=old; Path=/; Secure",
                        "__Secure-next-auth.session-token=new; Path=/; Secure",
                    ],
                ),
            ]
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["session_token"], "new")
        self.assertEqual(
            session.requests[1][1]["data"],
            {
                "redirect": "false",
                "csrfToken": "same-csrf",
                "callbackUrl": "https://labs.google/fx",
                "json": "true",
            },
        )
        self.assertEqual(session.requests[1][1]["data"]["csrfToken"], "same-csrf")
        self.assertIn("state=same-state", session.requests[2][0])
        self.assertIn("code_challenge=challenge", session.requests[2][0])
        self.assertIn(
            "__Secure-next-auth.state=same-state", session.requests[3][1]["headers"]["Cookie"]
        )
        self.assertIn(
            "__Secure-next-auth.pkce.code_verifier=verifier",
            session.requests[3][1]["headers"]["Cookie"],
        )

    async def test_html_callback_decodes_entities_without_corrupting_state(self):
        result, session = await self.login_with(
            prefix()
            + [
                response(
                    text='<meta http-equiv="refresh" content="0;url=https://labs.google/fx/api/auth/callback/google?code=test&amp;state=same-state">'
                ),
                response(302, cookies=["__Secure-next-auth.session-token=new; Path=/; Secure"]),
            ]
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            parse_qs(urlparse(session.requests[-1][0]).query),
            {"code": ["test"], "state": ["same-state"]},
        )

    async def test_callback_and_oauth_reject_nonstandard_port_or_userinfo(self):
        for url in (
            "https://labs.google:8443/fx/api/auth/callback/google?code=test",
            "https://user:password@labs.google/fx/api/auth/callback/google?code=test",
            "https://accounts.google.com:8443/o/oauth2/continue",
            "https://user@accounts.google.com/o/oauth2/continue",
        ):
            with self.subTest(url=url):
                result, session = await self.login_with(
                    prefix()
                    + [
                        response(302, location=url),
                        response(
                            302, cookies=["__Secure-next-auth.session-token=bad; Path=/; Secure"]
                        ),
                    ]
                )
                self.assertFalse(result["success"])
                self.assertEqual(len(session.requests), 3)

    async def test_error_status_never_accepts_callback_token_or_login_html(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(stage="callback", status=status):
                result, _ = await self.login_with(
                    prefix()
                    + [
                        response(
                            302,
                            location="https://labs.google/fx/api/auth/callback/google?code=test",
                        ),
                        response(
                            status, cookies=["__Secure-next-auth.session-token=bad; Path=/; Secure"]
                        ),
                    ]
                )
                self.assertFalse(result["success"])
            if status in (429, 500, 503):
                with self.subTest(stage="oauth", status=status):
                    result, _ = await self.login_with(
                        prefix() + [response(status, text='<input type="password">')]
                    )
                    self.assertEqual(result["error_code"], "transient_error")

    async def test_post_form_is_not_followed_as_a_get_without_its_fields(self):
        result, session = await self.login_with(
            prefix()
            + [
                response(
                    text='<form method="post" action="/oauth/continue"><input type="hidden" name="state" value="same-state"></form>'
                ),
                response(302, location="https://labs.google/fx/api/auth/callback/google?code=test"),
                response(302, cookies=["__Secure-next-auth.session-token=bad; Path=/; Secure"]),
            ]
        )
        self.assertFalse(result["success"])
        self.assertEqual(len(session.requests), 3)

    async def test_explicit_oauth_login_errors_stop_before_callback(self):
        for error in ("login_required", "interaction_required", "consent_required"):
            result, session = await self.login_with(
                prefix()
                + [
                    response(
                        302,
                        location="https://labs.google/fx/api/auth/callback/google?error=" + error,
                    ),
                ]
            )
            self.assertEqual(result["error_code"], "login_required")
            self.assertEqual(len(session.requests), 3)

    async def test_callback_followup_obeys_status_and_origin_before_cookies(self):
        for target in (
            "https://example.test/",
            "https://user@labs.google/fx",
            "https://labs.google:8443/fx",
        ):
            result, session = await self.login_with(
                prefix()
                + [
                    response(
                        302, location="https://labs.google/fx/api/auth/callback/google?code=test"
                    ),
                    response(302, location=target),
                ]
            )
            self.assertFalse(result["success"])
            self.assertEqual(len(session.requests), 4)
        for status in (200, 429, 503):
            result, _ = await self.login_with(
                prefix()
                + [
                    response(
                        302, location="https://labs.google/fx/api/auth/callback/google?code=test"
                    ),
                    response(302, location="/fx"),
                    response(
                        status, cookies=["__Secure-next-auth.session-token=new; Path=/; Secure"]
                    ),
                ]
            )
            self.assertEqual(result["success"], status == 200)

    async def test_callback_oauth_error_never_accepts_a_token(self):
        for error in ("access_denied", "server_error", ""):
            result, session = await self.login_with(
                prefix()
                + [
                    response(
                        302,
                        location="https://labs.google/fx/api/auth/callback/google?error=" + error,
                    ),
                    response(302, cookies=["__Secure-next-auth.session-token=bad; Path=/; Secure"]),
                ]
            )
            self.assertFalse(result["success"])
            self.assertEqual(len(session.requests), 3)

    async def test_callback_never_starts_another_auth_flow_to_find_a_token(self):
        for target in (
            "/fx/api/auth/callback/github",
            "/fx/api/auth/signin/google",
            "/fx/api/auth/callback/google?code=second",
            "/fx?error=Callback",
        ):
            result, session = await self.login_with(
                prefix()
                + [
                    response(
                        302, location="https://labs.google/fx/api/auth/callback/google?code=test"
                    ),
                    response(302, location=target),
                    response(302, cookies=["__Secure-next-auth.session-token=bad; Path=/; Secure"]),
                ]
            )
            self.assertFalse(result["success"])
            self.assertEqual(len(session.requests), 4)

    async def test_proxy_is_normalized_into_session_kwargs(self):
        session = FakeSession([response(data={"csrfToken": "test-csrf"})])
        with patch("src.services.protocol_login.AsyncSession", return_value=session) as factory:
            await ProtocolLogin().login("SID=test-sid", proxy="st5 proxy.test:1080:user:pass")
        self.assertEqual(factory.call_args.kwargs["proxy"], "socks5://user:pass@proxy.test:1080")

    async def test_invalid_proxy_fails_without_falling_back_to_direct_connection(self):
        session = FakeSession([response(503)])
        with patch("src.services.protocol_login.AsyncSession", return_value=session) as factory:
            result = await ProtocolLogin().login("SID=test-sid", proxy="ftp://proxy.test:80")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "invalid_proxy")
        factory.assert_not_called()

    async def test_proxy_connection_failure_is_explicit_and_never_direct(self):
        session = FakeSession([ProxyError("proxy connect failed")])
        with patch("src.services.protocol_login.AsyncSession", return_value=session) as factory:
            result = await ProtocolLogin().login("SID=test-sid", proxy="http://proxy.test:80")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "proxy_error")
        self.assertIn("代理", result["error"])
        self.assertEqual(factory.call_args.kwargs["proxy"], "http://proxy.test:80")

    async def test_success_uses_rotated_google_cookies_and_keeps_labs_separate(self):
        result, session = await self.login_with(
            prefix()
            + [
                response(
                    302,
                    cookies=[
                        "SID=rotated-sid; Domain=.google.com; Path=/; Secure",
                        "HSID=accounts-hsid; Domain=accounts.google.com; Path=/",
                        "unrelated=bad; Domain=example.test; Path=/",
                        "scoped=bad; Domain=.google.com; Path=/other",
                    ],
                    location="/oauth/continue",
                ),
                response(
                    302, location="https://labs.google/fx/api/auth/callback/google?code=test-code"
                ),
                response(
                    302,
                    cookies=[
                        "__Secure-next-auth.session-token=new-session; Path=/; Secure",
                        "labs-only=value; Path=/",
                    ],
                ),
            ],
            json.dumps(
                {
                    "email": "synthetic@example.test",
                    "projectId": "test-project",
                    "refreshCookie": {"SID": "test-sid"},
                    "sessionToken": "old-session",
                }
            ),
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["session_token"], "new-session")
        exported = json.loads(result["google_cookies"])
        self.assertEqual(
            {(item["name"], item["domain"], item["path"]): item["value"] for item in exported},
            {
                ("SID", ".google.com", "/"): "rotated-sid",
                ("HSID", "accounts.google.com", "/"): "accounts-hsid",
                ("scoped", ".google.com", "/other"): "bad",
            },
        )
        self.assertEqual(session.requests[2][1]["headers"]["Cookie"], "SID=test-sid")
        self.assertIn("SID=rotated-sid", session.requests[3][1]["headers"]["Cookie"])
        self.assertEqual(session.requests[4][1]["headers"]["Cookie"], "labs-csrf=csrf")

    async def test_failure_codes_separate_configuration_login_and_temporary_errors(self):
        cases = [
            ([], '{"refreshCookie":{},"sessionToken":"old"}', "invalid_cookies"),
            ([response(503)], "SID=test-sid", "transient_error"),
            (
                prefix() + [RuntimeError("synthetic network failure")],
                "SID=test-sid",
                "transient_error",
            ),
            (
                prefix() + [response(text="unknown OAuth page")],
                "SID=test-sid",
                "protocol_incomplete",
            ),
            (prefix() + [response(503, text="signin/rejected")], "SID=test-sid", "transient_error"),
            (prefix() + [response(401)], "SID=test-sid", "login_required"),
            (prefix() + [response(text="signin/rejected")], "SID=test-sid", "login_required"),
            (
                prefix() + [response(302, location="/v3/signin/identifier")],
                "SID=test-sid",
                "login_required",
            ),
            (
                prefix() + [response(text='<input type="password">')],
                "SID=test-sid",
                "login_required",
            ),
            (
                prefix()
                + [
                    response(302, location="https://labs.google/fx/api/auth/callback/google"),
                    response(503),
                ],
                "SID=test-sid",
                "transient_error",
            ),
        ]
        for responses, raw, code in cases:
            with self.subTest(code=code, responses=len(responses)):
                result, session = await self.login_with(responses, raw)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], code)
                if code == "invalid_cookies":
                    self.assertFalse(session.requests)

    async def test_unknown_redirect_never_receives_google_cookies(self):
        result, session = await self.login_with(
            prefix() + [response(302, location="https://example.test/oauth")]
        )
        self.assertEqual(result["error_code"], "transient_error")
        self.assertEqual(len(session.requests), 3)

    async def test_google_help_redirect_is_terminal_without_cookie_leak_or_search_loop(self):
        result, session = await self.login_with(
            prefix() + [response(302, location="https://support.google.com/accounts/answer/32050")]
        )
        self.assertEqual(result["error_code"], "login_required")
        self.assertEqual(len(session.requests), 3)
        self.assertFalse(any("support.google.com" in url for url, _ in session.requests))

    async def test_scoped_export_relogin_preserves_domain_path_and_labs_isolation(self):
        raw = json.dumps(
            [
                {
                    "name": "SID",
                    "value": "root",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "SID",
                    "value": "accounts",
                    "domain": "accounts.google.com",
                    "path": "/o",
                    "secure": True,
                },
                {
                    "name": "OSID",
                    "value": "flow",
                    "domain": "flow.google.com",
                    "path": "/",
                    "secure": True,
                },
            ]
        )

        def oauth():
            return prefix() + [
                response(
                    302,
                    cookies=["LSID=accounts-only; Path=/; Secure; httponly; samesite=Lax"],
                    location="https://flow.google.com/login",
                ),
                response(
                    302,
                    cookies=["OSID=flow-rotated; Path=/; Secure"],
                    location="https://accounts.google.com/o/continue",
                ),
                response(302, location="https://labs.google/fx/api/auth/callback/google?code=test"),
                response(
                    302, cookies=["__Secure-next-auth.session-token=new-session; Path=/; Secure"]
                ),
            ]

        first, first_session = await self.login_with(oauth(), raw)
        self.assertTrue(first["success"])
        exported = json.loads(first["google_cookies"])
        self.assertEqual(len([item for item in exported if item["name"] == "SID"]), 2)
        lsid = next(item for item in exported if item["name"] == "LSID")
        self.assertTrue(lsid["hostOnly"])
        self.assertTrue(lsid["httpOnly"])
        self.assertEqual(lsid["sameSite"], "Lax")
        second, second_session = await self.login_with(oauth(), first["google_cookies"])
        self.assertTrue(second["success"])
        self.assertIn("SID=accounts", second_session.requests[2][1]["headers"]["Cookie"])
        self.assertIn("LSID=accounts-only", second_session.requests[2][1]["headers"]["Cookie"])
        for session in (first_session, second_session):
            flow_header = session.requests[3][1]["headers"]["Cookie"]
            self.assertIn("SID=root", flow_header)
            self.assertIn("OSID=flow", flow_header)
            self.assertNotIn("SID=accounts", flow_header)
            self.assertNotIn("LSID=", flow_header)
            self.assertNotIn("OSID=", session.requests[4][1]["headers"]["Cookie"])
            self.assertEqual(session.requests[-1][1]["headers"]["Cookie"], "labs-csrf=csrf")
            self.assertTrue(all(not kwargs["allow_redirects"] for _, kwargs in session.requests))

    async def test_concurrent_oauth_logins_do_not_share_cookie_updates(self):
        def oauth(identity):
            return prefix() + [
                response(
                    302,
                    cookies=[f"SID=rotated-{identity}; Domain=.google.com; Path=/"],
                    location="https://labs.google/fx/api/auth/callback/google?code=test",
                ),
                response(
                    302, cookies=[f"__Secure-next-auth.session-token=st-{identity}; Path=/; Secure"]
                ),
            ]

        sessions = [FakeSession(oauth("first")), FakeSession(oauth("second"))]
        with patch("src.services.protocol_login.AsyncSession", side_effect=sessions):
            first, second = await asyncio.gather(
                ProtocolLogin().login("SID=first"), ProtocolLogin().login("SID=second")
            )
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(json.loads(first["google_cookies"])[0]["value"], "rotated-first")
        self.assertEqual(json.loads(second["google_cookies"])[0]["value"], "rotated-second")
        self.assertEqual(sessions[0].requests[2][1]["headers"]["Cookie"], "SID=first")
        self.assertEqual(sessions[1].requests[2][1]["headers"]["Cookie"], "SID=second")


if __name__ == "__main__":
    unittest.main()
