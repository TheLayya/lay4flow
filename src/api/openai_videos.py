"""OpenAI-shaped video jobs backed by the shared Flow execution pipeline."""

import asyncio
import base64
import hashlib
import json
import os
import struct
import time
from contextlib import aclosing
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from ..core.auth import verify_openai_api_key
from ..core.config import config
from ..core.models import ChatCompletionRequest
from ..core.public_models import PUBLIC_MODELS
from ..services.video_jobs import VideoJobError
from .openai_common import (
    APIError,
    OpenAIRoute,
    generated_cache_path,
    limit_request_body,
    load_generated_asset,
    parse_json_object_form,
    read_json_body,
)

FIELDS = {
    "model",
    "prompt",
    "seconds",
    "size",
    "input_reference",
    "generationConfig",
    "input_video",
}
VIDEO_SIZES = {
    "640x360": ("16:9", "360p"),
    "360x640": ("9:16", "360p"),
    "1280x720": ("16:9", "720p"),
    "720x1280": ("9:16", "720p"),
    "1920x1080": ("16:9", "1080p"),
    "1080x1920": ("9:16", "1080p"),
    "3840x2160": ("16:9", "4k"),
    "2160x3840": ("9:16", "4k"),
}


async def read_video_request(request: Request, allowed_fields=FIELDS, array_references=False):
    """Read bounded JSON or multipart without resolving a model or submitting a job."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    files = {}
    if content_type == "application/json":
        body = await read_json_body(request, 30 * 1024 * 1024)
    elif content_type == "multipart/form-data":
        limit_request_body(request, 101 * 1024 * 1024)
        async with request.form(
            max_files=8, max_fields=20, max_part_size=100 * 1024 * 1024
        ) as form:
            body = {}
            total = 0
            for name, value in form.multi_items():
                repeated_reference = array_references and name in {
                    "input_reference",
                    "input_reference[]",
                }
                if repeated_reference:
                    name = "input_reference"
                if (
                    name not in allowed_fields
                    or name in body
                    or (name in files and not repeated_reference)
                ):
                    raise APIError(400, "Unknown or duplicate video field", param=name)
                if isinstance(value, UploadFile):
                    if name not in {"input_reference", "input_video"}:
                        raise APIError(400, "Unexpected file field", param=name)
                    limit = 20 * 1024 * 1024 if name == "input_reference" else 100 * 1024 * 1024
                    data = await value.read(limit + 1)
                    total += len(data)
                    if not data or len(data) > limit or total > 100 * 1024 * 1024:
                        raise APIError(
                            413, "Video input is empty or too large", "request_too_large"
                        )
                    if repeated_reference:
                        if len(files.get(name, [])) >= 7:
                            raise APIError(
                                400, "At most 7 input references are supported", param=name
                            )
                        files.setdefault(name, []).append((data, value.content_type))
                    else:
                        files[name] = (data, value.content_type)
                else:
                    body[name] = value
    else:
        raise APIError(415, "Use application/json or multipart/form-data")
    return body, files


async def normalize_video_request(request: Request):
    body, files = await read_video_request(request)
    return await normalize_video_body(request, body, files)


async def normalize_video_body(
    request: Request, body: dict, files: dict, *, clamp_default_edit_trim=False
):
    from . import routes
    from .openai_images import image_dimensions

    unknown = set(body) - FIELDS
    if unknown:
        raise APIError(400, "Unsupported video parameter", param=sorted(unknown)[0])
    model = body.get("model", "veo_3_1")
    if (
        not isinstance(model, str)
        or model not in PUBLIC_MODELS
        or PUBLIC_MODELS[model]["type"] != "video"
    ):
        raise APIError(400, "A supported video model is required", param="model")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise APIError(400, "prompt must contain 1 to 32000 characters", param="prompt")
    generation = parse_json_object_form(body.get("generationConfig"))
    if set(generation) - {"videoConfig"}:
        raise APIError(400, "Only videoConfig is supported for videos", param="generationConfig")
    options = dict(parse_json_object_form(generation.get("videoConfig"), "videoConfig"))
    size = body.get("size")
    if size is not None:
        if not isinstance(size, str) or size not in VIDEO_SIZES:
            raise APIError(400, "Unsupported video size", param="size")
        aspect, resolution = VIDEO_SIZES[size]
        for name, value in (("aspectRatio", aspect), ("resolution", resolution)):
            if name in options and options[name] != value:
                raise APIError(400, f"size conflicts with videoConfig.{name}", param="size")
            options[name] = value
    seconds = body.get("seconds")
    if seconds is not None:
        if str(seconds) not in {"4", "6", "8", "10"} or isinstance(seconds, bool):
            raise APIError(400, "Unsupported seconds for Flow video", param="seconds")
        if options.get("mode") == "edit":
            raise APIError(
                400, "Use trimStartMs/trimEndMs for editing, not seconds", param="seconds"
            )
        if "durationSeconds" in options and options["durationSeconds"] != int(seconds):
            raise APIError(400, "Conflicting duration parameters", param="seconds")
        options["durationSeconds"] = int(seconds)
    content = [{"type": "text", "text": prompt}]
    if "input_reference" in files:
        references = files["input_reference"]
        if not isinstance(references, list):
            references = [references]
        for data, _ in references:
            dimensions = image_dimensions(data)
            if not dimensions:
                raise APIError(
                    400, "Reference must be a PNG, JPEG or WebP image", param="input_reference"
                )
            _, _, mime = dimensions
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode()},
                }
            )
    elif body.get("input_reference") is not None:
        reference = parse_json_object_form(body["input_reference"], "input_reference")
        if set(reference) != {"image_url"} or not isinstance(reference["image_url"], str):
            raise APIError(
                400,
                "input_reference requires image_url; file_id is not supported",
                param="input_reference",
            )
        uri = reference["image_url"]
        if not uri.startswith("data:image/"):
            # The public downloader rejects private destinations, redirects and large bodies.
            data, _ = await load_generated_asset(uri, "image", allow_local=False)
        else:
            _, data = routes._decode_data_url(uri)
        if len(data) > 20 * 1024 * 1024:
            raise APIError(413, "Reference image is too large")
        dimensions = image_dimensions(data)
        if not dimensions:
            raise APIError(
                400, "Reference must be a PNG, JPEG or WebP image", param="input_reference"
            )
        _, _, mime = dimensions
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode()},
            }
        )
    if len(content) > 1 and "mode" not in options and "input_video" not in files:
        options["mode"] = "image_to_video" if len(content) == 2 else "reference_to_video"
    if "input_video" in files:
        if model != "omni_1.1_flash":
            raise APIError(400, "input_video requires an Omni editing model", param="input_video")
        data, mime = files["input_video"]
        if options.get("mode", "edit") != "edit":
            raise APIError(400, "input_video requires edit mode")
        options["mode"] = "edit"
        if "durationSeconds" in options:
            raise APIError(
                400, "Use trimStartMs/trimEndMs for editing, not seconds", param="seconds"
            )
        content.append(
            {
                "type": "video_url",
                "video_url": {
                    "url": f"data:{mime or 'video/mp4'};base64," + base64.b64encode(data).decode()
                },
            }
        )
    elif "input_video" in body:
        raise APIError(400, "input_video must be a multipart video file", param="input_video")
    normalized = await routes._normalize_openai_request(
        ChatCompletionRequest.model_validate(
            {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "generationConfig": {"videoConfig": options},
            }
        ),
        clamp_default_edit_trim=clamp_default_edit_trim,
    )
    # Avoid a second Base64 copy in the bounded in-memory queue.
    normalized.messages = None
    options = normalized.generation_options
    actual_size = next(
        k
        for k, value in VIDEO_SIZES.items()
        if value == (options["aspectRatio"], options["resolution"])
    )
    duration = options.get("durationSeconds")
    if options.get("mode") == "edit":
        duration = (options.get("trimEndMs", 10000) - options.get("trimStartMs", 0)) / 1000
    return normalized, {
        "model": model,
        "prompt": prompt,
        "seconds": f"{duration:g}",
        "size": actual_size,
    }


def video_dimensions(data):
    """Read track dimensions from MP4 containers, without trusting the requested size."""

    def boxes(start, end, depth=0):
        if depth > 5:
            return None
        while start + 8 <= end:
            length, kind = struct.unpack_from(">I4s", data, start)
            header = 8
            if length == 1:
                if start + 16 > end:
                    return None
                length = struct.unpack_from(">Q", data, start + 8)[0]
                header = 16
            if length == 0:
                length = end - start
            if length < header or start + length > end:
                return None
            if kind == b"tkhd" and length >= header + 84:
                w, h = struct.unpack_from(">II", data, start + length - 8)
                if w and h:
                    return round(w / 65536), round(h / 65536)
            if kind in (b"moov", b"trak"):
                found = boxes(start + header, start + length, depth + 1)
                if found:
                    return found
            start += length
        return None

    return boxes(0, len(data))


async def execute_video(normalized, base_url, expected_duration=None):
    from . import routes

    media = None
    # Drain the generation lifecycle to completion so counters and slots release.
    async with aclosing(routes._iterate_openai_stream(normalized, base_url)) as stream:
        async for chunk in stream:
            raw = chunk[6:].strip()
            if raw == "[DONE]":
                continue
            payload = json.loads(raw)
            if "error" in payload:
                error = payload["error"]
                raise VideoJobError(
                    error.get("status_code", 502),
                    error.get("code", "generation_failed"),
                    "Video generation failed",
                )
            if payload.get("warnings"):
                if any(x.get("code") == "upscale_failed" for x in payload["warnings"]):
                    raise VideoJobError(
                        502, "upscale_failed", "Requested video resolution could not be produced"
                    )
            if payload.get("media"):
                media = payload["media"]
            yield {"type": "progress", "progress": min(99, payload.get("progress", 0))}
    if not media or media.get("type") != "video":
        raise VideoJobError(502, "missing_asset", "Generation returned no video")
    path = generated_cache_path(media["url"])
    if path is None:
        data, _ = await load_generated_asset(media["url"], "video")
        cache = routes._ensure_generation_handler().file_cache
        name = hashlib.sha256(data).hexdigest()[:32] + ".mp4"
        path = cache.cache_dir / name

        def write():
            temporary = path.with_suffix(".pending")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

        await asyncio.to_thread(write)
    if not path.is_file():
        raise VideoJobError(410, "asset_expired", "Generated video is no longer available")
    if path.stat().st_size > 100 * 1024 * 1024:
        raise VideoJobError(502, "asset_too_large", "Generated video is too large")
    content = await asyncio.to_thread(path.read_bytes)
    dimensions = video_dimensions(content)
    options = normalized.generation_options
    expected = next(
        k
        for k, value in VIDEO_SIZES.items()
        if value == (options["aspectRatio"], options["resolution"])
    )
    if not dimensions or f"{dimensions[0]}x{dimensions[1]}" != expected:
        raise VideoJobError(
            502,
            "output_size_mismatch",
            "Generated video dimensions do not match the requested size",
        )
    if expected_duration is not None:
        actual_duration = routes._mp4_duration_ms(content)
        if actual_duration is None or abs(actual_duration - expected_duration * 1000) > 250:
            raise VideoJobError(
                502,
                "output_duration_mismatch",
                "Generated video duration does not match the requested duration",
            )
    expires = int(time.time() + config.cache_timeout) if config.cache_timeout > 0 else None
    yield {"type": "completed", "media": {**media, "asset_path": str(path), "expires_at": expires}}


def create_videos_router(get_service, prefix="", normalize_request=None):
    router = APIRouter(prefix=prefix, route_class=OpenAIRoute, tags=["Videos"])
    normalize_request = normalize_request or normalize_video_request

    async def invoke(method, *args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except VideoJobError as exc:
            raise APIError(exc.status_code, exc.message, exc.code) from None

    def owner(key):
        return hashlib.sha256(key.encode()).hexdigest()

    @router.post("/v1/videos")
    async def create_video(
        request: Request,
        api_key: str = Depends(verify_openai_api_key),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        from . import routes

        if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 200):
            raise APIError(400, "Idempotency-Key must contain 1 to 200 characters")
        normalized, params = await normalize_request(request)
        return await invoke(
            get_service().submit,
            owner(api_key),
            normalized,
            params,
            routes._get_request_base_url(request),
            idempotency_key=idempotency_key,
        )

    @router.get("/v1/videos")
    async def list_videos(
        limit: int = 20,
        after: str | None = None,
        order: str = "desc",
        api_key: str = Depends(verify_openai_api_key),
    ):
        if not 1 <= limit <= 100 or order not in {"asc", "desc"}:
            raise APIError(400, "Invalid limit or order")
        rows = await invoke(
            get_service().list, owner(api_key), limit=limit + 1, after=after, order=order
        )
        data = rows[:limit]
        return {
            "object": "list",
            "data": data,
            "has_more": len(rows) > limit,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }

    @router.get("/v1/videos/{video_id}")
    async def retrieve_video(video_id: str, api_key: str = Depends(verify_openai_api_key)):
        row = await invoke(get_service().get, video_id, owner(api_key))
        if row is None:
            raise APIError(404, "Video not found", "not_found")
        return row

    @router.get("/v1/videos/{video_id}/content")
    async def video_content(
        video_id: str, variant: str = "video", api_key: str = Depends(verify_openai_api_key)
    ):
        if variant != "video":
            raise APIError(400, "Only variant=video is supported", param="variant")
        row = await invoke(get_service().get_internal, video_id, owner(api_key))
        if row is None:
            raise APIError(404, "Video not found", "not_found")
        if row["status"] != "completed":
            raise APIError(409, "Video is not completed", "video_not_ready")
        path = Path(row.get("asset_path") or "")
        from . import routes

        root = routes._ensure_generation_handler().file_cache.cache_dir.resolve()
        if not row.get("asset_path") or path.resolve().parent != root or not path.is_file():
            raise APIError(410, "Video content has expired", "asset_expired")
        if row.get("expires_at") and row["expires_at"] <= time.time():
            raise APIError(410, "Video content has expired", "asset_expired")
        return FileResponse(path, media_type="video/mp4", filename=video_id + ".mp4")

    @router.delete("/v1/videos/{video_id}")
    async def delete_video(video_id: str, api_key: str = Depends(verify_openai_api_key)):
        deleted = await invoke(get_service().delete, video_id, owner(api_key))
        if not deleted:
            raise APIError(404, "Video not found", "not_found")
        # Cache files may be shared by jobs; normal cache expiry owns their cleanup.
        return {"id": video_id, "object": "video.deleted", "deleted": True}

    return router
