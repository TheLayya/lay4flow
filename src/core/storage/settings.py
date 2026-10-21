"""Configuration persistence behind Database's existing methods and connection policy."""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import aiosqlite

from ..config import REMOTE_BROWSER_BASE_URL
from ..models import (
    AdminConfig,
    CacheConfig,
    CallLogicConfig,
    CaptchaConfig,
    DebugConfig,
    GenerationConfig,
    ProxyConfig,
    ProxyPoolEntry,
    TokenRefreshConfig,
)

if TYPE_CHECKING:
    from ..database import Database


class SettingsRepository:
    """Keep each setting's existing update rules and transaction boundaries."""

    def __init__(self, database: "Database"):
        self._database = database

    async def get_admin_config(self) -> Optional[AdminConfig]:
        """Get admin configuration"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return AdminConfig(**dict(row))
            return None

    async def update_admin_config(self, **kwargs):
        """Update admin configuration"""
        async with self._database._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                query = f"UPDATE admin_config SET {', '.join(updates)} WHERE id = 1"
                await db.execute(query, params)
                await db.commit()

    async def get_proxy_config(self) -> Optional[ProxyConfig]:
        """Get proxy configuration"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return ProxyConfig(**dict(row))
            return None

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str] = None,
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None,
    ):
        """Update proxy configuration"""
        async with self._database._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_media_proxy_enabled = (
                    media_proxy_enabled
                    if media_proxy_enabled is not None
                    else current.get("media_proxy_enabled", False)
                )
                new_media_proxy_url = (
                    media_proxy_url
                    if media_proxy_url is not None
                    else current.get("media_proxy_url")
                )

                await db.execute(
                    """
                    UPDATE proxy_config
                    SET enabled = ?, proxy_url = ?,
                        media_proxy_enabled = ?, media_proxy_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url),
                )
            else:
                new_media_proxy_enabled = (
                    media_proxy_enabled if media_proxy_enabled is not None else False
                )
                new_media_proxy_url = media_proxy_url
                await db.execute(
                    """
                    INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                    VALUES (1, ?, ?, ?, ?)
                """,
                    (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url),
                )

            await db.commit()

    async def list_proxy_pool(self) -> list[ProxyPoolEntry]:
        """List all proxy pool entries."""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM tokens t WHERE t.proxy_pool_id = p.id) "
                "AS bound_account_count FROM proxy_pool p ORDER BY p.id"
            )
            rows = await cursor.fetchall()
            return [ProxyPoolEntry(**dict(row)) for row in rows]

    async def get_proxy_pool_entry(self, entry_id: int) -> Optional[ProxyPoolEntry]:
        """Get a single proxy pool entry."""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM tokens t WHERE t.proxy_pool_id = p.id) "
                "AS bound_account_count FROM proxy_pool p WHERE p.id = ?",
                (entry_id,),
            )
            row = await cursor.fetchone()
            return ProxyPoolEntry(**dict(row)) if row else None

    async def add_proxy_pool_entry(
        self, proxy_url: str, note: str = "", captcha_enabled: bool = True
    ) -> int:
        """Add a proxy pool entry; returns the new id."""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute(
                "INSERT INTO proxy_pool (proxy_url, enabled, note, captcha_enabled) VALUES (?, 1, ?, ?)",
                (proxy_url, note, captcha_enabled),
            )
            await db.commit()
            return cursor.lastrowid

    async def remove_proxy_pool_entry(self, entry_id: int) -> None:
        """Delete a proxy pool entry."""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute(
                "SELECT 1 FROM tokens WHERE proxy_pool_id = ? LIMIT 1", (entry_id,)
            )
            if await cursor.fetchone():
                raise ValueError("该出口已绑定账号，请先迁移账号绑定再删除")
            await db.execute("DELETE FROM proxy_pool WHERE id = ?", (entry_id,))
            await db.commit()

    async def update_proxy_pool_entry(
        self, entry_id: int, proxy_url: str, note: str, captcha_enabled: bool | None = None
    ) -> bool:
        """Keep the row identity and reset stale health only when its URL changes."""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute("SELECT proxy_url FROM proxy_pool WHERE id = ?", (entry_id,))
            row = await cursor.fetchone()
            if row is None:
                return False
            if row[0] != proxy_url:
                cursor = await db.execute(
                    "SELECT 1 FROM tokens WHERE proxy_pool_id = ? LIMIT 1", (entry_id,)
                )
                if await cursor.fetchone():
                    raise ValueError("该出口已绑定账号，请先迁移账号绑定再修改地址")
                await db.execute(
                    """
                    UPDATE proxy_pool
                    SET proxy_url = ?, note = ?, last_check_ok = NULL, last_check_at = NULL,
                        success_count = 0, failure_count = 0, consecutive_failures = 0,
                        success_rate = 0.5, cooldown_until = NULL
                    WHERE id = ?
                    """,
                    (proxy_url, note, entry_id),
                )
            else:
                await db.execute("UPDATE proxy_pool SET note = ? WHERE id = ?", (note, entry_id))
            if captcha_enabled is not None:
                await db.execute(
                    "UPDATE proxy_pool SET captcha_enabled = ? WHERE id = ?",
                    (captcha_enabled, entry_id),
                )
            await db.commit()
            return True

    async def ensure_account_proxy_entry(self, proxy_url: str) -> tuple[int, bool]:
        """Register a legacy account exit without adding a new captcha source."""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute(
                "SELECT id, enabled FROM proxy_pool WHERE proxy_url = ? ORDER BY id LIMIT 1",
                (proxy_url,),
            )
            row = await cursor.fetchone()
            if row is not None:
                return int(row[0]), bool(row[1])
            cursor = await db.execute(
                "INSERT INTO proxy_pool (proxy_url, captcha_enabled, note) VALUES (?, 0, ?)",
                (proxy_url, "原账号出口"),
            )
            await db.commit()
            return cursor.lastrowid, True

    async def set_proxy_pool_enabled(self, entry_id: int, enabled: bool) -> None:
        """Enable or disable a proxy pool entry."""
        async with self._database._connect(write=True) as db:
            await db.execute(
                "UPDATE proxy_pool SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, entry_id),
            )
            await db.commit()

    async def update_proxy_pool_check(
        self, entry_id: int, ok: bool, expected_proxy_url: Optional[str] = None
    ) -> None:
        """Record health only for the URL that was actually checked, when provided."""
        async with self._database._connect(write=True) as db:
            query = (
                "UPDATE proxy_pool SET last_check_ok = ?, last_check_at = CURRENT_TIMESTAMP "
                "WHERE id = ?"
            )
            params = [1 if ok else 0, entry_id]
            if expected_proxy_url is not None:
                query += " AND proxy_url = ?"
                params.append(expected_proxy_url)
            await db.execute(query, params)
            await db.commit()

    async def record_proxy_pool_result(
        self, entry_id: int, expected_proxy_url: str, ok: bool, *, probe: bool = False
    ) -> None:
        """Keep health across restarts, without disabling an exit after transient failure."""
        async with self._database._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM proxy_pool WHERE id = ? AND proxy_url = ?",
                (entry_id, expected_proxy_url),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            failures = 0 if ok else row["consecutive_failures"] + 1
            cooldown = None if ok else row["cooldown_until"]
            if failures >= 3 and not ok:
                seconds = min(300, 30 * 2 ** min(failures - 3, 4))
                cooldown = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            success_count = row["success_count"] + int(ok and not probe)
            failure_count = row["failure_count"] + int(not ok and not probe)
            rate = row["success_rate"] if probe else row["success_rate"] * 0.8 + int(ok) * 0.2
            await db.execute(
                "UPDATE proxy_pool SET success_count = ?, failure_count = ?, "
                "consecutive_failures = ?, success_rate = ?, cooldown_until = ?, "
                "last_check_ok = ?, last_check_at = ? WHERE id = ? AND proxy_url = ?",
                (
                    success_count,
                    failure_count,
                    failures,
                    rate,
                    cooldown,
                    int(ok),
                    datetime.now(timezone.utc).isoformat(),
                    entry_id,
                    expected_proxy_url,
                ),
            )
            await db.commit()

    async def get_generation_config(self) -> Optional[GenerationConfig]:
        """Get generation configuration"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return GenerationConfig(**dict(row))
            return None

    async def update_generation_config(
        self,
        image_timeout: Optional[int] = None,
        video_timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        """Update generation configuration"""
        async with self._database._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            current = dict(row) if row else {}

            normalized_image_timeout = (
                image_timeout if image_timeout is not None else current.get("image_timeout", 300)
            )
            normalized_video_timeout = (
                video_timeout if video_timeout is not None else current.get("video_timeout", 1500)
            )
            try:
                normalized_max_retries = (
                    max(1, int(max_retries))
                    if max_retries is not None
                    else max(1, int(current.get("max_retries", 3)))
                )
            except Exception:
                normalized_max_retries = 3

            if row:
                await db.execute(
                    """
                    UPDATE generation_config
                    SET image_timeout = ?, video_timeout = ?, max_retries = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (normalized_image_timeout, normalized_video_timeout, normalized_max_retries),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries)
                    VALUES (1, ?, ?, ?)
                """,
                    (normalized_image_timeout, normalized_video_timeout, normalized_max_retries),
                )
            await db.commit()

    async def get_call_logic_config(self) -> CallLogicConfig:
        """Get token call logic configuration."""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM call_logic_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                row_dict = dict(row)
                mode = row_dict.get("call_mode")
                if mode not in ("default", "polling"):
                    row_dict["call_mode"] = (
                        "polling" if row_dict.get("polling_mode_enabled") else "default"
                    )
                return CallLogicConfig(**row_dict)
            return CallLogicConfig(call_mode="default", polling_mode_enabled=False)

    async def update_call_logic_config(self, call_mode: str):
        """Update token call logic configuration."""
        normalized = "polling" if call_mode == "polling" else "default"
        polling_mode_enabled = normalized == "polling"
        async with self._database._connect(write=True) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO call_logic_config (id, call_mode, polling_mode_enabled, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            """,
                (normalized, polling_mode_enabled),
            )
            await db.commit()

    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CacheConfig(**dict(row))
            # Return default if not found
            return CacheConfig(cache_enabled=False, cache_timeout=7200)

    async def update_cache_config(
        self, enabled: bool = None, timeout: int = None, base_url: Optional[str] = None
    ):
        """Update cache configuration"""
        async with self._database._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = (
                    enabled if enabled is not None else current.get("cache_enabled", False)
                )
                new_timeout = timeout if timeout is not None else current.get("cache_timeout", 7200)
                new_base_url = base_url if base_url is not None else current.get("cache_base_url")

                # If base_url is explicitly set to empty string, treat as None
                if base_url == "":
                    new_base_url = None

                await db.execute(
                    """
                    UPDATE cache_config
                    SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (new_enabled, new_timeout, new_base_url),
                )
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_timeout = timeout if timeout is not None else 7200
                new_base_url = base_url if base_url is not None else None

                await db.execute(
                    """
                    INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                    VALUES (1, ?, ?, ?)
                """,
                    (new_enabled, new_timeout, new_base_url),
                )

            await db.commit()

    async def get_debug_config(self) -> "DebugConfig":
        """Get debug configuration"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return DebugConfig(**dict(row))
            # Return default if not found
            return DebugConfig(
                enabled=False, log_requests=True, log_responses=True, mask_token=True
            )

    async def update_debug_config(
        self,
        enabled: bool = None,
        log_requests: bool = None,
        log_responses: bool = None,
        mask_token: bool = None,
    ):
        """Update debug configuration"""
        async with self._database._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("enabled", False)
                new_log_requests = (
                    log_requests if log_requests is not None else current.get("log_requests", True)
                )
                new_log_responses = (
                    log_responses
                    if log_responses is not None
                    else current.get("log_responses", True)
                )
                new_mask_token = (
                    mask_token if mask_token is not None else current.get("mask_token", True)
                )

                await db.execute(
                    """
                    UPDATE debug_config
                    SET enabled = ?, log_requests = ?, log_responses = ?, mask_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (new_enabled, new_log_requests, new_log_responses, new_mask_token),
                )
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_log_requests = log_requests if log_requests is not None else True
                new_log_responses = log_responses if log_responses is not None else True
                new_mask_token = mask_token if mask_token is not None else True

                await db.execute(
                    """
                    INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                    VALUES (1, ?, ?, ?, ?)
                """,
                    (new_enabled, new_log_requests, new_log_responses, new_mask_token),
                )

            await db.commit()

    async def get_captcha_config(self) -> CaptchaConfig:
        """Get captcha configuration"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CaptchaConfig(
                    **{**dict(row), "remote_browser_base_url": REMOTE_BROWSER_BASE_URL}
                )
            return CaptchaConfig()

    async def update_captcha_config(
        self,
        captcha_method: str | None = None,
        remote_browser_base_url: str | None = None,
        remote_browser_api_key: str | None = None,
        remote_browser_timeout: int | None = None,
    ):
        if captcha_method not in (None, "remote_browser"):
            raise ValueError("This edition uses remote headed captcha only")
        updates = {
            "captcha_method": "remote_browser",
            "remote_browser_base_url": REMOTE_BROWSER_BASE_URL,
        }
        for key, value in (
            ("remote_browser_api_key", remote_browser_api_key),
            ("remote_browser_timeout", remote_browser_timeout),
        ):
            if value is not None:
                updates[key] = max(5, int(value)) if key.endswith("timeout") else value
        async with self._database._connect(write=True) as db:
            columns = ", ".join(updates)
            placeholders = ", ".join("?" for _ in updates)
            assignments = ", ".join(f"{key} = excluded.{key}" for key in updates)
            await db.execute(
                f"INSERT INTO captcha_config (id, {columns}) VALUES (1, {placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {assignments}, updated_at = CURRENT_TIMESTAMP",
                tuple(updates.values()),
            )
            await db.commit()

    async def get_token_refresh_config(self) -> TokenRefreshConfig:
        """Get protocol ST refresh configuration"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_refresh_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return TokenRefreshConfig(**dict(row))
            return TokenRefreshConfig()

    async def update_token_refresh_config(
        self,
        enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
    ) -> TokenRefreshConfig:
        """Update protocol ST refresh configuration."""
        async with self._database._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_refresh_config WHERE id = 1")
            row = await cursor.fetchone()
            current = dict(row) if row else {}
            new_enabled = enabled if enabled is not None else bool(current.get("enabled", True))
            try:
                new_interval = int(
                    refresh_interval_minutes
                    if refresh_interval_minutes is not None
                    else current.get("refresh_interval_minutes", 120)
                )
            except Exception:
                new_interval = 120
            new_interval = max(1, new_interval)

            if row:
                await db.execute(
                    """
                    UPDATE token_refresh_config
                    SET enabled = ?, refresh_interval_minutes = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (new_enabled, new_interval),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO token_refresh_config (id, enabled, refresh_interval_minutes)
                    VALUES (1, ?, ?)
                """,
                    (new_enabled, new_interval),
                )
            await db.commit()

        return await self._database.get_token_refresh_config()
