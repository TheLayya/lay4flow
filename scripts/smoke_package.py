"""Start clean runtime copies, verify HTTP/auth/assets, then restart with its database.

Uses example configuration and synthetic credentials in a temporary directory.
Known HTTP clients and nonlocal sockets are guarded; CI also disables container
networking. No browser or generation job is started.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RUNTIME_CHECK = r"""
import contextlib
import io
import json
import re
import socket
import sys
from pathlib import Path

from fastapi.testclient import TestClient

original_connect = socket.socket.connect
network_attempts = []
def offline_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1"):
        network_attempts.append("socket")
        raise AssertionError("Runtime smoke attempted an external connection")
    return original_connect(sock, address)
socket.socket.connect = offline_connect
original_connect_ex = socket.socket.connect_ex
def offline_connect_ex(sock, address):
    if address[0] not in ("127.0.0.1", "::1"):
        network_attempts.append("socket-connect-ex")
        raise AssertionError("Runtime smoke attempted an external connection")
    return original_connect_ex(sock, address)
socket.socket.connect_ex = offline_connect_ex
from curl_cffi.requests import AsyncSession
async def no_curl_request(*args, **kwargs):
    network_attempts.append("curl")
    raise AssertionError("Runtime smoke attempted an upstream HTTP request")
AsyncSession.request = no_curl_request
import httpx
httpx.AsyncClient.request = no_curl_request
original_httpx_request = httpx.Client.request
def no_sync_request(*args, **kwargs):
    network_attempts.append("http-sync")
    raise AssertionError("Runtime smoke attempted an upstream HTTP request")
def local_test_client_only(client, *args, **kwargs):
    if isinstance(client, TestClient):
        return original_httpx_request(client, *args, **kwargs)
    return no_sync_request(client, *args, **kwargs)
httpx.Client.request = local_test_client_only
import urllib.request
urllib.request.OpenerDirector.open = no_sync_request

from src.core.config import config
config._config["global"].update(api_key="smoke-api-key")
assert config.admin_username == "admin" and config.admin_password == "admin"
config._config["captcha"]["captcha_method"] = "remote_browser"
from src.main import API_VERSION, app
from src.core.public_models import PUBLIC_MODELS

