"""Read the current Flow identity and credits through its browser Cookie jar."""

import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from ...core.account_identity import same_google_identity

FLOW_ORIGIN = "https://flow.google.com"
FLOW_URL = FLOW_ORIGIN + "/"
IDENTITY_RPC = "o30O0e"
CREDITS_RPC = "nzlxg"
_PEOPLE_REQUEST = [
    ["me"],
    [[["person.photo", "person.name", "person.email", "person.metadata"]], None, [1, 7, 10]],
]
_PAYGATE_TIERS = {
    0: "PAYGATE_TIER_UNSPECIFIED",
    1: "PAYGATE_TIER_ONE",
    2: "PAYGATE_TIER_TWO",
    3: "PAYGATE_TIER_NOT_PAID",
    4: "PAYGATE_TIER_UNSUBSCRIBED_WITH_CREDITS",
    5: "PAYGATE_TIER_ZERO",
    6: "PAYGATE_TIER_EXEMPT",
    7: "PAYGATE_TIER_GEMNOVA",
    8: "PAYGATE_TIER_TIER1P5",
}

_PUBLIC_ERRORS = {
    0: "PUBLIC_ERROR_UNSPECIFIED",
    2: "PUBLIC_ERROR_USER_QUOTA_REACHED",
    48: "PUBLIC_ERROR_WORKSPACE_ACCOUNT_QUOTA_REACHED",
    3: "PUBLIC_ERROR_USER_REQUESTS_THROTTLED",
    4: "PUBLIC_ERROR_HIGH_TRAFFIC",
    5: "PUBLIC_ERROR_MODEL_DISABLED_DUE_TO_TRAFFIC",
    7: "PUBLIC_ERROR_NON_ENGLISH_PROMPT",
    9: "PUBLIC_ERROR_GENERATION_ALREADY_IN_PROGRESS",
    1: "PUBLIC_ERROR_UNSAFE_GENERATION",
    11: "PUBLIC_ERROR_MODEL_ACCESS_DENIED",
    32: "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED",
    34: "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED_UPGRADEABLE",
    33: "PUBLIC_ERROR_AUDIO_FILTERED",
    8: "PUBLIC_ERROR_UNSAFE_IMAGE_UPLOAD",
    19: "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD",
    18: "PUBLIC_ERROR_SEXUAL_UPLOAD",
    17: "PUBLIC_ERROR_MINOR_UPLOAD",
    20: "PUBLIC_ERROR_PHOTOREAL_UPLOAD",
    22: "PUBLIC_ERROR_MINOR_HARM_UPLOAD",
    26: "PUBLIC_ERROR_UNKNOWN_IMAGE_FILE_FORMAT",
    31: "PUBLIC_ERROR_IMAGE_TOO_LARGE",
    44: "PUBLIC_ERROR_IMAGE_TOO_SMALL",
    24: "PUBLIC_ERROR_PROMINENT_PEOPLE_INPUT_IMAGE",
    25: "PUBLIC_ERROR_MINOR_INPUT_IMAGE",
    23: "PUBLIC_ERROR_PHOTOREAL_INPUT_IMAGE",
    41: "PUBLIC_ERROR_IP_INPUT_IMAGE",
    12: "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED",
    14: "PUBLIC_ERROR_VIOLENCE_FILTER",
    15: "PUBLIC_ERROR_DANGER_FILTER",
    16: "PUBLIC_ERROR_SEXUAL",
    21: "PUBLIC_ERROR_MINOR",
    47: "PUBLIC_ERROR_IMAGE_OUTPUT_IP_FILTER",
    27: "PUBLIC_ERROR_VIDEO_GENERATION_TIMED_OUT",
    28: "PUBLIC_ERROR_MODEL_OVERLOADED",
    29: "PUBLIC_ERROR_HANDLE_INVALID",
    30: "PUBLIC_ERROR_HANDLE_TAKEN",
    35: "PUBLIC_ERROR_SOMETHING_WENT_WRONG",
    36: "PUBLIC_ERROR_COLLECTION_INVALID",
    39: "PUBLIC_ERROR_COLLECTION_FULL",
    40: "PUBLIC_ERROR_COLLECTION_DEPTH_EXCEEDED",
    42: "PUBLIC_ERROR_APP_UNAVAILABLE",
    45: "PUBLIC_ERROR_USER_REGION_DISALLOWED",
    46: "PUBLIC_ERROR_USER_MINOR",
    49: "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
    50: "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC",
    43: "PUBLIC_BOTTLE_CONTENT_WARNING",
    51: "PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED",
    52: "PUBLIC_ERROR_MEDIA_GENERATION_CANNOT_BE_CANCELED",
    53: "PUBLIC_ERROR_UNSAFE_VIDEO_UPLOAD",
    54: "PUBLIC_ERROR_VIDEO_UPLOAD_TIMEOUT",
    55: "PUBLIC_ERROR_VIDEO_DURATION_TOO_LONG",
    75: "PUBLIC_ERROR_PUBLISH_TIME_LIMIT_REACHED",
    56: "PUBLIC_ERROR_APPLET_SIZE_LIMIT_EXCEEDED",
    57: "PUBLIC_ERROR_APPLET_STORAGE_QUOTA_EXCEEDED",
    58: "PUBLIC_ERROR_VIDEO_UPLOAD_FAILED",
    59: "PUBLIC_ERROR_SPEECH_EDIT",
    60: "PUBLIC_ERROR_VIDEO_EDIT",
    61: "PUBLIC_ERROR_REPUTATIONAL",
    63: "PUBLIC_ERROR_UNDERSPECIFIED_ANIMAL",
    64: "PUBLIC_ERROR_RESPONSE_TOO_LONG",
    65: "PUBLIC_ERROR_PROJECT_NOT_ALLOWLISTED",
    66: "PUBLIC_ERROR_USER_ROLE_NOT_GRANTED",
    67: "PUBLIC_ERROR_PROJECT_FLOW_ENTERPRISE_NOT_ENABLED",
    68: "PUBLIC_ERROR_VERTEX_SAFETY_FILTER_TRIGGERED",
    69: "PUBLIC_ERROR_VERTEX_UNSUPPORTED_INPUT",
    70: "PUBLIC_ERROR_VERTEX_QUOTA_EXHAUSTED",
    71: "PUBLIC_ERROR_VERTEX_QUOTA_EXHAUSTED_ADMIN",
    72: "PUBLIC_ERROR_VERTEX_SERVICE_UNAVAILABLE",
    73: "PUBLIC_ERROR_UNSAFE_SEARCH",
    74: "PUBLIC_ERROR_IAM_POLICY_DENIED",
}
_RETRYABLE_PUBLIC_ERRORS = {
    "PUBLIC_ERROR_USER_REQUESTS_THROTTLED",
    "PUBLIC_ERROR_HIGH_TRAFFIC",
    "PUBLIC_ERROR_MODEL_DISABLED_DUE_TO_TRAFFIC",
    "PUBLIC_ERROR_MODEL_OVERLOADED",
    "PUBLIC_ERROR_SOMETHING_WENT_WRONG",
    "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
    "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC",
    "PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED",
    "PUBLIC_ERROR_VERTEX_SERVICE_UNAVAILABLE",
}


