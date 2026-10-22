"""Pure request/response adapters for Flow's current Angular video RPCs."""

from __future__ import annotations

import uuid
from typing import Any

from .media import extract_image_url_from_media, normalize_video_generation_response

VIDEO_TEXT_RPC = "YhhmEf"
VIDEO_POLL_RPC = "jwpduf"
VIDEO_RPCS = {
    "text": VIDEO_TEXT_RPC,
    "start": "eb1hJf",
    "start_end": "nprQif",
    "references": "MZZa6b",
    "extend": "fZytfe",
    "upsample": "p0UkFb",
    "edit": "jIps6",
}

_ASPECT = {
    "LANDSCAPE": 2,
    "LANDSCAPE_4_3": 2,
    "4:3": 2,
    "16:9": 2,
    "PORTRAIT": 1,
    "PORTRAIT_3_4": 1,
    "3:4": 1,
    "9:16": 1,
    "SQUARE": 0,
    "1:1": 0,
}
_STATUS = {
    1: "MEDIA_GENERATION_STATUS_PENDING",
    2: "MEDIA_GENERATION_STATUS_PENDING",
    3: "MEDIA_GENERATION_STATUS_SUCCESSFUL",
    4: "MEDIA_GENERATION_STATUS_FAILED",
    5: "MEDIA_GENERATION_STATUS_FAILED",
    6: "MEDIA_GENERATION_STATUS_PENDING",
    7: "MEDIA_GENERATION_STATUS_FAILED",
}


def resolve_video_usage(
    models: Any,
    model_key: str,
    aspect_ratio: str | int,
    service_tier: int,
    duration: int | None = None,
    resolution: int | None = None,
    mode: str = "text",
) -> str:
    """Return an exact current model usage key; never silently substitute a model."""
    key = _text(model_key, "model_key")
    if type(service_tier) is not int:
        raise ValueError("service_tier is invalid")
    if duration is not None and (type(duration) is not int or duration <= 0):
        raise ValueError("duration must be a positive integer")
    if resolution is not None and (type(resolution) is not int or resolution <= 0):
        raise ValueError("resolution must be a positive integer")
    aspect = _aspect(aspect_ratio)
    if not isinstance(models, list) or not models or not isinstance(models[0], list):
        raise ValueError("current Flow models are unavailable")
    families = models[0][4] if len(models[0]) > 4 else None
    if not isinstance(families, list):
        raise ValueError("current Flow video models are unavailable")
    wanted_capability = {
        "text": 1,
        "start": 5,
        "start_end": 7,
        "references": 6,
        "extend": 14,
        "edit": 20,
        "upsample": {1: 21, 2: 15, 3: 16}.get(resolution),
    }.get(mode)
    if wanted_capability is None:
        raise ValueError("video mode is invalid")
    for family in families:
        if not isinstance(family, list) or len(family) < 2 or not isinstance(family[1], list):
            continue
        for usage in family[1]:
            if not isinstance(usage, list) or not usage or usage[0] != key:
                continue
            try:
                capabilities = [arm[0] for arm in usage[7][0] if isinstance(arm, list) and arm]
                aspects = usage[12][0]
            except (IndexError, TypeError):
                continue
            if not any(isinstance(arm, list) and wanted_capability in arm for arm in capabilities):
                continue
            if not isinstance(aspects, list) or aspect not in aspects:
                continue
            duration_value = usage[16] if len(usage) > 16 and usage[16] is not None else 8
            variable_duration = len(usage) > 22 and usage[22] is True
            if duration is not None and not variable_duration and duration_value != duration:
                continue
            if resolution not in (None, 0) and mode != "upsample":
                try:
                    resolutions = usage[23][0]
                except (IndexError, TypeError):
                    resolutions = [1]
                if not resolutions:
                    resolutions = [1]
                if resolution not in resolutions:
                    continue
            pricing = usage[4] if len(usage) > 4 else None
            if not isinstance(pricing, list):
                continue
            entry = next(
                (
                    pair[1]
                    for pair in pricing
                    if isinstance(pair, list) and len(pair) > 1 and pair[0] == service_tier
                ),
                None,
            )
            if not isinstance(entry, list) or not entry or entry[0] is None:
                continue
            return key
    raise ValueError(
        f"Flow video model is unavailable for the requested mode, aspect, or tier: {key}"
    )


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ValueError(f"{field} must be a non-empty string")
        return None
    return value.strip()


