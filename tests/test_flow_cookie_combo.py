"""Saved Flow credentials are joined locally, with scope/expiry and admin checks."""

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.admin_routes.tokens import create_token_router
from src.services.flow.google_cookies import extract_flow_cookie_combo


class FlowCookieComboTests(unittest.TestCase):
    def test_legacy_headers_maps_and_json_exports_keep_order_and_values(self):
        cookies = {"SID": "sid=value", "OSID": "osid%2Fvalue", "__Secure-1PSID": "psid"}
        for raw in (
            cookies,
            {"refreshCookie": cookies},
            {"cookies": cookies},
            "; ".join(f"{name}={value}" for name, value in cookies.items()),
            json.dumps([{"name": name, "value": value} for name, value in cookies.items()]),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    extract_flow_cookie_combo(raw),
                    {
                        "cookie": "psid::sid=value::osid%2Fvalue",
                        "cookie_names": ["__Secure-1PSID", "SID", "OSID"],
                        "missing": [],
                        "has_1psidts": False,
                    },
                )
        cookies["__Secure-1PSIDTS"] = "timestamp"
        result = extract_flow_cookie_combo(cookies)
        self.assertEqual(result["cookie"], "psid::sid=value::osid%2Fvalue::timestamp")
        self.assertTrue(result["has_1psidts"])

    def test_scopes_choose_flow_over_parent_and_ignore_unrelated_or_expired_values(self):
        rows = [
            {"name": "__Secure-1PSID", "value": "psid", "domain": ".google.com", "secure": True},
            {"name": "SID", "value": "parent", "domain": ".google.com"},
            {"name": "SID", "value": "flow", "domain": "flow.google.com"},
            {"name": "SID", "value": "account", "domain": "accounts.google.com"},
            {"name": "SID", "value": "path", "domain": "flow.google.com", "path": "/other"},
            {"name": "OSID", "value": "osid", "domain": "flow.google.com"},
            {"name": "OSID", "value": "foreign", "domain": "example.invalid"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "expired",
                "domain": ".google.com",
                "secure": True,
                "expires": time.time() - 60,
            },
        ]
        for raw in (rows, json.dumps({"cookies": rows}), list(reversed(rows))):
            result = extract_flow_cookie_combo(raw)
            self.assertEqual(result["cookie"], "psid::flow::osid")
            self.assertFalse(result["has_1psidts"])

    def test_missing_required_values_never_return_partial_credentials(self):
        for raw in (None, "", "not-a-cookie", [], {"SID": "synthetic-secret"}):
            result = extract_flow_cookie_combo(raw)
            self.assertTrue(result["missing"])
            self.assertEqual(result["cookie"], "")
            self.assertNotIn("synthetic-secret", json.dumps(result))
        for row in (
            {"name": "__Secure-1PSID", "value": "bad", "domain": ".google.com", "secure": False},
            {"name": "__Secure-1PSID", "value": "bad", "domain": "google.com", "secure": True},
            {
                "name": "__Secure-1PSID",
                "value": "bad",
                "domain": ".google.com",
                "secure": True,
                "partitioned": True,
            },
        ):
            self.assertIn("__Secure-1PSID", extract_flow_cookie_combo([row])["missing"])


class FlowCookieComboRouteTests(unittest.TestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            id=7,
            email="test@example.invalid",
            google_cookies="SID=sid; OSID=osid; __Secure-1PSID=psid",
        )
        self.manager = SimpleNamespace(get_token=AsyncMock(return_value=self.account))
        self.authenticated = False

        async def verify():
            if not self.authenticated:
                raise HTTPException(401, detail="Login required")
            return "synthetic-admin"

        router, _ = create_token_router(lambda: None, lambda: self.manager, lambda: None, verify)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_admin_auth_precedes_read_and_success_has_no_store(self):
        path = "/api/tokens/7/flow-cookie-combo"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.manager.get_token.assert_not_awaited()
        self.authenticated = True
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.json()["cookie"], "psid::sid::osid")
        self.assertEqual(response.json()["email"], "test@example.invalid")
        self.manager.get_token.assert_awaited_once_with(7)

    def test_missing_cookie_error_only_names_missing_fields(self):
        self.authenticated = True
        self.account.google_cookies = "SID=synthetic-secret"
        response = self.client.get("/api/tokens/7/flow-cookie-combo")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["detail"]["missing"], ["__Secure-1PSID", "OSID"])
        self.assertNotIn("synthetic-secret", response.text)

    def test_deleted_account_is_reported_without_refresh_or_browser_operations(self):
        self.authenticated = True
        self.manager.get_token.return_value = None
        response = self.client.get("/api/tokens/7/flow-cookie-combo")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
