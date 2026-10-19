"""API routes for OpenAI-compatible and Gemini generateContent endpoints."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import asyncio
import base64
import binascii
import ipaddress
import json
import mimetypes
import re
import socket
import struct
import time
import uuid
from urllib.parse import unquote, urlparse

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.auth import verify_api_key_flexible, verify_openai_api_key
from ..core.config import config
from ..core.logger import debug_logger
from ..core.model_resolver import get_base_model_aliases, resolve_model_name
from ..core.public_models import PUBLIC_MODELS, resolve_public_model
from ..core.models import (
    ChatCompletionRequest,
    ChatMessage,
    GeminiContent,
    GeminiGenerateContentRequest,
)
from ..services.generation_handler import MODEL_CONFIG, GenerationHandler
from .openai_common import OpenAIRoute, safe_error
from .openai_responses import create_responses_router
from ..services.model_gateway import ModelGateway

router = APIRouter(route_class=OpenAIRoute)
router.include_router(create_responses_router())
model_gateway = ModelGateway()

MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,\s]+);base64,(?P<data>.+)$", re.DOTALL)
MAX_VIDEO_BYTES = 100 * 1024 * 1024
VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/mpeg",
    "video/ogg",
    "video/x-matroska",
    "video/3gpp",
    "video/avi",
}


def _mp4_duration_ms(video_bytes: bytes) -> Optional[int]:
    """Read an MP4 movie-header duration without requiring ffmpeg."""
    marker = video_bytes.find(b"mvhd")
    if marker < 0 or marker + 28 > len(video_bytes):
        return None
    version = video_bytes[marker + 4]
    try:
        if version == 0:
            timescale = struct.unpack_from(">I", video_bytes, marker + 16)[0]
            duration = struct.unpack_from(">I", video_bytes, marker + 20)[0]
        elif version == 1 and marker + 40 <= len(video_bytes):
            timescale = struct.unpack_from(">I", video_bytes, marker + 24)[0]
            duration = struct.unpack_from(">Q", video_bytes, marker + 28)[0]
        else:
            return None
    except struct.error:
        return None
    if not timescale or not duration:
        return None
    return max(1, round(duration * 1000 / timescale))


def _validate_uploaded_edit_trim(
    video_bytes: Optional[bytes],
    video_mime_type: str,
    options: Optional[Dict[str, Any]],
    *,
    clamp_default: bool = False,
    explicit_trim_end: bool = False,
) -> None:
    if (
        not video_bytes
        or (video_mime_type or "").lower() != "video/mp4"
        or not isinstance(options, dict)
    ):
        return
    if options.get("mode") != "edit":
        return
    duration_ms = _mp4_duration_ms(video_bytes)
    if duration_ms is None:
        if clamp_default and not explicit_trim_end:
            raise HTTPException(status_code=400, detail="Unable to determine uploaded MP4 duration")
        return
    if clamp_default and not explicit_trim_end:
        start_ms = int(options.get("trimStartMs", 0))
        options["trimEndMs"] = min(duration_ms, start_ms + 10000)
        if options["trimEndMs"] <= start_ms:
            raise HTTPException(
                status_code=400, detail="trimStartMs must be before the uploaded video end"
            )
    end_ms = int(options.get("trimEndMs", 10000))
    if end_ms > duration_ms:
        raise HTTPException(
            status_code=400,
            detail=f"trimEndMs cannot exceed the uploaded video duration ({duration_ms} ms)",
        )


def _clamp_default_uploaded_edit(request, video_bytes, video_mime_type):
    """Only SKU adapters request a source-aware default edit endpoint."""
    if not video_bytes or (video_mime_type or "").lower() != "video/mp4":
        return
    generation = request.generationConfig
    if generation is None:
        return
    options = generation.model_extra.get("videoConfig")
    if not isinstance(options, dict) or options.get("mode") != "edit":
        return
    if "trimEndMs" in options:
        return
    _validate_uploaded_edit_trim(video_bytes, video_mime_type, options, clamp_default=True)


MEDIA_PROMPT_TOOL_BLOCK_RE = re.compile(r"<tools>.*?</tools>", re.IGNORECASE | re.DOTALL)
MEDIA_SYSTEM_INSTRUCTION_MARKERS = (
    "<tools>",
    "</tools>",
    "function calling ai model",
    "function signatures",
    '"$schema"',
    '"additionalproperties"',
)
MEDIA_PROMPT_PREAMBLE_PATTERNS = (
    re.compile(r"^you are a function calling ai model\.?$", re.IGNORECASE),
    re.compile(
        r"^you are provided with function signatures within .* xml tags\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^you may call one or more functions to assist with the user query\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^don't make assumptions about what values to plug into functions\.?$",
        re.IGNORECASE,
    ),
    re.compile(r"^here are the available tools:.*$", re.IGNORECASE),
)
GEMINI_STATUS_MAP = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ABORTED",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}

# Dependency injection will be set up in main.py
generation_handler: GenerationHandler = None


@dataclass
class NormalizedGenerationRequest:
    """Internal request shape shared by OpenAI and Gemini entrypoints."""

    model: str
    prompt: str
    images: List[bytes]
    messages: Optional[List[ChatMessage]] = None
    video_media_id: Optional[str] = None
    video_bytes: Optional[bytes] = None
    video_mime_type: str = "video/mp4"
    public_model: Optional[str] = None
    generation_options: Optional[Dict[str, Any]] = None


def set_generation_handler(handler: GenerationHandler):
    """Set generation handler instance."""
    global generation_handler
    generation_handler = handler


def set_model_gateway(gateway: ModelGateway):
    """Set the application-scoped provider registry."""
    global model_gateway
    model_gateway = gateway


def _get_model_gateway(raw_request: Optional[Request] = None) -> ModelGateway:
    """Resolve the registry from the app instance, with legacy fallback for direct callers."""
    app = getattr(raw_request, "app", None)
    state = getattr(app, "state", None)
    return getattr(state, "model_gateway", model_gateway)


def _ensure_generation_handler() -> GenerationHandler:
    if generation_handler is None:
        raise HTTPException(status_code=500, detail="Generation handler not initialized")
    return generation_handler


def _build_model_description(model_config: Dict[str, Any]) -> str:
    """Build a human-readable description for model listing endpoints."""
    description = f"{model_config['type'].capitalize()} generation"
    if model_config["type"] == "image":
        description += f" - {model_config['model_name']}"
    else:
        description += f" - {model_config['model_key']}"
    return description


def _get_openai_model_catalog(include_legacy: bool = False) -> List[Dict[str, Any]]:
    """Expose product models and public compatibility names."""
    catalog = [
        {"id": model_id, **spec}
        for model_id, spec in PUBLIC_MODELS.items()
        if include_legacy or not spec.get("legacy")
    ]
    if include_legacy:
        legacy = {
            **get_base_model_aliases(),
            **{name: _build_model_description(cfg) for name, cfg in MODEL_CONFIG.items()},
        }
        catalog.extend(
            {"id": name, "description": description, "legacy": True}
            for name, description in legacy.items()
            if name not in PUBLIC_MODELS
        )
    return catalog


def _get_gemini_model_catalog(include_legacy: bool = False) -> Dict[str, str]:
    """Collect Gemini-compatible model metadata for /models endpoints."""
    return {
        entry["id"]: entry["description"] for entry in _get_openai_model_catalog(include_legacy)
    }


def _build_gemini_model_resource(model_id: str, description: str) -> Dict[str, Any]:
    """Build a Gemini-compatible model resource payload."""
    resource = {
        "name": f"models/{model_id}",
        "displayName": model_id,
        "description": description,
        "version": "lay4flow",
        "inputTokenLimit": 0,
        "outputTokenLimit": 0,
        "supportedGenerationMethods": [
            "generateContent",
            "streamGenerateContent",
        ],
    }
    if model_id in PUBLIC_MODELS:
        resource.update({key: PUBLIC_MODELS[model_id][key] for key in ("capabilities", "defaults")})
    return resource


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.match(data_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid data URL")
    return match.group("mime"), base64.b64decode(match.group("data"))


def _validate_video_mime_type(mime_type: str) -> str:
    mime = mime_type.lower().split(";", 1)[0].strip()
    if mime not in VIDEO_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported video MIME type: {mime}")
    return mime


def _decode_video_base64(value: str, mime_type: str = "video/mp4") -> tuple[str, bytes]:
    """Decode one bounded video data/base64 payload at the API boundary."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="Video payload cannot be empty")
    mime = mime_type.lower().split(";", 1)[0].strip()
    encoded = value
    match = DATA_URL_RE.match(value)
    if match:
        mime = match.group("mime").lower()
        encoded = match.group("data")
    mime = _validate_video_mime_type(mime)
    encoded = re.sub(r"\s+", "", encoded)
    if len(encoded) > ((MAX_VIDEO_BYTES + 2) // 3) * 4:
        raise HTTPException(status_code=400, detail="Video exceeds the 100MB limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Invalid video base64")
    if not data:
        raise HTTPException(status_code=400, detail="Video payload cannot be empty")
    if len(data) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=400, detail="Video exceeds the 100MB limit")
    return mime, data


