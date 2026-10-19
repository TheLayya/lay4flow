"""Proxy configuration manager."""

from typing import Optional

from ..core.database import Database
from ..core.models import ProxyConfig
from .proxy_urls import normalize_proxy_url


class AccountProxyUnavailable(ValueError):
    """A fixed binding failed validation; its request must keep that exit."""

    def __init__(self):
        super().__init__("账号绑定出口不可用，请在代理池检查启用状态与账号绑定")


class ProxyManager:
    """Read and update request/media proxy configuration."""

    def __init__(self, db: Database):
        self.db = db
        self._media_last_id = 0

    def _parse_proxy_line(self, line: str) -> Optional[str]:
        """Compatibility alias for the old parser entry point."""
        try:
            return normalize_proxy_url(line)
        except ValueError:
            return None

    def normalize_proxy_url(self, proxy_url: Optional[str]) -> Optional[str]:
        """Normalize a proxy URL, raising ``ValueError`` for invalid input."""
        try:
            return normalize_proxy_url(proxy_url)
        except ValueError as exc:
            raise ValueError(
                "代理地址格式错误，支持示例："
                "http://user:pass@host:port / "
                "socks5://user:pass@host:port / "
                "host:port:user:pass / st5 host:port:user:pass"
            ) from exc

    async def get_proxy_url(self) -> Optional[str]:
        """Compatibility alias returning the request proxy URL."""
        return await self.get_request_proxy_url()

    async def get_request_proxy_url(self) -> Optional[str]:
        """Return the configured request proxy when enabled."""
        config = await self.db.get_proxy_config()
        if config and config.enabled:
            if not config.proxy_url or not config.proxy_url.strip():
                raise ValueError("request proxy is enabled but not configured")
            return self.normalize_proxy_url(config.proxy_url)
        return None

    async def get_media_proxy_url(self) -> Optional[str]:
        """Return the media proxy, falling back to the request proxy."""
        config = await self.db.get_proxy_config()
        if config and config.media_proxy_enabled:
            if not config.media_proxy_url or not config.media_proxy_url.strip():
                raise ValueError("media proxy is enabled but not configured")
            return self.normalize_proxy_url(config.media_proxy_url)
        return await self.get_request_proxy_url()

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str],
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None,
    ):
        """Normalize and persist request/media proxy settings."""
        if enabled and (not proxy_url or not proxy_url.strip()):
            raise ValueError("request proxy is enabled but not configured")
        if media_proxy_enabled and media_proxy_url is not None and not media_proxy_url.strip():
            raise ValueError("media proxy is enabled but not configured")
        normalized_proxy_url = self.normalize_proxy_url(proxy_url)
        normalized_media_proxy_url = (
            self.normalize_proxy_url(media_proxy_url) if media_proxy_url is not None else None
        )
        await self.db.update_proxy_config(
            enabled=enabled,
            proxy_url=normalized_proxy_url,
            media_proxy_enabled=media_proxy_enabled,
            media_proxy_url=normalized_media_proxy_url,
        )

    async def get_proxy_config(self) -> ProxyConfig:
        """Return the complete proxy configuration."""
        return await self.db.get_proxy_config()

    async def list_proxy_pool(self) -> list:
        """List all proxy pool entries."""
        return await self.db.list_proxy_pool()

    async def add_proxy_pool_entry(
        self, proxy_url: str, note: str = "", captcha_enabled: bool = True
    ) -> int:
        """Normalize and add a proxy pool entry; returns its id."""
        normalized = self.normalize_proxy_url(proxy_url)
        if captcha_enabled:
            return await self.db.add_proxy_pool_entry(normalized, note)
        return await self.db.add_proxy_pool_entry(normalized, note, captcha_enabled=False)

    async def remove_proxy_pool_entry(self, entry_id: int) -> None:
        """Remove a proxy pool entry."""
        await self.db.remove_proxy_pool_entry(entry_id)

    async def update_proxy_pool_entry(
        self,
        entry_id: int,
        proxy_url: Optional[str] = None,
        note: Optional[str] = None,
        captcha_enabled: bool | None = None,
    ) -> None:
        """Normalize editable fields and retain the pool row's identity."""
        current = await self.db.get_proxy_pool_entry(entry_id)
        if current is None:
            raise ValueError("代理不存在")
        normalized = current.proxy_url if proxy_url is None else self.normalize_proxy_url(proxy_url)
        if not normalized:
            raise ValueError("代理地址不能为空")
        args = (entry_id, normalized, current.note if note is None else note.strip())
        updated = (
            await self.db.update_proxy_pool_entry(*args)
            if captcha_enabled is None
            else await self.db.update_proxy_pool_entry(*args, captcha_enabled=captcha_enabled)
        )
        if not updated:
            raise ValueError("代理不存在")

    async def set_proxy_pool_enabled(self, entry_id: int, enabled: bool) -> None:
        """Enable or disable a proxy pool entry."""
        await self.db.set_proxy_pool_enabled(entry_id, enabled)

    async def check_proxy_pool_entry(self, entry_id: int) -> bool:
        """Test a proxy pool entry against Google and record the result."""
        entry = await self.db.get_proxy_pool_entry(entry_id)
        if entry is None:
            return False
        proxy = self.normalize_proxy_url(entry.proxy_url)
        try:
            from curl_cffi.requests import AsyncSession

            async with AsyncSession(trust_env=False, proxy=proxy, timeout=15) as session:
                response = await session.get("https://www.google.com/recaptcha/enterprise/anchor")
            ok = response.status_code == 200
        except Exception:
            ok = False
        await self.db.update_proxy_pool_check(entry_id, ok, expected_proxy_url=entry.proxy_url)
        return ok

    async def get_enabled_proxy_urls(self) -> list[str]:
        """Return the enabled proxy pool URLs, used as mint egress sources."""
        entries = await self.list_proxy_pool()
        return [
            entry.proxy_url
            for entry in entries
            if entry.enabled and getattr(entry, "captcha_enabled", True)
        ]

    async def select_account_proxy(
        self, proxy_pool_id: int | None = None, proxy_url: str | None = None
    ) -> tuple[int, str]:
        """Select once for persistence; existing accounts resolve their saved binding."""
        if proxy_pool_id is not None:
            if isinstance(proxy_pool_id, bool) or not isinstance(proxy_pool_id, int):
                raise AccountProxyUnavailable()
            entry = await self.db.get_proxy_pool_entry(proxy_pool_id)
            if entry is None or not entry.enabled:
                raise AccountProxyUnavailable()
            try:
                normalized = normalize_proxy_url(entry.proxy_url)
            except ValueError:
                raise AccountProxyUnavailable() from None
            if not normalized:
                raise AccountProxyUnavailable()
            return entry.id, normalized
        if proxy_url and proxy_url.strip():
            try:
                normalized = normalize_proxy_url(proxy_url)
            except ValueError:
                raise AccountProxyUnavailable() from None
            entry_id, enabled = await self.db.ensure_account_proxy_entry(normalized)
            if not enabled:
                raise AccountProxyUnavailable()
            return entry_id, normalized
        entries = [
            entry
            for entry in await self.list_proxy_pool()
            if entry.enabled and entry.last_check_ok is not False
        ]
        if not entries:
            raise AccountProxyUnavailable()
        selected = min(entries, key=lambda entry: (entry.bound_account_count, entry.id))
        return await self.select_account_proxy(selected.id)

    async def resolve_account_proxy(self, token) -> str | None:
        """Resolve fixed account egress, rejecting broken bindings without fallback."""
        entry_id = getattr(token, "proxy_pool_id", None)
        if entry_id is not None:
            _, proxy = await self.select_account_proxy(entry_id)
            try:
                snapshot = normalize_proxy_url(getattr(token, "proxy_url", None))
            except ValueError:
                raise AccountProxyUnavailable() from None
            if proxy != snapshot:
                raise AccountProxyUnavailable()
            return proxy
        try:
            own_proxy = normalize_proxy_url(getattr(token, "proxy_url", None))
            return own_proxy or await self.get_request_proxy_url()
        except ValueError:
            raise AccountProxyUnavailable() from None

    async def select_media_proxy(self) -> str:
        """Distribute independent downloads over usable exits from the shared pool."""
        entries = sorted(
            (
                entry
                for entry in await self.list_proxy_pool()
                if entry.enabled and entry.last_check_ok is not False
            ),
            key=lambda entry: entry.id,
        )
        if not entries:
            raise ValueError("代理池没有可用的媒体出口，请检查代理池")
        selected = next((entry for entry in entries if entry.id > self._media_last_id), entries[0])
        proxy = normalize_proxy_url(selected.proxy_url)
        if not proxy:
            raise ValueError("代理池没有可用的媒体出口，请检查代理池")
        self._media_last_id = selected.id
        return proxy
