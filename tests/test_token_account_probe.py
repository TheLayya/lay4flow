"""Synthetic current Flow identity/credits diagnostics, with no external requests."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.token_account_probe import TokenManagerAccountProbe
from src.services.flow.flow_rpc import FlowRPCError

EMAIL = "fixture@example.invalid"
SECRET = "proxy://fixture-user:fixture-password@fixture.invalid?token=fixture-token"


class TokenAccountProbeTests(unittest.IsolatedAsyncioTestCase):
    def probe(self, *, email=EMAIL, discover_identity=False):
        context = object()
        manager = SimpleNamespace(_validate_refresh_session=AsyncMock())
        token = SimpleNamespace(email=email)
        probe = TokenManagerAccountProbe(manager, token, discover_identity=discover_identity)
        probe.set_context(context)
        return probe, context

    async def test_valid_flow_session_is_fully_validated_without_legacy_tokens(self):
        probe, context = self.probe()
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(
                return_value={"email": EMAIL, "credits": 5, "user_paygate_tier": "PAYGATE_TIER_ONE"}
            ),
        ) as read:
            result = await probe.verify(EMAIL)
        self.assertEqual(result, {"email": EMAIL, "session_valid": True, "credits": 5})
        self.assertEqual(
            probe.validated_session,
            {
                "at": None,
                "at_expires": None,
                "credits": 5,
                "user_paygate_tier": "PAYGATE_TIER_ONE",
            },
        )
        self.assertEqual(probe.network_step, "flow_credits")
        read.assert_awaited_once_with(context)
        probe.token_manager._validate_refresh_session.assert_not_awaited()

    async def test_missing_browser_context_never_uses_token_manager_side_channel(self):
        probe = TokenManagerAccountProbe(
            SimpleNamespace(_validate_refresh_session=AsyncMock()), SimpleNamespace(email=EMAIL)
        )
        with patch(
            "src.services.browser.token_account_probe.read_flow_session", AsyncMock()
        ) as read:
            self.assertEqual((await probe.verify(EMAIL))["reason"], "session_invalid")
        read.assert_not_awaited()
        probe.token_manager._validate_refresh_session.assert_not_awaited()

    async def test_expected_account_mismatch_skips_all_requests(self):
        probe, _ = self.probe()
        with patch(
            "src.services.browser.token_account_probe.read_flow_session", AsyncMock()
        ) as read:
            result = await probe.verify("another@example.invalid")
        self.assertEqual(result["reason"], "identity_mismatch")
        self.assertIsNone(probe.validated_session)
        read.assert_not_awaited()

    async def test_observed_account_mismatch_is_explicit(self):
        probe, _ = self.probe()
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(return_value={"email": "other@example.invalid", "credits": 5}),
        ):
            self.assertEqual((await probe.verify(EMAIL))["reason"], "identity_mismatch")
        self.assertEqual(probe.observed_email, "other@example.invalid")
        self.assertIsNone(probe.validated_session)
        self.assertEqual(probe.network_step, "flow_identity")

    async def test_discovery_adopts_only_valid_official_identity_and_credits(self):
        probe, context = self.probe(
            email="profile-pending-synthetic@local.invalid", discover_identity=True
        )
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(return_value={"email": " Actual.User@gmail.com ", "credits": 0}),
        ) as read:
            result = await probe.verify("")
        self.assertEqual(
            result, {"email": "actual.user@gmail.com", "session_valid": True, "credits": 0}
        )
        self.assertEqual(probe.observed_email, "actual.user@gmail.com")
        self.assertIsNotNone(probe.validated_session)
        read.assert_awaited_once_with(context)

    async def test_discovery_rejects_missing_identity_and_invalid_credit_values(self):
        for payload, reason in (
            ({"email": "", "credits": 1}, "identity_mismatch"),
            ({"email": "missing-domain", "credits": 1}, "identity_mismatch"),
            ({"email": EMAIL, "credits": -1}, "credits_invalid"),
            ({"email": EMAIL, "credits": True}, "credits_invalid"),
            ({"email": EMAIL, "credits": "5"}, "credits_invalid"),
        ):
            with self.subTest(payload=payload):
                probe, _ = self.probe(discover_identity=True)
                with patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    AsyncMock(return_value=payload),
                ):
                    self.assertEqual((await probe.verify(""))["reason"], reason)
                self.assertIsNone(probe.validated_session)

    async def test_flow_errors_preserve_only_structured_reason_and_network_step(self):
        for reason, step in (
            ("login_required", "flow_bootstrap"),
            ("identity_mismatch", "flow_identity"),
            ("credits_invalid", "flow_credits"),
            ("transient_error", "flow_credits"),
            ("account_probe_error", "flow_identity"),
        ):
            with self.subTest(reason=reason):
                probe, _ = self.probe()
                with patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    AsyncMock(side_effect=FlowRPCError(reason, step, 503)),
                ):
                    result = await probe.verify(EMAIL)
                self.assertEqual(result, {"reason": reason})
                self.assertEqual(probe.network_step, step)
                self.assertIsNone(probe.validated_session)

    async def test_unexpected_errors_do_not_expose_secret_text(self):
        for error, reason in (
            (TimeoutError(SECRET), "transient_error"),
            (ConnectionError(SECRET), "transient_error"),
            (RuntimeError(SECRET), "account_probe_error"),
        ):
            with self.subTest(error=type(error).__name__):
                probe, _ = self.probe()
                with patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    AsyncMock(side_effect=error),
                ):
                    result = await probe.verify(EMAIL)
                self.assertEqual(result["reason"], reason)
                self.assertNotIn(SECRET, repr(result))

    async def test_each_verify_uses_current_context_and_resets_previous_success(self):
        probe, first = self.probe()
        second = object()
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(
                side_effect=[
                    {"email": EMAIL, "credits": 1},
                    FlowRPCError("login_required", "flow_bootstrap"),
                ]
            ),
        ) as read:
            self.assertTrue((await probe.verify(EMAIL))["session_valid"])
            self.assertEqual((await probe.verify(EMAIL, second))["reason"], "login_required")
        self.assertEqual(read.await_args_list[0].args, (first,))
        self.assertEqual(read.await_args_list[1].args, (second,))
        self.assertIsNone(probe.validated_session)
        self.assertEqual(probe.observed_email, "")

    async def test_verifier_uses_existing_identity_policy_after_rpc(self):
        for official, expected in (
            (EMAIL.upper(), EMAIL),
            ("person@googlemail.com", "person@gmail.com"),
        ):
            probe, _ = self.probe(email=expected)
            with patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": official, "credits": 1}),
            ):
                result = await ProfileVerifier().verify_account(expected, probe)
            self.assertEqual(result.reason, "account_ready")


if __name__ == "__main__":
    unittest.main()
