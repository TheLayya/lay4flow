"""Cookie-authenticated Studio access to the server-owned Flow gateway."""

import json
import re
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ...core.models import ChatCompletionRequest
from ...services.studio_media import StudioMediaArchive, StudioMediaError
from .. import routes
from ..admin_schemas import ArchiveMediaRequest
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


def create_studio_router(verify_token: Callable):
    """Build the admin-only Studio bridge without exposing the configured API key."""
    router = APIRouter(
        prefix="/api/admin/studio",
        tags=["Studio console"],
        dependencies=[Depends(verify_token)],
    )
    archive_instance = None

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