class FlowRPCError(Exception):
    """A bounded diagnostic that never contains upstream response or token text."""

    def __init__(
        self,
        reason: str,
        network_step: str,
        http_status: int | None = None,
        *,
        rpc_code: int | None = None,
        public_error: str | None = None,
    ):
        self.reason = self.code = reason
        self.network_step = network_step
        self.http_status = http_status
        self.rpc_code = rpc_code if type(rpc_code) is int and 0 <= rpc_code <= 599 else None
        self.public_error = (
            public_error
            if isinstance(public_error, str) and public_error in _PUBLIC_ERRORS.values()
            else None
        )
        # Only a fully decoded error-only RPC response proves a rejected write.
        self.submission_rejected = False
        self.retryable = reason not in {
            "login_required",
            "session_invalid",
            "identity_mismatch",
        } and (
            reason in {"transient_error", "xsrf_rejected"}
            or self.public_error in _RETRYABLE_PUBLIC_ERRORS
        )
        detail = f" (code={self.rpc_code})" if self.rpc_code is not None else ""
        if self.public_error:
            detail += f" {self.public_error}"
        super().__init__(f"Flow {network_step}: {reason}{detail}")


class _XSRFRejected(Exception):
    def __init__(self, token: str):
        self.token = token


def _source_path(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"/(?:projects|project/[A-Za-z0-9_-]+)?", value
    ):
        raise FlowRPCError("account_probe_error", "flow_rpc")
    return value


