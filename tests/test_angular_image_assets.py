"""Synthetic fixtures for the observed current Flow image asset RPCs."""

import base64
import unittest

from src.services.flow.angular_image_assets import (
    UPLOAD_IMAGE_RPC,
    UPSAMPLE_IMAGE_RPC,
    build_upload_image_request,
    build_upsample_image_request,
    parse_upload_image_response,
    parse_upsample_image_response,
)


class AngularImageAssetTests(unittest.TestCase):
    def test_upload_request_matches_frontend_fields_and_encodes_bytes(self):
        raw = b"\x89PNG\r\n\x1a\nsynthetic-image"
        self.assertEqual(UPLOAD_IMAGE_RPC, "maseQ")
        request = build_upload_image_request("project-fixture", raw, "image/png", "fixture.png")
        self.assertEqual(
            request,
            [
                [None, 22, None, None, None, "project-fixture"],
                base64.b64encode(raw).decode("ascii"),
                "image/png",
                True,
                None,
                None,
                None,
                None,
                "fixture.png",
            ],
        )
        self.assertEqual(base64.b64decode(request[1]), raw)

    def test_upload_response_uses_returned_media_name(self):
        self.assertEqual(
            parse_upload_image_response([["media/actual-upload"]]), "media/actual-upload"
        )
        self.assertEqual(parse_upload_image_response([["short-id", None, "workflow"]]), "short-id")
        for payload in ({}, [], [None], [[]], [[None]], [[""]], [[True]], ["media/guess"]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_upload_image_response(payload)

    def test_upload_preserves_binary_zeroes_and_unicode_filename(self):
        request = build_upload_image_request("project", b"\x00\x00\xff", "image/webp", "灯笼.webp")
        self.assertEqual(base64.b64decode(request[1]), b"\x00\x00\xff")
        self.assertEqual(request[8], "灯笼.webp")

    def test_invalid_upload_does_not_build_a_guessed_request(self):
        args = dict(
            project_id="project", image_bytes=b"synthetic", mime_type="image/png", file_name="f.png"
        )
        for change in (
            {"project_id": ""},
            {"image_bytes": b""},
            {"image_bytes": "base64"},
            {"mime_type": "text/plain"},
            {"mime_type": "image/png\n"},
            {"file_name": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_upload_image_request(**(args | change))

    def test_upsample_request_uses_download_resolution_enum_and_context(self):
        self.assertEqual(UPSAMPLE_IMAGE_RPC, "SPrCad")
        for resolution, enum in (
            ("2K", 1),
            ("4K", 2),
            ("UPSAMPLE_IMAGE_RESOLUTION_2K", 1),
            ("UPSAMPLE_IMAGE_RESOLUTION_4K", 2),
        ):
            with self.subTest(resolution=resolution):
                request = build_upsample_image_request(
                    "project", "media/source", resolution, "captcha"
                )
                self.assertEqual(
                    request,
                    [
                        "media/source",
                        enum,
                        [
                            None,
                            22,
                            None,
                            None,
                            None,
                            "project",
                            None,
                            None,
                            None,
                            None,
                            ["captcha", 1],
                        ],
                    ],
                )

    def test_invalid_upsample_input_is_rejected_instead_of_using_image_model_enum(self):
        args = dict(
            project_id="project",
            media_id="media/source",
            target_resolution="2K",
            captcha_token="captcha",
        )
        for change in (
            {"project_id": ""},
            {"media_id": ""},
            {"target_resolution": "1K"},
            {"target_resolution": "GEM_PIX_2_UPSAMPLE_2K"},
            {"target_resolution": 1},
            {"captcha_token": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_upsample_image_request(**(args | change))

    def test_upsample_response_downloads_field2_bytes_instead_of_media_url(self):
        raw = b"\xff\xd8\xffsynthetic-jpeg"
        encoded = base64.b64encode(raw).decode("ascii")
        media = ["media/optional"] + [None] * 5 + [[None] * 13 + ["https://media.invalid/unused"]]
        self.assertEqual(parse_upsample_image_response([media, encoded]), encoded)
        self.assertEqual(parse_upsample_image_response([None, encoded]), encoded)
        unpadded_urlsafe = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        self.assertEqual(parse_upsample_image_response([None, unpadded_urlsafe]), encoded)

    def test_empty_invalid_or_url_only_upsample_response_is_not_success(self):
        for payload in (
            {},
            [],
            [None],
            [None, None],
            [None, ""],
            [None, "%%%"],
            [None, "https://media.invalid/output"],
            [None, "é"],
            [None, "A"],
            [None, [1, 2, 3]],
            [["media/id"]],
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_upsample_image_response(payload)

    def test_upsample_response_normalizes_unpadded_standard_base64(self):
        self.assertEqual(parse_upsample_image_response([None, "AA"]), "AA==")


if __name__ == "__main__":
    unittest.main()
