"""Owner-bound direct Google login using the ordinary account Profile viewer."""

import asyncio
import re
import secrets
import time
from datetime import datetime, timezone

from ...core.models import Token
from ..proxy_manager import ProxyManager
from .profile_viewer import ViewerError
from .token_account_probe import TokenManagerAccountProbe


class DirectProfileLogin:
    def __init__(self, viewer):
        self.viewer = viewer
        self.tokens = viewer.token_manager
        self._guard = asyncio.Lock()
        self._sessions = {}

    async def start(
        self, email: str, proxy_url: str, owner: str, *, proxy_pool_id: int | None = None
    ) -> dict:
        email = email.strip().casefold()
        if not owner:
            raise ViewerError(401, "请先登录管理页")
        if len(email) > 254 or not re.fullmatch(
            r"[^\s@/\\]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email
        ):
            raise ViewerError(400, "请输入有效的账号邮箱")
        async with self._guard, self.viewer._switch_guard:
            if self.viewer._shutdown:
                raise ViewerError(503, "账号浏览器服务正在关闭")
            if self.viewer._task is not None:
                raise ViewerError(409, "请先关闭当前账号浏览器")
            refresher = self.tokens.profile_refresher
            if refresher is None:
                raise ViewerError(503, "服务器未配置账号 Profile")
            manager = refresher.manager
            profile = manager.root_dir / manager._normalize_account_key(email)
            existing = await self.tokens.db.get_token_by_email(email)
            resumed = bool(
                existing
                and not existing.is_active
                and existing.ban_reason == "profile_login"
                and existing.browser_profile_source == "cloak"
            )
            if existing and not resumed:
                raise ViewerError(409, "账号已存在，请在账号列表打开其浏览器")
            if profile.exists() and any(profile.iterdir()) and not resumed:
                raise ViewerError(409, "该邮箱已有 Profile，请先核对现有账号")
            if resumed:
                token_id = existing.id
                proxy_pool_id, proxy_url = existing.proxy_pool_id, existing.proxy_url
            else:
                try:
                    proxy_pool_id, proxy_url = await ProxyManager(
                        self.tokens.db
                    ).select_account_proxy(proxy_pool_id, proxy_url)
                except ValueError:
                    raise ViewerError(400, "请选择可用的代理池出口，或先在代理池添加出口") from None
            session_id = secrets.token_urlsafe(24)
            if not resumed:
                token_id = await self.tokens.db.add_token(
                    Token(
                        st="profile-login:" + session_id,
                        email=email,
                        name=email.split("@", 1)[0],
                        remark="Cloak 登录中",
                        is_active=False,
                        image_enabled=False,
                        video_enabled=False,
                        auto_refresh_enabled=False,
                        login_account=email,
                        proxy_url=proxy_url,
                        proxy_pool_id=proxy_pool_id,
                        browser_profile_source="cloak",
                        refresh_status="login_required",
                    )
                )
            for old_id, old in list(self._sessions.items()):
                if old["token_id"] == token_id:
                    timer = old.get("timer")
                    if timer is not None:
                        timer.cancel()
                    self._sessions.pop(old_id, None)
            pending = {"token_id": token_id, "email": email, "owner": owner}
            self._sessions[session_id] = pending
            try:
                await self.tokens.db.update_token(
                    token_id, ban_reason="profile_login", browser_profile_status="login_required"
                )
                token = await self.tokens.db.get_token(token_id)
                proxy = await refresher._proxy_url(token)
                async with manager.session(email, proxy_url=proxy) as context:
                    spec = await manager.spec(email)
                    status = await refresher.verifier.verify_environment(
                        context, str(spec.profile_dir)
                    )
                    if status != "environment_stable":
                        raise ViewerError(503, "账号浏览器环境初始化未通过")
                state = await self.viewer._open(token_id, owner)
                pending.update(
                    viewer_id=state["viewer_id"], expires_at=state["expires_at"], closed=False
                )
                pending["timer"] = asyncio.create_task(self._expire(session_id))
                return {
                    "success": True,
                    **state,
                    "session_id": session_id,
                    "email": email,
                    "viewer_url": f"/api/profile-browser/{token_id}",
                }
            except BaseException:
                await asyncio.shield(self._discard(session_id, pending, delete=not resumed))
                raise

    def _pending(self, session_id: str, owner: str, viewer_id: str) -> dict:
        pending = self._sessions.get(session_id)
        if pending is None:
            raise ViewerError(404, "登录会话已结束，请重新打开")
        if pending["owner"] != owner:
            raise ViewerError(403, "此登录会话属于其他管理员")
        if not viewer_id or pending.get("viewer_id") != viewer_id:
            raise ViewerError(409, "登录浏览器已切换，请重新打开")
        if pending.get("expires_at", 0) <= time.time():
            raise ViewerError(410, "登录会话已到期，请重新打开")
        return pending

    async def complete(self, session_id: str, owner: str, viewer_id: str) -> dict:
        async with self._guard, self.viewer._switch_guard:
            pending = self._pending(session_id, owner, viewer_id)
            if "result" in pending:
                return pending["result"]
            token_id = pending["token_id"]
            token = await self.tokens.db.get_token(token_id)
            if (
                token is None
                or token.email != pending["email"]
                or token.browser_profile_source != "cloak"
                or token.is_active
                or token.ban_reason != "profile_login"
            ):
                raise ViewerError(409, "账号配置已变化，请在账号列表核对")
            if not pending["closed"]:
                if not self.viewer.matches(token_id, owner, viewer_id):
                    raise ViewerError(409, "请保持登录浏览器打开后完成添加")
                result = await self.viewer.refresh(token_id, owner, viewer_id)
                if not result["success"]:
                    raise ViewerError(422, "登录验证未通过，请核对邮箱并完成 Google 登录")
                await self.viewer.close(token_id, owner, viewer_id)
                pending["closed"] = True
            if self.viewer._token_id == token_id and self.viewer._task is not None:
                raise ViewerError(409, "请先关闭该账号浏览器再完成验证")
            # A fresh context must prove persistence. No Cookie injection, reset,
            # OAuth renewal or browser-visibility claim substitutes for this probe.
            token = await self.tokens.db.get_token(token_id)
            refresher = self.tokens.profile_refresher
            manager = refresher.manager
            proxy = await refresher._proxy_url(token)
            probe = TokenManagerAccountProbe(self.tokens, token)
            async with manager.credential_session(
                token.email, refresher.verifier, proxy_url=proxy, existing_only=True
            ) as context:
                probe.set_context(context)
                verification = await refresher.verifier.verify_account(token.email, probe)
                if verification.reason != "account_ready" or not probe.validated_session:
                    raise ViewerError(422, "关闭后登录态验证未通过，账号保持停用")
            expected = self.tokens._refresh_expected(token, token)
            if not await self.tokens.db.update_token_if_credentials_match(
                token_id, expected, **probe.validated_session
            ):
                raise ViewerError(409, "账号配置已变化，请在账号列表核对")
            await manager.mark_verified(token.email)
            token = await self.tokens.db.get_token(token_id)
            expected = self.tokens._refresh_expected(token, token)
            expected.update(
                is_active=False,
                ban_reason="profile_login",
                auto_refresh_enabled=False,
                at=token.at,
                at_expires=token.at_expires,
                browser_profile_source="cloak",
            )
            try:
                await self.tokens._flow_call_for_token(
                    token, lambda: self.tokens.ensure_project_exists(token_id)
                )
            except Exception:
                raise ViewerError(503, "项目准备未通过，账号保持停用，请稍后重试") from None
            if not await self.tokens.db.update_token_if_credentials_match(
                token_id,
                expected,
                is_active=True,
                ban_reason=None,
                banned_at=None,
                image_enabled=True,
                video_enabled=True,
                auto_refresh_enabled=True,
                remark="Cloak 直接登录",
                browser_profile_status="ready",
                browser_profile_error="",
                browser_profile_verified_at=datetime.now(timezone.utc),
            ):
                raise ViewerError(409, "账号配置已变化，已保留当前启用设置")
            self.tokens.notify_profile_ready(token_id)
            pending["result"] = {
                "success": True,
                "session_id": session_id,
                "token_id": token_id,
                "email": token.email,
                "credits": token.credits,
                "status": "ready",
                "is_active": True,
                "message": "账号已添加，关闭后登录态验证通过，已参与生成调度",
            }
            return pending["result"]

    async def cancel(self, session_id: str, owner: str, viewer_id: str) -> dict:
        async with self._guard, self.viewer._switch_guard:
            pending = self._pending(session_id, owner, viewer_id)
            await self._discard(session_id, pending, delete=True)
            return {"success": True, "status": "closed"}

    async def _discard(self, session_id: str, pending: dict, *, delete: bool = True) -> None:
        if "result" not in pending:
            token_id = pending["token_id"]
            if self.viewer._token_id == token_id:
                try:
                    await self.viewer.close(token_id, pending["owner"], pending.get("viewer_id"))
                except ViewerError:
                    if delete:
                        raise
            manager = self.tokens.profile_refresher.manager
            if (
                delete
                and manager._normalize_account_key(pending["email"]) in manager._failed_handles
            ):
                raise ViewerError(503, "账号浏览器尚未确认关闭，已保留登录数据")
            token = await self.tokens.db.get_token(token_id)
            if delete and token and not token.is_active and token.ban_reason == "profile_login":
                await self.tokens.delete_token(token_id)
        if delete:
            self._sessions.pop(session_id, None)
        timer = pending.get("timer")
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _expire(self, session_id: str) -> None:
        pending = self._sessions[session_id]
        await asyncio.sleep(max(0, pending["expires_at"] - time.time()))
        async with self._guard, self.viewer._switch_guard:
            if self._sessions.get(session_id) is pending:
                try:
                    await self._discard(session_id, pending, delete=False)
                except ViewerError:
                    # An unconfirmed close retains ownership and the pending row.
                    pass

    async def close_all(self) -> None:
        async with self._guard, self.viewer._switch_guard:
            for session_id, pending in list(self._sessions.items()):
                await self._discard(session_id, pending, delete=False)