def _public_error(value: Any, *, depth: int = 0) -> str | None:
    if depth > 16:
        return None
    if isinstance(value, str):
        return value if value in _PUBLIC_ERRORS.values() else None
    if isinstance(value, list):
        # google.protobuf.Any: only map its typed enum, never arbitrary integers.
        if (
            len(value) > 1
            and isinstance(value[0], str)
            and value[0].endswith("google.internal.labs.aisandbox.proto.common.v1.PublicAitkError")
        ):
            body = value[1]
            code = body[0] if isinstance(body, list) and body else None
            return _PUBLIC_ERRORS.get(code) if type(code) is int else None
        for item in value:
            found = _public_error(item, depth=depth + 1)
            if found:
                return found
    return None


def _rpc_error(code: Any, error: Any, step: str, *, executor: bool = False) -> FlowRPCError:
    public = _public_error(error)
    auth_codes = (401, 403, 16) if executor else (7, 16, 401, 403)
    reason = (
        "login_required"
        # PERMISSION_DENIED also wraps typed business rejections. Keep their
        # reason rather than invalidating a valid login on unusual activity.
        if code in (16, 401) or (public is None and code in auth_codes)
        else "transient_error"
        if public in _RETRYABLE_PUBLIC_ERRORS
        or (public is None and code in (4, 8, 10, 13, 14, 408, 429, 502, 503, 504))
        else "rpc_rejected"
    )
    return FlowRPCError(reason, step, rpc_code=code, public_error=public)


def _trusted_flow_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "flow.google.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def _check_response(response: Any, step: str) -> None:
    status = response.status
    url = str(getattr(response, "url", "") or "")
    headers = response.headers
    target = urljoin(url or FLOW_URL, headers.get("location", ""))
    if status in {401, 403} or urlsplit(url).hostname == "accounts.google.com":
        raise FlowRPCError("login_required", step, status)
    if 300 <= status < 400 and urlsplit(target).hostname == "accounts.google.com":
        raise FlowRPCError("login_required", step, status)
    if status != 200:
        raise FlowRPCError("transient_error", step, status)
    if not _trusted_flow_url(url):
        raise FlowRPCError("account_probe_error", step, status)


