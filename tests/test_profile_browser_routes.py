"""Viewer access is bound to the administrator and current browser session."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.api.admin_routes.profile_browser import create_profile_browser_router


class ProfileBrowserRouteTests(unittest.TestCase):
    def setUp(self):
        self.assets = TemporaryDirectory()
        self.addCleanup(self.assets.cleanup)
        root = Path(self.assets.name)
        (root / "core").mkdir()
        (root / "core/rfb.js").write_text("export default class RFB {}", encoding="utf-8")
        self.viewer = SimpleNamespace(
            login=SimpleNamespace(
                start=AsyncMock(return_value={"success": True, "session_id": "pending"}),
                complete=AsyncMock(return_value={"success": True, "token_id": 4}),
                cancel=AsyncMock(return_value={"success": True, "status": "closed"}),
            ),
            open=AsyncMock(return_value={"token_id": 4, "status": "ready", "expires_at": 123}),
            close=AsyncMock(return_value={"token_id": 4, "status": "closed", "expires_at": None}),
            refresh=AsyncMock(return_value={"success": True, "at_expires": "2099-01-01T00:00:00Z"}),
            state=Mock(return_value={"token_id": 4, "status": "ready", "expires_at": 123}),
            owns=Mock(side_effect=lambda token_id, owner: token_id == 4 and owner == "admin-test"),
        )

        async def verify(request: Request):
            if request.cookies.get("admin_session") != "admin-test":
                raise HTTPException(status_code=401)
            return "admin-test"

        app = FastAPI()
        app.include_router(
            create_profile_browser_router(
                lambda: self.viewer,
                verify,
                lambda token: token == "admin-test",
                "admin_session",
                asset_root=root,
            )
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def login(self):
        self.client.cookies.set("admin_session", "admin-test")

    def test_requires_auth_and_same_origin_before_opening_profile(self):
        self.assertEqual(self.client.post("/api/tokens/4/browser").status_code, 401)
        self.login()
        for origin in (None, "http://untrusted.invalid"):
            headers = {"Origin": origin} if origin else {}
            self.assertEqual(
                self.client.post("/api/tokens/4/browser", headers=headers).status_code, 403
            )
        self.viewer.open.assert_not_awaited()
        response = self.client.post(
            "/api/tokens/4/browser", headers={"Origin": "http://testserver"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["viewer_url"], "/api/profile-browser/4")
        self.viewer.open.assert_awaited_once_with(4, "admin-test")

    def test_active_refresh_requires_origin_and_does_not_close_viewer(self):
        self.login()
        response = self.client.post("/api/tokens/4/browser/refresh")
        self.assertEqual(response.status_code, 403)
        self.viewer.refresh.assert_not_awaited()
        response = self.client.post(
            "/api/tokens/4/browser/refresh", headers={"Origin": "http://testserver"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.viewer.refresh.assert_awaited_once_with(4, "admin-test")
        self.viewer.close.assert_not_awaited()

    def test_direct_login_requires_auth_origin_and_bound_viewer_generation(self):
        payload = {"email": "new@example.invalid", "proxy_url": ""}
        self.assertEqual(self.client.post("/api/profile-login", json=payload).status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/profile-login", json=payload).status_code, 403)
        headers = {"Origin": "http://testserver"}
        response = self.client.post("/api/profile-login", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.viewer.login.start.assert_awaited_once_with("new@example.invalid", "", "admin-test")
        complete = "/api/profile-login/pending/complete"
        self.assertEqual(self.client.post(complete, json={}, headers=headers).status_code, 422)
        response = self.client.post(complete, json={"viewer_id": "current"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.viewer.login.complete.assert_awaited_once_with("pending", "admin-test", "current")
        self.client.delete("/api/profile-login/pending?viewer_id=current", headers=headers)
        self.viewer.login.cancel.assert_awaited_once_with("pending", "admin-test", "current")

    def test_direct_login_allows_anonymous_email_discovery_and_status_retry(self):
        self.viewer.login.status = Mock(return_value={"success": True, "status": "waiting"})
        self.viewer.login.retry = AsyncMock(return_value={"success": True, "status": "waiting"})
        headers = {"Origin": "http://testserver"}
        self.login()
        response = self.client.post("/api/profile-login", json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.viewer.login.start.assert_awaited_once_with("", "", "admin-test")
        response = self.client.get("/api/profile-login/pending?viewer_id=current", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.viewer.login.status.assert_called_once_with("pending", "admin-test", "current")
        self.assertEqual(
            self.client.post(
                "/api/profile-login/pending/retry", json={}, headers=headers
            ).status_code,
            422,
        )
        response = self.client.post(
            "/api/profile-login/pending/retry", json={"viewer_id": "current"}, headers=headers
        )
        self.assertEqual(response.status_code, 200)
        self.viewer.login.retry.assert_awaited_once_with("pending", "admin-test", "current")

    def test_direct_login_forwards_selected_proxy_without_starting_on_invalid_selection(self):
        headers = {"Origin": "http://testserver"}
        self.login()
        response = self.client.post(
            "/api/profile-login", json={"proxy_pool_id": 7}, headers=headers
        )
        self.assertEqual(response.status_code, 200)
        self.viewer.login.start.assert_awaited_once_with("", "", "admin-test", proxy_pool_id=7)
        self.viewer.login.start.reset_mock()

        for value in (0, -1, True, "7"):
            with self.subTest(value=value):
                response = self.client.post(
                    "/api/profile-login", json={"proxy_pool_id": value}, headers=headers
                )
                self.assertEqual(response.status_code, 422)
        self.viewer.login.start.assert_not_awaited()

    def test_direct_login_forwards_custom_residential_proxy(self):
        self.login()
        proxy = "socks5h://synthetic:secret@residential.invalid:1080"
        response = self.client.post(
            "/api/profile-login",
            json={"proxy_url": proxy},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(response.status_code, 200)
        self.viewer.login.start.assert_awaited_once_with("", proxy, "admin-test")

    def test_login_status_and_retry_check_auth_origin_and_preserve_viewer_errors(self):
        from src.services.browser.profile_viewer import ViewerError

        self.viewer.login.status = Mock(side_effect=ViewerError(403, "different owner"))
        self.viewer.login.retry = AsyncMock(side_effect=ViewerError(409, "stale viewer"))
        path = "/api/profile-login/pending"
        headers = {"Origin": "http://testserver"}
        self.assertEqual(self.client.get(path, headers=headers).status_code, 401)
        self.assertEqual(
            self.client.post(
                path + "/retry", json={"viewer_id": "current"}, headers=headers
            ).status_code,
            401,
        )
        self.login()
        self.assertEqual(self.client.get(path).status_code, 403)
        self.viewer.login.status.assert_called_once_with("pending", "admin-test", "")
        self.viewer.login.status.reset_mock()
        self.assertEqual(
            self.client.post(path + "/retry", json={"viewer_id": "current"}).status_code,
            403,
        )
        self.viewer.login.retry.assert_not_awaited()
        self.assertEqual(self.client.get(path, headers=headers).status_code, 403)
        response = self.client.post(path + "/retry", json={"viewer_id": "current"}, headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "stale viewer")

    def test_page_and_assets_are_protected_and_paths_cannot_escape(self):
        self.assertEqual(self.client.get("/api/profile-browser/4").status_code, 401)
        self.assertEqual(
            self.client.get("/api/profile-browser-assets/core/rfb.js").status_code, 401
        )
        self.login()
        self.assertEqual(self.client.get("/api/profile-browser/4").status_code, 200)
        self.assertEqual(self.client.get("/api/profile-browser/5").status_code, 404)
        self.assertEqual(
            self.client.get("/api/profile-browser-assets/core/rfb.js").status_code, 200
        )
        for path in ("secret", "core/%2e%2e%2fsecret", "core/%5csecret"):
            self.assertEqual(
                self.client.get("/api/profile-browser-assets/" + path).status_code, 404
            )

    def test_websocket_denies_wrong_origin_or_account_before_rfb_connection(self):
        self.login()
        for path, origin in (
            ("/api/profile-browser/4/ws", "http://untrusted.invalid"),
            ("/api/profile-browser/5/ws", "http://testserver"),
        ):
            with self.assertRaises(Exception) as failure:
                with self.client.websocket_connect(path, headers={"Origin": origin}):
                    pass
            self.assertEqual(getattr(failure.exception, "code", None), 1008)

    def test_viewer_generation_is_forwarded_and_stale_status_is_closed(self):
        self.login()
        self.viewer.state.return_value = {"token_id": 4, "status": "ready", "viewer_id": "new"}
        response = self.client.get("/api/tokens/4/browser?viewer_id=old")
        self.assertEqual(response.json()["status"], "closed")
        self.assertEqual(self.client.get("/api/profile-browser/4?viewer_id=old").status_code, 404)
        response = self.client.get("/api/profile-browser/4", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/api/profile-browser/4?viewer_id=new")
        self.client.delete(
            "/api/tokens/4/browser?viewer_id=old", headers={"Origin": "http://testserver"}
        )
        self.viewer.close.assert_awaited_once_with(4, "admin-test", viewer_id="old")
        self.client.post(
            "/api/tokens/4/browser/refresh?viewer_id=new", headers={"Origin": "http://testserver"}
        )
        self.viewer.refresh.assert_awaited_once_with(4, "admin-test", viewer_id="new")


if __name__ == "__main__":
    unittest.main()
