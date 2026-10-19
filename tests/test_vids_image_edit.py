import json
import unittest
from pathlib import Path

from src.services.vids.client import document_image_shapes
from src.services.vids.image_edit import build_document_image_edit_request


class VidsDocumentImageEditTests(unittest.TestCase):
    def setUp(self):
        self.model = {
            "chunk": [
                [
                    3,
                    "SYNTHETIC_SHAPE",
                    3,
                    [1, 0, 0, 1, 0, 0],
                    [8, 1024, 9, 1024, 49, "SYNTHETIC_IMAGE"],
                    "SYNTHETIC_SCENE",
                ]
            ],
            "revision": 3,
        }
        self.options = {
            "doc_id": "SYNTHETIC_DOCUMENT",
            "flags": {},
            "shape": document_image_shapes(self.model)[0],
            "revision": self.model["revision"],
            "document_session_id": "SYNTHETIC_DOCUMENT_SESSION",
            "prompt": "SYNTHETIC_PROMPT",
            "session_id": "SYNTHETIC_SESSION",
        }

    def test_full_request_matches_official_saved_image_context(self):
        fixture = json.loads(
            Path(__file__)
            .with_name("fixtures")
            .joinpath("vids-image-edit-context.json")
            .read_text()
        )
        self.assertEqual(build_document_image_edit_request(**self.options), fixture["request"])

    def test_selected_shape_and_image_resource_keep_separate_namespaces(self):
        request = build_document_image_edit_request(**self.options)
        selection = request[2][8][3][0][0][7][2][0][3]
        self.assertEqual(json.loads(selection)[5], [8, ["SYNTHETIC_SHAPE"]])
        self.assertEqual(request[3][11], [None, None, "SYNTHETIC_IMAGE"])
        self.assertNotIn("SYNTHETIC_IMAGE", selection)
        self.assertEqual(request[2][5], [[1, 0, None, "SYNTHETIC_SCENE"]])

    def test_current_feature_flag_changes_only_operation(self):
        expected = build_document_image_edit_request(**self.options)
        expected[0] = 234
        for flag in (True, 1, "true", "1"):
            with self.subTest(flag=flag):
                options = {**self.options, "flags": {"docs-eaiecsf": flag}}
                self.assertEqual(build_document_image_edit_request(**options), expected)

    def test_invalid_or_missing_saved_image_context_is_rejected_without_values(self):
        invalid = (
            {"flags": []},
            {"shape": None},
            {"shape": {}},
            {"shape": {**self.options["shape"], "cosmo_id": []}},
            {"shape": {**self.options["shape"], "scene_id": " "}},
            {"revision": True},
            {"revision": 0},
            {"revision": -1},
            {"revision": "3"},
            {"document_session_id": ""},
            {"doc_id": ""},
            {"prompt": " "},
            {"session_id": None},
            {"request_id": 0},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError) as caught:
                build_document_image_edit_request(**{**self.options, **overrides})
            self.assertNotIn("SYNTHETIC", str(caught.exception))

    def test_quoted_shape_id_preserves_selection_json(self):
        shape = {**self.options["shape"], "shape_id": 'shape"[}]'}
        request = build_document_image_edit_request(**{**self.options, "shape": shape})
        selection = json.loads(request[2][8][3][0][0][7][2][0][3])
        self.assertEqual(selection[5], [8, ['shape"[}]']])


if __name__ == "__main__":
    unittest.main()
