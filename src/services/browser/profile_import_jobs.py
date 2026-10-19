"""Process-local account imports that outlive the submitting HTTP connection."""

import asyncio
import hashlib
import json
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from .profile_transfer import (
    FORMAT,
    TRANSFER_STAGES,
    ProfileTransfer,
    ProfileTransferError,
    transfer_error_detail,
)

TERMINAL = {"completed", "failed", "cancelled"}
ITEM_STAGES = {
    "queued": "等待处理",
    "legacy_import": "验证并导入账号",
    **TRANSFER_STAGES,
    "completed": "导入完成",
    "cancelled": "服务已停止，请重新提交导入",
}


class ProfileImportJobs:
    """One worker shares ProfileTransfer's locks; only bounded input stays in RAM."""

    def __init__(
        self,
        transfer: ProfileTransfer,
        legacy_importer: Callable | None = None,
        *,
        max_pending: int = 500,
        max_job_bytes: int = 32 * 1024 * 1024,
        max_input_bytes: int = 64 * 1024 * 1024,
        max_history: int = 100,
        history_seconds: int = 24 * 60 * 60,
    ):
        self.transfer = transfer
        self.legacy_importer = legacy_importer
        self.max_pending = max_pending
        self.max_job_bytes = max_job_bytes
        self.max_input_bytes = max_input_bytes
        self.max_history = max_history
        self.history_seconds = history_seconds
        self._jobs: dict[str, dict[str, Any]] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._queue = deque()
        self._worker: asyncio.Task | None = None
        self._expiry: asyncio.TimerHandle | None = None
        self._input_bytes = 0
        self._closed = False

    @staticmethod
    def _owner(owner: str) -> str:
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def _discard_input(self, job, index):
        if job["_inputs"][index] is not None:
            self._input_bytes -= job["_sizes"][index]
            job["_inputs"][index] = None

    def _prune(self):
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        now = time.monotonic()
        terminal = [job for job in self._jobs.values() if job["state"] in TERMINAL]
        terminal.sort(key=lambda job: job["_finished"])
        for index, job in enumerate(terminal):
            if (
                now - job["_finished"] < self.history_seconds
                and len(terminal) - index <= self.max_history
            ):
                continue
            for item_index in range(job["total"]):
                self._discard_input(job, item_index)
            self._requests.pop((job["_owner"], job["request_id"]), None)
            self._jobs.pop(job["id"], None)
        retained = [job for job in terminal if job["id"] in self._jobs]
        if retained and not self._closed:
            delay = max(0, retained[0]["_finished"] + self.history_seconds - now)
            self._expiry = asyncio.get_running_loop().call_later(delay, self._prune)

    def _public(self, job):
        end = job.get("_finished", time.monotonic())
        started = job.get("_started", job["_created"])
        items = []
        for item in job["items"]:
            row = {key: value for key, value in item.items() if not key.startswith("_")}
            if item["state"] == "running":
                row["elapsed_seconds"] = round(time.monotonic() - item["_started"], 1)
            items.append(row)
        return {
            "id": job["id"],
            "request_id": job["request_id"],
            "state": job["state"],
            "items": items,
            "added": sum(item.get("added", 0) for item in job["items"]),
            "updated": sum(item.get("updated", 0) for item in job["items"]),
            "failed": sum(item["state"] == "failed" for item in job["items"]),
            "total": job["total"],
            "current": job["current"],
            "created_at": job["created_at"],
            "elapsed_seconds": round(max(0, end - started), 1),
            "can_retry": not self._closed
            and job["state"] in TERMINAL
            and any(
                item["state"] == "failed" and job["_inputs"][index] is not None
                for index, item in enumerate(job["items"])
            ),
        }

    def _pending(self):
        return sum(
            item["state"] in {"queued", "running"}
            for job in self._jobs.values()
            for item in job["items"]
        )

    def _schedule(self, job):
        self._queue.append(job["id"])
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="profile-import-worker")

    def submit(
        self,
        owner: str,
        bundles: list[dict[str, Any]],
        request_id: str,
        *,
        proxy_pool_id: int | None = None,
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        self._prune()
        if self._closed:
            raise ProfileTransferError("import_worker_closed", "导入服务正在停止，请稍后重试", 503)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ProfileTransferError("invalid_request_id", "导入请求标识无效")
        if not isinstance(bundles, list) or not 1 <= len(bundles) <= 500:
            raise ProfileTransferError("invalid_import_count", "每次请选择 1 至 500 个账号")
        try:
            encoded = [
                json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
                for value in bundles
            ]
            if any(not isinstance(value, dict) for value in bundles):
                raise ValueError
            sizes = [len(value.encode("utf-8")) for value in encoded]
            fingerprint = hashlib.sha256(
                json.dumps([encoded, proxy_pool_id, proxy_url], ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        except (ValueError, TypeError):
            raise ProfileTransferError("invalid_bundle", "导入 JSON 无效") from None
        owner_hash = self._owner(owner)
        duplicate = self._requests.get((owner_hash, request_id))
        if duplicate:
            job = self._jobs[duplicate]
            if job["_fingerprint"] != fingerprint:
                raise ProfileTransferError(
                    "import_request_conflict", "同一导入请求标识已用于其他内容，请重新提交", 409
                )
            return self._public(job)
        if sum(sizes) > self.max_job_bytes or self._input_bytes + sum(sizes) > self.max_input_bytes:
            raise ProfileTransferError(
                "import_input_too_large", "导入内容超过容量，请分批提交或等待现有任务完成", 413
            )
        if self._pending() + len(bundles) > self.max_pending:
            raise ProfileTransferError("import_queue_full", "导入队列已满，请稍后再提交", 429)
        items = []
        for value in bundles:
            account = value.get("account") if value.get("format") == FORMAT else value
            email = account.get("email") if isinstance(account, dict) else None
            items.append(
                {
                    "email": email.strip()[:320] if isinstance(email, str) else "未知账号",
                    "state": "queued",
                    "stage": "queued",
                    "message": ITEM_STAGES["queued"],
                    "elapsed_seconds": 0,
                }
            )
        job = {
            "id": uuid.uuid4().hex,
            "request_id": request_id,
            "state": "queued",
            "items": items,
            "total": len(items),
            "current": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "_created": time.monotonic(),
            "_owner": owner_hash,
            "_fingerprint": fingerprint,
            "_inputs": encoded,
            "_sizes": sizes,
            "_proxy_pool_id": proxy_pool_id,
            "_proxy_url": proxy_url,
        }
        self._jobs[job["id"]] = job
        self._requests[(owner_hash, request_id)] = job["id"]
        self._input_bytes += sum(sizes)
        self._schedule(job)
        return self._public(job)

    def list(self, owner: str) -> list[dict[str, Any]]:
        self._prune()
        owner_hash = self._owner(owner)
        return [
            self._public(job)
            for job in reversed(self._jobs.values())
            if job["_owner"] == owner_hash
        ]

    def _get(self, owner, job_id):
        self._prune()
        job = self._jobs.get(job_id)
        if job is None or job["_owner"] != self._owner(owner):
            raise ProfileTransferError("import_job_not_found", "导入任务不存在或已过期", 404)
        return job

    def get(self, owner: str, job_id: str) -> dict[str, Any]:
        return self._public(self._get(owner, job_id))

    def retry(self, owner: str, job_id: str) -> dict[str, Any]:
        job = self._get(owner, job_id)
        if self._closed:
            raise ProfileTransferError("import_worker_closed", "导入服务正在停止，请稍后重试", 503)
        if job["state"] not in TERMINAL:
            return self._public(job)
        failed = [
            index
            for index, item in enumerate(job["items"])
            if item["state"] == "failed" and job["_inputs"][index] is not None
        ]
        if not failed:
            raise ProfileTransferError("import_retry_empty", "此任务没有待重试的失败项", 409)
        if self._pending() + len(failed) > self.max_pending:
            raise ProfileTransferError("import_queue_full", "导入队列已满，请稍后再提交", 429)
        for index in failed:
            item = job["items"][index]
            job["items"][index] = {
                "email": item["email"],
                "state": "queued",
                "stage": "queued",
                "message": ITEM_STAGES["queued"],
                "elapsed_seconds": 0,
            }
        job.update(state="queued", current=0)
        job.pop("_finished", None)
        job.pop("_started", None)
        self._schedule(job)
        return self._public(job)

    async def _work(self):
        try:
            while self._queue:
                job = self._jobs[self._queue.popleft()]
                job.update(state="running", _started=time.monotonic())
                for index, item in enumerate(job["items"]):
                    if item["state"] != "queued":
                        continue
                    job["current"] = index + 1
                    item.update(state="running", _started=time.monotonic())

                    def progress(stage):
                        if stage in ITEM_STAGES:
                            item.update(stage=stage, message=ITEM_STAGES[stage])

                    try:
                        value = json.loads(job["_inputs"][index])
                        kwargs = {
                            "proxy_pool_id": job["_proxy_pool_id"],
                            "proxy_url": job["_proxy_url"],
                        }
                        if value.get("format") == FORMAT:
                            result = await self.transfer.import_bundle(
                                value, **kwargs, on_stage=progress
                            )
                            added, updated = 1, 0
                        elif self.legacy_importer is not None:
                            progress("legacy_import")
                            result = await self.legacy_importer(value, **kwargs)
                            added, updated = result.get("added", 0), result.get("updated", 0)
                        else:
                            raise ProfileTransferError(
                                "unsupported_format", "请选择 Cloak Profile 登录态迁移包"
                            )
                        if not result.get("success"):
                            raise ProfileTransferError(
                                "account_import_failed", "账号导入未完成", 422
                            )
                        item.update(
                            state="succeeded",
                            stage="completed",
                            message=ITEM_STAGES["completed"],
                            added=added,
                            updated=updated,
                        )
                        if isinstance(result.get("token_id"), int):
                            item["token_id"] = result["token_id"]
                        self._discard_input(job, index)
                    except Exception as error:
                        _, detail = transfer_error_detail(error)
                        item.update(state="failed", **detail)
                        if detail.get("requires_account_action"):
                            self._discard_input(job, index)
                    finally:
                        item["elapsed_seconds"] = round(time.monotonic() - item["_started"], 1)
                job["state"] = (
                    "failed"
                    if any(item["state"] == "failed" for item in job["items"])
                    else "completed"
                )
                job["_finished"] = time.monotonic()
                if not any(value is not None for value in job["_inputs"]):
                    job["_proxy_url"] = None
                self._prune()
        finally:
            self._worker = None

    async def close(self) -> None:
        self._closed = True
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        self._queue.clear()
        for job in self._jobs.values():
            if job["state"] not in TERMINAL:
                job.update(state="cancelled", _finished=time.monotonic())
                for item in job["items"]:
                    if item["state"] in {"queued", "running"}:
                        item.update(
                            state="cancelled", stage="cancelled", message=ITEM_STAGES["cancelled"]
                        )
            for index in range(job["total"]):
                self._discard_input(job, index)
            job["_proxy_url"] = None
