"""Synchronous text-video compatibility routes for the isolated Vids gateway."""

import base64
import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile

from ..services.vids.gateway import GatewayError
from .openai_common import APIError, OpenAIRoute, limit_request_body, read_json_body
from .openai_images import image_dimensions
from .vids_schemas import (
    ERROR_RESPONSES,
    LIST_VIDEO,
    VIDEO,
    file_response,
    json_response,
    operation,
    video_request_body,
)

FIELDS = {
    "model",
    "prompt",
    "seconds",
    "size",
    "mode",
    "image_base64",
    "images_base64",
    "shot_mode",
    "video_base64",
}
STATUS = {
    "generating": "in_progress",
    "completed": "completed",
    "failed": "failed",
    "no_result": "failed",
    "specification_mismatch": "failed",
    "download_failed": "failed",
    "received": "received",
    "unknown": "unknown",
}
ERRORS = {
    "acceptance_unknown": "Upstream acceptance is unknown; generation will not be repeated.",
    "interrupted_acceptance_unknown": "Acceptance was interrupted; generation will not be repeated.",
    "download_failed": "The accepted video could not be downloaded; use the Vids download endpoint.",
    "download_interrupted": "The accepted download was interrupted; use the Vids download endpoint.",
    "upstream_returned_no_media": "Upstream returned no video.",
    "upstream_video_specification": "The video does not match the verified specification.",
    "invalid_source": "The source video is unavailable.",
    "session_persistence_failed": "Session storage needs recovery.",
}