def _bootstrap(html: str) -> dict[str, Any]:
    marker = re.search(r"\b(?:window\.)?WIZ_global_data\s*=\s*", html)
    try:
        if marker is None:
            raise ValueError
        data, _ = json.JSONDecoder().raw_decode(html[marker.end() :])
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise FlowRPCError("account_probe_error", "flow_bootstrap") from None
    email = data.get("oPEP7c")
    if not isinstance(email, str) or not email.strip():
        raise FlowRPCError("login_required", "flow_bootstrap")
    if not _email(email):
        raise FlowRPCError("identity_mismatch", "flow_bootstrap")
    for key in ("SNlM0e", "cfb2h", "FdrFJe", "eptZe"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FlowRPCError("account_probe_error", "flow_bootstrap")
    if data["eptZe"] != "/_/AiSandboxAngularFrontend/":
        raise FlowRPCError("account_probe_error", "flow_bootstrap")
    authuser = data.get("QrtxK", "")
    if not isinstance(authuser, str) or (authuser and not authuser.isdecimal()):
        raise FlowRPCError("account_probe_error", "flow_bootstrap")
    return data


def _email(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[^\s@]+@[^\s@]+", value.strip()):
        return value.strip().casefold()
    return ""


def _rpc_payload(text: str, rpcid: str, step: str) -> Any:
    """Decode XSSI/length framing; replay only a definite, payload-free XSRF rejection."""
    remaining = text.lstrip()
    if remaining.startswith(")]}'"):
        remaining = remaining[4:]
    decoder = json.JSONDecoder()
    matches, errors, xsrf = [], [], []
    has_payload = False
    try:
        while remaining.strip():
            remaining = remaining.lstrip()
            length = re.match(r"\d+[ \t]*\r?\n", remaining)
            if length:
                remaining = remaining[length.end() :]
            records, end = decoder.raw_decode(remaining)
            remaining = remaining[end:]
            if not isinstance(records, list):
                raise ValueError
            rows = [records] if records and isinstance(records[0], str) else records
            for row in rows:
                if not isinstance(row, list) or not row:
                    continue
                if row[0] == "xsrf":
                    token = row[1] if len(row) > 1 else None
                    if (
                        not isinstance(token, str)
                        or not token
                        or len(token) > 4096
                        or any(ord(char) < 32 for char in token)
                    ):
                        raise ValueError
                    xsrf.append(token)
                elif row[0] == "er":
                    code = row[5] if len(row) > 5 else None
                    if code is None and len(row) > 6:
                        code = row[6]
                    errors.append(_rpc_error(code, row, step, executor=True))
                elif row[0] == "wrb.fr" and len(row) >= 3 and row[1] == rpcid:
                    has_payload = has_payload or row[2] is not None
                    error = row[5] if len(row) > 5 else None
                    if error:
                        code = error[0] if isinstance(error, list) and error else None
                        errors.append(_rpc_error(code, error, step))
                    elif isinstance(row[2], str):
                        matches.append(json.loads(row[2]))
                    elif row[2] is None:
                        errors.append(FlowRPCError("account_probe_error", step))
                    else:
                        raise ValueError
        if xsrf and not matches:
            auth_error = next((error for error in errors if error.reason == "login_required"), None)
            if auth_error is not None:
                raise auth_error
            if len(set(xsrf)) != 1:
                raise ValueError
            raise _XSRFRejected(xsrf[0])
        if errors:
            # A response with both a payload and an error is not a confirmed
            # rejection; callers must not replay a write in that case.
            errors[0].submission_rejected = not has_payload and len(errors) == 1
            raise errors[0]
        if len(matches) != 1:
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise FlowRPCError("account_probe_error", step) from None
    return matches[0]


def _identity(payload: Any) -> str:
    value = payload
    for index in (0, 0, 2, 9, 0, 1):
        if not isinstance(value, list) or len(value) <= index:
            raise FlowRPCError("identity_mismatch", "flow_identity")
        value = value[index]
    email = _email(value)
    if not email:
        raise FlowRPCError("identity_mismatch", "flow_identity")
    return email


async def _rpc(
    request: Any,
    bootstrap: dict[str, Any],
    rpcid: str,
    body: Any,
    step: str,
    timeout_ms: int = 20000,
    source_path: str = "/",
):
    source_path = _source_path(source_path)
    form = {
        "at": bootstrap["SNlM0e"],
        "f.req": json.dumps(
            [[[rpcid, json.dumps(body, separators=(",", ":")), None, "generic"]]],
            separators=(",", ":"),
        ),
    }
    try:
        for attempt in range(2):
            response = await request.post(
                FLOW_ORIGIN + bootstrap["eptZe"] + "data/batchexecute",
                params={
                    "rpcids": rpcid,
                    "source-path": source_path,
                    "f.sid": bootstrap["FdrFJe"],
                    "bl": bootstrap["cfb2h"],
                    "authuser": bootstrap.get("QrtxK") or "0",
                    "rt": "c",
                },
                form=dict(form),
                headers={
                    "Origin": FLOW_ORIGIN,
                    "Referer": FLOW_ORIGIN + source_path,
                    "X-Same-Domain": "1",
                },
                timeout=timeout_ms,
                max_redirects=0,
            )
            _check_response(response, step)
            try:
                return _rpc_payload(await response.text(), rpcid, step)
            except _XSRFRejected as rejected:
                if attempt or rejected.token == form["at"]:
                    raise FlowRPCError("xsrf_rejected", step) from None
                form["at"] = rejected.token
                bootstrap["SNlM0e"] = rejected.token
    except FlowRPCError:
        raise
    except Exception:
        raise FlowRPCError("transient_error", step) from None


async def read_flow_session(context: Any) -> dict[str, Any]:
    """Verify Flow's current identity and credits using the existing fixed proxy context."""
    try:
        response = await context.request.get(FLOW_URL, timeout=20000, max_redirects=0)
        _check_response(response, "flow_bootstrap")
        bootstrap = _bootstrap(await response.text())
    except FlowRPCError:
        raise
    except Exception:
        raise FlowRPCError("transient_error", "flow_bootstrap") from None
    identity = _identity(
        await _rpc(context.request, bootstrap, IDENTITY_RPC, _PEOPLE_REQUEST, "flow_identity")
    )
    if not same_google_identity(identity, _email(bootstrap["oPEP7c"])):
        raise FlowRPCError("identity_mismatch", "flow_identity")
    payload = await _rpc(context.request, bootstrap, CREDITS_RPC, [], "flow_credits")
    if not isinstance(payload, list) or not payload:
        raise FlowRPCError("credits_invalid", "flow_credits")
    credits = payload[0]
    if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
        raise FlowRPCError("credits_invalid", "flow_credits")
    tier = payload[1] if len(payload) > 1 else None
    return {
        "email": identity,
        "credits": credits,
        "user_paygate_tier": _PAYGATE_TIERS.get(tier) if type(tier) is int else None,
    }


async def flow_rpc(
    context: Any,
    rpcid: str,
    payload: Any,
    *,
    network_step: str = "flow_rpc",
    timeout_ms: int = 20000,
    source_path: str = "/",
    expected_email: str | None = None,
) -> Any:
    """Call one current Flow RPC using the browser context's Cookie/proxy jar."""
    if not isinstance(network_step, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", network_step):
        raise FlowRPCError("account_probe_error", "flow_rpc")
    if not isinstance(rpcid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", rpcid):
        raise FlowRPCError("account_probe_error", network_step)
    source_path = _source_path(source_path)
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise FlowRPCError("account_probe_error", network_step)
    cache = getattr(context, "flow_bootstrap_cache", None)
    cached = cache.get(source_path) if isinstance(cache, dict) else None
    if cached and time.monotonic() - cached[0] < 60:
        bootstrap = cached[1]
    else:
        try:
            response = await context.request.get(
                FLOW_ORIGIN + source_path, timeout=20000, max_redirects=0
            )
            _check_response(response, "flow_bootstrap")
            bootstrap = _bootstrap(await response.text())
        except FlowRPCError:
            raise
        except Exception:
            raise FlowRPCError("transient_error", "flow_bootstrap") from None
        if isinstance(cache, dict):
            cache[source_path] = (time.monotonic(), bootstrap)
    if expected_email is not None and not same_google_identity(bootstrap["oPEP7c"], expected_email):
        raise FlowRPCError("identity_mismatch", "flow_bootstrap")
    try:
        return await _rpc(
            context.request, bootstrap, rpcid, payload, network_step, timeout_ms, source_path
        )
    except FlowRPCError:
        if isinstance(cache, dict):
            cache.pop(source_path, None)
        raise
