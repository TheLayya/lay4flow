"""Remote captcha settings validate before persistence and reject other backends."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api import admin
from src.core.config import REMOTE_BROWSER_BASE_URL, config
from src.core.database import Database


class AdminCaptchaConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(redirect_stdout(io.StringIO()))
        self.database = Database(str(Path(directory.name) / "settings.db"))
        await self.database.init_db()
        await self.database.update_captcha_config(
            captcha_method="remote_browser",
            remote_browser_base_url="https://solver.example.invalid",
            remote_browser_api_key="synthetic-key",
            remote_browser_timeout=120,
        )
        self.enterContext(patch.object(admin, "db", self.database))
        self.enterContext(patch.object(config, "_config", {}))

    async def settings(self):
        return (await self.database.get_captcha_config()).model_dump(exclude={"updated_at"})

    async def test_partial_updates_preserve_saved_credentials_and_reload(self):
        result = await admin.update_captcha_config({"remote_browser_timeout": 90}, "session")
        self.assertTrue(result["success"])
        for request in ({}, {"captcha_method": "remote_browser"}):
            await admin.update_captcha_config(request, "session")
        settings = await admin.get_captcha_config("session")
        self.assertEqual(
            settings,
            {
                "captcha_method": "remote_browser",
                "remote_browser_base_url": REMOTE_BROWSER_BASE_URL,
                "remote_browser_api_key": "synthetic-key",
                "remote_browser_timeout": 90,
            },
        )
        self.assertEqual(config.remote_browser_timeout, 90)

    async def test_service_url_is_fixed_for_existing_rows_and_legacy_requests(self):
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                "UPDATE captcha_config SET remote_browser_base_url = ? WHERE id = 1",
                ("https://legacy.example.invalid",),
            )
            await connection.commit()
        self.assertEqual(
            (await admin.get_captcha_config("session"))["remote_browser_base_url"],
            REMOTE_BROWSER_BASE_URL,
        )
        for legacy_url in ("https://legacy.example.invalid", "", "invalid"):
            with self.subTest(legacy_url=legacy_url):
                await admin.update_captcha_config(
                    {"remote_browser_base_url": legacy_url}, "session"
                )
                self.assertEqual(
                    (await self.database.get_captcha_config()).remote_browser_base_url,
                    REMOTE_BROWSER_BASE_URL,
                )
                self.assertEqual(config.remote_browser_base_url, REMOTE_BROWSER_BASE_URL)

    async def test_invalid_credentials_timeout_and_old_backends_do_not_persist(self):
        expected = await self.settings()
        for request in (
            {"captcha_method": "protocol"},
            {"captcha_method": "browser"},
            {"captcha_method": "personal"},
            {"captcha_method": "extension"},
            {"remote_browser_base_url": None},
            {"remote_browser_base_url": 123},
            {"remote_browser_api_key": ""},
            {"remote_browser_api_key": None},
            {"remote_browser_timeout": 0},
            {"remote_browser_timeout": 601},
            {"remote_browser_timeout": "60"},
            {"remote_browser_timeout": True},
            {"browser_count": 3},
        ):
            with self.subTest(request=request):
                with self.assertRaises(HTTPException) as failure:
                    await admin.update_captcha_config(request, "session")
                self.assertEqual(failure.exception.status_code, 400)
                self.assertEqual(await self.settings(), expected)

    async def test_config_http_route_rejects_method_before_database_access(self):
        app = FastAPI()
        app.include_router(admin.router)
        app.dependency_overrides[admin.verify_admin_token] = lambda: "synthetic-session"
        with TestClient(app) as client:
            for method in ("protocol", "browser", "personal", "extension", "yescaptcha"):
                response = client.post("/api/captcha/config", json={"captcha_method": method})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["detail"], "Only remote_browser captcha is supported"
                )
        self.assertFalse(any(route.path.startswith("/api/plugin") for route in admin.router.routes))

    async def test_reload_runs_after_successful_persistence(self):
        order = []
        persist = self.database.update_captcha_config
        reload_config = self.database.reload_config_to_memory

        async def save(**values):
            order.append("save")
            await persist(**values)

        async def reload():
            order.append("reload")
            await reload_config()

        with (
            patch.object(self.database, "update_captcha_config", side_effect=save),
            patch.object(self.database, "reload_config_to_memory", side_effect=reload),
        ):
            result = await admin.update_captcha_config({"remote_browser_timeout": 80}, "session")
        self.assertTrue(result["success"])
        self.assertEqual(order, ["save", "reload"])


if __name__ == "__main__":
    unittest.main()