async def read_vids_video_request(request):
    """Parse the same generation fields for JSON and bounded multipart image uploads."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        limit_request_body(request, 85 * 1024**2)
        body = await read_json_body(request, 85 * 1024**2)
    elif content_type == "multipart/form-data":
        limit_request_body(request, 61 * 1024**2)
        body, images, total, video = {}, [], 0, None
        async with request.form(max_files=8, max_fields=12, max_part_size=14 * 1024**2) as form:
            for name, value in form.multi_items():
                if name == "input_video":
                    if not isinstance(value, UploadFile) or video is not None:
                        raise APIError(
                            400, "Exactly one MP4 input_video file is required", param=name
                        )
                    data = await value.read(50 * 1024**2 + 1)
                    if not data or len(data) > 50 * 1024**2:
                        raise APIError(413, "input_video must not exceed 50 MiB", param=name)
                    video = base64.b64encode(data).decode("ascii")
                elif name in {"input_reference", "input_reference[]", "images", "images[]"}:
                    if not isinstance(value, UploadFile):
                        raise APIError(400, "Reference images must be uploaded files", param=name)
                    data = await value.read(10 * 1024**2 + 1)
                    total += len(data)
                    if total > 10 * 1024**2:
                        raise APIError(
                            413, "Reference images must not exceed 10 MiB total", param=name
                        )
                    if not image_dimensions(data):
                        raise APIError(400, "Reference must be PNG, JPEG or WebP", param=name)
                    images.append(base64.b64encode(data).decode("ascii"))
                elif name not in FIELDS or name in body or not isinstance(value, str):
                    raise APIError(400, "Unsupported or duplicate video parameter", param=name)
                else:
                    body[name] = value
        if video is not None:
            if "video_base64" in body:
                raise APIError(400, "Do not mix uploaded and base64 video inputs")
            body["video_base64"] = video
            body.setdefault("mode", "edit")
        if "images_base64" in body:
            # This field is an array rather than the object accepted by the shared helper.
            try:
                body["images_base64"] = json.loads(body["images_base64"])
            except (TypeError, ValueError):
                raise APIError(
                    400, "images_base64 must be a JSON array", param="images_base64"
                ) from None
        if images:
            if "image_base64" in body or "images_base64" in body:
                raise APIError(400, "Do not mix uploaded and base64 reference images")
            mode = body.get("mode", "animate" if len(images) == 1 else "ingredients")
            body["mode"] = mode
            if mode == "animate":
                if len(images) != 1:
                    raise APIError(400, "Animate requires one image")
                body["image_base64"] = images[0]
            else:
                body["images_base64"] = images
    else:
        raise APIError(415, "Use application/json or multipart/form-data")
    unknown = set(body) - FIELDS
    if unknown:
        raise APIError(400, "Unsupported video parameter", param=sorted(unknown)[0])
    if body.get("mode") in {"edit", "extend", "upscale"} and "video_base64" not in body:
        raise APIError(400, "Upload an MP4 using input_video or video_base64", param="input_video")
    if body.get("model") != "vids-video":
        raise APIError(400, "model must be vids-video", "invalid_model", "model")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise APIError(400, "prompt must contain 1 to 32000 characters", param="prompt")
    if "seconds" in body:
        seconds = body["seconds"]
        if type(seconds) not in (int, str) or str(seconds) not in {
            str(value) for value in range(3, 11)
        }:
            raise APIError(400, "seconds must be 3 to 10", param="seconds")
        body["seconds"] = int(seconds)
    return {"kind": "video", **{name: value for name, value in body.items() if name != "model"}}


def public_video(record):
    """Expose receipt facts without upstream resources, URLs or acceptance responses."""
    specification = record.get("video_specification", {})
    code = record.get("error")
    if code and code not in ERRORS and not re.fullmatch(r"upstream_http_[1-5]\d\d", code):
        code = "generation_failed"
    status = record["status"]
    identifier = record["id"]
    message = (
        "Upstream acceptance is unknown; generation will not be repeated."
        if status == "unknown"
        else ERRORS.get(code, "Video generation failed.")
    )
    return {
        "id": identifier,
        "object": "video",
        "model": "vids-video",
        "status": STATUS.get(status, "unknown"),
        "progress": 100 if status == "completed" else 0,
        "created_at": record["created_at"],
        "completed_at": record.get("completed_at"),
        "expires_at": None,
        "prompt": record.get("video_request", {}).get("prompt"),
        "seconds": f"{specification['seconds']:g}" if "seconds" in specification else None,
        "size": specification.get("size"),
        "error": {"code": code, "message": message} if code else None,
        "vids_status": status,
        "receipt_url": f"/v1/vids/generations/{identifier}",
        "retry_download_url": f"/v1/vids/generations/{identifier}/download"
        if status in {"received", "download_failed"}
        else None,
        "content_url": f"/v1/videos/{identifier}/content" if status == "completed" else None,
    }


def create_vids_videos_router(gateway, authenticate):
    router = APIRouter(
        route_class=OpenAIRoute,
        dependencies=[Depends(authenticate)],
        tags=["Vids videos"],
        responses={"default": ERROR_RESPONSES["default"]},
    )

    def get_video(identifier):
        try:
            record = gateway.get(identifier)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        if record.get("kind") != "video" or record.get("operation") == "insert":
            raise APIError(404, "Video not found", "not_found")
        return record

    @router.post(
        "/v1/videos",
        openapi_extra=operation(
            body=video_request_body(),
            responses={
                **json_response(VIDEO),
                **json_response(VIDEO, "Pending or unknown acceptance", 202),
            },
            idempotent=True,
        ),
    )
    async def create_video(request: Request):
        body = await read_vids_video_request(request)
        try:
            result = await gateway.generate(
                body,
                request.headers.get("idempotency-key"),
            )
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        status = 202 if result["status"] in {"generating", "received", "unknown"} else 200
        return JSONResponse(public_video(get_video(result["id"])), status_code=status)

    @router.get("/v1/videos", openapi_extra=operation(responses=json_response(LIST_VIDEO)))
    async def list_videos(limit: int = 20, after: str | None = None, order: str = "desc"):
        if not 1 <= limit <= 100 or order not in {"asc", "desc"}:
            raise APIError(400, "Invalid limit or order", "invalid_pagination")
        anchor = get_video(after) if after is not None else None
        # ponytail: scan local receipts; use indexed storage when this development runtime grows.
        records = [gateway.get(path.stem) for path in gateway.root.glob("vids_*.json")]
        records = [
            record
            for record in records
            if record.get("kind") == "video" and record.get("operation") != "insert"
        ]
        records.sort(
            key=lambda record: (record["created_at"], record["id"]), reverse=order == "desc"
        )
        if anchor is not None:
            position = (anchor["created_at"], anchor["id"])
            records = [
                record
                for record in records
                if (
                    (record["created_at"], record["id"]) < position
                    if order == "desc"
                    else (record["created_at"], record["id"]) > position
                )
            ]
        data = [public_video(record) for record in records[:limit]]
        return {
            "object": "list",
            "data": data,
            "has_more": len(records) > limit,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }

    @router.get("/v1/videos/{video_id}", openapi_extra=operation(responses=json_response(VIDEO)))
    async def retrieve_video(video_id: str):
        return public_video(get_video(video_id))

    @router.get(
        "/v1/videos/{video_id}/content",
        response_class=FileResponse,
        openapi_extra=operation(responses=file_response("Completed MP4 video", ("video/mp4",))),
    )
    async def video_content(video_id: str, variant: str = "video"):
        if variant != "video":
            raise APIError(400, "Only variant=video is supported", param="variant")
        record = get_video(video_id)
        if record["status"] != "completed":
            raise APIError(409, "Video content is unavailable", "video_not_ready")
        if record.get("suffix") != ".mp4" or record.get("mime_type") != "video/mp4":
            raise APIError(410, "Video content is unavailable", "asset_expired")
        try:
            path, mime = gateway.content(video_id)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        if path.resolve().parent != gateway.root:
            raise APIError(410, "Video content is unavailable", "asset_expired")
        return FileResponse(path, media_type=mime, filename=video_id + ".mp4")

    return router
