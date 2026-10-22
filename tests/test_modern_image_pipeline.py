"""The ordinary generation pipeline reaches the current RPC with no legacy AT."""

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.core.models import Token
from src.services.concurrency_manager import ConcurrencyManager
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


class ModernImagePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_workbench_image_uses_profile_rpc_and_releases_slot(self):
        token = Token(
            id=3,
            st="profile-session:fixture",
            at=None,
            email="fixture@example.invalid",
            browser_profile_source="cloak",
            browser_profile_status="ready",
            google_cookies='{"SID":"synthetic"}',
            user_paygate_tier="PAYGATE_TIER_ONE",
            image_concurrency=1,
            proxy_url="http://fixed.invalid:8080",
            current_project_id="fixture-project",
        )
        context = object()

        @asynccontextmanager
        async def request_context(tokens, current, proxy):
            self.assertEqual(proxy, token.proxy_url)
            yield context

        @asynccontextmanager
        async def provider(current):
            self.assertEqual(current.id, token.id)
            yield context

        manager = SimpleNamespace(
            get_token=AsyncMock(return_value=token),
            db=SimpleNamespace(get_token=AsyncMock(return_value=token)),
            ensure_valid_token=AsyncMock(return_value=token),
            ensure_project_exists=AsyncMock(return_value="fixture-project"),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
            resolve_account_proxy=AsyncMock(return_value=token.proxy_url),
            profile_refresher=SimpleNamespace(
                active_context_provider=provider,
                _proxy_url=AsyncMock(return_value=token.proxy_url),
            ),
            is_profile_validation_recent=lambda current: True,
        )
        flow = FlowClient(None)
        flow.token_manager = manager
        flow.prefill_remote_browser_pool = AsyncMock()
        flow._get_recaptcha_token = AsyncMock(return_value=("synthetic-captcha", "lease"))
        flow._notify_browser_captcha_request_finished = AsyncMock()
        flow._make_request = AsyncMock(side_effect=AssertionError("legacy request"))
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client, handler.token_manager = flow, manager
        handler.load_balancer = SimpleNamespace(
            select_token=AsyncMock(return_value=token), release_pending=AsyncMock()
        )
        handler.concurrency_manager = ConcurrencyManager()
        await handler.concurrency_manager.initialize([token])
        handler.db = SimpleNamespace(
            add_request_log=AsyncMock(return_value=1), update_request_log=AsyncMock()
        )
        handler.file_cache = SimpleNamespace(download_and_cache=AsyncMock())
        generated = [None] * 13 + ["https://media.example.invalid/image.jpg"]
        calls = []

        async def rpc(current, rpcid, payload, **kwargs):
            self.assertIs(current, context)
            calls.append(rpcid)
            if rpcid == "HTrJv":
                return []
            if rpcid == "nzlxg":
                return [10, 1, 2, 2]
            self.assertEqual(rpcid, "ogiZ0b")
            self.assertEqual(payload[1][0][7][5], "fixture-project")
            self.assertEqual(payload[1][0][7][10], ["synthetic-captcha", 1])
            self.assertEqual(kwargs["source_path"], "/project/fixture-project")
            return [[["media-id", None, "workflow-id", None, None, None, [generated]]]]

        with (
            patch("src.services.browser.flow_generation.account_http_session", request_context),
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch(
                "src.services.browser.flow_generation.resolve_image_usage", return_value="BELUGA"
            ),
            patch.object(config, "_config", {**config._config, "cache": {"enabled": False}}),
        ):
            chunks = [
                chunk
                async for chunk in handler.handle_generation(
                    "gemini-3.1-flash-image-square", "A blue cup"
                )
            ]
        self.assertIn("https://media.example.invalid/image.jpg", "".join(chunks))
        self.assertEqual(calls, ["HTrJv", "nzlxg", "ogiZ0b"])
        flow._make_request.assert_not_awaited()
        manager.record_success.assert_awaited_once_with(3)
        manager.record_error.assert_not_awaited()
        self.assertEqual(await handler.concurrency_manager.snapshot(), {3: (0, 0)})
        self.assertIsNone(flow._account_token_ctx.get())
