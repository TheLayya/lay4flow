"""Fresh Vids quota observations affect only admission for new paid work."""

import asyncio
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api.admin_routes.vids import create_vids_admin_router
from src.services.vids.client import VidsTransportError
from src.services.vids.dispatcher import QUOTA_FRESHNESS_SECONDS, VidsPoolDispatcher
from src.services.vids.gateway import GatewayError
from src.services.vids.runtime import VidsRuntime
from tests import test_vids_dispatcher as dispatcher_tests


class VidsQuotaRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup = dispatcher_tests.VidsDispatcherTests()
        await self.setup.asyncSetUp()
        self.dispatcher = self.setup.dispatcher
        self.first, self.second = self.setup.first, self.setup.second
        self.body = self.setup.body
        self.pool = VidsPoolDispatcher(self.dispatcher)
        self.first.client.quota.reset_mock()
        self.second.client.quota.reset_mock()
        for gateway in (self.first, self.second):
            gateway.client.generate_image = AsyncMock(return_value=[])
            gateway.client.edit_document_image = AsyncMock(return_value=[])

    async def asyncTearDown(self):
        await self.setup.asyncTearDown()

    async def observe(self, token_id, kind="speech", available=0):
        gateway = self.dispatcher._accounts[token_id].gateway
        gateway.client.quota.return_value = [[None, ["20", None, str(available)], "0"]]
        return await self.dispatcher.read_quota(token_id, kind)

    async def test_new_key_skips_fresh_zero_without_probing_or_claiming_that_account(self):
        await self.observe(1)
        self.first.client.quota.reset_mock()
        result = await self.pool.generate(0, self.body, "fresh-zero-skip")
        self.assertEqual(result["status"], "completed")
        self.first.client.quota.assert_not_awaited()
        self.second.client.quota.assert_not_awaited()
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_awaited_once()
        self.assertEqual(self.pool._claimed_owner(result["id"]), 2)

    async def test_all_fresh_zero_reject_without_claim_and_positive_refresh_restores_selection(
        self,
    ):
        await self.observe(1)
        await self.observe(2)
        identifier = "vids_" + hashlib.sha256(b"all-zero").hexdigest()
        with self.assertRaises(GatewayError) as caught:
            await self.pool.generate(0, self.body, "all-zero")
        self.assertEqual((caught.exception.status, caught.exception.code), (429, "quota_exhausted"))
        self.assertIsNone(self.pool._claimed_owner(identifier))
        await self.observe(2, available=1)
        result = await self.pool.generate(0, self.body, "all-zero")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.pool._claimed_owner(identifier), 2)
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_awaited_once()

    async def test_stale_zero_is_unknown_and_does_not_permanently_disable_recovered_account(self):
        with patch("src.services.vids.dispatcher.time.monotonic", return_value=100) as clock:
            await self.observe(1)
            clock.return_value = 100 + QUOTA_FRESHNESS_SECONDS
            result = await self.pool.generate(0, self.body, "stale-zero")
        self.assertEqual(result["status"], "completed")
        self.first.client.generate_speech.assert_awaited_once()
        self.second.client.generate_speech.assert_not_awaited()

    async def test_quota_kind_isolation_does_not_block_other_generation_kinds(self):
        await self.observe(1, "image")
        result = await self.pool.generate(0, self.body, "image-zero-speech")
        self.assertEqual(result["status"], "completed")
        self.first.client.generate_speech.assert_awaited_once()
        self.second.client.generate_speech.assert_not_awaited()

    async def test_failed_or_cancelled_refresh_invalidates_zero_without_extending_freshness(self):
        for failure in (VidsTransportError("synthetic-private"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                self.first.client.quota.side_effect = None
                await self.observe(1)
                self.first.client.quota.side_effect = failure
                with self.assertRaises(type(failure)):
                    await self.dispatcher.read_quota(1, "speech")
                self.assertFalse(self.dispatcher.quota_exhausted(1, "speech"))

    async def test_generation_preflight_observation_skips_zero_only_for_subsequent_new_key(self):
        self.dispatcher.repair_session = AsyncMock()
        self.first.client.quota.return_value = [[None, ["20", None, "0"], "20"]]
        with self.assertRaises(GatewayError) as caught:
            await self.pool.generate(0, self.body, "first-zero-observation")
        self.assertEqual(caught.exception.code, "quota_exhausted")
        # Rotation alone must not explain the next selection.
        self.dispatcher._pool_last_selected["speech"] = 0
        result = await self.pool.generate(0, self.body, "after-zero-observation")
        self.assertEqual(result["status"], "completed")
        with self.assertRaises(GatewayError) as caught:
            await self.pool.generate(0, self.body, "first-zero-observation")
        self.assertEqual(caught.exception.code, "quota_exhausted")
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_awaited_once()
        self.dispatcher.repair_session.assert_not_awaited()

    async def test_completed_and_unknown_receipts_replay_without_quota_probe_or_paid_retry(self):
        completed = await self.dispatcher.generate(1, self.body, "known-completed")
        self.first.client.generate_speech.side_effect = VidsTransportError("synthetic-private")
        unknown = await self.dispatcher.generate(1, self.body, "known-unknown")
        await self.observe(1)
        self.first.client.quota.reset_mock()
        self.assertEqual(await self.pool.generate(0, self.body, "known-completed"), completed)
        self.assertEqual(await self.pool.generate(0, self.body, "known-unknown"), unknown)
        self.first.client.quota.assert_not_awaited()
        self.second.client.quota.assert_not_awaited()
        self.assertEqual(self.first.client.generate_speech.await_count, 2)
        self.second.client.generate_speech.assert_not_awaited()

    async def test_source_affinity_rejects_exhausted_original_account_without_switching(self):
        source = self.dispatcher.document_reference(1, "synthetic-saved-shape")
        await self.observe(1, "image")
        with self.assertRaises(GatewayError) as caught:
            await self.pool.generate(
                0,
                {"kind": "image", "mode": "edit", "prompt": "Blue boat", "source_id": source},
                "source-zero",
            )
        self.assertEqual(caught.exception.code, "quota_exhausted")
        identifier = "vids_" + hashlib.sha256(b"source-zero").hexdigest()
        self.assertEqual(self.pool._claimed_owner(identifier), 1)
        self.assertFalse(self.first._path(identifier).exists())
        self.first.client.edit_document_image.assert_not_awaited()
        self.second.client.generate_image.assert_not_awaited()

    async def test_quota_changed_during_final_account_lookup_prevents_paid_work(self):
        entered, release = asyncio.Event(), asyncio.Event()
        lookups = 0

        async def lookup(identifier):
            nonlocal lookups
            lookups += 1
            if lookups == 2:
                entered.set()
                await release.wait()
            return self.setup.tokens.get(identifier)

        self.dispatcher.db.get_token.side_effect = lookup
        pending = asyncio.create_task(self.pool.generate(0, self.body, "quota-race"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        # Model completion of a free read that already held the prior session lock.
        self.first.quota = AsyncMock(return_value={"available": 0})
        await self.dispatcher.read_quota(1, "speech")
        release.set()
        with self.assertRaises(GatewayError) as caught:
            await asyncio.wait_for(pending, timeout=5)
        self.assertEqual(caught.exception.code, "quota_exhausted")
        self.first.client.generate_speech.assert_not_awaited()
        self.second.client.generate_speech.assert_not_awaited()
        identifier = "vids_" + hashlib.sha256(b"quota-race").hexdigest()
        self.assertEqual(self.pool._claimed_owner(identifier), 1)
        self.assertFalse(self.first._path(identifier).exists())

    async def test_runtime_binding_replacement_discards_prior_quota_observations(self):
        await self.observe(1)
        self.dispatcher.replace(
            [self.setup.binding(1, self.first), self.setup.binding(2, self.second)]
        )
        self.assertFalse(self.dispatcher.quota_exhausted(1, "speech"))
        self.first.client.quota.reset_mock()
        result = await self.pool.generate(0, self.body, "after-binding-replacement")
        self.assertEqual(result["status"], "completed")
        self.first.client.quota.assert_not_awaited()
        self.first.client.generate_speech.assert_awaited_once()

    async def test_replaced_client_changed_feature_and_invalid_session_invalidate_old_zero(self):
        await self.observe(1, "video")
        original = self.first.client
        self.assertTrue(self.dispatcher.quota_exhausted(1, "video"))
        original.flags["flix-evgvl"] = True
        self.assertFalse(self.dispatcher.quota_exhausted(1, "video"))
        original.flags.pop("flix-evgvl")
        await self.observe(1, "video")
        original.initialization_valid = False
        self.assertFalse(self.dispatcher.quota_exhausted(1, "video"))
        original.initialization_valid = True
        await self.observe(1, "video")
        self.first.client = SimpleNamespace(flags=dict(original.flags), initialization_valid=True)
        self.assertFalse(self.dispatcher.quota_exhausted(1, "video"))
        self.first.client = original
        original.flags["docs-hue"] = "different-synthetic-identity"
        self.assertIsNone(self.dispatcher.quota_snapshot(1, "video"))
        original.flags["docs-hue"] = self.first.identity

    async def test_invalid_observation_never_keeps_a_preceding_zero(self):
        for value in (None, True, -1, "0"):
            await self.observe(1)
            with patch.object(self.first, "quota", AsyncMock(return_value={"available": value})):
                with self.assertRaises(GatewayError) as failure:
                    await self.dispatcher.read_quota(1, "speech")
                self.assertEqual(failure.exception.code, "schema_changed")
            self.assertFalse(self.dispatcher.quota_exhausted(1, "speech"))

    async def test_generation_observation_is_visible_to_admin_without_new_network_reads(self):
        await self.observe(1, "image", available=17)
        self.first.client.quota.reset_mock()
        app = FastAPI()
        app.include_router(create_vids_admin_router(self.dispatcher, lambda: True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.example.test"
        ) as http:
            response = await http.get("/api/admin/vids/accounts/detail")
        row = next(item for item in response.json()["accounts"] if item["token_id"] == 1)
        self.assertEqual(row["last_quota"]["image"]["available"], 17)
        self.first.client.quota.assert_not_awaited()

    async def test_account_removal_forgets_its_quota_and_preserves_sibling_observations(self):
        await self.observe(1)
        await self.observe(1, "image")
        await self.observe(2)
        runtime = VidsRuntime(self.dispatcher, None)
        runtime._gateways = [self.first, self.second]
        await runtime.remove_account(1)
        self.assertFalse(any(key[0] == 1 for key in self.dispatcher._quota_observations))
        self.assertTrue(self.dispatcher.quota_exhausted(2, "speech"))
        self.assertTrue(self.second._ready)
        self.assertFalse(self.first._ready)
        self.second.client.session.close.assert_not_awaited()

    async def test_old_binding_read_cannot_publish_or_invalidate_replacement_observation(self):
        for fail_old_read in (False, True):
            with self.subTest(fail_old_read=fail_old_read):
                entered, release = asyncio.Event(), asyncio.Event()
                old_account = self.dispatcher._accounts[1]

                async def old_read(kind):
                    entered.set()
                    await release.wait()
                    if fail_old_read:
                        raise VidsTransportError("synthetic-old-binding")
                    return {"available": 19}

                old_account.gateway.quota = AsyncMock(side_effect=old_read)
                pending = asyncio.create_task(self.dispatcher.read_quota(1, "speech"))
                await asyncio.wait_for(entered.wait(), timeout=5)
                replacement = await self.setup.gateway_for(10 + int(fail_old_read))
                self.dispatcher.replace(
                    [self.setup.binding(1, replacement), self.setup.binding(2, self.second)]
                )
                await self.observe(1)
                release.set()
                if fail_old_read:
                    with self.assertRaises(VidsTransportError):
                        await pending
                else:
                    self.assertEqual(await pending, {"available": 19})
                self.assertTrue(self.dispatcher.quota_exhausted(1, "speech"))

    async def test_admin_refresh_publishes_quota_for_pool_selection(self):
        async def authenticate():
            return True

        app = FastAPI()
        app.include_router(create_vids_admin_router(self.dispatcher, authenticate))
        self.first.client.quota.return_value = [[None, ["20", None, "0"], "20"]]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.example.test"
        ) as http:
            response = await http.post("/api/admin/vids/accounts/1/quota-refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["last_quota"]["image"]["available"], 0)
        self.first.client.quota.reset_mock()
        result = await self.pool.generate(0, {"kind": "image", "prompt": "Blue boat"}, "admin-zero")
        self.assertEqual(result["status"], "no_result")
        self.assertEqual(self.pool._claimed_owner(result["id"]), 2)
        self.first.client.generate_image.assert_not_awaited()
        self.second.client.generate_image.assert_awaited_once()
        self.first.client.quota.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
