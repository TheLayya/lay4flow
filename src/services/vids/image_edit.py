"""Image editing for a caller-selected, verified image in a saved document snapshot."""

import json
from typing import Any


def build_document_image_edit_request(
    doc_id: str,
    flags: dict[str, Any],
    shape: dict[str, str],
    revision: int,
    document_session_id: str,
    prompt: str,
    *,
    session_id: str,
    request_id: str = "0",
) -> list[Any]:
    """Build the accepted sidebar schema; the caller verifies the snapshot and shape."""
    if not isinstance(flags, dict) or not isinstance(shape, dict):
        raise ValueError("A verified document image and editor flags are required")
    if type(revision) is not int or revision < 1:
        raise ValueError("A saved document revision is required")
    shape_id, cosmo_id, scene_id = (shape.get(key) for key in ("shape_id", "cosmo_id", "scene_id"))
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            doc_id,
            shape_id,
            cosmo_id,
            scene_id,
            document_session_id,
            prompt,
            session_id,
            request_id,
        )
    ):
        raise ValueError("Document image editing requires non-empty document and image context")
    selection = json.dumps(
        [
            46,
            [1],
            [2, 0, 0, 0, None, False],
            [5],
            [7],
            [8, [shape_id]],
            [11],
            [13],
            [15],
            [20],
            [24],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )

    def resource_context(primary: int) -> list[Any]:
        document = [
            doc_id,
            "application/vnd.google-apps.flix",
            revision,
            selection,
            primary,
            document_session_id,
            None,
            None,
            None,
            None,
            None,
            [None, None, [selection]],
        ]
        reference = [None] * 7 + [[None, None, [document]]]
        return [None, None, None, [[reference]]]

    context = [None] * 44
    context[0], context[4], context[6] = 9, session_id, request_id
    context[5] = [[1, 0, None, scene_id]]
    context[7], context[8] = resource_context(0), resource_context(1)
    context[11], context[13], context[19] = [4, 0], "en", 1
    context[27], context[40], context[43] = [], 0, []
    feature = 234 if flags.get("docs-eaiecsf") in (True, 1, "true", "1") else 205
    return [
        feature,
        None,
        context,
        [None, None, prompt] + [None] * 8 + [[None, None, cosmo_id]],
        [[0, 4, 0]],
        [1, None, [[None, "1", 1188]]],
        1,
    ]
