"""Account-explicit production media routes sharing the Vids dispatcher."""

import base64
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile

from ..core.auth import verify_openai_api_key
from ..services.vids.client import PRESET_VOICES, VidsProtocolError, VidsTransportError
from ..services.vids.dispatcher import VidsPoolDispatcher
from ..services.vids.gateway import GatewayError, VidsGateway
from .openai_common import (
    APIError,
    OpenAIRoute,
    api_error_response,
    limit_request_body,
    read_json_body,
)
from .openai_images import IMAGE_LIMIT, image_dimensions
from .vids_schemas import (
    CONSOLE_DOCUMENT_IMAGE_REQUEST,
    CONSOLE_DOCUMENT_IMAGE_RESULT,
    ERROR_RESPONSES,
    IMAGE_EDIT_REQUEST,
    IMAGE_REQUEST,
    IMAGE_RESULT,
    LIST_VIDEO,
    MODEL_LIST,
    SPEECH_REQUEST,
    VIDEO,
    console_document_list_response,
    file_response,
    generation_request,
    json_response,
    media_capabilities,
    operation,
    receipt_response,
    request_body,
    speech_responses,
    video_request_body,
)
from .vids_videos import public_video, read_vids_video_request

PREFIX = "/providers/vids/accounts/{token_id}"
PUBLIC_PREFIX = "/providers/vids"
CONSOLE_PREFIX = "/api/admin/vids"


def speech_error_response(record, message):
    """Attach only public receipt facts to the existing speech error envelope."""
    pending = record["status"] in {"generating", "received", "unknown"}
    response = api_error_response(
        APIError(
            409 if pending else 502, message, "speech_not_ready" if pending else "speech_failed"
        )
    )
    data = json.loads(response.body)
    data["vids_receipt"] = {
        key: record[key]
        for key in ("id", "kind", "status", "error", "error_operation", "content_url")
        if key in record
    }
    if isinstance(record.get("upstream_error"), dict):
        data["vids_receipt"]["upstream_error"] = {
            key: record["upstream_error"][key]
            for key in ("status", "reason")
            if isinstance(record["upstream_error"].get(key), str)
        }
    return JSONResponse(data, status_code=response.status_code)


def validate_image_options(body):
    if set(body) - {"model", "prompt", "n", "size", "response_format", "aspect_ratio", "style"}:
        raise APIError(400, "Unsupported image generation parameter")
    if body.get("model") != "vids-image":
        raise APIError(400, "model must be vids-image", "invalid_model", "model")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise APIError(400, "prompt must contain 1 to 32000 characters", param="prompt")
    if type(body.get("n", 1)) is not int or not 1 <= body.get("n", 1) <= 4:
        raise APIError(400, "n must be an integer from 1 to 4", param="n")
    size = body.get("size", "auto")
    if size not in ("auto", "1024x1024", "1:1", "16:9", "9:16"):
        raise APIError(400, "Unsupported image size or aspect ratio", param="size")
    response_format = body.get("response_format", "url")
    if response_format not in ("url", "b64_json"):
        raise APIError(400, "response_format must be url or b64_json", param="response_format")
    return prompt, size, response_format


def image_generation_body(body, source=None):
    """Keep transport options separate from the actual upstream generation parameters."""
    prompt, size, _ = validate_image_options(body)
    result = {"kind": "image", "prompt": prompt, **(source or {})}
    aspect = body.get("aspect_ratio")
    size_aspect = {"1024x1024": "1:1", "1:1": "1:1", "16:9": "16:9", "9:16": "9:16"}.get(size)
    if source:
        if body.get("n", 1) != 1:
            raise APIError(400, "Image editing supports only n=1", param="n")
        if aspect is not None or "style" in body or size not in {"auto", "1024x1024"}:
            raise APIError(400, "Image editing does not accept output ratio or style options")
    else:
        if aspect is not None and size_aspect is not None and aspect != size_aspect:
            raise APIError(400, "size and aspect_ratio conflict", param="aspect_ratio")
        if aspect is not None or size_aspect is not None:
            result["aspect_ratio"] = aspect if aspect is not None else size_aspect
        if "style" in body:
            result["style"] = body["style"]
        if body.get("n", 1) != 1:
            result["n"] = body["n"]
    return result


