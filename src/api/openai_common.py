"""Public OpenAI HTTP errors and bounded media delivery helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import mimetypes
import re
import socket
import uuid
from urllib.parse import unquote, urlsplit

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request", param=None):
        self.status_code = status
        self.message = message
        self.code = code
        self.param = param
        super().__init__(message)


def safe_error(message: str) -> str:
    text = re.sub(r"https?://\S+", "[upstream URL]", str(message))
    text = re.sub(r"(?i)(bearer\s+|cookie\s*[:=]\s*)\S+", r"\1[redacted]", text)
    text = re.sub(r"[A-Za-z0-9_+/=.-]{80,}", "[redacted]", text)
    return text[:500]


def api_error_response(error: APIError) -> JSONResponse:
    status = error.status_code
    kind = "server_error" if status >= 500 else "invalid_request_error"
    if status == 401:
        kind = "authentication_error"
    elif status == 429:
        kind = "rate_limit_error"
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": safe_error(error.message),
                "type": kind,
                "param": error.param,
                "code": error.code,
            }
        },
    )


async def openai_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Keep unmatched public routes on the same error contract as routed ones."""
    if not request.url.path.startswith(
        ("/v1/", "/compat/v1/", "/providers/vids/", "/api/admin/vids/v1/")
    ):
        response = JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )
        return response
    code = {
        401: "invalid_api_key",
        404: "not_found",
        405: "method_not_allowed",
    }.get(exc.status_code, "invalid_request")
    response = api_error_response(APIError(exc.status_code, str(exc.detail), code))
    response.headers["x-request-id"] = "req_" + uuid.uuid4().hex
    for name, value in (exc.headers or {}).items():
        response.headers[name] = value
    return response


class OpenAIRoute(APIRoute):
    """Convert public OpenAI errors; leave Gemini/admin untouched."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request):
            if not request.url.path.startswith(
                ("/v1/", "/compat/v1/", "/providers/vids/", "/api/admin/vids/v1/")
            ):
                return await handler(request)
            request.state.request_id = "req_" + uuid.uuid4().hex
            try:
                response = await handler(request)
            except APIError as exc:
                response = api_error_response(exc)
            except HTTPException as exc:
                if str(exc.detail) == "Request body is too large":
                    exc = HTTPException(413, str(exc.detail))
                response = api_error_response(
                    APIError(
                        exc.status_code,
                        str(exc.detail),
                        "invalid_api_key" if exc.status_code == 401 else "invalid_request",
                    )
                )
                for name, value in (exc.headers or {}).items():
                    response.headers[name] = value
            except RequestValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {}
                param = ".".join(str(x) for x in first.get("loc", ())[1:]) or None
                response = api_error_response(
                    APIError(
                        400,
                        "Invalid request field" + (f": {param}" if param else ""),
                        "invalid_request",
                        param,
                    )
                )
            except Exception as exc:
                logger.error(
                    "Unhandled public API error: request_id=%s route=%s exception_type=%s",
                    request.state.request_id,
                    self.path,
                    type(exc).__name__,
                )
                response = api_error_response(
                    APIError(
                        500,
                        "Internal server error",
                        "internal_error",
                    )
                )
            response.headers["x-request-id"] = request.state.request_id
            return response

        return wrapped


def parse_json_object_form(value, name="generationConfig") -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise APIError(400, f"{name} must be a JSON object", param=name) from None
        if isinstance(value, dict):
            return value
    raise APIError(400, f"{name} must be an object", param=name)


async def read_json_body(request: Request, limit=1024 * 1024) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise APIError(413, "Request body is too large", "request_too_large")
        data.extend(chunk)
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError):
        raise APIError(400, "Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise APIError(400, "Request body must be a JSON object")
    return body


def limit_request_body(request: Request, limit: int):
    """Bound multipart file spooling as bytes arrive, including chunked uploads."""
    length = request.headers.get("content-length")
    if length and (not length.isdecimal() or int(length) > limit):
        raise APIError(413, "Request body is too large", "request_too_large")
    receive = request._receive
    total = 0

    async def bounded_receive():
        nonlocal total
        message = await receive()
        total += len(message.get("body", b""))
        if total > limit:
            # Starlette closes already-spooled files when catching this exception.
            raise MultiPartException("Request body is too large")
        return message

    request._receive = bounded_receive


def generated_cache_path(uri: str):
    """Only resolve server-created cache basenames, never a caller's arbitrary path."""
    from . import routes

    parsed = urlsplit(uri)
    if not parsed.path.startswith("/tmp/"):
        return None
    name = unquote(parsed.path[5:])
    if not re.fullmatch(r"[a-f0-9]{32}(?:_[A-Za-z0-9]+)?\.[a-zA-Z0-9]+", name):
        raise APIError(502, "Invalid generated asset path", "invalid_asset")
    cache = routes._ensure_generation_handler().file_cache
    root = cache.cache_dir.resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise APIError(502, "Invalid generated asset path", "invalid_asset")
    return path


