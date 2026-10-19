"""Admin document references keep image edits and uploads on their source account."""

import base64
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.services.vids.client import VidsTransportError
from tests import test_vids_console_media as console_tests
from tests.test_vids_images_api import JPEG


class VidsDocumentSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.console = console_tests.VidsConsoleMediaTests()
        await self.console.asyncSetUp()
        self.http = self.console.http
        self.media = self.console.media
        for gateway in self.media.gateways.values():
            gateway.client.model_chunk = {
                "revision": 4,
                "chunk": [
                    [12, "private-scene", 0, 0, [16, 5000]],
                    [
                        3,
                        "private-shape",
                        3,
                        [1, 0, 0, 1, 0, 0],
                        [49, "private-cosmo"],
                        "private-scene",
                    ],
                ],
            }
            gateway.client.edit_document_image = AsyncMock(
                return_value=gateway.client.generate_image.return_value
            )
            gateway.client.insert_document_image = AsyncMock(side_effect=self.complete_insert)
        await self.console.login()

    async def asyncTearDown(self):
        await self.console.asyncTearDown()

    url = staticmethod(console_tests.VidsConsoleMediaTests.url)

    async def sources(self, scenes=False):
        response = await self.http.get(
            self.url("/vids/document/" + ("scenes" if scenes else "images"))
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    async def create(self, key, body):
        return await self.console.create("/vids/generations", key, body)

    @staticmethod
    async def complete_insert(path, *, mime_type, shape_id, scene_id, checkpoint):
        if not Path(path).read_bytes().startswith(b"\xff\xd8\xff"):
            raise ValueError("invalid synthetic image")
        checkpoint(
            {
                "image": {"cosmo_id": "private-cosmo", "width": 1024, "height": 1024},
                "shape_id": shape_id,
                "scene_id": scene_id,
                "revision": 4,
            }
        )
        return {"shape_id": shape_id, "scene_id": scene_id, "revision": 5}

    async def upload(self, key, scene_id):
        return await self.console.create(
            "/vids/document/images",
            key,
            {"image_base64": base64.b64encode(JPEG).decode(), "scene_id": scene_id},
        )

    async def test_catalog_describes_only_verified_specifications_and_console_source_contract(self):
        rows = (await self.http.get(self.url("/models"))).json()["data"]
        capabilities = {row["kind"]: row["capabilities"] for row in rows}
        self.assertEqual(capabilities["image"]["verified_aspects"], ["square"])
        self.assertEqual(capabilities["image"]["modes"], ["text", "edit"])
        self.assertEqual(
            capabilities["video"]["extend_output_seconds"],
            "min(uploaded source duration, 10) plus requested seconds",
        )
        self.assertIn(
            {"mode": "text", "seconds": 4, "size": "720x1280"},
            capabilities["video"]["verified_specifications"],
        )
        self.assertEqual(len(capabilities["speech"]["voices"]), 30)
        self.assertEqual(capabilities["speech"]["verified_voices"], ["Puck", "Gacrux", "Algenib"])

    async def test_lists_return_opaque_account_bound_refs_without_private_fields_or_generation(
        self,
    ):
        for scenes, field, prefix in (
            (False, "source_id", "vids_image_"),
            (True, "scene_id", "vids_scene_"),
        ):
            rows = await self.sources(scenes)
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows[0]), {field, "label"})
            self.assertNotEqual(rows[0][field], rows[1][field])
            self.assertTrue(rows[0][field].startswith(prefix))
            self.assertNotIn("private", str(rows))
        for gateway in self.media.gateways.values():
            gateway.client.generate_image.assert_not_awaited()
            gateway.client.insert_document_image.assert_not_awaited()

    async def test_edit_uses_source_account_and_rejects_source_switch_on_replay(self):
        refs = await self.sources()
        body = {
            "kind": "image",
            "mode": "edit",
            "prompt": "Blue boat",
            "source_id": refs[1]["source_id"],
        }
        first = await self.create("edit-owner", body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "completed")
        replay = await self.create("edit-owner", body)
        self.assertEqual(replay.json(), first.json())
        changed = await self.create("edit-owner", {**body, "source_id": refs[0]["source_id"]})
        self.assertEqual(changed.status_code, 409)
        self.media.gateways[2].client.edit_document_image.assert_awaited_once_with(
            "private-shape", "Blue boat"
        )
        self.media.gateways[1].client.edit_document_image.assert_not_awaited()

    async def test_source_disabled_and_raw_shapes_cannot_fall_back_to_other_account(self):
        refs = await self.sources()
        self.media.tokens[2].is_active = False
        body = {
            "kind": "image",
            "mode": "edit",
            "prompt": "Blue boat",
            "source_id": refs[1]["source_id"],
        }
        denied = await self.create("edit-disabled", body)
        self.assertEqual(denied.status_code, 503)
        raw = await self.create(
            "edit-raw",
            {"kind": "image", "mode": "edit", "prompt": "Blue boat", "shape_id": "private-shape"},
        )
        self.assertEqual(raw.status_code, 400)
        for gateway in self.media.gateways.values():
            gateway.client.edit_document_image.assert_not_awaited()

    async def test_upload_is_explicit_idempotent_and_returns_edit_reference_without_private_metadata(
        self,
    ):
        scenes = await self.sources(True)
        first = await self.upload("upload-owner", scenes[1]["scene_id"])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "completed")
        self.assertTrue(first.json()["source_id"].startswith("vids_image_"))
        self.assertEqual(first.json()["metadata"], {})
        self.assertNotIn("private", first.text)
        self.assertEqual(
            (await self.upload("upload-owner", scenes[1]["scene_id"])).json(), first.json()
        )
        edited = await self.create(
            "uploaded-edit",
            {
                "kind": "image",
                "mode": "edit",
                "prompt": "Blue boat",
                "source_id": first.json()["source_id"],
            },
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.media.gateways[2].client.insert_document_image.assert_awaited_once()
        self.media.gateways[1].client.insert_document_image.assert_not_awaited()
        self.media.gateways[2].client.edit_document_image.assert_awaited_once()
        self.media.gateways[2].client.generate_image.assert_not_awaited()

    async def test_unknown_upload_verify_reads_snapshot_without_uploading_again(self):
        scenes = await self.sources(True)

        async def unknown(path, **options):
            await self.complete_insert(path, **options)
            raise VidsTransportError("private save URL")

        upstream = self.media.gateways[2].client
        upstream.insert_document_image.side_effect = unknown
        first = await self.upload("upload-unknown", scenes[1]["scene_id"])
        self.assertEqual(first.status_code, 202, first.text)
        self.assertIsNone(first.json()["source_id"])
        record = self.media.gateways[2].get(first.json()["id"])
        source = record["insertion_source"]
        upstream.model_chunk = {
            "revision": 5,
            "chunk": [
                [
                    3,
                    source["shape_id"],
                    3,
                    [1, 0, 0, 1, 0, 0],
                    [49, "private-cosmo"],
                    "private-scene",
                ]
            ],
        }
        confirmed = await self.http.post(
            self.url("/vids/document/images/" + first.json()["id"] + "/verify")
        )
        self.assertEqual(confirmed.json()["status"], "completed", confirmed.text)
        self.assertTrue(confirmed.json()["source_id"].startswith("vids_image_"))
        self.assertNotIn("private", confirmed.text)
        upstream.insert_document_image.assert_awaited_once()
        upstream.generate_image.assert_not_awaited()

    async def test_references_restore_from_snapshot_and_insert_receipt_after_pool_restart(self):
        scenes = await self.sources(True)
        saved = await self.upload("upload-restored", scenes[1]["scene_id"])
        refs = await self.sources()
        self.media.dispatcher.replace([self.media.binding(1), self.media.binding(2)])
        for index, reference in enumerate((refs[1]["source_id"], saved.json()["source_id"])):
            edited = await self.create(
                "restored-edit-" + str(index),
                {"kind": "image", "mode": "edit", "prompt": "Blue boat", "source_id": reference},
            )
            self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(self.media.gateways[2].client.edit_document_image.await_count, 2)
        self.media.gateways[1].client.edit_document_image.assert_not_awaited()

    async def test_invalid_upload_and_source_never_write_or_generate(self):
        scene = (await self.sources(True))[0]["scene_id"]
        for body in (
            {"image_base64": "bad", "scene_id": scene},
            {"image_base64": base64.b64encode(JPEG).decode(), "scene_id": "private-scene"},
            {"image_base64": base64.b64encode(JPEG).decode(), "scene_id": scene, "token_id": 2},
        ):
            response = await self.console.create("/vids/document/images", "invalid-upload", body)
            self.assertEqual(response.status_code, 400, response.text)
        invalid = await self.create(
            "invalid-ref",
            {
                "kind": "image",
                "mode": "edit",
                "prompt": "Blue boat",
                "source_id": "vids_image_" + "0" * 64,
            },
        )
        self.assertEqual(invalid.status_code, 400)
        for gateway in self.media.gateways.values():
            gateway.client.insert_document_image.assert_not_awaited()
            gateway.client.edit_document_image.assert_not_awaited()

    async def test_busy_source_and_upload_keep_original_account_without_fallback(self):
        refs = await self.sources()
        scenes = await self.sources(True)
        gateway = self.media.gateways[2]
        await gateway.lock.acquire()
        try:
            edit = await self.create(
                "busy-edit",
                {
                    "kind": "image",
                    "mode": "edit",
                    "prompt": "Blue boat",
                    "source_id": refs[1]["source_id"],
                },
            )
            upload = await self.upload("busy-upload", scenes[1]["scene_id"])
        finally:
            gateway.lock.release()
        self.assertEqual(edit.status_code, 429, edit.text)
        self.assertEqual(upload.status_code, 429, upload.text)
        for candidate in self.media.gateways.values():
            candidate.client.insert_document_image.assert_not_awaited()
            candidate.client.edit_document_image.assert_not_awaited()

    async def test_upload_admission_rechecks_disabled_account_and_verify_rejects_generation(self):
        scene = (await self.sources(True))[1]["scene_id"]
        self.media.tokens[2].is_active = False
        denied = await self.upload("disabled-upload", scene)
        self.assertEqual(denied.status_code, 503, denied.text)
        self.media.gateways[2].client.insert_document_image.assert_not_awaited()
        self.media.gateways[1].client.insert_document_image.assert_not_awaited()
        generated = await self.create("plain-image", {"kind": "image", "prompt": "Blue boat"})
        rejected = await self.http.post(
            self.url("/vids/document/images/" + generated.json()["id"] + "/verify")
        )
        self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
