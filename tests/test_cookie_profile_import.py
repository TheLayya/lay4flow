"""Synthetic external Cookie admission uses the real Profile transfer pipeline."""

import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import test_profile_transfer as transfer_fixture

from src.services.browser.cookie_import import CookieProfileImport, is_cookie_import
from src.services.browser.profile_import_jobs import ProfileImportJobs
from src.services.browser.profile_transfer import ProfileTransferError
from src.services.browser.profiles import ProfileNotReadyError

EMAIL = transfer_fixture.EMAIL
RUNTIME = transfer_fixture.RUNTIME


def external_json():
    return {
        "cookies": {"SID": "synthetic-sid", "OSID": "synthetic-osid"},
        "email": EMAIL,
        "projectId": "untrusted-project",
        "sid": "untrusted-session",
        "userPaygateTier": "untrusted-tier",
        "proxy_url": "http://untrusted.invalid:80",
        "fingerprint_seed": 1,
    }


class CookieProfileImportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transfer_fixture.ProfileTransferTests.asyncSetUp

    async def test_external_json_creates_account_after_fixed_proxy_verification(self):
        payload = external_json()
        stages = []
        with patch("src.services.browser.cookie_import._runtime", return_value=RUNTIME):
            result = await CookieProfileImport(self.service).import_account(
                payload, proxy_pool_id=self.proxy_id, on_stage=stages.append
            )
        self.assertEqual(self.backend.opens, 1)
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(stages[-2:], ["save_account", "finalize"])
        self.assertNotIn("cold_check_1", stages)
        self.assertNotIn("cold_check_2", stages)
        token = await self.database.get_token(result["token_id"])
        self.assertTrue(token.is_active)
        self.assertTrue(token.auto_refresh_enabled)
        self.assertEqual(token.browser_profile_source, "cloak")
        self.assertEqual(token.proxy_pool_id, self.proxy_id)
        self.assertEqual(token.credits, 1050)
        self.assertEqual(token.user_paygate_tier, "PAYGATE_TIER_ONE")
        self.assertNotEqual(token.current_project_id, payload["projectId"])
        self.assertNotEqual(token.st, payload["sid"])
        self.assertEqual(payload, external_json())
        for row in self.backend.restores[0]["cookies"]:
            self.assertEqual(row["expires"], -1)

    async def test_identity_mismatch_never_saves_or_enables_claimed_account(self):
        self.refresher.refresh_in_context.side_effect = ProfileNotReadyError(
            EMAIL, "identity_mismatch"
        )
        with (
            patch("src.services.browser.cookie_import._runtime", return_value=RUNTIME),
            self.assertRaises(ProfileNotReadyError) as failure,
        ):
            await CookieProfileImport(self.service).import_account(external_json())
        self.assertEqual(failure.exception.reason, "identity_mismatch")
        self.assertEqual(await self.database.get_all_tokens(), [])

    async def test_failed_import_retry_reuses_seed_proxy_and_state(self):
        self.tokens._profile_transfer = self.service
        cold_probe = self.refresher.refresh_in_context.side_effect
        self.refresher.refresh_in_context.side_effect = ProfileNotReadyError(
            EMAIL, "login_required"
        )
        importer = CookieProfileImport(self.service)
        with patch("src.services.browser.cookie_import._runtime", return_value=RUNTIME):
            with self.assertRaises(ProfileNotReadyError):
                await importer.import_account(external_json(), proxy_pool_id=self.proxy_id)
            spec = await self.manager.spec(EMAIL)
            with self.assertRaises(ProfileTransferError):
                await importer.import_account(external_json(), proxy_pool_id=self.proxy_id + 1)
            self.refresher.refresh_in_context.side_effect = cold_probe
            result = await importer.import_account(external_json())
        self.assertTrue(result["success"])
        self.assertEqual((await self.manager.spec(EMAIL)).fingerprint_seed, spec.fingerprint_seed)
        self.assertEqual(len(self.backend.restores), 1)
        self.assertEqual(
            (await self.database.get_token(result["token_id"])).proxy_pool_id, self.proxy_id
        )

    async def test_bad_input_rejects_before_browser_or_account_write(self):
        values = [
            {"cookies": {}, "email": EMAIL},
            {"cookies": {"SID": 12}, "email": EMAIL},
            {"cookies": {"SID": "synthetic"}},
            {"cookies": {"NID": "synthetic"}, "email": EMAIL},
            {"cookies": {"SID": "synthetic"}, "email": "invalid"},
            {"cookies": ["synthetic"], "email": EMAIL},
            {"cookies": {"SID": "s" * (512 * 1024)}, "email": EMAIL},
        ]
        for payload in values:
            with self.subTest(fields=list(payload)), self.assertRaises(ProfileTransferError):
                await CookieProfileImport(self.service).import_account(payload)
        self.assertEqual(self.backend.opens, 0)
        self.assertEqual(await self.database.get_all_tokens(), [])

    async def test_background_queue_routes_cookie_json_without_legacy_session_token(self):
        importer = AsyncMock(return_value={"success": True, "token_id": 77})
        legacy = AsyncMock()
        jobs = ProfileImportJobs(self.service, legacy)
        self.addAsyncCleanup(jobs.close)
        payload = external_json()
        with patch.object(CookieProfileImport, "import_account", importer):
            job = jobs.submit("owner", [payload], "cookie-request", proxy_pool_id=self.proxy_id)
            await jobs._worker
        result = jobs.get("owner", job["id"])
        self.assertEqual((result["added"], result["failed"]), (1, 0))
        self.assertEqual(importer.await_args.args[0], payload)
        self.assertEqual(importer.await_args.kwargs["proxy_pool_id"], self.proxy_id)
        legacy.assert_not_awaited()
        self.assertNotIn("synthetic-sid", json.dumps(result))
        self.assertIsNone(jobs._jobs[job["id"]]["_inputs"][0])

    def test_detection_preserves_other_formats(self):
        self.assertTrue(is_cookie_import(external_json()))
        for value in ([], None, {"session_token": "s"}, {"format": "unknown", "cookies": {}}):
            self.assertFalse(is_cookie_import(copy.deepcopy(value)))
