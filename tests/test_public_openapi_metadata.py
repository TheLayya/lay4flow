import unittest

import httpx

from src.main import API_VERSION, app, public_openapi

API_GUIDE = "docs/API.md"


class PublicOpenAPIMetadataTests(unittest.TestCase):
    def test_vids_schema_uses_pooled_upload_api_without_account_parameter(self):
        import asyncio

        from src.main import vids_openapi

        schema = asyncio.run(vids_openapi())
        self.assertTrue(schema["paths"])
        self.assertTrue(all(path.startswith("/providers/vids/v1/") for path in schema["paths"]))
        self.assertNotIn("token_id", str(schema["paths"]))
        self.assertFalse(any("/document/" in path for path in schema["paths"]))
        video = schema["paths"]["/providers/vids/v1/videos"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertIn("video_base64", video["properties"])
        self.assertNotIn("source_id", video["properties"])

    def test_application_and_public_api_reference_use_lay4flow_brand(self):
        import asyncio

        from src.main import compat_api_docs, compat_openapi, public_api_docs

        self.assertEqual(app.title, "lay4flow-remote")
        self.assertEqual(app.openapi()["info"]["title"], "lay4flow-remote")
        self.assertEqual(
            asyncio.run(public_openapi())["info"]["title"], "lay4flow-remote Developer API"
        )
        self.assertEqual(
            asyncio.run(compat_openapi())["info"]["title"], "lay4flow-remote Gateway Compatibility"
        )
        self.assertIn(b"lay4flow-remote API Reference", asyncio.run(public_api_docs()).body)
        self.assertIn(b"lay4flow-remote SKU API Reference", asyncio.run(compat_api_docs()).body)

    def test_gateway_schema_and_swagger_stay_separate_from_aggregate_api(self):
        import asyncio

        from src.main import compat_api_docs, compat_openapi

        schema = asyncio.run(compat_openapi())
        self.assertEqual(
            set(schema["paths"]),
            {
                "/compat/v1/models",
                "/compat/v1/models/{model}",
                "/compat/v1/chat/completions",
                "/compat/v1/images/generations",
                "/compat/v1/images/edits",
                "/compat/v1/videos",
                "/compat/v1/videos/{video_id}",
                "/compat/v1/videos/{video_id}/content",
            },
        )
        for operations in schema["paths"].values():
            for operation in operations.values():
                self.assertEqual(operation["security"], [{"HTTPBearer": []}])
        self.assertIn(b"/compat/v1/openapi.json", asyncio.run(compat_api_docs()).body)

    def test_api_guide_documents_distinct_openai_and_gemini_auth(self):
        from pathlib import Path

        guide = (Path(__file__).parents[1] / API_GUIDE).read_text(encoding="utf-8")
        self.assertIn("OpenAI 兼容接口（`/v1/*`）使用 `Authorization: Bearer", guide)
        self.assertIn("不接受 `x-goog-api-key` 或 `?key=`", guide)
        self.assertIn("Gemini 兼容接口（`/models/*`、`/v1beta/models/*`）仍支持", guide)
        self.assertIn("error.type=authentication_error", guide)
        self.assertIn("不承诺实现 OpenAI 全部资源", guide)
        for resource in ("Audio", "Embeddings", "Files", "Batches"):
            self.assertIn(f"`{resource}`", guide)

    def test_api_guide_documents_video_contract(self):
        from pathlib import Path

        guide = (Path(__file__).parents[1] / API_GUIDE).read_text(encoding="utf-8")
        for phrase in (
            "Videos 专用接口",
            "Idempotency-Key",
            "limit=20&after=<last_id>&order=desc",
            "has_more",
            "video_not_ready",
            "asset_expired",
            "video_in_progress",
        ):
            self.assertIn(phrase, guide)

    def test_api_guide_page_contains_copyable_endpoint_reference(self):
        from pathlib import Path

        page = (Path(__file__).parents[1] / "static" / "api-guide.html").read_text(encoding="utf-8")
        for phrase in (
            "Chat Completions：统一生成入口",
            "Gemini 原生格式",
            "Images：专用图片接口",
            "Videos：可恢复的异步视频任务",
            "/v1/models",
            "generationConfig",
            "/static/js/api-guide.js",
        ):
            self.assertIn(phrase, page)

    def test_application_and_public_schema_use_same_version(self):
        self.assertEqual(app.version, API_VERSION)
        schema = app.openapi()
        self.assertEqual(schema["info"]["version"], app.version)

    def test_public_schema_contains_only_supported_v1_endpoints(self):
        import asyncio

        schema = asyncio.run(public_openapi())
        paths = set(schema["paths"])
        self.assertTrue(paths)
        self.assertTrue(all(path.startswith("/v1/") for path in paths))
        expected_paths = {
            "/v1/chat/completions",
            "/v1/images/edits",
            "/v1/images/generations",
            "/v1/models",
            "/v1/models/aliases",
            "/v1/models/{model}",
            "/v1/responses",
            "/v1/videos",
            "/v1/videos/{video_id}",
            "/v1/videos/{video_id}/content",
        }
        self.assertEqual(paths, expected_paths)
        expected_methods = {
            "/v1/chat/completions": {"post"},
            "/v1/images/edits": {"post"},
            "/v1/images/generations": {"post"},
            "/v1/models": {"get"},
            "/v1/models/aliases": {"get"},
            "/v1/models/{model}": {"get"},
            "/v1/responses": {"post"},
            "/v1/videos": {"get", "post"},
            "/v1/videos/{video_id}": {"delete", "get"},
            "/v1/videos/{video_id}/content": {"get"},
        }
        self.assertEqual(
            {path: set(operations) for path, operations in schema["paths"].items()},
            expected_methods,
        )
        self.assertEqual(
            schema["components"]["securitySchemes"]["HTTPBearer"],
            {"type": "http", "scheme": "bearer"},
        )
        for operations in schema["paths"].values():
            for operation in operations.values():
                self.assertEqual(operation["security"], [{"HTTPBearer": []}])
        unsupported_prefixes = (
            "/v1/audio",
            "/v1/embeddings",
            "/v1/files",
            "/v1/batches",
            "/v1/realtime",
            "/v1/assistants",
        )
        self.assertFalse(any(path.startswith(unsupported_prefixes) for path in paths))
        self.assertFalse(any(path.startswith("/api/") for path in paths))

    def test_unmatched_public_routes_use_openai_error_shape(self):
        async def check():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
            ) as client:
                missing = await client.get("/v1/not-supported")
                wrong_method = await client.get("/v1/chat/completions")
                private = await client.get("/api/not-supported")
            return missing, wrong_method, private

        missing, wrong_method, private = __import__("asyncio").run(check())
        for response, code in ((missing, "not_found"), (wrong_method, "method_not_allowed")):
            self.assertEqual(response.status_code, 404 if code == "not_found" else 405)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertTrue(response.headers.get("x-request-id", "").startswith("req_"))
        self.assertEqual(private.status_code, 404)
        self.assertEqual(private.json(), {"detail": "Not Found"})

    def test_public_authentication_failures_use_openai_error_shape(self):
        async def check():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
            ) as client:
                missing = await client.get("/v1/models")
                invalid = await client.get(
                    "/v1/models", headers={"Authorization": "Bearer invalid-synthetic-key"}
                )
                google_header = await client.get(
                    "/v1/models", headers={"x-goog-api-key": "invalid-synthetic-key"}
                )
                query_key = await client.get("/v1/models", params={"key": "invalid-synthetic-key"})
            return missing, invalid, google_header, query_key

        missing, invalid, google_header, query_key = __import__("asyncio").run(check())
        for response in (missing, invalid, google_header, query_key):
            self.assertEqual(response.status_code, 401)
            payload = response.json()["error"]
            self.assertEqual(payload["type"], "authentication_error")
            self.assertEqual(payload["code"], "invalid_api_key")
            self.assertTrue(response.headers.get("x-request-id", "").startswith("req_"))


if __name__ == "__main__":
    unittest.main()
