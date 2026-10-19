"""Auth routes with explicit runtime dependencies."""

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Response

from ...core.admin_sessions import ADMIN_SESSION_MAX_AGE
from ..admin_schemas import ChangePasswordRequest, LoginRequest


def create_auth_router(
    get_database: Callable,
    get_auth_manager: Callable,
    get_sessions: Callable,
    get_cookie_name: Callable,
    new_token: Callable,
    verify_token: Callable,
    call_login: Callable,
    call_logout: Callable,
    call_change_password: Callable,
):
    router = APIRouter()

    @router.post("/api/admin/login")
    async def admin_login(request: LoginRequest, response: Response):
        """Admin login - returns session token (NOT API key)"""
        admin_config = await get_database().get_admin_config()
        if not get_auth_manager().verify_admin(request.username, request.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        session_token = f"admin-{new_token(32)}"
        get_sessions().add(session_token)
        response.set_cookie(
            key=get_cookie_name(),
            value=session_token,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
            max_age=ADMIN_SESSION_MAX_AGE,
        )
        return {"success": True, "token": session_token, "username": admin_config.username}

    @router.post("/api/admin/logout")
    async def admin_logout(response: Response, token: str = Depends(verify_token)):
        """Admin logout - invalidate session token"""
        get_sessions().discard(token)
        response.delete_cookie(get_cookie_name(), path="/")
        return {"success": True, "message": "退出登录成功"}

    @router.post("/api/admin/change-password")
    async def change_password(request: ChangePasswordRequest, token: str = Depends(verify_token)):
        """Change admin password"""
        admin_config = await get_database().get_admin_config()
        if not get_auth_manager().verify_admin(admin_config.username, request.old_password):
            raise HTTPException(status_code=400, detail="旧密码错误")
        update_params = {"password": request.new_password}
        if request.username:
            update_params["username"] = request.username
        await get_database().update_admin_config(**update_params)
        await get_database().reload_config_to_memory()
        get_sessions().clear()
        return {"success": True, "message": "密码修改成功,请重新登录"}

    @router.post("/api/login")
    async def login(request: LoginRequest, response: Response):
        """Login endpoint (alias for /api/admin/login)"""
        return await call_login(request, response)

    @router.post("/api/logout")
    async def logout(response: Response, token: str = Depends(verify_token)):
        """Logout endpoint (alias for /api/admin/logout)"""
        return await call_logout(response, token)

    @router.post("/api/admin/password")
    async def update_admin_password(
        request: ChangePasswordRequest, token: str = Depends(verify_token)
    ):
        """Update admin password"""
        return await call_change_password(request, token)

    return router, (
        admin_login,
        admin_logout,
        change_password,
        login,
        logout,
        update_admin_password,
    )
