"""New generation authentication checks never retry or move paid requests."""

import asyncio
import hashlib
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from src.api.admin_routes.vids import create_vids_admin_router
from src.services.vids.client import VidsProtocolError, VidsTransportError
from src.services.vids.dispatcher import VidsPoolDispatcher
from src.services.vids.gateway import GatewayError
from tests import test_vids_dispatcher as dispatcher_tests


class VidsGenerationPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup = dispatcher_tests.VidsDispatcherTests()
        await self.setup.asyncSetUp()
        self.dispatcher = self.setup.dispatcher
        self.first, self.second = self.setup.first, self.setup.second
        self.body = self.setup.body

        async def repair(token_id):
            self.dispatcher._accounts[token_id].gateway.client.initialization_valid = True

        self.repair = AsyncMock(side_effect=repair)
        self.dispatcher.repair_session = self.repair
        self.first.client.initialization_valid = self.second.client.initialization_valid = True
        self.first.client.quota.reset_mock()
        self.second.client.quota.reset_mock()
        self.pool = VidsPoolDispatcher(self.dispatcher)

    async def asyncTearDown(self):
        await self.setup.asyncTearDown()

    async def test_new_request_auth_failure_repairs_original_account_then_submits_once(self):
        original = self.first.client.quota.return_value
        self.first.client.quota.side_effect = [VidsProtocolError("private quota", 401), original]
        result = await self.pool.generate(0, self.body, "preflight-repair")
        self.assertEqual(result["status"], "completed")
        self.repair.assert_awaited_once_with(1)
        self.first.client.generate_speech.assert_awaited_once()
        self.second.client.generate_speech.assert_not_awaited()
        self.assertEqual(self.first.client.quota.await_count, 2)

    async def test_failed_repair_has_no_receipt_or_paid_work_and_claim_cannot_switch(self):
        self.first.client.quota.side_effect = VidsProtocolError("private quota", 401)
        self.repair.side_effect = GatewayError(503, "repair_failed", "Synthetic repair failure")
        for _ in range(2):
            with self.assertRaises(GatewayError):
                await self.pool.generate(0, self.body, "preflight-failed")
        generation = "vids_" + hashlib.sha256(b"preflight-failed").hexdigest()
        self.assertFalse(self.first._path(generation).exists())
        with self.assertRaises(GatewayError) as caught:
            await self.dispatcher.generate(2, self.body, "preflight-failed")
        self.assertEqual(caught.exception.code, "account_affinity_conflict")
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_not_awaited()

    async def test_completed_and_unknown_receipts_replay_with_invalid_session_without_probe_or_repair(
        self,
    ):
        completed = await self.dispatcher.generate(1, self.body, "preflight-completed")
        self.first.client.generate_speech.side_effect = VidsTransportError("private")
        unknown = await self.dispatcher.generate(1, self.body, "preflight-unknown")
        self.first.client.initialization_valid = False
        self.first.client.quota.reset_mock()
        self.repair.reset_mock()
        self.assertEqual(await self.pool.generate(0, self.body, "preflight-completed"), completed)
        self.assertEqual(await self.pool.generate(0, self.body, "preflight-unknown"), unknown)
        self.first.client.quota.assert_not_awaited()
        self.repair.assert_not_awaited()
        self.assertEqual(self.first.client.generate_speech.await_count, 2)
        with self.assertRaises(GatewayError) as caught:
            await self.pool.generate(0, {**self.body, "prompt": "Changed"}, "preflight-unknown")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def test_parallel_same_key_checks_receipt_again_before_second_paid_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.first.client.quota.return_value

        async def quota(kind):
            entered.set()
            await release.wait()
            return original

        self.first.client.quota.side_effect = quota
        first = asyncio.create_task(self.pool.generate(0, self.body, "preflight-parallel"))
        await entered.wait()
        second = asyncio.create_task(self.pool.generate(0, self.body, "preflight-parallel"))
        release.set()
        a, b = await asyncio.gather(first, second)
        self.assertEqual(a, b)
        self.first.client.generate_speech.assert_awaited_once()
        self.second.client.generate_speech.assert_not_awaited()
        self.assertEqual(self.first.client.quota.await_count, 1)

    async def test_exhausted_quota_blocks_new_generation_without_switching_claimed_account(self):
        self.first.client.quota.return_value = [[None, ["20", None, "0"], "20"]]
        key = "preflight-zero-quota"
        for _ in range(2):
            with self.assertRaises(GatewayError) as caught:
                await self.pool.generate(0, self.body, key)
            self.assertEqual(
                (caught.exception.status, caught.exception.code), (429, "quota_exhausted")
            )
        generation = "vids_" + hashlib.sha256(key.encode()).hexdigest()
        self.assertFalse(self.first._path(generation).exists())
        self.assertFalse(self.second._path(generation).exists())
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_not_awaited()
        self.second.client.quota.assert_not_awaited()
        with self.assertRaises(GatewayError) as caught:
            await self.dispatcher.generate(2, self.body, key)
        self.assertEqual(caught.exception.code, "account_affinity_conflict")

    async def test_completed_and_unknown_receipts_replay_after_quota_is_exhausted(self):
        completed = await self.dispatcher.generate(1, self.body, "quota-completed")
        self.first.client.generate_speech.side_effect = VidsTransportError("private")
        unknown = await self.dispatcher.generate(1, self.body, "quota-unknown")
        self.first.client.quota.return_value = [[None, ["20", None, "0"], "20"]]
        self.first.client.quota.reset_mock()
        self.second.client.quota.reset_mock()
        self.assertEqual(await self.pool.generate(0, self.body, "quota-completed"), completed)
        self.assertEqual(await self.pool.generate(0, self.body, "quota-unknown"), unknown)
        self.first.client.quota.assert_not_awaited()
        self.second.client.quota.assert_not_awaited()
        self.assertEqual(self.first.client.generate_speech.await_count, 2)
        self.second.client.generate_speech.assert_not_awaited()

    async def test_disabled_busy_or_identity_drift_cannot_probe_repair_or_submit(self):
        self.setup.tokens[1].is_active = False
        with self.assertRaises(GatewayError) as caught:
            await self.dispatcher.generate(1, self.body, "preflight-disabled")
        self.assertEqual(caught.exception.code, "account_disabled")
        self.setup.tokens[1].is_active = True
        await self.first.lock.acquire()
        try:
            with self.assertRaises(GatewayError) as caught:
                await self.dispatcher.generate(1, self.body, "preflight-busy")
            self.assertEqual(caught.exception.code, "busy")
        finally:
            self.first.lock.release()
        self.first.client.flags["docs-hue"] = "wrong identity"
        with self.assertRaises(GatewayError) as caught:
            await self.dispatcher.generate(1, self.body, "preflight-drift")
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.repair.assert_not_awaited()
        self.first.client.quota.assert_not_awaited()
        self.first.client.generate_speech.assert_not_awaited()

    async def test_recoverable_invalid_initialization_stays_on_claimed_account_and_paid401_never_retries(
        self,
    ):
        self.first.client.initialization_valid = False

        async def repair(token_id):
            self.assertEqual(token_id, 1)
            self.first.client.initialization_valid = True

        self.repair.side_effect = repair
        self.first.client.generate_speech.side_effect = VidsProtocolError("generate", 401)
        result = await self.dispatcher.generate(1, self.body, "preflight-paid401")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "upstream_http_401")
        self.assertEqual(await self.pool.generate(0, self.body, "preflight-paid401"), result)
        self.repair.assert_awaited_once_with(1)
        self.first.client.generate_speech.assert_awaited_once()
        self.second.client.generate_speech.assert_not_awaited()

    async def test_new_pool_request_prefers_healthy_account_before_invalid_session_repair(self):
        self.first.client.initialization_valid = False
        result = await self.pool.generate(0, self.body, "healthy-pool-account")
        self.assertEqual(result["status"], "completed")
        self.repair.assert_not_awaited()
        self.first.client.quota.assert_not_awaited()
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_awaited_once()
        self.assertEqual(await self.pool.generate(0, self.body, "healthy-pool-account"), result)

    async def test_failed403_repair_isolates_session_for_next_new_pool_request(self):
        self.first.client.quota.side_effect = VidsProtocolError("private quota", 403)
        self.repair.side_effect = GatewayError(409, "login_required", "Synthetic repair failure")
        with self.assertRaises(GatewayError) as caught:
            await self.pool.generate(0, self.body, "expired-session-key")
        self.assertEqual(caught.exception.code, "login_required")
        self.assertFalse(self.first.client.initialization_valid)
        # Start selection before the expired account again; health must win over rotation.
        self.dispatcher._pool_last_selected["speech"] = 0
        healthy = await self.pool.generate(0, self.body, "after-expired-session")
        self.assertEqual(healthy["status"], "completed")
        self.repair.assert_awaited_once_with(1)
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_awaited_once()
        identifier = "vids_" + hashlib.sha256(b"expired-session-key").hexdigest()
        self.assertEqual(self.pool._claimed_owner(identifier), 1)
        self.assertFalse(self.first._path(identifier).exists())
        self.assertFalse(self.second._path(identifier).exists())

    async def test_pool_repairs_original_account_when_all_other_accounts_are_disabled(self):
        self.first.client.initialization_valid = False
        self.setup.tokens[2].is_active = False

        async def repair(token_id):
            self.assertEqual(token_id, 1)
            self.first.client.initialization_valid = True

        self.repair.side_effect = repair
        result = await self.pool.generate(0, self.body, "single-recoverable-account")
        self.assertEqual(result["status"], "completed")
        self.repair.assert_awaited_once_with(1)
        self.first.client.generate_speech.assert_awaited_once()
        self.second.client.generate_speech.assert_not_awaited()

    async def test_image_edit_source_keeps_its_original_account_during_repair(self):
        source = self.dispatcher.document_reference(2, "synthetic-saved-shape")
        self.second.client.initialization_valid = False
        self.second.client.edit_document_image = AsyncMock(return_value=[])

        async def repair(token_id):
            self.assertEqual(token_id, 2)
            self.second.client.initialization_valid = True

        self.repair.side_effect = repair
        result = await self.pool.generate(
            0,
            {"kind": "image", "mode": "edit", "prompt": "Blue boat", "source_id": source},
            "preflight-edit-source",
        )
        self.assertEqual(result["status"], "no_result")
        self.repair.assert_awaited_once_with(2)
        self.second.client.edit_document_image.assert_awaited_once_with(
            "synthetic-saved-shape", "Blue boat"
        )
        self.first.client.quota.assert_not_awaited()
        self.first.client.generate_speech.assert_not_awaited()

    async def test_status_counts_recoverable_sessions_without_upstream_calls_and_rejects_invalid_pin_or_disabled(
        self,
    ):
        self.first.client.initialization_valid = False
        self.setup.tokens[2].is_active = False
        app = FastAPI()

        async def authenticate():
            return True

        app.include_router(create_vids_admin_router(self.dispatcher, authenticate))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        ) as http:
            state = (await http.get("/api/admin/vids/status")).json()
            self.assertEqual(state["available"], {"image": 0, "video": 0, "speech": 0})
            self.assertEqual(state["recoverable"], {"image": 1, "video": 1, "speech": 1})
            (self.first.root / ".account").write_text("a" * 64)
            state = (await http.get("/api/admin/vids/status")).json()
            self.assertEqual(state["recoverable"], {"image": 0, "video": 0, "speech": 0})
            (self.first.root / ".account").write_text(
                hashlib.sha256(self.first.identity.encode()).hexdigest()
            )
            self.setup.tokens[1].is_active = False
            state = (await http.get("/api/admin/vids/status")).json()
            self.assertEqual(state["recoverable"], {"image": 0, "video": 0, "speech": 0})
        self.first.client.quota.assert_not_awaited()
        self.second.client.quota.assert_not_awaited()
        self.repair.assert_not_awaited()

    async def test_document_scene_and_image_reads_repair_original_account_with_zero_quota(
        self,
    ):
        self.setup.tokens[2].is_active = False
        self.first.client.model_chunk = {
            "revision": 4,
            "chunk": [
                [12, "private-scene", 0, 0, [16, 5000]],
                [3, "private-shape", 3, [1, 0, 0, 1, 0, 0], [49, "private-cosmo"], "private-scene"],
            ],
        }
        self.first.client.initialization_valid = False
        self.first.client.quota.return_value = [[None, ["20", None, "0"], "20"]]
        self.first.client.insert_document_image = AsyncMock()

        async def repair(token_id):
            self.assertEqual(token_id, 1)
            self.first.client.initialization_valid = True

        self.repair.side_effect = repair
        scenes = await self.pool.document_images(0, True)
        images = await self.pool.document_images(0)
        self.repair.assert_awaited_once_with(1)
        self.assertEqual(len(scenes["data"]), 1)
        self.assertEqual(len(images["data"]), 1)
        reference = images["data"][0]["source_id"]
        self.assertEqual(
            self.dispatcher.resolve_document_reference(reference), (1, "private-shape")
        )
        scene_reference = scenes["data"][0]["scene_id"]
        self.assertEqual(
            self.dispatcher.resolve_document_reference(scene_reference, scenes=True),
            (1, "private-scene"),
        )
        self.assertNotIn("private", str(images) + str(scenes))
        self.assertEqual(list(self.first.root.glob("vids_*.json")), [])
        self.first.client.insert_document_image.assert_not_awaited()
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_not_awaited()

    async def test_document_reads_do_not_repair_disabled_account(self):
        self.setup.tokens[1].is_active = self.setup.tokens[2].is_active = False
        self.first.client.initialization_valid = False
        self.assertEqual(await self.pool.document_images(0, True), {"object": "list", "data": []})
        self.repair.assert_not_awaited()
        self.first.client.quota.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
