"""SQLite schema extraction preserves upgrades, rows, transactions, and callbacks."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

from src.core.config import UNCONFIGURED_API_KEY
from src.core.database import Database
from src.core.models import Token


async def snapshot(database):
    """Read schema and rows in stable order, without changing runtime pragmas."""
    async with aiosqlite.connect(database.db_path) as connection:
        cursor = await connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
        schema = await cursor.fetchall()
        tables = {}
        for kind, name, _, _ in schema:
            if kind == "table":
                cursor = await connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
                tables[name] = await cursor.fetchall()
        return schema, tables


async def prepare_legacy_database(database):
    """Construct synthetic old columns and data accepted by existing migrations."""
    await database.init_db()
    token_id = await database.add_token(
        Token(st="synthetic-session", email="legacy@example.invalid", is_active=False)
    )
    await database.update_token(token_id, ban_reason="manual")
    async with database._connect(write=True) as connection:
        for table in (
            "cache_config",
            "proxy_config",
            "call_logic_config",
            "captcha_config",
            "token_refresh_config",
        ):
            await connection.execute(f"DROP TABLE {table}")
        for table, column in (
            ("tokens", "refresh_status"),
            ("tokens", "refresh_reason"),
            ("tokens", "refresh_stage"),
            ("tokens", "refresh_network_step"),
            ("tokens", "refresh_failures"),
            ("admin_config", "error_ban_threshold"),
            ("generation_config", "max_retries"),
            ("token_stats", "today_date"),
            ("token_stats", "consecutive_error_count"),
        ):
            await connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        await connection.execute(
            "INSERT INTO admin_config (id, username, password, api_key) "
            "VALUES (1, 'legacy-admin', 'synthetic-password', 'synthetic-api-key')"
        )
        await connection.execute(
            "INSERT INTO generation_config (id, image_timeout, video_timeout) VALUES (1, 123, 456)"
        )
        await connection.execute("DROP TABLE request_logs")
        await connection.execute(
            "CREATE TABLE request_logs (id INTEGER PRIMARY KEY, token_id INTEGER, model TEXT, "
            "prompt TEXT, status TEXT, response_time FLOAT, created_at TIMESTAMP)"
        )
        for index, status in enumerate(("completed", "failed", "processing"), 1):
            await connection.execute(
                "INSERT INTO request_logs VALUES (?, ?, 'legacy-model', ?, ?, 2.5, "
                "'2026-09-01 00:00:00')",
                (index, token_id, "p" * 120, status),
            )
        await connection.commit()
    return token_id


class DatabaseSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(str(Path(self.directory.name) / "schema.db"))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))

    async def test_fresh_init_and_migration_are_idempotent_and_keep_stored_config(self):
        await self.database.init_db()
        await self.database.check_and_migrate_db(
            {
                "global": {
                    "admin_username": "first",
                    "admin_password": "synthetic-password",
                    "api_key": "synthetic",
                },
                "generation": {"image_timeout": 123, "video_timeout": 456},
                "flow": {"max_retries": 7},
            }
        )
        original = await snapshot(self.database)
        await self.database.init_db()
        await self.database.check_and_migrate_db(
            {
                "global": {"admin_username": "replacement", "admin_password": "admin"},
                "flow": {"max_retries": 2},
            }
        )
        self.assertEqual(await snapshot(self.database), original)
        self.assertEqual((await self.database.get_admin_config()).username, "first")
        self.assertEqual((await self.database.get_admin_config()).password, "synthetic-password")
        self.assertEqual((await self.database.get_generation_config()).max_retries, 7)
        async with self.database._connect() as connection:
            self.assertEqual(
                (await (await connection.execute("PRAGMA journal_mode")).fetchone())[0], "wal"
            )
            self.assertEqual(
                (await (await connection.execute("PRAGMA foreign_keys")).fetchone())[0], 1
            )

    async def test_fresh_schema_has_no_extension_bridge_tables_or_route_key(self):
        await self.database.init_db()
        await self.database.check_and_migrate_db()
        admin_config = await self.database.get_admin_config()
        self.assertEqual((admin_config.username, admin_config.password), ("admin", "admin"))
        self.assertEqual(admin_config.api_key, UNCONFIGURED_API_KEY)
        async with self.database._connect() as connection:
            cursor = await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            names = {row[0] for row in await cursor.fetchall()}
            self.assertTrue({"plugin_config", "account_import_sessions"}.isdisjoint(names))
            cursor = await connection.execute("PRAGMA table_info(tokens)")
            self.assertNotIn("extension_route_key", {row[1] for row in await cursor.fetchall()})

    async def test_missing_credentials_use_default_admin_and_unconfigured_api_key(self):
        await self.database.init_db()
        await self.database.check_and_migrate_db({"global": {"admin_username": "admin"}})
        admin_config = await self.database.get_admin_config()
        self.assertEqual(admin_config.username, "admin")
        self.assertEqual(admin_config.password, "admin")
        self.assertEqual(admin_config.api_key, UNCONFIGURED_API_KEY)

    async def test_legacy_upgrade_preserves_disabled_account_and_transforms_request_logs(self):
        token_id = await prepare_legacy_database(self.database)
        await self.database.init_db()
        await self.database.check_and_migrate_db({"global": {"admin_username": "ignored"}})
        token = await self.database.get_token(token_id)
        self.assertEqual(token.st, "synthetic-session")
        self.assertFalse(token.is_active)
        self.assertEqual(token.ban_reason, "manual")
        self.assertEqual(token.refresh_status, "idle")
        self.assertEqual(token.refresh_failures, 0)
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                "SELECT refresh_reason, refresh_stage, refresh_network_step FROM tokens WHERE id = ?",
                (token_id,),
            )
            self.assertEqual(await cursor.fetchone(), ("", "", ""))
        await self.database.update_token(
            token_id,
            refresh_reason="transient_error",
            refresh_stage="oauth_renewal",
            refresh_network_step="oauth_signin",
        )
        token = await self.database.get_token(token_id)
        self.assertEqual(
            (token.refresh_reason, token.refresh_stage, token.refresh_network_step),
            ("transient_error", "oauth_renewal", "oauth_signin"),
        )
        self.assertEqual((await self.database.get_admin_config()).username, "legacy-admin")
        generation = await self.database.get_generation_config()
        self.assertEqual(
            (generation.image_timeout, generation.video_timeout, generation.max_retries),
            (123, 456, 3),
        )
        for log_id, code, progress in ((1, 200, 100), (2, 500, 0), (3, 102, 0)):
            detail = await self.database.get_log_detail(log_id)
            self.assertEqual(detail["operation"], "legacy-model")
            self.assertEqual(detail["status_code"], code)
            self.assertEqual(detail["progress"], progress)
            self.assertEqual(detail["duration"], 2.5)
            self.assertEqual(
                json.loads(detail["request_body"]), {"model": "legacy-model", "prompt": "p" * 100}
            )
            self.assertEqual(detail["updated_at"], "2026-09-01 00:00:00")
        original = await snapshot(self.database)
        await self.database.init_db()
        await self.database.check_and_migrate_db()
        self.assertEqual(await snapshot(self.database), original)

    async def test_config_rows_follow_caller_commit_and_rollback(self):
        await self.database.init_db()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            async with self.database._connect(write=True) as connection:
                await self.database._ensure_config_rows(
                    connection, {"global": {"admin_username": "transient"}}
                )
                raise RuntimeError("rollback")
        self.assertIsNone(await self.database.get_admin_config())
        self.assertFalse(self.database._write_lock.locked())
        async with self.database._connect(write=True) as connection:
            await self.database._ensure_config_rows(
                connection, {"global": {"admin_username": "persisted"}}
            )
            await connection.commit()
        self.assertEqual((await self.database.get_admin_config()).username, "persisted")

    async def test_schema_callbacks_still_use_database_overrides(self):
        migrate = AsyncMock(wraps=self.database._migrate_request_logs)
        columns = AsyncMock(wraps=self.database._column_exists)
        ensure = AsyncMock(wraps=self.database._ensure_config_rows)
        with (
            patch.object(self.database, "_migrate_request_logs", migrate),
            patch.object(self.database, "_column_exists", columns),
            patch.object(self.database, "_ensure_config_rows", ensure),
        ):
            await self.database.init_db()
            await self.database.check_and_migrate_db({})
        migrate.assert_awaited_once()
        self.assertGreater(columns.await_count, 0)
        ensure.assert_awaited_once()
        self.assertEqual(ensure.await_args.kwargs, {"config_dict": {}})
        self.assertFalse(self.database._write_lock.locked())


if __name__ == "__main__":
    unittest.main()
