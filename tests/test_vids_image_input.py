"""Original-image editing keeps upload, paid submission and replay on one account."""

import asyncio
import base64
import unittest
from unittest.mock import AsyncMock

from src.services.vids.client import VidsProtocolError, VidsTransportError
from src.services.vids.dispatcher import VidsPoolDispatcher
from src.services.vids.gateway import GatewayError
from tests import test_vids_media_api as media_tests
from tests.test_vids_images_api import JPEG
from tests.test_vids_media_api import identifier


class VidsImageInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup = media_tests.VidsMediaAPITests()
        await self.setup.asyncSetUp()
        self.gateway = self.setup.gateways[1]
        self.upstream = self.gateway.client
        self.pool = VidsPoolDispatcher(self.setup.dispatcher)
        self.body = {
            "kind": "image",
            "mode": "edit",
            "prompt": "Make it evening",
            "image_base64": base64.b64encode(JPEG).decode(),
        }
        self.upstream.model_chunk = {
            "revision": 3,
            "chunk": [[12, "scene", 0, 0, [16, 5000]]],
        }
        self.upstream.insert_document_image = AsyncMock(side_effect=self.insert)
        self.upstream.edit_document_image = AsyncMock(
            return_value=self.upstream.generate_image.return_value
        )

    async def asyncTearDown(self):
        await self.setup.asyncTearDown()
        self.setup.doCleanups()

    async def insert(self, path, *, mime_type, shape_id, scene_id, checkpoint):
        self.assertEqual(path.read_bytes(), JPEG)
        self.assertEqual(mime_type, "image/jpeg")
        self.assertEqual(scene_id, "scene")
        self.assertTrue(self.gateway.lock.locked())
        checkpoint(
            {
                "image": {"cosmo_id": "private-cosmo", "width": 1024, "height": 1024},
                "shape_id": shape_id,
                "scene_id": scene_id,
                "revision": 3,
            }
        )
        return {"shape_id": shape_id, "scene_id": scene_id, "revision": 4}

    async def test_single_request_uploads_then_generates_and_replays_without_network(self):
        result = await self.pool.generate(0, self.body, "raw-image")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["id"], identifier("raw-image"))
        record = self.gateway.get(result["id"])
        insertion = self.gateway.get(record["image_insert_id"])
        self.upstream.edit_document_image.assert_awaited_once_with(
            insertion["metadata"]["shape_id"], self.body["prompt"]
        )
        self.upstream.insert_document_image.assert_awaited_once()
        self.upstream.generate_image.assert_not_awaited()
        self.assertNotIn("image_base64", str(record) + str(insertion))
        self.assertNotIn("private", str(result))
        self.setup.tokens[1].is_active = False
        self.upstream.initialization_valid = False
        self.upstream.bootstrap.reset_mock()
        self.upstream.quota.reset_mock()
        self.assertEqual(await self.pool.generate(0, self.body, "raw-image"), result)
        self.upstream.bootstrap.assert_not_awaited()
        self.upstream.quota.assert_not_awaited()
        with self.assertRaises(GatewayError) as caught:
            await self.pool.generate(0, {**self.body, "prompt": "Changed"}, "raw-image")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def test_unknown_upload_keeps_generation_receipt_and_never_buys_or_reuploads(self):
        self.upstream.insert_document_image.side_effect = VidsTransportError("private")
        result = await self.pool.generate(0, self.body, "unknown-upload")
        self.assertEqual((result["status"], result["error"]), ("unknown", "image_upload_unknown"))
        self.assertEqual(result["id"], identifier("unknown-upload"))
        self.assertEqual(await self.pool.generate(0, self.body, "unknown-upload"), result)
        self.upstream.insert_document_image.assert_awaited_once()
        self.upstream.edit_document_image.assert_not_awaited()
        self.setup.gateways[2].client.generate_image.assert_not_awaited()
        self.assertFalse(self.gateway.lock.locked())

    async def test_paid401_and_unknown_replay_without_upload_or_account_switch(self):
        self.setup.tokens[2].image_enabled = False
        for index, failure in enumerate(
            (VidsProtocolError("private", 401), VidsTransportError("private"))
        ):
            self.upstream.insert_document_image.reset_mock()
            self.upstream.edit_document_image.reset_mock()
            self.upstream.edit_document_image.side_effect = failure
            key = f"paid-failure-{index}"
            result = await self.pool.generate(0, self.body, key)
            self.assertEqual(result["status"], "failed" if index == 0 else "unknown")
            self.assertEqual(await self.pool.generate(0, self.body, key), result)
            self.upstream.insert_document_image.assert_awaited_once()
            self.upstream.edit_document_image.assert_awaited_once()
        self.assertEqual(list(self.setup.gateways[2].root.glob("vids_*.json")), [])

    async def test_parallel_same_key_and_other_requests_cannot_interleave_upload_and_generation(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()

        async def pending_insert(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.insert(*args, **kwargs)

        self.upstream.insert_document_image.side_effect = pending_insert
        first = asyncio.create_task(self.pool.generate(0, self.body, "parallel-edit"))
        await entered.wait()
        replay = await self.pool.generate(0, self.body, "parallel-edit")
        self.assertEqual(replay["status"], "generating")
        with self.assertRaises(GatewayError) as caught:
            await self.setup.dispatcher.generate(1, self.body, "other-edit")
        self.assertEqual(caught.exception.code, "busy")
        release.set()
        self.assertEqual((await first)["status"], "completed")
        self.upstream.insert_document_image.assert_awaited_once()
        self.upstream.edit_document_image.assert_awaited_once()

    async def test_disabled_after_upload_prevents_paid_submission(self):
        async def disable_after_upload(*args, **kwargs):
            result = await self.insert(*args, **kwargs)
            self.setup.tokens[1].is_active = False
            return result

        async def paid_request(*args):
            from src.services.vids.client import BEFORE_PAID_REQUEST

            await BEFORE_PAID_REQUEST.get()()
            self.fail("Disabled account reached paid submission")

        self.upstream.insert_document_image.side_effect = disable_after_upload
        self.upstream.edit_document_image.side_effect = paid_request
        result = await self.pool.generate(0, self.body, "disabled-upload")
        self.assertEqual((result["status"], result["error"]), ("failed", "account_disabled"))
        self.assertEqual(await self.pool.generate(0, self.body, "disabled-upload"), result)

    async def test_invalid_source_and_disabled_account_never_upload_or_create_receipts(self):
        for changes in (
            {"image_base64": ""},
            {"image_base64": "bad"},
            {"shape_id": "shape"},
            {"source_id": "vids_image_" + "a" * 64},
            {"mode": "text"},
        ):
            with self.subTest(changes=changes), self.assertRaises(GatewayError):
                await self.pool.generate(0, {**self.body, **changes}, "invalid-input")
        self.setup.tokens[1].is_active = self.setup.tokens[2].is_active = False
        with self.assertRaises(GatewayError):
            await self.pool.generate(0, self.body, "disabled-input")
        self.assertEqual(list(self.gateway.root.glob("vids_*.json")), [])
        self.upstream.insert_document_image.assert_not_awaited()
        self.upstream.edit_document_image.assert_not_awaited()

    async def test_no_scene_stops_before_upload_and_generation(self):
        self.upstream.model_chunk["chunk"] = []
        result = await self.pool.generate(0, self.body, "no-scene")
        self.assertEqual((result["status"], result["error"]), ("failed", "invalid_source"))
        self.upstream.insert_document_image.assert_not_awaited()
        self.upstream.edit_document_image.assert_not_awaited()

    async def test_internal_upload_claim_rejects_other_account_namespace_collisions(self):
        for existing_operation in ("insert", "generation"):
            key = "child-claim-" + existing_operation
            other = self.setup.gateways[2]
            if existing_operation == "insert":
                other.client.insert_document_image = AsyncMock(
                    return_value={"shape_id": "other-shape", "scene_id": "scene", "revision": 4}
                )
                scene = self.setup.dispatcher.document_reference(2, "scene", scenes=True)
                previous = await self.pool.insert_document_image(
                    0,
                    {"image_base64": self.body["image_base64"], "scene_id": scene},
                    key,
                )
            else:
                previous = await self.setup.dispatcher.generate(
                    2, {"kind": "image", "prompt": "Existing image"}, "document-image:" + key
                )
            self.assertEqual(previous["status"], "completed")
            self.setup.tokens[2].image_enabled = False
            with self.assertRaises(GatewayError) as caught:
                await self.pool.generate(0, self.body, key)
            self.setup.tokens[2].image_enabled = True
            self.assertEqual(caught.exception.code, "account_affinity_conflict")
            self.assertFalse(self.gateway._path(identifier(key)).exists())
            self.assertFalse(self.gateway._path(previous["id"]).exists())
            # No duplicate child can poison pool listing or route its owner's old receipt.
            self.assertEqual(self.pool.get(0, previous["id"])["id"], previous["id"])
            self.pool.list(0)
        self.upstream.insert_document_image.assert_not_awaited()
        self.upstream.edit_document_image.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
