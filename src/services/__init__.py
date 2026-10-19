"""Compatible service exports, loaded only when a caller needs them."""

from importlib import import_module

_EXPORTS = {
    "FlowClient": "flow_client",
    "ProxyManager": "proxy_manager",
    "LoadBalancer": "load_balancer",
    "ConcurrencyManager": "concurrency_manager",
    "TokenManager": "token_manager",
    "GenerationHandler": "generation_handler",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    globals()[name] = value
    return value
