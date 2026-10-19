"""Runtime metrics stay bounded and take one concurrency snapshot per scrape."""

import unittest
from unittest.mock import AsyncMock, patch

from prometheus_client import generate_latest

from src.core import monitoring
from src.core.config import REMOTE_BROWSER_BASE_URL, config
from src.core.monitoring import update_main_runtime_metrics


class MonitoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(config._config.setdefault("captcha", {}), {"remote_browser_api_key": ""})
        )
        self.remote_health = self.enterContext(
            patch.object(
                monitoring, "_cached_remote_browser_health", AsyncMock(return_value=(False, 0))
            )
        )

    def test_main_metrics_use_lay4flow_namespace(self):
        output = generate_latest(monitoring.MAIN_REGISTRY).decode()
        self.assertIn("lay4flow_up", output)
        self.assertIn("lay4flow_generation_requests", output)
        self.assertNotIn("old_service_", output)

    async def test_scrape_uses_one_snapshot_without_per_token_lock_queries(self):
        db = AsyncMock()
        db.get_all_tokens_with_stats.return_value = [
            {
                "id": 7,
                "is_active": True,
                "at": "at",
                "credits": 3,
                "error_count": 0,
                "today_error_count": 0,
                "consecutive_error_count": 0,
            },
            {
                "id": 8,
                "is_active": False,
                "at": "",
                "credits": 0,
                "error_count": 1,
                "today_error_count": 1,
                "consecutive_error_count": 1,
            },
        ]
        db.get_dashboard_stats.return_value = {}
        manager = AsyncMock()
        manager.snapshot.return_value = {7: (2, 1), 8: (0, 0)}

        await update_main_runtime_metrics(db, concurrency_manager=manager)

        manager.snapshot.assert_awaited_once_with()
        manager.get_image_inflight.assert_not_awaited()
        manager.get_video_inflight.assert_not_awaited()

    async def test_db_snapshot_is_cached_per_instance_and_recovers_after_failure(self):
        db = AsyncMock()
        db.get_all_tokens_with_stats.return_value = []
        db.get_dashboard_stats.return_value = {}
        manager = AsyncMock()
        manager.snapshot.return_value = {}
        with patch.object(monitoring, "_metrics_cache_ttl_seconds", 0):
            await update_main_runtime_metrics(db, manager)
            db.get_all_tokens_with_stats.side_effect = RuntimeError("temporary")
            db.get_dashboard_stats.side_effect = RuntimeError("temporary")
            await update_main_runtime_metrics(db, manager)
            self.assertEqual(db.get_all_tokens_with_stats.await_count, 2)
            db.get_all_tokens_with_stats.side_effect = None
            db.get_dashboard_stats.side_effect = None
            db.get_all_tokens_with_stats.return_value = []
            db.get_dashboard_stats.return_value = {"total_images": 2}
            await update_main_runtime_metrics(db, manager)
            self.assertEqual(db.get_all_tokens_with_stats.await_count, 3)

    async def test_captcha_readiness_requires_key_and_is_independent_of_network_health(self):
        db = AsyncMock()
        db.get_all_tokens_with_stats.return_value = []
        db.get_dashboard_stats.return_value = {}
        for key, configured in (("", False), ("   ", False), ("synthetic-key", True)):
            with self.subTest(configured=configured, key=key):
                config._config["captcha"]["remote_browser_api_key"] = key
                snapshot = await monitoring.build_public_health_snapshot(db)
                self.assertIs(snapshot["remote_browser_configured"], configured)
                self.remote_health.reset_mock()
                await update_main_runtime_metrics(db)
                self.assertEqual(
                    monitoring.REMOTE_BROWSER_CONFIGURED._value.get(), float(configured)
                )
                self.assertEqual(monitoring.REMOTE_BROWSER_TARGET_UP._value.get(), 0)
                if configured:
                    self.remote_health.assert_awaited_once_with(REMOTE_BROWSER_BASE_URL)
                else:
                    self.remote_health.assert_not_awaited()

    async def test_turning_off_token_labels_clears_previous_series(self):
        db = AsyncMock()
        db.get_all_tokens_with_stats.return_value = [
            {"id": 9001, "is_active": True, "at": "at", "credits": 1}
        ]
        db.get_dashboard_stats.return_value = {}
        manager = AsyncMock()
        manager.snapshot.return_value = {9001: (0, 0)}
        server_config = config._config.setdefault("server", {})
        previous = server_config.get("metrics_include_token_labels", False)
        try:
            server_config["metrics_include_token_labels"] = True
            await update_main_runtime_metrics(db, manager)
            self.assertIn('token_id="9001"', generate_latest(monitoring.MAIN_REGISTRY).decode())
            server_config["metrics_include_token_labels"] = False
            await update_main_runtime_metrics(db, manager)
            self.assertNotIn('token_id="9001"', generate_latest(monitoring.MAIN_REGISTRY).decode())
        finally:
            server_config["metrics_include_token_labels"] = previous


if __name__ == "__main__":
    unittest.main()
