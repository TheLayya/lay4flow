"""OpenAPI fragments for the verified Vids HTTP adapter.

These are deliberately plain dictionaries: the runtime routes keep their
existing request parsing and response objects, while both the standalone and
account-bound routers can share one contract description.
"""

from copy import deepcopy

from ..services.vids.client import PRESET_VOICE_OPTIONS, PRESET_VOICES
from ..services.vids.gateway import (
    IMAGE_ASPECTS,
    IMAGE_STYLES,
    SUPPORTED_VIDEO_SPECIFICATIONS,
    VERIFIED_VIDEO_SPECIFICATIONS,
    VIDEO_SHOT_MODES,
    VIDEO_SIZES,
)

ERROR = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["message", "type", "code"],
            "properties": {
                "message": {"type": "string"},
                "type": {"type": "string"},
                "code": {"type": "string"},
                "param": {"type": ["string", "null"]},
            },
        }
    },
}

ERROR_RESPONSES = {
    str(status): {"description": description, "content": {"application/json": {"schema": ERROR}}}
    for status, description in (
        (400, "Invalid request or unsupported parameter"),
        (401, "Invalid Bearer API key"),
        (404, "Generation or account not found"),
        (409, "Generation is pending or idempotency conflict"),
        (410, "Generated content has expired"),
        (413, "Request body is too large"),
        (415, "Unsupported content type"),
        (429, "The selected account is busy"),
        (502, "Upstream generation or content validation failed"),
        (503, "Vids session or account is unavailable"),
    )
}
ERROR_RESPONSES["default"] = {
    "description": "Other errors use the same OpenAI error envelope",
    "content": {"application/json": {"schema": ERROR}},
}

RECEIPT = {
    "type": "object",
    "required": ["id", "object", "kind", "status", "created_at", "error", "content_url"],
    "properties": {
        "id": {"type": "string", "pattern": r"^vids_[a-f0-9]{64}$"},
        "object": {"type": "string", "enum": ["vids.generation", "vids.document_image"]},
        "kind": {"type": "string", "enum": ["image", "speech", "video"]},
        "status": {
            "type": "string",
            "enum": [
                "generating",
                "inserting",
                "received",
                "completed",
                "failed",
                "no_result",
                "specification_mismatch",
                "download_failed",
                "unknown",
            ],
        },
        "created_at": {"type": "integer"},
        "completed_at": {"type": ["integer", "null"]},
        "error": {"type": ["string", "null"]},
        "error_operation": {
            "type": "string",
            "enum": ["upload-start", "upload-finalize", "generate", "getSettings"],
        },
        "upstream_error": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        "content_url": {"type": ["string", "null"]},
        "metadata": {"type": "object"},
        "outputs": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "description": "All completed images for a multi-image request, in upstream order. index=0 remains the legacy content_url.",
            "items": {
                "type": "object",
                "required": ["index", "metadata", "content_url"],
                "properties": {
                    "index": {"type": "integer", "minimum": 0, "maximum": 3},
                    "metadata": {"type": "object"},
                    "content_url": {"type": "string"},
                },
            },
        },
    },
}

VIDEO = {
    "type": "object",
    "required": ["id", "object", "model", "status", "progress", "created_at", "error"],
    "properties": {
        "id": {"type": "string", "pattern": r"^vids_[a-f0-9]{64}$"},
        "object": {"type": "string", "enum": ["video"]},
        "model": {"type": "string", "enum": ["vids-video"]},
        "status": {
            "type": "string",
            "enum": ["in_progress", "received", "completed", "failed", "unknown"],
        },
        "progress": {"type": "integer", "minimum": 0, "maximum": 100},
        "created_at": {"type": "integer"},
        "completed_at": {"type": ["integer", "null"]},
        "expires_at": {"type": ["integer", "null"]},
        "prompt": {"type": ["string", "null"]},
        "seconds": {
            "type": ["string", "null"],
            "pattern": r"^\d+(?:\.\d+)?$",
            "description": "Requested output duration in seconds; extension includes the source duration",
        },
        "size": {"type": ["string", "null"]},
        "error": {
            "type": ["object", "null"],
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
        },
        "vids_status": {"type": "string"},
        "receipt_url": {"type": "string"},
        "retry_download_url": {"type": ["string", "null"]},
        "content_url": {"type": ["string", "null"]},
    },
}

LIST_VIDEO = {
    "type": "object",
    "required": ["object", "data", "has_more", "first_id", "last_id"],
    "properties": {
        "object": {"type": "string", "enum": ["list"]},
        "data": {"type": "array", "items": VIDEO},
        "has_more": {"type": "boolean"},
        "first_id": {"type": ["string", "null"]},
        "last_id": {"type": ["string", "null"]},
    },
}

