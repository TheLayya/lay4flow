"""Existing Profile onboarding, persistence and restart without real upstream calls."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException, Request

from src.api.admin_routes.vids import create_vids_admin_router
from src.api.admin_routes.vids_onboarding import create_vids_onboarding_router
from src.services.vids.bindings import read_bindings, session_key_file, write_bindings
from src.services.vids.client import VidsProtocolError
from src.services.vids.deletion_markers import is_deleted
from src.services.vids.dispatcher import VidsProviderDispatcher
from src.services.vids.gateway import GatewayError
from src.services.vids.onboarding import (
    OnboardingError,
    VidsOnboardingService,
    inspect_profile_page,
)
from src.services.vids.runtime import VidsRuntime
from tests.test_vids_persistence_gateway import client, cookies


class VidsOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.tokens = {
            1: SimpleNamespace(id=1, email="synthetic-one@example.test", proxy_url=""),
            2: SimpleNamespace(id=2, email="synthetic-two@example.test", proxy_url=""),
        }
        self.db = SimpleNamespace(
            get_token=AsyncMock(side_effect=self.tokens.get),
            get_all_tokens=AsyncMock(side_effect=lambda: list(self.tokens.values())),
        )
        self.proxy = SimpleNamespace(get_request_proxy_url=AsyncMock(return_value=None))
        self.dispatcher = VidsProviderDispatcher(self.db)
        self.runtime = VidsRuntime(self.dispatcher, self.proxy)
        await self.runtime.start({})
        self.page = SimpleNamespace(close=AsyncMock())
        self.context = SimpleNamespace(
            new_page=AsyncMock(return_value=self.page), cookies=AsyncMock(return_value=cookies())
        )
        self.session_calls = []

        @asynccontextmanager
        async def session(email, **options):
            self.session_calls.append((email, options))
            yield self.context

        self.manager = SimpleNamespace(
            spec=AsyncMock(return_value=SimpleNamespace(profile_dir=self.root / "profile")),
            session=session,
            backend=SimpleNamespace(options={"headless": True}),
        )
        self.service = VidsOnboardingService(
            self.db,
            self.runtime,
            self.manager,
            registry_path=self.root / "vids" / "bindings.json",
            secrets_dir=self.root / "vids-secrets",
            runtime_dir=self.root / "vids" / "accounts",
        )
        self.service.verifier.verify_environment = AsyncMock(return_value="environment_stable")
        self.upstreams = []

        def upstream(editor, rows, *, auth_user, proxy):
            identifier = 1 if "/one/" in editor else 2
            result = client(rows, identity=self.tokens[identifier].email)
            result.editor_url, result.auth_user, result.proxy = editor, auth_user, proxy
            result.initialization_valid = True
            self.upstreams.append(result)
            return result

        self.factory = patch("src.services.vids.onboarding.VidsClient", side_effect=upstream)
        self.factory.start()
        self.resume_factory = patch("src.services.vids.gateway.VidsClient", side_effect=upstream)
        self.resume_factory.start()

        async def discover(page, email, **options):
            name = "one" if email == self.tokens[1].email else "two"
            return f"https://docs.google.com/videos/d/{name}/edit?authuser=0", 0

        self.discover = patch(
            "src.services.vids.onboarding.inspect_profile_page", side_effect=discover
        )
        self.discover.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.discover.stop()
        self.resume_factory.stop()
        self.factory.stop()
        self.directory.cleanup()

    async def test_delete_cleans_orphan_binding_session_marker_and_runtime_without_sibling(self):
        await self.service.onboard(1)
        await self.service.onboard(2)
        root = self.service.runtime_dir / "1"
        sibling = self.service.runtime_dir / "2"
        key = self.service.secrets_dir / "account1.key"
        marker = self.service.registry_path.parent / "auto-onboarding" / "1"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text("{}")
        self.runtime.account_errors[1] = "account_not_found"
        self.dispatcher._claims["synthetic"] = 1
        self.dispatcher._document_refs["synthetic"] = (1, "shape")
        self.tokens.pop(1)

        await self.service.delete_account(1)
        await self.service.delete_account(1)

        self.assertEqual(
            [row["token_id"] for row in read_bindings(self.service.registry_path)], [2]
        )
        self.assertFalse(root.exists())
        self.assertFalse(key.exists())
        self.assertFalse(marker.exists())
        self.assertTrue(sibling.exists())
        self.assertEqual(set(self.dispatcher._accounts), {2})
        self.assertNotIn(1, self.runtime.account_errors)
        self.assertNotIn(1, self.runtime.binding_rows)
        self.assertNotIn("synthetic", self.dispatcher._claims)
        self.assertNotIn("synthetic", self.dispatcher._document_refs)

    async def test_delete_busy_account_preserves_binding_and_files(self):
        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        async with gateway.lock:
            with self.assertRaises(GatewayError) as caught:
                await self.service.delete_account(1)
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue((self.service.runtime_dir / "1").exists())
        self.assertEqual(len(read_bindings(self.service.registry_path)), 1)

    async def test_delete_does_not_follow_custom_shared_runtime_or_key(self):
        shared = self.root / "shared-runtime"
        shared.mkdir()
        (shared / "keep").write_text("other account")
        shared_key = self.root / "shared.key"
        shared_key.write_text("keep")
        write_bindings(
            self.service.registry_path,
            [{"token_id": 1, "runtime_dir": str(shared), "session_key_file": str(shared_key)}],
        )
        await self.service.delete_account(1)
        self.assertEqual((shared / "keep").read_text(), "other account")
        self.assertEqual(shared_key.read_text(), "keep")

    async def test_deleted_static_binding_stays_absent_after_restart_and_admin_listing(self):
        await self.service.onboard(1)
        static = read_bindings(self.service.registry_path)[0]
        settings = {"accounts": [static], "registry_path": str(self.service.registry_path)}
        await self.service.delete_account(1)
        self.assertTrue(is_deleted(self.service.registry_path, 1))
        await self.runtime.close()
        await self.runtime.start(settings)
        self.assertEqual(self.runtime.binding_rows, {})
        self.assertEqual(self.runtime.account_errors, {})
        self.assertEqual(self.dispatcher._accounts, {})

        async def authenticate():
            return "admin"

        app = FastAPI()
        app.include_router(
            create_vids_admin_router(
                self.dispatcher, authenticate, get_settings=lambda: settings, runtime=self.runtime
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get("/api/admin/vids/accounts/detail")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accounts"], [])

        await self.service.onboard(1)
        self.assertFalse(is_deleted(self.service.registry_path, 1))

    async def test_ready_binding_persists_only_metadata_and_restores_without_profile(self):
        result = await self.service.onboard(1, create_blank=True)
        self.assertEqual(result, {"token_id": 1, "state": "ready", "identity_verified": True})
        self.assertEqual(set(self.dispatcher._accounts), {1})
        rows = read_bindings(self.service.registry_path)
        self.assertEqual(
            rows[0]["identity_hash"], hashlib.sha256(self.tokens[1].email.encode()).hexdigest()
        )
        self.assertNotIn("cookie", self.service.registry_path.read_text())
        encrypted = Path(rows[0]["runtime_dir"]).joinpath(".session.enc").read_bytes()
        self.assertNotIn(b"synthetic-initial-cookie", encrypted)
        self.assertEqual(self.session_calls[0][1]["existing_only"], True)
        self.page.close.assert_awaited_once()
        await self.runtime.close()
        await self.runtime.start({"registry_path": str(self.service.registry_path), "accounts": []})
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertEqual(len(self.session_calls), 1)

    async def test_failed_second_account_does_not_close_first_or_publish_invalid_binding(self):
        await self.service.onboard(1)
        first = self.dispatcher._accounts[1].gateway
        self.service._identity = AsyncMock(return_value="wrong-identity@example.test")
        with self.assertRaises(OnboardingError) as caught:
            await self.service.onboard(2)
        self.assertEqual(caught.exception.code, "identity_mismatch")
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertTrue(first._ready)
        self.assertEqual(len(read_bindings(self.service.registry_path)), 1)

    async def test_disabled_account_cannot_open_profile_or_create_session_material(self):
        self.tokens[1].is_active = False
        with self.assertRaises(OnboardingError) as caught:
            await self.service.onboard(1, create_blank=True)
        self.assertEqual(caught.exception.code, "account_disabled")
        self.assertEqual(self.session_calls, [])
        self.manager.spec.assert_not_awaited()
        self.assertEqual(self.upstreams, [])
        self.assertFalse(self.service.registry_path.exists())
        self.assertFalse(self.service.secrets_dir.exists())
        self.assertEqual(self.dispatcher._accounts, {})

    async def test_account_disabled_during_start_closes_candidate_without_publishing_and_preserves_existing_pool(
        self,
    ):
        await self.service.onboard(1)
        original = self.dispatcher._accounts[1].gateway
        old_registry = self.service.registry_path.read_bytes()
        roots, claims = dict(self.dispatcher._roots), dict(self.dispatcher._claims)
        calls = 0

        async def token_lookup(identifier):
            nonlocal calls
            if identifier == 2:
                calls += 1
                if calls == 2:
                    self.tokens[2].is_active = False
            return self.tokens.get(identifier)

        self.db.get_token.side_effect = token_lookup
        with self.assertRaises(OnboardingError) as caught:
            await self.service.onboard(2, create_blank=True)
        self.assertEqual(caught.exception.code, "account_disabled")
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertIs(self.dispatcher._accounts[1].gateway, original)
        self.assertTrue(original._ready)
        original.client.session.close.assert_not_awaited()
        self.assertEqual(self.service.registry_path.read_bytes(), old_registry)
        self.assertEqual(self.dispatcher._roots, roots)
        self.assertEqual(self.dispatcher._claims, claims)
        self.assertEqual(self.runtime._gateways, [original])
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        self.assertTrue((self.service.runtime_dir / "2" / ".session.enc").is_file())
        self.upstreams[-1].session.close.assert_awaited_once()

    async def test_login_failure_closes_page_and_does_not_create_keys_or_registry(self):
        self.discover.stop()
        self.discover = patch(
            "src.services.vids.onboarding.inspect_profile_page",
            side_effect=OnboardingError(409, "login_required", "Google login is required"),
        )
        self.discover.start()
        with self.assertRaises(OnboardingError) as caught:
            await self.service.onboard(1, create_blank=True)
        self.assertEqual(caught.exception.code, "login_required")
        self.assertFalse(self.service.registry_path.exists())
        self.assertFalse(self.service.secrets_dir.exists())
        self.page.close.assert_awaited_once()

    async def test_candidates_exclude_registered_accounts_and_already_bound_does_not_open_profile(
        self,
    ):
        await self.service.onboard(1)
        rows = await self.service.candidates()
        self.assertEqual([row["token_id"] for row in rows], [2])
        with self.assertRaises(OnboardingError) as caught:
            await self.service.onboard(1)
        self.assertEqual(caught.exception.code, "already_bound")
        self.assertEqual(len(self.session_calls), 1)

    async def test_candidates_exclude_configured_account_when_startup_failure_clears_dispatcher(
        self,
    ):
        await self.runtime.close()
        self.runtime.last_error_code = "session_authentication_failed"
        self.service.get_settings = lambda: {"accounts": [{"token_id": 1}]}
        rows = await self.service.candidates()
        self.assertEqual([row["token_id"] for row in rows], [2])
        self.assertEqual(self.dispatcher._accounts, {})
        self.assertEqual(self.session_calls, [])

    async def test_failed_configured_binding_is_restored_from_startup_snapshot_only(self):
        await self.service.onboard(1)
        row = read_bindings(self.service.registry_path)[0]
        await self.runtime.close()
        await self.runtime.start({})
        self.runtime.binding_rows = {1: dict(row)}
        self.runtime.account_errors[1] = "session_authentication_failed"
        self.runtime.dispatcher.unavailable_receipt_roots = {1: Path(row["runtime_dir"])}
        calls = len(self.session_calls)
        with self.assertRaises(OnboardingError) as caught:
            await self.service.onboard(1, create_blank=True)
        self.assertEqual(caught.exception.code, "already_bound")
        self.assertEqual(len(self.session_calls), calls)
        changed = {**row, "runtime_dir": str(self.root / "wrong-runtime")}
        write_bindings(self.service.registry_path, [changed])
        self.service.get_settings = lambda: {"accounts": [changed]}
        result = await self.service.reconnect(1)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(
            self.dispatcher._accounts[1].gateway.root, Path(row["runtime_dir"]).resolve()
        )
        self.assertFalse(Path(changed["runtime_dir"]).exists())
        self.assertEqual(self.runtime.binding_rows[1], row)

    async def test_registry_write_failure_closes_candidate_and_releases_runtime_lock(self):
        with patch(
            "src.services.vids.onboarding.write_bindings", side_effect=OSError("private path")
        ):
            with self.assertRaises(OnboardingError) as caught:
                await self.service.onboard(1)
        self.assertEqual(caught.exception.code, "onboarding_failed")
        self.assertEqual(self.dispatcher._accounts, {})
        self.assertEqual(self.runtime._gateways, [])
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        self.assertIsNone(self.runtime.last_error_code)
        self.assertTrue(all(upstream.session.close.await_count for upstream in self.upstreams))

    async def test_registry_duplicate_preserves_whole_pool_validation_and_key_file_is_external(
        self,
    ):
        await self.service.onboard(1)
        rows = read_bindings(self.service.registry_path)
        await self.runtime.close()
        with self.assertRaises(GatewayError) as caught:
            await self.runtime.start(
                {"accounts": rows, "registry_path": str(self.service.registry_path)}
            )
        self.assertEqual(caught.exception.code, "invalid_configuration")
        self.assertEqual(self.dispatcher._accounts, {})
        write_bindings(self.service.registry_path, [])
        bad = {**rows[0], "session_key_env": "SYNTHETIC_KEY"}
        with self.assertRaises(GatewayError):
            await self.runtime.start({"accounts": [bad]})
        self.assertEqual(len(session_key_file(rows[0]["session_key_file"])), 44)

    async def test_reconnect_invalid_session_preserves_gateway_affinity_and_receipts(self):
        await self.service.onboard(1)
        await self.service.onboard(2)
        gateway = self.dispatcher._accounts[1].gateway
        old = gateway.client
        old.initialization_valid = False
        receipt = gateway.root / ("vids_" + "1" * 64 + ".json")
        receipt.write_text('{"status":"unknown","kind":"image","error":"acceptance_unknown"}')
        saved_receipt = receipt.read_bytes()
        roots, claims = dict(self.dispatcher._roots), dict(self.dispatcher._claims)
        result = await self.service.reconnect(1)
        self.assertEqual(result, {"token_id": 1, "state": "ready", "identity_verified": True})
        self.assertIs(self.dispatcher._accounts[1].gateway, gateway)
        self.assertIsNot(gateway.client, old)
        self.assertTrue(gateway.client.initialization_valid)
        self.assertEqual(self.dispatcher._roots, roots)
        self.assertEqual(self.dispatcher._claims, claims)
        self.assertEqual(receipt.read_bytes(), saved_receipt)
        self.assertTrue(self.dispatcher._accounts[2].gateway._ready)
        old.session.close.assert_awaited_once()
        self.assertTrue(
            all(not upstream.generate_speech.await_count for upstream in self.upstreams)
        )

    async def test_failed_reconnect_keeps_original_client_and_encrypted_session(self):
        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        old = gateway.client
        encrypted = gateway.session_store.path.read_bytes()
        candidate = client(identity="wrong@example.test")
        with patch("src.services.vids.gateway.VidsClient", return_value=candidate):
            with self.assertRaises(OnboardingError) as caught:
                await self.service.reconnect(1)
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertIs(gateway.client, old)
        self.assertEqual(gateway.session_store.path.read_bytes(), encrypted)
        old.session.close.assert_not_awaited()
        candidate.session.close.assert_awaited_once()

    async def test_failed_startup_target_recovery_does_not_block_healthy_generation(self):
        self.tokens[1].is_active = self.tokens[2].is_active = True
        await self.service.onboard(1)
        await self.service.onboard(2)
        await self.runtime.close()
        settings = {"registry_path": str(self.service.registry_path), "accounts": []}
        self.service.get_settings = lambda: settings
        good = client(identity=self.tokens[1].email)
        good.editor_url = "https://docs.google.com/videos/d/one/edit?authuser=0"
        bad = client(identity=self.tokens[2].email)
        bad.editor_url = "https://docs.google.com/videos/d/two/edit?authuser=0"
        bad.bootstrap.side_effect = VidsProtocolError("synthetic expired session", 401)
        with patch("src.services.vids.gateway.VidsClient", side_effect=[good, bad]):
            await self.runtime.start(settings)
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertEqual(self.runtime.account_errors, {2: "session_authentication_failed"})
        entered, release = asyncio.Event(), asyncio.Event()

        async def unavailable_profile(*args):
            entered.set()
            await release.wait()
            raise OnboardingError(409, "login_required", "Synthetic login required")

        self.service._profile_cookies = AsyncMock(side_effect=unavailable_profile)
        original = self.dispatcher._accounts[1].gateway
        repair = asyncio.create_task(self.service.reconnect(2))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertTrue(self.runtime._lifecycle_lock.locked())
        try:
            result = await self.dispatcher.generate(
                1, {"kind": "speech", "prompt": "Hello"}, "healthy-during-recovery"
            )
            self.assertEqual(result["status"], "completed")
        finally:
            release.set()
            with self.assertRaises(GatewayError):
                await asyncio.wait_for(repair, 2)
        self.assertIs(self.dispatcher._accounts[1].gateway, original)
        self.assertTrue(original._ready)
        good.session.close.assert_not_awaited()
        good.generate_speech.assert_awaited_once()
        bad.generate_speech.assert_not_awaited()

    async def test_reconnect_busy_and_pin_mismatch_stop_before_profile(self):
        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        calls = len(self.session_calls)
        await gateway.lock.acquire()
        try:
            with self.assertRaises(OnboardingError) as caught:
                await self.service.reconnect(1)
            self.assertEqual(caught.exception.code, "busy")
        finally:
            gateway.lock.release()
        (gateway.root / ".account").write_text("a" * 64)
        with self.assertRaises(GatewayError) as caught:
            await self.service.reconnect(1)
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertEqual(len(self.session_calls), calls)

    async def test_reconnect_authentication_failure_reports_login_without_replacing_session(self):
        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        old = gateway.client
        candidate = client(identity=self.tokens[1].email)
        candidate.bootstrap.side_effect = VidsProtocolError("private-url", 401)
        with patch("src.services.vids.gateway.VidsClient", return_value=candidate):
            with self.assertRaises(OnboardingError) as caught:
                await self.service.reconnect(1)
        self.assertEqual(caught.exception.code, "login_required")
        self.assertNotIn("private", str(caught.exception))
        self.assertIs(gateway.client, old)
        candidate.session.close.assert_awaited_once()

    async def test_reconnect_does_not_replace_when_generation_acquires_lock_during_profile_read(
        self,
    ):
        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        old = gateway.client

        async def page_closed():
            await gateway.lock.acquire()

        self.page.close.side_effect = page_closed
        try:
            with self.assertRaises(OnboardingError) as caught:
                await self.service.reconnect(1)
            self.assertEqual(caught.exception.code, "busy")
            self.assertIs(gateway.client, old)
            old.session.close.assert_not_awaited()
        finally:
            gateway.lock.release()
            self.page.close.side_effect = None

    async def test_reconnect_cancellation_preserves_original_session_and_releases_lifecycle_lock(
        self,
    ):
        import asyncio

        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        old = gateway.client
        candidate = client(identity=self.tokens[1].email)
        started = asyncio.Event()
        release = asyncio.Event()

        async def bootstrap():
            started.set()
            await release.wait()

        candidate.bootstrap.side_effect = bootstrap
        with patch("src.services.vids.gateway.VidsClient", return_value=candidate):
            pending = asyncio.create_task(self.service.reconnect(1))
            await started.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertIs(gateway.client, old)
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        self.assertFalse(gateway.lock.locked())
        old.session.close.assert_not_awaited()
        candidate.session.close.assert_awaited_once()

    async def test_quota_auth_failure_failed_repair_then_next_refresh_recovers_without_generation(
        self,
    ):
        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        old = gateway.client
        self.tokens[1].is_active = self.tokens[1].image_enabled = self.tokens[1].video_enabled = (
            True
        )
        receipt = gateway.root / ("vids_" + "1" * 64 + ".json")
        receipt.write_text('{"status":"unknown","kind":"image","error":"acceptance_unknown"}')
        saved_receipt = receipt.read_bytes()

        async def failed_quota(kind):
            old.initialization_valid = False
            raise VidsProtocolError("private quota/editor URL", 401)

        old.quota.side_effect = failed_quota
        app = FastAPI()

        async def authenticate():
            return True

        app.include_router(
            create_vids_admin_router(
                self.dispatcher,
                authenticate,
                runtime=self.runtime,
                get_settings=lambda: {"registry_path": str(self.service.registry_path)},
                repair_session=self.service.reconnect,
            )
        )
        failed_candidate = client(identity=self.tokens[1].email)
        failed_candidate.bootstrap.side_effect = VidsProtocolError("private editor URL", 401)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        ) as http:
            with patch("src.services.vids.gateway.VidsClient", return_value=failed_candidate):
                first = await http.post("/api/admin/vids/accounts/1/quota-refresh")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(
                first.json()["account"]["last_quota"]["image"]["error"], "login_required"
            )
            self.assertFalse(first.json()["account"]["identity_verified"])
            self.assertIs(gateway.client, old)
            old.session.close.assert_not_awaited()
            self.assertEqual(receipt.read_bytes(), saved_receipt)
            second = await http.post("/api/admin/vids/accounts/1/quota-refresh")
            self.assertEqual(second.status_code, 200, second.text)
            self.assertTrue(second.json()["account"]["identity_verified"])
            self.assertEqual(second.json()["account"]["last_quota"]["image"]["available"], 19)
            self.assertIsNone(second.json()["account"]["last_quota"]["video"]["error"])
            self.assertNotIn("private", first.text + second.text)
        self.assertIsNot(gateway.client, old)
        self.assertEqual(receipt.read_bytes(), saved_receipt)
        failed_candidate.session.close.assert_awaited_once()
        old.session.close.assert_awaited_once()
        self.assertTrue(
            all(not upstream.generate_speech.await_count for upstream in self.upstreams)
        )

    async def test_startup_unavailable_without_settings_cannot_mutate_saved_session(
        self,
    ):
        await self.service.onboard(1)
        gateway = self.dispatcher._accounts[1].gateway
        encrypted = gateway.session_store.path.read_bytes()
        session_path = gateway.session_store.path
        await self.runtime.close()
        self.runtime.last_error_code = "session_authentication_failed"
        calls = len(self.session_calls)
        with self.assertRaises(OnboardingError) as caught:
            await self.service.reconnect(1)
        self.assertEqual(caught.exception.code, "runtime_unavailable")
        self.assertFalse(self.runtime._started)
        self.assertEqual(self.dispatcher._accounts, {})
        self.assertEqual(session_path.read_bytes(), encrypted)
        self.assertEqual(len(self.session_calls), calls)

    async def test_startup_authentication_failure_repairs_original_binding_and_restores_whole_pool(
        self,
    ):
        await self.service.onboard(1)
        await self.service.onboard(2)
        roots = dict(self.dispatcher._roots)
        await self.runtime.close()
        self.runtime.last_error_code = "session_authentication_failed"
        self.service.get_settings = lambda: {
            "accounts": [],
            "registry_path": str(self.service.registry_path),
        }
        result = await self.service.reconnect(1)
        self.assertEqual(result, {"token_id": 1, "state": "ready", "identity_verified": True})
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.assertEqual(self.dispatcher._roots, roots)
        self.assertTrue(self.runtime._started)
        self.assertIsNone(self.runtime.last_error_code)
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        self.assertTrue(
            all(not upstream.generate_speech.await_count for upstream in self.upstreams)
        )

    async def test_explicit_startup_repair_retries_after_profile_refresh_failure(self):
        await self.service.onboard(1)
        await self.service.onboard(2)
        rows = read_bindings(self.service.registry_path)
        roots = dict(self.dispatcher._roots)
        registry = self.service.registry_path.read_bytes()
        stores = {
            row["token_id"]: self.dispatcher._accounts[row["token_id"]].gateway.session_store
            for row in rows
        }
        encrypted = {identifier: store.path.read_bytes() for identifier, store in stores.items()}
        await self.runtime.close()
        await self.runtime.start({})
        self.runtime.binding_rows = {row["token_id"]: dict(row) for row in rows}
        self.runtime.account_errors[1] = "session_authentication_failed"
        self.service.get_settings = lambda: {
            "accounts": [],
            "registry_path": str(self.service.registry_path),
        }
        with (
            patch.object(
                self.service,
                "_profile_cookies",
                side_effect=[
                    RuntimeError("synthetic profile failure"),
                    cookies("synthetic-renewed"),
                ],
            ) as profile,
            patch("src.services.vids.onboarding.inspect_profile_page") as discover,
            patch.object(self.runtime, "start", wraps=self.runtime.start) as start,
        ):
            with self.assertRaises(GatewayError) as caught:
                await self.service.reconnect(1)
            self.assertEqual(caught.exception.code, "profile_refresh_failed")
            self.assertEqual(self.runtime.account_errors[1], "profile_refresh_failed")
            self.assertIsNone(self.runtime.last_error_code)
            self.assertTrue(self.runtime._started)
            self.assertEqual(self.dispatcher._accounts, {})
            for identifier, store in stores.items():
                self.assertEqual(store.path.read_bytes(), encrypted[identifier])
            result = await self.service.reconnect(1)
            start.assert_not_awaited()
            self.assertEqual(profile.await_count, 2)
            discover.assert_not_called()
        self.assertEqual(result, {"token_id": 1, "state": "ready", "identity_verified": True})
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertEqual(self.dispatcher._roots[1], roots[1])
        self.assertEqual(self.service.registry_path.read_bytes(), registry)
        self.assertTrue(self.runtime._started)
        self.assertIsNone(self.runtime.last_error_code)
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        self.assertEqual(stores[1].load()["cookies"][0]["value"], "synthetic-renewed")
        self.assertEqual(stores[2].load()["cookies"][0]["value"], "synthetic-initial-cookie")
        self.assertTrue(
            all(not upstream.generate_speech.await_count for upstream in self.upstreams)
        )

    async def test_startup_repair_failure_keeps_saved_ciphertext_and_closes_all_candidates(self):
        await self.service.onboard(1)
        rows = read_bindings(self.service.registry_path)
        session = Path(rows[0]["runtime_dir"]) / ".session.enc"
        encrypted = session.read_bytes()
        await self.runtime.close()
        self.runtime.last_error_code = "session_authentication_failed"
        self.service.get_settings = lambda: {
            "accounts": [],
            "registry_path": str(self.service.registry_path),
        }
        bad = client(identity=self.tokens[1].email)
        bad.bootstrap.side_effect = VidsProtocolError("private editor URL", 401)
        with patch("src.services.vids.gateway.VidsClient", return_value=bad):
            with self.assertRaises(GatewayError) as caught:
                await self.service.reconnect(1)
        self.assertEqual(caught.exception.code, "session_authentication_failed")
        self.assertEqual(session.read_bytes(), encrypted)
        self.assertEqual(self.dispatcher._accounts, {})
        self.assertEqual(self.runtime._gateways, [])
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        self.assertEqual(bad.session.close.await_count, 2)

    async def test_other_startup_account_failure_keeps_healthy_account_and_repairs_only_target(
        self,
    ):
        await self.service.onboard(1)
        await self.service.onboard(2)
        await self.runtime.close()
        self.runtime.last_error_code = "session_authentication_failed"
        self.service.get_settings = lambda: {
            "accounts": [],
            "registry_path": str(self.service.registry_path),
        }
        good = client(identity=self.tokens[1].email)
        good.editor_url = "https://docs.google.com/videos/d/one/edit?authuser=0"
        bad = client(identity=self.tokens[2].email)
        bad.editor_url = "https://docs.google.com/videos/d/two/edit?authuser=0"
        bad.bootstrap.side_effect = VidsProtocolError("private editor URL", 401)
        with patch("src.services.vids.gateway.VidsClient", side_effect=[good, bad, bad]):
            with self.assertRaises(GatewayError):
                await self.service.reconnect(2)
        self.assertTrue(self.runtime._started)
        self.assertEqual(set(self.dispatcher._accounts), {1})
        original = self.dispatcher._accounts[1].gateway
        self.assertEqual(self.runtime._gateways, [original])
        good.session.close.assert_not_awaited()
        self.assertEqual(bad.session.close.await_count, 2)
        result = await self.service.reconnect(2)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.assertIs(self.dispatcher._accounts[1].gateway, original)
        good.session.close.assert_not_awaited()

    async def test_startup_repair_rejects_non_auth_state_unknown_account_and_invalid_configuration(
        self,
    ):
        await self.runtime.close()
        self.service.get_settings = lambda: {
            "accounts": [],
            "registry_path": str(self.service.registry_path),
        }
        for error in ("invalid_configuration", "account_mismatch", "session_identity_mismatch"):
            self.runtime.last_error_code = error
            with self.subTest(error=error), self.assertRaises(OnboardingError) as caught:
                await self.service.reconnect(1)
            self.assertEqual(caught.exception.code, "runtime_unavailable")
        self.runtime.last_error_code = "session_authentication_failed"
        with self.assertRaises(OnboardingError) as caught:
            await self.service.reconnect(1)
        self.assertEqual(caught.exception.code, "account_not_found")
        self.service.get_settings = lambda: {"accounts": [None]}
        with self.assertRaises(OnboardingError) as caught:
            await self.service.reconnect(1)
        self.assertEqual(caught.exception.code, "invalid_configuration")
        self.assertEqual(self.session_calls, [])

    async def test_route_requires_admin_auth_and_rejects_unknown_fields_before_profile(self):
        async def denied(request: Request):
            raise HTTPException(401, "Admin session required")

        app = FastAPI()
        app.include_router(create_vids_onboarding_router(denied, lambda: self.service))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        ) as http:
            response = await http.post("/api/admin/vids/onboard/1", json={"create_blank": True})
            self.assertEqual(response.status_code, 401)

        async def allow():
            return True

        app = FastAPI()
        app.include_router(create_vids_onboarding_router(allow, lambda: self.service))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        ) as http:
            for body in ({"cookie": "synthetic"}, {"create_blank": "true"}):
                response = await http.post("/api/admin/vids/onboard/1", json=body)
                self.assertEqual(response.status_code, 400)
            malformed = await http.post("/api/admin/vids/onboard/1", content="not json")
            self.assertEqual(malformed.status_code, 400)
        self.assertEqual(self.session_calls, [])


class ProfilePageTests(unittest.IsolatedAsyncioTestCase):
    identity = "synthetic@example.test"
    home = "https://docs.google.com/videos/?authuser=0"
    editor = "https://docs.google.com/videos/d/synthetic/edit?authuser=0"

    def response(self, *, identity="synthetic@example.test", html="", url=None, status=200):
        flags = {} if identity is None else {"docs-hue": identity}
        return SimpleNamespace(
            url=url or self.home,
            status=status,
            text=AsyncMock(return_value="_docs_flag_initialData=" + json.dumps(flags) + ";" + html),
        )

    def page(self, responses, *, navigation=None):
        def no_dom(*args, **kwargs):
            raise AssertionError("Identity and document discovery must use HTTP initialization")

        page = SimpleNamespace(
            url=self.home,
            context=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=responses))),
            locator=no_dom,
            evaluate=no_dom,
            content=no_dom,
            wait_for_timeout=no_dom,
        )

        async def goto(url, **options):
            response = navigation or self.response(url=self.editor)
            page.url = response.url
            return response

        page.goto = AsyncMock(side_effect=goto)
        return page

    async def test_http_initialization_reuses_existing_or_explicitly_creates_blank(self):
        page = self.page([self.response()])
        with self.assertRaises(OnboardingError) as caught:
            await inspect_profile_page(page, self.identity)
        self.assertEqual(caught.exception.code, "document_required")
        page.goto.assert_not_awaited()
        page = self.page([self.response()])
        editor, auth = await inspect_profile_page(page, self.identity, create_blank=True)
        self.assertEqual(auth, 0)
        self.assertEqual(editor, self.editor)
        page.goto.assert_awaited_once_with(
            "https://vids.new", wait_until="domcontentloaded", timeout=45000
        )
        page = self.page(
            [self.response(html='<a href="/videos/d/synthetic/edit?authuser=0">document</a>')]
        )
        self.assertEqual(await inspect_profile_page(page, self.identity.upper()), (editor, 0))
        page.context.request.get.assert_awaited_once_with(self.home, timeout=45000)
        page.goto.assert_not_awaited()

    async def test_home_identity_missing_or_mismatched_blocks_document_creation(self):
        for identity, expected in (
            (None, "identity_unavailable"),
            ("different@example.test", "identity_mismatch"),
        ):
            with self.subTest(identity=identity):
                page = self.page([self.response(identity=identity)])
                with self.assertRaises(OnboardingError) as caught:
                    await inspect_profile_page(page, self.identity, create_blank=True)
                self.assertEqual(caught.exception.code, expected)
                page.goto.assert_not_awaited()

    async def test_login_redirect_and_authentication_errors_require_login(self):
        for url, status in (
            ("https://accounts.google.com/signin/identifier", 200),
            (self.home, 401),
            (self.home, 403),
        ):
            with self.subTest(url=url, status=status):
                page = self.page([self.response(url=url, status=status)])
                with self.assertRaises(OnboardingError) as caught:
                    await inspect_profile_page(page, self.identity, create_blank=True)
                self.assertEqual(caught.exception.code, "login_required")
                page.goto.assert_not_awaited()

    async def test_stored_editor_requires_same_protocol_identity_and_document(self):
        for identity, url, expected in (
            (self.identity, self.editor, None),
            ("different@example.test", self.editor, "identity_mismatch"),
            (self.identity, self.home, "login_required"),
        ):
            with self.subTest(identity=identity, url=url):
                page = self.page([self.response(), self.response(identity=identity, url=url)])
                if expected is None:
                    self.assertEqual(
                        await inspect_profile_page(page, self.identity, editor_url=self.editor),
                        (self.editor, 0),
                    )
                else:
                    with self.assertRaises(OnboardingError) as caught:
                        await inspect_profile_page(page, self.identity, editor_url=self.editor)
                    self.assertEqual(caught.exception.code, expected)
                self.assertEqual(page.context.request.get.await_count, 2)
                self.assertEqual(page.context.request.get.await_args.args[0], self.editor)
                page.goto.assert_not_awaited()

    async def test_static_document_links_enforce_editor_url_whitelist(self):
        unsafe = (
            "https://attacker.example/videos/d/synthetic/edit",
            "http://docs.google.com/videos/d/synthetic/edit",
            "https://docs.google.com:8443/videos/d/synthetic/edit",
            "https://user@docs.google.com/videos/d/synthetic/edit",
            "/videos/d/synthetic/edit?authuser=0&amp;token=private",
            "/videos/d/synthetic/edit#private",
            "/document/d/synthetic/edit",
            "javascript:alert(1)",
        )
        page = self.page(
            [self.response(html="".join(f'<a href="{href}">document</a>' for href in unsafe))]
        )
        with self.assertRaises(OnboardingError) as caught:
            await inspect_profile_page(page, self.identity)
        self.assertEqual(caught.exception.code, "document_required")
        page.goto.assert_not_awaited()

    async def test_new_document_navigation_rechecks_protocol_identity(self):
        page = self.page(
            [self.response()],
            navigation=self.response(identity="different@example.test", url=self.editor),
        )
        with self.assertRaises(OnboardingError) as caught:
            await inspect_profile_page(page, self.identity, create_blank=True)
        self.assertEqual(caught.exception.code, "identity_mismatch")
        page.goto.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
