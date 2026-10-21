import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.studio_jobs import StudioGenerationJobs, StudioJobError


class StudioJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_returns_before_worker_finishes_and_replays_idempotently(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def execute(provider, body, key, base_url):
            started.set()
            await release.wait()
            yield {"status": "completed", "outputs": [{"kind": "image", "url": "/tmp/a.png"}]}

        with tempfile.TemporaryDirectory() as directory:
            service = StudioGenerationJobs(execute, Path(directory) / "jobs.json")
            job = await service.submit(
                "flow",
                {"model": "synthetic", "messages": [{"role": "user", "content": "x"}]},
                2,
                "request-1",
                "http://test",
                kind="image",
                model="synthetic",
                prompt="x",
            )
            self.assertEqual(job["state"], "queued")
            await asyncio.wait_for(started.wait(), 1)
            replay = await service.submit(
                "flow",
                {"model": "synthetic", "messages": [{"role": "user", "content": "x"}]},
                2,
                "request-1",
                "http://test",
                kind="image",
                model="synthetic",
                prompt="x",
            )
            self.assertEqual(replay["id"], job["id"])
            self.assertEqual(replay["completed"], 0)
            release.set()
            for _ in range(20):
                if service.get(job["id"])["state"] == "completed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(service.get(job["id"])["completed"], 2)
            await service.close()

    async def test_request_id_conflict_is_rejected(self):
        async def execute(*_):
            yield {"status": "completed"}

        with tempfile.TemporaryDirectory() as directory:
            service = StudioGenerationJobs(execute, Path(directory) / "jobs.json")
            await service.submit(
                "flow",
                {"model": "a"},
                1,
                "same",
                "http://test",
                kind="image",
                model="a",
                prompt="one",
            )
            with self.assertRaises(StudioJobError) as error:
                await service.submit(
                    "flow",
                    {"model": "b"},
                    1,
                    "same",
                    "http://test",
                    kind="image",
                    model="b",
                    prompt="two",
                )
            self.assertEqual(error.exception.status, 409)
            await service.close()

    async def test_vids_batches_serialize_paid_work_and_keep_partial_results(self):
        active = maximum = calls = 0

        async def execute(provider, body, key, base_url):
            nonlocal active, maximum, calls
            active += 1
            maximum = max(maximum, active)
            calls += 1
            try:
                await asyncio.sleep(0.01)
                if calls == 2:
                    raise StudioJobError("synthetic failure")
                yield {"status": "completed"}
            finally:
                active -= 1

        with tempfile.TemporaryDirectory() as directory:
            service = StudioGenerationJobs(execute, Path(directory) / "jobs.json")
            job = await service.submit(
                "vids",
                {"kind": "image"},
                3,
                "sequence",
                "http://test",
                kind="image",
                model="vids-image",
                prompt="x",
            )
            await service._queues["vids"].join()
            result = service.get(job["id"])
            self.assertEqual((maximum, calls), (1, 3))
            self.assertEqual(
                (result["state"], result["completed"], result["failed"]), ("partial", 2, 1)
            )
            self.assertEqual(len({item["key"] for item in result["items"]}), 3)
            await service.close()

    async def test_shutdown_and_restart_keep_receipts_without_resubmitting(self):
        started = asyncio.Event()

        async def execute(*_):
            started.set()
            await asyncio.Event().wait()
            yield {}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            service = StudioGenerationJobs(execute, path)
            body = {"kind": "video", "video_base64": "PRIVATE_INPUT"}
            job = await service.submit(
                "vids",
                body,
                2,
                "interrupted",
                "http://test",
                kind="video",
                model="vids-video",
                prompt="x",
            )
            await started.wait()
            await service.close()
            self.assertNotIn("PRIVATE_INPUT", path.read_text(encoding="utf-8"))
            resumed = StudioGenerationJobs(execute, path)
            result = resumed.get(job["id"])
            self.assertEqual([item["status"] for item in result["items"]], ["unknown", "failed"])
            self.assertTrue(result["items"][0]["receipt"].startswith("vids_"))
            replay = await resumed.submit(
                "vids",
                body,
                2,
                "interrupted",
                "http://test",
                kind="video",
                model="vids-video",
                prompt="x",
            )
            self.assertEqual(replay["id"], job["id"])
            self.assertEqual(resumed._workers, [])
            await resumed.close()

    async def test_full_queue_rejects_new_work_without_affecting_replays(self):
        async def execute(*_):
            await asyncio.Event().wait()
            yield {}

        with tempfile.TemporaryDirectory() as directory:
            service = StudioGenerationJobs(execute, Path(directory) / "jobs.json", max_pending=1)
            job = await service.submit(
                "flow", {}, 1, "first", "http://test", kind="image", model="a", prompt="x"
            )
            with self.assertRaises(StudioJobError) as error:
                await service.submit(
                    "flow", {}, 1, "second", "http://test", kind="image", model="a", prompt="x"
                )
            self.assertEqual(error.exception.status, 429)
            replay = await service.submit(
                "flow", {}, 1, "first", "http://test", kind="image", model="a", prompt="x"
            )
            self.assertEqual(replay["id"], job["id"])
            await service.close()

    async def test_receipt_refresh_advances_original_result_without_new_submission(self):
        async def execute(*_):
            yield {"status": "download_failed"}

        def refresh(item):
            return {
                "status": "completed",
                "vids_receipt": {"id": item["receipt"], "status": "completed"},
            }

        with tempfile.TemporaryDirectory() as directory:
            service = StudioGenerationJobs(execute, Path(directory) / "jobs.json", refresh=refresh)
            job = await service.submit(
                "vids", {}, 1, "receipt", "http://test", kind="image", model="a", prompt="x"
            )
            await service._queues["vids"].join()
            result = service.get(job["id"])
            self.assertEqual(result["state"], "completed")
            self.assertEqual(
                result["items"][0]["receipt"], result["items"][0]["vids_receipt"]["id"]
            )
            await service.close()

    async def test_storage_failure_does_not_kill_worker_or_leak_queue_capacity(self):
        calls = 0

        async def execute(*_):
            nonlocal calls
            calls += 1
            yield {"status": "completed"}

        with tempfile.TemporaryDirectory() as directory:
            service = StudioGenerationJobs(execute, Path(directory) / "jobs.json")
            job = await service.submit(
                "vids", {}, 1, "storage", "http://test", kind="image", model="a", prompt="x"
            )
            with patch.object(service, "_save", side_effect=OSError("synthetic disk full")):
                await service._queues["vids"].join()
            self.assertEqual(calls, 0)
            self.assertEqual(service.get(job["id"])["state"], "failed")
            self.assertEqual(service._bytes, 0)
            self.assertTrue(all(not worker.done() for worker in service._workers))
            await service.submit(
                "vids", {}, 1, "after-storage", "http://test", kind="image", model="a", prompt="x"
            )
            await service._queues["vids"].join()
            self.assertEqual(calls, 1)
            await service.close()


if __name__ == "__main__":
    unittest.main()
