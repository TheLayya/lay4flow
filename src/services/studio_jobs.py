"""Bounded Studio batches that continue after the submitting browser leaves."""

import asyncio
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path


class StudioJobError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class StudioGenerationJobs:
    """Persist receipts and results, keeping uploaded input media only in memory.

    Interrupted work is never resubmitted automatically: an upstream request
    may already have consumed quota. Vids keeps its original receipt and key.
    """

    def __init__(self, execute, path, *, refresh=None, max_pending=100, max_bytes=256 * 1024**2):
        self.execute = execute
        self.refresh = refresh
        self.path = Path(path)
        self.max_pending = max_pending
        self.max_bytes = max_bytes
        self._jobs = {}
        self._inputs = {}
        self._bytes = 0
        self._queues = {provider: asyncio.Queue() for provider in ("flow", "vids")}
        self._workers = []
        self._admission = asyncio.Lock()
        self._closed = False
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            for job in saved:
                self._jobs[job["id"]] = job
                for item in job["items"]:
                    if item["status"] in {"queued", "generating"}:
                        self._interrupt(item)
                self._finish(job)
            self._prune()
            self._save()

    @staticmethod
    def _interrupt(item):
        active = item["status"] == "generating"
        item.update(
            status="unknown" if active else "failed",
            message=(
                "服务已重启或停止，原请求结果待确认；请查询原收据或请求日志"
                if active
                else "服务已重启或停止，排队任务未执行；请重新提交"
            ),
            completed_at=int(time.time()),
        )

    def _prune(self):
        finished = sorted(
            (
                job
                for job in self._jobs.values()
                if job["state"] not in {"queued", "running"}
                and not any(
                    item["status"] in {"unknown", "received", "download_failed"}
                    for item in job["items"]
                )
            ),
            key=lambda job: job.get("completed_at", job["created_at"]),
        )
        for index, job in enumerate(finished):
            if (
                job.get("completed_at", job["created_at"]) < time.time() - 86400
                or index < len(finished) - 100
            ):
                del self._jobs[job["id"]]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(list(self._jobs.values()), ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(self.path)

    @staticmethod
    def _public(job):
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def list(self):
        for job in self._jobs.values():
            self._refresh(job)
        return [self._public(job) for job in reversed(self._jobs.values())]

    def get(self, identifier):
        job = self._jobs.get(identifier)
        if job is None:
            raise StudioJobError("生成批次不存在或已过期", 404)
        self._refresh(job)
        return self._public(job)

    def _refresh(self, job):
        if self.refresh is None or job["provider"] != "vids":
            return
        changed = False
        for item in job["items"]:
            if item["status"] not in {"unknown", "received", "download_failed"}:
                continue
            value = self.refresh(item)
            if value and any(item.get(key) != value[key] for key in value):
                item.update(value)
                changed = True
        if changed:
            self._finish(job)
            self._save()

    async def submit(self, provider, body, count, request_id, base_url, *, kind, model, prompt):
        if provider not in self._queues or type(count) is not int or not 1 <= count <= 20:
            raise StudioJobError("请选择生成平台和 1 至 20 个作品")
        if kind not in {"image", "video", "speech"} or kind == "speech" and count != 1:
            raise StudioJobError("图片和视频支持批量生成；语音每次提交 1 个")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise StudioJobError("生成请求标识无效")
        try:
            encoded = json.dumps([provider, body, count], sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            raise StudioJobError("生成请求格式无效") from None
        size = len(encoded.encode("utf-8"))
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        async with self._admission:
            if self._closed:
                raise StudioJobError("生成服务正在停止，请稍后重试", 503)
            self._prune()
            for job in self._jobs.values():
                if job["request_id"] == request_id:
                    if job["_fingerprint"] != fingerprint:
                        raise StudioJobError("同一生成请求标识已用于其他参数", 409)
                    return self._public(job)
            pending = sum(
                item["status"] in {"queued", "generating"}
                for job in self._jobs.values()
                for item in job["items"]
            )
            if (
                pending + count > self.max_pending
                or self._bytes + size > self.max_bytes
                or len(self._jobs) >= 500
            ):
                raise StudioJobError("生成队列或素材容量已满，请稍后提交", 429)
            identifier = "studio_" + uuid.uuid4().hex
            items = []
            for index in range(count):
                key = f"{identifier}:{index}"
                items.append(
                    {
                        "id": key,
                        "key": key,
                        "index": index,
                        "provider": provider,
                        "kind": kind,
                        "model": model,
                        "status": "queued",
                        "message": "已加入后台队列",
                        "text": "",
                        "progressLog": "",
                        "outputs": [],
                        "receipt": "vids_" + hashlib.sha256(key.encode()).hexdigest()
                        if provider == "vids"
                        else "",
                    }
                )
            job = {
                "id": identifier,
                "request_id": request_id,
                "provider": provider,
                "kind": kind,
                "model": model,
                "prompt": prompt,
                "title": prompt[:38],
                "state": "queued",
                "status": "queued",
                "total": count,
                "completed": 0,
                "failed": 0,
                "created_at": int(time.time()),
                "items": items,
                "_fingerprint": fingerprint,
            }
            self._jobs[identifier] = job
            try:
                self._save()
            except OSError:
                del self._jobs[identifier]
                raise StudioJobError("生成任务记录保存失败，请检查数据目录", 503) from None
            # No await separates durable admission from enqueueing. A disconnected
            # client can safely replay request_id without submitting paid work twice.
            self._inputs[identifier] = {
                "body": copy.deepcopy(body),
                "base_url": base_url,
                "bytes": size,
                "remaining": count,
            }
            self._bytes += size
            for item in items:
                self._queues[provider].put_nowait((identifier, item["index"]))
            if not self._workers:
                # Flow's existing account/slot manager controls upstream concurrency.
                # Vids serializes admission so one account can serve a whole batch.
                self._workers = [
                    asyncio.create_task(self._work(name), name=f"studio-{name}-{index}")
                    for name, workers in (("flow", 4), ("vids", 1))
                    for index in range(workers)
                ]
            return self._public(job)

    @staticmethod
    def _finish(job):
        items = job["items"]
        pending = any(item["status"] in {"queued", "generating"} for item in items)
        job["completed"] = sum(item["status"] == "completed" for item in items)
        job["failed"] = sum(
            item["status"] not in {"queued", "generating", "completed"} for item in items
        )
        if pending:
            state = "running" if any(item["status"] != "queued" for item in items) else "queued"
        else:
            state = (
                "completed"
                if job["completed"] == len(items)
                else "partial"
                if job["completed"]
                else "failed"
            )
            job.setdefault("completed_at", int(time.time()))
        job["state"] = job["status"] = state

    async def _work(self, provider):
        queue = self._queues[provider]
        while True:
            identifier, index = await queue.get()
            job = item = inputs = stream = None
            try:
                job = self._jobs[identifier]
                item = job["items"][index]
                inputs = self._inputs[identifier]
                item.update(
                    status="generating", message="正在后台生成", started_at=int(time.time())
                )
                self._finish(job)
                self._save()
                stream = self.execute(provider, inputs["body"], item["key"], inputs["base_url"])
                async for event in stream:
                    if "text_delta" in event:
                        item["text"] = (item["text"] + event.pop("text_delta"))[-65536:]
                    if "progress_delta" in event:
                        item["progressLog"] = (item["progressLog"] + event.pop("progress_delta"))[
                            -16000:
                        ]
                    item.update(event)
                if item["status"] == "generating":
                    item.update(status="failed", message="生成结束但未返回作品")
            except asyncio.CancelledError:
                if item is not None:
                    self._interrupt(item)
                raise
            except Exception as error:
                if item is not None:
                    item.update(
                        status="failed",
                        message=str(error)[:500]
                        if isinstance(error, StudioJobError)
                        else "生成失败，请查看请求日志",
                    )
            finally:
                if stream is not None:
                    try:
                        await stream.aclose()
                    except Exception:
                        pass
                if item is not None and inputs is not None and job is not None:
                    item["completed_at"] = int(time.time())
                    inputs["remaining"] -= 1
                    if inputs["remaining"] == 0:
                        self._bytes -= inputs["bytes"]
                        self._inputs.pop(identifier, None)
                    self._finish(job)
                    try:
                        self._save()
                    except OSError:
                        pass
                queue.task_done()
                job = item = inputs = stream = None

    async def close(self):
        async with self._admission:
            self._closed = True
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
            for job in self._jobs.values():
                for item in job["items"]:
                    if item["status"] == "queued":
                        self._interrupt(item)
                self._finish(job)
            self._inputs.clear()
            self._bytes = 0
            try:
                self._save()
            except OSError:
                # Shutdown must release workers even when the data volume is full.
                pass
