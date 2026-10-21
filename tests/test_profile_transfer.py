"""Synthetic native-state migration exercises real Profile locks and SQLite."""

import copy
import json
import unittest
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.api.admin_routes.profile_transfer import create_profile_transfer_router
from src.core.database import Database
from src.services.browser.cloak_backend import CloakBrowserBackend
from src.services.browser.environment import EnvironmentSnapshot
from src.services.browser.profile_transfer import (
    FORMAT,
    ProfileTransfer,
    ProfileTransferError,
    _scoped_state,
    transfer_error_detail,
)
from src.services.browser.profile_verifier import AccountVerification
from src.services.browser.profiles import ProfileManager, ProfileNotReadyError
from src.services.token_manager import TokenManager

RUNTIME = {"cloakbrowser": "0.synthetic", "playwright": "1.synthetic"}
EMAIL = "transfer@example.invalid"
ENVIRONMENT = EnvironmentSnapshot(
    "Synthetic Chromium", "en-US", ("en-US",), "UTC", "Linux", 1280, 720
)


def cookie(name="SID", domain=".google.com"):
    return {
        "name": name,
        "value": "synthetic",
        "domain": domain,
        "path": "/",
        "expires": -1,
        "secure": True,
        "httpOnly": True,
        "sameSite": "None",
    }


def bundle(active=True):
    return {
        "format": FORMAT,
        "version": 1,
        "exported_at": "2026-01-01T00:00:00Z",
        "account": {
            "email": EMAIL,
            "remark": "portable",
            "is_active": active,
            "auto_refresh_enabled": True,
            "image_enabled": True,
            "video_enabled": False,
            "image_concurrency": 1,
            "video_concurrency": -1,
        },
        "browser": {
            "fingerprint_seed": 46281,
            "launch_options": {"stealth_args": False},
            "environment": json.loads(ENVIRONMENT.to_json()),
            "runtime": {**RUNTIME, "chromium": "146.synthetic"},
        },
        "storage_state": {
            "cookies": [
                cookie(),
                cookie("__Secure-next-auth.session-token.0", "labs.google"),
                {**cookie("partitioned"), "partitionKey": "https://flow.google.com"},
            ],
            "origins": [
                {
                    "origin": "https://flow.google.com",
                    "localStorage": [{"name": "key", "value": "synthetic"}],
                    "indexedDB": [{"name": "login", "version": 1, "stores": []}],
                }
            ],
        },
    }


class Backend(CloakBrowserBackend):
    def __init__(self):
        super().__init__(headless=False, stealth_args=False)
        self.states = {}
        self.opens = 0
        self.restores = []

    async def open(self, profile_dir, *, proxy_url=None):
        self.opens += 1
        (profile_dir / "Default").mkdir(exist_ok=True)
        if proxy_url is not None or not (profile_dir / "launch_settings.sha256").exists():
            self._check_launch_settings(profile_dir, self._launch_options(profile_dir, proxy_url))
        state = self.states.setdefault(str(profile_dir), {"cookies": [], "origins": []})

        async def restore(value):
            self.restores.append(copy.deepcopy(value))
            self.states[str(profile_dir)] = copy.deepcopy(value)

        context = SimpleNamespace(
            browser=SimpleNamespace(version="146.synthetic"),
            set_storage_state=AsyncMock(side_effect=restore),
            storage_state=AsyncMock(
                side_effect=lambda **kw: copy.deepcopy(self.states[str(profile_dir)])
            ),
            cookies=AsyncMock(side_effect=lambda: self.states[str(profile_dir)]["cookies"]),
            profile_dir=profile_dir,
            state=state,
        )
        return context

    async def close(self, context):
        pass

    open_background = open_interactive = open


class ProfileTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = Database(str(self.root / "test.db"))
        await self.database.init_db()
        self.proxy_id = await self.database.add_proxy_pool_entry("socks5://relay.invalid:1080")
        self.backend = Backend()
        self.manager = ProfileManager(self.root / "profiles", self.backend)
        self.tokens = TokenManager(
            self.database, SimpleNamespace(account_request_proxy=lambda proxy: nullcontext())
        )

        async def verify_environment(context, directory, **kwargs):
            (Path(directory) / "environment_snapshot.json").write_text(
                ENVIRONMENT.to_json(), encoding="utf-8"
            )
            return "environment_stable"

        async def verify_account(email, probe):
            probe.validated_session = {
                "st": "synthetic-renewed-st",
                "at": "synthetic-renewed-at",
                "at_expires": datetime.now(timezone.utc) + timedelta(hours=3),
                "credits": 1050,
            }
            return AccountVerification(True, True, 1050, "account_ready")

        async def renew(token, context, force=False):
            self.assertTrue(force)
            self.assertFalse(
                any(
                    row["name"].startswith("__Secure-next-auth.session-token")
                    for row in await context.cookies()
                )
            )
            self.backend.states[str(context.profile_dir)]["cookies"].append(
                cookie("__Secure-next-auth.session-token", "labs.google")
            )
            return {"_validated_session": {"st": "synthetic-renewed-st"}}

        self.refresher = SimpleNamespace(
            manager=self.manager,
            active_context_provider=None,
            verifier=SimpleNamespace(
                environment_snapshot=AsyncMock(return_value=ENVIRONMENT),
                verify_environment=AsyncMock(side_effect=verify_environment),
                verify_account=AsyncMock(side_effect=verify_account),
            ),
            _proxy_url=AsyncMock(
                side_effect=lambda token: token.proxy_url or "socks5://relay.invalid:1080"
            ),
            refresh_in_context=AsyncMock(side_effect=renew),
        )
        self.tokens.profile_refresher = self.refresher
        self.tokens.ensure_project_exists = AsyncMock(return_value="synthetic-project")
        self.tokens.notify_profile_ready = Mock()
        self.service = ProfileTransfer(self.tokens)
        self.runtime = patch("src.services.browser.profile_transfer._runtime", return_value=RUNTIME)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    async def test_native_restore_once_forced_google_renewal_and_two_cold_reopens_before_enabled_save(
        self,
    ):
        stages = []
        result = await self.service.import_bundle(bundle(), on_stage=stages.append)
        self.assertEqual(
            stages,
            [
                "open_profile",
                "restore_state",
                "oauth_renewal",
                "cold_check_1",
                "cold_check_2",
                "save_account",
                "prepare_workspace",
                "finalize",
            ],
        )
        token = await self.database.get_token(result["token_id"])
        self.assertTrue(result["is_active"])
        self.assertTrue(token.is_active)
        self.assertEqual(token.st, "synthetic-renewed-st")
        self.assertEqual(token.at, "synthetic-renewed-at")
        self.assertEqual(token.proxy_pool_id, self.proxy_id)
        self.assertEqual(token.credits, 1050)
        self.assertTrue(token.auto_refresh_enabled)
        self.assertEqual(token.browser_profile_status, "ready")
        self.assertEqual(token.login_password, "")
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(self.backend.opens, 3)
        state = self.backend.restores[0]
        self.assertEqual(state["origins"], bundle()["storage_state"]["origins"])
        self.assertEqual(state["cookies"][1]["partitionKey"], "https://flow.google.com")
        self.assertEqual((await self.manager.spec(EMAIL)).fingerprint_seed, 46281)
        self.tokens.ensure_project_exists.assert_awaited_once_with(token.id)
        self.assertIsNone(token.ban_reason)

    async def test_disabled_source_stays_disabled_and_duplicate_never_opens_or_overwrites(self):
        first = await self.service.import_bundle(bundle(False))
        token = await self.database.get_token(first["token_id"])
        self.assertFalse(token.is_active)
        self.assertEqual(token.ban_reason, "manual")
        self.tokens.ensure_project_exists.assert_not_awaited()
        with self.assertRaises(ProfileTransferError) as failure:
            await self.service.import_bundle(bundle())
        self.assertEqual(failure.exception.code, "account_exists")
        self.assertEqual(self.backend.opens, 3)

    async def test_bad_identity_quarantines_target_without_account_or_existing_mutation(self):
        self.refresher.refresh_in_context.side_effect = ProfileNotReadyError(
            EMAIL, "identity_mismatch"
        )
        with self.assertRaises(ProfileNotReadyError):
            await self.service.import_bundle(bundle())
        self.assertEqual(await self.database.get_all_tokens(), [])
        self.assertEqual((await self.manager.status(EMAIL)).state, "blocked")
        metadata_path = self.manager.root_dir / self.manager._normalize_account_key(EMAIL)
        diagnostic = json.loads((metadata_path / "profile_transfer.json").read_text("utf-8"))
        self.assertEqual(diagnostic["transfer_failure_reason"], "identity_mismatch")
        self.assertEqual(diagnostic["transfer_failure_stage"], "oauth_renewal")
        self.assertIsInstance(diagnostic["elapsed_seconds"], (int, float))
        self.assertNotIn("cookies", diagnostic)
        self.assertNotIn("proxy_url", diagnostic)
        metadata = json.loads((metadata_path / "metadata.json").read_text("utf-8"))
        self.assertEqual(metadata["last_error"], "transfer_failed")
        self.assertNotIn("transfer_failure_reason", metadata)

    async def test_retry_failed_new_profile_reuses_state_and_still_renews_and_checks_twice(self):
        renew = self.refresher.refresh_in_context.side_effect
        self.refresher.refresh_in_context.side_effect = ProfileNotReadyError(
            EMAIL, "transient_error"
        )
        with self.assertRaises(ProfileNotReadyError):
            await self.service.import_bundle(bundle(False))
        directory = self.manager.root_dir / self.manager._normalize_account_key(EMAIL)
        retained = directory / "keep-existing-data.txt"
        retained.write_text("retained", encoding="utf-8")
        self.refresher.refresh_in_context.side_effect = renew
        result = await self.service.import_bundle(bundle(False))
        self.assertTrue(result["success"])
        self.assertEqual(retained.read_text("utf-8"), "retained")
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(self.backend.opens, 4)
        self.assertEqual(self.refresher.refresh_in_context.await_count, 2)
        self.assertEqual(self.refresher.verifier.verify_account.await_count, 2)
        self.assertEqual((await self.manager.status(EMAIL)).state, "ready")
        diagnostic = json.loads((directory / "profile_transfer.json").read_text("utf-8"))
        self.assertTrue(diagnostic["completed"])
        self.assertNotIn("transfer_failure_reason", diagnostic)

    async def test_retry_requires_same_seed_browser_and_fixed_proxy(self):
        self.refresher.refresh_in_context.side_effect = ProfileNotReadyError(
            EMAIL, "transient_error"
        )
        with self.assertRaises(ProfileNotReadyError):
            await self.service.import_bundle(bundle())
        opens = self.backend.opens
        values = []
        for field, change in (
            ("fingerprint_seed", 46282),
            ("runtime", {**RUNTIME, "chromium": "147.synthetic"}),
            ("environment", json.loads(replace(ENVIRONMENT, timezone="Asia/Shanghai").to_json())),
        ):
            value = bundle()
            value["browser"][field] = change
            values.append((value, {}))
        other_proxy = await self.database.add_proxy_pool_entry("socks5://other.invalid:1080")
        values.append((bundle(), {"proxy_pool_id": other_proxy}))
        for value, kwargs in values:
            with self.subTest(browser=value["browser"], kwargs=kwargs):
                with self.assertRaises(ProfileTransferError) as failure:
                    await self.service.import_bundle(value, **kwargs)
                self.assertEqual(failure.exception.code, "profile_exists")
        self.assertEqual(self.backend.opens, opens)
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(await self.database.get_all_tokens(), [])

    async def legacy_failed_profile(self):
        renew = self.refresher.refresh_in_context.side_effect
        self.refresher.refresh_in_context.side_effect = ProfileNotReadyError(
            EMAIL, "transient_error"
        )
        with self.assertRaises(ProfileNotReadyError):
            await self.service.import_bundle(bundle())
        directory = self.manager.root_dir / self.manager._normalize_account_key(EMAIL)
        (directory / "profile_transfer.json").unlink()
        self.refresher.refresh_in_context.side_effect = renew
        return directory

    async def test_legacy_failed_profile_reuses_persistent_state_and_original_proxy(self):
        directory = await self.legacy_failed_profile()
        retained = directory / "original-data.txt"
        retained.write_text("preserved", encoding="utf-8")
        result = await self.service.import_bundle(bundle(False))
        self.assertTrue(result["success"])
        self.assertEqual(retained.read_text("utf-8"), "preserved")
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(self.backend.opens, 4)
        self.assertEqual(self.refresher.refresh_in_context.await_count, 2)
        self.assertEqual(self.refresher.verifier.verify_account.await_count, 2)
        self.assertEqual((await self.manager.status(EMAIL)).state, "ready")

    async def test_legacy_failed_profile_rejects_changed_proxy_without_writing_sidecar(self):
        directory = await self.legacy_failed_profile()
        original = (directory / "launch_settings.sha256").read_text("utf-8")
        proxy_id = await self.database.add_proxy_pool_entry("socks5://other.invalid:1080")
        with self.assertRaises(ProfileTransferError) as failure:
            await self.service.import_bundle(bundle(), proxy_pool_id=proxy_id)
        self.assertEqual(failure.exception.code, "profile_exists")
        self.assertEqual(self.backend.opens, 1)
        self.assertFalse((directory / "profile_transfer.json").exists())
        self.assertEqual((directory / "launch_settings.sha256").read_text("utf-8"), original)
        self.assertTrue((directory / "Default").is_dir())

    async def test_legacy_retry_still_rejects_ordinary_existing_profile(self):
        directory = await self.legacy_failed_profile()
        await self.manager.mark_verified(EMAIL)
        with self.assertRaises(ProfileTransferError) as failure:
            await self.service.import_bundle(bundle())
        self.assertEqual(failure.exception.code, "profile_exists")
        self.assertEqual(self.backend.opens, 1)
        self.assertFalse((directory / "profile_transfer.json").exists())
        self.assertEqual((await self.manager.status(EMAIL)).state, "ready")

    async def test_legacy_retry_requires_existing_identity_and_launch_evidence(self):
        directory = await self.legacy_failed_profile()
        environment_path = directory / "environment_snapshot.json"
        launch_path = directory / "launch_settings.sha256"
        metadata_path = directory / "metadata.json"
        original_environment = environment_path.read_text("utf-8")
        original_launch = launch_path.read_text("utf-8")
        original_metadata = metadata_path.read_text("utf-8")
        for broken in (
            "missing_environment",
            "missing_launch",
            "changed_environment",
            "changed_seed",
        ):
            with self.subTest(broken=broken):
                environment_path.write_text(original_environment, encoding="utf-8")
                launch_path.write_text(original_launch, encoding="utf-8")
                metadata_path.write_text(original_metadata, encoding="utf-8")
                if broken == "missing_environment":
                    environment_path.unlink()
                elif broken == "missing_launch":
                    launch_path.unlink()
                elif broken == "changed_environment":
                    environment_path.write_text(
                        replace(ENVIRONMENT, timezone="Asia/Shanghai").to_json(), encoding="utf-8"
                    )
                else:
                    metadata = json.loads(original_metadata)
                    metadata["fingerprint_seed"] = 46282
                    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaises(ProfileTransferError) as failure:
                    await self.service.import_bundle(bundle())
                self.assertEqual(failure.exception.code, "profile_exists")
                self.assertFalse((directory / "profile_transfer.json").exists())
        self.assertEqual(self.backend.opens, 1)
        self.assertEqual(len(self.backend.restores), 1)

    async def test_retry_never_skips_failed_cold_identity_check(self):
        self.refresher.verifier.verify_account.side_effect = None
        self.refresher.verifier.verify_account.return_value = AccountVerification(
            False, True, 0, "identity_mismatch"
        )
        with self.assertRaises(ProfileNotReadyError) as failure:
            await self.service.import_bundle(bundle())
        self.assertEqual(failure.exception.transfer_stage, "cold_check_1")
        self.refresher.refresh_in_context.side_effect = None
        self.refresher.refresh_in_context.return_value = {"_validated_session": {"st": "fresh"}}
        with self.assertRaises(ProfileNotReadyError) as retry:
            await self.service.import_bundle(bundle())
        self.assertEqual(retry.exception.reason, "identity_mismatch")
        self.assertEqual(retry.exception.transfer_stage, "cold_check_1")
        self.assertEqual(self.refresher.verifier.verify_account.await_count, 2)
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(await self.database.get_all_tokens(), [])
        self.tokens.ensure_project_exists.assert_not_awaited()

    async def test_failed_before_restore_retries_in_original_profile_and_restores_once(self):
        self.refresher.verifier.environment_snapshot.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.service.import_bundle(bundle())
        directory = self.manager.root_dir / self.manager._normalize_account_key(EMAIL)
        diagnostic = json.loads((directory / "profile_transfer.json").read_text("utf-8"))
        self.assertFalse(diagnostic["state_imported"])
        self.assertEqual(diagnostic["transfer_failure_reason"], "transient_error")
        self.assertEqual(diagnostic["transfer_failure_stage"], "open_profile")
        retained = directory / "retained.txt"
        retained.write_text("original-profile", encoding="utf-8")
        self.refresher.verifier.environment_snapshot.side_effect = None
        result = await self.service.import_bundle(bundle(False))
        self.assertTrue(result["success"])
        self.assertEqual(retained.read_text("utf-8"), "original-profile")
        self.assertEqual(self.backend.opens, 4)
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(self.refresher.refresh_in_context.await_count, 1)
        self.assertEqual(self.refresher.verifier.verify_account.await_count, 2)

    async def test_project_failure_preserves_account_disabled_with_profile(self):
        self.tokens.ensure_project_exists.side_effect = RuntimeError("synthetic")
        with self.assertRaises(RuntimeError) as failure:
            await self.service.import_bundle(bundle())
        token = (await self.database.get_all_tokens())[0]
        self.assertFalse(token.is_active)
        self.assertFalse(token.auto_refresh_enabled)
        self.assertEqual(token.browser_profile_status, "blocked")
        self.assertEqual(token.st, "synthetic-renewed-st")
        self.assertEqual(failure.exception.transfer_token_id, token.id)
        status, detail = transfer_error_detail(failure.exception)
        self.assertEqual(status, 502)
        self.assertEqual(detail["token_id"], token.id)
        self.assertTrue(detail["requires_account_action"])
        self.assertIn("账号已保存", detail["message"])
        self.assertEqual(detail["stage"], "prepare_workspace")
        self.assertNotIn("synthetic", json.dumps(detail))

    async def test_invalid_scope_runtime_and_native_api_reject_without_persistence(self):
        for mutate, expected in (
            (
                lambda value: value["storage_state"]["cookies"].append(
                    cookie(domain="foreign.invalid")
                ),
                "invalid_scope",
            ),
            (
                lambda value: value["browser"]["runtime"].update(playwright="different"),
                "runtime_mismatch",
            ),
            (
                lambda value: value["browser"].update(launch_options={"args": ["--arbitrary"]}),
                "launch_mismatch",
            ),
            (lambda value: value.update(version=True), "unsupported_format"),
            (lambda value: value["storage_state"].update(origins={}), "invalid_state"),
        ):
            value = bundle()
            mutate(value)
            with (
                self.subTest(expected=expected),
                self.assertRaises(ProfileTransferError) as failure,
            ):
                await self.service.import_bundle(value)
            self.assertEqual(failure.exception.code, expected)
        self.assertEqual(self.backend.opens, 0)
        self.assertEqual(await self.database.get_all_tokens(), [])

    async def test_export_borrows_viewer_keeps_seed_and_storage_without_at_password_or_proxy(self):
        receipt = await self.service.import_bundle(bundle(False))
        token = await self.database.get_token(receipt["token_id"])
        spec = await self.manager.spec(EMAIL)
        context = await self.backend.open(spec.profile_dir)
        before = self.backend.opens

        @asynccontextmanager
        async def viewer(account):
            self.assertEqual(account.id, token.id)
            yield context

        self.refresher.active_context_provider = viewer
        result = await self.service.export(token.id)
        self.assertEqual(self.backend.opens, before)
        self.assertEqual(result["browser"]["fingerprint_seed"], 46281)
        self.assertEqual(result["storage_state"]["origins"], bundle()["storage_state"]["origins"])
        self.assertNotIn("access_token", json.dumps(result))
        self.assertNotIn("synthetic-renewed-at", json.dumps(result))
        self.assertNotIn("proxy_url", json.dumps(result))
        self.assertNotIn("login_password", json.dumps(result))
        context.storage_state.assert_awaited_once_with(indexed_db=True)

    async def test_seed_collision_retains_existing_owner_and_does_not_open(self):
        self.manager._claim_seed("existing-owner", 46281)
        with self.assertRaises(ProfileTransferError) as failure:
            await self.service.import_bundle(bundle())
        self.assertEqual(failure.exception.code, "seed_conflict")
        self.assertFalse(
            (self.manager.root_dir / self.manager._normalize_account_key(EMAIL)).exists()
        )
        self.assertEqual(self.backend.opens, 0)

    async def test_window_bounds_are_presentation_but_timezone_remains_identity(self):
        self.refresher.verifier.environment_snapshot.return_value = replace(
            ENVIRONMENT, viewport_width=937, viewport_height=935
        )
        receipt = await self.service.import_bundle(bundle(False))
        self.assertTrue(receipt["success"])
        self.assertTrue(
            all(
                call.kwargs.get("allow_viewport_change") is True
                for call in self.refresher.verifier.verify_environment.await_args_list
            )
        )
        other = bundle(False)
        other["account"]["email"] = "other@example.invalid"
        other["browser"]["fingerprint_seed"] = 46282
        self.refresher.verifier.environment_snapshot.return_value = replace(
            ENVIRONMENT, timezone="Asia/Shanghai"
        )
        with self.assertRaises(ProfileTransferError) as failure:
            await self.service.import_bundle(other)
        self.assertEqual(failure.exception.code, "environment_mismatch")
        self.assertEqual(len(await self.database.get_all_tokens()), 1)

    def test_native_scope_keeps_partition_key_but_rejects_foreign_partition(self):
        value = bundle()["storage_state"]
        self.assertEqual(_scoped_state(value), value)
        value["cookies"][-1]["partitionKey"] = "https://foreign.invalid"
        with self.assertRaises(ProfileTransferError):
            _scoped_state(value)


