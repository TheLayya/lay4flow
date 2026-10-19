"""Normalize current Flow media and legacy operations without I/O or request state.

Keep new upstream response shapes here while FlowClient retains its compatibility
methods for existing upload, submission, and polling callers.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


def extract_media_name(media: Any) -> Optional[str]:
    """从新版 media 对象或数组中提取 media id。"""
    if isinstance(media, list):
        for item in media:
            media_name = extract_media_name(item)
            if media_name:
                return media_name
        return None
    if isinstance(media, dict):
        name = media.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def find_nested_string(value: Any, keys: tuple[str, ...]) -> Optional[str]:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for candidate in value.values():
            found = find_nested_string(candidate, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_nested_string(item, keys)
            if found:
                return found
    return None


def extract_image_url_from_media(media: Any) -> Optional[str]:
    """Validate and extract the first generated image URL from a Flow response."""
    if not isinstance(media, list) or not media or not isinstance(media[0], dict):
        return None
    image = media[0].get("image")
    generated_image = image.get("generatedImage") if isinstance(image, dict) else None
    image_url = generated_image.get("fifeUrl") if isinstance(generated_image, dict) else None
    if not isinstance(image_url, str) or not image_url.strip():
        return None
    image_url = image_url.strip()
    if any(character.isspace() or ord(character) < 32 for character in image_url):
        return None
    try:
        parsed = urlsplit(image_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        return None
    return image_url


def extract_video_status_from_media(media: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
    status_block = (
        media.get("mediaMetadata", {}).get("mediaStatus", {}) or media.get("mediaStatus", {}) or {}
    )
    status = (
        status_block.get("mediaGenerationStatus")
        or status_block.get("status")
        or media.get("status")
    )
    return status, status_block if isinstance(status_block, dict) else {}


def extract_video_url_from_media(media: Dict[str, Any]) -> Optional[str]:
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    candidates = [
        find_nested_string(video, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")),
        find_nested_string(media, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")),
        find_nested_string(video, ("uri", "url")),
    ]
    for candidate in candidates:
        if candidate and (
            candidate.startswith("http://")
            or candidate.startswith("https://")
            or candidate.startswith("/")
        ):
            return candidate
    return None


def extract_video_metadata_from_media(
    media: Dict[str, Any],
) -> Dict[str, Any]:
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    generated_video = (
        video.get("generatedVideo") if isinstance(video.get("generatedVideo"), dict) else {}
    )
    request_data = (
        media.get("mediaMetadata", {}).get("requestData", {}).get("videoGenerationRequestData", {})
        if isinstance(media.get("mediaMetadata"), dict)
        else {}
    )
    control_input = (
        request_data.get("videoModelControlInput", {}) if isinstance(request_data, dict) else {}
    )
    dimensions = video.get("dimensions") if isinstance(video.get("dimensions"), dict) else {}

    media_name = extract_media_name(media)
    aspect_ratio = (
        find_nested_string(generated_video, ("aspectRatio",))
        or find_nested_string(video, ("aspectRatio", "videoAspectRatio"))
        or find_nested_string(control_input, ("videoAspectRatio", "aspectRatio"))
        or find_nested_string(media.get("mediaMetadata", {}), ("videoAspectRatio", "aspectRatio"))
    )
    model_name = find_nested_string(generated_video, ("model",)) or find_nested_string(
        control_input, ("videoModelName", "videoModelKey")
    )
    duration = find_nested_string(dimensions, ("length", "duration")) or find_nested_string(
        generated_video, ("length", "duration")
    )

    video_metadata: Dict[str, Any] = {}
    if media_name:
        video_metadata["mediaName"] = media_name
        video_metadata["mediaGenerationId"] = media_name
    if aspect_ratio:
        video_metadata["aspectRatio"] = aspect_ratio
    if model_name:
        video_metadata["model"] = model_name
    if duration:
        video_metadata["duration"] = duration

    embedded_url = extract_video_url_from_media(media)
    if embedded_url:
        video_metadata["embeddedUrl"] = embedded_url
        video_metadata["fifeUrl"] = embedded_url

    return video_metadata


def media_to_video_operation(
    media: Dict[str, Any],
    fallback_project_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(media, dict):
        return None

    media_name = extract_media_name(media)
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    video_operation = video.get("operation") if isinstance(video.get("operation"), dict) else {}
    operation_name = (
        video_operation.get("name") or find_nested_string(video_operation, ("name",)) or media_name
    )
    if not operation_name:
        return None

    project_id = media.get("projectId") or fallback_project_id
    status, status_block = extract_video_status_from_media(media)
    operation: Dict[str, Any] = {
        "operation": {
            "name": operation_name,
        },
        "status": status or "MEDIA_GENERATION_STATUS_PENDING",
    }
    if media_name:
        operation["mediaName"] = media_name
    if project_id:
        operation["projectId"] = project_id

    scene_id = media.get("sceneId") or media.get("workflowStepId") or video_operation.get("sceneId")
    if scene_id:
        operation["sceneId"] = scene_id

    video_metadata = extract_video_metadata_from_media(media)
    if video_metadata:
        operation["operation"]["metadata"] = {"video": video_metadata}

    error = status_block.get("error") if isinstance(status_block, dict) else None
    if isinstance(error, dict):
        operation["operation"]["error"] = error
    failure_reasons = status_block.get("failureReasons") if isinstance(status_block, dict) else None
    if failure_reasons:
        operation["operation"]["failureReasons"] = failure_reasons

    return operation


def merge_video_operations_with_media(
    operations: List[Dict[str, Any]],
    media_operations: List[Dict[str, Any]],
    fallback_project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    media_by_name: Dict[str, Dict[str, Any]] = {}
    for item in media_operations:
        media_name = item.get("mediaName") or (item.get("operation") or {}).get("name")
        if media_name:
            media_by_name[media_name] = item

    merged: List[Dict[str, Any]] = []
    for raw_operation in operations:
        operation = dict(raw_operation) if isinstance(raw_operation, dict) else {}
        operation_body = dict(operation.get("operation") or {})
        operation["operation"] = operation_body
        name = operation_body.get("name") or operation.get("mediaName")
        media_operation = media_by_name.get(name) if name else None
        if media_operation:
            operation.setdefault("mediaName", media_operation.get("mediaName"))
            operation.setdefault("projectId", media_operation.get("projectId"))
            operation.setdefault("status", media_operation.get("status"))
            operation.setdefault("sceneId", media_operation.get("sceneId"))
            if "metadata" not in operation_body and (media_operation.get("operation") or {}).get(
                "metadata"
            ):
                operation_body["metadata"] = (media_operation.get("operation") or {}).get(
                    "metadata"
                )
            if "error" not in operation_body and (media_operation.get("operation") or {}).get(
                "error"
            ):
                operation_body["error"] = (media_operation.get("operation") or {}).get("error")
        elif fallback_project_id:
            operation.setdefault("projectId", fallback_project_id)
        merged.append(operation)

    return merged


def normalize_video_generation_response(
    result: Dict[str, Any],
    fallback_project_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result

    normalized = dict(result)
    media_items = normalized.get("media")
    media_operations: List[Dict[str, Any]] = []
    if isinstance(media_items, list):
        for media in media_items:
            operation = media_to_video_operation(media, fallback_project_id=fallback_project_id)
            if operation:
                media_operations.append(operation)

    operations = normalized.get("operations")
    if isinstance(operations, list) and operations:
        normalized["operations"] = merge_video_operations_with_media(
            operations,
            media_operations,
            fallback_project_id=fallback_project_id,
        )
    elif media_operations:
        normalized["operations"] = media_operations

    return normalized


def operations_to_media_refs(
    operations: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    media_refs: List[Dict[str, str]] = []
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        operation_body = operation.get("operation") or {}
        media_name = (
            operation.get("mediaName") or operation.get("name") or operation_body.get("name")
        )
        project_id = (
            operation.get("projectId")
            or operation.get("project_id")
            or operation_body.get("projectId")
        )
        if (
            isinstance(media_name, str)
            and media_name.strip()
            and isinstance(project_id, str)
            and project_id.strip()
        ):
            media_refs.append(
                {
                    "name": media_name.strip(),
                    "projectId": project_id.strip(),
                }
            )
    return media_refs
