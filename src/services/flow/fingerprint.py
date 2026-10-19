"""Remote captcha solve identity, scoped cookies, and credential-safe diagnostics.

These helpers have no configuration or request state. FlowClient owns the solve
ContextVar and decides when the remote protocol applies; proxy selection remains
in its existing transport path.
"""

import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def resolve_browser_impersonate(user_agent: str, fallback: str = "chrome150") -> str:
    """Match a UA to a supported TLS profile without a moving Chrome alias.

    Keep real profile UAs intact. For Chrome releases without a curl profile,
    use the closest older supported release, as the existing transport did.
    The pinned dependency must support our default identity; an outdated
    runtime must fail explicitly rather than silently impersonate Chrome 124.
    """
    from curl_cffi.requests.impersonate import BrowserType

    supported = {browser.value for browser in BrowserType}
    if "chrome150" not in supported:
        raise RuntimeError("Browser transport requires curl-cffi==0.16.3 with chrome150 support")

    ua = str(user_agent or "").strip().lower()
    if "android" in ua:
        return "chrome_android"
    if "edg/" in ua or " edge/" in ua:
        return "edge"
    if "safari/" in ua and "chrome/" not in ua and "chromium/" not in ua:
        return "safari_ios" if any(value in ua for value in ("iphone", "ipad", "ios")) else "safari"

    match = re.search(r"(?:chrome|chromium)/(\d+)", ua)
    if not match:
        return fallback
    major = int(match.group(1))
    exact = f"chrome{major}"
    if exact in supported:
        return exact

    candidates = []
    for profile in supported:
        target = re.fullmatch(r"chrome(\d+)([a-z]*)", profile)
        if target and int(target.group(1)) <= major:
            candidates.append((int(target.group(1)), profile))
    return max(candidates)[1] if candidates else fallback


def normalize_remote_fingerprint(
    payload: Dict[str, Any],
    *,
    project_id: str,
    origin: str,
    referer: str,
) -> Dict[str, Any]:
    """Take an allowlisted snapshot of one solve; never bind its proxy."""
    nested = payload.get("fingerprint")
    nested = nested if isinstance(nested, dict) else {}
    fingerprint = {
        "_source": "remote_browser",
        "project_id": project_id,
        "origin": origin,
        "referer": referer,
    }
    for key in (
        "user_agent",
        "accept_language",
        "sec_ch_ua",
        "sec_ch_ua_mobile",
        "sec_ch_ua_platform",
    ):
        for source in (nested, payload):
            value = source.get(key)
            if isinstance(value, str) and value.strip() and not any(c in value for c in "\r\n\0"):
                fingerprint[key] = value.strip()
                break
    for source in (nested, payload):
        cookies = source.get("session_cookies")
        if not isinstance(cookies, list):
            continue
        # Old unscoped cookie dictionaries cannot safely be attached to a host.
        valid = [
            dict(c)
            for c in cookies
            if isinstance(c, dict)
            and all(isinstance(c.get(k), str) for k in ("name", "value", "domain", "path"))
            and c["name"]
            and c["domain"]
            and c["path"].startswith("/")
            and not any(ch in c["name"] for ch in "=; \t\r\n\0")
            and not any(ch in c["value"] for ch in ";\r\n\0")
        ]
        if valid:
            fingerprint["session_cookies"] = valid
            break
    return fingerprint


def merge_cookie_header(
    existing_cookie_header: Optional[str],
    extra_cookies: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Merge cookies by name, preserving explicit account cookies."""
    cookie_items: Dict[str, str] = {}
    raw_existing = str(existing_cookie_header or "").strip()
    if raw_existing:
        for part in raw_existing.split(";"):
            item = str(part or "").strip()
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = str(key or "").strip()
            value = str(value or "").strip()
            if key:
                cookie_items[key] = value

    if isinstance(extra_cookies, dict):
        for key, value in extra_cookies.items():
            normalized_key = str(key or "").strip()
            normalized_value = str(value or "").strip()
            if normalized_key and normalized_value and normalized_key not in cookie_items:
                cookie_items[normalized_key] = normalized_value

    if not cookie_items:
        return raw_existing or None
    return "; ".join(f"{key}={value}" for key, value in cookie_items.items())


def apply_remote_fingerprint_headers(
    headers: Dict[str, str],
    url: str,
    fingerprint: Dict[str, Any],
    *,
    fallback_user_agent: str,
    now: float,
) -> None:
    """Apply one solve identity last; mutate only the supplied request headers."""
    if fingerprint.get("_source") != "remote_browser":
        fingerprint = {}
    values = {
        "User-Agent": fingerprint.get("user_agent") or fallback_user_agent,
        "Accept-Language": fingerprint.get("accept_language") or "zh-CN,zh;q=0.9",
        "sec-ch-ua": fingerprint.get("sec_ch_ua"),
        "sec-ch-ua-mobile": fingerprint.get("sec_ch_ua_mobile"),
        "sec-ch-ua-platform": fingerprint.get("sec_ch_ua_platform"),
    }
    for name, value in values.items():
        for key in list(headers):
            if key.lower() == name.lower():
                del headers[key]
        if value:
            headers[name] = value

    target = urlparse(url)
    host, path = (target.hostname or "").lower(), target.path or "/"
    cookies = {}
    for cookie in sorted(fingerprint.get("session_cookies", []), key=lambda c: -len(c["path"])):
        domain = cookie["domain"].lower().lstrip(".")
        host_only = cookie.get("hostOnly", not cookie["domain"].startswith("."))
        cookie_path = cookie["path"]
        if not domain or not (host == domain or (not host_only and host.endswith("." + domain))):
            continue
        if not (
            path == cookie_path
            or (
                path.startswith(cookie_path)
                and (cookie_path.endswith("/") or path[len(cookie_path) :].startswith("/"))
            )
        ):
            continue
        if cookie.get("secure") and target.scheme != "https":
            continue
        expires = cookie.get("expires", cookie.get("expirationDate"))
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now:
            continue
        cookies.setdefault(cookie["name"], cookie["value"])
    # Account login cookies win by name, regardless of header capitalization.
    cookie_keys = [key for key in headers if key.lower() == "cookie"]
    explicit = "; ".join(headers[key] for key in cookie_keys)
    merged = merge_cookie_header(explicit, cookies)
    if merged:
        for key in cookie_keys:
            del headers[key]
        headers["Cookie"] = merged


def redact_credentials(value: Any) -> Any:
    """Remove credential fields recursively without depending on debug masking."""
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if key.lower().replace("_", "").replace("-", "")
            in {
                "token",
                "authorization",
                "cookie",
                "setcookie",
                "sessioncookies",
                "apikey",
                "xapikey",
                "clientkey",
            }
            else redact_credentials(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_credentials(item) for item in value]
    return value


def summarize_remote_http_error(status_code: int, response_text: str) -> str:
    """Keep retry markers without propagating an upstream credential echo."""
    markers = (
        "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC",
        "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        "reCAPTCHA evaluation failed",
        "RESOURCE_EXHAUSTED",
        "INVALID_ARGUMENT",
        "PERMISSION_DENIED",
    )
    reasons = [marker for marker in markers if marker.lower() in response_text.lower()]
    return f"HTTP Error {status_code}" + (": " + ", ".join(reasons) if reasons else "")
