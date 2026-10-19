"""Task and request-log persistence using the owning Database connection policy."""

import json
from typing import TYPE_CHECKING, Any, Dict, Optional

import aiosqlite

from ..models import RequestLog, Task

if TYPE_CHECKING:
    from ..database import Database


class GenerationHistoryRepository:
    """Keep SQL here while Database owns connections, write locks, and schema."""

    def __init__(self, database: "Database"):
        self._database = database

    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO tasks (task_id, token_id, model, prompt, status, progress, scene_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    task.task_id,
                    task.token_id,
                    task.model,
                    task.prompt,
                    task.status,
                    task.progress,
                    task.scene_id,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                task_dict = dict(row)
                # Parse result_urls from JSON
                if task_dict.get("result_urls"):
                    task_dict["result_urls"] = json.loads(task_dict["result_urls"])
                return Task(**task_dict)
            return None

    async def update_task(self, task_id: str, **kwargs):
        """Update task"""
        async with self._database._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    # Convert list to JSON string for result_urls
                    if key == "result_urls" and isinstance(value, list):
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(task_id)
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
                await db.execute(query, params)
                await db.commit()

    async def add_request_log(self, log: RequestLog) -> int:
        """Add request log and return log id"""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO request_logs (token_id, operation, request_body, response_body, status_code, duration, status_text, progress)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    log.token_id,
                    log.operation,
                    log.request_body,
                    log.response_body,
                    log.status_code,
                    log.duration,
                    log.status_text or "",
                    log.progress,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def update_request_log(self, log_id: int, **kwargs):
        """Update an existing request log row."""
        if not kwargs:
            return

        allowed_fields = {
            "token_id",
            "operation",
            "request_body",
            "response_body",
            "status_code",
            "duration",
            "status_text",
            "progress",
        }
        update_fields = {key: value for key, value in kwargs.items() if key in allowed_fields}
        if not update_fields:
            return

        clauses = []
        values = []
        for key, value in update_fields.items():
            clauses.append(f"{key} = ?")
            values.append(value)
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(log_id)

        async with self._database._connect(write=True) as db:
            await db.execute(
                f"UPDATE request_logs SET {', '.join(clauses)} WHERE id = ?",
                values,
            )
            await db.commit()

    async def get_logs(
        self, limit: int = 100, token_id: Optional[int] = None, include_payload: bool = False
    ):
        """Get request logs with token info, optionally including payload fields"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            payload_columns = "rl.request_body, rl.response_body," if include_payload else ""
            response_excerpt_column = (
                "substr(COALESCE(rl.response_body, ''), 1, 2048) as response_body_excerpt,"
            )
            has_status_text = await self._database._column_exists(db, "request_logs", "status_text")
            has_progress = await self._database._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._database._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = (
                "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"
            )

            token_filter = "WHERE rl.token_id = ?" if token_id else ""
            params = (token_id, limit) if token_id else (limit,)
            cursor = await db.execute(
                f"""
                SELECT
                    rl.id,
                    rl.token_id,
                    rl.operation,
                    {payload_columns}
                    {response_excerpt_column}
                    rl.status_code,
                    rl.duration,
                    {status_text_column}
                    {progress_column}
                    rl.created_at,
                    {updated_at_column}
                    t.email as token_email,
                    t.name as token_username
                FROM request_logs rl
                LEFT JOIN tokens t ON rl.token_id = t.id
                {token_filter}
                ORDER BY rl.created_at DESC
                LIMIT ?
            """,
                params,
            )

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_log_detail(self, log_id: int) -> Optional[Dict[str, Any]]:
        """Get single request log detail including payload fields"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            has_status_text = await self._database._column_exists(db, "request_logs", "status_text")
            has_progress = await self._database._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._database._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = (
                "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"
            )
            cursor = await db.execute(
                f"""
                SELECT
                    rl.id,
                    rl.token_id,
                    rl.operation,
                    rl.request_body,
                    rl.response_body,
                    rl.status_code,
                    rl.duration,
                    {status_text_column}
                    {progress_column}
                    rl.created_at,
                    {updated_at_column}
                    t.email as token_email,
                    t.name as token_username
                FROM request_logs rl
                LEFT JOIN tokens t ON rl.token_id = t.id
                WHERE rl.id = ?
                LIMIT 1
            """,
                (log_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_all_logs(self):
        """Clear all request logs"""
        async with self._database._connect(write=True) as db:
            await db.execute("DELETE FROM request_logs")
            await db.commit()
