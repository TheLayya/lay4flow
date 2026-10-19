"""SKU video lifecycle uses one real SQLite job service and synthetic generation."""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api import compat_videos, routes
from src.api.compat_videos import create_compat_videos_router, execute_compat_video
from src.api.openai_videos import create_videos_router
from src.core.auth import verify_openai_api_key
from src.core.database import Database
from src.services.video_jobs import VideoJobService
from tests.test_media_sku_api import movie
from tests.test_openai_images import png

SKU = "flow-veo-3-1-fast-8s-720p"
EDIT_SKU = "flow-omni-1-1-flash-edit-720p"


class CompatVideoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name)
        self.asset = self.cache / ("a" * 32 + ".mp4")
        self.asset.write_bytes(movie())
        self.gate = asyncio.Event()
        self.calls = []
        self.warning = False
        self.closed = 0

        async def stream(normalized, base_url):
            self.calls.append(normalized)
            try:
                yield "data: " + json.dumps({"progress": 32}) + "\n\n"
                await self.gate.wait()
                result = {
                    "media": {
                        "type": "video",
                        "url": "https://api.example.test/tmp/" + self.asset.name,
                    }
                }
                if self.warning:
                    result["warnings"] = [{"code": "upscale_failed"}]
                yield "data: " + json.dumps(result) + "\n\n"
                yield "data: [DONE]\n\n"
            finally:
                self.closed += 1

        for binding in (
            patch.object(routes, "_iterate_openai_stream", stream),
            patch.object(
                routes,
                "generation_handler",
                SimpleNamespace(file_cache=SimpleNamespace(cache_dir=self.cache)),
            ),
        ):
            binding.start()
            self.addCleanup(binding.stop)
        self.service = VideoJobService(
            Database(str(self.cache / "jobs.db")),
            execute_compat_video,
            max_active=1,
            max_pending=10,
        )
        await self.service.start()
        self.addAsyncCleanup(self.service.close)
        self.app = FastAPI()
        self.app.dependency_overrides[verify_openai_api_key] = lambda: "synthetic-key"
        self.app.include_router(create_compat_videos_router(lambda: self.service))
        self.app.include_router(create_videos_router(lambda: self.service))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://api.example.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def create(self, **values):
        return await self.client.post(
            "/compat/v1/videos", json={"model": SKU, "prompt": "Synthetic cat", **values}
        )

    async def done(self, job_id):
        async with asyncio.timeout(3):
            while True:
                row = (await self.client.get("/compat/v1/videos/" + job_id)).json()
                if row["status"] in {"completed", "failed"}:
                    return row
                await asyncio.sleep(0.005)

    async def test_lifecycle_sku_identity_ownership_download_and_delete(self):
        response = await self.create(seconds="8", size="1280x720")
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual(row["model"], SKU)
        self.assertNotIn("usage", row)
        self.assertEqual(
            (await self.client.get("/compat/v1/videos/" + row["id"] + "/content")).status_code, 409
        )
        self.app.dependency_overrides[verify_openai_api_key] = lambda: "other-key"
        self.assertEqual((await self.client.get("/compat/v1/videos/" + row["id"])).status_code, 404)
        self.assertEqual((await self.client.get("/compat/v1/videos")).json()["data"], [])
        self.app.dependency_overrides[verify_openai_api_key] = lambda: "synthetic-key"
        self.gate.set()
        completed = await self.done(row["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["model"], SKU)
        self.assertEqual(completed["seconds"], "8")
        self.assertNotIn("usage", completed)
        self.assertNotIn("asset_path", completed)
        content = await self.client.get("/compat/v1/videos/" + row["id"] + "/content")
        self.assertEqual(content.content, movie())
        self.assertEqual(self.calls[0].public_model, SKU)
        self.assertEqual(self.calls[0].generation_options["durationSeconds"], 8)
        self.assertIsNone(self.calls[0].messages)
        self.assertEqual(
            (await self.client.get("/compat/v1/videos")).json()["data"][0]["model"], SKU
        )
        deleted = await self.client.delete("/compat/v1/videos/" + row["id"])
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual((await self.client.get("/compat/v1/videos/" + row["id"])).status_code, 404)

    async def test_price_conflicts_and_missing_edit_input_never_enter_queue(self):
        for values in (
            {"model": "veo_3_1"},
            {"model": "flow-banana-pro-1k"},
            {"seconds": "4"},
            {"size": "1920x1080"},
            {"generationConfig": {"videoConfig": {"quality": "lite"}}},
            {"generation_config": {"video_config": {"priority": "relaxed"}}},
            {"extraBody": {"duration_seconds": 4}},
            {"input_reference": []},
            {"input_reference": {"file_id": "fake"}},
            {"model": EDIT_SKU},
        ):
            with self.subTest(values=values):
                response = await self.create(**values)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.calls, [])
        self.assertEqual((await self.client.get("/compat/v1/videos")).json()["data"], [])

    async def test_idempotency_matches_sku_and_reference_bytes(self):
        body = {"model": SKU, "prompt": "Synthetic cat", "seconds": "8", "size": "1280x720"}
        headers = {"Idempotency-Key": "same-video"}
        first = await self.client.post("/compat/v1/videos", json=body, headers=headers)
        second = await self.client.post("/compat/v1/videos", json=body, headers=headers)
        self.assertEqual(first.json()["id"], second.json()["id"])
        conflicting = await self.client.post(
            "/compat/v1/videos",
            json={**body, "model": "flow-veo-3-1-lite-8s-720p"},
            headers=headers,
        )
        self.assertEqual(conflicting.status_code, 409, conflicting.text)
        different_prompt = await self.client.post(
            "/compat/v1/videos", json={**body, "prompt": "Different cat"}, headers=headers
        )
        self.assertEqual(different_prompt.status_code, 409, different_prompt.text)
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        source = png(128, 72)
        reference_body = {
            "model": SKU,
            "prompt": "Synthetic reference",
            "input_reference": {
                "image_url": "data:image/png;base64," + base64.b64encode(source).decode()
            },
        }
        reference_headers = {"Idempotency-Key": "reference-video"}
        original = await self.client.post(
            "/compat/v1/videos", json=reference_body, headers=reference_headers
        )
        duplicate = await self.client.post(
            "/compat/v1/videos", json=reference_body, headers=reference_headers
        )
        self.assertEqual(original.json()["id"], duplicate.json()["id"])
        changed = {
            **reference_body,
            "input_reference": {
                "image_url": "data:image/png;base64," + base64.b64encode(png(256, 144)).decode()
            },
        }
        rejected = await self.client.post(
            "/compat/v1/videos", json=changed, headers=reference_headers
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)

    async def test_legacy_standard_aliases_dispatch_sku_executor_and_retry_idempotently(self):
        self.gate.set()
        with patch.object(
            compat_videos, "execute_video", wraps=compat_videos.execute_video
        ) as executor:
            for index, (name, duration, mode) in enumerate(
                ((SKU, 8, "text_to_video"), (EDIT_SKU, 4, "edit")), start=1
            ):
                alias = name + "-standard"
                self.asset.write_bytes(movie(seconds=duration))
                body = {"model": alias, "prompt": "Synthetic legacy " + mode}
                request = {"json": body}
                if mode == "edit":
                    body["generationConfig"] = json.dumps(
                        {"videoConfig": {"trimStartMs": 0, "trimEndMs": 4000}}
                    )
                    request = {
                        "data": body,
                        "files": {"input_video": ("source.mp4", movie(), "video/mp4")},
                    }
                headers = {"Idempotency-Key": "legacy-" + mode}
                with self.subTest(model=alias):
                    first = await self.client.post("/compat/v1/videos", headers=headers, **request)
                    self.assertEqual(first.status_code, 200, first.text)
                    self.assertEqual(first.json()["model"], alias)
                    duplicate = await self.client.post(
                        "/compat/v1/videos", headers=headers, **request
                    )
                    self.assertEqual(duplicate.status_code, 200, duplicate.text)
                    self.assertEqual(duplicate.json()["id"], first.json()["id"])
                    completed = await self.done(first.json()["id"])
                    self.assertEqual(completed["status"], "completed", completed)
                    self.assertEqual(completed["model"], alias)
                    retry = await self.client.post("/compat/v1/videos", headers=headers, **request)
                    self.assertEqual(retry.status_code, 200, retry.text)
                    self.assertEqual(retry.json()["id"], first.json()["id"])
                    self.assertEqual(retry.json()["model"], alias)
                    self.assertEqual(len(self.calls), index)
                    self.assertEqual(self.calls[-1].public_model, alias)
                    self.assertEqual(self.calls[-1].generation_options["mode"], mode)
                    self.assertEqual(executor.call_count, index)
                    # Aggregate dispatch omits this argument and would skip paid-duration checks.
                    self.assertEqual(executor.call_args.kwargs, {"expected_duration": duration})

    async def test_invalid_reference_uploads_counts_and_unmatched_edit_trim_never_queue(self):
        response = await self.create(input_reference={"image_url": "data:image/png;base64,YmFk"})
        self.assertEqual(response.status_code, 400, response.text)
        for files, body in (
            ([("input_reference", ("bad.png", b"not an image", "image/png"))], {"model": SKU}),
            ([("input_reference[]", ("r.png", png(), "image/png"))] * 8, {"model": SKU}),
            (
                [("input_video", ("v.mp4", movie(seconds=4), "video/mp4"))],
                {
                    "model": EDIT_SKU,
                    "generationConfig": json.dumps({"videoConfig": {"trimEndMs": 8000}}),
                },
            ),
            (
                [("input_video", ("v.mp4", movie(), "video/mp4"))],
                {"model": EDIT_SKU, "seconds": "8"},
            ),
        ):
            response = await self.client.post(
                "/compat/v1/videos", data={"prompt": "Synthetic", **body}, files=files
            )
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.calls, [])

    async def test_ordered_multipart_first_last_frames_and_json_reference_array(self):
        first, second = png(128, 72), png(256, 144)
        response = await self.client.post(
            "/compat/v1/videos",
            data={
                "model": SKU,
                "prompt": "Synthetic transition",
                "seconds": "8",
                "size": "1280x720",
                "generationConfig": json.dumps({"videoConfig": {"mode": "first_last_frame"}}),
            },
            files=[
                ("input_reference[]", ("first.png", first, "image/png")),
                ("input_reference[]", ("second.png", second, "image/png")),
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.gate.set()
        self.assertEqual((await self.done(response.json()["id"]))["status"], "completed")
        self.assertEqual(self.calls[-1].images, [first, second])
        self.assertEqual(self.calls[-1].generation_options["mode"], "first_last_frame")
        references = [
            {"image_url": "data:image/png;base64," + base64.b64encode(data).decode()}
            for data in (first, second)
        ]
        json_response = await self.create(input_reference=references)
        self.assertEqual(json_response.status_code, 200, json_response.text)
        await self.done(json_response.json()["id"])
        self.assertEqual(self.calls[-1].generation_options["mode"], "reference_to_video")
        self.assertEqual(self.calls[-1].images, [first, second])

    async def test_edit_sku_requires_source_and_trim_duration_matches_result(self):
        self.asset.write_bytes(movie(seconds=4))
        response = await self.client.post(
            "/compat/v1/videos",
            data={
                "model": EDIT_SKU,
                "prompt": "Synthetic edit",
                "generationConfig": json.dumps(
                    {"videoConfig": {"trimStartMs": 0, "trimEndMs": 4000}}
                ),
            },
            files={"input_video": ("source.mp4", movie(seconds=8), "video/mp4")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["seconds"], "4")
        self.gate.set()
        done = await self.done(response.json()["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(self.calls[-1].generation_options["mode"], "edit")
        self.assertNotIn("durationSeconds", self.calls[-1].generation_options)
        self.assertEqual(self.calls[-1].public_model, EDIT_SKU)

    async def test_edit_default_uses_source_end_without_changing_aggregate_api(self):
        self.gate.set()
        for source_seconds, start_ms, result_seconds in ((8, 0, 8), (18, 5000, 10)):
            self.asset.write_bytes(movie(seconds=result_seconds))
            values = {"model": EDIT_SKU, "prompt": "Synthetic default edit"}
            if start_ms:
                values["generationConfig"] = json.dumps({"videoConfig": {"trimStartMs": start_ms}})
            response = await self.client.post(
                "/compat/v1/videos",
                data=values,
                files={"input_video": ("source.mp4", movie(seconds=source_seconds), "video/mp4")},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["seconds"], str(result_seconds))
            done = await self.done(response.json()["id"])
            self.assertEqual(done["status"], "completed", done)
            options = self.calls[-1].generation_options
            self.assertEqual(options["trimEndMs"], start_ms + result_seconds * 1000)
        unchanged = await self.client.post(
            "/v1/videos",
            data={"model": "omni_1.1_flash", "prompt": "Synthetic old edit"},
            files={"input_video": ("short.mp4", movie(seconds=8), "video/mp4")},
        )
        self.assertEqual(unchanged.status_code, 400, unchanged.text)

    async def test_edit_default_rejects_unknown_source_duration_before_queue(self):
        response = await self.client.post(
            "/compat/v1/videos",
            data={"model": EDIT_SKU, "prompt": "Synthetic unknown edit"},
            files={"input_video": ("bad.mp4", b"unreadable MP4 header", "video/mp4")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("duration", response.json()["error"]["message"])
        self.assertEqual(self.calls, [])

    async def test_wrong_duration_size_or_upscale_failure_never_completes(self):
        self.gate.set()
        for data, warning in ((movie(seconds=4), False), (movie(640, 360), False), (movie(), True)):
            self.asset.write_bytes(data)
            self.warning = warning
            created = await self.create()
            failed = await self.done(created.json()["id"])
            self.assertEqual(failed["status"], "failed", failed)
            self.assertNotIn("usage", failed)
            content = await self.client.get("/compat/v1/videos/" + failed["id"] + "/content")
            self.assertEqual(content.status_code, 409)
        self.assertEqual(self.closed, 3)

    async def test_aggregate_jobs_keep_original_duration_behavior_in_shared_service(self):
        self.asset.write_bytes(movie(seconds=4))
        self.gate.set()
        response = await self.client.post(
            "/v1/videos", json={"model": "veo_3_1", "prompt": "Synthetic old API", "seconds": "8"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        done = await self.done(response.json()["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(self.calls[-1].public_model, "veo_3_1")

    async def test_openapi_has_json_multipart_input_shapes_and_sku_enum(self):
        operation = self.app.openapi()["paths"]["/compat/v1/videos"]["post"]
        content = operation["requestBody"]["content"]
        self.assertIn("application/json", content)
        self.assertIn("multipart/form-data", content)
        properties = content["application/json"]["schema"]["properties"]
        self.assertIn(SKU, properties["model"]["enum"])
        self.assertIn(EDIT_SKU, properties["model"]["enum"])
        self.assertFalse(any(name.endswith("-standard") for name in properties["model"]["enum"]))
        self.assertIn("flow-veo-3-1-fast-8s-720p-relaxed", properties["model"]["enum"])
        multipart = content["multipart/form-data"]["schema"]["properties"]
        self.assertEqual(multipart["input_video"]["format"], "binary")
        self.assertEqual(multipart["input_reference[]"]["maxItems"], 7)


if __name__ == "__main__":
    unittest.main()
