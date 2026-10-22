"""Load-balancer metrics distinguish assigned work by generation type."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import src.services.load_balancer as load_balancer_module
from src.services.load_balancer import LoadBalancer


class LoadBalancerMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_profile_does_not_take_traffic_from_existing_generation_transport(self):
        modern = SimpleNamespace(id=1, browser_profile_source="cloak", at=None)
        existing = SimpleNamespace(
            id=2,
            browser_profile_source="cookies",
            at="fixture",
            email="fixture@example.invalid",
            image_enabled=True,
            user_paygate_tier="PAYGATE_TIER_TWO",
            credits=10,
        )
        manager = SimpleNamespace(
            get_active_tokens=AsyncMock(return_value=[modern, existing]),
            ensure_valid_token=AsyncMock(return_value=existing),
            needs_at_refresh=lambda token: False,
        )
        balancer = LoadBalancer(manager)
        self.assertIs(await balancer.select_token(for_image_generation=True), existing)
        manager.ensure_valid_token.assert_awaited_once_with(existing)
        manager.get_active_tokens.return_value = [modern]
        self.assertIsNone(await balancer.select_token(for_image_generation=True))
        self.assertIn(
            "新版生成接口适配尚未完成",
            await balancer.get_unavailable_reason(for_image_generation=True),
        )

    async def test_image_and_video_assigned_counts_do_not_overwrite(self):
        balancer = LoadBalancer(token_manager=object())
        with patch.object(load_balancer_module, "OBSERVABILITY") as metrics:
            await balancer._add_pending(1, True, False)
            await balancer._add_pending(2, False, True)
            await balancer.release_pending(1, for_image_generation=True)
            await balancer.release_pending(2, for_video_generation=True)

        self.assertEqual(
            metrics.set_load_balancer_assigned.call_args_list,
            [
                unittest.mock.call("image", 1),
                unittest.mock.call("video", 1),
                unittest.mock.call("image", 0),
                unittest.mock.call("video", 0),
            ],
        )


if __name__ == "__main__":
    unittest.main()
