"""Synthetic imports verify detached execution, queue limits and private status."""

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.api.admin_routes.profile_transfer import create_profile_transfer_router
from src.services.browser.profile_import_jobs import ProfileImportJobs
from src.services.browser.profile_transfer import FORMAT, ProfileTransferError
from src.services.browser.profiles import ProfileNotReadyError


def bundle(email="first@example.invalid"):
    return {
        "format": FORMAT,
        "account": {"email": email},
        "storage_state": {"cookies": [{"value": "synthetic-private-cookie"}]},
    }


async def finished(service):
    if service._worker is not None:
        await service._worker


class ProfileImportJobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transfer = SimpleNamespace(import_bundle=AsyncMock(return_value={"success": True}))
        self.jobs = ProfileImportJobs(self.transfer)
        self.addAsyncCleanup(self.jobs.close)

    async def test_submit_detaches_real_stages_keeps_single_worker_and_drops_success_input(self):
        opened, release = asyncio.Event(), asyncio.Event()
        order = []

        async def import_bundle(value, *, on_stage, **kwargs):
            order.append(value["account"]["email"])
            on_stage("flow_verification")
            opened.set()
            await release.wait()
            return {"success": True, "token_id": len(order)}

        self.transfer.import_bundle.side_effect = import_bundle
        first = self.jobs.submit("owner", [bundle()], "request-1", proxy_url="synthetic-secret")
        second = self.jobs.submit("owner", [bundle("second@example.invalid")], "request-2")
        self.assertEqual(first["state"], "queued")
        await opened.wait()
        self.assertEqual(order, ["first@example.invalid"])
        current = self.jobs.get("owner", first["id"])
        self.assertEqual(current["items"][0]["stage"], "flow_verification")
        self.assertEqual(current["current"], 1)
        self.assertEqual(self.jobs.get("owner", second["id"])["state"], "queued")
        self.assertNotIn("synthetic-private-cookie", json.dumps(current))
        self.assertNotIn("synthetic-secret", json.dumps(current))
        release.set()
        await finished(self.jobs)
        self.assertEqual(order, ["first@example.invalid", "second@example.invalid"])
        self.assertEqual(self.jobs.get("owner", first["id"])["added"], 1)
        self.assertEqual(self.jobs.get("owner", second["id"])["state"], "completed")
        self.assertEqual(self.jobs._input_bytes, 0)
        self.assertTrue(all(value is None for value in self.jobs._jobs[first["id"]]["_inputs"]))

    async def test_idempotency_survives_response_loss_rejects_changed_payload_and_owner_isolation(
        self,
    ):
        original = self.jobs.submit("owner", [bundle()], "request-1")
        duplicate = self.jobs.submit("owner", [bundle()], "request-1")
        self.assertEqual(original["id"], duplicate["id"])
        with self.assertRaises(ProfileTransferError) as conflict:
            self.jobs.submit("owner", [bundle("changed@example.invalid")], "request-1")
        self.assertEqual(conflict.exception.code, "import_request_conflict")
        self.assertEqual(self.jobs.list("other-owner"), [])
        with self.assertRaises(ProfileTransferError) as private:
            self.jobs.get("other-owner", original["id"])
        self.assertEqual(private.exception.status, 404)
        other = self.jobs.submit("other-owner", [bundle()], "request-1")
        self.assertNotEqual(other["id"], original["id"])
        await finished(self.jobs)
        self.assertEqual(self.transfer.import_bundle.await_count, 2)
        duplicate = self.jobs.submit("owner", [bundle()], "request-1")
        self.assertEqual(duplicate["state"], "completed")
        self.assertEqual(duplicate["request_id"], "request-1")

    async def test_retry_only_failed_items_retains_success_and_structured_private_errors(self):
        self.transfer.import_bundle.side_effect = [
            {"success": True},
            ProfileNotReadyError("failed@example.invalid", "transient_error"),
            {"success": True},
        ]
        job = self.jobs.submit("owner", [bundle(), bundle("failed@example.invalid")], "mixed")
        await finished(self.jobs)
        failed = self.jobs.get("owner", job["id"])
        self.assertEqual((failed["added"], failed["failed"]), (1, 1))
        self.assertTrue(failed["can_retry"])
        self.assertEqual(failed["items"][1]["reason"], "transient_error")
        self.assertEqual(failed["items"][1]["error_code"], "profile_verification_failed")
        self.assertIsNone(self.jobs._jobs[job["id"]]["_inputs"][0])
        self.assertIsNotNone(self.jobs._jobs[job["id"]]["_inputs"][1])
        self.jobs.retry("owner", job["id"])
        self.jobs.retry("owner", job["id"])
        await finished(self.jobs)
        completed = self.jobs.get("owner", job["id"])
        self.assertEqual((completed["added"], completed["failed"]), (2, 0))
        self.assertFalse(completed["can_retry"])
        self.assertEqual(self.transfer.import_bundle.await_count, 3)
        self.assertEqual(self.jobs._input_bytes, 0)

    async def test_legacy_and_profile_share_worker_but_keep_added_updated_results(self):
        legacy = AsyncMock(
            return_value={"success": True, "added": 0, "updated": 1, "st": "private"}
        )
        jobs = ProfileImportJobs(self.transfer, legacy)
        self.addAsyncCleanup(jobs.close)
        original = {"email": "legacy@example.invalid", "session_token": "synthetic-secret"}
        job = jobs.submit("owner", [bundle(), original], "mixed")
        await finished(jobs)
        result = jobs.get("owner", job["id"])
        self.assertEqual((result["added"], result["updated"], result["failed"]), (1, 1, 0))
        legacy.assert_awaited_once_with(original, proxy_pool_id=None, proxy_url=None)
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertNotIn('"st"', json.dumps(result))

    async def test_saved_account_failure_releases_input_and_directs_account_action_without_retry(
        self,
    ):
        error = RuntimeError("synthetic-private-error")
        error.transfer_token_id = 17
        error.transfer_stage = "prepare_workspace"
        self.transfer.import_bundle.side_effect = error
        job = self.jobs.submit("owner", [bundle()], "saved-account", proxy_url="synthetic-secret")
        await finished(self.jobs)
        result = self.jobs.get("owner", job["id"])
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["can_retry"])
        self.assertTrue(result["items"][0]["requires_account_action"])
        self.assertEqual(result["items"][0]["token_id"], 17)
        self.assertIn("账号列表检查并启用", result["items"][0]["message"])
        self.assertNotIn("synthetic-private-error", json.dumps(result))
        self.assertEqual(self.jobs._input_bytes, 0)
        self.assertIsNone(self.jobs._jobs[job["id"]]["_inputs"][0])
        self.assertIsNone(self.jobs._jobs[job["id"]]["_proxy_url"])
        with self.assertRaises(ProfileTransferError) as retry:
            self.jobs.retry("owner", job["id"])
        self.assertEqual(retry.exception.code, "import_retry_empty")

    async def test_queue_bytes_and_terminal_history_are_bounded(self):
        self.jobs.max_pending = 1
        self.jobs.submit("owner", [bundle()], "first")
        with self.assertRaises(ProfileTransferError) as full:
            self.jobs.submit("owner", [bundle()], "second")
        self.assertEqual(full.exception.status, 429)
        await finished(self.jobs)
        self.jobs.max_job_bytes = 1
        with self.assertRaises(ProfileTransferError) as large:
            self.jobs.submit("owner", [bundle()], "large")
        self.assertEqual(large.exception.status, 413)
        self.jobs.max_job_bytes = 4096
        self.jobs.max_input_bytes = 1
        with self.assertRaises(ProfileTransferError):
            self.jobs.submit("owner", [bundle()], "large-total")
        self.jobs.max_input_bytes = 4096
        self.jobs.max_history = 1
        self.jobs.submit("owner", [bundle()], "last")
        await finished(self.jobs)
        self.assertEqual(len(self.jobs.list("owner")), 1)
        self.assertEqual(self.jobs.list("owner")[0]["request_id"], "last")
        self.jobs.history_seconds = 0
        self.assertEqual(self.jobs.list("owner"), [])
        self.assertEqual(len(self.jobs._requests), 0)

    async def test_shutdown_awaits_import_cleanup_cancels_queued_and_releases_input(self):
        opened, cleaned = asyncio.Event(), asyncio.Event()

        async def importing(*args, **kwargs):
            try:
                opened.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        self.transfer.import_bundle.side_effect = importing
        first = self.jobs.submit("owner", [bundle()], "first")
        queued = self.jobs.submit("owner", [bundle()], "queued")
        await opened.wait()
        await self.jobs.close()
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.jobs.get("owner", first["id"])["state"], "cancelled")
        self.assertEqual(self.jobs.get("owner", queued["id"])["state"], "cancelled")
        self.assertEqual(self.jobs._input_bytes, 0)
        with self.assertRaises(ProfileTransferError) as closed:
            self.jobs.submit("owner", [bundle()], "after-close")
        self.assertEqual(closed.exception.status, 503)

    async def test_failed_input_expires_without_any_status_request(self):
        self.jobs.history_seconds = 0.01
        self.transfer.import_bundle.side_effect = RuntimeError("synthetic-private-cookie")
        self.jobs.submit("owner", [bundle()], "expires")
        await finished(self.jobs)
        self.assertGreater(self.jobs._input_bytes, 0)
        await asyncio.sleep(0.03)
        self.assertEqual(self.jobs._input_bytes, 0)
        self.assertEqual(self.jobs._jobs, {})


