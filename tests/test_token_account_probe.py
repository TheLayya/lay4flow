"""Synthetic diagnostics for browser session, identity and transport failures."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.token_account_probe import SESSION_URL, TokenManagerAccountProbe
from src.services.token_manager import RefreshFailure, TokenManager

EMAIL = "fixture@example.invalid"
SECRET = "proxy://fixture-user:fixture-password@fixture.invalid?token=fixture-token"


class TokenAccountProbeTests(unittest.IsolatedAsyncioTestCase):
    def probe(self, *, cookies=None, session_email=EMAIL):
        token = SimpleNamespace(email=EMAIL)
        manager = TokenManager(
            object(), SimpleNamespace(api_base_url="https://flow.google.com/api")
        )
        manager._validate_refresh_session = AsyncMock(
            side_effect=AssertionError("browser probe must use its own request context")
        )
        session = SimpleNamespace(
            status=200,
            json=AsyncMock(
                return_value={
                    "access_token": "fixture-at",
                    "expires": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                    "user": {"email": session_email},
                }
            ),
        )
        credits = SimpleNamespace(status=200, json=AsyncMock(return_value={"credits": 5}))
        context = SimpleNamespace(
            cookies=AsyncMock(
                return_value=(
                    cookies
                    if cookies is not None
                    else [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "fixture-st",
                            "domain": "labs.google",
                            "path": "/",
                        }
                    ]
                )
            ),
            request=SimpleNamespace(get=AsyncMock(side_effect=[session, credits])),
        )
        probe = TokenManagerAccountProbe(manager, token)
        probe.set_context(context)
        return probe, context, session, credits

    async def test_missing_browser_session_is_not_reported_as_identity_mismatch(self):
        probe, context, _, _ = self.probe(cookies=[])
        verified = await ProfileVerifier().verify_account(EMAIL, probe)
        self.assertEqual(verified.reason, "session_invalid")
        self.assertIsNone(probe.validated_session)
        context.request.get.assert_not_awaited()
        probe.token_manager._validate_refresh_session.assert_not_awaited()

    async def test_missing_explicit_session_is_reported_before_side_channel_validation(self):
        manager = SimpleNamespace(_validate_refresh_session=AsyncMock())
        probe = TokenManagerAccountProbe(manager, SimpleNamespace(email=EMAIL))
        result = await probe.verify(EMAIL)
        self.assertEqual(result["reason"], "session_invalid")
        manager._validate_refresh_session.assert_not_awaited()

    async def test_token_and_expected_account_mismatch_is_explicit(self):
        probe, context, _, _ = self.probe()
        result = await probe.verify("another@example.invalid")
        self.assertEqual(result["reason"], "identity_mismatch")
        self.assertIsNone(probe.validated_session)
        context.cookies.assert_not_awaited()
        context.request.get.assert_not_awaited()

    async def test_session_identity_mismatch_never_requests_credits(self):
        probe, context, _, _ = self.probe(session_email="another@example.invalid")
        result = await probe.verify(EMAIL)
        self.assertEqual(result["reason"], "identity_mismatch")
        self.assertEqual(context.request.get.await_count, 1)
        self.assertEqual(context.request.get.call_args.args[0], SESSION_URL)
        self.assertIsNone(probe.validated_session)

    async def test_known_timeouts_and_connection_failures_are_transient(self):
        for error in (
            asyncio.TimeoutError(SECRET),
            PlaywrightTimeoutError(SECRET),
            ConnectionResetError(SECRET),
            PlaywrightError(SECRET),
        ):
            for step in ("session", "credits"):
                with self.subTest(error=type(error).__name__, step=step):
                    probe, context, session, _ = self.probe()
                    context.request.get.side_effect = (
                        error if step == "session" else [session, error]
                    )
                    result = await probe.verify(EMAIL)
                    self.assertEqual(result["reason"], "transient_error")
                    self.assertIsNone(probe.validated_session)
                    self.assertNotIn(SECRET, repr(result))

    async def test_generic_probe_and_json_errors_remain_probe_errors(self):
        for step in ("cookies", "request", "session_json", "credits_json"):
            with self.subTest(step=step):
                probe, context, session, credits = self.probe()
                if step == "cookies":
                    context.cookies.side_effect = PlaywrightError(SECRET)
                elif step == "request":
                    context.request.get.side_effect = RuntimeError(SECRET)
                elif step == "session_json":
                    session.json.side_effect = ValueError(SECRET)
                else:
                    credits.json.side_effect = PlaywrightError(SECRET)
                result = await probe.verify(EMAIL)
                self.assertEqual(result["reason"], "account_probe_error")
                self.assertIsNone(probe.validated_session)
                self.assertNotIn(SECRET, repr(result))

    async def test_known_refresh_failure_codes_keep_their_diagnostics(self):
        for code, reason in (
            ("session_invalid", "session_invalid"),
            ("login_required", "login_required"),
            ("invalid_cookies", "identity_mismatch"),
            ("transient_error", "transient_error"),
            ("invalid_proxy", "invalid_proxy"),
            ("proxy_error", "proxy_error"),
            ("unrecognized", "account_probe_error"),
        ):
            with self.subTest(code=code):
                manager = SimpleNamespace(
                    _validate_refresh_session=AsyncMock(side_effect=RefreshFailure(SECRET, code))
                )
                probe = TokenManagerAccountProbe(
                    manager, SimpleNamespace(email=EMAIL), "fixture-st"
                )
                result = await probe.verify(EMAIL)
                self.assertEqual(result["reason"], reason)
                self.assertNotIn(SECRET, repr(result))

    async def test_valid_browser_session_keeps_full_identity_and_credits_validation(self):
        probe, context, _, _ = self.probe()
        result = await probe.verify(EMAIL)
        self.assertEqual(result, {"email": EMAIL, "session_valid": True, "credits": 5})
        self.assertEqual(probe.validated_session["st"], "fixture-st")
        self.assertEqual(probe.validated_session["at"], "fixture-at")
        self.assertEqual(context.request.get.await_count, 2)
        probe.token_manager._validate_refresh_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
