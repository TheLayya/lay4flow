"""Video jobs use temporary SQLite and synthetic, cancellable executors only."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from src.core.database import Database
from src.core.storage.video_jobs import VideoJobError, VideoJobRepository
from src.services.video_jobs import VideoJobService

PARAMS = {"model": "veo_3_1", "prompt": "synthetic cat", "seconds": "8", "size": "1280x720"}
REQUEST = {"prompt": "synthetic cat", "images": [b"synthetic-private-image-bytes"]}
OWNER = "a" * 64
OTHER = "b" * 64


class VideoJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(str(Path(self.directory.name) / "jobs.db"))
        self.repository = VideoJobRepository(self.database)
        await self.repository.init()

    async def service(self, execute, **kwargs):
        service = VideoJobService(self.database, execute, **kwargs)
        await service.start()
        self.addAsyncCleanup(service.close)
        return service

    async def create(self, repository=None, **kwargs):
        return await (repository or self.repository).create(
            OWNER, **PARAMS, request_hash="synthetic-hash", **kwargs
        )

    async def wait_status(self, service, job_id, status):
        async with asyncio.timeout(3):
            while True:
                job = await service.get(job_id, OWNER)
                if job["status"] == status:
                    return job
                await asyncio.sleep(0.005)

    async def test_repository_concurrent_admission_idempotency_and_fences(self):
        other = VideoJobRepository(Database(self.database.db_path))
        first, second = await asyncio.gather(
            self.create(idempotency_key="same", max_pending=1),
            self.create(other, idempotency_key="same", max_pending=1),
        )
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(sum([first[1], second[1]]), 1)
        with self.assertRaises(VideoJobError) as full:
            await self.create(max_pending=1)
        self.assertEqual(full.exception.status_code, 429)
        job_id = first[0]["id"]
        self.assertTrue(await self.repository.claim(job_id, "worker-a"))
        self.assertFalse(await other.claim(job_id, "worker-b"))
        self.assertFalse(await other.update(job_id, "worker-b", 90))
        self.assertTrue(await self.repository.update(job_id, "worker-a", 41))
        self.assertTrue(await self.repository.update(job_id, "worker-a", 5))
        self.assertEqual((await self.repository.get(job_id, OWNER))["progress"], 41)
        self.assertFalse(
            await other.finish(job_id, "worker-b", asset_url="https://synthetic/video")
        )
        self.assertTrue(
            await self.repository.finish(job_id, "worker-a", asset_path="synthetic.mp4")
        )
        self.assertFalse(await self.repository.update(job_id, "worker-a", 5))
        self.assertFalse(await self.repository.finish(job_id, "worker-a", error={"code": "late"}))

    async def test_owner_scope_pagination_and_terminal_only_delete(self):
        jobs = [(await self.create())[0] for _ in range(3)]
        await self.repository.create(OTHER, **PARAMS, request_hash="other")
        self.assertIsNone(await self.repository.get(jobs[0]["id"], OTHER))
        self.assertFalse(await self.repository.delete(jobs[0]["id"], OTHER))
        with self.assertRaises(VideoJobError) as active:
            await self.repository.delete(jobs[0]["id"], OWNER)
        self.assertEqual(active.exception.status_code, 409)
        first = await self.repository.list(OWNER, limit=2, order="asc")
        second = await self.repository.list(OWNER, limit=2, after=first[-1]["id"], order="asc")
        self.assertEqual(len(first + second), 3)
        self.assertEqual(len({row["id"] for row in first + second}), 3)
        with self.assertRaises(VideoJobError):
            await self.repository.list(OTHER, after=first[0]["id"])
        with self.assertRaises(VideoJobError):
            await self.repository.list(OWNER, order="desc; DROP TABLE video_jobs")
        await self.repository.recover_interrupted()
        self.assertTrue(await self.repository.delete(jobs[0]["id"], OWNER))
        self.assertEqual(len(await self.repository.list(OWNER, limit=101)), 2)
        with self.assertRaises(VideoJobError):
            await self.repository.list(OWNER, limit=102)

    async def test_completion_has_public_metadata_and_private_asset_only(self):
        released = asyncio.Event()
        received = []

        async def execute(normalized, base_url):
            try:
                received.append((normalized, base_url))
                yield {"type": "progress", "progress": 55}
                yield {
                    "type": "completed",
                    "media": {
                        "type": "video",
                        "url": "https://synthetic.example/video.mp4",
                        "path": "synthetic.mp4",
                        "media_id": "synthetic-media",
                        "expires_at": 4_000_000_000,
                    },
                    "warnings": ["synthetic warning"],
                }
            finally:
                released.set()

        service = await self.service(execute)
        job = await service.submit(OWNER, REQUEST, PARAMS, "https://api.example")
        completed = await self.wait_status(service, job["id"], "completed")
        await asyncio.wait_for(released.wait(), 1)
        self.assertEqual(completed["object"], "video")
        self.assertEqual(completed["seconds"], "8")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(received, [(REQUEST, "https://api.example")])
        for key in (
            "asset_url",
            "asset_path",
            "media_id",
            "owner_hash",
            "request_hash",
            "idempotency_key",
        ):
            self.assertNotIn(key, completed)
        internal = await service.get_internal(job["id"], OWNER)
        self.assertEqual(internal["asset_path"], "synthetic.mp4")
        self.assertEqual(internal["warnings"], ["synthetic warning"])
        self.assertIsNone(await service.get_internal(job["id"], OTHER))
        self.assertEqual(len(await service.list(OWNER)), 1)
        self.assertTrue(await service.delete(job["id"], OWNER))
        self.assertIsNone(await service.get(job["id"], OWNER))

    async def test_fixed_workers_bounded_capacity_and_idempotency_in_full_queue(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        calls = []
        active = 0
        peak = 0

        async def execute(normalized, base_url):
            nonlocal active, peak
            calls.append(normalized)
            active += 1
            peak = max(active, peak)
            if len(calls) == 2:
                started.set()
            try:
                await gate.wait()
                yield {
                    "type": "completed",
                    "media": {"type": "video", "url": "https://synthetic/v"},
                }
            finally:
                active -= 1

        service = await self.service(execute, max_active=2, max_pending=3)
        jobs = [await service.submit(OWNER, {"index": i}, PARAMS, "", f"key-{i}") for i in range(3)]
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual(len(calls), 2)
        duplicate = await service.submit(OWNER, {"index": 0}, PARAMS, "", "key-0")
        self.assertEqual(duplicate["id"], jobs[0]["id"])
        with self.assertRaises(VideoJobError) as conflict:
            await service.submit(OWNER, {"index": 99}, PARAMS, "", "key-0")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        with self.assertRaises(VideoJobError) as full:
            await service.submit(OWNER, REQUEST, PARAMS, "")
        self.assertEqual(full.exception.status_code, 429)
        self.assertEqual(len(service._workers), 2)
        gate.set()
        for job in jobs:
            await self.wait_status(service, job["id"], "completed")
        self.assertEqual(len(calls), 3)
        self.assertEqual(peak, 2)

    async def test_singleton_lock_prevents_recovery_of_live_jobs(self):
        entered = asyncio.Event()

        async def execute(normalized, base_url):
            entered.set()
            await asyncio.Event().wait()
            yield {}

        first = await self.service(execute, max_active=1, max_pending=2)
        job = await first.submit(OWNER, REQUEST, PARAMS, "")
        await asyncio.wait_for(entered.wait(), 1)
        second = VideoJobService(Database(self.database.db_path), execute)
        self.addAsyncCleanup(second.close)
        with self.assertRaises(VideoJobError) as error:
            await second.start()
        self.assertEqual(error.exception.code, "video_worker_busy")
        self.assertEqual((await first.get(job["id"], OWNER))["status"], "in_progress")
        await first.close()
        await second.start()
        self.assertEqual((await second.get(job["id"], OWNER))["status"], "failed")

    async def test_restart_marks_old_queued_and_running_failed_without_replay(self):
        queued, _ = await self.create()
        running, _ = await self.create()
        await self.repository.claim(running["id"], "dead-worker")
        called = []

        async def execute(normalized, base_url):
            called.append(normalized)
            yield {}

        service = await self.service(execute)
        for job in (queued, running):
            current = await service.get(job["id"], OWNER)
            self.assertEqual(current["status"], "failed")
            self.assertEqual(current["error"]["code"], "interrupted")
        self.assertEqual(called, [])
        self.assertFalse(await self.repository.finish(running["id"], "dead-worker"))

    async def test_shutdown_waits_for_upstream_release_and_drops_in_memory_media(self):
        entered = asyncio.Event()
        released = asyncio.Event()
        calls = []

        async def execute(normalized, base_url):
            calls.append(normalized)
            entered.set()
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                await asyncio.sleep(0.005)
                released.set()

        service = await self.service(execute, max_active=1, max_pending=2)
        first = await service.submit(OWNER, REQUEST, PARAMS, "")
        second = await service.submit(OWNER, REQUEST, PARAMS, "")
        await asyncio.wait_for(entered.wait(), 1)
        await service.close()
        self.assertTrue(released.is_set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(service._queue.qsize(), 0)
        self.assertEqual(service._pending_bytes, 0)
        for job in (first, second):
            self.assertEqual((await service.get(job["id"], OWNER))["status"], "failed")
        async with self.database._connect() as db:
            rows = await (await db.execute("SELECT * FROM video_jobs")).fetchall()
        self.assertNotIn("synthetic-private-image-bytes", json.dumps([list(row) for row in rows]))

    async def test_failure_is_sanitized_and_worker_handles_next_job(self):
        calls = []

        async def execute(normalized, base_url):
            calls.append(normalized)
            if len(calls) == 1:
                raise RuntimeError("token=synthetic-secret-cookie")
            yield {"type": "completed", "media": {"type": "video", "url": "https://synthetic/v"}}

        service = await self.service(execute, max_active=1, max_pending=2)
        first = await service.submit(OWNER, REQUEST, PARAMS, "")
        second = await service.submit(OWNER, REQUEST, PARAMS, "")
        failed = await self.wait_status(service, first["id"], "failed")
        self.assertEqual(failed["error"]["code"], "generation_failed")
        self.assertNotIn("synthetic-secret-cookie", json.dumps(failed))
        await self.wait_status(service, second["id"], "completed")

    async def test_cancelled_client_after_commit_cannot_leave_unqueued_job(self):
        committed = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def execute(normalized, base_url):
            calls.append(normalized)
            yield {"type": "completed", "media": {"type": "video", "url": "https://synthetic/v"}}

        service = await self.service(execute, max_active=1, max_pending=2)
        create = service.repository.create

        async def delayed_create(*args, **kwargs):
            result = await create(*args, **kwargs)
            committed.set()
            await release.wait()
            return result

        service.repository.create = delayed_create
        submission = asyncio.create_task(service.submit(OWNER, REQUEST, PARAMS, "", "cancelled"))
        await asyncio.wait_for(committed.wait(), 1)
        submission.cancel()
        await asyncio.sleep(0)
        self.assertFalse(
            submission.done(), "admission completes before caller cancellation unwinds"
        )
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await submission
        duplicate = await service.submit(OWNER, REQUEST, PARAMS, "", "cancelled")
        await self.wait_status(service, duplicate["id"], "completed")
        self.assertEqual(len(calls), 1)

    async def test_deleted_job_keeps_idempotency_tombstone_across_restart(self):
        job, _ = await self.create(idempotency_key="never-charge-twice")
        await self.repository.claim(job["id"], "worker")
        await self.repository.finish(job["id"], "worker", asset_path="synthetic-private.mp4")
        self.assertTrue(await self.repository.delete(job["id"], OWNER))
        self.assertFalse(await self.repository.delete(job["id"], OWNER))
        restarted = VideoJobRepository(Database(self.database.db_path))
        await restarted.init()
        self.assertIsNone(await restarted.get(job["id"], OWNER))
        self.assertEqual(await restarted.list(OWNER), [])
        with self.assertRaises(VideoJobError) as deleted:
            await self.create(restarted, idempotency_key="never-charge-twice")
        self.assertEqual(deleted.exception.status_code, 409)
        self.assertEqual(deleted.exception.code, "idempotency_deleted")
        async with self.database._connect() as db:
            row = await (
                await db.execute(
                    "SELECT prompt,asset_path,request_hash,idempotency_key FROM video_jobs WHERE id=?",
                    (job["id"],),
                )
            ).fetchone()
        self.assertEqual(row, ("", None, "synthetic-hash", "never-charge-twice"))
        other, created = await restarted.create(
            OTHER, **PARAMS, request_hash="other", idempotency_key="never-charge-twice"
        )
        self.assertTrue(created)
        self.assertNotEqual(other["id"], job["id"])

    async def test_schema_adds_tombstone_column_to_existing_jobs(self):
        job, _ = await self.create()
        async with self.database._connect(write=True) as db:
            await db.execute("ALTER TABLE video_jobs DROP COLUMN deleted_at")
            await db.commit()
        await self.repository.init()
        self.assertEqual((await self.repository.get(job["id"], OWNER))["status"], "queued")

    async def test_safe_executor_codes_survive_without_upstream_exception_text(self):
        codes = ["upscale_failed", "output_size_mismatch", "unexpected_upstream_code"]

        async def execute(normalized, base_url):
            raise VideoJobError(502, normalized["code"], "synthetic-secret-cookie=must-not-leak")
            yield {}

        service = await self.service(execute)
        for code in codes:
            job = await service.submit(OWNER, {"code": code}, PARAMS, "")
            failed = await self.wait_status(service, job["id"], "failed")
            expected = "generation_failed" if code == "unexpected_upstream_code" else code
            self.assertEqual(failed["error"]["code"], expected)
            self.assertNotIn("synthetic-secret-cookie", json.dumps(failed))

    async def test_media_budget_includes_active_and_queued_replay_is_free_and_release_reopens(self):
        @dataclass
        class Normalized:
            images: list[bytes]
            video_bytes: bytes | None = None

        gate = asyncio.Event()
        entered = asyncio.Event()
        seen = []
        image = b"image"
        video = b"video"
        request = Normalized([image], video)

        async def execute(normalized, base_url):
            seen.append(normalized)
            entered.set()
            await gate.wait()
            yield {"type": "completed", "media": {"type": "video", "url": "https://synthetic/v"}}

        service = await self.service(execute, max_active=1, max_pending=10, max_pending_bytes=20)
        first = await service.submit(OWNER, request, PARAMS, "", "first")
        await asyncio.wait_for(entered.wait(), 1)
        second = await service.submit(OWNER, request, PARAMS, "", "second")
        self.assertEqual(service._pending_bytes, 20)
        replay = await service.submit(OWNER, request, PARAMS, "", "first")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(service._pending_bytes, 20)
        with self.assertRaises(VideoJobError) as full:
            await service.submit(OWNER, request, PARAMS, "", "third")
        self.assertEqual(full.exception.status_code, 429)
        self.assertEqual(full.exception.code, "media_capacity_full")
        self.assertEqual(len(await service.list(OWNER)), 2, "rejected admission writes no job")
        self.assertIs(seen[0].images[0], image, "immutable image bytes are not copied")
        self.assertIs(seen[0].video_bytes, video, "immutable video bytes are not copied")
        gate.set()
        await asyncio.wait_for(service._queue.join(), 1)
        self.assertEqual(service._pending_bytes, 0)
        third = await service.submit(OWNER, request, PARAMS, "", "third")
        await self.wait_status(service, third["id"], "completed")
        self.assertNotEqual(second["id"], third["id"])

    async def test_media_budget_released_after_failure(self):
        async def execute(normalized, base_url):
            raise RuntimeError("synthetic failure")
            yield {}

        service = await self.service(execute, max_active=1, max_pending=10, max_pending_bytes=5)
        first = await service.submit(OWNER, {"images": [b"image"]}, PARAMS, "")
        await self.wait_status(service, first["id"], "failed")
        await asyncio.wait_for(service._queue.join(), 1)
        self.assertEqual(service._pending_bytes, 0)
        second = await service.submit(OWNER, {"video_bytes": b"video"}, PARAMS, "")
        await self.wait_status(service, second["id"], "failed")


if __name__ == "__main__":
    unittest.main()
