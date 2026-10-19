"""Cookie-authenticated Google Vids HTTP client.

Bootstrap, quota, settings, text/edit/animate video, image, music and speech were verified
without browser request execution. See docs/vids for exact tested specifications.
Callers supply refreshed cookies. This module never starts or drives a browser.
"""

import hashlib
import json
import re
import secrets
import time
import uuid
from contextvars import ContextVar
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

DOCS_ORIGIN = "https://docs.google.com"
BEFORE_PAID_REQUEST = ContextVar("vids_before_paid_request", default=None)
MIME_TYPE = "application/vnd.google-apps.flix"
CONTENT_TYPE = "application/json+protobuf"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FLAGS_PATTERN = re.compile(r"\b_docs_flag_initialData\s*=\s*")
MODEL_CHUNK_PATTERN = re.compile(r"\bvar\s+modelChunk\s*=\s*")
DOCUMENT_SESSION_PATTERN = re.compile(
    r"\bDOCS_warmStartDocumentLoader\.startLoad\s*\(\s*(\d+(?:\.0+)?)\s*,\s*"
    r"('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\s*(?:,|\))"
)
DOCUMENT_IMAGE_PATH_PATTERN = re.compile(
    r"/videos-images-(?:rt|qw|xx)/[A-Za-z0-9_-]+(?:=[A-Za-z0-9_-]+)*"
)
PRESET_VOICES = (
    "Achernar",
    "Achird",
    "Algenib",
    "Algieba",
    "Alnilam",
    "Aoede",
    "Autonoe",
    "Callirrhoe",
    "Charon",
    "Despina",
    "Enceladus",
    "Erinome",
    "Fenrir",
    "Gacrux",
    "Iapetus",
    "Kore",
    "Laomedeia",
    "Leda",
    "Orus",
    "Puck",
    "Pulcherrima",
    "Rasalgethi",
    "Sadachbia",
    "Sadaltager",
    "Schedar",
    "Sulafat",
    "Umbriel",
    "Vindemiatrix",
    "Zephyr",
    "Zubenelgenubi",
)


PRESET_VOICE_OPTIONS = (
    {"id": "Achernar", "name": "Nyla", "description": "柔和、音调较高"},
    {"id": "Achird", "name": "Elio", "description": "亲切友好、音调中等偏低"},
    {"id": "Algenib", "name": "Jett", "description": "低沉沙哑、音调较低"},
    {"id": "Algieba", "name": "Knox", "description": "平稳流畅、音调较低"},
    {"id": "Alnilam", "name": "Zeno", "description": "坚定、音调中等偏低"},
    {"id": "Aoede", "name": "Tova", "description": "轻松活泼、音调适中"},
    {"id": "Autonoe", "name": "Kaci", "description": "嘹亮、音调适中"},
    {"id": "Callirrhoe", "name": "Lani", "description": "轻松、音调适中"},
    {"id": "Charon", "name": "Holt", "description": "信息丰富、音调较低"},
    {"id": "Despina", "name": "Lora", "description": "平稳流畅、音调适中"},
    {"id": "Enceladus", "name": "Paz", "description": "带有气声、音调较低"},
    {"id": "Erinome", "name": "Tyra", "description": "清晰、音调适中"},
    {"id": "Fenrir", "name": "Kero", "description": "激动、音调中等偏低"},
    {"id": "Gacrux", "name": "Saro", "description": "成熟、音调适中"},
    {"id": "Iapetus", "name": "Nyx", "description": "清晰、音调中等偏低"},
    {"id": "Kore", "name": "Lito", "description": "坚定、音调适中"},
    {"id": "Laomedeia", "name": "Peli", "description": "欢快、音调较高"},
    {"id": "Leda", "name": "Fira", "description": "青春洋溢、音调较高"},
    {"id": "Orus", "name": "Cale", "description": "坚定、音调中等偏低"},
    {"id": "Puck", "name": "Neo", "description": "欢快、音调适中"},
    {"id": "Pulcherrima", "name": "Vira", "description": "激进，音调适中"},
    {"id": "Rasalgethi", "name": "Iro", "description": "信息丰富、音调适中"},
    {"id": "Sadachbia", "name": "Dori", "description": "生动活泼、音调较低"},
    {"id": "Sadaltager", "name": "Umi", "description": "知识丰富、音调适中"},
    {"id": "Schedar", "name": "Jacy", "description": "平稳、音调中等偏低"},
    {"id": "Sulafat", "name": "Fola", "description": "温暖、音调适中"},
    {"id": "Umbriel", "name": "Baya", "description": "轻松随意、音调中等偏低"},
    {"id": "Vindemiatrix", "name": "Orla", "description": "柔和、音调适中"},
    {"id": "Zephyr", "name": "Sani", "description": "嘹亮、音调较高"},
    {"id": "Zubenelgenubi", "name": "Yori", "description": "闲适、音调中等偏低"},
)


def sapisid_hash(cookie_value: str, timestamp: int) -> str:
    """Compute the signature for the Docs origin, not the request service origin."""
    value = f"{timestamp} {cookie_value} {DOCS_ORIGIN}".encode()
    digest = hashlib.sha1(value).hexdigest()
    return f"SAPISIDHASH {timestamp}_{digest}"


def parse_flags(html: str) -> dict[str, Any]:
    match = FLAGS_PATTERN.search(html)
    if match is None:
        raise ValueError("Vids editor initialization absent; refresh the Google session")
    try:
        flags, _ = json.JSONDecoder().raw_decode(html[match.end() :].lstrip())
    except ValueError:
        raise ValueError("Vids editor initialization is not JSON") from None
    if not isinstance(flags, dict):
        raise ValueError("Unexpected Vids editor initialization")
    keys = flags.get("docs-api-keys", {})
    if isinstance(keys, dict) and keys.get("espresso_api_key"):
        flags["espresso_api_key"] = keys["espresso_api_key"]
    # Some editor builds place keys in a separate initialization object.
    if not flags.get("espresso_api_key"):
        key = re.search(r'"espresso_api_key"\s*:\s*("(?:[^"\\]|\\.)*")', html)
        if key:
            flags["espresso_api_key"] = json.loads(key[1])
    return flags


