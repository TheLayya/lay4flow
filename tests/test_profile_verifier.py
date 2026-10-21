import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.token_account_probe import TokenManagerAccountProbe
from src.services.token_manager import RefreshFailure, TokenManager


def snapshot(text="edit\ndelete", *, path="/", ready="complete"):
    return {"url": "https://flow.google.com" + path, "ready": ready, "text": text}


class ProfileVerifierTests(unittest.IsolatedAsyncioTestCase):
    def context(self, values):
        current = iter(values)
        last = values[-1]

        async def evaluate(expression):
            nonlocal last
            last = next(current, last)
            return last

        page = SimpleNamespace(
            goto=AsyncMock(), evaluate=AsyncMock(side_effect=evaluate), close=AsyncMock()
        )
        return SimpleNamespace(new_page=AsyncMock(return_value=page)), page

    async def test_waits_for_complete_workspace_and_closes_only_probe(self):
        context, page = self.context([snapshot("", ready="loading"), snapshot()])
        result = await ProfileVerifier(timeout_seconds=1, poll_seconds=0.001).verify(context)
        self.assertTrue(result.workspace_visible)
        self.assertEqual(result.reason, "workspace_visible")
        self.assertEqual(page.evaluate.await_count, 2)
        page.close.assert_awaited_once()
        page.goto.assert_awaited_once_with(
            "https://flow.google.com/", wait_until="domcontentloaded"
        )

    async def test_empty_workspace_can_be_visible(self):
        for label in ("新建项目", "New project"):
            with self.subTest(label=label):
                context, _ = self.context([snapshot(label)])
                self.assertTrue((await ProfileVerifier().verify(context)).workspace_visible)

    async def test_public_page_never_passes_with_project_words(self):
        context, _ = self.context([snapshot("New project\nedit\ndelete", path="/about")])
        result = await ProfileVerifier().verify(context)
        self.assertFalse(result.workspace_visible)
        self.assertEqual(result.reason, "public_page")

    async def test_login_redirect_and_signed_out_text_are_rejected(self):
        values = [
            {"url": "https://accounts.google.com/signin?secret=synthetic", "ready": "complete"},
            snapshot("新建项目\n已退出账号"),
            snapshot("New project\nSign in"),
        ]
        for value in values:
            context, _ = self.context([value])
            result = await ProfileVerifier().verify(context)
            self.assertEqual(result.reason, "login_required")
            self.assertNotIn("synthetic", repr(result))

    async def test_root_url_alone_and_substring_words_do_not_pass(self):
        for value in (snapshot(""), snapshot("editors delete history"), snapshot(path="/project")):
            context, page = self.context([value])
            result = await ProfileVerifier(timeout_seconds=0.02, poll_seconds=0.001).verify(context)
            self.assertEqual(result.reason, "timeout")
            self.assertFalse(result.workspace_visible)
            page.close.assert_awaited_once()

    async def test_unexpected_origin_is_rejected(self):
        context, _ = self.context(
            [
                {
                    "url": "https://flow.google.com.evil.invalid/",
                    "ready": "complete",
                    "text": "edit\ndelete",
                }
            ]
        )
        self.assertEqual((await ProfileVerifier().verify(context)).reason, "unexpected_page")

    async def test_network_failure_does_not_leak_exception_or_claim_logout(self):
        context, page = self.context([snapshot()])
        page.goto.side_effect = RuntimeError("secret-cookie: ERR_CONNECTION_CLOSED")
        result = await ProfileVerifier().verify(context)
        self.assertEqual(result.reason, "probe_error")
        self.assertNotIn("secret-cookie", repr(result))
        page.close.assert_awaited_once()

    async def test_navigation_timeout_is_unknown_not_logout(self):
        context, page = self.context([snapshot()])
        page.goto.side_effect = TimeoutError("synthetic")
        self.assertEqual((await ProfileVerifier().verify(context)).reason, "timeout")

    async def test_cancellation_propagates_and_closes_probe(self):
        context, page = self.context([snapshot()])
        started = asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        page.goto.side_effect = blocked
        task = asyncio.create_task(ProfileVerifier().verify(context))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        page.close.assert_awaited_once()

    async def test_cleanup_failure_invalidates_success(self):
        context, page = self.context([snapshot()])
        page.close.side_effect = RuntimeError("synthetic")
        result = await ProfileVerifier().verify(context)
        self.assertFalse(result.workspace_visible)
        self.assertEqual(result.reason, "cleanup_error")

    async def test_environment_snapshot_is_recorded_and_reused(self):
        value = {
            "userAgent": "Mozilla/5.0 synthetic",
            "language": "zh-CN",
            "languages": ["zh-CN", "en-US"],
            "timezone": "Asia/Shanghai",
            "platform": "Win32",
            "width": 1440,
            "height": 900,
        }
        context, page = self.context([value, value, {**value, "timezone": "UTC"}])
        with self.subTest("first snapshot"):
            with TemporaryDirectory() as root:
                verifier = ProfileVerifier()
                self.assertEqual(
                    await verifier.verify_environment(context, root), "environment_stable"
                )
                self.assertEqual(
                    await verifier.verify_environment(context, root), "environment_stable"
                )
                self.assertEqual(
                    await verifier.verify_environment(context, root), "environment_changed"
                )
                self.assertEqual(page.close.await_count, 3)

    async def test_environment_snapshot_rejects_incomplete_values(self):
        context, _ = self.context([{"language": "zh-CN"}])
        with self.assertRaises(ValueError):
            await ProfileVerifier().environment_snapshot(context)

    async def test_live_viewer_allows_only_viewport_change_without_replacing_baseline(self):
        value = {
            "userAgent": "Mozilla/5.0 synthetic",
            "language": "en-US",
            "languages": ["en-US", "en"],
            "timezone": "UTC",
            "platform": "Linux x86_64",
            "width": 1920,
            "height": 947,
        }
        changed = {**value, "width": 1440, "height": 747}
        context, page = self.context(
            [value, changed, changed, {**changed, "timezone": "Asia/Shanghai"}]
        )
        with TemporaryDirectory() as root:
            verifier = ProfileVerifier()
            self.assertEqual(await verifier.verify_environment(context, root), "environment_stable")
            self.assertEqual(
                await verifier.verify_environment(context, root), "environment_changed"
            )
            self.assertEqual(
                await verifier.verify_environment(context, root, allow_viewport_change=True),
                "environment_stable",
            )
            self.assertEqual(
                await verifier.verify_environment(context, root, allow_viewport_change=True),
                "environment_changed",
            )

    async def test_verify_ready_blocks_before_workspace_probe(self):
        value = {
            "userAgent": "Mozilla/5.0 synthetic",
            "language": "zh-CN",
            "languages": ["zh-CN"],
            "timezone": "Asia/Shanghai",
            "platform": "Win32",
            "width": 1440,
            "height": 900,
        }
        context, page = self.context([value, snapshot()])
        with TemporaryDirectory() as root:
            verifier = ProfileVerifier()
            first = await verifier.verify_ready(context, root)
            self.assertEqual(first.reason, "workspace_visible")
            page.evaluate.side_effect = AsyncMock(return_value={**value, "timezone": "UTC"})
            blocked = await verifier.verify_ready(context, root)
            self.assertEqual(blocked.reason, "environment_changed")
            self.assertEqual(page.goto.await_count, 3)

    async def test_verify_ready_reports_environment_probe_failure(self):
        context, page = self.context([{"language": "zh-CN"}])
        with TemporaryDirectory() as root:
            result = await ProfileVerifier().verify_ready(context, root)
            self.assertEqual(result.reason, "environment_probe_error")
            page.close.assert_awaited_once()

    def test_invalid_timing_is_rejected(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                ProfileVerifier(timeout_seconds=value)
            with self.assertRaises(ValueError):
                ProfileVerifier(poll_seconds=value)

    async def test_environment_timeout_closes_probe_and_does_not_record_baseline(self):
        context, page = self.context([snapshot()])

        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        page.evaluate.side_effect = blocked
        with TemporaryDirectory() as root:
            result = await ProfileVerifier(timeout_seconds=0.02).verify_ready(context, root)
            self.assertEqual(result.reason, "environment_timeout")
            page.close.assert_awaited_once()
            self.assertEqual(page.goto.await_count, 1)

    async def test_environment_runtime_failure_is_safe(self):
        context, page = self.context([snapshot()])
        page.evaluate.side_effect = RuntimeError("secret-cookie synthetic")
        with TemporaryDirectory() as root:
            result = await ProfileVerifier().verify_ready(context, root)
            self.assertEqual(result.reason, "environment_probe_error")
            self.assertNotIn("secret-cookie", repr(result))
            page.close.assert_awaited_once()

    async def test_account_probe_requires_identity_session_and_credits_shape(self):
        class Probe:
            def __init__(self, value):
                self.value = value

            async def verify(self, expected_email):
                return self.value

        verifier = ProfileVerifier()
        ready = await verifier.verify_account(
            "User@example.com",
            Probe({"email": "user@example.com", "session_valid": True, "credits": 0}),
        )
        self.assertEqual(ready.reason, "account_ready")
        for value, reason in (
            (
                {"email": "other@example.com", "session_valid": True, "credits": 4},
                "identity_mismatch",
            ),
            (
                {"email": "user@example.com", "session_valid": False, "credits": 4},
                "session_invalid",
            ),
            (
                {"email": "user@example.com", "session_valid": True, "credits": -1},
                "credits_invalid",
            ),
            (
                {"email": "user@example.com", "session_valid": True, "credits": True},
                "credits_invalid",
            ),
        ):
            with self.subTest(reason=reason):
                result = await verifier.verify_account("User@example.com", Probe(value))
                self.assertEqual(result.reason, reason)

    async def test_token_manager_probe_reuses_canonical_validation(self):
        class Manager:
            def __init__(self, result=None, error=None):
                self.result = result
                self.error = error
                self.calls = []

            async def _validate_refresh_session(self, token, st):
                self.calls.append((token, st))
                if self.error:
                    raise self.error
                return self.result

        token = type("Token", (), {"email": "user@example.com"})()
        manager = Manager({"st": "rotated", "at": "valid", "credits": 8})
        probe = TokenManagerAccountProbe(manager, token, "synthetic-st")
        result = await probe.verify("USER@example.com")
        self.assertEqual(result, {"email": "user@example.com", "session_valid": True, "credits": 8})
        self.assertEqual(manager.calls, [(token, "synthetic-st")])

        mismatch = await TokenManagerAccountProbe(manager, token, "synthetic-st").verify(
            "other@example.com"
        )
        self.assertFalse(mismatch["session_valid"])
        failed = await TokenManagerAccountProbe(
            Manager(error=RuntimeError("secret token")), token, "synthetic-st"
        ).verify("user@example.com")
        self.assertNotIn("secret", repr(failed))

    async def test_token_manager_probe_reads_current_profile_session_cookie(self):
        class Context:
            async def cookies(self):
                return [
                    {
                        "name": "__Secure-next-auth.session-token.1",
                        "value": "second",
                        "domain": "labs.google",
                        "path": "/",
                    },
                    {
                        "name": "__Secure-next-auth.session-token.0",
                        "value": "first",
                        "domain": ".labs.google",
                        "path": "/",
                    },
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "foreign",
                        "domain": "evil.test",
                    },
                ]

        class Manager:
            async def _validate_refresh_session(self, token, st):
                self.st = st
                return {"st": st, "at": "valid", "credits": 1}

        token = type("Token", (), {"email": "user@example.com"})()
        manager = Manager()
        probe = TokenManagerAccountProbe(manager, token)
        probe.set_context(Context())
        result = await probe.verify("user@example.com")
        self.assertTrue(result["session_valid"])
        self.assertEqual(manager.st, "firstsecond")

    async def test_token_manager_probe_preserves_safe_refresh_failure_classification(self):
        token = SimpleNamespace(email="user@example.com")
        for code, reason in (
            ("session_invalid", "session_invalid"),
            ("login_required", "login_required"),
            ("invalid_cookies", "identity_mismatch"),
            ("transient_error", "transient_error"),
            ("invalid_proxy", "invalid_proxy"),
            ("proxy_error", "proxy_error"),
        ):
            with self.subTest(code=code):
                manager = SimpleNamespace(
                    _validate_refresh_session=AsyncMock(
                        side_effect=RefreshFailure("secret-cookie", code)
                    )
                )
                probe = TokenManagerAccountProbe(manager, token, "synthetic-st")
                result = await probe.verify(token.email)
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["session_valid"])
                self.assertNotIn("secret", repr(result))

    async def test_token_manager_probe_rejects_missing_current_profile_session(self):
        class Context:
            async def cookies(self):
                return []

        token = type("Token", (), {"email": "user@example.com"})()
        probe = TokenManagerAccountProbe(object(), token)
        probe.set_context(Context())
        result = await probe.verify("user@example.com")
        self.assertFalse(result["session_valid"])

    def _browser_probe(self, *, session_json, credits_response):
        class Response:
            def __init__(self, status, payload):
                self.status = status
                self.payload = payload

            async def json(self):
                return self.payload

        calls = []

        async def get(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/session"):
                return Response(200, session_json)
            return credits_response

        class Context:
            request = SimpleNamespace(get=get)
            cookie_reads = 0

            async def cookies(self):
                self.cookie_reads += 1
                return [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "initial-st" if self.cookie_reads == 1 else "rotated-st",
                        "domain": "labs.google",
                        "path": "/",
                    }
                ]

        token = SimpleNamespace(email="user@example.com")

        class Manager:
            flow_client = SimpleNamespace(api_base_url="https://flow.google.com/api")

            def _parse_at_expires(self, value):
                return TokenManager._parse_at_expires(self, value)

            def _as_utc(self, value):
                return TokenManager._as_utc(self, value)

        return TokenManagerAccountProbe(Manager(), token), Context(), calls

    async def test_browser_probe_expired_access_token_never_calls_credits(self):
        probe, context, calls = self._browser_probe(
            session_json={
                "access_token": "expired-at",
                "expires": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "user": {"email": "user@example.com"},
            },
            credits_response=SimpleNamespace(
                status=200, json=AsyncMock(return_value={"credits": 99})
            ),
        )
        result = await probe.verify("user@example.com", context)
        self.assertEqual(result["reason"], "session_invalid")
        self.assertIsNone(probe.validated_session)
        self.assertEqual(len(calls), 1)

    async def test_browser_probe_identity_mismatch_never_calls_credits(self):
        probe, context, calls = self._browser_probe(
            session_json={
                "access_token": "valid-at",
                "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "user": {"email": "other@example.com"},
            },
            credits_response=SimpleNamespace(
                status=200, json=AsyncMock(return_value={"credits": 99})
            ),
        )
        result = await probe.verify("user@example.com", context)
        self.assertEqual(result["reason"], "identity_mismatch")
        self.assertIsNone(probe.validated_session)
        self.assertEqual(len(calls), 1)

    async def test_browser_probe_credits_401_is_session_invalid(self):
        probe, context, calls = self._browser_probe(
            session_json={
                "access_token": "valid-at",
                "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "user": {"email": "user@example.com"},
            },
            credits_response=SimpleNamespace(status=401, json=AsyncMock()),
        )
        result = await probe.verify("user@example.com", context)
        self.assertEqual(result["reason"], "session_invalid")
        self.assertIsNone(probe.validated_session)
        self.assertEqual(len(calls), 2)

    async def test_browser_probe_malformed_credits_never_marks_session_valid(self):
        for value in (True, -1, "1040"):
            with self.subTest(value=value):
                probe, context, calls = self._browser_probe(
                    session_json={
                        "access_token": "valid-at",
                        "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                        "user": {"email": "user@example.com"},
                    },
                    credits_response=SimpleNamespace(
                        status=200, json=AsyncMock(return_value={"credits": value})
                    ),
                )
                result = await probe.verify("user@example.com", context)
                self.assertEqual(result["reason"], "credits_invalid")
                self.assertIsNone(probe.validated_session)
                self.assertEqual(len(calls), 2)

    async def test_browser_probe_success_records_validated_session_and_rotated_st(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        probe, context, calls = self._browser_probe(
            session_json={
                "access_token": "valid-at",
                "expires": expires.isoformat(),
                "user": {"email": "user@example.com"},
            },
            credits_response=SimpleNamespace(
                status=200, json=AsyncMock(return_value={"credits": 1040, "userPaygateTier": "pro"})
            ),
        )
        result = await probe.verify("user@example.com", context)
        self.assertEqual(
            result, {"email": "user@example.com", "session_valid": True, "credits": 1040}
        )
        self.assertEqual(probe.validated_session["st"], "rotated-st")
        self.assertEqual(probe.validated_session["at"], "valid-at")
        self.assertEqual(probe.validated_session["at_expires"], expires)
        self.assertEqual(probe.validated_session["credits"], 1040)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], "https://flow.google.com/api/credits")

    async def test_account_verification_accepts_googlemail_alias(self):
        class Probe:
            async def verify(self, expected_email):
                return {"email": "first.last@googlemail.com", "session_valid": True, "credits": 1}

        result = await ProfileVerifier().verify_account("first.last@gmail.com", Probe())
        self.assertEqual(result.reason, "account_ready")

    async def test_browser_probe_accepts_matching_googlemail_alias(self):
        probe, context, calls = self._browser_probe(
            session_json={
                "access_token": "valid-at",
                "expires": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                "user": {"email": "user@googlemail.com"},
            },
            credits_response=SimpleNamespace(
                status=200, json=AsyncMock(return_value={"credits": 8})
            ),
        )
        probe.token.email = "user@gmail.com"
        result = await probe.verify("USER@gmail.com", context)
        self.assertTrue(result["session_valid"])
        self.assertEqual(len(calls), 2)

    async def test_browser_probe_rejects_other_googlemail_mailboxes(self):
        for observed in (
            "other@googlemail.com",
            "user+tag@googlemail.com",
            "u.ser@googlemail.com",
            "user@example.com",
        ):
            with self.subTest(observed=observed):
                probe, context, calls = self._browser_probe(
                    session_json={
                        "access_token": "valid-at",
                        "expires": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                        "user": {"email": observed},
                    },
                    credits_response=SimpleNamespace(
                        status=200, json=AsyncMock(return_value={"credits": 8})
                    ),
                )
                probe.token.email = "user@gmail.com"
                result = await probe.verify("user@gmail.com", context)
                self.assertEqual(result["reason"], "identity_mismatch")
                self.assertIsNone(probe.validated_session)
                self.assertEqual(len(calls), 1)

    async def test_browser_probe_navigation_response_is_single_use_and_reads_current_cookie(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        session = {
            "access_token": "navigation-at",
            "expires": expires,
            "user": {"email": "user@example.com"},
        }
        probe, context, calls = self._browser_probe(
            session_json={**session, "access_token": "renewed-at"},
            credits_response=SimpleNamespace(
                status=200, json=AsyncMock(return_value={"credits": 7})
            ),
        )
        probe.set_context(context, session_response=(200, session))
        result = await probe.verify("user@example.com")
        self.assertTrue(result["session_valid"])
        self.assertEqual(probe.validated_session["at"], "navigation-at")
        self.assertEqual(probe.validated_session["st"], "rotated-st")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "https://flow.google.com/api/credits")
        result = await probe.verify("user@example.com")
        self.assertTrue(result["session_valid"])
        self.assertEqual(probe.validated_session["at"], "renewed-at")
        self.assertEqual(calls[1][0], "https://labs.google/fx/api/auth/session")
        self.assertEqual(len(calls), 3)

    async def test_browser_probe_ignores_navigation_response_for_another_context(self):
        session = {
            "access_token": "current-at",
            "expires": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "user": {"email": "user@example.com"},
        }
        probe, context, calls = self._browser_probe(
            session_json=session,
            credits_response=SimpleNamespace(
                status=200, json=AsyncMock(return_value={"credits": 7})
            ),
        )
        probe.set_context(object(), session_response=(401, None))
        result = await probe.verify("user@example.com", context)
        self.assertTrue(result["session_valid"])
        self.assertEqual(calls[0][0], "https://labs.google/fx/api/auth/session")
        self.assertEqual(probe.validated_session["at"], "current-at")


if __name__ == "__main__":
    unittest.main()
