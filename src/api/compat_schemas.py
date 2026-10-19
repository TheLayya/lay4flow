"""Public request and response descriptions for the gateway compatibility API."""

from ..core.media_skus import MEDIA_SKUS


def model_field(kind=None):
    return {
        "type": "string",
        "enum": [
            name for name, entry in MEDIA_SKUS.items() if kind is None or entry["type"] == kind
        ],
        "description": "Exact sales SKU; its billable specification cannot be overridden.",
    }


def request_body(properties, required, media_type="application/json"):
    return {
        "requestBody": {
            "required": True,
            "content": {
                media_type: {
                    "schema": {"type": "object", "required": required, "properties": properties}
                }
            },
        }
    }


def json_response(schema, description="Successful response"):
    return {
        "200": {
            "description": description,
            "content": {"application/json": {"schema": schema}},
        }
    }


MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "id": model_field(),
        "object": {"type": "string", "enum": ["model"]},
        "type": {"type": "string", "enum": ["image", "video"]},
        "public_model": {"type": "string"},
        "description": {"type": "string"},
        "locked_spec": {"type": "object"},
        "allowed_modes": {"type": "array", "items": {"type": "string"}},
        "capabilities": {"type": "object"},
        "defaults": {"type": "object"},
        "supported_endpoints": {"type": "array", "items": {"type": "string"}},
    },
}
VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "object": {"type": "string", "enum": ["video"]},
        "model": model_field("video"),
        "status": {"type": "string", "enum": ["queued", "in_progress", "completed", "failed"]},
        "prompt": {"type": "string"},
        "progress": {"type": "integer", "minimum": 0, "maximum": 100},
        "seconds": {"type": "string"},
        "size": {"type": "string"},
        "created_at": {"type": "integer"},
        "completed_at": {"type": ["integer", "null"]},
        "expires_at": {"type": ["integer", "null"]},
        "error": {"type": ["object", "null"]},
    },
}


def list_schema(item):
    return {
        "type": "object",
        "properties": {
            "object": {"type": "string", "enum": ["list"]},
            "data": {"type": "array", "items": item},
            "has_more": {"type": "boolean"},
            "first_id": {"type": ["string", "null"]},
            "last_id": {"type": ["string", "null"]},
        },
    }


USAGE_SCHEMA = {
    "type": "object",
    "description": "Fixed media settlement units, not measured upstream text tokens.",
    "properties": {
        "prompt_tokens": {"type": "integer", "enum": [1000]},
        "completion_tokens": {"type": "integer", "enum": [0]},
        "total_tokens": {"type": "integer", "enum": [1000]},
    },
}
ERROR_RESPONSES = {
    str(status): {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                name: {"type": "string"}
                                for name in ("message", "type", "code", "param")
                            },
                        }
                    },
                }
            }
        },
    }
    for status, description in (
        (400, "Invalid field or conflicting SKU specification"),
        (401, "Invalid Bearer API key"),
        (404, "Model or owned task not found"),
        (409, "Task state or idempotency conflict"),
        (410, "Generated content expired"),
        (413, "Input too large"),
        (415, "Unsupported content type"),
        (502, "Generation or output specification validation failed"),
        (503, "No available generation capacity"),
        (504, "Generation timed out"),
    )
}


def chat_schema():
    result = request_body(
        {
            "model": model_field(),
            "messages": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["role", "content"],
                    "properties": {
                        "role": {"type": "string"},
                        "content": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "object"}},
                            ]
                        },
                    },
                },
            },
            "stream": {"type": "boolean", "default": False},
            "n": {"type": "integer", "enum": [1]},
            "generationConfig": {
                "type": "object",
                "description": "Allowed aspect/mode or edit trim fields; SKU cost fields are locked.",
            },
        },
        ["model", "messages"],
    )
    result["responses"] = {
        **ERROR_RESPONSES,
        "200": {
            "description": "A validated completed result, or SSE emitted after completion",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "model": model_field(),
                            "choices": {"type": "array", "items": {"type": "object"}},
                            "media": {"type": "object"},
                            "usage": USAGE_SCHEMA,
                        },
                    }
                },
                "text/event-stream": {"schema": {"type": "string"}},
            },
        },
    }
    return result


def image_schema(edit=False):
    fields = {
        "model": model_field("image"),
        "prompt": {"type": "string", "minLength": 1, "maxLength": 32000},
        "n": {"type": "integer", "enum": [1], "default": 1},
        "size": {"type": "string", "default": "auto", "description": "Aspect ratio hint"},
        "quality": {"type": "string", "enum": ["auto", "standard", "high", "hd"]},
        "response_format": {"type": "string", "enum": ["url", "b64_json"], "default": "url"},
        "generationConfig": {"type": "string" if edit else "object"},
    }
    required = ["model", "prompt"]
    if edit:
        fields["image"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {"type": "string", "format": "binary"},
            "description": "image or repeated image[] PNG/JPEG/WebP files",
        }
        required.append("image")
    result = request_body(fields, required, "multipart/form-data" if edit else "application/json")
    result["responses"] = {
        **ERROR_RESPONSES,
        "200": {
            "description": "One validated image",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "created": {"type": "integer"},
                            "data": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "url": {"type": "string"},
                                        "b64_json": {"type": "string"},
                                    },
                                },
                            },
                            "usage": USAGE_SCHEMA,
                        },
                    }
                }
            },
        },
    }
    return result
