"""Observed Flow Omni 1.2 usages, public options, tiers and SKU compatibility."""

import json
import unittest
from pathlib import Path

from src.core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    PAYGATE_TIER_ONE,
    PAYGATE_TIER_TWO,
    PAYGATE_TIER_ZERO,
    get_paygate_tier_label,
    get_paygate_tier_rank,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from src.core.media_skus import MEDIA_SKUS, list_sku_models, prepare_sku_request
from src.core.model_catalog import MODEL_CONFIG
from src.core.model_resolver import resolve_model_name
from src.core.public_models import PUBLIC_MODELS, resolve_public_model


class Omni12CatalogTests(unittest.TestCase):
    def resolve(self, **options):
        mode = options.get("mode", "text_to_video")
        images = [b"synthetic"] * PUBLIC_MODELS["omni_1.2_flash"]["capabilities"]["modes"][mode][
            "minImages"
        ]
        return resolve_public_model(
            "omni_1.2_flash",
            {"videoConfig": options},
            MODEL_CONFIG,
            images=images,
            has_video=mode == "edit",
            video_media_id="synthetic-source" if mode == "extend" else None,
        )

    def test_internal_native_usage_set_exactly_matches_all_observed_112_catalog_keys(self):
        observed = json.loads(
            (Path(__file__).parent / "fixtures/flow_omni12_models.json").read_text(encoding="utf-8")
        )
        family = next(row for row in observed[0][4] if row[0] == "Omni 1.2 Flash")
        keys = {usage[0] for usage in family[1]}
        actual = {
            spec["model_key"]
            for alias, spec in MODEL_CONFIG.items()
            if alias.startswith("omni_1_2_")
        }
        self.assertEqual(len(keys), 112)
        self.assertEqual(actual, keys)
        for alias, spec in MODEL_CONFIG.items():
            if not alias.startswith("omni_1_2_"):
                continue
            with self.subTest(alias=alias):
                self.assertFalse(spec["allow_tier_upgrade"])
                if "_360p" in alias:
                    self.assertNotIn("upsample", spec)
                if "upsample" in spec:
                    self.assertFalse(spec["model_key"].endswith("_360p"))

    def test_native_modes_resolve_every_supported_second_without_rewriting_old_family(self):
        for mode, upstream in (
            ("text_to_video", "t2v"),
            ("image_to_video", "i2v"),
            ("reference_to_video", "r2v"),
            ("first_last_frame", "i2v_fl"),
            ("extend", "extend"),
        ):
            for seconds in range(3, 11 if mode in {"first_last_frame", "extend"} else 16):
                for resolution in ("720p", "360p"):
                    for aspect in ("16:9", "9:16"):
                        with self.subTest(
                            mode=mode, seconds=seconds, resolution=resolution, aspect=aspect
                        ):
                            resolved = self.resolve(
                                mode=mode,
                                durationSeconds=seconds,
                                resolution=resolution,
                                aspectRatio=aspect,
                            )
                            spec = MODEL_CONFIG[resolved.internal_model]
                            self.assertEqual(
                                spec["model_key"],
                                f"macaron_cookie_{upstream}_{seconds}s"
                                + ("_360p" if resolution == "360p" else ""),
                            )
                            self.assertEqual(spec["duration_seconds"], seconds)
        old = resolve_public_model("omni_1.1_flash", {}, MODEL_CONFIG)
        self.assertEqual(MODEL_CONFIG[old.internal_model]["model_key"], "abra_t2v_8s")
        for bad_mode in ("first_last_frame", "extend"):
            with self.assertRaises(ValueError):
                self.resolve(mode=bad_mode, durationSeconds=15)
        for bad_seconds in (0, 2, 16, True, 3.0):
            with self.assertRaises(ValueError):
                self.resolve(mode="text_to_video", durationSeconds=bad_seconds)

    def test_edit_trim_preserves_legacy_limits_and_encodes_effective_account_requirement(self):
        for end, seconds, tier in (
            (3000, 3, PAYGATE_TIER_NOT_PAID),
            (10000, 10, PAYGATE_TIER_NOT_PAID),
            (10001, 11, PAYGATE_TIER_ZERO),
            (15000, 15, PAYGATE_TIER_ZERO),
        ):
            with self.subTest(end=end):
                resolved = self.resolve(mode="edit", trimStartMs=0, trimEndMs=end)
                self.assertEqual(resolved.trim_end_ms, end)
                spec = MODEL_CONFIG[resolved.internal_model]
                self.assertEqual(spec["duration_seconds"], seconds)
                self.assertEqual(spec["max_trim_ms"], 15000)
                self.assertEqual(spec["model_key"], "macaron_cookie_edit")
                self.assertEqual(get_required_paygate_tier_for_model(resolved.internal_model), tier)
        for bad_span in (2999, 15001):
            with self.assertRaises(ValueError):
                self.resolve(mode="edit", trimStartMs=0, trimEndMs=bad_span)
        with self.assertRaises(ValueError):
            resolve_public_model(
                "omni_1.1_flash",
                {"videoConfig": {"mode": "edit", "trimEndMs": 15000}},
                MODEL_CONFIG,
                has_video=True,
            )

    def test_paid_durations_extend_and_upscale_cannot_schedule_a_free_or_unknown_account(self):
        self.assertEqual(normalize_user_paygate_tier(PAYGATE_TIER_ZERO), PAYGATE_TIER_ZERO)
        self.assertEqual(get_paygate_tier_label(PAYGATE_TIER_ZERO), "Plus")
        self.assertLess(
            get_paygate_tier_rank(PAYGATE_TIER_ZERO), get_paygate_tier_rank(PAYGATE_TIER_ONE)
        )
        for alias in (
            "omni_1_2_15s",
            "omni_1_2_r2v_portrait_11s_360p",
            "omni_1_2_extend_3s",
            "omni_1_2_edit_15s",
            "omni_1_2_8s_1080p",
        ):
            with self.subTest(alias=alias):
                self.assertEqual(get_required_paygate_tier_for_model(alias), PAYGATE_TIER_ZERO)
                for tier in (None, "unverified-import-hint", PAYGATE_TIER_NOT_PAID):
                    self.assertFalse(supports_model_for_tier(alias, tier))
                for tier in (PAYGATE_TIER_ZERO, PAYGATE_TIER_ONE, PAYGATE_TIER_TWO):
                    self.assertTrue(supports_model_for_tier(alias, tier))
        self.assertFalse(supports_model_for_tier("omni_1_2_15s_4k", PAYGATE_TIER_ZERO))
        self.assertTrue(supports_model_for_tier("omni_1_2_15s_4k", PAYGATE_TIER_TWO))
        self.assertFalse(supports_model_for_tier("omni_8s_1080p", PAYGATE_TIER_ZERO))
        self.assertTrue(supports_model_for_tier("omni_1_2_10s_360p", PAYGATE_TIER_NOT_PAID))

    def test_new_skus_keep_extend_distinct_and_allow_editor_trim_without_changing_old_skus(self):
        generated = MEDIA_SKUS["flow-omni-1-2-flash-15s-720p"]
        self.assertNotIn("first_last_frame", generated["allowed_modes"])
        self.assertNotIn("extend", generated["allowed_modes"])
        self.assertEqual(
            MEDIA_SKUS["flow-omni-1-2-flash-extend-3s-360p"]["allowed_modes"], ["extend"]
        )
        prepared = prepare_sku_request({"model": "flow-omni-1-2-flash-extend-3s-360p"})
        self.assertEqual(
            prepared["generationConfig"]["videoConfig"],
            {"mode": "extend", "durationSeconds": 3, "resolution": "360p"},
        )
        prepared = prepare_sku_request(
            {"model": "flow-omni-1-2-flash-edit-720p", "trimEndMs": 15000}
        )
        resolved = resolve_public_model(prepared["model"], prepared, MODEL_CONFIG, has_video=True)
        self.assertEqual(resolved.internal_model, "omni_1_2_edit_15s")
        with self.assertRaises(ValueError):
            prepare_sku_request({"model": "flow-omni-1-1-flash-edit-720p", "trimEndMs": 15000})
        public = {row["id"]: row for row in list_sku_models()}
        self.assertEqual(
            public["flow-omni-1-2-flash-15s-720p"]["capabilities"]["modes"]["image_to_video"][
                "minTier"
            ],
            "plus",
        )
        self.assertEqual(
            public["flow-omni-1-2-flash-edit-720p"]["capabilities"]["modes"]["edit"]["maxTrimMs"],
            15000,
        )

    def test_legacy_internal_resolution_alias_routes_both_orientations_for_new_model(self):
        from types import SimpleNamespace

        for base in (
            "omni_1_2_15s",
            "omni_1_2_i2v_10s_first_last",
            "omni_1_2_edit_15s",
            "omni_1_2_extend_5s",
        ):
            result = resolve_model_name(
                base,
                SimpleNamespace(generationConfig={"imageConfig": {"aspectRatio": "9:16"}}),
                MODEL_CONFIG,
            )
            self.assertIn("_portrait_", result)
            self.assertIn(result, MODEL_CONFIG)


if __name__ == "__main__":
    unittest.main()
