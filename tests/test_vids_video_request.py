"""Synthetic complete wires derived from official cus/Fts/Vts/Hts, without requests."""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.services.vids.client import VidsClient, build_video_request

DOCUMENT = ["contract-document", "application/vnd.google-apps.flix", None, None, 1]
SOURCE = {
    "corpus": "synthetic-corpus",
    "lookup_schema": "synthetic-schema",
    "unique_key": "synthetic-key",
    "duration": 3,
}
FLAGS = {"flix-evga": 1, "flix-evg1p2ui": 1, "flix-evge": 1}
CONVERSATION = "0123456789abcdef0123456789abcdef"
CLIENT_UUID = "00000000-0000-4000-8000-000000000001"


def source_node(*, named=False):
    # Vts: ogh field 8 names the resource; Hts leaves it unnamed.
    container = [None, None, ["synthetic-corpus", None, "synthetic-schema", "synthetic-key"]]
    if named:
        container += [None, None, None, None, "source_video"]
    return [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        container,
    ]


def expected_wire(feature, input_value, video_config, references=None):
    # Complete VSh with independent, fixed synthetic document/conversation context.
    document_reference = [None, None, None, None, None, None, None, [None, None, [DOCUMENT]]]
    context = [
        9,
        None,
        None,
        None,
        "goog_contract",
        None,
        "contract-request",
        None,
        [None, None, None, [[document_reference]]],
        None,
        None,
        [0, 0],
        None,
        "en",
        None,
        None,
        None,
        None,
        None,
        1,
        None,
        None,
        None,
        [[[DOCUMENT]]],
        None,
        references,
        None,
        None,
        CONVERSATION,
        ["1700000000", 123000000],
        0,
        CLIENT_UUID,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0,
    ]
    config = [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        video_config,
    ]
    return [feature, None, context, input_value, config, [1, None, [[None, "1", 1189]]], 1]


