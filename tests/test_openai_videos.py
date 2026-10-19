"""Public video HTTP contracts with durable SQLite and synthetic execution only."""

import asyncio
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api import routes
from src.api.openai_videos import create_videos_router, execute_video, video_dimensions
from src.core.auth import verify_openai_api_key
from src.core.database import Database
from src.services.video_jobs import VideoJobService


def movie(width=640, height=360):
    def box(kind, data):
        return struct.pack(">I4s", len(data) + 8, kind) + data

    header = bytes(76) + struct.pack(">II", width << 16, height << 16)
    return box(b"moov", box(b"trak", box(b"tkhd", header)))


class VideoHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        self.db = Database(str(self.cache / "flow.db"))
        self.file = self.cache / ("a" * 32 + ".mp4")
        self.file.write_bytes(movie())
        self.calls = []
        self.gate = asyncio.Event()

        async def executor(normalized, base_url):
            self.calls.append(normalized)
            yield {"type": "progress", "progress": 25}
            await self.gate.wait()
            yield {"type": "completed", "media": {"type": "video", "asset_path": str(self.file)}}

        self.service = VideoJobService(self.db, executor, max_active=1, max_pending=2)
        await self.service.start()
        self.addAsyncCleanup(self.service.close)
        self.app = FastAPI()
        self.app.include_router(create_videos_router(lambda: self.service))
        self.app.dependency_overrides[verify_openai_api_key] = lambda: "synthetic-owner"
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )
        self.addAsyncCleanup(self.client.aclose)
        patcher = patch.object(
            routes,
            "generation_handler",
            SimpleNamespace(file_cache=SimpleNamespace(cache_dir=self.cache)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def create(self, **kwargs):
        body = {"model": "omni_1.1_flash", "prompt": "A rabbit", "seconds": "4", "size": "640x360"}
        body.update(kwargs)
        return await self.client.post("/v1/videos", json=body)

    async def done(self, job_id):
        for _ in range(100):
            row = (await self.client.get("/v1/videos/" + job_id)).json()
            if row.get("status") in {"completed", "failed"}:
                return row
            await asyncio.sleep(0.005)
        self.fail("synthetic worker did not finish")

    async def test_lifecycle_download_and_ownership(self):
        response = await self.create()
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["object"], "video")
        self.assertIn("x-request-id", response.headers)
        self.assertEqual(
            (await self.client.get(f"/v1/videos/{row['id']}/content")).status_code, 409
        )
        self.assertEqual((await self.client.delete(f"/v1/videos/{row['id']}")).status_code, 409)
        self.app.dependency_overrides[verify_openai_api_key] = lambda: "different-owner"
        self.assertEqual((await self.client.get(f"/v1/videos/{row['id']}")).status_code, 404)
        self.assertEqual((await self.client.get("/v1/videos")).json()["data"], [])
        self.app.dependency_overrides[verify_openai_api_key] = lambda: "synthetic-owner"
        self.gate.set()
        result = await self.done(row["id"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("asset_path", result)
        content = await self.client.get(f"/v1/videos/{row['id']}/content")
        self.assertEqual(content.content, movie())
        self.assertIn("video/mp4", content.headers["content-type"])
        self.assertEqual(
            (await self.client.delete(f"/v1/videos/{row['id']}")).json()["object"], "video.deleted"
        )
        self.assertEqual((await self.client.get(f"/v1/videos/{row['id']}")).status_code, 404)

    async def test_invalid_parameters_fail_before_admission(self):
        for field in (
            {"seconds": "12"},
            {"seconds": True},
            {"model": "sora-2"},
            {"size": "1792x1024"},
            {"mask": "unsupported"},
            {"input_reference": {"file_id": "file_test"}},
            {"prompt": " "},
        ):
            response = await self.create(**field)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("error", response.json())
        self.assertEqual(self.calls, [])

    async def test_multipart_reference_and_idempotency(self):
        png = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 32, 32)
            + bytes(9)
        )
        options = {
            "model": "omni_1.1_flash",
            "prompt": "A rabbit",
            "size": "640x360",
            "seconds": "4",
        }
        kwargs = {
            "data": options,
            "files": {"input_reference": ("input.png", png, "image/png")},
            "headers": {"Idempotency-Key": "synthetic"},
        }
        first = await self.client.post("/v1/videos", **kwargs)
        second = await self.client.post("/v1/videos", **kwargs)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.gate.set()
        await self.done(first.json()["id"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].images, [png])
        self.assertEqual(self.calls[0].generation_options["mode"], "image_to_video")
        conflict = await self.client.post(
            "/v1/videos", **{**kwargs, "data": {**options, "prompt": "Other"}}
        )
        self.assertEqual(conflict.status_code, 409)

    async def test_list_cursor_and_expired_content_contract(self):
        first = (await self.create()).json()
        self.gate.set()
        await self.done(first["id"])

        second = (await self.create(prompt="Another rabbit")).json()
        await self.done(second["id"])

        complete = await self.client.get("/v1/videos", params={"limit": 100, "order": "asc"})
        complete_ids = [item["id"] for item in complete.json()["data"]]
        self.assertEqual(set(complete_ids), {first["id"], second["id"]})
        page = await self.client.get("/v1/videos", params={"limit": 1, "order": "asc"})
        self.assertEqual(page.status_code, 200, page.text)
        payload = page.json()
        self.assertEqual(payload["object"], "list")
        self.assertEqual(len(payload["data"]), 1)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["first_id"], payload["data"][0]["id"])
        self.assertEqual(payload["last_id"], payload["data"][-1]["id"])

        next_page = await self.client.get(
            "/v1/videos",
            params={"limit": 1, "order": "asc", "after": payload["last_id"]},
        )
        self.assertEqual(next_page.status_code, 200, next_page.text)
        self.assertEqual(next_page.json()["data"][0]["id"], complete_ids[1])
        invalid_cursor = await self.client.get(
            "/v1/videos", params={"after": "video_missing_cursor"}
        )
        self.assertEqual(invalid_cursor.status_code, 404)
        self.assertEqual(invalid_cursor.json()["error"]["code"], "video_not_found")

        async with self.db._connect(write=True) as db:
            await db.execute(
                "UPDATE video_jobs SET expires_at=? WHERE id=?",
                (int(time.time()) - 1, first["id"]),
            )
            await db.commit()
        expired = await self.client.get(f"/v1/videos/{first['id']}/content")
        self.assertEqual(expired.status_code, 410)
        self.assertEqual(expired.json()["error"]["code"], "asset_expired")

    async def test_executor_reads_structured_asset_and_checks_actual_size(self):
        normalized = routes.NormalizedGenerationRequest(
            "omni_4s_360p",
            "Rabbit",
            [],
            public_model="omni_1.1_flash",
            generation_options={"aspectRatio": "16:9", "resolution": "360p"},
        )

        async def chunks(*args):
            yield (
                "data: "
                + json.dumps(
                    {
                        "media": {
                            "type": "video",
                            "url": "http://testserver/tmp/" + self.file.name,
                        },
                        "progress": 100,
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        with patch.object(routes, "_iterate_openai_stream", chunks):
            result = [item async for item in execute_video(normalized, "http://testserver")]
            self.assertEqual(result[-1]["type"], "completed")
            self.file.write_bytes(movie(320, 180))
            with self.assertRaises(Exception) as exc:
                _ = [item async for item in execute_video(normalized, "http://testserver")]
            self.assertEqual(exc.exception.code, "output_size_mismatch")
        self.assertEqual(video_dimensions(movie()), (640, 360))
        self.assertIsNone(video_dimensions(b"not a movie"))


if __name__ == "__main__":
    unittest.main()
