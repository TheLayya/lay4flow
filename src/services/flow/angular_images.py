"""Pure JSON-proto contracts for Flow's current Angular image RPC.

Field numbers come from the public 2026-10-21 frontend bundle. GetModels
provides usage keys; legacy image-model enums are not request usage keys.
"""

from typing import Any

from .media import extract_image_url_from_media

IMAGE_RPC = "ogiZ0b"
MODELS_RPC = "HTrJv"

_ASPECTS = {
    "IMAGE_ASPECT_RATIO_SQUARE": 1,
    "IMAGE_ASPECT_RATIO_PORTRAIT": 2,
    "IMAGE_ASPECT_RATIO_LANDSCAPE": 3,
    "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": 4,
    "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": 5,
    "SQUARE": 1,
    "PORTRAIT": 2,
    "LANDSCAPE": 3,
    "PORTRAIT_3_4": 4,
    "LANDSCAPE_4_3": 5,
    "1:1": 1,
    "9:16": 2,
    "16:9": 3,
    "3:4": 4,
    "4:3": 5,
}
_IMAGE_INPUT_TYPES = {"IMAGE_INPUT_TYPE_REFERENCE": 1, "IMAGE_INPUT_TYPE_BASE_IMAGE": 2}


def _field(value: Any, number: int, default: Any = None) -> Any:
    return value[number - 1] if isinstance(value, list) and len(value) >= number else default


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Flow image {label} is missing")
    return value.strip()


def _aspect(value: Any) -> int:
    if isinstance(value, str) and value in _ASPECTS:
        return _ASPECTS[value]
    if type(value) is int and 0 <= value <= 5:
        return value
    raise ValueError("Flow image aspect ratio is invalid")


def build_image_request(
    project_id: str,
    prompt: str,
    model_name: str,
    aspect_ratio: str,
    captcha_token: str,
    seed: int,
    batch_id: str,
    media_id: str,
    workflow_id: str,
    image_inputs: list[dict[str, Any]] | None = None,
) -> list:
    """Build one image request; model_name must be a resolved GetModels usage key.

    media_id and workflow_id are fresh client UUID seeds, matching the frontend's
    gp/Ks values. The response supplies the actual media/workflow identifiers.
    """
    project_id = _string(project_id, "project")
    model_name = _string(model_name, "model usage key")
    captcha_token = _string(captcha_token, "captcha")
    batch_id = _string(batch_id, "batch ID")
    media_id = _string(media_id, "media seed")
    workflow_id = _string(workflow_id, "workflow seed")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Flow image prompt is missing")
    if type(seed) is not int or not 0 <= seed < 2147483647:
        raise ValueError("Flow image seed is invalid")
    if image_inputs is not None and not isinstance(image_inputs, list):
        raise ValueError("Flow image inputs are invalid")
    inputs = []
    for item in image_inputs or []:
        if not isinstance(item, dict):
            raise ValueError("Flow image input is invalid")
        kind = item.get("imageInputType", "IMAGE_INPUT_TYPE_REFERENCE")
        if kind not in _IMAGE_INPUT_TYPES:
            raise ValueError("Flow image input type is invalid")
        inputs.append(
            [_string(item.get("name"), "input media"), None, None, None, _IMAGE_INPUT_TYPES[kind]]
        )
    # TM: tool=22, projectId=6, reCAPTCHA=(token, WEB=1) at field11.
    context = [None, 22, None, None, None, project_id, None, None, None, None, [captcha_token, 1]]
    # fz: inputs=3, seed=4, aspect=5, model usage=6, context=8,
    # structured prompt=9, new workflow seed=13, new media seed=14.
    request = [
        None,
        None,
        inputs,
        seed,
        _aspect(aspect_ratio),
        model_name,
        None,
        context,
        [[[prompt]]],
        None,
        [],
        [],
        workflow_id,
        media_id,
    ]
    # u6a: requests=2, useNewMedia=3, shared context=4, batch metadata=5.
    return [None, [request], True, context, [batch_id]]


