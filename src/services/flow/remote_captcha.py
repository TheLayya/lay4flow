"""Bounded remote solver errors; never echo provider bodies or credentials."""

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


class RemoteCaptchaError(RuntimeError):
    def __init__(self, code, *, http_status=None):
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "SOLVE_REJECTED"
        self.http_status = http_status if type(http_status) is int else None
        self.retryable = self.code in {
            "ERROR_NO_SLOT_AVAILABLE",
            "ERROR_TIMEOUT",
            "ERROR_RATE_LIMIT",
            "ERROR_TOO_MANY_REQUESTS",
            "ERROR_INTERNAL",
            "ERROR_WORKER_UNAVAILABLE",
            "TRANSPORT_ERROR",
        } or self.http_status in {408, 429, 500, 502, 503, 504}
        detail = f" (HTTP {self.http_status})" if self.http_status else ""
        super().__init__(f"remote_browser: {self.code}{detail}")


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
