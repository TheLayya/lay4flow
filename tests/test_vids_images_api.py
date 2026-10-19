"""Exercise standard Vids image responses using durable receipts and synthetic JPEGs."""

import asyncio
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from src.api.vids import create_vids_app
from src.services.vids.client import VidsProtocolError, VidsTransportError
from src.services.vids.gateway import VidsGateway
from tests.test_vids_gateway import KEY, client as mock_client

BODY = {"model": "vids-image", "prompt": "\n A blue boat. \t"}
JPEG = b"\xff\xd8\xff\xc0\x00\x0b\x08\x04\x00\x04\x00\x01\x01\x11\x00\xff\xd9"


def identifier(key):
    return "vids_" + hashlib.sha256(key.encode()).hexdigest()


def image_response(count):
    return [
        [None] * 11
        + [
            [None] * 6
            + [
                [
                    f"private-key-{index}",
                    f"https://lh3.googleusercontent.com/private-{index}",
                    1024,
                    1024,
                ]
            ]
        ]
        for index in range(count)
    ]


def image_bytes(index):
    return JPEG + bytes([index])


def configure_image_edit(upstream):
    upstream.model_chunk = {"revision": 4, "chunk": [[12, "private-scene", 0, 0, [16, 5000]]]}

    async def insert(path, *, mime_type, shape_id, scene_id, checkpoint):
        assert Path(path).read_bytes() == JPEG
        assert mime_type == "image/jpeg"
        assert scene_id == "private-scene"
        checkpoint(
            {
                "image": {"cosmo_id": "private-cosmo", "width": 1024, "height": 1024},
                "shape_id": shape_id,
                "scene_id": scene_id,
                "revision": 4,
            }
        )
        return {"shape_id": shape_id, "scene_id": scene_id, "revision": 5}

    upstream.insert_document_image = AsyncMock(side_effect=insert)
    upstream.edit_document_image = AsyncMock(return_value=upstream.generate_image.return_value)


class VidsImagesAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.upstream = mock_client()
        image = ["private-key", "https://lh3.googleusercontent.com/private-image", 1024, 1024]
        self.upstream.generate_image = AsyncMock(
            return_value=[[None] * 11 + [[None] * 6 + [image]]]
        )

        async def download(resource, path):
            Path(path).write_bytes(JPEG)

        self.upstream.download_image = AsyncMock(side_effect=download)
        self.gateway = VidsGateway(self.upstream, self.directory.name)
        await self.gateway.start()
        self.app = create_vids_app(self.gateway, KEY)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://vids.example.test",
            headers={"Authorization": "Bearer " + KEY},
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.gateway.close()
        self.directory.cleanup()

    async def image(self, key, body=None):
        return await self.http.post(
            "/v1/images/generations",
            json=BODY if body is None else body,
            headers={"Idempotency-Key": key},
        )

    async def test_multiple_images_return_every_url_and_bytes_with_one_durable_request(self):
        async def download(resource, path):
            index = int(resource["unique_key"].rsplit("-", 1)[1])
            Path(path).write_bytes(image_bytes(index))

        self.upstream.download_image.side_effect = download
        for count in (2, 3, 4):
            with self.subTest(n=count):
                self.upstream.generate_image.reset_mock()
                self.upstream.download_image.reset_mock()
                self.upstream.generate_image.return_value = image_response(count)
                key = f"multiple-images-{count}"
                body = {**BODY, "n": count}
                first = await self.image(key, body)
                self.assertEqual(first.status_code, 200, first.text)
                result = first.json()
                self.assertEqual(len(result["data"]), count)
                base = (
                    "https://vids.example.test/v1/vids/generations/" + identifier(key) + "/content"
                )
                for index, item in enumerate(result["data"]):
                    expected = base + (f"?index={index}" if index else "")
                    self.assertEqual(item["url"], expected)
                    content = await self.http.get(expected)
                    self.assertEqual(content.content, image_bytes(index))
                    self.assertEqual(content.headers["content-type"], "image/jpeg")
                    self.assertEqual(
                        (await self.http.get(expected, headers={"Authorization": ""})).status_code,
                        401,
                    )
                encoded = await self.image(key, {**body, "response_format": "b64_json"})
                self.assertEqual(encoded.status_code, 200, encoded.text)
                self.assertEqual(
                    [base64.b64decode(item["b64_json"]) for item in encoded.json()["data"]],
                    [image_bytes(index) for index in range(count)],
                )
                self.assertEqual((await self.image(key, body)).json(), result)
                generic = await self.http.post(
                    "/v1/vids/generations",
                    json={"kind": "image", "prompt": BODY["prompt"], "n": count},
                    headers={"Idempotency-Key": key},
                )
                self.assertEqual(generic.json()["status"], "completed", generic.text)
                self.assertEqual(
                    [output["index"] for output in generic.json()["outputs"]], list(range(count))
                )
                self.assertEqual(
                    [
                        "https://vids.example.test" + output["content_url"]
                        for output in generic.json()["outputs"]
                    ],
                    [item["url"] for item in result["data"]],
                )
                conflict = await self.image(key, {**body, "n": 1})
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
                self.upstream.generate_image.assert_awaited_once_with(BODY["prompt"], count=count)
                self.assertEqual(self.upstream.download_image.await_count, count)
                for index, status in ((-1, 400), (count, 400 if count == 4 else 404)):
                    invalid = await self.http.get(base, params={"index": index})
                    self.assertEqual(invalid.status_code, status, invalid.text)
        await self.gateway.close()
        await self.gateway.start()
        self.assertEqual((await self.image(key, body)).json(), result)
        self.upstream.generate_image.assert_awaited_once()
        self.assertEqual(self.upstream.download_image.await_count, count)

    async def test_multiple_image_unknown_replays_without_another_submission(self):
        self.upstream.generate_image.side_effect = VidsTransportError("private upstream URL")
        body = {**BODY, "n": 4}
        for response_format in ("url", "b64_json"):
            response = await self.image(
                "unknown-four-images", {**body, "response_format": response_format}
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "image_not_ready")
        receipt = self.gateway.get(identifier("unknown-four-images"))
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["image_count"], 4)
        await self.gateway.close()
        await self.gateway.start()
        self.assertEqual((await self.image("unknown-four-images", body)).status_code, 409)
        self.upstream.generate_image.assert_awaited_once_with(BODY["prompt"], count=4)
        self.upstream.download_image.assert_not_awaited()

    async def test_multiple_images_validate_each_downloaded_output_and_response_count(self):
        self.upstream.generate_image.return_value = image_response(3)
        wrong_count = await self.image("wrong-image-count", {**BODY, "n": 4})
        self.assertEqual(wrong_count.status_code, 502, wrong_count.text)
        self.assertEqual(
            self.gateway.get(identifier("wrong-image-count"))["error"], "upstream_image_count"
        )
        self.upstream.download_image.assert_not_awaited()
        self.upstream.generate_image.return_value = image_response(4)
        for index in range(4):
            with self.subTest(index=index):
                key = f"validate-output-{index}"
                body = {**BODY, "n": 4}
                self.assertEqual((await self.image(key, body)).status_code, 200)
                path, _ = self.gateway.content(identifier(key), index)
                path.write_bytes(JPEG[:7] + b"\x02\x00\x02\x00" + JPEG[11:])
                mismatch = await self.image(key, {**body, "size": "1024x1024"})
                self.assertEqual(mismatch.status_code, 502, mismatch.text)
                self.assertEqual(mismatch.json()["error"]["code"], "image_specification_mismatch")
                path.write_bytes(b"\xff\xd8\xffmalformed-private")
                invalid = await self.image(key, {**body, "response_format": "b64_json"})
                self.assertEqual(invalid.status_code, 502, invalid.text)
                self.assertEqual(invalid.json()["error"]["code"], "invalid_image_content")
                self.assertNotIn("private", invalid.text)
                path.unlink()
                expired = await self.image(key, body)
                self.assertEqual(expired.status_code, 410, expired.text)
        self.assertEqual(self.upstream.generate_image.await_count, 5)
        self.assertEqual(self.upstream.download_image.await_count, 16)

    async def test_multiple_image_download_failure_recovers_each_saved_output(self):
        self.upstream.generate_image.return_value = image_response(4)

        async def download(resource, path):
            index = int(resource["unique_key"].rsplit("-", 1)[1])
            if index == 2:
                raise VidsTransportError("private download URL")
            Path(path).write_bytes(image_bytes(index))

        self.upstream.download_image.side_effect = download
        body = {**BODY, "n": 4}
        first = await self.image("failed-third-output", body)
        self.assertEqual(first.status_code, 502, first.text)
        self.assertEqual(
            self.gateway.get(identifier("failed-third-output"))["status"], "download_failed"
        )
        self.assertEqual((await self.image("failed-third-output", body)).status_code, 502)
        self.upstream.generate_image.assert_awaited_once()

        async def recovered_download(resource, path):
            index = int(resource["unique_key"].rsplit("-", 1)[1])
            Path(path).write_bytes(image_bytes(index))

        self.upstream.download_image.side_effect = recovered_download
        recovery = await self.http.post(
            "/v1/vids/generations/" + identifier("failed-third-output") + "/download"
        )
        self.assertEqual(recovery.json()["status"], "completed", recovery.text)
        final = await self.image("failed-third-output", {**body, "response_format": "b64_json"})
        self.assertEqual(len(final.json()["data"]), 4)
        self.upstream.generate_image.assert_awaited_once()

    async def test_file_edit_and_raw_json_share_bytes_receipt_without_source_discovery(self):
        configure_image_edit(self.upstream)
        key = "uploaded-image-edit"
        for field in ("image", "image[]"):
            response = await self.http.post(
                "/v1/images/edits",
                data={**BODY, "n": "1", "response_format": "b64_json", "size": "1024x1024"},
                files={field: ("renamed-" + field + ".jpg", JPEG, "application/octet-stream")},
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(base64.b64decode(response.json()["data"][0]["b64_json"]), JPEG)
            self.assertNotIn("private", response.text)
        generic = await self.http.post(
            "/v1/vids/generations",
            json={
                "kind": "image",
                "mode": "edit",
                "prompt": BODY["prompt"],
                "image_base64": base64.b64encode(JPEG).decode(),
            },
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(generic.json()["status"], "completed", generic.text)
        self.upstream.insert_document_image.assert_awaited_once()
        shape = self.upstream.insert_document_image.await_args.kwargs["shape_id"]
        self.upstream.edit_document_image.assert_awaited_once_with(shape, BODY["prompt"])
        self.upstream.generate_image.assert_not_awaited()
        self.upstream.download_image.assert_awaited_once()
        changed = await self.http.post(
            "/v1/images/edits",
            data={**BODY, "prompt": "Different edit"},
            files={"image": ("source.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["error"]["code"], "idempotency_conflict")
        await self.gateway.close()
        await self.gateway.start()
        replay = await self.http.post(
            "/v1/images/edits",
            data=BODY,
            files={"image": ("source.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual((await self.http.get(replay.json()["data"][0]["url"])).content, JPEG)
        self.upstream.insert_document_image.assert_awaited_once()
        self.upstream.edit_document_image.assert_awaited_once()

    async def test_file_edit_rejects_invalid_inputs_before_upload_or_generation(self):
        configure_image_edit(self.upstream)
        valid = [("image", ("source.jpg", JPEG, "image/jpeg"))]
        cases = [
            ({**BODY, "n": "2"}, valid, 400),
            ({**BODY, "size": "landscape"}, valid, 400),
            ({**BODY, "model": "gpt-image-1"}, valid, 400),
            ({**BODY, "prompt": " "}, valid, 400),
            ({**BODY, "source_id": "private"}, valid, 400),
            (BODY, [("image", "text instead of a file")], 400),
            (BODY, [("image", ("bad.jpg", b"invalid", "image/jpeg"))], 400),
            (BODY, valid + [("image[]", ("second.jpg", JPEG, "image/jpeg"))], 400),
            (BODY, [("image", ("large.jpg", JPEG + b"x" * (10 * 1024**2), "image/jpeg"))], 413),
            ({"model": "vids-image"}, valid, 400),
        ]
        for fields, files, status in cases:
            response = await self.http.post(
                "/v1/images/edits",
                data=fields,
                files=files,
                headers={"Idempotency-Key": "invalid-edit"},
            )
            self.assertEqual(response.status_code, status, response.text)
        duplicate = await self.http.post(
            "/v1/images/edits",
            files=[
                *valid,
                ("model", (None, "vids-image")),
                ("prompt", (None, "one")),
                ("prompt", (None, "two")),
            ],
            headers={"Idempotency-Key": "duplicate-edit"},
        )
        self.assertEqual(duplicate.status_code, 400)
        missing = await self.http.post(
            "/v1/images/edits", files={"model": (None, "vids-image"), "prompt": (None, "edit")}
        )
        self.assertEqual(missing.status_code, 400)
        wrong_type = await self.http.post("/v1/images/edits", json=BODY)
        self.assertEqual(wrong_type.status_code, 415)
        for headers, status in (({}, 400), ({"Authorization": ""}, 401)):
            denied = await self.http.post(
                "/v1/images/edits", data=BODY, files=valid, headers=headers
            )
            self.assertEqual(denied.status_code, status, denied.text)
        self.upstream.insert_document_image.assert_not_awaited()
        self.upstream.edit_document_image.assert_not_awaited()
        self.assertEqual(list(self.gateway.root.glob("vids_*.json")), [])

    async def test_two_formats_generic_route_and_restart_share_one_receipt(self):
        for generic_first in (False, True):
            with self.subTest(generic_first=generic_first):
                self.upstream.generate_image.reset_mock()
                self.upstream.download_image.reset_mock()
                key = "image-cross-route-" + str(generic_first)
                generic = {"kind": "image", "prompt": BODY["prompt"], "mode": "text"}
                if generic_first:
                    receipt = await self.http.post(
                        "/v1/vids/generations", json=generic, headers={"Idempotency-Key": key}
                    )
                    self.assertEqual(receipt.json()["status"], "completed")
                url_response = await self.image(key)
                self.assertEqual(url_response.status_code, 200, url_response.text)
                result = url_response.json()
                url = result["data"][0]["url"]
                self.assertEqual(
                    url,
                    "https://vids.example.test/v1/vids/generations/" + identifier(key) + "/content",
                )
                self.assertTrue(url_response.headers["x-request-id"].startswith("req_"))
                self.assertNotIn("private", url_response.text)
                self.assertNotIn("google", url_response.text)
                content = await self.http.get(url)
                self.assertEqual(content.content, JPEG)
                self.assertEqual(content.headers["content-type"], "image/jpeg")
                anonymous = await self.http.get(url, headers={"Authorization": ""})
                self.assertEqual(anonymous.status_code, 401)
                encoded = await self.image(
                    key, {**BODY, "response_format": "b64_json", "size": "1024x1024", "n": 1}
                )
                self.assertEqual(encoded.status_code, 200, encoded.text)
                self.assertEqual(encoded.json()["created"], result["created"])
                self.assertEqual(base64.b64decode(encoded.json()["data"][0]["b64_json"]), JPEG)
                receipt = await self.http.post(
                    "/v1/vids/generations", json=generic, headers={"Idempotency-Key": key}
                )
                self.assertEqual(receipt.json()["status"], "completed")
                self.assertEqual(receipt.json()["created_at"], result["created"])
                self.upstream.generate_image.assert_awaited_once_with(BODY["prompt"])
                self.upstream.download_image.assert_awaited_once()
                conflict = await self.image(key, {**BODY, "prompt": "Different"})
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
                self.upstream.generate_image.assert_awaited_once()
        await self.gateway.close()
        await self.gateway.start()
        self.assertEqual((await self.image(key)).json(), result)
        self.upstream.generate_image.assert_awaited_once()
        catalog = (await self.http.get("/v1/models")).json()["data"]
        image = next(row for row in catalog if row["id"] == "vids-image")
        self.assertIn("/v1/images/generations", image["supported_endpoints"])
        self.assertEqual(image["capabilities"]["image_response_formats"], ["url", "b64_json"])
        self.assertEqual(
            image["capabilities"]["image_url_authentication"], "Bearer API key required"
        )
        schema = self.app.openapi()["paths"]["/v1/images/generations"]["post"]["requestBody"]
        fields = schema["content"]["application/json"]["schema"]
        self.assertFalse(fields["additionalProperties"])
        self.assertEqual(
            set(fields["properties"]),
            {"model", "prompt", "n", "size", "response_format", "aspect_ratio", "style"},
        )

    async def test_strict_options_auth_body_and_key_fail_without_generation(self):
        invalid = [
            {field: value}
            for field, values in (
                ("model", (None, "gpt-image-1", [], 32)),
                ("prompt", (None, "", " \t\n", 42, "x" * 32001)),
                ("n", (None, True, 1.0, "1", 5)),
                ("size", (None, "512x512", "landscape", [])),
                ("response_format", (None, "jpeg", "URL", {})),
            )
            for value in values
        ]
        invalid += [
            {field: "private"}
            for field in (
                "quality",
                "generationConfig",
                "background",
                "user",
                "kind",
                "mode",
                "private-cookie",
            )
        ]
        invalid += [{"model": None}, {"prompt": None}]
        for changes in invalid:
            with self.subTest(changes=changes):
                response = await self.image("invalid-image", {**BODY, **changes})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("private", response.text)
        for field in BODY:
            response = await self.image(
                "missing-image", {k: v for k, v in BODY.items() if k != field}
            )
            self.assertEqual(response.status_code, 400)
        for headers in ({}, {"Idempotency-Key": ""}, {"Idempotency-Key": "x" * 201}):
            response = await self.http.post("/v1/images/generations", json=BODY, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "idempotency_required")
        for authorization in ("", "Bearer private-invalid"):
            response = await self.http.post(
                "/v1/images/generations",
                json=BODY,
                headers={"Idempotency-Key": "unauthorized", "Authorization": authorization},
            )
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("private", response.text)
        for content in (b"not-json", b"[]", b"null"):
            response = await self.http.post(
                "/v1/images/generations",
                content=content,
                headers={"Idempotency-Key": "invalid-json", "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 400)
        response = await self.http.post(
            "/v1/images/generations",
            content=b"{}",
            headers={"Idempotency-Key": "wrong-content-type", "Content-Type": "text/plain"},
        )
        self.assertEqual(response.status_code, 415)
        response = await self.http.post(
            "/v1/images/generations",
            content=b" " * (1024**2 + 1),
            headers={"Idempotency-Key": "large-image-json", "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        self.upstream.generate_image.assert_not_awaited()
        self.upstream.download_image.assert_not_awaited()
        self.assertEqual(list(self.gateway.root.glob("vids_*.json")), [])

    async def test_unknown_and_failed_acceptance_remain_replayable_without_submission(self):
        for index, error in enumerate(
            (
                VidsTransportError("private-url"),
                VidsProtocolError("private-generate", 503),
                VidsProtocolError("private-generate", 400),
            )
        ):
            with self.subTest(error=error):
                self.upstream.generate_image.reset_mock()
                self.upstream.generate_image.side_effect = error
                key = "unknown-image-" + str(index)
                expected_status = "failed" if index == 2 else "unknown"
                for response_format in ("url", "b64_json"):
                    response = await self.image(key, {**BODY, "response_format": response_format})
                    self.assertEqual(response.status_code, 502 if index == 2 else 409)
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "image_failed" if index == 2 else "image_not_ready",
                    )
                    self.assertIn(identifier(key), response.json()["error"]["message"])
                    self.assertNotIn("private", response.text)
                receipt = await self.http.get("/v1/vids/generations/" + identifier(key))
                self.assertEqual(receipt.json()["status"], expected_status)
                self.assertIsNone(receipt.json()["content_url"])
                recovery = await self.http.post(
                    "/v1/vids/generations/" + identifier(key) + "/download"
                )
                self.assertEqual(recovery.status_code, 409)
                self.upstream.generate_image.assert_awaited_once()
        self.upstream.generate_image.side_effect = None
        await self.gateway.close()
        await self.gateway.start()
        self.assertEqual((await self.image("unknown-image-0")).status_code, 409)
        self.upstream.generate_image.assert_awaited_once()
        self.upstream.download_image.assert_not_awaited()

    async def test_no_media_and_download_failure_recover_only_saved_result(self):
        self.upstream.generate_image.return_value = []
        response = await self.image("no-image")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.gateway.get(identifier("no-image"))["status"], "no_result")
        self.assertEqual((await self.image("no-image")).status_code, 502)
        self.upstream.generate_image.assert_awaited_once()
        self.upstream.download_image.assert_not_awaited()

        image = ["private-key", "https://lh3.googleusercontent.com/private-image", 1024, 1024]
        self.upstream.generate_image.return_value = [[None] * 11 + [[None] * 6 + [image]]]
        self.upstream.generate_image.reset_mock()
        download = self.upstream.download_image.side_effect
        self.upstream.download_image.side_effect = VidsTransportError("private-url")
        response = await self.image("download-image")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.text)
        receipt = self.gateway.get(identifier("download-image"))
        self.assertEqual(receipt["status"], "download_failed")
        self.assertEqual(receipt["response"], self.upstream.generate_image.return_value)
        self.assertEqual((await self.image("download-image")).status_code, 502)
        self.upstream.generate_image.assert_awaited_once()
        self.upstream.download_image.assert_awaited_once()
        self.upstream.download_image.side_effect = download
        recovery = await self.http.post(
            "/v1/vids/generations/" + identifier("download-image") + "/download"
        )
        self.assertEqual(recovery.json()["status"], "completed")
        self.assertEqual((await self.image("download-image")).status_code, 200)
        self.upstream.generate_image.assert_awaited_once()
        self.assertEqual(self.upstream.download_image.await_count, 2)

    async def test_actual_bytes_validate_requested_dimensions_and_expired_content(self):
        response = await self.image("content-image")
        self.assertEqual(response.status_code, 200)
        path, _ = self.gateway.content(identifier("content-image"))
        path.write_bytes(JPEG[:7] + b"\x02\x00\x02\x00" + JPEG[11:])
        mismatch = await self.image("content-image", {**BODY, "size": "1024x1024"})
        self.assertEqual(mismatch.status_code, 502)
        self.assertEqual(mismatch.json()["error"]["code"], "image_specification_mismatch")
        path.write_bytes(b"\xff\xd8\xffmalformed-private")
        invalid = await self.image("content-image", {**BODY, "response_format": "b64_json"})
        self.assertEqual(invalid.status_code, 502)
        self.assertEqual(invalid.json()["error"]["code"], "invalid_image_content")
        self.assertNotIn("private", invalid.text)
        path.unlink()
        for response_format in ("url", "b64_json"):
            expired = await self.image(
                "content-image", {**BODY, "response_format": response_format}
            )
            self.assertEqual(expired.status_code, 410)
            self.assertEqual(expired.json()["error"]["code"], "expired")
        self.upstream.generate_image.assert_awaited_once()
        self.upstream.download_image.assert_awaited_once()

    async def test_duplicate_inflight_receipt_and_account_lock_do_not_resubmit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        response = self.upstream.generate_image.return_value

        async def submit(prompt):
            entered.set()
            await release.wait()
            return response

        self.upstream.generate_image.side_effect = submit
        pending = asyncio.create_task(self.image("pending-image"))
        await entered.wait()
        try:
            duplicate = await self.image("pending-image", {**BODY, "response_format": "b64_json"})
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(duplicate.json()["error"]["code"], "image_not_ready")
            busy = await self.image("other-image")
            self.assertEqual(busy.status_code, 429)
            self.assertEqual(busy.json()["error"]["code"], "busy")
        finally:
            release.set()
        self.assertEqual((await pending).status_code, 200)
        self.upstream.generate_image.assert_awaited_once()

    @unittest.skipUnless(AsyncOpenAI, "Optional OpenAI SDK is not installed")
    async def test_sdk_edit_upload_returns_standard_image_result(self):
        configure_image_edit(self.upstream)
        sdk_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        async with AsyncOpenAI(
            api_key=KEY,
            base_url="https://vids.example.test/v1",
            max_retries=0,
            http_client=sdk_http,
        ) as sdk:
            result = await sdk.images.edit(
                model="vids-image",
                prompt=BODY["prompt"],
                image=("source.jpg", JPEG, "image/jpeg"),
                n=1,
                response_format="b64_json",
                extra_headers={"Idempotency-Key": "sdk-image-edit"},
            )
            self.assertEqual(base64.b64decode(result.data[0].b64_json), JPEG)
        self.upstream.insert_document_image.assert_awaited_once()
        self.upstream.edit_document_image.assert_awaited_once()
        self.upstream.generate_image.assert_not_awaited()

    @unittest.skipUnless(AsyncOpenAI, "Optional OpenAI SDK is not installed")
    async def test_sdk_b64_and_default_url_replay_without_generation(self):
        self.upstream.generate_image.return_value = image_response(4)
        sdk_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        async with AsyncOpenAI(
            api_key=KEY,
            base_url="https://vids.example.test/v1",
            max_retries=0,
            http_client=sdk_http,
        ) as sdk:
            result = await sdk.images.generate(
                model="vids-image",
                prompt=BODY["prompt"],
                n=4,
                size="1024x1024",
                response_format="b64_json",
                extra_headers={"Idempotency-Key": "sdk-image"},
            )
            self.assertEqual(len(result.data), 4)
            self.assertTrue(all(base64.b64decode(item.b64_json) == JPEG for item in result.data))
            url = await sdk.images.generate(
                model="vids-image",
                prompt=BODY["prompt"],
                n=4,
                extra_headers={"Idempotency-Key": "sdk-image"},
            )
            self.assertEqual(url.created, result.created)
            self.assertEqual(len(url.data), 4)
            for item in url.data:
                self.assertEqual((await self.http.get(item.url)).content, JPEG)
        self.upstream.generate_image.assert_awaited_once_with(BODY["prompt"], count=4)
        self.assertEqual(self.upstream.download_image.await_count, 4)


if __name__ == "__main__":
    unittest.main()
