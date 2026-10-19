import unittest
from io import BytesIO

from PIL import Image

from src.core.account_tiers import (
    PAYGATE_TIER_ONE,
    PAYGATE_TIER_TWO,
    get_required_paygate_tier_for_model,
    supports_model_for_tier,
)
from src.core.models import ChatCompletionRequest
from src.core.public_models import PUBLIC_MODELS, resolve_public_model
from src.services.generation_handler import MODEL_CONFIG


class PublicModelTests(unittest.TestCase):
    def resolve_video(self, model="veo_3_1", options=None, **inputs):
        return resolve_public_model(
            model, {"generationConfig": {"videoConfig": options or {}}}, MODEL_CONFIG, **inputs
        )

    def test_registry_retains_compatibility_aliases(self):
        self.assertEqual(
            set(PUBLIC_MODELS),
            {
                "gemini-3.1-flash-image",
                "gemini-3.1-flash-image-lite",
                "gemini-3.0-pro-image",
                "imagen-4.0-generate-preview",
                "veo_3_1",
                "veo_3_1_interpolation_lite",
                "veo_3_1_remix",
                "veo_3_1_extend",
                "omni_1.1_flash",
            },
        )
        self.assertIsNone(resolve_public_model("omni_edit_360p", {}, MODEL_CONFIG))
        self.assertIsNone(resolve_public_model("veo_3_1_t2v_fast_4s", {}, MODEL_CONFIG))

    def test_veo_aggregate_defaults_to_eight_seconds_without_legacy_public_modes(self):
        veo = PUBLIC_MODELS["veo_3_1"]
        self.assertEqual(veo["defaults"]["durationSeconds"], 8)
        self.assertNotIn("remix", veo["capabilities"]["modes"])
        self.assertEqual(
            {model_id for model_id, spec in PUBLIC_MODELS.items() if not spec.get("legacy")}
            & {"veo_3_1_interpolation_lite", "veo_3_1_remix", "veo_3_1_extend"},
            set(),
        )

    def test_interpolation_and_remix_models_resolve_to_existing_flows(self):
        interpolation = resolve_public_model(
            "veo_3_1_interpolation_lite",
            {"generationConfig": {"videoConfig": {"durationSeconds": 8, "aspectRatio": "9:16"}}},
            MODEL_CONFIG,
            images=[b"first", b"last"],
        )
        self.assertEqual(interpolation.internal_model, "veo_3_1_interpolation_lite_portrait_8s")
        self.assertEqual(interpolation.options["mode"], "first_last_frame")

        remix = resolve_public_model(
            "veo_3_1_remix",
            {"generationConfig": {"videoConfig": {}}},
            MODEL_CONFIG,
            video_media_id="source-video",
        )
        self.assertEqual(remix.internal_model, "veo_3_1_extend")
        self.assertEqual(remix.options["mode"], "extend")
        self.assertEqual(remix.options["quality"], "quality")
        for model in ("veo_3_1", "veo_3_1_remix"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "Unsupported mode"):
                self.resolve_video(model, {"mode": "remix"}, video_media_id="source-video")

    def test_every_advertised_combination_resolves_to_an_existing_config(self):
        checked = 0
        for model, entry in PUBLIC_MODELS.items():
            caps = entry["capabilities"]
            if entry["type"] == "image":
                for aspect in caps["aspectRatios"]:
                    for size in caps["imageSizes"]:
                        with self.subTest(model=model, aspect=aspect, size=size):
                            result = resolve_public_model(
                                model,
                                {
                                    "generationConfig": {
                                        "imageConfig": {"aspectRatio": aspect, "imageSize": size}
                                    }
                                },
                                MODEL_CONFIG,
                            )
                            self.assertEqual(MODEL_CONFIG[result.internal_model]["type"], "image")
                            checked += 1
            else:
                for combination in caps["combinations"]:
                    mode = combination["mode"]
                    for duration in combination["durations"] or [None]:
                        for resolution in combination["resolutions"]:
                            for aspect in caps["aspectRatios"]:
                                options = {
                                    "mode": mode,
                                    "resolution": resolution,
                                    "aspectRatio": aspect,
                                }
                                if duration is not None:
                                    options["durationSeconds"] = duration
                                if combination["quality"]:
                                    options["quality"] = combination["quality"]
                                if combination.get("priority"):
                                    options["priority"] = combination["priority"]
                                with self.subTest(model=model, options=options):
                                    result = self.resolve_video(
                                        model,
                                        options,
                                        images=[b"image"] * caps["modes"][mode]["minImages"],
                                        has_video=mode == "edit",
                                        video_media_id="source"
                                        if mode in ("extend", "remix")
                                        else None,
                                    )
                                    self.assertEqual(
                                        MODEL_CONFIG[result.internal_model]["type"], "video"
                                    )
                                    if combination.get("priority"):
                                        self.assertEqual(
                                            result.options["priority"], combination["priority"]
                                        )
                                    checked += 1
        self.assertGreater(checked, 200)

    def test_explicit_parameters_select_correct_upstream_operations(self):
        result = self.resolve_video(
            options={
                "mode": "image_to_video",
                "quality": "quality",
                "durationSeconds": 8,
                "aspectRatio": "9:16",
                "resolution": "1080p",
            },
            images=[b"frame"],
        )
        cfg = MODEL_CONFIG[result.internal_model]
        self.assertEqual(cfg["model_key"], "veo_3_1_i2v_s_portrait_fl")
        self.assertEqual(cfg["aspect_ratio"], "VIDEO_ASPECT_RATIO_PORTRAIT")
        self.assertEqual(cfg["upsample"]["model_key"], "veo_3_1_upsampler_1080p")
        result = self.resolve_video(
            "omni_1.1_flash",
            {"mode": "first_last_frame", "durationSeconds": 10, "resolution": "360p"},
            images=[b"first", b"last"],
        )
        self.assertEqual(
            MODEL_CONFIG[result.internal_model]["model_key"], "omni_flash_i2v_10s_first_last_360p"
        )

    def test_fast_eight_second_upscaling_uses_existing_ultra_inputs(self):
        for mode, count in (
            ("image_to_video", 1),
            ("first_last_frame", 2),
            ("reference_to_video", 3),
        ):
            for aspect, orientation in (("16:9", ""), ("9:16", "_portrait")):
                for resolution in ("1080p", "4k"):
                    with self.subTest(mode=mode, aspect=aspect, resolution=resolution):
                        result = self.resolve_video(
                            options={
                                "mode": mode,
                                "quality": "fast",
                                "durationSeconds": 8,
                                "aspectRatio": aspect,
                                "resolution": resolution,
                            },
                            images=[b"image"] * count,
                        )
                        cfg = MODEL_CONFIG[result.internal_model]
                        expected = (
                            f"veo_3_1_r2v_fast_{'portrait' if orientation else 'landscape'}_ultra"
                            if mode == "reference_to_video"
                            else f"veo_3_1_i2v_s_fast{orientation}_ultra_fl"
                        )
                        self.assertEqual(cfg["model_key"], expected)
                        self.assertEqual(
                            cfg["upsample"]["model_key"], f"veo_3_1_upsampler_{resolution}"
                        )
                        self.assertEqual(
                            get_required_paygate_tier_for_model(result.internal_model),
                            PAYGATE_TIER_TWO,
                        )
                        self.assertFalse(
                            supports_model_for_tier(result.internal_model, PAYGATE_TIER_ONE)
                        )
                        self.assertTrue(
                            supports_model_for_tier(result.internal_model, PAYGATE_TIER_TWO)
                        )
                        self.assertEqual(result.options["quality"], "fast")
                for duration in (4, 6):
                    with self.subTest(mode=mode, duration=duration), self.assertRaises(ValueError):
                        self.resolve_video(
                            options={
                                "mode": mode,
                                "quality": "fast",
                                "durationSeconds": duration,
                                "resolution": "1080p",
                            },
                            images=[b"image"] * count,
                        )

    def test_omni_output_resolutions_keep_native_keys_and_route_paid_upscalers(self):
        for mode, key_template, count in (
            ("text_to_video", "abra_t2v_{seconds}s", 0),
            ("image_to_video", "abra_i2v_{seconds}s", 1),
            ("first_last_frame", "omni_flash_i2v_{seconds}s_first_last", 2),
            ("reference_to_video", "abra_r2v_{seconds}s", 7),
            ("edit", "abra_edit", 5),
        ):
            for seconds in (None,) if mode == "edit" else (4, 6, 8, 10):
                for aspect in ("16:9", "9:16"):
                    for resolution in ("720p", "360p", "1080p", "4k"):
                        with self.subTest(
                            mode=mode, seconds=seconds, aspect=aspect, resolution=resolution
                        ):
                            options = {
                                "mode": mode,
                                "aspectRatio": aspect,
                                "resolution": resolution,
                            }
                            if seconds is not None:
                                options["durationSeconds"] = seconds
                            result = self.resolve_video(
                                "omni_1.1_flash",
                                options,
                                images=[b"image"] * count,
                                has_video=mode == "edit",
                            )
                            cfg = MODEL_CONFIG[result.internal_model]
                            expected_key = key_template.format(seconds=seconds)
                            self.assertEqual(
                                cfg["model_key"],
                                expected_key + ("_360p" if resolution == "360p" else ""),
                            )
                            self.assertFalse(cfg["allow_tier_upgrade"])
                            if resolution in ("1080p", "4k"):
                                self.assertEqual(
                                    cfg["upsample"],
                                    {
                                        "resolution": f"VIDEO_RESOLUTION_{resolution.upper()}",
                                        "model_key": f"veo_3_1_upsampler_{resolution}",
                                    },
                                )
                                required = (
                                    PAYGATE_TIER_ONE if resolution == "1080p" else PAYGATE_TIER_TWO
                                )
                                self.assertEqual(
                                    get_required_paygate_tier_for_model(result.internal_model),
                                    required,
                                )
                                self.assertEqual(
                                    supports_model_for_tier(
                                        result.internal_model, PAYGATE_TIER_ONE
                                    ),
                                    resolution == "1080p",
                                )
                                self.assertTrue(
                                    supports_model_for_tier(result.internal_model, PAYGATE_TIER_TWO)
                                )
                            else:
                                self.assertNotIn("upsample", cfg)

    def test_auto_uses_reference_images_without_guessing_frame_roles(self):
        for model in ("veo_3_1", "omni_1.1_flash"):
            self.assertEqual(self.resolve_video(model).options["mode"], "text_to_video")
            for count in (1, 2, 3):
                with self.subTest(model=model, count=count):
                    result = self.resolve_video(model, images=[b"reference"] * count)
                    self.assertEqual(result.options["mode"], "reference_to_video")
                    self.assertEqual(MODEL_CONFIG[result.internal_model]["video_type"], "r2v")
        self.assertEqual(
            self.resolve_video("omni_1.1_flash", has_video=True).options["mode"], "edit"
        )
        with self.assertRaises(ValueError):
            self.resolve_video(has_video=True)

    def test_image_models_accept_ten_references_but_reject_an_eleventh(self):
        image_models = [name for name, entry in PUBLIC_MODELS.items() if entry["type"] == "image"]
        for model in image_models:
            with self.subTest(model=model):
                result = resolve_public_model(model, {}, MODEL_CONFIG, images=[b"image"] * 10)
                self.assertEqual(MODEL_CONFIG[result.internal_model]["type"], "image")
                with self.assertRaises(ValueError):
                    resolve_public_model(model, {}, MODEL_CONFIG, images=[b"image"] * 11)

    def test_veo_reference_limit_stays_three_and_omni_reference_limit_is_seven(self):
        omni = self.resolve_video(
            "omni_1.1_flash",
            {"mode": "reference_to_video", "durationSeconds": 8, "resolution": "720p"},
            images=[b"image"] * 7,
        )
        self.assertEqual(omni.internal_model, "omni_r2v_8s")
        with self.assertRaises(ValueError):
            self.resolve_video(
                "omni_1.1_flash",
                {"mode": "reference_to_video", "durationSeconds": 8, "resolution": "720p"},
                images=[b"image"] * 8,
            )
        with self.assertRaises(ValueError):
            self.resolve_video(
                "veo_3_1",
                {"mode": "reference_to_video", "durationSeconds": 8, "quality": "lite"},
                images=[b"image"] * 4,
            )

    def test_omni_edit_requires_one_video_and_allows_up_to_five_image_references(self):
        result = self.resolve_video(
            "omni_1.1_flash",
            {"mode": "edit", "resolution": "720p"},
            images=[b"image"] * 5,
            has_video=True,
        )
        self.assertEqual(result.internal_model, "omni_edit")
        with self.assertRaises(ValueError):
            self.resolve_video(
                "omni_1.1_flash",
                {"mode": "edit", "resolution": "720p"},
                images=[b"image"] * 6,
                has_video=True,
            )
        with self.assertRaises(ValueError):
            self.resolve_video(
                "omni_1.1_flash",
                {"mode": "edit", "resolution": "720p"},
                images=[b"image"],
                has_video=True,
                video_media_id="second-video",
            )

    def test_frame_synonyms_and_snake_case_parameters(self):
        result = resolve_public_model(
            "omni_1.1_flash",
            {
                "extra_body": {
                    "video_config": {
                        "mode": "interpolation",
                        "duration_seconds": 4,
                        "aspect_ratio": "portrait",
                    }
                }
            },
            MODEL_CONFIG,
            images=[b"first", b"last"],
        )
        self.assertEqual(result.internal_model, "omni_i2v_portrait_4s_first_last")
        result = self.resolve_video("omni_1.1_flash", {"mode": "first_frame"}, images=[b"first"])
        self.assertEqual(result.options["mode"], "image_to_video")
        for ratio in ("16x9", "16X9"):
            with self.subTest(ratio=ratio):
                result = self.resolve_video("omni_1.1_flash", {"aspectRatio": ratio})
                self.assertEqual(result.options["aspectRatio"], "16:9")

    def test_duplicate_config_locations_must_agree(self):
        request = {
            "generationConfig": {"videoConfig": {"aspectRatio": "9:16"}},
            "video_config": {"aspect_ratio": "portrait"},
            "extra_body": {"generationConfig": {"videoConfig": {"durationSeconds": 4}}},
        }
        result = resolve_public_model("omni_1.1_flash", request, MODEL_CONFIG)
        self.assertEqual(result.internal_model, "omni_portrait_4s")
        request["extra_body"]["videoConfig"] = {"durationSeconds": 6}
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            resolve_public_model("omni_1.1_flash", request, MODEL_CONFIG)

    def test_video_invalid_values_fail_before_generation(self):
        invalid = [
            {"durationSeconds": True},
            {"durationSeconds": "4"},
            {"durationSeconds": 4.0},
            {"durationSeconds": 10},
            {"aspectRatio": "1:1"},
            {"resolution": "360p"},
            {"quality": "ultra"},
            {"duration": 4},
            {"mode": "edit"},
            {"mode": "text_to_video", "quality": "fast", "durationSeconds": 4, "resolution": "4k"},
            {"trimStartMs": 0},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.resolve_video(options=options)
        with self.assertRaises(ValueError):
            self.resolve_video(
                options={"mode": "reference_to_video", "quality": "quality"}, images=[b"ref"]
            )
        with self.assertRaises(ValueError):
            self.resolve_video("omni_1.1_flash", {"quality": "fast"})

    def test_input_counts_and_modes_must_agree(self):
        for mode, count in (
            ("text_to_video", 1),
            ("image_to_video", 2),
            ("first_last_frame", 1),
            ("reference_to_video", 0),
            ("reference_to_video", 8),
        ):
            with self.subTest(mode=mode, count=count), self.assertRaises(ValueError):
                self.resolve_video("omni_1.1_flash", {"mode": mode}, images=[b"x"] * count)
        with self.assertRaises(ValueError):
            self.resolve_video("omni_1.1_flash", {"mode": "text_to_video"}, has_video=True)
        with self.assertRaises(ValueError):
            self.resolve_video("omni_1.1_flash", has_video=True, video_media_id="duplicate")

    def test_edit_trim_and_duration_validation(self):
        result = self.resolve_video("omni_1.1_flash", has_video=True)
        self.assertEqual((result.trim_start_ms, result.trim_end_ms), (0, 10000))
        self.assertNotIn("durationSeconds", result.options)
        result = self.resolve_video(
            "omni_1.1_flash", {"trimStartMs": 12000, "trimEndMs": 16000}, has_video=True
        )
        self.assertEqual((result.trim_start_ms, result.trim_end_ms), (12000, 16000))
        invalid = [
            {"durationSeconds": 4},
            {"trimStartMs": -1},
            {"trimEndMs": 0},
            {"trimEndMs": 10001},
            {"trimStartMs": True},
            {"trimEndMs": "4000"},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.resolve_video("omni_1.1_flash", options, has_video=True)
        result = self.resolve_video("omni_1.1_flash", video_media_id="source")
        self.assertIsNone(result.trim_start_ms)
        self.assertNotIn("trimStartMs", result.options)
        with self.assertRaises(ValueError):
            self.resolve_video("omni_1.1_flash", {"trimEndMs": 4000}, video_media_id="source")

    def test_extend_requires_an_explicit_mode_and_media_id(self):
        result = self.resolve_video(options={"mode": "extend"}, video_media_id="source")
        self.assertEqual(result.internal_model, "veo_3_1_extend")
        self.assertEqual(result.options["quality"], "quality")
        self.assertNotIn("trimStartMs", result.options)
        for options, inputs in (
            ({}, {"video_media_id": "source"}),
            ({"mode": "extend"}, {"has_video": True}),
            ({"mode": "extend", "trimEndMs": 4000}, {"video_media_id": "source"}),
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.resolve_video(options=options, **inputs)

    def test_extend_quality_variants_use_the_captured_flow_keys(self):
        expected_keys = {
            "quality": ("veo_3_1_extend", "veo_3_1_extend_landscape"),
            "fast": ("veo_3_1_extend_fast_landscape", "veo_3_1_extend_fast_landscape"),
            "lite": ("veo_3_1_extension_lite_landscape", "veo_3_1_extension_lite"),
        }
        for quality, (internal, upstream) in expected_keys.items():
            with self.subTest(quality=quality):
                result = self.resolve_video(
                    options={"mode": "extend", "quality": quality}, video_media_id="source"
                )
                self.assertEqual(result.internal_model, internal)
                self.assertEqual(MODEL_CONFIG[internal]["video_type"], "extend")
                self.assertEqual(MODEL_CONFIG[internal]["model_key"], upstream)
        for model in ("veo_3_1_remix", "veo_3_1_extend"):
            for quality in ("fast", "lite"):
                with self.subTest(model=model, quality=quality):
                    result = self.resolve_video(
                        model, {"mode": "extend", "quality": quality}, video_media_id="source"
                    )
                    self.assertEqual(MODEL_CONFIG[result.internal_model]["video_type"], "extend")

    def test_images_support_old_openai_size_and_quality(self):
        request = ChatCompletionRequest(
            model="gemini-3.1-flash-image", size="1024x1792", quality="high"
        )
        result = resolve_public_model(request.model, request, MODEL_CONFIG)
        self.assertEqual(result.internal_model, "gemini-3.1-flash-image-portrait-4k")
        result = resolve_public_model(
            "gemini-3.0-pro-image",
            {
                "extra_body": {
                    "generationConfig": {"imageConfig": {"aspectRatio": "4:3", "imageSize": "2K"}}
                }
            },
            MODEL_CONFIG,
        )
        self.assertEqual(result.internal_model, "gemini-3.0-pro-image-four-three-2k")
        for quality, expected in (
            ("low", "1k"),
            ("standard", "1k"),
            ("medium", "2k"),
            ("high", "4k"),
            ("hd", "4k"),
            ("ultra", "4k"),
        ):
            with self.subTest(quality=quality):
                result = resolve_public_model(
                    "gemini-3.1-flash-image", {"quality": quality}, MODEL_CONFIG
                )
                self.assertEqual(result.options["imageSize"], expected)

    def test_invalid_openai_image_fields_do_not_silently_fall_back(self):
        for field, value in (
            ("size", "bad"),
            ("size", "2k"),
            ("size", "00x1024"),
            ("size", 1024),
            ("quality", "best"),
            ("quality", True),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                resolve_public_model("gemini-3.1-flash-image", {field: value}, MODEL_CONFIG)

    def test_canonical_video_rejects_misplaced_parameters(self):
        for request in (
            {"generationConfig": {"imageSize": "360p"}},
            {"generationConfig": {"aspectRatio": "9:16"}},
            {"extra_body": {"durationSeconds": 4}},
            {"quality": "fast"},
        ):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, "videoConfig"):
                resolve_public_model("omni_1.1_flash", request, MODEL_CONFIG)
        self.assertIsNone(
            resolve_public_model("omni", {"generationConfig": {"imageSize": "360p"}}, MODEL_CONFIG)
        )

    def test_image_ratio_follows_reference_only_when_unspecified(self):
        output = BytesIO()
        Image.new("RGB", (36, 64)).save(output, format="PNG")
        image = output.getvalue()
        result = resolve_public_model("gemini-3.1-flash-image", {}, MODEL_CONFIG, images=[image])
        self.assertEqual(result.options["aspectRatio"], "9:16")
        result = resolve_public_model(
            "gemini-3.1-flash-image",
            {"imageConfig": {"aspectRatio": "16:9"}},
            MODEL_CONFIG,
            images=[image],
        )
        self.assertEqual(result.options["aspectRatio"], "16:9")

    def test_images_reject_unsupported_parameters_and_conflicts(self):
        for model, request, inputs in (
            ("imagen-4.0-generate-preview", {"imageConfig": {"aspectRatio": "1:1"}}, {}),
            ("gemini-3.1-flash-image-lite", {"imageConfig": {"imageSize": "4k"}}, {}),
            ("gemini-3.1-flash-image", {"imageConfig": {"aspectRatio": "2:1"}}, {}),
            ("gemini-3.1-flash-image", {"imageConfig": {"imagSize": "2k"}}, {}),
            ("gemini-3.1-flash-image", {}, {"images": [b"x"] * 11}),
            ("gemini-3.1-flash-image", {}, {"has_video": True}),
            (
                "gemini-3.1-flash-image",
                {"imageConfig": {"aspectRatio": "16:9"}, "size": "1024x1792"},
                {},
            ),
            ("gemini-3.1-flash-image", {"videoConfig": {}}, {}),
        ):
            with self.subTest(model=model, request=request), self.assertRaises(ValueError):
                resolve_public_model(model, request, MODEL_CONFIG, **inputs)


if __name__ == "__main__":
    unittest.main()
