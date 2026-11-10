"""Real submission loops emit request attempt evidence with synthetic services."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_flow_profile_generation as fixtures

from src.services.flow.flow_rpc import (
    FlowRPCError,
    flow_rpc as real_flow_rpc,
    prepare_flow_rpc as real_prepare,
)
from src.services.flow.remote_captcha import RemoteCaptchaError
from src.services.generation_attempts import CURRENT_ATTEMPTS, GenerationAttempts


class FlowAttemptHistoryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.FlowProfileGenerationTests.setUp
    generation = fixtures.FlowProfileGenerationTests.generation
    response = fixtures.FlowProfileGenerationTests.response

    async def test_full_rpc_project_get_precedes_solve_and_first_post_uses_that_bootstrap(self):
        import test_flow_rpc as http_fixture

        budget = GenerationAttempts(1)
        budget.select_account(self.token)
        clock, events = [100.0], []
        solved = [False]
        self.client._get_video_submit_timeout = Mock(return_value=90)

        async def get(url, **kwargs):
            events.append("get_project" if "/project/" in url else "get_root")
            self.assertFalse(solved[0], "project bootstrap should normally precede solve")
            clock[0] += 2
            return http_fixture.bootstrap({**http_fixture.CONFIG, "oPEP7c": self.token.email})

        async def post(url, **kwargs):
            rpcid = kwargs["params"]["rpcids"]
            events.append(rpcid)
            clock[0] += 1
            if rpcid == "YhhmEf":
                self.assertTrue(solved[0])
                detail = [
                    "type.googleapis.com/google.internal.labs.aisandbox.proto.common.v1.PublicAitkError",
                    [50],
                ]
                import json

                return http_fixture.response(
                    json.dumps([["wrb.fr", rpcid, None, None, None, [8, None, [detail]]]]),
                    url=http_fixture.RPC_URL,
                )
            return http_fixture.rpc(rpcid, [9, 1, 2, 2] if rpcid == "nzlxg" else [])

        async def solve(*args, **kwargs):
            events.append("solve")
            clock[0] += 3
            solved[0] = True
            return "private-captcha", "private-lease"

        async def recheck(*args, **kwargs):
            if solved[0]:
                clock[0] += 4

        self.context = SimpleNamespace(
            request=SimpleNamespace(
                get=AsyncMock(side_effect=get), post=AsyncMock(side_effect=post)
            ),
            flow_bootstrap_cache={},
        )
        generation = self.generation()
        generation._verified = AsyncMock()
        generation._check_binding = AsyncMock(side_effect=recheck)
        self.client._get_recaptcha_token.side_effect = solve
        marker = CURRENT_ATTEMPTS.set(budget)
        try:
            with (
                patch("src.services.browser.flow_generation.prepare_flow_rpc", real_prepare),
                patch("src.services.browser.flow_generation.flow_rpc", real_flow_rpc),
                patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
                patch(
                    "src.services.browser.flow_generation.config",
                    SimpleNamespace(flow_max_retries=1),
                ),
                patch(
                    "src.services.browser.flow_generation.time.monotonic",
                    side_effect=lambda: clock[0],
                ),
            ):
                with self.assertRaises(FlowRPCError) as caught:
                    await generation.generate_video("text", "p", "prompt", "veo", "16:9")
            self.assertEqual(
                events, ["get_root", "HTrJv", "nzlxg", "get_project", "solve", "YhhmEf"]
            )
            row = budget.snapshot()["attempts"][0]
            self.assertEqual(
                (row["captcha_ms"], row["captcha_to_submit_ms"], row["submit_ms"]),
                (3000, 4000, 1000),
            )
            self.assertEqual(
                (row["http_status"], row["rpc_code"], row["submission_count"]), (200, 8, 1)
            )
            self.assertEqual(
                caught.exception.public_error, "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC"
            )
            self.client._notify_browser_captcha_error.assert_awaited_once_with(
                "private-lease", error_reason="upstream_rejected"
            )
        finally:
            CURRENT_ATTEMPTS.reset(marker)

    async def test_failed_project_prepare_never_solves_or_submits(self):
        self.client._get_video_submit_timeout = Mock(return_value=90)
        self.prepare.side_effect = FlowRPCError("identity_mismatch", "flow_bootstrap")

        async def rpc(context, rpcid, payload, **kwargs):
            self.assertNotEqual(rpcid, "YhhmEf")
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 9}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc) as call,
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
        ):
            with self.assertRaises(FlowRPCError):
                await self.generation().generate_video("text", "p", "prompt", "veo", "16:9")
        self.client._get_recaptcha_token.assert_not_awaited()
        self.client._notify_browser_captcha_error.assert_not_awaited()
        self.client._notify_browser_captcha_request_finished.assert_not_awaited()
        self.assertEqual(call.await_count, 2)

    async def test_actual_post_timing_preserves_typed_rejection_and_neutral_lease_outcome(self):
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                self.client._notify_browser_captcha_error.reset_mock()
                self.client._notify_browser_captcha_request_finished.reset_mock()
                self.client._get_video_submit_timeout = Mock(return_value=90)
                budget = GenerationAttempts(1)
                budget.select_account(self.token)
                clock = [100.0]
                events = []
                rejection = FlowRPCError(
                    "transient_error",
                    "flow_generate_" + kind,
                    rpc_code=8,
                    public_error="PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC",
                )
                rejection.submission_rejected = True
                self.tokens.generation_error_policy = SimpleNamespace(
                    availability=AsyncMock(return_value=None)
                )

                async def prepare(*args, **kwargs):
                    events.append("project_prepare")
                    clock[0] = 102.0

                async def solve(*args, **kwargs):
                    events.append("solve")
                    clock[0] = 105.0
                    return "must-not-appear-in-history", "lease-does-not-appear-either"

                async def check(proxy, **kwargs):
                    clock[0] = 109.0

                async def rpc(context, rpcid, payload, **kwargs):
                    if kwargs["network_step"] == "flow_generate_" + kind:
                        observer = kwargs["submit_observer"]
                        # Includes post-solve binding/bootstrap delay at the actual
                        # POST boundary, not the earlier start of _call.
                        observer("started", 111.0)
                        observer("finished", 113.0, 200)
                        clock[0] = 113.0
                        events.append("post")
                        raise rejection
                    return [9, 1, 2, 2] if rpcid == "nzlxg" else []

                self.client._get_recaptcha_token.side_effect = solve
                generation = self.generation()
                generation._check_binding = AsyncMock(side_effect=check)
                marker = CURRENT_ATTEMPTS.set(budget)
                try:
                    with (
                        patch(
                            "src.services.browser.token_account_probe.read_flow_session",
                            AsyncMock(return_value={"email": self.token.email, "credits": 9}),
                        ),
                        patch(
                            "src.services.browser.flow_generation.prepare_flow_rpc",
                            side_effect=prepare,
                        ),
                        patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
                        patch(
                            "src.services.browser.flow_generation.time.monotonic",
                            side_effect=lambda: clock[0],
                        ),
                        patch(
                            "src.services.browser.flow_generation.resolve_image_usage",
                            return_value="GEM_PIX_2",
                        ),
                        patch(
                            "src.services.flow.angular_video.resolve_video_usage",
                            return_value="veo",
                        ),
                        patch(
                            "src.services.browser.flow_generation.config",
                            SimpleNamespace(flow_max_retries=1, flow_image_request_timeout=60),
                        ),
                    ):
                        with self.assertRaises(FlowRPCError) as caught:
                            if kind == "image":
                                await generation.generate_image(
                                    "project", "prompt", "gemini", "16:9", [], None
                                )
                            else:
                                await generation.generate_video(
                                    "text", "project", "prompt", "veo", "16:9"
                                )
                        self.assertIs(caught.exception, rejection)
                    row = budget.snapshot()["attempts"][0]
                    self.assertEqual(events, ["project_prepare", "solve", "post"])
                    self.assertEqual(
                        {
                            key: row[key]
                            for key in (
                                "captcha_ms",
                                "captcha_to_submit_ms",
                                "submit_ms",
                                "submission_count",
                                "submission_state",
                            )
                        },
                        {
                            "captcha_ms": 3000,
                            "captcha_to_submit_ms": 6000,
                            "submit_ms": 2000,
                            "submission_count": 1,
                            "submission_state": "rejected",
                        },
                    )
                    self.assertEqual(row["error_code"], rejection.public_error)
                    self.assertEqual((row["http_status"], row["rpc_code"]), (200, 8))
                    self.assertEqual(
                        caught.exception.flow_retry_trace["generation_attempts"][0]["public_error"],
                        rejection.public_error,
                    )
                    serialized = str(budget.snapshot())
                    self.assertNotIn("must-not-appear", serialized)
                    self.assertNotIn("lease-does-not-appear", serialized)
                    self.client._notify_browser_captcha_error.assert_awaited_once_with(
                        "lease-does-not-appear-either", error_reason="upstream_rejected"
                    )
                    self.client._notify_browser_captcha_request_finished.assert_not_awaited()
                finally:
                    CURRENT_ATTEMPTS.reset(marker)

    async def test_unknown_submission_has_timing_but_never_success_callback_or_retry(self):
        budget = GenerationAttempts(3)
        budget.select_account(self.token)
        self.client._get_video_submit_timeout = Mock(return_value=90)
        calls = []

        async def rpc(context, rpcid, payload, **kwargs):
            if kwargs["network_step"] == "flow_generate_video":
                calls.append(rpcid)
                kwargs["submit_observer"]("started", 200.0)
                kwargs["submit_observer"]("finished", 201.0)
                raise FlowRPCError("transient_error", "flow_generate_video")
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        marker = CURRENT_ATTEMPTS.set(budget)
        try:
            with (
                patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    AsyncMock(return_value={"email": self.token.email, "credits": 9}),
                ),
                patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
                patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
                patch(
                    "src.services.browser.flow_generation.config",
                    SimpleNamespace(flow_max_retries=3),
                ),
            ):
                with self.assertRaises(FlowRPCError):
                    await self.generation().generate_video(
                        "text", "project", "prompt", "veo", "16:9"
                    )
            self.assertEqual(calls, ["YhhmEf"])
            self.assertEqual(budget.snapshot()["attempts"][0]["submission_state"], "unknown")
            self.assertEqual(budget.snapshot()["attempts"][0]["submit_ms"], 1000)
            self.client._notify_browser_captcha_error.assert_awaited_once_with(
                "browser-fixture", error_reason="submission_unknown"
            )
            self.client._notify_browser_captcha_request_finished.assert_not_awaited()
        finally:
            CURRENT_ATTEMPTS.reset(marker)

    async def test_cancelled_submission_releases_once_as_unknown_and_never_retries(self):
        import asyncio

        self.client._get_video_submit_timeout = Mock(return_value=90)
        calls = []

        async def rpc(context, rpcid, payload, **kwargs):
            if kwargs["network_step"] == "flow_generate_video":
                calls.append(rpcid)
                kwargs["submit_observer"]("started", 200.0)
                kwargs["submit_observer"]("finished", 201.0)
                raise asyncio.CancelledError()
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        with (
            patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(return_value={"email": self.token.email, "credits": 9}),
            ),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.generation().generate_video("text", "p", "prompt", "veo", "16:9")
        self.assertEqual(calls, ["YhhmEf"])
        self.client._notify_browser_captcha_error.assert_awaited_once_with(
            "browser-fixture", error_reason="submission_unknown"
        )
        self.client._notify_browser_captcha_request_finished.assert_not_awaited()

    async def run_generation(self, kind):
        budget = GenerationAttempts(3)
        budget.select_account(self.token)
        self.client._get_video_submit_timeout = Mock(return_value=90)
        self.client._get_recaptcha_token.side_effect = [
            RemoteCaptchaError("ERROR_NO_SLOT_AVAILABLE"),
            ("synthetic-captcha", "synthetic-lease"),
        ]

        async def rpc(context, rpcid, payload, **kwargs):
            if kwargs["network_step"] == "flow_generate_image":
                return self.response()
            return [9, 1, 2, 2] if rpcid == "nzlxg" else []

        marker = CURRENT_ATTEMPTS.set(budget)
        try:
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
                    "src.services.browser.flow_generation.resolve_image_usage",
                    return_value="GEM_PIX_2",
                ),
                patch(
                    "src.services.browser.flow_generation.config",
                    SimpleNamespace(flow_max_retries=3, flow_image_request_timeout=60),
                ),
                patch("src.services.browser.flow_generation.asyncio.sleep", AsyncMock()),
            ):
                if kind == "video":
                    await self.generation().generate_video(
                        "text", "synthetic-project", "prompt", "veo", "16:9"
                    )
                else:
                    await self.generation().generate_image(
                        "synthetic-project", "prompt", "gemini", "16:9", [], None
                    )
        finally:
            CURRENT_ATTEMPTS.reset(marker)
        return budget.snapshot()

    async def test_video_records_captcha_retry_and_accepted_submission(self):
        history = await self.run_generation("video")
        self.assertEqual(
            (history["total_attempts"], history["retry_count"], history["account_count"]),
            (2, 1, 1),
        )
        first, second = history["attempts"]
        self.assertEqual(first["stage"], "captcha")
        self.assertEqual(first["status"], "failed")
        self.assertIn("ERROR_NO_SLOT_AVAILABLE", first["error"])
        self.assertEqual(first["next_action"], "retry_same_account")
        self.assertEqual(first["delay_seconds"], 1)
        self.assertEqual(second["stage"], "polling")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(second["next_action"], "polling")
        self.assertEqual(second["account_id"], 7)

    async def test_image_records_each_acquisition_attempt_then_success(self):
        history = await self.run_generation("image")
        self.assertEqual(history["total_attempts"], 2)
        self.assertEqual(history["retry_count"], 1)
        self.assertEqual([row["status"] for row in history["attempts"]], ["failed", "success"])
        self.assertEqual(history["attempts"][0]["stage"], "captcha")
        self.assertEqual(history["attempts"][1]["stage"], "submit")
        self.assertEqual(history["attempts"][1]["next_action"], "completed")
        self.assertTrue(all(row["duration_ms"] >= 0 for row in history["attempts"]))

    async def test_confirmed_rejection_hands_off_to_policy_after_transport_failures(self):
        for public_error in (
            "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC",
        ):
            with self.subTest(public_error=public_error):
                budget = GenerationAttempts(20)
                budget.select_account(self.token)
                self.tokens.generation_error_policy = SimpleNamespace(
                    availability=AsyncMock(return_value=None)
                )
                self.client._get_recaptcha_token.reset_mock()
                self.client._notify_browser_captcha_error.reset_mock()
                self.client._get_video_submit_timeout = Mock(return_value=90)
                self.client._get_recaptcha_token.side_effect = [
                    *[RemoteCaptchaError("TRANSPORT_ERROR") for _ in range(10)],
                    ("synthetic-captcha", "synthetic-lease"),
                    ("must-not-be-requested", "unused-lease"),
                ]
                rejection = FlowRPCError(
                    "transient_error",
                    "flow_generate_video",
                    rpc_code=7,
                    public_error=public_error,
                )
                rejection.submission_rejected = True

                async def rpc(context, rpcid, payload, **kwargs):
                    if kwargs["network_step"] == "flow_generate_video":
                        raise rejection
                    return [9, 1, 2, 2] if rpcid == "nzlxg" else []

                marker = CURRENT_ATTEMPTS.set(budget)
                try:
                    with (
                        patch(
                            "src.services.browser.token_account_probe.read_flow_session",
                            AsyncMock(return_value={"email": self.token.email, "credits": 9}),
                        ),
                        patch(
                            "src.services.browser.flow_generation.flow_rpc", side_effect=rpc
                        ) as calls,
                        patch(
                            "src.services.flow.angular_video.resolve_video_usage",
                            return_value="veo",
                        ),
                        patch(
                            "src.services.browser.flow_generation.config",
                            SimpleNamespace(flow_max_retries=20),
                        ),
                        patch(
                            "src.services.browser.flow_generation.asyncio.sleep", AsyncMock()
                        ) as sleep,
                    ):
                        with self.assertRaises(FlowRPCError) as caught:
                            await self.generation().generate_video(
                                "text", "project", "prompt", "veo", "16:9"
                            )
                    self.assertIs(caught.exception, rejection)
                finally:
                    CURRENT_ATTEMPTS.reset(marker)
                self.assertEqual(self.client._get_recaptcha_token.await_count, 11)
                self.assertEqual(
                    sum(
                        c.kwargs["network_step"] == "flow_generate_video"
                        for c in calls.await_args_list
                    ),
                    1,
                )
                self.assertEqual(sleep.await_count, 10)
                self.client._notify_browser_captcha_error.assert_awaited_once_with(
                    "synthetic-lease", error_reason="upstream_rejected"
                )
                history = budget.snapshot()
                self.assertEqual(history["limit"], 20)
                self.assertEqual(history["total_attempts"], 11)
                self.assertEqual(history["attempts"][-1]["stage"], "submit")
                self.assertEqual(history["attempts"][-1]["error_code"], public_error)
                self.assertEqual(history["attempts"][-1]["next_action"], "stop")


if __name__ == "__main__":
    unittest.main()
