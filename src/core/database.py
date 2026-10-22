"""Database storage layer for lay4flow"""

import asyncio
import aiosqlite
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional, List, Dict, Any
from pathlib import Path
from .models import (
    Token,
    TokenStats,
    Task,
    RequestLog,
    AdminConfig,
    ProxyConfig,
    GenerationConfig,
    CacheConfig,
    Project,
    CaptchaConfig,
    CallLogicConfig,
    TokenRefreshConfig,
)
from .models import DebugConfig
from .storage.accounts import AccountRepository
from .storage.generation_history import GenerationHistoryRepository
from .storage.schema import SchemaManager
from .storage.settings import SettingsRepository


class Database:
    """SQLite database manager"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Store database in data directory
            data_dir = Path(__file__).parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "flow.db")
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._connect_timeout = 30
        self._busy_timeout_ms = 30000
        self._generation_history = GenerationHistoryRepository(self)
        self._schema = SchemaManager(self)
        self._settings = SettingsRepository(self)
        self._accounts = AccountRepository(self)

    def db_exists(self) -> bool:
        """Check if database file exists"""
        return Path(self.db_path).exists()

    async def _configure_connection(self, db):
        """Apply SQLite runtime settings for better concurrent behavior."""
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        await db.execute("PRAGMA foreign_keys = ON")

    def _current_stats_date(self) -> str:
        """Return the logical date used by daily token statistics."""
        return date.today().isoformat()

    @asynccontextmanager
    async def _connect(self, *, write: bool = False):
        """Open a configured SQLite connection and optionally serialize writes."""
        if write:
            async with self._write_lock:
                async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                    await self._configure_connection(db)
                    yield db
            return

        async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
            await self._configure_connection(db)
            yield db

    async def _table_exists(self, db, table_name: str) -> bool:
        """Check if a table exists in the database"""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        )
        result = await cursor.fetchone()
        return result is not None

    async def _column_exists(self, db, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        try:
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = await cursor.fetchall()
            return any(col[1] == column_name for col in columns)
        except:
            return False

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
                        If None, use default values instead of reading from TOML.
        """
        return await self._schema._ensure_config_rows(db, config_dict)

    async def check_and_migrate_db(self, config_dict: dict = None):
        """Check database integrity and perform migrations if needed

        This method is called during upgrade mode to:
        1. Create missing tables (if they don't exist)
        2. Add missing columns to existing tables
        3. Ensure all config tables have default rows

        Args:
            config_dict: Configuration dictionary from setting.toml (optional)
                        Used only to initialize missing config rows with default values.
                        Existing config rows will NOT be overwritten.
        """
        return await self._schema.check_and_migrate_db(config_dict)

    async def init_db(self):
        """Initialize database tables"""
        return await self._schema.init_db()

    async def _migrate_request_logs(self, db):
        """Migrate request_logs table from old schema to new schema"""
        return await self._schema._migrate_request_logs(db)

    # Token operations
    async def add_token(self, token: Token) -> int:
        """Add a new token"""
        return await self._accounts.add_token(token)

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        return await self._accounts.get_token(token_id)

    async def get_token_by_st(self, st: str) -> Optional[Token]:
        """Get token by ST"""
        return await self._accounts.get_token_by_st(st)

    async def get_token_by_email(self, email: str) -> Optional[Token]:
        """Get token by email"""
        return await self._accounts.get_token_by_email(email)

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        return await self._accounts.get_all_tokens()

    async def get_all_tokens_with_stats(self) -> List[Dict[str, Any]]:
        """Get all tokens with merged statistics in one query"""
        return await self._accounts.get_all_tokens_with_stats()

    async def get_dashboard_stats(self) -> Dict[str, int]:
        """Get dashboard counters with aggregated SQL queries"""
        return await self._accounts.get_dashboard_stats()

    async def get_system_info_stats(self) -> Dict[str, int]:
        """Get lightweight system counters used by admin dashboard"""
        return await self._accounts.get_system_info_stats()

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        return await self._accounts.get_active_tokens()

    async def update_token(self, token_id: int, **kwargs):
        """Update token fields"""
        return await self._accounts.update_token(token_id, **kwargs)

    async def update_token_if_credentials_match(
        self,
        token_id: int,
        expected: Dict[str, Any],
        *,
        expected_proxy_binding: tuple[int, str] | None = None,
        **kwargs,
    ) -> bool:
        """Conditionally update refresh state without replacing newer credentials."""
        return await self._accounts.update_token_if_credentials_match(
            token_id, expected, expected_proxy_binding=expected_proxy_binding, **kwargs
        )

    async def delete_token(self, token_id: int):
        """Delete token and related data"""
        return await self._accounts.delete_token(token_id)

    # Project operations
    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        return await self._accounts.add_project(project)

    async def add_project_if_credentials_match(
        self,
        project: Project,
        expected: Dict[str, Any],
        *,
        expected_proxy_binding: tuple[int, str] | None = None,
    ) -> Optional[int]:
        """Atomically retain the account and fixed-proxy binding of a project."""
        return await self._accounts.add_project_if_credentials_match(
            project, expected, expected_proxy_binding=expected_proxy_binding
        )

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get project by UUID"""
        return await self._accounts.get_project_by_id(project_id)

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """Get all projects for a token"""
        return await self._accounts.get_projects_by_token(token_id)

    async def delete_project(self, project_id: str):
        """Delete project"""
        return await self._accounts.delete_project(project_id)

    # Task operations
    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        return await self._generation_history.create_task(task)

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        return await self._generation_history.get_task(task_id)

    async def update_task(self, task_id: str, **kwargs):
        """Update task"""
        return await self._generation_history.update_task(task_id, **kwargs)

    # Token stats operations (kept for compatibility, now delegates to specific methods)
    async def increment_token_stats(self, token_id: int, stat_type: str):
        """Increment token statistics (delegates to specific methods)"""
        if stat_type == "image":
            await self.increment_image_count(token_id)
        elif stat_type == "video":
            await self.increment_video_count(token_id)
        elif stat_type == "error":
            await self.increment_error_count(token_id)

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        return await self._accounts.get_token_stats(token_id)

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        return await self._accounts.increment_image_count(token_id)

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        return await self._accounts.increment_video_count(token_id)

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset

        Updates two counters:
        - error_count: Historical total errors (never reset)
        - consecutive_error_count: Consecutive errors (reset on success/enable)
        - today_error_count: Today's errors (reset on date change)
        """
        return await self._accounts.increment_error_count(token_id)

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (only reset consecutive_error_count, keep error_count and today_error_count)

        This is called when:
        - Token is manually enabled by admin
        - Request succeeds (resets consecutive error counter)

        Note: error_count (total historical errors) is NEVER reset
        """
        return await self._accounts.reset_error_count(token_id)

    # Config operations
    async def get_admin_config(self) -> Optional[AdminConfig]:
        """Get admin configuration"""
        return await self._settings.get_admin_config()

    async def update_admin_config(self, **kwargs):
        """Update admin configuration"""
        return await self._settings.update_admin_config(**kwargs)

    async def get_proxy_config(self) -> Optional[ProxyConfig]:
        """Get proxy configuration"""
        return await self._settings.get_proxy_config()

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str] = None,
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None,
    ):
        """Update proxy configuration"""
        return await self._settings.update_proxy_config(
            enabled, proxy_url, media_proxy_enabled, media_proxy_url
        )

    async def list_proxy_pool(self) -> list:
        """List proxy pool entries."""
        return await self._settings.list_proxy_pool()

    async def get_proxy_pool_entry(self, entry_id: int):
        """Get a proxy pool entry."""
        return await self._settings.get_proxy_pool_entry(entry_id)

    async def add_proxy_pool_entry(
        self, proxy_url: str, note: str = "", captcha_enabled: bool = True
    ) -> int:
        """Add a proxy pool entry."""
        return await self._settings.add_proxy_pool_entry(proxy_url, note, captcha_enabled)

    async def ensure_account_proxy_entry(self, proxy_url: str) -> tuple[int, bool]:
        return await self._settings.ensure_account_proxy_entry(proxy_url)

    async def remove_proxy_pool_entry(self, entry_id: int):
        """Remove a proxy pool entry."""
        return await self._settings.remove_proxy_pool_entry(entry_id)

    async def update_proxy_pool_entry(
        self, entry_id: int, proxy_url: str, note: str, captcha_enabled: bool | None = None
    ):
        """Update one proxy pool entry while retaining its ID."""
        return await self._settings.update_proxy_pool_entry(
            entry_id, proxy_url, note, captcha_enabled
        )

    async def set_proxy_pool_enabled(self, entry_id: int, enabled: bool):
        """Enable or disable a proxy pool entry."""
        return await self._settings.set_proxy_pool_enabled(entry_id, enabled)

    async def update_proxy_pool_check(
        self, entry_id: int, ok: bool, expected_proxy_url: Optional[str] = None
    ):
        """Record a proxy pool health check."""
        return await self._settings.update_proxy_pool_check(entry_id, ok, expected_proxy_url)

    async def record_proxy_pool_result(
        self, entry_id: int, expected_proxy_url: str, ok: bool, *, probe: bool = False
    ):
        return await self._settings.record_proxy_pool_result(
            entry_id, expected_proxy_url, ok, probe=probe
        )

    async def get_generation_config(self) -> Optional[GenerationConfig]:
        """Get generation configuration"""
        return await self._settings.get_generation_config()

    async def update_generation_config(
        self,
        image_timeout: Optional[int] = None,
        video_timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        """Update generation configuration"""
        return await self._settings.update_generation_config(
            image_timeout, video_timeout, max_retries
        )

    async def get_call_logic_config(self) -> CallLogicConfig:
        """Get token call logic configuration."""
        return await self._settings.get_call_logic_config()

    async def update_call_logic_config(self, call_mode: str):
        """Update token call logic configuration."""
        return await self._settings.update_call_logic_config(call_mode)

    # Request log operations
    async def add_request_log(self, log: RequestLog) -> int:
        """Add request log and return log id"""
        return await self._generation_history.add_request_log(log)

    async def update_request_log(self, log_id: int, **kwargs):
        """Update an existing request log row."""
        return await self._generation_history.update_request_log(log_id, **kwargs)

    async def get_logs(
        self, limit: int = 100, token_id: Optional[int] = None, include_payload: bool = False
    ):
        """Get request logs with token info, optionally including payload fields"""
        return await self._generation_history.get_logs(limit, token_id, include_payload)

    async def get_log_detail(self, log_id: int) -> Optional[Dict[str, Any]]:
        """Get single request log detail including payload fields"""
        return await self._generation_history.get_log_detail(log_id)

    async def clear_all_logs(self):
        """Clear all request logs"""
        return await self._generation_history.clear_all_logs()

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, initialize all config rows from setting.toml.
                            If False (upgrade mode), only ensure missing config rows exist with default values.
        """
        async with self._connect(write=True) as db:
            if is_first_startup:
                # First startup: Initialize all config tables with values from setting.toml
                await self._ensure_config_rows(db, config_dict)
            else:
                # Upgrade mode: Only ensure missing config rows exist (with default values, not from TOML)
                await self._ensure_config_rows(db, config_dict=None)

            await db.commit()

    async def reload_config_to_memory(self):
        """
        Reload all configuration from database to in-memory Config instance.
        This should be called after any configuration update to ensure hot-reload.

        Includes:
        - Admin config (username, password, api_key)
        - Cache config (enabled, timeout, base_url)
        - Generation config (image_timeout, video_timeout)
        - Proxy config will be handled by ProxyManager
        """
        from .config import config

        # Reload admin config
        admin_config = await self.get_admin_config()
        if admin_config:
            config.set_admin_username_from_db(admin_config.username)
            config.set_admin_password_from_db(admin_config.password)
            config.api_key = admin_config.api_key

        # Reload cache config
        cache_config = await self.get_cache_config()
        if cache_config:
            config.set_cache_enabled(cache_config.cache_enabled)
            config.set_cache_timeout(cache_config.cache_timeout)
            config.set_cache_base_url(cache_config.cache_base_url or "")

        # Reload generation config
        generation_config = await self.get_generation_config()
        if generation_config:
            config.set_image_timeout(generation_config.image_timeout)
            config.set_video_timeout(generation_config.video_timeout)
            config.set_flow_max_retries(generation_config.max_retries)

        # Reload call logic config
        call_logic_config = await self.get_call_logic_config()
        if call_logic_config:
            config.set_call_logic_mode(call_logic_config.call_mode)

        # Reload debug config
        debug_config = await self.get_debug_config()
        if debug_config:
            config.set_debug_enabled(debug_config.enabled)

        # Remote headed captcha is the sole generation solver.
        captcha_config = await self.get_captcha_config()
        if captcha_config:
            config.set_captcha_method("remote_browser")
            config.set_remote_browser_base_url(captcha_config.remote_browser_base_url)
            config.set_remote_browser_api_key(captcha_config.remote_browser_api_key)
            config.set_remote_browser_timeout(captcha_config.remote_browser_timeout)

    # Cache config operations
    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        return await self._settings.get_cache_config()

    async def update_cache_config(
        self, enabled: bool = None, timeout: int = None, base_url: Optional[str] = None
    ):
        """Update cache configuration"""
        return await self._settings.update_cache_config(enabled, timeout, base_url)

    # Debug config operations
    async def get_debug_config(self) -> "DebugConfig":
        """Get debug configuration"""
        return await self._settings.get_debug_config()

    async def update_debug_config(
        self,
        enabled: bool = None,
        log_requests: bool = None,
        log_responses: bool = None,
        mask_token: bool = None,
    ):
        """Update debug configuration"""
        return await self._settings.update_debug_config(
            enabled, log_requests, log_responses, mask_token
        )

    # Captcha config operations
    async def get_captcha_config(self) -> CaptchaConfig:
        """Get captcha configuration"""
        return await self._settings.get_captcha_config()

    async def update_captcha_config(
        self,
        captcha_method: str | None = None,
        remote_browser_base_url: str | None = None,
        remote_browser_api_key: str | None = None,
        remote_browser_timeout: int | None = None,
    ):
        return await self._settings.update_captcha_config(
            captcha_method=captcha_method,
            remote_browser_base_url=remote_browser_base_url,
            remote_browser_api_key=remote_browser_api_key,
            remote_browser_timeout=remote_browser_timeout,
        )

    async def get_token_refresh_config(self) -> TokenRefreshConfig:
        """Get protocol ST refresh configuration"""
        return await self._settings.get_token_refresh_config()

    async def update_token_refresh_config(
        self,
        enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
    ) -> TokenRefreshConfig:
        """Update protocol ST refresh configuration."""
        return await self._settings.update_token_refresh_config(enabled, refresh_interval_minutes)
