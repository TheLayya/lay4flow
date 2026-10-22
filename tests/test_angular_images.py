"""Synthetic fixtures for the public Angular image JSON-proto contract."""

import json
import unittest

from src.services.flow.angular_images import (
    IMAGE_RPC,
    MODELS_RPC,
    build_image_request,
    parse_image_models,
    parse_image_response,
    resolve_image_usage,
)
from src.services.flow.media import extract_image_url_from_media


def proto(**fields):
    result = [None] * max((int(key) for key in fields), default=0)
    for key, value in fields.items():
        result[int(key) - 1] = value
    return result


def model_config(*, enabled=True):
    # GetModels response field1 config -> field6 image families. The usage
    # map fields match G7a, H7a, mbb, nbb and obb in the public frontend.
    pricing = proto(**{"1": proto(**{"2": 0})}) if enabled else proto(**{"2": []})
    usage = proto(
        **{
            "1": "GEM_PIX_2",
            "5": [[2, pricing]],
            "9": [[[[1]], [[1, 3]]]],
            "14": [[1, 2, 3, 4, 5]],
        }
    )
    family = ["Nano Banana Pro", [usage], None, "nano_banana_pro", [["Legacy Pro"]]]
    return [proto(**{"6": [family]})]


def generated_media(*, url="https://images.example.invalid/generated.png", status=3):
    return proto(
        **{
            "1": "media/actual-output",
            "3": "workflows/actual-workflow",
            "6": proto(**{"9": [status]}),
            "7": [proto(**{"14": url})],
        }
    )


class AngularImageTests(unittest.TestCase):
    def request(self, **changes):
        values = dict(
            project_id="project-fixture",
            prompt="A synthetic lantern",
            model_name="GEM_PIX_2",
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            captcha_token="synthetic-captcha",
            seed=123,
            batch_id="batch-fixture",
            media_id="media-seed",
            workflow_id="workflow-seed",
        )
        return build_image_request(**(values | changes))

    def test_plain_text_request_matches_frontend_field_numbers(self):
        payload = self.request()
        self.assertEqual(IMAGE_RPC, "ogiZ0b")
        self.assertEqual(MODELS_RPC, "HTrJv")
        self.assertEqual(payload[0], None)
        self.assertTrue(payload[2])
        self.assertEqual(payload[4], ["batch-fixture"])
        context = payload[3]
        self.assertEqual(
            context,
            [
                None,
                22,
                None,
                None,
                None,
                "project-fixture",
                None,
                None,
                None,
                None,
                ["synthetic-captcha", 1],
            ],
        )
        item = payload[1][0]
        self.assertEqual(item[2], [])
        self.assertEqual(item[3:6], [123, 3, "GEM_PIX_2"])
        self.assertEqual(item[7], context)
        self.assertEqual(item[8], [[["A synthetic lantern"]]])
        self.assertEqual(item[12:14], ["workflow-seed", "media-seed"])
        self.assertNotIn("clientContext", json.dumps(payload))

    def test_reference_and_base_images_use_media_name_and_explicit_type(self):
        payload = self.request(
            image_inputs=[
                {"name": "media/ref", "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"},
                {"name": "media/base", "imageInputType": "IMAGE_INPUT_TYPE_BASE_IMAGE"},
            ]
        )
        self.assertEqual(
            payload[1][0][2],
            [["media/ref", None, None, None, 1], ["media/base", None, None, None, 2]],
        )

    def test_request_preserves_unicode_and_boundary_seeds(self):
        for seed in (0, 2147483646):
            item = self.request(prompt="?????", seed=seed, aspect_ratio="1:1")[1][0]
            self.assertEqual(item[3], seed)
            self.assertEqual(item[4], 1)
            self.assertEqual(item[8], [[["?????"]]])

    def test_invalid_request_never_fabricates_payload(self):
        for changes in (
            {"project_id": ""},
            {"prompt": " "},
            {"model_name": ""},
            {"captcha_token": ""},
            {"seed": True},
            {"seed": -1},
            {"seed": 2147483647},
            {"aspect_ratio": "unknown"},
            {"workflow_id": ""},
            {"image_inputs": [{"name": "media/ref", "imageInputType": "unknown"}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.request(**changes)

    def test_response_preserves_actual_media_and_workflow_and_pipeline_shape(self):
        result = parse_image_response([[generated_media()], [["workflows/actual-workflow"]]])
        self.assertEqual(result["media"][0]["name"], "media/actual-output")
        self.assertEqual(result["media"][0]["workflowId"], "workflows/actual-workflow")
        self.assertEqual(
            extract_image_url_from_media(result["media"]),
            "https://images.example.invalid/generated.png",
        )
        self.assertEqual(result["workflows"], [["workflows/actual-workflow"]])

    def test_rejected_pending_malformed_and_unsafe_media_are_not_success(self):
        for payload in (
            {},
            ["invalid"],
            [[{}]],
            [[generated_media(status=4)]],
            [[generated_media(status=5)]],
            [[generated_media(status=7)]],
            [[generated_media(status=1)]],
            [[generated_media(status=2)]],
            [[generated_media(status=6)]],
            [[generated_media(status=True)]],
            [[generated_media(url=None, status=1)]],
            [[generated_media(url="javascript:alert(1)")]],
            [[generated_media(url="https://user:secret@images.example.invalid/a")]],
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_image_response(payload)

    def test_empty_media_response_stays_empty_for_pipeline_error_handling(self):
        self.assertEqual(parse_image_response([[]]), {"media": [], "workflows": []})

    def test_config_resolves_model_id_alias_and_exact_usage_without_static_guess(self):
        payload = model_config()
        models = parse_image_models(payload)
        self.assertEqual(models[0]["id"], "nano_banana_pro")
        self.assertEqual(models[0]["aliases"], ["Legacy Pro"])
        for name in ("nano_banana_pro", "Nano Banana Pro", "legacy pro", "GEM_PIX_2"):
            self.assertEqual(resolve_image_usage(payload, name, "16:9", 2), "GEM_PIX_2")
        self.assertEqual(resolve_image_usage(payload, "GEM_PIX_2", "1:1", 2, True), "GEM_PIX_2")

    def test_absent_disabled_or_wrong_tier_model_never_falls_back(self):
        for payload, model, tier in (
            (model_config(), "IMAGEN_3_5", 2),
            (model_config(enabled=False), "GEM_PIX_2", 2),
            (model_config(), "GEM_PIX_2", 1),
        ):
            with self.subTest(model=model, tier=tier), self.assertRaises(ValueError):
                resolve_image_usage(payload, model, "16:9", tier)


if __name__ == "__main__":
    unittest.main()
