"""Account-scoped persistent browser profile ownership."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

# Only regenerable caches, never site storage (including Service Worker CacheStorage).
PROFILE_CACHE_PATHS = (
    "BrowserMetrics",
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
)
_logger = logging.getLogger(__name__)


def _prune_closed_profile_cache(profile_dir: Path, root_dir: Path) -> None:
    """Called only after browser close, while the account file lock is held."""
    root = root_dir.absolute()
    profile = profile_dir.absolute()
    failures = 0
    for relative in PROFILE_CACHE_PATHS:
        try:
            if profile.parent != root:
                raise ValueError("unexpected profile location")
            target = profile / relative
            # Check every prefix: Default can itself be a symlink/junction.
            parts = Path(relative).parts
            for directory in (
                root,
                profile,
                *(profile.joinpath(*parts[:index]) for index in range(1, len(parts) + 1)),
            ):
                attributes = directory.lstat()
                if (
                    not stat.S_ISDIR(attributes.st_mode)
                    or getattr(attributes, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise ValueError("linked or non-directory cache ancestor")
            if not target.resolve().is_relative_to(profile.resolve()):
                raise ValueError("cache location changed")
            shutil.rmtree(target)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            failures += 1
    if failures:
        # Deliberately omit account names, paths, exception text and credentials.
        _logger.warning("Profile cache cleanup incomplete: %d cache paths skipped", failures)


class ProfileBackend(Protocol):
    async def open(self, profile_dir: Path, *, proxy_url: str | None = None) -> Any: ...

    async def close(self, context: Any) -> None: ...


class ProfileNotReadyError(RuntimeError):
    def __init__(self, account_key: str, reason: str):
        super().__init__(f"profile is not ready: {reason}")
        self.account_key = account_key
        self.reason = reason


@dataclass(frozen=True)
class ProfileSpec:
    account_key: str
    profile_dir: Path
    fingerprint_seed: int


@dataclass(frozen=True)
class ProfileStatus:
    account_key: str
    profile_dir: str
    state: str
    last_started_at: float | None = None
    last_verified_at: float | None = None
    last_error: str = ""
    fingerprint_seed: int | None = None


@dataclass
class _WarmHandle:
    spec: ProfileSpec
    context: Any
    file_lock: "_ProfileFileLock"
    proxy_url: str | None
    last_used: float
    interactive: bool = False
    in_use: bool = True
    expiry_task: asyncio.Task | None = None
    close_task: asyncio.Task | None = None


class _ProfileFileLock:
    def __init__(self, path: Path, timeout: float = 60.0):
        self.path = path
        self.timeout = timeout
        self._handle = None

    async def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle = open(self.path, "a+b")
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except OSError:
                if "handle" in locals():
                    handle.close()
                if time.monotonic() >= deadline:
                    raise TimeoutError("profile is busy") from None
                await asyncio.sleep(0.1)

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class ProfileManager:
    """Own one persistent browser context per account and limit concurrency."""

    def __init__(
        self,
        root_dir: str | Path,
        backend: ProfileBackend,
        max_concurrent: int = 1,
        warm_slots: int = 0,
        warm_idle_seconds: float = 300,
    ):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        if warm_slots < 0:
            raise ValueError("warm_slots must not be negative")
        if warm_idle_seconds <= 0:
            raise ValueError("warm_idle_seconds must be positive")
        self.root_dir = Path(root_dir)
        self.backend = backend
        self.max_concurrent = max_concurrent
        self._capacity = asyncio.Semaphore(max_concurrent)
        # The management viewer is an interactive lease. Keep one separate
        # slot so a human can inspect one account while background maintenance
        # refreshes another; the per-account lock and file lock still apply.
        self._interactive_capacity = asyncio.Semaphore(1)
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._seed_guard = asyncio.Lock()
        self._warm_slots = min(int(warm_slots), max_concurrent)
        self._warm_idle_seconds = float(warm_idle_seconds)
        self._warm_changed = asyncio.Condition()
        self._warm_handles: dict[str, _WarmHandle] = {}
        self._warm_opening: set[str] = set()
        self._warm_sessions: set[str] = set()
        self._warm_shutdown = False
        # Keep ownership if a backend cannot confirm that its browser closed.
        self._failed_handles: dict[str, _WarmHandle] = {}
        self._proxy_switch_failed_stages: list["ProfileManager"] = []

    async def switch_proxy(
        self, account_key, old_proxy_url, new_proxy_url, verifier, validate, commit
    ):
        from .proxy_switch import switch_profile_proxy

        return await switch_profile_proxy(
            self, account_key, old_proxy_url, new_proxy_url, verifier, validate, commit
        )

    def _existing_spec(self, normalized: str) -> ProfileSpec:
        profile_dir = self.root_dir / normalized
        try:
            if not (profile_dir / "Default").is_dir() or not all(
                (profile_dir / name).is_file()
                for name in ("metadata.json", "environment_snapshot.json", "launch_settings.sha256")
            ):
                raise ValueError
            stored = json.loads((profile_dir / "metadata.json").read_text(encoding="utf-8"))
            seed = (
                self._validate_seed(stored.get("fingerprint_seed"))
                if isinstance(stored, dict)
                else None
            )
            if seed is None or stored.get("account_key") != normalized:
                raise ValueError
        except (OSError, TypeError, ValueError):
            raise ValueError("An existing initialized account Profile is required") from None
        return ProfileSpec(normalized, profile_dir, seed)

    async def spec(self, account_key: str, *, existing_only: bool = False) -> ProfileSpec:
        normalized = self._normalize_account_key(account_key)
        profile_dir = self.root_dir / normalized
        metadata = profile_dir / "metadata.json"
        async with self._seed_guard:
            if existing_only:
                return self._existing_spec(normalized)
            if metadata.exists():
                stored = json.loads(metadata.read_text(encoding="utf-8"))
                if stored.get("account_key") != normalized:
                    raise ValueError("profile directory belongs to another account")
                seed = self._validate_seed(stored.get("fingerprint_seed"))
                if seed is None:
                    if "fingerprint_seed" in stored or (profile_dir / "Default").exists():
                        raise ValueError("existing profile requires explicit fingerprint migration")
                    seed = self._allocate_seed(normalized)
                    stored["fingerprint_seed"] = seed
                    self._write_metadata_file(metadata, stored)
                else:
                    self._claim_seed(normalized, seed)
            else:
                if profile_dir.exists() and any(profile_dir.iterdir()):
                    raise ValueError("existing profile requires explicit fingerprint migration")
                seed = self._allocate_seed(normalized)
                profile_dir.mkdir(parents=True, exist_ok=True)
                self._write_metadata_file(
                    metadata,
                    {
                        "account_key": normalized,
                        "profile_dir": str(profile_dir),
                        "state": "new",
                        "fingerprint_seed": seed,
                    },
                )
        return ProfileSpec(normalized, profile_dir, seed)

    async def adopt_identity(self, old_account_key: str, new_account_key: str) -> ProfileSpec:
        """Move a closed Profile to a verified identity without changing its state."""
        old_normalized = self._normalize_account_key(old_account_key)
        new_normalized = self._normalize_account_key(new_account_key)
        if old_normalized == new_normalized:
            raise ValueError("profile identity is unchanged")

        # Account locks are always acquired in lexical order, then the seed
        # registry guard.  This matches ``session``/``spec`` and avoids a
        # rename racing a browser open or seed allocation.
        lock_keys = sorted((old_normalized, new_normalized))
        locks = [await self._account_lock(key) for key in lock_keys]
        if any(lock.locked() for lock in locks):
            raise ProfileNotReadyError(old_normalized, "profile_is_open")
        async with locks[0], locks[1], self._seed_guard:
            if any(
                key in self._warm_handles
                or key in self._warm_opening
                or key in self._warm_sessions
                or key in self._failed_handles
                for key in (old_normalized, new_normalized)
            ):
                raise ProfileNotReadyError(old_normalized, "profile_is_open")

            root = self.root_dir.absolute()
            old_dir = self.root_dir / old_normalized
            new_dir = self.root_dir / new_normalized
            # Normalized keys are generated locally, but validate the actual
            # paths too: an imported/junctioned old Profile must never escape
            # the Profile root, and an existing destination is never replaced.
            if old_dir.parent.absolute() != root or new_dir.parent.absolute() != root:
                raise ValueError("profile path is outside the profile root")
            if old_dir.is_symlink() or new_dir.is_symlink():
                raise ValueError("linked profile paths are not supported")
            if not old_dir.is_dir() or new_dir.exists():
                raise ValueError("source Profile is missing or destination already exists")
            for directory in (root, old_dir):
                attributes = directory.lstat()
                if getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("linked profile paths are not supported")
            try:
                if old_dir.resolve().parent != root.resolve():
                    raise ValueError("source Profile is outside the profile root")
            except OSError:
                raise ValueError("source Profile path cannot be resolved") from None

            metadata_path = old_dir / "metadata.json"
            if metadata_path.is_symlink():
                raise ValueError("linked Profile metadata is not supported")
            try:
                metadata_bytes = metadata_path.read_bytes()
                metadata = json.loads(metadata_bytes.decode("utf-8"))
            except (OSError, UnicodeError, TypeError, ValueError):
                raise ValueError("source Profile metadata is invalid") from None
            if not isinstance(metadata, dict) or metadata.get("account_key") != old_normalized:
                raise ValueError("source Profile identity does not match")
            seed = self._validate_seed(metadata.get("fingerprint_seed"))
            if seed is None:
                raise ValueError("source Profile fingerprint seed is invalid")
            if metadata.get("state") == "starting":
                raise ProfileNotReadyError(old_normalized, "profile_is_open")

            source_lock = _ProfileFileLock(old_dir / ".profile.lock", timeout=0)
            database = sqlite3.connect(self.root_dir / "fingerprint_seeds.sqlite3", timeout=30)
            moved = []
            destination_created = False
            db_committed = False
            try:
                # The manager lock covers our own sessions; this non-blocking
                # file lock also catches a browser launched outside this
                # manager.  An identity move must never race Chromium writes.
                await source_lock.acquire()
                with database:
                    database.execute(
                        "CREATE TABLE IF NOT EXISTS seeds "
                        "(seed INTEGER PRIMARY KEY, account_key TEXT UNIQUE NOT NULL)"
                    )
                    old_row = database.execute(
                        "SELECT seed FROM seeds WHERE account_key = ?", (old_normalized,)
                    ).fetchone()
                    new_row = database.execute(
                        "SELECT seed FROM seeds WHERE account_key = ?", (new_normalized,)
                    ).fetchone()
                    seed_row = database.execute(
                        "SELECT account_key FROM seeds WHERE seed = ?", (seed,)
                    ).fetchone()
                    if new_row is not None or (
                        seed_row is not None and seed_row[0] != old_normalized
                    ):
                        raise ValueError(
                            "destination Profile identity already owns a fingerprint seed"
                        )
                    if old_row is not None and old_row[0] != seed:
                        raise ValueError("source Profile fingerprint seed registration conflict")
                    if seed_row is None:
                        database.execute(
                            "INSERT INTO seeds(seed, account_key) VALUES (?, ?)",
                            (seed, new_normalized),
                        )
                    else:
                        database.execute(
                            "UPDATE seeds SET account_key = ? WHERE seed = ? AND account_key = ?",
                            (new_normalized, seed, old_normalized),
                        )
                        if database.execute("SELECT changes()").fetchone()[0] != 1:
                            raise ValueError(
                                "source Profile fingerprint seed registration conflict"
                            )

                    new_dir.mkdir()
                    destination_created = True
                    for child in old_dir.iterdir():
                        if child.name == ".profile.lock":
                            continue
                        child.rename(new_dir / child.name)
                        moved.append(child.name)
                    metadata["account_key"] = new_normalized
                    metadata["profile_dir"] = str(new_dir)
                    self._write_metadata_file(new_dir / "metadata.json", metadata)
                    database.commit()
                    db_committed = True
            except BaseException:
                # ``with database`` rolls back an uncommitted seed update.
                if destination_created:
                    try:
                        if "metadata.json.tmp" not in moved:
                            (new_dir / "metadata.json.tmp").unlink(missing_ok=True)
                        for name in reversed(moved):
                            (new_dir / name).rename(old_dir / name)
                        metadata_path.write_bytes(metadata_bytes)
                        new_dir.rmdir()
                    except OSError as rollback_error:
                        self._failed_handles[old_normalized] = None
                        self._failed_handles[new_normalized] = None
                        _logger.error(
                            "Profile identity rollback failed: %s", type(rollback_error).__name__
                        )
                raise
            finally:
                source_lock.release()
                if db_committed:
                    try:
                        (old_dir / ".profile.lock").unlink(missing_ok=True)
                        old_dir.rmdir()
                    except OSError:
                        # The destination and seed row are valid; retain an
                        # empty source shell rather than deleting unknown data.
                        _logger.warning("Profile identity source cleanup incomplete")
                database.close()
            return ProfileSpec(new_normalized, new_dir, seed)

    async def status(self, account_key: str) -> ProfileStatus:
        spec = await self.spec(account_key)
        metadata = self._metadata_path(spec)
        if not metadata.exists():
            return ProfileStatus(
                spec.account_key,
                str(spec.profile_dir),
                "new",
                fingerprint_seed=spec.fingerprint_seed,
            )
        values = json.loads(metadata.read_text(encoding="utf-8"))
        return ProfileStatus(
            account_key=values["account_key"],
            profile_dir=values["profile_dir"],
            state=values["state"],
            last_started_at=values.get("last_started_at"),
            last_verified_at=values.get("last_verified_at"),
            last_error=values.get("last_error", ""),
            fingerprint_seed=values.get("fingerprint_seed", spec.fingerprint_seed),
        )

    async def mark_verified(self, account_key: str) -> ProfileStatus:
        spec = await self.spec(account_key)
        return await self._write_status(
            spec, state="ready", last_verified_at=time.time(), last_error=""
        )

    async def mark_error(self, account_key: str, error: str) -> ProfileStatus:
        spec = await self.spec(account_key)
        return await self._write_status(
            spec,
            state="error",
            last_error=type(error).__name__,
        )

    async def mark_blocked(self, account_key: str, reason: str) -> ProfileStatus:
        spec = await self.spec(account_key)
        return await self._write_status(
            spec, state="blocked", last_error=str(reason or "profile_not_ready")
        )

    async def remove(self, account_key: str) -> None:
        normalized = self._normalize_account_key(account_key)
        profile_dir = self.root_dir / normalized
        if not profile_dir.exists():
            database = sqlite3.connect(self.root_dir / "fingerprint_seeds.sqlite3", timeout=30)
            try:
                with database:
                    database.execute("DELETE FROM seeds WHERE account_key = ?", (normalized,))
            finally:
                database.close()
            return
        account_lock = await self._account_lock(normalized)
        async with account_lock:
            await self._drop_warm_locked(normalized)
            await self._reserve_capacity()
            try:
                lock = _ProfileFileLock(profile_dir / ".profile.lock")
                await lock.acquire()
                try:
                    for child in profile_dir.iterdir():
                        if child.name == ".profile.lock":
                            continue
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                finally:
                    lock.release()
                (profile_dir / ".profile.lock").unlink(missing_ok=True)
                profile_dir.rmdir()
                database = sqlite3.connect(self.root_dir / "fingerprint_seeds.sqlite3", timeout=30)
                try:
                    with database:
                        database.execute("DELETE FROM seeds WHERE account_key = ?", (normalized,))
                finally:
                    database.close()
            finally:
                await self._release_capacity()

    @asynccontextmanager
    async def session(
        self,
        account_key: str,
        *,
        proxy_url: str | None = None,
        reset_state: bool = False,
        interactive: bool = False,
        existing_only: bool = False,
    ) -> AsyncIterator[Any]:
        if existing_only and reset_state:
            raise ValueError("Existing Profile sessions cannot reset browser state")
        account_lock = await self._account_lock(self._normalize_account_key(account_key))
        async with account_lock:
            spec = await self.spec(account_key, existing_only=existing_only)
            # Bootstrap/viewer sessions close this account's idle warm browser
            # before reopening the original imported Profile exclusively.
            await self._drop_warm_locked(spec.account_key)
            handle = await self._open_handle(
                spec,
                proxy_url,
                interactive=interactive,
                reset_state=reset_state,
                existing_only=existing_only,
            )
            try:
                yield handle.context
            finally:
                await self._close_handle(handle)

    @property
    def warm_slots(self) -> int:
        return self._warm_slots

    @asynccontextmanager
    async def warm_session(self, account_key: str, *, proxy_url: str | None = None):
        """Borrow a bounded warm Profile; never inject cookies on this path."""
        if self._warm_slots <= 0:
            async with self.session(account_key, proxy_url=proxy_url) as context:
                yield context
            return
        spec = await self.spec(account_key)
        account_lock = await self._account_lock(spec.account_key)
        async with account_lock:
            async with self._warm_changed:
                if self._warm_shutdown:
                    raise ProfileNotReadyError(spec.account_key, "transient_error")
                self._warm_sessions.add(spec.account_key)
            try:
                handle, keep_warm = await self._borrow_warm(spec, proxy_url)
                succeeded = False
                try:
                    yield handle.context
                    succeeded = True
                finally:
                    finish = asyncio.create_task(self._finish_warm(handle, keep_warm and succeeded))
                    try:
                        await asyncio.shield(finish)
                    except asyncio.CancelledError:
                        await self._wait_uninterrupted(finish)
                        raise
            finally:
                async with self._warm_changed:
                    self._warm_sessions.discard(spec.account_key)
                    self._warm_changed.notify_all()

    async def _finish_warm(self, handle: _WarmHandle, keep_warm: bool) -> None:
        async with self._warm_changed:
            keep_warm = keep_warm and not self._warm_shutdown
            if keep_warm:
                handle.last_used = time.monotonic()
                handle.in_use = False
                self._arm_warm_expiry(handle)
                self._warm_changed.notify_all()
        if not keep_warm:
            await self._close_handle(handle)

    async def drop_warm(self, account_key: str) -> None:
        normalized = self._normalize_account_key(account_key)
        account_lock = await self._account_lock(normalized)
        async with account_lock:
            await self._drop_warm_locked(normalized)

    async def close_warm(self) -> None:
        async with self._warm_changed:
            self._warm_shutdown = True
            keys = set(self._warm_handles) | self._warm_opening | self._warm_sessions
            self._warm_changed.notify_all()

        async def close_account(key):
            account_lock = await self._account_lock(key)
            async with account_lock:
                await self._drop_warm_locked(key)

        async def shutdown():
            results = await asyncio.gather(
                *(close_account(key) for key in keys), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result

        task = asyncio.create_task(shutdown())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._wait_uninterrupted(task)
            raise

    async def _drop_warm_locked(self, account_key: str) -> None:
        async with self._warm_changed:
            handle = self._warm_handles.get(account_key)
            if handle is not None:
                handle.in_use = True
        if handle is not None:
            await self._close_handle(handle)

    async def _borrow_warm(self, spec: ProfileSpec, proxy_url: str | None):
        victim = None
        opened = None
        async with self._warm_changed:
            if self._warm_shutdown:
                raise ProfileNotReadyError(spec.account_key, "transient_error")
            handle = self._warm_handles.get(spec.account_key)
            if handle is not None:
                if handle.close_task is not None:
                    if not handle.close_task.done():
                        closing = handle.close_task
                    else:
                        raise ProfileNotReadyError(spec.account_key, "transient_error")
                else:
                    if handle.proxy_url != proxy_url:
                        raise ProfileNotReadyError(spec.account_key, "environment_changed")
                    handle.in_use = True
                    self._cancel_expiry(handle)
                    return handle, True
            else:
                closing = None
            if closing is None:
                keep_warm = len(self._warm_handles) + len(self._warm_opening) < self._warm_slots
                if not keep_warm:
                    idle = [h for h in self._warm_handles.values() if not h.in_use]
                    if idle:
                        victim = min(idle, key=lambda h: h.last_used)
                        victim.in_use = True
                        keep_warm = True
                if keep_warm:
                    self._warm_opening.add(spec.account_key)
        if closing is not None:
            await asyncio.shield(closing)
            return await self._borrow_warm(spec, proxy_url)
        try:
            if victim is not None:
                await self._close_handle(victim)
            handle = opened = await self._open_handle(spec, proxy_url)
            async with self._warm_changed:
                if keep_warm:
                    self._warm_handles[spec.account_key] = handle
            return handle, keep_warm
        except BaseException:
            if opened is not None:
                await self._close_handle(opened)
            raise
        finally:
            async with self._warm_changed:
                self._warm_opening.discard(spec.account_key)
                self._warm_changed.notify_all()

    async def _reserve_capacity(self) -> None:
        while True:
            async with self._warm_changed:
                if not self._capacity.locked():
                    await self._capacity.acquire()
                    return
                idle = [h for h in self._warm_handles.values() if not h.in_use]
                if idle:
                    victim = min(idle, key=lambda h: h.last_used)
                    victim.in_use = True
                else:
                    await self._warm_changed.wait()
                    continue
            await self._close_handle(victim)

    async def _release_capacity(self, *, interactive: bool = False) -> None:
        async with self._warm_changed:
            capacity = self._interactive_capacity if interactive else self._capacity
            capacity.release()
            self._warm_changed.notify_all()

    async def _open_handle(
        self,
        spec: ProfileSpec,
        proxy_url: str | None,
        *,
        interactive: bool = False,
        reset_state: bool = False,
        existing_only: bool = False,
    ) -> _WarmHandle:
        if spec.account_key in self._failed_handles:
            raise ProfileNotReadyError(spec.account_key, "transient_error")
        if interactive:
            await self._interactive_capacity.acquire()
        else:
            await self._reserve_capacity()
        file_lock = _ProfileFileLock(spec.profile_dir / ".profile.lock")
        context = None
        validated = not existing_only
        try:
            await file_lock.acquire()
            if existing_only:
                if self._existing_spec(spec.account_key) != spec:
                    raise ValueError("Existing Profile identity changed")
                self._claim_seed(spec.account_key, spec.fingerprint_seed)
                validated = True
            if reset_state:
                self._reset_browser_state(spec.profile_dir)
            await self._write_status(spec, state="starting", last_started_at=time.time())
            launch = asyncio.create_task(
                self._open_backend(spec.profile_dir, proxy_url, interactive=interactive)
            )
            try:
                context = await asyncio.shield(launch)
            except asyncio.CancelledError:
                context = await self._wait_uninterrupted(launch)
                raise
            return _WarmHandle(spec, context, file_lock, proxy_url, time.monotonic(), interactive)
        except BaseException as error:
            try:
                if existing_only and validated and isinstance(error, Exception):
                    await self._write_status(spec, state="error", last_error=type(error).__name__)
            finally:
                if context is not None:
                    handle = _WarmHandle(
                        spec, context, file_lock, proxy_url, time.monotonic(), interactive
                    )
                    await self._close_handle(handle)
                else:
                    file_lock.release()
                    await self._release_capacity(interactive=interactive)
            if not existing_only and isinstance(error, Exception):
                await self._write_status(spec, state="error", last_error=type(error).__name__)
            raise

    async def _close_handle(self, handle: _WarmHandle) -> None:
        self._cancel_expiry(handle)
        # All close paths share one task; cancellation cannot free locks while
        # Chromium is still writing the persistent Profile.
        if handle.close_task is None:

            async def close():
                try:
                    await self.backend.close(handle.context)
                except BaseException:
                    self._failed_handles[handle.spec.account_key] = handle
                    raise
                try:
                    try:
                        cleanup = asyncio.get_running_loop().run_in_executor(
                            None,
                            _prune_closed_profile_cache,
                            handle.spec.profile_dir,
                            self.root_dir,
                        )
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            # A shutdown may cancel close_task itself. A worker
                            # thread still deleting caches must retain the lock.
                            await self._wait_uninterrupted(cleanup)
                            raise
                    except Exception:
                        _logger.warning(
                            "Profile cache cleanup worker failed; account state retained"
                        )
                    values = json.loads(
                        self._metadata_path(handle.spec).read_text(encoding="utf-8")
                    )
                    if values.get("state") not in {"blocked", "error"}:
                        await self._write_status(handle.spec, state="stopped")
                finally:
                    handle.file_lock.release()
                    async with self._warm_changed:
                        if self._warm_handles.get(handle.spec.account_key) is handle:
                            self._warm_handles.pop(handle.spec.account_key)
                        capacity = (
                            self._interactive_capacity if handle.interactive else self._capacity
                        )
                        capacity.release()
                        self._warm_changed.notify_all()

            handle.close_task = asyncio.create_task(close())
        try:
            await asyncio.shield(handle.close_task)
        except asyncio.CancelledError:
            await self._wait_uninterrupted(handle.close_task)
            raise

    @staticmethod
    async def _wait_uninterrupted(task: asyncio.Task):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        return task.result()

    @staticmethod
    def _cancel_expiry(handle: _WarmHandle) -> None:
        if handle.expiry_task and handle.expiry_task is not asyncio.current_task():
            handle.expiry_task.cancel()
        handle.expiry_task = None

    def _arm_warm_expiry(self, handle: _WarmHandle) -> None:
        self._cancel_expiry(handle)

        async def expire():
            try:
                deadline = handle.last_used + self._warm_idle_seconds
                while (remaining := deadline - time.monotonic()) > 0:
                    await asyncio.sleep(remaining)
                account_lock = await self._account_lock(handle.spec.account_key)
                async with account_lock:
                    async with self._warm_changed:
                        if (
                            self._warm_handles.get(handle.spec.account_key) is not handle
                            or handle.in_use
                            or time.monotonic() - handle.last_used < self._warm_idle_seconds
                        ):
                            return
                        handle.in_use = True
                    await self._close_handle(handle)
            except asyncio.CancelledError:
                pass
            except Exception as error:
                await self._write_status(
                    handle.spec, state="error", last_error=type(error).__name__
                )

        handle.expiry_task = asyncio.create_task(expire())

    @asynccontextmanager
    async def ready_session(
        self,
        account_key: str,
        verifier: Any,
        *,
        proxy_url: str | None = None,
    ) -> AsyncIterator[Any]:
        spec = await self.spec(account_key)
        async with self.session(account_key, proxy_url=proxy_url) as context:
            result = await verifier.verify_ready(context, str(spec.profile_dir))
            if not result.workspace_visible:
                await self._write_status(spec, state="blocked", last_error=result.reason)
                raise ProfileNotReadyError(spec.account_key, result.reason)
            yield context

    @asynccontextmanager
    async def credential_session(
        self,
        account_key: str,
        verifier: Any,
        *,
        proxy_url: str | None = None,
        existing_only: bool = False,
    ) -> AsyncIterator[Any]:
        """Open a profile for credential refresh without requiring the workspace UI."""
        async with self.session(
            account_key, proxy_url=proxy_url, existing_only=existing_only
        ) as context:
            spec = await self.spec(account_key, existing_only=existing_only)
            if getattr(self.backend, "options", {}).get("headless") is False:
                result = await verifier.verify_environment(
                    context, str(spec.profile_dir), allow_viewport_change=True
                )
            else:
                result = await verifier.verify_environment(context, str(spec.profile_dir))
            if result != "environment_stable":
                await self._write_status(spec, state="blocked", last_error=result)
                raise ProfileNotReadyError(spec.account_key, result)
            yield context

    @asynccontextmanager
    async def bootstrap_session(
        self,
        account_key: str,
        verifier: Any,
        cookies: list[dict[str, Any]],
        *,
        account_probe: Any,
        proxy_url: str | None = None,
    ) -> AsyncIterator[Any]:
        spec = await self.spec(account_key)
        async with self.session(account_key, proxy_url=proxy_url, reset_state=True) as context:
            add_cookies = getattr(context, "add_cookies", None)
            if not callable(add_cookies):
                await self._write_status(
                    spec, state="blocked", last_error="cookie_injection_unavailable"
                )
                raise ProfileNotReadyError(spec.account_key, "cookie_injection_unavailable")
            try:
                for cookie in cookies:
                    await add_cookies([cookie])
            except Exception as error:
                name = str(cookie.get("name") or "unknown")
                await self._write_status(
                    spec,
                    state="blocked",
                    last_error=f"cookie_injection_failed:{name}:{type(error).__name__}",
                )
                raise ProfileNotReadyError(spec.account_key, "cookie_injection_failed") from None
            try:
                reason = await verifier.verify_environment(context, str(spec.profile_dir))
            except TimeoutError:
                reason = "environment_timeout"
            except Exception:
                reason = "environment_probe_error"
            if reason == "environment_stable":
                if not callable(getattr(getattr(context, "request", None), "get", None)):
                    reason = "account_probe_error"
                else:
                    account_probe.set_context(context)
                    account = await verifier.verify_account(str(account_key), account_probe)
                    reason = account.reason
            if reason != "account_ready":
                await self._write_status(spec, state="blocked", last_error=reason)
                raise ProfileNotReadyError(spec.account_key, reason)
            yield context

    @asynccontextmanager
    async def verified_session(
        self,
        account_key: str,
        verifier: Any,
        *,
        expected_email: str,
        account_probe: Any,
        proxy_url: str | None = None,
    ) -> AsyncIterator[Any]:
        spec = await self.spec(account_key)
        async with self.ready_session(account_key, verifier, proxy_url=proxy_url) as context:
            if hasattr(account_probe, "set_context"):
                account_probe.set_context(context)
            account = await verifier.verify_account(expected_email, account_probe)
            if account.reason != "account_ready":
                await self._write_status(spec, state="blocked", last_error=account.reason)
                raise ProfileNotReadyError(spec.account_key, account.reason)
            yield context

    async def _open_backend(
        self, profile_dir: Path, proxy_url: str | None, *, interactive: bool = False
    ) -> Any:
        method = "open_interactive" if interactive else "open_background"
        opener = getattr(self.backend, method, self.backend.open)
        if proxy_url is None:
            return await opener(profile_dir)
        return await opener(profile_dir, proxy_url=proxy_url)

    async def _account_lock(self, account_key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._account_locks.setdefault(account_key, asyncio.Lock())

    @staticmethod
    def _metadata_path(spec: ProfileSpec) -> Path:
        return spec.profile_dir / "metadata.json"

    @staticmethod
    def _reset_browser_state(profile_dir: Path) -> None:
        """Remove stale browser state while retaining identity-owned settings."""
        for child in profile_dir.iterdir():
            if child.name in {"metadata.json", "launch_settings.sha256", ".profile.lock"}:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    async def _write_status(self, spec: ProfileSpec, **changes: Any) -> ProfileStatus:
        metadata = self._metadata_path(spec)
        if metadata.exists():
            current = json.loads(metadata.read_text(encoding="utf-8"))
        else:
            current = ProfileStatus(
                spec.account_key,
                str(spec.profile_dir),
                "new",
                fingerprint_seed=spec.fingerprint_seed,
            ).__dict__
        current.update(changes)
        current["account_key"] = spec.account_key
        current["profile_dir"] = str(spec.profile_dir)
        current["fingerprint_seed"] = spec.fingerprint_seed
        spec.profile_dir.mkdir(parents=True, exist_ok=True)
        metadata = self._metadata_path(spec)
        self._write_metadata_file(metadata, current)
        return ProfileStatus(**current)

    @staticmethod
    def _write_metadata_file(metadata: Path, values: dict[str, Any]) -> None:
        temporary = metadata.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(metadata)

    def _allocate_seed(self, account_key: str) -> int:
        digest = hashlib.sha256(account_key.encode("utf-8")).digest()
        candidate = 10000 + int.from_bytes(digest[:4], "big") % 90000
        self.root_dir.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.root_dir / "fingerprint_seeds.sqlite3", timeout=30)
        try:
            with database:
                database.execute(
                    "CREATE TABLE IF NOT EXISTS seeds (seed INTEGER PRIMARY KEY, account_key TEXT UNIQUE NOT NULL)"
                )
                existing = database.execute(
                    "SELECT seed FROM seeds WHERE account_key = ?", (account_key,)
                ).fetchone()
                if existing:
                    return existing[0]
                for _ in range(90000):
                    try:
                        database.execute(
                            "INSERT INTO seeds(seed, account_key) VALUES (?, ?)",
                            (candidate, account_key),
                        )
                        return candidate
                    except sqlite3.IntegrityError:
                        owner = database.execute(
                            "SELECT account_key FROM seeds WHERE seed = ?", (candidate,)
                        ).fetchone()
                        if owner and owner[0] == account_key:
                            return candidate
                        candidate = 10000 + (candidate - 9999) % 90000
        finally:
            database.close()
        raise RuntimeError("no fingerprint seeds available")

    def _claim_seed(self, account_key: str, seed: int) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.root_dir / "fingerprint_seeds.sqlite3", timeout=30)
        try:
            with database:
                database.execute(
                    "CREATE TABLE IF NOT EXISTS seeds (seed INTEGER PRIMARY KEY, account_key TEXT UNIQUE NOT NULL)"
                )
                try:
                    database.execute(
                        "INSERT INTO seeds(seed, account_key) VALUES (?, ?)", (seed, account_key)
                    )
                except sqlite3.IntegrityError:
                    owner = database.execute(
                        "SELECT account_key FROM seeds WHERE seed = ?", (seed,)
                    ).fetchone()
                    if not owner or owner[0] != account_key:
                        raise ValueError("fingerprint seed registration conflict") from None
        finally:
            database.close()

    @staticmethod
    def _validate_seed(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or not 10000 <= value <= 99999:
            return None
        return value

    @staticmethod
    def _normalize_account_key(value: str) -> str:
        raw = str(value or "").strip()
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
        normalized = normalized.strip("._-")
        if not normalized:
            raise ValueError("account_key must not be empty")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return f"{normalized[:100]}-{digest}"
