"""Durable Studio media, independent of the public API's temporary file cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import weakref
from pathlib import Path
from urllib.parse import urlsplit

MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".avif",
    ".bmp",
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
    ".mkv",
}
TEMP_NAME = re.compile(
    r"[a-f0-9]{32}(?:_[24]K)?\.(?:jpg|jpeg|png|webp|gif|avif|bmp|mp4|webm|mov|m4v|mkv)"
)
ARCHIVE_ID = re.compile(r"[a-f0-9]{64}\.(?:jpg|jpeg|png|webp|gif|avif|bmp|mp4|webm|mov|m4v|mkv)")


class StudioMediaError(Exception):
    """A public, credential-free archive failure."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def temporary_media_name(value: str) -> str | None:
    """Recognize only the application's generated hash filenames, never paths."""
    try:
        parsed = urlsplit(value)
    except (ValueError, TypeError):
        return None
    if parsed.scheme not in {"", "http", "https"} or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment or not parsed.path.startswith("/tmp/"):
        return None
    name = parsed.path.removeprefix("/tmp/")
    return name if TEMP_NAME.fullmatch(name) else None


def canonical_source(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise StudioMediaError(400, "作品地址格式无效")
    name = temporary_media_name(value)
    if name:
        return "/tmp/" + name
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise StudioMediaError(400, "作品地址格式无效") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise StudioMediaError(400, "作品地址格式无效")
    return value


def _result_urls(payload: dict) -> list[str]:
    assets = payload.get("generated_assets") or {}
    if not isinstance(assets, dict):
        return []
    upscaled = assets.get("upscaled_image") or {}
    if not isinstance(upscaled, dict):
        upscaled = {}
    return [
        value
        for value in (
            payload.get("url"),
            assets.get("final_image_url"),
            assets.get("final_video_url"),
            upscaled.get("local_url"),
            upscaled.get("url"),
        )
        if isinstance(value, str)
    ]


def _same_source(left: str, right: str) -> bool:
    try:
        return canonical_source(left) == canonical_source(right)
    except StudioMediaError:
        return False


class StudioMediaArchive:
    """Archive local results or recover an exact successful logged Flow result."""

    def __init__(self, handler, root: Path | None = None):
        self.handler = handler
        self.root = (
            Path(root)
            if root is not None
            else Path(handler.db.db_path).resolve().parent / "studio-media"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = weakref.WeakValueDictionary()

    def _local_file(self, name: str) -> Path:
        directory = Path(self.handler.file_cache.cache_dir).resolve()
        path = directory / name
        if path.is_symlink() or path.resolve().parent != directory:
            raise StudioMediaError(400, "作品文件路径无效")
        return path

    async def _logged_result(self, source: str):
        logs = await self.handler.db.get_logs(limit=5000, include_payload=True)
        for row in logs:
            if row.get("status_code") != 200:
                continue
            try:
                payload = row.get("response_body") or {}
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, dict) or payload.get("status") not in {
                    "success",
                    "completed",
                }:
                    continue
                if any(_same_source(source, value) for value in _result_urls(payload)):
                    return row, payload
            except (ValueError, TypeError):
                continue
        raise StudioMediaError(404, "未找到与该作品匹配的成功生成记录")

    async def _recover(self, source: str) -> tuple[Path, str]:
        row, payload = await self._logged_result(source)
        assets = payload.get("generated_assets") or {}
        kind = assets.get("type")
        if kind not in {"image", "video"}:
            raise StudioMediaError(409, "原记录缺少可恢复的媒体信息")
        token = await self.handler.db.get_token(row.get("token_id"))
        if not token or not getattr(token, "st", None):
            raise StudioMediaError(409, "原生成账号已移除或会话未配置，请先恢复该账号")
        try:
            # Resolve only the original logged account/media. Recovery never
            # invokes generation, switches accounts, or accepts a client URL.
            if kind == "video":
                media_name = assets.get("mediaName") or assets.get("mediaGenerationId")
                if not isinstance(media_name, str) or not media_name:
                    raise StudioMediaError(409, "原视频记录缺少媒体标识")
                manager = getattr(self.handler, "token_manager", None)
                proxy_url = (
                    await manager.resolve_account_proxy(token)
                    if manager and hasattr(manager, "resolve_account_proxy")
                    else getattr(token, "proxy_url", None)
                )
                with self.handler._account_proxy_context(token, proxy_url):
                    remote = await self.handler.flow_client.get_media_url_redirect(
                        token.st, media_name, media_url_type="MEDIA_URL_TYPE_FULL_MEDIA"
                    )
            else:
                remote = assets.get("origin_image_url")
                if not isinstance(remote, str) or not remote:
                    # Cache-disabled generation may have returned its exact
                    # signed origin directly rather than a /tmp link.
                    remote = source if not temporary_media_name(source) else None
            if not remote:
                raise StudioMediaError(410, "原作品下载地址已失效，请保留记录并恢复原生成账号")
            normalized = canonical_source(remote)
            if temporary_media_name(normalized):
                raise StudioMediaError(410, "原作品源文件已失效")
            filename = await self.handler.file_cache.download_and_cache(remote, kind)
            if not TEMP_NAME.fullmatch(filename):
                raise StudioMediaError(502, "原成品下载返回了无效文件")
            path = self._local_file(filename)
            if not path.is_file() or path.stat().st_size == 0:
                raise StudioMediaError(502, "原成品下载为空")
            return path, kind
        except StudioMediaError:
            raise
        except Exception:
            # Download errors can contain signed URLs and credentials.
            raise StudioMediaError(502, "原成品恢复失败，请检查原账号会话与网络后重试") from None

    def _destination(self, source: str, suffix: str) -> Path:
        if suffix.lower() not in MEDIA_EXTENSIONS:
            raise StudioMediaError(400, "作品文件类型暂不支持")
        identifier = hashlib.sha256(source.encode("utf-8")).hexdigest() + suffix.lower()
        return self.root / identifier

    @staticmethod
    def _copy_atomic(source: Path, destination: Path):
        fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as original:
                shutil.copyfileobj(original, target)
                target.flush()
                os.fsync(target.fileno())
            Path(temporary).replace(destination)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def archive(self, url: str, *, local_only: bool = False) -> dict:
        source = canonical_source(url)
        name = temporary_media_name(source)
        suffix = Path(name or urlsplit(source).path).suffix.lower()
        lock = self._locks.setdefault(source, asyncio.Lock())
        async with lock:
            if suffix in MEDIA_EXTENSIONS:
                destination = self._destination(source, suffix)
                if (
                    destination.is_file()
                    and not destination.is_symlink()
                    and destination.stat().st_size
                ):
                    return self.result(destination)
            path = self._local_file(name) if name else None
            if path is None or not path.is_file() or not path.stat().st_size:
                if local_only:
                    raise StudioMediaError(404, "临时作品尚未落盘")
                path, _kind = await self._recover(source)
            destination = self._destination(source, path.suffix)
            if not destination.is_file():
                try:
                    await asyncio.to_thread(self._copy_atomic, path, destination)
                except OSError:
                    raise StudioMediaError(
                        507, "作品长期保存失败，请检查数据盘空间与权限"
                    ) from None
            return self.result(destination)

    def content(self, identifier: str) -> tuple[Path, str]:
        if not ARCHIVE_ID.fullmatch(identifier):
            raise StudioMediaError(404, "作品不存在")
        path = self.root / identifier
        if path.is_symlink() or path.resolve().parent != self.root.resolve() or not path.is_file():
            raise StudioMediaError(404, "作品不存在")
        return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    @staticmethod
    def result(path: Path) -> dict:
        return {
            "id": path.name,
            "url": f"/api/admin/studio/media/{path.name}/content",
            "kind": "video"
            if path.suffix.lower() in {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
            else "image",
            "cached": True,
        }
