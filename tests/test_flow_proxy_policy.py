"""Configured proxy failures never fall back to a direct auth request."""

import asyncio
import unittest
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from src.core.config import config
from src.core.models import Token
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler
from src.services.proxy_manager import AccountProxyUnavailable
from src.services.token_manager import TokenManager


class FlowProxyPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_st_to_at_has_one_request_path(self):
        client = FlowClient(proxy_manager=None)
        client._make_request = AsyncMock(side_effect=RuntimeError("proxy error: curl: (7)"))

        with self.assertRaisesRegex(RuntimeError, "proxy"):
            await client.st_to_at("synthetic-st")

        client._make_request.assert_awaited_once()
        self.assertNotIn("force_no_proxy", client._make_request.call_args.kwargs)


class AccountRefreshProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(config._config, {"captcha": {"captcha_method": "remote_browser"}})
        )
        self.proxy = SimpleNamespace(
            get_request_proxy_url=AsyncMock(return_value="http://global.proxy:8080")
        )
        self.client = FlowClient(self.proxy)
        self.manager = TokenManager(Mock(), self.client)
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.__aexit__ = AsyncMock(return_value=False)
        self.session.get = AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                url="https://labs.google/fx/api/auth/session",
                headers={},
                text="{}",
                json=lambda: {},
            )
        )
        self.enterContext(patch("src.services.flow_client.AsyncSession", return_value=self.session))

    async def test_account_proxy_reaches_actual_session_and_credits_requests(self):
        token = Token(st="st", email="account@example.test", proxy_url="http://account.proxy:8080")
        self.client._set_request_fingerprint({"user_agent": "synthetic-request-agent"})
        await self.manager._st_to_at_for_token(token, "st")
        await self.manager._get_credits_for_token(token, "at")
        self.assertEqual(
            [call.kwargs["proxy"] for call in self.session.get.await_args_list],
            [token.proxy_url, token.proxy_url],
        )
        await self.client.st_to_at("outside-auth")
        self.assertEqual(self.session.get.await_args.kwargs["proxy"], "http://global.proxy:8080")
        self.assertEqual(
            self.client.get_request_fingerprint(), {"user_agent": "synthetic-request-agent"}
        )

    async def test_concurrent_refresh_proxies_and_cancellation_do_not_leak(self):
        requests = []
        ready = asyncio.Event()

        async def get(url, **kwargs):
            requests.append((kwargs["headers"]["Cookie"], kwargs["proxy"]))
            if len(requests) == 2:
                ready.set()
            await ready.wait()
            return SimpleNamespace(status_code=200, url=url, headers={}, text="{}", json=lambda: {})

        self.session.get.side_effect = get
        tokens = [
            Token(st=name, email=name + "@example.test", proxy_url="http://" + name + ".proxy:8080")
            for name in ("first", "second")
        ]
        await asyncio.gather(
            *(self.manager._st_to_at_for_token(token, token.st) for token in tokens)
        )
        self.assertEqual(
            set(requests),
            {(f"__Secure-next-auth.session-token={token.st}", token.proxy_url) for token in tokens},
        )

        async def cancelled():
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.manager._flow_call_for_token(tokens[0], cancelled)
        self.session.get.side_effect = None
        await self.client.st_to_at("after-cancel")
        self.assertEqual(self.session.get.await_args.kwargs["proxy"], "http://global.proxy:8080")

    async def test_account_proxy_is_used_even_when_global_proxy_is_disabled(self):
        self.proxy.get_request_proxy_url.return_value = None
        token = Token(st="st", email="account@example.test", proxy_url="http://account.proxy:8080")
        await self.manager._st_to_at_for_token(token, token.st)
        self.assertEqual(self.session.get.await_args.kwargs["proxy"], token.proxy_url)
        self.proxy.get_request_proxy_url.assert_not_awaited()

    async def test_account_proxy_failure_never_retries_global_or_direct(self):
        token = Token(st="st", email="account@example.test", proxy_url="http://account.proxy:8080")
        self.session.get.side_effect = RuntimeError("curl: (7) proxy unavailable")
        with patch.object(
            self.client,
            "_sync_json_request_via_urllib",
            side_effect=RuntimeError("proxy unavailable"),
        ) as fallback:
            with self.assertRaisesRegex(Exception, "proxy"):
                await self.manager._st_to_at_for_token(token, token.st)
        self.session.get.assert_awaited_once()
        self.assertEqual(self.session.get.await_args.kwargs["proxy"], token.proxy_url)
        self.assertEqual(fallback.call_args.args[4], token.proxy_url)
        self.proxy.get_request_proxy_url.assert_not_awaited()

    async def test_invalid_account_proxy_fails_before_transport(self):
        token = Token(st="st", email="account@example.test", proxy_url="ftp://bad.proxy:80")
        with self.assertRaises(ValueError):
            await self.manager._st_to_at_for_token(token, "st")
        self.session.get.assert_not_awaited()

    async def test_bound_proxy_failure_skips_cached_account_without_auth_or_disable(self):
        self.proxy.resolve_account_proxy = AsyncMock(side_effect=AccountProxyUnavailable())
        token = Token(
            id=7,
            st="st",
            email="account@example.test",
            at="at",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=5),
            proxy_pool_id=1,
            proxy_url="http://account.proxy:8080",
        )
        self.manager._mark_at_valid(token.id)
        self.manager._refresh_at = AsyncMock()
        self.assertIsNone(await self.manager.ensure_valid_token(token))
        self.assertTrue(token.is_active)
        self.manager._refresh_at.assert_not_awaited()
        self.session.get.assert_not_awaited()
        self.proxy.get_request_proxy_url.assert_not_awaited()

    async def test_authenticated_media_redirect_uses_account_context(self):
        self.session.get.return_value = SimpleNamespace(
            status_code=302,
            headers={"Location": "https://storage.googleapis.com/result.mp4"},
        )
        with self.client.account_request_proxy("http://bound.proxy:8080"):
            await self.client.get_media_url_redirect("st", "synthetic-media")
        self.assertEqual(self.session.get.await_args.kwargs["proxy"], "http://bound.proxy:8080")
        self.proxy.get_request_proxy_url.assert_not_awaited()

    async def test_generation_scope_covers_all_modes_and_resets_between_yields(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = self.client
        token = Token(st="st", email="account@example.test", proxy_url="http://bound.proxy:8080")
        for mode in ("remote_browser",):
            observed = []

            async def pipeline():
                try:
                    observed.append(self.client._account_proxy_ctx.get())
                    yield "first"
                    observed.append(self.client._account_proxy_ctx.get())
                    yield "second"
                finally:
                    observed.append(self.client._account_proxy_ctx.get())

            with patch.dict(config._config["captcha"], {"captcha_method": mode}):
                async with aclosing(
                    handler._account_generation_pipeline(pipeline(), token)
                ) as scoped:
                    self.assertEqual(await anext(scoped), "first")
                    self.assertIsNone(self.client._account_proxy_ctx.get())
                    self.assertEqual(await anext(scoped), "second")
                    self.assertIsNone(self.client._account_proxy_ctx.get())
            self.assertEqual(observed, [token.proxy_url] * 3)

    async def test_project_creation_uses_resolved_binding(self):
        token = Token(st="st", email="account@example.test", proxy_url="http://bound.proxy:8080")
        self.proxy.resolve_account_proxy = AsyncMock(return_value=token.proxy_url)
        observed = []

        async def create(*args, **kwargs):
            observed.append(self.client._account_proxy_ctx.get())
            return "project"

        self.manager._project_pool.create_project_for_token = AsyncMock(side_effect=create)
        self.assertEqual(await self.manager._create_project_for_token(token, 1), "project")
        self.assertEqual(observed, [token.proxy_url])
        self.assertIsNone(self.client._account_proxy_ctx.get())

    async def test_refresh_credits_uses_resolved_account_binding(self):
        token = Token(
            id=7,
            st="st",
            at="at",
            email="account@example.test",
            proxy_pool_id=1,
            proxy_url="http://bound.proxy:8080",
        )
        self.proxy.resolve_account_proxy = AsyncMock(return_value=token.proxy_url)
        self.manager.db = SimpleNamespace(
            get_token=AsyncMock(return_value=token), update_token=AsyncMock()
        )
        self.manager.ensure_valid_token = AsyncMock(return_value=token)
        self.session.get.return_value.json = lambda: {"credits": 9}
        self.assertEqual(await self.manager.refresh_credits(7), 9)
        self.assertEqual(self.session.get.await_args.kwargs["proxy"], token.proxy_url)

    async def test_existing_cloak_profile_rejects_silent_proxy_change(self):
        token = Token(
            id=7,
            st="st",
            email="account@example.test",
            browser_profile_source="cloak",
            proxy_pool_id=1,
            proxy_url="http://bound.proxy:8080",
        )
        self.proxy.select_account_proxy = AsyncMock(return_value=(2, "http://other.proxy:8080"))
        self.manager.db = SimpleNamespace(
            get_token=AsyncMock(return_value=token), update_token=AsyncMock()
        )
        with self.assertRaisesRegex(ValueError, "Cloak Profile"):
            await self.manager.update_token(7, proxy_pool_id=2)
        self.manager.db.update_token.assert_not_awaited()
        self.proxy.select_account_proxy.return_value = (1, token.proxy_url)
        await self.manager.update_token(7, proxy_pool_id=1)
        self.assertEqual(self.manager.db.update_token.await_args.kwargs["proxy_pool_id"], 1)

    async def test_disabled_binding_prevents_refresh_without_changing_credentials(self):
        token = Token(
            id=7,
            st="st",
            email="account@example.test",
            proxy_pool_id=1,
            proxy_url="http://bound.proxy:8080",
        )
        self.proxy.resolve_account_proxy = AsyncMock(side_effect=AccountProxyUnavailable())
        self.manager.db = SimpleNamespace(
            get_token=AsyncMock(return_value=token), update_token_if_credentials_match=AsyncMock()
        )
        self.manager._refresh_credentials = AsyncMock()
        self.assertFalse(await self.manager._refresh_at_inner(7, force=True))
        self.manager.db.update_token_if_credentials_match.assert_not_awaited()
        self.manager._refresh_credentials.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
