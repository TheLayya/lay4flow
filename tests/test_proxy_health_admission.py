"""Known broken account-bound proxies never reach generation authentication."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.models import ProxyPoolEntry
from src.services.load_balancer import LoadBalancer
from src.services.proxy_health import bound_proxy_unavailable, proxy_pool_snapshot
from src.services.proxy_manager import AccountProxyUnavailable, ProxyManager
from src.services.vids.dispatcher import (
    VidsDispatchError,
    VidsPoolDispatcher,
    VidsProviderDispatcher,
)


def entry(**updates):
    return ProxyPoolEntry(id=7, proxy_url="socks5://192.0.2.7:1080", **updates)


def token(identifier=1, **updates):
    return SimpleNamespace(
        **{
            "id": identifier,
            "email": f"account-{identifier}@example.invalid",
            "proxy_pool_id": 7,
            "proxy_url": "socks5://192.0.2.7:1080",
            "is_active": True,
            "image_enabled": True,
            "video_enabled": True,
            "user_paygate_tier": "PAYGATE_TIER_TWO",
            "credits": 100,
            "browser_profile_source": "cloak",
            "at": "",
            **updates,
        }
    )


class ProxyHealthAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_disabled_cooling_missing_and_mismatched_are_unavailable(self):
        current = token()
        for candidate in (
            None,
            entry(last_check_ok=False),
            entry(enabled=False),
            entry(cooldown_until=datetime.now(timezone.utc) + timedelta(seconds=30)),
            entry(cooldown_until=datetime.now(timezone.utc) - timedelta(seconds=1)),
            entry(circuit_state="probing"),
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(
                    bound_proxy_unavailable(current, {} if candidate is None else {7: candidate})
                )
        self.assertTrue(
            bound_proxy_unavailable(token(proxy_url="socks5://192.0.2.9:1080"), {7: entry()})
        )
        self.assertFalse(bound_proxy_unavailable(current, {7: entry(last_check_ok=None)}))
        self.assertFalse(bound_proxy_unavailable(current, {7: entry(last_check_ok=True)}))

    async def test_failed_account_skipped_before_auth_and_recovery_restores_selection(self):
        bad, healthy = token(1), token(2, proxy_pool_id=8, proxy_url="socks5://192.0.2.8:1080")
        database = SimpleNamespace(
            list_proxy_pool=AsyncMock(
                return_value=[
                    entry(last_check_ok=False),
                    ProxyPoolEntry(id=8, proxy_url=healthy.proxy_url, last_check_ok=True),
                ]
            )
        )
        manager = SimpleNamespace(
            db=database,
            get_active_tokens=AsyncMock(return_value=[bad, healthy]),
            ensure_valid_token=AsyncMock(side_effect=lambda value: value),
            needs_at_refresh=lambda _: False,
        )
        balancer = LoadBalancer(manager)
        with patch("src.services.load_balancer.config") as config:
            config.call_logic_mode = "polling"
            self.assertIs(await balancer.select_token(for_video_generation=True), healthy)
            manager.ensure_valid_token.assert_awaited_once_with(healthy)
            database.list_proxy_pool.assert_awaited_once()
            database.list_proxy_pool.return_value[0] = entry(last_check_ok=True)
            manager.ensure_valid_token.reset_mock()
            self.assertIs(await balancer.select_token(for_video_generation=True), bad)
            manager.ensure_valid_token.assert_awaited_once_with(bad)

    async def test_all_bad_has_safe_reason_without_probe_or_auth(self):
        database = SimpleNamespace(
            list_proxy_pool=AsyncMock(return_value=[entry(last_check_ok=False)])
        )
        manager = SimpleNamespace(
            db=database,
            get_active_tokens=AsyncMock(return_value=[token()]),
            ensure_valid_token=AsyncMock(),
            needs_at_refresh=lambda _: False,
        )
        balancer = LoadBalancer(manager)
        self.assertIsNone(await balancer.select_token(for_image_generation=True))
        self.assertIn("代理", await balancer.get_unavailable_reason(for_image_generation=True))
        manager.ensure_valid_token.assert_not_awaited()

    async def test_fixed_resolver_refuses_failed_binding_without_fallback(self):
        database = SimpleNamespace(
            get_proxy_pool_entry=AsyncMock(return_value=entry(last_check_ok=False))
        )
        manager = ProxyManager(database)
        with self.assertRaises(AccountProxyUnavailable):
            await manager.resolve_account_proxy(token())
        database.get_proxy_pool_entry.assert_awaited_once_with(7)

    async def test_vids_new_generation_denied_but_existing_receipt_admission_kept(self):
        database = SimpleNamespace(
            get_token=AsyncMock(return_value=token()),
            list_proxy_pool=AsyncMock(return_value=[entry(last_check_ok=False)]),
        )
        dispatcher = VidsProviderDispatcher(database)
        dispatcher._account = lambda *args, **kwargs: object()
        with self.assertRaises(VidsDispatchError) as caught:
            await dispatcher._admission(1, "video", check_quota=True)
        self.assertEqual(caught.exception.code, "account_proxy_unavailable")
        database.list_proxy_pool.reset_mock()
        await dispatcher._admission(1, "video", check_session=False, check_quota=False)
        database.list_proxy_pool.assert_not_awaited()

    async def test_snapshot_is_one_database_read_without_network(self):
        database = SimpleNamespace(list_proxy_pool=AsyncMock(return_value=[entry()]))
        self.assertEqual(set(await proxy_pool_snapshot(database)), {7})
        database.list_proxy_pool.assert_awaited_once()

    async def test_vids_pool_skips_bad_proxy_and_reports_all_proxy_failures(self):
        first, second = token(1), token(2, proxy_pool_id=8, proxy_url="socks5://192.0.2.8:1080")
        database = SimpleNamespace(
            get_token=AsyncMock(side_effect=lambda identifier: {1: first, 2: second}[identifier]),
            list_proxy_pool=AsyncMock(
                return_value=[
                    entry(last_check_ok=False),
                    ProxyPoolEntry(id=8, proxy_url=second.proxy_url, last_check_ok=True),
                ]
            ),
        )
        dispatcher = VidsProviderDispatcher(database)
        dispatcher._accounts = {1: object(), 2: object()}
        gateway = SimpleNamespace(
            _ready=True, lock=asyncio.Lock(), client=SimpleNamespace(initialization_valid=True)
        )
        dispatcher._account = lambda *args, **kwargs: SimpleNamespace(gateway=gateway)
        pool = VidsPoolDispatcher(dispatcher)
        self.assertEqual(await pool._eligible("video"), 2)
        database.list_proxy_pool.assert_awaited_once()
        database.list_proxy_pool.return_value[1].last_check_ok = False
        with self.assertRaises(VidsDispatchError) as caught:
            await pool._eligible("video")
        self.assertEqual(caught.exception.code, "account_proxy_unavailable")


if __name__ == "__main__":
    unittest.main()
