"""Settings routes with explicit runtime dependencies."""

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from ..admin_schemas import (
    CallLogicConfigRequest,
    GenerationConfigRequest,
    UpdateAdminConfigRequest,
    UpdateAPIKeyRequest,
    UpdateDebugConfigRequest,
)


def create_settings_router(
    get_database: Callable,
    get_config: Callable,
    build_health: Callable,
    verify_token: Callable,
    call_generation_config: Callable,
):
    router = APIRouter()

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
                "version": "1.0.0",
            },
        }

    @router.get("/health")
    async def health_check():
        """Public health check endpoint - no auth required"""
        try:
            return await build_health(get_database())
        except Exception:
            return {"backend_running": True, "has_active_tokens": False}

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
        update_admin_config,
        update_api_key,
        update_debug_config,
        get_generation_timeout,
        update_generation_timeout,
    )
