"""Protocol login for labs.google NextAuth with exported Google cookies."""

import json
import re
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from curl_cffi.requests import AsyncSession

from ..core.logger import debug_logger
from .browser.cloak_proxy import transport_proxy
from .flow.google_cookies import GoogleCookieJar
from .flow.session import _session_cookie
from .proxy_urls import normalize_proxy_url

LABS_BASE = "https://labs.google/fx"
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
IMPERSONATE = "chrome124"
GOOGLE_COOKIE_NAMES = ("SID", "HSID", "SSID", "APISID", "SAPISID", "OSID", "__Secure-OSID")


def _parse_google_cookies(raw: Any) -> Dict[str, str]:
    """Legacy name/value compatibility view; login itself uses the scoped jar."""
    return {item["name"]: item["value"] for item in GoogleCookieJar(raw).export()}


def _build_cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if name and value)


def _get_set_cookies(headers: Any) -> List[str]:
    if hasattr(headers, "getlist"):
        return headers.getlist("set-cookie") or headers.getlist("Set-Cookie") or []
    if hasattr(headers, "get_list"):
        return headers.get_list("set-cookie") or headers.get_list("Set-Cookie") or []
    value = headers.get("set-cookie") or headers.get("Set-Cookie")
    return [value] if value else []


def _merge_cookies(cookies: Dict[str, str], headers: Any) -> None:
    for line in _get_set_cookies(headers):
        first = str(line).split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        if name.strip():
            cookies[name.strip()] = value.strip()


def _login_required(url: str, body: str = "") -> bool:
    path = urlparse(url).path.lower()
    if re.search(r"/signin/(?:v\d+/)?(?:rejected|identifier|challenge)(?:/|$)", path):
        return True
    if "/servicelogin" in path or "signin/rejected" in body.lower():
        return True
    return bool(
        re.search(
            r'<input\b[^>]*(?:type\s*=\s*["\']password["\']|id\s*=\s*["\']identifierId["\'])',
            body,
            re.IGNORECASE,
        )
    )


def _failure(error: str, error_code: str = "transient_error") -> Dict[str, Any]:
    return {"success": False, "error": error, "error_code": error_code}


def _extract_session_token(headers: Any, response_url: str) -> Optional[str]:
    # Share the session endpoint's chunk, expiry, deletion and scope checks.
    default_path = urlparse(response_url).path.rsplit("/", 1)[0] or "/"
    return _session_cookie(headers, default_path=default_path)


def _trusted_url(url: str, *, labs_only: bool = False) -> bool:
    try:
        target = urlparse(url)
        host = target.hostname or ""
        return (
            target.scheme == "https"
            and target.port in (None, 443)
            and target.username is None
            and target.password is None
            and (
                host == "labs.google"
                if labs_only
                else host == "labs.google" or host == "google.com" or host.endswith(".google.com")
            )
        )
    except (TypeError, ValueError):
        return False


def _extract_redirect_from_html(text: str) -> Optional[str]:
    body = text or ""
    match = re.search(
        r'content\s*=\s*["\']?\d+\s*;\s*url\s*=\s*([^"\'>\s]+)',
        body,
        re.IGNORECASE,
    )
    if match:
        return unescape(match.group(1))

    match = re.search(
        r'location(?:\.(?:href|replace))?\s*(?:\(|=)\s*["\']([^"\']+)',
        body,
        re.IGNORECASE,
    )
    if match:
        return unescape(match.group(1))

    # A form is not a GET redirect: its method and hidden fields matter.
    if re.search(r"<form\b", body, re.IGNORECASE):
        return None

    match = re.search(r'(https://labs\.google/fx/api/auth/callback/google[^"\'<>\s]*)', body)
    if match:
        return unescape(match.group(1))

    match = re.search(r'[&?]continue=([^"\'<>\s&]+)', body)
    if match:
        return unquote(unescape(match.group(1)))

    return None


def _normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    try:
        return normalize_proxy_url(proxy_url)
    except ValueError:
        return None


