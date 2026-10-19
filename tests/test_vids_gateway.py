import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.api.vids import create_vids_app
from src.services.vids.client import (
    DocumentImageSourceError,
    VidsClient,
    VidsProtocolError,
    VidsTransportError,
    cookie_jar,
)
from src.services.vids.gateway import GatewayError, VidsGateway
from tests.test_vids_video_input import mp4

KEY = "synthetic-vids-gateway-api-key"


def client():
    audio = [None, "audio/wav", None, "https://contribution-rt.usercontent.google.com/synthetic"]
    response = [[None] * 29 + [[audio]]]

    async def download(resource, path):
        Path(path).write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"synthetic")
        return Path(path)

    value = SimpleNamespace(
        flags={"docs-hue": "same-account", "flix-evga": 1, "flix-evg1p2ui": 1},
        editor_url="https://docs.google.com/videos/d/doc/edit",
        auth_user=0,
        proxy=None,
        bootstrap=AsyncMock(),
        quota=AsyncMock(return_value=[[None, ["20", None, "19"], "1"]]),
        generate_speech=AsyncMock(return_value=response),
        download_audio=AsyncMock(side_effect=download),
        session=SimpleNamespace(close=AsyncMock(), cookies=SimpleNamespace(jar=cookie_jar([]))),
    )
    return value


class VidsGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_start_cleanup_is_idempotent_and_preserves_authentication_failure(self):
        candidate = client()
        candidate.bootstrap.side_effect = VidsProtocolError("bootstrap", 401)
        candidate.session.close.side_effect = [None, TypeError("second close is invalid")]
        broken = VidsGateway(candidate, Path(self.directory.name) / "failed-start")
        with self.assertRaises(VidsProtocolError) as caught:
            await broken.start()
        self.assertEqual(caught.exception.status, 401)
        await broken.close()
        await broken.close()
        candidate.session.close.assert_awaited_once()
        self.assertIsNone(broken._lock_file)
        self.assertFalse(broken._ready)
        # A new gateway can own the released directory; the old closed HTTP
        # session is never silently reused by another start call.
        recovered = VidsGateway(client(), broken.root)
        try:
            await recovered.start()
        finally:
            await recovered.close()
        with self.assertRaises(GatewayError) as closed:
            await broken.start()
        self.assertEqual(closed.exception.code, "session_unavailable")

    async def test_document_images_refreshes_snapshot_and_exposes_only_saved_shape_identifiers(
        self,
    ):
        self.client.model_chunk = None
        self.client.edit_document_image = AsyncMock()
        self.client.generate_image = AsyncMock()

        async def snapshot():
            self.client.model_chunk = {
                "revision": 3,
                "chunk": [
                    [3, "image-shape", 3, [1, 0, 0, 1, 0, 0], [49, "private-cosmo"], "scene"],
                    [3, "non-image", 108, [1, 0, 0, 1, 0, 0], [49, "private-cosmo"], "scene"],
                    [9, [], [49, "https://private-signed-resource"]],
                ],
            }

        self.client.bootstrap.side_effect = snapshot
        unauthorized = await self.http.get(
            "/v1/vids/document/images", headers={"Authorization": "Bearer invalid"}
        )
        self.assertEqual(unauthorized.status_code, 401)
        self.client.bootstrap.reset_mock()
        response = await self.http.get("/v1/vids/document/images")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "object": "list",
                "revision": 3,
                "data": [{"shape_id": "image-shape", "scene_id": "scene"}],
            },
        )
        self.assertNotIn("private", response.text)
        self.assertNotIn("cosmo", response.text)
        self.client.bootstrap.assert_awaited_once()
        self.client.edit_document_image.assert_not_awaited()
        self.client.generate_image.assert_not_awaited()
        self.client.generate_speech.assert_not_awaited()
        self.assertEqual(list(Path(self.directory.name).glob("vids_*.json")), [])
        catalog = (await self.http.get("/v1/models")).json()["data"]
        capability = next(model["capabilities"] for model in catalog if model["kind"] == "image")
        self.assertEqual(capability["edit_source_endpoint"], "/v1/vids/document/images")
        self.assertEqual(capability["count"], 1)

    async def test_document_images_missing_snapshot_or_refreshed_identity_fails_without_generation(
        self,
    ):
        self.client.model_chunk = None
        self.client.generate_image = AsyncMock()
        response = await self.http.get("/v1/vids/document/images")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "unverified_document")

        async def drift():
            self.client.flags["docs-hue"] = "private-other-account"
            self.client.model_chunk = {"revision": 3, "chunk": []}

        self.client.bootstrap.side_effect = drift
        response = await self.http.get("/v1/vids/document/images")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "session_persistence_failed")
        self.assertNotIn("private", response.text)
        self.client.generate_image.assert_not_awaited()
        self.client.generate_speech.assert_not_awaited()

    async def test_image_edit_uses_saved_shape_once_and_preserves_private_receipt(self):
        image = ["key", "https://lh3.googleusercontent.com/private-image", 1024, 1024]
        self.client.edit_document_image = AsyncMock(
            return_value=[[None] * 11 + [[None] * 6 + [image]]]
        )
        self.client.generate_image = AsyncMock()

        async def download(resource, path):
            Path(path).write_bytes(b"\xff\xd8\xffsynthetic")

        self.client.download_image = AsyncMock(side_effect=download)
        body = {
            "kind": "image",
            "mode": "edit",
            "shape_id": "private-shape",
            "prompt": "Make it green",
        }
        headers = {"Idempotency-Key": "image-edit-key"}
        first = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        second = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["status"], "completed")
        self.assertNotIn("private", first.text)
        self.client.edit_document_image.assert_awaited_once_with("private-shape", "Make it green")
        self.client.generate_image.assert_not_awaited()
        content = await self.http.get(first.json()["content_url"])
        self.assertEqual(content.headers["content-type"], "image/jpeg")
        self.assertTrue(content.content.startswith(b"\xff\xd8\xff"))
        conflict = await self.http.post(
            "/v1/vids/generations", json={**body, "shape_id": "other-shape"}, headers=headers
        )
        self.assertEqual(conflict.status_code, 409)
        self.client.edit_document_image.assert_awaited_once()

    async def test_image_modes_keep_old_text_fingerprint_and_reject_invalid_sources(self):
        legacy = {"kind": "image", "prompt": "A landscape"}
        self.assertEqual(self.gateway.normalize(legacy), legacy)
        self.assertEqual(self.gateway.normalize({**legacy, "mode": "text"}), legacy)
        self.client.generate_image = AsyncMock(return_value=[])
        self.client.edit_document_image = AsyncMock()
        for body in (
            {**legacy, "mode": "unknown"},
            {**legacy, "mode": []},
            {**legacy, "mode": "edit"},
            {**legacy, "mode": "edit", "shape_id": ""},
            {**legacy, "mode": "edit", "shape_id": 3},
            {**legacy, "mode": "edit", "shape_id": "private shape"},
            {**legacy, "mode": "edit", "shape_id": "private\0shape"},
            {**legacy, "mode": "edit", "shape_id": "x" * 4097},
            {**legacy, "shape_id": "private-shape"},
            {**legacy, "mode": "edit", "shape_id": "private-shape", "image_base64": "private"},
            {"kind": "video", "prompt": "Move", "shape_id": "private-shape"},
        ):
            response = await self.http.post(
                "/v1/vids/generations", json=body, headers={"Idempotency-Key": "invalid-image-edit"}
            )
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("private", response.text)
        self.client.edit_document_image.assert_not_awaited()
        self.client.generate_image.assert_not_awaited()
        self.assertEqual(list(Path(self.directory.name).glob("vids_*.json")), [])
        first = await self.gateway.generate(legacy, "image-text-key")
        second = await self.gateway.generate({**legacy, "mode": "text"}, "image-text-key")
        self.assertEqual(first, second)
        self.client.generate_image.assert_awaited_once_with("A landscape")

    async def test_unknown_image_edit_is_not_repeated_and_download_recovers_without_edit(self):
        body = {"kind": "image", "mode": "edit", "shape_id": "shape", "prompt": "Green"}
        self.client.edit_document_image = AsyncMock(side_effect=VidsTransportError("private-url"))
        record = await self.gateway.generate(body, "unknown-image-edit")
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(await self.gateway.generate(body, "unknown-image-edit"), record)
        self.client.edit_document_image.assert_awaited_once()
        image = ["key", "https://lh3.googleusercontent.com/private-image", 1024, 1024]
        self.client.edit_document_image.side_effect = None
        self.client.edit_document_image.return_value = [[None] * 11 + [[None] * 6 + [image]]]
        self.client.download_image = AsyncMock(side_effect=VidsTransportError("private-url"))
        record = await self.gateway.generate(body, "download-image-edit")
        self.assertEqual(record["status"], "download_failed")

        async def download(resource, path):
            Path(path).write_bytes(b"\xff\xd8\xffsynthetic")

        self.client.download_image.side_effect = download
        recovered = await self.gateway.retry_download(record["id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.client.edit_document_image.await_count, 2)

    async def test_missing_document_image_is_failed_before_paid_submission_and_replayed(self):
        body = {"kind": "image", "mode": "edit", "shape_id": "private-shape", "prompt": "Green"}
        self.client.edit_document_image = AsyncMock(side_effect=DocumentImageSourceError("private"))
        self.client.generate_image = AsyncMock()
        self.client.download_image = AsyncMock()
        headers = {"Idempotency-Key": "missing-document-image"}
        first = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "failed")
        self.assertEqual(first.json()["error"], "invalid_source")
        self.assertNotIn("private", first.text)
        replay = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        self.assertEqual(replay.json(), first.json())
        self.client.edit_document_image.assert_awaited_once()
        self.client.generate_image.assert_not_awaited()
        self.client.download_image.assert_not_awaited()

    async def test_mutated_document_image_fails_before_generation_and_replays_failure(self):
        self.client.document_session_id = "LOAD_CONTEXT"
        self.client.settings = AsyncMock()
        self.client.document_image_url = AsyncMock()
        self.client._request = AsyncMock()
        self.client.edit_document_image = VidsClient.edit_document_image.__get__(self.client)
        body = {"kind": "image", "mode": "edit", "shape_id": "deleted-shape", "prompt": "Green"}
        for mutation in (
            [0, ["deleted-shape"]],
            [5, ["deleted-shape"], [], [49, "replacement"]],
            [59, "media-clip"],
        ):
            with self.subTest(operation=mutation[0]):
                self.client.model_chunk = {
                    "revision": 4,
                    "chunk": [
                        [3, "deleted-shape", 3, [1, 0, 0, 1, 0, 0], [49, "private-cosmo"], "scene"],
                        mutation,
                    ],
                }
                key = "mutated-document-image-" + str(mutation[0])
                first = await self.gateway.generate(body, key)
                self.assertEqual(first["status"], "failed")
                self.assertEqual(first["error"], "invalid_source")
                self.assertNotIn("private", json.dumps(first))
                self.assertEqual(await self.gateway.generate(body, key), first)
        self.assertEqual(self.client.settings.await_count, 3)
        self.client.document_image_url.assert_not_awaited()
        self.client._request.assert_not_awaited()

    async def test_image_edit_bootstrap_identity_drift_blocks_paid_post_without_persistence(self):
        self.assertIsNone(self.gateway.session_store)
        self.client._send = AsyncMock()
        self.client._headers = lambda: {}

        async def edit(shape_id, prompt):
            await self.client.bootstrap()
            return await VidsClient._request(self.client, "POST", "/v1/genai/generate", [])

        async def drift():
            self.client.flags["docs-hue"] = "private-other-account"

        self.client.bootstrap.side_effect = drift
        self.client.edit_document_image = AsyncMock(side_effect=edit)
        body = {"kind": "image", "mode": "edit", "shape_id": "shape", "prompt": "Green"}
        record = await self.gateway.generate(body, "identity-drift-image")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "session_persistence_failed")
        self.assertNotIn("private", json.dumps(record))
        self.assertEqual(await self.gateway.generate(body, "identity-drift-image"), record)
        self.client.edit_document_image.assert_awaited_once()
        self.client._send.assert_not_awaited()

    async def test_profile_refresh_is_explicit_cookie_only_and_rejects_request_configuration(self):
        disabled = await self.http.post("/v1/vids/session/refresh", json={})
        self.assertEqual(disabled.status_code, 503)
        rows = [{"name": "SAPISID", "value": "private-cookie", "domain": ".google.com"}]
        self.gateway.cookie_source = AsyncMock(return_value=rows)
        for body in ({"root": "private-profile"}, {"cookies": rows}, {"auth_user": 1}):
            response = await self.http.post("/v1/vids/session/refresh", json=body)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("private", response.text)
        self.gateway.cookie_source.assert_not_awaited()
        candidate = client()
        with patch("src.services.vids.gateway.VidsClient", return_value=candidate) as factory:
            response = await self.http.post("/v1/vids/session/refresh", json={})
        self.assertEqual(response.json(), {"updated": True, "transport": "pure_http"})
        self.gateway.cookie_source.assert_awaited_once_with(
            self.client.editor_url, self.client.proxy
        )
        factory.assert_called_once_with(
            self.client.editor_url, rows, auth_user=self.client.auth_user, proxy=self.client.proxy
        )
        candidate.bootstrap.assert_awaited_once()
        candidate.quota.assert_awaited_once()
        candidate.generate_speech.assert_not_awaited()
        self.assertEqual(self.client.session.close.await_count, 1)
        self.assertEqual(list(Path(self.directory.name).glob("vids_*.json")), [])

    async def test_failed_profile_candidates_preserve_old_session_and_release_lock(self):
        rows = [{"name": "SAPISID", "value": "private-cookie", "domain": ".google.com"}]
        self.gateway.cookie_source = AsyncMock(return_value=rows)
        for failure in ("bootstrap", "quota", "identity", "cancel"):
            candidate = client()
            if failure == "bootstrap":
                candidate.bootstrap.side_effect = VidsTransportError("private-url")
            elif failure == "quota":
                candidate.quota.side_effect = VidsProtocolError("quota", 401)
            elif failure == "identity":
                candidate.flags.pop("docs-hue")
            else:
                candidate.bootstrap.side_effect = asyncio.CancelledError()
            with patch("src.services.vids.gateway.VidsClient", return_value=candidate):
                with self.assertRaises(
                    (VidsTransportError, VidsProtocolError, GatewayError, asyncio.CancelledError)
                ):
                    await self.gateway.refresh_cookies()
            self.assertIs(self.gateway.client, self.client)
            self.client.session.close.assert_not_awaited()
            candidate.session.close.assert_awaited_once()
            self.assertFalse(self.gateway.lock.locked())
        self.gateway.cookie_source.side_effect = RuntimeError("private-cookie private-profile")
        response = await self.http.post("/v1/vids/session/refresh", json={})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.text)
        self.assertIs(self.gateway.client, self.client)

    async def test_profile_refresh_cannot_run_during_generation_or_retry_failed_submission(self):
        self.gateway.cookie_source = AsyncMock(return_value=[{"name": "SAPISID"}])
        entered, release = asyncio.Event(), asyncio.Event()

        async def submit(prompt, **options):
            entered.set()
            await release.wait()
            raise VidsTransportError("private")

        self.client.generate_speech.side_effect = submit
        body = {"kind": "speech", "prompt": "Hello"}
        pending = asyncio.create_task(self.gateway.generate(body, "profile-generation-key"))
        await entered.wait()
        refresh = asyncio.create_task(self.gateway.refresh_cookies())
        await asyncio.sleep(0)
        self.gateway.cookie_source.assert_not_awaited()
        refresh.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await refresh
        release.set()
        self.assertEqual((await pending)["status"], "unknown")
        self.assertEqual(
            (await self.gateway.generate(body, "profile-generation-key"))["status"], "unknown"
        )
        self.client.generate_speech.assert_awaited_once()
        self.gateway.cookie_source.assert_not_awaited()

    async def test_edit_with_reference_images_uses_existing_video_and_single_submission(self):
        source_id = "vids_" + "a" * 64
        self.gateway._save(
            {
                "id": source_id,
                "status": "completed",
                "kind": "video",
                "resource": {
                    "corpus": "co",
                    "lookup_schema": "sc",
                    "unique_key": "uk",
                    "duration": 3.008,
                    "width": 1280,
                    "height": 720,
                },
            }
        )
        self.client.upload_temporary = AsyncMock(return_value="reference-token")
        self.client.submit_video = AsyncMock(return_value=[])
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()
        result = await self.gateway.generate(
            {
                "kind": "video",
                "mode": "edit",
                "source_id": source_id,
                "prompt": "Add reference",
                "images_base64": [encoded],
            },
            "edit-reference-key",
        )
        self.assertEqual(result["status"], "no_result")
        self.client.submit_video.assert_awaited_once_with(
            self.gateway.get(source_id)["resource"],
            "Add reference",
            mode="edit",
            identifiers=["reference-token"],
            seconds=3,
            resolution="720p",
            orientation="landscape",
        )
        source = self.gateway.get(source_id)
        for changed in (
            {"width": 360, "height": 640, "duration": 4},
            {"duration": 2.89},
            {"duration": float("nan")},
            {"width": 1280.0},
        ):
            self.gateway._save({**source, "resource": {**source["resource"], **changed}})
            with self.assertRaises(GatewayError) as caught:
                self.gateway.normalize(
                    {"kind": "video", "mode": "edit", "source_id": source_id, "prompt": "Edit"}
                )
            self.assertEqual(caught.exception.code, "unsupported_source_specification")

    async def test_ingredients_uploads_distinct_inputs_and_submits_once(self):
        first = b"\x89PNG\r\n\x1a\nfirst"
        second = b"\xff\xd8\xffsecond"
        self.client.upload_temporary = AsyncMock(side_effect=["first-token", "second-token"])
        self.client.submit_images = AsyncMock(return_value=[])
        body = {
            "kind": "video",
            "mode": "ingredients",
            "prompt": "Both objects",
            "images_base64": [base64.b64encode(x).decode() for x in (first, second)],
        }
        response = await self.http.post(
            "/v1/vids/generations", json=body, headers={"Idempotency-Key": "multi-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.upload_temporary.await_count, 2)
        self.client.submit_images.assert_awaited_once_with(
            ["first-token", "second-token"],
            "Both objects",
            mode="ingredients",
            seconds=3,
            resolution="720p",
            orientation="landscape",
        )
        for invalid in ([], body["images_base64"] * 4, "not-a-list"):
            with self.assertRaises(GatewayError):
                self.gateway.normalize({**body, "images_base64": invalid})

    async def test_verified_portrait_parameters_catalog_and_idempotency(self):
        clip = [
            "co",
            None,
            "sc",
            "uk",
            "https://contribution-rt.usercontent.google.com/v",
            720,
            1280,
            ["4"],
        ]
        self.client.submit_text = AsyncMock(return_value=[[None] * 25 + [[None, None, clip]]])

        async def download(resource, path):
            Path(path).write_bytes(mp4(resource["duration"], resource["width"], resource["height"]))

        self.client.download_video = AsyncMock(side_effect=download)
        body = {"kind": "video", "prompt": "Boat", "seconds": 4, "size": "720x1280"}
        headers = {"Idempotency-Key": "portrait-key"}
        first = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        second = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["status"], "completed")
        self.assertEqual(first.json()["metadata"]["width"], 720)
        self.client.submit_text.assert_awaited_once_with(
            "Boat", seconds=4, resolution="720p", orientation="portrait"
        )
        content = await self.http.get(first.json()["content_url"])
        self.assertEqual(content.headers["content-type"], "video/mp4")
        catalog = (await self.http.get("/v1/models")).json()["data"][0]["capabilities"]
        self.assertIn(
            {"mode": "text", "seconds": 4, "size": "720x1280"}, catalog["verified_specifications"]
        )
        self.assertEqual(catalog["seconds"], list(range(3, 11)))
        for changed in (
            {"seconds": 2},
            {"seconds": 11},
            {"seconds": True},
            {"seconds": 4.0},
            {"size": [720, 1280]},
            {"mode": "animate"},
            {"mode": "ingredients"},
            {"mode": "unsupported"},
        ):
            invalid = await self.http.post(
                "/v1/vids/generations",
                json={**body, **changed},
                headers={"Idempotency-Key": "invalid-spec"},
            )
            self.assertEqual(invalid.status_code, 400)
        conflict = await self.http.post(
            "/v1/vids/generations", json={**body, "seconds": 3, "size": "1280x720"}, headers=headers
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.client.submit_text.await_count, 1)

    async def test_verified_landscape_four_second_parameters_use_720p_wire_option(self):
        clip = [
            "co",
            None,
            "sc",
            "uk",
            "https://contribution-rt.usercontent.google.com/v",
            1280,
            720,
            ["4"],
        ]
        self.client.submit_text = AsyncMock(return_value=[[None] * 25 + [[None, None, clip]]])

        async def download(resource, path):
            Path(path).write_bytes(mp4(resource["duration"], resource["width"], resource["height"]))

        self.client.download_video = AsyncMock(side_effect=download)
        body = {"kind": "video", "prompt": "Boat", "seconds": 4, "size": "1280x720"}
        result = await self.gateway.generate(body, "landscape-four-second-key")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["metadata"]["width"], 1280)
        self.assertEqual(result["metadata"]["duration"], 4)
        self.client.submit_text.assert_awaited_once_with(
            "Boat", seconds=4, resolution="720p", orientation="landscape"
        )
        self.assertEqual(await self.gateway.generate(body, "landscape-four-second-key"), result)
        self.client.submit_text.assert_awaited_once()
        catalog = (await self.http.get("/v1/models")).json()["data"][0]["capabilities"]
        self.assertIn(
            {"mode": "text", "seconds": 4, "size": "1280x720"}, catalog["verified_specifications"]
        )
        for changed in ({"size": "3840x2160"}, {"seconds": 11}, {"mode": "unsupported"}):
            with self.assertRaises(GatewayError) as caught:
                self.gateway.normalize({**body, **changed})
            self.assertIn(caught.exception.code, {"unsupported_specification", "invalid_mode"})

    async def test_wrong_upstream_video_specification_preserves_receipt_without_resubmit(self):
        for index, (width, height, seconds) in enumerate(((1280, 720, "4"), (720, 1280, "3"))):
            clip = [
                "co",
                None,
                "sc",
                "uk",
                "https://contribution-rt.usercontent.google.com/v",
                width,
                height,
                [seconds],
            ]
            self.client.submit_text = AsyncMock(return_value=[[None] * 25 + [[None, None, clip]]])
            self.client.download_video = AsyncMock()
            body = {"kind": "video", "prompt": "Boat", "seconds": 4, "size": "720x1280"}
            key = f"wrong-spec-{index}"
            record = await self.gateway.generate(body, key)
            self.assertEqual(record["status"], "specification_mismatch")
            self.assertIsNone(record["content_url"])
            self.assertIn("response", self.gateway.get(record["id"]))
            self.assertEqual(await self.gateway.generate(body, key), record)
            self.client.submit_text.assert_awaited_once()
            self.client.download_video.assert_not_awaited()
            with self.assertRaises(GatewayError):
                await self.gateway.retry_download(record["id"])

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = client()
        self.gateway = VidsGateway(self.client, self.directory.name)
        await self.gateway.start()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_vids_app(self.gateway, KEY)),
            base_url="http://localhost",
            headers={"Authorization": "Bearer " + KEY},
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.gateway.close()
        self.directory.cleanup()

    async def test_api_idempotency_content_and_private_response(self):
        body = {"kind": "speech", "prompt": "Hello"}
        headers = {"Idempotency-Key": "same-key"}
        first = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        second = await self.http.post("/v1/vids/generations", json=body, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.client.generate_speech.await_count, 1)
        self.assertNotIn("google.com", first.text)
        self.assertNotIn("response", first.json())
        content = await self.http.get(first.json()["content_url"])
        self.assertEqual(content.headers["content-type"], "audio/wav")
        self.assertTrue(content.content.startswith(b"RIFF"))
        changed = await self.http.post(
            "/v1/vids/generations", json={**body, "prompt": "Different"}, headers=headers
        )
        self.assertEqual(changed.status_code, 409)

    async def test_unknown_submission_is_never_repeated_even_after_restart(self):
        self.client.generate_speech.side_effect = VidsTransportError("private-sensitive-url")
        body = {"kind": "speech", "prompt": "Hello"}
        result = await self.gateway.generate(body, "timeout-key")
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("private", json.dumps(result))
        await self.gateway.generate(body, "timeout-key")
        self.assertEqual(self.client.generate_speech.await_count, 1)
        await self.gateway.close()
        new_client = client()
        restarted = VidsGateway(new_client, self.directory.name)
        await restarted.start()
        try:
            self.assertEqual((await restarted.generate(body, "timeout-key"))["status"], "unknown")
            self.assertEqual(new_client.generate_speech.await_count, 0)
        finally:
            await restarted.close()

    async def test_download_retry_uses_saved_response_without_generation(self):
        download = self.client.download_audio.side_effect
        self.client.download_audio.side_effect = VidsTransportError("private")
        result = await self.gateway.generate({"kind": "speech", "prompt": "Hello"}, "download-key")
        self.assertEqual(result["status"], "download_failed")
        self.assertIn("response", self.gateway.get(result["id"]))
        self.client.download_audio.side_effect = download
        result = await self.gateway.retry_download(result["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.client.generate_speech.await_count, 1)

    async def test_credentials_auth_and_unverified_specifications_fail_safely(self):
        secret = "DO-NOT-RETURN-COOKIE"
        response = await self.http.put("/v1/vids/session", json={"cookies": secret})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(secret, response.text)
        response = await self.http.get("/v1/models", headers={"Authorization": "Bearer invalid"})
        self.assertEqual(response.status_code, 401)
        response = await self.http.post(
            "/v1/vids/generations",
            json={"kind": "video", "prompt": "test", "mode": "upscale"},
            headers={"Idempotency-Key": "invalid"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.generate_speech.await_count, 0)
        self.assertEqual(list(Path(self.directory.name).glob("vids_*.json")), [])

    async def test_credential_update_validates_identity_and_preserves_old_session(self):
        wrong = client()
        wrong.flags["docs-hue"] = "another-account"
        with patch("src.services.vids.gateway.VidsClient", return_value=wrong):
            with self.assertRaises(GatewayError):
                await self.gateway.replace_cookies([{"name": "SAPISID"}])
        self.assertIs(self.gateway.client, self.client)
        self.assertEqual(self.client.session.close.await_count, 0)
        same = client()
        with patch("src.services.vids.gateway.VidsClient", return_value=same):
            await self.gateway.replace_cookies([{"name": "SAPISID"}])
        self.assertIs(self.gateway.client, same)
        self.assertEqual(self.client.session.close.await_count, 1)

    async def test_second_process_and_account_directory_reuse_are_rejected(self):
        second = VidsGateway(client(), self.directory.name)
        try:
            with self.assertRaises(GatewayError) as caught:
                await second.start()
            self.assertEqual(caught.exception.code, "runtime_busy")
        finally:
            await second.close()
        await self.gateway.close()
        other = client()
        other.flags["docs-hue"] = "other-account"
        second = VidsGateway(other, self.directory.name)
        try:
            with self.assertRaises(GatewayError) as caught:
                await second.start()
            self.assertEqual(caught.exception.code, "account_mismatch")
        finally:
            await second.close()


if __name__ == "__main__":
    unittest.main()