def parse_model_chunk(html: str) -> dict[str, Any]:
    """Read the bootstrap initializer, not the loadModelChunk(variable) call."""
    matches = list(MODEL_CHUNK_PATTERN.finditer(html))
    if not matches:
        raise ValueError("Vids document initialization absent")
    if len(matches) != 1:
        raise ValueError("Multiple Vids document chunks are not supported")
    if len(re.findall(r"DOCS_warmStartDocumentLoader\.loadModelChunk\s*\(", html)) > 1:
        raise ValueError("Multiple Vids document chunks are not supported")
    match = matches[0]
    try:
        model, _ = json.JSONDecoder().raw_decode(html[match.end() :].lstrip())
    except ValueError:
        raise ValueError("Vids document initialization is not JSON") from None
    if (
        not isinstance(model, dict)
        or not isinstance(model.get("chunk"), list)
        or type(model.get("revision")) is not int
        or model["revision"] < 0
        or any(
            not isinstance(command, list) or not command or type(command[0]) is not int
            for command in model["chunk"]
        )
    ):
        raise ValueError("Unexpected Vids document initialization")
    return model


def parse_document_session(html: str, revision: int) -> str:
    """Read the loader session used by the document reference, without executing JS."""
    matches = list(DOCUMENT_SESSION_PATTERN.finditer(html))
    if len(matches) != 1 or float(matches[0][1]) != revision:
        raise ValueError("Vids document load session absent or revision mismatch")
    match = matches[0]
    literal = match[2]
    # Session IDs are opaque identifiers, not general JavaScript string literals.
    value = json.loads(literal) if literal.startswith('"') else literal[1:-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", value):
        raise ValueError("Unexpected Vids document load session")
    return value


def document_image_shapes(model_chunk: dict[str, Any]) -> list[dict[str, str]]:
    """Extract top-level image insert commands; this is not a model replay engine."""
    if not isinstance(model_chunk, dict) or not isinstance(model_chunk.get("chunk"), list):
        raise ValueError("A Vids document chunk is required")
    # Document registry: 0 is shape delete, 5 is property update, 59 is removeMediaClip.
    if any(
        isinstance(command, list) and command and command[0] in (0, 5, 59)
        for command in model_chunk["chunk"]
    ):
        raise ValueError(
            "Vids image snapshots with shape deletes, property updates or media clip removals "
            "are unsupported"
        )
    images = []
    for command in model_chunk["chunk"]:
        if (
            not isinstance(command, list)
            or len(command) < 3
            or type(command[0]) is not int
            or command[0] != 3
            or type(command[2]) is not int
            or command[2] != 3
        ):
            continue
        # SQd: [insert, shape ID, shape type, transform, property pairs, page ID].
        if (
            len(command) != 6
            or not isinstance(command[1], str)
            or not command[1]
            or not isinstance(command[3], list)
            or len(command[3]) != 6
            or any(type(value) not in (int, float) for value in command[3])
            or not isinstance(command[4], list)
            or len(command[4]) % 2
            or any(type(key) is not int for key in command[4][::2])
            or not isinstance(command[5], str)
            or not command[5]
        ):
            raise ValueError("Unexpected Vids image insert command")
        properties = dict(zip(command[4][::2], command[4][1::2]))
        cosmo_id = properties.get(49)
        if cosmo_id is None:
            continue
        if not isinstance(cosmo_id, str) or not cosmo_id:
            raise ValueError("Unexpected Vids image resource")
        images.append({"shape_id": command[1], "cosmo_id": cosmo_id, "scene_id": command[5]})
    return images


def enabled(flags: dict[str, Any], name: str) -> bool:
    return flags.get(name) in (True, 1, "true", "1")


def video_feature(flags: dict[str, Any]) -> int:
    """Follow the current frontend's text-only feature precedence."""
    for name, feature in (("flix-evgvl", 257), ("flix-eofpsq", 386), ("flix-eof", 367)):
        if enabled(flags, name):
            return feature
    if enabled(flags, "flix-evga"):
        return 374 if enabled(flags, "flix-evg1p2ui") else 342
    return 104


def image_video_feature(flags: dict[str, Any], mode: str) -> int:
    if mode not in {"animate", "ingredients"}:
        raise ValueError("Unsupported image input mode")
    features = {
        257: (258, 257),
        386: (387, 388),
        367: (368, 369),
        374: (375, 376),
    }
    pair = features.get(video_feature(flags))
    if pair is None:
        raise ValueError("Unsupported image-to-video feature version")
    return pair[mode == "ingredients"]


def cookie_jar(rows: list[dict[str, Any]]) -> CookieJar:
    """Keep browser cookie scope; never flatten host-only cookies into a header."""
    jar = CookieJar(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for row in rows:
        name, value, domain = (row.get(k) for k in ("name", "value", "domain"))
        if not all(isinstance(item, str) and item for item in (name, value, domain)):
            continue
        host = domain.lstrip(".").lower()
        if host not in {"google.com", "docs.google.com"}:
            continue
        if any(ch in name for ch in "=; \t\r\n\0") or any(ch in value for ch in ";\r\n\0"):
            continue
        path = row.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        secure = bool(row.get("secure"))
        host_only = row.get("hostOnly", not domain.startswith("."))
        if name.startswith("__Secure-") and not secure:
            continue
        if name.startswith("__Host-") and (not secure or not host_only or path != "/"):
            continue
        expires = row.get("expirationDate", row.get("expires"))
        if row.get("session") or expires == -1:
            expires = None
        cookie = Cookie(
            0,
            name,
            value,
            None,
            False,
            host if host_only else "." + host,
            not host_only,
            domain.startswith("."),
            path,
            True,
            secure,
            int(expires) if expires is not None else None,
            expires is None,
            None,
            None,
            {"HttpOnly": None} if row.get("httpOnly") else {},
        )
        if not cookie.is_expired():
            jar.set_cookie(cookie)
    return jar


def cookie_rows(jar: CookieJar) -> list[dict[str, Any]]:
    """Export the current scoped jar, including HTTP rotations and session cookies."""
    rows = []
    for cookie in jar:
        if cookie.is_expired() or cookie.domain.lstrip(".").lower() not in {
            "google.com",
            "docs.google.com",
        }:
            continue
        rows.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "hostOnly": not cookie.domain_specified,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires if cookie.expires is not None else -1,
                "session": cookie.discard,
                "httpOnly": cookie.has_nonstandard_attr("HttpOnly")
                or cookie.get_nonstandard_attr("http_only") == "True",
            }
        )
    return sorted(rows, key=lambda row: (row["domain"], row["path"], row["name"]))


