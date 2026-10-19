"""Real SQLite coverage for account ordering, cascade transactions, and daily usage."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.core.database import Database
from src.core.models import Project, RequestLog, Task, Token


class DatabaseAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(str(Path(self.directory.name) / "accounts.db"))
        await self.database.init_db()

    async def add(self, name="one", **values):
        return await self.database.add_token(
            Token(
                **{
                    "st": f"synthetic-{name}",
                    "email": f"{name}@example.invalid",
                    "name": name,
                    **values,
                }
            )
        )

    async def test_lookup_order_duplicate_email_and_manual_disable_survive_partial_update(self):
        first = await self.add("first", email="Same@example.invalid", credits=7)
        second = await self.add("second", email="same@example.invalid", credits=11)
        third = await self.add("third", credits=20)
        await self.database.update_token(first, created_at="2026-09-01", last_used_at="2026-09-03")
        await self.database.update_token(second, created_at="2026-09-02", last_used_at="2026-09-01")
        await self.database.update_token(
            third, created_at="2026-09-03", is_active=False, ban_reason="manual"
        )
        await self.database.update_token(third, at="updated-at", remark=None, credits=0)
        self.assertEqual((await self.database.get_token_by_email("SAME@example.invalid")).id, first)
        self.assertEqual((await self.database.get_token_by_st("synthetic-second")).id, second)
        self.assertEqual(
            [row.id for row in await self.database.get_all_tokens()], [third, second, first]
        )
        self.assertEqual(
            [row.id for row in await self.database.get_active_tokens()], [second, first]
        )
        token = await self.database.get_token(third)
        self.assertFalse(token.is_active)
        self.assertEqual(token.ban_reason, "manual")
        self.assertEqual(token.credits, 0)
        self.assertIsNone(token.remark)
        self.assertEqual(
            await self.database.get_system_info_stats(),
            {
                "total_tokens": 3,
                "active_tokens": 2,
                "total_credits": 18,
            },
        )
        self.assertIsNone(await self.database.get_token(9999))
        self.assertIsNone(await self.database.get_token_by_email("missing@example.invalid"))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.add("first")
        self.assertEqual(len(await self.database.get_all_tokens()), 3)

    async def test_projects_foreign_keys_and_account_delete_keep_logs_but_remove_children(self):
        owner = await self.add()
        other = await self.add("other")
        project_id = await self.database.add_project(
            Project(project_id="project", token_id=owner, project_name="One")
        )
        await self.database.add_project(
            Project(project_id="later", token_id=owner, project_name="Two")
        )
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                "UPDATE projects SET created_at = '2026-09-01' WHERE project_id = 'project'"
            )
            await connection.execute(
                "UPDATE projects SET created_at = '2026-09-02' WHERE project_id = 'later'"
            )
            await connection.commit()
        self.assertEqual((await self.database.get_project_by_id("project")).id, project_id)
        self.assertEqual(
            [p.project_id for p in await self.database.get_projects_by_token(owner)],
            ["later", "project"],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.database.add_project(
                Project(project_id="bad", token_id=9999, project_name="Bad")
            )
        await self.database.delete_project("later")
        self.assertIsNone(await self.database.get_project_by_id("later"))
        await self.database.create_task(
            Task(
                task_id="task",
                token_id=owner,
                model="model",
                prompt="synthetic",
                status="processing",
            )
        )
        log_id = await self.database.add_request_log(
            RequestLog(token_id=owner, operation="video", status_code=200, duration=1)
        )
        await self.database.delete_token(owner)
        self.assertIsNone(await self.database.get_token(owner))
        self.assertIsNone(await self.database.get_task("task"))
        self.assertIsNone(await self.database.get_token_stats(owner))
        self.assertEqual(await self.database.get_projects_by_token(owner), [])
        self.assertIsNone((await self.database.get_log_detail(log_id))["token_id"])
        self.assertIsNotNone(await self.database.get_token(other))

    async def test_delete_failure_rolls_back_prior_child_updates_and_keeps_lock_usable(self):
        owner = await self.add()
        await self.database.add_project(
            Project(project_id="project", token_id=owner, project_name="One")
        )
        log_id = await self.database.add_request_log(
            RequestLog(token_id=owner, operation="image", status_code=200, duration=1)
        )
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                "CREATE TRIGGER reject_token_delete BEFORE DELETE ON tokens BEGIN SELECT RAISE(FAIL, 'reject-delete'); END"
            )
            await connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "reject-delete"):
            await self.database.delete_token(owner)
        self.assertIsNotNone(await self.database.get_token(owner))
        self.assertIsNotNone(await self.database.get_project_by_id("project"))
        self.assertIsNotNone(await self.database.get_token_stats(owner))
        self.assertEqual((await self.database.get_log_detail(log_id))["token_id"], owner)
        self.assertFalse(self.database._write_lock.locked())
        await self.database.update_token(owner, is_active=False, ban_reason="manual")
        self.assertFalse((await self.database.get_token(owner)).is_active)

    async def test_add_keeps_legacy_separate_token_and_statistics_commits(self):
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                "CREATE TRIGGER reject_stats BEFORE INSERT ON token_stats BEGIN SELECT RAISE(FAIL, 'reject-stats'); END"
            )
            await connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "reject-stats"):
            await self.add()
        token = await self.database.get_token_by_st("synthetic-one")
        self.assertIsNotNone(token)
        self.assertIsNone(await self.database.get_token_stats(token.id))
        self.assertFalse(self.database._write_lock.locked())

    async def test_dynamic_date_concurrent_counters_reset_only_daily_values(self):
        owner = await self.add()
        date = Mock(return_value="2026-09-26")
        with patch.object(self.database, "_current_stats_date", date):
            await asyncio.gather(
                *(self.database.increment_image_count(owner) for _ in range(8)),
                *(self.database.increment_video_count(owner) for _ in range(5)),
                *(self.database.increment_error_count(owner) for _ in range(3)),
            )
            stats = await self.database.get_token_stats(owner)
            self.assertEqual((stats.image_count, stats.video_count, stats.error_count), (8, 5, 3))
            self.assertEqual(
                (stats.today_image_count, stats.today_video_count, stats.today_error_count),
                (8, 5, 3),
            )
            self.assertEqual(stats.consecutive_error_count, 3)
            date.return_value = "2026-09-27"
            dashboard = await self.database.get_dashboard_stats()
            self.assertEqual(
                (dashboard["today_images"], dashboard["today_videos"], dashboard["today_errors"]),
                (0, 0, 0),
            )
            self.assertEqual(
                (await self.database.get_all_tokens_with_stats())[0]["today_image_count"], 0
            )
            await self.database.increment_video_count(owner)
            await self.database.reset_error_count(owner)
            stats = await self.database.get_token_stats(owner)
            self.assertEqual((stats.image_count, stats.video_count, stats.error_count), (8, 6, 3))
            self.assertEqual(
                (stats.today_image_count, stats.today_video_count, stats.today_error_count),
                (0, 1, 0),
            )
            self.assertEqual(stats.consecutive_error_count, 0)
            date.return_value = "2026-09-28"
            await self.database.increment_image_count(owner)
            date.return_value = "2026-09-29"
            await self.database.increment_error_count(owner)
            stats = await self.database.get_token_stats(owner)
            self.assertEqual(
                (stats.today_image_count, stats.today_video_count, stats.today_error_count),
                (0, 0, 1),
            )
            self.assertEqual(stats.today_date, "2026-09-29")
        self.assertFalse(self.database._write_lock.locked())

    async def test_legacy_stat_dispatch_and_dynamic_connection_hooks_remain_callable(self):
        with patch.object(self.database, "increment_image_count", new_callable=AsyncMock) as image:
            await self.database.increment_token_stats(1, "image")
            await self.database.increment_token_stats(1, "unknown")
        image.assert_awaited_once_with(1)
        with patch.object(self.database, "_connect", wraps=self.database._connect) as connect:
            owner = await self.add()
            await self.database.get_token(owner)
        self.assertEqual([call.kwargs for call in connect.call_args_list], [{"write": True}, {}])


if __name__ == "__main__":
    unittest.main()
