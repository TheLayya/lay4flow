import json
import unittest
from unittest.mock import patch

from src.services.flow_client import FlowClient


class _FakeResponse:
    status_code = 200
    headers = {}
    text = json.dumps({"ok": True})

    @staticmethod
    def json():
        return {"ok": True}


class _FakeAsyncSession:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _FakeResponse()

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeResponse()


class FlowClientDomainContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_and_text_requests_use_flow_but_keep_labs_auth_origin(self):
        client = FlowClient(proxy_manager=None)
        fake_session = _FakeAsyncSession()
        project_id = "project-123"

        with patch("src.services.flow_client.AsyncSession", lambda *args, **kwargs: fake_session):
            await client._make_request(
                method="POST",
                url=f"{client.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages",
                json_data={"clientContext": {"projectId": project_id}},
                use_at=True,
                at_token="at-token",
                timeout=1,
            )
            await client._make_text_request(
                method="GET",
                url=f"{client.labs_base_url}/auth/session",
                use_st=True,
                st_token="st-token",
                timeout=1,
            )

        flow_headers = fake_session.calls[0][2]["headers"]
        labs_headers = fake_session.calls[1][2]["headers"]
        self.assertEqual(flow_headers["Origin"], client.flow_base_url)
        self.assertEqual(flow_headers["Referer"], f"{client.flow_base_url}/project/{project_id}")
        self.assertEqual(labs_headers["Origin"], "https://labs.google")
        self.assertNotEqual(labs_headers["Origin"], client.flow_base_url)
        self.assertEqual(labs_headers["Cookie"], "__Secure-next-auth.session-token=st-token")

    def test_request_context_origin_preserves_labs_auth_host(self):
        client = FlowClient(proxy_manager=None)
        self.assertEqual(
            client._request_context_origin("https://labs.google/fx/api/auth/session"),
            "https://labs.google",
        )
        self.assertEqual(
            client._request_context_origin("https://aisandbox-pa.googleapis.com/v1/credits"),
            client.flow_base_url,
        )


if __name__ == "__main__":
    unittest.main()
