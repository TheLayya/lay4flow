"""Exercise Vids speech routes through real receipts with synthetic upstream audio."""

import hashlib
import tempfile
import unittest

import httpx

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from src.api.vids import create_vids_app
from src.services.vids.client import (
    PRESET_VOICE_OPTIONS,
    PRESET_VOICES,
    VidsProtocolError,
    VidsTransportError,
)
from src.services.vids.gateway import VidsGateway
from tests.test_vids_gateway import KEY, client as mock_client

BODY = {"model": "vids-speech", "input": "Hello, Vids.", "voice": "Puck", "response_format": "wav"}
WAV = b"RIFF" + bytes(4) + b"WAVEsynthetic"


def identifier(key):
    return "vids_" + hashlib.sha256(key.encode()).hexdigest()


class VidsSpeechAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_upstream_reason_survives_failed_receipt_and_never_resubmits(self):
        self.upstream.generate_speech.side_effect = VidsProtocolError(
            "generate",
            400,
            upstream_status="INVALID_ARGUMENT",
            upstream_reason="LANGUAGE_NOT_SUPPORTED",
        )
        body = {**BODY, "voice": "Algenib"}
        first = await self.speech("safe-speech-error", body)
        self.assertEqual(first.status_code, 502)
        receipt = first.json()["vids_receipt"]
        self.assertEqual(receipt["error_operation"], "generate")
        self.assertEqual(
            receipt["upstream_error"],
            {"status": "INVALID_ARGUMENT", "reason": "LANGUAGE_NOT_SUPPORTED"},
        )
        saved = self.gateway.get(identifier("safe-speech-error"))
        self.assertEqual(saved["stage"], "speech_generation")
        self.assertEqual(saved["upstream_error"], receipt["upstream_error"])
        self.assertNotIn("response", saved)
        self.assertEqual((await self.speech("safe-speech-error", body)).json(), first.json())
        self.upstream.generate_speech.assert_awaited_once_with(BODY["input"], voice="Algenib")
        self.upstream.download_audio.assert_not_awaited()

    async def test_all_official_preset_voices_reach_upstream_and_keep_receipt_voice(self):
        for voice in PRESET_VOICES:
            self.upstream.generate_speech.reset_mock()
            self.upstream.download_audio.reset_mock()
            key = "preset-" + voice
            body = {**BODY, "voice": voice}
            self.assert_wav(await self.speech(key, body))
            self.assert_wav(await self.speech(key, body))
            self.upstream.generate_speech.assert_awaited_once_with(BODY["input"], voice=voice)
            self.upstream.download_audio.assert_awaited_once()
            changed = await self.speech(
                key, {**body, "voice": "Kore" if voice != "Kore" else "Puck"}
            )
            self.assertEqual(changed.status_code, 409)
            self.assertEqual(changed.json()["error"]["code"], "idempotency_conflict")
        model = next(
            row
            for row in (await self.http.get("/v1/models")).json()["data"]
            if row["id"] == "vids-speech"
        )
        self.assertEqual(model["capabilities"]["voices"], list(PRESET_VOICES))
        self.assertEqual(model["capabilities"]["voice_options"], list(PRESET_VOICE_OPTIONS))
        self.assertEqual(tuple(row["id"] for row in PRESET_VOICE_OPTIONS), PRESET_VOICES)
        self.assertEqual(
            next(row for row in PRESET_VOICE_OPTIONS if row["id"] == "Puck"),
            {"id": "Puck", "name": "Neo", "description": "欢快、音调适中"},
        )
        self.assertEqual(model["capabilities"]["verified_voices"], ["Puck", "Gacrux", "Algenib"])

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.upstream = mock_client()
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

    async def speech(self, key, body=None, **options):
        return await self.http.post(
            "/v1/audio/speech",
            json=BODY if body is None else body,
            headers={"Idempotency-Key": key},
            **options,
        )

    def assert_wav(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.content, WAV)
        self.assertTrue(response.headers["x-request-id"].startswith("req_"))
        self.assertNotIn("google.com", str(response.headers))

    async def test_wav_replay_and_generic_route_share_one_receipt_in_both_directions(self):
        for generic_first in (False, True):
            with self.subTest(generic_first=generic_first):
                self.upstream.generate_speech.reset_mock()
                self.upstream.download_audio.reset_mock()
                key = "speech-cross-route-" + str(generic_first)
                text = "\n Hello, Vids. \t"
                body = {**BODY, "input": text}
                generic = {"kind": "speech", "prompt": text, "voice": "Puck"}
                if generic_first:
                    first = await self.http.post(
                        "/v1/vids/generations", json=generic, headers={"Idempotency-Key": key}
                    )
                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(first.json()["status"], "completed")
                self.assert_wav(await self.speech(key, body))
                self.assert_wav(await self.speech(key, body))
                receipt = await self.http.post(
                    "/v1/vids/generations", json=generic, headers={"Idempotency-Key": key}
                )
                self.assertEqual(receipt.status_code, 200)
                self.assertEqual(receipt.json()["id"], identifier(key))
                self.assertEqual(receipt.json()["status"], "completed")
                self.assertNotIn("google.com", receipt.text)
                self.assertNotIn("response", receipt.json())
                self.assert_wav(await self.http.get(receipt.json()["content_url"]))
                self.upstream.generate_speech.assert_awaited_once_with(text, voice="Puck")
                self.upstream.download_audio.assert_awaited_once()
                for changed in ({"prompt": "Different input"}, {"voice": "Kore"}):
                    conflict = await self.http.post(
                        "/v1/vids/generations",
                        json={**generic, **changed},
                        headers={"Idempotency-Key": key},
                    )
                    self.assertEqual(conflict.status_code, 409)
                    self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
                conflict = await self.speech(key, {**body, "input": "Different input"})
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
                self.upstream.generate_speech.assert_awaited_once()
        catalog = (await self.http.get("/v1/models")).json()["data"]
        model = next(row for row in catalog if row["id"] == "vids-speech")
        self.assertIn("/v1/audio/speech", model["supported_endpoints"])
        self.assertEqual(model["capabilities"]["verified_voices"], ["Puck", "Gacrux", "Algenib"])
        self.assertEqual(model["capabilities"]["speech_response_formats"], ["wav"])

    async def test_auth_invalid_fields_json_and_idempotency_fail_before_generation(self):
        invalid = [
            {field: value}
            for field, values in (
                ("model", (None, "tts-1", [], 254)),
                ("input", (None, "", " \t\n", 42, "x" * 4097)),
                ("voice", (None, "puck", "alloy", "unsupported", {})),
                ("response_format", (None, "mp3", "pcm", "WAV", [])),
            )
            for value in values
        ]
        invalid += [
            {field: value}
            for field, value in (
                ("speed", 1),
                ("instructions", "private"),
                ("stream_format", "audio"),
                ("prompt", "private"),
                ("kind", "speech"),
            )
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                response = await self.speech("invalid-speech", {**BODY, **changes})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.headers["content-type"], "application/json")
        for field in BODY:
            response = await self.speech(
                "missing-field", {k: v for k, v in BODY.items() if k != field}
            )
            self.assertEqual(response.status_code, 400)
        for headers in ({}, {"Idempotency-Key": ""}, {"Idempotency-Key": "x" * 201}):
            response = await self.http.post("/v1/audio/speech", json=BODY, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "idempotency_required")
        for authorization in ("", "Bearer private-invalid"):
            unauthorized = await self.http.post(
                "/v1/audio/speech",
                json=BODY,
                headers={"Idempotency-Key": "unauthorized", "Authorization": authorization},
            )
            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(unauthorized.json()["error"]["code"], "invalid_api_key")
            self.assertNotIn("private", unauthorized.text)
        for content in (b"not-json", b"[]", b"null"):
            response = await self.http.post(
                "/v1/audio/speech",
                content=content,
                headers={"Idempotency-Key": "invalid-json", "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 400)
        self.upstream.generate_speech.assert_not_awaited()
        self.upstream.download_audio.assert_not_awaited()
        self.assertEqual(list(self.gateway.root.glob("vids_*.json")), [])

    async def test_unknown_acceptance_keeps_receipt_and_never_resubmits_even_after_restart(self):
        for index, error in enumerate(
            (VidsTransportError("private-url"), VidsProtocolError("generate", 503))
        ):
            with self.subTest(error=type(error).__name__):
                self.upstream.generate_speech.reset_mock()
                self.upstream.generate_speech.side_effect = error
                key = "unknown-speech-" + str(index)
                first = await self.speech(key)
                repeated = await self.speech(key)
                self.assertEqual(first.status_code, 409)
                self.assertEqual(repeated.status_code, 409)
                self.assertEqual(first.json()["error"]["code"], "speech_not_ready")
                self.assertIn(identifier(key), first.json()["error"]["message"])
                self.assertNotIn("private", first.text)
                self.assertEqual(first.headers["content-type"], "application/json")
                record = self.gateway.get(identifier(key))
                self.assertEqual(record["status"], "unknown")
                self.assertNotIn("response", record)
                self.assertIsNotNone(record["request_hash"])
                receipt = await self.http.get("/v1/vids/generations/" + identifier(key))
                self.assertEqual(receipt.json()["status"], "unknown")
                self.assertIsNone(receipt.json()["content_url"])
                recovery = await self.http.post(
                    "/v1/vids/generations/" + identifier(key) + "/download"
                )
                self.assertEqual(recovery.status_code, 409)
                self.upstream.generate_speech.assert_awaited_once()
                self.upstream.download_audio.assert_not_awaited()
        self.upstream.generate_speech.side_effect = None
        await self.gateway.close()
        await self.gateway.start()
        self.assertEqual((await self.speech(key)).status_code, 409)
        self.upstream.generate_speech.assert_awaited_once()

    async def test_failed_generation_and_no_media_are_json_errors_with_replayable_receipts(self):
        for index, result in enumerate((VidsProtocolError("generate", 400), [])):
            with self.subTest(result=result):
                self.upstream.generate_speech.reset_mock()
                self.upstream.generate_speech.side_effect = (
                    result if isinstance(result, Exception) else None
                )
                self.upstream.generate_speech.return_value = []
                key = "failed-speech-" + str(index)
                first = await self.speech(key)
                self.assertEqual(first.status_code, 502)
                self.assertEqual(first.headers["content-type"], "application/json")
                self.assertEqual(first.json()["error"]["code"], "speech_failed")
                receipt = first.json()["vids_receipt"]
                self.assertEqual(receipt["id"], identifier(key))
                self.assertEqual(receipt["kind"], "speech")
                self.assertEqual(
                    receipt["error"],
                    "upstream_http_400" if index == 0 else "upstream_returned_no_media",
                )
                if index == 0:
                    self.assertEqual(receipt["error_operation"], "generate")
                self.assertNotIn("response", receipt)
                self.assertIn(identifier(key), first.json()["error"]["message"])
                saved = self.gateway.get(identifier(key))
                self.assertEqual(saved["status"], "failed" if index == 0 else "no_result")
                if index == 1:
                    self.assertEqual(saved["response"], [])
                self.assertEqual((await self.speech(key)).status_code, 502)
                self.upstream.generate_speech.assert_awaited_once()
                self.upstream.download_audio.assert_not_awaited()

    async def test_download_failure_retains_response_and_explicit_recovery_never_generates(self):
        download = self.upstream.download_audio.side_effect
        self.upstream.download_audio.side_effect = VidsTransportError("private-url")
        key = "download-failed-speech"
        first = await self.speech(key)
        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.headers["content-type"], "application/json")
        self.assertIn(identifier(key), first.json()["error"]["message"])
        self.assertNotIn("private", first.text)
        saved = self.gateway.get(identifier(key))
        self.assertEqual(saved["status"], "download_failed")
        self.assertEqual(saved["response"], self.upstream.generate_speech.return_value)
        self.assertEqual((await self.speech(key)).status_code, 502)
        self.upstream.generate_speech.assert_awaited_once()
        self.upstream.download_audio.assert_awaited_once()
        self.upstream.download_audio.side_effect = download
        recovered = await self.http.post("/v1/vids/generations/" + identifier(key) + "/download")
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["status"], "completed")
        self.assert_wav(await self.speech(key))
        self.upstream.generate_speech.assert_awaited_once()
        self.assertEqual(self.upstream.download_audio.await_count, 2)

    @unittest.skipUnless(AsyncOpenAI, "Optional OpenAI SDK is not installed")
    async def test_sdk_speech_reads_binary_wav_and_replays_without_new_submission(self):
        sdk_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        async with AsyncOpenAI(
            api_key=KEY,
            base_url="https://vids.example.test/v1",
            max_retries=0,
            http_client=sdk_http,
        ) as sdk:
            for _ in range(2):
                response = await sdk.audio.speech.create(
                    model="vids-speech",
                    input=BODY["input"],
                    voice="Puck",
                    response_format="wav",
                    extra_headers={"Idempotency-Key": "sdk-speech"},
                )
                self.assertEqual(response.content, WAV)
                self.assertEqual(response.response.headers["content-type"], "audio/wav")
        self.upstream.generate_speech.assert_awaited_once_with(BODY["input"], voice="Puck")
        self.upstream.download_audio.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
