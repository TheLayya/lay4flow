"""Cookie-authenticated Studio access to the server-owned Flow gateway."""

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError

from ...core.models import ChatCompletionRequest
from ...services.studio_jobs import StudioGenerationJobs, StudioJobError
from ...services.studio_media import StudioMediaArchive, StudioMediaError
from ...services.vids.dispatcher import VidsPoolDispatcher
from ...services.vids.gateway import GatewayError, VidsGateway
from .. import routes
from ..admin_schemas import ArchiveMediaRequest
from ..openai_common import APIError, read_json_body, safe_error
from .profile_browser import _same_origin


async def _archive_stream_line(line: str, archive: StudioMediaArchive) -> str:
    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
        return line
    try:
        payload = json.loads(line[5:].strip())
    except (ValueError, TypeError):
        return line
    replacements = {}

    async def replace_url(value):
        if not isinstance(value, str):
            return value
        if value in replacements:
            return replacements[value]
        try:
            saved = await archive.archive(value, local_only=True)
            replacements[value] = saved["url"]
            return saved["url"]
        except Exception:
            return value

    async def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"url", "content_url"} and isinstance(item, str):
                    value[key] = await replace_url(item)
                elif key == "content" and isinstance(item, str):
                    for match in re.finditer(
                        r"!\[[^\]]*\]\(([^)]+)\)|<video[^>]+src=['\"]([^'\"]+)['\"]", item
                    ):
                        original = match.group(1) or match.group(2)
                        item = item.replace(original, await replace_url(original))
                    value[key] = item
                else:
                    await walk(item)
        elif isinstance(value, list):
            for item in value:
                await walk(item)

    await walk(payload)
    return (
        "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if replacements
        else line
    )


async def _archive_stream(body, archive: StudioMediaArchive):
    buffer = ""
    async for chunk in body:
        buffer += chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield await _archive_stream_line(line, archive) + "\n"
    if buffer:
        yield await _archive_stream_line(buffer, archive)


