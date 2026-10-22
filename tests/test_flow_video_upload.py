import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.browser.flow_video_upload import upload_profile_video
from src.services.flow.flow_rpc import FlowRPCError


class Response:
    def __init__(
        self, *, status=200, url="https://flow.google.com/project/p", text="", headers=None
    ):
        self.status = status
        self.url = url
        self.headers = headers or {}
        self._text = text

    async def text(self):
        return self._text


def home():
    config = {
        "oPEP7c": "fixture@example.test",
        "SNlM0e": "synthetic-at",
        "eptZe": "/_/AiSandboxAngularFrontend/",
        "cfb2h": "synthetic-build",
        "FdrFJe": "synthetic-sid",
        "QrtxK": "0",
    }
    return Response(text="<script>window.WIZ_global_data = " + json.dumps(config) + ";</script>")


class FlowVideoUploadTests(unittest.IsolatedAsyncioTestCase):
    def context(self, *, start=None, final=None, cookies=None):
        request = SimpleNamespace(
            get=AsyncMock(return_value=home()),
            post=AsyncMock(side_effect=[start, final]),
        )
        return SimpleNamespace(request=request, cookies=AsyncMock(return_value=cookies or []))

    async def test_cookie_authenticated_resumable_start_and_finalize(self):
        start = Response(headers={"X-Goog-Upload-URL": "https://flow.google.com/upload/session-1"})
        final = Response(text=json.dumps({"media": {"name": "media/uploaded"}}))
        context = self.context(
            start=start,
            final=final,
            cookies=[{"name": "XSRF-TOKEN", "value": "csrf%2Ffixture", "httpOnly": False}],
        )
        media = await upload_profile_video(context, "projects/demo", b"video", "video/mp4", 0, 1000)
        self.assertEqual(media, "media/uploaded")
        context.request.get.assert_awaited_once()
        self.assertEqual(context.request.post.await_count, 2)
        first, second = context.request.post.await_args_list
        self.assertEqual(first.args[0], "https://flow.google.com/upload/v1/flow/upload/video/demo")
        self.assertEqual(first.kwargs["data"], b"")
        self.assertEqual(first.kwargs["headers"]["X-Framework-Xsrf-Token"], "csrf/fixture")
        self.assertEqual(first.kwargs["headers"]["X-Goog-Upload-Command"], "start")
        self.assertEqual(second.args[0], "https://flow.google.com/upload/session-1")
        self.assertEqual(second.kwargs["data"], b"video")
        self.assertEqual(second.kwargs["headers"]["X-Goog-Upload-Command"], "upload, finalize")
        self.assertNotIn("Authorization", first.kwargs["headers"])
        self.assertNotIn("Authorization", second.kwargs["headers"])

    async def test_missing_xsrf_cookie_is_allowed_like_official_source(self):
        start = Response(headers={"X-Goog-Upload-URL": "/upload/session-2"})
        final = Response(text=json.dumps({"mediaId": "media/2"}))
        context = self.context(start=start, final=final)
        self.assertEqual(
            await upload_profile_video(context, "demo", b"x", "video/webm", 10, 20), "media/2"
        )
        self.assertNotIn(
            "X-Framework-Xsrf-Token", context.request.post.await_args_list[0].kwargs["headers"]
        )

    async def test_never_substitutes_wiz_at_and_rejects_untrusted_upload_url(self):
        for target in (
            "https://evil.example/upload/session",
            "https://flow.google.com:444/upload/session",
            "https://user@flow.google.com/upload/session",
            "https://flow.google.com/upload/%2e%2e/api/side-effect",
            "http://flow.google.com/upload/session",
            "https://flow.google.com/api/side-effect",
        ):
            with self.subTest(target=target):
                start = Response(headers={"X-Goog-Upload-URL": target})
                context = self.context(start=start, final=Response(text="{}"))
                with self.assertRaises(FlowRPCError) as caught:
                    await upload_profile_video(context, "demo", b"x", "video/mp4", 0, 1)
                self.assertEqual(caught.exception.reason, "account_probe_error")
                self.assertEqual(context.request.post.await_count, 1)
                self.assertNotIn(
                    "Authorization", context.request.post.await_args_list[0].kwargs["headers"]
                )

    async def test_proto_xssi_result_and_unknown_transport_errors_are_bounded(self):
        context = self.context(
            start=Response(headers={"X-Goog-Upload-URL": "/upload/session"}),
            final=Response(text=")]}'\n" + json.dumps([None, None, ["media/proto"]])),
        )
        self.assertEqual(
            await upload_profile_video(context, "demo", b"x", "video/mp4", 0, 1), "media/proto"
        )
        context = self.context(start=Response(), final=Response())
        context.request.post.side_effect = TimeoutError("private-upload-url-and-cookie")
        with self.assertRaises(FlowRPCError) as caught:
            await upload_profile_video(context, "demo", b"x", "video/mp4", 0, 1)
        self.assertNotIn("private-upload-url-and-cookie", str(caught.exception))
        self.assertEqual(context.request.post.await_count, 1)

    async def test_http_failures_and_unknown_finalize_do_not_retry(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                context = self.context(start=Response(status=status), final=Response(text="{}"))
                with self.assertRaises(FlowRPCError) as caught:
                    await upload_profile_video(context, "demo", b"x", "video/mp4", 0, 1)
                self.assertEqual(caught.exception.network_step, "flow_upload_start")
                self.assertEqual(context.request.post.await_count, 1)
        context = self.context(
            start=Response(headers={"X-Goog-Upload-URL": "/upload/session"}),
            final=Response(text="not-json"),
        )
        with self.assertRaises(FlowRPCError) as caught:
            await upload_profile_video(context, "demo", b"x", "video/mp4", 0, 1)
        self.assertEqual(caught.exception.network_step, "flow_upload_finalize")
        self.assertEqual(context.request.post.await_count, 2)

    async def test_input_validation_prevents_network(self):
        context = self.context(start=Response(), final=Response(text="{}"))
        for args in (
            ("../../escape", b"x", "video/mp4", 0, 1),
            ("demo", b"", "video/mp4", 0, 1),
            ("demo", b"x", "video/mp4", 1, 1),
            ("demo", b"x", "application/octet-stream", 0, 1),
        ):
            with self.subTest(args=args), self.assertRaises((ValueError, FlowRPCError)):
                await upload_profile_video(context, *args)
        context.request.get.assert_not_awaited()
        context.request.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
