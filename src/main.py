"""FastAPI application initialization"""

import asyncio
import os
import sys
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import admin, routes
from .api.admin_routes.studio import create_studio_router
from .api.admin_routes.vids import create_vids_admin_router
from .api.admin_routes.vids_onboarding import create_vids_onboarding_router
from .api.compat import router as compat_router
from .api.compat_images import router as compat_images_router
from .api.compat_videos import create_compat_videos_router, execute_compat_video
from .api.openai_common import openai_http_exception_handler
from .api.openai_images import create_images_router
from .api.openai_videos import create_videos_router
from .api.vids_media import (
    CONSOLE_PREFIX as VIDS_CONSOLE_PREFIX,
    PUBLIC_PREFIX as VIDS_PUBLIC_PREFIX,
    create_vids_media_router,
)
from .core.config import config
from .core.monitoring import CONTENT_TYPE_LATEST, render_main_metrics
from .core.update_guard import UpdateActivity, UpdateGuardMiddleware
from .core.version import VERSION
from .services.factory import create_services
from .services.video_jobs import VideoJobService
from .services.vids.auto_onboarding import VidsAutoOnboarding
from .services.vids.gateway import GatewayError
from .services.vids.onboarding import VidsOnboardingService

# Auxiliary API containers may skip the Vids video-worker and its database lock.
_SKIP_VIDS = os.getenv("LAY4FLOW_SKIP_VIDS", "").strip().lower() in {"1", "true", "yes", "on"}
_LOCAL_NO_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")
API_VERSION = VERSION
PUBLIC_OPENAPI_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/images/edits",
        "/v1/images/generations",
        "/v1/models",
        "/v1/models/aliases",
        "/v1/models/{model}",
        "/v1/responses",
        "/v1/videos",
        "/v1/videos/{video_id}",
        "/v1/videos/{video_id}/content",
    }
)
COMPAT_OPENAPI_PATHS = frozenset(
    {
        "/compat/v1/chat/completions",
        "/compat/v1/images/edits",
        "/compat/v1/images/generations",
        "/compat/v1/models",
        "/compat/v1/models/{model}",
        "/compat/v1/videos",
        "/compat/v1/videos/{video_id}",
        "/compat/v1/videos/{video_id}/content",
    }
)


def _configure_stdio_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _configure_local_no_proxy() -> None:
    for env_name in ("NO_PROXY", "no_proxy"):
        raw_value = str(os.environ.get(env_name, "") or "")
        entries = [item.strip() for item in raw_value.replace(";", ",").split(",") if item.strip()]
        normalized = {item.lower() for item in entries}
        changed = False
        for host in _LOCAL_NO_PROXY_HOSTS:
            if host.lower() not in normalized:
                entries.append(host)
                normalized.add(host.lower())
                changed = True
        if changed or not raw_value:
            os.environ[env_name] = ",".join(entries)


def _configure_asyncio_policy() -> None:
    if os.name != "nt":
        return
    policy_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_cls is None:
        return
    try:
        if not isinstance(asyncio.get_event_loop_policy(), policy_cls):
            asyncio.set_event_loop_policy(policy_cls())
    except Exception:
        pass


def _suppress_known_runtime_warnings() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r".*Proactor event loop does not implement add_reader family of methods required.*",
        category=RuntimeWarning,
    )


def _configure_process_runtime() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _configure_stdio_utf8()
    _configure_local_no_proxy()
    _configure_asyncio_policy()
    _suppress_known_runtime_warnings()


_configure_process_runtime()


