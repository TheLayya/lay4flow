"""OpenAI image endpoints backed by the existing media generation pipeline."""

import base64
import struct
import time

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import UploadFile

from ..core.auth import verify_openai_api_key
from ..core.models import ChatCompletionRequest
from ..core.public_models import PUBLIC_MODELS
from .openai_common import (
    APIError,
    OpenAIRoute,
    limit_request_body,
    parse_json_object_form,
    read_json_body,
)

IMAGE_LIMIT = 20 * 1024 * 1024
TOTAL_IMAGE_LIMIT = 100 * 1024 * 1024
DEFAULT_MODEL = "gemini-3.1-flash-image"
FIELDS = {"model", "prompt", "n", "size", "quality", "response_format", "generationConfig"}


def _reject(message, status=400):
    raise APIError(status, message)


def image_dimensions(data):
    """Read PNG/JPEG/WebP dimensions without decoding pixels or extra dependencies."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33 and data[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", data, 16)
        if width and height:
            return width, height, "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                break
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset : offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if (
                marker
                in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
                and length >= 8
            ):
                height, width = struct.unpack_from(">HH", data, offset + 3)
                if width and height:
                    return width, height, "image/jpeg"
            offset += length
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            return (
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
                "image/webp",
            )
        if chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, "image/webp"
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack_from("<HH", data, 26)
            if width & 0x3FFF and height & 0x3FFF:
                return width & 0x3FFF, height & 0x3FFF, "image/webp"
    return None


def _validate_options(body):
    if not isinstance(body, dict):
        _reject("Request body must be a JSON object")
    unknown = set(body) - FIELDS
    if unknown:
        _reject(f"Unsupported parameter: {sorted(unknown)[0]}")
    model = body.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or PUBLIC_MODELS.get(model, {}).get("type") != "image":
        _reject("model must be a public image model")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        _reject("prompt must contain 1 to 32000 characters")
    if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        _reject("Only n=1 is supported")
    if body.get("response_format", "url") not in ("url", "b64_json"):
        _reject("response_format must be url or b64_json")
    if body.get("quality", "auto") not in ("auto", "standard"):
        _reject(
            "Only quality=auto or standard is supported; use imageConfig.imageSize for upscaling"
        )
    size = body.get("size", "auto")
    if size != "auto":
        _reject(
            "Only size=auto is supported; use generationConfig.imageConfig for Flow aspectRatio/imageSize"
        )
    generation_config = body.get("generationConfig", {})
    if not isinstance(generation_config, dict) or set(generation_config) - {"imageConfig"}:
        _reject("generationConfig supports only imageConfig")
    image_config = generation_config.get("imageConfig", {})
    if not isinstance(image_config, dict) or set(image_config) - {"aspectRatio", "imageSize"}:
        _reject("imageConfig supports only aspectRatio and imageSize")
    if any(not isinstance(value, str) or not value.strip() for value in image_config.values()):
        _reject("imageConfig values must be nonempty strings")
    return model, prompt.strip()


async def _read_edit(request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "multipart/form-data":
        _reject("Image edits require multipart/form-data", 415)
    limit_request_body(request, TOTAL_IMAGE_LIMIT + 1024 * 1024)
    body, images, total = {}, [], 0
    async with request.form(max_files=10, max_fields=20, max_part_size=IMAGE_LIMIT) as form:
        for name, value in form.multi_items():
            if name in ("image", "image[]"):
                if not isinstance(value, UploadFile):
                    _reject("image must be an uploaded file")
                if len(images) >= 10:
                    _reject("At most 10 input images are supported")
                if value.size is not None and value.size > IMAGE_LIMIT:
                    _reject("Each image must not exceed 20 MiB", 413)
                data = await value.read(IMAGE_LIMIT + 1)
                total += len(data)
                if len(data) > IMAGE_LIMIT or total > TOTAL_IMAGE_LIMIT:
                    _reject("Input images exceed the 20 MiB per-file or 100 MiB total limit", 413)
                dimensions = image_dimensions(data)
                if not dimensions:
                    _reject("Input must be a PNG, JPEG or WebP image")
                images.append((data, dimensions[2]))
            else:
                if name not in FIELDS or isinstance(value, UploadFile):
                    _reject(f"Unsupported parameter: {name}")
                if name in body:
                    _reject(f"Duplicate parameter: {name}")
                body[name] = value
        if not images:
            _reject("At least one image file is required")
        if "n" in body:
            if body["n"] != "1":
                _reject("Only n=1 is supported")
            body["n"] = 1
        if "generationConfig" in body:
            body["generationConfig"] = parse_json_object_form(body["generationConfig"])
    return body, images


async def _generate(request, body, images):
    from . import routes
    from .openai_common import load_generated_asset

    model, prompt = _validate_options(body)
    content = [{"type": "text", "text": prompt}] + [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"},
        }
        for data, mime in images
    ]
    chat_body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "generationConfig": body.get("generationConfig", {}),
    }
    normalized = await routes._normalize_openai_request(
        ChatCompletionRequest.model_validate(chat_body)
    )
    result = routes._parse_handler_result(
        await routes._collect_non_stream_result(
            normalized.model,
            normalized.prompt,
            normalized.images,
            base_url_override=routes._get_request_base_url(request),
            generation_options=normalized.generation_options,
            response_model=normalized.public_model,
        )
    )
    if "error" in result:
        error = result["error"] if isinstance(result["error"], dict) else {}
        _reject(
            error.get("message", "Image generation failed"), routes._get_error_status_code(result)
        )
    choices = result.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    media = message.get("media")
    if (
        not isinstance(media, dict)
        or media.get("type") != "image"
        or not isinstance(media.get("url"), str)
        or not media["url"]
    ):
        _reject("Generation returned no structured image result", 502)
    warnings = result.get("warnings", [])
    if (normalized.generation_options or {}).get("imageSize") in ("2k", "4k") and any(
        (item.get("code") if isinstance(item, dict) else item) == "upscale_failed"
        for item in warnings
    ):
        _reject("Image upscaling failed; the requested resolution was not produced", 502)
    uri = media["url"]
    response_format = body.get("response_format", "url")
    data = None
    if response_format == "b64_json":
        data, _ = await load_generated_asset(uri, "image")
        actual = image_dimensions(data)
        if not actual:
            _reject("Generated asset is not a supported image", 502)
    item = (
        {"url": uri}
        if response_format == "url"
        else {"b64_json": base64.b64encode(data).decode("ascii")}
    )
    return {"created": int(time.time()), "data": [item]}


def create_images_router():
    router = APIRouter(route_class=OpenAIRoute, dependencies=[Depends(verify_openai_api_key)])
    properties = {
        "prompt": {"type": "string", "minLength": 1, "maxLength": 32000},
        "model": {
            "type": "string",
            "default": DEFAULT_MODEL,
            "enum": [name for name, value in PUBLIC_MODELS.items() if value["type"] == "image"],
        },
        "n": {"type": "integer", "enum": [1], "default": 1},
        "size": {
            "type": "string",
            "default": "auto",
            "enum": ["auto"],
            "description": "Only auto is supported; use generationConfig.imageConfig for Flow size/aspect",
        },
        "quality": {"type": "string", "enum": ["auto", "standard"], "default": "auto"},
        "response_format": {"type": "string", "enum": ["url", "b64_json"], "default": "url"},
        "generationConfig": {
            "type": "object",
            "description": "Flow extension: imageConfig.aspectRatio and imageConfig.imageSize",
        },
    }

    def request_schema(editing):
        fields = dict(properties)
        if editing:
            fields["image"] = {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {"type": "string", "format": "binary"},
            }
            fields["generationConfig"] = {
                "type": "string",
                "description": "JSON-encoded generationConfig object",
            }
        return {
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data" if editing else "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["prompt", "image"] if editing else ["prompt"],
                            "additionalProperties": False,
                            "properties": fields,
                        }
                    }
                },
            }
        }

    async def execute(request, editing):
        if editing:
            body, images = await _read_edit(request)
        else:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
            ):
                _reject("Image generations require application/json", 415)
            body = await read_json_body(request)
            images = []
        return await _generate(request, body, images)

    @router.post("/v1/images/generations", tags=["Images"], openapi_extra=request_schema(False))
    async def image_generations(request: Request):
        return await execute(request, False)

    @router.post("/v1/images/edits", tags=["Images"], openapi_extra=request_schema(True))
    async def image_edits(request: Request):
        return await execute(request, True)

    return router
