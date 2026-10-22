"""Load-balancer metrics distinguish assigned work by generation type."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import src.services.load_balancer as load_balancer_module
from src.services.load_balancer import LoadBalancer


class LoadBalancerMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_profile_is_selected_without_legacy_bearer(self):
        modern = SimpleNamespace(
            id=2,
            browser_profile_source="cloak",
            at=None,
            email="fixture@example.invalid",
            image_enabled=True,
            user_paygate_tier="PAYGATE_TIER_TWO",
            credits=10,
        )
        manager = SimpleNamespace(
            get_active_tokens=AsyncMock(return_value=[modern]),
            ensure_valid_token=AsyncMock(return_value=modern),
            needs_at_refresh=lambda token: False,
        )
        balancer = LoadBalancer(manager)
        self.assertIs(await balancer.select_token(for_image_generation=True), modern)
        manager.ensure_valid_token.assert_awaited_once_with(modern)
        self.assertIsNone(await balancer.get_unavailable_reason(for_image_generation=True))

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
