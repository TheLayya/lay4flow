"""Offline authentication, routing and retry contracts for extension login."""

import asyncio
import io
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.api.admin_routes.extension_login import (
    ExtensionLoginSessions,
    create_extension_login_router,
)


class ExtensionLoginRouteTests(unittest.TestCase):
    def setUp(self):
        self.owner = "synthetic-admin"
        self.sessions = ExtensionLoginSessions()
        self.importer = AsyncMock(
            return_value={"success": True, "email": "member@example.test", "token_id": 7}
        )

        async def verify(request: Request):
            if request.cookies.get("admin-session") != self.owner:
                raise HTTPException(401)
            return self.owner

        app = FastAPI()
        app.include_router(
            create_extension_login_router(
                lambda: SimpleNamespace(db=object()),
                verify,
                sessions=self.sessions,
                import_login=self.importer,
            )
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.client.cookies.set("admin-session", self.owner)
        self.headers = {"Origin": "http://testserver"}

    def start(self):
        response = self.client.post("/api/plugin/import/start", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def complete(self, receipt, **overrides):
        body = {
            "session_id": receipt["session_id"],
            "tab_id": "42",
            "cookie_store_id": "0",
            "google_cookies": "synthetic-cookie-json",
            "browser_identity": {"user_agent": "synthetic"},
            **overrides,
        }
        return self.client.post(
            "/api/plugin/import/complete",
            json=body,
            headers={
                **self.headers,
                "Authorization": f"Bearer import:{receipt['session_id']}.{receipt['nonce']}",
            },
        )

    def test_admin_and_same_origin_are_required(self):
        self.client.cookies.clear()
        for path in ("start", "complete"):
            self.assertEqual(
                self.client.post(f"/api/plugin/import/{path}", json={}).status_code, 401
            )
        self.assertEqual(self.client.get("/api/plugin/import/extension.zip").status_code, 401)
        self.client.cookies.set("admin-session", self.owner)
        for headers in ({}, {"Origin": "https://untrusted.invalid"}):
            self.assertEqual(
                self.client.post("/api/plugin/import/start", json={}, headers=headers).status_code,
                403,
            )

    def test_start_needs_no_server_proxy_and_completion_ignores_browser_routing_inputs(self):
        receipt = self.start()
        self.assertNotIn("proxy_pool_id", self.sessions.items[receipt["session_id"]])
        self.assertNotIn("proxy_url", self.sessions.items[receipt["session_id"]])
        response = self.complete(receipt, proxy_url="http://attacker.invalid:80", proxy_pool_id=99)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "member@example.test")
        args = self.importer.await_args
        self.assertEqual(args.kwargs, {})
        self.assertEqual(set(args.args[0]), {"google_cookies", "browser_identity"})
        self.assertNotIn("synthetic-cookie", response.text)
        self.assertNotIn("secret", response.text)

    def test_retry_is_idempotent_and_bound_to_same_browser_and_administrator(self):
        receipt = self.start()
        self.assertEqual(self.complete(receipt).status_code, 200)
        self.assertTrue(self.complete(receipt).json()["idempotent"])
        self.importer.assert_awaited_once()
        self.assertEqual(self.complete(receipt, tab_id="other").status_code, 403)
        self.owner = "different-admin"
        self.client.cookies.set("admin-session", self.owner)
        self.assertEqual(self.complete(receipt).status_code, 403)

    def test_bad_nonce_expired_session_and_bad_binding_never_import(self):
        receipt = self.start()
        self.assertEqual(self.complete({**receipt, "nonce": "wrong"}).status_code, 403)
        self.assertEqual(self.complete(receipt, tab_id=None).status_code, 400)
        self.assertEqual(self.complete(receipt, nonce="must-not-be-body").status_code, 400)
        self.sessions.items[receipt["session_id"]]["expires_at"] = 0
        self.assertEqual(self.complete(receipt).status_code, 410)
        self.importer.assert_not_awaited()

    def test_failure_is_private_and_cannot_repeat_profile_mutations(self):
        receipt = self.start()
        self.importer.side_effect = RuntimeError("synthetic-private-cookie-content")
        response = self.complete(receipt)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("synthetic-private", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.complete(receipt).status_code, 422)
        self.importer.assert_awaited_once()

    def test_download_is_current_origin_only_and_contains_public_files(self):
        response = self.client.get("/api/plugin/import/extension.zip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
            self.assertTrue(
                {"manifest.json", "background.js", "content.js", "LICENSE", "NOTICE"} <= names
            )
            manifest = archive.read("manifest.json").decode()
            self.assertIn("http://testserver/*", manifest)
            self.assertNotIn("__LAY4FLOW_MANAGEMENT_MATCH__", manifest)
            self.assertFalse(any("private" in name or "cookies" in name for name in names))

    def test_invalid_and_oversize_body_do_not_echo_credentials(self):
        response = self.client.post(
            "/api/plugin/import/start",
            content='["synthetic-private"]',
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("synthetic-private", response.text)
        receipt = self.start()
        response = self.complete(receipt, google_cookies="x" * (1024 * 1024))
        self.assertEqual(response.status_code, 413)
        self.importer.assert_not_awaited()

    def test_session_store_is_bounded_and_keeps_only_nonce_and_owner_hashes(self):
        sessions = ExtensionLoginSessions(max_sessions=1)
        session_id, nonce, _ = sessions.issue("admin", "http://testserver")
        self.assertNotEqual(sessions.items[session_id]["nonce"], nonce)
        self.assertNotEqual(sessions.items[session_id]["owner"], "admin")
        with self.assertRaises(HTTPException) as caught:
            sessions.issue("admin", "http://testserver")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIsInstance(sessions.items[session_id]["lock"], asyncio.Lock)


if __name__ == "__main__":
    unittest.main()
