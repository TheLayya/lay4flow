"""Replay supported upstream shapes without submitting a generation job."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import admin
from src.services.flow_client import FlowClient


class FlowContractTests(unittest.TestCase):
    def test_supported_video_status_shapes_and_additive_fields(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/flow/video_status.json").read_text(encoding="utf-8")
        )
        client = FlowClient(None)
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                normalized = client._normalize_video_generation_response(
                    case["response"], "project"
                )
                operation = normalized["operations"][0]
                expected = case["expected"]
                self.assertEqual(operation["operation"]["name"], expected["media_id"])
                self.assertEqual(operation["status"], expected["status"])
                if "duration" in expected:
                    self.assertEqual(
                        operation["operation"]["metadata"]["video"]["duration"],
                        expected["duration"],
                    )
                if "error_code" in expected:
                    self.assertEqual(
                        operation["operation"]["error"]["code"], expected["error_code"]
                    )


class AdminScoreRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_score_request_body_reaches_configured_provider(self):
        db = SimpleNamespace(
            get_captcha_config=AsyncMock(
                return_value=SimpleNamespace(
                    captcha_method="remote_browser",
                    browser_proxy_enabled=False,
                    browser_proxy_url="",
                )
            )
        )
        solver = AsyncMock(
            return_value={"success": True, "verify_result": {"success": True, "score": 0.9}}
        )
        request = admin.CaptchaScoreTestRequest(action="synthetic_action")
        with (
            patch.object(admin, "db", db),
            patch.object(admin, "_score_test_with_remote_browser_service", solver),
        ):
            result = await admin.test_captcha_score(request, "synthetic-admin-token")
        self.assertEqual(solver.await_args.kwargs["action"], "synthetic_action")
        self.assertTrue(result["success"])
