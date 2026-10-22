"""Generation handler for lay4flow"""

import asyncio
import json
import time
from contextlib import aclosing, nullcontext
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from ..core.account_tiers import (
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from ..core.config import config
from ..core.logger import debug_logger

# Keep historical imports working while the model catalog lives in core.
from ..core.model_catalog import (
    MODEL_CONFIG as MODEL_CONFIG,
    _apply_omni_model_updates as _apply_omni_model_updates,
    _apply_veo_3_1_model_updates as _apply_veo_3_1_model_updates,
    _make_i2v_config as _make_i2v_config,
    _make_t2v_config as _make_t2v_config,
)
from ..core.models import RequestLog, Task
from ..core.monitoring import record_generation_result
from .file_cache import FileCache
from .flow import media as media_protocol
from .flow_client import validate_video_trim
from .proxy_manager import AccountProxyUnavailable, ProxyManager


def _known_video_model_keys() -> set[str]:
    return {
        cfg["model_key"]
        for cfg in MODEL_CONFIG.values()
        if cfg.get("type") == "video" and cfg.get("model_key")
    }


def _resolve_tier_two_model_key(model_key: str) -> str:
    """Only upgrade to an ultra key when that exact upstream key is known valid."""
    if "ultra" in model_key:
        return model_key
    if "_fl" in model_key:
        candidate = model_key.replace("_fl", "_ultra_fl")
    else:
        candidate = model_key + "_ultra"
    return candidate if candidate in _known_video_model_keys() else model_key


class GenerationHandler:
    """统一生成处理器"""

    def __init__(
        self, flow_client, token_manager, load_balancer, db, concurrency_manager, proxy_manager
    ):
        cache_dir = Path(__file__).resolve().parents[2] / "tmp"
        self.flow_client = flow_client
        self.token_manager = token_manager
        self.load_balancer = load_balancer
        self.db = db
        self.concurrency_manager = concurrency_manager
        self.proxy_manager = proxy_manager
        self.file_cache = FileCache(
            cache_dir=str(cache_dir),
            default_timeout=config.cache_timeout,
            proxy_manager=proxy_manager,
            flow_client=flow_client,
        )

    def _create_generation_result(self) -> Dict[str, Any]:
        """Create request-local generation status shared by the response paths."""
        return dict(success=False, error_message=None, error_emitted=False)

    def _account_proxy_context(self, token, proxy_url=None):
        if hasattr(self.flow_client, "account_request_proxy"):
            return self.flow_client.account_request_proxy(
                proxy_url if proxy_url is not None else getattr(token, "proxy_url", None)
            )
        return nullcontext()

    def _account_token_context(self, token):
        if hasattr(self.flow_client, "account_request_token"):
            return self.flow_client.account_request_token(token)
        return nullcontext()

    async def _account_generation_pipeline(self, pipeline, token, proxy_url=None):
        """Scope each advance/close, so proxy ContextVar tokens never span a yield."""
        try:
            while True:
                try:
                    with (
                        self._account_proxy_context(token, proxy_url),
                        self._account_token_context(token),
                    ):
                        chunk = await anext(pipeline)
                except StopAsyncIteration:
                    return
                yield chunk
        finally:
            with self._account_proxy_context(token, proxy_url), self._account_token_context(token):
                await pipeline.aclose()

    def _create_response_state(self) -> Dict[str, Any]:
        """为单次请求创建独立的响应状态，避免并发请求互相污染。"""
        return {
            "url": None,
            "generated_assets": None,
            "base_url": None,
        }

    def _mark_generation_failed(
        self, generation_result: Optional[Dict[str, Any]], error_message: str
    ):
        """Record a failure after its error response has been emitted."""
        if isinstance(generation_result, dict):
            generation_result["success"] = False
            generation_result["error_message"] = error_message
            generation_result["error_emitted"] = True

    def _mark_generation_succeeded(self, generation_result: Optional[Dict[str, Any]]):
        """Mark the result successful and clear any previous error state."""
        if isinstance(generation_result, dict):
            generation_result["success"] = True
            generation_result["error_message"] = None
            generation_result["error_emitted"] = False

    async def _resolve_video_asset(
        self,
        token,
        operation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """按当前上游逻辑解析视频资产：状态由 media 决定，URL 通过 redirect 二段获取。"""
        metadata = (operation.get("operation") or {}).get("metadata", {}) or {}
        video_info = metadata.get("video", {}) if isinstance(metadata.get("video"), dict) else {}
        media_name = (
            operation.get("mediaName")
            or video_info.get("mediaName")
            or video_info.get("mediaGenerationId")
            or operation.get("name")
            or (operation.get("operation") or {}).get("name")
        )

        video_url = str(video_info.get("fifeUrl") or video_info.get("embeddedUrl") or "")
        if not video_url and media_name and getattr(token, "st", None):
            video_url = (
                await self.flow_client.get_media_url_redirect(
                    token.st,
                    media_name,
                    media_url_type="MEDIA_URL_TYPE_FULL_MEDIA",
                )
                or ""
            )

        import re as _re

        uuid_match = _re.search(r"/video/([0-9a-f-]{36})", video_url or "")
        video_media_id = (
            uuid_match.group(1)
            if uuid_match
            else str(media_name or video_info.get("mediaGenerationId") or "")
        )

        return {
            "media_name": media_name,
            "video_url": video_url,
            "video_media_id": video_media_id,
            "aspect_ratio": video_info.get("aspectRatio", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
            "model": video_info.get("model"),
            "duration": video_info.get("duration"),
            "metadata": metadata,
            "video_info": video_info,
        }

    def _normalize_error_message(self, error_message: Any, max_length: int = 1000) -> str:
        """归一化错误文本，避免写入超长内容。"""
        text = str(error_message or "").strip() or "未知错误"
        if len(text) <= max_length:
            return text
        return f"{text[: max_length - 3]}..."

    def _resolve_video_model_key_for_tier(
        self, model_config: Dict[str, Any], user_tier: str
    ) -> tuple[str, Optional[str]]:
        """根据账号层级调整视频模型 key。"""
        model_key = model_config["model_key"]
        allow_tier_upgrade = bool(model_config.get("allow_tier_upgrade", True))

        if user_tier == "PAYGATE_TIER_TWO":
            if allow_tier_upgrade and "ultra" not in model_key:
                upgraded_model_key = _resolve_tier_two_model_key(model_key)
                if upgraded_model_key != model_key:
                    return (
                        upgraded_model_key,
                        f"TIER_TWO 账号自动切换到 ultra 模型: {upgraded_model_key}",
                    )
            return model_key, None

        if user_tier == "PAYGATE_TIER_ONE" and "ultra" in model_key:
            model_key = model_key.replace("_ultra_fl", "_fl").replace("_ultra", "")
            return model_key, f"TIER_ONE 账号自动切换到标准模型: {model_key}"

        return model_key, None

    async def _fail_video_task(
        self, operations: Optional[List[Dict[str, Any]]], error_message: str
    ):
        """将视频任务收口到失败态，避免残留 processing。"""
        if not operations:
            return

        operation = operations[0] if operations else {}
        task_id = (operation.get("operation") or {}).get("name")
        if not task_id:
            return

        try:
            await self.db.update_task(
                task_id,
                status="failed",
                error_message=self._normalize_error_message(error_message),
                completed_at=time.time(),
            )
        except Exception as exc:
            debug_logger.log_error(f"[VIDEO] 更新任务失败状态失败: {exc}")

    async def check_token_availability(self, is_image: bool, is_video: bool) -> bool:
        """检查Token可用性

        Args:
            is_image: 是否检查图片生成Token
            is_video: 是否检查视频生成Token

        Returns:
            True表示有可用Token, False表示无可用Token
        """
        token_obj = await self.load_balancer.select_token(
            for_image_generation=is_image, for_video_generation=is_video
        )
        return token_obj is not None

    @staticmethod
    def _validate_generation_options(
        model_config: Dict[str, Any],
        generation_options: Optional[Dict[str, Any]],
        video_bytes: Optional[bytes],
    ) -> None:
        if generation_options is None:
            return
        if not isinstance(generation_options, dict):
            raise ValueError("generation_options must be an object")
        if not {"trimStartMs", "trimEndMs"}.intersection(generation_options):
            return
        if model_config.get("video_type") != "edit":
            raise ValueError("Video trim is only supported for video editing")
        validate_video_trim(
            generation_options.get("trimStartMs", 0),
            generation_options.get("trimEndMs", 10000),
        )
        if not video_bytes:
            raise ValueError(
                "Video trim requires a video upload; an existing media ID cannot be trimmed"
            )

    async def handle_generation(
        self,
        model: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        stream: bool = False,
        base_url_override: Optional[str] = None,
        video_media_id: Optional[str] = None,
        video_bytes: Optional[bytes] = None,
        video_mime_type: str = "video/mp4",
        generation_options: Optional[Dict[str, Any]] = None,
        response_model: Optional[str] = None,
    ) -> AsyncGenerator:
        """统一生成入口

        Args:
            model: 模型名称
            prompt: 提示词
            images: 图片列表 (bytes格式)
            stream: 是否流式输出
        """
        start_time = time.time()
        public_model = response_model or model
        token = None
        generation_type = None
        pending_token_state = {"active": False}
        slot_acquired = False
        submission_lease = None
        submission_error = None
        request_id = f"gen-{int(start_time * 1000)}-{id(asyncio.current_task())}"
        perf_trace: Dict[str, Any] = {
            "request_id": request_id,
            "model": public_model,
            "resolved_model": model,
            "status": "processing",
        }
        generation_result = self._create_generation_result()
        response_state = self._create_response_state()
        response_state["base_url"] = (base_url_override or "").strip().rstrip("/") or None
        request_log_state: Dict[str, Any] = {"id": None, "progress": 0}

        # 防止并发链路复用到上一次请求的指纹上下文
        if hasattr(self.flow_client, "clear_request_fingerprint"):
            self.flow_client.clear_request_fingerprint()

        # 1. 验证模型
        if model not in MODEL_CONFIG:
            error_msg = f"不支持的模型: {model}"
            debug_logger.log_error(error_msg)
            record_generation_result("unknown", "invalid", time.time() - start_time)
            yield self._create_error_response(error_msg, status_code=400)
            return

        model_config = MODEL_CONFIG[model]
        generation_type = model_config["type"]
        video_type_for_op = model_config.get("video_type", "")
        try:
            self._validate_generation_options(model_config, generation_options, video_bytes)
        except ValueError as exc:
            record_generation_result(generation_type, "invalid", time.time() - start_time)
            yield self._create_error_response(str(exc), status_code=400)
            return
        request_operation = (
            "extend_video" if video_type_for_op == "extend" else f"generate_{generation_type}"
        )
        prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
        request_payload = {
            "model": public_model,
            "resolved_model": model,
            "prompt": prompt_for_log,
            "has_images": images is not None and len(images) > 0,
        }
        if generation_options:
            request_payload["generation_options"] = dict(generation_options)
        debug_logger.log_info(
            f"[GENERATION] 开始生成 - 模型: {public_model}, 解析模型: {model}, 类型: {generation_type}, Prompt: {prompt[:50]}..."
        )

        # 向用户展示开始信息
        if stream:
            yield self._create_stream_chunk(
                f"✨ {'视频' if generation_type == 'video' else '图片'}生成任务已启动\n",
                role="assistant",
            )
            request_log_state["id"] = await self._log_request(
                token_id=None,
                operation=request_operation,
                request_data=request_payload,
                response_data={
                    "status": "processing",
                    "status_text": "started",
                    "progress": 0,
                    "request_id": request_id,
                },
                status_code=102,
                duration=0,
                status_text="started",
                progress=0,
            )

        # 2. 选择Token
        debug_logger.log_info("[GENERATION] 正在选择可用Token...")
        token_select_started_at = time.time()

        if generation_type == "image":
            token = await self.load_balancer.select_token(
                for_image_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
            )
        else:
            token = await self.load_balancer.select_token(
                for_video_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
            )
        perf_trace["token_select_ms"] = int((time.time() - token_select_started_at) * 1000)

        if not token:
            error_msg = None
            if self.load_balancer and hasattr(self.load_balancer, "get_unavailable_reason"):
                error_msg = await self.load_balancer.get_unavailable_reason(
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                    model=model,
                )
            if not error_msg:
                error_msg = self._get_no_token_error_message(generation_type)
            debug_logger.log_error(f"[GENERATION] {error_msg}")
            record_generation_result(generation_type, "no_token", time.time() - start_time)
            await self._log_request(
                token_id=None,
                operation=request_operation,
                request_data=request_payload,
                response_data={"error": error_msg, "performance": perf_trace},
                status_code=503,
                duration=time.time() - start_time,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            if stream:
                yield self._create_stream_chunk(f"错误: {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=503)
            return

        debug_logger.log_info(f"[GENERATION] 已选择Token: {token.id} ({token.email})")
        pending_token_state["active"] = True
        selected_token_id = token.id
        concurrency_manager = getattr(self, "concurrency_manager", None)

        try:
            await self._update_request_log_progress(
                request_log_state,
                token_id=selected_token_id,
                status_text="token_selected",
                progress=8,
                response_extra={"token_email": token.email},
            )
            slot_wait_ms = 0
            limit = getattr(token, f"{generation_type}_concurrency", -1) or 0
            if concurrency_manager is not None and limit > 0:
                wait_acquire = (
                    concurrency_manager.wait_acquire_image
                    if generation_type == "image"
                    else concurrency_manager.wait_acquire_video
                )
                timeout = (
                    config.flow_image_slot_wait_timeout
                    if generation_type == "image"
                    else config.flow_video_slot_wait_timeout
                )
                slot_acquired, slot_wait_ms = await wait_acquire(selected_token_id, timeout)
                if not slot_acquired:
                    error_msg = "账号并发槽位等待超时或账号已删除，请稍后重试"
                    duration = time.time() - start_time
                    record_generation_result(generation_type, "failed", duration)
                    await self._log_request(
                        selected_token_id,
                        request_operation,
                        request_payload,
                        {"error": error_msg, "slot_wait_ms": slot_wait_ms},
                        503,
                        duration,
                        log_id=request_log_state.get("id"),
                        status_text="failed",
                        progress=request_log_state.get("progress", 0),
                    )
                    yield self._create_error_response(error_msg, status_code=503)
                    return

                # CM now counts this request; pending only covers waiting/unlimited work.
                await self.load_balancer.release_pending(
                    selected_token_id,
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                )
                pending_token_state["active"] = False
                # Waiting may outlive an admin disable/delete or credential update.
                token = await self.token_manager.get_token(selected_token_id)
                if (
                    token is None
                    or not token.is_active
                    or not getattr(token, f"{generation_type}_enabled", True)
                ):
                    error_msg = "账号已停用、删除或已关闭当前生成类型"
                    duration = time.time() - start_time
                    record_generation_result(generation_type, "failed", duration)
                    await self._log_request(
                        selected_token_id,
                        request_operation,
                        request_payload,
                        {"error": error_msg, "slot_wait_ms": slot_wait_ms},
                        503,
                        duration,
                        log_id=request_log_state.get("id"),
                        status_text="failed",
                        progress=request_log_state.get("progress", 0),
                    )
                    yield self._create_error_response(error_msg, status_code=503)
                    return
            perf_trace.setdefault(f"{generation_type}_generation", {})["slot_wait_ms"] = (
                slot_wait_ms
            )

            debug_logger.log_info("[GENERATION] 检查Token AT有效性...")
            if stream:
                yield self._create_stream_chunk("初始化生成环境...\n")

            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="token_ready",
                progress=15,
            )
            ensure_at_started_at = time.time()
            resolve_proxy = getattr(self.token_manager, "resolve_account_proxy", None)
            account_proxy = (
                await resolve_proxy(token) if resolve_proxy else getattr(token, "proxy_url", None)
            )
            token = await self.token_manager.ensure_valid_token(token)
            perf_trace["ensure_at_ms"] = int((time.time() - ensure_at_started_at) * 1000)
            if not token:
                error_msg = "Token AT无效或刷新失败"
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                if stream:
                    yield self._create_stream_chunk(f"错误: {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=503)
                return

            # 4. 确保Project存在
            debug_logger.log_info("[GENERATION] 检查/创建Project...")

            if not supports_model_for_tier(model, token.user_paygate_tier):
                required_tier = get_required_paygate_tier_for_model(model)
                error_msg = (
                    "当前模型需要 " + get_paygate_tier_label(required_tier) + " 账号: " + model
                )
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                if stream:
                    yield self._create_stream_chunk(f"错误: {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=403)
                return

            ensure_project_started_at = time.time()
            with self._account_proxy_context(token, account_proxy):
                project_id = await self.token_manager.ensure_project_exists(token.id)
            perf_trace["ensure_project_ms"] = int((time.time() - ensure_project_started_at) * 1000)
            debug_logger.log_info(f"[GENERATION] Project ID: {project_id}")
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="project_ready",
                progress=22,
                response_extra={"project_id": project_id},
            )
            prefill_action = (
                "IMAGE_GENERATION" if generation_type == "image" else "VIDEO_GENERATION"
            )
            acquire_proxy = getattr(
                getattr(self, "proxy_manager", None), "acquire_submission_proxy", None
            )
            if acquire_proxy is not None:
                submission_lease = await acquire_proxy(token)
                account_proxy = submission_lease.proxy_url
                perf_trace["submission_proxy"] = {
                    "mode": "fixed"
                    if getattr(token, "browser_profile_source", "") == "cloak"
                    else getattr(token, "submission_proxy_mode", "fixed"),
                    "pool_id": submission_lease.entry_id,
                }
            with self._account_proxy_context(token, account_proxy):
                await self.flow_client.prefill_remote_browser_pool(
                    project_id=project_id,
                    action=prefill_action,
                    token_id=token.id,
                )

            # 5. 根据类型处理
            generation_pipeline_started_at = time.time()
            if generation_type == "image":
                debug_logger.log_info("[GENERATION] 开始图片生成流程...")
                pipeline = self._handle_image_generation(
                    token,
                    project_id,
                    model_config,
                    prompt,
                    images,
                    stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state,
                )
                async with aclosing(
                    self._account_generation_pipeline(pipeline, token, account_proxy)
                ) as scoped:
                    async for chunk in scoped:
                        yield self._attach_public_media(
                            chunk, response_state, request_log_state, "image"
                        )
            else:  # video
                debug_logger.log_info("[GENERATION] 开始视频生成流程...")
                pipeline = self._handle_video_generation(
                    token,
                    project_id,
                    model_config,
                    prompt,
                    images,
                    stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state,
                    video_media_id=video_media_id,
                    video_bytes=video_bytes,
                    video_mime_type=video_mime_type,
                    generation_options=generation_options,
                    response_model=response_model,
                )
                async with aclosing(
                    self._account_generation_pipeline(pipeline, token, account_proxy)
                ) as scoped:
                    async for chunk in scoped:
                        yield self._attach_public_media(
                            chunk, response_state, request_log_state, "video"
                        )
            perf_trace["generation_pipeline_ms"] = int(
                (time.time() - generation_pipeline_started_at) * 1000
            )

            # 6. 记录使用
            if not generation_result.get("success"):
                error_msg = generation_result.get("error_message") or "生成未成功完成"
                submission_error = error_msg
                debug_logger.log_warning(f"[GENERATION] 生成未成功，不扣次数: {error_msg}")
                if token and not (
                    submission_error is not None
                    and ProxyManager.is_submission_transport_error(submission_error)
                ):
                    await self.token_manager.record_error(token.id)
                duration = time.time() - start_time
                record_generation_result(generation_type, "failed", duration)
                perf_trace["status"] = "failed"
                perf_trace["total_ms"] = int(duration * 1000)
                perf_trace["error"] = error_msg
                prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
                await self._log_request(
                    token.id if token else None,
                    request_operation,
                    request_payload,
                    {"error": error_msg, "performance": perf_trace},
                    500,
                    duration,
                    log_id=request_log_state.get("id"),
                    status_text="failed",
                    progress=request_log_state.get("progress", 0),
                )
                if not generation_result.get("error_emitted"):
                    if stream:
                        yield self._create_stream_chunk(f"错误: {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=500)
                return

            is_video = generation_type == "video"
            await self.token_manager.record_usage(token.id, is_video=is_video)

            # 重置错误计数 (请求成功时清空连续错误计数)
            await self.token_manager.record_success(token.id)

            debug_logger.log_info("[GENERATION] ✅ 生成成功完成")

            # 7. 记录成功日志
            duration = time.time() - start_time
            record_generation_result(generation_type, "success", duration)
            perf_trace["status"] = "success"
            perf_trace["total_ms"] = int(duration * 1000)
            # 日志中保留更完整的 prompt，避免管理页只看到过短内容
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"

            # 构建响应数据，包含生成的URL
            response_data = {
                "status": "success",
                "model": public_model,
                "resolved_model": model,
                "prompt": prompt_for_log,
                "performance": perf_trace,
            }

            # 添加生成的URL（如果有）
            if response_state.get("url"):
                response_data["url"] = response_state["url"]
            if response_state.get("generated_assets"):
                response_data["generated_assets"] = response_state["generated_assets"]
            if response_state.get("upscale_retry_trace"):
                response_data["upscale_retry_trace"] = response_state["upscale_retry_trace"]
            image_perf = (
                perf_trace.get("image_generation", {}) if isinstance(perf_trace, dict) else {}
            )
            video_perf = (
                perf_trace.get("video_generation", {}) if isinstance(perf_trace, dict) else {}
            )
            debug_logger.log_info(
                f"[PERF] [{request_id}] total={perf_trace.get('total_ms', 0)}ms, "
                f"select={perf_trace.get('token_select_ms', 0)}ms, "
                f"ensure_at={perf_trace.get('ensure_at_ms', 0)}ms, "
                f"project={perf_trace.get('ensure_project_ms', 0)}ms, "
                f"pipeline={perf_trace.get('generation_pipeline_ms', 0)}ms, "
                f"slot_wait={image_perf.get('slot_wait_ms', 0)}ms, "
                f"launch_queue={image_perf.get('launch_queue_wait_ms', 0)}ms, "
                f"launch_stagger={image_perf.get('launch_stagger_wait_ms', 0)}ms, "
                f"video_slot_wait={video_perf.get('slot_wait_ms', 0)}ms"
            )

            await self._log_request(
                token.id,
                request_operation,
                request_payload,
                response_data,
                200,
                duration,
                log_id=request_log_state.get("id"),
                status_text="completed",
                progress=100,
            )

        except asyncio.CancelledError:
            error_msg = "生成已取消: 客户端连接已断开"
            debug_logger.log_warning(f"[GENERATION] ⚠️ {error_msg}")
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "cancelled", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if "request_payload" in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                499,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            raise
        except Exception as e:
            submission_error = e
            error_msg = f"生成失败: {str(e)}"
            debug_logger.log_error(f"[GENERATION] 生成失败: {error_msg}")
            if token:
                if self._should_count_token_error(e):
                    await self.token_manager.record_error(token.id)
                else:
                    debug_logger.log_info(
                        f"[GENERATION] 跳过 token 错误计数: token_id={token.id}, reason={str(e)[:200]}"
                    )

            # 先将最终失败状态落库，再返回错误响应，避免日志停在 102。
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "failed", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if "request_payload" in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                500,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            retry_trace = self._safe_retry_trace(perf_trace)
            if stream:
                if retry_trace:
                    yield self._create_stream_chunk(
                        f"本项已尝试 {retry_trace['attempts']} / {retry_trace['max_attempts']} 次（包含首次）\n"
                    )
                yield self._create_stream_chunk(f"错误: {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=500, retry_trace=retry_trace)
        finally:
            try:
                if submission_lease is not None:
                    await self.proxy_manager.release_submission_proxy(
                        submission_lease,
                        success=bool(generation_result.get("success")),
                        error=submission_error,
                    )
            finally:
                try:
                    if slot_acquired:
                        if generation_type == "image":
                            await concurrency_manager.release_image(selected_token_id)
                        else:
                            await concurrency_manager.release_video(selected_token_id)
                finally:
                    if pending_token_state.get("active") and self.load_balancer:
                        await self.load_balancer.release_pending(
                            selected_token_id,
                            for_image_generation=(generation_type == "image"),
                            for_video_generation=(generation_type == "video"),
                        )
                        pending_token_state["active"] = False

    def _get_no_token_error_message(self, generation_type: str) -> str:
        """获取无可用Token时的详细错误信息"""
        if generation_type == "image":
            return "没有可用的Token进行图片生成。所有Token都处于禁用、冷却、锁定或已过期状态。"
        else:
            return "没有可用的Token进行视频生成。所有Token都处于禁用、冷却、配额耗尽或已过期状态。"

    def _should_count_token_error(self, error: Exception) -> bool:
        """判断失败是否应计入 token 连续错误。

        reCAPTCHA 获取失败、验证码供应商错误、打码资源不足等问题通常不是账号本身异常；
        若将其纳入连续错误，会在回归测试或代理波动时把 token 自动打成 inactive。
        """
        error_text = str(error or "").strip().lower()
        if isinstance(error, AccountProxyUnavailable):
            return False
        if ProxyManager.is_submission_transport_error(error):
            return False
        if not error_text:
            return True

        non_token_fault_markers = (
            "failed to obtain recaptcha token",
            "recaptcha evaluation failed",
            "recaptcha 验证失败",
            "recaptcha 错误",
            "public_error_unusual_activity",
            "too much traffic",
            "error_no_slot_available",
            "打码服务资源不足",
            "打码服务资源阻塞",
            "remote captcha",
            "remote_browser",
        )
        if any(marker in error_text for marker in non_token_fault_markers):
            return False

        if "没有可用的token进行" in error_text:
            return False

        return True

    async def _handle_image_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None,
    ) -> AsyncGenerator:
        """处理图片生成 (同步返回)"""

        if response_state is None:
            response_state = self._create_response_state()

        image_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            image_trace = perf_trace.setdefault("image_generation", {})
            image_trace["input_image_count"] = len(images) if images else 0

        # The outer generation lifecycle owns the account concurrency slot.
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if image_trace is not None:
            image_trace.setdefault("slot_wait_ms", 0)

        if images and len(images) > 0:
            await self._update_request_log_progress(
                request_log_state, token_id=token.id, status_text="uploading_images", progress=28
            )
        else:
            await self._update_request_log_progress(
                request_log_state, token_id=token.id, status_text="submitting_image", progress=28
            )

        try:
            # 上传图片 (如果有)
            upload_started_at = time.time()
            image_inputs = []
            if images and len(images) > 0:
                if stream:
                    yield self._create_stream_chunk(f"上传 {len(images)} 张参考图片...\n")

                # 支持多图输入
                for idx, image_bytes in enumerate(images):
                    media_id = await self.flow_client.upload_image(
                        token.at, image_bytes, model_config["aspect_ratio"], project_id=project_id
                    )
                    image_inputs.append(
                        {"name": media_id, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"}
                    )
                    if stream:
                        yield self._create_stream_chunk(
                            f"已上传第 {idx + 1}/{len(images)} 张图片\n"
                        )
            if image_trace is not None:
                image_trace["upload_images_ms"] = int((time.time() - upload_started_at) * 1000)

            # 调用生成API
            if stream:
                if images and len(images) > 0:
                    yield self._create_stream_chunk("参考图片上传完成，正在进行打码验证...\n")
                else:
                    yield self._create_stream_chunk("正在进行打码验证并提交图片生成请求...\n")

            async def _image_progress_callback(status_text: str, progress: int):
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text=status_text,
                    progress=progress,
                )

            generate_started_at = time.time()
            try:
                (
                    result,
                    generation_session_id,
                    upstream_trace,
                ) = await self.flow_client.generate_image(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_name=model_config["model_name"],
                    aspect_ratio=model_config["aspect_ratio"],
                    image_inputs=image_inputs,
                    token_id=token.id,
                    token_image_concurrency=token.image_concurrency,
                    progress_callback=_image_progress_callback,
                )
            except Exception as exc:
                if image_trace is not None:
                    retry_trace = getattr(exc, "flow_retry_trace", None)
                    if isinstance(retry_trace, dict):
                        image_trace["upstream_trace"] = retry_trace
                    image_trace["generate_api_ms"] = int((time.time() - generate_started_at) * 1000)
                    safe_error = getattr(self.flow_client, "_remote_safe_error", None)
                    image_trace["generation_error"] = (
                        safe_error(exc)[:240]
                        if callable(safe_error)
                        else self._normalize_error_message(exc, max_length=240)
                    )
                raise
            if image_trace is not None:
                image_trace["generate_api_ms"] = int((time.time() - generate_started_at) * 1000)
                image_trace["upstream_trace"] = upstream_trace
                attempts = (
                    upstream_trace.get("generation_attempts")
                    if isinstance(upstream_trace, dict)
                    else None
                )
                if isinstance(attempts, list) and attempts:
                    first_attempt = attempts[0] if isinstance(attempts[0], dict) else {}
                    image_trace["launch_queue_wait_ms"] = int(
                        first_attempt.get("launch_queue_ms") or 0
                    )
                    image_trace["launch_stagger_wait_ms"] = int(
                        first_attempt.get("launch_stagger_ms") or 0
                    )
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="image_generated",
                progress=72,
            )

            # 提取URL和mediaId
            media = result.get("media", []) if isinstance(result, dict) else None
            if media == []:
                self._mark_generation_failed(
                    generation_result, "\u751f\u6210\u7ed3\u679c\u4e3a\u7a7a"
                )
                yield self._create_error_response("生成结果为空", status_code=502)
                return

            image_url = media_protocol.extract_image_url_from_media(media)
            if image_url is None:
                error_message = "生成结果中的图片媒体无效"
                self._mark_generation_failed(generation_result, error_message)
                yield self._create_error_response(error_message, status_code=502)
                return
            media_id = media[0].get("name")  # 用于 upsample; validated above.
            response_state["generated_assets"] = {"type": "image", "origin_image_url": image_url}

            # 检查是否需要 upsample
            upsample_resolution = model_config.get("upsample")
            if upsample_resolution and media_id:
                upsample_started_at = time.time()
                resolution_name = "4K" if "4K" in upsample_resolution else "2K"
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text=f"upsampling_{resolution_name.lower()}",
                    progress=82,
                )
                if stream:
                    yield self._create_stream_chunk(f"正在放大图片到 {resolution_name}...\n")

                # FlowClient owns the retry budget and refreshes reCAPTCHA tokens.
                try:
                    # 调用 upsample API
                    encoded_image = await self.flow_client.upsample_image(
                        at=token.at,
                        project_id=project_id,
                        media_id=media_id,
                        target_resolution=upsample_resolution,
                        user_paygate_tier=normalized_tier,
                        session_id=generation_session_id,
                        token_id=token.id,
                    )

                    if encoded_image:
                        debug_logger.log_info(f"[UPSAMPLE] 图片已放大到 {resolution_name}")

                        if stream:
                            yield self._create_stream_chunk(f"✅ 图片已放大到 {resolution_name}\n")

                        # 2K/4K 图片统一落盘为真实文件，日志里只保留链接。
                        response_state["generated_assets"] = {
                            "type": "image",
                            "origin_image_url": image_url,
                            "upscaled_image": {"resolution": resolution_name},
                        }

                        try:
                            await self._update_request_log_progress(
                                request_log_state,
                                token_id=token.id,
                                status_text="caching_image",
                                progress=90,
                            )
                            if stream:
                                yield self._create_stream_chunk(
                                    f"缓存 {resolution_name} 图片中...\n"
                                )
                            cached_filename = await self.file_cache.cache_base64_image(
                                encoded_image, resolution_name
                            )
                            local_url = (
                                f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                            )
                            response_state["url"] = local_url
                            response_state["generated_assets"]["upscaled_image"]["local_url"] = (
                                local_url
                            )
                            response_state["generated_assets"]["upscaled_image"]["url"] = local_url
                            self._mark_generation_succeeded(generation_result)
                            if stream:
                                yield self._create_stream_chunk(
                                    f"✅ {resolution_name} 图片缓存成功\n"
                                )
                                yield self._create_stream_chunk(
                                    f"![Generated Image]({local_url})", finish_reason="stop"
                                )
                            else:
                                yield self._create_completion_response(
                                    local_url, media_type="image"
                                )
                            if image_trace is not None:
                                image_trace["upsample_ms"] = int(
                                    (time.time() - upsample_started_at) * 1000
                                )
                            return
                        except Exception as e:
                            debug_logger.log_error(
                                f"Failed to cache {resolution_name} image: {str(e)}"
                            )
                            response_state["url"] = image_url
                            response_state["generated_assets"]["upscaled_image"]["local_url"] = None
                            response_state["generated_assets"]["upscaled_image"]["url"] = image_url
                            response_state["generated_assets"]["upscaled_image"][
                                "delivery_mode"
                            ] = "inline_base64_fallback"
                            self._mark_generation_succeeded(generation_result)
                            base64_url = f"data:image/jpeg;base64,{encoded_image}"
                            response_state["url"] = base64_url
                            if stream:
                                cache_error = self._normalize_error_message(e, max_length=120)
                                yield self._create_stream_chunk(
                                    f"⚠️ 缓存失败: {cache_error}，返回内联图片...\n"
                                )
                                yield self._create_stream_chunk(
                                    f"![Generated Image]({base64_url})", finish_reason="stop"
                                )
                            else:
                                yield self._create_completion_response(
                                    base64_url, media_type="image"
                                )
                            if image_trace is not None:
                                image_trace["upsample_ms"] = int(
                                    (time.time() - upsample_started_at) * 1000
                                )
                            return
                    else:
                        debug_logger.log_warning("[UPSAMPLE] 返回结果为空")
                        response_state.setdefault("warnings", []).append(
                            {
                                "code": "upscale_failed",
                                "message": "Image upscaling failed; original returned",
                            }
                        )
                        if stream:
                            yield self._create_stream_chunk("⚠️ 放大失败，返回原图...\n")

                except Exception as e:
                    error_str = str(e)
                    debug_logger.log_error(f"[UPSAMPLE] 放大失败: {error_str}")
                    response_state.setdefault("warnings", []).append(
                        {
                            "code": "upscale_failed",
                            "message": "Image upscaling failed; original returned",
                        }
                    )
                    if stream:
                        yield self._create_stream_chunk(f"⚠️ 放大失败: {error_str}，返回原图...\n")
                if image_trace is not None:
                    image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)

            local_url = image_url
            cache_started_at = time.time()
            if config.cache_enabled:
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text="caching_image",
                    progress=90,
                )
                if stream:
                    yield self._create_stream_chunk("正在缓存 1K 图片文件...\n")
                try:
                    cached_filename = await self.file_cache.download_and_cache(image_url, "image")
                    local_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                    if stream:
                        yield self._create_stream_chunk("✅ 1K 图片缓存成功,准备返回缓存地址...\n")
                except Exception as e:
                    debug_logger.log_error(f"Failed to cache 1K image: {str(e)}")
                    local_url = image_url
                    if stream:
                        cache_error = self._normalize_error_message(e, max_length=120)
                        yield self._create_stream_chunk(
                            f"⚠️ 缓存失败: {cache_error}\n正在返回源链接...\n"
                        )
            elif stream:
                yield self._create_stream_chunk("缓存已关闭,正在返回官方图片链接...\n")
            if image_trace is not None:
                image_trace["cache_image_ms"] = int((time.time() - cache_started_at) * 1000)

            # 返回结果
            # 存储URL用于日志记录
            response_state["url"] = local_url
            response_state["generated_assets"] = {
                "type": "image",
                "origin_image_url": image_url,
                "final_image_url": local_url,
            }
            self._mark_generation_succeeded(generation_result)

            if stream:
                yield self._create_stream_chunk(
                    f"![Generated Image]({local_url})", finish_reason="stop"
                )
            else:
                yield self._create_completion_response(
                    local_url,  # 直接传URL,让方法内部格式化
                    media_type="image",
                )

        finally:
            pass

    async def _handle_video_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None,
        video_media_id: Optional[str] = None,
        video_bytes: Optional[bytes] = None,
        video_mime_type: str = "video/mp4",
        generation_options: Optional[Dict[str, Any]] = None,
        response_model: Optional[str] = None,
    ) -> AsyncGenerator:
        """处理视频生成 (异步轮询)"""

        try:
            self._validate_generation_options(model_config, generation_options, video_bytes)
        except ValueError as exc:
            self._mark_generation_failed(generation_result, str(exc))
            yield self._create_error_response(str(exc), status_code=400)
            return

        if response_state is None:
            response_state = self._create_response_state()

        video_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            video_trace = perf_trace.setdefault("video_generation", {})
            video_trace["input_image_count"] = len(images) if images else 0

        # The outer generation lifecycle owns the account concurrency slot.
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if video_trace is not None:
            video_trace.setdefault("slot_wait_ms", 0)

        await self._update_request_log_progress(
            request_log_state, token_id=token.id, status_text="preparing_video", progress=24
        )

        try:
            # 获取模型类型和配置
            video_type = model_config.get("video_type")
            min_images = model_config.get("min_images", 0)
            max_images = model_config.get("max_images", 0)
            use_v2_model_config = bool(model_config.get("use_v2_model_config", False))

            # 根据账号tier自动调整模型 key
            user_tier = normalized_tier

            original_model_key = model_config["model_key"]
            model_key, tier_message = self._resolve_video_model_key_for_tier(
                model_config, user_tier
            )
            if tier_message:
                if stream:
                    yield self._create_stream_chunk(f"{tier_message}\n")
                debug_logger.log_info(
                    f"[VIDEO] 账号层级模型调整: {original_model_key} -> {model_key}"
                )
            elif user_tier == "PAYGATE_TIER_TWO" and original_model_key == model_key:
                debug_logger.log_info(
                    f"[VIDEO] TIER_TWO 账号，未找到有效 ultra 变体，保持模型: {model_key}"
                )

            # 更新 model_config 中的 model_key
            model_config = dict(model_config)  # 创建副本避免修改原配置
            model_config["model_key"] = model_key

            # 图片数量
            image_count = len(images) if images else 0

            if video_type == "edit":
                if not video_bytes and not video_media_id:
                    error_msg = "Omni 视频编辑需要提供 video/* 素材"
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return
                if image_count > max_images:
                    error_msg = f"Omni 视频编辑最多支持 {max_images} 张参考图"
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return
            elif video_bytes:
                error_msg = "视频素材仅支持 omni_edit 模型"
                self._mark_generation_failed(generation_result, error_msg)
                yield self._create_error_response(error_msg, status_code=400)
                return

            # ========== 验证和处理图片 ==========

            # T2V: 文生视频 - 不支持图片
            if video_type == "t2v":
                if image_count > 0:
                    if stream:
                        yield self._create_stream_chunk(
                            "⚠️ 文生视频模型不支持上传图片,将忽略图片仅使用文本提示词生成\n"
                        )
                    debug_logger.log_warning(
                        f"[T2V] 模型 {model_config['model_key']} 不支持图片,已忽略 {image_count} 张图片"
                    )
                images = None  # 清空图片
                image_count = 0

            # Omni: 无图走 T2V，有图走当前上游 Reference Images 直连链路
            elif video_type == "omni":
                if max_images is not None and image_count > max_images:
                    error_msg = (
                        f"Omni 模型最多支持 {max_images} 张参考图，当前提供了 {image_count} 张"
                    )
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # I2V: 首尾帧模型 - 需要1-2张图片
            elif video_type == "i2v":
                if image_count < min_images or image_count > max_images:
                    error_msg = f"首尾帧模型需要 {min_images}-{max_images} 张图片，当前提供了 {image_count} 张"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # R2V: 多图生成 - 当前上游协议最多 3 张参考图
            elif video_type == "r2v":
                if image_count < min_images or (
                    max_images is not None and image_count > max_images
                ):
                    error_msg = f"参考图视频模型需要 {min_images}-{max_images} 张参考图，当前提供了 {image_count} 张"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # ========== 上传图片 ==========
            start_media_id = None
            end_media_id = None
            reference_images = []

            # I2V: 首尾帧处理
            if video_type == "i2v" and images:
                if image_count == 1:
                    # 只有1张图: 仅作为首帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] 仅上传首帧: {start_media_id}")

                elif image_count == 2:
                    # 2张图: 首帧+尾帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧和尾帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    end_media_id = await self.flow_client.upload_image(
                        token.at, images[1], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] 上传首尾帧: {start_media_id}, {end_media_id}")

            # R2V: 多图处理
            elif video_type == "r2v" and images:
                if stream:
                    yield self._create_stream_chunk(f"上传 {image_count} 张参考图片...\n")

                for img in images:
                    media_id = await self.flow_client.upload_image(
                        token.at, img, model_config["aspect_ratio"], project_id=project_id
                    )
                    reference_images.append(
                        {"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": media_id}
                    )
                debug_logger.log_info(f"[R2V] 上传了 {len(reference_images)} 张参考图片")

            # Omni R2V: 参考图上传到 project，随后直接走 batchAsyncGenerateVideoReferenceImages
            elif video_type in {"omni", "edit"} and images:
                if stream:
                    yield self._create_stream_chunk(f"上传 {image_count} 张 Omni 参考图片...\n")

                for img in images:
                    media_id = await self.flow_client.upload_image(
                        token.at, img, model_config["aspect_ratio"], project_id=project_id
                    )
                    reference_images.append(
                        {"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": media_id}
                    )
                debug_logger.log_info(
                    f"[VIDEO {video_type.upper()}] 上传了 {len(reference_images)} 张参考图片"
                )

            # ========== 调用生成API ==========
            if stream:
                yield self._create_stream_chunk("提交视频生成任务...\n")
            submit_started_at = time.time()

            # I2V: 首尾帧生成
            if video_type == "edit":
                source_media_id = video_media_id
                options = generation_options or {}
                trim_start_ms = options.get("trimStartMs", 0)
                trim_end_ms = options.get("trimEndMs", 10000)
                if video_bytes:
                    source_media_id = await self.flow_client.upload_video(
                        token.at,
                        video_bytes,
                        video_mime_type,
                        project_id=project_id,
                        trim_start_ms=trim_start_ms,
                        trim_end_ms=trim_end_ms,
                    )
                    # Upload already trims the source; edit the full resulting asset.
                    trim_start_ms, trim_end_ms = 0, trim_end_ms - trim_start_ms
                edit_kwargs = dict(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    video_media_id=source_media_id,
                    reference_images=reference_images,
                    trim_start_ms=trim_start_ms,
                    trim_end_ms=trim_end_ms,
                    uploaded_video=bool(video_bytes),
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )
                result = await self.flow_client.generate_video_edit(**edit_kwargs)
            elif video_type == "i2v" and start_media_id:
                if end_media_id:
                    # 有首尾帧
                    result = await self.flow_client.generate_video_start_end(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config["model_key"],
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        end_media_id=end_media_id,
                        use_v2_model_config=use_v2_model_config,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )
                else:
                    # 只有首帧 - 需要去掉 model_key 中的 _fl
                    # 情况1: _fl_ 在中间 (如 veo_3_1_i2v_s_fast_fl_ultra_relaxed -> veo_3_1_i2v_s_fast_ultra_relaxed)
                    # 情况2: _fl 在结尾 (如 veo_3_1_i2v_s_fast_ultra_fl -> veo_3_1_i2v_s_fast_ultra)
                    actual_model_key = model_config["model_key"].replace("_fl_", "_")
                    if actual_model_key.endswith("_fl"):
                        actual_model_key = actual_model_key[:-3]
                    debug_logger.log_info(
                        f"[I2V] 单帧模式，model_key: {model_config['model_key']} -> {actual_model_key}"
                    )
                    result = await self.flow_client.generate_video_start_image(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=actual_model_key,
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        use_v2_model_config=use_v2_model_config,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )

            # R2V: 多图生成
            elif video_type == "r2v" and reference_images:
                result = await self.flow_client.generate_video_reference_images(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    reference_images=reference_images,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )

            # Omni: 有图走 Reference Images 直连链路，无图走纯文本链路
            elif video_type == "omni" and reference_images:
                if stream:
                    yield self._create_stream_chunk("提交 Omni 参考图视频任务...\n")
                result = await self.flow_client.generate_video_reference_images(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config.get("reference_model_key", "abra_r2v_8s"),
                    aspect_ratio=model_config["aspect_ratio"],
                    reference_images=reference_images,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )

            # Extend: 视频续写
            elif video_type == "extend":
                if not video_media_id:
                    error_msg = "视频续写需要提供源视频的 mediaGenerationId，请在 image_url 中传入 extend://VIDEO_MEDIA_ID"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

                debug_logger.log_info(f"[EXTEND] 续写视频: {video_media_id}")
                if stream:
                    yield self._create_stream_chunk(
                        f"视频续写任务提交中，源视频: {video_media_id[:8]}...\n"
                    )
                result = await self.flow_client.generate_video_extend(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    video_media_id=video_media_id,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )

            # T2V 或 R2V无图: 纯文本生成
            else:
                result = await self.flow_client.generate_video_text(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    use_v2_model_config=use_v2_model_config,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )
            if video_trace is not None:
                video_trace["submit_generation_ms"] = int((time.time() - submit_started_at) * 1000)

            # 获取task_id和operations
            operations = result.get("operations", [])
            if not operations:
                self._mark_generation_failed(
                    generation_result, "\u751f\u6210\u4efb\u52a1\u521b\u5efa\u5931\u8d25"
                )
                yield self._create_error_response("生成任务创建失败", status_code=502)
                return

            operation = operations[0]
            task_id = operation["operation"]["name"]
            scene_id = operation.get("sceneId")

            # 保存Task到数据库
            task = Task(
                task_id=task_id,
                token_id=token.id,
                model=response_model or model_config["model_key"],
                prompt=prompt,
                status="processing",
                scene_id=scene_id,
            )
            await self.db.create_task(task)
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="video_submitted",
                progress=45,
                response_extra={"task_id": task_id, "scene_id": scene_id},
            )

            # 轮询结果
            if stream:
                yield self._create_stream_chunk("视频生成中...\n")

            # 检查是否需要放大
            upsample_config = model_config.get("upsample")

            # 如果是 extend，传入源视频 media_id 用于后续拼接
            extend_source_id = video_media_id if video_type == "extend" else None
            async for chunk in self._poll_video_result(
                token,
                project_id,
                operations,
                stream,
                upsample_config,
                generation_result,
                response_state,
                request_log_state,
                extend_source_media_id=extend_source_id,
            ):
                yield chunk

        finally:
            pass

    async def _poll_video_result(
        self,
        token,
        project_id: str,
        operations: List[Dict],
        stream: bool,
        upsample_config: Optional[Dict] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        extend_source_media_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """轮询视频生成结果

        Args:
            upsample_config: 放大配置 {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
        """

        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
        if response_state is None:
            response_state = self._create_response_state()

        max_attempts = config.max_poll_attempts
        poll_interval = config.poll_interval

        # 如果需要放大，轮询次数加倍（放大可能需要 30 分钟）
        if upsample_config:
            max_attempts = max_attempts * 3  # 放大需要更长时间

        consecutive_poll_errors = 0
        last_poll_error: Optional[Exception] = None
        max_consecutive_poll_errors = 3

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            try:
                result = await self.flow_client.check_video_status(token.at, operations)
                checked_operations = result.get("operations", [])
                consecutive_poll_errors = 0
                last_poll_error = None

                if not checked_operations:
                    continue

                operation = checked_operations[0]
                status = operation.get("status")

                # 状态更新 - 每20秒报告一次 (poll_interval=3秒, 20秒约7次轮询)
                progress_update_interval = 7  # 每7次轮询 = 21秒
                if stream and attempt % progress_update_interval == 0:  # 每20秒报告一次
                    progress = min(int((attempt / max_attempts) * 100), 95)
                    await self._update_request_log_progress(
                        request_log_state,
                        token_id=token.id,
                        status_text="video_polling",
                        progress=max(45, progress),
                        response_extra={"upstream_status": status},
                    )
                    yield self._create_stream_chunk(f"生成进度: {progress}%\n")

                # 检查状态
                if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                    try:
                        resolved_video = await self._resolve_video_asset(token, operation)
                    except Exception as redirect_error:
                        media_name = (
                            operation.get("mediaName")
                            or operation.get("name")
                            or operation["operation"].get("name")
                        )
                        error_msg = f"视频生成成功但获取媒体地址失败: {self._normalize_error_message(redirect_error)}"
                        debug_logger.log_warning(
                            f"[VIDEO POLL] 获取视频URL失败: media={media_name}, error={redirect_error}"
                        )
                        await self._fail_video_task(checked_operations, error_msg)
                        self._mark_generation_failed(generation_result, error_msg)
                        yield self._create_error_response(error_msg, status_code=502)
                        return

                    video_url = resolved_video["video_url"]
                    video_media_id = resolved_video["video_media_id"]
                    aspect_ratio = resolved_video["aspect_ratio"]
                    media_name = resolved_video["media_name"]
                    metadata = resolved_video["metadata"]
                    video_info = resolved_video["video_info"]

                    if not video_url:
                        media_name_for_fetch = operation.get("mediaName") or operation[
                            "operation"
                        ].get("name", "")
                        if media_name_for_fetch:
                            if stream:
                                yield self._create_stream_chunk(
                                    "视频生成完成，正在下载视频文件...\n"
                                )
                            try:
                                media_result = await self.flow_client.get_media(
                                    token.at, media_name_for_fetch
                                )
                                encoded_video = media_result.get("video", {}).get(
                                    "encodedVideo", ""
                                )
                                if encoded_video:
                                    cached_filename = await self.file_cache.cache_base64_video(
                                        encoded_video
                                    )
                                    video_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                                    video_info["fifeUrl"] = video_url
                                    debug_logger.log_info(
                                        f"[VIDEO] Video fetched via get_media and cached: {cached_filename}"
                                    )
                                else:
                                    debug_logger.log_error(
                                        "[VIDEO] get_media returned empty encodedVideo"
                                    )
                            except Exception as fetch_err:
                                debug_logger.log_error(
                                    f"[VIDEO] Failed to fetch video via get_media: {fetch_err}"
                                )

                    if not video_url:
                        error_msg = "视频生成成功但未获取到媒体地址"
                        await self._fail_video_task(checked_operations, error_msg)
                        self._mark_generation_failed(generation_result, error_msg)
                        yield self._create_error_response(error_msg, status_code=502)
                        return

                    video_info["url"] = video_url
                    video_info["mediaName"] = media_name
                    video_info["mediaGenerationId"] = video_media_id
                    metadata.setdefault("video", video_info)
                    operation["operation"]["metadata"] = metadata

                    # ========== 视频放大处理 ==========
                    if upsample_config and video_media_id:
                        if stream:
                            resolution_name = (
                                "4K" if "4K" in upsample_config["resolution"] else "1080P"
                            )
                            yield self._create_stream_chunk(
                                f"\n视频生成完成，开始 {resolution_name} 放大处理...（可能需要 30 分钟）\n"
                            )

                        try:
                            # 提交放大任务
                            upsample_result = await self.flow_client.upsample_video(
                                at=token.at,
                                project_id=project_id,
                                video_media_id=video_media_id,
                                aspect_ratio=aspect_ratio,
                                resolution=upsample_config["resolution"],
                                model_key=upsample_config["model_key"],
                                user_paygate_tier=normalized_tier,
                                token_id=token.id,
                                token_video_concurrency=token.video_concurrency,
                            )

                            upsample_operations = upsample_result.get("operations", [])
                            if upsample_operations:
                                if stream:
                                    yield self._create_stream_chunk("放大任务已提交，继续轮询...\n")

                                # 递归轮询放大结果（不再放大）
                                async for chunk in self._poll_video_result(
                                    token,
                                    project_id,
                                    upsample_operations,
                                    stream,
                                    None,
                                    generation_result,
                                    response_state,
                                    request_log_state,
                                ):
                                    yield chunk
                                return
                            else:
                                response_state.setdefault("warnings", []).append(
                                    {
                                        "code": "upscale_failed",
                                        "message": "Video upscaling failed; original returned",
                                    }
                                )
                                if stream:
                                    yield self._create_stream_chunk(
                                        "⚠️ 放大任务创建失败，返回原始视频\n"
                                    )
                        except Exception as e:
                            safe_error = getattr(self.flow_client, "_remote_safe_error", None)
                            log_error = (
                                safe_error(e, redact=True)[:240]
                                if callable(safe_error)
                                else f"{type(e).__name__}: request failed"
                            )
                            debug_logger.log_error(f"Video upsample failed: {log_error}")
                            response_state.setdefault("warnings", []).append(
                                {
                                    "code": "upscale_failed",
                                    "message": "Video upscaling failed; original returned",
                                }
                            )
                            retry_trace = getattr(e, "flow_retry_trace", None)
                            display_error = log_error
                            if isinstance(retry_trace, dict):
                                safe_trace = self._safe_upsample_retry_trace(retry_trace)
                                if safe_trace:
                                    response_state["upscale_retry_trace"] = safe_trace
                                    latest = (safe_trace.get("history") or [{}])[-1]
                                    reason = str(latest.get("reason") or "")
                                    display_error = str(latest.get("error") or display_error)
                                    display_error = {
                                        "pool_empty": "打码代理池没有启用的出口，请添加或启用代理",
                                        "pool_cooling": "打码代理池出口正在冷却，等待预算内未恢复",
                                        "pool_unhealthy": "打码代理池没有健康出口，请检查代理连接",
                                        "pool_busy": "打码服务繁忙，已用完本次重试预算",
                                        "provider_error": "打码服务异常，已用完本次重试预算",
                                        "recaptcha_rejected": "reCAPTCHA 验证被拒，已用完本次重试预算",
                                        "submission_error": "放大任务提交失败",
                                    }.get(reason, display_error)
                            if stream:
                                if isinstance(retry_trace, dict):
                                    safe_trace = self._safe_upsample_retry_trace(retry_trace) or {}
                                    attempts = int(safe_trace.get("attempts") or 0)
                                    maximum = int(safe_trace.get("max_attempts") or attempts or 1)
                                    submissions = int(safe_trace.get("submissions") or 0)
                                    yield self._create_stream_chunk(
                                        f"本次放大已尝试 {attempts} / {maximum} 次（包含首次）；"
                                        f"验证码尝试 {attempts} / {maximum} 次，实际提交 {submissions} 次\n"
                                    )
                                yield self._create_stream_chunk(
                                    f"⚠️ 放大失败: {display_error}，返回原始视频\n"
                                )

                    # ========== Extend 视频拼接 ==========
                    if (
                        extend_source_media_id
                        and video_media_id
                        and getattr(token, "browser_profile_source", "") != "cloak"
                    ):
                        try:
                            if stream:
                                yield self._create_stream_chunk(
                                    "\n视频续写完成，正在拼接完整视频...\n"
                                )
                            debug_logger.log_info(
                                f"[CONCAT] 开始拼接: original={extend_source_media_id[:12]}..., extend={video_media_id[:12]}..."
                            )

                            # 提交拼接任务
                            concat_result = await self.flow_client.run_concatenation(
                                at=token.at,
                                original_media_id=extend_source_media_id,
                                extend_media_id=video_media_id,
                            )

                            # 获取 operation name
                            concat_op = (
                                concat_result.get("operation", {})
                                .get("operation", {})
                                .get("name", "")
                            )
                            if concat_op:
                                if stream:
                                    yield self._create_stream_chunk("拼接任务已提交，等待完成...\n")

                                # 轮询拼接状态
                                concat_status = await self.flow_client.poll_concatenation_status(
                                    at=token.at,
                                    operation_name=concat_op,
                                    timeout=300,
                                    poll_interval=3,
                                )

                                concat_url = concat_status.get("outputUri", "")
                                if concat_url:
                                    # 如果是本地路径（/tmp/xxx.mp4），构造完整 URL
                                    if concat_url.startswith("/tmp/"):
                                        server_host = config.server_host or "0.0.0.0"
                                        server_port = config.server_port or 8000
                                        # 对外使用 localhost
                                        host = (
                                            "localhost" if server_host == "0.0.0.0" else server_host
                                        )
                                        concat_url = f"http://{host}:{server_port}{concat_url}"
                                    video_url = concat_url  # 替换为拼接后的完整视频 URL
                                    if stream:
                                        yield self._create_stream_chunk(
                                            "✅ 视频拼接完成！返回 16s 完整视频\n"
                                        )
                                    debug_logger.log_info(
                                        f"[CONCAT] 拼接成功: {concat_url[:80]}..."
                                    )
                                else:
                                    if stream:
                                        yield self._create_stream_chunk(
                                            "⚠️ 拼接完成但无 URL，返回续写片段\n"
                                        )
                            else:
                                debug_logger.log_warning("[CONCAT] 拼接任务创建失败，返回续写片段")
                                if stream:
                                    yield self._create_stream_chunk(
                                        "⚠️ 拼接任务创建失败，返回续写片段\n"
                                    )
                        except Exception as e:
                            import traceback

                            debug_logger.log_error(f"[CONCAT] 拼接失败: {str(e)}")
                            debug_logger.log_error(f"[CONCAT] traceback: {traceback.format_exc()}")
                            if stream:
                                yield self._create_stream_chunk(
                                    f"⚠️ 拼接失败: {str(e)}，返回续写片段\n"
                                )
                            # 拼接失败不影响返回，继续使用 extend 片段的 URL

                    # 缓存视频 (如果启用)
                    local_url = video_url
                    if config.cache_enabled:
                        await self._update_request_log_progress(
                            request_log_state,
                            token_id=token.id,
                            status_text="caching_video",
                            progress=92,
                        )
                        try:
                            if stream:
                                yield self._create_stream_chunk("正在缓存视频文件...\n")
                            cached_filename = await self.file_cache.download_and_cache(
                                video_url, "video"
                            )
                            local_url = (
                                f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                            )
                            if stream:
                                yield self._create_stream_chunk(
                                    "✅ 视频缓存成功,准备返回缓存地址...\n"
                                )
                        except Exception as e:
                            debug_logger.log_error(f"Failed to cache video: {str(e)}")
                            # 缓存失败不影响结果返回,使用原始URL
                            local_url = video_url
                            if stream:
                                cache_error = self._normalize_error_message(e, max_length=120)
                                yield self._create_stream_chunk(
                                    f"⚠️ 缓存失败: {cache_error}\n正在返回源链接...\n"
                                )
                    else:
                        if stream:
                            yield self._create_stream_chunk("缓存已关闭,正在返回源链接...\n")

                    # 更新数据库
                    task_id = operation["operation"]["name"]
                    await self.db.update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        result_urls=[local_url],
                        completed_at=time.time(),
                    )

                    # 存储URL用于日志记录
                    response_state["url"] = local_url
                    response_state["generated_assets"] = {
                        "type": "video",
                        "final_video_url": local_url,
                        "mediaGenerationId": video_media_id,
                        "mediaName": media_name,
                        "aspectRatio": aspect_ratio,
                        "model": resolved_video.get("model"),
                        "duration": resolved_video.get("duration"),
                    }

                    # 返回结果
                    self._mark_generation_succeeded(generation_result)

                    if stream:
                        yield self._create_stream_chunk(
                            f"<video src='{local_url}' data-media-id='{video_media_id}' controls style='max-width:100%'></video>",
                            finish_reason="stop",
                        )

                    else:
                        yield self._create_completion_response(
                            local_url,  # 直接传URL,让方法内部格式化
                            media_type="video",
                        )
                    return

                elif status == "MEDIA_GENERATION_STATUS_FAILED":
                    # 生成失败 - 提取错误信息
                    error_info = operation.get("operation", {}).get("error", {})
                    error_code = error_info.get("code", "unknown")
                    error_message = error_info.get("message", "未知错误")
                    failure_reasons = operation.get("operation", {}).get("failureReasons") or []
                    if failure_reasons:
                        error_message = f"{error_message}; reasons={'; '.join(str(x) for x in failure_reasons[:3])}"

                    # 更新数据库任务状态
                    await self._fail_video_task(
                        checked_operations, f"{error_message} (code: {error_code})"
                    )

                    # 返回友好的错误消息，提示用户重试
                    friendly_error = f"视频生成失败: {error_message}，请重试"
                    self._mark_generation_failed(generation_result, friendly_error)
                    if stream:
                        yield self._create_stream_chunk(f"错误: {friendly_error}\n")
                    yield self._create_error_response(friendly_error, status_code=502)
                    return

                elif status.startswith("MEDIA_GENERATION_STATUS_ERROR"):
                    # Treat upstream terminal error states as failed tasks.
                    error_msg = f"视频生成失败: {status}"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=502)
                    return

                elif status == "MEDIA_GENERATION_STATUS_ACTIVE" and attempt > 80:
                    # 如果持续4分钟（80次 * 3秒 = 240秒）依然是 ACTIVE 状态，则判定为卡死
                    error_msg = "视频生成超时 (上游卡顿超过4分钟，已自动取消)"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"错误: {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=504)
                    return

            except Exception as e:
                last_poll_error = e
                consecutive_poll_errors += 1
                debug_logger.log_error(f"Poll error: {str(e)}")
                if consecutive_poll_errors >= max_consecutive_poll_errors:
                    error_msg = f"视频状态查询失败: {self._normalize_error_message(e)}"
                    await self._fail_video_task(operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"错误: {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=502)
                    return
                continue

        # 超时
        if last_poll_error is not None:
            error_msg = f"视频状态查询持续失败: {self._normalize_error_message(last_poll_error)}"
        else:
            error_msg = f"视频生成超时 (已轮询 {max_attempts} 次)"
        await self._fail_video_task(operations, error_msg)
        self._mark_generation_failed(generation_result, error_msg)
        yield self._create_error_response(error_msg, status_code=504)

    # ========== 响应格式化 ==========

    @staticmethod
    def _attach_public_media(chunk, response_state, request_log_state, media_type):
        """Expose request-local assets directly; adapters never parse Markdown/HTML."""
        streaming = chunk.startswith("data: ")
        try:
            payload = json.loads(chunk[6:].strip() if streaming else chunk)
        except (ValueError, TypeError):
            return chunk
        if not isinstance(payload, dict) or "choices" not in payload:
            return chunk
        if "error" not in payload:
            payload["progress"] = min(99, int(request_log_state.get("progress", 0)))
            choices = payload.get("choices") or []
            final = choices and choices[0].get("finish_reason") == "stop"
            if final and response_state.get("url"):
                media = {"type": media_type, "url": response_state["url"]}
                assets = response_state.get("generated_assets") or {}
                if media_type == "video":
                    media["mime_type"] = "video/mp4"
                    if assets.get("mediaGenerationId"):
                        media["media_id"] = assets["mediaGenerationId"]
                payload["media"] = media
                choices[0].setdefault("delta" if streaming else "message", {})["media"] = media
                payload["progress"] = 100
            if response_state.get("warnings"):
                payload["warnings"] = list(response_state["warnings"])
        encoded = json.dumps(payload, ensure_ascii=False)
        return f"data: {encoded}\n\n" if streaming else encoded

    def _create_stream_chunk(
        self, content: str, role: str = None, finish_reason: str = None
    ) -> str:
        """创建流式响应chunk"""
        import json
        import time

        chunk = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "lay4flow",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }

        if role:
            chunk["choices"][0]["delta"]["role"] = role

        if finish_reason:
            chunk["choices"][0]["delta"]["content"] = content
        else:
            chunk["choices"][0]["delta"]["reasoning_content"] = content

        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    def _create_completion_response(
        self, content: str, media_type: str = "image", is_availability_check: bool = False
    ) -> str:
        """创建非流式响应

        Args:
            content: 媒体URL或纯文本消息
            media_type: 媒体类型 ("image" 或 "video")
            is_availability_check: 是否为可用性检查响应 (纯文本消息)

        Returns:
            JSON格式的响应
        """
        import json
        import time

        # 可用性检查: 返回纯文本消息
        if is_availability_check:
            formatted_content = content
        else:
            # 媒体生成: 根据媒体类型格式化内容为Markdown
            if media_type == "video":
                formatted_content = f"```html\n<video src='{content}' controls></video>\n```"
            else:  # image
                formatted_content = f"![Generated Image]({content})"

        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "lay4flow",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": formatted_content},
                    "finish_reason": "stop",
                }
            ],
        }

        return json.dumps(response, ensure_ascii=False)

    @staticmethod
    def _safe_upsample_retry_trace(trace: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(trace, dict):
            return None
        history = []
        for item in (trace.get("generation_attempts") or [])[:20]:
            if not isinstance(item, dict):
                continue
            submitted = bool(item.get("submitted", item.get("recaptcha_ok")))
            stage = "submission" if submitted else "captcha"
            reason = str(item.get("reason") or "")
            if reason not in {
                "pool_empty",
                "pool_cooling",
                "pool_unhealthy",
                "pool_busy",
                "provider_error",
                "recaptcha_rejected",
                "submission_error",
            }:
                reason = ""
            history.append(
                {
                    "attempt": int(item.get("attempt") or len(history) + 1),
                    "success": bool(item.get("success")),
                    "recaptcha_ok": bool(item.get("recaptcha_ok")),
                    "stage": stage,
                    "submitted": submitted,
                    "reason": reason,
                    "duration_ms": int(item.get("duration_ms") or 0),
                    "error": str(item.get("error") or "")[:240],
                }
            )
        if not history:
            return None
        return {
            "max_attempts": int(trace.get("max_retries") or len(history)),
            "attempts": len(history),
            "submissions": sum(item["submitted"] for item in history),
            "history": history,
        }

    @staticmethod
    def _safe_retry_trace(perf_trace: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        image_trace = perf_trace.get("image_generation") if isinstance(perf_trace, dict) else None
        trace = image_trace.get("upstream_trace") if isinstance(image_trace, dict) else None
        attempts = trace.get("generation_attempts") if isinstance(trace, dict) else None
        if not isinstance(attempts, list) or not attempts:
            return None
        history = []
        for item in attempts[:20]:
            if not isinstance(item, dict):
                continue
            history.append(
                {
                    "attempt": int(item.get("attempt") or len(history) + 1),
                    "success": bool(item.get("success")),
                    "recaptcha_ok": bool(item.get("recaptcha_ok")),
                    "duration_ms": int(item.get("duration_ms") or 0),
                    "error": str(item.get("error") or "")[:240],
                }
            )
        if not history:
            return None
        return {
            "max_attempts": int(trace.get("max_retries") or len(history)),
            "attempts": len(history),
            "history": history,
        }

    def _create_error_response(
        self,
        error_message: str,
        status_code: int = 500,
        retry_trace: Optional[Dict[str, Any]] = None,
    ) -> str:
        """创建错误响应"""
        import json

        error = {
            "error": {
                "message": error_message,
                "type": "server_error" if status_code >= 500 else "invalid_request_error",
                "code": "generation_failed",
                "status_code": status_code,
            }
        }
        if isinstance(retry_trace, dict):
            error["retry_trace"] = retry_trace

        return json.dumps(error, ensure_ascii=False)

    def _get_base_url(self, response_state: Optional[Dict[str, Any]] = None) -> str:
        """获取基础URL用于缓存文件访问"""
        # 已配置缓存访问域名时，始终优先使用它，避免被请求 Host/IP 覆盖。
        if config.cache_base_url:
            return config.cache_base_url.rstrip("/")

        request_base_url = ""
        if isinstance(response_state, dict):
            request_base_url = (response_state.get("base_url") or "").strip().rstrip("/")
        if request_base_url:
            return request_base_url

        # 回退到服务地址，避免把监听地址 0.0.0.0 / :: 直接返回给客户端
        server_host = (config.server_host or "").strip()
        if server_host in {"", "0.0.0.0", "::", "[::]"}:
            server_host = "127.0.0.1"

        return f"http://{server_host}:{config.server_port}"

    async def _update_request_log_progress(
        self,
        request_log_state: Optional[Dict[str, Any]],
        *,
        token_id: Optional[int] = None,
        status_text: str,
        progress: int,
        response_extra: Optional[Dict[str, Any]] = None,
    ):
        """Persist progress with throttling to avoid a write on every poll."""
        if not isinstance(request_log_state, dict):
            return
        log_id = request_log_state.get("id")
        if not log_id:
            return

        safe_progress = max(0, min(100, int(progress)))
        now = time.time()
        last_status_text = str(request_log_state.get("last_status_text") or "").strip()
        last_progress = int(request_log_state.get("last_progress") or 0)
        last_updated_at = float(request_log_state.get("last_progress_update_at") or 0)

        request_log_state["progress"] = safe_progress
        request_log_state["last_status_text"] = status_text
        request_log_state["last_progress"] = safe_progress
        payload = {
            "status": "processing",
            "status_text": status_text,
            "progress": safe_progress,
        }
        if isinstance(response_extra, dict):
            payload.update(response_extra)

        should_write = (
            safe_progress in (0, 100)
            or status_text != last_status_text
            or safe_progress >= last_progress + 5
            or (now - last_updated_at) >= 1.0
        )
        if not should_write:
            return

        request_log_state["last_progress_update_at"] = now

        try:
            await self.db.update_request_log(
                log_id,
                token_id=token_id,
                response_body=json.dumps(payload, ensure_ascii=False),
                status_code=102,
                duration=0,
                status_text=status_text,
                progress=safe_progress,
            )
        except Exception as e:
            debug_logger.log_error(f"Failed to update request log progress: {e}")

    async def _log_request(
        self,
        token_id: Optional[int],
        operation: str,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        status_code: int,
        duration: float,
        log_id: Optional[int] = None,
        status_text: Optional[str] = None,
        progress: Optional[int] = None,
    ):
        """Update the existing request log, or create one when no log_id is set."""
        try:
            effective_status_text = status_text or (
                "completed"
                if status_code == 200
                else "failed"
                if status_code >= 400
                else "processing"
            )
            effective_progress = progress
            if effective_progress is None:
                effective_progress = 100 if status_code == 200 else 0 if status_code >= 400 else 0
            effective_progress = max(0, min(100, int(effective_progress)))

            request_body = json.dumps(request_data, ensure_ascii=False)
            response_body = json.dumps(response_data, ensure_ascii=False)

            if log_id:
                await self.db.update_request_log(
                    log_id,
                    token_id=token_id,
                    operation=operation,
                    request_body=request_body,
                    response_body=response_body,
                    status_code=status_code,
                    duration=duration,
                    status_text=effective_status_text,
                    progress=effective_progress,
                )
                return log_id

            log = RequestLog(
                token_id=token_id,
                operation=operation,
                request_body=request_body,
                response_body=response_body,
                status_code=status_code,
                duration=duration,
                status_text=effective_status_text,
                progress=effective_progress,
            )
            return await self.db.add_request_log(log)
        except Exception as e:
            debug_logger.log_error(f"Failed to log request: {e}")
            return None
