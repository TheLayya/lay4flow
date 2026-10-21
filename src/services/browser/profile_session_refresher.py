"""Refresh adapter that rotates the session inside a verified Profile."""

import asyncio
import contextvars
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..flow.google_cookies import GoogleCookieJar
from ..proxy_manager import ProxyManager
from .profile_verifier import ProfileVerifier
from .profiles import ProfileManager, ProfileNotReadyError
from .token_account_probe import SESSION_URL, _session_cookie_from_browser


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
        self._active_context: contextvars.ContextVar[tuple[str, Any] | None] = (
            contextvars.ContextVar("lay4flow_active_profile_context", default=None)
        )
        self._renewal_step: contextvars.ContextVar[str] = contextvars.ContextVar(
            "lay4flow_profile_renewal_step", default=""
        )

    @asynccontextmanager
    async def use_context(self, email: str, context: Any):
        marker = self._active_context.set((str(email).strip().casefold(), context))
        try:
            yield context
        finally:
            self._active_context.reset(marker)

    def has_active_context(self, email: str) -> bool:
        active = self._active_context.get()
        return bool(active and active[0] == str(email).strip().casefold())

    async def refresh_active(self, token: AccountLike, force: bool = False) -> dict[str, str]:
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
            return await self.refresh_in_context(token, active[1], force=True)
        return await self.refresh_in_context(token, active[1])

    async def _proxy_url(self, token: AccountLike) -> str:
        manager = getattr(getattr(self.token_manager, "flow_client", None), "proxy_manager", None)
        if manager is None:
            manager = ProxyManager(self.token_manager.db)
        return await manager.resolve_account_proxy(token) or ""

    async def remove_profile(self, email: str) -> None:
        await self.manager.remove(email)

    async def _renew_session(self, context: Any) -> str:
        request = context.request
        self._renewal_step.set("oauth_csrf")
        csrf = await request.get("https://labs.google/fx/api/auth/csrf", timeout=15000)
        if csrf.status != 200:
            return "transient_error"
        payload = await csrf.json()
        self._renewal_step.set("oauth_signin")
        signin = await request.post(
            "https://labs.google/fx/api/auth/signin/google",
            form={
                "redirect": "false",
                "csrfToken": payload["csrfToken"],
                "callbackUrl": "https://labs.google/fx/api",
                "json": "true",
            },
            headers={"Origin": "https://labs.google", "Referer": "https://labs.google/fx"},
            timeout=15000,
        )
        if signin.status != 200:
            return "transient_error"
        redirect = (await signin.json()).get("url")
        address = urlsplit(redirect or "")
        if address.scheme != "https" or address.hostname != "accounts.google.com":
            return "transient_error"
        page = await context.new_page()
        try:
            self._renewal_step.set("oauth_redirect")
            await page.goto(redirect, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            address = urlsplit(page.url)
            if address.hostname == "accounts.google.com":
                self._renewal_step.set("oauth_confirmation")
                text = (await page.locator("body").inner_text()).casefold()
                if (
                    "/signin/challenge/" in address.path
                    or "/signin/identifier" in address.path
                    or "signed out" in text
                    or "已退出" in text
                ):
                    return "login_required"
                return "transient_error"
            if address.hostname in {"labs.google", "flow.google.com"}:
                return "session_renewed"
            return "transient_error"
        finally:
            await asyncio.wait_for(page.close(), 10)

    async def refresh_in_context(
        self, token: AccountLike, context: Any, force: bool = False
    ) -> dict[str, str]:
        from .token_account_probe import TokenManagerAccountProbe

        self._renewal_step.set("")
        probe = TokenManagerAccountProbe(self.token_manager, token)
        session_response = None
        # A forced refresh is used after a newly imported Profile has already
        # restored its portable state. Do not spend another 30 seconds on the
        # session navigation: OAuth renewal below is the authoritative check.
        if not force:
            page = await context.new_page()
            try:
                response = await asyncio.wait_for(
                    page.goto(
                        SESSION_URL,
                        wait_until="domcontentloaded",
                    ),
                    30,
                )
                # Only reuse an actual response from the session endpoint. A
                # redirect, absent response or unreadable JSON keeps the old probe.
                if getattr(response, "url", None) == SESSION_URL:
                    status = getattr(response, "status", None)
                    if isinstance(status, int) and not isinstance(status, bool):
                        try:
                            payload = (
                                await asyncio.wait_for(response.json(), 15)
                                if status == 200
                                else None
                            )
                        except Exception:
                            pass
                        else:
                            if status != 200 or isinstance(payload, dict):
                                session_response = status, payload
            finally:
                await asyncio.wait_for(page.close(), 30)
        probe.set_context(context, session_response=session_response)
        browser_cookie_rows = await context.cookies()
        session_token = _session_cookie_from_browser(browser_cookie_rows)
        if force:
            # An explicit/manual refresh is a renewal request, even when the
            # current AT still has hours left.  Do this before the final probe
            # so the saved session and AT come from the renewed browser state.
            try:
                renewal = await self._renew_session(context)
            except Exception:
                renewal = "transient_error"
            if renewal == "session_renewed":
                browser_cookie_rows = await context.cookies()
                session_token = _session_cookie_from_browser(browser_cookie_rows)
                probe.set_context(context)
                account = await self.verifier.verify_account(str(token.email), probe)
                reason = account.reason
                if not session_token or reason == "session_invalid":
                    reason = "transient_error"
            else:
                account = None
                reason = renewal
        elif session_token:
            account = await self.verifier.verify_account(str(token.email), probe)
            reason = account.reason
        else:
            # A Flow-migrated account can be signed in with Google/Flow
            # cookies while lacking the legacy Labs NextAuth cookie. Give the
            # existing OAuth renewal path one chance before declaring logout.
            account = None
            has_google_login = any(
                str(cookie.get("domain") or "").lstrip(".").lower()
                in {"google.com", "accounts.google.com", "flow.google.com", "labs.google"}
                for cookie in browser_cookie_rows
            )
            reason = "session_invalid" if has_google_login else "login_required"
        expiring = (
            reason == "account_ready"
            and isinstance(probe.validated_session, dict)
            and probe.validated_session.get("at_expires") is not None
            and probe.validated_session["at_expires"]
            <= datetime.now(timezone.utc) + timedelta(hours=1)
        )
        if not force and (reason == "session_invalid" or expiring):
            probe.network_step = ""
            try:
                renewal = await self._renew_session(context)
            except Exception:
                renewal = "transient_error"
            if renewal == "session_renewed":
                probe.set_context(context)
                account = await self.verifier.verify_account(str(token.email), probe)
                session_token = _session_cookie_from_browser(await context.cookies())
                reason = account.reason
                if not session_token or reason == "session_invalid":
                    reason = "transient_error"
            else:
                reason = renewal
        if reason != "account_ready":
            await self.manager.mark_blocked(str(token.email), reason)
            error = ProfileNotReadyError(str(token.email), reason)
            error.profile_network_step = probe.network_step or self._renewal_step.get()
            raise error
        google_rows = []
        for cookie in await context.cookies():
            row = dict(cookie)
            row["hostOnly"] = not str(row.get("domain") or "").startswith(".")
            if row.get("expires", -1) > 0:
                row["expirationDate"] = row["expires"]
            google_rows.append(row)
        result = {"session_token": session_token}
        if probe.validated_session:
            result["_validated_session"] = dict(probe.validated_session)
        exported = GoogleCookieJar(google_rows).export()
        if exported:
            result["google_cookies"] = json.dumps(exported)
        return result

    async def refresh(self, token: AccountLike, force: bool = False) -> dict[str, str]:
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
        browser_cookies.extend(_browser_cookie_rows(session_cookie_rows))
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
