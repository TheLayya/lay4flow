"""Pure retry classification and budgets; execution and callbacks stay in FlowClient."""

import re
from collections.abc import Callable
from typing import Optional, Union

YESCAPTCHA_SLOT_BACKOFF_SECONDS = (3, 5, 8, 13, 20)


def should_fallback_to_urllib(error_message: str) -> bool:
    """判断是否应从 curl_cffi 回退到 urllib。"""
    error_lower = (error_message or "").lower()
    return any(
        keyword in error_lower
        for keyword in [
            "curl: (6)",
            "curl: (7)",
            "curl: (28)",
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "connection timed out",
            "could not connect",
            "failed to connect",
            "ssl connect error",
            "tls connect error",
            "network is unreachable",
        ]
    )


def is_timeout_error(error: Exception) -> bool:
    """判断是否为网络超时，便于快速失败重试。"""
    error_lower = str(error).lower()
    return any(
        keyword in error_lower
        for keyword in [
            "timed out",
            "timeout",
            "curl: (28)",
            "connection timed out",
            "operation timed out",
        ]
    )


def is_proxy_connection_error(error: Exception) -> bool:
    """识别本地/上游代理不可用导致的连接失败。"""
    error_lower = str(error).lower()
    if any(cls.__name__.casefold() == "proxyerror" for cls in type(error).__mro__):
        return True
    return any(
        keyword in error_lower
        for keyword in [
            "failed to connect to 127.0.0.1 port",
            "failed to connect to localhost port",
            "proxyerror",
            "proxy error",
            "failed to connect to proxy",
            "couldn't connect to server",
            "curl: (7)",
        ]
    )


def proxy_connection_code(error: Exception) -> int | None:
    """Return curl's non-secret proxy handshake code when available."""
    value = getattr(error, "code", None)
    if isinstance(value, int):
        return value
    match = re.search(r"curl:\s*\((\d+)\)", str(error), re.I)
    return int(match.group(1)) if match else None


def is_proxy_handshake_error(error: Exception) -> bool:
    return is_proxy_connection_error(error) and proxy_connection_code(error) in {5, 7, 97}


def is_retryable_network_error(error_str: str) -> bool:
    """识别可重试的 TLS/连接类网络错误。"""
    error_lower = (error_str or "").lower()
    if "curl: (97)" in error_lower:
        return True
    return any(
        keyword in error_lower
        for keyword in [
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "ssl_error_syscall",
            "tls connect error",
            "ssl connect error",
            "connection reset",
            "connection aborted",
            "connection was reset",
            "connection timed out",
            "curl: (28)",
            "timed out",
            "timeout",
            "unexpected eof",
            "empty reply from server",
            "recv failure",
            "send failure",
            "connection refused",
            "network is unreachable",
            "remote host closed connection",
        ]
    )


def get_retry_reason(
    error_str: str, *, network_classifier: Callable[[str], bool] = is_retryable_network_error
) -> Optional[str]:
    """判断是否需要重试，返回日志提示内容"""
    error_lower = error_str.lower()
    if "error_no_slot_available_block" in error_lower:
        return "打码服务资源阻塞"
    if "error_no_slot_available" in error_lower:
        return "打码服务资源不足"
    if "403" in error_lower:
        return "403错误"
    if "429" in error_lower or "too many requests" in error_lower:
        return "429限流"
    if network_classifier(error_str):
        return "网络/TLS错误"
    if "recaptcha evaluation failed" in error_lower:
        return "reCAPTCHA 验证失败"
    if "recaptcha" in error_lower:
        return "reCAPTCHA 错误"
    if any(
        keyword in error_lower
        for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
            '"reason":"internal"',
            "server error",
            "upstream error",
        ]
    ):
        return "500/内部错误"
    return None


def get_retry_delay_seconds(
    error_str: str,
    retry_attempt: int,
    *,
    slot_backoff_seconds: tuple[int, ...] = YESCAPTCHA_SLOT_BACKOFF_SECONDS,
) -> int:
    error_lower = str(error_str or "").lower()
    if "error_no_slot_available_block" in error_lower:
        return 20
    if "error_no_slot_available" in error_lower:
        index = max(0, min(retry_attempt, len(slot_backoff_seconds) - 1))
        return slot_backoff_seconds[index]
    if "recaptcha evaluation failed" in error_lower:
        return 2
    if "recaptcha" in error_lower:
        return 2
    return 1


def is_recaptcha_evaluation_error(error: Optional[Union[Exception, str]]) -> bool:
    """Identify an explicit captcha evaluation rejection for diagnostics."""
    if error is None:
        return False
    error_str = str(error)
    return (
        "recaptcha evaluation failed" in error_str.lower()
        or "recaptcha \u9a8c\u8bc1\u5931\u8d25" in error_str
    )


def generation_retry_budget(
    base_max_retries: int, *, captcha_rejected: bool = False, captcha_retries: int = 6
) -> int:
    """Use the configured total attempts, with legacy keyword compatibility."""
    effective_max_retries = max(1, int(base_max_retries or 1))
    # Settings owns the total attempts, including the first request. Captcha
    # failures use that same bound rather than a hidden higher budget.
    return effective_max_retries
