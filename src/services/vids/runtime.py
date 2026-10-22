"""Restore explicit Vids accounts while isolating per-account session failures."""

import asyncio
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from ..proxy_urls import normalize_proxy_url
from .bindings import read_bindings, session_key_file
from .client import VidsClient, VidsProtocolError, cookie_rows
from .deletion_markers import is_deleted
from .dispatcher import VidsAccount, VidsProviderDispatcher
from .gateway import GatewayError, VidsGateway
from .session_store import SessionStore


class VidsRuntime:
    def __init__(self, dispatcher, proxy_manager):
        self.dispatcher = dispatcher
        self.proxy_manager = proxy_manager
        self._gateways = []
        self._started = False
        self.last_error_code = None
        # A failed account is kept out of the dispatcher, while its safe error
        # remains visible to the admin/onboarding layer for targeted recovery.
        self.account_errors = {}
        self.binding_rows = {}
        self._lifecycle_lock = asyncio.Lock()

    async def start(self, settings, *, cookie_sources=None):
        if self._lifecycle_lock.locked():
            raise GatewayError(503, "runtime_busy", "Vids runtime lifecycle is busy")
        async with self._lifecycle_lock:
            self.last_error_code = None
            try:
                await self._start(settings, cookie_sources=cookie_sources)
            except GatewayError as error:
                self.last_error_code = error.code
                raise

    async def _start(self, settings, *, cookie_sources=None):
        if self._started or self._gateways:
            raise GatewayError(503, "runtime_busy", "Vids runtime is already started")
        self.account_errors.clear()
        try:
            if not isinstance(settings, dict) or set(settings) - {"accounts", "registry_path"}:
                raise ValueError
            rows = settings.get("accounts", [])
            if not isinstance(rows, list):
                raise ValueError
            registry_path = settings.get("registry_path")
            if registry_path is not None:
                if not isinstance(registry_path, str) or not registry_path.strip():
                    raise ValueError
                rows = [*rows, *read_bindings(registry_path)]
                rows = [
                    row
                    for row in rows
                    if not isinstance(row, dict)
                    or not is_deleted(registry_path, row.get("token_id"))
                ]
            prepared, identifiers, identities, roots, key_names, editors = (
                [],
                set(),
                set(),
                [],
                set(),
                set(),
            )
            required = {"token_id", "identity_hash", "editor_url", "runtime_dir"}
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) - (required | {"proxy", "session_key_env", "session_key_file"})
                    or required - set(row)
                    or ("session_key_env" in row) == ("session_key_file" in row)
                ):
                    raise ValueError
                identifier, identity, editor, directory = (
                    row[name]
                    for name in (
                        "token_id",
                        "identity_hash",
                        "editor_url",
                        "runtime_dir",
                    )
                )
                if (
                    type(identifier) is not int
                    or identifier <= 0
                    or identifier in identifiers
                    or not isinstance(identity, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", identity)
                    or identity in identities
                    or not isinstance(editor, str)
                    or not re.fullmatch(
                        r"https://docs\.google\.com/videos/(?:u/[0-9]+/)?d/[A-Za-z0-9_-]+/edit\?authuser=[0-9]+",
                        editor,
                    )
                    or editor in editors
                    or not isinstance(directory, str)
                    or not directory.strip()
                    or "proxy" in row
                    and (not isinstance(row["proxy"], str) or not row["proxy"].strip())
                ):
                    raise ValueError
                root = Path(directory).resolve()
                if any(
                    root == previous or root in previous.parents or previous in root.parents
                    for previous in roots
                ):
                    raise ValueError
                if "session_key_env" in row:
                    key_name = row["session_key_env"]
                    if (
                        not isinstance(key_name, str)
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name)
                        or key_name in {"VIDS_API_KEY", "API_KEY"}
                    ):
                        raise ValueError
                    key_identifier = "env:" + key_name
                    key_source = key_name
                else:
                    filename = row["session_key_file"]
                    if not isinstance(filename, str) or not filename.strip():
                        raise ValueError
                    key_path = Path(filename).resolve()
                    if key_path == root or root in key_path.parents:
                        raise ValueError
                    key_identifier = "file:" + str(key_path)
                    key_source = key_path
                if key_identifier in key_names:
                    raise ValueError
                prepared.append((row, root, key_source))
                identifiers.add(identifier)
                identities.add(identity)
                roots.append(root)
                key_names.add(key_identifier)
                editors.add(editor)
        except Exception:
            raise GatewayError(
                503, "invalid_configuration", "Vids account configuration is invalid"
            ) from None

        self.binding_rows = {row["token_id"]: dict(row) for row, _, _ in prepared}
        try:
            bindings = []
            for row, root, key_source in prepared:
                token_id = row["token_id"]
                try:
                    key = (
                        os.environ[key_source]
                        if "session_key_env" in row
                        else session_key_file(key_source)
                    )
                    store = SessionStore(root, key)
                    token = await self.dispatcher.db.get_token(token_id)
                    if token is None:
                        self.account_errors[token_id] = "account_not_found"
                        continue
                    if "proxy" in row:
                        proxy = normalize_proxy_url(row["proxy"])
                    elif hasattr(self.proxy_manager, "resolve_account_proxy"):
                        proxy = await self.proxy_manager.resolve_account_proxy(token)
                    else:
                        proxy = normalize_proxy_url(getattr(token, "proxy_url", None))
                        if proxy is None:
                            proxy = await self.proxy_manager.get_request_proxy_url()
                except Exception:
                    self.account_errors[token_id] = "invalid_configuration"
                    continue
                source = (cookie_sources or {}).get(token_id)
                gateway = None
                try:
                    gateway = VidsGateway(
                        None,
                        root,
                        session_store=store,
                        resume=True,
                        proxy=proxy,
                        expected_account_hash=row["identity_hash"],
                        expected_editor_url=row["editor_url"],
                        cookie_source=source,
                        refresh_session=source is not None,
                    )
                    self._gateways.append(gateway)
                    await gateway.start()
                except Exception as error:
                    if gateway is not None:
                        try:
                            await self._finish_cleanup(gateway.close())
                        except Exception:
                            pass
                        self._gateways.remove(gateway)
                    self.account_errors[token_id] = self._account_error_code(error)
                    continue
                bindings.append(VidsAccount(token_id, gateway, row["identity_hash"]))
            self.dispatcher.replace(bindings)
            self.dispatcher.unavailable_receipt_roots = {
                row["token_id"]: root
                for row, root, _ in prepared
                if row["token_id"] in self.account_errors
            }
            # A valid configuration stays running even if every session needs
            # repair, so the admin can reconnect one account later.
            self._started = True
        except BaseException as error:
            # Cancellation remains pool-wide: release all candidate locks.
            try:
                await self._close()
            except Exception:
                pass
            if isinstance(error, (GatewayError, asyncio.CancelledError)):
                raise
            raise GatewayError(
                503, "session_unavailable", "Vids account pool could not start"
            ) from None

    @staticmethod
    def _account_error_code(error):
        if isinstance(error, GatewayError):
            return error.code
        if isinstance(error, VidsProtocolError) and error.status in {401, 403}:
            return "session_authentication_failed"
        return "session_unavailable"

    async def close(self):
        async def locked_close():
            async with self._lifecycle_lock:
                await self._close()

        await self._finish_cleanup(locked_close())

    @asynccontextmanager
    async def proxy_switch(self, token_id, target_proxy):
        """Stage one verified HTTP transport while the caller commits its Profile and DB."""
        if self._lifecycle_lock.locked():
            raise GatewayError(409, "runtime_busy", "Vids lifecycle maintenance is busy")
        async with self._lifecycle_lock:
            account = self.dispatcher._accounts.get(token_id)
            row = self.binding_rows.get(token_id)
            if account is None:
                if row is not None or token_id in self.account_errors:
                    raise GatewayError(503, "session_unavailable", "Vids account needs recovery")
                yield
                return
            try:
                target_proxy = normalize_proxy_url(target_proxy)
            except ValueError:
                raise GatewayError(400, "invalid_proxy", "Select a valid account proxy") from None
            if row is not None and "proxy" in row:
                if normalize_proxy_url(row["proxy"]) != target_proxy:
                    raise GatewayError(
                        409,
                        "fixed_proxy",
                        "Vids has a separately configured proxy; update that binding first",
                    )
            gateway = account.gateway
            preflight = self.dispatcher._preflight_locks.get(token_id)
            if (
                gateway.lock.locked()
                or token_id in self.dispatcher._pool_pending
                or preflight is not None
                and preflight.locked()
            ):
                raise GatewayError(
                    409, "account_busy", "Vids account is busy; retry after it finishes"
                )
            async with gateway.lock:
                self.dispatcher._account(token_id, network=True)
                old = gateway.client
                if old.proxy == target_proxy:
                    yield
                    return
                cipher = gateway.session_store.path if gateway.session_store is not None else None
                try:
                    encrypted = (
                        cipher.read_bytes() if cipher is not None and cipher.exists() else None
                    )
                except OSError:
                    raise GatewayError(
                        503, "session_persistence_failed", "Vids session backup failed"
                    ) from None
                previous = gateway.proxy, gateway._last_cookies, gateway.persistence_failed
                candidate = None
                committed = False
                persisted = False
                try:
                    try:
                        candidate = VidsClient(
                            old.editor_url,
                            cookie_rows(old.session.cookies.jar),
                            auth_user=old.auth_user,
                            proxy=target_proxy,
                        )
                        await candidate.bootstrap()
                        if (
                            candidate.flags.get("docs-hue") != gateway.identity
                            or getattr(candidate, "initialization_valid", True) is False
                        ):
                            raise GatewayError(
                                409, "account_mismatch", "Vids proxy verification changed account"
                            )
                        await candidate.quota()
                    except GatewayError:
                        raise
                    except Exception:
                        raise GatewayError(
                            503,
                            "proxy_verification_failed",
                            "Vids could not verify the selected proxy",
                        ) from None
                    persisted = True
                    gateway._persist_session(candidate)
                    gateway.client, gateway.proxy = candidate, target_proxy
                    gateway._watch_session()
                    yield
                    committed = True
                except BaseException:
                    gateway.client = old
                    gateway.proxy, gateway._last_cookies, gateway.persistence_failed = previous
                    gateway._watch_session()
                    try:
                        if persisted and cipher is not None:
                            self._restore_session_bytes(cipher, encrypted)
                    except OSError:
                        gateway.persistence_failed = True
                        raise GatewayError(
                            503, "session_persistence_failed", "Vids session rollback failed"
                        ) from None
                    raise
                finally:
                    closing = old if committed else candidate
                    if closing is not None:
                        try:
                            await self._finish_cleanup(closing.session.close())
                        except BaseException:
                            # The selected transport is already committed or restored.
                            # A cancelled caller must still drain the discarded HTTP client.
                            pass

    @staticmethod
    def _restore_session_bytes(path, encrypted):
        if encrypted is None:
            path.unlink(missing_ok=True)
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".session-rollback-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.chmod(temporary, 0o600)
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def adopt(self, binding, persist):
        """Publish one ready candidate while onboarding owns the lifecycle lock."""
        if not self._lifecycle_lock.locked() or not self._started:
            raise GatewayError(503, "session_unavailable", "Vids runtime is unavailable")
        candidate = VidsProviderDispatcher(self.dispatcher.db)
        # Snapshot existing bindings instead of re-registering them. A sibling
        # can become unhealthy while this target is being repaired; recovery
        # must validate only the new candidate and preserve that sibling.
        candidate._accounts = dict(self.dispatcher._accounts)
        candidate._roots = dict(self.dispatcher._roots)
        candidate.register(binding)
        persist()
        # No await between validation, durable binding write and publication.
        self.dispatcher.register(binding)
        self._gateways.append(binding.gateway)
        self.account_errors.pop(binding.token_id, None)
        self.dispatcher.unavailable_receipt_roots.pop(binding.token_id, None)

    async def _close(self):
        self.dispatcher.replace([])
        gateways, self._gateways = self._gateways, []
        self._started = False
        self.account_errors.clear()
        self.binding_rows.clear()

        async def cleanup():
            results = await asyncio.gather(
                *(gateway.close() for gateway in reversed(gateways)), return_exceptions=True
            )
            if any(isinstance(result, BaseException) for result in results):
                raise GatewayError(503, "session_close_failed", "Vids sessions could not all close")

        await self._finish_cleanup(cleanup())

    async def remove_account(self, token_id: int):
        """Detach one account without closing sibling Vids gateways."""
        if type(token_id) is not int or token_id <= 0:
            raise GatewayError(400, "invalid_account", "A valid Flow account is required")
        async with self._lifecycle_lock:
            account = self.dispatcher._accounts.get(token_id)
            preflight = self.dispatcher._preflight_locks.get(token_id)
            if (
                token_id in self.dispatcher._pool_pending
                or (preflight is not None and preflight.locked())
                or (account is not None and account.gateway.lock.locked())
            ):
                raise GatewayError(409, "busy", "Vids account is busy; retry after it finishes")
            if account is not None:
                await account.gateway.close()
                self._gateways = [
                    gateway for gateway in self._gateways if gateway is not account.gateway
                ]
            self.dispatcher._accounts.pop(token_id, None)
            self.dispatcher._roots.pop(token_id, None)
            self.dispatcher.unavailable_receipt_roots.pop(token_id, None)
            self.dispatcher._preflight_locks.pop(token_id, None)
            self.dispatcher._pool_pending.discard(token_id)
            self.account_errors.pop(token_id, None)
            self.binding_rows.pop(token_id, None)
            self.dispatcher._claims = {
                key: value for key, value in self.dispatcher._claims.items() if value != token_id
            }
            self.dispatcher._document_refs = {
                key: value
                for key, value in self.dispatcher._document_refs.items()
                if value[0] != token_id
            }

    @staticmethod
    async def _finish_cleanup(cleanup):
        task = asyncio.create_task(cleanup)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Match Profile cleanup: finish releasing runtime locks even if shutdown is cancelled.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
            task.result()
            raise
