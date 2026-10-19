"""Real SQLite settings keep field-specific defaults and partial-update semantics."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.core.config import REMOTE_BROWSER_BASE_URL
from src.core.database import Database
from src.core.models import TokenRefreshConfig

CAPTCHA_UPDATES = {
    "captcha_method": "remote_browser",
    "remote_browser_base_url": "https://remote.example.invalid",
    "remote_browser_api_key": "synthetic-remote",
    "remote_browser_timeout": 75,
}


def values(model):
    return model.model_dump(exclude={"created_at", "updated_at"}) if model is not None else None


class DatabaseSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(str(Path(self.directory.name) / "settings.db"))
        await self.database.init_db()

    async def test_empty_getters_and_insert_defaults_keep_existing_differences(self):
        for name in ("admin", "proxy", "generation"):
            self.assertIsNone(await getattr(self.database, f"get_{name}_config")())
        self.assertEqual(
            (await self.database.get_captcha_config()).captcha_method, "remote_browser"
        )
        self.assertFalse((await self.database.get_cache_config()).cache_enabled)
        self.assertEqual((await self.database.get_call_logic_config()).call_mode, "default")
        self.assertTrue((await self.database.get_debug_config()).mask_token)
        self.assertTrue((await self.database.get_token_refresh_config()).enabled)
        await self.database.update_admin_config(username="no-row")
        self.assertIsNone(await self.database.get_admin_config())
        await self.database.update_proxy_config(False)
        await self.database.update_generation_config()
        await self.database.update_cache_config(base_url="")
        await self.database.update_debug_config(enabled=False, log_requests=False)
        await self.database.update_captcha_config()
        refresh = await self.database.update_token_refresh_config(enabled=False)
        self.assertFalse((await self.database.get_proxy_config()).enabled)
        self.assertEqual((await self.database.get_generation_config()).max_retries, 3)
        self.assertEqual((await self.database.get_cache_config()).cache_base_url, "")
        self.assertFalse((await self.database.get_debug_config()).log_requests)
        self.assertEqual(
            (await self.database.get_captcha_config()).captcha_method, "remote_browser"
        )
        self.assertEqual((refresh.enabled, refresh.refresh_interval_minutes), (False, 120))

    async def test_each_captcha_field_update_preserves_other_values_and_none_means_keep(self):
        await self.database.update_captcha_config()
        expected = values(await self.database.get_captcha_config())
        for field, value in CAPTCHA_UPDATES.items():
            with self.subTest(field=field):
                await self.database.update_captcha_config(**{field: value})
                expected[field] = (
                    REMOTE_BROWSER_BASE_URL if field == "remote_browser_base_url" else value
                )
                self.assertEqual(values(await self.database.get_captcha_config()), expected)
                await self.database.update_captcha_config(**{field: None})
                self.assertEqual(values(await self.database.get_captcha_config()), expected)
        await self.database.update_captcha_config(remote_browser_api_key="")
        expected.update(remote_browser_api_key="")
        self.assertEqual(values(await self.database.get_captcha_config()), expected)

    async def test_partial_scalar_updates_false_zero_and_empty_keep_distinct_semantics(self):
        async with self.database._connect(write=True) as connection:
            await self.database._ensure_config_rows(connection)
            await connection.commit()
        for field, value in (
            ("username", "changed"),
            ("password", ""),
            ("api_key", "synthetic"),
            ("error_ban_threshold", 0),
        ):
            before = values(await self.database.get_admin_config())
            await self.database.update_admin_config(**{field: value})
            before[field] = value
            self.assertEqual(values(await self.database.get_admin_config()), before)
            await self.database.update_admin_config(**{field: None})
            self.assertEqual(values(await self.database.get_admin_config()), before)
        for field, value in (("image_timeout", 0), ("video_timeout", 123), ("max_retries", 7)):
            before = values(await self.database.get_generation_config())
            await self.database.update_generation_config(**{field: value})
            before[field] = value
            self.assertEqual(values(await self.database.get_generation_config()), before)
            await self.database.update_generation_config(**{field: None})
            self.assertEqual(values(await self.database.get_generation_config()), before)
        await self.database.update_cache_config(True, 90, "https://cache.example.invalid")
        await self.database.update_cache_config(enabled=False, timeout=0)
        cache = await self.database.get_cache_config()
        self.assertEqual(
            (cache.cache_enabled, cache.cache_timeout, cache.cache_base_url),
            (False, 0, "https://cache.example.invalid"),
        )
        await self.database.update_cache_config(base_url="")
        self.assertIsNone((await self.database.get_cache_config()).cache_base_url)
        await self.database.update_debug_config(True, True, True, True)
        for field in ("enabled", "log_requests", "log_responses", "mask_token"):
            before = values(await self.database.get_debug_config())
            await self.database.update_debug_config(**{field: False})
            before[field] = False
            self.assertEqual(values(await self.database.get_debug_config()), before)
            await self.database.update_debug_config(**{field: None})
            self.assertEqual(values(await self.database.get_debug_config()), before)

    async def test_proxy_url_clearing_and_call_logic_normalization_are_preserved(self):
        await self.database.update_proxy_config(
            True, "http://request.invalid", True, "http://media.invalid"
        )
        await self.database.update_proxy_config(False)
        proxy = await self.database.get_proxy_config()
        self.assertEqual(
            (proxy.enabled, proxy.proxy_url, proxy.media_proxy_enabled, proxy.media_proxy_url),
            (False, None, True, "http://media.invalid"),
        )
        await self.database.update_proxy_config(False, "", False, "")
        proxy = await self.database.get_proxy_config()
        self.assertEqual(
            (proxy.proxy_url, proxy.media_proxy_enabled, proxy.media_proxy_url), ("", False, "")
        )
        await self.database.update_call_logic_config("polling")
        self.assertTrue((await self.database.get_call_logic_config()).polling_mode_enabled)
        await self.database.update_call_logic_config("unknown")
        self.assertEqual((await self.database.get_call_logic_config()).call_mode, "default")
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                "UPDATE call_logic_config SET call_mode = 'legacy', polling_mode_enabled = 1"
            )
            await connection.commit()
        self.assertEqual((await self.database.get_call_logic_config()).call_mode, "polling")

    async def test_bounds_invalid_values_and_failed_update_release_lock_without_mutation(self):
        await self.database.update_captcha_config(**CAPTCHA_UPDATES)
        before = values(await self.database.get_captcha_config())
        with self.assertRaises(ValueError):
            await self.database.update_captcha_config(
                remote_browser_timeout="invalid", captcha_method="changed"
            )
        self.assertEqual(values(await self.database.get_captcha_config()), before)
        self.assertFalse(self.database._write_lock.locked())
        await self.database.update_captcha_config(remote_browser_timeout=0)
        self.assertEqual((await self.database.get_captcha_config()).remote_browser_timeout, 5)
        with self.assertRaises(ValueError):
            await self.database.update_captcha_config(captcha_method="unsupported")
        await self.database.update_generation_config(max_retries="invalid")
        self.assertEqual((await self.database.get_generation_config()).max_retries, 3)
        await self.database.update_generation_config(max_retries=0)
        self.assertEqual((await self.database.get_generation_config()).max_retries, 1)
        self.assertEqual(
            (
                await self.database.update_token_refresh_config(refresh_interval_minutes=0)
            ).refresh_interval_minutes,
            1,
        )
        self.assertEqual(
            (
                await self.database.update_token_refresh_config(refresh_interval_minutes="invalid")
            ).refresh_interval_minutes,
            120,
        )
        async with self.database._connect(write=True) as connection:
            await self.database._ensure_config_rows(connection)
            await connection.commit()
        before = values(await self.database.get_admin_config())
        with self.assertRaises(sqlite3.OperationalError):
            await self.database.update_admin_config(username="changed", missing_column=1)
        self.assertEqual(values(await self.database.get_admin_config()), before)
        self.assertFalse(self.database._write_lock.locked())

    async def test_repository_uses_late_bound_connection_and_refresh_callback(self):
        original_connect = self.database._connect
        with patch.object(self.database, "_connect", wraps=original_connect) as connect:
            await self.database.update_cache_config(enabled=True)
            await self.database.get_cache_config()
        self.assertEqual([call.kwargs for call in connect.call_args_list], [{"write": True}, {}])
        sentinel = TokenRefreshConfig(enabled=False, refresh_interval_minutes=17)
        with patch.object(
            self.database, "get_token_refresh_config", new=AsyncMock(return_value=sentinel)
        ) as get_refresh:
            self.assertIs(await self.database.update_token_refresh_config(enabled=False), sentinel)
        get_refresh.assert_awaited_once()
        async with self.database._write_lock:
            update = asyncio.create_task(self.database.update_debug_config(enabled=True))
            await asyncio.sleep(0)
            self.assertFalse(update.done())
            self.assertTrue((await self.database.get_cache_config()).cache_enabled)
        await asyncio.wait_for(update, timeout=2)
        self.assertFalse(self.database._write_lock.locked())


if __name__ == "__main__":
    unittest.main()
