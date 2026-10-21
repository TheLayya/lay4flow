"""Host maintenance gate covers streaming writes and releases counters on error."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from src.core.update_guard import UpdateActivity, UpdateGuardMiddleware


class UpdateGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_new_writes_while_reads_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "maintenance"
            marker.touch()
            activity = UpdateActivity(marker)
            calls, messages = [], []

            async def app(scope, receive, send):
                calls.append(scope["method"])

            async def send(value):
                messages.append(value)

            guard = UpdateGuardMiddleware(app, activity)
            await guard({"type": "http", "method": "POST"}, None, send)
            self.assertEqual(messages[0]["status"], 503)
            self.assertIn((b"retry-after", b"30"), messages[0]["headers"])
            await guard({"type": "http", "method": "GET"}, None, send)
            self.assertEqual(calls, ["GET"])
            self.assertEqual(activity.active_requests, 0)

    async def test_active_stream_is_counted_until_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            activity = UpdateActivity(Path(directory) / "maintenance")
            started, finish = asyncio.Event(), asyncio.Event()

            async def app(scope, receive, send):
                started.set()
                await finish.wait()

            guard = UpdateGuardMiddleware(app, activity)
            task = asyncio.create_task(guard({"type": "http", "method": "POST"}, None, None))
            await started.wait()
            self.assertEqual(activity.active_requests, 1)
            finish.set()
            await task
            self.assertEqual(activity.active_requests, 0)

    async def test_cancelled_write_releases_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            activity = UpdateActivity(Path(directory) / "maintenance")

            async def app(scope, receive, send):
                raise asyncio.CancelledError

            guard = UpdateGuardMiddleware(app, activity)
            with self.assertRaises(asyncio.CancelledError):
                await guard({"type": "http", "method": "PUT"}, None, None)
            self.assertEqual(activity.active_requests, 0)
