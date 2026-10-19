"""Runtime refresh/cache settings, with persistence and live reload kept explicit."""

from collections.abc import Callable
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..admin_schemas import TokenRefreshConfigRequest


def create_runtime_config_router(
    get_database: Callable, verify_token: Callable, sync_cache: Callable
):
    router = APIRouter()

    @router.get("/api/token-refresh/config")
    async def get_token_refresh_config(token: str = Depends(verify_token)):
        """Get AT/protocol refresh configuration."""
        refresh_config = await get_database().get_token_refresh_config()
        return {
            "success": True,
            "config": {
                "at_auto_refresh_enabled": True,
                "protocol_refresh_enabled": refresh_config.enabled,
                "refresh_interval_minutes": refresh_config.refresh_interval_minutes,
            },
        }

    @router.post("/api/token-refresh/enabled")
    async def update_token_refresh_enabled(
        request: Optional[dict] = None, token: str = Depends(verify_token)
    ):
        """Update protocol ST refresh enabled; AT refresh remains always enabled."""
        enabled = (request or {}).get("enabled")
        if enabled is not None:
            await get_database().update_token_refresh_config(enabled=bool(enabled))
        return {"success": True, "message": "刷新配置已更新"}

    @router.post("/api/token-refresh/config")
    async def update_token_refresh_config(
        request: TokenRefreshConfigRequest, token: str = Depends(verify_token)
    ):
        refresh_config = await get_database().update_token_refresh_config(
            enabled=request.enabled,
            refresh_interval_minutes=request.refresh_interval_minutes,
        )
        return {
            "success": True,
            "config": {
                "at_auto_refresh_enabled": True,
                "protocol_refresh_enabled": refresh_config.enabled,
                "refresh_interval_minutes": refresh_config.refresh_interval_minutes,
            },
        }

    @router.get("/api/cache/config")
    async def get_cache_config(token: str = Depends(verify_token)):
        """Get cache configuration"""
        cache_config = await get_database().get_cache_config()

        # Calculate effective base URL
        effective_base_url = (
            cache_config.cache_base_url if cache_config.cache_base_url else "http://127.0.0.1:8000"
        )

        return {
            "success": True,
            "config": {
                "enabled": cache_config.cache_enabled,
                "timeout": cache_config.cache_timeout,
                "base_url": cache_config.cache_base_url or "",
                "effective_base_url": effective_base_url,
            },
        }

    @router.post("/api/cache/enabled")
    async def update_cache_enabled(request: dict, token: str = Depends(verify_token)):
        """Update cache enabled status"""
        enabled = request.get("enabled", False)
        await get_database().update_cache_config(enabled=enabled)

        # 🔥 Hot reload: sync database config to memory
        await get_database().reload_config_to_memory()
        await sync_cache()

        return {"success": True, "message": f"缓存已{'启用' if enabled else '禁用'}"}

    @router.post("/api/cache/config")
    async def update_cache_config_full(request: dict, token: str = Depends(verify_token)):
        """Update complete cache configuration"""
        enabled = request.get("enabled")
        timeout = request.get("timeout")
        base_url = request.get("base_url")

        if timeout is not None:
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="缓存超时时间必须为整数")
            if timeout < 0:
                raise HTTPException(status_code=400, detail="缓存超时时间不能小于 0")

        await get_database().update_cache_config(
            enabled=enabled, timeout=timeout, base_url=base_url
        )

        # 🔥 Hot reload: sync database config to memory
        await get_database().reload_config_to_memory()
        await sync_cache()

        return {"success": True, "message": "缓存配置更新成功"}

    @router.post("/api/cache/base-url")
    async def update_cache_base_url(request: dict, token: str = Depends(verify_token)):
        """Update cache base URL"""
        base_url = request.get("base_url", "")
        await get_database().update_cache_config(base_url=base_url)

        # 🔥 Hot reload: sync database config to memory
        await get_database().reload_config_to_memory()
        await sync_cache()

        return {"success": True, "message": "缓存Base URL更新成功"}

    return router, (
        get_token_refresh_config,
        update_token_refresh_enabled,
        update_token_refresh_config,
        get_cache_config,
        update_cache_enabled,
        update_cache_config_full,
        update_cache_base_url,
    )
