"""Guard published names/semantics and offline upstream change detection."""

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.check_upstream import compare_snapshots, read_snapshot
from src.core.media_skus import MEDIA_SKUS
from src.core.model_catalog import MODEL_CONFIG
from src.core.model_resolver import VIDEO_BASE_MODELS
from src.core.public_models import PUBLIC_MODELS

ROOT = Path(__file__).resolve().parents[1]


class UpstreamCompatibilityTests(unittest.TestCase):
    def test_public_veo_policy_is_eight_seconds_and_legacy_names_remain(self):
        # The public policy is deliberate; historical internal names remain compatible.
        veo = PUBLIC_MODELS["veo_3_1"]
        self.assertEqual(veo["defaults"]["durationSeconds"], 8)
        for combination in veo["capabilities"]["combinations"]:
            self.assertEqual(combination["durations"], [8], combination)
        for sku, entry in MEDIA_SKUS.items():
            if entry["public_model"] == "veo_3_1":
                self.assertEqual(entry["locked_spec"]["durationSeconds"], 8, sku)
        for seconds in (4, 6):
            for kind in ("t2v", "i2v_s"):
                self.assertIn(f"veo_3_1_{kind}_landscape_{seconds}s", MODEL_CONFIG)

    def test_existing_names_keep_their_generation_semantics(self):
        baseline = json.loads(
            (ROOT / "tests/fixtures/model_compatibility.json").read_text(encoding="utf-8")
        )
        for name, expected in baseline["models"].items():
            with self.subTest(model=name):
                current = json.dumps(
                    MODEL_CONFIG[name], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                self.assertEqual(hashlib.sha256(current.encode()).hexdigest(), expected)
        for name, alias in baseline["video_aliases"].items():
            self.assertEqual(VIDEO_BASE_MODELS[name], alias, name)
        for name, original in baseline["public_models"].items():
            current = PUBLIC_MODELS[name]
            self.assertEqual(current["type"], original["type"], name)
            self.assertEqual(current["defaults"], original["defaults"], name)
            for field in ("aspectRatios", "imageSizes", "qualities", "priorities"):
                for value in original["capabilities"].get(field, []):
                    self.assertIn(value, current["capabilities"][field], (name, field, value))
            for field in ("minImages", "maxImages"):
                if field in original["capabilities"]:
                    self.assertEqual(
                        current["capabilities"][field], original["capabilities"][field]
                    )
            for mode, limits in original["capabilities"].get("modes", {}).items():
                self.assertEqual(current["capabilities"]["modes"][mode], limits, (name, mode))
            for combination in original["capabilities"].get("combinations", []):
                self.assertTrue(
                    any(
                        all(
                            candidate.get(key) == value
                            for key, value in combination.items()
                            if key not in ("durations", "resolutions")
                        )
                        and set(combination["durations"]).issubset(candidate["durations"])
                        and set(combination["resolutions"]).issubset(candidate["resolutions"])
                        for candidate in current["capabilities"]["combinations"]
                    ),
                    (name, combination),
                )

    def test_added_removed_and_unadvertised_legacy_keys_are_distinct(self):
        old = {
            "video": [{"models": [{"key": "native"}, {"key": "upscale"}]}],
            "image": [{"keys": ["image"]}],
        }
        new = deepcopy(old)
        new["video"][0]["models"] = [{"key": "native"}, {"key": "new"}]
        catalog = {
            "video": {"type": "video", "model_key": "native", "upsample": {"model_key": "upscale"}},
            "legacy": {"type": "image", "model_name": "legacy"},
        }
        result = compare_snapshots(old, new, catalog)
        self.assertEqual(result["video"]["added"], ["new"])
        self.assertEqual(result["video"]["newly_missing_used_keys"], ["upscale"])
        self.assertEqual(result["image"]["used_not_advertised"], ["legacy"])
        self.assertEqual(result["image"]["newly_missing_used_keys"], [])
        new["video"][0]["models"][0]["seconds"] = 6
        report = compare_snapshots(old, new, catalog)
        self.assertEqual(report["video"]["changed_definitions"]["native"]["after"]["seconds"], 6)

    def test_captured_snapshot_and_invalid_snapshot(self):
        actual = read_snapshot(ROOT / "tests/fixtures/flow-model-catalog.json")
        result = compare_snapshots(actual, actual, MODEL_CONFIG)
        self.assertFalse(any(v["added"] or v["removed"] for v in result.values()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for value in (
                {},
                {"video": [{"models": [None]}], "image": []},
                {"video": [{"models": ["wrong"]}], "image": []},
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_snapshot(path)