class ProfileTransferRouteTests(unittest.TestCase):
    def setUp(self):
        self.service = SimpleNamespace(
            export=AsyncMock(return_value=bundle()),
            import_bundle=AsyncMock(return_value={"success": True, "token_id": 1}),
        )
        self.manager = SimpleNamespace(_profile_transfer=self.service)

        async def verify(request: Request):
            if request.headers.get("Authorization") != "Bearer synthetic-admin":
                raise HTTPException(401)
            return "synthetic-admin"

        app = FastAPI()
        app.include_router(create_profile_transfer_router(lambda: self.manager, verify))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_authenticated_export_import_contract_and_private_failure(self):
        self.assertEqual(self.client.get("/api/tokens/1/profile-export").status_code, 401)
        self.service.export.assert_not_awaited()
        headers = {"Authorization": "Bearer synthetic-admin"}
        response = self.client.get("/api/tokens/1/profile-export", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["bundle"]["format"], FORMAT)
        response = self.client.post(
            "/api/profile-transfer/import", headers=headers, json={"bundle": bundle()}
        )
        self.assertTrue(response.json()["success"])
        self.service.import_bundle.side_effect = RuntimeError("synthetic-private-secret")
        response = self.client.post(
            "/api/profile-transfer/import", headers=headers, json={"bundle": bundle()}
        )
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("synthetic-private-secret", response.text)

    def test_verification_failure_explains_network_stage_without_raw_exception(self):
        error = ProfileNotReadyError(EMAIL, "transient_error")
        error.transfer_stage = "oauth_renewal"
        error.transfer_elapsed_seconds = 31.2
        error.profile_network_step = "credits_read"
        self.service.import_bundle.side_effect = error
        response = self.client.post(
            "/api/profile-transfer/import",
            headers={"Authorization": "Bearer synthetic-admin"},
            json={"bundle": bundle()},
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["reason"], "transient_error")
        self.assertEqual(detail["stage"], "oauth_renewal")
        self.assertEqual(detail["elapsed_seconds"], 31.2)
        self.assertEqual(detail["network_step"], "credits_read")
        self.assertIn("Flow 账号额度接口", detail["message"])
        self.assertIn("网络请求超时", detail["message"])
        self.assertIn("Google 登录态续签", detail["message"])
        self.assertNotIn(EMAIL, response.text)

    def test_unknown_verification_reason_is_not_echoed(self):
        error = ProfileNotReadyError(EMAIL, "synthetic-private-secret")
        error.profile_network_step = "synthetic-private-secret"
        self.service.import_bundle.side_effect = error
        response = self.client.post(
            "/api/profile-transfer/import",
            headers={"Authorization": "Bearer synthetic-admin"},
            json={"bundle": bundle()},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["reason"], "verification_failed")
        self.assertNotIn("synthetic-private-secret", response.text)
