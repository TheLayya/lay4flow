import unittest

from src.api import routes


class ModelEndpointContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_models_advertise_only_image_entrypoints(self):
        catalog = await routes.list_models("test-key")
        image_models = [
            model
            for model in catalog["data"]
            if model.get("provider") == "flow" and model.get("output_modalities") == ["image"]
        ]
        self.assertTrue(image_models)
        expected = {
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/images/generations",
            "/v1/images/edits",
        }
        for model in image_models:
            self.assertEqual(set(model["supported_endpoints"]), expected)

    async def test_video_models_advertise_only_video_entrypoints(self):
        catalog = await routes.list_models("test-key")
        video_models = [
            model
            for model in catalog["data"]
            if model.get("provider") == "flow" and model.get("output_modalities") == ["video"]
        ]
        self.assertTrue(video_models)
        expected = {"/v1/chat/completions", "/v1/responses", "/v1/videos"}
        for model in video_models:
            self.assertEqual(set(model["supported_endpoints"]), expected)


if __name__ == "__main__":
    unittest.main()
