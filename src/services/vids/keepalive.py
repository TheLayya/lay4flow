"""Bound-account HTTP maintenance, independent of Flow Profile refresh cadence."""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .client import VidsProtocolError, VidsTransportError
from .gateway import GatewayError


@dataclass
class _Observation:
    state: str = "scheduled"
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    next_attempt_at: str | None = None
    error: str | None = None
    consecutive_failures: int = 0
    due: float = 0


class VidsKeepalive:
    """Probe existing bindings only; never discover documents or submit generation.

    All scheduling is process local. Restart starts a fresh interval, while the
    encrypted sessions and account pins remain owned by the existing runtime.
    """

    def __init__(
        self,
        runtime,
        repair_session=None,
        *,
        profile_manager=None,
        get_config=None,
        interval=10 * 60,
        retry_base=300,
        retry_max=60 * 60,
        busy_interval=30,
        attempt_timeout=75,
        scan_interval=30,
        monotonic=None,
        utcnow=None,
    ):
        self.runtime = runtime
        self.dispatcher = runtime.dispatcher
        self.repair_session = repair_session
        self.profile_manager = profile_manager
        self.get_config = get_config
        self.enabled = True
        self.interval = interval
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.busy_interval = busy_interval
        self.attempt_timeout = attempt_timeout
        self.scan_interval = scan_interval
        self._monotonic = monotonic or time.monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._observations = {}
        self._task = None
        self._closed = False
        self._tick_lock = asyncio.Lock()

    def start(self):
        """Start one worker without making application startup wait for probes."""
        if not self._closed and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def close(self):
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            cleanup = asyncio.gather(task, return_exceptions=True)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Profile replacement owns cleanup of opened transports and
                # browser handles. Shutdown must not detach that cleanup.
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass
                cleanup.result()
                raise

    def snapshot(self, token_id):
        value = self._observations.get(token_id)
        if value is None:
            return {
                "state": "scheduled" if token_id in self._bound_ids() else "unbound",
                "last_attempt_at": None,
                "last_success_at": None,
                "next_attempt_at": None,
                "error": None,
                "consecutive_failures": 0,
            }
        return {
            name: getattr(value, name)
            for name in (
                "state",
                "last_attempt_at",
                "last_success_at",
                "next_attempt_at",
                "error",
                "consecutive_failures",
            )
        }

    def public_states(self):
        """Account-console diagnostics contain only timestamps and safe codes."""
        statuses = {
            "scheduled": "scheduled",
            "ready": "ready",
            "probing": "probing",
            "recovering": "recovering",
            "backoff": "retry_wait",
            "busy": "busy",
            "disabled": "paused",
            "missing": "unavailable",
            "unbound": "unavailable",
        }
        result = {}
        for token_id in self._bound_ids():
            value = self.snapshot(token_id)
            result[token_id] = {
                "status": statuses[value["state"]] if self.enabled else "disabled",
                "last_probe_at": value["last_attempt_at"],
                "last_success_at": value["last_success_at"],
                "next_probe_at": value["next_attempt_at"] if self.enabled else None,
                "last_error": value["error"],
                "consecutive_failures": value["consecutive_failures"],
            }
        return result

    def _bound_ids(self):
        return set(self.runtime.binding_rows) | set(self.dispatcher._accounts)

    def _schedule(self, value, delay):
        value.due = self._monotonic() + delay
        value.next_attempt_at = (self._utcnow() + timedelta(seconds=delay)).isoformat()

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Per-account diagnostics are recorded by tick. A database or
                # runtime-wide interruption must not permanently kill the worker.
                pass
            await asyncio.sleep(self.scan_interval)

    async def tick(self):
        """Run due accounts once; skips do not count as maintenance failures."""
        if self._closed or self._tick_lock.locked():
            return
        async with self._tick_lock:
            if self.get_config is not None:
                settings = await self.get_config()
                enabled = settings.get("keepalive_enabled", True)
                interval = settings.get("keepalive_interval_minutes", 10) * 60
                retry_base = settings.get("repair_cooldown_seconds", 300)
                if interval != self.interval or self.enabled != enabled:
                    for value in self._observations.values():
                        if enabled and not self.enabled and value.state == "backoff":
                            # A settings toggle must not bypass a failed repair's
                            # existing deadline and immediately reopen a Profile.
                            continue
                        if value.state == "ready" and value.last_success_at is not None:
                            # A shorter interval takes effect on the next scan.
                            since = (
                                self._utcnow() - datetime.fromisoformat(value.last_success_at)
                            ).total_seconds()
                            self._schedule(value, max(0, interval - since))
                        elif value.state == "scheduled":
                            self._schedule(value, interval)
                if retry_base != self.retry_base:
                    for value in self._observations.values():
                        if value.state == "backoff" and value.last_attempt_at is not None:
                            since = (
                                self._utcnow() - datetime.fromisoformat(value.last_attempt_at)
                            ).total_seconds()
                            delay = min(
                                max(self.retry_max, retry_base),
                                retry_base * 2 ** min(value.consecutive_failures - 1, 16),
                            )
                            self._schedule(value, max(0, delay - since))
                self.enabled, self.interval, self.retry_base = enabled, interval, retry_base
            bound = self._bound_ids()
            for token_id in set(self._observations) - bound:
                self._observations.pop(token_id, None)
            if not self.enabled:
                return
            for token_id in sorted(bound):
                value = self._observations.get(token_id)
                if value is None:
                    value = self._observations[token_id] = _Observation()
                    self._schedule(value, self.interval)
                if self._monotonic() < value.due:
                    continue
                try:
                    token = await self.dispatcher.db.get_token(token_id)
                    if token is None or not getattr(token, "is_active", False):
                        value.state = "disabled" if token is not None else "missing"
                        value.next_attempt_at = None
                        # Keep checking eligibility without repeatedly contacting upstream.
                        value.due = self._monotonic() + self.busy_interval
                        continue
                    policy = getattr(self.dispatcher, "account_policy", None)
                    if policy is not None and await policy.availability(token_id) is not None:
                        value.state = "disabled"
                        value.next_attempt_at = None
                        value.due = self._monotonic() + self.busy_interval
                        continue
                    if not (
                        getattr(token, "image_enabled", False)
                        or getattr(token, "video_enabled", False)
                    ):
                        value.state = "disabled"
                        value.next_attempt_at = None
                        value.due = self._monotonic() + self.busy_interval
                        continue
                    if self._busy(token_id, token):
                        value.state = "busy"
                        self._schedule(value, self.busy_interval)
                        continue
                    value.state = "probing"
                    value.last_attempt_at = self._utcnow().isoformat()
                    async with asyncio.timeout(self.attempt_timeout):
                        await self._maintain(token_id)
                        await self.dispatcher.reconcile_outcomes(token_id)
                except asyncio.CancelledError:
                    value.state = "scheduled"
                    self._schedule(value, self.busy_interval)
                    raise
                except Exception as error:
                    code = self._error_code(error)
                    if code in {"busy", "account_busy", "runtime_busy"}:
                        value.state = "busy"
                        self._schedule(value, self.busy_interval)
                        continue
                    if code in {"account_disabled", "account_not_found"}:
                        value.state = "disabled" if code == "account_disabled" else "missing"
                        value.next_attempt_at = None
                        value.due = self._monotonic() + self.busy_interval
                        continue
                    value.state = "backoff"
                    value.error = code
                    value.consecutive_failures += 1
                    self._schedule(
                        value,
                        min(
                            max(self.retry_max, self.retry_base),
                            self.retry_base * 2 ** min(value.consecutive_failures - 1, 16),
                        ),
                    )
                else:
                    value.state = "ready"
                    value.error = None
                    value.consecutive_failures = 0
                    value.last_success_at = self._utcnow().isoformat()
                    self._schedule(value, self.interval)

    def _busy(self, token_id, token):
        preflight = self.dispatcher._preflight_locks.get(token_id)
        account = self.dispatcher._accounts.get(token_id)
        if (
            not self.runtime._started
            or self.runtime._lifecycle_lock.locked()
            or token_id in self.dispatcher._pool_pending
            or preflight is not None
            and preflight.locked()
            or account is not None
            and account.gateway.lock.locked()
            or getattr(token, "refresh_status", None) == "refreshing"
        ):
            return True
        manager = self.profile_manager
        if manager is not None:
            key = manager._normalize_account_key(token.email)
            lock = manager._account_locks.get(key)
            if lock is not None and lock.locked():
                return True
        return False

    async def _maintain(self, token_id):
        # This order matches generation preflight -> reconnect -> lifecycle.
        # Reserving preflight also keeps paid work out while repair replaces the
        # old HTTP transport. Never retain gateway/lifecycle locks across repair.
        preflight = self.dispatcher._preflight_locks.setdefault(token_id, asyncio.Lock())
        async with preflight:
            if self.runtime._lifecycle_lock.locked() or token_id in self.dispatcher._pool_pending:
                raise GatewayError(409, "busy", "Vids account is busy")
            if token_id not in self._bound_ids():
                raise GatewayError(404, "account_not_found", "Vids binding was removed")
            account = self.dispatcher._accounts.get(token_id)
            repair = account is None
            if account is not None:
                gateway = account.gateway
                if gateway.lock.locked():
                    raise GatewayError(409, "busy", "Vids account is busy")
                async with gateway.lock:
                    # The account reservation prevents deletion/proxy/login
                    # changes; the gateway lock serializes HTTP and shutdown.
                    # No global lifecycle lock is held during slow network I/O.
                    self.dispatcher._verify_identity(
                        account, self.dispatcher._roots[token_id], check_session=False
                    )
                    current = await self.dispatcher.db.get_token(token_id)
                    if current is None or not current.is_active:
                        raise GatewayError(409, "account_disabled", "Vids account is disabled")
                    if self.dispatcher._accounts.get(token_id) is not account:
                        raise GatewayError(409, "runtime_busy", "Vids account binding changed")
                    try:
                        try:
                            await gateway.client.bootstrap()
                        except VidsProtocolError as error:
                            # Editor bootstrap deliberately rejects redirects.
                            # An expired login commonly redirects to Google;
                            # recovery uses the pinned existing Profile rather
                            # than following a response's Location header.
                            if error.status not in {301, 302, 303, 307, 308}:
                                raise
                            gateway.client.initialization_valid = False
                            repair = True
                        if not repair:
                            self.dispatcher._account(token_id, network=True)
                            await gateway.client.quota()
                            gateway._require_session()
                    except VidsProtocolError as error:
                        if error.status not in {401, 403}:
                            raise
                        gateway.client.initialization_valid = False
                        repair = True
            if repair:
                self._observations[token_id].state = "recovering"
                if self.repair_session is None:
                    raise GatewayError(503, "refresh_unavailable", "Vids repair is unavailable")
                # Identity/configuration errors require an explicit correction;
                # do not keep opening a Profile for a rejected saved binding.
                code = self.runtime.account_errors.get(token_id)
                if code in {
                    "account_mismatch",
                    "identity_mismatch",
                    "invalid_configuration",
                    "session_missing",
                    "session_restore_failed",
                }:
                    raise GatewayError(409, code, "Vids binding needs correction")
                current = await self.dispatcher.db.get_token(token_id)
                if current is None or not current.is_active:
                    raise GatewayError(409, "account_disabled", "Vids account is disabled")
                if self._repair_busy(token_id, current):
                    raise GatewayError(409, "busy", "Vids account maintenance is busy")
                await self.repair_session(token_id)
                self.dispatcher._account(token_id, network=True)

    def _repair_busy(self, token_id, token):
        # Our own preflight reservation is intentionally excluded.
        if self.runtime._lifecycle_lock.locked() or token_id in self.dispatcher._pool_pending:
            return True
        account = self.dispatcher._accounts.get(token_id)
        if account is not None and account.gateway.lock.locked():
            return True
        if self.profile_manager is not None:
            manager = self.profile_manager
            key = manager._normalize_account_key(token.email)
            lock = manager._account_locks.get(key)
            if lock is not None and lock.locked():
                return True
        return False

    @staticmethod
    def _error_code(error):
        if isinstance(error, TimeoutError):
            return "maintenance_timeout"
        if isinstance(error, VidsProtocolError):
            return (
                "session_authentication_failed"
                if error.status in {401, 403}
                else "upstream_http_" + str(error.status)
            )
        if isinstance(error, VidsTransportError):
            return "transport_failed"
        if isinstance(error, GatewayError):
            allowed = {
                "account_busy",
                "account_disabled",
                "account_mismatch",
                "account_not_found",
                "busy",
                "identity_mismatch",
                "invalid_configuration",
                "login_required",
                "profile_refresh_failed",
                "profile_unavailable",
                "refresh_unavailable",
                "runtime_busy",
                "runtime_unavailable",
                "session_authentication_failed",
                "session_missing",
                "session_persistence_failed",
                "session_repair_failed",
                "session_restore_failed",
                "session_unavailable",
            }
            if error.code in allowed:
                return error.code
        return "maintenance_failed"