def image_result_item(path, mime, size, response_format, url):
    """Verify each downloaded image before returning its bytes or authenticated URL."""
    try:
        with path.open("rb") as handle:
            data = handle.read(IMAGE_LIMIT + 1)
    except FileNotFoundError:
        raise APIError(410, "Generation content is unavailable", "expired") from None
    except OSError:
        raise APIError(502, "Image content could not be read", "image_content_failed") from None
    actual = image_dimensions(data)
    if len(data) > IMAGE_LIMIT or not actual or actual[2] != mime:
        raise APIError(502, "Generated content is not a supported image", "invalid_image_content")
    if size == "1024x1024" and actual[:2] != (1024, 1024):
        raise APIError(
            502, "The image does not match the requested size", "image_specification_mismatch"
        )
    return (
        {"url": url}
        if response_format == "url"
        else {"b64_json": base64.b64encode(data).decode("ascii")}
    )


async def read_image_edit(request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "multipart/form-data":
        raise APIError(415, "Image edits require multipart/form-data")
    limit = 10 * 1024**2
    limit_request_body(request, limit + 1024**2)
    body, image = {}, None
    async with request.form(max_files=2, max_fields=6, max_part_size=128 * 1024) as form:
        for name, value in form.multi_items():
            if name in {"image", "image[]"}:
                if not isinstance(value, UploadFile) or image is not None:
                    raise APIError(400, "Exactly one uploaded image is required", param="image")
                if value.size is not None and value.size > limit:
                    raise APIError(413, "The image must not exceed 10 MiB", param="image")
                image = await value.read(limit + 1)
                if len(image) > limit:
                    raise APIError(413, "The image must not exceed 10 MiB", param="image")
                if not image_dimensions(image):
                    raise APIError(400, "Input must be a PNG, JPEG or WebP image", param="image")
            elif (
                name
                not in {"model", "prompt", "n", "size", "response_format", "aspect_ratio", "style"}
                or not isinstance(value, str)
                or name in body
            ):
                raise APIError(400, "Unsupported or duplicate image parameter", param=name)
            else:
                body[name] = value
    if image is None:
        raise APIError(400, "Exactly one uploaded image is required", param="image")
    if "n" in body:
        if body["n"] != "1":
            raise APIError(400, "Only n=1 is supported", param="n")
        body["n"] = 1
    validate_image_options(body)
    return body, base64.b64encode(image).decode("ascii")


def create_vids_media_router(
    dispatcher, authenticate=verify_openai_api_key, *, prefix=PREFIX, tags=None, auto_pool=False
):
    async def account_id(token_id: int):
        return token_id

    async def pool_account():
        return 0

    account_dependency = pool_account if auto_pool else account_id
    console = prefix == CONSOLE_PREFIX
    if auto_pool:
        dispatcher = VidsPoolDispatcher(dispatcher)
    content_name = "vids_account_content" if prefix == PREFIX else "vids_account_content:" + prefix
    router = APIRouter(
        prefix=prefix,
        route_class=OpenAIRoute,
        dependencies=[Depends(authenticate)],
        tags=tags if tags is not None else ["Vids media"],
        responses={"default": ERROR_RESPONSES["default"]},
    )

    def public(record, token_id, *, video=False):
        result = public_video(record) if video else VidsGateway.public(record)
        if not video:
            result["receipt_url"] = f"/v1/vids/generations/{record['id']}"
            result["retry_download_url"] = (
                result["receipt_url"] + "/download"
                if record["status"] in {"received", "download_failed"}
                else None
            )
        account_prefix = prefix.format(token_id=token_id)
        for field in ("receipt_url", "retry_download_url", "content_url"):
            if result.get(field):
                result[field] = account_prefix + result[field]
        for output in result.get("outputs", []):
            output["content_url"] = account_prefix + output["content_url"]
        return result

    def get(token_id, identifier, *, video=False):
        try:
            record = dispatcher.get(token_id, identifier)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        if (
            record.get("kind") not in {"image", "speech", "video"}
            or record.get("operation") == "insert"
        ):
            raise APIError(404, "Generation not found", "not_found")
        if video and record["kind"] != "video":
            raise APIError(404, "Video not found", "not_found")
        return record

    def content(token_id, identifier, index=0):
        get(token_id, identifier)
        try:
            return (
                dispatcher.content(token_id, identifier, index)
                if index
                else dispatcher.content(token_id, identifier)
            )
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None

    async def generate(request, token_id, body):
        try:
            result = await dispatcher.generate(
                token_id, body, request.headers.get("idempotency-key")
            )
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        return public(get(token_id, result["id"]), token_id)

    @router.get("/v1/models", openapi_extra=operation(responses=json_response(MODEL_LIST)))
    async def models(token_id: int = Depends(account_dependency)):
        # Resolve the explicit binding even when no receipt exists.
        try:
            dispatcher.models() if auto_pool else dispatcher.list(token_id)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        account_prefix = prefix.format(token_id=token_id)
        return {
            "object": "list",
            "data": [
                {
                    "id": "vids-" + kind,
                    "object": "model",
                    "owned_by": "google",
                    "provider": "vids",
                    "kind": kind,
                    "capabilities": media_capabilities(kind, account_prefix, console=console),
                    "supported_endpoints": [
                        account_prefix + "/v1/vids/generations",
                        account_prefix + endpoint,
                    ]
                    + ([account_prefix + "/v1/images/edits"] if kind == "image" else []),
                }
                for kind, endpoint in (
                    ("image", "/v1/images/generations"),
                    ("speech", "/v1/audio/speech"),
                    ("video", "/v1/videos"),
                )
            ],
        }

    @router.post(
        "/v1/vids/generations",
        openapi_extra=operation(
            body=request_body(generation_request(console=console)),
            responses=receipt_response(pending=True, account=True),
            idempotent=True,
        ),
    )
    async def create_generation(request: Request, token_id: int = Depends(account_dependency)):
        body = await read_json_body(request, 85 * 1024**2)
        if body.get("kind") == "video" and "source_id" in body:
            raise APIError(400, "Upload the source MP4 using video_base64", param="source_id")
        result = await generate(request, token_id, body)
        status = 202 if result["status"] in {"generating", "received", "unknown"} else 200
        return JSONResponse(result, status_code=status)

    if auto_pool and console:

        async def invoke_document(method, *args):
            try:
                return await method(*args)
            except GatewayError as error:
                raise APIError(error.status, str(error), error.code) from None
            except VidsProtocolError as error:
                raise APIError(
                    502, "Upstream document request failed", f"upstream_http_{error.status}"
                ) from None
            except (VidsTransportError, ValueError):
                raise APIError(
                    502, "Upstream document request failed", "upstream_document_failed"
                ) from None

        @router.get(
            "/v1/vids/document/images",
            openapi_extra=operation(responses=console_document_list_response()),
        )
        async def document_images(token_id: int = Depends(account_dependency)):
            return await invoke_document(dispatcher.document_images, token_id)

        @router.get(
            "/v1/vids/document/scenes",
            openapi_extra=operation(responses=console_document_list_response(True)),
        )
        async def document_scenes(token_id: int = Depends(account_dependency)):
            return await invoke_document(dispatcher.document_images, token_id, True)

        @router.post(
            "/v1/vids/document/images",
            openapi_extra=operation(
                body=request_body(CONSOLE_DOCUMENT_IMAGE_REQUEST),
                responses={
                    **json_response(CONSOLE_DOCUMENT_IMAGE_RESULT),
                    **json_response(
                        CONSOLE_DOCUMENT_IMAGE_RESULT, "Unknown insertion acceptance", 202
                    ),
                },
                idempotent=True,
            ),
        )
        async def insert_document_image(
            request: Request, token_id: int = Depends(account_dependency)
        ):
            body = await read_json_body(request, 15 * 1024**2)
            result = await invoke_document(
                dispatcher.insert_document_image,
                token_id,
                body,
                request.headers.get("idempotency-key"),
            )
            return JSONResponse(result, status_code=202 if result["status"] == "unknown" else 200)

        @router.post(
            "/v1/vids/document/images/{identifier}/verify",
            openapi_extra=operation(responses=json_response(CONSOLE_DOCUMENT_IMAGE_RESULT)),
        )
        async def verify_document_image(
            identifier: str, token_id: int = Depends(account_dependency)
        ):
            return await invoke_document(dispatcher.verify_document_image, token_id, identifier)

    @router.get(
        "/v1/vids/generations/{generation_id}",
        openapi_extra=operation(responses=receipt_response(account=True)),
    )
    async def retrieve_generation(generation_id: str, token_id: int = Depends(account_dependency)):
        return public(get(token_id, generation_id), token_id)

    @router.post(
        "/v1/vids/generations/{generation_id}/download",
        openapi_extra=operation(responses=receipt_response(account=True)),
    )
    async def retry_download(generation_id: str, token_id: int = Depends(account_dependency)):
        get(token_id, generation_id)
        try:
            await dispatcher.retry_download(token_id, generation_id)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        return public(get(token_id, generation_id), token_id)

    @router.get(
        "/v1/vids/generations/{generation_id}/content",
        name=content_name,
        response_class=FileResponse,
        openapi_extra=operation(responses=file_response()),
    )
    async def generation_content(
        generation_id: str, token_id: int = Depends(account_dependency), index: int = 0
    ):
        path, mime = content(token_id, generation_id, index)
        return FileResponse(path, media_type=mime, filename=path.name)

    @router.post(
        "/v1/audio/speech",
        response_class=FileResponse,
        openapi_extra=operation(
            body=request_body(SPEECH_REQUEST),
            responses=speech_responses(),
            idempotent=True,
        ),
    )
    async def speech(request: Request, token_id: int = Depends(account_dependency)):
        body = await read_json_body(request)
        if set(body) - {"model", "input", "voice", "response_format"}:
            raise APIError(400, "Unsupported speech parameter")
        if body.get("model") != "vids-speech":
            raise APIError(400, "model must be vids-speech", "invalid_model", "model")
        text = body.get("input")
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise APIError(400, "input must contain 1 to 4096 characters", param="input")
        voice = body.get("voice")
        if not isinstance(voice, str) or voice not in PRESET_VOICES:
            raise APIError(400, "voice must be a supported preset name", param="voice")
        if body.get("response_format") != "wav":
            raise APIError(400, "Explicit response_format=wav is required", param="response_format")
        record = await generate(
            request, token_id, {"kind": "speech", "prompt": text, "voice": voice}
        )
        if record["status"] == "completed":
            path, mime = content(token_id, record["id"])
            return FileResponse(
                path,
                media_type=mime,
                filename=path.name,
                headers={"X-Vids-Generation-Id": record["id"]} if auto_pool else None,
            )
        return speech_error_response(
            record,
            f"Speech request is {record['status']}; recover using {record['receipt_url']}",
        )

    @router.post(
        "/v1/images/generations",
        openapi_extra=operation(
            body=request_body(IMAGE_REQUEST),
            responses=json_response(IMAGE_RESULT, "One to four validated images"),
            idempotent=True,
        ),
    )
    async def image_generations(request: Request, token_id: int = Depends(account_dependency)):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise APIError(415, "Image generations require application/json")
        body = await read_json_body(request)
        return await image_response(request, token_id, body)

    @router.post(
        "/v1/images/edits",
        openapi_extra=operation(
            body=request_body(IMAGE_EDIT_REQUEST, "multipart/form-data"),
            responses=json_response(IMAGE_RESULT, "One validated edited image"),
            idempotent=True,
        ),
    )
    async def image_edits(request: Request, token_id: int = Depends(account_dependency)):
        body, encoded = await read_image_edit(request)
        return await image_response(
            request, token_id, body, {"mode": "edit", "image_base64": encoded}
        )

    async def image_response(request, token_id, body, source=None):
        prompt, size, response_format = validate_image_options(body)
        record = await generate(request, token_id, image_generation_body(body, source))
        if record["status"] in {"generating", "received", "unknown"}:
            raise APIError(
                409,
                f"Image request is {record['status']}; recover using {record['receipt_url']}",
                "image_not_ready",
            )
        if record["status"] != "completed":
            raise APIError(
                502, f"Image request {record['id']} ended with {record['status']}", "image_failed"
            )
        items = []
        for index in range(body.get("n", 1)):
            path, mime = content(token_id, record["id"], index)
            url = request.url_for(
                content_name,
                **({"token_id": token_id} if not auto_pool else {}),
                generation_id=record["id"],
            )
            if index:
                url = url.include_query_params(index=index)
            items.append(image_result_item(path, mime, size, response_format, str(url)))
        result = {"created": record["created_at"], "data": items}
        return (
            JSONResponse(result, headers={"X-Vids-Generation-Id": record["id"]})
            if auto_pool
            else result
        )

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
    async def create_video(request: Request, token_id: int = Depends(account_dependency)):
        body = await read_vids_video_request(request)
        record = await generate(request, token_id, body)
        status = 202 if record["status"] in {"generating", "received", "unknown"} else 200
        return JSONResponse(
            public(get(token_id, record["id"], video=True), token_id, video=True),
            status_code=status,
        )

    @router.get("/v1/videos", openapi_extra=operation(responses=json_response(LIST_VIDEO)))
    async def list_videos(
        token_id: int = Depends(account_dependency),
        limit: int = 20,
        after: str | None = None,
        order: str = "desc",
    ):
        if not 1 <= limit <= 100 or order not in {"asc", "desc"}:
            raise APIError(400, "Invalid limit or order", "invalid_pagination")
        anchor = get(token_id, after, video=True) if after is not None else None
        try:
            records = dispatcher.list(token_id)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        records = [
            row
            for row in records
            if row.get("kind") == "video" and row.get("operation") != "insert"
        ]
        records.sort(key=lambda row: (row["created_at"], row["id"]), reverse=order == "desc")
        if anchor is not None:
            position = (anchor["created_at"], anchor["id"])
            records = [
                row
                for row in records
                if (
                    (row["created_at"], row["id"]) < position
                    if order == "desc"
                    else (row["created_at"], row["id"]) > position
                )
            ]
        data = [public(row, token_id, video=True) for row in records[:limit]]
        return {
            "object": "list",
            "data": data,
            "has_more": len(records) > limit,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }

    @router.get("/v1/videos/{video_id}", openapi_extra=operation(responses=json_response(VIDEO)))
    async def retrieve_video(video_id: str, token_id: int = Depends(account_dependency)):
        return public(get(token_id, video_id, video=True), token_id, video=True)

    @router.get(
        "/v1/videos/{video_id}/content",
        response_class=FileResponse,
        openapi_extra=operation(responses=file_response("Completed MP4 video", ("video/mp4",))),
    )
    async def video_content(
        video_id: str, token_id: int = Depends(account_dependency), variant: str = "video"
    ):
        if variant != "video":
            raise APIError(400, "Only variant=video is supported", param="variant")
        record = get(token_id, video_id, video=True)
        if record["status"] != "completed":
            raise APIError(409, "Video content is unavailable", "video_not_ready")
        if record.get("suffix") != ".mp4" or record.get("mime_type") != "video/mp4":
            raise APIError(410, "Video content is unavailable", "asset_expired")
        path, mime = content(token_id, video_id)
        return FileResponse(path, media_type=mime, filename=video_id + ".mp4")

    return router
