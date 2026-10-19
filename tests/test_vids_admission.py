import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_vids_gateway import client as gateway_client

from src.services.vids.client import BEFORE_PAID_REQUEST, VidsClient
from src.services.vids.gateway import GatewayError, VidsGateway


class VidsClientAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch("src.services.vids.client.AsyncSession"):
            self.client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
        self.client.flags = {"espresso_api_key": "synthetic-key"}
        self.client.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
        self.client._headers = Mock(return_value={})
        self.client._send = AsyncMock(
            return_value=SimpleNamespace(status_code=200, json=lambda: ["received"])
        )

    async def request(self, admission):
        token = BEFORE_PAID_REQUEST.set(admission)
        try:
            return await self.client._request("POST", "/v1/genai/generate", [])
        finally:
            BEFORE_PAID_REQUEST.reset(token)

    async def test_concurrent_tasks_keep_their_own_admission(self):
        first, second = asyncio.Event(), asyncio.Event()
        checks = []

        async def submit(name, entered, other):
            async def admission():
                entered.set()
                await other.wait()
                self.assertIs(BEFORE_PAID_REQUEST.get(), admission)
                checks.append(name)

            return await self.request(admission)

        self.assertEqual(
            await asyncio.gather(submit("first", first, second), submit("second", second, first)),
            [["received"], ["received"]],
        )
        self.assertCountEqual(checks, ["first", "second"])
        self.assertEqual(self.client._send.await_count, 2)
        self.assertIsNone(BEFORE_PAID_REQUEST.get())

    async def test_admission_precedes_legacy_sync_guard_and_post(self):
        checks = []

        async def admission():
            await asyncio.sleep(0)
            checks.append("admission")

        self.client.before_generate = lambda: checks.append("legacy")

        async def send(*args, **kwargs):
            checks.append("post")
            return SimpleNamespace(status_code=200, json=lambda: ["received"])

        self.client._send.side_effect = send
        self.assertEqual(await self.request(admission), ["received"])
        self.assertEqual(checks, ["admission", "legacy", "post"])

    async def test_admission_or_legacy_rejection_sends_no_paid_post(self):
        async def denied():
            raise GatewayError(503, "account_disabled", "Synthetic disabled account")

        legacy = Mock()
        self.client.before_generate = legacy
        with self.assertRaises(GatewayError):
            await self.request(denied)
        legacy.assert_not_called()
        self.client._send.assert_not_awaited()

        admission = AsyncMock()
        legacy.side_effect = GatewayError(503, "session_persistence_failed", "Synthetic failure")
        with self.assertRaises(GatewayError):
            await self.request(admission)
        admission.assert_awaited_once()
        legacy.assert_called_once()
        self.client._send.assert_not_awaited()
        self.assertIsNone(BEFORE_PAID_REQUEST.get())

    async def test_cancelled_admission_sends_no_paid_post(self):
        entered = asyncio.Event()

        async def admission():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.request(admission))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client._send.assert_not_awaited()
        self.assertIsNone(BEFORE_PAID_REQUEST.get())


class VidsGatewayAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = gateway_client()
        self.gateway = VidsGateway(self.client, self.directory.name)
        await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.close()
        self.directory.cleanup()

    async def test_cancelled_terminal_admission_keeps_unknown_receipt_and_resets_context(self):
        entered = asyncio.Event()
        paid_post = AsyncMock()
        admissions = 0
        restored = []
        outer_guard = object()

        async def admission():
            nonlocal admissions
            admissions += 1
            if admissions == 2:
                entered.set()
                await asyncio.Event().wait()

        async def submit(*args, **kwargs):
            await BEFORE_PAID_REQUEST.get()()
            return await paid_post()

        self.client.generate_speech.side_effect = submit
        body = {"kind": "speech", "prompt": "Synthetic speech"}

        async def generate():
            token = BEFORE_PAID_REQUEST.set(outer_guard)
            try:
                return await self.gateway.generate(body, "cancelled-key", admission=admission)
            finally:
                restored.append(BEFORE_PAID_REQUEST.get() is outer_guard)
                BEFORE_PAID_REQUEST.reset(token)

        task = asyncio.create_task(generate())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(restored, [True])
        self.assertFalse(self.gateway.lock.locked())
        paid_post.assert_not_awaited()
        replay = await self.gateway.generate(body, "cancelled-key", admission=AsyncMock())
        self.assertEqual((replay["status"], replay["error"]), ("unknown", "acceptance_unknown"))
        self.client.generate_speech.assert_awaited_once()
        self.assertEqual(admissions, 2)

    async def test_accepted_response_survives_later_disabled_admission_and_replays(self):
        response = self.client.generate_speech.return_value
        disabled = False
        admissions = 0

        async def admission():
            nonlocal admissions
            admissions += 1
            if disabled:
                raise GatewayError(503, "account_disabled", "Synthetic disabled account")

        async def submit(*args, **kwargs):
            nonlocal disabled
            await BEFORE_PAID_REQUEST.get()()
            disabled = True
            return response

        self.client.generate_speech.side_effect = submit
        body = {"kind": "speech", "prompt": "Synthetic speech"}
        result = await self.gateway.generate(body, "accepted-key", admission=admission)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.gateway.get(result["id"])["response"], response)
        self.assertEqual(
            await self.gateway.generate(body, "accepted-key", admission=admission), result
        )
        self.assertEqual(admissions, 2)
        self.client.generate_speech.assert_awaited_once()
        self.assertIsNone(BEFORE_PAID_REQUEST.get())


if __name__ == "__main__":
    unittest.main()
