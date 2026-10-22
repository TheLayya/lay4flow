"""Synthetic contract tests for FlowProfileGeneration; no real browser/network."""

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.services.browser.flow_generation import FlowProfileGeneration
from src.services.flow.flow_rpc import FlowRPCError
from src.services.flow.remote_captcha import RemoteCaptchaError


class FlowProfileGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.token = SimpleNamespace(
            id=7,
            email="user@example.com",
            st="",
            google_cookies='{"SID":"synthetic"}',
            proxy_url="http://proxy.invalid:8080",
            proxy_pool_id=2,
            browser_profile_source="cloak",
            browser_profile_epoch=1,
            is_active=True,
            ban_reason=None,
        )
        self.context = object()

        @asynccontextmanager
        async def request_context(tokens, token, proxy):
            self.assertEqual(proxy, "http://proxy.invalid:8080")
            try:
                yield self.context
            finally:
                self.owned_closed()

        self.enterContext(
            patch("src.services.browser.flow_generation.account_http_session", request_context)
        )
        self.current = SimpleNamespace(**self.token.__dict__)
        self.db = SimpleNamespace(get_token=AsyncMock(return_value=self.current))
        self.owned_closed = Mock()

        self.refresher = SimpleNamespace(
            _proxy_url=AsyncMock(return_value="http://proxy.invalid:8080"),
            active_context_provider=None,
            manager=SimpleNamespace(
                credential_session=Mock(side_effect=AssertionError("browser opened"))
            ),
            verifier=object(),
        )
        self.tokens = SimpleNamespace(
            db=self.db,
            profile_refresher=self.refresher,
            resolve_account_proxy=AsyncMock(return_value="http://proxy.invalid:8080"),
        )
        self.client = SimpleNamespace(
            _get_recaptcha_token=AsyncMock(return_value=("captcha-fixture", "browser-fixture")),
            _notify_browser_captcha_request_finished=AsyncMock(),
            _notify_browser_captcha_error=AsyncMock(),
        )

    def generation(self):
        return FlowProfileGeneration(self.client, self.tokens, self.token)

    def response(self):
        generated = [None] * 13 + ["https://images.example.invalid/a.png"]
        media = ["media/actual", None, "workflows/actual", None, None, None, [generated]]
        return [[media], [["workflows/actual"]]]

    async def test_uses_http_fixed_proxy_and_submits_once_without_borrowing_browser(self):
        entered = 0

        @asynccontextmanager
        async def provider(token):
            nonlocal entered
            entered += 1
            yield self.context

        self.refresher.active_context_provider = provider
        calls = []

        async def rpc(context, rpcid, payload, **kwargs):
            calls.append((context, rpcid, payload, kwargs))
            if rpcid == "nzlxg":
                return [895, 1, 2, 2, None, 895]
            if rpcid == "HTrJv":
                return [[None] * 7]
            return self.response()

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 895}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch(
                "src.services.browser.flow_generation.resolve_image_usage", return_value="GEM_PIX_2"
            ),
        ):
            result, batch, trace = await self.generation().generate_image(
                "project/1", "prompt", "nano_banana_pro", "16:9", [], None
            )
        self.assertEqual(entered, 0)
        self.tokens.resolve_account_proxy.assert_awaited()
        self.assertEqual(result["media"][0]["name"], "media/actual")
        self.assertEqual(batch.count("-"), 4)
        self.assertEqual(trace["final_success_attempt"], 1)
        self.assertEqual(self.client._get_recaptcha_token.await_count, 1)
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with(
            "browser-fixture"
        )
        self.assertEqual([call[1] for call in calls], ["HTrJv", "nzlxg", "ogiZ0b"])

    async def test_cas_binding_change_blocks_before_captcha(self):
        changed = SimpleNamespace(
            **{**self.current.__dict__, "proxy_url": "http://other.invalid:1"}
        )
        self.db.get_token.return_value = changed
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(return_value={"email": self.token.email, "credits": 1}),
        ):
            with self.assertRaises(FlowRPCError) as caught:
                await self.generation().generate_image(
                    "project/1", "prompt", "GEM_PIX_2", "16:9", [], None
                )
        self.assertEqual(caught.exception.reason, "account_changed")
        self.client._get_recaptcha_token.assert_not_awaited()

    async def test_unknown_generation_outcome_is_not_retried(self):
        calls = []

        async def rpc(context, rpcid, payload, **kwargs):
            calls.append(rpcid)
            if rpcid == "HTrJv":
                return [[None] * 7]
            if rpcid == "nzlxg":
                return [895, 1, 2, 2]
            raise RuntimeError("unknown submission outcome")

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 1}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch(
                "src.services.browser.flow_generation.resolve_image_usage", return_value="GEM_PIX_2"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "unknown submission outcome"):
                await self.generation().generate_image(
                    "project/1", "prompt", "GEM_PIX_2", "16:9", [], None
                )
        self.assertEqual(calls, ["HTrJv", "nzlxg", "ogiZ0b"])
        self.assertEqual(self.client._get_recaptcha_token.await_count, 1)

    async def test_http_session_closes_after_identity_rejection_without_captcha(self):
        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(side_effect=FlowRPCError("identity_mismatch", "flow_identity")),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", AsyncMock()) as rpc,
        ):
            with self.assertRaises(FlowRPCError) as caught:
                await self.generation().generate_image(
                    "project/1", "prompt", "GEM_PIX_2", "16:9", [], None
                )
        self.assertEqual(caught.exception.reason, "identity_mismatch")
        self.owned_closed.assert_called_once()
        rpc.assert_not_awaited()
        self.client._get_recaptcha_token.assert_not_awaited()

    async def test_proxy_resolver_change_blocks_even_when_saved_binding_is_same(self):
        self.tokens.resolve_account_proxy.side_effect = [
            "http://proxy.invalid:8080",
            "http://different.invalid:8080",
        ]
        with self.assertRaises(FlowRPCError) as caught:
            await self.generation().generate_image(
                "project/1", "prompt", "GEM_PIX_2", "16:9", [], None
            )
        self.assertEqual(caught.exception.reason, "account_changed")
        self.client._get_recaptcha_token.assert_not_awaited()

    async def test_cas_recheck_after_captcha_blocks_submission_and_finishes_browser(self):
        changed = SimpleNamespace(**{**self.current.__dict__, "browser_profile_epoch": 2})
        self.db.get_token.side_effect = [self.current, self.current, changed]

        async def rpc(context, rpcid, payload, **kwargs):
            if rpcid == "HTrJv":
                return [[None] * 7]
            if rpcid == "nzlxg":
                return [895, 1, 2, 2]
            raise AssertionError("generation should not be posted")

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 1}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc) as call,
            patch(
                "src.services.browser.flow_generation.resolve_image_usage", return_value="GEM_PIX_2"
            ),
        ):
            with self.assertRaises(FlowRPCError) as caught:
                await self.generation().generate_image(
                    "project/1", "prompt", "GEM_PIX_2", "16:9", [], None
                )
        self.assertEqual(caught.exception.reason, "account_changed")
        self.assertEqual([entry.args[1] for entry in call.await_args_list], ["HTrJv", "nzlxg"])
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with(
            "browser-fixture"
        )
        self.owned_closed.assert_called_once()

    async def test_service_tier_comes_from_field4_not_paygate_field2(self):
        async def rpc(context, rpcid, payload, **kwargs):
            if rpcid == "HTrJv":
                return ["models-fixture"]
            if rpcid == "nzlxg":
                return [895, 1, 2, 2]
            return self.response()

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 1}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch(
                "src.services.browser.flow_generation.resolve_image_usage", return_value="GEM_PIX_2"
            ) as resolve,
        ):
            await self.generation().generate_image(
                "project/1", "prompt", "GEM_PIX_2", "16:9", [], None
            )
        resolve.assert_called_once_with(["models-fixture"], "GEM_PIX_2", "16:9", 2, False)

    async def test_captcha_failure_is_retryable_but_each_attempt_has_one_token(self):
        self.client._get_recaptcha_token.side_effect = [("", "browser-1"), ("captcha", "browser-2")]
        calls = []

        async def rpc(context, rpcid, payload, **kwargs):
            calls.append(rpcid)
            if rpcid == "HTrJv":
                return [[None] * 7]
            if rpcid == "nzlxg":
                return [895, 1, 2, 2]
            return self.response()

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 1}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch(
                "src.services.browser.flow_generation.resolve_image_usage", return_value="GEM_PIX_2"
            ),
            patch(
                "src.services.browser.flow_generation.config",
                SimpleNamespace(flow_max_retries=2, flow_image_request_timeout=20),
            ),
        ):
            result, _, trace = await self.generation().generate_image(
                "project/1", "prompt", "GEM_PIX_2", "16:9", [], None
            )
        self.assertEqual(trace["final_success_attempt"], 2)
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.assertEqual(calls, ["HTrJv", "nzlxg", "ogiZ0b"])
        self.client._notify_browser_captcha_request_finished.assert_any_await("browser-1")
        self.client._notify_browser_captcha_request_finished.assert_any_await("browser-2")

    async def test_upload_uses_same_verified_context_and_returns_official_media_name(self):
        self.client._detect_image_mime_type = Mock(return_value="image/png")
        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 1}),
            ),
            patch(
                "src.services.browser.flow_generation.flow_rpc",
                AsyncMock(return_value=[["media/uploaded"]]),
            ) as rpc,
        ):
            self.assertEqual(
                await self.generation().upload_image("project/1", b"synthetic-image"),
                "media/uploaded",
            )
        self.assertEqual(rpc.call_args.args[0], self.context)
        self.assertEqual(rpc.call_args.args[1], "maseQ")
        self.assertEqual(rpc.call_args.args[2][0], [None, 22, None, None, None, "project/1"])
        self.client._get_recaptcha_token.assert_not_awaited()
        self.client._notify_browser_captcha_request_finished.assert_not_awaited()
        self.owned_closed.assert_called_once()

    async def test_upsample_uses_fresh_captcha_and_returns_base64_bytes(self):
        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 1}),
            ),
            patch(
                "src.services.browser.flow_generation.flow_rpc",
                AsyncMock(return_value=[None, "aW1hZ2U="]),
            ) as rpc,
        ):
            self.assertEqual(
                await self.generation().upsample_image("project/1", "media/a", "2K"), "aW1hZ2U="
            )
        self.assertEqual(rpc.call_args.args[0], self.context)
        self.assertEqual(rpc.call_args.args[1], "SPrCad")
        self.assertEqual(self.client._get_recaptcha_token.await_count, 1)
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with(
            "browser-fixture"
        )
        self.owned_closed.assert_called_once()

    async def test_invalid_upload_never_acquires_captcha_or_calls_rpc(self):
        self.client._detect_image_mime_type = Mock(return_value="image/png")
        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 1}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", AsyncMock()) as rpc,
        ):
            with self.assertRaises(ValueError):
                await self.generation().upload_image("project/1", b"")
        rpc.assert_not_awaited()
        self.client._get_recaptcha_token.assert_not_awaited()
        self.client._notify_browser_captcha_request_finished.assert_not_awaited()

    async def test_video_retries_temporary_acquisition_then_submits_once(self):
        self.client._get_video_submit_timeout = Mock(return_value=90)
        self.client._get_recaptcha_token.side_effect = [
            RemoteCaptchaError("ERROR_NO_SLOT_AVAILABLE"),
            ("fresh", "lease"),
        ]

        async def rpc(context, rpcid, payload, **kwargs):
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 9}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc) as call,
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
            patch(
                "src.services.flow.angular_video.parse_video_response",
                return_value={"operations": []},
            ),
            patch(
                "src.services.browser.flow_generation.config", SimpleNamespace(flow_max_retries=2)
            ),
            patch("src.services.browser.flow_generation.asyncio.sleep", AsyncMock()) as sleep,
        ):
            await self.generation().generate_video("text", "project", "prompt", "veo", "16:9")
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.assertEqual(
            sum(c.kwargs["network_step"] == "flow_generate_video" for c in call.await_args_list), 1
        )
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with("lease")
        sleep.assert_awaited_once_with(1)

    async def test_video_retries_explicit_temporary_rejection_with_fresh_captcha(self):
        self.client._get_video_submit_timeout = Mock(return_value=90)
        self.client._get_recaptcha_token.side_effect = [("first", "lease1"), ("fresh", "lease2")]
        rejection = FlowRPCError(
            "transient_error",
            "flow_generate_video",
            rpc_code=7,
            public_error="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        )
        rejection.submission_rejected = True
        payloads = []

        async def rpc(context, rpcid, payload, **kwargs):
            if kwargs["network_step"] == "flow_generate_video":
                payloads.append(payload)
                if len(payloads) == 1:
                    raise rejection
                return []
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        async def sleep_after_release(delay):
            self.assertGreaterEqual(delay, 2)
            self.assertLessEqual(delay, 3)
            self.client._notify_browser_captcha_error.assert_awaited_once_with(
                "lease1", error_reason="recaptcha_rejected"
            )
            self.client._notify_browser_captcha_request_finished.assert_not_awaited()

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 9}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
            patch(
                "src.services.flow.angular_video.parse_video_response",
                return_value={"operations": []},
            ),
            patch(
                "src.services.browser.flow_generation.config", SimpleNamespace(flow_max_retries=2)
            ),
            patch(
                "src.services.browser.flow_generation.asyncio.sleep",
                side_effect=sleep_after_release,
            ),
        ):
            await self.generation().generate_video("text", "project", "prompt", "veo", "16:9")
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0][2], payloads[1][2])
        self.assertEqual(payloads[0][0], payloads[1][0])
        self.assertEqual(payloads[0][1][-1], ["first", 1])
        self.assertEqual(payloads[1][1][-1], ["fresh", 1])
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with("lease2")

    async def test_video_unknown_rpc_outcome_and_permanent_rejection_are_not_retried(self):
        self.client._get_video_submit_timeout = Mock(return_value=90)
        errors = [
            FlowRPCError("transient_error", "flow_generate_video"),
            FlowRPCError("rpc_rejected", "flow_generate_video", rpc_code=7),
            FlowRPCError(
                "transient_error",
                "flow_generate_video",
                rpc_code=7,
                public_error="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            ),
            FlowRPCError(
                "rpc_rejected",
                "flow_generate_video",
                rpc_code=7,
                public_error="PUBLIC_ERROR_MODEL_ACCESS_DENIED",
            ),
        ]
        errors[1].submission_rejected = True
        errors[3].submission_rejected = True
        for error in errors:
            with self.subTest(error=error):
                self.client._get_recaptcha_token.reset_mock()

                async def rpc(context, rpcid, payload, **kwargs):
                    if kwargs["network_step"] == "flow_generate_video":
                        raise error
                    return [9, 1, 2, 2] if rpcid == "nzlxg" else []

                with (
                    patch(
                        "src.services.browser.token_account_probe.read_flow_session",
                        AsyncMock(return_value={"email": self.token.email, "credits": 9}),
                    ),
                    patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
                    patch(
                        "src.services.flow.angular_video.resolve_video_usage", return_value="veo"
                    ),
                    patch(
                        "src.services.browser.flow_generation.config",
                        SimpleNamespace(flow_max_retries=3),
                    ),
                    patch(
                        "src.services.browser.flow_generation.asyncio.sleep", AsyncMock()
                    ) as sleep,
                ):
                    with self.assertRaises(FlowRPCError) as caught:
                        await self.generation().generate_video(
                            "text", "project", "prompt", "veo", "16:9"
                        )
                self.assertIs(caught.exception, error)
                self.client._get_recaptcha_token.assert_awaited_once()
                sleep.assert_not_awaited()

    async def test_video_captcha_and_submission_share_one_retry_budget(self):
        self.client._get_video_submit_timeout = Mock(return_value=90)
        self.client._get_recaptcha_token.side_effect = [
            RemoteCaptchaError("ERROR_NO_SLOT_AVAILABLE"),
            ("fresh", "lease"),
        ]
        rejection = FlowRPCError(
            "transient_error",
            "flow_generate_video",
            rpc_code=8,
            public_error="PUBLIC_ERROR_HIGH_TRAFFIC",
        )
        rejection.submission_rejected = True

        async def rpc(context, rpcid, payload, **kwargs):
            if kwargs["network_step"] == "flow_generate_video":
                raise rejection
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 9}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc) as call,
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
            patch(
                "src.services.browser.flow_generation.config", SimpleNamespace(flow_max_retries=2)
            ),
            patch("src.services.browser.flow_generation.asyncio.sleep", AsyncMock()) as sleep,
        ):
            with self.assertRaises(FlowRPCError) as caught:
                await self.generation().generate_video("text", "project", "prompt", "veo", "16:9")
        self.assertIs(caught.exception, rejection)
        self.assertEqual(self.client._get_recaptcha_token.await_count, 2)
        self.assertEqual(
            sum(c.kwargs["network_step"] == "flow_generate_video" for c in call.await_args_list), 1
        )
        sleep.assert_awaited_once_with(1)

    async def test_video_auth_failure_stops_before_submission_and_unknown_write_is_not_replayed(
        self,
    ):
        self.client._get_video_submit_timeout = Mock(return_value=90)
        self.client._get_recaptcha_token.side_effect = RemoteCaptchaError("ERROR_IP_NOT_ALLOWED")

        async def rpc(context, rpcid, payload, **kwargs):
            if kwargs["network_step"] == "flow_generate_video":
                raise RuntimeError("unknown submission outcome")
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 9}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc) as call,
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
            patch(
                "src.services.browser.flow_generation.config", SimpleNamespace(flow_max_retries=3)
            ),
            patch("src.services.browser.flow_generation.asyncio.sleep", AsyncMock()) as sleep,
        ):
            with self.assertRaises(RemoteCaptchaError):
                await self.generation().generate_video("text", "project", "prompt", "veo", "16:9")
            self.client._get_recaptcha_token.assert_awaited_once()
            self.assertFalse(
                any(c.kwargs["network_step"] == "flow_generate_video" for c in call.await_args_list)
            )
            sleep.assert_not_awaited()
            self.client._get_recaptcha_token.reset_mock()
            self.client._get_recaptcha_token.side_effect = None
            self.client._get_recaptcha_token.return_value = ("fresh", "lease")
            with self.assertRaisesRegex(RuntimeError, "unknown submission outcome"):
                await self.generation().generate_video("text", "project", "prompt", "veo", "16:9")
            self.client._get_recaptcha_token.assert_awaited_once()
            self.assertEqual(
                sum(
                    c.kwargs["network_step"] == "flow_generate_video" for c in call.await_args_list
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
