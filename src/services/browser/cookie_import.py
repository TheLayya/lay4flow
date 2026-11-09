"""Adapt external Google Cookie JSON to the existing verified Profile import."""

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ...core.config import config
from ..flow.google_cookies import GoogleCookieJar
from ..proxy_manager import ProxyManager
from .environment import EnvironmentSnapshot
from .profile_session_refresher import _browser_cookie_rows
from .profile_transfer import FORMAT, ProfileTransfer, ProfileTransferError, _runtime

MAX_COOKIE_BYTES = 512 * 1024


def is_cookie_import(value: Any) -> bool:
    return isinstance(value, dict) and not value.get("format") and "cookies" in value


class CookieProfileImport:
    """Create a local browser identity; credentials must prove their claimed email."""

    def __init__(self, transfer: ProfileTransfer):
        self.transfer = transfer

    async def import_account(
        self,
        payload: dict[str, Any],
        *,
        proxy_pool_id: int | None = None,
        proxy_url: str | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if not is_cookie_import(payload):
            raise ProfileTransferError(
                "invalid_cookie_import", "请选择包含 cookies 和 email 的账号 JSON"
            )
        email = payload.get("email")
        if (
            not isinstance(email, str)
            or not email.strip()
            or len(email) > 320
            or "@" not in email
            or any(ord(char) <= 32 for char in email.strip())
        ):
            raise ProfileTransferError("invalid_account", "Cookie JSON 需要提供待校验的账号 email")
        email = email.strip()
        raw = payload["cookies"]
        if not isinstance(raw, (dict, list)) or not raw:
            raise ProfileTransferError(
                "invalid_cookie_import", "cookies 必须是非空名称和值对象或 Cookie 列表"
            )
        if isinstance(raw, dict) and any(
            not isinstance(name, str) or not name or not isinstance(value, str) or not value
            for name, value in raw.items()
        ):
            raise ProfileTransferError("invalid_cookie_import", "Cookie 名称和值必须是非空文本")
        if isinstance(raw, list) and any(not isinstance(row, dict) for row in raw):
            raise ProfileTransferError("invalid_cookie_import", "Cookie 列表的每一项必须是对象")
        try:
            encoded = json.dumps(raw, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise ProfileTransferError("invalid_cookie_import", "Cookie JSON 无效") from None
        if len(encoded) > MAX_COOKIE_BYTES or len(raw) > 1000:
            raise ProfileTransferError(
                "cookie_input_too_large", "Cookie 资料过大，请缩减后重试", 413
            )
        jar = GoogleCookieJar(raw)
        rows = jar.export()
        if not rows or not jar.has_any(("SID", "OSID", "__Secure-OSID", "__Secure-1PSID")):
            raise ProfileTransferError("invalid_cookie_import", "未找到有效的 Google 登录 Cookie")
        transfer = self.transfer
        manager = transfer.refresher.manager
        existing = await transfer.existing_account(email)
        if existing is not None:
            async with transfer._import_lock:
                return await transfer.update_existing(
                    existing, _browser_cookie_rows(rows), on_stage=on_stage
                )

        # Input cannot choose proxy credentials, browser launch arguments or seed.
        # An unfinished import must retain the exact original exit on retry.
        from .extension_login import ExtensionLogin

        retained = await ExtensionLogin(transfer.tokens).retained_proxy_binding(email)
        if retained is not None:
            if (proxy_pool_id is not None and proxy_pool_id != retained["proxy_pool_id"]) or (
                proxy_url is not None
                and ProxyManager.normalize_proxy_url(proxy_url) != retained["proxy_url"]
            ):
                raise ProfileTransferError(
                    "proxy_unavailable", "请使用原导入任务的固定代理重试", 409
                )
            proxy_pool_id, proxy_url = retained["proxy_pool_id"], retained["proxy_url"]
            seed = manager._existing_spec(manager._normalize_account_key(email)).fingerprint_seed
        else:
            async with manager._seed_guard:
                seed = manager._allocate_seed(manager._normalize_account_key(email))

        launch = transfer._launch_options()
        # Provenance only: _check_context records the actual target browser
        # environment before any identity check; no source fingerprint is claimed.
        environment = EnvironmentSnapshot(
            str(launch.get("user_agent") or config.flow_user_agent),
            str(launch.get("locale") or "en-US"),
            (str(launch.get("locale") or "en-US"),),
            str(launch.get("timezone") or "UTC"),
            "target-runtime",
            1280,
            720,
        )
        bundle = {
            "format": FORMAT,
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "account": {
                "email": email,
                "remark": "Cookie JSON 导入",
                "is_active": True,
                "auto_refresh_enabled": True,
                "image_enabled": True,
                "video_enabled": True,
                "image_concurrency": -1,
                "video_concurrency": -1,
                "submission_proxy_mode": "fixed",
            },
            "browser": {
                "fingerprint_seed": seed,
                "launch_options": launch,
                "environment": json.loads(environment.to_json()),
                "runtime": {**_runtime(), "chromium": "cookie-import"},
            },
            "storage_state": {
                "cookies": [
                    {"expires": -1, "sameSite": "Lax", **row} for row in _browser_cookie_rows(rows)
                ],
                "origins": [],
            },
        }
        # External sid/projectId/tier are hints, never admission evidence.
        return await transfer.import_bundle(
            bundle, proxy_url=proxy_url, proxy_pool_id=proxy_pool_id, on_stage=on_stage
        )
