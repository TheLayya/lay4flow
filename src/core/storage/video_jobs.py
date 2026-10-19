"""Durable video-job metadata. Request media stays out of SQLite."""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from ..database import Database


class VideoJobError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class VideoJobRepository:
    """Use Database's connection policy and SQLite transactions for admission."""

    def __init__(self, database: "Database"):
        self.database = database

    async def init(self) -> None:
        async with self.database._connect(write=True) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS video_jobs (
                    id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL,
                    model TEXT NOT NULL, prompt TEXT NOT NULL,
                    seconds TEXT NOT NULL, size TEXT NOT NULL,
                    status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL, completed_at INTEGER,
                    expires_at INTEGER, error TEXT, asset_url TEXT, asset_path TEXT,
                    media_id TEXT, warnings TEXT NOT NULL DEFAULT '[]',
                    request_hash TEXT NOT NULL, idempotency_key TEXT, worker_id TEXT,
                    deleted_at INTEGER,
                    UNIQUE(owner_hash, idempotency_key)
                )
            """)
            columns = await (await db.execute("PRAGMA table_info(video_jobs)")).fetchall()
            if not any(column[1] == "deleted_at" for column in columns):
                await db.execute("ALTER TABLE video_jobs ADD COLUMN deleted_at INTEGER")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_video_jobs_owner_created "
                "ON video_jobs(owner_hash, created_at, id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_video_jobs_status ON video_jobs(status)"
            )
            await db.commit()

    @staticmethod
    def _record(row) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["object"] = "video"
        value["error"] = json.loads(value["error"]) if value["error"] else None
        value["warnings"] = json.loads(value["warnings"])
        return value

    async def create(
        self,
        owner_hash: str,
        model: str,
        prompt: str,
        seconds: str,
        size: str,
        request_hash: str,
        idempotency_key: str | None = None,
        max_pending: int = 1000,
        *,
        media_capacity_available: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        async with self.database._connect(write=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            if idempotency_key is not None:
                cursor = await db.execute(
                    "SELECT * FROM video_jobs WHERE owner_hash=? AND idempotency_key=?",
                    (owner_hash, idempotency_key),
                )
                existing = self._record(await cursor.fetchone())
                if existing is not None:
                    if existing["deleted_at"] is not None:
                        raise VideoJobError(
                            409,
                            "idempotency_deleted",
                            "The video for this idempotency key was deleted.",
                        )
                    if existing["request_hash"] != request_hash:
                        raise VideoJobError(
                            409, "idempotency_conflict", "Idempotency key has different parameters."
                        )
                    return existing, False
            if not media_capacity_available:
                raise VideoJobError(
                    429, "media_capacity_full", "Pending video media capacity is full."
                )
            cursor = await db.execute(
                "SELECT COUNT(*) FROM video_jobs WHERE status IN ('queued','in_progress')"
            )
            if (await cursor.fetchone())[0] >= max_pending:
                raise VideoJobError(429, "queue_full", "Video job capacity is full. Retry later.")
            job_id = "video_" + uuid.uuid4().hex
            await db.execute(
                """INSERT INTO video_jobs
                    (id,owner_hash,model,prompt,seconds,size,status,created_at,request_hash,idempotency_key)
                    VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
                (
                    job_id,
                    owner_hash,
                    model,
                    prompt,
                    str(seconds),
                    size,
                    int(time.time()),
                    request_hash,
                    idempotency_key,
                ),
            )
            cursor = await db.execute("SELECT * FROM video_jobs WHERE id=?", (job_id,))
            created = self._record(await cursor.fetchone())
            await db.commit()
            return created, True

    async def get(self, job_id: str, owner_hash: str) -> dict[str, Any] | None:
        async with self.database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM video_jobs WHERE id=? AND owner_hash=? AND deleted_at IS NULL",
                (job_id, owner_hash),
            )
            return self._record(await cursor.fetchone())

    async def list(
        self, owner_hash: str, limit: int = 20, after: str | None = None, order: str = "desc"
    ) -> list[dict[str, Any]]:
        # Routes request one extra row to report has_more for a 100-item page.
        if order not in {"asc", "desc"} or not 1 <= limit <= 101:
            raise VideoJobError(400, "invalid_pagination", "Invalid pagination parameters.")
        async with self.database._connect() as db:
            db.row_factory = aiosqlite.Row
            params: list[Any] = [owner_hash]
            where = "owner_hash=? AND deleted_at IS NULL"
            if after:
                cursor = await db.execute(
                    "SELECT created_at,id FROM video_jobs "
                    "WHERE id=? AND owner_hash=? AND deleted_at IS NULL",
                    (after, owner_hash),
                )
                anchor = await cursor.fetchone()
                if anchor is None:
                    raise VideoJobError(404, "video_not_found", "Video cursor not found.")
                comparison = "<" if order == "desc" else ">"
                where += f" AND (created_at,id) {comparison} (?,?)"
                params.extend([anchor["created_at"], anchor["id"]])
            params.append(limit)
            cursor = await db.execute(
                f"SELECT * FROM video_jobs WHERE {where} "
                f"ORDER BY created_at {order},id {order} LIMIT ?",
                params,
            )
            return [self._record(row) for row in await cursor.fetchall()]

    async def claim(self, job_id: str, worker_id: str) -> bool:
        async with self.database._connect(write=True) as db:
            cursor = await db.execute(
                "UPDATE video_jobs SET status='in_progress',worker_id=? "
                "WHERE id=? AND status='queued'",
                (worker_id, job_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def update(self, job_id: str, worker_id: str, progress: int) -> bool:
        async with self.database._connect(write=True) as db:
            cursor = await db.execute(
                "UPDATE video_jobs SET progress=MAX(progress,?) "
                "WHERE id=? AND worker_id=? AND status='in_progress'",
                (max(0, min(99, int(progress))), job_id, worker_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def finish(
        self,
        job_id: str,
        worker_id: str,
        *,
        error: dict[str, str] | None = None,
        asset_url: str | None = None,
        asset_path: str | None = None,
        media_id: str | None = None,
        warnings: list[str] | None = None,
        expires_at: int | None = None,
    ) -> bool:
        async with self.database._connect(write=True) as db:
            cursor = await db.execute(
                """UPDATE video_jobs SET status=?, progress=CASE WHEN ? THEN 100 ELSE progress END,
                    completed_at=?,error=?,asset_url=?,asset_path=?,media_id=?,warnings=?,expires_at=?
                    WHERE id=? AND worker_id=? AND status='in_progress'""",
                (
                    "failed" if error else "completed",
                    not error,
                    int(time.time()),
                    json.dumps(error) if error else None,
                    asset_url,
                    asset_path,
                    media_id,
                    json.dumps(warnings or []),
                    expires_at,
                    job_id,
                    worker_id,
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def recover_interrupted(self) -> int:
        """Caller MUST hold the service's OS lock; never automatically resubmit."""
        error = json.dumps(
            {
                "code": "interrupted",
                "message": "Video processing was interrupted. Submit a new job.",
            }
        )
        async with self.database._connect(write=True) as db:
            cursor = await db.execute(
                "UPDATE video_jobs SET status='failed',completed_at=?,error=? "
                "WHERE status IN ('queued','in_progress')",
                (int(time.time()), error),
            )
            await db.commit()
            return cursor.rowcount

    async def delete(self, job_id: str, owner_hash: str) -> bool:
        async with self.database._connect(write=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT status FROM video_jobs WHERE id=? AND owner_hash=? AND deleted_at IS NULL",
                (job_id, owner_hash),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            if row[0] not in {"completed", "failed"}:
                raise VideoJobError(409, "video_in_progress", "An active video cannot be deleted.")
            await db.execute(
                "UPDATE video_jobs SET deleted_at=?,prompt='',asset_url=NULL,asset_path=NULL,"
                "media_id=NULL,warnings='[]',error=NULL WHERE id=? AND owner_hash=?",
                (int(time.time()), job_id, owner_hash),
            )
            await db.commit()
            return True