async def load_generated_asset(uri: str, media_type: str, *, allow_local=True) -> tuple[bytes, str]:
    """Read a generated asset with bounded memory, verified TLS and public destinations."""
    limit = 32 * 1024 * 1024 if media_type == "image" else 100 * 1024 * 1024
    if uri.startswith("data:"):
        header, separator, encoded = uri.partition(",")
        if (
            not separator
            or not header.endswith(";base64")
            or not header.startswith(f"data:{media_type}/")
        ):
            raise APIError(502, "Invalid generated media", "invalid_asset")
        if len(encoded) > (limit + 2) // 3 * 4:
            raise APIError(502, "Generated media is too large", "asset_too_large")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise APIError(502, "Invalid generated media encoding", "invalid_asset") from None
        if not data or len(data) > limit:
            raise APIError(502, "Generated media is empty or too large", "invalid_asset")
        return data, header[5:-7]
    local = generated_cache_path(uri) if allow_local else None
    if local is not None:
        if not local.is_file():
            raise APIError(410, "Generated asset has expired", "asset_expired")
        if local.stat().st_size > limit:
            raise APIError(502, "Generated media is too large", "asset_too_large")
        data = await asyncio.to_thread(local.read_bytes)
        if not data or len(data) > limit:
            raise APIError(502, "Generated media is empty or too large", "invalid_asset")
        return data, mimetypes.guess_type(local.name)[0] or f"{media_type}/mp4"
    try:
        parsed = urlsplit(uri)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise APIError(502, "Invalid generated asset URL", "invalid_asset") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise APIError(502, "Invalid generated asset URL", "invalid_asset")
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, port, type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise APIError(502, "Asset points to a private destination", "invalid_asset")
        address = addresses[0][4][0]
        address = f"[{address}]" if ":" in address else address
        # These unauthenticated public asset downloads never carry account headers.
        # Pin checked DNS, and do not let redirects reach an unchecked destination.
        async with AsyncSession(
            trust_env=False, curl_options={CurlOpt.RESOLVE: [f"{parsed.hostname}:{port}:{address}"]}
        ) as session:
            response = await session.get(
                uri, verify=True, allow_redirects=False, stream=True, timeout=90
            )
            try:
                if response.status_code != 200:
                    raise APIError(502, "Unable to fetch generated asset", "asset_download_failed")
                mime = (response.headers.get("content-type") or "").split(";", 1)[0]
                if (
                    mime
                    and mime != "application/octet-stream"
                    and not mime.startswith(media_type + "/")
                ):
                    raise APIError(502, "Unexpected generated asset type", "invalid_asset")
                result = bytearray()
                async for chunk in response.aiter_content(1024 * 1024):
                    if len(result) + len(chunk) > limit:
                        raise APIError(502, "Generated asset is too large", "asset_too_large")
                    result.extend(chunk)
            finally:
                await response.aclose()
        if not result:
            raise APIError(502, "Generated asset is empty", "invalid_asset")
        return bytes(result), mime or mimetypes.guess_type(parsed.path)[
            0
        ] or "application/octet-stream"
    except APIError:
        raise
    except Exception:
        raise APIError(502, "Unable to fetch generated asset", "asset_download_failed") from None
