"""Fixed-SKU asynchronous video requests sharing the existing durable job service."""

from contextlib import aclosing

from fastapi import Request

from ..core.media_skus import MEDIA_SKUS, get_sku_entry, prepare_sku_request
from ..services.video_jobs import VideoJobError
from .compat import _validate_resolved_sku
from .compat_schemas import ERROR_RESPONSES, VIDEO_SCHEMA, json_response, list_schema
from .openai_common import APIError, load_generated_asset, parse_json_object_form
from .openai_images import image_dimensions
from .openai_videos import (
    FIELDS,
    create_videos_router,
    execute_video,
    normalize_video_body,
    read_video_request,
)

COMPAT_FIELDS = FIELDS | {
    "mode",
    "quality",
    "priority",
    "resolution",
    "aspectRatio",
    "aspect_ratio",
    "durationSeconds",
    "duration_seconds",
    "generation_config",
    "videoConfig",
    "video_config",
    "trimStartMs",
    "trim_start_ms",
    "trimEndMs",
    "trim_end_ms",
    "extra_body",
    "extraBody",
}


async def normalize_sku_video_request(request: Request):
    body, files = await read_video_request(request, COMPAT_FIELDS, array_references=True)
    return await normalize_sku_video_body(request, body, files)


async def normalize_sku_video_body(request: Request, body: dict, files: dict):
    """Keep paid SKU identity while reusing public video material and mode resolution."""
    unknown = set(body) - COMPAT_FIELDS
    if unknown:
        raise APIError(400, "Unsupported video parameter", param=sorted(unknown)[0])
    sku = body.get("model")
    entry = get_sku_entry(sku)
    if not entry or entry["type"] != "video":
        raise APIError(400, "model must be a supported video SKU", param="model")
    body = dict(body)
    for name in (
        "generationConfig",
        "generation_config",
        "videoConfig",
        "video_config",
        "extra_body",
        "extraBody",
    ):
        if name in body and isinstance(body[name], str):
            body[name] = parse_json_object_form(body[name], name)
    try:
        prepared = prepare_sku_request(body)
    except ValueError as error:
        raise APIError(400, str(error)) from None
    prompt = prepared.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise APIError(400, "prompt must contain 1 to 32000 characters", param="prompt")
    canonical = {key: value for key, value in prepared.items() if key in FIELDS}
    files = dict(files)
    references = body.get("input_reference")
    if references is not None:
        if "input_reference" in files:
            raise APIError(400, "Use uploaded or URL references, not both", param="input_reference")
        if not isinstance(references, list):
            references = [references]
        if not 1 <= len(references) <= 7:
            raise APIError(400, "input_reference accepts 1 to 7 images", param="input_reference")
        loaded = []
        total = 0
        for reference in references:
            reference = parse_json_object_form(reference, "input_reference")
            if set(reference) != {"image_url"} or not isinstance(reference["image_url"], str):
                raise APIError(400, "input_reference requires image_url", param="input_reference")
            try:
                data, _ = await load_generated_asset(
                    reference["image_url"], "image", allow_local=False
                )
            except APIError as error:
                raise APIError(
                    413 if error.code == "asset_too_large" else 400,
                    "Invalid input reference image",
                    param="input_reference",
                ) from None
            total += len(data)
            if len(data) > 20 * 1024 * 1024 or total > 100 * 1024 * 1024:
                raise APIError(413, "Input images exceed the media limit", "request_too_large")
            dimensions = image_dimensions(data)
            if not dimensions:
                raise APIError(
                    400, "Reference must be a PNG, JPEG or WebP image", param="input_reference"
                )
            loaded.append((data, dimensions[2]))
        files["input_reference"] = loaded
        canonical.pop("input_reference", None)
    references = files.get("input_reference", [])
    if isinstance(references, tuple):
        references = [references]
    options = canonical["generationConfig"]["videoConfig"]
    if entry["allowed_modes"] == ["edit"] and "input_video" not in files:
        raise APIError(400, "Editing requires an uploaded input_video", param="input_video")
    if options.get("mode", "auto") == "auto":
        options["mode"] = (
            "image_to_video"
            if len(references) == 1
            else "reference_to_video"
            if references
            else "text_to_video"
        )
    normalized, params = await normalize_video_body(
        request, canonical, files, clamp_default_edit_trim=True
    )
    _validate_resolved_sku(entry, normalized)
    normalized.public_model = sku
    params["model"] = sku
    return normalized, params


