"""Bounded observability metrics never expose account or upstream payload data."""

import unittest

from prometheus_client import CollectorRegistry, generate_latest

from src.core.observability import FlowObservability


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        self.metrics = FlowObservability(self.registry)

    def text(self) -> str:
        return generate_latest(self.registry).decode("utf-8")

    def test_records_load_balancer_with_bounded_labels(self):
        self.metrics.record_load_balancer("/private-secret", "account@example.com")
        self.metrics.record_load_balancer("image", "success")
        self.metrics.set_load_balancer_assigned("video", 4)

        output = self.text()
        self.assertIn('generation_type="other",result="other"', output)
        self.assertIn('generation_type="image",result="success"', output)
        self.assertIn('generation_type="video"', output)
        self.assertNotIn("account@example.com", output)
        self.assertNotIn("private-secret", output)

    def test_status_classes_and_values_are_normalized(self):
        self.metrics.record_load_balancer("IMAGE", "SUCCESS")
        self.metrics.record_load_balancer("poll", "failure")
        self.metrics.set_load_balancer_assigned("video", -5)

        output = self.text()
        self.assertIn('generation_type="image",result="success"', output)
        self.assertIn('generation_type="other",result="other"', output)

    def test_isolated_registries_do_not_collide(self):
        other_registry = CollectorRegistry()
        other = FlowObservability(other_registry)
        other.record_load_balancer("image", "success")
        self.assertIn(
            "lay4flow_load_balancer_selections_total", generate_latest(other_registry).decode()
        )
        self.assertNotIn(
            'load_balancer_selections_total{generation_type="image",result="success"}', self.text()
        )


if __name__ == "__main__":
    unittest.main()