IMAGE_RESULT = {
    "type": "object",
    "required": ["created", "data"],
    "properties": {
        "created": {"type": "integer"},
        "data": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "b64_json": {"type": "string", "contentEncoding": "base64"},
                },
            },
        },
    },
}

MODEL_LIST = {
    "type": "object",
    "required": ["object", "data"],
    "properties": {
        "object": {"type": "string", "enum": ["list"]},
        "data": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "object", "owned_by", "provider", "kind", "supported_endpoints"],
                "properties": {
                    "id": {"type": "string"},
                    "object": {"type": "string", "enum": ["model"]},
                    "owned_by": {"type": "string", "enum": ["google"]},
                    "provider": {"type": "string", "enum": ["vids"]},
                    "kind": {"type": "string"},
                    "supported_endpoints": {"type": "array", "items": {"type": "string"}},
                    "capabilities": {"type": "object"},
                },
            },
        },
    },
}

QUOTA = {
    "type": "object",
    "required": ["kind", "total", "available", "consumed"],
    "properties": {
        "kind": {"type": "string", "enum": ["video", "image", "speech"]},
        "total": {"type": "integer", "minimum": 0},
        "available": {"type": "integer", "minimum": 0},
        "consumed": {"type": "integer", "minimum": 0},
    },
}


def request_body(schema, media_type="application/json"):
    return {"required": True, "content": {media_type: {"schema": schema}}}


def operation(*, body=None, responses=None, idempotent=False):
    """Build one openapi_extra object without changing route behavior."""
    result = {
        "responses": {
            **{
                status: response
                for status, response in ERROR_RESPONSES.items()
                if status != "default"
            },
            **(responses or {}),
        }
    }
    if body is not None:
        result["requestBody"] = body
    if idempotent:
        result["parameters"] = [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "description": "Keep the same key and parameters to retrieve the original receipt; unknown acceptance is never resubmitted.",
                "schema": {"type": "string", "minLength": 1, "maxLength": 200},
            }
        ]
    # FastAPI merges response dictionaries in place; do not share mutable schema lists
    # across routes or repeated OpenAPI builds.
    return deepcopy(result)


def json_response(schema, description="Successful response", status=200):
    return {
        str(status): {
            "description": description,
            "content": {"application/json": {"schema": schema}},
        }
    }


def file_response(
    description="Binary media content", mime_types=("image/jpeg", "audio/wav", "video/mp4")
):
    return {
        "200": {
            "description": description,
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}} for mime in mime_types
            },
        }
    }


COOKIES_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cookies"],
    "properties": {"cookies": {"type": "array", "items": {"type": "object"}, "minItems": 1}},
}

DOCUMENT_IMAGE_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["image_base64", "scene_id"],
    "properties": {
        "image_base64": {
            "type": "string",
            "contentEncoding": "base64",
            "description": "PNG, JPEG or WebP, at most 10 MiB decoded",
        },
        "scene_id": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]{1,4096}$"},
    },
}

EMPTY_JSON_REQUEST = {"type": "object", "additionalProperties": False}


GENERATION_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "prompt"],
    "properties": {
        "kind": {"type": "string", "enum": ["image", "speech", "video"]},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 32000},
        "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
        "mode": {
            "type": "string",
            "enum": ["text", "animate", "ingredients", "edit", "extend", "upscale"],
        },
        "seconds": {"type": "integer", "minimum": 3, "maximum": 10},
        "size": {"type": "string", "enum": list(VIDEO_SIZES)},
        "aspect_ratio": {"type": "string", "enum": list(IMAGE_ASPECTS)},
        "style": {"type": "string", "enum": list(IMAGE_STYLES)},
        "shot_mode": {"type": "string", "enum": list(VIDEO_SHOT_MODES)},
        "voice": {
            "type": "string",
            "enum": list(PRESET_VOICES),
            "default": "Puck",
            "description": "Puck, Gacrux and Algenib have passed short English WAV generation and authenticated download checks. Other preset voices have not been individually verified.",
        },
        "source_id": {"type": "string"},
        "shape_id": {"type": "string", "minLength": 1, "maxLength": 4096, "pattern": r"^\S+$"},
        "image_base64": {"type": "string", "contentEncoding": "base64", "maxLength": 14 * 1024**2},
        "video_base64": {
            "type": "string",
            "contentEncoding": "base64",
            "maxLength": 70 * 1024**2,
            "description": "Uploaded MP4 up to 50 MiB; edit/extend accept 3 to 60 seconds and use the last 10 seconds; upscale accepts 3 to 10 seconds",
        },
        "images_base64": {
            "type": "array",
            "minItems": 1,
            "maxItems": 7,
            "items": {"type": "string", "contentEncoding": "base64", "maxLength": 14 * 1024**2},
        },
    },
    "description": "Image/video/speech integration only. Reference images must be PNG, JPEG or WebP with at most 10 MiB decoded in total; the JSON body limit is 85 MiB including an optional 50 MiB MP4 edit input.",
}


