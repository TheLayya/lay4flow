"""Expire completed Vids output files without removing their durable receipts."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import stat
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse

_RECEIPT = re.compile(r"vids_[a-f0-9]{64}")
_MEDIA = {
    "image": (".jpg", "image/jpeg"),
    "video": (".mp4", "video/mp4"),
    "speech": (".wav", "audio/wav"),
    "music": (".wav", "audio/wav"),
}
_MAX_RECEIPT_BYTES = 16 * 1024**2


class _MediaAccess:
    def __init__(self):
        self.lock = threading.Lock()
        self.readers = 0
        self.writer = False


class MediaBusyError(RuntimeError):
    pass


_ACCESS = weakref.WeakValueDictionary()
_ACCESS_LOCK = threading.Lock()


def media_access(root):
    """Share leases across gateway replacement and unavailable-session cleanup."""
    key = os.path.normcase(str(Path(root).absolute()))
    with _ACCESS_LOCK:
        value = _ACCESS.get(key)
        if value is None:
            value = _ACCESS[key] = _MediaAccess()
        return value


@contextmanager
def media_lease(path):
    access = media_access(Path(path).parent)
    with access.lock:
        if access.writer:
            raise MediaBusyError("Media cleanup is active")
        access.readers += 1
    try:
        yield
    finally:
        with access.lock:
            access.readers -= 1


@asynccontextmanager
async def _async_media_lease(path):
    # Waiting for a filesystem worker must yield the event loop. The guard
    # protects counters only, never directory scanning or deletion itself.
    while True:
        lease = media_lease(path)
        try:
            lease.__enter__()
            break
        except MediaBusyError:
            await asyncio.sleep(0.01)
    try:
        yield
    finally:
        lease.__exit__(None, None, None)


class VidsFileResponse(FileResponse):
    """Keep a completed output leased until its ASGI transfer is finished."""

    async def __call__(self, scope, receive, send):
        async with _async_media_lease(self.path):
            # A response may be queued after content() resolves. Report expiry
            # factually if cleanup ran before the ASGI transfer acquired a lease.
            path = Path(self.path)
            if path.is_symlink() or not path.is_file():
                response = JSONResponse(
                    {
                        "error": {
                            "message": "Generation content has expired",
                            "type": "invalid_request_error",
                            "code": "expired",
                        }
                    },
                    status_code=410,
                )
                return await response(scope, receive, send)
            # Keep the lease until actual chunks are sent. A server path-send
            # extension can defer opening the file until after this call returns.
            scope = {**scope, "extensions": dict(scope.get("extensions", {}))}
            scope["extensions"].pop("http.response.pathsend", None)
            return await super().__call__(scope, receive, send)


def _plain_file(path):
    value = path.lstat()
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError("Unsafe media path")
    return value


def _plain_root(root):
    root = Path(root).absolute()
    for item in (root, *root.parents):
        value = item.lstat()
        if (
            not stat.S_ISDIR(value.st_mode)
            or getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("Unsafe media root")
    return root


def _atomic_receipt(path, record):
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def prune_completed_media(root, timeout, *, expected_identity, now=None, timeout_current=None):
    """Caller owns the runtime/gateway lock; no provider or browser is touched."""
    result = {"scanned": 0, "expired": 0, "files_removed": 0, "bytes_reclaimed": 0, "skipped": 0}
    if type(timeout) not in (int, float) or timeout <= 0 or not math.isfinite(timeout):
        return result
    now = time.time() if now is None else now
    try:
        root = _plain_root(root)
        pin = root / ".account"
        if _plain_file(pin).st_size != 64 or pin.read_text(encoding="ascii") != expected_identity:
            return {**result, "skipped": 1}
    except (OSError, UnicodeError, ValueError):
        return {**result, "skipped": 1}
    access = media_access(root)
    # Reserve a writer under a short guard, then release the guard before disk
    # I/O. Responses wait asynchronously; sync image readers get a retryable busy
    # error. Neither can block the event loop behind a slow filesystem worker.
    with access.lock:
        if access.readers or access.writer:
            return {**result, "skipped": 1}
        access.writer = True
    try:
        for path in root.glob("vids_*.json"):
            if timeout_current is not None and timeout_current() != timeout:
                break
            result["scanned"] += 1
            try:
                if (
                    not _RECEIPT.fullmatch(path.stem)
                    or _plain_file(path).st_size > _MAX_RECEIPT_BYTES
                ):
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(record, dict)
                    or record.get("id") != path.stem
                    or record.get("status") != "completed"
                    or record.get("operation") == "insert"
                    or record.get("kind") not in _MEDIA
                ):
                    continue
                suffix, mime = _MEDIA[record["kind"]]
                if record.get("suffix") != suffix or record.get("mime_type") != mime:
                    continue
                outputs = record.get("outputs")
                if outputs is None:
                    indices = [0]
                elif (
                    record["kind"] == "image"
                    and isinstance(outputs, list)
                    and 1 <= len(outputs) <= 4
                    and all(isinstance(v, dict) and type(v.get("index")) is int for v in outputs)
                    and [v["index"] for v in outputs] == list(range(len(outputs)))
                ):
                    indices = list(range(len(outputs)))
                else:
                    continue
                files = [
                    root / (path.stem + (f"_{index}" if index else "") + suffix)
                    for index in indices
                ]
                entries = []
                for file in files:
                    try:
                        entries.append((file, _plain_file(file)))
                    except FileNotFoundError:
                        continue
                if not entries:
                    continue
                checkpoint = record.get(
                    "media_cached_at",
                    record.get("completed_at", record.get("updated_at", record.get("created_at"))),
                )
                if type(checkpoint) not in (int, float) or not math.isfinite(checkpoint):
                    continue
                if max([checkpoint, *(info.st_mtime for _, info in entries)]) > now - timeout:
                    continue
                if timeout_current is not None and timeout_current() != timeout:
                    break
                # Write expiry before deleting. A process interruption can leave
                # an extra file, never a missing receipt or a replayable paid key.
                if not record.get("media_expired"):
                    record["media_expired"] = True
                    record["media_expired_at"] = now
                    _atomic_receipt(path, record)
                for file, info in entries:
                    if timeout_current is not None and timeout_current() != timeout:
                        break
                    current = _plain_file(file)
                    if (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns) != (
                        info.st_ino,
                        info.st_dev,
                        info.st_size,
                        info.st_mtime_ns,
                    ):
                        continue
                    file.unlink()
                    result["files_removed"] += 1
                    result["bytes_reclaimed"] += info.st_size
                result["expired"] += 1
            except (OSError, ValueError, TypeError, KeyError, UnicodeError):
                result["skipped"] += 1
    finally:
        with access.lock:
            access.writer = False
    return result


class _RuntimeLock:
    """Nonblocking ownership for a declared binding not held by a live gateway."""

    def __init__(self, root):
        self.path = root / ".lock"
        self.handle = None

    def acquire(self):
        try:
            _plain_file(self.path)
            self.handle = self.path.open("r+b")
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ValueError):
            self.release()
            return False

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class VidsMediaCleanup:
    def __init__(self, runtime, get_timeout, *, get_bindings=None, interval=60):
        self.runtime, self.get_timeout, self.interval = runtime, get_timeout, interval
        self.get_bindings = get_bindings
        self._task = None
        self._closed = False
        self._tick_lock = asyncio.Lock()
        self._stats = {
            "last_run_at": None,
            "scanned": 0,
            "expired": 0,
            "files_removed": 0,
            "bytes_reclaimed": 0,
            "skipped": 0,
        }

    def start(self):
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
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass
                raise

    def public_stats(self):
        return dict(self._stats)

    async def _binding_rows(self):
        """Declared bindings remain available when upstream startup has failed.

        Runtime bindings are authoritative. Conflicting or overlapping saved
        declarations are excluded rather than choosing an arbitrary owner.
        """
        current = dict(self.runtime.binding_rows)
        if self.get_bindings is None:
            return current
        try:
            rows = self.get_bindings()
            if inspect.isawaitable(rows):
                rows = await rows
        except (OSError, ValueError, TypeError):
            return current
        if not isinstance(rows, (list, tuple)):
            return current
        saved, conflicts = {}, set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            token_id, identity, directory = (
                row.get(key) for key in ("token_id", "identity_hash", "runtime_dir")
            )
            if (
                type(token_id) is not int
                or token_id <= 0
                or token_id in current
                or not isinstance(identity, str)
                or not re.fullmatch(r"[a-f0-9]{64}", identity)
                or not isinstance(directory, str)
                or not directory
                or not Path(directory).is_absolute()
                or ".." in Path(directory).parts
            ):
                continue
            candidate = {"token_id": token_id, "identity_hash": identity, "runtime_dir": directory}
            if token_id in saved and candidate != saved[token_id]:
                conflicts.add(token_id)
            saved[token_id] = candidate
        roots = {token_id: Path(row["runtime_dir"]).absolute() for token_id, row in saved.items()}
        occupied = [
            Path(row["runtime_dir"]).absolute()
            for row in current.values()
            if isinstance(row, dict) and isinstance(row.get("runtime_dir"), str)
        ]
        occupied.extend(
            account.gateway.root for account in self.runtime.dispatcher._accounts.values()
        )
        for token_id, root in roots.items():
            if any(
                root == other or root in other.parents or other in root.parents
                for other in occupied
            ):
                conflicts.add(token_id)
            for other_id, other in roots.items():
                if token_id != other_id and (
                    root == other or root in other.parents or other in root.parents
                ):
                    conflicts.update((token_id, other_id))
        current.update(
            {token_id: row for token_id, row in saved.items() if token_id not in conflicts}
        )
        return current

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.interval)

    async def _prune(self, root, identity, timeout):
        work = asyncio.create_task(
            asyncio.to_thread(
                prune_completed_media,
                root,
                timeout,
                expected_identity=identity,
                timeout_current=None
                if inspect.iscoroutinefunction(self.get_timeout)
                else self.get_timeout,
            )
        )
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    pass
            work.result()
            raise

    async def tick(self):
        if self._closed or self._tick_lock.locked():
            return
        async with self._tick_lock:
            timeout = self.get_timeout()
            if inspect.isawaitable(timeout):
                timeout = await timeout
            if type(timeout) not in (int, float) or timeout <= 0 or not math.isfinite(timeout):
                return
            runtime = self.runtime
            if runtime._lifecycle_lock.locked():
                return
            dispatcher = runtime.dispatcher
            total = dict.fromkeys(
                ("scanned", "expired", "files_removed", "bytes_reclaimed", "skipped"), 0
            )
            # Retain the lifecycle reservation until filesystem work has drained;
            # account deletion/replacement cannot race a worker using its root.
            async with runtime._lifecycle_lock:
                bindings = await self._binding_rows()
                for token_id in sorted(set(bindings) | set(dispatcher._accounts)):
                    preflight = dispatcher._preflight_locks.get(token_id)
                    account = dispatcher._accounts.get(token_id)
                    if (
                        token_id in dispatcher._pool_pending
                        or preflight is not None
                        and preflight.locked()
                    ):
                        total["skipped"] += 1
                        continue
                    if account is not None:
                        gateway = account.gateway
                        binding = bindings.get(token_id)
                        if binding is not None:
                            try:
                                if _plain_root(binding["runtime_dir"]) != gateway.root:
                                    total["skipped"] += 1
                                    continue
                            except (OSError, ValueError, KeyError):
                                total["skipped"] += 1
                                continue
                        if gateway.lock.locked():
                            total["skipped"] += 1
                            continue
                        async with gateway.lock:
                            row = await self._prune(gateway.root, account.identity_hash, timeout)
                    else:
                        binding = bindings[token_id]
                        try:
                            root = _plain_root(binding["runtime_dir"])
                        except (OSError, ValueError, KeyError):
                            total["skipped"] += 1
                            continue
                        lock = _RuntimeLock(root)
                        if not lock.acquire():
                            total["skipped"] += 1
                            continue
                        try:
                            row = await self._prune(root, binding.get("identity_hash"), timeout)
                        finally:
                            lock.release()
                    for key in total:
                        total[key] += row[key]
            self._stats = {"last_run_at": time.time(), **total}
