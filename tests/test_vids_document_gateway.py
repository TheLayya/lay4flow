import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from test_vids_gateway import KEY, client

from src.api.vids import create_vids_app
from src.services.vids.client import VidsTransportError
from src.services.vids.gateway import VidsGateway


class VidsDocumentGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_scene_discovery_never_uploads_or_inserts(self):
        self.client.model_chunk = {"revision": 4, "chunk": [[12, "scene", 0, 0, [16, 5000]]]}
        response = await self.http.get("/v1/vids/document/scenes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"object": "list", "revision": 4, "data": [{"scene_id": "scene"}]}
        )
        self.client.insert_document_image.assert_not_awaited()
        self.client.upload_image.assert_not_awaited()

    async def test_shape_delete_snapshot_does_not_expose_images_or_scenes(self):
        self.client.model_chunk = {
            "revision": 4,
            "chunk": [
                [12, "scene", 0, 0, [16, 5000]],
                [3, "deleted-shape", 3, [1, 0, 0, 1, 0, 0], [49, "private-cosmo"], "scene"],
                [0, ["deleted-shape"]],
            ],
        }
        for route in ("images", "scenes"):
            with self.subTest(route=route):
                response = await self.http.get("/v1/vids/document/" + route)
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("deleted-shape", response.text)
                self.assertNotIn("private-cosmo", response.text)
        self.client.insert_document_image.assert_not_awaited()
        self.client.upload_image.assert_not_awaited()

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = client()
        self.client.model_chunk = {"revision": 3, "chunk": []}
        self.client.insert_document_image = AsyncMock(side_effect=self.complete_insert)
        self.client.upload_image = AsyncMock()
        self.client.generate_image = AsyncMock()
        self.gateway = VidsGateway(self.client, self.directory.name)
        await self.gateway.start()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_vids_app(self.gateway, KEY)),
            base_url="http://localhost",
            headers={"Authorization": "Bearer " + KEY},
        )
        self.body = {
            "image_base64": base64.b64encode(b"\xff\xd8\xffsynthetic-image").decode(),
            "scene_id": "scene",
        }
        self.route = "/v1/vids/document/images"

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.gateway.close()
        self.directory.cleanup()

    @staticmethod
    def source(shape_id, scene_id):
        return {
            "image": {"cosmo_id": "private-cosmo", "width": 1024, "height": 1024},
            "shape_id": shape_id,
            "scene_id": scene_id,
            "revision": 3,
        }

    async def complete_insert(self, path, *, mime_type, shape_id, scene_id, checkpoint):
        self.assertEqual(Path(path).read_bytes(), base64.b64decode(self.body["image_base64"]))
        self.assertEqual(mime_type, "image/jpeg")
        checkpoint(self.source(shape_id, scene_id))
        return {"shape_id": shape_id, "scene_id": scene_id, "revision": 4}

    async def ambiguous_insert(self, path, *, mime_type, shape_id, scene_id, checkpoint):
        checkpoint(self.source(shape_id, scene_id))
        raise VidsTransportError("private signed save URL")

    def confirm_snapshot(self, source, *, revision=4, cosmo_id="private-cosmo", scene_id="scene"):
        async def snapshot():
            self.client.model_chunk = {
                "revision": revision,
                "chunk": [
                    [
                        3,
                        source["shape_id"],
                        3,
                        [1, 0, 0, 1, 0, 0],
                        [49, cosmo_id],
                        scene_id,
                    ]
                ],
            }

        self.client.bootstrap.side_effect = snapshot

    async def test_auth_and_invalid_input_never_create_receipts_or_insert(self):
        for headers in ({"Authorization": "Bearer invalid"}, {"Authorization": ""}):
            response = await self.http.post(self.route, json=self.body, headers=headers)
            self.assertEqual(response.status_code, 401)
        for body, headers in (
            (self.body, {}),
            ({**self.body, "extra": "private"}, {"Idempotency-Key": "invalid"}),
            ({"scene_id": "scene"}, {"Idempotency-Key": "invalid"}),
            ({**self.body, "scene_id": "private scene"}, {"Idempotency-Key": "invalid"}),
            ({**self.body, "scene_id": 1}, {"Idempotency-Key": "invalid"}),
            ({**self.body, "image_base64": "not base64"}, {"Idempotency-Key": "invalid"}),
            ({**self.body, "image_base64": ""}, {"Idempotency-Key": "invalid"}),
            (
                {**self.body, "image_base64": base64.b64encode(b"text").decode()},
                {"Idempotency-Key": "invalid"},
            ),
        ):
            with self.subTest(body=body):
                response = await self.http.post(self.route, json=body, headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("private", response.text)
        self.client.insert_document_image.assert_not_awaited()
        self.client.upload_image.assert_not_awaited()
        self.assertEqual(list(Path(self.directory.name).glob("vids_*.json")), [])

    async def test_completed_insert_is_private_replayable_and_conflicts_on_changed_parameters(self):
        headers = {"Idempotency-Key": "same-insert"}
        first = await self.http.post(self.route, json=self.body, headers=headers)
        second = await self.http.post(self.route, json=self.body, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json(), first.json())
        record = first.json()
        self.assertEqual(record["object"], "vids.document_image")
        self.assertEqual(record["status"], "completed")
        self.assertIsNone(record["content_url"])
        self.assertEqual(record["metadata"]["revision"], 4)
        self.assertNotIn("private", first.text)
        self.client.insert_document_image.assert_awaited_once()
        receipt = self.gateway.get(record["id"])
        self.assertEqual(receipt["insertion_source"]["image"]["cosmo_id"], "private-cosmo")
        self.assertNotIn("image_base64", receipt)
        content = await self.http.get(f"/v1/vids/generations/{record['id']}/content")
        self.assertEqual(content.status_code, 409)
        changed = await self.http.post(
            self.route,
            json={**self.body, "scene_id": "other-scene"},
            headers=headers,
        )
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["error"]["code"], "idempotency_conflict")
        self.client.insert_document_image.assert_awaited_once()
        self.client.generate_image.assert_not_awaited()
        self.client.generate_speech.assert_not_awaited()

    async def test_single_account_lock_blocks_concurrent_insert_and_is_released(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def pending_insert(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.complete_insert(*args, **kwargs)

        self.client.insert_document_image.side_effect = pending_insert
        first = asyncio.create_task(self.gateway.insert_document_image(self.body, "pending"))
        await entered.wait()
        response = await self.http.post(
            self.route,
            json=self.body,
            headers={"Idempotency-Key": "concurrent"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "busy")
        release.set()
        self.assertEqual((await first)["status"], "completed")
        self.assertFalse(self.gateway.lock.locked())
        self.client.insert_document_image.assert_awaited_once()
        self.assertEqual(len(list(Path(self.directory.name).glob("vids_*.json"))), 1)

    async def test_unknown_insert_replays_receipt_and_verify_only_reads_bootstrap(self):
        self.client.insert_document_image.side_effect = self.ambiguous_insert
        headers = {"Idempotency-Key": "ambiguous-insert"}
        first = await self.http.post(self.route, json=self.body, headers=headers)
        self.assertEqual(first.status_code, 202)
        record = first.json()
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["error"], "insertion_acceptance_unknown")
        self.assertNotIn("private", first.text)
        replay = await self.http.post(self.route, json=self.body, headers=headers)
        self.assertEqual(replay.json(), record)
        source = self.gateway.get(record["id"])["insertion_source"]
        self.confirm_snapshot(source)
        self.client.bootstrap.reset_mock()
        verified = await self.http.post(self.route + "/" + record["id"] + "/verify")
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()["status"], "completed")
        self.assertIsNone(verified.json()["error"])
        self.client.bootstrap.assert_awaited_once()
        self.client.insert_document_image.assert_awaited_once()
        self.client.upload_image.assert_not_awaited()
        self.client.generate_image.assert_not_awaited()
        repeated = await self.http.post(self.route + "/" + record["id"] + "/verify")
        self.assertEqual(repeated.json(), verified.json())
        self.client.bootstrap.assert_awaited_once()

    async def test_verify_requires_original_shape_resource_scene_and_advanced_revision(self):
        self.client.insert_document_image.side_effect = self.ambiguous_insert
        record = await self.gateway.insert_document_image(self.body, "verify-boundaries")
        source = self.gateway.get(record["id"])["insertion_source"]
        for parameters in (
            {"revision": 3},
            {"cosmo_id": "other-cosmo"},
            {"scene_id": "other-scene"},
        ):
            self.confirm_snapshot(source, **parameters)
            with self.subTest(parameters=parameters):
                response = await self.http.post(self.route + "/" + record["id"] + "/verify")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "unknown")
        self.client.insert_document_image.assert_awaited_once()
        self.client.upload_image.assert_not_awaited()

    async def test_restart_converts_interrupted_insert_to_unknown_without_resubmitting(self):
        record = await self.gateway.insert_document_image(self.body, "interrupted-insert")
        receipt = self.gateway.get(record["id"])
        receipt.update(status="inserting", error=None)
        self.gateway._save(receipt)
        await self.gateway.close()
        replacement = client()
        replacement.insert_document_image = AsyncMock()
        restarted = VidsGateway(replacement, self.directory.name)
        await restarted.start()
        try:
            recovered = await restarted.insert_document_image(self.body, "interrupted-insert")
            self.assertEqual(recovered["status"], "unknown")
            self.assertEqual(recovered["error"], "interrupted_acceptance_unknown")
            self.assertEqual(recovered["metadata"], record["metadata"])
            self.assertIn("insertion_source", restarted.get(record["id"]))
            replacement.insert_document_image.assert_not_awaited()
        finally:
            await restarted.close()

    async def test_identity_drift_before_insert_or_during_verify_prevents_mutation(self):
        self.client.flags["docs-hue"] = "private-other-account"
        response = await self.http.post(
            self.route,
            json=self.body,
            headers={"Idempotency-Key": "wrong-account"},
        )
        self.assertEqual(response.status_code, 503)
        self.client.insert_document_image.assert_not_awaited()
        self.client.flags["docs-hue"] = "same-account"
        self.client.insert_document_image.side_effect = self.ambiguous_insert
        record = await self.gateway.insert_document_image(self.body, "verify-drift")

        async def drift():
            self.client.flags["docs-hue"] = "private-other-account"

        self.client.bootstrap.side_effect = drift
        response = await self.http.post(self.route + "/" + record["id"] + "/verify")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "session_persistence_failed")
        self.assertNotIn("private", response.text)
        self.assertEqual(self.gateway.get(record["id"])["status"], "unknown")
        self.client.insert_document_image.assert_awaited_once()
        self.client.upload_image.assert_not_awaited()

    async def test_cancelled_insert_keeps_checkpoint_and_unknown_receipt_for_read_only_recovery(
        self,
    ):
        async def cancelled(path, *, mime_type, shape_id, scene_id, checkpoint):
            checkpoint(self.source(shape_id, scene_id))
            raise asyncio.CancelledError()

        self.client.insert_document_image.side_effect = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.gateway.insert_document_image(self.body, "cancelled-insert")
        receipt = json.loads(next(Path(self.directory.name).glob("vids_*.json")).read_text())
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["error"], "insertion_acceptance_unknown")
        self.assertIn("insertion_source", receipt)
        replay = await self.gateway.insert_document_image(self.body, "cancelled-insert")
        self.assertEqual(replay["status"], "unknown")
        self.client.insert_document_image.assert_awaited_once()
        self.assertFalse(self.gateway.lock.locked())


if __name__ == "__main__":
    unittest.main()
