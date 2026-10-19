import unittest
from types import SimpleNamespace

from src.api import routes
from src.services.model_gateway import ModelGateway


class Provider:
    async def complete(self, request):
        return {"request": request}

    async def stream(self, request):
        yield {"request": request}


class ModelGatewayTests(unittest.TestCase):
    def test_registers_explicit_project_metadata_and_provider(self):
        gateway = ModelGateway()
        provider = Provider()
        gateway.register(
            "other-project-chat",
            provider,
            owned_by="other-team",
            project_id="other-project",
            capabilities={"input_modalities": ["text"]},
        )

        self.assertIs(gateway.provider("other-project-chat"), provider)
        self.assertEqual(
            gateway.models(),
            [
                {
                    "id": "other-project-chat",
                    "object": "model",
                    "created": 0,
                    "owned_by": "other-team",
                    "provider": "other-project",
                    "supported_endpoints": ["/v1/chat/completions"],
                    "input_modalities": ["text"],
                }
            ],
        )

    def test_model_metadata_is_defensively_copied(self):
        gateway = ModelGateway()
        capabilities = {"modalities": ["text"]}
        gateway.register("copy-test", Provider(), owned_by="team", capabilities=capabilities)
        capabilities["modalities"].append("image")
        listed = gateway.models()
        listed[0]["modalities"].append("audio")

        self.assertEqual(gateway.models()[0]["modalities"], ["text"])

    def test_rejects_duplicate_or_existing_flow_model(self):
        gateway = ModelGateway()
        provider = Provider()
        gateway.register("custom-one", provider, owned_by="team", capabilities={})
        with self.assertRaises(ValueError):
            gateway.register("custom-one", provider, owned_by="team", capabilities={})
        with self.assertRaises(ValueError):
            gateway.register("veo_3_1", provider, owned_by="team", capabilities={})
        with self.assertRaises(ValueError):
            gateway.register(
                "custom-endpoint-override",
                provider,
                owned_by="team",
                capabilities={"supported_endpoints": ["/v1/responses"]},
            )

    def test_route_model_catalog_uses_application_state(self):
        first = ModelGateway()
        second = ModelGateway()
        first.register("project-one-chat", Provider(), owned_by="one", capabilities={})
        second.register("project-two-chat", Provider(), owned_by="two", capabilities={})

        first_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(model_gateway=first))
        )
        second_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(model_gateway=second))
        )
        first_catalog = __import__("asyncio").run(
            routes.list_models("test-key", raw_request=first_request)
        )
        second_catalog = __import__("asyncio").run(
            routes.list_models("test-key", raw_request=second_request)
        )

        self.assertIn("project-one-chat", {entry["id"] for entry in first_catalog["data"]})
        self.assertNotIn("project-two-chat", {entry["id"] for entry in first_catalog["data"]})
        self.assertIn("project-two-chat", {entry["id"] for entry in second_catalog["data"]})
        self.assertNotIn("project-one-chat", {entry["id"] for entry in second_catalog["data"]})


if __name__ == "__main__":
    unittest.main()