def _detect_image_mime_type(image_bytes: bytes, fallback: str = "image/png") -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def _guess_mime_type(uri: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(urlparse(uri).path)
    return guessed or fallback


async def retrieve_image_data(url: str) -> Optional[bytes]:
    """Read image bytes from local /tmp cache or remote URL."""
    file_cache = getattr(generation_handler, "file_cache", None)
    try:
        if "/tmp/" in url and file_cache:
            path = urlparse(url).path
            filename = path.split("/tmp/")[-1]
            local_file_path = file_cache.cache_dir / filename

            if local_file_path.exists() and local_file_path.is_file():
                data = local_file_path.read_bytes()
                if data:
                    return data
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] 本地缓存读取失败: {str(exc)}")

    proxy_url = None
    try:
        if file_cache and hasattr(file_cache, "_resolve_download_proxy"):
            proxy_url = await file_cache._resolve_download_proxy("image")
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] 图片下载代理解析失败: {str(exc)}")

    try:
        async with AsyncSession() as session:
            response = await session.get(
                url,
                timeout=60,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                headers={
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Referer": "https://labs.google/",
                },
                impersonate="chrome120",
                verify=False,
            )
            if response.status_code == 200 and response.content:
                return response.content
            debug_logger.log_warning(f"[CONTEXT] 图片下载失败，状态码: {response.status_code}")
    except Exception as exc:
        debug_logger.log_error(f"[CONTEXT] 图片下载异常: {str(exc)}")

    return None


