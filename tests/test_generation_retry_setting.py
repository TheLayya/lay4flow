"""The persisted retry setting is the total image generation attempt bound."""

import tempfile
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api.admin_routes.settings import create_settings_router
from src.core.config import config
from src.core.database import Database
from src.services.flow_client import FlowClient


class GenerationRetrySettingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.enterContext(patch.object(config, "_config", deepcopy(config._config)))
        self.db = Database(db_path=f"{self.temp_dir.name}/flow.db")
        await self.db.init_db()
        app = FastAPI()
        router, _ = create_settings_router(
            lambda: self.db,
            lambda: config,
            AsyncMock(),
            lambda: "synthetic-admin",
            AsyncMock(),
        )
        app.include_router(router)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://synthetic.invalid"
        )
        self.addAsyncCleanup(self.http.aclose)

    async def test_saved_setting_is_exact_total_attempt_bound(self):
        for configured, legacy in ((1, 6), (1, 10), (3, 6), (3, 10)):
            with self.subTest(configured=configured, legacy=legacy):
                response = await self.http.post(
                    "/api/config/generation", json={"max_retries": configured}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["success"])
                persisted = await self.db.get_generation_config()
                self.assertEqual(persisted.max_retries, configured)
                config._config.setdefault("captcha", {})["browser_captcha_generation_retries"] = (
                    legacy
                )
                self.assertEqual(config.flow_max_retries, configured)

                client = FlowClient(None)
                captcha_calls = 0
                finishes = []

                async def captcha(*_args, **_kwargs):
                    nonlocal captcha_calls
                    captcha_calls += 1
                    return f"captcha-{captcha_calls}", f"browser-{captcha_calls}"

                async def failed_submit(*_args, **_kwargs):
                    raise RuntimeError("HTTP Error 403: reCAPTCHA evaluation failed")

                async def finish(browser_id):
                    finishes.append(browser_id)

                with (
                    patch.object(client, "_get_recaptcha_token", AsyncMock(side_effect=captcha)),
                    patch.object(
                        client,
                        "_make_image_generation_request",
                        AsyncMock(side_effect=failed_submit),
                    ) as submit,
                    patch.object(
                        client,
                        "_notify_browser_captcha_request_finished",
                        AsyncMock(side_effect=finish),
                    ),
                    patch("src.services.flow_client.asyncio.sleep", new=AsyncMock()),
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        await client.generate_image(
                            at="synthetic-at",
                            project_id="synthetic-project",
                            prompt="synthetic prompt",
                            model_name="gemini-3.1-flash-image",
                            aspect_ratio="16:9",
                        )

                trace = raised.exception.flow_retry_trace
                self.assertEqual(captcha_calls, configured)
                self.assertEqual(submit.await_count, configured)
                self.assertEqual(
                    [
                        call.kwargs["json_data"]["clientContext"]["recaptchaContext"]["token"]
                        for call in submit.await_args_list
                    ],
                    [f"captcha-{number}" for number in range(1, configured + 1)],
                )
                self.assertEqual(len(finishes), configured)
                self.assertEqual(
                    finishes, [f"browser-{number}" for number in range(1, configured + 1)]
                )
                self.assertEqual(len(trace["generation_attempts"]), configured)
                self.assertEqual(trace["max_retries"], configured)
                self.assertTrue(all(item["recaptcha_ok"] for item in trace["generation_attempts"]))
                self.assertEqual(
                    [item["attempt"] for item in trace["generation_attempts"]],
                    list(range(1, configured + 1)),
                )


if __name__ == "__main__":
    unittest.main()
