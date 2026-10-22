"""Pure UploadImage/UpsampleImage JSON-proto contracts from Flow's frontend.

The public 2026-10-21 wO1vlb.js registers both RPCs and builds their requests
(vbb/Dbb, lines 870/878). XRV0Af.js lines 2506/2517 maps 2K -> 1, 4K -> 2
and downloads UpsampleImage response field2 as bytes. It does not use its
optional field1 media URL for that download.
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
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("Flow image upload bytes are missing")
    if not isinstance(mime_type, str) or any(character.isspace() for character in mime_type):
        raise ValueError("Flow image upload MIME type is invalid")
    mime_type = _string(mime_type, "MIME type")
    if not mime_type.startswith("image/"):
        raise ValueError("Flow image upload MIME type is invalid")
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
    if not isinstance(target_resolution, str) or target_resolution not in _RESOLUTIONS:
        raise ValueError("Flow image upsample resolution is invalid")
    return [
        _string(media_id, "upsample media"),
        _RESOLUTIONS[target_resolution],
        _context(project_id, _string(captcha_token, "captcha")),
    ]


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
