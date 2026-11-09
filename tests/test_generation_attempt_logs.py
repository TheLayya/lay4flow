"""One persisted request retains account and same-account attempt histories."""

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_generation_account_policy as fixtures

from src.core.database import Database
from src.services.flow.flow_rpc import FlowRPCError
from src.services.generation_attempts import CURRENT_ATTEMPTS, GenerationAttempts
from src.services.generation_handler import GenerationHandler


class GenerationAttemptLogTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.GenerationAccountPolicyTests.setUp
    pipeline = fixtures.GenerationAccountPolicyTests.pipeline
    prepare = fixtures.GenerationAccountPolicyTests.prepare
    rejection = fixtures.GenerationAccountPolicyTests.rejection

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(redirect_stdout(io.StringIO()))
        self.handler.db = Database(str(Path(temporary.name) / "test.db"))
        await self.handler.db.init_db()
        async with self.handler.db._connect(write=True) as connection:
            await connection.executemany(
                "INSERT INTO tokens (id, st, email) VALUES (?, ?, ?)",
                [
                    (7, "synthetic-session-one", "synthetic@example.invalid"),
                    (8, "synthetic-session-two", "second@example.invalid"),
                ],
            )
            await connection.commit()
        self.handler._log_request = GenerationHandler._log_request.__get__(self.handler)
        self.handler._update_request_log_progress = (
            GenerationHandler._update_request_log_progress.__get__(self.handler)
        )

    async def test_switch_then_success_is_one_log_with_both_outcomes(self):
        await self.prepare()

        async def pipeline(token, *args, **kwargs):
            budget = CURRENT_ATTEMPTS.get()
            budget.take()
            budget.stage("submit")
            if token.id == 7:
                raise self.rejection()
            kwargs["generation_result"]["success"] = True
            yield "success"

        self.handler._handle_video_generation = pipeline
        self.assertEqual(
            [v async for v in self.handler.handle_generation("video-test", "prompt")], ["success"]
        )
        logs = await self.handler.db.get_logs(include_payload=True)
        self.assertEqual(len(logs), 1)
        row = logs[0]
        self.assertEqual(
            (row["attempt_count"], row["retry_count"], row["account_count"]), (2, 1, 2)
        )
        self.assertEqual(row["status_code"], 200)
        history = json.loads(row["response_body"])["attempt_history"]
        self.assertEqual([v["account_id"] for v in history["attempts"]], [7, 8])
        self.assertEqual([v["status"] for v in history["attempts"]], ["failed", "success"])
        self.assertEqual(history["attempts"][0]["rule_action"], "LIMIT")
        self.assertEqual(history["attempts"][0]["next_action"], "switch_account")
        self.assertEqual(history["attempts"][1]["next_action"], "completed")

    async def test_exhausted_attempts_end_as_failed_without_duplicate_request_rows(self):
        await self.prepare()
        self.policy.get_config.return_value = {"max_account_attempts": 2}

        async def pipeline(token, *args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            raise self.rejection()
            yield

        self.handler._handle_video_generation = pipeline
        with patch("src.services.generation_handler.config._config", {"flow": {"max_retries": 2}}):
            values = [v async for v in self.handler.handle_generation("video-test", "prompt")]
        self.assertIn("error", json.loads(values[-1]))
        logs = await self.handler.db.get_logs(include_payload=True)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["status_text"], "failed")
        self.assertEqual(logs[0]["attempt_count"], 2)
        history = json.loads(logs[0]["response_body"])["attempt_history"]
        self.assertEqual(history["attempts"][-1]["next_action"], "stop")

    async def test_unavailable_next_account_keeps_original_failed_attempt(self):
        await self.prepare()
        self.handler.load_balancer.select_token.side_effect = [self.token, None]

        async def pipeline(token, *args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            raise self.rejection()
            yield

        self.handler._handle_video_generation = pipeline
        await anext(self.handler.handle_generation("video-test", "prompt"))
        logs = await self.handler.db.get_logs(include_payload=True)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["attempt_count"], 1)
        history = json.loads(logs[0]["response_body"])["attempt_history"]
        self.assertEqual(history["attempts"][0]["next_action"], "stop")

    async def final_log(self):
        logs = await self.handler.db.get_logs(include_payload=True)
        self.assertEqual(len(logs), 1)
        self.assertNotEqual(logs[0]["status_code"], 102)
        history = json.loads(logs[0]["response_body"])["attempt_history"]
        self.assertTrue(all(row["status"] != "in_progress" for row in history["attempts"]))
        return logs[0], history

    async def test_without_error_policy_still_records_single_account_attempt(self):
        await self.cm.initialize([self.token])
        values = [v async for v in self.handler.handle_generation("video-test", "prompt")]
        self.assertEqual(values, ["result"])
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 200)
        self.assertEqual(history["version"], 2)
        self.assertEqual(history["account_allocations"], 1)
        self.assertNotIn("accounts_limit", history)
        self.assertEqual(history["attempts"][0]["status"], "success")

    async def test_preflight_timeout_records_failed_attempt_and_releases_pending(self):
        await self.prepare()
        with patch.object(self.cm, "wait_acquire_video", return_value=(False, 1234)):
            await anext(self.handler.handle_generation("video-test", "prompt"))
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 503)
        self.assertEqual(history["total_attempts"], 1)
        self.assertEqual(history["attempts"][0]["stage"], "preflight")
        self.assertIn("并发槽位", history["attempts"][0]["error"])
        self.handler.load_balancer.release_pending.assert_awaited_once_with(
            7, for_image_generation=False, for_video_generation=True
        )

    async def test_invalid_refresh_is_terminal_with_original_account_identity(self):
        await self.prepare()
        self.handler.token_manager.ensure_valid_token.side_effect = None
        self.handler.token_manager.ensure_valid_token.return_value = None
        await anext(self.handler.handle_generation("video-test", "prompt"))
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 503)
        self.assertEqual(history["attempts"][0]["account_id"], 7)
        self.assertEqual(history["attempts"][0]["status"], "failed")
        self.assertEqual(history["attempts"][0]["next_action"], "stop")

    async def test_unsupported_tier_is_terminal_without_submitting(self):
        await self.prepare()
        self.handler._handle_video_generation = AsyncMock()
        with patch("src.services.generation_handler.supports_model_for_tier", return_value=False):
            await anext(self.handler.handle_generation("video-test", "prompt"))
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 403)
        self.assertEqual(history["attempts"][0]["stage"], "preflight")
        self.handler._handle_video_generation.assert_not_called()

    async def test_cancel_during_submission_finalizes_request_and_attempt(self):
        await self.prepare()
        entered = asyncio.Event()

        async def pipeline(*args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            CURRENT_ATTEMPTS.get().stage("submit")
            entered.set()
            await asyncio.Event().wait()
            yield

        self.handler._handle_video_generation = pipeline
        task = asyncio.create_task(anext(self.handler.handle_generation("video-test", "prompt")))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 499)
        self.assertEqual(history["attempts"][0]["status"], "cancelled")
        self.assertEqual(history["attempts"][0]["next_action"], "stop")
        self.assertEqual(await self.cm.get_video_inflight(7), 0)

    async def test_client_close_finalizes_unfinished_request(self):
        await self.prepare()

        async def pipeline(*args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            CURRENT_ATTEMPTS.get().stage("polling")
            yield "in-progress"
            await asyncio.Event().wait()

        self.handler._handle_video_generation = pipeline
        generator = self.handler.handle_generation("video-test", "prompt")
        self.assertEqual(await anext(generator), "in-progress")
        await generator.aclose()
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 499)
        self.assertEqual(history["attempts"][0]["status"], "cancelled")
        self.assertEqual(await self.cm.get_video_inflight(7), 0)

    async def test_client_close_after_stream_start_is_terminal_without_an_account(self):
        await self.prepare()
        generator = self.handler.handle_generation("video-test", "prompt", stream=True)
        self.assertIn("任务已启动", await anext(generator))
        await generator.aclose()
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 499)
        self.assertEqual(history["total_attempts"], 0)
        self.handler.load_balancer.select_token.assert_not_awaited()

    async def test_cancel_during_account_selection_is_terminal_without_an_account(self):
        await self.prepare()
        selecting = asyncio.Event()

        async def select(**kwargs):
            selecting.set()
            await asyncio.Event().wait()

        self.handler.load_balancer.select_token.side_effect = select
        task = asyncio.create_task(anext(self.handler.handle_generation("video-test", "prompt")))
        await asyncio.wait_for(selecting.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 499)
        self.assertEqual(history["total_attempts"], 0)

    async def test_cancel_during_account_delay_retains_failure_and_stops(self):
        await self.prepare()
        self.policy.record_failure.return_value = {"action": "DELAY", "seconds": 60}
        delaying = asyncio.Event()

        async def pipeline(*args, **kwargs):
            CURRENT_ATTEMPTS.get().take()
            raise self.rejection()
            yield

        async def sleep(seconds):
            delaying.set()
            await asyncio.Event().wait()

        self.handler._handle_video_generation = pipeline
        with patch("src.services.generation_handler.asyncio.sleep", side_effect=sleep):
            task = asyncio.create_task(
                anext(self.handler.handle_generation("video-test", "prompt"))
            )
            await asyncio.wait_for(delaying.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 499)
        self.assertEqual(history["attempts"][0]["status"], "failed")
        self.assertEqual(history["attempts"][0]["rule_action"], "DELAY")
        self.assertEqual(history["attempts"][0]["next_action"], "stop")
        self.assertEqual(self.handler.load_balancer.select_token.await_count, 1)
        self.assertEqual(await self.cm.get_video_inflight(7), 0)

    async def test_account_selection_exception_finalizes_existing_request(self):
        await self.prepare()
        self.handler.load_balancer.select_token.side_effect = RuntimeError("synthetic-selector")
        with self.assertRaisesRegex(RuntimeError, "synthetic-selector"):
            await anext(self.handler.handle_generation("video-test", "prompt"))
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 500)
        self.assertEqual(history["total_attempts"], 0)

    async def test_no_initial_account_has_terminal_log_with_zero_attempts(self):
        await self.prepare()
        self.handler.load_balancer.select_token.side_effect = None
        self.handler.load_balancer.select_token.return_value = None
        await anext(self.handler.handle_generation("video-test", "prompt"))
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 503)
        self.assertEqual(history["total_attempts"], 0)

    async def test_preflight_cooldown_is_retained_before_switching_account(self):
        await self.prepare()
        self.handler.token_manager.ensure_project_exists.side_effect = [
            FlowRPCError("account_cooldown", "flow_generation"),
            "synthetic-project",
        ]
        self.assertEqual(
            [v async for v in self.handler.handle_generation("video-test", "prompt")],
            ["result"],
        )
        log, history = await self.final_log()
        self.assertEqual(log["status_code"], 200)
        self.assertEqual(history["total_attempts"], 2)
        self.assertEqual(history["attempts"][0]["stage"], "preflight")
        self.assertEqual(history["attempts"][0]["status"], "failed")
        self.assertEqual(history["attempts"][0]["next_action"], "switch_account")
        self.assertEqual(history["attempts"][1]["account_id"], 8)