def _aspect(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("aspect_ratio is invalid")
    if type(value) is int and value in {0, 1, 2}:
        return value
    key = str(value or "").strip().upper()
    key = key.removeprefix("VIDEO_ASPECT_RATIO_")
    if key in _ASPECT:
        return _ASPECT[key]
    raise ValueError("aspect_ratio is invalid")


def build_video_text_request(
    *,
    project_id: str,
    prompt: str,
    model_name: str,
    aspect_ratio: str | int,
    captcha_token: str,
    batch_id: str | None = None,
    media_id: str | None = None,
    workflow_id: str | None = None,
    resolution: int | None = None,
    audio_failure_preference: int | None = None,
) -> list[Any]:
    """Build the P9a payload passed to ``VideoFxService.BatchAsyncGenerateVideoText``."""
    project = _text(project_id, "project_id")
    text_prompt = _text(prompt, "prompt")
    model = _text(model_name, "model_name")
    captcha = _text(captcha_token, "captcha_token")
    if resolution is not None and (type(resolution) is not int or resolution <= 0):
        raise ValueError("resolution must be a positive integer")
    if audio_failure_preference is not None and type(audio_failure_preference) is not int:
        raise ValueError("audio_failure_preference must be an integer")
    batch = _text(batch_id, "batch_id", required=False) or str(uuid.uuid4())
    aspect = _aspect(aspect_ratio)
    media = _text(media_id, "media_id", required=False) or str(uuid.uuid4())
    workflow = _text(workflow_id, "workflow_id", required=False) or str(uuid.uuid4())
    # LS fields 5 and 6 are new media/workflow UUID seeds, rather than existing IDs.
    metadata = [None, None, None, None, media, workflow]
    # U8a.field3 contains VL.field1 repeated bM text parts.
    item = [[None, None, [[[text_prompt]]]], model, aspect, None, metadata]
    if resolution is not None and resolution != 1:
        item.extend([None, None, [resolution]])
    # TM.field11 is the Flow generation context: [reCAPTCHA token, application type].
    tm = [None, 22, None, None, None, project, None, None, None, None, [captcha, 1]]
    ts = [batch] if audio_failure_preference is None else [batch, audio_failure_preference]
    return [[item], tm, ts]


def build_video_poll_request(operations: list[Any]) -> list[Any]:
    """Build the Z9a payload for ``BatchCheckAsyncVideoGenerationStatus``."""
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations must be a non-empty list")
    names: list[list[str]] = []
    seen: set[str] = set()
    for item in operations:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            body = item.get("operation") if isinstance(item.get("operation"), dict) else {}
            name = item.get("mediaName") or item.get("name") or body.get("name")
            name = name.strip() if isinstance(name, str) else ""
        else:
            name = ""
        if not name or name in seen:
            if not name:
                raise ValueError("operation is missing a media name")
            continue
        seen.add(name)
        names.append([name])
    return [None, None, names]


def _reference_images(value: Any) -> list[list[Any]]:
    if value is not None and not isinstance(value, list):
        raise ValueError("reference_images must be a list")
    references = []
    for ref in value or []:
        name = ref.get("mediaId") if isinstance(ref, dict) else ref
        item = [None, _text(name, "reference_image")]
        crop = ref.get("cropCoordinates", ref.get("be")) if isinstance(ref, dict) else None
        if crop is not None:
            if (
                not isinstance(crop, dict)
                or any(
                    type(crop.get(key)) not in (int, float) or not 0 <= crop[key] <= 1
                    for key in ("top", "left", "bottom", "right")
                )
                or crop["bottom"] <= crop["top"]
                or crop["right"] <= crop["left"]
            ):
                raise ValueError("reference image crop is invalid")
            # qR -> pR.field2 media name, field6 normalized crop vQ.
            coordinates = [crop[key] for key in ("top", "left", "bottom", "right")]
            item.extend([None, None, None, coordinates])
        references.append(item)
    return references


def build_video_request(
    *,
    mode: str,
    project_id: str,
    prompt: str,
    model_name: str,
    aspect_ratio: str | int,
    captcha_token: str,
    batch_id: str | None = None,
    media_id: str | None = None,
    workflow_id: str | None = None,
    start_media_id: str | None = None,
    end_media_id: str | None = None,
    reference_images: list[Any] | None = None,
    video_media_id: str | None = None,
    trim_start_frame: int | None = None,
    trim_end_frame: int | None = None,
    resolution: int | None = None,
    audio_failure_preference: int | None = None,
) -> list[Any]:
    """Build a current video payload; select its RPC using ``VIDEO_RPCS[mode]``."""
    if mode not in VIDEO_RPCS:
        raise ValueError("video mode is invalid")
    if resolution is not None and (type(resolution) is not int or resolution <= 0):
        raise ValueError("resolution must be a positive integer")
    if mode == "text":
        return build_video_text_request(
            project_id=project_id,
            prompt=prompt,
            model_name=model_name,
            aspect_ratio=aspect_ratio,
            captcha_token=captcha_token,
            batch_id=batch_id,
            media_id=media_id,
            workflow_id=workflow_id,
            resolution=resolution,
            audio_failure_preference=audio_failure_preference,
        )
    # Reuse the current outer context and per-item metadata builder.
    base = build_video_text_request(
        project_id=project_id,
        prompt=prompt if mode != "upsample" else "unused",
        model_name=model_name,
        aspect_ratio=aspect_ratio,
        captcha_token=captcha_token,
        batch_id=batch_id,
        media_id=media_id,
        workflow_id=workflow_id,
        audio_failure_preference=audio_failure_preference,
    )
    source = base[0][0]
    prompt_proto, model, aspect, _, metadata = source
    if mode in {"start", "start_end"}:
        first = [None, _text(start_media_id, "start_media_id")]
        if mode == "start":
            item = [prompt_proto, model, aspect, None, first, metadata]
            if resolution is not None and resolution != 1:
                item.extend([None, None, None, [resolution]])
        else:
            last = [None, _text(end_media_id, "end_media_id")]
            item = [prompt_proto, model, aspect, None, first, last, metadata]
    elif mode == "references":
        refs = _reference_images(reference_images)
        if not refs:
            raise ValueError("reference_images must include at least one media name")
        item = [prompt_proto, refs, model, aspect, None, metadata]
        if resolution is not None and resolution != 1:
            item.extend([None] * 5 + [[resolution]])
    elif mode in {"extend", "edit"}:
        video = [None, _text(video_media_id, "video_media_id")]
        if trim_start_frame is not None or trim_end_frame is not None:
            if (
                type(trim_start_frame) is not int
                or type(trim_end_frame) is not int
                or trim_start_frame < 0
                or trim_end_frame <= trim_start_frame
            ):
                raise ValueError("video frame range is invalid")
            video.extend([trim_start_frame, trim_end_frame])
        item = [video, prompt_proto, model, aspect]
        if mode == "edit":
            item.append(metadata)
        else:
            item.extend([None, metadata])
        refs = _reference_images(reference_images)
        if refs:
            # Both V8a (edit) and j9a (extend) receive qR(c.Nv) at field9.
            item.extend([None] * (8 - len(item)) + [refs])
        if resolution is not None and resolution != 1:
            item.extend([None] * (12 - len(item)) + [[resolution]])
    else:
        if type(resolution) is not int or resolution not in {1, 2, 3}:
            raise ValueError("video upsample resolution is invalid")
        item = [
            [None, _text(video_media_id, "video_media_id")],
            None,
            aspect,
            None,
            metadata,
            None,
            resolution,
        ]
        item.extend([None] * (31 - len(item)) + [model])
    base[0] = [item]
    return base


def _status(media: list[Any]) -> tuple[int | None, Any]:
    try:
        status = media[5][8][0]
        error = media[5][8][1] if len(media[5][8]) > 1 else None
        return status if type(status) is int else None, error
    except (IndexError, TypeError):
        return None, None


def _media_dict(raw: Any, project_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], str) or not raw[0]:
        return None
    name = raw[0]
    numeric_status, error = _status(raw)
    if numeric_status is not None and numeric_status not in _STATUS:
        raise ValueError("video media status is invalid")
    status = _STATUS.get(numeric_status, "MEDIA_GENERATION_STATUS_PENDING")
    result: dict[str, Any] = {
        "name": name,
        "projectId": project_id,
        "mediaMetadata": {"mediaStatus": {"mediaGenerationStatus": status}},
    }
    if len(raw) > 2 and isinstance(raw[2], str) and raw[2]:
        result["workflowId"] = raw[2]
    if error is not None:
        code = error[0] if isinstance(error, list) and error and type(error[0]) is int else None
        # Preserve the status code, while leaving arbitrary upstream response text private.
        result["mediaMetadata"]["mediaStatus"]["error"] = {
            "code": code if code is not None else 13,
            "message": "Flow video generation failed",
        }
    elif numeric_status == 5:
        result["mediaMetadata"]["mediaStatus"]["error"] = {
            "code": 1,
            "message": "Flow video generation was canceled",
        }
    image = raw[6] if len(raw) > 6 and isinstance(raw[6], list) else None
    if image is not None:
        generated = image[0] if image and isinstance(image[0], list) else None
        uploaded = image[1] if len(image) > 1 and isinstance(image[1], list) else None
        if generated is not None:
            url = generated[13] if len(generated) > 13 else None
            key = "generatedImage"
        elif uploaded is not None:
            url = uploaded[3] if len(uploaded) > 3 else None
            key = "userUploadedImage"
        else:
            raise ValueError("image media body is invalid")
        if url is not None:
            candidate = {"image": {"generatedImage": {"fifeUrl": url}}}
            url = extract_image_url_from_media([candidate])
            if url is None:
                raise ValueError("image media URL is invalid")
            result["image"] = {key: {"fifeUrl": url}}
        else:
            result["image"] = {key: {}}
        dimensions = image[2] if len(image) > 2 else None
        if isinstance(dimensions, list):
            size = {
                key: dimensions[index]
                for index, key in enumerate(("width", "height"))
                if len(dimensions) > index
                and type(dimensions[index]) is int
                and dimensions[index] > 0
            }
            if size:
                result["image"]["dimensions"] = size
        return result
    try:
        video = raw[7][0]
    except (IndexError, TypeError):
        video = None
    try:
        uploaded = raw[7][4]
    except (IndexError, TypeError):
        uploaded = None
    if isinstance(uploaded, list):
        video_dict: dict[str, Any] = {"userUploadedVideo": {}}
        if len(uploaded) > 2 and isinstance(uploaded[2], str) and uploaded[2]:
            video_dict["userUploadedVideo"]["fifeUrl"] = uploaded[2]
        result["video"] = video_dict
    elif isinstance(video, list):
        video_dict = {"generatedVideo": {}}
        if len(video) > 8 and isinstance(video[8], str) and video[8]:
            video_dict["generatedVideo"]["fifeUrl"] = video[8]
        if len(video) > 12 and isinstance(video[12], str) and video[12]:
            video_dict["generatedVideo"]["model"] = video[12]
        if len(video) > 16 and type(video[16]) is int and video[16] in {0, 1, 2}:
            video_dict["generatedVideo"]["aspectRatio"] = {
                0: "VIDEO_ASPECT_RATIO_SQUARE",
                1: "VIDEO_ASPECT_RATIO_PORTRAIT",
                2: "VIDEO_ASPECT_RATIO_LANDSCAPE",
            }[video[16]]
        result["video"] = video_dict
    try:
        dimensions = raw[7][1]
    except (IndexError, TypeError):
        dimensions = None
    if isinstance(video, list) or isinstance(uploaded, list):
        # _.sQ(bz) chooses 30 for uploaded TVa(field5), 24 for generated $Va(field1).
        result.setdefault("video", {})["frameRate"] = 30 if isinstance(uploaded, list) else 24
    if isinstance(dimensions, list):
        size = {}
        for index, key in enumerate(("width", "height")):
            if len(dimensions) > index and type(dimensions[index]) is int and dimensions[index] > 0:
                size[key] = dimensions[index]
        duration = dimensions[2] if len(dimensions) > 2 else None
        if isinstance(duration, list) and duration:
            seconds = duration[0]
            nanos = duration[1] if len(duration) > 1 else 0
            if (
                (type(seconds) is int or isinstance(seconds, str) and seconds.isdecimal())
                and int(seconds) >= 0
                and type(nanos) is int
                and 0 <= nanos < 1_000_000_000
            ):
                size["length"] = f"{int(seconds) + nanos / 1_000_000_000:g}s"
        if "video" in result:
            size["frameRate"] = result["video"]["frameRate"]
        if size:
            result.setdefault("video", {})["dimensions"] = size
    return result


