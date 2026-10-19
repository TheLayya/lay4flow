"""Exercise the ordinary Images API through the real, fully offline Flow pipeline."""

import asyncio
import json
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import openai_images, routes
from src.core.auth import verify_openai_api_key
from src.core.config import config
from src.services.concurrency_manager import ConcurrencyManager
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


class SyntheticSession:
    def __init__(self):
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_posts = False
        self.result = {
            "media": [
                {
                    "name": "synthetic-media",
                    "image": {
                        "generatedImage": {"fifeUrl": "https://asset.example.test/image.png"}
                    },
                }
            ]
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if self.block_posts:
            self.entered.set()
            await self.release.wait()
        return SimpleNamespace(
            status_code=200, headers={}, text=json.dumps(self.result), json=lambda: self.result
        )


class ImagePipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        example = Path(__file__).resolve().parents[1] / "config" / "setting_example.toml"
        synthetic_config = tomllib.loads(example.read_text(encoding="utf-8"))
        synthetic_config["captcha"]["captcha_method"] = "remote_browser"
        synthetic_config["flow"]["max_retries"] = 1
        synthetic_config["cache"]["enabled"] = False
        synthetic_config["debug"]["enabled"] = False
        self.enterContext(patch.object(config, "_config", synthetic_config))

        self.token = SimpleNamespace(
            id=7,
            email="synthetic@example.invalid",
            at="synthetic-at",
            st="synthetic-st",
            proxy_url="http://business.example.test:8080",
            is_active=True,
            image_concurrency=1,
            video_concurrency=1,
            user_paygate_tier="PAYGATE_TIER_TWO",
        )
        self.captcha = AsyncMock(
            return_value={
                "success": True,
                "token": "synthetic-captcha",
                "session_id": "remote-session",
                "fingerprint": {
                    "user_agent": "Synthetic source UA",
                    "accept_language": "en-US,en;q=0.9",
                    "proxy_url": "http://source.example.test:8080",
                },
            }
        )
        proxy = SimpleNamespace(
            get_request_proxy_url=AsyncMock(return_value="http://global.example.test:8080"),
            get_media_proxy_url=AsyncMock(return_value=None),
        )
        self.flow = FlowClient(proxy)
        self.flow._call_remote_browser_service = self.captcha
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler.flow_client = self.flow
        self.handler.db = SimpleNamespace(
            add_request_log=AsyncMock(return_value=1), update_request_log=AsyncMock()
        )
        self.handler.token_manager = SimpleNamespace(
            get_token=AsyncMock(return_value=self.token),
            ensure_valid_token=AsyncMock(return_value=self.token),
            ensure_project_exists=AsyncMock(return_value="synthetic-project"),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        self.handler.load_balancer = SimpleNamespace(
            select_token=AsyncMock(return_value=self.token), release_pending=AsyncMock()
        )
        self.handler.concurrency_manager = ConcurrencyManager()
        await self.handler.concurrency_manager.initialize([self.token])
        self.handler.file_cache = SimpleNamespace(download_and_cache=AsyncMock())
        self.enterContext(patch.object(routes, "generation_handler", self.handler))
        self.session = SyntheticSession()
        self.enterContext(patch("src.services.flow_client.AsyncSession", return_value=self.session))
        # A missed transport mock must fail locally rather than reach an upstream service.
        self.enterContext(
            patch.object(
                self.flow,
                "_sync_json_request_via_urllib",
                side_effect=AssertionError("Unexpected real network fallback"),
            )
        )
        app = FastAPI()
        app.dependency_overrides[verify_openai_api_key] = lambda: "synthetic-key"
        app.include_router(openai_images.create_images_router())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def generate(self):
        return await self.client.post("/v1/images/generations", json={"prompt": "Synthetic cat"})

    async def assert_released(self):
        self.assertEqual(await self.handler.concurrency_manager.snapshot(), {7: (0, 0)})
        self.handler.load_balancer.release_pending.assert_awaited_once_with(
            7, for_image_generation=True, for_video_generation=False
        )
        self.assertIsNone(self.flow._account_proxy_ctx.get())
        self.handler.file_cache.download_and_cache.assert_not_awaited()

    async def test_regular_images_api_reaches_flow_and_returns_structured_image(self):
        response = await self.generate()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], [{"url": "https://asset.example.test/image.png"}])
        solves = [
            call for call in self.captcha.await_args_list if call.kwargs["path"] == "/api/v1/solve"
        ]
        self.assertEqual(len(solves), 1)
        self.assertEqual(solves[0].kwargs["json_data"]["token_id"], 7)
        self.assertEqual(len(self.session.requests), 1)
        url, request = self.session.requests[0]
        self.assertIn("/projects/synthetic-project/flowMedia:batchGenerateImages", url)
        self.assertEqual(request["proxy"], self.token.proxy_url)
        self.assertEqual(request["headers"]["authorization"], "Bearer synthetic-at")
        self.assertEqual(request["headers"]["User-Agent"], "Synthetic source UA")
        self.assertFalse(any(key.lower() == "cookie" for key in request["headers"]))
        self.assertNotIn("synthetic-source-cookie", repr(request))
        self.assertFalse(request["allow_redirects"])
        payload = request["json"]
        self.assertEqual(payload["clientContext"]["recaptchaContext"]["token"], "synthetic-captcha")
        self.assertEqual(payload["clientContext"]["projectId"], "synthetic-project")
        self.assertEqual(
            payload["requests"][0]["structuredPrompt"]["parts"], [{"text": "Synthetic cat"}]
        )
        self.handler.token_manager.record_usage.assert_awaited_once_with(7, is_video=False)
        self.handler.token_manager.record_success.assert_awaited_once_with(7)
        self.handler.token_manager.record_error.assert_not_awaited()
        self.assertEqual(self.handler.db.add_request_log.await_args.args[0].status_code, 200)
        await self.assert_released()

    async def test_empty_upstream_media_is_http_failure_and_releases_account(self):
        self.session.result = {"media": []}
        response = await self.generate()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("error", response.json())
        self.assertEqual(response.json()["error"]["message"], "生成结果为空")
        self.assertNotIn("data", response.json())
        self.assertEqual(len(self.session.requests), 1)
        self.handler.token_manager.record_usage.assert_not_awaited()
        self.handler.token_manager.record_success.assert_not_awaited()
        self.handler.token_manager.record_error.assert_awaited_once_with(7)
        await self.assert_released()

    async def test_malformed_upstream_media_is_502_without_keyerror(self):
        malformed = [
            None,
            [],
            {"media": None},
            {"media": {}},
            {"media": "not-a-list"},
            {"media": [None]},
            {"media": ["not-an-object"]},
            {"media": [{}]},
            {"media": [{"image": None}]},
            {"media": [{"image": "not-an-object"}]},
            {"media": [{"image": {}}]},
            {"media": [{"image": {"generatedImage": None}}]},
            {"media": [{"image": {"generatedImage": "not-an-object"}}]},
            {"media": [{"image": {"generatedImage": {}}}]},
        ] + [
            {"media": [{"image": {"generatedImage": {"fifeUrl": url}}}]}
            for url in (
                None,
                7,
                [],
                "",
                "  ",
                "not-a-url",
                "ftp://asset.example.test/image.png",
                "https:///image.png",
                "https://[invalid/image.png",
                "https://asset.example.test:invalid/image.png",
                "https://asset.example.test:0/image.png",
                "https://asset.example.test/image bad.png",
                "https://user:password@asset.example.test/image.png",
            )
        ]
        for result in malformed:
            with self.subTest(result=result):
                self.session.result = result
                response = await self.generate()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.json()["error"]["message"], "生成结果中的图片媒体无效")
                self.assertNotIn("data", response.json())
                self.assertNotIn("KeyError", response.text)
                self.handler.token_manager.record_usage.assert_not_awaited()
                self.handler.token_manager.record_success.assert_not_awaited()
                self.handler.token_manager.record_error.assert_awaited_once_with(7)
                log = self.handler.db.add_request_log.await_args.args[0]
                self.assertEqual(log.status_text, "failed")
                self.assertEqual(log.status_code, 500)
                self.assertEqual(json.loads(log.response_body)["error"], "生成结果中的图片媒体无效")
                await self.assert_released()
                self.handler.load_balancer.release_pending.reset_mock()
                self.handler.token_manager.record_error.reset_mock()

    async def test_cancelled_second_image_request_releases_pending_while_first_holds_slot(self):
        config._config["captcha"]["captcha_method"] = "remote_browser"
        self.session.block_posts = True
        captcha = AsyncMock(return_value=("synthetic-captcha", None))
        self.enterContext(patch.object(self.flow, "_get_recaptcha_token", captcha))
        manager = self.handler.concurrency_manager
        original_wait = manager.wait_acquire_image
        waiting = asyncio.Event()

        async def wait_for_slot(token_id, timeout):
            if await manager.get_image_inflight(token_id):
                waiting.set()
            return await original_wait(token_id, timeout)

        self.enterContext(patch.object(manager, "wait_acquire_image", side_effect=wait_for_slot))

        first = asyncio.create_task(self.generate())
        second = None
        try:
            await asyncio.wait_for(self.session.entered.wait(), timeout=1)
            second = asyncio.create_task(self.generate())
            await asyncio.wait_for(waiting.wait(), timeout=1)
            self.assertEqual(self.handler.load_balancer.select_token.await_count, 2)
            self.assertEqual(len(self.session.requests), 1)
            self.assertEqual(await manager.get_image_inflight(7), 1)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            self.assertEqual(await manager.get_image_inflight(7), 1)
            self.assertEqual(self.handler.load_balancer.release_pending.await_count, 2)
            self.session.release.set()
            response = await asyncio.wait_for(first, timeout=1)
        finally:
            self.session.release.set()
            tasks = [task for task in (first, second) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.session.requests), 1)
        self.assertEqual(await manager.snapshot(), {7: (0, 0)})
        self.assertEqual(self.handler.load_balancer.release_pending.await_count, 2)
        captcha.assert_awaited_once_with("synthetic-project", action="IMAGE_GENERATION", token_id=7)
        self.handler.token_manager.record_usage.assert_awaited_once_with(7, is_video=False)
        self.handler.token_manager.record_error.assert_not_awaited()
        self.assertNotIn(
            "/api/v1/solve", [call.kwargs["path"] for call in self.captcha.await_args_list]
        )

    async def test_remote_solve_failure_never_submits_and_releases_account(self):
        self.captcha.side_effect = RuntimeError("Remote fixture unavailable")
        response = await self.generate()
        self.assertGreaterEqual(response.status_code, 500, response.text)
        self.assertIn("error", response.json())
        self.assertEqual(self.session.requests, [])
        self.handler.token_manager.record_usage.assert_not_awaited()
        self.handler.token_manager.record_success.assert_not_awaited()
        self.assertIsNone(self.flow.get_request_fingerprint())
        await self.assert_released()


if __name__ == "__main__":
    unittest.main()
