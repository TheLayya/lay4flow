"""Admin captcha diagnostics helpers with explicit transport and configuration inputs.

No service globals or request state are stored here. The admin facade retains its
legacy call points while routers and diagnostics consume narrow callbacks.
"""

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 24:
        return token
    return f"{token[:18]}...{token[-8:]}"


def normalize_http_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise RuntimeError("远程打码服务地址未配置")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("远程打码服务地址格式错误，必须是 http(s)://host[:port]")
    return normalized


def get_remote_browser_client_config(*, get_config, normalize_url) -> tuple[str, str, int]:
    base_url = normalize_url(get_config().remote_browser_base_url)
    api_key = (get_config().remote_browser_api_key or "").strip()
    if not api_key:
        raise RuntimeError("远程打码服务 API Key 未配置")
    timeout = max(5, int(get_config().remote_browser_timeout or 60))
    return (base_url, api_key, timeout)


def build_remote_browser_http_timeout(read_timeout: float, *, httpx_module) -> Any:
    read_value = max(3.0, float(read_timeout))
    write_value = min(10.0, max(3.0, read_value))
    if httpx_module is None:
        return read_value
    return httpx_module.Timeout(connect=2.5, read=read_value, write=write_value, pool=2.5)


def parse_json_response_text(text: str) -> Optional[Any]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


async def stdlib_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
    *,
    parse_json,
) -> tuple[int, Optional[Any], str]:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_data: Optional[bytes] = None
    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_data = json.dumps(payload).encode("utf-8")

    def do_request() -> tuple[int, str]:
        request = urllib.request.Request(
            url=url, data=request_data, headers=req_headers, method=request_method
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return (status_code, body.decode(charset, errors="replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            charset = exc.headers.get_content_charset() if exc.headers else None
            return (
                int(getattr(exc, "code", 0) or 0),
                body.decode(charset or "utf-8", errors="replace"),
            )

    try:
        status_code, text = await asyncio.to_thread(do_request)
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e
    return (status_code, parse_json(text), text)


async def sync_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
    *,
    get_httpx,
    build_timeout,
    stdlib_request,
    parse_json,
) -> tuple[int, Optional[Any], str]:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_kwargs: Dict[str, Any] = {"headers": req_headers, "timeout": build_timeout(timeout)}
    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_kwargs["json"] = payload
    if get_httpx() is None:
        return await stdlib_request(
            method=method, url=url, headers=req_headers, payload=payload, timeout=timeout
        )
    try:
        async with get_httpx().AsyncClient(follow_redirects=False, trust_env=False) as session:
            response = await session.request(method=request_method, url=url, **request_kwargs)
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e
    status_code = int(getattr(response, "status_code", 0) or 0)
    text = response.text or ""
    parsed = parse_json(text)
    return (status_code, parsed, text)


async def score_test_with_remote_browser_service(
    website_url: str,
    website_key: str,
    verify_url: str,
    action: str,
    enterprise: bool = False,
    *,
    get_client_config,
    request_json,
) -> Dict[str, Any]:
    """调用远程有头打码服务执行页面内打码+分数校验。"""
    base_url, api_key, timeout = get_client_config()
    endpoint = f"{base_url}/api/v1/custom-score"
    request_payload = {
        "website_url": website_url,
        "website_key": website_key,
        "verify_url": verify_url,
        "action": action,
        "enterprise": enterprise,
    }
    status_code, response_payload, response_text = await request_json(
        method="POST",
        url=endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        payload=request_payload,
        timeout=timeout,
    )
    if status_code >= 300:
        raise RuntimeError(f"Remote captcha service request failed (HTTP {status_code})")
    if not isinstance(response_payload, dict):
        raise RuntimeError("远程打码服务返回格式错误")
    return response_payload