@asynccontextmanager
async def _flow_lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    print("=" * 60)
    print("lay4flow-remote Starting...")
    print("=" * 60)

    # Get config from setting.toml
    config_dict = config.get_raw_config()

    # Check if database exists (determine if first startup)
    is_first_startup = not db.db_exists()

    # Initialize database tables structure
    await db.init_db()

    # Handle database initialization based on startup type
    if is_first_startup:
        print(
            "First startup detected. Initializing database and configuration from setting.toml..."
        )
        await db.init_config_from_toml(config_dict, is_first_startup=True)
        print("Database and configuration initialized successfully.")
    else:
        print("Existing database detected. Checking for missing tables and columns...")
        await db.check_and_migrate_db(config_dict)
        print("Database migration check completed.")

    # Synchronize persisted configuration before starting account workers.
    await db.reload_config_to_memory()
    generation_handler.file_cache.set_timeout(config.cache_timeout)
    cache_cleanup_enabled = await generation_handler.file_cache.refresh_cleanup_task()

    # 尽量在浏览器服务启动前就拿到 token 快照，后续并发管理和预热共用。
    tokens = await token_manager.get_all_tokens()

    # Initialize concurrency manager
    await concurrency_manager.initialize(tokens)

    try:
        warmed_projects = await flow_client.prefill_remote_browser_for_tokens(
            tokens, action="IMAGE_GENERATION"
        )
        print(f"Remote browser pool prefill started for {warmed_projects} project(s)")
    except Exception as e:
        print(f"Remote browser pool prefill failed: {e}")

    # Start 429 auto-unban task
    import asyncio

    async def auto_unban_task():
        """定时任务：每小时检查并解禁429被禁用的token"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时执行一次
                await token_manager.auto_unban_429_tokens()
            except Exception as e:
                print(f"Auto-unban task error: {e}")

    auto_unban_task_handle = asyncio.create_task(auto_unban_task())

    async def probe_proxy_pool():
        while True:
            await asyncio.sleep(30)
            try:
                await proxy_manager.probe_due_proxy_entries(limit=3)
            except Exception as error:
                print(f"Proxy pool probe failed: {type(error).__name__}")

    proxy_probe_task = asyncio.create_task(probe_proxy_pool())
    token_manager.start_protocol_refresher()
    if not _SKIP_VIDS:
        await video_job_service.start()

    print("Database initialized")
    print(f"Total tokens: {len(tokens)}")
    print(
        f"Cache: {'Enabled' if config.cache_enabled else 'Disabled'} (timeout: {config.cache_timeout}s)"
    )
    if cache_cleanup_enabled:
        print("File cache cleanup task started")
    else:
        print("File cache cleanup task disabled (timeout <= 0)")
    print("429 auto-unban task started (runs every hour)")
    print("Account session refresher started (runs every minute)")
    print(f"Server running on http://{config.server_host}:{config.server_port}")
    print("=" * 60)

    try:
        yield
    finally:
        print("lay4flow-remote Shutting down...")
        proxy_probe_task.cancel()
        await asyncio.gather(proxy_probe_task, return_exceptions=True)
        if not _SKIP_VIDS:
            await video_job_service.close()
        await generation_handler.file_cache.stop_cleanup_task()
        auto_unban_task_handle.cancel()
        try:
            await auto_unban_task_handle
        except asyncio.CancelledError:
            pass
        await token_manager.stop_protocol_refresher()
        await admin.close_profile_browser_viewer()
        if token_manager.profile_refresher is not None:
            manager = getattr(token_manager.profile_refresher, "manager", None)
            if manager is not None and hasattr(manager, "close_warm"):
                await manager.close_warm()
        print("File cache cleanup task stopped")
        print("429 auto-unban task stopped")
        print("Account session refresher stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with _flow_lifespan(app):
        try:
            if not _SKIP_VIDS:
                try:
                    await vids_runtime.start(config.get_raw_config().get("vids", {}))
                except GatewayError as error:
                    # A provider session failure must not stop Flow or its management UI.
                    vids_runtime.last_error_code = error.code
                if app.state.vids_auto_onboarding is not None:
                    for token in await db.get_all_tokens():
                        app.state.vids_auto_onboarding.schedule(token.id)
            yield
        finally:
            studio_jobs = getattr(app.state, "studio_jobs", None)
            if studio_jobs is not None:
                await studio_jobs.close()
            # Stop imports before their Profile/Vids dependencies are closed.
            import_jobs = getattr(token_manager, "_profile_import_jobs", None)
            if import_jobs is not None:
                await import_jobs.close()
            if not _SKIP_VIDS:
                if app.state.vids_auto_onboarding is not None:
                    await app.state.vids_auto_onboarding.close()
                await vids_runtime.close()


# Initialize components
services = create_services()
# Preserve these module-level names for existing integrations and the lifespan.
db = services.db
proxy_manager = services.proxy_manager
flow_client = services.flow_client
token_manager = services.token_manager
concurrency_manager = services.concurrency_manager
load_balancer = services.load_balancer
generation_handler = services.generation_handler
model_gateway = services.model_gateway
vids_runtime = services.vids_runtime
public_api_config = config.get_raw_config().get("public_api", {})
video_job_service = VideoJobService(
    db,
    execute_compat_video,
    max_active=int(public_api_config.get("video_workers", 4)),
    max_pending=int(public_api_config.get("video_pending_jobs", 100)),
    max_pending_bytes=int(public_api_config.get("video_pending_media_mib", 256)) * 1024 * 1024,
)

# Set dependencies
routes.set_generation_handler(generation_handler)
routes.set_model_gateway(model_gateway)
admin.set_dependencies(token_manager, proxy_manager, db, concurrency_manager)

# Create FastAPI app
app = FastAPI(
    title="lay4flow-remote",
    description="OpenAI-compatible API for Google VideoFX (Veo)",
    version=API_VERSION,
    lifespan=lifespan,
)
app.state.update_activity = UpdateActivity(Path(db.db_path).resolve().parent / "update-maintenance")
app.state.token_manager = token_manager
app.add_middleware(UpdateGuardMiddleware, activity=app.state.update_activity)
app.state.model_gateway = model_gateway
app.state.vids_dispatcher = services.vids_dispatcher
app.state.vids_runtime = vids_runtime
vids_registry_path = Path(
    config.get_raw_config().get("vids", {}).get("registry_path", "data/vids/bindings.json")
)
profile_manager = getattr(getattr(token_manager, "profile_refresher", None), "manager", None)
app.state.vids_onboarding = (
    VidsOnboardingService(
        db,
        vids_runtime,
        profile_manager,
        registry_path=vids_registry_path,
        secrets_dir=vids_registry_path.parent.parent / "vids-secrets",
        runtime_dir=vids_registry_path.parent / "accounts",
        get_settings=lambda: config.get_raw_config().get("vids", {}),
    )
    if profile_manager is not None
    else None
)
app.state.vids_auto_onboarding = (
    VidsAutoOnboarding(app.state.vids_onboarding) if app.state.vids_onboarding is not None else None
)
if app.state.vids_auto_onboarding is not None:
    token_manager.profile_ready_callback = app.state.vids_auto_onboarding.schedule


async def repair_vids_session(token_id):
    service = app.state.vids_onboarding
    if service is None:
        raise GatewayError(503, "profile_unavailable", "Existing account Profiles are unavailable")
    return await service.reconnect(token_id)


services.vids_dispatcher.repair_session = repair_vids_session


app.add_exception_handler(StarletteHTTPException, openai_http_exception_handler)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(routes.router)
app.include_router(create_images_router())
app.include_router(create_videos_router(lambda: video_job_service))
app.include_router(create_vids_media_router(services.vids_dispatcher))
app.include_router(
    create_vids_media_router(services.vids_dispatcher, prefix=VIDS_PUBLIC_PREFIX, auto_pool=True)
)
app.include_router(compat_router)
app.include_router(compat_images_router)
app.include_router(create_compat_videos_router(lambda: video_job_service))
app.include_router(admin.router)
app.include_router(
    create_studio_router(admin.verify_admin_token, get_vids=lambda: services.vids_dispatcher)
)
app.include_router(
    create_vids_admin_router(
        services.vids_dispatcher,
        admin.verify_admin_token,
        get_settings=lambda: config.get_raw_config().get("vids", {}),
        runtime=vids_runtime,
        repair_session=repair_vids_session,
    )
)
app.include_router(
    create_vids_onboarding_router(admin.verify_admin_token, lambda: app.state.vids_onboarding),
    include_in_schema=False,
)
app.include_router(
    create_vids_media_router(
        services.vids_dispatcher,
        admin.verify_admin_token,
        prefix=VIDS_CONSOLE_PREFIX,
        auto_pool=True,
        tags=["Vids console"],
    ),
    include_in_schema=False,
)


@app.get("/v1/openapi.json", include_in_schema=False)
async def public_openapi():
    """Expose developer endpoints without management/captcha internals."""
    return get_openapi(
        title="lay4flow-remote Developer API",
        version=API_VERSION,
        description="Chat image/video generation plus dedicated Images and asynchronous Videos. "
        "Flow parameters and compatibility limits are documented at /api-guide.",
        routes=[
            route for route in app.routes if getattr(route, "path", "") in PUBLIC_OPENAPI_PATHS
        ],
    )


@app.get("/api-docs", include_in_schema=False)
async def public_api_docs():
    return get_swagger_ui_html(
        openapi_url="/v1/openapi.json", title="lay4flow-remote API Reference"
    )


@app.get("/compat/v1/openapi.json", include_in_schema=False)
async def compat_openapi():
    """Keep the paid SKU catalog separate from the aggregate developer API."""
    return get_openapi(
        title="lay4flow-remote Gateway Compatibility",
        version=API_VERSION,
        description="Fixed media SKUs for OpenAI gateway channels. "
        "Successful Chat responses use 1000 settlement units, not upstream token measurements.",
        routes=[
            route for route in app.routes if getattr(route, "path", "") in COMPAT_OPENAPI_PATHS
        ],
    )


@app.get("/compat/api-docs", include_in_schema=False)
async def compat_api_docs():
    return get_swagger_ui_html(
        openapi_url="/compat/v1/openapi.json", title="lay4flow-remote SKU API Reference"
    )


@app.get("/api-guide", include_in_schema=False)
async def public_api_guide():
    return _frontend_page_response("api-guide.html")


@app.get("/providers/vids/openapi.json", include_in_schema=False)
async def vids_openapi():
    return get_openapi(
        title="lay4flow-remote Vids API",
        version=API_VERSION,
        description="Upload-first Vids API with automatic account selection. Bearer API key and Idempotency-Key are required "
        "for generation. Verified specifications and receipt recovery: /vids-api-guide.",
        routes=[
            route
            for route in app.routes
            if getattr(route, "path", "").startswith(VIDS_PUBLIC_PREFIX + "/v1/")
        ],
    )


@app.get("/providers/vids/api-docs", include_in_schema=False)
async def vids_api_docs():
    return get_swagger_ui_html(
        openapi_url="/providers/vids/openapi.json", title="lay4flow-remote Vids API Reference"
    )


@app.get("/vids-api-guide", include_in_schema=False)
@app.get("/vids/api-guide", include_in_schema=False)
async def vids_api_guide():
    return _frontend_page_response("vids-api-guide.html")


# Static files - serve tmp directory for cached files
tmp_dir = Path(__file__).parent.parent / "tmp"
tmp_dir.mkdir(exist_ok=True)
app.mount("/tmp", StaticFiles(directory=str(tmp_dir)), name="tmp")

# HTML routes for frontend
static_path = Path(__file__).parent.parent / "static"
# Only public script assets are mounted; protected HTML pages keep their auth guard.
app.mount("/static/js", StaticFiles(directory=str(static_path / "js")), name="scripts")
frontend_path = static_path / "app"
if (frontend_path / "assets").is_dir():
    app.mount(
        "/static/app/assets",
        StaticFiles(directory=str(frontend_path / "assets")),
        name="frontend-assets",
    )
_STATIC_PAGE_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _static_page_response(file_path: Path) -> FileResponse:
    return FileResponse(str(file_path), headers=_STATIC_PAGE_NO_CACHE_HEADERS)


def _frontend_page_response(legacy_filename: str):
    file_path = frontend_path / "index.html"
    if not file_path.is_file():
        file_path = static_path / legacy_filename
    if file_path.is_file():
        return _static_page_response(file_path)
    return HTMLResponse(content="<h1>Frontend Not Found</h1>", status_code=404)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the frontend entry; the application resolves its login state."""
    return _frontend_page_response("login.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page"""
    return _frontend_page_response("login.html")


@app.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request):
    """Management console page"""
    guard_response = _ensure_admin_page_session(request)
    if guard_response is not None:
        return guard_response
    return _frontend_page_response("manage.html")


