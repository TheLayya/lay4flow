"""Import a login captured by the optional browser account-import extension.

The extension only supplies scoped Google cookies and a browser fingerprint
snapshot.  The server validates that material through the current Flow RPCs
on the selected fixed proxy before handing it to :class:`ProfileTransfer`.
No legacy ST/NextAuth exchange is performed here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ...core.account_identity import same_google_identity
from ..flow.browser_request import snapshot_http_request
from ..flow.flow_rpc import read_flow_session
from ..flow.google_cookies import GoogleCookieJar
from ..proxy_manager import ProxyManager
from ..proxy_urls import normalize_proxy_url
from .environment import EnvironmentSnapshot
from .profile_session_refresher import _browser_cookie_rows
from .profile_transfer import ProfileTransfer, _runtime

_COOKIE_LIMIT = 524_288
_IDENTITY_LIMIT = 2048


class ExtensionLogin:
    """Validate and persist one extension-captured Google login."""

    def __init__(self, token_manager: Any):
        self.tokens = token_manager

    @staticmethod
    def _identity(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("扩展未提供有效的浏览器环境")
        user_agent = str(raw.get("user_agent") or raw.get("userAgent") or "").strip()
        language = str(raw.get("language") or "").strip()
        languages = raw.get("languages")
        if not isinstance(languages, (list, tuple)):
            languages = [language]
        languages = [str(value).strip() for value in languages if str(value).strip()]
        if language and language.casefold() not in {value.casefold() for value in languages}:
            languages.insert(0, language)
        if not user_agent or not language or not languages or len(user_agent) > _IDENTITY_LIMIT:
            raise ValueError("扩展未提供完整的浏览器环境")
        # Keep the same validation and representation used by persistent
        # Profile environments.  The extension cannot choose a fingerprint
        # seed or launch arguments.
        snapshot = EnvironmentSnapshot.from_probe(
            {
                "userAgent": user_agent,
                "language": language,
                "languages": languages[:10],
                "timezone": str(raw.get("timezone") or "UTC"),
                "platform": str(raw.get("platform") or "Win32"),
                "width": raw.get("viewport_width", raw.get("width", 1280)),
                "height": raw.get("viewport_height", raw.get("height", 800)),
            }
        )
        return json.loads(snapshot.to_json())

    async def import_login(
        self,
        payload: dict[str, Any],
        *,
        proxy_pool_id: int | None = None,
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("扩展登录数据格式无效")
        raw_cookies = payload.get("google_cookies")
        if not isinstance(raw_cookies, str) or not raw_cookies.strip():
            raise ValueError("扩展未提供 Google 登录 Cookie")
        if len(raw_cookies.encode("utf-8")) > _COOKIE_LIMIT:
            raise ValueError("Google 登录 Cookie 过大")
        identity = self._identity(payload.get("browser_identity") or payload.get("identity"))
        jar = GoogleCookieJar(raw_cookies)
        rows = jar.export()
        if not rows or not jar.has_any(("SID", "OSID", "__Secure-OSID", "__Secure-1PSID")):
            raise ValueError("Google 登录 Cookie 已失效或范围不受支持")

        selected_id, selected_proxy = await ProxyManager(self.tokens.db).select_account_proxy(
            proxy_pool_id, proxy_url
        )
        if (
            proxy_pool_id is not None
            and proxy_url
            and normalize_proxy_url(proxy_url) != selected_proxy
        ):
            raise ValueError("登录期间账号代理已变化，请重新选择代理后开始")
        # The same scoped Cookie jar and browser identity prove the account on
        # the exact exit that will be used by its new Profile.
        async with snapshot_http_request(raw_cookies, identity, selected_proxy) as context:
            session = await read_flow_session(context)
            rows = await context.cookies()
        email = str(session.get("email") or "").strip().casefold()
        if not email or "@" not in email:
            raise ValueError("无法从 Flow 登录态识别账号")
        hinted = str(payload.get("email") or "").strip().casefold()
        if hinted and not same_google_identity(hinted, email):
            raise ValueError("扩展登录账号与指定账号不一致")

        transfer = getattr(self.tokens, "_profile_transfer", None)
        if transfer is None:
            transfer = ProfileTransfer(self.tokens)
            self.tokens._profile_transfer = transfer
        manager = transfer.refresher.manager
        normalized = manager._normalize_account_key(email)
        async with manager._seed_guard:
            seed = manager._allocate_seed(normalized)
        launch = transfer._launch_options()
        browser_runtime = {**_runtime(), "chromium": "extension"}
        state = {
            "cookies": [
                {"expires": -1, "sameSite": "Lax", **row} for row in _browser_cookie_rows(rows)
            ],
            "origins": [],
        }
        account = {
            "email": email,
            "remark": str(payload.get("remark") or "扩展登录")[:500],
            "is_active": True,
            "auto_refresh_enabled": True,
            "image_enabled": True,
            "video_enabled": True,
            "image_concurrency": -1,
            "video_concurrency": -1,
            "submission_proxy_mode": "fixed",
        }
        bundle = {
            "format": "lay4flow-cloak-session",
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "account": account,
            "browser": {
                "fingerprint_seed": seed,
                "launch_options": launch,
                "environment": identity,
                "runtime": browser_runtime,
            },
            "storage_state": state,
        }
        result = await transfer.import_bundle(
            bundle, proxy_url=selected_proxy, proxy_pool_id=selected_id
        )
        return {
            "success": True,
            "action": "added",
            "email": result.get("email", email),
            "token_id": result.get("token_id"),
        }


__all__ = ["ExtensionLogin"]
