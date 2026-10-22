"""HTTP requests from a Profile snapshot on its validated fixed exit."""

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request as URLRequest

from curl_cffi.requests import AsyncSession

from ..browser.cloak_proxy import transport_proxy
from .fingerprint import resolve_browser_impersonate
from .flow_rpc import FlowRPCError
from .google_cookies import GoogleCookieJar


def _key(row):
    domain = str(row["domain"]).lstrip(".")
    host_only = row.get("hostOnly", not str(row["domain"]).startswith("."))
    return row["name"], domain if host_only else "." + domain, row.get("path") or "/"


class _Response:
    def __init__(self, response):
        self.status = response.status_code
        self.url = str(response.url)
        self.headers = response.headers
        self._text = response.text

    async def text(self):
        return self._text

    async def json(self):
        return json.loads(self._text)


def _identity(values):
    ua = values.get("user_agent") if isinstance(values, dict) else None
    if (
        not isinstance(ua, str)
        or not ua.strip()
        or len(ua) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in ua)
    ):
        raise FlowRPCError("account_probe_error", "flow_http_identity")
    raw_languages = values.get("languages") or [values.get("language")]
    languages = []
    for value in raw_languages[:10] if isinstance(raw_languages, (list, tuple)) else []:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*", value):
            if value.casefold() not in {language.casefold() for language in languages}:
                languages.append(value)
    if not languages:
        raise FlowRPCError("account_probe_error", "flow_http_identity")
    return {
        "User-Agent": ua.strip(),
        "Accept-Language": ",".join(
            value if index == 0 else f"{value};q={1 - index / 10:.1f}"
            for index, value in enumerate(languages)
        ),
    }, resolve_browser_impersonate(ua)


async def _profile_identity(context):
    """Read navigator identity without navigating or touching a manual-login page."""
    page = next((item for item in getattr(context, "pages", []) if not item.is_closed()), None)
    owned = page is None
    try:
        if page is None:
            page = await asyncio.wait_for(context.new_page(), 10)
        values = await asyncio.wait_for(
            page.evaluate(
                "() => ({user_agent: navigator.userAgent, language: navigator.language, "
                "languages: Array.from(navigator.languages || [])})"
            ),
            10,
        )
        return _identity(values)
    except Exception:
        raise FlowRPCError("account_probe_error", "flow_http_identity") from None
    finally:
        if owned and page is not None:
            await asyncio.wait_for(page.close(), 10)


@asynccontextmanager
async def profile_http_request(context, proxy):
    """Keep the scoped Profile jar authoritative, including response deletions."""
    original = await context.cookies()
    rows = []
    for cookie in original:
        row = dict(cookie)
        row["hostOnly"] = not str(row.get("domain") or "").startswith(".")
        if row.get("expires", -1) > 0:
            row["expirationDate"] = row["expires"]
        rows.append(row)
    identity_headers, impersonate = await _profile_identity(context)
    async with _http_request(rows, identity_headers, impersonate, proxy) as transport:
        before = {_key(row): row for row in GoogleCookieJar(rows).export()}
        try:
            yield transport
        finally:
            after = {_key(row): row for row in await transport.cookies()}
            current_rows = []
            for cookie in await context.cookies():
                row = dict(cookie)
                row["hostOnly"] = not str(row.get("domain") or "").startswith(".")
                if row.get("expires", -1) > 0:
                    row["expirationDate"] = row["expires"]
                current_rows.append(row)
            current = {_key(row): row for row in GoogleCookieJar(current_rows).export()}
            for key in before.keys() - after.keys():
                if current.get(key) == before[key]:
                    await context.clear_cookies(name=key[0], domain=key[1], path=key[2])
            changed = [
                row
                for key, row in after.items()
                if before.get(key) != row and current.get(key) == before.get(key)
            ]
            if changed:
                from ..browser.profile_session_refresher import _browser_cookie_rows

                await context.add_cookies(_browser_cookie_rows(changed))


@asynccontextmanager
async def snapshot_http_request(google_cookies, identity, proxy):
    """Use persisted login state and navigator identity without starting Chromium."""
    headers, impersonate = _identity(identity)
    async with _http_request(google_cookies, headers, impersonate, proxy) as transport:
        yield transport


@asynccontextmanager
async def _http_request(rows, identity_headers, impersonate, proxy):
    jar = GoogleCookieJar(rows)
    async with transport_proxy(proxy) as network_proxy, AsyncSession(trust_env=False) as session:

        class Request:
            async def send(self, method, url, **kwargs):
                headers = dict(kwargs.pop("headers", {}))
                for key in list(headers):
                    if key.lower() in {"user-agent", "accept-language"}:
                        del headers[key]
                headers.update(identity_headers)
                headers["Cookie"] = jar.header(url)
                timeout = kwargs.pop("timeout", 20000) / 1000
                kwargs.pop("max_redirects", None)
                if "form" in kwargs:
                    kwargs["data"] = kwargs.pop("form")
                response = await session.request(
                    method,
                    url,
                    headers=headers,
                    proxy=network_proxy,
                    timeout=timeout,
                    allow_redirects=False,
                    impersonate=impersonate,
                    default_headers=False,
                    discard_cookies=True,
                    **kwargs,
                )
                jar.merge(response.headers, str(response.url))
                return _Response(response)

            async def get(self, url, **kwargs):
                return await self.send("GET", url, **kwargs)

            async def post(self, url, **kwargs):
                return await self.send("POST", url, **kwargs)

        async def cookies(urls=None):
            exported = jar.export()
            if urls is None:
                return exported
            urls = [urls] if isinstance(urls, str) else urls
            if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
                raise ValueError("Profile Cookie URLs must be a string or list")
            selected = set()
            for url in urls:
                # header() initializes the stdlib expiry clock and enforces the
                # same trusted-origin boundary as the outbound Cookie header.
                try:
                    parsed = urlsplit(url)
                    port = parsed.port
                except ValueError:
                    continue
                if (
                    parsed.scheme not in ("http", "https")
                    or port not in (None, 80 if parsed.scheme == "http" else 443)
                    or not (
                        parsed.hostname == "google.com"
                        or str(parsed.hostname or "").endswith(".google.com")
                    )
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    continue
                jar.header(url)
                policy = jar.jar._policy
                policy._now = int(time.time())
                request = URLRequest(url)
                for cookie in jar.jar:
                    if (
                        policy.domain_return_ok(cookie.domain, request)
                        and policy.path_return_ok(cookie.path, request)
                        and policy.return_ok(cookie, request)
                    ):
                        selected.add((cookie.name, cookie.domain, cookie.path))
            return [
                row
                for row in exported
                if (
                    row["name"],
                    (
                        row["domain"].lstrip(".")
                        if row.get("hostOnly")
                        else "." + row["domain"].lstrip(".")
                    ),
                    row["path"],
                )
                in selected
            ]

        yield SimpleNamespace(request=Request(), cookies=cookies, flow_bootstrap_cache={})
