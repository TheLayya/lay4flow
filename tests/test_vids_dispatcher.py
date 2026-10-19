import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.vids.client import BEFORE_PAID_REQUEST, VidsClient, VidsTransportError
from src.services.vids.dispatcher import (
    VidsAccount,
    VidsDispatchError,
    VidsPoolDispatcher,
    VidsProviderDispatcher,
)
from src.services.vids.gateway import GatewayError, VidsGateway
from tests.test_vids_gateway import client


class VidsDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.tokens = {
            identifier: SimpleNamespace(
                id=identifier, is_active=True, image_enabled=True, video_enabled=True
            )
            for identifier in (1, 2)
        }
        self.db = SimpleNamespace(get_token=AsyncMock(side_effect=self.tokens.get))
        self.gateways = []
        self.first = await self.gateway_for(1)
        self.second = await self.gateway_for(2)
        self.dispatcher = self.new_dispatcher()
        self.body = {"kind": "speech", "prompt": "Hello"}

    async def asyncTearDown(self):
        for gateway in reversed(self.gateways):
            await gateway.close()
        self.directory.cleanup()

    async def gateway_for(self, identifier, *, root=None, identity=None):
        upstream = client()
        upstream.flags["docs-hue"] = identity or f"account-{identifier}"
        gateway = VidsGateway(upstream, root or self.root / str(identifier))
        self.gateways.append(gateway)
        await gateway.start()
        return gateway

    @staticmethod
    def binding(identifier, gateway):
        return VidsAccount(
            identifier, gateway, hashlib.sha256(gateway.identity.encode()).hexdigest()
        )

    def new_dispatcher(self):
        dispatcher = VidsProviderDispatcher(self.db)
        dispatcher.register(self.binding(1, self.first))
        dispatcher.register(self.binding(2, self.second))
        return dispatcher

    async def test_disabled_new_requests_are_blocked_and_completed_receipts_replay(self):
        result = await self.dispatcher.generate(1, self.body, "completed")
        self.assertEqual(result["status"], "completed")
        self.tokens[1].is_active = False
        replay = await self.dispatcher.generate(1, self.body, "completed")
        self.assertEqual(replay, result)
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.assertEqual(self.dispatcher.get(1, result["id"])["status"], "completed")
        path, mime = self.dispatcher.content(1, result["id"])
        self.assertEqual(mime, "audio/wav")
        self.assertTrue(path.read_bytes().startswith(b"RIFF"))
        with self.assertRaises(GatewayError) as caught:
            await self.dispatcher.generate(1, {**self.body, "prompt": "Changed"}, "completed")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(1, self.body, "new-disabled")
        self.assertEqual(caught.exception.code, "account_disabled")
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.assertEqual(BEFORE_PAID_REQUEST.get(), None)

    async def test_unknown_replay_and_account_affinity_survive_gateway_restart(self):
        self.first.client.generate_speech.side_effect = VidsTransportError("private")
        result = await self.dispatcher.generate(1, self.body, "unknown")
        self.assertEqual(result["status"], "unknown")
        await self.first.close()
        self.first = await self.gateway_for(1)
        restarted = self.new_dispatcher()
        self.tokens[1].is_active = False
        self.assertEqual(await restarted.generate(1, self.body, "unknown"), result)
        with self.assertRaises(VidsDispatchError) as caught:
            await restarted.generate(2, self.body, "unknown")
        self.assertEqual(caught.exception.code, "account_affinity_conflict")
        with self.assertRaises(GatewayError) as caught:
            await restarted.retry_download(1, result["id"])
        self.assertEqual(caught.exception.code, "no_received_result")
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_not_awaited()

    async def test_received_and_download_failed_stay_on_the_original_disabled_account(self):
        download = self.first.client.download_audio.side_effect
        self.first.client.download_audio.side_effect = VidsTransportError("private")
        failed = await self.dispatcher.generate(1, self.body, "download-failed")
        self.assertEqual(failed["status"], "download_failed")
        self.tokens[1].is_active = False
        self.assertEqual(await self.dispatcher.generate(1, self.body, "download-failed"), failed)
        self.first.client.download_audio.side_effect = download
        completed = await self.dispatcher.retry_download(1, failed["id"])
        self.assertEqual(completed["status"], "completed")
        received = self.first.get(failed["id"])
        received["status"] = "received"
        self.first._save(received)
        self.assertEqual(
            (await self.dispatcher.generate(1, self.body, "download-failed"))["status"], "received"
        )
        with self.assertRaises(VidsDispatchError):
            await self.dispatcher.generate(2, self.body, "download-failed")
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.second.client.generate_speech.assert_not_awaited()
        with self.assertRaises(GatewayError) as caught:
            self.dispatcher.content(2, failed["id"])
        self.assertEqual(caught.exception.code, "not_found")

    async def test_registration_and_identity_drift_reject_unsafe_bindings(self):
        for account in (
            self.binding(1, self.second),
            self.binding(3, self.first),
            VidsAccount(True, self.first, "a" * 64),
            VidsAccount(3, self.first, "not-a-hash"),
        ):
            with self.subTest(account=account.token_id), self.assertRaises(ValueError):
                self.dispatcher.register(account)
        duplicate_identity = await self.gateway_for(3, identity=self.first.identity)
        with self.assertRaises(ValueError):
            self.dispatcher.register(self.binding(3, duplicate_identity))
        self.second.root = self.first.root
        with self.assertRaises(ValueError):
            VidsProviderDispatcher(self.db).register(self.binding(2, self.second))
        self.second.root = self.root / "2"
        self.first.client.flags["docs-hue"] = "unexpected-account"
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(1, self.body, "identity-drift")
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.first.client.generate_speech.assert_not_awaited()
        with self.assertRaises(VidsDispatchError):
            self.dispatcher.get(1, "vids_" + "a" * 64)

    async def test_unavailable_receipt_keeps_owner_without_blocking_other_requests(self):
        saved = await self.dispatcher.generate(1, self.body, "offline-receipt")
        self.dispatcher.replace([self.binding(2, self.second)])
        self.dispatcher.unavailable_receipt_roots[1] = self.first.root
        pool = VidsPoolDispatcher(self.dispatcher)
        with self.assertRaises(VidsDispatchError) as caught:
            await pool.generate(0, self.body, "offline-receipt")
        self.assertEqual(caught.exception.code, "session_unavailable")
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(2, self.body, "offline-receipt")
        self.assertEqual(caught.exception.code, "account_affinity_conflict")
        self.second.client.generate_speech.assert_not_awaited()
        self.assertEqual(self.first.get(saved["id"])["status"], saved["status"])
        fresh = await pool.generate(0, self.body, "healthy-new-key")
        self.assertEqual(fresh["status"], "completed")
        self.second.client.generate_speech.assert_awaited_once()
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        for identifier in ("../vids_" + "a" * 64, "vids_" + "a" * 64 + ".json"):
            with self.assertRaises(VidsDispatchError):
                pool._owners(identifier)
        self.dispatcher.replace([])
        self.assertEqual(self.dispatcher.unavailable_receipt_roots, {})

    async def test_identity_drift_isolated_from_healthy_selection_and_receipt_reads(self):
        saved = await self.dispatcher.generate(1, self.body, "drifted-receipt")
        pool = VidsPoolDispatcher(self.dispatcher)
        self.first.client.flags["docs-hue"] = "unexpected-account"
        fresh = await pool.generate(0, self.body, "healthy-after-drift")
        self.assertEqual(fresh["status"], "completed")
        self.assertEqual(pool.get(0, fresh["id"])["id"], fresh["id"])
        self.assertEqual([record["id"] for record in pool.list(0)], [fresh["id"]])
        pool.models()
        with self.assertRaises(VidsDispatchError) as caught:
            await pool.generate(0, self.body, "drifted-receipt")
        self.assertEqual(caught.exception.code, "account_mismatch")
        with self.assertRaises(VidsDispatchError) as caught:
            pool.get(0, saved["id"])
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.second.client.generate_speech.assert_awaited_once()

    async def test_document_discovery_skips_failed_sibling_and_keeps_source_identity_checks(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        first_reference = self.dispatcher.document_reference(1, "first-shape")
        self.first.client.flags["docs-hue"] = "unexpected-account"
        self.first.document_images = AsyncMock()
        self.second.document_images = AsyncMock(
            return_value={"data": [{"shape_id": "second-shape", "scene_id": "second-scene"}]}
        )
        images = await pool.document_images(0)
        self.assertEqual(len(images["data"]), 1)
        self.assertEqual(
            self.dispatcher.resolve_document_reference(images["data"][0]["source_id"]),
            (2, "second-shape"),
        )
        with self.assertRaises(VidsDispatchError) as caught:
            self.dispatcher.resolve_document_reference(first_reference)
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.first.document_images.assert_not_awaited()
        self.second.document_images.assert_awaited_once_with(False)

    async def test_shared_database_switches_and_invalid_input_do_not_submit(self):
        for body, change in (
            ({"kind": "image", "prompt": "x"}, {"image_enabled": False}),
            ({"kind": "video", "prompt": "x"}, {"video_enabled": False}),
            (self.body, {"is_active": False}),
        ):
            for field, value in change.items():
                setattr(self.tokens[1], field, value)
            with self.assertRaises(VidsDispatchError) as caught:
                await self.dispatcher.generate(1, body, "disabled-" + body["kind"])
            self.assertEqual(caught.exception.code, "account_disabled")
            for field in change:
                setattr(self.tokens[1], field, True)
        for body in ({"kind": "music", "prompt": "x"}, {"kind": []}):
            with self.assertRaises(VidsDispatchError) as caught:
                await self.dispatcher.generate(1, body, "invalid")
            self.assertEqual(caught.exception.code, "invalid_kind")
        for token_id, key, code in (
            (True, "key", "account_not_found"),
            (1, "", "idempotency_required"),
        ):
            with self.assertRaises(VidsDispatchError) as caught:
                await self.dispatcher.generate(token_id, self.body, key)
            self.assertEqual(caught.exception.code, code)
        self.db.get_token.side_effect = OSError("private database path")
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(1, self.body, "database-failure")
        self.assertEqual(caught.exception.code, "account_lookup_failed")
        self.assertNotIn("private", str(caught.exception))
        self.first.client.generate_speech.assert_not_awaited()

    async def test_concurrent_same_key_cannot_switch_or_submit_twice(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused_lookup(identifier):
            entered.set()
            await release.wait()
            return self.tokens.get(identifier)

        self.db.get_token.side_effect = paused_lookup
        pending = asyncio.create_task(self.dispatcher.generate(1, self.body, "concurrent"))
        await entered.wait()
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(2, self.body, "concurrent")
        self.assertEqual(caught.exception.code, "account_affinity_conflict")
        with self.assertRaises(GatewayError) as caught:
            await self.dispatcher.generate(1, self.body, "concurrent")
        self.assertEqual(caught.exception.code, "busy")
        release.set()
        self.assertEqual((await pending)["status"], "completed")
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.second.client.generate_speech.assert_not_awaited()

    async def test_final_admission_after_settings_blocks_paid_transport_and_resets_guard(self):
        upstream = self.first.client
        upstream.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
        upstream._headers = lambda: {}
        upstream._send = AsyncMock()
        upstream._request = VidsClient._request.__get__(upstream)
        upstream.flags["espresso_api_key"] = "synthetic-key"
        upstream.text_request = lambda text: [None, None, [None] * 29, None, None, None]

        async def settings():
            await asyncio.sleep(0)
            self.tokens[1].is_active = False

        upstream.settings = settings
        upstream.generate_speech = VidsClient.generate_speech.__get__(upstream)
        result = await self.dispatcher.generate(1, self.body, "settings-disabled")
        self.assertEqual((result["status"], result["error"]), ("failed", "account_disabled"))
        upstream._send.assert_not_awaited()
        self.assertIsNone(BEFORE_PAID_REQUEST.get())
        self.assertEqual(self.db.get_token.await_count, 2)

    async def test_disable_after_paid_response_keeps_the_receipt_and_cancel_resets_guard(self):
        upstream = self.first.client
        response = upstream.generate_speech.return_value

        async def paid_response(text, *, voice):
            self.tokens[1].is_active = False
            return response

        upstream.generate_speech.side_effect = paid_response
        completed = await self.dispatcher.generate(1, self.body, "accepted")
        self.assertEqual(completed["status"], "completed")
        self.assertIn("response", self.first.get(completed["id"]))
        self.assertIsNone(BEFORE_PAID_REQUEST.get())
        self.tokens[1].is_active = True
        upstream.generate_speech.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.dispatcher.generate(1, self.body, "cancelled")
        self.assertIsNone(BEFORE_PAID_REQUEST.get())
        identifier = "vids_" + hashlib.sha256(b"cancelled").hexdigest()
        self.assertEqual(self.first.get(identifier)["status"], "unknown")
        self.assertNotIn("cancelled", json.dumps(self.dispatcher._claims))


if __name__ == "__main__":
    unittest.main()
