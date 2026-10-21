"""Authenticated interactive view of the selected account's persistent Profile."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, RedirectResponse

from ...services.browser.profile_viewer import ViewerError
from ..admin_schemas import CompleteProfileLoginRequest, DirectProfileLoginRequest


def _same_origin(connection) -> bool:
    expected = f"{connection.url.scheme}://{connection.headers.get('host', '')}"
    if connection.url.scheme in {"ws", "wss"}:
        expected = expected.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    return connection.headers.get("origin") == expected


def create_profile_browser_router(
    get_viewer: Callable,
    verify_token: Callable,
    session_valid: Callable,
    cookie_name: str,
    *,
    asset_root: Path = Path("/usr/share/novnc"),
):
    router = APIRouter()
    page_path = Path(__file__).resolve().parents[3] / "static" / "profile-browser.html"
    frontend_page_path = page_path.parent / "app" / "index.html"

    def error_response(error: ViewerError):
        return HTTPException(status_code=error.status, detail=error.message)

    def require_origin(request: Request):
        # Bearer callers may omit Origin; cookie requests must come from this application.
        if not _same_origin(request) and not (
            not request.headers.get("origin") and request.headers.get("authorization")
        ):
            raise HTTPException(status_code=403, detail="请从当前管理页打开账号浏览器")

    @router.post("/api/profile-login")
    async def profile_browser_login_start(
        payload: DirectProfileLoginRequest, request: Request, owner: str = Depends(verify_token)
    ):
        require_origin(request)
        try:
            kwargs = {"proxy_pool_id": payload.proxy_pool_id} if payload.proxy_pool_id else {}
            return await get_viewer().login.start(payload.email, payload.proxy_url, owner, **kwargs)
        except ViewerError as error:
            raise error_response(error) from None

    @router.post("/api/profile-login/{session_id}/complete")
    async def profile_browser_login_complete(
        session_id: str,
        payload: CompleteProfileLoginRequest,
        request: Request,
        owner: str = Depends(verify_token),
    ):
        require_origin(request)
        try:
            return await get_viewer().login.complete(session_id, owner, payload.viewer_id)
        except ViewerError as error:
            raise error_response(error) from None

    @router.delete("/api/profile-login/{session_id}")
    async def profile_browser_login_cancel(
        session_id: str, request: Request, owner: str = Depends(verify_token)
    ):
        require_origin(request)
        try:
            return await get_viewer().login.cancel(
                session_id, owner, request.query_params.get("viewer_id", "")
            )
        except ViewerError as error:
            raise error_response(error) from None

    @router.post("/api/tokens/{token_id}/browser")
    async def open_profile_browser(
        token_id: int, request: Request, owner: str = Depends(verify_token)
    ):
        require_origin(request)
        try:
            state = await get_viewer().open(token_id, owner)
        except ViewerError as error:
            raise error_response(error) from None
        return {"success": True, **state, "viewer_url": f"/api/profile-browser/{token_id}"}

    @router.delete("/api/tokens/{token_id}/browser")
    async def close_profile_browser(
        token_id: int, request: Request, owner: str = Depends(verify_token)
    ):
        require_origin(request)
        try:
            viewer_id = request.query_params.get("viewer_id")
            state = (
                await get_viewer().close(token_id, owner, viewer_id=viewer_id)
                if viewer_id
                else await get_viewer().close(token_id, owner)
            )
        except ViewerError as error:
            raise error_response(error) from None
        return {"success": True, **state}

    @router.post("/api/tokens/{token_id}/browser/focus")
    async def focus_profile_browser(
        token_id: int, request: Request, owner: str = Depends(verify_token)
    ):
        """Bring a Windows-native Cloak window forward without opening another one."""
        require_origin(request)
        try:
            viewer_id = request.query_params.get("viewer_id")
            state = await get_viewer().focus(token_id, owner, viewer_id=viewer_id)
        except ViewerError as error:
            raise error_response(error) from None
        return {"success": True, **state}

    @router.get("/api/tokens/{token_id}/browser")
    async def profile_browser_status(
        token_id: int, request: Request, owner: str = Depends(verify_token)
    ):
        try:
            viewer = get_viewer()
            state = viewer.state(token_id, owner)
            viewer_id = request.query_params.get("viewer_id")
            if viewer_id and state.get("viewer_id") != viewer_id:
                state = {"token_id": token_id, "status": "closed", "expires_at": None}
            return {"success": True, **state}
        except ViewerError as error:
            raise error_response(error) from None

    @router.post("/api/tokens/{token_id}/browser/refresh")
    async def refresh_profile_browser(
        token_id: int, request: Request, owner: str = Depends(verify_token)
    ):
        require_origin(request)
        try:
            viewer_id = request.query_params.get("viewer_id")
            result = (
                await get_viewer().refresh(token_id, owner, viewer_id=viewer_id)
                if viewer_id
                else await get_viewer().refresh(token_id, owner)
            )
        except ViewerError as error:
            raise error_response(error) from None
        return result

    @router.get("/api/profile-browser/{token_id}", include_in_schema=False)
    async def profile_browser_page(
        token_id: int, request: Request, owner: str = Depends(verify_token)
    ):
        viewer = get_viewer()
        if not viewer.owns(token_id, owner):
            raise HTTPException(status_code=404, detail="账号浏览器未打开")
        viewer_id = viewer.state(token_id, owner).get("viewer_id")
        if viewer_id and request.query_params.get("viewer_id") != viewer_id:
            if request.query_params.get("viewer_id"):
                raise HTTPException(status_code=404, detail="账号浏览器已切换或关闭")
            return RedirectResponse(f"/api/profile-browser/{token_id}?viewer_id={viewer_id}")
        return FileResponse(
            frontend_page_path if frontend_page_path.is_file() else page_path,
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    @router.get("/api/profile-browser-assets/{asset_path:path}", include_in_schema=False)
    async def profile_browser_asset(asset_path: str, owner: str = Depends(verify_token)):
        parts = Path(asset_path).parts
        if (
            not parts
            or parts[0] not in {"core", "vendor"}
            or any(part in {"..", "."} for part in parts)
            or "\\" in asset_path
        ):
            raise HTTPException(status_code=404)
        path = asset_root / asset_path
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @router.websocket("/api/profile-browser/{token_id}/ws")
    async def profile_browser_stream(websocket: WebSocket, token_id: int):
        owner = str(websocket.cookies.get(cookie_name) or "")
        viewer = get_viewer()
        if (
            not _same_origin(websocket)
            or not session_valid(owner)
            or not viewer.owns(token_id, owner)
        ):
            await websocket.close(code=1008)
            return
        try:
            if viewer.state(token_id, owner).get("presentation") == "native":
                await websocket.close(code=1008)
                return
        except ViewerError:
            await websocket.close(code=1008)
            return
        try:
            async with viewer.stream_session(
                token_id, owner, websocket.query_params.get("viewer_id")
            ) as stop:
                await relay(websocket, viewer, token_id, owner, stop)
        except ViewerError:
            await websocket.close(code=1008)

    async def relay(websocket, viewer, token_id, owner, stop):
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 5900), 5)
        except (OSError, TimeoutError):
            await websocket.close(code=1011)
            return

        async def send_screen():
            while not stop.is_set() and (data := await reader.read(65536)):
                if stop.is_set():
                    break
                await websocket.send_bytes(data)

        async def receive_input():
            while not stop.is_set():
                data = await websocket.receive_bytes()
                if stop.is_set():
                    break
                writer.write(data)
                await writer.drain()

        async def check_owner():
            while session_valid(owner) and viewer.owns(token_id, owner) and not stop.is_set():
                await asyncio.sleep(1)

        tasks = []
        try:
            await websocket.accept()
            tasks = [
                asyncio.create_task(call())
                for call in (send_screen, receive_input, check_owner, stop.wait)
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            try:
                await websocket.close()
            except RuntimeError:
                pass

    return router
