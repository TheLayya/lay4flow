"""Load-balancer metrics distinguish assigned work by generation type."""

import unittest
from unittest.mock import patch

import src.services.load_balancer as load_balancer_module
from src.services.load_balancer import LoadBalancer


class LoadBalancerMetricsTests(unittest.IsolatedAsyncioTestCase):
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
