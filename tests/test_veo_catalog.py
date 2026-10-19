"""Veo variants are capabilities of one product, while old names stay callable."""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import router
from src.core.auth import verify_api_key_flexible, verify_openai_api_key
from src.core.public_models import resolve_public_model
from src.core.account_tiers import supports_model_for_tier
from src.services.generation_handler import MODEL_CONFIG


ALIASES = {"veo_3_1_interpolation_lite", "veo_3_1_remix", "veo_3_1_extend"}


class VeoCatalogTests(unittest.TestCase):
    def test_relaxed_maps_supported_modes_to_existing_ultra_paths(self):
        for mode, count, kind in (
            ("text_to_video", 0, "t2v"),
            ("image_to_video", 1, "i2v_s"),
            ("first_last_frame", 2, "i2v_s"),
            ("reference_to_video", 3, "r2v"),
        ):
            for aspect, suffix in (("16:9", ""), ("9:16", "_portrait")):
                request = {
                    "videoConfig": {"mode": mode, "priority": "relaxed", "aspectRatio": aspect}
                }
                result = resolve_public_model(
                    "veo_3_1", request, MODEL_CONFIG, images=[b"image"] * count
                )
                self.assertEqual(
                    result.internal_model, f"veo_3_1_{kind}_fast{suffix}_ultra_relaxed"
                )
                self.assertEqual(result.public_model, "veo_3_1")
                self.assertEqual(result.options["priority"], "relaxed")
                self.assertIn("relaxed", MODEL_CONFIG[result.internal_model]["model_key"])
                self.assertFalse(supports_model_for_tier(result.internal_model, "PAYGATE_TIER_ONE"))
                self.assertTrue(supports_model_for_tier(result.internal_model, "PAYGATE_TIER_TWO"))

    def test_invalid_relaxed_combinations_are_rejected_instead_of_downgraded(self):
        for change in (
            {"quality": "lite"},
            {"quality": "quality"},
            {"durationSeconds": 4},
            {"durationSeconds": 6},
            {"resolution": "1080p"},
            {"resolution": "4k"},
            {"priority": "invalid"},
            {"mode": "extend"},
        ):
            options = {"mode": "text_to_video", "priority": "relaxed", **change}
            with self.assertRaises(ValueError):
                resolve_public_model(
                    "veo_3_1",
                    {"videoConfig": options},
                    MODEL_CONFIG,
                    video_media_id="source" if options["mode"] == "extend" else None,
                )
        with self.assertRaises(ValueError):
            resolve_public_model(
                "omni_1.1_flash", {"videoConfig": {"priority": "relaxed"}}, MODEL_CONFIG
            )
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            resolve_public_model(
                "veo_3_1",
                {
                    "videoConfig": {"priority": "relaxed"},
                    "extra_body": {"videoConfig": {"priority": "standard"}},
                },
                MODEL_CONFIG,
            )

    def test_all_http_catalogs_show_public_veo_aliases_and_legacy_lookup_survives(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[verify_api_key_flexible] = lambda: "test-key"
        app.dependency_overrides[verify_openai_api_key] = lambda: "test-key"
        with TestClient(app) as client:
            for endpoint in ("/v1/models", "/v1/models/aliases", "/models", "/v1beta/models"):
                payload = client.get(endpoint).json()
                entries = payload.get("data", payload.get("models"))
                names = {
                    entry.get("id", entry.get("name", "").removeprefix("models/"))
                    for entry in entries
                }
                self.assertEqual(len(names), 6)
                self.assertEqual(
                    {name for name in names if name.startswith("veo")},
                    {"veo_3_1"},
                )
                legacy = client.get(endpoint, params={"include_legacy": True}).json()
                names = {
                    entry.get("id", entry.get("name", "").removeprefix("models/"))
                    for entry in legacy.get("data", legacy.get("models"))
                }
                self.assertTrue(ALIASES <= names)
            for alias in ALIASES:
                self.assertEqual(client.get("/v1beta/models/" + alias).status_code, 200)

    def test_single_veo_parameters_preserve_interpolation_and_remix_routes(self):
        for aspect in ("16:9", "9:16"):
            for duration in (8,):
                config = {
                    "mode": "first_last_frame",
                    "quality": "lite",
                    "durationSeconds": duration,
                    "aspectRatio": aspect,
                }
                request = {"generationConfig": {"videoConfig": config}}
                result = resolve_public_model(
                    "veo_3_1", request, MODEL_CONFIG, images=[b"first", b"last"]
                )
                legacy = resolve_public_model(
                    "veo_3_1_interpolation_lite", request, MODEL_CONFIG, images=[b"first", b"last"]
                )
                self.assertEqual(result.internal_model, legacy.internal_model)
                self.assertIn("interpolation_lite", result.internal_model)
            for duration in (4, 6):
                config = {
                    "mode": "first_last_frame",
                    "quality": "lite",
                    "durationSeconds": duration,
                    "aspectRatio": aspect,
                }
                with self.assertRaisesRegex(
                    ValueError,
                    "Unsupported mode/quality/priority/durationSeconds/resolution combination",
                ):
                    resolve_public_model(
                        "veo_3_1", {"videoConfig": config}, MODEL_CONFIG, images=[b"first", b"last"]
                    )
            for mode in ("extend",):
                request = {"videoConfig": {"mode": mode, "aspectRatio": aspect}}
                result = resolve_public_model(
                    "veo_3_1", request, MODEL_CONFIG, video_media_id="existing-media"
                )
                for alias in ("veo_3_1_remix", "veo_3_1_extend"):
                    legacy = resolve_public_model(
                        alias, request, MODEL_CONFIG, video_media_id="existing-media"
                    )
                    self.assertEqual(result.internal_model, legacy.internal_model)
                self.assertIn("veo_3_1_extend", result.internal_model)

    def test_extend_exposes_fast_lite_and_quality(self):
        for quality, marker in (
            ("quality", "veo_3_1_extend"),
            ("fast", "veo_3_1_extend_fast"),
            ("lite", "veo_3_1_extension_lite"),
        ):
            result = resolve_public_model(
                "veo_3_1",
                {"videoConfig": {"mode": "extend", "quality": quality}},
                MODEL_CONFIG,
                video_media_id="existing-media",
            )
            self.assertIn(marker, result.internal_model)


if __name__ == "__main__":
    unittest.main()
