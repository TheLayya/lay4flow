"""The Studio bridge uses the admin cookie and never exposes the service key."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from src.api import admin, routes
from src.api.admin_routes.studio import create_studio_router
from src.services.model_gateway import ModelGateway


class StudioRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(create_studio_router(admin.verify_admin_token))
        self.app.state.model_gateway = ModelGateway()
        self.sessions = patch.object(admin, "active_admin_tokens", {"studio-session"})
        self.sessions.start()
        self.addCleanup(self.sessions.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://studio.example.test"
        )
        self.client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "studio-session")
        self.addAsyncCleanup(self.client.aclose)

    async def test_models_require_admin_session_and_do_not_accept_service_key(self):
        self.client.cookies.clear()
        with patch.object(routes, "create_chat_completion", AsyncMock()) as delegated:
            for headers in ({}, {"Authorization": "Bearer flow-service-key"}):
                self.assertEqual(
                    (
                        await self.client.get("/api/admin/studio/models", headers=headers)
                    ).status_code,
                    401,
                )
                response = await self.client.post(
                    "/api/admin/studio/chat/completions",
                    headers={**headers, "Origin": "https://studio.example.test"},
                    json={"model": "synthetic-image", "messages": []},
                )
                self.assertEqual(response.status_code, 401)
            delegated.assert_not_awaited()

    async def test_cookie_models_delegate_to_flow_catalog_without_returning_key(self):
        catalog = {"object": "list", "data": [{"id": "synthetic-image", "type": "image"}]}
        with patch.object(routes, "list_models", AsyncMock(return_value=catalog)) as delegated:
            response = await self.client.get("/api/admin/studio/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), catalog)
        delegated.assert_awaited_once()
        self.assertEqual(delegated.await_args.args[0], "admin-session")
        self.assertNotIn("api_key", response.text)

    async def test_cookie_write_requires_same_origin_and_delegates_sse_shape(self):
        payload = {
            "model": "synthetic-image",
            "messages": [{"role": "user", "content": "A synthetic anime scene"}],
            "stream": True,
        }
        with patch.object(routes, "create_chat_completion", AsyncMock()) as delegated:
            for origin in (
                None,
                "null",
                "https://evil.example.test",
                "https://studio.example.test.evil",
            ):
                response = await self.client.post(
                    "/api/admin/studio/chat/completions",
                    json=payload,
                    headers={"Origin": origin} if origin else {},
                )
                self.assertEqual(response.status_code, 403)
            delegated.assert_not_awaited()

        async def stream_response(*args):
            return StreamingResponse(
                iter(
                    [
                        'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                        "data: [DONE]\n\n",
                    ]
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        with patch.object(
            routes, "create_chat_completion", AsyncMock(side_effect=stream_response)
        ) as delegated:
            response = await self.client.post(
                "/api/admin/studio/chat/completions",
                json=payload,
                headers={"Origin": "https://studio.example.test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(
            response.text, 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        )
        delegated.assert_awaited_once()
        self.assertEqual(delegated.await_args.args[2], "admin-session")
        self.assertEqual(delegated.await_args.args[0].model, "synthetic-image")
        self.assertNotIn("flow-service-key", response.text)

    async def test_real_flow_handler_normalizes_options_and_preserves_streaming_headers(self):
        async def synthetic_stream(*args):
            yield 'data: {"choices":[{"delta":{"content":"Synthetic artwork"}}]}\n\n'
            yield "data: [DONE]\n\n"

        stream = Mock(side_effect=synthetic_stream)
        with patch.object(routes, "_iterate_openai_stream", stream):
            response = await self.client.post(
                "/api/admin/studio/chat/completions",
                headers={"Origin": "https://studio.example.test"},
                json={
                    "model": "gemini-3.1-flash-image",
                    "messages": [{"role": "user", "content": "Synthetic artwork"}],
                    "generationConfig": {"imageConfig": {"aspectRatio": "9:16", "imageSize": "1K"}},
                    "stream": True,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn("data: [DONE]\n\n", response.text)
        normalized, base_url = stream.call_args.args
        self.assertEqual(normalized.public_model, "gemini-3.1-flash-image")
        self.assertEqual(normalized.prompt, "Synthetic artwork")
        self.assertEqual(normalized.model, "gemini-3.1-flash-image-portrait")
        self.assertEqual(base_url, "https://studio.example.test")

    async def test_real_catalog_available_to_cookie_and_invalid_request_stays_validated(self):
        response = await self.client.get("/api/admin/studio/models")
        self.assertEqual(response.status_code, 200)
        self.assertIn("gemini-3.1-flash-image", {row["id"] for row in response.json()["data"]})
        response = await self.client.post(
            "/api/admin/studio/chat/completions",
            headers={"Origin": "https://studio.example.test"},
            json={"messages": []},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
