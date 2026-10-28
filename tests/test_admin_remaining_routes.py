"""Remaining admin groups retain route order and live auth/transport dependencies."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Response

from src.api import admin


class AdminRemainingRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_route_registration_keeps_management_precedence_without_removed_backends(self):
        routes = [route for route in admin.router.routes if hasattr(route, "methods")]
        names = [route.endpoint.__name__ for route in routes]
        ordered = [name for name in names if name in admin._ROUTE_ORDER]
        self.assertEqual(ordered, list(admin._ROUTE_ORDER))
        self.assertEqual(
            {route.path for route in routes if route.path.startswith("/api/plugin")},
            {
                "/api/plugin/import/start",
                "/api/plugin/import/complete",
                "/api/plugin/import/extension.zip",
            },
        )
        self.assertFalse(any(route.path == "/api/captcha/runtime-status" for route in routes))
        self.assertLess(
            names.index("batch_proxy_pool_action"), names.index("toggle_proxy_pool_entry")
        )

    async def test_auth_aliases_follow_replaced_facade_callbacks(self):
        request = admin.LoginRequest(username="synthetic", password="synthetic")
        response = Response()
        login = AsyncMock(return_value={"success": True})
        logout = AsyncMock(return_value={"success": True})
        password = AsyncMock(return_value={"success": True})
        with patch.multiple(
            admin, admin_login=login, admin_logout=logout, change_password=password
        ):
            await admin.login(request, response)
            await admin.logout(response, "session")
            await admin.update_admin_password(request, "session")
        login.assert_awaited_once_with(request, response)
        logout.assert_awaited_once_with(response, "session")
        password.assert_awaited_once_with(request, "session")

    async def test_password_change_preserves_reload_and_session_invalidation(self):
        order = []
        db = SimpleNamespace(
            get_admin_config=AsyncMock(return_value=SimpleNamespace(username="admin"))
        )
        db.update_admin_config = AsyncMock(side_effect=lambda **values: order.append("persist"))
        db.reload_config_to_memory = AsyncMock(side_effect=lambda: order.append("reload"))
        sessions = {"existing-session"}
        with (
            patch.multiple(admin, db=db, active_admin_tokens=sessions),
            patch.object(admin.AuthManager, "verify_admin", return_value=True),
        ):
            await admin.change_password(
                admin.ChangePasswordRequest(old_password="old", new_password="new"),
                "existing-session",
            )
        self.assertEqual(order, ["persist", "reload"])
        self.assertEqual(sessions, set())

    async def test_proxy_test_uses_replaced_session_and_proxy_manager(self):
        response = SimpleNamespace(status_code=204, url="https://destination.invalid")
        session = SimpleNamespace(get=AsyncMock(return_value=response))
        context_type = type(
            "SessionContext",
            (),
            {
                "__aenter__": AsyncMock(return_value=session),
                "__aexit__": AsyncMock(return_value=False),
            },
        )
        context = context_type()
        manager = SimpleNamespace(normalize_proxy_url=Mock(return_value="http://proxy.invalid:80"))
        with patch.multiple(admin, proxy_manager=manager, AsyncSession=Mock(return_value=context)):
            result = await admin.test_proxy_connectivity(
                admin.ProxyTestRequest(proxy_url="proxy.invalid:80", timeout_seconds=1), "session"
            )
        self.assertTrue(result["success"])
        self.assertEqual(session.get.await_args.kwargs["timeout"], 5)
        self.assertEqual(
            session.get.await_args.kwargs["proxies"],
            {"http": "http://proxy.invalid:80", "https": "http://proxy.invalid:80"},
        )

    async def test_remote_score_transport_uses_live_configuration_and_http_callback(self):
        request = AsyncMock(return_value=(200, {"success": True}, "{}"))
        with patch.multiple(
            admin,
            _get_remote_browser_client_config=Mock(
                return_value=("https://solver.invalid", "synthetic-key", 45)
            ),
            _sync_json_http_request=request,
        ):
            result = await admin._score_test_with_remote_browser_service(
                "https://page.invalid", "site-key", "https://verify.invalid", "ACTION"
            )
        self.assertTrue(result["success"])
        self.assertEqual(
            request.await_args.kwargs["url"], "https://solver.invalid/api/v1/custom-score"
        )
        self.assertEqual(
            request.await_args.kwargs["headers"], {"Authorization": "Bearer synthetic-key"}
        )
        self.assertEqual(request.await_args.kwargs["payload"]["action"], "ACTION")

    async def test_remote_score_diagnostic_uses_worker_page_verification(self):
        score = AsyncMock(
            return_value={
                "success": True,
                "token": "synthetic-token",
                "verify_result": {"success": True, "score": 0.9},
                "verify_http_status": 200,
                "verify_mode": "remote_browser_page",
                "fingerprint": {"user_agent": "worker-agent"},
            }
        )
        with patch.object(admin, "_score_test_with_remote_browser_service", score):
            result = await admin.test_captcha_score(admin.CaptchaScoreTestRequest(), "session")
        self.assertTrue(result["success"])
        self.assertEqual(result["score"], 0.9)
        self.assertEqual(result["verify_request_meta"]["mode"], "remote_browser_page")
        self.assertEqual(score.await_count, 1)


if __name__ == "__main__":
    unittest.main()
