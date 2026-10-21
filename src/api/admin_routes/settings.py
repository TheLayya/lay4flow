"""Settings routes with explicit runtime dependencies."""

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ...core.version import VERSION
from ..admin_schemas import (
    CallLogicConfigRequest,
    GenerationConfigRequest,
    UpdateAdminConfigRequest,
    UpdateAPIKeyRequest,
    UpdateDebugConfigRequest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RELEASE_API_URL = "https://api.github.com/repos/TheLayya/lay4flow/releases/latest"
RELEASE_PAGE_URL = "https://github.com/TheLayya/lay4flow/releases"


def _read_release_metadata() -> dict:
    """Read the checked-in release identity and changelog without network access."""
    version = VERSION
    changelog_path = PROJECT_ROOT / "docs" / "CHANGELOG.md"
    text = changelog_path.read_text(encoding="utf-8") if changelog_path.is_file() else ""
    entries: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        heading = line.strip()
        if heading.startswith("## "):
            if current:
                entries.append(current)
            title = heading[3:].strip()
            release_version, _, release_date = title.partition(" — ")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", title):
                release_version, release_date = "", title
            current = {
                "version": release_version.strip(),
                "date": release_date.strip(),
                "items": [],
            }
        elif current and heading.startswith("- "):
            current["items"].append(heading[2:].strip())
    if current:
        entries.append(current)
    date = entries[0]["date"] if entries else ""
    return {
        "project": "lay4flow-remote",
        "version": version,
        "release_date": date,
        "entries": entries,
        "release_page": RELEASE_PAGE_URL,
        "update_docs": {
            "windows": "/docs/WINDOWS_INSTALL.md",
            "docker": "/docs/DOCKER_UPDATE.md",
        },
    }


def _valid_release_payload(payload: object) -> dict | None:
    """Keep the remote check informational and limited to the trusted repository."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = str(payload.get("tag_name") or "").strip().removeprefix("v")
    html_url = str(payload.get("html_url") or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", tag) or html_url not in {
        f"{RELEASE_PAGE_URL}/tag/{tag}",
        f"{RELEASE_PAGE_URL}/tag/v{tag}",
    }:
        return None
    return {"version": tag, "url": html_url, "name": str(payload.get("name") or tag)}


async def _check_latest_release() -> dict:
    local = _read_release_metadata()

    def fetch() -> dict | None:
        request = urllib.request.Request(
            RELEASE_API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "lay4flow-remote"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return _valid_release_payload(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as error:
            return {"unpublished": True} if error.code == 404 else None
        except (OSError, ValueError):
            return None

    try:
        latest = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=9)
    except TimeoutError:
        latest = None
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if latest is None or latest.get("unpublished"):
        return {
            "success": True,
            "status": "unpublished" if latest else "unavailable",
            "message": (
                "GitHub 尚未发布稳定版，当前版本与本地更新记录仍可查看。"
                if latest
                else "线上版本暂时不可查询，当前版本与本地更新记录仍可使用。"
            ),
            "local": local,
            "checked_at": checked_at,
        }
    latest_version = latest["version"]
    newer = tuple(map(int, latest_version.split("."))) > tuple(map(int, VERSION.split(".")))
    return {
        "success": True,
        "status": "update_available" if newer else "up_to_date",
        "message": (
            f"发现线上版本 {latest_version}，请查看发布页。"
            if newer
            else "当前版本已包含发布页上的最新稳定版。"
        ),
        "local": local,
        "latest": latest,
        "checked_at": checked_at,
    }


def create_settings_router(
    get_database: Callable,
    get_config: Callable,
    build_health: Callable,
    verify_token: Callable,
    call_generation_config: Callable,
):
    router = APIRouter()
    release_check_lock = asyncio.Lock()
    release_check_cache = {"at": 0.0, "result": None}

    @router.get("/api/config/generation")
    async def get_generation_config(token: str = Depends(verify_token)):
        """Get generation timeout configuration"""
        config = await get_database().get_generation_config()
        return {
            "success": True,
            "config": {
                "image_timeout": config.image_timeout,
                "video_timeout": config.video_timeout,
                "max_retries": config.max_retries,
            },
        }

    @router.post("/api/config/generation")
    async def update_generation_config(
        request: GenerationConfigRequest, token: str = Depends(verify_token)
    ):
        """Update generation timeout configuration"""
        await get_database().update_generation_config(
            image_timeout=request.image_timeout,
            video_timeout=request.video_timeout,
            max_retries=request.max_retries,
        )
        await get_database().reload_config_to_memory()
        return {"success": True, "message": "生成配置更新成功"}

    @router.get("/api/call-logic/config")
    async def get_call_logic_config(token: str = Depends(verify_token)):
        """Get token call logic configuration."""
        config_obj = await get_database().get_call_logic_config()
        call_mode = getattr(config_obj, "call_mode", None)
        if call_mode not in ("default", "polling"):
            call_mode = (
                "polling" if getattr(config_obj, "polling_mode_enabled", False) else "default"
            )
        return {
            "success": True,
            "config": {"call_mode": call_mode, "polling_mode_enabled": call_mode == "polling"},
        }

    @router.post("/api/call-logic/config")
    async def update_call_logic_config(
        request: CallLogicConfigRequest, token: str = Depends(verify_token)
    ):
        """Update token call logic configuration."""
        call_mode = request.call_mode if request.call_mode in ("default", "polling") else None
        if call_mode is None:
            raise HTTPException(status_code=400, detail="Invalid call_mode")
        await get_database().update_call_logic_config(call_mode)
        await get_database().reload_config_to_memory()
        return {
            "success": True,
            "message": "Token轮询模式保存成功",
            "config": {"call_mode": call_mode, "polling_mode_enabled": call_mode == "polling"},
        }

    @router.get("/api/system/info")
    async def get_system_info(token: str = Depends(verify_token)):
        """Get system information"""
        stats = await get_database().get_system_info_stats()
        return {
            "success": True,
            "info": {
                "total_tokens": stats["total_tokens"],
                "active_tokens": stats["active_tokens"],
                "total_credits": stats["total_credits"],
                "version": VERSION,
            },
        }

    @router.get("/api/release")
    async def get_public_release_metadata():
        """Return the local release notes for the login screen and desktop shell.

        The payload contains only checked-in public metadata and the changelog;
        account, proxy, and runtime settings remain behind the admin session.
        """
        return {"success": True, **_read_release_metadata()}

    @router.get("/api/release/check")
    async def check_public_release():
        """Check the trusted GitHub release page without requiring a login."""
        async with release_check_lock:
            if (
                release_check_cache["result"] is None
                or time.monotonic() - release_check_cache["at"] > 300
            ):
                release_check_cache["result"] = await _check_latest_release()
                release_check_cache["at"] = time.monotonic()
            return release_check_cache["result"]

    @router.get("/api/admin/release")
    async def get_release_metadata(token: str = Depends(verify_token)):
        """Return the local release identity and checked-in changelog."""
        return await get_public_release_metadata()

    @router.get("/api/admin/release/check")
    async def check_latest_release(token: str = Depends(verify_token)):
        """Check the trusted GitHub release page without delaying app startup."""
        return await check_public_release()

    @router.get("/docs/{document}", include_in_schema=False)
    async def get_release_document(document: str):
        """Serve only public install/update instructions shipped with this release."""
        if document not in {"WINDOWS_INSTALL.md", "DOCKER_UPDATE.md", "CHANGELOG.md"}:
            raise HTTPException(status_code=404, detail="Document not found")
        path = PROJECT_ROOT / "docs" / document
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Document not found")
        return FileResponse(path, media_type="text/plain; charset=utf-8")

    @router.get("/health")
    async def health_check(request: Request):
        """Public health check endpoint - no auth required"""
        try:
            payload = await build_health(get_database())
        except Exception:
            payload = {"backend_running": False, "ready": False, "has_active_tokens": False}
        payload["version"] = VERSION
        app = getattr(request, "app", None)
        activity = getattr(getattr(app, "state", None), "update_activity", None)
        payload["update_active_requests"] = getattr(activity, "active_requests", 0)
        payload["update_in_progress"] = bool(activity and activity.marker.exists())
        manager = getattr(getattr(app, "state", None), "token_manager", None)
        imports = getattr(manager, "_profile_import_jobs", None)
        payload["update_pending_imports"] = imports._pending() if imports else 0
        # Browser profile presentation is local on Windows desktop installs;
        # containers keep the existing noVNC relay. This public hint lets the
        # static profile page choose a transport before opening a WebSocket.
        payload["viewer_presentation"] = (
            "native"
            if os.getenv("LAY4FLOW_PROFILE_PRESENTATION", "relay").strip().lower()
            in {"native", "windows", "local"}
            else "relay"
        )
        return payload

    @router.get("/api/stats")
    async def get_stats(token: str = Depends(verify_token)):
        """Get statistics for dashboard"""
        return await get_database().get_dashboard_stats()

    @router.get("/api/admin/config")
    async def get_admin_config(token: str = Depends(verify_token)):
        """Get admin configuration"""
        admin_config = await get_database().get_admin_config()
        return {
            "admin_username": admin_config.username,
            "api_key": admin_config.api_key,
            "error_ban_threshold": admin_config.error_ban_threshold,
            "debug_enabled": get_config().debug_enabled,
        }

    @router.get("/api/admin/network")
    async def get_host_network(token: str = Depends(verify_token)):
        """Expose only host transport selection; never account proxy credentials."""
        state = {}
        path = Path(getattr(get_database(), "db_path", "data/flow.db")).parent / "host-network.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            pass
        if not isinstance(state, dict):
            state = {}
        selected = os.getenv("LAY4FLOW_HOST_PROXY", "").strip()
        reachable = None
        try:
            proxy = urlsplit(selected)
            host = proxy.hostname or ""
            if host and proxy.port:
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, proxy.port), timeout=2
                    )
                    writer.close()
                    reachable = True
                except (OSError, TimeoutError):
                    reachable = False
            if ":" in host:
                host = f"[{host}]"
            endpoint = f"{proxy.scheme}://{host}:{proxy.port}" if host and proxy.port else ""
        except ValueError:
            endpoint = ""
        return {
            "mode": state.get("mode", "auto"),
            "source": state.get("source", "direct"),
            "upstream": endpoint,
            "reachable": reachable,
            "checked_at": state.get("checked_at", ""),
            "message": (
                "宿主代理端口已连通，账号仍使用原绑定出口"
                if reachable
                else "容器连接宿主代理端口失败，请检查代理程序并重新打开桌面端"
                if endpoint
                else "未设置宿主前置代理，按账号代理或直连网络访问"
            ),
        }

    @router.post("/api/admin/config")
    async def update_admin_config(
        request: UpdateAdminConfigRequest, token: str = Depends(verify_token)
    ):
        """Update admin configuration (error_ban_threshold)"""
        await get_database().update_admin_config(error_ban_threshold=request.error_ban_threshold)
        return {"success": True, "message": "配置更新成功"}

    @router.post("/api/admin/apikey")
    async def update_api_key(request: UpdateAPIKeyRequest, token: str = Depends(verify_token)):
        """Update API key (for external API calls, NOT for admin login)"""
        await get_database().update_admin_config(api_key=request.new_api_key)
        await get_database().reload_config_to_memory()
        return {"success": True, "message": "API Key更新成功"}

    @router.post("/api/admin/debug")
    async def update_debug_config(
        request: UpdateDebugConfigRequest, token: str = Depends(verify_token)
    ):
        """Update debug configuration"""
        try:
            get_config().set_debug_enabled(request.enabled)
            status = "enabled" if request.enabled else "disabled"
            return {"success": True, "message": f"Debug mode {status}", "enabled": request.enabled}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")

    @router.get("/api/generation/timeout")
    async def get_generation_timeout(token: str = Depends(verify_token)):
        """Get generation timeout configuration"""
        return await call_generation_config(token)

    @router.post("/api/generation/timeout")
    async def update_generation_timeout(
        request: GenerationConfigRequest, token: str = Depends(verify_token)
    ):
        """Update generation timeout configuration"""
        await get_database().update_generation_config(
            image_timeout=request.image_timeout,
            video_timeout=request.video_timeout,
            max_retries=request.max_retries,
        )
        await get_database().reload_config_to_memory()
        return {"success": True, "message": "生成配置更新成功"}

    return router, (
        get_generation_config,
        update_generation_config,
        get_call_logic_config,
        update_call_logic_config,
        get_system_info,
        health_check,
        get_stats,
        get_admin_config,
        get_host_network,
        update_admin_config,
        update_api_key,
        update_debug_config,
        get_generation_timeout,
        update_generation_timeout,
    )