class VidsVideoRequestContractTests(unittest.TestCase):
    def test_uploaded_video_matches_executed_official_temporary_resource_builder(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "vids-temporary-video-resource.json").read_text(
                encoding="utf-8"
            )
        )
        for mode in ("edit", "extend", "upscale"):
            with self.subTest(mode=mode):
                request = build_video_request(
                    "contract-document",
                    FLAGS,
                    "Update the supplied scene",
                    {"temporary_blob": fixture["token"], "duration": 10},
                    mode=mode,
                )
                if mode == "upscale":
                    node = request[3][3][0][0]
                    expected = fixture["named_video_node"][:]
                    expected[25] = expected[25][:6]
                    self.assertEqual(node, expected)
                else:
                    self.assertEqual(request[2][25][0], fixture["named_reference"])
                    node = request[2][25][0][0][7][1]
                self.assertEqual(node[25][5], fixture["temporary_video"])

    def test_uploaded_edit_preserves_temporary_resource_and_explicit_output_options(self):
        request = build_video_request(
            "contract-document",
            FLAGS,
            "Turn it into evening",
            {"temporary_blob": "uploaded-mp4", "duration": 12.5},
            mode="edit",
            seconds=8,
            resolution="1080p",
            orientation="portrait",
            shot_mode="montage",
        )
        self.assertEqual(
            request[4][15], [0, 12, None, 2, 6, None, ["2", 500000000], ["12", 500000000], 8]
        )
        node = request[2][25][0][0][7][1]
        self.assertEqual(
            node[25],
            [
                None,
                None,
                None,
                None,
                None,
                [None, None, None, "uploaded-mp4", 1],
                None,
                "source_video",
            ],
        )
        self.assertEqual(len(node), 26)

    def test_all_image_style_and_aspect_values_reach_native_configuration(self):
        with patch("src.services.vids.client.AsyncSession"):
            client = VidsClient("https://docs.google.com/videos/d/contract-document/edit", [])
            client.flags = FLAGS
            client.settings = AsyncMock()
            client._request = AsyncMock(return_value=[])
            for style, native in (
                ("auto", 0),
                ("photography", 1),
                ("background", 3),
                ("vector", 8),
                ("sketch", 2),
                ("watercolor", 7),
                ("cyberpunk", 10),
            ):
                for aspect, value in (("square", 1), ("landscape", 2), ("portrait", 5)):
                    asyncio.run(client.generate_image("A scene", style=style, aspect=aspect))
                    self.assertEqual(
                        client._request.await_args.args[2][4], [[native, 1, None, value]]
                    )

    def test_three_image_ingredients_submit_complete_wire_preserves_names_and_order(self):
        references = []
        # Official Qts/Sts/Rts: nR field9 has temporary input, field14 its name.
        for name, token in (
            ("image_1", "first-blob"),
            ("image_2", "second-blob"),
            ("image_3", "third-blob"),
        ):
            image = [
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [None, None, None, token, 1],
                None,
                None,
                None,
                None,
                name,
            ]
            node = [None, None, None, None, None, None, None, None, None, None, None, image]
            references.append([[None, None, None, None, None, None, None, [name, node]]])
        prompt = "Show all supplied objects together."
        expected = expected_wire(
            376,
            [None, None, None, [[[None, None, prompt]]]],
            [0, 12, None, 0, 1, None, None, None, 3],
            references,
        )
        with (
            patch("src.services.vids.client.AsyncSession"),
            patch("src.services.vids.client.uuid.uuid4", return_value=CLIENT_UUID),
        ):
            client = VidsClient("https://docs.google.com/videos/d/contract-document/edit", [])
            client.flags = FLAGS
            client.settings = AsyncMock()
            client._request = AsyncMock(return_value=[])
            asyncio.run(
                client.submit_images(
                    ["first-blob", "second-blob", "third-blob"],
                    prompt,
                    mode="ingredients",
                    session_id="goog_contract",
                    request_id="contract-request",
                    conversation_id=CONVERSATION,
                    conversation_time_ms=1700000000123,
                )
            )
        client._request.assert_awaited_once_with("POST", "/v1/genai/generate", expected)

    def request(self, mode, *, duration=3, **options):
        with patch("src.services.vids.client.uuid.uuid4", return_value=CLIENT_UUID):
            return build_video_request(
                "contract-document",
                FLAGS,
                "Continue the scene." if mode == "extend" else "",
                {**SOURCE, "duration": duration},
                mode=mode,
                session_id="goog_contract",
                request_id="contract-request",
                conversation_id=CONVERSATION,
                conversation_time_ms=1700000000123,
                **options,
            )

    def extension_wire(self, video_config):
        # Official Fts keeps mode 3 prompt flat; Vts/Rts put the source in L3r.
        reference = [
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            ["source_video", source_node(named=True)],
        ]
        return expected_wire(
            378,
            [None, None, "Continue the scene."],
            video_config,
            [[reference]],
        )

    def test_three_second_source_extension_complete_wire(self):
        expected = self.extension_wire([0, 12, None, 0, 1, None, None, None, 3])
        self.assertEqual(self.request("extend"), expected)

    def test_upscale_complete_wire_uses_source_duration_and_orientation(self):
        # Official internal feature 261 maps to wire 279; Hts supplies a single video part.
        for orientation, format_value in (("landscape", 5), ("portrait", 6)):
            with self.subTest(orientation=orientation):
                expected = expected_wire(
                    279,
                    [None, None, None, [[source_node()]]],
                    [0, 12, None, 0, format_value, None, None, None, 3],
                )
                actual = self.request("upscale", seconds=4, orientation=orientation)
                self.assertEqual(actual, expected)

    def test_long_extension_source_uses_last_ten_seconds(self):
        expected = self.extension_wire([0, 12, None, 0, 1, None, ["2"], ["12"], 4])
        self.assertEqual(self.request("extend", duration=12, seconds=4), expected)

    def test_fractional_long_extension_range_preserves_official_nanoseconds(self):
        # Official fus/kus preserves milliseconds in the selected source range.
        expected = self.extension_wire(
            [0, 12, None, 0, 1, None, ["2", 500000000], ["12", 500000000], 3]
        )
        self.assertEqual(self.request("extend", duration=12.5), expected)

    def test_uploaded_video_extend_and_upscale_use_temporary_video_resource(self):
        video = {"temporary_blob": "uploaded-video", "duration": 4.5}
        for mode, feature in (("extend", 378), ("upscale", 279)):
            with self.subTest(mode=mode):
                request = build_video_request(
                    "doc", FLAGS, "Continue", video, mode=mode, orientation="portrait"
                )
                self.assertEqual(request[0], feature)
                node = request[3][3][0][0] if mode == "upscale" else request[2][25][0][0][7][1]
                self.assertEqual(node[25][5], [None, None, None, "uploaded-video", 1])
                self.assertEqual(request[4][15][4], 6 if mode == "upscale" else 2)
                self.assertEqual(
                    request[4][15][8], round(video["duration"]) if mode == "upscale" else 3
                )


if __name__ == "__main__":
    unittest.main()
