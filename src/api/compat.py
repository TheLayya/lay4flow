"""Fixed media SKUs and completed-result billing for unmodified OpenAI gateways."""

import asyncio
import hashlib
import html
import json
import os
import time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from ..core.auth import verify_openai_api_key
from ..core.media_skus import SKU_ALIASES, get_sku_entry, list_sku_models, prepare_sku_request
from ..core.models import ChatCompletionRequest
from . import routes
from .compat_schemas import ERROR_RESPONSES, MODEL_SCHEMA, chat_schema, json_response, list_schema
from .openai_common import APIError, OpenAIRoute, load_generated_asset, read_json_body
from .openai_images import image_dimensions
from .openai_videos import VIDEO_SIZES, video_dimensions

# These are media settlement units, not upstream text-token measurements.
SETTLEMENT_USAGE = {"prompt_tokens": 1000, "completion_tokens": 0, "total_tokens": 1000}
router = APIRouter(
    prefix="/compat/v1",
    route_class=OpenAIRoute,
    dependencies=[Depends(verify_openai_api_key)],
    tags=["Media SKU compatibility"],
)


def _validate_resolved_sku(entry, normalized, status=400):
    options = normalized.generation_options or {}
    for name, expected in entry["locked_spec"].items():
        default = "standard" if name == "priority" else None
        if options.get(name, default) != expected:
            raise APIError(status, f"Requested parameters conflict with SKU {name}", param=name)
    if entry["type"] == "video" and options.get("mode") not in entry["allowed_modes"]:
        raise APIError(
            status, "This generation mode is unavailable for the selected SKU", param="mode"
        )


def _validate_input_shapes(parsed):
    for message in parsed.messages or []:
        if not isinstance(message.content, list):
            continue
        for part in message.content:
            kind = part.get("type")
            if kind == "text":
                if not isinstance(part.get("text"), str):
                    raise APIError(400, "Text content must contain a string", param="messages")
            elif kind in {"image_url", "video_url"}:
                reference = part.get(kind)
                if (
                    not isinstance(reference, dict)
                    or not isinstance(reference.get("url"), str)
                    or not reference["url"].strip()
                ):
                    raise APIError(400, f"{kind} must contain a nonempty URL", param="messages")
    if parsed.contents and not parsed.messages:
        try:
            routes._coerce_gemini_contents(parsed.contents)
        except ValidationError:
            raise APIError(400, "Invalid Gemini content", param="contents") from None


def _select_video_mode(parsed, entry):
    """Choose a callable default from typed inputs; the resolver verifies real inputs."""
    if entry["type"] != "video":
        return
    video_config = parsed.generationConfig.model_extra["videoConfig"]
    if video_config.get("mode", "auto") != "auto":
        return
    count = 0
    if parsed.messages:
        content = parsed.messages[-1].content
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                reference = item.get("image_url")
                if item.get("type") == "image_url" and isinstance(reference, dict):
                    uri = reference.get("url")
                    if isinstance(uri, str) and not uri.startswith("extend://"):
                        count += 1
        if not count and parsed.image:
            count = 1
    elif parsed.contents:
        contents = routes._coerce_gemini_contents(parsed.contents)
        content = next(
            (item for item in reversed(contents) if (item.role or "user") == "user"),
            contents[-1],
        )
        for part in content.parts:
            if part.inlineData and part.inlineData.mimeType.lower().startswith("image/"):
                count += 1
            elif part.fileData:
                mime = part.fileData.mimeType or routes._guess_mime_type(part.fileData.fileUri, "")
                if not mime or mime.lower().startswith("image/"):
                    count += 1
    mode = (
        "image_to_video" if count == 1 else "reference_to_video" if count > 1 else "text_to_video"
    )
    if mode in entry["allowed_modes"]:
        video_config["mode"] = mode


