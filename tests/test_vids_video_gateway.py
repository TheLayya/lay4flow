"""Offline API contracts for verified extension and upscale source/result specifications."""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from test_vids_gateway import KEY, client

from src.api.vids import create_vids_app
from src.services.vids.client import VidsProtocolError, VidsTransportError
from src.services.vids.gateway import GatewayError, VidsGateway
from tests.test_vids_video_input import mp4

SOURCE_ID = "vids_" + "a" * 64
SOURCE_RESOURCE = {
    "corpus": "synthetic-corpus",
    "lookup_schema": "synthetic-schema",
    "unique_key": "synthetic-source-key",
    "url": "https://contribution-rt.usercontent.google.com/private-source",
    "width": 1280,
    "height": 720,
    "duration": 3.008,
}


def video_response(width, height, seconds):
    clip = [
        "synthetic-corpus",
        None,
        "synthetic-schema",
        "synthetic-result-key",
        "https://contribution-rt.usercontent.google.com/private-result",
        width,
        height,
        [str(seconds)],
    ]
    return [[None] * 25 + [[None, None, clip]]]


class VidsVideoGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_errors_record_upload_or_generate_operation_and_replay_without_submission(
        self,
    ):
        body = {
            "kind": "video",
            "mode": "edit",
            "prompt": "Edit",
            "video_base64": base64.b64encode(mp4(10)).decode(),
        }
        for operation in ("upload-start", "upload-finalize", "generate"):
            self.client.upload_temporary.reset_mock()
            self.client.submit_video.reset_mock()
            self.client.upload_temporary.side_effect = (
                VidsProtocolError(operation, 400) if operation != "generate" else None
            )
            self.client.upload_temporary.return_value = "private-upload"
            self.client.submit_video.side_effect = (
                VidsProtocolError(operation, 400) if operation == "generate" else None
            )
            first = await self.gateway.generate(body, "http-operation-" + operation)
            self.assertEqual(first["status"], "failed")
            self.assertEqual(first["error"], "upstream_http_400")
            self.assertEqual(first["error_operation"], operation)
            self.assertEqual(
                self.gateway.get(first["id"])["stage"],
                "video_generation" if operation == "generate" else "video_upload",
            )
            self.assertEqual(
                await self.gateway.generate(body, "http-operation-" + operation), first
            )
            self.client.upload_temporary.assert_awaited_once()
            if operation == "generate":
                self.client.submit_video.assert_awaited_once()
            else:
                self.client.submit_video.assert_not_awaited()
            self.assertNotIn("private", str(first))

    async def test_supported_specs_reach_all_video_modes_and_keep_real_verification_separate(self):
        encoded = base64.b64encode(b"\xff\xd8\xffsynthetic").decode()
        self.client.upload_temporary.return_value = "uploaded-image"
        for mode in ("text", "animate", "ingredients", "edit"):
            for seconds in range(3, 11):
                for size in ("1280x720", "720x1280", "1920x1080", "1080x1920"):
                    width, height = map(int, size.split("x"))
                    response = video_response(width, height, seconds)
                    method = (
                        self.client.submit_text
                        if mode == "text"
                        else self.client.submit_video
                        if mode == "edit"
                        else self.client.submit_images
                    )
                    method.return_value = response
                    body = {
                        "kind": "video",
                        "mode": mode,
                        "prompt": "A scene",
                        "seconds": seconds,
                        "size": size,
                    }
                    if mode == "animate":
                        body["image_base64"] = encoded
                    elif mode == "ingredients":
                        body["images_base64"] = [encoded] * 7
                    elif mode == "edit":
                        body["video_base64"] = base64.b64encode(mp4(3.008)).decode()
                        body["images_base64"] = [encoded] * 3
                    if mode != "animate":
                        body["shot_mode"] = "montage"
                    result = await self.gateway.generate(body, f"full-{mode}-{seconds}-{size}")
                    self.assertEqual(result["status"], "completed", body)
                    self.assertEqual(method.await_args.kwargs["seconds"], seconds)
                    self.assertEqual(
                        method.await_args.kwargs["resolution"],
                        "1080p" if width == 1920 or height == 1920 else "720p",
                    )
                    self.assertEqual(
                        method.await_args.kwargs["orientation"],
                        "portrait" if height > width else "landscape",
                    )
        catalog = (await self.http.get("/v1/models")).json()["data"]
        caps = next(row["capabilities"] for row in catalog if row["kind"] == "video")
        self.assertIn(
            {"mode": "text", "seconds": 10, "size": "1080x1920"}, caps["supported_specifications"]
        )
        self.assertNotIn(
            {"mode": "text", "seconds": 10, "size": "1080x1920"}, caps["verified_specifications"]
        )
        self.assertIn(
            {"mode": "edit", "seconds": 10, "size": "1280x720"}, caps["verified_specifications"]
        )

    async def test_uploaded_portrait_source_defaults_extend_actual_duration_and_upscale(self):
        encoded = base64.b64encode(mp4(4.01, 720, 1280)).decode()
        for mode, size, output_seconds in (
            ("edit", "720x1280", 4),
            ("extend", "720x1280", 7),
            ("upscale", "1080x1920", 4),
        ):
            width, height = map(int, size.split("x"))
            self.client.submit_video.return_value = video_response(width, height, output_seconds)
            body = {"kind": "video", "mode": mode, "prompt": "Update", "video_base64": encoded}
            result = await self.gateway.generate(body, "portrait-source-" + mode)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(self.client.submit_video.await_args.kwargs["orientation"], "portrait")
            if mode == "extend":
                self.assertAlmostEqual(
                    self.gateway.get(result["id"])["video_specification"]["seconds"], 7.01
                )

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = client()
        self.client.submit_video = AsyncMock()
        self.client.submit_text = AsyncMock()
        self.client.submit_images = AsyncMock()
        self.client.upload_temporary = AsyncMock(return_value="uploaded-video")

        async def download(resource, path):
            Path(path).write_bytes(mp4(resource["duration"], resource["width"], resource["height"]))

        self.client.download_video = AsyncMock(side_effect=download)
        self.gateway = VidsGateway(self.client, self.directory.name)
        await self.gateway.start()
        self.save_source()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_vids_app(self.gateway, KEY)),
            base_url="http://localhost",
            headers={"Authorization": "Bearer " + KEY},
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.gateway.close()
        self.directory.cleanup()

    def save_source(self, *, identifier=SOURCE_ID, **changes):
        self.gateway._save(
            {
                "id": identifier,
                "kind": "video",
                "status": "completed",
                "resource": dict(SOURCE_RESOURCE),
                **changes,
            }
        )

    def body(self, mode):
        return {
            "kind": "video",
            "mode": mode,
            "prompt": "Continue the scene." if mode == "extend" else "Upscale the video.",
            "video_base64": base64.b64encode(mp4(3.008)).decode(),
            "seconds": 3,
            "size": "1280x720" if mode == "extend" else "1920x1080",
        }

    async def generate(self, body, key):
        return await self.http.post(
            "/v1/vids/generations", json=body, headers={"Idempotency-Key": key}
        )

    async def test_modes_upload_source_once_and_validate_complete_result(self):
        for mode, width, height, seconds in (
            ("extend", 1280, 720, 6),
            ("upscale", 1920, 1080, 3),
        ):
            with self.subTest(mode=mode):
                self.client.submit_video.reset_mock()
                self.client.download_video.reset_mock()
                self.client.upload_temporary.reset_mock()
                self.client.submit_video.return_value = video_response(width, height, seconds)
                body = self.body(mode)
                first = await self.generate(body, mode)
                self.assertEqual(first.status_code, 200)
                result = first.json()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(
                    {name: result["metadata"][name] for name in ("width", "height", "duration")},
                    {"width": width, "height": height, "duration": seconds},
                )
                self.assertNotIn("private", first.text)
                self.assertNotIn("response", result)
                self.assertEqual((await self.generate(body, mode)).json(), result)
                self.client.submit_video.assert_awaited_once_with(
                    {
                        "duration": 3.008,
                        "width": 1280,
                        "height": 720,
                        "temporary_blob": "uploaded-video",
                    },
                    body["prompt"],
                    mode=mode,
                    identifiers=None,
                    seconds=3,
                    resolution="720p" if mode == "extend" else "1080p",
                    orientation="landscape",
                )
                self.client.download_video.assert_awaited_once()
                self.client.upload_temporary.assert_awaited_once()
                content = await self.http.get(result["content_url"])
                self.assertEqual(content.status_code, 200)
                self.assertEqual(content.headers["content-type"], "video/mp4")
                self.assertEqual(content.content[4:8], b"ftyp")
                saved = self.gateway.get(result["id"])
                self.assertEqual(
                    saved["video_specification"],
                    {"seconds": seconds + 0.008, "size": body["size"]},
                )
        self.client.submit_text.assert_not_awaited()
        self.client.submit_images.assert_not_awaited()

    async def test_downloaded_video_bytes_are_validated_and_replayed_without_resubmission(self):
        body = {"kind": "video", "prompt": "Boat", "seconds": 3, "size": "1280x720"}
        self.client.submit_text.return_value = video_response(1280, 720, 3)

        async def malformed(_resource, path):
            Path(path).write_bytes(b"\x00\x00\x00\x18ftypisomsynthetic")

        self.client.download_video.side_effect = malformed
        failed = await self.generate(body, "malformed-output")
        self.assertEqual(failed.status_code, 200, failed.text)
        failed_result = failed.json()
        self.assertEqual(failed_result["status"], "download_failed")
        self.assertEqual(failed_result["error"], "download_failed")
        self.assertIn("response", self.gateway.get(failed_result["id"]))
        replay = await self.generate(body, "malformed-output")
        self.assertEqual(replay.json(), failed_result)
        self.client.submit_text.assert_awaited_once_with(
            "Boat", seconds=3, resolution="720p", orientation="landscape"
        )

        for name, data in (("duration", mp4(4, 1280, 720)), ("size", mp4(3, 720, 1280))):
            with self.subTest(mismatch=name):

                async def wrong_specification(_resource, path):
                    Path(path).write_bytes(data)

                self.client.download_video.side_effect = wrong_specification
                key = "mismatched-output-" + name
                mismatch = await self.generate(body, key)
                self.assertEqual(mismatch.status_code, 200, mismatch.text)
                mismatch_result = mismatch.json()
                self.assertEqual(mismatch_result["status"], "specification_mismatch")
                self.assertEqual(mismatch_result["error"], "upstream_video_specification")
                self.assertIsNone(mismatch_result["content_url"])
                self.assertIn("response", self.gateway.get(mismatch_result["id"]))
                self.assertEqual((await self.generate(body, key)).json(), mismatch_result)
                retry = await self.http.post(
                    "/v1/vids/generations/" + mismatch_result["id"] + "/download"
                )
                self.assertEqual(retry.status_code, 409)
        self.assertEqual(self.client.submit_text.await_count, 3)

        async def recovered_download(resource, path):
            Path(path).write_bytes(mp4(resource["duration"], resource["width"], resource["height"]))

        self.client.download_video.side_effect = recovered_download
        recovery = await self.http.post("/v1/vids/generations/" + failed_result["id"] + "/download")
        self.assertEqual(recovery.json()["status"], "completed", recovery.text)
        self.assertEqual(self.client.submit_text.await_count, 3)

    async def test_catalog_lists_exact_mode_specifications_and_additional_duration(self):
        catalog = (await self.http.get("/v1/models")).json()["data"]
        capabilities = next(item["capabilities"] for item in catalog if item["kind"] == "video")
        self.assertIn("extend", capabilities["modes"])
        self.assertIn("upscale", capabilities["modes"])
        specifications = capabilities["verified_specifications"]
        for mode, size in (("extend", "1280x720"), ("upscale", "1920x1080")):
            self.assertEqual(
                [item for item in specifications if item["mode"] == mode],
                [{"mode": mode, "seconds": 3, "size": size}],
            )
        self.assertIn("additional duration", capabilities["extend_seconds"])

    async def test_internal_legacy_completed_source_metadata_is_still_validated(self):
        cases = (
            {"status": "unknown"},
            {"status": "download_failed"},
            {"kind": "image"},
            {"resource": {}},
            {"resource": {**SOURCE_RESOURCE, "duration": 2.89}},
            {"resource": {**SOURCE_RESOURCE, "duration": 10.11}},
            {"resource": {**SOURCE_RESOURCE, "duration": True}},
            {"resource": {**SOURCE_RESOURCE, "duration": float("nan")}},
            {"resource": {**SOURCE_RESOURCE, "width": 1280.0}},
            {"resource": {**SOURCE_RESOURCE, "width": 640, "height": 360}},
            {"resource": {**SOURCE_RESOURCE, "width": 3840, "height": 2160}},
        )
        for mode in ("extend", "upscale"):
            for index, changed in enumerate(cases):
                with self.subTest(mode=mode, source=changed):
                    self.save_source(**changed)
                    body = self.body(mode)
                    body.pop("video_base64")
                    body["source_id"] = SOURCE_ID
                    with self.assertRaises(GatewayError) as caught:
                        self.gateway.normalize(body)
                    self.assertEqual(caught.exception.status, 400)
                    self.assertIn(
                        caught.exception.code,
                        {"invalid_source", "unsupported_source_specification"},
                    )
            self.save_source()
            missing = self.body(mode)
            missing.pop("video_base64")
            response = await self.generate(missing, f"missing-{mode}")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_source")
        self.client.submit_video.assert_not_awaited()
        self.client.download_video.assert_not_awaited()

    async def test_public_video_modes_require_uploaded_mp4_before_submission(self):
        invalid = (
            None,
            "invalid",
            base64.b64encode(b"not-an-mp4").decode(),
            base64.b64encode(mp4(2.99)).decode(),
            base64.b64encode(mp4(60.01)).decode(),
        )
        for mode in ("edit", "extend", "upscale"):
            body = {"kind": "video", "mode": mode, "prompt": "Update"}
            for index, encoded in enumerate(invalid):
                response = await self.generate(
                    {**body, "video_base64": encoded}, f"bad-upload-{mode}-{index}"
                )
                self.assertEqual(response.status_code, 400, response.text)
            for index, fields in enumerate(
                (
                    {},
                    {"source_id": SOURCE_ID},
                    {"source_id": SOURCE_ID, "video_base64": self.body(mode)["video_base64"]},
                )
            ):
                response = await self.generate({**body, **fields}, f"no-upload-{mode}-{index}")
                self.assertEqual(response.status_code, 400, response.text)
        for mode in ("text", "animate", "ingredients"):
            response = await self.generate(
                {**self.body("extend"), "mode": mode}, "wrong-upload-mode-" + mode
            )
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual([path.stem for path in self.gateway.root.glob("vids_*.json")], [SOURCE_ID])
        self.client.upload_temporary.assert_not_awaited()
        self.client.submit_video.assert_not_awaited()
        self.client.submit_text.assert_not_awaited()
        self.client.submit_images.assert_not_awaited()
        self.client.download_video.assert_not_awaited()

    async def test_long_uploaded_extension_uses_last_ten_seconds_and_upscale_rejects_it(self):
        encoded = base64.b64encode(mp4(60, 720, 1280)).decode()
        body = {"kind": "video", "mode": "extend", "prompt": "Continue", "video_base64": encoded}
        self.client.submit_video.return_value = video_response(720, 1280, 13)
        response = await self.generate(body, "long-upload-extension")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "completed", response.text)
        saved = self.gateway.get(response.json()["id"])
        self.assertEqual(saved["video_specification"], {"seconds": 13, "size": "720x1280"})
        self.assertEqual(self.client.submit_video.await_args.args[0]["duration"], 60)
        self.assertEqual(self.client.submit_video.await_args.kwargs["seconds"], 3)
        self.assertEqual(
            (await self.generate(body, "long-upload-extension")).json(), response.json()
        )
        rejected = await self.generate({**body, "mode": "upscale"}, "long-upload-upscale")
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(rejected.json()["error"]["code"], "unsupported_source_specification")
        self.client.upload_temporary.assert_awaited_once()
        self.client.submit_video.assert_awaited_once()

    async def test_internal_legacy_source_container_tolerance_remains_reusable(self):
        resource = {**SOURCE_RESOURCE, "duration": 2.99}
        self.save_source(resource=resource)
        for mode, width, height, seconds in (
            ("edit", 1280, 720, 3),
            ("extend", 1280, 720, 6),
            ("upscale", 1920, 1080, 3),
        ):
            with self.subTest(mode=mode):
                self.client.submit_video.reset_mock()
                self.client.submit_video.return_value = video_response(width, height, seconds)
                body = {"kind": "video", "mode": mode, "prompt": "Update", "source_id": SOURCE_ID}
                result = await self.gateway.generate(body, "tolerant-source-" + mode)
                self.assertEqual(result["status"], "completed", result)
                self.client.submit_video.assert_awaited_once()

    async def test_unverified_size_duration_and_input_combinations_fail_before_submission(self):
        for mode in ("extend", "upscale"):
            for index, changed in enumerate(
                (
                    {"seconds": 11},
                    {"seconds": True},
                    {"seconds": 3.0},
                    {"size": "3840x2160"},
                    {"size": "1080p"},
                    {"mode": "unsupported"},
                    {"mode": []},
                    {"images_base64": ["synthetic"]},
                    {"image_base64": "synthetic"},
                )
            ):
                with self.subTest(mode=mode, changed=changed):
                    response = await self.generate(
                        {**self.body(mode), **changed}, f"invalid-{mode}-{index}"
                    )
                    self.assertEqual(response.status_code, 400)
        self.client.submit_video.assert_not_awaited()
        self.client.upload_temporary.assert_not_awaited()
        upscale = self.body("upscale")
        upscale.pop("size")
        for index, body in enumerate(({**self.body("upscale"), "prompt": ""},)):
            response = await self.generate(body, f"upscale-required-{index}")
            self.assertEqual(response.status_code, 400)
        self.client.submit_video.assert_not_awaited()

    async def test_wrong_result_specification_keeps_response_without_download_or_resubmit(self):
        for index, (mode, width, height, seconds) in enumerate(
            (
                ("extend", 1280, 720, 3),
                ("extend", 1920, 1080, 6),
                ("upscale", 1280, 720, 3),
                ("upscale", 1920, 1080, 6),
            )
        ):
            with self.subTest(mode=mode, width=width, seconds=seconds):
                self.client.submit_video.reset_mock()
                self.client.submit_video.return_value = video_response(width, height, seconds)
                body, key = self.body(mode), f"mismatch-{index}"
                first = await self.generate(body, key)
                self.assertEqual(first.status_code, 200)
                result = first.json()
                self.assertEqual(result["status"], "specification_mismatch")
                self.assertIsNone(result["content_url"])
                self.assertIn("response", self.gateway.get(result["id"]))
                self.assertEqual((await self.generate(body, key)).json(), result)
                retry = await self.http.post(f"/v1/vids/generations/{result['id']}/download")
                self.assertEqual(retry.status_code, 409)
                self.client.submit_video.assert_awaited_once()
        self.client.download_video.assert_not_awaited()

    async def test_idempotency_covers_source_prompt_and_mode_and_normalized_defaults(self):
        self.client.submit_video.return_value = video_response(1280, 720, 6)
        body = self.body("extend")
        first = await self.generate(body, "same-extension")
        self.assertEqual(first.status_code, 200)
        defaults = dict(body)
        defaults.pop("seconds")
        defaults.pop("size")
        self.assertEqual((await self.generate(defaults, "same-extension")).json(), first.json())
        for changed in (
            {"prompt": "Different continuation."},
            {"video_base64": base64.b64encode(mp4(3.009)).decode()},
            {"mode": "edit"},
        ):
            response = await self.generate({**body, **changed}, "same-extension")
            self.assertEqual(response.status_code, 409)
        self.client.submit_video.assert_awaited_once()

    async def test_unknown_acceptance_is_replayed_without_new_submission(self):
        self.client.submit_video.side_effect = VidsTransportError("private-result")
        body = self.body("extend")
        first = await self.generate(body, "unknown-extension")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["status"], "unknown")
        self.assertEqual((await self.generate(body, "unknown-extension")).json(), first.json())
        self.assertNotIn("private", first.text)
        self.client.submit_video.assert_awaited_once()
        self.client.download_video.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
