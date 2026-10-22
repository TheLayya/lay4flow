"""Read the current Flow identity and credits through its browser Cookie jar."""

import json
import re
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


class FlowRPCError(Exception):
    """A bounded diagnostic that never contains upstream response or token text."""

    def __init__(self, reason: str, network_step: str, http_status: int | None = None):
        self.reason = self.code = reason
        self.network_step = network_step
        self.http_status = http_status
        super().__init__(f"Flow {network_step}: {reason}")


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
    """Decode XSSI-prefixed, length-framed batchexecute records."""
    remaining = text.lstrip()
    if remaining.startswith(")]}'"):
        remaining = remaining[4:]
    decoder = json.JSONDecoder()
    matches = []
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
            for row in records:
                if not isinstance(row, list) or not row or row[0] != "wrb.fr":
                    continue
                if len(row) < 3 or row[1] != rpcid:
                    continue
                # wrb.fr field 5 is google.rpc.Status; do not copy its message.
                error = row[5] if len(row) > 5 else None
                if error:
                    code = error[0] if isinstance(error, list) and error else None
                    reason = "login_required" if code in (7, 16) else "transient_error"
                    raise FlowRPCError(reason, step)
                if not isinstance(row[2], str):
                    raise ValueError
                matches.append(json.loads(row[2]))
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


async def _rpc(request: Any, bootstrap: dict[str, Any], rpcid: str, body: Any, step: str):
    try:
        response = await request.post(
            FLOW_ORIGIN + bootstrap["eptZe"] + "data/batchexecute",
            params={
                "rpcids": rpcid,
                "source-path": "/",
                "f.sid": bootstrap["FdrFJe"],
                "bl": bootstrap["cfb2h"],
                "authuser": bootstrap.get("QrtxK") or "0",
                "rt": "c",
            },
            form={
                "at": bootstrap["SNlM0e"],
                "f.req": json.dumps(
                    [[[rpcid, json.dumps(body, separators=(",", ":")), None, "generic"]]],
                    separators=(",", ":"),
                ),
            },
            headers={"Origin": FLOW_ORIGIN, "Referer": FLOW_URL, "X-Same-Domain": "1"},
            timeout=20000,
            max_redirects=0,
        )
        _check_response(response, step)
        return _rpc_payload(await response.text(), rpcid, step)
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
) -> Any:
    """Call one current Flow RPC using the browser context's Cookie/proxy jar."""
    if not isinstance(network_step, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", network_step):
        raise FlowRPCError("account_probe_error", "flow_rpc")
    if not isinstance(rpcid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", rpcid):
        raise FlowRPCError("account_probe_error", network_step)
    try:
        response = await context.request.get(FLOW_URL, timeout=20000, max_redirects=0)
        _check_response(response, "flow_bootstrap")
        bootstrap = _bootstrap(await response.text())
    except FlowRPCError:
        raise
    except Exception:
        raise FlowRPCError("transient_error", "flow_bootstrap") from None
    return await _rpc(context.request, bootstrap, rpcid, payload, network_step)
