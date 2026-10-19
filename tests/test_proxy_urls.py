"""Canonical proxy URL compatibility matrix."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.proxy_manager import ProxyManager
from src.services.proxy_urls import normalize_proxy_url


class ProxyUrlTests(unittest.TestCase):
    def test_supported_forms(self):
        expected = {
            "http://user:pass@host:80": "http://user:pass@host:80",
            "https://host:443": "https://host:443",
            "socks5://user:pass@host:1080": "socks5://user:pass@host:1080",
            "socks5h://host:1080": "socks5h://host:1080",
            "socks5://host:1080:user:pass": "socks5://user:pass@host:1080",
            "st5 host:1080:user:pass": "socks5://user:pass@host:1080",
            "host:80": "http://host:80",
            "host:80:user:pass:with:colons": "http://user:pass%3Awith%3Acolons@host:80",
            "192.0.2.1:6525:demo:p@ss/?#%": "http://demo:p%40ss%2F%3F%23%25@192.0.2.1:6525",
            "[2001:db8::1]:1080:demo:p@ss": "http://demo:p%40ss@[2001:db8::1]:1080",
            "user:pass@host:80": "http://user:pass@host:80",
        }
        for raw, normalized in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_proxy_url(raw), normalized)

    def test_empty_values_are_unconfigured(self):
        self.assertIsNone(normalize_proxy_url(None))
        self.assertIsNone(normalize_proxy_url("  "))

    def test_invalid_values_raise(self):
        for raw in (
            "ftp://host:80",
            "host",
            "host:not-a-port",
            "http://:80",
            "http://host:not-a-port",
            "http://host:0",
            "http://host:65536",
            "http://host:80/path",
            "http://host:80\n",
            "http://user:pass@host",
            "http://user:pass@host:bad",
            "http://user:pass@:80",
            "192.0.2.1:6525demo:password",
            "bad/path:80:user:pass",
            "bad?host:80:user:pass",
            "bad#host:80:user:pass",
            "http://bad\\host:80",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_proxy_url(raw)

    def test_proxy_manager_keeps_legacy_error_contract(self):
        manager = ProxyManager(SimpleNamespace())
        self.assertEqual(manager._parse_proxy_line("host"), None)
        with self.assertRaises(ValueError):
            manager.normalize_proxy_url("host")

    def test_proxy_manager_enabled_invalid_values_fail_closed(self):
        async def run():
            db = SimpleNamespace(get_proxy_config=AsyncMock())
            manager = ProxyManager(db)
            db.get_proxy_config.return_value = SimpleNamespace(
                enabled=True,
                proxy_url="",
                media_proxy_enabled=False,
                media_proxy_url=None,
            )
            with self.assertRaises(ValueError):
                await manager.get_request_proxy_url()
            db.get_proxy_config.return_value.proxy_url = "http://host:bad"
            with self.assertRaises(ValueError):
                await manager.get_request_proxy_url()
            db.get_proxy_config.return_value = SimpleNamespace(
                enabled=False,
                proxy_url=None,
                media_proxy_enabled=True,
                media_proxy_url="",
            )
            with self.assertRaises(ValueError):
                await manager.get_media_proxy_url()

        import asyncio

        asyncio.run(run())

    def test_proxy_manager_update_preserves_optional_media_value(self):
        async def run():
            db = SimpleNamespace(
                update_proxy_config=AsyncMock(),
                get_proxy_config=AsyncMock(),
            )
            manager = ProxyManager(db)
            await manager.update_proxy_config(True, "host:80")
            db.update_proxy_config.assert_awaited_once_with(
                enabled=True,
                proxy_url="http://host:80",
                media_proxy_enabled=None,
                media_proxy_url=None,
            )
            with self.assertRaises(ValueError):
                await manager.update_proxy_config(True, "")

        import asyncio

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
