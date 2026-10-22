"""Local Vids account state and cached, read-only upstream quota observations."""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ...services.vids.bindings import read_bindings
from ...services.vids.client import VidsProtocolError, VidsTransportError
from ...services.vids.deletion_markers import is_deleted
from ...services.vids.dispatcher import VidsDispatchError
from ...services.vids.gateway import GatewayError

KINDS = ("image", "video")
QUOTA_REFRESH_SECONDS = 60


def empty_quota():
    return {
        kind: {
            "total": None,
            "available": None,
            "consumed": None,
            "queried_at": None,
            "error": None,
            "stale": False,
        }
        for kind in KINDS
    }


def create_vids_admin_router(
    dispatcher, authenticate, *, get_settings=None, runtime=None, repair_session=None
):
    router = APIRouter(dependencies=[Depends(authenticate)], tags=["Vids console"])
    # ponytail: process-local quota cache; restart explicitly returns "not queried".
    quota_cache = {}
    quota_attempts = {}
    # ponytail: serialize this small pool's refreshes; use account locks if needed.
    refresh_lock = asyncio.Lock()

    def observed_quota(token_id):
        now = datetime.now(timezone.utc)
        return {
            kind: {
                **value,
                "stale": value["stale"]
                or value["queried_at"] is not None
                and (now - datetime.fromisoformat(value["queried_at"])).total_seconds()
                >= QUOTA_REFRESH_SECONDS,
            }
            for kind, value in quota_cache.get(token_id, empty_quota()).items()
        }

    def configured_ids():
        settings = get_settings() if get_settings is not None else {}
        rows = settings.get("accounts", []) if isinstance(settings, dict) else []
        if not isinstance(rows, list):
            return set()
        if settings.get("registry_path"):
            try:
                rows = [*rows, *read_bindings(settings["registry_path"])]
            except Exception:
                # An invalid registry is already reported by runtime preflight.
                pass
        return set(getattr(runtime, "binding_rows", {})) | {
            row["token_id"]
            for row in rows
            if isinstance(row, dict)
            and type(row.get("token_id")) is int
            and row["token_id"] > 0
            and not is_deleted(
                settings.get("registry_path", "data/vids/bindings.json"), row["token_id"]
            )
        }

    def runtime_ready():
        if runtime is not None:
            return bool(runtime._started)
        return bool(dispatcher._accounts)

    async def detail_rows():
        bound = {row["token_id"]: row for row in await dispatcher.public_accounts()}
        result = []
        for token_id in sorted(configured_ids() | set(bound)):
            row = bound.get(token_id)
            if row is None:
                token = await dispatcher.db.get_token(token_id)
                row = {
                    "token_id": token_id,
                    "email": getattr(token, "email", ""),
                    "enabled": bool(token and token.is_active),
                    "image_enabled": bool(token and getattr(token, "image_enabled", False)),
                    "video_enabled": bool(token and getattr(token, "video_enabled", False)),
                    "ready": False,
                }
            gateway_identity = None
            identity_verified = False
            error = getattr(runtime, "account_errors", {}).get(token_id)
            if error is None and not runtime_ready():
                error = getattr(runtime, "last_error_code", None)
            if row.get("ready") or runtime_ready() and token_id in bound:
                try:
                    binding = dispatcher._account(token_id, network=True)
                    identity = getattr(binding.gateway, "identity", None)
                    if row.get("ready") and isinstance(identity, str) and identity:
                        gateway_identity = identity
                        identity_verified = True
                        error = None
                except (GatewayError, VidsDispatchError) as failure:
                    error = failure.code
            if identity_verified:
                session_state = "ready"
            elif error == "session_authentication_failed":
                session_state = "authentication_expired"
            elif error in {"account_mismatch", "session_identity_mismatch"}:
                session_state = "identity_mismatch"
            else:
                session_state = "unavailable"
            result.append(
                {
                    **row,
                    "vids_identity": gateway_identity,
                    "identity_verified": identity_verified,
                    "session_state": session_state,
                    "runtime_error": error,
                    "last_quota": observed_quota(token_id),
                }
            )
        return result

    async def detail():
        accounts = await detail_rows()
        configured_count = len(configured_ids()) if get_settings is not None else len(accounts)
        return {
            "configured_count": configured_count,
            "connected_count": sum(item["identity_verified"] for item in accounts),
            "runtime_ready": runtime_ready(),
            "runtime_error": getattr(runtime, "last_error_code", None),
            "switches_source": "shared_token",
            "accounts": accounts,
        }

    @router.get("/api/admin/vids/accounts")
    async def vids_accounts():
        return JSONResponse(
            {"accounts": await dispatcher.public_accounts()},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/admin/vids/accounts/detail")
    async def vids_accounts_detail(refresh: bool = False):
        if refresh:
            await refresh_due_quotas()
        return JSONResponse(await detail(), headers={"Cache-Control": "no-store"})

    async def refresh_account_quota(token_id, *, allow_repair, kinds=KINDS):
        repaired = False
        try:
            if not runtime_ready():
                raise GatewayError(503, "session_unavailable", "Vids session is unavailable")
            dispatcher._account(token_id, network=True)
        except (GatewayError, VidsDispatchError) as error:
            if error.code != "session_unavailable" or not allow_repair or repair_session is None:
                raise
            await repair_session(token_id)
            repaired = True
            dispatcher._account(token_id, network=True)
        values = quota_cache.setdefault(token_id, empty_quota())
        for kind in kinds:
            try:
                account = dispatcher._account(token_id, network=True)
                try:
                    quota = await account.gateway.quota(kind)
                except VidsProtocolError as error:
                    if error.status in {401, 403}:
                        account.gateway.client.initialization_valid = False
                    if (
                        error.status not in {401, 403}
                        or repaired
                        or not allow_repair
                        or repair_session is None
                    ):
                        raise
                    await repair_session(token_id)
                    repaired = True
                    account = dispatcher._account(token_id, network=True)
                    quota = await account.gateway.quota(kind)
                values[kind] = {
                    "total": int(quota["total"]),
                    "available": int(quota["available"]),
                    "consumed": int(quota["consumed"]),
                    "queried_at": datetime.now(timezone.utc).isoformat(),
                    "error": None,
                    "stale": False,
                }
            except Exception as failure:
                if isinstance(failure, VidsProtocolError) and failure.status in {401, 403}:
                    binding = dispatcher._accounts.get(token_id)
                    if binding is not None:
                        binding.gateway.client.initialization_valid = False
                # Protocol and transport exceptions may contain private URLs
                # or headers. Keep the last observation and one safe code.
                values[kind] = {
                    **values[kind],
                    "error": (
                        "session_authentication_failed"
                        if isinstance(failure, VidsProtocolError) and failure.status in {401, 403}
                        else "upstream_http_" + str(failure.status)
                        if isinstance(failure, VidsProtocolError)
                        else failure.code
                        if isinstance(failure, GatewayError)
                        else "transport_failed"
                        if isinstance(failure, VidsTransportError)
                        else "quota_unavailable"
                    ),
                    "stale": values[kind]["queried_at"] is not None,
                }
        quota_attempts[token_id] = time.monotonic()

    async def refresh_due_quotas():
        async with refresh_lock:
            if not runtime_ready():
                return
            for row in await dispatcher.public_accounts():
                token_id = row["token_id"]
                kinds = [kind for kind in KINDS if row.get(kind + "_enabled")]
                if not row.get("ready") or not row.get("enabled") or not kinds:
                    continue
                attempted_at = quota_attempts.get(token_id)
                if (
                    attempted_at is not None
                    and time.monotonic() - attempted_at < QUOTA_REFRESH_SECONDS
                ):
                    continue
                try:
                    gateway = dispatcher._account(token_id, network=True).gateway
                    if getattr(gateway, "lock", None) is not None and gateway.lock.locked():
                        continue
                    await refresh_account_quota(token_id, allow_repair=False, kinds=kinds)
                except (GatewayError, VidsDispatchError):
                    # A concurrent runtime/session change is visible in detail().
                    continue

    @router.post("/api/admin/vids/accounts/{token_id}/quota-refresh")
    async def quota_refresh(token_id: int):
        async with refresh_lock:
            try:
                await refresh_account_quota(token_id, allow_repair=True)
            except (GatewayError, VidsDispatchError) as error:
                return JSONResponse(
                    {
                        "error": {
                            "code": error.code,
                            "message": "Vids account session requires recovery",
                        }
                    },
                    status_code=error.status,
                    headers={"Cache-Control": "no-store"},
                )
        accounts = await detail_rows()
        row = next((item for item in accounts if item["token_id"] == token_id), None)
        return JSONResponse({"account": row}, headers={"Cache-Control": "no-store"})

    @router.get("/api/admin/vids/status")
    async def vids_status(refresh: bool = False):
        if refresh:
            await refresh_due_quotas()
        state = await detail()
        accounts = state["accounts"]
        recoverable = dict.fromkeys(("image", "video", "speech"), 0)
        if dispatcher.repair_session is not None:
            for row in accounts:
                if row["ready"] or not row["enabled"]:
                    continue
                try:
                    binding = dispatcher._account(row["token_id"])
                except GatewayError:
                    continue
                gateway = binding.gateway
                if (
                    not getattr(gateway, "_ready", False)
                    or getattr(gateway.client, "initialization_valid", True) is not False
                    or gateway.lock.locked()
                ):
                    continue
                for kind in recoverable:
                    recoverable[kind] += kind == "speech" or row[kind + "_enabled"]
        quota_summary = {}
        for kind in KINDS:
            known = [
                row["last_quota"][kind]
                for row in accounts
                if row["last_quota"][kind]["available"] is not None
            ]
            active = [
                row["last_quota"][kind]
                for row in accounts
                if row["enabled"]
                and row[kind + "_enabled"]
                and row["last_quota"][kind]["available"] is not None
            ]
            active_queried = [item["queried_at"] for item in active]
            quota_summary[kind] = {
                "remaining": sum(item["available"] for item in known) if known else None,
                "total": sum(item["total"] for item in known) if known else None,
                "queried_at": min((item["queried_at"] for item in known), default=None),
                "covered": len(known),
                "eligible": sum(row["enabled"] and row[kind + "_enabled"] for row in accounts),
                "stale": any(item["stale"] for item in known),
                "active_remaining": sum(item["available"] for item in active) if active else None,
                "active_total": sum(item["total"] for item in active) if active else None,
                "active_covered": len(active),
                "active_stale": any(item["stale"] for item in active),
                "active_queried_at": min(active_queried, default=None),
            }
        return JSONResponse(
            {
                "configured": state["configured_count"] > 0,
                "configured_count": state["configured_count"],
                "runtime_ready": state["runtime_ready"],
                "available": {
                    kind: sum(
                        row["ready"]
                        and row["enabled"]
                        and (kind == "speech" or row[kind + "_enabled"])
                        for row in accounts
                    )
                    for kind in ("image", "video", "speech")
                },
                "recoverable": recoverable,
                "quota": quota_summary,
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
