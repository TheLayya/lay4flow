"""Adapter from the existing TokenManager validation to ProfileVerifier."""

from datetime import datetime, timezone
from typing import Any

try:  # Playwright is optional until the browser-profile feature is enabled.
    from playwright.async_api import (
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeoutError,
    )

    PLAYWRIGHT_ERRORS = (PlaywrightError,)
    PLAYWRIGHT_TIMEOUTS = (PlaywrightTimeoutError,)
except ImportError:  # pragma: no cover - exercised only in minimal API installs.
    PLAYWRIGHT_ERRORS = PLAYWRIGHT_TIMEOUTS = ()

SESSION_COOKIE = "__Secure-next-auth.session-token"
SESSION_URL = "https://labs.google/fx/api/auth/session"


def _probe_error_reason(error: BaseException, *, request_pending: bool) -> str:
    """Classify known transport failures without copying exception text into API data."""
    if isinstance(error, (TimeoutError, ConnectionError) + PLAYWRIGHT_TIMEOUTS):
        return "transient_error"
    # APIRequestContext.get uses Error for connection failures. Limit that
    # classification to our fixed HTTP requests, not Cookie or JSON errors.
    if request_pending and isinstance(error, PLAYWRIGHT_ERRORS):
        return "transient_error"
    return "account_probe_error"


def _session_cookie_from_browser(cookies: list[dict[str, Any]]) -> str | None:
    values: dict[str, str] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        path = str(cookie.get("path") or "/")
        if domain != "labs.google":
            continue
        scope = path if path.endswith("/") else path + "/"
        if path != "/fx/api/auth/session" and not "/fx/api/auth/session".startswith(scope):
            continue
        if name == SESSION_COOKIE or name.startswith(SESSION_COOKIE + "."):
            value = str(cookie.get("value") or "")
            if value:
                values[name] = value
    full = values.pop(SESSION_COOKIE, None)
    if full:
        return full if not values else None
    chunks = sorted(
        (int(name.rsplit(".", 1)[1]), value)
        for name, value in values.items()
        if name.rsplit(".", 1)[-1].isdigit()
    )
    if chunks and [index for index, _ in chunks] == list(range(len(chunks))):
        return "".join(value for _, value in chunks)
    return None


class TokenManagerAccountProbe:
    def __init__(self, token_manager: Any, token: Any, st: str = ""):
        self.token_manager = token_manager
        self.token = token
        self.st = st
        self.context = None
        self.validated_session = None
        self._session_response = None

    def set_context(
        self,
        context: Any,
        *,
        session_response: tuple[int, dict[str, Any] | None] | None = None,
    ) -> None:
        self.context = context
        self._session_response = session_response

    async def verify(self, expected_email: str, context: Any = None) -> dict[str, Any]:
        self.validated_session = None
        # Navigation and this probe share a Cookie jar. Consume its response
        # once so a post-OAuth verification always reads the renewed session.
        session_response, self._session_response = self._session_response, None
        if str(getattr(self.token, "email", "")).casefold() != expected_email.strip().casefold():
            return {
                "email": "",
                "session_valid": False,
                "credits": 0,
                "reason": "identity_mismatch",
            }
        request_pending = False
        try:
            active_context = context or self.context
            if active_context is not self.context:
                session_response = None
            st = self.st
            if active_context is not None:
                st = _session_cookie_from_browser(await active_context.cookies()) or ""
            if not st:
                return {
                    "email": "",
                    "session_valid": False,
                    "credits": 0,
                    "reason": "session_invalid",
                }
            session = None
            if active_context is not None and hasattr(active_context, "request"):
                if session_response is None:
                    request_pending = True
                    response = await active_context.request.get(SESSION_URL, timeout=15000)
                    request_pending = False
                    status = response.status
                else:
                    status, session = session_response
                if status in {401, 403}:
                    return {"reason": "session_invalid"}
                if status != 200:
                    return {"reason": "transient_error"}
                if session_response is None:
                    session = await response.json()
                st = _session_cookie_from_browser(await active_context.cookies()) or st
            if session is None:
                result = await self.token_manager._validate_refresh_session(self.token, st)
            else:
                at = str(session.get("access_token") or "").strip()
                expires = self.token_manager._as_utc(
                    self.token_manager._parse_at_expires(session.get("expires"))
                )
                email = str((session.get("user") or {}).get("email") or "")
                if not at or not expires or expires <= datetime.now(timezone.utc):
                    return {"reason": "session_invalid"}
                if email.casefold() != str(self.token.email).casefold():
                    return {"reason": "identity_mismatch"}
                url = self.token_manager.flow_client.api_base_url + "/credits"
                request_pending = True
                response = await active_context.request.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {at}",
                        "Origin": "https://flow.google.com",
                        "Referer": "https://flow.google.com/",
                    },
                    timeout=20000,
                )
                request_pending = False
                if response.status in {401, 403}:
                    return {"reason": "session_invalid"}
                if response.status != 200:
                    return {"reason": "transient_error"}
                credits_payload = await response.json()
                credits = credits_payload.get("credits", 0)
                if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
                    return {"reason": "credits_invalid"}
                result = {
                    "st": st,
                    "at": at,
                    "at_expires": expires,
                    "credits": credits,
                    "user_paygate_tier": credits_payload.get("userPaygateTier"),
                }
                self.validated_session = result
        except Exception as error:
            code = getattr(error, "code", None)
            reason = {
                "session_invalid": "session_invalid",
                "login_required": "login_required",
                "invalid_cookies": "identity_mismatch",
                "transient_error": "transient_error",
                "invalid_proxy": "invalid_proxy",
                "proxy_error": "proxy_error",
            }.get(code, _probe_error_reason(error, request_pending=request_pending))
            return {
                "email": "",
                "session_valid": False,
                "credits": 0,
                "reason": reason,
            }
        credits = result.get("credits", 0)
        if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
            return {"email": "", "session_valid": False, "credits": 0, "reason": "credits_invalid"}
        return {
            "email": str(getattr(self.token, "email", "")),
            "session_valid": bool(result.get("at")) and bool(result.get("st")),
            "credits": credits,
        }
