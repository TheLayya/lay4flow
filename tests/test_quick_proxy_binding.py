"""Quick account/proxy bindings stay local and preserve running account state."""

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Header, HTTPException

from src.api.admin_routes.tokens import create_token_router
from src.core.database import Database
from src.core.models import Token
from src.services.concurrency_manager import ConcurrencyManager
from src.services.proxy_manager import ProxyManager
from src.services.token_manager import TokenManager


class _FlowStub:
    def __init__(self, proxy_manager):
        self.proxy_manager = proxy_manager
        self.st_calls = []
        self.current_proxy = None
        self.email = "account@example.invalid"

    @contextmanager
    def account_request_proxy(self, _proxy_url):
        previous = self.current_proxy
        self.current_proxy = _proxy_url
        try:
            yield
        finally:
            self.current_proxy = previous

    async def st_to_at(self, st):
        self.st_calls.append((st, self.current_proxy))
        return {
            "access_token": "at-for-" + st,
            "user": {"email": self.email},
            "expires": "2030-01-01T00:00:00+00:00",
        }


class QuickProxyBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / "binding.db"))
        await self.db.init_db()
        self.proxy_manager = ProxyManager(self.db)
        self.flow = _FlowStub(self.proxy_manager)
        self.manager = TokenManager(self.db, self.flow)
        self.concurrency = ConcurrencyManager()
        self.concurrency.reset_token = AsyncMock(wraps=self.concurrency.reset_token)

        async def verify(authorization: str | None = Header(default=None)):
            if authorization != "Bearer synthetic-session":
                raise HTTPException(status_code=401, detail="需要登录")
            return "synthetic-session"

        router, _ = create_token_router(
            lambda: self.db,
            lambda: self.manager,
            lambda: self.concurrency,
            verify,
        )
        app = FastAPI()
        app.include_router(router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://synthetic.invalid",
            headers={"Authorization": "Bearer synthetic-session"},
        )
        self.addAsyncCleanup(self.client.aclose)

        self.pool_one = await self.proxy_manager.add_proxy_pool_entry(
            "socks5://user:pass@proxy-one.invalid:1080"
        )
        self.pool_two = await self.proxy_manager.add_proxy_pool_entry(
            "socks5://user:pass@proxy-two.invalid:1080"
        )
        self.token_id = await self.db.add_token(
            Token(
                st="old-st",
                at="old-at",
                at_expires=datetime.now(timezone.utc) - timedelta(hours=2),
                email="account@example.invalid",
                name="Synthetic account",
                google_cookies='{"SID":"synthetic-cookie"}',
                login_account="synthetic-login",
                login_password="synthetic-password",
                current_project_id="synthetic-project",
                current_project_name="Synthetic project",
                proxy_pool_id=None,
                proxy_url="",
                is_active=False,
                image_concurrency=2,
                video_concurrency=3,
            )
        )
        await self.db.update_token(
            self.token_id,
            ban_reason="manual",
            browser_profile_epoch="synthetic-epoch",
            browser_profile_status="ready",
        )
        await self.concurrency.initialize(await self.db.get_all_tokens())
        self.assertTrue(await self.concurrency.acquire_image(self.token_id))
        self.assertTrue(await self.concurrency.acquire_video(self.token_id))
        self.before = await self.db.get_token(self.token_id)

    async def test_unauthenticated_binding_is_rejected(self):
        self.client.headers.pop("Authorization")
        response = await self.client.put(
            f"/api/tokens/{self.token_id}", json={"proxy_pool_id": self.pool_one}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(await self.db.get_token(self.token_id), self.before)
        self.assertEqual(self.flow.st_calls, [])

    async def test_cookie_binding_is_local_and_does_not_reset_or_touch_identity(self):
        for pool_id in (self.pool_one, self.pool_two):
            response = await self.client.put(
                f"/api/tokens/{self.token_id}", json={"proxy_pool_id": pool_id}
            )
            self.assertEqual(response.status_code, 200, response.text)
            saved = await self.db.get_token(self.token_id)
            self.assertEqual(saved.proxy_pool_id, pool_id)
            self.assertEqual(
                saved.proxy_url, (await self.db.get_proxy_pool_entry(pool_id)).proxy_url
            )
            for field in (
                "st",
                "at",
                "at_expires",
                "email",
                "name",
                "is_active",
                "ban_reason",
                "google_cookies",
                "login_account",
                "login_password",
                "browser_profile_epoch",
                "browser_profile_status",
                "current_project_id",
                "current_project_name",
            ):
                self.assertEqual(getattr(saved, field), getattr(self.before, field), field)
            self.assertEqual(await self.concurrency.snapshot(), {self.token_id: (1, 1)})
        self.assertEqual(self.flow.st_calls, [])
        self.concurrency.reset_token.assert_not_awaited()

    async def test_repeated_same_binding_is_idempotent(self):
        for _ in range(2):
            response = await self.client.put(
                f"/api/tokens/{self.token_id}", json={"proxy_pool_id": self.pool_one}
            )
            self.assertEqual(response.status_code, 200, response.text)
            saved = await self.db.get_token(self.token_id)
            if _ == 0:
                first = saved
            else:
                self.assertEqual(saved, first)
        self.assertEqual((await self.db.get_proxy_pool_entry(self.pool_one)).bound_account_count, 1)
        self.assertEqual(self.flow.st_calls, [])
        self.concurrency.reset_token.assert_not_awaited()

    async def test_remark_keeps_live_limits_and_explicit_limit_change_resets(self):
        response = await self.client.put(
            f"/api/tokens/{self.token_id}", json={"remark": "fast path"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.concurrency.reset_token.assert_not_awaited()
        self.assertEqual(await self.concurrency.snapshot(), {self.token_id: (1, 1)})

        response = await self.client.put(
            f"/api/tokens/{self.token_id}", json={"image_concurrency": None}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.concurrency.reset_token.assert_not_awaited()

        response = await self.client.put(
            f"/api/tokens/{self.token_id}", json={"image_concurrency": 4}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.concurrency.reset_token.assert_awaited_once_with(
            self.token_id, image_concurrency=4, video_concurrency=3
        )
        self.assertEqual(await self.concurrency.snapshot(), {self.token_id: (1, 1)})
        self.assertEqual(await self.concurrency.get_image_remaining(self.token_id), 3)
        self.assertEqual(await self.concurrency.get_video_remaining(self.token_id), 2)

    async def test_disabled_and_missing_pool_are_user_errors_without_mutation(self):
        await self.proxy_manager.set_proxy_pool_enabled(self.pool_one, False)
        for pool_id in (self.pool_one, 999999):
            for extra in ({}, {"st": "changed-st"}):
                response = await self.client.put(
                    f"/api/tokens/{self.token_id}",
                    json={"proxy_pool_id": pool_id, **extra},
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(await self.db.get_token(self.token_id), self.before)
        self.assertEqual(self.flow.st_calls, [])
        self.concurrency.reset_token.assert_not_awaited()

    async def test_unknown_account_and_existing_http_errors_keep_status(self):
        response = await self.client.put(
            "/api/tokens/999999", json={"proxy_pool_id": self.pool_one}
        )
        self.assertEqual(response.status_code, 404)
        with patch.object(
            self.manager,
            "update_token",
            AsyncMock(side_effect=HTTPException(status_code=403, detail="synthetic policy")),
        ):
            response = await self.client.put(
                f"/api/tokens/{self.token_id}", json={"proxy_pool_id": self.pool_one}
            )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(await self.db.get_token(self.token_id), self.before)

    async def test_cloak_allows_same_url_initial_pool_binding_and_rejects_changes(self):
        cloak_id = await self.db.add_token(
            Token(
                st="cloak-st",
                at="cloak-at",
                email="account@example.invalid",
                browser_profile_source="cloak",
                proxy_pool_id=None,
                proxy_url=(await self.db.get_proxy_pool_entry(self.pool_one)).proxy_url,
            )
        )
        same = await self.client.put(
            f"/api/tokens/{cloak_id}", json={"proxy_pool_id": self.pool_one}
        )
        self.assertEqual(same.status_code, 200, same.text)
        first = await self.db.get_token(cloak_id)
        self.assertEqual(first.proxy_pool_id, self.pool_one)
        for payload in (
            {"proxy_pool_id": self.pool_one},
            {"proxy_url": first.proxy_url},
        ):
            response = await self.client.put(f"/api/tokens/{cloak_id}", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(await self.db.get_token(cloak_id), first)
        for extra in ({}, {"st": "changed-st"}):
            for selection in (
                {"proxy_pool_id": self.pool_two},
                {"proxy_url": "socks5://new.invalid:1080"},
            ):
                different = await self.client.put(
                    f"/api/tokens/{cloak_id}", json={**selection, **extra}
                )
                self.assertEqual(different.status_code, 400, different.text)
                self.assertEqual(await self.db.get_token(cloak_id), first)
        self.assertEqual(self.flow.st_calls, [])
        self.concurrency.reset_token.assert_not_awaited()

    async def test_changed_st_verifies_identity_through_selected_new_proxy(self):
        target_proxy = (await self.db.get_proxy_pool_entry(self.pool_two)).proxy_url
        self.flow.email = "other@example.invalid"
        response = await self.client.put(
            f"/api/tokens/{self.token_id}",
            json={"st": "new-st", "proxy_pool_id": self.pool_two},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(await self.db.get_token(self.token_id), self.before)
        self.assertEqual(self.flow.st_calls, [("new-st", target_proxy)])

        self.flow.email = " ACCOUNT@example.invalid "
        response = await self.client.put(
            f"/api/tokens/{self.token_id}",
            json={"st": "new-st", "proxy_pool_id": self.pool_two},
        )
        self.assertEqual(response.status_code, 200, response.text)
        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.st, "new-st")
        self.assertEqual(saved.at, "at-for-new-st")
        self.assertEqual(saved.email, self.before.email)
        self.assertEqual(saved.proxy_pool_id, self.pool_two)
        self.assertFalse(saved.is_active)
        self.assertEqual(saved.ban_reason, "manual")
        self.assertEqual(self.flow.st_calls, [("new-st", target_proxy)] * 2)
        self.concurrency.reset_token.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
