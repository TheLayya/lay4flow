"""Run the FastAPI backend from the bundled Windows Python runtime."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and (key.startswith("LAY4FLOW_") or key not in os.environ):
            os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--log-file", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    log_path = Path(args.log_file).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # ShellExecute launches the background interpreter without inheriting the
    # desktop launcher's pipe handles. Keep diagnostics in application data.
    sys.stdout = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stderr = log_path.with_suffix(log_path.suffix + ".error").open(
        "a", encoding="utf-8", buffering=1
    )
    os.chdir(root)
    load_env(root / ".env")
    # Native mode is loopback-only. The desktop shell is the only local client.
    os.environ["LAY4FLOW_NATIVE_RUNTIME"] = "1"
    os.environ.setdefault("LAY4FLOW_PROFILE_VIEWER_ENABLED", "1")
    os.environ.setdefault("LAY4FLOW_PROFILE_PRESENTATION", "native")
    os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")
    bundled_chrome = (
        root / ".lay4flow-windows" / "backend" / "windows-x64" / "cloakbrowser" / "chrome.exe"
    )
    if bundled_chrome.is_file():
        os.environ.setdefault("CLOAKBROWSER_BINARY_PATH", str(bundled_chrome))
    sys.path.insert(0, str(root))
    # Publish the actual native loopback port to absolute-media URL helpers.
    import uvicorn

    from src.core.config import config

    config.get_raw_config()["server"]["host"] = "127.0.0.1"
    config.get_raw_config()["server"]["port"] = args.port
    stop_file = Path(args.stop_file).resolve()
    server = uvicorn.Server(
        uvicorn.Config("src.main:app", host="127.0.0.1", port=args.port, reload=False, workers=1)
    )

    async def serve() -> None:
        async def watch_stop() -> None:
            while not server.should_exit:
                if stop_file.is_file():
                    server.should_exit = True
                    return
                await asyncio.sleep(0.25)

        watcher = asyncio.create_task(watch_stop())
        try:
            await server.serve()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
