"""Scoped Google login cookies; stdlib owns selection, expiry and Set-Cookie semantics."""

import json
from email.message import Message
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy, parse_ns_headers
from http.cookies import CookieError, SimpleCookie
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request

GOOGLE_HOSTS = {"google.com", "www.google.com", "accounts.google.com", "flow.google.com"}
ACCOUNT_COOKIES = {
    "LSID",
    "__Host-1PLSID",
    "__Host-3PLSID",
    "GAPS",
    "__Host-GAPS",
    "__Host-GAPSTS",
    "ACCOUNT_CHOOSER",
}
FLOW_COOKIES = {"OSID", "__Secure-OSID"}
COOKIE_ATTRIBUTES = {
    "domain",
    "path",
    "expires",
    "max-age",
    "secure",
    "httponly",
    "samesite",
    "priority",
}
FLOW_COOKIE_COMBO_NAMES = ("__Secure-1PSID", "SID", "OSID", "__Secure-1PSIDTS")


def extract_flow_cookie_combo(raw: Any) -> dict[str, Any]:
    """Format saved, unexpired Flow cookies without opening a browser or refreshing them."""
    jar = GoogleCookieJar(raw)
    # Select the same scopes a Flow request uses. Prefer the host-specific value
    # when both a parent cookie and a Flow cookie have the same name.
    cookies = {}
    for row in sorted(
        jar.export(),
        key=lambda item: (
            len(item["domain"].removeprefix(".")),
            len(item["path"]),
        ),
    ):
        domain = row["domain"].removeprefix(".")
        if row["name"] not in FLOW_COOKIE_COMBO_NAMES:
            continue
        if domain != "flow.google.com" and (domain != "google.com" or row["hostOnly"]):
            continue
        if row["path"] != "/":
            continue
        cookies[row["name"]] = row["value"]
    names = [name for name in FLOW_COOKIE_COMBO_NAMES if cookies.get(name)]
    missing = [name for name in FLOW_COOKIE_COMBO_NAMES[:3] if not cookies.get(name)]
    return {
        "cookie": "::".join(cookies[name] for name in names) if not missing else "",
        "cookie_names": names,
        "missing": missing,
        "has_1psidts": bool(cookies.get(FLOW_COOKIE_COMBO_NAMES[-1])),
    }


def _trusted_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in GOOGLE_HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def _valid_cookie(cookie: Cookie) -> bool:
    if cookie.domain.removeprefix(".") not in GOOGLE_HOSTS:
        return False
    if cookie.name.startswith(("__Secure-", "__Host-")) and not cookie.secure:
        return False
    if cookie.name.startswith("__Host-") and (cookie.domain_specified or cookie.path != "/"):
        return False
    return not cookie.name.startswith(("__Secure-next-auth.", "next-auth."))


class _GooglePolicy(DefaultCookiePolicy):
    def __init__(self):
        super().__init__(strict_ns_domain=self.DomainStrictNonDomain)

    def set_ok(self, cookie, request):
        return (
            _trusted_url(request.full_url)
            and _valid_cookie(cookie)
            and super().set_ok(cookie, request)
        )


def _legacy_row(name: str, value: str) -> dict[str, Any]:
    # Old dict/header exports lost scope and expiry. These are compatibility
    # assumptions, not recovered browser metadata; retain their provenance.
    if not isinstance(name, str):
        return {}
    domain = (
        "accounts.google.com"
        if name in ACCOUNT_COOKIES
        else ("flow.google.com" if name in FLOW_COOKIES else "google.com")
    )
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "hostOnly": domain != "google.com" or name.startswith("__Host-"),
        "secure": name.startswith(("__Secure-", "__Host-")),
        "scopeSource": "legacy",
    }


def _rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            cookies = SimpleCookie()
            try:
                cookies.load(raw)
            except CookieError:
                return []
            raw = {name: cookie.value for name, cookie in cookies.items()}
    if isinstance(raw, dict):
        for wrapper in ("refreshCookie", "cookies"):
            if wrapper in raw:
                return _rows(raw[wrapper])
        return [_legacy_row(name, value) for name, value in raw.items() if isinstance(value, str)]
    if isinstance(raw, list):
        return [
            row
            if row.get("domain")
            else {**_legacy_row(row.get("name", ""), row.get("value", "")), **row}
            for row in raw
            if isinstance(row, dict)
        ]
    return []


