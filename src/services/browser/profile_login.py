"""Owner-bound direct Google login using the ordinary account Profile viewer."""

import asyncio
import re
import secrets
import time
from datetime import datetime, timezone

from ...core.account_identity import same_google_identity
from ...core.models import Token
from ..proxy_manager import ProxyManager
from .profile_viewer import ViewerError
from .token_account_probe import TokenManagerAccountProbe

_LOGIN_REASON_MESSAGES = {
    "login_required": "Google 登录态未完成，请在浏览器中完成登录后重试",
    "session_invalid": "Flow 登录态校验未通过，请在浏览器中完成登录后重试",
    "identity_mismatch": "浏览器中的 Google 账号与所填邮箱不一致",
    "credits_invalid": "Flow 额度接口校验未通过，请稍后重试",
    "proxy_error": "账号代理连接失败，请检查代理后重试",
    "invalid_proxy": "账号代理配置无效，请检查代理后重试",
    "transient_error": "Google 或 Flow 网络请求暂时失败，请稍后重试",
    "account_probe_error": "账号接口校验失败，请检查代理后重试",
    "config_error": "账号 Profile 配置未通过校验，请检查后重试",
}
_LOGIN_STAGES = {"session_probe", "credits_probe", "prepare_workspace"}
_LOGIN_NETWORK_STEPS = {
    "flow_bootstrap",
    "flow_identity",
    "flow_credits",
    "flow_projects",
}


