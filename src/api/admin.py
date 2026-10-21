"""Admin API routes"""

import json  # noqa: F401 - legacy admin import/patch compatibility
import re  # noqa: F401 - legacy admin import/patch compatibility
import secrets
import time  # noqa: F401 - legacy admin import/patch compatibility
import urllib.error  # noqa: F401 - legacy admin import/patch compatibility
import urllib.request  # noqa: F401 - legacy admin import/patch compatibility
from datetime import datetime, timezone  # noqa: F401 - legacy admin import/patch compatibility
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse  # noqa: F401 - legacy admin import/patch compatibility

from curl_cffi.requests import AsyncSession
from fastapi import (  # noqa: F401 - legacy admin import/patch compatibility
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)

from ..core.account_identity import same_google_identity
from ..core.admin_sessions import AdminSessionStore
from ..core.auth import AuthManager
from ..core.config import config
from ..core.database import Database
from ..core.models import Token  # noqa: F401 - legacy admin import/patch compatibility
from ..core.monitoring import build_public_health_snapshot
from ..services.concurrency_manager import ConcurrencyManager
from ..services.proxy_manager import ProxyManager
from ..services.token_manager import TokenManager
from .admin_schemas import (
    AddTokenRequest as AddTokenRequest,
    CallLogicConfigRequest as CallLogicConfigRequest,
    CaptchaScoreTestRequest as CaptchaScoreTestRequest,
    ChangePasswordRequest as ChangePasswordRequest,
    GenerationConfigRequest as GenerationConfigRequest,
    ImportTokenItem as ImportTokenItem,
    ImportTokensRequest as ImportTokensRequest,
    LoginRequest as LoginRequest,
    ProxyConfigRequest as ProxyConfigRequest,
    ProxyTestRequest as ProxyTestRequest,
    ST2ATRequest as ST2ATRequest,
    TokenRefreshConfigRequest as TokenRefreshConfigRequest,
    UpdateAdminConfigRequest as UpdateAdminConfigRequest,
    UpdateAPIKeyRequest as UpdateAPIKeyRequest,
    UpdateDebugConfigRequest as UpdateDebugConfigRequest,
    UpdateTokenRequest as UpdateTokenRequest,
)

try:
    import httpx
except ImportError:
    httpx = None

from .admin_routes import captcha_tools
from .admin_routes.auth import create_auth_router
from .admin_routes.captcha_config import create_captcha_config_router
from .admin_routes.captcha_score import create_captcha_score_router
from .admin_routes.logs import (
    _extract_error_summary as _extract_error_summary,
    _truncate_text as _truncate_text,
    create_log_router,
)
from .admin_routes.profile_browser import create_profile_browser_router
from .admin_routes.profile_transfer import create_profile_transfer_router
from .admin_routes.proxy import create_proxy_router
from .admin_routes.runtime_config import create_runtime_config_router
from .admin_routes.settings import create_settings_router
from .admin_routes.tokens import create_token_router

router = APIRouter()

# Dependency injection
token_manager: TokenManager = None
proxy_manager: ProxyManager = None
db: Database = None
concurrency_manager: Optional[ConcurrencyManager] = None
profile_browser_viewer = None

# Keep sessions alongside the persistent application database across restarts.
active_admin_tokens = AdminSessionStore(
    lambda: (
        Path(db.db_path).parent / "admin_sessions.sqlite3"
        if db is not None
        else Path(__file__).resolve().parents[2] / "data" / "admin_sessions.sqlite3"
    )
)
ADMIN_SESSION_COOKIE_NAME = "lay4flow_remote_admin_session"


def _mask_token(token: Optional[str]) -> str:
    return captcha_tools.mask_token(token)


def _normalize_http_base_url(base_url: str) -> str:
    return captcha_tools.normalize_http_base_url(base_url)


def _get_remote_browser_client_config() -> tuple[str, str, int]:
    return captcha_tools.get_remote_browser_client_config(
        get_config=lambda: config, normalize_url=lambda value: _normalize_http_base_url(value)
    )


def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
    return captcha_tools.build_remote_browser_http_timeout(read_timeout, httpx_module=httpx)


def _parse_json_response_text(text: str) -> Optional[Any]:
    return captcha_tools.parse_json_response_text(text)


async def _stdlib_json_http_request(
    method: str, url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]], timeout: int
) -> tuple[int, Optional[Any], str]:
    return await captcha_tools.stdlib_json_http_request(
        method,
        url,
        headers,
        payload,
        timeout,
        parse_json=lambda value: _parse_json_response_text(value),
    )


async def _sync_json_http_request(
    method: str, url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]], timeout: int
) -> tuple[int, Optional[Any], str]:
    return await captcha_tools.sync_json_http_request(
        method,
        url,
        headers,
        payload,
        timeout,
        get_httpx=lambda: httpx,
        build_timeout=lambda value: _build_remote_browser_http_timeout(value),
        stdlib_request=lambda *a, **kw: _stdlib_json_http_request(*a, **kw),
        parse_json=lambda value: _parse_json_response_text(value),
    )


