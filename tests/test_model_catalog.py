"""The catalog needs no generation services and preserves compatible imports."""

import subprocess
import sys
import unittest
from pathlib import Path

from src.core import model_catalog
from src.services import generation_handler


class ModelCatalogTests(unittest.TestCase):
    def test_flash_aliases_use_current_successor_and_preserve_lite(self):
        aspects = ("landscape", "portrait", "square", "four-three", "three-four")
        for aspect in aspects:
            for suffix in ("", "-2k", "-4k"):
                with self.subTest(aspect=aspect, suffix=suffix):
                    cfg = model_catalog.MODEL_CONFIG[f"gemini-3.1-flash-image-{aspect}{suffix}"]
                    self.assertEqual(cfg["model_name"], "BELUGA")
                    self.assertEqual(cfg["type"], "image")
                    if suffix:
                        self.assertEqual(
                            cfg["upsample"], f"UPSAMPLE_IMAGE_RESOLUTION_{suffix[1:].upper()}"
                        )
            self.assertEqual(
                model_catalog.MODEL_CONFIG[f"gemini-3.1-flash-image-lite-{aspect}"]["model_name"],
                "HARBOR_SEAL",
            )

    def test_catalog_import_does_not_initialize_generation_services(self):
        # core.__init__ still exposes the existing config/auth/logging objects.
        # Model data must not load the transport, browser, or execution services.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                "sys.modules['src.services'] = None; "
                "sys.modules['curl_cffi'] = None; "
                "sys.modules['playwright'] = None; "
                "from src.core.model_catalog import MODEL_CONFIG; "
                "assert MODEL_CONFIG['omni_edit_1080p']['video_type'] == 'edit'",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_imports_share_the_same_catalog_and_factories(self):
        for name in (
            "MODEL_CONFIG",
            "_apply_omni_model_updates",
            "_apply_veo_3_1_model_updates",
            "_make_i2v_config",
            "_make_t2v_config",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(generation_handler, name), getattr(model_catalog, name))


if __name__ == "__main__":
    unittest.main()
