"""Exercise extracted account/config routes through the existing authenticated router."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.database import Database
from src.core.models import Token


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
            get_profile_maintenance_config=AsyncMock(
                return_value={
                    "enabled": True,
                    "refresh_interval_minutes": 30,
                    "revision": 0,
                }
            ),
            update_profile_maintenance_config=AsyncMock(
                return_value={
                    "enabled": False,
                    "refresh_interval_minutes": 45,
                    "revision": 1,
                }
            ),
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
                "scope": "legacy_protocol_accounts",
                "profile_maintenance": "global",
                "profile_default_interval_minutes": 30,
                "scheduler_scan_seconds": 60,
            },
        )

    def test_delete_failure_is_reported_and_does_not_remove_concurrency_state(self):
        self.login()
        self.manager.delete_token.side_effect = OSError("Profile cleanup failed")
        response = self.client.delete("/api/tokens/7")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Profile cleanup failed")
        self.concurrency.remove_token.assert_not_awaited()

    def test_profile_policy_requires_auth_and_exposes_global_scheduler_scope(self):
        self.assertEqual(self.client.get("/api/profile-maintenance/config").status_code, 401)
        self.assertEqual(
            self.client.post("/api/profile-maintenance/config", json={}).status_code, 401
        )
        self.db.get_profile_maintenance_config.assert_not_awaited()
        self.login()
        response = self.client.get("/api/profile-maintenance/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["config"],
            {
                "enabled": True,
                "refresh_interval_minutes": 30,
                "revision": 0,
                "scope": "all_profile_accounts",
                "scheduler_scan_seconds": 60,
            },
        )
        response = self.client.post(
            "/api/profile-maintenance/config",
            json={
                "enabled": False,
                "refresh_interval_minutes": 45,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.db.update_profile_maintenance_config.assert_awaited_once_with(
            enabled=False,
            refresh_interval_minutes=45,
        )

    def test_profile_policy_rejects_incomplete_unknown_and_invalid_payload(self):
        self.login()
        for payload in (
            {},
            {"enabled": True},
            {"enabled": True, "refresh_interval_minutes": 30, "extra": 1},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.client.post("/api/profile-maintenance/config", json=payload).status_code,
                    400,
                )
        self.db.update_profile_maintenance_config.assert_not_awaited()
        self.db.update_profile_maintenance_config.side_effect = ValueError("invalid interval")
        self.assertEqual(
            self.client.post(
                "/api/profile-maintenance/config",
                json={
                    "enabled": True,
                    "refresh_interval_minutes": 0,
                },
            ).status_code,
            400,
        )


class SubmissionProxyBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "accounts.db"))
        await self.db.init_db()
        self.ids = []
        for source, mode in (("cloak", "fixed"), ("cookies", "pool_random")):
            self.ids.append(
                await self.db.add_token(
                    Token(
                        st=f"synthetic-{source}",
                        email=f"{source}@example.invalid",
                        google_cookies='{"SID":"synthetic-cookie"}',
                        login_account=f"{source}@example.invalid",
                        login_password="synthetic-password",
                        proxy_url="http://fixed.invalid:8080",
                        browser_profile_source=source,
                        browser_profile_epoch="preserved-epoch",
                        submission_proxy_mode=mode,
                        is_active=False,
                        ban_reason="429_rate_limit",
                        at_expires="2099-01-01T00:00:00Z",
                        refresh_status="retrying",
                        refresh_failures=2,
                        auto_refresh_enabled=False,
                    )
                )
            )
        self.enterContext(
            patch.multiple(admin, db=self.db, active_admin_tokens={"synthetic-admin-session"})
        )
        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.path = "/api/tokens/submission-proxy/batch"

    async def test_authenticated_batch_deduplicates_missing_and_unchanged_without_identity_changes(
        self,
    ):
        before = [await self.db.get_token(token_id) for token_id in self.ids]
        payload = {"ids": [*self.ids, self.ids[0], 9999], "submission_proxy_mode": "pool_random"}
        self.assertEqual((await self.client.post(self.path, json=payload)).status_code, 401)
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")
        invalid = [
            {**payload, "submission_proxy_mode": "rotate"},
            *({**payload, "ids": ids} for ids in ([], [True], [0], ["1"], list(range(1, 502)))),
        ]
        for body in invalid:
            self.assertEqual((await self.client.post(self.path, json=body)).status_code, 422)
        self.assertEqual([await self.db.get_token(token_id) for token_id in self.ids], before)
        response = await self.client.post(self.path, json=payload)
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([row["id"] for row in results], [*self.ids, 9999])
        self.assertEqual([row["status"] for row in results], ["updated", "unchanged", "failed"])
        self.assertFalse(results[-1]["success"])
        for mode in ("pool_random", "fixed"):
            if mode == "fixed":
                response = await self.client.post(
                    self.path, json={**payload, "submission_proxy_mode": mode}
                )
                self.assertEqual(response.status_code, 200)
            for old in before:
                saved = await self.db.get_token(old.id)
                self.assertEqual(saved.submission_proxy_mode, mode)
                self.assertEqual(
                    saved.model_dump(exclude={"submission_proxy_mode"}),
                    old.model_dump(exclude={"submission_proxy_mode"}),
                )

    async def test_failed_transaction_rolls_back_all_accounts_and_can_be_retried(self):
        await self.db.update_token(self.ids[0], submission_proxy_mode="pool_random")
        async with self.db._connect(write=True) as connection:
            await connection.execute(
                "CREATE TRIGGER reject_strategy BEFORE UPDATE ON tokens "
                f"WHEN old.id={self.ids[1]} BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
            await connection.commit()
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")
        before = [await self.db.get_token(token_id) for token_id in self.ids]
        payload = {"ids": self.ids, "submission_proxy_mode": "fixed"}
        self.assertEqual((await self.client.post(self.path, json=payload)).status_code, 500)
        self.assertEqual([await self.db.get_token(token_id) for token_id in self.ids], before)
        async with self.db._connect(write=True) as connection:
            await connection.execute("DROP TRIGGER reject_strategy")
            await connection.commit()
        self.assertEqual((await self.client.post(self.path, json=payload)).status_code, 200)
        self.assertEqual(
            [(await self.db.get_token(token_id)).submission_proxy_mode for token_id in self.ids],
            ["fixed", "fixed"],
        )