class ProfileImportJobRoutesTests(unittest.TestCase):
    def setUp(self):
        self.transfer = SimpleNamespace(import_bundle=AsyncMock(return_value={"success": True}))
        self.manager = SimpleNamespace(_profile_transfer=self.transfer)

        async def verify(request: Request):
            owner = request.headers.get("Authorization")
            if owner not in {"Bearer owner", "Bearer other"}:
                raise HTTPException(401)
            return owner

        @asynccontextmanager
        async def lifespan(app):
            yield
            if hasattr(self.manager, "_profile_import_jobs"):
                await self.manager._profile_import_jobs.close()

        app = FastAPI(lifespan=lifespan)
        app.include_router(create_profile_transfer_router(lambda: self.manager, verify))
        self.client = self.enterContext(TestClient(app))
        self.headers = {"Authorization": "Bearer owner"}

    def test_auth_no_store_idempotent_submission_recovery_and_private_lookup(self):
        path = "/api/profile-transfer/jobs"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path).headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.post(path, json={}).status_code, 401)
        payload = {"bundles": [bundle()], "request_id": "synthetic-request"}
        submitted = self.client.post(path, headers=self.headers, json=payload)
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.headers["Cache-Control"], "no-store")
        job = submitted.json()["job"]
        repeated = self.client.post(path, headers=self.headers, json=payload)
        self.assertEqual(repeated.json()["job"]["id"], job["id"])
        listing = self.client.get(path, headers=self.headers)
        self.assertEqual(listing.headers["Cache-Control"], "no-store")
        self.assertEqual(listing.json()["jobs"][0]["request_id"], "synthetic-request")
        lookup = self.client.get(f"{path}/{job['id']}", headers=self.headers)
        self.assertTrue(lookup.json()["success"])
        self.assertNotIn("synthetic-private-cookie", lookup.text)
        private = self.client.get(f"{path}/{job['id']}", headers={"Authorization": "Bearer other"})
        self.assertEqual(private.status_code, 404)
        self.assertEqual(private.headers["Cache-Control"], "no-store")
        retry = self.client.post(f"{path}/{job['id']}/retry", headers=self.headers)
        self.assertEqual(retry.status_code, 409)

    def test_schema_error_does_not_echo_json_credentials_and_is_never_cached(self):
        response = self.client.post(
            "/api/profile-transfer/jobs",
            headers=self.headers,
            json={"bundles": "synthetic-private-cookie", "request_id": "id"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic-private-cookie", response.text)
