"""Pure UploadImage/UpsampleImage JSON-proto contracts from Flow's frontend.

Rechecked against the public 2026-11-05 frontend: UploadImage's esb caller
requests UPLOAD_IMAGE captcha and ssb requests IMAGE_GENERATION for upsample.
The download UI maps 2K -> 1, 4K -> 2 and reads response field2 as bytes,
rather than the optional field1 media URL.
"""

import base64
import binascii
from typing import Any

UPLOAD_IMAGE_RPC = "maseQ"
UPSAMPLE_IMAGE_RPC = "SPrCad"

_RESOLUTIONS = {
    "2K": 1,
    "4K": 2,
    "UPSAMPLE_IMAGE_RESOLUTION_2K": 1,
    "UPSAMPLE_IMAGE_RESOLUTION_4K": 2,
}


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Flow image {label} is missing")
    return value.strip()


def _context(project_id: str, captcha_token: str | None = None) -> list:
    context = [None, 22, None, None, None, _string(project_id, "project")]
    if captcha_token is not None:
        context.extend([None, None, None, None, [_string(captcha_token, "captcha"), 1]])
    return context


def build_upload_image_request(
    project_id: str,
    image_bytes: bytes,
    mime_type: str,
    file_name: str,
    captcha_token: str | None = None,
) -> list:
    """Build a plain upload, omitting optional crop/hidden/destination fields."""
    validate_upload_image_inputs(project_id, image_bytes, mime_type, file_name)
    return [
        _context(project_id, captcha_token),
        base64.b64encode(image_bytes).decode("ascii"),
        mime_type,
        True,
        None,
        None,
        None,
        None,
        _string(file_name, "file name"),
    ]


def validate_upload_image_inputs(
    project_id: str, image_bytes: bytes, mime_type: str, file_name: str
) -> None:
    """Reject invalid input before acquiring a short-lived remote captcha."""
    _string(project_id, "project")
    _string(file_name, "file name")
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("Flow image upload bytes are missing")
    if not isinstance(mime_type, str) or any(character.isspace() for character in mime_type):
        raise ValueError("Flow image upload MIME type is invalid")
    mime_type = _string(mime_type, "MIME type")
    if not mime_type.startswith("image/"):
        raise ValueError("Flow image upload MIME type is invalid")


def parse_upload_image_response(payload: Any) -> str:
    """UploadImage response field1 is bz media; its field1 is the real name."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        raise ValueError("Flow image upload response is invalid")
    media = payload[0]
    return _string(media[0] if media else None, "uploaded media name")


def build_upsample_image_request(
    project_id: str, media_id: str, target_resolution: str, captcha_token: str
) -> list:
    """Build the download upsampler request, using its distinct resolution enum."""
    validate_upsample_image_inputs(project_id, media_id, target_resolution)
    return [
        _string(media_id, "upsample media"),
        _RESOLUTIONS[target_resolution],
        _context(project_id, _string(captcha_token, "captcha")),
    ]


def validate_upsample_image_inputs(project_id: str, media_id: str, target_resolution: str) -> None:
    """Validate protected download inputs without consuming a remote solve."""
    _string(project_id, "project")
    _string(media_id, "upsample media")
    if not isinstance(target_resolution, str) or target_resolution not in _RESOLUTIONS:
        raise ValueError("Flow image upsample resolution is invalid")


def parse_upsample_image_response(payload: Any) -> str:
    """Return nonempty response field2 bytes as canonical base64 for the pipeline."""
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], str):
        raise ValueError("Flow image upsample response bytes are missing")
    value = payload[1]
    try:
        # Protobuf JSON bytes support standard and URL-safe alphabets, padded or not.
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("Flow image upsample response bytes are invalid") from None
    if not decoded:
        raise ValueError("Flow image upsample response bytes are empty")
    return base64.b64encode(decoded).decode("ascii")