async def _load_image_bytes_from_uri(uri: str) -> bytes:
    if not uri:
        raise HTTPException(status_code=400, detail="Image URI cannot be empty")

    if uri.startswith("data:image"):
        _, image_bytes = _decode_data_url(uri)
        return image_bytes

    if uri.startswith("http://") or uri.startswith("https://") or "/tmp/" in uri:
        image_bytes = await retrieve_image_data(uri)
        if image_bytes:
            return image_bytes
        raise HTTPException(status_code=400, detail=f"Failed to load image from {uri}")

    raise HTTPException(status_code=400, detail=f"Unsupported image URI: {uri}")


def _coerce_gemini_contents(raw_contents: Optional[List[Any]]) -> List[GeminiContent]:
    contents: List[GeminiContent] = []
    for item in raw_contents or []:
        if isinstance(item, GeminiContent):
            contents.append(item)
        else:
            contents.append(GeminiContent.model_validate(item))
    return contents


def _extract_text_from_gemini_content(content: Optional[GeminiContent]) -> str:
    if content is None:
        return ""
    text_parts = [part.text.strip() for part in content.parts if part.text]
    return "\n".join(part for part in text_parts if part).strip()


def _should_ignore_media_system_instruction(system_instruction: str) -> bool:
    """Drop agent/tool scaffolding before sending media prompts upstream."""
    if not system_instruction:
        return False

    normalized = system_instruction.lower()
    if len(system_instruction) > 1200:
        return True

    return any(marker in normalized for marker in MEDIA_SYSTEM_INSTRUCTION_MARKERS)


def _sanitize_media_prompt(prompt: str) -> str:
    """Strip agent/tool scaffolding that image/video models cannot use."""
    if not prompt:
        return ""

    sanitized = MEDIA_PROMPT_TOOL_BLOCK_RE.sub(" ", prompt.strip())
    cleaned_lines: List[str] = []
    for raw_line in sanitized.splitlines():
        line = raw_line.strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if any(pattern.fullmatch(line) for pattern in MEDIA_PROMPT_PREAMBLE_PATTERNS):
            continue
        cleaned_lines.append(line)

    sanitized = "\n".join(cleaned_lines).strip()
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


async def _extract_prompt_and_images_from_openai_messages(
    messages: List[ChatMessage],
) -> tuple[str, List[bytes], Optional[str], Optional[bytes], str]:
    """Extract the final prompt, images, and single video or extend:// reference."""
    last_message = messages[-1]
    content = last_message.content
    prompt_parts: List[str] = []
    images: List[bytes] = []
    video_media_id: Optional[str] = None
    video_bytes: Optional[bytes] = None
    video_mime_type = "video/mp4"

    if isinstance(content, str):
        prompt_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "").strip()
                if text:
                    prompt_parts.append(text)
            elif item_type == "image_url":
                image_url = item.get("image_url", {}).get("url", "")
                # extend://MEDIA_ID 用于视频续写
                if image_url.startswith("extend://"):
                    if video_bytes is not None or video_media_id is not None:
                        raise HTTPException(
                            status_code=400, detail="Only one video input is supported"
                        )
                    video_media_id = image_url[len("extend://") :]
                else:
                    images.append(await _load_image_bytes_from_uri(image_url))
            elif item_type == "video_url":
                if video_bytes is not None or video_media_id is not None:
                    raise HTTPException(status_code=400, detail="Only one video input is supported")
                video_reference = item.get("video_url")
                if not isinstance(video_reference, dict):
                    raise HTTPException(status_code=400, detail="video_url must contain a URL")
                video_url = video_reference.get("url", "")
                if not isinstance(video_url, str):
                    raise HTTPException(status_code=400, detail="Video URI must be a string")
                if video_url.startswith("data:"):
                    video_mime_type, video_bytes = _decode_video_base64(video_url)
                elif video_url.startswith("extend://"):
                    video_media_id = video_url[len("extend://") :]
                else:
                    video_mime_type = _guess_mime_type(video_url, "video/mp4")
                    _validate_video_mime_type(video_mime_type)
                    video_bytes = await _load_video_bytes_from_uri(video_url)

    prompt = "\n".join(part for part in prompt_parts if part).strip()
    return prompt, images, video_media_id, video_bytes, video_mime_type