class GoogleCookieJar:
    """Per-login Google jar, separate from Labs cookies and the transport's jar."""

    def __init__(self, raw: Any):
        self.jar = CookieJar(_GooglePolicy())
        for row in _rows(raw):
            name, value = row.get("name"), row.get("value")
            if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
                continue
            if (
                name.lower() in COOKIE_ATTRIBUTES
                or row.get("partitionKey")
                or row.get("partitioned")
            ):
                continue
            if any(ord(char) <= 32 or ord(char) == 127 or char in ";=" for char in name) or any(
                ord(char) < 32 or ord(char) == 127 or char == ";" for char in value
            ):
                continue
            if any(
                field in row and not isinstance(row[field], bool)
                for field in ("hostOnly", "secure", "httpOnly", "session")
            ):
                continue
            domain = str(row.get("domain") or "").lower()
            host = domain.removeprefix(".")
            if host not in GOOGLE_HOSTS:
                continue
            host_only = row.get("hostOnly", not domain.startswith("."))
            path = row.get("path") or "/"
            if not isinstance(path, str) or not path.startswith("/"):
                continue
            expires = row.get("expirationDate", row.get("expires"))
            if row.get("session") is True or expires == -1:
                expires = None
            try:
                expires = int(expires) if expires is not None else None
            except (ValueError, TypeError, OverflowError):
                continue
            rest = {}
            if row.get("httpOnly"):
                rest["HttpOnly"] = None
            if "httpOnly" not in row or row.get("httpOnlySource") == "unknown":
                rest["HttpOnlySource"] = "unknown"
            if isinstance(row.get("sameSite"), str):
                rest["SameSite"] = row["sameSite"]
            if row.get("scopeSource") in ("legacy", "unknown"):
                rest["ScopeSource"] = row["scopeSource"]
            elif not row.get("path") or "secure" not in row:
                rest["ScopeSource"] = "unknown"
            cookie = Cookie(
                0,
                name,
                value,
                None,
                False,
                host if host_only else "." + host,
                not host_only,
                domain.startswith("."),
                path,
                True,
                bool(row.get("secure")),
                expires,
                expires is None,
                None,
                None,
                rest,
            )
            if _valid_cookie(cookie) and not cookie.is_expired():
                self.jar.set_cookie(cookie)

    def has_any(self, names: tuple[str, ...]) -> bool:
        return any(cookie.name in names and not cookie.is_expired() for cookie in self.jar)

    def javascript_cookies(self) -> list[dict[str, Any]]:
        """Only recorded, script-readable cookies; HTTP compatibility guesses stay private."""
        return [
            row
            for row in self.export()
            if not row["httpOnly"]
            and row.get("httpOnlySource") != "unknown"
            and row.get("scopeSource") not in ("legacy", "unknown")
        ]

    def header(self, url: str) -> str:
        try:
            parsed = urlparse(url)
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 80 if parsed.scheme == "http" else 443)
            ):
                return ""
        except (TypeError, ValueError):
            return ""
        # Domain cookies can be sent to Google subdomains during OAuth; host-only
        # cookies remain restricted by the standard policy.
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not (
            host == "google.com" or host.endswith(".google.com")
        ):
            return ""
        request = Request(url)
        self.jar.add_cookie_header(request)
        return request.get_header("Cookie", "")

    def merge(self, headers: Any, response_url: str) -> None:
        if not _trusted_url(response_url):
            return
        source_host = urlparse(response_url).hostname
        for getter in ("get_list", "getlist", "get_all"):
            get_values = getattr(headers, getter, None)
            if callable(get_values):
                lines = get_values("set-cookie") or get_values("Set-Cookie") or []
                break
        else:
            lines = [value for key, value in headers.items() if key.lower() == "set-cookie"]
        accepted = Message()
        for line in lines:
            # CookieJar applies expiry deletions before set_ok; validate Domain
            # first so a foreign deletion cannot clear another host's cookie.
            parsed = parse_ns_headers([line])
            if not parsed or not parsed[0]:
                continue
            attrs = {name.lower(): value for name, value in parsed[0][1:]}
            name = parsed[0][0][0]
            if name.startswith(("__Secure-", "__Host-")) and "secure" not in attrs:
                continue
            if name.startswith("__Host-") and ("domain" in attrs or attrs.get("path") != "/"):
                continue
            domain = str(attrs.get("domain") or source_host).removeprefix(".").lower()
            if domain not in GOOGLE_HOSTS or not (
                source_host == domain or source_host.endswith("." + domain)
            ):
                continue
            accepted.add_header("Set-Cookie", line)
        self.jar.extract_cookies(SimpleNamespace(info=lambda: accepted), Request(response_url))

    def export(self) -> list[dict[str, Any]]:
        self.jar.clear_expired_cookies()
        result = []
        for cookie in self.jar:
            attributes = {key.lower(): value for key, value in cookie._rest.items()}
            row = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain
                if cookie.domain_initial_dot
                else cookie.domain.removeprefix("."),
                "path": cookie.path,
                "secure": cookie.secure,
                "hostOnly": not cookie.domain_specified,
                "httpOnly": "httponly" in attributes,
                "session": cookie.expires is None,
            }
            if cookie.expires is not None:
                row["expirationDate"] = cookie.expires
            if "samesite" in attributes:
                row["sameSite"] = attributes["samesite"]
            if attributes.get("scopesource") in ("legacy", "unknown"):
                row["scopeSource"] = attributes["scopesource"]
            if attributes.get("httponlysource") == "unknown":
                row["httpOnlySource"] = "unknown"
            result.append(row)
        return result