async def execute_sku_video(normalized, base_url):
    """Yield completion only after actual dimensions and paid duration are verified."""
    entry = get_sku_entry(normalized.public_model)
    if not entry or entry["type"] != "video":
        raise VideoJobError(400, "invalid_request", "A supported video SKU is required")
    try:
        _validate_resolved_sku(entry, normalized)
    except APIError as error:
        raise VideoJobError(error.status_code, error.code, error.message) from None
    options = normalized.generation_options
    duration = options.get("durationSeconds")
    if options.get("mode") == "edit":
        duration = (options.get("trimEndMs", 10000) - options.get("trimStartMs", 0)) / 1000
    async with aclosing(execute_video(normalized, base_url, expected_duration=duration)) as stream:
        async for event in stream:
            if event.get("type") == "completed":
                try:
                    _validate_resolved_sku(entry, normalized, status=502)
                except APIError as error:
                    raise VideoJobError(error.status_code, error.code, error.message) from None
            yield event


async def execute_compat_video(normalized, base_url):
    """Dispatch one shared job service without duplicating its database ownership lock."""
    executor = execute_sku_video if get_sku_entry(normalized.public_model) else execute_video
    async with aclosing(executor(normalized, base_url)) as stream:
        async for event in stream:
            yield event


def create_compat_videos_router(get_service):
    router = create_videos_router(
        get_service, prefix="/compat", normalize_request=normalize_sku_video_request
    )
    reference_schema = {
        "type": "object",
        "required": ["image_url"],
        "additionalProperties": False,
        "properties": {"image_url": {"type": "string", "minLength": 1}},
    }
    properties = {
        "model": {
            "type": "string",
            "enum": [name for name, entry in MEDIA_SKUS.items() if entry["type"] == "video"],
        },
        "prompt": {"type": "string", "minLength": 1, "maxLength": 32000},
        "seconds": {
            "oneOf": [{"type": "string"}, {"type": "integer"}],
            "description": "Must match the selected SKU; omitted for editing.",
        },
        "size": {
            "type": "string",
            "description": "Supported video pixel size matching the SKU resolution.",
        },
        "generationConfig": {
            "type": "object",
            "description": "videoConfig: aspectRatio/mode; editing accepts trimStartMs/trimEndMs.",
            "properties": {
                "videoConfig": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": [
                                "auto",
                                "text_to_video",
                                "image_to_video",
                                "first_last_frame",
                                "reference_to_video",
                                "edit",
                            ],
                        },
                        "aspectRatio": {"type": "string", "enum": ["16:9", "9:16"]},
                        "trimStartMs": {"type": "integer", "minimum": 0},
                        "trimEndMs": {"type": "integer", "minimum": 1},
                    },
                }
            },
        },
        "input_reference": {
            "oneOf": [
                reference_schema,
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 7,
                    "items": reference_schema,
                },
            ]
        },
    }
    json_schema = {"type": "object", "required": ["model", "prompt"], "properties": properties}
    multipart_properties = {
        **properties,
        "input_reference": {"type": "string", "format": "binary"},
        "input_reference[]": {
            "type": "array",
            "maxItems": 7,
            "items": {"type": "string", "format": "binary"},
        },
        "input_video": {"type": "string", "format": "binary"},
    }
    multipart_properties["generationConfig"] = {
        "type": "string",
        "description": "JSON-encoded videoConfig.",
    }
    for route in router.routes:
        if route.path == "/compat/v1/videos" and "POST" in route.methods:
            route.openapi_extra = {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": json_schema},
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["model", "prompt"],
                                "properties": multipart_properties,
                            }
                        },
                    },
                }
            }
        methods = getattr(route, "methods", set())
        if route.path.endswith("/content"):
            responses = {
                "200": {
                    "description": "Completed MP4 content",
                    "content": {"video/mp4": {"schema": {"type": "string", "format": "binary"}}},
                },
                "409": {"description": "Video not completed"},
                "410": {"description": "Content expired"},
            }
        elif "DELETE" in methods:
            responses = json_response(
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "object": {"type": "string", "enum": ["video.deleted"]},
                        "deleted": {"type": "boolean"},
                    },
                }
            )
        elif route.path == "/compat/v1/videos" and "GET" in methods:
            responses = json_response(list_schema(VIDEO_SCHEMA))
        else:
            responses = json_response(VIDEO_SCHEMA, "Video job snapshot; no settlement usage")
        route.openapi_extra = {
            **(route.openapi_extra or {}),
            "responses": {**ERROR_RESPONSES, **responses},
        }
    return router