async def _load_video_bytes_from_uri(uri: str) -> bytes:
    if not uri:
        raise HTTPException(status_code=400, detail="Video URI cannot be empty")
    if uri.startswith("data:"):
        _, data = _decode_video_base64(uri)
        return data

    try:
        parsed = urlparse(uri)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid video URI")
    file_cache = getattr(generation_handler, "file_cache", None)
    cache_url = urlparse(config.cache_base_url)
    relative_cache = not parsed.scheme and not parsed.netloc and parsed.path.startswith("/tmp/")
    same_cache_origin = bool(cache_url.netloc) and (parsed.scheme, parsed.netloc) == (
        cache_url.scheme,
        cache_url.netloc,
    )
    if file_cache and parsed.path.startswith("/tmp/") and (relative_cache or same_cache_origin):
        filename = unquote(parsed.path[len("/tmp/") :])
        path = (file_cache.cache_dir / filename).resolve()
        cache_root = file_cache.cache_dir.resolve()
        if path == cache_root or cache_root not in path.parents:
            raise HTTPException(status_code=400, detail="Invalid local video path")
        if path.is_file():
            if path.stat().st_size > MAX_VIDEO_BYTES:
                raise HTTPException(status_code=400, detail="Video exceeds the 100MB limit")
            with path.open("rb") as cached:
                data = cached.read(MAX_VIDEO_BYTES + 1)
            if len(data) > MAX_VIDEO_BYTES:
                raise HTTPException(status_code=400, detail="Video exceeds the 100MB limit")
            if not data:
                raise HTTPException(status_code=400, detail="Video payload cannot be empty")
            return data
        raise HTTPException(status_code=400, detail="Local video was not found")

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=400, detail="Video URI must be an http(s) URL or cache path"
        )
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, port, type=socket.SOCK_STREAM
        )
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global or ip.is_multicast:
                raise HTTPException(status_code=400, detail="Video URL points to a private network")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Video host could not be resolved")

    if not addresses:
        raise HTTPException(status_code=400, detail="Video host could not be resolved")
    address = addresses[0][4][0]
    resolved_address = f"[{address}]" if ":" in address else address
    received = bytearray()
    try:
        # Pin the checked address and disable redirects to prevent DNS rebinding/SSRF.
        async with AsyncSession(
            trust_env=False,
            curl_options={CurlOpt.RESOLVE: [f"{parsed.hostname}:{port}:{resolved_address}"]},
        ) as session:
            response = await session.get(
                uri,
                timeout=60,
                headers={"Accept": "video/*,*/*;q=0.8"},
                impersonate="chrome120",
                verify=True,
                stream=True,
                allow_redirects=False,
            )
            try:
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=400, detail="Failed to load video (redirects are not followed)"
                    )
                content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
                if content_type and content_type != "application/octet-stream":
                    _validate_video_mime_type(content_type)
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_VIDEO_BYTES:
                    raise HTTPException(status_code=400, detail="Video exceeds the 100MB limit")
                async for chunk in response.aiter_content(1024 * 1024):
                    if len(received) + len(chunk) > MAX_VIDEO_BYTES:
                        raise HTTPException(status_code=400, detail="Video exceeds the 100MB limit")
                    received.extend(chunk)
            finally:
                await response.aclose()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to load video")
    if not received:
        raise HTTPException(status_code=400, detail="Video payload cannot be empty")
    return bytes(received)


async def _append_openai_reference_images(
    model: str,
    messages: List[ChatMessage],
    images: List[bytes],
) -> List[bytes]:
    model_config = MODEL_CONFIG.get(model)
    if not model_config or model_config["type"] != "image" or len(messages) <= 1:
        return images

    debug_logger.log_info(f"[CONTEXT] 开始查找历史参考图，消息数量: {len(messages)}")

    for msg in reversed(messages[:-1]):
        if msg.role == "assistant" and isinstance(msg.content, str):
            matches = MARKDOWN_IMAGE_RE.findall(msg.content)
            if not matches:
                continue

            for image_url in reversed(matches):
                if not image_url.startswith("http") and "/tmp/" not in image_url:
                    continue
                try:
                    downloaded_bytes = await retrieve_image_data(image_url)
                    if downloaded_bytes:
                        images.insert(0, downloaded_bytes)
                        debug_logger.log_info(f"[CONTEXT] ✅ 添加历史参考图: {image_url}")
                        return images
                    debug_logger.log_warning(
                        f"[CONTEXT] 图片下载失败或为空，尝试下一个: {image_url}"
                    )
                except Exception as exc:
                    debug_logger.log_error(f"[CONTEXT] 处理参考图时出错: {str(exc)}")
    return images


async def _extract_prompt_and_images_from_gemini_contents(
    contents: List[GeminiContent],
) -> tuple[str, List[bytes], Optional[str], Optional[bytes], str]:
    if not contents:
        raise HTTPException(status_code=400, detail="contents cannot be empty")

    target_content = next(
        (content for content in reversed(contents) if (content.role or "user") == "user"),
        contents[-1],
    )

    prompt_parts: List[str] = []
    images: List[bytes] = []
    video_media_id: Optional[str] = None
    video_bytes: Optional[bytes] = None
    video_mime_type = "video/mp4"

    for part in target_content.parts:
        if part.text:
            text = part.text.strip()
            if text:
                prompt_parts.append(text)
        elif part.inlineData is not None:
            mime_type = part.inlineData.mimeType.lower()
            if mime_type.startswith("video/"):
                if video_bytes is not None or video_media_id is not None:
                    raise HTTPException(status_code=400, detail="Only one video input is supported")
                video_mime_type, video_bytes = _decode_video_base64(part.inlineData.data, mime_type)
                continue
            if not mime_type.startswith("image/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported inlineData mime type: {part.inlineData.mimeType}",
                )
            images.append(base64.b64decode(part.inlineData.data))
        elif part.fileData is not None:
            mime_type = (
                part.fileData.mimeType or _guess_mime_type(part.fileData.fileUri, "")
            ).lower()
            if mime_type.startswith("video/"):
                if video_bytes is not None or video_media_id is not None:
                    raise HTTPException(status_code=400, detail="Only one video input is supported")
                if part.fileData.fileUri.startswith("extend://"):
                    video_media_id = part.fileData.fileUri[len("extend://") :]
                else:
                    video_bytes = await _load_video_bytes_from_uri(part.fileData.fileUri)
                video_mime_type = _validate_video_mime_type(mime_type)
                continue
            if mime_type and not mime_type.startswith("image/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported fileData mime type: {part.fileData.mimeType}",
                )
            images.append(await _load_image_bytes_from_uri(part.fileData.fileUri))

    prompt = "\n".join(part for part in prompt_parts if part).strip()
    return prompt, images, video_media_id, video_bytes, video_mime_type


