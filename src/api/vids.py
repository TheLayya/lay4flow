"""Isolated development API for the Vids HTTP adapter, with no Flow credentials gate."""

import secrets
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer

from ..services.vids.client import PRESET_VOICES, VidsProtocolError, VidsTransportError
from ..services.vids.gateway import (
    KINDS,
    GatewayError,
    VidsGateway,
)
from .openai_common import APIError, OpenAIRoute, read_json_body
from .vids_media import (
    image_generation_body,
    image_result_item,
    read_image_edit,
    speech_error_response,
    validate_image_options,
)
from .vids_schemas import (
    COOKIES_REQUEST,
    DOCUMENT_IMAGE_REQUEST,
    EMPTY_JSON_REQUEST,
    ERROR_RESPONSES,
    IMAGE_EDIT_REQUEST,
    IMAGE_REQUEST,
    IMAGE_RESULT,
    MODEL_LIST,
    QUOTA,
    RECEIPT,
    SESSION_RESULT,
    SPEECH_REQUEST,
    document_list_response,
    file_response,
    generation_request,
    json_response,
    media_capabilities,
    operation,
    receipt_response,
    request_body,
    speech_responses,
)
from .vids_videos import create_vids_videos_router


def create_vids_app(gateway: VidsGateway, api_key: str) -> FastAPI:
    if not isinstance(api_key, str) or len(api_key) < 24:
        raise ValueError("VIDS_API_KEY must contain at least 24 characters")

    @asynccontextmanager
    async def lifespan(app):
        try:
            await gateway.start()
            yield
        finally:
            await gateway.close()

    app = FastAPI(
        title="Vids HTTP development gateway", lifespan=lifespan, docs_url=None, redoc_url=None
    )

    async def authenticate(request: Request, credentials=Depends(HTTPBearer(auto_error=False))):
        if not secrets.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + api_key).encode()
        ):
            raise APIError(401, "Invalid API key", "invalid_api_key")

    router = APIRouter(
        route_class=OpenAIRoute,
        dependencies=[Depends(authenticate)],
        responses={"default": ERROR_RESPONSES["default"]},
    )

    async def invoke(method, *args):
        try:
            return await method(*args)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None
        except VidsProtocolError as error:
            raise APIError(
                502, "Upstream HTTP request failed", f"upstream_http_{error.status}"
            ) from None
        except (VidsTransportError, ValueError):
            raise APIError(
                502, "Upstream session request failed", "upstream_session_failed"
            ) from None

    def invoke_sync(method, *args):
        try:
            return method(*args)
        except GatewayError as error:
            raise APIError(error.status, str(error), error.code) from None

    @router.get("/v1/models", openapi_extra=operation(responses=json_response(MODEL_LIST)))
    async def models():
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "owned_by": "google",
                    "provider": "vids",
                    "kind": kind,
                    "supported_endpoints": ["/v1/vids/generations"]
                    + (
                        ["/v1/audio/speech"]
                        if kind == "speech"
                        else ["/v1/images/generations", "/v1/images/edits"]
                        if kind == "image"
                        else ["/v1/videos"]
                        if kind == "video"
                        else []
                    ),
                    "capabilities": media_capabilities(kind, "")
                    if kind != "music"
                    else {"instrumental": True},
                }
                for kind, model in KINDS.items()
            ],
        }

    @router.get("/v1/vids/quota", openapi_extra=operation(responses=json_response(QUOTA)))
    async def quota(kind: str = "video"):
        return await invoke(gateway.quota, kind)

    @router.get(
        "/v1/vids/document/images",
        openapi_extra=operation(responses=document_list_response()),
    )
    async def document_images():
        return await invoke(gateway.document_images)

    @router.get(
        "/v1/vids/document/scenes",
        openapi_extra=operation(responses=document_list_response(True)),
    )
    async def document_scenes():
        return await invoke(gateway.document_images, True)

    @router.post(
        "/v1/vids/document/images",
        openapi_extra=operation(
            body=request_body(DOCUMENT_IMAGE_REQUEST),
            responses=receipt_response(pending=True),
            idempotent=True,
        ),
    )
    async def insert_document_image(request: Request):
        body = await read_json_body(request, 15 * 1024**2)
        record = await invoke(
            gateway.insert_document_image, body, request.headers.get("idempotency-key")
        )
        return JSONResponse(record, status_code=202 if record["status"] == "unknown" else 200)

    @router.post(
        "/v1/vids/document/images/{identifier}/verify",
        openapi_extra=operation(responses=json_response(RECEIPT)),
    )
    async def verify_document_image(identifier: str):
        return await invoke(gateway.verify_document_image, identifier)

    @router.put(
        "/v1/vids/session",
        openapi_extra=operation(
            body=request_body(COOKIES_REQUEST),
            responses=json_response(SESSION_RESULT),
        ),
    )
    async def update_session(request: Request):
        body = await read_json_body(request)
        if set(body) != {"cookies"}:
            raise APIError(400, "Only cookies may be updated", "invalid_session")
        return await invoke(gateway.replace_cookies, body["cookies"])

    @router.post(
        "/v1/vids/generations",
        openapi_extra=operation(
            body=request_body(generation_request()),
            responses=receipt_response(pending=True),
            idempotent=True,
        ),
    )
    async def generate(request: Request):
        body = await read_json_body(request, 85 * 1024**2)
        if body.get("kind") == "video" and "source_id" in body:
            raise APIError(400, "Upload the source MP4 using video_base64", param="source_id")
        record = await invoke(gateway.generate, body, request.headers.get("idempotency-key"))
        status = 202 if record["status"] in {"generating", "received", "unknown"} else 200
        return JSONResponse(record, status_code=status)

    @router.post(
        "/v1/audio/speech",
        response_class=FileResponse,
        openapi_extra=operation(
            body=request_body(SPEECH_REQUEST),
            responses=speech_responses(),
            idempotent=True,
        ),
    )
    async def speech(request: Request):
        body = await read_json_body(request, 1024 * 1024)
        allowed = {"model", "input", "voice", "response_format"}
        unknown = set(body) - allowed
        if unknown:
            raise APIError(400, f"Unsupported speech parameter: {sorted(unknown)[0]}")
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
        record = await invoke(
            gateway.generate,
            {"kind": "speech", "prompt": text, "voice": voice},
            request.headers.get("idempotency-key"),
        )
        status = record["status"]
        if status == "completed":
            path, mime = invoke_sync(gateway.content, record["id"])
            return FileResponse(path, media_type=mime, filename=path.name)
        return speech_error_response(
            record,
            f"Speech request {record['id']} is {status}; use the Vids receipt to recover it",
        )

    @router.post(
        "/v1/images/generations",
        openapi_extra=operation(
            body=request_body(IMAGE_REQUEST),
            responses=json_response(IMAGE_RESULT, "One to four validated images"),
            idempotent=True,
        ),
    )
    async def image_generations(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise APIError(415, "Image generations require application/json")
        body = await read_json_body(request)
        return await image_response(request, body)

    @router.post(
        "/v1/images/edits",
        openapi_extra=operation(
            body=request_body(IMAGE_EDIT_REQUEST, "multipart/form-data"),
            responses=json_response(IMAGE_RESULT, "One validated edited image"),
            idempotent=True,
        ),
    )
    async def image_edits(request: Request):
        body, encoded = await read_image_edit(request)
        return await image_response(request, body, {"mode": "edit", "image_base64": encoded})

    async def image_response(request, body, source=None):
        prompt, size, response_format = validate_image_options(body)
        record = await invoke(
            gateway.generate,
            image_generation_body(body, source),
            request.headers.get("idempotency-key"),
        )
        status = record["status"]
        if status in {"generating", "received", "unknown"}:
            raise APIError(
                409,
                f"Image request {record['id']} is {status}; use the Vids receipt to recover it",
                "image_not_ready",
            )
        if status != "completed":
            raise APIError(502, f"Image request {record['id']} ended with {status}", "image_failed")
        items = []
        for index in range(body.get("n", 1)):
            path, mime = invoke_sync(gateway.content, record["id"], index)
            url = request.url_for("content", generation_id=record["id"])
            if index:
                url = url.include_query_params(index=index)
            items.append(image_result_item(path, mime, size, response_format, str(url)))
        return {"created": record["created_at"], "data": items}

    @router.post(
        "/v1/vids/session/refresh",
        openapi_extra=operation(
            body=request_body(EMPTY_JSON_REQUEST),
            responses=json_response(SESSION_RESULT),
        ),
    )
    async def refresh_session(request: Request):
        body = await read_json_body(request)
        if body != {}:
            raise APIError(400, "Refresh accepts an empty JSON object", "invalid_session")
        return await invoke(gateway.refresh_cookies)

    @router.get(
        "/v1/vids/generations/{generation_id}",
        openapi_extra=operation(responses=json_response(RECEIPT)),
    )
    async def retrieve(generation_id: str):
        return gateway.public(invoke_sync(gateway.get, generation_id))

    @router.post(
        "/v1/vids/generations/{generation_id}/download",
        openapi_extra=operation(responses=json_response(RECEIPT)),
    )
    async def retry_download(generation_id: str):
        return await invoke(gateway.retry_download, generation_id)

    @router.get(
        "/v1/vids/generations/{generation_id}/content",
        response_class=FileResponse,
        openapi_extra=operation(responses=file_response()),
    )
    async def content(generation_id: str, index: int = 0):
        path, mime = invoke_sync(gateway.content, generation_id, index)
        return FileResponse(path, media_type=mime, filename=path.name)

    app.include_router(router)
    app.include_router(create_vids_videos_router(gateway, authenticate))
    return app
