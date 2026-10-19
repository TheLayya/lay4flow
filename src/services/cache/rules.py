"""Pure file-cache naming, headers, extension, and error rules."""

import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def guess_extension(url: str, media_type: str) -> str:
    """Keep a safe media extension, falling back to the historical default."""
    path = urlparse(url).path or ""
    guessed, _ = mimetypes.guess_type(path)
    suffix = Path(path).suffix.lower()

    if media_type == "video":
        if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
            return suffix
        if guessed == "video/webm":
            return ".webm"
        if guessed == "video/quicktime":
            return ".mov"
        return ".mp4"

    if media_type == "image":
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}:
            return suffix
        if guessed == "image/png":
            return ".png"
        if guessed == "image/webp":
            return ".webp"
        if guessed == "image/gif":
            return ".gif"
        if guessed == "image/avif":
            return ".avif"
        if guessed == "image/bmp":
            return ".bmp"
        return ".jpg"

    return suffix


def build_download_headers(
    media_type: str, fingerprint: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """Build media download headers while preserving the current fingerprint."""
    headers = {
        "Accept": (
            "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            if media_type == "image"
            else "*/*"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://labs.google/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "image" if media_type == "image" else "video",
    }
    if isinstance(fingerprint, dict):
        for source, target in (
            ("user_agent", "User-Agent"),
            ("accept_language", "Accept-Language"),
            ("sec_ch_ua", "sec-ch-ua"),
            ("sec_ch_ua_mobile", "sec-ch-ua-mobile"),
            ("sec_ch_ua_platform", "sec-ch-ua-platform"),
        ):
            if fingerprint.get(source):
                headers[target] = str(fingerprint[source])
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    return headers


def generate_cache_filename(url: str, media_type: str) -> str:
    return f"{hashlib.md5(url.encode()).hexdigest()}{guess_extension(url, media_type)}"


def normalize_cache_error(error: Exception) -> str:
    """Hide command details while retaining the historical user-facing message."""
    if isinstance(error, FileNotFoundError):
        missing_name = Path(getattr(error, "filename", "") or "curl").name or "curl"
        return f"本机未安装 {missing_name}"
    message = str(error or "").strip()
    if not message:
        return "未知错误"
    if message.startswith("Failed to cache file:"):
        message = message.split(":", 1)[1].strip() or "未知错误"
    return message
