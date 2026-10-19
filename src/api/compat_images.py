"""Fixed-SKU images for existing OpenAI gateway channels."""

import base64
import time

from fastapi import APIRouter, Depends, Request

from ..core.auth import verify_openai_api_key
from ..core.media_skus import MEDIA_SKUS
from .compat import generate_sku_completion
from .compat_schemas import image_schema
from .openai_common import APIError, OpenAIRoute, load_generated_asset, read_json_body
from .openai_images import FIELDS, _read_edit

router = APIRouter(
    prefix="/compat/v1",
    tags=["Gateway compatibility"],
    route_class=OpenAIRoute,
    dependencies=[Depends(verify_openai_api_key)],
)


async def _generate_image(request: Request, body: dict, images: list) -> dict:
    unknown = set(body) - FIELDS - {"user"}
    if unknown:
        raise APIError(400, "Unsupported image parameter", param=sorted(unknown)[0])
    model = body.get("model")
    if not isinstance(model, str) or MEDIA_SKUS.get(model, {}).get("type") != "image":
        raise APIError(400, "model must be a supported image SKU", param="model")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise APIError(400, "prompt must contain 1 to 32000 characters", param="prompt")
    if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        raise APIError(400, "Only n=1 is supported", param="n")
    response_format = body.get("response_format", "url")
    if response_format not in ("url", "b64_json"):
        raise APIError(400, "response_format must be url or b64_json", param="response_format")
    content = [{"type": "text", "text": prompt.strip()}] + [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"},
        }
        for data, mime in images
    ]
    chat_body = {
        key: value
        for key, value in body.items()
        if key not in {"prompt", "response_format", "user"}
    }
    chat_body["messages"] = [{"role": "user", "content": content}]
    payload = await generate_sku_completion(request, chat_body)
    uri = payload["media"]["url"]
    if response_format == "b64_json":
        data, _ = await load_generated_asset(uri, "image")
        item = {"b64_json": base64.b64encode(data).decode("ascii")}
    else:
        item = {"url": uri}
    return {"created": int(time.time()), "data": [item], "usage": payload["usage"]}


@router.post("/images/generations", openapi_extra=image_schema())
async def image_generations(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise APIError(415, "Image generations require application/json")
    return await _generate_image(request, await read_json_body(request), [])


@router.post("/images/edits", openapi_extra=image_schema(edit=True))
async def image_edits(request: Request):
    body, images = await _read_edit(request)
    return await _generate_image(request, body, images)