def parse_image_response(payload: Any) -> dict[str, Any]:
    """Convert the RPC's bz image protos to the existing generation media shape."""
    if not isinstance(payload, list):
        raise ValueError("Flow image response is invalid")
    protos = _field(payload, 1, [])
    if not isinstance(protos, list):
        raise ValueError("Flow image media list is invalid")
    media = []
    for proto in protos:
        if not isinstance(proto, list):
            raise ValueError("Flow image media is invalid")
        name = _string(_field(proto, 1), "response media name")
        workflow = _string(_field(proto, 3), "response workflow")
        metadata = _field(proto, 6)
        status = _field(_field(metadata, 9), 1)
        if status is not None and (type(status) is not int or status not in (1, 2, 3, 4, 5, 6, 7)):
            raise ValueError("Flow image generation status is invalid")
        if status in (4, 5, 7):
            raise ValueError("Flow image generation failed")
        if status in (1, 2, 6):
            raise ValueError("Flow image generation is incomplete")
        image = _field(proto, 7)
        generated = _field(image, 1)
        if not isinstance(generated, list):
            raise ValueError("Flow image response has no generated image")
        candidate = {
            "name": name,
            "workflowId": workflow,
            "image": {"generatedImage": {"fifeUrl": _field(generated, 14)}},
        }
        url = extract_image_url_from_media([candidate])
        if url is None:
            raise ValueError("Flow image response URL is invalid")
        candidate["image"]["generatedImage"]["fifeUrl"] = url
        media.append(candidate)
    workflows = _field(payload, 2, [])
    if not isinstance(workflows, list):
        raise ValueError("Flow image workflows are invalid")
    return {"media": media, "workflows": workflows}


def parse_image_models(payload: Any) -> list[dict[str, Any]]:
    """Read current GetModels response identity, usage and capability fields."""
    config = _field(payload, 1)
    models = _field(config, 6)
    if not isinstance(models, list):
        raise ValueError("Flow image model config is invalid")
    result = []
    for model in models:
        usages = _field(model, 2, [])
        aliases = _field(model, 5, [])
        if (
            not isinstance(model, list)
            or not isinstance(usages, list)
            or not isinstance(aliases, list)
        ):
            raise ValueError("Flow image model is invalid")
        result.append(
            {
                "id": _string(_field(model, 4), "model ID"),
                "display_name": _field(model, 1, ""),
                "aliases": [value for row in aliases if isinstance((value := _field(row, 1)), str)],
                "usages": usages,
            }
        )
    return result


def resolve_image_usage(
    payload: Any,
    model_name: str,
    aspect_ratio: str,
    service_tier: int,
    has_reference_images: bool = False,
) -> str:
    """Resolve model ID/alias or an exact usage against current official config."""
    wanted = _string(model_name, "model").casefold()
    aspect = _aspect(aspect_ratio)
    if type(service_tier) is not int or service_tier not in (1, 2, 3):
        raise ValueError("Flow image service tier is invalid")
    matches = []
    for model in parse_image_models(payload):
        names = [model["id"], model["display_name"], *model["aliases"]]
        selected = any(
            isinstance(name, str) and name.strip().casefold() == wanted for name in names
        )
        for usage in model["usages"]:
            key = _field(usage, 1)
            if selected or (isinstance(key, str) and key.casefold() == wanted):
                matches.append(usage)
    required = {1, 3} if has_reference_images else {1}
    for usage in matches:
        aspects = _field(_field(usage, 14), 1, [])
        modes = _field(_field(usage, 9), 1, [])
        pricing = _field(usage, 5, [])
        if not all(isinstance(value, list) for value in (aspects, modes, pricing)):
            continue
        price = next(
            (_field(entry, 2) for entry in pricing if _field(entry, 1) == service_tier), None
        )
        # t7a field2 is a disabled marker; field1 supplies enabled pricing.
        if (
            not isinstance(price, list)
            or _field(price, 2) is not None
            or not isinstance(_field(price, 1), list)
        ):
            continue
        if aspect in aspects and any(
            isinstance(parts := _field(mode, 1), list) and set(parts) - {0} == required
            for mode in modes
        ):
            return _string(_field(usage, 1), "model usage key")
    raise ValueError("Flow image model has no available usage for this request")