async def _score_test_with_remote_browser_service(
    website_url: str, website_key: str, verify_url: str, action: str, enterprise: bool = False
) -> Dict[str, Any]:
    return await captcha_tools.score_test_with_remote_browser_service(
        website_url,
        website_key,
        verify_url,
        action,
        enterprise,
        get_client_config=lambda: _get_remote_browser_client_config(),
        request_json=lambda *a, **kw: _sync_json_http_request(*a, **kw),
    )


def set_dependencies(
    tm: TokenManager, pm: ProxyManager, database: Database, cm: Optional[ConcurrencyManager] = None
):
    """Set service instances"""
    global token_manager, proxy_manager, db, concurrency_manager
    token_manager = tm
    proxy_manager = pm
    db = database
    concurrency_manager = cm


# ========== Auth Middleware ==========


async def verify_admin_token(request: Request, authorization: str = Header(None)):
    """Verify admin session token (NOT API key)"""
    header_token = ""
    if authorization and authorization.startswith("Bearer "):
        header_token = authorization[7:].strip()

    cookie_token = get_admin_token_from_cookie(request) or ""

    if header_token and header_token in active_admin_tokens:
        return header_token

    if cookie_token and cookie_token in active_admin_tokens:
        return cookie_token

    if header_token or cookie_token:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired admin token",
            headers={"X-Admin-Session": "expired"},
        )

    raise HTTPException(
        status_code=401, detail="Missing authorization", headers={"X-Admin-Session": "expired"}
    )


def get_admin_token_from_cookie(request: Request) -> Optional[str]:
    token = str(request.cookies.get(ADMIN_SESSION_COOKIE_NAME) or "").strip()
    return token or None


def is_admin_session_token_valid(token: Optional[str]) -> bool:
    normalized = str(token or "").strip()
    return bool(normalized) and normalized in active_admin_tokens


# ========== Auth Endpoints ==========

auth_router, (admin_login, admin_logout, change_password, login, logout, update_admin_password) = (
    create_auth_router(
        lambda: db,
        lambda: AuthManager,
        lambda: active_admin_tokens,
        lambda: ADMIN_SESSION_COOKIE_NAME,
        lambda size: secrets.token_urlsafe(size),
        verify_admin_token,
        lambda *a, **kw: admin_login(*a, **kw),
        lambda *a, **kw: admin_logout(*a, **kw),
        lambda *a, **kw: change_password(*a, **kw),
    )
)
router.include_router(auth_router)


# ========== Token Management ==========

(
    token_router,
    (
        get_tokens,
        add_token,
        update_token,
        delete_token,
        enable_token,
        disable_token,
        refresh_credits,
        refresh_at,
        st_to_at,
        import_tokens,
    ),
) = create_token_router(
    lambda: db,
    lambda: token_manager,
    lambda: concurrency_manager,
    verify_admin_token,
)
router.include_router(token_router)


# ========== Config Management ==========

(
    proxy_router,
    (
        get_proxy_config,
        get_proxy_config_alias,
        update_proxy_config_alias,
        update_proxy_config,
        test_proxy_connectivity,
    ),
) = create_proxy_router(
    lambda: proxy_manager,
    lambda *a, **kw: AsyncSession(*a, **kw),
    verify_admin_token,
)
router.include_router(proxy_router)


(
    settings_router,
    (
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
    ),
) = create_settings_router(
    lambda: db,
    lambda: config,
    lambda *a, **kw: build_public_health_snapshot(*a, **kw),
    verify_admin_token,
    lambda *a, **kw: get_generation_config(*a, **kw),
)
router.include_router(settings_router)


# ========== System Info ==========


# ========== Additional Routes for Frontend Compatibility ==========


log_router, get_logs, get_log_detail, clear_logs = create_log_router(
    lambda: db, verify_admin_token, lambda payload: _extract_error_summary(payload)
)
router.include_router(log_router)


# ========== AT Auto Refresh Config ==========


async def _sync_runtime_cache_config():
    from . import routes

    if routes.generation_handler and routes.generation_handler.file_cache:
        file_cache = routes.generation_handler.file_cache
        file_cache.set_timeout(config.cache_timeout)
        await file_cache.refresh_cleanup_task()


(
    runtime_config_router,
    (
        get_token_refresh_config,
        update_token_refresh_enabled,
        update_token_refresh_config,
        get_cache_config,
        update_cache_enabled,
        update_cache_config_full,
        update_cache_base_url,
    ),
) = create_runtime_config_router(
    lambda: db, verify_admin_token, lambda: _sync_runtime_cache_config()
)
router.include_router(runtime_config_router)


captcha_config_router, (update_captcha_config, get_captcha_config) = create_captcha_config_router(
    lambda: db, verify_admin_token, lambda value: _normalize_http_base_url(value)
)
router.include_router(captcha_config_router)

captcha_score_router, (test_captcha_score,) = create_captcha_score_router(
    verify_admin_token,
    lambda *args, **kwargs: _score_test_with_remote_browser_service(*args, **kwargs),
    lambda value: _mask_token(value),
)
router.include_router(captcha_score_router)


