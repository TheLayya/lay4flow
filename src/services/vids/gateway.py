"""Single-account development gateway: cached HTTP session and durable request receipts."""

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from .client import (
    BEFORE_PAID_REQUEST,
    PRESET_VOICES,
    DocumentImageSourceError,
    VidsClient,
    VidsProtocolError,
    VidsTransportError,
    audio_results,
    cookie_rows,
    document_image_shapes,
    image_results,
    video_feature,
    video_results,
)

KINDS = {
    "video": "vids-video",
    "image": "vids-image",
    "music": "vids-music",
    "speech": "vids-speech",
}
VERIFIED_VIDEO_SPECIFICATIONS = {
    "text": ((3, "1280x720"), (4, "1280x720"), (4, "720x1280")),
    "animate": ((3, "1280x720"),),
    "ingredients": ((3, "1280x720"),),
    "edit": ((3, "1280x720"), (10, "1280x720")),
    "extend": ((3, "1280x720"),),
    "upscale": ((3, "1920x1080"),),
}
VIDEO_SIZES = {
    "1280x720": ("720p", "landscape"),
    "720x1280": ("720p", "portrait"),
    "1920x1080": ("1080p", "landscape"),
    "1080x1920": ("1080p", "portrait"),
}
IMAGE_ASPECTS = {"1:1": "square", "16:9": "landscape", "9:16": "portrait"}
IMAGE_STYLES = ("auto", "photography", "background", "vector", "sketch", "watercolor", "cyberpunk")
VIDEO_SHOT_MODES = ("auto", "long_take", "montage")
SUPPORTED_VIDEO_SPECIFICATIONS = {
    mode: tuple((seconds, size) for seconds in range(3, 11) for size in VIDEO_SIZES)
    for mode in ("text", "animate", "ingredients", "edit", "extend")
}
SUPPORTED_VIDEO_SPECIFICATIONS["upscale"] = tuple(
    (seconds, size) for seconds in range(3, 11) for size in ("1920x1080", "1080x1920")
)


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code = status, code
        super().__init__(message)