class DirectProfileLogin:
    def __init__(self, viewer):
        self.viewer = viewer
        self.tokens = viewer.token_manager
        self._guard = asyncio.Lock()
        self._sessions = {}

    @staticmethod
    def _failure_detail(
        reason: str,
        *,
        stage: str,
        network_step: str = "",
        retryable: bool = True,
        browser_open: bool = False,
    ) -> dict:
        reason = reason if reason in _LOGIN_REASON_MESSAGES else "transient_error"
        stage = stage if stage in _LOGIN_STAGES else "session_probe"
        detail = {
            "error_code": "profile_login_verification_failed",
            "reason": reason,
            "stage": stage,
            "message": _LOGIN_REASON_MESSAGES[reason],
            "retryable": bool(retryable),
            "browser_open": bool(browser_open),
        }
        if network_step in _LOGIN_NETWORK_STEPS:
            detail["network_step"] = network_step
        return detail

    @classmethod
    def _refresh_failure(cls, token, *, browser_open: bool) -> ViewerError:
        reason = str(
            getattr(token, "refresh_reason", "")
            or (
                "login_required"
                if getattr(token, "refresh_status", "") == "login_required"
                else "transient_error"
            )
        )
        stage = str(getattr(token, "refresh_stage", "") or "session_probe")
        network = str(getattr(token, "refresh_network_step", "") or "")
        return ViewerError(
            422,
            _LOGIN_REASON_MESSAGES.get(reason, _LOGIN_REASON_MESSAGES["transient_error"]),
            detail=cls._failure_detail(
                reason,
                stage=stage,
                network_step=network,
                browser_open=browser_open,
            ),
        )

    @classmethod
    def _probe_failure(cls, token, reason: str, *, network_step: str = "") -> ViewerError:
        stage = (
            "credits_probe"
            if network_step == "flow_credits" or reason == "credits_invalid"
            else "session_probe"
        )
        # Keep diagnostics available to the next retry and to the account list.
        detail = cls._failure_detail(
            reason,
            stage=stage,
            network_step=network_step,
            browser_open=False,
        )
        return ViewerError(422, detail["message"], detail=detail)

    async def start(
        self,
        email: str = "",
        proxy_url: str = "",
        owner: str = "",
        *,
        proxy_pool_id: int | None = None,
    ) -> dict:
        email = email.strip().casefold()
        requested_email = email
        anonymous = not bool(email)
        if not owner:
            raise ViewerError(401, "请先登录管理页")
        if email and (
            len(email) > 254
            or not re.fullmatch(r"[^\s@/\\]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email)
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
            profile = manager.root_dir / manager._normalize_account_key(email) if email else None
            existing = await self.tokens.db.get_token_by_email(email) if email else None
            resumed = bool(
                existing
                and not existing.is_active
                and existing.ban_reason == "profile_login"
                and existing.browser_profile_source == "cloak"
            )
            if existing and not resumed:
                raise ViewerError(409, "账号已存在，请在账号列表打开其浏览器")
            if profile is not None and profile.exists() and any(profile.iterdir()) and not resumed:
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
            if anonymous:
                # The temporary key owns the Profile until the browser session
                # supplies the real Google identity.
                email = f"profile-pending-{secrets.token_hex(12)}@local.invalid"
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
            pending = {
                "token_id": token_id,
                "email": email,
                "requested_email": requested_email,
                "anonymous": anonymous,
                "owner": owner,
                "status": "waiting",
                "message": "请在 Cloak 浏览器中完成 Google 登录，系统会自动添加账号",
            }
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
                pending["monitor"] = asyncio.create_task(self._monitor(session_id))
                return {
                    "success": True,
                    **state,
                    "session_id": session_id,
                    "email": requested_email,
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

    def status(self, session_id: str, owner: str, viewer_id: str = "") -> dict:
        pending = self._sessions.get(session_id)
        if pending is None:
            raise ViewerError(404, "登录会话已结束，请重新打开")
        if pending.get("owner") != owner:
            raise ViewerError(403, "此登录会话属于其他管理员")
        if viewer_id and pending.get("viewer_id") != viewer_id:
            raise ViewerError(409, "登录浏览器已切换，请重新打开")
        result = pending.get("result")
        return {
            "success": True,
            "session_id": session_id,
            "status": "ready" if result else pending.get("status", "waiting"),
            "message": pending.get("message", ""),
            "email": (result or {}).get("email") or pending.get("requested_email", ""),
            "viewer_id": pending.get("viewer_id", ""),
            "expires_at": pending.get("expires_at"),
            "result": result,
            "error": pending.get("error"),
            "browser_open": self.viewer.matches(
                pending["token_id"], owner, pending.get("viewer_id")
            ),
            "retryable": True,
        }

    async def retry(self, session_id: str, owner: str, viewer_id: str) -> dict:
        async with self._guard, self.viewer._switch_guard:
            pending = self._pending(session_id, owner, viewer_id)
            if pending.get("result"):
                return pending["result"]
            if self.viewer._task is None or not self.viewer.matches(
                pending["token_id"], owner, viewer_id
            ):
                token_id = pending["token_id"]
                state = await self.viewer._open(token_id, owner)
                pending.update(
                    viewer_id=state["viewer_id"], expires_at=state["expires_at"], closed=False
                )
            state = self.viewer.state(pending["token_id"], owner)
            pending["status"] = "waiting"
            pending.pop("error", None)
            pending["message"] = "请在 Cloak 浏览器中完成 Google 登录，系统会自动添加账号"
            monitor = pending.get("monitor")
            if monitor is None or monitor.done():
                pending["monitor"] = asyncio.create_task(self._monitor(session_id))
            return {
                "success": True,
                **state,
                "session_id": session_id,
                "email": pending.get("requested_email", ""),
                "viewer_url": f"/api/profile-browser/{pending['token_id']}",
            }

    async def complete(self, session_id: str, owner: str, viewer_id: str) -> dict:
        if (self._sessions.get(session_id) or {}).get("anonymous"):
            return await self._complete_anonymous(session_id, owner, viewer_id)
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
                result = await self.viewer.refresh(
                    token_id, owner, viewer_id, accept_existing_session=True
                )
                if not result["success"]:
                    latest = await self.tokens.db.get_token(token_id)
                    raise self._refresh_failure(latest or token, browser_open=True)
                await self.viewer.close(token_id, owner, viewer_id)
                pending["closed"] = True
            if self.viewer._token_id == token_id and self.viewer._task is not None:
                raise ViewerError(409, "请先关闭该账号浏览器再完成验证")
            # A fresh context must prove persistence after normal browser close.
            token = await self.tokens.db.get_token(token_id)
            refresher = self.tokens.profile_refresher
            manager = refresher.manager
            proxy = await refresher._proxy_url(token)
            probe = TokenManagerAccountProbe(self.tokens, token)
            probe_expected = self.tokens._refresh_expected(token, token)
            async with manager.credential_session(
                token.email, refresher.verifier, proxy_url=proxy, existing_only=True
            ) as context:
                probe.set_context(context)
                verification = await refresher.verifier.verify_account(token.email, probe)
                if verification.reason != "account_ready" or not probe.validated_session:
                    reason = verification.reason or "account_probe_error"
                    await self.tokens.db.update_token_if_credentials_match(
                        token_id,
                        probe_expected,
                        refresh_reason=reason,
                        refresh_stage="credits_probe"
                        if probe.network_step == "flow_credits" or reason == "credits_invalid"
                        else "session_probe",
                        refresh_network_step=probe.network_step
                        if probe.network_step in _LOGIN_NETWORK_STEPS
                        else "",
                        browser_profile_status="login_required"
                        if reason in {"login_required", "session_invalid"}
                        else "error",
                        browser_profile_error=_LOGIN_REASON_MESSAGES.get(
                            reason, _LOGIN_REASON_MESSAGES["account_probe_error"]
                        ),
                    )
                    raise self._probe_failure(token, reason, network_step=probe.network_step)
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
                detail = self._failure_detail(
                    "transient_error",
                    stage="prepare_workspace",
                    retryable=True,
                    browser_open=False,
                )
                raise ViewerError(503, detail["message"], detail=detail) from None
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
                refresh_status="ready",
                refresh_error="",
                refresh_reason="",
                refresh_stage="",
                refresh_network_step="",
                refresh_failures=0,
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
                "message": "账号已添加，关闭后 Flow 登录态验证通过",
            }
            return pending["result"]

    async def _complete_anonymous(self, session_id: str, owner: str, viewer_id: str) -> dict:
        async with self._guard, self.viewer._switch_guard:
            pending = self._pending(session_id, owner, viewer_id)
            if pending.get("result"):
                return pending["result"]
            token = await self.tokens.db.get_token(pending["token_id"])
            if (
                token is None
                or token.email != pending["email"]
                or token.is_active
                or token.ban_reason != "profile_login"
            ):
                raise ViewerError(409, "登录会话对应账号已变化")
            if not self.viewer.matches(token.id, owner, viewer_id):
                raise ViewerError(409, "登录浏览器尚未就绪")
            refresher = self.tokens.profile_refresher
            manager = refresher.manager
            async with self.viewer.borrow_context(token) as context:
                if context is None:
                    raise ViewerError(409, "登录浏览器尚未就绪")
                probe = TokenManagerAccountProbe(self.tokens, token, discover_identity=True)
                probe.set_context(context)
                verification = await probe.verify("")
                if (
                    verification.get("reason")
                    or not probe.validated_session
                    or not probe.observed_email
                ):
                    reason = verification.get("reason", "session_invalid")
                    pending["status"] = (
                        "waiting" if reason in {"session_invalid", "login_required"} else "error"
                    )
                    pending["message"] = (
                        "请在 Cloak 浏览器中完成 Google 登录"
                        if pending["status"] == "waiting"
                        else "谷歌会话或额度请求暂时失败，正在自动重试"
                    )
                    raise ViewerError(422, pending["message"])
                observed_email = probe.observed_email
            for existing in await self.tokens.db.get_all_tokens():
                if existing.id != token.id and same_google_identity(existing.email, observed_email):
                    raise ViewerError(409, "该 Google 账号已存在，请打开账号列表中的 Profile")
            pending["status"] = "verifying"
            pending["message"] = "已识别 Google 账号，正在保存并复开验证 Profile"
            await self.viewer.close(token.id, owner, viewer_id)
            pending["closed"] = True
            old_email = token.email
            await manager.adopt_identity(old_email, observed_email)
            try:
                expected = self.tokens._refresh_expected(token, token)
                if not await self.tokens.db.update_token_if_credentials_match(
                    token.id,
                    expected,
                    email=observed_email,
                    login_account=observed_email,
                    name=observed_email.split("@", 1)[0],
                    **probe.validated_session,
                ):
                    raise ViewerError(409, "账号配置已变化，请重新打开登录")
            except BaseException:
                await asyncio.shield(manager.adopt_identity(observed_email, old_email))
                raise
            pending.update(email=observed_email, requested_email=observed_email, anonymous=False)
        return await self.complete(session_id, owner, viewer_id)

    async def _monitor(self, session_id: str) -> None:
        while True:
            await asyncio.sleep(3)
            pending = self._sessions.get(session_id)
            if (
                pending is None
                or pending.get("result")
                or pending.get("expires_at", 0) <= time.time()
            ):
                return
            try:
                await self.complete(session_id, pending["owner"], pending.get("viewer_id", ""))
            except ViewerError as error:
                if error.status in {401, 403, 404, 409, 410}:
                    pending.update(status="error", error=error.message, message=error.message)
                    return
                pending["message"] = error.message
                pending["status"] = (
                    "waiting" if error.status == 422 and not pending.get("closed") else "error"
                )
            except Exception:
                pending.update(status="error", message="登录验证暂时失败，正在自动重试")

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
                await self.tokens._delete_token(token_id)
        if delete:
            self._sessions.pop(session_id, None)
        timer = pending.get("timer")
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        monitor = pending.get("monitor")
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()

    def forget_token(self, token_id: int) -> None:
        """Drop only in-memory login receipts after the account was explicitly deleted."""
        for session_id, pending in list(self._sessions.items()):
            if pending["token_id"] != token_id:
                continue
            self._sessions.pop(session_id, None)
            timer = pending.get("timer")
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
            monitor = pending.get("monitor")
            if monitor is not None and monitor is not asyncio.current_task():
                monitor.cancel()

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
