"""Cached fixed-proxy admission for new generation, without network probes."""

import inspect

from .proxy_urls import normalize_proxy_url


def pool_entry_unavailable(entry):
    """A failed connection needs a successful probe before scheduling resumes."""
    return (
        entry is None
        or not getattr(entry, "enabled", False)
        or getattr(entry, "last_check_ok", None) is False
        or getattr(entry, "cooldown_until", None) is not None
        or getattr(entry, "circuit_state", "ready") in {"cooling", "probing", "open"}
    )


async def proxy_pool_snapshot(database):
    reader = getattr(database, "list_proxy_pool", None)
    if not inspect.iscoroutinefunction(reader):
        return None
    return {entry.id: entry for entry in await reader()}


def bound_proxy_unavailable(token, entries):
    identifier = getattr(token, "proxy_pool_id", None)
    if identifier is None or entries is None:
        return False
    entry = entries.get(identifier)
    if pool_entry_unavailable(entry):
        return True
    try:
        proxy = normalize_proxy_url(entry.proxy_url)
        return not proxy or normalize_proxy_url(getattr(token, "proxy_url", None)) != proxy
    except ValueError:
        return True
