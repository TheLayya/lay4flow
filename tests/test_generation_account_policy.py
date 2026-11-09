"""Generation failover is bounded, releases leases and never replays unknown writes."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_generation_concurrency as fixtures

from src.services.flow.flow_rpc import FlowRPCError
from src.services.generation_attempts import CURRENT_ATTEMPTS, GenerationAttempts
from src.services.load_balancer import LoadBalancer


class GenerationAccountPolicyTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.GenerationConcurrencyTests.setUp
    pipeline = fixtures.GenerationConcurrencyTests.pipeline

    async def prepare(self):
        self.second = SimpleNamespace(
            **{**vars(self.token), "id": 8, "email": "second@example.invalid"}
        )
        await self.cm.initialize([self.token, self.second])
        self.policy = SimpleNamespace(
            get_config=AsyncMock(return_value={"max_account_attempts": 3}),
            availability=AsyncMock(return_value=None),
            record_failure=AsyncMock(return_value={"action": "LIMIT", "seconds": 86400}),
            match=AsyncMock(return_value={"action": "LIMIT", "seconds": 86400}),
        )
        self.handler.token_manager.generation_error_policy = self.policy
        self.handler.load_balancer.select_token.side_effect = [self.token, self.second]
        self.handler.token_manager.get_token.side_effect = lambda identifier: (
            self.token if identifier == 7 else self.second
        )
        self.handler.token_manager.ensure_valid_token.side_effect = lambda token: token

    def rejection(self, accepted=False):
        error = FlowRPCError(
            "rpc_rejected",
            "flow_generate_video",
            rpc_code=8,
            public_error="PUBLIC_ERROR_USER_QUOTA_REACHED",
        )
        error.submission_rejected = not accepted
        return error

    async def test_confirmed_quota_switches_once_and_releases_old_slot(self):
        await self.prepare()
        seen = []

        async def pipeline(token, *args, **kwargs):
            seen.append(token.id)
            CURRENT_ATTEMPTS.get().take()
            if token.id == 7:
                raise self.rejection()
            self.assertEqual(await self.cm.get_video_inflight(7), 0)
            self.assertIn(7, CURRENT_ATTEMPTS.get().excluded)
            kwargs["generation_result"]["success"] = True
            yield "success"

        self.handler._handle_video_generation = pipeline
        values = [chunk async for chunk in self.handler.handle_generation("video-test", "prompt")]
        self.assertEqual(values, ["success"])
        self.assertEqual(seen, [7, 8])
        self.policy.record_failure.assert_awaited_once()
        self.handler.token_manager.record_error.assert_not_awaited()
        self.handler.token_manager.record_success.assert_awaited_once_with(8)
        self.assertEqual(await self.cm.get_video_inflight(8), 0)
        self.assertIsNone(CURRENT_ATTEMPTS.get())

    async def test_unknown_outcome_or_post_acceptance_never_replays(self):
        for accepted in (False, True):
            with self.subTest(post_accepted=accepted):
                await self.prepare()
                self.handler.load_balancer.select_token.reset_mock()

                async def pipeline(token, *args, **kwargs):
                    CURRENT_ATTEMPTS.get().take()
                    kwargs["generation_result"]["upstream_accepted"] = accepted
                    if accepted:
                        raise self.rejection()
                    raise FlowRPCError("transient_error", "flow_generate_video")
                    yield

                self.handler._handle_video_generation = pipeline
                values = [
                    chunk async for chunk in self.handler.handle_generation("video-test", "prompt")
                ]
                self.assertEqual(self.handler.load_balancer.select_token.await_count, 1)
                self.assertIn("error", json.loads(values[-1]))
                self.assertEqual(await self.cm.get_video_inflight(7), 0)

    async def test_one_total_attempt_means_no_account_fanout(self):
        await self.prepare()

        async def pipeline(token, *args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            raise self.rejection()
            yield

        self.handler._handle_video_generation = pipeline
        with patch("src.services.generation_handler.config._config", {"flow": {"max_retries": 1}}):
            values = [
                chunk async for chunk in self.handler.handle_generation("video-test", "prompt")
            ]
        self.assertEqual(self.handler.load_balancer.select_token.await_count, 1)
        self.assertIn("error", json.loads(values[-1]))
        self.assertEqual(await self.cm.get_video_inflight(7), 0)

    async def test_selector_skips_cooldown_and_request_exclusions_before_auth(self):
        await self.prepare()
        for token in (self.token, self.second):
            token.image_enabled = token.video_enabled = True
            token.credits = 100
        manager = SimpleNamespace(
            get_active_tokens=AsyncMock(return_value=[self.token, self.second]),
            ensure_valid_token=AsyncMock(side_effect=lambda token: token),
            needs_at_refresh=lambda token: False,
            generation_error_policy=SimpleNamespace(
                public_states=AsyncMock(return_value={7: {"reason": "quota exhausted"}}),
                availability=AsyncMock(return_value=None),
            ),
        )
        balancer = LoadBalancer(manager)
        selected = await balancer.select_token(for_video_generation=True)
        self.assertEqual(selected.id, 8)
        manager.ensure_valid_token.assert_awaited_once_with(self.second)
        manager.generation_error_policy.public_states.return_value = {}
        manager.ensure_valid_token.reset_mock()
        attempts = GenerationAttempts(3, excluded={8})
        marker = CURRENT_ATTEMPTS.set(attempts)
        try:
            selected = await balancer.select_token(for_video_generation=True)
        finally:
            CURRENT_ATTEMPTS.reset(marker)
        self.assertEqual(selected.id, 7)
        manager.ensure_valid_token.assert_awaited_once_with(self.token)

    async def test_lower_load_beats_ready_account_with_large_wait_queue(self):
        await self.prepare()
        for token in (self.token, self.second):
            token.image_enabled = token.video_enabled = True
            token.credits = 100
        manager = SimpleNamespace(
            get_active_tokens=AsyncMock(return_value=[self.token, self.second]),
            ensure_valid_token=AsyncMock(side_effect=lambda token: token),
            needs_at_refresh=lambda token: token.id == 8,
        )
        balancer = LoadBalancer(manager)
        await balancer._add_pending(7, False, True)
        await balancer._add_pending(7, False, True)
        selected = await balancer.select_token(for_video_generation=True)
        self.assertEqual(selected.id, 8)

    async def test_client_close_with_policy_releases_slot_and_context(self):
        await self.prepare()
        generator = self.handler.handle_generation("video-test", "prompt")
        self.assertEqual(await anext(generator), "result")
        self.assertEqual(await self.cm.get_video_inflight(7), 1)
        self.assertIsNone(CURRENT_ATTEMPTS.get())
        await generator.aclose()
        self.assertEqual(await self.cm.get_video_inflight(7), 0)
        self.assertIsNone(CURRENT_ATTEMPTS.get())

    async def test_retry_rule_can_use_single_account_without_disabling_it(self):
        await self.prepare()
        self.policy.record_failure.return_value = {"action": "RETRY", "seconds": 0}
        self.handler.load_balancer.select_token.side_effect = [self.token, self.token]
        calls = []

        async def pipeline(token, *args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            calls.append(token.id)
            if len(calls) == 1:
                raise self.rejection()
            self.assertNotIn(7, CURRENT_ATTEMPTS.get().excluded)
            kwargs["generation_result"]["success"] = True
            yield "ok"

        self.handler._handle_video_generation = pipeline
        values = [chunk async for chunk in self.handler.handle_generation("video-test", "prompt")]
        self.assertEqual(values, ["ok"])
        self.assertEqual(calls, [7, 7])
        self.assertEqual(await self.cm.get_video_inflight(7), 0)
        self.assertIsNone(CURRENT_ATTEMPTS.get())

    async def test_account_cooldown_after_acceptance_never_replays(self):
        await self.prepare()

        async def pipeline(token, *args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            kwargs["generation_result"]["upstream_accepted"] = True
            raise FlowRPCError("account_cooldown", "flow_generation")
            yield

        self.handler._handle_video_generation = pipeline
        values = [chunk async for chunk in self.handler.handle_generation("video-test", "prompt")]
        self.assertEqual(self.handler.load_balancer.select_token.await_count, 1)
        self.assertIn("error", json.loads(values[-1]))

    async def test_log_failure_after_recording_generation_failure_does_not_count_again(self):
        await self.prepare()

        async def pipeline(token, *args, **kwargs):
            kwargs["generation_result"]["success"] = False
            kwargs["generation_result"]["error_message"] = "synthetic-terminal-failure"
            kwargs["generation_result"]["error_emitted"] = True
            yield "terminal-error"

        self.handler._handle_video_generation = pipeline
        self.handler._log_request.side_effect = [None, RuntimeError("synthetic-log-failure"), None]
        values = [chunk async for chunk in self.handler.handle_generation("video-test", "prompt")]
        self.assertEqual(values[0], "terminal-error")
        self.policy.record_failure.assert_awaited_once_with(
            7, "synthetic-terminal-failure", model="video-test"
        )
        self.handler.token_manager.record_error.assert_not_awaited()
