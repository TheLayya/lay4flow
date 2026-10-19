"""Construct the application services once, without starting background workers."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core.config import config
from ..core.database import Database
from .concurrency_manager import ConcurrencyManager
from .flow_client import FlowClient
from .generation_handler import GenerationHandler
from .load_balancer import LoadBalancer
from .model_gateway import ModelGateway
from .proxy_manager import ProxyManager
from .token_manager import TokenManager
from .vids.dispatcher import VidsProviderDispatcher
from .vids.gateway import GatewayError
from .vids.runtime import VidsRuntime


@dataclass(frozen=True)
class Services:
    db: Database
    proxy_manager: ProxyManager
    flow_client: FlowClient
    token_manager: TokenManager
    concurrency_manager: ConcurrencyManager
    load_balancer: LoadBalancer
    generation_handler: GenerationHandler
    model_gateway: ModelGateway
    vids_dispatcher: VidsProviderDispatcher
    vids_runtime: VidsRuntime


def create_services(db: Database | None = None, profile_refresher=None) -> Services:
    """Share the same database, proxy policy and concurrency manager across services."""
    db = db if db is not None else Database()
    proxy = ProxyManager(db)
    client = FlowClient(proxy, db)
    tokens = TokenManager(db, client, profile_refresher=profile_refresher)
    if profile_refresher is None and config.browser_profile_enabled:
        try:
            import cloakbrowser  # noqa: F401
        except ImportError as error:
            raise RuntimeError(
                "Browser Profile is enabled but cloakbrowser is not installed; "
                "install requirements.txt or run `pip install cloakbrowser==0.5.11`"
            ) from error
        from .browser.cloak_backend import CloakBrowserBackend
        from .browser.profile_session_refresher import ProfileSessionRefresher
        from .browser.profile_verifier import ProfileVerifier
        from .browser.profiles import ProfileManager

        backend = CloakBrowserBackend(headless=config.browser_profile_headless)
        manager = ProfileManager(
            Path(config.browser_profile_root),
            backend,
            max_concurrent=config.browser_profile_max_concurrent,
            warm_slots=config.browser_profile_warm_slots,
            warm_idle_seconds=config.browser_profile_warm_idle_seconds,
        )
        tokens.profile_refresher = ProfileSessionRefresher(manager, ProfileVerifier(), tokens)
    concurrency = ConcurrencyManager()
    balancer = LoadBalancer(tokens, concurrency)
    handler = GenerationHandler(client, tokens, balancer, db, concurrency, proxy)
    gateway = ModelGateway()
    vids = VidsProviderDispatcher(db)
    runtime = VidsRuntime(vids, proxy)

    @asynccontextmanager
    async def proxy_maintenance(token, target_url):
        async with balancer.proxy_maintenance(token.id):
            try:
                async with runtime.proxy_switch(token.id, target_url):
                    yield
            except GatewayError as error:
                raise ValueError(str(error)) from None

    tokens.account_proxy_maintenance = proxy_maintenance
    return Services(
        db,
        proxy,
        client,
        tokens,
        concurrency,
        balancer,
        handler,
        gateway,
        vids,
        runtime,
    )
