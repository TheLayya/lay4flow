"""Exercise real frontend routes with isolated example settings and synthetic assets."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE_CHECK = r"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from fastapi.testclient import TestClient
from src.api import admin
import src.main as main

assert not Path("config/setting.toml").exists()
assert not Path("data/flow.db").exists()
client = TestClient(main.app)
protected = ("/manage", "/test", "/vids", "/vids-accounts", "/vids/accounts")
legacy = ("manage", "test", "vids", "vids-accounts", "api-guide", "vids-api-guide")
for path in protected + tuple("/legacy/" + page for page in legacy[:4]):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/login", path

public = ("/", "/login", "/api-guide", "/vids-api-guide", "/vids/api-guide")
for path in public:
    response = client.get(path)
    assert response.status_code == 200 and response.text == "synthetic-vue-entry", path
    assert "no-store" in response.headers["cache-control"]
for name in legacy[4:]:
    response = client.get("/legacy/" + name)
    assert response.status_code == 200 and response.text == "legacy-" + name
assert client.get("/static/app/assets/ui.js").status_code == 200
for path in ("/static/app/index.html", "/static/manage.html", "/static/vids.html", "/static/app/assets/../index.html", "/legacy/unknown"):
    assert client.get(path).status_code == 404, path

admin.active_admin_tokens.add("synthetic-session")
client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "synthetic-session")
admin.profile_browser_viewer = SimpleNamespace(
    owns=Mock(side_effect=lambda token_id, owner: token_id == 4 and owner == "synthetic-session"),
    state=Mock(return_value={"viewer_id": "synthetic-viewer"}),
)
profile = client.get("/api/profile-browser/4?viewer_id=synthetic-viewer")
assert profile.status_code == 200 and profile.text == "synthetic-vue-entry"
assert profile.headers["x-frame-options"] == "DENY"
assert "no-store" in profile.headers["cache-control"]
assert client.get("/api/profile-browser/5?viewer_id=synthetic-viewer").status_code == 404
assert client.get("/api/profile-browser/4?viewer_id=stale-viewer").status_code == 404
for path in protected:
    response = client.get(path)
    assert response.status_code == 200 and response.text == "synthetic-vue-entry", path
for page in legacy:
    response = client.get("/legacy/" + page)
    assert response.status_code == 200 and response.text == "legacy-" + page, page
    assert "no-store" in response.headers["cache-control"]

# An unbuilt checkout retains the original routes instead of failing at import.
(main.frontend_path / "index.html").unlink()
assert client.get("/api/profile-browser/4?viewer_id=synthetic-viewer").text == "legacy-profile-browser"
for path, expected in (("/login", "login"), ("/manage", "manage"), ("/test", "test"), ("/vids/accounts", "vids-accounts"), ("/vids/api-guide", "vids-api-guide")):
    assert client.get(path).text == "legacy-" + expected
client.cookies.set(admin.ADMIN_SESSION_COOKIE_NAME, "stale-session")
assert client.get("/manage", follow_redirects=False).status_code == 302
assert client.get("/legacy/api-guide").status_code == 200
assert not Path("data/flow.db").exists()
client.close()
print("Frontend routes, cookie guards, legacy fallbacks and asset boundaries passed")
"""


class FrontendRouteTests(unittest.TestCase):
    def test_spa_and_legacy_routes_preserve_sessions_and_asset_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="lay4flow-ui-routes-") as directory:
            target = Path(directory)
            shutil.copytree(
                ROOT / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
            (target / "config").mkdir()
            shutil.copy2(
                ROOT / "config/setting_example.toml", target / "config/setting_example.toml"
            )
            static = target / "static"
            (static / "js").mkdir(parents=True)
            (static / "app/assets").mkdir(parents=True)
            (static / "app/index.html").write_text("synthetic-vue-entry", encoding="utf-8")
            (static / "app/assets/ui.js").write_text(
                "export const fixture = true", encoding="utf-8"
            )
            for page in (
                "login",
                "manage",
                "test",
                "vids",
                "vids-accounts",
                "api-guide",
                "vids-api-guide",
                "profile-browser",
            ):
                (static / f"{page}.html").write_text("legacy-" + page, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", ROUTE_CHECK],
                cwd=target,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(target)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