def _resolve_request_model(model: str, request: Any, images: Optional[List[bytes]] = None) -> str:
    resolved_model = resolve_model_name(
        model=model, request=request, model_config=MODEL_CONFIG, images=images
    )
    if resolved_model != model:
        debug_logger.log_info(f"[ROUTE] 模型名已转换: {model} → {resolved_model}")
    return resolved_model


def _resolve_generation_request(model, request, images, video_bytes, video_media_id):
    try:
        resolved = resolve_public_model(
            model,
            request,
            MODEL_CONFIG,
            images=images,
            has_video=video_bytes is not None,
            video_media_id=video_media_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if resolved is not None:
        return resolved.internal_model, resolved.public_model, resolved.options
    raw = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else vars(request)
    sources = [raw, raw.get("extra_body") or {}, raw.get("extraBody") or {}]
    for source in sources:
        if not isinstance(source, dict):
            continue
        parents = [
            source,
            source.get("generationConfig") or {},
            source.get("generation_config") or {},
        ]
        if any(
            isinstance(parent, dict) and any(k in parent for k in ("videoConfig", "video_config"))
            for parent in parents
        ):
            raise HTTPException(
                status_code=400,
                detail="Use veo_3_1 or omni_1.1_flash with videoConfig; legacy video IDs already encode their parameters",
            )
    return _resolve_request_model(model, request, images), model, None


def _validate_video_model(model: str, uploaded_video: bool = True) -> None:
    """Video uploads are only meaningful for the Omni edit route."""
    config_entry = MODEL_CONFIG.get(model)
    allowed_types = {"edit"} if uploaded_video else {"edit", "extend"}
    if not config_entry or config_entry.get("video_type") not in allowed_types:
        raise HTTPException(
            status_code=400, detail="Video input is only supported by omni_edit models"
        )


def _get_request_base_url(request: Request) -> Optional[str]:
    """根据实际请求头推导对外可访问的基础地址。"""
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = (forwarded_host or request.headers.get("host") or "").strip()

    if not host:
        return None

    proto = forwarded_proto or request.url.scheme or "http"
    return f"{proto}://{host}"


async def _normalize_openai_request(
    request: ChatCompletionRequest,
    *,
    clamp_default_edit_trim: bool = False,
) -> NormalizedGenerationRequest:
    if request.messages:
        (
            prompt,
            images,
            video_media_id,
            video_bytes,
            video_mime_type,
        ) = await _extract_prompt_and_images_from_openai_messages(request.messages)
        if request.image and not images:
            images.append(await _load_image_bytes_from_uri(request.image))
        if video_bytes is not None or video_media_id is not None or request.video:
            if request.video and (video_bytes is not None or video_media_id is not None):
                raise HTTPException(status_code=400, detail="Only one video input is supported")
            if request.video and video_bytes is None:
                if request.video.startswith("data:") or not request.video.startswith(
                    ("http://", "https://", "/tmp/")
                ):
                    video_mime_type, video_bytes = _decode_video_base64(request.video)
                else:
                    video_mime_type = _validate_video_mime_type(
                        _guess_mime_type(request.video, "video/mp4")
                    )
                    video_bytes = await _load_video_bytes_from_uri(request.video)
        if clamp_default_edit_trim:
            _clamp_default_uploaded_edit(request, video_bytes, video_mime_type)
        model, public_model, options = _resolve_generation_request(
            request.model, request, images, video_bytes, video_media_id
        )
        if video_bytes is not None or video_media_id is not None:
            _validate_video_model(model, video_bytes is not None)
        _validate_uploaded_edit_trim(video_bytes, video_mime_type, options)
        initial_image_count = len(images)
        images = await _append_openai_reference_images(model, request.messages, images)
        if len(images) != initial_image_count:
            model, public_model, options = _resolve_generation_request(
                request.model, request, images, video_bytes, video_media_id
            )
        return NormalizedGenerationRequest(
            model=model,
            prompt=prompt,
            images=images,
            messages=request.messages,
            video_media_id=video_media_id,
            video_bytes=video_bytes,
            video_mime_type=video_mime_type,
            public_model=public_model,
            generation_options=options,
        )

    if request.contents:
        gemini_request = GeminiGenerateContentRequest.model_validate(
            {
                **request.model_dump(exclude_none=True),
                "contents": _coerce_gemini_contents(request.contents),
            }
        )
        normalized = await _normalize_gemini_request(
            request.model, gemini_request, clamp_default_edit_trim=clamp_default_edit_trim
        )
        normalized.messages = request.messages
        return normalized

    raise HTTPException(status_code=400, detail="Messages or contents cannot be empty")


async def _normalize_gemini_request(
    model: str,
    request: GeminiGenerateContentRequest,
    *,
    clamp_default_edit_trim: bool = False,
) -> NormalizedGenerationRequest:
    (
        prompt,
        images,
        video_media_id,
        video_bytes,
        video_mime_type,
    ) = await _extract_prompt_and_images_from_gemini_contents(request.contents)
    if clamp_default_edit_trim:
        _clamp_default_uploaded_edit(request, video_bytes, video_mime_type)
    resolved_model, public_model, options = _resolve_generation_request(
        model, request, images, video_bytes, video_media_id
    )
    if video_bytes is not None or video_media_id is not None:
        _validate_video_model(resolved_model, video_bytes is not None)
    _validate_uploaded_edit_trim(video_bytes, video_mime_type, options)
    system_instruction = _extract_text_from_gemini_content(request.systemInstruction)
    model_config = MODEL_CONFIG.get(resolved_model)
    media_model = bool(model_config and model_config.get("type") in {"image", "video"})

    if media_model:
        prompt = _sanitize_media_prompt(prompt)

    if system_instruction:
        if media_model and _should_ignore_media_system_instruction(system_instruction):
            debug_logger.log_warning(
                f"[GEMINI] 忽略媒体模型的 systemInstruction: model={resolved_model}, len={len(system_instruction)}"
            )
        else:
            if media_model:
                system_instruction = _sanitize_media_prompt(system_instruction)
            prompt = f"{system_instruction}\n\n{prompt}".strip()

    return NormalizedGenerationRequest(
        model=resolved_model,
        prompt=prompt,
        images=images,
        video_media_id=video_media_id,
        video_bytes=video_bytes,
        video_mime_type=video_mime_type,
        public_model=public_model,
        generation_options=options,
    )


async def _collect_non_stream_result(
    model: str,
    prompt: str,
    images: List[bytes],
    base_url_override: Optional[str] = None,
    video_media_id: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
    video_mime_type: str = "video/mp4",
    generation_options: Optional[Dict[str, Any]] = None,
    response_model: Optional[str] = None,
) -> str:
    handler = _ensure_generation_handler()
    result = None
    async for chunk in handler.handle_generation(
        model=model,
        prompt=prompt,
        images=images if images else None,
        stream=False,
        base_url_override=base_url_override,
        video_media_id=video_media_id,
        video_bytes=video_bytes,
        video_mime_type=video_mime_type,
        generation_options=generation_options,
        response_model=response_model,
    ):
        result = chunk

    if result is None:
        raise HTTPException(status_code=500, detail="Generation failed: No response")

    if response_model:
        payload = _parse_handler_result(result)
        if "error" not in payload:
            payload["model"] = response_model
        return json.dumps(payload, ensure_ascii=False)
    return result


def _parse_handler_result(result: str) -> Dict[str, Any]:
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return {"result": result}


def _get_error_status_code(payload: Dict[str, Any]) -> int:
    error = payload.get("error")
    if isinstance(error, dict):
        status_code = error.get("status_code")
        if isinstance(status_code, int):
            return status_code
        if isinstance(status_code, str) and status_code.isdigit():
            return int(status_code)
        return 400
    return 200


def _build_openai_json_response(payload: Dict[str, Any]) -> JSONResponse:
    if isinstance(payload.get("error"), dict):
        payload["error"]["message"] = safe_error(
            payload["error"].get("message", "Generation failed")
        )
        payload["error"].setdefault("param", None)
    return JSONResponse(content=payload, status_code=_get_error_status_code(payload))


def _build_gemini_error_payload(status_code: int, message: str) -> Dict[str, Any]:
    return {
        "error": {
            "code": status_code,
            "message": message,
            "status": GEMINI_STATUS_MAP.get(status_code, "UNKNOWN"),
        }
    }


def _build_gemini_error_response_from_handler(payload: Dict[str, Any]) -> JSONResponse:
    error = payload.get("error", {})
    status_code = _get_error_status_code(payload)
    message = error.get("message", "Generation failed")
    return JSONResponse(
        status_code=status_code,
        content=_build_gemini_error_payload(status_code, message),
    )


def _extract_openai_message_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return payload.get("result", "")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def _extract_url_from_openai_payload(payload: Dict[str, Any]) -> Optional[str]:
    direct_url = payload.get("url")
    if isinstance(direct_url, str) and direct_url.strip():
        return direct_url.strip()

    content = _extract_openai_message_content(payload).strip()
    if not content:
        return None

    image_match = MARKDOWN_IMAGE_RE.search(content)
    if image_match:
        return image_match.group(1).strip()

    video_match = HTML_VIDEO_RE.search(content)
    if video_match:
        return video_match.group(1).strip()

    return None


def _enrich_payload_with_direct_url(payload: Dict[str, Any]) -> Dict[str, Any]:
    extracted_url = _extract_url_from_openai_payload(payload)
    if extracted_url and not payload.get("url"):
        payload["url"] = extracted_url
    return payload


async def _build_image_parts_from_uri(uri: str) -> List[Dict[str, Any]]:
    if uri.startswith("data:image"):
        mime_type, _ = _decode_data_url(uri)
        match = DATA_URL_RE.match(uri)
        if match:
            return [{"inlineData": {"mimeType": mime_type, "data": match.group("data")}}]

    image_bytes = await retrieve_image_data(uri)
    if image_bytes:
        mime_type = _detect_image_mime_type(
            image_bytes,
            fallback=_guess_mime_type(uri, "image/png"),
        )
        return [
            {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            }
        ]

    return [
        {
            "fileData": {
                "mimeType": _guess_mime_type(uri, "image/png"),
                "fileUri": uri,
            }
        },
        {"text": uri},
    ]


def _build_video_parts_from_uri(uri: str) -> List[Dict[str, Any]]:
    return [
        {
            "fileData": {
                "mimeType": _guess_mime_type(uri, "video/mp4"),
                "fileUri": uri,
            }
        }
    ]


async def _build_gemini_parts_from_output(output: str) -> List[Dict[str, Any]]:
    if not output:
        return []

    image_matches = MARKDOWN_IMAGE_RE.findall(output)
    if image_matches:
        parts: List[Dict[str, Any]] = []
        for uri in image_matches:
            parts.extend(await _build_image_parts_from_uri(uri))
        return parts

    video_matches = HTML_VIDEO_RE.findall(output)
    if video_matches:
        parts: List[Dict[str, Any]] = []
        for uri in video_matches:
            parts.extend(_build_video_parts_from_uri(uri))
        return parts

    return [{"text": output}]


async def _build_gemini_success_payload(
    payload: Dict[str, Any],
    response_model: str,
) -> Dict[str, Any]:
    output = _extract_openai_message_content(payload)
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": await _build_gemini_parts_from_output(output),
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "modelVersion": response_model,
    }


