"""Native text-image output counts and durable download recovery use synthetic media only."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.vids.client import image_results
from src.services.vids.dispatcher import VidsAccount, VidsPoolDispatcher, VidsProviderDispatcher
from src.services.vids.gateway import GatewayError, VidsGateway
from tests.test_vids_gateway import client

JPEG = b"\xff\xd8\xffsynthetic"
BODY = {"kind": "image", "prompt": "Native image variants"}


def image_response(count, *, width=1024, height=1024):
    return [
        [None] * 11
        + [[None] * 6 + [[f"key-{index}", f"https://example.test/{index}", width, height]]]
        for index in range(count)
    ]


class VidsImageOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.upstream = client()
        self.upstream.generate_image = AsyncMock()

        async def download(resource, path):
            Path(path).write_bytes(JPEG + resource["unique_key"].encode())

        self.upstream.download_image = AsyncMock(side_effect=download)
        self.upstream.edit_document_image = AsyncMock()
        self.gateway = VidsGateway(self.upstream, self.directory.name)
        await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.close()
        self.directory.cleanup()

    async def test_counts_one_to_four_download_each_unique_output_and_replay_after_restart(self):
        for count in range(1, 5):
            self.upstream.generate_image.reset_mock()
            self.upstream.download_image.reset_mock()
            response = image_response(count)
            self.upstream.generate_image.return_value = [response, response]
            self.assertEqual(len(image_results([response, response])), count)
            body = {**BODY, "n": count}
            key = f"image-count-{count}"
            result = await self.gateway.generate(body, key)
            self.assertEqual(result["status"], "completed")
            expected_options = {"count": count} if count > 1 else {}
            self.upstream.generate_image.assert_awaited_once_with(
                BODY["prompt"], **expected_options
            )
            self.assertEqual(self.upstream.download_image.await_count, count)
            record = self.gateway.get(result["id"])
            self.assertEqual(record["resource"]["unique_key"], "key-0")
            if count > 1:
                self.assertEqual(record["image_count"], count)
                self.assertEqual(
                    [output["index"] for output in result["outputs"]], list(range(count))
                )
                self.assertEqual(result["outputs"][0]["content_url"], result["content_url"])
                self.assertTrue(
                    result["outputs"][-1]["content_url"].endswith(f"?index={count - 1}")
                )
                self.assertNotIn("key-", str(result))
                self.assertNotIn("example.test", str(result))
            else:
                self.assertNotIn("outputs", result)
                self.assertNotIn("image_count", record)
            for index in range(count):
                path, mime = self.gateway.content(result["id"], index)
                self.assertEqual(path.name, result["id"] + (f"_{index}" if index else "") + ".jpg")
                self.assertEqual(mime, "image/jpeg")
                self.assertEqual(path.read_bytes(), JPEG + f"key-{index}".encode())
            await self.gateway.close()
            await self.gateway.start()
            self.assertEqual(await self.gateway.generate(body, key), result)
            self.upstream.generate_image.assert_awaited_once()
            with self.assertRaises(GatewayError) as conflict:
                await self.gateway.generate({**BODY, "n": 4 if count != 4 else 3}, key)
            self.assertEqual(conflict.exception.code, "idempotency_conflict")

    async def test_invalid_counts_and_edit_batch_are_rejected_before_receipt_or_network(self):
        self.assertEqual(self.gateway.normalize({**BODY, "n": 1}), BODY)
        self.assertEqual(self.gateway.normalize({**BODY, "n": 1, "mode": "text"}), BODY)
        for value in (None, True, 1.0, "1", [], 0, -1, 5):
            with self.subTest(value=value), self.assertRaises(GatewayError):
                await self.gateway.generate({**BODY, "n": value}, "invalid-count")
        with self.assertRaises(GatewayError):
            await self.gateway.generate(
                {**BODY, "mode": "edit", "shape_id": "shape", "n": 2}, "invalid-edit-count"
            )
        for kind in ("video", "speech", "music"):
            with self.assertRaises(GatewayError):
                await self.gateway.generate({**BODY, "kind": kind, "n": 1}, "invalid-kind-count")
        self.assertEqual(list(self.gateway.root.glob("vids_*.json")), [])
        self.upstream.generate_image.assert_not_awaited()
        self.upstream.edit_document_image.assert_not_awaited()

    async def test_count_and_ratio_validation_cover_every_unique_resource_before_download(self):
        for count in (1, 3):
            self.upstream.generate_image.return_value = image_response(count)
            result = await self.gateway.generate({**BODY, "n": 2}, f"wrong-count-{count}")
            self.assertEqual(result["status"], "specification_mismatch")
            self.assertEqual(result["error"], "upstream_image_count")
            self.assertIsNone(result["content_url"])
        response = image_response(2, width=1600, height=900)
        response[1] = image_response(2)[1]
        self.upstream.generate_image.return_value = response
        result = await self.gateway.generate(
            {**BODY, "n": 2, "aspect_ratio": "16:9"}, "wrong-ratio"
        )
        self.assertEqual(result["error"], "upstream_image_specification")
        self.upstream.download_image.assert_not_awaited()

    async def test_later_download_failure_preserves_acceptance_and_only_downloads_again(self):
        self.upstream.generate_image.return_value = image_response(3)
        download = self.upstream.download_image.side_effect

        async def fail_second(resource, path):
            if resource["unique_key"] == "key-1":
                raise OSError("synthetic interruption")
            await download(resource, path)

        self.upstream.download_image.side_effect = fail_second
        body = {**BODY, "n": 3}
        result = await self.gateway.generate(body, "recover-output-download")
        self.assertEqual(result["status"], "download_failed")
        self.assertNotIn("outputs", result)
        self.assertEqual(
            self.gateway.get(result["id"])["response"], self.upstream.generate_image.return_value
        )
        self.assertEqual(await self.gateway.generate(body, "recover-output-download"), result)
        with self.assertRaises(GatewayError):
            self.gateway.content(result["id"])
        self.upstream.download_image.side_effect = download
        completed = await self.gateway.retry_download(result["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(completed["outputs"]), 3)
        self.upstream.generate_image.assert_awaited_once()
        self.assertEqual(self.upstream.download_image.await_count, 5)

    async def test_invalid_later_image_bytes_prevent_completed_outputs(self):
        self.upstream.generate_image.return_value = image_response(2)

        async def invalid_second(resource, path):
            Path(path).write_bytes(JPEG if resource["unique_key"] == "key-0" else b"not-an-image")

        self.upstream.download_image.side_effect = invalid_second
        result = await self.gateway.generate({**BODY, "n": 2}, "invalid-output-bytes")
        self.assertEqual(result["status"], "download_failed")
        self.assertIsNone(result["content_url"])
        self.assertNotIn("outputs", result)
        self.assertEqual(self.upstream.download_image.await_count, 2)

    async def test_invalid_or_missing_output_indices_and_receipt_paths_cannot_escape_root(self):
        self.upstream.generate_image.return_value = image_response(2)
        result = await self.gateway.generate({**BODY, "n": 2}, "image-index")
        for index in (None, True, 1.0, "1", "../outside", -1, 4):
            with self.subTest(index=index), self.assertRaises(GatewayError) as caught:
                self.gateway.content(result["id"], index)
            self.assertEqual(caught.exception.code, "invalid_output_index")
        with self.assertRaises(GatewayError) as missing:
            self.gateway.content(result["id"], 2)
        self.assertEqual(missing.exception.status, 404)
        path, _ = self.gateway.content(result["id"], 1)
        path.unlink()
        with self.assertRaises(GatewayError) as expired:
            self.gateway.content(result["id"], 1)
        self.assertEqual(expired.exception.status, 410)
        record = self.gateway.get(result["id"])
        record["suffix"] = "/../../outside.jpg"
        self.gateway._save(record)
        with self.assertRaises(GatewayError) as unsafe:
            self.gateway.content(result["id"])
        self.assertEqual(unsafe.exception.status, 410)

    async def test_indexed_content_keeps_dispatcher_account_affinity_and_pool_owner(self):
        self.upstream.generate_image.return_value = image_response(2)
        dispatcher = VidsProviderDispatcher(SimpleNamespace(get_token=AsyncMock()))
        dispatcher.register(
            VidsAccount(1, self.gateway, hashlib.sha256(self.gateway.identity.encode()).hexdigest())
        )
        result = await self.gateway.generate({**BODY, "n": 2}, "dispatcher-image-index")
        path, mime = dispatcher.content(1, result["id"], 1)
        self.assertEqual(path.read_bytes(), JPEG + b"key-1")
        pool = VidsPoolDispatcher(dispatcher)
        self.assertEqual(pool.content(None, result["id"], 1), (path, mime))
        for invalid in (False, True, "1", -1):
            with self.assertRaises(GatewayError):
                dispatcher.content(1, result["id"], invalid)
        with self.assertRaises(GatewayError):
            dispatcher.content(2, result["id"], 1)


if __name__ == "__main__":
    unittest.main()
