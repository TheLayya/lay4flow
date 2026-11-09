"""Flow video edit trim validation and processed-asset frame conventions."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Optional


def validate_video_trim(
    trim_start_ms: int = 0, trim_end_ms: int = 10000, *, max_duration_ms: int = 10000
) -> None:
    """Validate a nonempty segment within the selected model's edit limit."""
    if type(trim_start_ms) is not int or type(trim_end_ms) is not int:
        raise ValueError("trimStartMs and trimEndMs must be integer milliseconds")
    if trim_start_ms < 0 or trim_end_ms <= trim_start_ms:
        raise ValueError("Video trim must satisfy 0 <= trimStartMs < trimEndMs")
    if type(max_duration_ms) is not int or not 1 <= max_duration_ms <= 15000:
        raise ValueError("Invalid video edit duration limit")
    if trim_end_ms - trim_start_ms > max_duration_ms:
        raise ValueError(f"Video trim must not exceed {max_duration_ms} milliseconds")


def validate_optional_video_trim(
    trim_start_ms: Optional[int], trim_end_ms: Optional[int], *, max_duration_ms: int = 10000
) -> None:
    """Reject incomplete trim pairs before metadata or captcha requests."""
    if trim_start_ms is not None or trim_end_ms is not None:
        if trim_start_ms is None or trim_end_ms is None:
            raise ValueError("trim_start_ms and trim_end_ms must be provided together")
        validate_video_trim(trim_start_ms, trim_end_ms, max_duration_ms=max_duration_ms)


def validate_video_upload_range(trim_start_ms=0, trim_end_ms=10000, *, max_duration_ms=10000):
    """The original Extend upload can be longer than an editable segment."""
    if type(max_duration_ms) is not int or max_duration_ms not in {10000, 15000, 60000}:
        raise ValueError("Invalid video upload range limit")
    if type(trim_start_ms) is not int or type(trim_end_ms) is not int:
        raise ValueError("Video upload range requires integer milliseconds")
    if not 0 <= trim_start_ms < trim_end_ms or trim_end_ms - trim_start_ms > max_duration_ms:
        raise ValueError("Video upload range exceeds its duration limit")


def _seconds(value: Any) -> Decimal:
    try:
        duration = Decimal(str(value).removesuffix("s"))
    except InvalidOperation as exc:
        raise ValueError("Source video has invalid duration metadata") from exc
    if not duration.is_finite() or duration < 0:
        raise ValueError("Source video has invalid duration metadata")
    return duration


def video_edit_frame_range(
    media: Dict[str, Any],
    trim_start_ms: Optional[int],
    trim_end_ms: Optional[int],
    uploaded_video: bool = False,
    *,
    max_duration_ms: int = 10000,
) -> Dict[str, int]:
    """Bound edit frames by the processed asset, which can be shorter than the trim."""
    validate_optional_video_trim(trim_start_ms, trim_end_ms, max_duration_ms=max_duration_ms)
    status = ((media.get("mediaMetadata") or {}).get("mediaStatus") or {}).get(
        "mediaGenerationStatus", ""
    )
    if status and status != "MEDIA_GENERATION_STATUS_SUCCESSFUL":
        raise ValueError(f"Source video is not ready for editing: {status}")
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    if isinstance(video.get("userUploadedVideo"), dict) or isinstance(
        video.get("uploadedVideo"), dict
    ):
        uploaded_video = True
    elif isinstance(video.get("generatedVideo"), dict):
        uploaded_video = False

    dimensions = video.get("dimensions") or {}
    length = dimensions.get("length", dimensions.get("duration"))
    duration = _seconds(length) if length is not None else None
    offset = video.get("videoOffset") or {}
    if duration is None and offset.get("endOffset") is not None:
        duration = _seconds(offset["endOffset"]) - _seconds(offset.get("startOffset", "0s"))
    if duration is None and trim_start_ms is None:
        return {}

    start = Decimal(trim_start_ms or 0) / 1000
    maximum = Decimal(max_duration_ms) / 1000
    end = Decimal(trim_end_ms) / 1000 if trim_end_ms is not None else start + maximum
    if duration is not None:
        end = min(end, duration, start + maximum)
    # Flow's asset categories use 30/24 fps; this is not the source file's frame rate.
    fps = 30 if uploaded_video else 24
    start_frame = int((start * fps).to_integral_value(rounding=ROUND_HALF_UP))
    end_frame = int((end * fps).to_integral_value(rounding=ROUND_HALF_UP))
    if start >= end or start_frame >= end_frame:
        raise ValueError("Video trim starts at or beyond the source video end")
    return {"startFrameIndex": start_frame, "endFrameIndex": end_frame}
