import unittest

from src.core.media_skus import MEDIA_SKUS, get_sku_entry, list_sku_models, prepare_sku_request
from src.core.model_catalog import MODEL_CONFIG
from src.core.public_models import PUBLIC_MODELS, resolve_public_model


class MediaSkuTests(unittest.TestCase):
    def test_catalog_has_only_capability_backed_image_skus(self):
        self.assertEqual(
            {name for name in MEDIA_SKUS if name.startswith("flow-banana-pro-")},
            {"flow-banana-pro-1k", "flow-banana-pro-2k", "flow-banana-pro-4k"},
        )
        self.assertIn("flow-banana-lite-1k", MEDIA_SKUS)
        self.assertIn("flow-imagen-4-1k", MEDIA_SKUS)

    def test_video_catalog_groups_generation_modes_and_separates_edit_skus(self):
        self.assertIn("flow-veo-3-1-fast-8s-720p", MEDIA_SKUS)
        self.assertIn("flow-omni-1-1-flash-10s-4k", MEDIA_SKUS)
        self.assertFalse(any("extend" in name for name in MEDIA_SKUS))
        self.assertEqual(
            {name for name in MEDIA_SKUS if "-edit-" in name},
            {
                f"flow-omni-1-1-flash-edit-{resolution}"
                for resolution in ("360p", "720p", "1080p", "4k")
            },
        )
        self.assertEqual(len(list_sku_models()), len(MEDIA_SKUS))

    def test_standard_suffix_is_hidden_and_relaxed_names_remain_distinct(self):
        names = {model["id"] for model in list_sku_models()}
        self.assertEqual(names, set(MEDIA_SKUS))
        self.assertEqual(len(names), 37)
        self.assertFalse(any(name.endswith("-standard") for name in names))
        relaxed = {
            name
            for name, entry in MEDIA_SKUS.items()
            if entry["locked_spec"].get("priority") == "relaxed"
        }
        self.assertTrue(relaxed)
        self.assertTrue(all(name.endswith("-relaxed") for name in relaxed))
        self.assertIn("flow-veo-3-1-fast-8s-720p-relaxed", relaxed)

    def test_all_legacy_standard_aliases_prepare_the_same_locked_request(self):
        for name, entry in MEDIA_SKUS.items():
            if entry["type"] != "video" or entry["locked_spec"]["priority"] != "standard":
                continue
            alias = name + "-standard"
            with self.subTest(model=alias):
                self.assertNotIn(alias, MEDIA_SKUS)
                self.assertEqual(get_sku_entry(alias), entry)
                for mode in entry["allowed_modes"]:
                    body = {"videoConfig": {"mode": mode}, "priority": "standard"}
                    self.assertEqual(
                        prepare_sku_request({"model": alias, **body}),
                        prepare_sku_request({"model": name, **body}),
                    )
                with self.assertRaises(ValueError):
                    prepare_sku_request({"model": alias, "priority": "relaxed"})

    def test_standard_suffix_is_not_a_generic_model_alias(self):
        for name, entry in MEDIA_SKUS.items():
            if entry["type"] != "image" and entry["locked_spec"]["priority"] != "relaxed":
                continue
            invalid = name + "-standard"
            with self.subTest(model=invalid):
                self.assertIsNone(get_sku_entry(invalid))
                with self.assertRaises(ValueError):
                    prepare_sku_request({"model": invalid})
        for invalid in ("unknown-standard", "flow-veo-3-1-fast-8s-720p-standard-standard"):
            with self.subTest(model=invalid):
                self.assertIsNone(get_sku_entry(invalid))
                with self.assertRaises(ValueError):
                    prepare_sku_request({"model": invalid})

    def test_image_sku_locks_image_size_but_allows_ratio_and_quality_hint(self):
        prepared = prepare_sku_request(
            {
                "model": "flow-banana-pro-4k",
                "size": "1024x1024",
                "quality": "high",
                "extra_body": {"generation_config": {"image_size": "4k"}},
            }
        )
        self.assertEqual(prepared["model"], "gemini-3.0-pro-image")
        self.assertEqual(
            prepared["generationConfig"],
            {"imageConfig": {"imageSize": "4k", "aspectRatio": "1:1"}},
        )
        self.assertNotIn("size", prepared)
        self.assertNotIn("quality", prepared)

    def test_conflicting_aliases_and_locked_dimensions_fail(self):
        with self.assertRaises(ValueError):
            prepare_sku_request(
                {
                    "model": "flow-banana-flash-1k",
                    "generationConfig": {"imageConfig": {"imageSize": "4k"}},
                }
            )
        with self.assertRaises(ValueError):
            prepare_sku_request(
                {
                    "model": "flow-veo-3-1-fast-8s-720p",
                    "extra_body": {"videoConfig": {"duration_seconds": 4}},
                }
            )
        with self.assertRaises(ValueError):
            prepare_sku_request(
                {
                    "model": "flow-veo-3-1-fast-8s-720p",
                    "generationConfig": {"videoConfig": {"mode": "extend"}},
                }
            )

    def test_prepare_does_not_mutate_input_and_keeps_allowed_video_mode(self):
        body = {
            "model": "flow-veo-3-1-fast-8s-720p",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}
                    ],
                }
            ],
            "generationConfig": {"videoConfig": {"mode": "image_to_video"}},
        }
        prepared = prepare_sku_request(body)
        self.assertEqual(body["model"], "flow-veo-3-1-fast-8s-720p")
        self.assertEqual(prepared["generationConfig"]["videoConfig"]["mode"], "image_to_video")

    def test_every_catalog_mode_resolves_to_existing_model(self):
        self.assertEqual(len(MEDIA_SKUS), 37)
        for sku, entry in MEDIA_SKUS.items():
            if entry["type"] == "image":
                prepared = prepare_sku_request({"model": sku})
                resolved = resolve_public_model(entry["public_model"], prepared, MODEL_CONFIG)
                self.assertEqual(resolved.options["imageSize"], entry["locked_spec"]["imageSize"])
                continue
            for mode in entry["allowed_modes"]:
                with self.subTest(sku=sku, mode=mode):
                    prepared = prepare_sku_request({"model": sku, "video_config": {"mode": mode}})
                    count = PUBLIC_MODELS[entry["public_model"]]["capabilities"]["modes"][mode][
                        "minImages"
                    ]
                    resolved = resolve_public_model(
                        entry["public_model"],
                        prepared,
                        MODEL_CONFIG,
                        images=[b"x"] * count,
                        has_video=mode == "edit",
                    )
                    self.assertEqual(
                        resolved.options["resolution"], entry["locked_spec"]["resolution"]
                    )

    def test_model_metadata_has_locked_combinations_and_per_mode_tier(self):
        models = {item["id"]: item for item in list_sku_models()}
        fast = models["flow-veo-3-1-fast-8s-4k"]
        self.assertNotIn("extend", fast["allowed_modes"])
        self.assertEqual(fast["capabilities"]["modes"]["image_to_video"]["minTier"], "ultra")
        self.assertTrue(
            all(c["resolutions"] == ["4k"] for c in fast["capabilities"]["combinations"])
        )
        self.assertEqual(models["flow-banana-pro-1k"]["capabilities"]["imageSizes"], ["1k"])
        models["flow-banana-pro-1k"]["locked_spec"]["imageSize"] = "4k"
        self.assertEqual(MEDIA_SKUS["flow-banana-pro-1k"]["locked_spec"]["imageSize"], "1k")

    def test_video_skus_advertise_async_video_creation_and_images_keep_image_routes(self):
        for model in list_sku_models():
            with self.subTest(sku=model["id"]):
                self.assertIn("/compat/v1/chat/completions", model["supported_endpoints"])
                if model["type"] == "video":
                    self.assertIn("/compat/v1/videos", model["supported_endpoints"])
                    self.assertNotIn("/compat/v1/images/edits", model["supported_endpoints"])
                else:
                    self.assertIn("/compat/v1/images/edits", model["supported_endpoints"])
                    self.assertNotIn("/compat/v1/videos", model["supported_endpoints"])

    def test_image_auto_size_keeps_ratio_inference_and_quality_does_not_upgrade(self):
        for quality in ("auto", "standard", "high", "hd"):
            with self.subTest(quality=quality):
                prepared = prepare_sku_request(
                    {"model": "flow-banana-lite-1k", "size": "auto", "quality": quality}
                )
                self.assertEqual(prepared["generationConfig"], {"imageConfig": {"imageSize": "1k"}})
        prepared = prepare_sku_request(
            {"model": "flow-banana-pro-4k", "size": "1024x1024", "aspect_ratio": "1:1"}
        )
        self.assertEqual(prepared["generationConfig"]["imageConfig"]["aspectRatio"], "1:1")
        with self.assertRaises(ValueError):
            prepare_sku_request(
                {"model": "flow-banana-pro-4k", "size": "1024x1024", "aspectRatio": "16:9"}
            )

    def test_all_request_locations_are_checked_and_malformed_configs_fail(self):
        locations = (
            {"imageSize": "4k"},
            {"image_size": "4k"},
            {"generation_config": {"image_config": {"image_size": "4k"}}},
            {"extraBody": {"generationConfig": {"imageConfig": {"imageSize": "4k"}}}},
            {"extra_body": {"imageConfig": {"resolution": "4k"}}},
        )
        for value in locations:
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_sku_request({"model": "flow-banana-flash-1k", **value})
        for value in (
            {"extra_body": []},
            {"generationConfig": None},
            {"image_config": "bad"},
            {"generation_config": {"imageConfig": []}},
            {"videoConfig": {}},
            {"imageConfig": {"unknown": "value"}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_sku_request({"model": "flow-banana-flash-1k", **value})
        for model in (None, [], {}, 4):
            with self.subTest(model=model), self.assertRaises(ValueError):
                prepare_sku_request({"model": model})

    def test_video_standard_selectors_lock_dimensions_and_omni_omits_priority(self):
        prepared = prepare_sku_request(
            {
                "model": "flow-omni-1-1-flash-4s-360p",
                "seconds": "4",
                "size": "360x640",
                "priority": "standard",
                "quality": "flash",
            }
        )
        self.assertEqual(
            prepared["generationConfig"]["videoConfig"],
            {"durationSeconds": 4, "resolution": "360p", "aspectRatio": "9:16"},
        )
        self.assertNotIn("seconds", prepared)
        self.assertNotIn("size", prepared)
        for value in (
            {"seconds": 6},
            {"seconds": True},
            {"size": "1280x720"},
            {"priority": "relaxed"},
            {"quality": "fast"},
            {"videoConfig": {"trimStartMs": 0}},
            {"trim_end_ms": 4000},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_sku_request({"model": "flow-omni-1-1-flash-4s-360p", **value})

    def test_video_mode_aliases_and_auto_are_allowed_but_not_unsupported_modes(self):
        for mode in ("auto", "first_frame", "interpolation"):
            prepare_sku_request({"model": "flow-veo-3-1-fast-8s-720p", "mode": mode})
        with self.assertRaises(ValueError):
            prepare_sku_request(
                {"model": "flow-veo-3-1-quality-8s-720p", "mode": "reference_to_video"}
            )

    def test_edit_skus_fix_resolution_and_keep_variable_source_duration(self):
        for resolution in ("360p", "720p", "1080p", "4k"):
            sku = f"flow-omni-1-1-flash-edit-{resolution}"
            with self.subTest(sku=sku):
                self.assertEqual(MEDIA_SKUS[sku]["allowed_modes"], ["edit"])
                prepared = prepare_sku_request({"model": sku, "video": "synthetic-upload"})
                self.assertEqual(prepared["video"], "synthetic-upload")
                self.assertEqual(
                    prepared["generationConfig"]["videoConfig"],
                    {"mode": "edit", "resolution": resolution},
                )
                resolved = resolve_public_model(
                    "omni_1.1_flash", prepared, MODEL_CONFIG, has_video=True
                )
                self.assertNotIn("durationSeconds", resolved.options)
                self.assertEqual(
                    MODEL_CONFIG[resolved.internal_model]["model_key"],
                    "abra_edit_360p" if resolution == "360p" else "abra_edit",
                )
        models = {item["id"]: item for item in list_sku_models()}
        entry = models["flow-omni-1-1-flash-edit-1080p"]
        self.assertEqual(entry["capabilities"]["combinations"][0]["durations"], [])
        self.assertNotIn("durationSeconds", entry["defaults"])
        self.assertEqual(entry["defaults"]["mode"], "edit")
        self.assertEqual(entry["capabilities"]["modes"]["edit"]["minTier"], "pro")

    def test_edit_trim_aliases_are_retained_checked_and_conflicts_rejected(self):
        sku = "flow-omni-1-1-flash-edit-720p"
        prepared = prepare_sku_request(
            {
                "model": sku,
                "trim_start_ms": 2000,
                "extra_body": {"generation_config": {"video_config": {"trim_end_ms": 7000}}},
            }
        )
        self.assertEqual(
            prepared["generationConfig"]["videoConfig"],
            {"mode": "edit", "resolution": "720p", "trimStartMs": 2000, "trimEndMs": 7000},
        )
        for value in (
            {"seconds": 8},
            {"durationSeconds": 8},
            {"duration_seconds": 8},
            {"mode": "text_to_video"},
            {"mode": "extend"},
            {"resolution": "4k"},
            {"size": "1920x1080"},
            {"trimStartMs": -1},
            {"trimEndMs": 0},
            {"trimEndMs": 10001},
            {"trimStartMs": 3000, "trimEndMs": 2000},
            {"trimStartMs": True},
            {"trimEndMs": "5000"},
            {"trimStartMs": 1, "extraBody": {"videoConfig": {"trim_start_ms": 2}}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_sku_request({"model": sku, **value})


if __name__ == "__main__":
    unittest.main()
