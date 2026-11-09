"""Offline regression tests for bounded Vids reads and media credential boundaries."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from curl_cffi.requests.exceptions import RequestException

from src.services.vids.client import (
    DOCS_ORIGIN,
    READ_RETRY_DELAYS,
    READ_RETRY_STATUSES,
    VidsClient,
    VidsProtocolError,
    VidsTransportError,
    cookie_jar,
    image_video_feature,
    video_feature,
)
from tests.test_vids_video_input import mp4


class VidsClientReadTests(unittest.TestCase):
    def setUp(self):
        config_patch = patch(
            "src.services.vids.client.config.get_raw_config", return_value={"vids": {}}
        )
        self.config = config_patch.start()
        self.addCleanup(config_patch.stop)
        session_patch = patch("src.services.vids.client.AsyncSession")
        self.session = session_patch.start().return_value
        self.addCleanup(session_patch.stop)
        sleep_patch = patch("src.services.vids.client.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
        self.client.flags = {"espresso_api_key": "synthetic-key", "docs-est": "synthetic-token"}
        self.client.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
        self.session.cookies.jar = cookie_jar(
            [{"name": "SAPISID", "value": "synthetic", "domain": ".google.com", "secure": True}]
        )
        self.checkpoints = Mock()
        self.client.on_cookies_changed = self.checkpoints

    @staticmethod
    def response(status=200, **values):
        return SimpleNamespace(status_code=status, json=lambda: ["ok"], headers={}, **values)

    def test_split_feature_flag_and_legacy_precedence(self):
        for value in (True, 1, "true", "1"):
            flags = {"flix-evg1p2ui": value}
            self.assertEqual(video_feature(flags), 374)
            self.assertEqual(image_video_feature(flags, "animate"), 375)
            self.assertEqual(image_video_feature(flags, "ingredients"), 376)
        for value in (False, 0, "false", "0", None):
            self.assertEqual(video_feature({"flix-evg1p2ui": value}), 104)
        self.assertEqual(video_feature({"flix-evga": 1}), 342)
        flags = {"flix-evg1p2ui": 1, "flix-evga": 1}
        for flag, expected in (("flix-eof", 367), ("flix-eofpsq", 386), ("flix-evgvl", 257)):
            flags[flag] = 1
            self.assertEqual(video_feature(flags), expected)

    def test_each_explicit_protocol_read_retries_transient_transport_failures(self):
        for read in (self.client.quota, self.client.settings, lambda: self.client.operation("op")):
            self.session.request = AsyncMock(
                side_effect=[
                    RequestException("private-url"),
                    TimeoutError("private-proxy"),
                    self.response(),
                ]
            )
            self.assertEqual(asyncio.run(read()), ["ok"])
            self.assertEqual(self.session.request.await_count, 3)
        self.assertEqual(self.checkpoints.call_count, 9)
        self.assertEqual(
            [call.args[0] for call in self.sleep.await_args_list], list(READ_RETRY_DELAYS[:2]) * 3
        )

    def test_read_attempt_configuration_is_bounded_and_independent_of_paid_submits(self):
        for value, expected in (
            (1, 1),
            (5, 5),
            (99, 5),
            (0, 1),
            (-1, 1),
            (True, 3),
            ("5", 3),
            (None, 3),
        ):
            self.config.return_value = {"vids": {"read_max_attempts": value}}
            self.session.request = AsyncMock(return_value=self.response(503))
            self.sleep.reset_mock()
            with self.subTest(value=value), self.assertRaises(VidsProtocolError):
                asyncio.run(self.client.quota())
            self.assertEqual(self.session.request.await_count, expected)
            self.assertEqual(self.sleep.await_count, expected - 1)
            self.assertTrue(all(call.args[0] <= 2 for call in self.sleep.await_args_list))
            self.session.request.reset_mock()
            with self.assertRaises(VidsProtocolError):
                asyncio.run(self.client._request("POST", "/v1/genai/generate", []))
            self.session.request.assert_awaited_once()

    def test_only_transient_http_statuses_retry_and_exhaustion_is_bounded(self):
        for status in READ_RETRY_STATUSES:
            self.session.request = AsyncMock(return_value=self.response(status))
            with self.subTest(status=status), self.assertRaises(VidsProtocolError) as caught:
                asyncio.run(self.client.quota())
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(self.session.request.await_count, 3)
        for status in (302, 400, 403, 404, 409, 501):
            self.session.request = AsyncMock(return_value=self.response(status))
            with self.subTest(status=status), self.assertRaises(VidsProtocolError):
                asyncio.run(self.client.quota())
            self.session.request.assert_awaited_once()

    def test_transport_exhaustion_redacts_details_and_preserves_cookie_checkpoints(self):
        self.session.request = AsyncMock(side_effect=RequestException("private-url-and-token"))
        with self.assertRaises(VidsTransportError) as caught:
            asyncio.run(self.client.quota())
        self.assertEqual(self.session.request.await_count, 3)
        self.assertEqual(self.checkpoints.call_count, 3)
        self.assertNotIn("private", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_bootstrap_retry_checkpoints_only_the_final_validated_identity(self):
        flags = {
            "docs-gaopd": "appsgenaiserver-pa.clients6.google.com",
            "espresso_api_key": "synthetic-key",
            "docs-est": "synthetic-token",
            "docs-hue": "new-synthetic-account",
        }
        self.session.get = AsyncMock(
            side_effect=[
                self.response(503),
                self.response(text="_docs_flag_initialData=" + json.dumps(flags)),
            ]
        )
        identities = []
        self.client.on_cookies_changed = lambda: identities.append(self.client.flags["docs-hue"])
        asyncio.run(self.client.bootstrap())
        self.assertEqual(self.session.get.await_count, 2)
        self.assertEqual(identities, ["new-synthetic-account"])
        self.session.get = AsyncMock(return_value=self.response(text="invalid editor"))
        with self.assertRaises(ValueError):
            asyncio.run(self.client.bootstrap())
        self.session.get.assert_awaited_once()
        self.assertEqual(identities, ["new-synthetic-account"])
        self.assertFalse(self.client.initialization_valid)

    def test_cookie_rotation_refreshes_signatures_between_read_attempts(self):
        async def request(*args, **kwargs):
            if self.session.request.await_count == 1:
                next(iter(self.session.cookies.jar)).value = "rotated-synthetic"
                return self.response(503)
            return self.response()

        self.session.request = AsyncMock(side_effect=request)
        with patch("src.services.vids.client.time.time", return_value=1700000000):
            asyncio.run(self.client.quota())
        signatures = [
            call.kwargs["headers"]["Authorization"] for call in self.session.request.await_args_list
        ]
        self.assertNotEqual(*signatures)
        self.assertEqual(self.checkpoints.call_count, 2)

    def test_cookie_checkpoint_failure_does_not_retry_a_completed_read(self):
        self.session.request = AsyncMock(return_value=self.response(503))
        self.client.on_cookies_changed = Mock(side_effect=OSError("private-disk-error"))
        with self.assertRaises(VidsTransportError):
            asyncio.run(self.client.quota())
        self.session.request.assert_awaited_once()
        self.client.on_cookies_changed.assert_called_once()
        self.sleep.assert_not_awaited()

    def test_401_refresh_has_one_budget_and_paid_or_other_requests_never_replay(self):
        self.session.request = AsyncMock(
            side_effect=[
                self.response(503),
                self.response(401),
                self.response(503),
                self.response(401),
            ]
        )
        self.client.bootstrap = AsyncMock()
        with self.assertRaises(VidsProtocolError) as caught:
            asyncio.run(self.client.quota())
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.session.request.await_count, 4)
        self.client.bootstrap.assert_awaited_once()
        for method, path in (
            ("POST", "/v1/genai/generate"),
            ("GET", "/v1/genai/generate"),
            ("PUT", "/v1/genai/quotaSummary"),
            ("POST", "/v1/operations/op"),
        ):
            for failure in (self.response(503), self.response(401), TimeoutError("private")):
                self.session.request = AsyncMock(side_effect=[failure])
                with self.subTest(method=method, path=path, failure=failure):
                    with self.assertRaises((VidsProtocolError, VidsTransportError)):
                        asyncio.run(self.client._request(method, path, []))
                    self.session.request.assert_awaited_once()
        self.client.bootstrap.assert_awaited_once()

    def test_cancellation_during_request_or_backoff_does_not_retry(self):
        self.session.request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.client.quota())
        self.session.request.assert_awaited_once()
        self.checkpoints.assert_called_once()
        self.sleep.assert_not_awaited()
        self.session.request = AsyncMock(return_value=self.response(503))
        self.sleep.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.client.quota())
        self.session.request.assert_awaited_once()
        self.assertEqual(self.checkpoints.call_count, 2)

    def test_upload_start_finalize_and_document_image_upload_remain_single_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.png"
            source.write_bytes(b"synthetic-image")
            self.client.flags.update({"docs-tbue": 1, "docs-tbuu": "/upload/temporaryblob/videos"})
            self.session.post = AsyncMock(side_effect=TimeoutError("private"))
            self.session.put = AsyncMock()
            with self.assertRaises(VidsTransportError):
                asyncio.run(self.client.upload_temporary(source))
            self.session.post.assert_awaited_once()
            self.session.put.assert_not_awaited()
            self.session.post = AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200, headers={"X-Goog-Upload-URL": "https://docs.google.com/upload"}
                )
            )
            self.session.put = AsyncMock(side_effect=TimeoutError("private"))
            with self.assertRaises(VidsTransportError):
                asyncio.run(self.client.upload_temporary(source))
            self.session.post.assert_awaited_once()
            self.session.put.assert_awaited_once()
            self.client.flags.update(
                {
                    "scotty_upload_url": "/upload/videos/resumable",
                    "info_params": {"token": "synthetic-token", "ouid": "synthetic-user"},
                }
            )
            self.session.post = AsyncMock(return_value=self.response(503))
            with self.assertRaises(VidsProtocolError):
                asyncio.run(self.client.upload_image(source, mime_type="image/png"))
            self.session.post.assert_awaited_once()

    def test_video_follows_validated_redirects_and_retries_without_generation_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            self.session.get = AsyncMock(
                side_effect=[
                    self.response(503),
                    SimpleNamespace(
                        status_code=302,
                        headers={"Location": "https://lh3.googleusercontent.com/video"},
                    ),
                    RequestException("private-signed-url"),
                    self.response(content=mp4(3)),
                ]
            )
            output = asyncio.run(
                self.client.download_video(
                    {"url": "https://contribution-rt.usercontent.google.com/start"},
                    Path(directory) / "video.mp4",
                )
            )
            self.assertEqual(output.read_bytes(), mp4(3))
            self.assertFalse(output.with_suffix(".mp4.part").exists())
            self.assertEqual(self.session.get.await_count, 4)
            self.assertEqual(self.checkpoints.call_count, 4)
            for call in self.session.get.await_args_list:
                self.assertFalse(call.kwargs["allow_redirects"])
                self.assertEqual(call.kwargs["headers"], {"Referer": DOCS_ORIGIN + "/"})
                self.assertNotIn("params", call.kwargs)

    def test_untrusted_video_redirects_are_rejected_before_a_second_request(self):
        for destination in (
            "https://attacker.example/video",
            "http://lh3.googleusercontent.com/video",
            "https://lh3.googleusercontent.com:444/video",
            "https://lh3.googleusercontent.com:private-port/video",
            "https://private-user@lh3.googleusercontent.com/video",
            "https://lh3.googleusercontent.com.attacker.example/video",
            "https://docs.google.com/videos-images-rt/image",
            "https://work.fife.usercontent.google.com/video",
            "https://lh3.googleusercontent.com/\\video",
            "\nhttps://lh3.googleusercontent.com/video",
            "https://lh3.googleusercontent.com/vi\tdeo",
            "",
        ):
            with tempfile.TemporaryDirectory() as directory:
                self.session.get = AsyncMock(
                    return_value=SimpleNamespace(status_code=302, headers={"Location": destination})
                )
                output = Path(directory) / "video.mp4"
                with self.subTest(destination=destination), self.assertRaises(ValueError) as caught:
                    asyncio.run(
                        self.client.download_video(
                            {"url": "https://contribution-rt.usercontent.google.com/start"}, output
                        )
                    )
                self.session.get.assert_awaited_once()
                self.assertFalse(output.exists())
                self.assertNotIn("private", str(caught.exception))

    def test_relative_redirect_fifth_hop_succeeds_and_sixth_hop_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            redirects = [
                SimpleNamespace(status_code=307, headers={"Location": f"/v{n}"}) for n in range(5)
            ]
            self.session.get = AsyncMock(side_effect=[*redirects, self.response(content=mp4(3))])
            output = Path(directory) / "video.mp4"
            asyncio.run(
                self.client.download_video(
                    {"url": "https://lh3.googleusercontent.com/start"}, output
                )
            )
            self.assertEqual(self.session.get.await_count, 6)
            self.session.get = AsyncMock(return_value=redirects[0])
            with self.assertRaisesRegex(ValueError, "redirect limit"):
                asyncio.run(
                    self.client.download_video(
                        {"url": "https://lh3.googleusercontent.com/start"}, output
                    )
                )
            self.assertEqual(self.session.get.await_count, 6)
            self.assertEqual(output.read_bytes(), mp4(3))

    def test_download_type_errors_and_cancellation_preserve_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "video.mp4"
            output.write_bytes(b"previous")
            for response in (self.response(content=b"not-mp4"), asyncio.CancelledError()):
                self.session.get = AsyncMock(side_effect=[response])
                with self.assertRaises((ValueError, asyncio.CancelledError)):
                    asyncio.run(
                        self.client.download_video(
                            {"url": "https://lh3.googleusercontent.com/video"}, output
                        )
                    )
                self.session.get.assert_awaited_once()
                self.assertEqual(output.read_bytes(), b"previous")
                self.assertFalse(output.with_suffix(".mp4.part").exists())


if __name__ == "__main__":
    unittest.main()