def image_input(encoded):
    """Validate a single original image before claiming or uploading a request."""
    try:
        if not isinstance(encoded, str) or len(encoded) > 14 * 1024**2:
            raise ValueError
        image = base64.b64decode(encoded, validate=True)
        if not 0 < len(image) <= 10 * 1024**2:
            raise ValueError
        if image.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif image.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif image[:4] == b"RIFF" and image[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError
    except ValueError:
        raise GatewayError(
            400, "invalid_image", "A PNG, JPEG or WebP up to 10 MiB is required"
        ) from None
    return image, mime


class VidsGateway:
    """Keep one HTTP CookieJar, including server rotations, until a validated replacement.

    ponytail: one account/one active request; use shared account dispatch when integrating
    with Flow's production runtime. Only explicit credential refresh may open a Profile.
    """

    def __init__(
        self,
        client: VidsClient | None,
        root: str | Path,
        *,
        cookie_source=None,
        session_store=None,
        resume=False,
        proxy=None,
        refresh_session=False,
        expected_account_hash=None,
        expected_editor_url=None,
    ):
        self.client = client
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.identity = None
        self._lock_file = None
        self.cookie_source = cookie_source
        self.session_store = session_store
        self.resume = resume
        self.proxy = proxy
        self.refresh_session = refresh_session
        self.expected_account_hash = expected_account_hash
        self.expected_editor_url = expected_editor_url
        self.persistence_failed = False
        self._last_cookies = None
        self._ready = False
        self._closed = False
        self._failed_start = False

    def _persist_session(self, client):
        if self.session_store is None:
            return
        rows = cookie_rows(client.session.cookies.jar)
        if (
            not self.identity
            or client.flags.get("docs-hue") != self.identity
            or getattr(client, "initialization_valid", True) is False
        ):
            raise GatewayError(409, "account_mismatch", "Session account identity changed")
        try:
            self.session_store.save(
                editor_url=client.editor_url,
                auth_user=client.auth_user,
                account_hash=hashlib.sha256(self.identity.encode()).hexdigest(),
                cookies=rows,
            )
        except Exception:
            raise GatewayError(
                503, "session_persistence_failed", "Cannot persist session"
            ) from None
        self._last_cookies = rows
        self.persistence_failed = False

    def _checkpoint_session(self):
        if self.session_store is None:
            return
        try:
            if (
                self.client.flags.get("docs-hue") != self.identity
                or getattr(self.client, "initialization_valid", True) is False
            ):
                raise ValueError("Session identity changed")
            rows = cookie_rows(self.client.session.cookies.jar)
            if self.persistence_failed or rows != self._last_cookies:
                self._persist_session(self.client)
        except Exception:
            # A paid response must still be recorded even if session storage fails.
            self.persistence_failed = True

    def _watch_session(self):
        self.client.before_generate = self._require_session
        if self.session_store is not None:
            self.client.on_cookies_changed = self._checkpoint_session

    def _require_session(self):
        self._checkpoint_session()
        if (
            self.persistence_failed
            or self.client.flags.get("docs-hue") != self.identity
            or getattr(self.client, "initialization_valid", True) is False
        ):
            raise GatewayError(503, "session_persistence_failed", "Session storage needs recovery")

    def _recover(self):
        for path in self.root.glob("vids_*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] in {"generating", "inserting"}:
                record.update(status="unknown", error="interrupted_acceptance_unknown")
                self._save(record)

    async def start(self):
        if self._failed_start:
            raise GatewayError(503, "session_unavailable", "Vids gateway has been closed")
        if self._lock_file is not None:
            raise GatewayError(503, "runtime_busy", "Vids gateway is already started")
        self._closed = False
        # One process must own the receipt directory. Otherwise a second server
        # could mark a live paid request interrupted or submit a duplicate.
        handle = open(self.root / ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise GatewayError(
                503, "runtime_busy", "Another gateway owns this runtime directory"
            ) from None
        self._lock_file = handle
        try:
            await self._start_session()
        except BaseException:
            self._failed_start = True
            try:
                await self.close()
            except Exception:
                # Startup owns the original failure; cleanup still releases the
                # runtime lock even when closing the HTTP transport fails.
                pass
            raise

    async def _start_session(self):
        saved = None
        if self.refresh_session and (not self.resume or self.cookie_source is None):
            raise GatewayError(
                400, "invalid_session", "Refresh requires resume and a Profile source"
            )
        if self.resume:
            if self.session_store is None or self.client is not None:
                raise GatewayError(400, "invalid_session", "Resume requires an encrypted session")
            try:
                saved = self.session_store.load()
            except Exception:
                raise GatewayError(
                    503, "session_restore_failed", "Cannot restore session"
                ) from None
            if saved is None:
                raise GatewayError(503, "session_missing", "No saved session is available")
            if (
                self.expected_account_hash is not None
                and saved["account_hash"] != self.expected_account_hash
                or self.expected_editor_url is not None
                and saved["editor_url"] != self.expected_editor_url
            ):
                raise GatewayError(409, "account_mismatch", "Saved session binding does not match")
            pin = self.root / ".account"
            if pin.exists() and pin.read_text(encoding="utf-8") != saved["account_hash"]:
                raise GatewayError(
                    409, "account_mismatch", "Saved session account pin does not match"
                )
            cookies = saved["cookies"]
            if self.refresh_session:
                try:
                    cookies = await self.cookie_source(saved["editor_url"], self.proxy)
                except Exception:
                    raise GatewayError(
                        502, "profile_refresh_failed", "Profile cookie refresh failed"
                    ) from None
            self.client = VidsClient(
                saved["editor_url"],
                cookies,
                auth_user=saved["auth_user"],
                proxy=self.proxy,
            )
        elif self.session_store is not None and self.session_store.path.exists():
            raise GatewayError(409, "session_exists", "Use resume to keep the saved session")
        if self.client is None:
            raise GatewayError(400, "invalid_session", "An HTTP session is required")
        await self.client.bootstrap()
        self.identity = self.client.flags.get("docs-hue")
        if not isinstance(self.identity, str) or not self.identity:
            raise GatewayError(502, "identity_missing", "Account identity is unavailable")
        pin = self.root / ".account"
        digest = hashlib.sha256(self.identity.encode()).hexdigest()
        if (
            self.expected_account_hash is not None
            and digest != self.expected_account_hash
            or self.expected_editor_url is not None
            and self.client.editor_url != self.expected_editor_url
        ):
            raise GatewayError(409, "account_mismatch", "Vids session binding does not match")
        if saved is not None and saved["account_hash"] != digest:
            raise GatewayError(409, "account_mismatch", "Saved session belongs to another account")
        if pin.exists() and pin.read_text(encoding="utf-8") != digest:
            raise GatewayError(
                409, "account_mismatch", "Runtime directory belongs to another account"
            )
        await self.client.quota()
        self._recover()
        temporary = pin.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                output.write(digest)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(pin)
        finally:
            temporary.unlink(missing_ok=True)
        self._persist_session(self.client)
        self._watch_session()
        self._ready = True

    async def close(self):
        async with self.lock:
            if self._closed:
                return
            try:
                if self.client is not None:
                    if self._ready:
                        self._checkpoint_session()
                    await self.client.session.close()
            finally:
                if self._lock_file is not None:
                    self._lock_file.close()
                    self._lock_file = None
                self._ready = False
                self._closed = True

    def _path(self, generation_id):
        if not isinstance(generation_id, str) or not re.fullmatch(
            r"vids_[a-f0-9]{64}", generation_id
        ):
            raise GatewayError(404, "not_found", "Generation not found")
        return self.root / (generation_id + ".json")

    def _save(self, record):
        path = self._path(record["id"])
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    def get(self, generation_id):
        path = self._path(generation_id)
        if not path.is_file():
            raise GatewayError(404, "not_found", "Generation not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def public(record):
        result = {k: record[k] for k in ("id", "kind", "status", "created_at", "error")}
        result["object"] = (
            "vids.document_image" if record.get("operation") == "insert" else "vids.generation"
        )
        result["content_url"] = (
            f"/v1/vids/generations/{record['id']}/content"
            if record["status"] == "completed" and record.get("operation") != "insert"
            else None
        )
        result["metadata"] = record.get("metadata", {})
        if record.get("error_operation") in {
            "upload-start",
            "upload-finalize",
            "generate",
            "getSettings",
        }:
            result["error_operation"] = record["error_operation"]
        if isinstance(record.get("upstream_error"), dict):
            result["upstream_error"] = {
                key: record["upstream_error"][key]
                for key in ("status", "reason")
                if isinstance(record["upstream_error"].get(key), str)
            }
        if "outputs" in record:
            result["outputs"] = [
                {
                    "index": output["index"],
                    "metadata": output["metadata"],
                    "content_url": result["content_url"]
                    + (f"?index={output['index']}" if output["index"] else "")
                    if result["content_url"]
                    else None,
                }
                for output in record["outputs"]
            ]
        return result

    def normalize(self, body):
        allowed = {
            "kind",
            "prompt",
            "mode",
            "seconds",
            "size",
            "voice",
            "source_id",
            "shape_id",
            "image_base64",
            "images_base64",
            "aspect_ratio",
            "style",
            "shot_mode",
            "video_base64",
            "n",
        }
        if not isinstance(body, dict) or set(body) - allowed:
            raise GatewayError(400, "invalid_request", "Unsupported generation field")
        kind, prompt = body.get("kind"), body.get("prompt")
        if (
            not isinstance(kind, str)
            or kind not in KINDS
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > 32000
        ):
            raise GatewayError(
                400, "invalid_request", "A supported kind and non-empty prompt are required"
            )
        request = {"kind": kind, "prompt": prompt}
        optional = {
            "video": {
                "mode",
                "seconds",
                "size",
                "source_id",
                "image_base64",
                "images_base64",
                "shot_mode",
                "video_base64",
            },
            "image": {"mode", "shape_id", "image_base64", "aspect_ratio", "style", "n"},
            "speech": {"voice"},
        }
        if set(body) - {"kind", "prompt"} - optional.get(kind, set()):
            raise GatewayError(400, "invalid_request", "Field is unsupported for this generator")
        if kind == "image":
            mode = body.get("mode", "text")
            if not isinstance(mode, str) or mode not in {"text", "edit"}:
                raise GatewayError(400, "invalid_mode", "Image mode must be text or edit")
            count = body.get("n", 1)
            if type(count) is not int or not 1 <= count <= 4 or mode == "edit" and count != 1:
                raise GatewayError(
                    400, "invalid_image_count", "Image text accepts n from 1 to 4; edit requires 1"
                )
            if count != 1:
                request["n"] = count
            if mode == "edit" and ("aspect_ratio" in body or "style" in body):
                raise GatewayError(
                    400,
                    "unsupported_image_option",
                    "Image editing does not accept output ratio or style options",
                )
            aspect = body.get("aspect_ratio", "1:1")
            if not isinstance(aspect, str) or aspect not in IMAGE_ASPECTS:
                raise GatewayError(
                    400, "invalid_aspect_ratio", "aspect_ratio must be 1:1, 16:9 or 9:16"
                )
            if aspect != "1:1":
                request["aspect_ratio"] = aspect
            style = body.get("style", "auto")
            if not isinstance(style, str) or style not in IMAGE_STYLES:
                raise GatewayError(400, "invalid_style", "Unsupported image style")
            if style != "auto":
                request["style"] = style
            if mode == "edit":
                if "image_base64" in body:
                    if "shape_id" in body:
                        raise GatewayError(
                            400, "invalid_source", "Provide one original image or saved shape"
                        )
                    image_input(body["image_base64"])
                    request.update(mode=mode, image_base64=body["image_base64"])
                    return request
                shape_id = body.get("shape_id")
                if (
                    not isinstance(shape_id, str)
                    or not shape_id
                    or len(shape_id) > 4096
                    or any(char.isspace() or ord(char) < 32 for char in shape_id)
                ):
                    raise GatewayError(
                        400, "invalid_source", "Image edit requires a saved document image shape_id"
                    )
                request.update(mode=mode, shape_id=shape_id)
            elif "shape_id" in body or "image_base64" in body:
                raise GatewayError(400, "invalid_source", "Image source requires edit mode")
        if kind == "speech":
            voice = body.get("voice", "Puck")
            if not isinstance(voice, str) or voice not in PRESET_VOICES:
                raise GatewayError(400, "invalid_voice", "Unsupported preset voice")
            request["voice"] = voice
        if kind == "video":
            mode = body.get("mode", "text")
            if not isinstance(mode, str) or mode not in SUPPORTED_VIDEO_SPECIFICATIONS:
                raise GatewayError(400, "invalid_mode", "Unsupported video mode")
            default_seconds, default_size = 3, "1280x720"
            if "video_base64" in body:
                if mode not in {"edit", "extend", "upscale"} or "source_id" in body:
                    raise GatewayError(
                        400,
                        "invalid_source",
                        "Uploaded MP4 requires edit, extend or upscale mode and cannot be mixed with source_id",
                    )
                from .video_input import video_input

                try:
                    _, resource = video_input(body["video_base64"])
                except ValueError as error:
                    raise GatewayError(400, "invalid_video", str(error)) from None
                request["video_base64"] = body["video_base64"]
                duration = resource["duration"]
                if mode == "upscale" and duration > 10:
                    raise GatewayError(
                        400,
                        "unsupported_source_specification",
                        "Upscale input must be 3 to 10 seconds",
                    )
                if mode in {"edit", "upscale"}:
                    default_seconds = min(10, round(duration))
                default_size = "720x1280" if resource["height"] > resource["width"] else "1280x720"
                if mode == "upscale":
                    default_size = (
                        "1080x1920" if resource["height"] > resource["width"] else "1920x1080"
                    )
            elif mode in {"edit", "extend", "upscale"}:
                if "source_id" not in body:
                    raise GatewayError(400, "invalid_source", "This video mode requires source_id")
                source = self.get(body.get("source_id"))
                if source.get("status") != "completed" or source.get("kind") != "video":
                    raise GatewayError(400, "invalid_source", "A completed Vids video is required")
                resource = source.get("resource", {})
                if not isinstance(resource, dict):
                    raise GatewayError(
                        400, "invalid_source", "Source video metadata is unavailable"
                    )
                duration = resource.get("duration")
                if (
                    type(resource.get("width")) is not int
                    or type(resource.get("height")) is not int
                    or f"{resource.get('width')}x{resource.get('height')}" not in VIDEO_SIZES
                    or type(duration) not in (int, float)
                    or not 2.9 <= duration <= (20.1 if mode == "edit" else 10.1)
                ):
                    raise GatewayError(
                        400,
                        "unsupported_source_specification",
                        "Source must be a completed 720p or 1080p Vids video; edit accepts up to 20 seconds, extend/upscale up to 10 seconds",
                    )
                request["source_id"] = source["id"]
                default_size = f"{resource['width']}x{resource['height']}"
                if mode in {"edit", "upscale"}:
                    default_seconds = min(10, round(duration))
                if mode == "upscale":
                    default_size = (
                        "1080x1920" if resource["height"] > resource["width"] else "1920x1080"
                    )
            elif "source_id" in body:
                raise GatewayError(
                    400, "invalid_source", "source_id requires edit, extend or upscale mode"
                )
            seconds, size = body.get("seconds", default_seconds), body.get("size", default_size)
            if (
                type(seconds) is not int
                or not isinstance(size, str)
                or (seconds, size) not in SUPPORTED_VIDEO_SPECIFICATIONS[mode]
            ):
                raise GatewayError(
                    400,
                    "unsupported_specification",
                    "Video seconds must be 3 to 10 and size must be a supported 720p/1080p orientation",
                )
            if mode == "upscale" and (seconds != round(duration) or size != default_size):
                raise GatewayError(
                    400,
                    "invalid_upscale_specification",
                    "Upscale preserves the source duration and orientation",
                )
            request.update(mode=mode, seconds=seconds, size=size)
            shot = body.get("shot_mode", "auto")
            if not isinstance(shot, str) or shot not in VIDEO_SHOT_MODES:
                raise GatewayError(400, "invalid_shot_mode", "Unsupported video shot mode")
            if shot != "auto" and mode not in {"text", "ingredients", "edit"}:
                raise GatewayError(
                    400,
                    "unsupported_shot_mode",
                    "Shot mode requires text, ingredients or edit mode",
                )
            if shot != "auto":
                request["shot_mode"] = shot
            if mode in {"animate", "ingredients"} or mode == "edit" and "images_base64" in body:
                if mode == "animate":
                    inputs = [body.get("image_base64")]
                    if "images_base64" in body:
                        raise GatewayError(400, "invalid_image", "Use image_base64 for animate")
                else:
                    inputs = body.get("images_base64")
                    if (
                        "image_base64" in body
                        or not isinstance(inputs, list)
                        or not 1 <= len(inputs) <= (7 if mode == "ingredients" else 3)
                    ):
                        raise GatewayError(
                            400,
                            "invalid_image",
                            "Create accepts 1 to 7 reference images; video edit accepts 1 to 3",
                        )
                try:
                    total = 0
                    for image in inputs:
                        if not isinstance(image, str) or len(image) > 14 * 1024**2:
                            raise ValueError
                        data = base64.b64decode(image, validate=True)
                        total += len(data)
                        if not 0 < len(data) <= 10 * 1024**2 or not (
                            data.startswith(b"\x89PNG\r\n\x1a\n")
                            or data.startswith(b"\xff\xd8\xff")
                            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                        ):
                            raise ValueError
                    if total > 10 * 1024**2:
                        raise ValueError
                except ValueError:
                    raise GatewayError(
                        400,
                        "invalid_image",
                        "Image inputs must be PNG, JPEG or WebP with at most 10 MiB total",
                    ) from None
                request["image_base64" if mode == "animate" else "images_base64"] = (
                    inputs[0] if mode == "animate" else inputs
                )
            elif "image_base64" in body or "images_base64" in body:
                raise GatewayError(
                    400, "invalid_image", "Image inputs require animate or ingredients mode"
                )
        return request

    async def generate(self, body, idempotency_key, *, admission=None):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise GatewayError(
                400, "idempotency_required", "Idempotency-Key must contain 1 to 200 characters"
            )
        request = self.normalize(body)
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        generation_id = "vids_" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        path = self._path(generation_id)
        if path.exists():
            previous = self.get(generation_id)
            if previous["request_hash"] != digest:
                raise GatewayError(
                    409, "idempotency_conflict", "Idempotency-Key has different parameters"
                )
            return self.public(previous)
        if self.lock.locked():
            raise GatewayError(429, "busy", "The single-account gateway is busy")
        async with self.lock:
            # No await between receipt check and lock acquisition, so a duplicate
            # cannot reach submission twice on this event loop.
            if admission is not None:
                await admission()
            self._require_session()
            record = {
                "id": generation_id,
                "kind": request["kind"],
                "status": "generating",
                "created_at": int(time.time()),
                "error": None,
                "request_hash": digest,
            }
            options = {}
            if request["kind"] == "video":
                record["video_request"] = {
                    name: request[name] for name in ("prompt", "seconds", "size")
                }
                source_duration = None
                if request["mode"] == "extend":
                    if "video_base64" in request:
                        from .video_input import video_input

                        _, source_metadata = video_input(request["video_base64"])
                        source_duration = min(10, source_metadata["duration"])
                    else:
                        source_duration = self.get(request["source_id"])["resource"]["duration"]
                upscale_duration = None
                if request["mode"] == "upscale" and "video_base64" in request:
                    from .video_input import video_input

                    _, source_metadata = video_input(request["video_base64"])
                    upscale_duration = source_metadata["duration"]
                record["video_specification"] = {
                    "seconds": request["seconds"] + source_duration
                    if source_duration is not None
                    else upscale_duration
                    if upscale_duration is not None
                    else request["seconds"],
                    "size": request["size"],
                }
                options = {
                    "seconds": request["seconds"],
                    "resolution": VIDEO_SIZES[request["size"]][0],
                    "orientation": VIDEO_SIZES[request["size"]][1],
                }
                if "shot_mode" in request:
                    options["shot_mode"] = request["shot_mode"]
            elif request["kind"] == "image":
                if "aspect_ratio" in request:
                    record["image_specification"] = {"aspect_ratio": request["aspect_ratio"]}
                if "n" in request:
                    record["image_count"] = request["n"]
            self._save(record)
            admission_token = BEFORE_PAID_REQUEST.set(admission)
            try:
                kind, prompt = request["kind"], request["prompt"]
                if kind == "speech":
                    record["stage"] = "speech_generation"
                    self._save(record)
                    response = await self.client.generate_speech(prompt, voice=request["voice"])
                elif kind == "music":
                    response = await self.client.generate_music(prompt)
                elif kind == "image":
                    shape_id = request.get("shape_id")
                    if "image_base64" in request:
                        from .document import document_scenes

                        record.update(
                            stage="image_upload",
                            image_insert_id="vids_"
                            + hashlib.sha256(
                                ("document-image:" + idempotency_key).encode()
                            ).hexdigest(),
                        )
                        self._save(record)
                        await self.client.bootstrap()
                        self._require_session()
                        if self.client.model_chunk is None:
                            raise DocumentImageSourceError("Document snapshot is unavailable")
                        scenes = document_scenes(self.client.model_chunk)
                        if not scenes:
                            raise DocumentImageSourceError("Document has no saved scene")
                        inserted = await self._insert_document_image_locked(
                            {"image_base64": request["image_base64"], "scene_id": scenes[0]},
                            idempotency_key,
                            admission=admission,
                        )
                        record["image_insert_id"] = inserted["id"]
                        if inserted["status"] != "completed":
                            record.update(
                                status=inserted["status"],
                                error="image_upload_unknown"
                                if inserted["status"] == "unknown"
                                else inserted["error"],
                            )
                            self._save(record)
                            return self.public(record)
                        shape_id = inserted["metadata"]["shape_id"]
                        record["stage"] = "generation"
                        self._save(record)
                    response = (
                        await self.client.edit_document_image(shape_id, prompt)
                        if request.get("mode") == "edit"
                        else await self.client.generate_image(
                            prompt,
                            **{
                                **(
                                    {"aspect": IMAGE_ASPECTS[request["aspect_ratio"]]}
                                    if "aspect_ratio" in request
                                    else {}
                                ),
                                **({"style": request["style"]} if "style" in request else {}),
                                **({"count": request["n"]} if "n" in request else {}),
                            },
                        )
                    )
                elif request["mode"] in {"edit", "extend", "upscale"}:
                    record["stage"] = (
                        "video_upload" if "video_base64" in request else "source_receipt"
                    )
                    self._save(record)
                    if "video_base64" in request:
                        from .video_input import video_input

                        data, source = video_input(request["video_base64"])
                        with tempfile.TemporaryDirectory(dir=self.root) as directory:
                            video = Path(directory) / "input.mp4"
                            video.write_bytes(data)
                            source["temporary_blob"] = await self.client.upload_temporary(video)
                        record["stage"] = "video_generation"
                        self._save(record)
                    else:
                        source = self.get(request["source_id"])["resource"]
                        record["stage"] = "video_generation"
                        self._save(record)
                    tokens = None
                    if "images_base64" in request:
                        record["stage"] = "reference_upload"
                        self._save(record)
                        tokens = []
                        with tempfile.TemporaryDirectory(dir=self.root) as directory:
                            for index, encoded in enumerate(request["images_base64"]):
                                image = Path(directory) / f"input_{index}"
                                image.write_bytes(base64.b64decode(encoded))
                                tokens.append(await self.client.upload_temporary(image))
                    record["stage"] = "video_generation"
                    self._save(record)
                    response = await self.client.submit_video(
                        source, prompt, mode=request["mode"], identifiers=tokens, **options
                    )
                elif request["mode"] in {"animate", "ingredients"}:
                    mode = request["mode"]
                    inputs = (
                        [request["image_base64"]] if mode == "animate" else request["images_base64"]
                    )
                    with tempfile.TemporaryDirectory(dir=self.root) as directory:
                        tokens = []
                        for index, encoded in enumerate(inputs):
                            image = Path(directory) / f"input_{index}"
                            image.write_bytes(base64.b64decode(encoded))
                            tokens.append(await self.client.upload_temporary(image))
                    response = await self.client.submit_images(tokens, prompt, mode=mode, **options)
                else:
                    response = await self.client.submit_text(prompt, **options)
                # Save the accepted response before any parsing/download. A failed
                # download is recoverable without buying another generation.
                record.update(status="received", response=response)
                self._save(record)
            except asyncio.CancelledError:
                record.update(status="unknown", error="acceptance_unknown")
                self._save(record)
                raise
            except VidsTransportError:
                record.update(status="unknown", error="acceptance_unknown")
            except DocumentImageSourceError:
                record.update(status="failed", error="invalid_source")
            except GatewayError as error:
                record.update(status="failed", error=error.code)
            except VidsProtocolError as error:
                record.update(
                    status="unknown" if error.status >= 500 else "failed",
                    error=f"upstream_http_{error.status}",
                    error_operation=error.operation,
                )
                upstream_error = {}
                if error.upstream_status:
                    upstream_error["status"] = error.upstream_status
                if error.upstream_reason:
                    upstream_error["reason"] = error.upstream_reason
                if upstream_error:
                    record["upstream_error"] = upstream_error
            except Exception:
                record.update(status="unknown", error="acceptance_unknown")
            finally:
                BEFORE_PAID_REQUEST.reset(admission_token)
            self._save(record)
            if record["status"] == "received":
                await self._download(record)
            return self.public(record)

    async def _download(self, record):
        try:
            kind = record["kind"]
            resources = (
                video_results
                if kind == "video"
                else image_results
                if kind == "image"
                else audio_results
            )(record["response"])
            if not resources:
                record.update(status="no_result", error="upstream_returned_no_media")
                self._save(record)
                return
            resource = resources[0]
            if kind == "image" and len(resources) != record.get("image_count", 1):
                record.update(status="specification_mismatch", error="upstream_image_count")
                self._save(record)
                return
            if kind == "image" and "image_specification" in record:
                numerator, denominator = map(
                    int, record["image_specification"]["aspect_ratio"].split(":")
                )
                if any(
                    abs(image["width"] / image["height"] - numerator / denominator) > 0.02
                    for image in resources
                ):
                    record.update(
                        status="specification_mismatch", error="upstream_image_specification"
                    )
                    self._save(record)
                    return
            if kind == "video" and "video_specification" in record:
                specification = record["video_specification"]
                width, height = map(int, specification["size"].split("x"))
                if (resource["width"], resource["height"]) != (width, height) or abs(
                    resource["duration"] - specification["seconds"]
                ) > 0.1:
                    record.update(
                        status="specification_mismatch", error="upstream_video_specification"
                    )
                    self._save(record)
                    return
            suffix, mime = {
                "video": (".mp4", "video/mp4"),
                "image": (".jpg", "image/jpeg"),
                "speech": (".wav", "audio/wav"),
                "music": (".wav", "audio/wav"),
            }[kind]
            path = self.root / (record["id"] + suffix)
            outputs = []
            if kind == "video":
                await self.client.download_video(resource, path)
            elif kind == "image":
                for index, image in enumerate(resources):
                    image_path = self.root / (
                        record["id"] + (f"_{index}" if index else "") + suffix
                    )
                    await self.client.download_image(image, image_path)
                    image_data = image_path.read_bytes()
                    if not image_data.startswith(b"\xff\xd8\xff"):
                        raise ValueError("Expected JPEG")
                    outputs.append(
                        {
                            "index": index,
                            "metadata": {
                                "width": image["width"],
                                "height": image["height"],
                                "bytes": len(image_data),
                            },
                        }
                    )
            else:
                await self.client.download_audio(resource, path)
            data = path.read_bytes()
            if kind == "video":
                from .video_input import mp4_metadata

                actual = mp4_metadata(data, minimum_duration=2.9, maximum_duration=20.1)
                if (actual["width"], actual["height"]) != (
                    resource["width"],
                    resource["height"],
                ) or abs(actual["duration"] - resource["duration"]) > 0.1:
                    record.update(
                        status="specification_mismatch", error="upstream_video_specification"
                    )
                    self._save(record)
                    return
                if "video_specification" in record:
                    specification = record["video_specification"]
                    if (
                        f"{actual['width']}x{actual['height']}" != specification["size"]
                        or abs(actual["duration"] - specification["seconds"]) > 0.1
                    ):
                        record.update(
                            status="specification_mismatch", error="upstream_video_specification"
                        )
                        self._save(record)
                        return
                resource = {**resource, **actual}
            if kind in {"speech", "music"} and not (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
                raise ValueError("Expected WAV")
            record.update(
                status="completed",
                error=None,
                resource=resource,
                suffix=suffix,
                mime_type=mime,
                metadata={k: resource[k] for k in ("width", "height", "duration") if k in resource},
            )
            record["metadata"]["bytes"] = len(data)
            if len(outputs) > 1:
                record["outputs"] = outputs
            if kind == "video":
                record["completed_at"] = int(time.time())
        except asyncio.CancelledError:
            record.update(status="download_failed", error="download_interrupted")
            self._save(record)
            raise
        except Exception:
            record.update(status="download_failed", error="download_failed")
        self._save(record)

    async def retry_download(self, generation_id):
        async with self.lock:
            record = self.get(generation_id)
            if record["status"] in {"received", "download_failed"}:
                await self._download(record)
            elif record["status"] != "completed":
                raise GatewayError(
                    409,
                    "no_received_result",
                    "No accepted media response is available; generation will not be repeated",
                )
            return self.public(record)

    async def replace_cookies(self, cookies):
        async with self.lock:
            return await self._replace_cookies(cookies)

    async def refresh_cookies(self):
        if self.cookie_source is None:
            raise GatewayError(503, "refresh_unavailable", "No Profile cookie source is configured")
        async with self.lock:
            try:
                cookies = await self.cookie_source(self.client.editor_url, self.client.proxy)
            except Exception:
                raise GatewayError(
                    502, "profile_refresh_failed", "Profile cookie refresh failed"
                ) from None
            return await self._replace_cookies(cookies)

    async def _replace_cookies(self, cookies):
        if (
            not isinstance(cookies, list)
            or not cookies
            or not all(isinstance(row, dict) for row in cookies)
        ):
            raise GatewayError(400, "invalid_session", "cookies must contain cookie objects")
        candidate = VidsClient(
            self.client.editor_url,
            cookies,
            auth_user=self.client.auth_user,
            proxy=self.client.proxy,
        )
        try:
            await candidate.bootstrap()
            await candidate.quota()
            if not self.identity or candidate.flags.get("docs-hue") != self.identity:
                raise GatewayError(
                    409,
                    "account_mismatch",
                    "Credential update must preserve the current account",
                )
            self._persist_session(candidate)
        except BaseException:
            await candidate.session.close()
            raise
        old, self.client = self.client, candidate
        self._watch_session()
        await old.session.close()
        return {"updated": True, "transport": "pure_http"}

    async def quota(self, kind):
        if kind not in KINDS:
            raise GatewayError(400, "invalid_kind", "Unsupported generation kind")
        async with self.lock:
            feature = {
                "video": video_feature(self.client.flags),
                "image": 205,
                "music": 242,
                "speech": 254,
            }[kind]
            response = await self.client.quota(feature)
            try:
                value = response[0]
                return {
                    "kind": kind,
                    "total": int(value[1][0]),
                    "available": int(value[1][2]),
                    "consumed": int(value[2]),
                }
            except (IndexError, TypeError, ValueError):
                raise GatewayError(
                    502, "schema_changed", "Upstream quota structure changed"
                ) from None

    async def document_images(self, scenes=False):
        """List saved image shapes from a fresh single-chunk document snapshot."""
        async with self.lock:
            self._require_session()
            await self.client.bootstrap()
            self._require_session()
            model = self.client.model_chunk
            if model is None:
                raise GatewayError(
                    503, "unverified_document", "A saved Vids document snapshot is unavailable"
                )
            if scenes:
                from .document import document_scenes

                return {
                    "object": "list",
                    "revision": model["revision"],
                    "data": [{"scene_id": identifier} for identifier in document_scenes(model)],
                }
            return {
                "object": "list",
                "revision": model["revision"],
                "data": [
                    {"shape_id": shape["shape_id"], "scene_id": shape["scene_id"]}
                    for shape in document_image_shapes(model)
                ],
            }

    async def insert_document_image(self, body, idempotency_key, *, admission=None):
        if isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 200:
            identifier = (
                "vids_" + hashlib.sha256(("document-image:" + idempotency_key).encode()).hexdigest()
            )
            if self._path(identifier).exists():
                return self.public(await self._insert_document_image_locked(body, idempotency_key))
        if self.lock.locked():
            raise GatewayError(429, "busy", "The single-account gateway is busy")
        async with self.lock:
            return self.public(
                await self._insert_document_image_locked(body, idempotency_key, admission=admission)
            )

    async def _insert_document_image_locked(self, body, idempotency_key, *, admission=None):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise GatewayError(400, "idempotency_required", "Idempotency-Key is required")
        if not isinstance(body, dict) or set(body) != {"image_base64", "scene_id"}:
            raise GatewayError(400, "invalid_request", "image_base64 and scene_id are required")
        scene_id = body["scene_id"]
        if not isinstance(scene_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,4096}", scene_id):
            raise GatewayError(400, "invalid_source", "A saved scene_id is required")
        image, mime = image_input(body["image_base64"])
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        identifier = (
            "vids_" + hashlib.sha256(("document-image:" + idempotency_key).encode()).hexdigest()
        )
        if self._path(identifier).exists():
            previous = self.get(identifier)
            if previous["request_hash"] != digest:
                raise GatewayError(
                    409, "idempotency_conflict", "Idempotency-Key has different parameters"
                )
            return previous
        if admission is not None:
            await admission()
        self._require_session()
        record = {
            "id": identifier,
            "kind": "image",
            "operation": "insert",
            "status": "inserting",
            "created_at": int(time.time()),
            "error": None,
            "request_hash": digest,
            "metadata": {"shape_id": "http_image_" + uuid.uuid4().hex, "scene_id": scene_id},
        }
        self._save(record)

        def checkpoint(source):
            record["insertion_source"] = source
            self._save(record)

        try:
            with tempfile.TemporaryDirectory(dir=self.root) as directory:
                path = Path(directory) / "input"
                path.write_bytes(image)
                metadata = await self.client.insert_document_image(
                    path,
                    mime_type=mime,
                    shape_id=record["metadata"]["shape_id"],
                    scene_id=scene_id,
                    checkpoint=checkpoint,
                )
            record.update(status="completed", metadata=metadata)
        except DocumentImageSourceError:
            record.update(status="failed", error="invalid_source")
        except GatewayError as error:
            record.update(status="failed", error=error.code)
        except VidsProtocolError as error:
            record.update(
                status="unknown" if error.status >= 500 else "failed",
                error=f"upstream_http_{error.status}",
            )
        except asyncio.CancelledError:
            record.update(status="unknown", error="insertion_acceptance_unknown")
            self._save(record)
            raise
        except Exception:
            record.update(status="unknown", error="insertion_acceptance_unknown")
        self._save(record)
        return record

    async def verify_document_image(self, identifier):
        """Confirm a previous insertion by reading the snapshot; never upload/save again."""
        async with self.lock:
            record = self.get(identifier)
            source = record.get("insertion_source")
            if record.get("operation") != "insert" or not source:
                raise GatewayError(400, "invalid_source", "No previous document save is available")
            if record["status"] == "completed":
                return self.public(record)
            self._require_session()
            await self.client.bootstrap()
            self._require_session()
            model = self.client.model_chunk
            expected = {
                "shape_id": source["shape_id"],
                "scene_id": source["scene_id"],
                "cosmo_id": source["image"]["cosmo_id"],
            }
            if (
                model is not None
                and model["revision"] > source["revision"]
                and expected in document_image_shapes(model)
            ):
                record.update(
                    status="completed",
                    error=None,
                    metadata={
                        "shape_id": source["shape_id"],
                        "scene_id": source["scene_id"],
                        "revision": model["revision"],
                    },
                )
                self._save(record)
            return self.public(record)

    def content(self, generation_id, index=0):
        if type(index) is not int or not 0 <= index <= 3:
            raise GatewayError(400, "invalid_output_index", "Image output index must be 0 to 3")
        record = self.get(generation_id)
        if record.get("id") != generation_id:
            raise GatewayError(410, "expired", "Generation content is unavailable")
        if record["status"] != "completed" or record.get("operation") == "insert":
            raise GatewayError(409, "not_ready", "Generation content is unavailable")
        if index and (
            record["kind"] != "image"
            or index >= len(record.get("outputs", []))
            or record["outputs"][index]["index"] != index
        ):
            raise GatewayError(404, "not_found", "Image output not found")
        suffix = {"image/jpeg": ".jpg", "audio/wav": ".wav", "video/mp4": ".mp4"}.get(
            record["mime_type"]
        )
        if suffix is None or record["suffix"] != suffix:
            raise GatewayError(410, "expired", "Generation content is unavailable")
        path = self.root / (generation_id + (f"_{index}" if index else "") + suffix)
        if path.resolve().parent != self.root or not path.is_file():
            raise GatewayError(410, "expired", "Generation content is unavailable")
        return path, record["mime_type"]