def build_text_request(
    doc_id: str,
    flags: dict[str, Any],
    prompt: str,
    *,
    seconds: int = 3,
    resolution: str = "720p",
    orientation: str = "landscape",
    shot_mode: str = "auto",
    session_id: str | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    conversation_time_ms: int | None = None,
) -> list[Any]:
    """Frontend schema; verified combinations are published by the development gateway."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A non-empty prompt is required")
    if type(seconds) is not int or not 3 <= seconds <= 10:
        raise ValueError("Video duration must be an integer from 3 to 10 seconds")
    if not enabled(flags, "flix-evg1p2ui"):
        raise ValueError("This client requires the seconds-based video configuration")
    formats = {
        ("720p", "landscape"): 1,
        ("720p", "portrait"): 2,
        ("1080p", "landscape"): 5,
        ("1080p", "portrait"): 6,
    }
    if (resolution, orientation) not in formats:
        raise ValueError("Unsupported resolution or orientation")
    shots = {"auto": 0, "long_take": 1, "montage": 2}
    if shot_mode not in shots:
        raise ValueError("Unsupported video shot mode")
    doc = [doc_id, MIME_TYPE, None, None, 1]
    resource = [doc]
    reference = [None] * 7 + [[None, None, resource]]
    resource_context = [None, None, None, [[reference]]]
    context = [None] * 41
    context[0], context[4], context[6] = (
        9,
        session_id or f"goog_{secrets.randbelow(2**31)}",
        request_id or "0",
    )
    context[8] = resource_context
    context[11], context[13], context[19] = [0, 0], "en", 1
    context[23], context[40] = [[resource]], 0
    context[31] = str(uuid.uuid4())
    if conversation_id is not None:
        if not re.fullmatch(r"[a-f0-9]{32}", conversation_id):
            raise ValueError("A 32-character conversation ID is required")
        context[28] = conversation_id
        context[29] = (
            [str(conversation_time_ms // 1000), (conversation_time_ms % 1000) * 10**6]
            if conversation_time_ms is not None
            else []
        )
        context[30] = 0
    video_config = [
        0,
        12,
        None,
        shots[shot_mode],
        formats[resolution, orientation],
        None,
        None,
        None,
        seconds,
    ]
    config = [None] * 15 + [video_config]
    metadata = [1, None, [[None, "1", 1189]]] if enabled(flags, "flix-evge") else None
    return [video_feature(flags), None, context, [None, None, prompt], config, metadata, 1]


def video_results(response: Any) -> list[dict[str, Any]]:
    """Find uR field 26 -> ogh field 3 video resources, not arbitrary response URLs."""
    results = {}

    def visit(node):
        if not isinstance(node, list):
            return
        if len(node) > 25 and isinstance(node[25], list) and len(node[25]) > 2:
            clip = node[25][2]
            if (
                isinstance(clip, list)
                and len(clip) >= 8
                and isinstance(clip[4], str)
                and clip[4].startswith("https://")
                and type(clip[5]) is int
                and type(clip[6]) is int
            ):
                if not all(isinstance(clip[i], str) for i in (0, 2, 3)) or not isinstance(
                    clip[7], list
                ):
                    raise ValueError("Unexpected Vids video resource structure")
                key = tuple(clip[i] for i in (0, 2, 3))
                duration = clip[7]
                try:
                    seconds = int(duration[0]) if duration else 0
                    if len(duration) > 1 and duration[1] is not None:
                        seconds += int(duration[1]) / 1e9
                except (TypeError, ValueError):
                    raise ValueError("Unexpected Vids video duration") from None
                results[key] = {
                    "corpus": key[0],
                    "lookup_schema": key[1],
                    "unique_key": key[2],
                    "url": clip[4],
                    "width": clip[5],
                    "height": clip[6],
                    "duration": seconds,
                }
        for child in node:
            visit(child)

    visit(response)
    return list(results.values())


def image_results(response: Any) -> list[dict[str, Any]]:
    results = {}

    def visit(node):
        if not isinstance(node, list):
            return
        if len(node) > 11 and isinstance(node[11], list) and len(node[11]) > 6:
            image = node[11][6]
            if (
                isinstance(image, list)
                and len(image) > 3
                and isinstance(image[0], str)
                and image[0]
                and isinstance(image[1], str)
                and image[1].startswith("https://")
                and type(image[2]) is int
                and type(image[3]) is int
            ):
                results[image[0]] = {
                    "url": image[1],
                    "width": image[2],
                    "height": image[3],
                    "unique_key": image[0],
                    "corpus": image[4] if len(image) > 4 else None,
                    "lookup_schema": image[6] if len(image) > 6 else None,
                    "reference": node[11],
                }
        for child in node:
            visit(child)

    visit(response)
    return list(results.values())


def audio_results(response: Any) -> list[dict[str, str]]:
    results = {}

    def visit(node):
        if not isinstance(node, list):
            return
        if len(node) > 29 and isinstance(node[29], list) and node[29]:
            audio = node[29][0]
            if (
                isinstance(audio, list)
                and len(audio) > 3
                and isinstance(audio[3], str)
                and audio[3].startswith("https://")
            ):
                results[audio[3]] = {"url": audio[3], "mime_type": audio[1]}
        for child in node:
            visit(child)

    visit(response)
    return list(results.values())


def original_image_url(url: str, width: int, height: int) -> str:
    if (
        type(width) is not int
        or type(height) is not int
        or not 0 < width <= 8192
        or not 0 < height <= 8192
    ):
        raise ValueError("Invalid original image dimensions")
    parts = urlsplit(url)
    base, separator, options = parts.path.rpartition("=")
    if not separator:
        base, options = parts.path, ""
    retained = [x for x in options.split("-") if x and not re.fullmatch(r"[wh]\d+", x)]
    suffix = f"w{width}-h{height}" + ("-" + "-".join(retained) if retained else "")
    return urlunsplit((parts.scheme, parts.netloc, base + "=" + suffix, parts.query, ""))


def build_video_request(
    doc_id: str,
    flags: dict[str, Any],
    prompt: str,
    video: dict[str, Any],
    *,
    mode: str,
    seconds: int = 3,
    resolution: str = "720p",
    orientation: str = "landscape",
    **context,
) -> list[Any]:
    """Reuse generated resources; uploaded-video inputs require separate upload handling."""
    if mode not in {"edit", "extend", "upscale"}:
        raise ValueError("Unsupported video input mode")
    temporary_blob = video.get("temporary_blob")
    keys = [video.get(k) for k in ("corpus", "lookup_schema", "unique_key")]
    if temporary_blob is not None and (not isinstance(temporary_blob, str) or not temporary_blob):
        raise ValueError("A valid temporary video input is required")
    if temporary_blob is None and not all(isinstance(k, str) and k for k in keys):
        raise ValueError("A generated Vids resource identity is required")
    duration = video.get("duration")
    if type(duration) not in (int, float) or not 0 < duration <= (
        10.1 if mode == "upscale" else 60 if temporary_blob is not None else 20.1
    ):
        raise ValueError("A supported source duration is required")
    if mode == "upscale":
        resolution = "1080p"
        seconds = round(duration)
    request = build_text_request(
        doc_id,
        flags,
        prompt or "Upscale the video.",
        seconds=seconds,
        resolution=resolution,
        orientation=orientation,
        **context,
    )
    request[0] = {"edit": 377, "extend": 378, "upscale": 279}[mode]
    container = (
        [None, None, None, None, None, [None, None, None, temporary_blob, 1]]
        if temporary_blob is not None
        else [None, None, [keys[0], None, keys[1], keys[2]]]
    )
    node = [None] * 25 + [container]
    if mode == "upscale":
        request[3] = [None, None, None, [[node]]]
    else:
        name = "source_video"
        container.extend([None] * (7 - len(container)) + [name])
        named_resource = [None] * 7 + [[name, node]]
        request[2][25] = [[named_resource]]
        request[3] = (
            [None, None, None, [[[None, None, prompt]]]] if mode == "edit" else [None, None, prompt]
        )
        # Current frontend includes the selected source segment for edit/extension.
        config = request[4][15]
        source_ms = round(duration * 1000)

        def timestamp(milliseconds):
            whole, fraction = divmod(milliseconds, 1000)
            return [str(whole), fraction * 1000000] if fraction else [str(whole)]

        if mode == "edit":
            config[6], config[7] = (
                timestamp(source_ms - 10000) if duration > 10 else [],
                timestamp(source_ms),
            )
        elif duration > 10:
            config[6], config[7] = timestamp(source_ms - 10000), timestamp(source_ms)
    return request


def build_image_request(
    request: list[Any], prompt: str, identifiers: list[str], *, mode: str
) -> list[Any]:
    if mode not in {"animate", "ingredients"}:
        raise ValueError("Unsupported image input mode")
    maximum = 1 if mode == "animate" else 7
    if not 1 <= len(identifiers) <= maximum or not all(
        isinstance(x, str) and x for x in identifiers
    ):
        raise ValueError("Invalid number of temporary image inputs")
    request[0] = 375 if mode == "animate" else 376
    references = []
    for index, identifier in enumerate(identifiers):
        name = f"image_{index + 1}"
        image = [None] * 8 + [[None, None, None, identifier, 1]]
        image.extend([None] * 4 + [name])
        node = [None] * 11 + [image]
        named = [None] * 7 + [[name, node]]
        references.append([named])
    request[2][25] = references
    request[3] = (
        [None, None, prompt] if mode == "animate" else [None, None, None, [[[None, None, prompt]]]]
    )
    return request


class VidsProtocolError(RuntimeError):
    """Keep raw upstream bodies and credential-bearing URLs out of exceptions."""

    def __init__(self, operation: str, status: int, *, upstream_status=None, upstream_reason=None):
        self.operation, self.status = operation, status
        self.upstream_status = upstream_status
        self.upstream_reason = upstream_reason
        super().__init__(f"Vids {operation}: HTTP {status}")


def protocol_error(operation, response):
    """Keep only standard RPC status and ErrorInfo reason, never raw message or metadata."""
    upstream_status = upstream_reason = None
    try:
        content = getattr(response, "content", None)
        if isinstance(content, bytes) and len(content) > 64 * 1024:
            raise ValueError
        body = response.json()
        if isinstance(body, list) and len(body) >= 3:
            rpc_statuses = {
                1: "CANCELLED",
                2: "UNKNOWN",
                3: "INVALID_ARGUMENT",
                4: "DEADLINE_EXCEEDED",
                5: "NOT_FOUND",
                6: "ALREADY_EXISTS",
                7: "PERMISSION_DENIED",
                8: "RESOURCE_EXHAUSTED",
                9: "FAILED_PRECONDITION",
                10: "ABORTED",
                11: "OUT_OF_RANGE",
                12: "UNIMPLEMENTED",
                13: "INTERNAL",
                14: "UNAVAILABLE",
                15: "DATA_LOSS",
                16: "UNAUTHENTICATED",
            }
            if type(body[0]) is int:
                upstream_status = rpc_statuses.get(body[0])
            if isinstance(body[2], list):
                for detail in body[2][:10]:
                    if (
                        not isinstance(detail, list)
                        or len(detail) < 2
                        or detail[0] != "type.googleapis.com/google.rpc.ErrorInfo"
                    ):
                        continue
                    info = detail[1]
                    reason = info[0] if isinstance(info, list) and info else None
                    if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason):
                        upstream_reason = reason
                        break
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            status = error.get("status")
            if status in {
                "INVALID_ARGUMENT",
                "FAILED_PRECONDITION",
                "OUT_OF_RANGE",
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "RESOURCE_EXHAUSTED",
                "NOT_FOUND",
                "ALREADY_EXISTS",
                "ABORTED",
                "CANCELLED",
                "INTERNAL",
                "UNAVAILABLE",
                "DEADLINE_EXCEEDED",
                "DATA_LOSS",
                "UNIMPLEMENTED",
                "UNKNOWN",
            }:
                upstream_status = status
            details = error.get("details")
            if isinstance(details, list):
                for detail in details[:10]:
                    if (
                        not isinstance(detail, dict)
                        or detail.get("@type") != "type.googleapis.com/google.rpc.ErrorInfo"
                    ):
                        continue
                    reason = detail.get("reason")
                    if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason):
                        upstream_reason = reason
                        break
    except (ValueError, AttributeError, TypeError, RecursionError):
        pass
    return VidsProtocolError(
        operation,
        response.status_code,
        upstream_status=upstream_status,
        upstream_reason=upstream_reason,
    )


class VidsTransportError(RuntimeError):
    """Suppress transport exception URLs, API keys and proxy credentials."""


class DocumentImageSourceError(ValueError):
    """A requested saved image is unavailable before generation submission."""


class VidsClient:
    def __init__(
        self,
        editor_url: str,
        cookies: list[dict[str, Any]],
        *,
        auth_user: int = 0,
        proxy: str | None = None,
        timeout: int = 30,
    ):
        parsed = urlsplit(editor_url)
        match = re.fullmatch(r"/videos/(?:u/\d+/)?d/([\w-]+)/edit", parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "docs.google.com"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or not match
        ):
            raise ValueError("An existing Google Vids editor URL is required")
        if type(auth_user) is not int or auth_user < 0:
            raise ValueError("auth_user must be a non-negative integer")
        self.doc_id = match[1]
        self.editor_url = f"{DOCS_ORIGIN}{parsed.path}?authuser={auth_user}"
        self.auth_user = auth_user
        self.proxy = proxy
        self.timeout = timeout
        self.flags: dict[str, Any] = {}
        self.service_origin = ""
        self.settings_response = None
        self.model_chunk: dict[str, Any] | None = None
        self.document_session_id: str | None = None
        self.document_save_config: dict[str, str] | None = None
        self.on_cookies_changed = None
        self.before_generate = None
        self.initialization_valid = False
        self.session_id = f"goog_{secrets.randbelow(2**31)}"
        self.session = AsyncSession(
            cookies=cookie_jar(cookies),
            proxy=proxy,
            timeout=timeout,
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def _send(self, method, *args, checkpoint=True, **kwargs):
        try:
            return await getattr(self.session, method)(*args, **kwargs)
        finally:
            # Never hide an accepted generation response behind a disk failure.
            # The gateway records persistence health and blocks new paid requests.
            if checkpoint and self.on_cookies_changed is not None:
                self.on_cookies_changed()

    async def bootstrap(self) -> None:
        self.initialization_valid = False
        self.model_chunk = None
        self.document_session_id = None
        self.document_save_config = None
        try:
            response = await self._send(
                "get", self.editor_url, allow_redirects=False, checkpoint=False
            )
        except (RequestException, OSError):
            raise VidsTransportError("Vids editor request failed") from None
        if response.status_code != 200:
            raise VidsProtocolError("editor", response.status_code)
        flags = parse_flags(response.text)
        model_chunk = (
            parse_model_chunk(response.text) if MODEL_CHUNK_PATTERN.search(response.text) else None
        )
        document_session_id = (
            parse_document_session(response.text, model_chunk["revision"])
            if model_chunk is not None and DOCUMENT_SESSION_PATTERN.search(response.text)
            else None
        )
        domain = flags.get("docs-gaopd", "")
        if isinstance(domain, str) and "://" not in domain:
            domain = "https://" + domain
        target = urlsplit(domain)
        if (
            target.scheme != "https"
            or target.hostname != "appsgenaiserver-pa.clients6.google.com"
            or target.port not in (None, 443)
            or target.username
            or target.password
            or target.path not in ("", "/")
            or target.query
            or target.fragment
        ):
            raise ValueError("Unexpected Vids generation service; review its protocol before use")
        if not all(
            isinstance(flags.get(k), str) and flags[k] for k in ("espresso_api_key", "docs-est")
        ):
            raise ValueError("Vids service credentials absent from HTTP initialization")
        self.flags = flags
        self.model_chunk = model_chunk
        self.document_session_id = document_session_id
        # Reading/generation remains compatible with editors lacking a save config.
        from .document import parse_save_config

        try:
            self.document_save_config = parse_save_config(response.text, self.doc_id)
        except ValueError:
            self.document_save_config = None
        self.service_origin = "https://" + target.hostname
        self.settings_response = None
        self.initialization_valid = True
        # Validate the initialized account before persisting cookies from this GET.
        if self.on_cookies_changed is not None:
            self.on_cookies_changed()

    def _headers(self) -> dict[str, str]:
        if not self.flags:
            raise ValueError("Run HTTP bootstrap first")
        target = Request(self.editor_url)
        self.session.cookies.jar.add_cookie_header(target)
        values = {}
        for part in target.get_header("Cookie", "").split(";"):
            if "=" in part:
                name, value = part.strip().split("=", 1)
                values.setdefault(name, value)
        signing_cookie = values.get("SAPISID") or values.get("__Secure-3PAPISID")
        if not signing_cookie:
            raise ValueError("Google signing cookie missing; refresh the session")
        return {
            "Authorization": sapisid_hash(signing_cookie, int(time.time())),
            "Origin": self.service_origin,
            "Referer": self.service_origin + "/",
            "X-Origin": DOCS_ORIGIN,
            "X-Goog-AuthUser": str(self.auth_user),
            "X-Server-Token": self.flags["docs-est"],
            "Content-Type": CONTENT_TYPE,
            "Accept": "*/*",
        }

    async def _request(
        self, method: str, path: str, body: list[Any] | None = None, *, refresh_read: bool = True
    ) -> Any:
        # Settings/uploads can rotate credentials or fail storage before submission.
        if path == "/v1/genai/generate":
            admission = BEFORE_PAID_REQUEST.get()
            if admission is not None:
                await admission()
            if self.before_generate is not None:
                self.before_generate()
        # No submission retries: a transport failure can follow an accepted paid task.
        headers = self._headers()
        try:
            response = await self._send(
                "request",
                method,
                self.service_origin + path,
                params={"key": self.flags["espresso_api_key"]},
                headers=headers,
                data=json.dumps(body, separators=(",", ":")) if body is not None else None,
                allow_redirects=False,
                timeout=300 if path == "/v1/genai/generate" else self.timeout,
            )
        except (RequestException, OSError):
            raise VidsTransportError(
                "Vids request failed; acceptance unknown, not retried"
            ) from None
        read_only = path in {"/v1/genai/quotaSummary", "/v1/genai/getSettings"} or (
            method == "GET" and path.startswith("/v1/operations/")
        )
        if response.status_code == 401 and refresh_read and read_only:
            await self.bootstrap()
            return await self._request(method, path, body, refresh_read=False)
        if response.status_code != 200:
            raise protocol_error(path.rsplit("/", 1)[-1], response)
        try:
            return response.json()
        except ValueError:
            raise ValueError("Vids returned non-JSON protocol data") from None

    async def quota(self, feature: int | None = None) -> Any:
        body = [None, 1, [9]]
        if feature is not None:
            body.append(feature)
        return await self._request("POST", "/v1/genai/quotaSummary", body)

    async def settings(self) -> Any:
        if self.settings_response is None:
            self.settings_response = await self._request(
                "POST", "/v1/genai/getSettings", [[9, 9], 9]
            )
        return self.settings_response

    async def upload_temporary(self, path: str | Path) -> str:
        """Upload a temporary image/video blob without editing document content."""
        source = Path(path)
        if not source.is_file() or not 0 < source.stat().st_size <= 50 * 1024**2:
            raise ValueError("Temporary input must be a non-empty file of at most 50 MiB")
        if not enabled(self.flags, "docs-tbue"):
            raise ValueError("Temporary blob upload is unavailable")
        route = self.flags.get("docs-tbuu")
        if route != "/upload/temporaryblob/videos":
            raise ValueError("Unexpected temporary upload route")
        try:
            start = await self._send(
                "post",
                DOCS_ORIGIN + route,
                params={"authuser": self.auth_user},
                headers={
                    "X-Goog-Upload-Protocol": "resumable",
                    "X-Goog-Upload-Command": "start",
                    "X-Goog-Upload-Content-Length": str(source.stat().st_size),
                    "X-Goog-Upload-File-Name": "temporary_blob",
                    "Origin": DOCS_ORIGIN,
                    "Referer": self.editor_url,
                },
                data=b"",
                allow_redirects=False,
            )
        except (RequestException, OSError):
            raise VidsTransportError("Vids temporary upload start failed") from None
        if start.status_code != 200:
            raise VidsProtocolError("upload-start", start.status_code)
        destination = start.headers.get("X-Goog-Upload-URL", "")
        target = urlsplit(destination)
        if (
            target.scheme != "https"
            or target.hostname != "docs.google.com"
            or target.username
            or target.password
            or target.port not in (None, 443)
        ):
            raise ValueError("Unexpected temporary upload destination")
        try:
            finalized = await self._send(
                "put",
                destination,
                headers={
                    "X-Goog-Upload-Command": "upload, finalize",
                    "X-Goog-Upload-Offset": "0",
                    "Content-Type": "application/octet-stream",
                    "Origin": DOCS_ORIGIN,
                    "Referer": self.editor_url,
                },
                data=source.read_bytes(),
                allow_redirects=False,
                timeout=120,
            )
        except (RequestException, OSError):
            raise VidsTransportError("Vids temporary upload completion unknown") from None
        if finalized.status_code != 200:
            raise VidsProtocolError("upload-finalize", finalized.status_code)
        try:
            identifier = finalized.json()[0]
        except (ValueError, TypeError, IndexError):
            raise ValueError("Unexpected temporary upload response") from None
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Temporary media identifier absent")
        return identifier

    async def upload_image(
        self, path: str | Path, *, mime_type: str, with_metadata: bool = False
    ) -> str | dict[str, Any]:
        """Upload document-associated media and return its Cosmo ID, without scene edits."""
        source = Path(path)
        if (
            mime_type not in {"image/png", "image/jpeg", "image/webp"}
            or not source.is_file()
            or not 0 < source.stat().st_size <= 10 * 1024**2
        ):
            raise ValueError("A supported image of at most 10 MiB is required")
        route = self.flags.get("scotty_upload_url")
        if route != "/upload/videos/resumable":
            raise ValueError("Unexpected image upload route")
        info = self.flags.get("info_params")
        if not isinstance(info, dict) or not all(
            isinstance(info.get(k), str) and info[k] for k in ("token", "ouid")
        ):
            raise ValueError("Image upload initialization absent")
        metadata = {
            "uploadMediaType": "photo",
            "mimeType": mime_type,
            "containingCosmoId": self.doc_id,
        }
        fields = [
            {"external": {"name": "file", "filename": "image", "size": source.stat().st_size}}
        ]
        fields.extend(
            {"inlined": {"name": name, "content": value, "contentType": "text/plain"}}
            for name, value in metadata.items()
        )
        boundary = "scotty-" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()
            + json.dumps(
                {"protocolVersion": "0.8", "createSessionRequest": {"fields": fields}}
            ).encode()
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="Filedata"; filename="image"\r\nContent-Type: {mime_type}\r\nContent-Transfer-Encoding: binary\r\n\r\n'.encode()
            + source.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        params = {k: info[k] for k in ("token", "ouid")}
        params.update(authuser=self.auth_user, upload_protocol="multipart")
        try:
            response = await self._send(
                "post",
                DOCS_ORIGIN + route,
                params=params,
                data=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary=" + boundary,
                    "Origin": DOCS_ORIGIN,
                    "Referer": self.editor_url,
                },
                allow_redirects=False,
                timeout=120,
            )
        except (RequestException, OSError):
            raise VidsTransportError("Vids image upload completion unknown") from None
        if response.status_code != 200:
            raise VidsProtocolError("image-upload", response.status_code)
        try:
            data = response.json()
            completion = data["sessionStatus"]["additionalInfo"][
                "uploader_service.GoogleRupioAdditionalInfo"
            ]["completionInfo"]
            identifier = completion["customerSpecificInfo"]["id"]
            if (
                completion["status"] != "SUCCESS"
                or not isinstance(identifier, str)
                or not identifier
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ValueError("Unexpected image upload response") from None
        if with_metadata:
            try:
                details = completion["customerSpecificInfo"]
                width, height = int(details["width"]), int(details["height"])
                if not 0 < width <= 32768 or not 0 < height <= 32768:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise ValueError("Uploaded image dimensions are unavailable") from None
            return {"cosmo_id": identifier, "width": width, "height": height}
        return identifier

    async def insert_document_image(
        self, path: str | Path, *, mime_type: str, shape_id: str, scene_id: str, checkpoint=None
    ) -> dict[str, Any]:
        """Upload and save a native image once; ambiguous saves are never retried."""
        from .document import image_insert_commands, saved_revision

        await self.bootstrap()
        if self.before_generate is not None:
            self.before_generate()
        if self.model_chunk is None or self.document_save_config is None:
            raise DocumentImageSourceError("Document insertion context is unavailable")
        # Validate the target before uploading any media.
        try:
            image_insert_commands(
                self.model_chunk,
                shape_id,
                scene_id,
                {"width": 1, "height": 1, "cosmo_id": "preflight"},
                "preflight",
            )
        except ValueError:
            raise DocumentImageSourceError("A supported saved scene is required") from None
        info = self.flags.get("info_params")
        if not isinstance(info, dict) or not all(
            isinstance(info.get(k), str) and info[k] for k in ("token", "ouid")
        ):
            raise DocumentImageSourceError("Document save initialization is unavailable")
        image = await self.upload_image(path, mime_type=mime_type, with_metadata=True)
        url = await self.document_image_url(image["cosmo_id"])
        commands = image_insert_commands(self.model_chunk, shape_id, scene_id, image, url)
        revision = self.model_chunk["revision"]
        if checkpoint is not None:
            checkpoint(
                {"image": image, "shape_id": shape_id, "scene_id": scene_id, "revision": revision}
            )
        if self.before_generate is not None:
            self.before_generate()
        try:
            response = await self._send(
                "post",
                DOCS_ORIGIN + self.document_save_config["path"],
                params={**info, "id": self.doc_id, "authuser": self.auth_user},
                data={
                    "rev": str(revision),
                    "bundles": json.dumps(
                        [
                            {
                                "commands": commands,
                                "sid": self.document_save_config["sid"],
                                "reqId": 0,
                            }
                        ],
                        separators=(",", ":"),
                    ),
                },
                headers={"Origin": DOCS_ORIGIN, "Referer": self.editor_url},
                allow_redirects=False,
            )
        except (RequestException, OSError):
            raise VidsTransportError("Document save acceptance is unknown") from None
        if response.status_code != 200:
            raise VidsProtocolError("document-save", response.status_code)
        content = response.text.lstrip()
        if content.startswith(")]}'"):
            content = content[4:].lstrip()
        acknowledged = saved_revision(json.loads(content), revision)
        if checkpoint is not None:
            checkpoint(
                {
                    "image": image,
                    "shape_id": shape_id,
                    "scene_id": scene_id,
                    "revision": revision,
                    "acknowledged_revision": acknowledged,
                }
            )
        await self.bootstrap()
        if self.before_generate is not None:
            self.before_generate()
        expected = {"shape_id": shape_id, "cosmo_id": image["cosmo_id"], "scene_id": scene_id}
        if (
            self.model_chunk is None
            or self.model_chunk["revision"] < acknowledged
            or expected not in document_image_shapes(self.model_chunk)
        ):
            raise VidsTransportError("Saved image cannot be confirmed in the current snapshot")
        return {
            "shape_id": shape_id,
            "scene_id": scene_id,
            "revision": self.model_chunk["revision"],
        }

    async def document_image_url(self, cosmo_id: str) -> str:
        """Resolve existing document media through Docs; does not generate or edit."""
        if (
            not isinstance(cosmo_id, str)
            or not cosmo_id
            or len(cosmo_id) > 4096
            or any(char.isspace() or ord(char) < 32 for char in cosmo_id)
        ):
            raise ValueError("A valid document image Cosmo ID is required")
        info = self.flags.get("info_params")
        if not isinstance(info, dict) or not all(
            isinstance(info.get(key), str) and info[key] for key in ("token", "ouid")
        ):
            raise ValueError("Document image initialization absent")
        params = dict(info)
        params.update(id=self.doc_id, authuser=self.auth_user)
        try:
            response = await self._send(
                "post",
                DOCS_ORIGIN + "/videos/renderdata",
                params=params,
                data={"renderOps": json.dumps({"r0": ["image", {"cosmoId": cosmo_id}]})},
                headers={"Origin": DOCS_ORIGIN, "Referer": self.editor_url},
                allow_redirects=False,
            )
        except (RequestException, OSError):
            raise VidsTransportError("Vids document image lookup failed") from None
        if response.status_code != 200:
            raise VidsProtocolError("document-image", response.status_code)
        content = response.text.lstrip()
        if content.startswith(")]}'"):
            content = content[4:].lstrip()
        try:
            data = json.loads(content)
            url = data["r0"]
            if not isinstance(url, str):
                raise ValueError
            target = urlsplit(url)
            if (
                target.scheme != "https"
                or target.hostname != "docs.google.com"
                or target.username
                or target.password
                or target.port not in (None, 443)
                or not DOCUMENT_IMAGE_PATH_PATTERN.fullmatch(target.path)
                or target.fragment
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ValueError("Unexpected Vids document image response") from None
        return url

    def text_request(self, prompt: str, **options) -> list[Any]:
        options.setdefault("session_id", self.session_id)
        if self.settings_response is not None:
            try:
                conversation_enabled = self.settings_response[1][0] in (True, 1)
            except (IndexError, TypeError):
                raise ValueError("Unexpected Vids settings structure") from None
            if conversation_enabled:
                options.setdefault("conversation_id", uuid.uuid4().hex)
        return build_text_request(self.doc_id, self.flags, prompt, **options)

    async def submit_text(self, prompt: str, **options) -> Any:
        """Consumes generation quota; call only under an explicit generation budget."""
        await self.settings()
        return await self._request(
            "POST", "/v1/genai/generate", self.text_request(prompt, **options)
        )

    async def submit_video(
        self,
        video: dict[str, Any],
        prompt: str = "",
        *,
        mode: str,
        identifiers: list[str] | None = None,
        **options,
    ) -> Any:
        if identifiers is not None and mode != "edit":
            raise ValueError("Reference images require video edit mode")
        await self.settings()
        base = self.text_request(prompt or "Upscale the video.", **options)
        merged = dict(options)
        merged.setdefault("session_id", base[2][4])
        merged.setdefault("request_id", base[2][6])
        if base[2][28]:
            merged.setdefault("conversation_id", base[2][28])
        request = build_video_request(self.doc_id, self.flags, prompt, video, mode=mode, **merged)
        if identifiers is not None:
            references = build_image_request(
                self.text_request(prompt, **options), prompt, identifiers, mode="ingredients"
            )[2][25]
            request[2][25].extend(references)
        return await self._request("POST", "/v1/genai/generate", request)

    async def submit_images(
        self, identifiers: list[str], prompt: str, *, mode: str = "animate", **options
    ) -> Any:
        await self.settings()
        request = build_image_request(
            self.text_request(prompt, **options), prompt, identifiers, mode=mode
        )
        request[0] = image_video_feature(self.flags, mode)
        return await self._request("POST", "/v1/genai/generate", request)

    async def generate_image(
        self, prompt: str, *, count: int = 1, aspect: str = "square", style: str = "auto"
    ) -> Any:
        styles = {
            "auto": 0,
            "photography": 1,
            "background": 3,
            "vector": 8,
            "sketch": 2,
            "watercolor": 7,
            "cyberpunk": 10,
        }
        if (
            type(count) is not int
            or not 1 <= count <= 4
            or aspect not in {"square", "landscape", "portrait"}
            or style not in styles
        ):
            raise ValueError("Invalid image count or aspect ratio")
        await self.settings()
        request = self.text_request(prompt)
        request[0] = 205
        request[2][11] = [4, 0]
        request[4] = [
            [styles[style], count, None, {"square": 1, "landscape": 2, "portrait": 5}[aspect]]
        ]
        request[5] = [1]
        return await self._request("POST", "/v1/genai/generate", request)

    async def generate_speech(self, text: str, *, voice: str = "Puck") -> Any:
        if voice not in PRESET_VOICES:
            raise ValueError("Unsupported preset voice")
        await self.settings()
        request = self.text_request(text)
        request[0], request[4] = 254, [None] * 25 + [[voice]]
        request[2][11] = [16, 0]
        request[5] = [1] if enabled(self.flags, "docs-egqe") else None
        return await self._request("POST", "/v1/genai/generate", request)

    async def edit_image(self, image: dict[str, Any], prompt: str) -> Any:
        """Use an existing generated resource; its key is not a document Cosmo ID."""
        cosmo_id = image.get("cosmo_id")
        reference = (
            [None, None, cosmo_id]
            if isinstance(cosmo_id, str) and cosmo_id
            else image.get("reference")
        )
        if not isinstance(reference, list) or len(reference) < 3:
            raise ValueError("A generated Vids image reference is required")
        await self.settings()
        request = self.text_request(prompt)
        request[0] = 234 if enabled(self.flags, "docs-eaiecsf") else 205
        request[2][11] = [4, 0]
        request[3] = [None, None, prompt] + [None] * 8 + [reference]
        request[4], request[5] = [[0, 4, 0]], [1]
        return await self._request("POST", "/v1/genai/generate", request)

    async def edit_document_image(self, shape_id: str, prompt: str) -> Any:
        """Edit a saved image shape using a freshly read HTTP document snapshot."""
        from .image_edit import build_document_image_edit_request

        await self.bootstrap()
        await self.settings()
        if self.model_chunk is None or self.document_session_id is None:
            raise DocumentImageSourceError("Saved document image context is unavailable")
        try:
            shapes = document_image_shapes(self.model_chunk)
        except ValueError:
            raise DocumentImageSourceError(
                "A supported saved document image snapshot is required"
            ) from None
        shape = next((shape for shape in shapes if shape["shape_id"] == shape_id), None)
        if shape is None:
            raise DocumentImageSourceError("A saved image shape in this document is required")
        try:
            await self.document_image_url(shape["cosmo_id"])
        except (VidsProtocolError, VidsTransportError, ValueError):
            raise DocumentImageSourceError("Saved document image cannot be verified") from None
        request = build_document_image_edit_request(
            self.flags.get("docs-ci", self.doc_id),
            self.flags,
            shape,
            self.model_chunk["revision"],
            self.document_session_id,
            prompt,
            session_id=self.session_id,
        )
        return await self._request("POST", "/v1/genai/generate", request)

    async def generate_music(self, prompt: str, *, instrumental: bool = True) -> Any:
        await self.settings()
        if instrumental:
            prompt = "Instrumental, no lyrics or vocals. " + prompt
        request = self.text_request(prompt)
        request[0], request[4] = 242, None
        request[2][11] = [77, 0]
        request[5] = [1, None, [[None, "1", 1251]]] if enabled(self.flags, "flix-emgqr") else None
        return await self._request("POST", "/v1/genai/generate", request)

    async def download_image(self, image: dict[str, Any], path: str | Path) -> Path:
        url = original_image_url(image["url"], image["width"], image["height"])
        return await self._download_other(url, path, "image/")

    async def download_audio(self, audio: dict[str, Any], path: str | Path) -> Path:
        return await self._download_other(audio["url"], path, "audio/")

    async def _download_other(self, url: str, path: str | Path, mime_prefix: str) -> Path:
        for _ in range(5):
            try:
                target = urlsplit(url)
                host = target.hostname or ""
                port = target.port
            except ValueError:
                raise ValueError("Unexpected Vids media host") from None
            if (
                target.scheme != "https"
                or target.username
                or target.password
                or port not in (None, 443)
                or not (
                    host.endswith(".googleusercontent.com")
                    or (
                        host == "docs.google.com"
                        and DOCUMENT_IMAGE_PATH_PATTERN.fullmatch(target.path)
                        and mime_prefix == "image/"
                    )
                    or host
                    in {
                        "contribution-rt.usercontent.google.com",
                        "work.fife.usercontent.google.com",
                    }
                )
            ):
                raise ValueError("Unexpected Vids media host")
            try:
                response = await self._send(
                    "get",
                    url,
                    allow_redirects=False,
                    headers={"Referer": DOCS_ORIGIN + "/"},
                    timeout=120,
                )
            except (RequestException, OSError):
                raise VidsTransportError("Vids media download failed") from None
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                raise VidsProtocolError("download", response.status_code)
            if not response.headers.get("Content-Type", "").startswith(mime_prefix):
                raise ValueError("Unexpected Vids media content type")
            output = Path(path)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".part")
            temporary.write_bytes(response.content)
            temporary.replace(output)
            return output
        raise ValueError("Vids media redirect limit exceeded")

    async def operation(self, name: str) -> Any:
        if not isinstance(name, str) or not name or len(name) > 4096:
            raise ValueError("A valid operation name is required")
        return await self._request("GET", "/v1/operations/" + quote(name, safe=""))

    async def download_video(self, video: dict[str, Any], path: str | Path) -> Path:
        target = urlsplit(video.get("url", ""))
        host = target.hostname or ""
        if (
            target.scheme != "https"
            or target.username
            or target.password
            or target.port not in (None, 443)
            or not (
                host == "contribution-rt.usercontent.google.com"
                or host.endswith(".googleusercontent.com")
            )
        ):
            raise ValueError("Unexpected Vids media URL")
        # Google scopes the existing jar to the validated media host. Never attach
        # generation Authorization, API key or X-Server-Token to the media request.
        try:
            response = await self._send(
                "get",
                video["url"],
                allow_redirects=False,
                headers={"Referer": DOCS_ORIGIN + "/"},
                timeout=120,
            )
        except (RequestException, OSError):
            raise VidsTransportError("Vids media download failed") from None
        if response.status_code != 200:
            raise VidsProtocolError("download", response.status_code)
        if len(response.content) < 12 or response.content[4:8] != b"ftyp":
            raise ValueError("Vids media response is not an MP4")
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        # ponytail: callers serialize writes to the same destination; use unique
        # temporary files if concurrent downloads to one path become necessary.
        temporary = output.with_suffix(output.suffix + ".part")
        temporary.write_bytes(response.content)
        temporary.replace(output)
        return output
