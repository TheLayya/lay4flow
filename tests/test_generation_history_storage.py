"""Real SQLite checks for the Database task/log compatibility facade."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.core.database import Database
from src.core.models import RequestLog, Task, Token


class GenerationHistoryStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / "history.db"))
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(st="synthetic-session", email="account@example.invalid", name="Account")
        )

    def task(self, task_id="task-1", **values):
        return Task(
            **{
                "task_id": task_id,
                "token_id": self.token_id,
                "model": "omni_1.1_flash",
                "prompt": "Synthetic task",
                "status": "processing",
                **values,
            }
        )

    def log(self, **values):
        return RequestLog(
            **{
                "token_id": self.token_id,
                "operation": "video",
                "request_body": '{"prompt":"Synthetic task"}',
                "response_body": "x" * 2100,
                "status_code": 102,
                "duration": 0,
                **values,
            }
        )

    async def test_task_round_trip_updates_and_missing_rows(self):
        row_id = await self.db.create_task(self.task(scene_id="scene-1"))
        saved = await self.db.get_task("task-1")
        self.assertEqual(saved.id, row_id)
        self.assertEqual(saved.scene_id, "scene-1")
        self.assertIsNotNone(saved.created_at)
        self.assertIsNone(saved.result_urls)
        await self.db.update_task(
            "task-1",
            status="completed",
            progress=100,
            result_urls=["https://example.invalid/output.mp4"],
            completed_at="2026-09-26 12:00:00",
        )
        await self.db.update_task("task-1", status=None, result_urls=None)
        saved = await self.db.get_task("task-1")
        self.assertEqual(saved.status, "completed")
        self.assertEqual(saved.progress, 100)
        self.assertEqual(saved.result_urls, ["https://example.invalid/output.mp4"])
        self.assertEqual(saved.completed_at.isoformat(), "2026-09-26T12:00:00")
        await self.db.update_task("task-1", result_urls=[])
        self.assertEqual((await self.db.get_task("task-1")).result_urls, [])
        self.assertIsNone(await self.db.get_task("missing"))
        await self.db.update_task("missing", progress=10)

    async def test_logs_keep_payload_filter_order_and_null_update_contract(self):
        first_id = await self.db.add_request_log(self.log())
        second_id = await self.db.add_request_log(self.log(token_id=None, operation="image"))
        async with self.db._connect(write=True) as connection:
            await connection.execute(
                "UPDATE request_logs SET created_at = '2026-09-25 00:00:00' WHERE id = ?",
                (first_id,),
            )
            await connection.execute(
                "UPDATE request_logs SET created_at = '2026-09-26 00:00:00' WHERE id = ?",
                (second_id,),
            )
            await connection.commit()
        latest = await self.db.get_logs(limit=1)
        self.assertEqual([row["id"] for row in latest], [second_id])
        filtered = await self.db.get_logs(token_id=self.token_id)
        self.assertEqual([row["id"] for row in filtered], [first_id])
        self.assertNotIn("request_body", filtered[0])
        self.assertNotIn("response_body", filtered[0])
        self.assertEqual(filtered[0]["response_body_excerpt"], "x" * 2048)
        self.assertEqual(filtered[0]["token_email"], "account@example.invalid")
        self.assertEqual(filtered[0]["token_username"], "Account")
        payload = await self.db.get_logs(token_id=self.token_id, include_payload=True)
        self.assertEqual(payload[0]["response_body"], "x" * 2100)
        self.assertEqual(len(await self.db.get_logs(token_id=0)), 2)
        await self.db.update_request_log(
            first_id,
            token_id=None,
            response_body=None,
            status_text="done",
            status_code=200,
            progress=100,
            unknown_column="ignored",
        )
        detail = await self.db.get_log_detail(first_id)
        self.assertIsNone(detail["token_id"])
        self.assertIsNone(detail["response_body"])
        self.assertIsNone(detail["token_email"])
        self.assertEqual(detail["status_text"], "done")
        self.assertEqual(detail["progress"], 100)
        self.assertIsNotNone(detail["updated_at"])
        self.assertNotIn("response_body_excerpt", detail)
        await self.db.update_request_log(first_id, unknown_column="ignored")
        await self.db.update_request_log(first_id)
        self.assertEqual(await self.db.get_log_detail(first_id), detail)
        self.assertIsNone(await self.db.get_log_detail(9999))
        await self.db.clear_all_logs()
        self.assertEqual(await self.db.get_logs(), [])
        self.assertIsNotNone(await self.db.get_token(self.token_id))

    async def test_legacy_log_columns_still_have_read_fallbacks(self):
        async with self.db._connect(write=True) as connection:
            await connection.execute("DROP TABLE request_logs")
            await connection.execute(
                "CREATE TABLE request_logs (id INTEGER PRIMARY KEY, token_id INTEGER, "
                "operation TEXT, request_body TEXT, response_body TEXT, status_code INTEGER, "
                "duration FLOAT, created_at TIMESTAMP)"
            )
            await connection.execute(
                "INSERT INTO request_logs VALUES (1, ?, 'legacy', '{}', NULL, 200, 1.5, "
                "'2026-09-01 00:00:00')",
                (self.token_id,),
            )
            await connection.commit()
        listing = (await self.db.get_logs(include_payload=True))[0]
        detail = await self.db.get_log_detail(1)
        for row in (listing, detail):
            self.assertEqual(row["status_text"], "")
            self.assertEqual(row["progress"], 0)
            self.assertEqual(row["updated_at"], row["created_at"])
            self.assertEqual(row["token_email"], "account@example.invalid")
        self.assertEqual(listing["response_body_excerpt"], "")

    async def test_failed_statement_rolls_back_trigger_side_effects_and_releases_lock(self):
        async with self.db._connect(write=True) as connection:
            await connection.execute(
                "CREATE TRIGGER fail_task AFTER INSERT ON tasks "
                "WHEN NEW.task_id = 'fail' BEGIN "
                "INSERT INTO request_logs (operation, status_code, duration) "
                "VALUES ('trigger-side-effect', 500, 0); "
                "SELECT RAISE(FAIL, 'injected task failure'); END"
            )
            await connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected task failure"):
            await self.db.create_task(self.task("fail"))
        self.assertFalse(self.db._write_lock.locked())
        self.assertIsNone(await self.db.get_task("fail"))
        self.assertEqual(await self.db.get_logs(), [])
        successful = await self.db.create_task(self.task("recovered"))
        self.assertEqual((await self.db.get_task("recovered")).id, successful)

    async def test_foreign_key_and_update_errors_leave_previous_rows_unchanged(self):
        log_id = await self.db.add_request_log(self.log(status_text="original"))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.update_request_log(log_id, token_id=9999, status_text="changed")
        detail = await self.db.get_log_detail(log_id)
        self.assertEqual(detail["token_id"], self.token_id)
        self.assertEqual(detail["status_text"], "original")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.create_task(self.task(token_id=9999))
        self.assertIsNone(await self.db.get_task("task-1"))
        await self.db.create_task(self.task())
        with self.assertRaises(sqlite3.OperationalError):
            await self.db.update_task("task-1", nonexistent_column=1, progress=50)
        self.assertEqual((await self.db.get_task("task-1")).progress, 0)
        await self.db.update_task("task-1", progress=25)
        self.assertEqual((await self.db.get_task("task-1")).progress, 25)

    async def test_writes_share_the_existing_database_lock_and_reads_remain_available(self):
        async with self.db._write_lock:
            writes = [
                asyncio.create_task(self.db.create_task(self.task(f"parallel-{index}")))
                for index in range(3)
            ] + [asyncio.create_task(self.db.add_request_log(self.log())) for _ in range(3)]
            await asyncio.sleep(0)
            self.assertTrue(all(not write.done() for write in writes))
            self.assertEqual(await asyncio.wait_for(self.db.get_logs(), timeout=2), [])
        results = await asyncio.wait_for(asyncio.gather(*writes), timeout=5)
        self.assertEqual(len(set(results[:3])), 3)
        self.assertEqual(len(set(results[3:])), 3)
        self.assertEqual(len(await self.db.get_logs()), 3)
        self.assertFalse(self.db._write_lock.locked())


if __name__ == "__main__":
    unittest.main()