def _normalize_finish_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    mapping = {
        "stop": "STOP",
        "length": "MAX_TOKENS",
        "content_filter": "SAFETY",
    }
    return mapping.get(reason, "STOP")


async def _convert_openai_stream_chunk_to_gemini_event(
    payload: Dict[str, Any],
    response_model: str,
) -> Optional[str]:
    choices = payload.get("choices", [])
    if not choices:
        return None

    choice = choices[0]
    delta = choice.get("delta", {})
    text = delta.get("reasoning_content") or delta.get("content") or ""
    finish_reason = _normalize_finish_reason(choice.get("finish_reason"))

    candidate: Dict[str, Any] = {"index": choice.get("index", 0)}
    if text:
        candidate["content"] = {
            "role": "model",
            "parts": await _build_gemini_parts_from_output(text),
        }
    if finish_reason:
        candidate["finishReason"] = finish_reason

    if len(candidate) == 1:
        return None

    chunk = {
        "candidates": [candidate],
        "modelVersion": response_model,
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _iterate_openai_stream(
    normalized: NormalizedGenerationRequest,
    base_url_override: Optional[str] = None,
):
    handler = _ensure_generation_handler()
    stream_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=True,
        base_url_override=base_url_override,
        video_media_id=normalized.video_media_id,
        video_bytes=normalized.video_bytes,
        video_mime_type=normalized.video_mime_type,
        generation_options=normalized.generation_options,
        response_model=normalized.public_model,
    ):
        payload = _parse_handler_result(chunk[6:].strip() if chunk.startswith("data: ") else chunk)
        if "error" not in payload:
            payload.update(
                id=stream_id, created=created, model=normalized.public_model or normalized.model
            )
        elif isinstance(payload["error"], dict):
            payload["error"]["message"] = safe_error(
                payload["error"].get("message", "Generation failed")
            )
            payload["error"].setdefault("param", None)
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