# Preserve historical matching precedence after grouping route implementations.
_ROUTE_ORDER = (
    "admin_login",
    "admin_logout",
    "change_password",
    "get_tokens",
    "add_token",
    "update_token",
    "delete_token",
    "enable_token",
    "disable_token",
    "refresh_credits",
    "refresh_at",
    "st_to_at",
    "import_tokens",
    "get_proxy_config",
    "get_proxy_config_alias",
    "update_proxy_config_alias",
    "update_proxy_config",
    "test_proxy_connectivity",
    "list_proxy_pool",
    "add_proxy_pool_entry",
    "import_proxy_pool_entries",
    "remove_proxy_pool_entry",
    "update_proxy_pool_entry",
    "batch_proxy_pool_action",
    "toggle_proxy_pool_entry",
    "check_proxy_pool_entry",
    "get_generation_config",
    "update_generation_config",
    "get_call_logic_config",
    "update_call_logic_config",
    "get_system_info",
    "get_public_release_metadata",
    "check_public_release",
    "get_release_metadata",
    "check_latest_release",
    "get_release_document",
    "login",
    "logout",
    "health_check",
    "get_stats",
    "get_logs",
    "get_log_detail",
    "clear_logs",
    "get_admin_config",
    "update_admin_config",
    "update_admin_password",
    "update_api_key",
    "update_debug_config",
    "get_generation_timeout",
    "update_generation_timeout",
    "get_token_refresh_config",
    "update_token_refresh_enabled",
    "update_token_refresh_config",
    "get_cache_config",
    "update_cache_enabled",
    "update_cache_config_full",
    "update_cache_base_url",
    "update_captcha_config",
    "get_captcha_config",
    "test_captcha_score",
)
_route_positions = {name: index for index, name in enumerate(_ROUTE_ORDER)}
assert len(_route_positions) == len(router.routes)
assert {route.endpoint.__name__ for route in router.routes} == set(_route_positions)
router.routes.sort(key=lambda route: _route_positions[route.endpoint.__name__])


def get_profile_browser_viewer():
    global profile_browser_viewer
    if profile_browser_viewer is None:
        from ..services.browser.profile_viewer import ProfileBrowserViewer

        profile_browser_viewer = ProfileBrowserViewer(token_manager)
    return profile_browser_viewer


async def close_profile_browser_viewer():
    if profile_browser_viewer is not None:
        await profile_browser_viewer.close_all()


# Append new routes after the preserved legacy route order.
router.include_router(
    create_profile_browser_router(
        get_profile_browser_viewer,
        verify_admin_token,
        is_admin_session_token_valid,
        ADMIN_SESSION_COOKIE_NAME,
    )
)


async def import_legacy_account(bundle, *, proxy_url=None, proxy_pool_id=None):
    """Keep legacy validation and settings restoration in the server-side task."""
    from pydantic import ValidationError

    from ..services.browser.profile_transfer import ProfileTransferError

    try:
        item = ImportTokenItem.model_validate(bundle)
        settings = UpdateTokenRequest.model_validate(
            {
                key: bundle[key]
                for key in (
                    "remark",
                    "project_id",
                    "project_name",
                    "browser_profile_source",
                    "submission_proxy_mode",
                )
                if key in bundle
            }
        )
    except ValidationError:
        raise ProfileTransferError("invalid_legacy_account", "旧版账号 JSON 格式有误") from None

    async def execute():
        return await import_tokens(ImportTokensRequest(tokens=[item]), token="background-import")

    if proxy_pool_id is not None or proxy_url:
        try:
            selected_id, selected_url = await ProxyManager(db).select_account_proxy(
                proxy_pool_id, proxy_url
            )
        except ValueError:
            raise ProfileTransferError(
                "proxy_unavailable", "目标代理暂不可用，请检查代理池"
            ) from None
        item.proxy_url = selected_url
        result = await token_manager._flow_call_for_token(
            Token(
                st=item.session_token or "",
                email="",
                proxy_url=selected_url,
                proxy_pool_id=selected_id,
            ),
            execute,
        )
    else:
        result = await execute()
    if result.get("errors"):
        raise ProfileTransferError("legacy_import_failed", "旧版账号登录态校验或导入失败", 422)
    imported = result.get("imported") or []
    if len(imported) != 1:
        raise ProfileTransferError(
            "legacy_import_unverified", "账号身份尚未确认，原配置已保留", 422
        )
    verified = imported[0]
    account = await token_manager.get_token(verified["token_id"])
    if account is None or not same_google_identity(account.email, verified["email"]):
        raise ProfileTransferError("legacy_import_unverified", "账号身份已变化，原配置已保留", 409)
    if settings.model_fields_set:
        await update_token(account.id, settings, token="background-import")
    if bundle.get("is_active") is False and account.is_active:
        await disable_token(account.id, token="background-import")
    return {
        "success": True,
        "added": result.get("added", 0),
        "updated": result.get("updated", 0),
        "email": account.email,
        "token_id": account.id,
    }


router.include_router(
    create_profile_transfer_router(lambda: token_manager, verify_admin_token, import_legacy_account)
)
