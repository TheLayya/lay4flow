"""Validate current Flow credentials inside a verified browser Profile."""

import contextvars
import json
from contextlib import asynccontextmanager
from typing import Any, Protocol

from ..flow.flow_rpc import FlowRPCError
from ..flow.google_cookies import GoogleCookieJar
from ..proxy_manager import ProxyManager
from .flow_session import account_http_session
from .profile_verifier import ProfileVerifier
from .profiles import ProfileManager, ProfileNotReadyError


class AccountLike(Protocol):
    email: str


def _browser_cookie_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    same_sites = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None"}
    for row in rows:
        domain = str(row["domain"]).lstrip(".")
        cookie = {
            "name": row["name"],
            "value": row["value"],
            "path": row.get("path") or "/",
            "secure": bool(row.get("secure")),
            "httpOnly": bool(row.get("httpOnly")),
        }
        cookie["domain"] = domain if row.get("hostOnly", False) else "." + domain
        if row.get("expirationDate") is not None:
            cookie["expires"] = row["expirationDate"]
        same_site = same_sites.get(str(row.get("sameSite") or "").lower())
        if same_site:
            cookie["sameSite"] = same_site
        result.append(cookie)
    return result


class ProfileSessionRefresher:
    def __init__(
        self,
        manager: ProfileManager,
        verifier: ProfileVerifier,
        token_manager: Any,
        active_context_provider: Any = None,
    ):
        self.manager = manager
        self.verifier = verifier
        self.token_manager = token_manager
        # A viewer may register an async context manager here so refreshes
        # initiated by the scheduler can borrow its already-open Profile.
        # Keep this optional so the refresher remains usable without a viewer.
        self.active_context_provider = active_context_provider
        # Preserve request-scoped reuse for explicit viewer operations. The
        # provider above supplies a safe lease to independent refresh tasks.
        self._active_context: contextvars.ContextVar[tuple[str, Any, bool] | None] = (
            contextvars.ContextVar("lay4flow_active_profile_context", default=None)
        )

    @asynccontextmanager
    async def use_context(self, email: str, context: Any, *, accept_existing_session: bool = False):
        marker = self._active_context.set(
            (str(email).strip().casefold(), context, bool(accept_existing_session))
        )
        try:
            yield context
        finally:
            self._active_context.reset(marker)

    def has_active_context(self, email: str) -> bool:
        active = self._active_context.get()
        return bool(active and active[0] == str(email).strip().casefold())

    async def refresh_active(self, token: AccountLike, force: bool = False) -> dict[str, Any]:
        active = self._active_context.get()
        if not active or active[0] != str(token.email).strip().casefold():
            raise ProfileNotReadyError(str(token.email), "transient_error")
        spec = await self.manager.spec(str(token.email))
        result = await self.verifier.verify_environment(
            active[1], str(spec.profile_dir), allow_viewport_change=True
        )
        if result != "environment_stable":
            await self.manager.mark_blocked(str(token.email), result)
            raise ProfileNotReadyError(str(token.email), result)
        if force:
            kwargs = {"accept_existing_session": True} if active[2] else {}
            return await self.refresh_in_context(token, active[1], force=True, **kwargs)
        return await self.refresh_in_context(token, active[1])

    async def _proxy_url(self, token: AccountLike) -> str:
        manager = getattr(getattr(self.token_manager, "flow_client", None), "proxy_manager", None)
        if manager is None:
            manager = ProxyManager(self.token_manager.db)
        return await manager.resolve_account_proxy(token) or ""

    async def remove_profile(self, email: str) -> None:
        await self.manager.remove(email)

    async def refresh_in_context(
        self,
        token: AccountLike,
        context: Any,
        force: bool = False,
        accept_existing_session: bool = False,
    ) -> dict[str, Any]:
        from .token_account_probe import TokenManagerAccountProbe

        # Every check reads fresh Flow identity and credits in this Cookie jar.
        probe = TokenManagerAccountProbe(self.token_manager, token)
        probe.set_context(context)
        account = await self.verifier.verify_account(str(token.email), probe)
        if account.reason != "account_ready":
            await self.manager.mark_blocked(str(token.email), account.reason)
            error = ProfileNotReadyError(str(token.email), account.reason)
            error.profile_network_step = probe.network_step
            raise error
        google_rows = []
        for cookie in await context.cookies():
            row = dict(cookie)
            row.setdefault("hostOnly", not str(row.get("domain") or "").startswith("."))
            if row.get("expires", -1) > 0:
                row["expirationDate"] = row["expires"]
            google_rows.append(row)
        return {
            "_validated_session": dict(probe.validated_session),
            "google_cookies": json.dumps(GoogleCookieJar(google_rows).export()),
        }

    async def refresh(self, token: AccountLike, force: bool = False) -> dict[str, Any]:
        active = self._active_context.get()
        if active and active[0] == str(token.email).strip().casefold():
            if force:
                return await self.refresh_active(token, force=True)
            return await self.refresh_active(token)

        provider = self.active_context_provider
        if provider is not None:
            async with provider(token) as context:
                if context is not None:
                    async with self.use_context(str(token.email), context):
                        if force:
                            return await self.refresh_active(token, force=True)
                        return await self.refresh_active(token)
        proxy_url = await self._proxy_url(token)
        if getattr(token, "google_cookies", ""):
            try:
                async with account_http_session(
                    self.token_manager, token, proxy_url, persist=False
                ) as context:
                    return await self.refresh_in_context(token, context, force=force)
            except (FlowRPCError, ProfileNotReadyError) as error:
                # Network/proxy failures and identity changes keep their normal
                # backoff. Only expired/missing authentication needs Chromium.
                if error.reason not in {"login_required", "session_invalid"}:
                    if isinstance(error, FlowRPCError):
                        failure = ProfileNotReadyError(str(token.email), error.reason)
                        failure.profile_network_step = error.network_step
                        raise failure from None
                    raise
        session_factory = getattr(self.manager, "warm_session", None)
        warm = getattr(self.manager, "warm_slots", 0) > 0 and callable(session_factory)
        if warm:
            session = session_factory(str(token.email), proxy_url=proxy_url)
        else:
            session = self.manager.credential_session(
                str(token.email), self.verifier, proxy_url=proxy_url
            )
        async with session as context:
            if warm:
                kwargs = {}
                if getattr(self.manager.backend, "options", {}).get("headless") is False:
                    kwargs["allow_viewport_change"] = True
                result = await self.verifier.verify_environment(
                    context, str((await self.manager.spec(str(token.email))).profile_dir), **kwargs
                )
                if result != "environment_stable":
                    await self.manager.mark_blocked(str(token.email), result)
                    raise ProfileNotReadyError(str(token.email), result)
            if force:
                return await self.refresh_in_context(token, context, force=True)
            return await self.refresh_in_context(token, context)

    async def bootstrap(
        self,
        token: AccountLike,
        google_cookies: str,
        session_cookie_rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        from .token_account_probe import TokenManagerAccountProbe

        browser_cookies = _browser_cookie_rows(GoogleCookieJar(google_cookies).export())
        browser_cookies.extend(_browser_cookie_rows(GoogleCookieJar(session_cookie_rows).export()))
        if not browser_cookies:
            raise RuntimeError("Profile bootstrap has no browser cookies")
        proxy_url = await self._proxy_url(token)
        probe = TokenManagerAccountProbe(self.token_manager, token)
        async with self.manager.bootstrap_session(
            str(token.email),
            self.verifier,
            browser_cookies,
            proxy_url=proxy_url,
            account_probe=probe,
        ) as context:
            if not callable(getattr(getattr(context, "request", None), "get", None)):
                raise ProfileNotReadyError(str(token.email), "account_probe_error")
            probe.set_context(context)
        await self.manager.mark_verified(str(token.email))
        return dict(probe.validated_session) if isinstance(probe.validated_session, dict) else None
