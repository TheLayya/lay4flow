"""Exercise extracted account/config routes through the existing authenticated router."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin


class AdminAccountConfigRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(
            get_all_tokens_with_stats=AsyncMock(return_value=[]),
            update_token_refresh_config=AsyncMock(
                return_value=SimpleNamespace(
                    enabled=False,
                    refresh_interval_minutes=120,
                )
            ),
            update_cache_config=AsyncMock(),
            reload_config_to_memory=AsyncMock(),
        )
        self.manager = SimpleNamespace(
            add_token=AsyncMock(
                return_value=SimpleNamespace(
                    id=7,
                    email="test@example.invalid",
                    credits=100,
                    current_project_id="project",
                    current_project_name="Project",
                    image_concurrency=2,
                    video_concurrency=1,
                )
            ),
            delete_token=AsyncMock(),
            disable_token=AsyncMock(),
        )
        self.concurrency = SimpleNamespace(reset_token=AsyncMock(), remove_token=AsyncMock())
        self.sync_cache = AsyncMock()
        self.enterContext(
            patch.multiple(
                admin,
                db=self.db,
                token_manager=self.manager,
                concurrency_manager=self.concurrency,
                active_admin_tokens={"synthetic-admin-session"},
                _sync_runtime_cache_config=self.sync_cache,
            )
        )
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def login(self):
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")

    def test_account_routes_keep_auth_and_concurrency_updates(self):
        self.assertEqual(self.client.get("/api/tokens").status_code, 401)
        self.assertEqual(self.client.post("/api/cache/enabled", json={}).status_code, 401)
        self.db.get_all_tokens_with_stats.assert_not_awaited()
        self.db.update_cache_config.assert_not_awaited()
        self.login()
        response = self.client.post("/api/tokens", json={"st": "synthetic-st"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"]["id"], 7)
        self.concurrency.reset_token.assert_awaited_once_with(
            7, image_concurrency=2, video_concurrency=1
        )
        self.assertTrue(self.client.post("/api/tokens/7/disable").json()["success"])
        self.manager.disable_token.assert_awaited_once_with(7)
        self.assertTrue(self.client.delete("/api/tokens/7").json()["success"])
        self.concurrency.remove_token.assert_awaited_once_with(7)

    def test_runtime_services_are_resolved_after_router_registration(self):
        self.login()
        replacement = SimpleNamespace(get_all_tokens_with_stats=AsyncMock(return_value=[]))
        with patch.object(admin, "db", replacement):
            self.assertEqual(self.client.get("/api/tokens").json(), [])
        replacement.get_all_tokens_with_stats.assert_awaited_once()
        self.db.get_all_tokens_with_stats.assert_not_awaited()

    def test_cache_update_validation_and_reload_order(self):
        self.login()
        for timeout in (-1, "invalid"):
            self.assertEqual(
                self.client.post("/api/cache/config", json={"timeout": timeout}).status_code, 400
            )
        self.db.update_cache_config.assert_not_awaited()
        order = []

        async def persist(**values):
            order.append("persist")

        async def reload():
            order.append("reload")

        async def sync():
            order.append("sync")

        self.db.update_cache_config.side_effect = persist
        self.db.reload_config_to_memory.side_effect = reload
        self.sync_cache.side_effect = sync
        response = self.client.post("/api/cache/config", json={"timeout": "120", "enabled": False})
        self.assertEqual(response.status_code, 200)
        self.db.update_cache_config.assert_awaited_once_with(
            enabled=False, timeout=120, base_url=None
        )
        self.assertEqual(order, ["persist", "reload", "sync"])

    def test_refresh_toggle_preserves_omitted_false_and_configuration_payload(self):
        self.login()
        self.assertTrue(self.client.post("/api/token-refresh/enabled", json={}).json()["success"])
        self.db.update_token_refresh_config.assert_not_awaited()
        self.client.post("/api/token-refresh/enabled", json={"enabled": False})
        self.db.update_token_refresh_config.assert_awaited_once_with(enabled=False)
        self.db.update_token_refresh_config.reset_mock()
        response = self.client.post(
            "/api/token-refresh/config", json={"refresh_interval_minutes": 120}
        )
        self.db.update_token_refresh_config.assert_awaited_once_with(
            enabled=None, refresh_interval_minutes=120
        )
        self.assertEqual(
            response.json()["config"],
            {
                "at_auto_refresh_enabled": True,
                "protocol_refresh_enabled": False,
                "refresh_interval_minutes": 120,
            },
        )

    def test_delete_failure_is_reported_and_does_not_remove_concurrency_state(self):
        self.login()
        self.manager.delete_token.side_effect = OSError("Profile cleanup failed")
        response = self.client.delete("/api/tokens/7")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Profile cleanup failed")
        self.concurrency.remove_token.assert_not_awaited()