def generation_request(*, console=False):
    """Document mode-specific fields and verified specification pairs."""
    common = GENERATION_REQUEST["properties"]
    variants = []
    for kind, modes in (
        ("image", ("text", "edit")),
        ("speech", (None,)),
        ("video", tuple(SUPPORTED_VIDEO_SPECIFICATIONS)),
    ):
        for mode in modes:
            fields = {"kind": {"type": "string", "enum": [kind]}, "prompt": common["prompt"]}
            required = ["kind", "prompt"]
            if mode is not None:
                fields["mode"] = {"type": "string", "enum": [mode]}
                if mode != "text":
                    required.append("mode")
            if kind == "image" and mode == "text":
                for name in ("aspect_ratio", "style", "n"):
                    fields[name] = common[name]
            if kind == "speech":
                fields["voice"] = common["voice"]
            if kind == "image" and mode == "edit":
                field = "source_id" if console else "shape_id"
                fields[field] = (
                    {
                        "type": "string",
                        "pattern": r"^vids_image_[a-f0-9]{64}$",
                        "description": "Opaque saved document image returned by the console source list",
                    }
                    if console
                    else common["shape_id"]
                )
                fields["image_base64"] = common["image_base64"]
            if kind == "video":
                for name in ("seconds", "size", "shot_mode"):
                    fields[name] = common[name]
                if mode in {"edit", "extend", "upscale"}:
                    fields["video_base64"] = common["video_base64"]
                    required.append("video_base64")
                if mode == "animate":
                    fields["image_base64"] = common["image_base64"]
                    required.append("image_base64")
                if mode in {"ingredients", "edit"}:
                    fields["images_base64"] = {
                        **common["images_base64"],
                        "maxItems": 3 if mode == "edit" else 7,
                    }
                    if mode == "ingredients":
                        required.append("images_base64")
            variant = {
                "type": "object",
                "properties": fields,
                "required": required,
                "additionalProperties": False,
            }
            if kind == "image" and mode == "edit":
                variant["oneOf"] = [
                    {"required": [field], "not": {"required": ["image_base64"]}},
                    {"required": ["image_base64"], "not": {"required": [field]}},
                ]
            if kind == "video":
                variant["anyOf"] = [
                    {
                        "properties": {"seconds": {"enum": [seconds]}, "size": {"enum": [size]}},
                        "required": (["seconds"] if seconds != 3 else [])
                        + (["size"] if size != "1280x720" else []),
                    }
                    for seconds, size in SUPPORTED_VIDEO_SPECIFICATIONS[mode]
                ]
            variants.append(variant)
    return {"oneOf": variants, "description": GENERATION_REQUEST["description"]}


