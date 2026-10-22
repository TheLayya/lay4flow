"""Upload a video through Flow's current Cookie-authenticated Scotty endpoint."""

import json
import re
import uuid
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

from ..flow.flow_rpc import FLOW_ORIGIN, FlowRPCError, _bootstrap, _check_response
from ..flow.video_edit import validate_video_trim


def _upload_url(value: Any, base: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise FlowRPCError("account_probe_error", "flow_upload_start")
    address = urljoin(base, value)
    try:
        parsed = urlsplit(address)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "flow.google.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.path.startswith("/upload/")
            and "\\" not in address
            and not any(part in {".", ".."} for part in unquote(parsed.path).split("/"))
            and not any(char.isspace() or ord(char) < 32 for char in address)
        )
    except ValueError:
        valid = False
    if not valid:
        raise FlowRPCError("account_probe_error", "flow_upload_start")
    return address


def _media_name(payload: Any) -> str:
    name = None
    if isinstance(payload, dict):
        media = payload.get("media")
        if isinstance(media, dict):
            name = media.get("name")
        if not name:
            name = payload.get("mediaId") or payload.get("media_id")
    elif isinstance(payload, list):
        # UploadVideo protobuf: field3 bz media; a bare bz response is also recognized.
        media = payload[2] if len(payload) > 2 else None
        if isinstance(media, list) and media and isinstance(media[0], str):
            name = media[0]
        elif payload and isinstance(payload[0], str):
            name = payload[0]
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 1024
        or any(char.isspace() or ord(char) < 32 for char in name)
    ):
        raise FlowRPCError("account_probe_error", "flow_upload_finalize")
    if "://" in name or "?" in name or "#" in name:
        raise FlowRPCError("account_probe_error", "flow_upload_finalize")
    return name.strip()


async def upload_profile_video(
    context: Any,
    project_id: str,
    video_bytes: bytes,
    mime_type: str,
    trim_start_ms: int,
    trim_end_ms: int,
) -> str:
    """Start and finalize once in the existing account Cookie/fixed proxy context."""
    validate_video_trim(trim_start_ms, trim_end_ms)
    project = str(project_id or "").strip().removeprefix("projects/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", project):
        raise ValueError("project_id is invalid")
    if not isinstance(video_bytes, bytes) or not video_bytes:
        raise ValueError("video_bytes must be non-empty bytes")
    extensions = {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}
    if mime_type not in extensions:
        raise ValueError("video mime type is unsupported")
    source = f"{FLOW_ORIGIN}/project/{project}"
    step = "flow_upload_bootstrap"
    try:
        response = await context.request.get(source, timeout=20000, max_redirects=0)
        _check_response(response, step)
        _bootstrap(await response.text())
        # Official Pbb obtains _.mo.gq(), whose KC implementation reads XSRF-TOKEN.
        # SNlM0e is the batchexecute token; it is not substituted for this cookie.
        cookies = await context.cookies(source)
        xsrf = next(
            (
                unquote(row["value"]).strip('"')
                for row in cookies
                if row.get("name") == "XSRF-TOKEN"
                and isinstance(row.get("value"), str)
                and row["value"]
                and not row.get("httpOnly", False)
            ),
            None,
        )
        if xsrf and (len(xsrf) > 4096 or any(ord(char) < 32 for char in xsrf)):
            raise FlowRPCError("account_probe_error", step)
        headers = {
            "Origin": FLOW_ORIGIN,
            "Referer": source,
            "Slug": quote(f"lay4flow-upload-{uuid.uuid4().hex}.{extensions[mime_type]}"),
            "x-trim-start": str(trim_start_ms),
            "x-trim-end": str(trim_end_ms),
        }
        if xsrf:
            headers["X-Framework-Xsrf-Token"] = xsrf
        url = f"{FLOW_ORIGIN}/upload/v1/flow/upload/video/{quote(project, safe='')}"
        step = "flow_upload_start"
        start = await context.request.post(
            url,
            headers={
                **headers,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(len(video_bytes)),
                "X-Goog-Upload-Header-Content-Type": mime_type,
            },
            data=b"",
            timeout=60000,
            max_redirects=0,
        )
        _check_response(start, step)
        upload = next(
            (value for key, value in start.headers.items() if key.lower() == "x-goog-upload-url"),
            None,
        )
        target = _upload_url(upload, url)
        step = "flow_upload_finalize"
        finalized = await context.request.post(
            target,
            headers={
                **headers,
                "Content-Type": mime_type,
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
            },
            data=video_bytes,
            timeout=180000,
            max_redirects=0,
        )
        _check_response(finalized, step)
        text = await finalized.text()
        if text.lstrip().startswith(")]}'"):
            text = text.lstrip()[4:].lstrip()
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise FlowRPCError("account_probe_error", step) from None
        return _media_name(payload)
    except FlowRPCError:
        raise
    except Exception:
        # Never retry unknown start/finalize outcomes or expose upload URLs/Cookies.
        raise FlowRPCError("transient_error", step) from None