def create_studio_router(verify_token: Callable, get_vids=None):
    """Build the admin-only Studio bridge without exposing the configured API key."""
    router = APIRouter(
        prefix="/api/admin/studio",
        tags=["Studio console"],
        dependencies=[Depends(verify_token)],
    )
    archive_instance = None

    def vids_pool():
        if get_vids is None:
            raise StudioJobError("Vids 服务尚未就绪", 503)
        return VidsPoolDispatcher(get_vids())

    def vids_receipt(pool, identifier):
        result = VidsGateway.public(pool.get(0, identifier))
        prefix = "/api/admin/vids"
        result["receipt_url"] = prefix + "/v1/vids/generations/" + identifier
        result["retry_download_url"] = (
            result["receipt_url"] + "/download"
            if result["status"] in {"received", "download_failed"}
            else None
        )
        if result.get("content_url"):
            result["content_url"] = prefix + result["content_url"]
        for output in result.get("outputs", []):
            if output.get("content_url"):
                output["content_url"] = prefix + output["content_url"]
        return result

    def refreshed_receipt(item):
        try:
            receipt = vids_receipt(vids_pool(), item["receipt"])
        except (GatewayError, StudioJobError):
            return None
        return {
            "status": receipt["status"],
            "vids_receipt": receipt,
            "message": "作品已生成"
            if receipt["status"] == "completed"
            else receipt["error"] or receipt["status"],
        }

    async def execute(provider, body, key, base_url):
        if provider == "vids":
            pool = vids_pool()
            # A busy account has not submitted upstream work. Keep the same key
            # and claim while waiting, including when public callers share it.
            while True:
                try:
                    result = await pool.generate(0, body, key)
                    break
                except GatewayError as error:
                    if error.code != "busy":
                        raise StudioJobError(safe_error(str(error))) from None
                    yield {"message": "账号正在执行其他任务，等待可用额度槽位"}
                    await asyncio.sleep(2)
            receipt = vids_receipt(pool, result["id"])
            yield {
                "status": receipt["status"],
                "vids_receipt": receipt,
                "receipt": receipt["id"],
                "message": "作品已生成"
                if receipt["status"] == "completed"
                else receipt["error"] or receipt["status"],
            }
            return
        try:
            normalized = await routes._normalize_openai_request(
                ChatCompletionRequest.model_validate(body)
            )
        except HTTPException as error:
            raise StudioJobError(safe_error(str(error.detail))) from None
        if not normalized.prompt:
            raise StudioJobError("请先填写提示词")
        archive = get_archive()
        outputs, text = [], ""
        stream = routes._iterate_openai_stream(normalized, base_url)
        try:
            async for chunk in stream:
                for line in chunk.splitlines():
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                        continue
                    line = await _archive_stream_line(line, archive)
                    payload = json.loads(line[5:].strip())
                    retry_trace = payload.get("retry_trace")
                    if isinstance(retry_trace, dict):
                        yield {"retry_trace": retry_trace}
                    if payload.get("error"):
                        raise StudioJobError(
                            safe_error(payload["error"].get("message", "生成失败"))
                        )
                    choices = payload.get("choices") or []
                    delta = choices[0].get("delta", {}) if choices else {}
                    content, progress = delta.get("content", ""), delta.get("reasoning_content", "")
                    text = (text + content)[-65536:]
                    if content or progress:
                        yield {
                            "text_delta": content,
                            "progress_delta": progress,
                            "message": safe_error((progress or content).strip())[-180:],
                        }
                    media = payload.get("media") or {}
                    if (
                        media.get("type") in {"image", "video"}
                        and media.get("url")
                        and len(outputs) < 20
                    ):
                        outputs.append(
                            {"kind": media["type"], "url": media["url"], "sourceURL": media["url"]}
                        )
            for match in re.finditer(
                r"!\[[^\]]*\]\(([^)]+)\)|<video[^>]+src=['\"]([^'\"]+)['\"]", text
            ):
                url = match.group(1) or match.group(2)
                if len(outputs) < 20 and not any(output["url"] == url for output in outputs):
                    outputs.append(
                        {
                            "kind": "image" if match.group(1) else "video",
                            "url": url,
                            "sourceURL": url,
                        }
                    )
            if not outputs:
                raise StudioJobError("生成结束但未返回作品")
            yield {"status": "completed", "outputs": outputs, "message": "作品已生成"}
        finally:
            await stream.aclose()

    def jobs(request):
        service = getattr(request.app.state, "studio_jobs", None)
        if service is None:
            handler = routes.generation_handler
            if handler is None:
                raise HTTPException(503, "创作服务正在初始化")
            service = StudioGenerationJobs(
                execute,
                Path(handler.db.db_path).resolve().parent / "studio-jobs.json",
                refresh=refreshed_receipt,
            )
            request.app.state.studio_jobs = service
        return service

    @router.post("/jobs")
    async def submit_jobs(request: Request):
        if not _same_origin(request):
            raise HTTPException(403, "创作请求必须来自当前管理页")
        try:
            payload = await read_json_body(request, 145 * 1024**2)
        except APIError as error:
            raise HTTPException(error.status_code, error.message) from None
        if set(payload) - {"provider", "body", "count", "request_id"}:
            raise HTTPException(400, "生成批次包含未知参数")
        body, provider = payload.get("body"), payload.get("provider")
        if not isinstance(body, dict):
            raise HTTPException(400, "生成请求格式无效")
        if provider == "flow":
            try:
                parsed = ChatCompletionRequest.model_validate(body)
            except ValidationError:
                raise HTTPException(422, "请选择模型并填写有效生成参数") from None
            config = body.get("generationConfig") or {}
            kind = "video" if config.get("videoConfig") else "image"
            model = parsed.model
            prompt = "\n".join(
                message.content
                if isinstance(message.content, str)
                else "\n".join(
                    item.get("text", "")
                    for item in message.content or []
                    if isinstance(item, dict) and item.get("type") == "text"
                )
                for message in parsed.messages or []
            )
        elif provider == "vids":
            kind, prompt = body.get("kind"), body.get("prompt")
            model = "vids-" + kind if isinstance(kind, str) else ""
            if kind == "image" and body.get("n", 1) != 1:
                raise HTTPException(400, "请使用批次数量；单项 Vids 图片数量固定为 1")
        else:
            raise HTTPException(400, "请选择 Flow 或 Vids 平台")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            raise HTTPException(400, "提示词需要 1 至 32000 个字符")
        try:
            job = await jobs(request).submit(
                provider,
                body,
                payload.get("count", 1),
                payload.get("request_id"),
                routes._get_request_base_url(request),
                kind=kind,
                model=model,
                prompt=prompt,
            )
            return JSONResponse(
                {"success": True, "job": job},
                status_code=202,
                headers={"Cache-Control": "no-store"},
            )
        except StudioJobError as error:
            raise HTTPException(error.status, str(error)) from None

    @router.get("/jobs")
    async def list_jobs(request: Request):
        return JSONResponse(
            {"success": True, "jobs": jobs(request).list()}, headers={"Cache-Control": "no-store"}
        )

    @router.get("/jobs/{identifier}")
    async def get_job(identifier: str, request: Request):
        try:
            return JSONResponse(
                {"success": True, "job": jobs(request).get(identifier)},
                headers={"Cache-Control": "no-store"},
            )
        except StudioJobError as error:
            raise HTTPException(error.status, str(error)) from None

    def get_archive():
        nonlocal archive_instance
        handler = routes.generation_handler
        if handler is None:
            raise HTTPException(status_code=503, detail="创作服务正在初始化")
        if archive_instance is None or archive_instance.handler is not handler:
            archive_instance = StudioMediaArchive(handler)
        return archive_instance

    @router.post("/media/archive")
    async def archive_media(payload: ArchiveMediaRequest, request: Request):
        if not _same_origin(request):
            raise HTTPException(status_code=403, detail="作品保存请求必须来自当前管理页")
        try:
            return await get_archive().archive(payload.url)
        except StudioMediaError as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from None

    @router.get("/media/{identifier}/content")
    async def archived_content(identifier: str):
        try:
            path, mime = get_archive().content(identifier)
            return FileResponse(
                path,
                media_type=mime,
                filename=path.name,
                headers={"Cache-Control": "private, max-age=3600"},
            )
        except StudioMediaError as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from None

    @router.get("/models")
    async def studio_models(request: Request):
        return await routes.list_models("admin-session", raw_request=request)

    @router.post("/chat/completions")
    async def studio_chat(
        payload: ChatCompletionRequest,
        request: Request,
    ):
        if not _same_origin(request):
            raise HTTPException(status_code=403, detail="创作请求必须来自当前管理页")
        response = await routes.create_chat_completion(payload, request, "admin-session")
        if isinstance(response, StreamingResponse) and routes.generation_handler is not None:
            response.body_iterator = _archive_stream(response.body_iterator, get_archive())
        return response

    return router