def _append_login_hint(target_url: str, email: Optional[str]) -> str:
    email = (email or "").strip()
    if not email:
        return target_url
    parsed = urlparse(target_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["login_hint"] = email
    return urlunparse(parsed._replace(query=urlencode(query)))


class ProtocolLogin:
    """Login to labs.google/fx through NextAuth Google OAuth using Google cookies."""

    async def login(
        self,
        google_cookies_raw: str,
        proxy: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        google_cookies = GoogleCookieJar(google_cookies_raw)
        if not google_cookies.has_any(GOOGLE_COOKIE_NAMES):
            return _failure(
                "未找到有效的 Google cookie（需要 SID/HSID/SSID/APISID/SAPISID 中至少一个）",
                "invalid_cookies",
            )

        session_kwargs: Dict[str, Any] = {
            "impersonate": IMPERSONATE,
            "trust_env": False,
            "timeout": 30,
        }
        try:
            proxy_url = normalize_proxy_url(proxy)
        except ValueError:
            return _failure("代理地址格式错误，未发起登录请求", "invalid_proxy")
        if proxy_url:
            session_kwargs["proxy"] = proxy_url

        async with (
            transport_proxy(proxy_url) as network_proxy,
            AsyncSession(
                **{**session_kwargs, **({"proxy": network_proxy} if network_proxy else {})}
            ) as session,
        ):

            async def request(method, url, **kwargs):
                if not _trusted_url(url):
                    raise ValueError("Untrusted OAuth URL")
                # curl_cffi remembers every response Cookie independently of
                # explicit headers. Keep the transport jar empty on every hop.
                session.cookies.clear()
                return await getattr(session, method)(url, allow_redirects=False, **kwargs)

            try:
                csrf_resp = await request("get", f"{LABS_BASE}/api/auth/csrf")
                if csrf_resp.status_code != 200:
                    return _failure(f"CSRF 失败: HTTP {csrf_resp.status_code}")
                csrf_token = (csrf_resp.json() or {}).get("csrfToken")
                if not csrf_token:
                    return _failure("CSRF 响应缺少 csrfToken")

                labs_cookies: Dict[str, str] = {}
                _merge_cookies(labs_cookies, csrf_resp.headers)

                signin_resp = await request(
                    "post",
                    f"{LABS_BASE}/api/auth/signin/google",
                    data={
                        "redirect": "false",
                        "csrfToken": csrf_token,
                        "callbackUrl": LABS_BASE,
                        "json": "true",
                    },
                    headers={
                        "Referer": LABS_BASE,
                        "Origin": "https://labs.google",
                        "Cookie": _build_cookie_header(labs_cookies),
                    },
                )
                if signin_resp.status_code != 200:
                    return _failure(f"Signin 失败: HTTP {signin_resp.status_code}")

                _merge_cookies(labs_cookies, signin_resp.headers)
                signin_data = signin_resp.json() or {}
                redirect_url = signin_data.get("redirect") or signin_data.get("url")
                if not redirect_url:
                    return _failure("Signin 响应缺少重定向 URL")
                redirect_url = _append_login_hint(redirect_url, email)

                callback_url = ""
                current_url = redirect_url

                for attempt in range(10):
                    target = urlparse(current_url)
                    host = target.hostname or ""
                    # A help page is a terminal sign-in rejection, not an OAuth
                    # continuation. Never parse its search forms as redirects.
                    if host == "support.google.com" or target.path.startswith("/accounts/answer/"):
                        return _failure("Google 要求在浏览器中重新授权登录", "login_required")
                    if not _trusted_url(current_url) or (
                        host != "google.com" and not host.endswith(".google.com")
                    ):
                        return _failure("Google OAuth 返回了非 Google 跳转地址")
                    if _login_required(current_url):
                        return _failure("Google 要求重新登录或完成登录验证", "login_required")
                    oauth_resp = await request(
                        "get",
                        current_url,
                        headers={
                            "Cookie": google_cookies.header(current_url),
                            "Referer": "https://labs.google/"
                            if attempt == 0
                            else "https://accounts.google.com/",
                        },
                    )
                    google_cookies.merge(oauth_resp.headers, current_url)
                    if oauth_resp.status_code == 401:
                        return _failure("Google 登录凭据已失效（HTTP 401）", "login_required")
                    if oauth_resp.status_code not in (200, 301, 302, 303, 307, 308):
                        return _failure(f"Google OAuth 暂时失败: HTTP {oauth_resp.status_code}")
                    location = (oauth_resp.headers.get("location") or "").strip()
                    if location:
                        location = urljoin(current_url, location)
                        if (
                            urlparse(location).hostname == "labs.google"
                            and urlparse(location).path == "/fx/api/auth/callback/google"
                        ):
                            callback_url = location
                            break
                        current_url = location
                        continue

                    body = oauth_resp.text or ""
                    if _login_required(current_url, body):
                        return _failure("Google 要求重新登录或完成登录验证", "login_required")

                    if oauth_resp.status_code == 200:
                        html_redirect = _extract_redirect_from_html(body)
                        if html_redirect:
                            html_redirect = urljoin(current_url, html_redirect)
                            if (
                                urlparse(html_redirect).hostname == "labs.google"
                                and urlparse(html_redirect).path == "/fx/api/auth/callback/google"
                            ):
                                callback_url = html_redirect
                                break
                            current_url = html_redirect
                            continue

                    return _failure(
                        f"Google OAuth 未返回重定向（HTTP {oauth_resp.status_code}）",
                        "protocol_incomplete"
                        if oauth_resp.status_code == 200
                        else "transient_error",
                    )

                if not callback_url:
                    return _failure("Google OAuth 流程中未获得 callback URL", "protocol_incomplete")
                if not _trusted_url(callback_url, labs_only=True):
                    return _failure("Labs 登录回调地址无效")
                callback_query = dict(
                    parse_qsl(urlparse(callback_url).query, keep_blank_values=True)
                )
                oauth_error = callback_query.get("error")
                if oauth_error in {"login_required", "interaction_required", "consent_required"}:
                    return _failure("Google 要求重新登录或完成登录验证", "login_required")
                if "error" in callback_query:
                    return _failure("Google OAuth 返回授权错误，未执行回调")

                callback_resp = await request(
                    "get",
                    callback_url,
                    headers={
                        "Cookie": _build_cookie_header(labs_cookies),
                        "Referer": "https://accounts.google.com/",
                    },
                )

                if callback_resp.status_code not in (200, 301, 302, 303, 307, 308):
                    return _failure(f"Labs 登录回调失败: HTTP {callback_resp.status_code}")
                session_token = _extract_session_token(callback_resp.headers, callback_url)
                _merge_cookies(labs_cookies, callback_resp.headers)

                for _ in range(5):
                    if session_token:
                        break
                    location = (callback_resp.headers.get("location") or "").strip()
                    if not location or callback_resp.status_code not in (301, 302, 303, 307, 308):
                        break
                    callback_url = urljoin(callback_url, location)
                    if _login_required(callback_url):
                        return _failure("Google 要求重新登录或完成登录验证", "login_required")
                    target = urlparse(callback_url)
                    if not _trusted_url(callback_url, labs_only=True):
                        return _failure("Labs 登录回调返回了非 Labs 跳转地址")
                    if target.path.startswith("/fx/api/auth/") or "error" in dict(
                        parse_qsl(target.query, keep_blank_values=True)
                    ):
                        return _failure("Labs 回调未签发 ST，已停止后续授权跳转")
                    callback_resp = await request(
                        "get",
                        callback_url,
                        headers={"Cookie": _build_cookie_header(labs_cookies)},
                    )
                    if callback_resp.status_code not in (200, 301, 302, 303, 307, 308):
                        return _failure(f"Labs 登录回调失败: HTTP {callback_resp.status_code}")
                    _merge_cookies(labs_cookies, callback_resp.headers)
                    session_token = _extract_session_token(callback_resp.headers, callback_url)

                if not session_token:
                    if _login_required(callback_url, callback_resp.text or ""):
                        return _failure("Google 要求重新登录或完成登录验证", "login_required")
                    return _failure(f"未获取到 session token（HTTP {callback_resp.status_code}）")
                return {
                    "success": True,
                    "session_token": session_token,
                    "google_cookies": json.dumps(google_cookies.export(), ensure_ascii=False),
                }
            except Exception as exc:
                debug_logger.log_error(f"[PROTOCOL_LOGIN] 协议登录异常: {type(exc).__name__}")
                error_text = str(exc).lower()
                error_type = type(exc).__name__.lower()
                if proxy_url and (
                    "proxy" in error_type or "proxy" in error_text or "curl: (7)" in error_text
                ):
                    return _failure("代理连接失败，请检查代理地址和代理可用性", "proxy_error")
                return _failure(f"协议登录请求失败: {type(exc).__name__}")


protocol_loginer = ProtocolLogin()
