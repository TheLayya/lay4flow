"""HTTP requests from a Profile snapshot on its validated fixed exit."""

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request as URLRequest

from curl_cffi.requests import AsyncSession

from ..browser.cloak_proxy import transport_proxy
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
    jar = GoogleCookieJar(rows)
    before = {_key(row): row for row in jar.export()}
    async with transport_proxy(proxy) as network_proxy, AsyncSession(trust_env=False) as session:

        class Request:
            async def send(self, method, url, **kwargs):
                headers = dict(kwargs.pop("headers", {}))
                headers["Cookie"] = jar.header(url)
                headers.setdefault("Accept-Language", "en-US,en;q=0.9")
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
                    impersonate="chrome136",
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

        try:
            yield SimpleNamespace(request=Request(), cookies=cookies)
        finally:
            after = {_key(row): row for row in jar.export()}
            current_rows = []
            for cookie in await context.cookies():
                row = dict(cookie)
                row["hostOnly"] = not str(row.get("domain") or "").startswith(".")
                if row.get("expires", -1) > 0:
                    row["expirationDate"] = row["expires"]
                current_rows.append(row)
            current = {_key(row): row for row in GoogleCookieJar(current_rows).export()}
            # GUI/manual login may rotate the same Cookie while Curl is in
            # flight. Only merge entries whose original Profile state still
            # matches the snapshot; retain concurrent browser updates.
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