def parse_media_response(payload: Any, project_id: str) -> dict[str, Any]:
    """Normalize GetMedia's direct bz response, for either image or video media."""
    result = _media_dict(payload, _text(project_id, "project_id"))
    if result is None or not (
        isinstance(result.get("image"), dict)
        and any(key in result["image"] for key in ("generatedImage", "userUploadedImage"))
        or isinstance(result.get("video"), dict)
        and any(key in result["video"] for key in ("generatedVideo", "userUploadedVideo"))
    ):
        raise ValueError("Flow media RPC returned invalid media")
    return result


def parse_video_response(payload: Any, project_id: str, *, poll: bool = False) -> dict[str, Any]:
    """Convert a decoded Angular RPC response into the legacy operations shape."""
    project = _text(project_id, "project_id")
    if not isinstance(payload, list):
        raise ValueError("video RPC response must be an array")
    remaining = payload[1] if len(payload) > 1 and type(payload[1]) is int else None
    index = 2 if poll else 3
    raw_media = payload[index] if len(payload) > index else []
    if not isinstance(raw_media, list) or not raw_media:
        raise ValueError("video RPC response did not include media")
    media = []
    for raw in raw_media:
        item = _media_dict(raw, project)
        if item is None:
            raise ValueError("video RPC returned invalid media")
        media.append(item)
    result: dict[str, Any] = {"media": media, "operations": []}
    if remaining is not None:
        result["remainingCredits"] = remaining
    return normalize_video_generation_response(result, fallback_project_id=project)
