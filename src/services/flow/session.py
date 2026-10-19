"""Extract session rotation only from the authenticated Labs session response."""

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urlparse

SESSION_COOKIE = "__Secure-next-auth.session-token"
SESSION_PATH = "/fx/api/auth/session"


def _is_session_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "labs.google"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == SESSION_PATH
            and not parsed.query
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False


def _set_cookie_lines(headers: Any) -> list[str]:
    for name in ("get_list", "getlist", "get_all"):
        getter = getattr(headers, name, None)
        if callable(getter):
            return getter("set-cookie") or getter("Set-Cookie") or []
    return [value for key, value in headers.items() if key.lower() == "set-cookie"]


def _session_cookie(headers: Any, *, default_path: str = "/fx/api/auth") -> str | None:
    values = {}
    for line in _set_cookie_lines(headers):
        cookies = SimpleCookie()
        try:
            cookies.load(line)
        except (CookieError, TypeError):
            continue
        for name, cookie in cookies.items():
            if not re.fullmatch(re.escape(SESSION_COOKIE) + r"(?:\.(?:0|[1-9]\d*))?", name):
                continue
            if not cookie["secure"]:
                continue
            if cookie["domain"].removeprefix(".").lower() not in ("", "labs.google"):
                continue
            path = cookie["path"] or default_path
            prefix = path if path.endswith("/") else path + "/"
            if not (path == SESSION_PATH or SESSION_PATH.startswith(prefix)):
                continue
            live = bool(cookie.value)
            try:
                if cookie["max-age"]:
                    live = live and int(cookie["max-age"]) > 0
                elif cookie["expires"]:
                    expires = parsedate_to_datetime(cookie["expires"])
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    live = live and expires > datetime.now(timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
            if live:
                values[name] = cookie.value
            else:
                values.pop(name, None)

    full = values.pop(SESSION_COOKIE, None)
    if full:
        return full if not values else None
    chunks = sorted((int(name.rsplit(".", 1)[1]), value) for name, value in values.items())
    if chunks and [index for index, _ in chunks] == list(range(len(chunks))):
        return "".join(value for _, value in chunks)
    return None


def with_session_token(
    payload: Any,
    *,
    method: str,
    request_url: str,
    response_url: str,
    status_code: int,
    headers: Any,
) -> Any:
    """Preserve JSON fields and add a private token only from trusted Set-Cookie."""
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    result.pop("_session_token", None)
    if (
        method.upper() == "GET"
        and status_code == 200
        and _is_session_url(request_url)
        and _is_session_url(response_url)
    ):
        token = _session_cookie(headers)
        if token:
            result["_session_token"] = token
    return result
