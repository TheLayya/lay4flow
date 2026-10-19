"""Fixed account exits survive migration; media and captcha keep pool policies."""

import tempfile
import unittest
from pathlib import Path

from src.core.database import Database
from src.core.models import Token
from src.services.proxy_manager import AccountProxyUnavailable, ProxyManager


class AccountProxyPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / "pool.db"))
        await self.db.init_db()
        self.manager = ProxyManager(self.db)

    async def add_account(self, email, proxy_url="", proxy_pool_id=None):
        return await self.db.add_token(
            Token(
                st="synthetic-" + email,
                email=email,
                proxy_url=proxy_url,
                proxy_pool_id=proxy_pool_id,
            )
        )

    async def test_upgrade_preserves_effective_urls_and_original_captcha_sources(self):
        source = await self.manager.add_proxy_pool_entry("http://captcha.invalid:8080")
        own = await self.add_account("own@example.test", "socks5h://own.invalid:1080")
        inherited = await self.add_account("inherited@example.test")
        await self.db.update_proxy_config(enabled=True, proxy_url="http://relay.invalid:8080")
        await self.db.check_and_migrate_db()
        first = await self.db.get_token(own)
        second = await self.db.get_token(inherited)
        self.assertEqual(await self.manager.resolve_account_proxy(first), first.proxy_url)
        self.assertEqual(first.proxy_url, "socks5h://own.invalid:1080")
        self.assertEqual(second.proxy_url, "http://relay.invalid:8080")
        self.assertNotEqual(first.proxy_pool_id, second.proxy_pool_id)
        self.assertEqual(
            await self.manager.get_enabled_proxy_urls(), ["http://captcha.invalid:8080"]
        )
        before = [
            (p.id, p.proxy_url, p.captcha_enabled) for p in await self.manager.list_proxy_pool()
        ]
        await self.db.check_and_migrate_db()
        self.assertEqual(
            before,
            [(p.id, p.proxy_url, p.captcha_enabled) for p in await self.manager.list_proxy_pool()],
        )
        self.assertEqual((await self.db.get_token(own)).proxy_pool_id, first.proxy_pool_id)
        self.assertEqual((await self.db.get_proxy_pool_entry(source)).bound_account_count, 0)
        self.assertEqual(
            (await self.db.get_proxy_pool_entry(first.proxy_pool_id)).bound_account_count, 1
        )

    async def test_direct_legacy_account_is_not_randomly_bound_during_upgrade(self):
        await self.manager.add_proxy_pool_entry("http://pool.invalid:8080")
        account_id = await self.add_account("direct@example.test")
        await self.db.check_and_migrate_db()
        token = await self.db.get_token(account_id)
        self.assertIsNone(token.proxy_pool_id)
        self.assertIsNone(await self.manager.resolve_account_proxy(token))

    async def test_old_schema_columns_upgrade_and_bind_without_resetting_account(self):
        account_id = await self.add_account("legacy@example.test", "http://legacy.invalid:8080")
        await self.db.update_token(account_id, is_active=False, ban_reason="manual")
        async with self.db._connect(write=True) as connection:
            await connection.execute("ALTER TABLE tokens DROP COLUMN proxy_pool_id")
            await connection.execute("ALTER TABLE proxy_pool DROP COLUMN captcha_enabled")
            await connection.commit()
        await self.db.check_and_migrate_db()
        token = await self.db.get_token(account_id)
        self.assertEqual(
            await self.manager.resolve_account_proxy(token), "http://legacy.invalid:8080"
        )
        self.assertFalse(token.is_active)
        self.assertEqual(token.ban_reason, "manual")
        self.assertFalse((await self.db.get_proxy_pool_entry(token.proxy_pool_id)).captcha_enabled)

    async def test_fixed_binding_disabled_missing_or_mutated_fails_without_fallback(self):
        entry = await self.manager.add_proxy_pool_entry("http://account.invalid:8080")
        await self.db.update_proxy_config(enabled=True, proxy_url="http://global.invalid:8080")
        token_id = await self.add_account(
            "bound@example.test", "http://account.invalid:8080", entry
        )
        token = await self.db.get_token(token_id)
        self.assertEqual(await self.manager.resolve_account_proxy(token), token.proxy_url)
        await self.manager.set_proxy_pool_enabled(entry, False)
        with self.assertRaises(AccountProxyUnavailable):
            await self.manager.resolve_account_proxy(token)
        await self.manager.set_proxy_pool_enabled(entry, True)
        token.proxy_url = "http://changed.invalid:8080"
        with self.assertRaises(AccountProxyUnavailable):
            await self.manager.resolve_account_proxy(token)
        token.proxy_pool_id = 999
        with self.assertRaises(AccountProxyUnavailable):
            await self.manager.resolve_account_proxy(token)

    async def test_bound_exit_protects_url_and_delete_but_allows_note_and_pause(self):
        entry = await self.manager.add_proxy_pool_entry("http://account.invalid:8080")
        await self.add_account("bound@example.test", "http://account.invalid:8080", entry)
        with self.assertRaises(ValueError):
            await self.manager.update_proxy_pool_entry(entry, "http://changed.invalid:8080")
        with self.assertRaises(ValueError):
            await self.manager.remove_proxy_pool_entry(entry)
        await self.manager.update_proxy_pool_entry(entry, note="retained", captcha_enabled=False)
        await self.manager.set_proxy_pool_enabled(entry, False)
        saved = await self.db.get_proxy_pool_entry(entry)
        self.assertEqual(saved.note, "retained")
        self.assertFalse(saved.enabled)
        self.assertFalse(saved.captcha_enabled)

    async def test_assignment_uses_least_bound_exit_and_can_share_it(self):
        first = await self.manager.add_proxy_pool_entry("http://first.invalid:8080")
        second = await self.manager.add_proxy_pool_entry("http://second.invalid:8080")
        bad = await self.manager.add_proxy_pool_entry("http://bad.invalid:8080")
        await self.db.update_proxy_pool_check(bad, False)
        chosen, proxy = await self.manager.select_account_proxy()
        self.assertEqual(chosen, first)
        await self.add_account("first@example.test", proxy, chosen)
        chosen, proxy = await self.manager.select_account_proxy()
        self.assertEqual(chosen, second)
        await self.add_account("second@example.test", proxy, chosen)
        self.assertEqual((await self.manager.select_account_proxy())[0], first)
        # Explicit selection remains stable despite a stale failed health check.
        self.assertEqual((await self.manager.select_account_proxy(bad))[0], bad)
        for entry in (first, second, bad):
            await self.manager.set_proxy_pool_enabled(entry, False)
        with self.assertRaises(AccountProxyUnavailable):
            await self.manager.select_account_proxy()

    async def test_legacy_url_reuses_pool_entry_without_adding_captcha_egress(self):
        first = await self.manager.select_account_proxy(proxy_url="relay.invalid:8080")
        second = await self.manager.select_account_proxy(proxy_url="http://relay.invalid:8080")
        self.assertEqual(first, second)
        self.assertEqual(await self.manager.get_enabled_proxy_urls(), [])
        await self.manager.set_proxy_pool_enabled(first[0], False)
        with self.assertRaises(AccountProxyUnavailable):
            await self.manager.select_account_proxy(proxy_url=first[1])

    async def test_media_rotates_usable_pool_independently_of_fixed_account(self):
        first = await self.manager.add_proxy_pool_entry("http://first.invalid:8080")
        second = await self.manager.add_proxy_pool_entry("http://second.invalid:8080")
        bad = await self.manager.add_proxy_pool_entry("http://bad.invalid:8080")
        await self.db.update_proxy_pool_check(bad, False)
        await self.add_account("fixed@example.test", "http://first.invalid:8080", first)
        self.assertEqual(
            [await self.manager.select_media_proxy() for _ in range(3)],
            [
                "http://first.invalid:8080",
                "http://second.invalid:8080",
                "http://first.invalid:8080",
            ],
        )
        await self.manager.set_proxy_pool_enabled(first, False)
        self.assertEqual(await self.manager.select_media_proxy(), "http://second.invalid:8080")
        await self.manager.set_proxy_pool_enabled(second, False)
        with self.assertRaisesRegex(ValueError, "媒体出口"):
            await self.manager.select_media_proxy()


if __name__ == "__main__":
    unittest.main()
