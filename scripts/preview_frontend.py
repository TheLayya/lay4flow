"""Preview the built UI with a temporary database and synthetic local credentials.

Runtime code/example settings are copied outside the checkout. Only the frontend
build is served live from this checkout, so rebuilding does not require a restart.
Outbound HTTP is disabled; no browser profile or generation provider is started.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW_RUNTIME = r"""
import socket
import sys
from pathlib import Path

def no_http(*args, **kwargs):
    raise RuntimeError("Frontend preview has no upstream HTTP access")

async def no_async_http(*args, **kwargs):
    return no_http()

original_connect = socket.socket.connect
def local_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1"):
        raise RuntimeError("Frontend preview only connects to loopback")
    return original_connect(sock, address)
socket.socket.connect = local_connect
original_connect_ex = socket.socket.connect_ex
def local_connect_ex(sock, address):
    if address[0] not in ("127.0.0.1", "::1"):
        raise RuntimeError("Frontend preview only connects to loopback")
    return original_connect_ex(sock, address)
socket.socket.connect_ex = local_connect_ex

import httpx
import urllib.request
from curl_cffi.requests import AsyncSession
httpx.Client.request = no_http
httpx.AsyncClient.request = no_async_http
AsyncSession.request = no_async_http
urllib.request.OpenerDirector.open = no_http

from src.core.config import config
config._config["global"].update(
    api_key="preview-api-key", admin_username="preview-admin", admin_password="preview-password"
)
config._config["captcha"]["captcha_method"] = "remote_browser"
config._config["browser_profile"]["enabled"] = False

import src.main as main
from fastapi.staticfiles import StaticFiles
main.frontend_path = Path(sys.argv[1]) / "static" / "app"
main.app.routes[:] = [route for route in main.app.routes if getattr(route, "path", "") != "/static/app/assets"]
main.app.mount(
    "/static/app/assets",
    StaticFiles(directory=str(main.frontend_path / "assets"), check_dir=False),
    name="preview-frontend-assets",
)

import uvicorn
print("Synthetic preview login: preview-admin / preview-password", flush=True)
uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[2]), access_log=False)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=38123)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lay4flow-frontend-preview-") as directory:
        target = Path(directory)
        for name in ("src", "static", "docs"):
            shutil.copytree(
                ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        (target / "config").mkdir()
        shutil.copy2(ROOT / "config/setting_example.toml", target / "config/setting_example.toml")
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(target)}
        result = subprocess.run(
            [sys.executable, "-c", PREVIEW_RUNTIME, str(ROOT), str(args.port)],
            cwd=target,
            env=environment,
        )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
