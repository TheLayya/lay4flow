"""Existing-account login updates preserve identity, settings and rollback safety."""

import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import test_profile_transfer as fixtures

from src.services.browser.cookie_import import CookieProfileImport
from src.services.browser.profile_import_jobs import ProfileImportJobs
from src.services.browser.profile_transfer import ProfileTransferError
from src.services.browser.profile_verifier import AccountVerification
from src.services.browser.profiles import ProfileNotReadyError


class ProfileReimportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ProfileTransferTests.asyncSetUp

    async def create_existing(self):
        first = await self.service.import_bundle(fixtures.bundle(False))
        self.token_id = first["token_id"]
        await self.database.update_token(
            self.token_id,
            remark="keep-remark",
            image_concurrency=7,
            auto_refresh_enabled=False,
            current_project_id="keep-project",
        )
        self.tokens.account_proxy_maintenance = self.maintenance
        self.token = await self.database.get_token(self.token_id)
        self.spec = await self.manager.spec(self.token.email)
        (self.spec.profile_dir / "Default" / "site-data.txt").write_text("keep-storage")
        original = self.backend.open

        async def open_profile(directory, *, proxy_url=None):
            context = await original(directory, proxy_url=proxy_url)

            async def clear():
                self.backend.states[str(directory)]["cookies"] = []

            context.clear_cookies = AsyncMock(side_effect=clear)
            return context

        self.backend.open_background = open_profile
        self.backend.cookie_repairs.clear()
        self.opens_before = self.backend.opens

    @asynccontextmanager
    async def maintenance(self, token, proxy):
        self.assertEqual(proxy, self.token.proxy_url)
        yield

    async def test_native_reimport_updates_existing_row_once_preserving_all_controls(self):
        await self.create_existing()
        source = fixtures.bundle()
        source["browser"]["fingerprint_seed"] += 1
        source["account"]["remark"] = "do-not-copy"
        source["storage_state"]["cookies"][0]["value"] = "new-login"
        result = await self.service.import_bundle(source, proxy_pool_id=999)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["token_id"], self.token.id)
        saved = await self.database.get_token(self.token_id)
        for field in (
            "id",
            "email",
            "st",
            "proxy_url",
            "proxy_pool_id",
            "remark",
            "is_active",
            "auto_refresh_enabled",
            "image_concurrency",
            "current_project_id",
        ):
            self.assertEqual(getattr(saved, field), getattr(self.token, field), field)
        self.assertIn("new-login", saved.google_cookies)
        self.assertEqual(
            (await self.manager.spec(saved.email)).fingerprint_seed, self.spec.fingerprint_seed
        )
        self.assertEqual(self.backend.opens - self.opens_before, 1)
        self.refresher.verifier.verify_account.assert_awaited_once()
        self.assertEqual(
            (self.spec.profile_dir / "Default" / "site-data.txt").read_text(), "keep-storage"
        )
        self.assertEqual(len(await self.database.get_all_tokens()), 1)

    async def test_cookie_json_reimport_uses_canonical_account_and_reports_updated(self):
        await self.create_existing()
        value = {"email": fixtures.EMAIL.upper(), "cookies": {"SID": "fresh-cookie"}}
        result = await CookieProfileImport(self.service).import_account(value, proxy_pool_id=999)
        self.assertEqual((result["added"], result["updated"]), (0, 1))
        self.assertEqual(result["email"], self.token.email)
        self.assertEqual(
            (await self.database.get_token(self.token_id)).proxy_pool_id, self.token.proxy_pool_id
        )

    async def test_wrong_identity_preserves_original_profile_and_database(self):
        await self.create_existing()
        before = self.snapshot()
        self.refresher.verifier.verify_account.side_effect = None
        self.refresher.verifier.verify_account.return_value = AccountVerification(
            False, True, 0, "identity_mismatch"
        )
        with self.assertRaises(ProfileNotReadyError):
            await CookieProfileImport(self.service).import_account(
                {"email": fixtures.EMAIL, "cookies": {"SID": "wrong-login"}}
            )
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            (await self.database.get_token(self.token_id)).model_dump(), self.token.model_dump()
        )

    async def test_concurrent_account_edit_rolls_back_profile_and_preserves_edit(self):
        await self.create_existing()
        before = self.snapshot()
        commit = self.database.update_token_if_credentials_match

        async def changed(token_id, expected, **kwargs):
            await self.database.update_token(token_id, remark="concurrent-edit")
            return await commit(token_id, expected, **kwargs)

        self.database.update_token_if_credentials_match = AsyncMock(side_effect=changed)
        with self.assertRaises(ProfileTransferError) as error:
            await CookieProfileImport(self.service).import_account(
                {"email": fixtures.EMAIL, "cookies": {"SID": "new-login"}}
            )
        self.assertEqual(error.exception.code, "account_changed")
        self.assertEqual(self.snapshot(), before)
        saved = await self.database.get_token(self.token_id)
        self.assertEqual(saved.remark, "concurrent-edit")
        self.assertEqual(saved.google_cookies, self.token.google_cookies)

    async def test_queue_counts_reimport_as_updated(self):
        await self.create_existing()
        jobs = ProfileImportJobs(self.service)
        self.addAsyncCleanup(jobs.close)
        job = jobs.submit(
            "owner", [{"email": fixtures.EMAIL, "cookies": {"SID": "new-login"}}], "update"
        )
        await jobs._worker
        result = jobs.get("owner", job["id"])
        self.assertEqual((result["added"], result["updated"], result["failed"]), (0, 1, 0))

    def snapshot(self):
        return {
            str(path.relative_to(self.spec.profile_dir)): path.read_bytes()
            for path in self.spec.profile_dir.rglob("*")
            if path.is_file() and path.name != ".profile.lock"
        }