async def _validated_media(request, payload, entry, normalized):
    choices = payload.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    media = payload.get("media") or message.get("media")
    if (
        not isinstance(media, dict)
        or media.get("type") != entry["type"]
        or not isinstance(media.get("url"), str)
        or not media["url"]
    ):
        raise APIError(502, "Generation returned no structured media result", "missing_asset")
    if any(
        (warning.get("code") if isinstance(warning, dict) else warning) == "upscale_failed"
        for warning in payload.get("warnings", [])
    ):
        raise APIError(502, "The requested resolution could not be produced", "upscale_failed")

    data, _ = await load_generated_asset(media["url"], entry["type"])
    options = normalized.generation_options
    if entry["type"] == "image":
        dimensions = image_dimensions(data)
        minimum = {"1k": 1000, "2k": 2000, "4k": 4000}[options["imageSize"]]
        if not dimensions or max(dimensions[:2]) < minimum:
            raise APIError(
                502, "Generated image is below the requested resolution", "output_size_mismatch"
            )
        ratio_width, ratio_height = (int(value) for value in options["aspectRatio"].split(":"))
        expected_ratio = ratio_width / ratio_height
        if abs(dimensions[0] / dimensions[1] / expected_ratio - 1) > 0.03:
            raise APIError(
                502,
                "Generated image differs from the requested aspect ratio",
                "output_aspect_mismatch",
            )
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
            dimensions[2]
        ]
    else:
        dimensions = video_dimensions(data)
        expected = next(
            size
            for size, spec in VIDEO_SIZES.items()
            if spec == (options["aspectRatio"], options["resolution"])
        )
        if not dimensions or f"{dimensions[0]}x{dimensions[1]}" != expected:
            raise APIError(
                502, "Generated video dimensions differ from the SKU", "output_size_mismatch"
            )
        duration = routes._mp4_duration_ms(data)
        expected_duration = (
            options.get("trimEndMs", 10000) - options.get("trimStartMs", 0)
            if options.get("mode") == "edit"
            else options["durationSeconds"] * 1000
        )
        if duration is None or abs(duration - expected_duration) > 250:
            raise APIError(
                502, "Generated video duration differs from the SKU", "output_duration_mismatch"
            )
        extension = ".mp4"

    result = dict(media)
    if media["url"].startswith("data:"):
        # Keep a large Base64 asset out of gateway SSE scanners and token estimators.
        handler = routes._ensure_generation_handler()
        filename = hashlib.sha256(data).hexdigest()[:32] + extension
        path = handler.file_cache.cache_dir / filename

        def write():
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.pending")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

        await asyncio.to_thread(write)
        base_url = handler._get_base_url({"base_url": routes._get_request_base_url(request)})
        result["url"] = f"{base_url.rstrip('/')}/tmp/{filename}"
    elif media["url"].startswith("/tmp/"):
        handler = routes._ensure_generation_handler()
        base_url = handler._get_base_url({"base_url": routes._get_request_base_url(request)})
        result["url"] = base_url.rstrip("/") + media["url"]
    return result


async def generate_sku_completion(request: Request, body: dict) -> dict:
    """Finish generation and validate the paid specification before returning usage."""
    sku = body.get("model")
    try:
        prepared = prepare_sku_request(body)
        if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
            raise APIError(400, "Only n=1 is supported", param="n")
        if type(body.get("stream", False)) is not bool:
            raise APIError(400, "stream must be a boolean", param="stream")
        parsed = ChatCompletionRequest.model_validate(prepared)
    except ValueError as exc:
        message = "Invalid chat request" if isinstance(exc, ValidationError) else str(exc)
        raise APIError(400, message) from None
    entry = get_sku_entry(sku)
    _validate_input_shapes(parsed)
    _select_video_mode(parsed, entry)
    normalized = await routes._normalize_openai_request(parsed, clamp_default_edit_trim=True)
    if not normalized.prompt:
        raise APIError(400, "Prompt cannot be empty")
    _validate_resolved_sku(entry, normalized)
    payload = routes._parse_handler_result(
        await routes._collect_non_stream_result(
            normalized.model,
            normalized.prompt,
            normalized.images,
            base_url_override=routes._get_request_base_url(request),
            video_media_id=normalized.video_media_id,
            video_bytes=normalized.video_bytes,
            video_mime_type=normalized.video_mime_type,
            generation_options=normalized.generation_options,
            response_model=sku,
        )
    )
    if "error" in payload:
        error = payload["error"] if isinstance(payload["error"], dict) else {}
        status = routes._get_error_status_code(payload)
        raise APIError(
            status if 400 <= status <= 599 else 502,
            error.get("message", "Media generation failed"),
            error.get("code", "generation_failed"),
        )
    _validate_resolved_sku(entry, normalized, status=502)
    media = await _validated_media(request, payload, entry, normalized)
    uri = media["url"]
    content = (
        f"![Generated Image]({uri})"
        if entry["type"] == "image"
        else f'<video src="{html.escape(uri, quote=True)}" controls></video>'
    )
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": sku,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "media": media},
                "finish_reason": "stop",
            }
        ],
        "media": media,
        "usage": dict(SETTLEMENT_USAGE),
    }


async def _completed_stream(payload):
    common = {name: payload[name] for name in ("id", "created", "model")}
    common["object"] = "chat.completion.chunk"
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": payload["choices"][0]["message"]["content"],
                        "media": payload["media"],
                    },
                    "finish_reason": None,
                }
            ],
            "media": payload["media"],
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": payload["usage"]},
    ]
    for chunk in chunks:
        yield "data: " + json.dumps({**common, **chunk}, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


@router.get(
    "/models",
    openapi_extra={"responses": {**ERROR_RESPONSES, **json_response(list_schema(MODEL_SCHEMA))}},
)
async def list_models():
    return {"object": "list", "data": list_sku_models()}


@router.get(
    "/models/{model}",
    openapi_extra={"responses": {**ERROR_RESPONSES, **json_response(MODEL_SCHEMA)}},
)
async def get_model(model: str):
    for entry in list_sku_models():
        if entry["id"] == SKU_ALIASES.get(model, model):
            return {**entry, "id": model}
    raise APIError(404, "Model not found", "not_found", "model")


@router.post("/chat/completions", openapi_extra=chat_schema())
async def create_chat_completion(request: Request):
    body = await read_json_body(request, 101 * 1024 * 1024)
    payload = await generate_sku_completion(request, body)
    if body.get("stream", False):
        return StreamingResponse(
            _completed_stream(payload),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return payload
