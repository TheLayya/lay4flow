import asyncio
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.token_account_probe import TokenManagerAccountProbe
from src.services.flow.flow_rpc import FlowRPCError


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

    def _new_probe(self, email="user@example.com", *, discover_identity=False):
        token = SimpleNamespace(email=email)
        context = SimpleNamespace(request=SimpleNamespace())
        return TokenManagerAccountProbe(
            object(), token, discover_identity=discover_identity
        ), context

    async def test_token_manager_probe_uses_flow_identity_and_credits(self):
        probe, context = self._new_probe()
        session = {
            "email": "USER@example.com",
            "credits": 8,
            "user_paygate_tier": "PAYGATE_TIER_ONE",
        }
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            new=AsyncMock(return_value=session),
        ) as read:
            result = await probe.verify("USER@example.com", context)
        read.assert_awaited_once_with(context)
        self.assertEqual(result, {"email": "user@example.com", "session_valid": True, "credits": 8})
        self.assertEqual(probe.network_step, "flow_credits")
        self.assertEqual(
            probe.validated_session,
            {
                "at": None,
                "at_expires": None,
                "credits": 8,
                "user_paygate_tier": "PAYGATE_TIER_ONE",
            },
        )

    async def test_token_manager_probe_requires_context_and_does_not_use_legacy_labs(self):
        probe, _ = self._new_probe()
        result = await probe.verify("user@example.com")
        self.assertEqual(result["reason"], "session_invalid")
        self.assertIsNone(probe.validated_session)

    async def test_token_manager_probe_rejects_expected_identity_before_network(self):
        probe, context = self._new_probe("other@example.com")
        with patch(
            "src.services.browser.token_account_probe.read_flow_session", new=AsyncMock()
        ) as read:
            result = await probe.verify("user@example.com", context)
        self.assertEqual(result["reason"], "identity_mismatch")
        read.assert_not_awaited()

    async def test_token_manager_probe_accepts_only_gmail_domain_alias(self):
        for observed, expected, accepted in (
            ("user@googlemail.com", "user@gmail.com", True),
            ("user+tag@googlemail.com", "user@gmail.com", False),
            ("u.ser@googlemail.com", "user@gmail.com", False),
            ("other@googlemail.com", "user@gmail.com", False),
            ("user@corp.example", "user@gmail.com", False),
        ):
            with self.subTest(observed=observed):
                probe, context = self._new_probe(expected)
                with patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    new=AsyncMock(return_value={"email": observed, "credits": 1}),
                ) as read:
                    result = await probe.verify(expected, context)
                self.assertEqual(result.get("session_valid", False), accepted)
                if accepted:
                    read.assert_awaited_once_with(context)
                else:
                    self.assertEqual(result["reason"], "identity_mismatch")

    async def test_token_manager_probe_maps_flow_rpc_errors_without_secrets(self):
        token = "private-token-and-upstream-message"
        for error, reason, step in (
            (
                FlowRPCError("login_required", "flow_bootstrap", 401),
                "login_required",
                "flow_bootstrap",
            ),
            (
                FlowRPCError("identity_mismatch", "flow_identity"),
                "identity_mismatch",
                "flow_identity",
            ),
            (FlowRPCError("credits_invalid", "flow_credits"), "credits_invalid", "flow_credits"),
            (TimeoutError(token), "transient_error", "flow_bootstrap"),
            (ConnectionError(token), "transient_error", "flow_bootstrap"),
            (RuntimeError(token), "account_probe_error", "flow_bootstrap"),
        ):
            with self.subTest(reason=reason, step=step):
                probe, context = self._new_probe()
                with patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    new=AsyncMock(side_effect=error),
                ):
                    result = await probe.verify("user@example.com", context)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(
                    probe.network_step,
                    step if isinstance(error, FlowRPCError) else "flow_bootstrap",
                )
                self.assertNotIn(token, repr(result))

    async def test_token_manager_probe_rejects_malformed_flow_payload(self):
        for value in (
            {"email": "user@example.com", "credits": True},
            {"email": "user@example.com", "credits": -1},
            {"email": "user@example.com", "credits": "8"},
            {"email": "", "credits": 8},
        ):
            with self.subTest(value=value):
                probe, context = self._new_probe()
                with patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    new=AsyncMock(return_value=value),
                ):
                    result = await probe.verify("user@example.com", context)
                expected = "identity_mismatch" if not value.get("email") else "credits_invalid"
                self.assertEqual(result["reason"], expected)

    async def test_discover_identity_accepts_observed_mailbox_without_expected_match(self):
        probe, context = self._new_probe("configured@example.com", discover_identity=True)
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            new=AsyncMock(return_value={"email": "observed@example.com", "credits": 2}),
        ):
            result = await probe.verify("configured@example.com", context)
        self.assertEqual(
            result, {"email": "observed@example.com", "session_valid": True, "credits": 2}
        )


if __name__ == "__main__":
    unittest.main()
