"""Server-owned Omni extension length contracts, independent of user-provided hints."""

import re
from decimal import Decimal, InvalidOperation

from ..vids.video_input import mp4_metadata


def uploaded_extension_duration_ms(data, mime):
    if mime != "video/mp4":
        raise ValueError("Omni Extend currently accepts MP4 source uploads only")
    try:
        metadata = mp4_metadata(
            data, minimum_duration=3, maximum_duration=60, max_bytes=100 * 1024**2
        )
    except ValueError:
        raise ValueError("Omni Extend requires a valid 3 to 60 second MP4 up to 100 MiB") from None
    return round(metadata["duration"] * 1000)


def processed_uploaded_extension(metadata, local_duration_ms):
    if (
        type(metadata.get("source_duration_ms")) is not int
        or abs(metadata["source_duration_ms"] - local_duration_ms) > 250
    ):
        raise ValueError("Processed original video duration differs from the uploaded source")
    return metadata


def source_media_id(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,1023}", value)
        or "//" in value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("input_video_media_id must be an existing Flow media ID, not a URL")
    return value


def extension_metadata(result, identifier, added_seconds):
    """Require source metadata read on the selected account before paid submission."""
    source_media_id(identifier)
    if type(added_seconds) is not int or not 3 <= added_seconds <= 10:
        raise ValueError("Omni Extend requires 3 to 10 added seconds")
    matches = [
        row
        for row in result.get("media", [])
        if isinstance(row, dict) and row.get("name") == identifier
    ]
    if len(matches) != 1:
        raise ValueError("Original Flow video metadata is unavailable for this account")
    media = matches[0]
    state = ((media.get("mediaMetadata") or {}).get("mediaStatus") or {}).get(
        "mediaGenerationStatus"
    )
    if state != "MEDIA_GENERATION_STATUS_SUCCESSFUL":
        raise ValueError("Original Flow video must be successfully completed before extending")
    video = media.get("video") or {}
    dimensions = video.get("dimensions") or {}
    raw = dimensions.get("length", dimensions.get("duration"))
    try:
        seconds = Decimal(str(raw).removesuffix("s"))
        if not seconds.is_finite() or not 3 <= seconds <= 60:
            raise ValueError
        milliseconds = int((seconds * 1000).to_integral_value())
    except (ValueError, InvalidOperation, TypeError, OverflowError):
        raise ValueError("Original Flow video duration metadata is invalid") from None
    return {
        "source_media_id": identifier,
        "source_duration_ms": milliseconds,
        "added_seconds": added_seconds,
        "expected_duration_ms": milliseconds + added_seconds * 1000,
    }


def expected_extension_duration_ms(
    options, identifier, metadata, *, uploaded_video=None, mime="video/mp4"
):
    """Verify trusted completion metadata agrees with the normalized request."""
    if not isinstance(metadata, dict):
        raise ValueError("Completed extension lacks authoritative source duration metadata")
    if uploaded_video is not None:
        if identifier is not None:
            raise ValueError("Extension cannot have both an uploaded source and a source media ID")
        expected_source = source_media_id(metadata.get("source_media_id"))
        local_duration = uploaded_extension_duration_ms(uploaded_video, mime)
        processed_uploaded_extension(metadata, local_duration)
    else:
        expected_source = source_media_id(identifier)
    source = metadata.get("source_duration_ms")
    added = metadata.get("added_seconds")
    expected = metadata.get("expected_duration_ms")
    if (
        metadata.get("source_media_id") != expected_source
        or type(source) is not int
        or not 3000 <= source <= 60000
        or type(added) is not int
        or not 3 <= added <= 10
        or added != options.get("durationSeconds")
        or type(expected) is not int
        or expected != source + added * 1000
    ):
        raise ValueError(
            "Completed extension metadata does not match the original source and added duration"
        )
    return expected
