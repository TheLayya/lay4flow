"""SQLite initialization, legacy upgrades, and initial configuration rows.

The owning Database keeps connection/lock policy and compatibility callbacks.
Schema statements and their transaction boundaries remain in execution order.
"""

from typing import TYPE_CHECKING

from ..config import REMOTE_BROWSER_BASE_URL, UNCONFIGURED_API_KEY

if TYPE_CHECKING:
    from ..database import Database


class SchemaManager:
    """Concrete schema operations behind the existing Database entry points."""

    def __init__(self, database: "Database"):
        self._database = database

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
                        If None, use default values instead of reading from TOML.
        """
        # Ensure admin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM admin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            admin_username = "admin"
            admin_password = "admin"
            api_key = UNCONFIGURED_API_KEY
            error_ban_threshold = 3

            if config_dict:
                global_config = config_dict.get("global", {})
                admin_username = global_config.get("admin_username", "admin")
                admin_password = global_config.get("admin_password", "admin")
                api_key = global_config.get("api_key", UNCONFIGURED_API_KEY)

                admin_config = config_dict.get("admin", {})
                error_ban_threshold = admin_config.get("error_ban_threshold", 3)

            await db.execute(
                """
                INSERT INTO admin_config (id, username, password, api_key, error_ban_threshold)
                VALUES (1, ?, ?, ?, ?)
            """,
                (admin_username, admin_password, api_key, error_ban_threshold),
            )

        # Ensure proxy_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM proxy_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            proxy_enabled = False
            proxy_url = None
            media_proxy_enabled = False
            media_proxy_url = None

            if config_dict:
                proxy_config = config_dict.get("proxy", {})
                proxy_enabled = proxy_config.get("proxy_enabled", False)
                proxy_url = proxy_config.get("proxy_url", "")
                proxy_url = proxy_url if proxy_url else None
                media_proxy_enabled = proxy_config.get(
                    "media_proxy_enabled", proxy_config.get("image_io_proxy_enabled", False)
                )
                media_proxy_url = proxy_config.get(
                    "media_proxy_url", proxy_config.get("image_io_proxy_url", "")
                )
                media_proxy_url = media_proxy_url if media_proxy_url else None

            await db.execute(
                """
                INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                VALUES (1, ?, ?, ?, ?)
            """,
                (proxy_enabled, proxy_url, media_proxy_enabled, media_proxy_url),
            )

        # Ensure generation_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM generation_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            image_timeout = 300
            video_timeout = 1500
            max_retries = 3

            if config_dict:
                generation_config = config_dict.get("generation", {})
                flow_config = config_dict.get("flow", {})
                image_timeout = generation_config.get("image_timeout", 300)
                video_timeout = generation_config.get("video_timeout", 1500)
                max_retries = flow_config.get("max_retries", 3)

            try:
                max_retries = max(1, int(max_retries))
            except Exception:
                max_retries = 3

            await db.execute(
                """
                INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries)
                VALUES (1, ?, ?, ?)
            """,
                (image_timeout, video_timeout, max_retries),
            )

        # Ensure call_logic_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM call_logic_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            call_mode = "default"
            polling_mode_enabled = False

            if config_dict:
                call_logic_config = config_dict.get("call_logic", {})
                call_mode = call_logic_config.get("call_mode", "default")
                if call_mode not in ("default", "polling"):
                    polling_mode_enabled = call_logic_config.get("polling_mode_enabled", False)
                    call_mode = "polling" if polling_mode_enabled else "default"
                else:
                    polling_mode_enabled = call_mode == "polling"

            await db.execute(
                """
                INSERT INTO call_logic_config (id, call_mode, polling_mode_enabled)
                VALUES (1, ?, ?)
            """,
                (call_mode, polling_mode_enabled),
            )

        # Ensure cache_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM cache_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            cache_enabled = False
            cache_timeout = 7200
            cache_base_url = None

            if config_dict:
                cache_config = config_dict.get("cache", {})
                cache_enabled = cache_config.get("enabled", False)
                cache_timeout = cache_config.get("timeout", 7200)
                cache_base_url = cache_config.get("base_url", "")
                # Convert empty string to None
                cache_base_url = cache_base_url if cache_base_url else None

            await db.execute(
                """
                INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                VALUES (1, ?, ?, ?)
            """,
                (cache_enabled, cache_timeout, cache_base_url),
            )

        # Ensure debug_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM debug_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            debug_enabled = False
            log_requests = True
            log_responses = True
            mask_token = True

            if config_dict:
                debug_config = config_dict.get("debug", {})
                debug_enabled = debug_config.get("enabled", False)
                log_requests = debug_config.get("log_requests", True)
                log_responses = debug_config.get("log_responses", True)
                mask_token = debug_config.get("mask_token", True)

            await db.execute(
                """
                INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                VALUES (1, ?, ?, ?, ?)
            """,
                (debug_enabled, log_requests, log_responses, mask_token),
            )

        # Only the remote headed service is available in this edition.
        cursor = await db.execute("SELECT COUNT(*) FROM captcha_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            captcha = (config_dict or {}).get("captcha", {})
            try:
                timeout = max(5, int(captcha.get("remote_browser_timeout", 60)))
            except (TypeError, ValueError):
                timeout = 60
            await db.execute(
                "INSERT INTO captcha_config (id, captcha_method, remote_browser_base_url, "
                "remote_browser_api_key, remote_browser_timeout) VALUES (1, 'remote_browser', ?, ?, ?)",
                (
                    REMOTE_BROWSER_BASE_URL,
                    captcha.get("remote_browser_api_key", ""),
                    timeout,
                ),
            )

        # Ensure token_refresh_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM token_refresh_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("""
                INSERT INTO token_refresh_config (id, enabled, refresh_interval_minutes)
                VALUES (1, 1, 120)
            """)

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
        async with self._database._connect(write=True) as db:
            print("Checking database integrity and performing migrations...")
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")

            # ========== Step 1: Create missing tables ==========
            # Check and create cache_config table if missing
            if not await self._database._table_exists(db, "cache_config"):
                print("  ✓ Creating missing table: cache_config")
                await db.execute("""
                    CREATE TABLE cache_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        cache_enabled BOOLEAN DEFAULT 0,
                        cache_timeout INTEGER DEFAULT 7200,
                        cache_base_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create proxy_config table if missing
            if not await self._database._table_exists(db, "proxy_config"):
                print("  ✓ Creating missing table: proxy_config")
                await db.execute("""
                    CREATE TABLE proxy_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        enabled BOOLEAN DEFAULT 0,
                        proxy_url TEXT,
                        media_proxy_enabled BOOLEAN DEFAULT 0,
                        media_proxy_url TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create proxy_pool table if missing
            if not await self._database._table_exists(db, "proxy_pool"):
                print("  ✓ Creating missing table: proxy_pool")
                await db.execute("""
                    CREATE TABLE proxy_pool (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        proxy_url TEXT NOT NULL,
                        enabled BOOLEAN DEFAULT 1,
                        captcha_enabled BOOLEAN DEFAULT 1,
                        note TEXT DEFAULT '',
                        last_check_ok BOOLEAN,
                        last_check_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create call_logic_config table if missing
            if not await self._database._table_exists(db, "call_logic_config"):
                print("  Creating missing table: call_logic_config")
                await db.execute("""
                    CREATE TABLE call_logic_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        call_mode TEXT DEFAULT 'default',
                        polling_mode_enabled BOOLEAN DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create captcha_config table if missing
            if not await self._database._table_exists(db, "captcha_config"):
                print("  ✓ Creating missing table: captcha_config")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS captcha_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        captcha_method TEXT DEFAULT 'remote_browser',
                        remote_browser_base_url TEXT DEFAULT '',
                        remote_browser_api_key TEXT DEFAULT '',
                        remote_browser_timeout INTEGER DEFAULT 60,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            if not await self._database._table_exists(db, "token_refresh_config"):
                print("  ✓ Creating missing table: token_refresh_config")
                await db.execute("""
                    CREATE TABLE token_refresh_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        enabled BOOLEAN DEFAULT 1,
                        refresh_interval_minutes INTEGER DEFAULT 120,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # ========== Step 2: Add missing columns to existing tables ==========
            # Check and add missing columns to tokens table
            if await self._database._table_exists(db, "tokens"):
                columns_to_add = [
                    ("at", "TEXT"),  # Access Token
                    ("at_expires", "TIMESTAMP"),  # AT expiration time
                    ("credits", "INTEGER DEFAULT 0"),  # Balance
                    ("user_paygate_tier", "TEXT"),  # User tier
                    ("current_project_id", "TEXT"),  # Current project UUID
                    ("current_project_name", "TEXT"),  # Project name
                    ("image_enabled", "BOOLEAN DEFAULT 1"),
                    ("video_enabled", "BOOLEAN DEFAULT 1"),
                    ("image_concurrency", "INTEGER DEFAULT -1"),
                    ("video_concurrency", "INTEGER DEFAULT -1"),
                    ("captcha_proxy_url", "TEXT"),  # token级打码代理
                    ("protocol_mode", "TEXT DEFAULT 'session'"),  # ST 刷新模式
                    ("google_cookies", "TEXT DEFAULT ''"),  # 协议登录 Google Cookies
                    ("login_account", "TEXT DEFAULT ''"),  # 协议登录账号提示
                    ("login_password", "TEXT DEFAULT ''"),  # 预留
                    ("proxy_url", "TEXT DEFAULT ''"),  # 协议刷新代理
                    ("proxy_pool_id", "INTEGER"),
                    ("auto_refresh_enabled", "BOOLEAN DEFAULT 1"),
                    ("refresh_interval_minutes", "INTEGER DEFAULT 120"),
                    ("last_st_refresh_at", "TIMESTAMP"),
                    ("last_st_refresh_result", "TEXT DEFAULT ''"),
                    ("refresh_status", "TEXT DEFAULT 'idle'"),
                    ("refresh_error", "TEXT DEFAULT ''"),
                    ("refresh_failures", "INTEGER DEFAULT 0"),
                    ("next_refresh_at", "TIMESTAMP"),
                    ("last_refresh_at", "TIMESTAMP"),
                    ("browser_profile_epoch", "TEXT DEFAULT ''"),
                    ("browser_profile_source", "TEXT DEFAULT 'cookies'"),
                    ("browser_profile_status", "TEXT DEFAULT 'uninitialized'"),
                    ("browser_profile_error", "TEXT DEFAULT ''"),
                    ("browser_profile_verified_at", "TIMESTAMP"),
                    ("browser_profile_next_at", "TIMESTAMP"),
                    ("browser_profile_failures", "INTEGER DEFAULT 0"),
                    ("ban_reason", "TEXT"),  # 禁用原因
                    ("banned_at", "TIMESTAMP"),  # 禁用时间
                ]

                for col_name, col_type in columns_to_add:
                    if not await self._database._column_exists(db, "tokens", col_name):
                        try:
                            await db.execute(f"ALTER TABLE tokens ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to tokens table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            if not await self._database._column_exists(db, "proxy_pool", "captcha_enabled"):
                await db.execute(
                    "ALTER TABLE proxy_pool ADD COLUMN captcha_enabled BOOLEAN DEFAULT 1"
                )

            # Check and add missing columns to admin_config table
            if await self._database._table_exists(db, "admin_config"):
                if not await self._database._column_exists(
                    db, "admin_config", "error_ban_threshold"
                ):
                    try:
                        await db.execute(
                            "ALTER TABLE admin_config ADD COLUMN error_ban_threshold INTEGER DEFAULT 3"
                        )
                        print("  ✓ Added column 'error_ban_threshold' to admin_config table")
                    except Exception as e:
                        print(f"  ✗ Failed to add column 'error_ban_threshold': {e}")

            # Check and add missing columns to proxy_config table
            if await self._database._table_exists(db, "proxy_config"):
                proxy_columns_to_add = [
                    ("media_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("media_proxy_url", "TEXT"),
                ]

                for col_name, col_type in proxy_columns_to_add:
                    if not await self._database._column_exists(db, "proxy_config", col_name):
                        try:
                            await db.execute(
                                f"ALTER TABLE proxy_config ADD COLUMN {col_name} {col_type}"
                            )
                            print(f"  ✓ Added column '{col_name}' to proxy_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to generation_config table
            if await self._database._table_exists(db, "generation_config"):
                generation_columns_to_add = [
                    ("max_retries", "INTEGER DEFAULT 3"),
                ]

                for col_name, col_type in generation_columns_to_add:
                    if not await self._database._column_exists(db, "generation_config", col_name):
                        try:
                            await db.execute(
                                f"ALTER TABLE generation_config ADD COLUMN {col_name} {col_type}"
                            )
                            print(f"  ✓ Added column '{col_name}' to generation_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Preserve existing remote configuration during upgrades.
            if await self._database._table_exists(db, "captcha_config"):
                for name, kind in (
                    ("remote_browser_base_url", "TEXT DEFAULT ''"),
                    ("remote_browser_api_key", "TEXT DEFAULT ''"),
                    ("remote_browser_timeout", "INTEGER DEFAULT 60"),
                ):
                    if not await self._database._column_exists(db, "captcha_config", name):
                        await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {name} {kind}")
                await db.execute("UPDATE captcha_config SET captcha_method = 'remote_browser'")

            # Check and add missing columns to token_stats table
            if await self._database._table_exists(db, "token_stats"):
                stats_columns_to_add = [
                    ("today_image_count", "INTEGER DEFAULT 0"),
                    ("today_video_count", "INTEGER DEFAULT 0"),
                    ("today_error_count", "INTEGER DEFAULT 0"),
                    ("today_date", "DATE"),
                    ("consecutive_error_count", "INTEGER DEFAULT 0"),  # 🆕 连续错误计数
                ]

                for col_name, col_type in stats_columns_to_add:
                    if not await self._database._column_exists(db, "token_stats", col_name):
                        try:
                            await db.execute(
                                f"ALTER TABLE token_stats ADD COLUMN {col_name} {col_type}"
                            )
                            print(f"  ✓ Added column '{col_name}' to token_stats table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            if await self._database._table_exists(db, "token_refresh_config"):
                token_refresh_columns_to_add = [
                    ("enabled", "BOOLEAN DEFAULT 1"),
                    ("refresh_interval_minutes", "INTEGER DEFAULT 120"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                ]

                for col_name, col_type in token_refresh_columns_to_add:
                    if not await self._database._column_exists(
                        db, "token_refresh_config", col_name
                    ):
                        try:
                            await db.execute(
                                f"ALTER TABLE token_refresh_config ADD COLUMN {col_name} {col_type}"
                            )
                            print(f"  ✓ Added column '{col_name}' to token_refresh_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Preserve existing remote configuration during upgrades.
            if await self._database._table_exists(db, "captcha_config"):
                for name, kind in (
                    ("remote_browser_base_url", "TEXT DEFAULT ''"),
                    ("remote_browser_api_key", "TEXT DEFAULT ''"),
                    ("remote_browser_timeout", "INTEGER DEFAULT 60"),
                ):
                    if not await self._database._column_exists(db, "captcha_config", name):
                        await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {name} {kind}")
                await db.execute("UPDATE captcha_config SET captcha_method = 'remote_browser'")

            # ========== Step 3: Ensure all config tables have default rows ==========
            # Note: This will NOT overwrite existing config rows
            # It only ensures missing rows are created with default values from setting.toml
            await self._database._ensure_config_rows(db, config_dict=config_dict)
            await self._migrate_account_proxy_bindings(db)

            await db.commit()
            print("Database migration check completed.")

    async def _migrate_account_proxy_bindings(self, db):
        """Freeze each old account's effective exit without changing its Profile."""
        from ...services.proxy_urls import normalize_proxy_url

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tokens_proxy_pool_id ON tokens(proxy_pool_id)"
        )
        cursor = await db.execute("SELECT enabled, proxy_url FROM proxy_config WHERE id = 1")
        global_row = await cursor.fetchone()
        inherited = global_row[1] if global_row and global_row[0] else ""
        cursor = await db.execute(
            "SELECT id, proxy_url FROM tokens WHERE proxy_pool_id IS NULL ORDER BY id"
        )
        for token_id, own_proxy in await cursor.fetchall():
            try:
                proxy = normalize_proxy_url(own_proxy or inherited)
            except ValueError:
                # Invalid old configuration remains invalid; never select another exit.
                continue
            if not proxy:
                continue
            cursor = await db.execute(
                "SELECT id FROM proxy_pool WHERE proxy_url = ? ORDER BY id LIMIT 1", (proxy,)
            )
            entry = await cursor.fetchone()
            if entry is None:
                cursor = await db.execute(
                    "INSERT INTO proxy_pool (proxy_url, captcha_enabled, note) VALUES (?, 0, ?)",
                    (proxy, "原账号出口"),
                )
                entry_id = cursor.lastrowid
            else:
                entry_id = entry[0]
            await db.execute(
                "UPDATE tokens SET proxy_pool_id = ?, proxy_url = ? WHERE id = ?",
                (entry_id, proxy, token_id),
            )

    async def init_db(self):
        """Initialize database tables"""
        async with self._database._connect(write=True) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            # Tokens table (lay4flow版本)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    st TEXT UNIQUE NOT NULL,
                    at TEXT,
                    at_expires TIMESTAMP,
                    email TEXT NOT NULL,
                    name TEXT,
                    remark TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    user_paygate_tier TEXT,
                    current_project_id TEXT,
                    current_project_name TEXT,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT -1,
                    captcha_proxy_url TEXT,
                    protocol_mode TEXT DEFAULT 'session',
                    google_cookies TEXT DEFAULT '',
                    login_account TEXT DEFAULT '',
                    login_password TEXT DEFAULT '',
                    proxy_url TEXT DEFAULT '',
                    proxy_pool_id INTEGER,
                    auto_refresh_enabled BOOLEAN DEFAULT 1,
                    refresh_interval_minutes INTEGER DEFAULT 120,
                    last_st_refresh_at TIMESTAMP,
                    last_st_refresh_result TEXT DEFAULT '',
                    refresh_status TEXT DEFAULT 'idle',
                    refresh_error TEXT DEFAULT '',
                    refresh_failures INTEGER DEFAULT 0,
                    next_refresh_at TIMESTAMP,
                    last_refresh_at TIMESTAMP,
                    browser_profile_epoch TEXT DEFAULT '',
                    browser_profile_source TEXT DEFAULT 'cookies',
                    browser_profile_status TEXT DEFAULT 'uninitialized',
                    browser_profile_error TEXT DEFAULT '',
                    browser_profile_verified_at TIMESTAMP,
                    browser_profile_next_at TIMESTAMP,
                    browser_profile_failures INTEGER DEFAULT 0,
                    ban_reason TEXT,
                    banned_at TIMESTAMP
                )
            """)

            # Projects table (新增)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Token stats table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    error_message TEXT,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Request logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    status_text TEXT DEFAULT '',
                    progress INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Admin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT '__configure_api_key__',
                    error_ban_threshold INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Proxy config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    media_proxy_enabled BOOLEAN DEFAULT 0,
                    media_proxy_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Proxy pool table (mint egress exits)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy_url TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    captcha_enabled BOOLEAN DEFAULT 1,
                    note TEXT DEFAULT '',
                    last_check_ok BOOLEAN,
                    last_check_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Generation config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    max_retries INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Call logic config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS call_logic_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    call_mode TEXT DEFAULT 'default',
                    polling_mode_enabled BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cache config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 7200,
                    cache_base_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Debug config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS debug_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    log_requests BOOLEAN DEFAULT 1,
                    log_responses BOOLEAN DEFAULT 1,
                    mask_token BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Captcha config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS captcha_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        captcha_method TEXT DEFAULT 'remote_browser',
                        remote_browser_base_url TEXT DEFAULT '',
                        remote_browser_api_key TEXT DEFAULT '',
                        remote_browser_timeout INTEGER DEFAULT 60,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_refresh_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 1,
                    refresh_interval_minutes INTEGER DEFAULT 120,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON tasks(task_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_st ON tokens(st)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_project_id ON projects(project_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_email ON tokens(email)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tokens_is_active_last_used_at ON tokens(is_active, last_used_at)"
            )

            # Migrate request_logs table if needed
            await self._database._migrate_request_logs(db)

            # Request logs query indexes (列表按 created_at 排序 / token 过滤)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_token_id_created_at ON request_logs(token_id, created_at DESC)"
            )

            # Token stats lookup index
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_stats_token_id ON token_stats(token_id)"
            )

            await db.commit()

    async def _migrate_request_logs(self, db):
        """Migrate request_logs table from old schema to new schema"""
        try:
            has_model = await self._database._column_exists(db, "request_logs", "model")
            has_operation = await self._database._column_exists(db, "request_logs", "operation")

            if has_model and not has_operation:
                print("?? ?????request_logs???,????...")
                await db.execute("ALTER TABLE request_logs RENAME TO request_logs_old")
                await db.execute("""
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id INTEGER,
                        operation TEXT NOT NULL,
                        request_body TEXT,
                        response_body TEXT,
                        status_code INTEGER NOT NULL,
                        duration FLOAT NOT NULL,
                        status_text TEXT DEFAULT '',
                        progress INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (token_id) REFERENCES tokens(id)
                    )
                """)
                await db.execute("""
                    INSERT INTO request_logs (token_id, operation, request_body, status_code, duration, status_text, progress, created_at, updated_at)
                    SELECT
                        token_id,
                        model as operation,
                        json_object('model', model, 'prompt', substr(prompt, 1, 100)) as request_body,
                        CASE
                            WHEN status = 'completed' THEN 200
                            WHEN status = 'failed' THEN 500
                            ELSE 102
                        END as status_code,
                        response_time as duration,
                        CASE
                            WHEN status = 'completed' THEN 'completed'
                            WHEN status = 'failed' THEN 'failed'
                            ELSE 'processing'
                        END as status_text,
                        CASE
                            WHEN status = 'completed' THEN 100
                            WHEN status = 'failed' THEN 0
                            ELSE 0
                        END as progress,
                        created_at,
                        created_at
                    FROM request_logs_old
                """)
                await db.execute("DROP TABLE request_logs_old")
                print("? request_logs?????")

            if not await self._database._column_exists(db, "request_logs", "status_text"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN status_text TEXT DEFAULT ''")
            if not await self._database._column_exists(db, "request_logs", "progress"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN progress INTEGER DEFAULT 0")
            if not await self._database._column_exists(db, "request_logs", "updated_at"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN updated_at TIMESTAMP")
            await db.execute(
                "UPDATE request_logs SET updated_at = created_at WHERE updated_at IS NULL"
            )
        except Exception as e:
            print(f"?? request_logs?????: {e}")
            # Continue even if migration fails, matching the legacy upgrade policy.
