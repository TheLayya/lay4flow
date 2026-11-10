"""One total attempt budget spans account handoffs, captcha and preflight."""

import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_generation_account_policy as fixtures
import test_generation_options as generation_fixtures

from src.services.browser.flow_generation import FlowProfileGeneration
from src.services.flow.flow_rpc import FlowRPCError
from src.services.flow_client import FlowClient
from src.services.generation_attempts import (
    CURRENT_ATTEMPTS,
    AccountGenerationRetry,
    GenerationBudgetExceeded,
)


class UnifiedGenerationBudgetTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.GenerationAccountPolicyTests.setUp
    pipeline = fixtures.GenerationAccountPolicyTests.pipeline
    prepare = fixtures.GenerationAccountPolicyTests.prepare

    def rejected(self):
        error = FlowRPCError(
            "transient_error",
            "flow_generate_video",
            rpc_code=7,
            public_error="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        )
        error.submission_rejected = True
        return error

    async def many_accounts(self, count):
        await self.prepare()
        accounts = [
            SimpleNamespace(
                **{
                    **vars(self.token),
                    "id": index + 1,
                    "email": f"synthetic-{index + 1}@example.invalid",
                }
            )
            for index in range(count)
        ]
        await self.cm.initialize(accounts)
        self.handler.load_balancer.select_token.side_effect = accounts
        self.handler.token_manager.get_token.side_effect = lambda identifier: accounts[
            identifier - 1
        ]
        self.policy.match.return_value = self.policy.record_failure.return_value = None
        return accounts

    async def test_twenty_accounts_each_fail_once_even_with_legacy_account_limit_three(self):
        accounts = await self.many_accounts(20)
        seen = []
        budgets = []

        async def pipeline(token, *args, **kwargs):
            budget = CURRENT_ATTEMPTS.get()
            budgets.append(budget)
            self.assertEqual(budget.excluded, set(seen))
            for identifier in seen:
                self.assertEqual(await self.cm.get_video_inflight(identifier), 0)
            seen.append(token.id)
            budget.take()
            raise self.rejected()
            yield

        self.handler._handle_video_generation = pipeline
        with patch("src.services.generation_handler.config._config", {"flow": {"max_retries": 20}}):
            values = [
                chunk async for chunk in self.handler.handle_generation("video-test", "prompt")
            ]
        self.assertEqual(seen, [account.id for account in accounts])
        self.assertEqual(len({id(budget) for budget in budgets}), 1)
        snapshot = budgets[-1].snapshot()
        self.assertEqual(
            (snapshot["total_attempts"], snapshot["retry_count"], snapshot["limit"]), (20, 19, 20)
        )
        self.assertEqual((snapshot["account_count"], snapshot["account_allocations"]), (20, 20))
        self.assertEqual(self.policy.record_failure.await_count, 20)
        self.assertIn("最大尝试次数（含首次）20 次", json.loads(values[-1])["error"]["message"])
        self.assertEqual(snapshot["attempts"][-1]["next_action"], "stop")
        self.assertIsNone(CURRENT_ATTEMPTS.get())

    async def test_twentieth_attempt_can_succeed_after_nineteen_account_rejections(self):
        await self.many_accounts(20)
        budget = None

        async def pipeline(token, *args, **kwargs):
            nonlocal budget
            budget = CURRENT_ATTEMPTS.get()
            budget.take()
            if token.id < 20:
                raise self.rejected()
            kwargs["generation_result"]["success"] = True
            yield "success on twenty"

        self.handler._handle_video_generation = pipeline
        with patch("src.services.generation_handler.config._config", {"flow": {"max_retries": 20}}):
            values = [
                chunk async for chunk in self.handler.handle_generation("video-test", "prompt")
            ]
        self.assertEqual(values, ["success on twenty"])
        self.assertEqual((budget.used, len(budget.history)), (20, 20))
        self.assertEqual(self.policy.record_failure.await_count, 19)
        self.handler.token_manager.record_success.assert_awaited_once_with(20)
        self.assertEqual(budget.snapshot()["attempts"][-1]["next_action"], "completed")

    async def test_transport_attempts_and_rejected_writes_share_one_budget_across_accounts(self):
        await self.many_accounts(2)
        budget = None
        seen = []

        async def pipeline(token, *args, **kwargs):
            nonlocal budget
            budget = CURRENT_ATTEMPTS.get()
            seen.append(token.id)
            if token.id == 1:
                for _ in range(17):
                    budget.take()
                    budget.finish("failed", "synthetic transport", next_action="retry_same_account")
                budget.take()
                raise self.rejected()
            budget.take()
            budget.finish("failed", "synthetic transport", next_action="retry_same_account")
            budget.take()
            kwargs["generation_result"]["success"] = True
            yield "success"

        self.handler._handle_video_generation = pipeline
        with patch("src.services.generation_handler.config._config", {"flow": {"max_retries": 20}}):
            values = [
                chunk async for chunk in self.handler.handle_generation("video-test", "prompt")
            ]
        self.assertEqual(values, ["success"])
        self.assertEqual(seen, [1, 2])
        self.assertEqual((budget.used, len(budget.history)), (20, 20))
        self.policy.record_failure.assert_awaited_once()
        with self.assertRaises(GenerationBudgetExceeded):
            budget.take()

    async def test_preflight_handoffs_without_take_consume_budget_exactly_once(self):
        await self.prepare()
        self.handler.load_balancer.select_token.side_effect = None
        self.handler.load_balancer.select_token.return_value = self.token
        self.handler.token_manager.ensure_valid_token.side_effect = [
            AccountGenerationRetry(
                RuntimeError("synthetic preflight"), self.token.id, {"action": "RETRY"}
            )
            for _ in range(20)
        ]
        with patch("src.services.generation_handler.config._config", {"flow": {"max_retries": 20}}):
            values = [
                chunk async for chunk in self.handler.handle_generation("video-test", "prompt")
            ]
        self.assertEqual(self.handler.load_balancer.select_token.await_count, 20)
        self.assertEqual(self.handler.token_manager.ensure_valid_token.await_count, 20)
        self.handler.flow_client.prefill_remote_browser_pool.assert_not_awaited()
        self.assertIn("20 次", json.loads(values[-1])["error"]["message"])
        self.assertEqual(await self.cm.get_video_inflight(7), 0)
        self.assertIsNone(CURRENT_ATTEMPTS.get())

    async def test_no_eligible_account_ends_with_real_reason_and_no_phantom_attempts(self):
        await self.prepare()
        self.handler.load_balancer.select_token.side_effect = [self.token, None]
        self.handler.load_balancer.get_unavailable_reason = AsyncMock(
            return_value="可用账号均在冷却"
        )
        budget = None

        async def pipeline(token, *args, **kwargs):
            nonlocal budget
            budget = CURRENT_ATTEMPTS.get()
            budget.take()
            raise self.rejected()
            yield

        self.handler._handle_video_generation = pipeline
        with patch("src.services.generation_handler.config._config", {"flow": {"max_retries": 20}}):
            values = [
                chunk async for chunk in self.handler.handle_generation("video-test", "prompt")
            ]
        self.assertIn("可用账号均在冷却", json.loads(values[-1])["error"]["message"])
        self.assertEqual((budget.used, len(budget.history)), (1, 1))
        self.assertEqual(self.handler.load_balancer.select_token.await_count, 2)
        self.assertEqual(budget.snapshot()["account_allocations"], 1)


class UnifiedBudgetRealPipelineTests(unittest.IsolatedAsyncioTestCase):
    setUp = generation_fixtures.GenerationOptionsTests.setUp

    async def run_twenty(self, success_on_last):
        accounts = [
            SimpleNamespace(
                **{
                    **vars(self.token),
                    "id": index + 1,
                    "email": f"synthetic-{index + 1}@example.invalid",
                    "browser_profile_source": "cloak",
                }
            )
            for index in range(20)
        ]
        self.handler.load_balancer.select_token.side_effect = accounts
        self.handler.token_manager.ensure_valid_token.side_effect = lambda token: token
        policy = SimpleNamespace(
            # Legacy settings remain available, but are not a scheduling cap.
            get_config=AsyncMock(return_value={"max_account_attempts": 3}),
            availability=AsyncMock(return_value=None),
            match=AsyncMock(return_value=None),
            record_failure=AsyncMock(return_value=None),
        )
        self.handler.token_manager.generation_error_policy = policy
        client = FlowClient(proxy_manager=None)
        client.token_manager = self.handler.token_manager
        client.prefill_remote_browser_pool = AsyncMock()
        client._get_recaptcha_token = AsyncMock(
            return_value=("synthetic-captcha", "synthetic-lease")
        )
        client._notify_browser_captcha_error = AsyncMock()
        client._notify_browser_captcha_request_finished = AsyncMock()
        self.handler.flow_client = client
        submitted = []
        budget = None

        @asynccontextmanager
        async def context(generation, **kwargs):
            yield object(), "http://synthetic-proxy.invalid:8080"

        async def call(generation, context, rpcid, payload, **kwargs):
            nonlocal budget
            step = kwargs["network_step"]
            if step == "flow_models":
                return []
            if step == "flow_credits":
                return [9, None, None, 2]
            self.assertEqual(step, "flow_generate_video")
            budget = CURRENT_ATTEMPTS.get()
            submitted.append(generation.token.id)
            if success_on_last and len(submitted) == 20:
                return []
            error = FlowRPCError(
                "transient_error",
                step,
                rpc_code=7,
                public_error="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            )
            error.submission_rejected = True
            raise error

        with (
            patch.object(FlowProfileGeneration, "_context", context),
            patch.object(FlowProfileGeneration, "_verified", AsyncMock()),
            patch.object(FlowProfileGeneration, "_check_binding", AsyncMock()),
            patch.object(FlowProfileGeneration, "_call", call),
            patch("src.services.browser.flow_generation.prepare_flow_rpc", AsyncMock()),
            patch(
                "src.services.flow.angular_video.resolve_video_usage", return_value="abra_t2v_8s"
            ),
            patch(
                "src.services.flow.angular_video.parse_video_response",
                return_value={
                    "operations": [{"operation": {"name": "synthetic-task"}}],
                },
            ),
            patch("src.services.flow_client.config._config", {"flow": {"max_retries": 20}}),
        ):
            chunks = [
                chunk
                async for chunk in self.handler.handle_generation("omni_8s", "Synthetic audit")
            ]
        self.assertEqual(submitted, list(range(1, 21)))
        self.assertEqual(client._get_recaptcha_token.await_count, 20)
        self.assertEqual(self.handler.load_balancer.select_token.await_count, 20)
        self.assertEqual((budget.used, len(budget.history)), (20, 20))
        self.assertEqual(policy.record_failure.await_count, 19 if success_on_last else 20)
        self.assertIsNone(CURRENT_ATTEMPTS.get())
        if success_on_last:
            self.assertEqual(chunks, ['{"result":"mock-video"}'])
            self.handler.token_manager.record_success.assert_awaited_once_with(20)
        else:
            self.assertIn("20 次", json.loads(chunks[-1])["error"]["message"])
        return budget.snapshot()

    async def test_actual_handler_client_profile_keep_twenty_as_only_budget(self):
        history = await self.run_twenty(success_on_last=False)
        self.assertEqual((history["total_attempts"], history["retry_count"]), (20, 19))
        self.assertEqual(history["attempts"][-1]["next_action"], "stop")

    async def test_actual_pipeline_can_return_twentieth_success_after_nineteen_rejections(self):
        history = await self.run_twenty(success_on_last=True)
        self.assertEqual(history["attempts"][-1]["next_action"], "completed")


if __name__ == "__main__":
    unittest.main()
