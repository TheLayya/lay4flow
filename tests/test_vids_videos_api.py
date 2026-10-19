"""Offline Vids video contracts; every upstream call and MP4 is synthetic.

SDK validation checklist (openai==3.19.2): create/retrieve/list/download_content
use the installed SDK against ASGI, with retries disabled. Strict parsing covers
new completed and failed receipts, including string seconds="3" and vids-video.
Vids unknown/received statuses deliberately remain extensions: default SDK
parsing preserves them, while strict parsing rejects them. Historical receipts
with unknown size/seconds retain None rather than inventing a specification, so
they are also outside the strict SDK response contract. No test calls a real
generation endpoint, uploads an asset, opens a Profile, or consumes quota.
"""

import asyncio
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from test_vids_gateway import KEY, client as mock_client
from test_vids_video_gateway import video_response

from tests.test_vids_video_input import mp4

try:
    from openai import APIResponseValidationError, AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from src.api.vids import create_vids_app
from src.services.vids.client import VidsTransportError
from src.services.vids.gateway import VidsGateway

BODY = {"model": "vids-video", "prompt": "\n A blue boat moves slowly. \t"}
MP4 = mp4(3)


def identifier(key):
    return "vids_" + hashlib.sha256(key.encode()).hexdigest()


class VidsVideosAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_mp4_edit_json_multipart_and_generic_share_one_receipt(self):
        from tests.test_vids_video_input import mp4

        data = mp4(seconds=12.5, width=720, height=1280)
        encoded = base64.b64encode(data).decode()
        self.upstream.upload_temporary.return_value = "temporary-video"
        self.upstream.submit_video.return_value = video_response(1080, 1920, 8)
        body = {
            **BODY,
            "mode": "edit",
            "seconds": 8,
            "size": "1080x1920",
            "shot_mode": "montage",
            "video_base64": encoded,
        }
        first = await self.create("uploaded-edit", body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "completed")
        self.upstream.submit_video.assert_awaited_once_with(
            {"duration": 12.5, "width": 720, "height": 1280, "temporary_blob": "temporary-video"},
            BODY["prompt"],
            mode="edit",
            identifiers=None,
            seconds=8,
            resolution="1080p",
            orientation="portrait",
            shot_mode="montage",
        )
        # Put the file before mode: multipart field order must not create duplicate mode errors.
        replay = await self.http.post(
            "/v1/videos",
            files=[
                ("input_video", ("source.mp4", data, "video/mp4")),
                *[
                    (name, (None, str(value)))
                    for name, value in body.items()
                    if name != "video_base64"
                ],
            ],
            headers={"Idempotency-Key": "uploaded-edit"},
        )
        self.assertEqual(replay.json(), first.json())
        generic = await self.http.post(
            "/v1/vids/generations",
            json={
                "kind": "video",
                **{name: value for name, value in body.items() if name != "model"},
            },
            headers={"Idempotency-Key": "uploaded-edit"},
        )
        self.assertEqual(generic.json()["status"], "completed")
        self.upstream.upload_temporary.assert_awaited_once()
        self.upstream.submit_video.assert_awaited_once()
        for changes in (
            {"mode": "upscale"},
            {"source_id": identifier("other")},
            {"video_base64": "invalid"},
        ):
            invalid = await self.create(
                "invalid-upload-" + str(len(changes)) + str(changes), {**body, **changes}
            )
            self.assertEqual(invalid.status_code, 400, invalid.text)
        self.upstream.upload_temporary.assert_awaited_once()

    async def test_uploaded_extend_and_upscale_share_json_multipart_generic_receipts_after_restart(
        self,
    ):
        data = mp4(4.01, 720, 1280)
        encoded = base64.b64encode(data).decode()
        self.upstream.upload_temporary.return_value = "temporary-video"
        for mode, size, output_duration, specification in (
            ("extend", "720x1280", 7, 7.01),
            ("upscale", "1080x1920", 4, 4.01),
        ):
            self.upstream.upload_temporary.reset_mock()
            self.upstream.submit_video.reset_mock()
            width, height = map(int, size.split("x"))
            self.upstream.submit_video.return_value = video_response(width, height, output_duration)
            body = {**BODY, "mode": mode, "video_base64": encoded}
            key = "uploaded-" + mode
            first = await self.create(key, body)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["status"], "completed")
            self.assertEqual(first.json()["seconds"], f"{specification:g}")
            self.assertEqual(first.json()["size"], size)
            self.upstream.submit_video.assert_awaited_once_with(
                {
                    "duration": 4.01,
                    "width": 720,
                    "height": 1280,
                    "temporary_blob": "temporary-video",
                },
                BODY["prompt"],
                mode=mode,
                identifiers=None,
                seconds=3 if mode == "extend" else 4,
                resolution="720p" if mode == "extend" else "1080p",
                orientation="portrait",
            )
            replay = await self.http.post(
                "/v1/videos",
                files=[
                    ("input_video", ("renamed.mp4", data, "video/mp4")),
                    *[
                        (name, (None, str(value)))
                        for name, value in body.items()
                        if name != "video_base64"
                    ],
                ],
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(replay.json(), first.json())
            generic = await self.http.post(
                "/v1/vids/generations",
                json={
                    "kind": "video",
                    **{name: value for name, value in body.items() if name != "model"},
                },
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(generic.json()["id"], first.json()["id"])
            await self.restart()
            self.assertEqual((await self.create(key, body)).json(), first.json())
            self.upstream.upload_temporary.assert_awaited_once()
            self.upstream.submit_video.assert_awaited_once()
        self.upstream.submit_text.assert_not_awaited()
        self.upstream.submit_images.assert_not_awaited()

    async def test_every_uploaded_mode_rejects_receipt_source_and_missing_or_malformed_upload(self):
        source_id = self.save("legacy-source", file=True)
        encoded = base64.b64encode(MP4).decode()
        for mode in ("edit", "extend", "upscale"):
            for index, inputs in enumerate(
                (
                    {},
                    {"source_id": source_id},
                    {"source_id": source_id, "video_base64": encoded},
                    {"video_base64": "invalid"},
                )
            ):
                denied = await self.create(
                    f"rejected-{mode}-{index}", {**BODY, "mode": mode, **inputs}
                )
                self.assertEqual(denied.status_code, 400, denied.text)
            duplicate = await self.http.post(
                "/v1/videos",
                files=[
                    ("model", (None, "vids-video")),
                    ("prompt", (None, BODY["prompt"])),
                    ("mode", (None, mode)),
                    ("input_video", ("one.mp4", MP4, "video/mp4")),
                    ("input_video", ("two.mp4", MP4, "video/mp4")),
                ],
                headers={"Idempotency-Key": "duplicate-file-" + mode},
            )
            self.assertEqual(duplicate.status_code, 400, duplicate.text)
        self.assertEqual([path.stem for path in self.gateway.root.glob("vids_*.json")], [source_id])
        self.assert_no_submission()

    async def test_standard_reference_images_support_seven_inputs_and_reject_eight(self):
        from tests.test_vids_images_api import JPEG

        self.upstream.upload_temporary.return_value = "temporary-image"
        self.upstream.submit_images.return_value = video_response(1920, 1080, 10)
        fields = [
            (name, (None, value))
            for name, value in {
                **BODY,
                "mode": "ingredients",
                "seconds": "10",
                "size": "1920x1080",
                "shot_mode": "long_take",
            }.items()
        ]
        files = [("input_reference[]", (f"{index}.jpg", JPEG, "image/jpeg")) for index in range(7)]
        result = await self.http.post(
            "/v1/videos", files=[*fields, *files], headers={"Idempotency-Key": "seven-reference"}
        )
        self.assertEqual(result.json()["status"], "completed", result.text)
        self.upstream.submit_images.assert_awaited_once_with(
            ["temporary-image"] * 7,
            BODY["prompt"],
            mode="ingredients",
            seconds=10,
            resolution="1080p",
            orientation="landscape",
            shot_mode="long_take",
        )
        denied = await self.http.post(
            "/v1/videos",
            files=[*fields, *files, files[0]],
            headers={"Idempotency-Key": "eight-reference"},
        )
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(self.upstream.upload_temporary.await_count, 7)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.upstream = mock_client()
        self.upstream.submit_text = AsyncMock(return_value=video_response(1280, 720, 3))
        self.upstream.submit_video = AsyncMock()
        self.upstream.submit_images = AsyncMock()
        self.upstream.upload_temporary = AsyncMock()

        async def download(resource, path):
            Path(path).write_bytes(mp4(resource["duration"], resource["width"], resource["height"]))

        self.upstream.download_video = AsyncMock(side_effect=download)
        self.gateway = VidsGateway(self.upstream, self.directory.name)
        await self.gateway.start()
        self.connect()

    def connect(self):
        self.app = create_vids_app(self.gateway, KEY)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://vids.example.test",
            headers={"Authorization": "Bearer " + KEY},
        )

    async def restart(self):
        await self.http.aclose()
        await self.gateway.close()
        self.gateway = VidsGateway(self.upstream, self.directory.name)
        await self.gateway.start()
        self.connect()

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.gateway.close()
        self.directory.cleanup()

    async def create(self, key, body=None):
        return await self.http.post(
            "/v1/videos",
            json=BODY if body is None else body,
            headers={"Idempotency-Key": key},
        )

    def save(self, key, *, file=False, **changes):
        record = {
            "id": identifier(key),
            "kind": "video",
            "status": "completed",
            "created_at": 100,
            "error": None,
            "suffix": ".mp4",
            "mime_type": "video/mp4",
            "response": {"private-response": "https://private.example.test/result"},
            "resource": {"url": "https://private.example.test/result", "unique_key": "private-key"},
            **changes,
        }
        self.gateway._save(record)
        if file:
            (self.gateway.root / (record["id"] + ".mp4")).write_bytes(MP4)
        return record["id"]

    def assert_no_submission(self):
        self.upstream.submit_text.assert_not_awaited()
        self.upstream.submit_video.assert_not_awaited()
        self.upstream.submit_images.assert_not_awaited()
        self.upstream.upload_temporary.assert_not_awaited()
        self.upstream.download_video.assert_not_awaited()

    def assert_public(self, result):
        self.assertEqual(result["object"], "video")
        self.assertEqual(result["model"], "vids-video")
        self.assertRegex(result["id"], r"^vids_[a-f0-9]{64}$")
        self.assertNotIn("private", str(result))
        for field in (
            "resource",
            "response",
            "request_hash",
            "mode",
            "image_base64",
            "images_base64",
        ):
            self.assertNotIn(field, result)

    async def test_generic_and_standard_defaults_share_receipt_across_restart(self):
        for generic_first in (False, True):
            with self.subTest(generic_first=generic_first):
                key = "cross-route-" + str(generic_first)
                generic = {"kind": "video", "prompt": BODY["prompt"]}
                if generic_first:
                    initial = await self.http.post(
                        "/v1/vids/generations", json=generic, headers={"Idempotency-Key": key}
                    )
                    self.assertEqual(initial.status_code, 200)
                first = await self.create(key)
                self.assertEqual(first.status_code, 200)
                result = first.json()
                self.assert_public(result)
                self.assertEqual(result["id"], identifier(key))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["vids_status"], "completed")
                self.assertEqual(result["progress"], 100)
                self.assertEqual(result["prompt"], BODY["prompt"])
                self.assertEqual(result["seconds"], "3")
                self.assertEqual(result["size"], "1280x720")
                self.assertIsInstance(result["created_at"], int)
                self.assertIsInstance(result["completed_at"], int)
                self.assertGreaterEqual(result["completed_at"], result["created_at"])
                self.assertIsNone(result["error"])
                self.assertIsNone(result["retry_download_url"])
                self.assertEqual(result["receipt_url"], f"/v1/vids/generations/{result['id']}")
                self.assertEqual(result["content_url"], f"/v1/videos/{result['id']}/content")
                replay = await self.create(key, {**BODY, "seconds": "3", "size": "1280x720"})
                self.assertEqual(replay.json(), result)
                receipt = await self.http.post(
                    "/v1/vids/generations", json=generic, headers={"Idempotency-Key": key}
                )
                self.assertEqual(receipt.json()["id"], result["id"])
                self.assertEqual(
                    (await self.http.get(result["receipt_url"])).json(), receipt.json()
                )
                await self.restart()
                self.assertEqual((await self.create(key)).json(), result)
                self.assertEqual((await self.http.get(f"/v1/videos/{result['id']}")).json(), result)
        self.assertEqual(self.upstream.submit_text.await_count, 2)
        self.assertEqual(self.upstream.download_video.await_count, 2)
        self.upstream.submit_video.assert_not_awaited()
        self.upstream.submit_images.assert_not_awaited()
        self.upstream.upload_temporary.assert_not_awaited()

    async def test_verified_specifications_and_multipart_replay_match_json(self):
        for index, (seconds, size, width, height) in enumerate(
            ((3, "1280x720", 1280, 720), ("4", "1280x720", 1280, 720), (4, "720x1280", 720, 1280))
        ):
            with self.subTest(seconds=seconds, size=size):
                self.upstream.submit_text.reset_mock()
                self.upstream.submit_text.return_value = video_response(width, height, int(seconds))
                key = f"specification-{index}"
                body = {**BODY, "seconds": seconds, "size": size}
                result = await self.create(key, body)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["seconds"], str(seconds))
                self.assertEqual(result.json()["size"], size)
                multipart = await self.http.post(
                    "/v1/videos",
                    files=[(name, (None, str(value))) for name, value in body.items()],
                    headers={"Idempotency-Key": key},
                )
                self.assertEqual(multipart.status_code, 200)
                self.assertEqual(multipart.json(), result.json())
                self.upstream.submit_text.assert_awaited_once_with(
                    BODY["prompt"],
                    seconds=int(seconds),
                    resolution="720p",
                    orientation="portrait" if width == 720 else "landscape",
                )

    async def test_invalid_json_and_missing_idempotency_never_submit(self):
        cases = [
            {},
            {"prompt": BODY["prompt"]},
            {**BODY, "model": "sora-2"},
            {**BODY, "model": None},
            {**BODY, "model": []},
            {**BODY, "prompt": " \n\t"},
            {**BODY, "prompt": None},
            {**BODY, "prompt": 3},
            {**BODY, "prompt": "x" * 32001},
            {**BODY, "seconds": True},
            {**BODY, "seconds": 3.0},
            {**BODY, "seconds": "03"},
            {**BODY, "seconds": " 3"},
            {**BODY, "seconds": 11},
            {**BODY, "seconds": None},
            {**BODY, "size": "3840x2160"},
            {**BODY, "size": "1080p"},
            {**BODY, "size": None},
            {**BODY, "size": []},
            [],
        ]
        cases.extend(
            {**BODY, field: value}
            for field, value in (
                ("mode", "unsupported"),
                ("input_reference", {"image_url": "https://private.example.test/image"}),
                ("input_video", "private-file"),
                ("source_id", "private-id"),
                ("image_base64", "private-data"),
                ("images_base64", ["private-data"]),
                ("generationConfig", {}),
                ("kind", "video"),
            )
        )
        for index, body in enumerate(cases):
            with self.subTest(index=index):
                response = await self.create("invalid-" + str(index), body)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("private", response.text)
                self.assertIn("error", response.json())
        for headers in ({}, {"Idempotency-Key": ""}, {"Idempotency-Key": "x" * 201}):
            response = await self.http.post("/v1/videos", json=BODY, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "idempotency_required")
        malformed = await self.http.post(
            "/v1/videos", content=b"{", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(list(self.gateway.root.glob("vids_*.json")), [])
        self.assert_no_submission()

    async def test_multipart_duplicates_file_fields_and_other_media_types_never_submit(self):
        valid = [("model", (None, "vids-video")), ("prompt", (None, BODY["prompt"]))]
        cases = (
            valid + [("prompt", (None, "duplicate"))],
            valid + [("input_reference", ("private.png", b"private-bytes", "image/png"))],
            [("model", ("private.txt", b"vids-video")), ("prompt", (None, BODY["prompt"]))],
            valid + [("mode", (None, "unsupported"))],
        )
        for index, fields in enumerate(cases):
            response = await self.http.post(
                "/v1/videos", files=fields, headers={"Idempotency-Key": f"invalid-form-{index}"}
            )
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("private", response.text)
        for media_type in ("text/plain", "application/x-www-form-urlencoded"):
            response = await self.http.post(
                "/v1/videos", content="model=vids-video", headers={"Content-Type": media_type}
            )
            self.assertEqual(response.status_code, 415)
        self.assertEqual(list(self.gateway.root.glob("vids_*.json")), [])
        self.assert_no_submission()

    async def test_same_key_changed_prompt_duration_or_size_conflicts_without_submission(self):
        self.upstream.submit_text.return_value = video_response(1280, 720, 4)
        original = {**BODY, "seconds": 4}
        self.assertEqual((await self.create("conflict", original)).status_code, 200)
        for changed in ({"prompt": "Changed."}, {"seconds": 3}, {"size": "720x1280"}):
            response = await self.create("conflict", {**original, **changed})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")
        self.upstream.submit_text.assert_awaited_once()
        self.upstream.download_video.assert_awaited_once()

    async def test_all_standard_routes_require_authentication(self):
        video_id = self.save("auth", file=True)
        for method, path in (
            ("POST", "/v1/videos"),
            ("GET", "/v1/videos"),
            ("GET", f"/v1/videos/{video_id}"),
            ("GET", f"/v1/videos/{video_id}/content"),
        ):
            response = await self.http.request(
                method, path, headers={"Authorization": "Bearer invalid"}, json=BODY
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "invalid_api_key")
        self.assert_no_submission()

    async def test_pending_duplicate_returns_in_progress_without_second_submission(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def submit(prompt, **options):
            entered.set()
            await release.wait()
            return video_response(1280, 720, 3)

        self.upstream.submit_text.side_effect = submit
        pending = asyncio.create_task(self.create("pending"))
        await entered.wait()
        try:
            replay = await self.create("pending")
            self.assertEqual(replay.status_code, 202)
            self.assertEqual(replay.json()["status"], "in_progress")
            self.assertEqual(replay.json()["vids_status"], "generating")
            self.assertEqual(replay.json()["progress"], 0)
            self.assertIsNone(replay.json()["content_url"])
            self.assertIsNone(replay.json()["completed_at"])
            self.assertIsNone(replay.json()["retry_download_url"])
            self.assertEqual(
                (await self.http.get(f"/v1/videos/{identifier('pending')}")).json(), replay.json()
            )
            self.assertEqual((await self.create("other-while-busy")).status_code, 429)
        finally:
            release.set()
            completed = await pending
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"], "completed")
        self.upstream.submit_text.assert_awaited_once()

    async def test_unknown_acceptance_survives_restart_and_never_resubmits(self):
        self.upstream.submit_text.side_effect = VidsTransportError("https://private.example.test")
        first = await self.create("unknown")
        self.assertEqual(first.status_code, 202)
        result = first.json()
        self.assert_public(result)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["vids_status"], "unknown")
        self.assertEqual(result["error"]["code"], "acceptance_unknown")
        self.assertIsNone(result["content_url"])
        self.assertIsNone(result["retry_download_url"])
        self.assertIsNone(result["completed_at"])
        self.assertEqual((await self.create("unknown")).json(), result)
        await self.restart()
        self.assertEqual((await self.create("unknown")).json(), result)
        self.assertEqual((await self.http.get(f"/v1/videos/{result['id']}")).json(), result)
        unavailable = await self.http.get(f"/v1/videos/{result['id']}/content")
        self.assertEqual(unavailable.status_code, 409)
        refused = await self.http.post(result["receipt_url"] + "/download")
        self.assertEqual(refused.status_code, 409)
        self.upstream.submit_text.assert_awaited_once()
        self.upstream.download_video.assert_not_awaited()

    async def test_download_failure_requires_explicit_recovery_without_resubmission(self):
        self.upstream.download_video.side_effect = VidsTransportError("private-result")
        first = await self.create("download-failure")
        self.assertEqual(first.status_code, 200)
        result = first.json()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["vids_status"], "download_failed")
        self.assertEqual(result["error"]["code"], "download_failed")
        self.assertEqual(result["progress"], 0)
        self.assertIsNone(result["completed_at"])
        self.assertIsNone(result["content_url"])
        self.assertEqual(result["retry_download_url"], result["receipt_url"] + "/download")
        await self.restart()
        self.assertEqual((await self.create("download-failure")).json(), result)
        self.upstream.download_video.assert_awaited_once()

        async def download(resource, path):
            Path(path).write_bytes(MP4)

        self.upstream.download_video.side_effect = download
        recovered = await self.http.post(result["retry_download_url"])
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["status"], "completed")
        completed = (await self.http.get(f"/v1/videos/{result['id']}")).json()
        self.assertEqual(completed["status"], "completed")
        self.assertIsInstance(completed["completed_at"], int)
        self.assertIsNone(completed["error"])
        self.assertIsNone(completed["retry_download_url"])
        self.assertEqual((await self.http.get(completed["content_url"])).content, MP4)
        self.upstream.submit_text.assert_awaited_once()
        self.assertEqual(self.upstream.download_video.await_count, 2)

    async def test_legacy_receipts_preserve_missing_facts_and_total_output_seconds(self):
        old_id = self.save("old", file=True)
        result = (await self.http.get(f"/v1/videos/{old_id}")).json()
        self.assert_public(result)
        for field in ("prompt", "seconds", "size", "completed_at"):
            self.assertIsNone(result[field])
        extended = self.save(
            "extended",
            video_specification={"seconds": 6, "size": "1280x720"},
            video_request={"prompt": "Continue.", "seconds": 3, "size": "1280x720"},
        )
        result = (await self.http.get(f"/v1/videos/{extended}")).json()
        self.assertEqual(result["seconds"], "6")
        self.assertEqual(result["prompt"], "Continue.")
        self.assertEqual(result["size"], "1280x720")
        self.assertIsNone(result["completed_at"])
        self.assert_public(result)
        self.assert_no_submission()

    async def test_status_mapping_is_factual_and_recovery_links_are_limited(self):
        for status, mapped, error in (
            ("generating", "in_progress", None),
            ("received", "received", None),
            ("unknown", "unknown", "interrupted_acceptance_unknown"),
            ("failed", "failed", "upstream_http_403"),
            ("no_result", "failed", "upstream_returned_no_media"),
            ("specification_mismatch", "failed", "upstream_video_specification"),
            ("download_failed", "failed", "download_interrupted"),
        ):
            with self.subTest(status=status):
                video_id = self.save(status, status=status, error=error)
                response = await self.http.get(f"/v1/videos/{video_id}")
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assert_public(result)
                self.assertEqual(result["status"], mapped)
                self.assertEqual(result["vids_status"], status)
                self.assertEqual(result["progress"], 0)
                self.assertIsNone(result["content_url"])
                self.assertIsNone(result["completed_at"])
                self.assertEqual(
                    result["retry_download_url"],
                    result["receipt_url"] + "/download"
                    if status in {"received", "download_failed"}
                    else None,
                )
                if error:
                    self.assertEqual(result["error"]["code"], error)
                    self.assertIsInstance(result["error"]["message"], str)
                else:
                    self.assertIsNone(result["error"])
        secret_error = self.save("secret-error", status="failed", error="private-bearer-cookie")
        response = await self.http.get(f"/v1/videos/{secret_error}")
        self.assertNotIn("private", response.text)
        self.assertEqual(response.json()["error"]["code"], "generation_failed")
        self.assert_no_submission()

    async def test_other_media_insert_operations_and_unsafe_ids_are_not_videos(self):
        identifiers = [
            self.save("image", kind="image"),
            self.save("speech", kind="speech"),
            self.save("music", kind="music"),
            self.save("insert", operation="insert"),
            "private-id",
            "vids_" + "f" * 63,
            "vids_" + "F" * 64,
            identifier("absent"),
            "vids_%2Fprivate",
            "vids_%00private",
            "vids_%5C..%5Cprivate",
        ]
        for video_id in identifiers:
            for suffix in ("", "/content"):
                response = await self.http.get(f"/v1/videos/{video_id}{suffix}")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("private", response.text)
        self.assert_no_submission()

    async def test_content_serves_only_verified_mp4_and_rejects_missing_or_unready_assets(self):
        result = (await self.create("content")).json()
        for query in ("", "?variant=video"):
            response = await self.http.get(result["content_url"] + query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, MP4)
            self.assertEqual(response.headers["content-type"], "video/mp4")
            self.assertIn(result["id"] + ".mp4", response.headers["content-disposition"])
        for variant in ("thumbnail", "spritesheet", "unknown", ""):
            response = await self.http.get(result["content_url"], params={"variant": variant})
            self.assertEqual(response.status_code, 400)
        (self.gateway.root / (result["id"] + ".mp4")).unlink()
        self.assertEqual((await self.http.get(result["content_url"])).status_code, 410)
        for index, changes in enumerate(
            (
                {"suffix": ".wav", "mime_type": "audio/wav"},
                {"suffix": ".mp4", "mime_type": "text/html"},
                {"suffix": "/../../private", "mime_type": "video/mp4"},
                {"status": "received"},
            )
        ):
            video_id = self.save(f"bad-content-{index}", file=True, **changes)
            response = await self.http.get(f"/v1/videos/{video_id}/content")
            self.assertEqual(response.status_code, 409 if changes.get("status") else 410)
            self.assertNotIn("private", response.text)
        self.upstream.submit_text.assert_awaited_once()
        self.upstream.download_video.assert_awaited_once()

    async def test_listing_is_private_and_pages_stably_by_creation_then_id(self):
        expected = []
        for key, created_at in (
            ("later-b", 101),
            ("same-b", 100),
            ("earlier", 99),
            ("same-a", 100),
        ):
            expected.append((created_at, self.save(key, created_at=created_at)))
        self.save("hidden-image", kind="image")
        self.save("hidden-speech", kind="speech")
        self.save("hidden-insert", operation="insert")
        for order in ("asc", "desc"):
            expected_ids = [item[1] for item in sorted(expected, reverse=order == "desc")]
            found = []
            after = None
            for page_number in range(3):
                query = {"limit": 2, "order": order}
                if after:
                    query["after"] = after
                response = await self.http.get("/v1/videos", params=query)
                self.assertEqual(response.status_code, 200)
                page = response.json()
                self.assertEqual(page["object"], "list")
                self.assertEqual(page["has_more"], page_number == 0)
                ids = [item["id"] for item in page["data"]]
                self.assertEqual(page["first_id"], ids[0] if ids else None)
                self.assertEqual(page["last_id"], ids[-1] if ids else None)
                for item in page["data"]:
                    self.assert_public(item)
                self.assertNotIn("private", response.text)
                found.extend(ids)
                after = page["last_id"] or after
            self.assertEqual(found, expected_ids)
        default = (await self.http.get("/v1/videos")).json()
        self.assertEqual([item["id"] for item in default["data"]], expected_ids)
        self.assert_no_submission()

    async def test_empty_listing_and_invalid_pagination_are_read_only(self):
        self.assertEqual(
            (await self.http.get("/v1/videos")).json(),
            {"object": "list", "data": [], "has_more": False, "first_id": None, "last_id": None},
        )
        hidden = self.save("cursor-image", kind="image")
        for query in (
            {"limit": "0"},
            {"limit": "101"},
            {"limit": "true"},
            {"limit": "1.5"},
            {"order": "sideways"},
            {"after": "private-cursor"},
            {"after": identifier("missing-cursor")},
            {"after": hidden},
        ):
            with self.subTest(query=query):
                response = await self.http.get("/v1/videos", params=query)
                self.assertIn(response.status_code, (400, 404))
                self.assertNotIn("private", response.text)
        for limit in (1, 100):
            self.assertEqual(
                (await self.http.get("/v1/videos", params={"limit": limit})).status_code, 200
            )
        self.assert_no_submission()

    @unittest.skipUnless(AsyncOpenAI, "Optional OpenAI SDK is not installed")
    async def test_sdk_create_retrieve_list_and_download_parse_strict_completed_receipts(self):
        sdk_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        async with AsyncOpenAI(
            api_key=KEY,
            base_url="https://vids.example.test/v1",
            max_retries=0,
            http_client=sdk_http,
            _strict_response_validation=True,
        ) as sdk:
            first = await sdk.videos.create(
                model="vids-video",
                prompt=BODY["prompt"],
                extra_headers={"Idempotency-Key": "sdk-video"},
            )
            replay = await sdk.videos.create(
                model="vids-video",
                prompt=BODY["prompt"],
                seconds="3",
                size="1280x720",
                extra_headers={"Idempotency-Key": "sdk-video"},
            )
            self.assertEqual(first.model_dump(), replay.model_dump())
            self.assertEqual(first.object, "video")
            self.assertEqual(first.status, "completed")
            self.assertEqual(first.seconds, "3")
            self.assertEqual(first.model, "vids-video")
            self.assertEqual(first.prompt, BODY["prompt"])
            self.assertIsInstance(first.completed_at, int)
            retrieved = await sdk.videos.retrieve(first.id)
            self.assertEqual(retrieved.model_dump(), first.model_dump())
            second_id = self.save(
                "sdk-second",
                created_at=first.created_at + 1,
                video_specification={"seconds": 4, "size": "720x1280"},
            )
            page = await sdk.videos.list(limit=1, order="asc")
            self.assertEqual([item.id for item in page.data], [first.id])
            self.assertTrue(page.has_next_page())
            next_page = await page.get_next_page()
            self.assertEqual([item.id for item in next_page.data], [second_id])
            self.assertFalse(next_page.has_next_page())
            self.assertEqual(
                [item.id async for item in sdk.videos.list(limit=1, order="asc")],
                [first.id, second_id],
            )
            content = await sdk.videos.download_content(first.id, variant="video")
            self.assertEqual(content.content, MP4)
            self.assertEqual(content.response.headers["content-type"], "video/mp4")
        self.upstream.submit_text.assert_awaited_once_with(
            BODY["prompt"], seconds=3, resolution="720p", orientation="landscape"
        )
        self.upstream.download_video.assert_awaited_once()

    @unittest.skipUnless(AsyncOpenAI, "Optional OpenAI SDK is not installed")
    async def test_sdk_default_accepts_unknown_but_strict_parsing_rejects_extended_states(self):
        self.upstream.submit_text.side_effect = VidsTransportError("private-upstream")
        sdk_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        async with AsyncOpenAI(
            api_key=KEY,
            base_url="https://vids.example.test/v1",
            max_retries=0,
            http_client=sdk_http,
        ) as sdk:
            result = await sdk.videos.create(
                model="vids-video",
                prompt=BODY["prompt"],
                extra_headers={"Idempotency-Key": "sdk-unknown"},
            )
            self.assertEqual(result.status, "unknown")
            self.assertEqual(result.model_extra["vids_status"], "unknown")
            self.assertEqual((await sdk.videos.retrieve(result.id)).status, "unknown")
        sdk_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        received_id = self.save(
            "sdk-received",
            status="received",
            video_specification={"seconds": 3, "size": "1280x720"},
        )
        old_id = self.save("sdk-legacy")
        failed_id = self.save(
            "sdk-failed",
            status="download_failed",
            error="download_failed",
            video_specification={"seconds": 3, "size": "1280x720"},
        )
        async with AsyncOpenAI(
            api_key=KEY,
            base_url="https://vids.example.test/v1",
            max_retries=0,
            http_client=sdk_http,
            _strict_response_validation=True,
        ) as sdk:
            for video_id in (result.id, received_id, old_id):
                with self.assertRaises(APIResponseValidationError):
                    await sdk.videos.retrieve(video_id)
            failed = await sdk.videos.retrieve(failed_id)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error.code, "download_failed")
        self.upstream.submit_text.assert_awaited_once()
        self.upstream.download_video.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
