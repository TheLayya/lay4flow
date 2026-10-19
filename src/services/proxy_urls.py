"""Canonical proxy URL parsing shared by request and protocol services."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote, urlsplit

_SUPPORTED_SCHEMES = {"http", "https", "socks5", "socks5h"}
_MAX_PORT = 65535


def _validate_host(host: Optional[str]) -> str:
    if not host or any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in "/\\?#@[]" for ch in host
    ):
        raise ValueError("invalid proxy URL")
    return host


def _validate_url(raw: str, scheme: str) -> str:
    try:
        parsed = urlsplit(raw)
        _validate_host(parsed.hostname)
        port = parsed.port
    except (ValueError, UnicodeError):
        raise ValueError("invalid proxy URL") from None
    if parsed.scheme.lower() != scheme or port is None or not 1 <= port <= _MAX_PORT:
        raise ValueError("invalid proxy URL")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid proxy URL")
    return raw


def _legacy_host_parts(value: str, scheme: str) -> str:
    """Normalize ``host:port`` or ``host:port:user:password``."""
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError("invalid proxy URL")
        host = value[1:end]
        tail = value[end + 1 :]
        if not tail.startswith(":"):
            raise ValueError("invalid proxy URL")
        pieces = [host, *tail[1:].split(":", 2)]
    else:
        pieces = value.split(":", 3)
    if len(pieces) not in (2, 4):
        raise ValueError("invalid proxy URL")
    host, port = pieces[:2]
    _validate_host(host)
    if not port.isdigit() or not 1 <= int(port) <= _MAX_PORT:
        raise ValueError("invalid proxy URL")
    prefix = scheme if scheme.endswith("://") else f"{scheme}://"
    rendered_host = f"[{host}]" if ":" in host else host
    if len(pieces) == 2:
        return _validate_url(f"{prefix}{rendered_host}:{port}", scheme)
    username, password = pieces[2:]
    if not username or not password:
        raise ValueError("invalid proxy URL")
    return _validate_url(
        f"{prefix}{quote(username, safe='')}:{quote(password, safe='')}@{rendered_host}:{port}",
        scheme,
    )


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """Normalize historical proxy spellings and reject malformed values."""
    if proxy_url is None:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in proxy_url):
        raise ValueError("invalid proxy URL")
    raw = proxy_url.strip()
    if not raw:
        return None

    if raw.lower().startswith("st5 "):
        raw = f"socks5://{raw[4:].strip()}"

    scheme, separator, rest = raw.partition("://")
    if separator:
        scheme = scheme.lower()
        if scheme not in _SUPPORTED_SCHEMES:
            raise ValueError("invalid proxy URL")
    else:
        scheme, rest = "http", raw
    try:
        return _legacy_host_parts(rest, scheme)
    except ValueError:
        return _validate_url(f"{scheme}://{rest}", scheme)
