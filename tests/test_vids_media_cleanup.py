"""Synthetic filesystem and replay checks for shared-timeout Vids media expiry."""

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.api.openai_common import openai_http_exception_handler
from src.api.vids import create_vids_app
from src.api.vids_media import PREFIX, create_vids_media_router
from src.services.vids.dispatcher import VidsAccount, VidsProviderDispatcher
from src.services.vids.gateway import GatewayError, VidsGateway
from src.services.vids.media_cleanup import (
    MediaBusyError,
    VidsFileResponse,
    VidsMediaCleanup,
    _RuntimeLock,
    media_lease,
    prune_completed_media,
)
from tests.test_vids_gateway import KEY, client


class VidsMediaCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.identity = hashlib.sha256(b"same-account").hexdigest()
        (self.root / ".account").write_text(self.identity, encoding="ascii")

    def output(self, key="first", kind="speech", status="completed", count=1, **extra):
        identifier = "vids_" + hashlib.sha256(key.encode()).hexdigest()
        suffix, mime = {
            "speech": (".wav", "audio/wav"),
            "image": (".jpg", "image/jpeg"),
            "video": (".mp4", "video/mp4"),
        }[kind]
        record = {
            "id": identifier,
            "kind": kind,
            "status": status,
            "created_at": 100,
            "updated_at": 200,
            "completed_at": 200,
            "error": None,
            "suffix": suffix,
            "mime_type": mime,
            "request_hash": "synthetic-request",
            **extra,
        }
        if count > 1:
            record["outputs"] = [{"index": i, "metadata": {"bytes": 4}} for i in range(count)]
        receipt = self.root / (identifier + ".json")
        receipt.write_text(json.dumps(record), encoding="utf-8")
        files = [self.root / (identifier + (f"_{i}" if i else "") + suffix) for i in range(count)]
        for path in files:
            path.write_bytes(b"synthetic-media")
            os.utime(path, (200, 200))
        return record, files

    def prune(self, timeout=300, **kwargs):
        return prune_completed_media(
            self.root, timeout, expected_identity=self.identity, now=1000, **kwargs
        )

    async def test_only_completed_media_expire_receipts_sessions_and_unconfirmed_work_remain(self):
        records = []
        for kind in ("speech", "video", "image"):
            records.append(self.output(kind, kind, count=3 if kind == "image" else 1))
        protected = [
            self.output(state, status=state)
            for state in ("received", "generating", "unknown", "download_failed", "failed")
        ]
        inserted = self.output("insert", operation="insert")
        for name in (".session.enc", "secret.key", "unrelated.mp4"):
            (self.root / name).write_bytes(b"private-state")
        result = self.prune()
        self.assertEqual((result["expired"], result["files_removed"]), (3, 5))
        for record, files in records:
            self.assertTrue(all(not p.exists() for p in files))
            saved = json.loads((self.root / (record["id"] + ".json")).read_text())
            self.assertEqual({k: saved[k] for k in record}, record)
            self.assertTrue(saved["media_expired"])
            self.assertEqual(saved["status"], "completed")
        self.assertTrue(all(p.exists() for _, files in [*protected, inserted] for p in files))
        self.assertTrue((self.root / ".session.enc").exists())
        self.assertTrue((self.root / "unrelated.mp4").exists())

    async def test_zero_timeout_and_fresh_file_or_recent_restore_prevent_expiry(self):
        record, files = self.output()
        self.assertEqual(self.prune(0)["files_removed"], 0)
        os.utime(files[0], (900, 900))
        self.assertEqual(self.prune()["files_removed"], 0)
        os.utime(files[0], (200, 200))
        record["media_cached_at"] = 900
        (self.root / (record["id"] + ".json")).write_text(json.dumps(record))
        self.assertEqual(self.prune()["files_removed"], 0)
        self.assertEqual(self.prune(100)["files_removed"], 1)

    async def test_unsafe_media_receipts_pins_and_roots_are_skipped(self):
        record, files = self.output()
        outside = self.root / "outside"
        outside.write_bytes(b"retained")
        files[0].unlink()
        try:
            os.link(outside, files[0])
        except OSError:
            self.skipTest("Hard links unavailable")
        self.assertEqual(self.prune()["files_removed"], 0)
        self.assertEqual(outside.read_bytes(), b"retained")
        files[0].unlink()
        try:
            files[0].symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable")
        self.assertEqual(self.prune()["files_removed"], 0)
        self.assertTrue(files[0].is_symlink())
        self.assertEqual(
            prune_completed_media(self.root, 300, expected_identity="f" * 64, now=1000)[
                "files_removed"
            ],
            0,
        )
        bad = self.root / ("vids_" + "f" * 64 + ".json")
        bad.write_text('{"status":"completed","id":"../outside"}')
        self.assertEqual(self.prune()["files_removed"], 0)

    async def test_hot_timeout_change_stops_sweep_before_deleting(self):
        self.output()
        self.assertEqual(self.prune(timeout_current=lambda: 0)["files_removed"], 0)

    async def test_reader_lease_skips_cleanup_and_queued_response_reports_expiry(self):
        record, files = self.output()
        response = VidsFileResponse(files[0], media_type="audio/wav")
        with media_lease(files[0]):
            result = await asyncio.to_thread(self.prune)
            self.assertEqual(result["files_removed"], 0)
        self.assertEqual(self.prune()["files_removed"], 1)
        messages = []

        async def send(value):
            messages.append(value)

        await response(
            {"type": "http", "method": "GET", "headers": [], "extensions": {}}, AsyncMock(), send
        )
        self.assertEqual(messages[0]["status"], 410)
        self.assertTrue(json.loads(messages[1]["body"])["error"]["code"] == "expired")
        self.assertTrue((self.root / (record["id"] + ".json")).exists())

    async def test_file_response_holds_lease_until_transfer_finishes(self):
        _, files = self.output()
        started, release = asyncio.Event(), asyncio.Event()
        messages = []

        async def send(value):
            messages.append(value)
            if value["type"] == "http.response.start":
                started.set()
                await release.wait()

        task = asyncio.create_task(
            VidsFileResponse(files[0])(
                {"type": "http", "method": "GET", "headers": [], "extensions": {}},
                AsyncMock(),
                send,
            )
        )
        await started.wait()
        try:
            self.assertEqual((await asyncio.to_thread(self.prune))["files_removed"], 0)
        finally:
            release.set()
            await task
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(self.prune()["files_removed"], 1)

    async def test_slow_disk_cleanup_does_not_block_event_loop_or_stream_lease(self):
        _, files = self.output()
        started, release = threading.Event(), threading.Event()
        from src.services.vids import media_cleanup

        write = media_cleanup._atomic_receipt

        def slow_write(*args):
            started.set()
            release.wait(timeout=5)
            return write(*args)

        with patch.object(media_cleanup, "_atomic_receipt", side_effect=slow_write):
            cleanup = asyncio.create_task(asyncio.to_thread(self.prune))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            messages = []

            async def send(value):
                messages.append(value)

            response = asyncio.create_task(
                VidsFileResponse(files[0])(
                    {"type": "http", "method": "GET", "headers": [], "extensions": {}},
                    AsyncMock(),
                    send,
                )
            )
            try:
                with self.assertRaises(MediaBusyError):
                    with media_lease(files[0]):
                        self.fail("Writer must exclude new sync readers")
                await asyncio.wait_for(asyncio.sleep(0.03), timeout=0.2)
                self.assertFalse(response.done())
            finally:
                release.set()
                await cleanup
                await response
            self.assertEqual(messages[0]["status"], 410)

    async def gateway(self):
        gateway = VidsGateway(client(), self.root)
        await gateway.start()
        self.addAsyncCleanup(gateway.close)
        return gateway

    async def test_expired_replay_keeps_paid_outcome_and_explicit_download_only_restores(self):
        gateway = await self.gateway()
        body = {"kind": "speech", "prompt": "Synthetic voice", "voice": "Puck"}
        record = await gateway.generate(body, "same-key")
        path, _ = gateway.content(record["id"])
        os.utime(path, (200, 200))
        raw = gateway.get(record["id"])
        raw.update(created_at=100, updated_at=200, completed_at=200, media_cached_at=200)
        gateway._path(record["id"]).write_text(json.dumps(raw))
        self.assertEqual(self.prune()["files_removed"], 1)
        replay = await gateway.generate(body, "same-key")
        self.assertEqual(
            (replay["status"], replay["content_url"], replay["media_expired"]),
            ("completed", None, True),
        )
        gateway.client.generate_speech.assert_awaited_once()
        with self.assertRaises(GatewayError) as error:
            gateway.content(record["id"])
        self.assertEqual(error.exception.status, 410)
        recovered = await gateway.retry_download(record["id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertNotIn("media_expired", recovered)
        self.assertEqual(gateway.get(record["id"])["updated_at"], 200)
        self.assertEqual(gateway.get(record["id"])["completed_at"], 200)
        self.assertEqual(gateway.client.download_audio.await_count, 2)
        gateway.client.generate_speech.assert_awaited_once()

    async def test_failed_or_cancelled_restore_preserves_original_settled_receipt(self):
        gateway = await self.gateway()
        record = await gateway.generate(
            {"kind": "speech", "prompt": "Synthetic", "voice": "Puck"}, "failed-restore"
        )
        raw = gateway.get(record["id"])
        raw.update(media_expired=True, media_expired_at=100)
        gateway._path(record["id"]).write_text(json.dumps(raw))
        original = gateway.get(record["id"])
        for error in (ValueError("synthetic"), asyncio.CancelledError()):
            gateway.client.download_audio.side_effect = error
            with self.assertRaises((GatewayError, asyncio.CancelledError)):
                await gateway.retry_download(record["id"])
            self.assertEqual(gateway.get(record["id"]), original)
        gateway.client.generate_speech.assert_awaited_once()

    async def test_expired_content_http_returns_410_and_receipt_remains_completed(self):
        gateway = await self.gateway()
        raw, _ = self.output()
        raw["media_expired"] = True
        gateway._path(raw["id"]).write_text(json.dumps(raw))
        app = create_vids_app(gateway, KEY)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="https://synthetic.invalid",
            headers={"Authorization": "Bearer " + KEY},
        ) as http:
            response = await http.get("/v1/vids/generations/" + raw["id"] + "/content")
            self.assertEqual(response.status_code, 410)
            receipt = (await http.get("/v1/vids/generations/" + raw["id"])).json()
            self.assertEqual(receipt["status"], "completed")
            self.assertTrue(receipt["media_expired"])
            self.assertIsNone(receipt["content_url"])

    async def test_expired_multi_image_production_receipt_uses_null_urls_and_preserves_affinity(
        self,
    ):
        from fastapi import FastAPI

        gateway = await self.gateway()
        record, _ = self.output("multiple", kind="image", count=3)
        self.assertEqual(self.prune()["files_removed"], 3)
        db = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(is_active=True)))
        dispatcher = VidsProviderDispatcher(db)
        dispatcher.register(VidsAccount(1, gateway, self.identity))
        app = FastAPI()
        app.add_exception_handler(Exception, openai_http_exception_handler)
        app.include_router(create_vids_media_router(dispatcher, lambda: True))
        prefix = PREFIX.format(token_id=1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://synthetic.invalid"
        ) as http:
            receipt = (await http.get(prefix + "/v1/vids/generations/" + record["id"])).json()
            self.assertEqual(receipt["status"], "completed")
            self.assertTrue(receipt["media_expired"])
            self.assertIsNone(receipt["content_url"])
            self.assertTrue(all(row["content_url"] is None for row in receipt["outputs"]))
            self.assertEqual(
                receipt["retry_download_url"],
                prefix + "/v1/vids/generations/" + record["id"] + "/download",
            )
            denied = await http.get(
                PREFIX.format(token_id=2) + "/v1/vids/generations/" + record["id"]
            )
            self.assertEqual(denied.status_code, 404)

    async def test_expired_source_rejects_new_generation_but_same_key_remains_replay(self):
        gateway = await self.gateway()
        from tests.test_vids_video_gateway import SOURCE_RESOURCE

        source, _ = self.output("source", kind="video", resource=SOURCE_RESOURCE)
        source["media_expired"] = True
        gateway._path(source["id"]).write_text(json.dumps(source))
        body = {"kind": "video", "mode": "extend", "prompt": "Synthetic", "source_id": source["id"]}
        with self.assertRaises(GatewayError) as caught:
            await gateway.generate(body, "new-extension")
        self.assertEqual(caught.exception.status, 410)
        self.assertFalse(
            gateway._path("vids_" + hashlib.sha256(b"new-extension").hexdigest()).exists()
        )
        key = "already-submitted"
        identifier = "vids_" + hashlib.sha256(key.encode()).hexdigest()
        normalized = gateway.normalize(body)
        replay = {
            **source,
            "id": identifier,
            "request_hash": hashlib.sha256(
                json.dumps(normalized, sort_keys=True).encode()
            ).hexdigest(),
        }
        gateway._path(identifier).write_text(json.dumps(replay))
        self.assertEqual((await gateway.generate(body, key))["status"], "completed")

    async def test_runtime_tick_handles_busy_and_unavailable_bindings_without_network(self):
        record, files = self.output()
        (self.root / ".lock").write_bytes(b"0")
        runtime = SimpleNamespace(
            _lifecycle_lock=asyncio.Lock(),
            binding_rows={1: {"runtime_dir": str(self.root), "identity_hash": self.identity}},
            dispatcher=SimpleNamespace(_accounts={}, _preflight_locks={}, _pool_pending=set()),
        )
        timeout = 0
        worker = VidsMediaCleanup(runtime, lambda: timeout)
        await worker.tick()
        self.assertTrue(files[0].exists())
        timeout = 1
        await worker.tick()
        self.assertFalse(files[0].exists())
        self.assertEqual(worker.public_stats()["files_removed"], 1)
        self.assertEqual(
            json.loads((self.root / (record["id"] + ".json")).read_text())["status"], "completed"
        )
        await worker.close()

    async def test_exclusive_runtime_lock_skips_another_owner(self):
        _, files = self.output()
        (self.root / ".lock").write_bytes(b"0")
        lock = _RuntimeLock(self.root)
        self.assertTrue(lock.acquire())
        other = _RuntimeLock(self.root)
        self.assertFalse(other.acquire())
        lock.release()
        self.assertTrue(other.acquire())
        other.release()
        self.assertTrue(files[0].exists())

    async def test_saved_binding_cleanup_survives_empty_failed_runtime_without_network(self):
        record, files = self.output()
        (self.root / ".lock").write_bytes(b"0")
        runtime = SimpleNamespace(
            _lifecycle_lock=asyncio.Lock(),
            binding_rows={},
            dispatcher=SimpleNamespace(_accounts={}, _preflight_locks={}, _pool_pending=set()),
        )
        binding = {"token_id": 1, "runtime_dir": str(self.root), "identity_hash": self.identity}
        worker = VidsMediaCleanup(runtime, lambda: 1, get_bindings=lambda: [binding])
        await worker.tick()
        self.assertFalse(files[0].exists())
        self.assertTrue((self.root / (record["id"] + ".json")).exists())
        self.assertEqual(worker.public_stats()["files_removed"], 1)

    async def test_saved_binding_conflicts_links_and_external_runtime_lock_skip_cleanup(self):
        _, files = self.output()
        (self.root / ".lock").write_bytes(b"0")
        runtime = SimpleNamespace(
            _lifecycle_lock=asyncio.Lock(),
            binding_rows={},
            dispatcher=SimpleNamespace(_accounts={}, _preflight_locks={}, _pool_pending=set()),
        )
        binding = {"token_id": 1, "runtime_dir": str(self.root), "identity_hash": self.identity}
        rows = [binding, {**binding, "identity_hash": "f" * 64}]
        worker = VidsMediaCleanup(runtime, lambda: 1, get_bindings=lambda: rows)
        await worker.tick()
        self.assertTrue(files[0].exists())
        rows[:] = [binding, {**binding, "token_id": 2}]
        await worker.tick()
        self.assertTrue(files[0].exists())
        rows[:] = [binding]
        lock = _RuntimeLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            await worker.tick()
            self.assertTrue(files[0].exists())
        finally:
            lock.release()
        rows[:] = [{**binding, "runtime_dir": "relative/path"}, {**binding, "token_id": True}]
        await worker.tick()
        self.assertTrue(files[0].exists())

    async def test_cancelled_worker_retains_lifecycle_and_gateway_locks_until_disk_work_drains(
        self,
    ):
        gateway = await self.gateway()
        runtime = SimpleNamespace(
            _lifecycle_lock=asyncio.Lock(),
            binding_rows={},
            dispatcher=SimpleNamespace(
                _accounts={1: SimpleNamespace(gateway=gateway, identity_hash=self.identity)},
                _preflight_locks={},
                _pool_pending=set(),
            ),
        )
        worker = VidsMediaCleanup(runtime, lambda: 1)
        started, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            started.set()
            release.wait(5)
            return dict.fromkeys(
                ("scanned", "expired", "files_removed", "bytes_reclaimed", "skipped"), 0
            )

        with patch("src.services.vids.media_cleanup.prune_completed_media", side_effect=slow):
            task = asyncio.create_task(worker.tick())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(runtime._lifecycle_lock.locked())
                self.assertTrue(gateway.lock.locked())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
        self.assertFalse(runtime._lifecycle_lock.locked())
        self.assertFalse(gateway.lock.locked())


if __name__ == "__main__":
    unittest.main()
