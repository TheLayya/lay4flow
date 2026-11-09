"""Local Vids account state and cached, read-only upstream quota observations."""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ...services.account_error_policy import PolicyValidationError
from ...services.vids.bindings import read_bindings
from ...services.vids.client import VidsProtocolError, VidsTransportError
from ...services.vids.deletion_markers import is_deleted
from ...services.vids.dispatcher import QUOTA_FRESHNESS_SECONDS, VidsDispatchError
from ...services.vids.gateway import GatewayError

KINDS = ("image", "video")
# Quota is an observation, not a heartbeat.  Keep automatic reads sparse so
# opening several console pages cannot turn into a steady upstream poll.
QUOTA_REFRESH_SECONDS = QUOTA_FRESHNESS_SECONDS
QUOTA_RETRY_MAX_SECONDS = 60 * 60


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
    dispatcher,
    authenticate,
    *,
    get_settings=None,
    runtime=None,
    repair_session=None,
    get_maintenance=None,
):
    router = APIRouter(dependencies=[Depends(authenticate)], tags=["Vids console"])
    # ponytail: process-local quota cache; restart explicitly returns "not queried".
    quota_cache = {}
    # Stores the next monotonic time at which an automatic read is allowed.
    quota_attempts = {}
    quota_failures = {}
    # ponytail: serialize this small pool's refreshes; use account locks if needed.
    refresh_lock = asyncio.Lock()

    def record_quota_attempt(token_id, failed):
        failures = min(quota_failures.get(token_id, 0) + 1, 2) if failed else 0
        quota_failures[token_id] = failures
        delay = min(QUOTA_RETRY_MAX_SECONDS, QUOTA_REFRESH_SECONDS * (2**failures))
        quota_attempts[token_id] = time.monotonic() + delay

    def observed_quota(token_id):
        now = datetime.now(timezone.utc)
        values = dict(quota_cache.get(token_id, empty_quota()))
        for kind in KINDS:
            latest = dispatcher.quota_snapshot(token_id, kind)
            if latest is not None and (
                values[kind]["queried_at"] is None
                or latest["queried_at"] > values[kind]["queried_at"]
            ):
                values[kind] = latest
        return {
            kind: {
                **value,
                "stale": value["stale"]
                or value["queried_at"] is not None
                and (now - datetime.fromisoformat(value["queried_at"])).total_seconds()
                >= QUOTA_REFRESH_SECONDS,
            }
            for kind, value in values.items()
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
        identifiers = configured_ids() | set(bound)
        switches = await dispatcher.account_switches(identifiers - set(bound))
        policy = getattr(dispatcher, "account_policy", None)
        protection = await policy.public_states() if policy is not None else {}
        stats = await policy.account_stats_many(identifiers) if policy is not None else {}
        maintenance = get_maintenance() if get_maintenance is not None else None
        maintenance_states = maintenance.public_states() if maintenance is not None else {}
        result = []
        for token_id in sorted(identifiers):
            row = bound.get(token_id)
            if row is None:
                row = {
                    "token_id": token_id,
                    "email": "",
                    "enabled": False,
                    "image_enabled": False,
                    "video_enabled": False,
                    **switches.get(token_id, {}),
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
            elif error == "session_starting":
                session_state = "starting"
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
                    "protection": protection.get(token_id),
                    "maintenance": maintenance_states.get(token_id),
                    "stats": stats.get(token_id, {}),
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

    @router.get("/api/admin/vids/config")
    async def vids_config():
        policy = getattr(dispatcher, "account_policy", None)
        if policy is None:
            return JSONResponse(
                {"success": False, "message": "Vids policy is unavailable"}, status_code=503
            )
        return JSONResponse(
            {"config": await policy.get_config()}, headers={"Cache-Control": "no-store"}
        )

    @router.post("/api/admin/vids/config")
    async def save_vids_config(values: dict = Body(...)):
        policy = getattr(dispatcher, "account_policy", None)
        if policy is None:
            return JSONResponse(
                {"success": False, "message": "Vids policy is unavailable"}, status_code=503
            )
        try:
            saved = await policy.save_config(values)
        except (PolicyValidationError, ValueError):
            return JSONResponse(
                {"success": False, "message": "Vids 配置无效，请检查规则及数值范围"},
                status_code=422,
            )
        return JSONResponse({"success": True, "config": saved})

    @router.post("/api/admin/vids/accounts/{token_id}/protection/clear")
    async def clear_vids_protection(token_id: int):
        policy = getattr(dispatcher, "account_policy", None)
        if policy is None:
            return JSONResponse(
                {"success": False, "message": "Vids policy is unavailable"}, status_code=503
            )
        if await dispatcher.db.get_token(token_id) is None:
            return JSONResponse({"success": False, "message": "账号不存在"}, status_code=404)
        await policy.clear(token_id)
        return JSONResponse({"success": True})

    @router.get("/api/admin/vids/accounts/detail")
    async def vids_accounts_detail(refresh: bool = False):
        if refresh:
            await refresh_due_quotas()
        return JSONResponse(await detail(), headers={"Cache-Control": "no-store"})

    async def refresh_account_quota(token_id, *, allow_repair, kinds=KINDS):
        repaired = False
        had_error = False
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
                    quota = await dispatcher.read_quota(token_id, kind)
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
                    quota = await dispatcher.read_quota(token_id, kind)
                values[kind] = {
                    "total": int(quota["total"]),
                    "available": int(quota["available"]),
                    "consumed": int(quota["consumed"]),
                    "queried_at": datetime.now(timezone.utc).isoformat(),
                    "error": None,
                    "stale": False,
                }
            except Exception as failure:
                had_error = True
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
        record_quota_attempt(token_id, had_error)

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
                if attempted_at is not None and time.monotonic() < attempted_at:
                    continue
                try:
                    gateway = dispatcher._account(token_id, network=True).gateway
                    if getattr(gateway, "lock", None) is not None and gateway.lock.locked():
                        continue
                    await refresh_account_quota(token_id, allow_repair=False, kinds=kinds)
                except (GatewayError, VidsDispatchError):
                    # A concurrent runtime/session change is visible in detail().
                    record_quota_attempt(token_id, True)
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
                        and not row["protection"]
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
