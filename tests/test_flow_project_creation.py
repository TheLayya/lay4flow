"""Regression checks for account creation after Labs project RPC retirement."""

import unittest
from unittest.mock import AsyncMock

from src.services.flow_client import FlowClient


class FlowProjectCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_pool_can_create_multiple_projects_with_at(self):
        client = FlowClient(proxy_manager=None)
        client.st_to_at = AsyncMock(return_value={"access_token": "test-access"})
        calls = []

        async def upstream(**request):
            # Model the retired service separately: the old route must fail.
            if "/trpc/" in request["url"]:
                raise RuntimeError("HTTP Error 404: Flow RPCs have been deprecated and disabled")
            self.assertTrue(request["url"].endswith("/v1/projects/*"))
            self.assertTrue(request["use_at"])
            self.assertEqual(request["at_token"], "test-access")
            self.assertNotIn("st_token", request)
            self.assertEqual(request["headers"]["Origin"], "https://flow.google.com")
            calls.append(request)
            return {"projectId": f"project-{len(calls)}", **request["json_data"]}

        client._make_request = upstream
        for number in range(1, 5):
            title = f"Account P{number}"
            self.assertEqual(
                await client.create_project("test-session", title), f"project-{number}"
            )
            self.assertEqual(calls[-1]["json_data"], {"projectInfo": {"projectTitle": title}})

    async def test_invalid_session_does_not_create_a_project(self):
        client = FlowClient(proxy_manager=None)
        client.st_to_at = AsyncMock(return_value={})
        client._make_request = AsyncMock()
        with self.assertRaisesRegex(ValueError, "access token"):
            await client.create_project("expired-session", "Title")
        client._make_request.assert_not_awaited()

    async def test_empty_success_response_is_not_a_created_project(self):
        client = FlowClient(proxy_manager=None)
        client.st_to_at = AsyncMock(return_value={"access_token": "test-access"})
        client._make_request = AsyncMock(return_value={})
        with self.assertRaisesRegex(Exception, "missing projectId"):
            await client.create_project("test-session", "Title")


if __name__ == "__main__":
    unittest.main()
