"""Native image insertion into a single compacted Vids document snapshot."""

import json
import math
import re


def parse_save_config(html, doc_id):
    values = {}
    for key in ("xhrPrefix", "urlPrefix", "sid"):
        matches = list(re.finditer(r"\b" + key + r"\s*:\s*'((?:[^'\\]|\\.)*)'", html))
        if len(matches) != 1:
            raise ValueError("Vids save configuration is unavailable")
        values[key] = json.loads('"' + matches[0][1] + '"')
    prefix = values["xhrPrefix"] or values["urlPrefix"]
    if prefix != "/videos/d/" + doc_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", values["sid"]):
        raise ValueError("Unexpected Vids save configuration")
    return {"path": prefix + "/save", "sid": values["sid"]}


def document_scenes(model):
    chunk = model["chunk"]
    # Document registry: 0 is shape delete, 5 is property update, 59 is removeMediaClip.
    if any(c[0] in (0, 5, 59) for c in chunk):
        raise ValueError(
            "Document snapshots with shape deletes, property updates or media clip removals "
            "are unsupported"
        )
    return [
        c[1]
        for c in chunk
        if c[0] == 12 and len(c) == 5 and c[3] == 0 and isinstance(c[1], str) and c[1]
    ]


def image_insert_commands(model, shape_id, scene_id, image, url):
    """Build the native SQd image command; scene selection must be explicit."""
    chunk = model["chunk"]
    if not all(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,4096}", value)
        for value in (shape_id, scene_id)
    ):
        raise ValueError("Valid document shape and scene identifiers are required")
    scenes = document_scenes(model)
    if scene_id not in scenes or any(len(c) > 1 and c[1] == shape_id for c in chunk):
        raise ValueError("A saved scene and unused image shape are required")
    page = next((c[1] for c in chunk if c[0] == 1 and len(c) == 3), None)
    if (
        not isinstance(page, list)
        or len(page) != 2
        or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in page)
    ):
        raise ValueError("Document page dimensions are unavailable")
    width, height = image["width"], image["height"]
    if any(type(v) is not int or not 0 < v <= 32768 for v in (width, height)):
        raise ValueError("Invalid uploaded image dimensions")
    scale = min(page[0] * 0.8 / width, page[1] * 0.8 / height)
    transform = [scale, 0, 0, scale, (page[0] - width * scale) / 2, (page[1] - height * scale) / 2]
    properties = [
        15,
        [None, 4],
        39,
        url,
        177,
        1,
        9,
        height,
        8,
        width,
        19,
        [None, 3],
        22,
        381,
        49,
        image["cosmo_id"],
    ]
    return [[3, shape_id, 3, transform, properties, scene_id]]


def saved_revision(response, revision):
    if not isinstance(response, dict):
        raise ValueError("Unexpected document save acknowledgement")
    metadata = response.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Unexpected document save metadata")
    if metadata.get("needsTransformOnClient"):
        raise ValueError("Document revision changed; insertion was not confirmed")
    ranges = response.get("revisionRanges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("Document save acknowledgement is absent")
    previous = revision
    for bounds in ranges:
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or any(type(v) is not int for v in bounds)
            or bounds[0] <= previous
            or bounds[1] < bounds[0]
        ):
            raise ValueError("Invalid document save revision ranges")
        previous = bounds[1]
    return previous
