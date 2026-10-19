"""Vids account management uses synthetic sessions and its own quota observations."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.api.admin_routes.vids import create_vids_admin_router
from src.services.vids.client import VidsProtocolError, VidsTransportError
from src.services.vids.dispatcher import VidsDispatchError, VidsProviderDispatcher


class Token:
    email = "synthetic@example.test"
    is_active = True
    image_enabled = True
    video_enabled = True
    cookies = "private-cookie"
    st = "private-token"

    @property
    def credits(self):
        raise AssertionError("Vids quota must not read Flow credits")


class VidsAdminManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = Token()
        self.db = SimpleNamespace(get_token=AsyncMock(return_value=self.token))
        self.dispatcher = VidsProviderDispatcher(self.db)
        self.gateway = SimpleNamespace(
            quota=AsyncMock(side_effect=self.quota),
            generate=AsyncMock(),
            start=AsyncMock(),
            client=SimpleNamespace(bootstrap=AsyncMock()),
            refresh_from_profile=AsyncMock(),
        )
        self.binding = SimpleNamespace(gateway=self.gateway)
        self.dispatcher._accounts = {4: self.binding}
        self.ready = True

        def resolve(token_id, **kwargs):
            if token_id != 4:
                raise VidsDispatchError(404, "account_not_found", "Vids account is not registered")
            if not self.ready:
                raise VidsDispatchError(503, "session_unavailable", "Vids session is unavailable")
            return self.binding

        self.resolve = patch.object(self.dispatcher, "_account", side_effect=resolve)
        self.resolve.start()
        self.runtime = SimpleNamespace(_started=True, last_error_code=None, account_errors={})
        self.settings = {
            "accounts": [
                {
                    "token_id": 4,
                    "identity_hash": "private-identity",
                    "runtime_dir": "private-runtime-path",
                    "editor_url": "private-editor-url",
                    "session_key_env": "private-session-key",
                }
            ]
        }
        self.app = FastAPI()
        self.app.include_router(
            create_vids_admin_router(
                self.dispatcher,
                admin.verify_admin_token,
                get_settings=lambda: self.settings,
                runtime=self.runtime,
            )
        )
        self.auth = patch.object(admin, "active_admin_tokens", {"synthetic-admin-session"})
        self.auth.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://console.example.test"
        )
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.resolve.stop()
        self.auth.stop()

    @staticmethod
    async def quota(kind):
        return {
            "kind": kind,
            "total": 20 if kind == "image" else 430,
            "available": 19 if kind == "image" else 427,
            "consumed": 1 if kind == "image" else 3,
        }

    def assert_no_generation_or_session_start(self):
        self.gateway.generate.assert_not_awaited()
        self.gateway.start.assert_not_awaited()
        self.gateway.client.bootstrap.assert_not_awaited()
        self.gateway.refresh_from_profile.assert_not_awaited()

    async def test_admin_cookie_required_before_local_or_upstream_lookup(self):
        self.client.cookies.clear()
        for method, route in (
            ("GET", "/api/admin/vids/accounts/detail"),
            ("GET", "/api/admin/vids/accounts/detail?refresh=true"),
            ("GET", "/api/admin/vids/status"),
            ("GET", "/api/admin/vids/status?refresh=true"),
            ("POST", "/api/admin/vids/accounts/4/quota-refresh"),
        ):
            response = await self.client.request(method, route)
            self.assertEqual(response.status_code, 401)
        self.db.get_token.assert_not_awaited()
        self.gateway.quota.assert_not_awaited()

    async def test_empty_configuration_is_not_an_unavailable_account(self):
        self.settings = {"accounts": []}
        self.dispatcher._accounts.clear()
        self.runtime._started = False
        detail = (await self.client.get("/api/admin/vids/accounts/detail")).json()
        self.assertEqual(detail["configured_count"], 0)
        self.assertEqual(detail["accounts"], [])
        status = (await self.client.get("/api/admin/vids/status")).json()
        self.assertFalse(status["configured"])
        self.assertEqual(status["available"], {"image": 0, "video": 0, "speech": 0})
        self.assertIsNone(status["quota"]["image"]["remaining"])
        self.gateway.quota.assert_not_awaited()

    async def test_configured_but_unavailable_runtime_retains_safe_account_summary(self):
        self.dispatcher._accounts.clear()
        self.runtime._started = False
        self.runtime.last_error_code = "session_unavailable"
        response = await self.client.get("/api/admin/vids/accounts/detail")
        detail = response.json()
        self.assertEqual(detail["configured_count"], 1)
        self.assertFalse(detail["runtime_ready"])
        self.assertEqual(detail["runtime_error"], "session_unavailable")
        self.assertEqual(detail["accounts"][0]["email"], self.token.email)
        self.assertEqual(detail["accounts"][0]["session_state"], "unavailable")
        self.assertIsNone(detail["accounts"][0]["vids_identity"])
        self.assertFalse(detail["accounts"][0]["identity_verified"])
        self.assertNotIn("private-", json.dumps(detail))
        self.assertEqual(response.headers["cache-control"], "no-store")
        status = (await self.client.get("/api/admin/vids/status")).json()
        self.assertTrue(status["configured"])
        self.assertEqual(status["available"]["image"], 0)
        denied = await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
        self.assertEqual(denied.status_code, 503)
        self.gateway.quota.assert_not_awaited()

    async def test_ready_account_identity_is_separate_from_flow_email(self):
        self.binding.gateway.identity = "vids@example.test"
        detail = (await self.client.get("/api/admin/vids/accounts/detail")).json()
        account = detail["accounts"][0]
        self.assertEqual(account["email"], self.token.email)
        self.assertEqual(account["vids_identity"], "vids@example.test")
        self.assertTrue(account["identity_verified"])
        self.assertEqual(account["session_state"], "ready")
        self.assertEqual(detail["configured_count"], 1)
        self.assertEqual(detail["connected_count"], 1)
        # Losing the Vids authentication removes the verified identity even
        # though the associated Flow token remains known and enabled.
        self.ready = False
        self.runtime._started = False
        for error, state in (
            ("session_authentication_failed", "authentication_expired"),
            ("account_mismatch", "identity_mismatch"),
            ("session_unavailable", "unavailable"),
        ):
            self.runtime.last_error_code = error
            detail = (await self.client.get("/api/admin/vids/accounts/detail")).json()
            account = detail["accounts"][0]
            self.assertIsNone(account["vids_identity"])
            self.assertFalse(account["identity_verified"])
            self.assertEqual(account["session_state"], state)
            self.assertEqual(detail["connected_count"], 0)
            self.assertEqual(detail["configured_count"], 1)
            self.assertEqual(account["email"], self.token.email)

    async def test_failed_account_has_own_error_while_healthy_account_stays_available(self):
        self.binding.gateway.identity = "healthy-vids@example.test"
        self.settings["accounts"].append({"token_id": 7, "identity_hash": "private-other-identity"})
        for code, state in (
            ("session_authentication_failed", "authentication_expired"),
            ("session_identity_mismatch", "identity_mismatch"),
            ("session_unavailable", "unavailable"),
            ("profile_refresh_failed", "unavailable"),
        ):
            with self.subTest(code=code):
                self.runtime.account_errors = {4: code, 7: code, 9: "private-unrelated-error"}
                response = await self.client.get("/api/admin/vids/accounts/detail")
                detail = response.json()
                healthy, failed = detail["accounts"]
                self.assertTrue(detail["runtime_ready"])
                self.assertIsNone(detail["runtime_error"])
                self.assertEqual(detail["configured_count"], 2)
                self.assertEqual(detail["connected_count"], 1)
                self.assertEqual(healthy["token_id"], 4)
                self.assertTrue(healthy["ready"])
                self.assertTrue(healthy["identity_verified"])
                self.assertEqual(healthy["vids_identity"], "healthy-vids@example.test")
                self.assertEqual(healthy["session_state"], "ready")
                self.assertIsNone(healthy["runtime_error"])
                self.assertEqual(failed["token_id"], 7)
                self.assertFalse(failed["ready"])
                self.assertFalse(failed["identity_verified"])
                self.assertIsNone(failed["vids_identity"])
                self.assertEqual(failed["session_state"], state)
                self.assertEqual(failed["runtime_error"], code)
                self.assertNotIn("private-", response.text)
                status = (await self.client.get("/api/admin/vids/status")).json()
                self.assertTrue(status["runtime_ready"])
                self.assertEqual(status["available"], {"image": 1, "video": 1, "speech": 1})
        response = await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["last_quota"]["image"]["available"], 19)
        self.assertEqual(self.gateway.quota.await_count, 2)
        self.assert_no_generation_or_session_start()

    async def test_all_account_sessions_failed_does_not_become_global_runtime_error(self):
        self.dispatcher._accounts.clear()
        self.runtime.account_errors = {4: "session_authentication_failed"}
        detail = (await self.client.get("/api/admin/vids/accounts/detail")).json()
        self.assertTrue(detail["runtime_ready"])
        self.assertIsNone(detail["runtime_error"])
        self.assertEqual(detail["connected_count"], 0)
        self.assertEqual(detail["accounts"][0]["runtime_error"], "session_authentication_failed")
        self.assertEqual(detail["accounts"][0]["session_state"], "authentication_expired")
        self.gateway.quota.assert_not_awaited()

    async def test_quota_refresh_is_serial_read_only_and_uses_no_flow_credits(self):
        self.assertIsNone(
            (await self.client.get("/api/admin/vids/status")).json()["quota"]["image"]["remaining"]
        )
        self.gateway.quota.assert_not_awaited()
        response = await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
        self.assertEqual(response.status_code, 200)
        row = response.json()["account"]
        self.assertEqual(
            [call.args[0] for call in self.gateway.quota.await_args_list], ["image", "video"]
        )
        self.assertEqual(row["last_quota"]["image"]["available"], 19)
        self.assertEqual(row["last_quota"]["video"]["available"], 427)
        self.assertIsNotNone(row["last_quota"]["image"]["queried_at"])
        self.assertIsNone(row["last_quota"]["image"]["error"])
        status = (await self.client.get("/api/admin/vids/status")).json()
        self.assertEqual(status["quota"]["image"]["remaining"], 19)
        self.assertEqual(status["quota"]["image"]["covered"], 1)
        self.assertNotIn("token_id", json.dumps(status))
        self.assertNotIn(self.token.email, json.dumps(status))
        self.assert_no_generation_or_session_start()

    async def test_refresh_failure_keeps_old_quota_and_time_and_marks_stale(self):
        old = (await self.client.post("/api/admin/vids/accounts/4/quota-refresh")).json()[
            "account"
        ]["last_quota"]
        self.gateway.quota.side_effect = VidsTransportError("private-upstream-url-and-cookie")
        response = await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
        quota = response.json()["account"]["last_quota"]
        for kind in ("image", "video"):
            self.assertEqual(quota[kind]["available"], old[kind]["available"])
            self.assertEqual(quota[kind]["queried_at"], old[kind]["queried_at"])
            self.assertTrue(quota[kind]["stale"])
            self.assertEqual(quota[kind]["error"], "transport_failed")
        self.assertNotIn("private-", response.text)
        self.assertTrue(
            (await self.client.get("/api/admin/vids/status")).json()["quota"]["video"]["stale"]
        )
        self.assert_no_generation_or_session_start()

    async def test_first_refresh_failure_does_not_report_zero_quota(self):
        self.gateway.quota.side_effect = VidsProtocolError("private-details", 401)
        row = (await self.client.post("/api/admin/vids/accounts/4/quota-refresh")).json()["account"]
        self.assertIsNone(row["last_quota"]["image"]["available"])
        self.assertIsNone(row["last_quota"]["video"]["queried_at"])
        self.assertEqual(row["last_quota"]["image"]["error"], "session_authentication_failed")

    async def test_invalid_session_can_repair_before_reading_quota_without_generation(self):
        self.ready = False

        async def repair(token_id):
            self.assertEqual(token_id, 4)
            self.ready = True

        app = FastAPI()
        app.include_router(
            create_vids_admin_router(
                self.dispatcher,
                admin.verify_admin_token,
                runtime=self.runtime,
                get_settings=lambda: self.settings,
                repair_session=repair,
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.example.test"
        ) as http:
            http.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")
            response = await http.post("/api/admin/vids/accounts/4/quota-refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["last_quota"]["image"]["available"], 19)
        self.assertEqual(self.gateway.quota.await_count, 2)
        self.assert_no_generation_or_session_start()

    async def test_authentication_failure_repairs_at_most_once_and_retries_only_quota(self):
        repair = AsyncMock()
        values = [
            VidsProtocolError("quota", 401),
            await self.quota("image"),
            await self.quota("video"),
        ]
        self.gateway.quota.side_effect = values
        app = FastAPI()
        app.include_router(
            create_vids_admin_router(
                self.dispatcher,
                admin.verify_admin_token,
                runtime=self.runtime,
                get_settings=lambda: self.settings,
                repair_session=repair,
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.example.test"
        ) as http:
            http.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")
            response = await http.post("/api/admin/vids/accounts/4/quota-refresh")
        repair.assert_awaited_once_with(4)
        self.assertEqual(response.json()["account"]["last_quota"]["video"]["available"], 427)
        self.assertEqual(self.gateway.quota.await_count, 3)
        self.assert_no_generation_or_session_start()
        status = (await self.client.get("/api/admin/vids/status")).json()
        self.assertIsNone(status["quota"]["image"]["remaining"])
        self.assertEqual(status["quota"]["image"]["covered"], 0)

    async def test_non_ready_binding_and_unknown_id_never_refresh_upstream(self):
        response = await self.client.post("/api/admin/vids/accounts/9/quota-refresh")
        self.assertEqual(response.status_code, 404)
        self.ready = False
        response = await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
        self.assertEqual(response.status_code, 503)
        self.gateway.quota.assert_not_awaited()

    async def test_refresh_cancellation_propagates_and_releases_manual_refresh_lock(self):
        self.gateway.quota.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
        self.gateway.quota.side_effect = self.quota
        response = await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
        self.assertEqual(response.status_code, 200)

    async def test_automatic_refresh_shares_cache_and_retry_interval_between_pages(self):
        with patch("src.api.admin_routes.vids.time.monotonic", return_value=100) as clock:
            response = await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
            self.assertEqual(response.json()["accounts"][0]["last_quota"]["image"]["available"], 19)
            status = (await self.client.get("/api/admin/vids/status?refresh=true")).json()
            self.assertEqual(self.gateway.quota.await_count, 2)
            self.assertEqual(status["quota"]["image"]["total"], 20)
            self.assertEqual(status["quota"]["image"]["eligible"], 1)
            old = response.json()["accounts"][0]["last_quota"]

            clock.return_value = 161
            self.gateway.quota.side_effect = VidsTransportError("private-upstream")
            response = await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
            quota = response.json()["accounts"][0]["last_quota"]
            self.assertEqual(self.gateway.quota.await_count, 4)
            for kind in ("image", "video"):
                self.assertEqual(quota[kind]["available"], old[kind]["available"])
                self.assertEqual(quota[kind]["queried_at"], old[kind]["queried_at"])
                self.assertEqual(quota[kind]["error"], "transport_failed")
                self.assertTrue(quota[kind]["stale"])
            await self.client.get("/api/admin/vids/status?refresh=true")
            self.assertEqual(self.gateway.quota.await_count, 4)
            clock.return_value = 222
            await self.client.get("/api/admin/vids/status?refresh=true")
            self.assertEqual(self.gateway.quota.await_count, 6)
        self.assert_no_generation_or_session_start()

    async def test_concurrent_automatic_refreshes_query_each_kind_only_once(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def quota(kind):
            if kind == "image":
                entered.set()
                await release.wait()
            return await self.quota(kind)

        self.gateway.quota.side_effect = quota
        detail = asyncio.create_task(
            self.client.get("/api/admin/vids/accounts/detail?refresh=true")
        )
        await entered.wait()
        status = asyncio.create_task(self.client.get("/api/admin/vids/status?refresh=true"))
        await asyncio.sleep(0)
        self.assertEqual(self.gateway.quota.await_count, 1)
        release.set()
        detail_response, status_response = await asyncio.gather(detail, status)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(status_response.json()["quota"]["video"]["remaining"], 427)
        self.assertEqual(self.gateway.quota.await_count, 2)
        self.assert_no_generation_or_session_start()

    async def test_manual_refresh_bypasses_ttl_and_warms_automatic_cache(self):
        with patch("src.api.admin_routes.vids.time.monotonic", return_value=100):
            for _ in range(2):
                response = await self.client.post("/api/admin/vids/accounts/4/quota-refresh")
                self.assertEqual(response.status_code, 200)
            self.assertEqual(self.gateway.quota.await_count, 4)
            await self.client.get("/api/admin/vids/status?refresh=true")
            self.assertEqual(self.gateway.quota.await_count, 4)

    async def test_automatic_refresh_interval_starts_after_slow_upstream_completes(self):
        with patch("src.api.admin_routes.vids.time.monotonic", return_value=100) as clock:

            async def slow_quota(kind):
                clock.return_value += 60
                return await self.quota(kind)

            self.gateway.quota.side_effect = slow_quota
            await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
            await self.client.get("/api/admin/vids/status?refresh=true")
            self.assertEqual(self.gateway.quota.await_count, 2)

    async def test_cached_quota_becomes_stale_without_implicitly_querying_upstream(self):
        observed = datetime(2026, 10, 15, tzinfo=timezone.utc)
        with patch("src.api.admin_routes.vids.datetime", wraps=datetime) as clock:
            clock.now.return_value = observed
            await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
            clock.now.return_value = observed + timedelta(seconds=60)
            detail = (await self.client.get("/api/admin/vids/accounts/detail")).json()
            self.assertTrue(detail["accounts"][0]["last_quota"]["image"]["stale"])
            status = (await self.client.get("/api/admin/vids/status")).json()
            self.assertTrue(status["quota"]["video"]["stale"])
            self.assertEqual(self.gateway.quota.await_count, 2)

    async def test_automatic_refresh_skips_disabled_unavailable_and_busy_accounts(self):
        self.token.is_active = False
        await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
        self.token.is_active = True
        self.ready = False
        await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
        self.ready = True
        self.gateway.lock = asyncio.Lock()
        async with self.gateway.lock:
            await self.client.get("/api/admin/vids/status?refresh=true")
        self.gateway.quota.assert_not_awaited()
        self.token.video_enabled = False
        response = await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
        self.gateway.quota.assert_awaited_once_with("image")
        self.assertIsNone(response.json()["accounts"][0]["last_quota"]["video"]["available"])
        self.assert_no_generation_or_session_start()

    async def test_automatic_authentication_failure_never_repairs_sessions(self):
        repair = AsyncMock()
        app = FastAPI()
        app.include_router(
            create_vids_admin_router(
                self.dispatcher,
                admin.verify_admin_token,
                runtime=self.runtime,
                get_settings=lambda: self.settings,
                repair_session=repair,
            )
        )
        self.gateway.quota.side_effect = VidsProtocolError("private-credentials", 401)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.example.test"
        ) as http:
            http.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")
            response = await http.get("/api/admin/vids/accounts/detail?refresh=true")
        repair.assert_not_awaited()
        self.assertEqual(response.status_code, 200)
        quota = response.json()["accounts"][0]["last_quota"]
        self.assertIsNone(quota["image"]["available"])
        self.assertEqual(quota["image"]["error"], "session_authentication_failed")
        self.assertNotIn("private-", response.text)
        self.assert_no_generation_or_session_start()

    async def test_active_quota_excludes_disabled_account_and_capability_history(self):
        await self.client.get("/api/admin/vids/accounts/detail?refresh=true")
        status = (await self.client.get("/api/admin/vids/status")).json()
        image = status["quota"]["image"]
        self.assertEqual(image["active_remaining"], 19)
        self.assertEqual(image["active_total"], 20)
        self.assertEqual(image["active_covered"], 1)
        self.assertFalse(image["active_stale"])
        self.assertIsNotNone(image["active_queried_at"])

        self.token.image_enabled = False
        status = (await self.client.get("/api/admin/vids/status")).json()
        image = status["quota"]["image"]
        self.assertEqual(image["remaining"], 19)
        self.assertEqual(image["covered"], 1)
        self.assertIsNone(image["active_remaining"])
        self.assertIsNone(image["active_total"])
        self.assertEqual(image["active_covered"], 0)
        self.assertEqual(image["eligible"], 0)
        self.assertIsNone(image["active_queried_at"])
        self.assertEqual(status["quota"]["video"]["active_remaining"], 427)

        self.token.is_active = False
        status = (await self.client.get("/api/admin/vids/status")).json()
        for kind in ("image", "video"):
            quota = status["quota"][kind]
            self.assertIsNone(quota["active_remaining"])
            self.assertIsNone(quota["active_total"])
            self.assertEqual(quota["active_covered"], 0)
            self.assertFalse(quota["active_stale"])
            self.assertEqual(quota["eligible"], 0)
            self.assertIsNone(quota["active_queried_at"])
        self.assertEqual(status["quota"]["video"]["remaining"], 427)
        self.assertEqual(self.gateway.quota.await_count, 2)


if __name__ == "__main__":
    unittest.main()