def media_capabilities(kind, account_prefix, *, console=False):
    if kind == "video":
        return {
            "modes": list(VERIFIED_VIDEO_SPECIFICATIONS),
            "max_reference_images": 7,
            "max_edit_reference_images": 3,
            "shot_modes": list(VIDEO_SHOT_MODES),
            "edit_reference_images": True,
            "source_sizes": list(VIDEO_SIZES),
            "video_edit_sources": ["uploaded_mp4"],
            "video_extend_sources": ["uploaded_mp4"],
            "video_upscale_sources": ["uploaded_mp4"],
            "input_video_formats": ["mp4"],
            "max_input_video_bytes": 50 * 1024**2,
            "max_input_video_seconds": 60,
            "max_input_video_seconds_by_mode": {"edit": 60, "extend": 60, "upscale": 10},
            "seconds": list(range(3, 11)),
            "size": "1280x720",
            "extend_seconds": "additional duration; output is the uploaded source's last 10 seconds plus requested seconds",
            "max_source_seconds": {"edit": 20, "extend": 10, "upscale": 10},
            "extend_output_seconds": "min(uploaded source duration, 10) plus requested seconds",
            "supported_specifications": [
                {"mode": mode, "seconds": seconds, "size": size}
                for mode, specifications in SUPPORTED_VIDEO_SPECIFICATIONS.items()
                for seconds, size in specifications
            ],
            "verified_specifications": [
                {"mode": mode, "seconds": seconds, "size": size}
                for mode, specifications in VERIFIED_VIDEO_SPECIFICATIONS.items()
                for seconds, size in specifications
            ],
        }
    if kind == "image":
        result = {
            "modes": ["text", "edit"],
            "count": 1,
            "counts": [1, 2, 3, 4],
            "edit_counts": [1],
            "verified_counts": [1],
            "aspect": "square",
            "verified_aspects": ["square"],
            "aspect_ratios": list(IMAGE_ASPECTS),
            "styles": list(IMAGE_STYLES),
            "image_response_formats": ["url", "b64_json"],
            "image_url_authentication": "Admin session required"
            if console
            else "Bearer API key required",
            "image_sizes": ["auto", "1024x1024", *IMAGE_ASPECTS],
            "edit_source": "uploaded_image",
            "edit_output_size": "upstream_defined",
            "edit_sources": [
                "uploaded_image",
                "saved_document_image_reference" if console else "saved_document_image_shape",
            ],
            "image_edit_endpoint": account_prefix + "/v1/images/edits",
            "image_input_formats": ["png", "jpeg", "webp"],
            "max_input_images": 1,
            "max_input_image_bytes": 10 * 1024**2,
        }
        if console or not account_prefix:
            result.update(
                edit_source_endpoint=account_prefix + "/v1/vids/document/images",
                upload_insert_endpoint=account_prefix + "/v1/vids/document/images",
                insert_scene_endpoint=account_prefix + "/v1/vids/document/scenes",
            )
        return result
    return {
        "voices": list(PRESET_VOICES),
        "voice_options": list(PRESET_VOICE_OPTIONS),
        "accepted_generic_voices": list(PRESET_VOICES),
        "verified_voices": ["Puck", "Gacrux", "Algenib"],
        "speech_response_formats": ["wav"],
        "speech_input_limit": 4096,
    }


CONSOLE_DOCUMENT_IMAGE_REQUEST = {
    **DOCUMENT_IMAGE_REQUEST,
    "properties": {
        **DOCUMENT_IMAGE_REQUEST["properties"],
        "scene_id": {
            "type": "string",
            "pattern": r"^vids_scene_[a-f0-9]{64}$",
            "description": "Opaque scene reference returned by the console scene list",
        },
    },
}

CONSOLE_DOCUMENT_IMAGE_RESULT = {
    **RECEIPT,
    "properties": {
        **RECEIPT["properties"],
        "source_id": {"type": ["string", "null"], "pattern": r"^vids_image_[a-f0-9]{64}$"},
    },
}


def console_document_list_response(scenes=False):
    return json_response(
        {
            "type": "object",
            "required": ["object", "data"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["scene_id" if scenes else "source_id", "label"],
                        "properties": {
                            "scene_id" if scenes else "source_id": {"type": "string"},
                            "label": {"type": "string"},
                        },
                    },
                },
            },
        }
    )


SPEECH_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model", "input", "voice", "response_format"],
    "properties": {
        "model": {"type": "string", "enum": ["vids-speech"]},
        "input": {"type": "string", "minLength": 1, "maxLength": 4096},
        "voice": {"type": "string", "enum": list(PRESET_VOICES)},
        "response_format": {"type": "string", "enum": ["wav"]},
    },
}


def speech_responses():
    receipt = {
        "type": "object",
        "required": ["id", "kind", "status", "error", "content_url"],
        "properties": {
            name: RECEIPT["properties"][name]
            for name in (
                "id",
                "kind",
                "status",
                "error",
                "error_operation",
                "content_url",
                "upstream_error",
            )
        },
    }
    error = {**ERROR, "properties": {**ERROR["properties"], "vids_receipt": receipt}}
    return {
        **file_response("Verified WAV speech content", ("audio/wav",)),
        **json_response(error, "Pending speech receipt; do not resubmit", 409),
        **json_response(error, "Speech failed; safe receipt diagnostics", 502),
    }


IMAGE_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model", "prompt"],
    "properties": {
        "model": {"type": "string", "enum": ["vids-image"]},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 32000},
        "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
        "size": {
            "type": "string",
            "enum": ["auto", "1024x1024", *IMAGE_ASPECTS],
            "default": "auto",
        },
        "response_format": {"type": "string", "enum": ["url", "b64_json"], "default": "url"},
        "aspect_ratio": {"type": "string", "enum": list(IMAGE_ASPECTS)},
        "style": {"type": "string", "enum": list(IMAGE_STYLES)},
    },
}

