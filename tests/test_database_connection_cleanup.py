"""Cancellation during SQLite startup must not strand a worker thread."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from src.core.database import Database


class DatabaseConnectionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_connection_open_drains_and_closes_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "synthetic.db"))
            connection = aiosqlite.connect(database.db_path)
            connector = connection._connector
            started, release = threading.Event(), threading.Event()

            def delayed_connect():
                started.set()
                release.wait(timeout=5)
                return connector()

            connection._connector = delayed_connect

            async def use_connection():
                async with database._connect(write=True):
                    self.fail("Cancelled connection must not enter the body")

            with patch("src.core.database.aiosqlite.connect", return_value=connection):
                task = asyncio.create_task(use_connection())
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=5)
                finally:
                    release.set()
                    await asyncio.to_thread(connection.join, 5)
            self.assertFalse(connection.is_alive())
            self.assertFalse(database._write_lock.locked())
            async with database._connect() as db:
                self.assertEqual((await (await db.execute("SELECT 1")).fetchone())[0], 1)


if __name__ == "__main__":
    unittest.main()
