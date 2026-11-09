"""Import a login captured by the optional browser account-import extension.

The extension only supplies scoped Google cookies and a browser fingerprint
snapshot.  The server validates that material through the current Flow RPCs
on the selected fixed proxy before handing it to :class:`ProfileTransfer`.
No legacy ST/NextAuth exchange is performed here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
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
from .profile_transfer import ProfileTransfer, ProfileTransferError, _runtime

_COOKIE_LIMIT = 524_288
_IDENTITY_LIMIT = 2048


class ExtensionLogin:
    """Validate and persist one extension-captured Google login."""

    def __init__(self, token_manager: Any):
        self.tokens = token_manager

    def _transfer(self):
        transfer = getattr(self.tokens, "_profile_transfer", None)
        if transfer is None:
            transfer = ProfileTransfer(self.tokens)
            self.tokens._profile_transfer = transfer
        return transfer

    async def retained_proxy_binding(self, email: str) -> dict[str, Any] | None:
        """Recover the exact exit of an unfinished, identity-owned Profile."""
        transfer = self._transfer()
        manager = transfer.refresher.manager
        normalized = manager._normalize_account_key(email)
        directory = manager.root_dir / normalized
        if not directory.exists():
            return None
        try:
            if directory.is_symlink() or directory.resolve().parent != manager.root_dir.resolve():
                raise ValueError
            if any(
                (directory / name).is_symlink()
                for name in (
                    "metadata.json",
                    "profile_transfer.json",
                    "environment_snapshot.json",
                    "launch_settings.sha256",
                )
            ):
                raise ValueError
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            sidecar = json.loads((directory / "profile_transfer.json").read_text(encoding="utf-8"))
            if (
                not isinstance(metadata, dict)
                or not isinstance(sidecar, dict)
                or metadata.get("account_key") != normalized
                or metadata.get("state") not in {"starting", "stopped", "blocked", "error"}
                or metadata.get("last_verified_at")
                or sidecar.get("completed")
                or not isinstance(sidecar.get("state_imported"), bool)
            ):
                raise ValueError
            spec = manager._existing_spec(normalized)
            EnvironmentSnapshot.from_json(
                (directory / "environment_snapshot.json").read_text(encoding="utf-8")
            )
            stored_launch = (directory / "launch_settings.sha256").read_text(encoding="utf-8")
            launch = {"launch_options": transfer._launch_options()}
            matches = []
            for entry in await self.tokens.db.list_proxy_pool():
                proxy = normalize_proxy_url(entry.proxy_url)
                if not proxy:
                    continue
                binding = transfer._transfer_binding(
                    email, spec.fingerprint_seed, launch, proxy, entry.id
                )
                if binding == sidecar.get("binding_sha256"):
                    matches.append((entry, proxy))
            if len(matches) != 1 or not matches[0][0].enabled:
                raise ValueError
            entry, proxy = matches[0]
            options = manager.backend._launch_options(directory, proxy)

            def digest(value):
                return hashlib.sha256(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()

            accepted = {
                digest({key: value for key, value in options.items() if key != "headless"}),
                digest(options),
            }
            if isinstance(options.get("headless"), bool):
                accepted.add(digest({**options, "headless": not options["headless"]}))
            browser_proxy = options.get("proxy")
            if isinstance(browser_proxy, str) and browser_proxy.startswith("socks5://"):
                legacy = {**options, "proxy": "socks5h://" + browser_proxy[len("socks5://") :]}
                accepted.add(
                    digest({key: value for key, value in legacy.items() if key != "headless"})
                )
                accepted.add(digest(legacy))
                if isinstance(legacy.get("headless"), bool):
                    accepted.add(digest({**legacy, "headless": not legacy["headless"]}))
            if stored_launch not in accepted:
                raise ValueError
            return {"proxy_pool_id": entry.id, "proxy_url": proxy}
        except (OSError, TypeError, ValueError, AttributeError):
            raise ProfileTransferError(
                "profile_exists", "原 Profile 的身份或固定代理证据无法确认，现有资料已保留", 409
            ) from None

    async def prepare_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Select the account exit once, for encrypted durable queue storage."""
        selected_id, selected_proxy = await ProxyManager(self.tokens.db).select_account_proxy()
        return {
            **payload,
            "_extension_proxy_binding": {
                "proxy_pool_id": selected_id,
                "proxy_url": selected_proxy,
            },
        }

    async def import_prepared(
        self,
        payload: dict[str, Any],
        *,
        on_proxy_binding: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Consume only the server-prepared, durably encrypted exit snapshot."""
        binding = payload.get("_extension_proxy_binding")
        if (
            not isinstance(binding, dict)
            or type(binding.get("proxy_pool_id")) is not int
            or binding["proxy_pool_id"] < 1
            or not isinstance(binding.get("proxy_url"), str)
            or not binding["proxy_url"]
        ):
            raise ValueError("扩展登录任务缺少固定代理快照，请重试任务")
        return await self.import_login(
            payload,
            proxy_pool_id=binding["proxy_pool_id"],
            proxy_url=binding["proxy_url"],
            on_proxy_binding=on_proxy_binding,
        )

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
        on_proxy_binding: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
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

        transfer = self._transfer()
        existing = await transfer.existing_account(email)
        if existing is not None:
            existing_proxy = await transfer.refresher._proxy_url(existing)
            if existing_proxy != selected_proxy:
                binding = {"proxy_pool_id": existing.proxy_pool_id, "proxy_url": existing_proxy}
                if on_proxy_binding:
                    await on_proxy_binding(binding)
            async with transfer._import_lock:
                result = await transfer.update_existing(existing, _browser_cookie_rows(rows))
            return {
                "success": True,
                "action": "updated",
                "email": result["email"],
                "token_id": result["token_id"],
            }
        retained = await self.retained_proxy_binding(email)
        if retained is not None and (
            retained["proxy_pool_id"] != selected_id or retained["proxy_url"] != selected_proxy
        ):
            selected_id, selected_proxy = await ProxyManager(self.tokens.db).select_account_proxy(
                retained["proxy_pool_id"], retained["proxy_url"]
            )
            if selected_proxy != retained["proxy_url"] or selected_id != retained["proxy_pool_id"]:
                raise ValueError("原 Profile 固定代理已变化，现有资料已保留")
            if on_proxy_binding is not None:
                # The trusted queue commits this corrected binding before the
                # first request on the retained exit, including failed retries.
                await on_proxy_binding(retained)
            # The discovery exit cannot prove this login on its retained exit.
            # Recheck the same captured material there before touching Profile.
            async with snapshot_http_request(raw_cookies, identity, selected_proxy) as context:
                session = await read_flow_session(context)
                rows = await context.cookies()
            observed = str(session.get("email") or "").strip().casefold()
            if not same_google_identity(observed, email):
                raise ValueError("原 Profile 固定代理验证出的 Google 账号不一致")

        transfer = self._transfer()
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
        options = {"repair_existing_login": True} if retained is not None else {}
        result = await transfer.import_bundle(
            bundle, proxy_url=selected_proxy, proxy_pool_id=selected_id, **options
        )
        return {
            "success": True,
            "action": "added",
            "email": result.get("email", email),
            "token_id": result.get("token_id"),
        }


__all__ = ["ExtensionLogin"]
