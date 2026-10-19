"""Small, deterministic sales SKU catalog for the public media models.

The SKU layer only fixes billable generation dimensions.  The existing public
model resolver remains the authority for input counts, modes, and Flow model
availability; this module does not submit work or perform I/O. Image pixel
``size`` values are accepted as aspect-ratio hints only; they do not claim
that the returned asset has those exact dimensions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .model_resolver import OPENAI_IMAGE_SIZE_RE, _aspect_from_dimensions
from .public_models import PUBLIC_MODELS, _aspect

_IMAGE_ALIASES = {
    "aspectRatio": "aspectRatio",
    "aspect_ratio": "aspectRatio",
    "aspect": "aspectRatio",
    "imageSize": "imageSize",
    "image_size": "imageSize",
    "resolution": "imageSize",
    "size": "size",
    "quality": "quality",
    "imageQuality": "quality",
    "image_quality": "quality",
}
_VIDEO_ALIASES = {
    "mode": "mode",
    "quality": "quality",
    "durationSeconds": "durationSeconds",
    "duration_seconds": "durationSeconds",
    "resolution": "resolution",
    "aspectRatio": "aspectRatio",
    "aspect_ratio": "aspectRatio",
    "trimStartMs": "trimStartMs",
    "trim_start_ms": "trimStartMs",
    "trimEndMs": "trimEndMs",
    "trim_end_ms": "trimEndMs",
    "priority": "priority",
    "seconds": "seconds",
    "size": "size",
}
_IMAGE_QUALITY_HINTS = {"auto", "standard", "high", "hd"}
_GENERATION_NAMES = ("generationConfig", "generation_config")
_IMAGE_CONFIG_NAMES = ("imageConfig", "image_config")
_VIDEO_CONFIG_NAMES = ("videoConfig", "video_config")
_SOURCE_NAMES = ("extra_body", "extraBody")
_VIDEO_SIZES = {
    "640x360": ("16:9", "360p"),
    "360x640": ("9:16", "360p"),
    "1280x720": ("16:9", "720p"),
    "720x1280": ("9:16", "720p"),
    "1920x1080": ("16:9", "1080p"),
    "1080x1920": ("9:16", "1080p"),
    "3840x2160": ("16:9", "4k"),
    "2160x3840": ("9:16", "4k"),
}


def _add_video_sku(
    result: dict[str, dict[str, Any]],
    prefix: str,
    public_model: str,
    combination: dict[str, Any],
    mode: str,
) -> None:
    """Expand one capability combination into one grouped SKU per dimensions."""
    if mode == "extend":
        return
    is_edit = mode == "edit"
    if not is_edit and not combination.get("durations"):
        return
    quality = combination.get("quality")
    priority = combination.get("priority", "standard")
    for duration in combination["durations"] or [None]:
        for resolution in combination["resolutions"]:
            if is_edit:
                name = f"{prefix}-edit-{resolution}"
            elif public_model == "omni_1.1_flash":
                name = f"{prefix}-{duration}s-{resolution}"
            else:
                name = f"{prefix}-{quality}-{duration}s-{resolution}"
            if priority != "standard":
                name += f"-{priority}"
            entry = result.setdefault(
                name,
                {
                    "public_model": public_model,
                    "type": "video",
                    "locked_spec": {
                        **({"quality": quality} if quality is not None else {}),
                        **({"durationSeconds": duration} if duration is not None else {}),
                        "resolution": resolution,
                        "priority": priority,
                    },
                    "allowed_modes": [],
                    "description": (
                        (
                            f"{public_model} edit up to 10s {resolution}"
                            if is_edit
                            else f"{public_model} {quality or 'flash'} {duration}s {resolution}"
                        )
                        + (f" {priority}" if priority != "standard" else "")
                    ),
                    "mode_capabilities": {},
                },
            )
            if mode not in entry["allowed_modes"]:
                entry["allowed_modes"].append(mode)
            mode_caps = entry["mode_capabilities"].setdefault(
                mode, deepcopy(PUBLIC_MODELS[public_model]["capabilities"]["modes"][mode])
            )
            if combination.get("minTier"):
                mode_caps["minTier"] = combination["minTier"]


def _build_skus() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    image_prefixes = {
        "gemini-3.0-pro-image": "flow-banana-pro",
        "gemini-3.1-flash-image": "flow-banana-flash",
        "gemini-3.1-flash-image-lite": "flow-banana-lite",
        "imagen-4.0-generate-preview": "flow-imagen-4",
    }
    for public_model, prefix in image_prefixes.items():
        for image_size in PUBLIC_MODELS[public_model]["capabilities"]["imageSizes"]:
            name = f"{prefix}-{image_size}"
            result[name] = {
                "public_model": public_model,
                "type": "image",
                "locked_spec": {"imageSize": image_size},
                "allowed_modes": ["generate", "edit"],
                "description": f"{PUBLIC_MODELS[public_model]['description']} {image_size}",
            }

    for public_model, prefix in (
        ("veo_3_1", "flow-veo-3-1"),
        ("omni_1.1_flash", "flow-omni-1-1-flash"),
    ):
        for combination in PUBLIC_MODELS[public_model]["capabilities"]["combinations"]:
            _add_video_sku(result, prefix, public_model, combination, combination["mode"])
    return result


MEDIA_SKUS = _build_skus()
SKU_ALIASES = {
    f"{name}-standard": name
    for name, entry in MEDIA_SKUS.items()
    if entry["type"] == "video" and entry["locked_spec"]["priority"] == "standard"
}


def get_sku_entry(sku: Any) -> dict[str, Any] | None:
    """Accept legacy standard-priority names without duplicating the catalog."""
    return MEDIA_SKUS.get(SKU_ALIASES.get(sku, sku)) if isinstance(sku, str) else None


def list_sku_models() -> list[dict[str, Any]]:
    """Return a defensive model-list payload for the compatibility directory."""
    result = []
    for sku, entry in MEDIA_SKUS.items():
        capabilities = deepcopy(PUBLIC_MODELS[entry["public_model"]]["capabilities"])
        if entry["type"] == "image":
            capabilities["imageSizes"] = [entry["locked_spec"]["imageSize"]]
        else:
            capabilities["qualities"] = (
                [entry["locked_spec"]["quality"]] if "quality" in entry["locked_spec"] else []
            )
            capabilities["priorities"] = [entry["locked_spec"]["priority"]]
            capabilities["modes"] = deepcopy(entry["mode_capabilities"])
            capabilities["combinations"] = [
                deepcopy(combination)
                for combination in capabilities["combinations"]
                if combination["mode"] in entry["allowed_modes"]
                and combination.get("quality") == entry["locked_spec"].get("quality")
                and combination.get("priority", "standard") == entry["locked_spec"]["priority"]
                and (
                    "durationSeconds" not in entry["locked_spec"]
                    or entry["locked_spec"]["durationSeconds"] in combination["durations"]
                )
                and entry["locked_spec"]["resolution"] in combination["resolutions"]
            ]
            for combination in capabilities["combinations"]:
                combination["durations"] = (
                    [entry["locked_spec"]["durationSeconds"]]
                    if "durationSeconds" in entry["locked_spec"]
                    else []
                )
                combination["resolutions"] = [entry["locked_spec"]["resolution"]]
        defaults = {
            **deepcopy(PUBLIC_MODELS[entry["public_model"]]["defaults"]),
            **deepcopy(entry["locked_spec"]),
        }
        if entry["allowed_modes"] == ["edit"]:
            defaults.pop("durationSeconds", None)
            defaults["mode"] = "edit"
        result.append(
            {
                "id": sku,
                "object": "model",
                "created": 0,
                "owned_by": "lay4flow",
                "provider": "flow",
                **deepcopy(entry),
                "capabilities": capabilities,
                "defaults": defaults,
                "supported_endpoints": ["/compat/v1/chat/completions"]
                + (
                    ["/compat/v1/images/generations", "/compat/v1/images/edits"]
                    if entry["type"] == "image"
                    else ["/compat/v1/videos"]
                ),
            }
        )
    return result


def _sources(body: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [body]
    for name in _SOURCE_NAMES:
        if name in body:
            value = body[name]
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be an object")
            sources.append(value)
    return sources


def _sections(body: dict[str, Any], config_names: tuple[str, ...]):
    for source in _sources(body):
        yield source
        for generation_name in _GENERATION_NAMES:
            if generation_name not in source:
                continue
            generation = source[generation_name]
            if not isinstance(generation, dict):
                raise ValueError(f"{generation_name} must be an object")
            yield generation
            for config_name in config_names:
                if config_name in generation:
                    config = generation[config_name]
                    if not isinstance(config, dict):
                        raise ValueError(f"{config_name} must be an object")
                    yield config
        for config_name in config_names:
            if config_name in source:
                config = source[config_name]
                if not isinstance(config, dict):
                    raise ValueError(f"{config_name} must be an object")
                yield config


def _values(body: dict[str, Any], aliases: dict[str, str], config_names: tuple[str, ...]):
    found: dict[str, list[Any]] = {}
    for section in _sections(body, config_names):
        for name, value in section.items():
            field = aliases.get(name)
            if field:
                found.setdefault(field, []).append(value)
    return found


def _one(found: dict[str, list[Any]], field: str, normalizer):
    values = [normalizer(value) for value in found.get(field, [])]
    if values and any(value != values[0] for value in values[1:]):
        raise ValueError(f"Conflicting values for {field}")
    return values[0] if values else None


def _size_aspect(value: Any) -> str | None:
    if not isinstance(value, str):
        raise ValueError("size must contain positive pixel dimensions")
    if value.strip().lower() == "auto":
        return None
    match = OPENAI_IMAGE_SIZE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("size must contain positive pixel dimensions")
    aspect = _aspect_from_dimensions(int(match["w"]), int(match["h"]))
    if aspect is None:
        raise ValueError("size must contain positive pixel dimensions")
    return _aspect(aspect)


def _int(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("durationSeconds must be an integer")
    return value


def _str(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("SKU option must be a nonempty string")
    return value.strip().lower()


def _image_overrides(body: dict[str, Any], locked: dict[str, Any]) -> dict[str, Any]:
    found = _values(body, _IMAGE_ALIASES, _IMAGE_CONFIG_NAMES)
    image_size = _one(found, "imageSize", _str)
    if image_size and image_size != locked["imageSize"]:
        raise ValueError("imageSize conflicts with the SKU")
    qualities = [_str(value) for value in found.get("quality", [])]
    if any(value not in _IMAGE_QUALITY_HINTS for value in qualities):
        raise ValueError("Unsupported image quality hint")
    if qualities and any(value != qualities[0] for value in qualities[1:]):
        raise ValueError("Conflicting values for quality")
    aspects = [_aspect(value) for value in found.get("aspectRatio", [])]
    sizes = [aspect for value in found.get("size", []) if (aspect := _size_aspect(value))]
    all_aspects = aspects + sizes
    if all_aspects and any(value != all_aspects[0] for value in all_aspects[1:]):
        raise ValueError("Conflicting values for aspectRatio/size")
    return {"aspectRatio": all_aspects[0]} if all_aspects else {}


def _video_overrides(body: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    found = _values(body, _VIDEO_ALIASES, _VIDEO_CONFIG_NAMES)
    is_edit = entry["allowed_modes"] == ["edit"]
    if not is_edit and (found.get("trimStartMs") or found.get("trimEndMs")):
        raise ValueError("Fixed-duration SKUs do not accept trim parameters")
    locked = entry["locked_spec"]
    mode = _one(
        found,
        "mode",
        lambda value: {"first_frame": "image_to_video", "interpolation": "first_last_frame"}.get(
            _str(value), _str(value)
        ),
    )
    if is_edit and mode in {None, "auto"}:
        mode = "edit"
    if mode and mode != "auto" and mode not in entry["allowed_modes"]:
        raise ValueError(f"Unsupported mode for SKU: {mode}")
    for field, normalizer in (
        ("quality", _str),
        ("durationSeconds", _int),
        ("resolution", _str),
        ("priority", _str),
    ):
        value = _one(found, field, normalizer)
        if value is None:
            continue
        expected = locked.get(field)
        if is_edit and field == "durationSeconds":
            raise ValueError(
                "Editing SKUs do not accept durationSeconds; use trimStartMs/trimEndMs"
            )
        if field == "quality" and entry["public_model"] == "omni_1.1_flash" and value == "flash":
            continue
        if value != expected:
            raise ValueError(f"{field} conflicts with the SKU")
    aspects = [_aspect(value) for value in found.get("aspectRatio", [])]
    for section in _sections(body, _VIDEO_CONFIG_NAMES):
        if "seconds" in section:
            if is_edit:
                raise ValueError("Editing SKUs do not accept seconds; use trimStartMs/trimEndMs")
            seconds = section["seconds"]
            if isinstance(seconds, bool) or str(seconds) != str(locked["durationSeconds"]):
                raise ValueError("seconds conflicts with the SKU")
        if "size" in section:
            size = section["size"]
            if not isinstance(size, str) or size not in _VIDEO_SIZES:
                raise ValueError("Unsupported video size")
            aspect, resolution = _VIDEO_SIZES[size]
            if resolution != locked["resolution"]:
                raise ValueError("size conflicts with the SKU resolution")
            aspects.append(aspect)
    if aspects and any(value != aspects[0] for value in aspects[1:]):
        raise ValueError("Conflicting values for aspectRatio/size")
    aspect = aspects[0] if aspects else None
    result = {"mode": mode} if mode else {}
    if aspect:
        result["aspectRatio"] = aspect
    if is_edit:
        start = _one(found, "trimStartMs", _int)
        end = _one(found, "trimEndMs", _int)
        if start is not None or end is not None:
            if (
                not 0 <= (start or 0) < (end if end is not None else 10000)
                or (end if end is not None else 10000) - (start or 0) > 10000
            ):
                raise ValueError("Trim must satisfy 0 <= start < end and end-start <= 10000 ms")
            if start is not None:
                result["trimStartMs"] = start
            if end is not None:
                result["trimEndMs"] = end
    return result


def _strip_specs(source: dict[str, Any]) -> None:
    fields = set(_IMAGE_ALIASES) | set(_VIDEO_ALIASES) | {"seconds"}
    for field in fields:
        source.pop(field, None)
    for generation_name in _GENERATION_NAMES:
        generation = source.get(generation_name)
        if not isinstance(generation, dict):
            continue
        for field in fields:
            generation.pop(field, None)
        for config_name in _IMAGE_CONFIG_NAMES + _VIDEO_CONFIG_NAMES:
            generation.pop(config_name, None)
    for config_name in _IMAGE_CONFIG_NAMES + _VIDEO_CONFIG_NAMES:
        source.pop(config_name, None)


def _validate_configs(body: dict[str, Any], media_type: str) -> None:
    names = _IMAGE_CONFIG_NAMES if media_type == "image" else _VIDEO_CONFIG_NAMES
    opposite = _VIDEO_CONFIG_NAMES if media_type == "image" else _IMAGE_CONFIG_NAMES
    aliases = _IMAGE_ALIASES if media_type == "image" else _VIDEO_ALIASES
    for source in _sources(body):
        parents = [source]
        for name in _GENERATION_NAMES:
            if name in source:
                if not isinstance(source[name], dict):
                    raise ValueError(f"{name} must be an object")
                parents.append(source[name])
        for parent in parents:
            if any(name in parent for name in opposite):
                raise ValueError(f"{media_type} SKU does not accept {opposite[0]}")
            incompatible = (
                {
                    "mode",
                    "durationSeconds",
                    "duration_seconds",
                    "seconds",
                    "priority",
                    "trimStartMs",
                    "trim_start_ms",
                    "trimEndMs",
                    "trim_end_ms",
                }
                if media_type == "image"
                else {"imageSize", "image_size", "imageQuality", "image_quality"}
            )
            if incompatible & parent.keys():
                raise ValueError(
                    f"Unsupported {media_type} SKU parameter: {sorted(incompatible & parent.keys())[0]}"
                )
            for name in names:
                if name not in parent:
                    continue
                config = parent[name]
                if not isinstance(config, dict):
                    raise ValueError(f"{name} must be an object")
                unknown = set(config) - set(aliases)
                if unknown:
                    raise ValueError(f"Unknown {name} parameter: {sorted(unknown)[0]}")


def prepare_sku_request(body: dict[str, Any]) -> dict[str, Any]:
    """Lock a SKU's billable dimensions and return a new request dictionary."""
    if not isinstance(body, dict):
        raise ValueError("Request body must be an object")
    sku = body.get("model")
    entry = get_sku_entry(sku)
    if entry is None:
        raise ValueError(f"Unknown media SKU: {sku}")
    _validate_configs(body, entry["type"])
    result = deepcopy(body)
    if entry["type"] == "image":
        overrides = _image_overrides(body, entry["locked_spec"])
    else:
        overrides = _video_overrides(body, entry)
    for source in [
        result,
        *[result[name] for name in _SOURCE_NAMES if isinstance(result.get(name), dict)],
    ]:
        _strip_specs(source)
    result["model"] = entry["public_model"]
    if entry["type"] == "image":
        image_config = dict(entry["locked_spec"])
        image_config.update(overrides)
        result.setdefault("generationConfig", {})["imageConfig"] = image_config
    else:
        video_config = dict(entry["locked_spec"])
        video_config.update(overrides)
        if entry["public_model"] == "omni_1.1_flash":
            video_config.pop("priority", None)
        result.setdefault("generationConfig", {})["videoConfig"] = video_config
    return result
