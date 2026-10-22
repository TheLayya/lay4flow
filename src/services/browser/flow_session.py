"""Flow HTTP sessions bound to a persisted Profile identity and account exit."""

import json
from contextlib import asynccontextmanager
from dataclasses import asdict

from ..flow.browser_request import _key, snapshot_http_request
from ..flow.flow_rpc import FlowRPCError
from ..flow.google_cookies import GoogleCookieJar
from ..project_pool import flow_session_binding, profile_project_binding
from .environment import EnvironmentSnapshot


async def save_session_cookies(tokens, token, before, after):
    if GoogleCookieJar(before).export() == GoogleCookieJar(after).export():
        return
    lock = await tokens._get_token_lock(
        tokens._profile_cookie_locks, tokens._refresh_lock_guard, token.id
    )
    async with lock:
        await _save_session_cookies(tokens, token, before, after)


async def _save_session_cookies(tokens, token, before, after):
    """Merge only unchanged cookie keys; a concurrent login always wins."""
    original = {_key(row): row for row in GoogleCookieJar(before).export()}
    updated = {_key(row): row for row in GoogleCookieJar(after).export()}
    # Retry only the local compare-and-swap; never replay an upstream operation.
    for _ in range(4):
        current = await tokens.db.get_token(token.id)
        if current is None or flow_session_binding(current) != flow_session_binding(token):
            return
        merged = {_key(row): row for row in GoogleCookieJar(current.google_cookies).export()}
        for key in original.keys() - updated.keys():
            if merged.get(key) == original[key]:
                merged.pop(key, None)
        for key, row in updated.items():
            if original.get(key) != row and merged.get(key) == original.get(key):
                merged[key] = row
        value = json.dumps(list(merged.values()), separators=(",", ":"))
        if await tokens.db.update_token_if_credentials_match(
            token.id,
            profile_project_binding(current),
            expected_proxy_binding=(current.proxy_pool_id, current.proxy_url)
            if current.proxy_pool_id is not None
            else None,
            google_cookies=value,
        ):
            token.google_cookies = value
            return


@asynccontextmanager
async def account_http_session(tokens, token, proxy, *, persist=True):
    """Read the verified navigator snapshot, never open the account browser."""
    refresher = tokens.profile_refresher
    if refresher is None:
        raise FlowRPCError("account_probe_error", "flow_http_identity")
    try:
        spec = await refresher.manager.spec(token.email, existing_only=True)
        identity = EnvironmentSnapshot.from_json(
            (spec.profile_dir / "environment_snapshot.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError):
        raise FlowRPCError("account_probe_error", "flow_http_identity") from None
    before = token.google_cookies
    if not GoogleCookieJar(before).header("https://flow.google.com/"):
        raise FlowRPCError("login_required", "flow_bootstrap")
    async with snapshot_http_request(before, asdict(identity), proxy) as context:
        try:
            yield context
        finally:
            if persist:
                await save_session_cookies(tokens, token, before, await context.cookies())
