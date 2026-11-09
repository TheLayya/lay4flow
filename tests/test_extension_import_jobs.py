"""Durable synthetic extension submissions never disappear after acknowledgement."""

import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from src.services.browser.extension_import_jobs import ExtensionImportJobs
from src.services.browser.profile_transfer import ProfileTransferError

PAYLOAD = {
    "google_cookies": "synthetic-private-cookie",
    "browser_identity": {"user_agent": "Synthetic"},
}


class ExtensionImportJobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.importer = AsyncMock(
            return_value={"success": True, "email": "member@example.test", "token_id": 7}
        )
        self.jobs = ExtensionImportJobs(self.root, self.importer)
        self.addAsyncCleanup(self.jobs.close)

    def submit(self, session="session-1", owner="admin", **kwargs):
        return self.jobs.submit(
            owner, session, "nonce", "http://manage.test", ("42", "0"), kwargs or PAYLOAD
        )

    async def terminal(self, job_id):
        for _ in range(100):
            job = self.jobs.get("admin", job_id)
            if job["state"] in {"completed", "failed"}:
                return job
            await asyncio.sleep(0.01)
        self.fail("job did not finish")

    async def test_acknowledgement_commits_encrypted_payload_and_keeps_independent_accounts(self):
        release = asyncio.Event()

        async def slow(payload):
            await release.wait()
            return {"success": True, "email": "member@example.test", "token_id": 7}

        self.jobs.importer = AsyncMock(side_effect=slow)
        first = self.submit("one")
        second = self.submit("two")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.jobs.pending_count, 2)
        self.assertNotIn(b"synthetic-private-cookie", self.jobs.path.read_bytes())
        self.assertNotIn("synthetic-private-cookie", json.dumps(self.jobs.list("admin")))
        with closing(sqlite3.connect(self.jobs.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM extension_imports").fetchone()[0], 2)
        release.set()
        self.assertEqual((await self.terminal(first["id"]))["state"], "completed")
        self.assertEqual((await self.terminal(second["id"]))["state"], "completed")
        self.assertEqual(self.jobs.importer.await_count, 2)
        with closing(sqlite3.connect(self.jobs.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM extension_imports WHERE payload IS NOT NULL"
                ).fetchone()[0],
                0,
            )

    async def test_same_receipt_is_idempotent_and_foreign_owner_or_binding_cannot_access(self):
        first = self.submit()
        repeat = self.submit()
        self.assertEqual(first["id"], repeat["id"])
        await self.terminal(first["id"])
        self.importer.assert_awaited_once()
        self.assertEqual(self.jobs.list("other"), [])
        with self.assertRaises(ProfileTransferError):
            self.jobs.get("other", first["id"])
        with self.assertRaises(ProfileTransferError):
            self.jobs.restore_receipt(
                "session-1", "nonce", "admin", "http://manage.test", ("different-tab", "0")
            )
        restored = self.jobs.restore_receipt(
            "session-1", "nonce", "admin", "http://manage.test", ("42", "0")
        )
        self.assertEqual(restored["state"], "completed")

    async def test_queued_jobs_survive_restart_and_interrupted_job_requires_explicit_retry(self):
        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow(payload):
            entered.set()
            await release.wait()
            return {"success": True, "email": "member@example.test", "token_id": 7}

        self.jobs.importer = AsyncMock(side_effect=slow)
        first = self.submit("first")
        second = self.submit("second")
        await entered.wait()
        await self.jobs.close()
        restarted = ExtensionImportJobs(self.root, self.importer)
        self.addAsyncCleanup(restarted.close)
        first_state = restarted.get("admin", first["id"])
        self.assertEqual(first_state["state"], "failed")
        self.assertTrue(first_state["can_retry"])
        for _ in range(100):
            if restarted.get("admin", second["id"])["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(restarted.get("admin", second["id"])["state"], "completed")
        restarted.retry("admin", first["id"])
        for _ in range(100):
            if restarted.get("admin", first["id"])["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(restarted.get("admin", first["id"])["attempts"], 2)

    async def test_failure_retains_retryable_payload_without_leaking_exception(self):
        self.importer.side_effect = RuntimeError("synthetic-private-cookie")
        job = self.submit()
        failed = await self.terminal(job["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["can_retry"])
        self.assertNotIn("synthetic-private-cookie", json.dumps(failed))
        self.assertEqual(failed["attempts"], 1)
        self.jobs.list("admin")
        self.jobs.get("admin", job["id"])
        await asyncio.sleep(0)
        self.importer.assert_awaited_once()
        self.importer.side_effect = None
        self.jobs.retry("admin", job["id"])
        done = await self.terminal(job["id"])
        self.assertEqual(done["state"], "completed")

    async def test_capacity_rejection_does_not_acknowledge_or_erase_accepted_job(self):
        self.jobs.max_pending = 1
        first = self.submit("first")
        with self.assertRaises(ProfileTransferError) as caught:
            self.submit("second")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(self.jobs.list("admin")), 1)
        self.assertEqual(self.jobs.get("admin", first["id"])["id"], first["id"])

    async def test_second_queue_instance_cannot_interrupt_a_live_worker(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def importing(payload):
            entered.set()
            await release.wait()
            return {"success": True, "email": "member@example.test", "token_id": 7}

        self.jobs.importer = AsyncMock(side_effect=importing)
        submitted = self.submit()
        await entered.wait()
        second_importer = AsyncMock()
        observer = ExtensionImportJobs(self.root, second_importer)
        self.addAsyncCleanup(observer.close)
        self.assertEqual(observer.get("admin", submitted["id"])["state"], "running")
        await asyncio.sleep(0)
        self.assertEqual(observer.get("admin", submitted["id"])["state"], "running")
        second_importer.assert_not_awaited()
        release.set()
        self.assertEqual((await self.terminal(submitted["id"]))["state"], "completed")

    async def test_selected_proxy_is_encrypted_before_import_and_reused_after_restart_retry(self):
        binding = {"proxy_pool_id": 17, "proxy_url": "socks5://private:secret@fixed.invalid:1080"}
        prepare = AsyncMock(return_value={**PAYLOAD, "_extension_proxy_binding": binding})

        async def failed(payload):
            self.assertEqual(payload["_extension_proxy_binding"], binding)
            with closing(sqlite3.connect(self.jobs.path)) as db:
                encrypted = db.execute("SELECT payload FROM extension_imports").fetchone()[0]
            self.assertEqual(
                json.loads(self.jobs._cipher.decrypt(encrypted))["_extension_proxy_binding"],
                binding,
            )
            raise RuntimeError("synthetic verification failed")

        self.jobs.prepare_payload = prepare
        self.jobs.importer = AsyncMock(side_effect=failed)
        submitted = self.submit()
        self.assertEqual((await self.terminal(submitted["id"]))["state"], "failed")
        prepare.assert_awaited_once()
        self.assertNotIn(b"fixed.invalid", self.jobs.path.read_bytes())
        await self.jobs.close()

        retry_importer = AsyncMock(
            return_value={"success": True, "email": "member@example.test", "token_id": 7}
        )
        changed_pool = AsyncMock(side_effect=AssertionError("proxy reallocated"))
        restarted = ExtensionImportJobs(self.root, retry_importer, prepare_payload=changed_pool)
        self.addAsyncCleanup(restarted.close)
        restarted.retry("admin", submitted["id"])
        for _ in range(100):
            if restarted.get("admin", submitted["id"])["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(restarted.get("admin", submitted["id"])["state"], "completed")
        changed_pool.assert_not_awaited()
        self.assertEqual(retry_importer.await_args.args[0]["_extension_proxy_binding"], binding)

    async def test_proven_original_profile_binding_commits_before_retryable_import_failure(self):
        allocated = {"proxy_pool_id": 29, "proxy_url": "socks5://allocated.invalid:1080"}
        original = {"proxy_pool_id": 28, "proxy_url": "socks5://original.invalid:1080"}
        self.jobs.prepare_payload = AsyncMock(
            return_value={**PAYLOAD, "_extension_proxy_binding": allocated}
        )

        async def import_original(payload, *, on_proxy_binding):
            await on_proxy_binding(original)
            with closing(sqlite3.connect(self.jobs.path)) as db:
                encrypted = db.execute("SELECT payload FROM extension_imports").fetchone()[0]
            saved = json.loads(self.jobs._cipher.decrypt(encrypted))
            self.assertEqual(saved["_extension_proxy_binding"], original)
            self.assertEqual(saved["google_cookies"], PAYLOAD["google_cookies"])
            raise TimeoutError("synthetic original exit timeout")

        self.jobs.importer = import_original
        submitted = self.submit()
        self.assertEqual((await self.terminal(submitted["id"]))["state"], "failed")
        await self.jobs.close()
        importer = AsyncMock(
            return_value={"success": True, "email": "member@example.test", "token_id": 7}
        )
        restarted = ExtensionImportJobs(self.root, importer)
        self.addAsyncCleanup(restarted.close)
        restarted.retry("admin", submitted["id"])
        for _ in range(100):
            if restarted.get("admin", submitted["id"])["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(restarted.get("admin", submitted["id"])["state"], "completed")
        self.assertEqual(importer.await_args.args[0]["_extension_proxy_binding"], original)


if __name__ == "__main__":
    unittest.main()