IMAGE_EDIT_REQUEST = {
    **IMAGE_REQUEST,
    "required": ["model", "prompt"],
    "properties": {
        **{
            name: value
            for name, value in IMAGE_REQUEST["properties"].items()
            if name not in {"aspect_ratio", "style"}
        },
        "size": {"type": "string", "enum": ["auto", "1024x1024"], "default": "auto"},
        "n": {"type": "string", "enum": ["1"], "default": "1"},
        "image": {"type": "string", "format": "binary"},
        "image[]": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {"type": "string", "format": "binary"},
        },
    },
    "oneOf": [
        {"required": ["image"], "not": {"required": ["image[]"]}},
        {"required": ["image[]"], "not": {"required": ["image"]}},
    ],
    "description": "Upload exactly one PNG, JPEG or WebP image up to 10 MiB with the edit prompt. Document scene selection and insertion are automatic.",
}

VIDEO_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model", "prompt"],
    "properties": {
        "model": {"type": "string", "enum": ["vids-video"]},
        "prompt": GENERATION_REQUEST["properties"]["prompt"],
        "mode": GENERATION_REQUEST["properties"]["mode"],
        "seconds": {
            "oneOf": [
                {"type": "integer", "minimum": 3, "maximum": 10},
                {"type": "string", "enum": [str(value) for value in range(3, 11)]},
            ]
        },
        "size": {"type": "string", "enum": list(VIDEO_SIZES)},
        "shot_mode": GENERATION_REQUEST["properties"]["shot_mode"],
        "image_base64": GENERATION_REQUEST["properties"]["image_base64"],
        "video_base64": GENERATION_REQUEST["properties"]["video_base64"],
        "images_base64": GENERATION_REQUEST["properties"]["images_base64"],
    },
}

VIDEO_MULTIPART_REQUEST = {
    **VIDEO_REQUEST,
    "properties": {
        **VIDEO_REQUEST["properties"],
        "seconds": {"type": "string", "enum": [str(value) for value in range(3, 11)]},
        "images_base64": {"type": "string", "description": "JSON array of base64 references"},
        "input_video": {
            "type": "string",
            "format": "binary",
            "description": "MP4 up to 50 MiB; edit/extend 3 to 60 seconds (last 10 used); upscale 3 to 10 seconds",
        },
        "input_reference": {"type": "string", "format": "binary"},
        "input_reference[]": {
            "type": "array",
            "maxItems": 7,
            "items": {"type": "string", "format": "binary"},
        },
        "images": {"type": "array", "maxItems": 7, "items": {"type": "string", "format": "binary"}},
        "images[]": {
            "type": "array",
            "maxItems": 7,
            "items": {"type": "string", "format": "binary"},
        },
    },
}

VIDEO_REQUEST["allOf"] = [
    {
        "if": {
            "properties": {"mode": {"enum": ["edit", "extend", "upscale"]}},
            "required": ["mode"],
        },
        "then": {"required": ["video_base64"]},
    }
]
VIDEO_MULTIPART_REQUEST["allOf"] = [
    {
        "if": {
            "properties": {"mode": {"enum": ["edit", "extend", "upscale"]}},
            "required": ["mode"],
        },
        "then": {
            "oneOf": [
                {"required": ["video_base64"], "not": {"required": ["input_video"]}},
                {"required": ["input_video"], "not": {"required": ["video_base64"]}},
            ]
        },
    }
]


def video_request_body():
    return {
        "required": True,
        "content": {
            "application/json": {"schema": VIDEO_REQUEST},
            "multipart/form-data": {"schema": VIDEO_MULTIPART_REQUEST},
        },
    }


def receipt_response(*, pending=False, account=False):
    schema = RECEIPT
    if account:
        schema = {
            **schema,
            "properties": {
                **schema["properties"],
                "receipt_url": {"type": "string"},
                "retry_download_url": {"type": ["string", "null"]},
            },
        }
    responses = json_response(
        schema, "Local receipt; a failed receipt is not a successful generation"
    )
    if pending:
        responses.update(
            json_response(schema, "Pending or unknown acceptance; do not submit again", 202)
        )
    return responses


def document_list_response(scenes=False):
    fields = {"scene_id": {"type": "string"}}
    if not scenes:
        fields["shape_id"] = {"type": "string"}
    return json_response(
        {
            "type": "object",
            "required": ["object", "revision", "data"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "revision": {"type": "integer"},
                "data": {"type": "array", "items": {"type": "object", "properties": fields}},
            },
        }
    )


SESSION_RESULT = {
    "type": "object",
    "required": ["updated", "transport"],
    "properties": {
        "updated": {"type": "boolean", "enum": [True]},
        "transport": {"type": "string", "enum": ["pure_http"]},
    },
}
