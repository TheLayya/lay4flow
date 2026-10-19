"""Production Vids HTTP account isolation, using synthetic upstreams and media only."""

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.api.openai_common import openai_http_exception_handler
from src.api.vids_media import CONSOLE_PREFIX, PREFIX, PUBLIC_PREFIX, create_vids_media_router
from src.services.vids.client import VidsTransportError, protocol_error
from src.services.vids.dispatcher import VidsAccount, VidsProviderDispatcher
from src.services.vids.gateway import VidsGateway
from tests.test_vids_gateway import KEY, client
from tests.test_vids_images_api import JPEG, configure_image_edit, image_bytes, image_response
from tests.test_vids_video_gateway import video_response
from tests.test_vids_video_input import mp4

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

IMAGE = {"model": "vids-image", "prompt": "Blue boat"}
VIDEO = {"model": "vids-video", "prompt": "Boat moves"}
SPEECH = {"model": "vids-speech", "input": "Hello", "voice": "Puck", "response_format": "wav"}
MP4 = mp4(3)


def identifier(key):
    return "vids_" + hashlib.sha256(key.encode()).hexdigest()


class VidsMediaAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_language_failure_and_replay_share_safe_receipt_across_all_speech_routes(self):
        self.app.include_router(
            create_vids_media_router(self.dispatcher, prefix=PUBLIC_PREFIX, auto_pool=True)
        )
        self.app.include_router(admin.auth_router)
        self.app.include_router(
            create_vids_media_router(
                self.dispatcher,
                admin.verify_admin_token,
                prefix=CONSOLE_PREFIX,
                auto_pool=True,
            )
        )
        upstreams = {token_id: gateway.client for token_id, gateway in self.gateways.items()}
        failure = protocol_error(
            "generate",
            SimpleNamespace(
                status_code=400,
                content=b"[]",
                json=lambda: [
                    3,
                    "private message https://private.example token=secret",
                    [
                        [
                            "type.googleapis.com/google.rpc.ErrorInfo",
                            ["LANGUAGE_NOT_SUPPORTED", "private-domain", {"key": "private"}],
                        ]
                    ],
                ],
            ),
        )
        for upstream in upstreams.values():
            upstream.generate_speech.side_effect = failure
        body = {**SPEECH, "voice": "Algenib"}
        with (
            patch.multiple(
                admin,
                active_admin_tokens=set(),
                db=SimpleNamespace(
                    get_admin_config=AsyncMock(return_value=SimpleNamespace(username="test-admin"))
                ),
            ),
            patch.object(admin.AuthManager, "verify_admin", return_value=True),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="https://media.example.test"
            ) as console:
                login = await console.post(
                    "/api/admin/login", json={"username": "test-admin", "password": "test-password"}
                )
                self.assertEqual(login.status_code, 200, login.text)
                routes = (
                    (self.http, PREFIX.format(token_id=1)),
                    (self.http, PUBLIC_PREFIX),
                    (console, CONSOLE_PREFIX),
                )
                for index, (first_client, first_prefix) in enumerate(routes):
                    with self.subTest(first_prefix=first_prefix):
                        for upstream in upstreams.values():
                            upstream.generate_speech.reset_mock()
                        owner = 2 if index == 2 else 1
                        key = "speech-language-route-" + str(index)
                        first = await first_client.post(
                            first_prefix + "/v1/audio/speech",
                            json=body,
                            headers={"Idempotency-Key": key},
                        )
                        expected = {
                            "id": identifier(key),
                            "kind": "speech",
                            "status": "failed",
                            "error": "upstream_http_400",
                            "error_operation": "generate",
                            "content_url": None,
                            "upstream_error": {
                                "status": "INVALID_ARGUMENT",
                                "reason": "LANGUAGE_NOT_SUPPORTED",
                            },
                        }
                        self.assertEqual(first.status_code, 502, first.text)
                        self.assertEqual(first.json()["vids_receipt"], expected)
                        replay_routes = (
                            (self.http, PREFIX.format(token_id=owner)),
                            (self.http, PUBLIC_PREFIX),
                            (console, CONSOLE_PREFIX),
                        )
                        for http, prefix in replay_routes:
                            replay = await http.post(
                                prefix + "/v1/audio/speech",
                                json=body,
                                headers={"Idempotency-Key": key},
                            )
                            self.assertEqual(replay.status_code, 502, replay.text)
                            self.assertEqual(replay.json()["vids_receipt"], expected)
                            receipt = await http.get(
                                prefix + "/v1/vids/generations/" + identifier(key)
                            )
                            self.assertEqual(receipt.status_code, 200, receipt.text)
                            self.assertEqual(
                                receipt.json()["upstream_error"], expected["upstream_error"]
                            )
                            self.assertNotIn("private", first.text + replay.text + receipt.text)
                        upstreams[owner].generate_speech.assert_awaited_once_with(
                            "Hello", voice="Algenib"
                        )
                        upstreams[3 - owner].generate_speech.assert_not_awaited()
                        self.assertTrue(self.gateways[owner]._path(identifier(key)).exists())
        for upstream in upstreams.values():
            upstream.download_audio.assert_not_awaited()

    async def test_public_and_admin_speech_pass_selected_preset_without_changing_defaults(self):
        self.app.include_router(
            create_vids_media_router(self.dispatcher, prefix=PUBLIC_PREFIX, auto_pool=True)
        )
        for prefix, voice in ((PUBLIC_PREFIX, "Kore"), (PREFIX.format(token_id=1), "Aoede")):
            self.gateways[1].client.generate_speech.reset_mock()
            key = "voice-" + voice
            body = {**SPEECH, "voice": voice}
            response = await self.http.post(
                prefix + "/v1/audio/speech", json=body, headers={"Idempotency-Key": key}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content[:4], b"RIFF")
            self.assertEqual(
                (
                    await self.http.post(
                        prefix + "/v1/audio/speech", json=body, headers={"Idempotency-Key": key}
                    )
                ).content,
                response.content,
            )
            self.gateways[1].client.generate_speech.assert_awaited_once_with(
                SPEECH["input"], voice=voice
            )
            changed = await self.http.post(
                prefix + "/v1/audio/speech",
                json={**body, "voice": "Puck"},
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(changed.status_code, 409)

    async def test_public_pool_upload_video_modes_need_no_account_or_existing_media(self):
        self.app.include_router(
            create_vids_media_router(self.dispatcher, prefix=PUBLIC_PREFIX, auto_pool=True)
        )
        upstreams = {token_id: gateway.client for token_id, gateway in self.gateways.items()}
        for upstream in upstreams.values():
            upstream.upload_temporary = AsyncMock(return_value="private-upload-token")
            upstream.submit_video = AsyncMock()
        for index, mode in enumerate(("edit", "extend", "upscale")):
            owner = 1 + index % 2
            for upstream in upstreams.values():
                upstream.submit_video.reset_mock()
                upstream.upload_temporary.reset_mock()
                upstream.submit_video.return_value = video_response(
                    1920 if mode == "upscale" else 1280,
                    1080 if mode == "upscale" else 720,
                    6 if mode == "extend" else 3,
                )
            upstream = upstreams[owner]
            body = {**VIDEO, "mode": mode, "video_base64": base64.b64encode(MP4).decode()}
            key = "public-upload-" + mode
            first = await self.http.post(
                PUBLIC_PREFIX + "/v1/videos", json=body, headers={"Idempotency-Key": key}
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["status"], "completed", first.text)
            self.assertNotIn("accounts/", first.text)
            self.assertNotIn("private", first.text)
            self.assertEqual((await self.http.get(first.json()["content_url"])).status_code, 200)
            self.assertEqual(
                (
                    await self.http.post(
                        PUBLIC_PREFIX + "/v1/videos", json=body, headers={"Idempotency-Key": key}
                    )
                ).json(),
                first.json(),
            )
            upstream.upload_temporary.assert_awaited_once()
            upstream.submit_video.assert_awaited_once()
            upstreams[3 - owner].upload_temporary.assert_not_awaited()
            upstreams[3 - owner].submit_video.assert_not_awaited()
            self.assertTrue(self.gateways[owner]._path(identifier(key)).exists())
            denied = await self.http.post(
                PUBLIC_PREFIX + "/v1/videos",
                json={**body, "source_id": identifier("old-source")},
                headers={"Idempotency-Key": key + "-source"},
            )
            self.assertEqual(denied.status_code, 400)
            upstream.upload_temporary.assert_awaited_once()
            denied = await self.http.get(first.json()["content_url"], headers={"Authorization": ""})
            self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            (await self.http.get(PUBLIC_PREFIX + "/v1/vids/document/images")).status_code, 404
        )
        catalog = (await self.http.get(PUBLIC_PREFIX + "/v1/models")).json()["data"]
        self.assertTrue(all("accounts/" not in str(row) for row in catalog))

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tokens = {
            token_id: SimpleNamespace(is_active=True, image_enabled=True, video_enabled=True)
            for token_id in (1, 2)
        }
        self.dispatcher = VidsProviderDispatcher(
            SimpleNamespace(get_token=AsyncMock(side_effect=self.tokens.get))
        )
        self.gateways = {}
        for token_id in (1, 2):
            upstream = client()
            upstream.flags["docs-hue"] = f"synthetic-account-{token_id}"
            image = ["private-resource", "https://lh3.googleusercontent.com/private", 1024, 1024]
            upstream.generate_image = AsyncMock(return_value=[[None] * 11 + [[None] * 6 + [image]]])
            upstream.submit_text = AsyncMock(return_value=video_response(1280, 720, 3))

            async def download_image(resource, path):
                Path(path).write_bytes(JPEG)

            async def download_video(resource, path):
                Path(path).write_bytes(
                    mp4(resource["duration"], resource["width"], resource["height"])
                )

            upstream.download_image = AsyncMock(side_effect=download_image)
            upstream.download_video = AsyncMock(side_effect=download_video)
            gateway = VidsGateway(upstream, Path(self.directory.name) / str(token_id))
            await gateway.start()
            self.gateways[token_id] = gateway
            self.dispatcher.register(self.binding(token_id))
        auth = patch("src.core.auth.AuthManager.verify_api_key", side_effect=lambda key: key == KEY)
        auth.start()
        self.addCleanup(auth.stop)
        self.app = FastAPI()
        self.app.add_exception_handler(404, openai_http_exception_handler)
        self.app.add_exception_handler(405, openai_http_exception_handler)

        @self.app.post("/v1/images/generations")
        async def flow_images():
            return {"provider": "flow"}

        @self.app.post("/v1/videos")
        async def flow_videos():
            return {"provider": "flow"}

        self.app.include_router(create_vids_media_router(self.dispatcher))
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://media.example.test",
            headers={"Authorization": "Bearer " + KEY},
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        for gateway in self.gateways.values():
            await gateway.close()
        self.directory.cleanup()

    def binding(self, token_id):
        gateway = self.gateways[token_id]
        return VidsAccount(token_id, gateway, hashlib.sha256(gateway.identity.encode()).hexdigest())

    @staticmethod
    def url(path, token_id=1):
        return PREFIX.format(token_id=token_id) + "/v1" + path

    async def create(self, path, key, body, token_id=1):
        return await self.http.post(
            self.url(path, token_id), json=body, headers={"Idempotency-Key": key}
        )

    async def test_standard_uploaded_edit_keeps_account_and_shared_receipt(self):
        for gateway in self.gateways.values():
            configure_image_edit(gateway.client)
        key = "account-uploaded-edit"
        response = await self.http.post(
            self.url("/images/edits", 2),
            data={**IMAGE, "response_format": "b64_json"},
            files={"image": ("source.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(base64.b64decode(response.json()["data"][0]["b64_json"]), JPEG)
        replay = await self.create(
            "/vids/generations",
            key,
            {
                "kind": "image",
                "mode": "edit",
                "prompt": IMAGE["prompt"],
                "image_base64": base64.b64encode(JPEG).decode(),
            },
            2,
        )
        self.assertEqual(replay.json()["status"], "completed", replay.text)
        switched = await self.http.post(
            self.url("/images/edits", 1),
            data=IMAGE,
            files={"image": ("source.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(switched.status_code, 409)
        self.assertEqual(switched.json()["error"]["code"], "account_affinity_conflict")
        self.gateways[2].client.insert_document_image.assert_awaited_once()
        self.gateways[2].client.edit_document_image.assert_awaited_once()
        self.gateways[1].client.insert_document_image.assert_not_awaited()
        self.gateways[1].client.edit_document_image.assert_not_awaited()

    async def test_prefix_preserves_flow_routes_and_shared_auth_protects_every_endpoint(self):
        for path in ("/v1/images/generations", "/v1/videos"):
            self.assertEqual((await self.http.post(path)).json(), {"provider": "flow"})
        for route in self.app.routes:
            if not route.path.startswith("/providers/"):
                continue
            path = (
                route.path.replace("{token_id}", "1")
                .replace("{generation_id}", identifier("x"))
                .replace("{video_id}", identifier("x"))
            )
            method = next(iter(route.methods))
            response = await self.http.request(method, path, headers={"Authorization": ""})
            self.assertEqual(response.status_code, 401, (method, path, response.text))
            self.assertEqual(response.json()["error"]["code"], "invalid_api_key")
            self.assertTrue(response.headers["x-request-id"].startswith("req_"))
        models = (await self.http.get(self.url("/models"))).json()["data"]
        self.assertEqual({row["id"] for row in models}, {"vids-image", "vids-speech", "vids-video"})
        for row in models:
            self.assertTrue(
                all(path.startswith(self.url("")) for path in row["supported_endpoints"])
            )
        for path in (
            "/vids/session",
            "/vids/session/refresh",
            "/vids/document/images",
            "/vids/quota",
        ):
            self.assertEqual((await self.http.post(self.url(path))).status_code, 404)
        rejected = await self.create("/vids/generations", "music", {"kind": "music", "prompt": "x"})
        self.assertEqual(rejected.json()["error"]["code"], "invalid_kind")
        for gateway in self.gateways.values():
            gateway.client.generate_image.assert_not_awaited()
            gateway.client.submit_text.assert_not_awaited()
            gateway.client.generate_speech.assert_not_awaited()

    async def test_images_url_and_b64_replay_on_one_account_and_block_cross_account(self):
        first = await self.create("/images/generations", "image", IMAGE)
        self.assertEqual(first.status_code, 200, first.text)
        expected = "https://media.example.test" + self.url(
            "/vids/generations/" + identifier("image") + "/content"
        )
        self.assertEqual(first.json()["data"][0]["url"], expected)
        self.assertEqual((await self.http.get(expected)).content, JPEG)
        second = await self.create(
            "/images/generations",
            "image",
            {**IMAGE, "response_format": "b64_json", "size": "1024x1024"},
        )
        self.assertEqual(base64.b64decode(second.json()["data"][0]["b64_json"]), JPEG)
        generic = await self.create(
            "/vids/generations", "image", {"kind": "image", "prompt": IMAGE["prompt"]}
        )
        self.assertEqual(
            generic.json()["content_url"],
            self.url("/vids/generations/" + identifier("image") + "/content"),
        )
        switched = await self.create("/images/generations", "image", IMAGE, 2)
        self.assertEqual(switched.status_code, 409)
        self.assertEqual(switched.json()["error"]["code"], "account_affinity_conflict")
        for path in ("/vids/generations/", "/videos/"):
            response = await self.http.get(self.url(path + identifier("image"), 2))
            self.assertEqual(response.status_code, 404)
        self.assertNotIn("private", first.text + generic.text + switched.text)
        self.gateways[1].client.generate_image.assert_awaited_once()
        self.gateways[2].client.generate_image.assert_not_awaited()

    async def test_multiple_image_outputs_keep_explicit_account_urls_and_replay(self):
        upstream = self.gateways[2].client
        upstream.generate_image.return_value = image_response(4)

        async def download(resource, path):
            index = int(resource["unique_key"].rsplit("-", 1)[1])
            Path(path).write_bytes(image_bytes(index))

        upstream.download_image.side_effect = download
        body = {**IMAGE, "n": 4}
        key = "four-account-images"
        first = await self.create("/images/generations", key, body, 2)
        self.assertEqual(first.status_code, 200, first.text)
        base = "https://media.example.test" + self.url(
            "/vids/generations/" + identifier(key) + "/content", 2
        )
        self.assertEqual(len(first.json()["data"]), 4)
        for index, item in enumerate(first.json()["data"]):
            expected = base + (f"?index={index}" if index else "")
            self.assertEqual(item["url"], expected)
            self.assertEqual((await self.http.get(expected)).content, image_bytes(index))
            self.assertEqual(
                (await self.http.get(expected, headers={"Authorization": ""})).status_code, 401
            )
            wrong_account = await self.http.get(
                self.url("/vids/generations/" + identifier(key) + "/content", 1),
                params={"index": index},
            )
            self.assertEqual(wrong_account.status_code, 404, wrong_account.text)
        encoded = await self.create(
            "/images/generations", key, {**body, "response_format": "b64_json"}, 2
        )
        self.assertEqual(
            [base64.b64decode(item["b64_json"]) for item in encoded.json()["data"]],
            [image_bytes(index) for index in range(4)],
        )
        generic = await self.create(
            "/vids/generations", key, {"kind": "image", "prompt": IMAGE["prompt"], "n": 4}, 2
        )
        self.assertEqual(generic.json()["status"], "completed", generic.text)
        for index, output in enumerate(generic.json()["outputs"]):
            self.assertEqual(output["index"], index)
            self.assertEqual(
                "https://media.example.test" + output["content_url"],
                first.json()["data"][index]["url"],
            )
        self.assertEqual(
            (await self.create("/images/generations", key, body, 2)).json(), first.json()
        )
        changed = await self.create("/images/generations", key, {**body, "n": 3}, 2)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["error"]["code"], "idempotency_conflict")
        switched = await self.create("/images/generations", key, body, 1)
        self.assertEqual(switched.status_code, 409)
        self.assertEqual(switched.json()["error"]["code"], "account_affinity_conflict")
        upstream.generate_image.assert_awaited_once_with(IMAGE["prompt"], count=4)
        self.assertEqual(upstream.download_image.await_count, 4)
        self.gateways[1].client.generate_image.assert_not_awaited()

    async def test_admin_pool_multiple_outputs_keep_selected_account_and_cookie_protection(self):
        self.app.include_router(admin.auth_router)
        self.app.include_router(
            create_vids_media_router(
                self.dispatcher,
                admin.verify_admin_token,
                prefix=CONSOLE_PREFIX,
                auto_pool=True,
                tags=["Vids console"],
            ),
            include_in_schema=False,
        )
        self.tokens[1].is_active = False
        upstream = self.gateways[2].client
        upstream.generate_image.return_value = image_response(4)

        async def download(resource, path):
            index = int(resource["unique_key"].rsplit("-", 1)[1])
            Path(path).write_bytes(image_bytes(index))

        upstream.download_image.side_effect = download
        with (
            patch.multiple(
                admin,
                active_admin_tokens=set(),
                db=SimpleNamespace(
                    get_admin_config=AsyncMock(return_value=SimpleNamespace(username="test-admin"))
                ),
            ),
            patch.object(admin.AuthManager, "verify_admin", return_value=True),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="https://console.example.test"
            ) as console:
                login = await console.post(
                    "/api/admin/login", json={"username": "test-admin", "password": "test-password"}
                )
                self.assertEqual(login.status_code, 200, login.text)
                key = "four-pool-images"
                body = {**IMAGE, "n": 4}
                path = CONSOLE_PREFIX + "/v1/images/generations"
                first = await console.post(path, json=body, headers={"Idempotency-Key": key})
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(first.headers["x-vids-generation-id"], identifier(key))
                self.assertNotIn("Authorization", first.request.headers)
                for index, item in enumerate(first.json()["data"]):
                    expected = (
                        "https://console.example.test"
                        + CONSOLE_PREFIX
                        + "/v1/vids/generations/"
                        + identifier(key)
                        + "/content"
                        + (f"?index={index}" if index else "")
                    )
                    self.assertEqual(item["url"], expected)
                    self.assertEqual((await console.get(expected)).content, image_bytes(index))
                    for authorization in ("", "Bearer " + KEY):
                        denied = await self.http.get(
                            expected, headers={"Authorization": authorization}
                        )
                        self.assertEqual(denied.status_code, 401, denied.text)
                replay = await console.post(
                    path,
                    json={**body, "response_format": "b64_json"},
                    headers={"Idempotency-Key": key},
                )
                self.assertEqual(
                    [base64.b64decode(item["b64_json"]) for item in replay.json()["data"]],
                    [image_bytes(index) for index in range(4)],
                )
                receipt = await console.get(
                    CONSOLE_PREFIX + "/v1/vids/generations/" + identifier(key)
                )
                self.assertEqual(len(receipt.json()["outputs"]), 4)
                for index, output in enumerate(receipt.json()["outputs"]):
                    self.assertEqual(
                        "https://console.example.test" + output["content_url"],
                        first.json()["data"][index]["url"],
                    )
                switched = await self.create("/images/generations", key, body, 1)
                self.assertEqual(switched.json()["error"]["code"], "account_affinity_conflict")
                explicit = await self.create("/images/generations", key, body, 2)
                self.assertEqual(explicit.status_code, 200, explicit.text)
                for index, item in enumerate(explicit.json()["data"]):
                    self.assertIn(self.url("/vids/generations/", 2), item["url"])
                    self.assertEqual((await self.http.get(item["url"])).content, image_bytes(index))
                changed = await console.post(
                    path, json={**body, "n": 2}, headers={"Idempotency-Key": key}
                )
                self.assertEqual(changed.json()["error"]["code"], "idempotency_conflict")
        self.assertFalse(self.gateways[1]._path(identifier(key)).exists())
        self.assertTrue(self.gateways[2]._path(identifier(key)).exists())
        upstream.generate_image.assert_awaited_once_with(IMAGE["prompt"], count=4)
        self.assertEqual(upstream.download_image.await_count, 4)
        self.gateways[1].client.generate_image.assert_not_awaited()

    async def test_speech_returns_wav_and_disabled_receipts_remain_readable(self):
        first = await self.create("/audio/speech", "speech", SPEECH)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["content-type"], "audio/wav")
        self.assertTrue(first.content.startswith(b"RIFF"))
        self.tokens[1].is_active = False
        self.assertEqual(
            (await self.create("/audio/speech", "speech", SPEECH)).content, first.content
        )
        denied = await self.create("/audio/speech", "new-speech", SPEECH)
        self.assertEqual(denied.status_code, 503)
        self.assertEqual(denied.json()["error"]["code"], "account_disabled")
        receipt = await self.http.get(self.url("/vids/generations/" + identifier("speech")))
        self.assertEqual(
            (await self.http.get(receipt.json()["content_url"])).content, first.content
        )
        self.gateways[1].client.generate_speech.assert_awaited_once()

    async def test_video_json_multipart_listing_receipt_and_download_keep_account_urls(self):
        first = await self.create("/videos", "video-one", VIDEO)
        self.assertEqual(first.status_code, 200, first.text)
        result = first.json()
        self.assertEqual(
            result["receipt_url"], self.url("/vids/generations/" + identifier("video-one"))
        )
        self.assertEqual(
            result["content_url"], self.url("/videos/" + identifier("video-one") + "/content")
        )
        self.assertEqual((await self.http.get(result["content_url"])).content, MP4)
        second = await self.http.post(
            self.url("/videos"),
            files={name: (None, value) for name, value in VIDEO.items()},
            headers={"Idempotency-Key": "video-two"},
        )
        self.assertEqual(second.status_code, 200, second.text)
        third = await self.create("/videos", "video-three", VIDEO, 2)
        self.assertEqual(third.status_code, 200, third.text)
        listed = (await self.http.get(self.url("/videos"), params={"limit": 1})).json()
        self.assertTrue(listed["has_more"])
        self.assertEqual(len(listed["data"]), 1)
        next_page = (
            await self.http.get(self.url("/videos"), params={"after": listed["last_id"]})
        ).json()
        self.assertEqual(len(next_page["data"]), 1)
        self.assertEqual(
            {listed["first_id"], next_page["first_id"]}, {result["id"], second.json()["id"]}
        )
        wrong_anchor = await self.http.get(
            self.url("/videos"), params={"after": third.json()["id"]}
        )
        self.assertEqual(wrong_anchor.status_code, 404)
        self.assertEqual((await self.http.get(self.url("/videos/" + result["id"]))).json(), result)
        self.assertEqual(
            (await self.http.get(self.url("/videos/" + result["id"], 2))).status_code, 404
        )

    async def test_unknown_restarts_replay_and_cannot_change_account_or_download(self):
        self.gateways[1].client.submit_text.side_effect = VidsTransportError("private URL")
        first = await self.create("/videos", "unknown-video", VIDEO)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["status"], "unknown")
        self.assertIsNone(first.json()["content_url"])
        await self.gateways[1].close()
        await self.gateways[1].start()
        self.dispatcher.replace([self.binding(1), self.binding(2)])
        self.tokens[1].is_active = False
        self.assertEqual(
            (await self.create("/videos", "unknown-video", VIDEO)).json(), first.json()
        )
        switched = await self.create("/videos", "unknown-video", VIDEO, 2)
        self.assertEqual(switched.json()["error"]["code"], "account_affinity_conflict")
        retry = await self.http.post(first.json()["receipt_url"] + "/download")
        self.assertEqual(retry.status_code, 409)
        self.gateways[1].client.submit_text.assert_awaited_once()
        self.gateways[2].client.submit_text.assert_not_awaited()
        self.assertNotIn("private", first.text + switched.text + retry.text)

    async def test_download_recovery_uses_saved_result_even_when_account_is_disabled(self):
        upstream = self.gateways[1].client
        download = upstream.download_video.side_effect
        upstream.download_video.side_effect = VidsTransportError("private download URL")
        first = await self.create("/videos", "download-video", VIDEO)
        result = first.json()
        self.assertEqual(result["vids_status"], "download_failed")
        self.assertEqual(
            result["retry_download_url"],
            self.url("/vids/generations/" + result["id"] + "/download"),
        )
        self.tokens[1].is_active = False
        upstream.download_video.side_effect = download
        recovered = await self.http.post(result["retry_download_url"])
        self.assertEqual(recovered.json()["status"], "completed")
        self.assertEqual((await self.http.get(recovered.json()["content_url"])).content, MP4)
        upstream.submit_text.assert_awaited_once()
        self.assertEqual(upstream.download_video.await_count, 2)

    async def test_invalid_parameters_and_unknown_accounts_cannot_submit(self):
        cases = [
            ("/images/generations", {**IMAGE, "n": True}),
            ("/images/generations", {**IMAGE, "n": 5}),
            ("/images/generations", {**IMAGE, "size": "1280x720"}),
            ("/images/generations", {**IMAGE, "token_id": 2}),
            ("/audio/speech", {**SPEECH, "voice": "unverified"}),
            ("/audio/speech", {**SPEECH, "response_format": "mp3"}),
            ("/videos", {**VIDEO, "seconds": True}),
            ("/videos", {**VIDEO, "seconds": 11}),
            ("/videos", {**VIDEO, "model": "veo_3_1"}),
            ("/vids/generations", {"kind": "music", "prompt": "x"}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                response = await self.create(path, "invalid", body)
                self.assertEqual(response.status_code, 400, response.text)
        for path, body in (
            ("/images/generations", IMAGE),
            ("/audio/speech", SPEECH),
            ("/videos", VIDEO),
        ):
            missing_key = await self.http.post(self.url(path), json=body)
            self.assertEqual(missing_key.json()["error"]["code"], "idempotency_required")
            missing_account = await self.create(path, "missing-account", body, 999)
            self.assertEqual(missing_account.status_code, 404)
            self.assertEqual(missing_account.json()["error"]["code"], "account_not_found")
            invalid_token = await self.http.post(self.url(path, "true"), json=body)
            self.assertEqual(invalid_token.status_code, 400)
        file = await self.http.post(
            self.url("/videos"),
            files={"image": ("private.jpg", JPEG, "image/jpeg")},
            headers={"Idempotency-Key": "file"},
        )
        self.assertEqual(file.status_code, 400)
        for gateway in self.gateways.values():
            gateway.client.generate_image.assert_not_awaited()
            gateway.client.generate_speech.assert_not_awaited()
            gateway.client.submit_text.assert_not_awaited()
            self.assertEqual(list(gateway.root.glob("vids_*.json")), [])

    async def test_replace_is_atomic_and_list_validates_identity_before_reading_receipts(self):
        await self.create("/images/generations", "preserved", IMAGE)
        old_accounts = self.dispatcher._accounts
        old_claims = dict(self.dispatcher._claims)
        with self.assertRaises(ValueError):
            self.dispatcher.replace([self.binding(2), self.binding(2)])
        self.assertIs(self.dispatcher._accounts, old_accounts)
        self.assertEqual(self.dispatcher._claims, old_claims)
        self.assertEqual(len(self.dispatcher.list(1)), 1)
        self.gateways[1].client.flags["docs-hue"] = "unexpected identity"
        response = await self.http.get(self.url("/videos"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "account_mismatch")
        self.dispatcher.replace([])
        self.assertEqual(self.dispatcher._claims, {})
        self.assertEqual((await self.http.get(self.url("/videos"))).status_code, 404)

    async def test_content_rejects_unexpected_receipt_id_suffix_and_symlink(self):
        await self.create("/images/generations", "unsafe-image", IMAGE)
        gateway = self.gateways[1]
        generation_id = identifier("unsafe-image")
        original = gateway.get(generation_id)
        outside = Path(self.directory.name) / "outside.jpg"
        outside.write_bytes(JPEG)
        for changes in (
            {"id": "../outside", "suffix": ".jpg"},
            {"suffix": ".jpg", "mime_type": "application/octet-stream"},
            {"suffix": ".unexpected"},
        ):
            changed = {**original, **changes}
            # Write the intentionally malformed receipt under its original lookup ID.
            gateway._path(generation_id).write_text(json.dumps(changed))
            if changes.get("suffix") == ".unexpected":
                (gateway.root / (generation_id + ".unexpected")).write_bytes(JPEG)
            response = await self.http.get(
                self.url("/vids/generations/" + generation_id + "/content")
            )
            self.assertEqual(response.status_code, 410, response.text)
        gateway._save(original)
        asset, _ = gateway.content(generation_id)
        asset.unlink()
        try:
            asset.symlink_to(outside)
        except OSError:
            return  # Windows may require developer mode for symlink creation.
        response = await self.http.get(self.url("/vids/generations/" + generation_id + "/content"))
        self.assertEqual(response.status_code, 410)

    @unittest.skipUnless(AsyncOpenAI, "Optional OpenAI SDK is not installed")
    async def test_sdk_images_speech_and_videos_use_explicit_account_base_url(self):
        sdk_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        async with AsyncOpenAI(
            api_key=KEY,
            base_url="https://media.example.test" + self.url(""),
            max_retries=0,
            http_client=sdk_http,
        ) as sdk:
            image = await sdk.images.generate(
                **IMAGE, response_format="b64_json", extra_headers={"Idempotency-Key": "sdk-image"}
            )
            self.assertEqual(base64.b64decode(image.data[0].b64_json), JPEG)
            speech = await sdk.audio.speech.create(
                **SPEECH, extra_headers={"Idempotency-Key": "sdk-speech"}
            )
            self.assertTrue(speech.content.startswith(b"RIFF"))
            video = await sdk.videos.create(**VIDEO, extra_headers={"Idempotency-Key": "sdk-video"})
            self.assertEqual(video.status, "completed")
            self.assertEqual((await sdk.videos.retrieve(video.id)).id, video.id)
            self.assertEqual((await sdk.videos.list()).data[0].id, video.id)
            self.assertEqual((await sdk.videos.download_content(video.id)).content, MP4)
        for method in ("generate_image", "generate_speech", "submit_text"):
            getattr(self.gateways[1].client, method).assert_awaited_once()
            getattr(self.gateways[2].client, method).assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
