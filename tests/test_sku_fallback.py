"""The public SKU list comes from the same locked server contract."""

import json
import unittest
from unittest.mock import patch

from scripts.generate_model_fallback import OUTPUT as MODEL_OUTPUT, render_catalog as render_models
from scripts.generate_sku_fallback import FIELDS, OUTPUT, render_catalog
from src.core.media_skus import MEDIA_SKUS, list_sku_models, prepare_sku_request


class SkuFallbackTests(unittest.TestCase):
    def test_generated_skus_match_server_with_only_public_metadata(self):
        expected = render_catalog()
        self.assertEqual(OUTPUT.read_text(encoding="utf-8"), expected)
        payload = json.loads(expected.split("const FALLBACK_MEDIA_SKUS = ", 1)[1].rstrip(";\n"))
        self.assertEqual(len(payload), 37)
        self.assertEqual({item["id"] for item in payload}, set(MEDIA_SKUS))
        server = {item["id"]: item for item in list_sku_models()}
        for item in payload:
            self.assertEqual(set(item), set(FIELDS))
            self.assertEqual(item, {field: server[item["id"]][field] for field in FIELDS})
            self.assertTrue(item["description"])
            self.assertTrue(item["allowed_modes"])
            self.assertNotIn("price", item)
            if item["type"] == "image":
                self.assertEqual(set(item["locked_spec"]), {"imageSize"})
            else:
                self.assertTrue({"resolution", "priority"} <= set(item["locked_spec"]))
                self.assertNotIn("extend", item["allowed_modes"])
                prepared = prepare_sku_request({"model": item["id"]})
                options = prepared["generationConfig"]["videoConfig"]
                if item["allowed_modes"] == ["edit"]:
                    self.assertNotIn("durationSeconds", item["locked_spec"])
                    self.assertNotIn("durationSeconds", options)
                    self.assertEqual(options["mode"], "edit")
                else:
                    self.assertEqual(
                        options["durationSeconds"], item["locked_spec"]["durationSeconds"]
                    )

    def test_render_uses_server_source_and_does_not_modify_existing_catalog(self):
        before = MODEL_OUTPUT.read_bytes()
        model = {field: f"test-{field}" for field in FIELDS}
        with patch("src.core.media_skus.list_sku_models", return_value=[model]):
            rendered = render_catalog()
        self.assertIn('"id":"test-id"', rendered)
        self.assertNotIn("flow-banana", rendered)
        self.assertEqual(MODEL_OUTPUT.read_bytes(), before)
        self.assertEqual(MODEL_OUTPUT.read_text(encoding="utf-8"), render_models())


if __name__ == "__main__":
    unittest.main()