assert not Path("config/setting.toml").exists()
iteration = int(sys.argv[1])
assert Path("data/flow.db").exists() == bool(iteration)
with contextlib.redirect_stdout(io.StringIO()), TestClient(app) as client:
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["backend_running"]
    for path in ("/manage", "/test", "/vids", "/vids-accounts", "/vids/accounts", "/legacy/manage", "/legacy/test", "/legacy/vids", "/legacy/vids-accounts"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302 and response.headers["location"] == "/login", path
    assert client.get("/v1/models").status_code in (401, 403)
    vids_path = "/providers/vids/accounts/1/v1/models"
    assert client.get(vids_path).status_code == 401
    vids_missing = client.get(vids_path, headers={"Authorization":"Bearer smoke-api-key"})
    assert vids_missing.status_code == 404 and vids_missing.json()["error"]["code"] == "account_not_found"
    pooled_path = "/providers/vids/v1/models"
    assert client.get(pooled_path).status_code == 401
    pooled = client.get(pooled_path, headers={"Authorization":"Bearer smoke-api-key"})
    assert pooled.status_code == 200 and len(pooled.json()["data"]) == 3
    assert not app.state.vids_runtime._gateways and not app.state.vids_dispatcher._accounts
    if iteration:
        assert client.post("/api/login", json={"username":"admin", "password":"admin"}).status_code == 401
    login = client.post("/api/login", json={"username":"admin", "password":"admin" if iteration == 0 else "smoke-updated-password"})
    assert login.status_code == 200 and login.json()["success"]
    if iteration == 0:
        changed = client.post("/api/admin/change-password", json={"old_password":"admin", "new_password":"smoke-updated-password"})
        assert changed.status_code == 200 and changed.json()["success"]
        expired = client.get("/api/tokens")
        assert expired.status_code == 401 and expired.headers["X-Admin-Session"] == "expired"
        assert client.get("/api/tokens").status_code == 401
        assert client.post("/api/login", json={"username":"admin", "password":"admin"}).status_code == 401
        assert client.post("/api/login", json={"username":"admin", "password":"smoke-updated-password"}).status_code == 200
    for path in ("/manage", "/test", "/vids", "/vids-accounts", "/vids/accounts", "/api/tokens", "/api/stats", "/api/logs", "/legacy/manage", "/legacy/test", "/legacy/vids", "/legacy/vids-accounts", "/legacy/api-guide", "/legacy/vids-api-guide"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
    static_asset_count = 0
    for name in ("model_catalog.js", "media_skus.js", "test.js", "manage_theme.js", "manage.js", "api-guide.js", "vids.js", "vids-accounts.js"):
        asset = client.get("/static/js/" + name)
        assert asset.status_code == 200 and "javascript" in asset.headers["content-type"]
        static_asset_count += 1
    assert client.get("/static/manage.html").status_code == 404
    assert client.get("/static/vids.html").status_code == 404
    assert client.get("/static/app/index.html").status_code == 404
    app_index = Path("static/app/index.html")
    if app_index.is_file():
        frontend_entry = client.get("/manage")
        assert frontend_entry.text == app_index.read_text(encoding="utf-8")
        assert "no-store" in frontend_entry.headers["cache-control"]
        asset_paths = re.findall(r'(?:src|href)="(/static/app/assets/[^"?]+)', frontend_entry.text)
        assert asset_paths, "Built frontend entry does not reference local assets"
        for asset_path in asset_paths:
            response = client.get(asset_path)
            assert response.status_code == 200, asset_path
        for asset in Path("static/app/assets").rglob("*"):
            if asset.is_file():
                assert client.get("/static/app/assets/" + asset.relative_to(Path("static/app/assets")).as_posix()).status_code == 200
                static_asset_count += 1
    assert client.get("/api/admin/vids/accounts").json() == {"accounts": []}
    vids_status = client.get("/api/admin/vids/status").json()
    assert vids_status["configured"] is False and vids_status["configured_count"] == 0
    assert vids_status["available"] == {"image": 0, "video": 0, "speech": 0}
    assert vids_status["quota"]["image"]["remaining"] is None
    assert client.get("/api/admin/vids/accounts/detail").json()["accounts"] == []
    assert client.get("/api/admin/vids/v1/models").status_code == 200
    assert client.get("/api/admin/vids/v1/videos").json()["data"] == []
    assert client.get("/vids-api-guide").status_code == 200
    assert client.get("/providers/vids/api-docs").status_code == 200
    vids_paths = client.get("/providers/vids/openapi.json").json()["paths"]
    assert vids_paths and all(path.startswith("/providers/vids/v1/") for path in vids_paths)
    catalog = client.get("/v1/models", headers={"Authorization":"Bearer smoke-api-key"})
    assert catalog.status_code == 200
    models = {entry["id"]:entry for entry in catalog.json()["data"]}
    assert set(models) == {key for key, value in PUBLIC_MODELS.items() if not value.get("legacy")}
    assert not ({"veo_3_1_interpolation_lite", "veo_3_1_remix", "veo_3_1_extend"} & set(models))
    from src.core.media_skus import MEDIA_SKUS
    assert client.get("/compat/v1/models").status_code == 401
    sku_catalog = client.get("/compat/v1/models", headers={"Authorization":"Bearer smoke-api-key"})
    assert sku_catalog.status_code == 200
    sku_names = {entry["id"] for entry in sku_catalog.json()["data"]}
    assert sku_names == set(MEDIA_SKUS) and not sku_names.intersection(models)
    invalid_sku = client.post("/compat/v1/chat/completions", headers={"Authorization":"Bearer smoke-api-key"}, json={"model":"unknown-sku","messages":[{"role":"user","content":"offline"}]})
    assert invalid_sku.status_code == 400 and "usage" not in invalid_sku.json()
    compatibility_schema = client.get("/compat/v1/openapi.json").json()
    assert "/compat/v1/chat/completions" in compatibility_schema["paths"]
    assert {"/compat/v1/videos", "/compat/v1/videos/{video_id}", "/compat/v1/videos/{video_id}/content"} <= set(compatibility_schema["paths"])
    compat_jobs = client.get("/compat/v1/videos", headers={"Authorization":"Bearer smoke-api-key"})
    assert compat_jobs.status_code == 200 and compat_jobs.json()["data"] == []
    assert client.get("/compat/api-docs").status_code == 200
    assert client.get("/api-docs").status_code == 200
    assert client.get("/api-guide").status_code == 200
    public_schema = client.get("/v1/openapi.json").json()
    assert public_schema["info"]["version"] == API_VERSION
    paths = public_schema["paths"]
    assert all(path.startswith("/v1/") for path in paths)
    assert {"/v1/images/generations", "/v1/images/edits", "/v1/videos", "/v1/videos/{video_id}/content"} <= set(paths)
    unsupported_prefixes = ("/v1/audio", "/v1/embeddings", "/v1/files", "/v1/batches", "/v1/realtime", "/v1/assistants")
    assert not any(path.startswith(unsupported_prefixes) for path in paths)
    jobs = client.get("/v1/videos", headers={"Authorization":"Bearer smoke-api-key"})
    assert jobs.status_code == 200 and jobs.json()["data"] == []
    resolutions = {r for c in models["omni_1.1_flash"]["capabilities"]["combinations"] for r in c["resolutions"]}
    assert {"360p", "720p", "1080p", "4k"} <= resolutions
    if iteration == 0:
        updated = client.post("/api/cache/config", json={"enabled":False, "timeout":321})
        assert updated.status_code == 200
    assert client.get("/api/cache/config").json()["config"]["timeout"] == 321
    assert client.post("/api/logout").json()["success"]
    assert client.get("/api/tokens").status_code == 401
    for path in ("/", "/login", "/api-guide", "/vids-api-guide", "/vids/api-guide", "/legacy/api-guide", "/legacy/vids-api-guide"):
        assert client.get(path).status_code == 200, path
assert Path("data/flow.db").is_file()
assert not network_attempts, "A background task attempted an external request"
print(json.dumps({"phase":"first_start" if iteration == 0 else "cold_restart", "auth":True,"models":len(models),"vids_accounts":0,"static_assets":static_asset_count,"guarded_network_attempts":len(network_attempts)}))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lay4flow-runtime-smoke-") as directory:
        target = Path(directory)
        for name in ("src", "static", "docs"):
            shutil.copytree(
                ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        (target / "config").mkdir()
        (target / "docs").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "docs/API.md", target / "docs/API.md")
        shutil.copy2(ROOT / "config/setting_example.toml", target / "config/setting_example.toml")
        shutil.copy2(ROOT / "main.py", target / "main.py")
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(target)}
        for iteration in range(2):
            result = subprocess.run(
                [sys.executable, "-c", RUNTIME_CHECK, str(iteration)],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            if result.returncode:
                print(result.stderr, file=sys.stderr)
                return result.returncode
            print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
