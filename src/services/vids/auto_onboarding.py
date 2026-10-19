"""One automatic Vids connection attempt after a verified Flow Profile."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from ...core.logger import debug_logger
from .bindings import read_bindings

SAFE_CODES = {
    "account_disabled",
    "account_mismatch",
    "account_not_found",
    "already_bound",
    "busy",
    "captcha_required",
    "document_required",
    "identity_mismatch",
    "identity_unavailable",
    "invalid_configuration",
    "login_required",
    "onboarding_failed",
    "profile_unavailable",
    "runtime_unavailable",
    "session_authentication_failed",
    "session_identity_mismatch",
    "session_persistence_failed",
    "session_repair_failed",
    "session_unavailable",
    "unexpected_page",
}


def read_auto_onboarding_state(registry_path, token_id):
    """Return only safe public state; abandoned or malformed attempts are unknown."""
    if type(token_id) is not int or token_id <= 0:
        return {"state": "unbound", "code": None}
    path = Path(registry_path).parent / "auto-onboarding" / str(token_id)
    try:
        with path.open("rb") as handle:
            data = handle.read(1025)
    except FileNotFoundError:
        return {"state": "unbound", "code": None}
    except OSError:
        return {"state": "unknown", "code": "connection_unknown"}
    try:
        value = json.loads(data)
        if (
            len(data) <= 1024
            and isinstance(value, dict)
            and set(value) == {"state", "code"}
            and value["state"] == "failed"
            and value["code"] in SAFE_CODES
        ):
            return value
    except (ValueError, TypeError):
        pass
    return {"state": "unknown", "code": "connection_unknown"}


def _write_state(path, state, code=None):
    descriptor, temporary = tempfile.mkstemp(prefix=".attempt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(json.dumps({"state": state, "code": code}, separators=(",", ":")).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class VidsAutoOnboarding:
    def __init__(self, onboarding):
        self.onboarding = onboarding
        self._tasks = {}
        self._closed = False
        # ponytail: serialize Profile discovery; per-account admission if throughput matters.
        self._lock = asyncio.Lock()

    def schedule(self, token_id):
        """Do not make successful Flow imports or refreshes wait for Vids."""
        if self._closed or type(token_id) is not int or token_id <= 0 or token_id in self._tasks:
            return
        task = asyncio.create_task(self._run(token_id))
        self._tasks[token_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(token_id, None))

    async def _run(self, token_id):
        async with self._lock:
            await self._attempt(token_id)

    async def _attempt(self, token_id):
        service = self.onboarding
        marker = None
        try:
            token = await service.db.get_token(token_id)
            if (
                token is None
                or not token.is_active
                or token.browser_profile_source != "cloak"
                or token.browser_profile_status != "ready"
                or not token.browser_profile_verified_at
                or not (token.image_enabled or token.video_enabled)
                or service.runtime._lifecycle_lock.locked()
                or service._lock.locked()
            ):
                return
            configured = {row.get("token_id") for row in read_bindings(service.registry_path)}
            if getattr(service, "get_settings", None) is not None:
                configured.update(
                    row.get("token_id") for row in service.get_settings().get("accounts", [])
                )
            configured.update(service.runtime.dispatcher._accounts)
            configured.update(getattr(service.runtime, "binding_rows", {}))
            account = service.runtime.dispatcher._accounts.get(token_id)
            repair = token_id in configured and (
                token_id in getattr(service.runtime, "account_errors", {})
                or (
                    not service.runtime._started
                    and service.runtime.last_error_code
                    in {"session_authentication_failed", "session_unavailable"}
                )
                or (
                    service.runtime._started
                    and account is not None
                    and account.gateway._ready
                    and not account.gateway.client.initialization_valid
                    and not account.gateway.lock.locked()
                )
            )
            if (
                token_id in configured
                and not repair
                or token_id not in configured
                and not service.runtime._started
            ):
                return
            manager = service.profile_manager
            account_lock = await manager._account_lock(manager._normalize_account_key(token.email))
            if account_lock.locked():
                return
            if repair:
                await service.reconnect(token_id)
                return
            # A failed attempt can already have created a blank document. Keep
            # this marker across restarts; explicit admin onboarding can retry.
            path = service.registry_path.parent / "auto-onboarding" / str(token_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return
            os.close(descriptor)
            marker = path
            _write_state(marker, "working")
            await service.onboard(token_id, create_blank=True)
            _write_state(marker, "ready")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if marker is not None:
                code = getattr(error, "code", None)
                try:
                    _write_state(
                        marker,
                        "failed",
                        code
                        if isinstance(code, str) and code in SAFE_CODES
                        else "onboarding_failed",
                    )
                except OSError:
                    pass
            # Never expose upstream URLs, cookies, or raw browser exceptions.
            debug_logger.log_warning(
                f"[VIDS_AUTO_ONBOARD] Token {token_id}: {type(error).__name__}"
            )

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
