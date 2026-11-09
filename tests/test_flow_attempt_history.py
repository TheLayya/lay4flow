"""Real submission loops emit request attempt evidence with synthetic services."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_flow_profile_generation as fixtures

from src.services.flow.remote_captcha import RemoteCaptchaError
from src.services.generation_attempts import CURRENT_ATTEMPTS, GenerationAttempts


class FlowAttemptHistoryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.FlowProfileGenerationTests.setUp
    generation = fixtures.FlowProfileGenerationTests.generation
    response = fixtures.FlowProfileGenerationTests.response

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


if __name__ == "__main__":
    unittest.main()