async def _iterate_gemini_stream(
    normalized: NormalizedGenerationRequest,
    response_model: str,
    base_url_override: Optional[str] = None,
):
    handler = _ensure_generation_handler()
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=True,
        base_url_override=base_url_override,
        video_media_id=normalized.video_media_id,
        video_bytes=normalized.video_bytes,
        video_mime_type=normalized.video_mime_type,
        generation_options=normalized.generation_options,
        response_model=normalized.public_model,
    ):
        if chunk.startswith("data: "):
            payload_text = chunk[6:].strip()
            if payload_text == "[DONE]":
                continue
            payload = _parse_handler_result(payload_text)
            if "error" in payload:
                yield (
                    f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"
                )
                return

            event = await _convert_openai_stream_chunk_to_gemini_event(
                payload,
                response_model,
            )
            if event:
                yield event
            continue

        payload = _parse_handler_result(chunk)
        if "error" in payload:
            yield (
                f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"
            )
            return

        event = await _convert_openai_stream_chunk_to_gemini_event(
            payload,
            response_model,
        )
        if event:
            yield event


@router.get("/v1/models")
async def list_models(
    api_key: str = Depends(verify_openai_api_key),
    include_legacy: bool = False,
    raw_request: Request = None,
):
    """List available models."""
    models = [
        {
            **model,
            "object": "model",
            "created": 0,
            "owned_by": "lay4flow",
            "provider": "flow",
            "input_modalities": ["text", "image"]
            + (["video"] if model["id"] == "omni_1.1_flash" else []),
            "output_modalities": [model.get("type", "image")],
            "supported_endpoints": ["/v1/chat/completions", "/v1/responses"]
            + (
                ["/v1/images/generations", "/v1/images/edits"]
                if model.get("type") == "image"
                else ["/v1/videos"]
            ),
            "description": model["description"],
        }
        for model in _get_openai_model_catalog(include_legacy)
    ]

    return {"object": "list", "data": models + _get_model_gateway(raw_request).models()}


