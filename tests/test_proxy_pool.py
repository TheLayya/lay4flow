"""Offline tests for the proxy pool management (mint egress sources)."""

import unittest
from types import SimpleNamespace

from src.core.models import ProxyPoolEntry
from src.services.proxy_manager import ProxyManager


class FakeDB:
    def __init__(self):
        self.entries = {}
        self._next_id = 1

    async def list_proxy_pool(self):
        return [ProxyPoolEntry(**d) for d in sorted(self.entries.values(), key=lambda d: d["id"])]

    async def get_proxy_pool_entry(self, entry_id):
        d = self.entries.get(entry_id)
        return ProxyPoolEntry(**d) if d else None

    async def add_proxy_pool_entry(self, proxy_url, note=""):
        entry_id = self._next_id
        self.entries[entry_id] = {
            "id": entry_id,
            "proxy_url": proxy_url,
            "note": note,
            "enabled": True,
        }
        self._next_id += 1
        return entry_id

    async def remove_proxy_pool_entry(self, entry_id):
        self.entries.pop(entry_id, None)

    async def set_proxy_pool_enabled(self, entry_id, enabled):
        if entry_id in self.entries:
            self.entries[entry_id]["enabled"] = enabled

    async def update_proxy_pool_check(self, entry_id, ok):
        if entry_id in self.entries:
            self.entries[entry_id]["last_check_ok"] = ok

    async def get_proxy_config(self):
        return SimpleNamespace(enabled=0, proxy_url="", media_proxy_enabled=0, media_proxy_url="")

    async def update_proxy_config(self, **kwargs):
        pass


class ProxyPoolTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, db):
        return ProxyManager(db)

    async def test_add_list_and_enabled_filter(self):
        db = FakeDB()
        manager = self.manager(db)
        id1 = await manager.add_proxy_pool_entry("socks5h://u:p@1.1.1.1:1111")
        id2 = await manager.add_proxy_pool_entry("socks5h://u:p@2.2.2.2:2222")
        await manager.set_proxy_pool_enabled(id2, False)
        self.assertEqual(await manager.get_enabled_proxy_urls(), ["socks5h://u:p@1.1.1.1:1111"])
        self.assertIsNotNone(await db.get_proxy_pool_entry(id1))

    async def test_add_normalizes_legacy_proxy_and_removes(self):
        db = FakeDB()
        manager = self.manager(db)
        entry_id = await manager.add_proxy_pool_entry("1.2.3.4:1234:user:pass")
        entry = await db.get_proxy_pool_entry(entry_id)
        self.assertEqual(entry.proxy_url, "http://user:pass@1.2.3.4:1234")
        await manager.remove_proxy_pool_entry(entry_id)
        self.assertIsNone(await db.get_proxy_pool_entry(entry_id))

    async def test_add_rejects_invalid_proxy(self):
        db = FakeDB()
        manager = self.manager(db)
        with self.assertRaises(ValueError):
            await manager.add_proxy_pool_entry("not-a-proxy:::")


if __name__ == "__main__":
    unittest.main()
