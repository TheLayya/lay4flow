"""Bounded video execution with persistent status and no restart resubmission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from ..core.storage.video_jobs import VideoJobError, VideoJobRepository

PUBLIC_FIELDS = (
    "id",
    "object",
    "created_at",
    "status",
    "progress",
    "model",
    "prompt",
    "seconds",
    "size",
    "completed_at",
    "error",
    "expires_at",
)
SAFE_JOB_ERRORS = {
    "generation_failed": "Video generation failed.",
    "upscale_failed": "Requested video resolution could not be produced.",
    "output_size_mismatch": "Generated video dimensions do not match the requested size.",
    "missing_asset": "Generation returned no video.",
    "asset_expired": "Generated video is no longer available.",
    "asset_too_large": "Generated video is too large.",
    "output_duration_mismatch": "Generated video duration does not match the requested duration.",
}


def public_job(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in PUBLIC_FIELDS}


def _digest_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    raise TypeError("Unsupported normalized request type")


def _media_bytes(normalized) -> int:
    get = (
        normalized.get
        if isinstance(normalized, dict)
        else lambda key: getattr(normalized, key, None)
    )
    values = [*(get("images") or []), get("video_bytes")]
    return sum(
        memoryview(value).nbytes
        for value in values
        if isinstance(value, (bytes, bytearray, memoryview))
    )


class VideoJobService:
    """One process per SQLite file; fixed workers and a bounded in-memory queue.

    The OS lock is held for the service lifetime and released by a crashed
    process. A second process cannot mark a live process's jobs interrupted.
    Queued media is intentionally not persisted; restart marks pending jobs
    failed rather than risking duplicate upstream submissions.
    """

    def __init__(
        self,
        database,
        execute,
        *,
        max_active: int = 4,
        max_pending: int = 100,
        max_pending_bytes: int = 256 * 1024 * 1024,
    ):
        if max_active < 1 or max_pending < max_active or max_pending_bytes < 1:
            raise ValueError("Require 1 <= max_active <= max_pending")
        self.repository = VideoJobRepository(database)
        self.execute = execute
        self.max_active = max_active
        self.max_pending = max_pending
        self.max_pending_bytes = max_pending_bytes
        self._pending_bytes = 0
        self._queue = asyncio.Queue(maxsize=max_pending)
        self._workers: list[asyncio.Task] = []
        self._admission = asyncio.Lock()
        self._lock_file = None
        self._running = False

    def _acquire_lock(self):
        path = Path(self.repository.database.db_path).resolve()
        handle = open(str(path) + ".video-jobs.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise VideoJobError(
                503, "video_worker_busy", "Another video worker owns this database."
            ) from error
        self._lock_file = handle

    def _release_lock(self):
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    async def start(self) -> None:
        async with self._admission:
            if self._running:
                return
            self._acquire_lock()
            try:
                await self.repository.init()
                await self.repository.recover_interrupted()
                self._running = True
                self._workers = [
                    asyncio.create_task(self._work(uuid.uuid4().hex), name=f"video-worker-{i}")
                    for i in range(self.max_active)
                ]
            except BaseException:
                self._release_lock()
                raise

    async def close(self) -> None:
        async with self._admission:
            if not self._running:
                return
            self._running = False
            try:
                for worker in self._workers:
                    worker.cancel()
                await asyncio.gather(*self._workers, return_exceptions=True)
                self._workers.clear()
                while not self._queue.empty():
                    _, _, _, media_bytes = self._queue.get_nowait()
                    self._pending_bytes -= media_bytes
                    self._queue.task_done()
                await self.repository.recover_interrupted()
            finally:
                self._release_lock()

    async def submit(
        self,
        owner_hash: str,
        normalized,
        params: dict[str, Any],
        base_url: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 256):
            raise VideoJobError(400, "invalid_idempotency_key", "Invalid idempotency key.")
        try:
            serialized = json.dumps(
                {"request": normalized, "params": params},
                sort_keys=True,
                separators=(",", ":"),
                default=_digest_value,
            )
        except (TypeError, ValueError) as error:
            raise VideoJobError(
                400, "invalid_request", "Invalid normalized video request."
            ) from error
        request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        async with self._admission:
            if not self._running:
                raise VideoJobError(503, "video_unavailable", "Video job service is not running.")
            admission = asyncio.create_task(
                self._persist_and_enqueue(
                    owner_hash,
                    normalized,
                    params,
                    base_url,
                    request_hash,
                    idempotency_key,
                )
            )
            try:
                return await asyncio.shield(admission)
            except asyncio.CancelledError:
                # At most one admission task exists under this lock. Finish the
                # commit/enqueue pair even if the client's response is cancelled.
                await admission
                raise

    async def _persist_and_enqueue(
        self, owner_hash, normalized, params, base_url, request_hash, idempotency_key
    ):
        media_bytes = _media_bytes(normalized)
        job, created = await self.repository.create(
            owner_hash,
            params["model"],
            params["prompt"],
            str(params["seconds"]),
            params["size"],
            request_hash,
            idempotency_key,
            self.max_pending,
            media_capacity_available=self._pending_bytes + media_bytes <= self.max_pending_bytes,
        )
        if created:
            # Workers only remove entries; the durable pending count bounds
            # this queue plus all active executions.
            self._pending_bytes += media_bytes
            self._queue.put_nowait((job["id"], normalized, base_url, media_bytes))
        return public_job(job)

    async def get(self, job_id: str, owner_hash: str) -> dict[str, Any] | None:
        record = await self.repository.get(job_id, owner_hash)
        return public_job(record) if record else None

    async def get_internal(self, job_id: str, owner_hash: str) -> dict[str, Any] | None:
        return await self.repository.get(job_id, owner_hash)

    async def list(
        self, owner_hash: str, limit: int = 20, after: str | None = None, order: str = "desc"
    ) -> list[dict[str, Any]]:
        return [
            public_job(row) for row in await self.repository.list(owner_hash, limit, after, order)
        ]

    async def delete(self, job_id: str, owner_hash: str) -> bool:
        return await self.repository.delete(job_id, owner_hash)

    async def _run(self, job_id: str, worker_id: str, normalized, base_url: str):
        stream = self.execute(normalized, base_url)
        try:
            async for event in stream:
                if event.get("type") == "progress":
                    await self.repository.update(job_id, worker_id, event.get("progress", 0))
                elif event.get("type") == "completed":
                    media = event.get("media") or {}
                    if media.get("type") != "video" or not (
                        media.get("url") or media.get("asset_path") or media.get("path")
                    ):
                        break
                    await self.repository.finish(
                        job_id,
                        worker_id,
                        asset_url=media.get("url"),
                        asset_path=media.get("asset_path") or media.get("path"),
                        media_id=media.get("media_id"),
                        warnings=event.get("warnings") or [],
                        expires_at=media.get("expires_at"),
                    )
                    return
                elif event.get("type") in {"error", "failed"}:
                    break
            await self._fail(job_id, worker_id)
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    async def _fail(self, job_id: str, worker_id: str, error: Exception | None = None):
        # Upstream exception strings may contain signed URLs or credentials.
        code = (
            error.code
            if isinstance(error, VideoJobError) and error.code in SAFE_JOB_ERRORS
            else "generation_failed"
        )
        await self.repository.finish(
            job_id,
            worker_id,
            error={"code": code, "message": SAFE_JOB_ERRORS[code]},
        )

    async def _work(self, worker_id: str):
        while True:
            job_id, normalized, base_url, media_bytes = await self._queue.get()
            try:
                if await self.repository.claim(job_id, worker_id):
                    try:
                        await self._run(job_id, worker_id, normalized, base_url)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        await self._fail(job_id, worker_id, error)
            finally:
                self._pending_bytes -= media_bytes
                self._queue.task_done()
                # Do not retain input media while this worker waits for a job.
                normalized = None
