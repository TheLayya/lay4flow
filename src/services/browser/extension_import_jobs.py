"""Durable, owner-scoped extension imports with encrypted pending credentials."""

import asyncio
import hashlib
import inspect
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from .profile_transfer import ProfileTransferError, transfer_error_detail

TERMINAL = {"completed", "failed"}


class ExtensionImportJobs:
    """Commit before acknowledging; never replay an interrupted account mutation."""

    def __init__(
        self,
        root: Path,
        importer: Callable,
        *,
        prepare_payload: Callable | None = None,
        max_pending: int = 100,
        max_history: int = 1000,
        history_seconds: int = 24 * 60 * 60,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "extension-imports.sqlite3"
        self.importer = importer
        self.prepare_payload = prepare_payload
        self.max_pending = max_pending
        self.max_history = max_history
        self.history_seconds = history_seconds
        self._worker = None
        self._closed = False
        self._worker_lock = None
        key_path = self.root / "extension-imports.key"
        try:
            descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(Fernet.generate_key())
                output.flush()
                os.fsync(output.fileno())
        self._cipher = Fernet(key_path.read_bytes())
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS extension_imports (
                id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
                owner_hash TEXT NOT NULL, nonce_hash TEXT NOT NULL,
                origin TEXT NOT NULL, binding_hash TEXT NOT NULL,
                payload BLOB, state TEXT NOT NULL, stage TEXT NOT NULL,
                message TEXT NOT NULL, result TEXT, error TEXT,
                created REAL NOT NULL, started REAL, finished REAL,
                attempts INTEGER NOT NULL DEFAULT 0)"""
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db

    def _prune(self, db):
        db.execute(
            "DELETE FROM extension_imports WHERE state IN ('completed','failed') AND finished < ?",
            (time.time() - self.history_seconds,),
        )
        db.execute(
            "DELETE FROM extension_imports WHERE id IN "
            "(SELECT id FROM extension_imports WHERE state IN ('completed','failed') "
            "ORDER BY finished DESC LIMIT -1 OFFSET ?)",
            (self.max_history,),
        )

    def _public(self, row):
        result = json.loads(row["result"]) if row["result"] else {}
        error = json.loads(row["error"]) if row["error"] else {}
        end = row["finished"] or time.time()
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "state": row["state"],
            "stage": row["stage"],
            "message": row["message"],
            "email": result.get("email", ""),
            "token_id": result.get("token_id"),
            "action": result.get("action", "added"),
            "error": error,
            "created_at": row["created"],
            "elapsed_seconds": round(max(0, end - (row["started"] or row["created"])), 1),
            "attempts": row["attempts"],
            "can_retry": row["state"] == "failed" and row["payload"] is not None,
        }

    def _owner_row(self, db, owner, job_id):
        row = db.execute(
            "SELECT * FROM extension_imports WHERE id=? AND owner_hash=?",
            (job_id, self.digest(owner)),
        ).fetchone()
        if row is None:
            raise ProfileTransferError("extension_job_not_found", "扩展导入任务不存在或已过期", 404)
        return row

    def restore_receipt(self, session_id, nonce, owner, origin, binding):
        """Allow the exact accepted receipt to recover after a server restart."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM extension_imports WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            expected = (
                self.digest(owner),
                self.digest(nonce),
                origin,
                self.digest(json.dumps(binding, separators=(",", ":"))),
            )
            actual = tuple(
                row[key] for key in ("owner_hash", "nonce_hash", "origin", "binding_hash")
            )
            if actual != expected:
                raise ProfileTransferError(
                    "extension_receipt_invalid", "扩展登录凭据或窗口已变化", 403
                )
            return self._public(row)

    def submit(self, owner, session_id, nonce, origin, binding, payload):
        if self._closed:
            raise ProfileTransferError(
                "extension_queue_closed", "导入服务正在停止，请稍后重试", 503
            )
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > 1024 * 1024:
            raise ProfileTransferError("extension_payload_too_large", "扩展登录资料过大", 413)
        encrypted = self._cipher.encrypt(encoded)
        binding_hash = self.digest(json.dumps(binding, separators=(",", ":")))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db)
            prior = db.execute(
                "SELECT * FROM extension_imports WHERE session_id=?", (session_id,)
            ).fetchone()
            if prior is not None:
                if (
                    prior["owner_hash"] != self.digest(owner)
                    or prior["nonce_hash"] != self.digest(nonce)
                    or prior["origin"] != origin
                    or prior["binding_hash"] != binding_hash
                ):
                    raise ProfileTransferError(
                        "extension_receipt_invalid", "扩展登录窗口已变化", 403
                    )
                public = self._public(prior)
            else:
                pending = db.execute(
                    "SELECT COUNT(*) FROM extension_imports WHERE state IN ('queued','running')"
                ).fetchone()[0]
                if pending >= self.max_pending:
                    raise ProfileTransferError(
                        "extension_queue_full", "后台验证队列已满，请稍后重试", 429
                    )
                job_id = secrets.token_urlsafe(18)
                db.execute(
                    "INSERT INTO extension_imports "
                    "(id,session_id,owner_hash,nonce_hash,origin,binding_hash,payload,state,stage,message,created) "
                    "VALUES (?,?,?,?,?,?,?,'queued','queued',?,?)",
                    (
                        job_id,
                        session_id,
                        self.digest(owner),
                        self.digest(nonce),
                        origin,
                        binding_hash,
                        encrypted,
                        "已安全保存登录资料，等待后台验证",
                        time.time(),
                    ),
                )
                public = self._public(
                    db.execute("SELECT * FROM extension_imports WHERE id=?", (job_id,)).fetchone()
                )
        # The connection context committed both ciphertext and job before this
        # method returns a receipt or starts its worker.
        self.start()
        return public

    def list(self, owner):
        with self._connect() as db:
            self._prune(db)
            rows = db.execute(
                "SELECT * FROM extension_imports WHERE owner_hash=? ORDER BY created DESC LIMIT ?",
                (self.digest(owner), self.max_history),
            ).fetchall()
            result = [self._public(row) for row in rows]
        self.start()
        return result

    def get(self, owner, job_id):
        with self._connect() as db:
            public = self._public(self._owner_row(db, owner, job_id))
        self.start()
        return public

    def retry(self, owner, job_id):
        if self._closed:
            raise ProfileTransferError(
                "extension_queue_closed", "导入服务正在停止，请稍后重试", 503
            )
        with self._connect() as db:
            row = self._owner_row(db, owner, job_id)
            if row["state"] == "failed":
                if row["payload"] is None:
                    raise ProfileTransferError(
                        "extension_retry_unavailable", "此任务资料已清理，请重新登录", 409
                    )
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM extension_imports WHERE state IN ('queued','running')"
                    ).fetchone()[0]
                    >= self.max_pending
                ):
                    raise ProfileTransferError(
                        "extension_queue_full", "后台验证队列已满，请稍后重试", 429
                    )
                db.execute(
                    "UPDATE extension_imports SET state='queued',stage='queued',message=?,error=NULL,"
                    "started=NULL,finished=NULL WHERE id=?",
                    ("已重新排队，保留原登录资料", job_id),
                )
                row = self._owner_row(db, owner, job_id)
            public = self._public(row)
        self.start()
        return public

    def start(self):
        if self._closed:
            return
        if self._worker is None or self._worker.done():
            if not self._acquire_worker_lock():
                return
            # Only the process that owns the queue lock may declare an old
            # running record interrupted. Auxiliary readers never alter it.
            with self._connect() as db:
                db.execute(
                    "UPDATE extension_imports SET state='failed',stage='interrupted',"
                    "message=?,finished=? WHERE state='running'",
                    ("验证被服务重启中断，登录资料已保留；请检查账号后重试此任务", time.time()),
                )
            self._worker = asyncio.create_task(self._work(), name="extension-import-worker")

    def _acquire_worker_lock(self):
        if self._worker_lock is not None:
            return True
        handle = (self.root / "extension-import-worker.lock").open("a+b")
        try:
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
        except OSError:
            handle.close()
            return False
        self._worker_lock = handle
        return True

    def _release_worker_lock(self):
        handle, self._worker_lock = self._worker_lock, None
        if handle is not None:
            handle.close()

    async def _work(self):
        try:
            await self._work_owned()
        finally:
            self._release_worker_lock()

    async def _work_owned(self):
        while not self._closed:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM extension_imports WHERE state='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row is None:
                    return
                db.execute(
                    "UPDATE extension_imports SET state='running',stage='verifying',message=?,"
                    "started=?,attempts=attempts+1 WHERE id=?",
                    ("正在验证账号身份并保存 Profile", time.time(), row["id"]),
                )
            try:
                payload = json.loads(self._cipher.decrypt(row["payload"]))
                if self.prepare_payload is not None and "_extension_proxy_binding" not in payload:
                    payload = await self.prepare_payload(payload)
                    # Freeze the selected exit before any Google/Profile call.
                    # Retry decrypts this exact snapshot instead of allocating
                    # an exit again after other accounts changed pool usage.
                    encrypted = self._cipher.encrypt(
                        json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
                    )
                    with self._connect() as db:
                        db.execute(
                            "UPDATE extension_imports SET payload=? WHERE id=? AND state='running'",
                            (encrypted, row["id"]),
                        )

                async def save_original_binding(binding):
                    updated = {**payload, "_extension_proxy_binding": dict(binding)}
                    encrypted = self._cipher.encrypt(
                        json.dumps(updated, separators=(",", ":"), allow_nan=False).encode()
                    )
                    with self._connect() as db:
                        changed = db.execute(
                            "UPDATE extension_imports SET payload=? WHERE id=? AND state='running'",
                            (encrypted, row["id"]),
                        ).rowcount
                    if changed != 1:
                        raise ProfileTransferError(
                            "extension_task_changed", "导入任务已变化，登录资料已保留", 409
                        )

                options = {}
                if "on_proxy_binding" in inspect.signature(self.importer).parameters:
                    options["on_proxy_binding"] = save_original_binding
                result = await asyncio.wait_for(self.importer(payload, **options), timeout=240)
                if (
                    not result.get("success")
                    or type(result.get("token_id")) is not int
                    or result["token_id"] < 1
                ):
                    raise ProfileTransferError("extension_unverified", "账号验证未完成", 422)
                public_result = {
                    "email": result.get("email", ""),
                    "token_id": result["token_id"],
                    "action": result.get("action", "added"),
                }
                with self._connect() as db:
                    db.execute(
                        "UPDATE extension_imports SET state='completed',stage='completed',message=?,"
                        "result=?,payload=NULL,error=NULL,finished=? WHERE id=?",
                        ("账号已验证并保存", json.dumps(public_result), time.time(), row["id"]),
                    )
            except asyncio.CancelledError:
                self._fail(row["id"], None)
                raise
            except Exception as error:
                self._fail(row["id"], error)

    def _fail(self, job_id, error):
        if error is None:
            detail = {
                "error_code": "verification_interrupted",
                "message": "验证被服务停止中断，资料已保留，可重试此任务",
            }
        else:
            _, detail = transfer_error_detail(error)
        with self._connect() as db:
            db.execute(
                "UPDATE extension_imports SET state='failed',stage=?,message=?,error=?,finished=? WHERE id=?",
                (
                    detail.get("stage", "failed"),
                    detail["message"],
                    json.dumps(detail),
                    time.time(),
                    job_id,
                ),
            )

    @property
    def pending_count(self):
        with self._connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM extension_imports WHERE state IN ('queued','running')"
            ).fetchone()[0]

    async def close(self):
        self._closed = True
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._release_worker_lock()


def get_extension_import_jobs(token_manager):
    """Share one durable worker across startup, status endpoints and admission."""
    jobs = getattr(token_manager, "_extension_import_jobs", None)
    if jobs is None:
        from .extension_login import ExtensionLogin

        login = ExtensionLogin(token_manager)
        jobs = ExtensionImportJobs(
            Path(token_manager.db.db_path).resolve().parent,
            login.import_prepared,
            prepare_payload=login.prepare_import,
        )
        token_manager._extension_import_jobs = jobs
    return jobs