class AttemptLedgerTests(unittest.TestCase):
    def test_rule_duration_and_actual_retry_account_are_retained(self):
        budget = GenerationAttempts(3)
        first = SimpleNamespace(id=7, email="first@example.invalid")
        second = SimpleNamespace(id=8, email="second@example.invalid")
        budget.select_account(first)
        budget.take()
        budget.finish("failed", "synthetic quota")
        budget.decision({"action": "LIMIT", "seconds": 86400}, retry=True)
        self.assertEqual(budget.snapshot()["attempts"][0]["rule_seconds"], 86400)
        budget.decision({"action": "RETRY"}, retry=True)
        self.assertEqual(budget.snapshot()["attempts"][0]["next_action"], "retry")
        budget.select_account(second)
        self.assertEqual(budget.snapshot()["attempts"][0]["next_action"], "switch_account")

    def test_preflight_failures_share_budget_and_polling_finalizes(self):
        budget = GenerationAttempts(3)
        token = SimpleNamespace(id=7, email="synthetic@example.invalid")
        budget.select_account(token)
        budget.take()
        budget.finish("failed", "synthetic captcha")
        budget.take()
        budget.finish("failed", "synthetic submit")
        budget.select_account(token)
        budget.finish("failed", "synthetic preflight")
        self.assertEqual(budget.used, 3)
        self.assertEqual(budget.history[-1]["started_at"], budget.account_started_at)
        with self.assertRaisesRegex(RuntimeError, "生成尝试次数"):
            budget.take()
        budget = GenerationAttempts(1)
        budget.select_account(token)
        budget.take()
        budget.stage("polling")
        budget.finish("accepted", next_action="polling")
        budget.finalize("cancelled", "synthetic cancel")
        row = budget.snapshot()["attempts"][0]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["next_action"], "stop")

    def test_cookie_header_redacts_all_cookie_fields(self):
        budget = GenerationAttempts(1)
        budget.select_account(SimpleNamespace(id=7, email="synthetic@example.invalid"))
        budget.take()
        budget.finish("failed", "Cookie: SID=first-secret; HSID=second-secret")
        value = budget.snapshot()["attempts"][0]["error"]
        self.assertNotIn("first-secret", value)
        self.assertNotIn("second-secret", value)

    def test_saved_error_preserves_public_code_without_credentials(self):
        public_error = "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC_PLEASE_TRY_AGAIN_LATER"
        error = FlowRPCError(
            "rpc_rejected",
            "flow_generate_video",
            public_error=public_error,
        )
        error.args = (
            f"{public_error} Bearer secret-value SOCKS4://user:private@proxy.invalid "
            + "SyntheticOpaqueCredential0123456789" * 2,
        )
        budget = GenerationAttempts(1)
        budget.select_account(SimpleNamespace(id=7, email="synthetic@example.invalid"))
        budget.take()
        budget.finish("failed", error, next_action="stop")
        row = budget.snapshot()["attempts"][0]
        self.assertEqual(row["error_code"], public_error)
        self.assertIn(public_error, row["error"])
        self.assertNotIn("secret-value", row["error"])
        self.assertNotIn("proxy.invalid", row["error"])
        self.assertNotIn("SyntheticOpaque", row["error"])

    def test_inner_retries_keep_separate_durations_and_safe_error_fields(self):
        budget = GenerationAttempts(3)
        budget.select_account(SimpleNamespace(id=7, email="synthetic@example.invalid"))
        budget.take()
        budget.finish(
            "failed",
            "cookie=secret-value https://private.invalid proxy",
            next_action="retry_same_account",
            delay_seconds=2,
        )
        budget.take()
        budget.stage("submit")
        budget.finish("success", next_action="completed")
        result = budget.snapshot()
        self.assertEqual(
            (result["total_attempts"], result["retry_count"], result["account_count"]), (2, 1, 1)
        )
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertNotIn("private.invalid", json.dumps(result))
        self.assertNotIn("_started", json.dumps(result))
