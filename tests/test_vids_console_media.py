"""Cookie-authenticated Vids console media reuse the public account dispatcher."""

import asyncio
import base64
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.api import admin
from src.api.vids_media import CONSOLE_PREFIX, PREFIX, PUBLIC_PREFIX, create_vids_media_router
from src.services.vids.client import VidsTransportError
from src.services.vids.dispatcher import VidsPoolDispatcher
from tests import test_vids_media_api as media_tests
from tests.test_vids_images_api import JPEG, configure_image_edit
from tests.test_vids_media_api import IMAGE, MP4, SPEECH, VIDEO, identifier


class VidsConsoleMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse the synthetic account/media setup, not a real session or service key.
        self.media = media_tests.VidsMediaAPITests()
        await self.media.asyncSetUp()
        self.app = self.media.app
        self.app.include_router(admin.auth_router)
        self.app.include_router(
            create_vids_media_router(
                self.media.dispatcher,
                admin.verify_admin_token,
                prefix=CONSOLE_PREFIX,
                auto_pool=True,
                tags=["Vids console"],
            ),
            include_in_schema=False,
        )
        self.admin_state = patch.multiple(
            admin,
            active_admin_tokens=set(),
            db=SimpleNamespace(
                get_admin_config=AsyncMock(return_value=SimpleNamespace(username="test-admin"))
            ),
        )
        self.admin_state.start()
        self.credentials = patch.object(admin.AuthManager, "verify_admin", return_value=True)
        self.credentials.start()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://console.example.test"
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        self.credentials.stop()
        self.admin_state.stop()
        await self.media.asyncTearDown()
        self.media.doCleanups()

    @staticmethod
    def url(path, token_id=1):
        return CONSOLE_PREFIX + "/v1" + path

    async def login(self):
        response = await self.http.post(
            "/api/admin/login", json={"username": "test-admin", "password": "test-password"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            any(cookie.name == admin.ADMIN_SESSION_COOKIE_NAME for cookie in self.http.cookies.jar)
        )
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    async def create(self, path, key, body, token_id=1):
        response = await self.http.post(
            self.url(path, token_id), json=body, headers={"Idempotency-Key": key}
        )
        self.assertNotIn("Authorization", response.request.headers)
        return response

    def receipt_owner(self, key):
        owners = [
            token_id
            for token_id, gateway in self.media.gateways.items()
            if gateway._path(identifier(key)).exists()
        ]
        self.assertEqual(len(owners), 1, owners)
        return owners[0]

    async def test_fresh_requests_rotate_independently_for_each_media_kind(self):
        await self.login()
        media = (
            ("image", "/images/generations", IMAGE, "generate_image"),
            ("video", "/videos", VIDEO, "submit_text"),
            ("speech", "/audio/speech", SPEECH, "generate_speech"),
        )
        selected = {kind: [] for kind, *_ in media}
        for index in range(4):
            for kind, path, body, _ in media:
                with self.subTest(kind=kind, index=index):
                    key = f"fair-{kind}-{index}"
                    response = await self.create(path, key, body)
                    self.assertEqual(response.status_code, 200, response.text)
                    selected[kind].append(self.receipt_owner(key))
        for kind, _, _, method in media:
            self.assertEqual(selected[kind], [1, 2, 1, 2])
            for gateway in self.media.gateways.values():
                self.assertEqual(getattr(gateway.client, method).await_count, 2)

    async def test_completed_and_unknown_replays_do_not_advance_pool_selection(self):
        await self.login()
        first = await self.create("/videos", "fair-completed", VIDEO)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.receipt_owner("fair-completed"), 1)
        for _ in range(2):
            replay = await self.create("/videos", "fair-completed", VIDEO)
            self.assertEqual(replay.json(), first.json())
        self.media.gateways[2].client.submit_text.side_effect = VidsTransportError(
            "synthetic interruption"
        )
        unknown = await self.create("/videos", "fair-unknown", VIDEO)
        self.assertEqual(unknown.status_code, 202, unknown.text)
        self.assertEqual(unknown.json()["status"], "unknown")
        self.assertEqual(self.receipt_owner("fair-unknown"), 2)
        for _ in range(2):
            replay = await self.create("/videos", "fair-unknown", VIDEO)
            self.assertEqual(replay.json(), unknown.json())
        next_request = await self.create("/videos", "fair-after-replays", VIDEO)
        self.assertEqual(next_request.status_code, 200, next_request.text)
        self.assertEqual(self.receipt_owner("fair-after-replays"), 1)
        self.assertEqual(self.media.gateways[1].client.submit_text.await_count, 2)
        self.media.gateways[2].client.submit_text.assert_awaited_once()

    async def test_public_and_console_pool_wrappers_share_selection_cursor(self):
        await self.login()
        self.app.include_router(
            create_vids_media_router(self.media.dispatcher, prefix=PUBLIC_PREFIX, auto_pool=True)
        )
        selected = []
        for index in range(4):
            key = f"fair-shared-{index}"
            if index % 2:
                response = await self.media.http.post(
                    PUBLIC_PREFIX + "/v1/videos", json=VIDEO, headers={"Idempotency-Key": key}
                )
            else:
                response = await self.create("/videos", key, VIDEO)
            self.assertEqual(response.status_code, 200, response.text)
            selected.append(self.receipt_owner(key))
        self.assertEqual(selected, [1, 2, 1, 2])

    async def test_saved_image_source_affinity_does_not_advance_pool_selection(self):
        await self.login()
        first = await self.create("/images/generations", "fair-before-source", IMAGE)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.receipt_owner("fair-before-source"), 1)
        configure_image_edit(self.media.gateways[2].client)
        source = self.media.dispatcher.document_reference(2, "saved-shape")
        pool = VidsPoolDispatcher(self.media.dispatcher)
        body = {"kind": "image", "mode": "edit", "prompt": "Blue boat", "source_id": source}
        edited = await pool.generate(None, body, "fair-bound-source")
        self.assertEqual(edited["status"], "completed")
        self.assertEqual(self.receipt_owner("fair-bound-source"), 2)
        self.assertEqual(await pool.generate(None, body, "fair-bound-source"), edited)
        following = await self.create("/images/generations", "fair-after-source", IMAGE)
        self.assertEqual(following.status_code, 200, following.text)
        self.assertEqual(self.receipt_owner("fair-after-source"), 2)
        self.media.gateways[2].client.edit_document_image.assert_awaited_once_with(
            "saved-shape", "Blue boat"
        )

    async def test_rotation_skips_disabled_generation_and_preflight_busy_accounts(self):
        await self.login()
        self.media.tokens[1].is_active = False
        response = await self.create("/videos", "fair-disabled", VIDEO)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.receipt_owner("fair-disabled"), 2)
        self.media.tokens[1].is_active = True
        await self.media.gateways[2].lock.acquire()
        try:
            response = await self.create("/videos", "fair-generation-busy", VIDEO)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.receipt_owner("fair-generation-busy"), 1)
        finally:
            self.media.gateways[2].lock.release()
        preflight = self.media.dispatcher._preflight_locks.setdefault(2, asyncio.Lock())
        await preflight.acquire()
        try:
            response = await self.create("/videos", "fair-preflight-busy", VIDEO)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.receipt_owner("fair-preflight-busy"), 1)
        finally:
            preflight.release()
        response = await self.create("/videos", "fair-after-busy", VIDEO)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.receipt_owner("fair-after-busy"), 2)

    async def test_concurrent_fresh_keys_do_not_queue_on_busy_preflight_accounts(self):
        await self.login()
        self.media.dispatcher.repair_session = AsyncMock()
        waiting = {token_id: asyncio.Event() for token_id in (1, 2)}
        release = asyncio.Event()

        def quota_for(token_id):
            async def quota(kind):
                self.assertEqual(kind, "video")
                waiting[token_id].set()
                await release.wait()
                return {"available": 10}

            return quota

        for token_id, gateway in self.media.gateways.items():
            gateway.quota = AsyncMock(side_effect=quota_for(token_id))
        first = asyncio.create_task(self.create("/videos", "fair-concurrent-one", VIDEO))
        second = None
        try:
            await asyncio.wait_for(waiting[1].wait(), 2)
            second = asyncio.create_task(self.create("/videos", "fair-concurrent-two", VIDEO))
            await asyncio.wait_for(waiting[2].wait(), 2)
            busy = await asyncio.wait_for(self.create("/videos", "fair-concurrent-full", VIDEO), 2)
            self.assertEqual(busy.status_code, 429, busy.text)
            self.assertEqual(busy.json()["error"]["code"], "busy")
        finally:
            release.set()
            results = await asyncio.gather(*(task for task in (first, second) if task))
        self.assertTrue(all(response.status_code == 200 for response in results))
        self.assertEqual(self.receipt_owner("fair-concurrent-one"), 1)
        self.assertEqual(self.receipt_owner("fair-concurrent-two"), 2)
        for gateway in self.media.gateways.values():
            gateway.quota.assert_awaited_once()
            gateway.client.submit_text.assert_awaited_once()
            self.assertFalse(gateway._path(identifier("fair-concurrent-full")).exists())
        self.media.dispatcher.repair_session.assert_not_awaited()

    async def test_admin_cookie_guards_all_media_routes_and_public_key_cannot_replace_it(self):
        with patch.object(
            self.media.dispatcher, "list", wraps=self.media.dispatcher.list
        ) as lookup:
            for route in self.app.routes:
                if not route.path.startswith(CONSOLE_PREFIX):
                    continue
                path = (
                    route.path.replace("{token_id}", "1")
                    .replace("{generation_id}", identifier("missing"))
                    .replace("{video_id}", identifier("missing"))
                )
                method = next(iter(route.methods))
                response = await self.http.request(method, path)
                self.assertEqual(response.status_code, 401, (method, path, response.text))
                self.assertIn("error", response.json())
                self.assertTrue(response.headers["x-request-id"].startswith("req_"))
            lookup.assert_not_called()
        from tests.test_vids_gateway import KEY

        denied = await self.http.get(
            self.url("/models"), headers={"Authorization": "Bearer " + KEY}
        )
        self.assertEqual(denied.status_code, 401)
        self.http.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "expired-session")
        self.assertEqual((await self.http.get(self.url("/models"))).status_code, 401)
        await self.login()
        models = await self.http.get(self.url("/models"))
        self.assertEqual(models.status_code, 200)
        self.assertNotIn("Authorization", models.request.headers)
        self.assertEqual(
            {row["id"] for row in models.json()["data"]},
            {"vids-image", "vids-video", "vids-speech"},
        )
        for row in models.json()["data"]:
            self.assertTrue(
                all(path.startswith(self.url("")) for path in row["supported_endpoints"])
            )
        self.assertEqual(
            (await self.http.get(PREFIX.format(token_id=1) + "/v1/models")).status_code, 401
        )
        self.assertEqual((await self.http.post("/api/admin/logout")).status_code, 200)
        self.assertEqual((await self.http.get(self.url("/models"))).status_code, 401)
        for gateway in self.media.gateways.values():
            gateway.client.generate_image.assert_not_awaited()
            gateway.client.generate_speech.assert_not_awaited()
            gateway.client.submit_text.assert_not_awaited()

    async def test_image_url_receipt_and_speech_download_use_same_admin_cookie(self):
        await self.login()
        first = await self.create("/images/generations", "console-image", IMAGE)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["x-vids-generation-id"], identifier("console-image"))
        expected = "https://console.example.test" + self.url(
            "/vids/generations/" + identifier("console-image") + "/content"
        )
        self.assertEqual(first.json()["data"][0]["url"], expected)
        self.assertEqual((await self.http.get(expected)).content, JPEG)
        receipt = await self.create(
            "/vids/generations", "console-image", {"kind": "image", "prompt": IMAGE["prompt"]}
        )
        for field in ("receipt_url", "content_url"):
            self.assertTrue(receipt.json()[field].startswith(self.url("")))
        retrieved = await self.http.get(receipt.json()["receipt_url"])
        self.assertEqual(retrieved.json(), receipt.json())
        replay = await self.create(
            "/images/generations", "console-image", {**IMAGE, "response_format": "b64_json"}
        )
        self.assertEqual(base64.b64decode(replay.json()["data"][0]["b64_json"]), JPEG)
        speech = await self.create("/audio/speech", "console-speech", SPEECH)
        self.assertEqual(speech.status_code, 200, speech.text)
        self.assertEqual(speech.headers["x-vids-generation-id"], identifier("console-speech"))
        self.assertEqual(speech.headers["content-type"], "audio/wav")
        audio_receipt = await self.http.get(
            self.url("/vids/generations/" + identifier("console-speech"))
        )
        self.assertEqual(
            (await self.http.get(audio_receipt.json()["content_url"])).content, speech.content
        )
        # URL generation must not accidentally resolve the earlier public router's name.
        public = await self.media.create("/images/generations", "console-image", IMAGE)
        self.assertTrue(
            public.json()["data"][0]["url"].startswith(
                "https://media.example.test" + PREFIX.format(token_id=1)
            )
        )
        self.media.gateways[1].client.generate_image.assert_awaited_once()
        self.media.gateways[1].client.generate_speech.assert_awaited_once()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as anonymous:
            self.assertEqual((await anonymous.get(expected)).status_code, 401)

    async def test_uploaded_image_edit_uses_pool_without_document_source_request(self):
        await self.login()
        for gateway in self.media.gateways.values():
            configure_image_edit(gateway.client)
        self.media.tokens[1].is_active = False
        key = "console-uploaded-edit"
        response = await self.http.post(
            self.url("/images/edits"),
            data=IMAGE,
            files={"image": ("source.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("Authorization", response.request.headers)
        self.assertEqual(response.headers["x-vids-generation-id"], identifier(key))
        url = response.json()["data"][0]["url"]
        self.assertTrue(url.startswith("https://console.example.test" + self.url("")))
        self.assertEqual((await self.http.get(url)).content, JPEG)
        replay = await self.http.post(
            self.url("/images/edits"),
            data={**IMAGE, "response_format": "b64_json"},
            files={"image[]": ("renamed.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(base64.b64decode(replay.json()["data"][0]["b64_json"]), JPEG)
        self.media.gateways[2].client.insert_document_image.assert_awaited_once()
        self.media.gateways[2].client.edit_document_image.assert_awaited_once()
        self.media.gateways[1].client.insert_document_image.assert_not_awaited()
        self.media.gateways[1].client.edit_document_image.assert_not_awaited()

    async def test_video_json_multipart_list_and_recovery_keep_console_account_urls(self):
        await self.login()
        first = await self.create("/videos", "console-video", VIDEO)
        self.assertEqual(first.status_code, 200, first.text)
        result = first.json()
        for field in ("receipt_url", "content_url"):
            self.assertTrue(result[field].startswith(self.url("")))
        self.assertEqual((await self.http.get(result["content_url"])).content, MP4)
        self.assertEqual((await self.http.get(self.url("/videos/" + result["id"]))).json(), result)
        multipart = await self.http.post(
            self.url("/videos"),
            files={name: (None, value) for name, value in VIDEO.items()},
            headers={"Idempotency-Key": "console-multipart"},
        )
        self.assertEqual(multipart.status_code, 200, multipart.text)
        listed = await self.http.get(self.url("/videos"), params={"limit": 1})
        self.assertTrue(listed.json()["has_more"])
        self.assertTrue(listed.json()["data"][0]["receipt_url"].startswith(self.url("")))
        self.assertEqual((await self.http.get(result["receipt_url"])).json()["kind"], "video")
        self.assertEqual(
            (
                await self.http.get(result["content_url"], params={"variant": "thumbnail"})
            ).status_code,
            400,
        )
        upstream = self.media.gateways[1].client
        download = upstream.download_video.side_effect
        upstream.download_video.side_effect = VidsTransportError("private download URL")
        failed = await self.create("/videos", "console-download", VIDEO)
        retry_url = failed.json()["retry_download_url"]
        self.assertTrue(retry_url.startswith(self.url("")))
        self.media.tokens[1].is_active = False
        upstream.download_video.side_effect = download
        recovered = await self.http.post(retry_url)
        self.assertEqual(recovered.json()["status"], "completed")
        self.assertEqual((await self.http.get(recovered.json()["content_url"])).content, MP4)
        self.assertEqual(upstream.submit_text.await_count, 2)
        self.assertEqual(upstream.download_video.await_count, 3)
        self.media.gateways[2].client.submit_text.assert_awaited_once()

    async def test_unknown_disabled_replay_and_cross_account_keys_cannot_generate_again(self):
        await self.login()
        upstream = self.media.gateways[1].client
        upstream.submit_text.side_effect = VidsTransportError("private URL")
        first = await self.create("/videos", "console-unknown", VIDEO)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["status"], "unknown")
        self.media.tokens[1].is_active = False
        replay = await self.create("/videos", "console-unknown", VIDEO)
        self.assertEqual(replay.json(), first.json())
        switched = await self.media.create("/videos", "console-unknown", VIDEO, 2)
        self.assertEqual(switched.status_code, 409)
        self.assertEqual(switched.json()["error"]["code"], "account_affinity_conflict")
        self.assertEqual(
            (
                await self.media.http.get(self.media.url("/videos/" + first.json()["id"], 2))
            ).status_code,
            404,
        )
        retry = await self.http.post(first.json()["receipt_url"] + "/download")
        self.assertEqual(retry.status_code, 409)
        self.media.tokens[2].is_active = False
        denied = await self.create("/videos", "console-new-disabled", VIDEO)
        self.assertEqual(denied.status_code, 503)
        self.assertEqual(denied.json()["error"]["code"], "account_disabled")
        upstream.submit_text.assert_awaited_once()
        self.media.gateways[2].client.submit_text.assert_not_awaited()
        self.assertNotIn("private", first.text + switched.text + retry.text)

    async def test_admin_route_uses_bounded_parsers_and_stays_out_of_public_schema(self):
        self.assertFalse(
            any(path.startswith("/api/admin/vids/v1/") for path in self.app.openapi()["paths"])
        )
        await self.login()
        cases = (
            ("/images/generations", {**IMAGE, "n": True}),
            ("/audio/speech", {**SPEECH, "voice": "unverified"}),
            ("/videos", {**VIDEO, "seconds": 11}),
            ("/vids/generations", {"kind": "music", "prompt": "x"}),
        )
        for suffix, body in cases:
            response = await self.create(suffix, "console-invalid", body)
            self.assertEqual(response.status_code, 400, response.text)
        missing_key = await self.http.post(self.url("/videos"), json=VIDEO)
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_required")
        invalid_path = await self.media.http.get(PREFIX.format(token_id="invalid") + "/v1/models")
        self.assertEqual(invalid_path.status_code, 400)
        oversized = await self.http.post(
            self.url("/videos"),
            content=b"{}",
            # Declared oversized body is rejected before JSON parsing.,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "console-large",
                "Content-Length": str(85 * 1024**2 + 1),
            },
        )
        self.assertEqual(oversized.status_code, 413)
        file = await self.http.post(
            self.url("/videos"),
            files={"image": ("image.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": "console-file"},
        )
        self.assertEqual(file.status_code, 400)
        for gateway in self.media.gateways.values():
            gateway.client.generate_image.assert_not_awaited()
            gateway.client.generate_speech.assert_not_awaited()
            gateway.client.submit_text.assert_not_awaited()

    async def test_selection_respects_switches_busy_accounts_and_never_falls_back_after_claim(self):
        await self.login()
        self.media.tokens[1].image_enabled = False
        image = await self.create("/images/generations", "pool-image-switch", IMAGE)
        self.assertEqual(image.status_code, 200)
        self.assertTrue(self.media.gateways[2]._path(identifier("pool-image-switch")).exists())
        self.media.gateways[1].client.generate_image.assert_not_awaited()
        await self.media.gateways[1].lock.acquire()
        try:
            video = await self.create("/videos", "pool-busy-choice", VIDEO)
            self.assertEqual(video.status_code, 200)
            self.assertTrue(self.media.gateways[2]._path(identifier("pool-busy-choice")).exists())
        finally:
            self.media.gateways[1].lock.release()
        calls = 0

        async def disable_after_selection(token_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.media.tokens[1].is_active = False
            return self.media.tokens[token_id]

        self.media.dispatcher.db.get_token.side_effect = disable_after_selection
        denied = await self.create("/videos", "pool-claimed-disabled", VIDEO)
        self.assertEqual(denied.status_code, 503)
        self.assertEqual(denied.json()["error"]["code"], "account_disabled")
        repeated = await self.create("/videos", "pool-claimed-disabled", VIDEO)
        self.assertEqual(repeated.status_code, 503)
        self.media.gateways[1].client.submit_text.assert_not_awaited()
        self.media.gateways[2].client.submit_text.assert_awaited_once()

    async def test_concurrent_same_key_rechecks_affinity_after_async_account_selection(self):
        await self.login()
        waiting = asyncio.Event()
        release = asyncio.Event()
        first = True

        async def lookup(token_id):
            nonlocal first
            if first:
                first = False
                snapshot = SimpleNamespace(is_active=True, image_enabled=True, video_enabled=True)
                waiting.set()
                await release.wait()
                return snapshot
            return self.media.tokens[token_id]

        self.media.dispatcher.db.get_token.side_effect = lookup
        pending = asyncio.create_task(self.create("/videos", "pool-race", VIDEO))
        await waiting.wait()
        self.media.tokens[1].is_active = False
        concurrent = asyncio.create_task(self.create("/videos", "pool-race", VIDEO))
        release.set()
        original, replay = await asyncio.gather(pending, concurrent)
        self.assertEqual(original.status_code, 503, original.text)
        self.assertEqual(original.json()["error"]["code"], "account_disabled")
        self.assertEqual(replay.json(), original.json())
        self.media.gateways[1].client.submit_text.assert_not_awaited()
        self.media.gateways[2].client.submit_text.assert_not_awaited()

    async def test_uploaded_video_selects_pool_account_and_keeps_durable_receipt_affinity(self):
        await self.login()
        source = await self.media.create("/videos", "pool-source-two", VIDEO, 2)
        self.assertEqual(source.status_code, 200)
        uploaded = (await self.media.http.get(source.json()["content_url"])).content
        self.assertEqual(uploaded, MP4)
        upstream = self.media.gateways[1].client

        async def upload(path):
            self.assertEqual(Path(path).read_bytes(), uploaded)
            return "uploaded-video"

        upstream.upload_temporary = AsyncMock(side_effect=upload)
        upstream.submit_video = AsyncMock(return_value=media_tests.video_response(1280, 720, 3))
        body = {
            "kind": "video",
            "mode": "edit",
            "prompt": "A green boat",
            "video_base64": base64.b64encode(uploaded).decode(),
        }
        edited = await self.create("/vids/generations", "pool-uploaded-edit", body)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["status"], "completed")
        self.assertTrue(self.media.gateways[1]._path(edited.json()["id"]).exists())
        self.assertFalse(self.media.gateways[2]._path(edited.json()["id"]).exists())
        self.media.gateways[2].client.submit_text.assert_awaited_once()
        upstream.submit_text.assert_not_awaited()
        self.media.tokens[1].is_active = False
        await self.media.gateways[1].close()
        await self.media.gateways[1].start()
        self.media.dispatcher.replace([self.media.binding(1), self.media.binding(2)])
        replay = await self.create("/vids/generations", "pool-uploaded-edit", body)
        self.assertEqual(replay.json(), edited.json())
        conflicting = await self.media.create("/vids/generations", "pool-uploaded-edit", body, 2)
        self.assertEqual(conflicting.status_code, 409)
        self.assertEqual(conflicting.json()["error"]["code"], "account_affinity_conflict")
        upstream.upload_temporary.assert_awaited_once()
        upstream.submit_video.assert_awaited_once()
        self.media.gateways[1].client.flags["docs-hue"] = "unexpected identity"
        healthy = await self.create("/videos", "pool-bad-identity", VIDEO)
        self.assertEqual(healthy.status_code, 200)
        self.assertTrue(self.media.gateways[2]._path(healthy.json()["id"]).exists())
        self.assertEqual((await self.http.get(self.url("/models"))).status_code, 200)
        receipt = await self.http.get(self.url("/vids/generations/" + edited.json()["id"]))
        self.assertEqual(receipt.status_code, 409)
        self.assertEqual(receipt.json()["error"]["code"], "account_mismatch")

    async def test_public_pool_uploaded_video_modes_share_receipts_and_validate_inputs(self):
        self.app.include_router(
            create_vids_media_router(self.media.dispatcher, prefix=PUBLIC_PREFIX, auto_pool=True)
        )
        prefix = PUBLIC_PREFIX + "/v1"
        self.media.tokens[1].is_active = False
        upstream = self.media.gateways[2].client

        async def upload(path):
            self.assertEqual(Path(path).read_bytes(), MP4)
            return "uploaded-video"

        upstream.upload_temporary = AsyncMock(side_effect=upload)
        upstream.submit_video = AsyncMock()
        for mode in ("edit", "extend", "upscale"):
            with self.subTest(mode=mode):
                key = "public-uploaded-" + mode
                width, height = (1920, 1080) if mode == "upscale" else (1280, 720)
                upstream.submit_video.return_value = media_tests.video_response(
                    width, height, 6 if mode == "extend" else 3
                )
                body = {**VIDEO, "mode": mode, "video_base64": base64.b64encode(MP4).decode()}
                headers = {"Idempotency-Key": key}
                first = await self.media.http.post(prefix + "/videos", json=body, headers=headers)
                self.assertEqual(first.status_code, 200, first.text)
                result = first.json()
                self.assertEqual(result["status"], "completed")
                self.assertTrue(self.media.gateways[2]._path(result["id"]).exists())
                multipart = await self.media.http.post(
                    prefix + "/videos",
                    files=[
                        ("input_video", ("renamed.mp4", MP4, "video/mp4")),
                        *[
                            (name, (None, value))
                            for name, value in body.items()
                            if name != "video_base64"
                        ],
                    ],
                    headers=headers,
                )
                self.assertEqual(multipart.json(), result)
                generic = await self.media.http.post(
                    prefix + "/vids/generations",
                    json={
                        "kind": "video",
                        **{name: value for name, value in body.items() if name != "model"},
                    },
                    headers=headers,
                )
                self.assertEqual(generic.json()["status"], "completed", generic.text)
                self.assertEqual(generic.json()["id"], result["id"])
                self.assertTrue(result["receipt_url"].startswith(prefix + "/vids/generations/"))
                self.assertTrue(result["content_url"].startswith(prefix + "/videos/"))
                content = await self.media.http.get(result["content_url"])
                self.assertEqual(content.status_code, 200)
                self.assertEqual(
                    content.content, media_tests.mp4(6 if mode == "extend" else 3, width, height)
                )
                switched = await self.media.create("/videos", key, body, 1)
                self.assertEqual(switched.status_code, 409)
                self.assertEqual(switched.json()["error"]["code"], "account_affinity_conflict")
        self.assertEqual(upstream.upload_temporary.await_count, 3)
        self.assertEqual(upstream.submit_video.await_count, 3)
        for mode in ("edit", "extend", "upscale"):
            key = "public-rejected-source-" + mode
            source = {**VIDEO, "mode": mode, "source_id": identifier("public-uploaded-edit")}
            for path, body in (
                ("/videos", source),
                (
                    "/vids/generations",
                    {
                        "kind": "video",
                        **{name: value for name, value in source.items() if name != "model"},
                    },
                ),
            ):
                denied = await self.media.http.post(
                    prefix + path, json=body, headers={"Idempotency-Key": key}
                )
                self.assertEqual(denied.status_code, 400, denied.text)
            missing = await self.media.http.get(prefix + "/vids/generations/" + identifier(key))
            self.assertEqual(missing.status_code, 404)
        for index, invalid in enumerate(("invalid", base64.b64encode(b"not an MP4").decode())):
            rejected = await self.media.http.post(
                prefix + "/videos",
                json={**VIDEO, "mode": "edit", "video_base64": invalid},
                headers={"Idempotency-Key": f"public-invalid-video-{index}"},
            )
            self.assertEqual(rejected.status_code, 400, rejected.text)
            self.assertEqual(rejected.json()["error"]["code"], "invalid_video")
        self.assertEqual(upstream.upload_temporary.await_count, 3)
        self.assertEqual(upstream.submit_video.await_count, 3)
        self.media.gateways[1].client.submit_text.assert_not_awaited()
        upstream.submit_text.assert_not_awaited()
        missing = await self.media.http.get(prefix + "/videos/" + identifier("missing-receipt"))
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
