"""Public media models and strict parameter resolution into existing internal keys."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

from .model_resolver import (
    ASPECT_RATIO_MAP,
    IMAGE_SIZE_MAP,
    OPENAI_IMAGE_SIZE_RE,
    OPENAI_QUALITY_MAP,
    _extract_generation_params,
    _infer_aspect_ratio_from_images,
)


_ASPECTS = {
    "16:9": "landscape",
    "9:16": "portrait",
    "1:1": "square",
    "4:3": "four-three",
    "3:4": "three-four",
}
_VIDEO_MODES = {
    "text_to_video": {"minImages": 0, "maxImages": 0, "videoInput": False, "mediaId": False},
    "image_to_video": {"minImages": 1, "maxImages": 1, "videoInput": False, "mediaId": False},
    "first_last_frame": {"minImages": 2, "maxImages": 2, "videoInput": False, "mediaId": False},
    "reference_to_video": {"minImages": 1, "maxImages": 3, "videoInput": False, "mediaId": False},
}


def _combination(mode, quality, durations, resolutions):
    return {"mode": mode, "quality": quality, "durations": durations, "resolutions": resolutions}


_VEO_COMBINATIONS = (
    [
        _combination(mode, "quality", [8], ["720p", "1080p", "4k"])
        for mode in ("text_to_video", "image_to_video", "first_last_frame")
    ]
    + [
        _combination(mode, quality, [8], ["720p"])
        for mode in ("text_to_video", "image_to_video", "first_last_frame")
        for quality in ("fast", "lite")
    ]
    + [
        _combination(mode, "lite", [8], ["1080p"])
        for mode in ("text_to_video", "image_to_video", "first_last_frame")
    ]
    + [
        _combination("text_to_video", "fast", [8], ["1080p", "4k"]),
        _combination("reference_to_video", "fast", [8], ["720p"]),
        _combination("reference_to_video", "lite", [8], ["720p"]),
        _combination("reference_to_video", "lite", [8], ["1080p"]),
    ]
    + [_combination("extend", quality, [8], ["720p"]) for quality in ("quality", "fast", "lite")]
    + [_combination("extend", "lite", [8], ["1080p"])]
    + [
        {**_combination(mode, "fast", [8], ["1080p", "4k"]), "minTier": "ultra"}
        for mode in ("image_to_video", "first_last_frame", "reference_to_video")
    ]
    + [
        {**_combination(mode, "fast", [8], ["720p"]), "priority": "relaxed", "minTier": "ultra"}
        for mode in _VIDEO_MODES
    ]
)

PUBLIC_MODELS = {}
for _id, _description, _sizes in (
    ("gemini-3.1-flash-image", "Nano Banana 2.1（兼容调用名）", ["1k", "2k", "4k"]),
    ("gemini-3.1-flash-image-lite", "Nano Banana 2 Lite", ["1k"]),
    ("gemini-3.0-pro-image", "Nano Banana Pro", ["1k", "2k", "4k"]),
    ("imagen-4.0-generate-preview", "Imagen 4.0", ["1k"]),
):
    PUBLIC_MODELS[_id] = {
        "type": "image",
        "description": _description,
        "usage": "根据提示词生成图片。",
        "recommended_endpoint": "/v1/chat/completions",
        "capabilities": {
            "aspectRatios": list(_ASPECTS) if not _id.startswith("imagen") else ["16:9", "9:16"],
            "imageSizes": _sizes,
            "minImages": 0,
            "maxImages": 10,
        },
        "defaults": {"aspectRatio": "16:9", "imageSize": "1k"},
    }
PUBLIC_MODELS["veo_3_1"] = {
    "type": "video",
    "description": "Veo 3.1",
    "compatibilityAliases": {
        "veo_3_1_interpolation_lite": "mode=first_last_frame, quality=lite",
        "veo_3_1_remix": "legacy name; historical Extend continuation",
        "veo_3_1_extend": "mode=extend",
    },
    "usage": "统一视频入口：文生、首帧、首尾帧、参考素材和已有媒体续写。",
    "recommended_endpoint": "/v1/videos",
    "capabilities": {
        "aspectRatios": ["16:9", "9:16"],
        "qualities": ["quality", "fast", "lite"],
        "priorities": ["standard", "relaxed"],
        "modes": {
            **_VIDEO_MODES,
            "extend": {"minImages": 0, "maxImages": 0, "videoInput": False, "mediaId": True},
        },
        "combinations": _VEO_COMBINATIONS,
    },
    "defaults": {
        "mode": "auto",
        "quality": "fast",
        "priority": "standard",
        "durationSeconds": 8,
        "resolution": "720p",
        "aspectRatio": "16:9",
    },
}
PUBLIC_MODELS["veo_3_1_interpolation_lite"] = {
    "legacy": True,
    "type": "video",
    "description": "Veo 3.1 Interpolation Lite",
    "usage": "兼容旧调用名：使用两张图片生成首尾帧过渡视频。",
    "recommended_endpoint": "/v1/videos",
    "capabilities": {
        "aspectRatios": ["16:9", "9:16"],
        "qualities": ["lite"],
        "modes": {"first_last_frame": _VIDEO_MODES["first_last_frame"]},
        "combinations": [_combination("first_last_frame", "lite", [4, 6, 8], ["720p", "1080p"])],
    },
    "defaults": {
        "mode": "first_last_frame",
        "quality": "lite",
        "durationSeconds": 8,
        "resolution": "720p",
        "aspectRatio": "16:9",
    },
}
PUBLIC_MODELS["veo_3_1_remix"] = {
    "legacy": True,
    "type": "video",
    "description": "Veo 3.1 legacy Remix compatibility name",
    "usage": "旧兼容名称；历史请求按 Flow Extend 续写链解析，不代表视频重写能力。",
    "recommended_endpoint": "/v1/videos",
    "capabilities": {
        "aspectRatios": ["16:9", "9:16"],
        "qualities": ["quality", "fast", "lite"],
        "modes": {"extend": {"minImages": 0, "maxImages": 0, "videoInput": False, "mediaId": True}},
        "combinations": [
            _combination("extend", quality, [8], ["720p"])
            for quality in ("quality", "fast", "lite")
        ]
        + [_combination("extend", "lite", [8], ["1080p"])],
    },
    "defaults": {
        "mode": "extend",
        "quality": "quality",
        "durationSeconds": 8,
        "resolution": "720p",
        "aspectRatio": "16:9",
    },
}
# Keep existing API names accepted without duplicating the Veo product in catalogs.
PUBLIC_MODELS["veo_3_1_extend"] = {
    **PUBLIC_MODELS["veo_3_1_remix"],
    "description": "Veo 3.1 Extend",
    "usage": "使用 extend://<MEDIA_ID> 续写已有 Flow 视频。",
}
PUBLIC_MODELS["omni_1.1_flash"] = {
    "type": "video",
    "description": "Gemini Omni 1.1 Flash",
    "usage": "低成本视频生成与视频编辑入口，支持文本、图片和视频素材。",
    "recommended_endpoint": "/v1/videos",
    "capabilities": {
        "aspectRatios": ["16:9", "9:16"],
        "qualities": [],
        "modes": {
            **_VIDEO_MODES,
            "reference_to_video": {
                "minImages": 1,
                "maxImages": 7,
                "videoInput": False,
                "mediaId": False,
            },
            "edit": {"minImages": 0, "maxImages": 5, "videoInput": True, "mediaId": True},
        },
        "combinations": [
            _combination(mode, None, [4, 6, 8, 10], ["720p", "360p"]) for mode in _VIDEO_MODES
        ]
        + [_combination("edit", None, [], ["720p", "360p"])]
        + [
            {
                **_combination(mode, None, [] if mode == "edit" else [4, 6, 8, 10], [resolution]),
                "minTier": tier,
            }
            for mode in (*_VIDEO_MODES, "edit")
            for resolution, tier in (("1080p", "pro"), ("4k", "ultra"))
        ],
    },
    "defaults": {"mode": "auto", "durationSeconds": 8, "resolution": "720p", "aspectRatio": "16:9"},
}

_PUBLIC_USAGE = {
    "gemini-3.1-flash-image": "\u6839\u636e\u63d0\u793a\u8bcd\u751f\u6210\u56fe\u7247\u3002",
    "gemini-3.1-flash-image-lite": "\u6839\u636e\u63d0\u793a\u8bcd\u751f\u6210\u56fe\u7247\u3002",
    "gemini-3.0-pro-image": "\u6839\u636e\u63d0\u793a\u8bcd\u751f\u6210\u56fe\u7247\u3002",
    "imagen-4.0-generate-preview": "\u6839\u636e\u63d0\u793a\u8bcd\u751f\u6210\u56fe\u7247\u3002",
    "veo_3_1": "\u7edf\u4e00\u89c6\u9891\u5165\u53e3\uff1a\u6587\u751f\u3001\u9996\u5e27\u3001\u9996\u5c3e\u5e27\u3001\u53c2\u8003\u7d20\u6750\u548c\u5df2\u6709\u5a92\u4f53\u7eed\u5199\u3002",
    "veo_3_1_interpolation_lite": "\u517c\u5bb9\u65e7\u8c03\u7528\u540d\uff1a\u4f7f\u7528\u4e24\u5f20\u56fe\u7247\u751f\u6210\u9996\u5c3e\u5e27\u8fc7\u6e21\u89c6\u9891\u3002",
    "veo_3_1_remix": "\u65e7\u517c\u5bb9\u540d\u79f0\uff1b\u5386\u53f2\u8bf7\u6c42\u6309 Flow Extend \u7eed\u5199\u94fe\u89e3\u6790\uff0c\u4e0d\u4ee3\u8868\u89c6\u9891\u91cd\u5199\u80fd\u529b\u3002",
    "veo_3_1_extend": "\u4f7f\u7528 extend://<MEDIA_ID> \u7eed\u5199\u5df2\u6709 Flow \u89c6\u9891\u3002",
    "omni_1.1_flash": "\u4f4e\u6210\u672c\u89c6\u9891\u751f\u6210\u4e0e\u89c6\u9891\u7f16\u8f91\u5165\u53e3\uff0c\u652f\u6301\u6587\u672c\u3001\u56fe\u7247\u548c\u89c6\u9891\u7d20\u6750\u3002",
}
for _model_id, _usage in _PUBLIC_USAGE.items():
    PUBLIC_MODELS[_model_id]["usage"] = _usage


@dataclass(frozen=True)
class ResolvedMedia:
    internal_model: str
    public_model: str
    options: dict
    trim_start_ms: Optional[int] = None
    trim_end_ms: Optional[int] = None


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "__dict__"):
        result = {k: v for k, v in vars(value).items() if not k.startswith("_") and v is not None}
        result.update(getattr(value, "__pydantic_extra__", None) or {})
        return result
    raise ValueError("Generation configuration must be an object")


def _sources(request: Any) -> list[dict]:
    root = _mapping(request) if request is not None else {}
    sources = [root]
    for name in ("extra_body", "extraBody"):
        if name in root:
            sources.append(_mapping(root[name]))
    return sources


def _configs(sources: list[dict], kind: str) -> list[dict]:
    result = []
    names = (kind + "Config", kind + "_config")
    for source in sources:
        parents = [source]
        for name in ("generationConfig", "generation_config"):
            if name in source:
                parents.append(_mapping(source[name]))
        for parent in parents:
            for name in names:
                if name in parent:
                    result.append(_mapping(parent[name]))
    return result


def _merge(result: dict, field: str, value: Any):
    if field in result and result[field] != value:
        raise ValueError(f"Conflicting values for {field}")
    result[field] = value


def _aspect(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("aspectRatio must be a supported ratio")
    value = (
        value.strip()
        .replace("：", ":")
        .replace("/", ":")
        .replace("x", ":")
        .replace("X", ":")
        .replace(" ", "")
    )
    if value in _ASPECTS:
        return value
    aspect = ASPECT_RATIO_MAP.get(value) or ASPECT_RATIO_MAP.get(value.lower())
    for ratio, name in _ASPECTS.items():
        if aspect == name:
            return ratio
    raise ValueError(f"Unsupported aspectRatio: {value}")


def _integer(value: Any, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


_VIDEO_FIELDS = {
    "priority": "priority",
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
}


def _video_options(sources: list[dict]) -> dict:
    result = {}
    for source in sources:
        parents = [source]
        for name in ("generationConfig", "generation_config"):
            if name in source:
                parents.append(_mapping(source[name]))
        misplaced = set(_VIDEO_FIELDS) | {
            "imageSize",
            "image_size",
            "size",
            "imageQuality",
            "image_quality",
        }
        for parent in parents:
            if misplaced & parent.keys():
                raise ValueError("Video generation parameters must be placed in videoConfig")
    for cfg in _configs(sources, "video"):
        for name, value in cfg.items():
            if name not in _VIDEO_FIELDS:
                raise ValueError(f"Unknown videoConfig parameter: {name}")
            field = _VIDEO_FIELDS[name]
            if field == "aspectRatio":
                value = _aspect(value)
            elif field in ("durationSeconds", "trimStartMs", "trimEndMs"):
                value = _integer(value, field)
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a nonempty string")
            else:
                value = value.strip().lower()
                if field == "mode":
                    value = {
                        "first_frame": "image_to_video",
                        "interpolation": "first_last_frame",
                    }.get(value, value)
            _merge(result, field, value)
    return result


def _legacy_image_option(result: dict, name: str, value: Any):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    value = value.strip()
    if name == "size":
        match = OPENAI_IMAGE_SIZE_RE.fullmatch(value)
        if not match or int(match["w"]) <= 0 or int(match["h"]) <= 0:
            raise ValueError("size must contain positive pixel dimensions, for example 1024x1792")
    elif value.lower() not in OPENAI_QUALITY_MAP and value.lower() not in IMAGE_SIZE_MAP:
        raise ValueError(f"Unsupported image quality: {value}")
    aspect, size = _extract_generation_params(
        SimpleNamespace(generationConfig={"imageConfig": {name: value}})
    )
    if aspect:
        _merge(result, "aspectRatio", _aspect(aspect))
    if name != "size":
        _merge(result, "imageSize", size or "1k")


def _image_options(sources: list[dict]) -> dict:
    result = {}
    aliases = {
        "aspectRatio": "aspectRatio",
        "aspect_ratio": "aspectRatio",
        "aspect": "aspectRatio",
        "imageSize": "imageSize",
        "image_size": "imageSize",
        "resolution": "imageSize",
    }
    legacy_fields = {"size", "quality", "imageQuality", "image_quality"}
    for cfg in _configs(sources, "image"):
        for name, value in cfg.items():
            if name in aliases:
                field = aliases[name]
                if field == "aspectRatio":
                    value = _aspect(value)
                elif not isinstance(value, str):
                    raise ValueError("imageSize must be a string")
                else:
                    value = value.strip().lower()
                _merge(result, field, value)
            elif name not in legacy_fields:
                raise ValueError(f"Unknown imageConfig parameter: {name}")
        # Reuse the OpenAI size/quality adapter instead of maintaining a second mapping.
        for name in legacy_fields & cfg.keys():
            _legacy_image_option(result, name, cfg[name])
    for source in sources:
        # Older clients put size/quality/aspect directly in generationConfig or the body.
        parents = [source]
        for name in ("generationConfig", "generation_config"):
            if name in source:
                parents.append(_mapping(source[name]))
        for parent in parents:
            for name in (set(aliases) | legacy_fields) & parent.keys():
                value = parent[name]
                if name in aliases:
                    field = aliases[name]
                    if field == "aspectRatio":
                        value = _aspect(value)
                    elif isinstance(value, str):
                        value = value.strip().lower()
                    else:
                        raise ValueError("imageSize must be a string")
                    _merge(result, field, value)
                else:
                    _legacy_image_option(result, name, value)
    return result


def _video_internal(model: str, options: dict) -> str:
    model = {
        "veo_3_1_interpolation_lite": "veo_3_1",
        "veo_3_1_remix": "veo_3_1",
        "veo_3_1_extend": "veo_3_1",
    }.get(model, model)
    mode = options["mode"]
    orientation = _ASPECTS[options["aspectRatio"]]
    portrait = "_portrait" if orientation == "portrait" else ""
    resolution = "" if options["resolution"] == "720p" else "_" + options["resolution"]
    if model == "omni_1.1_flash":
        if mode == "edit":
            return f"omni_edit{portrait}{resolution}"
        base = {
            "text_to_video": "omni",
            "reference_to_video": "omni_r2v",
            "image_to_video": "omni_i2v",
            "first_last_frame": "omni_i2v",
        }[mode]
        frames = "_first_last" if mode == "first_last_frame" else ""
        return f"{base}{portrait}_{options['durationSeconds']}s{frames}{resolution}"
    quality, duration = options["quality"], options["durationSeconds"]
    if options.get("priority") == "relaxed":
        kind = {
            "text_to_video": "t2v",
            "image_to_video": "i2v_s",
            "first_last_frame": "i2v_s",
            "reference_to_video": "r2v",
        }[mode]
        return f"veo_3_1_{kind}_fast{portrait}_ultra_relaxed"
    if mode == "extend":
        if quality == "fast":
            return f"veo_3_1_extend_fast_{orientation}"
        if quality == "lite":
            return f"veo_3_1_extension_lite_{orientation}{resolution}"
        return "veo_3_1_extend" + portrait
    if mode == "reference_to_video":
        if quality == "lite" and resolution:
            return f"veo_3_1_r2v_lite_{orientation}{resolution}"
        if resolution:
            return f"veo_3_1_r2v_fast{portrait}_ultra{resolution}"
        return (
            f"veo_3_1_r2v_lite_{orientation}"
            if quality == "lite"
            else f"veo_3_1_r2v_fast{portrait}"
        )
    if quality == "lite":
        kind = {
            "text_to_video": "t2v",
            "image_to_video": "i2v",
            "first_last_frame": "interpolation",
        }[mode]
        return f"veo_3_1_{kind}_lite_{orientation}_{duration}s{resolution}"
    kind = "t2v" if mode == "text_to_video" else "i2v_s"
    if quality == "quality":
        return f"veo_3_1_{kind}_{orientation}_{duration}s{resolution}"
    if resolution:
        if kind == "i2v_s":
            return f"veo_3_1_i2v_s_fast{portrait}_ultra_fl{resolution}"
        return f"veo_3_1_t2v_fast{portrait}{resolution}"
    frames = "_fl" if kind == "i2v_s" else ""
    return f"veo_3_1_{kind}_fast_{orientation}_{duration}s{frames}"


def resolve_public_model(
    model, request, model_config, images=None, has_video=False, video_media_id=None
):
    """Resolve public feature IDs; legacy IDs remain the responsibility of the old resolver."""
    entry = PUBLIC_MODELS.get(model)
    if entry is None:
        return None
    sources = _sources(request)
    count = len(images or [])
    caps = entry["capabilities"]
    if entry["type"] == "image":
        if has_video or video_media_id or _configs(sources, "video"):
            raise ValueError("Image models do not accept video input or videoConfig")
        supplied = _image_options(sources)
        options = {**entry["defaults"], **supplied}
        if "aspectRatio" not in supplied:
            inferred = _infer_aspect_ratio_from_images(images)
            if inferred and _aspect(inferred) in caps["aspectRatios"]:
                options["aspectRatio"] = _aspect(inferred)
        if not caps["minImages"] <= count <= caps["maxImages"]:
            raise ValueError(f"{model} accepts at most {caps['maxImages']} images")
        if (
            options["aspectRatio"] not in caps["aspectRatios"]
            or options["imageSize"] not in caps["imageSizes"]
        ):
            raise ValueError(f"Unsupported aspectRatio/imageSize for {model}")
        size = "" if options["imageSize"] == "1k" else "-" + options["imageSize"]
        internal = f"{model}-{_ASPECTS[options['aspectRatio']]}{size}"
        trim_start = trim_end = None
    else:
        if _configs(sources, "image"):
            raise ValueError("Video models require videoConfig, not imageConfig")
        supplied = _video_options(sources)
        options = {**entry["defaults"], **supplied}
        mode = options["mode"]
        if mode == "auto":
            if has_video or video_media_id:
                if model != "omni_1.1_flash":
                    raise ValueError("Veo video input requires explicit mode=extend and a media ID")
                mode = "edit"
            else:
                mode = "reference_to_video" if count else "text_to_video"
        options["mode"] = mode
        if mode not in caps["modes"]:
            raise ValueError(f"Unsupported mode for {model}: {mode}")
        mode_caps = caps["modes"][mode]
        if not mode_caps["minImages"] <= count <= mode_caps["maxImages"]:
            raise ValueError(
                f"{mode} requires {mode_caps['minImages']}-{mode_caps['maxImages']} images"
            )
        if has_video and video_media_id:
            raise ValueError("Only one video input is supported")
        if mode == "edit":
            if not (has_video or video_media_id):
                raise ValueError("edit requires one source video")
            if "durationSeconds" in supplied:
                raise ValueError(
                    "edit does not accept durationSeconds; select the source segment with trimStartMs/trimEndMs"
                )
            options.pop("durationSeconds", None)
        elif mode == "extend":
            if has_video or not video_media_id:
                raise ValueError("extend requires a media ID, not uploaded video bytes")
            if "quality" not in supplied:
                options["quality"] = "quality"
        elif has_video or video_media_id:
            raise ValueError(f"{mode} does not accept video input")
        trim_start = trim_end = None
        explicit_trim = "trimStartMs" in supplied or "trimEndMs" in supplied
        if explicit_trim and mode != "edit":
            raise ValueError("Trim parameters are only supported for video editing")
        if explicit_trim and video_media_id:
            raise ValueError("Trim parameters require uploaded video bytes, not a media ID")
        if mode == "edit" and has_video:
            trim_start, trim_end = supplied.get("trimStartMs", 0), supplied.get("trimEndMs", 10000)
            if not 0 <= trim_start < trim_end or trim_end - trim_start > 10000:
                raise ValueError("Trim must satisfy 0 <= start < end and end-start <= 10000 ms")
            options.update(trimStartMs=trim_start, trimEndMs=trim_end)
        if options["aspectRatio"] not in caps["aspectRatios"]:
            raise ValueError(f"Unsupported video aspectRatio: {options['aspectRatio']}")
        quality = options.get("quality")
        if quality is not None and quality not in caps["qualities"]:
            raise ValueError(f"Unsupported quality for {model}: {quality}")
        if "priority" in options and options["priority"] not in caps.get("priorities", []):
            raise ValueError(f"Unsupported priority for {model}: {options['priority']}")
        if not any(
            c["mode"] == mode
            and c["quality"] == quality
            and c.get("priority", "standard") == options.get("priority", "standard")
            and (mode == "edit" or options["durationSeconds"] in c["durations"])
            and options["resolution"] in c["resolutions"]
            for c in caps["combinations"]
        ):
            raise ValueError(
                f"Unsupported mode/quality/priority/durationSeconds/resolution combination for {model}"
            )
        internal = _video_internal(model, options)
    if internal not in model_config:
        raise ValueError(f"This model configuration is not available: {internal}")
    return ResolvedMedia(internal, model, options, trim_start, trim_end)
