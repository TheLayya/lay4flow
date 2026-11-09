"""Bounded remote solver errors; never echo provider bodies or credentials."""

import errno
import socket
import ssl
import urllib.error

_ERROR_CODES = {
    "ERROR_IP_NOT_ALLOWED",
    "ERROR_KEY_DOES_NOT_EXIST",
    "ERROR_KEY_INVALID",
    "ERROR_KEY_DISABLED",
    "ERROR_ZERO_BALANCE",
    "ERROR_NO_SLOT_AVAILABLE",
    "ERROR_NO_SLOT_AVAILABLE_BLOCK",
    "ERROR_TASK_NOT_SUPPORTED",
    "ERROR_BAD_PARAMETERS",
    "ERROR_CAPTCHA_UNSOLVABLE",
    "ERROR_TIMEOUT",
    "ERROR_RATE_LIMIT",
    "ERROR_TOO_MANY_REQUESTS",
    "ERROR_INTERNAL",
    "ERROR_WORKER_UNAVAILABLE",
    "ERROR_SESSION_NOT_FOUND",
    "ERROR_SESSION_OWNER_MISMATCH",
    "INVALID_RESPONSE",
    "MISSING_TOKEN_OR_SESSION",
    "SOLVE_REJECTED",
    "TRANSPORT_ERROR",
}

_TRANSPORT_REASONS = {
    "CONNECT_TIMEOUT": "连接远程打码服务超时",
    "READ_TIMEOUT": "等待远程打码服务响应超时",
    "WRITE_TIMEOUT": "发送远程打码请求超时",
    "POOL_TIMEOUT": "等待远程打码连接超时",
    "DNS_ERROR": "远程打码服务域名解析失败",
    "TLS_CERTIFICATE_ERROR": "远程打码服务证书校验失败",
    "TLS_ERROR": "远程打码服务 TLS 连接失败",
    "CONNECTION_REFUSED": "远程打码服务拒绝连接",
    "CONNECTION_RESET": "远程打码连接被中断",
    "CONNECTION_ERROR": "无法连接远程打码服务",
    "PROTOCOL_ERROR": "远程打码服务响应协议异常",
    "INVALID_URL": "远程打码服务地址无效",
    "TIMEOUT": "远程打码请求超时",
    "UNKNOWN_TRANSPORT": "远程打码请求传输失败",
}


def transport_error_reason(error):
    """Use exception types/OS codes only, never untrusted exception descriptions."""
    try:
        import httpx
    except ImportError:
        httpx = None

    chain, pending, seen = [], [error], set()
    while pending and len(chain) < 12:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend([current.__context__, current.__cause__])
        if isinstance(current, urllib.error.URLError):
            pending.append(current.reason)
    # Underlying DNS/TLS/OS causes explain broad httpx.ConnectError wrappers.
    for current in chain:
        if isinstance(current, ssl.SSLCertVerificationError):
            return "TLS_CERTIFICATE_ERROR"
        if isinstance(current, ssl.SSLError) and not isinstance(
            current, (ssl.SSLWantReadError, ssl.SSLWantWriteError)
        ):
            return "TLS_ERROR"
        if isinstance(current, socket.gaierror):
            return "DNS_ERROR"
        if isinstance(current, ConnectionRefusedError) or (
            isinstance(current, OSError)
            and (current.errno == errno.ECONNREFUSED or getattr(current, "winerror", None) == 10061)
        ):
            return "CONNECTION_REFUSED"
        if isinstance(current, (ConnectionResetError, BrokenPipeError)) or (
            isinstance(current, OSError)
            and (current.errno == errno.ECONNRESET or getattr(current, "winerror", None) == 10054)
        ):
            return "CONNECTION_RESET"
    if httpx is not None:
        for current in chain:
            for error_type, reason in (
                (httpx.ConnectTimeout, "CONNECT_TIMEOUT"),
                (httpx.ReadTimeout, "READ_TIMEOUT"),
                (httpx.WriteTimeout, "WRITE_TIMEOUT"),
                (httpx.PoolTimeout, "POOL_TIMEOUT"),
                (httpx.InvalidURL, "INVALID_URL"),
                (httpx.ProtocolError, "PROTOCOL_ERROR"),
                (httpx.ConnectError, "CONNECTION_ERROR"),
            ):
                if isinstance(current, error_type):
                    return reason
    for current in chain:
        if isinstance(current, TimeoutError):
            return "TIMEOUT"
        if isinstance(current, ConnectionError):
            return "CONNECTION_ERROR"
    return "UNKNOWN_TRANSPORT"


class RemoteCaptchaError(RuntimeError):
    def __init__(self, code, *, http_status=None, transport_reason=None):
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "SOLVE_REJECTED"
        self.http_status = http_status if type(http_status) is int else None
        self.transport_reason = (
            transport_reason
            if self.code == "TRANSPORT_ERROR"
            and isinstance(transport_reason, str)
            and transport_reason in _TRANSPORT_REASONS
            else "UNKNOWN_TRANSPORT"
            if self.code == "TRANSPORT_ERROR"
            else None
        )
        self.reason = self.transport_reason or self.code
        self.retryable = self.code in {
            "ERROR_NO_SLOT_AVAILABLE",
            "ERROR_TIMEOUT",
            "ERROR_RATE_LIMIT",
            "ERROR_TOO_MANY_REQUESTS",
            "ERROR_INTERNAL",
            "ERROR_WORKER_UNAVAILABLE",
            "TRANSPORT_ERROR",
        } or self.http_status in {408, 429, 500, 502, 503, 504}
        if self.transport_reason in {"TLS_CERTIFICATE_ERROR", "INVALID_URL"}:
            self.retryable = False
        detail = f" (HTTP {self.http_status})" if self.http_status else ""
        if self.transport_reason:
            detail += f" [{self.transport_reason}: {_TRANSPORT_REASONS[self.transport_reason]}]"
        super().__init__(f"remote_browser: {self.code}{detail}")

    @classmethod
    def from_transport(cls, error):
        if isinstance(error, cls):
            return error
        return cls("TRANSPORT_ERROR", transport_reason=transport_error_reason(error))


def check_remote_response(payload, status):
    code = payload.get("errorCode") if isinstance(payload, dict) else None
    if status >= 300:
        raise RemoteCaptchaError(code, http_status=status)
    if not isinstance(payload, dict):
        raise RemoteCaptchaError("INVALID_RESPONSE")
    if (
        payload.get("errorId", 0) not in (None, 0, "0")
        or payload.get("success") is False
        or payload.get("ok") is False
    ):
        raise RemoteCaptchaError(code)
    return payload
