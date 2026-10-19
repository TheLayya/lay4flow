"""Durable Studio media archives use original results, never new generation."""

import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from src.api import admin, routes
from src.api.admin_routes.studio import create_studio_router
from src.services.file_cache import FileCache
from src.services.studio_media import StudioMediaArchive, StudioMediaError


class StudioMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache_dir = self.root / "tmp"
        self.cache_dir.mkdir()
        self.database = SimpleNamespace(
            db_path=str(self.root / "data" / "flow.db"),
            get_logs=AsyncMock(return_value=[]),
            get_token=AsyncMock(return_value=SimpleNamespace(st="synthetic-session")),
        )
        self.cache = SimpleNamespace(cache_dir=self.cache_dir, download_and_cache=AsyncMock())
        self.handler = SimpleNamespace(
            db=self.database,
            file_cache=self.cache,
            flow_client=SimpleNamespace(get_media_url_redirect=AsyncMock()),
            _account_proxy_context=Mock(return_value=nullcontext()),
        )
        self.archive = StudioMediaArchive(self.handler)
        self.name = "a" * 32 + ".jpg"
        self.source = "http://studio.example/tmp/" + self.name

    def local(self, name=None, content=b"synthetic image bytes"):
        path = self.cache_dir / (name or self.name)
        path.write_bytes(content)
        return path

    def log(self, assets, source=None):
        self.database.get_logs.return_value = [
            {
                "status_code": 200,
                "token_id": 7,
                "response_body": json.dumps(
                    {"status": "success", "url": source or self.source, "generated_assets": assets}
                ),
            }
        ]

    async def test_existing_file_archives_atomically_independent_of_tmp_cleanup_and_restart(self):
        original = self.local()
        results = await asyncio.gather(*[self.archive.archive(self.source) for _ in range(8)])
        self.assertEqual(len({result["url"] for result in results}), 1)
        original.unlink()
        restored = StudioMediaArchive(self.handler)
        path, mime = restored.content(results[0]["id"])
        self.assertEqual(path.read_bytes(), b"synthetic image bytes")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(len(list(self.archive.root.iterdir())), 1)
        self.database.get_logs.assert_not_awaited()
        self.cache.download_and_cache.assert_not_awaited()
        self.assertTrue((await restored.archive(self.source))["cached"])

    async def test_matching_old_image_downloads_logged_origin_on_original_account(self):
        origin = "https://storage.googleapis.com/fixture-image?signature=private"
        self.log({"type": "image", "origin_image_url": origin, "final_image_url": self.source})
        self.cache.download_and_cache.return_value = self.local().name
        # The requested /tmp result has expired; a distinct new cache file is
        # created only while recovering the logged original image.
        self.local().unlink()

        async def download(_url, _kind):
            return self.local("b" * 32 + ".jpg").name

        self.cache.download_and_cache.side_effect = download
        result = await self.archive.archive(self.source)
        self.assertEqual(
            self.archive.content(result["id"])[0].read_bytes(), b"synthetic image bytes"
        )
        self.cache.download_and_cache.assert_awaited_once_with(origin, "image")
        self.database.get_token.assert_awaited_once_with(7)
        self.handler.flow_client.get_media_url_redirect.assert_not_awaited()
        self.assertNotIn("signature", json.dumps(result))
        self.assertNotIn("synthetic-session", json.dumps(result))

    async def test_matching_video_resolves_original_media_using_its_logged_account(self):
        self.name = "c" * 32 + ".mp4"
        self.source = "/tmp/" + self.name
        self.log({"type": "video", "mediaName": "original-media", "final_video_url": self.source})
        self.handler.flow_client.get_media_url_redirect.return_value = (
            "https://storage.googleapis.com/original.mp4?signature=private"
        )

        async def download(_url, _kind):
            return self.local("d" * 32 + ".mp4", b"synthetic mp4").name

        self.cache.download_and_cache.side_effect = download
        result = await self.archive.archive(self.source)
        self.assertEqual(result["kind"], "video")
        self.handler.flow_client.get_media_url_redirect.assert_awaited_once_with(
            "synthetic-session", "original-media", media_url_type="MEDIA_URL_TYPE_FULL_MEDIA"
        )
        self.assertEqual(self.cache.download_and_cache.await_args.args[1], "video")

    async def test_unmatched_url_path_traversal_and_missing_account_never_download(self):
        for url in (
            "file:///private/file.jpg",
            "/tmp/../setting.toml",
            "http://127.0.0.1/private",
            "/tmp/" + self.name + "?download=1",
        ):
            with self.subTest(url=url), self.assertRaises(StudioMediaError):
                await self.archive.archive(url)
        self.cache.download_and_cache.assert_not_awaited()
        self.log(
            {"type": "image", "origin_image_url": "https://storage.googleapis.com/original.jpg"}
        )
        self.database.get_token.return_value = None
        with self.assertRaises(StudioMediaError) as error:
            await self.archive.archive(self.source)
        self.assertEqual(error.exception.status, 409)
        self.cache.download_and_cache.assert_not_awaited()

    async def test_recovery_failure_exposes_no_upstream_credentials(self):
        self.log(
            {
                "type": "image",
                "origin_image_url": "https://storage.googleapis.com/original.jpg?secret=PRIVATE",
            }
        )
        self.cache.download_and_cache.side_effect = RuntimeError(
            "signed url PRIVATE and synthetic-session"
        )
        with self.assertRaises(StudioMediaError) as error:
            await self.archive.archive(self.source)
        self.assertEqual(error.exception.status, 502)
        self.assertNotIn("PRIVATE", str(error.exception))

    async def test_temporary_expiry_cleanup_leaves_archive_untouched(self):
        self.local()
        result = await self.archive.archive(self.source)
        cache = FileCache(str(self.cache_dir), default_timeout=1)
        with patch("src.services.file_cache.time.time", return_value=9999999999):
            await cache._cleanup_expired_files()
        self.assertFalse((self.cache_dir / self.name).exists())
        self.assertTrue(self.archive.content(result["id"])[0].exists())

    async def test_cookie_archive_and_sse_automatically_return_durable_content_urls(self):
        self.local()
        app = FastAPI()
        app.include_router(create_studio_router(admin.verify_admin_token))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://studio.example"
        ) as client:
            with (
                patch.object(admin, "active_admin_tokens", {"archive-session"}),
                patch.object(routes, "generation_handler", self.handler),
            ):
                response = await client.post(
                    "/api/admin/studio/media/archive", json={"url": self.source}
                )
                self.assertEqual(response.status_code, 401)
                client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "archive-session")
                response = await client.post(
                    "/api/admin/studio/media/archive",
                    json={"url": self.source},
                    headers={"Origin": "http://foreign.example"},
                )
                self.assertEqual(response.status_code, 403)
                response = await client.post(
                    "/api/admin/studio/media/archive",
                    json={"url": self.source},
                    headers={"Origin": "http://studio.example"},
                )
                self.assertEqual(response.status_code, 200)
                output = response.json()
                self.assertEqual(
                    (await client.get(output["url"])).content, b"synthetic image bytes"
                )
                partial = await client.get(output["url"], headers={"Range": "bytes=0-8"})
                self.assertEqual(partial.status_code, 206)
                self.assertEqual(partial.content, b"synthetic")
                self.assertEqual(
                    (
                        await client.get("/api/admin/studio/media/" + "0" * 64 + ".jpg/content")
                    ).status_code,
                    404,
                )
                # This second file was never explicitly archived: only the
                # final generation SSE wrapper can preserve it.
                stream_source = "http://studio.example/tmp/" + "e" * 32 + ".jpg"
                stream_file = self.local("e" * 32 + ".jpg", b"stream-only artwork")
                event = {
                    "media": {"url": stream_source, "kind": "image"},
                    "choices": [
                        {
                            "delta": {
                                "content": f"![Generated Image]({stream_source})",
                                "media": {"url": stream_source, "kind": "image"},
                            }
                        }
                    ],
                }

                async def stream(*_args):
                    return StreamingResponse(
                        iter(["data: " + json.dumps(event) + "\n\n", "data: [DONE]\n\n"]),
                        media_type="text/event-stream",
                        headers={"X-Accel-Buffering": "no"},
                    )

                with patch.object(routes, "create_chat_completion", AsyncMock(side_effect=stream)):
                    streamed = await client.post(
                        "/api/admin/studio/chat/completions",
                        json={"model": "fixture", "messages": [], "stream": True},
                        headers={"Origin": "http://studio.example"},
                    )
                self.assertEqual(streamed.status_code, 200)
                self.assertEqual(streamed.headers["x-accel-buffering"], "no")
                final = json.loads(streamed.text.splitlines()[0].removeprefix("data: "))
                preserved_url = final["media"]["url"]
                self.assertEqual(final["choices"][0]["delta"]["media"]["url"], preserved_url)
                self.assertNotIn(stream_source, streamed.text)
                stream_file.unlink()
                self.assertEqual((await client.get(preserved_url)).content, b"stream-only artwork")
                self.assertIn("data: [DONE]\n\n", streamed.text)
                client.cookies.clear()
                self.assertEqual((await client.get(output["url"])).status_code, 401)

    async def test_sse_archive_disk_failure_preserves_successful_original_event(self):
        from src.api.admin_routes.studio import _archive_stream_line

        self.local()
        event = "data: " + json.dumps(
            {"choices": [{"delta": {"content": f"![result]({self.source})"}}]}
        )
        with patch.object(self.archive, "_copy_atomic", side_effect=OSError("private disk error")):
            self.assertEqual(await _archive_stream_line(event, self.archive), event)


if __name__ == "__main__":
    unittest.main()