@router.get("/v1/models/aliases")
async def list_model_aliases(
    api_key: str = Depends(verify_openai_api_key), include_legacy: bool = False
):
    """List simplified model aliases for generationConfig-based resolution."""
    aliases = {
        entry["id"]: entry["description"] for entry in _get_openai_model_catalog(include_legacy)
    }
    alias_models = []
    for alias_id, description in aliases.items():
        alias_models.append(
            {
                "id": alias_id,
                "object": "model",
                "owned_by": "lay4flow",
                "description": description,
                "is_alias": True,
            }
        )
    return {"object": "list", "data": alias_models}


@router.get("/v1beta/models")
@router.get("/models")
async def list_gemini_models(
    api_key: str = Depends(verify_api_key_flexible), include_legacy: bool = False
):
    """List available models using Gemini-compatible response shape."""
    catalog = _get_gemini_model_catalog(include_legacy)
    return {
        "models": [
            _build_gemini_model_resource(model_id, description)
            for model_id, description in catalog.items()
        ]
    }


@router.get("/v1/models/{model}")
async def get_openai_model(
    model: str,
    api_key: str = Depends(verify_openai_api_key),
    raw_request: Request = None,
):
    catalog = await list_models(api_key, include_legacy=True, raw_request=raw_request)
    for entry in catalog["data"]:
        if entry["id"] == model:
            return entry
    raise HTTPException(404, "Model not found")


@router.get("/v1beta/models/{model}")
@router.get("/models/{model}")
async def get_gemini_model(model: str, api_key: str = Depends(verify_api_key_flexible)):
    """Return a single model using Gemini-compatible response shape."""
    catalog = _get_gemini_model_catalog(include_legacy=True)
    description = catalog.get(model)
    if not description:
        return JSONResponse(
            status_code=404,
            content=_build_gemini_error_payload(404, f"Model not found: {model}"),
        )

    return _build_gemini_model_resource(model, description)


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    raw_request: Request,
    api_key: str = Depends(verify_openai_api_key),
):
    """OpenAI-compatible unified generation endpoint."""
    try:
        provider = _get_model_gateway(raw_request).provider(request.model)
        if provider is not None:
            # Preserve complete conversation/tool fields for actual text providers.
            body = request.model_dump(exclude_none=True)
            if not request.stream:
                return _build_openai_json_response(await provider.complete(body))

            async def provider_stream():
                stream_id, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
                async for item in provider.stream(body):
                    if "error" not in item:
                        item = {**item, "id": stream_id, "created": created, "model": request.model}
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                provider_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        normalized = await _normalize_openai_request(request)
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = _get_request_base_url(raw_request)

        if request.stream:
            return StreamingResponse(
                _iterate_openai_stream(normalized, request_base_url),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        payload = _parse_handler_result(
            await _collect_non_stream_result(
                normalized.model,
                normalized.prompt,
                normalized.images,
                base_url_override=request_base_url,
                video_media_id=normalized.video_media_id,
                video_bytes=normalized.video_bytes,
                video_mime_type=normalized.video_mime_type,
                generation_options=normalized.generation_options,
                response_model=normalized.public_model,
            )
        )
        return _build_openai_json_response(payload)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/v1beta/models/{model}:generateContent")
@router.post("/models/{model}:generateContent")
async def generate_content(
    model: str,
    request: GeminiGenerateContentRequest,
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    """Gemini official generateContent endpoint."""
    try:
        normalized = await _normalize_gemini_request(model, request)
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = _get_request_base_url(raw_request)

        payload = _enrich_payload_with_direct_url(
            _parse_handler_result(
                await _collect_non_stream_result(
                    normalized.model,
                    normalized.prompt,
                    normalized.images,
                    base_url_override=request_base_url,
                    video_media_id=normalized.video_media_id,
                    video_bytes=normalized.video_bytes,
                    video_mime_type=normalized.video_mime_type,
                    generation_options=normalized.generation_options,
                    response_model=normalized.public_model,
                )
            )
        )
        if "error" in payload:
            return _build_gemini_error_response_from_handler(payload)

        return JSONResponse(
            content=await _build_gemini_success_payload(
                payload, normalized.public_model or normalized.model
            )
        )

    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=_build_gemini_error_payload(exc.status_code, str(exc.detail)),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=_build_gemini_error_payload(500, str(exc)),
        )


@router.post("/v1beta/models/{model}:streamGenerateContent")
@router.post("/models/{model}:streamGenerateContent")
async def stream_generate_content(
    model: str,
    request: GeminiGenerateContentRequest,
    raw_request: Request,
    alt: Optional[str] = Query(None),
    api_key: str = Depends(verify_api_key_flexible),
):
    """Gemini official streamGenerateContent endpoint."""
    try:
        normalized = await _normalize_gemini_request(model, request)
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = _get_request_base_url(raw_request)

        return StreamingResponse(
            _iterate_gemini_stream(
                normalized, normalized.public_model or normalized.model, request_base_url
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=_build_gemini_error_payload(exc.status_code, str(exc.detail)),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=_build_gemini_error_payload(500, str(exc)),
        )
