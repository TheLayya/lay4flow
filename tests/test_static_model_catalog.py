"""Generated UI capabilities stay in sync and only scripts are public assets."""

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.generate_model_fallback import OUTPUT, render_catalog
from src.core.public_models import PUBLIC_MODELS


class StaticModelCatalogTests(unittest.TestCase):
    def test_generated_catalog_matches_public_server_contract(self):
        expected = render_catalog()
        self.assertEqual(OUTPUT.read_text(encoding="utf-8"), expected)
        payload = json.loads(expected.split("const FALLBACK_MODELS = ", 1)[1].rstrip(";\n"))
        self.assertEqual(payload["gemini-3.1-flash-image"]["displayName"], "Nano Banana 2.1")
        self.assertEqual(payload["gemini-3.0-pro-image"]["displayName"], "Nano Banana Pro")
        self.assertEqual(
            set(payload), {key for key, value in PUBLIC_MODELS.items() if not value.get("legacy")}
        )
        for key, spec in payload.items():
            self.assertEqual(spec["capabilities"], PUBLIC_MODELS[key]["capabilities"])
            self.assertEqual(spec["defaults"], PUBLIC_MODELS[key]["defaults"])
            self.assertTrue(spec["usage"])
            self.assertTrue(spec["recommended_endpoint"].startswith("/v1/"))
        html = (OUTPUT.parents[1] / "test.html").read_text(encoding="utf-8")
        self.assertNotIn("const FALLBACK_MODELS", html)
        self.assertLess(html.index("/static/js/model_catalog.js"), html.index("/static/js/test.js"))
        self.assertIn("?v=20261003-09", html)

    def test_script_assets_load_without_exposing_protected_pages(self):
        # Importing the app wires route dependencies; restore them for other tests.
        from src.api import admin, routes

        with (
            patch.object(routes, "generation_handler", routes.generation_handler),
            patch.multiple(
                admin,
                db=admin.db,
                token_manager=admin.token_manager,
                proxy_manager=admin.proxy_manager,
                concurrency_manager=admin.concurrency_manager,
            ),
        ):
            from src.main import app

        with patch.object(admin, "active_admin_tokens", set()):
            client = TestClient(app)
            self.addCleanup(client.close)
            for filename in ("model_catalog.js", "test.js", "manage.js", "manage_theme.js"):
                response = client.get(f"/static/js/{filename}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("javascript", response.headers["content-type"])
            self.assertEqual(client.get("/test", follow_redirects=False).status_code, 302)
            self.assertEqual(client.get("/static/test.html").status_code, 404)
            self.assertEqual(client.get("/static/js/%2e%2e/manage.html").status_code, 404)