@app.get("/test", response_class=HTMLResponse)
async def test_page(request: Request):
    """Model testing page"""
    guard_response = _ensure_admin_page_session(request)
    if guard_response is not None:
        return guard_response
    return _frontend_page_response("test.html")


@app.get("/vids", response_class=HTMLResponse, include_in_schema=False)
async def vids_page(request: Request):
    guard_response = _ensure_admin_page_session(request)
    if guard_response is not None:
        return guard_response
    return _frontend_page_response("vids.html")


@app.get("/vids-accounts", response_class=HTMLResponse, include_in_schema=False)
@app.get("/vids/accounts", response_class=HTMLResponse, include_in_schema=False)
async def vids_accounts_page(request: Request):
    guard_response = _ensure_admin_page_session(request)
    if guard_response is not None:
        return guard_response
    return _frontend_page_response("vids-accounts.html")


_LEGACY_ADMIN_PAGES = {
    "manage": "manage.html",
    "test": "test.html",
    "vids": "vids.html",
    "vids-accounts": "vids-accounts.html",
    "api-guide": "api-guide.html",
    "vids-api-guide": "vids-api-guide.html",
}


@app.get("/legacy/{page}", response_class=HTMLResponse, include_in_schema=False)
async def legacy_admin_page(page: str, request: Request):
    """Keep original workflows and the publicly accessible developer handbooks."""
    filename = _LEGACY_ADMIN_PAGES.get(page)
    if filename is None:
        return HTMLResponse(content="<h1>Page Not Found</h1>", status_code=404)
    if page not in {"api-guide", "vids-api-guide"}:
        guard_response = _ensure_admin_page_session(request)
        if guard_response is not None:
            return guard_response
    return _static_page_response(static_path / filename)


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint for the main lay4flow-remote service."""
    payload = await render_main_metrics(db, concurrency_manager=concurrency_manager)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


def _ensure_admin_page_session(request: Request):
    token = admin.get_admin_token_from_cookie(request)
    if not admin.is_admin_session_token_valid(token):
        return RedirectResponse(url="/login", status_code=302)
    return None
