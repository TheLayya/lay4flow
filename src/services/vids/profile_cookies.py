"""Read Vids cookies from an existing account Profile without Flow refresh."""

import asyncio
import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..browser.profile_verifier import ProfileVerifier
from ..browser.profiles import ProfileManager
from .client import VidsTransportError


def _editor_url(value: str):
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError
        address = urlsplit(value)
        query = parse_qsl(address.query, keep_blank_values=True, strict_parsing=True)
        if (
            address.scheme != "https"
            or address.hostname != "docs.google.com"
            or address.port not in (None, 443)
            or address.username is not None
            or address.password is not None
            or address.fragment
            or not re.fullmatch(r"/videos/(?:u/[0-9]+/)?d/[A-Za-z0-9_-]+/edit", address.path)
            or (query and (len(query) != 1 or query[0][0] != "authuser"))
            or (query and not re.fullmatch(r"[0-9]+", query[0][1]))
        ):
            raise ValueError
        return address
    except (TypeError, ValueError):
        raise ValueError("An existing Google Vids editor URL is required") from None


def _cookie_rows(cookies: Any) -> list[dict[str, Any]]:
    if not isinstance(cookies, list):
        raise ValueError("Invalid Profile cookie rows")
    result = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError("Invalid Profile cookie rows")
        name, value, domain = (cookie.get(key) for key in ("name", "value", "domain"))
        if (
            not all(isinstance(item, str) and item for item in (name, value, domain))
            or any(ord(char) <= 32 or ord(char) >= 127 or char in "=;" for char in name)
            or any(ord(char) < 32 or ord(char) >= 127 or char == ";" for char in value)
            or any(char.isspace() or char in "/\\\0" for char in domain)
        ):
            raise ValueError("Invalid Profile cookie rows")
        if domain.lower() not in {
            "google.com",
            ".google.com",
            "docs.google.com",
            ".docs.google.com",
        }:
            continue
        path = cookie.get("path")
        secure, http_only = cookie.get("secure"), cookie.get("httpOnly")
        host_only = cookie.get("hostOnly", not domain.startswith("."))
        expires = cookie.get("expirationDate", cookie.get("expires", -1))
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or any(ord(char) < 32 for char in path)
            or not all(type(item) is bool for item in (secure, http_only, host_only))
            or type(expires) not in (int, float)
            or not math.isfinite(expires)
            or (expires < 0 and expires != -1)
            or ("session" in cookie and type(cookie["session"]) is not bool)
            or cookie.get("partitionKey")
            or cookie.get("partitioned")
            or (name.startswith("__Secure-") and not secure)
            or (name.startswith("__Host-") and (not secure or not host_only or path != "/"))
        ):
            raise ValueError("Invalid Profile cookie rows")
        row = dict(cookie)
        row["hostOnly"] = host_only
        if "expirationDate" not in row and expires > 0:
            row["expirationDate"] = expires
        result.append(row)
    if not result:
        raise ValueError("Profile has no Vids cookies")
    return result


class VidsProfileCookieSource:
    def __init__(
        self, manager: ProfileManager, account_key: str, verifier: ProfileVerifier | None = None
    ):
        if (
            not isinstance(account_key, str)
            or not account_key.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in account_key)
        ):
            raise ValueError("A valid Profile account key is required")
        self.manager = manager
        self.account_key = account_key.strip()
        self.verifier = verifier or ProfileVerifier()

    async def __call__(self, editor_url: str, proxy_url: str | None = None) -> list[dict[str, Any]]:
        target = _editor_url(editor_url)
        await self.manager.spec(self.account_key, existing_only=True)
        try:
            async with self.manager.credential_session(
                self.account_key, self.verifier, proxy_url=proxy_url, existing_only=True
            ) as context:
                page = None
                try:
                    async with asyncio.timeout(self.verifier.timeout_seconds):
                        page = await context.new_page()
                        await page.goto(editor_url, wait_until="domcontentloaded")
                        observed = urlsplit(page.url)
                        if (
                            observed.scheme != target.scheme
                            or observed.hostname != target.hostname
                            or observed.port not in (None, 443)
                            or observed.username is not None
                            or observed.password is not None
                            or observed.path != target.path
                        ):
                            raise ValueError
                        return _cookie_rows(await context.cookies())
                finally:
                    if page is not None:
                        await asyncio.wait_for(page.close(), timeout=self.verifier.timeout_seconds)
        except Exception:
            raise VidsTransportError("Vids Profile cookie refresh failed") from None
