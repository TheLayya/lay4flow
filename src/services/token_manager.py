"""Token manager for lay4flow with AT auto-refresh"""

import asyncio
import inspect
import re
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..core.account_identity import same_google_identity
from ..core.config import config
from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import Project, Token
from ..core.monitoring import record_token_refresh
from .flow.retry import is_proxy_connection_error, is_retryable_network_error
from .flow_client import FlowClient
from .project_pool import ProjectPool
from .proxy_manager import AccountProxyUnavailable, ProxyManager
from .proxy_urls import normalize_proxy_url


class RefreshFailure(Exception):
    def __init__(self, message: str, code: str = "transient_error", *, validation_stage=None):
        super().__init__(message)
        self.code = code
        self.validation_stage = validation_stage


class TokenManager:
    """Token lifecycle manager with AT auto-refresh"""

    def __init__(self, db: Database, flow_client: FlowClient, profile_refresher: Any = None):
        self.db = db
        self.flow_client = flow_client
        self.profile_refresher = profile_refresher
        self.profile_ready_callback = None
        self.profile_delete_handler = None
        self.account_delete_handler = None
        self.account_proxy_maintenance = None
        self._refresh_lock_guard = asyncio.Lock()
        self._project_lock_guard = asyncio.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._profile_cookie_locks: dict[int, asyncio.Lock] = {}
        self._project_pool = ProjectPool(db, flow_client, lambda: config.personal_project_pool_size)
        # Compatibility alias for callers that used the old private map.
        self._project_locks = self._project_pool._locks
        self._refresh_futures: dict[int, asyncio.Task] = {}
        self._refresh_intents: dict[int, dict[str, bool]] = {}
        self._at_validation_cache: dict[int, tuple[float, Optional[tuple[Any, ...]]]] = {}
        self._protocol_refresher_task: Optional[asyncio.Task] = None

    async def _get_token_lock(
        self,
        lock_map: dict[int, asyncio.Lock],
        guard: asyncio.Lock,
        token_id: int,
    ) -> asyncio.Lock:
        """按 token 维度获取锁，避免不同 token 之间串行阻塞。"""
        async with guard:
            lock = lock_map.get(token_id)
            if lock is None:
                lock = asyncio.Lock()
                lock_map[token_id] = lock
            return lock

    def notify_profile_ready(self, token_id: int) -> None:
        """Optional provider discovery must never fail a verified Flow account."""
        if self.profile_ready_callback is not None:
            try:
                self.profile_ready_callback(token_id)
            except Exception as error:
                debug_logger.log_warning(
                    f"[PROFILE_READY] Token {token_id}: {type(error).__name__}"
                )

    def _get_project_pool_size(self) -> int:
        """读取当前生效的单 Token 项目池大小配置。"""
        try:
            return self._project_pool.pool_size()
        except Exception:
            return 4

    def _sort_projects(self, projects: List[Project]) -> List[Project]:
        """Sort projects in a stable order for round-robin selection."""
        return self._project_pool.sort_projects(projects)

    def _normalize_project_name_base(self, project_name: Optional[str] = None) -> str:
        """Normalize a project base name for pooled creation."""
        return self._project_pool.normalize_project_name_base(project_name)

    def _build_project_name(self, pool_index: int, base_name: Optional[str] = None) -> str:
        """Build a project name for the pool."""
        return self._project_pool.build_project_name(pool_index, base_name)

    def _normalize_protocol_mode(self, value: Optional[str]) -> str:
        mode = (value or "session").strip().lower()
        return "protocol" if mode == "protocol" else "session"

    @staticmethod
    def _normalize_browser_profile_source(value: Optional[str]) -> str:
        source = str(value or "cookies").strip().lower()
        if source not in {"cookies", "cloak"}:
            raise ValueError("browser_profile_source must be cookies or cloak")
        return source

    def _normalize_refresh_interval(self, value: Optional[int]) -> int:
        try:
            return max(1, int(value if value is not None else 120))
        except Exception:
            return 120

    def _parse_at_expires(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _as_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if not value:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _normalize_token_proxy_url(self, proxy_url: Optional[str]) -> str:
        raw = (proxy_url or "").strip()
        if not raw:
            return ""
        proxy_manager = getattr(self.flow_client, "proxy_manager", None)
        if proxy_manager and hasattr(proxy_manager, "normalize_proxy_url"):
            normalized = proxy_manager.normalize_proxy_url(raw)
            return normalized or ""
        return normalize_proxy_url(raw) or ""

    async def resolve_account_proxy(self, token: Token) -> Optional[str]:
        proxy_manager = getattr(self.flow_client, "proxy_manager", None)
        resolver = getattr(proxy_manager, "resolve_account_proxy", None)
        if inspect.iscoroutinefunction(resolver):
            return await resolver(token)
        own_proxy = normalize_proxy_url(getattr(token, "proxy_url", None))
        if own_proxy:
            return own_proxy
        get_global = getattr(proxy_manager, "get_request_proxy_url", None)
        if inspect.iscoroutinefunction(get_global):
            return await get_global()
        return await ProxyManager(self.db).resolve_account_proxy(token)

    async def _select_account_proxy(self, proxy_pool_id, proxy_url):
        proxy_manager = getattr(self.flow_client, "proxy_manager", None)
        selector = getattr(proxy_manager, "select_account_proxy", None)
        if inspect.iscoroutinefunction(selector):
            return await selector(proxy_pool_id, proxy_url)
        return proxy_pool_id, self._normalize_token_proxy_url(proxy_url)

    async def _flow_call_for_token(self, token: Token, call):
        proxy_url = await self.resolve_account_proxy(token)
        context = (
            self.flow_client.account_request_proxy(proxy_url)
            if "account_request_proxy" in dir(self.flow_client)
            else nullcontext()
        )
        with context:
            return await call()

    def _clear_at_validation_cache(self, token_id: int) -> None:
        self._at_validation_cache.pop(int(token_id), None)

    @staticmethod
    def _profile_validation_key(token: Token) -> tuple[Any, ...]:
        return tuple(
            getattr(token, key, None)
            for key in (
                "email",
                "login_account",
                "st",
                "google_cookies",
                "at",
                "at_expires",
                "proxy_pool_id",
                "proxy_url",
                "browser_profile_source",
                "browser_profile_epoch",
            )
        )

    def _mark_at_valid(
        self, token_id: int, ttl_seconds: int = 300, token: Optional[Token] = None
    ) -> None:
        key = self._profile_validation_key(token) if token is not None else None
        self._at_validation_cache[int(token_id)] = (
            time.monotonic() + max(30, int(ttl_seconds or 300)),
            key,
        )

    def _has_recent_at_validation(self, token_id: int, token: Optional[Token] = None) -> bool:
        value = self._at_validation_cache.get(int(token_id))
        if not value:
            return False
        expires, key = value
        if expires <= time.monotonic() or (
            key is not None and token is not None and key != self._profile_validation_key(token)
        ):
            self._clear_at_validation_cache(token_id)
            return False
        return True

    def is_profile_validation_recent(self, token: Optional[Token]) -> bool:
        """Return true only for the same full Profile credential/proxy snapshot."""
        value = (
            self._at_validation_cache.get(int(token.id)) if token and token.id is not None else None
        )
        return bool(
            token
            and token.browser_profile_source == "cloak"
            and value
            and value[1] is not None
            and token.refresh_status not in {"config_error", "login_required", "retrying"}
            and self._has_recent_at_validation(token.id, token)
        )

    def mark_profile_validated(self, token: Optional[Token]) -> None:
        """Mark a just-probed Profile snapshot; callers must have verified it first."""
        if token and token.id is not None and token.browser_profile_source == "cloak":
            self._mark_at_valid(token.id, token=token)

    async def _st_to_at_for_token(self, token: Token, st: str) -> Dict[str, Any]:
        if token.browser_profile_source == "cloak":
            raise RefreshFailure(
                "Cloak Profile requires browser session verification", "config_error"
            )
        return await self._flow_call_for_token(token, lambda: self.flow_client.st_to_at(st))

    async def _get_credits_for_token(self, token: Token, at: Optional[str]) -> Dict[str, Any]:
        if token.browser_profile_source == "cloak":
            if not await self._refresh_at(token.id, force=True):
                latest = await self.db.get_token(token.id)
                raise RefreshFailure(
                    getattr(latest, "refresh_error", "") or "Cloak Profile verification failed",
                    getattr(latest, "refresh_reason", "") or "transient_error",
                )
            latest = await self.db.get_token(token.id)
            if latest is None:
                raise RefreshFailure(
                    "Account changed during Profile verification", "transient_error"
                )
            return {"credits": latest.credits, "userPaygateTier": latest.user_paygate_tier}
        return await self._flow_call_for_token(token, lambda: self.flow_client.get_credits(at))

    async def get_personal_warmup_project_ids(
        self,
        tokens: Optional[List[Token]] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        """返回 personal 模式启动时建议预热的项目 ID 列表。"""
        token_list = tokens if tokens is not None else await self.get_all_tokens()
        return await self._project_pool.get_personal_warmup_project_ids(
            token_list,
            limit,
            sort_projects=self._sort_projects,
        )

    async def _create_project_for_token(
        self, token: Token, pool_index: int, base_name: Optional[str] = None
    ) -> Project:
        """Create a new pooled project for a token and persist it."""
        if token.browser_profile_source == "cloak":
            from .browser.flow_projects import create_profile_project

            return await create_profile_project(
                self, token, self._build_project_name(pool_index, base_name)
            )
        return await self._flow_call_for_token(
            token,
            lambda: self._project_pool.create_project_for_token(
                token,
                pool_index,
                base_name,
                build_name=self._build_project_name,
            ),
        )

    def _select_next_project(self, token: Token, projects: List[Project]) -> Project:
        """Select the next project from the pool in round-robin order."""
        return self._project_pool.select_next_project(token, projects)

    # ========== Token CRUD ==========

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        return await self.db.get_all_tokens()

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        return await self.db.get_active_tokens()

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        return await self.db.get_token(token_id)

    async def delete_token(self, token_id: int):
        """Close account viewers before deleting their persistent Profile."""
        if self.profile_delete_handler is not None:
            return await self.profile_delete_handler(token_id)
        await self._delete_token(token_id)

    async def _delete_token(self, token_id: int):
        """Delete a token after its interactive browser lease has been closed."""
        if self.account_delete_handler is not None:
            await self.account_delete_handler(token_id)
        token = await self.db.get_token(token_id)
        project_ids: List[str] = []
        if token:
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                project_ids.append(current_project_id)

        profile_refresher = self.profile_refresher
        profile_email = str(token.email).strip() if token else ""
        profile_source = token.browser_profile_source if token else "cookies"

        for project in await self.db.get_projects_by_token(token_id):
            project_id = str(project.project_id or "").strip()
            if project_id and project_id not in project_ids:
                project_ids.append(project_id)

        refresh_task = self._refresh_futures.pop(token_id, None)
        self._refresh_intents.pop(token_id, None)
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if profile_refresher is not None and profile_email and profile_source == "cloak":
            await profile_refresher.remove_profile(profile_email)

        await self.db.delete_token(token_id)

        self._refresh_locks.pop(token_id, None)
        self._profile_cookie_locks.pop(token_id, None)
        self._clear_at_validation_cache(token_id)
        self._project_pool.clear_token(token_id)

    async def enable_token(self, token_id: int):
        """Enable a token and reset error count"""
        token = await self.db.get_token(token_id)
        expected = None
        if token and token.refresh_status in {"config_error", "login_required"}:
            valid = await self._refresh_at(
                token_id, force=True, refresh_st=token.protocol_mode == "protocol"
            )
            if not valid:
                raise ValueError("认证尚未恢复，请更新 Cookie 后重试刷新")
        # Enable the token
        latest = await self.db.get_token(token_id)
        if not latest:
            raise ValueError("账号状态已变化，请等待恢复结果")
        if expected is None:
            expected = self._refresh_expected(latest, latest)
            expected["refresh_status"] = latest.refresh_status
        updated = await self.db.update_token_if_credentials_match(
            token_id,
            expected,
            is_active=True,
            ban_reason=None,
            banned_at=None,
            refresh_status="idle",
            refresh_error="",
            refresh_failures=0,
            next_refresh_at=datetime.now(timezone.utc),
        )
        if not updated:
            raise ValueError("账号状态已变化，请稍后重试")
        # Reset error count when enabling (only reset total error_count, keep today_error_count)
        await self.db.reset_error_count(token_id)

    async def disable_token(self, token_id: int, reason: str = "manual"):
        """Disable a token"""
        await self.db.update_token(
            token_id,
            is_active=False,
            ban_reason=reason,
            banned_at=datetime.now(timezone.utc),
        )

    # ========== Token添加 (支持Project创建) ==========

    async def add_token(
        self,
        st: str,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: bool = True,
        video_enabled: bool = True,
        image_concurrency: int = -1,
        video_concurrency: int = -1,
        captcha_proxy_url: Optional[str] = None,
        protocol_mode: str = "session",
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: bool = True,
        refresh_interval_minutes: int = 120,
        browser_profile_source: str = "cookies",
        proxy_pool_id: Optional[int] = None,
        submission_proxy_mode: str = "fixed",
    ) -> Token:
        """Add a new token and prepare its pooled projects."""
        if self._normalize_browser_profile_source(browser_profile_source) == "cloak":
            raise ValueError("Cloak Profile must be initialized through direct browser login")
        existing_token = await self.db.get_token_by_st(st)
        if existing_token:
            raise ValueError(f"Token ??????: {existing_token.email}?")

        if proxy_pool_id is not None or proxy_url:
            proxy_pool_id, proxy_url = await self._select_account_proxy(proxy_pool_id, proxy_url)
        validation_token = Token(
            st=st, email="", proxy_url=proxy_url or "", proxy_pool_id=proxy_pool_id
        )

        debug_logger.log_info("[ADD_TOKEN] Converting ST to AT...")
        try:
            result = await self._st_to_at_for_token(validation_token, st)
            at = result["access_token"]
            expires = result.get("expires")
            user_info = result.get("user", {})
            email = user_info.get("email", "")
            name = user_info.get("name", email.split("@")[0] if email else "")
            st = result.get("_session_token") or st
            at_expires = None
            if expires:
                try:
                    at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except Exception:
                    pass
        except Exception as e:
            raise ValueError(f"ST?AT??: {str(e)}")

        try:
            credits_result = await self._get_credits_for_token(validation_token, at)
            credits = credits_result.get("credits", 0)
            user_paygate_tier = credits_result.get("userPaygateTier")
        except Exception:
            credits = 0
            user_paygate_tier = None

        base_project_name = self._normalize_project_name_base(project_name)
        project_pool_size = self._get_project_pool_size()
        pooled_projects: List[Project] = []

        if project_id:
            first_project_name = self._build_project_name(1, base_project_name)
            debug_logger.log_info(
                f"[ADD_TOKEN] Using provided project_id as pooled project #1: {project_id}"
            )
            pooled_projects.append(
                Project(
                    project_id=project_id,
                    token_id=0,
                    project_name=first_project_name,
                    tool_name="PINHOLE",
                )
            )
        else:
            try:
                first_project_name = self._build_project_name(1, base_project_name)
                first_project_id = await self._flow_call_for_token(
                    validation_token,
                    lambda: self.flow_client.create_project(st, first_project_name),
                )
                debug_logger.log_info(
                    f"[ADD_TOKEN] Created pooled project #1: {first_project_name} (ID: {first_project_id})"
                )
                pooled_projects.append(
                    Project(
                        project_id=first_project_id,
                        token_id=0,
                        project_name=first_project_name,
                        tool_name="PINHOLE",
                    )
                )
            except Exception as e:
                raise ValueError(f"??????: {str(e)}")

        token = Token(
            st=st,
            at=at,
            at_expires=at_expires,
            email=email,
            name=name,
            remark=remark,
            is_active=True,
            credits=credits,
            user_paygate_tier=user_paygate_tier,
            current_project_id=pooled_projects[0].project_id,
            current_project_name=pooled_projects[0].project_name,
            image_enabled=image_enabled,
            video_enabled=video_enabled,
            image_concurrency=image_concurrency,
            video_concurrency=video_concurrency,
            captcha_proxy_url=captcha_proxy_url,
            protocol_mode=self._normalize_protocol_mode(protocol_mode),
            google_cookies=(google_cookies or "").strip(),
            login_account=(login_account or "").strip(),
            login_password=login_password or "",
            proxy_url=self._normalize_token_proxy_url(proxy_url),
            proxy_pool_id=proxy_pool_id,
            submission_proxy_mode=submission_proxy_mode,
            auto_refresh_enabled=bool(auto_refresh_enabled),
            refresh_interval_minutes=self._normalize_refresh_interval(refresh_interval_minutes),
            browser_profile_source=self._normalize_browser_profile_source(browser_profile_source),
        )

        token_id = await self.db.add_token(token)
        token = await self.db.get_token(token_id)

        pooled_projects[0].token_id = token_id
        pooled_projects[0].id = await self.db.add_project(pooled_projects[0])

        while len(pooled_projects) < project_pool_size:
            new_project = await self._create_project_for_token(
                token, len(pooled_projects) + 1, base_project_name
            )
            pooled_projects.append(new_project)

        debug_logger.log_info(
            f"[ADD_TOKEN] Token added successfully (ID: {token_id}, Email: {email}, pooled_projects={len(pooled_projects)})"
        )
        return token

    async def update_token(
        self,
        token_id: int,
        st: Optional[str] = None,
        at: Optional[str] = None,
        at_expires: Optional[datetime] = None,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: Optional[bool] = None,
        video_enabled: Optional[bool] = None,
        image_concurrency: Optional[int] = None,
        video_concurrency: Optional[int] = None,
        captcha_proxy_url: Optional[str] = None,
        protocol_mode: Optional[str] = None,
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
        browser_profile_source: Optional[str] = None,
        proxy_pool_id: Optional[int] = None,
        submission_proxy_mode: Optional[str] = None,
    ):
        """Update token (支持修改project_id和project_name)

        当用户编辑保存token时，如果token未过期，自动清空429禁用状态
        """
        update_fields = {}

        if submission_proxy_mode is not None:
            if submission_proxy_mode not in {"fixed", "pool_random"}:
                raise ValueError("submission_proxy_mode must be fixed or pool_random")
            update_fields["submission_proxy_mode"] = submission_proxy_mode

        if st is not None:
            update_fields["st"] = st
        if at is not None:
            update_fields["at"] = at
        if at_expires is not None:
            update_fields["at_expires"] = at_expires
        if project_id is not None:
            update_fields["current_project_id"] = project_id
        if project_name is not None:
            update_fields["current_project_name"] = project_name
        if remark is not None:
            update_fields["remark"] = remark
        if image_enabled is not None:
            update_fields["image_enabled"] = image_enabled
        if video_enabled is not None:
            update_fields["video_enabled"] = video_enabled
        if image_concurrency is not None:
            update_fields["image_concurrency"] = image_concurrency
        if video_concurrency is not None:
            update_fields["video_concurrency"] = video_concurrency
        if captcha_proxy_url is not None:
            update_fields["captcha_proxy_url"] = captcha_proxy_url
        if protocol_mode is not None:
            update_fields["protocol_mode"] = self._normalize_protocol_mode(protocol_mode)
        if google_cookies is not None:
            update_fields["google_cookies"] = google_cookies.strip()
        if login_account is not None:
            update_fields["login_account"] = login_account.strip()
        if login_password is not None:
            update_fields["login_password"] = login_password
        if auto_refresh_enabled is not None:
            update_fields["auto_refresh_enabled"] = bool(auto_refresh_enabled)
        if refresh_interval_minutes is not None:
            update_fields["refresh_interval_minutes"] = self._normalize_refresh_interval(
                refresh_interval_minutes
            )
        if browser_profile_source is not None:
            update_fields["browser_profile_source"] = self._normalize_browser_profile_source(
                browser_profile_source
            )

        token = await self.db.get_token(token_id)
        if proxy_pool_id is not None or proxy_url is not None:
            selected_id, selected_url = await self._select_account_proxy(proxy_pool_id, proxy_url)
            if (
                token
                and token.browser_profile_source == "cloak"
                and self._normalize_token_proxy_url(selected_url)
                != self._normalize_token_proxy_url(token.proxy_url)
            ):
                if any(
                    key in update_fields and update_fields[key] != getattr(token, key)
                    for key in (
                        "st",
                        "at",
                        "at_expires",
                        "google_cookies",
                        "login_account",
                        "login_password",
                        "protocol_mode",
                        "browser_profile_source",
                    )
                ):
                    raise ValueError("请先单独切换账号代理，再编辑登录凭据")
                return await self._switch_profile_proxy(
                    token, selected_id, selected_url, update_fields
                )
            update_fields.update(proxy_pool_id=selected_id, proxy_url=selected_url or "")
        if (
            browser_profile_source is not None
            and self._normalize_browser_profile_source(browser_profile_source) == "cloak"
            and (not token or token.browser_profile_source != "cloak")
        ):
            raise ValueError("Cloak Profile must be initialized through direct browser login")

        # 检查token是否因429被禁用，如果是且未过期，则清空429状态
        refresh_material = (
            "st",
            "google_cookies",
            "protocol_mode",
            "proxy_url",
            "proxy_pool_id",
            "login_account",
            "browser_profile_source",
        )
        if token and any(
            key in update_fields and update_fields[key] != getattr(token, key)
            for key in refresh_material
        ):
            update_fields.update(
                refresh_status="idle",
                refresh_error="",
                refresh_failures=0,
                next_refresh_at=datetime.now(timezone.utc),
            )
            if token.browser_profile_source == "cloak":
                update_fields["browser_profile_epoch"] = uuid.uuid4().hex
        if token and token.ban_reason == "429_rate_limit":
            # 检查token是否过期
            is_expired = False
            if token.at_expires:
                now = datetime.now(timezone.utc)
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires
                is_expired = at_expires_aware <= now

            # 如果未过期，清空429禁用状态
            if not is_expired:
                debug_logger.log_info(f"[UPDATE_TOKEN] Token {token_id} 编辑保存，清空429禁用状态")
                update_fields["ban_reason"] = None
                update_fields["banned_at"] = None

        if update_fields:
            if any(key in update_fields for key in ("st", "at", "at_expires")):
                self._clear_at_validation_cache(token_id)
            await self.db.update_token(token_id, **update_fields)

    async def _switch_profile_proxy(self, token, selected_id, selected_url, update_fields):
        """Finish validation and rollback even if the initiating HTTP request disconnects."""
        if self.profile_refresher is None or self.account_proxy_maintenance is None:
            raise ValueError("Cloak Profile 账号代理切换服务尚未就绪")
        task = asyncio.create_task(
            self._switch_profile_proxy_inner(token, selected_id, selected_url, update_fields)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self.profile_refresher.manager._wait_uninterrupted(task)
            raise

    async def _switch_profile_proxy_inner(self, token, selected_id, selected_url, update_fields):
        from .browser.profile_session_refresher import ProfileSessionRefresher

        refresh_lock = await self._get_token_lock(
            self._refresh_locks, self._refresh_lock_guard, token.id
        )
        pending = self._refresh_futures.get(token.id)
        if refresh_lock.locked() or (pending is not None and not pending.done()):
            raise ValueError("账号正在刷新登录态，请完成后再切换代理")
        async with refresh_lock, self.account_proxy_maintenance(token, selected_url):
            latest = await self.db.get_token(token.id)
            if latest is None or latest.model_dump() != token.model_dump():
                raise ValueError("账号配置已变化，请刷新后重试")
            manager = self.profile_refresher.manager
            verifier = self.profile_refresher.verifier
            candidate = token.model_copy(
                update={"proxy_url": selected_url or "", "proxy_pool_id": selected_id}
            )
            first = True

            async def validate(staged_manager, context):
                nonlocal first
                refresher = ProfileSessionRefresher(staged_manager, verifier, self)
                result = await refresher.refresh_in_context(candidate, context, force=first)
                first = False
                values = self._profile_credential_updates(result)
                if result.get("google_cookies"):
                    values["google_cookies"] = result["google_cookies"]
                return values

            async def commit(validated):
                committed_id, committed_url = await self._select_account_proxy(
                    selected_id, selected_url
                )
                if committed_id != selected_id or self._normalize_token_proxy_url(
                    committed_url
                ) != self._normalize_token_proxy_url(selected_url):
                    raise ValueError("目标代理配置已变化，切换已回滚，请刷新代理池后重试")
                now = datetime.now(timezone.utc)
                values = {**update_fields, **validated}
                values.update(
                    proxy_pool_id=selected_id,
                    proxy_url=selected_url or "",
                    refresh_status="ready",
                    refresh_error="",
                    refresh_failures=0,
                    last_refresh_at=now,
                    last_st_refresh_at=now,
                    last_st_refresh_result="success",
                    browser_profile_status="ready",
                    browser_profile_error="",
                    browser_profile_verified_at=now,
                    browser_profile_failures=0,
                    next_refresh_at=now
                    + timedelta(
                        minutes=self._normalize_refresh_interval(token.refresh_interval_minutes)
                    ),
                    browser_profile_next_at=now
                    + timedelta(
                        minutes=self._normalize_refresh_interval(token.refresh_interval_minutes)
                    ),
                )
                if not await self.db.update_token_if_credentials_match(
                    token.id,
                    token.model_dump(exclude={"id"}),
                    expected_proxy_binding=(selected_id, selected_url)
                    if selected_id is not None
                    else None,
                    **values,
                ):
                    raise ValueError("账号配置已变化，代理切换已回滚，请刷新后重试")
                self._clear_at_validation_cache(token.id)
                self.mark_profile_validated(token.model_copy(update=values))

            try:
                await manager.switch_proxy(
                    token.email,
                    self._normalize_token_proxy_url(token.proxy_url),
                    selected_url or "",
                    verifier,
                    validate,
                    commit,
                )
            except ValueError:
                raise
            except Exception:
                raise ValueError(
                    "新代理校验失败，已保留原代理和登录态，请检查代理或账号验证状态"
                ) from None

    # ========== AT自动刷新逻辑 (核心) ==========

    def _profile_session_fresh(self, token: Token) -> bool:
        if token.browser_profile_status != "ready" or token.refresh_status in {
            "config_error",
            "login_required",
        }:
            return False
        verified = (
            self._as_utc(token.last_st_refresh_at)
            if self._profile_cycle_anchor_valid(token)
            else self._as_utc(token.browser_profile_verified_at)
            or (self._as_utc(token.last_refresh_at) if token.refresh_status == "ready" else None)
        )
        if verified is None:
            return False
        now = datetime.now(timezone.utc)
        interval = timedelta(
            minutes=self._normalize_refresh_interval(token.refresh_interval_minutes)
        )
        return verified <= now and now - verified < interval

    def _profile_cycle_anchor_valid(self, token: Token) -> bool:
        """Persist one modern-cycle marker without moving the renewal clock on probes."""
        anchor = self._as_utc(token.last_st_refresh_at)
        due = self._as_utc(token.browser_profile_next_at)
        return bool(
            token.browser_profile_source == "cloak"
            and token.last_st_refresh_result == "success"
            and token.at is None
            and token.at_expires is None
            and anchor
            and due
            and due
            == anchor
            + timedelta(minutes=self._normalize_refresh_interval(token.refresh_interval_minutes))
        )

    @staticmethod
    def _profile_credential_updates(result: Dict[str, Any]) -> Dict[str, Any]:
        validated = result.get("_validated_session")
        if not isinstance(validated, dict):
            raise RefreshFailure(
                "Cloak Profile did not return verified credentials", "transient_error"
            )
        credits = validated.get("credits")
        tier = validated.get("user_paygate_tier")
        if (
            type(credits) is not int
            or credits < 0
            or (tier is not None and not isinstance(tier, str))
        ):
            raise RefreshFailure("Cloak Profile returned invalid credits", "transient_error")
        # Google Flow browser sessions do not supply a legacy NextAuth ST/AT.
        return {"at": None, "at_expires": None, "credits": credits, "user_paygate_tier": tier}

    def _should_refresh_at(self, token: Token) -> bool:
        """根据当前 token 快照判断是否需要刷新 AT。"""
        if token.browser_profile_source == "cloak":
            return not self._profile_session_fresh(token)
        if not token.at:
            debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT不存在,需要刷新")
            return True

        if not token.at_expires:
            debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT过期时间未知,尝试刷新")
            return True

        now = datetime.now(timezone.utc)
        if token.at_expires.tzinfo is None:
            at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
        else:
            at_expires_aware = token.at_expires

        time_until_expiry = at_expires_aware - now
        if time_until_expiry.total_seconds() < 3600:
            debug_logger.log_info(
                f"[AT_CHECK] Token {token.id}: AT即将过期 "
                f"(剩余 {time_until_expiry.total_seconds():.0f} 秒),需要刷新"
            )
            return True

        return False

    def needs_at_refresh(self, token: Optional[Token]) -> bool:
        """供调度层快速判断当前 token 是否大概率会触发 AT 刷新。"""
        if not token:
            return True
        return self._should_refresh_at(token)

    async def ensure_valid_token(self, token: Optional[Token]) -> Optional[Token]:
        """确保 token 的 AT 可用，并在必要时返回刷新后的最新对象。"""
        if not token or not token.is_active:
            return None

        if getattr(token, "proxy_pool_id", None) is not None:
            try:
                await self.resolve_account_proxy(token)
            except AccountProxyUnavailable:
                return None

        if token.browser_profile_source == "cloak":
            token = await self.db.get_token(token.id)
            if token is None or not token.is_active or token.browser_profile_source != "cloak":
                return None
            if token.proxy_pool_id is not None:
                try:
                    await self.resolve_account_proxy(token)
                except AccountProxyUnavailable:
                    return None
            if (
                token.google_cookies.strip()
                and self._profile_session_fresh(token)
                and self.is_profile_validation_recent(token)
            ):
                return token
            if await self._refresh_at(token.id, validation_only=True):
                latest = await self.db.get_token(token.id)
                return (
                    latest
                    if latest and latest.is_active and self._profile_session_fresh(latest)
                    else None
                )
            return None

        at_rejected = False
        if not self._should_refresh_at(token):
            if self._has_recent_at_validation(token.id, token):
                return token
            try:
                credits = await self._get_credits_for_token(token, token.at)
                await self.db.update_token(
                    token.id,
                    credits=credits.get("credits", 0),
                    user_paygate_tier=credits.get("userPaygateTier"),
                )
                self._mark_at_valid(token.id, token=token)
                latest = await self.db.get_token(token.id)
                return latest if latest and latest.is_active else None
            except Exception as exc:
                if isinstance(exc, AccountProxyUnavailable):
                    return None
                if not self._is_auth_error(exc):
                    return token
                at_rejected = True

        if not await self._refresh_at(token.id, force=at_rejected):
            latest = await self.db.get_token(token.id)
            expires = self._as_utc(latest.at_expires) if latest else None
            # A failed early renewal must not discard a still usable credential.
            if (
                not at_rejected
                and latest
                and latest.is_active
                and latest.at
                and expires
                and expires > datetime.now(timezone.utc)
            ):
                return latest
            return None

        latest = await self.db.get_token(token.id)
        return latest if latest and latest.is_active else None

    async def is_at_valid(self, token_id: int, token: Optional[Token] = None) -> bool:
        """检查AT是否有效,如果无效或即将过期则自动刷新

        Returns:
            True if AT is valid or refreshed successfully
            False if AT cannot be refreshed
        """
        token_obj = token if token and token.id == token_id else await self.db.get_token(token_id)
        if not token_obj:
            return False

        valid_token = await self.ensure_valid_token(token_obj)
        return valid_token is not None

    @staticmethod
    def _is_auth_error(error: Exception) -> bool:
        if getattr(error, "code", None) in {"session_invalid", "invalid_cookies", "login_required"}:
            return True
        status = getattr(getattr(error, "response", None), "status_code", None)
        if isinstance(status, int):
            return status in (401, 403)
        text = re.sub(r"https?://\S+", "[URL]", str(error).lower())
        return bool(
            re.search(r"\bhttp(?:/\d(?:\.\d)?)?(?: error| status)?[: ]+(?:401|403)\b", text)
            or re.search(r"\b(?:401 unauthorized|403 forbidden)\b", text)
            or text.strip() in {"401", "403"}
            or re.search(
                r"\b(?:unauthenticated|credentials_missing|auth_error|grant_expired|access_token_refresh_needed)\b",
                text,
            )
            or re.search(r"\b(?:invalid|expired|missing) (?:session|access[_ ]token)\b", text)
        )

    @staticmethod
    def _is_temporary_refresh_error(error: Exception) -> bool:
        if TokenManager._is_auth_error(error):
            return False
        status = getattr(getattr(error, "response", None), "status_code", None)
        return (
            isinstance(error, (TimeoutError, ConnectionError))
            or (isinstance(status, int) and (status == 429 or 500 <= status < 600))
            or is_proxy_connection_error(error)
            or is_retryable_network_error(str(error))
            or bool(re.search(r"\bhttp(?: error| status)?[: ]+(?:429|5\d\d)\b", str(error).lower()))
        )

    @staticmethod
    def _refresh_expected(token: Token, latest: Token) -> Dict[str, Any]:
        # SQL compares credentials AND control flags at the moment of the write.
        expected = {
            key: getattr(token, key)
            for key in (
                "st",
                "google_cookies",
                "proxy_url",
                "proxy_pool_id",
                "protocol_mode",
                "login_account",
                "email",
                "browser_profile_source",
                "browser_profile_epoch",
            )
        }
        expected.update(is_active=latest.is_active, ban_reason=latest.ban_reason)
        return expected

    @staticmethod
    def _safe_refresh_error(error: Exception) -> str:
        message = re.sub(r"https?://\S+", "[URL]", str(error))
        message = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", message)
        message = re.sub(
            r"(?i)(cookie|authorization|password|token)\s*[:=]\s*\S+", r"\1=[redacted]", message
        )
        message = re.sub(r"[A-Za-z0-9_+/=-]{40,}", "[redacted]", message)
        return message[:240] or "刷新超时或上游暂时不可用"

    async def _validate_refresh_session(
        self, token: Token, st: str, *, session: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if token.browser_profile_source == "cloak":
            raise RefreshFailure(
                "Cloak Profile requires browser session verification", "config_error"
            )
        if session is None:
            session = await self._st_to_at_for_token(token, st)
        at = str(session.get("access_token") or "").strip()
        expires = self._as_utc(self._parse_at_expires(session.get("expires")))
        if not at or not expires or expires <= datetime.now(timezone.utc):
            raise RefreshFailure(
                "ST 未返回有效且未过期的 AT", "session_invalid", validation_stage="access_token"
            )
        user = session.get("user") or {}
        if not same_google_identity(user.get("email"), token.email):
            raise RefreshFailure(
                "刷新返回的账号与当前账号不一致，请检查 Cookie 所属账号",
                "invalid_cookies",
                validation_stage="identity",
            )
        # Do not restore a suspended account until the upstream accepts the AT.
        try:
            credits = await self._get_credits_for_token(token, at)
        except Exception as exc:
            if self._is_auth_error(exc):
                raise RefreshFailure(
                    str(exc) or "AT validation rejected",
                    "session_invalid",
                    validation_stage="credits",
                ) from exc
            failure = RefreshFailure(
                str(exc) or "AT validation failed",
                "transient_error",
                validation_stage="credits",
            )
            if self._is_temporary_refresh_error(exc):
                failure.credential_updates = {"st": session.get("_session_token") or st}
            raise failure from exc
        return {
            # The session endpoint can rotate its HttpOnly cookie independently
            # of the JSON access-token expiry. Persist it after validation only.
            "st": session.get("_session_token") or st,
            "at": at,
            "at_expires": expires,
            "credits": credits.get("credits", 0),
            "user_paygate_tier": credits.get("userPaygateTier"),
        }

    async def _try_protocol_refresh_st(self, token_id: int, token: Token) -> Dict[str, Any]:
        from .protocol_login import protocol_loginer

        if token.browser_profile_source == "cloak":
            raise RefreshFailure(
                "Cloak Profile requires browser session verification", "config_error"
            )
        try:
            proxy = await self.resolve_account_proxy(token)
        except ValueError as exc:
            raise RefreshFailure("代理地址无效，未发起协议刷新", "invalid_proxy") from exc

        result = await protocol_loginer.login(
            token.google_cookies,
            proxy=proxy,
            email=token.login_account or token.email or None,
        )
        if not result.get("success") or not result.get("session_token"):
            raise RefreshFailure(
                str(result.get("error") or "协议刷新失败"),
                result.get("error_code") or "transient_error",
            )
        return result

    async def _try_refresh_st(
        self, token_id: int, token: Token, *, force: bool = False
    ) -> Dict[str, Any]:
        if token.browser_profile_source == "cloak":
            return await self._try_profile_refresh(token, force=force)
        # Google Cookies are the refresh material regardless of the legacy UI
        # label.  A session-mode account with cookies must use the same OAuth
        # renewal path once its stored AT/ST is stale; otherwise it can only
        # replay the expired Labs session forever.
        if token.google_cookies.strip():
            return await self._try_protocol_refresh_st(token_id, token)
        if token.protocol_mode == "protocol":
            if not token.google_cookies.strip():
                raise RefreshFailure("未配置 Google Cookies", "invalid_cookies")
            return await self._try_protocol_refresh_st(token_id, token)
        raise RefreshFailure("ST 已失效，需要重新登录并同步 Cookie 或新的 ST", "login_required")

    async def _try_profile_refresh(self, token: Token, *, force: bool = False) -> Dict[str, Any]:
        from .browser.profiles import ProfileNotReadyError

        if self.profile_refresher is None:
            raise RefreshFailure("Cloak Profile 未配置", "config_error")
        try:
            if force:
                result = await asyncio.wait_for(
                    self.profile_refresher.refresh(token, force=True), 90
                )
            else:
                result = await asyncio.wait_for(self.profile_refresher.refresh(token), 90)
            result["_profile_used"] = True
            return result
        except RefreshFailure:
            raise
        except asyncio.TimeoutError as exc:
            raise RefreshFailure("Cloak Profile 校验超时", "transient_error") from exc
        except ProfileNotReadyError as exc:
            network_step = str(getattr(exc, "profile_network_step", "") or "")
            code = (
                "transient_error"
                if exc.reason in {"transient_error", "account_probe_error", "timeout"}
                else "login_required"
                if exc.reason in {"login_required", "session_invalid"}
                else "config_error"
            )
            message = (
                "服务器 Profile 需要重新登录或完成 Google 验证，请在账号列表打开该账号浏览器，登录后刷新凭据"
                if exc.reason == "login_required"
                else f"Cloak Profile 未通过校验: {exc.reason}"
            )
            failure = RefreshFailure(message, code)
            failure.profile_reason = exc.reason
            failure.profile_network_step = network_step
            failure.profile_stage = (
                "credits_probe"
                if network_step == "flow_credits" or exc.reason == "credits_invalid"
                else "session_probe"
            )
            raise failure from exc
        except Exception as exc:
            raise RefreshFailure("Cloak Profile 恢复异常", "transient_error") from exc

    async def _refresh_credentials(
        self, token: Token, refresh_st: bool, *, force: bool = False
    ) -> Dict[str, Any]:
        if token.browser_profile_source == "cloak":
            result = await self._try_profile_refresh(token, force=force)
            updates = self._profile_credential_updates(result)
            if result.get("google_cookies"):
                updates["google_cookies"] = result["google_cookies"]
            updates.update(
                browser_profile_status="ready",
                browser_profile_error="",
                browser_profile_verified_at=datetime.now(timezone.utc),
                browser_profile_failures=0,
                last_st_refresh_at=datetime.now(timezone.utc),
                last_st_refresh_result="success",
            )
            return updates
        if not refresh_st:
            try:
                current = await self._validate_refresh_session(token, token.st)
                if (
                    not token.google_cookies.strip()
                    or (current["at_expires"] - datetime.now(timezone.utc)).total_seconds() >= 3600
                ):
                    return current
            except Exception as exc:
                if getattr(exc, "credential_updates", None):
                    raise
                if isinstance(exc, RefreshFailure) and exc.code == "invalid_cookies":
                    raise
                auth_failed = isinstance(exc, RefreshFailure) and exc.code == "session_invalid"
                if token.refresh_status in {"config_error", "login_required"} and (
                    auth_failed or self._is_auth_error(exc)
                ):
                    code = (
                        "invalid_cookies"
                        if token.refresh_status == "config_error"
                        else "login_required"
                    )
                    raise RefreshFailure(token.refresh_error, code) from exc
                if not auth_failed and not self._is_auth_error(exc):
                    raise
        try:
            result = await self._try_refresh_st(token.id, token, force=force)
        except RefreshFailure as exc:
            if exc.code in {"invalid_cookies", "login_required"}:
                try:
                    updates = await self._validate_refresh_session(token, token.st)
                except Exception as fallback_error:
                    if (
                        self._is_auth_error(fallback_error)
                        or getattr(fallback_error, "code", None) == "session_invalid"
                    ):
                        raise exc
                    raise
                updates["_warning"] = exc
                return updates
            raise
        try:
            updates = result.get("_validated_session") or await self._validate_refresh_session(
                token, result["session_token"]
            )
        except RefreshFailure as exc:
            if getattr(exc, "credential_updates", None) and result.get("google_cookies"):
                exc.credential_updates["google_cookies"] = result["google_cookies"]
            raise
        if result.get("google_cookies"):
            updates["google_cookies"] = result["google_cookies"]
        if result.get("_profile_used"):
            updates.update(
                browser_profile_status="ready",
                browser_profile_error="",
                browser_profile_verified_at=datetime.now(timezone.utc),
                browser_profile_failures=0,
            )
        updates["last_st_refresh_at"] = datetime.now(timezone.utc)
        updates["last_st_refresh_result"] = "success"
        return updates

    async def _refresh_at_inner(
        self,
        token_id: int,
        force: bool = False,
        refresh_st: bool = False,
        validation_only: bool = False,
    ) -> bool:
        lock = await self._get_token_lock(self._refresh_locks, self._refresh_lock_guard, token_id)
        async with lock:
            token = await self.db.get_token(token_id)
            if not token:
                return False
            if getattr(token, "proxy_pool_id", None) is not None:
                try:
                    await self.resolve_account_proxy(token)
                except AccountProxyUnavailable:
                    return False
            now = datetime.now(timezone.utc)
            validation_only = bool(validation_only and token.browser_profile_source == "cloak")
            repair_cycle = validation_only and (
                not self._profile_cycle_anchor_valid(token)
                or not self._profile_session_fresh(token)
            )
            if repair_cycle:
                # Imported/old rows can have a future next_refresh_at from the
                # retired AT path. The first verified modern Profile pass must
                # repair that cycle anchor; subsequent 5-minute probes do not.
                validation_only = False
                force = force or (
                    token.refresh_status == "ready" and token.browser_profile_status == "ready"
                )
            if not force:
                if not token.is_active and token.ban_reason != "refresh_failed":
                    return False
                if (
                    token.refresh_status in {"config_error", "login_required"}
                    and not token.is_active
                ):
                    return False
                if (
                    token.next_refresh_at
                    and self._as_utc(token.next_refresh_at) > now
                    and (not validation_only or token.refresh_status == "retrying")
                ):
                    return False
                if (
                    validation_only
                    and token.google_cookies.strip()
                    and self.is_profile_validation_recent(token)
                ):
                    return True
            if not await self.db.update_token_if_credentials_match(
                token_id,
                self._refresh_expected(token, token),
                refresh_status="refreshing",
                last_refresh_at=now,
            ):
                return False
            try:
                updates = await asyncio.wait_for(
                    self._refresh_credentials(token, refresh_st, force=force), 120
                )
                latest = await self.db.get_token(token_id)
                if not latest:
                    return False
                # Discard an in-flight response if someone replaced its credentials.
                if any(
                    getattr(latest, key) != getattr(token, key)
                    for key in (
                        "st",
                        "google_cookies",
                        "proxy_url",
                        "proxy_pool_id",
                        "protocol_mode",
                        "login_account",
                        "email",
                        "browser_profile_source",
                        "browser_profile_epoch",
                    )
                ):
                    return False
                now = datetime.now(timezone.utc)
                if token.browser_profile_source == "cloak":
                    if validation_only:
                        updates.pop("last_st_refresh_at", None)
                        updates.pop("last_st_refresh_result", None)
                        due = self._as_utc(token.next_refresh_at)
                    else:
                        updates.update(last_st_refresh_at=now, last_st_refresh_result="success")
                        due = now + timedelta(
                            minutes=self._normalize_refresh_interval(token.refresh_interval_minutes)
                        )
                else:
                    due = updates["at_expires"] - timedelta(hours=1)
                    interval = self._normalize_refresh_interval(token.refresh_interval_minutes)
                    due = min(
                        due,
                        now
                        + timedelta(
                            minutes=interval if token.protocol_mode == "protocol" else 1440
                        ),
                    )
                warning = updates.pop("_warning", None)
                if (
                    token.browser_profile_source != "cloak"
                    and not force
                    and token.refresh_status in {"config_error", "login_required"}
                ):
                    warning = RefreshFailure(
                        token.refresh_error,
                        "invalid_cookies"
                        if token.refresh_status == "config_error"
                        else "login_required",
                    )
                updates.update(
                    refresh_status="ready",
                    refresh_error="",
                    refresh_reason="",
                    refresh_stage="",
                    refresh_network_step="",
                    refresh_failures=0,
                    last_refresh_at=now,
                    next_refresh_at=max(now + timedelta(minutes=5), due) if due else None,
                )
                if token.browser_profile_source == "cloak":
                    updates.update(next_refresh_at=due, browser_profile_next_at=due)
                if warning:
                    updates.update(
                        refresh_status="config_error"
                        if warning.code == "invalid_cookies"
                        else "login_required",
                        refresh_error=self._safe_refresh_error(warning),
                        refresh_failures=token.refresh_failures + 1,
                        next_refresh_at=max(
                            now + timedelta(minutes=5), updates["at_expires"] - timedelta(hours=1)
                        ),
                    )
                    if token.browser_profile_source == "cloak" and warning.code == "login_required":
                        updates.update(
                            browser_profile_status="login_required",
                            browser_profile_error=self._safe_refresh_error(warning),
                            next_refresh_at=None,
                            browser_profile_next_at=None,
                        )
                if latest.ban_reason == "refresh_failed":
                    updates.update(is_active=True, ban_reason=None, banned_at=None)
                expected = self._refresh_expected(token, latest)
                if not await self.db.update_token_if_credentials_match(
                    token_id, expected, **updates
                ):
                    return False
                self._mark_at_valid(token_id, token=latest.model_copy(update=updates))
                record_token_refresh("at", "success")
                if "last_st_refresh_at" in updates:
                    record_token_refresh("st", "success")
                if warning is None and updates.get("browser_profile_status") == "ready":
                    self.notify_profile_ready(token_id)
                return warning is None
            except Exception as exc:
                self._clear_at_validation_cache(token_id)
                latest = await self.db.get_token(token_id)
                if not latest:
                    return False
                if any(
                    getattr(latest, key) != getattr(token, key)
                    for key in (
                        "st",
                        "google_cookies",
                        "proxy_url",
                        "proxy_pool_id",
                        "protocol_mode",
                        "login_account",
                        "email",
                        "browser_profile_source",
                        "browser_profile_epoch",
                    )
                ):
                    return False
                now = datetime.now(timezone.utc)
                failures = latest.refresh_failures + 1
                code = getattr(exc, "code", "transient_error")
                status = {
                    "invalid_cookies": "config_error",
                    "invalid_proxy": "config_error",
                    "proxy_error": "config_error",
                    "config_error": "config_error",
                    "login_required": "login_required",
                }.get(code, "retrying")
                # Bounded backoff survives restarts; per-account offset spreads retries.
                delay = min(3600, 60 * 2 ** min(failures - 1, 6)) + token_id % 30
                error = self._safe_refresh_error(exc)
                updates = dict(
                    refresh_status=status,
                    refresh_error=error,
                    refresh_reason=str(getattr(exc, "profile_reason", "") or code),
                    refresh_stage=str(getattr(exc, "profile_stage", "") or ""),
                    refresh_network_step=str(getattr(exc, "profile_network_step", "") or ""),
                    refresh_failures=failures,
                    last_refresh_at=now,
                    next_refresh_at=now + timedelta(seconds=delay)
                    if status == "retrying"
                    else None,
                )
                if token.browser_profile_source == "cloak" and code in {
                    "login_required",
                    "config_error",
                }:
                    updates.update(
                        browser_profile_status="login_required"
                        if code == "login_required"
                        else "error",
                        browser_profile_error=error,
                        browser_profile_failures=failures,
                        browser_profile_next_at=updates["next_refresh_at"],
                    )
                expires = self._as_utc(latest.at_expires)
                if token.browser_profile_source == "cloak":
                    # A network failure does not establish that a Google session expired.
                    unusable = code != "transient_error"
                    if not unusable and latest.browser_profile_verified_at is None:
                        # Keep the last successful legacy timestamp from becoming the
                        # failed attempt time when a ready Profile predates this field.
                        updates["browser_profile_verified_at"] = token.last_refresh_at
                    updates["browser_profile_next_at"] = updates["next_refresh_at"]
                else:
                    unusable = (
                        not latest.at or not expires or expires <= now or self._is_auth_error(exc)
                    )
                if unusable and latest.is_active:
                    updates.update(is_active=False, ban_reason="refresh_failed", banned_at=now)
                expected = self._refresh_expected(token, latest)
                # Only identity-verified rotations are attached to this exception.
                updates.update(getattr(exc, "credential_updates", {}))
                if not await self.db.update_token_if_credentials_match(
                    token_id, expected, **updates
                ):
                    return False
                record_token_refresh("at", "failure")
                debug_logger.log_warning(f"[TOKEN_REFRESH] Token {token_id}: {status}: {error}")
                return False

    async def _refresh_at(
        self,
        token_id: int,
        force: bool = False,
        refresh_st: bool = False,
        validation_only: bool = False,
    ) -> bool:
        """Share the final result, including stronger intents arriving during a refresh."""
        existing = self._refresh_futures.get(token_id)
        if existing and not existing.done():
            intent = self._refresh_intents[token_id]
            intent["force"] |= bool(force)
            intent["refresh_st"] |= bool(refresh_st)
            intent["renew"] |= not validation_only
            return await asyncio.shield(existing)

        intent = {
            "force": bool(force),
            "refresh_st": bool(refresh_st),
            "renew": not validation_only,
        }
        self._refresh_intents[token_id] = intent

        def cleanup(finished):
            if self._refresh_futures.get(token_id) is finished:
                self._refresh_futures.pop(token_id, None)
                self._refresh_intents.pop(token_id, None)

        async def runner():
            try:
                applied_force, applied_st, applied_renew = (
                    intent["force"],
                    intent["refresh_st"],
                    intent["renew"],
                )
                while True:
                    kwargs = {} if applied_renew else {"validation_only": True}
                    result = await self._refresh_at_inner(
                        token_id, force=applied_force, refresh_st=applied_st, **kwargs
                    )
                    if (
                        (intent["force"] and not applied_force)
                        or (intent["refresh_st"] and not applied_st)
                        or (intent["renew"] and not applied_renew)
                    ):
                        # Force the stronger follow-up past the first run's next_refresh_at.
                        # Both bits only grow: at most two upgrades, never failure retries.
                        applied_force, applied_st, applied_renew = (
                            True,
                            intent["refresh_st"],
                            intent["renew"],
                        )
                        continue
                    return result
            finally:
                cleanup(asyncio.current_task())

        task = asyncio.create_task(runner())
        self._refresh_futures[token_id] = task
        # A task cancelled before its first instruction never enters runner's finally.
        task.add_done_callback(cleanup)
        return await asyncio.shield(task)

    async def run_protocol_refresh_once(self) -> None:
        """Renew expiring ATs and due STs, including accounts awaiting recovery."""
        refresh_config = await self.db.get_token_refresh_config()
        tokens = await self.db.get_all_tokens()
        now = datetime.now(timezone.utc)
        profile_due, protocol_due = [], []
        for token in tokens:
            if not token.auto_refresh_enabled or (
                not token.is_active and token.ban_reason != "refresh_failed"
            ):
                continue
            terminal = token.refresh_status in {"config_error", "login_required"}
            if terminal and not token.is_active:
                continue
            if (
                token.browser_profile_source == "cloak"
                and token.browser_profile_status == "login_required"
                and token.next_refresh_at is None
            ):
                continue
            if token.next_refresh_at and self._as_utc(token.next_refresh_at) > now:
                continue
            st_due = False
            if (
                not terminal
                and token.browser_profile_source != "cloak"
                and token.protocol_mode == "protocol"
                and refresh_config
                and refresh_config.enabled
            ):
                last_st = self._as_utc(token.last_st_refresh_at)
                interval = self._normalize_refresh_interval(token.refresh_interval_minutes)
                st_due = not last_st or now - last_st >= timedelta(minutes=interval)
            if st_due or self._should_refresh_at(token) or token.ban_reason == "refresh_failed":
                due = profile_due if token.browser_profile_source == "cloak" else protocol_due
                due.append((token.id, st_due))

        # Do not start more Profile refresh timers than the manager can run.
        # Cookie-only requests keep their independent four-worker budget.
        manager = getattr(self.profile_refresher, "manager", None)
        profile_workers = getattr(manager, "max_concurrent", None)
        if not isinstance(profile_workers, int) or profile_workers < 1:
            profile_workers = config.browser_profile_max_concurrent
        pending_profiles, pending_protocol = iter(profile_due), iter(protocol_due)

        async def worker(pending):
            for token_id, st_due in pending:
                try:
                    await self._refresh_at(token_id, refresh_st=st_due)
                except Exception as exc:
                    debug_logger.log_warning(
                        f"[TOKEN_REFRESH] Token {token_id}: {self._safe_refresh_error(exc)}"
                    )

        await asyncio.gather(
            *(worker(pending_profiles) for _ in range(min(profile_workers, len(profile_due)))),
            *(worker(pending_protocol) for _ in range(min(4, len(protocol_due)))),
        )

    async def _protocol_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.run_protocol_refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug_logger.log_error(f"[PROTOCOL_REFRESH] 后台任务异常 - {e}")

    def start_protocol_refresher(self) -> None:
        if self._protocol_refresher_task and not self._protocol_refresher_task.done():
            return
        self._protocol_refresher_task = asyncio.create_task(self._protocol_refresh_loop())

    async def stop_protocol_refresher(self) -> None:
        task = self._protocol_refresher_task
        self._protocol_refresher_task = None
        tasks = [task] if task else []
        tasks.extend(self._refresh_futures.values())
        for pending in tasks:
            pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def ensure_project_exists(self, token_id: int) -> str:
        """Ensure a token has a pooled set of projects and return one in round-robin order."""
        return await self._project_pool.ensure_project_exists(
            token_id,
            create_project=self._create_project_for_token,
            select_project=self._select_next_project,
            sort_projects=self._sort_projects,
            pool_size=self._get_project_pool_size(),
        )

    async def record_usage(self, token_id: int, is_video: bool = False):
        """Record token usage"""
        await self.db.update_token(token_id, use_count=1, last_used_at=datetime.now())

        if is_video:
            await self.db.increment_token_stats(token_id, "video")
        else:
            await self.db.increment_token_stats(token_id, "image")

    async def record_error(self, token_id: int):
        """Record token error and auto-disable if threshold reached"""
        await self.db.increment_token_stats(token_id, "error")

        # Check if should auto-disable token (based on consecutive errors)
        stats = await self.db.get_token_stats(token_id)
        admin_config = await self.db.get_admin_config()

        if stats and stats.consecutive_error_count >= admin_config.error_ban_threshold:
            debug_logger.log_warning(
                f"[TOKEN_BAN] Token {token_id} consecutive error count ({stats.consecutive_error_count}) "
                f"reached threshold ({admin_config.error_ban_threshold}), auto-disabling"
            )
            await self.disable_token(token_id, reason="error_threshold")

    async def record_success(self, token_id: int):
        """Record successful request (reset consecutive error count)

        This method resets error_count to 0, which is used for auto-disable threshold checking.
        Note: today_error_count and historical statistics are NOT reset.
        """
        await self.db.reset_error_count(token_id)

    async def ban_token_for_429(self, token_id: int):
        """因429错误立即禁用token

        Args:
            token_id: Token ID
        """
        debug_logger.log_warning(f"[429_BAN] 禁用Token {token_id} (原因: 429 Rate Limit)")
        await self.db.update_token(
            token_id,
            is_active=False,
            ban_reason="429_rate_limit",
            banned_at=datetime.now(timezone.utc),
        )

    async def auto_unban_429_tokens(self):
        """自动解禁因429被禁用的token

        规则:
        - 距离禁用时间12小时后自动解禁
        - 仅解禁未过期的token
        - 仅解禁因429被禁用的token
        """
        all_tokens = await self.db.get_all_tokens()
        now = datetime.now(timezone.utc)

        for token in all_tokens:
            # 跳过非429禁用的token
            if token.ban_reason != "429_rate_limit":
                continue

            # 跳过未禁用的token
            if token.is_active:
                continue

            # 跳过没有禁用时间的token
            if not token.banned_at:
                continue

            # 检查token是否已过期
            if token.at_expires:
                # 确保时区一致
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires

                # 如果已过期，跳过
                if at_expires_aware <= now:
                    debug_logger.log_info(f"[AUTO_UNBAN] Token {token.id} 已过期，跳过解禁")
                    continue

            # 确保banned_at时区一致
            if token.banned_at.tzinfo is None:
                banned_at_aware = token.banned_at.replace(tzinfo=timezone.utc)
            else:
                banned_at_aware = token.banned_at

            # 检查是否已过12小时
            time_since_ban = now - banned_at_aware
            if time_since_ban.total_seconds() >= 12 * 3600:  # 12小时
                debug_logger.log_info(
                    f"[AUTO_UNBAN] 解禁Token {token.id} (禁用时间: {banned_at_aware}, "
                    f"已过 {time_since_ban.total_seconds() / 3600:.1f} 小时)"
                )
                await self.db.update_token(
                    token.id, is_active=True, ban_reason=None, banned_at=None
                )
                # 重置错误计数
                await self.db.reset_error_count(token.id)

    # ========== 余额刷新 ==========

    async def refresh_credits(self, token_id: int) -> int:
        """刷新Token余额

        Returns:
            credits
        """
        token = await self.db.get_token(token_id)
        if not token:
            return 0

        if token.browser_profile_source == "cloak":
            if not token.is_active and token.ban_reason != "refresh_failed":
                return 0
            try:
                result = await self._get_credits_for_token(token, None)
                return result["credits"]
            except Exception as exc:
                debug_logger.log_error(
                    f"Failed to refresh Profile credits for token {token_id}: {self._safe_refresh_error(exc)}"
                )
                return 0

        # 确保AT有效
        token = await self.ensure_valid_token(token)
        if not token:
            return 0

        try:
            result = await self._get_credits_for_token(token, token.at)
            credits = result.get("credits", 0)
            user_paygate_tier = result.get("userPaygateTier")

            # 更新数据库
            await self.db.update_token(
                token_id,
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

            return credits
        except Exception as e:
            debug_logger.log_error(f"Failed to refresh credits for token {token_id}: {str(e)}")
            return 0
