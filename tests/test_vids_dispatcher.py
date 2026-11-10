import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.services.vids.client import (
    BEFORE_PAID_REQUEST,
    VidsClient,
    VidsProtocolError,
    VidsTransportError,
)
from src.services.vids.dispatcher import (
    POOL_SUCCESSOR_FIELD,
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

        async def switches(token_ids):
            return {
                token_id: {
                    "token_id": token_id,
                    "email": getattr(token, "email", ""),
                    "enabled": token.is_active,
                    "image_enabled": token.image_enabled,
                    "video_enabled": token.video_enabled,
                }
                for token_id in token_ids
                if (token := self.tokens.get(token_id)) is not None
            }

        dispatcher.account_switches = AsyncMock(side_effect=switches)
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

    async def test_real_speech_wire_parses_language_and_cross_product_reasons_safely(self):
        upstream = self.first.client
        upstream.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
        upstream._headers = lambda: {}
        upstream.flags["espresso_api_key"] = "synthetic-key"
        upstream.doc_id = "doc"
        upstream.session_id = "goog_synthetic"
        upstream.settings_response = [None, [False]]
        upstream.settings = VidsClient.settings.__get__(upstream)
        upstream.text_request = VidsClient.text_request.__get__(upstream)
        upstream.generate_speech = VidsClient.generate_speech.__get__(upstream)
        upstream._request = VidsClient._request.__get__(upstream)
        body = [
            3,
            "private message https://private.invalid token=secret",
            [
                [
                    "type.googleapis.com/google.rpc.ErrorInfo",
                    ["LANGUAGE_NOT_SUPPORTED", "private-domain", {"key": "private"}],
                ]
            ],
        ]
        upstream._send = AsyncMock(
            return_value=SimpleNamespace(status_code=400, content=b"[]", json=lambda: body)
        )
        result = await self.dispatcher.generate(1, self.body, "wire-language")
        self.assertEqual((result["status"], result["error"]), ("failed", "upstream_http_400"))
        self.assertEqual(
            result["upstream_error"],
            {"status": "INVALID_ARGUMENT", "reason": "LANGUAGE_NOT_SUPPORTED"},
        )
        self.assertNotIn("private", json.dumps(result))
        call = upstream._send.await_args
        self.assertEqual(call.args[0], "request")
        self.assertEqual(call.args[1], "POST")
        self.assertTrue(call.args[2].endswith("/v1/genai/generate"))
        wire = json.loads(call.kwargs["data"])
        self.assertEqual(wire[0], 254)
        self.assertEqual(wire[2][11], [16, 0])
        self.assertEqual(wire[4][25], ["Puck"])
        self.assertEqual(upstream._send.await_count, 1)

        upstream._send = AsyncMock(
            return_value=SimpleNamespace(
                status_code=403,
                content=b"[]",
                json=lambda: [
                    7,
                    "private permission message",
                    [
                        [
                            "type.googleapis.com/google.rpc.ErrorInfo",
                            ["CROSS_PRODUCT_SHARING_DISABLED", "private-domain", {}],
                        ]
                    ],
                ],
            )
        )
        result = await self.dispatcher.generate(1, self.body, "wire-entitlement")
        self.assertEqual((result["status"], result["error"]), ("failed", "upstream_http_403"))
        self.assertEqual(
            result["upstream_error"],
            {"status": "PERMISSION_DENIED", "reason": "CROSS_PRODUCT_SHARING_DISABLED"},
        )
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(upstream._send.await_count, 1)

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

    @staticmethod
    def _base_id(key):
        return "vids_" + hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def _child_id(base_id, attempt):
        return VidsPoolDispatcher._receipt_id(VidsPoolDispatcher._successor_key(base_id, attempt))

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_pool_falls_over_after_confirmed_403_and_replays_linked_chain(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        reason = {"status": "PERMISSION_DENIED", "reason": "CROSS_PRODUCT_SHARING_DISABLED"}
        self.first.client.generate_speech.side_effect = VidsProtocolError(
            "generate", 403, upstream_status=reason["status"], upstream_reason=reason["reason"]
        )
        key = "pool-fallback"
        result = await pool.generate(0, self.body, key)
        base_id = self._base_id(key)
        child_id = self._child_id(base_id, 2)
        self.assertEqual((result["id"], result["status"]), (child_id, "completed"))
        base = self.first.get(base_id)
        self.assertEqual(base["status"], "failed")
        self.assertEqual(
            base[POOL_SUCCESSOR_FIELD],
            {
                "attempt": 2,
                "base_receipt_id": base_id,
                "token_id": 2,
                "idempotency_key": VidsPoolDispatcher._successor_key(base_id, 2),
                "receipt_id": child_id,
            },
        )
        self.assertNotIn(POOL_SUCCESSOR_FIELD, result)
        self.assertEqual(self.second.get(child_id)["status"], "completed")
        self.assertEqual(pool.get(0, base_id)["id"], base_id)
        self.assertEqual(pool.latest_receipt(base_id)["id"], child_id)
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.assertEqual(self.second.client.generate_speech.await_count, 1)

        self.assertEqual(await pool.generate(0, self.body, key), result)
        recreated = VidsPoolDispatcher(self.new_dispatcher())
        self.assertEqual(await recreated.generate(0, self.body, key), result)
        with self.assertRaises(VidsDispatchError) as caught:
            await pool.generate(0, {**self.body, "prompt": "Changed"}, key)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.assertEqual(self.second.client.generate_speech.await_count, 1)

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_pool_serializes_same_key_and_replays_final_result_once(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        entered, release = asyncio.Event(), asyncio.Event()

        async def denied(text, *, voice):
            entered.set()
            await release.wait()
            raise VidsProtocolError("generate", 403)

        self.first.client.generate_speech.side_effect = denied
        key = "pool-serialized"
        first = asyncio.create_task(pool.generate(0, self.body, key))
        await entered.wait()
        second = asyncio.create_task(pool.generate(0, self.body, key))
        await asyncio.sleep(0)
        release.set()
        original, duplicate = await asyncio.gather(first, second)
        self.assertEqual(original, duplicate)
        self.assertEqual(original["status"], "completed")
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.assertEqual(self.second.client.generate_speech.await_count, 1)

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_pool_allows_different_keys_to_use_accounts_concurrently(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        first_response = self.first.client.generate_speech.return_value
        second_response = self.second.client.generate_speech.return_value
        entered = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def first_call(text, *, voice):
            entered[0].set()
            await release.wait()
            return first_response

        async def second_call(text, *, voice):
            entered[1].set()
            await release.wait()
            return second_response

        self.first.client.generate_speech.side_effect = first_call
        self.second.client.generate_speech.side_effect = second_call
        first = asyncio.create_task(pool.generate(0, self.body, "pool-parallel-a"))
        await entered[0].wait()
        second = asyncio.create_task(pool.generate(0, self.body, "pool-parallel-b"))
        await entered[1].wait()
        release.set()
        original, parallel = await asyncio.gather(first, second)
        self.assertEqual(original["status"], "completed")
        self.assertEqual(parallel["status"], "completed")
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.assertEqual(self.second.client.generate_speech.await_count, 1)

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_pool_stops_on_language_unknown_and_download_failure(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        failures = (
            (
                "pool-language-403",
                VidsProtocolError(
                    "generate",
                    403,
                    upstream_status="PERMISSION_DENIED",
                    upstream_reason="LANGUAGE_NOT_SUPPORTED",
                ),
                "failed",
            ),
            (
                "pool-language-400",
                VidsProtocolError(
                    "generate",
                    400,
                    upstream_status="INVALID_ARGUMENT",
                    upstream_reason="LANGUAGE_NOT_SUPPORTED",
                ),
                "failed",
            ),
            ("pool-unknown", VidsTransportError("private"), "unknown"),
        )
        for key, failure, status in failures:
            with self.subTest(key=key):
                self.dispatcher._pool_last_selected.clear()
                self.second.client.generate_speech.reset_mock()
                self.first.client.generate_speech.side_effect = failure
                result = await pool.generate(0, self.body, key)
                self.assertEqual(result["status"], status)
                base = self.first.get(self._base_id(key))
                self.assertIsNone(base.get(POOL_SUCCESSOR_FIELD))
                self.second.client.generate_speech.assert_not_awaited()

        self.dispatcher._pool_last_selected.clear()
        self.second.client.generate_speech.reset_mock()
        self.first.client.generate_speech.side_effect = None
        self.first.client.download_audio.side_effect = VidsTransportError("private")
        result = await pool.generate(0, self.body, "pool-download-failed")
        self.assertEqual(result["status"], "download_failed")
        self.assertIsNone(
            self.first.get(self._base_id("pool-download-failed")).get(POOL_SUCCESSOR_FIELD)
        )
        self.second.client.generate_speech.assert_not_awaited()

        self.first.client.generate_speech.side_effect = VidsProtocolError("generate", 403)
        legacy = await self.dispatcher.generate(1, self.body, "pool-legacy-terminal")
        self.assertEqual(legacy["status"], "failed")
        self.assertIsNone(
            self.first.get(self._base_id("pool-legacy-terminal")).get(POOL_SUCCESSOR_FIELD)
        )
        self.first.client.generate_speech.reset_mock()
        self.assertEqual(await pool.generate(0, self.body, "pool-legacy-terminal"), legacy)
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_not_awaited()

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_pool_exhausts_accounts_and_attempt_budget_with_last_real_failure(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        denied = VidsProtocolError("generate", 403)
        self.first.client.generate_speech.side_effect = denied
        self.second.client.generate_speech.side_effect = denied
        key = "pool-all-denied"
        result = await pool.generate(0, self.body, key)
        base_id = self._base_id(key)
        child_id = self._child_id(base_id, 2)
        self.assertEqual((result["id"], result["status"]), (child_id, "failed"))
        self.assertEqual(self.first.get(base_id)["status"], "failed")
        self.assertIsNone(self.second.get(child_id).get(POOL_SUCCESSOR_FIELD))
        self.assertEqual(pool.latest_receipt(base_id)["id"], child_id)
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.assertEqual(self.second.client.generate_speech.await_count, 1)

        self.first.client.generate_speech.reset_mock()
        self.second.client.generate_speech.reset_mock()
        self.dispatcher._pool_last_selected.clear()
        with patch.dict(
            config._config,
            {"flow": {**config._config.get("flow", {}), "max_retries": 1}},
        ):
            budget = await pool.generate(0, self.body, "pool-budget-one")
        self.assertEqual(
            (budget["id"], budget["status"]),
            (self._base_id("pool-budget-one"), "failed"),
        )
        self.assertIsNone(
            self.first.get(self._base_id("pool-budget-one")).get(POOL_SUCCESSOR_FIELD)
        )
        self.assertEqual(self.first.client.generate_speech.await_count, 1)
        self.second.client.generate_speech.assert_not_awaited()

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_latest_receipt_is_read_only_and_resumes_fixed_successor_after_crash(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        self.first.client.generate_speech.side_effect = VidsProtocolError("generate", 403)
        key = "pool-resume"
        completed = await pool.generate(0, self.body, key)
        base_id = self._base_id(key)
        self.assertEqual(completed["status"], "completed")
        self.second._path(completed["id"]).unlink()
        first_calls = self.first.client.generate_speech.await_count
        second_calls = self.second.client.generate_speech.await_count

        parent = pool.latest_receipt(base_id)
        self.assertEqual((parent["id"], parent["status"]), (base_id, "failed"))
        self.assertEqual(self.first.client.generate_speech.await_count, first_calls)
        self.assertEqual(self.second.client.generate_speech.await_count, second_calls)

        resumed = await pool.generate(0, self.body, key)
        self.assertEqual((resumed["id"], resumed["status"]), (completed["id"], "completed"))
        self.assertEqual(self.first.client.generate_speech.await_count, first_calls)
        self.assertEqual(self.second.client.generate_speech.await_count, second_calls + 1)
        self.assertEqual(pool.latest_receipt(base_id)["id"], completed["id"])
        self.assertEqual(pool.latest_receipt(completed["id"])["id"], completed["id"])

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_latest_receipt_follows_intermediate_child_and_checks_identity(self):
        third = await self.gateway_for(3)
        self.tokens[3] = SimpleNamespace(
            id=3, is_active=True, image_enabled=True, video_enabled=True
        )
        self.dispatcher.register(self.binding(3, third))
        denied = VidsProtocolError("generate", 403)
        self.first.client.generate_speech.side_effect = denied
        self.second.client.generate_speech.side_effect = denied
        third.client.generate_speech.side_effect = denied
        pool = VidsPoolDispatcher(self.dispatcher)
        key = "pool-three-denied"
        result = await pool.generate(0, self.body, key)
        base_id = self._base_id(key)
        child2 = self._child_id(base_id, 2)
        child3 = self._child_id(base_id, 3)
        self.assertEqual((result["id"], result["status"]), (child3, "failed"))
        self.assertEqual(pool.get(0, child2)["id"], child2)
        self.assertEqual(pool.latest_receipt(child2)["id"], child3)
        self.first.client.flags["docs-hue"] = "unexpected-account"
        with self.assertRaises(VidsDispatchError) as caught:
            pool.latest_receipt(base_id)
        self.assertEqual(caught.exception.code, "account_mismatch")
        with self.assertRaises(VidsDispatchError) as caught:
            pool.latest_receipt(child2)
        self.assertEqual(caught.exception.code, "account_mismatch")

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_source_bound_and_explicit_routes_never_fail_over(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        denied = VidsProtocolError("generate", 403)
        self.first.client.generate_speech.side_effect = denied
        explicit = await self.dispatcher.generate(1, self.body, "pool-explicit")
        self.assertEqual(explicit["status"], "failed")
        self.assertIsNone(self.first.get(self._base_id("pool-explicit")).get(POOL_SUCCESSOR_FIELD))
        self.second.client.generate_speech.assert_not_awaited()

        source_id = "vids_" + hashlib.sha256(b"pool-source-video").hexdigest()
        self.first._save(
            {
                "id": source_id,
                "kind": "video",
                "status": "completed",
                "created_at": 100,
                "error": None,
                "suffix": ".mp4",
                "mime_type": "video/mp4",
                "resource": {
                    "url": "https://example.invalid/source",
                    "width": 1280,
                    "height": 720,
                    "duration": 3,
                },
            }
        )
        self.first.client.submit_video = AsyncMock(side_effect=denied)
        self.second.client.submit_video = AsyncMock()
        body = {"kind": "video", "prompt": "Edit it", "mode": "edit", "source_id": source_id}
        result = await pool.generate(0, body, "pool-source-fixed")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(
            self.first.get(self._base_id("pool-source-fixed")).get(POOL_SUCCESSOR_FIELD)
        )
        self.first.client.submit_video.assert_awaited_once()
        self.second.client.submit_video.assert_not_awaited()

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_latest_receipt_rejects_invalid_successor_link_without_submission(self):
        pool = VidsPoolDispatcher(self.dispatcher)
        self.first.client.generate_speech.side_effect = VidsProtocolError("generate", 403)
        key = "pool-bad-link"
        result = await pool.generate(0, self.body, key)
        self.assertEqual(result["status"], "completed")
        base_id = self._base_id(key)
        record = self.first.get(base_id)
        link = dict(record[POOL_SUCCESSOR_FIELD])
        link["receipt_id"] = self._child_id(base_id, 3)
        record[POOL_SUCCESSOR_FIELD] = link
        self.first._save(record)
        first_calls = self.first.client.generate_speech.await_count
        second_calls = self.second.client.generate_speech.await_count
        with self.assertRaises(VidsDispatchError) as caught:
            pool.latest_receipt(base_id)
        self.assertEqual(caught.exception.code, "receipt_chain_invalid")
        with self.assertRaises(VidsDispatchError) as caught:
            await pool.generate(0, self.body, key)
        self.assertEqual(caught.exception.code, "receipt_chain_invalid")
        self.assertEqual(self.first.client.generate_speech.await_count, first_calls)
        self.assertEqual(self.second.client.generate_speech.await_count, second_calls)


if __name__ == "__main__":
    unittest.main()
