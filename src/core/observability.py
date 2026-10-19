"""Bounded metrics for the load-balancer events actually recorded by the service."""

from typing import Any

from .monitoring import MAIN_REGISTRY, CollectorRegistry, Counter, Gauge

_GENERATION_TYPES = frozenset({"image", "video", "other"})
_RESULTS = frozenset({"success", "no_token", "other"})


def _bounded(value: Any, allowed: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else "other"


class FlowObservability:
    """Metrics use fixed label sets; account data and errors never become labels."""

    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry if registry is not None else MAIN_REGISTRY
        self.load_balancer_selections = Counter(
            "lay4flow_load_balancer_selections_total",
            "Token selection outcomes grouped by generation type.",
            ("generation_type", "result"),
            registry=self.registry,
        )
        self.load_balancer_assigned = Gauge(
            "lay4flow_load_balancer_assigned_requests",
            "Unlimited or not-yet-admitted requests tracked by the load balancer; not a physical wait queue.",
            ("generation_type",),
            registry=self.registry,
        )

    def record_load_balancer(self, generation_type: Any, result: Any) -> None:
        self.load_balancer_selections.labels(
            generation_type=_bounded(generation_type, _GENERATION_TYPES),
            result=_bounded(result, _RESULTS),
        ).inc()

    def set_load_balancer_assigned(self, generation_type: str, count: int) -> None:
        self.load_balancer_assigned.labels(
            generation_type=_bounded(generation_type, _GENERATION_TYPES)
        ).set(max(0, count))


OBSERVABILITY = FlowObservability()
