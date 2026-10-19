"""Console discovery and public docs use local synthetic state and preserve auth."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.api import admin
from src.main import app, config
from src.services.vids.dispatcher import VidsDispatchError, VidsProviderDispatcher


class VidsConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_returns_only_aggregate_availability_and_requires_login(self):
        lookup = AsyncMock(
            return_value=[
                {
                    "token_id": 4,
                    "email": "private@example.test",
                    "enabled": True,
                    "ready": True,
                    "image_enabled": True,
                    "video_enabled": False,
                },
                {
                    "token_id": 6,
                    "email": "another@example.test",
                    "enabled": False,
                    "ready": True,
                    "image_enabled": True,
                    "video_enabled": True,
                },
                {
                    "token_id": 9,
                    "email": "not-ready@example.test",
                    "enabled": True,
                    "ready": False,
                    "image_enabled": True,
                    "video_enabled": True,
                },
            ]
        )
        with (
            patch.object(app.state.vids_dispatcher, "public_accounts", lookup),
            patch.object(
                config,
                "get_raw_config",
                side_effect=lambda: {
                    "vids": {
                        "accounts": [{"token_id": row["token_id"]} for row in lookup.return_value]
                    }
                },
            ),
            patch.object(admin, "active_admin_tokens", {"synthetic-admin-session"}),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://console.example.test"
            ) as client:
                self.assertEqual((await client.get("/api/admin/vids/status")).status_code, 401)
                lookup.assert_not_awaited()
                client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")
                status = await client.get("/api/admin/vids/status")
                self.assertEqual(
                    status.json(),
                    {
                        "configured": True,
                        "configured_count": 3,
                        "runtime_ready": False,
                        "available": {"image": 1, "video": 0, "speech": 1},
                        "recoverable": {"image": 0, "video": 0, "speech": 0},
                        "quota": {
                            kind: {
                                "remaining": None,
                                "total": None,
                                "queried_at": None,
                                "covered": 0,
                                "eligible": 2 if kind == "image" else 1,
                                "stale": False,
                                "active_remaining": None,
                                "active_total": None,
                                "active_covered": 0,
                                "active_stale": False,
                                "active_queried_at": None,
                            }
                            for kind in ("image", "video")
                        },
                    },
                )
                self.assertEqual(status.headers["cache-control"], "no-store")
                self.assertNotIn("email", status.text)
                self.assertNotIn("token_id", status.text)
                lookup.return_value = []
                self.assertEqual(
                    (await client.get("/api/admin/vids/status")).json(),
                    {
                        "configured": False,
                        "configured_count": 0,
                        "runtime_ready": False,
                        "available": {"image": 0, "video": 0, "speech": 0},
                        "recoverable": {"image": 0, "video": 0, "speech": 0},
                        "quota": {
                            kind: {
                                "remaining": None,
                                "total": None,
                                "queried_at": None,
                                "covered": 0,
                                "eligible": 0,
                                "stale": False,
                                "active_remaining": None,
                                "active_total": None,
                                "active_covered": 0,
                                "active_stale": False,
                                "active_queried_at": None,
                            }
                            for kind in ("image", "video")
                        },
                    },
                )

    async def test_discovery_reads_only_bound_accounts_and_masks_private_state(self):
        token = SimpleNamespace(
            email="synthetic@example.test",
            is_active=False,
            image_enabled=True,
            video_enabled=False,
            st="private-st",
            cookies="private-cookie",
            proxy_url="private-proxy",
        )
        db = SimpleNamespace(
            get_token=AsyncMock(side_effect=lambda key: token if key == 4 else None)
        )
        dispatcher = VidsProviderDispatcher(db)
        dispatcher._accounts = {4: object(), 9: object()}
        with patch.object(
            dispatcher,
            "_account",
            side_effect=[object(), VidsDispatchError(503, "session_unavailable", "Unavailable")],
        ) as binding:
            rows = await dispatcher.public_accounts()
        self.assertEqual(
            rows,
            [
                {
                    "token_id": 4,
                    "email": token.email,
                    "enabled": False,
                    "image_enabled": True,
                    "video_enabled": False,
                    "ready": True,
                },
                {
                    "token_id": 9,
                    "email": "",
                    "enabled": False,
                    "image_enabled": False,
                    "video_enabled": False,
                    "ready": False,
                },
            ],
        )
        self.assertEqual([call.args[0] for call in db.get_token.await_args_list], [4, 9])
        self.assertEqual(binding.call_count, 2)
        self.assertNotIn("private-", json.dumps(rows))

    async def test_console_and_discovery_require_admin_session_before_lookup(self):
        lookup = AsyncMock(return_value=[])
        dispatcher = app.state.vids_dispatcher
        with (
            patch.object(dispatcher, "public_accounts", lookup),
            patch.object(admin, "active_admin_tokens", {"synthetic-admin-session"}),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://console.example.test",
                follow_redirects=False,
            ) as client:
                denied = await client.get("/vids")
                self.assertEqual((denied.status_code, denied.headers["location"]), (302, "/login"))
                self.assertEqual((await client.get("/api/admin/vids/accounts")).status_code, 401)
                self.assertEqual((await client.get("/static/vids.html")).status_code, 404)
                lookup.assert_not_awaited()
                client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-admin-session")
                page = await client.get("/vids?embedded=1")
                self.assertEqual(page.status_code, 200)
                self.assertIn("no-store", page.headers["cache-control"])
                legacy_page = await client.get("/legacy/vids?embedded=1")
                self.assertEqual(legacy_page.status_code, 200)
                self.assertIn("no-store", legacy_page.headers["cache-control"])
                self.assertIn("/static/js/vids.js", legacy_page.text)
                rows = await client.get("/api/admin/vids/accounts")
                self.assertEqual(rows.json(), {"accounts": []})
                self.assertEqual(rows.headers["cache-control"], "no-store")
                lookup.assert_awaited_once()

    async def test_vids_schema_is_public_and_contains_only_account_media_routes(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.example.test"
        ) as client:
            schema = (await client.get("/providers/vids/openapi.json")).json()
            self.assertTrue(schema["paths"])
            self.assertTrue(all(path.startswith("/providers/vids/v1/") for path in schema["paths"]))
            self.assertNotIn("/api/admin/vids/accounts", schema["paths"])
            for operations in schema["paths"].values():
                for operation in operations.values():
                    self.assertEqual(operation["security"], [{"HTTPBearer": []}])
            swagger = await client.get("/providers/vids/api-docs")
            self.assertIn("/providers/vids/openapi.json", swagger.text)
            guide = await client.get("/vids-api-guide")
            self.assertEqual(guide.status_code, 200)
            self.assertIn("no-store", guide.headers["cache-control"])
            guide_source = (
                Path(__file__).resolve().parents[1] / "static" / "vids-api-guide.html"
            ).read_text(encoding="utf-8")
            self.assertIn("Idempotency-Key", guide_source)
            self.assertEqual(
                (await client.get("/v1/openapi.json")).json()["info"]["title"],
                "lay4flow-remote Developer API",
            )


if __name__ == "__main__":
    unittest.main()
