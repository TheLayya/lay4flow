"""Keep one existing account Profile open on the shared debugging desktop."""

import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from .profiles import ProfileNotReadyError


class ViewerError(RuntimeError):
    def __init__(self, status: int, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class ProfileBrowserViewer:
    def __init__(self, token_manager: Any, *, ttl_seconds: float = 1800):
        from .profile_login import DirectProfileLogin

        if not 0 < ttl_seconds <= 1800:
            raise ValueError("viewer lifetime must be between 0 and 1800 seconds")
        self.token_manager = token_manager
        self.token_manager.profile_delete_handler = self.delete_token
        self.login = DirectProfileLogin(self)
        self.ttl_seconds = ttl_seconds
        self._guard = asyncio.Lock()
        self._switch_guard = asyncio.Lock()
        self._shutdown = False
        self._viewer_id = ""
        self._streams: dict[asyncio.Task, tuple[asyncio.Event, asyncio.Event]] = {}
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._token_id: int | None = None
        self._owner = ""
        self._status = "closed"
        self._expires_at: float | None = None
        self._entered = False
        self._stop_requested = False
        self._context: Any = None
        self._page: Any = None
        self._email = ""
        self._proxy_url = ""
        self._presentation = (
            "native"
            if os.getenv("LAY4FLOW_PROFILE_PRESENTATION", "relay").strip().lower()
            in {"native", "windows", "local"}
            else "relay"
        )
        refresher = getattr(token_manager, "profile_refresher", None)
        if hasattr(refresher, "active_context_provider"):
            refresher.active_context_provider = self.borrow_context

    @asynccontextmanager
    async def borrow_context(self, token: Any):
        """Lease this viewer's context for a background refresh, if it matches."""
        async with self._guard:
            if self._task is not None and getattr(token, "id", None) == self._token_id:
                if (
                    self._status != "ready"
                    or self._context is None
                    or str(getattr(token, "email", "")).strip().casefold()
                    != self._email.strip().casefold()
                    or getattr(token, "browser_profile_source", None) != "cloak"
                ):
                    raise ProfileNotReadyError(str(getattr(token, "email", "")), "transient_error")
                refresher = getattr(self.token_manager, "profile_refresher", None)
                try:
                    proxy_url = await refresher._proxy_url(token)
                except Exception:
                    raise ProfileNotReadyError(str(token.email), "transient_error") from None
                if proxy_url != self._proxy_url:
                    raise ProfileNotReadyError(str(token.email), "transient_error")
                yield self._context
                return
        # A non-matching or closed viewer must not hold its guard while
        # the refresher opens the ordinary credential session.
        yield None

    def owns(self, token_id: int, owner: str) -> bool:
        return bool(
            self._task
            and not self._task.done()
            and self._token_id == token_id
            and self._owner == owner
            and self._status == "ready"
        )

    def state(self, token_id: int, owner: str) -> dict[str, Any]:
        if self._task is not None:
            if self._token_id != token_id or self._owner != owner:
                if self._owner == owner and self._token_id != token_id:
                    return {"token_id": token_id, "status": "closed", "expires_at": None}
                raise ViewerError(403, "无权访问此 Profile 浏览器窗口")
            return {
                "token_id": token_id,
                "status": self._status,
                "expires_at": self._expires_at,
                "viewer_id": self._viewer_id,
                # A local Windows service presents Cloak's headed Chromium
                # directly. The old VNC stream remains available only for
                # container deployments that expose it.
                "presentation": self._presentation,
            }
        return {
            "token_id": token_id,
            "status": "closed",
            "expires_at": None,
            "presentation": self._presentation,
        }

    async def open(self, token_id: int, owner: str) -> dict[str, Any]:
        if not owner:
            raise ViewerError(401, "请先登录管理页")
        if self._task is not None and self._owner != owner:
            raise ViewerError(409, "其他管理员正在使用账号浏览器")
        if self._status == "error":
            raise ViewerError(409, "Profile 浏览器窗口尚未完成关闭，请检查服务状态")
        if self._task is not None and self._token_id != token_id:
            await self._validate_target(token_id)
        # One shared desktop: switch atomically rather than accumulate windows.
        return await self._switch(token_id, owner)

    async def _switch(self, token_id: int, owner: str) -> dict[str, Any]:
        async with self._switch_guard:
            if self._shutdown:
                raise ViewerError(503, "账号浏览器服务正在关闭")
            if self._task is not None:
                if self._owner != owner:
                    raise ViewerError(409, "其他管理员正在使用账号浏览器")
                if self._token_id != token_id:
                    # Validate B first so a bad request does not close A.
                    await self._validate_target(token_id)
                    await self._close(self._token_id, owner)
            return await self._open(token_id, owner)

    async def _validate_target(self, token_id: int):
        refresher = self.token_manager.profile_refresher
        if refresher is None:
            raise ViewerError(503, "服务器未配置账号 Profile")
        token = await self.token_manager.db.get_token(token_id)
        if token is None or token.id != token_id:
            raise ViewerError(404, "账号不存在")
        if token.browser_profile_source != "cloak":
            raise ViewerError(400, "此账号没有可查看的 Cloak Profile")
        manager = refresher.manager
        presentation = self._presentation
        # Native Windows presentation can launch a headed context even when
        # background maintenance is configured headless. Relay mode keeps the
        # previous explicit headed requirement for the VNC desktop.
        if (
            presentation != "native"
            and getattr(manager.backend, "options", {}).get("headless") is not False
        ):
            raise ViewerError(503, "Profile 浏览器窗口需要启用有头浏览器")
        profile_dir = manager.root_dir / manager._normalize_account_key(token.email)
        if not (profile_dir / "metadata.json").is_file() or not (profile_dir / "Default").is_dir():
            raise ViewerError(404, "此账号尚未保存浏览器 Profile")
        try:
            proxy_url = await refresher._proxy_url(token)
        except Exception:
            raise ViewerError(503, "账号浏览器代理配置无效") from None
        return token, manager, proxy_url

    async def _open(self, token_id: int, owner: str) -> dict[str, Any]:
        if not owner:
            raise ViewerError(401, "请先登录管理页")
        if os.getenv("LAY4FLOW_PROFILE_VIEWER_ENABLED", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise ViewerError(503, "服务器尚未启用 Profile 浏览器窗口")
        async with self._guard:
            if self._task is not None:
                if self._token_id != token_id or self._owner != owner:
                    raise ViewerError(409, "已有 Profile 浏览器窗口正在使用，请先关闭")
                if self._status in {"closing", "error"}:
                    raise ViewerError(409, "Profile 浏览器窗口尚未完成关闭，请检查服务状态")
                ready = self._ready
            else:
                token, manager, proxy_url = await self._validate_target(token_id)
                self._viewer_id = secrets.token_urlsafe(16)
                self._token_id = token_id
                self._owner = owner
                self._status = "starting"
                self._entered = False
                self._stop_requested = False
                self._email = token.email
                self._proxy_url = proxy_url
                self._ready = asyncio.get_running_loop().create_future()
                ready = self._ready
                start_url = (
                    "https://accounts.google.com/ServiceLogin"
                    "?continue=https%3A%2F%2Fflow.google.com%2F"
                    if getattr(token, "ban_reason", None) == "profile_login"
                    else "https://flow.google.com/"
                )
                self._task = asyncio.create_task(
                    self._run(manager, token.email, proxy_url, ready, start_url)
                )
                self._task.add_done_callback(lambda task: self._finished(task, ready))
        try:
            result = await asyncio.wait_for(asyncio.shield(ready), timeout=90)
        except TimeoutError:
            await self._close(token_id, owner)
            raise ViewerError(503, "Profile 浏览器启动超时，请稍后重试") from None
        except asyncio.CancelledError:
            await self._close(token_id, owner)
            raise
        if isinstance(result, ViewerError):
            if self._task is not None:
                await self._close(token_id, owner)
            raise result
        return self.state(token_id, owner)

    async def _run(
        self, manager: Any, email: str, proxy_url: str, ready: asyncio.Future, start_url: str
    ) -> ViewerError | None:
        try:
            async with manager.session(email, proxy_url=proxy_url, interactive=True) as context:
                self._entered = True
                self._context = context
                if self._stop_requested:
                    return None
                try:
                    page = await context.new_page()
                    self._page = page
                    # Window presentation must not alter the Profile's launch identity.
                    try:
                        desktop = await context.new_cdp_session(page)
                        try:
                            window = await desktop.send("Browser.getWindowForTarget")
                            await desktop.send(
                                "Browser.setWindowBounds",
                                {
                                    "windowId": window["windowId"],
                                    "bounds": {"windowState": "maximized"},
                                },
                            )
                        finally:
                            await desktop.detach()
                    except Exception:
                        pass
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                    # Keep the account window in front after navigation. This
                    # is a no-op on runtimes without a native window.
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                except Exception:
                    ready.set_result(ViewerError(503, "Profile 浏览器启动失败，请检查服务配置"))
                    return None
                self._status = "ready"
                self._expires_at = time.time() + self.ttl_seconds
                ready.set_result(None)
                await asyncio.sleep(self.ttl_seconds)
                # Wait for any borrowed refresh context before closing it.
                async with self._guard:
                    self._status = "closing"
                    self._context = None
                await self._drain_streams()
        except asyncio.CancelledError:
            pass
        except Exception:
            if not ready.done():
                ready.set_result(ViewerError(503, "Profile 浏览器启动失败，请检查服务配置"))
            if self._entered:
                return ViewerError(503, "Profile 浏览器关闭失败，请检查服务状态")
        finally:
            self._context = None
            self._page = None
        return None

    def _finished(self, task: asyncio.Task, ready: asyncio.Future) -> None:
        if not ready.done():
            ready.set_result(ViewerError(409, "Profile 浏览器窗口已关闭"))
        if not task.cancelled() and isinstance(task.result(), ViewerError):
            # A failed browser close may leave the shared desktop occupied.
            # Keep its owner and slot until the server restarts.
            self._status = "error"
            return
        if self._task is task:
            self._task = None
            self._ready = None
            self._token_id = None
            self._owner = ""
            self._status = "closed"
            self._expires_at = None
            self._entered = False
            self._stop_requested = False
            self._context = None
            self._page = None
            self._email = ""
            self._proxy_url = ""

    async def refresh(
        self,
        token_id: int,
        owner: str,
        viewer_id: str | None = None,
        *,
        accept_existing_session: bool = False,
    ) -> dict[str, Any]:
        # Share the close/start guard so neither close nor lifetime expiry can
        # destroy the browser while the normal credential pipeline uses it.
        async with self._guard:
            if viewer_id and viewer_id != self._viewer_id:
                raise ViewerError(409, "账号浏览器已切换或关闭，请重新打开")
            self.state(token_id, owner)
            if not self.owns(token_id, owner) or self._context is None:
                raise ViewerError(409, "账号浏览器未就绪，请先打开浏览器")
            token = await self.token_manager.db.get_token(token_id)
            if token is None:
                raise ViewerError(404, "账号不存在")
            if (
                token.email.strip().casefold() != self._email.strip().casefold()
                or token.browser_profile_source != "cloak"
            ):
                raise ViewerError(409, "账号配置已变化，请关闭后重新打开浏览器")
            refresher = self.token_manager.profile_refresher
            try:
                proxy_url = await refresher._proxy_url(token)
            except Exception:
                raise ViewerError(503, "账号浏览器代理配置无效") from None
            if proxy_url != self._proxy_url:
                raise ViewerError(409, "账号代理已变化，请关闭后重新打开浏览器")
            pending = getattr(self.token_manager, "_refresh_futures", {}).get(token_id)
            if pending is not None and not pending.done():
                raise ViewerError(409, "该账号正在刷新，请等待完成后重试")
            kwargs = {"accept_existing_session": True} if accept_existing_session else {}
            async with refresher.use_context(self._email, self._context, **kwargs):
                operation = asyncio.create_task(
                    self.token_manager._refresh_at(token_id, force=True, refresh_st=True)
                )
                try:
                    success = await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # TokenManager shields its worker; retain browser ownership
                    # until that worker finishes even when HTTP disconnects.
                    await asyncio.shield(operation)
                    raise
            latest = await self.token_manager.db.get_token(token_id)
            return {
                "success": bool(success),
                **self.state(token_id, owner),
                "at_expires": latest.at_expires.isoformat()
                if latest is not None and latest.at_expires
                else None,
                "credits": latest.credits if latest is not None else None,
                "refresh_status": latest.refresh_status if latest is not None else None,
                "message": "已刷新凭据，浏览器保持打开"
                if success
                else "凭据刷新未成功，请查看账号刷新状态",
            }

    async def close(
        self, token_id: int, owner: str, viewer_id: str | None = None
    ) -> dict[str, Any]:
        # Close must also be able to interrupt navigation during startup.
        return await self._close(token_id, owner, viewer_id)

    async def focus(
        self, token_id: int, owner: str, viewer_id: str | None = None
    ) -> dict[str, Any]:
        """Bring the native Cloak window to the foreground without reopening it."""
        async with self._guard:
            if viewer_id and viewer_id != self._viewer_id:
                raise ViewerError(409, "账号浏览器已切换或关闭，请重新打开")
            self.state(token_id, owner)
            if not self.owns(token_id, owner) or self._context is None or self._page is None:
                raise ViewerError(409, "账号浏览器未就绪，请先打开浏览器")
            page = self._page
            context = self._context
        try:
            await page.bring_to_front()
            desktop = await context.new_cdp_session(page)
            try:
                window = await desktop.send("Browser.getWindowForTarget")
                await desktop.send(
                    "Browser.setWindowBounds",
                    {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}},
                )
            finally:
                await desktop.detach()
        except Exception:
            # The browser may have been closed by the user between the two
            # calls. Keep the viewer lease authoritative and report a retryable
            # status instead of losing profile ownership.
            raise ViewerError(409, "本地浏览器窗口已关闭，请重新打开") from None
        return self.state(token_id, owner)

    async def _close(
        self, token_id: int, owner: str, viewer_id: str | None = None
    ) -> dict[str, Any]:
        async with self._guard:
            if self._task is None:
                return self.state(token_id, owner)
            if self._owner != owner:
                raise ViewerError(403, "无权访问此 Profile 浏览器窗口")
            if self._token_id != token_id or (viewer_id and viewer_id != self._viewer_id):
                return {"token_id": token_id, "status": "closed", "expires_at": None}
            self.state(token_id, owner)
            if self._status == "error":
                raise ViewerError(503, "Profile 浏览器关闭失败，请检查服务状态")
            task = self._task
            if self._status != "closing":
                self._status = "closing"
                self._stop_requested = True
                # ProfileManager cannot clean a cancellation during backend.open.
                # Let context entry finish, then close before opening a page.
                if self._entered:
                    task.cancel()
        await self._drain_streams()
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            result = None
        except TimeoutError:
            raise ViewerError(503, "Profile 浏览器正在关闭，请稍后重试") from None
        if isinstance(result, ViewerError):
            raise result
        if self._task is task:
            self._finished(task, self._ready)
        return {"token_id": token_id, "status": "closed", "expires_at": None}

    async def delete_token(self, token_id: int) -> None:
        """An explicit account delete also owns its viewer and pending login cleanup."""
        async with self.login._guard, self._switch_guard:
            if self._task is not None and self._token_id == token_id:
                await self._close(token_id, self._owner)
            await self.token_manager._delete_token(token_id)
            self.login.forget_token(token_id)

    async def close_all(self) -> None:
        await self.login.close_all()
        self._shutdown = True
        async with self._switch_guard:
            if self._task is not None:
                await self._close(self._token_id, self._owner)

    def matches(self, token_id: int, owner: str, viewer_id: str | None = None) -> bool:
        return self.owns(token_id, owner) and (not viewer_id or viewer_id == self._viewer_id)

    @asynccontextmanager
    async def stream_session(self, token_id: int, owner: str, viewer_id: str | None = None):
        if self._presentation == "native":
            raise ViewerError(409, "当前运行在 Windows 原生浏览器模式，无需画面转发")
        if not viewer_id or not self.matches(token_id, owner, viewer_id):
            raise ViewerError(404, "账号浏览器已切换或关闭")
        task = asyncio.current_task()
        stop, done = asyncio.Event(), asyncio.Event()
        self._streams[task] = (stop, done)
        try:
            yield stop
        finally:
            self._streams.pop(task, None)
            done.set()

    async def _drain_streams(self):
        streams = list(self._streams.values())
        for stop, _ in streams:
            stop.set()
        if streams:
            try:
                await asyncio.wait_for(asyncio.gather(*(done.wait() for _, done in streams)), 10)
            except TimeoutError:
                raise ViewerError(503, "旧浏览器画面连接尚未关闭，请稍后重试") from None
