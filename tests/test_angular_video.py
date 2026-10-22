import unittest

from src.services.flow.angular_video import (
    VIDEO_POLL_RPC,
    VIDEO_RPCS,
    VIDEO_TEXT_RPC,
    build_video_poll_request,
    build_video_request,
    build_video_text_request,
    parse_media_response,
    parse_video_response,
    resolve_video_usage,
)


class AngularVideoTests(unittest.TestCase):
    def models(self):
        def usage(key, capabilities, aspect, price=True, duration=8):
            row = [None] * 24
            row[0] = key
            row[4] = [[2, [[None, 20]] if price else [None, []]]]
            row[7] = [[[capabilities]]]
            row[12] = [aspect]
            row[16] = duration
            row[23] = [[1]]
            return row

        return [
            [
                None,
                None,
                None,
                None,
                [
                    [
                        "Veo 3.1 - Fast",
                        [
                            usage("veo_3_1_t2v_fast", [1], [2]),
                            usage("veo_3_1_t2v_fast_ultra", [1], [2], price=False),
                            usage("veo_3_1_t2v_fast_portrait", [1], [1]),
                            usage("veo_3_1_i2v_s_fast", [1, 5], [2]),
                            usage("veo_3_1_r2v_fast_landscape", [1, 6], [2]),
                        ],
                        True,
                        "veo_3_1_fast",
                    ]
                ],
            ]
        ]

    def test_usage_resolver_accepts_exact_official_key_without_substitution(self):
        self.assertEqual(
            resolve_video_usage(self.models(), "veo_3_1_t2v_fast", "16:9", 2), "veo_3_1_t2v_fast"
        )
        self.assertEqual(
            resolve_video_usage(self.models(), "veo_3_1_i2v_s_fast", "16:9", 2, mode="start"),
            "veo_3_1_i2v_s_fast",
        )
        for key, aspect, tier, options in (
            ("veo_3_1_t2v_fast", "9:16", 2, {}),
            ("veo_3_1_t2v_fast", "16:9", 3, {}),
            ("veo_3_1_t2v_fast_ultra", "16:9", 2, {}),
            ("veo_3_1_t2v_fast", "16:9", 2, {"duration": 4}),
            ("veo_3_1_t2v_fast", "16:9", 2, {"resolution": 3}),
            ("veo_3_1_t2v_fast", "16:9", 2, {"mode": "references"}),
            ("other", "16:9", 2, {}),
        ):
            with self.subTest(key=key, options=options), self.assertRaises(ValueError):
                resolve_video_usage(self.models(), key, aspect, tier, **options)

    def test_all_video_mode_payloads_have_verified_slots(self):
        kwargs = dict(
            project_id="p",
            prompt="x",
            model_name="model",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            captcha_token="c",
            batch_id="b",
            media_id="m",
            workflow_id="w",
        )
        first = build_video_request(mode="start", start_media_id="first", **kwargs)
        self.assertEqual(first[0][0][4], [None, "first"])
        self.assertEqual(first[0][0][5][4:6], ["m", "w"])
        frames = build_video_request(
            mode="start_end", start_media_id="first", end_media_id="last", **kwargs
        )
        self.assertEqual(frames[0][0][4:6], [[None, "first"], [None, "last"]])
        refs = build_video_request(
            mode="references", reference_images=[{"mediaId": "ref"}], **kwargs
        )
        self.assertEqual(refs[0][0][1], [[None, "ref"]])
        for mode, metadata_slot in (("extend", 5), ("edit", 4)):
            item = build_video_request(
                mode=mode, video_media_id="source", trim_start_frame=0, trim_end_frame=192, **kwargs
            )[0][0]
            self.assertEqual(item[0], [None, "source", 0, 192])
            self.assertEqual(item[metadata_slot][4:6], ["m", "w"])
        upsample = build_video_request(
            mode="upsample", video_media_id="source", resolution=2, **kwargs
        )[0][0]
        self.assertEqual(upsample[0], [None, "source"])
        self.assertEqual(upsample[6], 2)
        self.assertEqual(upsample[31], "model")
        self.assertEqual(
            set(VIDEO_RPCS),
            {"text", "start", "start_end", "references", "extend", "upsample", "edit"},
        )

    def test_text_request_uses_current_rpc_shape_and_no_rest_fields(self):
        payload = build_video_text_request(
            project_id="projects/demo",
            prompt="a synthetic sunset",
            model_name="veo_3_1_t2v_fast_4s",
            aspect_ratio="16:9",
            captcha_token="synthetic-captcha",
            batch_id="batch-1",
            media_id="media-seed",
            workflow_id="workflow-seed",
        )
        self.assertEqual(VIDEO_TEXT_RPC, "YhhmEf")
        self.assertEqual(payload[0][0][0][2], [[["a synthetic sunset"]]])
        self.assertEqual(payload[0][0][1], "veo_3_1_t2v_fast_4s")
        self.assertEqual(payload[0][0][2], 2)
        self.assertEqual(payload[0][0][4][4:6], ["media-seed", "workflow-seed"])
        self.assertEqual(
            payload[1],
            [
                None,
                22,
                None,
                None,
                None,
                "projects/demo",
                None,
                None,
                None,
                None,
                ["synthetic-captcha", 1],
            ],
        )
        self.assertEqual(payload[2], ["batch-1"])

    def test_text_request_generates_unique_seeds_and_optional_resolution(self):
        first = build_video_text_request(
            project_id="p",
            prompt="x",
            model_name="veo_3_1_t2v_fast_4s",
            aspect_ratio="PORTRAIT",
            captcha_token="c",
            batch_id="b",
            resolution=4,
            audio_failure_preference=1,
        )
        second = build_video_text_request(
            project_id="p",
            prompt="x",
            model_name="veo_3_1_t2v_fast_4s",
            aspect_ratio="PORTRAIT",
            captcha_token="c",
            batch_id="b",
            resolution=4,
            audio_failure_preference=1,
        )
        self.assertNotEqual(first[0][0][4][4:6], second[0][0][4][4:6])
        self.assertEqual(first[0][0][2], 1)
        self.assertEqual(first[0][0][7], [4])
        self.assertEqual(first[2], ["b", 1])

    def test_poll_request_accepts_legacy_operations_deduplicates_names(self):
        payload = build_video_poll_request(
            [
                {"operation": {"name": "media-a"}},
                {"mediaName": "media-a"},
                "media-b",
            ]
        )
        self.assertEqual(VIDEO_POLL_RPC, "jwpduf")
        self.assertEqual(payload, [None, None, [["media-a"], ["media-b"]]])

    def test_poll_request_rejects_empty_or_missing_media(self):
        for value in ([], [{}], [None], ["  "]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_video_poll_request(value)

    def test_parse_submit_response_builds_legacy_operations_and_sanitizes_errors(self):
        media = [None] * 8
        media[0] = "media-a"
        media[2] = "workflow-a"
        media[5] = [None] * 9
        media[5][8] = [3]
        media[7] = [None] * 9
        media[7][0] = [None] * 9
        media[7][0][8] = "https://media.example/video.mp4"
        parsed = parse_video_response([None, 95, None, [media]], "projects/demo")
        operation = parsed["operations"][0]
        self.assertEqual(operation["operation"]["name"], "media-a")
        self.assertEqual(operation["mediaName"], "media-a")
        self.assertEqual(operation["projectId"], "projects/demo")
        self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_SUCCESSFUL")
        self.assertEqual(parsed["remainingCredits"], 95)
        self.assertEqual(
            parsed["media"][0]["video"]["generatedVideo"]["fifeUrl"],
            "https://media.example/video.mp4",
        )

    def test_parse_poll_response_maps_status_and_uses_media_names(self):
        media = [None] * 6
        media[0] = "media-pending"
        media[5] = [None] * 9
        media[5][8] = [2]
        parsed = parse_video_response([None, 90, [media]], "p", poll=True)
        self.assertEqual(parsed["operations"][0]["operation"]["name"], "media-pending")
        self.assertEqual(parsed["operations"][0]["status"], "MEDIA_GENERATION_STATUS_PENDING")

    def test_parse_failure_does_not_copy_upstream_error_text(self):
        media = [None] * 6
        media[0] = "media-failed"
        media[5] = [None] * 9
        media[5][8] = [4, "private upstream error"]
        parsed = parse_video_response([None, 0, [media]], "p", poll=True)
        operation = parsed["operations"][0]
        self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_FAILED")
        self.assertNotIn("private upstream error", repr(parsed))

    def test_cancel_and_empty_poll_response_terminate_instead_of_staying_pending(self):
        media = ["media-canceled", None, None, None, None, [None] * 8 + [[5]]]
        parsed = parse_video_response([None, 0, [media]], "p", poll=True)
        self.assertEqual(parsed["operations"][0]["status"], "MEDIA_GENERATION_STATUS_FAILED")
        self.assertEqual(parsed["operations"][0]["operation"]["error"]["code"], 1)
        for payload in ([], [None, 0, []], [None, 0, [None]]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_video_response(payload, "p", poll=True)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            build_video_text_request(
                project_id="", prompt="x", model_name="m", aspect_ratio="16:9", captcha_token="c"
            )
        with self.assertRaises(ValueError):
            build_video_text_request(
                project_id="p", prompt="x", model_name="m", aspect_ratio="bad", captcha_token="c"
            )
        with self.assertRaises(ValueError):
            build_video_text_request(
                project_id="p", prompt="x", model_name="m", aspect_ratio="16:9", captcha_token=""
            )

    def test_edit_extend_reference_images_use_field9_with_optional_crop(self):
        kwargs = dict(
            project_id="p",
            prompt="edit",
            model_name="m",
            aspect_ratio="16:9",
            captcha_token="c",
            video_media_id="source",
            batch_id="b",
            media_id="m",
            workflow_id="w",
        )
        references = [
            {"mediaId": "plain-reference"},
            {
                "mediaId": "cropped-reference",
                "cropCoordinates": {"top": 0.1, "left": 0.2, "bottom": 0.9, "right": 0.8},
            },
        ]
        for mode, metadata in (("edit", 4), ("extend", 5)):
            with self.subTest(mode=mode):
                item = build_video_request(
                    mode=mode, reference_images=references, resolution=2, **kwargs
                )[0][0]
                self.assertEqual(
                    item[8],
                    [
                        [None, "plain-reference"],
                        [None, "cropped-reference", None, None, None, [0.1, 0.2, 0.9, 0.8]],
                    ],
                )
                self.assertEqual(item[metadata][4:6], ["m", "w"])
                self.assertEqual(item[12], [2])
        for refs in (
            "invalid",
            [None],
            [{"mediaId": "ref", "cropCoordinates": {"top": 1, "left": 0, "bottom": 0, "right": 1}}],
        ):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                build_video_request(mode="edit", reference_images=refs, **kwargs)

    def test_get_media_direct_generated_video_preserves_dimensions_duration_and_source_fps(self):
        generated = [None] * 17
        generated[8] = "https://media.example/video.mp4"
        generated[12] = "veo_3_1_t2v_fast"
        generated[16] = 2
        video = [generated, [1280, 720, ["8", 500000000]]]
        raw = ["actual-video", None, "actual-workflow", None, None, [None] * 8 + [[3]], None, video]
        parsed = parse_media_response(raw, "project")
        self.assertEqual(parsed["name"], "actual-video")
        self.assertEqual(parsed["workflowId"], "actual-workflow")
        self.assertEqual(
            parsed["video"]["dimensions"],
            {"width": 1280, "height": 720, "length": "8.5s", "frameRate": 24},
        )
        self.assertEqual(parsed["video"]["frameRate"], 24)
        self.assertEqual(
            parsed["video"]["generatedVideo"]["fifeUrl"], "https://media.example/video.mp4"
        )
        self.assertNotIn("operations", parsed)

    def test_get_media_direct_uploaded_video_uses_tva_field5_and_fps30(self):
        video = [
            None,
            [1920, 1080, ["10"]],
            None,
            None,
            [None, None, "https://media.example/upload.mp4"],
        ]
        raw = ["uploaded-video", None, "workflow", None, None, None, None, video]
        parsed = parse_media_response(raw, "project")
        self.assertEqual(
            parsed["video"]["userUploadedVideo"]["fifeUrl"], "https://media.example/upload.mp4"
        )
        self.assertEqual(parsed["video"]["frameRate"], 30)
        self.assertEqual(parsed["video"]["dimensions"]["length"], "10s")
        self.assertEqual(parsed["video"]["dimensions"]["frameRate"], 30)

    def test_get_media_direct_image_parses_generated_and_uploaded_oneofs(self):
        generated = [None] * 13 + ["https://media.example/generated.png"]
        uploaded = [None] * 3 + ["https://media.example/uploaded.png"]
        for body, key, url in (
            (
                [generated, None, [1024, 1024]],
                "generatedImage",
                "https://media.example/generated.png",
            ),
            (
                [None, uploaded, [800, 600]],
                "userUploadedImage",
                "https://media.example/uploaded.png",
            ),
        ):
            with self.subTest(key=key):
                raw = ["image", None, "workflow", None, None, None, body]
                parsed = parse_media_response(raw, "project")
                self.assertEqual(parsed["image"][key]["fifeUrl"], url)
                self.assertIn("width", parsed["image"]["dimensions"])
                self.assertNotIn("video", parsed)

    def test_get_media_parser_does_not_invent_fps_or_accept_wrapped_non_bz(self):
        for payload in ({}, [], [["nested-bz"]], ["media-no-type"], [None]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_media_response(payload, "project")
        raw = ["media", None, None, None, None, None, None, [None, [640, 360, ["3"]]]]
        with self.assertRaises(ValueError):
            parse_media_response(raw, "project")


if __name__ == "__main__":
    unittest.main()
